from __future__ import annotations

from datetime import datetime
from threading import Thread

from .config import Settings
from .market_time import now_market_time
from .models import OrderResult, is_executed_order_status, is_order_error_status
from .openclaw_notify import safe_send_openclaw_messages
from .state import append_order


def record_order_and_notify(
    settings: Settings,
    result: OrderResult,
    reason: str,
    *,
    broker_name: str,
    order_time: datetime | None = None,
) -> None:
    """记录最终订单结果并通知 OpenClaw；拒单和撤单也会记录。"""
    order_time = order_time or now_market_time(settings)
    append_order(settings.output_dir, result, reason, day=order_time.date(), created_at=order_time)
    safe_notify_trade_order_event(settings, result, reason, broker_name=broker_name)


def notify_order_submitted(settings: Settings, result: OrderResult, reason: str, *, broker_name: str) -> Thread | None:
    """Alpaca 接受订单请求后立即通知，不等待最终成交或撤单。"""
    if not settings.trade_notify_openclaw_enabled:
        return None
    try:
        message = render_order_submitted_message(result, reason, broker_name=broker_name)
        thread = Thread(
            target=safe_send_openclaw_messages,
            args=(settings, [message]),
            kwargs={"context": f"submitted {result.side} {result.symbol}"},
            daemon=False,
        )
        thread.start()
        print(f"OpenClaw 下单通知已启动: {result.side} {result.symbol} status={result.status}", flush=True)
        return thread
    except Exception as exc:
        print(f"OpenClaw 通知失败，不影响主流程 {result.side} {result.symbol}：{type(exc).__name__}: {exc}")
        return None


def safe_notify_trade_order_event(settings: Settings, result: OrderResult, reason: str, *, broker_name: str) -> None:
    """安全发送最终订单通知；OpenClaw 失败不能影响交易主流程。"""
    try:
        notify_trade_order_event(settings, result, reason, broker_name=broker_name)
    except Exception as exc:
        print(f"OpenClaw 通知失败，不影响主流程 {result.side} {result.symbol}：{type(exc).__name__}: {exc}")


def notify_trade_order_event(settings: Settings, result: OrderResult, reason: str, *, broker_name: str) -> None:
    """格式化订单结果并交给 OpenClaw 发送。"""
    messages = render_trade_order_messages(result, reason, broker_name=broker_name)
    safe_send_openclaw_messages(settings, messages, context=f"{result.side} {result.symbol}")


def render_trade_order_messages(result: OrderResult, reason: str, *, broker_name: str) -> list[str]:
    """把最终订单结果压成一条结构化消息，避免原因和状态分散发送。"""
    side_text = order_side_text(result.side)
    status = result.status.upper()
    if result.side.upper() == "CANCEL":
        title = f"Alpaca交易撤单状态: {result.symbol or '订单'}"
    elif is_order_error_status(status):
        title = f"Alpaca交易{side_text}失败: {result.symbol}"
    elif is_executed_order_status(status):
        title = f"Alpaca交易{side_text}已成交: {result.symbol}"
    elif status in {"CANCELED", "CANCELLED"}:
        title = f"Alpaca交易{side_text}已取消: {result.symbol}"
    else:
        title = f"Alpaca交易{side_text}状态: {result.symbol}"

    symbol = result.symbol or "订单"
    lines = [
        f"【Alpaca 交易结果】{symbol}",
        f"结论：{title}",
        "动作：已记录订单结果；如为买入/卖出成交，系统会按当前策略继续后续监控。",
        "",
        "订单信息",
        f"- 账户：{broker_name}",
        f"- 方向：{side_text}",
        f"- 数量：{format_quantity(result.quantity)}",
        f"- 价格：{format_price(result.price)}",
        f"- 状态：{status}",
    ]
    if result.order_id:
        lines.append(f"- 订单号：{compact_text(result.order_id, 36)}")

    if reason:
        lines.extend(["", "策略原因", f"- {full_text(reason)}"])
    if result.message:
        lines.extend(["", "执行结果", f"- {full_text(result.message)}"])
    return ["\n".join(lines)]


def render_order_submitted_message(result: OrderResult, reason: str, *, broker_name: str) -> str:
    """渲染订单刚提交成功时的即时通知。"""
    side_text = order_side_text(result.side)
    title = f"Alpaca交易{side_text}已提交: {result.symbol}"
    lines = [
        f"【Alpaca 交易提交】{result.symbol}",
        f"结论：{title}",
        "动作：订单已提交到 Alpaca，等待成交/撤单结果；最终状态会另行通知。",
        "",
        "订单信息",
        f"- 账户：{broker_name}",
        f"- 方向：{side_text}",
        f"- 数量：{format_quantity(result.quantity)}",
        f"- 价格：{format_price(result.price)}",
        f"- 状态：{result.status.upper()}",
    ]
    if result.order_id:
        lines.append(f"- 订单号：{compact_text(result.order_id, 36)}")
    if reason:
        lines.extend(["", "策略原因", f"- {full_text(reason)}"])
    return "\n".join(lines)


def order_side_text(side: str) -> str:
    """把订单方向翻译成通知里的中文动作。"""
    side = side.upper()
    if side == "BUY":
        return "买入"
    if side == "SELL":
        return "卖出"
    return "撤单"


def format_price(value: float) -> str:
    """统一价格显示格式。"""
    return f"{float(value):.4f}"


def format_quantity(value: float) -> str:
    """统一股数显示格式，兼容碎股。"""
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def compact_text(value: str, max_chars: int) -> str:
    """压缩长文本，避免通知刷屏。"""
    text = " ".join(str(value).split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "..."


def full_text(value: str) -> str:
    """保留完整内容，只压缩换行和重复空白。"""
    return " ".join(str(value).split())
