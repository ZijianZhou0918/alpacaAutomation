from __future__ import annotations

from .alpaca_connection import build_trading_connection
from .config import Settings, build_settings
from .errors import short_error
from .market_data import AlpacaMarketData
from .models import OrderResult
from .order_guard import wait_for_fill_or_cancel
from .state import append_order
from .watchlist import normalize_symbol, to_alpaca_symbol


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
    Submit one real Alpaca limit-buy test order from PyCharm.
    Edit the values below before running if you want a different symbol or size.
    """
    settings = settings or build_settings()
    cancel_after_seconds = settings.order_cancel_after_seconds if cancel_after_seconds is None else cancel_after_seconds
    order_status_poll_seconds = settings.order_status_poll_seconds if order_status_poll_seconds is None else order_status_poll_seconds

    symbol = normalize_symbol(symbol)
    # 测试下单也使用真实监控同一套 Alpaca 行情源，避免价格口径不一致。
    market_data = market_data or AlpacaMarketData(settings.market_timezone)
    mode = "CLIENT"
    if client is None:
        connection = build_trading_connection()
        client = connection.client
        mode = "PAPER" if connection.paper else "LIVE"
    snapshot = market_data.get_snapshot(symbol)
    limit_price = discounted_limit_price(snapshot.current_price, limit_price_multiplier)
    quantity = quantity_for_notional(buy_notional_usd, limit_price)
    reason = f"manual test limit buy at {limit_price_multiplier:.0%} of current price"

    print("=== Alpaca real test order ===", flush=True)
    print(f"Mode: {mode}", flush=True)
    print("Price source: Alpaca Market Data", flush=True)
    print(f"Symbol: {symbol}", flush=True)
    print("Side: BUY", flush=True)
    print(f"Current price: {snapshot.current_price:.4f}", flush=True)
    print(f"Limit price: {limit_price:.2f}", flush=True)
    print(f"Quantity: {quantity}", flush=True)

    # 折价买入限价单会真实提交，但通常不会马上成交；超时后自动撤单。
    try:
        raw = _submit_limit_buy(client, symbol, quantity, limit_price)
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
    append_order(settings.output_dir, result, reason)

    print(f"Order status: {result.status}", flush=True)
    print(f"Order id: {result.order_id}", flush=True)
    print(f"Quantity: {result.quantity}", flush=True)
    print(f"Limit price used: {result.price:.2f}", flush=True)
    print(f"Message: {result.message}", flush=True)
    print("=== done ===", flush=True)
    return result


def discounted_limit_price(current_price: float, multiplier: float) -> float:
    """根据当前价和折扣系数计算测试限价。"""
    if current_price <= 0:
        raise ValueError("current_price must be positive")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")
    return round(current_price * multiplier, 2)


def quantity_for_notional(notional_usd: float, limit_price: float) -> float:
    """用目标金额和限价计算下单股数。"""
    if notional_usd <= 0:
        raise ValueError("notional_usd must be positive")
    if limit_price <= 0:
        raise ValueError("limit_price must be positive")
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
