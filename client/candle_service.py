"""
candle_service.py - Historical OHLCV Candlestick Service with Multi-Tier Storage.
Provides historical candle data for OpenAlgo-compatible charting (/api/v1/history).
Features:
1. Local SQLite caching for sub-millisecond retrieval of previously fetched candles.
2. Symphony XTS Market Data Interactive historical API integration (/instruments/ohlc).
3. Intelligent realistic synthetic candle generation for paper accounts or offline broker sessions.
"""
import os
import time
import math
import hashlib
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Compression interval mapping in seconds
INTERVAL_SECONDS = {
    "1m": 60,
    "2m": 120,
    "3m": 180,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1h": 3600,
    "D": 86400,
    "1D": 86400,
}

# Base reference prices for common market contracts
DEFAULT_BASE_PRICES = {
    "SILVER": 2360.0,
    "GOLD": 72500.0,
    "CRUDEOIL": 6450.0,
    "NATURALGAS": 185.0,
    "NIFTY": 24500.0,
    "BANKNIFTY": 51200.0,
    "FINNIFTY": 23400.0,
    "MIDCPNIFTY": 12800.0,
    "RELIANCE": 2980.0,
    "TCS": 4400.0,
    "INFY": 1850.0,
    "HDFCBANK": 1640.0,
    "ICICIBANK": 1220.0,
    "SBIN": 820.0,
    "TATAMOTORS": 980.0,
}


class CandleService:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(os.path.dirname(__file__), "candles_cache.db")
        self._mem_conn = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:")
            self._mem_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS candles_cache (
                        symbol TEXT NOT NULL,
                        exchange TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        timestamp INTEGER NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        volume INTEGER NOT NULL,
                        oi INTEGER DEFAULT 0,
                        PRIMARY KEY (symbol, exchange, interval, timestamp)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_candles_lookup 
                    ON candles_cache (symbol, exchange, interval, timestamp)
                """)
                conn.commit()
        except Exception as e:
            logger.error(f"CandleService database init failed: {e}")

    def get_cached_candles(
        self, symbol: str, exchange: str, interval: str, start_ts: int, end_ts: int
    ) -> List[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT timestamp, open, high, low, close, volume, oi
                    FROM candles_cache
                    WHERE symbol = ? AND exchange = ? AND interval = ?
                      AND timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                    """,
                    (symbol.upper(), exchange.upper(), interval, start_ts, end_ts),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Error querying candle cache: {e}")
            return []

    def save_candles(
        self, symbol: str, exchange: str, interval: str, candles: List[Dict[str, Any]]
    ):
        if not candles:
            return
        try:
            with self._get_connection() as conn:
                data = [
                    (
                        symbol.upper(),
                        exchange.upper(),
                        interval,
                        int(c["timestamp"]),
                        float(c["open"]),
                        float(c["high"]),
                        float(c["low"]),
                        float(c["close"]),
                        int(c.get("volume", 0)),
                        int(c.get("oi", 0)),
                    )
                    for c in candles
                ]
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO candles_cache 
                    (symbol, exchange, interval, timestamp, open, high, low, close, volume, oi)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    data,
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Error saving candles to cache: {e}")

    def _resolve_base_price(self, symbol: str, seed_price: Optional[float] = None) -> float:
        if seed_price and seed_price > 0:
            return float(seed_price)
        sym_clean = symbol.upper().replace(" ", "")
        for prefix, price in DEFAULT_BASE_PRICES.items():
            if prefix in sym_clean:
                return price
        # Deterministic seed price derived from symbol hash
        h = int(hashlib.md5(symbol.encode()).hexdigest()[:6], 16)
        return 100.0 + (h % 2000)

    def get_last_price(self, symbol: str, seed_price: Optional[float] = None) -> float:
        """Returns the last known or base reference price for a symbol."""
        return self._resolve_base_price(symbol, seed_price)

    def generate_synthetic_candles(
        self,
        symbol: str,
        exchange: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        seed_price: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generates realistic Brownian-motion candlestick data aligned to interval boundaries.
        Deterministic per symbol so charts render stably across reloads.
        """
        interval_sec = INTERVAL_SECONDS.get(interval, 300)
        start_aligned = (start_ts // interval_sec) * interval_sec
        end_aligned = (end_ts // interval_sec) * interval_sec

        if end_aligned < start_aligned:
            end_aligned = start_aligned + (interval_sec * 60)

        # Cap maximum candles to prevent browser overload (e.g. 500 bars)
        num_bars = min(500, max(10, (end_aligned - start_aligned) // interval_sec))
        if num_bars * interval_sec < (end_aligned - start_aligned):
            start_aligned = end_aligned - (num_bars * interval_sec)

        base_price = self._resolve_base_price(symbol, seed_price)
        current_price = base_price

        # Hash-based pseudo-random generator seeded with symbol and start timestamp
        seed_val = int(hashlib.sha256(f"{symbol}:{interval}".encode()).hexdigest()[:8], 16)

        def pseudo_rand(step: int) -> float:
            x = (seed_val + step * 1103515245 + 12345) & 0x7FFFFFFF
            return (x / 0x7FFFFFFF) * 2.0 - 1.0  # -1.0 to 1.0

        candles = []
        volatility = 0.003  # 0.3% per bar

        for i in range(num_bars):
            ts = start_aligned + (i * interval_sec)
            r1 = pseudo_rand(i * 4 + 1)
            r2 = pseudo_rand(i * 4 + 2)
            r3 = abs(pseudo_rand(i * 4 + 3))
            r4 = abs(pseudo_rand(i * 4 + 4))

            open_p = current_price
            delta = current_price * volatility * r1
            close_p = round(max(1.0, open_p + delta), 2)
            high_p = round(max(open_p, close_p) + (current_price * volatility * r3), 2)
            low_p = round(min(open_p, close_p) - (current_price * volatility * r4), 2)
            if low_p <= 0:
                low_p = round(min(open_p, close_p) * 0.99, 2)

            vol = int(max(10, 50 + int(abs(r2) * 500)))
            oi = int(1000 + (ts % 5000))

            candles.append({
                "timestamp": ts,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol,
                "oi": oi,
            })
            current_price = close_p

        return candles

    def fetch_history(
        self,
        symbol: str,
        exchange: str,
        interval: str = "5m",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        xts_client: Any = None,
        is_paper: bool = True,
        seed_price: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Main entrypoint: retrieves cached candles, fetches broker historical data if live,
        or falls back to high-fidelity synthetic candles.
        """
        now = int(time.time())
        interval_sec = INTERVAL_SECONDS.get(interval, 300)

        # Parse start_date and end_date
        if start_date:
            try:
                if len(start_date) == 10:  # YYYY-MM-DD
                    dt_start = datetime.strptime(start_date, "%Y-%m-%d")
                else:
                    dt_start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                start_ts = int(dt_start.timestamp())
            except Exception:
                start_ts = now - (86400 * 5)
        else:
            # Default to 5 days back for intraday, 60 days for daily
            days_back = 60 if interval in ("D", "1D") else 5
            start_ts = now - (86400 * days_back)

        if end_date:
            try:
                if len(end_date) == 10:
                    dt_end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                else:
                    dt_end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                end_ts = int(dt_end.timestamp())
            except Exception:
                end_ts = now
        else:
            end_ts = now

        # 1. Check local cache
        cached = self.get_cached_candles(symbol, exchange, interval, start_ts, end_ts)
        if len(cached) >= 30:
            return cached

        # 2. If live broker client provided and not in paper mode, attempt XTS historical fetch
        if not is_paper and xts_client and hasattr(xts_client, "get_history"):
            try:
                broker_candles = xts_client.get_history(
                    symbol=symbol,
                    exchange=exchange,
                    timeframe=interval,
                    from_date=datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d"),
                    to_date=datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d"),
                )
                if broker_candles and len(broker_candles) > 0:
                    self.save_candles(symbol, exchange, interval, broker_candles)
                    return broker_candles
            except Exception as ex:
                logger.warning(f"XTS broker historical fetch failed: {ex}. Falling back to synthetic candles.")

        # 3. Fallback: generate smooth realistic synthetic candles
        synthetic = self.generate_synthetic_candles(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            seed_price=seed_price,
        )
        # Cache synthetic candles so subsequent timeframe requests are fast & continuous
        self.save_candles(symbol, exchange, interval, synthetic)
        return synthetic


# Singleton instance
default_candle_service = CandleService()
