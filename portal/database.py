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
                    max_daily_loss_inr REAL DEFAULT 50000.0,
                    telegram_bot_token TEXT DEFAULT '',
                    telegram_chat_id TEXT DEFAULT '',
                    discord_webhook_url TEXT DEFAULT '',
                    min_days_before_expiry_mcx INTEGER DEFAULT 3,
                    min_days_before_expiry_derivatives INTEGER DEFAULT 0,
                    cancel_lingering_partial_fills INTEGER DEFAULT 1,
                    partial_fill_timeout_seconds REAL DEFAULT 2.0,
                    paper_trade_mode INTEGER DEFAULT 0,
                    updated_at REAL NOT NULL
                )
            """)

            # Column migrations if table already existed
            existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(tenant_risk_limits)").fetchall()]
            if "max_daily_loss_inr" not in existing_cols:
                conn.execute("ALTER TABLE tenant_risk_limits ADD COLUMN max_daily_loss_inr REAL DEFAULT 50000.0")
            if "telegram_bot_token" not in existing_cols:
                conn.execute("ALTER TABLE tenant_risk_limits ADD COLUMN telegram_bot_token TEXT DEFAULT ''")
            if "telegram_chat_id" not in existing_cols:
                conn.execute("ALTER TABLE tenant_risk_limits ADD COLUMN telegram_chat_id TEXT DEFAULT ''")
            if "discord_webhook_url" not in existing_cols:
                conn.execute("ALTER TABLE tenant_risk_limits ADD COLUMN discord_webhook_url TEXT DEFAULT ''")

            # 4. Tenant SuperTrend Auto-Trading Strategy Configurations
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenant_supertrend_configs (
                    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
                    is_enabled INTEGER DEFAULT 0,
                    is_configured INTEGER DEFAULT 0,
                    symbol TEXT DEFAULT '',
                    exchange_segment TEXT DEFAULT '',
                    timeframe TEXT DEFAULT '5m',
                    quantity INTEGER DEFAULT 1,
                    product_type TEXT DEFAULT 'NRML',
                    atr_period INTEGER DEFAULT 10,
                    multiplier REAL DEFAULT 3.0,
                    execution_mode TEXT DEFAULT 'LIVE',
                    updated_at REAL NOT NULL
                )
            """)

            st_cols = [row[1] for row in conn.execute("PRAGMA table_info(tenant_supertrend_configs)").fetchall()]
            if "execution_mode" not in st_cols:
                conn.execute("ALTER TABLE tenant_supertrend_configs ADD COLUMN execution_mode TEXT DEFAULT 'LIVE'")

            # 4. Multi-Symbol Tenant SuperTrend Auto-Trading Strategies (Max 6 per tenant)
            # Automatic schema migration: migrate from UNIQUE(tenant_id, symbol) to UNIQUE(tenant_id, symbol, timeframe)
            try:
                table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tenant_supertrend_strategies'").fetchone()
                if table_sql and "UNIQUE(tenant_id, symbol)" in table_sql[0] and "UNIQUE(tenant_id, symbol, timeframe)" not in table_sql[0]:
                    logger.info("Migrating tenant_supertrend_strategies schema to UNIQUE(tenant_id, symbol, timeframe)...")
                    conn.execute("PRAGMA foreign_keys = OFF;")
                    conn.execute("""
                        CREATE TABLE tenant_supertrend_strategies_migrating (
                            id TEXT PRIMARY KEY,
                            tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                            symbol TEXT NOT NULL,
                            exchange_segment TEXT NOT NULL DEFAULT 'MCXFO',
                            timeframe TEXT NOT NULL DEFAULT '5m',
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
                    conn.execute("INSERT OR IGNORE INTO tenant_supertrend_strategies_migrating SELECT * FROM tenant_supertrend_strategies;")
                    conn.execute("DROP TABLE tenant_supertrend_strategies;")
                    conn.execute("ALTER TABLE tenant_supertrend_strategies_migrating RENAME TO tenant_supertrend_strategies;")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_st_strat_tenant ON tenant_supertrend_strategies(tenant_id);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_st_strat_tenant_sym_tf ON tenant_supertrend_strategies(tenant_id, symbol, timeframe);")
                    conn.execute("PRAGMA foreign_keys = ON;")
                    logger.info("Successfully migrated tenant_supertrend_strategies schema.")
            except Exception as e:
                logger.warning(f"tenant_supertrend_strategies schema migration note: {e}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenant_supertrend_strategies (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    exchange_segment TEXT NOT NULL DEFAULT 'MCXFO',
                    timeframe TEXT NOT NULL DEFAULT '5m',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    product_type TEXT NOT NULL DEFAULT 'NRML',
                    atr_period INTEGER NOT NULL DEFAULT 10,
                    multiplier REAL NOT NULL DEFAULT 3.0,
                    execution_mode TEXT NOT NULL DEFAULT 'LIVE',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(tenant_id, symbol, timeframe)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_st_strat_tenant ON tenant_supertrend_strategies(tenant_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_st_strat_tenant_sym_tf ON tenant_supertrend_strategies(tenant_id, symbol, timeframe)")

            # Auto-migrate legacy single-symbol config rows
            try:
                legacy_configs = conn.execute("SELECT * FROM tenant_supertrend_configs WHERE is_configured=1 AND symbol != ''").fetchall()
                for l_cfg in legacy_configs:
                    tid = l_cfg["tenant_id"]
                    sym = l_cfg["symbol"]
                    tf = l_cfg["timeframe"] or "5m"
                    existing_strat = conn.execute("SELECT id FROM tenant_supertrend_strategies WHERE tenant_id=? AND symbol=? AND timeframe=?", (tid, sym, tf)).fetchone()
                    if not existing_strat:
                        import uuid
                        strat_id = f"st_{uuid.uuid4().hex[:12]}"
                        conn.execute("""
                            INSERT INTO tenant_supertrend_strategies (
                                id, tenant_id, symbol, exchange_segment, timeframe, quantity,
                                product_type, atr_period, multiplier, execution_mode, is_enabled,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            strat_id,
                            tid,
                            sym,
                            l_cfg["exchange_segment"] or "MCXFO",
                            tf,
                            int(l_cfg["quantity"] or 1),
                            l_cfg["product_type"] or "NRML",
                            int(l_cfg["atr_period"] or 10),
                            float(l_cfg["multiplier"] or 3.0),
                            l_cfg["execution_mode"] or "LIVE",
                            int(l_cfg["is_enabled"] or 0),
                            time.time(),
                            l_cfg["updated_at"] or time.time()
                        ))
            except Exception as e:
                logger.warning(f"Legacy SuperTrend migration note: {e}")

            # 5. Admin Users & 2FA State
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
