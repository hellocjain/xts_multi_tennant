"""
Unit Tests for Universal Symbology Engine & Tender-Period Auto-Rollover
======================================================================
"""

import datetime
from client.symbol_resolver import (
    parse_symbol_multi_format,
    get_contract_multiplier,
    calculate_tender_period_cutoff,
    select_active_contract_with_rollover,
    COMMODITY_MULTIPLIERS,
    COMMON_ALIASES
)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def test_tradingview_continuous_symbology_parsing():
    """Verify TradingView continuous contract parsing."""
    p1 = parse_symbol_multi_format("SILVER1001!")
    assert p1.root == "SILVER100"
    assert p1.sym_type == "CONTINUOUS"
    assert p1.depth == 1

    p2 = parse_symbol_multi_format("GOLDPETAL1!")
    assert p2.root == "GOLDPETAL"
    assert p2.sym_type == "CONTINUOUS"
    assert p2.depth == 1

    p3 = parse_symbol_multi_format("MCX:ZINCMINI2!")
    assert p3.root == "ZINCMINI"
    assert p3.sym_type == "CONTINUOUS"
    assert p3.depth == 2
    assert p3.segment_hint == "MCXFO"

    p4 = parse_symbol_multi_format("NSE:NIFTY1!")
    assert p4.root == "NIFTY"
    assert p4.sym_type == "CONTINUOUS"
    assert p4.depth == 1
    assert p4.segment_hint == "NSEFO"


def test_openalgo_unified_symbology_parsing():
    """Verify OpenAlgo unified futures and options parsing."""
    f1 = parse_symbol_multi_format("SILVER100-FUT")
    assert f1.root == "SILVER100"
    assert f1.sym_type == "FUT"

    f2 = parse_symbol_multi_format("CRUDEOIL-FUT")
    assert f2.root == "CRUDEOIL"
    assert f2.sym_type == "FUT"

    o1 = parse_symbol_multi_format("NIFTY-25000-CE")
    assert o1.root == "NIFTY"
    assert o1.sym_type == "OPT"
    assert o1.strike == 25000.0
    assert o1.option_type == "CE"

    o2 = parse_symbol_multi_format("BANKNIFTY-OPT-52000-PE")
    assert o2.root == "BANKNIFTY"
    assert o2.sym_type == "OPT"
    assert o2.strike == 52000.0
    assert o2.option_type == "PE"


def test_exchange_exact_symbology_parsing():
    """Verify Exchange exact ticker parsing."""
    e1 = parse_symbol_multi_format("SILVER10030SEP2026FUT")
    assert e1.root == "SILVER100"
    assert e1.sym_type == "FUT"
    assert e1.expiry_hint == "30SEP2026"


def test_commodity_multipliers_accuracy():
    """Verify exact contract multipliers for commodities and mini variants."""
    assert get_contract_multiplier("ZINCMINI") == 1000.0
    assert get_contract_multiplier("LEADMINI") == 1000.0
    assert get_contract_multiplier("ALUMINI") == 1000.0
    assert get_contract_multiplier("SILVERM") == 5.0
    assert get_contract_multiplier("SILVERMIC") == 1.0
    assert get_contract_multiplier("SILVER100") == 1.0
    assert get_contract_multiplier("GOLDM") == 10.0
    assert get_contract_multiplier("GOLDPETAL") == 1.0
    assert get_contract_multiplier("CRUDEOILM") == 10.0
    assert get_contract_multiplier("NATGASMINI") == 250.0
    assert get_contract_multiplier("CRUDEOIL") == 100.0
    assert get_contract_multiplier("NATURALGAS") == 1250.0
    assert get_contract_multiplier("COPPER") == 2500.0


def test_mcx_tender_period_auto_rollover():
    """Verify MCX commodities auto-roll 7 calendar days before expiry."""
    today = datetime.date(2026, 9, 10)
    ref_time = datetime.datetime(2026, 9, 10, 10, 0, 0, tzinfo=IST)

    # 1. Expiry in 15 days -> Active, no rollover
    exp_safe = today + datetime.timedelta(days=15)
    roll, days, badge = calculate_tender_period_cutoff(exp_safe, "MCXFO", ref_time)
    assert roll is False
    assert days == 15
    assert "15d" in badge

    # 2. Expiry in 5 days -> In Tender Period, MUST ROLLOVER
    exp_tender = today + datetime.timedelta(days=5)
    roll, days, badge = calculate_tender_period_cutoff(exp_tender, "MCXFO", ref_time)
    assert roll is True
    assert days == 5
    assert "Tender Period Rollover" in badge


def test_nse_index_expiry_rollover_at_1430():
    """Verify NSE Index Futures auto-roll on expiry day at 14:30 IST."""
    today = datetime.date(2026, 9, 24)

    # 1. Expiry day before 14:30 (e.g. 11:00) -> Still trading front month
    morning_time = datetime.datetime(2026, 9, 24, 11, 0, 0, tzinfo=IST)
    roll, days, badge = calculate_tender_period_cutoff(today, "NSEFO", morning_time)
    assert roll is False
    assert days == 0

    # 2. Expiry day after 14:30 (e.g. 14:35) -> Rollover to next contract
    afternoon_time = datetime.datetime(2026, 9, 24, 14, 35, 0, tzinfo=IST)
    roll, days, badge = calculate_tender_period_cutoff(today, "NSEFO", afternoon_time)
    assert roll is True
    assert days == 0
    assert "Post 14:30" in badge


def test_select_active_contract_with_rollover():
    """Verify contract selection rolls to next month during tender period."""
    today = datetime.date(2026, 9, 10)
    ref_time = datetime.datetime(2026, 9, 10, 10, 0, 0, tzinfo=IST)

    # Two contracts: c1 expires in 4 days (tender period), c2 expires in 34 days
    c1 = (today + datetime.timedelta(days=4), 101, "MCXFO", "SILVER100 14SEP26", 0.05, 1, 100)
    c2 = (today + datetime.timedelta(days=34), 102, "MCXFO", "SILVER100 14OCT26", 0.05, 1, 100)

    # When depth=1: Should auto-roll to c2 because c1 is inside tender cutoff (<= 7 days)
    selected = select_active_contract_with_rollover([c1, c2], depth=1, current_time=ref_time)
    assert selected is not None
    assert selected[1] == 102 # c2 selected
