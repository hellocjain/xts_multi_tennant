import ast
import re
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# Allowed standard/math libraries for quant trading strategies
ALLOWED_MODULES = {
    "math", "cmath", "statistics", "random", "decimal", "fractions",
    "datetime", "time", "json", "collections", "itertools", "functools",
    "typing", "numpy", "pandas", "scipy", "talib"
}

# Forbidden built-ins and functions that could compromise host security
BLOCKED_CALLS = {
    "eval", "exec", "compile", "open", "input", "__import__",
    "globals", "locals", "vars", "breakpoint", "help", "exit", "quit"
}

BLOCKED_MODULES = {
    "os", "sys", "subprocess", "shutil", "importlib", "socket", "pty",
    "ctypes", "multiprocessing", "threading", "signal", "tempfile",
    "inspect", "pickle", "shelve", "marshal", "posix", "nt"
}

def validate_strategy_code(code_str: str) -> Dict[str, Any]:
    """
    Parses and validates custom Python strategy code using AST analysis.
    
    Returns:
    {
        "valid": True/False,
        "error": None or "Error message",
        "class_name": "MyStrategy",
        "docstring": "Strategy documentation",
        "warnings": [...]
    }
    """
    if not code_str or not code_str.strip():
        return {"valid": False, "error": "Strategy code cannot be empty.", "warnings": []}

    try:
        tree = ast.parse(code_str, filename="<uploaded_strategy>")
    except SyntaxError as e:
        return {
            "valid": False,
            "error": f"Python Syntax Error (Line {e.lineno}, Col {e.offset}): {e.msg}",
            "warnings": []
        }
    except Exception as e:
        return {"valid": False, "error": f"Failed to parse Python code: {e}", "warnings": []}

    warnings = []
    class_found = False
    class_name = None
    docstring = None
    has_on_candle = False

    for node in ast.walk(tree):
        # 1. Inspect Imports for Security
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg in BLOCKED_MODULES:
                    return {
                        "valid": False,
                        "error": f"Security Violation: Import of forbidden module '{alias.name}' is not allowed in sandbox.",
                        "warnings": warnings
                    }
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split(".")[0]
                if root_pkg in BLOCKED_MODULES:
                    return {
                        "valid": False,
                        "error": f"Security Violation: Import from forbidden module '{node.module}' is not allowed in sandbox.",
                        "warnings": warnings
                    }

        # 2. Inspect Function Calls for Security
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALLS:
                return {
                    "valid": False,
                    "error": f"Security Violation: Call to built-in function '{node.func.id}()' is prohibited.",
                    "warnings": warnings
                }

        # 3. Inspect Strategy Class Structure
        elif isinstance(node, ast.ClassDef):
            # Find the primary strategy class
            if not class_found or "Strategy" in node.name:
                class_found = True
                class_name = node.name
                docstring = ast.get_docstring(node)

                # Check methods inside this class
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "on_candle":
                        # Check argument count: self, candle, history, position
                        args = [a.arg for a in item.args.args]
                        if len(args) < 4:
                            warnings.append(
                                f"Method on_candle in class '{node.name}' should accept 4 arguments: (self, candle, history, position). Found {len(args)} arguments: {args}"
                            )
                        has_on_candle = True

    if not class_found:
        return {
            "valid": False,
            "error": "No Strategy Class definition found. Strategy must define a class (e.g. class CustomStrategy(BaseStrategy): ...)",
            "warnings": warnings
        }

    if not has_on_candle:
        return {
            "valid": False,
            "error": f"Class '{class_name}' is missing required method: on_candle(self, candle, history, position)",
            "warnings": warnings
        }

    return {
        "valid": True,
        "error": None,
        "class_name": class_name,
        "docstring": docstring or "Custom Algorithmic Strategy",
        "warnings": warnings
    }

def generate_boilerplate_code() -> str:
    """Returns a production-ready, heavily commented starter template for XTS strategies."""
    return '''"""
XTS Custom Algorithmic Trading Strategy Template
Standard Event-Driven Python Interface for Symphony XTS Multi-Tenant Engine.

This strategy template demonstrates a dual Exponential Moving Average (EMA) Crossover
with an optional Average True Range (ATR) trend filter.
"""

from typing import Dict, Any, List, Optional
import math


class BaseStrategy:
    """Base strategy class providing standard lifecycle helpers."""
    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.params = params or {}
        # Strategy parameters with defaults
        self.fast_period = int(self.params.get("fast_period", 9))
        self.slow_period = int(self.params.get("slow_period", 21))

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]], position: str) -> str:
        """
        Invoked on each confirmed candle close.
        
        Parameters:
        - candle: Dict containing current closed candle:
          {'open': float, 'high': float, 'low': float, 'close': float, 'volume': int, 'time': int}
        - history: List of past closed candles in chronological order.
        - position: Current position state ('FLAT', 'LONG', 'SHORT').
        
        Returns:
        - 'BUY': Trigger buy entry or reverse short to long.
        - 'SELL': Trigger sell entry or reverse long to short.
        - 'HOLD': Maintain current position.
        """
        # 1. Warmup check: Ensure sufficient historical bars exist
        if len(history) < self.slow_period + 2:
            return "HOLD"

        # 2. Extract chronological closing prices
        closes = [float(c["close"]) for c in history] + [float(candle["close"])]

        # 3. Calculate Fast and Slow EMAs
        fast_ema = self.calculate_ema(closes, self.fast_period)
        slow_ema = self.calculate_ema(closes, self.slow_period)

        # 4. Check Crossover on the last 2 confirmed bars
        # Golden Cross: Fast EMA crosses ABOVE Slow EMA
        is_bullish_cross = (fast_ema[-1] > slow_ema[-1]) and (fast_ema[-2] <= slow_ema[-2])

        # Death Cross: Fast EMA crosses BELOW Slow EMA
        is_bearish_cross = (fast_ema[-1] < slow_ema[-1]) and (fast_ema[-2] >= slow_ema[-2])

        # 5. Signal Generation
        if is_bullish_cross:
            return "BUY"
        elif is_bearish_cross:
            return "SELL"

        return "HOLD"

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Calculates Exponential Moving Average series."""
        if not prices or period <= 0:
            return []
        k = 2.0 / (period + 1)
        ema = [prices[0]]
        for price in prices[1:]:
            val = (price * k) + (ema[-1] * (1.0 - k))
            ema.append(val)
        return ema


class CustomStrategy(BaseStrategy):
    """Your custom strategy implementation."""
    pass
'''
