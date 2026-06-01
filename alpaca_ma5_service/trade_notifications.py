from __future__ import annotations

from .config import Settings
from .models import OrderResult, is_executed_order_status
from .openclaw_notify import safe_send_openclaw_messages


def notify_trade_order_event(settings: Settings, result: OrderResult, reason: str, *, broker_name: str) -> None:
    """格式化买卖订单结果，并通过 OpenClaw 安全发送。"""
    try:
        messages = render_trade_order_messages(result, reason, broker_name=broker_name)
        safe_send_openclaw_messages(settings, messages, context=f"{result.side} {result.symbol}")
    except Exception as exc:
        print(f"OpenClaw 通知失败，不影响主流程 {result.side} {result.symbol}：{type(exc).__name__}: {exc}")


def render_trade_order_messages(result: OrderResult, reason: str, *, broker_name: str) -> list[str]:
    """把 OrderResult 渲染成短消息；避免 OpenClaw 截断长换行文本。"""
    side_text = "买入" if result.side.upper() == "BUY" else "卖出"
    status = result.status.upper()
    if status == "REJECTED" or status.endswith("FAILED"):
        title = f"Alpaca交易{side_text}失败: {result.symbol}"
    elif is_executed_order_status(status):
        title = f"Alpaca交易{side_text}已成交: {result.symbol}"
    elif status in {"CANCELED", "CANCELLED"}:
        title = f"Alpaca交易{side_text}已取消: {result.symbol}"
    else:
        title = f"Alpaca交易{side_text}状态: {result.symbol}"

    summary_parts = [
        f"账户: {broker_name}",
        f"数量: {format_quantity(result.quantity)}",
        f"价格: {format_price(result.price)}",
        f"状态: {status}",
    ]
    if result.order_id:
        summary_parts.append(f"订单号: {compact_text(result.order_id, 36)}")

    messages = [title, " | ".join(summary_parts)]
    if reason:
        messages.append(f"原因: {compact_text(reason, 160)}")
    if result.message:
        messages.append(f"结果: {compact_text(result.message, 160)}")
    return messages


def format_price(value: float) -> str:
    """统一价格格式。"""
    return f"{float(value):.4f}"


def format_quantity(value: float) -> str:
    """统一股数格式，兼容碎股。"""
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def compact_text(value: str, max_chars: int) -> str:
    """压缩空白并限制长度。"""
    text = " ".join(str(value).split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "..."
