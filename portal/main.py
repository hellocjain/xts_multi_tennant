from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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

import database
import security
import docker_manager
import caddy_manager
import telemetry_service

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

# =====================================================================
# AUTHENTICATION ROUTES
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
    min_days_before_expiry_mcx: int = Form(3),
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

    return templates.TemplateResponse(request=request, name="client_detail.html", context={
        "client": tel_data,
        "risk": dict(r_row) if r_row else {},
        "supertrend": supertrend_config,
        "supertrend_strategies": st_strategies,
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
                resp = await client.post(target_url, headers=headers, timeout=12.0)
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
    strat_id = id.strip() if id.strip() else f"st_{uuid.uuid4().hex[:12]}"

    with closing(database.get_db_connection()) as conn:
        with conn:
            # Check capacity limit: only enforce if inserting a brand-new symbol / record
            existing_rec = None
            if id.strip():
                existing_rec = conn.execute("SELECT id FROM tenant_supertrend_strategies WHERE tenant_id=? AND id=?", (tenant_id, id.strip())).fetchone()
            if not existing_rec:
                existing_rec = conn.execute("SELECT id FROM tenant_supertrend_strategies WHERE tenant_id=? AND symbol=?", (tenant_id, clean_sym)).fetchone()

            if not existing_rec:
                cur_count = conn.execute("SELECT COUNT(*) FROM tenant_supertrend_strategies WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
                if cur_count >= 6:
                    raise HTTPException(status_code=400, detail="Maximum strategy limit (6 symbols) reached for this account. Remove an existing strategy first.")

            conn.execute("""
                INSERT INTO tenant_supertrend_strategies (
                    id, tenant_id, symbol, exchange_segment, timeframe, quantity,
                    product_type, atr_period, multiplier, execution_mode, is_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, symbol) DO UPDATE SET
                    exchange_segment=excluded.exchange_segment,
                    timeframe=excluded.timeframe,
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

    logger.info(f"Tenant [{tenant_id}] saved SuperTrend strategy: {clean_sym} ({clean_tf}, {clean_mode}, qty={clean_qty}, enabled={clean_enabled})")

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
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/strategy/{sym}/toggle"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/{sym}/toggle"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/strategy/{sym}/toggle"

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

    database.record_audit(user["username"], "TOGGLE_SUPERTREND_STRATEGY", {"strategy_id": strategy_id, "symbol": sym, "is_enabled": bool(new_state)}, tenant_id)
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
    url_caddy = f"{telemetry_service.CADDY_PROXY_BASE}/{tenant_id}/internal/supertrend/strategy/{sym}"
    url_docker = f"http://xts_client_{tenant_id}:8000/internal/supertrend/strategy/{sym}"
    url_local = f"http://127.0.0.1:{port}/internal/supertrend/strategy/{sym}"

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

    database.record_audit(user["username"], "DELETE_SUPERTREND_STRATEGY", {"strategy_id": strategy_id, "symbol": sym}, tenant_id)
    return RedirectResponse(url=f"/admin/clients/{tenant_id}?tab=supertrend&deleted=1", status_code=303)

@app.post("/admin/clients/{tenant_id}/supertrend/config")
async def save_supertrend_config(
    tenant_id: str,
    request: Request,
    is_enabled: bool = Form(False),
    symbol: str = Form(""),
    exchange_segment: str = Form(""),
    timeframe: str = Form("5m"),
    timeframe_select: str = Form("5m"),
    custom_minutes: str = Form(""),
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

    if timeframe_select == "custom" and custom_minutes.strip().isdigit():
        clean_tf = f"{int(custom_minutes.strip())}m"
    elif timeframe.strip():
        clean_tf = timeframe.strip().lower()
    else:
        clean_tf = timeframe_select.strip().lower() or "5m"

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
    user: dict = Depends(require_auth)
):
    """Proxies candlestick and SuperTrend series data for TradingView Lightweight Charts v4."""
    with closing(database.get_db_connection()) as conn:
        st_row = conn.execute("SELECT * FROM tenant_supertrend_configs WHERE tenant_id=?", (tenant_id,)).fetchone()
        strat_rows = conn.execute("SELECT symbol, timeframe FROM tenant_supertrend_strategies WHERE tenant_id=?", (tenant_id,)).fetchall()

    cfg_sym = symbol or (strat_rows[0]["symbol"] if strat_rows else (st_row["symbol"] if st_row and st_row["symbol"] else ""))
    cfg_tf = timeframe or (strat_rows[0]["timeframe"] if strat_rows else (st_row["timeframe"] if st_row and st_row["timeframe"] else "5m"))

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

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, headers=headers, params=params, timeout=6.0)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass

    return {"status": "ERROR", "error": "Client container unreachable"}

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

