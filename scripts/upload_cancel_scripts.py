#!/usr/bin/env python3
import os
import sys
import paramiko

HOST = os.environ.get("VPS_HOST", "139.59.20.239")
USER = os.environ.get("VPS_USER", "root")
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")

CLIENTS = ["abk01", "abk02", "abk03", "abk04", "abk05", "abk06", "abk09", "abk10", "abk11"]

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    sftp = client.open_sftp()

    # 1. Upload master_auto_cancel.py
    local_master = "/Users/chinmayajain/Documents/antigravity/xts_platform_v2/scripts/master_auto_cancel.py"
    remote_master = "/opt/xts_multi/master_auto_cancel.py"
    print(f"Uploading {local_master} -> {remote_master}...")
    sftp.put(local_master, remote_master)
    sftp.chmod(remote_master, 0o755)

    # 2. Upload auto_cancel.py to all client directories
    local_cancel = "/Users/chinmayajain/Documents/antigravity/xts_platform_v2/scripts/auto_cancel.py"
    for c in CLIENTS:
        remote_c = f"/opt/xts_multi/data/{c}/auto_cancel.py"
        print(f"Uploading {local_cancel} -> {remote_c}...")
        sftp.put(local_cancel, remote_c)
        sftp.chmod(remote_c, 0o755)

    sftp.close()
    client.close()
    print("All files successfully uploaded!")

if __name__ == "__main__":
    main()
