from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .broker import AlpacaStockBroker
from .config import Settings, build_settings
from .market_data import build_market_data
from .models import OrderResult
from .watchlist import normalize_symbol


SUPPORTED_TRADE_COMMAND_FORMATS = [
    "帮我买5刀的NTAP，购买价格固定160",
    "帮我买5刀的NTAP，购买价格为当前价*0.95",
    "帮我买5刀的NTAP，市价买入",
    "帮我卖出10股NTAP，限价160",
    "帮我卖出10股NTAP，价格为当前价*1.05",
    "帮我卖出NTAP，市价卖出",
    "撤单NTAP",
    "撤单 订单号: FU1C9F9AADE3E56000",
    "全部撤单",
]


@dataclass(frozen=True)
class TradeCommand:
    """OpenClaw 传入的一句话交易指令，解析后统一落到这个结构。"""

    action: str
    raw_message: str
    symbol: str = ""
    notional_usd: float = 0.0
    quantity: float = 0.0
    limit_price: float = 0.0
    limit_price_multiplier: float = 0.0
    order_id: str = ""
    cancel_all: bool = False


@dataclass(frozen=True)
class TradeCommandResponse:
    """交易指令执行结果；HTTP 接口和点击脚本都用同一个返回格式。"""

    ok: bool
    command: TradeCommand
    broker_name: str
    results: list[OrderResult]
    message: str


def parse_trade_command(message: str) -> TradeCommand:
    """把中文/英文短句解析成买入、卖出或撤单指令。"""
    raw_message = " ".join(str(message or "").strip().split())
    if not raw_message:
        raise ValueError(_unsupported_format_message("消息为空，无法执行交易指令"))

    text = raw_message.upper()
    try:
        action = _parse_action(text)
    except ValueError as exc:
        raise ValueError(_unsupported_format_message(str(exc))) from exc
    symbol = _parse_symbol(text)
    notional_usd = _parse_notional_usd(text)
    quantity = _parse_quantity(text)
    limit_price = _parse_limit_price(text)
    limit_price_multiplier = _parse_limit_price_multiplier(text)
    market_order = _has_market_order_intent(text)
    order_id = _parse_order_id(text) if action == "CANCEL" else ""
    cancel_all = action == "CANCEL" and any(word in raw_message for word in ("全部", "所有")) and not symbol and not order_id

    if action in {"BUY", "SELL"} and not symbol:
        raise ValueError(_unsupported_format_message("买入/卖出指令必须包含股票代码，例如 AAPL 或 US.AAPL"))
    if action == "BUY" and notional_usd <= 0 and quantity <= 0:
        raise ValueError(_unsupported_format_message("买入指令必须包含金额或股数，例如 3000刀 或 10股"))
    if action in {"BUY", "SELL"} and limit_price <= 0 and limit_price_multiplier <= 0 and not market_order:
        raise ValueError(_unsupported_format_message("买入/卖出必须明确写固定限价、当前价倍率，或市价"))
    if action == "CANCEL" and not order_id and not symbol and not cancel_all:
        raise ValueError(_unsupported_format_message("撤单指令必须包含订单号、股票代码，或明确写“全部撤单”"))

    return TradeCommand(
        action=action,
        raw_message=raw_message,
        symbol=symbol,
        notional_usd=notional_usd,
        quantity=quantity,
        limit_price=limit_price,
        limit_price_multiplier=limit_price_multiplier,
        order_id=order_id,
        cancel_all=cancel_all,
    )


def execute_trade_command(
    message_or_command: str | TradeCommand,
    settings: Settings | None = None,
    broker=None,
    market_data=None,
) -> TradeCommandResponse:
    """执行已解析或原始文本交易指令；真实下单仍走 broker 的记录和通知链路。"""
    settings = settings or build_settings()
    command = parse_trade_command(message_or_command) if isinstance(message_or_command, str) else message_or_command
    broker = broker or AlpacaStockBroker(settings)
    broker_name = broker.source_name()
    reason = f"OpenClaw手动指令: {command.raw_message}"

    if command.action == "BUY":
        results = [_execute_buy(command, broker, settings, market_data, reason)]
    elif command.action == "SELL":
        results = _execute_sell(command, broker, settings, market_data, reason)
    elif command.action == "CANCEL":
        results = _execute_cancel(command, broker, reason)
    else:
        raise ValueError(f"未知交易动作：{command.action}")

    ok = all(result.status.upper() not in {"REJECTED", "CANCEL_FAILED"} for result in results)
    return TradeCommandResponse(ok=ok, command=command, broker_name=broker_name, results=results, message=_response_message(results))


def _execute_buy(command: TradeCommand, broker, settings: Settings, market_data, reason: str) -> OrderResult:
    """执行买入；有限价就用固定 BUY LIMIT，没有限价才读取实时价走原买入链路。"""
    if command.limit_price > 0:
        notional = command.notional_usd or command.quantity * command.limit_price
        return broker.place_limit_buy(command.symbol, notional, command.limit_price, reason, skip_time_validation=True)

    created_market_data = market_data is None
    market_data = market_data or build_market_data(settings)
    try:
        snapshot = market_data.get_snapshot(command.symbol)
        if command.limit_price_multiplier > 0:
            limit_price = _dynamic_limit_price(snapshot.current_price, command.limit_price_multiplier)
            notional = command.notional_usd or command.quantity * limit_price
            reason = (
                f"{reason} | 动态限价: 当前价 {snapshot.current_price:.4f} "
                f"* {command.limit_price_multiplier:.4f} = {limit_price:.2f}"
            )
            return broker.place_limit_buy(command.symbol, notional, limit_price, reason, skip_time_validation=True)
        notional = command.notional_usd or command.quantity * snapshot.current_price
        return broker.place_market_buy(command.symbol, notional, snapshot.current_price, reason, skip_time_validation=True)
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()


def _execute_sell(command: TradeCommand, broker, settings: Settings, market_data, reason: str) -> list[OrderResult]:
    """执行卖出；不写股数时默认卖出该股票当前全部持仓。"""
    quantity = command.quantity
    if quantity <= 0:
        position = broker.get_positions().get(command.symbol)
        quantity = position.quantity if position else 0.0

    if command.limit_price > 0:
        return [broker.place_limit_sell(command.symbol, quantity, command.limit_price, reason, skip_time_validation=True)]

    created_market_data = market_data is None
    market_data = market_data or build_market_data(settings)
    try:
        snapshot = market_data.get_snapshot(command.symbol)
        if command.limit_price_multiplier > 0:
            limit_price = _dynamic_limit_price(snapshot.current_price, command.limit_price_multiplier)
            reason = (
                f"{reason} | 动态限价: 当前价 {snapshot.current_price:.4f} "
                f"* {command.limit_price_multiplier:.4f} = {limit_price:.2f}"
            )
            return [broker.place_limit_sell(command.symbol, quantity, limit_price, reason, skip_time_validation=True)]
        return [broker.place_market_sell(command.symbol, quantity, snapshot.current_price, reason, skip_time_validation=True)]
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()


def _execute_cancel(command: TradeCommand, broker, reason: str) -> list[OrderResult]:
    """执行撤单；优先按订单号，其次按股票代码，最后才允许全部撤单。"""
    if command.order_id:
        return [broker.cancel_order(command.order_id, reason)]
    if command.symbol:
        return broker.cancel_open_orders(command.symbol, reason)
    return broker.cancel_open_orders("", reason)


def trade_command_response_to_dict(response: TradeCommandResponse) -> dict:
    """把执行结果转成 HTTP JSON 可直接返回的字典。"""
    return {
        "ok": response.ok,
        "message": response.message,
        "broker": response.broker_name,
        "command": asdict(response.command),
        "results": [asdict(result) for result in response.results],
    }


def render_trade_command_response(response: TradeCommandResponse) -> str:
    """生成控制台/聊天里好读的中文摘要。"""
    lines = [
        "OpenClaw 交易指令结果",
        f"通道：{response.broker_name}",
        f"指令：{response.command.raw_message}",
        f"解析：{_describe_command(response.command)}",
    ]
    for result in response.results:
        lines.append(
            "结果："
            f"{result.symbol or '订单'} | {result.side} | 状态 {result.status} | "
            f"数量 {result.quantity:.6f} | 价格 {_format_price(result.price)} | {result.message}"
        )
        if result.order_id:
            lines.append(f"订单号：{result.order_id}")
        if result.message:
            label = "失败原因" if result.status.upper() in {"REJECTED", "CANCEL_FAILED"} else "执行原因"
            lines.append(f"{label}：{result.message}")
    lines.append(f"总结：{response.message}")
    return "\n".join(lines)


def _parse_action(text: str) -> str:
    if any(word in text for word in ("撤单", "取消订单", "取消挂单", "CANCEL")):
        return "CANCEL"
    if any(word in text for word in ("卖", "SELL")):
        return "SELL"
    if any(word in text for word in ("买", "购买", "BUY")):
        return "BUY"
    raise ValueError("没有识别到买入、卖出或撤单动作")


def _parse_symbol(text: str) -> str:
    excluded = {"BUY", "SELL", "CANCEL", "USD", "US", "DAY", "GTC", "ID", "ORDER"}
    for match in re.finditer(r"(?<![A-Z0-9.])(?:US\.)?[A-Z]{1,6}(?:\.[A-Z])?(?![A-Z0-9.])", text):
        value = match.group(0)
        if value in excluded:
            continue
        return normalize_symbol(value)
    return ""


def _parse_notional_usd(text: str) -> float:
    patterns = [
        r"\$\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:USD|US\$|刀|美元|美金)",
    ]
    return _first_number(patterns, text)


def _parse_quantity(text: str) -> float:
    return _first_number([r"(\d+(?:\.\d+)?)\s*(?:股|SHARE|SHARES)"], text)


def _parse_limit_price(text: str) -> float:
    patterns = [
        r"(?:购买价格固定|买入价格固定|卖出价格固定|固定价格|价格固定|限价|LIMIT|PRICE)\s*[:：=为是到在固定]*\s*(\d+(?:\.\d+)?)",
        r"(?:购买价格为|买入价格为|卖出价格为|价格为)\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:限价|固定价)",
    ]
    return _first_number(patterns, text)


def _parse_order_id(text: str) -> str:
    keyed = re.search(r"(?:订单号|ORDER\s*ID|ORDER_ID|ID|订单)\s*[:：#]?\s*([A-Z0-9-]{3,})", text)
    if keyed:
        return keyed.group(1)
    candidates = re.findall(r"\b[A-Z0-9][A-Z0-9-]{7,}\b", text)
    return candidates[0] if candidates else ""


def _first_number(patterns: list[str], text: str) -> float:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return 0.0


def _describe_command(command: TradeCommand) -> str:
    parts = [command.action]
    if command.symbol:
        parts.append(command.symbol)
    if command.notional_usd > 0:
        parts.append(f"金额 {command.notional_usd:.2f} USD")
    if command.quantity > 0:
        parts.append(f"数量 {command.quantity:.6f}")
    if command.limit_price > 0:
        parts.append(f"限价 {command.limit_price:.4f}")
    if command.limit_price_multiplier > 0:
        parts.append(f"动态限价 当前价*{command.limit_price_multiplier:.4f}")
    if command.order_id:
        parts.append(f"订单号 {command.order_id}")
    if command.cancel_all:
        parts.append("全部挂单")
    return " | ".join(parts)


def _response_message(results: list[OrderResult]) -> str:
    if not results:
        return "没有执行任何动作"
    if len(results) == 1:
        result = results[0]
        return f"{result.side} {result.symbol or result.order_id} 状态 {result.status}"
    return f"已处理 {len(results)} 笔订单：" + ", ".join(f"{result.symbol}:{result.status}" for result in results)


def _format_price(value: float) -> str:
    return f"{value:.4f}" if value > 0 else "未知"


def _parse_limit_price_multiplier(text: str) -> float:
    """识别“当前价*0.95”这类动态限价表达式。"""
    price_words = r"(?:当前价|当前价格|现价|实时价|CURRENT\s*PRICE|CURRENT|MARKET\s*PRICE|LAST\s*PRICE)"
    patterns = [
        rf"{price_words}\s*(?:\*|X|×|乘以|乘)\s*(\d+(?:\.\d+)?)",
        rf"(\d+(?:\.\d+)?)\s*(?:\*|X|×|乘以|乘)\s*{price_words}",
    ]
    value = _first_number(patterns, text)
    return value if 0 < value < 10 else 0.0


def _dynamic_limit_price(current_price: float, multiplier: float) -> float:
    """把当前价倍率换算成 Alpaca 可提交的两位小数限价。"""
    if current_price <= 0 or multiplier <= 0:
        return 0.0
    return round(current_price * multiplier, 2)


def _has_market_order_intent(text: str) -> bool:
    """只有用户明确写市价/按当前价时，才允许没有限价的手动单。"""
    return any(word in text for word in ("市价", "按当前价", "以当前价", "当前价买", "当前价卖", "MARKET", "MKT"))


def _unsupported_format_message(reason: str) -> str:
    """把拒绝原因和常用模板放在一起，方便 OpenClaw 原样回复。"""
    examples = "\n".join(f"- {item}" for item in SUPPORTED_TRADE_COMMAND_FORMATS)
    return f"交易指令格式不支持：{reason}\n常见可用格式：\n{examples}"
