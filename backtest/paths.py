from __future__ import annotations

from alpaca_ma5_service.config import BASE_DIR


OFFICIAL_DAILY_DB_PATH = (
    BASE_DIR / "backtest" / "data" / "market_data.sqlite"
)
SIGNAL_DYNAMIC_MA5_MINUTE_CACHE_PATH = (
    BASE_DIR
    / "backtest"
    / "data"
    / "signal_dynamic_ma5_minute_cache.sqlite"
)
