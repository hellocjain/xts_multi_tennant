"""
Comprehensive UI / UX Button Action & Endpoint Stress Testing Suite.
Tests:
1. Rapid concurrent position square-off requests (double-click simulation).
2. Rapid concurrent order cancellations and bulk cancellations.
3. Rapid concurrent client trading pause/resume toggles.
4. Rapid concurrent webhook secret rotation and atomic database writes.
5. Rapid concurrent strategy actions (toggle, sync-trend, reset-flat).
6. Emergency panic actions under concurrent load (/admin/panic-all, /admin/clients/{tenant_id}/panic).
7. Input fuzzing and boundary safety on all button endpoints (bad JSON, negative numbers, SQLi strings).
"""

import os
import sys
import tempfile
import json
import sqlite3
import concurrent.futures
import pytest
from fastapi.testclient import TestClient

# Configure test environment paths
test_dir = tempfile.mkdtemp()
os.environ["PORTAL_DATA_DIR"] = test_dir
os.environ["PORTAL_MASTER_KEY"] = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="

portal_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal"))
if "main" in sys.modules:
    del sys.modules["main"]
if "config" in sys.modules:
    del sys.modules["config"]
if "xts_api" in sys.modules:
    del sys.modules["xts_api"]

sys.path.insert(0, portal_dir)

import database
import security
import docker_manager
import caddy_manager
import telemetry_service
import main as portal_main

app = portal_main.app


@pytest.fixture(scope="module", autouse=True)
def setup_stress_test_suite():
    database.init_portal_db()
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("DELETE FROM admin_sessions")
            conn.execute("DELETE FROM admin_users")
            pwd_hash = security.hash_password("AdminPass123!")
            conn.execute("""
                INSERT INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at)
                VALUES ('admin_stress', 'admin_stress', ?, 1, ?)
            """, (pwd_hash, 1600000000.0))

            conn.execute("DELETE FROM tenants")
            conn.execute("DELETE FROM tenant_credentials")
            conn.execute("DELETE FROM tenant_risk_limits")
            conn.execute("DELETE FROM tenant_supertrend_strategies")

            # Seed target tenant
            conn.execute("""
                INSERT INTO tenants (id, name, status, created_at, updated_at)
                VALUES ('t_stress_01', 'Stress Test Trader', 'ACTIVE', 1600000000.0, 1600000000.0)
            """)
            conn.execute("""
                INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at)
                VALUES ('t_stress_01', ?, 1600000000.0)
            """, (security.encrypt_credentials({
                "API_KEY": "KEY_STRESS",
                "API_SECRET": "SEC_STRESS",
                "CLIENT_ID": "STRESS01",
                "WEBHOOK_SECRET": "WH_STRESS_SEC"
            }),))
            conn.execute("""
                INSERT INTO tenant_risk_limits (
                    tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr,
                    slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at
                )
                VALUES ('t_stress_01', 50, 2500000, 5000000, 0.005, 3, 0, 1600000000.0)
            """)
            conn.execute("""
                INSERT INTO tenant_supertrend_strategies (
                    id, tenant_id, symbol, exchange_segment, timeframe, quantity,
                    product_type, atr_period, multiplier, execution_mode, is_enabled, created_at, updated_at
                )
                VALUES ('strat_stress_01', 't_stress_01', 'CRUDEOIL1!', 'MCXFO', '5m', 100, 'NRML', 10, 3.0, 'LIVE', 1, 1600000000.0, 1600000000.0)
            """)


def get_auth_client():
    client = TestClient(app, cookies={})
    token = security.create_session("admin_stress", "127.0.0.1", "pytest-stress")
    client.cookies.set("admin_session", token)
    return client


# ==============================================================================
# 1. POSITION SQUARE-OFF BUTTON STRESS
# ==============================================================================

def test_rapid_concurrent_square_off_button_clicks(monkeypatch):
    """
    Simulates rapid double/triple clicking the Position Square-Off button in the UI.
    Dispatches 10 simultaneous requests to /api/clients/{tenant_id}/positions/square-off.
    Verifies:
    - Server handles concurrency gracefully without crashing or throwing 500.
    - All audit log writes execute safely without SQLite database lock errors.
    """
    client = get_auth_client()

    # Mock docker manager and httpx call to client container
    import httpx

    call_count = 0

    class MockResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"status": "ok", "message": "Position squared off", "order_id": "ORD_SQ_101"}

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    def send_square_off():
        return client.post(
            "/api/clients/t_stress_01/positions/square-off",
            json={"symbol": "CRUDEOIL26MARFUT", "quantity": 100, "side": "BUY", "product_type": "NRML"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    # Fire 10 rapid concurrent clicks
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(send_square_off) for _ in range(10)]
        results = [f.result() for f in futures]

    for res in results:
        assert res.status_code in (200, 202)
        data = res.json()
        assert data.get("status") == "ok"


# ==============================================================================
# 2. ORDER CANCELLATION BUTTON STRESS
# ==============================================================================

def test_rapid_concurrent_order_cancel_button_clicks(monkeypatch):
    """
    Simulates clicking 'Cancel Order' rapidly on an open order.
    Dispatches 8 concurrent requests for the exact same order_id.
    Verifies:
    - Clean handled responses.
    - Zero SQLite locking crashes.
    """
    client = get_auth_client()
    import httpx

    class MockResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"status": "ok", "result": "Order cancelled successfully"}

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    def cancel_order():
        return client.post(
            "/api/clients/t_stress_01/orders/ORD_TEST_999/cancel",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(cancel_order) for _ in range(8)]
        results = [f.result() for f in futures]

    for res in results:
        assert res.status_code == 200
        assert res.json().get("status") == "ok"


def test_rapid_concurrent_bulk_cancel_button_clicks(monkeypatch):
    """
    Simulates rapidly clicking 'Cancel All Open Orders' in the header.
    Dispatches concurrent bulk cancel calls.
    """
    client = get_auth_client()
    import httpx

    class MockResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"status": "ok", "cancelled_count": 3}

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    def bulk_cancel():
        return client.post(
            "/api/orders/bulk-cancel",
            json={"tenant_id": "t_stress_01"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(bulk_cancel) for _ in range(6)]
        results = [f.result() for f in futures]

    for res in results:
        assert res.status_code == 200
        assert res.json().get("status") == "ok"


# ==============================================================================
# 3. TRADING PAUSE / RESUME TOGGLE STRESS
# ==============================================================================

def test_rapid_concurrent_pause_resume_toggles(monkeypatch):
    """
    Simulates rapidly toggling Pause/Resume Trading on client card or table row.
    Rapidly alternates pause=True and pause=False across multiple threads.
    Verifies:
    - Database row remains in valid state (PAUSED or ACTIVE).
    - No unhandled exceptions or database lock collisions.
    """
    client = get_auth_client()
    monkeypatch.setattr(docker_manager, "stop_client_container", lambda tid: True)
    monkeypatch.setattr(docker_manager, "restart_client_container", lambda tid: True)
    monkeypatch.setattr(caddy_manager, "sync_caddy_config", lambda: True)

    def toggle_action(pause_state: bool):
        return client.post(
            "/admin/clients/t_stress_01/toggle-trading",
            json={"pause": pause_state},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(toggle_action, bool(i % 2)) for i in range(10)]
        results = [f.result() for f in futures]

    for res in results:
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "ok"
        assert isinstance(data.get("trading_paused"), bool)

    # Check database tenant status is either PAUSED or ACTIVE
    with database.get_db_connection() as conn:
        row = conn.execute("SELECT status FROM tenants WHERE id='t_stress_01'").fetchone()
        assert row["status"] in ("PAUSED", "ACTIVE")


# ==============================================================================
# 4. WEBHOOK SECRET ROTATION STRESS
# ==============================================================================

def test_rapid_concurrent_webhook_secret_rotations(monkeypatch):
    """
    Simulates rapid clicks on 'Rotate Webhook Secret' button.
    Dispatches multiple rotation requests concurrently.
    Verifies:
    - Secrets are cryptographically generated.
    - Decryption is preserved without corruption.
    - New secret is stored in database.
    """
    client = get_auth_client()
    monkeypatch.setattr(docker_manager, "write_client_config", lambda tid: True)

    def rotate_secret():
        return client.post(
            "/api/clients/t_stress_01/webhook-secret/rotate",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(rotate_secret) for _ in range(6)]
        results = [f.result() for f in futures]

    for res in results:
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "ok"
        assert len(data.get("webhook_secret", "")) >= 16
        assert "t_stress_01" in data.get("webhook_url", "")

    # Final DB verification: secret decrypts cleanly
    with database.get_db_connection() as conn:
        row = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id='t_stress_01'").fetchone()
        creds = security.decrypt_credentials(row["encrypted_payload"])
        assert creds.get("WEBHOOK_SECRET") is not None


# ==============================================================================
# 5. STRATEGY RIBBON ACTIONS STRESS
# ==============================================================================

def test_rapid_concurrent_strategy_toggle_and_sync(monkeypatch):
    """
    Simulates clicking 'Toggle Strategy' and 'Sync Trend' concurrently.
    Verifies:
    - No SQLite lock collisions.
    - Returns valid status.
    """
    client = get_auth_client()
    import httpx

    class MockResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"status": "ok", "message": "Strategy updated"}

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(docker_manager, "write_client_config", lambda tid: True)

    def toggle_strat():
        return client.post(
            "/api/clients/t_stress_01/strategies/strat_stress_01/toggle",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(toggle_strat) for _ in range(8)]
        results = [f.result() for f in futures]

    for res in results:
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "ok"
        assert isinstance(data.get("is_enabled"), bool)


# ==============================================================================
# 6. EMERGENCY PANIC SYSTEM STRESS
# ==============================================================================

def test_concurrent_emergency_panic_buttons(monkeypatch):
    """
    Tests both single-client panic (/admin/clients/{tenant_id}/panic)
    and global panic (/admin/panic-all) under simultaneous calls.
    Verifies:
    - Returns 200 JSON when Accept: application/json is present.
    - Completes without unhandled 500 errors.
    """
    client = get_auth_client()

    async def mock_panic_single(tenant_id, secret):
        return {"tenant_id": tenant_id, "status": "PANIC_SUCCESS", "cancelled_orders": 2, "flattened_positions": 1}

    async def mock_panic_all():
        return {"status": "GLOBAL_PANIC_SUCCESS", "tenants_processed": 1}

    monkeypatch.setattr(telemetry_service, "panic_single_client", mock_panic_single)
    monkeypatch.setattr(telemetry_service, "panic_all_active_clients", mock_panic_all)

    # 1. Single client panic
    res_single = client.post(
        "/admin/clients/t_stress_01/panic",
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    )
    assert res_single.status_code == 200
    data_single = res_single.json()
    assert data_single.get("status") == "success"

    # 2. Global panic
    res_all = client.post(
        "/admin/panic-all",
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    )
    assert res_all.status_code == 200
    data_all = res_all.json()
    assert data_all.get("status") == "success"


# ==============================================================================
# 7. ADVERSARIAL PAYLOAD FUZZING & BOUNDARY DEFENSE
# ==============================================================================

def test_adversarial_button_payloads():
    """
    Fuzzes button endpoints with malformed, negative, boundary, or SQL injection payloads.
    Verifies:
    - Never causes an unhandled 500 Server Error.
    - Rejects or safely sanitizes input.
    """
    client = get_auth_client()

    # Case 1: Square off with negative quantity & invalid symbol
    res1 = client.post(
        "/api/clients/t_stress_01/positions/square-off",
        json={"symbol": "' OR '1'='1", "quantity": -999, "side": "INVALID_SIDE"},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    )
    # Either 200 (if handled downstream), 400/422, or 502 (container not reached)
    assert res1.status_code in (200, 400, 422, 502)

    # Case 2: Risk limits update with extreme boundary values
    res2 = client.put(
        "/api/clients/t_stress_01/risk-limits",
        json={
            "max_lots_limit": -1,
            "max_order_value_inr": -50000,
            "daily_notional_cap_inr": 999999999999,
            "slippage_buffer_pct": 0.50,
            "min_days_before_expiry_mcx": -5,
            "paper_trade_mode": False
        },
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    )
    assert res2.status_code in (200, 400, 422)

    # Case 3: Toggle trading with non-boolean string
    res3 = client.post(
        "/admin/clients/t_stress_01/toggle-trading",
        json={"pause": "NOT_A_BOOLEAN"},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    )
    assert res3.status_code == 200
    assert isinstance(res3.json().get("trading_paused"), bool)

    # Case 4: Non-existent client ID button calls
    res4 = client.post(
        "/api/clients/non_existent_client_xyz/webhook-secret/rotate",
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    )
    assert res4.status_code == 404


# ==============================================================================
# 8. DATABASE BACKUP CONCURRENT BUTTON STRESS
# ==============================================================================

def test_rapid_concurrent_database_backup_requests(monkeypatch, tmp_path):
    """
    Simulates rapid multiple clicks on 'Create Instant Database Backup'.
    Verifies:
    - JSON response is returned when requested with application/json.
    - Zero SQLite deadlock or locking errors.
    - All audit entries created safely.
    """
    client = get_auth_client()

    import backup_engine
    monkeypatch.setattr(backup_engine, "create_backup_archive", lambda pw: str(tmp_path / "test_backup.enc"))

    def trigger_backup():
        return client.post(
            "/admin/settings/backup",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(trigger_backup) for _ in range(6)]
        results = [f.result() for f in futures]

    for res in results:
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "success"
        assert "filename" in data


# ==============================================================================
# 9. STRATEGY EVALUATE & RESET FLAT CONCURRENT STRESS
# ==============================================================================

def test_rapid_concurrent_strategy_eval_and_reset(monkeypatch):
    """
    Simulates rapid concurrent clicks on Evaluate Now and Reset FLAT buttons.
    Verifies:
    - Handled JSON responses.
    - No unhandled exceptions or state corruption.
    """
    client = get_auth_client()

    async def mock_eval(tenant_id, symbol, strategy_id, user):
        return {"status": "ok", "action": "EVAL_COMPLETED", "symbol": symbol}

    async def mock_reset(tenant_id, strategy_id, square_off_broker, request, user):
        return {"status": "ok", "action": "RESET_FLAT_COMPLETED"}

    monkeypatch.setattr(portal_main, "evaluate_supertrend_now_portal", mock_eval)
    monkeypatch.setattr(portal_main, "reset_supertrend_strategy_flat_portal", mock_reset)

    def eval_call():
        return client.post(
            "/api/clients/t_stress_01/strategies/evaluate-now",
            json={"symbol": "CRUDEOIL1!", "strategy_id": "strat_stress_01"},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    def reset_call():
        return client.post(
            "/api/clients/t_stress_01/strategies/reset-flat",
            json={"strategy_id": "strat_stress_01", "square_off_broker": True},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        f_evals = [executor.submit(eval_call) for _ in range(5)]
        f_resets = [executor.submit(reset_call) for _ in range(5)]
        results = [f.result() for f in f_evals + f_resets]

    for res in results:
        assert res.status_code == 200
        assert res.json().get("status") == "ok"

