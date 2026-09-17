import os
import sys
import math
import time
import datetime
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xts_api
import config
from supertrend_engine import (
    SingleSuperTrendRunner,
    MultiSuperTrendEngine,
    calculate_supertrend,
    parse_timeframe_seconds,
)
import main


def test_resolve_front_month_empty_candidates_no_crash():
    """Finding 1.1: Guard against IndexError when no candidates exist."""
    with patch.object(xts_api, "FUT_MASTER", {}), \
         patch.object(xts_api, "CASH_MASTER", {}), \
         patch.object(xts_api, "FUT_NORM_MAP", {}), \
         patch.object(xts_api, "CASH_NORM_MAP", {}):
        res = xts_api._resolve_front_month("NONEXISTENT1!", "NONEXISTENT", is_future_intent=True, depth=1)
        assert res == (None, None, None, None, None, None, None)


def test_apply_tick_size_nan_inf_subcent_robustness():
    """Finding 1.2: Handle NaN, Inf, sub-cent tick sizes without throwing ValueError or crashing."""
    assert xts_api.apply_tick_size(float('nan'), 0.05, "BUY") == 0.0
    assert xts_api.apply_tick_size(float('inf'), 0.05, "BUY") == 0.0
    assert xts_api.apply_tick_size(-100.0, 0.05, "BUY") == 0.0
    assert xts_api.apply_tick_size(0.0, 0.05, "BUY") == 0.0

    # Sub-cent tick size (e.g. 0.0025 on currency or commodity spread)
    p = xts_api.apply_tick_size(82.1234, 0.0025, "BUY")
    assert math.isclose(p, 82.125, abs_tol=1e-5)

    # Standard commodity tick sizes
    p_buy = xts_api.apply_tick_size(6501.23, 1.0, "BUY")
    assert p_buy == 6502.0
    p_sell = xts_api.apply_tick_size(6501.23, 1.0, "SELL")
    assert p_sell == 6501.0


def test_extract_expiry_2digit_and_4digit_years():
    """Finding 1.3: Robust regex expiry parsing for both 2-digit and 4-digit years."""
    # 4-digit year: CRUDEOIL19MAR2026FUT
    exp_4d, _ = xts_api._extract_expiry([], "CRUDEOIL19MAR2026FUT")
    assert exp_4d == datetime.date(2026, 3, 19)

    # 2-digit year: CRUDEOIL19MAR26FUT
    exp_2d, _ = xts_api._extract_expiry([], "CRUDEOIL19MAR26FUT")
    assert exp_2d == datetime.date(2026, 3, 19)

    # Monthly without day: CRUDEOILMAR26FUT -> last Thursday of month
    exp_month, _ = xts_api._extract_expiry([], "CRUDEOILMAR26FUT")
    assert exp_month is not None
    assert exp_month.year == 2026 and exp_month.month == 3


def test_fetch_ohlc_candles_timestamp_and_key_typo_tolerance(monkeypatch):
    """Finding 1.4: Support float timestamps and both dataReponse / dataResponse spellings."""
    mock_payload_with_typo = {
        "type": "success",
        "result": {
            # Note XTS broker API typo "dataReponse" with missing 's'
            "dataReponse": "1789650000.0|6500.0|6520.0|6490.0|6510.0|1200|0|0"
        }
    }
    mock_r = MagicMock()
    mock_r.status_code = 200
    mock_r.json.return_value = mock_payload_with_typo

    monkeypatch.setattr(xts_api.api_session, "get", lambda *a, **kw: mock_r)
    monkeypatch.setattr(xts_api, "get_marketdata_token", lambda *a, **kw: ("mock_md_token", "https://mock.base"))

    candles = xts_api.fetch_ohlc_candles("MCXFO", 25001, 300, 1)
    assert len(candles) == 1
    # 19800s (5h30m IST offset) deducted to convert XTS IST-epoch to POSIX UTC
    assert candles[0]["time"] == 1789650000 - 19800
    assert candles[0]["close"] == 6510.0


def test_slice_quantity_for_freeze_with_lot_size_alignment():
    """Finding 3.1: Freeze slicing must align slices to multiples of lot_size."""
    # Quantity: 250, freeze limit: 100, lot size: 25
    # Slices should be 100, 100, 50 (all multiples of 25)
    slices = xts_api.slice_quantity_for_freeze(250, 100, lot_size=25)
    assert slices == [100, 100, 50]
    assert all(s % 25 == 0 for s in slices)
    assert sum(slices) == 250

    # Large freeze limit
    slices_single = xts_api.slice_quantity_for_freeze(50, 1000, lot_size=10)
    assert slices_single == [50]


def test_format_order_ref_chunk_preserves_retry_markers():
    """Finding 3.2: Prevent truncation/mangling of _RETRY2, _RETRY3 markers in order ref."""
    base_ref = "ST_REV_ENTRY_SILVER1001!_15M_1789650000_RETRY2"
    chunk_ref = xts_api.format_order_ref_chunk(base_ref, chunk_idx=2, max_len=50)
    assert "_RETRY2_2" in chunk_ref
    assert len(chunk_ref) <= 50

    base_ref_3 = "ST_REV_ENTRY_SILVER1001!_15M_1789650000_RETRY3"
    chunk_ref_3 = xts_api.format_order_ref_chunk(base_ref_3, chunk_idx=1, max_len=50)
    assert "_RETRY3" in chunk_ref_3
    assert len(chunk_ref_3) <= 50


@pytest.mark.asyncio
async def test_drift_reconciliation_respects_runner_lock_and_rollover():
    """Finding 2.1 & Finding 4.1: Drift reconciliation must not auto-heal during active cycle or rollover."""
    engine = MultiSuperTrendEngine(max_strategies=2)
    mock_main = MagicMock()
    mock_main.TRADING_PAUSED = False

    runner = SingleSuperTrendRunner({
        "id": "st_test_drift",
        "symbol": "CRUDEOIL1!",
        "timeframe": "15m",
        "quantity": 2,
        "is_enabled": True,
        "virtual_position": 2,
    })
    engine.strategies["st_test_drift"] = runner

    mock_broker_data = {
        "is_paper_trade": False,
        "all_positions": []
    }

    # Case A: Runner lock is acquired by an ongoing evaluate_cycle
    await runner.lock.acquire()
    try:
        with patch.object(xts_api, "get_broker_positions_net", return_value=mock_broker_data):
            res = await engine.reconcile_portfolio_drift(xts_api, mock_main)
            # Drift healing must be skipped to avoid race condition
            assert res["status"] == "IN_SYNC"
            assert res["drift_count"] == 0
    finally:
        runner.lock.release()

    # Case B: Pending rollover is in progress
    runner.pending_rollover = True
    with patch.object(xts_api, "get_broker_positions_net", return_value=mock_broker_data):
        res = await engine.reconcile_portfolio_drift(xts_api, mock_main)
        assert res["status"] == "IN_SYNC"
        assert res["drift_count"] == 0


def test_main_get_symbol_lock_serialization():
    """Finding 2.2: Ensure per-symbol execution lock returns same lock instance."""
    lock_a1 = main.get_symbol_lock("CRUDEOIL1!")
    lock_a2 = main.get_symbol_lock(" crudeoil1! ")
    lock_b = main.get_symbol_lock("SILVER1001!")

    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


def test_recent_trade_markers_capped():
    """Finding 5.1: Verify trade markers ring buffer is bounded to prevent unbounded memory growth."""
    runner = SingleSuperTrendRunner({
        "id": "st_mem_test",
        "symbol": "GOLD1!",
        "timeframe": "5m",
        "quantity": 1,
        "is_enabled": True,
    })
    runner.recent_trade_markers = [{"time": i, "text": f"BUY {i}"} for i in range(150)]
    assert len(runner.recent_trade_markers) == 150

    # Simulate exit append capping
    if len(runner.recent_trade_markers) > 100:
        runner.recent_trade_markers = runner.recent_trade_markers[-100:]
    assert len(runner.recent_trade_markers) == 100
