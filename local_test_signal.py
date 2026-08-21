#!/usr/bin/env python3
"""
🧪 XTS LOCAL WEBHOOK SIGNAL DISPATCHER
Simulates a TradingView Alert webhook fired directly at a specific local client instance.

Usage:
  python3 local_test_signal.py <client_id> <action> <symbol> <quantity> <price> [secret]

Examples:
  python3 local_test_signal.py c01_alpha BUY CRUDEOIL 1 6520.0
  python3 local_test_signal.py c02_beta SELL NATURALGAS 2 182.5
"""
import sys
import os
import requests
import json

# Try to find assigned port from local_data or default
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal"))
import docker_manager

def send_signal(tenant_id="c01_alpha", action="BUY", symbol="CRUDEOIL", quantity=1, price=6500.0, secret="Secret123"):
    port = docker_manager.get_tenant_port(tenant_id)
    url = f"http://127.0.0.1:{port}/webhook"

    payload = {
        "secret": secret,
        "action": action.upper(),
        "symbol": symbol.upper(),
        "quantity": int(quantity),
        "price": float(price)
    }

    print(f"\n📡 Dispatched Signal to Tenant [{tenant_id}] on Port {port}:")
    print(f"• URL     : {url}")
    print(f"• Payload : {json.dumps(payload, indent=2)}")

    try:
        resp = requests.post(url, json=payload, timeout=5)
        print(f"\n📥 Response (HTTP {resp.status_code}):")
        print(json.dumps(resp.json(), indent=2))
        print("\n✅ Check http://127.0.0.1:8500/admin/dashboard to see live position & MTM updates!")
    except Exception as e:
        print(f"\n❌ Error connecting to client on {url}: {e}")
        print("Make sure run_local.py is running in another terminal.")

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(__doc__)
        print("Executing default test signal: c01_alpha BUY CRUDEOIL 1 6500.0 Secret123")
        send_signal()
    else:
        tid = sys.argv[1]
        act = sys.argv[2]
        sym = sys.argv[3]
        qty = int(sys.argv[4])
        px = float(sys.argv[5])
        sec = sys.argv[6] if len(sys.argv) > 6 else "Secret123"
        send_signal(tid, act, sym, qty, px, sec)
