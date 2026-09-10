import os
import sys
import time
import paramiko

HOST = "139.59.20.239"
USER = "root"
PASSWORD = os.environ.get("VPS_PASSWORD", "Check")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

print(f"🚀 Connecting to VPS {HOST} as {USER}...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = client.open_sftp()

print("\n📦 Uploading updated portal files to /opt/xts_multi/portal/...")
files = [
    ("portal/main.py", "/opt/xts_multi/portal/main.py"),
    ("portal/templates/login.html", "/opt/xts_multi/portal/templates/login.html")
]

for local_rel, remote_path in files:
    local_full = os.path.join(REPO_ROOT, local_rel)
    print(f"  Uploading {local_rel} -> {remote_path}...")
    sftp.put(local_full, remote_path)
    sftp.chmod(remote_path, 0o644)

sftp.close()

print("\n🌐 Hot-patching and restarting xts_portal container...")
commands = [
    "docker cp /opt/xts_multi/portal/main.py xts_portal:/app/main.py",
    "docker cp /opt/xts_multi/portal/templates/login.html xts_portal:/app/templates/login.html",
    "docker restart xts_portal"
]

for cmd in commands:
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    print(f"  Ran [{exit_code}]: {cmd}")

print("\n⏳ Waiting 4 seconds for xts_portal to initialize...")
time.sleep(4)

print("\n🔍 Verifying portal health...")
stdin, stdout, stderr = client.exec_command("docker ps --format 'table {{.Names}}\t{{.Status}}' | grep xts_portal")
print("  " + stdout.read().decode('utf-8').strip())

print("\n🧪 Testing login directly on host with curl...")
test_cmd = """curl -i -s -X POST http://127.0.0.1:8500/admin/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=AdminPass123!" | head -n 15"""
stdin, stdout, stderr = client.exec_command(test_cmd)
output = stdout.read().decode('utf-8').strip()
print("  Response from /admin/login:")
for line in output.split('\n'):
    print(f"    {line}")

client.close()
print("\n🎉 Deployment completed successfully!")
