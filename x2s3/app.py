import os
import sys
from typing import Optional

from loguru import logger
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from x2s3.utils import *
from x2s3 import registry
from x2s3.settings import get_settings

def create_app(settings):

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET","HEAD"],
        allow_headers=["*"],
        expose_headers=["Range", "Content-Range"],
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
        if callable(settings):
            app.settings = settings()
        else:
            app.settings = settings

        # Configure logging
        logger.remove()
        logger.add(sys.stderr, level=app.settings.log_level)

        logger.trace("Available protocols:")
        for proto in registry.available_protocols():
            logger.trace(f"- {proto}")

        # Configure targets
        app.clients = {}
        for target_name in app.settings.get_target_map():
            target_key = target_name.lower()
            target_config = app.settings.get_target_config(target_key)
            proxy_kwargs = {
                'target_name': target_name,
            }

            client = registry.client(target_config.client,
                proxy_kwargs, **target_config.options)

            if target_key in app.clients:
                logger.warning(f"Overriding target key: {target_key}")

            app.clients[target_key] = client
            logger.debug(f"Configured target {target_name}")

        logger.info(f"Server ready with {len(app.clients)} targets")


    def get_client(target_name):
        target_key = target_name.lower()
        if target_key in app.clients:
            return app.clients[target_key]
        return None


    def get_target(request, path):
        target_path = path
        base_url = app.settings.base_url
        
        logger.trace(f"base_url: {base_url}")
        logger.trace(f"request.url.hostname: {request.url.hostname}")

        subdomain = None
        if app.settings.virtual_buckets:
            if base_url:
                subdomain = request.url.hostname.removesuffix(base_url.host).removesuffix('.')
            else:
                logger.warning("virtual_buckets enabled but no base URL is configured")

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
        if client is None:
            raise HTTPException(status_code=500, detail="Client for target bucket not found")

        response = await client.list_objects_v2(continuation_token, '/', None,
                                                False, max_keys, prefix, None)

        if response.status_code != 200:
            # Return error respone
            return response

        xml = response.body.decode("utf-8")
        root = parse_xml(xml)

        common_prefixes = []
        cps = [c for c in root.findall('CommonPrefixes')]
        if cps:
            for cp in cps:
                common_prefixes += [dir_path(e.text) for e in cp.iter('Prefix')] if cps else []

        contents = []
        cs = [c for c in root.findall('Contents')]
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
                        content['lastmod'] = format_isoformat_as_local(lm_elem.text)

                    contents.append(content)

        next_token = None
        truncated_elem = root.find('IsTruncated')
        if truncated_elem is not None and truncated_elem.text=='true':
            next_ct_elem = root.find('NextContinuationToken')
            next_token = next_ct_elem.text

        target_prefix = '' if is_virtual else '/'+target_name
        parent_prefix = dir_path(os.path.dirname(prefix.rstrip('/')))

        return templates.TemplateResponse("browse.html", {
            "request": request,
            "prefix": prefix,
            "index_url": app.settings.base_url or '/',
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
                                list_type: int = Query(None, alias="list-type"),
                                continuation_token: Optional[str] = Query(None, alias="continuation-token"),
                                delimiter: Optional[str] = Query(None, alias="delimiter"),
                                encoding_type: Optional[str] = Query(None, alias="encoding-type"),
                                fetch_owner: Optional[bool] = Query(None, alias="fetch-owner"),
                                max_keys: Optional[int] = Query(1000, alias="max-keys"),
                                prefix: Optional[str] = Query(None, alias="prefix"),
                                start_after: Optional[str] = Query(None, alias="start-after")):

        target_name, target_path, is_virtual = get_target(request, path)
        logger.debug(f"target_name={target_name}, target_path={target_path}, is_virtual={is_virtual}")

        if not target_name or (is_virtual and target_name=='www'):
            # Return target index
            bucket_list = { target: f"/{target}/" for target in app.settings.get_browseable_targets()}
            if app.settings.ui:
                return templates.TemplateResponse("index.html", {"request": request, "links": bucket_list})
            else:
                xml = get_bucket_list_xml(bucket_list)
                return Response(content=xml, status_code=200, media_type="application/xml")
        
        target_config = app.settings.get_target_config(target_name)
        if not target_config:
            return get_nosuchbucket_response(target_name)

        client = get_client(target_name)
        if client is None:
            raise HTTPException(status_code=500, detail="Client for target bucket not found")

        if 'acl' in request.query_params:
            return get_read_access_acl()

        if list_type:
            if not target_path:
                if list_type == 2:
                    return await client.list_objects_v2(continuation_token, delimiter, \
                        encoding_type, fetch_owner, max_keys, prefix, start_after)
                else:
                    raise HTTPException(status_code=400, detail="Invalid list type")
            else:
                range_header = request.headers.get("range")
                return await client.get_object(target_path, range_header)

        if not target_path or target_path.endswith("/"):
            if app.settings.ui:
                return await browse_bucket(request, target_name, target_path,
                    continuation_token=continuation_token,
                    max_keys=100,
                    is_virtual=is_virtual)
            else:
                return get_nosuchbucket_response(target_name)
        else:
            range_header = request.headers.get("range")
            return await client.get_object(target_path, range_header)



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
            if client is None:
                raise HTTPException(status_code=500, detail="Client for target bucket not found")

            return await client.head_object(target_path)
        except:
            logger.opt(exception=sys.exc_info()).info("Error requesting head")
            return JSONResponse({"error":"Error requesting HEAD"}, status_code=500)

    return app


app = create_app(get_settings)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
