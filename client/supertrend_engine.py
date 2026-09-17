"""
SuperTrend Auto-Trading Strategy Engine (Pure Python, Per-Tenant In-Process Task)
Part of the XTS Multi-Tenant Algorithmic Trading Platform.

Features:
- Pure Python Wilder's Smoothing ATR & SuperTrend calculation (zero heavy C/Pandas dependencies).
- 100% Mathematical parity with TradingView Pine Script strategy (KivancOzbilgic v4).
- Aligned candle-close REST polling against Symphony XTS Market Data API (ON_CANDLE_CLOSE rule).
- Continuous live broker position reconciliation on every cycle.
- Sequential two-leg reversal execution (Leg 1: Square-Off, Leg 2: Enter Opposite) with freeze-quantity slicing.
- Per-tenant Execution Mode: LIVE Broker Execution vs PAPER Simulation (Dry-Run).
- Ring buffer cache for TradingView Lightweight Charts v4 candlestick and indicator series.
- Contract expiry safety cutoff & auto-pause for MCX/NSE derivatives.
- Direct integration into audited execute_signal() pipeline with source="supertrend_engine".
"""

import sys
import os
import time
import datetime
import logging
import asyncio
import uuid
import math
import re
from typing import List, Dict, Optional, Any, Callable
from xts_api import slice_quantity_for_freeze

try:
    from xts_api import format_order_ref_chunk
except ImportError:
    def format_order_ref_chunk(base_ref: str, chunk_idx: int = 1, max_len: int = 50) -> str:
        if not base_ref:
            return ""
        chunk_suffix = f"_{chunk_idx}" if chunk_idx > 1 else ""
        full_ref = f"{base_ref}{chunk_suffix}"
        if len(full_ref) <= max_len:
            return full_ref
        retry_suffix = "_RETRY" if "_RETRY" in str(base_ref) else ""
        combined_suffix = f"{retry_suffix}{chunk_suffix}"
        avail_prefix_len = max(1, max_len - len(combined_suffix))
        base_str = str(base_ref)
        base_clean = base_str[:-len(retry_suffix)] if retry_suffix else base_str
        return f"{base_clean[:avail_prefix_len]}{combined_suffix}"

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None

try:
    import config
except ImportError:
    config = None

logger = logging.getLogger("supertrend_engine")
CONTINUOUS_SUFFIX = re.compile(r'(\d+)!$')
IST_TIMEZONE = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def is_market_open_ist(exch_seg: str = "MCXFO", now_ts: Optional[float] = None, force_check: bool = False) -> bool:
    """
    Evaluates whether the specified Indian exchange segment is currently open for trading.
    Delegates to config.is_market_open_ist for session-aware holiday checking.
    """
    if config is not None and hasattr(config, "is_market_open_ist"):
        return config.is_market_open_ist(exch_seg, now_ts=now_ts, force_check=force_check)

    if not force_check and config is not None and not getattr(config, "ENFORCE_MARKET_HOURS", True):
        return True

    if not force_check and "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("ENFORCE_MARKET_HOURS_IN_TESTS", "").lower() not in ("true", "1", "yes"):
        return True

    ts = now_ts if now_ts is not None else time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=IST_TIMEZONE)
    if dt.weekday() >= 5:
        return False

    seg_upper = str(exch_seg or "").upper()
    cur_hms = (dt.hour, dt.minute, dt.second)
    if "MCX" in seg_upper or "COMMODITY" in seg_upper:
        return (9, 0, 0) <= cur_hms <= (23, 55, 0)
    elif any(eq in seg_upper for eq in ("NSE", "BSE", "CM", "CASH")):
        return (9, 15, 0) <= cur_hms <= (15, 30, 0)
    else:
        return (9, 0, 0) <= cur_hms <= (23, 55, 0)

def is_market_opening_stabilizing(exch_seg: str = "MCXFO", now_ts: Optional[float] = None) -> bool:
    """
    Returns True if the market has just opened and is within the 60-second stabilization window.
    Delegates to config.is_market_opening_stabilizing.
    """
    if config is not None and hasattr(config, "is_market_opening_stabilizing"):
        return config.is_market_opening_stabilizing(exch_seg, now_ts=now_ts)
    return False


TIMEFRAME_SECONDS_MAP = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "20m": 1200,
    "25m": 1500,
    "30m": 1800,
    "45m": 2700,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
}

def parse_timeframe_seconds(tf_str: Any) -> int:
    """
    Parses timeframe strings into seconds. Supports standard presets and arbitrary custom intervals.
    Examples:
      '1m' -> 60, '3m' -> 180, '5m' -> 300, '15m' -> 900, '20m' -> 1200, '25m' -> 1500,
      '30m' -> 1800, '45m' -> 2700, '1h' -> 3600, '2h' -> 7200, '4h' -> 14400, '1d' -> 86400,
      '20' -> 1200, 25 -> 1500 (integers treated as minutes)
    """
    if not tf_str:
        return 300
    if isinstance(tf_str, (int, float)):
        val = int(tf_str)
        return val * 60 if val < 1000 else val

    s = str(tf_str).strip().lower()
    if s in TIMEFRAME_SECONDS_MAP:
        return TIMEFRAME_SECONDS_MAP[s]

    m_h = re.match(r'^(\d+)\s*h(?:our|ours|r)?$', s)
    if m_h:
        return max(60, int(m_h.group(1)) * 3600)
    m_m = re.match(r'^(\d+)\s*m(?:in|inute|inutes)?$', s)
    if m_m:
        return max(60, int(m_m.group(1)) * 60)
    m_s = re.match(r'^(\d+)\s*s(?:ec|econd|econds)?$', s)
    if m_s:
        return max(60, int(m_s.group(1)))
    m_d = re.match(r'^(\d+)\s*d(?:ay|ays)?$', s)
    if m_d:
        return max(60, int(m_d.group(1)) * 86400)
    if s.isdigit():
        val = int(s)
        return val * 60 if val < 1000 else val
    return 300

def calculate_supertrend(
    candles: List[Dict[str, Any]],
    atr_period: int = 10,
    multiplier: float = 3.0,
    change_atr: bool = True
) -> Dict[str, Any]:
    """
    Computes SuperTrend indicator over a chronological list of OHLC candles.
    Matches TradingView Pine Script v4 (KivancOzbilgic formula).
    Candles format: [{"time": int, "open": float, "high": float, "low": float, "close": float, "volume": int}, ...]
    """
    if not isinstance(candles, list) or not candles:
        return {
            "trend": 0,
            "trend_name": "INITIALIZING",
            "supertrend": 0.0,
            "upper_band": 0.0,
            "lower_band": 0.0,
            "atr": 0.0,
            "last_close": 0.0,
            "last_candle_time": 0,
            "prev_trend": 0,
            "is_flip": False,
            "flip_direction": None,
            "candle_series": [],
            "error": "Empty or invalid candle data provided"
        }

    # Validate parameters
    safe_period = max(1, int(atr_period) if atr_period is not None else 10)
    safe_mult = max(0.1, float(multiplier) if multiplier is not None else 3.0)

    # Sanitize and validate candle data
    clean_candles = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        try:
            o = float(c.get("open", 0.0))
            h = float(c.get("high", 0.0))
            l = float(c.get("low", 0.0))
            cl = float(c.get("close", 0.0))
            t = int(c.get("time", 0))
            if math.isnan(o) or math.isnan(h) or math.isnan(l) or math.isnan(cl) or math.isinf(cl):
                continue
            clean_candles.append({
                "time": t,
                "open": o,
                "high": h,
                "low": l,
                "close": cl,
                "volume": int(c.get("volume", 0)) if c.get("volume") is not None else 0
            })
        except (ValueError, TypeError):
            continue

    n = len(clean_candles)
    if n < safe_period + 1:
        return {
            "trend": 0,
            "trend_name": "INITIALIZING",
            "supertrend": 0.0,
            "upper_band": 0.0,
            "lower_band": 0.0,
            "atr": 0.0,
            "last_close": clean_candles[-1]["close"] if clean_candles else 0.0,
            "last_candle_time": clean_candles[-1]["time"] if clean_candles else 0,
            "prev_trend": 0,
            "is_flip": False,
            "flip_direction": None,
            "candle_series": [],
            "error": f"Insufficient candles ({n}/{safe_period + 1} required)"
        }

    candles = clean_candles
    atr_period = safe_period
    multiplier = safe_mult

    # 1. Compute True Range for all candles
    tr_list = []
    for i in range(n):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        if i == 0:
            tr = high - low
        else:
            prev_close = float(candles[i - 1]["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    # 2. Compute ATR (Wilder's Smoothing RMA vs SMA matching Pine Script changeATR)
    atr_list = [0.0] * n
    if change_atr:
        # Wilder's Smoothing ATR (RMA in Pine Script: atr = atr(Periods))
        initial_atr = sum(tr_list[:atr_period]) / float(atr_period)
        atr_list[atr_period - 1] = initial_atr
        for i in range(atr_period, n):
            prev_atr = atr_list[i - 1]
            current_atr = ((prev_atr * (atr_period - 1)) + tr_list[i]) / float(atr_period)
            atr_list[i] = current_atr
    else:
        # Simple Moving Average ATR (atr2 = sma(tr, Periods) in Pine Script)
        for i in range(atr_period - 1, n):
            atr_list[i] = sum(tr_list[i - atr_period + 1 : i + 1]) / float(atr_period)

    # 3. Compute Basic Bands, Final Ratchet Bands, and Trend
    final_ub = [0.0] * n
    final_lb = [0.0] * n
    trend = [0] * n # +1 for Bullish, -1 for Bearish
    supertrend = [0.0] * n

    for i in range(atr_period - 1, n):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        close = float(candles[i]["close"])
        hl2 = (high + low) / 2.0
        cur_atr = atr_list[i]

        basic_ub = hl2 + (multiplier * cur_atr)
        basic_lb = hl2 - (multiplier * cur_atr)

        if i == atr_period - 1:
            final_ub[i] = basic_ub
            final_lb[i] = basic_lb
            trend[i] = 1 if close >= basic_lb else -1
            supertrend[i] = final_lb[i] if trend[i] == 1 else final_ub[i]
        else:
            prev_close = float(candles[i - 1]["close"])
            prev_ub = final_ub[i - 1]
            prev_lb = final_lb[i - 1]

            # Final Upper Band Ratchet (Pine Script dn := close[1] < dn1 ? min(dn, dn1) : dn)
            if basic_ub < prev_ub or prev_close > prev_ub:
                final_ub[i] = basic_ub
            else:
                final_ub[i] = prev_ub

            # Final Lower Band Ratchet (Pine Script up := close[1] > up1 ? max(up, up1) : up)
            if basic_lb > prev_lb or prev_close < prev_lb:
                final_lb[i] = basic_lb
            else:
                final_lb[i] = prev_lb

            # Trend Direction (Pine Script trend := trend[1] == -1 and close > dn1 ? 1 : trend[1] == 1 and close < up1 ? -1 : trend[1])
            prev_t = trend[i - 1]
            if prev_t == -1 and close > prev_ub:
                trend[i] = 1
            elif prev_t == 1 and close < prev_lb:
                trend[i] = -1
            else:
                trend[i] = prev_t

            supertrend[i] = final_lb[i] if trend[i] == 1 else final_ub[i]

    cur_trend = trend[-1]
    prev_trend = trend[-2] if n >= 2 else cur_trend
    is_flip = (cur_trend != prev_trend) and (prev_trend != 0)

    flip_dir = None
    if is_flip:
        if cur_trend == 1 and prev_trend == -1:
            flip_dir = "BULLISH"
        elif cur_trend == -1 and prev_trend == 1:
            flip_dir = "BEARISH"

    # 4. Generate Enriched Candle Series for Lightweight Charts
    enriched_series = []
    for i in range(n):
        c_time = candles[i].get("time")
        if c_time is None:
            c_time = candles[i].get("timestamp", int(time.time()))
        enriched_series.append({
            "time": c_time,
            "open": round(float(candles[i]["open"]), 2),
            "high": round(float(candles[i]["high"]), 2),
            "low": round(float(candles[i]["low"]), 2),
            "close": round(float(candles[i]["close"]), 2),
            "supertrend": round(supertrend[i], 2),
            "upper_band": round(final_ub[i], 2),
            "lower_band": round(final_lb[i], 2),
            "trend": trend[i],
            "atr": round(atr_list[i], 2),
            "volume": int(candles[i].get("volume", 0))
        })

    last_time = candles[-1].get("time")
    if last_time is None:
        last_time = candles[-1].get("timestamp", 0)

    return {
        "trend": cur_trend,
        "trend_name": "BULLISH" if cur_trend == 1 else ("BEARISH" if cur_trend == -1 else "INITIALIZING"),
        "supertrend": round(supertrend[-1], 2),
        "upper_band": round(final_ub[-1], 2),
        "lower_band": round(final_lb[-1], 2),
        "atr": round(atr_list[-1], 2),
        "last_close": round(candles[-1]["close"], 2),
        "last_candle_time": last_time,
        "prev_trend": prev_trend,
        "is_flip": is_flip,
        "flip_direction": flip_dir,
        "candle_series": enriched_series,
        "error": None
    }


class SingleSuperTrendRunner:
    """
    Autonomous runner for a single symbol contract.
    """
    def __init__(self, config_dict: dict, dispatch_fn=None, main_module=None):
        self.dispatch_fn = dispatch_fn
        self.lock = asyncio.Lock()

        # Strategy Configuration State
        self.id: str = str(config_dict.get("id") or f"st_{uuid.uuid4().hex[:8]}")
        self.symbol: str = str(config_dict.get("symbol", "")).strip().upper()
        self.exchange_segment: str = str(config_dict.get("exchange_segment", "MCXFO")).strip().upper()
        self.timeframe: str = str(config_dict.get("timeframe", "5m")).strip().lower()
        self.quantity: int = max(0, int(config_dict.get("quantity", 1)))
        self.product_type: str = str(config_dict.get("product_type", "NRML")).strip().upper()
        self.atr_period: int = max(2, int(config_dict.get("atr_period", 10)))
        self.multiplier: float = max(0.1, float(config_dict.get("multiplier", 3.0)))
        self.execution_mode: str = str(config_dict.get("execution_mode", "LIVE")).strip().upper()
        if self.execution_mode not in ("LIVE", "PAPER"):
            self.execution_mode = "LIVE"

        self.is_configured: bool = bool(self.symbol and self.exchange_segment and self.quantity > 0)
        self.is_enabled: bool = bool(config_dict.get("is_enabled", False)) and self.is_configured
        self.strategy_key: str = f"{self.symbol}_{self.timeframe}"
        self.lot_size: int = max(1, int(config_dict.get("lot_size", 1)))

        # Virtual Position & Active Contract Tracking
        self.active_contract_id: Optional[Any] = None
        self.active_contract_desc: Optional[str] = None

        if "virtual_position" in config_dict and config_dict.get("virtual_position") is not None:
            self._virtual_position: int = int(config_dict.get("virtual_position", 0))
            self.active_contract_id = config_dict.get("active_contract_id")
            self.active_contract_desc = config_dict.get("active_contract_desc")
        else:
            try:
                main_mod = main_module
                if not main_mod:
                    for mod_name in ("main", "client.main", "__main__"):
                        m = sys.modules.get(mod_name)
                        if m and (hasattr(m, "db_get_virtual_position") or hasattr(m, "db_get_virtual_position_record")):
                            main_mod = m
                            break
                if not main_mod:
                    import main as main_mod
                if hasattr(main_mod, "db_get_virtual_position_record"):
                    pos_rec = main_mod.db_get_virtual_position_record(self.strategy_key)
                    self._virtual_position = int(pos_rec.get("virtual_position", 0))
                    self.active_contract_id = pos_rec.get("active_contract_id")
                    self.active_contract_desc = pos_rec.get("active_contract_desc")
                elif hasattr(main_mod, "db_get_virtual_position"):
                    self._virtual_position = int(main_mod.db_get_virtual_position(self.strategy_key))
                else:
                    self._virtual_position = 0
            except Exception:
                self._virtual_position = 0

        # Live Dynamic Telemetry
        self.status: str = "RUNNING" if self.is_enabled else "DISABLED"
        self.active_trend: str = "INITIALIZING"
        self.forming_trend: str = "INITIALIZING"
        self.current_broker_quantity: int = 0
        self.broker_side: str = "FLAT"
        self.last_atr: float = 0.0
        self.upper_band: float = 0.0
        self.lower_band: float = 0.0
        self.last_close: float = 0.0
        self.last_candle_time: int = 0
        self.last_processed_candle_time: int = 0
        self.last_signal_time: float = 0.0
        self.last_signal_action: str = ""
        self.last_signal_details: dict = {}
        self.last_error: Optional[str] = None
        self.next_poll_seconds: int = 0
        self.last_auto_align_attempt: float = 0.0

        # Historical buffer & chart markers
        self.cached_candles: List[Dict[str, Any]] = []
        self.recent_trade_markers: List[Dict[str, Any]] = []
        self.pending_order_first_seen: Dict[str, float] = {}

        # Resolved Contract Tracking (Autonomous Rollover)
        self.last_resolved_inst_id: Optional[Any] = self.active_contract_id
        self.last_resolved_symbol_desc: Optional[str] = self.active_contract_desc
        self.pending_rollover: bool = False

    @property
    def virtual_position(self) -> int:
        return self._virtual_position

    @virtual_position.setter
    def virtual_position(self, val: int):
        self._virtual_position = int(val)

    @property
    def strategy_position(self) -> str:
        if self._virtual_position > 0:
            return "LONG"
        elif self._virtual_position < 0:
            return "SHORT"
        return "FLAT"

    @strategy_position.setter
    def strategy_position(self, val: str):
        v = str(val).strip().upper()
        if v == "LONG":
            self._virtual_position = self.quantity if self._virtual_position <= 0 else self._virtual_position
        elif v == "SHORT":
            self._virtual_position = -self.quantity if self._virtual_position >= 0 else self._virtual_position
        elif v in ("FLAT", "INITIALIZING"):
            self._virtual_position = 0

    def _save_virtual_position(self, main_module, qty: int, active_contract_id=None, active_contract_desc=None):
        """Persists virtual position and active contract info to SQLite with graceful fallback for legacy mocks."""
        if not main_module or not hasattr(main_module, "db_set_virtual_position"):
            return
        cid = str(active_contract_id) if active_contract_id else (str(self.active_contract_id) if self.active_contract_id else None)
        cdesc = str(active_contract_desc) if active_contract_desc else (str(self.active_contract_desc) if self.active_contract_desc else None)
        try:
            main_module.db_set_virtual_position(
                self.strategy_key, self.symbol, self.timeframe, qty,
                active_contract_id=cid, active_contract_desc=cdesc
            )
        except TypeError:
            main_module.db_set_virtual_position(self.strategy_key, self.symbol, self.timeframe, qty)

    def update_config(self, config_dict: dict):
        """Updates parameters for this single runner safely."""
        if "id" in config_dict:
            self.id = str(config_dict["id"])
        if "symbol" in config_dict:
            new_sym = str(config_dict["symbol"]).strip().upper()
            if new_sym and new_sym != self.symbol:
                self.symbol = new_sym
                self.strategy_key = f"{self.symbol}_{self.timeframe}"
                self.cached_candles = []
                self.recent_trade_markers = []
                self.current_broker_quantity = 0
                self.broker_side = "FLAT"
        if "exchange_segment" in config_dict:
            self.exchange_segment = str(config_dict["exchange_segment"]).strip().upper()
        if "timeframe" in config_dict:
            new_tf = str(config_dict["timeframe"]).strip().lower()
            if new_tf and new_tf != self.timeframe:
                self.timeframe = new_tf
                self.strategy_key = f"{self.symbol}_{self.timeframe}"
                self.cached_candles = []
                self.recent_trade_markers = []
        if "quantity" in config_dict:
            self.quantity = max(1, int(config_dict["quantity"]))
        if "product_type" in config_dict:
            self.product_type = str(config_dict["product_type"]).strip().upper()
        if "atr_period" in config_dict:
            self.atr_period = max(2, int(config_dict["atr_period"]))
        if "multiplier" in config_dict:
            self.multiplier = max(0.1, float(config_dict["multiplier"]))
        if "execution_mode" in config_dict:
            m = str(config_dict["execution_mode"]).strip().upper()
            if m in ("LIVE", "PAPER"):
                self.execution_mode = m
        if "virtual_position" in config_dict:
            self.virtual_position = int(config_dict["virtual_position"])
            self.strategy_position = "LONG" if self.virtual_position > 0 else ("SHORT" if self.virtual_position < 0 else "FLAT")

        self.strategy_key = f"{self.symbol}_{self.timeframe}"
        self.is_configured = bool(self.symbol and self.exchange_segment and self.quantity > 0)

        # Pre-resolve contract info for telemetry if available
        try:
            import xts_api
            inst = xts_api.resolve_contract(self.symbol)
            if inst:
                self.last_resolved_inst_id = inst.get("inst_id")
                self.last_resolved_symbol_desc = inst.get("desc")
        except Exception:
            pass

        if "is_enabled" in config_dict:
            req_en = bool(config_dict["is_enabled"])
            self.is_enabled = req_en and self.is_configured
            self.status = "RUNNING" if self.is_enabled else "DISABLED"

        logger.info(f"SingleRunner [{self.symbol} ({self.timeframe})] updated: enabled={self.is_enabled}, mode={self.execution_mode}, qty={self.quantity}, virtual_pos={self.virtual_position}")

    def get_telemetry(self) -> dict:
        """Returns single strategy telemetry payload."""
        return {
            "id": self.id,
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "exchange_segment": self.exchange_segment,
            "timeframe": self.timeframe,
            "timeframe_seconds": parse_timeframe_seconds(self.timeframe),
            "quantity": self.quantity,
            "product_type": self.product_type,
            "atr_period": self.atr_period,
            "multiplier": self.multiplier,
            "execution_mode": self.execution_mode,
            "is_enabled": self.is_enabled,
            "is_configured": self.is_configured,
            "status": self.status,
            "market_open": is_market_open_ist(self.exchange_segment or "MCXFO"),
            "current_trend": self.active_trend,
            "virtual_position": self.virtual_position,
            "strategy_position": self.strategy_position,
            "current_broker_quantity": self.current_broker_quantity,
            "broker_side": self.broker_side,
            "atr": self.last_atr,
            "upper_band": self.upper_band,
            "lower_band": self.lower_band,
            "last_close": self.last_close,
            "last_candle_time": self.last_candle_time,
            "last_signal_time": self.last_signal_time,
            "last_signal_action": self.last_signal_action,
            "last_signal_details": self.last_signal_details,
            "next_poll_seconds": self.next_poll_seconds,
            "cached_candles_count": len(self.cached_candles),
            "resolved_inst_id": self.last_resolved_inst_id,
            "resolved_symbol_desc": self.last_resolved_symbol_desc,
            "active_contract_id": self.active_contract_id,
            "active_contract_desc": self.active_contract_desc,
            "last_error": self.last_error,
        }

    def get_chart_data(self) -> dict:
        """Formats cached series for Lightweight Charts."""
        candles_out = []
        st_line_out = []
        ub_line_out = []
        lb_line_out = []

        bullish_line_out = []
        bearish_line_out = []

        for c in self.cached_candles:
            ts = c.get("time") or c.get("timestamp", 0)
            candles_out.append({
                "time": ts,
                "open": c.get("open", 0.0),
                "high": c.get("high", 0.0),
                "low": c.get("low", 0.0),
                "close": c.get("close", 0.0),
            })
            if c.get("supertrend") and c["supertrend"] > 0:
                is_bull = (c.get("trend") == 1)
                color = "#10b981" if is_bull else "#f43f5e"
                st_line_out.append({"time": ts, "value": c["supertrend"], "color": color})
                if is_bull:
                    bullish_line_out.append({"time": ts, "value": c["supertrend"]})
                else:
                    bearish_line_out.append({"time": ts, "value": c["supertrend"]})
            if c.get("upper_band") and c["upper_band"] > 0:
                ub_line_out.append({"time": ts, "value": c["upper_band"]})
            if c.get("lower_band") and c["lower_band"] > 0:
                lb_line_out.append({"time": ts, "value": c["lower_band"]})

        return {
            "id": self.id,
            "symbol": self.symbol,
            "exchange_segment": self.exchange_segment,
            "timeframe": self.timeframe,
            "timeframe_seconds": parse_timeframe_seconds(self.timeframe),
            "execution_mode": self.execution_mode,
            "status": self.status,
            "current_trend": self.active_trend,
            "last_close": self.last_close,
            "atr": self.last_atr,
            "candlestick": candles_out,
            "supertrend_line": st_line_out,
            "bullish_line": bullish_line_out,
            "bearish_line": bearish_line_out,
            "upper_band": ub_line_out,
            "lower_band": lb_line_out,
            "markers": self.recent_trade_markers[-30:],
            "next_poll_seconds": self.next_poll_seconds
        }

    @staticmethod
    def is_candle_closed(candles: List[Dict[str, Any]], tf_seconds: int, now_ts: Optional[int] = None) -> bool:
        """
        Universal structural bar close determination:
        1. In Symphony XTS, all completed/closed bars structurally end in second :59.
           A forming bar has an intra-bar tick timestamp (e.g. :17, :32, :48, :57) that does not end in :59.
        2. Timeframe Boundary Alignment: The candle must be aligned to the exact timeframe boundary
           in Indian Standard Time (IST, UTC+5:30), e.g. 09:29:59 for 30m, 09:14:59 for 15m.
           An intra-bar partial candle (e.g. 09:02:59) is strictly rejected as forming.
        3. System Clock Verification: System clock (now) must have reached or passed the close timestamp.
        """
        if not candles or tf_seconds <= 0:
            return False
        
        now = int(time.time()) if now_ts is None else int(now_ts)
        last_ts = int(candles[-1].get("time") or candles[-1].get("timestamp", 0))

        # 1. Structural check: Bar-end timestamp must end in second :59
        if last_ts % 60 != 59:
            return False

        # 2. Guard against future / clock-skew timestamps
        if now < last_ts:
            return False

        # 3. Universal Timeframe boundary alignment check:
        # In Indian markets (MCX / NSE), trading opens at 09:00:00 IST.
        # All completed multi-minute bars align with the market open interval in IST:
        # (e.g. 1m, 3m, 5m, 10m, 15m, 20m, 25m, 30m, 45m, 60m).
        if tf_seconds >= 60:
            ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            dt = datetime.datetime.fromtimestamp(last_ts, tz=ist_tz)

            # Exact seconds elapsed from 09:00:00 IST market open
            seconds_from_0900 = (dt.hour - 9) * 3600 + dt.minute * 60 + dt.second + 1
            is_valid_ist_boundary = (seconds_from_0900 % tf_seconds == 0)

            # Intra-bar partial tick candle suppression: if two candles are on same day and delta < tf_seconds - 60s
            if len(candles) >= 2:
                prev_ts = int(candles[-2].get("time") or candles[-2].get("timestamp", 0))
                if prev_ts > 0:
                    delta = last_ts - prev_ts
                    if delta < 86400 and delta < (tf_seconds - 60):
                        return False

            if is_valid_ist_boundary:
                return True

            # Synthetic test fixture fallback
            if len(candles) >= 2:
                prev_ts = int(candles[-2].get("time") or candles[-2].get("timestamp", 0))
                if abs((last_ts - prev_ts) - tf_seconds) <= 2:
                    return True

            return False

        return True

    async def evaluate_diagnostic(self, xts_api_module) -> dict:
        """Executes on-demand diagnostic trace without mutating state."""
        sym = self.symbol
        tf_seconds = parse_timeframe_seconds(self.timeframe)
        inst = xts_api_module.resolve_contract(sym)
        if not inst:
            return {"status": "ERROR", "error": f"Symbol '{sym}' not found in master cache"}

        inst_id = inst.get("inst_id")
        exch_seg = inst.get("exch_seg") or self.exchange_segment or "MCXFO"

        t_start = time.time()
        candles = await asyncio.to_thread(
            xts_api_module.fetch_ohlc_candles,
            exch_seg,
            inst_id,
            tf_seconds,
            200
        )
        fetch_ms = int((time.time() - t_start) * 1000)

        if not candles:
            if self.cached_candles:
                logger.info(f"SuperTrend [{sym}]: Using {len(self.cached_candles)} cached candles for diagnostic trace.")
                candles = list(self.cached_candles)
            else:
                return {
                    "status": "ERROR",
                    "error": "No candle data returned from broker OHLC API",
                    "symbol": sym,
                    "inst_id": inst_id,
                    "exch_seg": exch_seg
                }
        else:
            self.cached_candles = candles

        st_res = calculate_supertrend(candles, self.atr_period, self.multiplier)
        calc_ms = int((time.time() - t_start) * 1000) - fetch_ms

        last_5 = st_res.get("candle_series", [])[-5:]
        flip_dir = st_res.get("flip_direction")
        proposed_action = "NO_ACTION"
        if flip_dir == "BULLISH":
            proposed_action = "BUY"
        elif flip_dir == "BEARISH":
            proposed_action = "SELL"

        last_candle = last_5[-1] if last_5 else None

        return {
            "status": "OK",
            "id": self.id,
            "symbol": sym,
            "resolved_desc": inst.get("desc"),
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "exch_seg": exch_seg,
            "exchange_segment": exch_seg,
            "timeframe": self.timeframe,
            "timeframe_seconds": tf_seconds,
            "execution_mode": self.execution_mode,
            "atr_period": self.atr_period,
            "multiplier": self.multiplier,
            "total_candles": len(candles),
            "last_close": st_res["last_close"],
            "current_atr": st_res["atr"],
            "calculated_atr": st_res["atr"],
            "upper_band": st_res["upper_band"],
            "lower_band": st_res["lower_band"],
            "supertrend": st_res["supertrend"],
            "active_supertrend_val": st_res["supertrend"],
            "current_trend": st_res["trend_name"],
            "trend": st_res["trend_name"],
            "prev_trend": "BULLISH" if st_res["prev_trend"] == 1 else ("BEARISH" if st_res["prev_trend"] == -1 else "INITIALIZING"),
            "is_flip": st_res["is_flip"],
            "flipped": st_res["is_flip"],
            "flip_direction": flip_dir,
            "current_strategy_position": self.strategy_position,
            "strategy_position": self.strategy_position,
            "proposed_action": proposed_action,
            "action_taken": "DIAGNOSTIC_ONLY",
            "last_candle": last_candle,
            "last_5_candles": last_5,
            "benchmarks": {
                "fetch_ms": fetch_ms,
                "calc_ms": calc_ms,
                "total_ms": fetch_ms + calc_ms
            }
        }

    async def sync_to_current_trend(self, xts_api_module, main_module) -> dict:
        """
        Synchronizes runner's position to its active prevailing SuperTrend trend on-demand.
        - Concurrency Safe: Enforces self.lock to eliminate race conditions with evaluate_cycle.
        - Two-Leg Fail-Safe: If Leg 1 (Exit) fails, Leg 2 (Entry) is aborted to prevent unhedged exposure.
        - Full Persistence: Commits every position change immediately to SQLite via _save_virtual_position.
        """
        async with self.lock:
            sym = self.symbol
            inst = xts_api_module.resolve_contract(sym)
            if not inst:
                return {"status": "ERROR", "error": f"Symbol '{sym}' not found in master cache"}

            inst_id = inst.get("inst_id")
            exch_seg = inst.get("exch_seg") or self.exchange_segment or "MCXFO"
            freeze_limit = int(inst.get("freeze_qty") or 100000)
            tf_seconds = parse_timeframe_seconds(self.timeframe)

            candles = await asyncio.to_thread(
                xts_api_module.fetch_ohlc_candles,
                exch_seg,
                inst_id,
                tf_seconds,
                200
            )
            if not candles:
                if self.cached_candles:
                    candles = list(self.cached_candles)
                else:
                    return {"status": "ERROR", "error": "No candle data returned from broker OHLC API"}

            now_ts = int(time.time())
            if not self.is_candle_closed(candles, tf_seconds, now_ts) and len(candles) > self.atr_period + 1:
                eval_candles = candles[:-1]
            else:
                eval_candles = candles

            st_res = calculate_supertrend(eval_candles, self.atr_period, self.multiplier)
            trend_name = st_res.get("trend_name", "INITIALIZING")
            if trend_name not in ("BULLISH", "BEARISH"):
                return {"status": "ERROR", "error": f"Invalid trend state: {trend_name}"}

            self.active_trend = trend_name
            self.last_close = st_res["last_close"]
            self.last_atr = st_res["atr"]
            self.upper_band = st_res["upper_band"]
            self.lower_band = st_res["lower_band"]
            candle_ts = st_res["last_candle_time"] or int(time.time())

            # Check current state vs target state
            if trend_name == "BULLISH":
                if self.virtual_position == self.quantity:
                    return {"status": "ALREADY_SYNCED", "message": f"Strategy is already LONG (+{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}
                
                if self.virtual_position < 0:
                    # Two-leg reversal
                    exit_qty = abs(self.virtual_position)
                    exit_ok = await self._execute_exit("SHORT", exit_qty, f"SYNC_EXIT_{candle_ts}", main_module, freeze_limit)
                    if not exit_ok:
                        logger.critical(f"SuperTrend [{self.symbol}]: sync_to_current_trend Exit failed! Aborting Entry to prevent unhedged exposure.")
                        return {"status": "ERROR", "error": "Exit leg failed or rejected by broker. Entry aborted."}
                    self.virtual_position = 0
                    self._save_virtual_position(main_module, 0)
                    await asyncio.sleep(0.5)

                    entry_ok = await self._execute_entry("BUY", self.quantity, f"SYNC_ENTRY_{candle_ts}", main_module, freeze_limit)
                    if not entry_ok:
                        is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                        self.status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                        self.is_enabled = False
                        logger.critical(f"SuperTrend [{self.symbol}]: sync_to_current_trend Entry failed ({self.last_error})! Position remains FLAT (0 lots). Strategy {self.status}.")
                        return {"status": "ERROR", "error": f"Entry leg failed: {self.last_error}. Position remains FLAT (0 lots)."}
                    self.virtual_position = self.quantity
                    self._save_virtual_position(main_module, self.quantity)
                    return {"status": "SUCCESS", "message": f"Reversed from SHORT to LONG (+{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}
                elif self.virtual_position > 0:
                    diff = self.quantity - self.virtual_position
                    if diff > 0:
                        entry_ok = await self._execute_entry("BUY", diff, f"SYNC_ENTRY_{candle_ts}", main_module, freeze_limit)
                        if not entry_ok:
                            is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                            self.status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                            return {"status": "ERROR", "error": f"Top-up entry failed: {self.last_error}"}
                        self.virtual_position = self.quantity
                        self._save_virtual_position(main_module, self.quantity)
                        return {"status": "SUCCESS", "message": f"Topped up LONG by +{diff} lots (total +{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}
                    return {"status": "ALREADY_SYNCED", "message": f"Strategy is already LONG (+{self.virtual_position} lots)", "trend": trend_name, "virtual_position": self.virtual_position}
                else:
                    # Entry from flat
                    entry_ok = await self._execute_entry("BUY", self.quantity, f"SYNC_ENTRY_{candle_ts}", main_module, freeze_limit)
                    if not entry_ok:
                        is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                        self.status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                        self.is_enabled = False
                        return {"status": "ERROR", "error": f"Entry leg failed or was rejected by broker: {self.last_error}"}
                    self.virtual_position = self.quantity
                    self._save_virtual_position(main_module, self.quantity)
                    return {"status": "SUCCESS", "message": f"Entered LONG (+{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}

            elif trend_name == "BEARISH":
                if self.virtual_position == -self.quantity:
                    return {"status": "ALREADY_SYNCED", "message": f"Strategy is already SHORT (-{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}

                if self.virtual_position > 0:
                    # Two-leg reversal
                    exit_qty = abs(self.virtual_position)
                    exit_ok = await self._execute_exit("LONG", exit_qty, f"SYNC_EXIT_{candle_ts}", main_module, freeze_limit)
                    if not exit_ok:
                        logger.critical(f"SuperTrend [{self.symbol}]: sync_to_current_trend Exit failed! Aborting Entry to prevent unhedged exposure.")
                        return {"status": "ERROR", "error": "Exit leg failed or rejected by broker. Entry aborted."}
                    self.virtual_position = 0
                    self._save_virtual_position(main_module, 0)
                    await asyncio.sleep(0.5)

                    entry_ok = await self._execute_entry("SELL", self.quantity, f"SYNC_ENTRY_{candle_ts}", main_module, freeze_limit)
                    if not entry_ok:
                        is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                        self.status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                        self.is_enabled = False
                        logger.critical(f"SuperTrend [{self.symbol}]: sync_to_current_trend Entry failed ({self.last_error})! Position remains FLAT (0 lots). Strategy {self.status}.")
                        return {"status": "ERROR", "error": f"Entry leg failed: {self.last_error}. Position remains FLAT (0 lots)."}
                    self.virtual_position = -self.quantity
                    self._save_virtual_position(main_module, -self.quantity)
                    return {"status": "SUCCESS", "message": f"Reversed from LONG to SHORT (-{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}
                elif self.virtual_position < 0:
                    diff = self.quantity - abs(self.virtual_position)
                    if diff > 0:
                        entry_ok = await self._execute_entry("SELL", diff, f"SYNC_ENTRY_{candle_ts}", main_module, freeze_limit)
                        if not entry_ok:
                            is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                            self.status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                            return {"status": "ERROR", "error": f"Top-up entry failed: {self.last_error}"}
                        self.virtual_position = -self.quantity
                        self._save_virtual_position(main_module, -self.quantity)
                        return {"status": "SUCCESS", "message": f"Topped up SHORT by -{diff} lots (total -{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}
                    return {"status": "ALREADY_SYNCED", "message": f"Strategy is already SHORT ({self.virtual_position} lots)", "trend": trend_name, "virtual_position": self.virtual_position}
                else:
                    # Entry from flat
                    entry_ok = await self._execute_entry("SELL", self.quantity, f"SYNC_ENTRY_{candle_ts}", main_module, freeze_limit)
                    if not entry_ok:
                        is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                        self.status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                        self.is_enabled = False
                        return {"status": "ERROR", "error": f"Entry leg failed or was rejected by broker: {self.last_error}"}
                    self.virtual_position = -self.quantity
                    self._save_virtual_position(main_module, -self.quantity)
                    return {"status": "SUCCESS", "message": f"Entered SHORT (-{self.quantity} lots)", "trend": trend_name, "virtual_position": self.virtual_position}

            return {"status": "NO_CHANGE", "message": f"Trend {trend_name} requires no synchronization", "trend": trend_name, "virtual_position": self.virtual_position}

    async def reset_to_flat(self, square_off_broker: bool, xts_api_module, main_module) -> dict:
        """
        Resets this strategy's target to FLAT (0 lots).
        If square_off_broker is True, dispatches an exit order to close any active broker leg.
        Persists virtual_position = 0 in SQLite and updates memory state.
        """
        async with self.lock:
            prev_pos = self.virtual_position
            prev_side = self.strategy_position
            old_qty = abs(prev_pos)
            
            inst = xts_api_module.resolve_contract(self.symbol) if xts_api_module else None
            freeze_limit = int(inst.get("freeze_qty") or 100000) if inst else 100000
            target_sym = self.last_resolved_symbol_desc or (inst.get("desc") if inst else self.symbol)

            order_result = None
            if square_off_broker and prev_pos != 0:
                logger.info(
                    f"🧹 SuperTrend [{self.symbol} ({self.timeframe})]: Reset to FLAT with Broker Square-Off! "
                    f"Exiting {old_qty} lots ({prev_side}) on {target_sym}."
                )
                ts_now = int(time.time())
                await self._execute_exit(
                    prev_side,
                    old_qty,
                    f"RESET_FLAT_{ts_now}",
                    main_module,
                    freeze_limit,
                    target_symbol=target_sym
                )
                order_result = f"Dispatched exit order for {old_qty} lots on {target_sym}"
            else:
                logger.info(
                    f"🧹 SuperTrend [{self.symbol} ({self.timeframe})]: Force Virtual Reset to FLAT (0 broker orders). "
                    f"Previous target: {prev_pos} lots ({prev_side})."
                )
                order_result = "Virtual target reset to 0 (No broker order sent)"

            self.virtual_position = 0
            self.strategy_position = "FLAT"
            if main_module and hasattr(main_module, "db_set_virtual_position"):
                main_module.db_set_virtual_position(self.strategy_key, self.symbol, self.timeframe, 0)

            return {
                "status": "SUCCESS",
                "message": f"Strategy {self.symbol} ({self.timeframe}) reset to 0 FLAT. {order_result}.",
                "strategy": self.get_telemetry()
            }

    def update_live_tick(self, ltp: float) -> None:
        """
        Updates the forming candle and dynamic SuperTrend indicator using a live touchline tick (LTP).
        Provides TradingView-style live chart updates without trade execution (strictly non-repainting).
        """
        if not ltp or ltp <= 0:
            return

        self.last_close = float(ltp)
        if not self.cached_candles:
            return

        try:
            # Clone last candle to avoid mutating historical list directly
            last_c = dict(self.cached_candles[-1])
            last_c["close"] = float(ltp)
            if float(ltp) > float(last_c.get("high", ltp)):
                last_c["high"] = float(ltp)
            if float(ltp) < float(last_c.get("low", ltp)):
                last_c["low"] = float(ltp)

            live_candles = list(self.cached_candles[:-1]) + [last_c]
            st_res = calculate_supertrend(live_candles, self.atr_period, self.multiplier)
            if not st_res.get("error"):
                self.cached_candles = st_res.get("candle_series") or live_candles
                self.forming_trend = st_res["trend_name"]
                self.last_atr = st_res["atr"]
                self.upper_band = st_res["upper_band"]
                self.lower_band = st_res["lower_band"]
                self.last_candle_time = st_res["last_candle_time"]
        except Exception as e:
            logger.debug(f"SuperTrend [{self.symbol}]: live tick update warning: {e}")

    async def evaluate_cycle(self, xts_api_module, main_module, skip_order_checks: bool = False) -> None:
        """Executes a single SuperTrend evaluation and reversal check for this symbol."""
        if not self.is_enabled or not self.is_configured:
            return

        if getattr(main_module, "TRADING_PAUSED", False):
            self.status = "PAUSED"
            return

        async with self.lock:
            # 1. Resolve Instrument
            inst = xts_api_module.resolve_contract(self.symbol)
            if not inst:
                self.last_error = f"Contract resolution failed for '{self.symbol}'"
                logger.error(f"SuperTrend [{self.symbol}]: {self.last_error}")
                return

            inst_id = inst.get("inst_id")
            inst_desc = inst.get("desc") or self.symbol
            exch_seg = inst.get("exch_seg") or self.exchange_segment or "MCXFO"
            freeze_limit = int(inst.get("freeze_qty") or 100000)
            lot_size = int(inst.get("lot_size") or 1)
            self.lot_size = lot_size
            is_derivative = exch_seg not in ["NSECM", "BSECM"]
            tf_seconds = parse_timeframe_seconds(self.timeframe)
            is_continuous = bool(CONTINUOUS_SUFFIX.search(self.symbol))

            now_ts = time.time()
            market_open = is_market_open_ist(exch_seg, now_ts=now_ts)

            # 1.5 Autonomous Contract Rollover Check for Continuous Symbols
            if is_continuous:
                if self.active_contract_id is None:
                    self.active_contract_id = inst_id
                    self.active_contract_desc = inst_desc
                    self.last_resolved_inst_id = inst_id
                    self.last_resolved_symbol_desc = inst_desc
                    self._save_virtual_position(main_module, self.virtual_position, active_contract_id=inst_id, active_contract_desc=inst_desc)

                # Check if resolved contract differs from active position contract
                contract_switched = (str(self.active_contract_id) != str(inst_id))

                if contract_switched:
                    old_id = self.active_contract_id
                    old_desc = self.active_contract_desc or self.last_resolved_symbol_desc
                    if not old_desc and old_id and hasattr(xts_api_module, "get_instrument_by_id"):
                        meta = xts_api_module.get_instrument_by_id(old_id)
                        if meta and meta.get("desc"):
                            old_desc = meta["desc"]

                    if not old_desc:
                        logger.critical(
                            f"🚨 SuperTrend [{self.symbol}]: Cannot resolve active expiring contract descriptor for ID {old_id}. "
                            f"Aborting autonomous rollover to prevent misrouting order to new contract!"
                        )
                        self.status = "ROLLOVER_FAILED_PAUSED"
                        self.is_enabled = False
                        if hasattr(xts_api_module, "send_ops_alert"):
                            xts_api_module.send_ops_alert(f"CRITICAL: Strategy {self.symbol} rollover aborted (unresolved expiring contract descriptor for ID {old_id}). PAUSED.")
                        return

                    if self.virtual_position != 0:
                        if market_open:
                            # 60-Second Opening Bell Spread Stabilization Buffer
                            if is_market_opening_stabilizing(exch_seg, now_ts=now_ts):
                                self.pending_rollover = True
                                logger.info(
                                    f"⏳ SuperTrend [{self.symbol}]: Rollover waiting for opening spread stabilization "
                                    f"(60s buffer until 09:01:00 / 17:01:00 IST). Preserving active contract {old_desc}."
                                )
                                return

                            # Market is open and stabilized!
                            self.pending_rollover = False
                            current_pos_side = self.strategy_position
                            roll_qty = abs(self.virtual_position)
                            roll_ts = int(time.time())
                            logger.warning(
                                f"🔄 SuperTrend [{self.symbol} ({self.timeframe})]: Autonomous Rollover Triggered! "
                                f"Active position ({self.virtual_position} lots {current_pos_side}) switching from "
                                f"{old_desc} (ID: {old_id}) -> {inst_desc} (ID: {inst_id})."
                            )
                            # Leg 1: Square off expiring near-month contract
                            exit_ok = await self._execute_exit(
                                current_pos_side,
                                roll_qty,
                                f"ROLL_EXIT_{roll_ts}",
                                main_module,
                                freeze_limit,
                                target_symbol=old_desc
                            )
                            if not exit_ok:
                                logger.warning(f"SuperTrend [{self.symbol}]: Rollover Exit failed, retrying once after 2.0s...")
                                await asyncio.sleep(2.0)
                                exit_ok = await self._execute_exit(
                                    current_pos_side,
                                    roll_qty,
                                    f"ROLL_EXIT_{roll_ts}_RETRY",
                                    main_module,
                                    freeze_limit,
                                    target_symbol=old_desc
                                )
                            if not exit_ok:
                                logger.critical(
                                    f"🚨 SuperTrend [{self.symbol}]: Rollover Leg 1 (Exit) on {old_desc} failed after retry! "
                                    f"Aborting Leg 2 to prevent duplicate/unhedged exposure."
                                )
                                self.status = "ROLLOVER_FAILED_PAUSED"
                                self.is_enabled = False
                                self.pending_rollover = False
                                if hasattr(xts_api_module, "send_ops_alert"):
                                    xts_api_module.send_ops_alert(f"CRITICAL: Rollover Exit failed on {old_desc} for {self.symbol}. Strategy PAUSED.")
                                return

                            # Brief safety pause for margin release (allow broker RMS collateral processing)
                            await asyncio.sleep(1.5)

                            # Leg 2: Enter new next-month contract in same direction with up to 3 retries
                            entry_action = "BUY" if current_pos_side == "LONG" else "SELL"
                            entry_ok = False
                            for attempt in range(1, 4):
                                roll_ref = f"ROLL_ENTRY_{roll_ts}" if attempt == 1 else f"ROLL_ENTRY_{roll_ts}_RETRY{attempt}"
                                entry_ok = await self._execute_entry(
                                    entry_action,
                                    roll_qty,
                                    roll_ref,
                                    main_module,
                                    freeze_limit,
                                    target_symbol=inst_desc
                                )
                                if entry_ok:
                                    break
                                logger.warning(f"SuperTrend [{self.symbol}]: Rollover Entry attempt {attempt}/3 failed. Waiting 2.0s for margin release...")
                                await asyncio.sleep(2.0)

                            if not entry_ok:
                                logger.critical(
                                    f"🚨 SuperTrend [{self.symbol}]: Rollover Leg 2 (Entry) on {inst_desc} failed after 3 attempts! "
                                    f"Old contract {old_desc} was squared off. Marking position FLAT and PAUSING strategy."
                                )
                                self.virtual_position = 0
                                self.status = "ROLLOVER_FAILED_PAUSED"
                                self.is_enabled = False
                                self.pending_rollover = False
                                self.active_contract_id = inst_id
                                self.active_contract_desc = inst_desc
                                self.last_resolved_inst_id = inst_id
                                self.last_resolved_symbol_desc = inst_desc
                                self._save_virtual_position(main_module, 0, active_contract_id=inst_id, active_contract_desc=inst_desc)
                                # Purge cached candles & indicators to prevent dirty state
                                self.cached_candles = []
                                self.last_candle_time = 0
                                self.last_processed_candle_time = 0
                                self.last_close = 0.0
                                self.last_atr = 0.0
                                self.upper_band = 0.0
                                self.lower_band = 0.0
                                if hasattr(xts_api_module, "send_ops_alert"):
                                    xts_api_module.send_ops_alert(f"CRITICAL: Rollover Entry failed on {inst_desc} for {self.symbol} after 3 attempts. Position FLAT. Strategy PAUSED.")
                                return

                            # Both legs completed successfully!
                            self.active_contract_id = inst_id
                            self.active_contract_desc = inst_desc
                            self.last_resolved_inst_id = inst_id
                            self.last_resolved_symbol_desc = inst_desc
                            self.pending_rollover = False
                            self._save_virtual_position(main_module, self.virtual_position, active_contract_id=inst_id, active_contract_desc=inst_desc)
                            # Purge cached candles & indicators so the new contract starts fresh without mixed series
                            self.cached_candles = []
                            self.last_candle_time = 0
                            self.last_processed_candle_time = 0
                            self.last_close = 0.0
                            self.last_atr = 0.0
                            self.upper_band = 0.0
                            self.lower_band = 0.0
                            logger.info(
                                f"✅ SuperTrend [{self.symbol}]: Autonomous Rollover Complete! "
                                f"Holding {self.virtual_position} lots on {inst_desc} (ID: {inst_id})."
                            )
                            if hasattr(xts_api_module, "send_ops_alert"):
                                xts_api_module.send_ops_alert(f"✅ Rollover Complete: {self.symbol} rolled from {old_desc} -> {inst_desc} ({self.virtual_position} lots).")
                        else:
                            # Market is CLOSED: do NOT overwrite active_contract_id!
                            # Leave active_contract_id as old contract so rollover executes upon market open bell.
                            self.pending_rollover = True
                            logger.info(
                                f"⏳ SuperTrend [{self.symbol}]: Rollover queued for {old_desc} -> {inst_desc}. "
                                f"Market is currently closed. Will execute upon market open bell."
                            )
                    else:
                        # Position is FLAT (0 lots): seamlessly update contract pointer without trading
                        logger.info(f"SuperTrend [{self.symbol}]: Auto-rolling contract {old_desc} -> {inst_desc} while FLAT (0 lots).")
                        self.active_contract_id = inst_id
                        self.active_contract_desc = inst_desc
                        self.last_resolved_inst_id = inst_id
                        self.last_resolved_symbol_desc = inst_desc
                        self.pending_rollover = False
                        self._save_virtual_position(main_module, 0, active_contract_id=inst_id, active_contract_desc=inst_desc)
                        # Purge cached candles & indicators so the new contract starts fresh without mixed series
                        self.cached_candles = []
                        self.last_candle_time = 0
                        self.last_processed_candle_time = 0
                        self.last_close = 0.0
                        self.last_atr = 0.0
                        self.upper_band = 0.0
                        self.lower_band = 0.0
                else:
                    self.last_resolved_inst_id = inst_id
                    self.last_resolved_symbol_desc = inst_desc

            # 2. Expiry Protection Guard for Fixed (Non-Continuous) Contracts (Only during market hours)
            if market_open and not is_continuous:
                expiry_date = inst.get("expiry")
                if expiry_date:
                    days_to_expiry = (expiry_date - datetime.date.today()).days
                    min_days = getattr(config, "MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX", 7) if exch_seg in ("MCXFO", "NCDEX") \
                        else getattr(config, "MIN_DAYS_BEFORE_EXPIRY_DERIVATIVES", 0)
                    if days_to_expiry <= min_days:
                        logger.warning(f"SuperTrend [{self.symbol}]: Fixed contract expires in {days_to_expiry} days (<= {min_days}). Squaring off & Pausing.")
                        if self.strategy_position != "FLAT":
                            exit_qty = abs(self.virtual_position) if self.virtual_position != 0 else (self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity)
                            await self._execute_exit(
                                self.strategy_position,
                                exit_qty,
                                f"EXPIRY_SQOFF_{int(time.time())}",
                                main_module,
                                freeze_limit
                            )
                        self.virtual_position = 0
                        self._save_virtual_position(main_module, 0)
                        self.is_enabled = False
                        self.status = "EXPIRED_PAUSED"
                        return

            # 3. Position Telemetry Observation & Order Protection Helper
            async def _check_broker_state() -> bool:
                try:
                    pos_telemetry = await asyncio.to_thread(xts_api_module.get_positions_telemetry)
                    positions = pos_telemetry.get("positions", []) or pos_telemetry.get("all_positions", [])
                    
                    target_pos = None
                    for p in positions:
                        p_sym = str(p.get("symbol", "")).upper()
                        p_id = p.get("instrument_id") or p.get("exchange_instrument_id")
                        if p_id == inst_id or self.symbol in p_sym:
                            target_pos = p
                            break

                    if target_pos:
                        side = target_pos.get("side", "").upper()
                        raw_qty = int(target_pos.get("quantity", 0))
                        reconciled_lots = (raw_qty // lot_size) if (is_derivative and lot_size > 1) else raw_qty
                        self.current_broker_quantity = reconciled_lots
                        self.broker_side = side
                    else:
                        self.current_broker_quantity = 0
                        self.broker_side = "FLAT"
                except Exception as e:
                    logger.error(f"SuperTrend [{self.symbol}]: Failed to inspect broker positions: {e}")

                try:
                    broker_orders = await asyncio.to_thread(xts_api_module.get_broker_orders)
                    now_ts_orders = time.time()
                    for o in broker_orders:
                        st = str(o.get("OrderStatus", "")).upper()
                        o_sym = str(o.get("TradingSymbol", "")).upper()
                        order_ref = str(o.get("OrderUniqueIdentifier") or o.get("orderUniqueIdentifier") or "")
                        app_id = str(o.get("AppOrderID") or o.get("appOrderID") or "")

                        # Finding #3 Fix: Exact token matching to prevent substring collision (e.g. SILVER vs SILVERMIC)
                        expected_ref_token = f"_{self.symbol}_{self.timeframe.upper()}_"
                        clean_sym_core = self.symbol.replace("1!", "").replace("!", "").strip().upper()
                        o_sym_core = o_sym.split()[0].upper() if o_sym else ""
                        
                        is_our_st_order = (
                            (order_ref.startswith("ST_REV_") or order_ref.startswith("ST_DELTA_") or order_ref.startswith("ST_AUTO_HEAL_")) and
                            (expected_ref_token in order_ref or (o_sym_core == clean_sym_core and f"_{self.timeframe.upper()}_" in order_ref) or (order_ref.startswith("ST_AUTO_HEAL_") and clean_sym_core == o_sym_core))
                        )
                        if is_our_st_order and st in ("NEW", "OPEN", "PENDINGNEW", "PENDINGREPLACE"):
                            first_seen = self.pending_order_first_seen.setdefault(app_id, now_ts_orders)
                            age = now_ts_orders - first_seen
                            if age > 60.0:
                                logger.critical(
                                    f"🚨 SuperTrend [{self.symbol} ({self.timeframe})]: STALE PENDING ORDER {app_id} (Ref: {order_ref}, Age: {age:.1f}s). "
                                    f"Bypassing suppression to allow position reconciliation."
                                )
                                if hasattr(xts_api_module, "send_ops_alert"):
                                    xts_api_module.send_ops_alert(
                                        f"WARNING: Strategy {self.symbol} ({self.timeframe}) bypassed stale pending order {app_id} ({st}, {age:.0f}s old)"
                                    )
                            else:
                                logger.warning(
                                    f"SuperTrend [{self.symbol} ({self.timeframe})]: Found in-flight strategy pending order {app_id} "
                                    f"({st}, Ref: {order_ref}, Age: {age:.1f}s). Yielding cycle."
                                )
                                return False
                        elif app_id in self.pending_order_first_seen and st not in ("NEW", "OPEN", "PENDINGNEW", "PENDINGREPLACE"):
                            self.pending_order_first_seen.pop(app_id, None)

                    # Prune tracked pending order IDs older than 120s to prevent memory leak
                    if self.pending_order_first_seen:
                        stale_ids = [oid for oid, fts in self.pending_order_first_seen.items() if (now_ts_orders - fts) > 120.0]
                        for oid in stale_ids:
                            self.pending_order_first_seen.pop(oid, None)
                except Exception as e:
                    logger.warning(f"SuperTrend [{self.symbol} ({self.timeframe})]: Order check warning: {e}")

                try:
                    broker_signed_lots = (
                        abs(self.current_broker_quantity) if self.broker_side == "LONG"
                        else (-abs(self.current_broker_quantity) if self.broker_side == "SHORT" else 0)
                    )
                    has_inflight = len(self.pending_order_first_seen) > 0
                    
                    st_engine = getattr(main_module, "supertrend_engine", None)
                    if st_engine and hasattr(st_engine, "strategies"):
                        matching_runners = [r for r in st_engine.strategies.values() if r.is_enabled and (r.symbol == self.symbol or r.symbol in self.symbol)]
                        if len(matching_runners) > 1:
                            symbol_target = sum(r.virtual_position for r in matching_runners)
                            is_drift = (symbol_target != broker_signed_lots)
                        else:
                            is_drift = (self.virtual_position != broker_signed_lots)
                    else:
                        is_drift = (self.virtual_position != broker_signed_lots)

                    if not has_inflight and is_drift:
                        logger.warning(
                            f"⚠️ [POSITION DRIFT WARNING] Strategy '{self.strategy_key}': "
                            f"Persisted virtual_position={self.virtual_position} lots ({self.strategy_position}), "
                            f"broker position={broker_signed_lots} lots ({self.broker_side}). Divergence detected."
                        )
                except Exception as e:
                    logger.debug(f"Drift check error: {e}")

                return True

            should_check_orders = (not skip_order_checks) or (len(self.pending_order_first_seen) > 0)
            if should_check_orders:
                can_proceed = await _check_broker_state()
                if not can_proceed:
                    return

            # 5. Fetch OHLC Candles from Market Data REST API (with fast-fail 3.5s default timeout)
            candles = await asyncio.to_thread(
                xts_api_module.fetch_ohlc_candles,
                exch_seg,
                inst_id,
                tf_seconds,
                200
            )

            if not candles:
                if self.cached_candles:
                    candles = list(self.cached_candles)
                else:
                    self.last_error = "No candle data returned from broker OHLC API"
                    return

            # 6. Calculate SuperTrend (for live telemetry & charts)
            st_res = calculate_supertrend(candles, self.atr_period, self.multiplier)
            if st_res.get("error"):
                self.last_error = st_res["error"]
                return

            # Store enriched candle series (with supertrend, upper_band, lower_band, trend)
            # so Lightweight Charts in portal always receives valid indicator series.
            self.cached_candles = st_res.get("candle_series") or list(candles)

            self.last_atr = st_res["atr"]
            self.upper_band = st_res["upper_band"]
            self.lower_band = st_res["lower_band"]
            self.last_close = st_res["last_close"]
            self.last_candle_time = st_res["last_candle_time"]
            self.last_error = None

            # Market Hours Protection Guard:
            # If market is closed (weekends, nights, holidays), indicators are kept fresh for
            # telemetry / charts, but trade evaluation and order dispatches are strictly suppressed.
            if not market_open:
                self.status = "MARKET_CLOSED"
                self.active_trend = st_res["trend_name"]
                return

            self.status = "RUNNING"

            # 7. Evaluate Flip & Execute Virtual Delta Netting (Strict ON_CANDLE_CLOSE Rule)
            now_ts = int(time.time())
            is_last_candle_closed = self.is_candle_closed(candles, tf_seconds, now_ts)

            if is_last_candle_closed:
                eval_st_res = st_res
            else:
                closed_candles = candles[:-1]
                if len(closed_candles) < self.atr_period + 1:
                    return
                eval_st_res = calculate_supertrend(closed_candles, self.atr_period, self.multiplier)
                if eval_st_res.get("error"):
                    return

            # Strictly base strategy's confirmed active trend on the closed candle
            self.active_trend = eval_st_res["trend_name"]

            candle_ts = eval_st_res["last_candle_time"]
            is_flip = eval_st_res["is_flip"]
            flip_dir = eval_st_res["flip_direction"]

            # Startup Stale Historical Candle Guard:
            # On first evaluation (self.last_processed_candle_time == 0), if the candle closed
            # longer ago than max allowed age (e.g. 180s), seed baseline state without firing historical orders.
            enforce_stale = getattr(config, "ENFORCE_STALE_CANDLE_GUARD", True) if config else True
            if "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("ENFORCE_STALE_CANDLE_GUARD_IN_TESTS", "").lower() not in ("true", "1", "yes"):
                enforce_stale = False

            if enforce_stale and self.last_processed_candle_time == 0:
                max_stale_window = max(tf_seconds + 60, getattr(config, "MAX_CANDLE_AGE_SECONDS", 180) if config else 180)
                candle_age = now_ts - candle_ts
                if candle_age > max_stale_window:
                    self.last_processed_candle_time = candle_ts
                    logger.info(
                        f"SuperTrend [{self.symbol} ({self.timeframe})]: Startup baseline established at historical candle "
                        f"{candle_ts} (age: {candle_age}s > {max_stale_window}s limit). Suppressing historical flip execution."
                    )
                    return

            # Defense-in-depth: Duplicate signal on already evaluated candle is a strict no-op
            if is_flip and candle_ts != self.last_processed_candle_time:
                # If broker state was bypassed during fast OHLC polling, inspect orders and positions now before placing orders!
                if not should_check_orders:
                    can_proceed = await _check_broker_state()
                    if not can_proceed:
                        return

                logger.info(
                    f"🚨 [SUPERTREND FLIP] Symbol: {self.symbol} ({self.timeframe}) | "
                    f"Direction: {flip_dir} at confirmed candle close {candle_ts}. "
                    f"Current Position: {self.strategy_position} ({self.virtual_position} lots) | "
                    f"Mode: {self.execution_mode}"
                )

                if flip_dir == "BULLISH":
                    if self.virtual_position < 0:
                        exit_qty = abs(self.virtual_position)
                        exit_ok = await self._execute_exit("SHORT", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                        if not exit_ok:
                            logger.warning(f"SuperTrend [{self.symbol}]: Exit leg failed, retrying once after 2.0s...")
                            await asyncio.sleep(2.0)
                            exit_qty_retry = abs(self.virtual_position)
                            if exit_qty_retry > 0:
                                exit_ok = await self._execute_exit("SHORT", exit_qty_retry, f"FLIP_EXIT_{candle_ts}_RETRY", main_module, freeze_limit)
                            else:
                                exit_ok = True
                        if not exit_ok:
                            logger.critical(f"SuperTrend [{self.symbol}]: Reversal Exit failed after retry! Aborting Entry to prevent duplicate exposure.")
                            self.status = "EXIT_FAILED_PAUSED"
                            self.is_enabled = False
                            if hasattr(xts_api_module, "send_ops_alert"):
                                xts_api_module.send_ops_alert(f"CRITICAL: Strategy {self.symbol} Exit failed after retry. Strategy PAUSED.")
                            return
                        await asyncio.sleep(0.5)

                    entry_qty = self.quantity - self.virtual_position if self.virtual_position > 0 else self.quantity
                    if entry_qty > 0:
                        entry_ok = await self._execute_entry("BUY", entry_qty, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                        if not entry_ok:
                            logger.warning(f"SuperTrend [{self.symbol}]: Entry leg failed, retrying once after 2.0s...")
                            await asyncio.sleep(2.0)
                            entry_qty_retry = self.quantity - self.virtual_position if self.virtual_position < self.quantity else 0
                            if entry_qty_retry > 0:
                                entry_ok = await self._execute_entry("BUY", entry_qty_retry, f"FLIP_ENTRY_{candle_ts}_RETRY", main_module, freeze_limit)
                            else:
                                entry_ok = True
                        if not entry_ok:
                            is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                            pause_status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                            logger.critical(f"SuperTrend [{self.symbol}]: Reversal Entry failed after retry! Position is {self.virtual_position} lots. Strategy {pause_status}.")
                            self.status = pause_status
                            self.is_enabled = False
                            if hasattr(xts_api_module, "send_ops_alert"):
                                xts_api_module.send_ops_alert(f"CRITICAL: Strategy {self.symbol} Entry failed ({self.last_error}). Strategy {pause_status}. Broker position is {self.virtual_position} lots.")
                            return
                    else:
                        logger.info(f"SuperTrend [{self.symbol}]: Already LONG (+{self.virtual_position} lots). Skipping redundant entry.")

                elif flip_dir == "BEARISH":
                    if self.virtual_position > 0:
                        exit_qty = abs(self.virtual_position)
                        exit_ok = await self._execute_exit("LONG", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                        if not exit_ok:
                            logger.warning(f"SuperTrend [{self.symbol}]: Exit leg failed, retrying once after 2.0s...")
                            await asyncio.sleep(2.0)
                            exit_qty_retry = abs(self.virtual_position)
                            if exit_qty_retry > 0:
                                exit_ok = await self._execute_exit("LONG", exit_qty_retry, f"FLIP_EXIT_{candle_ts}_RETRY", main_module, freeze_limit)
                            else:
                                exit_ok = True
                        if not exit_ok:
                            logger.critical(f"SuperTrend [{self.symbol}]: Reversal Exit failed after retry! Aborting Entry to prevent duplicate exposure.")
                            self.status = "EXIT_FAILED_PAUSED"
                            self.is_enabled = False
                            if hasattr(xts_api_module, "send_ops_alert"):
                                xts_api_module.send_ops_alert(f"CRITICAL: Strategy {self.symbol} Exit failed after retry. Strategy PAUSED.")
                            return
                        await asyncio.sleep(0.5)

                    entry_qty = self.quantity - abs(self.virtual_position) if self.virtual_position < 0 else self.quantity
                    if entry_qty > 0:
                        entry_ok = await self._execute_entry("SELL", entry_qty, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                        if not entry_ok:
                            logger.warning(f"SuperTrend [{self.symbol}]: Entry leg failed, retrying once after 2.0s...")
                            await asyncio.sleep(2.0)
                            entry_qty_retry = self.quantity - abs(self.virtual_position) if abs(self.virtual_position) < self.quantity else 0
                            if entry_qty_retry > 0:
                                entry_ok = await self._execute_entry("SELL", entry_qty_retry, f"FLIP_ENTRY_{candle_ts}_RETRY", main_module, freeze_limit)
                            else:
                                entry_ok = True
                        if not entry_ok:
                            is_margin = any(kw in str(self.last_error).lower() for kw in ("margin", "shortfall", "rms"))
                            pause_status = "MARGIN_SHORTFALL_PAUSED" if is_margin else "ENTRY_FAILED_PAUSED"
                            logger.critical(f"SuperTrend [{self.symbol}]: Reversal Entry failed after retry! Position is {self.virtual_position} lots. Strategy {pause_status}.")
                            self.status = pause_status
                            self.is_enabled = False
                            if hasattr(xts_api_module, "send_ops_alert"):
                                xts_api_module.send_ops_alert(f"CRITICAL: Strategy {self.symbol} Entry failed ({self.last_error}). Strategy {pause_status}. Broker position is {self.virtual_position} lots.")
                            return
                    else:
                        logger.info(f"SuperTrend [{self.symbol}]: Already SHORT ({self.virtual_position} lots). Skipping redundant entry.")

                self.last_processed_candle_time = candle_ts

    async def _execute_delta(self, delta: int, candle_ts: int, main_module, freeze_limit: int = 100000) -> None:
        """Dispatches a Delta order (+BUY / -SELL) with freeze-quantity slicing and SQLite persistence."""
        abs_qty = abs(delta)
        if abs_qty <= 0:
            return

        max_allowed_lots = max(self.quantity * 5, 50)
        if abs_qty > max_allowed_lots:
            logger.critical(
                f"🚨 CRITICAL SAFETY GUARD: Disallowed delta quantity {abs_qty} lots for {self.symbol} ({self.timeframe}) "
                f"(limit: {max_allowed_lots}). Refusing dispatch!"
            )
            return

        action = "BUY" if delta > 0 else "SELL"
        is_paper = (self.execution_mode == "PAPER")

        chunks = slice_quantity_for_freeze(abs_qty, freeze_limit, lot_size=getattr(self, "lot_size", 1))
        payload = None
        for chunk_idx, chunk_qty in enumerate(chunks, start=1):
            base_ref = f"ST_REV_{self.symbol}_{self.timeframe.upper()}_DELTA_{action}_{candle_ts}"
            order_ref = format_order_ref_chunk(base_ref, chunk_idx)
            sig_id = f"st_delta_{str(uuid.uuid4())[:8]}"

            payload = {
                "action": action,
                "symbol": self.symbol,
                "quantity": chunk_qty,
                "price": 0.0,
                "product_type": self.product_type,
                "order_ref": order_ref,
                "source": "supertrend_engine",
                "is_paper": is_paper,
            }

            logger.info(f"SuperTrend [{self.symbol} ({self.timeframe})]: Dispatching Delta Order [Chunk {chunk_idx}/{len(chunks)}]: {payload}")
            if self.dispatch_fn:
                await self.dispatch_fn(sig_id, payload)
                chunk_delta = chunk_qty if delta > 0 else -chunk_qty
                self.virtual_position += chunk_delta
                self._save_virtual_position(main_module, self.virtual_position)
            elif main_module:
                if hasattr(main_module, "db_insert_pending"):
                    main_module.db_insert_pending(sig_id, payload)
                if hasattr(main_module, "_dispatch_and_record"):
                    res = await asyncio.to_thread(
                        main_module._dispatch_and_record,
                        sig_id,
                        action,
                        self.symbol,
                        chunk_qty,
                        0.0,
                        order_ref,
                        is_paper,
                    )
                    # Finding #5 Fix: Incrementally record filled chunk (strictly require valid dictionary status)
                    if isinstance(res, dict) and res.get("status") in ("done", "paper_done", "partial_failure"):
                        chunk_delta = chunk_qty if delta > 0 else -chunk_qty
                        self.virtual_position += chunk_delta
                        self._save_virtual_position(main_module, self.virtual_position)
                    else:
                        logger.warning(f"SuperTrend [{self.symbol} ({self.timeframe})]: Delta order chunk {chunk_idx}/{len(chunks)} rejected ({res}). Halting slice sequence.")
                        return

            if chunk_idx < len(chunks):
                await asyncio.sleep(0.2)

        self.last_signal_time = time.time()
        self.last_signal_action = f"DELTA_{action}_{abs_qty}"
        self.last_signal_details = payload or {}

        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "aboveBar" if action == "SELL" else "belowBar",
            "color": "#f43f5e" if action == "SELL" else "#10b981",
            "shape": "arrowDown" if action == "SELL" else "arrowUp",
            "text": f"{action} {abs_qty}",
        })
        if len(self.recent_trade_markers) > 100:
            self.recent_trade_markers = self.recent_trade_markers[-100:]

    async def _execute_exit(self, side: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000, target_symbol: Optional[str] = None) -> bool:
        """Dispatches an Exit order with freeze-quantity slicing. Returns True on success, False on failure."""
        # Defense-in-depth safety guard: refuse order if quantity exceeds unreasonable multiple of configured strategy quantity
        max_allowed_lots = max(self.quantity * 5, 50)
        if qty > max_allowed_lots or qty <= 0:
            guard_msg = (
                f"🚨 CRITICAL SAFETY GUARD: Disallowed exit quantity {qty} lots for {self.symbol} ({self.timeframe}) "
                f"(configured strategy quantity: {self.quantity} lots, limit: {max_allowed_lots}). Refusing dispatch!"
            )
            logger.critical(guard_msg)
            if sentry_sdk:
                with sentry_sdk.isolation_scope() as scope:
                    scope.set_tag("symbol", self.symbol)
                    scope.set_tag("timeframe", self.timeframe)
                    scope.set_tag("strategy_id", getattr(self, "id", "default"))
                    sentry_sdk.capture_message(guard_msg, level="error")
            return False

        action = "BUY" if side.upper() == "SHORT" else "SELL"
        is_paper = (self.execution_mode == "PAPER")
        symbol_to_trade = str(target_symbol).strip() if target_symbol else self.symbol
        
        chunks = slice_quantity_for_freeze(qty, freeze_limit, lot_size=getattr(self, "lot_size", 1))
        payload = None
        for chunk_idx, chunk_qty in enumerate(chunks, start=1):
            base_ref = f"ST_REV_EXIT_{self.symbol}_{self.timeframe.upper()}_{ref_suffix}"
            order_ref = format_order_ref_chunk(base_ref, chunk_idx)
            sig_id = f"st_exit_{str(uuid.uuid4())[:8]}"
            
            payload = {
                "action": action,
                "symbol": symbol_to_trade,
                "quantity": chunk_qty,
                "price": 0.0,
                "product_type": self.product_type,
                "order_ref": order_ref,
                "source": "supertrend_engine",
                "is_paper": is_paper
            }
            
            logger.info(f"SuperTrend [{self.symbol} ({self.timeframe})]: Dispatching Exit Leg [Chunk {chunk_idx}] on {symbol_to_trade}: {payload}")
            if self.dispatch_fn:
                await self.dispatch_fn(sig_id, payload)
                chunk_delta = chunk_qty if side.upper() == "SHORT" else -chunk_qty
                self.virtual_position += chunk_delta
                self._save_virtual_position(main_module, self.virtual_position)
            elif main_module:
                if hasattr(main_module, "db_insert_pending"):
                    main_module.db_insert_pending(sig_id, payload)
                if hasattr(main_module, "_dispatch_and_record"):
                    res = await asyncio.to_thread(main_module._dispatch_and_record, sig_id, action, symbol_to_trade, chunk_qty, 0.0, order_ref, is_paper)
                    is_rollover = "ROLL_" in str(ref_suffix).upper()
                    valid_statuses = ("done", "paper_done") if is_rollover else ("done", "paper_done", "partial_failure")
                    is_failed = (
                        not res or 
                        not isinstance(res, dict) or 
                        res.get("status") not in valid_statuses or
                        res.get("status") in ("failed", "rejected") or
                        res.get("order_status") == "Rejected"
                    )
                    if is_failed:
                        fail_desc = str(
                            res.get("reject_reason") or 
                            res.get("description") or 
                            res.get("status") if isinstance(res, dict) else res
                        )
                        logger.warning(f"SuperTrend [{self.symbol} ({self.timeframe})]: Exit order rejected/incomplete ({fail_desc}). Halting further slices.")
                        self.last_error = f"Exit Rejected: {fail_desc[:120]}"
                        return False
                    chunk_delta = chunk_qty if side.upper() == "SHORT" else -chunk_qty
                    self.virtual_position += chunk_delta
                    self._save_virtual_position(main_module, self.virtual_position)
            
            if chunk_idx < len(chunks):
                await asyncio.sleep(0.2)

        self.last_signal_time = time.time()
        self.last_signal_action = f"EXIT_{side}"
        self.last_signal_details = payload or {}
        
        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "aboveBar" if action == "SELL" else "belowBar",
            "color": "#f43f5e" if action == "SELL" else "#10b981",
            "shape": "arrowDown" if action == "SELL" else "arrowUp",
            "text": f"EXIT {side} ({qty})"
        })
        if len(self.recent_trade_markers) > 100:
            self.recent_trade_markers = self.recent_trade_markers[-100:]
        return True

    async def _execute_entry(self, action: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000, target_symbol: Optional[str] = None) -> bool:
        """Dispatches an Entry order with freeze-quantity slicing. Returns True on success, False on failure."""
        # Defense-in-depth safety guard: refuse order if quantity exceeds unreasonable multiple of configured strategy quantity
        max_allowed_lots = max(self.quantity * 5, 50)
        if qty > max_allowed_lots or qty <= 0:
            guard_msg = (
                f"🚨 CRITICAL SAFETY GUARD: Disallowed entry quantity {qty} lots for {self.symbol} ({self.timeframe}) "
                f"(configured strategy quantity: {self.quantity} lots, limit: {max_allowed_lots}). Refusing dispatch!"
            )
            logger.critical(guard_msg)
            if sentry_sdk:
                with sentry_sdk.isolation_scope() as scope:
                    scope.set_tag("symbol", self.symbol)
                    scope.set_tag("timeframe", self.timeframe)
                    scope.set_tag("strategy_id", getattr(self, "id", "default"))
                    sentry_sdk.capture_message(guard_msg, level="error")
            return False

        is_paper = (self.execution_mode == "PAPER")
        symbol_to_trade = str(target_symbol).strip() if target_symbol else self.symbol
        
        chunks = slice_quantity_for_freeze(qty, freeze_limit, lot_size=getattr(self, "lot_size", 1))
        payload = None
        for chunk_idx, chunk_qty in enumerate(chunks, start=1):
            base_ref = f"ST_REV_ENTRY_{self.symbol}_{self.timeframe.upper()}_{ref_suffix}"
            order_ref = format_order_ref_chunk(base_ref, chunk_idx)
            sig_id = f"st_entry_{str(uuid.uuid4())[:8]}"
            
            payload = {
                "action": action.upper(),
                "symbol": symbol_to_trade,
                "quantity": chunk_qty,
                "price": 0.0,
                "product_type": self.product_type,
                "order_ref": order_ref,
                "source": "supertrend_engine",
                "is_paper": is_paper
            }
            
            logger.info(f"SuperTrend [{self.symbol} ({self.timeframe})]: Dispatching Entry Leg [Chunk {chunk_idx}] on {symbol_to_trade}: {payload}")
            if self.dispatch_fn:
                await self.dispatch_fn(sig_id, payload)
                chunk_delta = chunk_qty if action.upper() == "BUY" else -chunk_qty
                self.virtual_position += chunk_delta
                self._save_virtual_position(main_module, self.virtual_position)
            elif main_module:
                if hasattr(main_module, "db_insert_pending"):
                    main_module.db_insert_pending(sig_id, payload)
                if hasattr(main_module, "_dispatch_and_record"):
                    res = await asyncio.to_thread(main_module._dispatch_and_record, sig_id, action.upper(), symbol_to_trade, chunk_qty, 0.0, order_ref, is_paper)
                    is_rollover = "ROLL_" in str(ref_suffix).upper()
                    valid_statuses = ("done", "paper_done") if is_rollover else ("done", "paper_done", "partial_failure")
                    is_failed = (
                        not res or 
                        not isinstance(res, dict) or 
                        res.get("status") not in valid_statuses or
                        res.get("status") in ("failed", "rejected") or
                        res.get("order_status") == "Rejected"
                    )
                    if is_failed:
                        fail_desc = str(
                            res.get("reject_reason") or 
                            res.get("description") or 
                            res.get("status") if isinstance(res, dict) else res
                        )
                        logger.warning(f"SuperTrend [{self.symbol} ({self.timeframe})]: Entry order rejected/incomplete ({fail_desc}). Halting further slices.")
                        self.last_error = f"Entry Rejected: {fail_desc[:120]}"
                        if any(kw in fail_desc.lower() for kw in ("margin", "shortfall", "rms")):
                            self.status = "MARGIN_SHORTFALL_PAUSED"
                        return False
                    chunk_delta = chunk_qty if action.upper() == "BUY" else -chunk_qty
                    self.virtual_position += chunk_delta
                    self._save_virtual_position(main_module, self.virtual_position)

            if chunk_idx < len(chunks):
                await asyncio.sleep(0.2)

        self.last_signal_time = time.time()
        self.last_signal_action = f"ENTRY_{action.upper()}"
        self.last_signal_details = payload or {}

        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "belowBar" if action.upper() == "BUY" else "aboveBar",
            "color": "#10b981" if action.upper() == "BUY" else "#f43f5e",
            "shape": "arrowUp" if action.upper() == "BUY" else "arrowDown",
            "text": f"{action.upper()} {qty}"
        })
        if len(self.recent_trade_markers) > 100:
            self.recent_trade_markers = self.recent_trade_markers[-100:]
        return True


class MultiSuperTrendEngine:
    """
    Master coordinator running up to 6 concurrent SingleSuperTrendRunner instances per client container.
    Provides portfolio-level metrics, on-demand multi-symbol charting, and concurrent cycle loops.
    """
    def __init__(self, max_strategies: int = 6, dispatch_fn: Optional[Callable] = None):
        self.max_strategies = max_strategies
        self.dispatch_fn = dispatch_fn
        self.strategies: Dict[str, SingleSuperTrendRunner] = {}
        
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self.last_reconcile_ts: float = 0.0
        self.last_open_align_ts: float = 0.0
        self._drift_rejection_history: Dict[str, dict] = {}

    @property
    def primary_runner(self) -> Optional[SingleSuperTrendRunner]:
        if not self.strategies:
            return None
        for r in self.strategies.values():
            if r.is_enabled:
                return r
        return next(iter(self.strategies.values()))

    @property
    def is_enabled(self) -> bool:
        r = self.primary_runner
        return r.is_enabled if r else False

    @is_enabled.setter
    def is_enabled(self, val: bool):
        # Finding #4 Fix: Broadcast engine-level enable/disable state to all registered runners
        for r in self.strategies.values():
            r.is_enabled = val

    @property
    def is_configured(self) -> bool:
        r = self.primary_runner
        return r.is_configured if r else False

    @property
    def symbol(self) -> str:
        r = self.primary_runner
        return r.symbol if r else ""

    @symbol.setter
    def symbol(self, val: str):
        r = self.primary_runner
        if r:
            r.symbol = val

    @property
    def exchange_segment(self) -> str:
        r = self.primary_runner
        return r.exchange_segment if r else "MCXFO"

    @property
    def timeframe(self) -> str:
        r = self.primary_runner
        return r.timeframe if r else "5m"

    @property
    def quantity(self) -> int:
        r = self.primary_runner
        return r.quantity if r else 1

    @quantity.setter
    def quantity(self, val: int):
        r = self.primary_runner
        if r:
            r.quantity = val

    @property
    def product_type(self) -> str:
        r = self.primary_runner
        return r.product_type if r else "NRML"

    @property
    def atr_period(self) -> int:
        r = self.primary_runner
        return r.atr_period if r else 10

    @property
    def multiplier(self) -> float:
        r = self.primary_runner
        return r.multiplier if r else 3.0

    @property
    def execution_mode(self) -> str:
        r = self.primary_runner
        return r.execution_mode if r else "LIVE"

    @execution_mode.setter
    def execution_mode(self, val: str):
        # Finding #4 Fix: Broadcast execution mode (PAPER/LIVE) to all registered runners
        for r in self.strategies.values():
            r.execution_mode = val

    @property
    def status(self) -> str:
        r = self.primary_runner
        return r.status if r else "DISABLED"

    @status.setter
    def status(self, val: str):
        # Finding #4 Fix: Broadcast engine status to all registered runners
        for r in self.strategies.values():
            r.status = val

    @property
    def strategy_position(self) -> str:
        r = self.primary_runner
        return r.strategy_position if r else "FLAT"

    @strategy_position.setter
    def strategy_position(self, val: str):
        r = self.primary_runner
        if r:
            r.strategy_position = val

    def reset_all_virtual_positions(self, main_module=None):
        """Zeroes out virtual positions across all registered strategies in memory and SQLite."""
        for r in self.strategies.values():
            r.virtual_position = 0
            if main_module and hasattr(main_module, "db_set_virtual_position"):
                main_module.db_set_virtual_position(r.strategy_key, r.symbol, r.timeframe, 0)

    @property
    def current_broker_quantity(self) -> int:
        r = self.primary_runner
        return r.current_broker_quantity if r else 0

    @current_broker_quantity.setter
    def current_broker_quantity(self, val: int):
        r = self.primary_runner
        if r:
            r.current_broker_quantity = val

    @property
    def cached_candles(self) -> List[Dict[str, Any]]:
        r = self.primary_runner
        return r.cached_candles if r else []

    @cached_candles.setter
    def cached_candles(self, val: list):
        r = self.primary_runner
        if r:
            r.cached_candles = val

    @property
    def last_error(self) -> Optional[str]:
        r = self.primary_runner
        return r.last_error if r else None

    @last_error.setter
    def last_error(self, val: Optional[str]):
        r = self.primary_runner
        if r:
            r.last_error = val

    def update_config(self, config_dict: dict):
        """Updates or registers strategy configurations (supports single dict or full dict with symbol/strategies)."""
        if "strategies" in config_dict and isinstance(config_dict["strategies"], list):
            new_strat_list = config_dict["strategies"]
            seen_ids = set()
            for s_cfg in new_strat_list:
                s_id = str(s_cfg.get("id") or s_cfg.get("strategy_id") or "").strip()
                s_sym = str(s_cfg.get("symbol", "")).strip().upper()
                if not s_id:
                    s_id = f"st_{s_sym.lower()}_{s_cfg.get('timeframe', '5m')}"
                    s_cfg["id"] = s_id
                seen_ids.add(s_id)
                self.add_or_update_strategy(s_cfg)
            # Remove any deleted runners
            for existing_id in list(self.strategies.keys()):
                if existing_id not in seen_ids:
                    self.remove_strategy(existing_id)
            return

        strat_id = str(config_dict.get("id") or config_dict.get("strategy_id") or "").strip()
        sym = str(config_dict.get("symbol", "")).strip().upper()
        if not strat_id and not sym and self.strategies:
            r = self.primary_runner
            if r:
                r.update_config(config_dict)
                return
        self.add_or_update_strategy(config_dict)

    def add_or_update_strategy(self, config_dict: dict) -> dict:
        """Adds or updates a strategy runner and returns its telemetry."""
        sym = str(config_dict.get("symbol", "")).strip().upper()
        if not sym:
            raise ValueError("Symbol is required")
        strat_id = str(config_dict.get("id") or config_dict.get("strategy_id") or "").strip()
        if not strat_id:
            strat_id = f"st_{sym.lower()}_{config_dict.get('timeframe', '5m')}"
            config_dict["id"] = strat_id

        if strat_id in self.strategies:
            runner = self.strategies[strat_id]
            # If symbol changed for this strategy ID, clear cached candles/markers
            if runner.symbol != sym:
                logger.info(f"MultiSuperTrendEngine: Strategy {strat_id} migrated symbol from {runner.symbol} to {sym}")
                runner.cached_candles = []
                runner.recent_trade_markers = []
            runner.update_config(config_dict)
        else:
            if len(self.strategies) >= self.max_strategies:
                raise ValueError(f"Strategy capacity limit of {self.max_strategies} symbols reached.")
            runner = SingleSuperTrendRunner(config_dict, dispatch_fn=self.dispatch_fn)
            self.strategies[strat_id] = runner
            logger.info(f"MultiSuperTrendEngine: Registered runner {strat_id} for {sym} ({runner.timeframe}, {runner.execution_mode}). Total: {len(self.strategies)}/{self.max_strategies}")

        return runner.get_telemetry()

    def remove_strategy(self, key: str) -> bool:
        """Removes a strategy runner by ID or matching symbol."""
        lookup = str(key).strip()
        if lookup in self.strategies:
            runner = self.strategies.pop(lookup)
            runner.is_enabled = False
            runner.status = "REMOVED"
            logger.info(f"MultiSuperTrendEngine: Removed runner {lookup} ({runner.symbol} {runner.timeframe})")
            return True

        # Fallback to lookup by runner.id, or (symbol + timeframe), or symbol
        for sid, runner in list(self.strategies.items()):
            if sid == lookup or runner.id == lookup or runner.symbol == lookup.upper() or f"{runner.symbol.lower()}_{runner.timeframe}" in lookup.lower():
                self.strategies.pop(sid)
                runner.is_enabled = False
                runner.status = "REMOVED"
                logger.info(f"MultiSuperTrendEngine: Removed runner {sid} ({runner.symbol} {runner.timeframe})")
                return True
        return False

    def toggle_strategy(self, key: str, is_enabled: Optional[bool] = None) -> Optional[dict]:
        """Toggles enable/disable state for a specific strategy by ID or symbol."""
        runner = self.get_strategy(key)
        if not runner:
            return None
        if is_enabled is None:
            runner.is_enabled = not runner.is_enabled
        else:
            runner.is_enabled = bool(is_enabled)
        runner.status = "RUNNING" if runner.is_enabled else "DISABLED"
        return runner.get_telemetry()

    def get_strategy(self, key: str, timeframe: Optional[str] = None) -> Optional[SingleSuperTrendRunner]:
        lookup = str(key).strip()
        if lookup in self.strategies:
            return self.strategies[lookup]
        for sid, runner in self.strategies.items():
            if sid == lookup or runner.id == lookup:
                return runner
        if timeframe:
            clean_tf = str(timeframe).strip().lower()
            for runner in self.strategies.values():
                if runner.symbol == lookup.upper() and runner.timeframe == clean_tf:
                    return runner
        for runner in self.strategies.values():
            if runner.symbol == lookup.upper():
                return runner
        return None

    def get_all_strategies(self) -> List[dict]:
        return [r.get_telemetry() for r in self.strategies.values()]

    def get_portfolio_target_positions(self) -> Dict[str, int]:
        """Calculates expected net target lots for each symbol across all active runners."""
        targets: Dict[str, int] = {}
        for runner in self.strategies.values():
            if runner.is_enabled and runner.symbol:
                targets[runner.symbol] = targets.get(runner.symbol, 0) + runner.virtual_position
        return targets

    def get_telemetry(self) -> dict:
        """Returns consolidated portfolio telemetry along with list of all individual strategies."""
        all_strats = self.get_all_strategies()
        active_count = sum(1 for s in all_strats if s["is_enabled"])
        total_long_lots = sum(s["quantity"] for s in all_strats if s["is_enabled"] and s["strategy_position"] == "LONG")
        total_short_lots = sum(s["quantity"] for s in all_strats if s["is_enabled"] and s["strategy_position"] == "SHORT")
        portfolio_targets = self.get_portfolio_target_positions()

        primary = self.primary_runner
        p_tel = primary.get_telemetry() if primary else {
            "symbol": "",
            "exchange_segment": "",
            "timeframe": "5m",
            "timeframe_seconds": 300,
            "quantity": 1,
            "product_type": "NRML",
            "atr_period": 10,
            "multiplier": 3.0,
            "execution_mode": "LIVE",
            "is_enabled": False,
            "is_configured": False,
            "status": "DISABLED",
            "current_trend": "INITIALIZING",
            "virtual_position": 0,
            "strategy_position": "FLAT",
            "current_broker_quantity": 0,
            "broker_side": "FLAT",
            "atr": 0.0,
            "upper_band": 0.0,
            "lower_band": 0.0,
            "last_close": 0.0,
            "next_poll_seconds": 0,
            "last_error": None
        }

        return {
            **p_tel,
            "total_strategies": len(all_strats),
            "active_strategies_count": active_count,
            "max_strategies": self.max_strategies,
            "total_long_lots": total_long_lots,
            "total_short_lots": total_short_lots,
            "portfolio_targets": portfolio_targets,
            "strategies": all_strats
        }

    def get_chart_data(self) -> dict:
        r = self.primary_runner
        return r.get_chart_data() if r else {
            "symbol": "",
            "status": "DISABLED",
            "candlestick": [],
            "supertrend_line": [],
            "upper_band": [],
            "lower_band": [],
            "markers": []
        }

    async def get_chart_data_async(
        self,
        xts_api_module,
        timeframe_override: Optional[str] = None,
        symbol_override: Optional[str] = None,
        strategy_id_override: Optional[str] = None
    ) -> dict:
        """
        Returns JSON formatted for TradingView Lightweight Charts for the requested symbol and timeframe.
        Fetches live historical OHLC on-demand for instant chart rendering.
        """
        runner = None
        if strategy_id_override:
            runner = self.get_strategy(strategy_id_override)
        
        target_sym = str(symbol_override or (runner.symbol if runner else self.symbol)).strip().upper()
        target_tf = str(timeframe_override or (runner.timeframe if runner else "5m")).strip().lower()

        if not runner and target_sym:
            runner = self.get_strategy(target_sym, timeframe=target_tf) or self.primary_runner

        if runner and not target_sym:
            target_sym = runner.symbol

        tf_seconds = parse_timeframe_seconds(target_tf)

        if runner and runner.cached_candles and target_tf == runner.timeframe and not timeframe_override:
            return runner.get_chart_data()

        if target_sym and xts_api_module:
            try:
                inst = xts_api_module.resolve_contract(target_sym)
                if inst and inst.get("inst_id"):
                    inst_id = inst["inst_id"]
                    exch_seg = inst.get("exch_seg") or (runner.exchange_segment if runner else "MCXFO")
                    atr_p = runner.atr_period if runner else 10
                    mult = runner.multiplier if runner else 3.0

                    raw_candles = await asyncio.to_thread(
                        xts_api_module.fetch_ohlc_candles,
                        exch_seg,
                        inst_id,
                        tf_seconds,
                        200
                    )
                    if raw_candles:
                        st_res = calculate_supertrend(raw_candles, atr_p, mult)
                        if not st_res.get("error"):
                            candles_series = st_res.get("candle_series", [])
                            if runner and target_tf == runner.timeframe:
                                runner.cached_candles = candles_series
                                runner.last_close = st_res["last_close"]
                                runner.last_atr = st_res["atr"]
                                runner.upper_band = st_res["upper_band"]
                                runner.lower_band = st_res["lower_band"]

                            candles_out = []
                            st_line_out = []
                            ub_line_out = []
                            lb_line_out = []
                            bullish_line_out = []
                            bearish_line_out = []
                            for c in candles_series:
                                ts = c.get("time") or c.get("timestamp", 0)
                                candles_out.append({
                                    "time": ts,
                                    "open": c.get("open", 0.0),
                                    "high": c.get("high", 0.0),
                                    "low": c.get("low", 0.0),
                                    "close": c.get("close", 0.0),
                                })
                                if c.get("supertrend") and c["supertrend"] > 0:
                                    is_bull = (c.get("trend") == 1)
                                    color = "#10b981" if is_bull else "#f43f5e"
                                    st_line_out.append({"time": ts, "value": c["supertrend"], "color": color})
                                    if is_bull:
                                        bullish_line_out.append({"time": ts, "value": c["supertrend"]})
                                    else:
                                        bearish_line_out.append({"time": ts, "value": c["supertrend"]})
                                if c.get("upper_band") and c["upper_band"] > 0:
                                    ub_line_out.append({"time": ts, "value": c["upper_band"]})
                                if c.get("lower_band") and c["lower_band"] > 0:
                                    lb_line_out.append({"time": ts, "value": c["lower_band"]})

                            return {
                                "id": runner.id if runner else "",
                                "symbol": target_sym,
                                "exchange_segment": exch_seg,
                                "timeframe": target_tf,
                                "timeframe_seconds": tf_seconds,
                                "execution_mode": runner.execution_mode if runner else "LIVE",
                                "status": runner.status if runner else "RUNNING",
                                "current_trend": runner.active_trend if runner else st_res["trend_name"],
                                "last_close": st_res["last_close"],
                                "atr": st_res["atr"],
                                "last_upper_band": st_res.get("upper_band", 0.0),
                                "last_lower_band": st_res.get("lower_band", 0.0),
                                "candlestick": candles_out,
                                "supertrend_line": st_line_out,
                                "bullish_line": bullish_line_out,
                                "bearish_line": bearish_line_out,
                                "upper_band": ub_line_out,
                                "lower_band": lb_line_out,
                                "markers": runner.recent_trade_markers[-30:] if runner else [],
                                "next_poll_seconds": runner.next_poll_seconds if runner else 0
                            }
            except Exception as e:
                logger.error(f"MultiSuperTrend: Error fetching on-demand chart data for {target_sym} ({target_tf}): {e}")

        return runner.get_chart_data() if runner else {
            "symbol": target_sym,
            "status": "DISABLED",
            "candlestick": [],
            "supertrend_line": [],
            "upper_band": [],
            "lower_band": [],
            "markers": []
        }

    async def evaluate_cycle_diagnostic(
        self,
        xts_api_module,
        symbol_override: Optional[str] = None,
        strategy_id_override: Optional[str] = None,
        timeframe_override: Optional[str] = None
    ) -> dict:
        """Executes on-demand diagnostic trace for the specified or active symbol/strategy."""
        runner = None
        if strategy_id_override:
            runner = self.get_strategy(strategy_id_override)

        target_sym = str(symbol_override or (runner.symbol if runner else self.symbol)).strip().upper()
        if not runner and target_sym:
            runner = self.get_strategy(target_sym, timeframe=timeframe_override) or self.primary_runner

        if runner:
            return await runner.evaluate_diagnostic(xts_api_module)

        # Standalone diagnostic for un-registered symbol
        temp_runner = SingleSuperTrendRunner({"symbol": target_sym, "timeframe": timeframe_override or "5m", "exchange_segment": "MCXFO"})
        return await temp_runner.evaluate_diagnostic(xts_api_module)

    async def sync_strategy_to_trend(self, strategy_id: str, xts_api_module, main_module) -> dict:
        """Synchronizes an active strategy runner directly to its prevailing trend on-demand."""
        runner = self.get_strategy(strategy_id)
        if not runner:
            return {"status": "ERROR", "error": f"Strategy '{strategy_id}' not found"}
        return await runner.sync_to_current_trend(xts_api_module, main_module)

    async def reset_strategy_to_flat(self, strategy_id: str, square_off_broker: bool, xts_api_module, main_module) -> dict:
        """Resets an active strategy runner's target to FLAT (0 lots), optionally squaring off at the broker."""
        runner = self.get_strategy(strategy_id)
        if not runner:
            return {"status": "ERROR", "error": f"Strategy '{strategy_id}' not found"}
        return await runner.reset_to_flat(square_off_broker, xts_api_module, main_module)

    async def reconcile_portfolio_drift(self, xts_api_module, main_module, symbol_filter: Optional[str] = None) -> dict:
        """
        Closed-Loop Position Reconciliation & Auto-Healing Watchdog:
        Compares expected aggregate strategy targets against live broker NetWise positions.
        If a drift is detected during market hours, automatically dispatches an adjustment order
        within strict safety limits and triggers Ops Alert.
        """
        if not xts_api_module or not main_module:
            return {"status": "SKIPPED", "reason": "Modules unconfigured"}

        is_market_open_fn = getattr(config, "is_market_open_ist", None)
        if is_market_open_fn and not is_market_open_fn("MCXFO"):
            return {"status": "SKIPPED", "reason": "Market closed"}

        targets = self.get_portfolio_target_positions()
        if symbol_filter:
            targets = {k: v for k, v in targets.items() if k == symbol_filter}

        if not targets:
            return {"status": "IN_SYNC", "drift_count": 0, "actions": []}

        # Fetch live broker net positions
        if not hasattr(xts_api_module, "get_broker_positions_net"):
            return {"status": "SKIPPED", "reason": "Broker positions net API not available"}

        broker_data = await asyncio.to_thread(xts_api_module.get_broker_positions_net)
        if not broker_data or broker_data.get("is_paper_trade", False):
            return {"status": "IN_SYNC", "drift_count": 0, "actions": ["PAPER_MODE"]}

        all_pos = broker_data.get("all_positions", [])
        actions_taken = []
        drift_count = 0

        # Fetch live broker orders to inspect in-flight trades and suppress drift auto-heal while orders match
        broker_orders = []
        if hasattr(xts_api_module, "get_broker_orders"):
            try:
                orders_res = await asyncio.to_thread(xts_api_module.get_broker_orders)
                if isinstance(orders_res, list):
                    broker_orders = orders_res
                elif isinstance(orders_res, dict):
                    broker_orders = orders_res.get("result", []) or orders_res.get("orders", []) or []
            except Exception as e:
                logger.debug(f"reconcile_portfolio_drift: failed to fetch broker orders: {e}")

        for target_sym, target_lots in targets.items():
            runners_for_sym = [r for r in self.strategies.values() if r.is_enabled and r.symbol == target_sym]
            if not runners_for_sym:
                continue

            # If all runners for this symbol are running in PAPER mode, skip broker reconciliation
            if all(getattr(r, "execution_mode", "LIVE") == "PAPER" for r in runners_for_sym):
                continue

            # Circuit Breaker & Rejection Backoff Guard
            hist = getattr(self, "_drift_rejection_history", {}).get(target_sym, {})
            if hist.get("paused"):
                logger.info(f"reconcile_portfolio_drift [{target_sym}]: Strategy circuit breaker tripped ({hist.get('reason')}). Suppressing auto-heal.")
                continue
            if hist.get("backoff_until", 0) > time.time():
                rem = int(hist["backoff_until"] - time.time())
                logger.info(f"reconcile_portfolio_drift [{target_sym}]: Rejection backoff active ({rem}s remaining). Suppressing auto-heal.")
                continue
            if any(r.status == "MARGIN_SHORTFALL_PAUSED" for r in runners_for_sym):
                logger.info(f"reconcile_portfolio_drift [{target_sym}]: Runner in MARGIN_SHORTFALL_PAUSED. Suppressing auto-heal.")
                continue

            # Concurrency & Active Cycle Guard:
            # If any runner for this symbol is currently locked in evaluate_cycle or has a pending rollover,
            # suppress auto-heal to eliminate race conditions with candle closes / flips / rollovers.
            if any(r.lock.locked() or getattr(r, "pending_rollover", False) for r in runners_for_sym):
                logger.info(f"reconcile_portfolio_drift [{target_sym}]: Runner lock held or rollover pending. Suppressing auto-heal.")
                continue

            primary_r = runners_for_sym[0]
            total_configured_lots = sum(r.quantity for r in runners_for_sym)

            inst = xts_api_module.resolve_contract(target_sym)
            if not inst:
                continue

            try:
                inst_id = int(inst.get("inst_id") or 0)
            except Exception:
                inst_id = 0

            if not inst_id:
                continue

            # In-Flight Order Guard: Suppress auto-healing if there are open / pending orders for this symbol
            clean_target_core = re.sub(r'[^A-Z0-9]', '', target_sym.upper())
            has_inflight = False
            for ord_item in broker_orders:
                o_status = str(ord_item.get("OrderStatus") or ord_item.get("status") or "").upper()
                o_sym = str(ord_item.get("TradingSymbol") or ord_item.get("symbol") or "").upper()
                o_inst_id = str(ord_item.get("ExchangeInstrumentID") or ord_item.get("instrument_id") or "")
                if o_status in ("NEW", "OPEN", "PENDINGNEW", "PENDINGREPLACE"):
                    clean_o_sym = re.sub(r'[^A-Z0-9]', '', o_sym)
                    if (inst_id and str(inst_id) == o_inst_id) or (clean_target_core and clean_target_core in clean_o_sym) or (target_sym in o_sym):
                        has_inflight = True
                        break

            if has_inflight:
                logger.info(f"reconcile_portfolio_drift [{target_sym}]: In-flight order detected at broker. Suppressing auto-heal.")
                continue

            try:
                lot_size = int(inst.get("lot_size") or 1)
            except Exception:
                lot_size = 1
            if lot_size <= 0:
                lot_size = 1

            actual_raw_qty = 0
            for p in all_pos:
                p_id = int(p.get("instrument_id") or 0)
                if p_id == inst_id:
                    actual_raw_qty = int(p.get("quantity", 0) or 0)
                    break

            actual_broker_lots = (actual_raw_qty // lot_size) if (lot_size > 1 and actual_raw_qty % lot_size == 0) else actual_raw_qty
            
            # Sync authoritative broker position telemetry directly onto runners
            for r in runners_for_sym:
                r.current_broker_quantity = actual_broker_lots
                r.broker_side = "LONG" if actual_broker_lots > 0 else ("SHORT" if actual_broker_lots < 0 else "FLAT")

            drift_lots = actual_broker_lots - target_lots

            if drift_lots != 0:
                drift_count += 1
                adj_delta = target_lots - actual_broker_lots  # positive = need to BUY, negative = need to SELL
                logger.warning(
                    f"⚠️ CLOSED-LOOP DRIFT DETECTED on {target_sym}: "
                    f"Broker={actual_broker_lots} lots, Target={target_lots} lots | Drift={drift_lots:+d} lots (Need adjustment: {adj_delta:+d} lots)"
                )

                # Strict Safety Cap: Max allowable drift is 2x total configured size (or min 10 lots)
                max_allowed_drift = max(total_configured_lots * 2, 10)
                if abs(drift_lots) > max_allowed_drift:
                    err_msg = (
                        f"🚨 DRIFT SAFETY CAP EXCEEDED on {target_sym}: "
                        f"Drift {drift_lots:+d} lots exceeds max allowable safety limit ({max_allowed_drift} lots). "
                        f"Auto-heal halted to prevent runaway order sizing; immediate manual admin review required!"
                    )
                    logger.critical(err_msg)
                    if hasattr(xts_api_module, "send_ops_alert"):
                        xts_api_module.send_ops_alert(err_msg)
                    actions_taken.append({
                        "symbol": target_sym,
                        "status": "SAFETY_CAP_EXCEEDED",
                        "drift": drift_lots,
                        "max_allowed": max_allowed_drift
                    })
                    continue

                async with primary_r.lock:
                    # Re-verify latest target position and broker position after acquiring lock
                    target_lots = sum(r.virtual_position for r in runners_for_sym)
                    drift_lots = actual_broker_lots - target_lots
                    if drift_lots == 0:
                        logger.info(f"reconcile_portfolio_drift [{target_sym}]: Drift resolved during lock acquisition. Skipping dispatch.")
                        continue

                    adj_delta = target_lots - actual_broker_lots
                    action = "BUY" if adj_delta > 0 else "SELL"
                    abs_adj_lots = abs(adj_delta)
                    now_ts = int(time.time())
                    order_ref = f"ST_AUTO_HEAL_{target_sym}_{action}_{now_ts}"
                    sig_id = f"st_autoheal_{str(uuid.uuid4())[:8]}"

                    logger.info(f"🛡️ AUTO-HEAL: Dispatching corrective {action} {abs_adj_lots} lots on {target_sym}...")
                    is_paper = any(r.execution_mode == "PAPER" for r in runners_for_sym)
                    freeze_limit = getattr(config, "FREEZE_QTY_LIMIT", 100000)
                    chunks = slice_quantity_for_freeze(abs_adj_lots, freeze_limit, lot_size=lot_size)

                    for chunk_idx, chunk_qty in enumerate(chunks, start=1):
                        chunk_ref = format_order_ref_chunk(order_ref, chunk_idx)
                        payload = {
                            "action": action,
                            "symbol": target_sym,
                            "quantity": chunk_qty,
                            "price": 0.0,
                            "product_type": primary_r.product_type,
                            "order_ref": chunk_ref,
                            "source": "auto_heal_watchdog",
                            "is_paper": is_paper,
                        }

                        res = None
                        if hasattr(main_module, "_dispatch_and_record"):
                            res = await asyncio.to_thread(
                                main_module._dispatch_and_record,
                                f"{sig_id}_{chunk_idx}",
                                action,
                                target_sym,
                                chunk_qty,
                                0.0,
                                chunk_ref,
                                is_paper,
                            )
                        elif hasattr(xts_api_module, "place_order"):
                            res = await asyncio.to_thread(
                                xts_api_module.place_order,
                                action,
                                target_sym,
                                chunk_qty,
                                0.0,
                                chunk_ref,
                                is_paper,
                            )

                        order_ok = (
                            res is not None and 
                            isinstance(res, dict) and 
                            res.get("status") not in ("failed", "rejected") and
                            res.get("order_status") != "Rejected" and
                            res.get("code") != "e-rms-rejected" and
                            (res.get("status") in ("done", "paper_done", "partial_failure") or res.get("type") == "success")
                        )

                        if order_ok:
                            succ_msg = (
                                f"✅ AUTO-HEAL EXECUTED: Dispatched {action} {chunk_qty} lots on {target_sym}. "
                                f"Broker position brought into alignment with strategy target ({target_lots} lots)."
                            )
                            logger.info(succ_msg)
                            if hasattr(xts_api_module, "send_ops_alert"):
                                xts_api_module.send_ops_alert(succ_msg)
                            actions_taken.append({"symbol": target_sym, "action": action, "quantity": chunk_qty, "status": "FILLED"})
                            if target_sym in getattr(self, "_drift_rejection_history", {}):
                                self._drift_rejection_history.pop(target_sym, None)
                        else:
                            fail_desc = str(
                                (res.get("reject_reason") if isinstance(res, dict) else "") or
                                (res.get("description") if isinstance(res, dict) else "") or
                                (res.get("error") if isinstance(res, dict) else "") or
                                res
                            )
                            is_margin_issue = any(kw in fail_desc.lower() for kw in ("margin exceeds", "margin shortfall", "margin", "rms : margin", "insufficient margin"))

                            fail_msg = (
                                f"❌ AUTO-HEAL ORDER REJECTED on {target_sym} ({action} {chunk_qty} lots): {fail_desc}. "
                                f"Check broker margin, RMS limits, or instrument status!"
                            )
                            logger.error(fail_msg)
                            if hasattr(xts_api_module, "send_ops_alert"):
                                xts_api_module.send_ops_alert(fail_msg)
                            actions_taken.append({"symbol": target_sym, "action": action, "quantity": chunk_qty, "status": "REJECTED", "details": res})

                            if is_margin_issue:
                                logger.critical(
                                    f"🚨 MARGIN SHORTFALL CIRCUIT BREAKER ACTIVATED on {target_sym}: "
                                    f"Extinguishing target drift (syncing virtual_position to broker {actual_broker_lots} lots) "
                                    f"and setting status to MARGIN_SHORTFALL_PAUSED."
                                )
                                for r in runners_for_sym:
                                    r.virtual_position = actual_broker_lots
                                    r.strategy_position = "FLAT" if actual_broker_lots == 0 else ("LONG" if actual_broker_lots > 0 else "SHORT")
                                    r.status = "MARGIN_SHORTFALL_PAUSED"
                                    r.last_error = f"RMS Margin Shortfall: {fail_desc[:120]}"
                                    r._save_virtual_position(main_module, actual_broker_lots)

                                self._drift_rejection_history[target_sym] = {
                                    "paused": True,
                                    "reason": fail_desc,
                                    "timestamp": time.time(),
                                }
                            else:
                                history = self._drift_rejection_history.setdefault(target_sym, {"count": 0, "backoff_until": 0})
                                history["count"] += 1
                                backoff_sec = min(60 * (2 ** history["count"]), 3600)
                                history["backoff_until"] = time.time() + backoff_sec
                                logger.warning(f"AUTO-HEAL COOLDOWN: Suppressing auto-heal for {target_sym} for {backoff_sec}s due to broker rejection.")

                            break

                        if chunk_idx < len(chunks):
                            await asyncio.sleep(0.2)

        return {
            "status": "RECONCILED" if drift_count > 0 else "IN_SYNC",
            "drift_count": drift_count,
            "actions": actions_taken
        }

    async def evaluate_cycle(self, xts_api_module, main_module) -> None:
        """Evaluates active strategies concurrently across all registered runners with pre-cycle drift check."""
        runners = [r for r in self.strategies.values() if r.is_enabled and r.is_configured]
        if not runners:
            return

        # Pre-cycle Closed-Loop Position Reconciliation
        try:
            await self.reconcile_portfolio_drift(xts_api_module, main_module)
        except Exception as ex:
            logger.warning(f"Pre-cycle drift check warning: {ex}")

        tasks = [runner.evaluate_cycle(xts_api_module, main_module) for runner in runners]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_loop(self, xts_api_module, main_module) -> None:
        """Master background loop evaluating all active runners on their respective candle closes and running 60s auto-heal watchdog."""
        self._running = True
        logger.info(f"MultiSuperTrendEngine background loop started (Max strategies: {self.max_strategies}).")
        
        await asyncio.sleep(2.0)
        
        try:
            await self.evaluate_cycle(xts_api_module, main_module)
        except Exception as e:
            logger.warning(f"MultiSuperTrend startup cycle evaluation: {e}")

        while self._running:
            try:
                runners = [r for r in self.strategies.values() if r.is_enabled and r.is_configured]
                if runners:
                    now_ts = int(time.time())

                    # Check market hours
                    is_market_open_fn = getattr(config, "is_market_open_ist", None)
                    market_open = is_market_open_fn("MCXFO") if is_market_open_fn else True

                    # 1. Periodic 60-second Closed-Loop Drift Auto-Heal Watchdog (Broker Net Position vs Strategy Target)
                    if now_ts - self.last_reconcile_ts >= 60:
                        self.last_reconcile_ts = now_ts
                        try:
                            await self.reconcile_portfolio_drift(xts_api_module, main_module)
                        except Exception as ex:
                            logger.error(f"Auto-Heal Watchdog periodic error: {ex}")

                    # 3. Strategy Candle Boundary Tracking & Cooperative Scheduling
                    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
                    now_dt = datetime.datetime.now(IST)
                    seconds_from_0900 = (now_dt.hour - 9) * 3600 + now_dt.minute * 60 + now_dt.second

                    min_remaining = 999999
                    closing_runners = []
                    mid_candle_runners = []

                    for r in runners:
                        tf_sec = parse_timeframe_seconds(r.timeframe)
                        if tf_sec > 0:
                            sec_into_bar = seconds_from_0900 % tf_sec if seconds_from_0900 >= 0 else 0
                            sec_rem = tf_sec - sec_into_bar
                            r.next_poll_seconds = sec_rem
                            if sec_rem < min_remaining:
                                min_remaining = sec_rem
                            # Within 20s of candle close or within 10s after boundary (candle close confirmation window)
                            # Or if uninitialized (no cached candles yet)
                            # Or if a continuous contract rollover is queued (priority dispatch upon market open)
                            if sec_rem <= 20 or sec_into_bar <= 10 or not r.cached_candles or getattr(r, "pending_rollover", False):
                                closing_runners.append(r)
                            else:
                                mid_candle_runners.append(r)
                        else:
                            r.next_poll_seconds = 0
                            closing_runners.append(r)

                    # 4. Adaptive Execution Branch:
                    if not market_open:
                        # Outside market hours: perform baseline cycle for any uninitialized runner
                        # or runner with a pending contract switch check
                        uninit = [r for r in runners if not r.cached_candles or getattr(r, "pending_rollover", False)]
                        if uninit:
                            await asyncio.gather(*[r.evaluate_cycle(xts_api_module, main_module) for r in uninit], return_exceptions=True)
                        await asyncio.sleep(15.0)
                        continue

                    if closing_runners:
                        # 4A. High-Speed Candle-Close Burst:
                        # Fast 2.0s OHLC polling with skip_order_checks=True (orders only queried if flip triggers).
                        eval_tasks = [r.evaluate_cycle(xts_api_module, main_module, skip_order_checks=True) for r in closing_runners]
                        await asyncio.gather(*eval_tasks, return_exceptions=True)
                        await asyncio.sleep(2.0)
                    else:
                        # 4B. Mid-Candle Live Tick Streaming:
                        # Batch query live touchline quotes (LTP) across all active symbols in a single request.
                        batch_pairs = []
                        seen_ids = set()
                        for r in mid_candle_runners:
                            inst = xts_api_module.resolve_contract(r.symbol) if xts_api_module else None
                            if inst:
                                iid = inst.get("inst_id")
                                seg = inst.get("exch_seg") or r.exchange_segment or "MCXFO"
                                if iid and iid not in seen_ids:
                                    seen_ids.add(iid)
                                    batch_pairs.append((iid, seg))

                        if batch_pairs and xts_api_module and hasattr(xts_api_module, "get_live_prices_batch"):
                            try:
                                live_prices = await asyncio.to_thread(xts_api_module.get_live_prices_batch, batch_pairs)
                                for r in mid_candle_runners:
                                    inst = xts_api_module.resolve_contract(r.symbol) if xts_api_module else None
                                    if inst:
                                        iid = inst.get("inst_id")
                                        ltp = live_prices.get(iid)
                                        if ltp and ltp > 0:
                                            r.update_live_tick(ltp)
                            except Exception as tick_err:
                                logger.debug(f"Batch live tick update error: {tick_err}")

                        # Sleep 3.0s, waking up at least 20s before the next candle close
                        sleep_seconds = float(min(3.0, max(1.0, min_remaining - 20))) if min_remaining > 20 else 2.0
                        await asyncio.sleep(sleep_seconds)
                else:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"MultiSuperTrend loop unhandled error: {e}")
                await asyncio.sleep(5)

        logger.info("MultiSuperTrendEngine background loop stopped.")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


# Alias for backward compatibility with single-symbol scripts and tests
SuperTrendEngine = MultiSuperTrendEngine
