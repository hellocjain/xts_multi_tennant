import asyncio
import time
import json
import uuid
import logging
import os
from typing import Dict, Any, List, Optional, AsyncGenerator
from contextlib import closing
import httpx

import database
import docker_manager
import telemetry_service

logger = logging.getLogger("bulk_operations")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

ACTIVE_BULK_JOBS: Dict[str, Dict[str, Any]] = {}

def get_tenant_name(tenant_id: str) -> str:
    try:
        with closing(database.get_db_connection()) as conn:
            row = conn.execute("SELECT name FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if row and row["name"]:
                return row["name"]
    except Exception:
        pass
    return tenant_id.upper()

async def dispatch_client_internal_post(
    tenant_id: str,
    endpoint: str,
    json_payload: Optional[dict] = None,
    timeout_sec: float = 8.0
) -> dict:
    """
    Sends an internal POST request to a client container via multi-tier fallback:
    Local port -> Docker DNS -> Caddy proxy.
    """
    port = docker_manager.get_tenant_port(tenant_id)
    caddy_base = getattr(telemetry_service, "CADDY_PROXY_BASE", "http://caddy/internal-client-proxy")
    
    clean_ep = endpoint.lstrip("/")
    url_local = f"http://127.0.0.1:{port}/{clean_ep}"
    url_docker = f"http://xts_client_{tenant_id}:8000/{clean_ep}"
    url_caddy = f"{caddy_base}/{tenant_id}/{clean_ep}"

    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        for target_url in [url_local, url_docker, url_caddy]:
            try:
                resp = await client.post(target_url, headers=headers, json=json_payload or {})
                if resp.status_code in (200, 201, 202):
                    try:
                        return resp.json()
                    except Exception:
                        return {"status": "ok", "raw": resp.text}
                elif resp.status_code in (400, 403, 404, 500):
                    try:
                        err_data = resp.json()
                        return {"status": "error", "code": resp.status_code, "error": err_data.get("message") or err_data.get("error") or resp.text}
                    except Exception:
                        return {"status": "error", "code": resp.status_code, "error": resp.text}
            except Exception:
                continue

    return {"status": "error", "code": 503, "error": "Client container unreachable"}


async def _execute_single_pause(tenant_id: str) -> dict:
    t0 = time.time()
    name = get_tenant_name(tenant_id)
    
    # 1. Update SQLite DB state
    try:
        with closing(database.get_db_connection()) as conn:
            with conn:
                conn.execute("UPDATE tenants SET status='PAUSED', updated_at=? WHERE id=?", (time.time(), tenant_id))
                conn.execute("UPDATE tenant_supertrend_strategies SET is_enabled=0, updated_at=? WHERE tenant_id=?", (time.time(), tenant_id))
    except Exception as e:
        logger.warning(f"Failed to update DB for {tenant_id}: {e}")

    # 2. Dispatch to container in-memory runner
    resp = await dispatch_client_internal_post(tenant_id, "/internal/strategies/toggle-all", {"is_enabled": False}, timeout_sec=5.0)
    latency_ms = int((time.time() - t0) * 1000)

    if resp.get("status") == "ok":
        return {
            "tenant_id": tenant_id,
            "name": name,
            "status": "PAUSED",
            "message": f"Trading paused ({resp.get('strategies_updated', 0)} strategies frozen)",
            "latency_ms": latency_ms,
            "is_error": False
        }
    else:
        return {
            "tenant_id": tenant_id,
            "name": name,
            "status": "WARNING",
            "message": f"DB updated to PAUSED (Container: {resp.get('error', 'Unreachable')})",
            "latency_ms": latency_ms,
            "is_error": False
        }


async def _execute_single_resume_and_sync(tenant_id: str, semaphore: asyncio.Semaphore) -> dict:
    t0 = time.time()
    name = get_tenant_name(tenant_id)

    # 1. Update DB to active
    try:
        with closing(database.get_db_connection()) as conn:
            with conn:
                conn.execute("UPDATE tenants SET status='ACTIVE', updated_at=? WHERE id=?", (time.time(), tenant_id))
                conn.execute("UPDATE tenant_supertrend_strategies SET is_enabled=1, updated_at=? WHERE tenant_id=?", (time.time(), tenant_id))
    except Exception as e:
        logger.warning(f"Failed to activate DB for {tenant_id}: {e}")

    # 2. Unfreeze runner in memory
    await dispatch_client_internal_post(tenant_id, "/internal/strategies/toggle-all", {"is_enabled": True}, timeout_sec=4.0)

    # 3. Synchronize trend under rate-limited concurrency semaphore
    async with semaphore:
        await asyncio.sleep(0.1)  # 100ms spacing to protect broker API gateway
        sync_resp = await dispatch_client_internal_post(tenant_id, "/internal/supertrend/sync-trend", timeout_sec=10.0)

    latency_ms = int((time.time() - t0) * 1000)

    status_code = sync_resp.get("status", "ERROR")
    msg = sync_resp.get("message") or sync_resp.get("description") or sync_resp.get("error") or ""

    if status_code in ("SUCCESS", "SYNCED", "OK"):
        return {
            "tenant_id": tenant_id,
            "name": name,
            "status": "SYNCED",
            "message": f"Resumed & Synced to trend ({msg or 'Positions aligned'})",
            "latency_ms": latency_ms,
            "is_error": False
        }
    elif status_code in ("ALREADY_SYNCED", "NO_CHANGE"):
        return {
            "tenant_id": tenant_id,
            "name": name,
            "status": "ALREADY_SYNCED",
            "message": f"Resumed — Already aligned with trend ({msg or 'No change needed'})",
            "latency_ms": latency_ms,
            "is_error": False
        }
    elif "Margin" in msg or "shortfall" in msg.lower() or status_code == "MARGIN_SHORTFALL":
        return {
            "tenant_id": tenant_id,
            "name": name,
            "status": "RMS_SHORTFALL",
            "message": f"Resumed — Broker RMS Hold / Margin Shortfall: {msg}",
            "latency_ms": latency_ms,
            "is_error": True
        }
    else:
        return {
            "tenant_id": tenant_id,
            "name": name,
            "status": "ERROR",
            "message": f"Resumed — Trend sync error: {msg or 'Gateway timeout'}",
            "latency_ms": latency_ms,
            "is_error": True
        }


async def _run_bulk_job(job_id: str, action: str, tenant_ids: List[str], username: str):
    job = ACTIVE_BULK_JOBS.get(job_id)
    if not job:
        return

    q: asyncio.Queue = job["event_queue"]
    results = []
    total = len(tenant_ids)

    # Initial event
    await q.put({
        "type": "INIT",
        "job_id": job_id,
        "action": action,
        "total": total,
        "tenant_ids": tenant_ids
    })

    if action == "pause":
        # Parallel execution across all tenants
        tasks = [_execute_single_pause(t_id) for t_id in tenant_ids]
        for idx, task in enumerate(asyncio.as_completed(tasks), 1):
            res = await task
            results.append(res)
            job["completed"] = idx
            job["results"] = results
            await q.put({
                "type": "PROGRESS",
                "job_id": job_id,
                "current": idx,
                "total": total,
                "pct": int((idx / total) * 100),
                "client_result": res
            })

        database.record_audit(username, "BULK_PAUSE_TRADING", {"total": total, "tenants": tenant_ids})

    elif action == "resume_and_sync":
        # Rate-limited worker pool with max 3 concurrent broker requests and 100ms stagger
        semaphore = asyncio.Semaphore(3)
        tasks = [_execute_single_resume_and_sync(t_id, semaphore) for t_id in tenant_ids]
        
        for idx, task in enumerate(asyncio.as_completed(tasks), 1):
            res = await task
            results.append(res)
            job["completed"] = idx
            job["results"] = results
            await q.put({
                "type": "PROGRESS",
                "job_id": job_id,
                "current": idx,
                "total": total,
                "pct": int((idx / total) * 100),
                "client_result": res
            })

        database.record_audit(username, "BULK_RESUME_AND_SYNC_TREND", {"total": total, "tenants": tenant_ids})

    job["status"] = "completed"
    job["completed_at"] = time.time()

    # Final summary event
    await q.put({
        "type": "COMPLETE",
        "job_id": job_id,
        "action": action,
        "total": total,
        "success_count": sum(1 for r in results if not r.get("is_error")),
        "warning_count": sum(1 for r in results if r.get("status") == "WARNING"),
        "error_count": sum(1 for r in results if r.get("is_error")),
        "results": results
    })


def start_bulk_operation(action: str, tenant_ids: List[str], username: str) -> str:
    """Initiates a background bulk operation and returns its job_id."""
    clean_tenants = [str(t).strip().lower() for t in tenant_ids if str(t).strip()]
    if not clean_tenants:
        raise ValueError("tenant_ids cannot be empty")

    job_id = str(uuid.uuid4())
    ACTIVE_BULK_JOBS[job_id] = {
        "job_id": job_id,
        "action": action,
        "tenant_ids": clean_tenants,
        "total": len(clean_tenants),
        "completed": 0,
        "status": "running",
        "created_at": time.time(),
        "event_queue": asyncio.Queue(),
        "results": []
    }

    asyncio.create_task(_run_bulk_job(job_id, action, clean_tenants, username))
    return job_id


async def stream_bulk_job_events(job_id: str) -> AsyncGenerator[str, None]:
    """Generates SSE strings for real-time frontend streaming."""
    job = ACTIVE_BULK_JOBS.get(job_id)
    if not job:
        yield f"data: {json.dumps({'type': 'ERROR', 'error': 'Job not found'})}\n\n"
        return

    q: asyncio.Queue = job["event_queue"]
    
    # If job already completed before client connected, yield final state
    if job.get("status") == "completed":
        yield f"data: {json.dumps({'type': 'COMPLETE', 'job_id': job_id, 'results': job.get('results', [])})}\n\n"
        return

    timeout_counter = 0
    while True:
        try:
            event = await asyncio.wait_for(q.get(), timeout=15.0)
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "COMPLETE":
                break
        except asyncio.TimeoutError:
            timeout_counter += 1
            if timeout_counter > 4:  # 60s total timeout
                yield f"data: {json.dumps({'type': 'TIMEOUT', 'job_id': job_id})}\n\n"
                break
            yield f": keep-alive\n\n"
