from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.premarket_watchlist import generate_premarket_watch_codes


# 点运行箭头只改这里。
TOP_COUNT = 50
MAX_SYMBOLS = None
LOOKBACK_DAYS = 20
BATCH_SIZE = 100
FEED = "sip"


def generate_premarket_watchcode() -> None:
    """Click-run entry: generate watch_codes_premarket.txt."""
    generate_premarket_watch_codes(
        max_symbols=MAX_SYMBOLS,
        top_count=TOP_COUNT,
        lookback_days=LOOKBACK_DAYS,
        batch_size=BATCH_SIZE,
        feed=FEED,
    )


if __name__ == "__main__":
    generate_premarket_watchcode()
