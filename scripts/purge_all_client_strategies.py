#!/usr/bin/env python3
"""
Purge All Client Trading Strategies
----------------------------------
1. Deletes all records from `tenant_supertrend_strategies`, `tenant_supertrend_configs`,
   and `tenant_custom_strategies` in portal.db on the VPS.
2. Clears `strategy_virtual_positions` across all client SQLite databases.
3. Inserts an immutable audit record in `audit_logs`.
4. Restarts all 11 client containers and xts_portal for a completely clean boot.
"""
import os
import sys
import time
import json
import paramiko

HOST = "139.59.20.239"
USER = "root"
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")

def main():
    print(f"🚀 Connecting to VPS {HOST} as {USER}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=25)

    purge_script = '''
import sqlite3
import glob
import os
import time
import json

portal_db = "/opt/xts_multi/portal/portal.db"
if not os.path.exists(portal_db):
    portal_db = "/opt/xts_multi/data/portal.db"

print(f"📦 Connecting to Portal Database at {portal_db}...")
conn = sqlite3.connect(portal_db)
c = conn.cursor()

# 1. Count before deletion
c_st = c.execute("SELECT count(*) FROM tenant_supertrend_strategies").fetchone()[0]
c_cfg = c.execute("SELECT count(*) FROM tenant_supertrend_configs").fetchone()[0]
c_cust = c.execute("SELECT count(*) FROM tenant_custom_strategies").fetchone()[0]
print(f"Current Counts: SuperTrend Strategies={c_st}, SuperTrend Configs={c_cfg}, Custom Strategies={c_cust}")

# 2. Delete all strategies
c.execute("DELETE FROM tenant_supertrend_strategies")
c.execute("DELETE FROM tenant_supertrend_configs")
c.execute("DELETE FROM tenant_custom_strategies")

# 3. Add audit log entry
now = time.time()
c.execute(
    "INSERT INTO audit_logs (timestamp, actor, action, target_tenant_id, details_json) VALUES (?, ?, ?, ?, ?)",
    (now, "admin", "PURGE_ALL_STRATEGIES", "GLOBAL", json.dumps({"deleted_supertrend": c_st, "deleted_custom": c_cust}))
)
conn.commit()
c.execute("VACUUM")
conn.close()
print("✅ Portal database strategies purged and vacuumed successfully.")

# 4. Clear client SQLite databases
print("\\n🧹 Cleaning client container SQLite virtual position records...")
for db_path in glob.glob("/opt/xts_multi/data/*/*.db") + glob.glob("/opt/xts_multi/data/*.db"):
    if "portal.db" in db_path:
        continue
    try:
        c_client = sqlite3.connect(db_path)
        tables = [r[0] for r in c_client.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "strategy_virtual_positions" in tables:
            c_client.execute("DELETE FROM strategy_virtual_positions")
            c_client.commit()
            print(f"  Truncated strategy_virtual_positions in {db_path}")
        c_client.close()
    except Exception as e:
        print(f"  Notice for {db_path}: {e}")
'''

    sftp = client.open_sftp()
    with sftp.file("/tmp/run_purge.py", "w") as f:
        f.write(purge_script)
    sftp.close()

    print("\n⚡ Executing strategy purge on VPS...")
    stdin, stdout, stderr = client.exec_command("python3 /tmp/run_purge.py")
    out = stdout.read().decode("utf-8")
    err = stderr.read().decode("utf-8")
    print(out)
    if err:
        print("STDERR:", err)

    print("\n🔄 Restarting xts_portal and all 11 client containers...")
    restart_cmd = """
docker restart xts_portal
for c in $(docker ps --filter "name=xts_client_" --format "{{.Names}}"); do
    echo "Restarting $c..."
    docker restart $c
done
"""
    stdin, stdout, stderr = client.exec_command(restart_cmd)
    print(stdout.read().decode("utf-8"))

    print("⏳ Waiting 10 seconds for containers to stabilize...")
    time.sleep(10)

    print("🔍 Checking container statuses...")
    stdin, stdout, stderr = client.exec_command("docker ps --format 'table {{.Names}}\t{{.Status}}'")
    print(stdout.read().decode("utf-8"))

    client.close()
    print("🎉 Purge execution finished!")

if __name__ == "__main__":
    main()
