import pytest
import asyncio
import time
import math
import sys
import os

# Ensure client is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from custom_strategy_engine import (
    BaseStrategy,
    SingleCustomStrategyRunner,
    MultiCustomStrategyEngine,
    parse_timeframe_to_seconds
)

def test_base_strategy_math_helpers():
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
    ema = BaseStrategy.calculate_ema(prices, 3)
    assert len(ema) == len(prices)
    assert ema[0] == 10.0
    assert ema[-1] > 10.0

    sma = BaseStrategy.calculate_sma(prices, 3)
    assert len(sma) == len(prices) - 2
    assert sma[0] == (10.0 + 11.0 + 12.0) / 3.0

    rsi = BaseStrategy.calculate_rsi(prices, 5)
    assert isinstance(rsi, list)

def test_parse_timeframe_to_seconds():
    assert parse_timeframe_to_seconds("5m") == 300
    assert parse_timeframe_to_seconds("15m") == 900
    assert parse_timeframe_to_seconds("1h") == 3600
    assert parse_timeframe_to_seconds("1d") == 86400

def test_custom_strategy_runner_compilation():
    code = """
class MyRunnerStrategy(BaseStrategy):
    def on_candle(self, candle, history, position):
        return "BUY"
"""
    runner = SingleCustomStrategyRunner({
        "id": "test_runner_1",
        "name": "My Runner",
        "symbol": "GOLDPETAL1!",
        "timeframe": "15m",
        "quantity": 4,
        "is_enabled": True,
        "code_content": code
    })
    assert runner.strategy_instance is not None
    assert runner.compile_error is None
    assert runner.symbol == "GOLDPETAL1!"
    assert runner.quantity == 4

def test_custom_strategy_runner_syntax_error():
    broken_code = "class BrokenStrategy(BaseStrategy): def on_candle(self:"
    runner = SingleCustomStrategyRunner({
        "id": "test_runner_2",
        "name": "Broken",
        "code_content": broken_code
    })
    assert runner.strategy_instance is None
    assert runner.compile_error is not None

def test_multi_custom_strategy_engine_dry_run():
    ema_code = """
class DualEmaStrategy(BaseStrategy):
    def on_candle(self, candle, history, position):
        if len(history) < 10:
            return "HOLD"
        closes = [c["close"] for c in history] + [candle["close"]]
        fast_ema = self.calculate_ema(closes, 5)
        slow_ema = self.calculate_ema(closes, 10)
        if fast_ema[-1] > slow_ema[-1] and fast_ema[-2] <= slow_ema[-2]:
            return "BUY"
        elif fast_ema[-1] < slow_ema[-1] and fast_ema[-2] >= slow_ema[-2]:
            return "SELL"
        return "HOLD"
"""
    # Generate 50 synthetic candles oscillating with sine wave
    candles = []
    base_time = 1700000000
    for i in range(50):
        price = 10000.0 + (math.sin(i * 0.3) * 50.0)
        candles.append({
            "time": base_time + (i * 900),
            "open": price,
            "high": price + 5.0,
            "low": price - 5.0,
            "close": price,
            "volume": 100
        })

    result = MultiCustomStrategyEngine.evaluate_dry_run(ema_code, candles)
    assert result.get("error") is None
    assert result.get("total_candles") == 50
    assert result.get("signals_count") > 0
    assert isinstance(result.get("signals"), list)

@pytest.mark.asyncio
async def test_custom_strategy_fault_tolerance():
    # User strategy raises runtime exception
    crash_code = """
class CrashingStrategy(BaseStrategy):
    def on_candle(self, candle, history, position):
        # Intentional divide by zero error
        x = 1 / 0
        return "BUY"
"""
    runner = SingleCustomStrategyRunner({
        "id": "crash_runner",
        "name": "Crashing Strategy",
        "symbol": "GOLDPETAL1!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True,
        "code_content": crash_code
    })

    # Mock xts_api and main_module
    class MockXtsApi:
        @staticmethod
        def resolve_contract(sym):
            return {"inst_id": 12345, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000}
        @staticmethod
        def get_positions_telemetry():
            return {"positions": []}
        @staticmethod
        def get_broker_orders():
            return []
        @staticmethod
        def fetch_ohlc_candles(seg, iid, tf, count):
            now = int(time.time())
            return [
                {"time": now - 1800, "open": 100, "high": 105, "low": 95, "close": 100, "volume": 10},
                {"time": now - 900, "open": 100, "high": 105, "low": 95, "close": 100, "volume": 10},
                {"time": now, "open": 100, "high": 105, "low": 95, "close": 100, "volume": 10}
            ]

    class MockMain:
        pass

    # Should not raise exception
    await runner.evaluate_cycle(MockXtsApi, MockMain)
    assert runner.last_error is not None
    assert "division by zero" in runner.last_error.lower()

@pytest.mark.asyncio
async def test_custom_strategy_infinite_loop_timeout():
    # User strategy with an intentional infinite loop in on_candle
    loop_code = """
class RunawayStrategy(BaseStrategy):
    def on_candle(self, candle, history, position):
        # Runaway loop exceeding 2.0s timeout
        time.sleep(3.0)
        return "BUY"
"""
    runner = SingleCustomStrategyRunner({
        "id": "loop_runner",
        "name": "Runaway Strategy",
        "symbol": "GOLDPETAL1!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True,
        "code_content": loop_code
    })

    class MockXtsApi:
        @staticmethod
        def resolve_contract(sym):
            return {"inst_id": 12345, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000}
        @staticmethod
        def get_positions_telemetry():
            return {"positions": []}
        @staticmethod
        def get_broker_orders():
            return []
        @staticmethod
        def fetch_ohlc_candles(seg, iid, tf, count):
            now = int(time.time())
            return [
                {"time": now - 1800, "open": 100, "high": 105, "low": 95, "close": 100, "volume": 10},
                {"time": now - 900, "open": 100, "high": 105, "low": 95, "close": 100, "volume": 10},
                {"time": now, "open": 100, "high": 105, "low": 95, "close": 100, "volume": 10}
            ]

    class MockMain:
        pass

    # Should catch TimeoutError and not hang indefinitely
    t0 = time.time()
    await runner.evaluate_cycle(MockXtsApi, MockMain)
    elapsed = time.time() - t0
    assert elapsed < 3.0
    assert runner.last_error is not None
    assert "exceeded 2.0s wall-clock limit" in runner.last_error

def test_safe_getattr_blocks_dunder():
    from custom_strategy_engine import safe_getattr, safe_hasattr, safe_setattr
    class Dummy:
        pass
    d = Dummy()
    assert safe_hasattr(d, "__class__") is False
    with pytest.raises(PermissionError):
        safe_getattr(d, "__class__")
    with pytest.raises(PermissionError):
        safe_setattr(d, "__class__", int)

