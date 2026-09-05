"""
Dynamic Multi-Tenant API Gateway Router
Enables drop-in OpenAlgo URL compatibility (https://domain.com/api/v1/...) by automatically
resolving the tenant container matching the request's API Key or Webhook Secret.
"""
import os
import time
import json
import logging
import httpx
from fastapi import APIRouter, Request, Response, HTTPException, status
from fastapi.responses import JSONResponse
from contextlib import closing

import security
import docker_manager
from database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["OpenAlgo API Gateway"])

# In-memory fast cache mapping API_KEY / WEBHOOK_SECRET -> tenant_id
KEY_CACHE = {}
LAST_CACHE_REFRESH = 0
CACHE_TTL = 30.0 # 30 seconds

def refresh_key_cache(force: bool = False):
    global LAST_CACHE_REFRESH, KEY_CACHE
    now = time.time()
    if not force and (now - LAST_CACHE_REFRESH < CACHE_TTL) and KEY_CACHE:
        return

    new_cache = {}
    try:
        with closing(get_db_connection()) as conn:
            rows = conn.execute("SELECT tenant_id, encrypted_payload FROM tenant_credentials").fetchall()
            for r in rows:
                tid = r["tenant_id"]
                try:
                    creds = security.decrypt_credentials(r["encrypted_payload"])
                    api_key = str(creds.get("API_KEY", "")).strip()
                    wh_secret = str(creds.get("WEBHOOK_SECRET", "")).strip()
                    md_key = str(creds.get("MD_API_KEY", "")).strip()
                    client_id = str(creds.get("CLIENT_ID", "")).strip()

                    if api_key: new_cache[api_key] = tid
                    if wh_secret: new_cache[wh_secret] = tid
                    if md_key: new_cache[md_key] = tid
                    if client_id: new_cache[f"client_{client_id}"] = tid
                    new_cache[tid] = tid
                except Exception as dec_err:
                    logger.warning(f"Gateway failed decrypting credentials for tenant {tid}: {dec_err}")
        KEY_CACHE = new_cache
        LAST_CACHE_REFRESH = now
    except Exception as e:
        logger.error(f"Error refreshing API Gateway key cache: {e}")

def resolve_tenant_id(key: str) -> str | None:
    if not key:
        return None
    refresh_key_cache()
    if key in KEY_CACHE:
        return KEY_CACHE[key]
    # Retry with forced refresh in case new client was just provisioned
    refresh_key_cache(force=True)
    return KEY_CACHE.get(key)

@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def gateway_dispatch(request: Request, path: str):
    """
    Catch-all dispatcher for /api/v1/* requests.
    Inspects headers, query params, and body to locate API key and route to appropriate container.
    """
    # 1. Check Ping shortcut without tenant resolution
    if path == "ping":
        return {"status": "success", "message": "pong", "service": "XTS Multi-Tenant Gateway"}

    # 2. Extract API Key / Secret from headers, params, or body
    api_key = request.headers.get("x-api-key") or ""
    if not api_key:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            api_key = auth[7:].strip()

    if not api_key:
        api_key = request.query_params.get("apikey") or request.query_params.get("secret") or ""

    body_bytes = await request.body()
    body_json = None
    if not api_key and body_bytes:
        try:
            body_json = json.loads(body_bytes.decode())
            api_key = str(body_json.get("apikey") or body_json.get("secret") or body_json.get("api_key") or "").strip()
        except Exception:
            pass

    tenant_id = resolve_tenant_id(api_key)
    if not tenant_id:
        client_session = request.cookies.get("client_session")
        if client_session:
            import security
            ip = request.client.host if request.client else "127.0.0.1"
            ua = request.headers.get("user-agent", "")
            u = security.validate_client_session(client_session, ip, ua)
            if u and "tenant_id" in u:
                tenant_id = u["tenant_id"]

    if not tenant_id:
        logger.warning(f"API Gateway: Request to /api/v1/{path} rejected - invalid or missing API key '{api_key[:6]}***'")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "message": "Invalid API key or matching tenant container not found"}
        )

    # 3. Resolve destination container URLs
    port = docker_manager.get_tenant_port(tenant_id)
    caddy_base = os.environ.get("CADDY_PROXY_BASE", "http://caddy/internal-client-proxy")
    
    url_local = f"http://127.0.0.1:{port}/api/v1/{path}"
    url_docker = f"http://xts_client_{tenant_id}:8000/api/v1/{path}"
    url_caddy = f"{caddy_base}/{tenant_id}/api/v1/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    # 4. Proxy request with timeout
    async with httpx.AsyncClient(timeout=10.0) as client:
        for dest_url in [url_local, url_docker, url_caddy]:
            try:
                resp = await client.request(
                    method=request.method,
                    url=dest_url,
                    headers=headers,
                    params=request.query_params,
                    content=body_bytes
                )
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json")
                )
            except Exception:
                continue

    return JSONResponse(
        status_code=502,
        content={"status": "error", "message": f"Tenant container for '{tenant_id}' is unreachable or starting up"}
    )
