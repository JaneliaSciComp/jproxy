import os
import sys
from typing import Optional

from loguru import logger
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import Response, HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import HttpUrl

from jproxy.utils import *
from jproxy.settings import get_settings, S3LikeTarget
#from jproxy.proxy_fsspec import FSSpecProxyClient
#from jproxy.proxy_boto3 import Boto3ProxyClient
from jproxy.proxy_aioboto import AiobotoProxyClient


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET","HEAD"],
    allow_headers=["Content-Type"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse({"error":str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return JSONResponse({"error":str(exc)}, status_code=400)


@app.on_event("startup")
async def startup_event():
    """ Runs once when the service is first starting.
        Reads the configuration and sets up the proxy clients. 
    """
    app.settings = get_settings()
    app.clients = {}

    for target_name in app.settings.get_targets():
        target_key = target_name.lower()
        target_config = app.settings.get_target_config(target_key)

        if isinstance(target_config, S3LikeTarget):
            client = AiobotoProxyClient(target_config)
        else:
            raise RuntimeError(f"Unknown target type: {type(target_config)}")

        app.clients[target_key] = client
        logger.info(f"Configured target {target_name}")


def get_client(target_name):
    target_key = target_name.lower()
    if target_key in app.clients:
        return app.clients[target_key]
    return None


def get_read_access_acl():
    """ Returns an S3 ACL that grants full read access
    """
    acl_xml = """
    <AccessControlPolicy>
        <Owner>
            <ID>1</ID>
            <DisplayName>unknown</DisplayName>
        </Owner>
        <AccessControlList>
            <Grant>
                <Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Group">
                    <URI>http://acs.amazonaws.com/groups/global/AllUsers</URI>
                </Grantee>
                <Permission>READ</Permission>
            </Grant>
        </AccessControlList>
    </AccessControlPolicy>
    """
    return Response(content=acl_xml, media_type="application/xml")


def get_target(request, path):
    target_path = path
    base_url = app.settings.base_url
    subdomain = request.url.hostname.removesuffix(base_url.host).removesuffix('.')
    logger.trace(f"subdomain: {subdomain}")
    if subdomain:
        # Target is given in the subdomain
        is_virtual = True
        target_name = subdomain.split('.')[0]
    else:
        # Target is encoded as the first element in the path
        is_virtual = False
        # Extract target from path
        ts = target_path.removeprefix('/').split('/', maxsplit=1)
        logger.trace(f"target path components: {ts}")
        if len(ts)==2:
            target_name, target_path = ts
        elif len(ts)==1:
            # This happens if we are at the root of the proxy
            target_name, target_path = ts[0], ''
        else:
            # This shouldn't happen
            target_name, target_path = None, ''

    logger.trace(f"target_name={target_name}, target_path={target_path}, is_virtual={is_virtual}")
    return target_name, target_path, is_virtual


async def browse_bucket(request: Request,
                        target_name: str,
                        prefix: str,
                        continuation_token: str = None,
                        max_keys: int = 10,
                        is_virtual: bool = False):
    
    target_config = app.settings.get_target_config(target_name)
    if not target_config:
        raise HTTPException(status_code=404, detail="Target bucket not found")

    client = get_client(target_name)
    bucket_name = target_config.bucket

    parent_prefix = dir_path(os.path.dirname(prefix.rstrip('/')))
    response = await client.list_objects_v2(continuation_token, '/', None,
                                            False, max_keys, prefix, None)

    if response.status_code != 200:
        # Return error respone
        return response

    xml = response.body.decode("utf-8")
    root = parse_xml(xml)

    cps = root.find('CommonPrefixes')
    common_prefixes = [dir_path(e.text) for e in cps.iter('Prefix')] if cps else []

    contents = []
    cs =[c for c in root.findall('Contents')]
    if cs:
        for c in cs:
            key_elem = c.find('Key')
            if key_elem is not None and key_elem.text != prefix:

                content = {'key': key_elem.text}

                size_elem = c.find('Size')
                if size_elem is not None and size_elem.text:
                    num_bytes = int(size_elem.text)
                    content['size'] = humanize_bytes(num_bytes)

                lm_elem = c.find('LastModified')
                if lm_elem is not None and lm_elem.text:
                    content['lastmod'] = lm_elem.text

                contents.append(content)

    next_token = None
    truncated_elem = root.find('IsTruncated')
    if truncated_elem is not None and truncated_elem.text=='true':
        next_ct_elem = root.find('NextContinuationToken')
        next_token = next_ct_elem.text

    target_prefix = ''
    if not is_virtual:
        target_prefix = '/'+target_name

    return templates.TemplateResponse("browse.html", {
        "request": request,
        "bucket_name": bucket_name,
        "prefix": prefix,
        "target_prefix": target_prefix,
        "common_prefixes": common_prefixes,
        "contents": contents,
        "parent_prefix": parent_prefix,
        "remove_prefix": remove_prefix,
        "continuation_token": next_token
    })



@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    return FileResponse('static/favicon.ico')


@app.get('/robots.txt', response_class=PlainTextResponse)
def robots():
    return """User-agent: *\nDisallow: /"""


@app.get("/{path:path}")
async def target_dispatcher(request: Request,
                            path: str,
                            acl: str = Query(None),
                            list_type: int = Query(None, alias="list-type"),
                            continuation_token: Optional[str] = Query(None, alias="continuation-token"),
                            delimiter: Optional[str] = Query('/', alias="delimiter"),
                            encoding_type: Optional[str] = Query(None, alias="encoding-type"),
                            fetch_owner: Optional[bool] = Query(None, alias="fetch-owner"),
                            max_keys: Optional[int] = Query(1000, alias="max-keys"),
                            prefix: Optional[str] = Query(None, alias="prefix"),
                            start_after: Optional[str] = Query(None, alias="start-after")):

    target_name, target_path, is_virtual = get_target(request, path)
    if not target_name:
        # Return target index
        bucket_list = { target: f"/{target}/" for target in app.settings.get_targets()}
        return templates.TemplateResponse("index.html", {"request": request, "links": bucket_list})

    target_config = app.settings.get_target_config(target_name)
    if not target_config:
        return get_nosuchbucket_response(target_name)

    client = get_client(target_name)

    if acl is not None:
        return get_read_access_acl()

    if list_type:
        if not target_path:
            if list_type == 2:
                return await client.list_objects_v2(continuation_token, delimiter, \
                    encoding_type, fetch_owner, max_keys, prefix, start_after)
            else:
                raise HTTPException(status_code=400, detail="Invalid list type")
        else:
            return await client.get_object(target_path)

    if not target_path or target_path.endswith("/"):
        return await browse_bucket(request, target_name, target_path,
            continuation_token=continuation_token,
            max_keys=100,
            is_virtual=is_virtual)
    else:
        return await client.get_object(target_path)



@app.head("{path:path}")
async def head_object(request: Request, path: str):

    target_name, target_path, _ = get_target(request, path)
    if not target_name:
        return get_nosuchbucket_response('')

    try:
        target_config = app.settings.get_target_config(target_name)
        if not target_config:
            return get_nosuchbucket_response(target_name)

        client = get_client(target_name)
        client_prefix = target_config.prefix

        key = target_path
        if client_prefix:
            key = os.path.join(client_prefix, key) if key else client_prefix

        return await client.head_object(key)
    except:
        logger.opt(exception=sys.exc_info()).info("Error requesting head")
        return JSONResponse({"error":"Error requesting HEAD"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
