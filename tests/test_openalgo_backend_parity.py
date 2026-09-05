"""
tests/test_openalgo_backend_parity.py - Parity Tests for OpenAlgo Backend & UI Enhancements.
Validates:
1. Pre-Trade Margin Calculator (Equity CNC/MIS, Option Long/Short, Basket Spread Hedge Benefits)
2. Tri-State Trading Mode (LIVE / PAPER / ANALYZER) and Signal Analyzer Zero-Risk Pipeline
3. Multi-Channel Notification Broadcaster (Telegram, Discord, MTM Summary)
4. Frontend Parity: Command+K search modal, Mode Switcher dropdown, Telegram Notification Card
"""
import pytest
import os
import sys
import importlib.util
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

BASE_DIR = str(Path(__file__).parent.parent)
client_path = os.path.join(BASE_DIR, "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

# Explicitly isolate client/main.py from portal/main.py
client_main_file = os.path.join(client_path, "main.py")
spec = importlib.util.spec_from_file_location("client_main_isolated_parity", client_main_file)
client_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_main)

import config
import openalgo_router
import margin_service
import notification_service

# Test credentials
TEST_API_KEY = "TEST_OPENALGO_KEY_999"

AUTH_HEADERS = {
    "X-API-KEY": TEST_API_KEY,
    "Content-Type": "application/json"
}


@pytest.fixture
def client_app(monkeypatch):
    """Provides isolated TestClient wired directly to client_main.app with mock authentication."""
    monkeypatch.setattr(config, "API_KEY", TEST_API_KEY)
    monkeypatch.setattr(config, "WEBHOOK_SECRET", TEST_API_KEY)
    monkeypatch.setattr(openalgo_router.config, "API_KEY", TEST_API_KEY)
    monkeypatch.setattr(openalgo_router.config, "WEBHOOK_SECRET", TEST_API_KEY)
    return TestClient(client_main.app)


@pytest.fixture(autouse=True)
def reset_operational_state(monkeypatch):
    """Resets operational mode and signals registry between test runs."""
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", True)
    monkeypatch.setattr(config, "ANALYZER_MODE", False)
    monkeypatch.setattr(openalgo_router.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(openalgo_router.config, "PAPER_TRADE_MODE", True)
    monkeypatch.setattr(openalgo_router.config, "ANALYZER_MODE", False)
    openalgo_router._ANALYZER_SIGNALS.clear()
    openalgo_router._ORDERS_REGISTRY.clear()
    yield
    openalgo_router.set_current_trading_mode("PAPER")
    config.TRADING_MODE = "PAPER"
    config.PAPER_TRADE_MODE = True
    config.ANALYZER_MODE = False
    openalgo_router.config.TRADING_MODE = "PAPER"
    openalgo_router.config.PAPER_TRADE_MODE = True
    openalgo_router.config.ANALYZER_MODE = False
    openalgo_router._ANALYZER_SIGNALS.clear()
    openalgo_router._ORDERS_REGISTRY.clear()


# =====================================================================
# 1. Pre-Trade Regulatory Margin Calculator Tests
# =====================================================================

def test_margin_calculator_equity_cnc_and_mis():
    """Equity CNC requires 100% capital; MIS allows 5x leverage (20% margin)."""
    # CNC Delivery
    cnc_res = margin_service.calculate_order_margin(
        symbol="RELIANCE",
        action="BUY",
        quantity=10,
        price=3000.0,
        product="CNC",
        exchange="NSE"
    )
    assert cnc_res["notional_value"] == 30000.0
    assert cnc_res["margin_required"] == 30000.0
    assert cnc_res["leverage"] == 1.0

    # MIS Intraday (5x leverage)
    mis_res = margin_service.calculate_order_margin(
        symbol="RELIANCE",
        action="BUY",
        quantity=10,
        price=3000.0,
        product="MIS",
        exchange="NSE"
    )
    assert mis_res["notional_value"] == 30000.0
    assert mis_res["margin_required"] == 6000.0  # 20% of 30,000
    assert mis_res["leverage"] == 5.0


def test_margin_calculator_option_buy_vs_sell():
    """Option buying requires 100% premium; Option selling requires SPAN + Exposure."""
    # Long Option (Buying CE)
    long_res = margin_service.calculate_order_margin(
        symbol="NIFTY24SEP25000CE",
        action="BUY",
        quantity=50,
        price=120.0,
        product="NRML",
        exchange="NFO"
    )
    assert long_res["notional_value"] == 6000.0  # 50 * 120
    assert long_res["margin_required"] == 6000.0  # 100% premium
    assert long_res["instrument_type"] == "OPT"

    # Short Option (Selling CE)
    short_res = margin_service.calculate_order_margin(
        symbol="NIFTY24SEP25000CE",
        action="SELL",
        quantity=50,
        price=120.0,
        product="NRML",
        exchange="NFO"
    )
    # Short option requires SPAN + Exposure (at 16% notional + premium)
    assert short_res["margin_required"] > 6000.0
    assert short_res["margin_required"] > 100000.0  # Industry standard index writing margin


def test_margin_calculator_basket_hedged_spread_benefit():
    """Multi-leg spread (Bull Call Spread) applies up to 70% hedge discount on short margin."""
    basket = [
        # Leg 1: Long Call (Protective Hedge)
        {"symbol": "NIFTY24SEP24800CE", "action": "BUY", "quantity": 50, "price": 250.0, "product": "NRML", "exchange": "NFO"},
        # Leg 2: Short Call (Sold leg)
        {"symbol": "NIFTY24SEP25200CE", "action": "SELL", "quantity": 50, "price": 80.0, "product": "NRML", "exchange": "NFO"},
    ]

    result = margin_service.calculate_basket_margin(basket, available_funds=500000.0)
    assert result["status"] == "success"
    assert result["initial_margin"] > 0
    assert result["hedged_benefit"] > 0
    assert result["total_margin_required"] < result["initial_margin"]
    assert result["can_place"] is True
    assert len(result["items"]) == 2


def test_margin_api_endpoint_single_and_basket(client_app):
    """Validates /api/v1/margin endpoint with single order and multi-leg basket."""
    # Single order
    resp_single = client_app.post("/api/v1/margin", json={
        "apikey": TEST_API_KEY,
        "symbol": "TCS",
        "action": "BUY",
        "quantity": 5,
        "price": 4000.0,
        "product": "MIS"
    }, headers=AUTH_HEADERS)
    assert resp_single.status_code == 200
    data = resp_single.json()
    assert data["status"] == "success"
    assert data["margin_required"] == 4000.0  # 20% of 20,000
    assert data["can_place"] is True

    # Basket orders
    resp_basket = client_app.post("/api/v1/margincalculator", json={
        "apikey": TEST_API_KEY,
        "orders": [
            {"symbol": "NIFTY24SEP25000CE", "action": "BUY", "quantity": 50, "price": 100.0},
            {"symbol": "NIFTY24SEP25500CE", "action": "SELL", "quantity": 50, "price": 30.0}
        ]
    }, headers=AUTH_HEADERS)
    assert resp_basket.status_code == 200
    basket_data = resp_basket.json()
    assert basket_data["status"] == "success"
    assert basket_data["hedged_benefit"] > 0
    assert basket_data["total_margin_required"] < basket_data["initial_margin"]


# =====================================================================
# 2. Tri-State Trading Mode & Signal Analyzer Tests
# =====================================================================

def test_analyzer_endpoint_get_and_post(client_app):
    """Tests GET & POST /api/v1/analyzer and mode switching."""
    # Initial state
    resp = client_app.get("/api/v1/analyzer")
    assert resp.status_code == 200
    assert resp.json()["analyzer"] is False

    # Enable analyzer mode
    post_resp = client_app.post("/api/v1/analyzer", json={
        "apikey": TEST_API_KEY,
        "analyzer": True
    }, headers=AUTH_HEADERS)
    assert post_resp.status_code == 200
    assert post_resp.json()["analyzer"] is True
    assert post_resp.json()["mode"] == "ANALYZER"

    # Query mode endpoint
    mode_resp = client_app.get("/api/v1/mode")
    assert mode_resp.status_code == 200
    assert mode_resp.json()["mode"] == "ANALYZER"

    # Switch back to LIVE or PAPER
    switch_resp = client_app.post("/api/v1/mode", json={
        "apikey": TEST_API_KEY,
        "mode": "LIVE"
    }, headers=AUTH_HEADERS)
    assert switch_resp.status_code == 200
    assert switch_resp.json()["mode"] == "LIVE"
    assert openalgo_router.get_current_trading_mode() == "LIVE"


def test_analyzer_mode_blocks_broker_execution_and_logs_signals(client_app):
    """In ANALYZER mode, orders are recorded as hypothetical signals with zero broker impact."""
    # Put engine into ANALYZER mode
    openalgo_router.set_current_trading_mode("ANALYZER")

    with patch("xts_api.execute_trade_with_retry") as mock_exec, \
         patch("xts_api.place_order") as mock_place:
        
        resp = client_app.post("/api/v1/placeorder", json={
            "apikey": TEST_API_KEY,
            "symbol": "INFY",
            "action": "BUY",
            "quantity": 25,
            "price": 1850.0
        }, headers=AUTH_HEADERS)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["mode"] == "ANALYZER"
        assert "ANALYZER_" in data["orderid"]

        # Crucial: verify broker execution functions were NEVER invoked!
        mock_exec.assert_not_called()
        mock_place.assert_not_called()

        # Verify signal was registered
        signals = openalgo_router._ANALYZER_SIGNALS
        assert len(signals) == 1
        assert signals[0]["symbol"] == "INFY"
        assert signals[0]["action"] == "BUY"
        assert signals[0]["quantity"] == 25


# =====================================================================
# 3. Multi-Channel Notification Broadcaster Tests
# =====================================================================

@pytest.mark.asyncio
async def test_notification_service_format_and_dispatch():
    """Validates order execution receipt and daily MTM card formatting."""
    # Test Executed order receipt
    res_exec = await notification_service.notify_order_execution(
        tenant_id="tenant_alpha",
        order_data={"symbol": "NIFTY24SEP25000CE", "action": "BUY", "quantity": 50, "price": 125.0},
        status="COMPLETE",
        execution_price=125.0,
        app_order_id="ORD_98765"
    )
    assert res_exec["status"] == "success"
    assert "ORDER EXECUTED" in res_exec["message"]
    assert "NIFTY24SEP25000CE" in res_exec["message"]
    assert "ORD_98765" in res_exec["message"]

    # Test Rejected order receipt
    res_rej = await notification_service.notify_order_execution(
        tenant_id="tenant_alpha",
        order_data={"symbol": "BANKNIFTY", "action": "SELL", "quantity": 15, "price": 51000.0, "rejection_reason": "Insufficient Margin"},
        status="REJECTED",
        execution_price=51000.0,
        app_order_id="ORD_FAIL"
    )
    assert "ORDER REJECTED" in res_rej["message"]
    assert "Insufficient Margin" in res_rej["message"]

    # Test Daily MTM summary
    res_mtm = await notification_service.notify_daily_mtm_summary(
        tenant_id="tenant_alpha",
        net_mtm=14520.50,
        realized_pnl=12000.0,
        open_positions_count=2
    )
    assert res_mtm["status"] == "success"
    assert "DAILY TRADING SUMMARY" in res_mtm["message"]
    assert "₹14,520.50" in res_mtm["message"]


@pytest.mark.asyncio
async def test_notification_service_send_test_alert_with_mock():
    """Tests send_test_alert connectivity validation."""
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        res = await notification_service.send_test_alert(
            bot_token="123456:FAKE_TOKEN",
            chat_id="99887766"
        )
        assert res["status"] == "success"
        assert "delivered successfully" in res["message"]


# =====================================================================
# 4. Frontend Parity & DOM Elements Tests
# =====================================================================

def test_command_k_search_modal_and_mode_switcher_in_base_template():
    """Verifies that base.html renders the Command+K search modal and Mode Switcher dropdown."""
    template_path = os.path.join(BASE_DIR, "portal", "templates", "base.html")
    with open(template_path, "r") as f:
        content = f.read()

    # Mode switcher checks
    assert "mode-switcher-container" in content
    assert "mode-switcher-btn" in content
    assert "mode-dropdown-menu" in content
    assert "switchTradingMode" in content
    assert "LIVE Broker" in content
    assert "PAPER Trade" in content
    assert "ANALYZER" in content

    # Command+K search modal checks
    assert "cmdk-modal-backdrop" in content
    assert "cmdk-search-input" in content
    assert "cmdk-results-list" in content
    assert "openCmdKSearch" in content
    assert "closeCmdKSearch" in content
    assert "handleCmdKInput" in content
    assert "OpenAlgo Master Index" in content


def test_telegram_notification_card_in_developer_template():
    """Verifies that client_developer.html contains Telegram configuration and test ping button."""
    template_path = os.path.join(BASE_DIR, "portal", "templates", "client_developer.html")
    with open(template_path, "r") as f:
        content = f.read()

    assert "telegram_bot_token" in content
    assert "telegram_chat_id" in content
    assert "discord_webhook_url" in content
    assert "sendTestNotification" in content
    assert "Send Test Notification" in content
    assert "/api/v1/margin" in content
    assert "/api/v1/analyzer" in content
    assert "/api/v1/mode" in content


# =====================================================================
# 5. Portal Client Dynamic Endpoints Tests
# =====================================================================

def test_portal_set_trading_mode_and_notification_routes(tmp_path, monkeypatch):
    """Tests /client/set-trading-mode, /client/notification-settings, and /client/test-notification in portal."""
    import time
    from cryptography.fernet import Fernet
    os.environ["PORTAL_MASTER_KEY"] = Fernet.generate_key().decode()
    monkeypatch.setenv("PORTAL_DATA_DIR", str(tmp_path / "portal_root"))
    monkeypatch.setenv("CLIENT_DATA_ROOT", str(tmp_path / "client_data"))

    portal_dir = os.path.join(BASE_DIR, "portal")
    if portal_dir not in sys.path:
        sys.path.insert(0, portal_dir)

    import database
    import security
    import api_gateway
    import main as portal_main

    test_db = str(tmp_path / "portal_backend_parity.db")
    monkeypatch.setattr(database, "get_db_path", lambda: test_db)
    database.init_portal_db()

    # Seed test tenant
    with database.closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("tenant_test_backend", "Test Parity Corp", "ACTIVE", time.time(), time.time())
            )
            enc = security.encrypt_credentials({"API_KEY": "KEY123", "API_SECRET": "SEC123"})
            conn.execute(
                "INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)",
                ("tenant_test_backend", enc, time.time())
            )
            conn.execute(
                "INSERT INTO tenant_risk_limits (tenant_id, paper_trade_mode, updated_at) VALUES (?, 1, ?)",
                ("tenant_test_backend", time.time())
            )

    pass_hash = security.hash_password("Pass1234!")
    database.create_client_user(
        tenant_id="tenant_test_backend",
        username="parity_trader",
        password_hash=pass_hash,
        email="trader@test.com"
    )

    client = TestClient(portal_main.app)
    login_res = client.post("/client/login", data={"username": "parity_trader", "password": "Pass1234!"}, follow_redirects=False)
    session_token = login_res.cookies["client_session"]
    client.cookies.set("client_session", session_token)

    # 1. Test set-trading-mode
    mode_res = client.post("/client/set-trading-mode", json={"mode": "ANALYZER"})
    assert mode_res.status_code == 200
    assert mode_res.json()["status"] == "success"
    assert mode_res.json()["mode"] == "ANALYZER"

    # Verify updated in database
    with database.closing(database.get_db_connection()) as conn:
        row = conn.execute("SELECT trading_mode FROM tenant_risk_limits WHERE tenant_id=?", ("tenant_test_backend",)).fetchone()
        assert row["trading_mode"] == "ANALYZER"

    # 2. Test notification-settings
    save_notif_res = client.post("/client/notification-settings", data={
        "telegram_bot_token": "987654:BOT_TOKEN",
        "telegram_chat_id": "12345678",
        "discord_webhook_url": "https://discord.com/api/webhooks/test"
    }, follow_redirects=False)
    assert save_notif_res.status_code == 303

    # Verify saved in database
    with database.closing(database.get_db_connection()) as conn:
        r_row = conn.execute("SELECT telegram_bot_token, telegram_chat_id FROM tenant_risk_limits WHERE tenant_id=?", ("tenant_test_backend",)).fetchone()
        assert r_row["telegram_bot_token"] == "987654:BOT_TOKEN"
        assert r_row["telegram_chat_id"] == "12345678"

    # 3. Test test-notification
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "success", "message": "Delivered"}
        mock_post.return_value = mock_resp

        test_alert_res = client.post("/client/test-notification", json={})
        assert test_alert_res.status_code == 200
        assert test_alert_res.json()["status"] == "success"

