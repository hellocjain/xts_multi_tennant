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
