"""
Unit Tests for Symphony XTS Binary Market Data Parser & Ring Buffer
===================================================================
"""

import struct
import time
from client.binary_feed import (
    BinaryMarketDataParser,
    TickRingBuffer,
    BinaryFeedManager
)

def test_tick_ring_buffer_fifo_and_recent():
    """Verify TickRingBuffer capacity, FIFO eviction, and recent slicing."""
    buf = TickRingBuffer(maxlen=5)
    for i in range(10):
        buf.append({"tick_index": i, "price": 100.0 + i})

    assert len(buf) == 5
    assert buf.get_latest()["tick_index"] == 9
    
    recent = buf.get_recent(3)
    assert len(recent) == 3
    assert recent[-1]["tick_index"] == 9
    assert recent[0]["tick_index"] == 7


def test_binary_touchline_parser_valid_frame():
    """Verify parsing of synthetic binary Touchline (1501) packet."""
    # Build a valid Touchline 1501 binary frame
    msg_version = 1001
    app_type = 2
    token_id = 998877
    seq_no = 12345
    skip_bytes = 0
    exch_seg = 2 # NSEFO
    inst_id = 45678
    exch_ts = int(time.time() * 1000)

    # Metrics
    lut = exch_ts
    ltp = 24500.50
    ltq = 50
    total_buy = 12000
    total_sell = 15000
    total_vol = 850000
    atp = 24480.25
    ltt = exch_ts
    pct_chg = 1.25
    o = 24300.0
    h = 24550.0
    l = 24280.0
    c = 24200.0

    header = struct.pack('<HHQQihIQ', msg_version, app_type, token_id, seq_no, skip_bytes, exch_seg, inst_id, exch_ts)
    body = struct.pack('<QdqQQQdqddddd', lut, ltp, ltq, total_buy, total_sell, total_vol, atp, ltt, pct_chg, o, h, l, c)
    raw_frame = header + body

    parsed = BinaryMarketDataParser.parse_touchline(raw_frame)
    assert parsed is not None
    assert parsed["msg_code"] == 1501
    assert parsed["instrument_id"] == 45678
    assert parsed["ltp"] == 24500.50
    assert parsed["ltq"] == 50
    assert parsed["open"] == 24300.0
    assert parsed["high"] == 24550.0
    assert parsed["low"] == 24280.0


def test_binary_feed_manager_singleton_and_dispatch():
    """Verify BinaryFeedManager singleton, buffer creation, and callback dispatch."""
    mgr = BinaryFeedManager()
    
    received_ticks = []
    mgr.subscribe_ticks(lambda t: received_ticks.append(t))

    # Construct frame
    raw_frame = struct.pack('<HHQQihIQQdqQQQdqddddd',
        1001, 2, 1, 1, 0, 1, 9999, int(time.time()),
        0, 75500.0, 1, 100, 100, 500, 75400.0, 0, 0.5, 75000.0, 75600.0, 74900.0, 75000.0
    )

    mgr.on_binary_message(1501, raw_frame)

    assert len(received_ticks) == 1
    assert received_ticks[0]["instrument_id"] == 9999
    assert received_ticks[0]["ltp"] == 75500.0

    latest = mgr.get_latest_tick(1, 9999)
    assert latest is not None
    assert latest["ltp"] == 75500.0
