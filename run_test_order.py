from entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.manual_order import place_test_order


def run_test_limit_order(
    symbol: str,
    buy_notional_usd: float,
    limit_price_multiplier: float,
    cancel_after_seconds: int,
    order_status_poll_seconds: int,
) -> None:
    """提交真实 Alpaca BUY LIMIT 测试单，用来验证下单和自动撤单链路。"""
    place_test_order(
        symbol=symbol,
        buy_notional_usd=buy_notional_usd,
        limit_price_multiplier=limit_price_multiplier,
        cancel_after_seconds=cancel_after_seconds,
        order_status_poll_seconds=order_status_poll_seconds,
    )


if __name__ == "__main__":
    # 点箭头运行只改这里：金额很小，超时未成交会自动撤单。
    run_test_limit_order(
        symbol="NTAP",
        buy_notional_usd=5.0,
        limit_price_multiplier=0.9,
        cancel_after_seconds=300,
        order_status_poll_seconds=5,
    )
