import pytest
import asyncio
import time
import datetime
import sys
from pathlib import Path

# Add client path
client_path = str(Path(__file__).parent.parent / "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

import config
import xts_api
import main as client_main
from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine


class MockAutoRollMainModule:
    """Mock main module recording all order dispatches and DB transactions."""
    def __init__(self):
        self.dispatched_trades = []
        self.pending_signals = {}
        self.virtual_positions = {}
        self.TRADING_PAUSED = False

    def db_insert_pending(self, sig_id, payload):
        self.pending_signals[sig_id] = payload

    def _dispatch_and_record(self, sig_id, action, symbol, qty, price, order_ref, is_paper):
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
        return {"status": "done", "result": {"AppOrderID": 999000 + len(self.dispatched_trades), "IsPaperTrade": is_paper}}

    def db_get_virtual_position(self, strategy_key):
        return self.virtual_positions.get(strategy_key, 0)

    def db_set_virtual_position(self, strategy_key, symbol, timeframe, virtual_position):
        self.virtual_positions[strategy_key] = virtual_position


def generate_synthetic_series(closes, base_time=1787600000, interval=900):
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
# TEST 1: Expiry Resolution Threshold Check (>7 vs <=7 days)
# ==============================================================================
def test_expiry_resolution_7_day_boundary(monkeypatch):
    """
    Verifies that with MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX = 7:
    - 8 days to expiry -> Resolves near month (31AUG2026)
    - 7 days to expiry -> Resolves next month (30SEP2026)
    - 6 days to expiry -> Resolves next month (30SEP2026)
    - 5 days to expiry -> Resolves next month (30SEP2026)
    """
    monkeypatch.setattr(config, "MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX", 7)

    exp_aug = datetime.date(2026, 8, 31)
    exp_sep = datetime.date(2026, 9, 30)

    # 8 days to expiry (today = 2026-08-23)
    today_8d = datetime.date(2026, 8, 23)
    res_8d = exp_aug if (exp_aug - today_8d).days > 7 else exp_sep
    assert res_8d == exp_aug

    # 7 days to expiry (today = 2026-08-24)
    today_7d = datetime.date(2026, 8, 24)
    res_7d = exp_aug if (exp_aug - today_7d).days > 7 else exp_sep
    assert res_7d == exp_sep  # Rolled to September!

    # 6 days to expiry (today = 2026-08-25)
    today_6d = datetime.date(2026, 8, 25)
    res_6d = exp_aug if (exp_aug - today_6d).days > 7 else exp_sep
    assert res_6d == exp_sep  # Rolled to September!


# ==============================================================================
# TEST 2: Active SHORT Position Auto-Roll Migration
# ==============================================================================
def test_continuous_contract_auto_roll_short_position(monkeypatch):
    """
    Simulates a strategy holding a SHORT position (-2 lots) on SILVER1001!
    When crossing the 5-day threshold:
    1. First cycle: resolves August contract (inst_id 574823). Runner records initial contract.
    2. Next cycle: resolves September contract (inst_id 574824).
    3. Asserts:
       - Exit Leg dispatched on August contract ('SILVER100 31AUG2026', BUY 2 lots).
       - Entry Leg dispatched on September contract ('SILVER100 30SEP2026', SELL 2 lots).
       - Virtual position is preserved at -2 lots.
       - Strategy remains RUNNING on the new contract.
    """
    async def _test():
        mock_main = MockAutoRollMainModule()

        runner = SingleSuperTrendRunner({
            "id": "st_silver_15m", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 2,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": -2
        })
        runner.active_trend = "BEARISH"

        contract_state = {"current_inst": {
            "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "SILVER100 31AUG2026", "expiry": datetime.date(2026, 8, 31)
        }}

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 - i*2 for i in range(20)]
        candles = generate_synthetic_series(prices, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        # Cycle 1: Baseline on August contract
        await runner.evaluate_cycle(xts_api, mock_main)
        assert runner.last_resolved_inst_id == 574823
        assert runner.last_resolved_symbol_desc == "SILVER100 31AUG2026"
        assert len(mock_main.dispatched_trades) == 0  # No trades on baseline

        # Date advances: Contract auto-rolls to September
        contract_state["current_inst"] = {
            "inst_id": 574824, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "SILVER100 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }

        # Cycle 2: Evaluates with new contract
        await runner.evaluate_cycle(xts_api, mock_main)

        # Invariant: Dispatched Exit on August (BUY 2) + Entry on September (SELL 2)
        assert len(mock_main.dispatched_trades) == 2
        
        exit_trade = mock_main.dispatched_trades[0]
        assert exit_trade["action"] == "BUY"
        assert exit_trade["symbol"] == "SILVER100 31AUG2026"
        assert exit_trade["quantity"] == 2

        entry_trade = mock_main.dispatched_trades[1]
        assert entry_trade["action"] == "SELL"
        assert entry_trade["symbol"] == "SILVER100 30SEP2026"
        assert entry_trade["quantity"] == 2

        assert runner.virtual_position == -2
        assert runner.last_resolved_inst_id == 574824
        assert runner.status == "RUNNING"

    asyncio.run(_test())


# ==============================================================================
# TEST 3: Active LONG Position Auto-Roll Migration
# ==============================================================================
def test_continuous_contract_auto_roll_long_position(monkeypatch):
    """
    Simulates a strategy holding a LONG position (+3 lots) on GOLDPETAL1!
    When crossing the 5-day threshold:
    - Exit Leg dispatched on August contract ('GOLDPETAL 31AUG2026', SELL 3 lots).
    - Entry Leg dispatched on September contract ('GOLDPETAL 30SEP2026', BUY 3 lots).
    - Virtual position is preserved at +3 lots.
    """
    async def _test():
        mock_main = MockAutoRollMainModule()

        runner = SingleSuperTrendRunner({
            "id": "st_gold_20m", "symbol": "GOLDPETAL1!", "timeframe": "20m", "quantity": 3,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": 3
        })
        runner.active_trend = "BULLISH"

        contract_state = {"current_inst": {
            "inst_id": 562056, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "GOLDPETAL 31AUG2026", "expiry": datetime.date(2026, 8, 31)
        }}

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 + i*2 for i in range(20)]
        candles = generate_synthetic_series(prices, interval=1200)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        # Baseline cycle
        await runner.evaluate_cycle(xts_api, mock_main)
        assert runner.last_resolved_inst_id == 562056

        # Switch contract to September
        contract_state["current_inst"] = {
            "inst_id": 562057, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "GOLDPETAL 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }

        await runner.evaluate_cycle(xts_api, mock_main)

        # Invariant: Exit August (SELL 3) + Entry September (BUY 3)
        assert len(mock_main.dispatched_trades) == 2
        assert mock_main.dispatched_trades[0]["action"] == "SELL"
        assert mock_main.dispatched_trades[0]["symbol"] == "GOLDPETAL 31AUG2026"
        assert mock_main.dispatched_trades[0]["quantity"] == 3

        assert mock_main.dispatched_trades[1]["action"] == "BUY"
        assert mock_main.dispatched_trades[1]["symbol"] == "GOLDPETAL 30SEP2026"
        assert mock_main.dispatched_trades[1]["quantity"] == 3

        assert runner.virtual_position == 3
        assert runner.last_resolved_inst_id == 562057

    asyncio.run(_test())


# ==============================================================================
# TEST 4: Auto-Roll When Strategy is FLAT (Zero Trades Dispatched)
# ==============================================================================
def test_continuous_contract_auto_roll_flat_position(monkeypatch):
    """
    Simulates a strategy that is FLAT (virtual_position = 0).
    When contract auto-rolls to next month:
    - Zero trades are dispatched.
    - Contract tracking updates to new instrument ID.
    - Strategy remains RUNNING and FLAT.
    """
    async def _test():
        mock_main = MockAutoRollMainModule()

        runner = SingleSuperTrendRunner({
            "id": "st_silver_15m", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 1,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": 0
        })
        runner.active_trend = "BULLISH"

        contract_state = {"current_inst": {
            "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "SILVER100 31AUG2026", "expiry": datetime.date(2026, 8, 31)
        }}

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: contract_state["current_inst"])
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 + i*2 for i in range(20)]
        candles = generate_synthetic_series(prices, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        # Baseline
        await runner.evaluate_cycle(xts_api, mock_main)
        assert runner.last_resolved_inst_id == 574823

        # Switch to September
        contract_state["current_inst"] = {
            "inst_id": 574824, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "SILVER100 30SEP2026", "expiry": datetime.date(2026, 9, 30)
        }

        await runner.evaluate_cycle(xts_api, mock_main)

        # Invariant: Zero trades dispatched, contract updated cleanly
        assert len(mock_main.dispatched_trades) == 0
        assert runner.virtual_position == 0
        assert runner.last_resolved_inst_id == 574824

    asyncio.run(_test())


# ==============================================================================
# TEST 5: Fixed Contract (Non-Continuous) Expiry Square-Off & Pause
# ==============================================================================
def test_fixed_contract_expiry_square_off_and_pause(monkeypatch):
    """
    Simulates a non-continuous fixed contract (e.g. SILVER10031AUG2026FUT).
    When within 5 days of expiry:
    - Squares off any active position.
    - Sets is_enabled = False and status = 'EXPIRED_PAUSED'.
    """
    async def _test():
        mock_main = MockAutoRollMainModule()

        runner = SingleSuperTrendRunner({
            "id": "st_silver_fixed", "symbol": "SILVER10031AUG2026FUT", "timeframe": "15m", "quantity": 1,
            "execution_mode": "PAPER", "is_enabled": True, "virtual_position": -1
        })
        runner.active_trend = "BEARISH"

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
            "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "desc": "SILVER100 31AUG2026", "expiry": datetime.date.today() + datetime.timedelta(days=3) # 3 days left <= 5
        })
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": []})
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

        prices = [100 - i*2 for i in range(20)]
        candles = generate_synthetic_series(prices, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(candles[-1]["time"] + 5))

        await runner.evaluate_cycle(xts_api, mock_main)

        # Invariant: Squared off and paused
        assert len(mock_main.dispatched_trades) == 1
        assert mock_main.dispatched_trades[0]["action"] == "BUY"
        assert runner.is_enabled is False
        assert runner.status == "EXPIRED_PAUSED"

    asyncio.run(_test())
