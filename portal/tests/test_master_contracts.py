"""
Unit Tests for SQLite FTS5 Master Contracts Search Engine
=========================================================
"""

from fastapi.testclient import TestClient
from portal.main import app
import portal.database as database
import portal.security as security
import portal.master_contracts as master_contracts

def test_fts5_master_records_sync_and_search():
    """Verify syncing master records and fast FTS5 search."""
    sample_records = [
        {
            "exchange_segment": "MCXFO",
            "instrument_id": 54321,
            "name": "SILVER100",
            "description": "SILVER100 30SEP2026",
            "series": "FUT",
            "lot_size": 1,
            "tick_size": 0.05,
            "freeze_qty": 20,
            "multiplier": 1.0,
            "expiry_date": "2026-09-30"
        },
        {
            "exchange_segment": "MCXFO",
            "instrument_id": 54322,
            "name": "ZINCMINI",
            "description": "ZINCMINI 30SEP2026",
            "series": "FUT",
            "lot_size": 1,
            "tick_size": 0.05,
            "freeze_qty": 100,
            "multiplier": 1000.0,
            "expiry_date": "2026-09-30"
        },
        {
            "exchange_segment": "NSEFO",
            "instrument_id": 12345,
            "name": "NIFTY",
            "description": "NIFTY 24SEP2026",
            "series": "FUT",
            "lot_size": 25,
            "tick_size": 0.05,
            "freeze_qty": 1800,
            "multiplier": 1.0,
            "expiry_date": "2026-09-24"
        }
    ]

    master_contracts.sync_master_records(sample_records)

    # Search for "SILVER"
    res1 = master_contracts.search_master_contracts("SILVER")
    assert len(res1) >= 1
    assert res1[0]["name"] == "SILVER100"

    # Search for "ZINC" with segment filter
    res2 = master_contracts.search_master_contracts("ZINC", segment="MCXFO")
    assert len(res2) >= 1
    assert res2[0]["name"] == "ZINCMINI"

    # Search for "NIFTY"
    res3 = master_contracts.search_master_contracts("NIFTY")
    assert len(res3) >= 1
    assert res3[0]["name"] == "NIFTY"


def test_portal_symbol_api_search_and_expiry_endpoints():
    """Verify /admin/api/symbols/search and /admin/api/symbols/expiry-status endpoints."""
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR REPLACE INTO admin_users (id, username, password_hash, is_2fa_enabled, created_at) VALUES ('admin_search', 'admin_search', 'hash', 1, 1000)")

    token = security.create_session("admin_search", "testclient", "testclient")
    with TestClient(app, cookies={"admin_session": token}) as client:
        # 1. Test search endpoint
        res_search = client.get("/admin/api/symbols/search?q=SILVER100")
        assert res_search.status_code == 200
        data = res_search.json()
        assert data["status"] == "success"
        assert len(data["results"]) >= 1

        # 2. Test expiry status endpoint
        res_exp = client.get("/admin/api/symbols/expiry-status?expiry=2026-09-24&segment=MCXFO")
        assert res_exp.status_code == 200
        exp_data = res_exp.json()
        assert exp_data["status"] == "success"
        assert "days_left" in exp_data
        assert "requires_rollover" in exp_data
        assert "status_badge" in exp_data
