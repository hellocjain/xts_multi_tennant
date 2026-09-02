#!/usr/bin/env python3
import sys
import paramiko

def run_remote_command(cmd, host="168.144.72.117", user="root", password="Check"):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=user, password=password, timeout=15)
        stdin, stdout, stderr = client.exec_command(cmd)
        out = stdout.read().decode()
        err = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        client.close()
        if out:
            print(out, end="")
        if err:
            print(err, end="", file=sys.stderr)
        return code
    except Exception as e:
        print(f"SSH Error: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/remote_ssh.py '<command>'")
        sys.exit(1)
    cmd = " ".join(sys.argv[1:])
    code = run_remote_command(cmd)
    sys.exit(code)
