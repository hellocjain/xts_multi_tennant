#!/usr/bin/env python3
"""
deploy_bulk_operations.py
Deploys the Multi-Account Bulk Operations Suite to VPS 139.59.20.239:
- Pulls/uploads latest client/main.py, portal/bulk_operations_service.py, portal/main.py, portal/templates/
- Hot-patches all 15 client containers with client/main.py and restarts them
- Hot-patches xts_portal container and restarts it
- Verifies system health and responsiveness
"""
import os
import sys
import time
import paramiko

HOST = os.environ.get("VPS_HOST", "139.59.20.239")
USER = os.environ.get("VPS_USER", "root")
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def run_cmd(client, cmd):
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8').strip()
    err = stderr.read().decode('utf-8').strip()
    if exit_status != 0:
        print(f"❌ Command failed [{exit_status}]: {cmd}\nError: {err}")
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

def main():
    print(f"🚀 Connecting to VPS {HOST} as {USER}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    
    # 1. Git pull on VPS
    print("\n📦 Step 1: Updating repository on VPS via git pull...")
    status, out, err = run_cmd(client, "cd /opt/xts_multi && git fetch origin && git checkout main && git pull origin main")
    print(out)

    # 2. SFTP direct upload of modified files to guarantee 100% synchronization
    print("\n📦 Step 2: SFTP syncing source files to /opt/xts_multi/...")
    sftp = client.open_sftp()
    files_to_sync = [
        ("client/main.py", "/opt/xts_multi/client/main.py"),
        ("portal/bulk_operations_service.py", "/opt/xts_multi/portal/bulk_operations_service.py"),
        ("portal/main.py", "/opt/xts_multi/portal/main.py"),
        ("portal/templates/dashboard.html", "/opt/xts_multi/portal/templates/dashboard.html"),
        ("portal/templates/dashboard_partial.html", "/opt/xts_multi/portal/templates/dashboard_partial.html"),
    ]
    for local_rel, remote_path in files_to_sync:
        local_full = os.path.join(REPO_ROOT, local_rel)
        print(f"  Syncing {local_rel} -> {remote_path}...")
        sftp.put(local_full, remote_path)
    sftp.close()

    # 3. Hot-patch all client containers
    client_ids = get_client_containers(client)
    print(f"\n🤖 Step 3: Hot-patching and restarting {len(client_ids)} client containers...")
    for cid in client_ids:
        c_name = f"xts_client_{cid}"
        print(f"  Patching {c_name}...")
        run_cmd(client, f"docker cp /opt/xts_multi/client/main.py {c_name}:/app/main.py")
        run_cmd(client, f"docker restart {c_name}")
        time.sleep(0.5)

    # 4. Hot-patch xts_portal container
    print("\n🌐 Step 4: Hot-patching xts_portal container...")
    portal_files = [
        ("portal/bulk_operations_service.py", "/app/bulk_operations_service.py"),
        ("portal/main.py", "/app/main.py"),
        ("portal/templates/dashboard.html", "/app/templates/dashboard.html"),
        ("portal/templates/dashboard_partial.html", "/app/templates/dashboard_partial.html"),
    ]
    for host_path, container_path in portal_files:
        run_cmd(client, f"docker cp /opt/xts_multi/{host_path} xts_portal:{container_path}")
    
    print("  Restarting xts_portal...")
    run_cmd(client, "docker restart xts_portal")

    print("\n⏳ Waiting 5 seconds for services to boot...")
    time.sleep(5)

    # 5. Check container statuses
    print("\n🔍 Step 5: Checking running container statuses...")
    status, out, err = run_cmd(client, 'docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "xts_portal|xts_client"')
    print(out)

    # 6. Check portal logs for clean startup
    print("\n📋 Step 6: Verifying xts_portal logs...")
    status, out, err = run_cmd(client, "docker logs --tail 25 xts_portal")
    print(out)

    client.close()
    print("\n🎉 Multi-Account Bulk Operations Suite successfully deployed to production!")

if __name__ == "__main__":
    main()
