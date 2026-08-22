import os
import sys
import tempfile
import pytest
import datetime
import time
import asyncio
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if client_dir not in sys.path:
    sys.path.insert(0, client_dir)

from supertrend_engine import calculate_supertrend, SuperTrendEngine, TIMEFRAME_SECONDS_MAP
from main import app, supertrend_engine
import xts_api
import main as client_main

def generate_synthetic_candles(prices, base_time=1787200000, interval=300):
    candles = []
    for i, p in enumerate(prices):
        t = base_time + (i * interval)
        candles.append({
            "time": t,
            "open": float(p) - 2.0,
            "high": float(p) + 5.0,
            "low": float(p) - 5.0,
            "close": float(p),
            "volume": 1000 + i * 50
        })
    return candles

def test_supertrend_mathematical_calculation():
    # 15 candles of steady uptrend
    prices = [100.0, 102.0, 105.0, 107.0, 110.0, 112.0, 115.0, 118.0, 120.0, 122.0, 125.0, 128.0, 130.0, 133.0, 135.0]
    candles = generate_synthetic_candles(prices)

    res = calculate_supertrend(candles, atr_period=10, multiplier=3.0)
    assert res["error"] is None
    assert res["trend"] == 1
    assert res["trend_name"] == "BULLISH"
    assert res["atr"] > 0
    assert res["lower_band"] > 0
    assert res["upper_band"] > res["lower_band"]
    assert res["last_close"] == 135.0

def test_supertrend_bullish_and_bearish_flips():
    # 1. Start with uptrend, then sharp drop on the last candle
    prices = [
        100, 102, 104, 106, 108, 110, 112, 114, 116, 118,
        120, 122, 125,
        50 # Drop on last candle
    ]
    candles = generate_synthetic_candles(prices)
    res = calculate_supertrend(candles, atr_period=10, multiplier=2.0)
    
    assert res["trend"] == -1
    assert res["trend_name"] == "BEARISH"
    assert res["is_flip"] is True
    assert res["flip_direction"] == "BEARISH"

    # 2. Start with downtrend, then sharp rally on the last candle
    prices_downtrend = [
        130, 128, 126, 124, 122, 120, 118, 116, 114, 112,
        110, 100, 50, 40, 30,
        150 # Rally on last candle
    ]
    candles_rally = generate_synthetic_candles(prices_downtrend)
    res_rally = calculate_supertrend(candles_rally, atr_period=10, multiplier=2.0)
    
    assert res_rally["trend"] == 1
    assert res_rally["trend_name"] == "BULLISH"
    assert res_rally["is_flip"] is True
    assert res_rally["flip_direction"] == "BULLISH"

@pytest.mark.anyio
async def test_supertrend_sequential_reversal_execution(monkeypatch):
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "CRUDEOIL",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 2,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 2.0
    })

    # Mock master cache
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })

    # 1. Case A: Currently holding SHORT 2 lots, Bullish Flip detected
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {
        "positions": [{"symbol": "CRUDEOIL 31AUG2026", "instrument_id": 574823, "side": "SHORT", "quantity": 2}],
        "all_positions": [{"symbol": "CRUDEOIL 31AUG2026", "instrument_id": 574823, "side": "SHORT", "quantity": 2}]
    })
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Synthetic candles producing Bullish Flip on the last candle
    candles_bullish = generate_synthetic_candles([
        130, 128, 126, 124, 122, 120, 118, 116, 114, 112,
        110, 100, 50, 40, 30,
        150
    ])
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: candles_bullish)

    await engine.evaluate_cycle(xts_api, client_main)

    # Assert Sequential Reversal Dispatched: Leg 1 (Exit SHORT -> BUY 2), Leg 2 (Entry LONG -> BUY 2)
    assert len(dispatched_orders) == 2
    
    # Leg 1: Exit
    sig_exit, p_exit = dispatched_orders[0]
    assert p_exit["action"] == "BUY"
    assert p_exit["quantity"] == 2
    assert p_exit["source"] == "supertrend_engine"
    assert "ST_REV_EXIT_" in p_exit["order_ref"]

    # Leg 2: Entry
    sig_entry, p_entry = dispatched_orders[1]
    assert p_entry["action"] == "BUY"
    assert p_entry["quantity"] == 2
    assert p_entry["source"] == "supertrend_engine"
    assert "ST_REV_ENTRY_" in p_entry["order_ref"]

@pytest.mark.anyio
async def test_supertrend_expiry_safety_pause(monkeypatch):
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "CRUDEOIL",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 1
    })
    engine.strategy_position = "LONG"
    engine.current_broker_quantity = 1

    # Mock expiring contract (1 day to expiry on MCXFO <= 3 days cutoff)
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "expiry": datetime.date.today() + datetime.timedelta(days=1)
    })

    await engine.evaluate_cycle(xts_api, client_main)

    # Assert market square-off dispatched and strategy paused
    assert len(dispatched_orders) == 1
    assert dispatched_orders[0][1]["action"] == "SELL" # Exit LONG
    assert engine.is_enabled is False
    assert engine.status == "EXPIRED_PAUSED"

@pytest.mark.anyio
async def test_supertrend_pending_order_suppression(monkeypatch):
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "CRUDEOIL",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 1
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    
    # Mock broker order book returning an OPEN order for CRUDEOIL
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [
        {"AppOrderID": "ORD_99182", "TradingSymbol": "CRUDEOIL 31AUG2026", "OrderStatus": "OPEN"}
    ])

    await engine.evaluate_cycle(xts_api, client_main)

    # Assert zero orders dispatched because pending order is active
    assert len(dispatched_orders) == 0

def test_supertrend_internal_api_endpoints():
    client = TestClient(app)

    # 1. Update config
    cfg_payload = {
        "is_enabled": True,
        "symbol": "SILVER100",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 4,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 3.0
    }
    res_post = client.post("/internal/supertrend/config", json=cfg_payload)
    assert res_post.status_code == 200
    data = res_post.json()
    assert data["status"] == "success"
    assert data["telemetry"]["symbol"] == "SILVER100"
    assert data["telemetry"]["quantity"] == 4
    assert data["telemetry"]["is_enabled"] is True

    # 2. Query status
    res_get = client.get("/internal/supertrend/status")
    assert res_get.status_code == 200
    st_data = res_get.json()
    assert st_data["symbol"] == "SILVER100"
    assert st_data["timeframe"] == "5m"

    # 3. Consolidated telemetry endpoint includes supertrend
    res_tel = client.get("/internal/telemetry")
    assert res_tel.status_code == 200
    tel_json = res_tel.json()
    assert "supertrend" in tel_json
    assert tel_json["supertrend"]["symbol"] == "SILVER100"

def test_custom_timeframe_parsing():
    from supertrend_engine import parse_timeframe_seconds
    assert parse_timeframe_seconds("1m") == 60
    assert parse_timeframe_seconds("3m") == 180
    assert parse_timeframe_seconds("5m") == 300
    assert parse_timeframe_seconds("15m") == 900
    assert parse_timeframe_seconds("20m") == 1200
    assert parse_timeframe_seconds("25m") == 1500
    assert parse_timeframe_seconds("30m") == 1800
    assert parse_timeframe_seconds("45m") == 2700
    assert parse_timeframe_seconds("1h") == 3600
    assert parse_timeframe_seconds("2h") == 7200
    assert parse_timeframe_seconds(20) == 1200
    assert parse_timeframe_seconds("25") == 1500

def test_symbol_validation_and_market_readiness_endpoints(monkeypatch):
    client = TestClient(app)

    # 1. Valid TradingView format symbol
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "prod_type": "NRML",
        "lot_size": 1.0,
        "tick_size": 1,
        "freeze_qty": 6000,
        "expiry": datetime.date(2026, 8, 31),
        "expiry_str": "31-Aug-2026",
        "days_to_expiry": 20,
        "desc": "SILVER 31AUG2026",
        "name": "SILVER"
    })
    
    res_valid = client.get("/internal/validate-symbol?symbol=SILVER1001!")
    assert res_valid.status_code == 200
    d_valid = res_valid.json()
    assert d_valid["valid"] is True
    assert d_valid["inst_id"] == 574823
    assert d_valid["exch_seg"] == "MCXFO"
    assert d_valid["desc"] == "SILVER 31AUG2026"

    # 2. Invalid symbol
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: None)
    res_invalid = client.get("/internal/validate-symbol?symbol=INVALID_XYZ")
    assert res_invalid.status_code == 200
    d_invalid = res_invalid.json()
    assert d_invalid["valid"] is False

    # 3. Market readiness check
    monkeypatch.setattr(xts_api, "check_live_market_readiness", lambda sym: {
        "interactive_auth": {"status": "OK", "client_id": "ABK01"},
        "market_data_auth": {"status": "OK"},
        "master_cache": {"status": "OK", "total_contracts": 18898},
        "live_feed": {"status": "OK", "last_close": 2463.0},
        "market_hours": {"status": "OPEN"},
        "all_ready": True
    })
    res_readiness = client.get("/internal/market-readiness?symbol=SILVER1001!")
    assert res_readiness.status_code == 200
    d_read = res_readiness.json()
    assert d_read["all_ready"] is True
    assert d_read["interactive_auth"]["status"] == "OK"

def test_supertrend_chart_data_and_evaluate_now_endpoints(monkeypatch):
    client = TestClient(app)

    # 1. Test /internal/supertrend/candles
    supertrend_engine.cached_candles = [
        {"time": 1787200000, "open": 100.0, "high": 105.0, "low": 98.0, "close": 104.0, "supertrend": 95.0, "upper_band": 110.0, "lower_band": 95.0, "trend": 1, "atr": 3.5, "volume": 500}
    ]
    res_candles = client.get("/internal/supertrend/candles")
    assert res_candles.status_code == 200
    c_data = res_candles.json()
    assert "candlestick" in c_data
    assert "supertrend_line" in c_data
    assert len(c_data["candlestick"]) == 1

    # 2. Test /internal/supertrend/evaluate-now
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 1000,
        "expiry": datetime.date.today() + datetime.timedelta(days=25)
    })
    synthetic_feed = generate_synthetic_candles([100, 102, 105, 108, 110, 112, 115, 118, 120, 122, 125, 128, 130, 135])
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: synthetic_feed)

    res_eval = client.post("/internal/supertrend/evaluate-now")
    assert res_eval.status_code == 200
    eval_json = res_eval.json()
    assert eval_json["status"] == "OK"
    assert eval_json["trend"] == "BULLISH"
    assert eval_json["calculated_atr"] > 0

@pytest.mark.anyio
async def test_supertrend_freeze_quantity_auto_slicing(monkeypatch):
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "SILVER100",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 25,  # Order total 25 lots
        "product_type": "NRML",
        "execution_mode": "PAPER"
    })

    # Mock freeze quantity = 10 lots max per order slice
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 10,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Synthetic candles producing Bullish Flip on the last candle
    candles_bullish = generate_synthetic_candles([
        130, 128, 126, 124, 122, 120, 118, 116, 114, 112,
        110, 100, 50, 40, 30,
        150
    ])
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: candles_bullish)

    await engine.evaluate_cycle(xts_api, client_main)

    # Total quantity 25 with freeze cap 10 must be sliced into: 10, 10, 5 = 3 slices
    assert len(dispatched_orders) == 3
    assert dispatched_orders[0][1]["quantity"] == 10
    assert dispatched_orders[1][1]["quantity"] == 10
    assert dispatched_orders[2][1]["quantity"] == 5
    assert dispatched_orders[0][1]["is_paper"] is True

@pytest.mark.anyio
async def test_multi_supertrend_engine_cap_and_concurrent_execution(monkeypatch):
    from supertrend_engine import MultiSuperTrendEngine
    dispatched = []

    async def mock_dispatch(sig_id, payload):
        dispatched.append((sig_id, payload))

    engine = MultiSuperTrendEngine(dispatch_fn=mock_dispatch)

    # 1. Enforce max 6 strategies cap
    for i in range(6):
        sym = f"SYM{i+1}"
        engine.add_or_update_strategy({
            "symbol": sym,
            "exchange_segment": "MCXFO",
            "timeframe": "5m",
            "quantity": 1,
            "is_enabled": True
        })
    assert len(engine.strategies) == 6

    # 7th strategy must raise ValueError
    with pytest.raises(ValueError, match="limit of 6 symbols reached"):
        engine.add_or_update_strategy({
            "symbol": "SYM7",
            "exchange_segment": "MCXFO",
            "timeframe": "5m",
            "quantity": 1,
            "is_enabled": True
        })
    assert len(engine.strategies) == 6

    # 2. Test concurrent evaluation
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 12345,
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 1000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])
    synthetic = generate_synthetic_candles([100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120])
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: synthetic)

    await engine.evaluate_cycle(xts_api, client_main)
    # Check that runners evaluated cleanly without cross-symbol interference
    summary = engine.get_telemetry()
    assert summary["total_strategies"] == 6
    assert summary["active_strategies_count"] == 6

def test_multi_supertrend_client_crud_routes():
    client = TestClient(app)

    # 1. Save new strategy
    save_res = client.post("/internal/supertrend/strategy/save", json={
        "symbol": "GOLDPETAL1!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 8,
        "execution_mode": "PAPER",
        "is_enabled": True
    })
    assert save_res.status_code == 200
    assert save_res.json()["status"] == "success"

    # 2. List strategies
    list_res = client.get("/internal/supertrend/strategies")
    assert list_res.status_code == 200
    strats = list_res.json()["strategies"]
    assert any(s["symbol"] == "GOLDPETAL1!" for s in strats)

    # 3. Toggle strategy
    toggle_res = client.post("/internal/supertrend/strategy/GOLDPETAL1!/toggle")
    assert toggle_res.status_code == 200
    assert toggle_res.json()["strategy"]["is_enabled"] is False

    # 4. Delete strategy
    del_res = client.delete("/internal/supertrend/strategy/GOLDPETAL1!")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"


