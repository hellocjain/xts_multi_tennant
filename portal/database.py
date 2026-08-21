import sqlite3
import os
import json
import time
import logging
from contextlib import closing

logger = logging.getLogger(__name__)

def get_portal_data_dir():
    d = os.environ.get("PORTAL_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(d, exist_ok=True)
    return d

def get_db_path():
    return os.path.join(get_portal_data_dir(), "portal.db")

def get_db_connection():
    conn = sqlite3.connect(get_db_path(), timeout=15, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

def init_portal_db():
    with closing(get_db_connection()) as conn:
        with conn:
            # 1. Tenants Registry
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            # 2. Encrypted Tenant Credentials
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenant_credentials (
                    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
                    encrypted_payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            # 3. Tenant Risk Limits & Execution Parameters
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenant_risk_limits (
                    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
                    order_type TEXT DEFAULT 'LIMIT',
                    slippage_buffer_pct REAL DEFAULT 0.005,
                    tv_sends_lots INTEGER DEFAULT 1,
                    max_lots_limit INTEGER DEFAULT 100,
                    max_units_limit INTEGER DEFAULT 100000,
                    max_order_value_inr REAL DEFAULT 5000000.0,
                    daily_notional_cap_inr REAL DEFAULT 10000000.0,
                    min_days_before_expiry_mcx INTEGER DEFAULT 3,
                    min_days_before_expiry_derivatives INTEGER DEFAULT 0,
                    cancel_lingering_partial_fills INTEGER DEFAULT 1,
                    partial_fill_timeout_seconds REAL DEFAULT 2.0,
                    paper_trade_mode INTEGER DEFAULT 0,
                    updated_at REAL NOT NULL
                )
            """)

            # 4. Admin Users & 2FA State
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    totp_secret_enc TEXT,
                    is_2fa_enabled INTEGER DEFAULT 0,
                    recovery_codes_hash_json TEXT,
                    created_at REAL NOT NULL
                )
            """)

            # 5. Admin Sessions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT REFERENCES admin_users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT
                )
            """)

            # 6. Immutable Audit Trail
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_tenant_id TEXT,
                    details_json TEXT NOT NULL
                )
            """)
    logger.info("Portal database initialized successfully.")

def record_audit(actor: str, action: str, details: dict, target_tenant_id: str = None):
    try:
        with closing(get_db_connection()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO audit_logs (timestamp, actor, action, target_tenant_id, details_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (time.time(), actor, action, target_tenant_id, json.dumps(details))
                )
    except Exception as e:
        logger.error(f"Failed to record audit log: {e}")
