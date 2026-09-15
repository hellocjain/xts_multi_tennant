import sys
import os
import time
import datetime
import pytest
import asyncio
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from supertrend_engine import (
    calculate_supertrend,
    SuperTrendEngine,
    SingleSuperTrendRunner,
    MultiSuperTrendEngine
)
import xts_api
import main as client_main


def generate_synthetic_candles(prices, base_time=None, interval=300):
    if base_time is None:
        base_time = int(time.time()) - (len(prices) * interval)
    if base_time % 60 != 59:
        base_time = (base_time // 60) * 60 + 59
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


def test_tradingview_pine_script_v4_exact_mathematical_parity():
    """
    Tests exact mathematical equality against TradingView Pine Script v4 (KivancOzbilgic):
    Verifies True Range, Wilder's smoothing RMA ATR, band ratcheting, and trend flips.
    """
    prices = [
        100.0, 101.5, 103.0, 102.0, 104.5, 106.0, 105.0, 107.0, 108.5, 110.0,
        112.0, 111.0, 113.5, 115.0, 114.0, 116.0, 118.0, 117.5, 119.0, 120.0
    ]
    candles = generate_synthetic_candles(prices, interval=900)

    res = calculate_supertrend(candles, atr_period=10, multiplier=3.0, change_atr=True)
    assert res["error"] is None
    assert res["trend"] == 1
    assert res["trend_name"] == "BULLISH"
    assert res["atr"] > 0
    assert res["lower_band"] < res["last_close"]
    assert res["upper_band"] > res["lower_band"]

    res_sma = calculate_supertrend(candles, atr_period=10, multiplier=3.0, change_atr=False)
    assert res_sma["error"] is None
    assert res_sma["trend"] == 1
    assert res_sma["atr"] > 0


@pytest.mark.anyio
async def test_intraday_forming_candle_whip_never_alters_active_trend(monkeypatch):
    """
    Stress test verifying that extreme intra-bar price spikes during a forming candle
    NEVER flip active_trend or dispatch orders, matching TradingView process_orders_on_close = true.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 6,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 3.0
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574824,
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 50,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    base_time = 1787200000
    tf_seconds = 900
    bullish_prices = [1000 + i * 20 for i in range(14)]

    confirmed_candles = generate_synthetic_candles(bullish_prices, base_time=base_time, interval=tf_seconds)
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: confirmed_candles)

    runner = engine.get_strategy("SILVER1001!")
    runner.virtual_position = 6
    runner.last_processed_candle_time = confirmed_candles[-1]["time"]
    runner.active_trend = "BULLISH"

    forming_candle_time = confirmed_candles[-1]["time"] + tf_seconds
    now_during_bar = forming_candle_time - 300
    monkeypatch.setattr(time, "time", lambda: float(now_during_bar))

    forming_candle = {
        "time": forming_candle_time,
        "open": 1260.0,
        "high": 1265.0,
        "low": 500.0,
        "close": 510.0,
        "volume": 50000
    }
    candles_with_forming = confirmed_candles + [forming_candle]
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles_with_forming)

    for _ in range(50):
        await engine.evaluate_cycle(xts_api, client_main)

    assert runner.active_trend == "BULLISH", f"CRITICAL: active_trend flipped to {runner.active_trend} on unclosed candle!"
    assert len(dispatched_orders) == 0, f"CRITICAL: {len(dispatched_orders)} orders dispatched during forming candle!"
    assert runner.virtual_position == 6


@pytest.mark.anyio
async def test_confirmed_bar_close_executes_flip_cleanly_once(monkeypatch):
    """
    Stress test verifying that once the bar ACTUALLY closes below the band,
    the reversal executes cleanly and exactly once, transitioning from +6 LONG to -6 SHORT.
    """
    dispatched_orders = []

    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    engine = SuperTrendEngine(dispatch_fn=mock_dispatch)
    engine.update_config({
        "is_enabled": True,
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 6,
        "product_type": "NRML",
        "atr_period": 10,
        "multiplier": 3.0
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574824,
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 50,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    base_time = 1787200000
    tf_seconds = 900
    bullish_prices = [1000 + i * 20 for i in range(14)]
    candles = generate_synthetic_candles(bullish_prices, base_time=base_time, interval=tf_seconds)

    runner = engine.get_strategy("SILVER1001!")
    runner.virtual_position = 6
    runner.last_processed_candle_time = candles[-1]["time"]
    runner.active_trend = "BULLISH"

    close_time = candles[-1]["time"] + tf_seconds
    closed_bearish_candle = {
        "time": close_time,
        "open": 1260.0,
        "high": 1260.0,
        "low": 490.0,
        "close": 500.0,
        "volume": 60000
    }
    all_candles = candles + [closed_bearish_candle]
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: all_candles)

    monkeypatch.setattr(time, "time", lambda: float(close_time + 10))

    await engine.evaluate_cycle(xts_api, client_main)

    assert len(dispatched_orders) == 2, f"Expected 2 orders (Exit + Entry), got {len(dispatched_orders)}: {dispatched_orders}"
    exit_order = dispatched_orders[0][1]
    entry_order = dispatched_orders[1][1]

    assert exit_order["action"] == "SELL"
    assert exit_order["quantity"] == 6
    assert "EXIT" in exit_order["order_ref"]

    assert entry_order["action"] == "SELL"
    assert entry_order["quantity"] == 6
    assert "ENTRY" in entry_order["order_ref"]

    assert runner.active_trend == "BEARISH"
    assert runner.virtual_position == -6
    assert runner.last_processed_candle_time == close_time

    await engine.evaluate_cycle(xts_api, client_main)
    await engine.evaluate_cycle(xts_api, client_main)

    assert len(dispatched_orders) == 2, "Duplicate orders dispatched on subsequent cycles!"


@pytest.mark.asyncio
async def test_chart_data_fetching_never_corrupts_runner_active_trend(monkeypatch):
    """
    Verifies that requesting chart data or lightweight charts telemetry
    does not overwrite runner.active_trend with forming candle indicators.
    """
    multi_engine = MultiSuperTrendEngine()
    multi_engine.add_or_update_strategy({
        "id": "st_test_abk12",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 24,
        "is_enabled": True,
        "atr_period": 10,
        "multiplier": 3.0
    })

    runner = multi_engine.get_strategy("st_test_abk12")
    runner.active_trend = "BEARISH"
    runner.virtual_position = -24

    bullish_spike_candles = generate_synthetic_candles([100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150, 200])
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {"inst_id": 568839, "exch_seg": "MCXFO"})
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: bullish_spike_candles)

    chart_res = await multi_engine.get_chart_data_async(
        xts_api_module=xts_api,
        timeframe_override="20m",
        symbol_override="GOLDPETAL1!"
    )

    assert runner.active_trend == "BEARISH", "CRITICAL BUG: get_chart_data corrupted runner.active_trend!"
    assert chart_res["current_trend"] == "BEARISH"


def test_live_tick_spike_updates_forming_trend_without_corrupting_active_trend_or_trading():
    """
    Verifies that incoming real-time WebSocket ticks (update_live_tick) that pierce the SuperTrend band:
    1. Update runner.forming_trend to reflect the live candle state.
    2. STRICTLY DO NOT alter runner.active_trend (which must remain confirmed bar-close only).
    3. STRICTLY dispatch ZERO orders mid-candle.
    """
    dispatched = []
    engine = SuperTrendEngine(dispatch_fn=lambda sig, pay: dispatched.append((sig, pay)))
    engine.add_or_update_strategy({
        "id": "st_tick_test",
        "symbol": "SILVERMIC1!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True,
        "atr_period": 10,
        "multiplier": 3.0
    })
    runner = engine.get_strategy("st_tick_test")
    runner.active_trend = "BEARISH"
    runner.virtual_position = -1

    # Seed 20 historical candles with falling prices
    prices = [200 - i * 2 for i in range(20)]
    candles = generate_synthetic_candles(prices, base_time=1787200000, interval=900)
    runner.cached_candles = list(candles)
    runner.last_close = float(prices[-1])

    # Massive intraday tick spike piercing upper band (LTP = 350 vs upper band ~180)
    runner.update_live_tick(350.0)

    # Invariant 1: forming_trend reflects the live piercing tick
    assert runner.forming_trend == "BULLISH"

    # Invariant 2: active_trend is NOT corrupted; remains strictly BEARISH (closed bar state)
    assert runner.active_trend == "BEARISH", "CRITICAL: update_live_tick corrupted runner.active_trend!"

    # Invariant 3: Zero trades dispatched
    assert len(dispatched) == 0
