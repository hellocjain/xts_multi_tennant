import json
import sys
import os
import time
import paramiko

clients = [
    ("abk01", "Check"),
    ("abk02", "Check"),
    ("abk03", "Check"),
    ("abk04", "Check"),
    ("abk05", "Check"),
    ("abk06", "CHECK"),
    ("abk09", "CHECK"),
    ("abk10", "CHECK"),
    ("abk11", "CHECK"),
    ("abk12", "Check"),
    ("dm933", "Check"),
]

print("🚨 STARTING GLOBAL PANIC SQUARE-OFF ACROSS ALL CLIENTS...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("139.59.20.239", username="root", password="Check", timeout=20)

panic_results = {}

for c, sec in clients:
    print(f"\n⚡ Triggering /panic on xts_client_{c} (secret={sec})...")
    cmd = f"""docker exec xts_client_{c} python3 -c '
import urllib.request, json
req = urllib.request.Request(
    "http://127.0.0.1:8000/panic",
    data=json.dumps({{"secret": "{sec}"}}).encode("utf-8"),
    headers={{"Content-Type": "application/json"}}
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(resp.read().decode("utf-8"))
except Exception as e:
    print(json.dumps({{"status": "error", "error": str(e)}}))
'"""
    stdin, stdout, stderr = client.exec_command(cmd)
    raw = stdout.read().decode("utf-8").strip()
    try:
        res = json.loads(raw)
        panic_results[c] = res
        status = res.get("status")
        sq_off = res.get("squared_off", [])
        failed = res.get("failed_chunks", [])
        print(f"  Result [{c}]: status={status} | squared_off={len(sq_off)} chunks | failed={len(failed)} chunks")
        for item in sq_off:
            print(f"    -> Squared off {item.get('action')} {item.get('qty')} {item.get('symbol')} @ {item.get('price')} (Ref: {item.get('order_ref')})")
        for f in failed:
            print(f"    -> ❌ Failed: {f}")
    except Exception as e:
        print(f"  ❌ Raw output [{c}]: {raw} | Error: {e}")
        panic_results[c] = {"error": raw}

# Also disable strategies in portal.db so they do not attempt re-entry
print("\n🔒 Disabling all SuperTrend strategies in portal.db to enforce permanent FLAT state...")
cmd_db = "sqlite3 /opt/xts_multi/portal/portal.db 'UPDATE tenant_supertrend_strategies SET is_enabled=0;'"
stdin, stdout, stderr = client.exec_command(cmd_db)
stdout.channel.recv_exit_status()
print("  All tenant_supertrend_strategies disabled (is_enabled=0).")

# Wait 6 seconds for exchange execution and settlement
print("\n⏳ Waiting 6 seconds for broker fills to settle...")
time.sleep(6)

# Post-panic verification audit
print("\n🔍 POST-PANIC VERIFICATION AUDIT:")
print("| Client | Broker NetWise Positions | Strategy Positions | Trading Paused |")
print("|:---|:---|:---|:---|")

for c, _ in clients:
    cmd_tel = f"docker exec xts_client_{c} curl -s http://127.0.0.1:8000/internal/telemetry"
    stdin, stdout, stderr = client.exec_command(cmd_tel)
    raw_tel = stdout.read().decode("utf-8").strip()
    if raw_tel:
        try:
            d = json.loads(raw_tel)
            pos_data = d.get("positions", {})
            pos = pos_data.get("positions", []) if isinstance(pos_data, dict) else []
            p_list = [f"{p.get('symbol')}: {p.get('quantity')}" for p in pos if int(p.get("quantity", 0)) != 0]
            strats = d.get("supertrend", {}).get("strategies", [])
            s_list = [f"{s.get('symbol')}({s.get('timeframe')}): {s.get('virtual_position')}" for s in strats if s.get("virtual_position", 0) != 0]
            paused = d.get("trading_paused", False)
            p_str = ", ".join(p_list) if p_list else "FLAT (0) ✅"
            s_str = ", ".join(s_list) if s_list else "FLAT (0) ✅"
            print(f"| {c} | {p_str} | {s_str} | {paused} |")
        except Exception as e:
            print(f"| {c} | Parse error: {e} | | |")
    else:
        print(f"| {c} | No response | | |")

client.close()
print("\n🏁 Global panic square-off sweep finished.")
