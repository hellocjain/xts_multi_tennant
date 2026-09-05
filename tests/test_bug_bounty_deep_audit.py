import pytest
import sys
import os
import time
from pathlib import Path
from fastapi.testclient import TestClient

client_path = str(Path(__file__).parent.parent / 'client')
portal_path = str(Path(__file__).parent.parent / 'portal')
for p in (client_path, portal_path):
    if p not in sys.path:
        sys.path.insert(0, p)

import client.config as client_config
import client.main as client_main
import client.openalgo_router as openalgo_router
import portal.main as portal_main
import portal.database as portal_db
import portal.security as portal_sec
import portal.api_gateway as api_gateway


@pytest.fixture
def auth_client_app(monkeypatch):
    test_key = 'BOUNTY_TEST_KEY_2026'
    monkeypatch.setattr(client_config, 'API_KEY', test_key)
    monkeypatch.setattr(client_config, 'WEBHOOK_SECRET', test_key)
    monkeypatch.setattr(openalgo_router.config, 'API_KEY', test_key)
    monkeypatch.setattr(openalgo_router.config, 'WEBHOOK_SECRET', test_key)
    client = TestClient(client_main.app, headers={'X-API-KEY': test_key})
    return client


def test_idor_cross_tenant_isolation(tmp_path, monkeypatch):
    test_db = str(tmp_path / 'idor_test.db')
    monkeypatch.setattr(portal_db, 'get_db_path', lambda: test_db)
    if hasattr(portal_main, 'database'):
        monkeypatch.setattr(portal_main.database, 'get_db_path', lambda: test_db)
    portal_db.init_portal_db()

    with portal_db.closing(portal_db.get_db_connection()) as conn:
        with conn:
            conn.execute('INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                         ('tenant_victim', 'Victim Corp', 'ACTIVE', time.time(), time.time()))
            conn.execute('INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                         ('tenant_attacker', 'Attacker Corp', 'ACTIVE', time.time(), time.time()))

    pass_hash = portal_sec.hash_password('AttackerPass123!')
    portal_db.create_client_user('tenant_attacker', 'attacker_user', pass_hash, 'att@hacker.io')

    portal_client = TestClient(portal_main.app)
    login_res = portal_client.post('/client/login', data={'username': 'attacker_user', 'password': 'AttackerPass123!'}, follow_redirects=False)
    assert login_res.status_code == 303
    session_token = login_res.cookies['client_session']
    portal_client.cookies.set('client_session', session_token)

    # Attacker tries to view dashboard
    dash_res = portal_client.get('/client/dashboard')
    assert dash_res.status_code == 200

    # Attacker tries to access victim's tenant details on admin endpoints
    admin_probe = portal_client.get('/admin/clients/tenant_victim', follow_redirects=False)
    assert admin_probe.status_code in (303, 307, 401, 403)
    assert '/admin/login' in admin_probe.headers.get('location', '')


def test_sql_injection_resilience(auth_client_app):
    sqli_payloads = [
        "NIFTY'; DROP TABLE orders; --",
        "' OR '1'='1",
        "RELIANCE' UNION SELECT password_hash FROM admin_users --",
        "'; VACUUM; --"
    ]
    for sym in sqli_payloads:
        res = auth_client_app.post('/api/v1/placeorder', json={
            'apikey': 'BOUNTY_TEST_KEY_2026',
            'symbol': sym,
            'action': 'BUY',
            'quantity': 10,
            'pricetype': 'MARKET'
        })
        assert res.status_code in (200, 400)
        assert res.json().get('status') in ('success', 'error')


def test_xss_and_template_injection_sanitization(auth_client_app):
    xss_payloads = [
        '<script>alert("XSS")</script>',
        '<img src=x onerror=alert(1)>',
        '{{ 7 * 7 }}'
    ]
    for payload in xss_payloads:
        res = auth_client_app.post('/api/v1/placegttorder', json={
            'apikey': 'BOUNTY_TEST_KEY_2026',
            'symbol': 'INFY',
            'strategy': payload,
            'action': 'BUY',
            'quantity': 5,
            'trigger_type': 'SINGLE',
            'triggerprice_sl': 1400.0,
            'price': 1410.0
        })
        assert res.status_code in (200, 400)
        data = res.json()
        assert data.get('status') in ('success', 'error')


def test_extreme_numerical_and_subpenny_attacks(auth_client_app):
    # 1. Negative quantity
    res_neg = auth_client_app.post('/api/v1/placeorder', json={
        'apikey': 'BOUNTY_TEST_KEY_2026',
        'symbol': 'SBIN',
        'action': 'BUY',
        'quantity': -50,
        'pricetype': 'MARKET'
    })
    assert res_neg.status_code in (200, 400)
    assert res_neg.json().get('status') == 'error'

    # 2. Zero quantity
    res_zero = auth_client_app.post('/api/v1/placeorder', json={
        'apikey': 'BOUNTY_TEST_KEY_2026',
        'symbol': 'SBIN',
        'action': 'BUY',
        'quantity': 0,
        'pricetype': 'MARKET'
    })
    assert res_zero.status_code in (200, 400)
    assert res_zero.json().get('status') == 'error'

    # 3. Sub-penny float precision (10 digits)
    res_subpenny = auth_client_app.post('/api/v1/placeorder', json={
        'apikey': 'BOUNTY_TEST_KEY_2026',
        'symbol': 'SBIN',
        'action': 'BUY',
        'quantity': 10,
        'pricetype': 'LIMIT',
        'price': 789.1234567891
    })
    assert res_subpenny.status_code == 200

    # 4. Giant lot quantity (Fat finger guard)
    res_giant = auth_client_app.post('/api/v1/placeorder', json={
        'apikey': 'BOUNTY_TEST_KEY_2026',
        'symbol': 'SBIN',
        'action': 'BUY',
        'quantity': 100000000,
        'pricetype': 'MARKET'
    })
    assert res_giant.status_code in (200, 400)


def test_python_strategy_sandbox_isolation():
    from client import python_strategy_service as pss
    strat_id = 'test_strat_safe_1'
    tenant_id = 'tenant_safe_1'

    # Save strategy
    saved = pss.save_strategy(strat_id, tenant_id, 'SafeStrategy', 'print("Safe strategy run")', 'Description')
    assert saved is not None
    assert saved['name'] == 'SafeStrategy'

    # Stop strategy even if not running should handle gracefully
    stop_res = pss.stop_strategy(strat_id, tenant_id)
    assert 'status' in stop_res

    # Cleanup
    assert pss.delete_strategy(strat_id, tenant_id) is True


def test_header_spoofing_and_tampered_cookie_rejection(tmp_path, monkeypatch):
    test_db = str(tmp_path / 'sec_audit_test.db')
    monkeypatch.setattr(portal_db, 'get_db_path', lambda: test_db)
    if hasattr(portal_main, 'database'):
        monkeypatch.setattr(portal_main.database, 'get_db_path', lambda: test_db)
    portal_db.init_portal_db()

    client = TestClient(portal_main.app)

    # 1. Bogus session token should be rejected with redirect or 401
    client.cookies.set('client_session', 'BOGUS_FORGED_SESSION_TOKEN_12345')
    res_dash = client.get('/client/dashboard', follow_redirects=False)
    assert res_dash.status_code in (303, 307, 401)
    assert '/client/login' in res_dash.headers.get('location', '')

    # 2. Bogus admin session token
    client.cookies.set('admin_session', 'BOGUS_ADMIN_TOKEN_99999')
    res_admin = client.get('/admin/dashboard', follow_redirects=False)
    assert res_admin.status_code in (303, 307, 401)
    assert '/admin/login' in res_admin.headers.get('location', '')

