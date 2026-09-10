import os
import sys
import pytest
import asyncio
from unittest.mock import MagicMock, patch

client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if client_dir not in sys.path:
    sys.path.insert(0, client_dir)

from supertrend_engine import MultiSuperTrendEngine, SingleSuperTrendRunner


@pytest.fixture
def mock_xts():
    xts = MagicMock()
    # Mock instrument master and resolver
    xts.resolve_contract.return_value = {
        "inst_id": 12345,
        "symbol": "GOLDPETAL 30SEP2026",
        "lot_size": 1,
        "exch_seg": "MCXFO"
    }
    xts.lookup_symbol.return_value = {
        "ExchangeInstrumentID": 12345,
        "ExchangeSegment": "MCXFO",
        "DisplayName": "GOLDPETAL 30SEP2026",
        "LotSize": 1,
    }
    xts.get_broker_positions_net.return_value = {
        "net_map": {12345: 0},
        "matched_qty": 0,
        "all_positions": [{"instrument_id": 12345, "symbol": "GOLDPETAL 30SEP2026", "quantity": 0}],
        "is_paper_trade": False
    }
    xts.send_ops_alert = MagicMock()
    xts.place_order = MagicMock(return_value={"status": "done", "order_id": "ST_AUTO_HEAL_1"})
    return xts


@pytest.fixture
def mock_main():
    main = MagicMock()
    main._dispatch_and_record = MagicMock(return_value={"status": "done", "type": "success"})
    return main


@pytest.mark.asyncio
async def test_reconcile_in_sync(mock_xts, mock_main):
    engine = MultiSuperTrendEngine()
    engine.add_or_update_strategy({
        "id": "st_goldpetal_20m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 2,
        "virtual_position": -2,
        "is_enabled": True
    })

    # Broker already has -2 lots
    mock_xts.get_broker_positions_net.return_value = {
        "net_map": {12345: -2},
        "matched_qty": -2,
        "all_positions": [{"instrument_id": 12345, "symbol": "GOLDPETAL 30SEP2026", "quantity": -2}],
        "is_paper_trade": False
    }

    res = await engine.reconcile_portfolio_drift(mock_xts, mock_main)
    assert res["status"] == "IN_SYNC"
    assert res["drift_count"] == 0
    assert len(res["actions"]) == 0
    mock_main._dispatch_and_record.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_positive_drift_heals_with_sell(mock_xts, mock_main):
    # Case like abk09: 20m Short 2, 30m Long 2 -> Net Target = 0
    # Broker holds +2 (orphan lots). Watchdog should SELL 2.
    engine = MultiSuperTrendEngine()
    engine.add_or_update_strategy({
        "id": "st_goldpetal_20m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 2,
        "virtual_position": -2,
        "is_enabled": True
    })
    engine.add_or_update_strategy({
        "id": "st_goldpetal_30m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "30m",
        "quantity": 2,
        "virtual_position": 2,
        "is_enabled": True
    })

    # Broker holds +2
    mock_xts.get_broker_positions_net.return_value = {
        "net_map": {12345: 2},
        "matched_qty": 2,
        "all_positions": [{"instrument_id": 12345, "symbol": "GOLDPETAL 30SEP2026", "quantity": 2}],
        "is_paper_trade": False
    }

    res = await engine.reconcile_portfolio_drift(mock_xts, mock_main)
    assert res["status"] == "RECONCILED"
    assert res["drift_count"] == 1
    assert len(res["actions"]) == 1
    action = res["actions"][0]
    assert action["action"] == "SELL"
    assert action["quantity"] == 2
    assert action["status"] == "FILLED"

    mock_main._dispatch_and_record.assert_called_once()
    call_args = mock_main._dispatch_and_record.call_args[0]
    # sig_id, action, target_sym, chunk_qty, price, chunk_ref, is_paper
    assert call_args[1] == "SELL"
    assert call_args[2] == "GOLDPETAL1!"
    assert call_args[3] == 2


@pytest.mark.asyncio
async def test_reconcile_negative_drift_heals_with_sell(mock_xts, mock_main):
    # Target is -4 lots (both strategies Short). Broker has 0.
    engine = MultiSuperTrendEngine()
    engine.add_or_update_strategy({
        "id": "st_goldpetal_20m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 2,
        "virtual_position": -2,
        "is_enabled": True
    })
    engine.add_or_update_strategy({
        "id": "st_goldpetal_30m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "30m",
        "quantity": 2,
        "virtual_position": -2,
        "is_enabled": True
    })

    # Broker has 0 lots
    mock_xts.get_broker_positions_net.return_value = {
        "net_map": {12345: 0},
        "matched_qty": 0,
        "all_positions": [{"instrument_id": 12345, "symbol": "GOLDPETAL 30SEP2026", "quantity": 0}],
        "is_paper_trade": False
    }

    res = await engine.reconcile_portfolio_drift(mock_xts, mock_main)
    assert res["status"] == "RECONCILED"
    assert res["drift_count"] == 1
    action = res["actions"][0]
    assert action["action"] == "SELL"
    assert action["quantity"] == 4
    assert action["status"] == "FILLED"


@pytest.mark.asyncio
async def test_reconcile_safety_cap_exceeded(mock_xts, mock_main):
    engine = MultiSuperTrendEngine()
    engine.add_or_update_strategy({
        "id": "st_goldpetal_20m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 2,
        "virtual_position": 0,
        "is_enabled": True
    })

    # Broker has +50 lots! (Safety cap is max(2*2, 10) = 10)
    mock_xts.get_broker_positions_net.return_value = {
        "net_map": {12345: 50},
        "matched_qty": 50,
        "all_positions": [{"instrument_id": 12345, "symbol": "GOLDPETAL 30SEP2026", "quantity": 50}],
        "is_paper_trade": False
    }

    res = await engine.reconcile_portfolio_drift(mock_xts, mock_main)
    assert res["status"] == "RECONCILED"
    assert len(res["actions"]) == 1
    action = res["actions"][0]
    assert action["status"] == "SAFETY_CAP_EXCEEDED"
    assert action["drift"] == 50
    assert action["max_allowed"] == 10

    # Ensure NO trade was dispatched!
    mock_main._dispatch_and_record.assert_not_called()
    mock_xts.place_order.assert_not_called()
    mock_xts.send_ops_alert.assert_called_once()
    assert "SAFETY CAP EXCEEDED" in mock_xts.send_ops_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_reconcile_order_rejected_by_broker(mock_xts, mock_main):
    engine = MultiSuperTrendEngine()
    engine.add_or_update_strategy({
        "id": "st_goldpetal_20m",
        "symbol": "GOLDPETAL1!",
        "timeframe": "20m",
        "quantity": 2,
        "virtual_position": -2,
        "is_enabled": True
    })

    # Broker has 0, target is -2 (Need SELL 2)
    mock_xts.get_broker_positions_net.return_value = {
        "net_map": {12345: 0},
        "matched_qty": 0,
        "all_positions": [{"instrument_id": 12345, "symbol": "GOLDPETAL 30SEP2026", "quantity": 0}],
        "is_paper_trade": False
    }

    # Simulate RMS margin reject
    mock_main._dispatch_and_record.return_value = {
        "status": "rejected",
        "type": "error",
        "description": "Margin Shortfall ₹598.39"
    }

    res = await engine.reconcile_portfolio_drift(mock_xts, mock_main)
    assert res["status"] == "RECONCILED"
    action = res["actions"][0]
    assert action["status"] == "REJECTED"
    assert action["action"] == "SELL"
    assert action["quantity"] == 2
    mock_xts.send_ops_alert.assert_called_once()
    assert "AUTO-HEAL ORDER REJECTED" in mock_xts.send_ops_alert.call_args[0][0]
