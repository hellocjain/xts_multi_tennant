import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import datetime
import logging
import re
import json
import time
import os
import threading
import fcntl
import calendar
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import config

# IDEMPOTENT TRANSPORT ONLY: Never auto-retry POST requests at the network socket layer
api_session = requests.Session()
retries = Retry(
    total=3, 
    backoff_factor=0.3, 
    status_forcelist=[502, 503, 504],
    allowed_methods=frozenset(['GET', 'HEAD', 'OPTIONS'])
)
api_session.mount('https://', HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=retries))
api_session.mount('http://', HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=retries))

logger = logging.getLogger(__name__)

INTERACTIVE_TOKEN = None
MARKET_DATA_TOKEN = None
VALID_MD_BASE_URL = None
INTERACTIVE_TOKEN_ACQUIRED_AT = 0
MARKET_DATA_TOKEN_ACQUIRED_AT = 0
TOKEN_MAX_LIFESPAN_SECONDS = 72000 # 20 hours proactive renewal (XTS max is 24h)

FUT_MASTER = {}
CASH_MASTER = {}
FUT_NORM_MAP = {}
CASH_NORM_MAP = {}
OPT_MASTER = {}
OPT_ROOTS = set()

CACHE_DATE = None
CACHE_LOCK = threading.Lock()
MD_TOKEN_LOCK = threading.Lock()
INTERACTIVE_TOKEN_LOCK = threading.Lock()
REFRESH_LOCK = threading.Lock()

INTERACTIVE_REFRESH_CV = threading.Condition(INTERACTIVE_TOKEN_LOCK)
MD_REFRESH_CV = threading.Condition(MD_TOKEN_LOCK)

REFRESHING_INTERACTIVE = False
REFRESHING_MD = False

LAST_REFRESH_ATTEMPT = 0
REFRESH_COOLDOWN_SECONDS = 15
MIN_SANE_INSTRUMENT_COUNT = 500

SEGMENT_MAP = {
    "NSECM": 1, "NSEFO": 2, "NSECD": 3, "BSECM": 11, "BSEFO": 12, "BSECD": 13, "MCXFO": 51, "NCDEX": 21,
    "NSE": 1, "BSE": 11, "MCX": 51, "NCD": 21,
    1: 1, 2: 2, 3: 3, 11: 11, 12: 12, 13: 13, 51: 51, 21: 21
}

PREFIX_STRIPPER = re.compile(r'^(MCXFO|MCX|NSECD|NSEFO|NSE|BSEFO|BSECD|BSE|NCDEX|MSEI|CDS):', re.IGNORECASE)
SUFFIX_STRIPPER = re.compile(r'(\.NS|\.BO|[1-9]!|FUT)$', re.IGNORECASE)
ALPHANUM_ONLY = re.compile(r'[^A-Z0-9]')

FUT_MONTH_CODES = {'F': 1, 'G': 2, 'H': 3, 'J': 4, 'K': 5, 'M': 6, 'N': 7, 'Q': 8, 'U': 9, 'V': 10, 'X': 11, 'Z': 12}
MONTH_ABBR_TO_NUM = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
NUM_TO_MONTH_ABBR = {v: k for k, v in MONTH_ABBR_TO_NUM.items()}

CONTINUOUS_SUFFIX = re.compile(r'([1-9])!$', re.IGNORECASE)
DERIVATIVE_PREFIX = re.compile(r'^(MCXFO|MCX|NSECD|NSEFO|NCDEX|BSEFO|BSECD|MSEI|CDS):', re.IGNORECASE)

COMMODITY_MULTIPLIERS = {
    "CRUDEOIL": 100.0,
    "CRUDEOILM": 10.0,
    "NATURALGAS": 1250.0,
    "NATURALGASM": 250.0,
    "GOLD": 100.0,
    "GOLDM": 10.0,
    "GOLDPETAL": 1.0,
    "SILVER": 30.0,
    "SILVERM": 5.0,
    "SILVERMIC": 1.0,
    "COPPER": 2500.0,
    "ZINC": 5000.0,
    "LEAD": 5000.0,
    "ALUMINI": 1000.0,
    "ALUMINIUM": 5000.0,
    "COTTON": 25.0,
    "MCXBULLDEX": 50.0,
}

def get_contract_multiplier(symbol: str, exch_seg: str = "") -> float:
    if exch_seg and exch_seg not in ("MCXFO", "NCDEX"):
        return 1.0
    clean = resolve_symbol_smart(symbol)
    # Match longest root first so variants (GOLDPETAL, GOLDM, CRUDEOILM, SILVERMIC) are not shadowed by base roots
    for root in sorted(COMMODITY_MULTIPLIERS.keys(), key=len, reverse=True):
        if clean.startswith(root):
            return COMMODITY_MULTIPLIERS[root]
    for root in sorted(COMMODITY_MULTIPLIERS.keys(), key=len, reverse=True):
        if root in clean:
            return COMMODITY_MULTIPLIERS[root]
    return 1.0

COMMON_ALIASES = {
    "NIFTY50": "NIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "NATGAS": "NATURALGAS",
    "NATGASMINI": "NATURALGASM",
    "CRUDEOILMINI": "CRUDEOILM",
    "GOLDMINI": "GOLDM",
    "SILVERMINI": "SILVERM",
    "SILVERMICRO": "SILVERMIC",
    "ALUMINIUMMINI": "ALUMINI",
    "ZINCMINI": "ZINC",
    "LEADMINI": "LEAD",
}

NO_EXPIRY = datetime.date.max

def get_safe_base_url():
    return getattr(config, "XTS_API_BASE_URL", "").rstrip('/')

def send_ops_alert(message):
    url = getattr(config, "OPS_ALERT_WEBHOOK_URL", "")
    if not url:
        return
    try:
        api_session.post(url, json={"text": message}, timeout=3)
    except Exception:
        pass

def clear_tokens():
    global INTERACTIVE_TOKEN, MARKET_DATA_TOKEN, LAST_INTERACTIVE_AUTH_ATTEMPT, INTERACTIVE_TOKEN_ACQUIRED_AT, MARKET_DATA_TOKEN_ACQUIRED_AT
    with INTERACTIVE_REFRESH_CV:
        INTERACTIVE_TOKEN = None
        INTERACTIVE_TOKEN_ACQUIRED_AT = 0
        LAST_INTERACTIVE_AUTH_ATTEMPT = 0
    with MD_REFRESH_CV:
        MARKET_DATA_TOKEN = None
        MARKET_DATA_TOKEN_ACQUIRED_AT = 0
    logger.info("Session tokens cleared.")

LAST_INTERACTIVE_AUTH_ATTEMPT = 0
LAST_INTERACTIVE_AUTH_ERROR = None
INTERACTIVE_COOLDOWN_SECONDS = 15

def get_last_auth_error():
    return LAST_INTERACTIVE_AUTH_ERROR

def get_interactive_token(force_refresh=False):
    global INTERACTIVE_TOKEN, REFRESHING_INTERACTIVE, LAST_INTERACTIVE_AUTH_ATTEMPT, LAST_INTERACTIVE_AUTH_ERROR, INTERACTIVE_TOKEN_ACQUIRED_AT
    wait_timeout = getattr(config, "TOKEN_REFRESH_WAIT_TIMEOUT", 8.0)
    now = time.time()

    # Proactive expiry: If token older than 20 hours, force refresh
    if INTERACTIVE_TOKEN and (now - INTERACTIVE_TOKEN_ACQUIRED_AT > TOKEN_MAX_LIFESPAN_SECONDS):
        logger.info("INTERACTIVE token exceeded 20h proactive lifespan. Refreshing...")
        force_refresh = True

    if not force_refresh and INTERACTIVE_TOKEN:
        return INTERACTIVE_TOKEN

    # Cooldown guard only when there was an auth failure and not forced
    if not force_refresh and LAST_INTERACTIVE_AUTH_ERROR and (now - LAST_INTERACTIVE_AUTH_ATTEMPT < INTERACTIVE_COOLDOWN_SECONDS) and not INTERACTIVE_TOKEN:
        return None

    with INTERACTIVE_REFRESH_CV:
        if not force_refresh and INTERACTIVE_TOKEN:
            return INTERACTIVE_TOKEN
        if REFRESHING_INTERACTIVE:
            got_it = INTERACTIVE_REFRESH_CV.wait_for(lambda: (not REFRESHING_INTERACTIVE), timeout=wait_timeout)
            if INTERACTIVE_TOKEN:
                return INTERACTIVE_TOKEN
            if not got_it:
                logger.error("Timed out waiting for concurrent interactive-token refresh.")
                return None
        REFRESHING_INTERACTIVE = True

    try:
        LAST_INTERACTIVE_AUTH_ATTEMPT = time.time()
        safe_url = get_safe_base_url()
        url = f"{safe_url}/user/session"
        payload = {"appKey": getattr(config, "API_KEY", ""), "secretKey": getattr(config, "API_SECRET", ""), "source": "WEBAPI"}
        response = api_session.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get('type') == 'success':
            with INTERACTIVE_REFRESH_CV:
                INTERACTIVE_TOKEN = data['result']['token']
                INTERACTIVE_TOKEN_ACQUIRED_AT = time.time()
                LAST_INTERACTIVE_AUTH_ERROR = None
            return INTERACTIVE_TOKEN
        else:
            err_desc = data.get('description', 'Auth rejected')
            err_code = data.get('code', 'e-auth')
            LAST_INTERACTIVE_AUTH_ERROR = f"{err_code}: {err_desc}"
            logger.error(f"Interactive login rejected: {data}")
    except Exception as e:
        LAST_INTERACTIVE_AUTH_ERROR = str(e)
        logger.error(f"Interactive Login error: {e}")
    finally:
        with INTERACTIVE_REFRESH_CV:
            REFRESHING_INTERACTIVE = False
            INTERACTIVE_REFRESH_CV.notify_all()
    return None

def get_marketdata_token():
    global MARKET_DATA_TOKEN, VALID_MD_BASE_URL, REFRESHING_MD, MARKET_DATA_TOKEN_ACQUIRED_AT
    wait_timeout = getattr(config, "TOKEN_REFRESH_WAIT_TIMEOUT", 8.0)
    now = time.time()

    if MARKET_DATA_TOKEN and (now - MARKET_DATA_TOKEN_ACQUIRED_AT > TOKEN_MAX_LIFESPAN_SECONDS):
        logger.info("MARKET_DATA token exceeded 20h proactive lifespan. Refreshing...")
        with MD_REFRESH_CV:
            MARKET_DATA_TOKEN = None

    with MD_REFRESH_CV:
        if MARKET_DATA_TOKEN and VALID_MD_BASE_URL:
            return MARKET_DATA_TOKEN, VALID_MD_BASE_URL
        if REFRESHING_MD:
            got_it = MD_REFRESH_CV.wait_for(lambda: (not REFRESHING_MD), timeout=wait_timeout)
            if MARKET_DATA_TOKEN and VALID_MD_BASE_URL:
                return MARKET_DATA_TOKEN, VALID_MD_BASE_URL
            if not got_it:
                logger.error("Timed out waiting for concurrent market-data-token refresh.")
                return None, None
        REFRESHING_MD = True

    try:
        md_key = getattr(config, "MD_API_KEY", getattr(config, "API_KEY", ""))
        md_secret = getattr(config, "MD_API_SECRET", getattr(config, "API_SECRET", ""))
        payload = {"appKey": md_key, "secretKey": md_secret, "source": "WEBAPI"}

        safe_url = get_safe_base_url()
        base_urls = [
            safe_url.replace('/interactive', '/apimarketdata'),
            safe_url.replace('/interactive', '/marketdata'),
            safe_url.replace('/interactive', '/apibinarymarketdata'),
        ]

        for base_md in base_urls:
            for url in [f"{base_md.rstrip('/')}/auth/login", f"{base_md.rstrip('/')}/user/session"]:
                try:
                    response = api_session.post(url, json=payload, timeout=3)
                    if response.status_code == 200 and response.json().get('type') == 'success':
                        with MD_REFRESH_CV:
                            MARKET_DATA_TOKEN = response.json()['result']['token']
                            MARKET_DATA_TOKEN_ACQUIRED_AT = time.time()
                            VALID_MD_BASE_URL = base_md.rstrip('/')
                        return MARKET_DATA_TOKEN, VALID_MD_BASE_URL
                except requests.exceptions.RequestException:
                    pass
    except Exception as e:
        logger.error(f"Market Data login error: {e}")
    finally:
        with MD_REFRESH_CV:
            REFRESHING_MD = False
            MD_REFRESH_CV.notify_all()
    return None, None

def start_token_keepalive():
    interval = getattr(config, "TOKEN_KEEPALIVE_INTERVAL_SECONDS", 240)
    def _heartbeat():
        while True:
            time.sleep(interval)
            try:
                # Proactive refresh if token approaching 20h
                now = time.time()
                if INTERACTIVE_TOKEN and (now - INTERACTIVE_TOKEN_ACQUIRED_AT > TOKEN_MAX_LIFESPAN_SECONDS):
                    get_interactive_token(force_refresh=True)
                else:
                    t_int = get_interactive_token()
                    if t_int:
                        safe_url = get_safe_base_url()
                        api_session.get(f"{safe_url}/portfolio/positions?dayOrNet=DayWise", 
                                        headers={"authorization": t_int}, timeout=4)
                
                if MARKET_DATA_TOKEN and (now - MARKET_DATA_TOKEN_ACQUIRED_AT > TOKEN_MAX_LIFESPAN_SECONDS):
                    get_marketdata_token()
            except Exception:
                pass
    threading.Thread(target=_heartbeat, name="token-keepalive", daemon=True).start()

def _extract_expiry(parts, desc):
    for idx, col in enumerate(parts):
        col_str = col.strip()
        if len(col_str) >= 10 and 'T' in col_str and col_str.count('-') >= 2:
            try: return datetime.datetime.fromisoformat(col_str).date(), idx
            except ValueError: pass

    desc_upper = desc.upper().strip()

    m1 = re.search(r'(0[1-9]|[12]\d|3[01])([A-Z]{3})(20\d{2})', desc_upper)
    if m1:
        d, m, y = m1.groups()
        month_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        try: return datetime.date(int(y), month_map.get(m, 1), int(d)), None
        except ValueError: pass

    m2 = re.search(r'(2\d)([1-9OND])(0[1-9]|[12]\d|3[01])\d+(?:\.\d+)?(?:CE|PE)$', desc_upper)
    if m2:
        y, m_char, d = m2.groups()
        month_codes = {'1':1,'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'O':10,'N':11,'D':12}
        try: return datetime.date(2000 + int(y), month_codes.get(m_char, 1), int(d)), None
        except ValueError: pass

    m3 = re.search(r'(2\d)([A-Z]{3})(?:\d+(?:\.\d+)?(?:CE|PE)|FUT)$', desc_upper)
    if m3:
        y, m = m3.groups()
        year = 2000 + int(y)
        month_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        month = month_map.get(m, 1)
        
        c = calendar.Calendar(firstweekday=calendar.MONDAY)
        try:
            last_thurs = [day for week in c.monthdatescalendar(year, month) 
                          if day.weekday() == calendar.THURSDAY and day.month == month][-1]
            return last_thurs, None
        except IndexError:
            pass

    return None, None

def _sane_numeric(value, minimum, maximum, field_name, line_preview):
    if value is None:
        return None
    if not (minimum <= value <= maximum):
        return None
    return value

def apply_tick_size(price, tick_size, action="BUY"):
    if tick_size <= 0:
        tick_size = 0.05
    p = Decimal(str(round(price, 4)))
    t = Decimal(str(tick_size))
    if action == "BUY":
        ticks = (p / t).quantize(Decimal('1'), rounding=ROUND_CEILING)
    else:
        ticks = (p / t).quantize(Decimal('1'), rounding=ROUND_FLOOR)
    if ticks < Decimal('1'):
        ticks = Decimal('1')
    valid_price = ticks * t
    if tick_size >= 1.0:
        return float(valid_price.quantize(Decimal('1')))
    else:
        return float(valid_price)

def refresh_master_cache(force=False):
    global FUT_MASTER, CASH_MASTER, FUT_NORM_MAP, CASH_NORM_MAP, CACHE_DATE, LAST_REFRESH_ATTEMPT
    global OPT_MASTER, OPT_ROOTS

    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    today = datetime.datetime.now(IST).date()

    with CACHE_LOCK:
        if not force and CACHE_DATE == today and (FUT_MASTER or CASH_MASTER):
            return True

    acquired = REFRESH_LOCK.acquire(blocking=False)
    if not acquired:
        if CACHE_DATE != today or (not FUT_MASTER and not CASH_MASTER):
            logger.info("Cache is stale/empty and being refreshed. Waiting up to 25s...")
            got_lock = REFRESH_LOCK.acquire(timeout=25.0)
            if got_lock:
                REFRESH_LOCK.release()
        return bool(FUT_MASTER or CASH_MASTER)

    try:
        if not force and time.time() - LAST_REFRESH_ATTEMPT < REFRESH_COOLDOWN_SECONDS:
            return bool(FUT_MASTER or CASH_MASTER)
        LAST_REFRESH_ATTEMPT = time.time()

        md_token, md_base_url = get_marketdata_token()
        if not md_token:
            logger.error("Failed to acquire Market Data token.")
            send_ops_alert("XTS bot: failed to acquire market-data token during master refresh.")
            return bool(FUT_MASTER or CASH_MASTER)

        url = f"{md_base_url}/instruments/master"
        payload = {"exchangeSegmentList": ["MCXFO", "NSEFO", "NSECM", "NSECD", "NCDEX", "BSECM", "BSEFO", "BSECD"]}
        headers = {"authorization": md_token, "Content-Type": "application/json"}

        response = api_session.post(url, headers=headers, json=payload, timeout=25)
        data = response.json()

        if data.get('type') != 'success':
            logger.error(f"Master fetch returned non-success: {data}")
            send_ops_alert("XTS bot: master file fetch returned non-success payload.")
            return bool(FUT_MASTER or CASH_MASTER)

        lines = data.get('result', '').strip().split('\n')

        n_fut, n_cash, n_opt = {}, {}, {}
        n_fut_norm, n_cash_norm = {}, {}
        n_opt_roots = set()

        for line in lines:
            if not line: continue
            parts = line.split('|')
            if len(parts) < 5: continue

            try:
                exch_seg = parts[0].strip().upper()
                if exch_seg not in ["MCXFO", "NSEFO", "NSECM", "NSECD", "NCDEX", "BSECM", "BSEFO", "BSECD"]:
                    continue

                exch_id = int(parts[1])
                inst_type = parts[2].strip().upper()
                name = parts[3].strip().upper()
                desc = parts[4].strip().upper()

                tick_size, lot_size, freeze_qty = 0.05, 1, None
                if len(parts) > 10 and parts[10].strip():
                    try:
                        freeze_qty = _sane_numeric(int(float(parts[10])), 1, 10_000_000, "freeze_qty", line)
                    except Exception: pass
                if len(parts) > 11 and parts[11].strip():
                    try:
                        tick_size = _sane_numeric(float(parts[11]), 0.0001, 10000, "tick_size", line) or 0.05
                    except Exception: pass
                if len(parts) > 12 and parts[12].strip():
                    try:
                        lot_size = _sane_numeric(int(float(parts[12])), 1, 1_000_000, "lot_size", line) or 1
                    except Exception: pass
                if freeze_qty is None:
                    freeze_qty = getattr(config, "DEFAULT_FREEZE_QTY_IF_UNKNOWN", 100000)

                norm_key = ALPHANUM_ONLY.sub('', name)

                if exch_seg in ["NSECM", "BSECM"]:
                    if norm_key and norm_key not in n_cash_norm:
                        n_cash_norm[norm_key] = name
                    if name not in n_cash:
                        n_cash[name] = []
                    n_cash[name].append((NO_EXPIRY, exch_id, exch_seg, desc, tick_size, lot_size, freeze_qty))
                    continue

                if inst_type == "1" or "FUT" in desc:
                    if norm_key and norm_key not in n_fut_norm:
                        n_fut_norm[norm_key] = name

                    exp_date, _ = _extract_expiry(parts, desc)
                    if exp_date and exp_date >= today:
                        if name not in n_fut:
                            n_fut[name] = []
                        n_fut[name].append((exp_date, exch_id, exch_seg, desc, tick_size, lot_size, freeze_qty))

            except Exception:
                continue

        for k in n_fut:
            n_fut[k].sort(key=lambda x: x[0])
            
        for k in n_cash:
            n_cash[k].sort(key=lambda x: SEGMENT_MAP.get(x[2], 99))

        total_new = len(n_fut) + len(n_cash)
        if total_new < MIN_SANE_INSTRUMENT_COUNT:
            logger.error(f"MASTER FILE SANITY SHIELD: Fetch returned only {total_new} instruments. Cache kept.")
            return bool(FUT_MASTER or CASH_MASTER)

        with CACHE_LOCK:
            FUT_MASTER, CASH_MASTER = n_fut, n_cash
            FUT_NORM_MAP, CASH_NORM_MAP = n_fut_norm, n_cash_norm
            OPT_MASTER = n_opt
            OPT_ROOTS = n_opt_roots
            CACHE_DATE = today
            logger.info(f"Master Cache Loaded: {len(FUT_MASTER)} Futures | {len(CASH_MASTER)} Cash.")
        return True

    except Exception as e:
        logger.error(f"Failed to load master cache: {e}")
        return bool(FUT_MASTER or CASH_MASTER)
    finally:
        REFRESH_LOCK.release()

def get_instrument_by_id(target_id: int):
    with CACHE_LOCK:
        for root, contract_list in FUT_MASTER.items():
            for c in contract_list:
                if c[1] == target_id:
                    return {
                        "name": root,
                        "expiry": c[0],
                        "inst_id": c[1],
                        "exch_seg": c[2],
                        "desc": c[3],
                        "tick_size": c[4],
                        "lot_size": c[5],
                        "freeze_qty": c[6],
                    }
        for root, contract_list in CASH_MASTER.items():
            for c in contract_list:
                if c[1] == target_id:
                    return {
                        "name": root,
                        "expiry": c[0],
                        "inst_id": c[1],
                        "exch_seg": c[2],
                        "desc": c[3],
                        "tick_size": c[4],
                        "lot_size": c[5],
                        "freeze_qty": c[6],
                    }
    return None

def cache_is_healthy():
    with CACHE_LOCK:
        IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        today = datetime.datetime.now(IST).date()
        return bool((FUT_MASTER or CASH_MASTER) and CACHE_DATE == today)

def start_cache_watchdog():
    interval = getattr(config, "CACHE_WATCHDOG_INTERVAL_SECONDS", 30)
    def _loop():
        while True:
            try:
                if not cache_is_healthy():
                    logger.warning("CACHE WATCHDOG: cache stale/empty, forcing refresh attempt.")
                    refresh_master_cache(force=True)
            except Exception as e:
                logger.error(f"Cache watchdog error: {e}")
            time.sleep(interval)
    t = threading.Thread(target=_loop, name="cache-watchdog", daemon=True)
    t.start()
    return t

def resolve_symbol_smart(raw_symbol):
    clean_sym = str(raw_symbol).upper().strip()
    clean_sym = PREFIX_STRIPPER.sub('', clean_sym)
    clean_sym = SUFFIX_STRIPPER.sub('', clean_sym)
    user_map = getattr(config, "TV_TO_XTS_MAP", {})
    if clean_sym in user_map:
        return user_map[clean_sym]
    if clean_sym in COMMON_ALIASES:
        return COMMON_ALIASES[clean_sym]
    return clean_sym

def _apply_aliases(name):
    user_map = getattr(config, "TV_TO_XTS_MAP", {})
    if name in user_map:
        return user_map[name]
    if name in COMMON_ALIASES:
        return COMMON_ALIASES[name]
    return name

def _lookup_exact_or_normalized(name, cache, norm_map):
    if name in cache and cache[name]:
        return name
    norm_key = ALPHANUM_ONLY.sub('', name)
    if norm_key in norm_map:
        matched = norm_map[norm_key]
        if matched in cache and cache[matched]:
            return matched
    return None

def _resolve_front_month(symbol, target_name, is_future_intent=True, depth=1):
    search_queues = [(FUT_MASTER, FUT_NORM_MAP), (CASH_MASTER, CASH_NORM_MAP)] if is_future_intent \
        else [(CASH_MASTER, CASH_NORM_MAP), (FUT_MASTER, FUT_NORM_MAP)]

    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).date()

    with CACHE_LOCK:
        for cache, norm_map in search_queues:
            matched = _lookup_exact_or_normalized(target_name, cache, norm_map)
            if matched:
                available_contracts = cache[matched]
                
                valid_contracts = []
                for c in available_contracts:
                    exp_date = c[0]
                    exch_seg = c[2]
                    if exp_date == NO_EXPIRY:
                        valid_contracts.append(c)
                        continue
                    days_left = (exp_date - today).days
                    min_days = getattr(config, "MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX", 3) if exch_seg in ("MCXFO", "NCDEX") \
                        else getattr(config, "MIN_DAYS_BEFORE_EXPIRY_DERIVATIVES", 0)
                    
                    if days_left > min_days:
                        valid_contracts.append(c)

                candidates = valid_contracts if valid_contracts else available_contracts
                idx = min(max(0, depth - 1), len(candidates) - 1)
                front = candidates[idx]
                exp_date, exch_id, exch_seg, desc, tick_size, lot_size, freeze_qty = front
                prod_type = "MIS" if exch_seg in ["NSECM", "BSECM"] else "NRML"
                logger.info(f"FRONT-MONTH MATCH -> {symbol} => {matched} [{desc}] : ID {exch_id} | Seg {exch_seg} | Expiry {exp_date}")
                return exch_id, exch_seg, prod_type, tick_size, lot_size, freeze_qty, exp_date

    logger.error(f"Cannot resolve active contract for '{symbol}'. Trade aborted.")
    return None, None, None, None, None, None, None

def get_dynamic_contract_info(symbol):
    refresh_master_cache()

    target_name = resolve_symbol_smart(symbol)
    cont_match = CONTINUOUS_SUFFIX.search(symbol)
    is_continuous = bool(cont_match)
    depth = int(cont_match.group(1)) if is_continuous else 1
    is_derivative_prefix = bool(DERIVATIVE_PREFIX.match(symbol))

    if is_continuous:
        return _resolve_front_month(symbol, target_name, is_future_intent=True, depth=depth)

    clean_sym = PREFIX_STRIPPER.sub('', symbol.upper().strip())
    clean_sym = re.sub(r'[\s\-_]+', '', clean_sym)
    clean_sym = re.sub(r'FUT$', '', clean_sym, flags=re.IGNORECASE)

    with CACHE_LOCK:
        known_roots = sorted(FUT_MASTER.keys(), key=len, reverse=True)

    for root in known_roots:
        aliased_root = _apply_aliases(root)
        if clean_sym.startswith(root) or clean_sym.startswith(aliased_root):
            matched_root = root
            date_part = clean_sym[len(root):] if clean_sym.startswith(root) else clean_sym[len(aliased_root):]
            
            if not date_part:
                return _resolve_front_month(symbol, matched_root, is_future_intent=True, depth=1)

            with CACHE_LOCK:
                candidates = FUT_MASTER.get(matched_root, [])

            m_letter = re.match(r'^([FGHJKMNQUVXZ])(\d{2}|\d{4})$', date_part)
            if m_letter:
                m_code, y_str = m_letter.groups()
                target_month = FUT_MONTH_CODES[m_code]
                target_year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
                for c in candidates:
                    if c[0].year == target_year and c[0].month == target_month:
                        return c[1], c[2], "NRML", c[4], c[5], c[6], c[0]

            m_text = re.match(r'^(\d{0,2})([A-Z]{3})(\d{0,4})$', date_part)
            if m_text:
                d_str, mon_str, y_str = m_text.groups()
                if mon_str in MONTH_ABBR_TO_NUM:
                    target_month = MONTH_ABBR_TO_NUM[mon_str]
                    target_year = None
                    if y_str:
                        target_year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
                    elif d_str and len(d_str) == 2:
                        target_year = 2000 + int(d_str)

                    for c in candidates:
                        year_match = (target_year is None) or (c[0].year == target_year)
                        if year_match and c[0].month == target_month:
                            return c[1], c[2], "NRML", c[4], c[5], c[6], c[0]

    if is_derivative_prefix:
        return _resolve_front_month(symbol, target_name, is_future_intent=True, depth=1)

    return _resolve_front_month(symbol, target_name, is_future_intent=False, depth=1)

def get_live_price(instrument_id, exch_seg):
    md_token, md_base_url = get_marketdata_token()
    if not md_token:
        return None
    seg_numeric_id = SEGMENT_MAP.get(exch_seg, 51)
    url = f"{md_base_url}/instruments/quotes"
    headers = {"authorization": md_token, "Content-Type": "application/json"}
    payload = {
        "instruments": [{"exchangeSegment": seg_numeric_id, "exchangeInstrumentID": instrument_id}],
        "xtsMessageCode": 1512, "publishFormat": "JSON",
    }
    try:
        response = api_session.post(url, headers=headers, json=payload, timeout=5)
        data = response.json()
        
        if data.get('type') == 'error' and any(kw in str(data).lower() for kw in ("token", "session", "auth", "unauthorized")):
            logger.error("Market Data Token expired during price fetch. Clearing session tokens.")
            clear_tokens()
            return "TOKEN_EXPIRED"

        if data.get('type') == 'success':
            list_quotes = data.get('result', {}).get('listQuotes', [])
            raw = list_quotes[0] if (isinstance(list_quotes, list) and list_quotes) else (list_quotes if isinstance(list_quotes, dict) else None)

            if raw is not None:
                quote_data = json.loads(raw) if isinstance(raw, str) else raw
                touchline = quote_data.get('Touchline') or {}
                ltp = quote_data.get('LastTradedPrice') or touchline.get('LastTradedPrice', 0)
                try:
                    ltp = float(ltp or 0)
                except (ValueError, TypeError):
                    return None
                return ltp if ltp > 0 else None
    except Exception as e:
        logger.error(f"get_live_price parse failure for instrument {instrument_id}: {e}")
    return None

def get_live_prices_batch(instrument_pairs):
    """Batch queries live quotes for a list of (instrument_id, exch_seg) tuples."""
    if not instrument_pairs:
        return {}
    md_token, md_base_url = get_marketdata_token()
    if not md_token:
        return {}

    inst_list = []
    seen = set()
    for iid, seg in instrument_pairs:
        if iid and (iid, seg) not in seen:
            seen.add((iid, seg))
            inst_list.append({"exchangeSegment": SEGMENT_MAP.get(seg, 51), "exchangeInstrumentID": int(iid)})

    if not inst_list:
        return {}

    url = f"{md_base_url}/instruments/quotes"
    headers = {"authorization": md_token, "Content-Type": "application/json"}
    payload = {
        "instruments": inst_list,
        "xtsMessageCode": 1512, "publishFormat": "JSON",
    }
    prices = {}
    try:
        response = api_session.post(url, headers=headers, json=payload, timeout=5)
        data = response.json()
        if data.get('type') == 'success':
            list_quotes = data.get('result', {}).get('listQuotes', [])
            if isinstance(list_quotes, list):
                for raw in list_quotes:
                    try:
                        quote_data = json.loads(raw) if isinstance(raw, str) else raw
                        touchline = quote_data.get('Touchline') or {}
                        ltp = float(quote_data.get('LastTradedPrice') or touchline.get('LastTradedPrice', 0) or 0)
                        iid = int(quote_data.get('ExchangeInstrumentID', 0) or touchline.get('ExchangeInstrumentID', 0))
                        if iid and ltp > 0:
                            prices[iid] = ltp
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"Batch quotes fetch error: {e}")
    return prices

def _get_daily_risk_file():
    data_dir = getattr(config, "DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(data_dir, "daily_risk_state.json")

def _get_paper_trade_file():
    data_dir = getattr(config, "DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(data_dir, "paper_trades.log")

def _today_ist_str():
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.now(IST).date().isoformat()

def get_daily_notional_state():
    risk_file = _get_daily_risk_file()
    today_str = _today_ist_str()
    cap = getattr(config, "DAILY_NOTIONAL_CAP_INR", 10000000.0)
    if not os.path.exists(risk_file):
        return {"date": today_str, "notional": 0.0, "cap": cap, "remaining": cap}
    try:
        with open(risk_file, "r") as f:
            state = json.load(f)
            if state.get("date") == today_str:
                n = float(state.get("notional", 0.0))
                return {"date": today_str, "notional": n, "cap": cap, "remaining": max(0.0, cap - n)}
    except Exception:
        pass
    return {"date": today_str, "notional": 0.0, "cap": cap, "remaining": cap}

def check_and_reserve_daily_notional(order_val):
    cap = getattr(config, "DAILY_NOTIONAL_CAP_INR", 10000000.0)
    if cap is None or cap <= 0:
        return True, None
        
    today_str = _today_ist_str()
    risk_file = _get_daily_risk_file()
    
    fd = os.open(risk_file, os.O_RDWR | os.O_CREAT, 0o666)
    with open(fd, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX) 
        try:
            f.seek(0)
            try:
                state = json.load(f)
            except Exception:
                state = {"date": today_str, "notional": 0.0}
                
            if state.get("date") != today_str:
                state = {"date": today_str, "notional": 0.0}
                
            if state["notional"] + order_val > cap:
                return False, state["notional"]
                
            state["notional"] += order_val
            
            f.seek(0)
            f.write(json.dumps(state))
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
            return True, state["notional"]
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def refund_daily_notional(order_val):
    cap = getattr(config, "DAILY_NOTIONAL_CAP_INR", 10000000.0)
    if cap is None or cap <= 0:
        return
        
    today_str = _today_ist_str()
    risk_file = _get_daily_risk_file()
    
    if not os.path.exists(risk_file):
        return
        
    fd = os.open(risk_file, os.O_RDWR | os.O_CREAT, 0o666)
    with open(fd, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            try:
                state = json.load(f)
            except Exception:
                state = {"date": today_str, "notional": 0.0}
                
            if state.get("date") == today_str:
                state["notional"] = max(0.0, state["notional"] - order_val)
                f.seek(0)
                f.write(json.dumps(state))
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def _log_paper_trade_to_file(action, symbol, execution_qty, instrument_id, exch_seg,
                              execution_price, order_ref, order_val, paper_order_id):
    if not getattr(config, "LOG_PAPER_TRADES_TO_FILE", False):
        return
    try:
        IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        record = {
            "timestamp": datetime.datetime.now(IST).isoformat(),
            "paper_order_id": paper_order_id,
            "order_ref": order_ref,
            "action": action,
            "symbol": symbol,
            "instrument_id": instrument_id,
            "exchange_segment": exch_seg,
            "execution_qty": execution_qty,
            "execution_price": execution_price,
            "order_value_inr": round(order_val, 2),
        }
        with open(_get_paper_trade_file(), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.error(f"Failed to write paper trade log entry: {e}")

XTS_STATUS_CODE_MAP = {
    48: "New",
    49: "PartiallyFilled",
    50: "Filled",
    52: "Cancelled",
    53: "Replaced",
    54: "PendingCancel",
    56: "Rejected",
    65: "PendingNew",
    69: "PendingReplace",
}

def check_order_status_by_ref(order_ref):
    token = get_interactive_token()
    if not token:
        return "NETWORK_ERROR"
    safe_url = get_safe_base_url()
    url = f"{safe_url}/orders"
    headers = {"authorization": token}
    try:
        response = api_session.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('type') == 'success':
                for ord in data.get('result', []):
                    ref_tag = ord.get('OrderUniqueIdentifier') or ord.get('orderUniqueIdentifier')
                    if ref_tag and str(ref_tag) == str(order_ref):
                        raw_st = ord.get('OrderStatus')
                        if isinstance(raw_st, int) or (isinstance(raw_st, str) and raw_st.isdigit()):
                            return XTS_STATUS_CODE_MAP.get(int(raw_st), str(raw_st))
                        return str(raw_st) if raw_st is not None else "UNKNOWN"
                return "NOT_FOUND"
    except Exception as e:
        logger.error(f"Failed to check order status for {order_ref}: {e}")
        return "NETWORK_ERROR"
    return "NOT_FOUND"

def execute_trade_with_retry(action, symbol, quantity, tv_price, order_ref, attempt=1):
    result = place_order(action, symbol, quantity, tv_price, order_ref)

    if result.get("status") == "error" or result.get("type") == "error" or result.get("type") != "success":
        err_msg = str(result.get("description") or result.get("message") or result.get("error") or result).lower()
        is_auth_issue = any(kw in err_msg for kw in ("token", "session", "auth"))
        is_transient = any(kw in err_msg for kw in ("connection", "expecting value"))
        is_timeout = any(kw in err_msg for kw in ("timeout", "timed out", "readtimeout", "bad gateway", "502", "503", "504"))

        if is_timeout and attempt == 1:
            logger.critical(f"TIMEOUT/GATEWAY SHIELD: Order {order_ref} state unknown. Refusing blind retry.")
            send_ops_alert(f"CRITICAL: Order {order_ref} timed out or hit gateway error. Verify terminal manually.")
            return {"status": "error", "message": "Order placed but API timed out. Check terminal manually."}

        if is_transient and attempt == 1:
            logger.critical(f"TRANSIENT DROP SHIELD: Order {order_ref} network dropped post-dispatch. Refusing blind retry.")
            return {"status": "error", "message": "Connection lost post-dispatch. Manual check required."}

        if is_auth_issue and attempt == 1:
            logger.warning("Session expired. Auto-healing authentication immediately...")
            clear_tokens()
            return execute_trade_with_retry(action, symbol, quantity, tv_price, order_ref, attempt=2)

    if result.get("status") == "error" or result.get("type") == "error":
        send_ops_alert(f"XTS bot: order FAILED for {symbol} ({action} x{quantity}) ref={order_ref}: {result}")

    return result

def _monitor_and_clean_partial_fills(order_ref, client_id, token):
    timeout_sec = getattr(config, "PARTIAL_FILL_TIMEOUT_SECONDS", 2.0)
    time.sleep(timeout_sec)
    
    safe_url = get_safe_base_url()
    url = f"{safe_url}/orders"
    headers = {"authorization": token}
    try:
        resp = api_session.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('type') == 'success':
                for ord in data.get('result', []):
                    ref_tag = ord.get('OrderUniqueIdentifier') or ord.get('orderUniqueIdentifier')
                    if ref_tag and str(ref_tag) == str(order_ref):
                        status = ord.get('OrderStatus')
                        app_order_id = ord.get('AppOrderID')
                        if status in ("PartiallyFilled", "Open", "New") and app_order_id:
                            logger.warning(f"PARTIAL FILL GUARD: Cancelling unfilled remainder for AppOrderID {app_order_id} (Status: {status})")
                            cancel_url = f"{safe_url}/orders?appOrderID={app_order_id}&clientID={client_id}"
                            api_session.delete(cancel_url, json={"appOrderID": app_order_id, "clientID": client_id}, headers=headers, timeout=5)
                            return
    except Exception as e:
        logger.error(f"Partial fill monitor error: {e}")

def place_order(action, symbol, quantity, tv_price, order_ref):
    is_paper = getattr(config, "PAPER_TRADE_MODE", False)
    token = None
    if not is_paper:
        token = get_interactive_token()
        if not token:
            return {"status": "error", "message": "Auth failed"}

    instrument_id, exch_seg, prod_type, tick_size, lot_size, freeze_qty, contract_expiry = get_dynamic_contract_info(symbol)
    if not instrument_id:
        if is_paper:
            instrument_id = 99999
            exch_seg = "MCXFO" if "MCX" in symbol or any(k in symbol for k in ("CRUDE", "GOLD", "SILVER", "NAT")) else "NSEFO"
            prod_type = "NRML"
            tick_size = 0.05
            lot_size = 1
            freeze_qty = 100000
            contract_expiry = datetime.date.today()
        else:
            return {"status": "error", "message": f"Instrument resolution failed for {symbol}"}

    cfg_prod = str(getattr(config, "DEFAULT_PRODUCT_TYPE", getattr(config, "PRODUCT_TYPE", ""))).strip().upper()
    if cfg_prod in ("NRML", "MIS", "CNC"):
        prod_type = cfg_prod

    tv_sends_lots = getattr(config, "TV_SENDS_LOTS", True)
    execution_qty = (quantity * lot_size) if (tv_sends_lots and exch_seg not in ["NSECM", "BSECM"]) else quantity

    if execution_qty > freeze_qty:
        return {"status": "error", "message": f"Quantity ({execution_qty}) exceeds broker freeze limit ({freeze_qty})"}

    max_lots = getattr(config, "MAX_LOTS_LIMIT", 100)
    if (execution_qty // lot_size) > max_lots:
        return {"status": "error", "message": f"Order lots ({execution_qty // lot_size}) exceed safety cap ({max_lots})"}

    max_units = getattr(config, "MAX_UNITS_LIMIT", 100000)
    if execution_qty > max_units:
        return {"status": "error", "message": f"Quantity ({execution_qty}) exceeds max safety units ({max_units})"}

    live_price = get_live_price(instrument_id, exch_seg)
    base_price = live_price if (live_price and live_price != "TOKEN_EXPIRED") else tv_price

    if base_price <= 0:
        base_price = tv_price if tv_price > 0 else 1.0

    buffer_pct = getattr(config, "SLIPPAGE_BUFFER_PCT", 0.005)
    buffer = max(base_price * buffer_pct, tick_size * 5)
    raw_limit_price = (base_price + buffer) if action == "BUY" else (base_price - buffer)
    execution_price = apply_tick_size(raw_limit_price, tick_size, action)

    contract_mult = get_contract_multiplier(symbol, exch_seg)
    order_val = base_price * execution_qty * contract_mult
    max_val = getattr(config, "MAX_ORDER_VALUE_INR", 5000000.0)
    if order_val > max_val:
        logger.error(f"POSITION VALUE SHIELD: Order value Rs {order_val:,.2f} exceeds cap Rs {max_val:,.2f}.")
        return {"status": "error", "message": "Order value exceeds max safety threshold"}
        
    allowed, running_total = check_and_reserve_daily_notional(order_val)
    if not allowed:
        logger.error(f"DAILY NOTIONAL SHIELD: Cap reached (already at Rs {running_total:,.2f}).")
        return {"status": "error", "message": "Daily cumulative notional cap reached"}

    client_id = getattr(config, "CLIENT_ID", "").strip()
    if not client_id:
        refund_daily_notional(order_val)
        return {"status": "error", "message": "CRITICAL: CLIENT_ID is unconfigured."}

    time_in_force = getattr(config, "TIME_IN_FORCE", "DAY")
    safe_url = get_safe_base_url()
    url = f"{safe_url}/orders"
    headers = {"authorization": token, "Content-Type": "application/json"}
    payload = {
        "exchangeSegment": exch_seg,
        "exchangeInstrumentID": instrument_id,
        "productType": prod_type,
        "orderType": "LIMIT",
        "orderSide": action,
        "timeInForce": time_in_force,
        "disclosedQuantity": 0,
        "orderQuantity": execution_qty,
        "limitPrice": execution_price,
        "stopPrice": 0,
        "apiOrderSource": "WEBAPI",
        "orderUniqueIdentifier": order_ref,
        "clientID": client_id,
    }

    if getattr(config, "PAPER_TRADE_MODE", False):
        paper_order_id = f"PAPER_{int(time.time() * 1000)}"
        simulated_response = {
            "type": "success",
            "code": "s-orders-0001",
            "description": "Paper order executed successfully",
            "result": {
                "AppOrderID": paper_order_id,
                "OrderUniqueIdentifier": order_ref,
                "OrderStatus": "Filled",
                "ExecutionPrice": base_price,
                "ExecutionQty": execution_qty,
                "ExchangeSegment": exch_seg,
                "ExchangeInstrumentID": instrument_id,
                "IsPaperTrade": True,
            },
        }

        log_line = f"[PAPER MARKET TRADE] {action} {execution_qty} qty of {symbol} (ID: {instrument_id} [{contract_expiry}]) @ Limit Px Rs {execution_price} (LTP Rs {base_price}) | Ref: {order_ref}"
        logger.info(log_line)

        _log_paper_trade_to_file(
            action, symbol, execution_qty, instrument_id, exch_seg,
            base_price, order_ref, order_val, paper_order_id,
        )
        return simulated_response

    try:
        logger.info(f"Routing MARKETABLE LIMIT {action} Order -> Exec Qty: {execution_qty} (ID: {instrument_id}) | Limit Px: {execution_price} (LTP: {base_price})")
        response = api_session.post(url, headers=headers, json=payload, timeout=8)
        data = response.json()

        if data.get('type') == 'success':
            logger.info(f"✅ BROKER ACCEPTED ORDER: {data}")
            
            if getattr(config, "CANCEL_LINGERING_PARTIAL_FILLS", True):
                threading.Thread(
                    target=_monitor_and_clean_partial_fills,
                    args=(order_ref, client_id, token),
                    daemon=True
                ).start()
        else:
            logger.error(f"❌ BROKER REJECTED: {data}")
            refund_daily_notional(order_val)

        return data
    except requests.exceptions.Timeout as e:
        logger.critical(f"TIMEOUT: State unknown. NOT refunding notional cap for {order_ref}. Verify manually.")
        return {"status": "error", "message": f"timeout: {e}"}
    except Exception as e:
        logger.error(f"Network error routing order: {e}")
        refund_daily_notional(order_val)
        return {"status": "error", "message": str(e)}

def get_margin_telemetry():
    """Fetches RMS balance and margin sub-limits from Symphony XTS broker."""
    if getattr(config, "PAPER_TRADE_MODE", False):
        return {
            "available_margin": 1000000.0,
            "margin_used": 0.0,
            "total_collateral": 500000.0,
            "net_margin_available": 1000000.0,
            "total_account_value": 1500000.0,
            "is_simulated": True,
            "error": None
        }

    token = get_interactive_token()
    if not token:
        return {
            "available_margin": 0.0, "margin_used": 0.0, "total_collateral": 0.0,
            "net_margin_available": 0.0, "total_account_value": 0.0,
            "is_simulated": False, "error": "Broker Auth Failed"
        }

    client_id = getattr(config, "CLIENT_ID", "").strip()
    safe_url = get_safe_base_url()
    headers = {"authorization": token}

    # Primary XTS endpoint: /user/limits
    for endpoint in [f"{safe_url}/user/limits?dayOrNet=DayWise&clientID={client_id}", f"{safe_url}/user/balance"]:
        try:
            resp = api_session.get(endpoint, headers=headers, timeout=4)
            if resp.status_code in (400, 401, 403):
                clear_tokens()
                token = get_interactive_token(force_refresh=True)
                if token:
                    headers = {"authorization": token}
                    resp = api_session.get(endpoint, headers=headers, timeout=4)

            if resp.status_code == 200:
                data = resp.json()
                if data.get('type') == 'success':
                    bal_list = data.get('result', {}).get('BalanceList', [])
                    if bal_list and isinstance(bal_list, list):
                        rms = bal_list[0].get('limitObject', {}).get('RMSSubLimits', {})
                        cash_avail = float(rms.get('cashAvailable', 0.0) or 0.0)
                        collateral = float(rms.get('collateralMargin', 0.0) or 0.0)
                        margin_used = float(rms.get('marginUtilized', 0.0) or 0.0)
                        net_avail = float(rms.get('netMarginAvailable', cash_avail + collateral - margin_used) or (cash_avail + collateral - margin_used))
                        return {
                            "available_margin": cash_avail,
                            "margin_used": margin_used,
                            "total_collateral": collateral,
                            "net_margin_available": net_avail,
                            "total_account_value": cash_avail + collateral,
                            "is_simulated": False,
                            "error": None
                        }
        except Exception:
            pass

    return {
        "available_margin": 0.0, "margin_used": 0.0, "total_collateral": 0.0,
        "net_margin_available": 0.0, "total_account_value": 0.0,
        "is_simulated": False, "error": "Margin API Unavailable from Broker Feed"
    }

def get_positions_telemetry():
    """Fetches live broker positions (NetWise) and computes real-time MTM telemetry."""
    if getattr(config, "PAPER_TRADE_MODE", False):
        return {
            "is_paper_trade": True,
            "positions_count": 0,
            "unrealized_mtm": 0.0,
            "realized_pnl": 0.0,
            "net_mtm": 0.0,
            "positions": []
        }

    token = get_interactive_token()
    if not token:
        return {"error": "Auth failed", "positions": []}

    safe_url = get_safe_base_url()
    url = f"{safe_url}/portfolio/positions?dayOrNet=NetWise"
    headers = {"authorization": token}

    try:
        resp = api_session.get(url, headers=headers, timeout=5)
        if resp.status_code in (400, 401, 403):
            clear_tokens()
            token = get_interactive_token(force_refresh=True)
            if token:
                headers = {"authorization": token}
                resp = api_session.get(url, headers=headers, timeout=5)

        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "positions": []}

        data = resp.json()
        positions_raw = data.get("result", {}).get("positionList", [])
        
        pos_list = []
        total_unrealized = 0.0
        total_realized = 0.0

        # Batch query all live quotes in a single network request
        batch_pairs = []
        for p in positions_raw:
            iid = int(p.get("ExchangeInstrumentId") or p.get("ExchangeInstrumentID") or p.get("InstrumentId") or 0)
            seg = p.get("ExchangeSegment", "MCXFO")
            if iid > 0:
                batch_pairs.append((iid, seg))

        live_prices_map = get_live_prices_batch(batch_pairs) if batch_pairs else {}

        for p in positions_raw:
            sym = str(p.get("TradingSymbol") or "")
            inst_id = int(p.get("ExchangeInstrumentId") or p.get("ExchangeInstrumentID") or p.get("InstrumentId") or 0)
            exch_seg = str(p.get("ExchangeSegment") or "MCXFO")
            qty = int(p.get("Quantity", 0) or 0)
            mult = float(p.get("Multiplier", 1) or 1)
            
            buy_avg = float(p.get("BuyAveragePrice", 0) or p.get("ActualBuyAveragePrice", 0) or p.get("AveragePrice", 0) or 0)
            sell_avg = float(p.get("SellAveragePrice", 0) or p.get("ActualSellAveragePrice", 0) or 0)
            
            open_buy_qty = int(p.get("OpenBuyQuantity", 0) or 0)
            open_sell_qty = int(p.get("OpenSellQuantity", 0) or 0)
            buy_amt = float(p.get("BuyAmount", 0) or p.get("ActualBuyAmount", 0) or 0)
            sell_amt = float(p.get("SellAmount", 0) or p.get("ActualSellAmount", 0) or 0)

            # Defensive carryforward price reconstruction if broker returns 0 buy average
            if buy_avg == 0 and open_buy_qty > 0 and buy_amt > 0:
                buy_avg = buy_amt / (open_buy_qty * mult) if mult > 0 else (buy_amt / open_buy_qty)
            if sell_avg == 0 and open_sell_qty > 0 and sell_amt > 0:
                sell_avg = sell_amt / (open_sell_qty * mult) if mult > 0 else (sell_amt / open_sell_qty)

            ltp = live_prices_map.get(inst_id)
            if not ltp or ltp <= 0:
                ltp = float(p.get("LastTradedPrice", 0) or p.get("LTP", 0) or buy_avg or sell_avg)

            unrealized_pnl = 0.0
            side = "FLAT"

            if qty > 0:
                side = "LONG"
                unrealized_pnl = (ltp - buy_avg) * qty * mult
            elif qty < 0:
                side = "SHORT"
                unrealized_pnl = (sell_avg - ltp) * abs(qty) * mult
            
            realized_raw = float(p.get("RealizedMTM", 0) or p.get("RealizedPNL", 0) or 0)
            if realized_raw != 0:
                realized_pnl = realized_raw
            else:
                net_amt = float(p.get("NetAmount", 0) or 0)
                closed_qty = min(open_buy_qty, open_sell_qty)

                if closed_qty > 0 and buy_avg > 0 and sell_avg > 0:
                    realized_pnl = (sell_avg - buy_avg) * closed_qty * mult
                elif sell_amt > 0 and buy_amt > 0:
                    realized_pnl = (sell_amt - buy_amt)
                elif net_amt != 0 and qty == 0:
                    realized_pnl = net_amt
                else:
                    realized_pnl = 0.0

            total_realized += realized_pnl
            total_unrealized += unrealized_pnl

            pos_list.append({
                "symbol": sym,
                "instrument_id": inst_id,
                "segment": exch_seg,
                "side": side,
                "quantity": qty,
                "buy_avg": round(buy_avg, 2),
                "sell_avg": round(sell_avg, 2),
                "ltp": round(ltp, 2),
                "unrealized_mtm": round(unrealized_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
            })

        active_positions = [p for p in pos_list if p["quantity"] != 0]
        closed_positions = [p for p in pos_list if p["quantity"] == 0]

        return {
            "is_paper_trade": False,
            "positions_count": len(active_positions),
            "all_positions_count": len(pos_list),
            "unrealized_mtm": round(total_unrealized, 2),
            "realized_pnl": round(total_realized, 2),
            "net_mtm": round(total_unrealized + total_realized, 2),
            "positions": active_positions,
            "closed_positions": closed_positions,
            "all_positions": pos_list
        }
    except Exception as e:
        logger.error(f"Error fetching position telemetry: {e}")
        return {"error": str(e), "positions": [], "all_positions": []}

def get_holdings_telemetry():
    """Fetches Demat CNC Equity Holdings and computes real-time valuation and P&L."""
    if getattr(config, "PAPER_TRADE_MODE", False):
        return {
            "invested_value": 0.0,
            "current_value": 0.0,
            "overall_pnl": 0.0,
            "overall_pnl_pct": 0.0,
            "day_pnl": 0.0,
            "day_pnl_pct": 0.0,
            "holdings_count": 0,
            "holdings": []
        }

    token = get_interactive_token()
    if not token:
        return {
            "invested_value": 0.0, "current_value": 0.0, "overall_pnl": 0.0,
            "overall_pnl_pct": 0.0, "day_pnl": 0.0, "day_pnl_pct": 0.0,
            "holdings_count": 0, "holdings": [], "error": "Auth failed"
        }

    safe_url = get_safe_base_url()
    url = f"{safe_url}/portfolio/holdings"
    headers = {"authorization": token}

    try:
        resp = api_session.get(url, headers=headers, timeout=5)
        if resp.status_code in (400, 401, 403):
            clear_tokens()
            token = get_interactive_token(force_refresh=True)
            if token:
                headers = {"authorization": token}
                resp = api_session.get(url, headers=headers, timeout=5)

        if resp.status_code != 200:
            return {
                "invested_value": 0.0, "current_value": 0.0, "overall_pnl": 0.0,
                "overall_pnl_pct": 0.0, "day_pnl": 0.0, "day_pnl_pct": 0.0,
                "holdings_count": 0, "holdings": [], "error": f"HTTP {resp.status_code}"
            }

        data = resp.json()
        result_obj = data.get("result", {})
        
        raw_list = []
        if isinstance(result_obj, list):
            raw_list = result_obj
        elif isinstance(result_obj, dict):
            rms_holdings = result_obj.get("RMSHoldings", result_obj.get("Holdings", {}))
            if isinstance(rms_holdings, list):
                raw_list = rms_holdings
            elif isinstance(rms_holdings, dict):
                raw_list = rms_holdings.get("HoldingsList", rms_holdings.get("holdingList", rms_holdings.get("HoldingList", []))) or []
                if not raw_list and rms_holdings:
                    raw_list = [v for v in rms_holdings.values() if isinstance(v, dict)]

        batch_pairs = []
        for h in raw_list:
            if not isinstance(h, dict):
                continue
            iid = int(h.get("ExchangeInstrumentId") or h.get("ExchangeInstrumentID") or h.get("InstrumentId") or 0)
            seg = h.get("ExchangeSegment", "NSECM")
            if iid > 0:
                batch_pairs.append((iid, seg))
        
        live_prices_map = get_live_prices_batch(batch_pairs) if batch_pairs else {}

        holdings_list = []
        tot_invested = 0.0
        tot_current = 0.0
        tot_day_pnl = 0.0

        for h in raw_list:
            if not isinstance(h, dict):
                continue
            sym = str(h.get("TradingSymbol") or "")
            isin = str(h.get("ISIN") or "")
            qty = int(h.get("HoldingQuantity", 0) or h.get("Quantity", 0) or 0)
            buy_avg = float(h.get("BuyAveragePrice", 0.0) or h.get("Price", 0.0) or 0.0)
            inst_id = int(h.get("ExchangeInstrumentId") or h.get("ExchangeInstrumentID") or h.get("InstrumentId") or 0)
            seg = str(h.get("ExchangeSegment") or "NSECM")
            
            ltp = live_prices_map.get(inst_id)
            if not ltp or ltp <= 0:
                ltp = float(h.get("LastTradedPrice", 0.0) or h.get("LTP", 0.0) or buy_avg)
            
            close_price = float(h.get("ClosePrice", 0.0) or h.get("PreviousClose", 0.0) or ltp)
            
            invested = buy_avg * qty
            current_val = ltp * qty
            pnl = current_val - invested
            pnl_pct = round((pnl / invested * 100), 2) if invested > 0 else 0.0
            day_pnl = (ltp - close_price) * qty
            
            tot_invested += invested
            tot_current += current_val
            tot_day_pnl += day_pnl

            holdings_list.append({
                "symbol": sym,
                "isin": isin,
                "quantity": qty,
                "buy_avg": round(buy_avg, 2),
                "ltp": round(ltp, 2),
                "invested": round(invested, 2),
                "current_value": round(current_val, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": pnl_pct,
                "segment": seg
            })

        overall_pnl = tot_current - tot_invested
        overall_pct = round((overall_pnl / tot_invested * 100), 2) if tot_invested > 0 else 0.0
        day_pnl_pct = round((tot_day_pnl / tot_invested * 100), 2) if tot_invested > 0 else 0.0

        return {
            "invested_value": round(tot_invested, 2),
            "current_value": round(tot_current, 2),
            "overall_pnl": round(overall_pnl, 2),
            "overall_pnl_pct": overall_pct,
            "day_pnl": round(tot_day_pnl, 2),
            "day_pnl_pct": day_pnl_pct,
            "holdings_count": len(holdings_list),
            "holdings": holdings_list
        }
    except Exception as e:
        logger.error(f"Error fetching holdings telemetry: {e}")
        return {
            "invested_value": 0.0, "current_value": 0.0, "overall_pnl": 0.0,
            "overall_pnl_pct": 0.0, "day_pnl": 0.0, "day_pnl_pct": 0.0,
            "holdings_count": 0, "holdings": [], "error": str(e)
        }

def get_broker_orders():
    """Fetches real-time broker order book normalized into standard format."""
    token = get_interactive_token()
    if not token:
        return []
    safe_url = get_safe_base_url()
    headers = {"authorization": token}
    try:
        resp = api_session.get(f"{safe_url}/orders", headers=headers, timeout=5)
        if resp.status_code == 200:
            raw_orders = resp.json().get("result", []) or []
            normalized = []
            for o in raw_orders:
                if not isinstance(o, dict):
                    continue
                raw_time = str(o.get("OrderGeneratedDateTime", "") or o.get("OrderUpdateDateTime", ""))
                status_raw = str(o.get("OrderStatus", "UNKNOWN")).upper()
                reject_reason = str(o.get("CancelRejectReason") or o.get("OrderRejectReason") or o.get("RejectReason") or o.get("Text") or "")
                normalized.append({
                    "app_order_id": str(o.get("AppOrderID", o.get("OrderID", ""))),
                    "order_time": raw_time,
                    "action": str(o.get("OrderSide", o.get("OrderTransactionType", ""))).upper(),
                    "symbol": str(o.get("TradingSymbol", "")),
                    "order_qty": int(o.get("OrderQuantity", 0) or 0),
                    "traded_qty": int(o.get("CumulativeQuantity", o.get("OrderDisclosedQuantity", o.get("LeavesQuantity", 0))) or 0),
                    "price": float(o.get("OrderPrice", o.get("OrderAverageTradedPrice", o.get("LimitPrice", 0.0))) or 0.0),
                    "status": status_raw,
                    "segment": str(o.get("ExchangeSegment", "MCXFO")),
                    "product": str(o.get("ProductType", "NRML")),
                    "order_type": str(o.get("OrderType", "LIMIT")),
                    "reject_reason": reject_reason,
                    "order_ref": str(o.get("OrderUniqueIdentifier", ""))
                })
            return normalized
    except Exception as e:
        logger.error(f"Error fetching broker orders: {e}")
    return []

def get_broker_trades():
    """Fetches real-time broker trade book."""
    token = get_interactive_token()
    if not token:
        return []
    safe_url = get_safe_base_url()
    headers = {"authorization": token}
    for endpoint in [f"{safe_url}/orders/trades", f"{safe_url}/trades", f"{safe_url}/reports/trades"]:
        try:
            resp = api_session.get(endpoint, headers=headers, timeout=5)
            if resp.status_code == 200:
                raw_trades = resp.json().get("result", []) or []
                if isinstance(raw_trades, list):
                    normalized_trades = []
                    for t in raw_trades:
                        if not isinstance(t, dict):
                            continue
                        normalized_trades.append({
                            "trade_id": str(t.get("TradeID", t.get("ExecutionID", ""))),
                            "order_id": str(t.get("AppOrderID", t.get("OrderID", ""))),
                            "trade_time": str(t.get("TradeGeneratedDateTime", t.get("TradeExecutionTime", ""))),
                            "symbol": str(t.get("TradingSymbol", "")),
                            "qty": int(t.get("TradedQuantity", t.get("Quantity", 0)) or 0),
                            "price": float(t.get("TradePrice", t.get("Price", 0.0)) or 0.0),
                            "action": str(t.get("OrderSide", t.get("OrderTransactionType", ""))).upper(),
                            "segment": str(t.get("ExchangeSegment", "MCXFO")),
                            "product": str(t.get("ProductType", "NRML"))
                        })
                    return normalized_trades
        except Exception as e:
            logger.error(f"Error fetching broker trades from {endpoint}: {e}")
    return []

def panic_square_off_all():
    """Emergency Kill-Switch: Cancels all pending orders and squares off all open positions."""
    if getattr(config, "PAPER_TRADE_MODE", False):
        logger.critical("🚨 PAPER TRADE PANIC: Simulated square-off triggered.")
        return {"status": "success", "mode": "PAPER", "squared_off": []}

    token = get_interactive_token()
    if not token:
        return {"status": "error", "message": "Auth failed"}

    client_id = getattr(config, "CLIENT_ID", "").strip()
    safe_url = get_safe_base_url()
    headers = {"authorization": token, "Content-Type": "application/json"}
    results = []

    # 1. Cancel all open pending orders
    try:
        ord_url = f"{safe_url}/orders"
        o_resp = api_session.get(ord_url, headers=headers, timeout=5)
        orders = o_resp.json().get("result", [])
        for ord_item in orders:
            if isinstance(ord_item, dict):
                st = ord_item.get("OrderStatus")
                app_id = ord_item.get("AppOrderID")
                if st in ("Open", "New", "Pending", "PartiallyFilled") and app_id:
                    cancel_url = f"{safe_url}/orders?appOrderID={app_id}&clientID={client_id}"
                    api_session.delete(cancel_url, json={"appOrderID": app_id, "clientID": client_id}, headers=headers, timeout=4)
                    logger.critical(f"🚨 CANCELLED OPEN ORDER: AppOrderID {app_id}")
    except Exception as e:
        logger.error(f"Order cancellation sweep error: {e}")

    # 2. Fetch and square off all open positions (NetWise)
    try:
        pos_url = f"{safe_url}/portfolio/positions?dayOrNet=NetWise"
        resp = api_session.get(pos_url, headers=headers, timeout=5)
        if resp.status_code in (400, 401, 403):
            clear_tokens()
            token = get_interactive_token(force_refresh=True)
            if token:
                headers = {"authorization": token, "Content-Type": "application/json"}
                resp = api_session.get(pos_url, headers=headers, timeout=5)

        positions = resp.json().get("result", {}).get("positionList", [])

        for p in positions:
            qty = int(p.get("Quantity", 0))
            if qty == 0: continue
            
            inst_id = int(p.get("ExchangeInstrumentId", 0))
            exch_seg = p.get("ExchangeSegment", "MCXFO")
            prod_type = p.get("ProductType", "NRML")
            sym = p.get("TradingSymbol", "")

            action = "SELL" if qty > 0 else "BUY"
            square_qty = abs(qty)

            # Direct instrument master lookup by instrument ID
            inst_meta = get_instrument_by_id(inst_id)
            if inst_meta:
                tick_size = inst_meta["tick_size"]
                freeze_limit = inst_meta["freeze_qty"]
                exch_seg = inst_meta["exch_seg"]
            else:
                clean_lookup = re.sub(r'[\s\-]+', '', sym)
                _, _, _, inst_tick, _, inst_freeze, _ = get_dynamic_contract_info(clean_lookup)
                tick_size = inst_tick or 0.05
                freeze_limit = inst_freeze or getattr(config, "DEFAULT_FREEZE_QTY_IF_UNKNOWN", 100000)

            live_price = get_live_price(inst_id, exch_seg)
            if not live_price or live_price == "TOKEN_EXPIRED":
                if action == "BUY": # Closing a short position
                    live_price = float(p.get("SellAveragePrice", 0) or p.get("ActualSellAveragePrice", 0) or p.get("LastTradedPrice", 0) or p.get("LTP", 0) or p.get("BuyAveragePrice", 0) or 100)
                else: # Closing a long position
                    live_price = float(p.get("BuyAveragePrice", 0) or p.get("ActualBuyAveragePrice", 0) or p.get("LastTradedPrice", 0) or p.get("LTP", 0) or p.get("SellAveragePrice", 0) or 100)

            buffer = max(live_price * 0.01, tick_size * 10)
            raw_limit = (live_price + buffer) if action == "BUY" else (live_price - buffer)
            exec_price = apply_tick_size(raw_limit, tick_size, action)

            # Slicing chunk for freeze limits
            remaining_qty = square_qty
            while remaining_qty > 0:
                chunk_qty = min(remaining_qty, freeze_limit)
                order_ref = f"PANIC_{int(time.time()*1000)}"
                order_url = f"{safe_url}/orders"
                payload = {
                    "exchangeSegment": exch_seg,
                    "exchangeInstrumentID": inst_id,
                    "productType": prod_type,
                    "orderType": "LIMIT",
                    "orderSide": action,
                    "timeInForce": "DAY",
                    "disclosedQuantity": 0,
                    "orderQuantity": chunk_qty,
                    "limitPrice": exec_price,
                    "stopPrice": 0,
                    "apiOrderSource": "WEBAPI",
                    "orderUniqueIdentifier": order_ref,
                    "clientID": client_id,
                }
                try:
                    resp_post = api_session.post(order_url, headers=headers, json=payload, timeout=8)
                    try:
                        res = resp_post.json()
                    except Exception:
                        res = {"type": "error", "description": f"HTTP {resp_post.status_code}: {resp_post.text[:100]}"}

                    if res.get('type') == 'error' and any(kw in str(res).lower() for kw in ('token', 'session', 'auth')):
                        clear_tokens()
                        token = get_interactive_token(force_refresh=True)
                        if token:
                            headers = {"authorization": token, "Content-Type": "application/json"}
                            resp_post = api_session.post(order_url, headers=headers, json=payload, timeout=8)
                            try:
                                res = resp_post.json()
                            except Exception:
                                res = {"type": "error", "description": f"HTTP {resp_post.status_code}: {resp_post.text[:100]}"}
                except Exception as post_err:
                    res = {"type": "error", "description": f"Dispatch failed: {post_err}"}

                results.append({"symbol": sym, "action": action, "qty": chunk_qty, "result": res})
                logger.critical(f"🚨 PANIC SQUARE OFF EXECUTED: {action} {chunk_qty} of {sym} -> {res}")
                remaining_qty -= chunk_qty
    except Exception as e:
        logger.error(f"Panic square off error: {e}")
        return {"status": "error", "error": str(e)}

    return {"status": "success", "squared_off": results}
