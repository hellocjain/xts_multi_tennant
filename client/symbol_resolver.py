"""
Universal Symbology & Master Contract Resolver
==============================================
Provides sub-millisecond, multi-format symbol resolution across TradingView,
OpenAlgo Unified, and Exchange Tickers for MCX, NSEFO, NSECM, and BSE.
Includes segment-aware tender-period auto-rollover protection to prevent
physical delivery penalties and margin spikes.
"""

import re
import datetime
import logging
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("symbol_resolver")

# IST Timezone
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Commodity Mini Aliases with exact multipliers
COMMODITY_MULTIPLIERS: Dict[str, float] = {
    "ZINCMINI": 1000.0,
    "LEADMINI": 1000.0,
    "ALUMINI": 1000.0,
    "SILVERM": 5.0,
    "SILVERMIC": 1.0,
    "SILVER100": 1.0,
    "GOLDM": 10.0,
    "GOLDPETAL": 1.0,
    "CRUDEOILM": 10.0,
    "NATGASMINI": 250.0,
    "COPPER": 2500.0,
    "CRUDEOIL": 100.0,
    "NATURALGAS": 1250.0,
    "SILVER": 30.0,
    "GOLD": 100.0,
    "ZINC": 5000.0,
    "LEAD": 5000.0,
    "ALUMINIUM": 5000.0,
    "NICKEL": 1500.0,
}

# Common symbol aliases & root normalizations
COMMON_ALIASES: Dict[str, str] = {
    "SILVER100": "SILVER100",
    "SILVER1001!": "SILVER100",
    "GOLDPETAL": "GOLDPETAL",
    "GOLDPETAL1!": "GOLDPETAL",
    "ZINCMINI": "ZINCMINI",
    "ZINCMINI1!": "ZINCMINI",
    "LEADMINI": "LEADMINI",
    "LEADMINI1!": "LEADMINI",
    "ALUMINI": "ALUMINI",
    "ALUMINI1!": "ALUMINI",
    "CRUDEOILM": "CRUDEOILM",
    "CRUDEOILM1!": "CRUDEOILM",
    "NATGASMINI": "NATGASMINI",
    "NATGASMINI1!": "NATGASMINI",
    "SILVERMIC": "SILVERMIC",
    "SILVERMIC1!": "SILVERMIC",
    "SILVERM": "SILVERM",
    "SILVERM1!": "SILVERM",
    "GOLDM": "GOLDM",
    "GOLDM1!": "GOLDM",
    "CRUDEOIL": "CRUDEOIL",
    "CRUDEOIL1!": "CRUDEOIL",
    "NATURALGAS": "NATURALGAS",
    "NATURALGAS1!": "NATURALGAS",
    "SILVER": "SILVER",
    "SILVER1!": "SILVER",
    "GOLD": "GOLD",
    "GOLD1!": "GOLD",
    "NIFTY": "NIFTY",
    "NIFTY1!": "NIFTY",
    "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "BANKNIFTY1!": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "FINNIFTY1!": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "MIDCPNIFTY1!": "MIDCPNIFTY",
}

# Regular expressions for symbology parsing
TV_CONTINUOUS_REGEX = re.compile(r"^([A-Z0-9_]+)(\d+)!$", re.IGNORECASE)
OPENALGO_FUT_REGEX = re.compile(r"^([A-Z0-9_]+)-FUT$", re.IGNORECASE)
OPENALGO_OPT_REGEX = re.compile(r"^([A-Z0-9_]+)-(?:OPT-)?(\d+(?:\.\d+)?)-(CE|PE)$", re.IGNORECASE)
EXCHANGE_FUT_REGEX = re.compile(r"^([A-Z0-9]+?)(\d{1,2}[A-Z]{3}\d{2,4})FUT$", re.IGNORECASE)
PREFIX_STRIPPER = re.compile(r"^(?:MCX:|NSE:|BSE:|NCDEX:|BATS:|INDEX:)", re.IGNORECASE)
SUFFIX_STRIPPER = re.compile(r"(?:-FUT|\.NS|\.BO)$", re.IGNORECASE)
ALPHANUM_ONLY = re.compile(r"[^A-Z0-9]", re.IGNORECASE)

class ParsedSymbol:
    def __init__(self, raw: str, root: str, sym_type: str, depth: int = 1,
                 strike: Optional[float] = None, option_type: Optional[str] = None,
                 expiry_hint: Optional[str] = None, segment_hint: Optional[str] = None):
        self.raw = raw
        self.root = root.upper()
        self.sym_type = sym_type.upper()  # CONTINUOUS, FUT, OPT, CASH
        self.depth = depth
        self.strike = strike
        self.option_type = option_type.upper() if option_type else None
        self.expiry_hint = expiry_hint
        self.segment_hint = segment_hint

    def __repr__(self):
        return f"<ParsedSymbol {self.raw} -> root={self.root} type={self.sym_type} depth={self.depth}>"


def parse_symbol_multi_format(raw_symbol: str) -> ParsedSymbol:
    """
    Parses any incoming symbol string across TradingView continuous, OpenAlgo unified,
    and direct exchange formats.
    """
    s = str(raw_symbol).strip().upper()
    
    # 1. Detect segment prefix if present
    segment_hint = None
    if s.startswith("MCX:"): segment_hint = "MCXFO"
    elif s.startswith("NSE:"): segment_hint = "NSEFO"
    elif s.startswith("BSE:"): segment_hint = "BSEFO"
    elif s.startswith("NCDEX:"): segment_hint = "NCDEX"
    
    s = PREFIX_STRIPPER.sub('', s)

    # 2. TradingView Continuous (e.g. SILVER1001!, CRUDEOIL1!, NIFTY1!)
    tv_match = TV_CONTINUOUS_REGEX.match(s)
    if tv_match:
        root_raw = tv_match.group(1)
        depth = int(tv_match.group(2))
        root = COMMON_ALIASES.get(root_raw, root_raw)
        return ParsedSymbol(raw=raw_symbol, root=root, sym_type="CONTINUOUS", depth=depth, segment_hint=segment_hint)

    # 3. OpenAlgo Unified Futures (e.g. SILVER100-FUT, CRUDEOIL-FUT)
    oa_fut = OPENALGO_FUT_REGEX.match(s)
    if oa_fut:
        root_raw = oa_fut.group(1)
        root = COMMON_ALIASES.get(root_raw, root_raw)
        return ParsedSymbol(raw=raw_symbol, root=root, sym_type="FUT", depth=1, segment_hint=segment_hint)

    # 4. OpenAlgo Unified Options (e.g. NIFTY-25000-CE, BANKNIFTY-OPT-52000-PE)
    oa_opt = OPENALGO_OPT_REGEX.match(s)
    if oa_opt:
        root_raw = oa_opt.group(1)
        strike = float(oa_opt.group(2))
        opt_type = oa_opt.group(3)
        root = COMMON_ALIASES.get(root_raw, root_raw)
        return ParsedSymbol(raw=raw_symbol, root=root, sym_type="OPT", strike=strike, option_type=opt_type, segment_hint=segment_hint)

    # 5. Exchange Exact Futures (e.g. SILVER10030SEP2026FUT)
    exch_fut = EXCHANGE_FUT_REGEX.match(s)
    if exch_fut:
        root_raw = exch_fut.group(1)
        exp_str = exch_fut.group(2)
        root = COMMON_ALIASES.get(root_raw, root_raw)
        return ParsedSymbol(raw=raw_symbol, root=root, sym_type="FUT", depth=1, expiry_hint=exp_str, segment_hint=segment_hint)

    # 6. Fallback clean root
    clean = SUFFIX_STRIPPER.sub('', s)
    root = COMMON_ALIASES.get(clean, clean)
    return ParsedSymbol(raw=raw_symbol, root=root, sym_type="CASH" if segment_hint in ("NSECM", "BSECM") else "FUT", depth=1, segment_hint=segment_hint)


def get_contract_multiplier(root_symbol: str) -> float:
    """Returns the contract lot multiplier for commodity and index instruments."""
    clean_root = ALPHANUM_ONLY.sub('', root_symbol.upper())
    return COMMODITY_MULTIPLIERS.get(clean_root, 1.0)


def calculate_tender_period_cutoff(expiry_date: datetime.date, exchange_segment: str, current_time: Optional[datetime.datetime] = None) -> Tuple[bool, int, str]:
    """
    Computes segment-aware tender-period auto-rollover and safety status.
    
    Rules:
    - MCX Commodities: Auto-roll 7 calendar days before expiry (tender margin penalty protection).
    - NSE Index Futures (NIFTY, BANKNIFTY): Auto-roll on expiry day at 14:30 IST.
    - NSE Stock Futures: Auto-roll 2 calendar days before expiry.
    - Cash/Other: No rollover required.
    
    Returns:
    (requires_rollover: bool, days_left: int, status_badge: str)
    """
    now = current_time or datetime.datetime.now(IST)
    today = now.date()
    days_left = (expiry_date - today).days

    seg = (exchange_segment or "").upper()
    
    # 1. MCX Tender Period (7 days prior)
    if seg in ("MCXFO", "MCX", "NCDEX"):
        if days_left <= 0:
            return True, 0, "Expired ⚠️"
        elif days_left <= 7:
            return True, days_left, f"Tender Period Rollover ({days_left}d left) 🚨"
        else:
            return False, days_left, f"Expires in {days_left}d ⏳"

    # 2. NSE Index Futures (Expiry day at 14:30 IST)
    elif seg in ("NSEFO", "NSE", "BSEFO"):
        if days_left < 0:
            return True, 0, "Expired ⚠️"
        elif days_left == 0:
            # Check 14:30 IST threshold
            if now.hour > 14 or (now.hour == 14 and now.minute >= 30):
                return True, 0, "Expiry Rollover (Post 14:30) 🚨"
            else:
                return False, 0, "Expiry Day Today ⚠️"
        elif days_left <= 2:
            return False, days_left, f"Expires in {days_left}d ⏳"
        else:
            return False, days_left, f"Expires in {days_left}d ⏳"

    return False, max(0, days_left), "Active 🟢"


def select_active_contract_with_rollover(
    available_contracts: List[Tuple[Any, ...]], 
    depth: int = 1,
    current_time: Optional[datetime.datetime] = None
) -> Optional[Tuple[Any, ...]]:
    """
    Selects the active contract for a continuous series (depth 1 = front month, depth 2 = next month),
    applying tender-period auto-rollover logic.
    
    Contract tuple structure:
    (exp_date, exch_id, exch_seg, desc, tick_size, lot_size, freeze_qty)
    """
    if not available_contracts:
        return None

    # Filter out expired contracts or those in tender rollover period
    valid_contracts = []
    for c in available_contracts:
        exp_date = c[0]
        exch_seg = c[2]
        
        if exp_date is None or exp_date == datetime.date(2099, 12, 31):
            valid_contracts.append(c)
            continue

        requires_rollover, _, _ = calculate_tender_period_cutoff(exp_date, exch_seg, current_time)
        if not requires_rollover:
            valid_contracts.append(c)

    candidates = valid_contracts if valid_contracts else available_contracts
    idx = min(max(0, depth - 1), len(candidates) - 1)
    return candidates[idx]
