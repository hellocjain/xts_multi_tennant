import pytest
import sys
import os
import time
from pathlib import Path
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

# Set test environment master key
os.environ["PORTAL_MASTER_KEY"] = Fernet.generate_key().decode()

portal_path = str(Path(__file__).parent.parent / "portal")
if portal_path not in sys.path:
    sys.path.insert(0, portal_path)

import database
import security
import api_gateway
import main as portal_main


@pytest.fixture
def client_authenticated(tmp_path, monkeypatch):
    test_db = str(tmp_path / "portal_parity_test.db")
    monkeypatch.setattr(database, "get_db_path", lambda: test_db)
    database.init_portal_db()

    # Seed test tenant and credentials
    with database.closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("tenant_oa_1", "OpenAlgo Capital Alpha", "ACTIVE", time.time(), time.time())
            )
            enc_creds = security.encrypt_credentials({
                "API_KEY": "KEY_OPENALGO_TEST",
                "API_SECRET": "SECRET_TEST",
                "CLIENT_ID": "OA001",
                "WEBHOOK_SECRET": "WH_SECRET_TEST"
            })
            conn.execute(
                "INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)",
                ("tenant_oa_1", enc_creds, time.time())
            )

    api_gateway.refresh_key_cache(force=True)

    # Create client user
    pass_hash = security.hash_password("OpenAlgo2026!")
    database.create_client_user(
        tenant_id="tenant_oa_1",
        username="oa_trader",
        password_hash=pass_hash,
        email="trader@openalgo.in"
    )

    client = TestClient(portal_main.app)
    # Perform login
    login_res = client.post("/client/login", data={"username": "oa_trader", "password": "OpenAlgo2026!"}, follow_redirects=False)
    assert login_res.status_code == 303
    session_token = login_res.cookies["client_session"]
    client.cookies.set("client_session", session_token)
    return client


def test_theme_switcher_and_nav_links_in_base_template(client_authenticated):
    """Verify that base template contains theme switcher and all 8 OpenAlgo suite nav links."""
    res = client_authenticated.get("/client/dashboard")
    assert res.status_code == 200
    html = res.text

    # Theme Switcher elements
    assert 'oa_theme' in html
    assert 'toggleTheme' in html
    assert 'theme-toggle-btn' in html
    assert 'theme-toggle-icon' in html

    # OpenAlgo navigation links in persistent navbar
    assert '/client/dashboard' in html
    assert '/client/trading' in html
    assert '/client/orders' in html
    assert '/client/positions' in html
    assert '/client/options' in html
    assert '/client/strategies' in html
    assert '/client/logs' in html
    assert '/client/developer' in html


def test_client_orders_page(client_authenticated):
    """Verify dedicated Order Book page (/client/orders) renders correctly with sub-tabs and controls."""
    res = client_authenticated.get("/client/orders")
    assert res.status_code == 200
    html = res.text
    assert "Order Book" in html
    assert "Completed / Filled" in html
    assert "Open / Pending" in html
    assert "Cancelled / Rejected" in html
    assert "Cancel All Pending" in html


def test_client_positions_page(client_authenticated):
    """Verify dedicated Position Book page (/client/positions) renders correctly with MTM & controls."""
    res = client_authenticated.get("/client/positions")
    assert res.status_code == 200
    html = res.text
    assert "Position Book" in html
    assert "Net MTM" in html
    assert ("Realized P&amp;L" in html or "Realized P&L" in html)
    assert "Square Off All" in html


def test_client_options_page(client_authenticated):
    """Verify dedicated Option Chain Suite page (/client/options) renders correctly."""
    res = client_authenticated.get("/client/options")
    assert res.status_code == 200
    html = res.text
    assert "Option Chain Suite" in html
    assert "Put-Call Ratio" in html
    assert "Max Pain" in html
    assert "ATM Strike" in html
    assert "NIFTY 50" in html
    assert "CALLS (CE)" in html
    assert "PUTS (PE)" in html


def test_client_strategies_page(client_authenticated):
    """Verify dedicated Strategy Studio page (/client/strategies) renders correctly."""
    res = client_authenticated.get("/client/strategies")
    assert res.status_code == 200
    html = res.text
    assert "Strategy Studio" in html
    assert "Multi-SuperTrend" in html
    assert "9:20 AM Intraday Straddle" in html
    assert "Opening Range Breakout" in html
    assert "Reset All to Flat" in html


def test_client_logs_page(client_authenticated):
    """Verify dedicated Webhook Logs & Simulator page (/client/logs) renders correctly."""
    res = client_authenticated.get("/client/logs")
    assert res.status_code == 200
    html = res.text
    assert "Webhook Alert Logs" in html
    assert "Send Test Webhook" in html
    assert "In-Browser Webhook Simulator" in html
    assert "All Logs" in html


def test_client_developer_page(client_authenticated):
    """Verify dedicated Developer Hub page (/client/developer) renders with code generators."""
    res = client_authenticated.get("/client/developer")
    assert res.status_code == 200
    html = res.text
    assert ("Developer Hub &amp; API Docs" in html or "Developer Hub & API Docs" in html)
    assert "Your API Secret Key" in html
    assert "KEY_OPENALGO_TEST" in html
    assert "TradingView Pine Script v5" in html
    assert "Python SDK" in html
    assert "AmiBroker AFL" in html
    assert "cURL" in html
    assert "/api/v1/placesmartorder" in html


def test_unauthenticated_client_redirects_to_login():
    """Verify unauthenticated access to client pages redirects to /client/login."""
    client = TestClient(portal_main.app)
    endpoints = [
        "/client/dashboard",
        "/client/trading",
        "/client/orders",
        "/client/positions",
        "/client/options",
        "/client/strategies",
        "/client/logs",
        "/client/developer"
    ]
    for ep in endpoints:
        res = client.get(ep, follow_redirects=False)
        assert res.status_code in (303, 307)
        assert "/client/login" in res.headers["Location"]
