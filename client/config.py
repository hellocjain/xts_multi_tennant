# =====================================================================
# XTS CLIENT CONFIGURATION LOADER (MULTI-TENANT CONTAINERIZED)
# =====================================================================
import os
import sys
import json

# If a custom data directory is mounted (e.g. /app/data), ensure it is in sys.path
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

# 1. Broker API Credentials
API_KEY = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")
MD_API_KEY = os.environ.get("MD_API_KEY", API_KEY)
MD_API_SECRET = os.environ.get("MD_API_SECRET", API_SECRET)
CLIENT_ID = os.environ.get("CLIENT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
XTS_API_BASE_URL = os.environ.get("XTS_API_BASE_URL", "https://symphony.acagarwal.com:3000/interactive")

# 2. Execution & Marketable Limit Rules
ORDER_TYPE = os.environ.get("ORDER_TYPE", "LIMIT")
SLIPPAGE_BUFFER_PCT = float(os.environ.get("SLIPPAGE_BUFFER_PCT", "0.005"))
TIME_IN_FORCE = os.environ.get("TIME_IN_FORCE", "DAY")

# 3. Supertrend & Price Deviation
ENFORCE_PRICE_DEVIATION_CHECK = os.environ.get("ENFORCE_PRICE_DEVIATION_CHECK", "False").lower() in ("true", "1", "yes")
ALLOW_TRADE_WITHOUT_LIVE_PRICE = os.environ.get("ALLOW_TRADE_WITHOUT_LIVE_PRICE", "True").lower() in ("true", "1", "yes")
MAX_PRICE_DEVIATION_PCT = float(os.environ.get("MAX_PRICE_DEVIATION_PCT", "0.05"))

# 4. Expiry & Rollover Shields
MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX = int(os.environ.get("MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX", "5"))
MIN_DAYS_BEFORE_EXPIRY_DERIVATIVES = int(os.environ.get("MIN_DAYS_BEFORE_EXPIRY_DERIVATIVES", "0"))

# 5. Partial Fill Guard
CANCEL_LINGERING_PARTIAL_FILLS = os.environ.get("CANCEL_LINGERING_PARTIAL_FILLS", "True").lower() in ("true", "1", "yes")
PARTIAL_FILL_TIMEOUT_SECONDS = float(os.environ.get("PARTIAL_FILL_TIMEOUT_SECONDS", "2.0"))

# 6. Sizing & Risk Caps
TV_SENDS_LOTS = os.environ.get("TV_SENDS_LOTS", "True").lower() in ("true", "1", "yes")
MAX_LOTS_LIMIT = int(os.environ.get("MAX_LOTS_LIMIT", "100"))
MAX_UNITS_LIMIT = int(os.environ.get("MAX_UNITS_LIMIT", "100000"))
MAX_ORDER_VALUE_INR = float(os.environ.get("MAX_ORDER_VALUE_INR", "5000000.0"))
DAILY_NOTIONAL_CAP_INR = float(os.environ.get("DAILY_NOTIONAL_CAP_INR", "10000000.0"))
DEDUP_WINDOW_SECONDS = float(os.environ.get("DEDUP_WINDOW_SECONDS", "3.0"))

TV_TO_XTS_MAP = {}
ALLOW_PREFIX_FALLBACK = False
MAX_SYMBOL_LENGTH = int(os.environ.get("MAX_SYMBOL_LENGTH", "35"))

# 7. Concurrency & Network Keepalive
DEFAULT_FREEZE_QTY_IF_UNKNOWN = int(os.environ.get("DEFAULT_FREEZE_QTY_IF_UNKNOWN", "100000"))
TOKEN_REFRESH_WAIT_TIMEOUT = float(os.environ.get("TOKEN_REFRESH_WAIT_TIMEOUT", "8.0"))
CACHE_WATCHDOG_INTERVAL_SECONDS = int(os.environ.get("CACHE_WATCHDOG_INTERVAL_SECONDS", "30"))
TOKEN_KEEPALIVE_INTERVAL_SECONDS = int(os.environ.get("TOKEN_KEEPALIVE_INTERVAL_SECONDS", "240"))
MAX_TRACKED_FAILED_IPS = int(os.environ.get("MAX_TRACKED_FAILED_IPS", "2000"))
STALE_SIGNAL_WINDOW_SECONDS = float(os.environ.get("STALE_SIGNAL_WINDOW_SECONDS", "30.0"))
BACKGROUND_THREAD_POOL_SIZE = int(os.environ.get("BACKGROUND_THREAD_POOL_SIZE", "20"))
MAX_WEBHOOK_BODY_BYTES = int(os.environ.get("MAX_WEBHOOK_BODY_BYTES", "10000"))
OPS_ALERT_WEBHOOK_URL = os.environ.get("OPS_ALERT_WEBHOOK_URL", "")

# 8. Paper Trading Mode
PAPER_TRADE_MODE = os.environ.get("PAPER_TRADE_MODE", "True").lower() in ("true", "1", "yes")
LOG_PAPER_TRADES_TO_FILE = os.environ.get("LOG_PAPER_TRADES_TO_FILE", "True").lower() in ("true", "1", "yes")

# 9. Execution & Fill Push Notifications
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Load mounted config overrides if present
_mounted_config = os.path.join(DATA_DIR, "config.json")
if os.path.exists(_mounted_config):
    try:
        with open(_mounted_config, "r") as _f:
            _overrides = json.load(_f)
            for _k, _v in _overrides.items():
                globals()[_k] = _v
    except Exception as _e:
        pass
