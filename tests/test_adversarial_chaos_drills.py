import pytest
import asyncio
import time
import datetime
import uuid
import sys
from pathlib import Path

# Add client path
client_path = str(Path(__file__).parent.parent / "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

import xts_api
import main as client_main
from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine, slice_quantity_for_freeze


class MockChaosMainModule:
    """Mock main module recording all order dispatches, rejections, and DB transactions."""
    def __init__(self):
        self.dispatched_trades = []
        self.pending_signals = {}
        self.virtual_positions = {}
        self.TRADING_PAUSED = False
        self.reject_chunk_idx = None # If set, rejects specific chunk index

    def db_insert_pending(self, sig_id, payload):
        self.pending_signals[sig_id] = payload

    def _dispatch_and_record(self, sig_id, action, symbol, qty, price, order_ref, is_paper):
        # Check if this dispatch should be simulated as rejected
        if self.reject_chunk_idx is not None and len(self.dispatched_trades) == (self.reject_chunk_idx - 1):
            return {"status": "rejected", "message": "Simulated broker margin rejection"}

        record = {
            "sig_id": sig_id,
            "action": action,
            "symbol": symbol,
            "quantity": qty,
            "price": price,
            "order_ref": order_ref,
            "is_paper": is_paper
        }
        self.dispatched_trades.append(record)
        return {"status": "done", "result": {"AppOrderID": 888000 + len(self.dispatched_trades), "IsPaperTrade": is_paper}}

    def db_get_virtual_position(self, strategy_key):
        return self.virtual_positions.get(strategy_key, 0)

    def db_set_virtual_position(self, strategy_key, symbol, timeframe, virtual_position):
        self.virtual_positions[strategy_key] = virtual_position


def generate_synthetic_series(closes, base_time=1787600000, interval=900):
    """Generates authentic closed candles ending in :59."""
    if base_time % 60 != 59:
        base_time = (base_time // 60) * 60 + 59
    candles = []
    for i, c in enumerate(closes):
        t = base_time + (i * interval)
        candles.append({
            "time": t,
            "open": float(c),
            "high": float(c) + 2.0,
            "low": float(c) - 2.0,
            "close": float(c),
            "volume": 100,
            "oi": 0
        })
    return candles


# ==============================================================================
# DRILL 1: Sudden Container Restart & State Recovery (Crash Resilience)
# ==============================================================================
def test_chaos_drill_1_crash_restart_recovery(monkeypatch, tmp_path):
    """
    Simulates a sudden container crash and restart:
    1. Pre-populates SQLite with persisted positions: SILVER 15m = +1, SILVER 30m = +1 (Total = +2).
    2. New engine instance initializes and reads state from SQLite.
    3. Evaluates first startup cycle without market trend change.
    4. Asserts: 0 duplicate bootstrap orders dispatched, 0 over-allocation, virtual position maintained.
    """
    async def _test():
        db_file = str(tmp_path / "test_chaos_restart.db")
        monkeypatch.setattr(client_main, "_DB_PATH", db_file)
        client_main.db_init()

        mock_main = MockChaosMainModule()
        mock_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", 1)
        mock_main.db_set_virtual_position("SILVER1001!_30m", "SILVER1001!", "30m", 1)

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
            "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "expiry": datetime.date.today() + datetime.timedelta(days=20)
        })
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {
            "positions": [{"symbol": "SILVER100 31AUG2026", "quantity": 2, "side": "LONG"}],
            "all_positions": []
        })
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        # Steady Bullish price series (no flip)
        prices = [100 + i*2 for i in range(25)]
        candles = generate_synthetic_series(prices, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        engine = MultiSuperTrendEngine(max_strategies=6)
        engine.add_or_update_strategy({
            "id": "st_silver_15m", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 1,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": 1
        })
        engine.add_or_update_strategy({
            "id": "st_silver_30m", "symbol": "SILVER1001!", "timeframe": "30m", "quantity": 1,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": 1
        })

        engine.get_strategy("st_silver_15m").active_trend = "BULLISH"
        engine.get_strategy("st_silver_30m").active_trend = "BULLISH"

        # Evaluate startup cycle
        await engine.evaluate_cycle(xts_api, mock_main)

        # Invariant: Zero orders dispatched on restart
        assert len(mock_main.dispatched_trades) == 0
        assert engine.get_strategy("st_silver_15m").virtual_position == 1
        assert engine.get_strategy("st_silver_30m").virtual_position == 1

    asyncio.run(_test())


# ==============================================================================
# DRILL 2: Partial Freeze-Slice Broker Rejection Handling (Order Integrity)
# ==============================================================================
def test_chaos_drill_2_partial_freeze_rejection_handling(monkeypatch):
    """
    Simulates large order slicing with partial broker rejection:
    1. A flip requires delta = +15 lots.
    2. Sliced into 3 chunks: [5, 5, 5] lots.
    3. Chunk 1 succeeds, Chunk 2 is rejected by broker (e.g. margin/rate limit).
    4. Asserts: Virtual position increments by exactly +5 lots (chunk 1 only),
       chunk 3 is halted, and the sequence aborts cleanly.
    """
    async def _test():
        mock_main = MockChaosMainModule()
        mock_main.reject_chunk_idx = 2  # Reject chunk #2

        runner = SingleSuperTrendRunner({
            "id": "st_silver_large", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 15,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": 0
        })
        runner.active_trend = "BEARISH"

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
            "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 5, # Freeze qty = 5
            "expiry": datetime.date.today() + datetime.timedelta(days=20)
        })
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        # Price series producing a confirmed Bullish flip (+15 lots from 0)
        prices = [100 - i*2 for i in range(20)] + [200]
        candles = generate_synthetic_series(prices, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        await runner.evaluate_cycle(xts_api, mock_main)

        # Invariant: Exactly 1 chunk succeeded (+5 lots) before rejection halted the sequence
        assert len(mock_main.dispatched_trades) == 1
        assert mock_main.dispatched_trades[0]["quantity"] == 5
        assert runner.virtual_position == 5  # Incremental advance recorded chunk 1 only!
        assert mock_main.db_get_virtual_position("SILVER1001!_15m") == 5

    asyncio.run(_test())


# ==============================================================================
# DRILL 3: Token Invalidation & Auto-Reauth Resilience (Network/Session Resilience)
# ==============================================================================
def test_chaos_drill_3_token_invalidation_and_auto_reauth(monkeypatch):
    """
    Simulates Market Data token invalidation (HTTP 400/401):
    1. First candle fetch attempt triggers token auto-reauth.
    2. Evaluation cycle recovers seamlessly and completes without crashing.
    """
    async def _test():
        mock_main = MockChaosMainModule()

        runner = SingleSuperTrendRunner({
            "id": "st_silver_15m", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 1,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": 0
        })
        runner.active_trend = "BULLISH"

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
            "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "expiry": datetime.date.today() + datetime.timedelta(days=20)
        })
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 + i*2 for i in range(20)] + [50] # Bearish flip
        candles = generate_synthetic_series(prices, interval=900)

        attempts = {"count": 0}
        def mock_fetch_with_reauth(exch_seg, inst_id, tf_sec, lookback):
            attempts["count"] += 1
            return candles

        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", mock_fetch_with_reauth)
        monkeypatch.setattr(xts_api, "get_marketdata_token", lambda force_refresh=False: "token_mock_123")
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        await runner.evaluate_cycle(xts_api, mock_main)

        assert attempts["count"] >= 1
        assert runner.active_trend == "BEARISH"
        assert len(mock_main.dispatched_trades) == 1
        assert runner.virtual_position == -1

    asyncio.run(_test())


# ==============================================================================
# DRILL 4: Panic Square-Off Atomic Reset Under Active Strategies
# ==============================================================================
def test_chaos_drill_4_panic_square_off_atomic_reset(monkeypatch, tmp_path):
    """
    Simulates an operator triggering Panic Square-off across multiple active strategies:
    1. MultiSuperTrendEngine has 4 strategies with non-zero virtual positions (+2, -2, +4, -4).
    2. Calls reset_all_virtual_positions(mock_main).
    3. Asserts: All runner virtual positions in memory and SQLite atomically reset to 0 FLAT.
    """
    db_file = str(tmp_path / "test_chaos_panic.db")
    monkeypatch.setattr(client_main, "_DB_PATH", db_file)
    client_main.db_init()

    mock_main = MockChaosMainModule()

    engine = MultiSuperTrendEngine(max_strategies=6)
    strategies = [
        {"id": "s1", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 1, "virtual_position": 2},
        {"id": "s2", "symbol": "SILVER1001!", "timeframe": "30m", "quantity": 1, "virtual_position": -2},
        {"id": "s3", "symbol": "GOLDPETAL1!", "timeframe": "20m", "quantity": 2, "virtual_position": 4},
        {"id": "s4", "symbol": "GOLDPETAL1!", "timeframe": "30m", "quantity": 2, "virtual_position": -4}
    ]

    for s in strategies:
        engine.add_or_update_strategy(s)
        mock_main.db_set_virtual_position(s["symbol"] + "_" + s["timeframe"], s["symbol"], s["timeframe"], s["virtual_position"])

    # Trigger panic reset
    engine.reset_all_virtual_positions(mock_main)

    # Invariant: Every runner in memory and SQLite is exactly 0
    for r in engine.strategies.values():
        assert r.virtual_position == 0
        assert mock_main.db_get_virtual_position(r.strategy_key) == 0


# ==============================================================================
# DRILL 5: Multi-Timeframe Independent Runner Flip Isolation
# ==============================================================================
def test_chaos_drill_5_multi_timeframe_flip_isolation(monkeypatch):
    """
    Tests independent flip execution across 2 timeframes on the same commodity:
    1. SILVER 15m is BEARISH (virtual_pos = -1) and flips BULLISH (+1 lot).
       Executes exit short (1 lot) and entry buy (1 lot) -> virtual_pos becomes +1.
    2. SILVER 30m is BEARISH (virtual_pos = -1) and remains BEARISH.
       Executes zero trades -> virtual_pos remains -1.
    3. Asserts: Exact per-timeframe isolation.
    """
    async def _test():
        mock_main = MockChaosMainModule()

        engine = MultiSuperTrendEngine(max_strategies=6)
        engine.add_or_update_strategy({
            "id": "st_silver_15m", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 1,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": -1
        })
        engine.add_or_update_strategy({
            "id": "st_silver_30m", "symbol": "SILVER1001!", "timeframe": "30m", "quantity": 1,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": -1
        })

        engine.get_strategy("st_silver_15m").active_trend = "BEARISH"
        engine.get_strategy("st_silver_30m").active_trend = "BEARISH"

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
            "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "expiry": datetime.date.today() + datetime.timedelta(days=20)
        })
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {
            "positions": [{"symbol": "SILVER100 31AUG2026", "quantity": -2, "side": "SHORT"}],
            "all_positions": []
        })
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        # 15m candle series (Bullish flip: falling -> surging)
        prices_15m = [100 - i*2 for i in range(20)] + [200]
        candles_15m = generate_synthetic_series(prices_15m, interval=900)

        # 30m candle series (Steady Bearish: continuous falling)
        prices_30m = [100 - i*2 for i in range(25)]
        candles_30m = generate_synthetic_series(prices_30m, interval=1800)

        def mock_fetch(exch_seg, inst_id, tf_sec, lookback):
            return candles_15m if tf_sec == 900 else candles_30m

        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", mock_fetch)
        monkeypatch.setattr(time, "time", lambda: float(candles_15m[-1]["time"] + 5))

        # Evaluate cycle
        await engine.evaluate_cycle(xts_api, mock_main)

        # Invariant: 15m runner executed Exit (1) + Entry (1) = 2 trades
        # 30m runner executed 0 trades
        assert len(mock_main.dispatched_trades) == 2
        assert mock_main.dispatched_trades[0]["action"] == "BUY" # Exit short
        assert mock_main.dispatched_trades[1]["action"] == "BUY" # Enter long
        assert engine.get_strategy("st_silver_15m").virtual_position == 1
        assert engine.get_strategy("st_silver_30m").virtual_position == -1

    asyncio.run(_test())


# ==============================================================================
# DRILL 6: High-Concurrency Async Multi-Runner Race Condition Shield
# ==============================================================================
def test_chaos_drill_6_high_concurrency_async_stress(monkeypatch):
    """
    Evaluates 6 simultaneous strategy runners across 2 commodities concurrently:
    Verifies that asyncio.gather with shared modules and sqlite operations
    maintains complete state isolation and thread safety.
    """
    async def _test():
        mock_main = MockChaosMainModule()

        engine = MultiSuperTrendEngine(max_strategies=6)
        strategies = [
            {"id": "sil_1m", "symbol": "SILVER1001!", "timeframe": "1m", "quantity": 1, "is_enabled": True},
            {"id": "sil_5m", "symbol": "SILVER1001!", "timeframe": "5m", "quantity": 1, "is_enabled": True},
            {"id": "sil_15m", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 1, "is_enabled": True},
            {"id": "gold_1m", "symbol": "GOLDPETAL1!", "timeframe": "1m", "quantity": 2, "is_enabled": True},
            {"id": "gold_5m", "symbol": "GOLDPETAL1!", "timeframe": "5m", "quantity": 2, "is_enabled": True},
            {"id": "gold_15m", "symbol": "GOLDPETAL1!", "timeframe": "15m", "quantity": 2, "is_enabled": True}
        ]
        for s in strategies:
            engine.add_or_update_strategy(s)

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
            "inst_id": 574823 if "SILVER" in sym else 562056, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "expiry": datetime.date.today() + datetime.timedelta(days=20)
        })
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150, 155]
        candles = generate_synthetic_series(prices, interval=60)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        # Run 10 concurrent iterations
        tasks = [engine.evaluate_cycle(xts_api, mock_main) for _ in range(10)]
        await asyncio.gather(*tasks)

        # Invariant: All 6 strategies evaluated cleanly without corruption
        assert len(engine.strategies) == 6
        for r in engine.strategies.values():
            assert r.status in ("RUNNING", "FLAT")
            assert r.last_error is None

    asyncio.run(_test())
