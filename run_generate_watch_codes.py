from __future__ import annotations

from entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.watchlist_generator import generate_watch_codes


def run_generate_watch_codes(
    symbols,
    max_symbols: int | None,
    lookback_days: int,
    batch_size: int,
    feed: str,
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
    run_generate_watch_codes(
        symbols=None,
        max_symbols=None,
        lookback_days=60,
        batch_size=100,
        feed="sip",
    )
