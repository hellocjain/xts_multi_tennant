"""
Unit & Integration Tests for OpenAlgo-Compatible Master Contract & Symbol Mapping Subsystem.
Covers:
1. Equity CSV Parsing (NSECM & BSECM)
2. Derivatives Symbology Parsing (FUT, CE, PE across NFO, BFO, MCX, CDS)
3. Index List Normalization (NSE_INDEX & BSE_INDEX)
4. SQLite Persistence & In-Memory Dual-Indexed Cache (token_db)
5. REST Endpoints: GET /api/v1/search & POST /api/v1/symbols
6. Telemetry Reverse Mapping (Orderbook / Positionbook)
"""
import os
import sys
import importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

BASE_DIR = str(Path(__file__).parent.parent)
client_path = os.path.join(BASE_DIR, "client")
portal_path = os.path.join(BASE_DIR, "portal")
for p in (client_path, portal_path):
    if p not in sys.path:
        sys.path.insert(0, p)

import config
import xts_api
import token_db
import master_contract_service

# Explicitly load client/main.py isolated
client_main_file = os.path.join(client_path, "main.py")
spec = importlib.util.spec_from_file_location("client_main_sym_test", client_main_file)
client_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_main)


def test_equity_csv_parsing():
    """Verifies that equity CSV parsing generates canonical OpenAlgo stock and spot index symbols."""
    # NSECM CSV Header & Sample
    nse_rows = [
        ["ExchangeSegment", "ExchangeInstrumentID", "InstrumentType", "Name", "Description", "Series", "NameWithSeries", "InstrumentID", "PriceBand.High", "PriceBand.Low", "FreezeQty", "TickSize", "LotSize", "Multiplier", "DisplayName"],
        ["NSECM", "1594", "1", "INFY", "INFOSYS LTD", "EQ", "INFY-EQ", "1594", "2000.0", "1500.0", "0", "0.05", "1", "1", "INFOSYS"],
        ["NSECM", "2885", "1", "RELIANCE", "RELIANCE IND", "EQ", "RELIANCE-EQ", "2885", "3200.0", "2600.0", "0", "0.05", "1", "1", "RELIANCE"],
        ["NSECM", "9999", "1", "NON_EQ_BOND", "BONDS", "GS", "BOND-GS", "9999", "100.0", "90.0", "0", "0.05", "1", "1", "BONDS"],
    ]
    parsed_nse = master_contract_service.parse_equity_csv_rows(nse_rows, "NSECM")
    assert len(parsed_nse) == 2 # Only EQ series included
    assert parsed_nse[0]["symbol"] == "INFY"
    assert parsed_nse[0]["exchange"] == "NSE"
    assert parsed_nse[0]["token"] == "1594"
    assert parsed_nse[1]["symbol"] == "RELIANCE"

    # BSECM CSV with SPOT index and EQ stock
    bse_rows = [
        ["ExchangeSegment", "ExchangeInstrumentID", "InstrumentType", "Name", "Description", "Series", "NameWithSeries", "InstrumentID", "PriceBand.High", "PriceBand.Low", "FreezeQty", "TickSize", "LotSize", "Multiplier", "DisplayName"],
        ["BSECM", "500209", "1", "INFY", "INFOSYS BSE", "EQ", "INFY-EQ", "500209", "2000.0", "1500.0", "0", "0.05", "1", "1", "INFY"],
        ["BSECM", "1", "1", "SNSX50", "SENSEX 50 SPOT", "SPOT", "SNSX50-SPOT", "1", "0.0", "0.0", "0", "0.05", "1", "1", "SENSEX 50"],
    ]
    parsed_bse = master_contract_service.parse_equity_csv_rows(bse_rows, "BSECM")
    assert len(parsed_bse) == 2
    assert parsed_bse[0]["symbol"] == "INFY"
    assert parsed_bse[0]["exchange"] == "BSE"
    # SPOT series mapped to SENSEX50 on BSE_INDEX
    assert parsed_bse[1]["symbol"] == "SENSEX50"
    assert parsed_bse[1]["exchange"] == "BSE_INDEX"


def test_derivatives_csv_parsing_canonical_symbology():
    """Verifies that derivatives parser generates exact OpenAlgo canonical format: [Name][DDMMMYY][Strike][CE/PE] or [Name][DDMMMYY]FUT."""
    fo_rows = [
        ["ExchangeSegment", "ExchangeInstrumentID", "InstrumentType", "Name", "Description", "Series", "NameWithSeries", "InstrumentID", "PriceBand.High", "PriceBand.Low", "FreezeQty", "TickSize", "LotSize", "Multiplier", "UnderlyingInstrumentId", "UnderlyingIndexName", "ContractExpiration", "StrikePrice", "OptionType", "DisplayName"],
        # 1. NIFTY Futures
        ["NSEFO", "45001", "2", "NIFTY", "NIFTY FUT", "FUTIDX", "NIFTY-FUT", "45001", "25500", "23500", "1800", "0.05", "50", "1", "26000", "NIFTY", "2026-03-26T14:30:00", "0", "1", "NIFTY 26MAR26 FUT"],
        # 2. NIFTY Call Option
        ["NSEFO", "45002", "2", "NIFTY", "NIFTY 24500 CE", "OPTIDX", "NIFTY-CE", "45002", "800", "20", "1800", "0.05", "50", "1", "26000", "NIFTY", "2026-03-26T14:30:00", "24500.0", "3", "NIFTY 26MAR26 24500 CE"],
        # 3. NIFTY Put Option
        ["NSEFO", "45003", "2", "NIFTY", "NIFTY 24000 PE", "OPTIDX", "NIFTY-PE", "45003", "500", "10", "1800", "0.05", "50", "1", "26000", "NIFTY", "2026-03-26T14:30:00", "24000.0", "4", "NIFTY 26MAR26 24000 PE"],
        # 4. VEDL Stock Option with Decimal Strike (292.5)
        ["NSEFO", "45004", "2", "VEDL", "VEDL 292.5 CE", "OPTSTK", "VEDL-CE", "45004", "50", "1", "2000", "0.05", "1000", "1", "3000", "VEDL", "2026-04-25T14:30:00", "292.5", "3", "VEDL 25APR26 292.5 CE"],
        # 5. MCX Crude Oil Futures & Option
        ["MCXFO", "88001", "2", "CRUDEOIL", "CRUDEOIL FUT", "FUTCOM", "CRUDEOIL-FUT", "88001", "7000", "6000", "10000", "1.0", "100", "1", "100", "CRUDEOIL", "2026-03-19T23:30:00", "0", "1", "CRUDEOIL 19MAR26 FUT"],
        ["MCXFO", "88002", "2", "CRUDEOIL", "CRUDEOIL 6500 PE", "OPTCOM", "CRUDEOIL-PE", "88002", "300", "10", "10000", "0.5", "100", "1", "100", "CRUDEOIL", "2026-03-19T23:30:00", "6500.0", "4", "CRUDEOIL 19MAR26 6500 PE"],
    ]

    nfo_parsed = master_contract_service.parse_derivatives_csv_rows(fo_rows[:5], "NSEFO")
    assert len(nfo_parsed) == 4
    assert nfo_parsed[0]["symbol"] == "NIFTY26MAR26FUT"
    assert nfo_parsed[0]["exchange"] == "NFO"
    assert nfo_parsed[0]["instrumenttype"] == "FUT"

    assert nfo_parsed[1]["symbol"] == "NIFTY26MAR2624500CE"
    assert nfo_parsed[1]["exchange"] == "NFO"
    assert nfo_parsed[1]["instrumenttype"] == "CE"

    assert nfo_parsed[2]["symbol"] == "NIFTY26MAR2624000PE"
    assert nfo_parsed[2]["exchange"] == "NFO"
    assert nfo_parsed[2]["instrumenttype"] == "PE"

    assert nfo_parsed[3]["symbol"] == "VEDL25APR26292.5CE"
    assert nfo_parsed[3]["exchange"] == "NFO"

    mcx_parsed = master_contract_service.parse_derivatives_csv_rows([fo_rows[0], fo_rows[5], fo_rows[6]], "MCXFO")
    assert len(mcx_parsed) == 2
    assert mcx_parsed[0]["symbol"] == "CRUDEOIL19MAR26FUT"
    assert mcx_parsed[0]["exchange"] == "MCX"
    assert mcx_parsed[1]["symbol"] == "CRUDEOIL19MAR266500PE"
    assert mcx_parsed[1]["exchange"] == "MCX"


def test_indexlist_parsing():
    """Verifies that XTS index list items are mapped into canonical OpenAlgo index names."""
    nse_raw = ["NIFTY 50_26000", "NIFTY BANK_26001", "INDIA VIX_26002"]
    nse_parsed = master_contract_service.parse_index_list_records(nse_raw, exchange_segment=1)
    assert len(nse_parsed) == 3
    assert nse_parsed[0]["symbol"] == "NIFTY"
    assert nse_parsed[0]["exchange"] == "NSE_INDEX"
    assert nse_parsed[0]["token"] == "26000"

    assert nse_parsed[1]["symbol"] == "BANKNIFTY"
    assert nse_parsed[1]["exchange"] == "NSE_INDEX"
    assert nse_parsed[1]["token"] == "26001"

    bse_raw = ["SENSEX_1"]
    bse_parsed = master_contract_service.parse_index_list_records(bse_raw, exchange_segment=11)
    assert len(bse_parsed) == 1
    assert bse_parsed[0]["symbol"] == "SENSEX"
    assert bse_parsed[0]["exchange"] == "BSE_INDEX"
    assert bse_parsed[0]["token"] == "1"


def test_sqlite_persistence_and_dual_indexing(tmp_path):
    """Tests saving records into SQLite and loading them into TokenDatabase in-memory cache."""
    db_file = str(tmp_path / "test_master_contract.db")

    sample_records = [
        {
            "symbol": "INFY", "brsymbol": "INFOSYS", "name": "INFY", "exchange": "NSE",
            "brexchange": "NSECM", "token": "1594", "expiry": "", "strike": 0.0,
            "lotsize": 1, "instrumenttype": "EQ", "tick_size": 0.05, "freeze_qty": 0
        },
        {
            "symbol": "NIFTY26MAR2624500CE", "brsymbol": "NIFTY 24500 CE", "name": "NIFTY",
            "exchange": "NFO", "brexchange": "NSEFO", "token": "45002", "expiry": "26-MAR-26",
            "strike": 24500.0, "lotsize": 50, "instrumenttype": "CE", "tick_size": 0.05, "freeze_qty": 1800
        },
        {
            "symbol": "CRUDEOIL19MAR26FUT", "brsymbol": "CRUDEOIL FUT", "name": "CRUDEOIL",
            "exchange": "MCX", "brexchange": "MCXFO", "token": "88001", "expiry": "19-MAR-26",
            "strike": 0.0, "lotsize": 100, "instrumenttype": "FUT", "tick_size": 1.0, "freeze_qty": 10000
        },
    ]

    master_contract_service.save_records_to_sqlite(sample_records, db_file)
    assert os.path.exists(db_file)

    # Load into TokenDatabase
    tdb = token_db.TokenDatabase()
    count = tdb.load_from_sqlite(db_file)
    assert count == 3

    # Forward resolution: Symbol -> Token
    assert tdb.get_token("INFY", "NSE") == "1594"
    assert tdb.get_token("NIFTY26MAR2624500CE", "NFO") == "45002"
    assert tdb.get_token("CRUDEOIL19MAR26FUT", "MCX") == "88001"
    assert tdb.get_token("UNKNOWN", "NSE") is None

    # Reverse resolution: Token -> Symbol (via OpenAlgo exchange)
    assert tdb.get_symbol("1594", "NSE") == "INFY"
    assert tdb.get_symbol("45002", "NFO") == "NIFTY26MAR2624500CE"
    # Reverse resolution: Token -> Symbol (via XTS broker exchange)
    assert tdb.get_symbol("45002", "NSEFO") == "NIFTY26MAR2624500CE"
    assert tdb.get_symbol("88001", "MCXFO") == "CRUDEOIL19MAR26FUT"

    # Search symbols
    res = tdb.search_symbols("NIFTY")
    assert len(res) == 1
    assert res[0]["symbol"] == "NIFTY26MAR2624500CE"
    assert res[0]["lotsize"] == 50

    # Search with exchange filter
    res_nse = tdb.search_symbols("INFY", exchange="NSE")
    assert len(res_nse) == 1
    assert res_nse[0]["token"] == "1594"


def test_openalgo_search_and_symbols_rest_endpoints(monkeypatch):
    """Tests /api/v1/search and /api/v1/symbols REST routes on client_main."""
    # Register test symbols into global token DB
    global_tdb = token_db.get_token_db()
    global_tdb.clear()
    global_tdb.register_symbol({
        "symbol": "TCS", "name": "TCS", "exchange": "NSE", "brexchange": "NSECM", "token": "11536", "lotsize": 1, "tick_size": 0.05
    })
    global_tdb.register_symbol({
        "symbol": "BANKNIFTY26MAR26FUT", "name": "BANKNIFTY", "exchange": "NFO", "brexchange": "NSEFO", "token": "55001", "lotsize": 15, "tick_size": 0.05, "freeze_qty": 900
    })

    client = TestClient(client_main.app)

    # 1. GET /api/v1/search
    res = client.get("/api/v1/search?query=BANKNIFTY")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert len(data["data"]) == 1
    assert data["data"][0]["symbol"] == "BANKNIFTY26MAR26FUT"
    assert data["data"][0]["lotsize"] == 15

    # 2. POST /api/v1/search with exchange filter
    res_post = client.post("/api/v1/search", json={"query": "TCS", "exchange": "NSE"})
    assert res_post.status_code == 200
    assert len(res_post.json()["data"]) == 1
    assert res_post.json()["data"][0]["token"] == "11536"

    # 3. POST /api/v1/symbols metadata lookup
    res_meta = client.post("/api/v1/symbols", json={"symbol": "BANKNIFTY26MAR26FUT", "exchange": "NFO"})
    assert res_meta.status_code == 200
    meta = res_meta.json()["data"]
    assert meta["symbol"] == "BANKNIFTY26MAR26FUT"
    assert meta["lotsize"] == 15
    assert meta["freeze_qty"] == 900

    # 4. POST /api/v1/symbols for unknown symbol -> 404
    res_404 = client.post("/api/v1/symbols", json={"symbol": "DOES_NOT_EXIST"})
    assert res_404.status_code == 404


def test_orderbook_and_positionbook_symbol_reverse_mapping(monkeypatch):
    """Tests that broker telemetry with raw tokens is properly mapped to OpenAlgo canonical symbols."""
    import openalgo_router

    monkeypatch.setattr(config, "WEBHOOK_SECRET", "TEST_SECRET_KEY_123")
    monkeypatch.setattr(openalgo_router.config, "WEBHOOK_SECRET", "TEST_SECRET_KEY_123")

    global_tdb = token_db.get_token_db()
    global_tdb.clear()
    global_tdb.register_symbol({
        "symbol": "NIFTY26MAR2624500CE", "name": "NIFTY", "exchange": "NFO", "brexchange": "NSEFO", "token": "45002", "lotsize": 50
    })

    mock_orders = lambda: [
        {"ExchangeInstrumentID": "45002", "ExchangeSegment": "NSEFO", "OrderStatus": "Filled", "TradingSymbol": ""}
    ]
    mock_positions = lambda: {
        "positions": [
            {"ExchangeInstrumentID": "45002", "ExchangeSegment": "NSEFO", "quantity": 50, "TradingSymbol": ""}
        ],
        "net_mtm": 1250.0
    }

    # Mock XTS returns raw ExchangeInstrumentID 45002 with empty or raw TradingSymbol
    monkeypatch.setattr(xts_api, "get_broker_orders", mock_orders)
    monkeypatch.setattr(openalgo_router.xts_api, "get_broker_orders", mock_orders)
    monkeypatch.setattr(xts_api, "get_positions_telemetry", mock_positions)
    monkeypatch.setattr(openalgo_router.xts_api, "get_positions_telemetry", mock_positions)

    client = TestClient(client_main.app)

    # 1. Orderbook reverse mapping
    res_orders = client.post("/api/v1/orderbook", json={"apikey": "TEST_SECRET_KEY_123"})
    assert res_orders.status_code == 200
    orders_data = res_orders.json()["data"]
    assert len(orders_data) == 1
    assert orders_data[0]["symbol"] == "NIFTY26MAR2624500CE"

    # 2. Positionbook reverse mapping
    res_pos = client.post("/api/v1/positionbook", json={"apikey": "TEST_SECRET_KEY_123"})
    assert res_pos.status_code == 200
    pos_data = res_pos.json()["data"]
    assert len(pos_data) == 1
    assert pos_data[0]["symbol"] == "NIFTY26MAR2624500CE"
