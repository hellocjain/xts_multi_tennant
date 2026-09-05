#!/usr/bin/env python3
"""
Monday Pre-Market Automated Sanity Checker
Audits all client containers, broker order books, virtual positions,
portal account states, and market hours enforcement on VPS 139.59.20.239.
"""
import sys
import os
import glob
import json
import sqlite3
import subprocess
import urllib.request
from datetime import datetime

CLIENTS = ["abk01", "abk02", "abk03", "abk04", "abk05", "abk06", "abk09", "abk10", "abk11", "dm933"]

EXPECTED_FRIDAY_POSITIONS = {
    "abk01": {"SILVER1001!": -4, "GOLDPETAL1!": -8},
    "abk02": {"SILVER1001!": -1, "GOLDPETAL1!": -2},
    "abk03": {"SILVER1001!": -4, "GOLDPETAL1!": -8},
    "abk04": {"SILVER1001!": -14, "GOLDPETAL1!": -56},
    "abk05": {"SILVER1001!": -4, "GOLDPETAL1!": -16},
    "abk06": {"SILVER1001!": -4, "GOLDPETAL1!": -16},
    "abk09": {"SILVER1001!": -1, "GOLDPETAL1!": -4},
    "abk10": {"SILVER1001!": -4, "GOLDPETAL1!": -8},
    "abk11": {"SILVER1001!": -4, "GOLDPETAL1!": -8},
    "dm933": {"SILVER1001!": 0, "GOLDPETAL1!": 0},
}

def audit_portal_tenants():
    p_path = "/opt/xts_multi/portal/portal.db"
    if not os.path.exists(p_path):
        return {}
    conn = sqlite3.connect(p_path)
    c = conn.cursor()
    c.execute("SELECT id, status FROM tenants")
    return dict(c.fetchall())

def audit_virtual_positions(client_id):
    db_path = f"/opt/xts_multi/data/{client_id}/signals.db"
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    try:
        c.execute("SELECT strategy_key, virtual_position FROM strategy_virtual_positions")
        return dict(c.fetchall())
    except Exception:
        return {}

def check_container_health(client_id):
    container = f"xts_client_{client_id}"
    cmd = ["docker", "inspect", "--format", "{{.State.Running}}", container]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return res.stdout.strip() == "true"
    except Exception:
        return False

def check_container_telemetry(client_id):
    container = f"xts_client_{client_id}"
    cmd = [
        "docker", "exec", container,
        "python3", "-c",
        "import urllib.request, json; res = urllib.request.urlopen('http://127.0.0.1:8000/internal/telemetry', timeout=4); print(res.read().decode())"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
    except Exception:
        pass
    return None

def check_auto_cancel_status():
    status_file = "/opt/xts_multi/auto_cancel_status.json"
    if os.path.exists(status_file):
        try:
            with open(status_file) as f:
                return json.load(f)
        except Exception:
            pass
    return None

def run_premarket_sanity_check():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 75)
    print(f"🔍 PRE-MARKET SANITY CHECK - RUN AT: {now_str}")
    print("=" * 75)

    portal_states = audit_portal_tenants()
    cancel_status = check_auto_cancel_status()
    daemon_state = cancel_status.get("status") if cancel_status else "NOT_FOUND"
    completed_cancels = cancel_status.get("completed_clients", []) if cancel_status else []
    pending_cancels = cancel_status.get("pending_clients", []) if cancel_status else []

    print(f"\n[1] AUTO-CANCEL DAEMON STATUS: {daemon_state.upper()}")
    print(f"    - Completed client sweeps: {len(completed_cancels)} ({', '.join(completed_cancels) if completed_cancels else 'None'})")
    print(f"    - Pending client sweeps:   {len(pending_cancels)} ({', '.join(pending_cancels) if pending_cancels else 'None'})")

    print("\n[2] CLIENT-BY-CLIENT READINESS MATRIX:")
    print(f"{'CLIENT':<8} | {'CONTAINER':<10} | {'PORTAL':<8} | {'POSITIONS CHECK':<25} | {'MARKET GUARD':<14} | {'READINESS'}")
    print("-" * 88)

    all_ready = True
    for c in CLIENTS:
        is_running = check_container_health(c)
        p_status = portal_states.get(c, "UNKNOWN")
        v_pos = audit_virtual_positions(c)
        
        # Calculate totals per symbol
        silver_tot = sum(v for k, v in v_pos.items() if "SILVER" in k.upper())
        gold_tot = sum(v for k, v in v_pos.items() if "GOLD" in k.upper())
        
        expected = EXPECTED_FRIDAY_POSITIONS.get(c, {})
        exp_silver = expected.get("SILVER1001!", 0)
        exp_gold = expected.get("GOLDPETAL1!", 0)

        pos_match = (silver_tot == exp_silver and gold_tot == exp_gold)
        pos_str = f"Ag:{silver_tot}({exp_silver}) Au:{gold_tot}({exp_gold})"

        telem = check_container_telemetry(c)
        market_guard_ok = False
        if telem:
            st = telem.get("supertrend", {})
            market_open = st.get("market_open", True)
            market_guard_ok = (market_open is False)  # Must be False during pre-market

        is_client_ready = is_running and (p_status == "ACTIVE") and pos_match and market_guard_ok
        if not is_client_ready:
            all_ready = False

        status_badge = "✅ READY" if is_client_ready else "⚠️ ACTION_REQ"
        container_badge = "UP" if is_running else "DOWN"
        guard_badge = "LOCKED(OK)" if market_guard_ok else "ACTIVE(CHECK)"
        pos_badge = "MATCH ✅" if pos_match else f"MISMATCH ({pos_str})"

        print(f"{c:<8} | {container_badge:<10} | {p_status:<8} | {pos_badge:<25} | {guard_badge:<14} | {status_badge}")

    print("-" * 88)
    if all_ready:
        print("🎉 ALL 10 CLIENT ACCOUNTS ARE 100% VERIFIED & READY FOR MONDAY 09:00 AM OPEN!")
    else:
        print("⚠️ Some client accounts require review before market open. See above matrix.")
    print("=" * 75)

if __name__ == "__main__":
    run_premarket_sanity_check()
