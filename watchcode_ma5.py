from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.watchlist_generator import generate_watch_codes


def generate_ma5_watchcode(
    symbols=None,
    max_symbols: int | None = None,
    lookback_days: int = 60,
    batch_size: int = 100,
    feed: str = "sip",
) -> None:
    """按最近已收盘日线生成 watch_codes.txt，并刷新图表页面。"""
    generate_watch_codes(
        settings=build_settings(),
        symbols=symbols,
        max_symbols=max_symbols,
        lookback_days=lookback_days,
        batch_size=batch_size,
        feed=feed,
    )


if __name__ == "__main__":
    # 点箭头运行只改这里；symbols=None 表示扫描 Alpaca 全部可交易普通股。
    generate_ma5_watchcode()
