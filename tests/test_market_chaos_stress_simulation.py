import pytest
import sys
import os
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

client_path = str(Path(__file__).parent.parent / 'client')
portal_path = str(Path(__file__).parent.parent / 'portal')
for p in (client_path, portal_path):
    if p not in sys.path:
        sys.path.insert(0, p)

import client.config as config
import client.candle_service as candle_service
import client.order_services as order_services
import client.token_db as token_db
import client.main as client_main
from fastapi.testclient import TestClient


@pytest.fixture
def api_client(monkeypatch):
    test_key = 'CHAOS_TEST_KEY_2026'
    monkeypatch.setattr(config, 'API_KEY', test_key)
    monkeypatch.setattr(config, 'WEBHOOK_SECRET', test_key)
    client = TestClient(client_main.app, headers={'X-API-KEY': test_key})
    return client


def test_5000_ticks_per_second_burst_conflation():
    cs = candle_service.CandleService()
    start_time = time.time()
    total_ticks = 5000
    base_price = 24500.0

    # Stream 5,000 synthetic ticks across 5 instruments
    for i in range(total_ticks):
        sym = f'NIFTY_{i % 5}'
        price = base_price + (i % 20) * 0.5
        vol = 50 + (i % 10)
        ts = int(time.time())
        cs.ingest_tick(sym, 'NFO', price, vol, ts, '1m')

    elapsed = time.time() - start_time
    assert elapsed < 3.0, f'Tick conflation too slow: {elapsed:.2f}s for 5,000 ticks'

    # Check OHLCV validity
    c = cs.get_active_bar('NIFTY_0', 'NFO', '1m')
    assert c is not None
    assert c['high'] >= c['low']
    assert c['high'] >= c['open']
    assert c['high'] >= c['close']
    assert c['low'] <= c['open']
    assert c['low'] <= c['close']
    assert c['volume'] > 0


def test_50_concurrent_order_storm(api_client, monkeypatch):
    # Enforce paper trading mode
    monkeypatch.setattr(config, 'PAPER_TRADE_MODE', True)
    monkeypatch.setattr(config, 'TRADING_MODE', 'PAPER')

    results = []
    errors = []

    def place_random_order(idx):
        action = 'BUY' if idx % 2 == 0 else 'SELL'
        payload = {
            'apikey': 'CHAOS_TEST_KEY_2026',
            'symbol': 'TATASTEEL',
            'action': action,
            'quantity': 10,
            'pricetype': 'MARKET',
            'product': 'MIS',
            'exchange': 'NSE'
        }
        try:
            res = api_client.post('/api/v1/placeorder', json=payload)
            if res.status_code == 200:
                results.append(res.json())
            else:
                errors.append(res.text)
        except Exception as e:
            errors.append(str(e))

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(place_random_order, i) for i in range(50)]
        for f in futures:
            f.result()

    # Verify no unhandled crashes
    assert len(errors) == 0, f'Errors occurred during order storm: {errors[:5]}'
    assert len(results) == 50


def test_simultaneous_circuit_breaker_and_panic_square_off(api_client, monkeypatch):
    monkeypatch.setattr(config, 'PAPER_TRADE_MODE', True)
    monkeypatch.setattr(config, 'TRADING_MODE', 'PAPER')

    # Trigger emergency panic square off all
    res = api_client.post('/api/v1/closeposition', json={'apikey': 'CHAOS_TEST_KEY_2026'})
    assert res.status_code == 200
    data = res.json()
    assert data.get('status') in ('success', 'error')


def test_concurrent_token_db_lookups():
    errors = []

    def lookup_tokens(worker_id):
        try:
            for i in range(50):
                tok = token_db.get_token('RELIANCE', 'NSE')
                sym = token_db.get_symbol('2885', 'NSE')
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=lookup_tokens, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f'Token DB errors: {errors}'
