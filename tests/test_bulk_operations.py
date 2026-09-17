"""
Unit and Integration Tests for Multi-Account Bulk Operations Suite:
- Client Microservice: /internal/strategies/toggle-all
- Bulk Operations Service: Pause, Resume & Sync Trend, Concurrency Semaphore
- Error Isolation and Unreachable Container Handling
- Portal Endpoints: /admin/bulk-operations and /admin/bulk-operations/status
"""

import pytest
import asyncio
import time
import os
import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

BASE_DIR = str(Path(__file__).parent.parent)
client_path = os.path.join(BASE_DIR, "client")
portal_path = os.path.join(BASE_DIR, "portal")
if client_path not in sys.path:
    sys.path.insert(0, client_path)
if portal_path not in sys.path:
    sys.path.insert(0, portal_path)

import importlib.util

# Load isolated client main
client_main_file = os.path.join(client_path, "main.py")
client_spec = importlib.util.spec_from_file_location("client_main_bulk", client_main_file)
client_main = importlib.util.module_from_spec(client_spec)
client_spec.loader.exec_module(client_main)

# Import portal modules
import database as portal_db
import bulk_operations_service


@pytest.fixture(autouse=True)
def setup_portal_test_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_portal_bulk.db")
    monkeypatch.setattr(portal_db, "get_db_path", lambda: test_db)
    portal_db.init_portal_db()

    # Seed test tenants
    with portal_db.get_db_connection() as conn:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) "
                "VALUES ('abk01', 'ABK Alpha', 'ACTIVE', ?, ?)",
                (time.time(), time.time())
            )
            conn.execute(
                "INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) "
                "VALUES ('abk02', 'ABK Beta', 'PAUSED', ?, ?)",
                (time.time(), time.time())
            )
            conn.execute(
                "INSERT OR REPLACE INTO tenants (id, name, status, created_at, updated_at) "
                "VALUES ('abk03', 'ABK Gamma', 'ACTIVE', ?, ?)",
                (time.time(), time.time())
            )
            conn.execute(
                "INSERT OR REPLACE INTO tenant_supertrend_strategies (tenant_id, symbol, timeframe, is_enabled, created_at, updated_at) "
                "VALUES ('abk01', 'NIFTY', '5m', 1, ?, ?)",
                (time.time(), time.time())
            )
            conn.execute(
                "INSERT OR REPLACE INTO tenant_supertrend_strategies (tenant_id, symbol, timeframe, is_enabled, created_at, updated_at) "
                "VALUES ('abk02', 'NIFTY', '5m', 0, ?, ?)",
                (time.time(), time.time())
            )
    yield


@pytest.mark.asyncio
async def test_client_toggle_all_strategies():
    """
    Verifies /internal/strategies/toggle-all toggles runners in memory.
    """
    runner1 = MagicMock()
    runner1.strategy_id = "st_1"
    runner1.symbol = "NIFTY"
    runner1.timeframe = "5m"
    runner1.is_configured = True
    runner1.is_enabled = True
    runner1.status = "RUNNING"

    runner2 = MagicMock()
    runner2.strategy_id = "st_2"
    runner2.symbol = "BANKNIFTY"
    runner2.timeframe = "15m"
    runner2.is_configured = True
    runner2.is_enabled = True
    runner2.status = "RUNNING"

    mock_strategies = {"st_1": runner1, "st_2": runner2}

    with patch.object(client_main.supertrend_engine, "strategies", mock_strategies):
        # 1. Test Pause
        req_pause = AsyncMock()
        req_pause.headers = {}
        req_pause.json.return_value = {"is_enabled": False}

        res_pause = await client_main.toggle_all_strategies_endpoint(req_pause)
        assert res_pause["status"] == "ok"
        assert res_pause["action"] == "PAUSED"
        assert res_pause["strategies_updated"] == 2
        assert runner1.is_enabled is False
        assert runner1.status == "PAUSED"
        assert runner2.is_enabled is False
        assert runner2.status == "PAUSED"

        # 2. Test Resume
        req_resume = AsyncMock()
        req_resume.headers = {}
        req_resume.json.return_value = {"is_enabled": True}

        res_resume = await client_main.toggle_all_strategies_endpoint(req_resume)
        assert res_resume["status"] == "ok"
        assert res_resume["action"] == "RESUMED"
        assert res_resume["strategies_updated"] == 2
        assert runner1.is_enabled is True
        assert runner1.status == "RUNNING"
        assert runner2.is_enabled is True
        assert runner2.status == "RUNNING"


@pytest.mark.asyncio
async def test_execute_single_pause():
    """
    Verifies pause updates SQLite DB to PAUSED and calls container toggle-all.
    """
    with patch("bulk_operations_service.dispatch_client_internal_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"status": "ok", "action": "PAUSED", "strategies_updated": 1}
        
        result = await bulk_operations_service._execute_single_pause("abk01")
        assert result["status"] == "PAUSED"
        assert result["is_error"] is False
        assert result["tenant_id"] == "abk01"
        assert "Trading paused" in result["message"]

        # Check DB state
        with portal_db.get_db_connection() as conn:
            row = conn.execute("SELECT status FROM tenants WHERE id='abk01'").fetchone()
            assert row["status"] == "PAUSED"
            strat_row = conn.execute("SELECT is_enabled FROM tenant_supertrend_strategies WHERE tenant_id='abk01'").fetchone()
            assert strat_row["is_enabled"] == 0


@pytest.mark.asyncio
async def test_execute_single_resume_and_sync_success():
    """
    Verifies resume and sync unfreezes DB/container, runs sync-trend, and handles success.
    """
    with patch("bulk_operations_service.dispatch_client_internal_post", new_callable=AsyncMock) as mock_post:
        # 1st call is toggle-all, 2nd call is sync-trend
        mock_post.side_effect = [
            {"status": "ok", "action": "RESUMED", "strategies_updated": 1},
            {"status": "SYNCED", "message": "Synced -32 lots"}
        ]
        
        sem = asyncio.Semaphore(3)
        result = await bulk_operations_service._execute_single_resume_and_sync("abk02", sem)
        assert result["status"] == "SYNCED"
        assert result["is_error"] is False
        assert result["tenant_id"] == "abk02"
        assert "Resumed & Synced to trend" in result["message"]

        # Verify DB is now ACTIVE
        with portal_db.get_db_connection() as conn:
            row = conn.execute("SELECT status FROM tenants WHERE id='abk02'").fetchone()
            assert row["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_execute_single_resume_and_sync_rms_shortfall():
    """
    Verifies that when broker RMS reports margin shortfall, it returns RMS_SHORTFALL status.
    """
    with patch("bulk_operations_service.dispatch_client_internal_post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = [
            {"status": "ok", "action": "RESUMED", "strategies_updated": 1},
            {"status": "MARGIN_SHORTFALL", "message": "Shortfall of ₹12,450 required"}
        ]
        
        sem = asyncio.Semaphore(3)
        result = await bulk_operations_service._execute_single_resume_and_sync("abk01", sem)
        assert result["status"] == "RMS_SHORTFALL"
        assert result["is_error"] is True
        assert "Margin Shortfall" in result["message"]


@pytest.mark.asyncio
async def test_run_bulk_job_full_flow():
    """
    Tests full async bulk job execution with event streaming queue.
    """
    with patch("bulk_operations_service.dispatch_client_internal_post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"status": "ok", "action": "PAUSED", "strategies_updated": 1}

        job_id = bulk_operations_service.start_bulk_operation(
            action="pause",
            tenant_ids=["abk01", "abk02", "abk03"],
            username="admin"
        )

        job = bulk_operations_service.ACTIVE_BULK_JOBS[job_id]
        events = []
        async for sse_chunk in bulk_operations_service.stream_bulk_job_events(job_id):
            if sse_chunk.startswith("data: "):
                event_json = json.loads(sse_chunk[6:].strip())
                events.append(event_json)

        assert any(e.get("type") == "INIT" for e in events)
        progress_events = [e for e in events if e.get("type") == "PROGRESS"]
        assert len(progress_events) == 3
        complete_events = [e for e in events if e.get("type") == "COMPLETE"]
        assert len(complete_events) == 1
        assert complete_events[0]["total"] == 3
        assert complete_events[0]["success_count"] == 3


@pytest.mark.asyncio
async def test_bulk_unreachable_container_isolation():
    """
    Ensures that if one container is down, other tenants succeed and the job completes safely.
    """
    def mock_post_side_effect(tenant_id, endpoint, json_payload=None, timeout_sec=8.0):
        if tenant_id == "abk02":
            return {"status": "error", "code": 503, "error": "Client container unreachable"}
        return {"status": "ok", "action": "PAUSED", "strategies_updated": 1}

    with patch("bulk_operations_service.dispatch_client_internal_post", side_effect=mock_post_side_effect):
        job_id = bulk_operations_service.start_bulk_operation(
            action="pause",
            tenant_ids=["abk01", "abk02"],
            username="admin"
        )

        events = []
        async for sse_chunk in bulk_operations_service.stream_bulk_job_events(job_id):
            if sse_chunk.startswith("data: "):
                events.append(json.loads(sse_chunk[6:].strip()))

        complete = [e for e in events if e.get("type") == "COMPLETE"][0]
        assert complete["total"] == 2
        # abk02 produces WARNING (DB updated, container unreachable)
        assert complete["success_count"] == 2
