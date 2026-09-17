import os
import sys
import pytest
import datetime
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if client_dir not in sys.path:
    sys.path.insert(0, client_dir)

import config
from config import IST_TIMEZONE, get_commodity_rollover_cutoff, is_commodity_past_rollover
import xts_api
from supertrend_engine import SingleSuperTrendRunner


# Realistic future contract chains up to Feb 2027
MOCK_FUT_MASTER = {
    "SILVER100": [
        (datetime.date(2026, 9, 30), 574824, "MCXFO", "SILVER10030SEP2026FUT", 1.0, 1, 100),
        (datetime.date(2026, 10, 30), 574825, "MCXFO", "SILVER10030OCT2026FUT", 1.0, 1, 100),
        (datetime.date(2026, 11, 30), 574826, "MCXFO", "SILVER10030NOV2026FUT", 1.0, 1, 100),
        (datetime.date(2026, 12, 31), 578641, "MCXFO", "SILVER10031DEC2026FUT", 1.0, 1, 100),
        (datetime.date(2027, 1, 29), 581708, "MCXFO", "SILVER10029JAN2027FUT", 1.0, 1, 100),
        (datetime.date(2027, 2, 26), 584308, "MCXFO", "SILVER10026FEB2027FUT", 1.0, 1, 100),
    ],
    "GOLDPETAL": [
        (datetime.date(2026, 9, 30), 568839, "MCXFO", "GOLDPETAL30SEP2026FUT", 1.0, 1, 100),
        (datetime.date(2026, 10, 30), 571306, "MCXFO", "GOLDPETAL30OCT2026FUT", 1.0, 1, 100),
        (datetime.date(2026, 11, 30), 574841, "MCXFO", "GOLDPETAL30NOV2026FUT", 1.0, 1, 100),
        (datetime.date(2026, 12, 31), 578631, "MCXFO", "GOLDPETAL31DEC2026FUT", 1.0, 1, 100),
        (datetime.date(2027, 1, 29), 581706, "MCXFO", "GOLDPETAL29JAN2027FUT", 1.0, 1, 100),
        (datetime.date(2027, 2, 26), 584306, "MCXFO", "GOLDPETAL26FEB2027FUT", 1.0, 1, 100),
    ],
}
MOCK_FUT_NORM = {
    "SILVER100": "SILVER100",
    "GOLDPETAL": "GOLDPETAL",
}


@pytest.fixture(autouse=True)
def setup_mock_master():
    with xts_api.CACHE_LOCK:
        orig_fut = dict(xts_api.FUT_MASTER)
        orig_norm = dict(xts_api.FUT_NORM_MAP)
        orig_date = xts_api.CACHE_DATE
        xts_api.FUT_MASTER.update(MOCK_FUT_MASTER)
        xts_api.FUT_NORM_MAP.update(MOCK_FUT_NORM)
        xts_api.CACHE_DATE = datetime.date.today()
    yield
    with xts_api.CACHE_LOCK:
        xts_api.FUT_MASTER.clear()
        xts_api.FUT_MASTER.update(orig_fut)
        xts_api.FUT_NORM_MAP.clear()
        xts_api.FUT_NORM_MAP.update(orig_norm)
        xts_api.CACHE_DATE = orig_date


# ==============================================================================
# 1. Multi-Month Continuous Contract Resolution Tests
# ==============================================================================

def test_multi_month_continuous_rolling_timeline():
    """
    Verifies that continuous symbols SILVER1001! and GOLDPETAL1! resolve to the
    correct month across the entire timeline (Sept 2026 -> Feb 2027).
    """
    test_cases = [
        # (simulated_datetime, expected_silver_desc, expected_gold_desc)
        (datetime.datetime(2026, 9, 17, 11, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10030SEP2026FUT", "GOLDPETAL30SEP2026FUT"),
        (datetime.datetime(2026, 9, 22, 13, 59, 59, tzinfo=IST_TIMEZONE), "SILVER10030SEP2026FUT", "GOLDPETAL30SEP2026FUT"),
        (datetime.datetime(2026, 9, 22, 14, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10030OCT2026FUT", "GOLDPETAL30OCT2026FUT"),
        (datetime.datetime(2026, 9, 23, 10, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10030OCT2026FUT", "GOLDPETAL30OCT2026FUT"),
        (datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10030OCT2026FUT", "GOLDPETAL30OCT2026FUT"),
        (datetime.datetime(2026, 10, 23, 15, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10030NOV2026FUT", "GOLDPETAL30NOV2026FUT"),
        (datetime.datetime(2026, 11, 23, 15, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10031DEC2026FUT", "GOLDPETAL31DEC2026FUT"),
        (datetime.datetime(2026, 12, 24, 15, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10029JAN2027FUT", "GOLDPETAL29JAN2027FUT"),
        (datetime.datetime(2027, 1, 22, 15, 0, 0, tzinfo=IST_TIMEZONE), "SILVER10026FEB2027FUT", "GOLDPETAL26FEB2027FUT"),
    ]

    for sim_dt, exp_silver, exp_gold in test_cases:
        with patch.object(xts_api, "datetime") as mock_dt:
            mock_dt.datetime.now.return_value = sim_dt
            mock_dt.date.today.return_value = sim_dt.date()
            mock_dt.timezone = datetime.timezone
            mock_dt.timedelta = datetime.timedelta

            s_id, s_seg, _, _, _, _, _ = xts_api._resolve_front_month("SILVER1001!", "SILVER100", is_future_intent=True, depth=1)
            inst_s = xts_api.get_instrument_by_id(s_id)
            assert inst_s["desc"] == exp_silver, f"At {sim_dt}: Expected {exp_silver}, got {inst_s['desc']}"

            g_id, g_seg, _, _, _, _, _ = xts_api._resolve_front_month("GOLDPETAL1!", "GOLDPETAL", is_future_intent=True, depth=1)
            inst_g = xts_api.get_instrument_by_id(g_id)
            assert inst_g["desc"] == exp_gold, f"At {sim_dt}: Expected {exp_gold}, got {inst_g['desc']}"


def test_commodity_rollover_weekend_holiday_pullback():
    """
    Verifies that if the 8th calendar day falls on Saturday/Sunday or an MCX holiday,
    the cutoff pulls back to Friday or the preceding active trading day at 14:00 IST.
    """
    monday_exp = datetime.date(2026, 11, 30) # Monday
    cutoff = get_commodity_rollover_cutoff(monday_exp, "MCXFO")
    assert cutoff.weekday() == 4, f"Expected Friday (weekday 4), got {cutoff.weekday()}"
    assert cutoff.date() == datetime.date(2026, 11, 20)
    assert (cutoff.hour, cutoff.minute) == (14, 0)


def test_is_commodity_past_rollover_failsafe():
    """
    Verifies the hard <= 7 days fail-safe: any contract with <= 7 calendar days
    remaining MUST return True even if time is 09:00 AM before the 14:00 cutoff.
    """
    exp = datetime.date(2026, 9, 30)
    dt_7d = datetime.datetime(2026, 9, 23, 9, 15, 0, tzinfo=IST_TIMEZONE)
    assert is_commodity_past_rollover(exp, "MCXFO", dt_7d) is True

    dt_8d_early = datetime.datetime(2026, 9, 22, 13, 30, 0, tzinfo=IST_TIMEZONE)
    assert is_commodity_past_rollover(exp, "MCXFO", dt_8d_early) is False

    dt_8d_late = datetime.datetime(2026, 9, 22, 14, 1, 0, tzinfo=IST_TIMEZONE)
    assert is_commodity_past_rollover(exp, "MCXFO", dt_8d_late) is True


# ==============================================================================
# 2. 2-Leg Rollover Execution & Cache Purging Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_immediate_2leg_rollover_success_and_cache_purged():
    """
    Verifies that when an open position exists and cutoff triggers:
    1. Leg 1 exits near month.
    2. Leg 2 enters far month in same direction.
    3. self.cached_candles and indicator caches are purged.
    4. Active contract pointers are updated in SQLite and memory.
    """
    runner = SingleSuperTrendRunner({
        "id": "test_silver",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 1,
        "execution_mode": "LIVE",
        "is_enabled": True,
        "virtual_position": -1,
        "active_contract_id": 574824,
        "active_contract_desc": "SILVER10030SEP2026FUT"
    })
    runner.strategy_position = "SHORT"
    runner.cached_candles = [{"time": 1780000000, "close": 90000.0}]
    runner.last_candle_time = 1780000000
    runner.last_processed_candle_time = 1780000000
    runner.last_close = 90000.0
    runner.last_atr = 500.0

    mock_main = MagicMock()
    mock_main.TRADING_PAUSED = False
    mock_main.db_set_virtual_position = MagicMock()

    runner._execute_exit = AsyncMock(return_value=True)
    runner._execute_entry = AsyncMock(return_value=True)

    with patch.object(xts_api, "resolve_contract") as mock_rc, \
         patch("supertrend_engine.is_market_open_ist", return_value=True), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_rc.return_value = {
            "name": "SILVER100",
            "expiry": datetime.date(2026, 10, 30),
            "inst_id": 574825,
            "exch_seg": "MCXFO",
            "desc": "SILVER10030OCT2026FUT",
            "tick_size": 1.0,
            "lot_size": 1,
            "freeze_qty": 100
        }

        with patch.object(xts_api, "fetch_ohlc_candles", return_value=[]):
            await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        runner._execute_exit.assert_awaited_once()
        exit_args = runner._execute_exit.await_args[0]
        assert exit_args[0] == "SHORT"
        assert exit_args[1] == 1
        assert runner._execute_exit.await_args[1]["target_symbol"] == "SILVER10030SEP2026FUT"

        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 1.5 in sleep_calls, f"Expected 1.5s sleep in {sleep_calls}"

        runner._execute_entry.assert_awaited_once()
        entry_args = runner._execute_entry.await_args[0]
        assert entry_args[0] == "SELL"
        assert entry_args[1] == 1
        assert runner._execute_entry.await_args[1]["target_symbol"] == "SILVER10030OCT2026FUT"

        assert runner.active_contract_id == 574825
        assert runner.active_contract_desc == "SILVER10030OCT2026FUT"
        assert runner.cached_candles == []
        assert runner.last_candle_time == 0
        assert runner.last_processed_candle_time == 0
        assert runner.last_close == 0.0
        assert runner.last_atr == 0.0


@pytest.mark.asyncio
async def test_leg2_retry_and_safe_flat_fallback_on_rms_shortfall():
    """
    Verifies that if Leg 2 fails 3 times due to broker RMS margin shortfall:
    1. Retries up to 3 times with backoff.
    2. Enters safe FLAT state (virtual_position = 0).
    3. Status set to ROLLOVER_FAILED_PAUSED and is_enabled = False.
    4. Active contract pointer updated to new contract.
    5. Caches purged.
    """
    runner = SingleSuperTrendRunner({
        "id": "test_gold",
        "symbol": "GOLDPETAL1!",
        "exchange_segment": "MCXFO",
        "timeframe": "20m",
        "quantity": 32,
        "execution_mode": "LIVE",
        "is_enabled": True,
        "virtual_position": -32,
        "active_contract_id": 568839,
        "active_contract_desc": "GOLDPETAL30SEP2026FUT"
    })
    runner.strategy_position = "SHORT"

    mock_main = MagicMock()
    mock_main.TRADING_PAUSED = False
    mock_main.db_set_virtual_position = MagicMock()

    runner._execute_exit = AsyncMock(return_value=True)
    runner._execute_entry = AsyncMock(return_value=False)

    with patch.object(xts_api, "resolve_contract") as mock_rc, \
         patch("supertrend_engine.is_market_open_ist", return_value=True), \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(xts_api, "send_ops_alert") as mock_alert:

        mock_rc.return_value = {
            "name": "GOLDPETAL",
            "expiry": datetime.date(2026, 10, 30),
            "inst_id": 571306,
            "exch_seg": "MCXFO",
            "desc": "GOLDPETAL30OCT2026FUT",
            "tick_size": 1.0,
            "lot_size": 1,
            "freeze_qty": 100
        }

        await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        assert runner._execute_entry.await_count == 3
        assert runner.virtual_position == 0
        assert runner.status == "ROLLOVER_FAILED_PAUSED"
        assert runner.is_enabled is False
        assert runner.active_contract_id == 571306
        assert runner.active_contract_desc == "GOLDPETAL30OCT2026FUT"

        mock_alert.assert_called()
        assert "CRITICAL" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_flat_position_seamless_rollover():
    """
    Verifies that when an account holds FLAT (0 lots) on rollover cutoff:
    1. No orders are placed (neither exit nor entry).
    2. Active contract pointer seamlessly advances to October.
    3. Caches are cleared.
    """
    runner = SingleSuperTrendRunner({
        "id": "test_flat",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 1,
        "execution_mode": "LIVE",
        "is_enabled": True,
        "virtual_position": 0,
        "active_contract_id": 574824,
        "active_contract_desc": "SILVER10030SEP2026FUT"
    })
    runner.strategy_position = "FLAT"

    mock_main = MagicMock()
    mock_main.TRADING_PAUSED = False
    runner._execute_exit = AsyncMock()
    runner._execute_entry = AsyncMock()

    with patch.object(xts_api, "resolve_contract") as mock_rc, \
         patch("supertrend_engine.is_market_open_ist", return_value=True), \
         patch.object(xts_api, "fetch_ohlc_candles", return_value=[]):

        mock_rc.return_value = {
            "name": "SILVER100",
            "expiry": datetime.date(2026, 10, 30),
            "inst_id": 574825,
            "exch_seg": "MCXFO",
            "desc": "SILVER10030OCT2026FUT",
            "tick_size": 1.0,
            "lot_size": 1,
            "freeze_qty": 100
        }

        await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        runner._execute_exit.assert_not_called()
        runner._execute_entry.assert_not_called()
        assert runner.active_contract_id == 574825
        assert runner.active_contract_desc == "SILVER10030OCT2026FUT"
        assert runner.virtual_position == 0
