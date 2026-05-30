from entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.manual_order import place_test_order


def run_test_limit_order(symbol: str, buy_notional_usd: float, limit_price_multiplier: float) -> None:
    """点击运行用；提交一笔真实 Alpaca BUY LIMIT 测试单。"""
    place_test_order(
        symbol=symbol,
        buy_notional_usd=buy_notional_usd,
        limit_price_multiplier=limit_price_multiplier,
    )


if __name__ == "__main__":
    # 在这里改测试下单参数；不用 parser.add_argument。
    run_test_limit_order(
        symbol="NTAP",
        buy_notional_usd=5.0,
        limit_price_multiplier=0.9,
    )
