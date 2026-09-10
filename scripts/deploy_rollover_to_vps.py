#!/usr/bin/env python3
import sys
import paramiko

HOST = "139.59.20.239"
USER = "root"
PASSWORD = "Check"

CLIENTS = ["abk01", "abk02", "abk03", "abk04", "abk05", "abk06", "abk09", "abk10", "abk11", "abk12", "dm933"]

def run_ssh(client, cmd):
    stdin, stdout, stderr = client.exec_command(cmd)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode()
    err = stderr.read().decode()
    return code, out, err

def main():
    print("=== CONNECTING TO VPS TO DEPLOY ROLLOVER HARDENING ===")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)

    # 1. Hot-patch all 11 running client containers
    print("\n1. Hot-patching all 11 running client containers...")
    for c in CLIENTS:
        container = f"xts_client_{c}"
        cmd = (
            f"docker cp /opt/xts_multi/client/config.py {container}:/app/config.py && "
            f"docker cp /opt/xts_multi/client/main.py {container}:/app/main.py && "
            f"docker cp /opt/xts_multi/client/supertrend_engine.py {container}:/app/supertrend_engine.py && "
            f"docker cp /opt/xts_multi/client/xts_api.py {container}:/app/xts_api.py && "
            f"docker restart {container}"
        )
        code, out, err = run_ssh(client, cmd)
        if code == 0:
            print(f"  ✓ {container} patched & restarted.")
        else:
            print(f"  ✗ {container} failed: {err}")

    # 2. Rebuild xts_bot:latest image on host
    print("\n2. Rebuilding xts_bot:latest image on host for future clients...")
    code, out, err = run_ssh(client, "cd /opt/xts_multi/client && docker build -t xts_bot:latest .")
    if code == 0:
        print("  ✓ Docker image xts_bot:latest rebuilt successfully.")
    else:
        print(f"  ✗ Image build failed: {err}")

    # 3. Live verification of symbol resolution
    print("\n3. Live contract resolution check on xts_client_abk01...")
    symbols_to_test = ["GOLDPETAL1!", "SILVER1001!", "GOLD1!", "SILVER1!", "CRUDEOIL1!"]
    for sym in symbols_to_test:
        test_cmd = f"docker exec xts_client_abk01 curl -s http://localhost:8000/internal/validate-symbol?symbol={sym}"
        code, out, err = run_ssh(client, test_cmd)
        print(f"  {sym} -> {out.strip()}")

    client.close()
    print("\n=== DEPLOYMENT AND VERIFICATION COMPLETE ===")

if __name__ == "__main__":
    main()
