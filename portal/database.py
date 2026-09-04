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
                    min_days_before_expiry_mcx INTEGER DEFAULT 7,
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

            # 7. Global Custom Python Strategies Library
            conn.execute("""
                CREATE TABLE IF NOT EXISTS custom_strategies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    filename TEXT NOT NULL,
                    code_content TEXT NOT NULL,
                    default_timeframe TEXT DEFAULT '15m',
                    default_symbol TEXT DEFAULT 'GOLDPETAL1!',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            # 8. Tenant Custom Strategy Assignments
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenant_custom_strategies (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    strategy_id TEXT NOT NULL REFERENCES custom_strategies(id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    exchange_segment TEXT NOT NULL DEFAULT 'MCXFO',
                    timeframe TEXT NOT NULL DEFAULT '15m',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    product_type TEXT NOT NULL DEFAULT 'NRML',
                    execution_mode TEXT NOT NULL DEFAULT 'LIVE',
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(tenant_id, strategy_id, symbol, timeframe)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant_custom_strat ON tenant_custom_strategies(tenant_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_custom_strat_id ON tenant_custom_strategies(strategy_id)")

            # 9. Client User Accounts (Role-Based Access for Individual Clients)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS client_users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_client_users_tenant ON client_users(tenant_id)")

            # 10. Client User Sessions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS client_sessions (
                    token_hash TEXT PRIMARY KEY,
                    client_user_id TEXT NOT NULL REFERENCES client_users(id) ON DELETE CASCADE,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_client_sessions_user ON client_sessions(client_user_id)")
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

# =========================================================================
# Custom Python Strategy CRUD Helpers
# =========================================================================

def get_all_tenants():
    with closing(get_db_connection()) as conn:
        rows = conn.execute("SELECT * FROM tenants ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

def get_custom_strategies():
    with closing(get_db_connection()) as conn:
        rows = conn.execute("""
            SELECT s.*, 
                   COUNT(t.id) as assigned_count,
                   SUM(CASE WHEN t.is_enabled = 1 THEN 1 ELSE 0 END) as active_count
            FROM custom_strategies s
            LEFT JOIN tenant_custom_strategies t ON s.id = t.strategy_id
            GROUP BY s.id
            ORDER BY s.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

def get_custom_strategy(strategy_id: str):
    with closing(get_db_connection()) as conn:
        row = conn.execute("SELECT * FROM custom_strategies WHERE id=?", (strategy_id,)).fetchone()
        return dict(row) if row else None

def save_custom_strategy(id: str, name: str, description: str, filename: str, code_content: str, default_timeframe: str = "15m", default_symbol: str = "GOLDPETAL1!"):
    now = time.time()
    with closing(get_db_connection()) as conn:
        with conn:
            existing = conn.execute("SELECT id FROM custom_strategies WHERE id=?", (id,)).fetchone()
            if existing:
                conn.execute("""
                    UPDATE custom_strategies 
                    SET name=?, description=?, filename=?, code_content=?, default_timeframe=?, default_symbol=?, updated_at=?
                    WHERE id=?
                """, (name, description, filename, code_content, default_timeframe, default_symbol, now, id))
            else:
                conn.execute("""
                    INSERT INTO custom_strategies (id, name, description, filename, code_content, default_timeframe, default_symbol, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, name, description, filename, code_content, default_timeframe, default_symbol, now, now))
    return id

def delete_custom_strategy(strategy_id: str):
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM custom_strategies WHERE id=?", (strategy_id,))

def get_tenant_custom_strategies(tenant_id: str = None, strategy_id: str = None):
    with closing(get_db_connection()) as conn:
        query = """
            SELECT t.*, s.name as strategy_name, s.filename, s.code_content, tn.name as tenant_name
            FROM tenant_custom_strategies t
            JOIN custom_strategies s ON t.strategy_id = s.id
            JOIN tenants tn ON t.tenant_id = tn.id
            WHERE 1=1
        """
        params = []
        if tenant_id:
            query += " AND t.tenant_id = ?"
            params.append(tenant_id)
        if strategy_id:
            query += " AND t.strategy_id = ?"
            params.append(strategy_id)
        query += " ORDER BY t.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def save_tenant_custom_strategy(id: str, tenant_id: str, strategy_id: str, symbol: str, exchange_segment: str = "MCXFO", timeframe: str = "15m", quantity: int = 1, product_type: str = "NRML", execution_mode: str = "LIVE", is_enabled: int = 0):
    now = time.time()
    with closing(get_db_connection()) as conn:
        with conn:
            existing = conn.execute("SELECT id FROM tenant_custom_strategies WHERE id=?", (id,)).fetchone()
            if existing:
                conn.execute("""
                    UPDATE tenant_custom_strategies 
                    SET symbol=?, exchange_segment=?, timeframe=?, quantity=?, product_type=?, execution_mode=?, is_enabled=?, updated_at=?
                    WHERE id=?
                """, (symbol, exchange_segment, timeframe, quantity, product_type, execution_mode, is_enabled, now, id))
            else:
                conn.execute("""
                    INSERT INTO tenant_custom_strategies (id, tenant_id, strategy_id, symbol, exchange_segment, timeframe, quantity, product_type, execution_mode, is_enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, tenant_id, strategy_id, symbol, exchange_segment, timeframe, quantity, product_type, execution_mode, is_enabled, now, now))
    return id

def toggle_tenant_custom_strategy(id: str, is_enabled: int):
    now = time.time()
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("UPDATE tenant_custom_strategies SET is_enabled=?, updated_at=? WHERE id=?", (is_enabled, now, id))

def delete_tenant_custom_strategy(id: str):
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM tenant_custom_strategies WHERE id=?", (id,))

# =========================================================================
# Client User Management Helpers (Role-Based Access)
# =========================================================================

def create_client_user(tenant_id: str, username: str, password_hash: str, email: str = "") -> str:
    import uuid
    user_id = str(uuid.uuid4())
    now = time.time()
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("""
                INSERT INTO client_users (id, tenant_id, username, password_hash, email, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (user_id, tenant_id, username.strip(), password_hash, email.strip(), now, now))
    return user_id

def get_client_user_by_username(username: str) -> dict | None:
    with closing(get_db_connection()) as conn:
        row = conn.execute("SELECT * FROM client_users WHERE username=?", (username.strip(),)).fetchone()
        return dict(row) if row else None

def get_client_user_by_id(user_id: str) -> dict | None:
    with closing(get_db_connection()) as conn:
        row = conn.execute("SELECT * FROM client_users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

def get_client_users_for_tenant(tenant_id: str) -> list:
    with closing(get_db_connection()) as conn:
        rows = conn.execute("SELECT id, tenant_id, username, email, is_active, created_at FROM client_users WHERE tenant_id=?", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]

def delete_client_user(user_id: str):
    with closing(get_db_connection()) as conn:
        with conn:
            conn.execute("DELETE FROM client_users WHERE id=?", (user_id,))


