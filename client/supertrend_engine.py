"""
SuperTrend Auto-Trading Strategy Engine (Pure Python, Per-Tenant In-Process Task)
Part of the XTS Multi-Tenant Algorithmic Trading Platform.

Features:
- Pure Python Wilder's Smoothing ATR & SuperTrend calculation (zero heavy C/Pandas dependencies).
- Aligned candle-close REST polling against Symphony XTS Market Data API.
- Continuous live broker position reconciliation on every cycle.
- Sequential two-leg reversal execution (Leg 1: Square-Off, Leg 2: Enter Opposite).
- Pending order suppression & deterministic order_ref generation.
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

    # 2. Compute Wilder's Smoothing ATR
    atr_list = [0.0] * n
    # Initial ATR is simple average of first atr_period TRs
    initial_atr = sum(tr_list[:atr_period]) / float(atr_period)
    atr_list[atr_period - 1] = initial_atr

    for i in range(atr_period, n):
        prev_atr = atr_list[i - 1]
        current_atr = ((prev_atr * (atr_period - 1)) + tr_list[i]) / float(atr_period)
        atr_list[i] = current_atr

    # 3. Compute Basic Bands, Final Bands, and Trend
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

            # Final Upper Band Ratchet
            if basic_ub < prev_ub or prev_close > prev_ub:
                final_ub[i] = basic_ub
            else:
                final_ub[i] = prev_ub

            # Final Lower Band Ratchet
            if basic_lb > prev_lb or prev_close < prev_lb:
                final_lb[i] = basic_lb
            else:
                final_lb[i] = prev_lb

            # Trend Direction
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

    return {
        "trend": cur_trend,
        "trend_name": "BULLISH" if cur_trend == 1 else ("BEARISH" if cur_trend == -1 else "INITIALIZING"),
        "supertrend": round(supertrend[-1], 2),
        "upper_band": round(final_ub[-1], 2),
        "lower_band": round(final_lb[-1], 2),
        "atr": round(atr_list[-1], 2),
        "last_close": round(candles[-1]["close"], 2),
        "last_candle_time": candles[-1]["time"],
        "prev_trend": prev_trend,
        "is_flip": is_flip,
        "flip_direction": flip_dir,
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
        
        # Live Strategy Telemetry State
        self.status: str = "DISABLED" # DISABLED, IDLE, RUNNING, EXPIRED_PAUSED, ERROR
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

        logger.info(f"SuperTrend config updated: enabled={self.is_enabled}, configured={self.is_configured}, symbol={self.symbol}, tf={self.timeframe}, qty={self.quantity}")

    def get_telemetry(self) -> dict:
        """Returns instantaneous strategy telemetry for API and HTMX views."""
        return {
            "is_enabled": self.is_enabled,
            "is_configured": self.is_configured,
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
            "last_error": self.last_error,
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

        # 2. Expiry Safety Cutoff Check
        if expiry_date:
            today = datetime.date.today()
            days_to_expiry = (expiry_date - today).days
            cutoff_days = 3 if "MCX" in str(exch_seg).upper() else 0
            
            if days_to_expiry <= cutoff_days:
                logger.critical(f"🚨 [EXPIRY CUTOFF] SuperTrend: Instrument {self.symbol} is {days_to_expiry} days from expiry ({expiry_date}). Pausing strategy.")
                
                # Square-off any open position immediately
                if self.strategy_position != "FLAT":
                    await self._execute_exit(self.strategy_position, self.current_broker_quantity, "EXPIRY_SAFETY", main_module)
                
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
        self.last_error = None
        self.status = "RUNNING"

        # 7. Evaluate Flip & Execute Reversal
        candle_ts = st_res["last_candle_time"]
        is_flip = st_res["is_flip"]
        flip_dir = st_res["flip_direction"]

        if is_flip and candle_ts != self.last_processed_candle_time:
            logger.info(f"🚨 [SUPERTREND FLIP] Detected {flip_dir} flip at candle {candle_ts}. Current Position: {self.strategy_position}")
            
            if flip_dir == "BULLISH":
                # If currently SHORT -> Sequential Leg 1: Exit SHORT, Leg 2: Enter LONG
                if self.strategy_position == "SHORT":
                    exit_qty = self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity
                    await self._execute_exit("SHORT", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module)
                    await asyncio.sleep(0.5) # Brief gap between sequential legs
                    await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module)
                elif self.strategy_position == "FLAT":
                    await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module)
            
            elif flip_dir == "BEARISH":
                # If currently LONG -> Sequential Leg 1: Exit LONG, Leg 2: Enter SHORT
                if self.strategy_position == "LONG":
                    exit_qty = self.current_broker_quantity if self.current_broker_quantity > 0 else self.quantity
                    await self._execute_exit("LONG", exit_qty, f"FLIP_EXIT_{candle_ts}", main_module)
                    await asyncio.sleep(0.5)
                    await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module)
                elif self.strategy_position == "FLAT":
                    await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module)

            self.last_processed_candle_time = candle_ts

    async def _execute_exit(self, side: str, qty: int, ref_suffix: str, main_module) -> None:
        """Dispatches an Exit / Square-Off order via the audited signal pipeline."""
        action = "BUY" if side.upper() == "SHORT" else "SELL"
        order_ref = f"ST_REV_EXIT_{self.symbol}_{ref_suffix}"
        sig_id = f"st_exit_{str(uuid.uuid4())[:8]}"
        
        payload = {
            "action": action,
            "symbol": self.symbol,
            "quantity": qty,
            "price": 0.0, # Marketable limit order
            "product_type": self.product_type,
            "order_ref": order_ref,
            "source": "supertrend_engine"
        }
        
        logger.info(f"SuperTrend: Dispatching Exit Leg: {payload}")
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
                    qty,
                    0.0,
                    order_ref
                )

        self.last_signal_time = time.time()
        self.last_signal_action = f"EXIT_{side}"
        self.last_signal_details = payload

    async def _execute_entry(self, action: str, qty: int, ref_suffix: str, main_module) -> None:
        """Dispatches an Entry order via the audited signal pipeline."""
        order_ref = f"ST_REV_ENTRY_{self.symbol}_{ref_suffix}"
        sig_id = f"st_entry_{str(uuid.uuid4())[:8]}"
        
        payload = {
            "action": action.upper(),
            "symbol": self.symbol,
            "quantity": qty,
            "price": 0.0,
            "product_type": self.product_type,
            "order_ref": order_ref,
            "source": "supertrend_engine"
        }
        
        logger.info(f"SuperTrend: Dispatching Entry Leg: {payload}")
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
                    qty,
                    0.0,
                    order_ref
                )

        self.last_signal_time = time.time()
        self.last_signal_action = f"ENTRY_{action}"
        self.last_signal_details = payload

    async def run_loop(self, xts_api_module, main_module) -> None:
        """Background loop aligned to candle close timestamps with 3s grace period."""
        self._running = True
        logger.info("SuperTrend engine background loop started.")
        
        # Wait for startup reconciliation watchdog to complete
        await asyncio.sleep(2.0)
        
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
