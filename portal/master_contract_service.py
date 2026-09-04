"""
master_contract_service.py - Portal Master Contract Engine for Symphony XTS.
Downloads daily exchange instrument definitions from Symphony XTS Market Data API,
normalizes them into OpenAlgo canonical symbology across all 6 segments plus indices,
and compiles an indexed SQLite master_contract.db for shared tenant access.
"""
import os
import sys
import csv
import json
import time
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)

# BSE Index Name Standardization Map
BSE_INDEX_SYMBOL_MAP = {
    "SNSX50": "SENSEX50",
    "SNXT50": "BSESENSEXNEXT50",
    "MID150": "BSE150MIDCAPINDEX",
    "LMI250": "BSE250LARGEMIDCAPINDEX",
    "MSL400": "BSE400MIDSMALLCAPINDEX",
    "AUTO": "BSEAUTO",
    "BSE CG": "BSECAPITALGOODS",
    "CARBON": "BSECARBONEX",
    "BSE CD": "BSECONSUMERDURABLES",
    "CPSE": "BSECPSE",
    "DOL100": "BSEDOLLEX100",
    "DOL200": "BSEDOLLEX200",
    "DOL30": "BSEDOLLEX30",
    "ENERGY": "BSEENERGY",
    "BSEFMC": "BSEFASTMOVINGCONSUMERGOODS",
    "FIN": "BSEFINANCIALSERVICES",
    "FINSER": "BSEFINANCIALSERVICES",
    "GREENX": "BSEGREENEX",
    "BSE HC": "BSEHEALTHCARE",
    "INFRA": "BSEINDIAINFRASTRUCTUREINDEX",
    "INDSTR": "BSEINDUSTRIALS",
    "BSE IT": "BSEINFORMATIONTECHNOLOGY",
    "LRGCAP": "BSELARGECAP",
    "METAL": "BSEMETAL",
    "MIDCAP": "BSEMIDCAP",
    "MIDSEL": "BSEMIDCAPSELECTINDEX",
    "OILGAS": "BSEOIL&GAS",
    "POWER": "BSEPOWER",
    "BSEPBI": "BSEPSU",
    "REALTY": "BSEREALTY",
    "SMLCAP": "BSESMALLCAP",
    "SMLSEL": "BSESMALLCAPSELECTINDEX",
    "SMEIPO": "BSESMEIPO",
    "TECK": "BSETECK",
    "TELCOM": "BSETELECOM",
}

# NSE Index Name Standardization Map
NSE_INDEX_SYMBOL_MAP = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "INDIA VIX": "INDIAVIX",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
    "NIFTY NEXT 50": "NIFTYNXT50",
    "HANGSENG BEES NAV": "HANGSENGBEESNAV",
    "HANGSENG BEES-NAV": "HANGSENGBEESNAV",
}


def parse_date_to_ddmmmyy(val: Any) -> str:
    """Parses date string or timestamp to canonical DDMMMYY format (e.g. 26MAR26)."""
    if not val or str(val).strip() in ("", "None", "1"):
        return ""
    val_str = str(val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d%b%Y"):
        try:
            dt = datetime.strptime(val_str.split(".")[0], fmt)
            return dt.strftime("%d%b%y").upper()
        except ValueError:
            continue
    return val_str.upper()


def format_strike(val: Any) -> str:
    """Formats strike price cleanly (e.g. 24500.0 -> '24500', 292.5 -> '292.5')."""
    try:
        f = float(val)
        return str(int(f)) if f == int(f) else str(f)
    except (ValueError, TypeError):
        return ""


def parse_equity_csv_rows(rows: List[List[str]], exchange_segment: str) -> List[Dict[str, Any]]:
    """
    Parses XTS Equity CSV rows (NSECM or BSECM).
    Expected columns:
    ExchangeSegment,ExchangeInstrumentID,InstrumentType,Name,Description,Series,NameWithSeries,InstrumentID,
    PriceBand.High,PriceBand.Low,FreezeQty,TickSize,LotSize,Multiplier,DisplayName,...
    """
    records = []
    if not rows:
        return records

    header = [h.strip() for h in rows[0]]
    col_idx = {h: i for i, h in enumerate(header)}

    is_bse = "BSE" in exchange_segment.upper()

    for r in rows[1:]:
        if len(r) < 6:
            continue
        series = r[col_idx.get("Series", 5)].strip().upper() if "Series" in col_idx else ""
        name = r[col_idx.get("Name", 3)].strip().upper() if "Name" in col_idx else ""
        token = r[col_idx.get("ExchangeInstrumentID", 1)].strip() if "ExchangeInstrumentID" in col_idx else ""
        display_name = r[col_idx.get("DisplayName", 14)].strip() if "DisplayName" in col_idx and len(r) > col_idx["DisplayName"] else name

        if not name or not token:
            continue

        if is_bse:
            if series == "SPOT":
                norm_name = BSE_INDEX_SYMBOL_MAP.get(name, name).replace(" ", "")
                records.append({
                    "symbol": norm_name,
                    "brsymbol": display_name or name,
                    "name": norm_name,
                    "exchange": "BSE_INDEX",
                    "brexchange": exchange_segment,
                    "token": token,
                    "expiry": "",
                    "strike": 0.0,
                    "lotsize": 1,
                    "instrumenttype": "INDEX",
                    "tick_size": 0.05,
                    "freeze_qty": 0,
                })
            elif series in ("EQ", "A", "B", "T"):
                records.append({
                    "symbol": name,
                    "brsymbol": display_name or name,
                    "name": name,
                    "exchange": "BSE",
                    "brexchange": exchange_segment,
                    "token": token,
                    "expiry": "",
                    "strike": 0.0,
                    "lotsize": int(float(r[col_idx.get("LotSize", 12)])) if "LotSize" in col_idx and len(r) > col_idx["LotSize"] and r[col_idx["LotSize"]].strip() else 1,
                    "instrumenttype": "EQ",
                    "tick_size": float(r[col_idx.get("TickSize", 11)]) if "TickSize" in col_idx and len(r) > col_idx["TickSize"] and r[col_idx["TickSize"]].strip() else 0.05,
                    "freeze_qty": int(float(r[col_idx.get("FreezeQty", 10)])) if "FreezeQty" in col_idx and len(r) > col_idx["FreezeQty"] and r[col_idx["FreezeQty"]].strip() else 0,
                })
        else: # NSECM
            if series == "EQ":
                records.append({
                    "symbol": name,
                    "brsymbol": display_name or name,
                    "name": name,
                    "exchange": "NSE",
                    "brexchange": exchange_segment,
                    "token": token,
                    "expiry": "",
                    "strike": 0.0,
                    "lotsize": int(float(r[col_idx.get("LotSize", 12)])) if "LotSize" in col_idx and len(r) > col_idx["LotSize"] and r[col_idx["LotSize"]].strip() else 1,
                    "instrumenttype": "EQ",
                    "tick_size": float(r[col_idx.get("TickSize", 11)]) if "TickSize" in col_idx and len(r) > col_idx["TickSize"] and r[col_idx["TickSize"]].strip() else 0.05,
                    "freeze_qty": int(float(r[col_idx.get("FreezeQty", 10)])) if "FreezeQty" in col_idx and len(r) > col_idx["FreezeQty"] and r[col_idx["FreezeQty"]].strip() else 0,
                })

    return records


def parse_derivatives_csv_rows(rows: List[List[str]], exchange_segment: str) -> List[Dict[str, Any]]:
    """
    Parses XTS Derivatives CSV rows (NSEFO, BSEFO, MCXFO, NSECD).
    Derives canonical OpenAlgo symbols:
    - Futures: [Name][DDMMMYY]FUT
    - Options: [Name][DDMMMYY][Strike][CE/PE]
    """
    records = []
    if not rows:
        return records

    header = [h.strip() for h in rows[0]]
    col_idx = {h: i for i, h in enumerate(header)}

    oa_exchange = {
        "NSEFO": "NFO",
        "BSEFO": "BFO",
        "MCXFO": "MCX",
        "NSECD": "CDS",
    }.get(exchange_segment.upper(), exchange_segment)

    for r in rows[1:]:
        if len(r) < 18:
            continue

        name = r[col_idx.get("Name", 3)].strip().upper() if "Name" in col_idx else ""
        token = r[col_idx.get("ExchangeInstrumentID", 1)].strip() if "ExchangeInstrumentID" in col_idx else ""
        raw_exp = r[col_idx.get("ContractExpiration", 16)].strip() if "ContractExpiration" in col_idx else ""
        raw_strike = r[col_idx.get("StrikePrice", 17)].strip() if "StrikePrice" in col_idx else ""
        raw_opt = r[col_idx.get("OptionType", 18)].strip() if "OptionType" in col_idx else ""
        display_name = r[col_idx.get("Description", 4)].strip() if "Description" in col_idx else ""

        if not name or not token or not raw_exp or raw_exp == "1":
            continue

        exp_str = parse_date_to_ddmmmyy(raw_exp)
        if not exp_str:
            continue

        # OptionType: 1=FUT, 3=CE, 4=PE
        opt_type_int = 1
        try:
            opt_type_int = int(float(raw_opt))
        except (ValueError, TypeError):
            pass

        lotsize = int(float(r[col_idx.get("LotSize", 12)])) if "LotSize" in col_idx and len(r) > col_idx["LotSize"] and r[col_idx["LotSize"]].strip() else 1
        tick_size = float(r[col_idx.get("TickSize", 11)]) if "TickSize" in col_idx and len(r) > col_idx["TickSize"] and r[col_idx["TickSize"]].strip() else 0.05
        freeze_qty = int(float(r[col_idx.get("FreezeQty", 10)])) if "FreezeQty" in col_idx and len(r) > col_idx["FreezeQty"] and r[col_idx["FreezeQty"]].strip() else 0

        if opt_type_int == 1:
            canonical_sym = f"{name}{exp_str}FUT"
            records.append({
                "symbol": canonical_sym,
                "brsymbol": display_name or canonical_sym,
                "name": name,
                "exchange": oa_exchange,
                "brexchange": exchange_segment,
                "token": token,
                "expiry": exp_str,
                "strike": 0.0,
                "lotsize": lotsize,
                "instrumenttype": "FUT",
                "tick_size": tick_size,
                "freeze_qty": freeze_qty,
            })
        else:
            strike_str = format_strike(raw_strike)
            opt_suffix = "CE" if opt_type_int == 3 else "PE"
            canonical_sym = f"{name}{exp_str}{strike_str}{opt_suffix}"
            records.append({
                "symbol": canonical_sym,
                "brsymbol": display_name or canonical_sym,
                "name": name,
                "exchange": oa_exchange,
                "brexchange": exchange_segment,
                "token": token,
                "expiry": exp_str,
                "strike": float(raw_strike) if raw_strike else 0.0,
                "lotsize": lotsize,
                "instrumenttype": opt_suffix,
                "tick_size": tick_size,
                "freeze_qty": freeze_qty,
            })

    return records


def parse_index_list_records(index_list: List[str], exchange_segment: int) -> List[Dict[str, Any]]:
    """
    Parses Symphony XTS /instruments/indexlist response:
    Items format: "NIFTY 50_26000" or "SENSEX_1"
    """
    records = []
    is_nse = (exchange_segment == 1)
    target_exchange = "NSE_INDEX" if is_nse else "BSE_INDEX"

    for entry in index_list:
        if "_" not in entry:
            continue
        raw_name, token = entry.rsplit("_", 1)
        clean_name = raw_name.strip().upper()

        if is_nse:
            sym = NSE_INDEX_SYMBOL_MAP.get(clean_name, clean_name).replace(" ", "")
        else:
            sym = BSE_INDEX_SYMBOL_MAP.get(clean_name, clean_name).replace(" ", "")

        records.append({
            "symbol": sym,
            "brsymbol": entry,
            "name": sym,
            "exchange": target_exchange,
            "brexchange": target_exchange,
            "token": token.strip(),
            "expiry": "",
            "strike": 0.0,
            "lotsize": 1,
            "instrumenttype": "INDEX",
            "tick_size": 0.05,
            "freeze_qty": 0,
        })

    return records


def save_records_to_sqlite(records: List[Dict[str, Any]], db_path: str):
    """
    Atomically writes parsed records into an indexed SQLite master_contract.db file.
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    temp_db_path = f"{db_path}.tmp.{os.getpid()}"

    if os.path.exists(temp_db_path):
        try:
            os.remove(temp_db_path)
        except OSError:
            pass

    conn = sqlite3.connect(temp_db_path)
    with conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

        conn.execute("""
            CREATE TABLE symtoken (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                brsymbol TEXT,
                name TEXT,
                exchange TEXT NOT NULL,
                brexchange TEXT,
                token TEXT NOT NULL,
                expiry TEXT,
                strike REAL DEFAULT 0.0,
                lotsize INTEGER DEFAULT 1,
                instrumenttype TEXT DEFAULT 'EQ',
                tick_size REAL DEFAULT 0.05,
                freeze_qty INTEGER DEFAULT 0
            )
        """)

        # Fast bulk insert
        conn.executemany("""
            INSERT INTO symtoken (
                symbol, brsymbol, name, exchange, brexchange, token,
                expiry, strike, lotsize, instrumenttype, tick_size, freeze_qty
            ) VALUES (
                :symbol, :brsymbol, :name, :exchange, :brexchange, :token,
                :expiry, :strike, :lotsize, :instrumenttype, :tick_size, :freeze_qty
            )
        """, records)

        # Build high-performance lookup indexes
        conn.execute("CREATE INDEX idx_symtoken_sym_exch ON symtoken (symbol, exchange)")
        conn.execute("CREATE INDEX idx_symtoken_tok_exch ON symtoken (token, exchange)")
        conn.execute("CREATE INDEX idx_symtoken_tok_brexch ON symtoken (token, brexchange)")
        conn.execute("CREATE INDEX idx_symtoken_name ON symtoken (name)")

    conn.close()

    # Atomic rename replaces existing DB smoothly without reader interruption
    os.replace(temp_db_path, db_path)
    logger.info(f"Successfully compiled {len(records)} symbols into SQLite master contract DB at {db_path}")


def get_xts_market_data_token(base_url: str, app_key: str, app_secret: str) -> Optional[str]:
    """Authenticates with Symphony XTS Market Data API to obtain token."""
    try:
        url = f"{base_url.rstrip('/')}/marketdata/auth/login"
        payload = {"appKey": app_key, "secretKey": app_secret, "source": "WebAPI"}
        resp = httpx.post(url, json=payload, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("result", {}).get("token")
        logger.error(f"XTS MD Login failed: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Exception during XTS MD token acquisition: {e}")
    return None


def download_and_refresh_master_contracts(
    base_url: Optional[str] = None,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrates downloading master CSVs from Symphony XTS and saving them to master_contract.db.
    """
    target_db = db_path or os.environ.get("MASTER_CONTRACT_DB_PATH") or os.path.join(
        os.path.dirname(__file__), "..", "data", "master_contract.db"
    )

    url = base_url or os.environ.get("MASTER_XTS_URL") or "https://xts.acagarwal.com"
    key = app_key or os.environ.get("MASTER_XTS_API_KEY")
    secret = app_secret or os.environ.get("MASTER_XTS_API_SECRET")

    # Fallback to first active tenant if credentials not in env
    if not key or not secret:
        try:
            import database
            import security
            tenants = database.get_all_tenants()
            for t in tenants:
                if t.get("is_active", 1):
                    creds = security.decrypt_credentials(t["encrypted_credentials"])
                    key = creds.get("API_KEY")
                    secret = creds.get("API_SECRET")
                    if key and secret:
                        logger.info(f"Using XTS credentials from active tenant '{t['name']}' for master contract refresh")
                        break
        except Exception as e:
            logger.warning(f"Could not load tenant fallback credentials: {e}")

    if not key or not secret:
        msg = "No Symphony XTS API credentials available for master contract download"
        logger.warning(msg)
        return {"status": "error", "message": msg, "count": 0}

    token = get_xts_market_data_token(url, key, secret)
    if not token:
        msg = "Failed to obtain Market Data session token from XTS"
        logger.error(msg)
        return {"status": "error", "message": msg, "count": 0}

    headers = {"Authorization": token, "Content-Type": "application/json"}
    client = httpx.Client(timeout=45.0)

    segments = ["NSECM", "NSEFO", "BSECM", "BSEFO", "MCXFO", "NSECD"]
    all_records = []

    for seg in segments:
        try:
            logger.info(f"Downloading master contract for segment {seg}...")
            resp = client.post(
                f"{url.rstrip('/')}/marketdata/instruments/master",
                headers=headers,
                json={"exchangeSegmentList": [seg]},
            )
            if resp.status_code != 200:
                logger.warning(f"Failed to download {seg}: HTTP {resp.status_code}")
                continue

            result_str = resp.json().get("result", "")
            if not result_str:
                continue

            rows = [line.split("|") for line in result_str.strip().split("\n") if line.strip()]
            if seg in ("NSECM", "BSECM"):
                parsed = parse_equity_csv_rows(rows, seg)
            else:
                parsed = parse_derivatives_csv_rows(rows, seg)

            all_records.extend(parsed)
            logger.info(f"Parsed {len(parsed)} instruments from {seg}")
        except Exception as e:
            logger.error(f"Error processing segment {seg}: {e}")

    # Fetch Index Lists
    for exch_seg in (1, 11):
        try:
            resp = client.get(
                f"{url.rstrip('/')}/marketdata/instruments/indexlist?exchangeSegment={exch_seg}",
                headers=headers,
            )
            if resp.status_code == 200:
                indices = resp.json().get("result", {}).get("indexList", [])
                parsed_idx = parse_index_list_records(indices, exch_seg)
                all_records.extend(parsed_idx)
                logger.info(f"Parsed {len(parsed_idx)} indices for segment {exch_seg}")
        except Exception as e:
            logger.error(f"Error fetching indexlist {exch_seg}: {e}")

    client.close()

    if all_records:
        save_records_to_sqlite(all_records, target_db)
        return {"status": "success", "count": len(all_records), "db_path": target_db}
    else:
        return {"status": "error", "message": "Zero records were fetched from XTS", "count": 0}
