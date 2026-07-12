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
from backtest.data_spotcheck import DataSpotcheckConfig, run_data_spotcheck


def main() -> None:
    local_env = load_env_file(BASE_DIR / ".env")

    # ===== Backtest DB spotcheck configuration: edit here for PyCharm click-run =====
    today = date.today()
    manual_stock_symbols: list[str] = []
    stock_pool_max_symbols = None
    normal_stock_symbol_pattern = r"^[A-Z]{1,4}$"

    spotcheck_start_date = today - timedelta(days=365)
    spotcheck_end_date = today
    sample_size = 100
    sample_seed = None
    feed = MASSIVE_DAILY_FEED
    adjustment = MASSIVE_DAILY_ADJUSTMENT

    massive_api_keys = load_massive_api_keys(local_env)
    massive_max_workers = 1
    massive_request_timeout_seconds = 30.0
    massive_retry_sleep_seconds = 5.0
    massive_max_retries = 2
    massive_request_spacing_seconds = 2.5
    massive_progress_interval_seconds = 10.0
    massive_progress_interval_dates = 10

    data_cache_dir = BASE_DIR / "backtest" / "data"
    data_cache_name = "market_data.sqlite"
    output_dir = BASE_DIR / "backtest" / "output"
    issue_csv_name = "daily_spotcheck_issues.csv"
    summary_csv_name = "daily_spotcheck_summary.csv"
    sampled_symbols_csv_name = "daily_spotcheck_sampled_symbols.csv"

    if manual_stock_symbols:
        symbols = manual_stock_symbols
    else:
        symbols = load_tradable_symbols(max_symbols=stock_pool_max_symbols)
    if normal_stock_symbol_pattern:
        pattern = re.compile(normal_stock_symbol_pattern)
        symbols = [symbol for symbol in symbols if pattern.fullmatch(to_alpaca_symbol(symbol) or "")]

    config = DataSpotcheckConfig(
        symbols=symbols,
        start_date=spotcheck_start_date,
        end_date=spotcheck_end_date,
        sample_size=sample_size,
        sample_seed=sample_seed,
        feed=feed,
        adjustment=adjustment,
        data_cache_dir=data_cache_dir,
        data_cache_name=data_cache_name,
        output_dir=output_dir,
        issue_csv_name=issue_csv_name,
        summary_csv_name=summary_csv_name,
        sampled_symbols_csv_name=sampled_symbols_csv_name,
        massive_api_keys=massive_api_keys,
        massive_max_workers=massive_max_workers,
        massive_request_timeout_seconds=massive_request_timeout_seconds,
        massive_retry_sleep_seconds=massive_retry_sleep_seconds,
        massive_max_retries=massive_max_retries,
        massive_request_spacing_seconds=massive_request_spacing_seconds,
        massive_progress_interval_seconds=massive_progress_interval_seconds,
        massive_progress_interval_dates=massive_progress_interval_dates,
    )

    result = run_data_spotcheck(config)
    print("Backtest data spotcheck finished.")
    print(f"Sample seed: {result.sample_seed}")
    print(f"Sampled symbols: {len(result.sampled_symbols)}")
    print(f"Cache: {result.cache_path}")
    print(f"Sampled symbols CSV: {result.sampled_symbols_path}")
    print(f"Issues CSV: {result.issue_csv_path}")
    print(f"Summary CSV: {result.summary_csv_path}")
    print(f"Local rows checked: {result.local_rows_checked:,}")
    print(f"Remote rows checked: {result.remote_rows_checked:,}")
    print(f"Issues: {result.issue_count:,}")
    print(f"Symbols with issues: {result.symbols_with_issues}")
    print(f"Massive fetch failures: {result.fetch_failures}")


if __name__ == "__main__":
    main()
