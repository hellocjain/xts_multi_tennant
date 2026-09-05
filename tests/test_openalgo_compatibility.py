import pytest
import sys
from pathlib import Path
from fastapi.testclient import TestClient

import os
import importlib.util

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

@pytest.fixture
def client(monkeypatch):
    import config
    import xts_api
    import order_services
    import openalgo_router

    # Ensure config secrets and trading mode are actively set during the test
    openalgo_router.set_current_trading_mode("PAPER")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "TEST_SECRET_KEY_123")
    monkeypatch.setattr(config, "API_KEY", "TEST_SECRET_KEY_123")
    monkeypatch.setattr(config, "PAPER_TRADE_MODE", True)
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(config, "ANALYZER_MODE", False)

    monkeypatch.setattr(openalgo_router.config, "WEBHOOK_SECRET", "TEST_SECRET_KEY_123")
    monkeypatch.setattr(openalgo_router.config, "API_KEY", "TEST_SECRET_KEY_123")
    monkeypatch.setattr(openalgo_router.config, "PAPER_TRADE_MODE", True)
    monkeypatch.setattr(openalgo_router.config, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(openalgo_router.config, "ANALYZER_MODE", False)

    # Mock XTS calls for deterministic testing across all module references
    mock_execute = lambda action, symbol, qty, price, order_ref, is_paper=True, attempt=1: {
        "type": "success",
        "status": "success",
        "result": {
            "AppOrderID": 887766,
            "OrderAverageTradedPrice": price or 850.0,
            "IsPaperTrade": True
        }
    }
    monkeypatch.setattr(xts_api, "execute_trade_with_retry", mock_execute)
    monkeypatch.setattr(openalgo_router.xts_api, "execute_trade_with_retry", mock_execute)
    monkeypatch.setattr(order_services.xts_api, "execute_trade_with_retry", mock_execute)

    mock_positions = lambda: {
        "positions": [{
            "symbol": "CRUDEOIL 19MAR2026",
            "quantity": 100,
            "buy_avg": 6500.0,
            "sell_avg": 0.0,
            "net_mtm": 2500.0
        }],
        "net_mtm": 2500.0
    }
    monkeypatch.setattr(xts_api, "get_positions_telemetry", mock_positions)
    monkeypatch.setattr(openalgo_router.xts_api, "get_positions_telemetry", mock_positions)

    mock_orders = lambda: [
        {"AppOrderID": "887766", "OrderStatus": "Filled", "TradingSymbol": "CRUDEOIL 19MAR2026", "OrderQuantity": 100}
    ]
    monkeypatch.setattr(xts_api, "get_broker_orders", mock_orders)
    monkeypatch.setattr(openalgo_router.xts_api, "get_broker_orders", mock_orders)

    mock_trades = lambda: [
        {"AppOrderID": "887766", "TradingSymbol": "CRUDEOIL 19MAR2026", "TradePrice": 6500.0}
    ]
    monkeypatch.setattr(xts_api, "get_broker_trades", mock_trades)
    monkeypatch.setattr(openalgo_router.xts_api, "get_broker_trades", mock_trades)

    mock_margin = lambda: {
        "available_margin": 150000.0,
        "used_margin": 50000.0,
        "total_balance": 200000.0
    }
    monkeypatch.setattr(xts_api, "get_margin_telemetry", mock_margin)
    monkeypatch.setattr(openalgo_router.xts_api, "get_margin_telemetry", mock_margin)

    mock_holdings = lambda: {
        "holdings": [{"symbol": "INFY", "quantity": 25, "ltp": 1850.0}]
    }
    monkeypatch.setattr(xts_api, "get_holdings_telemetry", mock_holdings)
    monkeypatch.setattr(openalgo_router.xts_api, "get_holdings_telemetry", mock_holdings)

    return TestClient(client_main.app)

def test_ping_endpoint(client):
    res = client.get("/api/v1/ping")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "pong" in data["message"]

def test_place_order_unauthorized(client):
    payload = {
        "apikey": "WRONG_SECRET",
        "symbol": "CRUDEOIL1!",
        "action": "BUY",
        "quantity": 100,
        "price": 6500.0
    }
    res = client.post("/api/v1/placeorder", json=payload)
    assert res.status_code == 401
    assert res.json()["status"] == "error"

def test_place_order_authorized(client):
    payload = {
        "apikey": "TEST_SECRET_KEY_123",
        "symbol": "CRUDEOIL1!",
        "action": "BUY",
        "quantity": 100,
        "price": 6500.0
    }
    res = client.post("/api/v1/placeorder", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["orderid"] == "887766"

def test_split_order_execution(client):
    payload = {
        "apikey": "TEST_SECRET_KEY_123",
        "symbol": "CRUDEOIL1!",
        "action": "BUY",
        "quantity": 250,
        "split_size": 100,
        "delay": 0.01,
        "price": 6500.0
    }
    res = client.post("/api/v1/splitorder", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["total_quantity"] == 250
    assert len(data["successful_slices"]) == 3 # 100, 100, 50

def test_basket_order_margin_sequencing(client):
    # Pass SELL order first, BUY order second
    payload = {
        "apikey": "TEST_SECRET_KEY_123",
        "orders": [
            {"symbol": "NIFTY26SEP24500PE", "action": "SELL", "quantity": 50, "price": 100.0},
            {"symbol": "NIFTY26SEP24000PE", "action": "BUY", "quantity": 50, "price": 40.0}
        ]
    }
    res = client.post("/api/v1/basketorder", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["total_orders"] == 2
    # Verify BUY leg was executed before SELL leg
    assert data["results"][0]["action"] == "BUY"
    assert data["results"][1]["action"] == "SELL"

def test_portfolio_endpoints(client):
    auth_payload = {"apikey": "TEST_SECRET_KEY_123"}
    
    # 1. Orderbook
    res_orders = client.post("/api/v1/orderbook", json=auth_payload)
    assert res_orders.status_code == 200
    assert len(res_orders.json()["data"]) == 1

    # 2. Positionbook
    res_pos = client.post("/api/v1/positionbook", json=auth_payload)
    assert res_pos.status_code == 200
    assert res_pos.json()["net_mtm"] == 2500.0

    # 3. Funds
    res_funds = client.post("/api/v1/funds", json=auth_payload)
    assert res_funds.status_code == 200
    assert res_funds.json()["data"]["available_margin"] == 150000.0

    # 4. Holdings
    res_holdings = client.post("/api/v1/holdings", json=auth_payload)
    assert res_holdings.status_code == 200
    assert len(res_holdings.json()["data"]) == 1

def test_option_chain_greeks(client):
    payload = {
        "apikey": "TEST_SECRET_KEY_123",
        "symbol": "NIFTY",
        "spot": 24500.0
    }
    res = client.post("/api/v1/optionchain", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["spot"] == 24500.0
    assert len(data["strikes"]) == 21 # 10 above, 10 below, 1 ATM

    # Check ATM strike greeks
    atm_item = [s for s in data["strikes"] if s["is_atm"]][0]
    assert atm_item["strike"] == 24500.0
    assert 0.4 < atm_item["call"]["delta"] < 0.6
    assert -0.6 < atm_item["put"]["delta"] < -0.4
    assert atm_item["call"]["gamma"] > 0
