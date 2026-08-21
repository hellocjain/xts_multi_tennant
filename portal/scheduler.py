import asyncio
import logging
import datetime
import time
from contextlib import closing
import database
import docker_manager
import telemetry_service

logger = logging.getLogger(__name__)

async def run_rolling_cache_warmup(batch_size=4, delay_between_batches_sec=5.0):
    """
    Executes a staged rolling restart across all active client containers
    to warm up the Symphony XTS instrument master caches at 08:30 IST without API rate-limit bans.
    """
    logger.info("🌅 [08:30 IST WARMUP] Initiating Staged Rolling Cache Warmup Engine...")
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

async def start_scheduler_loop():
    """Background scheduler loop checking time for 08:30 IST daily trigger."""
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    while True:
        try:
            now_ist = datetime.datetime.now(IST)
            # Check if 08:30:00 - 08:30:10 IST
            if now_ist.hour == 8 and now_ist.minute == 30 and now_ist.second < 15:
                await run_rolling_cache_warmup()
                # Sleep 60 seconds to avoid duplicate trigger in the same minute
                await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        await asyncio.sleep(10)
