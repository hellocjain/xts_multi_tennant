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
    res = client.get("/admin/2fa-setup")
    assert res.status_code == 200
    assert "Mandatory 2FA Enrollment" in res.text
    assert "Emergency One-Time Backup Recovery Codes" in res.text

def test_custom_jinja_filters():
    from main import format_inr
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



