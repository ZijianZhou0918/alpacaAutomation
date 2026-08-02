from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .broker import AlpacaStockBroker
from .config import Settings, build_settings
from .market_data import build_market_data
from .models import OrderResult
from .trade_notifications import format_broker_name, order_kind_text, render_trade_order_messages
from .watchlist import normalize_symbol


TRADE_COMMAND_EXAMPLES_BY_ACTION = {
    "买入": [
        "帮我买3000刀的NTAP，购买价格固定160",
        "帮我买3000刀的NTAP，购买价格为当前价*0.95",
        "帮我买3000刀的NTAP，市价买入",
    ],
    "卖出": [
        "帮我卖出10股NTAP，限价160",
        "帮我卖出10股NTAP，价格为当前价*1.05",
        "帮我卖出NTAP，市价卖出",
    ],
    "撤单": [
        "撤单NTAP",
        "撤单 订单号: FU1C9F9AADE3E56000",
        "全部撤单",
    ],
}
SUPPORTED_TRADE_COMMAND_FORMATS = [
    example
    for examples in TRADE_COMMAND_EXAMPLES_BY_ACTION.values()
    for example in examples
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
    """执行 OpenClaw 买入、卖出或撤单指令。

    该入口是明确的手动交易路径，不调用自动 MA5 买卖策略；解析完成后直接调用
    Broker 的 ``place_*`` / ``cancel_*``，因此可能产生真实账户写操作。
    Broker 仍统一负责时段转换、订单记录、通知和超时撤单。
    """
    # 【手动交易总入口 1/5：加载运行配置】
    # 这里取得的策略/超时/通知配置与自动监控共用，但本函数不会调用自动 MA5
    # 买卖判断。调用方未显式传入 Settings 时，读取项目当前的本机配置。
    settings = settings or build_settings()

    # 【手动交易总入口 2/5：把文本解析成结构化指令】
    # parse_trade_command 只解析动作、股票、金额/股数、限价和订单号，不访问券商；
    # 解析失败会在 Broker 写入发生前抛错，避免用不完整参数猜测下单。
    command = parse_trade_command(message_or_command) if isinstance(message_or_command, str) else message_or_command

    # 【手动交易总入口 3/5：建立真实订单边界】
    # 调用方没有注入 fake/dry-run broker 时，此处创建 AlpacaStockBroker，并自动
    # 识别当前 key 属于 Paper 还是 Live。后面的 place_*/cancel_* 都可能外部写入。
    broker = broker or AlpacaStockBroker(settings)
    broker_name = broker.source_name()

    # reason 会进入本地订单记录和通知，用于区分手动指令与自动监控订单；
    # 它只描述本次来源，不参与价格、数量或授权判断。
    reason = f"OpenClaw手动指令: {command.raw_message}"

    # 【手动交易总入口 4/5：按动作分发】
    # 三个分支都绕过自动 MA5 信号，但不会绕过 Broker 的数量校验、订单构造、
    # 结果记录和终态/撤单确认。skip_time_validation 只在各手动执行函数内显式传入。
    if command.action == "BUY":
        # 【手动买入路由】进入 _execute_buy 后直接调用 Broker。
        results = [_execute_buy(command, broker, settings, market_data, reason)]
    elif command.action == "SELL":
        # 【手动卖出路由】进入 _execute_sell 后直接调用 Broker。
        results = _execute_sell(command, broker, settings, market_data, reason)
    elif command.action == "CANCEL":
        # 【手动撤单路由】进入 _execute_cancel 后直接调用 Broker。
        results = _execute_cancel(command, broker, reason)
    else:
        raise ValueError(f"未知交易动作：{command.action}")

    # 【手动交易总入口 5/5：汇总 Broker 返回的真实状态】
    # ok 只表示没有 REJECTED/CANCEL_FAILED；成交、部分成交、已撤销和撤单待确认
    # 仍由每个 OrderResult.status 精确表达，不能仅凭 ok=True 宣称已经完全成交。
    ok = all(result.status.upper() not in {"REJECTED", "CANCEL_FAILED"} for result in results)
    return TradeCommandResponse(ok=ok, command=command, broker_name=broker_name, results=results, message=_response_message(results))


def _execute_buy(command: TradeCommand, broker, settings: Settings, market_data, reason: str) -> OrderResult:
    """执行手动买入；固定价/倍率用 LIMIT，明确写市价才用 MARKET。

    所有返回 ``broker.place_*`` 的分支都是实际下单边界；这里使用
    ``skip_time_validation=True`` 是手动授权语义，但券商仍会校验订单。
    """
    # 【手动买入 1/3：固定限价直接下单】
    # 指令已经给出绝对限价时无需读取实时行情；未直接给美元金额时，才用
    # “股数 × 限价”换算 notional。Broker 随后会把 notional 再换算成整数股。
    if command.limit_price > 0:
        notional = command.notional_usd or command.quantity * command.limit_price
        # 【执行买入订单：OpenClaw】固定价格 BUY LIMIT。
        return broker.place_limit_buy(command.symbol, notional, command.limit_price, reason, skip_time_validation=True)

    # 【手动买入 2/3：需要实时价时建立行情资源】
    # 动态限价和明确市价单都必须先取得当前行情；只有本函数自行创建的行情对象
    # 才在 finally 中关闭，调用方复用传入的行情对象不会被意外关闭。
    created_market_data = market_data is None
    market_data = market_data or build_market_data(settings)
    try:
        snapshot = market_data.get_snapshot(command.symbol)
        if command.limit_price_multiplier > 0:
            # 动态限价严格按“实时价 × 用户倍率”计算，不回退为自动策略买点。
            limit_price = _dynamic_limit_price(snapshot.current_price, command.limit_price_multiplier)
            notional = command.notional_usd or command.quantity * limit_price
            reason = (
                f"{reason} | 动态限价: 当前价 {snapshot.current_price:.4f} "
                f"* {command.limit_price_multiplier:.4f} = {limit_price:.2f}"
            )
            # 【执行买入订单：OpenClaw】按实时价倍率计算的 BUY LIMIT。
            return broker.place_limit_buy(command.symbol, notional, limit_price, reason, skip_time_validation=True)

        # 【手动买入 3/3：明确市价路径】
        # 只有解析器已确认用户明确表达 MARKET 意图才会到达此处；不能把缺少限价
        # 的模糊指令默认解释成市价单。真实写入仍发生在 Broker.submit_order。
        notional = command.notional_usd or command.quantity * snapshot.current_price
        # 【执行买入订单：OpenClaw】用户明确要求的 MARKET 买入。
        return broker.place_market_buy(command.symbol, notional, snapshot.current_price, reason, skip_time_validation=True)
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()


def _execute_sell(command: TradeCommand, broker, settings: Settings, market_data, reason: str) -> list[OrderResult]:
    """执行手动卖出；不写股数时默认卖出该股票当前全部持仓。

    固定价/倍率走 LIMIT，明确写市价才走 MARKET；这些分支直接进入 Broker，
    不经过自动监控的止损、止盈或尾盘策略。
    """
    # 【手动卖出 1/3：解析实际卖出数量】
    # 用户没有给股数时，只读取目标股票当前持仓并默认卖出全部；不会遍历或清空
    # 其他股票。没有持仓时数量保持 0，交给 Broker 明确返回 REJECTED。
    quantity = command.quantity
    if quantity <= 0:
        position = broker.get_positions().get(command.symbol)
        quantity = position.quantity if position else 0.0

    # 【手动卖出 2/3：固定限价无需读取实时行情】
    # 该路径不经过自动止损/止盈策略，指定价格和数量直接交由 Broker 校验并提交。
    if command.limit_price > 0:
        # 【执行卖出订单：OpenClaw】固定价格 SELL LIMIT。
        return [broker.place_limit_sell(command.symbol, quantity, command.limit_price, reason, skip_time_validation=True)]

    # 【手动卖出 3/3：动态限价或明确市价路径】
    # 与买入相同，只关闭本函数自行创建的行情对象；倍率路径生成 SELL LIMIT，
    # 无倍率路径则必须来自明确的 MARKET 指令，最终都进入 Broker 真实写入边界。
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
            # 【执行卖出订单：OpenClaw】按实时价倍率计算的 SELL LIMIT。
            return [broker.place_limit_sell(command.symbol, quantity, limit_price, reason, skip_time_validation=True)]
        # 【执行卖出订单：OpenClaw】用户明确要求的 MARKET 卖出。
        return [broker.place_market_sell(command.symbol, quantity, snapshot.current_price, reason, skip_time_validation=True)]
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()


def _execute_cancel(command: TradeCommand, broker, reason: str) -> list[OrderResult]:
    """执行手动撤单；优先按订单号，其次按股票代码，最后才允许全部撤单。

    这些分支最终进入 ``order_guard.cancel_unfilled_order`` 的真实撤单写入点。
    """
    # 【手动撤单 1/3：订单号优先】
    # 唯一订单号最精确；Broker 会先读取最新状态，若已成交/已撤销等终态则不再
    # 发送撤单请求，避免把终态订单误报为新撤单。
    if command.order_id:
        # 【执行撤单请求：OpenClaw】按唯一订单号撤单。
        return [broker.cancel_order(command.order_id, reason)]

    # 【手动撤单 2/3：其次限定股票】
    # Broker 只查询该股票的开放订单，再逐笔复用 cancel_order 的终态保护。
    if command.symbol:
        # 【执行撤单请求：OpenClaw】查找并撤销指定股票的开放订单。
        return broker.cancel_open_orders(command.symbol, reason)

    # 【手动撤单 3/3：全部开放订单是最宽范围】
    # 只有 parse_trade_command 已明确识别为“全部撤单”且没有订单号/股票时才到达；
    # 空字符串在 Broker 中代表查询全部开放订单，不代表取消持仓。
    # 【执行撤单请求：OpenClaw】只有明确解析为“全部撤单”才会走到这里。
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
    """用与订单通知一致的固定字段格式生成 Agent 交易指令回复。"""
    command_kind = order_kind_text(response.command.action)
    overview = "✅ 请求已处理" if response.ok else "❌ 请求失败或需要处理"
    lines = [
        f"【Agent 交易指令｜{command_kind}】",
        f"总览：{overview}",
        f"账户：{format_broker_name(response.broker_name)}",
        f"原始指令：{response.command.raw_message}",
        f"解析结果：{_describe_command(response.command)}",
        f"汇总：{response.message}",
    ]
    result_count = len(response.results)
    for index, result in enumerate(response.results, start=1):
        detail = render_trade_order_messages(
            result,
            f"Agent 手动指令：{response.command.raw_message}",
            broker_name=response.broker_name,
        )[0]
        lines.extend(["", f"订单结果 {index}/{result_count}", detail])
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
    return f"交易指令格式不支持：{reason}\n{supported_trade_command_help()}"


def supported_trade_command_help() -> str:
    """集中维护 OpenClaw 指令约束和示例，避免拒绝回复里复制 prompt。"""
    examples = "\n".join(f"- {item}" for item in SUPPORTED_TRADE_COMMAND_FORMATS)
    return (
        "目标：只执行明确的买入、卖出、撤单指令。\n"
        "输入要求：买入/卖出必须包含股票代码；买入必须包含金额或股数；买卖必须明确固定限价、当前价倍率或市价。\n"
        "失败处理：格式不完整时直接拒绝，不猜金额、价格或方向。\n"
        f"常见可用格式：\n{examples}"
    )
