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

from supertrend_engine import calculate_supertrend, SuperTrendEngine, SingleSuperTrendRunner, TIMEFRAME_SECONDS_MAP
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
    
    # Mock broker order book returning an OPEN order originated by SuperTrend
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [
        {"AppOrderID": "ORD_99182", "TradingSymbol": "CRUDEOIL 31AUG2026", "OrderUniqueIdentifier": "ST_REV_ENTRY_CRUDEOIL_1", "OrderStatus": "OPEN"}
    ])

    await engine.evaluate_cycle(xts_api, client_main)

    # Assert zero orders dispatched because in-flight strategy pending order is active
    assert len(dispatched_orders) == 0


@pytest.mark.anyio
async def test_sec_xts_010_pending_order_suppression_ignores_unrelated_orders_and_stale_timeout(monkeypatch):
    """
    Regression Test for SEC-XTS-010:
    1. Verifies that pending orders NOT belonging to SuperTrend (e.g. manual GTC orders)
       do NOT suppress strategy cycle evaluation.
    2. Verifies that a SuperTrend order stuck in OPEN beyond 60s is bypassed to prevent
       permanent strategy starvation.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "CRUDEOIL",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 1,
        "multiplier": 2.0
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})

    # Historical OHLC with bullish flip
    prices_downtrend = [130, 128, 126, 124, 122, 120, 118, 116, 114, 112, 110, 100, 50, 40, 30, 150]
    mock_candles = generate_synthetic_candles(prices_downtrend, base_time=1700000000, interval=300)
    last_candle_close_time = mock_candles[-1]["time"] + 300
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: mock_candles)

    # Case A: Unrelated manual pending order present on broker
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [
        {"AppOrderID": "MANUAL_991", "TradingSymbol": "CRUDEOIL 31AUG2026", "OrderUniqueIdentifier": "MANUAL_GTC_ORDER_1", "OrderStatus": "OPEN"}
    ])
    # Mock clock so candle is recognized as closed
    monkeypatch.setattr("time.time", lambda: last_candle_close_time + 10)

    runner = engine.primary_runner
    runner.active_trend = "BEARISH" # Force bullish flip on candle close

    await engine.evaluate_cycle(xts_api, client_main)

    # Invariant 1: Unrelated manual order did NOT block SuperTrend from placing entry trade
    assert len(dispatched_orders) == 1
    assert dispatched_orders[0][1]["action"] == "BUY"

    # Case B: SuperTrend pending order older than 60s should time out and bypass suppression
    dispatched_orders.clear()
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [
        {"AppOrderID": "ST_ORD_992", "TradingSymbol": "CRUDEOIL 31AUG2026", "OrderUniqueIdentifier": "ST_REV_ENTRY_CRUDEOIL_992", "OrderStatus": "OPEN"}
    ])
    runner.pending_order_first_seen["ST_ORD_992"] = (last_candle_close_time + 10) - 70 # 70 seconds old (stale)
    runner.strategy_position = "FLAT"
    runner.active_trend = "BEARISH"
    runner.last_processed_candle_time = 0

    await engine.evaluate_cycle(xts_api, client_main)

    # Invariant 2: Stale pending order was bypassed after 60s
    assert len(dispatched_orders) == 1

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


@pytest.mark.anyio
async def test_sec_xts_001_reversal_exit_lot_vs_unit_reconciliation(monkeypatch):
    """
    Regression Test for SEC-XTS-001:
    Verifies that when broker reports open position in raw exchange units (e.g. 1250 units for 1 lot of Natural Gas),
    a subsequent reversal exit dispatches the quantity in LOTS (1 lot), NOT in raw units (1250),
    preventing double multiplication by lot_size in xts_api.place_order.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "NATURALGAS1!",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 1,  # Configured as 1 Lot
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 3.0
    })

    # Mock contract resolution with lot_size = 1250
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 568245,
        "exch_seg": "MCXFO",
        "lot_size": 1250,
        "freeze_qty": 1200,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })

    # Broker reports 1 open lot in raw exchange units: 1250
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {
        "positions": [{"symbol": "NATURALGAS25SEP2026FUT", "instrument_id": 568245, "side": "SHORT", "quantity": 1250}],
        "all_positions": [{"symbol": "NATURALGAS25SEP2026FUT", "instrument_id": 568245, "side": "SHORT", "quantity": 1250}]
    })
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Synthetic candles producing Bullish Flip
    candles_bullish = generate_synthetic_candles([
        130, 128, 126, 124, 122, 120, 118, 116, 114, 112,
        110, 100, 50, 40, 30,
        150
    ])
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: candles_bullish)

    await engine.evaluate_cycle(xts_api, client_main)

    # We expect 2 orders: Leg 1 (Exit SHORT 1 Lot), Leg 2 (Entry BUY 1 Lot)
    assert len(dispatched_orders) == 2, f"Expected 2 orders, got {len(dispatched_orders)}: {dispatched_orders}"

    sig_exit, p_exit = dispatched_orders[0]
    assert p_exit["action"] == "BUY"
    # MUST BE 1 LOT, NOT 1250 UNITS!
    assert p_exit["quantity"] == 1, (
        f"CRITICAL BUG SEC-XTS-001 DETECTED: Exit quantity was dispatched as {p_exit['quantity']} "
        f"instead of 1 lot! (Passing {p_exit['quantity']} to place_order would multiply by lot_size 1250 "
        f"yielding {p_exit['quantity'] * 1250} units / {p_exit['quantity']} lots!)"
    )


@pytest.mark.anyio
async def test_sec_xts_001_defense_in_depth_guard():
    """
    Verifies that the defense-in-depth guard refuses to dispatch any entry or exit
    if the quantity exceeds the unreasonable multiple limit or is non-positive.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    runner = SingleSuperTrendRunner({
        "symbol": "CRUDEOIL1!",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 2,  # Configured: 2 lots. Limit: max(2*5, 50) = 50 lots
        "product_type": "NRML"
    }, dispatch_fn=mock_dispatch)

    # 1. Unreasonable exit quantity (e.g. 500 lots for a 2-lot strategy)
    await runner._execute_exit("LONG", 500, "TEST_EXIT", None)
    assert len(dispatched_orders) == 0, "Guard failed to block unreasonable exit quantity"

    # 2. Unreasonable entry quantity (e.g. 100 lots for a 2-lot strategy)
    await runner._execute_entry("BUY", 100, "TEST_ENTRY", None)
    assert len(dispatched_orders) == 0, "Guard failed to block unreasonable entry quantity"

    # 3. Valid quantity within limits (2 lots)
    await runner._execute_entry("BUY", 2, "TEST_ENTRY_VALID", None)
    assert len(dispatched_orders) == 1, "Valid order was blocked by guard"
    assert dispatched_orders[0][1]["quantity"] == 2


@pytest.mark.anyio
async def test_sec_xts_002_unclosed_bar_rejection(monkeypatch):
    """
    Regression Test for SEC-XTS-002 (Part 1 - Rejection):
    Verifies that the SuperTrend evaluation loop refuses to execute flips on unclosed, in-progress candles.
    When an in-progress candle exhibits an intra-bar flip, no order must be dispatched and
    last_processed_candle_time must not be advanced.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "CRUDEOIL1!",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",  # 300 seconds
        "quantity": 1,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 2.0
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "lot_size": 100,
        "freeze_qty": 10000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    base_time = 1787200000
    tf_seconds = 300

    # 15 historical bearish candles (candles 0..14). Candle 15 is at 1787204500 (close time).
    candle_prices = [
        130, 128, 126, 124, 122, 120, 118, 116, 114, 112,
        110, 100, 50, 40, 30,
        150  # Candle 15 price: 150 (would produce Bullish Flip)
    ]
    candles = generate_synthetic_candles(candle_prices, base_time=base_time, interval=tf_seconds)
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: candles)

    # Mock clock is at 1787204400 (100 seconds before Candle 15 closes at 1787204500)
    mock_now = 1787204400.0
    monkeypatch.setattr(time, "time", lambda: mock_now)

    await engine.evaluate_cycle(xts_api, client_main)

    # Invariant: NO order should be dispatched while the bar is still forming!
    assert len(dispatched_orders) == 0, (
        f"CRITICAL BUG SEC-XTS-002 DETECTED: Order dispatched on in-progress unclosed candle at {mock_now}! "
        f"Orders: {dispatched_orders}"
    )

    runner = engine.get_strategy("CRUDEOIL1!")
    assert runner.last_processed_candle_time != 1787204500, (
        "CRITICAL BUG SEC-XTS-002: last_processed_candle_time was prematurely advanced for an unclosed bar!"
    )


@pytest.mark.anyio
async def test_sec_xts_002_confirmed_closed_bar_execution(monkeypatch):
    """
    Regression Test for SEC-XTS-002 (Part 2 - Execution):
    Verifies that once the clock advances past the candle close timestamp,
    the flip condition DOES execute immediately on the very next cycle, exactly once,
    and advances last_processed_candle_time to prevent duplicate executions.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "CRUDEOIL1!",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",  # 300 seconds
        "quantity": 1,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 2.0
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823,
        "exch_seg": "MCXFO",
        "lot_size": 100,
        "freeze_qty": 10000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    base_time = 1787200000
    tf_seconds = 300

    candle_prices = [
        130, 128, 126, 124, 122, 120, 118, 116, 114, 112,
        110, 100, 50, 40, 30,
        150  # Candle 15 price: 150 (produces Bullish Flip)
    ]
    candles = generate_synthetic_candles(candle_prices, base_time=base_time, interval=tf_seconds)
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: candles)

    # 1. Mock clock is past close timestamp (1787204505 >= 1787204500)
    mock_now = 1787204505.0
    monkeypatch.setattr(time, "time", lambda: mock_now)

    await engine.evaluate_cycle(xts_api, client_main)

    # Invariant: Order MUST fire on the very next cycle after candle close
    assert len(dispatched_orders) == 1, (
        f"Expected 1 order dispatched on confirmed candle close, got {len(dispatched_orders)}"
    )
    sig_id, payload = dispatched_orders[0]
    assert payload["action"] == "BUY"
    assert payload["quantity"] == 1
    assert payload["symbol"] == "CRUDEOIL1!"

    runner = engine.get_strategy("CRUDEOIL1!")
    assert runner.last_processed_candle_time == 1787204500

    # 2. Subsequent cycle with same closed candle (e.g. 5 seconds later)
    mock_now = 1787204510.0
    monkeypatch.setattr(time, "time", lambda: mock_now)

    await engine.evaluate_cycle(xts_api, client_main)
    assert len(dispatched_orders) == 1, "Duplicate order dispatched on already-processed closed bar!"


def test_sec_xts_007_strategy_id_isolation_and_symbol_migration():
    """
    Regression Test for SEC-XTS-007:
    1. Verifies that strategies are isolated by unique ID, allowing multiple strategies
       on the same underlying symbol with different timeframes/parameters without collision.
    2. Verifies that modifying a strategy's underlying symbol updates the existing runner in-place
       and does not leave an orphaned runner running in memory.
    3. Verifies that multi-strategy sync cleans up deleted runners.
    """
    engine = SuperTrendEngine()

    # 1. Add two strategies on the SAME symbol (CRUDEOIL) with different timeframes
    s1 = engine.add_or_update_strategy({
        "id": "strat_crude_5m",
        "symbol": "CRUDEOIL",
        "timeframe": "5m",
        "quantity": 1,
        "is_enabled": True
    })
    s2 = engine.add_or_update_strategy({
        "id": "strat_crude_15m",
        "symbol": "CRUDEOIL",
        "timeframe": "15m",
        "quantity": 2,
        "is_enabled": True
    })

    # Invariant 1: Both strategies coexist simultaneously without overwriting each other
    assert len(engine.strategies) == 2
    r1 = engine.get_strategy("strat_crude_5m")
    r2 = engine.get_strategy("strat_crude_15m")
    assert r1 is not None and r2 is not None
    assert r1.timeframe == "5m" and r1.quantity == 1
    assert r2.timeframe == "15m" and r2.quantity == 2

    # 2. Update strat_crude_5m symbol to NATURALGAS
    s1_updated = engine.add_or_update_strategy({
        "id": "strat_crude_5m",
        "symbol": "NATURALGAS",
        "timeframe": "5m",
        "quantity": 1,
        "is_enabled": True
    })

    # Invariant 2: Total strategy count remains 2 (no orphaned runner for old symbol)
    assert len(engine.strategies) == 2
    assert engine.get_strategy("strat_crude_5m").symbol == "NATURALGAS"

    # 3. Synchronize full strategies list with only 1 new strategy
    engine.update_config({
        "strategies": [
            {
                "id": "strat_gold_5m",
                "symbol": "GOLD",
                "timeframe": "5m",
                "quantity": 1,
                "is_enabled": True
            }
        ]
    })

    # Invariant 3: Previous runners were cleanly removed and deactivated
    assert len(engine.strategies) == 1
    assert engine.get_strategy("strat_gold_5m") is not None
    assert engine.get_strategy("strat_crude_5m") is None
    assert engine.get_strategy("strat_crude_15m") is None

@pytest.mark.anyio
async def test_multi_timeframe_same_symbol_execution_and_order_refs(monkeypatch):
    """
    Verifies that when multiple strategies run on the SAME symbol with different timeframes
    (e.g., SILVER1001! on 15m and SILVER1001! on 30m):
    1. Both runners execute their confirmed flips independently with distinct lot sizes.
    2. Order references format: ST_REV_ENTRY_{SYMBOL}_{TIMEFRAME}_{TS} prevents order ID collisions.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)

    # 1. Register 15m (Qty 1) and 30m (Qty 2) runners for SILVER1001!
    engine.add_or_update_strategy({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 1,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 2.0,
        "is_enabled": True
    })
    engine.add_or_update_strategy({
        "id": "st_silver_30m",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "30m",
        "quantity": 2,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 2.0,
        "is_enabled": True
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 999123,
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 10000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Synthetic candles for 15m and 30m
    prices_bullish = [130, 128, 126, 124, 122, 120, 118, 116, 114, 112, 110, 100, 50, 40, 30, 150]
    candles_15m = generate_synthetic_candles(prices_bullish, base_time=1787200000, interval=900)
    candles_30m = generate_synthetic_candles(prices_bullish, base_time=1787200000, interval=1800)

    def mock_fetch_ohlc(seg, inst_id, tf_seconds, bars):
        if tf_seconds == 900:
            return candles_15m
        elif tf_seconds == 1800:
            return candles_30m
        return candles_15m

    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", mock_fetch_ohlc)
    monkeypatch.setattr(time, "time", lambda: 1787250000.0) # Past candle close timestamps

    await engine.evaluate_cycle(xts_api, client_main)

    # Invariant: Both strategies dispatched orders
    assert len(dispatched_orders) == 2, f"Expected 2 dispatched orders, got {len(dispatched_orders)}"

    sig_1, p_1 = next((s, p) for s, p in dispatched_orders if p["quantity"] == 1)
    sig_2, p_2 = next((s, p) for s, p in dispatched_orders if p["quantity"] == 2)

    assert p_1["symbol"] == "SILVER1001!"
    assert p_1["action"] == "BUY"
    assert "15M" in p_1["order_ref"].upper()

    assert p_2["symbol"] == "SILVER1001!"
    assert p_2["action"] == "BUY"
    assert "30M" in p_2["order_ref"].upper()

@pytest.mark.anyio
async def test_multi_timeframe_pending_suppression_isolation(monkeypatch):
    """
    Verifies that an in-flight order on the 15m timeframe does NOT suppress
    the 30m runner on the same underlying symbol.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)

    engine.add_or_update_strategy({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 1,
        "multiplier": 2.0,
        "is_enabled": True
    })
    engine.add_or_update_strategy({
        "id": "st_silver_30m",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "30m",
        "quantity": 2,
        "multiplier": 2.0,
        "is_enabled": True
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 999123,
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 10000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})

    # Mock broker order book returning an OPEN order specifically for 15M runner
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [
        {
            "AppOrderID": "ORD_15M_99",
            "TradingSymbol": "SILVER1001! 31AUG2026",
            "OrderUniqueIdentifier": "ST_REV_ENTRY_SILVER1001!_15M_1787200000",
            "OrderStatus": "OPEN"
        }
    ])

    prices_bullish = [130, 128, 126, 124, 122, 120, 118, 116, 114, 112, 110, 100, 50, 40, 30, 150]
    candles = generate_synthetic_candles(prices_bullish, base_time=1787200000, interval=1800)
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
    monkeypatch.setattr(time, "time", lambda: 1787250000.0)

    await engine.evaluate_cycle(xts_api, client_main)

    # 15M was suppressed due to in-flight pending order, but 30M must execute successfully!
    assert len(dispatched_orders) == 1
    sig, payload = dispatched_orders[0]
    assert payload["quantity"] == 2
    assert "30M" in payload["order_ref"].upper()

def test_pine_script_v4_kivancozbilgic_mathematical_parity():
    """
    Direct validation of calculate_supertrend against KivancOzbilgic Pine Script v4 formulas:
    - tr = max(high-low, abs(high-close[1]), abs(low-close[1]))
    - atr = rma(tr, Periods) (Wilder's Smoothing)
    - up = src - Multiplier*atr; up := close[1] > up1 ? max(up, up1) : up
    - dn = src + Multiplier*atr; dn := close[1] < dn1 ? min(dn, dn1) : dn
    - trend := trend[1] == -1 and close > dn1 ? 1 : trend[1] == 1 and close < up1 ? -1 : trend[1]
    """
    # 20 controlled bars with known price actions:
    # Bars 0-9: Flat range for RMA initialization (seed = SMA of first 10 TRs)
    # Bars 10-14: Rising prices establishing Bullish trend (trend = 1)
    # Bars 15-16: Sharp collapse breaking lower band (trend = -1, sellSignal)
    # Bars 17-19: Sharp surge breaking upper band (trend = 1, buySignal)
    candles = [
        {"time": 1000 + i*60, "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 1000}
        for i in range(10)
    ]
    # TR for each of first 10 bars = 102 - 98 = 4.0. Initial ATR(10) at bar 9 = 4.0.
    
    # Bar 10: High 110, Low 100, Close 108. hl2 = 105.0. TR = max(110-100, |110-100|, |100-100|) = 10.0.
    # ATR(10) = (4.0 * 9 + 10.0) / 10 = 4.6.
    # basic_lb = 105.0 - (3.0 * 4.6) = 91.2. basic_ub = 105.0 + 13.8 = 118.8.
    candles.append({"time": 1600, "open": 100.0, "high": 110.0, "low": 100.0, "close": 108.0, "volume": 1000})

    # Bar 11: High 120, Low 107, Close 118. hl2 = 113.5. TR = max(13, |120-108|, |107-108|) = 13.0.
    # ATR(10) = (4.6 * 9 + 13.0) / 10 = 5.44.
    # basic_lb = 113.5 - (3.0 * 5.44) = 97.18.
    # Since basic_lb (97.18) > prev_lb (91.2) and prev_close (108.0) >= 91.2 -> final_lb = 97.18 (Ratcheted up!)
    candles.append({"time": 1660, "open": 108.0, "high": 120.0, "low": 107.0, "close": 118.0, "volume": 1000})

    # Bar 12: Whipsaw Bar: Price drops to 90 mid-candle (piercing lower band 97.18) but closes at 115 (above lower band).
    # Pine Script rule: Trend is evaluated on CLOSE. Since close (115) > prev_lb (97.18), trend remains +1 (BULLISH)!
    candles.append({"time": 1720, "open": 118.0, "high": 119.0, "low": 90.0, "close": 115.0, "volume": 1000})

    # Bar 13: Collapse Bar: Close breaks below lower band (Close = 80.0)
    # Flips trend to -1 (BEARISH)
    candles.append({"time": 1780, "open": 115.0, "high": 115.0, "low": 75.0, "close": 80.0, "volume": 1000})

    # Bar 14: Rebound Bar: Close surges above upper band (Close = 135.0)
    # Flips trend to +1 (BULLISH)
    candles.append({"time": 1840, "open": 80.0, "high": 140.0, "low": 80.0, "close": 135.0, "volume": 1000})

    res = calculate_supertrend(candles, atr_period=10, multiplier=3.0, change_atr=True)
    assert res["error"] is None
    assert len(res["candle_series"]) == 15

    # Check Bar 9 (initialization bar)
    bar9 = res["candle_series"][9]
    assert bar9["atr"] == 4.0
    assert bar9["trend"] == 1

    # Check Bar 11 (ratchet verification)
    bar11 = res["candle_series"][11]
    assert bar11["lower_band"] > bar9["lower_band"] # Lower band ratcheted upward
    assert bar11["trend"] == 1

    # Check Bar 12 (whipsaw bar)
    bar12 = res["candle_series"][12]
    assert bar12["trend"] == 1 # Maintained Bullish because close > prev_lb

    # Check Bar 13 (bearish flip bar)
    bar13 = res["candle_series"][13]
    assert bar13["trend"] == -1 # Flipped to Bearish

    # Check Bar 14 (bullish flip bar)
    bar14 = res["candle_series"][14]
    assert bar14["trend"] == 1 # Flipped to Bullish
    assert res["is_flip"] is True
    assert res["flip_direction"] == "BULLISH"

def test_pine_script_v4_sma_atr_mode_parity():
    """
    Validates calculate_supertrend with change_atr=False (sma(tr, Periods) mode in Pine Script).
    """
    candles = [
        {"time": 1000 + i*60, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0, "volume": 1000}
        for i in range(15)
    ]
    # Each bar TR = 105 - 95 = 10.0. SMA(TR, 10) = 10.0 everywhere.
    res_sma = calculate_supertrend(candles, atr_period=10, multiplier=3.0, change_atr=False)
    assert res_sma["error"] is None
    for c in res_sma["candle_series"][9:]:
        assert c["atr"] == 10.0

@pytest.mark.asyncio
async def test_multi_timeframe_same_symbol_markers_isolation(monkeypatch):
    """
    Validates that when a 15m runner executes a trade flip, trade markers are appended ONLY
    to that specific runner's recent_trade_markers, and NOT to the 30m runner's recent_trade_markers.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)

    engine.add_or_update_strategy({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 2,
        "is_enabled": True
    })
    engine.add_or_update_strategy({
        "id": "st_silver_30m",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "30m",
        "quantity": 2,
        "is_enabled": True
    })

    runner_15m = engine.get_strategy("st_silver_15m")
    runner_30m = engine.get_strategy("st_silver_30m")

    # Set 15m runner in LONG position, then trigger exit and entry
    runner_15m.strategy_position = "LONG"
    await runner_15m._execute_exit("LONG", 2, "FLIP_EXIT_1001", None, 10000)
    await runner_15m._execute_entry("SELL", 2, "FLIP_ENTRY_1001", None, 10000)

    # 15m runner must have 2 markers
    assert len(runner_15m.recent_trade_markers) == 2
    assert runner_15m.recent_trade_markers[0]["text"] == "EXIT LONG (2)"
    assert runner_15m.recent_trade_markers[1]["text"] == "SELL 2"

    # 30m runner must have 0 markers (clean isolation)
    assert len(runner_30m.recent_trade_markers) == 0









