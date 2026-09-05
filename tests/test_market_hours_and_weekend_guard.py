import pytest
import datetime
import time
import os
import sys
import importlib.util
from contextlib import closing

client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../client"))
portal_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../portal"))

if client_dir not in sys.path:
    sys.path.insert(0, client_dir)
if portal_dir not in sys.path:
    sys.path.insert(0, portal_dir)

import supertrend_engine
import scheduler
import database
import security

def load_client_main():
    spec = importlib.util.spec_from_file_location("client_main_mod", os.path.join(client_dir, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

client_main = load_client_main()
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_key = "uYvN3lM8k9P2w4X6Z8a0b2c4d6e8f0g2h4j6k8m0n2p="
    monkeypatch.setenv("PORTAL_MASTER_KEY", test_key)
    monkeypatch.setattr(database, "get_db_path", lambda: str(tmp_path / "portal_test.db"))
    database.init_portal_db()
    
    test_sig_db = str(tmp_path / "signals_test.db")
    monkeypatch.setattr(client_main, "_DB_PATH", test_sig_db)
    client_main.db_init()

def test_is_market_open_ist_accuracy():
    """Verifies market hours and weekend detection across various days and times in IST."""
    
    # Saturday 2026-09-05 15:45:00 IST (Weekend -> CLOSED)
    sat_dt = datetime.datetime(2026, 9, 5, 15, 45, 0, tzinfo=IST)
    sat_ts = sat_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=sat_ts, force_check=True) is False
    assert supertrend_engine.is_market_open_ist("NSEFO", now_ts=sat_ts, force_check=True) is False
    assert scheduler.is_market_open_ist("MCXFO", now_dt=sat_dt, force_check=True) is False

    # Sunday 2026-09-06 10:00:00 IST (Weekend -> CLOSED)
    sun_dt = datetime.datetime(2026, 9, 6, 10, 0, 0, tzinfo=IST)
    sun_ts = sun_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=sun_ts, force_check=True) is False
    assert scheduler.is_market_open_ist("MCXFO", now_dt=sun_dt, force_check=True) is False

    # Monday 2026-09-07 08:30:00 IST (Pre-market -> CLOSED)
    mon_early_dt = datetime.datetime(2026, 9, 7, 8, 30, 0, tzinfo=IST)
    mon_early_ts = mon_early_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=mon_early_ts, force_check=True) is False
    assert supertrend_engine.is_market_open_ist("NSEFO", now_ts=mon_early_ts, force_check=True) is False
    assert scheduler.is_market_open_ist("MCXFO", now_dt=mon_early_dt, force_check=True) is False

    # Monday 2026-09-07 09:05:00 IST (MCX OPEN, NSE CLOSED)
    mon_mcx_open_dt = datetime.datetime(2026, 9, 7, 9, 5, 0, tzinfo=IST)
    mon_mcx_open_ts = mon_mcx_open_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=mon_mcx_open_ts, force_check=True) is True
    assert supertrend_engine.is_market_open_ist("NSEFO", now_ts=mon_mcx_open_ts, force_check=True) is False

    # Monday 2026-09-07 14:00:00 IST (Both OPEN)
    mon_mid_dt = datetime.datetime(2026, 9, 7, 14, 0, 0, tzinfo=IST)
    mon_mid_ts = mon_mid_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=mon_mid_ts, force_check=True) is True
    assert supertrend_engine.is_market_open_ist("NSEFO", now_ts=mon_mid_ts, force_check=True) is True
    assert scheduler.is_market_open_ist("MCXFO", now_dt=mon_mid_dt, force_check=True) is True

    # Monday 2026-09-07 16:00:00 IST (MCX OPEN, NSE CLOSED)
    mon_post_nse_dt = datetime.datetime(2026, 9, 7, 16, 0, 0, tzinfo=IST)
    mon_post_nse_ts = mon_post_nse_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=mon_post_nse_ts, force_check=True) is True
    assert supertrend_engine.is_market_open_ist("NSEFO", now_ts=mon_post_nse_ts, force_check=True) is False

    # Friday 2026-09-11 23:25:00 IST (MCX Late evening session -> OPEN)
    fri_late_dt = datetime.datetime(2026, 9, 11, 23, 25, 0, tzinfo=IST)
    fri_late_ts = fri_late_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=fri_late_ts, force_check=True) is True

    # Friday 2026-09-11 23:58:00 IST (Post-MCX Close -> CLOSED)
    fri_post_close_dt = datetime.datetime(2026, 9, 11, 23, 58, 0, tzinfo=IST)
    fri_post_close_ts = fri_post_close_dt.timestamp()
    assert supertrend_engine.is_market_open_ist("MCXFO", now_ts=fri_post_close_ts, force_check=True) is False

@pytest.mark.anyio
async def test_supertrend_evaluation_suppressed_when_market_closed(monkeypatch):
    """
    Verifies that when market is closed:
    1. SingleSuperTrendRunner computes indicators and bands for live telemetry & charts.
    2. Runner status is set to 'MARKET_CLOSED'.
    3. Order execution is strictly suppressed (zero orders dispatched).
    """
    import xts_api

    dispatched_orders = []
    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    runner = supertrend_engine.SingleSuperTrendRunner({
        "id": "st_silver_test",
        "symbol": "SILVER1001!",
        "exchange_segment": "MCXFO",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True
    }, dispatch_fn=mock_dispatch)

    # Mock Saturday timestamp: 2026-09-05 15:45:00 IST
    sat_dt = datetime.datetime(2026, 9, 5, 15, 45, 0, tzinfo=IST)
    sat_ts = sat_dt.timestamp()
    monkeypatch.setattr(time, "time", lambda: sat_ts)
    monkeypatch.setenv("ENFORCE_MARKET_HOURS_IN_TESTS", "true")

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 112233,
        "desc": "SILVER 05MAR2027",
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 10000,
        "expiry": datetime.date(2027, 3, 5)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Friday night candles ending at 23:29:59 IST with a bullish flip
    fri_close_dt = datetime.datetime(2026, 9, 4, 23, 29, 59, tzinfo=IST)
    fri_close_ts = int(fri_close_dt.timestamp())
    candles = []
    for i, p in enumerate([100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 150]):
        t = fri_close_ts - ((15 - i) * 900)
        candles.append({
            "time": t,
            "open": float(p) - 1.0,
            "high": float(p) + 2.0,
            "low": float(p) - 2.0,
            "close": float(p),
            "volume": 1000
        })

    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: candles)

    await runner.evaluate_cycle(xts_api, client_main)

    # Invariant 1: No orders were dispatched
    assert len(dispatched_orders) == 0, f"Expected 0 orders on weekend, got {len(dispatched_orders)}"

    # Invariant 2: Telemetry reflects MARKET_CLOSED
    telemetry = runner.get_telemetry()
    assert telemetry["status"] == "MARKET_CLOSED"
    assert telemetry["market_open"] is False
    assert telemetry["last_close"] == 150.0
    assert telemetry["current_trend"] == "BULLISH"

@pytest.mark.anyio
async def test_startup_stale_candle_guard_suppresses_historical_signal(monkeypatch):
    """
    Verifies that when a container boots up during active market hours,
    a historical candle older than MAX_CANDLE_AGE_SECONDS seeds the baseline state
    without firing a stale historical flip order.
    """
    import xts_api

    dispatched_orders = []
    async def mock_dispatch(sig_id, payload):
        dispatched_orders.append((sig_id, payload))

    runner = supertrend_engine.SingleSuperTrendRunner({
        "id": "st_crude_stale",
        "symbol": "CRUDEOIL1!",
        "exchange_segment": "MCXFO",
        "timeframe": "5m",
        "quantity": 1,
        "is_enabled": True
    }, dispatch_fn=mock_dispatch)

    assert runner.last_processed_candle_time == 0

    # Monday 2026-09-07 14:00:00 IST (Active market hours)
    now_dt = datetime.datetime(2026, 9, 7, 14, 0, 0, tzinfo=IST)
    now_ts = now_dt.timestamp()
    monkeypatch.setattr(time, "time", lambda: now_ts)
    monkeypatch.setenv("ENFORCE_MARKET_HOURS_IN_TESTS", "true")
    monkeypatch.setenv("ENFORCE_STALE_CANDLE_GUARD_IN_TESTS", "true")

    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 556677,
        "desc": "CRUDEOIL 19OCT2026",
        "exch_seg": "MCXFO",
        "lot_size": 1,
        "freeze_qty": 10000,
        "expiry": datetime.date(2026, 10, 19)
    })
    monkeypatch.setattr(xts_api, "get_positions_telemetry", lambda: {"positions": [], "all_positions": []})
    monkeypatch.setattr(xts_api, "get_broker_orders", lambda: [])

    # Old candle that closed 2 hours ago (12:00:00 IST -> 7200s old > 360s stale limit)
    old_candle_dt = datetime.datetime(2026, 9, 7, 11, 59, 59, tzinfo=IST)
    old_candle_ts = int(old_candle_dt.timestamp())
    candles = []
    for i, p in enumerate([100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 150]):
        t = old_candle_ts - ((15 - i) * 300)
        candles.append({
            "time": t,
            "open": float(p) - 1.0,
            "high": float(p) + 2.0,
            "low": float(p) - 2.0,
            "close": float(p),
            "volume": 1000
        })

    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda seg, iid, tf, bars: candles)

    await runner.evaluate_cycle(xts_api, client_main)

    # Invariant 1: No orders fired on stale historical candle
    assert len(dispatched_orders) == 0

    # Invariant 2: Baseline was seeded with historical candle timestamp
    assert runner.last_processed_candle_time == old_candle_ts

@pytest.mark.anyio
async def test_circuit_breaker_suppressed_on_weekend(monkeypatch):
    """
    Verifies that the portal drawdown circuit breaker does not trigger emergency panic
    or auto-pause tenants on weekends, even if Net MTM breaches the daily loss threshold.
    """
    import telemetry_service

    # 1. Setup tenant with daily loss limit = 25,000 INR
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) VALUES ('weekend_client', 'Weekend Client', 'ACTIVE', 0, 0)")
            enc = security.encrypt_credentials({"WEBHOOK_SECRET": "sec_weekend"})
            conn.execute("INSERT OR REPLACE INTO tenant_credentials (tenant_id, encrypted_payload, updated_at) VALUES ('weekend_client', ?, 0)", (enc,))
            conn.execute("INSERT OR REPLACE INTO tenant_risk_limits (tenant_id, max_daily_loss_inr, updated_at) VALUES ('weekend_client', 25000.0, 0)")

    # 2. Mock Net MTM = -₹50,000 (breaches limit)
    async def mock_telemetry(t_id):
        return {"net_mtm": -50000.0}

    panic_called = []
    async def mock_panic(t_id, secret):
        panic_called.append((t_id, secret))
        return {"status": "success"}

    monkeypatch.setattr(telemetry_service, "get_single_client_telemetry", mock_telemetry)
    monkeypatch.setattr(telemetry_service, "panic_single_client", mock_panic)
    monkeypatch.setenv("ENFORCE_MARKET_HOURS_IN_TESTS", "true")

    # Mock Saturday timestamp
    sat_dt = datetime.datetime(2026, 9, 5, 15, 46, 31, tzinfo=IST)
    monkeypatch.setattr(scheduler, "is_market_open_ist", lambda *a, **kw: False)

    # Execute circuit breaker check with market hours enforcement
    await scheduler.check_drawdown_circuit_breakers(enforce_market_hours=True)

    # Invariant 1: Panic was NOT called
    assert len(panic_called) == 0, f"Expected panic NOT called on weekend, but got {panic_called}"

    # Invariant 2: Tenant remains ACTIVE
    with database.get_db_connection() as conn:
        row = conn.execute("SELECT status FROM tenants WHERE id='weekend_client'").fetchone()
        assert row["status"] == "ACTIVE"

def test_place_order_blocked_when_market_closed(monkeypatch):
    """
    Verifies that place_order at the central API gateway layer immediately rejects
    any order dispatch when is_market_open_ist() returns False.
    """
    import xts_api
    import config

    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(config, "ENFORCE_MARKET_HOURS", True)
    monkeypatch.setattr(config, "is_market_open_ist", lambda exch_seg=None, **kw: False)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda: "mock_token")
    monkeypatch.setattr(xts_api, "get_dynamic_contract_info", lambda sym: (574824, "MCXFO", "NRML", 0.05, 1, 1000, datetime.date.today()))

    res = xts_api.place_order("BUY", "SILVER1001!", 1, 85000.0, "TEST_WEEKEND_ORDER", is_paper=False)
    assert res.get("status") == "error"
    assert "Market closed" in res.get("message", "")

def test_panic_square_off_suppresses_position_orders_when_market_closed(monkeypatch):
    """
    Verifies that panic_square_off_all safely cancels all pending orders, but suppresses
    sending market orders to square off open positions when the market is closed.
    """
    import xts_api
    import config

    monkeypatch.setattr(config, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(config, "ENFORCE_MARKET_HOURS", True)
    monkeypatch.setattr(config, "is_market_open_ist", lambda exch_seg=None, **kw: False)
    monkeypatch.setattr(xts_api, "get_interactive_token", lambda *a, **kw: "mock_token")
    monkeypatch.setattr(xts_api, "get_safe_base_url", lambda: "https://mock.xts")

    # Mock cancel all response
    class MockResp:
        status_code = 200
        def json(self):
            return {"type": "success", "result": {"positionList": [{"Quantity": -14, "ExchangeInstrumentId": 574824}]}}

    monkeypatch.setattr(xts_api.api_session, "post", lambda *a, **kw: MockResp())
    monkeypatch.setattr(xts_api.api_session, "get", lambda *a, **kw: MockResp())

    res = xts_api.panic_square_off_all()
    assert res.get("status") == "partial_success"
    assert "Market closed" in res.get("message", "")
    assert res.get("open_positions_count") == 1

def test_fetch_ohlc_candles_filters_weekend_bars(monkeypatch):
    """
    Verifies that fetch_ohlc_candles discards any mock/test candles timestamped
    on Saturdays or Sundays from entering downstream calculation.
    """
    import xts_api
    import config

    monkeypatch.setattr(config, "ENFORCE_MARKET_HOURS", True)
    monkeypatch.setattr(xts_api, "get_marketdata_token", lambda *a, **kw: ("mock_token", "https://mock.md"))

    # Friday 2026-09-04 23:15:00 IST = 1788543900 UTC (+19800 = 1788563700 in raw XTS)
    fri_raw = 1788543900 + 19800
    # Saturday 2026-09-05 15:45:00 IST = 1788603300 UTC (+19800 = 1788623100 in raw XTS)
    sat_raw = 1788603300 + 19800

    # Broker response containing 1 Friday bar and 1 Saturday mock bar
    data_response = f"{fri_raw}|100|105|98|103|1000|5000,{sat_raw}|104|108|102|107|500|4000"

    class MockMDResp:
        status_code = 200
        def json(self):
            return {"result": {"dataReponse": data_response}}

    monkeypatch.setattr(xts_api.api_session, "get", lambda *a, **kw: MockMDResp())

    candles = xts_api.fetch_ohlc_candles("MCXFO", 574824, 900, 10)

    # Invariant: Only the Friday candle survived; the Saturday mock candle was discarded
    assert len(candles) == 1
    assert candles[0]["time"] == 1788543900

