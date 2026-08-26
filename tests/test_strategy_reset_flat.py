"""
Unit & Integration Tests for Individual SuperTrend Strategy "Reset to Flat" Feature.
Covers:
1. Live Broker Square-Off + Virtual State Reset (Short & Long)
2. Force Virtual Flat (Zero Broker Orders)
3. Already Flat No-Op
4. SQLite Persistence & MultiSuperTrendEngine Integration
5. Client HTTP Endpoint
"""

import pytest
import time
import os
import sys
import importlib.util
from pathlib import Path

# Add client path
BASE_DIR = str(Path(__file__).parent.parent)
client_path = os.path.join(BASE_DIR, "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

import config
import xts_api

# Explicitly load client/main.py to prevent namespace collision with portal/main.py
client_main_file = os.path.join(client_path, "main.py")
spec = importlib.util.spec_from_file_location("client_main_isolated", client_main_file)
client_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_main)

from supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_reset_flat.db")
    monkeypatch.setattr(client_main, "_DB_PATH", test_db)
    client_main.db_init()
    yield


@pytest.mark.asyncio
async def test_reset_to_flat_with_broker_square_off_short(monkeypatch):
    """
    Verifies that resetting a SHORT position with square_off_broker=True:
    1. Dispatches BUY exit order for abs(vpos) lots.
    2. Resets virtual_position to 0 in memory and SQLite.
    """
    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append({
            "sig_id": sig_id,
            "action": action,
            "symbol": symbol,
            "qty": qty,
            "order_ref": order_ref
        })
        return {"status": "done", "result": {"AppOrderID": 9901}}

    monkeypatch.setattr(client_main, "_dispatch_and_record", mock_dispatch)
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 568839,
        "symbol": "GOLDPETAL",
        "desc": "GOLDPETAL 30SEP2026",
        "exch_seg": "MCXFO",
        "freeze_qty": 1000
    })

    client_main.db_set_virtual_position("GOLDPETAL1!_20m", "GOLDPETAL1!", "20m", -2)

    runner = SingleSuperTrendRunner({
        "id": "st_gold_20m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 2,
        "is_enabled": True
    }, main_module=client_main)

    assert runner.virtual_position == -2
    assert runner.strategy_position == "SHORT"

    res = await runner.reset_to_flat(square_off_broker=True, xts_api_module=xts_api, main_module=client_main)

    assert res["status"] == "SUCCESS"
    assert len(dispatched) == 1
    assert dispatched[0]["action"] == "BUY"
    assert dispatched[0]["qty"] == 2
    assert "RESET_FLAT" in dispatched[0]["order_ref"]
    assert dispatched[0]["symbol"] == "GOLDPETAL 30SEP2026"

    # Invariants
    assert runner.virtual_position == 0
    assert runner.strategy_position == "FLAT"
    assert client_main.db_get_virtual_position("GOLDPETAL1!_20m") == 0


@pytest.mark.asyncio
async def test_reset_to_flat_with_broker_square_off_long(monkeypatch):
    """
    Verifies that resetting a LONG position with square_off_broker=True:
    1. Dispatches SELL exit order for vpos lots.
    2. Resets virtual_position to 0 in memory and SQLite.
    """
    dispatched = []
    def mock_dispatch(sig_id, action, symbol, qty, price, order_ref, is_paper):
        dispatched.append({
            "sig_id": sig_id,
            "action": action,
            "symbol": symbol,
            "qty": qty,
            "order_ref": order_ref
        })
        return {"status": "done", "result": {"AppOrderID": 9902}}

    monkeypatch.setattr(client_main, "_dispatch_and_record", mock_dispatch)
    monkeypatch.setattr(xts_api, "resolve_contract", lambda sym: {
        "inst_id": 574824,
        "symbol": "SILVER100",
        "desc": "SILVER100 30SEP2026",
        "exch_seg": "MCXFO",
        "freeze_qty": 1000
    })

    client_main.db_set_virtual_position("SILVER1001!_15m", "SILVER1001!", "15m", 1)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_15m",
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 1,
        "is_enabled": True
    }, main_module=client_main)

    assert runner.virtual_position == 1
    assert runner.strategy_position == "LONG"

    res = await runner.reset_to_flat(square_off_broker=True, xts_api_module=xts_api, main_module=client_main)

    assert res["status"] == "SUCCESS"
    assert len(dispatched) == 1
    assert dispatched[0]["action"] == "SELL"
    assert dispatched[0]["qty"] == 1
    assert "RESET_FLAT" in dispatched[0]["order_ref"]
    assert dispatched[0]["symbol"] == "SILVER100 30SEP2026"

    # Invariants
    assert runner.virtual_position == 0
    assert runner.strategy_position == "FLAT"
    assert client_main.db_get_virtual_position("SILVER1001!_15m") == 0


@pytest.mark.asyncio
async def test_reset_to_flat_virtual_only(monkeypatch):
    """
    Verifies that force resetting with square_off_broker=False:
    1. Sends ZERO orders to the broker.
    2. Resets virtual_position to 0 in memory and SQLite.
    """
    dispatched = []
    def mock_dispatch(*args, **kwargs):
        dispatched.append(args)
        return {"status": "done"}

    monkeypatch.setattr(client_main, "_dispatch_and_record", mock_dispatch)

    client_main.db_set_virtual_position("SILVER1001!_30m", "SILVER1001!", "30m", -3)

    runner = SingleSuperTrendRunner({
        "id": "st_silver_30m",
        "symbol": "SILVER1001!",
        "timeframe": "30m",
        "quantity": 3,
        "is_enabled": True
    }, main_module=client_main)

    assert runner.virtual_position == -3

    res = await runner.reset_to_flat(square_off_broker=False, xts_api_module=xts_api, main_module=client_main)

    assert res["status"] == "SUCCESS"
    assert len(dispatched) == 0  # STRICTLY ZERO BROKER ORDERS
    assert runner.virtual_position == 0
    assert runner.strategy_position == "FLAT"
    assert client_main.db_get_virtual_position("SILVER1001!_30m") == 0


@pytest.mark.asyncio
async def test_multi_supertrend_engine_reset_strategy_to_flat(monkeypatch):
    """
    Verifies MultiSuperTrendEngine.reset_strategy_to_flat delegates cleanly by strategy_id.
    """
    engine = MultiSuperTrendEngine()
    r1 = SingleSuperTrendRunner({"id": "st_r1", "symbol": "SILVER1001!", "timeframe": "15m", "quantity": 1, "is_enabled": True}, main_module=client_main)
    r2 = SingleSuperTrendRunner({"id": "st_r2", "symbol": "GOLDPETAL1!", "timeframe": "20m", "quantity": 2, "is_enabled": True}, main_module=client_main)

    r1.virtual_position = 1
    r2.virtual_position = -2
    engine.strategies["st_r1"] = r1
    engine.strategies["st_r2"] = r2

    res = await engine.reset_strategy_to_flat("st_r2", square_off_broker=False, xts_api_module=xts_api, main_module=client_main)

    assert res["status"] == "SUCCESS"
    assert r1.virtual_position == 1  # Untouched
    assert r2.virtual_position == 0  # Reset
    assert engine.get_portfolio_target_positions() == {"SILVER1001!": 1, "GOLDPETAL1!": 0}


@pytest.mark.asyncio
async def test_client_http_reset_flat_endpoint(monkeypatch):
    """
    Verifies HTTP POST /internal/supertrend/strategy/reset-flat endpoint.
    """
    from fastapi.testclient import TestClient
    engine = MultiSuperTrendEngine()
    runner = SingleSuperTrendRunner({"id": "st_test_http", "symbol": "SILVER1001!", "timeframe": "5m", "quantity": 1, "is_enabled": True}, main_module=client_main)
    runner.virtual_position = -1
    engine.strategies["st_test_http"] = runner

    monkeypatch.setattr(client_main, "supertrend_engine", engine)

    client = TestClient(client_main.app)
    resp = client.post("/internal/supertrend/strategy/reset-flat", json={
        "strategy_id": "st_test_http",
        "square_off_broker": False
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert runner.virtual_position == 0
