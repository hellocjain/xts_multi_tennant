import json
import sys
import os
import paramiko

clients = ['abk01', 'abk02', 'abk03', 'abk04', 'abk05', 'abk06', 'abk07', 'abk09', 'abk10', 'abk11', 'abk12', 'abk13', 'abk14', 'abk15', 'dm933']
print("| Client | Broker NetWise Positions | Strategy Positions |")
print("|:---|:---|:---|")

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
            p_list = [f"{p.get('symbol')}: {p.get('quantity')}" for p in pos if int(p.get('quantity', 0)) != 0]
            strats = d.get('supertrend', {}).get('strategies', [])
            s_list = [f"{s.get('symbol')}({s.get('timeframe')}): {s.get('virtual_position')}" for s in strats if s.get('virtual_position', 0) != 0]
            p_str = ", ".join(p_list) if p_list else "FLAT (0)"
            s_str = ", ".join(s_list) if s_list else "FLAT (0)"
            print(f"| {c} | {p_str} | {s_str} |")
        except Exception as e:
            print(f"| {c} | Parse error: {e} | |")
    else:
        print(f"| {c} | No response | |")

client.close()
