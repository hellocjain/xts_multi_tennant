"""
token_db.py - OpenAlgo-Compatible Dual-Indexed Token and Symbol Engine.
Provides sub-millisecond in-memory forward and reverse resolution between
OpenAlgo canonical symbols and Symphony XTS ExchangeInstrumentIDs.
"""
import os
import sqlite3
import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# Exchange code translation dictionaries
OA_TO_XTS_EXCHANGE = {
    "NSE": "NSECM",
    "BSE": "BSECM",
    "NFO": "NSEFO",
    "BFO": "BSEFO",
    "MCX": "MCXFO",
    "CDS": "NSECD",
    "NSE_INDEX": "NSECM",
    "BSE_INDEX": "BSECM",
}

XTS_TO_OA_EXCHANGE = {
    "NSECM": "NSE",
    "BSECM": "BSE",
    "NSEFO": "NFO",
    "BSEFO": "BFO",
    "MCXFO": "MCX",
    "NSECD": "CDS",
}

# Standard Indian exchange freeze limits
DEFAULT_FREEZE_LIMITS = {
    "NIFTY": 1800,
    "BANKNIFTY": 900,
    "FINNIFTY": 1800,
    "MIDCPNIFTY": 4200,
    "CRUDEOIL": 10000,
    "NATURALGAS": 10000,
}


@dataclass
class SymbolData:
    symbol: str
    brsymbol: str
    name: str
    exchange: str
    brexchange: str
    token: str
    expiry: str = ""
    strike: float = 0.0
    lotsize: int = 1
    instrumenttype: str = "EQ"
    tick_size: float = 0.05
    freeze_qty: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TokenDatabase:
    """Thread-safe, dual-indexed in-memory token and symbol database."""

    def __init__(self):
        # Key: (symbol_upper, exchange_upper) -> SymbolData
        self._symbol_to_info: Dict[Tuple[str, str], SymbolData] = {}
        # Key: (str(token), exchange_upper) -> symbol_str
        self._token_to_symbol: Dict[Tuple[str, str], str] = {}
        # Key: (str(token), brexchange_upper) -> symbol_str
        self._brexchange_token_to_symbol: Dict[Tuple[str, str], str] = {}
        # Flat list for substring search
        self._symbols_list: List[SymbolData] = []
        self._is_loaded: bool = False

    def clear(self):
        self._symbol_to_info.clear()
        self._token_to_symbol.clear()
        self._brexchange_token_to_symbol.clear()
        self._symbols_list.clear()
        self._is_loaded = False

    def register_symbol(self, data: Dict[str, Any]):
        """Registers a symbol into the in-memory indexes."""
        sym = str(data.get("symbol", "")).strip().upper()
        exch = str(data.get("exchange", "")).strip().upper()
        brexch = str(data.get("brexchange", "")).strip().upper()
        tok = str(data.get("token", "")).strip()

        if not sym or not exch or not tok:
            return

        name = str(data.get("name", sym)).strip().upper()
        lotsize = int(data.get("lotsize", 1) or 1)
        tick_size = float(data.get("tick_size", 0.05) or 0.05)
        strike = float(data.get("strike", 0.0) or 0.0)
        freeze_qty = int(data.get("freeze_qty", 0) or DEFAULT_FREEZE_LIMITS.get(name, 0))

        item = SymbolData(
            symbol=sym,
            brsymbol=str(data.get("brsymbol", sym)),
            name=name,
            exchange=exch,
            brexchange=brexch or OA_TO_XTS_EXCHANGE.get(exch, exch),
            token=tok,
            expiry=str(data.get("expiry", "")),
            strike=strike,
            lotsize=lotsize,
            instrumenttype=str(data.get("instrumenttype", "EQ")).upper(),
            tick_size=tick_size,
            freeze_qty=freeze_qty,
        )

        self._symbol_to_info[(sym, exch)] = item
        self._token_to_symbol[(tok, exch)] = sym
        if item.brexchange:
            self._brexchange_token_to_symbol[(tok, item.brexchange)] = sym

        self._symbols_list.append(item)

    def load_from_sqlite(self, db_path: str) -> int:
        """Loads master contracts from a SQLite database into memory."""
        if not os.path.exists(db_path):
            logger.warning(f"Master contract DB not found at {db_path}")
            return 0

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Verify table exists
            check = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='symtoken'"
            ).fetchone()
            if not check:
                logger.warning(f"symtoken table does not exist in {db_path}")
                conn.close()
                return 0

            rows = cursor.execute("""
                SELECT symbol, brsymbol, name, exchange, brexchange, token, 
                       expiry, strike, lotsize, instrumenttype, tick_size, freeze_qty
                FROM symtoken
            """).fetchall()

            self.clear()
            for r in rows:
                self.register_symbol(dict(r))

            conn.close()
            self._is_loaded = True
            logger.info(f"Loaded {len(self._symbols_list)} master contract symbols into memory from {db_path}")
            return len(self._symbols_list)
        except Exception as e:
            logger.error(f"Failed to load master contract DB from {db_path}: {e}")
            return 0

    def get_token(self, symbol: str, exchange: str) -> Optional[str]:
        """Resolves an OpenAlgo canonical symbol to an exchangeInstrumentID."""
        if not symbol or not exchange:
            return None
        key = (symbol.strip().upper(), exchange.strip().upper())
        info = self._symbol_to_info.get(key)
        return info.token if info else None

    def get_symbol(self, token: Any, exchange: str) -> Optional[str]:
        """Resolves an exchangeInstrumentID to an OpenAlgo canonical symbol."""
        if not token or not exchange:
            return None
        tok_str = str(token).strip()
        exch_upper = exchange.strip().upper()

        # 1. Direct match by OpenAlgo exchange
        sym = self._token_to_symbol.get((tok_str, exch_upper))
        if sym:
            return sym

        # 2. Match by broker exchange segment
        sym = self._brexchange_token_to_symbol.get((tok_str, exch_upper))
        if sym:
            return sym

        # 3. Match by translated broker exchange segment
        mapped_brexch = OA_TO_XTS_EXCHANGE.get(exch_upper)
        if mapped_brexch:
            sym = self._brexchange_token_to_symbol.get((tok_str, mapped_brexch))
            if sym:
                return sym

        return None

    def get_symbol_info(self, symbol: str, exchange: str) -> Optional[SymbolData]:
        """Returns the full SymbolData object for a symbol and exchange."""
        if not symbol or not exchange:
            return None
        key = (symbol.strip().upper(), exchange.strip().upper())
        return self._symbol_to_info.get(key)

    def search_symbols(
        self, query: Optional[str] = None, exchange: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Fast multi-term search across canonical symbol, underlying name, and token.
        """
        q_terms = [t.strip().upper() for t in (query or "").split() if t.strip()]
        exch_filter = exchange.strip().upper() if exchange else None

        results = []
        for item in self._symbols_list:
            if exch_filter and item.exchange != exch_filter:
                continue

            if not q_terms:
                results.append(item.to_dict())
            else:
                matches = True
                for t in q_terms:
                    if t not in item.symbol and t not in item.name and t != item.token:
                        matches = False
                        break
                if matches:
                    results.append(item.to_dict())

            if len(results) >= limit:
                break

        return results


# Global singleton instance
_GLOBAL_TOKEN_DB = TokenDatabase()


def get_token_db() -> TokenDatabase:
    return _GLOBAL_TOKEN_DB


def get_token(symbol: str, exchange: str) -> Optional[str]:
    return _GLOBAL_TOKEN_DB.get_token(symbol, exchange)


def get_symbol(token: Any, exchange: str) -> Optional[str]:
    return _GLOBAL_TOKEN_DB.get_symbol(token, exchange)


def get_symbol_info(symbol: str, exchange: str) -> Optional[SymbolData]:
    return _GLOBAL_TOKEN_DB.get_symbol_info(symbol, exchange)


def search_symbols(query: Optional[str] = None, exchange: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    return _GLOBAL_TOKEN_DB.search_symbols(query, exchange, limit)


def init_token_db(db_path: Optional[str] = None) -> int:
    """Initializes token DB from environment or standard path."""
    target_path = db_path or os.environ.get("MASTER_CONTRACT_DB_PATH")
    if not target_path:
        # Standard fallback search paths
        candidates = [
            "/app/master_contract.db",
            os.path.join(os.path.dirname(__file__), "..", "data", "master_contract.db"),
            os.path.join(os.path.dirname(__file__), "master_contract.db"),
        ]
        for c in candidates:
            if os.path.exists(c):
                target_path = c
                break

    if target_path and os.path.exists(target_path):
        return _GLOBAL_TOKEN_DB.load_from_sqlite(target_path)
    return 0
