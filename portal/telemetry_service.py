import os
import asyncio
import httpx
import logging
from contextlib import closing
from database import get_db_connection
import docker_manager
import security

logger = logging.getLogger(__name__)

CADDY_PROXY_BASE = os.environ.get("CADDY_PROXY_BASE", "http://caddy/internal-client-proxy")

def build_client_telemetry_dict(
    tenant_id: str,
    name: str,
    status: str = "HEALTHY",
    docker_status: str = "HEALTHY",
    healthy: bool = False,
    paper_mode: bool = False,
    client_id: str = "",
    unrealized_mtm: float = 0.0,
    realized_pnl: float = 0.0,
    net_mtm: float = 0.0,
    positions: list = None,
    all_positions: list = None,
    closed_positions: list = None,
    holdings: dict = None,
    available_margin: float = 0.0,
    margin_used: float = 0.0,
    total_collateral: float = 0.0,
    net_margin_available: float = 0.0,
    total_account_value: float = 0.0,
    margin_pct: float = 0.0,
    notional_today: float = 0.0,
    notional_cap: float = 10000000.0,
    notional_pct: float = 0.0,
    broker_orders: list = None,
    broker_trades: list = None,
    recent_signals: list = None,
    error: str = None
) -> dict:
    pos_list = positions or []
    all_pos_list = all_positions or pos_list
    closed_pos_list = closed_positions or [p for p in all_pos_list if p.get("quantity", 0) == 0]
    holdings_dict = holdings or {
        "invested_value": 0.0, "current_value": 0.0, "overall_pnl": 0.0,
        "overall_pnl_pct": 0.0, "day_pnl": 0.0, "day_pnl_pct": 0.0,
        "holdings_count": 0, "holdings": []
    }
    return {
        "id": tenant_id or "",
        "name": name or tenant_id or "",
        "client_id": client_id or tenant_id or "",
        "status": status or "UNKNOWN",
        "docker_status": docker_status or "UNKNOWN",
        "healthy": bool(healthy),
        "paper_mode": bool(paper_mode),
        "unrealized_mtm": float(unrealized_mtm or 0.0),
        "realized_pnl": float(realized_pnl or 0.0),
        "net_mtm": float(net_mtm or 0.0),
        "positions_count": len(pos_list),
        "all_positions_count": len(all_pos_list),
        "positions": pos_list,
        "all_positions": all_pos_list,
        "closed_positions": closed_pos_list,
        "holdings": holdings_dict,
        "available_margin": float(available_margin or 0.0),
        "margin_used": float(margin_used or 0.0),
        "total_collateral": float(total_collateral or 0.0),
        "net_margin_available": float(net_margin_available or 0.0),
        "total_account_value": float(total_account_value or 0.0),
        "margin_pct": float(margin_pct or 0.0),
        "notional_today": float(notional_today or 0.0),
        "notional_cap": float(notional_cap or 10000000.0),
        "notional_pct": float(notional_pct or 0.0),
        "recent_signals": recent_signals or [],
        "broker_orders": broker_orders or [],
        "broker_trades": broker_trades or [],
        "error": error
    }

async def fetch_single_client_telemetry(client_session: httpx.AsyncClient, tenant: dict) -> dict:
    t_id = tenant["id"]
    t_name = tenant.get("name", t_id)
    t_status = tenant.get("status", "ACTIVE")
    configured_paper = bool(tenant.get("paper_trade_mode", False))
    docker_st = docker_manager.get_container_status(t_id)

    broker_client_id = tenant.get("client_id")
    if not broker_client_id:
        enc = tenant.get("encrypted_payload")
        if not enc:
            with closing(get_db_connection()) as conn:
                crow = conn.execute("SELECT encrypted_payload FROM tenant_credentials WHERE tenant_id=?", (t_id,)).fetchone()
                if crow:
                    enc = crow["encrypted_payload"]
        if enc:
            try:
                broker_client_id = security.decrypt_credentials(enc).get("CLIENT_ID", t_id)
            except Exception:
                broker_client_id = t_id
    if not broker_client_id:
        broker_client_id = t_id

    if t_status == "PAUSED" or docker_st == "STOPPED":
        return build_client_telemetry_dict(
            tenant_id=t_id,
            name=t_name,
            client_id=broker_client_id,
            status="PAUSED",
            docker_status=docker_st,
            healthy=False,
            paper_mode=configured_paper,
            error="Container Paused / Stopped"
        )

    port = docker_manager.get_tenant_port(t_id)
    url_caddy = f"{CADDY_PROXY_BASE}/{t_id}/internal/telemetry"
    url_docker = f"http://xts_client_{t_id}:8000/internal/telemetry"
    url_local = f"http://127.0.0.1:{port}/internal/telemetry"

    headers = {}
    internal_token = os.environ.get("INTERNAL_AUTH_TOKEN", "").strip()
    if internal_token:
        headers["X-Internal-Token"] = internal_token

    data = None
    for target_url in [url_local, url_caddy, url_docker]:
        try:
            resp = await client_session.get(target_url, headers=headers, timeout=1.5)
            if resp.status_code == 200:
                data = resp.json()
                break
        except Exception:
            pass

    if not data:
        return build_client_telemetry_dict(
            tenant_id=t_id,
            name=t_name,
            client_id=broker_client_id,
            status="UNREACHABLE" if t_status == "ACTIVE" else "PAUSED",
            docker_status=docker_st,
            healthy=False,
            paper_mode=configured_paper,
            error="Telemetry Timeout / Unreachable"
        )

    health = data.get("health", {})
    pos = data.get("positions", {})
    margin = data.get("margin", {})
    recent = data.get("recent_signals", [])
    broker_orders = data.get("broker_orders", [])
    broker_trades = data.get("broker_trades", [])

    notional_today = float(health.get("notional_today", 0.0))
    notional_cap = float(health.get("notional_cap", 10000000.0))
    notional_pct = min(100.0, round((notional_today / notional_cap) * 100, 1)) if notional_cap > 0 else 0.0

    is_paper = bool(health.get("paper_trade_mode")) if ("paper_trade_mode" in health) else configured_paper
    if not health.get("interactive_token_active") and not is_paper:
        auth_err = health.get("auth_error") or ""
        if "e-user-0013" in auth_err or "trusted IP" in auth_err:
            status = "IP_NOT_WHITELISTED"
        else:
            status = "AUTH_FAILED"
    elif not health.get("cache_healthy"):
        status = "CACHE_STALE"
    else:
        status = "HEALTHY"

    # Margin Metrics
    avail_margin = float(margin.get("available_margin", 0.0))
    margin_used = float(margin.get("margin_used", 0.0))
    collateral = float(margin.get("total_collateral", 0.0))
    net_avail = float(margin.get("net_margin_available", 0.0))
    tot_val = float(margin.get("total_account_value", 0.0))
    margin_pct = min(100.0, round((margin_used / tot_val) * 100, 1)) if tot_val > 0 else 0.0

    holdings = data.get("holdings", {})
    all_pos = pos.get("all_positions", pos.get("positions", []))
    closed_pos = pos.get("closed_positions", [p for p in all_pos if p.get("quantity", 0) == 0])

    return build_client_telemetry_dict(
        tenant_id=t_id,
        name=t_name,
        client_id=broker_client_id,
        status=status,
        docker_status=docker_st,
        healthy=bool(health.get("cache_healthy")),
        paper_mode=is_paper,
        unrealized_mtm=float(pos.get("unrealized_mtm", 0.0)),
        realized_pnl=float(pos.get("realized_pnl", 0.0)),
        net_mtm=float(pos.get("net_mtm", 0.0)),
        positions=pos.get("positions", []),
        all_positions=all_pos,
        closed_positions=closed_pos,
        holdings=holdings,
        available_margin=avail_margin,
        margin_used=margin_used,
        total_collateral=collateral,
        net_margin_available=net_avail,
        total_account_value=tot_val,
        margin_pct=margin_pct,
        notional_today=notional_today,
        notional_cap=notional_cap,
        notional_pct=notional_pct,
        broker_orders=broker_orders,
        broker_trades=broker_trades,
        recent_signals=recent,
        error=None
    )

async def get_single_client_telemetry(tenant_id: str) -> dict:
    """Fetches telemetry for a single tenant by tenant_id."""
    with closing(get_db_connection()) as conn:
        row = conn.execute("""
            SELECT t.id, t.name, t.status, r.paper_trade_mode, c.encrypted_payload
            FROM tenants t
            LEFT JOIN tenant_risk_limits r ON t.id = r.tenant_id
            LEFT JOIN tenant_credentials c ON t.id = c.tenant_id
            WHERE t.id = ?
        """, (tenant_id,)).fetchone()
    
    if not row:
        return build_client_telemetry_dict(tenant_id=tenant_id, name=tenant_id, status="NOT_FOUND", error="Tenant not found in database")
    
    tenant_dict = dict(row)
    async with httpx.AsyncClient() as client:
        return await fetch_single_client_telemetry(client, tenant_dict)

async def aggregate_all_telemetry() -> dict:
    with closing(get_db_connection()) as conn:
        tenants = [dict(r) for r in conn.execute("""
            SELECT t.id, t.name, t.status, r.paper_trade_mode, c.encrypted_payload 
            FROM tenants t 
            LEFT JOIN tenant_risk_limits r ON t.id = r.tenant_id
            LEFT JOIN tenant_credentials c ON t.id = c.tenant_id
        """).fetchall()]

    async with httpx.AsyncClient() as client:
        tasks = [fetch_single_client_telemetry(client, t) for t in tenants]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    client_data = []
    total_unrealized = 0.0
    total_realized = 0.0
    active_count = 0
    healthy_count = 0

    for idx, res in enumerate(results):
        if isinstance(res, Exception):
            t = tenants[idx]
            res = build_client_telemetry_dict(
                tenant_id=t["id"],
                name=t["name"],
                status="ERROR",
                docker_status="UNKNOWN",
                healthy=False,
                paper_mode=bool(t.get("paper_trade_mode", False)),
                error=str(res)
            )
        client_data.append(res)
        total_unrealized += float(res.get("unrealized_mtm") or 0.0)
        total_realized += float(res.get("realized_pnl") or 0.0)
        if res.get("status") in ("HEALTHY", "DEGRADED"):
            active_count += 1
        if res.get("healthy"):
            healthy_count += 1

    return {
        "summary": {
            "total_clients": len(tenants),
            "active_clients": active_count,
            "healthy_clients": healthy_count,
            "total_unrealized_mtm": round(total_unrealized, 2),
            "total_realized_pnl": round(total_realized, 2),
            "total_net_mtm": round(total_unrealized + total_realized, 2),
        },
        "clients": client_data
    }

def aggregate_all_signals(search: str = "", client_filter: str = "", status_filter: str = "", limit: int = 100) -> list:
    """Aggregates all trading signals across all tenant SQLite databases."""
    import sqlite3
    import datetime
    all_signals = []
    
    with closing(get_db_connection()) as conn:
        tenants = [dict(r) for r in conn.execute("SELECT id, name FROM tenants").fetchall()]

    tenant_map = {t["id"]: t["name"] for t in tenants}
    data_root = docker_manager.get_client_data_root()

    for t_id, t_name in tenant_map.items():
        if client_filter and client_filter != t_id:
            continue
            
        sig_db = os.path.join(data_root, t_id, "signals.db")
        if not os.path.exists(sig_db):
            continue

        try:
            with sqlite3.connect(sig_db, timeout=2.0) as s_conn:
                s_conn.row_factory = sqlite3.Row
                rows = s_conn.execute("""
                    SELECT id, received_at, payload, status, result, updated_at
                    FROM signals ORDER BY received_at DESC LIMIT ?
                """, (limit,)).fetchall()

                for r in rows:
                    import json
                    payload = {}
                    try: payload = json.loads(r["payload"]) if r["payload"] else {}
                    except Exception: pass

                    result = {}
                    try: result = json.loads(r["result"]) if r["result"] else {}
                    except Exception: pass
                    
                    symbol = payload.get("symbol", "")
                    action = payload.get("action", "")
                    qty = payload.get("quantity", 0)
                    price = payload.get("price", 0.0)
                    status = r["status"] or "unknown"
                    
                    order_ref = (
                        payload.get("order_ref")
                        or (result.get("_audit", {}).get("order_ref") if isinstance(result, dict) and isinstance(result.get("_audit"), dict) else None)
                        or (result.get("result", {}).get("OrderUniqueIdentifier") if isinstance(result, dict) and isinstance(result.get("result"), dict) else None)
                        or (result.get("order_ref") if isinstance(result, dict) else None)
                        or ""
                    )
                    err_msg = ""
                    if isinstance(result, dict):
                        err_msg = str(
                            result.get("description")
                            or result.get("error")
                            or result.get("message")
                            or (result.get("result", {}).get("description") if isinstance(result.get("result"), dict) else None)
                            or (result.get("result", {}).get("RejectReason") if isinstance(result.get("result"), dict) else None)
                            or (result.get("result", {}).get("OrderRejectReason") if isinstance(result.get("result"), dict) else None)
                            or ""
                        )
                        err_code = str(result.get("code") or "")
                        if err_code and err_code not in err_msg and err_msg:
                            err_msg = f"[{err_code}] {err_msg}"
                        elif err_code and not err_msg:
                            err_msg = f"Error: {err_code}"

                    # Filter by status
                    if status_filter and status != status_filter:
                        continue

                    # Filter by search string
                    if search:
                        s_lower = search.lower()
                        if s_lower not in symbol.lower() and s_lower not in str(order_ref).lower() and s_lower not in t_name.lower():
                            continue

                    # IST formatted time
                    rec_at = float(r["received_at"] or time.time())
                    dt = datetime.datetime.fromtimestamp(rec_at, datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
                    time_str = dt.strftime('%Y-%m-%d %H:%M:%S IST')

                    all_signals.append({
                        "id": r["id"],
                        "tenant_id": t_id,
                        "client_name": t_name,
                        "timestamp": time_str,
                        "received_at": rec_at,
                        "symbol": symbol,
                        "action": action,
                        "quantity": qty,
                        "price": price,
                        "status": status,
                        "order_ref": order_ref,
                        "error_message": err_msg,
                        "raw_payload": r["payload"]
                    })
        except Exception as e:
            logger.error(f"Error querying signals for {t_id}: {e}")

    # Sort descending by timestamp
    all_signals.sort(key=lambda x: x.get("received_at", 0), reverse=True)
    return all_signals[:limit]

async def panic_single_client(tenant_id: str, webhook_secret: str) -> dict:
    port = docker_manager.get_tenant_port(tenant_id)
    url_caddy = f"{CADDY_PROXY_BASE}/{tenant_id}/panic"
    url_docker = f"http://xts_client_{tenant_id}:8000/panic"
    url_local = f"http://127.0.0.1:{port}/panic"
    payload = {"secret": webhook_secret}

    async with httpx.AsyncClient() as client:
        for target_url in [url_local, url_caddy, url_docker]:
            try:
                resp = await client.post(target_url, json=payload, timeout=5.0)
                if resp.status_code in (200, 401):
                    return resp.json()
            except Exception:
                pass
    return {"status": "error", "message": "Failed to reach client panic endpoint"}

async def panic_all_active_clients() -> dict:
    with closing(get_db_connection()) as conn:
        tenants = [dict(r) for r in conn.execute("""
            SELECT t.id, c.encrypted_payload 
            FROM tenants t 
            LEFT JOIN tenant_credentials c ON t.id = c.tenant_id 
            WHERE t.status='ACTIVE'
        """).fetchall()]

    tasks = []
    async with httpx.AsyncClient() as client:
        for t in tenants:
            t_id = t["id"]
            enc = t.get("encrypted_payload")
            secret = ""
            if enc:
                try:
                    secret = security.decrypt_credentials(enc).get("WEBHOOK_SECRET", "")
                except Exception:
                    secret = ""
            tasks.append(panic_single_client(t_id, secret))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    summary = []
    for idx, res in enumerate(results):
        t_id = tenants[idx]["id"]
        if isinstance(res, Exception):
            summary.append({"tenant_id": t_id, "status": "error", "message": str(res)})
        else:
            summary.append({"tenant_id": t_id, "status": "success", "result": res})
    return {"status": "completed", "total_clients": len(tenants), "results": summary}

def generate_trade_book_csv(trades: list, tenant_id: str = "") -> str:
    """
    Generates a standard Indian capital markets Contract Note & Trade Book CSV.
    Calculates gross turnover, STT/CTT, exchange turnover charges, SEBI fees, stamp duty, GST, and total statutory deductions.
    """
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

    # Header Row
    writer.writerow([
        "Trade ID",
        "Order ID",
        "Execution Time (IST)",
        "Tenant ID",
        "Exchange Segment",
        "Trading Symbol",
        "Side",
        "Quantity",
        "Price (INR)",
        "Gross Turnover (INR)",
        "Estimated STT / CTT (INR)",
        "Estimated Exchange Charges (INR)",
        "Estimated SEBI Fees (INR)",
        "Estimated Stamp Duty (INR)",
        "Estimated GST 18% (INR)",
        "Estimated Total Statutory Charges (INR)"
    ])

    for tr in trades:
        trade_id = tr.get("TradeID") or tr.get("tradeID") or tr.get("id") or "N/A"
        app_order_id = tr.get("AppOrderID") or tr.get("appOrderID") or "N/A"
        exec_time = tr.get("OrderExecutionTime") or tr.get("ExecutionTime") or tr.get("time") or "N/A"
        seg = str(tr.get("ExchangeSegment") or tr.get("exchangeSegment") or "MCXFO").upper()
        sym = tr.get("TradingSymbol") or tr.get("tradingSymbol") or tr.get("symbol") or "N/A"
        side = str(tr.get("OrderSide") or tr.get("orderSide") or tr.get("action") or "BUY").upper()
        
        try:
            qty = abs(int(tr.get("TradedQuantity") or tr.get("tradedQuantity") or tr.get("quantity") or 0))
            price = abs(float(tr.get("TradePrice") or tr.get("tradePrice") or tr.get("price") or 0.0))
        except (ValueError, TypeError):
            qty = 0
            price = 0.0

        # Multiplier determination for commodities
        sym_upper = sym.upper()
        if "CRUDEOILM" in sym_upper:
            mult = 10
        elif "CRUDEOIL" in sym_upper:
            mult = 100
        elif "NATURALGASM" in sym_upper:
            mult = 250
        elif "NATURALGAS" in sym_upper:
            mult = 1250
        elif "SILVERM" in sym_upper:
            mult = 5
        elif "SILVERMIC" in sym_upper or "SILVER100" in sym_upper:
            mult = 1
        elif "SILVER" in sym_upper:
            mult = 30
        elif "GOLDM" in sym_upper:
            mult = 10
        elif "GOLDPETAL" in sym_upper:
            mult = 1
        elif "GOLD" in sym_upper:
            mult = 100
        else:
            mult = 1

        gross_turnover = round(price * qty * mult, 2)

        # Statutory Charges Estimation
        if "MCX" in seg:
            # MCX CTT: 0.01% on Sell
            stt = round(gross_turnover * 0.0001, 2) if side == "SELL" else 0.0
            exch_fee = round(gross_turnover * 0.000021, 2) # 0.0021%
            stamp_duty = round(gross_turnover * 0.00003, 2) if side == "BUY" else 0.0 # 0.003% on buy
        elif "FO" in seg or "FUT" in seg:
            # NSE/BSE Futures: 0.02% on Sell
            stt = round(gross_turnover * 0.0002, 2) if side == "SELL" else 0.0
            exch_fee = round(gross_turnover * 0.0000345, 2)
            stamp_duty = round(gross_turnover * 0.00003, 2) if side == "BUY" else 0.0
        else:
            # Intraday Cash: 0.025% on Sell
            stt = round(gross_turnover * 0.00025, 2) if side == "SELL" else 0.0
            exch_fee = round(gross_turnover * 0.0000345, 2)
            stamp_duty = round(gross_turnover * 0.00003, 2) if side == "BUY" else 0.0

        sebi_fee = round(gross_turnover * 0.000001, 2) # ₹10 per crore (0.0001%)
        gst = round((exch_fee + sebi_fee) * 0.18, 2) # 18% GST on exchange & SEBI charges
        total_statutory = round(stt + exch_fee + sebi_fee + stamp_duty + gst, 2)

        writer.writerow([
            trade_id,
            app_order_id,
            exec_time,
            tenant_id or tr.get("tenant_id", "ALL"),
            seg,
            sym,
            side,
            qty,
            f"{price:.2f}",
            f"{gross_turnover:.2f}",
            f"{stt:.2f}",
            f"{exch_fee:.2f}",
            f"{sebi_fee:.2f}",
            f"{stamp_duty:.2f}",
            f"{gst:.2f}",
            f"{total_statutory:.2f}"
        ])

    return output.getvalue()
