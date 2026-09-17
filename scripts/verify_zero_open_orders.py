import json
import sys
import os
import paramiko

clients = ['abk01', 'abk02', 'abk03', 'abk04', 'abk05', 'abk06', 'abk07', 'abk09', 'abk10', 'abk11', 'abk12', 'abk13', 'abk14', 'abk15', 'dm933']
print("| Client | Broker Open Orders | Broker Open Positions | Status |")
print("|:---|:---|:---|:---|")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("139.59.20.239", username="root", password="Check", timeout=15)

for c in clients:
    cmd = f"docker exec xts_client_{c} curl -s http://127.0.0.1:8000/internal/telemetry"
    stdin, stdout, stderr = client.exec_command(cmd)
    raw = stdout.read().decode('utf-8')
    if raw:
        try:
            d = json.loads(raw)
            pos_data = d.get('positions', {})
            pos = pos_data.get('positions', []) if isinstance(pos_data, dict) else []
            active_pos = [f"{p.get('symbol')}: {p.get('quantity')}" for p in pos if int(p.get('quantity', 0)) != 0]

            orders = d.get('broker_orders', [])
            open_orders = [o for o in orders if o.get('OrderStatus') in ('Open', 'New', 'Pending', 'PartiallyFilled', 'PendingNew', 'Replaced')]

            pos_txt = ", ".join(active_pos) if active_pos else "0 Open (FLAT)"
            ord_txt = f"{len(open_orders)} Open Orders" if open_orders else "0 Open Orders"
            status_txt = "✅ ALL CLEAN & FLAT" if (not active_pos and not open_orders) else "⚠️ ACTION NEEDED"
            print(f"| {c} | {ord_txt} | {pos_txt} | {status_txt} |")
        except Exception as e:
            print(f"| {c} | Error: {e} | | |")

client.close()
