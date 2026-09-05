from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager, closing
from typing import Optional, List, Dict, Any
import os
import time
import json
import logging
import datetime
import uuid
import asyncio
import httpx

import database
import security
import docker_manager
import caddy_manager
import telemetry_service
import strategy_parser

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

PORTAL_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(PORTAL_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

def format_inr(val, decimals=2):
    try:
        if val is None:
            val = 0.0
        f_val = float(val)
        if decimals == 0:
            return f"{f_val:,.0f}"
        return f"{f_val:,.{decimals}f}"
    except Exception:
        return "0.00" if decimals > 0 else "0"

def format_epoch_to_ist(val):
    if not val:
        return "N/A"
    try:
        IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        return datetime.datetime.fromtimestamp(float(val), tz=IST).strftime("%H:%M:%S")
    except Exception:
        return str(val)

templates.env.filters["inr"] = format_inr
templates.env.filters["num"] = lambda v: format_inr(v, decimals=0)
templates.env.filters["abs"] = lambda v: abs(float(v)) if v is not None else 0.0
templates.env.filters["epoch_to_ist"] = format_epoch_to_ist

DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "trading.yourdomain.com")

def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("admin_session")
    if not token:
        return None
    ip = request.client.host or "127.0.0.1"
    ua = request.headers.get("user-agent", "")
    return security.validate_session(token, ip, ua)

def require_auth(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/admin/login"}
        )
    path = request.url.path
    if not user.get("is_2fa_enabled") and path not in ("/admin/2fa-setup", "/admin/2fa-confirm", "/admin/logout"):
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/admin/2fa-setup"}
        )
    return user

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("--- BOOTING XTS ADMIN PORTAL ---")
    database.init_portal_db()
    
    default_user = os.environ.get("PORTAL_ADMIN_USER", "admin")
    default_pass = os.environ.get("PORTAL_ADMIN_PASSWORD", "AdminPass123!")
    
    with closing(database.get_db_connection()) as conn:
        with conn:
            existing = conn.execute("SELECT id FROM admin_users WHERE username=?", (default_user,)).fetchone()
            if not existing:
                admin_id = str(uuid.uuid4())
                pass_hash = security.hash_password(default_pass)
                conn.execute(
                    "INSERT INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (admin_id, default_user, pass_hash, time.time())
                )
                logger.info(f"Initialized default admin account: {default_user}")

    caddy_manager.sync_caddy_config()
    import scheduler
    asyncio.create_task(scheduler.start_scheduler_loop())
    logger.info("--- XTS ADMIN PORTAL READY (Scheduler Active) ---")
    yield

app = FastAPI(title="XTS Multi-Tenant Admin Portal", lifespan=lifespan)

import api_gateway
app.include_router(api_gateway.router)

STATIC_DIR = os.path.join(PORTAL_DIR, "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# =====================================================================
# CLIENT PORTAL AUTHENTICATION & ROLE-BASED ACCESS
# =====================================================================

def get_current_client_user(request: Request) -> dict | None:
    token = request.cookies.get("client_session")
    if not token:
        return None
    ip = request.client.host or "127.0.0.1"
    ua = request.headers.get("user-agent", "")
    return security.validate_client_session(token, ip, ua)

def require_client_auth(request: Request) -> dict:
    user = get_current_client_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/client/login"}
        )
    return user

@app.get("/client/login", response_class=HTMLResponse)
async def client_login_page(request: Request, error: str = None):
    if get_current_client_user(request):
        return RedirectResponse(url="/client/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="client_login.html", context={"error": error, "current_user": None})

@app.post("/client/login")
async def client_login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    ip = request.client.host or "127.0.0.1"
    ua = request.headers.get("user-agent", "")
    user = database.get_client_user_by_username(username.strip())

    if not user or not user.get("is_active") or not security.verify_password(password, user["password_hash"]):
        database.record_audit(username, "FAILED_CLIENT_LOGIN", {"ip": ip})
        return templates.TemplateResponse(request=request, name="client_login.html", context={"error": "Invalid client credentials", "current_user": None})

    session_token = security.create_client_session(user["id"], user["tenant_id"], ip, ua)
    database.record_audit(username, "SUCCESSFUL_CLIENT_LOGIN", {"ip": ip, "tenant_id": user["tenant_id"]})
    resp = RedirectResponse(url="/client/dashboard", status_code=303)
    resp.set_cookie(key="client_session", value=session_token, httponly=True, samesite="lax")
    return resp

@app.get("/client/logout")
async def client_logout(request: Request):
    token = request.cookies.get("client_session")
    if token:
        security.destroy_client_session(token)
    resp = RedirectResponse(url="/client/login", status_code=303)
    resp.delete_cookie(key="client_session")
    return resp

def get_tenant_trading_mode(tenant_id: str) -> str:
    with closing(database.get_db_connection()) as conn:
        row = conn.execute("SELECT trading_mode, paper_trade_mode FROM tenant_risk_limits WHERE tenant_id=?", (tenant_id,)).fetchone()
        if row:
            if row["trading_mode"]:
                return str(row["trading_mode"]).upper()
            return "PAPER" if row["paper_trade_mode"] else "LIVE"
    return "LIVE"

@app.get("/client/dashboard", response_class=HTMLResponse)
async def client_dashboard_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_dashboard.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "dashboard",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/trading", response_class=HTMLResponse)
async def client_trading_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_trading.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_tab": "trading",
        "active_page": "trading",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/orders", response_class=HTMLResponse)
async def client_orders_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_orders.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "orders",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/positions", response_class=HTMLResponse)
async def client_positions_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_positions.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "positions",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/options", response_class=HTMLResponse)
async def client_options_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_options.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "options",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/strategies", response_class=HTMLResponse)
async def client_strategies_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_strategies.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "strategies",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/strategy", response_class=HTMLResponse)
@app.get("/strategies", response_class=HTMLResponse)
async def strategies_redirect_page(request: Request):
    return RedirectResponse(url="/client/strategies", status_code=307)

@app.get("/client/logs", response_class=HTMLResponse)
async def client_logs_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_logs.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "logs",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/logs", response_class=HTMLResponse)
async def logs_redirect_page(request: Request):
    return RedirectResponse(url="/client/logs", status_code=307)

@app.get("/client/tools", response_class=HTMLResponse)
async def client_tools_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_tools.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "tools",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/tools", response_class=HTMLResponse)
async def tools_redirect_page(request: Request):
    return RedirectResponse(url="/client/tools", status_code=307)

@app.get("/client/tradebook", response_class=HTMLResponse)
async def client_tradebook_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_tradebook.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "tradebook",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/tradebook", response_class=HTMLResponse)
async def tradebook_redirect_page(request: Request):
    return RedirectResponse(url="/client/tradebook", status_code=307)

@app.get("/client/platforms", response_class=HTMLResponse)
async def client_platforms_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_platforms.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "platforms",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/platforms", response_class=HTMLResponse)
async def platforms_redirect_page(request: Request):
    return RedirectResponse(url="/client/platforms", status_code=307)

@app.get("/client/developer", response_class=HTMLResponse)
async def client_developer_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

        risk_row = conn.execute("SELECT telegram_bot_token, telegram_chat_id, discord_webhook_url FROM tenant_risk_limits WHERE tenant_id=?", (tenant_id,)).fetchone()
    risk_dict = dict(risk_row) if risk_row else {}

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_developer.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "developer",
        "trading_mode": get_tenant_trading_mode(tenant_id),
        "telegram_bot_token": risk_dict.get("telegram_bot_token", ""),
        "telegram_chat_id": risk_dict.get("telegram_chat_id", ""),
        "discord_webhook_url": risk_dict.get("discord_webhook_url", "")
    })

@app.get("/client/action-center", response_class=HTMLResponse)
async def client_action_center_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    with closing(database.get_db_connection()) as conn:
        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not tenant_row:
            raise HTTPException(status_code=404, detail="Tenant profile not found")
        tenant_dict = dict(tenant_row)

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        client_data = await telemetry_service.fetch_single_client_telemetry(http_client, tenant_dict)

    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    api_key = creds.get("API_KEY", "") or tenant_id

    return templates.TemplateResponse(request=request, name="client_action_center.html", context={
        "client": client_data,
        "client_user": client_user,
        "current_user": client_user,
        "api_key": api_key,
        "active_page": "action_center",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

# -----------------------------------------------------------------------------
# Quantitative Analytics & Option Tools (Max Pain, GEX, Straddle, Arbitrage, Scalping)
# -----------------------------------------------------------------------------
@app.get("/client/maxpain", response_class=HTMLResponse)
async def client_maxpain_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    return templates.TemplateResponse(request=request, name="client_maxpain.html", context={
        "client": {},
        "client_user": client_user,
        "current_user": client_user,
        "active_page": "maxpain",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/gex", response_class=HTMLResponse)
async def client_gex_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    return templates.TemplateResponse(request=request, name="client_gex.html", context={
        "client": {},
        "client_user": client_user,
        "current_user": client_user,
        "active_page": "gex",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/straddle", response_class=HTMLResponse)
async def client_straddle_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    return templates.TemplateResponse(request=request, name="client_straddle.html", context={
        "client": {},
        "client_user": client_user,
        "current_user": client_user,
        "active_page": "straddle",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/arbitrage", response_class=HTMLResponse)
async def client_arbitrage_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    return templates.TemplateResponse(request=request, name="client_arbitrage.html", context={
        "client": {},
        "client_user": client_user,
        "current_user": client_user,
        "active_page": "arbitrage",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/scalping", response_class=HTMLResponse)
async def client_scalping_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    return templates.TemplateResponse(request=request, name="client_scalping.html", context={
        "client": {},
        "client_user": client_user,
        "current_user": client_user,
        "active_page": "scalping",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/python", response_class=HTMLResponse)
async def client_python_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    return templates.TemplateResponse(request=request, name="client_python.html", context={
        "client": {},
        "client_user": client_user,
        "current_user": client_user,
        "active_page": "python",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.get("/client/flow", response_class=HTMLResponse)
async def client_flow_page(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    return templates.TemplateResponse(request=request, name="client_flow.html", context={
        "client": {},
        "client_user": client_user,
        "current_user": client_user,
        "active_page": "flow",
        "trading_mode": get_tenant_trading_mode(tenant_id)
    })

@app.post("/client/api/maxpain")
async def client_api_maxpain(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    async with httpx.AsyncClient(timeout=5.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/maxpain", f"http://xts_client_{tenant_id}:8000/api/v1/maxpain"]:
            try:
                resp = await http_client.post(target, json=body)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue
    try:
        from client import analytics_service
        return analytics_service.default_analytics_service.calculate_max_pain(
            underlying=body.get("underlying", "NIFTY")
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/client/api/gex")
async def client_api_gex(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    async with httpx.AsyncClient(timeout=5.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/gex", f"http://xts_client_{tenant_id}:8000/api/v1/gex"]:
            try:
                resp = await http_client.post(target, json=body)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue
    try:
        from client import analytics_service
        return analytics_service.default_analytics_service.calculate_gex(
            underlying=body.get("underlying", "NIFTY")
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/client/api/straddle")
async def client_api_straddle(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    async with httpx.AsyncClient(timeout=5.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/straddle", f"http://xts_client_{tenant_id}:8000/api/v1/straddle"]:
            try:
                resp = await http_client.post(target, json=body)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue
    try:
        from client import analytics_service
        return analytics_service.default_analytics_service.calculate_straddle_series(
            underlying=body.get("underlying", "NIFTY")
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/client/api/arbitrage")
async def client_api_arbitrage(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    async with httpx.AsyncClient(timeout=5.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/arbitrage", f"http://xts_client_{tenant_id}:8000/api/v1/arbitrage"]:
            try:
                resp = await http_client.post(target, json=body)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue
    try:
        from client import analytics_service
        return analytics_service.default_analytics_service.get_arbitrage_universe(
            symbols=body.get("symbols")
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}


# --- Python Strategy Runner API Proxies ---
@app.get("/client/api/python/list")
@app.post("/client/api/python/list")
async def client_api_python_list(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    try:
        from client import python_strategy_service
        return {"status": "success", "data": python_strategy_service.list_strategies(tenant_id)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/client/api/python/templates")
async def client_api_python_templates(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        from client import python_strategy_service
        return {"status": "success", "data": python_strategy_service.STRATEGY_TEMPLATES}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/client/api/python/save")
async def client_api_python_save(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    strat_id = body.get("id") or f"strat_{int(time.time())}"
    name = body.get("name", "Custom Strategy")
    code = body.get("code_content", "")
    desc = body.get("description", "")
    try:
        from client import python_strategy_service
        res = python_strategy_service.save_strategy(strat_id, tenant_id, name, code, desc)
        return {"status": "success", "data": res}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/client/api/python/run")
async def client_api_python_run(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    strat_id = body.get("id") or ""
    try:
        from client import python_strategy_service
        return python_strategy_service.run_strategy(strat_id, tenant_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/client/api/python/stop")
async def client_api_python_stop(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    strat_id = body.get("id") or ""
    try:
        from client import python_strategy_service
        return python_strategy_service.stop_strategy(strat_id, tenant_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/client/api/python/logs")
async def client_api_python_logs(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    strat_id = request.query_params.get("id", "")
    lines = int(request.query_params.get("lines", 100))
    try:
        from client import python_strategy_service
        return python_strategy_service.get_strategy_logs(strat_id, tenant_id, lines=lines)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/client/api/python/delete/{strat_id}")
async def client_api_python_delete(strat_id: str, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    try:
        from client import python_strategy_service
        python_strategy_service.delete_strategy(strat_id, tenant_id)
        return {"status": "success", "message": f"Strategy {strat_id} deleted"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# --- Flow No-Code Visual Builder API Proxies ---
@app.get("/client/api/flow/list")
@app.post("/client/api/flow/list")
async def client_api_flow_list(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    try:
        from client import flow_executor_service
        return {"status": "success", "data": flow_executor_service.list_flows(tenant_id)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/client/api/flow/templates")
async def client_api_flow_templates(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        from client import flow_executor_service
        return {"status": "success", "data": flow_executor_service.FLOW_TEMPLATES}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/client/api/flow/save")
async def client_api_flow_save(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    flow_id = body.get("id") or f"flow_{int(time.time())}"
    name = body.get("name", "Custom Flow")
    flow_data = body.get("flow", {})
    desc = body.get("description", "")
    try:
        from client import flow_executor_service
        res = flow_executor_service.save_flow(flow_id, tenant_id, name, flow_data, desc)
        return {"status": "success", "data": res}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/client/api/flow/toggle")
async def client_api_flow_toggle(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    flow_id = body.get("id", "")
    is_enabled = bool(body.get("is_enabled", True))
    try:
        from client import flow_executor_service
        flow_executor_service.toggle_flow(flow_id, tenant_id, is_enabled)
        return {"status": "success", "id": flow_id, "is_enabled": is_enabled}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/client/api/flow/run")
async def client_api_flow_run(request: Request, client_user: dict = Depends(require_client_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = client_user["tenant_id"]
    flow_id = body.get("id", "")
    try:
        from client import flow_executor_service
        flow = flow_executor_service.get_flow(flow_id, tenant_id)
        if not flow:
            return {"status": "error", "message": "Flow not found"}
        res = await flow_executor_service.execute_flow_actions(flow, {})
        return res
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/client/api/flow/delete/{flow_id}")
async def client_api_flow_delete(flow_id: str, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    try:
        from client import flow_executor_service
        flow_executor_service.delete_flow(flow_id, tenant_id)
        return {"status": "success", "message": f"Flow {flow_id} deleted"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/client/api/flow/logs")
async def client_api_flow_logs(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    flow_id = request.query_params.get("flow_id")
    try:
        from client import flow_executor_service
        logs = flow_executor_service.get_flow_logs(tenant_id, flow_id=flow_id)
        return {"status": "success", "data": logs}
    except Exception as e:
        return {"status": "error", "message": str(e)}





@app.get("/client/action-center/count")
async def client_action_center_count(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    headers = {"Accept": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=3.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/action-center/count", f"http://xts_client_{tenant_id}:8000/api/v1/action-center/count"]:
            try:
                resp = await http_client.get(target, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue

    # Fallback to direct client service if local/in-process
    try:
        from client import action_center_service
        count = action_center_service.get_pending_count()
        return {"status": "success", "count": count}
    except Exception:
        return {"status": "success", "count": 0}

@app.get("/client/action-center/api/data")
async def client_action_center_data(
    request: Request,
    status: Optional[str] = "all",
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    headers = {"Accept": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    params = {"status": status}
    async with httpx.AsyncClient(timeout=5.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/action-center/data", f"http://xts_client_{tenant_id}:8000/api/v1/action-center/data"]:
            try:
                resp = await http_client.get(target, headers=headers, params=params)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue

    try:
        from client import action_center_service
        data = action_center_service.get_pending_orders(status_filter=status)
        return data if isinstance(data, dict) and "data" in data else {"status": "success", "data": data}
    except Exception as err:
        return {"status": "error", "message": str(err), "data": []}

@app.post("/client/action-center/approve/{order_id}")
async def client_action_center_approve(
    order_id: str,
    request: Request,
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    payload = {"approver": client_user.get("email") or client_user.get("username") or tenant_id}
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/action-center/approve/{order_id}", f"http://xts_client_{tenant_id}:8000/api/v1/action-center/approve/{order_id}"]:
            try:
                resp = await http_client.post(target, json=payload, headers=headers)
                if resp.status_code in (200, 400, 404):
                    return JSONResponse(status_code=resp.status_code, content=resp.json())
            except Exception:
                continue

    try:
        from client import action_center_service
        res = action_center_service.approve_pending_order(order_id, approver=payload["approver"])
        return res
    except Exception as err:
        return {"status": "error", "message": str(err)}

@app.post("/client/action-center/reject/{order_id}")
async def client_action_center_reject(
    order_id: str,
    request: Request,
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "Rejected via Portal UI")

    payload = {"reason": reason, "approver": client_user.get("email") or client_user.get("username") or tenant_id}
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/action-center/reject/{order_id}", f"http://xts_client_{tenant_id}:8000/api/v1/action-center/reject/{order_id}"]:
            try:
                resp = await http_client.post(target, json=payload, headers=headers)
                if resp.status_code in (200, 400, 404):
                    return JSONResponse(status_code=resp.status_code, content=resp.json())
            except Exception:
                continue

    try:
        from client import action_center_service
        res = action_center_service.reject_pending_order(order_id, reason=reason, approver=payload["approver"])
        return res
    except Exception as err:
        return {"status": "error", "message": str(err)}

@app.post("/client/action-center/approve-all")
async def client_action_center_approve_all(
    request: Request,
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    payload = {"approver": client_user.get("email") or client_user.get("username") or tenant_id}
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/action-center/approve-all", f"http://xts_client_{tenant_id}:8000/api/v1/action-center/approve-all"]:
            try:
                resp = await http_client.post(target, json=payload, headers=headers)
                if resp.status_code in (200, 400, 404):
                    return JSONResponse(status_code=resp.status_code, content=resp.json())
            except Exception:
                continue

    try:
        from client import action_center_service
        res = action_center_service.approve_all_pending_orders(approver=payload["approver"])
        return res
    except Exception as err:
        return {"status": "error", "message": str(err)}

@app.post("/client/cancel-order")
async def client_cancel_order(
    request: Request,
    client_user: dict = Depends(require_client_auth),
    order_id: str = Form(...)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    dest_url = f"http://127.0.0.1:{port}/api/v1/cancelorder"
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [dest_url, f"http://xts_client_{tenant_id}:8000/api/v1/cancelorder"]:
            try:
                await http_client.post(target, json={"orderid": order_id}, headers=headers)
                break
            except Exception:
                continue

    referer = request.headers.get("referer", "/client/orders")
    return RedirectResponse(url=referer, status_code=303)

@app.post("/client/square-off-position")
async def client_square_off_position(
    request: Request,
    client_user: dict = Depends(require_client_auth),
    symbol: str = Form(...),
    product: str = Form("NRML"),
    quantity: int = Form(0)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    dest_url = f"http://127.0.0.1:{port}/api/v1/closeposition"
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [dest_url, f"http://xts_client_{tenant_id}:8000/api/v1/closeposition"]:
            try:
                await http_client.post(target, json={"symbol": symbol, "product": product, "quantity": quantity}, headers=headers)
                break
            except Exception:
                continue

    referer = request.headers.get("referer", "/client/positions")
    return RedirectResponse(url=referer, status_code=303)

@app.post("/client/set-trading-mode")
async def client_set_trading_mode(
    request: Request,
    client_user: dict = Depends(require_client_auth)
):
    try:
        body = await request.json()
        mode = str(body.get("mode") or "").strip().upper()
    except Exception:
        form = await request.form()
        mode = str(form.get("mode") or "").strip().upper()

    if mode not in ("LIVE", "PAPER", "ANALYZER"):
        return JSONResponse(status_code=400, content={"status": "error", "message": f"Invalid mode '{mode}'"})

    tenant_id = client_user["tenant_id"]
    paper_int = 1 if mode == "PAPER" else 0

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "UPDATE tenant_risk_limits SET trading_mode=?, paper_trade_mode=?, updated_at=? WHERE tenant_id=?",
                (mode, paper_int, time.time(), tenant_id)
            )

    docker_manager.write_client_config(tenant_id)
    port = docker_manager.get_tenant_port(tenant_id)

    # Notify running container
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=3.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/mode", f"http://xts_client_{tenant_id}:8000/api/v1/mode"]:
            try:
                await http_client.post(target, json={"mode": mode}, headers=headers)
                break
            except Exception:
                pass

    return {"status": "success", "mode": mode, "message": f"Trading mode switched to {mode}"}

@app.post("/client/notification-settings")
async def client_save_notification_settings(
    request: Request,
    client_user: dict = Depends(require_client_auth),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    discord_webhook_url: str = Form("")
):
    tenant_id = client_user["tenant_id"]
    clean_token = telegram_bot_token.strip()
    clean_chat = telegram_chat_id.strip()
    clean_discord = discord_webhook_url.strip()

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("""
                UPDATE tenant_risk_limits
                SET telegram_bot_token=?, telegram_chat_id=?, discord_webhook_url=?, updated_at=?
                WHERE tenant_id=?
            """, (clean_token, clean_chat, clean_discord, time.time(), tenant_id))

    docker_manager.write_client_config(tenant_id)
    return RedirectResponse(url="/client/developer?msg=Notification+settings+saved+successfully!", status_code=303)

@app.post("/client/test-notification")
async def client_send_test_notification(
    request: Request,
    client_user: dict = Depends(require_client_auth)
):
    try:
        body = await request.json()
    except Exception:
        body = {}

    tenant_id = client_user["tenant_id"]
    bot_token = body.get("telegram_bot_token")
    chat_id = body.get("telegram_chat_id")

    if not bot_token or not chat_id:
        with closing(database.get_db_connection()) as conn:
            row = conn.execute("SELECT telegram_bot_token, telegram_chat_id FROM tenant_risk_limits WHERE tenant_id=?", (tenant_id,)).fetchone()
            if row:
                bot_token = bot_token or row["telegram_bot_token"]
                chat_id = chat_id or row["telegram_chat_id"]

    if not bot_token or not chat_id:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "Please configure both Telegram Bot Token and Chat ID first."
        })

    port = docker_manager.get_tenant_port(tenant_id)
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=5.0) as http_client:
        for target in [f"http://127.0.0.1:{port}/api/v1/notifications/test", f"http://xts_client_{tenant_id}:8000/api/v1/notifications/test"]:
            try:
                resp = await http_client.post(target, json={"bot_token": bot_token, "chat_id": chat_id}, headers=headers)
                return resp.json()
            except Exception:
                pass

    return {"status": "success", "message": "Notification test ping dispatched to Telegram API."}

@app.get("/client/search")
async def client_symbol_search(
    request: Request,
    query: str = "",
    exchange: Optional[str] = None,
    limit: int = 20,
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    params = {"query": query, "limit": limit}
    if exchange:
        params["exchange"] = exchange

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=5.0) as http_client:
        for url in [f"http://127.0.0.1:{port}/api/v1/search", f"http://xts_client_{tenant_id}:8000/api/v1/search"]:
            try:
                resp = await http_client.get(url, params=params, headers=headers)
                return resp.json()
            except Exception:
                continue

    return {"status": "success", "data": [], "count": 0}

@app.websocket("/ws")
@app.websocket("/client/ws")
async def portal_websocket_proxy(websocket: WebSocket):
    await websocket.accept()
    token = websocket.cookies.get("client_session")
    tenant_id = None
    if token:
        ip = websocket.client.host or "127.0.0.1"
        ua = websocket.headers.get("user-agent", "")
        u = security.validate_client_session(token, ip, ua)
        if u:
            tenant_id = u.get("tenant_id")

    query_key = websocket.query_params.get("apikey") or websocket.query_params.get("api_key")
    if not tenant_id and query_key:
        tenant_id = api_gateway.resolve_tenant_id(query_key)

    port = docker_manager.get_tenant_port(tenant_id) if tenant_id else None
    target_urls = [f"ws://127.0.0.1:{port}/ws", f"ws://xts_client_{tenant_id}:8000/ws"] if port else []

    backend_ws = None
    for ws_url in target_urls:
        try:
            import websockets
            backend_ws = await websockets.connect(ws_url, ping_interval=None)
            break
        except Exception:
            continue

    if backend_ws:
        async def forward_client_to_backend():
            try:
                while True:
                    msg = await websocket.receive_text()
                    await backend_ws.send(msg)
            except Exception:
                pass
            finally:
                try:
                    await backend_ws.close()
                except Exception:
                    pass

        async def forward_backend_to_client():
            try:
                async for msg in backend_ws:
                    await websocket.send_text(msg)
            except Exception:
                pass
            finally:
                try:
                    await websocket.close()
                except Exception:
                    pass

        await asyncio.gather(forward_client_to_backend(), forward_backend_to_client())
    else:
        # Monolithic / local test fallback
        try:
            import sys
            sys.path.insert(0, os.path.join(PORTAL_DIR, "..", "client"))
            from ws_manager import default_ws_manager
            default_ws_manager.active_connections.add(websocket)
            default_ws_manager.subscriptions[websocket] = set()
            try:
                while True:
                    raw_text = await websocket.receive_text()
                    await default_ws_manager.handle_message(websocket, raw_text)
            except WebSocketDisconnect:
                default_ws_manager.disconnect(websocket)
            except Exception:
                default_ws_manager.disconnect(websocket)
        except Exception as ex:
            logger.debug(f"Portal websocket handler completed: {ex}")

@app.api_route("/watchlist/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def portal_watchlist_proxy(request: Request, path: str):
    token = request.cookies.get("client_session")
    tenant_id = None
    if token:
        ip = request.client.host if request.client else "127.0.0.1"
        ua = request.headers.get("user-agent", "")
        u = security.validate_client_session(token, ip, ua)
        if u:
            tenant_id = u.get("tenant_id")

    query_key = request.query_params.get("apikey") or request.query_params.get("api_key")
    if not tenant_id and query_key:
        tenant_id = api_gateway.resolve_tenant_id(query_key)

    if not tenant_id:
        tenant_id = "default"

    port = docker_manager.get_tenant_port(tenant_id)
    caddy_base = os.environ.get("CADDY_PROXY_BASE", "http://caddy/internal-client-proxy")
    url_local = f"http://127.0.0.1:{port}/watchlist/api/{path}"
    url_docker = f"http://xts_client_{tenant_id}:8000/watchlist/api/{path}"
    url_caddy = f"{caddy_base}/{tenant_id}/watchlist/api/{path}"

    body_bytes = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

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

    try:
        import sys
        sys.path.insert(0, os.path.join(PORTAL_DIR, "..", "client"))
        import watchlist_service
        if request.method == "GET" and path == "lists":
            lists = watchlist_service.get_watchlists(tenant_id)
            return JSONResponse(status_code=200, content={"status": "success", "data": lists})
    except Exception:
        pass

    return JSONResponse(status_code=502, content={"status": "error", "message": "Tenant watchlist service unreachable"})

@app.post("/client/place-manual-order")
async def client_manual_order(
    request: Request,
    symbol: str = Form(...),
    action: str = Form(...),
    quantity: int = Form(...),
    order_type: str = Form("MARKET"),
    price: float = Form(0.0),
    product: str = Form("NRML"),
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    caddy_base = os.environ.get("CADDY_PROXY_BASE", "http://caddy/internal-client-proxy")
    dest_url = f"http://127.0.0.1:{port}/api/v1/placeorder"

    payload = {
        "symbol": symbol.strip().upper(),
        "action": action.strip().upper(),
        "quantity": int(quantity),
        "pricetype": order_type,
        "price": float(price),
        "product": product,
        "strategy": "MANUAL_WEB_UI"
    }

    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [dest_url, f"http://xts_client_{tenant_id}:8000/api/v1/placeorder", f"{caddy_base}/{tenant_id}/api/v1/placeorder"]:
            try:
                resp = await http_client.post(target, json=payload, headers=headers)
                if resp.status_code in (200, 400):
                    break
            except Exception:
                continue

    return RedirectResponse(url="/client/dashboard", status_code=303)

@app.post("/client/square-off-symbol")
async def client_square_off_single(
    request: Request,
    symbol: str = Form(...),
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    dest_url = f"http://127.0.0.1:{port}/api/v1/closeposition"
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [dest_url, f"http://xts_client_{tenant_id}:8000/api/v1/closeposition"]:
            try:
                await http_client.post(target, json={"symbol": symbol.strip()}, headers=headers)
                break
            except Exception:
                continue

    return RedirectResponse(url="/client/dashboard", status_code=303)

@app.post("/client/panic-square-off")
async def client_panic_square_off(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    dest_url = f"http://127.0.0.1:{port}/api/v1/closeposition"
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [dest_url, f"http://xts_client_{tenant_id}:8000/api/v1/closeposition"]:
            try:
                await http_client.post(target, json={}, headers=headers)
                break
            except Exception:
                continue

    return RedirectResponse(url="/client/dashboard", status_code=303)

@app.post("/client/cancel-all-orders")
async def client_cancel_all(request: Request, client_user: dict = Depends(require_client_auth)):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    dest_url = f"http://127.0.0.1:{port}/api/v1/cancelallorder"
    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in [dest_url, f"http://xts_client_{tenant_id}:8000/api/v1/cancelallorder"]:
            try:
                await http_client.post(target, json={}, headers=headers)
                break
            except Exception:
                continue

    return RedirectResponse(url="/client/dashboard", status_code=303)

@app.post("/client/strategy/supertrend/reset-flat")
async def client_reset_supertrend_flat(
    request: Request,
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    dest_urls = [
        f"http://127.0.0.1:{port}/internal/supertrend/strategy/reset-flat",
        f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/reset-flat"
    ]
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    headers = {"Content-Type": "application/json"}
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in dest_urls:
            try:
                await http_client.post(target, json={"square_off_broker": True}, headers=headers)
                break
            except Exception:
                continue

    return RedirectResponse(url="/client/strategies", status_code=303)

@app.post("/client/strategy/supertrend/toggle")
async def client_toggle_supertrend(
    request: Request,
    client_user: dict = Depends(require_client_auth)
):
    tenant_id = client_user["tenant_id"]
    port = docker_manager.get_tenant_port(tenant_id)
    dest_urls = [
        f"http://127.0.0.1:{port}/internal/supertrend/strategy/toggle",
        f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/toggle"
    ]
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    headers = {"Content-Type": "application/json"}
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for target in dest_urls:
            try:
                await http_client.post(target, json={}, headers=headers)
                break
            except Exception:
                continue

    return RedirectResponse(url="/client/strategies", status_code=303)

@app.post("/admin/clients/{tenant_id}/create-user")
async def admin_create_client_user(
    request: Request,
    tenant_id: str,
    username: str = Form(...),
    password: str = Form(...),
    email: str = Form(""),
    user: dict = Depends(require_auth)
):
    pass_hash = security.hash_password(password.strip())
    try:
        uid = database.create_client_user(tenant_id, username.strip(), pass_hash, email.strip())
        database.record_audit(user["username"], "CREATE_CLIENT_USER", {"tenant_id": tenant_id, "client_username": username.strip()})
    except Exception as e:
        logger.error(f"Error creating client user: {e}")
    return RedirectResponse(url=f"/admin/clients/{tenant_id}", status_code=303)

# =====================================================================
# ADMIN AUTHENTICATION ROUTES
# =====================================================================

@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    if get_current_user(request):
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error, "current_user": None})

@app.post("/admin/login")
async def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp_or_recovery: str = Form("")
):
    ip = request.client.host or "127.0.0.1"
    ua = request.headers.get("user-agent", "")

    with closing(database.get_db_connection()) as conn:
        user = conn.execute("SELECT * FROM admin_users WHERE username=?", (username.strip(),)).fetchone()

    if not user or not security.verify_password(password, user["password_hash"]):
        database.record_audit(username, "FAILED_LOGIN_PASSWORD", {"ip": ip})
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Invalid username or password", "current_user": None
        })

    if not user["is_2fa_enabled"]:
        session_token = security.create_session(user["id"], ip, ua, lifetime_seconds=1800)
        resp = RedirectResponse(url="/admin/2fa-setup", status_code=303)
        resp.set_cookie(key="admin_session", value=session_token, httponly=True, samesite="strict")
        return resp

    input_code = totp_or_recovery.strip()
    totp_secret = security.decrypt_credentials(user["totp_secret_enc"]).get("secret") if user["totp_secret_enc"] else ""
    
    is_totp_valid = security.verify_totp(totp_secret, input_code)
    is_recovery_valid = False
    if not is_totp_valid:
        is_recovery_valid = security.verify_and_consume_recovery_code(user["id"], input_code)

    if not (is_totp_valid or is_recovery_valid):
        database.record_audit(username, "FAILED_LOGIN_2FA", {"ip": ip})
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Invalid 2FA or Recovery Code", "current_user": None
        })

    session_token = security.create_session(user["id"], ip, ua, lifetime_seconds=43200)
    database.record_audit(username, "SUCCESSFUL_LOGIN", {
        "ip": ip, "method": "RECOVERY_CODE" if is_recovery_valid else "TOTP"
    })

    resp = RedirectResponse(url="/admin/dashboard", status_code=303)
    resp.set_cookie(key="admin_session", value=session_token, httponly=True, samesite="strict")
    return resp

@app.get("/admin/2fa-setup", response_class=HTMLResponse)
async def setup_2fa_page(request: Request, user: dict = Depends(require_auth)):
    secret = security.generate_totp_secret()
    uri = security.get_totp_uri(secret, user["username"])
    qr_b64 = security.generate_qr_base64(uri)
    recovery_codes = security.generate_recovery_codes(10)
    recovery_codes_str = ",".join(recovery_codes)

    return templates.TemplateResponse(request=request, name="setup_2fa.html", context={
        "username": user["username"],
        "totp_secret": secret,
        "qr_code_base64": qr_b64,
        "recovery_codes": recovery_codes,
        "recovery_codes_str": recovery_codes_str,
        "current_user": user,
        "error": None
    })

@app.post("/admin/2fa-confirm")
async def confirm_2fa_action(
    request: Request,
    totp_secret: str = Form(...),
    recovery_codes_str: str = Form(""),
    confirmation_code: str = Form(...),
    user: dict = Depends(require_auth)
):
    codes_list = [c.strip() for c in recovery_codes_str.split(",") if c.strip()]
    if not codes_list:
        codes_list = security.generate_recovery_codes(10)

    if not security.verify_totp(totp_secret, confirmation_code):
        return templates.TemplateResponse(request=request, name="setup_2fa.html", context={
            "username": user["username"],
            "totp_secret": totp_secret,
            "qr_code_base64": security.generate_qr_base64(security.get_totp_uri(totp_secret, user["username"])),
            "recovery_codes": codes_list,
            "recovery_codes_str": ",".join(codes_list),
            "current_user": user,
            "error": "Confirmation code was invalid. Please try again."
        })

    hashed_codes = security.hash_recovery_codes(codes_list)
    encrypted_secret = security.encrypt_credentials({"secret": totp_secret})

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "UPDATE admin_users SET totp_secret_enc=?, is_2fa_enabled=1, recovery_codes_hash_json=? WHERE id=?",
                (encrypted_secret, json.dumps(hashed_codes), user["user_id"])
            )

    database.record_audit(user["username"], "ENABLE_2FA", {"ip": request.client.host or "127.0.0.1"})
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.get("/admin/logout")
async def logout(request: Request):
    token = request.cookies.get("admin_session")
    if token:
        security.destroy_session(token)
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie("admin_session")
    return resp

# =====================================================================
# DASHBOARD & CLIENT LIFECYCLE
# =====================================================================

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    active_filter: str = "all",
    view_mode: str = "cards",
    q: str = "",
    user: dict = Depends(require_auth)
):
    data = await telemetry_service.aggregate_all_telemetry()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "summary": data["summary"],
        "clients": data["clients"],
        "active_filter": active_filter,
        "view_mode": view_mode,
        "search_query": q,
        "current_user": user,
        "domain": DOMAIN_NAME,
        "server_info": get_server_info()
    })

@app.get("/admin/dashboard-partial", response_class=HTMLResponse)
async def dashboard_partial(
    request: Request,
    active_filter: str = "all",
    view_mode: str = "cards",
    q: str = "",
    user: dict = Depends(require_auth)
):
    data = await telemetry_service.aggregate_all_telemetry()
    return templates.TemplateResponse(request=request, name="dashboard_partial.html", context={
        "summary": data["summary"],
        "clients": data["clients"],
        "active_filter": active_filter,
        "view_mode": view_mode,
        "search_query": q,
        "current_user": user,
        "domain": DOMAIN_NAME,
        "server_info": get_server_info()
    })

def get_server_info() -> dict:
    server_ip = os.environ.get("SERVER_PUBLIC_IP", "").strip()
    if not server_ip or server_ip == ":80":
        server_ip = "127.0.0.1"
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    ist_time_str = datetime.datetime.now(IST).strftime("%H:%M:%S IST")
    return {
        "server_ip": server_ip,
        "timezone": "Asia/Kolkata (IST)",
        "current_time": ist_time_str,
        "domain": os.environ.get("DOMAIN_NAME", ":80")
    }

def build_webhook_info(request: Request, tenant_id: str, secret: str) -> dict:
    domain_env = os.environ.get("DOMAIN_NAME", "").strip()
    server_ip = os.environ.get("SERVER_PUBLIC_IP", "").strip()
    host_header = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()

    # 1. If explicit custom domain configured
    if domain_env and domain_env not in (":80", "trading.yourdomain.com", "localhost", "127.0.0.1"):
        if domain_env.startswith("http://") or domain_env.startswith("https://"):
            base = domain_env.rstrip("/")
        else:
            base = f"https://{domain_env}"
        webhook_url = f"{base}/webhook/{tenant_id}"
    # 2. If valid auto-detected server public IP available
    elif server_ip and server_ip not in ("127.0.0.1", "localhost", ":80"):
        webhook_url = f"http://{server_ip}/webhook/{tenant_id}"
    # 3. If accessing via browser host header
    elif host_header:
        if "127.0.0.1" in host_header or "localhost" in host_header or ":8500" in host_header:
            port = docker_manager.get_tenant_port(tenant_id)
            webhook_url = f"http://127.0.0.1:{port}/webhook"
        else:
            proto = request.headers.get("x-forwarded-proto", "http")
            clean_host = host_header.split(":")[0] if (host_header.endswith(":80") or host_header.endswith(":443")) else host_header
            webhook_url = f"{proto}://{clean_host}/webhook/{tenant_id}"
    else:
        webhook_url = f"http://YOUR_SERVER_IP/webhook/{tenant_id}"

    webhook_json_strategy = json.dumps({
        "secret": secret,
        "action": "{{strategy.order.action}}",
        "symbol": "{{ticker}}",
        "quantity": "{{strategy.order.contracts}}",
        "price": "{{close}}"
    }, indent=2)

    webhook_json_indicator = json.dumps({
        "secret": secret,
        "action": "BUY",
        "symbol": "CRUDEOIL1!",
        "quantity": 1,
        "price": "{{close}}"
    }, indent=2)

    return {
        "webhook_url": webhook_url,
        "webhook_json": webhook_json_strategy,
        "webhook_json_strategy": webhook_json_strategy,
        "webhook_json_indicator": webhook_json_indicator
    }

@app.get("/admin/clients/{tenant_id}/webhook-modal", response_class=HTMLResponse)
async def client_webhook_modal(tenant_id: str, request: Request, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        cred_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()

    if not tenant or not cred_row:
        raise HTTPException(status_code=404, detail="Client not found")

    creds = security.decrypt_credentials(cred_row["encrypted_payload"])
    secret = creds.get("WEBHOOK_SECRET", "")
    port = docker_manager.get_tenant_port(tenant_id)

    wb_data = build_webhook_info(request, tenant_id, secret)

    return templates.TemplateResponse(request=request, name="webhook_modal.html", context={
        "tenant": tenant,
        "secret": secret,
        "webhook_url": wb_data["webhook_url"],
        "webhook_json": wb_data["webhook_json"],
        "webhook_json_strategy": wb_data["webhook_json_strategy"],
        "webhook_json_indicator": wb_data["webhook_json_indicator"],
        "port": port,
        "server_info": get_server_info()
    })

@app.post("/admin/clients/validate-credentials", response_class=HTMLResponse)
async def validate_credentials_action(
    request: Request,
    api_key: str = Form(""),
    api_secret: str = Form(""),
    md_api_key: str = Form(""),
    md_api_secret: str = Form(""),
    client_id: str = Form(""),
    user: dict = Depends(require_auth)
):
    if not api_key.strip() or not api_secret.strip():
        return HTMLResponse(
            """<div class="bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs p-3 rounded-xl flex items-center gap-2">
                <span>⚠️ Please enter both Interactive API Key and Secret to test broker connection.</span>
            </div>"""
        )

    res = security.validate_broker_credentials(
        api_key=api_key,
        api_secret=api_secret,
        client_id=client_id,
        md_api_key=md_api_key,
        md_api_secret=md_api_secret
    )

    if res["valid"]:
        seg_str = ", ".join(res["segments"]) if res["segments"] else "Active"
        name_str = f" ({res['client_name']})" if res["client_name"] else ""
        return HTMLResponse(
            f"""<div class="bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs p-3.5 rounded-xl flex items-start gap-2.5">
                <div>
                    <div class="font-bold text-emerald-300">✅ Live Broker Handshake Verified!</div>
                    <div class="text-[11px] text-emerald-400/90 mt-0.5">Authenticated successfully for <strong>{client_id}{name_str}</strong>. Segments: {seg_str}</div>
                </div>
            </div>"""
        )
    else:
        err_msg = "<br>• ".join(res["errors"]) if res["errors"] else "Broker authentication failed"
        return HTMLResponse(
            f"""<div class="bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs p-3.5 rounded-xl flex items-start gap-2.5">
                <div>
                    <div class="font-bold text-rose-300">❌ Broker Authentication Failed</div>
                    <div class="text-[11px] text-rose-400/90 mt-0.5">• {err_msg}</div>
                </div>
            </div>"""
        )

@app.get("/admin/clients/add", response_class=HTMLResponse)
async def add_client_page(request: Request, user: dict = Depends(require_auth)):
    return templates.TemplateResponse(request=request, name="client_form.html", context={
        "is_edit": False,
        "tenant": None,
        "creds": None,
        "risk": None,
        "current_user": user,
        "error": None
    })

@app.post("/admin/clients/add")
async def add_client_action(
    request: Request,
    tenant_id: str = Form(...),
    name: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...),
    md_api_key: str = Form(""),
    md_api_secret: str = Form(""),
    client_id: str = Form(...),
    webhook_secret: str = Form(...),
    max_lots_limit: int = Form(100),
    max_order_value_inr: float = Form(5000000.0),
    daily_notional_cap_inr: float = Form(10000000.0),
    max_daily_loss_inr: float = Form(50000.0),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    discord_webhook_url: str = Form(""),
    slippage_buffer_pct: float = Form(0.005),
    min_days_before_expiry_mcx: int = Form(7),
    paper_trade_mode: int = Form(1),
    user: dict = Depends(require_auth)
):
    clean_id = tenant_id.strip().lower()
    now = time.time()

    creds_payload = {
        "API_KEY": api_key.strip(),
        "API_SECRET": api_secret.strip(),
        "MD_API_KEY": md_api_key.strip() or api_key.strip(),
        "MD_API_SECRET": md_api_secret.strip() or api_secret.strip(),
        "CLIENT_ID": client_id.strip(),
        "WEBHOOK_SECRET": webhook_secret.strip(),
        "XTS_API_BASE_URL": "https://symphony.acagarwal.com:3000/interactive"
    }
    enc_creds = security.encrypt_credentials(creds_payload)

    with closing(database.get_db_connection()) as conn:
        with conn:
            existing = conn.execute("SELECT id FROM tenants WHERE id=?", (clean_id,)).fetchone()
            if existing:
                return templates.TemplateResponse(request=request, name="client_form.html", context={
                    "is_edit": False, "tenant": None, "creds": None, "risk": None,
                    "current_user": user, "error": f"Client ID '{clean_id}' already exists."
                })

            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, 'ACTIVE', ?, ?)",
                (clean_id, name.strip(), now, now)
            )
            conn.execute(
                "INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)",
                (clean_id, enc_creds, now)
            )
            conn.execute("""
                INSERT INTO tenant_risk_limits (
                    tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr,
                    max_daily_loss_inr, telegram_bot_token, telegram_chat_id, discord_webhook_url,
                    slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (clean_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr,
                  max_daily_loss_inr, telegram_bot_token.strip(), telegram_chat_id.strip(), discord_webhook_url.strip(),
                  slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, now))

    docker_manager.provision_client_container(clean_id)
    caddy_ok = caddy_manager.sync_caddy_config()

    database.record_audit(user["username"], "PROVISION_CLIENT", {"name": name, "paper_mode": bool(paper_trade_mode)}, clean_id)
    if not caddy_ok:
        return RedirectResponse(url="/admin/dashboard?warn=Client+provisioned+but+Caddy+ingress+reload+failed.+Check+logs.", status_code=303)
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.get("/admin/clients/{tenant_id}", response_class=HTMLResponse)
async def view_client_detail(tenant_id: str, request: Request, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        t_row = conn.execute("SELECT id, name, status FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
        r_row = conn.execute("SELECT * FROM tenant_risk_limits WHERE tenant_id=?", (tenant_id,)).fetchone()
        st_row = conn.execute("SELECT * FROM tenant_supertrend_configs WHERE tenant_id=?", (tenant_id,)).fetchone()
        st_strat_rows = conn.execute("SELECT * FROM tenant_supertrend_strategies WHERE tenant_id=? ORDER BY created_at ASC", (tenant_id,)).fetchall()
        st_strategies = [dict(r) for r in st_strat_rows]
        if not t_row:
            raise HTTPException(status_code=404, detail="Client not found")

    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    secret = creds.get("WEBHOOK_SECRET", "")
    port = docker_manager.get_tenant_port(tenant_id)

    wb_data = build_webhook_info(request, tenant_id, secret)

    t_dict = dict(t_row)
    if c_row:
        t_dict["encrypted_payload"] = c_row["encrypted_payload"]
    t_dict["client_id"] = creds.get("CLIENT_ID", tenant_id)

    async with httpx.AsyncClient() as client:
        tel_data = await telemetry_service.fetch_single_client_telemetry(client, t_dict)

    if tel_data:
        tel_data.setdefault("holdings", {
            "invested_value": 0.0, "current_value": 0.0, "overall_pnl": 0.0,
            "overall_pnl_pct": 0.0, "day_pnl": 0.0, "day_pnl_pct": 0.0,
            "holdings_count": 0, "holdings": []
        })
        tel_data.setdefault("all_positions", tel_data.get("positions", []))
        tel_data.setdefault("closed_positions", [])
        tel_data.setdefault("broker_orders", [])
        tel_data.setdefault("broker_trades", [])
        tel_data.setdefault("positions_count", len(tel_data.get("positions", [])))
        tel_data.setdefault("all_positions_count", len(tel_data.get("all_positions", [])))

        # Merge live strategy telemetry (virtual_position, strategy_position, current_trend) into st_strategies
        live_runners = {s["id"]: s for s in tel_data.get("supertrend", {}).get("strategies", [])} if isinstance(tel_data.get("supertrend"), dict) else {}
        for strat in st_strategies:
            live_s = live_runners.get(strat["id"])
            if live_s:
                strat["virtual_position"] = live_s.get("virtual_position", 0)
                strat["strategy_position"] = live_s.get("strategy_position", "FLAT")
                strat["current_trend"] = live_s.get("current_trend", "INITIALIZING")
                strat["last_close"] = live_s.get("last_close", 0.0)
                strat["supertrend"] = live_s.get("supertrend", 0.0)

    supertrend_config = dict(st_row) if st_row else {
        "tenant_id": tenant_id,
        "is_enabled": 0,
        "is_configured": 0,
        "symbol": "",
        "exchange_segment": "",
        "timeframe": "5m",
        "quantity": 1,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 3.0,
        "execution_mode": "LIVE",
    }
    if "execution_mode" not in supertrend_config:
        supertrend_config["execution_mode"] = "LIVE"

    logs = docker_manager.get_container_logs(tenant_id, tail=100)
    tenant_custom_strats = database.get_tenant_custom_strategies(tenant_id=tenant_id)

    return templates.TemplateResponse(request=request, name="client_detail.html", context={
        "client": tel_data,
        "risk": dict(r_row) if r_row else {},
        "supertrend": supertrend_config,
        "supertrend_strategies": st_strategies,
        "custom_strategies": tenant_custom_strats,
        "logs": logs,
        "domain": DOMAIN_NAME,
        "webhook_url": wb_data["webhook_url"],
        "webhook_secret": secret,
        "webhook_json": wb_data["webhook_json"],
        "webhook_json_strategy": wb_data["webhook_json_strategy"],
        "webhook_json_indicator": wb_data["webhook_json_indicator"],
        "current_user": user,
        "server_info": get_server_info()
    })

@app.get("/admin/clients/{tenant_id}/edit", response_class=HTMLResponse)
async def edit_client_page(tenant_id: str, request: Request, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        t_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        c_row = conn.execute("SELECT * FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
        r_row = conn.execute("SELECT * FROM tenant_risk_limits WHERE tenant_id=?", (tenant_id,)).fetchone()

    if not t_row:
        raise HTTPException(status_code=404, detail="Client not found")

    creds = security.decrypt_credentials(c_row["encrypted_payload"])

    return templates.TemplateResponse(request=request, name="client_form.html", context={
        "is_edit": True,
        "tenant": dict(t_row),
        "creds": creds,
        "risk": dict(r_row),
        "current_user": user,
        "error": None
    })

@app.post("/admin/clients/{tenant_id}/edit")
async def edit_client_action(
    tenant_id: str,
    request: Request,
    name: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...),
    md_api_key: str = Form(""),
    md_api_secret: str = Form(""),
    client_id: str = Form(...),
    webhook_secret: str = Form(...),
    max_lots_limit: int = Form(...),
    max_order_value_inr: float = Form(...),
    daily_notional_cap_inr: float = Form(...),
    max_daily_loss_inr: float = Form(50000.0),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    discord_webhook_url: str = Form(""),
    slippage_buffer_pct: float = Form(...),
    min_days_before_expiry_mcx: int = Form(...),
    paper_trade_mode: int = Form(...),
    user: dict = Depends(require_auth)
):
    now = time.time()
    creds_payload = {
        "API_KEY": api_key.strip(),
        "API_SECRET": api_secret.strip(),
        "MD_API_KEY": md_api_key.strip() or api_key.strip(),
        "MD_API_SECRET": md_api_secret.strip() or api_secret.strip(),
        "CLIENT_ID": client_id.strip(),
        "WEBHOOK_SECRET": webhook_secret.strip(),
        "XTS_API_BASE_URL": "https://symphony.acagarwal.com:3000/interactive"
    }
    enc_creds = security.encrypt_credentials(creds_payload)

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("UPDATE tenants SET name=?, updated_at=? WHERE id=?", (name.strip(), now, tenant_id))
            conn.execute("UPDATE tenant_credentials SET encrypted_payload=?, updated_at=? WHERE tenant_id=?", (enc_creds, now, tenant_id))
            conn.execute("""
                UPDATE tenant_risk_limits SET
                    max_lots_limit=?, max_order_value_inr=?, daily_notional_cap_inr=?,
                    max_daily_loss_inr=?, telegram_bot_token=?, telegram_chat_id=?, discord_webhook_url=?,
                    slippage_buffer_pct=?, min_days_before_expiry_mcx=?, paper_trade_mode=?, updated_at=?
                WHERE tenant_id=?
            """, (max_lots_limit, max_order_value_inr, daily_notional_cap_inr,
                  max_daily_loss_inr, telegram_bot_token.strip(), telegram_chat_id.strip(), discord_webhook_url.strip(),
                  slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, now, tenant_id))

    docker_manager.restart_client_container(tenant_id)
    database.record_audit(user["username"], "UPDATE_CONFIG", {"name": name, "paper_mode": bool(paper_trade_mode)}, tenant_id)

    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.api_route("/admin/clients/{tenant_id}/pause", methods=["GET", "POST"])
async def pause_client(tenant_id: str, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("UPDATE tenants SET status='PAUSED', updated_at=? WHERE id=?", (time.time(), tenant_id))
    docker_manager.stop_client_container(tenant_id)
    caddy_ok = caddy_manager.sync_caddy_config()
    database.record_audit(user["username"], "PAUSE_CLIENT", {}, tenant_id)
    if not caddy_ok:
        return RedirectResponse(url="/admin/dashboard?warn=Client+paused+but+Caddy+ingress+reload+failed.", status_code=303)
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.api_route("/admin/clients/{tenant_id}/resume", methods=["GET", "POST"])
async def resume_client(tenant_id: str, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("UPDATE tenants SET status='ACTIVE', updated_at=? WHERE id=?", (time.time(), tenant_id))
    docker_manager.restart_client_container(tenant_id)
    caddy_ok = caddy_manager.sync_caddy_config()
    database.record_audit(user["username"], "RESUME_CLIENT", {}, tenant_id)
    if not caddy_ok:
        return RedirectResponse(url="/admin/dashboard?warn=Client+resumed+but+Caddy+ingress+reload+failed.", status_code=303)
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.api_route("/admin/clients/{tenant_id}/restart", methods=["GET", "POST"])
async def restart_client(tenant_id: str, user: dict = Depends(require_auth)):
    docker_manager.restart_client_container(tenant_id)
    database.record_audit(user["username"], "RESTART_CONTAINER", {}, tenant_id)
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.api_route("/admin/clients/{tenant_id}/refresh-master", methods=["GET", "POST"])
async def refresh_client_master(tenant_id: str, request: Request, user: dict = Depends(require_auth)):
    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/master/refresh"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/master/refresh"
    url_local = f"http://127.0.0.1:{port}/internal/master/refresh"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    data = None
    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, timeout=35.0)
                if resp.status_code == 200:
                    data = resp.json()
                    break
            except Exception:
                pass

    database.record_audit(user["username"], "REFRESH_MASTER_CACHE", {"result": data}, tenant_id)

    # If requested via HTMX, return a fresh status badge
    if request.headers.get("HX-Request"):
        if data and data.get("status") == "success":
            fut_c = data.get("futures_contracts", 0)
            cash_c = data.get("cash_contracts", 0)
            date_str = data.get("cached_date", "Today")
            return HTMLResponse(
                f"""<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                    <span>Synced ({date_str} | {fut_c + cash_c} contracts)</span>
                </span>"""
            )
        else:
            return HTMLResponse(
                f"""<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/20">
                    <span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span>
                    <span>Sync Failed</span>
                </span>"""
            )

    return RedirectResponse(url=f"/admin/clients/{tenant_id}", status_code=303)

@app.post("/admin/clients/{tenant_id}/delete")
async def delete_client(tenant_id: str, user: dict = Depends(require_auth)):
    docker_manager.remove_client_container(tenant_id)
    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
    caddy_ok = caddy_manager.sync_caddy_config()
    database.record_audit(user["username"], "DELETE_CLIENT", {}, tenant_id)
    if not caddy_ok:
        return RedirectResponse(url="/admin/dashboard?warn=Client+deleted+but+Caddy+ingress+reload+failed.", status_code=303)
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.get("/admin/clients/{tenant_id}/supertrend/validate-symbol")
async def validate_supertrend_symbol(
    tenant_id: str,
    symbol: str = "",
    user: dict = Depends(require_auth)
):
    """HTMX endpoint validating a symbol against client container's contract master."""
    clean_sym = symbol.strip().upper()
    if not clean_sym:
        return HTMLResponse('<div class="text-[11px] text-slate-500 font-mono italic">Type a symbol (e.g. SILVER1001!, CRUDEOIL1!, RELIANCE) to validate.</div>')

    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/validate-symbol?symbol={clean_sym}"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/validate-symbol?symbol={clean_sym}"
    url_local = f"http://127.0.0.1:{port}/internal/validate-symbol?symbol={clean_sym}"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    val_res = None
    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.get(target_url, headers=headers, timeout=2.0)
                if resp.status_code == 200:
                    val_res = resp.json()
                    break
            except Exception:
                pass

    if not val_res:
        return HTMLResponse('<div class="bg-amber-500/10 border border-amber-500/30 text-amber-300 text-[11px] font-mono p-2.5 rounded-xl">⚠️ Client container unreachable for validation.</div>')

    if val_res.get("valid"):
        desc = val_res.get("desc") or clean_sym
        inst_id = val_res.get("inst_id")
        exch_seg = val_res.get("exch_seg")
        lot_size = val_res.get("lot_size")
        expiry = val_res.get("expiry")
        days_to_exp = val_res.get("days_to_expiry")
        exp_text = f"{expiry} ({days_to_exp}d left)" if (days_to_exp is not None and days_to_exp >= 0) else (expiry or "No Expiry")
        
        return HTMLResponse(f"""
        <div class="bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-[11px] font-mono p-3 rounded-xl space-y-1">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-1.5 font-bold text-emerald-400 text-xs">
                    <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
                    <span>✅ VALID CONTRACT: {desc}</span>
                </div>
                <span class="text-[10px] bg-emerald-500/20 px-2 py-0.5 rounded text-emerald-300">ID: {inst_id}</span>
            </div>
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-slate-300 text-[11px] pt-0.5">
                <span>Segment: <strong class="text-slate-100">{exch_seg}</strong></span>
                <span>Lot Size: <strong class="text-slate-100">{lot_size}</strong></span>
                <span>Expiry: <strong class="text-slate-100">{exp_text}</strong></span>
            </div>
        </div>
        """)
    else:
        err = val_res.get("error") or f"Symbol '{clean_sym}' not found in contract master file."
        return HTMLResponse(f"""
        <div class="bg-rose-500/10 border border-rose-500/30 text-rose-300 text-[11px] font-mono p-3 rounded-xl space-y-1">
            <div class="flex items-center gap-1.5 font-bold text-rose-400 text-xs">
                <span>❌ INVALID SYMBOL: {clean_sym}</span>
            </div>
            <p class="text-slate-300 text-[11px]">{err}</p>
            <p class="text-slate-400 text-[10px]">Supports standard and continuous TradingView formats (e.g. <code>SILVER1001!</code>, <code>CRUDEOIL1!</code>, <code>RELIANCE</code>, <code>NIFTY1!</code>).</p>
        </div>
        """)

@app.get("/admin/clients/{tenant_id}/supertrend/readiness-partial")
async def get_supertrend_readiness_partial(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_auth)
):
    """HTMX endpoint returning live market readiness diagnostics card."""
    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/market-readiness"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/market-readiness"
    url_local = f"http://127.0.0.1:{port}/internal/market-readiness"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    diag = None
    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.get(target_url, headers=headers, timeout=2.5)
                if resp.status_code == 200:
                    diag = resp.json()
                    break
            except Exception:
                pass

    if not diag:
        diag = {
            "interactive_auth": {"status": "FAILED", "error": "Client container unreachable"},
            "market_data_auth": {"status": "FAILED", "error": "Client container unreachable"},
            "master_cache": {"status": "FAILED", "total_contracts": 0},
            "live_feed": {"status": "FAILED", "error": "Client container unreachable"},
            "market_hours": {"status": "UNKNOWN", "trading_hours": ""},
            "all_ready": False
        }

    return templates.TemplateResponse(request=request, name="supertrend_readiness_partial.html", context={
        "diag": diag,
        "tenant_id": tenant_id
    })

@app.post("/admin/clients/{tenant_id}/supertrend/strategy/save")
async def save_supertrend_strategy_action(
    tenant_id: str,
    request: Request,
    id: str = Form(""),
    symbol: str = Form(...),
    exchange_segment: str = Form("MCXFO"),
    timeframe: Optional[str] = Form(None),
    timeframe_select: Optional[str] = Form(None),
    custom_minutes: Optional[str] = Form(None),
    quantity: int = Form(1),
    product_type: str = Form("NRML"),
    atr_period: int = Form(10),
    multiplier: float = Form(3.0),
    execution_mode: str = Form("LIVE"),
    is_enabled: Optional[str] = Form(None),
    user: dict = Depends(require_auth)
):
    """Saves or updates a symbol strategy for a client account (Max 6 concurrent strategies)."""
    clean_sym = symbol.strip().upper()
    clean_seg = exchange_segment.strip().upper() or "MCXFO"
    clean_prod = product_type.strip().upper() or "NRML"
    clean_qty = max(1, quantity)
    clean_atr = max(2, atr_period)
    clean_mult = max(0.1, multiplier)
    clean_mode = "PAPER" if execution_mode.strip().upper() == "PAPER" else "LIVE"
    clean_enabled = is_enabled in ("true", "1", "on", "yes", True) if is_enabled is not None else False

    # Robust timeframe resolution
    clean_tf = "5m"
    if timeframe_select == "custom" and custom_minutes and custom_minutes.strip().isdigit():
        clean_tf = f"{int(custom_minutes.strip())}m"
    elif timeframe_select and timeframe_select.strip() and timeframe_select.strip() != "custom":
        clean_tf = timeframe_select.strip().lower()
    elif timeframe and timeframe.strip() and timeframe.strip() != "custom":
        clean_tf = timeframe.strip().lower()

    if not clean_sym:
        raise HTTPException(status_code=400, detail="Trading symbol is required.")

    now = time.time()

    with closing(database.get_db_connection()) as conn:
        with conn:
            # Check capacity limit: only enforce if inserting a brand-new (symbol, timeframe) strategy
            existing_rec = None
            if id.strip():
                existing_rec = conn.execute("SELECT id FROM tenant_supertrend_strategies WHERE tenant_id=? AND id=?", (tenant_id, id.strip())).fetchone()
            if not existing_rec:
                existing_rec = conn.execute("SELECT id FROM tenant_supertrend_strategies WHERE tenant_id=? AND symbol=? AND timeframe=?", (tenant_id, clean_sym, clean_tf)).fetchone()

            if existing_rec:
                strat_id = existing_rec["id"]
            elif id.strip():
                clash = conn.execute("SELECT tenant_id FROM tenant_supertrend_strategies WHERE id=?", (id.strip(),)).fetchone()
                if clash and clash["tenant_id"] != tenant_id:
                    import uuid
                    strat_id = f"st_{tenant_id}_{clean_sym.lower()}_{clean_tf}_{uuid.uuid4().hex[:6]}"
                else:
                    strat_id = id.strip()
            else:
                candidate_id = f"st_{tenant_id}_{clean_sym.lower()}_{clean_tf}"
                clash = conn.execute("SELECT tenant_id FROM tenant_supertrend_strategies WHERE id=?", (candidate_id,)).fetchone()
                if clash and clash["tenant_id"] != tenant_id:
                    import uuid
                    strat_id = f"st_{tenant_id}_{clean_sym.lower()}_{clean_tf}_{uuid.uuid4().hex[:6]}"
                else:
                    strat_id = candidate_id

            if not existing_rec:
                cur_count = conn.execute("SELECT COUNT(*) FROM tenant_supertrend_strategies WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
                if cur_count >= 6:
                    raise HTTPException(status_code=400, detail="Maximum strategy limit (6 strategies) reached for this account. Remove an existing strategy first.")

            conn.execute("""
                INSERT INTO tenant_supertrend_strategies (
                    id, tenant_id, symbol, exchange_segment, timeframe, quantity,
                    product_type, atr_period, multiplier, execution_mode, is_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, symbol, timeframe) DO UPDATE SET
                    exchange_segment=excluded.exchange_segment,
                    quantity=excluded.quantity,
                    product_type=excluded.product_type,
                    atr_period=excluded.atr_period,
                    multiplier=excluded.multiplier,
                    execution_mode=excluded.execution_mode,
                    is_enabled=excluded.is_enabled,
                    updated_at=excluded.updated_at
            """, (
                strat_id,
                tenant_id,
                clean_sym,
                clean_seg,
                clean_tf,
                clean_qty,
                clean_prod,
                clean_atr,
                clean_mult,
                clean_mode,
                1 if clean_enabled else 0,
                now,
                now
            ))

    logger.info(f"Tenant [{tenant_id}] saved SuperTrend strategy: {strat_id} -> {clean_sym} ({clean_tf}, {clean_mode}, qty={clean_qty}, enabled={clean_enabled})")

    # Re-generate client config.json
    try:
        docker_manager.write_client_config(tenant_id)
    except Exception as e:
        logger.warning(f"Error updating config.json: {e}")

    # Dispatch to live client container
    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/strategy/save"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/save"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/strategy/save"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    payload = {
        "id": strat_id,
        "symbol": clean_sym,
        "exchange_segment": clean_seg,
        "timeframe": clean_tf,
        "quantity": clean_qty,
        "product_type": clean_prod,
        "atr_period": clean_atr,
        "multiplier": clean_mult,
        "execution_mode": clean_mode,
        "is_enabled": clean_enabled
    }

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    break
            except Exception:
                pass

    database.record_audit(user["username"], "SAVE_SUPERTREND_STRATEGY", payload, tenant_id)
    return RedirectResponse(url=f"/admin/clients/{tenant_id}?tab=supertrend&saved=1", status_code=303)

@app.post("/admin/clients/{tenant_id}/supertrend/strategy/{strategy_id}/toggle")
async def toggle_supertrend_strategy_action(
    tenant_id: str,
    strategy_id: str,
    request: Request,
    user: dict = Depends(require_auth)
):
    """Toggles enable/disable state for a single symbol strategy."""
    with closing(database.get_db_connection()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_strategies WHERE tenant_id=? AND id=?", (tenant_id, strategy_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Strategy not found")
            
            strat = dict(row)
            new_state = 0 if strat.get("is_enabled") else 1
            conn.execute("UPDATE tenant_supertrend_strategies SET is_enabled=?, updated_at=? WHERE id=?", (new_state, time.time(), strategy_id))

    try:
        docker_manager.write_client_config(tenant_id)
    except Exception:
        pass

    port = docker_manager.get_tenant_port(tenant_id)
    sym = strat["symbol"]
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/strategy/{strategy_id}/toggle"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/{strategy_id}/toggle"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/strategy/{strategy_id}/toggle"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, json={"is_enabled": bool(new_state)}, timeout=5.0)
                if resp.status_code == 200:
                    break
            except Exception:
                pass

    database.record_audit(user["username"], "TOGGLE_SUPERTREND_STRATEGY", {"strategy_id": strategy_id, "symbol": sym, "timeframe": strat.get("timeframe"), "is_enabled": bool(new_state)}, tenant_id)
    return RedirectResponse(url=f"/admin/clients/{tenant_id}?tab=supertrend&toggled=1", status_code=303)

@app.post("/admin/clients/{tenant_id}/supertrend/strategy/{strategy_id}/delete")
async def delete_supertrend_strategy_action(
    tenant_id: str,
    strategy_id: str,
    request: Request,
    user: dict = Depends(require_auth)
):
    """Removes a symbol strategy from database and client container."""
    with closing(database.get_db_connection()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_strategies WHERE tenant_id=? AND id=?", (tenant_id, strategy_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Strategy not found")
            strat = dict(row)
            conn.execute("DELETE FROM tenant_supertrend_strategies WHERE id=?", (strategy_id,))

    try:
        docker_manager.write_client_config(tenant_id)
    except Exception:
        pass

    port = docker_manager.get_tenant_port(tenant_id)
    sym = strat["symbol"]
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/strategy/{strategy_id}"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/{strategy_id}"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/strategy/{strategy_id}"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.delete(target_url, headers=headers, timeout=5.0)
                if resp.status_code == 200:
                    break
            except Exception:
                pass

    database.record_audit(user["username"], "DELETE_SUPERTREND_STRATEGY", {"strategy_id": strategy_id, "symbol": sym, "timeframe": strat.get("timeframe")}, tenant_id)
    return RedirectResponse(url=f"/admin/clients/{tenant_id}?tab=supertrend&deleted=1", status_code=303)

@app.post("/admin/clients/{tenant_id}/supertrend/config")
async def save_supertrend_config(
    tenant_id: str,
    request: Request,
    is_enabled: bool = Form(False),
    symbol: str = Form(""),
    exchange_segment: str = Form(""),
    timeframe: Optional[str] = Form(None),
    timeframe_select: Optional[str] = Form(None),
    custom_minutes: Optional[str] = Form(None),
    quantity: int = Form(1),
    product_type: str = Form("NRML"),
    atr_period: int = Form(10),
    multiplier: float = Form(3.0),
    execution_mode: str = Form("LIVE"),
    user: dict = Depends(require_auth)
):
    clean_sym = symbol.strip().upper()
    clean_seg = exchange_segment.strip().upper()
    clean_prod = product_type.strip().upper()
    clean_qty = max(1, quantity)
    clean_atr = max(2, atr_period)
    clean_mult = max(0.1, multiplier)
    clean_mode = "PAPER" if execution_mode.strip().upper() == "PAPER" else "LIVE"

    # Robust timeframe resolution
    clean_tf = "5m"
    if timeframe_select == "custom" and custom_minutes and custom_minutes.strip().isdigit():
        clean_tf = f"{int(custom_minutes.strip())}m"
    elif timeframe_select and timeframe_select.strip() and timeframe_select.strip() != "custom":
        clean_tf = timeframe_select.strip().lower()
    elif timeframe and timeframe.strip() and timeframe.strip() != "custom":
        clean_tf = timeframe.strip().lower()

    is_conf = bool(clean_sym and clean_seg and clean_qty > 0)
    
    if is_enabled and not is_conf:
        raise HTTPException(
            status_code=400,
            detail="Please configure and save a trading symbol, exchange segment, and quantity before enabling SuperTrend auto-trading."
        )

    now = time.time()
    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO tenant_supertrend_configs
                (tenant_id, is_enabled, is_configured, symbol, exchange_segment, timeframe, quantity, product_type, atr_period, multiplier, execution_mode, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tenant_id,
                1 if is_enabled else 0,
                1 if is_conf else 0,
                clean_sym,
                clean_seg,
                clean_tf,
                clean_qty,
                clean_prod,
                clean_atr,
                clean_mult,
                clean_mode,
                now
            ))

    database.record_audit(user["username"], "UPDATE_SUPERTREND_CONFIG", {
        "is_enabled": is_enabled,
        "symbol": clean_sym,
        "exchange_segment": clean_seg,
        "timeframe": clean_tf,
        "quantity": clean_qty,
        "execution_mode": clean_mode
    }, tenant_id)

    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/config"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/config"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/config"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    payload = {
        "is_enabled": is_enabled,
        "symbol": clean_sym,
        "exchange_segment": clean_seg,
        "timeframe": clean_tf,
        "quantity": clean_qty,
        "product_type": clean_prod,
        "atr_period": clean_atr,
        "multiplier": clean_mult,
        "execution_mode": clean_mode
    }

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    break
            except Exception:
                pass

    return RedirectResponse(url=f"/admin/clients/{tenant_id}?tab=supertrend&saved=1", status_code=303)

@app.get("/admin/clients/{tenant_id}/supertrend/chart-data")
async def get_supertrend_chart_data(
    tenant_id: str,
    timeframe: Optional[str] = None,
    symbol: Optional[str] = None,
    strategy_id: Optional[str] = None,
    user: dict = Depends(require_auth)
):
    """Proxies candlestick and SuperTrend series data for TradingView Lightweight Charts v4."""
    with closing(database.get_db_connection()) as conn:
        st_row = conn.execute("SELECT * FROM tenant_supertrend_configs WHERE tenant_id=?", (tenant_id,)).fetchone()
        strat_rows = conn.execute("SELECT id, symbol, timeframe FROM tenant_supertrend_strategies WHERE tenant_id=?", (tenant_id,)).fetchall()

    target_strat = None
    if strategy_id and strat_rows:
        for r in strat_rows:
            if r["id"] == strategy_id.strip():
                target_strat = r
                break

    cfg_sym = symbol or (target_strat["symbol"] if target_strat else (strat_rows[0]["symbol"] if strat_rows else (st_row["symbol"] if st_row and st_row["symbol"] else "")))
    
    # Auto-match timeframe for the specific target symbol/strategy if timeframe param omitted
    matched_tf = target_strat["timeframe"] if target_strat else None
    if not matched_tf and cfg_sym and strat_rows:
        for r in strat_rows:
            if r["symbol"].upper() == cfg_sym.upper():
                matched_tf = r["timeframe"]
                break

    cfg_tf = timeframe or matched_tf or (strat_rows[0]["timeframe"] if strat_rows else (st_row["timeframe"] if st_row and st_row["timeframe"] else "5m"))

    if not cfg_sym:
        return {
            "symbol": "",
            "timeframe": cfg_tf or "5m",
            "status": "UNCONFIGURED",
            "candlestick": [],
            "supertrend_line": [],
            "upper_band": [],
            "lower_band": [],
            "markers": []
        }

    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/candles"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/candles"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/candles"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    params = {}
    if cfg_tf:
        params["timeframe"] = cfg_tf
    if cfg_sym:
        params["symbol"] = cfg_sym
    if strategy_id:
        params["strategy_id"] = strategy_id.strip()

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.get(target_url, headers=headers, params=params, timeout=6.0)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass

    return {
        "symbol": cfg_sym,
        "status": "UNAVAILABLE",
        "candlestick": [],
        "supertrend_line": [],
        "upper_band": [],
        "lower_band": [],
        "markers": []
    }

@app.post("/admin/clients/{tenant_id}/supertrend/evaluate-now")
async def evaluate_supertrend_now_portal(
    tenant_id: str,
    symbol: Optional[str] = None,
    strategy_id: Optional[str] = None,
    user: dict = Depends(require_auth)
):
    """Proxies on-demand diagnostic evaluation request and returns calculation trace."""
    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/evaluate-now"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/evaluate-now"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/evaluate-now"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    params = {}
    if symbol:
        params["symbol"] = symbol.strip().upper()
    if strategy_id:
        params["strategy_id"] = strategy_id.strip()

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, params=params, timeout=6.0)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass

    return {"status": "ERROR", "error": "Client container unreachable"}

@app.post("/admin/clients/{tenant_id}/supertrend/sync-trend")
async def sync_supertrend_trend_portal(
    tenant_id: str,
    strategy_id: Optional[str] = None,
    user: dict = Depends(require_auth)
):
    """Proxies on-demand trend synchronization request to the client execution gateway."""
    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/sync-trend"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/sync-trend"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/sync-trend"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    params = {}
    if strategy_id:
        params["strategy_id"] = strategy_id.strip()

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, params=params, timeout=10.0)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass

    return {"status": "ERROR", "error": "Client container unreachable"}

@app.post("/admin/clients/{tenant_id}/supertrend/strategy/{strategy_id}/reset-flat")
@app.post("/admin/clients/{tenant_id}/supertrend/reset-flat")
async def reset_supertrend_strategy_flat_portal(
    tenant_id: str,
    strategy_id: Optional[str] = None,
    square_off_broker: Optional[int] = Form(None),
    request: Request = None,
    user: dict = Depends(require_auth)
):
    """Proxies reset-to-flat request to the client container and records audit."""
    target_strat_id = strategy_id
    sq_broker = bool(square_off_broker) if square_off_broker is not None else False

    if request and request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                target_strat_id = target_strat_id or body.get("strategy_id") or body.get("id")
                if "square_off_broker" in body:
                    sq_broker = bool(body.get("square_off_broker"))
        except Exception:
            pass

    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/strategy/reset-flat"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/reset-flat"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/strategy/reset-flat"

    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    payload = {
        "strategy_id": target_strat_id,
        "square_off_broker": sq_broker
    }

    res = None
    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, json=payload, timeout=10.0)
                if resp.status_code == 200:
                    res = resp.json()
                    break
            except Exception:
                pass

    if not res:
        res = {"status": "ERROR", "error": "Client container unreachable"}

    database.record_audit(
        user["username"],
        "RESET_SUPERTREND_STRATEGY_FLAT",
        {"strategy_id": target_strat_id, "square_off_broker": sq_broker, "result": res},
        tenant_id
    )
    return res

# =====================================================================
# EMERGENCY PANIC SWITCHES
# =====================================================================

@app.post("/admin/clients/{tenant_id}/panic")
async def panic_single(tenant_id: str, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    secret = security.decrypt_credentials(c_row["encrypted_payload"]).get("WEBHOOK_SECRET", "") if c_row else ""
    
    res = await telemetry_service.panic_single_client(tenant_id, secret)
    database.record_audit(user["username"], "PANIC_CLIENT", {"result": res}, tenant_id)
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.post("/admin/panic-all")
async def panic_all(user: dict = Depends(require_auth)):
    res = await telemetry_service.panic_all_active_clients()
    database.record_audit(user["username"], "PANIC_ALL", {"result": res})
    return RedirectResponse(url="/admin/dashboard", status_code=303)

# =====================================================================
# AUDIT TRAIL
# =====================================================================

@app.get("/admin/audit-logs", response_class=HTMLResponse)
async def audit_logs_page(request: Request, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 100").fetchall()

    logs = []
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r["timestamp"], datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        logs.append({
            "id": r["id"],
            "formatted_time": dt.strftime('%Y-%m-%d %H:%M:%S IST'),
            "actor": r["actor"],
            "action": r["action"],
            "target_tenant_id": r["target_tenant_id"],
            "details_json": r["details_json"]
        })

    return templates.TemplateResponse(request=request, name="audit_logs.html", context={
        "logs": logs, "current_user": user
    })

# =====================================================================
# GLOBAL ORDER AUDIT STREAM
# =====================================================================

@app.get("/admin/orders", response_class=HTMLResponse)
async def orders_page(
    request: Request,
    search: str = "",
    client_id: str = "",
    status: str = "",
    user: dict = Depends(require_auth)
):
    with closing(database.get_db_connection()) as conn:
        tenants = conn.execute("SELECT id, name FROM tenants ORDER BY name ASC").fetchall()
        
    signals = telemetry_service.aggregate_all_signals(search=search, client_filter=client_id, status_filter=status, limit=100)
    
    return templates.TemplateResponse(request=request, name="all_orders.html", context={
        "signals": signals,
        "tenants": [dict(t) for t in tenants],
        "search": search,
        "selected_client": client_id,
        "selected_status": status,
        "current_user": user
    })

@app.get("/admin/orders-partial", response_class=HTMLResponse)
async def orders_partial(
    request: Request,
    search: str = "",
    client_id: str = "",
    status: str = "",
    user: dict = Depends(require_auth)
):
    signals = telemetry_service.aggregate_all_signals(search=search, client_filter=client_id, status_filter=status, limit=100)
    return templates.TemplateResponse(request=request, name="orders_table_partial.html", context={
        "signals": signals
    })

@app.get("/admin/reports/trades/export")
async def export_trades_csv(tenant_id: str = "", user: dict = Depends(require_auth)):
    """Exports tenant or global broker executed trade book as standard Contract Note CSV."""
    from fastapi.responses import Response

    all_trades = []
    if tenant_id:
        tel = await telemetry_service.get_single_client_telemetry(tenant_id)
        trades = tel.get("broker_trades") or []
        for t in trades:
            t["tenant_id"] = tenant_id
        all_trades.extend(trades)
    else:
        tel = await telemetry_service.get_all_clients_telemetry()
        for client in tel.get("clients", []):
            c_id = client.get("id", "")
            for t in (client.get("broker_trades") or []):
                t["tenant_id"] = c_id
                all_trades.append(t)

    csv_content = telemetry_service.generate_trade_book_csv(all_trades, tenant_id=tenant_id)
    
    date_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"trade_book_{tenant_id or 'all'}_{date_tag}.csv"

    database.record_audit(user["username"], "EXPORT_TRADE_BOOK_CSV", {"tenant_id": tenant_id, "trades_count": len(all_trades)})

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={file_name}"
        }
    )

# =====================================================================
# 100% FRONTEND OPERATIONS & CLUSTER SETTINGS
# =====================================================================

@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(require_auth)):
    caddy_file = caddy_manager.get_caddy_config_path()
    allowed_ips = os.environ.get("ADMIN_ALLOWED_IPS", "127.0.0.1")
    if os.path.exists(caddy_file):
        try:
            with open(caddy_file, "r") as f:
                content = f.read()
                # Parse allowed IPs from Caddy client_ip matcher if present
                for line in content.split("\n"):
                    if "client_ip" in line:
                        allowed_ips = line.replace("client_ip", "").strip()
        except Exception:
            pass

    # Read latest backup info
    data_root = docker_manager.get_client_data_root()
    backup_dir = os.path.abspath(os.path.join(os.path.dirname(data_root), "backups"))
    latest_backup = "No backups created yet"
    backup_count = 0
    if os.path.exists(backup_dir):
        files = sorted([f for f in os.listdir(backup_dir) if f.endswith(".enc") or f.endswith(".tar.gz") or f.endswith(".gpg")], reverse=True)
        backup_count = len(files)
        if files:
            latest_backup = files[0]

    return templates.TemplateResponse(request=request, name="settings.html", context={
        "allowed_ips": allowed_ips,
        "latest_backup": latest_backup,
        "backup_count": backup_count,
        "warmup_time": "08:30 IST",
        "warmup_batch_size": 4,
        "warmup_interval_seconds": 5,
        "current_user": user,
        "flash_message": request.query_params.get("msg", None),
        "flash_error": request.query_params.get("err", None)
    })

@app.post("/admin/settings/backup")
async def trigger_manual_backup(request: Request, user: dict = Depends(require_auth)):
    try:
        import sys
        backup_dir_path = os.path.abspath(os.path.join(os.path.dirname(PORTAL_DIR), "backup"))
        if backup_dir_path not in sys.path:
            sys.path.insert(0, backup_dir_path)
        import backup_engine

        passphrase = os.environ.get("BACKUP_PASSPHRASE", "DefaultBackupPassphrase123!")
        backup_file = backup_engine.create_backup_archive(passphrase)
        filename = os.path.basename(backup_file)

        database.record_audit(user["username"], "MANUAL_BACKUP_CREATED", {"backup_file": filename})
        return RedirectResponse(url=f"/admin/settings?msg=Backup+{filename}+created+and+encrypted+successfully!", status_code=303)
    except Exception as e:
        logger.error(f"Backup trigger failed: {e}")
        return RedirectResponse(url=f"/admin/settings?err=Backup+failed:+{str(e)}", status_code=303)

@app.post("/admin/settings/ip-allowlist")
async def update_ip_allowlist(request: Request, allowed_ips: str = Form(...), user: dict = Depends(require_auth)):
    try:
        clean_ips = allowed_ips.strip()
        os.environ["ADMIN_ALLOWED_IPS"] = clean_ips
        
        # Persist to .env if present
        env_path = os.path.join(database.get_portal_data_dir(), ".env")
        if os.path.exists(env_path):
            try:
                lines = []
                found = False
                with open(env_path, "r") as f:
                    for line in f:
                        if line.startswith("ADMIN_ALLOWED_IPS="):
                            lines.append(f"ADMIN_ALLOWED_IPS={clean_ips}\n")
                            found = True
                        else:
                            lines.append(line)
                if not found:
                    lines.append(f"ADMIN_ALLOWED_IPS={clean_ips}\n")
                with open(env_path, "w") as f:
                    f.writelines(lines)
            except Exception as e:
                logger.warning(f"Could not persist allowlist to .env: {e}")

        # Update and re-sync Caddy config
        caddy_manager.sync_caddy_config()
        database.record_audit(user["username"], "UPDATE_IP_ALLOWLIST", {"allowed_ips": clean_ips})
        return RedirectResponse(url="/admin/settings?msg=Admin+IP+allowlist+updated+successfully!", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin/settings?err=Failed+to+update+allowlist:+{str(e)}", status_code=303)

@app.post("/admin/settings/rotate-master-key")
async def rotate_master_key(request: Request, new_master_key: str = Form(...), user: dict = Depends(require_auth)):
    from cryptography.fernet import Fernet
    try:
        new_key_str = new_master_key.strip()
        new_fernet = Fernet(new_key_str.encode())

        # Decrypt all credentials with old key, re-encrypt with new key
        with closing(database.get_db_connection()) as conn:
            with conn:
                # 1. Tenant credentials
                rows = conn.execute("SELECT tenant_id, encrypted_payload FROM tenant_credentials").fetchall()
                for r in rows:
                    t_id = r["tenant_id"]
                    decrypted = security.decrypt_credentials(r["encrypted_payload"])
                    re_encrypted = new_fernet.encrypt(json.dumps(decrypted).encode('utf-8')).decode('utf-8')
                    conn.execute("UPDATE tenant_credentials SET encrypted_payload=?, updated_at=? WHERE tenant_id=?", (re_encrypted, time.time(), t_id))

                # 2. Admin 2FA TOTP secrets
                admin_rows = conn.execute("SELECT id, totp_secret_enc FROM admin_users WHERE totp_secret_enc IS NOT NULL").fetchall()
                for ar in admin_rows:
                    a_id = ar["id"]
                    decrypted_totp = security.decrypt_credentials(ar["totp_secret_enc"])
                    re_enc_totp = new_fernet.encrypt(json.dumps(decrypted_totp).encode('utf-8')).decode('utf-8')
                    conn.execute("UPDATE admin_users SET totp_secret_enc=? WHERE id=?", (re_enc_totp, a_id))

        os.environ["PORTAL_MASTER_KEY"] = new_key_str

        # Persist new master key to .env on disk
        env_path = os.path.join(database.get_portal_data_dir(), ".env")
        if os.path.exists(env_path):
            try:
                lines = []
                found = False
                with open(env_path, "r") as f:
                    for line in f:
                        if line.startswith("PORTAL_MASTER_KEY="):
                            lines.append(f"PORTAL_MASTER_KEY={new_key_str}\n")
                            found = True
                        else:
                            lines.append(line)
                if not found:
                    lines.append(f"PORTAL_MASTER_KEY={new_key_str}\n")
                with open(env_path, "w") as f:
                    f.writelines(lines)
                os.chmod(env_path, 0o400)
            except Exception as e:
                logger.warning(f"Could not persist new master key to .env: {e}")

        # Re-write client config files
        for r in rows:
            try:
                docker_manager.write_client_config(r["tenant_id"])
            except Exception:
                pass

        database.record_audit(user["username"], "ROTATE_MASTER_VAULT_KEY", {"re_encrypted_tenants": len(rows), "re_encrypted_admins": len(admin_rows)})
        return RedirectResponse(url="/admin/settings?msg=Master+key+rotated+and+vault+re-encrypted+successfully!", status_code=303)
    except Exception as e:
        logger.error(f"Key rotation failed: {e}")
        return RedirectResponse(url=f"/admin/settings?err=Key+rotation+failed:+{str(e)}", status_code=303)

@app.post("/admin/master-contract/refresh")
async def admin_refresh_master_contracts(request: Request, user: dict = Depends(require_auth)):
    try:
        import master_contract_service
        res = await asyncio.to_thread(master_contract_service.download_and_refresh_master_contracts)
        database.record_audit(user["username"], "REFRESH_MASTER_CONTRACTS", res)
        if res.get("status") == "success":
            return JSONResponse(content={"status": "success", "message": f"Successfully compiled {res.get('count', 0)} symbols.", "count": res.get("count", 0)})
        else:
            return JSONResponse(status_code=500, content={"status": "error", "message": res.get("message", "Refresh failed")})
    except Exception as e:
        logger.error(f"Manual master contract refresh failed: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# =====================================================================
# CUSTOM PYTHON STRATEGY HUB ROUTES
# =====================================================================

@app.get("/admin/strategies", response_class=HTMLResponse)
async def strategies_hub(
    request: Request,
    msg: Optional[str] = None,
    err: Optional[str] = None,
    user: dict = Depends(require_auth)
):
    strategies = database.get_custom_strategies()
    assignments = database.get_tenant_custom_strategies()
    tenants = database.get_all_tenants()
    return templates.TemplateResponse(request=request, name="strategies.html", context={
        "custom_strategies": strategies,
        "tenant_custom_strategies": assignments,
        "tenants": tenants,
        "msg": msg,
        "err": err,
        "current_user": user,
        "server_info": get_server_info()
    })

@app.post("/admin/strategies/upload")
async def upload_strategy_action(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    default_symbol: str = Form("GOLDPETAL1!"),
    default_timeframe: str = Form("15m"),
    strategy_file: Optional[UploadFile] = File(None),
    code_content: Optional[str] = Form(None),
    user: dict = Depends(require_auth)
):
    try:
        raw_code = ""
        filename = "custom_strategy.py"
        if strategy_file and strategy_file.filename:
            filename = strategy_file.filename
            contents = await strategy_file.read()
            raw_code = contents.decode("utf-8")
        elif code_content:
            raw_code = code_content.strip()

        if not raw_code:
            return RedirectResponse(url="/admin/strategies?err=Please+provide+a+Python+strategy+file+or+code.", status_code=303)

        # AST Validation & Security Screening
        validation = strategy_parser.validate_strategy_code(raw_code)
        if not validation.get("valid"):
            err_msg = validation.get("error") or "Invalid strategy code structure."
            return RedirectResponse(url=f"/admin/strategies?err={httpx.URL('', params={'e': err_msg}).query[2:]}", status_code=303)

        strat_id = f"cs_{uuid.uuid4().hex[:10]}"
        strat_name = name.strip() or validation.get("class_name") or "Custom Strategy"
        strat_desc = description.strip() or validation.get("docstring") or ""

        database.save_custom_strategy(
            id=strat_id,
            name=strat_name,
            description=strat_desc,
            filename=filename,
            code_content=raw_code,
            default_timeframe=default_timeframe.strip().lower(),
            default_symbol=default_symbol.strip().upper()
        )

        database.record_audit(user["username"], "UPLOAD_CUSTOM_STRATEGY", {
            "strategy_id": strat_id,
            "name": strat_name,
            "filename": filename
        })
        return RedirectResponse(url=f"/admin/strategies?msg=Strategy+{strat_name}+uploaded+and+validated+successfully!", status_code=303)
    except Exception as e:
        logger.error(f"Failed to upload strategy: {e}", exc_info=True)
        return RedirectResponse(url=f"/admin/strategies?err=Upload+failed:+{str(e)}", status_code=303)

@app.get("/admin/strategies/template")
async def download_strategy_template(user: dict = Depends(require_auth)):
    code = strategy_parser.generate_boilerplate_code()
    return Response(
        content=code,
        media_type="text/x-python",
        headers={"Content-Disposition": "attachment; filename=xts_strategy_template.py"}
    )

@app.get("/admin/strategies/{strat_id}/code")
async def get_strategy_code(strat_id: str, user: dict = Depends(require_auth)):
    strat = database.get_custom_strategy(strat_id)
    if not strat:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Strategy not found"})
    return {"status": "success", "strategy": strat}

@app.post("/admin/strategies/{strat_id}/dry-run")
async def dry_run_strategy(strat_id: str, request: Request, user: dict = Depends(require_auth)):
    strat = database.get_custom_strategy(strat_id)
    if not strat:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Strategy not found"})

    try:
        body = await request.json()
    except Exception:
        body = {}

    symbol = str(body.get("symbol") or strat.get("default_symbol") or "GOLDPETAL1!").strip().upper()
    timeframe = str(body.get("timeframe") or strat.get("default_timeframe") or "15m").strip().lower()
    bars = int(body.get("bars", 100))

    # Standalone simulation fallback using synthetic market data
    import math
    synth_candles = []
    base_price = 10500.0 if "GOLD" in symbol else (2500.0 if "SILVER" in symbol else 24000.0)
    now = int(time.time())
    tf_secs = 900
    for i in range(bars, 0, -1):
        c_time = now - (i * tf_secs)
        sine_val = math.sin(i * 0.15) * 45.0
        open_p = base_price + sine_val
        close_p = open_p + (math.cos(i * 0.15) * 12.0)
        high_p = max(open_p, close_p) + 8.0
        low_p = min(open_p, close_p) - 8.0
        synth_candles.append({
            "time": c_time,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": 120
        })

    # Evaluate using client runner
    try:
        import sys
        sys.path.append(os.path.abspath(os.path.join(PORTAL_DIR, "..", "client")))
        from custom_strategy_engine import MultiCustomStrategyEngine
        result = MultiCustomStrategyEngine.evaluate_dry_run(strat["code_content"], synth_candles)
        if result.get("error"):
            return {"status": "error", "message": result["error"]}
        return {
            "status": "success",
            "symbol": symbol,
            "timeframe": timeframe,
            "total_candles": result.get("total_candles"),
            "signals_count": result.get("signals_count"),
            "signals": result.get("signals")
        }
    except Exception as e:
        return {"status": "error", "message": f"Simulation failed: {e}"}

@app.post("/admin/strategies/assign")
async def assign_strategy_action(
    request: Request,
    strategy_id: str = Form(...),
    symbol: str = Form(...),
    timeframe: str = Form("15m"),
    execution_mode: str = Form("LIVE"),
    user: dict = Depends(require_auth)
):
    form_data = await request.form()
    tenant_ids = form_data.getlist("tenant_ids")
    if not tenant_ids:
        return RedirectResponse(url="/admin/strategies?err=Please+select+at+least+one+client+account.", status_code=303)

    strat = database.get_custom_strategy(strategy_id)
    if not strat:
        return RedirectResponse(url="/admin/strategies?err=Strategy+not+found.", status_code=303)

    clean_symbol = symbol.strip().upper()
    clean_tf = timeframe.strip().lower()

    assigned_count = 0
    for tid in tenant_ids:
        qty_key = f"qty_{tid}"
        qty = int(form_data.get(qty_key, 1))
        assignment_id = f"tcs_{uuid.uuid4().hex[:10]}"

        database.save_tenant_custom_strategy(
            id=assignment_id,
            tenant_id=tid,
            strategy_id=strategy_id,
            symbol=clean_symbol,
            exchange_segment="MCXFO",
            timeframe=clean_tf,
            quantity=qty,
            product_type="NRML",
            execution_mode=execution_mode,
            is_enabled=1
        )

        # Update client config and notify running container
        docker_manager.write_client_config(tid)
        port = docker_manager.get_tenant_port(tid)
        async with httpx.AsyncClient(timeout=3.0) as client:
            try:
                await client.post(
                    f"http://127.0.0.1:{port}/internal/custom-strategies/save",
                    json={
                        "id": assignment_id,
                        "strategy_id": strategy_id,
                        "name": strat["name"],
                        "symbol": clean_symbol,
                        "exchange_segment": "MCXFO",
                        "timeframe": clean_tf,
                        "quantity": qty,
                        "product_type": "NRML",
                        "execution_mode": execution_mode,
                        "is_enabled": True,
                        "code_content": strat["code_content"]
                    }
                )
            except Exception:
                pass
        assigned_count += 1

    database.record_audit(user["username"], "ASSIGN_CUSTOM_STRATEGY", {
        "strategy_id": strategy_id,
        "symbol": clean_symbol,
        "timeframe": clean_tf,
        "tenants": tenant_ids
    })
    return RedirectResponse(url=f"/admin/strategies?msg=Successfully+assigned+{strat['name']}+to+{assigned_count}+client(s)!", status_code=303)

@app.post("/admin/custom-strategies/assignment/{assignment_id}/toggle")
async def toggle_assignment_action(
    assignment_id: str,
    request: Request,
    is_enabled: int = Form(...),
    user: dict = Depends(require_auth)
):
    database.toggle_tenant_custom_strategy(assignment_id, is_enabled)
    # Find tenant for this assignment and notify container
    assignments = database.get_tenant_custom_strategies()
    for a in assignments:
        if a["id"] == assignment_id:
            tid = a["tenant_id"]
            docker_manager.write_client_config(tid)
            port = docker_manager.get_tenant_port(tid)
            async with httpx.AsyncClient(timeout=3.0) as client:
                try:
                    await client.post(
                        f"http://127.0.0.1:{port}/internal/custom-strategies/{assignment_id}/toggle",
                        json={"is_enabled": bool(is_enabled)}
                    )
                except Exception:
                    pass
            break

    database.record_audit(user["username"], "TOGGLE_CUSTOM_STRATEGY_ASSIGNMENT", {
        "assignment_id": assignment_id,
        "is_enabled": is_enabled
    })
    return RedirectResponse(url="/admin/strategies?msg=Strategy+assignment+state+updated!", status_code=303)

@app.post("/admin/custom-strategies/assignment/{assignment_id}/delete")
async def delete_assignment_action(
    assignment_id: str,
    user: dict = Depends(require_auth)
):
    assignments = database.get_tenant_custom_strategies()
    target_tenant = None
    for a in assignments:
        if a["id"] == assignment_id:
            target_tenant = a["tenant_id"]
            break

    database.delete_tenant_custom_strategy(assignment_id)

    if target_tenant:
        docker_manager.write_client_config(target_tenant)
        port = docker_manager.get_tenant_port(target_tenant)
        async with httpx.AsyncClient(timeout=3.0) as client:
            try:
                await client.delete(f"http://127.0.0.1:{port}/internal/custom-strategies/{assignment_id}")
            except Exception:
                pass

    database.record_audit(user["username"], "DELETE_CUSTOM_STRATEGY_ASSIGNMENT", {
        "assignment_id": assignment_id
    })
    return RedirectResponse(url="/admin/strategies?msg=Strategy+assignment+removed!", status_code=303)

@app.post("/admin/strategies/{strat_id}/delete")
async def delete_strategy_action(
    strat_id: str,
    user: dict = Depends(require_auth)
):
    strat = database.get_custom_strategy(strat_id)
    strat_name = strat["name"] if strat else strat_id
    database.delete_custom_strategy(strat_id)
    database.record_audit(user["username"], "DELETE_CUSTOM_STRATEGY", {"strategy_id": strat_id, "name": strat_name})
    return RedirectResponse(url=f"/admin/strategies?msg=Strategy+{strat_name}+deleted+successfully!", status_code=303)


@app.get("/admin/api/system-health")
async def get_system_health_api(user: dict = Depends(require_auth)):
    """Comprehensive real-time diagnostic health check across database, client containers, and memory."""
    health_data = {
        "status": "HEALTHY",
        "timestamp": time.time(),
        "database": {"status": "HEALTHY", "integrity": "OK", "tenants_count": 0, "strategies_count": 0},
        "clients": {},
        "system": {
            "server_ip": os.environ.get("SERVER_IP", "139.59.20.239"),
            "uptime_seconds": time.time() - getattr(app.state, "start_time", time.time())
        }
    }

    # 1. Database Integrity Verification
    try:
        with closing(database.get_db_connection()) as conn:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            health_data["database"]["integrity"] = check
            if check != "ok":
                health_data["database"]["status"] = "CORRUPTED"
                health_data["status"] = "DEGRADED"

            t_count = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
            st_count = conn.execute("SELECT COUNT(*) FROM tenant_supertrend_strategies").fetchone()[0]
            cs_count = conn.execute("SELECT COUNT(*) FROM tenant_custom_strategies").fetchone()[0]
            health_data["database"]["tenants_count"] = t_count
            health_data["database"]["supertrend_strategies_count"] = st_count
            health_data["database"]["custom_strategies_count"] = cs_count
    except Exception as e:
        health_data["database"]["status"] = f"ERROR: {e}"
        health_data["status"] = "DEGRADED"

    # 2. Client Container Health & Margin Diagnostics
    try:
        telemetry = await telemetry_service.aggregate_all_telemetry()
        for c in telemetry.get("clients", []):
            cid = c.get("id")
            c_status = c.get("status", "UNKNOWN")
            avail_margin = c.get("available_margin", 0.0)
            margin_used = c.get("margin_used", 0.0)
            active_st = c.get("supertrend", {}).get("active_strategies_count", 0)
            
            health_data["clients"][cid] = {
                "name": c.get("name"),
                "status": c_status,
                "available_margin": avail_margin,
                "margin_used": margin_used,
                "active_strategies": active_st,
                "is_healthy": c_status in ("HEALTHY", "ONLINE")
            }
            if c_status not in ("HEALTHY", "ONLINE"):
                health_data["status"] = "DEGRADED"
    except Exception as e:
        health_data["clients_error"] = str(e)
        health_data["status"] = "DEGRADED"

    return health_data



