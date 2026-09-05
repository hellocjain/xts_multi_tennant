"""
client/gtt_service.py - Server-Side GTT (Good-Till-Triggered) Conditional Order Engine.
Implements 1:1 OpenAlgo GTT Parity:
1. Single Leg GTT (Target OR Stop-Loss).
2. Two-Leg OCO (One-Cancels-the-Other) GTT.
3. Server-side tick evaluation against live prices with automatic child order placement.
4. GTT Order Book, Modify, and Cancel APIs.
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
import notification_service

logger = logging.getLogger("gtt_service")

GTT_DB_FILE = os.path.join(getattr(config, "DATA_DIR", "/app/data"), "gtt_orders.db")


def get_ist_timestamp() -> str:
    """Returns current timestamp formatted in IST."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def get_db_connection():
    os.makedirs(os.path.dirname(os.path.abspath(GTT_DB_FILE)), exist_ok=True)
    conn = sqlite3.connect(GTT_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_gtt_db():
    """Initializes GTT persistence table."""
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gtt_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                strategy TEXT,
                trigger_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                action TEXT NOT NULL,
                product TEXT NOT NULL,
                quantity REAL NOT NULL,
                pricetype TEXT NOT NULL,
                price REAL NOT NULL,
                triggerprice_sl REAL DEFAULT 0.0,
                triggerprice_tg REAL DEFAULT 0.0,
                stoploss REAL DEFAULT 0.0,
                target REAL DEFAULT 0.0,
                status TEXT DEFAULT 'ACTIVE',
                triggered_leg TEXT DEFAULT '',
                child_order_id TEXT DEFAULT '',
                created_at_ist TEXT NOT NULL,
                updated_at_ist TEXT NOT NULL
            )
        """)
        conn.commit()


# Initialize table on module load
init_gtt_db()


def place_gtt_order(data: Dict[str, Any], user_id: str = "default", tenant_id: str = "default") -> Dict[str, Any]:
    """Places a new Single or OCO GTT rule."""
    symbol = str(data.get("symbol", "")).strip().upper()
    exchange = str(data.get("exchange", "NSE")).strip().upper()
    action = str(data.get("action", "BUY")).strip().upper()
    product = str(data.get("product", "CNC")).strip().upper()
    quantity = float(data.get("quantity", 1))
    pricetype = str(data.get("pricetype", "LIMIT")).strip().upper()
    price = float(data.get("price", 0.0))
    trigger_type = str(data.get("trigger_type", "SINGLE")).strip().upper()
    strategy = str(data.get("strategy", "GTTOrder")).strip()

    triggerprice_sl = float(data.get("triggerprice_sl", 0.0))
    triggerprice_tg = float(data.get("triggerprice_tg", 0.0))
    stoploss = float(data.get("stoploss", 0.0))
    target = float(data.get("target", 0.0))

    if trigger_type not in ("SINGLE", "OCO"):
        trigger_type = "SINGLE"

    if trigger_type == "SINGLE" and triggerprice_sl <= 0 and triggerprice_tg <= 0:
        return {"status": "error", "message": "SINGLE GTT requires either triggerprice_sl or triggerprice_tg"}

    if trigger_type == "OCO" and (triggerprice_sl <= 0 or triggerprice_tg <= 0):
        return {"status": "error", "message": "OCO GTT requires both triggerprice_sl and triggerprice_tg"}

    trigger_id = f"GTT-{int(time.time()*1000)}"
    now_ist = get_ist_timestamp()

    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO gtt_orders (
                trigger_id, user_id, tenant_id, strategy, trigger_type, symbol, exchange,
                action, product, quantity, pricetype, price, triggerprice_sl, triggerprice_tg,
                stoploss, target, status, created_at_ist, updated_at_ist
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
        """, (
            trigger_id, user_id, tenant_id, strategy, trigger_type, symbol, exchange,
            action, product, quantity, pricetype, price, triggerprice_sl, triggerprice_tg,
            stoploss, target, now_ist, now_ist
        ))
        conn.commit()

    return {
        "status": "success",
        "trigger_id": trigger_id,
        "symbol": symbol,
        "trigger_type": trigger_type,
        "message": f"GTT order {trigger_id} placed successfully"
    }


def modify_gtt_order(data: Dict[str, Any], user_id: str = "default") -> Dict[str, Any]:
    """Modifies trigger or limit prices for an active GTT order."""
    trigger_id = str(data.get("trigger_id") or data.get("triggerid") or "").strip()
    if not trigger_id:
        return {"status": "error", "message": "trigger_id is required"}

    now_ist = get_ist_timestamp()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM gtt_orders WHERE trigger_id=?", (trigger_id,)).fetchone()
        if not row:
            return {"status": "error", "message": f"GTT order {trigger_id} not found"}
        if row["status"] != "ACTIVE":
            return {"status": "error", "message": f"Cannot modify GTT in status '{row['status']}'"}

        new_sl = float(data.get("triggerprice_sl", row["triggerprice_sl"]))
        new_tg = float(data.get("triggerprice_tg", row["triggerprice_tg"]))
        new_price = float(data.get("price", row["price"]))
        new_qty = float(data.get("quantity", row["quantity"]))
        new_stoploss = float(data.get("stoploss", row["stoploss"]))
        new_target = float(data.get("target", row["target"]))

        conn.execute("""
            UPDATE gtt_orders SET
                triggerprice_sl=?, triggerprice_tg=?, price=?, quantity=?,
                stoploss=?, target=?, updated_at_ist=?
            WHERE trigger_id=?
        """, (new_sl, new_tg, new_price, new_qty, new_stoploss, new_target, now_ist, trigger_id))
        conn.commit()

    return {
        "status": "success",
        "trigger_id": trigger_id,
        "message": f"GTT order {trigger_id} modified successfully"
    }


def cancel_gtt_order(trigger_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Cancels an active GTT order."""
    now_ist = get_ist_timestamp()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM gtt_orders WHERE trigger_id=?", (trigger_id,)).fetchone()
        if not row:
            return {"status": "error", "message": f"GTT order {trigger_id} not found"}
        if row["status"] != "ACTIVE":
            return {"status": "error", "message": f"GTT order is already {row['status']}"}

        conn.execute("UPDATE gtt_orders SET status='CANCELLED', updated_at_ist=? WHERE trigger_id=?", (now_ist, trigger_id))
        conn.commit()

    return {
        "status": "success",
        "trigger_id": trigger_id,
        "message": f"GTT order {trigger_id} cancelled successfully"
    }


def get_gtt_orderbook(user_id: str = "default", status_filter: str = "all") -> Dict[str, Any]:
    """Returns list of GTT orders filtered by status."""
    with get_db_connection() as conn:
        query = "SELECT * FROM gtt_orders ORDER BY id DESC"
        rows = conn.execute(query).fetchall()

    items = []
    for r in rows:
        r_dict = dict(r)
        if status_filter != "all" and r_dict["status"].lower() != status_filter.lower():
            continue
        items.append(r_dict)

    return {
        "status": "success",
        "data": items,
        "total": len(items)
    }


def evaluate_tick(symbol: str, ltp: float, is_paper: bool = True) -> List[Dict[str, Any]]:
    """
    Evaluates active GTT orders against live incoming tick price.
    Triggers child orders and manages OCO cancellation.
    """
    triggered_actions = []
    now_ist = get_ist_timestamp()

    with get_db_connection() as conn:
        active_gtts = conn.execute(
            "SELECT * FROM gtt_orders WHERE symbol=? AND status='ACTIVE'",
            (symbol.upper(),)
        ).fetchall()

        for gtt in active_gtts:
            t_id = gtt["trigger_id"]
            action = gtt["action"]
            qty = int(gtt["quantity"])
            t_type = gtt["trigger_type"]
            sl_price = gtt["triggerprice_sl"]
            tg_price = gtt["triggerprice_tg"]

            triggered = False
            leg_name = ""
            exec_price = gtt["price"]

            if t_type == "SINGLE":
                # For BUY: trigger when price >= tg or <= sl
                # For SELL: trigger when price <= sl (stoploss) or >= tg (target)
                if sl_price > 0:
                    if (action == "SELL" and ltp <= sl_price) or (action == "BUY" and ltp <= sl_price):
                        triggered = True
                        leg_name = "STOPLOSS"
                        exec_price = gtt["price"] or ltp
                if not triggered and tg_price > 0:
                    if (action == "SELL" and ltp >= tg_price) or (action == "BUY" and ltp >= tg_price):
                        triggered = True
                        leg_name = "TARGET"
                        exec_price = gtt["price"] or ltp

            elif t_type == "OCO":
                if sl_price > 0 and ltp <= sl_price:
                    triggered = True
                    leg_name = "STOPLOSS"
                    exec_price = gtt["stoploss"] or ltp
                elif tg_price > 0 and ltp >= tg_price:
                    triggered = True
                    leg_name = "TARGET"
                    exec_price = gtt["target"] or ltp

            if triggered:
                order_ref = f"gtt_{t_id}_{leg_name.lower()}"
                try:
                    res = xts_api.place_order(
                        action=action,
                        symbol=symbol,
                        quantity=qty,
                        tv_price=exec_price if gtt["pricetype"] == "LIMIT" else 0.0,
                        order_ref=order_ref,
                        is_paper=is_paper
                    )
                except Exception as ord_err:
                    logger.error(f"Error placing child order for GTT {t_id}: {ord_err}")
                    res = {"status": "error", "message": str(ord_err)}

                child_id = (res.get("result") or {}).get("AppOrderID") or res.get("orderid") or f"ORD_{int(time.time())}"

                conn.execute("""
                    UPDATE gtt_orders SET
                        status='TRIGGERED', triggered_leg=?, child_order_id=?, updated_at_ist=?
                    WHERE trigger_id=?
                """, (leg_name, str(child_id), now_ist, t_id))

                triggered_actions.append({
                    "trigger_id": t_id,
                    "symbol": symbol,
                    "leg": leg_name,
                    "status": "TRIGGERED",
                    "child_order_id": str(child_id),
                    "execution_price": exec_price
                })
                logger.info(f"GTT {t_id} triggered ({leg_name}) for {symbol} at {ltp}, placed child {child_id}")

        conn.commit()

    return triggered_actions
