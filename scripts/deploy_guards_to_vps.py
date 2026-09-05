#!/usr/bin/env python3
import os
import sys
import paramiko

HOST = os.environ.get("VPS_HOST", "139.59.20.239")
USER = os.environ.get("VPS_USER", "root")
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")

CLIENTS = ["abk01", "abk02", "abk03", "abk04", "abk05", "abk06", "abk09", "abk10", "abk11", "dm933"]

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    sftp = client.open_sftp()

    # 1. Upload files
    files = [
        ("client/config.py", "/opt/xts_multi/client/config.py"),
        ("client/xts_api.py", "/opt/xts_multi/client/xts_api.py"),
        ("scripts/monday_premarket_sanity_check.py", "/opt/xts_multi/monday_premarket_sanity_check.py")
    ]
    for local_f, remote_f in files:
        full_local = os.path.join("/Users/chinmayajain/Documents/antigravity/xts_platform_v2", local_f)
        print(f"Uploading {full_local} -> {remote_f}...")
        sftp.put(full_local, remote_f)
        sftp.chmod(remote_f, 0o755)

    sftp.close()

    # 2. Hot-patch all running client containers
    print("\nHot-patching all client containers...")
    for c in CLIENTS:
        cmd = f"docker cp /opt/xts_multi/client/config.py xts_client_{c}:/app/config.py && docker cp /opt/xts_multi/client/xts_api.py xts_client_{c}:/app/xts_api.py"
        stdin, stdout, stderr = client.exec_command(cmd)
        stdout.channel.recv_exit_status()
        print(f"  Container xts_client_{c} patched.")

    # 3. Rebuild Docker image on host so changes persist on any recreation
    print("\nRebuilding xts_bot:latest image on host...")
    stdin, stdout, stderr = client.exec_command("docker build -t xts_bot:latest -f /opt/xts_multi/Dockerfile.client /opt/xts_multi")
    stdout.channel.recv_exit_status()
    print("Docker image xts_bot:latest rebuilt successfully.")

    # 4. Run premarket sanity check
    print("\nRunning Monday Pre-Market Sanity Check on VPS...")
    stdin, stdout, stderr = client.exec_command("python3 /opt/xts_multi/monday_premarket_sanity_check.py")
    out = stdout.read().decode('utf-8')
    print(out)

    client.close()

if __name__ == "__main__":
    main()
