import os
import sys
import tempfile
import json
import pytest
import asyncio
from decimal import Decimal

# Setup isolated test directory
test_root = tempfile.mkdtemp()
os.environ["PORTAL_DATA_DIR"] = os.path.join(test_root, "portal")
os.environ["CLIENT_DATA_ROOT"] = os.path.join(test_root, "data")
os.environ["BACKUP_DEST_DIR"] = os.path.join(test_root, "backups")
os.environ["CADDY_CONFIG_PATH"] = os.path.join(test_root, "caddy", "Caddyfile")
os.environ["PORTAL_MASTER_KEY"] = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="
os.environ["BACKUP_PASSPHRASE"] = "TestBackupPassphrase99!"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backup")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))

import database
import security
import docker_manager
import caddy_manager
import scheduler
import backup_engine
import dr_restore

@pytest.fixture(autouse=True)
def setup_all():
    database.init_portal_db()
    yield

def test_multi_tenant_lifecycle_and_isolation():
    # 1. Onboard Tenant 1 (c01_alpha)
    t1_creds = {"API_KEY": "KEY_A", "API_SECRET": "SEC_A", "CLIENT_ID": "ABK01", "WEBHOOK_SECRET": "SecA"}
    t1_enc = security.encrypt_credentials(t1_creds)
    
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES ('c01_alpha', 'Alpha Account', 'ACTIVE', 1000, 1000)")
            conn.execute("INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('c01_alpha', ?, 1000)", (t1_enc,))
            conn.execute("INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at) VALUES ('c01_alpha', 10, 1000000, 2000000, 0.005, 3, 1, 1000)")

    # 2. Onboard Tenant 2 (c02_beta)
    t2_creds = {"API_KEY": "KEY_B", "API_SECRET": "SEC_B", "CLIENT_ID": "ABK02", "WEBHOOK_SECRET": "SecB"}
    t2_enc = security.encrypt_credentials(t2_creds)
    
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES ('c02_beta', 'Beta Account', 'ACTIVE', 1000, 1000)")
            conn.execute("INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('c02_beta', ?, 1000)", (t2_enc,))
            conn.execute("INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at) VALUES ('c02_beta', 50, 5000000, 10000000, 0.005, 3, 0, 1000)")

    # 3. Write configs and verify tenant file isolation
    docker_manager.write_client_config("c01_alpha")
    docker_manager.write_client_config("c02_beta")

    c1_config_file = os.path.join(os.environ["CLIENT_DATA_ROOT"], "c01_alpha", "config.json")
    c2_config_file = os.path.join(os.environ["CLIENT_DATA_ROOT"], "c02_beta", "config.json")

    assert os.path.exists(c1_config_file)
    assert os.path.exists(c2_config_file)

    with open(c1_config_file) as f:
        c1_cfg = json.load(f)
        assert c1_cfg["CLIENT_ID"] == "ABK01"
        assert c1_cfg["DAILY_NOTIONAL_CAP_INR"] == 2000000
        assert c1_cfg["PAPER_TRADE_MODE"] is True

    with open(c2_config_file) as f:
        c2_cfg = json.load(f)
        assert c2_cfg["CLIENT_ID"] == "ABK02"
        assert c2_cfg["DAILY_NOTIONAL_CAP_INR"] == 10000000
        assert c2_cfg["PAPER_TRADE_MODE"] is False

    # 4. Verify Caddyfile dynamic generation
    assert caddy_manager.sync_caddy_config() is True
    with open(os.environ["CADDY_CONFIG_PATH"]) as f:
        caddy_txt = f.read()
        assert "handle_path /webhook/c01_alpha*" in caddy_txt
        assert "handle_path /webhook/c02_beta*" in caddy_txt
        assert "reverse_proxy xts_client_c01_alpha:8000" in caddy_txt

def test_backup_and_disaster_recovery_drill():
    # 1. Create Hot SQLite Backup Archive
    backup_file = backup_engine.create_backup_archive(passphrase=os.environ["BACKUP_PASSPHRASE"])
    assert os.path.exists(backup_file)
    assert backup_file.endswith(".tar.gz.gpg")

    # 2. Simulate complete destruction & recovery to clean directory
    dr_dest_root = tempfile.mkdtemp()
    dr_restore.restore_disaster_backup(
        backup_file=backup_file,
        passphrase=os.environ["BACKUP_PASSPHRASE"],
        master_key=os.environ["PORTAL_MASTER_KEY"],
        dest_root=dr_dest_root
    )

    # 3. Verify DR Restored State
    restored_portal_db = os.path.join(dr_dest_root, "portal", "portal.db")
    restored_master_env = os.path.join(dr_dest_root, "portal", ".env")
    restored_c1_dir = os.path.join(dr_dest_root, "data", "c01_alpha")

    assert os.path.exists(restored_portal_db)
    assert os.path.exists(restored_master_env)
    assert os.path.exists(restored_c1_dir)

    with open(restored_master_env) as f:
        assert os.environ["PORTAL_MASTER_KEY"] in f.read()

def test_rolling_warmup_execution():
    async def run():
        res = await scheduler.run_rolling_cache_warmup(batch_size=2, delay_between_batches_sec=0.1)
        assert res["status"] == "success"
        assert res["warmed_up"] >= 2
    asyncio.run(run())
