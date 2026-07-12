from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from datetime import date, timedelta
from pathlib import Path

from alpaca_ma5_service.config import BASE_DIR, build_settings
from alpaca_ma5_service.envfile import load_env_file
from backtest.daily_sources import load_massive_api_keys
from backtest.data_sync import DataSyncConfig, run_data_sync


def main() -> None:
    project_settings = build_settings()
    local_env = load_env_file(BASE_DIR / ".env")

    # ===== Full 1Min data sync configuration: edit here for PyCharm click-run =====
    today = date.today()
    manual_stock_symbols: list[str] = []
    stock_pool_max_symbols = None
    normal_stock_symbol_pattern = r"^[A-Z]{1,4}$"

    sync_start_date = today - timedelta(days=365)
    sync_end_date = today - timedelta(days=1)
    timeframe = "1Day"
    data_feed = "sip"
    daily_data_source = "massive"
    batch_size = 100
    minute_chunk_days = 1
    daily_chunk_days = 370
    sync_daily_bars = True
    sync_minute_bars = False
    refresh_data_cache = False
    moomoo_history_max_requests_per_window = 50
    moomoo_history_request_window_seconds = 30.0
    moomoo_history_rate_limit_retry_seconds = 31.0
    moomoo_history_max_retries = 3
    yahoo_request_sleep_seconds = 0.05
    yahoo_rate_limit_retry_seconds = 10.0
    yahoo_max_retries = 3
    massive_api_keys = load_massive_api_keys(local_env)
    massive_max_workers = 3
    massive_request_timeout_seconds = 30.0
    massive_retry_sleep_seconds = 3.0
    massive_max_retries = 3
    massive_progress_interval_seconds = 10.0
    massive_progress_interval_dates = 20
    massive_retry_failed_dates_until_complete = True
    massive_failed_date_retry_sleep_seconds = 75.0
    massive_failed_date_retry_sleep_multiplier = 1.25
    massive_failed_date_retry_max_sleep_seconds = 300.0
    massive_failed_date_max_retry_rounds = None
    massive_fallback_to_yahoo = False

    # None means full run. Set to 1 or 2 when you only want a quick smoke test.
    max_date_chunks = None

    data_cache_dir = BASE_DIR / "backtest" / "data"
    data_cache_name = "market_data.sqlite"
    output_dir = BASE_DIR / "backtest" / "output"
    summary_csv_name = "data_sync_summary.csv"

    config = DataSyncConfig(
        symbols=manual_stock_symbols,
        start_date=sync_start_date,
        end_date=sync_end_date,
        timeframe=timeframe,
        data_feed=data_feed,
        daily_data_source=daily_data_source,
        batch_size=batch_size,
        minute_chunk_days=minute_chunk_days,
        daily_chunk_days=daily_chunk_days,
        market_timezone=project_settings.market_timezone,
        normal_stock_symbol_pattern=normal_stock_symbol_pattern,
        stock_pool_max_symbols=stock_pool_max_symbols,
        sync_daily_bars=sync_daily_bars,
        sync_minute_bars=sync_minute_bars,
        refresh_data_cache=refresh_data_cache,
        max_date_chunks=max_date_chunks,
        data_cache_dir=data_cache_dir,
        data_cache_name=data_cache_name,
        output_dir=output_dir,
        summary_csv_name=summary_csv_name,
        moomoo_host=project_settings.moomoo_host,
        moomoo_port=project_settings.moomoo_port,
        moomoo_security_firm=project_settings.moomoo_security_firm,
        moomoo_connect_timeout=project_settings.moomoo_connect_timeout,
        moomoo_opend_exe_path=project_settings.moomoo_opend_exe_path,
        moomoo_opend_startup_timeout=project_settings.moomoo_opend_startup_timeout,
        moomoo_history_max_requests_per_window=moomoo_history_max_requests_per_window,
        moomoo_history_request_window_seconds=moomoo_history_request_window_seconds,
        moomoo_history_rate_limit_retry_seconds=moomoo_history_rate_limit_retry_seconds,
        moomoo_history_max_retries=moomoo_history_max_retries,
        yahoo_request_sleep_seconds=yahoo_request_sleep_seconds,
        yahoo_rate_limit_retry_seconds=yahoo_rate_limit_retry_seconds,
        yahoo_max_retries=yahoo_max_retries,
        massive_api_keys=massive_api_keys,
        massive_max_workers=massive_max_workers,
        massive_request_timeout_seconds=massive_request_timeout_seconds,
        massive_retry_sleep_seconds=massive_retry_sleep_seconds,
        massive_max_retries=massive_max_retries,
        massive_progress_interval_seconds=massive_progress_interval_seconds,
        massive_progress_interval_dates=massive_progress_interval_dates,
        massive_retry_failed_dates_until_complete=massive_retry_failed_dates_until_complete,
        massive_failed_date_retry_sleep_seconds=massive_failed_date_retry_sleep_seconds,
        massive_failed_date_retry_sleep_multiplier=massive_failed_date_retry_sleep_multiplier,
        massive_failed_date_retry_max_sleep_seconds=massive_failed_date_retry_max_sleep_seconds,
        massive_failed_date_max_retry_rounds=massive_failed_date_max_retry_rounds,
        massive_fallback_to_yahoo=massive_fallback_to_yahoo,
    )

    stats = run_data_sync(config)
    print("Data sync finished.")
    print(f"Symbols: {stats.symbol_count}")
    print(f"Daily rows written: {stats.daily_rows:,}")
    print(f"1Min rows written: {stats.minute_rows:,}")
    print(f"Daily symbol-ranges fetched/skipped: {stats.fetched_daily_symbol_ranges:,} / {stats.skipped_daily_symbol_ranges:,}")
    print(f"1Min symbol-ranges fetched/skipped: {stats.fetched_minute_symbol_ranges:,} / {stats.skipped_minute_symbol_ranges:,}")
    print(f"Cache: {stats.cache_path}")
    print(f"Summary CSV: {stats.summary_path}")


if __name__ == "__main__":
    main()
