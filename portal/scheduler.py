import os
import asyncio
import logging
import datetime
import time
from contextlib import closing
import database
import docker_manager
import telemetry_service

logger = logging.getLogger(__name__)

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

async def check_drawdown_circuit_breakers():
    """
    Evaluates real-time Net MTM against each tenant's max_daily_loss_inr.
    If loss limit is breached, triggers emergency square-off and sets tenant status to PAUSED.
    """
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

async def start_scheduler_loop(poll_interval_sec=5):
    """Background scheduler loop checking time for 08:30 IST daily trigger and drawdown breakers."""
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    while True:
        try:
            # 1. Check Drawdown Circuit Breakers across active clients
            await check_drawdown_circuit_breakers()

            # 2. Check 08:30 IST master contract refresh & cache warmup
            now_ist = datetime.datetime.now(IST)
            if now_ist.hour == 8 and now_ist.minute == 30 and now_ist.second < 15:
                try:
                    import master_contract_service
                    logger.info("🌅 [08:30 IST] Downloading and compiling daily Symphony XTS master contracts...")
                    res = await asyncio.to_thread(master_contract_service.download_and_refresh_master_contracts)
                    logger.info(f"🌅 [08:30 IST] Master contract refresh result: {res}")
                except Exception as mc_err:
                    logger.error(f"Failed to refresh master contracts at 08:30 IST: {mc_err}")

                await run_rolling_cache_warmup()
                await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        await asyncio.sleep(poll_interval_sec)
