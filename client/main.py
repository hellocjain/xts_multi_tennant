from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager, closing
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import logging
import time
import hashlib
import hmac
import math
import json
import os
import sqlite3
import threading
import uuid
import anyio
import re
import sys
import asyncio

import config
import xts_api
from supertrend_engine import SuperTrendEngine

supertrend_engine = SuperTrendEngine()

# Sanitize string variables
for _key in ("WEBHOOK_SECRET", "CLIENT_ID", "API_KEY", "API_SECRET", "MD_API_KEY", "MD_API_SECRET", "XTS_API_BASE_URL"):
    if hasattr(config, _key):
        setattr(config, _key, str(getattr(config, _key, "")).strip())

if int(os.environ.get("WEB_CONCURRENCY", 1)) > 1 or int(os.environ.get("WORKERS", 1)) > 1:
    raise RuntimeError("CRITICAL: Bot relies on global memory for master contract caching. You must use --workers 1.")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

FAILED_ATTEMPTS = OrderedDict()
MAX_TRACKED_IPS = getattr(config, "MAX_TRACKED_FAILED_IPS", 2000)
FAILED_ATTEMPTS_LOCK = threading.Lock()

def _record_failed_attempt(client_ip, now):
    with FAILED_ATTEMPTS_LOCK:
        attempts = FAILED_ATTEMPTS.pop(client_ip, [])
        attempts = [ts for ts in attempts if now - ts < 900]
        attempts.append(now)
        FAILED_ATTEMPTS[client_ip] = attempts
        FAILED_ATTEMPTS.move_to_end(client_ip)
        while len(FAILED_ATTEMPTS) > MAX_TRACKED_IPS:
            FAILED_ATTEMPTS.popitem(last=False)

def _clear_failed_attempts(client_ip):
    with FAILED_ATTEMPTS_LOCK:
        FAILED_ATTEMPTS.pop(client_ip, None)

DATA_DIR = getattr(config, "DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(DATA_DIR, "signals.db")
_DB_LOCK = threading.Lock()

def _db_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-2000")
    return conn

def db_init():
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    received_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS signal_dedup (hash TEXT PRIMARY KEY, timestamp REAL)")
            conn.commit()

def _sanitize_dict(d):
    if not isinstance(d, dict):
        return d
    sanitized = {}
    sensitive_keys = {"secret", "api_key", "api_secret", "password", "token", "totp_secret", "webhook_secret"}
    for k, v in d.items():
        if str(k).lower() in sensitive_keys:
            sanitized[k] = "***MASKED***"
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_dict(v)
        else:
            sanitized[k] = v
    return sanitized

def db_insert_pending(sig_id, payload_dict):
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            now = time.time()
            clean_payload = _sanitize_dict(payload_dict) if isinstance(payload_dict, dict) else payload_dict
            conn.execute(
                "INSERT INTO signals (id, received_at, payload, status, result, updated_at) "
                "VALUES (?, ?, ?, 'pending', NULL, ?)",
                (sig_id, now, json.dumps(clean_payload), now),
            )
            conn.commit()

def db_update_status(sig_id, status, result=None, payload=None):
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            clean_res = _sanitize_dict(result) if isinstance(result, dict) else result
            if payload is not None:
                clean_payload = _sanitize_dict(payload) if isinstance(payload, dict) else payload
                conn.execute(
                    "UPDATE signals SET status=?, result=?, payload=?, updated_at=? WHERE id=?",
                    (status, json.dumps(clean_res) if clean_res is not None else None, json.dumps(clean_payload), time.time(), sig_id),
                )
            else:
                conn.execute(
                    "UPDATE signals SET status=?, result=?, updated_at=? WHERE id=?",
                    (status, json.dumps(clean_res) if clean_res is not None else None, time.time(), sig_id),
                )
            conn.commit()

def db_fetch_unfinished():
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            return conn.execute(
                "SELECT id, received_at, payload, status FROM signals WHERE status IN ('pending','processing')"
            ).fetchall()

def db_fetch_recent(limit=10):
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            rows = conn.execute(
                "SELECT id, received_at, payload, status, result, updated_at FROM signals ORDER BY received_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            signals = []
            for r in rows:
                try:
                    payload = json.loads(r[2]) if r[2] else {}
                    result = json.loads(r[4]) if r[4] else None
                except Exception:
                    payload, result = {}, None
                signals.append({
                    "id": r[0],
                    "received_at": r[1],
                    "payload": payload,
                    "status": r[3],
                    "result": result,
                    "updated_at": r[5]
                })
            return signals

def db_prune_old(max_age_seconds=7 * 24 * 3600):
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            cutoff = time.time() - max_age_seconds
            conn.execute(
                "DELETE FROM signals WHERE updated_at < ? AND status NOT IN ('pending','processing')",
                (cutoff,),
            )
            conn.commit()

def send_execution_notification(action: str, symbol: str, quantity: int, price: float, status: str, result: dict):
    """
    Asynchronously dispatches Telegram & Discord alerts on trade execution, fill, or rejection.
    Runs non-blocking in a background daemon thread so it never delays order processing.
    """
    bot_token = str(getattr(config, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
    discord_url = str(getattr(config, "DISCORD_WEBHOOK_URL", "") or "").strip()

    if not (bot_token and chat_id) and not discord_url:
        return

    client_id = getattr(config, "CLIENT_ID", "UNKNOWN")
    is_paper = bool((result.get("result") or {}).get("IsPaperTrade"))
    mode_str = "[PAPER SANDBOX]" if is_paper else "[LIVE BROKER]"
    
    res_obj = result.get("result") or {}
    app_order_id = res_obj.get("AppOrderID") or res_obj.get("appOrderID") or "N/A"
    
    if status in ("done", "paper_done"):
        title = f"🟢 ORDER FILLED {mode_str}"
        traded_price = res_obj.get("OrderAverageTradedPrice") or res_obj.get("OrderPrice") or price
        msg_text = (
            f"{title}\n"
            f"• Account: {client_id}\n"
            f"• Signal: {action} {quantity}x {symbol}\n"
            f"• Execution Price: ₹{traded_price}\n"
            f"• Order ID: {app_order_id}\n"
            f"• Status: FILLED / SUCCESS"
        )
    else:
        title = f"🔴 ORDER REJECTED / FAILED {mode_str}"
        err_code = result.get("code") or "e-order-error"
        err_desc = result.get("description") or result.get("error") or "Order placement failed"
        msg_text = (
            f"{title}\n"
            f"• Account: {client_id}\n"
            f"• Signal: {action} {quantity}x {symbol}\n"
            f"• Desired Price: ₹{price}\n"
            f"• Error Code: {err_code}\n"
            f"• Reason: {err_desc}\n"
            f"• Status: REJECTED"
        )

    def _post():
        import requests
        # 1. Telegram
        if bot_token and chat_id:
            try:
                tg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                requests.post(tg_url, json={"chat_id": chat_id, "text": msg_text}, timeout=4)
            except Exception as e:
                logger.warning(f"Telegram alert dispatch failed: {e}")

        # 2. Discord
        if discord_url:
            try:
                requests.post(discord_url, json={"content": msg_text}, timeout=4)
            except Exception as e:
                logger.warning(f"Discord alert dispatch failed: {e}")

    threading.Thread(target=_post, daemon=True, name=f"notify_{symbol}").start()

def _dispatch_and_record(sig_id, action, symbol, quantity, price, order_ref):
    db_update_status(sig_id, "processing")
    try:
        result = xts_api.execute_trade_with_retry(action, symbol, quantity, price, order_ref)
        is_paper_trade = bool((result.get("result") or {}).get("IsPaperTrade"))

        if result.get("type") == "success":
            status = "paper_done" if is_paper_trade else "done"
        else:
            status = "failed"

        audit_result = dict(result) if isinstance(result, dict) else {"raw_result": result}
        audit_result["_audit"] = {
            "action": action,
            "symbol": symbol,
            "quantity": quantity,
            "tv_price": price,
            "order_ref": order_ref,
            "is_paper_trade": is_paper_trade,
            "dispatched_at": time.time(),
        }

        db_update_status(sig_id, status, audit_result)
        send_execution_notification(action, symbol, quantity, price, status, audit_result)
    except Exception as e:
        logger.error(f"UNCAUGHT ERROR dispatching signal {sig_id}: {e}")
        err_res = {"error": str(e), "code": "e-uncaught"}
        db_update_status(sig_id, "failed", err_res)
        xts_api.send_ops_alert(f"XTS bot: uncaught error executing signal {sig_id} ({symbol}): {e}")
        send_execution_notification(action, symbol, quantity, price, "failed", err_res)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"--- CLIENT BOOT [{getattr(config, 'CLIENT_ID', 'UNKNOWN')}]: PRE-LOADING BROKER MASTER FILE ---")
    ok = xts_api.refresh_master_cache()
    if not ok:
        logger.error("--- STARTUP WARNING: master cache failed to load. Watchdog will keep retrying. ---")
    
    xts_api.start_cache_watchdog()
    xts_api.start_token_keepalive()

    try:
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = getattr(config, "BACKGROUND_THREAD_POOL_SIZE", 100)
    except Exception:
        pass

    db_init()
    stale_window = getattr(config, "STALE_SIGNAL_WINDOW_SECONDS", 30.0)
    unfinished = db_fetch_unfinished()
    
    def process_recovery(unfinished_list):
        now_time = time.time()
        with ThreadPoolExecutor(max_workers=5) as executor:
            for sig_id, received_at, payload_json, status in unfinished_list:
                age = now_time - received_at
                try:
                    data = json.loads(payload_json)
                except Exception as e:
                    db_update_status(sig_id, "abandoned_error", {"error": str(e)})
                    continue

                if age <= stale_window or status == "processing":
                    order_ref = data.get("order_ref")
                    logger.warning(f"RECOVERY: Checking broker order book for {sig_id} (Ref: {order_ref})...")
                    
                    broker_status = xts_api.check_order_status_by_ref(order_ref)
                    st_norm = str(broker_status).upper().replace("_", "").replace(" ", "")

                    NON_TERMINAL_BROKER_STATUSES = {
                        "OPEN", "NEW", "FILLED", "PARTIALLYFILLED", "PENDING", "PENDINGNEW",
                        "REPLACED", "PENDINGREPLACE", "PENDINGCANCEL", "SUCCESS", "COMPLETE", "EXECUTED"
                    }
                    TERMINAL_FAILED_STATUSES = {
                        "REJECTED", "CANCELLED", "CANCELED", "EXPIRED"
                    }
                    
                    if st_norm in NON_TERMINAL_BROKER_STATUSES:
                        logger.warning(f"RECOVERY SAFEGUARD: Order {order_ref} exists on broker ({broker_status}). Not re-executing.")
                        db_update_status(sig_id, "done", {"status": "recovered_from_broker", "broker_status": broker_status})
                        continue
                    elif st_norm in TERMINAL_FAILED_STATUSES:
                        logger.warning(f"RECOVERY SAFEGUARD: Order {order_ref} hit terminal state ({broker_status}). Not re-executing.")
                        db_update_status(sig_id, "failed", {"status": "broker_terminal_state", "broker_status": broker_status})
                        continue
                    elif broker_status == "NETWORK_ERROR":
                        logger.critical(f"RECOVERY SAFEGUARD: Network query failed for {order_ref}. Aborting auto-replay.")
                        db_update_status(sig_id, "failed", {"status": "recovery_network_failure"})
                        continue
                    elif broker_status == "NOT_FOUND" and status == "processing":
                        logger.critical(f"RECOVERY SAFEGUARD: Order {order_ref} was sent to XTS but missing from reports. Aborting replay.")
                        db_update_status(sig_id, "abandoned_manual_review_needed")
                        xts_api.send_ops_alert(f"CRITICAL: Order {order_ref} recovered but missing from XTS. Check terminal.")
                        continue
                        
                    logger.warning(f"RECOVERY: Order missing from broker. Re-queuing {sig_id}.")
                    executor.submit(
                        _dispatch_and_record,
                        sig_id, data["action"], data["symbol"], data["quantity"], 
                        data["price"], data["order_ref"]
                    )
                else:
                    logger.error(f"RECOVERY: signal {sig_id} is {age:.0f}s old — marking abandoned.")
                    db_update_status(sig_id, "abandoned_stale")

    if unfinished:
        logger.warning(f"RECOVERY: found {len(unfinished)} unfinished signal(s). Processing...")
        threading.Thread(target=process_recovery, args=(unfinished,), daemon=True).start()
    
    db_prune_old()
    logger.info(f"--- CLIENT READY [{getattr(config, 'CLIENT_ID', 'UNKNOWN')}]: SESSIONS ACTIVE ---")
    st_task = asyncio.create_task(supertrend_engine.run_loop(xts_api, sys.modules[__name__]))
    yield
    supertrend_engine.stop()
    st_task.cancel()

app = FastAPI(title="XTS Client Execution Gateway", lifespan=lifespan)

MAX_WEBHOOK_BODY_BYTES = getattr(config, "MAX_WEBHOOK_BODY_BYTES", 10_000)

def generate_order_ref(data: dict, action: str, symbol: str, quantity: int, price: float, now: float) -> str:
    # 1. Check if TradingView payload sent explicit order/alert id
    explicit_id = str(data.get("order_id") or data.get("alert_id") or data.get("order_ref") or "").strip()
    if explicit_id:
        clean_id = re.sub(r'[^a-zA-Z0-9]', '', explicit_id)[:14]
        if clean_id:
            return f"TV_{clean_id}"

    # 2. Check if TradingView payload sent bar time / timestamp
    bar_time = str(data.get("time") or data.get("timenow") or data.get("bar_time") or data.get("timestamp") or "").strip()
    if bar_time:
        sig_raw = f"{action}_{symbol}_{quantity}_{price:.4f}_{bar_time}"
    else:
        # Use a 5-second bucket so webhook re-transmissions within dedup window yield identical ref
        bucket = int(now // 5) * 5
        sig_raw = f"{action}_{symbol}_{quantity}_{price:.4f}_{bucket}"

    sig_hash_id = hashlib.md5(sig_raw.encode()).hexdigest()[:14]
    return f"TVBot_{sig_hash_id}"

def is_duplicate_signal(action: str, symbol: str, quantity: int, price: float, data: dict = None) -> bool:
    now = time.time()
    explicit_id = str(data.get("order_id") or data.get("alert_id") or data.get("order_ref") or "").strip() if data else ""
    if explicit_id:
        sig_hash = hashlib.md5(f"id_{explicit_id}".encode()).hexdigest()
    else:
        sig_hash = hashlib.md5(f"{action}_{symbol}_{quantity}_{price:.4f}".encode()).hexdigest()
    window = getattr(config, "DEDUP_WINDOW_SECONDS", 3.0)

    try:
        with _DB_LOCK: 
            with closing(_db_conn()) as conn:
                conn.execute("DELETE FROM signal_dedup WHERE timestamp < ?", (now - window,))
                try:
                    conn.execute("INSERT INTO signal_dedup (hash, timestamp) VALUES (?, ?)", (sig_hash, now))
                    conn.commit()
                    return False
                except sqlite3.IntegrityError:
                    return True 
    except Exception as e:
        logger.error(f"DEDUP SHIELD ERROR: {e}. Failing closed to prevent duplicate orders.")
        return True 

TRADING_PAUSED = False

@app.get("/health")
async def health():
    unfinished = await anyio.to_thread.run_sync(db_fetch_unfinished)
    notional_state = await anyio.to_thread.run_sync(xts_api.get_daily_notional_state)
    fut_contracts = sum(len(contracts) for contracts in xts_api.FUT_MASTER.values())
    cash_contracts = sum(len(contracts) for contracts in xts_api.CASH_MASTER.values())
    return {
        "client_id": getattr(config, "CLIENT_ID", ""),
        "trading_paused": TRADING_PAUSED,
        "cache_healthy": xts_api.cache_is_healthy(),
        "cache_date": str(xts_api.CACHE_DATE) if xts_api.CACHE_DATE else "N/A",
        "futures_loaded": len(xts_api.FUT_MASTER),
        "futures_contracts": fut_contracts,
        "cash_loaded": len(xts_api.CASH_MASTER),
        "cash_contracts": cash_contracts,
        "interactive_token_active": bool(xts_api.INTERACTIVE_TOKEN),
        "market_data_token_active": bool(xts_api.MARKET_DATA_TOKEN),
        "auth_error": xts_api.get_last_auth_error(),
        "unfinished_signals_in_queue": len(unfinished),
        "paper_trade_mode": getattr(config, "PAPER_TRADE_MODE", False),
        "notional_today": notional_state.get("notional", 0.0),
        "notional_cap": notional_state.get("cap", 0.0),
        "notional_remaining": notional_state.get("remaining", 0.0),
    }

@app.post("/internal/master/refresh")
async def refresh_master(request: Request):
    """Manually forces a refresh of the Symphony XTS instrument master cache."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    ok = await anyio.to_thread.run_sync(xts_api.refresh_master_cache, True)
    fut_contracts = sum(len(contracts) for contracts in xts_api.FUT_MASTER.values())
    cash_contracts = sum(len(contracts) for contracts in xts_api.CASH_MASTER.values())
    
    return {
        "status": "success" if ok else "error",
        "cache_healthy": ok,
        "cached_date": str(xts_api.CACHE_DATE) if xts_api.CACHE_DATE else "N/A",
        "futures_symbols": len(xts_api.FUT_MASTER),
        "futures_contracts": fut_contracts,
        "cash_symbols": len(xts_api.CASH_MASTER),
        "cash_contracts": cash_contracts,
        "message": "Master cache refreshed successfully" if ok else "Failed to download master cache from broker"
    }

SIM_PAPER_MTM = 0.0

@app.post("/internal/sim-mtm")
async def set_sim_mtm(request: Request):
    """Sets a simulated paper MTM for staging/drawdown soak testing."""
    global SIM_PAPER_MTM
    try:
        data = await request.json()
    except Exception:
        data = {}
    SIM_PAPER_MTM = float(data.get("mtm", 0.0))
    logger.info(f"STAGING: Set simulated paper MTM to ₹{SIM_PAPER_MTM:,.2f}")
    return {"status": "success", "sim_mtm": SIM_PAPER_MTM}

@app.get("/internal/telemetry")
async def telemetry(request: Request):
    """Consolidated telemetry endpoint for Admin Portal polling (Internal Network Only)."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    health_data = await health()
    pos_data = await anyio.to_thread.run_sync(xts_api.get_positions_telemetry)
    
    if SIM_PAPER_MTM != 0.0 and getattr(config, "PAPER_TRADE_MODE", False):
        pos_data["net_mtm"] = SIM_PAPER_MTM
        pos_data["unrealized_mtm"] = SIM_PAPER_MTM
    
    holdings_data = await anyio.to_thread.run_sync(xts_api.get_holdings_telemetry)
    margin_data = await anyio.to_thread.run_sync(xts_api.get_margin_telemetry)
    recent_signals = await anyio.to_thread.run_sync(db_fetch_recent, 50)
    broker_orders = await anyio.to_thread.run_sync(xts_api.get_broker_orders)
    broker_trades = await anyio.to_thread.run_sync(xts_api.get_broker_trades)
    supertrend_telemetry = supertrend_engine.get_telemetry()
    
    return {
        "health": health_data,
        "positions": pos_data,
        "holdings": holdings_data,
        "margin": margin_data,
        "recent_signals": recent_signals,
        "broker_orders": broker_orders,
        "broker_trades": broker_trades,
        "supertrend": supertrend_telemetry,
        "server_time": time.time()
    }

@app.post("/internal/supertrend/config")
async def configure_supertrend(request: Request):
    """Updates active SuperTrend configuration parameters."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    try:
        data = await request.json()
    except Exception:
        data = {}

    supertrend_engine.update_config(data)
    return {"status": "success", "telemetry": supertrend_engine.get_telemetry()}

@app.get("/internal/supertrend/status")
async def get_supertrend_status(request: Request):
    """Returns current SuperTrend strategy telemetry."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    return supertrend_engine.get_telemetry()

@app.post("/internal/pause")
async def pause_trading(request: Request):
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    global TRADING_PAUSED
    TRADING_PAUSED = True
    logger.warning(f"CIRCUIT BREAKER: Client {getattr(config, 'CLIENT_ID', '')} trading state set to PAUSED")
    return {"status": "success", "trading_paused": True}

@app.post("/internal/resume")
async def resume_trading(request: Request):
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    global TRADING_PAUSED
    TRADING_PAUSED = False
    logger.info(f"RESUMED: Client {getattr(config, 'CLIENT_ID', '')} trading state set to ACTIVE")
    return {"status": "success", "trading_paused": False}

@app.post("/panic")
async def panic(request: Request):
    """Emergency Kill-Switch API endpoint"""
    global TRADING_PAUSED
    TRADING_PAUSED = True

    try:
        data = await request.json()
    except Exception:
        data = {}
    incoming_secret = str(data.get("secret", "")).strip()
    expected_secret = getattr(config, "WEBHOOK_SECRET", "").strip()
    
    if not expected_secret or not hmac.compare_digest(incoming_secret, expected_secret):
        logger.error("REJECTED: Unauthorized /panic request or WEBHOOK_SECRET is unconfigured.")
        return JSONResponse(status_code=401, content={"status": "error", "message": "Unauthorized"})
    
    sig_id = f"panic_{str(uuid.uuid4())[:8]}"
    panic_ref = f"PANIC_{int(time.time()*1000)}"
    panic_payload = {
        "action": "PANIC_SELL",
        "symbol": "ALL_OPEN",
        "quantity": 0,
        "price": 0.0,
        "order_ref": panic_ref,
        "source": "admin_portal_killswitch"
    }
    db_insert_pending(sig_id, panic_payload)

    result = await anyio.to_thread.run_sync(xts_api.panic_square_off_all)

    # Enrich payload with executed details
    squared_list = result.get("squared_off", []) if isinstance(result, dict) else []
    if squared_list:
        total_sq_qty = sum(item.get("qty", 0) for item in squared_list)
        sym_names = ", ".join(list(set(item.get("symbol", "") for item in squared_list if item.get("symbol"))))
        panic_payload["quantity"] = total_sq_qty
        if sym_names:
            panic_payload["symbol"] = sym_names

    status = "done" if (isinstance(result, dict) and result.get("status") == "success") else "failed"
    db_update_status(sig_id, status, result, payload=panic_payload)
    return result

@app.post("/")
@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    client_ip = request.client.host or "unknown"
    now = time.time()

    if TRADING_PAUSED:
        logger.warning(f"REJECTED: Trading is currently PAUSED on client {getattr(config, 'CLIENT_ID', '')}. Webhook signal dropped.")
        return JSONResponse(status_code=403, content={"status": "error", "message": "Trading is paused on this account due to circuit breaker/kill switch"})

    body_bytes = b""
    try:
        with anyio.fail_after(5.0):
            async for chunk in request.stream():
                body_bytes += chunk
                if len(body_bytes) > MAX_WEBHOOK_BODY_BYTES:
                    _record_failed_attempt(client_ip, now)
                    return JSONResponse(status_code=413, content={"status": "error", "message": "Payload too large"})
        
        if not body_bytes:
            raise ValueError("Empty body")
            
        data = json.loads(body_bytes)
        if not isinstance(data, dict):
            raise ValueError("Payload is not a JSON object")
            
    except TimeoutError:
        _record_failed_attempt(client_ip, now)
        return JSONResponse(status_code=408, content={"status": "error", "message": "Payload transfer timed out"})
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid JSON"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

    incoming_secret = str(data.get("secret", "")).strip()
    expected_secret = getattr(config, "WEBHOOK_SECRET", "")
    secret_ok = bool(expected_secret) and hmac.compare_digest(incoming_secret, expected_secret)

    if not secret_ok:
        prior = FAILED_ATTEMPTS.get(client_ip, [])
        prior = [ts for ts in prior if now - ts < 900]
        if len(prior) >= 10:
            logger.warning(f"RATE LIMIT: IP {client_ip} temporarily banned due to brute force.")
            return JSONResponse(status_code=429, content={"status": "error", "message": "Too many failed attempts"})
        _record_failed_attempt(client_ip, now)
        logger.error(f"REJECTED: Unauthorized access attempt from IP {client_ip}.")
        return JSONResponse(status_code=401, content={"status": "error", "message": "Unauthorized"})

    _clear_failed_attempts(client_ip)

    action_raw = str(data.get("action", "")).upper().strip()
    if action_raw in ["BUY", "LONG", "COVER"]:
        action = "BUY"
    elif action_raw in ["SELL", "SHORT", "EXIT_LONG", "EXIT_SHORT", "CLOSE"]:
        action = "SELL"
    else:
        logger.error(f"REJECTED: Unrecognized action '{action_raw}'")
        return JSONResponse(status_code=422, content={"status": "error", "message": f"Invalid action: {action_raw}"})

    symbol = str(data.get("symbol", "")).upper().strip()
    max_len = getattr(config, "MAX_SYMBOL_LENGTH", 35)
    if len(symbol) > max_len:
        logger.error(f"REJECTED: Symbol too long ({len(symbol)} chars)")
        return JSONResponse(status_code=422, content={"status": "error", "message": "Symbol exceeds max length"})

    try:
        price = float(data.get("price", 0.0))
        raw_quantity = float(data.get("quantity", 0))

        if not math.isfinite(price) or not math.isfinite(raw_quantity):
            raise ValueError("Non-finite numerical payload")

        if abs(raw_quantity - round(raw_quantity)) > 1e-6:
            logger.error(f"REJECTED: Fractional quantity ({raw_quantity}) not allowed.")
            return JSONResponse(status_code=422, content={"status": "error", "message": "Quantity must be a whole number"})
        quantity = int(round(raw_quantity))
    except (ValueError, TypeError, OverflowError):
        logger.error("REJECTED: Malformed numerical values.")
        return JSONResponse(status_code=422, content={"status": "error", "message": "Invalid number format"})

    if quantity <= 0:
        return JSONResponse(status_code=422, content={"status": "error", "message": "Quantity must be > 0"})
    if price < 0:
        return JSONResponse(status_code=422, content={"status": "error", "message": "Price cannot be negative"})

    is_dup = await anyio.to_thread.run_sync(is_duplicate_signal, action, symbol, quantity, price, data)
    if is_dup:
        logger.warning(f"DEDUP SHIELD: Duplicate signal for {action} {symbol} suppressed.")
        return JSONResponse(status_code=200, content={"status": "warning", "message": "Duplicate signal suppressed"})

    order_ref = generate_order_ref(data, action, symbol, quantity, price, now)
    sig_id = str(uuid.uuid4())

    logger.info(f"Signal Approved -> Action: {action} | Symbol: {symbol} | TV Qty: {quantity} | TV Price: {price} | Ref: {order_ref}")

    payload_dict = {
        "action": action, "symbol": symbol, "quantity": quantity, "price": price, "order_ref": order_ref,
    }
    await anyio.to_thread.run_sync(db_insert_pending, sig_id, payload_dict)

    background_tasks.add_task(
        _dispatch_and_record, sig_id, action, symbol, quantity, price, order_ref
    )

    return {"status": "success", "message": "Execution queued", "signal_id": sig_id}
