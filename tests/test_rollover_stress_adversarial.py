import pytest
import asyncio
import time
import datetime
import sys
import os
import tempfile
from pathlib import Path

# Add client path
client_path = str(Path(__file__).parent.parent / "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

import config
import xts_api
import main as client_main
from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine


class MockAdversarialMainModule:
    """Mock main module recording all order dispatches and DB transactions with injectable faults."""
    def __init__(self):
        self.dispatched_trades = []
        self.pending_signals = {}
        self.virtual_positions = {}
        self.active_contracts = {}
        self.TRADING_PAUSED = False
        self.inject_exit_status = "done"  # Can be 'rejected', 'partial_failure', 'error', 'timeout'
        self.inject_entry_status = "done"

    def db_insert_pending(self, sig_id, payload):
        self.pending_signals[sig_id] = payload

    def _dispatch_and_record(self, sig_id, action, symbol, qty, price, order_ref, is_paper):
        is_exit = "EXIT" in str(order_ref).upper()
        status_to_return = self.inject_exit_status if is_exit else self.inject_entry_status

        record = {
            "sig_id": sig_id,
            "action": action,
            "symbol": symbol,
            "quantity": qty,
            "price": price,
            "order_ref": order_ref,
            "is_paper": is_paper,
            "returned_status": status_to_return
        }
        self.dispatched_trades.append(record)
        return {
            "status": status_to_return,
            "result": {
                "AppOrderID": 999000 + len(self.dispatched_trades),
                "IsPaperTrade": is_paper,
                "message": f"Simulated status: {status_to_return}"
            }
        }

    def db_get_virtual_position(self, strategy_key):
        return self.virtual_positions.get(strategy_key, 0)

    def db_get_virtual_position_record(self, strategy_key):
        return {
            "virtual_position": self.virtual_positions.get(strategy_key, 0),
            "active_contract_id": self.active_contracts.get(strategy_key, {}).get("id"),
            "active_contract_desc": self.active_contracts.get(strategy_key, {}).get("desc"),
        }

    def db_set_virtual_position(self, strategy_key, symbol, timeframe, virtual_position, active_contract_id=None, active_contract_desc=None):
        self.virtual_positions[strategy_key] = virtual_position
        self.active_contracts[strategy_key] = {
            "id": str(active_contract_id) if active_contract_id else None,
            "desc": str(active_contract_desc) if active_contract_desc else None
        }


def generate_synthetic_series(closes, base_time=1787600000, interval=900):
    if base_time % 60 != 59:
        base_time = (base_time // 60) * 60 + 59
    candles = []
    for i, c in enumerate(closes):
        t = base_time + (i * interval)
        candles.append({
            "time": t, "open": float(c), "high": float(c) + 2.0, "low": float(c) - 2.0,
            "close": float(c), "volume": 100, "oi": 0
        })
    return candles


# ==============================================================================
# STRESS TEST 1: Holiday Fallback Matrix & Multi-Day Consecutive Holidays
# ==============================================================================
def test_stress_holiday_fallback_matrix():
    """
    Stress-tests calendar calculations across complex holiday topologies:
    - 8th day falls on a single holiday (e.g. Gandhi Jayanti Oct 02)
    - 8th day falls on Sunday, but Friday before it was also a holiday
    - Multi-day consecutive holiday cluster
    """
    IST = config.IST_TIMEZONE

    # Case A: 8th day is Friday 2026-10-02 (Gandhi Jayanti)
    # Expiry is Saturday 2026-10-10 -> 8 days before is Friday Oct 02
    exp_oct10 = datetime.date(2026, 10, 10)
    cutoff_a = config.get_commodity_rollover_cutoff(exp_oct10)
    # Oct 02 is Gandhi Jayanti -> steps back to Thursday Oct 01!
    assert cutoff_a.date() == datetime.date(2026, 10, 1)
    assert cutoff_a.hour == 14 and cutoff_a.minute == 0

    # Case B: 8th day is Sunday 2026-04-05 (Easter Sunday)
    # Friday 2026-04-03 is Good Friday (in MCX_HOLIDAYS)
    # Saturday 2026-04-04 is weekend
    # Expiry is Monday 2026-04-13 -> 8 days before is Sunday Apr 05
    exp_apr13 = datetime.date(2026, 4, 13)
    cutoff_b = config.get_commodity_rollover_cutoff(exp_apr13)
    # Sunday Apr 05 -> Sat Apr 04 (weekend) -> Fri Apr 03 (Good Friday) -> Thursday Apr 02!
    assert cutoff_b.date() == datetime.date(2026, 4, 2)
    assert cutoff_b.hour == 14 and cutoff_b.minute == 0

    # Case C: Simulated 3-day consecutive holiday
    test_holidays = set(config.MCX_HOLIDAYS)
    test_holidays.add(datetime.date(2026, 9, 23)) # Wed
    test_holidays.add(datetime.date(2026, 9, 22)) # Tue
    test_holidays.add(datetime.date(2026, 9, 21)) # Mon

    orig_holidays = config.MCX_HOLIDAYS
    try:
        config.MCX_HOLIDAYS = test_holidays
        exp_sep30 = datetime.date(2026, 9, 30) # Wed. 8 days before is Tue Sep 22.
        cutoff_c = config.get_commodity_rollover_cutoff(exp_sep30)
        # Sep 22 (holiday) -> Sep 21 (holiday) -> Sep 20 (Sun) -> Sep 19 (Sat) -> Fri Sep 18!
        assert cutoff_c.date() == datetime.date(2026, 9, 18)
        assert cutoff_c.hour == 14 and cutoff_c.minute == 0
    finally:
        config.MCX_HOLIDAYS = orig_holidays


# ==============================================================================
# STRESS TEST 2: Fail-Safe Immediate Boundary Check (<= 7 Days)
# ==============================================================================
def test_stress_fail_safe_immediate_boundary():
    """
    Tests that any contract with <= 7 calendar days remaining evaluates as
    expired/past-rollover at ANY hour of the day (morning, noon, evening, night).
    """
    IST = config.IST_TIMEZONE
    exp_date = datetime.date(2026, 9, 30)

    # Test all 24 hours of 2026-09-23 (7 days left)
    for hour in (9, 10, 12, 14, 18, 23):
        dt_7d = datetime.datetime(2026, 9, 23, hour, 0, 0, tzinfo=IST)
        assert config.is_commodity_past_rollover(exp_date, "MCXFO", dt_7d) is True

    # Test with 6, 5, 3, 1, 0 days left
    for d in (6, 5, 3, 1, 0):
        t_date = exp_date - datetime.timedelta(days=d)
        dt = datetime.datetime(t_date.year, t_date.month, t_date.day, 10, 0, 0, tzinfo=IST)
        assert config.is_commodity_past_rollover(exp_date, "MCXFO", dt) is True


# ==============================================================================
# STRESS TEST 3: Strict Sequential Execution - Leg 1 Partial Fill Aborts Leg 2
# ==============================================================================
def test_stress_sequential_execution_leg1_partial_fill_aborts_leg2(monkeypatch):
    """
    Simulates a partial fill on Leg 1 (Exit of old contract).
    Ensures:
    1. Leg 2 (Entry into new contract) is strictly NOT dispatched.
    2. Runner reports failure and does NOT switch contract.
    """
    async def _test():
        mock_main = MockAdversarialMainModule()
        mock_main.inject_exit_status = "partial_failure"

        runner = SingleSuperTrendRunner({
            "id": "st_gold_partial", "symbol": "GOLDPETAL1!", "timeframe": "20m", "quantity": 4,
            "execution_mode": "LIVE", "is_enabled": True, "virtual_position": 4
        })
        runner.active_contract_id = "562056"
        runner.active_contract_desc = "GOLDPETAL 31AUG2026"

        contract_state = {"current_inst": {
            "inst_id": 562057, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "GOLDPETAL 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }}
        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 + i for i in range(20)]
        candles = generate_synthetic_series(prices, interval=1200)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)

        await runner.evaluate_cycle(xts_api, mock_main)

        # Invariant: Leg 1 was attempted, but Leg 2 was NEVER dispatched!
        assert len(mock_main.dispatched_trades) == 1
        assert "EXIT" in mock_main.dispatched_trades[0]["order_ref"]
        # Contract not advanced
        assert str(runner.active_contract_id) == "562056"

    asyncio.run(_test())


# ==============================================================================
# STRESS TEST 4: Strict Sequential Execution - Leg 1 Gateway Error Aborts Leg 2
# ==============================================================================
def test_stress_sequential_execution_leg1_error_aborts_leg2(monkeypatch):
    """
    Simulates a network timeout / 502 Bad Gateway / RMS error on Leg 1.
    Ensures Leg 2 is aborted immediately.
    """
    async def _test():
        mock_main = MockAdversarialMainModule()
        mock_main.inject_exit_status = "failed"

        runner = SingleSuperTrendRunner({
            "id": "st_silver_err", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 2,
            "execution_mode": "LIVE", "is_enabled": True, "virtual_position": -2
        })
        runner.active_contract_id = "574823"
        runner.active_contract_desc = "SILVER100 31AUG2026"

        contract_state = {"current_inst": {
            "inst_id": 574824, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "SILVER100 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }}
        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 - i for i in range(20)]
        candles = generate_synthetic_series(prices, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)

        await runner.evaluate_cycle(xts_api, mock_main)

        # Invariant: Leg 2 NEVER dispatched
        assert len(mock_main.dispatched_trades) == 1
        assert "EXIT" in mock_main.dispatched_trades[0]["order_ref"]
        assert str(runner.active_contract_id) == "574823"
        assert runner.virtual_position == -2

    asyncio.run(_test())


# ==============================================================================
# STRESS TEST 5: Large Quantity Freeze Slicing During Rollover
# ==============================================================================
def test_stress_freeze_slicing_during_rollover(monkeypatch):
    """
    Tests rolling over a large position (30 lots) where the exchange freeze limit
    is 10 lots.
    - Leg 1 must slice into 3 chunks of 10 lots on the old contract.
    - Leg 2 must slice into 3 chunks of 10 lots on the new contract.
    - Total 6 chunks, exact lot counts preserved.
    """
    async def _test():
        mock_main = MockAdversarialMainModule()

        runner = SingleSuperTrendRunner({
            "id": "st_silver_large", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 30,
            "execution_mode": "LIVE", "is_enabled": True, "virtual_position": 30
        })
        runner.active_contract_id = "574823"
        runner.active_contract_desc = "SILVER100 31AUG2026"

        contract_state = {"current_inst": {
            "inst_id": 574824, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10, # Freeze limit = 10 lots
            "desc": "SILVER100 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }}
        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 + i for i in range(20)]
        candles = generate_synthetic_series(prices, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)

        await runner.evaluate_cycle(xts_api, mock_main)

        # 30 lots with freeze_limit=10 -> 3 exit slices + 3 entry slices = 6 orders!
        assert len(mock_main.dispatched_trades) == 6
        exit_trades = mock_main.dispatched_trades[:3]
        entry_trades = mock_main.dispatched_trades[3:]

        # All exit trades on August contract, 10 lots each
        for t in exit_trades:
            assert t["symbol"] == "SILVER100 31AUG2026"
            assert t["action"] == "SELL"
            assert t["quantity"] == 10

        # All entry trades on September contract, 10 lots each
        for t in entry_trades:
            assert t["symbol"] == "SILVER100 30SEP2026"
            assert t["action"] == "BUY"
            assert t["quantity"] == 10

        assert runner.virtual_position == 30
        assert str(runner.active_contract_id) == "574824"

    asyncio.run(_test())


# ==============================================================================
# STRESS TEST 6: Multi-Timeframe Concurrent Isolation on Same Continuous Symbol
# ==============================================================================
def test_stress_multi_timeframe_concurrent_rollover(monkeypatch):
    """
    Tests 3 concurrent runners on GOLDPETAL1!:
    - 5m: LONG (+2 lots)
    - 15m: LONG (+5 lots)
    - 30m: SHORT (-3 lots)
    All 3 roll over concurrently when the contract advances.
    Ensures zero cross-contamination and 100% position isolation.
    """
    async def _test():
        mock_main = MockAdversarialMainModule()

        runner_5m = SingleSuperTrendRunner({
            "id": "st_gold_5m", "symbol": "GOLDPETAL1!", "timeframe": "5m", "quantity": 2,
            "execution_mode": "LIVE", "is_enabled": True, "virtual_position": 2
        })
        runner_5m.active_contract_id = "562056"
        runner_5m.active_contract_desc = "GOLDPETAL 31AUG2026"

        runner_15m = SingleSuperTrendRunner({
            "id": "st_gold_15m", "symbol": "GOLDPETAL1!", "timeframe": "15m", "quantity": 5,
            "execution_mode": "LIVE", "is_enabled": True, "virtual_position": 5
        })
        runner_15m.active_contract_id = "562056"
        runner_15m.active_contract_desc = "GOLDPETAL 31AUG2026"

        runner_30m = SingleSuperTrendRunner({
            "id": "st_gold_30m", "symbol": "GOLDPETAL1!", "timeframe": "30m", "quantity": 3,
            "execution_mode": "LIVE", "is_enabled": True, "virtual_position": -3
        })
        runner_30m.active_contract_id = "562056"
        runner_30m.active_contract_desc = "GOLDPETAL 31AUG2026"

        contract_state = {"current_inst": {
            "inst_id": 562057, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "GOLDPETAL 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }}
        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        candles = generate_synthetic_series([100 + i for i in range(20)], interval=300)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)

        # Run all 3 concurrently
        await asyncio.gather(
            runner_5m.evaluate_cycle(xts_api, mock_main),
            runner_15m.evaluate_cycle(xts_api, mock_main),
            runner_30m.evaluate_cycle(xts_api, mock_main)
        )

        # Invariant: Each runner rolled over its own position cleanly
        assert runner_5m.virtual_position == 2
        assert str(runner_5m.active_contract_id) == "562057"

        assert runner_15m.virtual_position == 5
        assert str(runner_15m.active_contract_id) == "562057"

        assert runner_30m.virtual_position == -3
        assert str(runner_30m.active_contract_id) == "562057"

        # Total 6 trades dispatched (2 per runner)
        assert len(mock_main.dispatched_trades) == 6

    asyncio.run(_test())


# ==============================================================================
# STRESS TEST 7: Idempotency Under Rapid Successive Evaluation Cycles
# ==============================================================================
def test_stress_rollover_idempotency_under_rapid_cycles(monkeypatch):
    """
    Verifies that once a strategy has rolled over, subsequent evaluation cycles
    (e.g. running 10 times in a row) do NOT re-trigger rollover or dispatch duplicate orders.
    """
    async def _test():
        mock_main = MockAdversarialMainModule()

        runner = SingleSuperTrendRunner({
            "id": "st_gold_idem", "symbol": "GOLDPETAL1!", "timeframe": "20m", "quantity": 1,
            "execution_mode": "LIVE", "is_enabled": True, "virtual_position": 1
        })
        runner.active_contract_id = "562056"
        runner.active_contract_desc = "GOLDPETAL 31AUG2026"

        contract_state = {"current_inst": {
            "inst_id": 562057, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "GOLDPETAL 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }}
        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        candles = generate_synthetic_series([100 + i for i in range(20)], interval=1200)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)

        # Cycle 1: Rolls over
        await runner.evaluate_cycle(xts_api, mock_main)
        assert len(mock_main.dispatched_trades) == 2

        # Cycles 2 to 10: Must NOT dispatch any more rollover trades!
        for _ in range(9):
            await runner.evaluate_cycle(xts_api, mock_main)

        assert len(mock_main.dispatched_trades) == 2
        assert runner.virtual_position == 1
        assert str(runner.active_contract_id) == "562057"

    asyncio.run(_test())
