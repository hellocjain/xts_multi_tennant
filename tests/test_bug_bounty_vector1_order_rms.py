"""
Bug Bounty Vector 1: Order Execution, Freeze Slicing & RMS Rejections Test Suite.
Verifies:
1. Slicing arithmetic under exact multiples, non-multiples, and boundary limits.
2. Halt-on-rejection safety during sliced execution (no phantom fills).
3. RMS margin shortfall & [NONSQROFF] tender lockout handling.
4. Defense-in-depth safety limit refusing rogue quantities.
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
from supertrend_engine import (
    SingleSuperTrendRunner,
    MultiSuperTrendEngine,
    slice_quantity_for_freeze
)
from chaos_test_harness import ChaosBrokerMock


@pytest.fixture
def mock_main(tmp_path, monkeypatch):
    """Provides an isolated client main module with temporary SQLite database."""
    client_main_file = os.path.join(client_path, "main.py")
    spec = importlib.util.spec_from_file_location("client_main_v1", client_main_file)
    client_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client_main)

    test_db = str(tmp_path / "test_v1.db")
    monkeypatch.setattr(client_main, "_DB_PATH", test_db)
    client_main.db_init()
    return client_main


def test_freeze_slicing_math():
    """Verifies freeze slicing mathematics across multiple boundary quantities."""
    # Case 1: Exact multiple
    assert slice_quantity_for_freeze(25, 5) == [5, 5, 5, 5, 5]
    # Case 2: Non-multiple
    assert slice_quantity_for_freeze(23, 5) == [5, 5, 5, 5, 3]
    # Case 3: Below limit
    assert slice_quantity_for_freeze(4, 5) == [4]
    # Case 4: Exactly limit
    assert slice_quantity_for_freeze(5, 5) == [5]
    # Case 5: 1 lot
    assert slice_quantity_for_freeze(1, 100) == [1]
    # Case 6: 0 or negative
    assert slice_quantity_for_freeze(0, 5) == []
    assert slice_quantity_for_freeze(-10, 5) == []


@pytest.mark.asyncio
async def test_sliced_entry_execution_with_unique_order_refs(mock_main, monkeypatch):
    """
    Verifies that slicing an order of 12 lots with freeze limit 5:
    1. Produces 3 slices [5, 5, 2].
    2. Dispatches all slices with distinct order_refs (ST_REV_ENTRY_..., _2, _3).
    3. Accurately updates virtual_position to 12.
    """
    chaos_broker = ChaosBrokerMock()

    async def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        return await chaos_broker.simulate_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper)

    def sync_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        # Synchronous bridge for asyncio.to_thread
        return asyncio.run(chaos_broker.simulate_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper))

    monkeypatch.setattr(mock_main, "_dispatch_and_record", sync_dispatch)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_slicing",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 12,
        "is_enabled": True
    }, main_module=mock_main)

    await runner._execute_entry("BUY", 12, "TEST_SLICE", mock_main, freeze_limit=5)

    assert len(chaos_broker.dispatched_orders) == 3
    assert [o["quantity"] for o in chaos_broker.dispatched_orders] == [5, 5, 2]
    refs = [o["order_ref"] for o in chaos_broker.dispatched_orders]
    assert len(set(refs)) == 3  # All distinct order refs
    assert runner.virtual_position == 12


@pytest.mark.asyncio
async def test_sliced_entry_halts_on_broker_margin_rejection(mock_main, monkeypatch):
    """
    Simulates a 10-lot entry sliced into [5, 5].
    Slice 1 succeeds (+5). Slice 2 is rejected by RMS with 'INSUFFICIENT_MARGIN'.
    Verifies:
    1. Slice 2 rejection halts further execution.
    2. Virtual position records ONLY +5 lots (what actually filled).
    3. Does NOT assume +10 lots.
    """
    calls = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        calls.append(qty)
        if len(calls) == 1:
            return {"status": "done", "result": {"AppOrderID": 101}}
        else:
            return {"status": "rejected", "error": "Insufficient Margin in account"}

    monkeypatch.setattr(mock_main, "_dispatch_and_record", mock_dispatch)

    runner = SingleSuperTrendRunner({
        "id": "st_margin_halt",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 10,
        "is_enabled": True
    }, main_module=mock_main)

    await runner._execute_entry("BUY", 10, "MARGIN_FAIL", mock_main, freeze_limit=5)

    assert len(calls) == 2
    # Invariant: Only slice 1 (5 lots) credited to virtual position
    assert runner.virtual_position == 5
    assert mock_main.db_get_virtual_position("SILVER1001!_15m") == 5


@pytest.mark.asyncio
async def test_rogue_quantity_safety_guard(mock_main, monkeypatch):
    """
    Verifies that a rogue entry or delta order exceeding 5x the strategy quantity
    or the absolute safety cap is rejected BEFORE hitting the broker API.
    """
    dispatched = []
    def mock_dispatch(*args, **kwargs):
        dispatched.append(args)
        return {"status": "done"}

    monkeypatch.setattr(mock_main, "_dispatch_and_record", mock_dispatch)

    runner = SingleSuperTrendRunner({
        "id": "st_safety_guard",
        "symbol": "GOLDPETAL1!",
        "timeframe": "5m",
        "quantity": 2,
        "is_enabled": True
    }, main_module=mock_main)

    # Rogue quantity 500 lots (configured quantity is 2)
    await runner._execute_entry("BUY", 500, "ROGUE_ENTRY", mock_main)
    assert len(dispatched) == 0
    assert runner.virtual_position == 0

    await runner._execute_exit("LONG", 500, "ROGUE_EXIT", mock_main)
    assert len(dispatched) == 0
    assert runner.virtual_position == 0
