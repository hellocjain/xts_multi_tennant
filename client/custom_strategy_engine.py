import asyncio
import datetime
import logging
import math
import os
import time
import uuid
from typing import Dict, Any, List, Optional, Callable, Tuple

logger = logging.getLogger(__name__)

def safe_getattr(obj, name, *default):
    s_name = str(name)
    if s_name.startswith("__") or "__" in s_name:
        raise PermissionError(f"Security Violation: Access to dunder attribute '{name}' is prohibited.")
    return getattr(obj, name, *default)

def safe_hasattr(obj, name):
    s_name = str(name)
    if s_name.startswith("__") or "__" in s_name:
        return False
    return hasattr(obj, name)

def safe_setattr(obj, name, value):
    s_name = str(name)
    if s_name.startswith("__") or "__" in s_name:
        raise PermissionError(f"Security Violation: Setting dunder attribute '{name}' is prohibited.")
    return setattr(obj, name, value)

def safe_delattr(obj, name):
    s_name = str(name)
    if s_name.startswith("__") or "__" in s_name:
        raise PermissionError(f"Security Violation: Deleting dunder attribute '{name}' is prohibited.")
    return delattr(obj, name)

# Standard safe namespace for executing strategy scripts
SAFE_BUILTINS = {
    "__build_class__": __build_class__,
    "__name__": "__main__",
    "staticmethod": staticmethod,
    "classmethod": classmethod,
    "property": property,
    "getattr": safe_getattr,
    "hasattr": safe_hasattr,
    "setattr": safe_setattr,
    "delattr": safe_delattr,
    "abs": abs, "all": all, "any": any, "bool": bool, "chr": chr, "ord": ord, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "format": format,
    "frozenset": frozenset, "int": int, "isinstance": isinstance, "issubclass": issubclass,
    "iter": iter, "len": len, "list": list, "map": map, "max": max,
    "min": min, "next": next, "pow": pow, "range": range, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "zip": zip, "print": print,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError
}

class BaseStrategy:
    """Base strategy class providing standard lifecycle helpers."""
    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.params = params or {}

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]], position: str) -> str:
        """
        Invoked on confirmed candle close.
        Returns: 'BUY', 'SELL', or 'HOLD'
        """
        return "HOLD"

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        if not prices or period <= 0:
            return []
        k = 2.0 / (period + 1)
        ema = [prices[0]]
        for price in prices[1:]:
            val = (price * k) + (ema[-1] * (1.0 - k))
            ema.append(val)
        return ema

    @staticmethod
    def calculate_sma(prices: List[float], period: int) -> List[float]:
        if not prices or period <= 0 or len(prices) < period:
            return []
        res = []
        for i in range(period - 1, len(prices)):
            res.append(sum(prices[i - period + 1 : i + 1]) / float(period))
        return res

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> List[float]:
        if len(prices) < period + 1:
            return []
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]
        
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsi_series = []
        
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                rsi_series.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_series.append(100.0 - (100.0 / (1.0 + rs)))
        return rsi_series


def parse_timeframe_to_seconds(tf: str) -> int:
    tf = str(tf).strip().lower()
    if tf.endswith("m"):
        return max(60, int(tf[:-1]) * 60)
    elif tf.endswith("h"):
        return int(tf[:-1]) * 3600
    elif tf.endswith("d"):
        return int(tf[:-1]) * 86400
    elif tf.endswith("s"):
        return int(tf[:-1])
    try:
        val = int(tf)
        return val * 60 if val < 100 else val
    except ValueError:
        return 900 # default 15m


def _strategy_process_worker(code_content: str, strategy_id: str, eval_candle: dict, history: list, strategy_position: str, conn):
    """Worker process target: executes on_candle() in complete OS-level process isolation."""
    try:
        import datetime as dt_mod
        import json as json_mod
        import math as math_mod
        import time as time_mod

        exec_globals = {
            "__builtins__": SAFE_BUILTINS,
            "BaseStrategy": BaseStrategy,
            "math": math_mod,
            "datetime": dt_mod,
            "json": json_mod,
            "time": time_mod,
            "List": List, "Dict": Dict, "Any": Any, "Optional": Optional
        }
        compiled_bytecode = compile(code_content, f"<strategy_{strategy_id}>", "exec")
        exec(compiled_bytecode, exec_globals)

        target_cls = None
        for k, v in exec_globals.items():
            if isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy:
                target_cls = v
                break
        if not target_cls:
            for k, v in exec_globals.items():
                if isinstance(v, type) and hasattr(v, "on_candle") and k != "BaseStrategy":
                    target_cls = v
                    break

        if not target_cls:
            conn.send({"error": "No Strategy Class implementing on_candle() found in code."})
            return

        instance = target_cls()
        sig = instance.on_candle(eval_candle, history, strategy_position)
        conn.send({"result": sig})
    except Exception as e:
        conn.send({"error": str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def run_strategy_in_isolated_process(
    code_content: str, strategy_id: str, eval_candle: dict, history: list, strategy_position: str, timeout: float = 2.0
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Executes user strategy in a dedicated OS subprocess.
    If execution exceeds timeout (e.g. CPU-bound while True: pass infinite loop),
    the process is forcefully terminated via SIGKILL, ensuring zero CPU leakage.
    Returns: (signal_str, error_message, worker_pid)
    """
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()

    proc = ctx.Process(
        target=_strategy_process_worker,
        args=(code_content, strategy_id, eval_candle, history, strategy_position, child_conn)
    )
    proc.start()
    worker_pid = proc.pid

    try:
        await asyncio.to_thread(proc.join, timeout)
        if proc.is_alive():
            # Forcefully kill the runaway subprocess to prevent CPU starvation
            proc.kill()
            await asyncio.to_thread(proc.join, 0.5)
            return None, "Execution Timeout: on_candle() exceeded 2.0s wall-clock limit", worker_pid

        if parent_conn.poll():
            data = parent_conn.recv()
            if "error" in data:
                return None, f"Runtime Exception in on_candle(): {data['error']}", worker_pid
            return str(data.get("result", "")), None, worker_pid
        else:
            return None, "Strategy process terminated unexpectedly without returning a signal", worker_pid
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            proc.kill()
            proc.join()


class SingleCustomStrategyRunner:
    """Manages evaluation and trade execution for a single custom Python strategy."""

    def __init__(self, config_dict: dict, dispatch_fn: Optional[Callable] = None):
        self.id: str = str(config_dict.get("id") or uuid.uuid4().hex[:12])
        self.strategy_id: str = str(config_dict.get("strategy_id") or self.id)
        self.name: str = str(config_dict.get("name") or "Custom Python Strategy")
        self.symbol: str = str(config_dict.get("symbol", "")).strip().upper()
        self.exchange_segment: str = str(config_dict.get("exchange_segment", "MCXFO")).strip().upper()
        self.timeframe: str = str(config_dict.get("timeframe", "15m")).strip().lower()
        self.quantity: int = max(1, int(config_dict.get("quantity", 1)))
        self.product_type: str = str(config_dict.get("product_type", "NRML")).strip().upper()
        self.execution_mode: str = str(config_dict.get("execution_mode", "LIVE")).strip().upper()
        self.is_enabled: bool = bool(config_dict.get("is_enabled", False))
        self.code_content: str = str(config_dict.get("code_content", "")).strip()
        
        self.dispatch_fn = dispatch_fn
        self.strategy_instance: Optional[BaseStrategy] = None
        self.compile_error: Optional[str] = None

        # Telemetry State
        self.status: str = "RUNNING" if self.is_enabled else "PAUSED"
        self.strategy_position: str = "INITIALIZING"
        self.current_broker_quantity: int = 0
        self.last_close: float = 0.0
        self.last_candle_time: int = 0
        self.last_processed_candle_time: int = 0
        self.last_signal_time: float = 0.0
        self.last_signal_action: str = ""
        self.last_error: Optional[str] = None
        self.recent_trade_markers: List[Dict[str, Any]] = []
        self.pending_order_first_seen: Dict[str, float] = {}

        if self.code_content:
            self._compile_strategy()

    def _compile_strategy(self) -> bool:
        """Dynamically compiles and instantiates the user's Strategy class safely."""
        try:
            import datetime as dt_mod
            import json as json_mod
            import math as math_mod
            import time as time_mod

            exec_globals = {
                "__builtins__": SAFE_BUILTINS,
                "BaseStrategy": BaseStrategy,
                "math": math_mod,
                "datetime": dt_mod,
                "json": json_mod,
                "time": time_mod,
                "List": List, "Dict": Dict, "Any": Any, "Optional": Optional
            }

            compiled_bytecode = compile(self.code_content, f"<strategy_{self.id}>", "exec")
            exec(compiled_bytecode, exec_globals)

            # Find strategy class in namespace
            target_cls = None
            for k, v in exec_globals.items():
                if isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy:
                    target_cls = v
                    break

            if not target_cls:
                # Check for any class with on_candle
                for k, v in exec_globals.items():
                    if isinstance(v, type) and hasattr(v, "on_candle") and k != "BaseStrategy":
                        target_cls = v
                        break

            if not target_cls:
                self.compile_error = "No class implementing on_candle found in strategy code."
                self.strategy_instance = None
                return False

            self.strategy_instance = target_cls()
            self.compile_error = None
            logger.info(f"✅ Successfully compiled custom strategy '{self.name}' (Class: {target_cls.__name__})")
            return True
        except Exception as e:
            self.compile_error = f"Compilation Error: {e}"
            self.strategy_instance = None
            logger.error(f"❌ Failed to compile strategy '{self.name}': {e}")
            return False

    def update_config(self, cfg: dict):
        if "symbol" in cfg:
            self.symbol = str(cfg["symbol"]).strip().upper()
        if "exchange_segment" in cfg:
            self.exchange_segment = str(cfg["exchange_segment"]).strip().upper()
        if "timeframe" in cfg:
            self.timeframe = str(cfg["timeframe"]).strip().lower()
        if "quantity" in cfg:
            self.quantity = max(1, int(cfg["quantity"]))
        if "product_type" in cfg:
            self.product_type = str(cfg["product_type"]).strip().upper()
        if "execution_mode" in cfg:
            self.execution_mode = str(cfg["execution_mode"]).strip().upper()
        if "is_enabled" in cfg:
            self.is_enabled = bool(cfg["is_enabled"])
            self.status = "RUNNING" if self.is_enabled else "PAUSED"
        if "code_content" in cfg and cfg["code_content"] != self.code_content:
            self.code_content = str(cfg["code_content"]).strip()
            self._compile_strategy()



    @staticmethod
    def is_candle_closed(candles: List[Dict[str, Any]], tf_seconds: int, now_ts: Optional[int] = None) -> bool:
        """
        Universal structural bar close determination:
        1. In Symphony XTS, all completed/closed bars structurally end in second :59.
        2. Universal Timeframe boundary alignment in Indian Standard Time (IST, UTC+5:30).
        3. Guard against future timestamps and intra-bar partial ticks.
        """
        if not candles or tf_seconds <= 0:
            return False
        
        now = int(time.time()) if now_ts is None else int(now_ts)
        last_ts = int(candles[-1].get("time") or candles[-1].get("timestamp", 0))

        if last_ts % 60 != 59:
            return False

        if now < last_ts:
            return False

        if tf_seconds >= 60:
            ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            dt = datetime.datetime.fromtimestamp(last_ts, tz=ist_tz)

            seconds_from_0900 = (dt.hour - 9) * 3600 + dt.minute * 60 + dt.second + 1
            is_valid_ist_boundary = (seconds_from_0900 % tf_seconds == 0)

            if len(candles) >= 2:
                prev_ts = int(candles[-2].get("time") or candles[-2].get("timestamp", 0))
                if prev_ts > 0:
                    delta = last_ts - prev_ts
                    if delta < 86400 and delta < (tf_seconds - 60):
                        return False

            if is_valid_ist_boundary:
                return True

            if len(candles) >= 2:
                prev_ts = int(candles[-2].get("time") or candles[-2].get("timestamp", 0))
                if abs((last_ts - prev_ts) - tf_seconds) <= 2:
                    return True

            return False

        return True

    async def evaluate_cycle(self, xts_api_module, main_module) -> None:
        if not self.is_enabled or not self.strategy_instance or self.status in ("STOPPED", "ERROR"):
            return

        # 1. Resolve Instrument
        exch_seg = getattr(self, "exchange_segment", "MCXFO") or "MCXFO"
        inst_info = xts_api_module.resolve_contract(self.symbol)
        if not inst_info or not inst_info.get("inst_id"):
            self.last_error = f"Contract resolution failed for {self.symbol}"
            return

        inst_id = inst_info["inst_id"]
        freeze_limit = int(inst_info.get("freeze_qty") or 6000)

        # 2. Check Broker Positions
        positions = xts_api_module.get_positions_telemetry()
        broker_qty = 0
        target_token = f"_{self.symbol}_"
        for p in positions.get("positions", []):
            p_sym = p.get("symbol", "")
            if target_token in p_sym or p.get("instrument_id") == inst_id:
                raw_q = int(p.get("quantity", 0))
                broker_qty += -raw_q if str(p.get("side", "")).upper() == "SHORT" else raw_q

        self.current_broker_quantity = broker_qty

        # 3. Check for In-Flight / Open Orders for this exact strategy token
        try:
            broker_orders = xts_api_module.get_broker_orders()
            expected_ref_token = f"_{self.symbol}_{self.timeframe.upper()}_"
            for order in broker_orders:
                status = str(order.get("OrderStatus", "")).strip().upper()
                order_ref = str(order.get("OrderUniqueIdentifier", "")).strip()
                if status in ("OPEN", "PENDING", "NEW", "TRIGGER_PENDING") and expected_ref_token in order_ref:
                    app_id = order.get("AppOrderID")
                    logger.warning(f"Custom Strategy [{self.name}]: In-flight order {app_id} active. Yielding cycle.")
                    return
        except Exception as e:
            logger.warning(f"Custom Strategy [{self.name}]: Order check warning: {e}")

        # 4. Fetch OHLC Candles
        tf_seconds = parse_timeframe_to_seconds(self.timeframe)
        try:
            candles = await asyncio.to_thread(xts_api_module.fetch_ohlc_candles, exch_seg, inst_id, tf_seconds, 150)
            if not candles or len(candles) < 2:
                self.last_error = "Insufficient candle history returned from broker."
                return
        except Exception as e:
            self.last_error = f"OHLC Fetch error: {e}"
            return

        # 5. Evaluate Strictly on Confirmed Closed Bar
        now_ts = int(time.time())
        is_last_candle_closed = self.is_candle_closed(candles, tf_seconds, now_ts)

        if is_last_candle_closed:
            eval_candle = candles[-1]
            history = candles[:-1]
        else:
            eval_candle = candles[-2]
            history = candles[:-2]

        candle_ts = int(eval_candle.get("time") or eval_candle.get("timestamp", 0))
        self.last_close = float(eval_candle.get("close", 0.0))
        self.last_candle_time = candle_ts
        self.status = "RUNNING"
        self.last_error = None

        if candle_ts == self.last_processed_candle_time:
            return # Already processed this closed candle

        # 6. Execute User Strategy on_candle in Isolated Process with Hard SIGKILL on 2.0s Timeout
        signal_raw, exec_error, _ = await run_strategy_in_isolated_process(
            self.code_content, self.id, eval_candle, history, self.strategy_position, timeout=2.0
        )

        if exec_error:
            self.last_error = exec_error
            if "Timeout" in exec_error:
                logger.error(f"❌ Custom Strategy [{self.name}] execution timed out after 2.0s (subprocess killed).")
            else:
                logger.error(f"❌ Custom Strategy [{self.name}] runtime exception: {exec_error}")
            self.last_processed_candle_time = candle_ts
            return

        signal = str(signal_raw).strip().upper() if signal_raw else "HOLD"

        # 7. Execute Stop and Reverse (SAR) Signals
        if signal in ("BUY", "SELL"):
            logger.info(f"🚨 [CUSTOM STRATEGY SIGNAL] Strategy: {self.name} | Signal: {signal} on {self.symbol} ({self.timeframe}) at {candle_ts}. Position: {self.strategy_position}")
            self.last_signal_time = time.time()
            self.last_signal_action = signal

            if signal == "BUY":
                if self.strategy_position == "SHORT":
                    # Reversal: Exit Short -> Enter Long
                    await self._execute_exit("SHORT", self.quantity, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                    await asyncio.sleep(0.5)
                    await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                    self.strategy_position = "LONG"
                elif self.strategy_position in ("FLAT", "INITIALIZING"):
                    # First trade: Enter Long
                    await self._execute_entry("BUY", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                    self.strategy_position = "LONG"

            elif signal == "SELL":
                if self.strategy_position == "LONG":
                    # Reversal: Exit Long -> Enter Short
                    await self._execute_exit("LONG", self.quantity, f"FLIP_EXIT_{candle_ts}", main_module, freeze_limit)
                    await asyncio.sleep(0.5)
                    await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                    self.strategy_position = "SHORT"
                elif self.strategy_position in ("FLAT", "INITIALIZING"):
                    # First trade: Enter Short
                    await self._execute_entry("SELL", self.quantity, f"FLIP_ENTRY_{candle_ts}", main_module, freeze_limit)
                    self.strategy_position = "SHORT"

        self.last_processed_candle_time = candle_ts

    async def _execute_exit(self, side: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000) -> None:
        action = "BUY" if side == "SHORT" else "SELL"
        order_ref = f"CS_REV_EXIT_{self.symbol}_{self.timeframe.upper()}_{ref_suffix}"
        payload = {
            "action": action,
            "symbol": self.symbol,
            "quantity": qty,
            "price": 0.0,
            "product_type": self.product_type,
            "order_ref": order_ref,
            "source": f"custom_strategy_{self.strategy_id}",
            "is_paper": (self.execution_mode == "PAPER")
        }
        sig_id = f"cs_exit_{uuid.uuid4().hex[:8]}"
        if self.dispatch_fn:
            await self.dispatch_fn(sig_id, payload)
        elif hasattr(main_module, "dispatch_and_record_signal"):
            await main_module.dispatch_and_record_signal(sig_id, payload)

        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "aboveBar" if action == "SELL" else "belowBar",
            "color": "#f43f5e" if action == "SELL" else "#10b981",
            "shape": "arrowDown" if action == "SELL" else "arrowUp",
            "text": f"EXIT {side} ({qty})"
        })

    async def _execute_entry(self, action: str, qty: int, ref_suffix: str, main_module, freeze_limit: int = 100000) -> None:
        order_ref = f"CS_REV_ENTRY_{self.symbol}_{self.timeframe.upper()}_{ref_suffix}"
        payload = {
            "action": action,
            "symbol": self.symbol,
            "quantity": qty,
            "price": 0.0,
            "product_type": self.product_type,
            "order_ref": order_ref,
            "source": f"custom_strategy_{self.strategy_id}",
            "is_paper": (self.execution_mode == "PAPER")
        }
        sig_id = f"cs_entry_{uuid.uuid4().hex[:8]}"
        if self.dispatch_fn:
            await self.dispatch_fn(sig_id, payload)
        elif hasattr(main_module, "dispatch_and_record_signal"):
            await main_module.dispatch_and_record_signal(sig_id, payload)

        self.recent_trade_markers.append({
            "time": self.last_candle_time or int(time.time()),
            "position": "aboveBar" if action == "SELL" else "belowBar",
            "color": "#f43f5e" if action == "SELL" else "#10b981",
            "shape": "arrowDown" if action == "SELL" else "arrowUp",
            "text": f"{action} {qty}"
        })


class MultiCustomStrategyEngine:
    """Orchestrates all assigned custom Python strategies for a client."""

    def __init__(self, dispatch_fn: Optional[Callable] = None):
        self.dispatch_fn = dispatch_fn
        self.strategies: Dict[str, SingleCustomStrategyRunner] = {}
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None

    def add_or_update_strategy(self, cfg: dict) -> SingleCustomStrategyRunner:
        strat_id = str(cfg.get("id") or uuid.uuid4().hex[:12])
        if strat_id in self.strategies:
            self.strategies[strat_id].update_config(cfg)
        else:
            self.strategies[strat_id] = SingleCustomStrategyRunner(cfg, dispatch_fn=self.dispatch_fn)
        return self.strategies[strat_id]

    def remove_strategy(self, strat_id: str):
        self.strategies.pop(strat_id, None)

    def get_strategy(self, strat_id: str) -> Optional[SingleCustomStrategyRunner]:
        return self.strategies.get(strat_id)

    async def evaluate_cycle(self, xts_api_module, main_module):
        for s in list(self.strategies.values()):
            if s.is_enabled:
                try:
                    await s.evaluate_cycle(xts_api_module, main_module)
                except Exception as e:
                    logger.error(f"Error evaluating custom strategy {s.name} ({s.id}): {e}", exc_info=True)

    def get_telemetry(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": s.id,
                "strategy_id": s.strategy_id,
                "name": s.name,
                "symbol": s.symbol,
                "exchange_segment": s.exchange_segment,
                "timeframe": s.timeframe,
                "quantity": s.quantity,
                "product_type": s.product_type,
                "execution_mode": s.execution_mode,
                "is_enabled": s.is_enabled,
                "status": s.status,
                "strategy_position": s.strategy_position,
                "last_close": s.last_close,
                "last_signal_time": s.last_signal_time,
                "last_signal_action": s.last_signal_action,
                "last_error": s.last_error,
                "compile_error": s.compile_error,
                "markers": s.recent_trade_markers[-20:]
            }
            for s in self.strategies.values()
        ]

    @staticmethod
    def evaluate_dry_run(code_str: str, candles: List[Dict[str, Any]], params: Optional[dict] = None) -> Dict[str, Any]:
        """Runs the strategy against historical candles and returns signal points and summary stats."""
        if not candles or len(candles) < 5:
            return {"error": "Insufficient candle data for dry-run simulation."}

        try:
            import datetime as dt_mod
            import json as json_mod
            import math as math_mod
            import time as time_mod

            exec_globals = {
                "__builtins__": SAFE_BUILTINS,
                "BaseStrategy": BaseStrategy,
                "math": math_mod,
                "datetime": dt_mod,
                "json": json_mod,
                "time": time_mod,
                "List": List, "Dict": Dict, "Any": Any, "Optional": Optional
            }

            compiled = compile(code_str, "<dry_run_strategy>", "exec")
            exec(compiled, exec_globals)

            target_cls = None
            for k, v in exec_globals.items():
                if isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy:
                    target_cls = v
                    break

            if not target_cls:
                for k, v in exec_globals.items():
                    if isinstance(v, type) and hasattr(v, "on_candle") and k != "BaseStrategy":
                        target_cls = v
                        break

            if not target_cls:
                return {"error": "No Strategy Class implementing on_candle() found in code."}

            strat_instance = target_cls(params=params)
            signals = []
            cur_pos = "FLAT"

            for i in range(1, len(candles)):
                candle = candles[i]
                history = candles[:i]
                try:
                    sig = str(strat_instance.on_candle(candle, history, cur_pos)).strip().upper()
                except Exception as e:
                    return {"error": f"Runtime Exception on Bar {i} ({candle.get('time')}): {e}"}

                if sig in ("BUY", "SELL"):
                    prev_pos = cur_pos
                    cur_pos = "LONG" if sig == "BUY" else "SHORT"
                    signals.append({
                        "time": candle.get("time") or candle.get("timestamp"),
                        "price": float(candle.get("close", 0.0)),
                        "signal": sig,
                        "prev_position": prev_pos,
                        "new_position": cur_pos,
                        "bar_index": i
                    })

            return {
                "error": None,
                "total_candles": len(candles),
                "signals_count": len(signals),
                "signals": signals
            }
        except Exception as e:
            return {"error": f"Dry-run execution failure: {e}"}
