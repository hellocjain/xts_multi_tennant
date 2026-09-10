import json
import sys
import os
import paramiko

clients = [
    ("abk01", "Divya Yadav"),
    ("abk02", "Divya Yadav"),
    ("abk03", "Adarsh Somani"),
    ("abk04", "Laxmi Narayan"),
    ("abk05", "Hemant Kumar"),
    ("abk06", "Prashant"),
    ("abk09", "Lokesh Kumar Mishra"),
    ("abk10", "Devendra"),
    ("abk11", "Bhawani"),
    ("abk12", "NK Vaishnav"),
    ("dm933", "Paper/Demo")
]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("139.59.20.239", username="root", password="Check", timeout=20)

print("\n" + "="*95)
print("🔍 EXHAUSTIVE LIVE BROKER AUDIT - RECHECK")
print("="*95)
print(f"{'Client':<8} | {'Owner':<20} | {'Open Broker Positions':<26} | {'Open Orders':<14} | {'Strategy State':<15}")
print("-" * 95)

all_clean = True

for c, owner in clients:
    cmd_tel = f"docker exec xts_client_{c} curl -s http://127.0.0.1:8000/internal/telemetry"
    stdin, stdout, stderr = client.exec_command(cmd_tel)
    raw = stdout.read().decode("utf-8").strip()
    
    if raw:
        try:
            d = json.loads(raw)
            # 1. Broker Positions
            pos_data = d.get("positions", {})
            pos = pos_data.get("positions", []) if isinstance(pos_data, dict) else []
            active_pos = [f"{p.get('symbol')}: {p.get('quantity')}" for p in pos if int(p.get("quantity", 0)) != 0]
            pos_str = ", ".join(active_pos) if active_pos else "0 Open (FLAT) ✅"

            # 2. Broker Orders
            orders = d.get("broker_orders", []) if isinstance(d.get("broker_orders"), list) else []
            open_orders = [o for o in orders if isinstance(o, dict) and o.get("OrderStatus") in ("Open", "New", "Pending", "PartiallyFilled", "PendingNew", "Replaced")]
            ord_str = f"{len(open_orders)} open ⚠️" if open_orders else "0 open ✅"

            # 3. SuperTrend Strategies
            strats = d.get("supertrend", {}).get("strategies", []) if isinstance(d.get("supertrend"), dict) else []
            active_strats = [f"{s.get('symbol')}({s.get('timeframe')}): {s.get('virtual_position')}" for s in strats if s.get("virtual_position", 0) != 0]
            st_status = ", ".join(active_strats) if active_strats else "FLAT (0) ✅"

            if active_pos or open_orders or active_strats:
                all_clean = False

            print(f"{c:<8} | {owner:<20} | {pos_str:<26} | {ord_str:<14} | {st_status:<15}")
        except Exception as e:
            all_clean = False
            print(f"{c:<8} | {owner:<20} | Parse error: {e}")
    else:
        all_clean = False
        print(f"{c:<8} | {owner:<20} | No response from container")

# Also check portal database strategy state
cmd_db = "sqlite3 /opt/xts_multi/portal/portal.db 'SELECT COUNT(*) FROM tenant_supertrend_strategies WHERE is_enabled=1;'"
stdin, stdout, stderr = client.exec_command(cmd_db)
enabled_strats_count = int(stdout.read().decode("utf-8").strip() or 0)

client.close()

print("-" * 95)
print(f"Portal DB Active Strategies Count (is_enabled=1): {enabled_strats_count}")
print(f"Overall State: {'✅ 100% FLAT & CLEAN ACROSS ALL CLIENTS (ZERO POSITIONS, ZERO ORDERS)' if (all_clean and enabled_strats_count == 0) else '⚠️ OPEN POSITIONS/ORDERS DETECTED'}")
print("="*95 + "\n")
