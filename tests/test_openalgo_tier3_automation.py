import os
import sys
import pytest
import time
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal")))

from cryptography.fernet import Fernet
if not os.environ.get("PORTAL_MASTER_KEY"):
    os.environ["PORTAL_MASTER_KEY"] = Fernet.generate_key().decode()

from fastapi.testclient import TestClient
from portal import main as portal_main
import client.openalgo_router as openalgo_router
from fastapi import FastAPI

# Test client app mounting openalgo router
client_app = FastAPI()
client_app.include_router(openalgo_router.router)
api_client = TestClient(client_app)
portal_client = TestClient(portal_main.app)


# -----------------------------------------------------------------------------
# 1. Python Strategy Service & Subprocess Management
# -----------------------------------------------------------------------------
def test_python_strategy_service_lifecycle():
    from client import python_strategy_service
    tenant_id = "test_tenant_py"
    strat_id = f"test_strat_{int(time.time()*1000)}"

    # 1. Verify Templates
    templates = python_strategy_service.STRATEGY_TEMPLATES
    assert "sma_crossover" in templates
    assert "atm_straddle" in templates
    assert "supertrend_options" in templates

    # 2. Save custom python script
    code = """
import time
import sys
print("Starting automated test strategy...")
for i in range(3):
    print(f"Tick cycle {i}")
    time.sleep(0.5)
print("Finished test script")
"""
    saved = python_strategy_service.save_strategy(strat_id, tenant_id, "Test Automation Script", code, "Unit test script")
    assert saved is not None
    assert saved["name"] == "Test Automation Script"
    assert saved["is_running"] is False

    # 3. List strategies
    strats = python_strategy_service.list_strategies(tenant_id)
    assert any(s["id"] == strat_id for s in strats)

    # 4. Run strategy
    run_res = python_strategy_service.run_strategy(strat_id, tenant_id)
    assert run_res["status"] == "success"
    assert "pid" in run_res

    # Give subprocess a short moment to emit stdout
    time.sleep(0.8)

    # 5. Fetch logs
    logs_res = python_strategy_service.get_strategy_logs(strat_id, tenant_id)
    assert logs_res["status"] == "success"
    assert "Tick cycle" in logs_res["logs"] or "Starting automated test strategy" in logs_res["logs"]

    # 6. Stop strategy
    stop_res = python_strategy_service.stop_strategy(strat_id, tenant_id)
    assert stop_res["status"] == "success"

    # 7. Delete strategy
    del_res = python_strategy_service.delete_strategy(strat_id, tenant_id)
    assert del_res is True
    assert python_strategy_service.get_strategy(strat_id, tenant_id) is None


# -----------------------------------------------------------------------------
# 2. Flow Executor Service & Visual Node Engine
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_executor_service_lifecycle():
    from client import flow_executor_service
    tenant_id = "test_tenant_flow"
    flow_id = f"test_flow_{int(time.time()*1000)}"

    # 1. Verify Templates
    templates = flow_executor_service.FLOW_TEMPLATES
    assert "straddle_flow" in templates
    assert "risk_guard_circuit_breaker" in templates

    # 2. Save Flow
    flow_data = {
        "nodes": [
            {"id": "t1", "type": "trigger", "title": "Time Trigger", "data": {"trigger_type": "TIME", "time": "09:20:00"}},
            {"id": "c1", "type": "condition", "title": "Flat Position", "data": {"type": "POSITION_COUNT", "operator": "==", "value": 0}},
            {"id": "a1", "type": "action", "title": "Alert Admin", "data": {"type": "SEND_ALERT", "message": "Test flow triggered successfully"}}
        ],
        "edges": [
            {"from": "t1", "to": "c1"},
            {"from": "c1", "to": "a1"}
        ]
    }
    saved = flow_executor_service.save_flow(flow_id, tenant_id, "Test Automated Flow", flow_data, "Test flow desc")
    assert saved is not None
    assert saved["name"] == "Test Automated Flow"
    assert saved["is_enabled"] is True

    # 3. List Flows
    flows = flow_executor_service.list_flows(tenant_id)
    assert any(f["id"] == flow_id for f in flows)

    # 4. Toggle Flow
    flow_executor_service.toggle_flow(flow_id, tenant_id, False)
    assert flow_executor_service.get_flow(flow_id, tenant_id)["is_enabled"] is False
    flow_executor_service.toggle_flow(flow_id, tenant_id, True)

    # 5. Execute Flow Actions
    exec_res = await flow_executor_service.execute_flow_actions(saved, {})
    assert exec_res["status"] == "success"
    assert exec_res["actions_executed"] >= 1

    # 6. Check Audit Logs
    logs = flow_executor_service.get_flow_logs(tenant_id, flow_id=flow_id)
    assert len(logs) >= 1
    assert logs[0]["event_type"] == "ALERT"

    # 7. Delete Flow
    del_res = flow_executor_service.delete_flow(flow_id, tenant_id)
    assert del_res is True
    assert flow_executor_service.get_flow(flow_id, tenant_id) is None


# -----------------------------------------------------------------------------
# 3. OpenAlgo REST API Endpoints for Python & Flow
# -----------------------------------------------------------------------------
def test_openalgo_python_rest_endpoints(monkeypatch):
    monkeypatch.setattr("client.openalgo_router._verify_auth", lambda d, r: True)

    # 1. Templates
    r_tmpl = api_client.get("/api/v1/python/templates")
    assert r_tmpl.status_code == 200
    assert r_tmpl.json()["status"] == "success"

    # 2. Save Strategy
    strat_id = f"api_strat_{int(time.time()*1000)}"
    r_save = api_client.post("/api/v1/python/save", json={
        "id": strat_id,
        "name": "API Saved Script",
        "code_content": "print('Hello from API')",
        "tenant_id": "default"
    })
    assert r_save.status_code == 200
    assert r_save.json()["status"] == "success"

    # 3. List Strategies
    r_list = api_client.get("/api/v1/python/list?tenant_id=default")
    assert r_list.status_code == 200
    assert any(s["id"] == strat_id for s in r_list.json()["data"])

    # 4. Logs Endpoint
    r_logs = api_client.post("/api/v1/python/logs", json={"id": strat_id, "tenant_id": "default"})
    assert r_logs.status_code == 200
    assert r_logs.json()["status"] == "success"

    # 5. Delete Endpoint
    r_del = api_client.delete(f"/api/v1/python/delete/{strat_id}")
    assert r_del.status_code == 200


def test_openalgo_flow_rest_endpoints(monkeypatch):
    monkeypatch.setattr("client.openalgo_router._verify_auth", lambda d, r: True)

    # 1. Templates
    r_tmpl = api_client.get("/api/v1/flow/templates")
    assert r_tmpl.status_code == 200
    assert r_tmpl.json()["status"] == "success"

    # 2. Save Flow
    flow_id = f"api_flow_{int(time.time()*1000)}"
    r_save = api_client.post("/api/v1/flow/save", json={
        "id": flow_id,
        "name": "API Flow",
        "flow": {"nodes": [{"id": "n1", "type": "action", "data": {"type": "SEND_ALERT", "message": "API alert"}}]},
        "tenant_id": "default"
    })
    assert r_save.status_code == 200
    assert r_save.json()["status"] == "success"

    # 3. Toggle Flow
    r_toggle = api_client.post("/api/v1/flow/toggle", json={"id": flow_id, "is_enabled": False, "tenant_id": "default"})
    assert r_toggle.status_code == 200
    assert r_toggle.json()["is_enabled"] is False

    # 4. Run Flow
    r_run = api_client.post("/api/v1/flow/run", json={"id": flow_id, "tenant_id": "default"})
    assert r_run.status_code == 200
    assert r_run.json()["status"] == "success"

    # 5. Flow Logs
    r_logs = api_client.get("/api/v1/flow/logs?tenant_id=default")
    assert r_logs.status_code == 200
    assert r_logs.json()["status"] == "success"

    # 6. Delete Flow
    r_del = api_client.delete(f"/api/v1/flow/delete/{flow_id}")
    assert r_del.status_code == 200


# -----------------------------------------------------------------------------
# 4. Portal Views & API Proxies
# -----------------------------------------------------------------------------
def test_portal_python_and_flow_pages_and_proxies(tmp_path, monkeypatch):
    from portal import database as portal_db
    from portal import security as portal_sec

    test_db = str(tmp_path / "portal_test_tier3.db")
    monkeypatch.setattr(portal_db, "get_db_path", lambda: test_db)
    if "database" in sys.modules:
        monkeypatch.setattr(sys.modules["database"], "get_db_path", lambda: test_db)
    portal_db.init_portal_db()

    tenant_id = "test_tier3_user"
    with portal_db.closing(portal_db.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, "Tier 3 Parity Trader", "ACTIVE", time.time(), time.time())
            )
            conn.execute(
                "INSERT INTO tenant_risk_limits (tenant_id, trading_mode, paper_trade_mode, updated_at) VALUES (?, ?, ?, ?)",
                (tenant_id, "PAPER", 1, time.time())
            )
            conn.execute(
                "INSERT INTO client_users (id, tenant_id, username, email, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("usr_tier3_1", tenant_id, "tier3_client", "tier3@test.com", "hash", time.time(), time.time())
            )

    sess_tok = portal_sec.create_client_session("usr_tier3_1", tenant_id, "127.0.0.1", "pytest-agent")
    portal_client.cookies.set("client_session", sess_tok)

    # 1. Test Python Runner Page
    r_py_page = portal_client.get("/client/python")
    assert r_py_page.status_code == 200
    assert "Python Strategy Runner" in r_py_page.text
    assert "Console Terminal Stream" in r_py_page.text
    assert "Run Strategy" in r_py_page.text

    # 2. Test Flow Visual Builder Page
    r_flow_page = portal_client.get("/client/flow")
    assert r_flow_page.status_code == 200
    assert "Flow No-Code Visual Builder" in r_flow_page.text
    assert "Visual Flow Canvas" in r_flow_page.text
    assert "Execute Flow Now" in r_flow_page.text

    # 3. Test Proxy Endpoints
    r_py_list = portal_client.get("/client/api/python/list")
    assert r_py_list.status_code == 200
    assert r_py_list.json()["status"] == "success"

    r_py_tmpl = portal_client.get("/client/api/python/templates")
    assert r_py_tmpl.status_code == 200
    assert "sma_crossover" in r_py_tmpl.json()["data"]

    r_flow_list = portal_client.get("/client/api/flow/list")
    assert r_flow_list.status_code == 200
    assert r_flow_list.json()["status"] == "success"

    r_flow_tmpl = portal_client.get("/client/api/flow/templates")
    assert r_flow_tmpl.status_code == 200
    assert "straddle_flow" in r_flow_tmpl.json()["data"]
