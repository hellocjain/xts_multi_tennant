import os
import asyncio
import httpx
import logging
from contextlib import closing
from database import get_db_connection
import docker_manager

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
    available_margin: float = 0.0,
    margin_used: float = 0.0,
    total_collateral: float = 0.0,
    net_margin_available: float = 0.0,
    total_account_value: float = 0.0,
    margin_pct: float = 0.0,
    notional_today: float = 0.0,
    notional_cap: float = 10000000.0,
    notional_pct: float = 0.0,
    recent_signals: list = None,
    error: str = None
) -> dict:
    pos_list = positions or []
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
        "positions": pos_list,
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

    data = None
    for target_url in [url_local, url_caddy, url_docker]:
        try:
            resp = await client_session.get(target_url, timeout=1.5)
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
        available_margin=avail_margin,
        margin_used=margin_used,
        total_collateral=collateral,
        net_margin_available=net_avail,
        total_account_value=tot_val,
        margin_pct=margin_pct,
        notional_today=notional_today,
        notional_cap=notional_cap,
        notional_pct=notional_pct,
        recent_signals=recent,
        error=None
    )

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
        total_unrealized += res.get("unrealized_mtm", 0.0)
        total_realized += res.get("realized_pnl", 0.0)
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
                    order_ref = result.get("order_ref", "") if isinstance(result, dict) else ""
                    err_msg = result.get("error", "") if isinstance(result, dict) else ""

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
        tenants = conn.execute("SELECT id FROM tenants WHERE status='ACTIVE'").fetchall()

    tasks = []
    async with httpx.AsyncClient() as client:
        for t in tenants:
            t_id = t["id"]
            # In paper/internal mode, panic endpoint can be triggered with empty secret or fetched secret
            tasks.append(panic_single_client(t_id, ""))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    summary = []
    for idx, res in enumerate(results):
        t_id = tenants[idx]["id"]
        if isinstance(res, Exception):
            summary.append({"tenant_id": t_id, "status": "error", "error": str(res)})
        else:
            summary.append({"tenant_id": t_id, "result": res})
    return {"total_panicked": len(tenants), "results": summary}
