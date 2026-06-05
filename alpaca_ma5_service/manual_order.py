from __future__ import annotations

from dataclasses import dataclass

from .alpaca_connection import build_trading_connection
from .config import Settings, build_settings
from .errors import short_error
from .market_data import build_market_data
from .market_time import now_market_time
from .models import MarketSnapshot, OrderResult
from .order_guard import normalize_order_status, wait_for_fill_or_cancel
from .trade_notifications import notify_order_submitted, record_order_and_notify
from .watchlist import normalize_symbol, to_alpaca_symbol


@dataclass(frozen=True)
class TestOrderPreview:
    """测试下单预览：复用真实下单行情，但不提交订单。"""

    snapshot: MarketSnapshot
    side: str
    buy_notional_usd: float
    limit_price_multiplier: float
    limit_price: float
    quantity: float


def place_test_order(
    symbol: str = "AAPL",
    buy_notional_usd: float = 5.0,
    limit_price_multiplier: float = 0.90,
    settings: Settings | None = None,
    market_data=None,
    client=None,
    cancel_after_seconds: int | None = None,
    order_status_poll_seconds: int | None = None,
) -> OrderResult:
    """
    PyCharm 点箭头入口：提交一笔真实 Alpaca BUY LIMIT 测试单。
    股票、金额、折扣价都在 run_test_order.py 里改。
    """
    settings = settings or build_settings()
    cancel_after_seconds = settings.order_cancel_after_seconds if cancel_after_seconds is None else cancel_after_seconds
    order_status_poll_seconds = settings.order_status_poll_seconds if order_status_poll_seconds is None else order_status_poll_seconds

    symbol = normalize_symbol(symbol)
    created_market_data = market_data is None
    market_data = market_data or build_market_data(settings)
    mode = "CLIENT"
    broker_name = "alpaca-client"
    if client is None:
        connection = build_trading_connection()
        client = connection.client
        mode = "PAPER" if connection.paper else "LIVE"
        broker_name = "alpaca-paper" if connection.paper else "alpaca-live"

    try:
        preview = build_test_order_preview(
            symbol=symbol,
            buy_notional_usd=buy_notional_usd,
            limit_price_multiplier=limit_price_multiplier,
            settings=settings,
            market_data=market_data,
        )
        snapshot = preview.snapshot
        limit_price = preview.limit_price
        quantity = preview.quantity
        reason = f"manual test limit buy at {limit_price_multiplier:.0%} of current price"

        print("=== Alpaca 真实测试下单 ===", flush=True)
        print(f"账户模式：{mode}", flush=True)
        print(f"行情来源：{settings.realtime_price_source.upper()} 实时价 + Alpaca 日线", flush=True)
        print(f"股票代码：{symbol}", flush=True)
        print("方向：买入", flush=True)
        print(f"当前价：{snapshot.current_price:.4f}", flush=True)
        print(f"当前价来源：{snapshot.current_price_source or '未知'}", flush=True)
        if snapshot.today_open > 0:
            print(f"今日开盘：{snapshot.today_open:.4f}（来源：{snapshot.today_open_source or '未知'}）", flush=True)
        else:
            print("今日开盘：未知", flush=True)
        print(f"限价：{limit_price:.2f}", flush=True)
        print(f"数量：{quantity}", flush=True)
        print(f"前4日收盘：{format_previous_closes(snapshot)}", flush=True)
        print(f"今日动态MA5：{snapshot.today_ma5:.4f}", flush=True)

        # 这里会真实提交订单；未在配置时间内完全成交就自动撤单。
        try:
            raw = _submit_limit_buy(client, symbol, quantity, limit_price)
            submitted = OrderResult(
                str(getattr(raw, "id", "") or ""),
                symbol,
                "BUY",
                quantity,
                limit_price,
                normalize_order_status(raw) or "SUBMITTED",
                f"Alpaca {mode.lower()} 测试单已提交",
            )
            notify_order_submitted(settings, submitted, reason, broker_name=broker_name)
            result = wait_for_fill_or_cancel(
                client,
                raw,
                symbol,
                "BUY",
                quantity,
                limit_price,
                mode.lower(),
                timeout_seconds=cancel_after_seconds,
                poll_seconds=order_status_poll_seconds,
            )
        except Exception as exc:
            result = OrderResult("", symbol, "BUY", quantity, limit_price, "REJECTED", short_error(exc))
        order_time = now_market_time(settings)
        record_order_and_notify(settings, result, reason, broker_name=broker_name, order_time=order_time)

        print(f"订单状态：{result.status}", flush=True)
        print(f"订单ID：{result.order_id}", flush=True)
        print(f"成交/下单数量：{result.quantity}", flush=True)
        print(f"使用限价：{result.price:.2f}", flush=True)
        print(f"消息：{result.message}", flush=True)
        print("=== 完成 ===", flush=True)
        return result
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()


def build_test_order_preview(
    symbol: str = "AAPL",
    buy_notional_usd: float = 5.0,
    limit_price_multiplier: float = 0.90,
    settings: Settings | None = None,
    market_data=None,
) -> TestOrderPreview:
    """读取行情和 MA 数据，只计算 BUY LIMIT 参数，不提交订单。"""
    settings = settings or build_settings()
    symbol = normalize_symbol(symbol)
    created_market_data = market_data is None
    market_data = market_data or build_market_data(settings)

    try:
        snapshot = market_data.get_snapshot(symbol)
        limit_price = discounted_limit_price(snapshot.current_price, limit_price_multiplier)
        quantity = quantity_for_notional(buy_notional_usd, limit_price)
        return TestOrderPreview(
            snapshot=snapshot,
            side="BUY",
            buy_notional_usd=buy_notional_usd,
            limit_price_multiplier=limit_price_multiplier,
            limit_price=limit_price,
            quantity=quantity,
        )
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()


def format_previous_closes(snapshot: MarketSnapshot) -> str:
    """格式化动态 MA5 使用的前 4 个完成日收盘价。"""
    closes = ", ".join(f"{close:.4f}" for close in snapshot.previous_closes[-4:])
    return f"[{closes}]"


def discounted_limit_price(current_price: float, multiplier: float) -> float:
    """用当前价和折扣系数计算测试限价。"""
    if current_price <= 0:
        raise ValueError("当前价必须大于 0")
    if multiplier <= 0:
        raise ValueError("折扣系数必须大于 0")
    return round(current_price * multiplier, 2)


def quantity_for_notional(notional_usd: float, limit_price: float) -> float:
    """按目标金额和限价换算下单股数。"""
    if notional_usd <= 0:
        raise ValueError("目标金额必须大于 0")
    if limit_price <= 0:
        raise ValueError("限价必须大于 0")
    return round(notional_usd / limit_price, 6)


def _submit_limit_buy(client, symbol: str, quantity: float, limit_price: float):
    """向 Alpaca 提交真实 BUY LIMIT 测试单。"""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    request = LimitOrderRequest(
        symbol=to_alpaca_symbol(symbol),
        qty=quantity,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    return client.submit_order(order_data=request)
