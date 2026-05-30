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
    """提交后最多等 timeout_seconds；没完全成交就向 Alpaca 请求取消。"""
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
    if status in FINAL_STATUSES:
        return OrderResult(order_id, symbol, side, qty, price, status, f"Alpaca {source_name} order ended with status={status}")

    # 超过 1 分钟还没完全成交，马上取消剩余订单，避免挂单继续留在市场里。
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
    """向 Alpaca 请求取消未成交订单；取消失败也返回结果，不抛异常。"""
    try:
        client.cancel_order_by_id(order_id)
        return OrderResult(order_id, symbol, side, quantity, price, success_status, success_message)
    except Exception as exc:
        failure_prefix = failure_prefix or f"Not filled within {timeout_seconds}s"
        return OrderResult(order_id, symbol, side, quantity, price, "CANCEL_FAILED", f"{failure_prefix}; cancel failed: {short_error(exc)}")


def normalize_order_status(raw_order) -> str:
    """把 alpaca-py enum 或普通字符串统一成 FILLED/ACCEPTED 这种状态。"""
    value = getattr(raw_order, "status", "") or ""
    value = getattr(value, "value", value)
    return str(value).split(".")[-1].upper()


def filled_quantity(raw_order) -> float:
    """读取已成交股数；没有部分成交信息时返回 0。"""
    try:
        return float(getattr(raw_order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
