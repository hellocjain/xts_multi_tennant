import os
import sys
import tempfile
import json
import sqlite3
import pytest
import concurrent.futures
from fastapi.testclient import TestClient

test_dir = tempfile.mkdtemp()
os.environ["PORTAL_DATA_DIR"] = test_dir
os.environ["PORTAL_MASTER_KEY"] = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="

portal_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal"))
if "main" in sys.modules:
    del sys.modules["main"]
if sys.path[0] != portal_dir:
    sys.path.insert(0, portal_dir)

import database
import security
import docker_manager
import telemetry_service
import main as portal_main

app = portal_main.app

@pytest.fixture(autouse=True)
def setup_stress_db():
    os.environ["PORTAL_DATA_DIR"] = test_dir
    database.init_portal_db()
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("DELETE FROM admin_sessions")
            conn.execute("DELETE FROM admin_users")
            pwd_hash = security.hash_password("StressPass123!")
            conn.execute("""
                INSERT INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at)
                VALUES ('stress_admin', 'stress_admin', ?, 1, ?)
            """, (pwd_hash, 1600000000.0))
            
            conn.execute("DELETE FROM tenants")
            conn.execute("DELETE FROM tenant_credentials")
            conn.execute("DELETE FROM tenant_risk_limits")

            # Tenant with extreme numbers
            conn.execute("INSERT INTO tenants VALUES ('t_extreme', 'Extreme Valued Tenant', 'ACTIVE', 1600000000, 1600000000)")
            conn.execute("INSERT INTO tenant_credentials VALUES ('t_extreme', ?, 1600000000)", (
                security.encrypt_credentials({"API_KEY": "K", "API_SECRET": "S", "CLIENT_ID": "EXTR01", "WEBHOOK_SECRET": "W"}),
            ))

            # Tenant with corrupted data
            conn.execute("INSERT INTO tenants VALUES ('t_corrupted', 'Corrupted Data Tenant', 'ACTIVE', 1600000000, 1600000000)")
            conn.execute("INSERT INTO tenant_credentials VALUES ('t_corrupted', ?, 1600000000)", (
                security.encrypt_credentials({"API_KEY": "K", "API_SECRET": "S", "CLIENT_ID": "CORR01", "WEBHOOK_SECRET": "W"}),
            ))
    yield

def get_auth_client():
    os.environ["PORTAL_DATA_DIR"] = test_dir
    client = TestClient(app, cookies={})
    token = security.create_session("stress_admin", "127.0.0.1", "stress-tester")
    client.cookies.set("admin_session", token)
    return client

def test_extreme_numerical_rendering(monkeypatch):
    """Test large positive/negative currency amounts and float boundaries."""
    async def mock_fetch(client_session, tenant):
        return telemetry_service.build_client_telemetry_dict(
            tenant_id="t_extreme",
            name="Extreme Valued Tenant",
            client_id="EXTR01",
            status="HEALTHY",
            docker_status="RUNNING",
            healthy=True,
            paper_mode=False,
            unrealized_mtm=-9876543210.50,
            realized_pnl=1234567890.25,
            net_mtm=-8641975320.25,
            positions=[
                {
                    "symbol": "CRUDEOIL26MARFUT",
                    "segment": "MCX",
                    "side": "LONG",
                    "quantity": 100000,
                    "buy_avg": 9999999.95,
                    "sell_avg": 0.0,
                    "ltp": 9999998.00,
                    "unrealized_mtm": -9876543210.50,
                    "realized_pnl": 1234567890.25,
                    "product_type": "NRML"
                }
            ],
            available_margin=5000000000.0,
            margin_used=2500000000.0,
            total_collateral=1000000000.0,
            net_margin_available=4000000000.0,
            total_account_value=6000000000.0,
            margin_pct=50.0,
            notional_today=99999999999.0,
            notional_cap=100000000000.0,
            notional_pct=99.9,
            recent_signals=[]
        )
    monkeypatch.setattr(telemetry_service, "fetch_single_client_telemetry", mock_fetch)

    client = get_auth_client()
    res_dash = client.get("/admin/dashboard")
    assert res_dash.status_code == 200
    assert "₹-8,641,975,320.25" in res_dash.text or "-8,641,975,320.25" in res_dash.text

    res_detail = client.get("/admin/clients/t_extreme")
    assert res_detail.status_code == 200
    assert "Extreme Valued Tenant" in res_detail.text
    assert "CRUDEOIL26MARFUT" in res_detail.text

def test_corrupted_and_null_telemetry_handling(monkeypatch):
    """Test telemetry payloads containing None, empty collections, and broken strings."""
    async def mock_corrupt_fetch(client_session, tenant):
        return {
            "id": "t_corrupted",
            "name": None,
            "client_id": None,
            "status": None,
            "docker_status": None,
            "healthy": False,
            "paper_mode": False,
            "unrealized_mtm": None,
            "realized_pnl": None,
            "net_mtm": None,
            "positions_count": 0,
            "positions": [],
            "available_margin": None,
            "margin_used": None,
            "total_collateral": None,
            "net_margin_available": None,
            "total_account_value": None,
            "margin_pct": None,
            "notional_today": None,
            "notional_cap": None,
            "notional_pct": None,
            "recent_signals": [
                {
                    "received_at": None,
                    "payload": None,
                    "result": None,
                    "status": "corrupt"
                }
            ],
            "error": "Simulated hardware fault"
        }
    monkeypatch.setattr(telemetry_service, "fetch_single_client_telemetry", mock_corrupt_fetch)

    client = get_auth_client()
    res_dash = client.get("/admin/dashboard-partial")
    assert res_dash.status_code == 200

    res_detail = client.get("/admin/clients/t_corrupted")
    assert res_detail.status_code == 200
    assert "t_corrupted" in res_detail.text

def test_concurrent_request_stress():
    """Simulate 50 parallel requests hitting dashboard and partials to verify SQLite connection concurrency."""
    client = get_auth_client()
    endpoints = [
        "/admin/dashboard",
        "/admin/dashboard-partial",
        "/admin/orders",
        "/admin/orders-partial",
        "/admin/settings",
        "/admin/audit-logs"
    ]
    
    def hit_endpoint(ep):
        return client.get(ep).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(hit_endpoint, endpoints[i % len(endpoints)]) for i in range(50)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert all(code == 200 for code in results)
