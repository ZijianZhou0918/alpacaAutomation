from __future__ import annotations

import time

from .errors import short_error
from .models import OrderResult


FINAL_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "DONE_FOR_DAY", "EXPIRED", "REJECTED", "REPLACED"}


def wait_for_fill_or_cancel(
    client,
    raw_order,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    source_name: str,
    timeout_seconds: int = 60,
    poll_seconds: int = 5,
    sleep=time.sleep,
) -> OrderResult:
    """提交后等待成交；超过 timeout_seconds 未完全成交就请求撤单。"""
    order_id = str(getattr(raw_order, "id", "") or "")
    status = normalize_order_status(raw_order)
    qty = float(getattr(raw_order, "qty", quantity) or quantity)
    if not order_id:
        return OrderResult("", symbol, side, qty, price, status or "SUBMITTED", "Alpaca order submitted but order id is empty")

    deadline = time.monotonic() + max(timeout_seconds, 0)
    while status not in FINAL_STATUSES and time.monotonic() < deadline:
        sleep(max(0.1, min(poll_seconds, deadline - time.monotonic())))
        try:
            raw_order = client.get_order_by_id(order_id)
            status = normalize_order_status(raw_order)
            qty = float(getattr(raw_order, "qty", qty) or qty)
        except Exception as exc:
            reason = f"Status check failed: {short_error(exc)}"
            return cancel_unfilled_order(client, order_id, symbol, side, qty, price, timeout_seconds, f"{reason}; cancel requested.", reason)

    if status == "FILLED":
        return OrderResult(order_id, symbol, side, qty, price, "FILLED", f"Alpaca {source_name} order filled")
    filled_qty = filled_quantity(raw_order)
    if filled_qty > 0 and status in FINAL_STATUSES:
        return OrderResult(order_id, symbol, side, filled_qty, price, f"PARTIALLY_FILLED_{status}", f"Alpaca {source_name} order ended with partial fill: filled_qty={filled_qty} status={status}")
    if status in FINAL_STATUSES:
        return OrderResult(order_id, symbol, side, qty, price, status, f"Alpaca {source_name} order ended with status={status}")

    # 超过配置等待时间仍未完全成交，取消剩余挂单以减少市场暴露。
    filled_qty = filled_quantity(raw_order)
    detail = f" filled_qty={filled_qty}" if filled_qty > 0 else ""
    result_status = "PARTIALLY_FILLED_CANCEL_REQUESTED" if filled_qty > 0 else "CANCEL_REQUESTED"
    result_quantity = filled_qty if filled_qty > 0 else qty
    reason = f"Not filled within {timeout_seconds}s"
    return cancel_unfilled_order(
        client,
        order_id,
        symbol,
        side,
        result_quantity,
        price,
        timeout_seconds,
        f"{reason}; cancel requested.{detail}",
        reason,
        success_status=result_status,
    )


def cancel_unfilled_order(
    client,
    order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    timeout_seconds: int,
    success_message: str,
    failure_prefix: str | None = None,
    success_status: str = "CANCEL_REQUESTED",
) -> OrderResult:
    """请求取消未成交订单；取消失败返回 CANCEL_FAILED，不中断主流程。"""
    try:
        client.cancel_order_by_id(order_id)
    except Exception as exc:
        failure_prefix = failure_prefix or f"Not filled within {timeout_seconds}s"
        return OrderResult(order_id, symbol, side, quantity, price, "CANCEL_FAILED", f"{failure_prefix}; cancel failed: {short_error(exc)}")
    return cancel_confirmation_result(client, order_id, symbol, side, quantity, price, success_status, success_message)


def cancel_confirmation_result(client, order_id: str, symbol: str, side: str, quantity: float, price: float, fallback_status: str, fallback_message: str) -> OrderResult:
    """撤单后再查一次最终状态，区分已取消、已成交和未确认撤单。"""
    try:
        raw_order = client.get_order_by_id(order_id)
    except Exception as exc:
        return OrderResult(order_id, symbol, side, quantity, price, fallback_status, f"{fallback_message} status not confirmed: {short_error(exc)}")

    status = normalize_order_status(raw_order)
    filled_qty = filled_quantity(raw_order)
    qty = filled_qty if filled_qty > 0 else quantity
    if status == "FILLED":
        return OrderResult(order_id, symbol, side, qty, price, "FILLED", "Order filled while cancel was pending")
    if filled_qty > 0:
        result_status = "PARTIALLY_FILLED_CANCELED" if status in {"CANCELED", "CANCELLED"} else "PARTIALLY_FILLED_CANCEL_REQUESTED"
        detail = "" if "filled_qty=" in fallback_message else f" filled_qty={filled_qty}"
        return OrderResult(order_id, symbol, side, qty, price, result_status, f"{fallback_message}{detail} latest_status={status}")
    if status in {"CANCELED", "CANCELLED"}:
        return OrderResult(order_id, symbol, side, quantity, price, "CANCELED", f"{fallback_message} cancel confirmed")
    if status in FINAL_STATUSES:
        return OrderResult(order_id, symbol, side, quantity, price, status, f"{fallback_message} latest_status={status}")
    return OrderResult(order_id, symbol, side, quantity, price, fallback_status, f"{fallback_message} latest_status={status}")


def normalize_order_status(raw_order) -> str:
    """把 alpaca-py enum 或字符串统一成大写状态。"""
    value = getattr(raw_order, "status", "") or ""
    value = getattr(value, "value", value)
    return str(value).split(".")[-1].upper()


def filled_quantity(raw_order) -> float:
    """读取已成交股数；没有字段或格式异常时返回 0。"""
    try:
        return float(getattr(raw_order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
