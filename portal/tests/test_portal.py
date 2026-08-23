import os
import sys
import tempfile
import json
import pytest
from contextlib import closing
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
import telemetry_service
import main as portal_main
app = portal_main.app

@pytest.fixture(autouse=True)
def init_test_db(monkeypatch):
    test_key = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="
    monkeypatch.setenv("PORTAL_MASTER_KEY", test_key)
    database.init_portal_db()
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("DELETE FROM admin_sessions")
            conn.execute("DELETE FROM admin_users")
            conn.execute("DELETE FROM tenant_credentials")
            conn.execute("DELETE FROM tenant_risk_limits")
            conn.execute("DELETE FROM tenants")
            pwd_hash = security.hash_password("AdminPass123!")
            conn.execute("""
                INSERT INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at)
                VALUES ('admin_init', 'admin', ?, 0, ?)
            """, (pwd_hash, 1600000000.0))
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

def test_vault_encryption_refuses_missing_master_key(monkeypatch):
    monkeypatch.delenv("PORTAL_MASTER_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PORTAL_MASTER_KEY environment variable is not configured"):
        security.get_fernet()

def test_migrate_vault_script(monkeypatch):
    import scripts.migrate_vault_master_key as migrator
    old_key = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="
    new_key = "wK9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p4r6t8v0="
    
    # Setup test tenant credentials with old key
    monkeypatch.setenv("PORTAL_MASTER_KEY", old_key)
    creds = {"API_KEY": "TEST_KEY", "CLIENT_ID": "TEST_ID"}
    enc = security.encrypt_credentials(creds)
    
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('mig_test', 'Mig Test', 'ACTIVE', 0, 0)")
            conn.execute("INSERT OR REPLACE INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('mig_test', ?, 0)", (enc,))
    
    res = migrator.migrate_vault(old_key, new_key)
    assert res["status"] == "success"
    assert res["re_encrypted_tenants"] >= 1
    
    # Decrypt with new key
    monkeypatch.setenv("PORTAL_MASTER_KEY", new_key)
    with database.get_db_connection() as conn:
        row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id='mig_test'").fetchone()
        dec = security.decrypt_credentials(row["encrypted_payload"])
        assert dec["API_KEY"] == "TEST_KEY"

def test_sec_xts_004_migrate_vault_refuses_missing_old_master_key(monkeypatch):
    """
    Regression Test for SEC-XTS-004:
    Verifies that migrate_vault refuses to run and raises an explicit error
    if OLD_PORTAL_MASTER_KEY is not configured, failing closed rather than
    falling back to a hardcoded default key.
    """
    import scripts.migrate_vault_master_key as migrator
    monkeypatch.delenv("OLD_PORTAL_MASTER_KEY", raising=False)
    monkeypatch.delenv("PORTAL_MASTER_KEY", raising=False)

    with pytest.raises((RuntimeError, ValueError), match="OLD_PORTAL_MASTER_KEY"):
        migrator.migrate_vault(old_key_str=None)

    # Hardening test: Even if PORTAL_MASTER_KEY is present in env, if OLD_PORTAL_MASTER_KEY is unset, it must STILL raise!
    monkeypatch.setenv("PORTAL_MASTER_KEY", "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p=")
    monkeypatch.delenv("OLD_PORTAL_MASTER_KEY", raising=False)
    with pytest.raises((RuntimeError, ValueError), match="OLD_PORTAL_MASTER_KEY"):
        migrator.migrate_vault(old_key_str=None)


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
            "recovery_codes_str": ",".join(rec_codes),
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

def test_telemetry_broker_reject_reason_extraction(tmp_path, monkeypatch):
    import telemetry_service
    import sqlite3
    
    # Create temporary tenant signals.db
    tenant_dir = tmp_path / "clients" / "test_tenant"
    tenant_dir.mkdir(parents=True)
    db_path = tenant_dir / "signals.db"
    
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE signals (
                id TEXT PRIMARY KEY,
                status TEXT,
                payload TEXT,
                result TEXT,
                received_at REAL,
                updated_at REAL
            )
        """)
        
        # Insert a signal with broker description and code
        payload = json.dumps({"action": "BUY", "symbol": "CRUDEOIL", "quantity": 1, "price": 6500.0, "order_ref": "TV_123"})
        result = json.dumps({"type": "error", "code": "e-order-0008", "description": "Tick size invalid for exchange segment"})
        conn.execute("INSERT INTO signals VALUES ('sig_1', 'failed', ?, ?, 1700000000.0, 1700000001.0)", (payload, result))
    
    monkeypatch.setattr(docker_manager, "get_client_data_root", lambda: str(tmp_path / "clients"))
    
    with database.get_db_connection() as pconn:
        with pconn:
            pconn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('test_tenant', 'Test Tenant', 'ACTIVE', 0, 0)")
    
    signals = telemetry_service.aggregate_all_signals()
    assert len(signals) >= 1
    target = next(s for s in signals if s["id"] == "sig_1")
    assert "[e-order-0008] Tick size invalid for exchange segment" in target["error_message"]

def test_caddy_sync_failure_propagation(monkeypatch):
    # If Caddy reload fails, sync_caddy_config returns False
    monkeypatch.setattr(caddy_manager, "get_caddy_admin_socket", lambda: "/tmp/nonexistent_caddy.sock")
    # In absence of socket and standalone mode, writes config and returns True
    res = caddy_manager.sync_caddy_config()
    assert res is True

    # If write fails (e.g. permission error), returns False
    monkeypatch.setattr(caddy_manager, "get_caddy_config_path", lambda: "/nonexistent_dir/caddy/Caddyfile")
    res_fail = caddy_manager.sync_caddy_config()
    assert res_fail is False

def test_thread_safe_port_allocation():
    import concurrent.futures
    import docker_manager

    # Clean local ports
    with docker_manager.PORT_LOCK:
        docker_manager.LOCAL_PORTS.clear()

    def allocate(t_id):
        return docker_manager.get_tenant_port(t_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(allocate, f"tenant_{i}"): f"tenant_{i}" for i in range(10)}
        ports = [f.result() for f in concurrent.futures.as_completed(futures)]

    # All 10 allocated ports must be unique
    assert len(ports) == 10
    assert len(set(ports)) == 10
    
    # Recycle one and verify it can be reallocated
    docker_manager.remove_client_container("tenant_0")
    with docker_manager.PORT_LOCK:
        assert "tenant_0" not in docker_manager.LOCAL_PORTS

def test_validate_broker_credentials_wizard(monkeypatch):
    # Setup test admin user and session
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) VALUES ('admin_wiz', 'admin_wiz', 'hash', 1, 1000)")
    
    token = security.create_session("admin_wiz", "testclient", "testclient")
    
    client = TestClient(app, cookies={"admin_session": token})

    # 1. Missing keys returns warning
    res_empty = client.post("/admin/clients/validate-credentials", data={"api_key": "", "api_secret": ""})
    assert res_empty.status_code == 200
    assert "Please enter both Interactive API Key and Secret" in res_empty.text

    # 2. Mock valid broker response
    monkeypatch.setattr(security, "validate_broker_credentials", lambda **kwargs: {
        "valid": True,
        "interactive": True,
        "market_data": True,
        "client_name": "Test Trader",
        "segments": ["NSECM", "MCXFO"],
        "errors": []
    })

    res_valid = client.post("/admin/clients/validate-credentials", data={
        "api_key": "VALID_KEY",
        "api_secret": "VALID_SECRET",
        "client_id": "ABK01"
    })
    assert res_valid.status_code == 200
    assert "Live Broker Handshake Verified" in res_valid.text
    assert "Test Trader" in res_valid.text
    assert "MCXFO" in res_valid.text

    # 3. Mock failed broker response
    monkeypatch.setattr(security, "validate_broker_credentials", lambda **kwargs: {
        "valid": False,
        "interactive": False,
        "market_data": False,
        "client_name": "",
        "segments": [],
        "errors": ["Interactive Auth Error: Invalid API Key or Secret"]
    })

    res_fail = client.post("/admin/clients/validate-credentials", data={
        "api_key": "BAD_KEY",
        "api_secret": "BAD_SECRET",
        "client_id": "ABK01"
    })
    assert res_fail.status_code == 200
    assert "Broker Authentication Failed" in res_fail.text
    assert "Invalid API Key or Secret" in res_fail.text

@pytest.mark.anyio
async def test_drawdown_circuit_breaker_auto_kill_switch(monkeypatch):
    import scheduler
    import telemetry_service
    
    # 1. Setup active tenant with max_daily_loss_inr = 25000.0
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('loss_tenant', 'Loss Trader', 'ACTIVE', 0, 0)")
            enc = security.encrypt_credentials({"WEBHOOK_SECRET": "test_sec"})
            conn.execute("INSERT OR REPLACE INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('loss_tenant', ?, 0)", (enc,))
            conn.execute("""
                INSERT OR REPLACE INTO tenant_risk_limits (
                    tenant_id, max_daily_loss_inr, updated_at
                ) VALUES ('loss_tenant', 25000.0, 0)
            """)

    # 2. Case A: Net MTM is -₹10,000 (below threshold) -> No circuit breaker
    async def mock_telemetry_safe(t_id):
        return {"net_mtm": -10000.0}
    monkeypatch.setattr(telemetry_service, "get_single_client_telemetry", mock_telemetry_safe)
    await scheduler.check_drawdown_circuit_breakers()

    with database.get_db_connection() as conn:
        t_row = conn.execute("SELECT status FROM tenants WHERE id='loss_tenant'").fetchone()
        assert t_row["status"] == "ACTIVE"

    # 3. Case B: Net MTM is -₹30,000 (breaches ₹25,000 limit) -> Triggers panic and auto-pauses
    panic_called = []
    async def mock_panic(t_id, secret):
        panic_called.append((t_id, secret))
        return {"status": "success", "orders_cancelled": 2, "positions_squared_off": 1}

    async def mock_telemetry_loss(t_id):
        return {"net_mtm": -30000.0}

    monkeypatch.setattr(telemetry_service, "get_single_client_telemetry", mock_telemetry_loss)
    monkeypatch.setattr(telemetry_service, "panic_single_client", mock_panic)
    monkeypatch.setattr(caddy_manager, "sync_caddy_config", lambda: True)

    await scheduler.check_drawdown_circuit_breakers()

    # Verify panic was called with secret
    assert len(panic_called) == 1
    assert panic_called[0] == ("loss_tenant", "test_sec")

    # Verify tenant status is now PAUSED
    with database.get_db_connection() as conn:
        t_row = conn.execute("SELECT status FROM tenants WHERE id='loss_tenant'").fetchone()
        assert t_row["status"] == "PAUSED"
        
        # Verify critical audit log
        audit = conn.execute("SELECT * FROM audit_logs WHERE action='AUTO_KILL_SWITCH_TRIGGERED'").fetchone()
        assert audit is not None
        assert audit["target_tenant_id"] == "loss_tenant"
        details = json.loads(audit["details_json"])
        assert details["net_mtm"] == -30000.0
        assert details["max_daily_loss_inr"] == 25000.0

def test_admin_master_cache_manual_sync(monkeypatch):
    import httpx
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) VALUES ('admin_sync', 'admin_sync', 'hash', 1, 1000)")
            conn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('sync_client', 'Sync Client', 'ACTIVE', 0, 0)")
    
    token = security.create_session("admin_sync", "testclient", "testclient")
    client = TestClient(app, cookies={"admin_session": token})

    class MockResp:
        status_code = 200
        def json(self):
            return {
                "status": "success",
                "cache_healthy": True,
                "cached_date": "2026-08-22",
                "futures_contracts": 12,
                "cash_contracts": 85
            }

    async def mock_post(self, url, **kwargs):
        return MockResp()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    # 1. Standard POST request redirects to detail page
    res_redirect = client.post("/admin/clients/sync_client/refresh-master", follow_redirects=False)
    assert res_redirect.status_code in (303, 307)
    assert "/admin/clients/sync_client" in res_redirect.headers["Location"]

    # 2. HTMX request returns HTML status badge
    res_htmx = client.post("/admin/clients/sync_client/refresh-master", headers={"HX-Request": "true"})
    assert res_htmx.status_code == 200
    assert "Synced (2026-08-22 | 97 contracts)" in res_htmx.text

def test_trade_book_contract_note_csv_export(monkeypatch):
    import telemetry_service
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) VALUES ('admin_export', 'admin_export', 'hash', 1, 1000)")
            conn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('export_client', 'Export Client', 'ACTIVE', 0, 0)")

    token = security.create_session("admin_export", "testclient", "testclient")
    client = TestClient(app, cookies={"admin_session": token})

    # Mock single client telemetry returning broker trades
    mock_trades = [
        {
            "TradeID": "TR_99182",
            "AppOrderID": 771122,
            "OrderExecutionTime": "22-Aug-2026 10:15:30",
            "ExchangeSegment": "MCXFO",
            "TradingSymbol": "CRUDEOIL24AUGFUT",
            "OrderSide": "BUY",
            "TradedQuantity": 100,
            "TradePrice": 6500.0
        },
        {
            "TradeID": "TR_99183",
            "AppOrderID": 771123,
            "OrderExecutionTime": "22-Aug-2026 11:30:15",
            "ExchangeSegment": "MCXFO",
            "TradingSymbol": "CRUDEOIL24AUGFUT",
            "OrderSide": "SELL",
            "TradedQuantity": 100,
            "TradePrice": 6550.0
        }
    ]

    async def mock_telemetry(tenant_id):
        return {"broker_trades": mock_trades}

    monkeypatch.setattr(telemetry_service, "get_single_client_telemetry", mock_telemetry)

    # Test single tenant export
    res = client.get("/admin/reports/trades/export?tenant_id=export_client")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "attachment; filename=trade_book_export_client_" in res.headers["content-disposition"]

    csv_text = res.text
    lines = csv_text.strip().split("\r\n" if "\r\n" in csv_text else "\n")
    assert len(lines) == 3 # Header + 2 rows
    assert "Gross Turnover (INR)" in lines[0]
    assert "Estimated Total Statutory Charges (INR)" in lines[0]
    assert "TR_99182" in lines[1]
    assert "CRUDEOIL24AUGFUT" in lines[1]
    assert "TR_99183" in lines[2]

def test_portal_supertrend_config_and_validation_guard(monkeypatch):
    with TestClient(app) as client:
        # Create dummy admin and session
        enc_payload = security.encrypt_credentials({"CLIENT_ID": "ST01", "WEBHOOK_SECRET": "sec123"})
        with closing(database.get_db_connection()) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) VALUES ('admin_st', 'admin_st', 'hash', 1, 100)")
                conn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('st_tenant', 'ST Test', 'ACTIVE', 100, 100)")
                conn.execute("INSERT OR REPLACE INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('st_tenant', ?, 100)", (enc_payload,))
                conn.execute("INSERT OR REPLACE INTO tenant_risk_limits (tenant_id, updated_at) VALUES ('st_tenant', 100)")

        cookie = security.create_session("admin_st", "127.0.0.1", "pytest-st")

        # 1. Validation Guard: Attempting to enable unconfigured strategy must fail with 400
        res_invalid = client.post("/admin/clients/st_tenant/supertrend/config", data={
            "is_enabled": "true",
            "symbol": "",
            "exchange_segment": "",
            "quantity": 1
        }, cookies={"admin_session": cookie})
        assert res_invalid.status_code == 400
        assert "Please configure and save a trading symbol" in res_invalid.text

        # 2. Save valid configuration
        res_valid = client.post("/admin/clients/st_tenant/supertrend/config", data={
            "is_enabled": "true",
            "symbol": "CRUDEOIL",
            "exchange_segment": "MCXFO",
            "timeframe": "5m",
            "quantity": 2,
            "product_type": "NRML",
            "atr_period": 10,
            "multiplier": 3.0
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_valid.status_code == 303

        with closing(database.get_db_connection()) as conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_configs WHERE tenant_id='st_tenant'").fetchone()
            assert row is not None
            assert row["is_enabled"] == 1
            assert row["is_configured"] == 1
            assert row["symbol"] == "CRUDEOIL"
            assert row["quantity"] == 2

        # 3. Verify client detail view renders 6th tab
        async def mock_fetch_tel(*a, **k):
            return telemetry_service.build_client_telemetry_dict(
                tenant_id="st_tenant",
                name="ST Test",
                status="HEALTHY",
                healthy=True,
                supertrend={"status": "RUNNING", "current_trend": "BULLISH", "atr": 15.2, "symbol": "CRUDEOIL", "timeframe": "5m", "is_enabled": True}
            )
        monkeypatch.setattr(telemetry_service, "fetch_single_client_telemetry", mock_fetch_tel)
        res_detail = client.get("/admin/clients/st_tenant", cookies={"admin_session": cookie})
        assert res_detail.status_code == 200
        assert "SuperTrend Strategy" in res_detail.text
        assert "client-tab-supertrend" in res_detail.text
        assert "st-readiness-container" in res_detail.text

        # 4. Verify HTMX Symbol Validation Endpoint
        res_val = client.get("/admin/clients/st_tenant/supertrend/validate-symbol?symbol=SILVER1001!", cookies={"admin_session": cookie})
        assert res_val.status_code == 200

        # 5. Verify HTMX Live Readiness Diagnostic Partial
        res_readiness = client.get("/admin/clients/st_tenant/supertrend/readiness-partial", cookies={"admin_session": cookie})
        assert res_readiness.status_code == 200
        assert "Live Market Readiness Check" in res_readiness.text

        # 6. Save custom timeframe (e.g. 25m)
        res_custom_tf = client.post("/admin/clients/st_tenant/supertrend/config", data={
            "is_enabled": "true",
            "symbol": "SILVER1001!",
            "exchange_segment": "MCXFO",
            "timeframe_select": "custom",
            "custom_minutes": "25",
            "quantity": 1,
            "product_type": "NRML",
            "atr_period": 10,
            "multiplier": 3.0
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_custom_tf.status_code == 303

        with closing(database.get_db_connection()) as conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_configs WHERE tenant_id='st_tenant'").fetchone()
            assert row["timeframe"] == "25m"
            assert row["symbol"] == "SILVER1001!"

        # 7. Verify Dashboard displays SuperTrend Active Chip
        res_dash = client.get("/admin/dashboard", cookies={"admin_session": cookie})
        assert res_dash.status_code == 200
        assert "ST:" in res_dash.text

def test_save_multi_supertrend_strategy_preserves_15m_and_custom_timeframes():
    with TestClient(app) as client:
        enc_payload = security.encrypt_credentials({"CLIENT_ID": "ST02", "WEBHOOK_SECRET": "sec123"})
        with closing(database.get_db_connection()) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) VALUES ('admin_tf', 'admin_tf', 'hash', 1, 100)")
                conn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('tenant_tf', 'TF Test', 'ACTIVE', 100, 100)")
                conn.execute("INSERT OR REPLACE INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('tenant_tf', ?, 100)", (enc_payload,))
                conn.execute("INSERT OR REPLACE INTO tenant_risk_limits (tenant_id, updated_at) VALUES ('tenant_tf', 100)")
                conn.execute("DELETE FROM tenant_supertrend_strategies WHERE tenant_id='tenant_tf'")

        cookie = security.create_session("admin_tf", "127.0.0.1", "pytest-tf")

        # 1. Save strategy with 15m preset
        res_15m = client.post("/admin/clients/tenant_tf/supertrend/strategy/save", data={
            "symbol": "NATURALGAS1!",
            "exchange_segment": "MCXFO",
            "timeframe_select": "15m",
            "quantity": 2,
            "product_type": "NRML",
            "atr_period": 10,
            "multiplier": 3.0,
            "execution_mode": "LIVE",
            "is_enabled": "true"
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_15m.status_code == 303

        with closing(database.get_db_connection()) as conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_strategies WHERE tenant_id='tenant_tf' AND symbol='NATURALGAS1!'").fetchone()
            assert row is not None
            assert row["timeframe"] == "15m"
            assert row["quantity"] == 2
            assert row["is_enabled"] == 1

        # 2. Save strategy with custom 20m interval
        res_20m = client.post("/admin/clients/tenant_tf/supertrend/strategy/save", data={
            "symbol": "SILVER1001!",
            "exchange_segment": "MCXFO",
            "timeframe_select": "custom",
            "custom_minutes": "20",
            "quantity": 1,
            "product_type": "NRML",
            "atr_period": 10,
            "multiplier": 3.0,
            "execution_mode": "PAPER",
            "is_enabled": "true"
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_20m.status_code == 303

        with closing(database.get_db_connection()) as conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_strategies WHERE tenant_id='tenant_tf' AND symbol='SILVER1001!'").fetchone()
            assert row is not None
            assert row["timeframe"] == "20m"
            assert row["execution_mode"] == "PAPER"

        # 3. Uncheck is_enabled to disable
        res_disable = client.post("/admin/clients/tenant_tf/supertrend/strategy/save", data={
            "symbol": "SILVER1001!",
            "exchange_segment": "MCXFO",
            "timeframe_select": "20m",
            "quantity": 1,
            "product_type": "NRML",
            "execution_mode": "PAPER"
            # is_enabled omitted = False
        }, cookies={"admin_session": cookie}, follow_redirects=False)
        assert res_disable.status_code == 303

        with closing(database.get_db_connection()) as conn:
            row = conn.execute("SELECT * FROM tenant_supertrend_strategies WHERE tenant_id='tenant_tf' AND symbol='SILVER1001!'").fetchone()
            assert row["is_enabled"] == 0




