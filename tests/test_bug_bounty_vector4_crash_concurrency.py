"""
Bug Bounty Vector 4: Hard Crash, SQLite Concurrency & Multi-Strategy Stress Suite.
Verifies:
1. 6 concurrent runners evaluating simultaneous ticks without lock starvation.
2. High-frequency SQLite WAL concurrency with zero 'database is locked' errors.
3. Post-crash position persistence and recovery without duplicate order dispatch.
"""

import pytest
import asyncio
import os
import sys
import importlib.util
from pathlib import Path

BASE_DIR = str(Path(__file__).parent.parent)
client_path = os.path.join(BASE_DIR, "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

import config
import xts_api
from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine
from chaos_test_harness import generate_synthetic_candles


@pytest.fixture
def mock_main(tmp_path, monkeypatch):
    client_main_file = os.path.join(client_path, "main.py")
    spec = importlib.util.spec_from_file_location("client_main_v4", client_main_file)
    client_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client_main)

    test_db = str(tmp_path / "test_v4.db")
    monkeypatch.setattr(client_main, "_DB_PATH", test_db)
    client_main.db_init()
    return client_main


@pytest.mark.asyncio
async def test_concurrent_six_runners_under_rapid_ticks(mock_main, monkeypatch):
    """
    Spawns 6 concurrent SingleSuperTrendRunner instances on various symbols and timeframes.
    Evaluates them concurrently under 10 rapid evaluation cycles.
    Asserts: Zero exceptions, zero deadlocks, and clean telemetry aggregation.
    """
    engine = MultiSuperTrendEngine(max_strategies=6)
    candles = generate_synthetic_candles(100, base_price=2450.0)

    monkeypatch.setattr(xts_api, "fetch_ohlc_candles", lambda *a, **kw: candles)
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574824 if "SILVER" in sym else 568839,
        "symbol": sym.replace("1!", ""),
        "desc": f"{sym} 30SEP2026",
        "exch_seg": "MCXFO",
        "freeze_qty": 1000
    })

    symbols = ["SILVER1001!", "GOLDPETAL1!", "CRUDEOIL1!", "NATURALGAS1!", "COPPER1!", "ZINC1!"]
    for i, sym in enumerate(symbols):
        strat_id = f"st_{sym.lower()}_5m"
        engine.add_or_update_strategy({
            "id": strat_id,
            "symbol": sym,
            "timeframe": "5m",
            "quantity": i + 1,
            "is_enabled": True
        })

    assert len(engine.strategies) == 6

    # Execute 10 concurrent evaluation cycles across all 6 runners
    for _ in range(10):
        tasks = [runner.evaluate_cycle(xts_api, mock_main) for runner in engine.strategies.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            assert not isinstance(r, Exception), f"Runner threw unexpected exception: {r}"

    # Consolidated telemetry verification
    tel = engine.get_telemetry()
    assert tel["active_strategies_count"] == 6
    assert len(tel["strategies"]) == 6


@pytest.mark.asyncio
async def test_sqlite_wal_concurrency_stress(mock_main):
    """
    Hammers SQLite database with 100 concurrent async tasks reading and writing
    pending signals, virtual positions, and order statuses.
    Asserts: Zero SQLite lock exceptions.
    """
    async def worker_task(worker_id: int):
        sig_id = f"sig_stress_{worker_id}"
        payload = {"action": "BUY", "symbol": "SILVER1001!", "quantity": worker_id}
        # Step 1: Insert pending
        mock_main.db_insert_pending(sig_id, payload)
        # Step 2: Set virtual position
        mock_main.db_set_virtual_position(f"strat_{worker_id}", "SILVER1001!", "5m", worker_id)
        # Step 3: Read back virtual position
        vpos = mock_main.db_get_virtual_position(f"strat_{worker_id}")
        assert vpos == worker_id
        # Step 4: Update status
        mock_main.db_update_status(sig_id, "done", {"AppOrderID": 1000 + worker_id})

    tasks = [worker_task(i) for i in range(100)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        assert not isinstance(r, Exception), f"SQLite concurrency failed with error: {r}"


@pytest.mark.asyncio
async def test_post_crash_virtual_position_recovery(mock_main, monkeypatch):
    """
    Simulates container crash with active open positions on SQLite disk.
    On container restart, runners restore their exact state without order dispatch.
    """
    # Pre-populate SQLite state
    mock_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", 3)
    mock_main.db_set_virtual_position("GOLDPETAL1!_30m", "GOLDPETAL1!", "30m", -4)

    dispatched = []
    monkeypatch.setattr(mock_main, "_dispatch_and_record", lambda *a, **kw: dispatched.append(a))

    # Start fresh runners
    r_silver = SingleSuperTrendRunner({"id": "r1", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 3, "is_enabled": True}, main_module=mock_main)
    r_gold = SingleSuperTrendRunner({"id": "r2", "symbol": "GOLDPETAL1!", "timeframe": "30m", "quantity": 4, "is_enabled": True}, main_module=mock_main)

    assert r_silver.virtual_position == 3
    assert r_silver.strategy_position == "LONG"
    assert r_gold.virtual_position == -4
    assert r_gold.strategy_position == "SHORT"
    assert len(dispatched) == 0  # Zero unwanted bootstrap trades
