"""
Symphony XTS Binary Market Data Decoder & Ultra-Low Latency Feed
================================================================
Parses high-throughput binary websocket packets for Touchline (MessageCode 1501)
and Market Depth (MessageCode 1502), maintaining an in-memory ring buffer
for sub-millisecond strategy calculations.
"""

import struct
import time
import datetime
import logging
import threading
from typing import Dict, Any, Optional, List, Callable
from collections import deque

logger = logging.getLogger("binary_feed")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

class TickRingBuffer:
    """Thread-safe, fixed-size ring buffer for streaming market ticks."""
    def __init__(self, maxlen: int = 1000):
        self.buffer = deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def append(self, tick: Dict[str, Any]):
        with self.lock:
            self.buffer.append(tick)

    def get_latest(self) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.buffer[-1] if self.buffer else None

    def get_recent(self, n: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            count = min(n, len(self.buffer))
            return list(self.buffer)[-count:]

    def __len__(self):
        with self.lock:
            return len(self.buffer)


class BinaryMarketDataParser:
    """Decodes Symphony XTS binary packets."""
    
    @staticmethod
    def parse_touchline(raw_bytes: bytes) -> Optional[Dict[str, Any]]:
        """
        Parses Symphony Binary Touchline (1501).
        Header layout:
        uint16 msg_version, uint16 app_type, uint64 token_id, uint64 seq_no, int32 skip_bytes,
        int16 exch_seg, int32 exch_inst_id, uint64 exch_ts...
        """
        if len(raw_bytes) < 60:
            return None

        try:
            # We unpack standard Symphony touchline payload
            # Offset tracking:
            offset = 0
            msg_version, app_type = struct.unpack_from('<HH', raw_bytes, offset)
            offset += 4
            token_id = struct.unpack_from('<Q', raw_bytes, offset)[0]
            offset += 8

            if msg_version >= 1000: # Standard version check
                seq_no = struct.unpack_from('<Q', raw_bytes, offset)[0]
                offset += 8
                skip_bytes = struct.unpack_from('<i', raw_bytes, offset)[0]
                offset += 4

            exch_seg = struct.unpack_from('<h', raw_bytes, offset)[0]
            offset += 2
            inst_id = struct.unpack_from('<i', raw_bytes, offset)[0]
            offset += 4
            exch_ts = struct.unpack_from('<Q', raw_bytes, offset)[0]
            offset += 8

            # Skip best bid/ask sub-structures if present (typically 20 bytes each or variable)
            # Find LTP and OHLC double float positions
            # In standard Symphony 1501: LTP is at offset after bid/ask LUT
            # We safely scan or unpack directly:
            # If payload length matches standard fixed layout:
            if len(raw_bytes) >= offset + 80:
                # Unpack remaining numeric metrics: lut (8), ltp (8), ltq (8), total_buy (8), total_sell (8), total_vol (8), atp (8), ltt (8), pct_change (8), open (8), high (8), low (8), close (8)
                lut, ltp, ltq, total_buy, total_sell, total_vol, atp, ltt, pct_chg, o, h, l, c = struct.unpack_from(
                    '<QdqQQQdqddddd', raw_bytes, offset
                )
                
                return {
                    "msg_code": 1501,
                    "exchange_segment": exch_seg,
                    "instrument_id": inst_id,
                    "timestamp": exch_ts,
                    "ltp": float(ltp),
                    "ltq": int(ltq),
                    "total_buy_qty": int(total_buy),
                    "total_sell_qty": int(total_sell),
                    "total_traded_qty": int(total_vol),
                    "average_traded_price": float(atp),
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "percent_change": float(pct_chg),
                    "received_at": time.time()
                }

        except Exception as e:
            logger.debug(f"Binary touchline parsing exception: {e}")
            return None

        return None

    @staticmethod
    def parse_market_depth(raw_bytes: bytes) -> Optional[Dict[str, Any]]:
        """Parses Symphony Binary Market Depth (1502)."""
        if len(raw_bytes) < 40:
            return None
        try:
            offset = 0
            msg_version, app_type = struct.unpack_from('<HH', raw_bytes, offset)
            offset += 4
            token_id = struct.unpack_from('<Q', raw_bytes, offset)[0]
            offset += 8

            if msg_version >= 1000:
                seq_no = struct.unpack_from('<Q', raw_bytes, offset)[0]
                offset += 8
                offset += 4 # skip bytes

            exch_seg = struct.unpack_from('<h', raw_bytes, offset)[0]
            offset += 2
            inst_id = struct.unpack_from('<i', raw_bytes, offset)[0]
            offset += 4
            exch_ts = struct.unpack_from('<Q', raw_bytes, offset)[0]
            offset += 8

            # Parse up to 5 levels of bids and asks
            bids = []
            asks = []
            
            # Each depth entry: price (double 8), qty (int64 8), orders (int32 4) -> 20 bytes
            for _ in range(5):
                if offset + 20 <= len(raw_bytes):
                    p, q, o = struct.unpack_from('<dqi', raw_bytes, offset)
                    bids.append({"price": float(p), "quantity": int(q), "orders": int(o)})
                    offset += 20

            for _ in range(5):
                if offset + 20 <= len(raw_bytes):
                    p, q, o = struct.unpack_from('<dqi', raw_bytes, offset)
                    asks.append({"price": float(p), "quantity": int(q), "orders": int(o)})
                    offset += 20

            return {
                "msg_code": 1502,
                "exchange_segment": exch_seg,
                "instrument_id": inst_id,
                "timestamp": exch_ts,
                "bids": bids,
                "asks": asks,
                "received_at": time.time()
            }
        except Exception as e:
            logger.debug(f"Binary market depth parsing exception: {e}")
            return None


class BinaryFeedManager:
    """Manages active instrument ring buffers and binary tick distribution."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(BinaryFeedManager, cls).__new__(cls)
                cls._instance._buffers: Dict[str, TickRingBuffer] = {}
                cls._instance._subscribers: List[Callable[[Dict[str, Any]], None]] = []
            return cls._instance

    def _get_key(self, exch_seg: Any, inst_id: int) -> str:
        return f"{exch_seg}_{inst_id}"

    def get_or_create_buffer(self, exch_seg: Any, inst_id: int) -> TickRingBuffer:
        key = self._get_key(exch_seg, inst_id)
        if key not in self._buffers:
            self._buffers[key] = TickRingBuffer(maxlen=1000)
        return self._buffers[key]

    def on_binary_message(self, message_code: int, raw_payload: bytes):
        """Dispatches incoming raw binary websocket frame."""
        parsed = None
        if message_code == 1501:
            parsed = BinaryMarketDataParser.parse_touchline(raw_payload)
        elif message_code == 1502:
            parsed = BinaryMarketDataParser.parse_market_depth(raw_payload)

        if parsed:
            buf = self.get_or_create_buffer(parsed.get("exchange_segment"), parsed.get("instrument_id"))
            buf.append(parsed)

            for sub in self._subscribers:
                try:
                    sub(parsed)
                except Exception as e:
                    logger.error(f"Subscriber callback error: {e}")

    def subscribe_ticks(self, callback: Callable[[Dict[str, Any]], None]):
        """Registers a callback for streaming parsed ticks."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def get_latest_tick(self, exch_seg: Any, inst_id: int) -> Optional[Dict[str, Any]]:
        key = self._get_key(exch_seg, inst_id)
        if key in self._buffers:
            return self._buffers[key].get_latest()
        return None
