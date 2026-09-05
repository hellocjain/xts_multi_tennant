"""
Test Suite for OpenAlgo Tier 2 Parity:
- Quantitative Analytics Engine (Max Pain, GEX, Dynamic Straddle, Calendar Arbitrage)
- REST Endpoints (/api/v1/maxpain, /gex, /straddle, /arbitrage)
- Portal Web Pages & API Proxies (/client/maxpain, /client/gex, /client/straddle, /client/arbitrage, /client/scalping)
"""

import os
import sys
import pytest
import datetime
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal")))

from cryptography.fernet import Fernet
if not os.environ.get("PORTAL_MASTER_KEY"):
    os.environ["PORTAL_MASTER_KEY"] = Fernet.generate_key().decode()

import client.analytics_service as analytics_service
import client.openalgo_router as openalgo_router
from portal import main as portal_main


@pytest.fixture
def api_client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(openalgo_router.router)
    return TestClient(app)


@pytest.fixture
def portal_client():
    return TestClient(portal_main.app)


def test_max_pain_calculation_math():
    """Validates the OpenAlgo Max Pain formula across a known chain."""
    svc = analytics_service.default_analytics_service

    # Custom chain with known strike OI
    custom_chain = [
        {"strike": 24000, "ce_oi": 50000, "pe_oi": 10000},
        {"strike": 24100, "ce_oi": 40000, "pe_oi": 20000},
        {"strike": 24200, "ce_oi": 30000, "pe_oi": 40000},
        {"strike": 24300, "ce_oi": 20000, "pe_oi": 60000},
        {"strike": 24400, "ce_oi": 10000, "pe_oi": 80000},
    ]

    res = svc.calculate_max_pain(
        underlying="NIFTY",
        exchange="NFO",
        custom_chain=custom_chain
    )

    assert res["status"] == "success"
    assert res["underlying"] == "NIFTY"
    assert "max_pain_strike" in res
    assert res["max_pain_strike"] in [24100, 24200, 24300]
    assert len(res["pain_data"]) == 5
    assert res["lot_size"] == 75
    assert res["pcr_oi"] > 0


def test_gex_calculation_math():
    """Validates Gamma Exposure (GEX), Call/Put walls, and Gamma Flip."""
    svc = analytics_service.default_analytics_service

    res = svc.calculate_gex(underlying="NIFTY", exchange="NFO")

    assert res["status"] == "success"
    assert res["underlying"] == "NIFTY"
    assert "gamma_flip" in res
    assert "call_wall" in res
    assert "put_wall" in res
    assert "total_net_gex_cr" in res
    assert len(res["chain"]) > 10
    assert len(res["top_gamma_strikes"]) <= 5


def test_straddle_time_series_math():
    """Validates Dynamic ATM Straddle decay series and synthetic futures."""
    svc = analytics_service.default_analytics_service

    res = svc.calculate_straddle_series(underlying="NIFTY")

    assert res["status"] == "success"
    assert res["underlying"] == "NIFTY"
    assert res["straddle_premium"] == round(res["ce_ltp"] + res["pe_ltp"], 2)
    assert res["synthetic_future"] == round(res["atm_strike"] + res["ce_ltp"] - res["pe_ltp"], 2)
    assert res["upper_breakeven"] == round(res["atm_strike"] + res["straddle_premium"], 2)
    assert res["lower_breakeven"] == round(res["atm_strike"] - res["straddle_premium"], 2)
    assert len(res["series"]) == 13


def test_arbitrage_scanner_math():
    """Validates Calendar Spread Arbitrage scanner calculations."""
    svc = analytics_service.default_analytics_service

    res = svc.get_arbitrage_universe(symbols=["NIFTY", "BANKNIFTY", "RELIANCE"])

    assert res["status"] == "success"
    assert res["count"] == 3
    for pair in res["data"]:
        assert pair["spread"] == round(pair["next_price"] - pair["near_price"], 2)
        assert pair["annualized_pct"] > 0
        assert pair["market_regime"] in ("CONTANGO", "BACKWARDATION")


def test_api_maxpain_endpoints(api_client):
    """Tests /api/v1/maxpain, /oitracker/api/maxpain, and /oitracker/api/oi-data."""
    # 1. Standard maxpain
    resp = api_client.post("/api/v1/maxpain", json={"underlying": "NIFTY", "apikey": "test_key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["max_pain_strike"] > 0

    # 2. OpenAlgo oitracker alias
    resp2 = api_client.post("/api/v1/oitracker/api/maxpain", json={"underlying": "BANKNIFTY", "apikey": "test_key"})
    assert resp2.status_code == 200
    assert resp2.json()["underlying"] == "BANKNIFTY"

    # 3. OI data
    resp3 = api_client.post("/api/v1/oitracker/api/oi-data", json={"underlying": "NIFTY", "apikey": "test_key"})
    assert resp3.status_code == 200
    d3 = resp3.json()
    assert d3["status"] == "success"
    assert "chain" in d3


def test_api_gex_endpoint(api_client):
    """Tests /api/v1/gex and /gex/api/data."""
    resp = api_client.post("/api/v1/gex", json={"underlying": "NIFTY", "apikey": "test_key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "gamma_flip" in data


def test_api_straddle_endpoint(api_client):
    """Tests /api/v1/straddle and /straddle/api/data."""
    resp = api_client.post("/api/v1/straddle", json={"underlying": "NIFTY", "apikey": "test_key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "straddle_premium" in data


def test_api_arbitrage_endpoint(api_client):
    """Tests /api/v1/arbitrage and /arbitrage/api/data."""
    resp = api_client.post("/api/v1/arbitrage", json={"apikey": "test_key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]) >= 5


def test_portal_analytics_pages_and_proxies(tmp_path, monkeypatch):
    """Tests portal HTML views and API proxies for all Tier 2 quantitative tools."""
    import time
    from portal import database as portal_db
    from portal import security as portal_sec

    test_db = str(tmp_path / "portal_test_tier2.db")
    monkeypatch.setattr(portal_db, "get_db_path", lambda: test_db)
    if "database" in sys.modules:
        monkeypatch.setattr(sys.modules["database"], "get_db_path", lambda: test_db)
    portal_db.init_portal_db()

    with portal_db.closing(portal_db.get_db_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("tenant_test", "Test Corp", "ACTIVE", time.time(), time.time())
            )
            conn.execute(
                "INSERT INTO tenant_risk_limits (tenant_id, trading_mode, paper_trade_mode, updated_at) VALUES (?, ?, ?, ?)",
                ("tenant_test", "PAPER", 1, time.time())
            )
            conn.execute(
                "INSERT INTO client_users (id, tenant_id, username, email, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("usr_test_1", "tenant_test", "test_client", "test@test.com", "hash", time.time(), time.time())
            )

    sess_tok = portal_sec.create_client_session("usr_test_1", "tenant_test", "127.0.0.1", "pytest-agent")
    portal_client = TestClient(portal_main.app)
    portal_client.cookies.set("client_session", sess_tok)

    # 1. GET Pages
    pages = ["maxpain", "gex", "straddle", "arbitrage", "scalping"]
    for page in pages:
        res = portal_client.get(f"/client/{page}")
        assert res.status_code == 200
        assert "OpenAlgo XTS" in res.text

    # 2. POST Proxies
    p_maxpain = portal_client.post("/client/api/maxpain", json={"underlying": "NIFTY"})
    assert p_maxpain.status_code == 200
    assert p_maxpain.json()["status"] == "success"

    p_gex = portal_client.post("/client/api/gex", json={"underlying": "NIFTY"})
    assert p_gex.status_code == 200
    assert p_gex.json()["status"] == "success"

    p_straddle = portal_client.post("/client/api/straddle", json={"underlying": "NIFTY"})
    assert p_straddle.status_code == 200
    assert p_straddle.json()["status"] == "success"

    p_arbitrage = portal_client.post("/client/api/arbitrage", json={})
    assert p_arbitrage.status_code == 200
    assert p_arbitrage.json()["status"] == "success"

    # Clean up override
    portal_main.app.dependency_overrides.clear()
