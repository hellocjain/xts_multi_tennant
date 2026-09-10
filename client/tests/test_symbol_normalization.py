import os
import sys
import pytest

client_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if client_dir not in sys.path:
    sys.path.insert(0, client_dir)

from xts_api import get_contract_multiplier, resolve_symbol_smart, ALPHANUM_ONLY


def test_contract_multiplier_gold_variants():
    # Gold Petal: 1 gram = 1.0 multiplier
    assert get_contract_multiplier("GOLDPETAL", "MCXFO") == 1.0
    assert get_contract_multiplier("GOLD PETAL", "MCXFO") == 1.0
    assert get_contract_multiplier("GOLDPETAL1!", "MCXFO") == 1.0
    assert get_contract_multiplier("gold petal", "MCXFO") == 1.0
    assert get_contract_multiplier("GOLDPETAL 30SEP2026", "MCXFO") == 1.0
    assert get_contract_multiplier("GOLD PETAL 30SEP2026", "MCXFO") == 1.0

    # Gold Mini: 100 grams / 10 grams lot = 10.0 multiplier
    assert get_contract_multiplier("GOLDM", "MCXFO") == 10.0
    assert get_contract_multiplier("GOLD MINI", "MCXFO") == 10.0
    assert get_contract_multiplier("GOLDM1!", "MCXFO") == 10.0
    assert get_contract_multiplier("gold mini", "MCXFO") == 10.0

    # Gold Base (100g standard): 100.0 multiplier
    assert get_contract_multiplier("GOLD", "MCXFO") == 100.0
    assert get_contract_multiplier("GOLD1!", "MCXFO") == 100.0
    assert get_contract_multiplier("GOLD 05OCT2026", "MCXFO") == 100.0


def test_contract_multiplier_silver_variants():
    # Silver Micro: 1 kg = 1.0 multiplier
    assert get_contract_multiplier("SILVERMIC", "MCXFO") == 1.0
    assert get_contract_multiplier("SILVER MICRO", "MCXFO") == 1.0
    assert get_contract_multiplier("SILVERMICRO", "MCXFO") == 1.0
    assert get_contract_multiplier("SILVERMIC1!", "MCXFO") == 1.0

    # Silver Mini: 5 kg = 5.0 multiplier
    assert get_contract_multiplier("SILVERM", "MCXFO") == 5.0
    assert get_contract_multiplier("SILVER MINI", "MCXFO") == 5.0
    assert get_contract_multiplier("SILVERMINI", "MCXFO") == 5.0
    assert get_contract_multiplier("SILVERM1!", "MCXFO") == 5.0

    # Silver Base (30 kg): 30.0 multiplier
    assert get_contract_multiplier("SILVER", "MCXFO") == 30.0
    assert get_contract_multiplier("SILVER1!", "MCXFO") == 30.0
    assert get_contract_multiplier("SILVER 05DEC2026", "MCXFO") == 30.0


def test_contract_multiplier_energy_and_base_metals():
    # Crude Oil
    assert get_contract_multiplier("CRUDEOIL", "MCXFO") == 100.0
    assert get_contract_multiplier("CRUDE OIL", "MCXFO") == 100.0
    assert get_contract_multiplier("CRUDEOIL1!", "MCXFO") == 100.0
    assert get_contract_multiplier("CRUDEOILM", "MCXFO") == 10.0
    assert get_contract_multiplier("CRUDE OIL MINI", "MCXFO") == 10.0

    # Natural Gas
    assert get_contract_multiplier("NATURALGAS", "MCXFO") == 1250.0
    assert get_contract_multiplier("NATURAL GAS", "MCXFO") == 1250.0
    assert get_contract_multiplier("NATURALGASM", "MCXFO") == 250.0
    assert get_contract_multiplier("NATURAL GAS MINI", "MCXFO") == 250.0

    # Base Metals Minis
    assert get_contract_multiplier("ZINCMINI", "MCXFO") == 1000.0
    assert get_contract_multiplier("ZINC MINI", "MCXFO") == 1000.0
    assert get_contract_multiplier("LEADMINI", "MCXFO") == 1000.0
    assert get_contract_multiplier("LEAD MINI", "MCXFO") == 1000.0
    assert get_contract_multiplier("ALUMINI", "MCXFO") == 1000.0
    assert get_contract_multiplier("ALUMINIUM MINI", "MCXFO") == 1000.0


def test_resolve_symbol_smart_space_and_alias_normalization():
    assert resolve_symbol_smart("GOLD PETAL") == "GOLDPETAL"
    assert resolve_symbol_smart("GOLD MINI") == "GOLDM"
    assert resolve_symbol_smart("SILVER MICRO") == "SILVERMIC"
    assert resolve_symbol_smart("SILVER MINI") == "SILVERM"
    assert resolve_symbol_smart("CRUDE OIL MINI") == "CRUDEOILM"
    assert resolve_symbol_smart("CRUDE OIL") == "CRUDEOIL"
    assert resolve_symbol_smart("NATURAL GAS") == "NATURALGAS"
    assert resolve_symbol_smart("NATURAL GAS MINI") == "NATURALGASM"


def test_equity_and_nse_defaults():
    # Non-MCXFO segments should default to 1.0 multiplier
    assert get_contract_multiplier("RELIANCE", "NSECM") == 1.0
    assert get_contract_multiplier("NIFTY 50", "NSEFO") == 1.0
    assert get_contract_multiplier("BANKNIFTY", "NSEFO") == 1.0
