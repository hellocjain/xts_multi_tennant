"""
Comprehensive Automated Test Suite for 100% OpenAlgo Full-Line Parity:
- End-to-End verification of all 8 new OpenAlgo REST API endpoints
- Level 2 Market Depth (5-level DOM ladder)
- RMS Slippage Buffer calculation & Atomic Position Auto-Reversal
- In-Terminal multi-tab side drawer templates and UI controller verification
"""
import pytest
from fastapi.testclient import TestClient
import client.main as client_app
import client.order_services as order_services
import client.token_db as token_db
import client.xts_api as xts_api

@pytest.fixture
def client_tc():
    return TestClient(client_app.app)


# =============================================================================
# 1. Order Status API Tests (/api/v1/orderstatus)
# =============================================================================
def test_orderstatus_success_and_not_found(client_tc):
    # 1. First place a paper order to get a valid orderid
    order_payload = {
        "action": "BUY",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": 10,
        "price": 2950.0,
        "is_paper": True,
        "apikey": "test"
    }
    place_resp = client_tc.post("/api/v1/order", json=order_payload)
    assert place_resp.status_code == 200
    order_id = place_resp.json()["orderid"]
    assert order_id != "N/A"

    # 2. Query status of this order
    status_payload = {"orderid": order_id, "apikey": "test"}
    status_resp = client_tc.post("/api/v1/orderstatus", json=status_payload)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "success"
    order_data = body["data"]
    assert str(order_data["orderid"]) == str(order_id)
    assert order_data["action"] == "BUY"
    assert order_data["quantity"] == 10
    assert "status" in order_data
    assert "filled_quantity" in order_data
    assert "pending_quantity" in order_data
    assert "average_price" in order_data
    assert "rejection_reason" in order_data

    # 3. Query non-existent order ID
    missing_resp = client_tc.post("/api/v1/orderstatus", json={"orderid": "INVALID_999999", "apikey": "test"})
    assert missing_resp.status_code == 404
    assert missing_resp.json()["status"] == "error"

    # 4. Query without orderid
    bad_resp = client_tc.post("/api/v1/orderstatus", json={"apikey": "test"})
    assert bad_resp.status_code == 400


# =============================================================================
# 2. MultiQuotes API Tests (/api/v1/multiquotes)
# =============================================================================
def test_multiquotes_endpoint(client_tc):
    payload = {
        "apikey": "test",
        "symbols": [
            {"symbol": "RELIANCE", "exchange": "NSE"},
            {"symbol": "SILVER10030SEP26FUT", "exchange": "MCX"}
        ]
    }
    resp = client_tc.post("/api/v1/multiquotes", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert len(body["data"]) == 2

    q1 = body["data"][0]
    assert q1["symbol"] == "RELIANCE"
    assert q1["exchange"] == "NSE"
    assert q1["ltp"] > 0
    assert "open" in q1
    assert "high" in q1
    assert "low" in q1
    assert "close" in q1
    assert "change" in q1
    assert "volume" in q1

    # Bad request test
    bad_resp = client_tc.post("/api/v1/multiquotes", json={"symbols": [], "apikey": "test"})
    assert bad_resp.status_code == 400


# =============================================================================
# 3. Market Depth DOM API Tests (/api/v1/depth)
# =============================================================================
def test_market_depth_endpoint(client_tc):
    payload = {
        "apikey": "test",
        "symbol": "SILVER10030SEP26FUT",
        "exchange": "MCX"
    }
    resp = client_tc.post("/api/v1/depth", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    data = body["data"]

    assert data["symbol"] == "SILVER10030SEP26FUT"
    assert data["exchange"] == "MCX"
    assert data["ltp"] > 0

    # Verify 5 bid levels and 5 ask levels
    assert len(data["bids"]) == 5
    assert len(data["asks"]) == 5
    assert data["total_buy_qty"] > 0
    assert data["total_sell_qty"] > 0

    # Ensure bids are in descending price order and asks are ascending
    bid_prices = [b["price"] for b in data["bids"]]
    ask_prices = [a["price"] for a in data["asks"]]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)
    assert bid_prices[0] <= ask_prices[0]  # No negative spread


# =============================================================================
# 4. Supported Intervals API Tests (/api/v1/intervals)
# =============================================================================
def test_intervals_endpoint(client_tc):
    # GET method
    resp_get = client_tc.get("/api/v1/intervals?apikey=test")
    assert resp_get.status_code == 200
    body_get = resp_get.json()
    assert body_get["status"] == "success"
    assert "data" in body_get
    assert "minutes" in body_get["data"]
    assert "1m" in body_get["data"]["minutes"]
    assert "5m" in body_get["data"]["minutes"]
    assert "intervals" in body_get
    assert "D" in body_get["intervals"]

    # POST method
    resp_post = client_tc.post("/api/v1/intervals", json={"apikey": "test"})
    assert resp_post.status_code == 200
    assert resp_post.json()["status"] == "success"


# =============================================================================
# 5. Expiry Dates API Tests (/api/v1/expiry)
# =============================================================================
def test_expiry_dates_endpoint(client_tc):
    payload = {
        "apikey": "test",
        "symbol": "NIFTY",
        "exchange": "NFO",
        "instrumenttype": "options"
    }
    resp = client_tc.post("/api/v1/expiry", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "data" in body
    assert isinstance(body["data"], list)
    assert len(body["data"]) > 0


# =============================================================================
# 6. Market Calendar & Timings API Tests
# =============================================================================
def test_market_holidays_endpoints(client_tc):
    for endpoint in ("/api/v1/holidays", "/api/v1/market/holidays"):
        # GET
        resp_get = client_tc.get(f"{endpoint}?year=2025&apikey=test")
        assert resp_get.status_code == 200
        body_get = resp_get.json()
        assert body_get["status"] == "success"
        assert body_get["year"] == 2025
        assert len(body_get["data"]) > 5

        # POST
        resp_post = client_tc.post(endpoint, json={"year": 2026, "apikey": "test"})
        assert resp_post.status_code == 200
        assert resp_post.json()["year"] == 2026

def test_market_timings_endpoints(client_tc):
    for endpoint in ("/api/v1/timings", "/api/v1/market/timings"):
        # Weekday
        resp = client_tc.get(f"{endpoint}?date=2025-03-12&apikey=test")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["is_trading_day"] is True
        assert "NSE" in data["sessions"]
        assert "MCX" in data["sessions"]

        # Weekend
        resp_weekend = client_tc.post(endpoint, json={"date": "2025-03-15", "apikey": "test"})
        assert resp_weekend.status_code == 200
        assert resp_weekend.json()["data"]["is_trading_day"] is False


# =============================================================================
# 7. Single Option Greeks API Tests (/api/v1/optiongreeks)
# =============================================================================
def test_option_greeks_endpoint(client_tc):
    payload = {
        "apikey": "test",
        "symbol": "NIFTY28NOV2424000CE",
        "exchange": "NFO",
        "interest_rate": 7.0,
        "forward_price": 24200.0
    }
    resp = client_tc.post("/api/v1/optiongreeks", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["symbol"] == "NIFTY28NOV2424000CE"
    assert body["strike"] == 24000.0
    assert body["option_type"] == "CE"
    assert body["forward_price"] == 24200.0

    greeks = body["greeks"]
    assert "delta" in greeks
    assert "gamma" in greeks
    assert "theta" in greeks
    assert "vega" in greeks
    assert "rho" in greeks
    assert 0.0 <= greeks["delta"] <= 1.0  # Call delta must be between 0 and 1
    assert greeks["gamma"] >= 0.0


# =============================================================================
# 8. RMS Slippage Protection & Atomic Position Reversal Tests
# =============================================================================
def test_calculate_slippage_price():
    ltp = 1000.0
    # Buy slippage buffer +0.5%
    buy_price = order_services.calculate_slippage_price("RELIANCE", "BUY", ltp, buffer_pct=0.005, tick_size=0.05)
    assert buy_price == 1005.0

    # Sell slippage buffer -0.5%
    sell_price = order_services.calculate_slippage_price("RELIANCE", "SELL", ltp, buffer_pct=0.005, tick_size=0.05)
    assert sell_price == 995.0

@pytest.mark.asyncio
async def test_execute_reversal_order():
    # Test atomic order reversal service execution
    res = await order_services.execute_reversal_order(
        symbol="RELIANCE",
        new_action="BUY",
        new_quantity=15,
        price=2950.0,
        is_paper=True
    )
    assert res["status"] == "success"
    assert res["action"] == "BUY"
    assert res["quantity"] == 15
    assert "reversed" in res


# =============================================================================
# 9. UI Template & Static Controller Verification
# =============================================================================
def test_terminal_template_and_js_side_drawer():
    import os
    template_path = os.path.join(os.path.dirname(__file__), "..", "portal", "templates", "client_trading.html")
    js_path = os.path.join(os.path.dirname(__file__), "..", "portal", "static", "js", "trading_terminal.js")

    with open(template_path, "r") as f:
        html_content = f.read()
    with open(js_path, "r") as f:
        js_content = f.read()

    # Verify Drawer Elements in HTML
    assert "id=\"btn-toggle-drawer\"" in html_content
    assert "id=\"terminal-side-drawer\"" in html_content
    assert "data-tab=\"dom\"" in html_content
    assert "data-tab=\"orders\"" in html_content
    assert "data-tab=\"positions\"" in html_content
    assert "data-tab=\"trades\"" in html_content
    assert "id=\"dom-table-body\"" in html_content

    # Verify Drawer Methods in JS Controller
    assert "bindDrawerEvents" in js_content
    assert "fetchDOMDrawer" in js_content
    assert "renderDOM" in js_content
    assert "fetchOrdersDrawer" in js_content
    assert "fetchPositionsDrawer" in js_content
    assert "fetchTradesDrawer" in js_content
