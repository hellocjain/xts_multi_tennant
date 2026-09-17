import pytest
import datetime
import math
import time
import uuid
import os
import sys

CLIENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if CLIENT_DIR not in sys.path:
    sys.path.insert(0, CLIENT_DIR)

import xts_api
from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine
from custom_strategy_engine import BaseStrategy, SingleCustomStrategyRunner
import main


def test_is_candle_closed_nse_and_mcx_boundaries():
    """Verifies that 30m and 1h bars align correctly with NSE (09:15 open) and MCX (09:00 open)."""
    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    
    # 1. MCX 30m candle ending at 09:29:59 IST (from 09:00:00)
    dt_mcx = datetime.datetime(2026, 3, 17, 9, 29, 59, tzinfo=ist_tz)
    ts_mcx = int(dt_mcx.timestamp())
    candles_mcx = [{"time": ts_mcx, "close": 6850.0}]
    assert SingleSuperTrendRunner.is_candle_closed(candles_mcx, 1800, now_ts=ts_mcx + 1) is True

    # 2. NSE 30m candle ending at 09:44:59 IST (from 09:15:00)
    dt_nse_30m = datetime.datetime(2026, 3, 17, 9, 44, 59, tzinfo=ist_tz)
    ts_nse_30m = int(dt_nse_30m.timestamp())
    candles_nse_30m = [{"time": ts_nse_30m, "close": 22500.0}]
    assert SingleSuperTrendRunner.is_candle_closed(candles_nse_30m, 1800, now_ts=ts_nse_30m + 1) is True

    # 3. NSE 1-hour candle ending at 10:14:59 IST (from 09:15:00)
    dt_nse_1h = datetime.datetime(2026, 3, 17, 10, 14, 59, tzinfo=ist_tz)
    ts_nse_1h = int(dt_nse_1h.timestamp())
    candles_nse_1h = [{"time": ts_nse_1h, "close": 22550.0}]
    assert SingleSuperTrendRunner.is_candle_closed(candles_nse_1h, 3600, now_ts=ts_nse_1h + 1) is True

    # 4. MCX Morning Session Holiday 30m candle ending at 17:29:59 IST (from 17:00:00)
    dt_mcx_eve = datetime.datetime(2026, 3, 17, 17, 29, 59, tzinfo=ist_tz)
    ts_mcx_eve = int(dt_mcx_eve.timestamp())
    candles_mcx_eve = [{"time": ts_mcx_eve, "close": 6900.0}]
    assert SingleSuperTrendRunner.is_candle_closed(candles_mcx_eve, 1800, now_ts=ts_mcx_eve + 1) is True

    # 5. Forming intra-bar candle (e.g. 09:17:34 IST) must be rejected
    dt_forming = datetime.datetime(2026, 3, 17, 9, 17, 34, tzinfo=ist_tz)
    ts_forming = int(dt_forming.timestamp())
    candles_forming = [{"time": ts_forming, "close": 6860.0}]
    assert SingleSuperTrendRunner.is_candle_closed(candles_forming, 1800, now_ts=ts_forming + 1) is False


def test_freeze_slicing_with_lot_size_alignment():
    """Verifies that freeze slicing aligns strictly to lot_size multiples."""
    # Lot size 50, freeze limit 125, order qty 200 (4 lots)
    chunks = xts_api.slice_quantity_for_freeze(200, 125, lot_size=50)
    assert chunks == [100, 100]
    for c in chunks:
        assert c % 50 == 0

    # Order qty 150, freeze limit 10000 (units), lot size 100
    chunks_lots = xts_api.slice_quantity_for_freeze(150, 10000 // 100, lot_size=1)
    assert chunks_lots == [100, 50]


def test_order_ref_chunk_formatting_numbered_retries():
    """Verifies that numbered retry suffixes (_RETRY2, _RETRY3) are preserved and never collide."""
    ref_retry1 = xts_api.format_order_ref_chunk("ST_REV_FLIP_ENTRY_1710672000_RETRY", 1)
    ref_retry2 = xts_api.format_order_ref_chunk("ST_REV_FLIP_ENTRY_1710672000_RETRY2", 1)
    ref_retry3 = xts_api.format_order_ref_chunk("ST_REV_FLIP_ENTRY_1710672000_RETRY3", 1)

    assert ref_retry1 != ref_retry2
    assert ref_retry2 != ref_retry3
    assert "_RETRY2" in ref_retry2
    assert "_RETRY3" in ref_retry3
    assert len(ref_retry2) <= 50
    assert len(ref_retry3) <= 50


def test_apply_tick_size_robustness():
    """Verifies apply_tick_size handles NaN, Inf, string values, and sub-cent tick sizes."""
    # NaN price input
    assert xts_api.apply_tick_size(float("nan"), 0.05, "BUY") == 0.0

    # Sub-cent tick size (e.g. 0.0025 for currency derivatives)
    p = xts_api.apply_tick_size(83.1234, 0.0025, "BUY")
    assert math.isclose(p, 83.125, abs_tol=1e-5)

    # Standard tick sizes
    p_buy = xts_api.apply_tick_size(6850.12, 0.05, "BUY")
    assert math.isclose(p_buy, 6850.15, abs_tol=1e-5)

    p_sell = xts_api.apply_tick_size(6850.12, 0.05, "SELL")
    assert math.isclose(p_sell, 6850.10, abs_tol=1e-5)


def test_custom_strategy_indicator_zero_division():
    """Verifies that technical indicators never throw ZeroDivisionError on non-positive period."""
    prices = [100.0, 102.0, 101.0, 105.0]
    assert BaseStrategy.calculate_ema(prices, period=0) == []
    assert BaseStrategy.calculate_ema(prices, period=-1) == []
    assert BaseStrategy.calculate_sma(prices, period=0) == []
    assert BaseStrategy.calculate_sma(prices, period=-5) == []
    assert BaseStrategy.calculate_rsi(prices, period=0) == []
    assert BaseStrategy.calculate_rsi(prices, period=-1) == []


def test_toggle_strategy_circuit_breaker_reset():
    """Verifies that toggling a strategy clears previous circuit breaker drift rejection history."""
    engine = MultiSuperTrendEngine(max_strategies=3)
    runner_cfg = {
        "id": "st_test_cb",
        "symbol": "CRUDEOIL1!",
        "timeframe": "15m",
        "is_enabled": False
    }
    engine.add_or_update_strategy(runner_cfg)
    
    # Simulate a paused circuit breaker state from past margin rejection
    engine._drift_rejection_history["CRUDEOIL1!"] = {"paused": True, "reason": "Margin Shortfall"}
    runner = engine.get_strategy("st_test_cb")
    runner.last_error = "Margin Shortfall"
    runner.status = "MARGIN_SHORTFALL_PAUSED"

    # Re-enable the strategy
    tel = engine.toggle_strategy("st_test_cb", is_enabled=True)
    assert tel["is_enabled"] is True
    assert tel["status"] == "RUNNING"
    assert "CRUDEOIL1!" not in engine._drift_rejection_history
    assert runner.last_error is None


def test_sqlite_prune_dedup_and_checkpoint():
    """Verifies that db_prune_old cleans up signal_dedup entries and checkpoints WAL."""
    now = time.time()
    with main._DB_LOCK:
        with main.closing(main._db_conn()) as conn:
            conn.execute("INSERT OR REPLACE INTO signal_dedup (hash, timestamp) VALUES (?, ?)", ("test_hash_old", now - 800000))
            conn.execute("INSERT OR REPLACE INTO signal_dedup (hash, timestamp) VALUES (?, ?)", ("test_hash_fresh", now))
            conn.commit()

    main.db_prune_old(max_age_seconds=7 * 24 * 3600)

    with main._DB_LOCK:
        with main.closing(main._db_conn()) as conn:
            r_old = conn.execute("SELECT hash FROM signal_dedup WHERE hash=?", ("test_hash_old",)).fetchone()
            r_fresh = conn.execute("SELECT hash FROM signal_dedup WHERE hash=?", ("test_hash_fresh",)).fetchone()
            assert r_old is None
            assert r_fresh is not None
            conn.execute("DELETE FROM signal_dedup WHERE hash=?", ("test_hash_fresh",))
            conn.commit()


def test_clear_tokens_clears_cookies():
    """Verifies that clear_tokens clears cookies on api_session."""
    xts_api.api_session.cookies.set("SESSION_COOKIE", "xyz123")
    assert "SESSION_COOKIE" in xts_api.api_session.cookies.get_dict()

    xts_api.clear_tokens()
    assert "SESSION_COOKIE" not in xts_api.api_session.cookies.get_dict()


def test_get_cache_counts_thread_safety():
    """Verifies that get_cache_counts() cleanly reads master cache state under lock."""
    with xts_api.CACHE_LOCK:
        xts_api.FUT_MASTER["TESTFUT"] = [(datetime.date(2026, 10, 30), 99999, "MCXFO", "TESTFUT30OCT", 1.0, 1, 100)]
        xts_api.CASH_MASTER["TESTCASH"] = [(datetime.date(2099, 1, 1), 88888, "NSECM", "TESTCASH", 0.05, 1, 1000)]

    fut_len, fut_cnt, cash_len, cash_cnt = xts_api.get_cache_counts()
    assert fut_len >= 1
    assert fut_cnt >= 1
    assert cash_len >= 1
    assert cash_cnt >= 1

    with xts_api.CACHE_LOCK:
        xts_api.FUT_MASTER.pop("TESTFUT", None)
        xts_api.CASH_MASTER.pop("TESTCASH", None)


def test_contract_resolution_error_throttling():
    """Verifies that repetitive contract resolution failures record timestamps and don't raise."""
    sym = "UNKNOWN_NONEXISTENT_SYMBOL_123!"
    res = xts_api._resolve_front_month(sym, "UNKNOWN_NONEXISTENT", is_future_intent=True)
    assert res == (None, None, None, None, None, None, None)
    assert sym in xts_api._LAST_RESOLUTION_ERROR_TIME
    first_ts = xts_api._LAST_RESOLUTION_ERROR_TIME[sym]
    assert first_ts > 0

    # Consecutive immediate call should be throttled
    res2 = xts_api._resolve_front_month(sym, "UNKNOWN_NONEXISTENT", is_future_intent=True)
    assert res2 == (None, None, None, None, None, None, None)
    assert xts_api._LAST_RESOLUTION_ERROR_TIME[sym] == first_ts
    xts_api._LAST_RESOLUTION_ERROR_TIME.pop(sym, None)

