import pytest
import sys
import os
import time
import json
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
def portal_client(tmp_path, monkeypatch):
    test_db = str(tmp_path / "portal_test.db")
    monkeypatch.setattr(database, "get_db_path", lambda: test_db)
    database.init_portal_db()

    # Seed test tenant and credentials
    with database.closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("tenant_test_1", "Test Trader Corp", "ACTIVE", time.time(), time.time())
            )
            enc_creds = security.encrypt_credentials({
                "API_KEY": "KEY_TRADER_111",
                "API_SECRET": "SECRET_111",
                "CLIENT_ID": "ABK001",
                "WEBHOOK_SECRET": "WH_SECRET_111"
            })
            conn.execute(
                "INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)",
                ("tenant_test_1", enc_creds, time.time())
            )

    api_gateway.refresh_key_cache(force=True)
    return TestClient(portal_main.app)

def test_api_gateway_ping(portal_client):
    res = portal_client.get("/api/v1/ping")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["message"] == "pong"

def test_api_gateway_unknown_key_rejected(portal_client):
    payload = {
        "apikey": "UNKNOWN_OR_INVALID_KEY",
        "symbol": "CRUDEOIL1!",
        "action": "BUY",
        "quantity": 100
    }
    res = portal_client.post("/api/v1/placeorder", json=payload)
    assert res.status_code == 401
    assert res.json()["status"] == "error"
    assert "Invalid API key" in res.json()["message"]

def test_api_gateway_resolves_tenant_key():
    # Test internal key resolution logic
    assert api_gateway.resolve_tenant_id("KEY_TRADER_111") == "tenant_test_1"
    assert api_gateway.resolve_tenant_id("WH_SECRET_111") == "tenant_test_1"
    assert api_gateway.resolve_tenant_id("NON_EXISTENT_KEY") is None

def test_client_portal_authentication(portal_client):
    # 1. Create client user
    pass_hash = security.hash_password("TraderPass123!")
    user_id = database.create_client_user(
        tenant_id="tenant_test_1",
        username="trader_john",
        password_hash=pass_hash,
        email="trader@test.com"
    )
    assert user_id is not None

    # 2. Failed login (wrong password)
    res_fail = portal_client.post("/client/login", data={"username": "trader_john", "password": "WrongPassword"})
    assert res_fail.status_code == 200
    assert "Invalid client credentials" in res_fail.text

    # 3. Successful login
    res_ok = portal_client.post("/client/login", data={"username": "trader_john", "password": "TraderPass123!"}, follow_redirects=False)
    assert res_ok.status_code == 303
    assert "/client/dashboard" in res_ok.headers["Location"]
    assert "client_session" in res_ok.cookies

    # 4. Access protected dashboard
    cookie_val = res_ok.cookies["client_session"]
    portal_client.cookies.set("client_session", cookie_val)
    res_dash = portal_client.get("/client/dashboard")
    assert res_dash.status_code == 200
    assert "Test Trader Corp" in res_dash.text
    assert "Trading Terminal" in res_dash.text

    # 5. Logout
    res_logout = portal_client.get("/client/logout", follow_redirects=False)
    assert res_logout.status_code == 303
    assert "/client/login" in res_logout.headers["Location"]
