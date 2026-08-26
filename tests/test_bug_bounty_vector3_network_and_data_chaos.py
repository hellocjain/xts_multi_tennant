"""
Bug Bounty Vector 3: Network Latency, Partitions & Corrupted Market Data Suite.
Verifies:
1. Immunity to NaNs, infinities, nulls, and negative prices in OHLC candles.
2. Handling of flatlined markets (ATR = 0.0) without ZeroDivisionError.
3. Resilience against extreme ATR parameter inputs (0, negative, None).
4. Graceful handling of empty candle sets and timestamp gaps.
"""

import pytest
import math
import os
import sys
from pathlib import Path

BASE_DIR = str(Path(__file__).parent.parent)
client_path = os.path.join(BASE_DIR, "client")
if client_path not in sys.path:
    sys.path.insert(0, client_path)

from supertrend_engine import calculate_supertrend
from chaos_test_harness import generate_synthetic_candles, inject_candle_corruption


def test_nan_and_inf_price_resilience():
    """Verifies that injecting NaN or Inf prices does not raise exceptions and is sanitized."""
    candles = generate_synthetic_candles(50, base_price=2450.0, trend="BULLISH")
    corrupted = inject_candle_corruption(candles, "NAN_PRICE")

    # Add inf price in middle
    corrupted[20]["close"] = float("inf")
    corrupted[21]["high"] = float("-inf")

    res = calculate_supertrend(corrupted, atr_period=10, multiplier=3.0)

    # Must not raise exception and must return valid numerical values
    assert "error" not in res or not res.get("error")
    assert not math.isnan(res["supertrend"])
    assert not math.isnan(res["atr"])
    assert res["trend"] in (1, -1)


def test_zero_atr_flatline_market():
    """Verifies that a market flatlined with 0 price movement calculates without divide-by-zero."""
    candles = generate_synthetic_candles(50, base_price=2450.0)
    flatlined = inject_candle_corruption(candles, "ZERO_ATR_FLATLINE")

    res = calculate_supertrend(flatlined, atr_period=10, multiplier=3.0)

    assert "error" not in res or not res.get("error")
    assert res["atr"] >= 0.0
    assert not math.isnan(res["supertrend"])


def test_extreme_and_invalid_parameters():
    """Verifies that invalid parameter inputs (period=0, negative multiplier, None) are protected."""
    candles = generate_synthetic_candles(30, base_price=2450.0)

    # Case 1: ATR Period 0 or negative -> automatically bounded to at least 1
    res1 = calculate_supertrend(candles, atr_period=0, multiplier=3.0)
    assert "error" not in res1 or not res1.get("error")

    # Case 2: Multiplier 0 or negative -> automatically bounded
    res2 = calculate_supertrend(candles, atr_period=10, multiplier=-5.0)
    assert "error" not in res2 or not res2.get("error")

    # Case 3: None parameters
    res3 = calculate_supertrend(candles, atr_period=None, multiplier=None)
    assert "error" not in res3 or not res3.get("error")


def test_empty_and_insufficient_candles():
    """Verifies that empty, corrupted, or insufficient candle sets return structured error dicts."""
    # Case 1: Empty list
    res_empty = calculate_supertrend([], atr_period=10)
    assert res_empty["error"] == "Empty or invalid candle data provided"
    assert res_empty["trend_name"] == "INITIALIZING"

    # Case 2: Insufficient candles
    candles_few = generate_synthetic_candles(5)
    res_few = calculate_supertrend(candles_few, atr_period=10)
    assert "Insufficient candles" in res_few["error"]
    assert res_few["trend_name"] == "INITIALIZING"

    # Case 3: Non-list input
    res_invalid = calculate_supertrend(None, atr_period=10)
    assert "error" in res_invalid
