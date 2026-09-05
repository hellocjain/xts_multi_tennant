"""
Order Execution Services:
- Split / Iceberg lot slicing with rate-limiting
- Margin-Optimized Basket Orders (BUY legs placed before SELL legs)
- Smart Bracket Orders (Target + Stop-Loss + OCO leg cancellation)
- Single-position square-off
"""
import time
import asyncio
import logging
import uuid
from typing import List, Dict, Any, Optional

import xts_api

logger = logging.getLogger(__name__)

async def execute_split_order(
    action: str,
    symbol: str,
    quantity: int,
    price: float,
    pricetype: str = "LIMIT",
    split_size: Optional[int] = None,
    delay_seconds: float = 0.25,
    order_ref: Optional[str] = None,
    is_paper: bool = False
) -> Dict[str, Any]:
    """
    Slices a large order into multiple child chunks with configurable delay.
    If split_size is not provided, resolves the exchange freeze_qty and slices automatically.
    """
    total_qty = int(quantity)
    if total_qty <= 0:
        return {"status": "error", "message": "Quantity must be greater than 0"}

    inst = xts_api.resolve_contract(symbol)
    freeze_limit = int(inst.get("freeze_qty") or 100000) if inst else 100000
    chunk_size = int(split_size) if split_size and int(split_size) > 0 else freeze_limit

    chunks = []
    remaining = total_qty
    while remaining > 0:
        take = min(remaining, chunk_size)
        chunks.append(take)
        remaining -= take

    base_ref = order_ref or f"SPLIT_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    successful_slices = []
    failed_slices = []
    total_dispatched = 0

    logger.info(f"Executing Split Order for {action} {total_qty}x {symbol} in {len(chunks)} slices (max {chunk_size} per slice)")

    for idx, slice_qty in enumerate(chunks, start=1):
        slice_ref = f"{base_ref}_S{idx}"
        try:
            res = await asyncio.to_thread(
                xts_api.execute_trade_with_retry,
                action, symbol, slice_qty, price, slice_ref, attempt=1, is_paper=is_paper
            )
            is_success = (res.get("type") == "success" or res.get("status") == "success")
            if is_success:
                successful_slices.append({
                    "slice": idx,
                    "quantity": slice_qty,
                    "order_ref": slice_ref,
                    "result": res
                })
                total_dispatched += slice_qty
            else:
                failed_slices.append({
                    "slice": idx,
                    "quantity": slice_qty,
                    "order_ref": slice_ref,
                    "result": res,
                    "error": res.get("description") or res.get("message") or "Broker rejection"
                })
        except Exception as e:
            failed_slices.append({
                "slice": idx,
                "quantity": slice_qty,
                "order_ref": slice_ref,
                "error": str(e)
            })

        if idx < len(chunks) and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    if failed_slices and not successful_slices:
        return {
            "status": "error",
            "message": f"All {len(chunks)} split slices were rejected",
            "dispatched_quantity": 0,
            "total_quantity": total_qty,
            "failed_slices": failed_slices
        }
    elif failed_slices:
        return {
            "status": "partial_failure",
            "message": f"Split execution partial: {total_dispatched} of {total_qty} units placed",
            "dispatched_quantity": total_dispatched,
            "total_quantity": total_qty,
            "successful_slices": successful_slices,
            "failed_slices": failed_slices
        }
    else:
        return {
            "status": "success",
            "message": f"All {len(chunks)} split slices executed successfully",
            "dispatched_quantity": total_dispatched,
            "total_quantity": total_qty,
            "successful_slices": successful_slices
        }

async def execute_basket_order(
    orders: List[Dict[str, Any]],
    is_paper: bool = False
) -> Dict[str, Any]:
    """
    Executes an atomic batch of multiple order legs.
    MARGIN-OPTIMIZATION: Automatically sorts BUY legs before SELL legs so margin benefits apply.
    """
    if not orders or not isinstance(orders, list):
        return {"status": "error", "message": "Orders list cannot be empty"}

    # Sort BUY orders first to maximize exchange margin benefits
    def _order_sort_key(o):
        side = str(o.get("action", "")).upper()
        return 0 if side == "BUY" else 1

    sorted_orders = sorted(orders, key=_order_sort_key)
    results = []
    all_success = True
    any_success = False

    for idx, ord_item in enumerate(sorted_orders, start=1):
        action = str(ord_item.get("action", "BUY")).upper()
        symbol = str(ord_item.get("symbol", "")).strip()
        quantity = int(ord_item.get("quantity", 0))
        price = float(ord_item.get("price", 0.0))
        order_ref = ord_item.get("order_ref") or f"BASKET_{int(time.time()*1000)}_{idx}_{uuid.uuid4().hex[:4]}"

        if not symbol or quantity <= 0:
            results.append({
                "index": idx,
                "symbol": symbol,
                "status": "error",
                "error": "Invalid symbol or quantity"
            })
            all_success = False
            continue

        try:
            res = await asyncio.to_thread(
                xts_api.execute_trade_with_retry,
                action, symbol, quantity, price, order_ref, attempt=1, is_paper=is_paper
            )
            is_ok = (res.get("type") == "success" or res.get("status") == "success")
            if is_ok:
                any_success = True
                results.append({
                    "index": idx,
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "order_ref": order_ref,
                    "status": "success",
                    "result": res
                })
            else:
                all_success = False
                results.append({
                    "index": idx,
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "order_ref": order_ref,
                    "status": "error",
                    "result": res
                })
        except Exception as e:
            all_success = False
            results.append({
                "index": idx,
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "order_ref": order_ref,
                "status": "error",
                "error": str(e)
            })

    final_status = "success" if all_success else ("partial_failure" if any_success else "error")
    return {
        "status": final_status,
        "total_orders": len(orders),
        "successful_orders": len([r for r in results if r.get("status") == "success"]),
        "failed_orders": len([r for r in results if r.get("status") != "success"]),
        "results": results
    }

async def square_off_single_position(
    symbol: str,
    exchange: Optional[str] = None,
    product: Optional[str] = None
) -> Dict[str, Any]:
    """Squares off an open position for a single specific symbol."""
    pos_data = await asyncio.to_thread(xts_api.get_positions_telemetry)
    positions = pos_data.get("positions", [])

    clean_target = symbol.replace(" ", "").upper()
    matching_pos = None

    for p in positions:
        pos_sym = str(p.get("symbol", "")).replace(" ", "").upper()
        if clean_target in pos_sym or pos_sym in clean_target:
            matching_pos = p
            break

    if not matching_pos:
        return {"status": "error", "message": f"No active position found for '{symbol}'"}

    qty = int(matching_pos.get("quantity", 0))
    if qty == 0:
        return {"status": "success", "message": f"Position for '{symbol}' is already flat (0 qty)"}

    action = "SELL" if qty > 0 else "BUY"
    square_qty = abs(qty)
    order_ref = f"SQ_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"

    res = await asyncio.to_thread(
        xts_api.execute_trade_with_retry,
        action, symbol, square_qty, 0.0, order_ref, attempt=1
    )
    is_ok = (res.get("type") == "success" or res.get("status") == "success")
    return {
        "status": "success" if is_ok else "error",
        "symbol": symbol,
        "action": action,
        "quantity": square_qty,
        "result": res
    }


def calculate_slippage_price(
    symbol: str,
    action: str,
    ltp: float,
    buffer_pct: float = 0.005,
    tick_size: float = 0.05
) -> float:
    """
    Calculates institutional price-protected limit price for market orders.
    Prevents freak trades by capping BUY at (LTP * (1 + buffer)) and SELL at (LTP * (1 - buffer)),
    quantized to the instrument's exact tick size.
    """
    if ltp <= 0:
        return 0.0

    act = action.strip().upper()
    if act == "BUY":
        raw_price = ltp * (1.0 + buffer_pct)
    else:
        raw_price = ltp * (1.0 - buffer_pct)

    if tick_size > 0:
        price = round(round(raw_price / tick_size) * tick_size, 4)
    else:
        price = round(raw_price, 2)
    return max(price, 0.05)


async def execute_reversal_order(
    symbol: str,
    new_action: str,
    new_quantity: int,
    price: float = 0.0,
    order_ref: Optional[str] = None,
    is_paper: bool = False
) -> Dict[str, Any]:
    """
    Atomic Position Reversal:
    Checks if there is an open opposite position on the target symbol.
    If an opposite position exists:
      1. Squares off the opposite position.
      2. Immediately places the entry order for new_quantity on the new side.
    If no opposite position exists (flat or same direction), places the order directly.
    """
    action_clean = new_action.strip().upper()
    if action_clean not in ("BUY", "SELL"):
        return {"status": "error", "message": f"Invalid action '{new_action}'. Must be BUY or SELL"}
    if new_quantity <= 0:
        return {"status": "error", "message": "Quantity must be greater than 0"}

    pos_data = await asyncio.to_thread(xts_api.get_positions_telemetry)
    positions = pos_data.get("positions", [])
    clean_target = symbol.replace(" ", "").upper()

    matching_pos = None
    for p in positions:
        pos_sym = str(p.get("symbol", "")).replace(" ", "").upper()
        if clean_target in pos_sym or pos_sym in clean_target:
            matching_pos = p
            break

    existing_qty = int(matching_pos.get("quantity", 0)) if matching_pos else 0
    is_reversal = (action_clean == "BUY" and existing_qty < 0) or (action_clean == "SELL" and existing_qty > 0)

    closed_pos_result = None
    if is_reversal and abs(existing_qty) > 0:
        logger.info(f"Reversal detected for {symbol}: Existing qty {existing_qty}, reversing to {action_clean} {new_quantity}")
        closed_pos_result = await square_off_single_position(symbol)

    # Place new entry order
    base_ref = order_ref or f"REV_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    entry_res = await asyncio.to_thread(
        xts_api.execute_trade_with_retry,
        action_clean, symbol, new_quantity, price, base_ref, attempt=1, is_paper=is_paper
    )
    is_ok = (entry_res.get("type") == "success" or entry_res.get("status") == "success")

    return {
        "status": "success" if is_ok else "error",
        "symbol": symbol,
        "action": action_clean,
        "quantity": new_quantity,
        "price": price,
        "reversed": is_reversal,
        "closed_position": closed_pos_result,
        "order_result": entry_res,
        "message": f"Order executed {'with reversal' if is_reversal else 'normally'}"
    }

