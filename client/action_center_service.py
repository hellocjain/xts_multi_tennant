"""
client/action_center_service.py - Action Center & Semi-Auto Human-in-the-Loop Queue.
Implements 1:1 OpenAlgo Action Center Parity:
1. Pending order queueing when API key is set to 'semi_auto'.
2. Human-in-the-loop 1-Click Order Approval and Rejection.
3. Bulk "Approve All" execution.
4. Real-time stats, IST timestamps, and navbar pending count.
"""

import os
import time
import json
import sqlite3
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

import config
import xts_api
import order_services
import notification_service

logger = logging.getLogger("action_center_service")

ACTION_CENTER_DB_FILE = os.path.join(getattr(config, "DATA_DIR", "/app/data"), "action_center.db")

# In-memory runtime order mode cache: tenant_id -> "auto" | "semi_auto"
_ORDER_MODES: Dict[str, str] = {}


def get_ist_timestamp() -> str:
    """Returns current timestamp formatted in IST."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def get_db_connection():
    os.makedirs(os.path.dirname(os.path.abspath(ACTION_CENTER_DB_FILE)), exist_ok=True)
    conn = sqlite3.connect(ACTION_CENTER_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_action_center_db():
    """Initializes Action Center pending_orders table."""
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                api_type TEXT NOT NULL,
                order_data TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at REAL NOT NULL,
                created_at_ist TEXT NOT NULL,
                approved_at_ist TEXT DEFAULT '',
                approved_by TEXT DEFAULT '',
                rejected_at_ist TEXT DEFAULT '',
                rejected_by TEXT DEFAULT '',
                rejected_reason TEXT DEFAULT '',
                broker_order_id TEXT DEFAULT '',
                broker_status TEXT DEFAULT ''
            )
        """)
        conn.commit()


# Initialize table on module load
init_action_center_db()


def get_order_mode(tenant_id: str = "default") -> str:
    """Returns 'auto' or 'semi_auto'."""
    tid = str(tenant_id or getattr(config, "CLIENT_ID", "") or "default").strip()
    return _ORDER_MODES.get(tid, getattr(config, "ORDER_MODE", "auto")).lower()


def set_order_mode(mode: str, tenant_id: str = "default") -> str:
    """Sets order mode to 'auto' or 'semi_auto'."""
    tid = str(tenant_id or getattr(config, "CLIENT_ID", "") or "default").strip()
    clean = "semi_auto" if str(mode).strip().lower() in ("semi_auto", "semi-auto", "semiauto") else "auto"
    _ORDER_MODES[tid] = clean
    return clean


def is_semi_auto_mode(tenant_id: str = "default") -> bool:
    """Returns True if the tenant is operating in semi-auto mode."""
    return get_order_mode(tenant_id) == "semi_auto"


def create_pending_order(
    user_id: str,
    api_type: str,
    order_data: Dict[str, Any],
    tenant_id: str = "default"
) -> int:
    """Queues an incoming order into the Action Center for human approval."""
    now_ts = time.time()
    now_ist = get_ist_timestamp()
    order_json = json.dumps(order_data)

    with get_db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO pending_orders (
                user_id, tenant_id, api_type, order_data, status,
                created_at, created_at_ist
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """, (user_id, tenant_id, api_type, order_json, now_ts, now_ist))
        conn.commit()
        order_id = cursor.lastrowid

    logger.info(f"Queued order {order_id} ({api_type}) in Action Center for tenant {tenant_id}")
    return order_id


def execute_queued_order(api_type: str, order_data: Dict[str, Any], tenant_id: str = "default") -> Tuple[bool, Dict[str, Any]]:
    """Executes an approved order through the respective service."""
    try:
        is_paper = bool(getattr(config, "PAPER_TRADE_MODE", True))

        if api_type in ("placeorder", "order"):
            action = str(order_data.get("action", "BUY")).upper()
            symbol = str(order_data.get("symbol", "")).upper()
            qty = int(order_data.get("quantity", 1))
            price = float(order_data.get("price", 0.0))
            order_ref = str(order_data.get("order_ref") or f"ac_{int(time.time()*1000)}")

            try:
                res = xts_api.place_order(
                    action=action, symbol=symbol, quantity=qty, tv_price=price, order_ref=order_ref, is_paper=is_paper
                )
            except Exception as ord_err:
                logger.error(f"Error placing approved order: {ord_err}")
                res = {"status": "error", "message": str(ord_err)}
            is_ok = (res.get("type") == "success" or res.get("status") == "success")
            order_id = (res.get("result") or {}).get("AppOrderID") or res.get("orderid") or f"ORD_{int(time.time())}"
            return is_ok, {"orderid": str(order_id), "status": "success" if is_ok else "error"}

        elif api_type == "placesmartorder":
            action = str(order_data.get("action", "BUY")).upper()
            symbol = str(order_data.get("symbol", "")).upper()
            qty = int(order_data.get("quantity", 1))
            pos_size = int(order_data.get("position_size", 0))
            price = float(order_data.get("price", 0.0))
            order_ref = str(order_data.get("order_ref") or f"smart_{int(time.time()*1000)}")

            res = order_services.execute_smart_order(
                action=action, symbol=symbol, quantity=qty, position_size=pos_size,
                price=price, order_ref=order_ref, is_paper=is_paper
            )
            is_ok = (res.get("status") == "success")
            order_id = res.get("orderid") or f"SMART_{int(time.time())}"
            return is_ok, {"orderid": str(order_id), "status": "success" if is_ok else "error"}

        elif api_type == "splitorder":
            action = str(order_data.get("action", "BUY")).upper()
            symbol = str(order_data.get("symbol", "")).upper()
            qty = int(order_data.get("quantity", 1))
            split_size = int(order_data.get("split_size", 100))
            price = float(order_data.get("price", 0.0))

            res = order_services.execute_split_order(
                action=action, symbol=symbol, total_quantity=qty, split_size=split_size,
                delay=float(order_data.get("delay", 0.05)), price=price, is_paper=is_paper
            )
            is_ok = (res.get("status") == "success")
            return is_ok, {"orderid": f"SPLIT_{int(time.time())}", "status": "success" if is_ok else "error"}

        elif api_type == "basketorder":
            orders = order_data.get("orders") or []
            res = order_services.execute_basket_order(orders, is_paper=is_paper)
            is_ok = (res.get("status") == "success")
            return is_ok, {"orderid": f"BASKET_{int(time.time())}", "status": "success" if is_ok else "error"}

        elif api_type == "optionsorder":
            import options_order_service
            res = options_order_service.execute_options_order(order_data, curr_mode="PAPER" if is_paper else "LIVE", tenant_id=tenant_id)
            is_ok = (res.get("status") == "success")
            return is_ok, {"orderid": res.get("orderid", f"OPT_{int(time.time())}"), "status": "success" if is_ok else "error"}

        # Default fallback
        return True, {"orderid": f"ORD_{int(time.time())}", "status": "success"}

    except Exception as e:
        logger.exception(f"Failed to execute queued order ({api_type}): {e}")
        return False, {"status": "error", "message": str(e)}


def approve_pending_order(order_id: int, approver: str = "trader", tenant_id: str = "default") -> Tuple[bool, Dict[str, Any]]:
    """Approves and executes a pending order from Action Center."""
    now_ist = get_ist_timestamp()

    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM pending_orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return False, {"status": "error", "message": f"Order {order_id} not found"}
        if row["status"] != "pending":
            return False, {"status": "error", "message": f"Order {order_id} is already '{row['status']}'"}

        order_data = json.loads(row["order_data"])
        api_type = row["api_type"]

        # Execute the order
        is_ok, exec_res = execute_queued_order(api_type, order_data, tenant_id=tenant_id)
        broker_order_id = str(exec_res.get("orderid", ""))
        broker_status = "FILLED" if is_ok else "REJECTED"

        conn.execute("""
            UPDATE pending_orders SET
                status='approved', approved_at_ist=?, approved_by=?,
                broker_order_id=?, broker_status=?
            WHERE id=?
        """, (now_ist, approver, broker_order_id, broker_status, order_id))
        conn.commit()

    return is_ok, {
        "status": "success" if is_ok else "warning",
        "message": "Order approved and executed successfully" if is_ok else "Order approved but broker execution failed",
        "broker_order_id": broker_order_id
    }


def reject_pending_order(order_id: int, reason: str = "Rejected by trader", rejecter: str = "trader") -> bool:
    """Rejects a pending order in Action Center."""
    now_ist = get_ist_timestamp()

    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM pending_orders WHERE id=?", (order_id,)).fetchone()
        if not row or row["status"] != "pending":
            return False

        conn.execute("""
            UPDATE pending_orders SET
                status='rejected', rejected_at_ist=?, rejected_by=?, rejected_reason=?
            WHERE id=?
        """, (now_ist, rejecter, reason, order_id))
        conn.commit()

    return True


def delete_pending_order(order_id: int) -> bool:
    """Deletes an approved or rejected order entry."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM pending_orders WHERE id=?", (order_id,))
        conn.commit()
    return True


def approve_all_pending_orders(approver: str = "trader", tenant_id: str = "default") -> Dict[str, Any]:
    """Approves and executes all currently pending orders."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT id FROM pending_orders WHERE status='pending'").fetchall()

    approved_count = 0
    errors = []

    for r in rows:
        oid = r["id"]
        ok, res = approve_pending_order(oid, approver=approver, tenant_id=tenant_id)
        if ok:
            approved_count += 1
        else:
            errors.append({"order_id": oid, "error": res.get("message")})

    return {
        "status": "success",
        "total_approved": approved_count,
        "approved_count": approved_count,
        "total_failed": len(errors),
        "errors": errors
    }


def get_pending_count(user_id: str = "default") -> int:
    """Returns count of active pending orders."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM pending_orders WHERE status='pending'").fetchone()
        return int(row["cnt"]) if row else 0


def get_pending_orders(status_filter: str = "all") -> Dict[str, Any]:
    """Returns pending order items and statistics matching OpenAlgo response schema."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM pending_orders ORDER BY id DESC").fetchall()

    orders: List[Dict[str, Any]] = []
    total_pending = 0
    total_approved = 0
    total_rejected = 0
    total_buy = 0
    total_sell = 0

    for r in rows:
        r_dict = dict(r)
        status = r_dict["status"]

        if status == "pending":
            total_pending += 1
        elif status == "approved":
            total_approved += 1
        elif status == "rejected":
            total_rejected += 1

        try:
            raw_data = json.loads(r_dict["order_data"])
        except Exception:
            raw_data = {}

        action = str(raw_data.get("action", "BUY")).upper()
        if action == "BUY":
            total_buy += 1
        elif action == "SELL":
            total_sell += 1

        if status_filter != "all" and status.lower() != status_filter.lower():
            continue

        orders.append({
            "id": r_dict["id"],
            "strategy": raw_data.get("strategy", "Manual / Webhook"),
            "api_type": r_dict["api_type"],
            "symbol": raw_data.get("symbol", "NIFTY"),
            "exchange": raw_data.get("exchange", "NSE"),
            "action": action,
            "quantity": raw_data.get("quantity", 1),
            "price": raw_data.get("price", 0.0),
            "price_type": raw_data.get("pricetype", "MARKET"),
            "product_type": raw_data.get("product", "MIS"),
            "status": status,
            "created_at_ist": r_dict["created_at_ist"],
            "approved_at_ist": r_dict["approved_at_ist"],
            "approved_by": r_dict["approved_by"],
            "rejected_at_ist": r_dict["rejected_at_ist"],
            "rejected_reason": r_dict["rejected_reason"],
            "broker_order_id": r_dict["broker_order_id"],
            "raw_order_data": raw_data,
            "order_data": raw_data
        })

    return {
        "status": "success",
        "data": {
            "orders": orders,
            "statistics": {
                "total_pending": total_pending,
                "total_approved": total_approved,
                "total_rejected": total_rejected,
                "total_buy_orders": total_buy,
                "total_sell_orders": total_sell
            }
        }
    }
