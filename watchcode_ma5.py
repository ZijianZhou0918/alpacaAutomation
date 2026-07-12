from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import MA5_DIP_STRATEGY_NAME, build_settings
from alpaca_ma5_service.watchlist_generator import generate_watch_codes
from monitor_ma5_forever import BUY_NOTIONAL_USD, BUY_STOCK_COUNT, apply_ma5_dip_config


def generate_ma5_watchcode(
    symbols=None,
    max_symbols: int | None = None,
    lookback_days: int = 60,
    batch_size: int = 100,
    feed: str = "sip",
) -> None:
    """按最近已收盘日线生成 watch_codes.txt，并刷新图表页面。"""
    apply_ma5_dip_config()
    generate_watch_codes(
        settings=build_settings(
            strategy_name=MA5_DIP_STRATEGY_NAME,
            buy_stock_count=BUY_STOCK_COUNT,
            buy_notional_usd=BUY_NOTIONAL_USD,
        ),
        symbols=symbols,
        max_symbols=max_symbols,
        lookback_days=lookback_days,
        batch_size=batch_size,
        feed=feed,
    )


if __name__ == "__main__":
    # 点箭头运行只改这里；symbols=None 表示扫描 Alpaca 全部可交易普通股。
    generate_ma5_watchcode()
