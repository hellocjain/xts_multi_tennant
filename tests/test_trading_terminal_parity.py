import pytest
from fastapi.testclient import TestClient
import os
import sys

# Add client and portal to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal")))

import client.main as client_main
import client.watchlist_service as watchlist_service

@pytest.fixture
def terminal_client():
    return TestClient(client_main.app)

def test_watchlist_service_crud():
    # 1. Default watchlist exists
    lists = watchlist_service.get_watchlists()
    assert len(lists) >= 1
    assert lists[0]["name"] == "Watchlist"
    assert len(lists[0]["items"]) >= 5

    # 2. Create custom watchlist
    created = watchlist_service.create_watchlist("F&O High Beta", [
        {"symbol": "NIFTY 50", "exchange": "NSE_INDEX"},
        {"symbol": "TATAMOTORS", "exchange": "NSE"}
    ])
    assert created is not None
    assert created["name"] == "F&O High Beta"
    wl_id = created["id"]

    # 3. Add item
    item = watchlist_service.add_item(wl_id, "BAJFINANCE", "NSE")
    assert item is not None
    assert item["symbol"] == "BAJFINANCE"
    assert item["exchange"] == "NSE"

    # 4. Remove item
    success = watchlist_service.remove_item(wl_id, item["id"])
    assert success is True

    # 5. Rename watchlist
    renamed = watchlist_service.rename_watchlist(wl_id, "F&O Momentum")
    assert renamed is True

    # 6. Delete watchlist
    deleted = watchlist_service.delete_watchlist(wl_id)
    assert deleted is True

def test_watchlist_rest_endpoints(terminal_client):
    # GET /api/v1/watchlist
    resp = terminal_client.get("/api/v1/watchlist")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert isinstance(data["data"], list)

    # POST /api/v1/watchlist
    resp = terminal_client.post("/api/v1/watchlist", json={"name": "Tech Stocks", "items": [{"symbol": "TCS", "exchange": "NSE"}]})
    assert resp.status_code == 200
    created = resp.json()["data"]
    wl_id = created["id"]

    # POST /api/v1/watchlist/item
    resp = terminal_client.post("/api/v1/watchlist/item", json={"watchlist_id": wl_id, "symbol": "WIPRO", "exchange": "NSE"})
    assert resp.status_code == 200
    item = resp.json()["data"]
    item_id = item["id"]

    # DELETE /api/v1/watchlist/item
    resp = terminal_client.request("DELETE", "/api/v1/watchlist/item", json={"watchlist_id": wl_id, "item_id": item_id})
    assert resp.status_code == 200

    # DELETE /api/v1/watchlist
    resp = terminal_client.request("DELETE", "/api/v1/watchlist", json={"id": wl_id})
    assert resp.status_code == 200

def test_watchlist_terminal_api_endpoints(terminal_client):
    # GET /watchlist/api/lists
    resp = terminal_client.get("/watchlist/api/lists")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"

    # POST /watchlist/api/lists
    resp = terminal_client.post("/watchlist/api/lists", json={"name": "Options Scalping"})
    assert resp.status_code == 201
    wl = resp.json()["data"]
    wl_id = wl["id"]

    # POST /watchlist/api/lists/{id}/items
    resp = terminal_client.post(f"/watchlist/api/lists/{wl_id}/items", json={"symbol": "NIFTY", "exchange": "NSE"})
    assert resp.status_code == 201
    item_id = resp.json()["data"]["id"]

    # DELETE /watchlist/api/lists/{id}/items/{item_id}
    resp = terminal_client.delete(f"/watchlist/api/lists/{wl_id}/items/{item_id}")
    assert resp.status_code == 200

    # DELETE /watchlist/api/lists/{id}
    resp = terminal_client.delete(f"/watchlist/api/lists/{wl_id}")
    assert resp.status_code == 200

def test_enhanced_option_chain_endpoint(terminal_client):
    # POST /api/v1/optionchain
    resp = terminal_client.post("/api/v1/optionchain", json={"symbol": "NIFTY"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["underlying"] == "NIFTY"
    assert data["spot"] > 0
    assert data["atm_strike"] > 0
    assert "expiries" in data
    assert "expiry" in data
    assert len(data["strikes"]) >= 10

    # Inspect strike data structure
    first_strike = data["strikes"][0]
    assert "strike" in first_strike
    assert "is_atm" in first_strike
    assert "call" in first_strike
    assert "put" in first_strike

    call = first_strike["call"]
    assert "symbol" in call
    assert "ltp" in call
    assert "delta" in call
    assert "gamma" in call
    assert "theta" in call
    assert "vega" in call
    assert "iv" in call
    assert "oi" in call
    assert "moneyness" in call
    assert call["moneyness"] in ["ITM", "ATM", "OTM"]

    put = first_strike["put"]
    assert "symbol" in put
    assert "ltp" in put
    assert "moneyness" in put

    # GET /api/v1/optionchain
    resp_get = terminal_client.get("/api/v1/optionchain?symbol=BANKNIFTY")
    assert resp_get.status_code == 200
    assert resp_get.json()["underlying"] == "BANKNIFTY"

def test_trading_terminal_template_elements():
    template_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal", "templates", "client_trading.html"))
    with open(template_path, "r") as f:
        html = f.read()

    # Right Rail
    assert 'id="terminal-right-rail"' in html
    assert 'id="btn-rail-watchlist"' in html
    assert 'id="btn-rail-options"' in html
    assert 'id="btn-rail-drawer"' in html

    # Slide-Out Panels
    assert 'id="oa-panel-watchlist"' in html
    assert 'id="watchlist-select"' in html
    assert 'id="watchlist-items-list"' in html

    assert 'id="oa-panel-options"' in html
    assert 'id="optionchain-underlying-select"' in html
    assert 'id="optionchain-expiry-select"' in html
    assert 'id="optionchain-strikes-body"' in html

    # Multi-Chart Grid
    assert 'id="btn-layout-dropdown"' in html
    assert 'id="layout-dropdown-menu"' in html
    assert 'id="chart-grid-container"' in html
    assert 'id="pane-p0"' in html
    assert 'id="pane-p1"' in html
    assert 'id="pane-p2"' in html
    assert 'id="pane-p3"' in html

    # Chart Type Switcher & Sync
    assert 'id="btn-chart-type"' in html
    assert 'id="chart-type-dropdown"' in html
    assert 'id="btn-sync-dropdown"' in html
    assert 'id="sync-dropdown-menu"' in html
