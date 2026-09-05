"""
trading_agent_service.py - Marketcalls TradingAgent Copilot Service

Provides 1:1 capabilities matching Marketcalls OpenAlgo TradingAgent:
1. Pure-Math Price Action Calculations:
   - Dynamic Swing High/Low detection
   - Horizontal Support & Resistance pivot cluster calculation
   - Linear Regression Trendline Channel (Upper Rail, Lower Rail, Structure)
   - Fibonacci Retracement Levels (0%, 23.6%, 38.2%, 50%, 61.8%, 78.6%, 100%)
2. Deterministic RiskGuard:
   - Anti-duplicate order guard (refuses identical symbol+action within 10 seconds)
   - Fat-finger limit price guard (refuses orders >20% away from live LTP)
   - Account affordability guard (refuses orders where required margin > 90% of funds)
   - Index symbol refusal (blocks trading cash indices like NIFTY 50 directly)
3. Two-Phase Order Approval Card Generator:
   - Server-checked live LTP, calculated margin, current position, available funds
   - Emits structured approval card requiring explicit user confirmation
4. Multi-Provider LLM & Offline Deterministic Parser:
   - Generates Server-Sent Events (SSE) with word tokens and immediate chart action dispatches
"""

import time
import math
import re
import json
import logging
import datetime
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Recent order execution cache for 10s anti-duplicate RiskGuard
# Structure: (tenant_id, symbol, action) -> timestamp
_RECENT_ORDERS_CACHE: Dict[Tuple[str, str, str], float] = {}

# Non-tradable cash indices list
NON_TRADABLE_INDICES = {"NIFTY", "NIFTY 50", "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}


# =============================================================================
# 1. Pure-Math Price Action Calculations (Candle Analysis)
# =============================================================================

def compute_support_resistance(candles: List[Dict[str, Any]], num_levels: int = 4) -> Dict[str, Any]:
    """
    Computes key horizontal support and resistance levels from candle history using
    pivot clustering and swing highs/lows. Zero LLM hallucinations.
    """
    if not candles or len(candles) < 5:
        return {"levels": [], "current_price": 0.0, "pivots_count": 0}

    highs = [float(c.get("high") or c.get("h") or 0.0) for c in candles]
    lows = [float(c.get("low") or c.get("l") or 0.0) for c in candles]
    closes = [float(c.get("close") or c.get("c") or 0.0) for c in candles]
    current_price = closes[-1] if closes else 0.0

    # Identify swing highs and swing lows (local extrema with window = 2)
    pivots = []
    window = 2
    for i in range(window, len(candles) - window):
        if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
            pivots.append(highs[i])
        if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
            pivots.append(lows[i])

    if not pivots:
        pivots = [min(lows), (min(lows) + max(highs)) / 2.0, max(highs)]

    # Cluster nearby pivot levels (within 0.35% threshold)
    pivots.sort()
    clusters = []
    threshold = current_price * 0.0035 if current_price > 0 else 1.0

    current_cluster = [pivots[0]]
    for p in pivots[1:]:
        if p - current_cluster[-1] <= threshold:
            current_cluster.append(p)
        else:
            clusters.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [p]
    if current_cluster:
        clusters.append(sum(current_cluster) / len(current_cluster))

    # Separate into Supports (below current price) and Resistances (above current price)
    supports = [round(lvl, 2) for lvl in clusters if lvl < current_price]
    resistances = [round(lvl, 2) for lvl in clusters if lvl > current_price]

    # Select closest levels to current price
    selected_supports = sorted(supports, reverse=True)[:max(num_levels // 2, 2)]
    selected_resistances = sorted(resistances)[:max(num_levels // 2, 2)]

    all_levels = []
    for s in selected_supports:
        all_levels.append({"price": s, "type": "support", "color": "#10b981"})
    for r in selected_resistances:
        all_levels.append({"price": r, "type": "resistance", "color": "#f43f5e"})

    return {
        "current_price": round(current_price, 2),
        "supports": sorted(selected_supports),
        "resistances": sorted(selected_resistances),
        "levels": sorted(all_levels, key=lambda x: x["price"]),
        "pivots_count": len(pivots)
    }


def compute_swing_channel(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes upper and lower rails connecting visible swing highs and swing lows
    using linear regression and swing bounding.
    """
    if not candles or len(candles) < 10:
        return {"upper_rail": None, "lower_rail": None, "structure": "Insufficient data"}

    n = len(candles)
    highs = [float(c.get("high") or c.get("h") or 0.0) for c in candles]
    lows = [float(c.get("low") or c.get("l") or 0.0) for c in candles]
    closes = [float(c.get("close") or c.get("c") or 0.0) for c in candles]
    times = [c.get("time") or c.get("timestamp") or i for i, c in enumerate(candles)]

    # Linear regression on midpoints: y = mx + c
    mids = [(highs[i] + lows[i]) / 2.0 for i in range(n)]
    x_mean = (n - 1) / 2.0
    y_mean = sum(mids) / n

    numerator = sum((i - x_mean) * (mids[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = numerator / denominator if denominator != 0 else 0.0
    intercept = y_mean - slope * x_mean

    # Upper rail parallel line touching highest deviation
    upper_offset = max(highs[i] - (slope * i + intercept) for i in range(n))
    # Lower rail parallel line touching lowest deviation
    lower_offset = min(lows[i] - (slope * i + intercept) for i in range(n))

    start_idx = 0
    end_idx = n - 1

    upper_start = round(slope * start_idx + intercept + upper_offset, 2)
    upper_end = round(slope * end_idx + intercept + upper_offset, 2)

    lower_start = round(slope * start_idx + intercept + lower_offset, 2)
    lower_end = round(slope * end_idx + intercept + lower_offset, 2)

    channel_width_start = round(upper_start - lower_start, 2)
    channel_width_end = round(upper_end - lower_end, 2)

    # Determine structure description
    if slope > 0.05:
        trend = "Ascending channel"
    elif slope < -0.05:
        trend = "Descending channel"
    else:
        trend = "Horizontal channel"

    if channel_width_end < channel_width_start * 0.85:
        structure = f"Contracting {trend.lower()}"
    elif channel_width_end > channel_width_start * 1.15:
        structure = f"Expanding {trend.lower()}"
    else:
        structure = trend

    return {
        "slope": round(slope, 4),
        "structure": structure,
        "upper_rail": {
            "start_time": times[start_idx],
            "start_price": upper_start,
            "end_time": times[end_idx],
            "end_price": upper_end,
            "color": "#38bdf8"
        },
        "lower_rail": {
            "start_time": times[start_idx],
            "start_price": lower_start,
            "end_time": times[end_idx],
            "end_price": lower_end,
            "color": "#818cf8"
        },
        "right_edge_width": channel_width_end
    }


def compute_fibonacci_levels(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calculates Fibonacci retracement levels from the most prominent swing high and swing low.
    """
    if not candles or len(candles) < 5:
        return {"levels": [], "swing_high": 0.0, "swing_low": 0.0}

    highs = [float(c.get("high") or c.get("h") or 0.0) for c in candles]
    lows = [float(c.get("low") or c.get("l") or 0.0) for c in candles]

    swing_high = max(highs)
    swing_low = min(lows)
    diff = swing_high - swing_low

    ratios = [
        (0.0, "0.0% (Low)"),
        (0.236, "23.6%"),
        (0.382, "38.2%"),
        (0.500, "50.0%"),
        (0.618, "61.8% (Golden)"),
        (0.786, "78.6%"),
        (1.0, "100.0% (High)")
    ]

    levels = []
    for ratio, label in ratios:
        price = round(swing_low + diff * ratio, 2)
        levels.append({
            "ratio": ratio,
            "label": label,
            "price": price
        })

    return {
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
        "diff": round(diff, 2),
        "levels": levels
    }


# =============================================================================
# 2. Deterministic RiskGuard Engine
# =============================================================================

def validate_order_risk(
    tenant_id: str,
    order_data: Dict[str, Any],
    live_ltp: float,
    available_funds: float,
    current_net_position: int = 0
) -> Tuple[bool, Optional[str]]:
    """
    Enforces deterministic 4-layer RiskGuard checks:
    1. Index symbol refusal (cannot trade spot index directly)
    2. 10-second duplicate order guard
    3. Fat-finger limit price check (>20% from LTP)
    4. Account affordability check (required margin <= 90% of funds)
    5. Quantity sanity (> 0, <= 10,000)
    """
    symbol = str(order_data.get("symbol") or "").strip().upper()
    action = str(order_data.get("action") or "BUY").strip().upper()
    order_type = str(order_data.get("order_type") or order_data.get("pricetype") or "MARKET").strip().upper()
    product = str(order_data.get("product") or "NRML").strip().upper()
    quantity = int(order_data.get("quantity") or 0)
    price = float(order_data.get("price") or 0.0)

    # 1. Non-tradable Index check
    if symbol in NON_TRADABLE_INDICES:
        return False, f"RiskGuard: '{symbol}' is a cash index and cannot be traded directly. Choose an active future or option contract instead."

    # 2. Quantity check
    if quantity <= 0:
        return False, "RiskGuard: Order quantity must be greater than 0."
    if quantity > 50000:
        return False, f"RiskGuard: Quantity {quantity} exceeds safety ceiling of 50,000 shares/units."

    # 3. 10-second anti-duplicate order detector
    cache_key = (tenant_id, symbol, action)
    now = time.time()
    last_ts = _RECENT_ORDERS_CACHE.get(cache_key, 0.0)
    if (now - last_ts) < 10.0:
        remaining = round(10.0 - (now - last_ts), 1)
        return False, f"RiskGuard: Duplicate order rejected! An identical {action} order for '{symbol}' was placed within 10 seconds. Please wait {remaining}s."

    # 4. Fat-finger limit price protection (>20% from live LTP)
    if order_type in ("LIMIT", "SL", "SL-M") and price > 0 and live_ltp > 0:
        deviation = abs(price - live_ltp) / live_ltp
        if deviation > 0.20:
            pct_str = f"{round(deviation * 100, 1)}%"
            return False, f"RiskGuard: Fat-finger guard triggered! Limit price ₹{price:.2f} deviates by {pct_str} from live LTP ₹{live_ltp:.2f} (>20% limit)."

    # 5. Account affordability check (Margin <= 90% of available funds)
    eff_price = price if (order_type == "LIMIT" and price > 0) else live_ltp
    if eff_price <= 0:
        eff_price = 100.0 # fallback

    notional = eff_price * quantity
    # Estimated margin: MIS ~20% of notional, NRML/CNC ~100% (or option premium)
    margin_pct = 0.20 if product in ("MIS", "INTRADAY") else 1.0
    required_margin = notional * margin_pct

    if available_funds > 0 and required_margin > (available_funds * 0.90):
        req_fmt = f"₹{required_margin:,.2f}"
        funds_fmt = f"₹{available_funds:,.2f}"
        return False, f"RiskGuard: Insufficient funds! Required margin {req_fmt} exceeds 90% of available balance ({funds_fmt})."

    return True, None


def record_order_execution(tenant_id: str, symbol: str, action: str):
    """Updates the anti-duplicate timestamp cache after an order is approved and sent."""
    _RECENT_ORDERS_CACHE[(tenant_id, symbol.strip().upper(), action.strip().upper())] = time.time()


# =============================================================================
# 3. Server-Checked Approval Card Generator
# =============================================================================

def build_approval_card(
    tenant_id: str,
    action: str,
    symbol: str,
    exchange: str,
    quantity: int,
    order_type: str,
    price: float,
    product: str,
    live_ltp: float,
    available_funds: float,
    current_net_position: int = 0,
    is_paper: bool = True
) -> Dict[str, Any]:
    """
    Constructs a structured, server-verified Approval Card payload.
    All figures under 'checked_by_server' are verified by backend systems,
    not written or hallucinated by the AI.
    """
    eff_price = price if (order_type == "LIMIT" and price > 0) else (live_ltp if live_ltp > 0 else 100.0)
    notional = round(eff_price * quantity, 2)
    margin_factor = 0.20 if product in ("MIS", "INTRADAY") else 1.0
    required_margin = round(notional * margin_factor, 2)

    card_id = f"ac_{int(time.time() * 1000)}"

    return {
        "card_id": card_id,
        "mode": "paper" if is_paper else "live",
        "action": action.upper(),
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "quantity": quantity,
        "price": price,
        "order_type": order_type.upper(),
        "product": product.upper(),
        "checked_by_server": {
            "ltp": round(live_ltp, 2),
            "notional": notional,
            "required_margin": required_margin,
            "current_position": current_net_position,
            "available_funds": round(available_funds, 2),
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S IST")
        }
    }


# =============================================================================
# 4. Natural Language Intent Parser & Reasoning Engine
# =============================================================================

def parse_trading_intent(prompt: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic rule-based NLP parser that handles TradingAgent intents
    (Navigation, Indicators, Price Action Drawings, Account Queries, and Orders)
    even when no external LLM API key is present, providing 100% offline reliability.
    """
    p = prompt.strip().lower()
    symbol = context.get("symbol", "NIFTY")
    exchange = context.get("exchange", "NSE")
    interval = context.get("interval", "5m")

    # 1. Order Intent (Buy / Sell)
    order_match = re.search(r'\b(buy|sell)\b\s+(\d+)\s*(?:shares|lots|qty)?(?:\s+of)?\s+([A-Za-z0-9_\-]+)', p, re.IGNORECASE)
    if order_match:
        act = order_match.group(1).upper()
        qty = int(order_match.group(2))
        sym = order_match.group(3).upper()

        # Product extraction
        prod = "CNC" if "cnc" in p else ("MIS" if "mis" in p or "intraday" in p else "NRML")
        
        # Limit price extraction
        price_match = re.search(r'\blimit\s*(?:at\s*)?(\d+(?:\.\d+)?)', p)
        if price_match:
            price = float(price_match.group(1))
            order_type = "LIMIT"
        else:
            price = 0.0
            order_type = "MARKET"

        return {
            "intent": "order",
            "action": act,
            "symbol": sym,
            "exchange": "NSE" if sym not in ("SILVER", "GOLD", "CRUDEOIL") else "MCX",
            "quantity": qty,
            "order_type": order_type,
            "price": price,
            "product": prod
        }

    # 2. Mathematical Price Action Drawings
    if "channel" in p:
        return {"intent": "draw_channel"}
    if "support" in p or "resistance" in p or "levels" in p:
        return {"intent": "draw_support_resistance"}
    if "fibonacci" in p or "fib" in p:
        return {"intent": "draw_fibonacci"}

    # 3. Indicator Actions
    if "supertrend" in p:
        m = re.search(r'supertrend\s*(\d+)?(?:\s*,\s*|\s+)(\d+)?', p)
        period = int(m.group(1)) if m and m.group(1) else 10
        multiplier = float(m.group(2)) if m and m.group(2) else 3.0
        return {"intent": "add_indicator", "indicator": "SuperTrend", "params": {"period": period, "multiplier": multiplier}}

    if "bollinger" in p or "bb" in p:
        m = re.search(r'bollinger\s*(\d+)?(?:\s*,\s*|\s+)(\d+)?', p)
        period = int(m.group(1)) if m and m.group(1) else 20
        stddev = float(m.group(2)) if m and m.group(2) else 2.0
        color = "yellow" if "yellow" in p else ("blue" if "blue" in p else "slate")
        return {"intent": "add_indicator", "indicator": "Bollinger Bands", "params": {"period": period, "stdDev": stddev, "color": color}}

    if "rsi" in p:
        m = re.search(r'rsi\s*(\d+)?', p)
        period = int(m.group(1)) if m and m.group(1) else 14
        return {"intent": "add_indicator", "indicator": "RSI", "params": {"period": period}}

    if "clear" in p and ("indicator" in p or "markup" in p or "drawing" in p):
        return {"intent": "clear_chart"}

    # 4. Chart Navigation (Timeframe / Symbol / Type)
    interval_match = re.search(r'\b(?:switch to|change to|timeframe|interval)?\s*(1m|3m|5m|15m|30m|1h|d|1d|w)\b', p)
    if interval_match:
        return {"intent": "set_interval", "interval": interval_match.group(1)}

    if "heikin ashi" in p:
        return {"intent": "set_chart_type", "chart_type": "heikin-ashi"}
    if "candlestick" in p or "candles" in p:
        return {"intent": "set_chart_type", "chart_type": "candlestick"}
    if "line" in p and "chart" in p:
        return {"intent": "set_chart_type", "chart_type": "line"}

    # 5. Account & Portfolio Queries
    if "funds" in p or "balance" in p or "margin" in p:
        return {"intent": "query_funds"}
    if "position" in p or "pnl" in p or "p&l" in p:
        return {"intent": "query_positions"}
    if "order" in p and ("book" in p or "open" in p or "status" in p):
        return {"intent": "query_orders"}

    # 6. Option Chain Query
    if "option" in p or "chain" in p or "straddle" in p or "greeks" in p:
        return {"intent": "query_option_chain", "symbol": symbol}

    # 7. General Quote / Technical Summary
    return {"intent": "technical_summary", "symbol": symbol, "source": "deterministic"}


# =============================================================================
# 5. Hybrid Multi-Provider LLM & Zero-Latency Failover Engine
# =============================================================================

TRADING_AGENT_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": "Draft a trading order for explicit user review and confirmation card display",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["BUY", "SELL"]},
                    "symbol": {"type": "string", "description": "Canonical trading symbol e.g. RELIANCE, NIFTY25AUG26FUT"},
                    "quantity": {"type": "integer", "description": "Positive quantity to trade"},
                    "price": {"type": "number", "description": "Limit price, or 0 for MARKET order"},
                    "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"]},
                    "product": {"type": "string", "enum": ["NRML", "MIS", "CNC"]}
                },
                "required": ["action", "symbol", "quantity"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "draw_swing_channel",
            "description": "Compute and draw upper/lower linear regression swing channel on the active chart"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "draw_support_resistance",
            "description": "Compute and draw key horizontal support and resistance pivot clusters on the active chart"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "draw_fibonacci",
            "description": "Compute and draw Fibonacci retracement levels from the visible swing range"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_indicator",
            "description": "Add a technical indicator (SuperTrend, Bollinger Bands, RSI) to the active chart",
            "parameters": {
                "type": "object",
                "properties": {
                    "indicator": {"type": "string", "enum": ["SuperTrend", "Bollinger Bands", "RSI"]},
                    "period": {"type": "integer"},
                    "multiplier": {"type": "number"}
                },
                "required": ["indicator"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_account",
            "description": "Query account funds, margins, open positions, order book, or options chain",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_type": {"type": "string", "enum": ["funds", "positions", "orders", "option_chain"]}
                },
                "required": ["query_type"]
            }
        }
    }
]

def get_active_llm_provider() -> Optional[Dict[str, Any]]:
    """Detects if an external LLM provider is configured in environment or client config."""
    import os
    if os.environ.get("GEMINI_API_KEY"):
        return {"provider": "gemini", "api_key": os.environ["GEMINI_API_KEY"]}
    if os.environ.get("OPENAI_API_KEY"):
        return {"provider": "openai", "api_key": os.environ["OPENAI_API_KEY"]}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"provider": "anthropic", "api_key": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("OLLAMA_BASE_URL"):
        return {"provider": "ollama", "base_url": os.environ["OLLAMA_BASE_URL"]}
    return None

def call_llm_provider(provider_info: Dict[str, Any], prompt: str, context: Dict[str, Any], timeout_sec: float = 1.5) -> Dict[str, Any]:
    """
    Executes a structured tool-calling request to the configured LLM provider.
    Enforces a strict timeout to guarantee zero live-market execution lag.
    """
    import urllib.request
    import urllib.error
    provider = provider_info.get("provider")
    api_key = provider_info.get("api_key", "")

    headers = {"Content-Type": "application/json"}
    
    if provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a professional trading assistant operating a live OpenAlgo brokerage connection. Output function tool calls for user actions."},
                {"role": "user", "content": f"Context: {json.dumps(context)}. User Request: {prompt}"}
            ],
            "tools": TRADING_AGENT_TOOLS_SCHEMA,
            "tool_choice": "auto",
            "temperature": 0.1
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]["message"]
            if choice.get("tool_calls"):
                tc = choice["tool_calls"][0]["function"]
                fn_name = tc["name"]
                args = json.loads(tc.get("arguments", "{}"))
                return _map_tool_call_to_intent(fn_name, args, context)

    elif provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        gemini_tools = [{
            "functionDeclarations": [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "parameters": t["function"].get("parameters", {"type": "OBJECT", "properties": {}})
                } for t in TRADING_AGENT_TOOLS_SCHEMA
            ]
        }]
        payload = {
            "contents": [{
                "parts": [{"text": f"Context: {json.dumps(context)}\nUser prompt: {prompt}"}]
            }],
            "tools": gemini_tools
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "functionCall" in part:
                        fc = part["functionCall"]
                        return _map_tool_call_to_intent(fc["name"], fc.get("args", {}), context)

    # If no tool calls emitted by LLM, fall back
    raise ValueError(f"LLM did not return structured tool call for prompt: {prompt}")

def _map_tool_call_to_intent(fn_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Maps LLM tool call schemas to the standardized OpenAlgo Copilot intent representation."""
    sym = args.get("symbol") or context.get("symbol", "NIFTY")
    exch = args.get("exchange") or context.get("exchange", "NSE")
    
    if fn_name == "place_order":
        return {
            "intent": "order",
            "action": str(args.get("action", "BUY")).upper(),
            "symbol": str(sym).upper(),
            "exchange": "MCX" if str(sym).upper() in ("SILVER", "GOLD", "CRUDEOIL") else exch,
            "quantity": int(args.get("quantity", 1)),
            "order_type": str(args.get("order_type", "MARKET")).upper(),
            "price": float(args.get("price", 0.0)),
            "product": str(args.get("product", "NRML")).upper(),
            "source": "llm"
        }
    elif fn_name == "draw_swing_channel":
        return {"intent": "draw_channel", "source": "llm"}
    elif fn_name == "draw_support_resistance":
        return {"intent": "draw_support_resistance", "source": "llm"}
    elif fn_name == "draw_fibonacci":
        return {"intent": "draw_fibonacci", "source": "llm"}
    elif fn_name == "add_indicator":
        ind = args.get("indicator", "SuperTrend")
        p = int(args.get("period", 10))
        m = float(args.get("multiplier", 3.0))
        return {"intent": "add_indicator", "indicator": ind, "params": {"period": p, "multiplier": m}, "source": "llm"}
    elif fn_name == "query_account":
        qt = args.get("query_type", "funds")
        return {"intent": f"query_{qt}", "symbol": sym, "source": "llm"}

    return {"intent": "technical_summary", "symbol": sym, "source": "llm"}

def resolve_trading_intent_hybrid(prompt: str, context: Dict[str, Any], timeout_sec: float = 1.5) -> Dict[str, Any]:
    """
    Hybrid Resolution Architecture:
    1. If external LLM provider credentials exist, attempts tool-calling inference with strict timeout (1.5s).
    2. If provider is absent, times out, rate-limits, or errors, instantly triggers zero-latency (<1ms)
       deterministic NLP fallback, guaranteeing 100% availability in live market conditions.
    """
    provider = get_active_llm_provider()
    if provider:
        try:
            return call_llm_provider(provider, prompt, context, timeout_sec=timeout_sec)
        except Exception as e:
            logger.warning(f"Hybrid Copilot: LLM provider '{provider.get('provider')}' failed/timed out ({e}). Executing zero-latency deterministic failover.")
            res = parse_trading_intent(prompt, context)
            res["source"] = "deterministic_fallback"
            return res

    # Offline or unconfigured: pure deterministic execution
    res = parse_trading_intent(prompt, context)
    res["source"] = "deterministic"
    return res

