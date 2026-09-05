import pytest
import sys
import os
import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

portal_path = str(Path(__file__).parent.parent / 'portal')
client_path = str(Path(__file__).parent.parent / 'client')
for p in (portal_path, client_path):
    if p not in sys.path:
        sys.path.insert(0, p)

import portal.main as portal_main
import portal.database as portal_db
import portal.security as portal_sec
import portal.api_gateway as api_gateway


@pytest.fixture
def auth_portal_client(tmp_path, monkeypatch):
    test_db = str(tmp_path / 'btn_audit_test.db')
    monkeypatch.setattr(portal_db, 'get_db_path', lambda: test_db)
    if hasattr(portal_main, 'database'):
        monkeypatch.setattr(portal_main.database, 'get_db_path', lambda: test_db)
    portal_db.init_portal_db()

    with portal_db.closing(portal_db.get_db_connection()) as conn:
        with conn:
            conn.execute(
                'INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                ('tenant_btn_audit', 'Button Audit Corp', 'ACTIVE', time.time(), time.time())
            )
            enc_creds = portal_sec.encrypt_credentials({
                'API_KEY': 'KEY_BTN_AUDIT',
                'API_SECRET': 'SECRET_BTN_AUDIT',
                'CLIENT_ID': 'BTN001',
                'WEBHOOK_SECRET': 'WH_SECRET_BTN'
            })
            conn.execute(
                'INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)',
                ('tenant_btn_audit', enc_creds, time.time())
            )

    pass_hash = portal_sec.hash_password('AuditPass2026!')
    portal_db.create_client_user(
        tenant_id='tenant_btn_audit',
        username='btn_auditor',
        password_hash=pass_hash,
        email='auditor@acagarwal.com'
    )

    client = TestClient(portal_main.app)
    login_res = client.post('/client/login', data={'username': 'btn_auditor', 'password': 'AuditPass2026!'}, follow_redirects=False)
    assert login_res.status_code == 303
    token = login_res.cookies['client_session']
    client.cookies.set('client_session', token)
    return client


def test_all_templates_jinja2_syntax_and_rendering():
    tmpl_dir = Path(portal_path) / 'templates'
    templates = list(tmpl_dir.glob('*.html'))
    assert len(templates) >= 30, f'Expected at least 30 templates, found {len(templates)}'

    env = portal_main.templates.env
    errors = []
    for tmpl_file in templates:
        try:
            with open(tmpl_file, 'r', encoding='utf-8') as f:
                content = f.read()
            env.parse(content)
        except Exception as e:
            errors.append((tmpl_file.name, str(e)))

    assert len(errors) == 0, f'Jinja2 template parsing errors: {errors}'


def test_all_frontend_form_action_routes_exist():
    tmpl_dir = Path(portal_path) / 'templates'
    registered_routes = set()
    for route in portal_main.app.routes:
        if hasattr(route, 'path'):
            # Convert FastAPI path params like {tenant_id} to regex
            regex = re.sub(r"\{[^}]+\}", r"[^/]+", route.path)
            registered_routes.add(re.compile(f"^{regex}$"))

    dangling_actions = []
    for tmpl_file in tmpl_dir.glob('*.html'):
        with open(tmpl_file, 'r', encoding='utf-8') as f:
            content = f.read()
            actions = re.findall(r'<form[^>]+action=["\'](/[^"\']+)["\']', content, re.IGNORECASE)
            for action in actions:
                clean_action = action.split('?')[0]
                matched = any(r.match(clean_action) for r in registered_routes)
                if not matched:
                    if '{{' not in clean_action and '{%' not in clean_action:
                        dangling_actions.append((tmpl_file.name, action))

    assert len(dangling_actions) == 0, f'Found forms with unregistered action targets: {dangling_actions}'


def test_all_19_client_portal_routes_render_200(auth_portal_client):
    routes = [
        '/client/dashboard',
        '/client/orders',
        '/client/positions',
        '/client/trading',
        '/client/tools',
        '/client/tradebook',
        '/client/platforms',
        '/client/options',
        '/client/strategies',
        '/client/logs',
        '/client/developer',
        '/client/action-center',
        '/client/maxpain',
        '/client/gex',
        '/client/straddle',
        '/client/arbitrage',
        '/client/scalping',
        '/client/python',
        '/client/flow'
    ]
    for route in routes:
        res = auth_portal_client.get(route)
        assert res.status_code == 200, f'Route {route} failed with status {res.status_code}'
        assert len(res.text) > 100, f'Route {route} returned empty response'


def test_emergency_button_form_actions_redirect_cleanly(auth_portal_client):
    # 1. Cancel all orders button action
    res_cancel = auth_portal_client.post('/client/cancel-all-orders', follow_redirects=False)
    assert res_cancel.status_code == 303
    assert res_cancel.headers['location'] == '/client/dashboard'

    # 2. Panic square off all positions button action
    res_panic = auth_portal_client.post('/client/panic-square-off', follow_redirects=False)
    assert res_panic.status_code == 303
    assert res_panic.headers['location'] == '/client/dashboard'

    # 3. Square off single symbol button action
    res_single = auth_portal_client.post('/client/square-off-symbol', data={'symbol': 'RELIANCE'}, follow_redirects=False)
    assert res_single.status_code == 303
    assert res_single.headers['location'] == '/client/dashboard'
