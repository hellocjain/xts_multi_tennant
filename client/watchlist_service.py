import os
import sqlite3
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "watchlist.db")
MAX_ITEMS_PER_LIST = 100

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_watchlist_db():
    conn = _get_db()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    user TEXT DEFAULT 'default',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(name, user)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlist_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (watchlist_id) REFERENCES watchlists (id) ON DELETE CASCADE,
                    UNIQUE(watchlist_id, symbol, exchange)
                )
            """)

            cursor = conn.execute("SELECT COUNT(*) FROM watchlists")
            if cursor.fetchone()[0] == 0:
                cursor = conn.execute("INSERT INTO watchlists (name, user) VALUES (?, ?)", ("Watchlist", "default"))
                wl_id = cursor.lastrowid
                default_items = [
                    ("NIFTY 50", "NSE_INDEX", 0),
                    ("BANKNIFTY", "NSE_INDEX", 1),
                    ("RELIANCE", "NSE", 2),
                    ("TCS", "NSE", 3),
                    ("INFY", "NSE", 4),
                    ("HDFCBANK", "NSE", 5),
                    ("SILVER10030SEP26FUT", "MCX", 6),
                ]
                conn.executemany(
                    "INSERT INTO watchlist_items (watchlist_id, symbol, exchange, position) VALUES (?, ?, ?, ?)",
                    [(wl_id, sym, exch, pos) for sym, exch, pos in default_items]
                )
    finally:
        conn.close()

init_watchlist_db()

def get_watchlists(user: str = "default") -> List[Dict[str, Any]]:
    conn = _get_db()
    try:
        cursor = conn.execute("SELECT id, name, user FROM watchlists WHERE user = ? OR user = 'default' ORDER BY id ASC", (user,))
        lists = [dict(row) for row in cursor.fetchall()]
        for wl in lists:
            c_items = conn.execute(
                "SELECT id, symbol, exchange, position FROM watchlist_items WHERE watchlist_id = ? ORDER BY position ASC, id ASC",
                (wl["id"],)
            )
            wl["items"] = [dict(r) for r in c_items.fetchall()]
        return lists
    finally:
        conn.close()

def create_watchlist(name: str, items: Optional[List[Dict[str, str]]] = None, user: str = "default") -> Optional[Dict[str, Any]]:
    conn = _get_db()
    wl_id = None
    try:
        with conn:
            cur = conn.execute("SELECT id FROM watchlists WHERE name = ? AND user = ?", (name, user))
            row = cur.fetchone()
            if row:
                wl_id = row["id"]
            else:
                cursor = conn.execute("INSERT INTO watchlists (name, user) VALUES (?, ?)", (name, user))
                wl_id = cursor.lastrowid
            if items:
                for idx, it in enumerate(items[:MAX_ITEMS_PER_LIST]):
                    sym = it.get("symbol")
                    exch = it.get("exchange", "NSE")
                    if sym and exch:
                        conn.execute(
                            "INSERT OR IGNORE INTO watchlist_items (watchlist_id, symbol, exchange, position) VALUES (?, ?, ?, ?)",
                            (wl_id, sym.strip().upper(), exch.strip().upper(), idx)
                        )
    finally:
        conn.close()

    if wl_id is not None:
        lists = get_watchlists(user)
        for wl in lists:
            if wl["id"] == wl_id:
                return wl
    return None

def rename_watchlist(watchlist_id: int, new_name: str, user: str = "default") -> bool:
    conn = _get_db()
    try:
        with conn:
            cursor = conn.execute("UPDATE watchlists SET name = ? WHERE id = ?", (new_name, watchlist_id))
            return cursor.rowcount > 0
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def delete_watchlist(watchlist_id: int, user: str = "default") -> bool:
    conn = _get_db()
    try:
        with conn:
            conn.execute("DELETE FROM watchlist_items WHERE watchlist_id = ?", (watchlist_id,))
            cursor = conn.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))
            return cursor.rowcount > 0
    finally:
        conn.close()

def clear_watchlist(watchlist_id: int) -> bool:
    conn = _get_db()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM watchlist_items WHERE watchlist_id = ?", (watchlist_id,))
            return cursor.rowcount > 0
    finally:
        conn.close()

def add_item(watchlist_id: int, symbol: str, exchange: str) -> Optional[Dict[str, Any]]:
    conn = _get_db()
    sym = symbol.strip().upper()
    exch = exchange.strip().upper()
    try:
        with conn:
            count_cur = conn.execute("SELECT COUNT(*) FROM watchlist_items WHERE watchlist_id = ?", (watchlist_id,))
            count = count_cur.fetchone()[0]
            if count >= MAX_ITEMS_PER_LIST:
                return None

            max_pos_cur = conn.execute("SELECT MAX(position) FROM watchlist_items WHERE watchlist_id = ?", (watchlist_id,))
            max_pos = max_pos_cur.fetchone()[0]
            pos = 0 if max_pos is None else max_pos + 1

            try:
                cursor = conn.execute(
                    "INSERT INTO watchlist_items (watchlist_id, symbol, exchange, position) VALUES (?, ?, ?, ?)",
                    (watchlist_id, sym, exch, pos)
                )
                item_id = cursor.lastrowid
                return {"id": item_id, "symbol": sym, "exchange": exch, "position": pos}
            except sqlite3.IntegrityError:
                cur = conn.execute("SELECT id, symbol, exchange, position FROM watchlist_items WHERE watchlist_id = ? AND symbol = ? AND exchange = ?", (watchlist_id, sym, exch))
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()

def remove_item(watchlist_id: int, item_id: int) -> bool:
    conn = _get_db()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM watchlist_items WHERE watchlist_id = ? AND id = ?", (watchlist_id, item_id))
            return cursor.rowcount > 0
    finally:
        conn.close()

def reorder_items(watchlist_id: int, order_ids: List[int]) -> bool:
    conn = _get_db()
    try:
        with conn:
            for pos, item_id in enumerate(order_ids):
                conn.execute("UPDATE watchlist_items SET position = ? WHERE watchlist_id = ? AND id = ?", (pos, watchlist_id, item_id))
        return True
    finally:
        conn.close()
