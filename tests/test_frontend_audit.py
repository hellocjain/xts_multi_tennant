import os
import sys
import tempfile
import json
import sqlite3
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
def setup_test_suite():
    database.init_portal_db()
    # Seed admin user
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("DELETE FROM admin_sessions")
            conn.execute("DELETE FROM admin_users")
            pwd_hash = security.hash_password("AdminPass123!")
            conn.execute("""
                INSERT INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at)
                VALUES ('admin_01', 'admin', ?, 1, ?)
            """, (pwd_hash, 1600000000.0))
            
            # Seed diverse mock tenants
            conn.execute("DELETE FROM tenants")
            conn.execute("DELETE FROM tenant_credentials")
            conn.execute("DELETE FROM tenant_risk_limits")
            
            # Tenant 1: Live, in profit
            conn.execute("""
                INSERT INTO tenants (id, name, status, created_at, updated_at)
                VALUES ('t_live_profit', 'Live Trader Pro', 'ACTIVE', 1600000000.0, 1600000000.0)
            """)
            conn.execute("""
                INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at)
                VALUES ('t_live_profit', ?, 1600000000.0)
            """, (security.encrypt_credentials({
                "API_KEY": "KEY1", "API_SECRET": "SEC1", "CLIENT_ID": "DM933", "WEBHOOK_SECRET": "WHSEC1"
            }),))
            conn.execute("""
                INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at)
                VALUES ('t_live_profit', 50, 2500000, 5000000, 0.005, 3, 0, 1600000000.0)
            """)

            # Tenant 2: Paper, in loss
            conn.execute("""
                INSERT INTO tenants (id, name, status, created_at, updated_at)
                VALUES ('t_paper_loss', 'Paper Sandbox Account', 'ACTIVE', 1600000000.0, 1600000000.0)
            """)
            conn.execute("""
                INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at)
                VALUES ('t_paper_loss', ?, 1600000000.0)
            """, (security.encrypt_credentials({
                "API_KEY": "KEY2", "API_SECRET": "SEC2", "CLIENT_ID": "PAPER99", "WEBHOOK_SECRET": "WHSEC2"
            }),))
            conn.execute("""
                INSERT INTO tenant_risk_limits (tenant_id, max_lots_limit, max_order_value_inr, daily_notional_cap_inr, slippage_buffer_pct, min_days_before_expiry_mcx, paper_trade_mode, updated_at)
                VALUES ('t_paper_loss', 100, 5000000, 10000000, 0.005, 3, 1, 1600000000.0)
            """)

            # Tenant 3: Fresh Empty Account (missing risk row test)
            conn.execute("""
                INSERT INTO tenants (id, name, status, created_at, updated_at)
                VALUES ('t_fresh_empty', 'Brand New Account', 'ACTIVE', 1600000000.0, 1600000000.0)
            """)
            conn.execute("""
                INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at)
                VALUES ('t_fresh_empty', ?, 1600000000.0)
            """, (security.encrypt_credentials({
                "API_KEY": "KEY3", "API_SECRET": "SEC3", "CLIENT_ID": "EMPTY01", "WEBHOOK_SECRET": "WHSEC3"
            }),))

    # Seed mock client data files
    client_data_root = docker_manager.get_client_data_root()
    os.makedirs(client_data_root, exist_ok=True)
    
    # 1. Populate t_live_profit state
    t1_dir = os.path.join(client_data_root, "t_live_profit")
    os.makedirs(t1_dir, exist_ok=True)
    with open(os.path.join(t1_dir, "positions.json"), "w") as f:
        json.dump([
            {
                "symbol": "CRUDEOIL26MARFUT",
                "quantity": 200,
                "side": "BUY",
                "buy_avg_price": 6250.0,
                "sell_avg_price": 0.0,
                "ltp": 6310.0,
                "unrealized_mtm": 12000.0,
                "product_type": "NRML"
            }
        ], f)
    with open(os.path.join(t1_dir, "margin.json"), "w") as f:
        json.dump({
            "available_margin": 450000.0,
            "margin_used": 150000.0,
            "total_collateral": 100000.0,
            "net_margin_available": 400000.0,
            "total_account_value": 600000.0
        }, f)
    
    # Populate signals.db for t_live_profit
    sig_db_path = os.path.join(t1_dir, "signals.db")
    with sqlite3.connect(sig_db_path) as s_conn:
        s_conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at REAL NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        s_conn.execute("""
            INSERT INTO signals (received_at, payload_json, result_json, status)
            VALUES (?, ?, ?, ?)
        """, (
            1600001000.0,
            json.dumps({"action": "BUY", "symbol": "CRUDEOIL26MARFUT", "quantity": 100, "price": 6250.0, "order_ref": "ORD_REF_1001"}),
            json.dumps({"status": "filled", "order_id": "SYM_991823"}),
            "done"
        ))

    yield

def get_auth_client():
    client = TestClient(app, cookies={})
    token = security.create_session("admin_01", "127.0.0.1", "pytest-agent")
    client.cookies.set("admin_session", token)
    return client

# ==============================================================================
# AUDIT TESTS
# ==============================================================================

@pytest.fixture(autouse=True)
def mock_telemetry(monkeypatch):
    async def mock_fetch(client_session, tenant):
        t_id = tenant["id"]
        if t_id == "t_live_profit":
            return telemetry_service.build_client_telemetry_dict(
                tenant_id=t_id,
                name="Live Trader Pro",
                client_id="DM933",
                status="HEALTHY",
                docker_status="RUNNING",
                healthy=True,
                paper_mode=False,
                unrealized_mtm=12000.0,
                realized_pnl=4500.0,
                net_mtm=16500.0,
                positions=[
                    {
                        "symbol": "CRUDEOIL26MARFUT",
                        "segment": "MCX",
                        "side": "LONG",
                        "quantity": 200,
                        "buy_avg": 6250.0,
                        "sell_avg": 0.0,
                        "ltp": 6310.0,
                        "unrealized_mtm": 12000.0,
                        "realized_pnl": 0.0,
                        "product_type": "NRML"
                    }
                ],
                available_margin=450000.0,
                margin_used=150000.0,
                total_collateral=100000.0,
                net_margin_available=400000.0,
                total_account_value=600000.0,
                margin_pct=25.0,
                notional_today=1250000.0,
                notional_cap=5000000.0,
                notional_pct=25.0,
                recent_signals=[
                    {
                        "received_at": 1600001000.0,
                        "payload": {"action": "BUY", "symbol": "CRUDEOIL26MARFUT", "quantity": 100, "price": 6250.0, "order_ref": "ORD_REF_1001"},
                        "result": {"status": "filled", "order_id": "SYM_991823"},
                        "status": "done"
                    }
                ]
            )
        elif t_id == "t_paper_loss":
            return telemetry_service.build_client_telemetry_dict(
                tenant_id=t_id,
                name="Paper Sandbox Account",
                client_id="PAPER99",
                status="HEALTHY",
                docker_status="RUNNING",
                healthy=True,
                paper_mode=True,
                unrealized_mtm=-3500.0,
                realized_pnl=-1500.0,
                net_mtm=-5000.0,
                positions=[],
                available_margin=95000.0,
                margin_used=5000.0,
                total_collateral=0.0,
                net_margin_available=95000.0,
                total_account_value=100000.0,
                margin_pct=5.0,
                notional_today=50000.0,
                notional_cap=10000000.0,
                notional_pct=0.5,
                recent_signals=[]
            )
        else:
            return telemetry_service.build_client_telemetry_dict(
                tenant_id=t_id,
                name=tenant.get("name", t_id),
                client_id="EMPTY01",
                status="HEALTHY",
                docker_status="RUNNING",
                healthy=True,
                paper_mode=False,
                positions=[],
                recent_signals=[]
            )
    monkeypatch.setattr(telemetry_service, "fetch_single_client_telemetry", mock_fetch)

def test_login_page_renders():
    client = TestClient(app)
    res = client.get("/admin/login")
    assert res.status_code == 200
    assert "Institutional Admin Gateway" in res.text
    assert "lucide" in res.text

def test_dashboard_full_page():
    client = get_auth_client()
    res = client.get("/admin/dashboard")
    assert res.status_code == 200
    # Check KPI metrics container
    assert "Total Net P&amp;L Today" in res.text or "Total Net P&L Today" in res.text
    assert "Trading Engines Health" in res.text
    assert "Broker Capital Deployed" in res.text
    assert "Turnover &amp; Exposure" in res.text or "Turnover & Exposure" in res.text
    # Check Lucide icons initialization
    assert "lucide.createIcons()" in res.text
    # Check 2-step Global Panic Modal presence
    assert 'id="global-panic-modal"' in res.text
    # Check Cards and Table view containers
    assert 'id="cardsViewContainer"' in res.text
    assert 'id="denseTableViewContainer"' in res.text

def test_dashboard_partial_htmx_stream():
    client = get_auth_client()
    res = client.get("/admin/dashboard-partial")
    assert res.status_code == 200
    assert "Live Trader Pro" in res.text
    assert "Paper Sandbox Account" in res.text
    assert "Brand New Account" in res.text

def test_client_detail_5_tab_layout_live_account():
    client = get_auth_client()
    res = client.get("/admin/clients/t_live_profit")
    assert res.status_code == 200
    # Tab navigation buttons
    assert 'id="tab-nav-positions"' in res.text
    assert 'id="tab-nav-webhook"' in res.text
    assert 'id="tab-nav-risk"' in res.text
    assert 'id="tab-nav-orders"' in res.text
    assert 'id="tab-nav-logs"' in res.text
    # Tab content panes
    assert 'id="client-tab-positions"' in res.text
    assert 'id="client-tab-webhook"' in res.text
    assert 'id="client-tab-risk"' in res.text
    assert 'id="client-tab-orders"' in res.text
    assert 'id="client-tab-logs"' in res.text
    # Check positions table
    assert "CRUDEOIL26MARFUT" in res.text
    # Check 2-step panic modal
    assert 'id="client-panic-modal"' in res.text
    assert 'id="client-delete-modal"' in res.text

def test_client_detail_fresh_empty_account():
    """Verify zero crashes when an account has no positions, no signals, and no custom risk row."""
    client = get_auth_client()
    res = client.get("/admin/clients/t_fresh_empty")
    assert res.status_code == 200
    assert "Brand New Account" in res.text
    assert "No open positions currently held" in res.text

def test_client_webhook_modal():
    client = get_auth_client()
    res = client.get("/admin/clients/t_live_profit/webhook-modal")
    assert res.status_code == 200
    assert "TradingView Webhook Setup" in res.text
    assert "webhook_url" in res.text or "/webhook/t_live_profit" in res.text
    assert 'id="tab-strat-btn"' in res.text
    assert 'id="tab-ind-btn"' in res.text

def test_all_orders_page_and_partial():
    client = get_auth_client()
    res = client.get("/admin/orders")
    assert res.status_code == 200
    assert "Global Order Audit Stream" in res.text
    
    # Check partial HTMX stream
    res_p = client.get("/admin/orders-partial")
    assert res_p.status_code == 200
    assert "Live Signals" in res.text or "Active Stream" in res.text or "CRUDEOIL26MARFUT" in res.text

def test_client_edit_and_add_forms():
    client = get_auth_client()
    # Add form
    res_add = client.get("/admin/clients/add")
    assert res_add.status_code == 200
    assert "Onboard New Client Account" in res_add.text
    
    # Edit form
    res_edit = client.get("/admin/clients/t_live_profit/edit")
    assert res_edit.status_code == 200
    assert "Edit Client Account" in res_edit.text
    assert "Live Trader Pro" in res_edit.text

def test_settings_page():
    client = get_auth_client()
    res = client.get("/admin/settings")
    assert res.status_code == 200
    assert "Cluster Operations &amp; Settings" in res.text or "Cluster Operations & Settings" in res.text
    assert "On-Demand Hot Backup" in res.text
    assert "Admin Portal IP Allowlist" in res.text
    assert "Master Encryption Key Vault Rotation" in res.text

def test_audit_logs_page():
    client = get_auth_client()
    res = client.get("/admin/audit-logs")
    assert res.status_code == 200
    assert "Immutable Audit Trail" in res.text

def test_setup_2fa_page_qr_generation():
    client = get_auth_client()
    res = client.get("/admin/2fa-setup", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/admin/dashboard"

def test_custom_jinja_filters():
    format_inr = getattr(portal_main, "format_inr")
    assert format_inr(1234567.89) == "1,234,567.89"
    assert format_inr(1234567.89, decimals=0) == "1,234,568"
    assert format_inr(None) == "0.00"
    assert format_inr("invalid") == "0.00"
    assert format_inr(0) == "0.00"
    assert format_inr(-500.5) == "-500.50"


def test_sec_xts_008_chart_abort_controller_race_protection():
    """
    Regression Test for SEC-XTS-008:
    Verifies that client_detail.html contains AbortController, request sequence counter,
    and out-of-order response drop guards to prevent symbol-switch race conditions.
    """
    client = get_auth_client()
    res = client.get("/admin/clients/t_live_profit")
    assert res.status_code == 200
    html = res.text

    assert "chartAbortController" in html, "Missing chartAbortController in client_detail.html"
    assert "chartRequestSeq" in html, "Missing chartRequestSeq in client_detail.html"
    assert "signal: chartAbortController.signal" in html, "Missing signal option in chart fetch call"
    assert "currentSeq !== chartRequestSeq" in html, "Missing out-of-order sequence check in refreshSuperTrendChart"

def test_multi_strategy_card_actions_id_binding():
    """
    Verifies that client_detail.html correctly binds strategy ID to 'View Chart',
    'Evaluate Single Strategy', and the delete/toggle form routes so multi-timeframe
    strategies on the same symbol are uniquely controlled.
    """
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO tenant_supertrend_strategies (
                    id, tenant_id, symbol, exchange_segment, timeframe, quantity,
                    product_type, atr_period, multiplier, execution_mode, is_enabled,
                    created_at, updated_at
                ) VALUES 
                ('st_silv_15m', 't_live_profit', 'SILVER1001!', 'MCXFO', '15m', 1, 'NRML', 10, 3.0, 'LIVE', 1, 100, 100),
                ('st_silv_30m', 't_live_profit', 'SILVER1001!', 'MCXFO', '30m', 2, 'NRML', 10, 3.0, 'LIVE', 1, 200, 200);
            """)

    client = get_auth_client()
    res = client.get("/admin/clients/t_live_profit")
    assert res.status_code == 200
    html = res.text

    assert "switchChartStrategy('st_silv_15m')" in html
    assert "switchChartStrategy('st_silv_30m')" in html
    assert "openEvaluateModal('SILVER1001!', 'st_silv_15m')" in html
    assert "openEvaluateModal('SILVER1001!', 'st_silv_30m')" in html
    assert "/supertrend/strategy/st_silv_15m/toggle" in html
    assert "/supertrend/strategy/st_silv_30m/toggle" in html
    assert "/supertrend/strategy/st_silv_15m/delete" in html
    assert "/supertrend/strategy/st_silv_30m/delete" in html

def test_chart_markers_and_line_clearing_on_strategy_switch():
    """
    Verifies that client_detail.html properly passes empty arrays to setMarkers and setData
    to prevent trade markers from one timeframe (e.g. 15m) bleeding into another (e.g. 30m).
    """
    client = get_auth_client()
    res = client.get("/admin/clients/t_live_profit")
    assert res.status_code == 200
    html = res.text

    assert "stCandleSeries.setMarkers(Array.isArray(data.markers) ? data.markers : [])" in html
    assert "stLineSeries.setData(Array.isArray(data.supertrend_line) ? data.supertrend_line : [])" in html


def test_global_panic_modal_markup_and_js_handlers():
    """
    Verifies that dashboard.html contains the global panic modal with 2-step verification,
    input field, and all required JavaScript handlers (open, close, validate, submit).
    """
    client = get_auth_client()
    res = client.get("/admin/dashboard")
    assert res.status_code == 200
    html = res.text

    # Verify modal elements
    assert 'id="global-panic-modal"' in html
    assert 'id="global-panic-confirm-input"' in html
    assert 'id="btn-global-panic-submit"' in html
    assert 'onclick="openGlobalPanicModal()"' in html
    assert 'onclick="closeGlobalPanicModal()"' in html

    # Verify JavaScript function definitions
    assert "function openGlobalPanicModal()" in html
    assert "function closeGlobalPanicModal()" in html
    assert "function validateGlobalPanicConfirm(val)" in html
    assert "async function handleGlobalPanicSubmit(event)" in html
    assert "PANIC ALL" in html or "SQUARE OFF" in html


def test_base_html_dismisses_global_panic_modal():
    """
    Verifies that base.html modal dismissal helper includes 'global-panic-modal'
    for Escape key and backdrop click handlers.
    """
    client = get_auth_client()
    res = client.get("/admin/dashboard")
    assert res.status_code == 200
    html = res.text

    assert "'global-panic-modal'" in html


def test_admin_panic_all_endpoints(monkeypatch):
    """
    Verifies that POST /admin/panic-all works seamlessly:
    1. Returns JSON when Accept: application/json is present.
    2. Returns 303 Redirect to /admin/dashboard?panic=completed on standard HTML form post.
    """
    async def mock_panic_all():
        return {
            "status": "completed",
            "total_clients": 2,
            "results": [
                {"tenant_id": "t_live_profit", "status": "success", "result": {"status": "success"}},
                {"tenant_id": "t_paper_loss", "status": "success", "result": {"status": "success"}}
            ]
        }
    monkeypatch.setattr(telemetry_service, "panic_all_active_clients", mock_panic_all)

    client = get_auth_client()

    # 1. AJAX request returns JSON
    res_json = client.post("/admin/panic-all", headers={"Accept": "application/json"})
    assert res_json.status_code == 200
    data = res_json.json()
    assert data["status"] == "success"
    assert data["result"]["total_clients"] == 2

    # 2. Browser form submission returns 303 Redirect
    res_form = client.post("/admin/panic-all", follow_redirects=False)
    assert res_form.status_code == 303
    assert res_form.headers["location"] == "/admin/dashboard?panic=completed"


def test_admin_panic_single_client_endpoint(monkeypatch):
    """
    Verifies that POST /admin/clients/{tenant_id}/panic works:
    1. Returns JSON when Accept: application/json is present.
    2. Returns 303 Redirect on standard HTML form post.
    """
    async def mock_panic_single(t_id, sec):
        return {"status": "success", "squared_off": [{"symbol": "SILVER1001!", "qty": 1}]}
    monkeypatch.setattr(telemetry_service, "panic_single_client", mock_panic_single)

    client = get_auth_client()

    # 1. AJAX request returns JSON
    res_json = client.post("/admin/clients/t_live_profit/panic", headers={"Accept": "application/json"})
    assert res_json.status_code == 200
    assert res_json.json()["status"] == "success"

    # 2. Browser form submission returns 303 Redirect
    res_form = client.post("/admin/clients/t_live_profit/panic", follow_redirects=False)
    assert res_form.status_code == 303
    assert "/admin/clients/t_live_profit" in res_form.headers["location"]




def test_api_auth_me_unauthenticated_and_authenticated():
    """Verifies GET /api/auth/me returns unauthenticated vs authenticated status."""
    raw_client = TestClient(app)
    # Unauthenticated
    res = raw_client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.json() == {"authenticated": False}

    # Authenticated
    auth_client = get_auth_client()
    res_auth = auth_client.get("/api/auth/me")
    assert res_auth.status_code == 200
    assert res_auth.json()["authenticated"] is True
    assert res_auth.json()["user"]["username"] == "admin"


def test_api_auth_login_and_logout():
    """Verifies POST /api/auth/login and POST /api/auth/logout with JSON payloads."""
    raw_client = TestClient(app)
    # Invalid login
    res_invalid = raw_client.post("/api/auth/login", json={"username": "admin", "password": "WrongPassword"})
    assert res_invalid.status_code == 401

    # Valid login
    res_valid = raw_client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass123!"})
    assert res_valid.status_code == 200
    assert res_valid.json()["status"] == "ok"
    assert "admin_session" in res_valid.cookies

    # Me with cookie
    res_me = raw_client.get("/api/auth/me", cookies=res_valid.cookies)
    assert res_me.json()["authenticated"] is True

    # Logout
    res_logout = raw_client.post("/api/auth/logout", cookies=res_valid.cookies)
    assert res_logout.status_code == 200
    assert res_logout.json()["status"] == "ok"


def test_api_dashboard_telemetry_schema(monkeypatch):
    """Verifies GET /api/dashboard returns complete structured JSON for React frontend."""
    async def mock_telemetry():
        return {
            "summary": {
                "total_clients": 2,
                "active_clients": 2,
                "healthy_clients": 2,
                "total_net_mtm": 12500.50,
                "total_realized_pnl": 5000.00,
                "total_unrealized_mtm": 7500.50,
            },
            "clients": [
                {
                    "id": "t_live_profit",
                    "name": "Live Trader Pro",
                    "client_id": "ABK001",
                    "status": "ACTIVE",
                    "healthy": True,
                    "paper_mode": False,
                    "net_mtm": 12500.50,
                    "realized_pnl": 5000.00,
                    "unrealized_mtm": 7500.50,
                    "positions": [{"symbol": "CRUDEOIL1!", "quantity": 1}],
                    "broker_orders": [],
                    "available_margin": 250000.0,
                    "margin_used": 50000.0,
                    "supertrend": {"total_strategies": 1, "active_strategies_count": 1}
                }
            ]
        }
    monkeypatch.setattr(telemetry_service, "aggregate_all_telemetry", mock_telemetry)

    client = get_auth_client()
    res = client.get("/api/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert "aggregate_net_mtm" in data
    assert data["aggregate_net_mtm"] == 12500.50
    assert "market_status" in data
    assert "clients" in data
    assert len(data["clients"]) == 1
    assert data["clients"][0]["id"] == "t_live_profit"
    assert data["clients"][0]["execution_mode"] == "LIVE"


def test_api_client_detail_and_toggle_trading(monkeypatch):
    """Verifies GET /api/clients/{tenant_id} and POST /admin/clients/{tenant_id}/toggle-trading."""
    async def mock_single(t_id):
        return {
            "id": t_id,
            "name": "Live Trader Pro",
            "client_id": "ABK001",
            "status": "ACTIVE",
            "healthy": True,
            "paper_mode": False,
            "net_mtm": 12500.50,
            "realized_pnl": 5000.00,
            "unrealized_mtm": 7500.50,
            "positions": [{"symbol": "CRUDEOIL1!", "quantity": 1, "pnl": 7500.50}],
            "broker_orders": [{"app_order_id": "ORD001", "symbol": "CRUDEOIL1!", "side": "BUY", "quantity": 1, "status": "COMPLETE"}],
            "broker_trades": [],
            "available_margin": 250000.0,
            "margin_used": 50000.0,
            "supertrend": {"strategies": []},
            "mcx_margin": {"cash_available": 200000.0, "pay_in_amount": 0.0}
        }
    monkeypatch.setattr(telemetry_service, "get_single_client_telemetry", mock_single)
    monkeypatch.setattr(docker_manager, "stop_client_container", lambda cid: True)
    monkeypatch.setattr(docker_manager, "restart_client_container", lambda cid: True)
    monkeypatch.setattr(caddy_manager, "sync_caddy_config", lambda: True)

    client = get_auth_client()
    res = client.get("/api/clients/t_live_profit")
    assert res.status_code == 200
    data = res.json()
    assert data["client"]["id"] == "t_live_profit"
    assert len(data["positions"]) == 1
    assert len(data["orders"]) == 1

    # Toggle trading to PAUSE
    res_pause = client.post("/admin/clients/t_live_profit/toggle-trading", json={"pause": True})
    assert res_pause.status_code == 200
    assert res_pause.json()["trading_paused"] is True

    # Toggle trading to RESUME
    res_resume = client.post("/admin/clients/t_live_profit/toggle-trading", json={"pause": False})
    assert res_resume.status_code == 200
    assert res_resume.json()["trading_paused"] is False


def test_api_orders_and_audit_logs(monkeypatch):
    """Verifies GET /api/orders and GET /api/audit-logs."""
    async def mock_telemetry():
        return {
            "summary": {"total_net_mtm": 0},
            "clients": [
                {
                    "id": "t_live_profit",
                    "name": "Live Trader Pro",
                    "broker_orders": [
                        {"app_order_id": "ORD999", "symbol": "SILVER1001!", "side": "BUY", "quantity": 1, "status": "COMPLETE"}
                    ]
                }
            ]
        }
    monkeypatch.setattr(telemetry_service, "aggregate_all_telemetry", mock_telemetry)

    client = get_auth_client()
    res_orders = client.get("/api/orders")
    assert res_orders.status_code == 200
    assert len(res_orders.json()["orders"]) == 1
    assert res_orders.json()["orders"][0]["app_order_id"] == "ORD999"

    res_logs = client.get("/api/audit-logs")
    assert res_logs.status_code == 200
    assert "logs" in res_logs.json()


def test_api_client_settings_and_update_credentials(monkeypatch):
    """Verifies GET /api/clients/{tenant_id}/settings and PUT /api/clients/{tenant_id}/credentials."""
    monkeypatch.setattr(docker_manager, "restart_client_container", lambda cid: True)

    client = get_auth_client()
    res = client.get("/api/clients/t_live_profit/settings")
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Live Trader Pro"
    assert "credentials" in data
    assert data["credentials"]["broker_client_id"] == "DM933"
    assert "risk_limits" in data
    assert "webhook" in data
    assert "webhook_url" in data["webhook"]
    assert "webhook_secret" in data["webhook"]

    # Test updating credentials
    res_update = client.put("/api/clients/t_live_profit/credentials", json={
        "name": "Live Trader Pro Updated",
        "api_key": "NEW_KEY_123",
        "api_secret": "NEW_SECRET_456",
        "broker_client_id": "XTS_NEW_ID",
        "execution_mode": "PAPER"
    })
    assert res_update.status_code == 200

    # Verify updated settings
    res_after = client.get("/api/clients/t_live_profit/settings")
    assert res_after.status_code == 200
    after_data = res_after.json()
    assert after_data["name"] == "Live Trader Pro Updated"
    assert after_data["credentials"]["api_key"] == "NEW_KEY_123"
    assert after_data["credentials"]["broker_client_id"] == "XTS_NEW_ID"
    assert after_data["credentials"]["execution_mode"] == "PAPER"


def test_api_client_update_risk_limits(monkeypatch):
    """Verifies PUT /api/clients/{tenant_id}/risk-limits."""
    monkeypatch.setattr(docker_manager, "write_client_config", lambda cid: True)

    client = get_auth_client()
    res = client.put("/api/clients/t_live_profit/risk-limits", json={
        "max_lots_limit": 50,
        "max_order_value_inr": 2500000.0,
        "daily_notional_cap_inr": 8000000.0,
        "max_daily_loss_inr": 35000.0,
        "slippage_buffer_pct": 0.008,
        "min_days_before_expiry_mcx": 5
    })
    assert res.status_code == 200

    # Verify updated values in settings
    res_get = client.get("/api/clients/t_live_profit/settings")
    assert res_get.status_code == 200
    risk = res_get.json()["risk_limits"]
    assert risk["max_lots_limit"] == 50
    assert risk["max_order_value_inr"] == 2500000.0
    assert risk["max_daily_loss_inr"] == 35000.0
    assert risk["min_days_before_expiry_mcx"] == 5


def test_api_client_rotate_webhook_secret(monkeypatch):
    """Verifies POST /api/clients/{tenant_id}/webhook-secret/rotate."""
    monkeypatch.setattr(docker_manager, "write_client_config", lambda cid: True)

    client = get_auth_client()
    orig_res = client.get("/api/clients/t_live_profit/settings")
    orig_secret = orig_res.json()["webhook"]["webhook_secret"]

    res_rot = client.post("/api/clients/t_live_profit/webhook-secret/rotate")
    assert res_rot.status_code == 200
    new_secret = res_rot.json()["webhook_secret"]
    assert new_secret != orig_secret
    assert len(new_secret) >= 16

    # Verify new secret is reflected
    res_after = client.get("/api/clients/t_live_profit/settings")
    assert res_after.json()["webhook"]["webhook_secret"] == new_secret


def test_api_positions_square_off_and_bulk_cancel(monkeypatch):
    """Verifies /api/clients/{tenant_id}/positions/square-off and /api/orders/bulk-cancel."""
    # Mock httpx.AsyncClient to simulate client container responses
    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
            self.headers = {"content-type": "application/json"}
        def json(self):
            return self._json

    class MockAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            if "positions/square-off" in url:
                return MockResponse(200, {"status": "success", "message": "Position squared off"})
            if "orders/cancel-all" in url:
                return MockResponse(200, {"status": "success", "message": "All orders cancelled"})
            return MockResponse(404, {"detail": "Not found"})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    client = get_auth_client()
    res_sq = client.post("/api/clients/t_live_profit/positions/square-off", json={
        "symbol": "CRUDEOIL1!",
        "quantity": 1,
        "side": "SELL"
    })
    assert res_sq.status_code == 200
    assert res_sq.json()["status"] == "success"

    res_bulk = client.post("/api/orders/bulk-cancel", json={"tenant_id": "t_live_profit"})
    assert res_bulk.status_code == 200
    assert res_bulk.json()["status"] == "success"

