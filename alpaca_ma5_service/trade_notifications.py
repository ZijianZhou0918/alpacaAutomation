from __future__ import annotations

from datetime import datetime
from threading import Thread

from .config import Settings
from .market_time import now_market_time
from .models import OrderResult, has_unconfirmed_order_status
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
    """把最终订单结果压成一条固定字段消息，供用户和 Agent 快速判断风险。"""
    return [
        "\n".join(
            render_order_message_lines(
                result,
                reason,
                broker_name=broker_name,
                submitted=False,
            )
        )
    ]


def render_order_submitted_message(result: OrderResult, reason: str, *, broker_name: str) -> str:
    """渲染订单刚提交成功时的即时通知，明确它还不代表成交。"""
    return "\n".join(
        render_order_message_lines(
            result,
            reason,
            broker_name=broker_name,
            submitted=True,
        )
    )


def render_order_message_lines(
    result: OrderResult,
    reason: str,
    *,
    broker_name: str,
    submitted: bool,
) -> list[str]:
    """渲染 Agent 友好的固定字段订单消息，不改变任何订单状态语义。"""
    side = (result.side or "CANCEL").upper()
    status = (result.status or "UNKNOWN").upper()
    status_title, status_line = order_status_display(status, submitted=submitted)
    lines = [
        f"【{order_kind_text(side)}｜{status_title}】",
        f"股票：{result.symbol or '订单'}",
        f"账户：{format_broker_name(broker_name)}",
        f"状态：{status_line}（{status}）",
        "",
        "订单信息",
        f"- 方向：{order_side_text(side)}（{side}）",
    ]
    if side != "CANCEL":
        lines.extend(
            [
                f"- 数量：{format_quantity(result.quantity)} 股",
                f"- 价格：{format_price(result.price)}",
                f"- 估算金额：{format_order_notional(result.quantity, result.price)}",
            ]
        )
    if result.order_id:
        lines.append(f"- 订单号：{full_text(result.order_id)}")
    if reason:
        lines.extend(["", "策略原因", f"- {full_text(reason)}"])
    if not submitted and result.message:
        result_heading = "失败原因" if status in {"REJECTED", "CANCEL_FAILED"} else "执行结果"
        lines.extend(["", result_heading, f"- {full_text(result.message)}"])
    lines.extend(["", "下一步", f"- {order_follow_up_text(status, submitted=submitted)}"])
    return lines


def order_kind_text(side: str) -> str:
    """把 BUY/SELL/CANCEL 转成一眼可辨的订单类别。"""
    if side == "BUY":
        return "买单"
    if side == "SELL":
        return "卖单"
    return "撤单"


def order_status_display(status: str, *, submitted: bool) -> tuple[str, str]:
    """返回标题状态和带视觉标记的中文状态，原始状态另行保留。"""
    if submitted:
        return "等待成交", "🟡 已提交，等待成交"
    if status == "FILLED":
        return "已成交", "✅ 已成交"
    if status == "DRY_RUN":
        return "模拟完成", "🧪 模拟完成，未提交真实订单"
    if status.startswith("PARTIALLY_FILLED"):
        if has_unconfirmed_order_status(status):
            return "部分成交｜余单待确认", "⚠️ 部分成交，未成交余量仍待确认"
        return "部分成交｜余单已结束", "⚠️ 部分成交，未成交余量已结束"
    if status == "REJECTED":
        return "下单失败", "❌ 已拒绝，未建立新订单"
    if status == "SUBMIT_UNCONFIRMED":
        return "提交状态未知", "⚠️ 券商是否接单尚未确认"
    if status in {"CANCELED", "CANCELLED"}:
        return "已取消", "⏹ 已取消"
    if status == "CANCEL_FAILED":
        return "撤单失败", "❌ 撤单失败，订单状态需人工确认"
    if status in {"CANCEL_REQUESTED", "PENDING_CANCEL"}:
        return "撤单处理中", "🟡 已请求撤单，等待最终确认"
    if status == "NO_OPEN_ORDERS":
        return "没有挂单", "ℹ️ 没有找到可撤挂单"
    if has_unconfirmed_order_status(status):
        return "等待成交", "🟡 订单仍在处理中"
    return "状态更新", f"ℹ️ 状态已更新为 {status}"


def order_follow_up_text(status: str, *, submitted: bool) -> str:
    """按真实状态说明下一步，避免把提交、部分成交或模拟误报成已成交。"""
    if submitted:
        return "订单已提交至 Alpaca，但尚未证明成交；最终成交、撤单或拒单状态会另行通知。"
    if status == "FILLED":
        return "成交结果已记录；后续仓位管理以当前策略和券商持仓为准。"
    if status == "DRY_RUN":
        return "这是本地模拟结果，没有向 Paper 或 Live 账户提交订单。"
    if status.startswith("PARTIALLY_FILLED"):
        if has_unconfirmed_order_status(status):
            return "实际成交部分已记录；系统将继续按订单号确认未成交余量。"
        return "实际成交部分已记录，未成交余量已经结束；请按剩余持仓继续管理。"
    if status == "REJECTED":
        return "本次订单没有建立；请检查执行结果和策略原因。"
    if status == "SUBMIT_UNCONFIRMED":
        return "不要重试下单；先按唯一订单号向券商恢复查询，确认是否存在真实暴露。"
    if status in {"CANCELED", "CANCELLED", "NO_OPEN_ORDERS"}:
        return "当前没有继续等待的该笔挂单；如曾有部分成交，以券商持仓为准。"
    if status in {"CANCEL_FAILED", "CANCEL_REQUESTED", "PENDING_CANCEL"}:
        return "不要重复撤单或反向下单；继续按订单号确认最终状态。"
    if has_unconfirmed_order_status(status):
        return "订单仍未终态；继续按订单号确认，不要把当前状态当作成交。"
    return "请以券商订单状态和本地订单记录为准。"


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
    price = float(value)
    return "未提供" if price <= 0 else f"${price:,.4f}"


def format_quantity(value: float) -> str:
    """统一股数显示格式，兼容碎股。"""
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def format_order_notional(quantity: float, price: float) -> str:
    """用消息中的数量和价格给出清晰但不冒充实际成交额的估算值。"""
    quantity_value = float(quantity)
    price_value = float(price)
    if quantity_value <= 0 or price_value <= 0:
        return "不可计算"
    return f"约 ${quantity_value * price_value:,.2f}"


def format_broker_name(value: str) -> str:
    """突出真实账户、Paper 和 DryRun，避免 Agent 忽略账户风险。"""
    text = full_text(value) or "未知账户"
    lowered = text.lower()
    if "live" in lowered:
        return "Alpaca LIVE（真实账户）"
    if "paper" in lowered:
        return "Alpaca PAPER（模拟账户）"
    if "dry" in lowered:
        return "DryRun（本地模拟，不下单）"
    return text


def full_text(value: str) -> str:
    """保留完整内容，只压缩换行和重复空白。"""
    return " ".join(str(value).split())
