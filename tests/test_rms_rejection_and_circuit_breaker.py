import os
import sys
import json
import time
import pytest
import asyncio
from unittest.mock import MagicMock, patch

# Add client and root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from client import xts_api, main
from client.supertrend_engine import SingleSuperTrendRunner, MultiSuperTrendEngine

def test_verify_order_rms_acceptance_detects_rejection():
    """Verify that verify_order_rms_acceptance queries GET /orders and catches RMS rejection."""
    mock_orders_response = {
        "type": "success",
        "result": [
            {
                "OrderUniqueIdentifier": "ST_AUTO_HEAL_SILVER1001!_SELL_1789578600",
                "AppOrderID": "98765432",
                "OrderStatus": 56,  # 56 = Rejected
                "CancelRejectReason": "OEMS:RMS : Margin Exceeds : Set Limit:[68572.37] Total Required Margin:[69129.2] Available Margin[22868.17] Margin Shortfall[556.83]"
            }
        ]
    }
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_orders_response

    with patch.object(xts_api, "get_interactive_token", return_value="test_token"):
        with patch.object(xts_api.api_session, "get", return_value=mock_resp):
            res = xts_api.verify_order_rms_acceptance("ST_AUTO_HEAL_SILVER1001!_SELL_1789578600", wait_ms=0)
            assert res["verified"] is True
            assert res["is_rejected"] is True
            assert res["status"] == "Rejected"
            assert "Margin Shortfall[556.83]" in res["reject_reason"]

def test_verify_order_rms_acceptance_detects_filled():
    """Verify that verify_order_rms_acceptance handles filled orders properly."""
    mock_orders_response = {
        "type": "success",
        "result": [
            {
                "OrderUniqueIdentifier": "ST_REV_ENTRY_SILVER1001!_15M_1789578600",
                "AppOrderID": "98765433",
                "OrderStatus": 50,  # 50 = Filled
                "CancelRejectReason": ""
            }
        ]
    }
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_orders_response

    with patch.object(xts_api, "get_interactive_token", return_value="test_token"):
        with patch.object(xts_api.api_session, "get", return_value=mock_resp):
            res = xts_api.verify_order_rms_acceptance("ST_REV_ENTRY_SILVER1001!_15M_1789578600", wait_ms=0)
            assert res["verified"] is True
            assert res["is_rejected"] is False
            assert res["status"] == "Filled"

def test_place_order_returns_rejected_when_rms_rejects():
    """Verify place_order checks RMS verification post-dispatch and returns error if RMS rejects."""
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json.return_value = {
        "type": "success",
        "code": "s-orders-0001",
        "description": "API Order Id sent",
        "result": {"AppOrderID": "98765432"}
    }

    mock_rms_res = {
        "verified": True,
        "status": "Rejected",
        "is_rejected": True,
        "reject_reason": "OEMS:RMS : Margin Exceeds : Margin Shortfall[556.83]",
        "order_data": {}
    }

    with patch.object(xts_api.config, "PAPER_TRADE_MODE", False):
        with patch.object(xts_api.config, "CLIENT_ID", "TEST_CLIENT"):
            with patch.object(xts_api, "get_interactive_token", return_value="test_token"):
                with patch.object(xts_api, "get_dynamic_contract_info", return_value=(574824, "MCXFO", "NRML", 1.0, 1, 100, "2026-09-30")):
                    with patch.object(xts_api, "get_live_price", return_value=2315.0):
                        with patch.object(xts_api.ORDER_RATE_LIMITER, "acquire", return_value=True):
                            with patch.object(xts_api.api_session, "post", return_value=post_resp):
                                with patch.object(xts_api, "verify_order_rms_acceptance", return_value=mock_rms_res):
                                    res = xts_api.place_order("SELL", "SILVER1001!", 8, 2315.0, "ORDER_REF_TEST", is_paper=False)
                                    assert res["status"] == "rejected"
                                    assert res["code"] == "e-rms-rejected"
                                    assert "Margin Shortfall" in res["reject_reason"]

def test_execute_trade_with_retry_refuses_retry_on_rms_rejection():
    """Verify execute_trade_with_retry does NOT retry when RMS rejects for margin."""
    mock_place_order = MagicMock(return_value={
        "type": "error",
        "status": "rejected",
        "code": "e-rms-rejected",
        "description": "OEMS:RMS : Margin Exceeds : Margin Shortfall[556.83]",
        "reject_reason": "OEMS:RMS : Margin Exceeds : Margin Shortfall[556.83]"
    })

    with patch.object(xts_api, "place_order", mock_place_order):
        with patch.object(xts_api, "send_ops_alert", MagicMock()) as mock_alert:
            res = xts_api.execute_trade_with_retry("SELL", "SILVER1001!", 8, 2315.0, "ORDER_REF_TEST", attempt=1, is_paper=False)
            assert res["status"] == "rejected"
            # Should have called place_order EXACTLY once (no retry!)
            assert mock_place_order.call_count == 1
            assert mock_alert.call_count >= 1

def test_dispatch_and_record_marks_failed_on_rejection():
    """Verify _dispatch_and_record marks signal failed and passes reject reason."""
    mock_trade_res = {
        "type": "error",
        "status": "rejected",
        "code": "e-rms-rejected",
        "description": "OEMS:RMS : Margin Exceeds : Margin Shortfall[556.83]",
        "reject_reason": "OEMS:RMS : Margin Exceeds : Margin Shortfall[556.83]",
        "result": {"AppOrderID": "98765432"}
    }

    with patch.object(main.config, "PAPER_TRADE_MODE", False):
        with patch.object(main.xts_api, "execute_trade_with_retry", return_value=mock_trade_res):
            with patch.object(main, "db_update_status", MagicMock()) as mock_db:
                with patch.object(main, "send_execution_notification", MagicMock()):
                    res = main._dispatch_and_record("sig_123", "SELL", "SILVER1001!", 8, 2315.0, "ORDER_REF_TEST")
                    assert res["status"] == "failed"
                    assert res["is_rejected"] is True
                    assert "Margin Shortfall[556.83]" in res["reject_reason"]
                    mock_db.assert_any_call("sig_123", "failed", res["result"])

@pytest.mark.asyncio
async def test_reconcile_portfolio_drift_circuit_breaker_on_margin_shortfall():
    """
    Verify that when auto-heal order is rejected due to RMS margin shortfall:
    1. Circuit breaker trips (paused = True).
    2. Runner virtual_position is reset to actual broker lots (0).
    3. Runner status transitions to MARGIN_SHORTFALL_PAUSED.
    4. Subsequent drift checks are suppressed.
    """
    engine = MultiSuperTrendEngine(max_strategies=2)
    runner = SingleSuperTrendRunner({
        "symbol": "SILVER1001!",
        "timeframe": "15m",
        "quantity": 8,
        "is_enabled": True,
        "execution_mode": "LIVE"
    })
    runner.virtual_position = -8  # Strategy thinks it should be short 8
    runner.strategy_position = "SHORT"
    engine.strategies["SILVER1001!_15m"] = runner

    mock_xts = MagicMock()
    mock_xts.resolve_contract.return_value = {"inst_id": 574824, "lot_size": 1, "freeze_qty": 100}
    mock_xts.get_broker_orders.return_value = []
    # Broker has 0 lots (FLAT)
    mock_xts.get_broker_positions_net.return_value = {
        "is_paper_trade": False,
        "all_positions": [{"instrument_id": 574824, "quantity": 0}]
    }
    mock_xts.send_ops_alert = MagicMock()

    mock_main = MagicMock()
    # Mock dispatch returning RMS rejection
    mock_main._dispatch_and_record.return_value = {
        "status": "failed",
        "is_rejected": True,
        "code": "e-rms-rejected",
        "reject_reason": "OEMS:RMS : Margin Exceeds : Margin Shortfall[556.83]",
        "description": "OEMS:RMS : Margin Exceeds : Margin Shortfall[556.83]"
    }

    # Iteration 1: Closed-loop drift detected (0 != -8), tries auto-heal, gets RMS rejected
    res1 = await engine.reconcile_portfolio_drift(mock_xts, mock_main)
    assert res1["status"] == "RECONCILED"
    assert res1["drift_count"] == 1
    assert res1["actions"][0]["status"] == "REJECTED"

    # Verify Circuit Breaker State
    assert runner.virtual_position == 0  # Extinguished drift
    assert runner.strategy_position == "FLAT"
    assert runner.status == "MARGIN_SHORTFALL_PAUSED"
    assert "RMS Margin Shortfall" in runner.last_error
    assert engine._drift_rejection_history["SILVER1001!"]["paused"] is True

    # Iteration 2: Watchdog wakes up again. Must suppress auto-heal completely!
    mock_main._dispatch_and_record.reset_mock()
    res2 = await engine.reconcile_portfolio_drift(mock_xts, mock_main)
    # Zero orders dispatched!
    assert mock_main._dispatch_and_record.call_count == 0
