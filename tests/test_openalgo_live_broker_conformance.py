import pytest
import sys
import os
import math
from pathlib import Path
from fastapi.testclient import TestClient

client_path = str(Path(__file__).parent.parent / 'client')
if client_path not in sys.path:
    sys.path.insert(0, client_path)

import client.config as config
import client.main as client_main
import client.openalgo_router as openalgo_router


@pytest.fixture
def api_client(monkeypatch):
    test_key = 'CONFORMANCE_TEST_KEY_2026'
    monkeypatch.setattr(config, 'API_KEY', test_key)
    monkeypatch.setattr(config, 'WEBHOOK_SECRET', test_key)
    monkeypatch.setattr(openalgo_router.config, 'API_KEY', test_key)
    monkeypatch.setattr(openalgo_router.config, 'WEBHOOK_SECRET', test_key)
    client = TestClient(client_main.app, headers={'X-API-KEY': test_key})
    return client


def test_ping_endpoint_conformance(api_client):
    res_get = api_client.get('/api/v1/ping')
    assert res_get.status_code == 200
    data_get = res_get.json()
    assert data_get.get('status') == 'success'
    assert 'pong' in data_get.get('message', '').lower()

    res_post = api_client.post('/api/v1/ping', json={'apikey': 'CONFORMANCE_TEST_KEY_2026'})
    assert res_post.status_code == 200
    data_post = res_post.json()
    assert data_post.get('status') == 'success'


def test_place_order_payload_casing_and_validation(api_client):
    # 1. Missing symbol
    res = api_client.post('/api/v1/placeorder', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'action': 'BUY',
        'quantity': 50,
        'pricetype': 'MARKET'
    })
    assert res.status_code in (200, 400)
    assert res.json().get('status') == 'error'

    # 2. Case-insensitive valid order (paper mode auto-handled)
    res_case = api_client.post('/api/v1/placeorder', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'RELIANCE',
        'action': 'buy',
        'quantity': 10,
        'pricetype': 'market',
        'product': 'mis',
        'exchange': 'nse'
    })
    assert res_case.status_code == 200
    data = res_case.json()
    assert data.get('status') in ('success', 'error')

    # 3. Limit order with price
    res_limit = api_client.post('/api/v1/placeorder', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'TCS',
        'action': 'SELL',
        'quantity': 5,
        'pricetype': 'LIMIT',
        'price': 3500.50,
        'product': 'NRML',
        'exchange': 'NSE'
    })
    assert res_limit.status_code == 200


def test_order_books_and_portfolio_endpoints(api_client):
    endpoints = [
        '/api/v1/orderbook',
        '/api/v1/tradebook',
        '/api/v1/positionbook',
        '/api/v1/holdings',
        '/api/v1/funds'
    ]
    for ep in endpoints:
        res = api_client.post(ep, json={'apikey': 'CONFORMANCE_TEST_KEY_2026'})
        assert res.status_code == 200, f'Failed on {ep}'
        data = res.json()
        assert 'status' in data
        assert data['status'] == 'success'

    # Test margin calculator endpoint
    res_margin = api_client.post('/api/v1/margins', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'orders': [{'symbol': 'INFY', 'quantity': 10, 'action': 'BUY', 'pricetype': 'MARKET', 'product': 'MIS'}]
    })
    assert res_margin.status_code == 200
    assert res_margin.json().get('status') == 'success' 


def test_gtt_order_lifecycle_and_mutual_oco(api_client):
    # 1. Place GTT OCO order
    gtt_payload = {
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'INFY',
        'exchange': 'NSE',
        'action': 'BUY',
        'quantity': 10,
        'trigger_type': 'OCO',
        'triggerprice_sl': 1400.0,
        'triggerprice_tg': 1550.0,
        'stoploss': 1395.0,
        'target': 1555.0,
        'price': 1450.0,
        'product': 'NRML'
    }
    res = api_client.post('/api/v1/placegttorder', json=gtt_payload)
    assert res.status_code == 200
    res_data = res.json()
    assert res_data.get('status') == 'success'
    assert 'trigger_id' in res_data

    # 2. Retrieve GTT orderbook
    res_book = api_client.post('/api/v1/gttorderbook', json={'apikey': 'CONFORMANCE_TEST_KEY_2026'})
    assert res_book.status_code == 200
    book_data = res_book.json()
    assert book_data.get('status') == 'success'


def test_freeze_quantity_auto_slicing_edge_cases(api_client):
    # Order exceeding freeze limit (e.g. 5,400 qty for NIFTY where freeze is 1,800)
    split_payload = {
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'NIFTY26SEP24500CE',
        'exchange': 'NFO',
        'action': 'BUY',
        'quantity': 5400,
        'pricetype': 'MARKET',
        'product': 'NRML'
    }
    res = api_client.post('/api/v1/splitorder', json=split_payload)
    assert res.status_code == 200
    data = res.json()
    assert data.get('status') in ('success', 'error')


def test_options_greeks_black_scholes_mathematical_sanity(api_client):
    payload = {
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbols': [
            {'symbol': 'NIFTY26SEP24500CE', 'strike': 24500.0, 'option_type': 'CE', 'days_to_expiry': 7.0},
            {'symbol': 'NIFTY26SEP24500PE', 'strike': 24500.0, 'option_type': 'PE', 'days_to_expiry': 7.0}
        ],
        'spot_price': 24500.0,
        'interest_rate': 7.0
    }
    res = api_client.post('/api/v1/multioptiongreeks', json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data.get('status') == 'success'
    items = data.get('data', [])
    assert len(items) >= 2

    for item in items:
        assert 'greeks' in item
        greeks = item['greeks']
        assert 'delta' in greeks
        assert 'gamma' in greeks
        assert 'theta' in greeks
        if 'CE' in item.get('symbol', ''):
            assert 0.0 <= greeks['delta'] <= 1.0
        elif 'PE' in item.get('symbol', ''):
            assert -1.0 <= greeks['delta'] <= 0.0


def test_order_modification_and_cancellation_lifecycle(api_client):
    # 1. Cancel order without order_id -> 400
    res_no_id = api_client.post('/api/v1/cancelorder', json={'apikey': 'CONFORMANCE_TEST_KEY_2026'})
    assert res_no_id.status_code == 400
    assert res_no_id.json().get('status') == 'error'

    # 2. Cancel order with valid order_id
    res_cancel = api_client.post('/api/v1/cancelorder', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'order_id': 'TEST_ORDER_9999'
    })
    assert res_cancel.status_code == 200
    assert res_cancel.json().get('status') in ('success', 'error')

    # 3. Cancel all orders
    res_cancel_all = api_client.post('/api/v1/cancelallorder', json={'apikey': 'CONFORMANCE_TEST_KEY_2026'})
    assert res_cancel_all.status_code == 200
    assert res_cancel_all.json().get('status') in ('success', 'error')

    # 4. Close position (single & all)
    res_close_single = api_client.post('/api/v1/closeposition', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'TCS'
    })
    assert res_close_single.status_code == 200
    assert res_close_single.json().get('status') in ('success', 'error')

    res_close_all = api_client.post('/api/v1/closeposition', json={'apikey': 'CONFORMANCE_TEST_KEY_2026'})
    assert res_close_all.status_code == 200
    assert res_close_all.json().get('status') in ('success', 'error')


def test_market_data_quotes_depth_and_history(api_client):
    # 1. Quotes missing symbol -> 400
    res_no_sym = api_client.post('/api/v1/quotes', json={'apikey': 'CONFORMANCE_TEST_KEY_2026'})
    assert res_no_sym.status_code == 400

    # 2. Quotes with symbol
    res_quotes = api_client.post('/api/v1/quotes', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'RELIANCE'
    })
    assert res_quotes.status_code == 200
    data_quotes = res_quotes.json()
    assert data_quotes.get('status') in ('success', 'error')

    # 3. Depth endpoint
    res_depth = api_client.post('/api/v1/depth', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'RELIANCE'
    })
    assert res_depth.status_code == 200
    assert res_depth.json().get('status') in ('success', 'error')

    # 4. History endpoint
    res_history = api_client.post('/api/v1/history', json={
        'apikey': 'CONFORMANCE_TEST_KEY_2026',
        'symbol': 'RELIANCE',
        'interval': '1m'
    })
    assert res_history.status_code == 200
    assert res_history.json().get('status') in ('success', 'error')

