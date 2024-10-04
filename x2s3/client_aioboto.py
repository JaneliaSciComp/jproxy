import os
import sys
import typing
from typing_extensions import override

from loguru import logger
from starlette.background import BackgroundTask
import botocore
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from aiobotocore.session import get_session
from aiobotocore.config import AioConfig
from fastapi.responses import Response, StreamingResponse, JSONResponse

from x2s3.utils import *
from x2s3.client import ProxyClient

def handle_s3_exception(e, key=None):
    """ Handle various cases of generic errors from the boto AWS API.
    """
    if isinstance(e, (NoCredentialsError, PartialCredentialsError)):
        logger.opt(exception=sys.exc_info()).error("AWS credentials not configured properly")
        return JSONResponse({"error":"AWS credentials not configured properly"}, status_code=408)
    elif isinstance(e, botocore.exceptions.ReadTimeoutError):
        return JSONResponse({"error":"Upstream endpoint timed out"}, status_code=408)
    elif isinstance(e, botocore.exceptions.ClientError):
        status_code = e.response['ResponseMetadata']['HTTPStatusCode']
        error = e.response['Error']
        error_code = error['Code'] if 'Code' in error else 'Unknown'
        if error_code == "NoSuchKey":
            return get_nosuchkey_response(key)
        else:
            message = error['Message'] if 'Message' in error else 'Unknown'
            resource = error['Resource'] if 'Resource' in error else 'Unknown'
            return get_error_response(status_code, error_code, message, resource)
    else:
        logger.opt(exception=sys.exc_info()).error("Error communicating with AWS S3")
        return JSONResponse({"error":"Error communicating with AWS S3"}, status_code=500)


class AiobotoProxyClient(ProxyClient):

    def __init__(self, proxy_kwargs, **kwargs):

        self.proxy_kwargs = proxy_kwargs or {}
        self.target_name = self.proxy_kwargs['target_name']
        self.bucket_name = kwargs['bucket']
        self.bucket_prefix = kwargs.get('prefix')

        self.anonymous = True
        access_key,secret_key = '',''

        if 'access_key_path' in kwargs:
            self.anonymous = False
            access_key_path = kwargs['access_key_path']
            secret_key_path = kwargs['secret_key_path']

            with open(access_key_path, 'r') as ak_file:
                access_key = ak_file.read().strip()

            with open(secret_key_path, 'r') as sk_file:
                secret_key = sk_file.read().strip()

        self.client_kwargs = {
            'aws_access_key_id': access_key,
            'aws_secret_access_key': secret_key,
        }

        if 'endpoint' in kwargs:
            self.client_kwargs['endpoint_url'] = kwargs.get('endpoint')


    def get_client_creator(self):
        session = get_session()
        conf = AioConfig(signature_version=botocore.UNSIGNED) if self.anonymous else AioConfig()
        return session.create_client('s3', config=conf, **self.client_kwargs)


    @override
    async def head_object(self, key: str):
        real_key = key
        if self.bucket_prefix:
            real_key = os.path.join(self.bucket_prefix, key) if key else self.bucket_prefix

        async with self.get_client_creator() as client:
            try:
                s3_res = await client.head_object(Bucket=self.bucket_name, Key=real_key)
                headers = {
                    "ETag": s3_res.get("ETag"),
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(s3_res.get("ContentLength")),
                    "Last-Modified": s3_res.get("LastModified").strftime("%a, %d %b %Y %H:%M:%S GMT"),
                }

                content_type = guess_content_type(real_key)
                headers['Content-Type'] = content_type

                return Response(headers=headers)
            except Exception as e:
                return handle_s3_exception(e, key)


    @override
    async def get_object(self, key: str, range_header: str = None):
        real_key = key
        if self.bucket_prefix:
            real_key = os.path.join(self.bucket_prefix, key) if key else self.bucket_prefix

        filename = os.path.basename(real_key)
        content_type = guess_content_type(filename)

        headers = {
            'Accept-Ranges': "bytes",
            'Content-Type': content_type,
        }

        if content_type=='application/octet-stream':
            headers['Content-Disposition'] = f'attachment; filename="{filename}"'

        try:
            return S3Stream(
                self.get_client_creator,
                headers=headers,
                media_type=content_type,
                bucket=self.bucket_name,
                key=key,
                real_key=real_key,
                range_header=range_header,
                )
        except Exception as e:
            return handle_s3_exception(e, key)


    @override
    async def list_objects_v2(self,
                            continuation_token: str,
                            delimiter: str,
                            encoding_type: str,
                            fetch_owner: str,
                            max_keys: str,
                            prefix: str,
                            start_after: str):

        # prefix user-supplied prefix with configured prefix
        real_prefix = prefix
        if self.bucket_prefix:
            real_prefix = os.path.join(self.bucket_prefix, prefix) if prefix else self.bucket_prefix

        # ensure the prefix ends with a slash
        if real_prefix and not real_prefix.endswith('/'):
            real_prefix += '/'

        async with self.get_client_creator() as client:
            try:
                params = {
                    "Bucket": self.bucket_name,
                    "ContinuationToken": continuation_token,
                    "Delimiter": delimiter,
                    "EncodingType": encoding_type,
                    "FetchOwner": fetch_owner,
                    "MaxKeys": max_keys,
                    "Prefix": real_prefix,
                    "StartAfter": start_after
                }
                # Remove any None values because boto3 doesn't like those
                params = {k: v for k, v in params.items() if v is not None}

                response = await client.list_objects_v2(**params)
                next_token = remove_prefix(self.bucket_prefix, response.get("NextContinuationToken", ""))
                is_truncated = "true" if response.get("IsTruncated", False) else "false"

                contents = []
                for obj in response.get("Contents", []):
                    contents.append({
                        'Key': remove_prefix(self.bucket_prefix, obj["Key"]),
                        'LastModified': obj["LastModified"].isoformat(),
                        'ETag': obj.get("ETag"),
                        'Size': obj.get("Size"),
                        'StorageClass': obj.get("StorageClass")
                    })

                common_prefixes = []
                for cp in response.get("CommonPrefixes", []):
                    common_prefix = remove_prefix(self.bucket_prefix, cp["Prefix"])
                    common_prefixes.append(common_prefix)

                kwargs = {
                    'Name': self.target_name,
                    'Prefix': prefix,
                    'Delimiter': delimiter,
                    'MaxKeys': max_keys,
                    'EncodingType': encoding_type,
                    'KeyCount': response.get("KeyCount", 0),
                    'IsTruncated': is_truncated,
                    'ContinuationToken': continuation_token,
                    'NextContinuationToken': next_token,
                    'StartAfter': start_after
                }

                xml = get_list_xml(contents, common_prefixes, **kwargs)
                return Response(content=xml, media_type="application/xml")

            except Exception as e:
                return handle_s3_exception(e, key=prefix)


# Adapted from https://stackoverflow.com/questions/69617252/response-file-stream-from-s3-fastapi
class S3Stream(StreamingResponse):
    """ Stream the result of GetObject.
    """
    def __init__(
            self,
            client_creator: typing.Callable,
            content: typing.Any = None,
            status_code: int = 200,
            headers: dict = None,
            media_type: str = None,
            background: BackgroundTask = None,
            bucket: str = None,
            key: str = None,
            real_key: str = None,
            range_header: str = None
    ):
        super(S3Stream, self).__init__(content, status_code, headers, media_type, background)
        self.client_creator = client_creator
        self.bucket = bucket
        self.key = key
        self.real_key = real_key
        self.range_header = range_header

    async def stream_response(self, send) -> None:

        async def send_response(r):
            await send({
                "type": "http.response.start",
                "status": r.status_code,
                "headers": r.raw_headers,
            })
            await send({
                "type": "http.response.body",
                "body": r.body,
                "more_body": False,
            })

        async with self.client_creator() as client:
            result = None
            try:
                # Get the object with the range specified in headers
                get_object_params = {
                    "Bucket": self.bucket,
                    "Key": self.real_key,
                }
                if self.range_header:
                    get_object_params["Range"] = self.range_header

                result = await client.get_object(**get_object_params)
                res_headers = result["ResponseMetadata"]["HTTPHeaders"]

                # Determine if this is a Range result
                if "content-range" in res_headers:
                    self.status_code = 206 # Partial Content
                    self.raw_headers.append((b"content-range",
                        res_headers["content-range"].encode('utf-8')))
                    
                if "content-length" in res_headers:
                    self.raw_headers.append((b"content-length",
                        res_headers["content-length"].encode('utf-8')))

                await send({
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                })

                async for chunk in result["Body"]:

                    if not isinstance(chunk, bytes):
                        chunk = chunk.encode(self.charset)

                    await send({
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True
                    })

                await send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False})

            except Exception as e:
                r = handle_s3_exception(e, self.key)
                await send_response(r)
