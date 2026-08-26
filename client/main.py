from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager, closing
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Any
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
from custom_strategy_engine import MultiCustomStrategyEngine

supertrend_engine = SuperTrendEngine()
custom_strategy_engine = MultiCustomStrategyEngine()

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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategy_virtual_positions (
                    strategy_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    virtual_position INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.commit()

# Ensure database tables exist on module load
try:
    db_init()
except Exception:
    pass

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

def db_get_virtual_position(strategy_key: str) -> int:
    try:
        with _DB_LOCK:
            with closing(_db_conn()) as conn:
                row = conn.execute(
                    "SELECT virtual_position FROM strategy_virtual_positions WHERE strategy_key=?",
                    (strategy_key,)
                ).fetchone()
                return int(row[0]) if row else 0
    except Exception as e:
        logger.warning(f"Error fetching virtual position for {strategy_key}: {e}")
        return 0

def db_set_virtual_position(strategy_key: str, symbol: str, timeframe: str, virtual_position: int):
    try:
        with _DB_LOCK:
            with closing(_db_conn()) as conn:
                now = time.time()
                conn.execute(
                    "INSERT INTO strategy_virtual_positions (strategy_key, symbol, timeframe, virtual_position, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(strategy_key) DO UPDATE SET virtual_position=excluded.virtual_position, updated_at=excluded.updated_at",
                    (strategy_key, symbol, timeframe, virtual_position, now)
                )
                conn.commit()
    except Exception as e:
        logger.error(f"Error saving virtual position for {strategy_key}: {e}")

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
    elif status == "partial_failure":
        title = f"⚠️ PARTIAL EXECUTION {mode_str}"
        dispatched_qty = result.get("dispatched_quantity", 0)
        total_qty = result.get("total_quantity", quantity)
        err_desc = result.get("message") or "Partial slice execution"
        msg_text = (
            f"{title}\n"
            f"• Account: {client_id}\n"
            f"• Signal: {action} {quantity}x {symbol}\n"
            f"• Slices Placed: {dispatched_qty} of {total_qty} units\n"
            f"• Status: PARTIAL FAILURE\n"
            f"• Reason: {err_desc}"
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

def _dispatch_and_record(sig_id, action, symbol, quantity, price, order_ref, is_paper=False):
    db_update_status(sig_id, "processing")
    try:
        result = xts_api.execute_trade_with_retry(action, symbol, quantity, price, order_ref, is_paper=is_paper)
        is_paper_trade = is_paper or bool((result.get("result") or {}).get("IsPaperTrade")) or getattr(config, "PAPER_TRADE_MODE", False)

        if result.get("type") == "success":
            status = "paper_done" if is_paper_trade else "done"
        elif result.get("status") == "partial_failure" or result.get("type") == "partial_failure":
            status = "partial_failure"
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
        return {"status": status, "result": audit_result}
    except Exception as e:
        logger.error(f"UNCAUGHT ERROR dispatching signal {sig_id}: {e}")
        err_res = {"error": str(e), "code": "e-uncaught"}
        db_update_status(sig_id, "failed", err_res)
        xts_api.send_ops_alert(f"XTS bot: uncaught error executing signal {sig_id} ({symbol}): {e}")
        send_execution_notification(action, symbol, quantity, price, "failed", err_res)
        return {"status": "failed", "error": str(e)}

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
    
    st_strats = getattr(config, "SUPERTREND_STRATEGIES", None)
    if st_strats and isinstance(st_strats, list):
        for s_cfg in st_strats:
            if isinstance(s_cfg, dict):
                try:
                    supertrend_engine.add_or_update_strategy(s_cfg)
                except Exception as e:
                    logger.warning(f"Error loading initial strategy {s_cfg}: {e}")
        logger.info(f"Loaded {len(supertrend_engine.strategies)} initial SuperTrend strategies.")
    else:
        st_cfg = getattr(config, "SUPERTREND_CONFIG", None)
        if st_cfg and isinstance(st_cfg, dict):
            supertrend_engine.update_config(st_cfg)
            logger.info(f"Loaded initial legacy SuperTrend configuration for {st_cfg.get('symbol')} ({st_cfg.get('timeframe')})")

    # Load initial custom Python strategies
    cs_strats = getattr(config, "CUSTOM_STRATEGIES", None)
    if cs_strats and isinstance(cs_strats, list):
        for cs_cfg in cs_strats:
            if isinstance(cs_cfg, dict):
                try:
                    custom_strategy_engine.add_or_update_strategy(cs_cfg)
                except Exception as e:
                    logger.warning(f"Error loading initial custom strategy {cs_cfg}: {e}")
        logger.info(f"Loaded {len(custom_strategy_engine.strategies)} initial custom Python strategies.")

    logger.info(f"--- CLIENT READY [{getattr(config, 'CLIENT_ID', 'UNKNOWN')}]: SESSIONS ACTIVE ---")
    st_task = asyncio.create_task(supertrend_engine.run_loop(xts_api, sys.modules[__name__]))

    async def _custom_strat_loop():
        while True:
            try:
                await custom_strategy_engine.evaluate_cycle(xts_api, sys.modules[__name__])
            except Exception as e:
                logger.error(f"Custom Strategy background loop error: {e}", exc_info=True)
            await asyncio.sleep(5)

    cs_task = asyncio.create_task(_custom_strat_loop())
    yield
    supertrend_engine.stop()
    st_task.cancel()
    cs_task.cancel()

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
    custom_strat_telemetry = custom_strategy_engine.get_telemetry()
    
    return {
        "health": health_data,
        "positions": pos_data,
        "holdings": holdings_data,
        "margin": margin_data,
        "recent_signals": recent_signals,
        "broker_orders": broker_orders,
        "broker_trades": broker_trades,
        "supertrend": supertrend_telemetry,
        "custom_strategies": custom_strat_telemetry,
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

    try:
        supertrend_engine.update_config(data)
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})
    return {"status": "success", "telemetry": supertrend_engine.get_telemetry()}

@app.get("/internal/supertrend/strategies")
async def get_supertrend_strategies(request: Request):
    """Returns all registered SuperTrend strategies and consolidated telemetry."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    return {
        "status": "success",
        "strategies": supertrend_engine.get_all_strategies(),
        "telemetry": supertrend_engine.get_telemetry()
    }

@app.post("/internal/supertrend/strategy/save")
async def save_supertrend_strategy(request: Request):
    """Adds or updates a symbol strategy in the multi-strategy engine."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    try:
        data = await request.json()
    except Exception:
        data = {}

    try:
        strat_tel = supertrend_engine.add_or_update_strategy(data)
        return {"status": "success", "strategy": strat_tel, "telemetry": supertrend_engine.get_telemetry()}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

@app.post("/internal/supertrend/strategy/{key}/toggle")
async def toggle_supertrend_strategy(key: str, request: Request):
    """Toggles enable/disable state for a single symbol strategy by ID or symbol."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    is_enabled = None
    try:
        body = await request.json()
        if "is_enabled" in body:
            is_enabled = bool(body["is_enabled"])
    except Exception:
        pass

    strat_tel = supertrend_engine.toggle_strategy(key, is_enabled)
    if not strat_tel:
        return JSONResponse(status_code=404, content={"status": "error", "message": f"Strategy '{key}' not found"})

    return {"status": "success", "strategy": strat_tel, "telemetry": supertrend_engine.get_telemetry()}

@app.delete("/internal/supertrend/strategy/{key}")
async def delete_supertrend_strategy(key: str, request: Request):
    """Deletes a symbol strategy from the running container by ID or symbol."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    ok = supertrend_engine.remove_strategy(key)
    if not ok:
        return JSONResponse(status_code=404, content={"status": "error", "message": f"Strategy '{key}' not found"})

    return {"status": "success", "message": f"Strategy '{key}' removed", "telemetry": supertrend_engine.get_telemetry()}

@app.get("/internal/supertrend/status")
async def get_supertrend_status(request: Request):
    """Returns current SuperTrend strategy telemetry."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    return supertrend_engine.get_telemetry()

@app.get("/internal/supertrend/candles")
async def get_supertrend_candles(
    request: Request,
    timeframe: Optional[str] = None,
    symbol: Optional[str] = None,
    strategy_id: Optional[str] = None
):
    """Returns candlestick and indicator series for TradingView Lightweight Charts."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    return await supertrend_engine.get_chart_data_async(
        xts_api,
        timeframe_override=timeframe,
        symbol_override=symbol,
        strategy_id_override=strategy_id
    )

@app.post("/internal/supertrend/evaluate-now")
async def evaluate_supertrend_now(
    request: Request,
    symbol: Optional[str] = None,
    strategy_id: Optional[str] = None,
    timeframe: Optional[str] = None
):
    """Executes on-demand diagnostic cycle evaluation and returns full formula trace."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    return await supertrend_engine.evaluate_cycle_diagnostic(
        xts_api,
        symbol_override=symbol,
        strategy_id_override=strategy_id,
        timeframe_override=timeframe
    )

@app.post("/internal/supertrend/sync-trend")
async def sync_supertrend_trend_endpoint(
    request: Request,
    strategy_id: Optional[str] = None
):
    """Executes on-demand position synchronization to active prevailing SuperTrend trend."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    target_id = strategy_id
    if not target_id:
        try:
            body = await request.json()
            if isinstance(body, dict):
                target_id = body.get("strategy_id")
        except Exception:
            pass

    target_id = target_id or (supertrend_engine.primary_runner.id if supertrend_engine.primary_runner else "")
    if not target_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No active strategy found to sync"})

    this_mod = sys.modules.get(__name__) or sys.modules.get("main") or sys.modules.get("client_main")
    return await supertrend_engine.sync_strategy_to_trend(target_id, xts_api, this_mod)

@app.post("/internal/supertrend/strategy/reset-flat")
async def reset_supertrend_strategy_flat_endpoint(
    request: Request
):
    """Resets an individual strategy target to 0 FLAT, optionally squaring off at the broker."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    try:
        body = await request.json()
    except Exception:
        body = {}

    target_id = body.get("strategy_id") or body.get("id")
    square_off_broker = bool(body.get("square_off_broker", False))

    target_id = target_id or (supertrend_engine.primary_runner.id if supertrend_engine.primary_runner else "")
    if not target_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No active strategy found to reset"})

    this_mod = sys.modules.get(__name__) or sys.modules.get("main") or sys.modules.get("client_main")
    res = await supertrend_engine.reset_strategy_to_flat(target_id, square_off_broker, xts_api, this_mod)
    return res

# =========================================================================
# Custom Python Strategy Internal Endpoints
# =========================================================================

@app.get("/internal/custom-strategies")
async def get_custom_strategies_endpoint(request: Request):
    """Returns telemetry of all registered custom Python strategies."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    return {
        "status": "success",
        "strategies": custom_strategy_engine.get_telemetry()
    }

@app.post("/internal/custom-strategies/save")
async def save_custom_strategy_endpoint(request: Request):
    """Adds or updates a custom Python strategy runner."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    try:
        data = await request.json()
    except Exception:
        data = {}

    try:
        runner = custom_strategy_engine.add_or_update_strategy(data)
        return {
            "status": "success",
            "strategy": {
                "id": runner.id,
                "strategy_id": runner.strategy_id,
                "name": runner.name,
                "symbol": runner.symbol,
                "timeframe": runner.timeframe,
                "quantity": runner.quantity,
                "is_enabled": runner.is_enabled,
                "compile_error": runner.compile_error
            },
            "telemetry": custom_strategy_engine.get_telemetry()
        }
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

@app.post("/internal/custom-strategies/{strat_id}/toggle")
async def toggle_custom_strategy_endpoint(strat_id: str, request: Request):
    """Toggles enable/disable state for a custom strategy runner."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    runner = custom_strategy_engine.get_strategy(strat_id)
    if not runner:
        return JSONResponse(status_code=404, content={"status": "error", "message": f"Strategy '{strat_id}' not found"})

    try:
        body = await request.json()
        is_enabled = bool(body.get("is_enabled", not runner.is_enabled))
    except Exception:
        is_enabled = not runner.is_enabled

    runner.update_config({"is_enabled": is_enabled})
    return {"status": "success", "strategy_id": strat_id, "is_enabled": runner.is_enabled, "telemetry": custom_strategy_engine.get_telemetry()}

@app.delete("/internal/custom-strategies/{strat_id}")
async def delete_custom_strategy_endpoint(strat_id: str, request: Request):
    """Deletes a custom strategy runner."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    custom_strategy_engine.remove_strategy(strat_id)
    return {"status": "success", "message": f"Strategy '{strat_id}' removed", "telemetry": custom_strategy_engine.get_telemetry()}

@app.post("/internal/custom-strategies/dry-run")
async def dry_run_custom_strategy_endpoint(request: Request):
    """Executes a backtest dry-run on historical candles."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    try:
        body = await request.json()
    except Exception:
        body = {}

    code_str = str(body.get("code_content", ""))
    symbol = str(body.get("symbol", "GOLDPETAL1!")).strip().upper()
    timeframe = str(body.get("timeframe", "15m")).strip().lower()
    params = body.get("params", {})

    # Fetch real historical candles
    tf_seconds = parse_timeframe_to_seconds(timeframe) if "parse_timeframe_to_seconds" in globals() else 900
    try:
        resolved = xts_api.resolve_contract(symbol)
        inst_id = resolved.get("inst_id") if resolved else None
        exch_seg = resolved.get("exch_seg", "MCXFO") if resolved else "MCXFO"
        if not inst_id:
            return {"status": "error", "message": f"Symbol '{symbol}' not found in contract master."}
        candles = xts_api.fetch_ohlc_candles(exch_seg, inst_id, tf_seconds, 150)
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch market data: {e}"}

    result = MultiCustomStrategyEngine.evaluate_dry_run(code_str, candles, params=params)
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

@app.get("/internal/validate-symbol")
async def validate_symbol_endpoint(request: Request, symbol: str = ""):
    """Validates and resolves a trading symbol or TradingView continuous contract against contract master."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    sym = symbol.strip()
    if not sym:
        return {"valid": False, "error": "Symbol cannot be empty"}

    contract = xts_api.resolve_contract(sym)
    if not contract or not contract.get("inst_id"):
        return {"valid": False, "symbol": sym, "error": f"Symbol '{sym}' not found in contract master file"}

    return {
        "valid": True,
        "symbol": sym,
        "inst_id": contract.get("inst_id"),
        "exch_seg": contract.get("exch_seg"),
        "prod_type": contract.get("prod_type"),
        "lot_size": contract.get("lot_size"),
        "tick_size": contract.get("tick_size"),
        "freeze_qty": contract.get("freeze_qty"),
        "expiry": contract.get("expiry_str"),
        "days_to_expiry": contract.get("days_to_expiry"),
        "desc": contract.get("desc"),
        "name": contract.get("name")
    }

@app.get("/internal/market-readiness")
async def get_market_readiness(request: Request, symbol: str = ""):
    """Executes live 4-point market readiness diagnostics."""
    internal_auth_token = str(getattr(config, "INTERNAL_AUTH_TOKEN", "")).strip()
    if internal_auth_token:
        req_token = request.headers.get("X-Internal-Token", "").strip()
        if not hmac.compare_digest(req_token, internal_auth_token):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Forbidden"})

    sym = symbol.strip() or supertrend_engine.symbol
    return xts_api.check_live_market_readiness(sym)

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

    status = "done" if (isinstance(result, dict) and result.get("status") == "success") else "partial_failure" if (isinstance(result, dict) and result.get("status") == "partial_failure") else "failed"
    db_update_status(sig_id, status, result, payload=panic_payload)

    # Finding #7 Fix: Reset virtual positions in memory and SQLite on Panic Square-off
    if supertrend_engine:
        for r in getattr(supertrend_engine, "strategies", {}).values():
            r.virtual_position = 0
            db_set_virtual_position(r.strategy_key, r.symbol, r.timeframe, 0)
    if custom_strategy_engine:
        for r in getattr(custom_strategy_engine, "strategies", {}).values():
            if hasattr(r, "virtual_position"):
                r.virtual_position = 0

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
