import os
import asyncio
import logging
import datetime
import time
from typing import Optional
from contextlib import closing
import database
import docker_manager
import telemetry_service
import httpx

logger = logging.getLogger(__name__)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def is_market_open_ist(exch_seg: str = "MCXFO", now_dt: Optional[datetime.datetime] = None, force_check: bool = False) -> bool:
    """
    Evaluates whether the Indian exchange segment is open for live trading.
    - Monday to Friday only (weekday 0-4). Saturday (5) & Sunday (6) are strictly closed.
    - MCX: 09:00:00 to 23:55:00 IST
    - NSE/BSE: 09:15:00 to 15:30:00 IST
    """
    if not force_check and "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("ENFORCE_MARKET_HOURS_IN_TESTS", "").lower() not in ("true", "1", "yes"):
        return True

    dt = now_dt if now_dt is not None else datetime.datetime.now(IST)
    if dt.weekday() >= 5:
        return False
    cur_hms = (dt.hour, dt.minute, dt.second)
    seg_upper = str(exch_seg or "").upper()
    if "MCX" in seg_upper or "COMMODITY" in seg_upper:
        return (9, 0, 0) <= cur_hms <= (23, 55, 0)
    elif any(eq in seg_upper for eq in ("NSE", "BSE", "CM", "CASH")):
        return (9, 15, 0) <= cur_hms <= (15, 30, 0)
    else:
        return (9, 0, 0) <= cur_hms <= (23, 55, 0)


async def run_rolling_cache_warmup(batch_size: int = None, delay_between_batches_sec=3.0):
    """
    Executes a staged rolling restart across all active client containers
    to warm up the Symphony XTS instrument master caches at 08:30 IST without API rate-limit bans.
    """
    if batch_size is None:
        cpu_n = os.cpu_count() or 1
        batch_size = 2 if cpu_n <= 1 else 4

    logger.info(f"🌅 [08:30 IST WARMUP] Initiating Staged Rolling Warmup (Batch Size: {batch_size}, CPU: {os.cpu_count() or 1})...")
    start_time = time.time()

    with closing(database.get_db_connection()) as conn:
        active_tenants = [dict(r) for r in conn.execute("SELECT id, name FROM tenants WHERE status='ACTIVE'").fetchall()]

    total = len(active_tenants)
    if total == 0:
        logger.info("[08:30 IST WARMUP] No active tenants found to warm up.")
        return {"status": "success", "warmed_up": 0, "failures": []}

    logger.info(f"[08:30 IST WARMUP] Found {total} active clients. Processing in batches of {batch_size}...")

    failures = []
    warmed_count = 0

    for i in range(0, total, batch_size):
        batch = active_tenants[i:i + batch_size]
        batch_names = [t["id"] for t in batch]
        logger.info(f"[08:30 IST WARMUP] Restarting Batch {i//batch_size + 1}: {batch_names}")

        # Restart batch in parallel
        for t in batch:
            res = docker_manager.restart_client_container(t["id"])
            if res.get("status") == "error":
                failures.append({"tenant_id": t["id"], "error": res.get("message")})
            else:
                warmed_count += 1

        # Pause before healthcheck and next batch
        await asyncio.sleep(delay_between_batches_sec)

    elapsed = round(time.time() - start_time, 2)
    logger.info(f"✅ [08:30 IST WARMUP] Staged warmup completed in {elapsed}s. Warmed: {warmed_count} | Failures: {len(failures)}")
    
    database.record_audit(
        "SCHEDULER_WARMUP",
        "ROLLING_CACHE_WARMUP",
        {"total": total, "warmed": warmed_count, "failures": failures, "elapsed_seconds": elapsed}
    )
    return {"status": "success", "warmed_up": warmed_count, "failures": failures, "elapsed_seconds": elapsed}

async def check_drawdown_circuit_breakers(enforce_market_hours: bool = True):
    """
    Evaluates real-time Net MTM against each tenant's max_daily_loss_inr.
    If loss limit is breached during active market hours, triggers emergency square-off and sets tenant status to PAUSED.
    Outside market hours (weekends, nights), circuit breaker evaluation is strictly suppressed.
    """
    if enforce_market_hours and not is_market_open_ist():
        logger.debug("Market is closed (IST). Skipping drawdown circuit breaker evaluation.")
        return

    with closing(database.get_db_connection()) as conn:
        active_tenants = [dict(r) for r in conn.execute("SELECT id, name FROM tenants WHERE status='ACTIVE'").fetchall()]
        risk_limits = {r["tenant_id"]: dict(r) for r in conn.execute("SELECT * FROM tenant_risk_limits").fetchall()}
        credentials = {r["tenant_id"]: dict(r) for r in conn.execute("SELECT * FROM tenant_credentials").fetchall()}

    for t in active_tenants:
        t_id = t["id"]
        r_lim = risk_limits.get(t_id, {})
        max_loss = float(r_lim.get("max_daily_loss_inr") or 50000.0)
        
        # Query telemetry for net_mtm
        tel = await telemetry_service.get_single_client_telemetry(t_id)
        net_mtm = float(tel.get("net_mtm") or 0.0)
        
        # If net_mtm is negative and breaches max_loss
        if net_mtm <= -abs(max_loss) and max_loss > 0:
            logger.critical(
                f"🚨 [CIRCUIT BREAKER TRIGGERED] Tenant {t_id} ({t['name']}) breached max daily loss limit: "
                f"Net MTM = -₹{abs(net_mtm):,.2f} <= -₹{max_loss:,.2f}. Initiating emergency panic & auto-pause."
            )
            
            c_row = credentials.get(t_id)
            secret = ""
            if c_row:
                try:
                    import security
                    dec = security.decrypt_credentials(c_row["encrypted_payload"])
                    secret = dec.get("WEBHOOK_SECRET", "")
                except Exception:
                    pass

            # 1. Trigger panic square off on client
            panic_res = await telemetry_service.panic_single_client(t_id, secret)
            
            # 2. Pause client in database
            with closing(database.get_db_connection()) as conn:
                with conn:
                    conn.execute("UPDATE tenants SET status='PAUSED', updated_at=? WHERE id=?", (time.time(), t_id))
            
            # 3. Reload caddy ingress
            import caddy_manager
            caddy_manager.sync_caddy_config()

            # 4. Record critical audit log
            database.record_audit(
                "CIRCUIT_BREAKER",
                "AUTO_KILL_SWITCH_TRIGGERED",
                {
                    "tenant_id": t_id,
                    "net_mtm": net_mtm,
                    "max_daily_loss_inr": max_loss,
                    "panic_result": panic_res
                },
                target_tenant_id=t_id
            )

async def run_market_open_fleet_alignment():
    """
    Executes automated fleet alignment across all active clients at 09:00:05 IST market open.
    Synchronizes any client whose position is FLAT or misaligned to the prevailing market trend.
    """
    logger.info("🔔 [09:00 IST MARKET OPEN] Executing automated fleet alignment across all active clients...")
    start_time = time.time()

    with closing(database.get_db_connection()) as conn:
        active_tenants = [dict(r) for r in conn.execute("SELECT id, name FROM tenants WHERE status='ACTIVE'").fetchall()]

    if not active_tenants:
        logger.info("[09:00 IST MARKET OPEN] No active tenants to align.")
        return {"status": "success", "synced": 0, "already_aligned": 0, "failures": []}

    aligned_count = 0
    synced_count = 0
    failures = []

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        for t in active_tenants:
            t_id = t["id"]
            try:
                tel = await telemetry_service.get_single_client_telemetry(t_id)
                st_data = tel.get("supertrend") or {}
                strategies = st_data.get("strategies") or []

                port = docker_manager.get_tenant_port(t_id)
                headers = {}
                internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
                if internal_token:
                    headers["X-Internal-Token"] = internal_token

                for s in strategies:
                    strat_id = s.get("id")
                    sym = s.get("symbol")
                    tf = s.get("timeframe")
                    qty = s.get("quantity", 0)
                    trend = s.get("current_trend", "INITIALIZING")
                    vpos = s.get("virtual_position", 0)
                    bpos = s.get("current_broker_quantity", 0)
                    target_pos = -qty if trend == "BEARISH" else (qty if trend == "BULLISH" else 0)

                    is_aligned = (vpos == target_pos) and (bpos == target_pos)
                    if is_aligned:
                        aligned_count += 1
                        continue

                    logger.warning(
                        f"⚠️ [09:00 IST MARKET OPEN] Aligning {t_id.upper()} [{sym} ({tf})]: "
                        f"Trend={trend} | Current={vpos:+d} lots -> Target={target_pos:+d} lots"
                    )

                    sync_urls = [
                        f"http://xts_client_{t_id}:8000/internal/supertrend/sync-trend?strategy_id={strat_id}",
                        f"http://127.0.0.1:{port}/internal/supertrend/sync-trend?strategy_id={strat_id}",
                        f"{telemetry_service.CADDY_PROXY_BASE}/{t_id}/internal/supertrend/sync-trend?strategy_id={strat_id}"
                    ]
                    synced_ok = False
                    sync_err_desc = "All sync URLs failed"
                    for s_url in sync_urls:
                        try:
                            resp = await http_client.post(s_url, headers=headers, json={"strategy_id": strat_id})
                            if resp.status_code == 200:
                                res_json = resp.json()
                                res_status = res_json.get("status") if isinstance(res_json, dict) else "UNKNOWN"
                                if res_status in ("SUCCESS", "ALREADY_SYNCED"):
                                    logger.info(f"✅ [09:00 IST MARKET OPEN] Sync result for {t_id.upper()} ({strat_id}): {res_json}")
                                    synced_count += 1
                                    synced_ok = True
                                    break
                                else:
                                    sync_err_desc = res_json.get("error") or res_json.get("message") or f"Invalid status {res_status}"
                                    logger.error(f"❌ [09:00 IST MARKET OPEN] Sync error for {t_id.upper()} ({strat_id}): {res_json}")
                                    break
                        except Exception as err:
                            logger.debug(f"Sync attempt on {s_url} failed: {err}")

                    if not synced_ok:
                        failures.append({"tenant_id": t_id, "strategy_id": strat_id, "symbol": sym, "error": sync_err_desc})
            except Exception as e:
                logger.error(f"[09:00 IST MARKET OPEN] Error checking {t_id}: {e}")
                failures.append({"tenant_id": t_id, "error": str(e)})

    elapsed = round(time.time() - start_time, 2)
    logger.info(f"🏁 [09:00 IST MARKET OPEN] Fleet alignment completed in {elapsed}s. Synced: {synced_count} | Already Aligned: {aligned_count} | Failures: {len(failures)}")

    database.record_audit(
        "MARKET_OPEN_ALIGNMENT",
        "AUTO_FLEET_SYNC",
        {"synced": synced_count, "already_aligned": aligned_count, "failures": failures, "elapsed_seconds": elapsed}
    )
    return {"status": "success", "synced": synced_count, "already_aligned": aligned_count, "failures": failures}

async def start_scheduler_loop(poll_interval_sec=5):
    """Background scheduler loop checking time for 08:30 IST daily trigger, 09:00 IST market open alignment, and drawdown breakers."""
    while True:
        try:
            # 1. Check Drawdown Circuit Breakers across active clients
            await check_drawdown_circuit_breakers()

            now_ist = datetime.datetime.now(IST)

            # 2. Check 08:30 IST cache warmup on trading days only (Mon-Fri)
            if now_ist.weekday() < 5 and now_ist.hour == 8 and now_ist.minute == 30 and now_ist.second < 15:
                await run_rolling_cache_warmup()
                await asyncio.sleep(30)

            # 3. Check 09:00:05 IST Market Open Fleet Alignment on trading days only (Mon-Fri)
            if now_ist.weekday() < 5 and now_ist.hour == 9 and now_ist.minute == 0 and 5 <= now_ist.second <= 35:
                await run_market_open_fleet_alignment()
                await asyncio.sleep(40)

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        await asyncio.sleep(poll_interval_sec)
