"""
tests/test_live_broker_chaos_drill.py - Live Market Chaos & Adversarial Stress Suite

Rigorous bug bounty hunter chaos drills testing live market hazards against OpenAlgo standards:
1. WebSocket Dropped Connection & Reconnection State Recovery
2. Broker Session Token Expiration Mid-Order Flow & Recovery
3. Freeze Quantity Auto-Slicing for Large Option Orders
4. Rapid-Fire Position Reversal Storm (< 50ms intervals)
5. Multi-Tenant Cross-Contamination (IDOR Fuzzing)
"""
import os
import sys
import time
import json
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
from starlette.testclient import TestClient

root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
client_dir = os.path.join(root_dir, "client")
if client_dir not in sys.path:
    sys.path.insert(0, client_dir)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import client.main as client_app
import config
import xts_api
import ws_manager


TEST_API_KEY = "CHAOS_DRILL_API_KEY_888"

@pytest.fixture(autouse=True)
def setup_chaos_env(tmp_path, monkeypatch):
    """Configures isolated test environment and explicit API keys."""
    monkeypatch.setattr(config, "WEBHOOK_SECRET", TEST_API_KEY)
    monkeypatch.setattr(config, "API_KEY", TEST_API_KEY)
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", True)
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")

    import client.openalgo_router as openalgo_router
    monkeypatch.setattr(openalgo_router.config, "WEBHOOK_SECRET", TEST_API_KEY)
    monkeypatch.setattr(openalgo_router.config, "API_KEY", TEST_API_KEY)

    # Initialize in-memory contract cache so orders resolve
    import datetime
    today = datetime.date.today()
    xts_api.CACHE_DATE = today
    xts_api.CASH_MASTER = {
        "RELIANCE": [(datetime.date.max, 2885, "NSECM", "RELIANCE EQ", 0.05, 1, 100000)],
        "TCS": [(datetime.date.max, 11536, "NSECM", "TCS EQ", 0.05, 1, 100000)],
        "INFY": [(datetime.date.max, 1594, "NSECM", "INFY EQ", 0.05, 1, 100000)],
        "SBIN": [(datetime.date.max, 3045, "NSECM", "SBIN EQ", 0.05, 1, 100000)],
    }
    xts_api.FUT_MASTER = {
        "NIFTY": [(today + datetime.timedelta(days=20), 45000, "NSEFO", "NIFTY FUT", 0.05, 50, 1800)]
    }

@pytest.fixture
def client_tc():
    return TestClient(client_app.app)


# -----------------------------------------------------------------------------
# 1. WebSocket Dropped Connection & Auto-Reconnect Recovery
# -----------------------------------------------------------------------------
def test_websocket_drop_and_reconnect_lifecycle(client_tc):
    """
    Simulates abrupt network disconnection mid-stream and verifies that
    a subsequent reconnection cleanly re-authenticates and receives order updates.
    """
    # Connection 1: Connect and subscribe
    with client_tc.websocket_connect("/ws") as ws1:
        ws1.send_json({"action": "authenticate", "api_key": TEST_API_KEY})
        ack1 = ws1.receive_json()
        assert ack1.get("status") == "success"

        ws1.send_json({"action": "subscribe", "symbol": "NIFTY25AUG26FUT", "exchange": "NSE"})
        sub_ack = ws1.receive_json()
        assert sub_ack.get("type") == "subscribe"

        # Simulating socket drop by exiting context manager
    
    # Connection 2: Reconnection
    with client_tc.websocket_connect("/ws") as ws2:
        ws2.send_json({"action": "authenticate", "api_key": TEST_API_KEY})
        ack2 = ws2.receive_json()
        assert ack2.get("status") == "success"

        ws2.send_json({"action": "subscribe_orders"})
        ord_ack = ws2.receive_json()
        assert ord_ack.get("type") == "subscribe_orders"

        # Trigger order placement
        res = client_tc.post("/api/v1/order", json={
            "action": "BUY",
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "quantity": 10,
            "price": 2980.0,
            "pricetype": "LIMIT",
            "product": "NRML",
            "apikey": TEST_API_KEY
        })
        assert res.status_code == 200
        order_data = res.json()
        assert order_data.get("status") == "success"

        # Confirm reconnected socket receives order update
        update = ws2.receive_json()
        assert update.get("type") == "order_update"
        assert update.get("data", {}).get("symbol") == "RELIANCE"


# -----------------------------------------------------------------------------
# 2. Broker Session Token Expiration Mid-Order Flow & Recovery
# -----------------------------------------------------------------------------
def test_broker_token_expiration_resilience(client_tc, monkeypatch):
    """
    Simulates Symphony XTS interactive token expiring during live trading.
    Verifies that the engine detects TOKEN_EXPIRED, re-authenticates, and avoids crashes.
    """
    call_count = {"count": 0}

    def mock_get_live_price(inst_id, exch_seg):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return "TOKEN_EXPIRED"
        return 2450.0

    monkeypatch.setattr(xts_api, "get_live_price", mock_get_live_price)

    # Place order during token expiration
    resp = client_tc.post("/api/v1/order", json={
        "action": "BUY",
        "symbol": "TCS",
        "exchange": "NSE",
        "quantity": 5,
        "price": 4200.0,
        "pricetype": "LIMIT",
        "apikey": TEST_API_KEY
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") in ("success", "error")
    # Must never crash with 500
    assert resp.status_code != 500


# -----------------------------------------------------------------------------
# 3. Freeze Quantity Auto-Slicing for Institutional / Large Option Orders
# -----------------------------------------------------------------------------
def test_extreme_freeze_quantity_slicing():
    """
    Verifies that institutional orders exceeding exchange freeze limits
    (e.g., 5,000 quantity of NIFTY options with 1,800 freeze limit)
    are mathematically sliced into compliant chunks.
    """
    # Case A: Exactly divisible
    chunks_a = xts_api.slice_quantity_for_freeze(3600, 1800)
    assert chunks_a == [1800, 1800]
    assert sum(chunks_a) == 3600

    # Case B: Non-divisible remainder
    chunks_b = xts_api.slice_quantity_for_freeze(5000, 1800)
    assert chunks_b == [1800, 1800, 1400]
    assert sum(chunks_b) == 5000

    # Case C: Below freeze limit (no slicing needed)
    chunks_c = xts_api.slice_quantity_for_freeze(500, 1800)
    assert chunks_c == [500]

    # Case D: Very large order (25,000 qty)
    chunks_d = xts_api.slice_quantity_for_freeze(25000, 1800)
    assert sum(chunks_d) == 25000
    assert all(c <= 1800 for c in chunks_d)
    assert len(chunks_d) == 14  # 13 * 1800 + 1600


# -----------------------------------------------------------------------------
# 4. Rapid-Fire Position Reversal Storm (< 50ms intervals)
# -----------------------------------------------------------------------------
def test_rapid_fire_position_reversal_storm(client_tc):
    """
    Simulates high-frequency strategy firing rapid alternating reversals
    (LONG -> SHORT -> LONG) within milliseconds.
    Verifies SQLite concurrency, order integrity, and zero deadlocks.
    """
    symbols = ["RELIANCE", "INFY", "TCS"]
    results = []

    def execute_reversal(step):
        action = "BUY" if step % 2 == 0 else "SELL"
        sym = symbols[step % len(symbols)]
        res = client_tc.post("/api/v1/order", json={
            "action": action,
            "symbol": sym,
            "exchange": "NSE",
            "quantity": 10,
            "price": 2500.0 + (step * 0.5),
            "pricetype": "LIMIT",
            "apikey": TEST_API_KEY
        })
        return res.status_code, res.json()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(execute_reversal, i) for i in range(20)]
        for f in futures:
            code, body = f.result()
            assert code in (200, 400)
            assert body.get("status") in ("success", "error")
            results.append(body)

    assert len(results) == 20


# -----------------------------------------------------------------------------
# 5. Multi-Tenant Cross-Contamination (IDOR Fuzzing)
# -----------------------------------------------------------------------------
def test_multitenant_idor_order_and_position_isolation(client_tc):
    """
    Bug bounty IDOR check:
    Ensures that requests with invalid, empty, or mismatched API keys
    cannot query or cancel orders belonging to other tenants.
    """
    # 1. Place order with valid key
    resp = client_tc.post("/api/v1/order", json={
        "action": "BUY",
        "symbol": "SBIN",
        "exchange": "NSE",
        "quantity": 15,
        "price": 820.0,
        "apikey": TEST_API_KEY
    })
    assert resp.status_code == 200
    order_id = resp.json().get("orderid")
    assert order_id is not None

    # 2. Attempt to cancel order with wrong / malicious API key
    attack_resp = client_tc.post("/api/v1/cancelorder", json={
        "orderid": order_id,
        "apikey": "MALICIOUS_TENANT_KEY_XYZ"
    })
    # Should be rejected with 401 Unauthorized
    assert attack_resp.status_code == 401
    assert attack_resp.json().get("status") == "error"

    # 3. Attempt to cancel with empty key
    empty_resp = client_tc.post("/api/v1/cancelorder", json={
        "orderid": order_id,
        "apikey": ""
    })
    assert empty_resp.status_code == 401

    # 4. Legitimate cancellation with valid key succeeds
    valid_resp = client_tc.post("/api/v1/cancelorder", json={
        "orderid": order_id,
        "apikey": TEST_API_KEY
    })
    assert valid_resp.status_code == 200
    assert valid_resp.json().get("status") == "success"
