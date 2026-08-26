"""
Chaos & Fault-Injection Test Harness for Multi-Tenant SuperTrend Trading Engine.
Simulates:
- Programmable broker latency (0ms - 10,000ms)
- Random & targeted HTTP error codes (401, 429, 500, 502, 504)
- Broker RMS rejections ('INSUFFICIENT_MARGIN', 'NONSQROFF', 'CIRCUIT_LIMIT')
- Corrupted candle data (NaNs, negative prices, zero ATR, timestamp gaps)
- Network partitions and socket disconnection spikes
"""

import asyncio
import time
import math
import random
from typing import List, Dict, Any, Optional


class ChaosBrokerMock:
    """
    Programmable mock broker that injects realistic financial exchange faults.
    """
    def __init__(self):
        self.latency_seconds: float = 0.0
        self.failure_rate: float = 0.0
        self.forced_http_status: Optional[int] = None
        self.forced_rejection_reason: Optional[str] = None
        self.dispatched_orders: List[Dict[str, Any]] = []
        self.rejection_count: int = 0
        self.success_count: int = 0

    def configure_chaos(
        self,
        latency_seconds: float = 0.0,
        failure_rate: float = 0.0,
        forced_http_status: Optional[int] = None,
        forced_rejection_reason: Optional[str] = None
    ):
        self.latency_seconds = latency_seconds
        self.failure_rate = failure_rate
        self.forced_http_status = forced_http_status
        self.forced_rejection_reason = forced_rejection_reason

    def reset_metrics(self):
        self.dispatched_orders.clear()
        self.rejection_count = 0
        self.success_count = 0

    async def simulate_dispatch(
        self,
        sig_id: str,
        action: str,
        symbol: str,
        quantity: int,
        price: float = 0.0,
        order_ref: str = "",
        is_paper: bool = False
    ) -> Dict[str, Any]:
        """Simulates placing an order through the broker with injected faults."""
        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

        # Check forced HTTP status
        if self.forced_http_status and self.forced_http_status != 200:
            self.rejection_count += 1
            return {
                "status": "failed",
                "error": f"HTTP {self.forced_http_status} Gateway Error",
                "http_status": self.forced_http_status
            }

        # Check forced RMS rejection
        if self.forced_rejection_reason:
            self.rejection_count += 1
            return {
                "status": "rejected",
                "error": self.forced_rejection_reason,
                "order_ref": order_ref,
                "result": {"description": self.forced_rejection_reason}
            }

        # Check random failure rate
        if self.failure_rate > 0 and random.random() < self.failure_rate:
            self.rejection_count += 1
            return {
                "status": "rejected",
                "error": "Random Broker Fault Injection",
                "order_ref": order_ref
            }

        self.success_count += 1
        record = {
            "sig_id": sig_id,
            "action": action,
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "order_ref": order_ref,
            "is_paper": is_paper,
            "timestamp": time.time()
        }
        self.dispatched_orders.append(record)
        return {
            "status": "done" if not is_paper else "paper_done",
            "result": {"AppOrderID": random.randint(100000, 999999)},
            "order_ref": order_ref
        }


def generate_synthetic_candles(
    count: int = 100,
    base_price: float = 2450.0,
    trend: str = "BULLISH",
    timeframe_seconds: int = 300,
    start_time: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Generates synthetic, mathematically consistent OHLC candle series."""
    start_ts = start_time or int(time.time() - (count * timeframe_seconds))
    candles = []
    current_price = base_price

    for i in range(count):
        candle_ts = start_ts + (i * timeframe_seconds)
        drift = 2.5 if trend == "BULLISH" else -2.5
        noise = random.uniform(-1.0, 1.0)
        c_open = current_price
        c_close = c_open + drift + noise
        c_high = max(c_open, c_close) + random.uniform(0.5, 3.0)
        c_low = min(c_open, c_close) - random.uniform(0.5, 3.0)
        c_volume = random.randint(100, 5000)

        candles.append({
            "time": candle_ts,
            "open": round(c_open, 2),
            "high": round(c_high, 2),
            "low": round(c_low, 2),
            "close": round(c_close, 2),
            "volume": c_volume
        })
        current_price = c_close

    return candles


def inject_candle_corruption(candles: List[Dict[str, Any]], corruption_type: str) -> List[Dict[str, Any]]:
    """Injects real-world market data corruption into a candle series."""
    corrupted = [dict(c) for c in candles]
    if not corrupted:
        return corrupted

    if corruption_type == "NAN_PRICE":
        corrupted[-1]["close"] = float("nan")
        corrupted[-1]["high"] = float("nan")
    elif corruption_type == "ZERO_PRICE":
        corrupted[-1]["open"] = 0.0
        corrupted[-1]["close"] = 0.0
    elif corruption_type == "NEGATIVE_PRICE":
        corrupted[-2]["low"] = -500.0
    elif corruption_type == "ZERO_ATR_FLATLINE":
        flat_val = corrupted[-1]["close"]
        for c in corrupted[-15:]:
            c["open"] = flat_val
            c["high"] = flat_val
            c["low"] = flat_val
            c["close"] = flat_val
    elif corruption_type == "TIMESTAMP_GAP":
        # Delete middle 20 candles creating large time discontinuity
        if len(corrupted) > 30:
            corrupted = corrupted[:10] + corrupted[25:]
    elif corruption_type == "OUT_OF_ORDER_TIMESTAMPS":
        if len(corrupted) >= 3:
            corrupted[-1]["time"], corrupted[-2]["time"] = corrupted[-2]["time"], corrupted[-1]["time"]

    return corrupted
