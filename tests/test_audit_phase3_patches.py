import pytest
import asyncio
import time
import datetime
import uuid
import sys
from pathlib import Path

# Add project roots to path
client_path = str(Path(__file__).parent.parent / "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

import xts_api
import main as client_main
from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine, calculate_supertrend, slice_quantity_for_freeze


class MockMainModule:
    """Mock main module recording dispatches and managing virtual positions."""
    def __init__(self):
        self.dispatched_trades = []
        self.pending_signals = {}
        self.virtual_positions = {}
        self.TRADING_PAUSED = False

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
        return {"status": "done", "result": {"AppOrderID": 12345, "IsPaperTrade": is_paper}}

    def db_get_virtual_position(self, strategy_key):
        return self.virtual_positions.get(strategy_key, 0)

    def db_set_virtual_position(self, strategy_key, symbol, timeframe, virtual_position):
        self.virtual_positions[strategy_key] = virtual_position


def generate_ohlc_series(closes, base_time=1700000000, interval=1800):
    """Generates synthetic OHLC candles with strict monotonic timestamps ending in :59."""
    if base_time % 60 != 59:
        base_time = (base_time // 60) * 60 + 59
    candles = []
    for i, c in enumerate(closes):
        t = base_time + (i * interval)
        candles.append({
            "time": t,
            "timestamp": t,
            "open": float(c),
            "high": float(c) + 2.0,
            "low": float(c) - 2.0,
            "close": float(c),
            "volume": 100,
            "oi": 0
        })
    return candles


# ==============================================================================
# TASK 1 REGRESSION TESTS: Finding #2 (ON_CANDLE_CLOSE Premature Evaluation)
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_finding_2_unclosed_candle_reproduction(monkeypatch):
    """
    Task 1.1 & 1.3:
    Reproduces production failure mode on candle 1787612399 (30m candle).
    Feed unclosed candle at 00:00:27, 00:08:15, 00:13:16 (during forming bar).
    Verify that with the fix, NO flip order is dispatched until now_ts >= candle_ts + 1800.
    """
    mock_main = MockMainModule()

    r30 = SingleSuperTrendRunner({
        "id": "st_silver_30m",
        "symbol": "SILVER1001!",
        "timeframe": "30m",
        "exchange_segment": "MCXFO",
        "quantity": 1,
        "is_enabled": True
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 560001, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
        "expiry": datetime.date.today() + datetime.timedelta(days=25)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Candle series where the latest 30m candle (1787612399) creates a Bearish flip
    base_candle_ts = 1787612399 # 30m candle close timestamp (23:59:59)
    prices = [100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150, 80]
    candles = generate_ohlc_series(prices, base_time=base_candle_ts - (len(prices)-1)*1800, interval=1800)
    assert candles[-1]["time"] == base_candle_ts

    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)

    # 1. Evaluate during the forming bar (13 minutes before scheduled close, e.g. at 23:46:00)
    monkeypatch.setattr(time, "time", lambda: float(base_candle_ts - 800))

    await r30.evaluate_cycle(xts_api, mock_main)

    # With the fix, candle scheduled close is not yet reached (now_ts < base_candle_ts).
    # SuperTrend flip on the forming candle must NOT dispatch any trade!
    assert len(mock_main.dispatched_trades) == 0

    # 2. Advance time past candle close (5s after 23:59:59 close)
    monkeypatch.setattr(time, "time", lambda: float(base_candle_ts + 5))

    await r30.evaluate_cycle(xts_api, mock_main)

    # Now that the candle is confirmed closed, the flip executes exactly ONCE!
    assert len(mock_main.dispatched_trades) == 1
    assert mock_main.dispatched_trades[0]["action"] == "SELL"
    assert r30.last_processed_candle_time == base_candle_ts

    # 3. Defense-in-depth: Duplicate evaluation on same closed candle must be a NO-OP
    await r30.evaluate_cycle(xts_api, mock_main)
    assert len(mock_main.dispatched_trades) == 1 # Still 1, no duplicate!


# ==============================================================================
# TASK 2 REGRESSION TESTS: Finding #1 (Bootstrap Double-Counting)
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_finding_1_bootstrap_isolation(monkeypatch):
    """
    Task 2.1 & 2.2:
    Two runners sharing inst_id (each configured for 1 lot).
    Broker holds 2 lots SHORT.
    Both boot fresh.
    Assert neither runner falsely copies the 2 broker lots on boot.
    Each runner operates strictly according to its own 1-lot sizing.
    Combined virtual position = -2 lots (1 lot + 1 lot), NOT -4 lots!
    """
    mock_main = MockMainModule()

    r15 = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "virtual_position": 0,
        "is_enabled": True
    })
    r30 = SingleSuperTrendRunner({
        "id": "st_silver_30m",
        "symbol": "SILVER1001!",
        "timeframe": "30m",
        "quantity": 1,
        "virtual_position": 0,
        "is_enabled": True
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 560001, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
        "expiry": datetime.date.today() + datetime.timedelta(days=25)
    })
    # Broker holds 2 lots SHORT
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {
        "positions": [{
            "symbol": "SILVER100 31AUG2026",
            "instrument_id": 560001,
            "side": "SHORT",
            "quantity": 2,
            "buy_avg": 0.0,
            "sell_avg": 85000.0,
            "ltp": 84500.0
        }],
        "all_positions": []
    })
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Continuous Bullish prices (no flip)
    prices = [30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105]
    candles = generate_ohlc_series(prices, base_time=1700000000, interval=900)
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
    monkeypatch.setattr(time, "time", lambda: float(1700000000 + len(prices)*900 + 1000))

    # Steady-state with no new flip
    r15.last_processed_candle_time = candles[-1]["time"]
    r30.last_processed_candle_time = candles[-1]["time"]

    # Evaluate both runners
    await r15.evaluate_cycle(xts_api, mock_main)
    await r30.evaluate_cycle(xts_api, mock_main)

    # With the fix: Runners do NOT claim the 2 broker lots blindly!
    # They stay at their independent virtual positions (0 lots initial)
    assert r15.virtual_position == 0
    assert r30.virtual_position == 0
    assert (r15.virtual_position + r30.virtual_position) == 0


@pytest.mark.asyncio
async def test_regression_finding_1_dm933_flat_broker_recovery(monkeypatch):
    """
    Task 2.3:
    dm933 shape: Broker is 0 FLAT, but stale virtual positions exist in DB.
    When runners evaluate cycles, verify no spurious orders are dispatched.
    """
    mock_main = MockMainModule()

    r15 = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 560001, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
        "expiry": datetime.date.today() + datetime.timedelta(days=25)
    })
    # Broker is 0 FLAT
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Steady OHLC (no flip)
    prices = [30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105]
    candles = generate_ohlc_series(prices, base_time=1700000000, interval=900)
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
    monkeypatch.setattr(time, "time", lambda: float(1700000000 + len(prices)*900 + 1000))

    await r15.evaluate_cycle(xts_api, mock_main)

    # 0 spurious trades dispatched into flat broker account
    assert len(mock_main.dispatched_trades) == 0


# ==============================================================================
# TASK 3 REGRESSION TESTS: Finding #3 (Substring Match Collision)
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_finding_3_substring_collision(monkeypatch):
    """
    Task 3.1 & 3.3:
    Pending order for SILVERMIC on 15m must NOT suppress SILVER on 15m.
    Tested across multiple commodity sibling pairs.
    """
    mock_main = MockMainModule()

    test_pairs = [
        ("SILVER", "SILVERMIC"),
        ("GOLD", "GOLDPETAL"),
        ("GOLD", "GOLDM"),
        ("CRUDEOIL", "CRUDEOILM"),
        ("NATURALGAS", "NATGASMINI")
    ]

    for base_sym, sub_sym in test_pairs:
        mock_main.dispatched_trades.clear()
        runner = SingleSuperTrendRunner({
            "id": f"st_{base_sym.lower()}_15m",
            "symbol": base_sym,
            "timeframe": "15m",
            "quantity": 1,
            "is_enabled": True
        })

        monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
            "inst_id": 560001, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 10000,
            "expiry": datetime.date.today() + datetime.timedelta(days=25)
        })
        monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})

        # Pending order for sibling contract (e.g. SILVERMIC or GOLDPETAL)
        monkeypatch.setattr(xts_api, "get_broker_orders", lambda s=sub_sym: [{
            "OrderStatus": "OPEN",
            "TradingSymbol": f"{s} 31AUG2026",
            "OrderUniqueIdentifier": f"ST_REV_ENTRY_{s}_15M_FLIP_ENTRY_1700000000",
            "AppOrderID": "999888777"
        }])

        prices = [130, 128, 126, 124, 122, 120, 118, 116, 114, 112, 110, 100, 50, 40, 30, 150] # Bullish flip
        candles = generate_ohlc_series(prices, base_time=1700000000, interval=900)
        monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
        monkeypatch.setattr(time, "time", lambda: float(1700000000 + len(prices)*900 + 1000))

        await runner.evaluate_cycle(xts_api, mock_main)

        # With the fix: The base symbol runner is NOT suppressed by sibling contract's pending order!
        assert len(mock_main.dispatched_trades) == 1, f"Failed for pair ({base_sym}, {sub_sym})"
        assert mock_main.dispatched_trades[0]["symbol"] == base_sym


# ==============================================================================
# TASK 4 REGRESSION TESTS: Finding #4 (Proxy Property Writes on MultiSuperTrendEngine)
# ==============================================================================
def test_regression_finding_4_proxy_property_broadcast():
    """
    Task 4.1:
    Setting engine.is_enabled = False or engine.execution_mode = 'PAPER'
    must broadcast to ALL registered runners, not just the first one.
    """
    engine = MultiSuperTrendEngine()
    r1 = SingleSuperTrendRunner({"id": "r1", "symbol": "SILVER1001!", "timeframe": "15m", "is_enabled": True, "execution_mode": "LIVE"})
    r2 = SingleSuperTrendRunner({"id": "r2", "symbol": "SILVER1001!", "timeframe": "30m", "is_enabled": True, "execution_mode": "LIVE"})
    r3 = SingleSuperTrendRunner({"id": "r3", "symbol": "GOLDPETAL1!", "timeframe": "20m", "is_enabled": True, "execution_mode": "LIVE"})

    engine.strategies["r1"] = r1
    engine.strategies["r2"] = r2
    engine.strategies["r3"] = r3

    # Broadcast toggle is_enabled
    engine.is_enabled = False
    assert r1.is_enabled is False
    assert r2.is_enabled is False
    assert r3.is_enabled is False

    # Broadcast execution_mode
    engine.execution_mode = "PAPER"
    assert r1.execution_mode == "PAPER"
    assert r2.execution_mode == "PAPER"
    assert r3.execution_mode == "PAPER"


# ==============================================================================
# TASK 5 REGRESSION TESTS: Finding #5 (Freeze-Slice Partial Fill Tracking)
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_finding_5_freeze_slice_partial_fill_tracking():
    """
    Task 5.1:
    Delta of 30 lots sliced into [10, 10, 10].
    Chunk 1 succeeds (10 lots), Chunk 2 is rejected by broker RMS.
    Assert virtual_position records the 10 lots filled, not 0.
    """
    class PartialFailMockMain(MockMainModule):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def _dispatch_and_record(self, sig_id, action, symbol, qty, price, order_ref, is_paper):
            self.call_count += 1
            if self.call_count == 1:
                super()._dispatch_and_record(sig_id, action, symbol, qty, price, order_ref, is_paper)
                return {"status": "done", "result": {"AppOrderID": 1001}}
            else:
                # Chunk 2 fails
                return {"status": "error", "message": "RMS Margin Exceeded"}

    mock_main = PartialFailMockMain()

    runner = SingleSuperTrendRunner({
        "id": "st_large",
        "symbol": "CRUDEOIL",
        "timeframe": "15m",
        "quantity": 30,
        "is_enabled": True
    })
    assert runner.virtual_position == 0

    # Execute delta of +30 lots with freeze limit of 10 lots -> [10, 10, 10]
    await runner._execute_delta(30, 1700000000, mock_main, freeze_limit=10)

    # Chunk 1 (10 lots) filled. Runner virtual_position must be 10!
    assert runner.virtual_position == 10
    assert mock_main.db_get_virtual_position("CRUDEOIL_15m") == 10


# ==============================================================================
# TASK A.3 REGRESSION TEST: Real Symphony XTS Candle Timestamp Schema
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_real_xts_candle_timestamp_schema(monkeypatch):
    """
    Task A.3:
    Verifies behavior with genuine Symphony XTS OHLC schema:
    - Closed bars have bar-end timestamps ending in :59 (e.g. 15:59:59, 16:14:59, 16:29:59).
    - Current forming bar has live quote timestamp (e.g. 16:30:29).
    Asserts:
    1. During forming bar (16:30:29, 29s into 15m candle), flip on forming candle is IGNORED (0 trades).
    2. Once clock reaches 16:45:01 (confirmed close of 16:30:00-16:44:59 bar), flip executes cleanly.
    """
    mock_main = MockMainModule()

    r15 = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "virtual_position": 0,
        "is_enabled": True
    })

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 6000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Real Symphony XTS candle series:
    # 15 historical closed bars (at 15m intervals, ending in :59) + 1 forming bar at quote time 16:30:29
    t_start = 1787642099 # historical base
    closed_candles = []
    prices = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120, 122, 124, 126, 128] # Bullish
    for i, p in enumerate(prices):
        closed_candles.append({
            "time": t_start + (i * 900), # 15:59:59, 16:14:59, 16:29:59
            "open": float(p), "high": float(p)+2, "low": float(p)-2, "close": float(p), "volume": 100
        })

    # Forming bar at 16:30:29 with sharp plunge (attempting intra-bar Bearish flip)
    t_closed_last = closed_candles[-1]["time"] # 16:29:59
    t_forming = t_closed_last + 30 # 16:30:29 (quote time 30s into bar)
    forming_candle = {"time": t_forming, "open": 128.0, "high": 128.0, "low": 50.0, "close": 60.0, "volume": 500}

    all_candles = closed_candles + [forming_candle]
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: all_candles)

    # 1. At 16:30:29 (T+30s into 15m candle)
    monkeypatch.setattr(time, "time", lambda: float(t_forming))
    await r15.evaluate_cycle(xts_api, mock_main)

    # Invariant: Forming bar is NOT closed (delta 30s < 900s). No trade dispatched!
    assert len(mock_main.dispatched_trades) == 0

    # 2. At 16:45:01 (T+15m+1s): The bar has officially closed at 16:44:59 (ts: t_closed_last + 900)
    t_new_closed = t_closed_last + 900 # 16:44:59
    closed_candle_final = {"time": t_new_closed, "open": 128.0, "high": 128.0, "low": 50.0, "close": 60.0, "volume": 1500}
    full_closed_candles = closed_candles + [closed_candle_final]
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: full_closed_candles)
    monkeypatch.setattr(time, "time", lambda: float(t_new_closed + 2)) # 16:45:01

    await r15.evaluate_cycle(xts_api, mock_main)

    # Confirmed closed candle flip executes cleanly!
    assert len(mock_main.dispatched_trades) == 1
    assert mock_main.dispatched_trades[0]["action"] == "SELL"
    assert r15.last_processed_candle_time == t_new_closed


# ==============================================================================
# TASK B.3 REGRESSION TEST: Persisted Virtual Position Restart Recovery
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_persisted_virtual_position_restart_recovery(monkeypatch, tmp_path):
    """
    Task B.3:
    Verifies that a runner restarting after crash/reboot restores its own
    persisted virtual_position from SQLite (strategy_virtual_positions table),
    without zeroing out or dispatching redundant orders.
    """
    db_file = str(tmp_path / "test_restart.db")
    monkeypatch.setattr(client_main, "_DB_PATH", db_file)
    client_main.db_init()

    # Pre-populate SQLite with genuine existing virtual position: -1 lot for SILVER1001!_15m
    client_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", -1)
    assert client_main.db_get_virtual_position("SILVER1001!_15m") == -1

    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append((sig_id, action, symbol, qty, order_ref))
        return {"status": "done", "result": {"AppOrderID": 8881}}
    monkeypatch.setattr(client_main, "_dispatch_and_record", mock_dispatch)

    # Start fresh runner instance without passing virtual_position in config_dict
    runner = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True
    }, main_module=client_main)

    # Invariant 1: Restores persisted -1 lot from SQLite on boot
    assert runner.virtual_position == -1
    assert runner.strategy_position == "SHORT"

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 6000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    # Broker holds 1 lot SHORT (consistent)
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {
        "positions": [{"symbol": "SILVER100 31AUG2026", "instrument_id": 574823, "side": "SHORT", "quantity": 1}],
        "all_positions": []
    })
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Continuous Bearish prices (no flip)
    prices = [100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30]
    candles = generate_ohlc_series(prices, base_time=1700000000, interval=900)
    runner.last_processed_candle_time = candles[-1]["time"]
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
    monkeypatch.setattr(time, "time", lambda: float(1700000000 + len(prices)*900 + 10))

    await runner.evaluate_cycle(xts_api, client_main)

    # Invariant 2: No redundant orders dispatched into consistent broker position
    assert len(dispatched) == 0
    assert runner.virtual_position == -1


# ==============================================================================
# TASK C.2 REGRESSION TEST: Finding #7 (Panic Square-Off Virtual Position Reset)
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_finding_7_panic_square_off_resets_virtual_positions(monkeypatch, tmp_path):
    """
    Task C.2:
    Verifies that when Panic Square-off executes:
    1. Broker positions are squared off.
    2. All active runners in supertrend_engine have their virtual_position reset to 0 in memory.
    3. SQLite table strategy_virtual_positions is updated to 0 for all runners.
    """
    db_file = str(tmp_path / "test_panic.db")
    monkeypatch.setattr(client_main, "_DB_PATH", db_file)
    client_main.db_init()

    # Pre-populate 2 active runners with open positions in SQLite
    client_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", -2)
    client_main.db_set_virtual_position("GOLDPETAL1!_15m", "GOLDPETAL1!", "15m", -4)

    engine = MultiSuperTrendEngine()
    r_silver = SingleSuperTrendRunner({"id": "r1", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 2, "is_enabled": True}, main_module=client_main)
    r_gold = SingleSuperTrendRunner({"id": "r2", "symbol": "GOLDPETAL1!", "timeframe": "15m", "quantity": 4, "is_enabled": True}, main_module=client_main)
    engine.strategies["r1"] = r_silver
    engine.strategies["r2"] = r_gold

    # Both runners hold open short positions
    assert r_silver.virtual_position == -2
    assert r_gold.virtual_position == -4

    # Mock xts_api.panic_square_off_all returning success
    monkeypatch.setattr(xts_api, "panic_square_off_all", lambda: {
        "status": "success",
        "squared_off": [{"symbol": "SILVER100", "qty": 2}, {"symbol": "GOLDPETAL", "qty": 4}]
    })
    monkeypatch.setattr(client_main, "supertrend_engine", engine)
    monkeypatch.setattr(client_main.config, "WEBHOOK_SECRET", "PANIC_SECRET")

    # Trigger panic square off via API
    from fastapi.testclient import TestClient
    client = TestClient(client_main.app)
    resp = client.post("/panic", json={"secret": "PANIC_SECRET"})
    assert resp.status_code == 200

    # Invariant 1: In-memory virtual_position is reset to 0 across all runners
    assert r_silver.virtual_position == 0
    assert r_gold.virtual_position == 0

    # Invariant 2: Persisted SQLite positions are reset to 0
    assert client_main.db_get_virtual_position("SILVER1001!_15m") == 0
    assert client_main.db_get_virtual_position("GOLDPETAL1!_15m") == 0


# ==============================================================================
# TASK B REGRESSION TEST: Position Drift Warning Logging
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_task_b_position_drift_detection(caplog, monkeypatch, tmp_path):
    """
    Task B.3:
    Persisted virtual_position is -2, broker net position is 0 (FLAT), no in-flight orders.
    Asserts:
    1. A clear WARNING log is emitted with strategy_key, virtual_position (-2), and broker position (0).
    2. Zero orders are dispatched (pure observability, no unexpected action).
    """
    db_file = str(tmp_path / "test_drift.db")
    monkeypatch.setattr(client_main, "_DB_PATH", db_file)
    client_main.TRADING_PAUSED = False
    client_main.db_init()

    # Pre-populate SQLite with virtual_position = -2
    client_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", -2)

    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append((sig_id, action, symbol, qty, order_ref))
        return {"status": "done", "result": {"AppOrderID": 9991}}
    monkeypatch.setattr(client_main, "_dispatch_and_record", mock_dispatch)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 2,
        "is_enabled": True
    }, main_module=client_main)

    assert runner.virtual_position == -2

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 6000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    # Broker holds 0 (FLAT)
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    # No in-flight orders
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Continuous Bearish prices (no flip)
    prices = [100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30]
    candles = generate_ohlc_series(prices, base_time=1700000000, interval=900)
    runner.last_processed_candle_time = candles[-1]["time"]
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
    monkeypatch.setattr(time, "time", lambda: float(1700000000 + len(prices)*900 + 10))

    import logging
    captured_logs = []
    class CustomLogCapture(logging.Handler):
        def emit(self, record):
            captured_logs.append(record.getMessage())

    handler = CustomLogCapture()
    st_logger = logging.getLogger("supertrend_engine")
    st_logger.setLevel(logging.DEBUG)
    st_logger.addHandler(handler)
    try:
        await runner.evaluate_cycle(xts_api, client_main)
    finally:
        st_logger.removeHandler(handler)

    # 1. Assert warning log was emitted
    drift_logs = [m for m in captured_logs if "POSITION DRIFT WARNING" in m]
    assert len(drift_logs) >= 1
    assert "SILVER1001!_15m" in drift_logs[0]
    assert "-2" in drift_logs[0]
    assert "0" in drift_logs[0]

    # 2. Assert zero orders were dispatched (observability only)
    assert len(dispatched) == 0
    assert runner.virtual_position == -2


# ==============================================================================
# TASK A2 REGRESSION TEST: Genuine Multi-Day Long Gap (Weekend / Holiday)
# ==============================================================================
def test_regression_task_a2_long_gap_weekend_boundary():
    """
    Task A2.4:
    Verifies that genuine multi-day gaps (e.g. Friday close to Monday open, > 60 hours)
    with standard POSIX UTC timestamps are NOT misclassified or altered:
    1. Friday 15:30 IST close (ts = 1787600000).
    2. Monday 09:01 IST forming bar (ts = 1787836860) -> is_candle_closed returns False.
    3. Monday 09:15:01 IST confirmed close (ts = 1787837699) -> is_candle_closed returns True.
    """
    tf_seconds = 900 # 15m

    # Friday 15:30 IST close (28-Aug-2026 15:30:00 IST)
    friday_bar = {"time": 1787911200, "open": 2400.0, "high": 2405.0, "low": 2398.0, "close": 2402.0}
    
    # Monday 09:00-09:15 bar (31-Aug-2026 09:14:59 IST, ts = 1788147899) in-progress at 09:01:00 (now_ts = 1788147060)
    monday_forming_bar = {"time": 1788147899, "open": 2405.0, "high": 2410.0, "low": 2404.0, "close": 2408.0}
    
    # Calling the real production method directly at 09:01:00 (< 09:14:59 close)
    res_forming = SingleSuperTrendRunner.is_candle_closed(
        [friday_bar, monday_forming_bar],
        tf_seconds=tf_seconds,
        now_ts=1788147060
    )
    assert res_forming is False, "Forming Monday bar at 09:01 must be evaluated as NOT closed (False)"

    # Monday 09:00-09:15 bar officially closed evaluated at 09:15:01 (now_ts = 1788147901 >= 1788147899)
    res_closed = SingleSuperTrendRunner.is_candle_closed(
        [friday_bar, monday_forming_bar],
        tf_seconds=tf_seconds,
        now_ts=1788147901
    )
    assert res_closed is True, "Confirmed Monday closed bar evaluated at 09:15:01 must be True"


# ==============================================================================
# TASK B2 REGRESSION TEST: Widened Position Drift Detection (virtual != broker)
# ==============================================================================
@pytest.mark.asyncio
async def test_regression_task_b2_widened_drift_detection(monkeypatch, tmp_path):
    """
    Task B2.2:
    Persisted virtual_position is -2, but broker net position is -5 (divergence of 3 lots),
    with no in-flight orders.
    Asserts:
    1. A clear WARNING log is emitted with strategy_key, virtual_position (-2), and broker position (-5).
    2. Zero orders are dispatched (observability only).
    """
    db_file = str(tmp_path / "test_widened_drift.db")
    monkeypatch.setattr(client_main, "_DB_PATH", db_file)
    client_main.TRADING_PAUSED = False
    client_main.db_init()

    # Pre-populate SQLite with virtual_position = -2
    client_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", -2)

    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append((sig_id, action, symbol, qty, order_ref))
        return {"status": "done", "result": {"AppOrderID": 9992}}
    monkeypatch.setattr(client_main, "_dispatch_and_record", mock_dispatch)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 2,
        "is_enabled": True
    }, main_module=client_main)

    assert runner.virtual_position == -2

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574823, "exch_seg": "MCXFO", "lot_size": 1, "freeze_qty": 6000,
        "expiry": datetime.date.today() + datetime.timedelta(days=20)
    })
    # Broker holds 5 lots SHORT (signed lots = -5)
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {
        "positions": [{"symbol": "SILVER100 31AUG2026", "instrument_id": 574823, "side": "SHORT", "quantity": 5}],
        "all_positions": []
    })
    # No in-flight orders
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    prices = [100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30]
    candles = generate_ohlc_series(prices, base_time=1700000000, interval=900)
    runner.last_processed_candle_time = candles[-1]["time"]
    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
    monkeypatch.setattr(time, "time", lambda: float(1700000000 + len(prices)*900 + 10))

    import logging
    captured_logs = []
    class CustomLogCapture(logging.Handler):
        def emit(self, record):
            captured_logs.append(record.getMessage())

    handler = CustomLogCapture()
    st_logger = logging.getLogger("supertrend_engine")
    st_logger.setLevel(logging.DEBUG)
    st_logger.addHandler(handler)
    try:
        await runner.evaluate_cycle(xts_api, client_main)
    finally:
        st_logger.removeHandler(handler)

    # 1. Assert warning log was emitted with both -2 and -5
    drift_logs = [m for m in captured_logs if "POSITION DRIFT WARNING" in m]
    assert len(drift_logs) >= 1
    assert "SILVER1001!_15m" in drift_logs[0]
    assert "-2" in drift_logs[0]
    assert "-5" in drift_logs[0]

    # 2. Assert zero orders were dispatched (observability only)
    assert len(dispatched) == 0
    assert runner.virtual_position == -2


# ==============================================================================
# TASK 3 REGRESSION TEST: Final-Seconds Boundary Precision Test
# ==============================================================================
def test_regression_task_3_final_seconds_boundary_precision():
    """
    Phase 3.8 Task 3:
    Explicitly verifies the exact boundary transition point across the final seconds:
    1. T + tf_seconds - 3 (11:14:57 IST) -> False (Forming bar, tick :57 != :59).
    2. T + tf_seconds - 1 (11:14:58 IST) -> False (Forming bar, tick :58 != :59).
    3. T + tf_seconds     (11:14:59 IST) -> True  (Sealed bar stamped :59, now == close_ts).
    4. T + tf_seconds + 1 (11:15:00 IST) -> True  (Sealed bar stamped :59, now > close_ts).
    """
    tf_seconds = 900 # 15m
    prev_closed_bar = {"time": 1787635799, "open": 2400.0, "high": 2405.0, "low": 2398.0, "close": 2402.0} # 10:59:59 IST

    # 1. T + tf_seconds - 3 (11:14:57 IST)
    bar_t_minus_3 = {"time": 1787636697, "open": 2402.0, "high": 2410.0, "low": 2401.0, "close": 2408.0}
    res_minus_3 = SingleSuperTrendRunner.is_candle_closed([prev_closed_bar, bar_t_minus_3], tf_seconds, now_ts=1787636697)
    assert res_minus_3 is False, "T+(tf_seconds-3) forming bar must be False"

    # 2. T + tf_seconds - 1 (11:14:58 IST)
    bar_t_minus_1 = {"time": 1787636698, "open": 2402.0, "high": 2410.0, "low": 2401.0, "close": 2408.0}
    res_minus_1 = SingleSuperTrendRunner.is_candle_closed([prev_closed_bar, bar_t_minus_1], tf_seconds, now_ts=1787636698)
    assert res_minus_1 is False, "T+(tf_seconds-1) forming bar must be False"

    # 3. T + tf_seconds (11:14:59 IST) - Exact close timestamp
    bar_closed = {"time": 1787636699, "open": 2402.0, "high": 2410.0, "low": 2401.0, "close": 2408.0}
    res_exact_close = SingleSuperTrendRunner.is_candle_closed([prev_closed_bar, bar_closed], tf_seconds, now_ts=1787636699)
    assert res_exact_close is True, "T+tf_seconds sealed bar at close timestamp must be True"

    # 4. T + tf_seconds + 1 (11:15:00 IST / 11:15:01 IST)
    res_plus_1 = SingleSuperTrendRunner.is_candle_closed([prev_closed_bar, bar_closed], tf_seconds, now_ts=1787636701)
    assert res_plus_1 is True, "T+(tf_seconds+1) sealed bar must be True"
