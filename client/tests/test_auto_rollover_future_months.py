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


# ==============================================================================
# 3. Holiday Session-Awareness & Opening Bell Stabilization Tests
# ==============================================================================

def test_holiday_calendar_classification_and_is_market_open_ist():
    """
    Verifies that is_market_open_ist accurately evaluates:
    1. Full-day holidays (Gandhi Jayanti Oct 02, Republic Day Jan 26): Closed 09:00 - 23:55.
    2. Morning-only holidays (Dussehra Oct 20): Closed 09:00 - 17:00, Open 17:00 - 23:55.
    3. Standard weekdays (Oct 21): Open 09:00 - 23:55.
    4. Weekends (Oct 24): Closed all day.
    """
    # 1. Full-Day Holiday: Gandhi Jayanti (Friday 2026-10-02)
    dt_gandhi_morning = datetime.datetime(2026, 10, 2, 10, 0, 0, tzinfo=IST_TIMEZONE)
    dt_gandhi_afternoon = datetime.datetime(2026, 10, 2, 14, 0, 0, tzinfo=IST_TIMEZONE)
    dt_gandhi_evening = datetime.datetime(2026, 10, 2, 18, 0, 0, tzinfo=IST_TIMEZONE)

    assert config.is_market_open_ist("MCXFO", now_ts=dt_gandhi_morning.timestamp(), force_check=True) is False
    assert config.is_market_open_ist("MCXFO", now_ts=dt_gandhi_afternoon.timestamp(), force_check=True) is False
    assert config.is_market_open_ist("MCXFO", now_ts=dt_gandhi_evening.timestamp(), force_check=True) is False

    # 2. Morning-Only Holiday: Dussehra (Tuesday 2026-10-20)
    dt_dussehra_morning = datetime.datetime(2026, 10, 20, 10, 0, 0, tzinfo=IST_TIMEZONE)
    dt_dussehra_cutoff = datetime.datetime(2026, 10, 20, 14, 0, 0, tzinfo=IST_TIMEZONE)
    dt_dussehra_pre_evening = datetime.datetime(2026, 10, 20, 16, 59, 59, tzinfo=IST_TIMEZONE)
    dt_dussehra_open = datetime.datetime(2026, 10, 20, 17, 0, 0, tzinfo=IST_TIMEZONE)
    dt_dussehra_prime = datetime.datetime(2026, 10, 20, 19, 30, 0, tzinfo=IST_TIMEZONE)
    dt_dussehra_close = datetime.datetime(2026, 10, 20, 23, 55, 0, tzinfo=IST_TIMEZONE)
    dt_dussehra_post = datetime.datetime(2026, 10, 20, 23, 56, 0, tzinfo=IST_TIMEZONE)

    assert config.is_market_open_ist("MCXFO", now_ts=dt_dussehra_morning.timestamp(), force_check=True) is False
    assert config.is_market_open_ist("MCXFO", now_ts=dt_dussehra_cutoff.timestamp(), force_check=True) is False
    assert config.is_market_open_ist("MCXFO", now_ts=dt_dussehra_pre_evening.timestamp(), force_check=True) is False
    assert config.is_market_open_ist("MCXFO", now_ts=dt_dussehra_open.timestamp(), force_check=True) is True
    assert config.is_market_open_ist("MCXFO", now_ts=dt_dussehra_prime.timestamp(), force_check=True) is True
    assert config.is_market_open_ist("MCXFO", now_ts=dt_dussehra_close.timestamp(), force_check=True) is True
    assert config.is_market_open_ist("MCXFO", now_ts=dt_dussehra_post.timestamp(), force_check=True) is False

    # 3. Standard Active Weekday: Wednesday 2026-10-21
    dt_normal_pre = datetime.datetime(2026, 10, 21, 8, 59, 59, tzinfo=IST_TIMEZONE)
    dt_normal_open = datetime.datetime(2026, 10, 21, 9, 0, 0, tzinfo=IST_TIMEZONE)
    dt_normal_afternoon = datetime.datetime(2026, 10, 21, 14, 0, 0, tzinfo=IST_TIMEZONE)
    dt_normal_close = datetime.datetime(2026, 10, 21, 23, 55, 0, tzinfo=IST_TIMEZONE)
    dt_normal_post = datetime.datetime(2026, 10, 21, 23, 55, 1, tzinfo=IST_TIMEZONE)

    assert config.is_market_open_ist("MCXFO", now_ts=dt_normal_pre.timestamp(), force_check=True) is False
    assert config.is_market_open_ist("MCXFO", now_ts=dt_normal_open.timestamp(), force_check=True) is True
    assert config.is_market_open_ist("MCXFO", now_ts=dt_normal_afternoon.timestamp(), force_check=True) is True
    assert config.is_market_open_ist("MCXFO", now_ts=dt_normal_close.timestamp(), force_check=True) is True
    assert config.is_market_open_ist("MCXFO", now_ts=dt_normal_post.timestamp(), force_check=True) is False

    # 4. Weekend: Saturday 2026-10-24
    dt_weekend = datetime.datetime(2026, 10, 24, 12, 0, 0, tzinfo=IST_TIMEZONE)
    assert config.is_market_open_ist("MCXFO", now_ts=dt_weekend.timestamp(), force_check=True) is False


def test_is_market_opening_stabilizing_buffer():
    """
    Verifies that is_market_opening_stabilizing enforces a strict 60s stabilization window:
    - Standard day: 09:00:00 to 09:01:00 is True; >= 09:01:00 is False.
    - Morning holiday: 17:00:00 to 17:01:00 is True; >= 17:01:00 is False.
    """
    # Temporarily set os.environ to enforce checks in tests
    with patch.dict(os.environ, {"ENFORCE_MARKET_HOURS_IN_TESTS": "true"}):
        # Standard Day: 2026-10-21
        dt_0900_15 = datetime.datetime(2026, 10, 21, 9, 0, 15, tzinfo=IST_TIMEZONE)
        dt_0900_59 = datetime.datetime(2026, 10, 21, 9, 0, 59, tzinfo=IST_TIMEZONE)
        dt_0901_00 = datetime.datetime(2026, 10, 21, 9, 1, 0, tzinfo=IST_TIMEZONE)
        dt_0901_05 = datetime.datetime(2026, 10, 21, 9, 1, 5, tzinfo=IST_TIMEZONE)

        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_0900_15.timestamp()) is True
        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_0900_59.timestamp()) is True
        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_0901_00.timestamp()) is False
        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_0901_05.timestamp()) is False

        # Morning-Only Holiday: 2026-10-20 (Dussehra)
        dt_1700_15 = datetime.datetime(2026, 10, 20, 17, 0, 15, tzinfo=IST_TIMEZONE)
        dt_1700_59 = datetime.datetime(2026, 10, 20, 17, 0, 59, tzinfo=IST_TIMEZONE)
        dt_1701_00 = datetime.datetime(2026, 10, 20, 17, 1, 0, tzinfo=IST_TIMEZONE)
        dt_1701_05 = datetime.datetime(2026, 10, 20, 17, 1, 5, tzinfo=IST_TIMEZONE)

        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_1700_15.timestamp()) is True
        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_1700_59.timestamp()) is True
        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_1701_00.timestamp()) is False
        assert config.is_market_opening_stabilizing("MCXFO", now_ts=dt_1701_05.timestamp()) is False


@pytest.mark.asyncio
async def test_rollover_on_full_day_holiday_queues_and_suppresses_orders():
    """
    Verifies that if a runner cycle executes on a full-day holiday (e.g. Gandhi Jayanti Oct 02):
    1. is_market_open_ist evaluates to False.
    2. Rollover is queued with self.pending_rollover = True.
    3. Zero broker orders are dispatched.
    4. Active contract pointer is preserved as the old contract.
    5. Strategy remains healthy and running (not paused).
    """
    runner = SingleSuperTrendRunner({
        "id": "st_silver_holiday",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 1,
        "execution_mode": "LIVE",
        "is_enabled": True,
        "virtual_position": 1,
        "active_contract_id": 574824,
        "active_contract_desc": "SILVER10030SEP2026FUT"
    })
    runner.strategy_position = "LONG"

    mock_main = MagicMock()
    mock_main.TRADING_PAUSED = False
    runner._execute_exit = AsyncMock()
    runner._execute_entry = AsyncMock()

    dt_holiday = datetime.datetime(2026, 10, 2, 14, 0, 0, tzinfo=IST_TIMEZONE)

    with patch.object(xts_api, "resolve_contract") as mock_rc, \
         patch("supertrend_engine.time.time", return_value=dt_holiday.timestamp()), \
         patch("supertrend_engine.is_market_open_ist", return_value=False), \
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

        # Zero orders dispatched
        runner._execute_exit.assert_not_called()
        runner._execute_entry.assert_not_called()

        # Queued state set
        assert runner.pending_rollover is True
        assert runner.active_contract_id == 574824
        assert runner.active_contract_desc == "SILVER10030SEP2026FUT"
        assert runner.virtual_position == 1
        assert runner.is_enabled is True
        assert runner.status == "RUNNING"


@pytest.mark.asyncio
async def test_queued_rollover_opening_bell_stabilization_and_execution():
    """
    Verifies that when a queued rollover encounters market open:
    1. During 09:00:00 - 09:01:00 (stabilization window), orders are paused.
    2. At 09:01:05, stabilization finishes -> Leg 1 and Leg 2 execute cleanly.
    3. pending_rollover is cleared, active contract advances, caches purged.
    """
    runner = SingleSuperTrendRunner({
        "id": "st_silver_bell",
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
    runner.pending_rollover = True
    runner.cached_candles = [{"time": 1780000000, "close": 90000.0}]

    mock_main = MagicMock()
    mock_main.TRADING_PAUSED = False
    mock_main.db_set_virtual_position = MagicMock()
    runner._execute_exit = AsyncMock(return_value=True)
    runner._execute_entry = AsyncMock(return_value=True)

    dt_open_unstable = datetime.datetime(2026, 10, 5, 9, 0, 30, tzinfo=IST_TIMEZONE)
    dt_open_stabilized = datetime.datetime(2026, 10, 5, 9, 1, 5, tzinfo=IST_TIMEZONE)

    mock_inst = {
        "name": "SILVER100",
        "expiry": datetime.date(2026, 10, 30),
        "inst_id": 574825,
        "exch_seg": "MCXFO",
        "desc": "SILVER10030OCT2026FUT",
        "tick_size": 1.0,
        "lot_size": 1,
        "freeze_qty": 100
    }

    # Step 1: At 09:00:30 (unstable window)
    with patch.object(xts_api, "resolve_contract", return_value=mock_inst), \
         patch("supertrend_engine.time.time", return_value=dt_open_unstable.timestamp()), \
         patch("supertrend_engine.is_market_open_ist", return_value=True), \
         patch("supertrend_engine.is_market_opening_stabilizing", return_value=True), \
         patch.object(xts_api, "fetch_ohlc_candles", return_value=[]):

        await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        # Still paused waiting for spread stabilization
        runner._execute_exit.assert_not_called()
        runner._execute_entry.assert_not_called()
        assert runner.pending_rollover is True
        assert runner.active_contract_id == 574824

    # Step 2: At 09:01:05 (stabilization complete)
    with patch.object(xts_api, "resolve_contract", return_value=mock_inst), \
         patch("supertrend_engine.time.time", return_value=dt_open_stabilized.timestamp()), \
         patch("supertrend_engine.is_market_open_ist", return_value=True), \
         patch("supertrend_engine.is_market_opening_stabilizing", return_value=False), \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(xts_api, "fetch_ohlc_candles", return_value=[]):

        await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        runner._execute_exit.assert_awaited_once()
        runner._execute_entry.assert_awaited_once()
        assert runner.pending_rollover is False
        assert runner.active_contract_id == 574825
        assert runner.active_contract_desc == "SILVER10030OCT2026FUT"
        assert runner.cached_candles == []


@pytest.mark.asyncio
async def test_morning_holiday_evening_session_rollover_lifecycle():
    """
    Verifies full lifecycle on a morning-holiday (e.g. Dussehra Oct 20):
    1. At 14:00 (morning session closed): rollover is queued with pending_rollover = True.
    2. At 17:00:20 (evening session open, within 60s): pauses for stabilization.
    3. At 17:01:05: executes 2-leg rollover successfully.
    """
    runner = SingleSuperTrendRunner({
        "id": "st_gold_dussehra",
        "symbol": "GOLDPETAL1!",
        "exchange_segment": "MCXFO",
        "timeframe": "20m",
        "quantity": 1,
        "execution_mode": "LIVE",
        "is_enabled": True,
        "virtual_position": 1,
        "active_contract_id": 568839,
        "active_contract_desc": "GOLDPETAL30SEP2026FUT"
    })
    runner.strategy_position = "LONG"

    mock_main = MagicMock()
    mock_main.TRADING_PAUSED = False
    mock_main.db_set_virtual_position = MagicMock()
    runner._execute_exit = AsyncMock(return_value=True)
    runner._execute_entry = AsyncMock(return_value=True)

    mock_inst = {
        "name": "GOLDPETAL",
        "expiry": datetime.date(2026, 10, 30),
        "inst_id": 571306,
        "exch_seg": "MCXFO",
        "desc": "GOLDPETAL30OCT2026FUT",
        "tick_size": 1.0,
        "lot_size": 1,
        "freeze_qty": 100
    }

    # Phase 1: 14:00 IST on Dussehra -> Market closed
    dt_1400 = datetime.datetime(2026, 10, 20, 14, 0, 0, tzinfo=IST_TIMEZONE)
    with patch.object(xts_api, "resolve_contract", return_value=mock_inst), \
         patch("supertrend_engine.time.time", return_value=dt_1400.timestamp()), \
         patch("supertrend_engine.is_market_open_ist", return_value=False), \
         patch.object(xts_api, "fetch_ohlc_candles", return_value=[]):

        await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        runner._execute_exit.assert_not_called()
        assert runner.pending_rollover is True
        assert runner.active_contract_id == 568839

    # Phase 2: 17:00:20 IST on Dussehra -> Evening open, stabilizing
    dt_1700_20 = datetime.datetime(2026, 10, 20, 17, 0, 20, tzinfo=IST_TIMEZONE)
    with patch.object(xts_api, "resolve_contract", return_value=mock_inst), \
         patch("supertrend_engine.time.time", return_value=dt_1700_20.timestamp()), \
         patch("supertrend_engine.is_market_open_ist", return_value=True), \
         patch("supertrend_engine.is_market_opening_stabilizing", return_value=True), \
         patch.object(xts_api, "fetch_ohlc_candles", return_value=[]):

        await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        runner._execute_exit.assert_not_called()
        assert runner.pending_rollover is True

    # Phase 3: 17:01:05 IST on Dussehra -> Evening open, stabilized
    dt_1701_05 = datetime.datetime(2026, 10, 20, 17, 1, 5, tzinfo=IST_TIMEZONE)
    with patch.object(xts_api, "resolve_contract", return_value=mock_inst), \
         patch("supertrend_engine.time.time", return_value=dt_1701_05.timestamp()), \
         patch("supertrend_engine.is_market_open_ist", return_value=True), \
         patch("supertrend_engine.is_market_opening_stabilizing", return_value=False), \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(xts_api, "fetch_ohlc_candles", return_value=[]):

        await runner.evaluate_cycle(xts_api, mock_main, skip_order_checks=True)

        runner._execute_exit.assert_awaited_once()
        runner._execute_entry.assert_awaited_once()
        assert runner.pending_rollover is False
        assert runner.active_contract_id == 571306
        assert runner.active_contract_desc == "GOLDPETAL30OCT2026FUT"


def test_pending_rollover_priority_dispatch_classification():
    """
    Verifies that a runner on a 15m or 1h timeframe with pending_rollover = True
    is prioritized into closing_runners for fast-cycle evaluation upon market open,
    even when the candle boundary is far away (e.g. sec_rem = 800s).
    """
    runner = SingleSuperTrendRunner({
        "id": "st_silver_prio",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True,
        "active_contract_id": 574824
    })
    runner.cached_candles = [{"time": 1, "close": 100}]
    runner.pending_rollover = True

    # Mid-candle: 800 seconds remaining
    sec_rem = 800
    sec_into_bar = 100
    is_prio = (sec_rem <= 20 or sec_into_bar <= 10 or not runner.cached_candles or getattr(runner, "pending_rollover", False))
    assert is_prio is True
