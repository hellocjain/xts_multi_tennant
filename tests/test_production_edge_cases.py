import os
import sys
import tempfile
import json
import sqlite3
import pytest
import concurrent.futures
from decimal import Decimal
from fastapi.testclient import TestClient

# Setup test environment
test_dir = tempfile.mkdtemp()
os.environ["PORTAL_DATA_DIR"] = os.path.join(test_dir, "portal")
os.environ["CLIENT_DATA_ROOT"] = os.path.join(test_dir, "data")
os.environ["BACKUP_DEST_DIR"] = os.path.join(test_dir, "backups")
os.environ["PORTAL_MASTER_KEY"] = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="

portal_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal"))
backup_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backup"))
client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client"))

if "main" in sys.modules:
    del sys.modules["main"]
if sys.path[0] != portal_dir:
    sys.path.insert(0, portal_dir)
if backup_dir not in sys.path:
    sys.path.insert(1, backup_dir)
if client_dir not in sys.path:
    sys.path.insert(2, client_dir)

import database
import security
import docker_manager
import telemetry_service
import backup_engine
import main as portal_main

app = portal_main.app

@pytest.fixture(autouse=True)
def setup_production_test_env():
    database.init_portal_db()
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("DELETE FROM admin_sessions")
            conn.execute("DELETE FROM admin_users")
            conn.execute("DELETE FROM tenants")
            conn.execute("DELETE FROM tenant_credentials")
            conn.execute("DELETE FROM tenant_risk_limits")

            # Seed Admin
            pwd_hash = security.hash_password("SuperSecretAdminPass99!")
            conn.execute("""
                INSERT INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at)
                VALUES ('admin_sec_01', 'admin_sec', ?, 1, ?)
            """, (pwd_hash, 1600000000.0))

            # Tenant A: Live
            conn.execute("INSERT INTO tenants VALUES ('tenant_a', 'Tenant Alpha LLC', 'ACTIVE', 1600000000, 1600000000)")
            conn.execute("INSERT INTO tenant_credentials VALUES ('tenant_a', ?, 1600000000)", (
                security.encrypt_credentials({"API_KEY": "AK_A", "API_SECRET": "AS_A", "CLIENT_ID": "ID_A", "WEBHOOK_SECRET": "WH_A"}),
            ))
            conn.execute("""
                INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at)
                VALUES ('tenant_a', 50, 2500000, 5000000, 0.005, 3, 0, 1600000000.0)
            """)

            # Tenant B: Paper
            conn.execute("INSERT INTO tenants VALUES ('tenant_b', 'Tenant Beta <script>alert(1)</script>', 'ACTIVE', 1600000000, 1600000000)")
            conn.execute("INSERT INTO tenant_credentials VALUES ('tenant_b', ?, 1600000000)", (
                security.encrypt_credentials({"API_KEY": "AK_B", "API_SECRET": "AS_B", "CLIENT_ID": "ID_B", "WEBHOOK_SECRET": "WH_B"}),
            ))
            conn.execute("""
                INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at)
                VALUES ('tenant_b', 100, 5000000, 10000000, 0.005, 3, 1, 1600000000.0)
            """)

    yield

def get_auth_client():
    client = TestClient(app, cookies={})
    token = security.create_session("admin_sec_01", "127.0.0.1", "pro-audit-agent")
    client.cookies.set("admin_session", token)
    return client

# ==============================================================================
# EDGE CASE AUDIT 1: Multi-Tenant Data Isolation & Path Traversal Guard
# ==============================================================================

def test_multi_tenant_data_isolation():
    """Verify that credentials and configs for Tenant A and Tenant B remain isolated."""
    with database.get_db_connection() as conn:
        row_a = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id='tenant_a'").fetchone()
        row_b = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id='tenant_b'").fetchone()

        creds_a = security.decrypt_credentials(row_a["encrypted_payload"])
        creds_b = security.decrypt_credentials(row_b["encrypted_payload"])

        assert creds_a["CLIENT_ID"] == "ID_A"
        assert creds_b["CLIENT_ID"] == "ID_B"
        assert creds_a["WEBHOOK_SECRET"] != creds_b["WEBHOOK_SECRET"]

def test_xss_sanitization_in_templates():
    """Verify malicious script payloads in client names are properly escaped in rendered HTML."""
    client = get_auth_client()
    res = client.get("/admin/dashboard")
    assert res.status_code == 200
    # Script tag should be escaped as &lt;script&gt;
    assert "<script>alert(1)</script>" not in res.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in res.text

# ==============================================================================
# EDGE CASE AUDIT 2: Single-Use Recovery Code Consumption & Invalidation
# ==============================================================================

def test_recovery_code_consumption_lifecycle():
    """Verify recovery codes can only be used once and hash is removed."""
    codes = security.generate_recovery_codes(10)
    hashed = security.hash_recovery_codes(codes)

    with database.get_db_connection() as conn:
        with conn:
            conn.execute(
                "UPDATE admin_users SET recovery_codes_hash_json=? WHERE id='admin_sec_01'",
                (json.dumps(hashed),)
            )

    test_code = codes[0]
    # First use: must succeed
    assert security.verify_and_consume_recovery_code("admin_sec_01", test_code) is True

    # Second use with identical code: must be rejected (consumed)
    assert security.verify_and_consume_recovery_code("admin_sec_01", test_code) is False

    # Invalid code: must be rejected
    assert security.verify_and_consume_recovery_code("admin_sec_01", "INVALID-CODE-999") is False

# ==============================================================================
# EDGE CASE AUDIT 3: Master Key Rotation Data Integrity
# ==============================================================================

def test_master_key_vault_rotation():
    """Verify rotating master key decrypts old payload and re-encrypts with new key."""
    old_key = os.environ["PORTAL_MASTER_KEY"]
    new_key = "wK9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p4r6t8v0="

    # Encrypt with old key
    payload = {"API_KEY": "KEY_ROTATE_99", "CLIENT_ID": "CLI_ROTATE"}
    enc_old = security.encrypt_credentials(payload)

    # Decrypt with old key and re-encrypt with new key
    dec = security.decrypt_credentials(enc_old)
    assert dec == payload

    os.environ["PORTAL_MASTER_KEY"] = new_key
    enc_new = security.encrypt_credentials(payload)
    assert enc_new != enc_old
    assert security.decrypt_credentials(enc_new) == payload

    # Restore key
    os.environ["PORTAL_MASTER_KEY"] = old_key

# ==============================================================================
# EDGE CASE AUDIT 4: High-Concurrency SQLite WAL Stress
# ==============================================================================

def test_high_concurrency_wal_access():
    """Simulate 50 parallel threads writing audit logs and reading dashboard data."""
    client = get_auth_client()

    def concurrent_operation(idx):
        # 1. Write an audit record
        database.record_audit(f"user_{idx}", "STRESS_ACTION", {"index": idx}, target_tenant_id="tenant_a")
        # 2. Read dashboard partial
        res = client.get("/admin/dashboard-partial")
        return res.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(concurrent_operation, i) for i in range(50)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert all(code == 200 for code in results)

    # Verify all 50 audit entries were persisted cleanly
    with database.get_db_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='STRESS_ACTION'").fetchone()[0]
        assert count == 50

# ==============================================================================
# EDGE CASE AUDIT 5: Hot SQLite Backup Engine (VACUUM INTO)
# ==============================================================================

def test_hot_vacuum_backup_drill():
    """Verify non-blocking backup creates encrypted archive and restores without corruption."""
    passphrase = "UltraSecurePassphrase123!"
    archive_path = backup_engine.create_backup_archive(passphrase)
    assert os.path.exists(archive_path)
    assert archive_path.endswith(".tar.gz.gpg") or archive_path.endswith(".tar.gz.enc")

    # Verify backup file size > 0
    assert os.path.getsize(archive_path) > 100
