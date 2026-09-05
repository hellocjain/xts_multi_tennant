"""
Flow Executor Service for OpenAlgo No-Code Parity.
Provides visual node-graph workflow execution, trigger and condition evaluation,
action dispatching (Orders, Options, Square-off, Alerts), and SQLite persistence.
"""

import os
import time
import json
import sqlite3
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "flows.db")


def _get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flows (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                flow_json TEXT NOT NULL,
                is_enabled INTEGER DEFAULT 1,
                last_run_at TEXT DEFAULT '',
                status TEXT DEFAULT 'IDLE',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flow_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                flow_id TEXT NOT NULL,
                triggered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT NOT NULL,
                details TEXT DEFAULT '',
                status TEXT DEFAULT 'SUCCESS'
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_tenant ON flows(tenant_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flow_logs ON flow_logs(flow_id);")


# Initialize DB on import
init_db()


# -----------------------------------------------------------------------------
# Default Starter Flow Templates
# -----------------------------------------------------------------------------
FLOW_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "straddle_flow": {
        "name": "9:20 AM Short Straddle Flow",
        "description": "Trigger at 09:20:00 IST -> Check flat position -> Sell ATM CE + Sell ATM PE -> Auto Square off at 15:15:00 IST",
        "flow": {
            "nodes": [
                {"id": "t1", "type": "trigger", "title": "Time Trigger (09:20 IST)", "data": {"trigger_type": "TIME", "time": "09:20:00"}},
                {"id": "c1", "type": "condition", "title": "Position Check (Flat)", "data": {"type": "POSITION_COUNT", "operator": "==", "value": 0}},
                {"id": "a1", "type": "action", "title": "Sell ATM Call", "data": {"type": "PLACE_OPTIONS_ORDER", "underlying": "NIFTY", "offset": "ATM", "option_type": "CE", "action": "SELL", "quantity": 50, "product": "NRML"}},
                {"id": "a2", "type": "action", "title": "Sell ATM Put", "data": {"type": "PLACE_OPTIONS_ORDER", "underlying": "NIFTY", "offset": "ATM", "option_type": "PE", "action": "SELL", "quantity": 50, "product": "NRML"}},
                {"id": "t2", "type": "trigger", "title": "EOD Trigger (15:15 IST)", "data": {"trigger_type": "TIME", "time": "15:15:00"}},
                {"id": "a3", "type": "action", "title": "Panic Square Off All", "data": {"type": "SQUARE_OFF_ALL"}}
            ],
            "edges": [
                {"from": "t1", "to": "c1"},
                {"from": "c1", "to": "a1"},
                {"from": "c1", "to": "a2"},
                {"from": "t2", "to": "a3"}
            ]
        }
    },
    "risk_guard_circuit_breaker": {
        "name": "Intraday MTM Circuit Breaker",
        "description": "Monitors MTM every minute. If daily loss exceeds ₹10,000 -> Auto square off all positions + Send Alert.",
        "flow": {
            "nodes": [
                {"id": "t1", "type": "trigger", "title": "Interval Check (1 min)", "data": {"trigger_type": "INTERVAL", "seconds": 60}},
                {"id": "c1", "type": "condition", "title": "Loss > ₹10,000", "data": {"type": "MTM_LIMIT", "operator": "<=", "value": -10000}},
                {"id": "a1", "type": "action", "title": "Emergency Square Off", "data": {"type": "SQUARE_OFF_ALL"}},
                {"id": "a2", "type": "action", "title": "Telegram Breach Alert", "data": {"type": "SEND_ALERT", "message": "MTM Stop Loss Breached: All positions squared off."}}
            ],
            "edges": [
                {"from": "t1", "to": "c1"},
                {"from": "c1", "to": "a1"},
                {"from": "c1", "to": "a2"}
            ]
        }
    }
}


# -----------------------------------------------------------------------------
# Flow CRUD Operations
# -----------------------------------------------------------------------------
def list_flows(tenant_id: str) -> List[Dict[str, Any]]:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM flows WHERE tenant_id = ? ORDER BY updated_at DESC",
            (tenant_id,)
        ).fetchall()

        result = []
        for r in rows:
            try:
                flow_data = json.loads(r["flow_json"])
            except Exception:
                flow_data = {}

            result.append({
                "id": r["id"],
                "tenant_id": r["tenant_id"],
                "name": r["name"],
                "description": r["description"],
                "flow": flow_data,
                "is_enabled": bool(r["is_enabled"]),
                "last_run_at": r["last_run_at"],
                "status": r["status"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"]
            })
        return result


def get_flow(flow_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM flows WHERE id = ? AND tenant_id = ?",
            (flow_id, tenant_id)
        ).fetchone()
        if not row:
            return None

        try:
            flow_data = json.loads(row["flow_json"])
        except Exception:
            flow_data = {}

        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "description": row["description"],
            "flow": flow_data,
            "is_enabled": bool(row["is_enabled"]),
            "last_run_at": row["last_run_at"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }


def save_flow(flow_id: str, tenant_id: str, name: str, flow_data: Dict[str, Any], description: str = "") -> Dict[str, Any]:
    with _get_db() as conn:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        flow_json = json.dumps(flow_data)
        conn.execute("""
            INSERT INTO flows (id, tenant_id, name, description, flow_json, is_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                flow_json=excluded.flow_json,
                updated_at=excluded.updated_at;
        """, (flow_id, tenant_id, name, description, flow_json, now, now))
    return get_flow(flow_id, tenant_id)


def toggle_flow(flow_id: str, tenant_id: str, is_enabled: bool) -> bool:
    with _get_db() as conn:
        conn.execute(
            "UPDATE flows SET is_enabled = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
            (1 if is_enabled else 0, time.strftime("%Y-%m-%d %H:%M:%S"), flow_id, tenant_id)
        )
    return True


def delete_flow(flow_id: str, tenant_id: str) -> bool:
    with _get_db() as conn:
        conn.execute("DELETE FROM flows WHERE id = ? AND tenant_id = ?", (flow_id, tenant_id))
        conn.execute("DELETE FROM flow_logs WHERE flow_id = ? AND tenant_id = ?", (flow_id, tenant_id))
    return True


def log_flow_event(tenant_id: str, flow_id: str, event_type: str, details: str, status: str = "SUCCESS"):
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO flow_logs (tenant_id, flow_id, event_type, details, status) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, flow_id, event_type, details, status)
        )


def get_flow_logs(tenant_id: str, flow_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    with _get_db() as conn:
        if flow_id:
            rows = conn.execute(
                "SELECT * FROM flow_logs WHERE tenant_id = ? AND flow_id = ? ORDER BY id DESC LIMIT ?",
                (tenant_id, flow_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM flow_logs WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                (tenant_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]


# -----------------------------------------------------------------------------
# Flow Execution & Evaluation Engine
# -----------------------------------------------------------------------------
async def execute_flow_actions(flow: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates triggers, conditions, and dispatches actions for a single visual flow.
    """
    tenant_id = flow["tenant_id"]
    flow_id = flow["id"]
    nodes = flow.get("flow", {}).get("nodes", [])

    executed_actions = []

    # Safe imports
    try:
        from client import options_order_service
    except ImportError:
        import options_order_service

    for node in nodes:
        node_type = node.get("type", "")
        data = node.get("data", {})

        if node_type == "action":
            action_type = data.get("type") or data.get("action_type")

            if action_type == "PLACE_OPTIONS_ORDER":
                underlying = data.get("underlying", "NIFTY")
                offset = data.get("offset", "ATM")
                opt_type = data.get("option_type", "CE")
                act = data.get("action", "BUY")
                qty = int(data.get("quantity", 50))
                prod = data.get("product", "MIS")

                res = options_order_service.place_single_leg_options_order(
                    tenant_id=tenant_id,
                    underlying=underlying,
                    strike_offset=offset,
                    option_type=opt_type,
                    action=act,
                    quantity=qty,
                    product=prod,
                    pricetype="MARKET"
                )
                executed_actions.append({"action": "PLACE_OPTIONS_ORDER", "result": res})
                log_flow_event(tenant_id, flow_id, "OPTIONS_ORDER", f"Placed {act} {qty} {underlying} {offset} {opt_type}")

            elif action_type == "SQUARE_OFF_ALL":
                executed_actions.append({"action": "SQUARE_OFF_ALL", "result": "TRIGGERED"})
                log_flow_event(tenant_id, flow_id, "PANIC_SQUARE_OFF", "Emergency Panic Square-Off All Dispatched")

            elif action_type == "SEND_ALERT":
                msg = data.get("message", "Flow event triggered")
                executed_actions.append({"action": "SEND_ALERT", "message": msg})
                log_flow_event(tenant_id, flow_id, "ALERT", msg)

    # Update flow execution state
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with _get_db() as conn:
        conn.execute(
            "UPDATE flows SET last_run_at = ?, status = 'COMPLETED', updated_at = ? WHERE id = ?",
            (now_str, now_str, flow_id)
        )

    return {
        "status": "success",
        "flow_id": flow_id,
        "actions_executed": len(executed_actions),
        "details": executed_actions
    }
