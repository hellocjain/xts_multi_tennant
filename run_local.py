#!/usr/bin/env python3
"""
🚀 XTS MULTI-TENANT LOCAL RUNNER (FOR LOCAL TESTING ON MAC/LINUX)
Runs the Admin Portal on http://127.0.0.1:8500 and manages local isolated client processes.
"""
import os
import sys
import time
import subprocess
import uvicorn

# Setup local environment variables
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_DATA = os.path.join(PROJECT_ROOT, "local_data")
os.makedirs(os.path.join(LOCAL_DATA, "portal"), exist_ok=True)
os.makedirs(os.path.join(LOCAL_DATA, "data"), exist_ok=True)
os.makedirs(os.path.join(LOCAL_DATA, "caddy"), exist_ok=True)

os.environ["PORTAL_DATA_DIR"] = os.path.join(LOCAL_DATA, "portal")
os.environ["CLIENT_DATA_ROOT"] = os.path.join(LOCAL_DATA, "data")
os.environ["CADDY_CONFIG_PATH"] = os.path.join(LOCAL_DATA, "caddy", "Caddyfile")

from cryptography.fernet import Fernet
if "PORTAL_MASTER_KEY" not in os.environ:
    dev_key_file = os.path.join(LOCAL_DATA, ".dev_master_key")
    if os.path.exists(dev_key_file):
        try:
            with open(dev_key_file, "r") as f:
                os.environ["PORTAL_MASTER_KEY"] = f.read().strip()
        except Exception:
            pass
    if "PORTAL_MASTER_KEY" not in os.environ or not os.environ["PORTAL_MASTER_KEY"]:
        new_dev_key = Fernet.generate_key().decode()
        os.environ["PORTAL_MASTER_KEY"] = new_dev_key
        try:
            with open(dev_key_file, "w") as f:
                f.write(new_dev_key)
            os.chmod(dev_key_file, 0o600)
        except Exception:
            pass

os.environ["PORTAL_ADMIN_USER"] = "admin"
os.environ["PORTAL_ADMIN_PASSWORD"] = "AdminPass123!"
os.environ["DOMAIN_NAME"] = "localhost:8500"

sys.path.insert(0, os.path.join(PROJECT_ROOT, "portal"))
import database
import security
import docker_manager

def seed_sample_paper_clients():
    """Pre-configures two sample paper trading clients so you can test immediately."""
    database.init_portal_db()
    with database.get_db_connection() as conn:
        with conn:
            # Client 1: Alpha
            if not conn.execute("SELECT id FROM tenants WHERE id='c01_alpha'").fetchone():
                creds_a = {"API_KEY": "MOCK_KEY_A", "API_SECRET": "MOCK_SEC_A", "CLIENT_ID": "ALPHA01", "WEBHOOK_SECRET": "Secret123", "XTS_API_BASE_URL": "https://symphony.acagarwal.com:3000/interactive"}
                conn.execute("INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES ('c01_alpha', 'Rahul Mehta (Paper)', 'ACTIVE', ?, ?)", (time.time(), time.time()))
                conn.execute("INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('c01_alpha', ?, ?)", (security.encrypt_credentials(creds_a), time.time()))
                conn.execute("INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at) VALUES ('c01_alpha', 50, 2500000, 5000000, 0.005, 3, 1, ?)", (time.time(),))

            # Client 2: Beta
            if not conn.execute("SELECT id FROM tenants WHERE id='c02_beta'").fetchone():
                creds_b = {"API_KEY": "MOCK_KEY_B", "API_SECRET": "MOCK_SEC_B", "CLIENT_ID": "BETA01", "WEBHOOK_SECRET": "Secret123", "XTS_API_BASE_URL": "https://symphony.acagarwal.com:3000/interactive"}
                conn.execute("INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES ('c02_beta', 'Amit Kumar (Paper)', 'ACTIVE', ?, ?)", (time.time(), time.time()))
                conn.execute("INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('c02_beta', ?, ?)", (security.encrypt_credentials(creds_b), time.time()))
                conn.execute("INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at) VALUES ('c02_beta', 100, 5000000, 10000000, 0.005, 3, 1, ?)", (time.time(),))

    docker_manager.provision_client_container("c01_alpha")
    docker_manager.provision_client_container("c02_beta")

if __name__ == "__main__":
    print("\n" + "=" * 75)
    print("🚀 STARTING XTS MULTI-TENANT ENTERPRISE CLUSTER (LOCAL MODE)")
    print("=" * 75)
    
    seed_sample_paper_clients()

    print("\n🌐 Admin Portal URL    : http://127.0.0.1:8500/admin/login")
    print("👤 Admin Username      : admin")
    print("🔑 Admin Password      : AdminPass123!")
    print("\n📱 On First Login      : Scan QR code with Google Authenticator / 1Password")
    print("💡 Pre-Loaded Clients  : c01_alpha (Port 8001) | c02_beta (Port 8002)")
    print("=" * 75 + "\n")

    # Start Portal with Uvicorn
    uvicorn.run("main:app", app_dir=os.path.join(PROJECT_ROOT, "portal"), host="127.0.0.1", port=8500, log_level="info")
