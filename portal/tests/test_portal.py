import os
import sys
import tempfile
import json
import pytest
from fastapi.testclient import TestClient

test_dir = tempfile.mkdtemp()
os.environ["PORTAL_DATA_DIR"] = test_dir
os.environ["PORTAL_MASTER_KEY"] = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p=" # 32 bytes urlsafe base64

# Add portal path and remove any cached client.main
portal_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if "main" in sys.modules:
    del sys.modules["main"]
if "config" in sys.modules:
    del sys.modules["config"]
if "xts_api" in sys.modules:
    del sys.modules["xts_api"]

sys.path.insert(0, portal_dir)

import database
import security
import caddy_manager
import docker_manager
import main as portal_main
app = portal_main.app

@pytest.fixture(autouse=True)
def init_test_db():
    database.init_portal_db()
    yield

def test_vault_encryption():
    creds = {
        "API_KEY": "KEY_123",
        "API_SECRET": "SECRET_456",
        "CLIENT_ID": "ABK01",
        "WEBHOOK_SECRET": "MySuperSecret"
    }
    enc = security.encrypt_credentials(creds)
    assert isinstance(enc, str)
    assert enc != json.dumps(creds)

    dec = security.decrypt_credentials(enc)
    assert dec == creds

def test_password_hashing():
    pwd = "EnterprisePassword99!"
    h = security.hash_password(pwd)
    assert security.verify_password(pwd, h) is True
    assert security.verify_password("WrongPassword", h) is False

def test_totp_and_recovery_codes():
    secret = security.generate_totp_secret()
    import pyotp
    totp = pyotp.TOTP(secret)
    code = totp.now()

    assert security.verify_totp(secret, code) is True
    assert security.verify_totp(secret, "000000") is False

    codes = security.generate_recovery_codes(10)
    assert len(codes) == 10
    hashed = security.hash_recovery_codes(codes)
    assert len(hashed) == 10

def test_session_lifecycle():
    # Insert user first to satisfy foreign key constraint
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) VALUES ('user_01', 'admin_test', 'hash', 1, 1000)")

    token = security.create_session("user_01", "127.0.0.1", "TestAgent", lifetime_seconds=10)
    assert token is not None

    validated = security.validate_session(token, "127.0.0.1", "TestAgent")
    assert validated is not None
    assert validated["username"] == "admin_test"

    security.destroy_session(token)
    assert security.validate_session(token, "127.0.0.1", "TestAgent") is None

def test_portal_flow():
    with TestClient(app) as client:
        # 1. Access login
        res = client.get("/admin/login")
        assert res.status_code == 200

        # 2. Login with default admin
        res = client.post("/admin/login", data={
            "username": "admin",
            "password": "AdminPass123!",
            "totp_or_recovery": ""
        }, follow_redirects=False)
        assert res.status_code == 303
        assert "/admin/2fa-setup" in res.headers["Location"]

        cookie = res.cookies.get("admin_session")

        # 3. Setup 2FA
        res_setup = client.get("/admin/2fa-setup", cookies={"admin_session": cookie})
        assert res_setup.status_code == 200

        secret = security.generate_totp_secret()
        import pyotp
        totp = pyotp.TOTP(secret)
        current_code = totp.now()
        rec_codes = security.generate_recovery_codes(10)

        res_confirm = client.post("/admin/2fa-confirm", data={
            "totp_secret": secret,
            "recovery_codes_json": json.dumps(rec_codes),
            "confirmation_code": current_code
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_confirm.status_code == 303

        # 4. Access Dashboard
        res_dash = client.get("/admin/dashboard", cookies={"admin_session": cookie})
        assert res_dash.status_code == 200
        assert "EMERGENCY GLOBAL KILL-SWITCH" in res_dash.text

        # 5. Onboard Client
        res_add = client.post("/admin/clients/add", data={
            "tenant_id": "c01_test",
            "name": "Test Client Rahul",
            "api_key": "AK_111",
            "api_secret": "AS_222",
            "client_id": "ABK99",
            "webhook_secret": "WhSecret123",
            "max_lots_limit": 50,
            "max_order_value_inr": 2500000,
            "daily_notional_cap_inr": 5000000,
            "slippage_buffer_pct": 0.005,
            "min_days_before_expiry_mcx": 3,
            "paper_trade_mode": 1
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_add.status_code == 303

        # 6. Verify client detail & audit logs
        res_detail = client.get("/admin/clients/c01_test", cookies={"admin_session": cookie})
        assert res_detail.status_code == 200
        assert "Test Client Rahul" in res_detail.text
        assert "WhSecret123" in res_detail.text

        # 7. Test Webhook Modal
        res_modal = client.get("/admin/clients/c01_test/webhook-modal", cookies={"admin_session": cookie})
        assert res_modal.status_code == 200
        assert "WhSecret123" in res_modal.text
        assert "{{strategy.order.action}}" in res_modal.text

        # 8. Edit Client (Update Webhook Secret & Risk Limits)
        res_edit = client.post("/admin/clients/c01_test/edit", data={
            "name": "Test Client Rahul Renamed",
            "api_key": "AK_111",
            "api_secret": "AS_222",
            "client_id": "ABK99",
            "webhook_secret": "NewWhSecret999",
            "max_lots_limit": 100,
            "max_order_value_inr": 3000000,
            "daily_notional_cap_inr": 6000000,
            "slippage_buffer_pct": 0.005,
            "min_days_before_expiry_mcx": 3,
            "paper_trade_mode": 1
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_edit.status_code == 303

        # 9. Verify Edited Secret
        res_detail_updated = client.get("/admin/clients/c01_test", cookies={"admin_session": cookie})
        assert res_detail_updated.status_code == 200
        assert "NewWhSecret999" in res_detail_updated.text

        res_audit = client.get("/admin/audit-logs", cookies={"admin_session": cookie})
        assert res_audit.status_code == 200
        assert "UPDATE_CONFIG" in res_audit.text

        # 10. Test Global Orders Stream
        res_orders = client.get("/admin/orders", cookies={"admin_session": cookie})
        assert res_orders.status_code == 200
        assert "Global Order Audit Stream" in res_orders.text

        res_orders_partial = client.get("/admin/orders-partial", cookies={"admin_session": cookie})
        assert res_orders_partial.status_code == 200

        # 11. Test Settings Page & Manual Backup
        res_settings = client.get("/admin/settings", cookies={"admin_session": cookie})
        assert res_settings.status_code == 200
        assert "Cluster Operations" in res_settings.text

        res_backup = client.post("/admin/settings/backup", cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_backup.status_code == 303
        assert "msg=Backup+" in res_backup.headers["Location"]

        # 12. Test Key Rotation
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key().decode()
        res_rot = client.post("/admin/settings/rotate-master-key", data={"new_master_key": new_key}, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_rot.status_code == 303
        assert "msg=Master+key+rotated" in res_rot.headers["Location"]

