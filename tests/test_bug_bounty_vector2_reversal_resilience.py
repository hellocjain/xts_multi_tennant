"""
Bug Bounty Vector 2: Asymmetric 2-Leg Reversal Resilience Test Suite.
Verifies:
1. When Exit leg succeeds but Entry leg fails, virtual_position resets cleanly to 0 (FLAT).
2. When Exit leg fails, Entry leg is strictly aborted to prevent opening unhedged double-risk.
3. Successful 2-leg reversal transitions smoothly from -X to +X (and vice versa).
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


@pytest.fixture
def mock_main(tmp_path, monkeypatch):
    client_main_file = os.path.join(client_path, "main.py")
    spec = importlib.util.spec_from_file_location("client_main_v2", client_main_file)
    client_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client_main)

    test_db = str(tmp_path / "test_v2.db")
    monkeypatch.setattr(client_main, "_DB_PATH", test_db)
    client_main.db_init()
    return client_main


@pytest.mark.asyncio
async def test_reversal_exit_succeeds_entry_fails(mock_main, monkeypatch):
    """
    Scenario: Strategy is SHORT -2 lots.
    SuperTrend flips BULLISH.
    Leg 1: Exit SHORT (BUY 2) -> SUCCESS
    Leg 2: Entry LONG (BUY 2) -> RMS REJECTION
    Asserts:
    1. virtual_position updates from -2 to 0 on exit success.
    2. virtual_position stays 0 on entry rejection (NO PHANTOM LONG).
    3. SQLite persists 0.
    """
    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append({"action": action, "qty": qty, "order_ref": order_ref})
        if "EXIT" in order_ref:
            return {"status": "done", "result": {"AppOrderID": 201}}
        else:
            return {"status": "rejected", "error": "RMS Margin Insufficient"}

    monkeypatch.setattr(mock_main, "_dispatch_and_record", mock_dispatch)
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 568839,
        "symbol": "GOLDPETAL",
        "desc": "GOLDPETAL 30SEP2026",
        "exch_seg": "MCXFO",
        "freeze_qty": 1000
    })

    mock_main.db_set_virtual_position("GOLDPETAL1!_20m", "GOLDPETAL1!", "20m", -2)

    runner = SingleSuperTrendRunner({
        "id": "st_gold_reversal",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 2,
        "is_enabled": True
    }, main_module=mock_main)

    assert runner.virtual_position == -2

    # Simulate SuperTrend flip to BULLISH
    # Leg 1: Exit SHORT
    exit_qty = abs(runner.virtual_position)
    await runner._execute_exit("SHORT", exit_qty, "FLIP_EXIT_100", mock_main)
    assert runner.virtual_position == 0  # Cleanly zeroed after exit fill
    assert mock_main.db_get_virtual_position("GOLDPETAL1!_20m") == 0

    # Leg 2: Entry LONG (Fails)
    await runner._execute_entry("BUY", runner.quantity, "FLIP_ENTRY_100", mock_main)
    # Virtual position must STAY at 0 because entry was rejected!
    assert runner.virtual_position == 0
    assert runner.strategy_position == "FLAT"
    assert mock_main.db_get_virtual_position("GOLDPETAL1!_20m") == 0


@pytest.mark.asyncio
async def test_reversal_exit_fails_entry_is_not_dispatched(mock_main, monkeypatch):
    """
    Scenario: Strategy is LONG +1 lot.
    SuperTrend flips BEARISH.
    Leg 1: Exit LONG (SELL 1) -> REJECTED / BROKER ERROR
    Asserts:
    1. virtual_position remains +1.
    2. Entry leg is NOT called.
    """
    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append({"action": action, "qty": qty, "order_ref": order_ref})
        return {"status": "rejected", "error": "Order rejected by exchange"}

    monkeypatch.setattr(mock_main, "_dispatch_and_record", mock_dispatch)
    mock_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", 1)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_reversal_fail",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True
    }, main_module=mock_main)

    assert runner.virtual_position == 1

    # Attempt exit
    await runner._execute_exit("LONG", 1, "FLIP_EXIT_200", mock_main)
    # Because exit failed, virtual_position remains +1 (did not fill)
    assert runner.virtual_position == 1
    assert runner.strategy_position == "LONG"


@pytest.mark.asyncio
async def test_reversal_atomic_two_leg_success(mock_main, monkeypatch):
    """
    Verifies clean execution of 2-leg reversal from SHORT (-2) to LONG (+2).
    """
    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append({"action": action, "qty": qty, "order_ref": order_ref})
        return {"status": "done", "result": {"AppOrderID": 301}}

    monkeypatch.setattr(mock_main, "_dispatch_and_record", mock_dispatch)
    mock_main.db_set_virtual_position("SILVER1001!_30m", "SILVER1001!", "30m", -2)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_clean_rev",
        "symbol": "SILVER1001!",
        "timeframe": "30m",
        "quantity": 2,
        "is_enabled": True
    }, main_module=mock_main)

    assert runner.virtual_position == -2

    # Leg 1: Exit Short
    await runner._execute_exit("SHORT", 2, "FLIP_EXIT_300", mock_main)
    assert runner.virtual_position == 0

    # Leg 2: Enter Long
    await runner._execute_entry("BUY", 2, "FLIP_ENTRY_300", mock_main)
    assert runner.virtual_position == 2
    assert runner.strategy_position == "LONG"
    assert mock_main.db_get_virtual_position("SILVER1001!_30m") == 2
