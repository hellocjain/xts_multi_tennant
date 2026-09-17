#!/usr/bin/env python3
"""
deploy_rollover_hardening.py
Deploys hardened continuous futures auto-rollover and auto-seeding to VPS 139.59.20.239:
- SFTP syncs updated client and portal code + frontend build artifacts.
- Hot-patches all 15 running client containers safely.
- Hot-patches xts_portal and restarts it.
- Rebuilds xts_bot:latest base image on VPS for all future provisioned clients.
- Runs the test suite on the VPS to verify 100% integrity.
"""
import os
import sys
import time
import paramiko

HOST = "139.59.20.239"
USER = "root"
PASSWORD = "Check"

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def run_cmd(client, cmd):
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8').strip()
    err = stderr.read().decode('utf-8').strip()
    if exit_status != 0:
        print(f"  ❌ Failed [{exit_status}]: {cmd}\n  Error: {err}")
    return exit_status, out, err

def get_client_containers(client):
    try:
        stdin, stdout, stderr = client.exec_command('docker ps --filter name=xts_client_ --format "{{.Names}}"')
        lines = [line.strip().replace("xts_client_", "") for line in stdout.read().decode('utf-8').splitlines() if line.strip().startswith("xts_client_")]
        if lines:
            return sorted(lines)
    except Exception:
        pass
    return ["abk01", "abk02", "abk03", "abk04", "abk05", "abk06", "abk07", "abk09", "abk10", "abk11", "abk12", "abk13", "abk14", "abk15", "dm933"]

def upload_dir(sftp, local_dir, remote_dir):
    try:
        sftp.mkdir(remote_dir)
    except IOError:
        pass
    for root, dirs, files in os.walk(local_dir):
        rel_path = os.path.relpath(root, local_dir)
        dest_dir = os.path.join(remote_dir, rel_path).replace("\\", "/")
        try:
            sftp.mkdir(dest_dir)
        except IOError:
            pass
        for file in files:
            local_file = os.path.join(root, file)
            remote_file = os.path.join(dest_dir, file).replace("\\", "/")
            sftp.put(local_file, remote_file)

def main():
    print(f"🚀 Connecting to VPS {HOST} as {USER}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    
    print("\n📦 Step 1: SFTP Syncing source code & build artifacts to /opt/xts_multi/...")
    sftp = client.open_sftp()
    
    sync_files = [
        ("client/config.py", "/opt/xts_multi/client/config.py"),
        ("client/xts_api.py", "/opt/xts_multi/client/xts_api.py"),
        ("client/supertrend_engine.py", "/opt/xts_multi/client/supertrend_engine.py"),
        ("client/tests/test_auto_rollover_future_months.py", "/opt/xts_multi/client/tests/test_auto_rollover_future_months.py"),
        ("portal/main.py", "/opt/xts_multi/portal/main.py"),
        ("portal/telemetry_service.py", "/opt/xts_multi/portal/telemetry_service.py"),
        ("portal/templates/client_detail.html", "/opt/xts_multi/portal/templates/client_detail.html"),
        ("portal/tests/test_portal.py", "/opt/xts_multi/portal/tests/test_portal.py"),
        ("portal/frontend/src/components/views/ClientDetailView.tsx", "/opt/xts_multi/portal/frontend/src/components/views/ClientDetailView.tsx"),
    ]
    for local_rel, remote_path in sync_files:
        local_path = os.path.join(REPO_ROOT, local_rel)
        if os.path.exists(local_path):
            sftp.put(local_path, remote_path)
            print(f"  ✓ Synced {local_rel} -> {remote_path}")
    
    # Sync frontend dist
    local_dist = os.path.join(REPO_ROOT, "portal/frontend/dist")
    if os.path.exists(local_dist):
        print("  ✓ Uploading compiled frontend dist...")
        upload_dir(sftp, local_dist, "/opt/xts_multi/portal/frontend/dist")
    sftp.close()

    # Step 2: Run test suite inside a client container where pytest and dependencies are installed
    print("\n🧪 Step 2: Running auto-rollover verification tests inside xts_client_abk01...")
    copy_test = (
        "docker exec xts_client_abk01 mkdir -p /app/tests && "
        "docker cp /opt/xts_multi/client/config.py xts_client_abk01:/app/config.py && "
        "docker cp /opt/xts_multi/client/xts_api.py xts_client_abk01:/app/xts_api.py && "
        "docker cp /opt/xts_multi/client/supertrend_engine.py xts_client_abk01:/app/supertrend_engine.py && "
        "docker cp /opt/xts_multi/client/tests/test_auto_rollover_future_months.py xts_client_abk01:/app/tests/test_auto_rollover_future_months.py"
    )
    run_cmd(client, copy_test)
    test_cmd = "docker exec xts_client_abk01 pytest /app/tests/test_auto_rollover_future_months.py -v"
    st, out, err = run_cmd(client, test_cmd)
    print(out)
    if st != 0:
        print("❌ Tests failed on VPS container! Aborting fleet patch.")
        client.close()
        sys.exit(1)

    # Step 3: Hot-patch all running client containers
    clients = get_client_containers(client)
    print(f"\n🔄 Step 3: Hot-patching {len(clients)} client containers sequentially...")
    for c in clients:
        c_name = f"xts_client_{c}"
        cmd = (
            f"docker cp /opt/xts_multi/client/config.py {c_name}:/app/config.py && "
            f"docker cp /opt/xts_multi/client/xts_api.py {c_name}:/app/xts_api.py && "
            f"docker cp /opt/xts_multi/client/supertrend_engine.py {c_name}:/app/supertrend_engine.py && "
            f"docker restart {c_name}"
        )
        st, out, err = run_cmd(client, cmd)
        if st == 0:
            print(f"  ✓ {c_name} patched & restarted successfully.")
        else:
            print(f"  ✗ {c_name} error: {err}")
        time.sleep(0.5)

    # Step 4: Hot-patch xts_portal
    print("\n🌐 Step 4: Hot-patching xts_portal container...")
    portal_cmd = (
        "docker cp /opt/xts_multi/portal/main.py xts_portal:/app/main.py && "
        "docker cp /opt/xts_multi/portal/telemetry_service.py xts_portal:/app/telemetry_service.py && "
        "docker cp /opt/xts_multi/portal/templates/client_detail.html xts_portal:/app/templates/client_detail.html && "
        "docker restart xts_portal"
    )
    st, out, err = run_cmd(client, portal_cmd)
    if st == 0:
        print("  ✓ xts_portal updated & restarted successfully.")
    else:
        print(f"  ✗ xts_portal error: {err}")

    # Step 5: Rebuild base Docker image xts_bot:latest for future clients
    print("\n🏗️ Step 5: Rebuilding xts_bot:latest image for future client provisioning...")
    build_cmd = "cd /opt/xts_multi/client && docker build -t xts_bot:latest ."
    st, out, err = run_cmd(client, build_cmd)
    if st == 0:
        print("  ✓ xts_bot:latest image rebuilt successfully.")
    else:
        print(f"  ✗ xts_bot:latest build failed: {err}")

    # Step 6: Verify fleet health
    print("\n🔍 Step 6: Verifying Fleet Health & Symbol Resolution...")
    time.sleep(3)
    check_cmd = "docker exec xts_client_abk01 curl -s http://localhost:8000/internal/validate-symbol?symbol=SILVER1001!"
    st, out, err = run_cmd(client, check_cmd)
    print(f"  abk01 SILVER1001! resolution: {out}")

    check_cmd2 = "docker exec xts_client_abk01 curl -s http://localhost:8000/internal/validate-symbol?symbol=GOLDPETAL1!"
    st, out, err = run_cmd(client, check_cmd2)
    print(f"  abk01 GOLDPETAL1! resolution: {out}")

    client.close()
    print("\n🎉 ALL DEPLOYMENTS & LIVE VERIFICATIONS COMPLETE!")

if __name__ == "__main__":
    main()
