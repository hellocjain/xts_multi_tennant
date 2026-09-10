#!/usr/bin/env python3
import os
import sys
import time
import json
import paramiko

HOST = os.environ.get("VPS_HOST", "139.59.20.239")
USER = os.environ.get("VPS_USER", "root")
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")

CLIENTS = [
    "abk01", "abk02", "abk03", "abk04", "abk05",
    "abk06", "abk09", "abk10", "abk11", "abk12", "dm933"
]

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def run_cmd(client, cmd):
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8').strip()
    err = stderr.read().decode('utf-8').strip()
    if exit_status != 0:
        print(f"❌ Command failed [{exit_status}]: {cmd}\nError: {err}")
    return exit_status, out, err

def main():
    print(f"🚀 Connecting to VPS {HOST} as {USER}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = client.open_sftp()

    # 1. Upload hardened files to host
    print("\n📦 Step 1: Uploading hardened source files to /opt/xts_multi/...")
    upload_files = [
        ("client/xts_api.py", "/opt/xts_multi/client/xts_api.py"),
        ("client/supertrend_engine.py", "/opt/xts_multi/client/supertrend_engine.py"),
        ("portal/main.py", "/opt/xts_multi/portal/main.py"),
    ]
    for local_rel, remote_path in upload_files:
        local_full = os.path.join(REPO_ROOT, local_rel)
        print(f"  Uploading {local_rel} -> {remote_path}...")
        sftp.put(local_full, remote_path)
        sftp.chmod(remote_path, 0o644)
    sftp.close()

    # 2. Hot-patch portal
    print("\n🌐 Step 2: Hot-patching and restarting xts_portal...")
    run_cmd(client, "docker cp /opt/xts_multi/portal/main.py xts_portal:/app/main.py")
    run_cmd(client, "docker restart xts_portal")
    time.sleep(3)
    print("  xts_portal updated and restarted.")

    # 3. Hot-patch all client containers
    print("\n🤖 Step 3: Hot-patching and restarting 11 client containers...")
    for c in CLIENTS:
        c_name = f"xts_client_{c}"
        print(f"  Patching {c_name}...")
        run_cmd(client, f"docker cp /opt/xts_multi/client/xts_api.py {c_name}:/app/xts_api.py")
        run_cmd(client, f"docker cp /opt/xts_multi/client/supertrend_engine.py {c_name}:/app/supertrend_engine.py")
        run_cmd(client, f"docker restart {c_name}")
        time.sleep(1.5)

    # 4. Rebuild base docker image
    print("\n🔨 Step 4: Rebuilding base Docker image (xts_bot:latest) on host...")
    status, out, err = run_cmd(client, "docker build -t xts_bot:latest -f /opt/xts_multi/Dockerfile.client /opt/xts_multi")
    if status == 0:
        print("  Base image xts_bot:latest rebuilt successfully.")
    else:
        print(f"  ⚠️ Rebuild returned status {status}: {err}")

    # 5. Verification: Check health & telemetry across all containers
    print("\n🔍 Step 5: Verifying container health and telemetry...")
    time.sleep(5)
    for c in CLIENTS:
        c_name = f"xts_client_{c}"
        status, out, err = run_cmd(client, f"docker exec {c_name} curl -s http://127.0.0.1:8000/internal/telemetry")
        if status == 0 and out:
            try:
                data = json.loads(out)
                st_data = data.get("supertrend", {})
                strats = st_data.get("strategies", [])
                strat_summaries = [f"{s.get('symbol')} ({s.get('timeframe')}) pos={s.get('virtual_position')}" for s in strats]
                print(f"  ✅ {c_name}: Status={data.get('status')} | Strategies={len(strats)} | {', '.join(strat_summaries) if strat_summaries else 'No active strats'}")
            except Exception as e:
                print(f"  ⚠️ {c_name}: JSON parse error: {e} | Raw: {out[:100]}")
        else:
            print(f"  ❌ {c_name}: Failed to get telemetry (Code {status})")

    client.close()
    print("\n🎉 Deployment completed successfully!")

if __name__ == "__main__":
    main()
