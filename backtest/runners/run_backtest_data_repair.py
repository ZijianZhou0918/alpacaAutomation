"""Backtest-data repair command implementation."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from datetime import date, timedelta
import re

from alpaca_ma5_service.config import BASE_DIR
from alpaca_ma5_service.envfile import load_env_file
from alpaca_ma5_service.watchlist import to_alpaca_symbol
from alpaca_ma5_service.watchlist_generator import load_tradable_symbols
from backtest.daily_sources import MASSIVE_DAILY_ADJUSTMENT, MASSIVE_DAILY_FEED, load_massive_api_keys
from backtest.data_repair import DataRepairConfig, run_data_repair


def main() -> None:
    local_env = load_env_file(BASE_DIR / ".env")

    # ===== Backtest DB repair configuration: edit here for PyCharm click-run =====
    today = date.today()
    manual_stock_symbols: list[str] = []
    stock_pool_max_symbols = None
    normal_stock_symbol_pattern = r"^[A-Z]{1,4}$"

    repair_start_date = today - timedelta(days=365)
    repair_end_date = today
    ma_warmup_calendar_days = 45
    feed = MASSIVE_DAILY_FEED
    adjustment = MASSIVE_DAILY_ADJUSTMENT

    create_backup = True
    delete_invalid_ohlc_rows = True
    delete_untrusted_fetch_ranges = True
    recompute_daily_mas = True
    backfill_low_coverage_dates = True
    min_range_date_coverage_ratio = 0.98
    min_daily_symbol_coverage_ratio = 0.65
    max_backfill_dates = None

    massive_api_keys = load_massive_api_keys(local_env)
    massive_max_workers = 4
    massive_request_timeout_seconds = 30.0
    massive_retry_sleep_seconds = 5.0
    massive_max_retries = 2
    massive_progress_interval_seconds = 10.0
    massive_progress_interval_dates = 10

    data_cache_dir = BASE_DIR / "backtest" / "data"
    data_cache_name = "market_data.sqlite"
    output_dir = BASE_DIR / "backtest" / "output"

    if manual_stock_symbols:
        symbols = manual_stock_symbols
    else:
        symbols = load_tradable_symbols(max_symbols=stock_pool_max_symbols)
    if normal_stock_symbol_pattern:
        pattern = re.compile(normal_stock_symbol_pattern)
        symbols = [symbol for symbol in symbols if pattern.fullmatch(to_alpaca_symbol(symbol) or "")]

    config = DataRepairConfig(
        symbols=symbols,
        start_date=repair_start_date,
        end_date=repair_end_date,
        ma_warmup_calendar_days=ma_warmup_calendar_days,
        feed=feed,
        adjustment=adjustment,
        data_cache_dir=data_cache_dir,
        data_cache_name=data_cache_name,
        output_dir=output_dir,
        create_backup=create_backup,
        delete_invalid_ohlc_rows=delete_invalid_ohlc_rows,
        delete_untrusted_fetch_ranges=delete_untrusted_fetch_ranges,
        recompute_daily_mas=recompute_daily_mas,
        backfill_low_coverage_dates=backfill_low_coverage_dates,
        min_range_date_coverage_ratio=min_range_date_coverage_ratio,
        min_daily_symbol_coverage_ratio=min_daily_symbol_coverage_ratio,
        max_backfill_dates=max_backfill_dates,
        massive_api_keys=massive_api_keys,
        massive_max_workers=massive_max_workers,
        massive_request_timeout_seconds=massive_request_timeout_seconds,
        massive_retry_sleep_seconds=massive_retry_sleep_seconds,
        massive_max_retries=massive_max_retries,
        massive_progress_interval_seconds=massive_progress_interval_seconds,
        massive_progress_interval_dates=massive_progress_interval_dates,
    )

    result = run_data_repair(config)
    print("Backtest data repair finished.")
    print(f"Symbols: {len(symbols)}")
    print(f"Cache: {result.cache_path}")
    print(f"Backup: {result.backup_path}")
    print(f"Integrity CSV: {result.audit_csv_path}")
    print(f"Invalid OHLC rows deleted: {result.invalid_rows_deleted}")
    print(f"Untrusted fetch ranges deleted: {result.fetch_ranges_deleted}")
    print(f"MA symbols recomputed: {result.ma_symbols_recomputed}")
    print(f"Target-range null MA rows remaining: {result.target_null_ma_rows}")
    print(f"Low/missing coverage dates after repair: {result.low_coverage_dates}")
    print(f"Backfill rows written: {result.backfill_rows_written:,}")
    print(f"Backfill unresolved failures: {result.backfill_failures}")


if __name__ == "__main__":
    main()
