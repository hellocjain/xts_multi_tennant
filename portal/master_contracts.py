"""
Master Contract Full-Text Search Engine (SQLite FTS5)
====================================================
Provides sub-millisecond full-text search and auto-complete across 198,000+
exchange instruments (MCX, NSEFO, NSECM, BSECM, BSEFO, NCDEX).
"""

import os
import sqlite3
import datetime
import logging
from typing import List, Dict, Any, Optional
from contextlib import closing

logger = logging.getLogger("master_contracts")

def get_master_db_path() -> str:
    from portal.database import get_portal_data_dir
    return os.path.join(get_portal_data_dir(), "master_contracts.db")


def get_master_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_master_db_path(), timeout=15, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_master_db():
    """Initializes the master contracts relational tables and FTS5 search index."""
    with closing(get_master_db_connection()) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS instruments (
                    exchange_segment TEXT NOT NULL,
                    instrument_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    series TEXT DEFAULT '',
                    lot_size INTEGER DEFAULT 1,
                    tick_size REAL DEFAULT 0.05,
                    freeze_qty INTEGER DEFAULT 100000,
                    multiplier REAL DEFAULT 1.0,
                    expiry_date TEXT DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (exchange_segment, instrument_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_name ON instruments(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_seg ON instruments(exchange_segment)")

            # Check if FTS5 is available and initialize virtual table
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS instruments_fts USING fts5(
                        exchange_segment,
                        instrument_id UNINDEXED,
                        name,
                        description,
                        series,
                        lot_size UNINDEXED,
                        tick_size UNINDEXED,
                        freeze_qty UNINDEXED,
                        multiplier UNINDEXED,
                        expiry_date UNINDEXED,
                        tokenize='porter unicode61'
                    )
                """)
            except Exception as e:
                logger.warning(f"FTS5 initialization notice: {e}")


def sync_master_records(records: List[Dict[str, Any]]):
    """
    Bulk synchronizes master records into SQLite relational and FTS5 search tables.
    """
    if not records:
        return

    init_master_db()
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    with closing(get_master_db_connection()) as conn:
        with conn:
            # Batch upsert into instruments table
            conn.executemany("""
                INSERT OR REPLACE INTO instruments (
                    exchange_segment, instrument_id, name, description, series,
                    lot_size, tick_size, freeze_qty, multiplier, expiry_date, updated_at
                ) VALUES (
                    :exchange_segment, :instrument_id, :name, :description, :series,
                    :lot_size, :tick_size, :freeze_qty, :multiplier, :expiry_date, :updated_at
                )
            """, [{**r, "updated_at": now} for r in records])

            # Rebuild / sync FTS5 index
            try:
                conn.execute("DELETE FROM instruments_fts")
                conn.executemany("""
                    INSERT INTO instruments_fts (
                        exchange_segment, instrument_id, name, description, series,
                        lot_size, tick_size, freeze_qty, multiplier, expiry_date
                    ) VALUES (
                        :exchange_segment, :instrument_id, :name, :description, :series,
                        :lot_size, :tick_size, :freeze_qty, :multiplier, :expiry_date
                    )
                """, records)
            except Exception as e:
                logger.error(f"FTS5 bulk sync error: {e}")


def search_master_contracts(query: str, limit: int = 25, segment: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Executes sub-millisecond prefix full-text search across master instruments.
    """
    q = (query or "").strip().upper()
    if not q:
        return []

    init_master_db()
    
    # Clean query for FTS5 syntax
    clean_q = "".join(c for c in q if c.isalnum() or c in (" ", "-", "_")).strip()
    if not clean_q:
        return []

    fts_query = f'"{clean_q}"* OR {clean_q}*'

    seg_pattern = None
    if segment and segment.upper() != "ALL":
        seg_clean = segment.strip().upper()
        seg_pattern = f"{seg_clean}%"

    with closing(get_master_db_connection()) as conn:
        try:
            # First try FTS5 query
            if seg_pattern:
                rows = conn.execute("""
                    SELECT exchange_segment, instrument_id, name, description, series,
                           lot_size, tick_size, freeze_qty, multiplier, expiry_date,
                           rank
                    FROM instruments_fts
                    WHERE instruments_fts MATCH ? AND exchange_segment LIKE ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, seg_pattern, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT exchange_segment, instrument_id, name, description, series,
                           lot_size, tick_size, freeze_qty, multiplier, expiry_date,
                           rank
                    FROM instruments_fts
                    WHERE instruments_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, limit)).fetchall()

            if rows:
                return [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"FTS5 match fallback to LIKE: {e}")

        # Fallback to standard LIKE index query
        pattern = f"{clean_q}%"
        if seg_pattern:
            rows = conn.execute("""
                SELECT exchange_segment, instrument_id, name, description, series,
                       lot_size, tick_size, freeze_qty, multiplier, expiry_date
                FROM instruments
                WHERE (name LIKE ? OR description LIKE ?) AND exchange_segment LIKE ?
                ORDER BY name ASC
                LIMIT ?
            """, (pattern, pattern, seg_pattern, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT exchange_segment, instrument_id, name, description, series,
                       lot_size, tick_size, freeze_qty, multiplier, expiry_date
                FROM instruments
                WHERE name LIKE ? OR description LIKE ?
                ORDER BY name ASC
                LIMIT ?
            """, (pattern, pattern, limit)).fetchall()

        return [dict(r) for r in rows]

