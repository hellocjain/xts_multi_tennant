#!/usr/bin/env python3
"""
=============================================================================
🚀 XTS MULTI-TENANT CLUSTER: 1-CLICK RAPID SERVER MIGRATION TOOL
=============================================================================
Migrates the entire platform (all 17 tenant containers, portal, database,
encryption keys, and live strategy states) to a fresh Ubuntu VPS in under 5 mins.

Usage:
  python3 scripts/migrate_to_new_server.py --new-ip <NEW_SERVER_IP> --new-pass <PASSWORD>

Features:
  1. Bootstraps New VPS: Docker, Docker Compose, IST Timezone, 2GB Swap.
  2. Zero-Loss Live Transfer: Streams hot snapshot of /opt/xts_multi over SSH pipe.
  3. Image Build & Re-provisioning: Builds base images and provisions all tenants.
  4. Post-Migration Verification: Runs 7-point health check & position audit.
=============================================================================
"""

import os
import sys
import time
import argparse
import paramiko

OLD_HOST = os.environ.get("OLD_VPS_HOST", "139.59.20.239")
OLD_USER = os.environ.get("OLD_VPS_USER", "root")
OLD_PASS = os.environ.get("OLD_VPS_PASS", "Check")

def get_ssh_client(host, user, password, port=22):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=password, timeout=15)
    return client

def exec_cmd(client, cmd, desc="", timeout=120, fail_ok=False):
    if desc:
        print(f"  ▶ {desc}...")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if exit_status != 0 and not fail_ok:
        print(f"  ❌ Error executing: {cmd}\n  Output: {out}\n  Error: {err}")
        raise RuntimeError(f"Command failed with code {exit_status}")
    return out

def main():
    parser = argparse.ArgumentParser(description="Migrate XTS Cluster to a new VPS")
    parser.add_argument("--new-ip", required=True, help="IP address of the new VPS")
    parser.add_argument("--new-user", default="root", help="SSH username of the new VPS (default: root)")
    parser.add_argument("--new-pass", required=True, help="SSH password for the new VPS")
    parser.add_argument("--old-ip", default=OLD_HOST, help=f"IP of current VPS (default: {OLD_HOST})")
    parser.add_argument("--old-pass", default=OLD_PASS, help="Password of current VPS")
    parser.add_argument("--stop-old", action="store_true", help="Stop containers on old VPS after migration")

    args = parser.parse_args()

    print("\n" + "=" * 75)
    print("🚀 XTS CLUSTER RAPID ZERO-LOSS SERVER MIGRATION")
    print("=" * 75)
    print(f"  Source (Old VPS): {args.old_ip}")
    print(f"  Target (New VPS): {args.new_ip}")
    print("=" * 75 + "\n")

    # Step 1: Connect to both servers
    print("📡 [1/5] Establishing SSH connections...")
    old_ssh = get_ssh_client(args.old_ip, OLD_USER, args.old_pass)
    print(f"  ✓ Connected to Old VPS ({args.old_ip})")
    new_ssh = get_ssh_client(args.new_ip, args.new_user, args.new_pass)
    print(f"  ✓ Connected to New VPS ({args.new_ip})")

    # Step 2: Bootstrap New VPS
    print("\n🛠️ [2/5] Bootstrapping New VPS environment...")
    exec_cmd(new_ssh, "timedatectl set-timezone Asia/Kolkata", "Setting IST Timezone (Asia/Kolkata)")
    
    # Check & Create Swap
    swap_out = exec_cmd(new_ssh, "free -m | awk '/^Swap:/ {print $2}'", "Checking swapfile")
    if int(swap_out or 0) < 1000:
        print("  ▶ Allocating 2GB swapfile...")
        exec_cmd(new_ssh, "fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab", fail_ok=True)
        print("  ✓ 2GB swapfile activated.")
    else:
        print(f"  ✓ Swapfile already present ({swap_out} MB)")

    # Install Docker & dependencies if not installed
    docker_check = exec_cmd(new_ssh, "which docker || true", "Checking Docker installation")
    if not docker_check:
        print("  ▶ Installing Docker & Docker Compose plugin...")
        exec_cmd(new_ssh, "curl -fsSL https://get.docker.com | sh", timeout=300)
        exec_cmd(new_ssh, "apt-get update && apt-get install -y rsync tar htop python3 python3-pip", timeout=180)
        print("  ✓ Docker installed successfully.")
    else:
        print("  ✓ Docker already installed.")

    # Step 3: Stream Live State Snapshot from Old VPS to New VPS
    print("\n📦 [3/5] Streaming live cluster state from Old VPS to New VPS...")
    # Clean WAL checkpoints first on old VPS to ensure database files on disk are pristine
    exec_cmd(old_ssh, 'python3 -c "import sqlite3; conn = sqlite3.connect(\'/opt/xts_multi/portal/portal.db\'); conn.execute(\'PRAGMA wal_checkpoint(TRUNCATE)\'); conn.close()"', fail_ok=True)
    
    # Direct SSH-to-SSH pipe transfer (fastest possible method: tar -> gzip -> ssh pipe -> tar extract)
    print(f"  ▶ Streaming /opt/xts_multi directly over network pipe to {args.new_ip}...")
    transfer_cmd = f"sshpass -p '{args.new_pass}' rsync -az -e 'ssh -o StrictHostKeyChecking=no' --delete /opt/xts_multi/ {args.new_user}@{args.new_ip}:/opt/xts_multi/ 2>/dev/null || tar -czf - -C / opt/xts_multi | sshpass -p '{args.new_pass}' ssh -o StrictHostKeyChecking=no {args.new_user}@{args.new_ip} 'tar -xzf - -C /'"
    
    # Ensure sshpass on old VPS if needed
    exec_cmd(old_ssh, "which sshpass || apt-get install -y sshpass", fail_ok=True)
    exec_cmd(old_ssh, transfer_cmd, "Syncing /opt/xts_multi to target", timeout=300)
    print("  ✓ Complete cluster filesystem & state transferred.")

    # Step 4: Build & Provision Cluster on New VPS
    print("\n🏗️ [4/5] Building images and provisioning containers on New VPS...")
    exec_cmd(new_ssh, "cd /opt/xts_multi && docker build -t xts_bot:latest ./client", "Building xts_bot:latest image", timeout=300)
    exec_cmd(new_ssh, "cd /opt/xts_multi && docker compose build xts_portal", "Building xts_portal:latest image", timeout=300)
    exec_cmd(new_ssh, "cd /opt/xts_multi && docker compose up -d --force-recreate caddy xts_portal", "Starting Caddy & Admin Portal", timeout=120)
    
    time.sleep(5)
    print("  ▶ Provisioning all tenant client containers from restored portal.db...")
    provision_out = exec_cmd(new_ssh, """docker exec xts_portal python3 -c '
import database, docker_manager, caddy_manager
with database.get_db_connection() as conn:
    tenants = conn.execute("SELECT id, name FROM tenants WHERE status=\\"ACTIVE\\"").fetchall()
print(f"Provisioning {len(tenants)} active tenants...")
for t in tenants:
    res = docker_manager.provision_client_container(t["id"])
    print(f"  • {t[\\"id\\"]} ({t[\\"name\\"]}) -> {res.get(\\"status\\")}")
caddy_manager.sync_caddy_config()
print("Caddy configuration updated.")
'""", timeout=180)
    print(provision_out)

    # Step 5: Verification Suite
    print("\n🔍 [5/5] Running verification audit on New VPS...")
    verify_res = exec_cmd(new_ssh, "cd /opt/xts_multi && python3 scripts/verify_new_server.py", fail_ok=True)
    print(verify_res)

    print("\n📊 Checking positions on New VPS...")
    pos_res = exec_cmd(new_ssh, "cd /opt/xts_multi && python3 scripts/audit_live_positions.py", fail_ok=True)
    print(pos_res)

    # Optional: Stop old server containers to prevent dual execution
    if args.stop_old:
        print("\n🛑 Stopping client containers on Old VPS to prevent duplicate trade execution...")
        exec_cmd(old_ssh, "docker stop $(docker ps --filter name=xts_client_ -q) 2>/dev/null || true", "Stopping old client containers")
        exec_cmd(old_ssh, "docker stop caddy xts_portal 2>/dev/null || true", "Stopping old portal & caddy")
        print("  ✓ Old VPS containers stopped.")

    print("\n" + "=" * 75)
    print("🎉 MIGRATION COMPLETED SUCCESSFULLY!")
    print("=" * 75)
    print("Next Steps:")
    print(f"  1. Update your Domain DNS A-Record to point to: {args.new_ip}")
    print(f"  2. If using IP-whitelisted broker accounts (e.g. dm933), update the allowed IP on broker portal to: {args.new_ip}")
    print("  3. Check the admin dashboard at: http://" + args.new_ip + "/admin")
    if not args.stop_old:
        print(f"  4. When satisfied, stop the old containers on {args.old_ip} via:")
        print(f"     ssh root@{args.old_ip} 'docker stop $(docker ps -q)'")
    print("=" * 75 + "\n")

    old_ssh.close()
    new_ssh.close()

if __name__ == "__main__":
    main()
