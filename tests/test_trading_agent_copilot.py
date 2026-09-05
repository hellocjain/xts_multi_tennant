"""
tests/test_trading_agent_copilot.py
Comprehensive automated test suite for Marketcalls TradingAgent Copilot integration:
1. Pure-Math Price Action Calculations (Support/Resistance, Swing Channel, Fibonacci)
2. Deterministic 4-Layer RiskGuard Engine (Anti-duplicate, Fat-finger, Affordability, Index refusal)
3. Two-Phase Order Approval Card Generation & Lifecycle
4. SSE Streaming Endpoint (/api/v1/agent/stream) with Immediate Action Dispatch
5. Order Approval Execution Endpoint (/api/v1/agent/approve-order)
6. Chart Math Endpoint (/api/v1/agent/chart-math)
7. Template UI Elements and JS Controller Integration
"""

import os
import sys
import json
import pytest
from fastapi.testclient import TestClient

# Ensure client and portal are accessible on python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal")))

import client.main as client_main
import client.trading_agent_service as agent_service


@pytest.fixture
def terminal_client():
    return TestClient(client_main.app)


def generate_synthetic_candles(num_bars=50, trend="up", base_price=24000.0):
    candles = []
    price = base_price
    for i in range(num_bars):
        step = 15.0 if trend == "up" else (-15.0 if trend == "down" else 0.0)
        import math
        osc = math.sin(i / 3.0) * 35.0
        open_p = price + step + osc
        high_p = open_p + 25.0
        low_p = open_p - 20.0
        close_p = open_p + 10.0
        candles.append({
            "time": 1700000000 + i * 300,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": 1000 + i * 10
        })
        price = close_p
    return candles


# =============================================================================
# 1. Pure-Math Price Action Engine Tests
# =============================================================================

def test_support_resistance_calculation():
    candles = generate_synthetic_candles(num_bars=60, base_price=24000.0)
    res = agent_service.compute_support_resistance(candles)

    assert "current_price" in res
    assert res["current_price"] > 0
    assert "supports" in res
    assert "resistances" in res
    assert "levels" in res
    assert len(res["levels"]) > 0

    # Ensure supports are below current price and resistances are above current price
    curr = res["current_price"]
    for s in res["supports"]:
        assert s < curr
    for r in res["resistances"]:
        assert r > curr


def test_swing_channel_calculation():
    candles = generate_synthetic_candles(num_bars=50, trend="up", base_price=24000.0)
    res = agent_service.compute_swing_channel(candles)

    assert "upper_rail" in res
    assert "lower_rail" in res
    assert "slope" in res
    assert "structure" in res

    # In an uptrend, slope should be positive
    assert res["slope"] > 0
    assert "channel" in res["structure"].lower()

    upper = res["upper_rail"]
    lower = res["lower_rail"]
    assert upper["start_price"] > lower["start_price"]
    assert upper["end_price"] > lower["end_price"]


def test_fibonacci_level_calculation():
    candles = generate_synthetic_candles(num_bars=40, base_price=20000.0)
    res = agent_service.compute_fibonacci_levels(candles)

    assert "swing_high" in res
    assert "swing_low" in res
    assert "levels" in res
    assert len(res["levels"]) == 7

    high = res["swing_high"]
    low = res["swing_low"]
    assert high > low

    # 50% retracement should be exact midpoint
    mid_lvl = [lvl for lvl in res["levels"] if lvl["ratio"] == 0.5][0]
    expected_mid = round(low + (high - low) * 0.5, 2)
    assert abs(mid_lvl["price"] - expected_mid) < 0.05


# =============================================================================
# 2. Deterministic RiskGuard Engine Tests
# =============================================================================

def test_riskguard_refuses_cash_index():
    passed, reason = agent_service.validate_order_risk(
        tenant_id="tenant_test",
        order_data={"symbol": "NIFTY 50", "action": "BUY", "quantity": 50, "product": "MIS"},
        live_ltp=24500.0,
        available_funds=1000000.0
    )
    assert passed is False
    assert "cash index and cannot be traded" in reason


def test_riskguard_anti_duplicate_10s_guard():
    tenant = "tenant_test_dup"
    sym = "TCS"
    act = "BUY"

    # 1. First order validation passes
    passed, _ = agent_service.validate_order_risk(
        tenant_id=tenant,
        order_data={"symbol": sym, "action": act, "quantity": 10, "product": "CNC"},
        live_ltp=3900.0,
        available_funds=500000.0
    )
    assert passed is True

    # 2. Record execution
    agent_service.record_order_execution(tenant, sym, act)

    # 3. Second duplicate order immediately afterwards is blocked
    passed2, reason2 = agent_service.validate_order_risk(
        tenant_id=tenant,
        order_data={"symbol": sym, "action": act, "quantity": 10, "product": "CNC"},
        live_ltp=3900.0,
        available_funds=500000.0
    )
    assert passed2 is False
    assert "Duplicate order rejected" in reason2
    assert "within 10 seconds" in reason2


def test_riskguard_fat_finger_limit_price():
    # Live LTP = 1000. Limit price = 1250 (25% deviation -> triggers fat-finger guard)
    passed, reason = agent_service.validate_order_risk(
        tenant_id="tenant_test",
        order_data={"symbol": "INFY", "action": "BUY", "quantity": 10, "order_type": "LIMIT", "price": 1250.0},
        live_ltp=1000.0,
        available_funds=500000.0
    )
    assert passed is False
    assert "Fat-finger guard triggered" in reason
    assert ">20% limit" in reason

    # Normal limit price within 5% deviation passes
    passed_ok, _ = agent_service.validate_order_risk(
        tenant_id="tenant_test",
        order_data={"symbol": "INFY", "action": "BUY", "quantity": 10, "order_type": "LIMIT", "price": 1020.0},
        live_ltp=1000.0,
        available_funds=500000.0
    )
    assert passed_ok is True


def test_riskguard_affordability_funds_check():
    # Available funds = 1,000. Order notional = 500,000 -> exceeds 90% funds
    passed, reason = agent_service.validate_order_risk(
        tenant_id="tenant_test",
        order_data={"symbol": "RELIANCE", "action": "BUY", "quantity": 100, "product": "CNC"},
        live_ltp=3000.0,
        available_funds=1000.0
    )
    assert passed is False
    assert "Insufficient funds" in reason
    assert "exceeds 90%" in reason


# =============================================================================
# 3. Approval Card Generator & Lifecycle Tests
# =============================================================================

def test_build_approval_card_structure():
    card = agent_service.build_approval_card(
        tenant_id="tenant_demo",
        action="BUY",
        symbol="TATAMOTORS",
        exchange="NSE",
        quantity=25,
        order_type="MARKET",
        price=0.0,
        product="CNC",
        live_ltp=980.50,
        available_funds=150000.0,
        is_paper=True
    )

    assert card["action"] == "BUY"
    assert card["symbol"] == "TATAMOTORS"
    assert card["quantity"] == 25
    assert card["mode"] == "paper"
    assert "checked_by_server" in card
    chk = card["checked_by_server"]
    assert chk["ltp"] == 980.50
    assert chk["notional"] == round(980.50 * 25, 2)
    assert chk["available_funds"] == 150000.0


# =============================================================================
# 4. SSE Stream Endpoint Tests (/api/v1/agent/stream)
# =============================================================================

def test_agent_stream_order_drafting(terminal_client):
    payload = {
        "prompt": "Buy 10 shares of RELIANCE at market, CNC",
        "symbol": "RELIANCE",
        "exchange": "NSE"
    }
    resp = terminal_client.post("/api/v1/agent/stream", json=payload)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    content = resp.text
    assert "approval_card" in content
    assert "RELIANCE" in content
    assert "checked_by_server" in content


def test_agent_stream_draw_channel(terminal_client):
    candles = generate_synthetic_candles(num_bars=30)
    payload = {
        "prompt": "Draw channel connecting visible highs and lows",
        "symbol": "NIFTY",
        "interval": "5m",
        "candles": candles
    }
    resp = terminal_client.post("/api/v1/agent/stream", json=payload)
    assert resp.status_code == 200

    content = resp.text
    assert "chart_action" in content
    assert "draw_channel" in content
    assert "upper_rail" in content
    assert "lower_rail" in content


def test_agent_stream_support_resistance(terminal_client):
    candles = generate_synthetic_candles(num_bars=30)
    payload = {
        "prompt": "Mark support and resistance levels",
        "symbol": "BANKNIFTY",
        "interval": "15m",
        "candles": candles
    }
    resp = terminal_client.post("/api/v1/agent/stream", json=payload)
    assert resp.status_code == 200

    content = resp.text
    assert "draw_support_resistance" in content
    assert "levels" in content


def test_agent_stream_add_indicator(terminal_client):
    payload = {
        "prompt": "Add supertrend 10, 3",
        "symbol": "SBIN"
    }
    resp = terminal_client.post("/api/v1/agent/stream", json=payload)
    assert resp.status_code == 200

    content = resp.text
    assert "add_indicator" in content
    assert "SuperTrend" in content


# =============================================================================
# 5. Order Approval Execution Endpoint (/api/v1/agent/approve-order)
# =============================================================================

def test_agent_approve_order_endpoint(terminal_client):
    payload = {
        "card_id": "ac_test_123",
        "symbol": "WIPRO",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 5,
        "order_type": "MARKET",
        "price": 0.0,
        "product": "NRML",
        "apikey": "test"
    }
    resp = terminal_client.post("/api/v1/agent/approve-order", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "orderid" in data
    assert data["symbol"] == "WIPRO"
    assert data["action"] == "BUY"


def test_agent_chart_math_endpoint(terminal_client):
    candles = generate_synthetic_candles(num_bars=25)
    payload = {
        "type": "channel",
        "symbol": "NIFTY",
        "candles": candles
    }
    resp = terminal_client.post("/api/v1/agent/chart-math", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "upper_rail" in data["data"]


# =============================================================================
# 6. Template Elements & UI Integration Tests
# =============================================================================

def test_copilot_template_elements_in_trading_page():
    template_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal", "templates", "client_trading.html"))
    js_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal", "static", "js", "trading_terminal.js"))

    with open(template_path, "r") as f:
        html = f.read()
    with open(js_path, "r") as f:
        js = f.read()

    # Right Rail Copilot button
    assert 'id="btn-rail-agent"' in html

    # Slide-out Panel
    assert 'id="oa-panel-agent"' in html
    assert 'id="btn-clear-agent-chat"' in html
    assert 'id="btn-close-agent-panel"' in html
    assert 'id="agent-chat-messages"' in html
    assert 'id="agent-prompt-input"' in html
    assert 'id="btn-send-agent-prompt"' in html
    assert 'agent-chip' in html

    # JS Methods
    assert 'bindAgentCopilot' in js
    assert 'sendAgentPrompt' in js
    assert 'executeAgentChartAction' in js
    assert 'renderApprovalCard' in js
    assert 'handleApproveOrder' in js
