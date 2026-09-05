#!/usr/bin/env python3
"""
Master Auto-Cancel Daemon for XTS Multi-Tenant Platform
Continuously probes the broker gateway. As soon as weekend maintenance ends,
it runs the auto_cancel routine inside each client container, cancels all pending
orders, verifies 0 open orders, and exits cleanly.
"""
import subprocess
import time
import sys
import os
import json
import logging
from datetime import datetime

LOG_FILE = "/opt/xts_multi/master_auto_cancel.log"
STATUS_FILE = "/opt/xts_multi/auto_cancel_status.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger("master_auto_cancel")

CLIENTS = ["abk01", "abk02", "abk03", "abk04", "abk05", "abk06", "abk09", "abk10", "abk11"]

def probe_broker_gateway():
    """Test if broker gateway has ended maintenance and is accepting logins."""
    probe_cmd = [
        "docker", "exec", "xts_client_abk01",
        "python3", "-c",
        "import xts_api; tok = xts_api.get_interactive_token(force_refresh=True); exit(0 if tok else 1)"
    ]
    try:
        proc = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
        return proc.returncode == 0
    except Exception as e:
        logger.debug(f"Broker probe exception: {e}")
        return False

def run_client_cancel(client_name):
    """Executes the auto_cancel.py inside the client's container."""
    container = f"xts_client_{client_name}"
    cmd = ["docker", "exec", container, "python3", "/app/data/auto_cancel.py"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT_EXPIRED"
    except Exception as e:
        return False, str(e)

def update_status_file(status_data):
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status_data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write status file: {e}")

def main():
    logger.info("=" * 60)
    logger.info("🚀 Starting Master Auto-Cancel Daemon")
    logger.info(f"Target clients ({len(CLIENTS)}): {', '.join(CLIENTS)}")
    logger.info(f"Logging to: {LOG_FILE}")
    logger.info("=" * 60)

    pending_clients = list(CLIENTS)
    status_data = {
        "status": "RUNNING",
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "pending_clients": pending_clients,
        "completed_clients": [],
        "client_results": {}
    }
    update_status_file(status_data)

    attempt = 0
    while pending_clients:
        attempt += 1
        logger.info(f"--- Probe #{attempt} | Waiting for broker gateway (Pending: {len(pending_clients)} clients) ---")
        
        is_online = probe_broker_gateway()
        if not is_online:
            logger.info("Broker API gateway still in weekend maintenance. Sleeping 60s...")
            status_data["updated_at"] = datetime.now().isoformat()
            update_status_file(status_data)
            time.sleep(60)
            continue

        logger.info("🟢 Broker API gateway is ONLINE! Initiating cancellation sweep...")
        for c in list(pending_clients):
            logger.info(f"Executing cancellation sweep for client {c}...")
            ok, out = run_client_cancel(c)
            logger.info(f"Result for {c} (Success={ok}):\n{out}")
            if ok:
                pending_clients.remove(c)
                status_data["completed_clients"].append(c)
                status_data["client_results"][c] = {
                    "status": "SUCCESS",
                    "resolved_at": datetime.now().isoformat(),
                    "output": out
                }
            else:
                status_data["client_results"][c] = {
                    "status": "FAILED_WILL_RETRY",
                    "attempt_at": datetime.now().isoformat(),
                    "output": out
                }
            status_data["pending_clients"] = pending_clients
            status_data["updated_at"] = datetime.now().isoformat()
            update_status_file(status_data)
            time.sleep(2)  # 2s rate limit cushion between clients

        if pending_clients:
            logger.warning(f"⚠️ {len(pending_clients)} clients still pending ({', '.join(pending_clients)}). Retrying in 30s...")
            time.sleep(30)

    logger.info("🎉" * 20)
    logger.info("🎉 ALL CLIENT ORDERS HAVE BEEN SUCCESSFULLY CANCELLED AND VERIFIED!")
    logger.info("🎉 Zero open/pending orders remain across all client accounts.")
    logger.info("🎉" * 20)
    status_data["status"] = "COMPLETED"
    status_data["completed_at"] = datetime.now().isoformat()
    status_data["updated_at"] = datetime.now().isoformat()
    update_status_file(status_data)

if __name__ == "__main__":
    main()
