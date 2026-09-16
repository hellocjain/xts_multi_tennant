import time
import os
import sys
import pytest
from unittest.mock import MagicMock

# Add client and root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "portal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from client import xts_api
from portal import telemetry_service

def test_margin_telemetry_negative_notional_hold_and_unified_clamp():
    """
    Reproduces the exact ABK04 broker payload from Symphony XTS:
    - Gross MCX Cash: ₹1,73,184.37
    - NotinalCash: -₹104,612.00
    - Unified Limit: ₹68,572.37
    - Margin Used: ₹45,446.36
    """
    mock_balance_response = {
        "type": "success",
        "code": "s-user-0002",
        "description": "OK",
        "result": {
            "BalanceList": [
                {
                    "limitHeader": "ALL|ALL|ALL",
                    "limitObject": {
                        "RMSSubLimits": {
                            "cashAvailable": "68572.37",
                            "collateral": 0,
                            "marginUtilized": "45446.35625",
                            "netMarginAvailable": "23126.01375",
                            "MTM": "-557.5",
                            "UnrealizedMTM": "0",
                            "RealizedMTM": "-557.5"
                        },
                        "marginAvailable": {
                            "CashMarginAvailable": "68572.37",
                            "NotinalCash": "0",
                            "PayInAmount": "0",
                            "AdhocMargin": "0"
                        }
                    }
                },
                {
                    "limitHeader": "COMMODITIES|MCX|ALL",
                    "limitObject": {
                        "RMSSubLimits": {
                            "cashAvailable": "173184.37",
                            "collateral": 0,
                            "marginUtilized": "45446.35625",
                            "netMarginAvailable": "23126.01375",
                            "MTM": "-557.5",
                            "UnrealizedMTM": "0",
                            "RealizedMTM": "-557.5"
                        },
                        "marginAvailable": {
                            "CashMarginAvailable": "173184.37",
                            "NotinalCash": "-104612",
                            "PayInAmount": "0",
                            "AdhocMargin": "0"
                        }
                    }
                }
            ]
        }
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_balance_response

    xts_api.INTERACTIVE_TOKEN = "test_token_123"
    xts_api.INTERACTIVE_TOKEN_ACQUIRED_AT = time.time()
    xts_api.config.PAPER_TRADE_MODE = False
    orig_get = xts_api.api_session.get
    xts_api.api_session.get = MagicMock(return_value=mock_resp)

    try:
        margin = xts_api.get_margin_telemetry()
        
        # Verify True Usable Limit is clamped to broker's unified limit ₹68,572.37
        assert round(margin["available_margin"], 2) == 68572.37
        assert round(margin["broker_rms_limit"], 2) == 68572.37
        
        # Verify Gross MCX cash is preserved for reporting
        assert round(margin["gross_mcx_cash"], 2) == 173184.37
        
        # Verify Broker Hold detection
        assert round(margin["broker_hold_amount"], 2) == 104612.00
        assert margin["has_broker_hold"] is True
        
        # Verify Net Headroom and utilization
        assert round(margin["margin_used"], 2) == 45446.36
        assert round(margin["net_margin_available"], 2) == 23126.01
        assert round(margin["free_headroom"], 2) == 23126.01
        assert margin["margin_utilization_pct"] == 66.3
        assert margin["margin_health_status"] == "HEALTHY" # 23126 / 68572 = 33.7% > 30%
    finally:
        xts_api.api_session.get = orig_get


def test_margin_telemetry_normal_account_no_holds():
    """Verifies that normal accounts (like ABK01) with zero notional hold report 100% usable margin."""
    mock_balance_response = {
        "type": "success",
        "code": "s-user-0002",
        "result": {
            "BalanceList": [
                {
                    "limitHeader": "ALL|ALL|ALL",
                    "limitObject": {
                        "RMSSubLimits": {
                            "cashAvailable": "39760.31",
                            "collateral": 0,
                            "marginUtilized": "12000.00",
                            "netMarginAvailable": "27760.31"
                        },
                        "marginAvailable": {
                            "CashMarginAvailable": "39760.31",
                            "NotinalCash": "0",
                            "PayInAmount": "0"
                        }
                    }
                },
                {
                    "limitHeader": "COMMODITIES|MCX|ALL",
                    "limitObject": {
                        "RMSSubLimits": {
                            "cashAvailable": "39760.31",
                            "collateral": 0,
                            "marginUtilized": "12000.00",
                            "netMarginAvailable": "27760.31"
                        },
                        "marginAvailable": {
                            "CashMarginAvailable": "39760.31",
                            "NotinalCash": "0",
                            "PayInAmount": "0"
                        }
                    }
                }
            ]
        }
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_balance_response

    xts_api.INTERACTIVE_TOKEN = "test_token_123"
    xts_api.INTERACTIVE_TOKEN_ACQUIRED_AT = time.time()
    xts_api.config.PAPER_TRADE_MODE = False
    orig_get = xts_api.api_session.get
    xts_api.api_session.get = MagicMock(return_value=mock_resp)

    try:
        margin = xts_api.get_margin_telemetry()
        assert round(margin["available_margin"], 2) == 39760.31
        assert round(margin["broker_rms_limit"], 2) == 39760.31
        assert margin["has_broker_hold"] is False
        assert margin["broker_hold_amount"] == 0.0
        assert round(margin["free_headroom"], 2) == 27760.31
        assert margin["margin_health_status"] == "HEALTHY"
    finally:
        xts_api.api_session.get = orig_get


def test_margin_health_status_transitions():
    """Verifies that health status transitions properly between HEALTHY, WARNING, and CRITICAL."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "type": "success",
        "result": {
            "BalanceList": [{
                "limitHeader": "ALL|ALL|ALL",
                "limitObject": {
                    "RMSSubLimits": {"cashAvailable": "50000.0", "marginUtilized": "48000.0", "netMarginAvailable": "2000.0"},
                    "marginAvailable": {"CashMarginAvailable": "50000.0", "NotinalCash": "0"}
                }
            }]
        }
    }
    xts_api.INTERACTIVE_TOKEN = "test_token_123"
    xts_api.INTERACTIVE_TOKEN_ACQUIRED_AT = time.time()
    xts_api.config.PAPER_TRADE_MODE = False
    orig_get = xts_api.api_session.get
    xts_api.api_session.get = MagicMock(return_value=mock_resp)

    try:
        # Free headroom is 2,000 / 50,000 = 4% -> CRITICAL (<10%)
        margin = xts_api.get_margin_telemetry()
        assert margin["margin_health_status"] == "CRITICAL"
        assert margin["free_headroom"] == 2000.0

        # Warning: 20% free headroom
        mock_resp.json.return_value["result"]["BalanceList"][0]["limitObject"]["RMSSubLimits"]["marginUtilized"] = "40000.0"
        mock_resp.json.return_value["result"]["BalanceList"][0]["limitObject"]["RMSSubLimits"]["netMarginAvailable"] = "10000.0"
        margin = xts_api.get_margin_telemetry()
        assert margin["margin_health_status"] == "WARNING"
    finally:
        xts_api.api_session.get = orig_get


def test_portal_telemetry_service_build_dict():
    """Verifies that portal telemetry_service correctly formats and serializes the new margin fields."""
    client_dict = telemetry_service.build_client_telemetry_dict(
        tenant_id="abk04",
        name="Client ABK04",
        client_id="ABK04",
        available_margin=68572.37,
        broker_rms_limit=68572.37,
        gross_mcx_cash=173184.37,
        broker_hold_amount=104612.0,
        has_broker_hold=True,
        margin_used=45446.36,
        net_margin_available=23126.01,
        free_headroom=23126.01,
        margin_utilization_pct=66.3,
        margin_health_status="HEALTHY"
    )

    assert client_dict["available_margin"] == 68572.37
    assert client_dict["broker_rms_limit"] == 68572.37
    assert client_dict["gross_mcx_cash"] == 173184.37
    assert client_dict["broker_hold_amount"] == 104612.0
    assert client_dict["has_broker_hold"] is True
    assert client_dict["free_headroom"] == 23126.01
    assert client_dict["margin_health_status"] == "HEALTHY"
