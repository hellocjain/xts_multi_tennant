import pytest
import asyncio
import time
import uuid
import sys
from pathlib import Path

# Add project roots to path
client_path = str(Path(__file__).parent.parent / "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine, slice_quantity_for_freeze

class MockMainModule:
    """Mock main module recording dispatches and managing virtual positions."""
    def __init__(self):
        self.dispatched_trades = []
        self.pending_signals = {}
        self.virtual_positions = {}

    def db_insert_pending(self, sig_id, payload):
        self.pending_signals[sig_id] = payload

    def _dispatch_and_record(self, sig_id, action, symbol, qty, price, order_ref, is_paper):
        self.dispatched_trades.append({
            "sig_id": sig_id,
            "action": action,
            "symbol": symbol,
            "quantity": qty,
            "price": price,
            "order_ref": order_ref,
            "is_paper": is_paper
        })

    def db_get_virtual_position(self, strategy_key):
        return self.virtual_positions.get(strategy_key, 0)

    def db_set_virtual_position(self, strategy_key, symbol, timeframe, virtual_position):
        self.virtual_positions[strategy_key] = virtual_position


@pytest.mark.asyncio
async def test_multi_timeframe_same_symbol_opposite_signals():
    """
    Tests that two strategies running on the SAME symbol with different timeframes:
    1. SILVER100 15m (Qty 2)
    2. SILVER100 30m (Qty 2)
    Execute delta orders cleanly without cross-contaminating broker positions.
    """
    mock_main = MockMainModule()

    # 1. Initialize 15m and 30m runners
    r15 = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER100",
        "timeframe": "15m",
        "exchange_segment": "MCXFO",
        "quantity": 2,
        "is_enabled": True
    })
    r30 = SingleSuperTrendRunner({
        "id": "st_silver_30m",
        "symbol": "SILVER100",
        "timeframe": "30m",
        "exchange_segment": "MCXFO",
        "quantity": 2,
        "is_enabled": True
    })

    engine = MultiSuperTrendEngine()
    engine.strategies["st_silver_15m"] = r15
    engine.strategies["st_silver_30m"] = r30

    assert engine.get_portfolio_target_positions() == {"SILVER100": 0}

    # Step 1: 15m flips BULLISH at 10:15
    # Target: +2 Lots (Delta: +2)
    candle_ts_1 = 1700000900
    delta_1 = 2 - r15.virtual_position # +2
    await r15._execute_delta(delta_1, candle_ts_1, mock_main)
    r15.virtual_position = 2
    r15.strategy_position = "LONG"

    assert len(mock_main.dispatched_trades) == 1
    assert mock_main.dispatched_trades[-1]["action"] == "BUY"
    assert mock_main.dispatched_trades[-1]["quantity"] == 2
    assert engine.get_portfolio_target_positions() == {"SILVER100": 2}

    # Step 2: 30m flips BEARISH at 10:30 (15m is still BULLISH)
    # Target: -2 Lots (Delta: -2) -> Net Portfolio becomes 0 FLAT!
    candle_ts_2 = 1700001800
    delta_2 = -2 - r30.virtual_position # -2
    await r30._execute_delta(delta_2, candle_ts_2, mock_main)
    r30.virtual_position = -2
    r30.strategy_position = "SHORT"

    assert len(mock_main.dispatched_trades) == 2
    assert mock_main.dispatched_trades[-1]["action"] == "SELL"
    assert mock_main.dispatched_trades[-1]["quantity"] == 2
    # Combined target is 0 (Net Flat)
    assert engine.get_portfolio_target_positions() == {"SILVER100": 0}

    # Step 3: 15m now also flips BEARISH at 10:45 (both 15m & 30m are BEARISH)
    # 15m Target: -2 Lots (Delta: -2 - (+2) = -4 Lots) -> Net Portfolio becomes -4 SHORT!
    candle_ts_3 = 1700002700
    delta_3 = -2 - r15.virtual_position # -4
    await r15._execute_delta(delta_3, candle_ts_3, mock_main)
    r15.virtual_position = -2
    r15.strategy_position = "SHORT"

    assert len(mock_main.dispatched_trades) == 3
    assert mock_main.dispatched_trades[-1]["action"] == "SELL"
    assert mock_main.dispatched_trades[-1]["quantity"] == 4
    # Combined target is -4 SHORT
    assert engine.get_portfolio_target_positions() == {"SILVER100": -4}

    # Step 4: 15m flips back BULLISH at 11:00 (15m is +2, 30m is -2)
    # 15m Target: +2 Lots (Delta: +2 - (-2) = +4 Lots) -> Net Portfolio becomes 0 FLAT!
    candle_ts_4 = 1700003600
    delta_4 = 2 - r15.virtual_position # +4
    await r15._execute_delta(delta_4, candle_ts_4, mock_main)
    r15.virtual_position = 2
    r15.strategy_position = "LONG"

    assert len(mock_main.dispatched_trades) == 4
    assert mock_main.dispatched_trades[-1]["action"] == "BUY"
    assert mock_main.dispatched_trades[-1]["quantity"] == 4
    assert engine.get_portfolio_target_positions() == {"SILVER100": 0}


@pytest.mark.asyncio
async def test_multi_symbol_independence():
    """
    Tests that multiple symbols (SILVER100 and GOLDM) maintain independent portfolios.
    """
    mock_main = MockMainModule()

    r_silver = SingleSuperTrendRunner({
        "id": "st_silver",
        "symbol": "SILVER100",
        "timeframe": "15m",
        "exchange_segment": "MCXFO",
        "quantity": 2,
        "is_enabled": True
    })
    r_gold = SingleSuperTrendRunner({
        "id": "st_gold",
        "symbol": "GOLDM",
        "timeframe": "5m",
        "exchange_segment": "MCXFO",
        "quantity": 4,
        "is_enabled": True
    })

    engine = MultiSuperTrendEngine()
    engine.strategies["st_silver"] = r_silver
    engine.strategies["st_gold"] = r_gold

    # Silver flips BULLISH
    await r_silver._execute_delta(2, 1700000000, mock_main)
    r_silver.virtual_position = 2

    # Gold flips BEARISH
    await r_gold._execute_delta(-4, 1700000000, mock_main)
    r_gold.virtual_position = -4

    targets = engine.get_portfolio_target_positions()
    assert targets == {"SILVER100": 2, "GOLDM": -4}
    assert len(mock_main.dispatched_trades) == 2
    assert mock_main.dispatched_trades[0]["symbol"] == "SILVER100"
    assert mock_main.dispatched_trades[0]["quantity"] == 2
    assert mock_main.dispatched_trades[1]["symbol"] == "GOLDM"
    assert mock_main.dispatched_trades[1]["quantity"] == 4


@pytest.mark.asyncio
async def test_freeze_limit_slicing_with_delta():
    """
    Tests that large delta orders exceeding exchange freeze limit are cleanly sliced.
    """
    mock_main = MockMainModule()

    r_large = SingleSuperTrendRunner({
        "id": "st_large",
        "symbol": "CRUDEOIL",
        "timeframe": "15m",
        "exchange_segment": "MCXFO",
        "quantity": 30,
        "is_enabled": True
    })

    # Delta of -30 lots with freeze limit of 10 lots -> [10, 10, 10]
    await r_large._execute_delta(-30, 1700000000, mock_main, freeze_limit=10)

    assert len(mock_main.dispatched_trades) == 3
    for trade in mock_main.dispatched_trades:
        assert trade["action"] == "SELL"
        assert trade["quantity"] == 10


@pytest.mark.asyncio
async def test_virtual_position_sqlite_persistence():
    """
    Tests that virtual positions are saved and restored from SQLite seamlessly.
    """
    mock_main = MockMainModule()

    r1 = SingleSuperTrendRunner({
        "id": "st_test",
        "symbol": "SILVER100",
        "timeframe": "15m",
        "quantity": 2,
        "is_enabled": True
    })

    # Execute +2 buy delta
    await r1._execute_delta(2, 1700000000, mock_main)
    r1.virtual_position = 2

    # Verify saved to mock_main
    assert mock_main.db_get_virtual_position("SILVER100_15m") == 2

    # Simulate container boot / new runner creation
    r2 = SingleSuperTrendRunner({
        "id": "st_test_restored",
        "symbol": "SILVER100",
        "timeframe": "15m",
        "virtual_position": mock_main.db_get_virtual_position("SILVER100_15m"),
        "quantity": 2,
        "is_enabled": True
    })

    assert r2.virtual_position == 2
    assert r2.strategy_position == "LONG"
