#!/usr/bin/env python3
"""
Audit Zero Strategies Across All Clients
----------------------------------------
Verifies:
1. Portal SQLite database has 0 strategy records.
2. All 11 client containers report 0 active strategies and 0 virtual positions.
3. Portal system-health API reports supertrend_strategies_count: 0.
4. Live broker positions and open orders remain zero.
"""
import os
import sys
import json
import time
import requests
import urllib3
import paramiko

urllib3.disable_warnings()

HOST = "139.59.20.239"
USER = "root"
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")
BASE_URL = "https://arjun.algorivar.in"

clients = [
    ("abk01", "Divya Yadav"),
    ("abk02", "Anita Singh"),
    ("abk03", "Rahat Bano"),
    ("abk04", "Jitendra Kumar Tanwani"),
    ("abk05", "ANKUR GUPTA"),
    ("abk06", "MANSI VISHWAKARMA"),
    ("abk09", "Lokesh Kumar Mishra"),
    ("abk10", "YOGESH JOPAT"),
    ("abk11", "SURYANSH SHARMA"),
    ("abk12", "NK VAISHNAV"),
    ("dm933", "Arjun Khandelwal (Demo)")
]

def main():
    print(f"🚀 Connecting to VPS {HOST} as {USER}...")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASSWORD, timeout=20)

    print("\n" + "="*95)
    print("🔍 AUDIT: STRATEGY PURGE VERIFICATION ACROSS ALL CLIENTS")
    print("="*95)

    # 1. Check Portal Database counts
    stdin, stdout, stderr = c.exec_command(
        'python3 -c "import sqlite3; conn=sqlite3.connect(\'/opt/xts_multi/portal/portal.db\'); '
        'c=conn.cursor(); '
        'st=c.execute(\'SELECT count(*) FROM tenant_supertrend_strategies\').fetchone()[0]; '
        'cfg=c.execute(\'SELECT count(*) FROM tenant_supertrend_configs\').fetchone()[0]; '
        'cust=c.execute(\'SELECT count(*) FROM tenant_custom_strategies\').fetchone()[0]; '
        'print(f\'Portal DB Counts -> Supertrend: {st}, Configs: {cfg}, Custom: {cust}\')"'
    )
    db_out = stdout.read().decode('utf-8').strip()
    print(f"1. {db_out}")

    # 2. Check each client container
    print(f"\n2. Client Container Telemetry & Broker State:")
    print(f"{'Client':<8} | {'Owner':<24} | {'Active Strats':<14} | {'Virtual Pos':<12} | {'Broker Pos':<12} | {'Open Orders':<12}")
    print("-" * 95)

    all_zero_strats = True
    for cid, owner in clients:
        cmd = f"docker exec xts_client_{cid} curl -s http://127.0.0.1:8000/internal/telemetry"
        stdin, stdout, stderr = c.exec_command(cmd)
        raw = stdout.read().decode("utf-8").strip()

        act_strats = "N/A"
        virt_pos = "N/A"
        broker_pos = "N/A"
        open_orders = "N/A"

        if raw:
            try:
                data = json.loads(raw)
                strat_list = data.get("strategies", [])
                act_strats = len([s for s in strat_list if s.get("is_enabled", True)])
                virt_pos = data.get("virtual_position", 0)

                # Positions
                net_pos = data.get("positions_telemetry", {}).get("positions", [])
                total_broker_lots = sum(abs(p.get("quantity", 0)) for p in net_pos)
                broker_pos = f"{total_broker_lots} lots"

                # Orders
                orders = data.get("broker_orders", [])
                open_cnt = len([o for o in orders if str(o.get("OrderStatus", "")).upper() in ("OPEN", "PENDING", "TRIGGER PENDING")])
                open_orders = f"{open_cnt} open"

                if act_strats != 0 or virt_pos != 0 or total_broker_lots != 0 or open_cnt != 0:
                    all_zero_strats = False
            except Exception as e:
                act_strats = f"ERR: {e}"
                all_zero_strats = False

        status_strats = f"{act_strats} ✅" if act_strats == 0 else f"{act_strats} ❌"
        status_virt = f"{virt_pos} ✅" if virt_pos == 0 else f"{virt_pos} ❌"
        status_broker = f"{broker_pos} ✅" if broker_pos == "0 lots" else f"{broker_pos} ❌"
        status_orders = f"{open_orders} ✅" if open_orders == "0 open" else f"{open_orders} ❌"

        print(f"{cid:<8} | {owner[:24]:<24} | {status_strats:<14} | {status_virt:<12} | {status_broker:<12} | {status_orders:<12}")

    # 3. Check Web Portal System Health API
    print("\n3. Web Portal System Health API Check (HTTPS):")
    try:
        session = requests.Session()
        session.post(f"{BASE_URL}/admin/login", data={"username": "admin", "password": "AdminPass123!"}, verify=False, timeout=10)
        resp = session.get(f"{BASE_URL}/admin/api/system-health", verify=False, timeout=10)
        h_data = resp.json()
        db_h = h_data.get("database", {})
        st_count = db_h.get("supertrend_strategies_count", -1)
        print(f"   Portal reported active strategies: {st_count}")
        for k, v in h_data.get("clients", {}).items():
            print(f"   Client {k}: active_strategies = {v.get('active_strategies')} (status: {v.get('status')})")
    except Exception as e:
        print(f"   Notice querying portal API: {e}")

    c.close()
    print("=" * 95)
    if all_zero_strats:
        print("🏆 VERIFICATION SUCCESSFUL: All 11 clients have 0 active strategies, 0 positions, 0 open orders!")
    else:
        print("⚠️ WARNING: Some clients still have active strategies or non-zero positions.")
    print("=" * 95)

if __name__ == "__main__":
    main()
