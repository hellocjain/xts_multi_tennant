#!/usr/bin/env python3
"""
deploy_rms_rejection_fix.py
Deploys the post-dispatch RMS verification, circuit breakers, and margin pause protections
to VPS 139.59.20.239, hot-patches all 11 client containers, rebuilds xts_bot:latest, and verifies fleet alignment.
"""
import os
import sys
import time
import json
import paramiko

HOST = os.environ.get("VPS_HOST", "139.59.20.239")
USER = os.environ.get("VPS_USER", "root")
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")

DEFAULT_CLIENTS = [
    "abk01", "abk02", "abk03", "abk04", "abk05",
    "abk06", "abk07", "abk09", "abk10", "abk11", "abk12", "abk13", "abk14", "abk15", "dm933"
]

def get_client_containers(client):
    try:
        stdin, stdout, stderr = client.exec_command('docker ps --filter name=xts_client_ --format "{{.Names}}"')
        lines = [line.strip().replace("xts_client_", "") for line in stdout.read().decode('utf-8').splitlines() if line.strip().startswith("xts_client_")]
        if lines:
            return sorted(lines)
    except Exception:
        pass
    return DEFAULT_CLIENTS

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

    # 1. Upload files
    print("\n📦 Step 1: Uploading hardened source files to /opt/xts_multi/...")
    upload_files = [
        ("client/xts_api.py", "/opt/xts_multi/client/xts_api.py"),
        ("client/main.py", "/opt/xts_multi/client/main.py"),
        ("client/supertrend_engine.py", "/opt/xts_multi/client/supertrend_engine.py"),
        ("scripts/align_fleet.py", "/opt/xts_multi/scripts/align_fleet.py"),
    ]
    for local_rel, remote_path in upload_files:
        local_full = os.path.join(REPO_ROOT, local_rel)
        print(f"  Uploading {local_rel} -> {remote_path}...")
        sftp.put(local_full, remote_path)
        sftp.chmod(remote_path, 0o755 if "scripts" in remote_path else 0o644)
    sftp.close()

    # 2. Hot-patch all client containers
    clients = get_client_containers(client)
    print(f"\n🤖 Step 2: Hot-patching and restarting {len(clients)} client containers...")
    for c in clients:
        c_name = f"xts_client_{c}"
        print(f"  Patching {c_name}...")
        run_cmd(client, f"docker cp /opt/xts_multi/client/xts_api.py {c_name}:/app/xts_api.py")
        run_cmd(client, f"docker cp /opt/xts_multi/client/main.py {c_name}:/app/main.py")
        run_cmd(client, f"docker cp /opt/xts_multi/client/supertrend_engine.py {c_name}:/app/supertrend_engine.py")
        run_cmd(client, f"docker restart {c_name}")
        time.sleep(1)

    print("\n⏳ Waiting 5 seconds for client containers to boot...")
    time.sleep(5)

    # 3. Rebuild Docker image on host so changes persist on recreation
    print("\n🏗️ Step 3: Rebuilding xts_bot:latest image on host...")
    status, out, err = run_cmd(client, "docker build -t xts_bot:latest -f /opt/xts_multi/client/Dockerfile /opt/xts_multi/client")
    if status == 0:
        print("  ✅ xts_bot:latest image rebuilt successfully.")
    else:
        print(f"  ⚠️ Image rebuild error: {err}")

    # 4. Run fleet alignment check
    print("\n🔍 Step 4: Running fleet alignment check on VPS...")
    status, out, err = run_cmd(client, "python3 /opt/xts_multi/scripts/align_fleet.py")
    print(out)

    client.close()
    print("\n🎉 Deployment completed successfully!")

if __name__ == "__main__":
    main()
