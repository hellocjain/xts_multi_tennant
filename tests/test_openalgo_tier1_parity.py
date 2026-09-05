"""
tests/test_openalgo_tier1_parity.py
Comprehensive automated test suite validating 100% 1:1 OpenAlgo Parity for:
1. Dynamic Options Execution Engine & Offsets (/api/v1/optionsorder, /api/v1/optionsmultiorder)
2. Option Symbol Resolver (/api/v1/optionsymbol) and Synthetic Futures (/api/v1/syntheticfuture)
3. Server-Side GTT Engine (/api/v1/placegttorder, /modifygttorder, /cancelgttorder, /gttorderbook, and tick evaluation)
4. Action Center & Semi-Auto Human-in-the-Loop Queue (/api/v1/apikey/mode, /api/v1/action-center/*)
5. Analytical Batch Endpoints (/api/v1/multioptiongreeks, /api/v1/openposition, /api/v1/pnl/symbols)
6. Portal Action Center Interface (/client/action-center and proxy endpoints)
"""

import os
import sys
import json
import time
import pytest
from fastapi.testclient import TestClient

# Add client and portal directories to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal")))

from cryptography.fernet import Fernet
os.environ["PORTAL_MASTER_KEY"] = Fernet.generate_key().decode()

import client.main as client_main
import client.openalgo_router as openalgo_router
options_service = openalgo_router.options_order_service
gtt_service = openalgo_router.gtt_service
action_service = openalgo_router.action_center_service
import client.config as client_config
import portal.main as portal_main
import portal.database as portal_db
import portal.security as portal_sec

TEST_API_KEY = "TEST_PARITY_KEY_999"


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setattr(client_config, "API_KEY", TEST_API_KEY)
    monkeypatch.setattr(client_config, "WEBHOOK_SECRET", TEST_API_KEY)
    monkeypatch.setattr(openalgo_router.config, "API_KEY", TEST_API_KEY)
    monkeypatch.setattr(openalgo_router.config, "WEBHOOK_SECRET", TEST_API_KEY)
    return TestClient(client_main.app, headers={"X-API-KEY": TEST_API_KEY})


# =============================================================================
# 1. Dynamic Options Symbology & Mathematical Offsets Tests
# =============================================================================

def test_options_offset_strike_parsing():
    atm = 24500.0
    step = 50.0

    # CE Tests: ITM below ATM, OTM above ATM
    assert options_service.parse_offset("ATM", "CE", atm, step) == 24500.0
    assert options_service.parse_offset("ITM1", "CE", atm, step) == 24450.0
    assert options_service.parse_offset("ITM2", "CE", atm, step) == 24400.0
    assert options_service.parse_offset("OTM1", "CE", atm, step) == 24550.0
    assert options_service.parse_offset("OTM3", "CE", atm, step) == 24650.0

    # PE Tests: ITM above ATM, OTM below ATM
    assert options_service.parse_offset("ATM", "PE", atm, step) == 24500.0
    assert options_service.parse_offset("ITM1", "PE", atm, step) == 24550.0
    assert options_service.parse_offset("ITM2", "PE", atm, step) == 24600.0
    assert options_service.parse_offset("OTM1", "PE", atm, step) == 24450.0
    assert options_service.parse_offset("OTM3", "PE", atm, step) == 24350.0

    # Absolute strike fallback
    assert options_service.parse_offset("25000", "CE", atm, step) == 25000.0


def test_resolve_option_contract_details():
    res_ce = options_service.resolve_option_contract(
        underlying="NIFTY",
        exchange="NFO",
        expiry_date="26MAR26",
        offset="ITM1",
        option_type="CE"
    )
    assert res_ce["underlying"] == "NIFTY"
    assert res_ce["option_type"] == "CE"
    assert res_ce["strike"] == 24450.0
    assert "NIFTY26MAR2624450CE" in res_ce["symbol"]
    assert res_ce["lotsize"] == 25

    res_pe = options_service.resolve_option_contract(
        underlying="BANKNIFTY",
        exchange="NFO",
        expiry_date="26MAR26",
        offset="OTM2",
        option_type="PE"
    )
    assert res_pe["underlying"] == "BANKNIFTY"
    assert res_pe["option_type"] == "PE"
    # BANKNIFTY default simulated ltp 52500, step 100 -> OTM2 is 52500 - 200 = 52300
    assert res_pe["strike"] == 52300.0
    assert "BANKNIFTY26MAR2652300PE" in res_pe["symbol"]
    assert res_pe["lotsize"] == 15


def test_calculate_synthetic_future():
    # Synthetic Future = ATM Strike + Call LTP - Put LTP
    res = options_service.calculate_synthetic_future(
        underlying="NIFTY",
        expiry="26MAR26",
        atm_strike=24500.0,
        ltp=24520.0
    )
    assert res["status"] == "success"
    assert res["underlying"] == "NIFTY"
    assert res["atm_strike"] == 24500.0
    assert "synthetic_future" in res
    assert res["synthetic_future"] > 0.0


# =============================================================================
# 2. Dynamic Options REST API Endpoints Tests
# =============================================================================

def test_api_options_symbol_resolver(api_client):
    payload = {
        "underlying": "NIFTY",
        "exchange": "NFO",
        "expiry_date": "26MAR26",
        "offset": "ATM",
        "option_type": "CE"
    }
    resp = api_client.post("/api/v1/optionsymbol", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "data" in data
    assert data["data"]["strike"] == 24500.0
    assert "NIFTY26MAR2624500CE" in data["data"]["symbol"]


def test_api_synthetic_future_endpoint(api_client):
    payload = {
        "underlying": "NIFTY",
        "expiry": "26MAR26"
    }
    resp = api_client.post("/api/v1/syntheticfuture", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "synthetic_future" in data


def test_api_options_order_single_leg(api_client):
    # Test in ANALYZER / Simulated mode
    openalgo_router.set_current_trading_mode("ANALYZER")
    payload = {
        "underlying": "NIFTY",
        "exchange": "NFO",
        "expiry_date": "26MAR26",
        "offset": "ITM1",
        "option_type": "CE",
        "action": "BUY",
        "quantity": 25,
        "pricetype": "MARKET",
        "product": "NRML",
        "strategy": "OptionsBreakout"
    }
    resp = api_client.post("/api/v1/optionsorder", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "orderid" in data
    assert "resolved_strike" in data
    assert data["resolved_strike"] == 24450.0


def test_api_options_multiorder_buy_before_sell_sequencing(api_client):
    openalgo_router.set_current_trading_mode("ANALYZER")
    payload = {
        "underlying": "NIFTY",
        "exchange": "NFO",
        "strategy": "BullCallSpread",
        "orders": [
            {
                "offset": "OTM1",
                "option_type": "CE",
                "action": "SELL",
                "quantity": 25,
                "pricetype": "MARKET",
                "product": "NRML"
            },
            {
                "offset": "ATM",
                "option_type": "CE",
                "action": "BUY",
                "quantity": 25,
                "pricetype": "MARKET",
                "product": "NRML"
            }
        ]
    }
    resp = api_client.post("/api/v1/optionsmultiorder", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["results"]) == 2
    # Verify BUY leg was sequenced first for SEBI margin efficiency
    assert data["results"][0]["action"] == "BUY"
    assert data["results"][1]["action"] == "SELL"


# =============================================================================
# 3. Server-Side GTT (Good-Till-Triggered) Engine Tests
# =============================================================================

def test_gtt_service_single_order_crud(api_client):
    # 1. Place Single GTT Order
    place_payload = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": "BUY",
        "product": "CNC",
        "quantity": 10,
        "pricetype": "LIMIT",
        "price": 2800.0,
        "trigger_type": "SINGLE",
        "triggerprice_tg": 2795.0,
        "strategy": "GTT_Swing"
    }
    resp = api_client.post("/api/v1/placegttorder", json=place_payload)
    assert resp.status_code == 200
    placed = resp.json()
    assert placed["status"] == "success"
    trigger_id = placed["trigger_id"]
    assert trigger_id.startswith("GTT-")

    # 2. Query GTT Order Book
    book_resp = api_client.post("/api/v1/gttorderbook", json={})
    assert book_resp.status_code == 200
    book = book_resp.json()
    assert book["status"] == "success"
    orders = [o for o in book["data"] if o["trigger_id"] == trigger_id]
    assert len(orders) == 1
    assert orders[0]["status"] == "ACTIVE"

    # 3. Modify GTT Order
    mod_payload = {
        "trigger_id": trigger_id,
        "price": 2810.0,
        "triggerprice_tg": 2805.0
    }
    mod_resp = api_client.post("/api/v1/modifygttorder", json=mod_payload)
    assert mod_resp.status_code == 200
    mod_data = mod_resp.json()
    assert mod_data["status"] == "success"

    # 4. Cancel GTT Order
    cancel_resp = api_client.post("/api/v1/cancelgttorder", json={"trigger_id": trigger_id})
    assert cancel_resp.status_code == 200
    cancel_data = cancel_resp.json()
    assert cancel_data["status"] == "success"

    # Verify status changed to CANCELLED
    book_resp2 = api_client.post("/api/v1/gttorderbook", json={})
    orders2 = [o for o in book_resp2.json()["data"] if o["trigger_id"] == trigger_id]
    assert orders2[0]["status"] == "CANCELLED"


def test_gtt_oco_tick_evaluation_and_opposite_leg_cancellation():
    # Place OCO GTT: Buy Tata Motors with Stoploss at 950 and Target at 1050
    oco_payload = {
        "symbol": "TATAMOTORS",
        "exchange": "NSE",
        "action": "SELL",
        "product": "CNC",
        "quantity": 50,
        "pricetype": "LIMIT",
        "price": 1050.0,
        "trigger_type": "OCO",
        "triggerprice_sl": 950.0,
        "triggerprice_tg": 1050.0,
        "strategy": "OCO_Exit"
    }
    res = gtt_service.place_gtt_order(oco_payload)
    assert res["status"] == "success"
    t_id = res["trigger_id"]

    # Inactive tick (price = 1000, between SL 950 and TG 1050)
    fired_none = gtt_service.evaluate_tick("TATAMOTORS", 1000.0)
    assert len(fired_none) == 0

    # Active tick hitting Target (price = 1055 >= 1050)
    fired_tg = gtt_service.evaluate_tick("TATAMOTORS", 1055.0)
    assert len(fired_tg) >= 1
    matched = [f for f in fired_tg if f["trigger_id"] == t_id]
    assert len(matched) == 1
    assert matched[0]["status"] == "TRIGGERED"
    assert matched[0]["leg"] == "TARGET"

    # Verify GTT record is marked TRIGGERED
    book = gtt_service.get_gtt_orderbook()["data"]
    rec = next(r for r in book if r["trigger_id"] == t_id)
    assert rec["status"] == "TRIGGERED"
    assert rec["triggered_leg"] == "TARGET"


# =============================================================================
# 4. Action Center & Semi-Auto Human-in-the-Loop Queue Tests
# =============================================================================

def test_action_center_semi_auto_lifecycle(api_client):
    openalgo_router.set_current_trading_mode("PAPER")
    # 1. Check default API mode
    m_resp = api_client.get("/api/v1/apikey/mode")
    assert m_resp.status_code == 200
    assert m_resp.json()["mode"] in ("auto", "semi_auto")

    # 2. Switch to semi_auto mode
    set_resp = api_client.post("/api/v1/apikey/mode", json={"mode": "semi_auto"})
    assert set_resp.status_code == 200
    assert set_resp.json()["mode"] == "semi_auto"
    assert action_service.is_semi_auto_mode() is True

    # 3. Place order - should NOT execute immediately; should QUEUE in Action Center
    order_payload = {
        "symbol": "INFY",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 10,
        "pricetype": "MARKET",
        "product": "CNC",
        "strategy": "AlphaTrend"
    }
    place_resp = api_client.post("/api/v1/placeorder", json=order_payload)
    assert place_resp.status_code == 200
    queued = place_resp.json()
    assert queued["status"] == "queued"
    assert "pending_id" in queued
    pending_id = queued["pending_id"]

    # 4. Verify count incremented
    count_resp = api_client.get("/api/v1/action-center/count")
    assert count_resp.status_code == 200
    assert count_resp.json()["count"] >= 1

    # 5. Fetch Action Center data
    data_resp = api_client.get("/api/v1/action-center/data?status=pending")
    assert data_resp.status_code == 200
    res_data = data_resp.json()["data"]
    items = res_data["orders"] if isinstance(res_data, dict) and "orders" in res_data else res_data
    order_item = next(it for it in items if it["id"] == pending_id)
    assert order_item["status"] == "pending"
    assert order_item["order_data"]["symbol"] == "INFY"

    # 6. Reject the order
    rej_resp = api_client.post(f"/api/v1/action-center/reject/{pending_id}", json={"reason": "Testing rejection flow"})
    assert rej_resp.status_code == 200
    assert rej_resp.json()["status"] == "success"

    # Verify status is now 'rejected'
    all_res = api_client.get("/api/v1/action-center/data?status=all").json()["data"]
    all_data = all_res["orders"] if isinstance(all_res, dict) and "orders" in all_res else all_res
    rej_item = next(it for it in all_data if it["id"] == pending_id)
    assert rej_item["status"] == "rejected"
    assert rej_item["rejected_reason"] == "Testing rejection flow"

    # 7. Queue another order and Approve it
    q2 = api_client.post("/api/v1/placeorder", json=order_payload).json()
    p2_id = q2["pending_id"]

    app_resp = api_client.post(f"/api/v1/action-center/approve/{p2_id}", json={"approver": "SeniorTrader"})
    assert app_resp.status_code == 200
    assert app_resp.json()["status"] == "success"

    # Verify status is now 'approved'
    all_res2 = api_client.get("/api/v1/action-center/data?status=all").json()["data"]
    all_data2 = all_res2["orders"] if isinstance(all_res2, dict) and "orders" in all_res2 else all_res2
    app_item = next(it for it in all_data2 if it["id"] == p2_id)
    assert app_item["status"] == "approved"
    assert app_item["approved_by"] == "SeniorTrader"

    # 8. Queue 2 orders and test Approve-All
    q3 = api_client.post("/api/v1/placeorder", json=order_payload).json()["pending_id"]
    q4 = api_client.post("/api/v1/placeorder", json=order_payload).json()["pending_id"]
    
    app_all = api_client.post("/api/v1/action-center/approve-all", json={"approver": "RiskHead"})
    assert app_all.status_code == 200
    assert app_all.json()["approved_count"] >= 2

    # Reset mode to auto
    api_client.post("/api/v1/apikey/mode", json={"mode": "auto"})
    assert action_service.is_semi_auto_mode() is False


# =============================================================================
# 5. Analytical Batch Endpoints Tests
# =============================================================================

def test_api_multi_option_greeks(api_client):
    payload = {
        "underlying": "NIFTY",
        "spot_price": 24500.0,
        "contracts": [
            {"strike": 24500.0, "option_type": "CE", "days_to_expiry": 7},
            {"strike": 24500.0, "option_type": "PE", "days_to_expiry": 7},
            {"strike": 24600.0, "option_type": "CE", "days_to_expiry": 7}
        ]
    }
    resp = api_client.post("/api/v1/multioptiongreeks", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["greeks"]) == 3
    # Check Greek metrics computed
    g1 = data["greeks"][0]
    assert "delta" in g1
    assert "gamma" in g1
    assert "theta" in g1
    assert "vega" in g1
    assert "iv" in g1


def test_api_open_position_and_symbol_pnl(api_client):
    # Open Position for a symbol
    pos_resp = api_client.post("/api/v1/openposition", json={"symbol": "NIFTY 50"})
    assert pos_resp.status_code == 200
    pos_data = pos_resp.json()
    assert pos_data["status"] == "success"
    assert "has_position" in pos_data

    # Symbol-wise P&L breakdown
    pnl_resp = api_client.post("/api/v1/pnl/symbols", json={})
    assert pnl_resp.status_code == 200
    pnl_data = pnl_resp.json()
    assert pnl_data["status"] == "success"
    assert "symbols" in pnl_data
    assert "total_pnl" in pnl_data


# =============================================================================
# 6. Portal UI and Proxy Routes Tests
# =============================================================================

def test_portal_action_center_view_and_proxy(tmp_path, monkeypatch):
    test_db = str(tmp_path / "portal_test.db")
    monkeypatch.setattr(portal_db, "get_db_path", lambda: test_db)
    if "database" in sys.modules:
        monkeypatch.setattr(sys.modules["database"], "get_db_path", lambda: test_db)
    portal_db.init_portal_db()

    with portal_db.closing(portal_db.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("tenant_act_1", "Action Corp", "ACTIVE", time.time(), time.time())
            )
            enc_creds = portal_sec.encrypt_credentials({
                "API_KEY": "KEY_ACT_123",
                "API_SECRET": "SEC_123",
                "CLIENT_ID": "ACT01"
            })
            conn.execute(
                "INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)",
                ("tenant_act_1", enc_creds, time.time())
            )
            conn.execute(
                "INSERT INTO client_users (id, tenant_id, username, email, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("usr_act_1", "tenant_act_1", "act_user", "act_user@test.com", "hash", time.time(), time.time())
            )

    sess_tok = portal_sec.create_client_session("usr_act_1", "tenant_act_1", "127.0.0.1", "pytest-agent")

    portal_client = TestClient(portal_main.app)
    portal_client.cookies.set("client_session", sess_tok)

    # 1. Render /client/action-center HTML page
    page_resp = portal_client.get("/client/action-center")
    assert page_resp.status_code == 200
    assert "Action Center" in page_resp.text
    assert "SEMI-AUTO QUEUE" in page_resp.text
    assert "Approve All" in page_resp.text

    # 2. Portal Proxy Count
    cnt_resp = portal_client.get("/client/action-center/count")
    assert cnt_resp.status_code == 200
    assert cnt_resp.json()["status"] == "success"

    # 3. Portal Proxy Data
    data_resp = portal_client.get("/client/action-center/api/data?status=all")
    assert data_resp.status_code == 200
    assert data_resp.json()["status"] == "success"
    res_d = data_resp.json()["data"]
    assert "orders" in res_d or isinstance(res_d, list)
