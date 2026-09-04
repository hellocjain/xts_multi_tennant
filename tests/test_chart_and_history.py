"""
test_chart_and_history.py - Complete Verification Suite for OpenAlgo Charts Integration.
Tests:
1. /api/v1/history endpoint format & multi-timeframe candle response
2. CandleService SQLite caching & deterministic synthetic generation
3. /ws WebSocket authentication, subscription, and live tick delivery
4. On-chart order placement, modification, and live order_update broadcast
5. /client/trading page and static bundle delivery
"""
import pytest
import os
import json
import time
from fastapi.testclient import TestClient
import client.main as client_app
import portal.main as portal_app
import candle_service

@pytest.fixture
def client_tc():
    return TestClient(client_app.app)

@pytest.fixture
def portal_tc():
    return TestClient(portal_app.app)

# -----------------------------------------------------------------------------
# 1. Historical Candles API (/api/v1/history)
# -----------------------------------------------------------------------------
def test_history_endpoint_schema_and_intervals(client_tc):
    """Verifies that /api/v1/history produces valid OpenAlgo-compatible OHLCV candles."""
    for interval in ("1m", "5m", "15m", "D"):
        resp = client_tc.post("/api/v1/history", json={
            "symbol": "SILVER10030SEP26FUT",
            "exchange": "MCX",
            "interval": interval,
            "apikey": "test"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "success"
        candles = data.get("data", [])
        assert len(candles) > 0

        # Validate candle structure and math invariants
        for c in candles[:10]:
            assert "timestamp" in c
            assert "open" in c
            assert "high" in c
            assert "low" in c
            assert "close" in c
            assert "volume" in c
            assert "oi" in c
            assert c["high"] >= max(c["open"], c["close"])
            assert c["low"] <= min(c["open"], c["close"])
            assert c["low"] > 0

def test_history_get_method_support(client_tc):
    """Verifies GET method support for /api/v1/history."""
    resp = client_tc.get("/api/v1/history?symbol=INFY&exchange=NSE&interval=5m&apikey=test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]) > 0

# -----------------------------------------------------------------------------
# 2. SQLite Candle Caching
# -----------------------------------------------------------------------------
def test_candle_service_sqlite_caching():
    """Verifies candle insertion, retrieval, and persistence in SQLite cache."""
    cs = candle_service.CandleService(db_path=":memory:")
    now = int(time.time())
    test_candles = [
        {"timestamp": now - 600, "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 100, "oi": 50},
        {"timestamp": now - 300, "open": 102.0, "high": 108.0, "low": 101.0, "close": 107.0, "volume": 150, "oi": 60},
    ]

    cs.save_candles("TESTSYM", "NSE", "5m", test_candles)
    cached = cs.get_cached_candles("TESTSYM", "NSE", "5m", now - 1000, now)

    assert len(cached) == 2
    assert cached[0]["close"] == 102.0
    assert cached[1]["close"] == 107.0

# -----------------------------------------------------------------------------
# 3. WebSocket Streaming (/ws)
# -----------------------------------------------------------------------------
def test_websocket_openalgo_tick_streaming(client_tc):
    """Verifies WebSocket handshake, authentication, symbol subscription, and live tick delivery."""
    with client_tc.websocket_connect("/ws") as ws:
        # 1. Ping / Pong
        ws.send_json({"action": "ping"})
        msg = ws.receive_json()
        assert msg.get("type") == "pong"

        # 2. Authenticate
        ws.send_json({"action": "authenticate", "api_key": "test"})
        auth_ack = ws.receive_json()
        assert auth_ack.get("type") == "auth"
        assert auth_ack.get("status") == "success"

        # 3. Subscribe to market data
        ws.send_json({"action": "subscribe", "symbol": "SILVER10030SEP26FUT", "exchange": "MCX", "mode": 1})
        sub_ack = ws.receive_json()
        assert sub_ack.get("type") == "subscribe"
        assert sub_ack.get("symbol") == "SILVER10030SEP26FUT"

        # 4. Immediate tick delivery
        tick = ws.receive_json()
        assert tick.get("type") == "market_data"
        assert tick.get("symbol") == "SILVER10030SEP26FUT"
        assert "ltp" in tick.get("data", {})
        assert tick["data"]["ltp"] > 0

# -----------------------------------------------------------------------------
# 4. On-Chart Order Lifecycle & Real-Time Broadcast
# -----------------------------------------------------------------------------
def test_on_chart_order_placement_and_ws_broadcast(client_tc):
    """Verifies that placing and modifying orders broadcasts real-time updates over WebSocket."""
    with client_tc.websocket_connect("/ws") as ws:
        # Authenticate & listen to order updates
        ws.send_json({"action": "authenticate", "api_key": "test"})
        ws.receive_json()
        ws.send_json({"action": "subscribe_orders"})
        ord_sub_ack = ws.receive_json()
        assert ord_sub_ack.get("type") == "subscribe_orders"

        # Place an order via /api/v1/order
        res = client_tc.post("/api/v1/order", json={
            "action": "BUY",
            "symbol": "SILVER10030SEP26FUT",
            "exchange": "MCX",
            "quantity": 1,
            "price": 2350.0,
            "pricetype": "LIMIT",
            "product": "NRML",
            "apikey": "test"
        })
        assert res.status_code == 200
        order_res = res.json()
        assert order_res.get("status") == "success"
        order_id = order_res.get("orderid")
        assert order_id is not None

        # Verify WebSocket received order_update frame
        update_frame = ws.receive_json()
        assert update_frame.get("type") == "order_update"
        data = update_frame.get("data", {})
        assert data.get("orderId") == str(order_id)
        assert data.get("symbol") == "SILVER10030SEP26FUT"
        assert data.get("action") == "BUY"

        # Modify the order
        mod_res = client_tc.post("/api/v1/modifyorder", json={
            "orderid": str(order_id),
            "price": 2355.0,
            "quantity": 1,
            "apikey": "test"
        })
        assert mod_res.status_code == 200
        assert mod_res.json().get("status") == "success"

        # Verify WebSocket received modified order_update frame
        mod_frame = ws.receive_json()
        assert mod_frame.get("type") == "order_update"
        assert mod_frame["data"]["price"] == 2355.0

# -----------------------------------------------------------------------------
# 5. Static Bundle Assets & Trading Terminal Page
# -----------------------------------------------------------------------------
def test_static_assets_served_by_portal(portal_tc):
    """Verifies that openalgo-charts.bundle.js and trading_terminal.js are served with HTTP 200."""
    res1 = portal_tc.get("/static/js/openalgo-charts.bundle.js")
    assert res1.status_code == 200
    assert len(res1.content) > 100_000 # Bundle is ~490KB

    res2 = portal_tc.get("/static/js/trading_terminal.js")
    assert res2.status_code == 200
    assert b"class OpenAlgoTradingTerminal" in res2.content

def test_client_trading_page_structure(tmp_path, monkeypatch):
    """Verifies that /client/trading renders the full-screen terminal with chart canvas and trading pills."""
    import database
    import security
    test_db = str(tmp_path / "portal_trading_test.db")
    monkeypatch.setattr(database, "get_db_path", lambda: test_db)
    database.init_portal_db()

    with database.closing(database.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("tenant_demo", "AC Agarwal Trader", "ACTIVE", time.time(), time.time())
            )
            conn.execute(
                "INSERT INTO client_users (id, tenant_id, username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("user_demo", "tenant_demo", "demo_trader", "hash_pw", time.time(), time.time())
            )
            enc_creds = security.encrypt_credentials({
                "API_KEY": "KEY_DEMO_123",
                "API_SECRET": "SECRET_DEMO",
                "CLIENT_ID": "DEMO01",
                "WEBHOOK_SECRET": "WH_DEMO"
            })
            conn.execute(
                "INSERT INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES (?, ?, ?)",
                ("tenant_demo", enc_creds, time.time())
            )

    portal_tc = TestClient(portal_app.app)
    session_token = security.create_client_session("user_demo", "tenant_demo", "127.0.0.1", "test-agent")
    portal_tc.cookies.set("client_session", session_token)

    res = portal_tc.get("/client/trading")
    assert res.status_code == 200
    html = res.text

    assert "Trading Terminal" in html
    assert "chart-container" in html
    assert "pill-sell-btn" in html
    assert "pill-buy-btn" in html
    assert "openalgo-charts.bundle.js" in html
    assert "trading_terminal.js" in html
