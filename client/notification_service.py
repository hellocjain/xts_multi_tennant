"""
notification_service.py - Multi-Channel Trade & Telemetry Notification Broadcaster.
Provides non-blocking async dispatch of trade executions, rejections, strategy signals,
and daily MTM summaries across Telegram Bot API, Discord, and Webhooks.
"""
import os
import time
import json
import logging
import datetime
import httpx
from typing import Dict, Any, Optional

import config

logger = logging.getLogger(__name__)

def _get_ist_timestamp() -> str:
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M:%S")

async def send_telegram_alert(
    bot_token: str,
    chat_id: str,
    message: str,
    parse_mode: str = "HTML"
) -> bool:
    """
    Dispatches a message to a Telegram chat using official Telegram Bot API.
    Non-blocking, fail-safe with 5-second timeout.
    """
    token = str(bot_token or "").strip()
    target_chat = str(chat_id or "").strip()

    if not token or not target_chat:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info(f"Telegram alert dispatched to chat {target_chat}")
                return True
            else:
                logger.warning(f"Telegram API returned status {resp.status_code}: {resp.text}")
                return False
    except Exception as e:
        logger.warning(f"Failed to dispatch Telegram alert: {e}")
        return False

async def send_discord_alert(
    webhook_url: str,
    message: str
) -> bool:
    """Dispatches a formatted message to Discord webhook."""
    url = str(webhook_url or "").strip()
    if not url or not url.startswith("http"):
        return False

    # Strip basic HTML tags for Discord markdown
    clean_msg = message.replace("<b>", "**").replace("</b>", "**")
    clean_msg = clean_msg.replace("<code>", "`").replace("</code>", "`")
    clean_msg = clean_msg.replace("<pre>", "```\n").replace("</pre>", "\n```")

    payload = {"content": clean_msg}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code in (200, 204)
    except Exception as e:
        logger.warning(f"Failed to dispatch Discord alert: {e}")
        return False

async def send_webhook_alert(
    webhook_url: str,
    payload: Dict[str, Any]
) -> bool:
    """Dispatches arbitrary JSON payload to generic ops webhook."""
    url = str(webhook_url or "").strip()
    if not url or not url.startswith("http"):
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code in (200, 201, 202, 204)
    except Exception as e:
        logger.warning(f"Failed to dispatch generic webhook alert: {e}")
        return False

async def notify_order_execution(
    tenant_id: str,
    order_data: Dict[str, Any],
    status: str,
    execution_price: float = 0.0,
    app_order_id: str = ""
) -> Dict[str, Any]:
    """
    Builds a formatted receipt and dispatches it asynchronously to all configured channels.
    """
    symbol = str(order_data.get("symbol") or "UNKNOWN").upper()
    action = str(order_data.get("action") or "BUY").upper()
    quantity = order_data.get("quantity") or 0
    price = execution_price if execution_price > 0 else float(order_data.get("price") or 0.0)
    order_id = app_order_id or order_data.get("orderid") or order_data.get("order_ref") or "N/A"
    strategy = order_data.get("strategy") or order_data.get("order_ref") or "Manual Order"
    mode = getattr(config, "TRADING_MODE", "PAPER" if getattr(config, "PAPER_TRADE_MODE", True) else "LIVE")
    if getattr(config, "ANALYZER_MODE", False):
        mode = "ANALYZER"

    norm_status = str(status or "").upper()
    if norm_status in ("COMPLETE", "SUCCESS", "FILLED"):
        header = "🟢 <b>ORDER EXECUTED</b>"
    elif norm_status in ("REJECTED", "ERROR", "CANCELLED", "FAILED"):
        header = "🔴 <b>ORDER REJECTED</b>"
    elif norm_status in ("ANALYZED", "SIGNAL"):
        header = "🟣 <b>SIGNAL ANALYZED (NO RISK)</b>"
    else:
        header = "⚡ <b>ORDER STATUS UPDATE</b>"

    timestamp = _get_ist_timestamp()

    msg_lines = [
        f"{header}",
        f"<b>Account:</b> <code>{tenant_id}</code> [{mode}]",
        f"<b>Instrument:</b> <b>{symbol}</b>",
        f"<b>Action:</b> <b>{action}</b> {quantity} qty @ ₹{price:,.2f}",
        f"<b>Order ID:</b> <code>{order_id}</code>",
        f"<b>Status:</b> <b>{norm_status}</b>",
        f"<b>Strategy:</b> {strategy}",
        f"<b>Time:</b> {timestamp} IST"
    ]

    reason = order_data.get("rejection_reason") or order_data.get("error") or order_data.get("message")
    if reason and norm_status in ("REJECTED", "ERROR", "FAILED"):
        msg_lines.append(f"<b>Reason:</b> <i>{reason}</i>")

    formatted_msg = "\n".join(msg_lines)

    bot_token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    discord_url = getattr(config, "DISCORD_WEBHOOK_URL", "")
    ops_url = getattr(config, "OPS_ALERT_WEBHOOK_URL", "")

    dispatched = {
        "telegram": False,
        "discord": False,
        "webhook": False
    }

    if bot_token and chat_id:
        dispatched["telegram"] = await send_telegram_alert(bot_token, chat_id, formatted_msg)

    if discord_url:
        dispatched["discord"] = await send_discord_alert(discord_url, formatted_msg)

    if ops_url:
        dispatched["webhook"] = await send_webhook_alert(ops_url, {
            "type": "ORDER_EXECUTION",
            "tenant_id": tenant_id,
            "order_id": order_id,
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "price": price,
            "status": norm_status,
            "mode": mode,
            "timestamp": timestamp
        })

    return {
        "status": "success",
        "message": formatted_msg,
        "dispatched": dispatched
    }

async def notify_daily_mtm_summary(
    tenant_id: str,
    net_mtm: float,
    realized_pnl: float,
    open_positions_count: int
) -> Dict[str, Any]:
    """
    Sends a formatted daily market close summary card.
    """
    pnl_emoji = "🟢" if net_mtm >= 0 else "🔴"
    timestamp = _get_ist_timestamp()

    formatted_msg = (
        f"📊 <b>DAILY TRADING SUMMARY</b>\n"
        f"<b>Tenant:</b> <code>{tenant_id}</code>\n"
        f"<b>Net MTM:</b> {pnl_emoji} ₹{net_mtm:,.2f}\n"
        f"<b>Realized P&L:</b> ₹{realized_pnl:,.2f}\n"
        f"<b>Open Positions:</b> {open_positions_count}\n"
        f"<b>Status:</b> Market Closed\n"
        f"<b>Timestamp:</b> {timestamp} IST"
    )

    bot_token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")

    sent = False
    if bot_token and chat_id:
        sent = await send_telegram_alert(bot_token, chat_id, formatted_msg)

    return {
        "status": "success",
        "sent": sent,
        "message": formatted_msg
    }

async def send_test_alert(
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Dispatches a test notification ping to verify bot credentials.
    """
    token = bot_token or getattr(config, "TELEGRAM_BOT_TOKEN", "")
    chat = chat_id or getattr(config, "TELEGRAM_CHAT_ID", "")

    if not token or not chat:
        return {
            "status": "error",
            "message": "Missing Telegram Bot Token or Chat ID. Please configure credentials first."
        }

    timestamp = _get_ist_timestamp()
    test_msg = (
        f"🚀 <b>OpenAlgo Telemetry Test</b>\n\n"
        f"Your notification channel is operational and connected to Symphony XTS backend!\n"
        f"<b>Time:</b> {timestamp} IST\n"
        f"<b>Status:</b> Online & Ready 🟢"
    )

    ok = await send_telegram_alert(token, chat, test_msg)
    if ok:
        return {"status": "success", "message": "Test notification delivered successfully!"}
    else:
        return {"status": "error", "message": "Failed to send message via Telegram API. Check bot token and chat ID."}
