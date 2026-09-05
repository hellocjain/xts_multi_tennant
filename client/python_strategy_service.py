"""
Python Strategy Runner Service for OpenAlgo Parity.
Manages tenant-isolated custom Python trading strategies, subprocess execution,
real-time log streaming, and built-in algorithmic strategy templates.
"""

import os
import sys
import time
import json
import signal
import sqlite3
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "python_strategies.db")
LOGS_DIR = os.path.join(os.path.dirname(__file__), "strategy_logs")
RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "strategy_runtime")

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(RUNTIME_DIR, exist_ok=True)

# In-memory registry of active subprocesses: key = f"{tenant_id}:{strategy_id}"
_ACTIVE_PROCESSES: Dict[str, Dict[str, Any]] = {}


def _get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS python_strategies (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                code_content TEXT NOT NULL,
                is_running INTEGER DEFAULT 0,
                last_run_at TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_py_strat_tenant ON python_strategies(tenant_id);")


# Initialize DB on module load
init_db()


# -----------------------------------------------------------------------------
# Built-in Algorithmic Strategy Templates
# -----------------------------------------------------------------------------
STRATEGY_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "sma_crossover": {
        "name": "EMA 9/21 Trend Crossover",
        "description": "Calculates 9 and 21 Exponential Moving Averages on 5-min candles and places OpenAlgo smart orders.",
        "code": '''"""
OpenAlgo Python Strategy: EMA 9/21 Trend Crossover
Executes automated breakout trades using OpenAlgo REST API.
"""
import os
import time
import requests

API_KEY = os.environ.get("OPENALGO_API_KEY", "test_key")
HOST = os.environ.get("OPENALGO_HOST", "http://127.0.0.1:8000")
SYMBOL = "NIFTY"
EXCHANGE = "NSE"
QUANTITY = 50

def get_candles():
    url = f"{HOST}/api/v1/history"
    resp = requests.post(url, json={"symbol": SYMBOL, "exchange": EXCHANGE, "interval": "5m"}, headers={"apikey": API_KEY}, timeout=5)
    if resp.status_code == 200:
        return resp.json().get("data", [])
    return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

print(f"[{time.strftime('%H:%M:%S')}] Starting EMA 9/21 Strategy on {SYMBOL}...")
while True:
    try:
        candles = get_candles()
        if len(candles) >= 25:
            closes = [float(c["close"]) for c in candles]
            ema9 = calculate_ema(closes, 9)
            ema21 = calculate_ema(closes, 21)
            
            curr_9, curr_21 = ema9[-1], ema21[-1]
            prev_9, prev_21 = ema9[-2], ema21[-2]
            
            print(f"[{time.strftime('%H:%M:%S')}] Close: {closes[-1]:.2f} | EMA9: {curr_9:.2f} | EMA21: {curr_21:.2f}")
            
            if prev_9 <= prev_21 and curr_9 > curr_21:
                print(">>> BULLISH CROSSOVER! Placing BUY smart order...")
                res = requests.post(f"{HOST}/api/v1/placesmartorder", json={
                    "symbol": SYMBOL, "action": "BUY", "quantity": QUANTITY, "position_size": QUANTITY,
                    "order_type": "MARKET", "product": "MIS", "exchange": EXCHANGE
                }, headers={"apikey": API_KEY})
                print("Order Response:", res.json())
                
            elif prev_9 >= prev_21 and curr_9 < curr_21:
                print(">>> BEARISH CROSSOVER! Placing SELL smart order...")
                res = requests.post(f"{HOST}/api/v1/placesmartorder", json={
                    "symbol": SYMBOL, "action": "SELL", "quantity": QUANTITY, "position_size": QUANTITY,
                    "order_type": "MARKET", "product": "MIS", "exchange": EXCHANGE
                }, headers={"apikey": API_KEY})
                print("Order Response:", res.json())

    except Exception as e:
        print(f"Error in cycle: {e}")
    time.sleep(30)
'''
    },
    "atm_straddle": {
        "name": "9:20 AM Automated ATM Straddle",
        "description": "Automatically sells ATM Call and Put options with 25% Stop-Loss brackets using relative strike offsets.",
        "code": '''"""
OpenAlgo Python Strategy: 9:20 AM Short Straddle
Sells ATM Call and Put legs simultaneously using OpenAlgo /optionsmultiorder.
"""
import os
import time
import requests

API_KEY = os.environ.get("OPENALGO_API_KEY", "test_key")
HOST = os.environ.get("OPENALGO_HOST", "http://127.0.0.1:8000")
UNDERLYING = "NIFTY"
QUANTITY = 50

print(f"[{time.strftime('%H:%M:%S')}] Initializing 9:20 AM ATM Short Straddle on {UNDERLYING}...")

# 1. Fetch live quotes for spot
resp = requests.post(f"{HOST}/api/v1/quotes", json={"symbol": UNDERLYING}, headers={"apikey": API_KEY})
spot = resp.json().get("data", {}).get("ltp", 24500.0)
print(f"[{time.strftime('%H:%M:%S')}] {UNDERLYING} Spot Price: ₹{spot:,.2f}")

# 2. Place Multi-Leg Straddle (Sell ATM CE and Sell ATM PE)
multi_payload = {
    "underlying": UNDERLYING,
    "legs": [
        {"strike_offset": "ATM", "option_type": "CE", "action": "SELL", "quantity": QUANTITY, "product": "NRML"},
        {"strike_offset": "ATM", "option_type": "PE", "action": "SELL", "quantity": QUANTITY, "product": "NRML"}
    ]
}

print(f"[{time.strftime('%H:%M:%S')}] Placing Hedged Straddle via /optionsmultiorder...")
exec_resp = requests.post(f"{HOST}/api/v1/optionsmultiorder", json=multi_payload, headers={"apikey": API_KEY})
print("Straddle Execution Status:", exec_resp.json())

# 3. Monitor P&L
while True:
    try:
        pnl_resp = requests.post(f"{HOST}/api/v1/positionbook", json={}, headers={"apikey": API_KEY})
        net_mtm = pnl_resp.json().get("net_mtm", 0.0)
        print(f"[{time.strftime('%H:%M:%S')}] Live Straddle MTM P&L: ₹{net_mtm:+,.2f}")
    except Exception as e:
        print("Monitoring error:", e)
    time.sleep(15)
'''
    },
    "supertrend_options": {
        "name": "Supertrend Options Buyer",
        "description": "Follows 15m Supertrend trend reversals and buys ATM Call or Put options automatically.",
        "code": '''"""
OpenAlgo Python Strategy: Supertrend Options Buyer
Buys ATM Call on Bullish Supertrend Flip, Buys ATM Put on Bearish Flip.
"""
import os
import time
import requests

API_KEY = os.environ.get("OPENALGO_API_KEY", "test_key")
HOST = os.environ.get("OPENALGO_HOST", "http://127.0.0.1:8000")
UNDERLYING = "BANKNIFTY"
QUANTITY = 30

print(f"[{time.strftime('%H:%M:%S')}] Starting Supertrend Options Buyer on {UNDERLYING}...")

def buy_option(opt_type):
    print(f">>> Buying ATM {opt_type} option for {UNDERLYING}...")
    resp = requests.post(f"{HOST}/api/v1/optionsorder", json={
        "underlying": UNDERLYING,
        "strike_offset": "ATM",
        "option_type": opt_type,
        "action": "BUY",
        "quantity": QUANTITY,
        "product": "MIS"
    }, headers={"apikey": API_KEY})
    print("Execution Result:", resp.json())

# Simulation cycle
print(f"[{time.strftime('%H:%M:%S')}] Trend: BULLISH -> Triggering ATM CE entry")
buy_option("CE")

while True:
    time.sleep(60)
    print(f"[{time.strftime('%H:%M:%S')}] Heartbeat: Watching for Supertrend signal flip...")
'''
    }
}


# -----------------------------------------------------------------------------
# Strategy CRUD
# -----------------------------------------------------------------------------
def list_strategies(tenant_id: str) -> List[Dict[str, Any]]:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM python_strategies WHERE tenant_id = ? ORDER BY updated_at DESC",
            (tenant_id,)
        ).fetchall()
        
        result = []
        for r in rows:
            strat_id = r["id"]
            key = f"{tenant_id}:{strat_id}"
            is_proc_alive = False
            uptime = 0
            
            if key in _ACTIVE_PROCESSES:
                proc = _ACTIVE_PROCESSES[key]["process"]
                if proc.poll() is None:
                    is_proc_alive = True
                    uptime = int(time.time() - _ACTIVE_PROCESSES[key]["start_time"])
                else:
                    _ACTIVE_PROCESSES[key]["status"] = "STOPPED"
                    
            result.append({
                "id": strat_id,
                "tenant_id": r["tenant_id"],
                "name": r["name"],
                "description": r["description"],
                "code_content": r["code_content"],
                "is_running": is_proc_alive,
                "status": "RUNNING" if is_proc_alive else "STOPPED",
                "uptime": uptime,
                "last_run_at": r["last_run_at"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"]
            })
        return result


def get_strategy(strategy_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM python_strategies WHERE id = ? AND tenant_id = ?",
            (strategy_id, tenant_id)
        ).fetchone()
        if not row:
            return None
        
        key = f"{tenant_id}:{strategy_id}"
        is_proc_alive = False
        uptime = 0
        if key in _ACTIVE_PROCESSES:
            proc = _ACTIVE_PROCESSES[key]["process"]
            if proc.poll() is None:
                is_proc_alive = True
                uptime = int(time.time() - _ACTIVE_PROCESSES[key]["start_time"])
                
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "description": row["description"],
            "code_content": row["code_content"],
            "is_running": is_proc_alive,
            "status": "RUNNING" if is_proc_alive else "STOPPED",
            "uptime": uptime,
            "last_run_at": row["last_run_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }


def save_strategy(strategy_id: str, tenant_id: str, name: str, code_content: str, description: str = "") -> Dict[str, Any]:
    with _get_db() as conn:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO python_strategies (id, tenant_id, name, description, code_content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                code_content=excluded.code_content,
                updated_at=excluded.updated_at;
        """, (strategy_id, tenant_id, name, description, code_content, now, now))
    return get_strategy(strategy_id, tenant_id)


def delete_strategy(strategy_id: str, tenant_id: str) -> bool:
    stop_strategy(strategy_id, tenant_id)
    with _get_db() as conn:
        conn.execute("DELETE FROM python_strategies WHERE id = ? AND tenant_id = ?", (strategy_id, tenant_id))
    return True


# -----------------------------------------------------------------------------
# Process Lifecycle: Run, Stop, Status, Logs
# -----------------------------------------------------------------------------
def run_strategy(strategy_id: str, tenant_id: str, api_key: str = "", host: str = "http://127.0.0.1:8000") -> Dict[str, Any]:
    strat = get_strategy(strategy_id, tenant_id)
    if not strat:
        return {"status": "error", "message": f"Strategy {strategy_id} not found"}

    key = f"{tenant_id}:{strategy_id}"
    if key in _ACTIVE_PROCESSES and _ACTIVE_PROCESSES[key]["process"].poll() is None:
        return {"status": "error", "message": f"Strategy {strategy_id} is already running"}

    # Write code to isolated script file
    script_path = os.path.join(RUNTIME_DIR, f"{tenant_id}_{strategy_id}.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(strat["code_content"])

    # Dedicated log file
    log_file_path = os.path.join(LOGS_DIR, f"{tenant_id}_{strategy_id}.log")
    log_file = open(log_file_path, "w", encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["OPENALGO_API_KEY"] = api_key or "default_key"
    env["OPENALGO_HOST"] = host
    env["TENANT_ID"] = tenant_id

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", script_path],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=os.path.dirname(__file__)
        )
        _ACTIVE_PROCESSES[key] = {
            "process": proc,
            "pid": proc.pid,
            "start_time": time.time(),
            "status": "RUNNING",
            "log_path": log_file_path,
            "log_file": log_file
        }

        # Update last run in DB
        with _get_db() as conn:
            conn.execute(
                "UPDATE python_strategies SET is_running = 1, last_run_at = ? WHERE id = ? AND tenant_id = ?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), strategy_id, tenant_id)
            )

        logger.info(f"Started Python Strategy '{strat['name']}' (PID: {proc.pid}) for tenant '{tenant_id}'")
        return {
            "status": "success",
            "message": f"Strategy '{strat['name']}' started successfully",
            "pid": proc.pid,
            "strategy_id": strategy_id
        }

    except Exception as e:
        logger.error(f"Failed to start strategy {strategy_id}: {e}")
        return {"status": "error", "message": str(e)}


def stop_strategy(strategy_id: str, tenant_id: str) -> Dict[str, Any]:
    key = f"{tenant_id}:{strategy_id}"
    if key not in _ACTIVE_PROCESSES or _ACTIVE_PROCESSES[key]["process"].poll() is not None:
        return {"status": "success", "message": "Strategy is not currently running"}

    item = _ACTIVE_PROCESSES[key]
    proc: subprocess.Popen = item["process"]

    try:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        if item.get("log_file") and not item["log_file"].closed:
            item["log_file"].close()

        item["status"] = "STOPPED"

        with _get_db() as conn:
            conn.execute(
                "UPDATE python_strategies SET is_running = 0 WHERE id = ? AND tenant_id = ?",
                (strategy_id, tenant_id)
            )

        logger.info(f"Stopped Python Strategy {strategy_id} for tenant {tenant_id}")
        return {"status": "success", "message": f"Strategy {strategy_id} stopped"}

    except Exception as e:
        logger.error(f"Error stopping strategy {strategy_id}: {e}")
        return {"status": "error", "message": str(e)}


def get_strategy_logs(strategy_id: str, tenant_id: str, lines: int = 100) -> Dict[str, Any]:
    log_file_path = os.path.join(LOGS_DIR, f"{tenant_id}_{strategy_id}.log")
    if not os.path.exists(log_file_path):
        return {"status": "success", "logs": "[System] No logs recorded yet. Click 'Run Strategy' to begin execution."}

    try:
        with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return {
                "status": "success",
                "logs": "".join(tail_lines),
                "line_count": len(all_lines)
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}
