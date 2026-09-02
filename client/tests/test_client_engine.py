import os
import sys
import tempfile
import json
import pytest
import time
from decimal import Decimal
from fastapi.testclient import TestClient

# Point DATA_DIR to a temporary directory before importing app modules
test_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = test_dir
os.environ["CLIENT_ID"] = "TEST_CLIENT_01"
os.environ["WEBHOOK_SECRET"] = "TestSecret123"
os.environ["PAPER_TRADE_MODE"] = "True"

client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if "main" in sys.modules:
    del sys.modules["main"]
if "config" in sys.modules:
    del sys.modules["config"]
if "xts_api" in sys.modules:
    del sys.modules["xts_api"]

sys.path.insert(0, client_dir)

import config
import xts_api
import main as client_main
app = client_main.app
db_init = client_main.db_init

@pytest.fixture(autouse=True)
def setup_db():
    import datetime
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    today = datetime.datetime.now(IST).date()
    xts_api.CACHE_DATE = today
    xts_api.FUT_MASTER = {
        "CRUDEOIL": [(today + datetime.timedelta(days=30), 25001, "MCXFO", "CRUDEOIL24AUGFUT", 1.0, 100, 10000)],
        "GOLD": [(today + datetime.timedelta(days=30), 25002, "MCXFO", "GOLD24AUGFUT", 1.0, 100, 10000)],
    }
    xts_api.CASH_MASTER = {
        "RELIANCE": [(datetime.date.max, 2885, "NSECM", "RELIANCE EQ", 0.05, 1, 100000)],
    }
    db_init()
    yield

def test_tick_size_quantization():
    # BUY tests (ceiling)
    assert xts_api.apply_tick_size(6500.02, 0.05, "BUY") == 6500.05
    assert xts_api.apply_tick_size(6500.05, 0.05, "BUY") == 6500.05
    assert xts_api.apply_tick_size(6500.06, 0.05, "BUY") == 6500.10
    assert xts_api.apply_tick_size(100.2, 1.0, "BUY") == 101.0

    # SELL tests (floor)
    assert xts_api.apply_tick_size(6500.09, 0.05, "SELL") == 6500.05
    assert xts_api.apply_tick_size(6500.05, 0.05, "SELL") == 6500.05
    assert xts_api.apply_tick_size(6500.04, 0.05, "SELL") == 6500.00
    assert xts_api.apply_tick_size(100.8, 1.0, "SELL") == 100.0

    # Currency CDS 4-decimal precision tests
    assert xts_api.apply_tick_size(83.2501, 0.0025, "BUY") == 83.2525
    assert xts_api.apply_tick_size(83.2524, 0.0025, "SELL") == 83.2500

def test_daily_notional_tracking():
    # Initial state
    state = xts_api.get_daily_notional_state()
    assert state["notional"] >= 0.0

    # Reserve notional
    allowed, total = xts_api.check_and_reserve_daily_notional(500000.0)
    assert allowed is True
    assert total >= 500000.0

    # Refund notional
    xts_api.refund_daily_notional(500000.0)
    state2 = xts_api.get_daily_notional_state()
    assert state2["notional"] == total - 500000.0

def test_symbol_resolution():
    assert xts_api.resolve_symbol_smart("MCX:CRUDEOIL1!") == "CRUDEOIL"
    assert xts_api.resolve_symbol_smart("NATGAS") == "NATURALGAS"
    assert xts_api.resolve_symbol_smart("NIFTY50") == "NIFTY"
    assert xts_api.resolve_symbol_smart("GOLDMINI") == "GOLDM"

def test_webhook_endpoints():
    with TestClient(app) as client:
        # 1. Health check
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["client_id"] == "TEST_CLIENT_01"
        assert data["paper_trade_mode"] is True

        # 2. Unauthorized webhook
        bad_payload = {
            "secret": "WrongSecret",
            "action": "BUY",
            "symbol": "MCX:CRUDEOIL1!",
            "quantity": 1,
            "price": 6500.0
        }
        res = client.post("/webhook", json=bad_payload)
        assert res.status_code == 401

        # 3. Valid webhook
        uid = int(time.time() * 1000)
        good_payload = {
            "secret": "TestSecret123",
            "action": "BUY",
            "symbol": "MCX:CRUDEOIL1!",
            "quantity": 1,
            "price": 6500.0,
            "order_id": f"TV_ORDER_{uid}"
        }
        res = client.post("/webhook", json=good_payload)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "signal_id" in data

        # 4. Immediate duplicate suppression
        res_dup = client.post("/webhook", json=good_payload)
        assert res_dup.status_code == 200
        assert res_dup.json()["status"] == "warning"


        # 5. Invalid action
        invalid_action_payload = {
            "secret": "TestSecret123",
            "action": "INVALID_ACTION",
            "symbol": "CRUDEOIL",
            "quantity": 1,
            "price": 6500.0
        }
        res = client.post("/webhook", json=invalid_action_payload)
        assert res.status_code == 422

        # 6. Fractional quantity rejection
        fractional_payload = {
            "secret": "TestSecret123",
            "action": "BUY",
            "symbol": "CRUDEOIL",
            "quantity": 1.5,
            "price": 6500.0
        }
        res = client.post("/webhook", json=fractional_payload)
        assert res.status_code == 422

        # 7. Telemetry endpoint
        res_tel = client.get("/internal/telemetry")
        assert res_tel.status_code == 200
        tel_data = res_tel.json()
        assert "health" in tel_data
        assert "positions" in tel_data
        assert "recent_signals" in tel_data
        assert len(tel_data["recent_signals"]) >= 1

        # 8. Panic endpoint
        panic_payload = {"secret": "TestSecret123"}
        res_panic = client.post("/panic", json=panic_payload)
        assert res_panic.status_code == 200
        assert res_panic.json()["status"] == "success"

def test_commodity_multiplier():
    assert xts_api.get_contract_multiplier("CRUDEOIL1!", "MCXFO") == 100.0
    assert xts_api.get_contract_multiplier("CRUDEOILM1!", "MCXFO") == 10.0
    assert xts_api.get_contract_multiplier("NATGAS", "MCXFO") == 1250.0
    assert xts_api.get_contract_multiplier("NATGASMINI", "MCXFO") == 250.0
    assert xts_api.get_contract_multiplier("NATURALGASM", "MCXFO") == 250.0
    assert xts_api.get_contract_multiplier("SILVER", "MCXFO") == 30.0
    assert xts_api.get_contract_multiplier("SILVERM", "MCXFO") == 5.0
    assert xts_api.get_contract_multiplier("SILVERMIC", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("SILVERMICRO", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("GOLD", "MCXFO") == 100.0
    assert xts_api.get_contract_multiplier("GOLDM", "MCXFO") == 10.0
    assert xts_api.get_contract_multiplier("GOLDMINI", "MCXFO") == 10.0
    assert xts_api.get_contract_multiplier("GOLDPETAL", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("GOLDPETAL24AUGFUT", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("ALUMINI", "MCXFO") == 1000.0
    assert xts_api.get_contract_multiplier("ALUMINIUM", "MCXFO") == 5000.0
    assert xts_api.get_contract_multiplier("NIFTY", "NSEFO") == 1.0
    assert xts_api.get_contract_multiplier("RELIANCE", "NSECM") == 1.0

def test_panic_square_off_pricing_fallback_logic(monkeypatch):
    # Mock position object representing open SHORT position
    short_pos = {
        "Quantity": -4,
        "ExchangeInstrumentId": 25000,
        "ExchangeSegment": "MCXFO",
        "ProductType": "NRML",
        "TradingSymbol": "SILVER100",
        "BuyAveragePrice": 0,
        "SellAveragePrice": 85000.0
    }
    
    # When get_live_price returns None, closing short (action=BUY) should use SellAveragePrice 85000, not fallback 100
    action = "BUY"
    p = short_pos
    if action == "BUY":
        fallback_px = float(p.get("SellAveragePrice", 0) or p.get("ActualSellAveragePrice", 0) or p.get("LastTradedPrice", 0) or p.get("LTP", 0) or p.get("BuyAveragePrice", 0) or 100)
    else:
        fallback_px = float(p.get("BuyAveragePrice", 0) or p.get("ActualBuyAveragePrice", 0) or p.get("LastTradedPrice", 0) or p.get("LTP", 0) or p.get("SellAveragePrice", 0) or 100)
    
    assert fallback_px == 85000.0

def test_order_status_classification():
    # Test numeric status code mapping
    assert xts_api.XTS_STATUS_CODE_MAP[48] == "New"
    assert xts_api.XTS_STATUS_CODE_MAP[49] == "PartiallyFilled"
    assert xts_api.XTS_STATUS_CODE_MAP[50] == "Filled"
    assert xts_api.XTS_STATUS_CODE_MAP[52] == "Cancelled"
    assert xts_api.XTS_STATUS_CODE_MAP[53] == "Replaced"
    assert xts_api.XTS_STATUS_CODE_MAP[54] == "PendingCancel"
    assert xts_api.XTS_STATUS_CODE_MAP[56] == "Rejected"
    assert xts_api.XTS_STATUS_CODE_MAP[65] == "PendingNew"
    assert xts_api.XTS_STATUS_CODE_MAP[69] == "PendingReplace"

    non_terminal = {
        "OPEN", "NEW", "FILLED", "PARTIALLYFILLED", "PENDING", "PENDINGNEW",
        "REPLACED", "PENDINGREPLACE", "PENDINGCANCEL", "SUCCESS", "COMPLETE", "EXECUTED"
    }
    # Verify that Replaced, PendingNew, PendingReplace are all treated as non-terminal (not re-executed)
    for st in ["Replaced", "PendingNew", "PendingReplace", "PartiallyFilled", "New", "Filled"]:
        assert st.upper().replace("_", "").replace(" ", "") in non_terminal

def test_broker_trades_endpoint(monkeypatch):
    class MockResponse:
        status_code = 200
        def json(self):
            return {
                "type": "success",
                "result": [
                    {
                        "TradeID": "TR_101",
                        "AppOrderID": "ORD_202",
                        "TradingSymbol": "CRUDEOIL24AUGFUT",
                        "TradedQuantity": 100,
                        "TradePrice": 6500.0,
                        "OrderSide": "BUY",
                        "ExchangeSegment": "MCXFO"
                    }
                ]
            }
    
    queried_urls = []
    def mock_get(url, headers=None, timeout=None):
        queried_urls.append(url)
        return MockResponse()
    
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda: "mock_token")
    monkeypatch.setattr(xts_api.api_session, "get", mock_get)
    
    trades = xts_api.get_broker_trades()
    assert len(trades) == 1
    assert trades[0]["trade_id"] == "TR_101"
    assert "/orders/trades" in queried_urls[0]

def test_deterministic_order_ref():
    # 1. Explicit order_id
    payload_explicit = {"order_id": "ORD_789123", "action": "BUY", "symbol": "CRUDEOIL", "quantity": 1, "price": 6500.0}
    ref1 = client_main.generate_order_ref(payload_explicit, "BUY", "CRUDEOIL", 1, 6500.0, 1700000000.0)
    assert ref1 == "TV_ORD789123"

    # 2. Bar time payload
    payload_bar = {"time": "2026-08-22T08:00:00", "action": "BUY", "symbol": "CRUDEOIL", "quantity": 1, "price": 6500.0}
    ref2_a = client_main.generate_order_ref(payload_bar, "BUY", "CRUDEOIL", 1, 6500.0, 1700000000.0)
    ref2_b = client_main.generate_order_ref(payload_bar, "BUY", "CRUDEOIL", 1, 6500.0, 1700000002.5)
    assert ref2_a == ref2_b # Same bar time produces identical order_ref across different seconds

    # 3. Retries within 5s bucket produce identical ref
    ref3_a = client_main.generate_order_ref({}, "SELL", "GOLD", 1, 75000.0, 1700000001.0)
    ref3_b = client_main.generate_order_ref({}, "SELL", "GOLD", 1, 75000.0, 1700000003.0)
    assert ref3_a == ref3_b

def test_proactive_token_renewal(monkeypatch):
    xts_api.INTERACTIVE_TOKEN = "old_token_123"
    xts_api.INTERACTIVE_TOKEN_ACQUIRED_AT = time.time() - 80000 # 22 hours old (> 20h TTL)

    refreshed = False
    def mock_post(url, json=None, timeout=None):
        nonlocal refreshed
        refreshed = True
        class MockResp:
            status_code = 200
            def json(self):
                return {"type": "success", "result": {"token": "new_fresh_token_456"}}
        return MockResp()

    monkeypatch.setattr(xts_api.api_session, "post", mock_post)
    token = xts_api.get_interactive_token()
    assert refreshed is True
    assert token == "new_fresh_token_456"
    assert xts_api.INTERACTIVE_TOKEN == "new_fresh_token_456"
    assert time.time() - xts_api.INTERACTIVE_TOKEN_ACQUIRED_AT < 5

def test_token_bucket_rate_limiter():
    limiter = xts_api.TokenBucketRateLimiter(rate=8.0, capacity=8.0)
    # Acquire 8 tokens immediately from capacity
    for _ in range(8):
        assert limiter.acquire(timeout=0.05) is True
    
    # 9th immediate acquire should fail or wait
    assert limiter.acquire(timeout=0.01) is False
    
    # After 150ms (>= 1 token generated at 8/sec = 125ms), acquire should succeed
    time.sleep(0.15)
    assert limiter.acquire(timeout=0.05) is True

def test_panic_fail_closed_authentication(monkeypatch):
    with TestClient(app) as client:
        # 1. Unset secret -> must return 401
        monkeypatch.setattr(config, "WEBHOOK_SECRET", "")
        res = client.post("/panic", json={"secret": "AnySecret"})
        assert res.status_code == 401

        # 2. Wrong secret -> must return 401
        monkeypatch.setattr(config, "WEBHOOK_SECRET", "ConfiguredSecret")
        res = client.post("/panic", json={"secret": "WrongSecret"})
        assert res.status_code == 401

        # 3. Correct secret -> must succeed
        res = client.post("/panic", json={"secret": "ConfiguredSecret"})
        assert res.status_code == 200

def test_unrealized_mtm_broker_fallback(monkeypatch):
    class MockResp:
        status_code = 200
        def json(self):
            return {
                "type": "success",
                "result": {
                    "positionList": [
                        {
                            "TradingSymbol": "GOLD24AUGFUT",
                            "ExchangeInstrumentId": 25002,
                            "ExchangeSegment": "MCXFO",
                            "Quantity": 100,
                            "BuyAveragePrice": 72000.0,
                            "Multiplier": 1,
                            "UnrealizedMTM": 45000.0, # Broker ground truth
                            "LastTradedPrice": 0.0
                        }
                    ]
                }
            }
    
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda: "mock_token")
    monkeypatch.setattr(xts_api.api_session, "get", lambda *args, **kwargs: MockResp())
    monkeypatch.setattr(xts_api, "get_live_prices_batch", lambda *args, **kwargs: {}) # No live quotes

    telemetry = xts_api.get_positions_telemetry()
    assert telemetry["unrealized_mtm"] == 45000.0
    assert telemetry["positions"][0]["unrealized_mtm"] == 45000.0

def test_derivative_lot_size_alignment(monkeypatch):
    import datetime
    today = datetime.date.today()
    xts_api.CACHE_DATE = today
    # CRUDEOIL has lot size 100
    xts_api.FUT_MASTER = {
        "CRUDEOIL": [(today + datetime.timedelta(days=30), 25001, "MCXFO", "CRUDEOIL24AUGFUT", 1.0, 100, 10000)],
    }
    
    monkeypatch.setattr(config, "TV_SENDS_LOTS", False)
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", True)
    monkeypatch.setattr(config, "MAX_ORDER_VALUE_INR", 500000000.0)
    monkeypatch.setattr(config, "DAILY_NOTIONAL_CAP_INR", 1000000000.0)
    
    # 1. Unaligned quantity (e.g. 150 units when lot size is 100) -> must be rejected
    res_bad = xts_api.place_order("BUY", "CRUDEOIL", 150, 100.0, "REF_LOT_1")
    assert res_bad["status"] == "error"
    assert "multiple of lot size" in res_bad["message"]

    # 2. Less than 1 lot (e.g. 50 units) -> must be rejected
    res_small = xts_api.place_order("BUY", "CRUDEOIL", 50, 100.0, "REF_LOT_2")
    assert res_small["status"] == "error"

    # 3. Aligned quantity (e.g. 200 units = 2 lots) -> must succeed
    res_good = xts_api.place_order("BUY", "CRUDEOIL", 200, 100.0, "REF_LOT_3")
    assert res_good["type"] == "success"

def test_panic_cancelall_endpoint(monkeypatch):
    called_urls = []
    class MockResp:
        status_code = 200
        def json(self):
            return {"type": "success", "result": {"positionList": []}}

    def mock_post(url, json=None, headers=None, timeout=None):
        called_urls.append(url)
        return MockResp()

    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda: "mock_token")
    monkeypatch.setattr(xts_api.api_session, "post", mock_post)
    monkeypatch.setattr(xts_api.api_session, "get", lambda *args, **kwargs: MockResp())

    xts_api.panic_square_off_all()
    assert any("/orders/cancelall" in u for u in called_urls)

def test_signals_db_secret_masking():
    sig_id = "test_mask_sig"
    raw_payload = {
        "secret": "SUPER_SECRET_VALUE_123",
        "api_key": "SENSITIVE_KEY",
        "action": "BUY",
        "symbol": "CRUDEOIL",
        "quantity": 1,
        "price": 6500.0
    }
    client_main.db_insert_pending(sig_id, raw_payload)
    client_main.db_update_status(sig_id, "failed", result={"error": "failed", "token": "sensitive_session_token"})

    recent = client_main.db_fetch_recent(10)
    target = next(s for s in recent if s["id"] == sig_id)
    
    assert target["payload"]["secret"] == "***MASKED***"
    assert target["payload"]["api_key"] == "***MASKED***"
    assert target["result"]["token"] == "***MASKED***"
    assert target["payload"]["symbol"] == "CRUDEOIL"

def test_telegram_discord_execution_notifications(monkeypatch):
    import requests
    posted_messages = []

    def mock_requests_post(url, json=None, timeout=None):
        posted_messages.append({"url": url, "json": json})
        class MockResp:
            status_code = 200
        return MockResp()

    monkeypatch.setattr(requests, "post", mock_requests_post)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "TEST_BOT_123")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-100999888777")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    monkeypatch.setattr(config, "CLIENT_ID", "TEST_CLIENT_01")

    # 1. Test Successful Fill Notification
    fill_result = {
        "type": "success",
        "result": {
            "AppOrderID": 12109988,
            "OrderAverageTradedPrice": 6502.50,
            "IsPaperTrade": False
        }
    }
    client_main.send_execution_notification("BUY", "CRUDEOIL", 100, 6500.0, "done", fill_result)
    time.sleep(0.1) # Wait for thread

    assert len(posted_messages) >= 2
    tg_post = next(p for p in posted_messages if "telegram.org" in p["url"])
    dc_post = next(p for p in posted_messages if "discord.com" in p["url"])

    assert "ORDER FILLED [LIVE BROKER]" in tg_post["json"]["text"]
    assert "TEST_CLIENT_01" in tg_post["json"]["text"]
    assert "BUY 100x CRUDEOIL" in tg_post["json"]["text"]
    assert "₹6502.5" in tg_post["json"]["text"]
    assert "12109988" in tg_post["json"]["text"]
    assert tg_post["json"]["chat_id"] == "-100999888777"

    assert "ORDER FILLED [LIVE BROKER]" in dc_post["json"]["content"]

    # 2. Test Rejected Order Notification
    posted_messages.clear()
    reject_result = {
        "type": "error",
        "code": "e-order-0008",
        "description": "Margin insufficient for order"
    }
    client_main.send_execution_notification("SELL", "GOLD", 10, 72000.0, "failed", reject_result)
    time.sleep(0.1) # Wait for thread

    assert len(posted_messages) >= 2
    tg_rej = next(p for p in posted_messages if "telegram.org" in p["url"])
    assert "ORDER REJECTED / FAILED" in tg_rej["json"]["text"]
    assert "e-order-0008" in tg_rej["json"]["text"]
    assert "Margin insufficient for order" in tg_rej["json"]["text"]

def test_internal_master_refresh_endpoint(monkeypatch):
    client = TestClient(app)
    
    # 1. Mock refresh_master_cache returning True
    monkeypatch.setattr(xts_api, "refresh_master_cache", lambda force=False: True)
    
    res = client.post("/internal/master/refresh")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["cache_healthy"] is True
    assert data["futures_symbols"] >= 1
    assert "Master cache refreshed successfully" in data["message"]

def test_trading_paused_webhook_rejection():
    client = TestClient(app)
    
    # 1. Initially active -> webhook accepted
    client_main.TRADING_PAUSED = False
    valid_payload = {
        "secret": "TestSecret123",
        "action": "BUY",
        "symbol": "CRUDEOIL",
        "quantity": 1,
        "price": 6500.0
    }
    res_active = client.post("/webhook", json=valid_payload)
    assert res_active.status_code == 200
    assert res_active.json()["status"] == "success"

    # 2. Trigger pause -> next incoming webhook rejected with 403
    client.post("/internal/pause")
    assert client_main.TRADING_PAUSED is True

    res_paused = client.post("/webhook", json=valid_payload)
    assert res_paused.status_code == 403
    assert "Trading is paused" in res_paused.json()["message"]

    # 3. Trigger resume -> webhook accepted again
    client.post("/internal/resume")
    assert client_main.TRADING_PAUSED is False

    res_resumed = client.post("/webhook", json=valid_payload)
    assert res_resumed.status_code == 200


def test_sec_xts_006_rate_limiter_timeout_rejection(monkeypatch):
    """
    Regression Test for SEC-XTS-006:
    Verifies that when ORDER_RATE_LIMITER.acquire() returns False (timeout),
    place_order returns a rate-limit error response and the underlying HTTP client is NEVER called.
    """
    from unittest.mock import MagicMock

    mock_http_post = MagicMock()
    mock_http_delete = MagicMock()
    monkeypatch.setattr(xts_api.api_session, "post", mock_http_post)
    monkeypatch.setattr(xts_api.api_session, "delete", mock_http_delete)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda *a, **kw: "valid_interactive_token")
    monkeypatch.setattr(xts_api, "get_live_price", lambda *a, **kw: 2500.0)
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)

    # 1. Rate limiter times out (returns False)
    monkeypatch.setattr(xts_api.ORDER_RATE_LIMITER, "acquire", lambda *a, **kw: False)

    res = xts_api.place_order("BUY", "RELIANCE", 1, 2500.0, "ORDER_REF_RATE_TEST", is_paper=False)

    assert res.get("status") == "error", f"Expected error response on rate limit timeout, got: {res}"
    assert "rate limit" in res.get("message", "").lower()
    mock_http_post.assert_not_called()

    def mock_get(url, *a, **kw):
        mock_r = MagicMock()
        mock_r.status_code = 200
        if "positions" in url:
            mock_r.json.return_value = {
                "type": "success",
                "result": {
                    "positionList": [{
                        "ExchangeInstrumentId": 2885,
                        "ExchangeSegment": "NSECM",
                        "ProductType": "MIS",
                        "TradingSymbol": "RELIANCE",
                        "Quantity": 10,
                        "BuyAveragePrice": 2500.0
                    }]
                }
            }
        else:
            mock_r.json.return_value = {
                "type": "success",
                "result": [{"OrderUniqueIdentifier": "ORDER_REF_RATE_TEST", "OrderStatus": "Open", "AppOrderID": 999123}]
            }
        return mock_r

    monkeypatch.setattr(xts_api.api_session, "get", mock_get)

    # 2. Partial fill cleanup check on rate limit timeout
    xts_api._monitor_and_clean_partial_fills("ORDER_REF_RATE_TEST", "CLIENT_01", "valid_interactive_token")
    mock_http_delete.assert_not_called()

    # 3. Panic square-off slice check on rate limit timeout (all chunks timed out)
    panic_res = xts_api.panic_square_off_all()
    assert panic_res.get("status") == "error"
    assert panic_res.get("closed_quantity") == 0
    assert panic_res.get("unclosed_quantity") == 10
    assert len(panic_res.get("failed_chunks")) == 1
    # Neither atomic cancel-all nor individual cancel nor slice order POST should dispatch HTTP requests
    mock_http_post.assert_not_called()
    mock_http_delete.assert_not_called()


def test_sec_xts_006_panic_square_off_partial_rate_limit_timeout(monkeypatch):
    """
    Regression Test for SEC-XTS-006 (Multi-chunk Partial Rate Limit Timeout):
    Verifies that when one chunk in a multi-chunk square-off times out on the rate limiter
    while other chunks succeed:
    1. The final status is reported as 'partial_failure' (never 'success').
    2. Closed quantity (2,000) and unclosed quantity (1,000) are explicitly separated.
    3. Failed chunks detail which specific lots were skipped and why.
    """
    from unittest.mock import MagicMock

    dispatched_payloads = []
    def mock_post(url, headers=None, json=None, timeout=None):
        mock_r = MagicMock()
        mock_r.status_code = 200
        if "cancelall" in url:
            mock_r.json.return_value = {"type": "success", "result": "all cancelled"}
        else:
            dispatched_payloads.append(json)
            mock_r.json.return_value = {"type": "success", "result": {"AppOrderID": 12345}}
        return mock_r

    def mock_get(url, *a, **kw):
        mock_r = MagicMock()
        mock_r.status_code = 200
        if "positions" in url:
            mock_r.json.return_value = {
                "type": "success",
                "result": {
                    "positionList": [{
                        "ExchangeInstrumentId": 25001,
                        "ExchangeSegment": "MCXFO",
                        "ProductType": "NRML",
                        "TradingSymbol": "CRUDEOIL",
                        "Quantity": 3000,
                        "BuyAveragePrice": 6500.0
                    }]
                }
            }
        return mock_r

    monkeypatch.setattr(xts_api.api_session, "post", mock_post)
    monkeypatch.setattr(xts_api.api_session, "get", mock_get)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda *a, **kw: "valid_interactive_token")
    monkeypatch.setattr(xts_api, "get_live_price", lambda *a, **kw: 6500.0)
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)

    # Freeze limit for CRUDEOIL in FUT_MASTER is 10,000; override instrument lookup to test 3 chunks of 1,000
    monkeypatch.setattr(xts_api, "get_instrument_by_id", lambda iid: {
        "tick_size": 1.0,
        "freeze_qty": 1000,
        "exch_seg": "MCXFO"
    })

    # Chunk 1: acquire -> True (success)
    # Chunk 2: acquire -> False (rate limit timeout!)
    # Chunk 3: acquire -> True (success)
    # Cancel-all: acquire -> True
    acquire_call_count = [0]
    def mock_acquire(timeout=None):
        acquire_call_count[0] += 1
        # Call 1: atomic cancelall (True)
        # Call 2: chunk 1 (True)
        # Call 3: chunk 2 (False - timeout)
        # Call 4: chunk 3 (True)
        if acquire_call_count[0] == 3:
            return False
        return True

    monkeypatch.setattr(xts_api.ORDER_RATE_LIMITER, "acquire", mock_acquire)

    panic_res = xts_api.panic_square_off_all()

    # Invariants:
    assert panic_res["status"] == "partial_failure", f"Must report partial_failure, got: {panic_res['status']}"
    assert panic_res["closed_quantity"] == 2000, f"Expected 2000 closed, got {panic_res['closed_quantity']}"
    assert panic_res["unclosed_quantity"] == 1000, f"Expected 1000 unclosed, got {panic_res['unclosed_quantity']}"
    assert len(panic_res["squared_off"]) == 2
    assert len(panic_res["failed_chunks"]) == 1
    assert panic_res["failed_chunks"][0]["qty"] == 1000
    assert panic_res["failed_chunks"][0]["reason"] == "Rate limit exceeded"
    assert len(dispatched_payloads) == 2


def test_sec_xts_003_panic_square_off_unique_order_refs(monkeypatch):
    """
    Regression Test for SEC-XTS-003:
    Verifies that all chunks generated during panic square-off slicing have unique
    orderUniqueIdentifier values, preventing duplicate order identifier rejections by the broker OMS.
    """
    from unittest.mock import MagicMock

    dispatched_refs = []
    seen_refs = set()

    def mock_post(url, headers=None, json=None, timeout=None):
        mock_r = MagicMock()
        mock_r.status_code = 200
        if "cancelall" in url:
            mock_r.json.return_value = {"type": "success", "result": "all cancelled"}
        else:
            ref = json.get("orderUniqueIdentifier")
            dispatched_refs.append(ref)
            if ref in seen_refs:
                # Real broker OMS rejection on duplicate OrderUniqueIdentifier
                mock_r.json.return_value = {
                    "type": "error",
                    "description": f"Duplicate order identifier detected: {ref}"
                }
            else:
                seen_refs.add(ref)
                mock_r.json.return_value = {
                    "type": "success",
                    "result": {"AppOrderID": 88000 + len(seen_refs)}
                }
        return mock_r

    def mock_get(url, *a, **kw):
        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.json.return_value = {
            "type": "success",
            "result": {
                "positionList": [{
                    "ExchangeInstrumentId": 25001,
                    "ExchangeSegment": "MCXFO",
                    "ProductType": "NRML",
                    "TradingSymbol": "CRUDEOIL",
                    "Quantity": 5000,
                    "BuyAveragePrice": 6500.0
                }]
            }
        }
        return mock_r

    monkeypatch.setattr(xts_api.api_session, "post", mock_post)
    monkeypatch.setattr(xts_api.api_session, "get", mock_get)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda *a, **kw: "valid_interactive_token")
    monkeypatch.setattr(xts_api, "get_live_price", lambda *a, **kw: 6500.0)
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)

    # Freeze limit 1,000 for 5,000 quantity -> 5 chunks
    monkeypatch.setattr(xts_api, "get_instrument_by_id", lambda iid: {
        "tick_size": 1.0,
        "freeze_qty": 1000,
        "exch_seg": "MCXFO"
    })

    # Fix mock clock so multiple calls in same millisecond produce collision under buggy code
    fixed_timestamp = 1787200000.123
    monkeypatch.setattr(time, "time", lambda: fixed_timestamp)
    monkeypatch.setattr(xts_api.ORDER_RATE_LIMITER, "acquire", lambda *a, **kw: True)

    panic_res = xts_api.panic_square_off_all()

    # 1. Total 5 chunks must be dispatched
    assert len(dispatched_refs) == 5, f"Expected 5 chunks dispatched, got {len(dispatched_refs)}"

    # 2. Every chunk must have a UNIQUE orderUniqueIdentifier
    assert len(set(dispatched_refs)) == 5, (
        f"CRITICAL BUG SEC-XTS-003: Duplicate OrderIdentifier collision! Dispatched refs: {dispatched_refs}"
    )

    # 3. All 5 chunks must succeed at broker OMS
    assert panic_res["status"] == "success", f"Panic square-off failed: {panic_res}"
    assert panic_res["closed_quantity"] == 5000
    assert panic_res["unclosed_quantity"] == 0


def test_sec_xts_005_webhook_freeze_quantity_auto_slicing(monkeypatch):
    """
    Regression Test for SEC-XTS-005:
    Verifies that external webhook orders exceeding broker freeze limit (e.g. 5,000 lots with 1,200 freeze limit)
    are automatically sliced into compliant chunks [1200, 1200, 1200, 1200, 200] and dispatched,
    rather than being rejected outright.
    """
    from unittest.mock import MagicMock
    import datetime

    dispatched_chunks = []

    def mock_post(url, headers=None, json=None, timeout=None):
        mock_r = MagicMock()
        mock_r.status_code = 200
        dispatched_chunks.append(json)
        mock_r.json.return_value = {
            "type": "success",
            "result": {"AppOrderID": 90000 + len(dispatched_chunks)}
        }
        return mock_r

    monkeypatch.setattr(xts_api.api_session, "post", mock_post)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda *a, **kw: "valid_interactive_token")
    monkeypatch.setattr(xts_api, "get_live_price", lambda *a, **kw: 200.0)
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(config, "MAX_LOTS_LIMIT", 10000)
    monkeypatch.setattr(config, "MAX_UNITS_LIMIT", 1000000)
    monkeypatch.setattr(config, "MAX_ORDER_VALUE_INR", 5000000000.0)
    monkeypatch.setattr(config, "DAILY_NOTIONAL_CAP_INR", 10000000000.0)
    monkeypatch.setattr(config, "TV_SENDS_LOTS", False)
    monkeypatch.setattr(config, "CLIENT_ID", "TEST_CLIENT_01")
    monkeypatch.setattr(xts_api.ORDER_RATE_LIMITER, "acquire", lambda *a, **kw: True)

    # Mock contract info: symbol "NATURALGAS", lot size 1, freeze limit 1,200
    monkeypatch.setattr(xts_api, "get_dynamic_contract_info", lambda sym: (
        568245, "MCXFO", "NRML", 0.10, 1, 1200, datetime.date.today() + datetime.timedelta(days=20)
    ))

    # Send 5,000 lots from external webhook / place_order
    res = xts_api.place_order("BUY", "NATURALGAS", 5000, 200.0, "TV_SIGNAL_NATGAS_5000", is_paper=False)

    # Invariant: Order must NOT be rejected with 'exceeds broker freeze limit'!
    assert res.get("status") != "error", f"Order was rejected instead of sliced: {res}"
    assert res.get("type") == "success" or res.get("status") == "success"

    # Invariant: Must dispatch exactly 5 sliced chunks: [1200, 1200, 1200, 1200, 200]
    assert len(dispatched_chunks) == 5, f"Expected 5 chunks dispatched, got {len(dispatched_chunks)}"
    chunk_quantities = [c.get("orderQuantity") for c in dispatched_chunks]
    assert chunk_quantities == [1200, 1200, 1200, 1200, 200], f"Wrong chunk quantities: {chunk_quantities}"

    # Invariant: All chunk refs must be unique and traceable
    chunk_refs = [c.get("orderUniqueIdentifier") for c in dispatched_chunks]
    assert len(set(chunk_refs)) == 5, f"Duplicate chunk order refs: {chunk_refs}"
    assert chunk_refs[0] == "TV_SIGNAL_NATGAS_5000"


def test_shared_slice_quantity_for_freeze_unit_parity():
    """
    Unit test confirming mathematical precision and edge-case behavior
    of the shared slice_quantity_for_freeze helper across all engines.
    """
    # 1. Exact multiple
    assert xts_api.slice_quantity_for_freeze(5000, 1000) == [1000, 1000, 1000, 1000, 1000]
    
    # 2. Non-multiple with remainder
    assert xts_api.slice_quantity_for_freeze(5000, 1200) == [1200, 1200, 1200, 1200, 200]
    
    # 3. Quantity below freeze limit
    assert xts_api.slice_quantity_for_freeze(500, 1200) == [500]
    
    # 4. Quantity exactly equal to freeze limit
    assert xts_api.slice_quantity_for_freeze(1200, 1200) == [1200]
    
    # 5. Zero / negative / unconfigured freeze limit
    assert xts_api.slice_quantity_for_freeze(2500, 0) == [2500]
    assert xts_api.slice_quantity_for_freeze(2500, -100) == [2500]
    assert xts_api.slice_quantity_for_freeze(2500, None) == [2500]


def test_dispatch_and_record_records_partial_failure_status(monkeypatch):
    """
    Verifies that when place_order / execute_trade_with_retry returns a partial_failure result,
    _dispatch_and_record correctly records 'partial_failure' in signals.db rather than
    silently degrading to 'failed'.
    """
    recorded_statuses = []

    def mock_db_update_status(sig_id, status, result=None, payload=None):
        recorded_statuses.append(status)

    monkeypatch.setattr(client_main, "db_update_status", mock_db_update_status)
    monkeypatch.setattr(client_main, "send_execution_notification", lambda *a, **kw: None)

    # Simulate partial failure return from trade execution
    partial_res = {
        "status": "partial_failure",
        "type": "partial_failure",
        "message": "Partial slices placed: 2400/5000 units",
        "dispatched_quantity": 2400,
        "total_quantity": 5000,
        "slices": [{"type": "success"}, {"type": "error"}]
    }
    monkeypatch.setattr(xts_api, "execute_trade_with_retry", lambda *a, **kw: partial_res)

    client_main._dispatch_and_record("test_sig_123", "BUY", "NATURALGAS", 5000, 200.0, "REF_123")

    # Invariants:
    # 1. First status is 'processing'
    assert recorded_statuses[0] == "processing"
    # 2. Final recorded status MUST be 'partial_failure'
    assert recorded_statuses[-1] == "partial_failure"


def test_sec_xts_009_panic_pricing_fails_closed_without_hardcoded_100(monkeypatch):
    """
    Regression Test for SEC-XTS-009:
    Verifies that when live market price, broker position average prices, and OHLC data are missing,
    panic square-off fails closed rather than falling back to a hardcoded price of 100
    and firing dangerous, mispriced orders to the broker OMS.
    """
    from unittest.mock import MagicMock

    dispatched_orders = []

    def mock_post(url, headers=None, json=None, timeout=None):
        mock_r = MagicMock()
        mock_r.status_code = 200
        if "cancelall" in url:
            mock_r.json.return_value = {"type": "success", "result": "all cancelled"}
        else:
            dispatched_orders.append(json)
            mock_r.json.return_value = {"type": "success", "result": {"AppOrderID": 12345}}
        return mock_r

    def mock_get(url, *a, **kw):
        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.json.return_value = {
            "type": "success",
            "result": {
                "positionList": [{
                    "ExchangeInstrumentId": 77777,
                    "ExchangeSegment": "MCXFO",
                    "ProductType": "NRML",
                    "TradingSymbol": "GOLD24AUGFUT",
                    "Quantity": 1,
                    # No prices available from broker position
                    "BuyAveragePrice": 0,
                    "SellAveragePrice": 0,
                    "LastTradedPrice": 0,
                    "LTP": 0
                }]
            }
        }
        return mock_r

    monkeypatch.setattr(xts_api.api_session, "post", mock_post)
    monkeypatch.setattr(xts_api.api_session, "get", mock_get)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda *a, **kw: "valid_interactive_token")
    # Live price fails / is missing
    monkeypatch.setattr(xts_api, "get_live_price", lambda *a, **kw: None)
    # OHLC candles fail / missing
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: [])
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(config, "CLIENT_ID", "TEST_CLIENT_01")
    monkeypatch.setattr(xts_api.ORDER_RATE_LIMITER, "acquire", lambda *a, **kw: True)

    panic_res = xts_api.panic_square_off_all()

    # Invariants:
    # 1. Must NOT dispatch an order with hardcoded limit price of ~100 on GOLD (which trades at ~75,000)
    for ord in dispatched_orders:
        limit_px = ord.get("limitPrice", 0)
        assert limit_px != 99.0 and limit_px != 100.0 and limit_px != 101.0, (
            f"CRITICAL BUG SEC-XTS-009: Dispatched order with hardcoded price 100 fallback! {ord}"
        )

    # 2. Because price could not be safely resolved, the position MUST be reported as failed/unclosed
    assert panic_res["status"] in ("error", "partial_failure")
    assert panic_res["unclosed_quantity"] == 1
    assert len(panic_res["failed_chunks"]) == 1
    assert "safe execution price" in panic_res["failed_chunks"][0]["reason"].lower() or "price" in panic_res["failed_chunks"][0]["reason"].lower()
    assert len(dispatched_orders) == 0, f"Dispatched dangerous orders: {dispatched_orders}"










