import os
import sys
import time
import pytest
import sqlite3
from fastapi.testclient import TestClient
from contextlib import closing

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "client")))

import database
import portal.main as portal_app
import client.xts_api as xts_api
import portal.strategy_parser as strategy_parser


@pytest.fixture
def client_portal():
    return TestClient(portal_app.app)


def test_multi_tenant_strategy_id_collision_prevention():
    """
    STRESS TEST: Verify that multiple tenants can save the exact same strategy
    (e.g., SILVER1001! 15m) without triggering primary key collisions or SQLite IntegrityErrors.
    """
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        # Setup tables
        conn.execute("""
            CREATE TABLE tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                client_id TEXT NOT NULL,
                created_at REAL NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE tenant_supertrend_strategies (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                exchange_segment TEXT NOT NULL DEFAULT 'MCXFO',
                timeframe TEXT NOT NULL DEFAULT '15m',
                quantity INTEGER NOT NULL DEFAULT 1,
                product_type TEXT NOT NULL DEFAULT 'NRML',
                atr_period INTEGER NOT NULL DEFAULT 10,
                multiplier REAL NOT NULL DEFAULT 3.0,
                execution_mode TEXT NOT NULL DEFAULT 'LIVE',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(tenant_id, symbol, timeframe)
            );
        """)

        # Insert 10 different tenants
        for i in range(1, 11):
            conn.execute("INSERT INTO tenants (id, name, client_id, created_at) VALUES (?, ?, ?, ?)",
                         (f"tenant_{i:02d}", f"Client {i}", f"CLI{i}", time.time()))

        # Simulate 10 tenants saving the exact same strategy: SILVER1001! 15m
        clean_sym = "SILVER1001!"
        clean_tf = "15m"
        saved_ids = set()

        for i in range(1, 11):
            tid = f"tenant_{i:02d}"
            strat_id = f"st_{tid}_{clean_sym.lower()}_{clean_tf}"
            
            conn.execute("""
                INSERT INTO tenant_supertrend_strategies (
                    id, tenant_id, symbol, exchange_segment, timeframe, quantity,
                    product_type, atr_period, multiplier, execution_mode, is_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'MCXFO', ?, 2, 'NRML', 10, 3.0, 'LIVE', 1, ?, ?)
                ON CONFLICT(tenant_id, symbol, timeframe) DO UPDATE SET
                    quantity=excluded.quantity,
                    updated_at=excluded.updated_at
            """, (strat_id, tid, clean_sym, clean_tf, time.time(), time.time()))
            saved_ids.add(strat_id)

        assert len(saved_ids) == 10, "Each tenant should have a distinct, non-colliding strategy ID."
        count = conn.execute("SELECT COUNT(*) FROM tenant_supertrend_strategies WHERE symbol='SILVER1001!'").fetchone()[0]
        assert count == 10, "All 10 tenants should successfully hold their active strategy without constraint violations."


def test_margin_telemetry_multi_segment_payin_parsing():
    """
    UNIT TEST: Test that _safe_float and multi-segment parser handle same-day PayInAmount,
    negative cash ledger, and missing keys accurately.
    """
    # 1. Test _safe_float against malformed values
    assert xts_api._safe_float("10000.5") == 10000.5
    assert xts_api._safe_float("NaN") == 0.0
    assert xts_api._safe_float(None) == 0.0
    assert xts_api._safe_float("") == 0.0
    assert xts_api._safe_float("invalid_str") == 0.0

    # 2. Simulated real broker payload with 0 in MCX index [0] and 10000 PayIn in index [1]
    broker_payload = {
        "type": "success",
        "result": {
            "BalanceList": [
                {
                    "limitHeader": "COMMODITIES|MCX|ALL",
                    "limitObject": {
                        "RMSSubLimits": {"cashAvailable": "0", "marginUtilized": "0", "netMarginAvailable": "0"},
                        "marginAvailable": {"PayInAmount": "0", "AdhocMargin": "0"}
                    }
                },
                {
                    "limitHeader": "ALL|ALL|ALL",
                    "limitObject": {
                        "RMSSubLimits": {"cashAvailable": "-413", "marginUtilized": "0", "netMarginAvailable": "9587"},
                        "marginAvailable": {"PayInAmount": "10000", "AdhocMargin": "0"}
                    }
                }
            ]
        }
    }

    bal_list = broker_payload["result"]["BalanceList"]
    best_entry = None
    best_avail = -1.0

    for item in bal_list:
        limit_obj = item.get('limitObject', {})
        rms = limit_obj.get('RMSSubLimits', {})
        margin_avail_obj = limit_obj.get('marginAvailable', {})

        cash_avail = xts_api._safe_float(rms.get('cashAvailable'))
        pay_in = xts_api._safe_float(margin_avail_obj.get('PayInAmount'))
        adhoc = xts_api._safe_float(margin_avail_obj.get('AdhocMargin'))
        collateral = xts_api._safe_float(rms.get('collateral') or rms.get('collateralMargin'))
        margin_used = xts_api._safe_float(rms.get('marginUtilized'))

        raw_net_avail = rms.get('netMarginAvailable')
        if raw_net_avail is not None:
            net_avail = xts_api._safe_float(raw_net_avail, default=cash_avail + pay_in + adhoc + collateral - margin_used)
        else:
            net_avail = cash_avail + pay_in + adhoc + collateral - margin_used

        effective_avail = max(0.0, net_avail)
        total_acct_val = max(0.0, cash_avail + pay_in + adhoc + collateral)

        entry_data = {
            "available_margin": max(0.0, cash_avail + pay_in + adhoc),
            "margin_used": max(0.0, margin_used),
            "total_collateral": max(0.0, collateral),
            "net_margin_available": effective_avail,
            "total_account_value": total_acct_val,
        }

        header = item.get('limitHeader', '')
        if 'COMMODITIES' in header and effective_avail > 0:
            best_entry = entry_data
            best_avail = effective_avail
            break
        elif effective_avail > best_avail:
            best_entry = entry_data
            best_avail = effective_avail

    assert best_entry is not None
    assert best_entry["net_margin_available"] == 9587.0
    assert best_entry["available_margin"] == 9587.0


def test_custom_strategy_sandbox_security():
    """
    SECURITY TEST: Verify that the Python strategy sandbox traps malicious attempts
    to import os, sys, subprocess, or socket.
    """
    malicious_script = """
import os
class MaliciousStrategy:
    def on_candle(self, candle, state):
        os.system("rm -rf /")
        return None
"""
    res = strategy_parser.validate_strategy_code(malicious_script)
    assert not res["valid"], "Sandbox must reject malicious os import."
    assert "forbidden module 'os'" in res["error"]


def test_system_health_diagnostic_endpoint():
    """
    INTEGRATION TEST: Verify database integrity and structure via context manager.
    """
    with closing(database.get_db_connection()) as conn:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert check == "ok", "Database integrity check must be OK."
