"""
Bug Bounty Vector 5: UI Double-Clicks, Webhook Race Conditions & Circuit Breakers.
Verifies:
1. Double-clicking /reset-flat or /sync-trend is serialized by mutex locks without duplicate orders.
2. Emergency Circuit Breaker (TRADING_PAUSED) strictly prevents any order execution.
3. Webhook authentication security and internal token enforcement.
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
    spec = importlib.util.spec_from_file_location("client_main_v5", client_main_file)
    client_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client_main)

    test_db = str(tmp_path / "test_v5.db")
    monkeypatch.setattr(client_main, "_DB_PATH", test_db)
    client_main.db_init()
    return client_main


@pytest.mark.asyncio
async def test_rapid_concurrent_double_clicks_on_reset_flat(mock_main, monkeypatch):
    """
    Simulates a user rapidly double/triple clicking 'Square Off & Reset' in the UI.
    Sends 10 concurrent reset_to_flat requests simultaneously.
    Asserts:
    1. Exactly ONE exit order is dispatched.
    2. Virtual position cleanly reaches 0 FLAT without multiple exit market orders.
    """
    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append({"order_ref": order_ref, "qty": qty})
        return {"status": "done", "result": {"AppOrderID": 501}}

    monkeypatch.setattr(mock_main, "_dispatch_and_record", mock_dispatch)
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574824,
        "symbol": "SILVER100",
        "desc": "SILVER100 30SEP2026",
        "exch_seg": "MCXFO",
        "freeze_qty": 1000
    })

    mock_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", 2)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_race",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 2,
        "is_enabled": True
    }, main_module=mock_main)

    assert runner.virtual_position == 2

    # Dispatch 10 concurrent reset requests simultaneously
    tasks = [runner.reset_to_flat(square_off_broker=True, xts_api_module=xts_api, main_module=mock_main) for _ in range(10)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # All requests return success
    for res in results:
        assert isinstance(res, dict) and res.get("status") == "SUCCESS"

    # Invariant: EXACTLY 1 exit order was dispatched (mutex serialized, subsequent calls saw 0 flat)
    assert len(dispatched) == 1
    assert runner.virtual_position == 0
    assert mock_main.db_get_virtual_position("SILVER1001!_15m") == 0


@pytest.mark.asyncio
async def test_circuit_breaker_trading_paused_blocks_evaluation(mock_main, monkeypatch):
    """
    Verifies that when TRADING_PAUSED is active, evaluate_cycle immediately halts
    and marks status as 'PAUSED' without fetching candles or checking reversals.
    """
    dispatched = []
    monkeypatch.setattr(mock_main, "_dispatch_and_record", lambda *a, **kw: dispatched.append(a))
    mock_main.TRADING_PAUSED = True

    runner = SingleSuperTrendRunner({
        "id": "st_paused",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True
    }, main_module=mock_main)

    await runner.evaluate_cycle(xts_api, mock_main)

    assert runner.status == "PAUSED"
    assert len(dispatched) == 0


@pytest.mark.asyncio
async def test_internal_endpoint_token_enforcement(mock_main, monkeypatch):
    """
    Verifies that internal control endpoints reject requests with invalid or missing X-Internal-Token.
    """
    from fastapi.testclient import TestClient
    monkeypatch.setattr(mock_main.config, "INTERNAL_AUTH_TOKEN", "SECRET_AUDIT_TOKEN_XYZ")

    client = TestClient(mock_main.app)

    # Case 1: Missing token -> 403 Forbidden
    resp1 = client.post("/internal/supertrend/strategy/reset-flat", json={"strategy_id": "st_test"})
    assert resp1.status_code == 403

    # Case 2: Wrong token -> 403 Forbidden
    resp2 = client.post("/internal/supertrend/strategy/reset-flat", json={"strategy_id": "st_test"}, headers={"X-Internal-Token": "WRONG_TOKEN"})
    assert resp2.status_code == 403

    # Case 3: Valid token -> Processed (returns 400 because strategy_id is dummy, but auth succeeded)
    resp3 = client.post("/internal/supertrend/strategy/reset-flat", json={"strategy_id": "non_existent"}, headers={"X-Internal-Token": "SECRET_AUDIT_TOKEN_XYZ"})
    assert resp3.status_code == 200
    assert resp3.json().get("status") == "ERROR"
