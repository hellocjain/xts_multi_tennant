import pytest
import sys
import os
import time
from pathlib import Path
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

os.environ['PORTAL_MASTER_KEY'] = 'o1w3qB0mQ_aV-1iA-q3jX9N4Kz8w1y2t3u4v5w6x7y8='

portal_path = str(Path(__file__).parent.parent / 'portal')
if portal_path not in sys.path:
    sys.path.insert(0, portal_path)

import database
import security
import api_gateway
import main as portal_main


@pytest.fixture
def client_authenticated(tmp_path, monkeypatch):
    test_db = str(tmp_path / 'portal_parity_test.db')
    monkeypatch.setattr(database, 'get_db_path', lambda: test_db)
    database.init_portal_db()

    with database.closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute(
                'INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                ('tenant_ac_1', 'AC Agarwal Alpha', 'ACTIVE', time.time(), time.time())
            )
            enc_creds = security.encrypt_credentials({
                'API_KEY': 'KEY_ACAGARWAL_TEST',
                'API_SECRET': 'SECRET_TEST',
                'CLIENT_ID': 'AC001',
                'WEBHOOK_SECRET': 'WH_SECRET_TEST'
            })
            conn.execute(
                'INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)',
                ('tenant_ac_1', enc_creds, time.time())
            )

    api_gateway.refresh_key_cache(force=True)

    pass_hash = security.hash_password('ACAgarwal2026!')
    database.create_client_user(
        tenant_id='tenant_ac_1',
        username='ac_trader',
        password_hash=pass_hash,
        email='trader@acagarwal.com'
    )

    client = TestClient(portal_main.app)
    login_res = client.post('/client/login', data={'username': 'ac_trader', 'password': 'ACAgarwal2026!'}, follow_redirects=False)
    assert login_res.status_code == 303
    session_token = login_res.cookies['client_session']
    client.cookies.set('client_session', session_token)
    return client


def test_base_template_ac_agarwal_branding_and_nav_tabs(client_authenticated):
    res = client_authenticated.get('/client/dashboard')
    assert res.status_code == 200
    html = res.text

    # 1. Branding
    assert 'AC Agarwal' in html

    # 2. Navigation items
    assert 'Dashboard' in html
    assert 'Orderbook' in html
    assert 'Tradebook' in html
    assert 'Positions' in html
    assert 'Trading' in html
    assert 'Platforms' in html
    assert 'Strategies' in html
    assert 'Logs' in html
    assert 'Tools' in html

    # 3. Active solid pill style
    assert 'bg-[#5c5bf0]' in html

    # 4. Right cluster elements
    assert 'acagarwal' in html
    assert 'Analyze Mode' in html
    assert 'theme-toggle-btn' in html

    # 5. Footer matching screenshot
    assert 'Copyright 2026' in html
    assert 'acagarwal.com' in html
    assert 'Algorithmic Trading Platform by A C Agarwal Share Brokers' in html
    assert 'v 2.0.2.2' in html
    assert '1 session' in html


def test_client_dashboard_pixel_perfect_parity(client_authenticated):
    res = client_authenticated.get('/client/dashboard')
    assert res.status_code == 200
    html = res.text

    # Header & Master contract
    assert 'Trading Dashboard' in html
    assert 'Overview of your trading account and market positions' in html
    assert 'Master Contract:' in html
    assert 'Ready (137542 symbols)' in html

    # 5 Metric Cards
    assert 'Available Balance' in html
    assert 'Collateral' in html
    assert 'Unrealized P&amp;L' in html or 'Unrealized P&L' in html
    assert 'Realized P&amp;L' in html or 'Realized P&L' in html
    assert 'Utilised Margin' in html

    # Quick Access items
    assert 'AC Agarwal Symbols' in html
    assert 'Live Logs' in html
    assert 'Documentation' in html
    assert 'P&amp;L Tracker' in html or 'P&L Tracker' in html
    assert 'Client Portal' in html
    assert 'Latency Monitor' in html


def test_client_orders_pixel_perfect_parity(client_authenticated):
    res = client_authenticated.get('/client/orders')
    assert res.status_code == 200
    html = res.text

    # Header & Subtabs
    assert 'Orderbook' in html
    assert 'Orders' in html
    assert 'GTT' in html

    # Actions
    assert 'Filters' in html
    assert 'Refresh' in html
    assert 'Export' in html
    assert 'Cancel All' in html
    assert '/client/cancel-all-orders' in html

    # 5 Metric Cards
    assert 'Buy Orders' in html
    assert 'Sell Orders' in html
    assert 'Completed' in html
    assert 'Open' in html
    assert 'Rejected' in html

    # Empty state clipboard
    assert 'No orders today' in html
    assert 'Orders you place will display here.' in html


def test_client_positions_pixel_perfect_parity(client_authenticated):
    res = client_authenticated.get('/client/positions')
    assert res.status_code == 200
    html = res.text

    # Header
    assert 'Positions' in html
    assert 'Live' in html

    # Actions
    assert 'Settings' in html
    assert 'Refresh' in html
    assert 'Export' in html
    assert 'Close All' in html
    assert '/client/panic-square-off' in html

    # 4 Metric Cards
    assert 'Open Positions' in html
    assert 'Long' in html
    assert 'Short' in html
    assert 'Total P&amp;L' in html or 'Total P&L' in html

    # Table columns
    assert 'Symbol' in html
    assert 'Exchange' in html
    assert 'Product' in html
    assert 'Avg Price' in html
    assert 'LTP' in html
    assert 'P&amp;L %' in html or 'P&L %' in html
    assert 'Action' in html


def test_client_tools_pixel_perfect_parity(client_authenticated):
    res = client_authenticated.get('/client/tools')
    assert res.status_code == 200
    html = res.text

    # Header
    assert 'Tools' in html
    assert 'Analytical tools for options trading and market analysis' in html

    # 15 tool cards matching exact screenshot
    tools = [
        'Strategy Builder', 'Strategy Portfolio', 'Portfolio Backtester',
        'SIP Backtester', 'Portfolio Analyzer', 'Option Chain',
        'Option Greeks', 'OI Tracker', 'OI Range',
        'Max Pain Calculator', 'Gamma Exposure (GEX)', 'ATM Straddle Engine',
        'Calendar Arbitrage', 'Python Runner', 'Flow Builder'
    ]
    for tool in tools:
        assert tool in html

    # Check redirect from /tools
    red_res = client_authenticated.get('/tools', follow_redirects=False)
    assert red_res.status_code == 307
    assert red_res.headers['location'] == '/client/tools'


def test_client_tradebook_pixel_perfect_parity(client_authenticated):
    res = client_authenticated.get('/client/tradebook')
    assert res.status_code == 200
    html = res.text

    assert 'Trade Book' in html
    assert 'All executed fills and completed transactions' in html

    # Check redirect from /tradebook
    red_res = client_authenticated.get('/tradebook', follow_redirects=False)
    assert red_res.status_code == 307
    assert red_res.headers['location'] == '/client/tradebook'


def test_client_platforms_pixel_perfect_parity(client_authenticated):
    res = client_authenticated.get('/client/platforms')
    assert res.status_code == 200
    html = res.text

    assert 'Platforms &amp; Integrations' in html or 'Platforms & Integrations' in html
    assert 'TradingView Webhooks' in html
    assert 'Python OpenAlgo SDK' in html
    assert 'Microsoft Excel / VBA' in html
    assert 'AmiBroker' in html

    # Check redirect from /platforms
    red_res = client_authenticated.get('/platforms', follow_redirects=False)
    assert red_res.status_code == 307
    assert red_res.headers['location'] == '/client/platforms'


def test_client_trading_terminal_pixel_perfect_parity(client_authenticated):
    res = client_authenticated.get('/client/trading')
    assert res.status_code == 200
    html = res.text

    assert 'Trading Terminal' in html
    assert 'AC Agarwal' in html
    assert 'Panic Square Off' in html
    assert '/client/orders' in html
    assert '/client/positions' in html
    assert '/client/tools' in html


def test_client_strategy_and_logs_redirect_parity(client_authenticated):
    # Verify /strategy and /strategies redirect to /client/strategies
    res_strat = client_authenticated.get('/strategy', follow_redirects=False)
    assert res_strat.status_code == 307
    assert res_strat.headers['location'] == '/client/strategies'

    res_strats = client_authenticated.get('/strategies', follow_redirects=False)
    assert res_strats.status_code == 307
    assert res_strats.headers['location'] == '/client/strategies'

    # Verify /logs redirects to /client/logs
    res_logs = client_authenticated.get('/logs', follow_redirects=False)
    assert res_logs.status_code == 307
    assert res_logs.headers['location'] == '/client/logs'

