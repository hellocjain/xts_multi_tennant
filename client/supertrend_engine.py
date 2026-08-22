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


class SingleSuperTrendRunner:
    """
    Autonomous runner for a single symbol contract.
    """
    def __init__(self, config_dict: dict, dispatch_fn: Optional[Callable] = None):
        self.dispatch_fn = dispatch_fn
        self.lock = asyncio.Lock()

        # Strategy Configuration State
        self.id: str = str(config_dict.get("id") or f"st_{uuid.uuid4().hex[:10]}")
        self.symbol: str = str(config_dict.get("symbol", "")).strip().upper()
        self.exchange_segment: str = str(config_dict.get("exchange_segment", "MCXFO")).strip().upper()
        self.timeframe: str = str(config_dict.get("timeframe", "5m")).strip().lower()
        self.quantity: int = max(1, int(config_dict.get("quantity", 1)))
        self.product_type: str = str(config_dict.get("product_type", "NRML")).strip().upper()
        self.atr_period: int = max(2, int(config_dict.get("atr_period", 10)))
        self.multiplier: float = max(0.1, float(config_dict.get("multiplier", 3.0)))
        self.execution_mode: str = str(config_dict.get("execution_mode", "LIVE")).strip().upper()
        if self.execution_mode not in ("LIVE", "PAPER"):
            self.execution_mode = "LIVE"

        self.is_configured: bool = bool(self.symbol and self.exchange_segment and self.quantity > 0)
        self.is_enabled: bool = bool(config_dict.get("is_enabled", False)) and self.is_configured

        # Live Dynamic Telemetry
        self.status: str = "RUNNING" if self.is_enabled else "DISABLED"
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

        # Historical buffer & chart markers
        self.cached_candles: List[Dict[str, Any]] = []
        self.recent_trade_markers: List[Dict[str, Any]] = []

    def update_config(self, config_dict: dict):
        """Updates parameters for this single runner safely."""
        if "id" in config_dict:
            self.id = str(config_dict["id"])
        if "symbol" in config_dict:
            self.symbol = str(config_dict["symbol"]).strip().upper()
        if "exchange_segment" in config_dict:
            self.exchange_segment = str(config_dict["exchange_segment"]).strip().upper()
        if "timeframe" in config_dict:
            self.timeframe = str(config_dict["timeframe"]).strip().lower()
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

        self.is_configured = bool(self.symbol and self.exchange_segment and self.quantity > 0)
        if "is_enabled" in config_dict:
            req_en = bool(config_dict["is_enabled"])
            self.is_enabled = req_en and self.is_configured
            self.status = "RUNNING" if self.is_enabled else "DISABLED"

        logger.info(f"SingleRunner [{self.symbol}] updated: enabled={self.is_enabled}, mode={self.execution_mode}, tf={self.timeframe}, qty={self.quantity}")

    def get_telemetry(self) -> dict:
        """Returns single strategy telemetry payload."""
        return {
            "id": self.id,
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
        """Formats cached series for Lightweight Charts."""
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
                st_line_out.append({"time": ts, "value": c["supertrend"], "color": color})
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
            "upper_band": ub_line_out,
            "lower_band": lb_line_out,
            "markers": self.recent_trade_markers[-30:],
            "next_poll_seconds": self.next_poll_seconds
        }

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

    async def evaluate_cycle(self, xts_api_module, main_module) -> None:
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
            exch_seg = inst.get("exch_seg") or self.exchange_segment or "MCXFO"
            freeze_limit = int(inst.get("freeze_qty") or 100000)
            tf_seconds = parse_timeframe_seconds(self.timeframe)

            # 2. Expiry Protection Guard
            expiry_date = inst.get("expiry")
            if expiry_date:
                days_to_expiry = (expiry_date - datetime.date.today()).days
                min_days = 3 if exch_seg in ("MCXFO", "NCDEX") else 0
                if days_to_expiry <= min_days:
                    logger.warning(f"SuperTrend [{self.symbol}]: Contract expires in {days_to_expiry} days (<= {min_days}). Squaring off & Pausing.")
                    if self.strategy_position != "FLAT":
                        await self._execute_exit(
                            self.strategy_position,
                            self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity,
                            f"EXPIRY_SQOFF_{int(time.time())}",
                            main_module,
                            freeze_limit
                        )
                    self.is_enabled = False
                    self.status = "EXPIRED_PAUSED"
                    return

            # 3. Position Reconciliation
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
                logger.error(f"SuperTrend [{self.symbol}]: Failed to reconcile broker positions: {e}")

            # 4. Pending Order Protection
            try:
                broker_orders = await asyncio.to_thread(xts_api_module.get_broker_orders)
                for o in broker_orders:
                    st = str(o.get("OrderStatus", "")).upper()
                    o_sym = str(o.get("TradingSymbol", "")).upper()
                    if self.symbol in o_sym and st in ("NEW", "OPEN", "PENDINGNEW", "PENDINGREPLACE"):
                        logger.warning(f"SuperTrend [{self.symbol}]: Found pending order {o.get('AppOrderID')} ({st}). Yielding cycle.")
                        return
            except Exception as e:
                logger.warning(f"SuperTrend [{self.symbol}]: Order check warning: {e}")

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
                logger.info(f"🚨 [SUPERTREND FLIP] Symbol: {self.symbol} | Direction: {flip_dir} at candle {candle_ts}. Current Position: {self.strategy_position} | Mode: {self.execution_mode}")
                
                if flip_dir == "BULLISH":
                    if self.strategy_position == "SHORT":
                        exit_qty = self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity
                        await self._execute_exit("SHORT", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                        await asyncio.sleep(0.5)
                        await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                    elif self.strategy_position == "FLAT":
                        await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                
                elif flip_dir == "BEARISH":
                    if self.strategy_position == "LONG":
                        exit_qty = self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity
                        await self._execute_exit("LONG", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                        await asyncio.sleep(0.5)
                        await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                    elif self.strategy_position == "FLAT":
                        await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)

                self.last_processed_candle_time = candle_ts

    async def _execute_exit(self, side: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000) -> None:
        """Dispatches an Exit order with freeze-quantity slicing."""
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
                "price": 0.0,
                "product_type": self.product_type,
                "order_ref": order_ref,
                "source": "supertrend_engine",
                "is_paper": is_paper
            }
            
            logger.info(f"SuperTrend [{self.symbol}]: Dispatching Exit Leg [Chunk {chunk_idx}]: {payload}")
            if self.dispatch_fn:
                await self.dispatch_fn(sig_id, payload)
            elif main_module:
                if hasattr(main_module, "db_insert_pending"):
                    main_module.db_insert_pending(sig_id, payload)
                if hasattr(main_module, "_dispatch_and_record"):
                    await asyncio.to_thread(main_module._dispatch_and_record, sig_id, action, self.symbol, chunk_qty, 0.0, order_ref, is_paper)
            
            remaining_qty -= chunk_qty
            chunk_idx += 1
            if remaining_qty > 0:
                await asyncio.sleep(0.2)

        self.last_signal_time = time.time()
        self.last_signal_action = f"EXIT_{side}"
        self.last_signal_details = payload
        
        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "aboveBar" if action == "SELL" else "belowBar",
            "color": "#f43f5e" if action == "SELL" else "#10b981",
            "shape": "arrowDown" if action == "SELL" else "arrowUp",
            "text": f"EXIT {side} ({qty})"
        })

    async def _execute_entry(self, action: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000) -> None:
        """Dispatches an Entry order with freeze-quantity slicing."""
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
            
            logger.info(f"SuperTrend [{self.symbol}]: Dispatching Entry Leg [Chunk {chunk_idx}]: {payload}")
            if self.dispatch_fn:
                await self.dispatch_fn(sig_id, payload)
            elif main_module:
                if hasattr(main_module, "db_insert_pending"):
                    main_module.db_insert_pending(sig_id, payload)
                if hasattr(main_module, "_dispatch_and_record"):
                    await asyncio.to_thread(main_module._dispatch_and_record, sig_id, action.upper(), self.symbol, chunk_qty, 0.0, order_ref, is_paper)

            remaining_qty -= chunk_qty
            chunk_idx += 1
            if remaining_qty > 0:
                await asyncio.sleep(0.2)

        self.last_signal_time = time.time()
        self.last_signal_action = f"ENTRY_{action}"
        self.last_signal_details = payload

        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "belowBar" if action.upper() == "BUY" else "aboveBar",
            "color": "#10b981" if action.upper() == "BUY" else "#f43f5e",
            "shape": "arrowUp" if action.upper() == "BUY" else "arrowDown",
            "text": f"{action.upper()} {qty}"
        })


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
        r = self.primary_runner
        if r:
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
        r = self.primary_runner
        if r:
            r.execution_mode = val

    @property
    def status(self) -> str:
        r = self.primary_runner
        return r.status if r else "DISABLED"

    @status.setter
    def status(self, val: str):
        r = self.primary_runner
        if r:
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
        """Updates or registers strategy configurations (supports single dict or full dict with symbol)."""
        sym = str(config_dict.get("symbol", "")).strip().upper()
        if not sym and self.strategies:
            r = self.primary_runner
            if r:
                r.update_config(config_dict)
                return
        if not sym:
            sym = "DEFAULT_SYMBOL"
            config_dict["symbol"] = sym

        if sym in self.strategies:
            self.strategies[sym].update_config(config_dict)
        else:
            if len(self.strategies) >= self.max_strategies:
                raise ValueError(f"Max strategies limit ({self.max_strategies}) reached. Cannot add {sym}.")
            runner = SingleSuperTrendRunner(config_dict, dispatch_fn=self.dispatch_fn)
            self.strategies[sym] = runner
            logger.info(f"MultiSuperTrendEngine: Registered runner for {sym} ({runner.timeframe}, {runner.execution_mode}). Total: {len(self.strategies)}/{self.max_strategies}")

    def add_or_update_strategy(self, config_dict: dict) -> dict:
        """Adds or updates a symbol strategy and returns its telemetry."""
        sym = str(config_dict.get("symbol", "")).strip().upper()
        if not sym:
            raise ValueError("Symbol is required")
        if sym not in self.strategies and len(self.strategies) >= self.max_strategies:
            raise ValueError(f"Strategy capacity limit of {self.max_strategies} symbols reached.")

        if sym in self.strategies:
            self.strategies[sym].update_config(config_dict)
        else:
            self.strategies[sym] = SingleSuperTrendRunner(config_dict, dispatch_fn=self.dispatch_fn)

        return self.strategies[sym].get_telemetry()

    def remove_strategy(self, symbol: str) -> bool:
        """Removes a symbol strategy runner."""
        sym = str(symbol).strip().upper()
        if sym in self.strategies:
            runner = self.strategies.pop(sym)
            runner.is_enabled = False
            runner.status = "REMOVED"
            logger.info(f"MultiSuperTrendEngine: Removed runner for {sym}")
            return True
        return False

    def toggle_strategy(self, symbol: str, is_enabled: Optional[bool] = None) -> Optional[dict]:
        """Toggles enable/disable state for a specific strategy."""
        sym = str(symbol).strip().upper()
        runner = self.strategies.get(sym)
        if not runner:
            return None
        if is_enabled is None:
            runner.is_enabled = not runner.is_enabled
        else:
            runner.is_enabled = bool(is_enabled)
        runner.status = "RUNNING" if runner.is_enabled else "DISABLED"
        return runner.get_telemetry()

    def get_strategy(self, symbol: str) -> Optional[SingleSuperTrendRunner]:
        return self.strategies.get(str(symbol).strip().upper())

    def get_all_strategies(self) -> List[dict]:
        return [r.get_telemetry() for r in self.strategies.values()]

    def get_telemetry(self) -> dict:
        """Returns consolidated portfolio telemetry along with list of all individual strategies."""
        all_strats = self.get_all_strategies()
        active_count = sum(1 for s in all_strats if s["is_enabled"])
        total_long_lots = sum(s["current_broker_quantity"] for s in all_strats if s["strategy_position"] == "LONG")
        total_short_lots = sum(s["current_broker_quantity"] for s in all_strats if s["strategy_position"] == "SHORT")

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
            "strategy_position": "FLAT",
            "current_broker_quantity": 0,
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

    async def get_chart_data_async(self, xts_api_module, timeframe_override: Optional[str] = None, symbol_override: Optional[str] = None) -> dict:
        """
        Returns JSON formatted for TradingView Lightweight Charts for the requested symbol and timeframe.
        Fetches live historical OHLC on-demand for instant chart rendering.
        """
        target_sym = str(symbol_override or self.symbol).strip().upper()
        if not target_sym and self.strategies:
            target_sym = next(iter(self.strategies.keys()))

        runner = self.strategies.get(target_sym)
        target_tf = str(timeframe_override or (runner.timeframe if runner else "5m")).strip().lower()
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
                        150
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
                                runner.active_trend = st_res["trend_name"]

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
                                "id": runner.id if runner else "",
                                "symbol": target_sym,
                                "exchange_segment": exch_seg,
                                "timeframe": target_tf,
                                "timeframe_seconds": tf_seconds,
                                "execution_mode": runner.execution_mode if runner else "LIVE",
                                "status": runner.status if runner else "RUNNING",
                                "current_trend": st_res["trend_name"],
                                "last_close": st_res["last_close"],
                                "atr": st_res["atr"],
                                "candlestick": candles_out,
                                "supertrend_line": st_line_out,
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

    async def evaluate_cycle_diagnostic(self, xts_api_module, symbol_override: Optional[str] = None) -> dict:
        """Executes on-demand diagnostic trace for the specified or active symbol."""
        target_sym = str(symbol_override or self.symbol).strip().upper()
        if not target_sym and self.strategies:
            target_sym = next(iter(self.strategies.keys()))

        runner = self.strategies.get(target_sym)
        if runner:
            return await runner.evaluate_diagnostic(xts_api_module)

        # Standalone diagnostic for un-registered symbol
        temp_runner = SingleSuperTrendRunner({"symbol": target_sym, "timeframe": "5m", "exchange_segment": "MCXFO"})
        return await temp_runner.evaluate_diagnostic(xts_api_module)

    async def evaluate_cycle(self, xts_api_module, main_module) -> None:
        """Evaluates active strategies concurrently across all registered runners."""
        runners = [r for r in self.strategies.values() if r.is_enabled and r.is_configured]
        if not runners:
            return
        tasks = [runner.evaluate_cycle(xts_api_module, main_module) for runner in runners]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_loop(self, xts_api_module, main_module) -> None:
        """Master background loop evaluating all active runners on their respective candle closes."""
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
                    eval_tasks = []
                    for r in runners:
                        tf_sec = parse_timeframe_seconds(r.timeframe)
                        elapsed = now_ts % tf_sec
                        remaining = tf_sec - elapsed
                        r.next_poll_seconds = remaining
                        eval_tasks.append(r.evaluate_cycle(xts_api_module, main_module))

                    if eval_tasks:
                        await asyncio.gather(*eval_tasks, return_exceptions=True)

                    await asyncio.sleep(5)
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
