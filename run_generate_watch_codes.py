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
    """点击运行用；按最新已收盘日线生成 watch_codes.txt。"""
    generate_watch_codes(
        settings=build_settings(),
        symbols=symbols,
        max_symbols=max_symbols,
        lookback_days=lookback_days,
        batch_size=batch_size,
        feed=feed,
    )


if __name__ == "__main__":
    # 在这里改选股参数；symbols=None 表示从 Alpaca assets 读取全部可交易美股。
    run_generate_watch_codes(
        symbols=None,
        max_symbols=None,
        lookback_days=60,
        batch_size=100,
        feed="sip",
    )
