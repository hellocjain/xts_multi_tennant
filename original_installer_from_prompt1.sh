<USER_REQUEST>
#!/bin/bash
# =====================================================================
# 🚀 XTS V10.0-ENTERPRISE HARDENED MCX ALGO BOT ONE-SHOT AUTO-INSTALLER
# Broker: AC Agarwal (Symphony XTS API)
# Exchange Segments: MCX Commodities & NSE/BSE Derivatives
# Optimizations: Zero-Lag Keepalive, Marketable Limit, 3-Day Auto-Rollover,
# Emergency Kill-Switch, SQLite WAL Recovery, and Full CLI Suite
# =====================================================================
set -e

PROJECT_DIR="/opt/xts_bot"
USER_NAME="${SUDO_USER:-$(whoami)}"

echo "==========================================================="
echo "   🚀 XTS V10.0-ENTERPRISE ALGO BOT ONE-SHOT INSTALLER     "
echo "==========================================================="

# 1. System Dependencies & Port Freeing
echo "[1/7] Updating system and installing required packages..."
sudo systemctl stop apache2 nginx 2>/dev/null || true

sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv curl nano debian-keyring debian-archive-keyring apt-transport-https

# Configure UFW firewall
if command -v ufw >/dev/null 2>&1; then
    echo "Configuring UFW firewall for HTTP/HTTPS..."
    sudo ufw allow 80/tcp >/dev/null 2>&1 || true
    sudo ufw allow 443/tcp >/dev/null 2>&1 || true
fi

# Install Caddy for Reverse Proxy & Automatic SSL
if ! command -v caddy &> /dev/null; then
    echo "Installing Caddy Web Server..."
    sudo mkdir -p /usr/share/keyrings
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update -y
    sudo apt-get install caddy -y
fi

# 2. Application Directory Setup
echo "[2/7] Preparing application directory at $PROJECT_DIR..."
sudo mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# 3. Interactive Credentials & Configuration Wizard
echo ""
echo "=== 3. BROKER CREDENTIALS & SECURITY SETUP ==="
if [ -f "$PROJECT_DIR/config.py" ]; then
    echo "⚠️ config.py already exists. Skipping credentials wizard."
else
    read -p "Enter Interactive API Key: " API_KEY
    read -s -p "Enter Interactive API Secret: " API_SECRET; echo
    read -p "Enter Market Data API Key: " MD_API_KEY
    read -s -p "Enter Market Data API Secret: " MD_API_SECRET; echo
    read -p "Enter Broker Client ID (e.g. ABK01): " CLIENT_ID

    while [ -z "$CLIENT_ID" ]; do
        echo "❌ Client ID cannot be empty!"
        read -p "Enter Broker Client ID: " CLIENT_ID
    done

    echo ""
    echo "--- SECURITY PERIMETER ---"
    read -s -p "Create a Webhook Secret Password (e.g. MySuperSecretBot): " WEBHOOK_SECRET; echo
    while [ -z "$WEBHOOK_SECRET" ]; do
        echo "❌ Webhook secret cannot be empty!"
        read -s -p "Create a Webhook Secret Password: " WEBHOOK_SECRET; echo
    done

    echo ""
    echo "--- XTS API ENDPOINT ---"
    read -p "Enter XTS Interactive API Base URL [Default: https://symphony.acagarwal.com:3000/interactive]: " XTS_BASE_URL
    XTS_BASE_URL=${XTS_BASE_URL:-"https://symphony.acagarwal.com:3000/interactive"}

    echo ""
    echo "--- TRADING MODE ---"
    read -p "Start in Paper Trading Mode? (y/n) [Default: y]: " PAPER_MODE_CHOICE
    PAPER_MODE_CHOICE=${PAPER_MODE_CHOICE:-y}
    if [[ "$PAPER_MODE_CHOICE" =~ ^[Yy]$ ]]; then
        PAPER_MODE_BOOL="True"
        echo "📝 Bot configured in PAPER TRADING mode (Zero Risk)."
    else
        PAPER_MODE_BOOL="False"
        echo "🔥 Bot configured in LIVE REAL-MONEY mode."
    fi

    export BOT_API_KEY="$API_KEY"
    export BOT_API_SECRET="$API_SECRET"
    export BOT_MD_API_KEY="$MD_API_KEY"
    export BOT_MD_API_SECRET="$MD_API_SECRET"
    export BOT_CLIENT_ID="$CLIENT_ID"
    export BOT_WEBHOOK_SECRET="$WEBHOOK_SECRET"
    export BOT_XTS_API_BASE_URL="$XTS_BASE_URL"
    export BOT_PAPER_MODE="$PAPER_MODE_BOOL"

    python3 - << 'PYEOF'
import os, json

fields = {
    "API_KEY": os.environ.get("BOT_API_KEY", ""),
    "API_SECRET": os.environ.get("BOT_API_SECRET", ""),
    "MD_API_KEY": os.environ.get("BOT_MD_API_KEY", ""),
    "MD_API_SECRET": os.environ.get("BOT_MD_API_SECRET", ""),
    "CLIENT_ID": os.environ.get("BOT_CLIENT_ID", ""),
    "WEBHOOK_SECRET": os.environ.get("BOT_WEBHOOK_SECRET", ""),
    "XTS_API_BASE_URL": os.environ.get("BOT_XTS_API_BASE_URL", ""),
}
paper_mode = os.environ.get("BOT_PAPER_MODE", "True") == "True"

with open("config.py", "w") as f:
    f.write("# =====================================================================\n")
    f.write("# V10.0-ENTERPRISE HARDENED CONFIGURATION\n")
    f.write("# =====================================================================\n\n")
    for k, v in fields.items():
        f.write(f"{k} = {json.dumps(v)}\n")
    
    f.write(f"""
# ================= REAL MONEY EXECUTION RULES =================
ORDER_TYPE = "LIMIT"                    # SEBI/Algo Compliant Marketable Limit Order
SLIPPAGE_BUFFER_PCT = 0.005             # 0.5% buffer ensures instant execution at best market ask/bid
TIME_IN_FORCE = "DAY"                   # Sweeps market depth

# ---- SUPERTREND EXECUTION GUARANTEE ----
ENFORCE_PRICE_DEVIATION_CHECK = False   # Never drop signals due to price slippage/volatility
ALLOW_TRADE_WITHOUT_LIVE_PRICE = True   # Fallback to TV price for risk cap if MD feed lags
MAX_PRICE_DEVIATION_PCT = 0.05          # 5% safety ceiling if manually re-enabled

# ---- 3-DAY AUTO-ROLLOVER SHIELD ----
MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX = 3    # Auto-rolls to next month contract if <= 3 days left
MIN_DAYS_BEFORE_EXPIRY_DERIVATIVES = 0  

# ---- PARTIAL FILL GUARD ----
CANCEL_LINGERING_PARTIAL_FILLS = True   # Auto-cancels any unfilled remainder after dispatch
PARTIAL_FILL_TIMEOUT_SECONDS = 2        # Seconds to wait before killing unfilled remainder

# ---- SIZING & RISK SHIELD ----
TV_SENDS_LOTS = True                    # TradingView quantity sent as Lots (1 = 1 lot)
MAX_LOTS_LIMIT = 100                    # Max Lots per single order
MAX_UNITS_LIMIT = 100000                # Max Raw Units (Allows high-multiplier commodities like NatGas)
MAX_ORDER_VALUE_INR = 5000000           # Max single order value (Rs 50 Lakhs)
DAILY_NOTIONAL_CAP_INR = 10000000       # Daily cumulative traded value cap (Rs 1 Crore)
DEDUP_WINDOW_SECONDS = 3                # Suppress exact duplicate signals within 3 seconds

TV_TO_XTS_MAP = {{}}
ALLOW_PREFIX_FALLBACK = False  
MAX_SYMBOL_LENGTH = 35

# ---- CONCURRENCY & RECOVERY ----
DEFAULT_FREEZE_QTY_IF_UNKNOWN = 100000
TOKEN_REFRESH_WAIT_TIMEOUT = 8
CACHE_WATCHDOG_INTERVAL_SECONDS = 30
TOKEN_KEEPALIVE_INTERVAL_SECONDS = 240   # Pings broker every 4 minutes to keep session permanently hot
MAX_TRACKED_FAILED_IPS = 2000
STALE_SIGNAL_WINDOW_SECONDS = 30         
BACKGROUND_THREAD_POOL_SIZE = 100        
MAX_WEBHOOK_BODY_BYTES = 10000
OPS_ALERT_WEBHOOK_URL = ""

# ================= PAPER TRADING =================
PAPER_TRADE_MODE = {paper_mode}
LOG_PAPER_TRADES_TO_FILE = True
""")
PYEOF
    echo "✅ config.py generated successfully."
fi

# 4. Domain & Caddy Reverse Proxy Setup
echo ""
echo "=== 4. DOMAIN & SSL CONFIGURATION ==="
read -p "Enter your domain for HTTPS (e.g., bot.yourdomain.com) [Press Enter to skip & use HTTP IP]: " DOMAIN_NAME

if [ -z "$DOMAIN_NAME" ]; then
    echo "⚠️ WARNING: Skipping domain. Webhook will run on HTTP (Port 80)."
    sudo tee /etc/caddy/Caddyfile > /dev/null << EOF
:80 {
    reverse_proxy 127.0.0.1:8000
}
EOF
else
    sudo tee /etc/caddy/Caddyfile > /dev/null << EOF
$DOMAIN_NAME {
    reverse_proxy 127.0.0.1:8000
}
EOF
fi

# 5. Virtual Environment Setup
echo ""
echo "[5/7] Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn requests anyio

# 6. Generate Core Application Files
echo "[6/7] Writing core application files (xts_api.py & main.py)..."

# ======== INJECT XTS_API.PY ========
cat << 'EOF' > xts_api.py
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import datetime
import logging
import config
import re
import json
import time
import os
import threading
import fcntl
import calendar
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

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

SEGMENT_MAP = {"NSECM": 1, "NSEFO": 2, "NSECD": 3, "BSECM": 11, "BSEFO": 12, "BSECD": 13, "MCXFO": 51, "NCDEX": 21}

PREFIX_STRIPPER = re.compile(r'^(MCXFO|MCX|NSECD|NSEFO|NSE|BSEFO|BSECD|BSE|NCDEX|MSEI|CDS):', re.IGNORECASE)
SUFFIX_STRIPPER = re.compile(r'(\.NS|\.BO|[1-9]!|FUT)$', re.IGNORECASE)
ALPHANUM_ONLY = re.compile(r'[^A-Z0-9]')

FUT_MONTH_CODES = {'F': 1, 'G': 2, 'H': 3, 'J': 4, 'K': 5, 'M': 6, 'N': 7, 'Q': 8, 'U': 9, 'V': 10, 'X': 11, 'Z': 12}
MONTH_ABBR_TO_NUM = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
NUM_TO_MONTH_ABBR = {v: k for k, v in MONTH_ABBR_TO_NUM.items()}

CONTINUOUS_SUFFIX = re.compile(r'([1-9])!$', re.IGNORECASE)
DERIVATIVE_PREFIX = re.compile(r'^(MCXFO|MCX|NSECD|NSEFO|NCDEX|BSEFO|BSECD|MSEI|CDS):', re.IGNORECASE)

COMMON_ALIASES = {
    "NIFTY50": "NIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "NATGAS": "NATURALGAS",
    "NATGASMINI": "NATURALGASM",
    "CRUDEOILMINI": "CRUDEOILM",
    "GOLDMINI": "GOLDM",
    "SILVERMINI": "SILVERM",
    "SILVERMICRO": "SILVERMIC",
    "ALUMINIUM": "ALUMINI",
    "ALUMINIUMMINI": "ALUMINI",
    "ZINCMINI": "ZINC",
    "LEADMINI": "LEAD",
}

NO_EXPIRY = datetime.date.max
SAFE_BASE_URL = getattr(config, "XTS_API_BASE_URL", "").rstrip('/')

def send_ops_alert(message):
    url = getattr(config, "OPS_ALERT_WEBHOOK_URL", "")
    if not url:
        return
    try:
        api_session.post(url, json={"text": message}, timeout=3)
    except Exception:
        pass

def clear_tokens():
    global INTERACTIVE_TOKEN, MARKET_DATA_TOKEN
    with INTERACTIVE_REFRESH_CV:
        INTERACTIVE_TOKEN = None
    with MD_REFRESH_CV:
        MARKET_DATA_TOKEN = None
    logger.info("Session tokens cleared.")

def get_interactive_token():
    global INTERACTIVE_TOKEN, REFRESHING_INTERACTIVE
    wait_timeout = getattr(config, "TOKEN_REFRESH_WAIT_TIMEOUT", 8)

    with INTERACTIVE_REFRESH_CV:
        if INTERACTIVE_TOKEN:
            return INTERACTIVE_TOKEN
        if REFRESHING_INTERACTIVE:
            got_it = INTERACTIVE_REFRESH_CV.wait_for(
                lambda: (not REFRESHING_INTERACTIVE), timeout=wait_timeout)
            if INTERACTIVE_TOKEN:
                return INTERACTIVE_TOKEN
            if not got_it:
                logger.error("Timed out waiting for concurrent interactive-token refresh.")
                return None
        REFRESHING_INTERACTIVE = True

    try:
        url = f"{SAFE_BASE_URL}/user/session"
        payload = {"appKey": config.API_KEY, "secretKey": config.API_SECRET, "source": "WEBAPI"}
        response = api_session.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get('type') == 'success':
            with INTERACTIVE_REFRESH_CV:
                INTERACTIVE_TOKEN = data['result']['token']
            return INTERACTIVE_TOKEN
        else:
            logger.error(f"Interactive login rejected: {data}")
    except Exception as e:
        logger.error(f"Interactive Login error: {e}")
    finally:
        with INTERACTIVE_REFRESH_CV:
            REFRESHING_INTERACTIVE = False
            INTERACTIVE_REFRESH_CV.notify_all()
    return None

def get_marketdata_token():
    global MARKET_DATA_TOKEN, VALID_MD_BASE_URL, REFRESHING_MD
    wait_timeout = getattr(config, "TOKEN_REFRESH_WAIT_TIMEOUT", 8)

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
        md_key = getattr(config, "MD_API_KEY", config.API_KEY)
        md_secret = getattr(config, "MD_API_SECRET", config.API_SECRET)
        payload = {"appKey": md_key, "secretKey": md_secret, "source": "WEBAPI"}

        base_urls = [
            SAFE_BASE_URL.replace('/interactive', '/apimarketdata'),
            SAFE_BASE_URL.replace('/interactive', '/marketdata'),
            SAFE_BASE_URL.replace('/interactive', '/apibinarymarketdata'),
        ]

        for base_md in base_urls:
            for url in [f"{base_md.rstrip('/')}/auth/login", f"{base_md.rstrip('/')}/user/session"]:
                try:
                    response = api_session.post(url, json=payload, timeout=3)
                    if response.status_code == 200 and response.json().get('type') == 'success':
                        with MD_REFRESH_CV:
                            MARKET_DATA_TOKEN = response.json()['result']['token']
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
    """Proactively keeps broker sessions hot by heartbeating every 4 minutes."""
    interval = getattr(config, "TOKEN_KEEPALIVE_INTERVAL_SECONDS", 240)
    def _heartbeat():
        while True:
            time.sleep(interval)
            try:
                t_int = get_interactive_token()
                t_md, _ = get_marketdata_token()
                if t_int:
                    # Lightweight ping to keep session alive
                    api_session.get(f"{SAFE_BASE_URL}/portfolio/positions?dayOrNet=DayWise", 
                                    headers={"authorization": t_int}, timeout=4)
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
                
                # 3-Day Intelligent Auto-Rollover Filter
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

    # Specific Expiry Search Logic
    clean_sym = PREFIX_STRIPPER.sub('', symbol.upper().strip())
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

_DAILY_RISK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_risk_state.json")

def _today_ist_str():
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.now(IST).date().isoformat()

def check_and_reserve_daily_notional(order_val):
    cap = getattr(config, "DAILY_NOTIONAL_CAP_INR", 10000000)
    if cap is None or cap <= 0:
        return True, None
        
    today_str = _today_ist_str()
    
    fd = os.open(_DAILY_RISK_FILE, os.O_RDWR | os.O_CREAT, 0o666)
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
    cap = getattr(config, "DAILY_NOTIONAL_CAP_INR", 10000000)
    if cap is None or cap <= 0:
        return
        
    today_str = _today_ist_str()
    
    fd = os.open(_DAILY_RISK_FILE, os.O_RDWR | os.O_CREAT, 0o666)
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

_PAPER_TRADE_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.log")

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
        with open(_PAPER_TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.error(f"Failed to write paper trade log entry: {e}")

def check_order_status_by_ref(order_ref):
    token = get_interactive_token()
    if not token:
        return "NETWORK_ERROR"
    url = f"{SAFE_BASE_URL}/reports/orders"
    headers = {"authorization": token}
    try:
        response = api_session.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('type') == 'success':
                for ord in data.get('result', []):
                    ref_tag = ord.get('OrderUniqueIdentifier') or ord.get('orderUniqueIdentifier')
                    if ref_tag and str(ref_tag) == str(order_ref):
                        return ord.get('OrderStatus')
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

        # Optimized zero-sleep instant re-authentication
        if is_auth_issue and attempt == 1:
            logger.warning("Session expired. Auto-healing authentication immediately...")
            clear_tokens()
            return execute_trade_with_retry(action, symbol, quantity, tv_price, order_ref, attempt=2)

    if result.get("status") == "error" or result.get("type") == "error":
        send_ops_alert(f"XTS bot: order FAILED for {symbol} ({action} x{quantity}) ref={order_ref}: {result}")

    return result

def _monitor_and_clean_partial_fills(order_ref, client_id, token):
    timeout_sec = getattr(config, "PARTIAL_FILL_TIMEOUT_SECONDS", 2)
    time.sleep(timeout_sec)
    
    url = f"{SAFE_BASE_URL}/reports/orders"
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
                            cancel_url = f"{SAFE_BASE_URL}/orders?appOrderID={app_order_id}&clientID={client_id}"
                            api_session.delete(cancel_url, json={"appOrderID": app_order_id, "clientID": client_id}, headers=headers, timeout=5)
                            return
    except Exception as e:
        logger.error(f"Partial fill monitor error: {e}")

def place_order(action, symbol, quantity, tv_price, order_ref):
    token = get_interactive_token()
    if not token:
        return {"status": "error", "message": "Auth failed"}

    instrument_id, exch_seg, prod_type, tick_size, lot_size, freeze_qty, contract_expiry = get_dynamic_contract_info(symbol)
    if not instrument_id:
        return {"status": "error", "message": f"Instrument resolution failed for {symbol}"}

    # Lot Multiplier Calculation
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

    # Base price calculation for risk shields & marketable limit order
    live_price = get_live_price(instrument_id, exch_seg)
    base_price = live_price if (live_price and live_price != "TOKEN_EXPIRED") else tv_price

    if base_price <= 0:
        base_price = tv_price if tv_price > 0 else 1.0

    # Calculate Marketable Limit Price (SEBI/XTS Algo Compliant)
    buffer_pct = getattr(config, "SLIPPAGE_BUFFER_PCT", 0.005)
    buffer = max(base_price * buffer_pct, tick_size * 5)
    raw_limit_price = (base_price + buffer) if action == "BUY" else (base_price - buffer)
    execution_price = apply_tick_size(raw_limit_price, tick_size, action)

    order_val = base_price * execution_qty
    max_val = getattr(config, "MAX_ORDER_VALUE_INR", 5000000)
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

    # SEBI/XTS ALGO COMPLIANT MARKETABLE LIMIT ORDER PAYLOAD
    time_in_force = getattr(config, "TIME_IN_FORCE", "DAY")
    url = f"{SAFE_BASE_URL}/orders"
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

    # ================= PAPER TRADE INTERCEPT =================
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
        print(log_line)
        logger.info(log_line)

        _log_paper_trade_to_file(
            action, symbol, execution_qty, instrument_id, exch_seg,
            base_price, order_ref, order_val, paper_order_id,
        )
        return simulated_response
    # =========================================================

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

def panic_square_off_all():
    """Emergency Kill-Switch: Cancels all pending orders and squares off all open positions."""
    token = get_interactive_token()
    if not token:
        return {"status": "error", "message": "Auth failed"}

    client_id = getattr(config, "CLIENT_ID", "").strip()
    headers = {"authorization": token, "Content-Type": "application/json"}
    results = []

    # 1. Cancel all open pending orders
    try:
        ord_url = f"{SAFE_BASE_URL}/reports/orders"
        o_resp = api_session.get(ord_url, headers=headers, timeout=5)
        orders = o_resp.json().get("result", [])
        for ord_item in orders:
            if isinstance(ord_item, dict):
                st = ord_item.get("OrderStatus")
                app_id = ord_item.get("AppOrderID")
                if st in ("Open", "New", "Pending", "PartiallyFilled") and app_id:
                    cancel_url = f"{SAFE_BASE_URL}/orders?appOrderID={app_id}&clientID={client_id}"
                    api_session.delete(cancel_url, json={"appOrderID": app_id, "clientID": client_id}, headers=headers, timeout=4)
                    logger.critical(f"🚨 CANCELLED OPEN ORDER: AppOrderID {app_id}")
    except Exception as e:
        logger.error(f"Order cancellation sweep error: {e}")

    # 2. Fetch and square off all open positions
    try:
        pos_url = f"{SAFE_BASE_URL}/portfolio/positions?dayOrNet=DayWise"
        resp = api_session.get(pos_url, headers=headers, timeout=5)
        positions = resp.json().get("result", {}).get("positionList", [])

        for p in positions:
            qty = int(p.get("Quantity", 0))
            if qty == 0: continue
            
            inst_id = int(p.get("ExchangeInstrumentId", 0))
            exch_seg = p.get("ExchangeSegment", "MCXFO")
            prod_type = p.get("ProductType", "NRML")
            sym = p.get("TradingSymbol", "")

            # Opposite action to square off
            action = "SELL" if qty > 0 else "BUY"
            square_qty = abs(qty)

            live_price = get_live_price(inst_id, exch_seg) or float(p.get("BuyAveragePrice", 100))
            tick_size = 0.05
            buffer = max(live_price * 0.01, tick_size * 10)
            raw_limit = (live_price + buffer) if action == "BUY" else (live_price - buffer)
            exec_price = apply_tick_size(raw_limit, tick_size, action)

            order_ref = f"PANIC_{int(time.time()*1000)}"
            order_url = f"{SAFE_BASE_URL}/orders"
            payload = {
                "exchangeSegment": exch_seg,
                "exchangeInstrumentID": inst_id,
                "productType": prod_type,
                "orderType": "LIMIT",
                "orderSide": action,
                "timeInForce": "DAY",
                "disclosedQuantity": 0,
                "orderQuantity": square_qty,
                "limitPrice": exec_price,
                "stopPrice": 0,
                "apiOrderSource": "WEBAPI",
                "orderUniqueIdentifier": order_ref,
                "clientID": client_id,
            }
            res = api_session.post(order_url, headers=headers, json=payload, timeout=5).json()
            results.append({"symbol": sym, "action": action, "qty": square_qty, "result": res})
            logger.critical(f"🚨 PANIC SQUARE OFF EXECUTED: {action} {square_qty} of {sym} -> {res}")
    except Exception as e:
        logger.error(f"Panic square off error: {e}")
        return {"status": "error", "error": str(e)}

    return {"status": "success", "squared_off": results}
EOF

# ======== INJECT MAIN.PY ========
cat << 'EOF' > main.py
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager, closing
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import logging
import time
import hashlib
import hmac
import math
import json
import os
import sqlite3
import threading
import uuid
import anyio

import config
import xts_api

# Sanitize string variables
for _key in ("WEBHOOK_SECRET", "CLIENT_ID", "API_KEY", "API_SECRET", "MD_API_KEY", "MD_API_SECRET", "XTS_API_BASE_URL"):
    if hasattr(config, _key):
        setattr(config, _key, str(getattr(config, _key)).strip())

if not getattr(config, "WEBHOOK_SECRET", ""):
    raise RuntimeError("CRITICAL SECURITY ERROR: WEBHOOK_SECRET is unconfigured in config.py.")

if not getattr(config, "CLIENT_ID", ""):
    raise RuntimeError("CRITICAL SECURITY ERROR: CLIENT_ID is unconfigured in config.py.")

if int(os.environ.get("WEB_CONCURRENCY", 1)) > 1 or int(os.environ.get("WORKERS", 1)) > 1:
    raise RuntimeError("CRITICAL: Bot relies on global memory for master contract caching. You must use --workers 1.")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

FAILED_ATTEMPTS = OrderedDict()
MAX_TRACKED_IPS = getattr(config, "MAX_TRACKED_FAILED_IPS", 2000)
FAILED_ATTEMPTS_LOCK = threading.Lock()

def _record_failed_attempt(client_ip, now):
    with FAILED_ATTEMPTS_LOCK:
        attempts = FAILED_ATTEMPTS.pop(client_ip, [])
        attempts = [ts for ts in attempts if now - ts < 900]
        attempts.append(now)
        FAILED_ATTEMPTS[client_ip] = attempts
        FAILED_ATTEMPTS.move_to_end(client_ip)
        while len(FAILED_ATTEMPTS) > MAX_TRACKED_IPS:
            FAILED_ATTEMPTS.popitem(last=False)

def _clear_failed_attempts(client_ip):
    with FAILED_ATTEMPTS_LOCK:
        FAILED_ATTEMPTS.pop(client_ip, None)

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db")
_DB_LOCK = threading.Lock()

def _db_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def db_init():
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    received_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS signal_dedup (hash TEXT PRIMARY KEY, timestamp REAL)")
            conn.commit()

def db_insert_pending(sig_id, payload_dict):
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            now = time.time()
            conn.execute(
                "INSERT INTO signals (id, received_at, payload, status, result, updated_at) "
                "VALUES (?, ?, ?, 'pending', NULL, ?)",
                (sig_id, now, json.dumps(payload_dict), now),
            )
            conn.commit()

def db_update_status(sig_id, status, result=None):
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            conn.execute(
                "UPDATE signals SET status=?, result=?, updated_at=? WHERE id=?",
                (status, json.dumps(result) if result is not None else None, time.time(), sig_id),
            )
            conn.commit()

def db_fetch_unfinished():
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            return conn.execute(
                "SELECT id, received_at, payload, status FROM signals WHERE status IN ('pending','processing')"
            ).fetchall()

def db_prune_old(max_age_seconds=7 * 24 * 3600):
    with _DB_LOCK:
        with closing(_db_conn()) as conn:
            cutoff = time.time() - max_age_seconds
            conn.execute(
                "DELETE FROM signals WHERE updated_at < ? AND status NOT IN ('pending','processing')",
                (cutoff,),
            )
            conn.commit()

def _dispatch_and_record(sig_id, action, symbol, quantity, price, order_ref):
    db_update_status(sig_id, "processing")
    try:
        result = xts_api.execute_trade_with_retry(action, symbol, quantity, price, order_ref)
        is_paper_trade = bool((result.get("result") or {}).get("IsPaperTrade"))

        if result.get("type") == "success":
            status = "paper_done" if is_paper_trade else "done"
        else:
            status = "failed"

        audit_result = dict(result) if isinstance(result, dict) else {"raw_result": result}
        audit_result["_audit"] = {
            "action": action,
            "symbol": symbol,
            "quantity": quantity,
            "tv_price": price,
            "order_ref": order_ref,
            "is_paper_trade": is_paper_trade,
            "dispatched_at": time.time(),
        }

        db_update_status(sig_id, status, audit_result)
    except Exception as e:
        logger.error(f"UNCAUGHT ERROR dispatching signal {sig_id}: {e}")
        db_update_status(sig_id, "failed", {"error": str(e)})
        xts_api.send_ops_alert(f"XTS bot: uncaught error executing signal {sig_id} ({symbol}): {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("--- SYSTEM BOOT: PRE-LOADING BROKER MASTER FILE ---")
    ok = xts_api.refresh_master_cache()
    if not ok:
        logger.error("--- STARTUP WARNING: master cache failed to load. Watchdog will keep retrying. ---")
    
    xts_api.start_cache_watchdog()
    xts_api.start_token_keepalive()

    try:
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = getattr(config, "BACKGROUND_THREAD_POOL_SIZE", 100)
    except Exception:
        pass

    db_init()
    stale_window = getattr(config, "STALE_SIGNAL_WINDOW_SECONDS", 30)
    unfinished = db_fetch_unfinished()
    
    def process_recovery(unfinished_list):
        now_time = time.time()
        with ThreadPoolExecutor(max_workers=5) as executor:
            for sig_id, received_at, payload_json, status in unfinished_list:
                age = now_time - received_at
                try:
                    data = json.loads(payload_json)
                except Exception as e:
                    db_update_status(sig_id, "abandoned_error", {"error": str(e)})
                    continue

                if age <= stale_window or status == "processing":
                    order_ref = data.get("order_ref")
                    logger.warning(f"RECOVERY: Checking broker order book for {sig_id} (Ref: {order_ref})...")
                    
                    broker_status = xts_api.check_order_status_by_ref(order_ref)
                    
                    if broker_status in ("Open", "New", "Filled", "PartiallyFilled", "Pending", "SUCCESS"):
                        logger.warning(f"RECOVERY SAFEGUARD: Order {order_ref} exists on broker ({broker_status}). Not re-executing.")
                        db_update_status(sig_id, "done", {"status": "recovered_from_broker", "broker_status": broker_status})
                        continue
                    elif broker_status in ("Rejected", "Cancelled", "Canceled", "Expired"):
                        logger.warning(f"RECOVERY SAFEGUARD: Order {order_ref} hit terminal state ({broker_status}). Not re-executing.")
                        db_update_status(sig_id, "failed", {"status": "broker_terminal_state", "broker_status": broker_status})
                        continue
                    elif broker_status == "NETWORK_ERROR":
                        logger.critical(f"RECOVERY SAFEGUARD: Network query failed for {order_ref}. Aborting auto-replay.")
                        db_update_status(sig_id, "failed", {"status": "recovery_network_failure"})
                        continue
                    elif broker_status == "NOT_FOUND" and status == "processing":
                        logger.critical(f"RECOVERY SAFEGUARD: Order {order_ref} was sent to XTS but missing from reports. Aborting replay.")
                        db_update_status(sig_id, "abandoned_manual_review_needed")
                        xts_api.send_ops_alert(f"CRITICAL: Order {order_ref} recovered but missing from XTS. Check terminal.")
                        continue
                        
                    logger.warning(f"RECOVERY: Order missing from broker. Re-queuing {sig_id}.")
                    executor.submit(
                        _dispatch_and_record,
                        sig_id, data["action"], data["symbol"], data["quantity"], 
                        data["price"], data["order_ref"]
                    )
                else:
                    logger.error(f"RECOVERY: signal {sig_id} is {age:.0f}s old — marking abandoned.")
                    db_update_status(sig_id, "abandoned_stale")

    if unfinished:
        logger.warning(f"RECOVERY: found {len(unfinished)} unfinished signal(s). Processing...")
        threading.Thread(target=process_recovery, args=(unfinished,), daemon=True).start()
    
    db_prune_old()
    logger.info("--- SYSTEM READY: PROACTIVE SESSIONS ACTIVE ---")
    yield

app = FastAPI(title="XTS Real-Money SaaS Bot V10.0-PRO", lifespan=lifespan)

MAX_WEBHOOK_BODY_BYTES = getattr(config, "MAX_WEBHOOK_BODY_BYTES", 10_000)

def is_duplicate_signal(action: str, symbol: str, quantity: int, price: float) -> bool:
    now = time.time()
    sig_hash = hashlib.md5(f"{action}_{symbol}_{quantity}".encode()).hexdigest()
    window = getattr(config, "DEDUP_WINDOW_SECONDS", 3)

    try:
        with _DB_LOCK: 
            with closing(_db_conn()) as conn:
                conn.execute("DELETE FROM signal_dedup WHERE timestamp < ?", (now - window,))
                try:
                    conn.execute("INSERT INTO signal_dedup (hash, timestamp) VALUES (?, ?)", (sig_hash, now))
                    conn.commit()
                    return False
                except sqlite3.IntegrityError:
                    return True 
    except Exception as e:
        logger.error(f"DEDUP SHIELD ERROR: {e}. Failing closed to prevent duplicate orders.")
        return True 

@app.get("/health")
async def health():
    unfinished = await anyio.to_thread.run_sync(db_fetch_unfinished)
    return {
        "cache_healthy": xts_api.cache_is_healthy(),
        "futures_loaded": len(xts_api.FUT_MASTER),
        "cash_loaded": len(xts_api.CASH_MASTER),
        "interactive_token_active": bool(xts_api.INTERACTIVE_TOKEN),
        "market_data_token_active": bool(xts_api.MARKET_DATA_TOKEN),
        "unfinished_signals_in_queue": len(unfinished),
        "paper_trade_mode": getattr(config, "PAPER_TRADE_MODE", False),
    }

@app.post("/panic")
async def panic(request: Request):
    """Emergency Kill-Switch API endpoint"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    incoming_secret = str(data.get("secret", "")).strip()
    if not hmac.compare_digest(incoming_secret, getattr(config, "WEBHOOK_SECRET", "")):
        return JSONResponse(status_code=401, content={"status": "error", "message": "Unauthorized"})
    
    result = await anyio.to_thread.run_sync(xts_api.panic_square_off_all)
    return result

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    client_ip = request.client.host or "unknown"
    now = time.time()

    body_bytes = b""
    try:
        with anyio.fail_after(5.0):
            async for chunk in request.stream():
                body_bytes += chunk
                if len(body_bytes) > MAX_WEBHOOK_BODY_BYTES:
                    _record_failed_attempt(client_ip, now)
                    return JSONResponse(status_code=413, content={"status": "error", "message": "Payload too large"})
        
        if not body_bytes:
            raise ValueError("Empty body")
            
        data = json.loads(body_bytes)
        if not isinstance(data, dict):
            raise ValueError("Payload is not a JSON object")
            
    except TimeoutError:
        _record_failed_attempt(client_ip, now)
        return JSONResponse(status_code=408, content={"status": "error", "message": "Payload transfer timed out"})
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid JSON"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

    incoming_secret = str(data.get("secret", "")).strip()
    expected_secret = getattr(config, "WEBHOOK_SECRET", "")
    secret_ok = bool(expected_secret) and hmac.compare_digest(incoming_secret, expected_secret)

    if not secret_ok:
        prior = FAILED_ATTEMPTS.get(client_ip, [])
        prior = [ts for ts in prior if now - ts < 900]
        if len(prior) >= 10:
            logger.warning(f"RATE LIMIT: IP {client_ip} temporarily banned due to brute force.")
            return JSONResponse(status_code=429, content={"status": "error", "message": "Too many failed attempts"})
        _record_failed_attempt(client_ip, now)
        logger.error(f"REJECTED: Unauthorized access attempt from IP {client_ip}.")
        return JSONResponse(status_code=401, content={"status": "error", "message": "Unauthorized"})

    _clear_failed_attempts(client_ip)

    action_raw = str(data.get("action", "")).upper().strip()
    if action_raw in ["BUY", "LONG", "COVER"]:
        action = "BUY"
    elif action_raw in ["SELL", "SHORT", "EXIT_LONG", "EXIT_SHORT", "CLOSE"]:
        action = "SELL"
    else:
        logger.error(f"REJECTED: Unrecognized action '{action_raw}'")
        return JSONResponse(status_code=422, content={"status": "error", "message": f"Invalid action: {action_raw}"})

    symbol = str(data.get("symbol", "")).upper().strip()
    max_len = getattr(config, "MAX_SYMBOL_LENGTH", 35)
    if len(symbol) > max_len:
        logger.error(f"REJECTED: Symbol too long ({len(symbol)} chars)")
        return JSONResponse(status_code=422, content={"status": "error", "message": "Symbol exceeds max length"})

    try:
        price = float(data.get("price", 0.0))
        raw_quantity = float(data.get("quantity", 0))

        if not math.isfinite(price) or not math.isfinite(raw_quantity):
            raise ValueError("Non-finite numerical payload")

        if abs(raw_quantity - round(raw_quantity)) > 1e-6:
            logger.error(f"REJECTED: Fractional quantity ({raw_quantity}) not allowed.")
            return JSONResponse(status_code=422, content={"status": "error", "message": "Quantity must be a whole number"})
        quantity = int(round(raw_quantity))
    except (ValueError, TypeError, OverflowError):
        logger.error("REJECTED: Malformed numerical values.")
        return JSONResponse(status_code=422, content={"status": "error", "message": "Invalid number format"})

    if quantity <= 0:
        return JSONResponse(status_code=422, content={"status": "error", "message": "Quantity must be > 0"})
    if price < 0:
        return JSONResponse(status_code=422, content={"status": "error", "message": "Price cannot be negative"})

    is_dup = await anyio.to_thread.run_sync(is_duplicate_signal, action, symbol, quantity, price)
    if is_dup:
        logger.warning(f"DEDUP SHIELD: Duplicate signal for {action} {symbol} suppressed.")
        return JSONResponse(status_code=200, content={"status": "warning", "message": "Duplicate signal suppressed"})

    sig_hash_id = hashlib.md5(f"{action}_{symbol}_{quantity}_{price}_{int(now)}".encode()).hexdigest()[:14]
    order_ref = f"TVBot_{sig_hash_id}"
    sig_id = str(uuid.uuid4())

    logger.info(f"Signal Approved -> Action: {action} | Symbol: {symbol} | TV Qty: {quantity} | TV Price: {price} | Ref: {order_ref}")

    payload_dict = {
        "action": action, "symbol": symbol, "quantity": quantity, "price": price, "order_ref": order_ref,
    }
    await anyio.to_thread.run_sync(db_insert_pending, sig_id, payload_dict)

    background_tasks.add_task(
        _dispatch_and_record, sig_id, action, symbol, quantity, price, order_ref
    )

    return {"status": "success", "message": "Execution queued", "signal_id": sig_id}
EOF

# 7. Generate CLI Helper Tools & Shortcuts in /usr/local/bin
echo "[7/7] Installing Enterprise CLI Suite in /usr/local/bin..."

# Helper 1: Position Book
cat << 'EOF' > "$PROJECT_DIR/check_positions.py"
import sys
sys.path.append("/opt/xts_bot")
import config, xts_api, requests

token = xts_api.get_interactive_token()
if not token:
    print("❌ Failed to get Interactive Token")
    sys.exit(1)

base_url = config.XTS_API_BASE_URL.rstrip('/')
url = f"{base_url}/portfolio/positions?dayOrNet=DayWise"
headers = {"authorization": token}

try:
    resp = requests.get(url, headers=headers, timeout=5)
    positions = resp.json().get("result", {}).get("positionList", [])
    
    if not positions:
        print("\n📊 No Open Positions Found\n")
    else:
        print("\n========================= 📊 LIVE BROKER POSITIONS =========================")
        print(f"{'SYMBOL':<24} {'SIDE':<6} {'QTY':<6} {'BUY AVG':<12} {'VALUE':<14} {'MTM P&L'}")
        print("-" * 75)
        for p in positions:
            sym = p.get("TradingSymbol", "")
            qty = int(p.get("Quantity", 0))
            side = "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT")
            buy_avg = float(p.get("BuyAveragePrice", 0) or p.get("ActualBuyAveragePrice", 0))
            val = float(p.get("BuyAmount", 0) or p.get("ActualBuyAmount", 0))
            mtm = p.get("MTM", "0.00")
            print(f"{sym:<24} {side:<6} {qty:<6} ₹{buy_avg:<10.2f} ₹{val:<12.2f} ₹{mtm}")
        print("============================================================================\n")
except Exception as e:
    print(f"Error checking positions: {e}")
EOF

# Helper 2: Live MTM
cat << 'EOF' > "$PROJECT_DIR/live_mtm.py"
import sys
sys.path.append("/opt/xts_bot")
import config, xts_api, requests, time

def get_live_mtm():
    token = xts_api.get_interactive_token()
    if not token:
        print("❌ Failed to get Interactive Token")
        return

    base_url = config.XTS_API_BASE_URL.rstrip('/')
    url = f"{base_url}/portfolio/positions?dayOrNet=DayWise"
    headers = {"authorization": token}

    try:
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        positions = data.get("result", {}).get("positionList", [])
        
        if not positions:
            print("\n📊 No positions found for today.\n")
            return

        total_unrealized_pnl = 0.0
        total_realized_pnl = 0.0

        print("\n" + "=" * 90)
        print(f"   📈 LIVE REAL-TIME MTM DASHBOARD (AC AGARWAL / XTS) - {time.strftime('%H:%M:%S IST')}")
        print("=" * 90)
        print(f"{'SYMBOL':<22} {'SIDE':<6} {'QTY':<5} {'BUY AVG':<10} {'SELL AVG':<10} {'LTP':<10} {'PTS':<8} {'MTM (₹)'}")
        print("-" * 90)

        for p in positions:
            sym = p.get("TradingSymbol", "")
            inst_id = int(p.get("ExchangeInstrumentId", 0))
            exch_seg = p.get("ExchangeSegment", "MCXFO")
            qty = int(p.get("Quantity", 0))
            mult = float(p.get("Multiplier", 1) or 1)
            
            buy_avg = float(p.get("BuyAveragePrice", 0) or p.get("ActualBuyAveragePrice", 0))
            sell_avg = float(p.get("SellAveragePrice", 0) or p.get("ActualSellAveragePrice", 0))
            
            ltp = xts_api.get_live_price(inst_id, exch_seg)
            if not ltp or ltp == "TOKEN_EXPIRED":
                ltp = float(p.get("LastTradedPrice", 0) or buy_avg)

            unrealized_pnl = 0.0
            pts_diff = 0.0
            side = "FLAT"

            if qty > 0:
                side = "LONG"
                pts_diff = ltp - buy_avg
                unrealized_pnl = pts_diff * qty * mult
            elif qty < 0:
                side = "SHORT"
                pts_diff = sell_avg - ltp
                unrealized_pnl = pts_diff * abs(qty) * mult
            
            realized_pnl = float(p.get("RealizedMTM", 0) or 0)
            total_realized_pnl += realized_pnl
            total_unrealized_pnl += unrealized_pnl

            pnl_color = "\033[92m" if unrealized_pnl >= 0 else "\033[91m"
            reset_color = "\033[0m"
            pts_str = f"{pts_diff:+.1f}"
            pnl_str = f"₹{unrealized_pnl:+,.2f}"

            print(f"{sym:<22} {side:<6} {qty:<5} ₹{buy_avg:<9.1f} ₹{sell_avg:<9.1f} ₹{ltp:<9.1f} {pts_str:<8} {pnl_color}{pnl_str:<12}{reset_color}")

        total_net_pnl = total_unrealized_pnl + total_realized_pnl
        net_color = "\033[92m" if total_net_pnl >= 0 else "\033[91m"
        reset_color = "\033[0m"

        print("-" * 90)
        print(f"📊 Realized P&L: ₹{total_realized_pnl:+,.2f}  |  Unrealized P&L: ₹{total_unrealized_pnl:+,.2f}")
        print(f"💰 {net_color}TOTAL NET MTM P&L: ₹{total_net_pnl:+,.2f}{reset_color}")
        print("=" * 90 + "\n")

    except Exception as e:
        print(f"Error fetching live MTM: {e}")

if __name__ == "__main__":
    get_live_mtm()
EOF

# Helper 3: Emergency Kill-Switch
cat << 'EOF' > "$PROJECT_DIR/panic_kill_switch.py"
import sys
sys.path.append("/opt/xts_bot")
import xts_api, json

print("\n🚨 INITIATING EMERGENCY KILL-SWITCH: SQUARING OFF ALL POSITIONS & CANCELLING ORDERS...")
res = xts_api.panic_square_off_all()
print(json.dumps(res, indent=2))
print("✅ EMERGENCY SQUARING COMPLETE.\n")
EOF

# Helper 4: Test Signal Generator
cat << 'EOF' > "$PROJECT_DIR/test_signal.py"
import sys, requests, json
sys.path.append("/opt/xts_bot")
import config

symbol = sys.argv[1] if len(sys.argv) > 1 else "MCX:CRUDEOIL1!"
action = sys.argv[2].upper() if len(sys.argv) > 2 else "BUY"
qty = int(sys.argv[3]) if len(sys.argv) > 3 else 1
price = float(sys.argv[4]) if len(sys.argv) > 4 else 6500.0

payload = {
    "secret": config.WEBHOOK_SECRET,
    "action": action,
    "symbol": symbol,
    "quantity": qty,
    "price": price
}

print(f"📤 Dispatching Test Webhook -> {action} {qty}x {symbol} @ Rs {price}")
try:
    r = requests.post("http://127.0.0.1:8000/webhook", json=payload, timeout=5)
    print(f"📥 Server Response: {r.status_code} | {r.text}")
except Exception as e:
    print(f"❌ Failed: {e}")
EOF

# Install Global Commands in /usr/local/bin
sudo tee /usr/local/bin/xts-positions > /dev/null << 'EOF'
#!/bin/bash
/opt/xts_bot/venv/bin/python3 /opt/xts_bot/check_positions.py
EOF

sudo tee /usr/local/bin/xts-mtm > /dev/null << 'EOF'
#!/bin/bash
if [ "$1" == "--watch" ] || [ "$1" == "-w" ]; then
    watch -n 2 /opt/xts_bot/venv/bin/python3 /opt/xts_bot/live_mtm.py
else
    /opt/xts_bot/venv/bin/python3 /opt/xts_bot/live_mtm.py
fi
EOF

sudo tee /usr/local/bin/xts-panic > /dev/null << 'EOF'
#!/bin/bash
/opt/xts_bot/venv/bin/python3 /opt/xts_bot/panic_kill_switch.py
EOF

sudo tee /usr/local/bin/xts-status > /dev/null << 'EOF'
#!/bin/bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
EOF

sudo tee /usr/local/bin/xts-logs > /dev/null << 'EOF'
#!/bin/bash
sudo journalctl -u xtsbot -f
EOF

sudo tee /usr/local/bin/xts-restart > /dev/null << 'EOF'
#!/bin/bash
sudo systemctl restart xtsbot
echo "✅ XTS Bot Service Restarted."
EOF

sudo tee /usr/local/bin/xts-test > /dev/null << 'EOF'
#!/bin/bash
/opt/xts_bot/venv/bin/python3 /opt/xts_bot/test_signal.py "$@"
EOF

sudo chmod +x /usr/local/bin/xts-*

# Set Permissions
sudo chown -R $USER_NAME:$USER_NAME "$PROJECT_DIR"
sudo chmod 600 "$PROJECT_DIR/config.py"

# Systemd Core Application Service
sudo tee /etc/systemd/system/xtsbot.service > /dev/null << EOF
[Unit]
Description=XTS TradingView Webhook Bot V10.0-ENTERPRISE
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Systemd Daily Restart Service (08:30 AM IST Warmup)
sudo tee /etc/systemd/system/xtsbot-restart.service > /dev/null << EOF
[Unit]
Description=Restart XTS Trading Bot (Daily Master Cache Warm-Up)

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart xtsbot.service
EOF

# Systemd Timer
sudo tee /etc/systemd/system/xtsbot-restart.timer > /dev/null << EOF
[Timer]
OnCalendar=*-*-* 08:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Reload and Enable Services
sudo systemctl daemon-reload
sudo systemctl enable xtsbot.service
sudo systemctl enable xtsbot-restart.timer
sudo systemctl restart xtsbot.service
sudo systemctl restart xtsbot-restart.timer
sudo systemctl restart caddy

echo "==========================================================="
echo " ✅ V10.0-ENTERPRISE OPTIMIZED INSTALLED SUCCESSFULLY!     "
echo "                                                           "
echo " 🛠️ Global Command Suite Active:                            "
echo "   • xts-status     : Check cache, token health & mode       "
echo "   • xts-positions  : View live open positions               "
echo "   • xts-mtm        : Live P&L (use 'xts-mtm -w' for ticker) "
echo "   • xts-test       : Fire test trade e.g. xts-test MCX:CRUDEOIL1! BUY 1 "
echo "   • xts-panic      : Emergency One-Key Kill Switch          "
echo "   • xts-logs       : Live log monitor                       "
echo "   • xts-restart    : Restart service                        "
echo "                                                           "
echo " Webhook Endpoint : http://$(curl -s ifconfig.me)/webhook   "
echo "==========================================================="

digest it completely and understand it completely like a pro
</USER_REQUEST>
<ADDITIONAL_METADATA>
The current local time is: 2026-08-20T16:29:06+05:30.
</ADDITIONAL_METADATA>
<USER_SETTINGS_CHANGE>
The user changed setting `Model Selection` from None to Gemini 3.7 Flash (High). No need to comment on this change if the user doesn't ask about it. If reporting what model you are, please use a human readable name instead of the exact string.
</USER_SETTINGS_CHANGE>