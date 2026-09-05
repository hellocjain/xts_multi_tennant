#!/usr/bin/env python3
import sys
sys.path.insert(0, "/app")
import time, os, logging
import xts_api, config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/app/data/auto_cancel.log")
    ]
)
logger = logging.getLogger("auto_cancel")

OPEN_STATUSES = (
    "OPEN", "NEW", "PENDING", "PARTIALLYFILLED", 
    "PENDINGNEW", "REPLACED", "TRIGGERPENDING", "TRIGGER_PENDING"
)

def get_order_status(o):
    return str(o.get("OrderStatus") or o.get("status") or "").strip().upper()

def get_order_id(o):
    return str(o.get("AppOrderID") or o.get("app_order_id") or o.get("OrderID") or "").strip()

def get_order_symbol(o):
    return str(o.get("TradingSymbol") or o.get("symbol") or "").strip()

def is_order_open(o):
    return get_order_status(o) in OPEN_STATUSES

def perform_cancel():
    client_id = getattr(config, "CLIENT_ID", "").strip()
    logger.info(f"Initiating cancellation routine for client {client_id}...")
    token = xts_api.get_interactive_token(force_refresh=True)
    if not token:
        logger.error(f"Failed to acquire interactive token for {client_id} (broker offline or maintenance).")
        return False, "NO_TOKEN"

    headers = {"authorization": token, "Content-Type": "application/json"}
    safe_url = config.XTS_API_BASE_URL.rstrip("/")

    # 1. Fetch current broker orders directly
    try:
        resp = xts_api.api_session.get(f"{safe_url}/orders", headers=headers, timeout=5)
        if resp.status_code != 200:
            logger.error(f"Fetch orders failed for {client_id}: HTTP {resp.status_code} - {resp.text}")
            return False, f"HTTP_{resp.status_code}"
        res_data = resp.json()
        orders = res_data.get("result", []) or []
    except Exception as e:
        logger.error(f"Exception fetching orders for {client_id}: {e}")
        return False, f"EXCEPTION_{e}"

    logger.info(f"Retrieved {len(orders)} total orders from broker for {client_id}.")

    pending_orders = [o for o in orders if is_order_open(o)]
    logger.info(f"Found {len(pending_orders)} pending/open orders to cancel for {client_id}.")

    if len(pending_orders) > 0:
        # 2. Targeted cancellation of each open order
        for o in pending_orders:
            oid = get_order_id(o)
            sym = get_order_symbol(o)
            st = get_order_status(o)
            if not oid:
                continue
            logger.info(f"Cancelling order #{oid} ({sym}, status: {st}) for {client_id}...")
            c_url = f"{safe_url}/orders?appOrderID={oid}&clientID={client_id}"
            try:
                res = xts_api.api_session.delete(c_url, json={"appOrderID": oid, "clientID": client_id}, headers=headers, timeout=5)
                logger.info(f"Cancel response for #{oid}: {res.status_code} - {res.text}")
            except Exception as e:
                logger.error(f"Error cancelling #{oid}: {e}")

        # 3. Redundant Atomic Cancel-All Sweep
        try:
            ca_url = f"{safe_url}/orders/cancelall"
            ca_res = xts_api.api_session.post(ca_url, json={"clientID": client_id}, headers=headers, timeout=5)
            logger.info(f"Atomic cancel-all response for {client_id}: {ca_res.status_code} - {ca_res.text}")
        except Exception as cae:
            logger.error(f"Atomic cancel-all error for {client_id}: {cae}")

    # 4. Final verification audit
    try:
        v_resp = xts_api.api_session.get(f"{safe_url}/orders", headers=headers, timeout=5)
        if v_resp.status_code != 200:
            logger.error(f"Verification fetch failed for {client_id}: HTTP {v_resp.status_code}")
            return False, f"VERIFY_HTTP_{v_resp.status_code}"
        final_orders = v_resp.json().get("result", []) or []
    except Exception as e:
        logger.error(f"Verification exception for {client_id}: {e}")
        return False, f"VERIFY_EXCEPTION_{e}"

    still_open = [fo for fo in final_orders if is_order_open(fo)]
    if len(still_open) == 0:
        logger.info(f"✅ SUCCESS: All pending orders for client {client_id} are 100% CANCELLED/RESOLVED!")
        return True, "ALL_RESOLVED"
    else:
        logger.warning(f"⚠️ {len(still_open)} orders still pending for client {client_id}.")
        return False, f"{len(still_open)}_STILL_OPEN"

if __name__ == "__main__":
    is_loop = "--loop" in sys.argv
    if is_loop:
        attempt = 0
        while True:
            attempt += 1
            logger.info(f"--- Attempt #{attempt} for client {getattr(config, 'CLIENT_ID', '')} ---")
            success, msg = perform_cancel()
            if success:
                break
            time.sleep(60)
    else:
        success, msg = perform_cancel()
        sys.exit(0 if success else 1)
