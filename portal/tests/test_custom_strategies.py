import pytest
import os
import sys
import tempfile
import time

# Ensure portal is on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import strategy_parser
import database

def test_ast_validator_valid_code():
    valid_code = """
class MyCustomStrategy(BaseStrategy):
    def on_candle(self, candle, history, position):
        if len(history) < 10:
            return "HOLD"
        return "BUY"
"""
    res = strategy_parser.validate_strategy_code(valid_code)
    assert res["valid"] is True
    assert res["error"] is None
    assert res["class_name"] == "MyCustomStrategy"

def test_ast_validator_syntax_error():
    broken_code = """
class BrokenStrategy(BaseStrategy):
    def on_candle(self, candle, history, position)
        return "BUY"
"""
    res = strategy_parser.validate_strategy_code(broken_code)
    assert res["valid"] is False
    assert "Syntax Error" in res["error"]

def test_ast_validator_missing_on_candle():
    missing_method_code = """
class IncompleteStrategy(BaseStrategy):
    def calculate_indicators(self):
        pass
"""
    res = strategy_parser.validate_strategy_code(missing_method_code)
    assert res["valid"] is False
    assert "missing required method: on_candle" in res["error"]

def test_ast_validator_blocked_imports():
    malicious_code = """
import os

class MaliciousStrategy(BaseStrategy):
    def on_candle(self, candle, history, position):
        os.system("ls")
        return "HOLD"
"""
    res = strategy_parser.validate_strategy_code(malicious_code)
    assert res["valid"] is False
    assert "Security Violation: Import of forbidden module 'os'" in res["error"]

def test_ast_validator_blocked_call():
    eval_code = """
class EvalStrategy(BaseStrategy):
    def on_candle(self, candle, history, position):
        eval("1 + 1")
        return "HOLD"
"""
    res = strategy_parser.validate_strategy_code(eval_code)
    assert res["valid"] is False
    assert "Security Violation: Call to built-in function 'eval()'" in res["error"]

def test_boilerplate_generator_is_valid():
    code = strategy_parser.generate_boilerplate_code()
    assert code is not None
    assert "class BaseStrategy" in code
    assert "def on_candle" in code
    res = strategy_parser.validate_strategy_code(code)
    assert res["valid"] is True

def test_custom_strategy_db_crud(tmp_path, monkeypatch):
    test_db_dir = tmp_path / "portal_test_data"
    test_db_dir.mkdir()
    monkeypatch.setenv("PORTAL_DATA_DIR", str(test_db_dir))
    database.init_portal_db()

    # 1. Save Strategy
    strat_id = "test_strat_123"
    database.save_custom_strategy(
        id=strat_id,
        name="Test EMA Cross",
        description="Dual EMA crossover strategy",
        filename="test_ema.py",
        code_content=strategy_parser.generate_boilerplate_code(),
        default_timeframe="15m",
        default_symbol="GOLDPETAL1!"
    )

    strat = database.get_custom_strategy(strat_id)
    assert strat is not None
    assert strat["name"] == "Test EMA Cross"
    assert strat["default_symbol"] == "GOLDPETAL1!"

    # 2. Assign to Tenant
    # Create tenant first
    with database.get_db_connection() as conn:
        with conn:
            conn.execute("INSERT OR IGNORE INTO tenants (id, name, status, created_at, updated_at) VALUES ('t1', 'Tenant One', 'ACTIVE', 1, 1)")

    assign_id = "tcs_123"
    database.save_tenant_custom_strategy(
        id=assign_id,
        tenant_id="t1",
        strategy_id=strat_id,
        symbol="GOLDPETAL1!",
        exchange_segment="MCXFO",
        timeframe="15m",
        quantity=4,
        product_type="NRML",
        execution_mode="LIVE",
        is_enabled=1
    )

    assignments = database.get_tenant_custom_strategies(tenant_id="t1")
    assert len(assignments) == 1
    assert assignments[0]["quantity"] == 4
    assert assignments[0]["is_enabled"] == 1

    # 3. Toggle
    database.toggle_tenant_custom_strategy(assign_id, 0)
    assignments_after = database.get_tenant_custom_strategies(tenant_id="t1")
    assert assignments_after[0]["is_enabled"] == 0

    # 4. Delete Assignment
    database.delete_tenant_custom_strategy(assign_id)
    assert len(database.get_tenant_custom_strategies(tenant_id="t1")) == 0

    # 5. Delete Strategy
    database.delete_custom_strategy(strat_id)
    assert database.get_custom_strategy(strat_id) is None
