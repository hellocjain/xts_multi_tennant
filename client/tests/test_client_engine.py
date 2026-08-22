import os
import sys
import tempfile
import json
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient

# Point DATA_DIR to a temporary directory before importing app modules
test_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = test_dir
os.environ["CLIENT_ID"] = "TEST_CLIENT_01"
os.environ["WEBHOOK_SECRET"] = "TestSecret123"
os.environ["PAPER_TRADE_MODE"] = "True"

client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if "main" in sys.modules:
    del sys.modules["main"]
if "config" in sys.modules:
    del sys.modules["config"]
if "xts_api" in sys.modules:
    del sys.modules["xts_api"]

sys.path.insert(0, client_dir)

import config
import xts_api
import main as client_main
app = client_main.app
db_init = client_main.db_init

@pytest.fixture(autouse=True)
def setup_db():
    db_init()
    yield

def test_tick_size_quantization():
    # BUY tests (ceiling)
    assert xts_api.apply_tick_size(6500.02, 0.05, "BUY") == 6500.05
    assert xts_api.apply_tick_size(6500.05, 0.05, "BUY") == 6500.05
    assert xts_api.apply_tick_size(6500.06, 0.05, "BUY") == 6500.10
    assert xts_api.apply_tick_size(100.2, 1.0, "BUY") == 101.0

    # SELL tests (floor)
    assert xts_api.apply_tick_size(6500.09, 0.05, "SELL") == 6500.05
    assert xts_api.apply_tick_size(6500.05, 0.05, "SELL") == 6500.05
    assert xts_api.apply_tick_size(6500.04, 0.05, "SELL") == 6500.00
    assert xts_api.apply_tick_size(100.8, 1.0, "SELL") == 100.0

def test_daily_notional_tracking():
    # Initial state
    state = xts_api.get_daily_notional_state()
    assert state["notional"] >= 0.0

    # Reserve notional
    allowed, total = xts_api.check_and_reserve_daily_notional(500000.0)
    assert allowed is True
    assert total >= 500000.0

    # Refund notional
    xts_api.refund_daily_notional(500000.0)
    state2 = xts_api.get_daily_notional_state()
    assert state2["notional"] == total - 500000.0

def test_symbol_resolution():
    assert xts_api.resolve_symbol_smart("MCX:CRUDEOIL1!") == "CRUDEOIL"
    assert xts_api.resolve_symbol_smart("NATGAS") == "NATURALGAS"
    assert xts_api.resolve_symbol_smart("NIFTY50") == "NIFTY"
    assert xts_api.resolve_symbol_smart("GOLDMINI") == "GOLDM"

def test_webhook_endpoints():
    with TestClient(app) as client:
        # 1. Health check
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["client_id"] == "TEST_CLIENT_01"
        assert data["paper_trade_mode"] is True

        # 2. Unauthorized webhook
        bad_payload = {
            "secret": "WrongSecret",
            "action": "BUY",
            "symbol": "MCX:CRUDEOIL1!",
            "quantity": 1,
            "price": 6500.0
        }
        res = client.post("/webhook", json=bad_payload)
        assert res.status_code == 401

        # 3. Valid webhook
        good_payload = {
            "secret": "TestSecret123",
            "action": "BUY",
            "symbol": "MCX:CRUDEOIL1!",
            "quantity": 1,
            "price": 6500.0
        }
        res = client.post("/webhook", json=good_payload)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "signal_id" in data

        # 4. Immediate duplicate suppression
        res_dup = client.post("/webhook", json=good_payload)
        assert res_dup.status_code == 200
        assert res_dup.json()["status"] == "warning"

        # 5. Invalid action
        invalid_action_payload = {
            "secret": "TestSecret123",
            "action": "INVALID_ACTION",
            "symbol": "CRUDEOIL",
            "quantity": 1,
            "price": 6500.0
        }
        res = client.post("/webhook", json=invalid_action_payload)
        assert res.status_code == 422

        # 6. Fractional quantity rejection
        fractional_payload = {
            "secret": "TestSecret123",
            "action": "BUY",
            "symbol": "CRUDEOIL",
            "quantity": 1.5,
            "price": 6500.0
        }
        res = client.post("/webhook", json=fractional_payload)
        assert res.status_code == 422

        # 7. Telemetry endpoint
        res_tel = client.get("/internal/telemetry")
        assert res_tel.status_code == 200
        tel_data = res_tel.json()
        assert "health" in tel_data
        assert "positions" in tel_data
        assert "recent_signals" in tel_data
        assert len(tel_data["recent_signals"]) >= 1

        # 8. Panic endpoint
        panic_payload = {"secret": "TestSecret123"}
        res_panic = client.post("/panic", json=panic_payload)
        assert res_panic.status_code == 200
        assert res_panic.json()["status"] == "success"

def test_commodity_multiplier():
    assert xts_api.get_contract_multiplier("CRUDEOIL1!", "MCXFO") == 100.0
    assert xts_api.get_contract_multiplier("CRUDEOILM1!", "MCXFO") == 10.0
    assert xts_api.get_contract_multiplier("NATGAS", "MCXFO") == 1250.0
    assert xts_api.get_contract_multiplier("NATGASMINI", "MCXFO") == 250.0
    assert xts_api.get_contract_multiplier("NATURALGASM", "MCXFO") == 250.0
    assert xts_api.get_contract_multiplier("SILVER", "MCXFO") == 30.0
    assert xts_api.get_contract_multiplier("SILVERM", "MCXFO") == 5.0
    assert xts_api.get_contract_multiplier("SILVERMIC", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("SILVERMICRO", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("GOLD", "MCXFO") == 100.0
    assert xts_api.get_contract_multiplier("GOLDM", "MCXFO") == 10.0
    assert xts_api.get_contract_multiplier("GOLDMINI", "MCXFO") == 10.0
    assert xts_api.get_contract_multiplier("GOLDPETAL", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("GOLDPETAL24AUGFUT", "MCXFO") == 1.0
    assert xts_api.get_contract_multiplier("ALUMINI", "MCXFO") == 1000.0
    assert xts_api.get_contract_multiplier("ALUMINIUM", "MCXFO") == 5000.0
    assert xts_api.get_contract_multiplier("NIFTY", "NSEFO") == 1.0
    assert xts_api.get_contract_multiplier("RELIANCE", "NSECM") == 1.0
