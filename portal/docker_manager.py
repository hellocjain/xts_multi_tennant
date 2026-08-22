import os
import sys
import json
import logging
import subprocess
import time
import socket
import threading
from contextlib import closing
from database import get_db_connection, get_portal_data_dir
import security

logger = logging.getLogger(__name__)

def get_client_data_root():
    if "CLIENT_DATA_ROOT" in os.environ:
        return os.environ["CLIENT_DATA_ROOT"]
    candidate_opt = "/opt/xts_multi/data"
    if os.path.exists(candidate_opt):
        return candidate_opt
    candidate_local = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "local_data", "data"))
    if os.path.exists(candidate_local):
        return candidate_local
    candidate_parent = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
    if os.path.exists(candidate_parent):
        return candidate_parent
    return os.path.join(get_portal_data_dir(), "data")

CLIENT_IMAGE = os.environ.get("CLIENT_IMAGE", "xts_bot:latest")
INGRESS_NETWORK = os.environ.get("INGRESS_NETWORK", "xts_ingress_net")

# Local process registry for macOS / non-Docker dev environments
LOCAL_PROCESSES = {}
LOCAL_PORTS = {}
BASE_LOCAL_PORT = 8001
PORT_LOCK = threading.Lock()

def get_docker_client():
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return client
    except Exception:
        return None

def write_client_config(tenant_id: str):
    """Retrieves encrypted credentials and risk parameters, writing them to tenant's mounted volume."""
    with closing(get_db_connection()) as conn:
        cred_row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (tenant_id,)).fetchone()
        if not cred_row:
            raise ValueError(f"Missing credentials for tenant {tenant_id}")

        risk_row = conn.execute("SELECT * FROM tenant_risk_limits WHERE tenant_id=?", (tenant_id,)).fetchone()
        creds = security.decrypt_credentials(cred_row["encrypted_payload"])
        risk_dict = dict(risk_row) if risk_row else {}
        
        config_payload = {
            "API_KEY": creds.get("API_KEY", ""),
            "API_SECRET": creds.get("API_SECRET", ""),
            "MD_API_KEY": creds.get("MD_API_KEY", creds.get("API_KEY", "")),
            "MD_API_SECRET": creds.get("MD_API_SECRET", creds.get("API_SECRET", "")),
            "CLIENT_ID": creds.get("CLIENT_ID", ""),
            "WEBHOOK_SECRET": creds.get("WEBHOOK_SECRET", ""),
            "XTS_API_BASE_URL": creds.get("XTS_API_BASE_URL", "https://symphony.acagarwal.com:3000/interactive"),
            "ORDER_TYPE": risk_dict.get("order_type") or "LIMIT",
            "SLIPPAGE_BUFFER_PCT": float(risk_dict.get("slippage_buffer_pct", 0.005) or 0.005),
            "TV_SENDS_LOTS": bool(risk_dict.get("tv_sends_lots", True) if risk_dict.get("tv_sends_lots") is not None else True),
            "MAX_LOTS_LIMIT": int(risk_dict.get("max_lots_limit", 100) or 100),
            "MAX_UNITS_LIMIT": int(risk_dict.get("max_units_limit", 100000) or 100000),
            "MAX_ORDER_VALUE_INR": float(risk_dict.get("max_order_value_inr", 5000000.0) or 5000000.0),
            "DAILY_NOTIONAL_CAP_INR": float(risk_dict.get("daily_notional_cap_inr", 10000000.0) or 10000000.0),
            "MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX": int(risk_dict.get("min_days_before_expiry_mcx", 3) or 3),
            "MIN_DAYS_BEFORE_EXPIRY_DERIVATIVES": int(risk_dict.get("min_days_before_expiry_derivatives", 0) or 0),
            "CANCEL_LINGERING_PARTIAL_FILLS": bool(risk_dict.get("cancel_lingering_partial_fills", True)),
            "PARTIAL_FILL_TIMEOUT_SECONDS": float(risk_dict.get("partial_fill_timeout_seconds", 2.0) or 2.0),
            "PAPER_TRADE_MODE": bool(risk_dict.get("paper_trade_mode", True)),
            "TELEGRAM_BOT_TOKEN": str(risk_dict.get("telegram_bot_token") or "").strip(),
            "TELEGRAM_CHAT_ID": str(risk_dict.get("telegram_chat_id") or "").strip(),
            "DISCORD_WEBHOOK_URL": str(risk_dict.get("discord_webhook_url") or "").strip(),
        }

    tenant_dir = os.path.join(get_client_data_root(), tenant_id)
    os.makedirs(tenant_dir, exist_ok=True)
    config_file = os.path.join(tenant_dir, "config.json")
    
    with open(config_file, "w") as f:
        json.dump(config_payload, f, indent=2)
    os.chmod(config_file, 0o600)
    logger.info(f"Wrote config.json for tenant {tenant_id}")

def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        return s.connect_ex(('127.0.0.1', port)) == 0

def get_tenant_port(tenant_id: str) -> int:
    with PORT_LOCK:
        if tenant_id in LOCAL_PORTS:
            return LOCAL_PORTS[tenant_id]
        used = set(LOCAL_PORTS.values())
        p = BASE_LOCAL_PORT
        while p in used or _is_port_in_use(p):
            p += 1
        LOCAL_PORTS[tenant_id] = p
        return p

def provision_client_container(tenant_id: str) -> dict:
    write_client_config(tenant_id)
    client = get_docker_client()

    # DOCKER MODE (Ubuntu Server)
    if client:
        container_name = f"xts_client_{tenant_id}"
        tenant_data_dir = os.path.abspath(os.path.join(get_client_data_root(), tenant_id))

        try:
            existing = client.containers.get(container_name)
            existing.remove(force=True)
        except Exception:
            pass

        try:
            container = client.containers.run(
                image=CLIENT_IMAGE,
                name=container_name,
                hostname=container_name,
                network=INGRESS_NETWORK,
                volumes={tenant_data_dir: {"bind": "/app/data", "mode": "rw"}},
                environment={"DATA_DIR": "/app/data", "CLIENT_ID": tenant_id},
                mem_reservation="45m",
                mem_limit="256m",
                restart_policy={"Name": "unless-stopped"},
                detach=True
            )
            return {"status": "success", "container_id": container.id, "mode": "docker"}
        except Exception as e:
            logger.error(f"Failed to provision container {container_name}: {e}")
            return {"status": "error", "message": str(e)}

    # LOCAL PROCESS MODE (macOS / Local Test Environment)
    logger.info(f"Local environment detected: Spawning tenant {tenant_id} as local background process...")
    port = get_tenant_port(tenant_id)
    tenant_data_dir = os.path.abspath(os.path.join(get_client_data_root(), tenant_id))
    log_file = os.path.join(tenant_data_dir, "stdout.log")

    stop_client_container(tenant_id)
    time.sleep(0.3)

    client_code_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "client"))
    env = os.environ.copy()
    env["DATA_DIR"] = tenant_data_dir
    env["CLIENT_ID"] = tenant_id

    # Detect python venv executable
    py_exec = sys.executable

    log_fd = open(log_file, "a")
    proc = subprocess.Popen(
        [py_exec, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
        cwd=client_code_dir,
        env=env,
        stdout=log_fd,
        stderr=subprocess.STDOUT
    )
    LOCAL_PROCESSES[tenant_id] = proc
    try:
        with open(os.path.join(tenant_data_dir, "client.pid"), "w") as pf:
            pf.write(str(proc.pid))
    except Exception:
        pass
    logger.info(f"✅ Local process for {tenant_id} launched on PID {proc.pid} (Port: {port})")
    return {"status": "success", "pid": proc.pid, "port": port, "mode": "process"}

def restart_client_container(tenant_id: str) -> dict:
    write_client_config(tenant_id)
    client = get_docker_client()
    if client:
        container_name = f"xts_client_{tenant_id}"
        try:
            container = client.containers.get(container_name)
            container.restart(timeout=5)
            return {"status": "success"}
        except Exception:
            return provision_client_container(tenant_id)

    return provision_client_container(tenant_id)

def stop_client_container(tenant_id: str) -> dict:
    client = get_docker_client()
    if client:
        container_name = f"xts_client_{tenant_id}"
        try:
            container = client.containers.get(container_name)
            container.stop(timeout=5)
            return {"status": "success"}
        except Exception:
            return {"status": "not_found"}

    tenant_data_dir = os.path.abspath(os.path.join(get_client_data_root(), tenant_id))
    pid_file = os.path.join(tenant_data_dir, "client.pid")

    proc = LOCAL_PROCESSES.pop(tenant_id, None)
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try: proc.kill()
            except Exception: pass

    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as pf:
                old_pid = int(pf.read().strip())
            os.kill(old_pid, 15) # SIGTERM
            time.sleep(0.2)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass

    return {"status": "success"}

def remove_client_container(tenant_id: str) -> dict:
    stop_client_container(tenant_id)
    with PORT_LOCK:
        LOCAL_PORTS.pop(tenant_id, None)
        LOCAL_PROCESSES.pop(tenant_id, None)
    client = get_docker_client()
    if client:
        container_name = f"xts_client_{tenant_id}"
        try:
            container = client.containers.get(container_name)
            container.remove(force=True)
            return {"status": "success"}
        except Exception:
            return {"status": "not_found"}
    return {"status": "success"}

def get_container_logs(tenant_id: str, tail=100) -> str:
    client = get_docker_client()
    if client:
        container_name = f"xts_client_{tenant_id}"
        try:
            container = client.containers.get(container_name)
            logs = container.logs(tail=tail, timestamps=True)
            return logs.decode('utf-8', errors='replace')
        except Exception as e:
            return f"Container logs unavailable: {e}"

    tenant_data_dir = os.path.join(get_client_data_root(), tenant_id)
    log_file = os.path.join(tenant_data_dir, "stdout.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                lines = f.readlines()
                return "".join(lines[-tail:])
        except Exception as e:
            return f"Error reading log file: {e}"
    return "No logs generated yet."

def get_container_status(tenant_id: str) -> str:
    client = get_docker_client()
    if client:
        container_name = f"xts_client_{tenant_id}"
        try:
            container = client.containers.get(container_name)
            return container.status.upper()
        except Exception:
            return "STOPPED"

    proc = LOCAL_PROCESSES.get(tenant_id)
    if proc and proc.poll() is None:
        return "HEALTHY"

    tenant_data_dir = os.path.abspath(os.path.join(get_client_data_root(), tenant_id))
    pid_file = os.path.join(tenant_data_dir, "client.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as pf:
                pid = int(pf.read().strip())
            os.kill(pid, 0) # Checks if PID is alive
            return "HEALTHY"
        except Exception:
            pass

    return "STOPPED"
