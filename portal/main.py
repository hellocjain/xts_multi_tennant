from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
import re

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
    return user

def require_api_auth(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: session expired or authentication required"
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

# =====================================================================
# AUTHENTICATION ROUTES
# =====================================================================

# In-memory brute-force rate limiter for admin login
LOGIN_ATTEMPTS: Dict[str, List[float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 900  # 15 minutes

def check_login_rate_limit(ip: str) -> Optional[int]:
    """Returns remaining lockout seconds if IP is locked out, or None if allowed."""
    now = time.time()
    attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    LOGIN_ATTEMPTS[ip] = attempts
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        oldest_in_window = attempts[0]
        remaining = int(LOCKOUT_WINDOW_SECONDS - (now - oldest_in_window))
        return max(1, remaining)
    return None

def record_failed_login(ip: str):
    now = time.time()
    attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    attempts.append(now)
    LOGIN_ATTEMPTS[ip] = attempts

def clear_failed_logins(ip: str):
    LOGIN_ATTEMPTS.pop(ip, None)

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

    # Brute-force rate limit guard
    lockout_sec = check_login_rate_limit(ip)
    if lockout_sec:
        minutes = max(1, (lockout_sec + 59) // 60)
        database.record_audit(username.strip() or "unknown", "LOCKED_OUT_RATE_LIMIT", {"ip": ip, "lockout_sec": lockout_sec})
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": f"Too many failed login attempts. Temporarily locked out. Please try again in {minutes} minute(s).",
            "current_user": None
        })

    with closing(database.get_db_connection()) as conn:
        user = conn.execute("SELECT * FROM admin_users WHERE username=?", (username.strip(),)).fetchone()

    if not user or not security.verify_password(password, user["password_hash"]):
        record_failed_login(ip)
        remaining_attempts = max(0, MAX_LOGIN_ATTEMPTS - len(LOGIN_ATTEMPTS.get(ip, [])))
        database.record_audit(username, "FAILED_LOGIN_PASSWORD", {"ip": ip, "attempts_remaining": remaining_attempts})
        err_msg = "Invalid username or password"
        if remaining_attempts in (1, 2):
            err_msg += f" ({remaining_attempts} attempt{'s' if remaining_attempts > 1 else ''} remaining before temporary 15-min lockout)"
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": err_msg, "current_user": None
        })

    # Clear failed attempt counter on successful login
    clear_failed_logins(ip)

    # 30-day persistent session (2,592,000 seconds)
    session_lifetime = 2592000
    session_token = security.create_session(user["id"], ip, ua, lifetime_seconds=session_lifetime)
    database.record_audit(username, "SUCCESSFUL_LOGIN", {
        "ip": ip, "method": "PASSWORD", "session_lifetime": "30d"
    })

    resp = RedirectResponse(url="/admin/dashboard", status_code=303)
    resp.set_cookie(key="admin_session", value=session_token, max_age=session_lifetime, httponly=True, samesite="strict")
    return resp

@app.get("/admin/2fa-setup")
async def setup_2fa_page(request: Request):
    """Legacy route: gracefully redirect to dashboard."""
    return RedirectResponse(url="/admin/dashboard", status_code=303)

@app.post("/admin/2fa-confirm")
async def confirm_2fa_action(request: Request):
    """Legacy route: gracefully redirect to dashboard."""
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
    clean_sym = re.sub(r'[\s\-_]+', '', symbol.strip().upper())
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
    clean_sym = re.sub(r'[\s\-_]+', '', symbol.strip().upper())
    clean_seg = exchange_segment.strip().upper() or "MCXFO"
    if clean_seg == "MCXFO":
        if clean_sym in ("GOLDPETAL", "GOLD"):
            clean_sym = f"{clean_sym}1!"
        elif clean_sym in ("SILVER100", "SILVERM", "SILVERMIC", "SILVER"):
            clean_sym = f"{clean_sym}1!"
        elif clean_sym in ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATURALGASM", "COPPER", "ZINC", "LEAD", "ALUMINIUM"):
            clean_sym = f"{clean_sym}1!"
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
async def panic_single(tenant_id: str, request: Request, user: dict = Depends(require_auth)):
    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
    secret = security.decrypt_credentials(c_row["encrypted_payload"]).get("WEBHOOK_SECRET", "") if c_row else ""
    
    res = await telemetry_service.panic_single_client(tenant_id, secret)
    database.record_audit(user["username"], "PANIC_CLIENT", {"result": res}, tenant_id)
    if request.headers.get("accept") == "application/json" or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"status": "success", "result": res})
    referer = request.headers.get("referer")
    target_url = referer if (referer and f"/admin/clients/{tenant_id}" in referer) else f"/admin/clients/{tenant_id}"
    return RedirectResponse(url=target_url, status_code=303)

@app.post("/admin/panic-all")
async def panic_all(request: Request, user: dict = Depends(require_auth)):
    res = await telemetry_service.panic_all_active_clients()
    database.record_audit(user["username"], "PANIC_ALL", {"result": res})
    if request.headers.get("accept") == "application/json" or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"status": "success", "result": res})
    return RedirectResponse(url="/admin/dashboard?panic=completed", status_code=303)

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
        if request.headers.get("accept") == "application/json" or request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"status": "success", "filename": filename, "message": f"Backup {filename} created and encrypted successfully!"})
        return RedirectResponse(url=f"/admin/settings?msg=Backup+{filename}+created+and+encrypted+successfully!", status_code=303)
    except Exception as e:
        logger.error(f"Backup trigger failed: {e}")
        if request.headers.get("accept") == "application/json" or request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"status": "error", "message": f"Backup failed: {str(e)}"}, status_code=500)
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




# =====================================================================
# REACT SPA & JSON API EXTENSION (v10.0-PRO)
# =====================================================================

@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    user = get_current_user(request)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "user": {"username": user.get("username", "admin")}}

@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    ip = request.client.host or "127.0.0.1"
    ua = request.headers.get("user-agent", "")

    lockout_sec = check_login_rate_limit(ip)
    if lockout_sec:
        minutes = max(1, (lockout_sec + 59) // 60)
        database.record_audit("unknown", "LOCKED_OUT_RATE_LIMIT", {"ip": ip, "lockout_sec": lockout_sec})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Temporarily locked out. Please try again in {minutes} minute(s)."
        )

    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        form = await request.form()
        body = dict(form)

    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    with closing(database.get_db_connection()) as conn:
        user = conn.execute("SELECT * FROM admin_users WHERE username=?", (username,)).fetchone()

    if not user or not security.verify_password(password, user["password_hash"]):
        record_failed_login(ip)
        remaining = max(0, MAX_LOGIN_ATTEMPTS - len(LOGIN_ATTEMPTS.get(ip, [])))
        database.record_audit(username, "FAILED_LOGIN_PASSWORD", {"ip": ip, "attempts_remaining": remaining})
        raise HTTPException(status_code=401, detail="Invalid username or password")

    clear_failed_logins(ip)
    session_lifetime = 2592000
    session_token = security.create_session(user["id"], ip, ua, lifetime_seconds=session_lifetime)
    database.record_audit(username, "SUCCESSFUL_LOGIN", {"ip": ip, "method": "API_PASSWORD"})

    resp = JSONResponse({"status": "ok", "user": {"username": username}})
    resp.set_cookie(key="admin_session", value=session_token, max_age=session_lifetime, httponly=True, samesite="lax")
    return resp

@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    token = request.cookies.get("admin_session")
    if token:
        security.destroy_session(token)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(key="admin_session")
    return resp

@app.get("/api/dashboard")
async def api_dashboard(request: Request, user: dict = Depends(require_api_auth)):
    data = await telemetry_service.aggregate_all_telemetry()
    summary = data.get("summary", {})
    clients = data.get("clients", [])

    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_ist = datetime.datetime.now(IST)
    weekday = now_ist.weekday()
    is_weekday = weekday < 5
    current_time_str = now_ist.strftime("%H:%M:%S")

    mcx_open = is_weekday and (datetime.time(9, 0) <= now_ist.time() <= datetime.time(23, 55))
    nse_open = is_weekday and (datetime.time(9, 15) <= now_ist.time() <= datetime.time(15, 30))

    formatted_clients = []
    paper_count = 0
    live_count = 0
    total_positions = 0

    for c in clients:
        is_paper = bool(c.get("paper_mode", False))
        if is_paper:
            paper_count += 1
        else:
            live_count += 1

        c_positions = c.get("positions", [])
        total_positions += len(c_positions)

        formatted_clients.append({
            "id": c.get("id"),
            "name": c.get("name") or c.get("id"),
            "broker_client_id": c.get("client_id") or c.get("id"),
            "status": "PAUSED" if c.get("status") == "PAUSED" else ("ACTIVE" if c.get("healthy") else c.get("status", "UNKNOWN")),
            "trading_paused": c.get("status") == "PAUSED",
            "container_status": c.get("docker_status", "RUNNING"),
            "execution_mode": "PAPER" if is_paper else "LIVE",
            "net_mtm": float(c.get("net_mtm", 0.0)),
            "realized_pnl": float(c.get("realized_pnl", 0.0)),
            "unrealized_mtm": float(c.get("unrealized_mtm", 0.0)),
            "open_positions_count": len(c_positions),
            "orders_count": len(c.get("broker_orders", [])),
            "available_margin": float(c.get("available_margin", 0.0)),
            "margin_used": float(c.get("margin_used", 0.0)),
            "strategies_count": c.get("supertrend", {}).get("total_strategies", 0),
            "active_strategies_count": c.get("supertrend", {}).get("active_strategies_count", 0),
            "webhook_url": build_webhook_info(request, c.get("id"), "")["webhook_url"]
        })

    return {
        "aggregate_net_mtm": float(summary.get("total_net_mtm", 0.0)),
        "aggregate_realized_pnl": float(summary.get("total_realized_pnl", 0.0)),
        "aggregate_unrealized_mtm": float(summary.get("total_unrealized_mtm", 0.0)),
        "total_clients_count": len(clients),
        "active_clients_count": summary.get("active_clients", 0),
        "paper_clients_count": paper_count,
        "live_clients_count": live_count,
        "open_positions_total": total_positions,
        "market_status": {
            "mcx_open": mcx_open,
            "nse_open": nse_open,
            "current_time_ist": current_time_str
        },
        "clients": formatted_clients
    }

@app.get("/api/clients/{tenant_id}")
async def api_client_detail(tenant_id: str, request: Request, user: dict = Depends(require_api_auth)):
    c = await telemetry_service.get_single_client_telemetry(tenant_id)
    if c.get("status") == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="Client not found")

    is_paper = bool(c.get("paper_mode", False))
    positions = c.get("positions", [])
    orders = c.get("broker_orders", [])
    trades = c.get("broker_trades", [])
    strategies = c.get("supertrend", {}).get("strategies", [])

    formatted_strategies = []
    for s in strategies:
        formatted_strategies.append({
            "id": str(s.get("id")),
            "tenant_id": tenant_id,
            "symbol": s.get("symbol"),
            "exchange_segment": s.get("exchange_segment", "MCXFO"),
            "timeframe": s.get("timeframe", "5m"),
            "quantity": s.get("quantity", 1),
            "product_type": s.get("product_type", "MIS"),
            "atr_period": s.get("atr_period", 7),
            "multiplier": float(s.get("multiplier", 3.0)),
            "execution_mode": s.get("execution_mode", "LIVE"),
            "is_enabled": bool(s.get("is_enabled", True)),
            "virtual_position": s.get("virtual_position", 0),
            "active_contract_id": s.get("active_contract_id", ""),
            "active_contract_desc": s.get("active_contract_desc", s.get("symbol")),
            "current_trend": s.get("current_trend", "INITIALIZING"),
            "last_eval_time": s.get("last_eval_time", "")
        })

    client_summary = {
        "id": c.get("id"),
        "name": c.get("name") or c.get("id"),
        "broker_client_id": c.get("client_id") or c.get("id"),
        "status": "PAUSED" if c.get("status") == "PAUSED" else ("ACTIVE" if c.get("healthy") else c.get("status", "UNKNOWN")),
        "trading_paused": c.get("status") == "PAUSED",
        "container_status": c.get("docker_status", "RUNNING"),
        "execution_mode": "PAPER" if is_paper else "LIVE",
        "net_mtm": float(c.get("net_mtm", 0.0)),
        "realized_pnl": float(c.get("realized_pnl", 0.0)),
        "unrealized_mtm": float(c.get("unrealized_mtm", 0.0)),
        "open_positions_count": len(positions),
        "orders_count": len(orders),
        "available_margin": float(c.get("available_margin", 0.0)),
        "margin_used": float(c.get("margin_used", 0.0)),
        "strategies_count": len(strategies),
        "active_strategies_count": sum(1 for s in strategies if s.get("is_enabled")),
        "webhook_url": build_webhook_info(request, tenant_id, "")["webhook_url"]
    }

    margin_info = {
        "available_margin": float(c.get("available_margin", 0.0)),
        "margin_used": float(c.get("margin_used", 0.0)),
        "total_collateral": float(c.get("total_collateral", 0.0)),
        "net_margin_available": float(c.get("net_margin_available", 0.0)),
        "cash_available": float(c.get("mcx_margin", {}).get("cash_available", 0.0)),
        "pay_in_amount": float(c.get("mcx_margin", {}).get("pay_in_amount", 0.0)),
        "total_account_value": float(c.get("total_account_value", 0.0)),
    }

    return {
        "client": client_summary,
        "positions": positions,
        "orders": orders,
        "trades": trades,
        "strategies": formatted_strategies,
        "margin": margin_info
    }

@app.post("/admin/clients/{tenant_id}/toggle-trading")
async def toggle_trading_action(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_api_auth)
):
    try:
        body = await request.json()
    except Exception:
        body = {}

    pause = bool(body.get("pause", False))
    target_status = "PAUSED" if pause else "ACTIVE"

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("UPDATE tenants SET status=?, updated_at=? WHERE id=?", (target_status, time.time(), tenant_id))

    if pause:
        docker_manager.stop_client_container(tenant_id)
        action_name = "PAUSE_CLIENT"
    else:
        docker_manager.restart_client_container(tenant_id)
        action_name = "RESUME_CLIENT"

    caddy_manager.sync_caddy_config()
    database.record_audit(user["username"], action_name, {"status": target_status}, tenant_id)
    return {"status": "ok", "trading_paused": pause}

@app.post("/api/clients")
async def api_provision_client(request: Request, user: dict = Depends(require_api_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}

    clean_id = str(body.get("tenant_id", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    if not clean_id or not name:
        raise HTTPException(status_code=400, detail="Tenant ID and Name are required")

    api_key = str(body.get("app_key", "")).strip()
    api_secret = str(body.get("secret_key", "")).strip()
    client_id = str(body.get("broker_client_id", clean_id)).strip()
    is_paper = 1 if str(body.get("execution_mode", "LIVE")).upper() == "PAPER" else 0
    now = time.time()

    creds_payload = {
        "API_KEY": api_key,
        "API_SECRET": api_secret,
        "MD_API_KEY": api_key,
        "MD_API_SECRET": api_secret,
        "CLIENT_ID": client_id,
        "WEBHOOK_SECRET": str(uuid.uuid4()).replace("-", ""),
        "XTS_API_BASE_URL": "https://symphony.acagarwal.com:3000/interactive"
    }
    enc_creds = security.encrypt_credentials(creds_payload)

    with closing(database.get_db_connection()) as conn:
        with conn:
            existing = conn.execute("SELECT id FROM tenants WHERE id=?", (clean_id,)).fetchone()
            if existing:
                raise HTTPException(status_code=400, detail=f"Client ID '{clean_id}' already exists")

            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, 'ACTIVE', ?, ?)",
                (clean_id, name, now, now)
            )
            conn.execute(
                "INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)",
                (clean_id, enc_creds, now)
            )
            conn.execute("""
                INSERT INTO tenant_risk_limits (
                    tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr,
                    max_daily_loss_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at
                ) VALUES (?, 100, 5000000.0, 10000000.0, 50000.0, 0.005, 7, ?, ?)
            """, (clean_id, is_paper, now))

    docker_manager.provision_client_container(clean_id)
    caddy_manager.sync_caddy_config()
    database.record_audit(user["username"], "PROVISION_CLIENT", {"name": name, "paper_mode": bool(is_paper)}, clean_id)
    return {"status": "ok", "client_id": clean_id}

@app.delete("/api/clients/{tenant_id}")
async def api_delete_client(tenant_id: str, user: dict = Depends(require_api_auth)):
    docker_manager.remove_client_container(tenant_id)
    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
    caddy_manager.sync_caddy_config()
    database.record_audit(user["username"], "DELETE_CLIENT", {}, tenant_id)
    return {"status": "ok"}

@app.get("/api/clients/{tenant_id}/strategies")
async def api_get_strategies(tenant_id: str, user: dict = Depends(require_api_auth)):
    c = await telemetry_service.get_single_client_telemetry(tenant_id)
    return {"strategies": c.get("supertrend", {}).get("strategies", [])}

@app.post("/api/clients/{tenant_id}/strategies")
async def api_save_strategy(tenant_id: str, request: Request, user: dict = Depends(require_api_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}

    clean_sym = re.sub(r'[\s\-_]+', '', str(body.get("symbol", "")).strip().upper())
    if not clean_sym:
        raise HTTPException(status_code=400, detail="Symbol is required")

    if clean_sym in ("GOLDPETAL", "GOLD", "SILVER100", "SILVERM", "SILVERMIC", "SILVER", "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATURALGASM", "COPPER", "ZINC", "LEAD", "ALUMINIUM"):
        clean_sym = f"{clean_sym}1!"

    clean_tf = str(body.get("timeframe", "5m")).strip().lower()
    clean_qty = max(1, int(body.get("quantity", 1)))
    clean_atr = max(2, int(body.get("atr_period", 7)))
    clean_mult = max(0.1, float(body.get("multiplier", 3.0)))
    clean_mode = "PAPER" if str(body.get("execution_mode", "")).upper() == "PAPER" else "LIVE"
    clean_prod = str(body.get("product_type", "MIS")).upper()
    now = time.time()

    with closing(database.get_db_connection()) as conn:
        with conn:
            existing = conn.execute("SELECT id FROM tenant_supertrend_strategies WHERE tenant_id=? AND symbol=? AND timeframe=?", (tenant_id, clean_sym, clean_tf)).fetchone()
            if existing:
                strat_id = existing["id"]
            else:
                strat_id = f"st_{tenant_id}_{clean_sym.lower()}_{clean_tf}"
                cur_count = conn.execute("SELECT COUNT(*) FROM tenant_supertrend_strategies WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
                if cur_count >= 6:
                    raise HTTPException(status_code=400, detail="Maximum 6 strategies allowed per account.")

            conn.execute("""
                INSERT INTO tenant_supertrend_strategies (
                    id, tenant_id, symbol, exchange_segment, timeframe, quantity,
                    product_type, atr_period, multiplier, execution_mode, is_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'MCXFO', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(tenant_id, symbol, timeframe) DO UPDATE SET
                    quantity=excluded.quantity,
                    product_type=excluded.product_type,
                    atr_period=excluded.atr_period,
                    multiplier=excluded.multiplier,
                    execution_mode=excluded.execution_mode,
                    is_enabled=1,
                    updated_at=excluded.updated_at
            """, (strat_id, tenant_id, clean_sym, clean_tf, clean_qty, clean_prod, clean_atr, clean_mult, clean_mode, now, now))

    try:
        docker_manager.write_client_config(tenant_id)
    except Exception:
        pass

    database.record_audit(user["username"], "SAVE_SUPERTREND_STRATEGY", {"symbol": clean_sym, "timeframe": clean_tf, "quantity": clean_qty}, tenant_id)
    return {"status": "ok", "strategy_id": strat_id}

@app.post("/api/clients/{tenant_id}/strategies/{strategy_id}/toggle")
async def api_toggle_strategy(tenant_id: str, strategy_id: str, request: Request, user: dict = Depends(require_api_auth)):
    with closing(database.get_db_connection()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_strategies WHERE tenant_id=? AND id=?", (tenant_id, strategy_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Strategy not found")
            new_state = 0 if row["is_enabled"] else 1
            conn.execute("UPDATE tenant_supertrend_strategies SET is_enabled=?, updated_at=? WHERE id=?", (new_state, time.time(), strategy_id))

    try:
        docker_manager.write_client_config(tenant_id)
    except Exception:
        pass

    port = docker_manager.get_tenant_port(tenant_id)
    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    for target_url in [
        f"http://127.0.0.1:{port}/internal/supertrend/strategy/{strategy_id}/toggle",
        f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/{strategy_id}/toggle",
        f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/strategy/{strategy_id}/toggle"
    ]:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(target_url, headers=headers, json={"is_enabled": bool(new_state)}, timeout=5.0)
                if resp.status_code == 200:
                    break
        except Exception:
            pass

    database.record_audit(user["username"], "TOGGLE_SUPERTREND_STRATEGY", {"strategy_id": strategy_id, "is_enabled": bool(new_state)}, tenant_id)
    return {"status": "ok", "is_enabled": bool(new_state)}

@app.delete("/api/clients/{tenant_id}/strategies/{strategy_id}")
async def api_delete_strategy(tenant_id: str, strategy_id: str, user: dict = Depends(require_api_auth)):
    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM tenant_supertrend_strategies WHERE tenant_id=? AND id=?", (tenant_id, strategy_id))

    try:
        docker_manager.write_client_config(tenant_id)
    except Exception:
        pass

    database.record_audit(user["username"], "DELETE_SUPERTREND_STRATEGY", {"strategy_id": strategy_id}, tenant_id)
    return {"status": "ok"}

@app.post("/api/clients/{tenant_id}/strategies/evaluate-now")
async def api_evaluate_strategy_now(tenant_id: str, request: Request, user: dict = Depends(require_api_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await evaluate_supertrend_now_portal(
        tenant_id=tenant_id,
        symbol=body.get("symbol"),
        strategy_id=body.get("strategy_id"),
        user=user
    )
    return {"status": "ok", "result": res}

@app.post("/api/clients/{tenant_id}/strategies/sync-trend")
async def api_sync_strategy_trend(tenant_id: str, request: Request, user: dict = Depends(require_api_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await sync_supertrend_trend_portal(
        tenant_id=tenant_id,
        strategy_id=body.get("strategy_id"),
        user=user
    )
    return {"status": "ok", "message": "Trend synchronization triggered", "result": res}

@app.post("/api/clients/{tenant_id}/strategies/reset-flat")
async def api_reset_strategy_flat(tenant_id: str, request: Request, user: dict = Depends(require_api_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await reset_supertrend_strategy_flat_portal(
        tenant_id=tenant_id,
        strategy_id=body.get("strategy_id"),
        square_off_broker=1 if body.get("square_off_broker") else 0,
        request=request,
        user=user
    )
    return {"status": "ok", "message": "Reset flat executed", "result": res}

@app.get("/api/clients/{tenant_id}/candles")
async def api_client_candles(
    tenant_id: str,
    symbol: str,
    timeframe: str = "5m",
    limit: int = 300,
    strategy_id: Optional[str] = None,
    user: dict = Depends(require_api_auth)
):
    chart_data = await get_supertrend_chart_data(
        tenant_id=tenant_id,
        timeframe=timeframe,
        symbol=symbol,
        strategy_id=strategy_id,
        user=user
    )
    return {
        "candles": chart_data.get("candlestick", []),
        "supertrend_line": chart_data.get("supertrend_line", []),
        "upper_band": chart_data.get("upper_band", []),
        "lower_band": chart_data.get("lower_band", []),
        "markers": chart_data.get("markers", []),
        "symbol": chart_data.get("symbol", symbol),
        "timeframe": chart_data.get("timeframe", timeframe)
    }

@app.get("/api/orders")
async def api_global_orders(
    request: Request,
    status: Optional[str] = None,
    search: Optional[str] = None,
    user: dict = Depends(require_api_auth)
):
    data = await telemetry_service.aggregate_all_telemetry()
    all_orders = []

    for c in data.get("clients", []):
        cid = c.get("id")
        cname = c.get("name") or cid
        for ord_item in c.get("broker_orders", []):
            order_dict = dict(ord_item)
            order_dict["client_id"] = cid
            order_dict["client_name"] = cname
            all_orders.append(order_dict)

    return {"orders": all_orders}

@app.post("/api/clients/{tenant_id}/orders/{app_order_id}/cancel")
async def api_cancel_order(
    tenant_id: str,
    app_order_id: str,
    request: Request,
    user: dict = Depends(require_api_auth)
):
    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/orders/{app_order_id}/cancel"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/orders/{app_order_id}/cancel"
    url_local = f"http://127.0.0.1:{port}/internal/orders/{app_order_id}/cancel"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, timeout=5.0)
                if resp.status_code in (200, 202):
                    database.record_audit(user["username"], "CANCEL_ORDER", {"app_order_id": app_order_id}, tenant_id)
                    return {"status": "ok", "result": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else "Order cancellation sent"}
            except Exception:
                pass

    return {"status": "error", "message": "Failed to reach client container"}

@app.post("/api/clients/{tenant_id}/positions/square-off")
async def api_square_off_position(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_api_auth)
):
    try:
        body = await request.json()
    except Exception:
        body = {}

    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/positions/square-off"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/positions/square-off"
    url_local = f"http://127.0.0.1:{port}/internal/positions/square-off"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, json=body, timeout=10.0)
                if resp.status_code in (200, 202):
                    res = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"status": "ok"}
                    database.record_audit(user["username"], "SQUARE_OFF_POSITION", {"target": body, "result": res}, tenant_id)
                    return res
            except Exception:
                pass

    return JSONResponse(status_code=502, content={"status": "error", "message": f"Failed to reach client {tenant_id} container for position square-off"})

@app.post("/api/orders/bulk-cancel")
async def api_bulk_cancel_orders(
    request: Request,
    user: dict = Depends(require_api_auth)
):
    try:
        body = await request.json()
    except Exception:
        body = {}

    target_tenant = body.get("tenant_id")
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    headers = {}
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    if target_tenant and target_tenant != "ALL":
        port = docker_manager.get_tenant_port(target_tenant)
        urls = [
            f"http://127.0.0.1:{port}/internal/orders/cancel-all",
            f"{telemetry_service.CADDY_PROXY_BASE}/{target_tenant}/internal/orders/cancel-all",
            f"http://xts_client_{target_tenant}:8000/internal/orders/cancel-all"
        ]
        async with httpx.AsyncClient() as client:
            for u in urls:
                try:
                    resp = await client.post(u, headers=headers, timeout=8.0)
                    if resp.status_code in (200, 202):
                        database.record_audit(user["username"], "BULK_CANCEL_ORDERS", {"tenant_id": target_tenant})
                        return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"status": "ok"}
                except Exception:
                    pass
        return JSONResponse(status_code=502, content={"status": "error", "message": f"Failed to reach client {target_tenant}"})

    # Bulk cancel across all active clients
    with closing(database.get_db_connection()) as conn:
        tenants = conn.execute("SELECT id FROM tenants WHERE status='ACTIVE'").fetchall()

    async def _cancel_one(tid: str):
        p = docker_manager.get_tenant_port(tid)
        for u in [
            f"http://127.0.0.1:{p}/internal/orders/cancel-all",
            f"{telemetry_service.CADDY_PROXY_BASE}/{tid}/internal/orders/cancel-all",
            f"http://xts_client_{tid}:8000/internal/orders/cancel-all"
        ]:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(u, headers=headers, timeout=5.0)
                    if resp.status_code in (200, 202):
                        return {"tenant_id": tid, "status": "success"}
            except Exception:
                pass
        return {"tenant_id": tid, "status": "unreachable"}

    results = await asyncio.gather(*[_cancel_one(t["id"]) for t in tenants], return_exceptions=True)
    database.record_audit(user["username"], "BULK_CANCEL_ALL_CLIENTS", {"results": str(results)})
    return {"status": "ok", "cancelled_clients": len(tenants), "results": results}

@app.get("/api/clients/{tenant_id}/settings")
async def api_get_client_settings(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_api_auth)
):
    with closing(database.get_db_connection()) as conn:
        t_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not t_row:
            raise HTTPException(status_code=404, detail="Client not found")
        c_row = conn.execute("SELECT * FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
        r_row = conn.execute("SELECT * FROM tenant_risk_limits WHERE tenant_id=?", (tenant_id,)).fetchone()

    creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    risk_dict = dict(r_row) if r_row else {}

    return {
        "name": t_row["name"],
        "credentials": {
            "api_key": creds.get("API_KEY", ""),
            "api_secret": creds.get("API_SECRET", ""),
            "broker_client_id": creds.get("CLIENT_ID", tenant_id),
            "execution_mode": "PAPER" if risk_dict.get("paper_trade_mode") else "LIVE"
        },
        "risk_limits": {
            "max_lots_limit": risk_dict.get("max_lots_limit", 100),
            "max_order_value_inr": float(risk_dict.get("max_order_value_inr", 5000000.0)),
            "daily_notional_cap_inr": float(risk_dict.get("daily_notional_cap_inr", 10000000.0)),
            "max_daily_loss_inr": float(risk_dict.get("max_daily_loss_inr", 50000.0)),
            "slippage_buffer_pct": float(risk_dict.get("slippage_buffer_pct", 0.005)),
            "min_days_before_expiry_mcx": int(risk_dict.get("min_days_before_expiry_mcx", 7)),
        },
        "webhook": {
            "webhook_url": build_webhook_info(request, tenant_id, "")["webhook_url"],
            "webhook_secret": creds.get("WEBHOOK_SECRET", "")
        }
    }

@app.put("/api/clients/{tenant_id}/credentials")
async def api_update_client_credentials(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_api_auth)
):
    try:
        body = await request.json()
    except Exception:
        body = {}

    with closing(database.get_db_connection()) as conn:
        t_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not t_row:
            raise HTTPException(status_code=404, detail="Client not found")
        c_row = conn.execute("SELECT * FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()

    existing_creds = security.decrypt_credentials(c_row["encrypted_payload"]) if c_row else {}
    name = str(body.get("name", t_row["name"])).strip() or t_row["name"]
    api_key = str(body.get("api_key", existing_creds.get("API_KEY", ""))).strip()
    api_secret = str(body.get("api_secret", existing_creds.get("API_SECRET", ""))).strip()
    client_id = str(body.get("broker_client_id", existing_creds.get("CLIENT_ID", tenant_id))).strip()
    is_paper = 1 if str(body.get("execution_mode", "LIVE")).upper() == "PAPER" else 0
    now = time.time()

    creds_payload = {
        "API_KEY": api_key,
        "API_SECRET": api_secret,
        "MD_API_KEY": api_key,
        "MD_API_SECRET": api_secret,
        "CLIENT_ID": client_id,
        "WEBHOOK_SECRET": existing_creds.get("WEBHOOK_SECRET", uuid.uuid4().hex),
        "XTS_API_BASE_URL": existing_creds.get("XTS_API_BASE_URL", "https://symphony.acagarwal.com:3000/interactive")
    }
    enc_creds = security.encrypt_credentials(creds_payload)

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("UPDATE tenants SET name=?, updated_at=? WHERE id=?", (name, now, tenant_id))
            conn.execute("UPDATE tenant_credentials SET encrypted_payload=?, updated_at=? WHERE tenant_id=?", (enc_creds, now, tenant_id))
            conn.execute("UPDATE tenant_risk_limits SET paper_trade_mode=?, updated_at=? WHERE tenant_id=?", (is_paper, now, tenant_id))

    try:
        docker_manager.restart_client_container(tenant_id)
    except Exception as d_err:
        logger.warning(f"Container restart warning for {tenant_id}: {d_err}")

    database.record_audit(user["username"], "UPDATE_CLIENT_CREDENTIALS", {"execution_mode": "PAPER" if is_paper else "LIVE"}, tenant_id)
    return {"status": "ok", "message": "Credentials updated and client container restarted"}

@app.put("/api/clients/{tenant_id}/risk-limits")
async def api_update_client_risk_limits(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_api_auth)
):
    try:
        body = await request.json()
    except Exception:
        body = {}

    max_lots = max(1, int(body.get("max_lots_limit", 100)))
    max_order_val = max(1000.0, float(body.get("max_order_value_inr", 5000000.0)))
    daily_notional = max(1000.0, float(body.get("daily_notional_cap_inr", 10000000.0)))
    max_daily_loss = max(500.0, float(body.get("max_daily_loss_inr", 50000.0)))
    slippage_buf = max(0.0001, min(0.05, float(body.get("slippage_buffer_pct", 0.005))))
    min_days_mcx = max(0, int(body.get("min_days_before_expiry_mcx", 7)))
    now = time.time()

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("""
                UPDATE tenant_risk_limits SET
                    max_lots_limit=?, max_order_value_inr=?, daily_notional_cap_inr=?,
                    max_daily_loss_inr=?, slippage_buffer_pct=?, min_days_before_expiry_mcx=?, updated_at=?
                WHERE tenant_id=?
            """, (max_lots, max_order_val, daily_notional, max_daily_loss, slippage_buf, min_days_mcx, now, tenant_id))

    try:
        docker_manager.write_client_config(tenant_id)
    except Exception as d_err:
        logger.warning(f"Config write warning for {tenant_id}: {d_err}")

    database.record_audit(user["username"], "UPDATE_RISK_LIMITS", {
        "max_lots_limit": max_lots,
        "max_daily_loss_inr": max_daily_loss,
        "daily_notional_cap_inr": daily_notional,
        "slippage_buffer_pct": slippage_buf,
        "min_days_before_expiry_mcx": min_days_mcx
    }, tenant_id)
    return {"status": "ok", "message": "Risk limits saved and client config regenerated"}

@app.post("/api/clients/{tenant_id}/webhook-secret/rotate")
async def api_rotate_webhook_secret(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_api_auth)
):
    with closing(database.get_db_connection()) as conn:
        c_row = conn.execute("SELECT * FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
        if not c_row:
            raise HTTPException(status_code=404, detail="Client credentials not found")

    existing_creds = security.decrypt_credentials(c_row["encrypted_payload"])
    new_secret = uuid.uuid4().hex
    existing_creds["WEBHOOK_SECRET"] = new_secret
    enc_creds = security.encrypt_credentials(existing_creds)
    now = time.time()

    with closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute("UPDATE tenant_credentials SET encrypted_payload=?, updated_at=? WHERE tenant_id=?", (enc_creds, now, tenant_id))

    try:
        docker_manager.write_client_config(tenant_id)
    except Exception:
        pass

    webhook_info = build_webhook_info(request, tenant_id, "")
    database.record_audit(user["username"], "ROTATE_WEBHOOK_SECRET", {}, tenant_id)
    return {"status": "ok", "webhook_secret": new_secret, "webhook_url": webhook_info["webhook_url"]}

@app.get("/api/audit-logs")
async def api_audit_logs(limit: int = 100, user: dict = Depends(require_api_auth)):
    with closing(database.get_db_connection()) as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()

    logs = []
    for r in rows:
        d = dict(r)
        d["formatted_time"] = format_epoch_to_ist(d.get("timestamp"))
        logs.append(d)
    return {"logs": logs}

# WebSocket Telemetry Streaming Endpoint
@app.websocket("/ws/telemetry")
async def websocket_telemetry_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"type": "connection_status", "connected": True})

    try:
        while True:
            data = await telemetry_service.aggregate_all_telemetry()
            summary = data.get("summary", {})
            clients = data.get("clients", [])

            IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            now_ist = datetime.datetime.now(IST)
            weekday = now_ist.weekday()
            is_weekday = weekday < 5

            mcx_open = is_weekday and (datetime.time(9, 0) <= now_ist.time() <= datetime.time(23, 55))
            nse_open = is_weekday and (datetime.time(9, 15) <= now_ist.time() <= datetime.time(15, 30))

            paper_count = sum(1 for c in clients if c.get("paper_mode"))
            live_count = len(clients) - paper_count
            total_positions = sum(len(c.get("positions", [])) for c in clients)

            formatted_clients = []
            for c in clients:
                formatted_clients.append({
                    "id": c.get("id"),
                    "name": c.get("name") or c.get("id"),
                    "broker_client_id": c.get("client_id") or c.get("id"),
                    "status": "PAUSED" if c.get("status") == "PAUSED" else ("ACTIVE" if c.get("healthy") else c.get("status", "UNKNOWN")),
                    "trading_paused": c.get("status") == "PAUSED",
                    "container_status": c.get("docker_status", "RUNNING"),
                    "execution_mode": "PAPER" if c.get("paper_mode") else "LIVE",
                    "net_mtm": float(c.get("net_mtm", 0.0)),
                    "realized_pnl": float(c.get("realized_pnl", 0.0)),
                    "unrealized_mtm": float(c.get("unrealized_mtm", 0.0)),
                    "open_positions_count": len(c.get("positions", [])),
                    "orders_count": len(c.get("broker_orders", [])),
                    "available_margin": float(c.get("available_margin", 0.0)),
                    "margin_used": float(c.get("margin_used", 0.0)),
                    "strategies_count": c.get("supertrend", {}).get("total_strategies", 0),
                    "active_strategies_count": c.get("supertrend", {}).get("active_strategies_count", 0),
                })

            telemetry_payload = {
                "type": "telemetry_update",
                "aggregate_net_mtm": float(summary.get("total_net_mtm", 0.0)),
                "aggregate_realized_pnl": float(summary.get("total_realized_pnl", 0.0)),
                "aggregate_unrealized_mtm": float(summary.get("total_unrealized_mtm", 0.0)),
                "total_clients_count": len(clients),
                "active_clients_count": summary.get("active_clients", 0),
                "paper_clients_count": paper_count,
                "live_clients_count": live_count,
                "open_positions_total": total_positions,
                "market_status": {
                    "mcx_open": mcx_open,
                    "nse_open": nse_open,
                    "current_time_ist": now_ist.strftime("%H:%M:%S")
                },
                "clients": formatted_clients
            }

            await websocket.send_json(telemetry_payload)
            await asyncio.sleep(2.5)
    except (WebSocketDisconnect, Exception):
        pass

# =====================================================================
# SPA STATIC ASSETS & HTML CATCH-ALL ROUTING
# =====================================================================
FRONTEND_DIST = os.path.join(PORTAL_DIR, "frontend", "dist")
if not os.path.exists(FRONTEND_DIST):
    FRONTEND_DIST = "/app/frontend/dist"

if os.path.exists(FRONTEND_DIST):
    assets_dir = os.path.join(FRONTEND_DIST, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    async def serve_spa_root(request: Request):
        index_path = os.path.join(FRONTEND_DIST, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    @app.get("/{full_path:path}")
    async def serve_spa_catchall(full_path: str, request: Request):
        if full_path.startswith("admin") or full_path.startswith("api") or full_path.startswith("ws") or full_path.startswith("webhook"):
            raise HTTPException(status_code=404, detail="Endpoint not found")
        index_path = os.path.join(FRONTEND_DIST, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return RedirectResponse(url="/admin/dashboard", status_code=303)
