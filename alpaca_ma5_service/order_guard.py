"""订单终态与撤单保护层。

Broker 提交订单后会进入本模块：先轮询是否成交，超时或查询失败时再请求撤单，
最后复查订单状态。项目中真正的 Alpaca 撤单 SDK 调用集中在
``cancel_unfilled_order``。
"""

from __future__ import annotations

import time

from .errors import short_error
from .models import OrderResult


FINAL_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


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
    """等待已提交订单进入终态，必要时自动撤单。

    功能顺序：读取订单号 -> 定时查询状态 -> 区分全成/部分成交/其他终态 ->
    超时后只取消剩余挂单。该函数不创建新订单，但可能触发撤单外部写操作。
    """
    # 【终态保护 1/4：保存券商订单标识和初始状态】
    # 后续所有查询与撤单都使用 submit_order 返回的同一 order_id；缺失订单号时
    # 无法安全定位挂单，因此只返回当前状态，绝不尝试按股票猜测撤单。
    order_id = str(getattr(raw_order, "id", "") or "")
    status = normalize_order_status(raw_order)
    qty = float(getattr(raw_order, "qty", quantity) or quantity)
    if not order_id:
        return OrderResult("", symbol, side, qty, price, status or "SUBMITTED", "Alpaca order submitted but order id is empty")

    # REPLACED 的旧订单不再成交，但 replaced_by 指向的新订单仍可能开放。先沿
    # 替换链取得当前订单，避免 timeout=0 时直接对旧订单号发送无效撤单。
    seen_order_ids = {order_id}
    while status == "REPLACED":
        replacement_id = str(getattr(raw_order, "replaced_by", "") or "")
        if not replacement_id or replacement_id in seen_order_ids:
            return OrderResult(
                order_id,
                symbol,
                side,
                qty,
                price,
                "REPLACED",
                "Order was replaced but the active replacement order could not be identified",
            )
        seen_order_ids.add(replacement_id)
        try:
            raw_order = client.get_order_by_id(replacement_id)
        except Exception as exc:
            return OrderResult(
                order_id,
                symbol,
                side,
                qty,
                price,
                "REPLACED",
                f"Replacement order {replacement_id} could not be confirmed: {short_error(exc)}",
            )
        order_id = replacement_id
        status = normalize_order_status(raw_order)
        qty = float(getattr(raw_order, "qty", qty) or qty)

    # 【终态保护 2/4：在单调时钟截止点前轮询】
    # 使用 monotonic 避免系统时间调整影响超时；每次只读 get_order_by_id。
    # 状态查询异常时也不能放任未知挂单，下面会进入同一个安全撤单函数。
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while status not in FINAL_STATUSES and time.monotonic() < deadline:
        sleep(max(0.1, min(poll_seconds, deadline - time.monotonic())))
        try:
            raw_order = client.get_order_by_id(order_id)
            status = normalize_order_status(raw_order)
            qty = float(getattr(raw_order, "qty", qty) or qty)
            # REPLACED 只代表旧订单结束，替换后的新订单仍可能继续成交。沿
            # replaced_by 切换到新订单，后续查询和撤单都只针对当前有效订单。
            if status == "REPLACED":
                replacement_id = str(getattr(raw_order, "replaced_by", "") or "")
                if replacement_id and replacement_id not in seen_order_ids:
                    seen_order_ids.add(replacement_id)
                    order_id = replacement_id
                    raw_order = client.get_order_by_id(replacement_id)
                    status = normalize_order_status(raw_order)
                    qty = float(getattr(raw_order, "qty", qty) or qty)
                else:
                    return OrderResult(
                        order_id,
                        symbol,
                        side,
                        qty,
                        price,
                        "REPLACED",
                        "Order was replaced but the active replacement order could not be identified",
                    )
        except Exception as exc:
            reason = f"Status check failed: {short_error(exc)}"
            return cancel_unfilled_order(client, order_id, symbol, side, qty, price, timeout_seconds, f"{reason}; cancel requested.", reason)

    # 【终态保护 3/4：先识别成交和已结束状态】
    # 完全成交直接返回；部分成交必须保留真实 filled_qty；其他最终状态原样返回，
    # 这些分支都不会再向券商发送撤单请求。
    if status == "FILLED":
        return OrderResult(order_id, symbol, side, qty, price, "FILLED", f"Alpaca {source_name} order filled")
    filled_qty = filled_quantity(raw_order)
    if filled_qty > 0 and status in FINAL_STATUSES:
        return OrderResult(order_id, symbol, side, filled_qty, price, f"PARTIALLY_FILLED_{status}", f"Alpaca {source_name} order ended with partial fill: filled_qty={filled_qty} status={status}")
    if status in FINAL_STATUSES:
        return OrderResult(order_id, symbol, side, qty, price, status, f"Alpaca {source_name} order ended with status={status}")

    # 【终态保护 4/4：超时后只撤销未成交剩余挂单】
    # 已成交数量会进入返回结果，不能因撤销剩余量而把部分成交记成零成交。
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
    """向 Alpaca 请求取消订单，并立即复查确认结果。

    这是自动超时撤单和手动撤单共用的唯一底层写入点。取消失败返回
    ``CANCEL_FAILED``，不会吞掉错误，也不会中断主监控循环。
    """
    try:
        # 【真实券商写入：撤单】
        # 这是自动超时撤单与 OpenClaw 手动撤单共用的唯一 SDK 写入点。
        # 参数必须是前面已确认的唯一 order_id；调用成功只代表撤单请求被接收，
        # 订单仍可能在竞态中成交，因此不能在这里直接返回 CANCELED。
        client.cancel_order_by_id(order_id)
    except Exception as exc:
        failure_prefix = failure_prefix or f"Not filled within {timeout_seconds}s"
        # 部分成交后撤单失败时，不能退化成普通 CANCEL_FAILED：那会丢失“已有成交”
        # 这一事实，导致每日名额、持仓暴露和通知都把真实成交量误判为零。
        partial_fill = success_status.startswith("PARTIALLY_FILLED")
        failure_status = "PARTIALLY_FILLED_CANCEL_FAILED" if partial_fill else "CANCEL_FAILED"
        filled_detail = f"; filled_qty={quantity}" if partial_fill else ""
        return OrderResult(
            order_id,
            symbol,
            side,
            quantity,
            price,
            failure_status,
            f"{failure_prefix}; cancel failed: {short_error(exc)}{filled_detail}",
        )
    # 【撤单后确认】
    # 立即只读复查一次，区分已取消、撤单期间成交、部分成交、其他终态以及仍未确认；
    # 最终状态由 cancel_confirmation_result 决定，不用“请求成功”冒充“撤单完成”。
    return cancel_confirmation_result(client, order_id, symbol, side, quantity, price, success_status, success_message)


def cancel_confirmation_result(client, order_id: str, symbol: str, side: str, quantity: float, price: float, fallback_status: str, fallback_message: str) -> OrderResult:
    """撤单请求后只读复查一次，区分已取消、竞态成交、部分成交和未确认。"""
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
