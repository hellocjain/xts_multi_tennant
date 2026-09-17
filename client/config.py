# =====================================================================
# XTS CLIENT CONFIGURATION LOADER (MULTI-TENANT CONTAINERIZED)
# =====================================================================
import os
import sys
import json
import time
from typing import Optional

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
MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX = int(os.environ.get("MIN_DAYS_BEFORE_EXPIRY_MCX_NCDEX", "7"))
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
INTERNAL_AUTH_TOKEN = os.environ.get("INTERNAL_AUTH_TOKEN", "")

# 10. Market Hours & Stale Candle Restrictions
ENFORCE_MARKET_HOURS = os.environ.get("ENFORCE_MARKET_HOURS", "True").lower() in ("true", "1", "yes")
ENFORCE_STALE_CANDLE_GUARD = os.environ.get("ENFORCE_STALE_CANDLE_GUARD", "True").lower() in ("true", "1", "yes")
MCX_MARKET_OPEN_TIME = os.environ.get("MCX_MARKET_OPEN_TIME", "09:00:00")
MCX_MARKET_CLOSE_TIME = os.environ.get("MCX_MARKET_CLOSE_TIME", "23:55:00")
NSE_MARKET_OPEN_TIME = os.environ.get("NSE_MARKET_OPEN_TIME", "09:15:00")
NSE_MARKET_CLOSE_TIME = os.environ.get("NSE_MARKET_CLOSE_TIME", "15:30:00")
MAX_CANDLE_AGE_SECONDS = int(os.environ.get("MAX_CANDLE_AGE_SECONDS", "180"))

import datetime

IST_TIMEZONE = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Full-Day Trading Holidays (MCX Closed for both Morning & Evening sessions: 09:00 - 23:55)
MCX_FULL_DAY_HOLIDAYS = {
    # 2026
    datetime.date(2026, 1, 26),   # Republic Day
    datetime.date(2026, 8, 15),   # Independence Day
    datetime.date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    datetime.date(2026, 12, 25),  # Christmas
    # 2027
    datetime.date(2027, 1, 26),   # Republic Day
    datetime.date(2027, 8, 15),   # Independence Day
    datetime.date(2027, 10, 2),   # Mahatma Gandhi Jayanti
    datetime.date(2027, 12, 25),  # Christmas
}

# Morning Session Holidays (MCX Morning Session Closed: 09:00 - 17:00 IST | Evening Session Open: 17:00 - 23:55 IST)
MCX_MORNING_SESSION_HOLIDAYS = {
    # 2026
    datetime.date(2026, 2, 17),   # Mahashivratri
    datetime.date(2026, 3, 6),    # Holi
    datetime.date(2026, 3, 20),   # Id-Ul-Fitr
    datetime.date(2026, 4, 3),    # Good Friday
    datetime.date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    datetime.date(2026, 5, 1),    # Maharashtra Day
    datetime.date(2026, 5, 27),   # Bakri Id / Eid ul-Adha
    datetime.date(2026, 6, 26),   # Muharram
    datetime.date(2026, 10, 20),  # Dussehra
    datetime.date(2026, 11, 8),   # Diwali-Laxmi Pujan (Muhurat session evening only)
    datetime.date(2026, 11, 24),  # Gurunanak Jayanti
    # 2027
    datetime.date(2027, 3, 22),   # Holi
    datetime.date(2027, 3, 26),   # Good Friday
    datetime.date(2027, 4, 14),   # Ambedkar Jayanti
    datetime.date(2027, 5, 1),    # Maharashtra Day
}

# Unified set of all holidays (used for daylight 14:00 cutoff pullback calculations where market must be open at 14:00)
MCX_HOLIDAYS = MCX_FULL_DAY_HOLIDAYS | MCX_MORNING_SESSION_HOLIDAYS

def is_market_open_ist(exch_seg: str = "MCXFO", now_ts: Optional[float] = None, force_check: bool = False) -> bool:
    """
    Evaluates whether the specified Indian exchange segment is currently open for trading.
    - Indian Standard Time (IST) = UTC+5:30
    - Monday to Friday only (weekday 0-4). Saturday (5) & Sunday (6) are strictly closed.
    - MCX (MCXFO, MCXCOM):
        * Standard trading days: 09:00:00 to 23:55:00 IST
        * Morning Session Holidays: Closed 09:00:00 to 17:00:00 IST; Open 17:00:00 to 23:55:00 IST
        * Full-Day Holidays: Closed all day (00:00:00 to 24:00:00 IST)
    - NSE/BSE (NSEFO, NSECM, BSEFO, BSECM):
        * Standard trading days: 09:15:00 to 15:30:00 IST
        * Holidays: Closed all day
    - Respects config.ENFORCE_MARKET_HOURS.
    """
    if not force_check and not ENFORCE_MARKET_HOURS:
        return True

    # In automated test runs without explicit market hour enforcement, allow bypass
    if not force_check and "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("ENFORCE_MARKET_HOURS_IN_TESTS", "").lower() not in ("true", "1", "yes"):
        return True

    ts = now_ts if now_ts is not None else time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=IST_TIMEZONE)
    d_date = dt.date()

    # 1. Weekday Check (Monday = 0 ... Friday = 4; Saturday = 5, Sunday = 6)
    if dt.weekday() >= 5:
        return False

    seg_upper = str(exch_seg or "").upper()
    cur_hms = (dt.hour, dt.minute, dt.second)
    is_mcx = ("MCX" in seg_upper or "COMMODITY" in seg_upper)

    # 2. Segment-specific Holiday & Trading Hours Evaluation
    if is_mcx:
        if d_date in MCX_FULL_DAY_HOLIDAYS:
            return False
        if d_date in MCX_MORNING_SESSION_HOLIDAYS:
            # Morning closed, Evening open from 17:00:00 to 23:55:00 IST
            return (17, 0, 0) <= cur_hms <= (23, 55, 0)
        # Standard weekday trading session
        return (9, 0, 0) <= cur_hms <= (23, 55, 0)
    else:
        # NSE / BSE Equity & Derivatives: Closed on all Indian market holidays
        if d_date in MCX_HOLIDAYS:
            return False
        if any(eq in seg_upper for eq in ("NSE", "BSE", "CM", "CASH")):
            return (9, 15, 0) <= cur_hms <= (15, 30, 0)
        else:
            return (9, 0, 0) <= cur_hms <= (23, 55, 0)

def is_market_opening_stabilizing(exch_seg: str = "MCXFO", now_ts: Optional[float] = None) -> bool:
    """
    Returns True if the market has just opened and is within the 60-second stabilization window:
    - Standard MCX day: between 09:00:00 and 09:01:00 IST
    - Morning-Holiday MCX day: between 17:00:00 and 17:01:00 IST
    During this 60-second window, automated rollover orders should pause to avoid
    opening auction orderbook imbalances, wide bid-ask spreads, and excessive slippage.
    """
    if "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("ENFORCE_MARKET_HOURS_IN_TESTS", "").lower() not in ("true", "1", "yes"):
        return False

    ts = now_ts if now_ts is not None else time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=IST_TIMEZONE)
    d_date = dt.date()
    cur_hms = (dt.hour, dt.minute, dt.second)

    seg_upper = str(exch_seg or "").upper()
    is_mcx = ("MCX" in seg_upper or "COMMODITY" in seg_upper)

    if is_mcx:
        if d_date in MCX_MORNING_SESSION_HOLIDAYS:
            # Evening session open is 17:00:00 -> buffer is 17:00:00 to 17:01:00 IST
            return (17, 0, 0) <= cur_hms < (17, 1, 0)
        elif d_date not in MCX_FULL_DAY_HOLIDAYS:
            # Standard session open is 09:00:00 -> buffer is 09:00:00 to 09:01:00 IST
            return (9, 0, 0) <= cur_hms < (9, 1, 0)
    return False

def get_commodity_rollover_cutoff(exp_date, exch_seg: str = "MCXFO") -> datetime.datetime:
    """
    Computes the exact timestamp when a commodity futures contract must roll over:
    - 8 days prior to expiry date at 14:00:00 IST.
    - If the 8th day falls on a weekend (Saturday/Sunday) or an exchange holiday,
      it rolls back to the preceding active trading day at 14:00:00 IST.
    """
    if exp_date is None:
        return datetime.datetime.max.replace(tzinfo=IST_TIMEZONE)
    if isinstance(exp_date, datetime.datetime):
        exp_date = exp_date.date()

    # Target date: 8 calendar days before expiry
    target_date = exp_date - datetime.timedelta(days=8)

    # Step backward until an active trading weekday is reached
    while target_date.weekday() >= 5 or target_date in MCX_HOLIDAYS:
        target_date -= datetime.timedelta(days=1)

    return datetime.datetime.combine(
        target_date,
        datetime.time(14, 0, 0),
        tzinfo=IST_TIMEZONE
    )

def is_commodity_past_rollover(exp_date, exch_seg: str = "MCXFO", now_dt: Optional[datetime.datetime] = None) -> bool:
    """
    Determines if a commodity contract has crossed its rollover cutoff:
    1. Returns True if now_dt >= get_commodity_rollover_cutoff(exp_date, exch_seg).
    2. Fail-safe immediate rollover: returns True if (exp_date - now_date).days <= 7.
    """
    if exp_date is None:
        return False
    if isinstance(exp_date, datetime.datetime):
        exp_date = exp_date.date()

    if now_dt is None:
        now_dt = datetime.datetime.now(IST_TIMEZONE)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=IST_TIMEZONE)

    # Fail-safe: <= 7 calendar days remaining to expiry is always past rollover
    days_left = (exp_date - now_dt.date()).days
    if days_left <= 7:
        return True

    cutoff_dt = get_commodity_rollover_cutoff(exp_date, exch_seg)
    return now_dt >= cutoff_dt


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

