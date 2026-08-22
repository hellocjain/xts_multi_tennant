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

import time
import datetime
import logging
import asyncio
import uuid
import math
import re
from typing import List, Dict, Optional, Any, Callable

logger = logging.getLogger("supertrend_engine")

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
    multiplier: float = 3.0
) -> Dict[str, Any]:
    """
    Computes SuperTrend indicator over a chronological list of OHLC candles.
    Matches TradingView Pine Script v4 (KivancOzbilgic formula).
    Candles format: [{"time": int, "open": float, "high": float, "low": float, "close": float, "volume": int}, ...]
    """
    n = len(candles)
    if n < atr_period + 1:
        return {
            "trend": 0,
            "trend_name": "INITIALIZING",
            "supertrend": 0.0,
            "upper_band": 0.0,
            "lower_band": 0.0,
            "atr": 0.0,
            "last_close": candles[-1]["close"] if candles else 0.0,
            "last_candle_time": candles[-1]["time"] if candles else 0,
            "prev_trend": 0,
            "is_flip": False,
            "flip_direction": None,
            "candle_series": [],
            "error": f"Insufficient candles ({n}/{atr_period + 1} required)"
        }

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

    # 2. Compute Wilder's Smoothing ATR (RMA in Pine Script)
    atr_list = [0.0] * n
    initial_atr = sum(tr_list[:atr_period]) / float(atr_period)
    atr_list[atr_period - 1] = initial_atr

    for i in range(atr_period, n):
        prev_atr = atr_list[i - 1]
        current_atr = ((prev_atr * (atr_period - 1)) + tr_list[i]) / float(atr_period)
        atr_list[i] = current_atr

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

            # Trend Direction (Pine Script trend := trend == -1 and close > dn1 ? 1 : trend == 1 and close < up1 ? -1 : trend)
            if close > prev_ub:
                trend[i] = 1
            elif close < prev_lb:
                trend[i] = -1
            else:
                trend[i] = trend[i - 1]

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


class SuperTrendEngine:
    """
    Manages in-process SuperTrend auto-trading execution for a single tenant container.
    """
    def __init__(self, dispatch_fn: Optional[Callable] = None):
        self.dispatch_fn = dispatch_fn
        
        # Configuration State
        self.is_enabled: bool = False
        self.is_configured: bool = False
        self.symbol: str = ""
        self.exchange_segment: str = ""
        self.timeframe: str = "5m"
        self.quantity: int = 1
        self.product_type: str = "NRML"
        self.atr_period: int = 10
        self.multiplier: float = 3.0
        self.execution_mode: str = "LIVE" # LIVE vs PAPER
        
        # Live Strategy Telemetry State
        self.status: str = "DISABLED" # DISABLED, IDLE, RUNNING, EXPIRED_PAUSED, ERROR, PAUSED
        self.active_trend: str = "INITIALIZING"
        self.strategy_position: str = "FLAT" # FLAT, LONG, SHORT
        self.current_broker_quantity: int = 0
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
        
        # Ring Buffer & Trade Markers for Visual Charting
        self.cached_candles: List[Dict[str, Any]] = []
        self.recent_trade_markers: List[Dict[str, Any]] = []
        
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    def update_config(self, config_dict: dict):
        """Updates strategy configuration safely."""
        self.symbol = str(config_dict.get("symbol", "")).strip().upper()
        self.exchange_segment = str(config_dict.get("exchange_segment", "")).strip().upper()
        self.timeframe = str(config_dict.get("timeframe", "5m")).strip().lower()
        self.quantity = max(1, int(config_dict.get("quantity", 1)))
        self.product_type = str(config_dict.get("product_type", "NRML")).strip().upper()
        self.atr_period = max(2, int(config_dict.get("atr_period", 10)))
        self.multiplier = max(0.1, float(config_dict.get("multiplier", 3.0)))
        self.execution_mode = str(config_dict.get("execution_mode", "LIVE")).strip().upper()
        if self.execution_mode not in ("LIVE", "PAPER"):
            self.execution_mode = "LIVE"
        
        is_conf = bool(self.symbol and self.exchange_segment and self.quantity > 0)
        self.is_configured = is_conf
        
        # Only allow enable if explicitly configured
        requested_enabled = bool(config_dict.get("is_enabled", False))
        if requested_enabled and not is_conf:
            logger.warning(f"SuperTrend: Cannot enable unconfigured strategy for symbol='{self.symbol}', segment='{self.exchange_segment}'")
            self.is_enabled = False
            self.status = "DISABLED"
        else:
            self.is_enabled = requested_enabled
            self.status = "RUNNING" if self.is_enabled else "DISABLED"

        logger.info(f"SuperTrend config updated: enabled={self.is_enabled}, configured={self.is_configured}, mode={self.execution_mode}, symbol={self.symbol}, tf={self.timeframe}, qty={self.quantity}")

    def get_telemetry(self) -> dict:
        """Returns instantaneous strategy telemetry for API and HTMX views."""
        return {
            "is_enabled": self.is_enabled,
            "is_configured": self.is_configured,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "symbol": self.symbol,
            "exchange_segment": self.exchange_segment,
            "timeframe": self.timeframe,
            "timeframe_seconds": parse_timeframe_seconds(self.timeframe),
            "quantity": self.quantity,
            "product_type": self.product_type,
            "atr_period": self.atr_period,
            "multiplier": self.multiplier,
            "current_trend": self.active_trend,
            "strategy_position": self.strategy_position,
            "current_broker_quantity": self.current_broker_quantity,
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
            "last_error": self.last_error,
        }

    def get_chart_data(self) -> dict:
        """Returns JSON payload formatted specifically for TradingView Lightweight Charts v4 from cache."""
        candles_out = []
        st_line_out = []
        ub_line_out = []
        lb_line_out = []

        for c in self.cached_candles:
            ts = c["time"]
            candles_out.append({
                "time": ts,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            })
            if c.get("supertrend") and c["supertrend"] > 0:
                color = "#10b981" if c.get("trend") == 1 else "#f43f5e"
                st_line_out.append({
                    "time": ts,
                    "value": c["supertrend"],
                    "color": color
                })
            if c.get("upper_band") and c["upper_band"] > 0:
                ub_line_out.append({"time": ts, "value": c["upper_band"]})
            if c.get("lower_band") and c["lower_band"] > 0:
                lb_line_out.append({"time": ts, "value": c["lower_band"]})

        return {
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
            "upper_band": ub_line_out,
            "lower_band": lb_line_out,
            "markers": self.recent_trade_markers[-30:],
            "next_poll_seconds": self.next_poll_seconds
        }

    async def get_chart_data_async(self, xts_api_module, timeframe_override: Optional[str] = None, symbol_override: Optional[str] = None) -> dict:
        """
        Returns JSON payload formatted for TradingView Lightweight Charts.
        Fetches live historical OHLC candles on-demand so chart renders instantly on page load
        and timeframe switches, even before first background poll cycle or when disabled.
        """
        target_sym = str(symbol_override or self.symbol).strip().upper()
        target_tf = str(timeframe_override or self.timeframe).strip().lower()
        tf_seconds = parse_timeframe_seconds(target_tf)

        # If active timeframe matches and we already have cached candles for this symbol and no override was requested
        if self.cached_candles and target_sym == self.symbol and target_tf == self.timeframe and not symbol_override and not timeframe_override:
            return self.get_chart_data()

        if target_sym and xts_api_module:
            try:
                inst = xts_api_module.resolve_contract(target_sym)
                if inst and inst.get("inst_id"):
                    inst_id = inst["inst_id"]
                    exch_seg = inst.get("exch_seg") or self.exchange_segment or "MCXFO"
                    raw_candles = await asyncio.to_thread(
                        xts_api_module.fetch_ohlc_candles,
                        exch_seg,
                        inst_id,
                        tf_seconds,
                        150
                    )
                    if raw_candles:
                        st_res = calculate_supertrend(raw_candles, self.atr_period, self.multiplier)
                        if not st_res.get("error"):
                            candles_series = st_res.get("candle_series", [])
                            if target_tf == self.timeframe and target_sym == self.symbol:
                                self.cached_candles = candles_series
                                self.last_close = st_res["last_close"]
                                self.last_atr = st_res["atr"]
                                self.upper_band = st_res["upper_band"]
                                self.lower_band = st_res["lower_band"]
                                self.active_trend = st_res["trend_name"]

                            candles_out = []
                            st_line_out = []
                            ub_line_out = []
                            lb_line_out = []
                            for c in candles_series:
                                ts = c["time"]
                                candles_out.append({
                                    "time": ts,
                                    "open": c["open"],
                                    "high": c["high"],
                                    "low": c["low"],
                                    "close": c["close"],
                                })
                                if c.get("supertrend") and c["supertrend"] > 0:
                                    color = "#10b981" if c.get("trend") == 1 else "#f43f5e"
                                    st_line_out.append({"time": ts, "value": c["supertrend"], "color": color})
                                if c.get("upper_band") and c["upper_band"] > 0:
                                    ub_line_out.append({"time": ts, "value": c["upper_band"]})
                                if c.get("lower_band") and c["lower_band"] > 0:
                                    lb_line_out.append({"time": ts, "value": c["lower_band"]})

                            return {
                                "symbol": target_sym,
                                "exchange_segment": exch_seg,
                                "timeframe": target_tf,
                                "timeframe_seconds": tf_seconds,
                                "execution_mode": self.execution_mode,
                                "status": self.status,
                                "current_trend": st_res["trend_name"],
                                "last_close": st_res["last_close"],
                                "atr": st_res["atr"],
                                "candlestick": candles_out,
                                "supertrend_line": st_line_out,
                                "upper_band": ub_line_out,
                                "lower_band": lb_line_out,
                                "markers": self.recent_trade_markers[-30:],
                                "next_poll_seconds": self.next_poll_seconds
                            }
            except Exception as e:
                logger.error(f"Error fetching on-demand chart data for {target_sym} ({target_tf}): {e}")

        return self.get_chart_data()

    async def evaluate_cycle_diagnostic(self, xts_api_module) -> dict:
        """
        Executes on-demand diagnostic evaluation of live OHLC data and returns step-by-step formula trace.
        Does not mutate trade state or place orders.
        """
        sym = self.symbol or "SILVER1001!"
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
            50
        )
        fetch_ms = int((time.time() - t_start) * 1000)

        if not candles:
            return {
                "status": "ERROR",
                "error": "No candle data returned from broker OHLC API",
                "symbol": sym,
                "inst_id": inst_id,
                "exch_seg": exch_seg
            }

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

    async def evaluate_cycle(self, xts_api_module, main_module) -> None:
        """Executes a single SuperTrend evaluation and reversal check."""
        if not self.is_enabled or not self.is_configured:
            return

        if getattr(main_module, "TRADING_PAUSED", False):
            self.status = "PAUSED"
            logger.info("SuperTrend: Trading is PAUSED at container level. Skipping evaluation.")
            return

        tf_seconds = parse_timeframe_seconds(self.timeframe)

        # 1. Resolve Instrument in loaded Master Cache
        inst = xts_api_module.resolve_contract(self.symbol)
        if not inst:
            self.status = "ERROR"
            self.last_error = f"Symbol '{self.symbol}' not found in master cache"
            return

        inst_id = inst.get("inst_id")
        exch_seg = inst.get("exch_seg") or self.exchange_segment
        expiry_date = inst.get("expiry")
        freeze_limit = inst.get("freeze_qty") or 100000

        # 2. Expiry Safety Cutoff Check
        if expiry_date:
            today = datetime.date.today()
            days_to_expiry = (expiry_date - today).days
            cutoff_days = 3 if "MCX" in str(exch_seg).upper() else 0
            
            if days_to_expiry <= cutoff_days:
                logger.critical(f"🚨 [EXPIRY CUTOFF] SuperTrend: Instrument {self.symbol} is {days_to_expiry} days from expiry ({expiry_date}). Pausing strategy.")
                
                # Square-off any open position immediately
                if self.strategy_position != "FLAT":
                    await self._execute_exit(self.strategy_position, self.current_broker_quantity, "EXPIRY_SAFETY", main_module, freeze_limit)
                
                self.is_enabled = False
                self.status = "EXPIRED_PAUSED"
                self.last_error = f"Contract reached expiry cutoff ({expiry_date}). Auto-trading paused."
                return

        # 3. Continuous Live Broker Position Reconciliation (Source of Truth)
        try:
            pos_data = await asyncio.to_thread(xts_api_module.get_positions_telemetry)
            all_positions = pos_data.get("positions", []) + pos_data.get("all_positions", [])
            
            # Find matching position
            target_pos = None
            for p in all_positions:
                sym_name = p.get("symbol", "").upper()
                if self.symbol in sym_name or str(inst_id) == str(p.get("instrument_id", "")):
                    target_pos = p
                    break

            if target_pos:
                side = target_pos.get("side", "").upper()
                qty = int(target_pos.get("quantity", 0))
                if side == "LONG" and qty > 0:
                    self.strategy_position = "LONG"
                    self.current_broker_quantity = qty
                elif side == "SHORT" and qty > 0:
                    self.strategy_position = "SHORT"
                    self.current_broker_quantity = qty
                else:
                    self.strategy_position = "FLAT"
                    self.current_broker_quantity = 0
            else:
                self.strategy_position = "FLAT"
                self.current_broker_quantity = 0
        except Exception as e:
            logger.error(f"SuperTrend: Failed to reconcile broker positions: {e}")

        # 4. Pending Order Protection
        try:
            broker_orders = await asyncio.to_thread(xts_api_module.get_broker_orders)
            for o in broker_orders:
                st = str(o.get("OrderStatus", "")).upper()
                o_sym = str(o.get("TradingSymbol", "")).upper()
                if self.symbol in o_sym and st in ("NEW", "OPEN", "PENDINGNEW", "PENDINGREPLACE"):
                    logger.warning(f"SuperTrend: Found pending order {o.get('AppOrderID')} in state {st}. Yielding cycle.")
                    return
        except Exception as e:
            logger.warning(f"SuperTrend: Order book check warning: {e}")

        # 5. Fetch OHLC Candles from Market Data REST API
        candles = await asyncio.to_thread(
            xts_api_module.fetch_ohlc_candles,
            exch_seg,
            inst_id,
            tf_seconds,
            100
        )

        if not candles:
            self.last_error = "No candle data returned from broker OHLC API"
            return

        # 6. Calculate SuperTrend
        st_res = calculate_supertrend(candles, self.atr_period, self.multiplier)
        if st_res.get("error"):
            self.last_error = st_res["error"]
            return

        self.active_trend = st_res["trend_name"]
        self.last_atr = st_res["atr"]
        self.upper_band = st_res["upper_band"]
        self.lower_band = st_res["lower_band"]
        self.last_close = st_res["last_close"]
        self.last_candle_time = st_res["last_candle_time"]
        self.cached_candles = st_res.get("candle_series", [])
        self.last_error = None
        self.status = "RUNNING"

        # 7. Evaluate Flip & Execute Reversal (ON_CANDLE_CLOSE Rule)
        candle_ts = st_res["last_candle_time"]
        is_flip = st_res["is_flip"]
        flip_dir = st_res["flip_direction"]

        if is_flip and candle_ts != self.last_processed_candle_time:
            logger.info(f"🚨 [SUPERTREND FLIP] Detected {flip_dir} flip at candle {candle_ts}. Current Position: {self.strategy_position} | Mode: {self.execution_mode}")
            
            if flip_dir == "BULLISH":
                # If currently SHORT -> Sequential Leg 1: Exit SHORT, Leg 2: Enter LONG
                if self.strategy_position == "SHORT":
                    exit_qty = self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity
                    await self._execute_exit("SHORT", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                    await asyncio.sleep(0.5) # Brief gap between sequential legs
                    await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                elif self.strategy_position == "FLAT":
                    await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
            
            elif flip_dir == "BEARISH":
                # If currently LONG -> Sequential Leg 1: Exit LONG, Leg 2: Enter SHORT
                if self.strategy_position == "LONG":
                    exit_qty = self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity
                    await self._execute_exit("LONG", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                    await asyncio.sleep(0.5)
                    await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                elif self.strategy_position == "FLAT":
                    await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)

            self.last_processed_candle_time = candle_ts

    async def _execute_exit(self, side: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000) -> None:
        """Dispatches an Exit / Square-Off order with freeze-quantity slicing via the audited signal pipeline."""
        action = "BUY" if side.upper() == "SHORT" else "SELL"
        is_paper = (self.execution_mode == "PAPER")
        
        remaining_qty = qty
        chunk_idx = 1
        while remaining_qty > 0:
            chunk_qty = min(remaining_qty, freeze_limit)
            order_ref = f"ST_REV_EXIT_{self.symbol}_{ref_suffix}" if chunk_idx == 1 else f"ST_REV_EXIT_{self.symbol}_{ref_suffix}_{chunk_idx}"
            sig_id = f"st_exit_{str(uuid.uuid4())[:8]}"
            
            payload = {
                "action": action,
                "symbol": self.symbol,
                "quantity": chunk_qty,
                "price": 0.0, # Marketable limit order
                "product_type": self.product_type,
                "order_ref": order_ref,
                "source": "supertrend_engine",
                "is_paper": is_paper
            }
            
            logger.info(f"SuperTrend: Dispatching Exit Leg [Chunk {chunk_idx}]: {payload}")
            if self.dispatch_fn:
                await self.dispatch_fn(sig_id, payload)
            elif main_module:
                if hasattr(main_module, "db_insert_pending"):
                    main_module.db_insert_pending(sig_id, payload)
                if hasattr(main_module, "_dispatch_and_record"):
                    await asyncio.to_thread(
                        main_module._dispatch_and_record,
                        sig_id,
                        action,
                        self.symbol,
                        chunk_qty,
                        0.0,
                        order_ref,
                        is_paper
                    )
            
            remaining_qty -= chunk_qty
            chunk_idx += 1
            if remaining_qty > 0:
                await asyncio.sleep(0.2)

        self.last_signal_time = time.time()
        self.last_signal_action = f"EXIT_{side}"
        self.last_signal_details = payload
        
        # Append trade marker for visual chart
        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "aboveBar" if action == "SELL" else "belowBar",
            "color": "#f43f5e" if action == "SELL" else "#10b981",
            "shape": "arrowDown" if action == "SELL" else "arrowUp",
            "text": f"EXIT {side} ({qty})"
        })

    async def _execute_entry(self, action: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000) -> None:
        """Dispatches an Entry order with freeze-quantity slicing via the audited signal pipeline."""
        is_paper = (self.execution_mode == "PAPER")
        
        remaining_qty = qty
        chunk_idx = 1
        while remaining_qty > 0:
            chunk_qty = min(remaining_qty, freeze_limit)
            order_ref = f"ST_REV_ENTRY_{self.symbol}_{ref_suffix}" if chunk_idx == 1 else f"ST_REV_ENTRY_{self.symbol}_{ref_suffix}_{chunk_idx}"
            sig_id = f"st_entry_{str(uuid.uuid4())[:8]}"
            
            payload = {
                "action": action.upper(),
                "symbol": self.symbol,
                "quantity": chunk_qty,
                "price": 0.0,
                "product_type": self.product_type,
                "order_ref": order_ref,
                "source": "supertrend_engine",
                "is_paper": is_paper
            }
            
            logger.info(f"SuperTrend: Dispatching Entry Leg [Chunk {chunk_idx}]: {payload}")
            if self.dispatch_fn:
                await self.dispatch_fn(sig_id, payload)
            elif main_module:
                if hasattr(main_module, "db_insert_pending"):
                    main_module.db_insert_pending(sig_id, payload)
                if hasattr(main_module, "_dispatch_and_record"):
                    await asyncio.to_thread(
                        main_module._dispatch_and_record,
                        sig_id,
                        action.upper(),
                        self.symbol,
                        chunk_qty,
                        0.0,
                        order_ref,
                        is_paper
                    )

            remaining_qty -= chunk_qty
            chunk_idx += 1
            if remaining_qty > 0:
                await asyncio.sleep(0.2)

        self.last_signal_time = time.time()
        self.last_signal_action = f"ENTRY_{action}"
        self.last_signal_details = payload

        # Append trade marker for visual chart
        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "belowBar" if action.upper() == "BUY" else "aboveBar",
            "color": "#10b981" if action.upper() == "BUY" else "#f43f5e",
            "shape": "arrowUp" if action.upper() == "BUY" else "arrowDown",
            "text": f"{action.upper()} {qty}"
        })

    async def run_loop(self, xts_api_module, main_module) -> None:
        """Background loop aligned to candle close timestamps with 3s grace period."""
        self._running = True
        logger.info("SuperTrend engine background loop started.")
        
        # Wait for startup reconciliation watchdog to complete
        await asyncio.sleep(2.0)
        
        # Initial cold startup reconciliation & buffer fill
        try:
            if self.is_enabled and self.is_configured:
                await self.evaluate_cycle(xts_api_module, main_module)
        except Exception as e:
            logger.warning(f"SuperTrend startup cycle evaluation: {e}")

        while self._running:
            try:
                if self.is_enabled and self.is_configured:
                    tf_seconds = parse_timeframe_seconds(self.timeframe)
                    now_ts = int(time.time())
                    
                    # Calculate seconds remaining until current candle close
                    elapsed = now_ts % tf_seconds
                    remaining = tf_seconds - elapsed
                    self.next_poll_seconds = remaining
                    
                    # Evaluate on cycle
                    await self.evaluate_cycle(xts_api_module, main_module)
                    
                    # Recompute sleep: sleep until 3 seconds past candle close
                    now_ts = int(time.time())
                    elapsed = now_ts % tf_seconds
                    sleep_duration = (tf_seconds - elapsed) + 3
                    if sleep_duration <= 3:
                        sleep_duration += tf_seconds
                    
                    self.next_poll_seconds = sleep_duration
                    await asyncio.sleep(min(sleep_duration, 10))
                else:
                    self.next_poll_seconds = 0
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SuperTrend loop unhandled error: {e}")
                self.last_error = str(e)
                await asyncio.sleep(5)

        logger.info("SuperTrend engine background loop stopped.")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
