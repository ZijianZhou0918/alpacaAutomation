from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep
from zoneinfo import ZoneInfo

from alpaca_ma5_service.afterhours_high_low import fetch_minute_bars
from alpaca_ma5_service.errors import short_error
from alpaca_ma5_service.watchlist import to_alpaca_symbol
from alpaca_ma5_service.watchlist_generator import DailyBar, load_tradable_symbols
from backtest.data_cache import ADJUSTMENT_SPLIT, MarketDataCache, normalize_symbols, utc_key
from backtest.daily_sources import (
    MASSIVE_DAILY_ADJUSTMENT,
    MASSIVE_DAILY_FEED,
    MOOMOO_DAILY_ADJUSTMENT,
    MOOMOO_DAILY_FEED,
    YAHOO_DAILY_ADJUSTMENT,
    YAHOO_DAILY_FEED,
    DailyFetchResult,
    MassiveDailyConfig,
    MoomooDailyConfig,
    YahooDailyConfig,
    fetch_massive_grouped_daily_bars_with_failures,
    fetch_moomoo_daily_bars,
    fetch_yahoo_daily_bars_with_failures,
    coalesced_date_ranges,
    failure_dates,
    filter_daily_bars_to_dates,
    format_elapsed,
    is_massive_rate_limit_failure,
    merge_daily_bars,
)


@dataclass(frozen=True)
class DataSyncConfig:
    symbols: list[str]
    start_date: date
    end_date: date
    timeframe: str
    data_feed: str
    daily_data_source: str
    batch_size: int
    minute_chunk_days: int
    daily_chunk_days: int
    market_timezone: str
    normal_stock_symbol_pattern: str
    stock_pool_max_symbols: int | None
    sync_daily_bars: bool
    sync_minute_bars: bool
    refresh_data_cache: bool
    max_date_chunks: int | None
    data_cache_dir: Path
    data_cache_name: str
    output_dir: Path
    summary_csv_name: str
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    moomoo_security_firm: str = "FUTUINC"
    moomoo_connect_timeout: float = 3.0
    moomoo_opend_exe_path: str = ""
    moomoo_opend_startup_timeout: float = 30.0
    moomoo_history_max_requests_per_window: int = 50
    moomoo_history_request_window_seconds: float = 30.0
    moomoo_history_rate_limit_retry_seconds: float = 31.0
    moomoo_history_max_retries: int = 3
    yahoo_request_sleep_seconds: float = 0.05
    yahoo_rate_limit_retry_seconds: float = 10.0
    yahoo_max_retries: int = 3
    massive_api_keys: tuple[str, ...] = ()
    massive_max_workers: int = 12
    massive_request_timeout_seconds: float = 30.0
    massive_retry_sleep_seconds: float = 3.0
    massive_max_retries: int = 3
    massive_progress_interval_seconds: float = 10.0
    massive_progress_interval_dates: int = 20
    massive_retry_failed_dates_until_complete: bool = False
    massive_failed_date_retry_sleep_seconds: float = 75.0
    massive_failed_date_retry_sleep_multiplier: float = 1.25
    massive_failed_date_retry_max_sleep_seconds: float = 300.0
    massive_failed_date_max_retry_rounds: int | None = 3
    massive_fallback_to_yahoo: bool = False


@dataclass(frozen=True)
class SyncStats:
    symbol_count: int
    daily_rows: int
    minute_rows: int
    fetched_daily_symbol_ranges: int
    fetched_minute_symbol_ranges: int
    skipped_daily_symbol_ranges: int
    skipped_minute_symbol_ranges: int
    cache_path: Path
    summary_path: Path


def run_data_sync(config: DataSyncConfig) -> SyncStats:
    if config.sync_minute_bars and config.timeframe != "1Min":
        raise ValueError("1Min sync requires timeframe='1Min'.")
    if not config.sync_minute_bars and config.timeframe not in {"1Day", "1Min"}:
        raise ValueError("Daily-only sync requires timeframe='1Day' or '1Min'.")
    if config.start_date > config.end_date:
        raise ValueError("start_date must be <= end_date.")

    symbols = load_sync_symbols(config)
    if not symbols:
        raise ValueError("No symbols matched the configured stock pool filter.")

    cache = MarketDataCache(config.data_cache_dir / config.data_cache_name)
    rows: list[dict[str, object]] = []
    failure_rows: list[dict[str, object]] = []
    daily_rows = 0
    minute_rows = 0
    fetched_daily_symbol_ranges = 0
    fetched_minute_symbol_ranges = 0
    skipped_daily_symbol_ranges = 0
    skipped_minute_symbol_ranges = 0

    print(f"Sync symbols: {len(symbols)} daily_source={config.daily_data_source} intraday_feed={config.data_feed} cache={cache.path}", flush=True)
    print(
        f"Date range: {config.start_date} -> {config.end_date} inclusive",
        flush=True,
    )
    print(
        f"Planned chunks, not days: "
        f"daily={planned_chunk_count(config, config.daily_chunk_days) if config.sync_daily_bars else 0} chunks x {config.daily_chunk_days} days/chunk; "
        f"1Min={planned_chunk_count(config, config.minute_chunk_days) if config.sync_minute_bars else 0} chunks x {config.minute_chunk_days} days/chunk",
        flush=True,
    )

    if config.sync_daily_bars:
        stats = sync_daily_bars(config, cache, symbols, rows, failure_rows)
        daily_rows += stats["rows"]
        fetched_daily_symbol_ranges += stats["fetched"]
        skipped_daily_symbol_ranges += stats["skipped"]

    if config.sync_minute_bars:
        stats = sync_minute_bars(config, cache, symbols, rows)
        minute_rows += stats["rows"]
        fetched_minute_symbol_ranges += stats["fetched"]
        skipped_minute_symbol_ranges += stats["skipped"]

    summary_path = write_sync_summary(config, rows)
    if failure_rows:
        write_sync_failures(config, failure_rows)
    return SyncStats(
        symbol_count=len(symbols),
        daily_rows=daily_rows,
        minute_rows=minute_rows,
        fetched_daily_symbol_ranges=fetched_daily_symbol_ranges,
        fetched_minute_symbol_ranges=fetched_minute_symbol_ranges,
        skipped_daily_symbol_ranges=skipped_daily_symbol_ranges,
        skipped_minute_symbol_ranges=skipped_minute_symbol_ranges,
        cache_path=cache.path,
        summary_path=summary_path,
    )


def load_sync_symbols(config: DataSyncConfig) -> list[str]:
    if config.symbols:
        raw_symbols = config.symbols
        source = "manual"
    else:
        raw_symbols = load_tradable_symbols(max_symbols=config.stock_pool_max_symbols)
        source = "Alpaca active/tradable common-stock pool"
    symbols = normalize_symbols(raw_symbols)
    if config.normal_stock_symbol_pattern:
        pattern = re.compile(config.normal_stock_symbol_pattern)
        symbols = [symbol for symbol in symbols if pattern.fullmatch(to_alpaca_symbol(symbol) or "")]
    print(f"Loaded symbols: source={source} count={len(symbols)} pattern={config.normal_stock_symbol_pattern}", flush=True)
    return symbols


def sync_daily_bars(
    config: DataSyncConfig,
    cache: MarketDataCache,
    symbols: list[str],
    rows: list[dict[str, object]],
    failure_rows: list[dict[str, object]],
) -> dict[str, int]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from alpaca_ma5_service.alpaca_connection import load_alpaca_credentials
    from alpaca_ma5_service.watchlist_generator import daily_bar_from_alpaca

    market_tz = ZoneInfo(config.market_timezone)
    client = None
    fetched = 0
    skipped = 0
    row_count = 0
    chunk_index = 0
    progress = ProgressTracker("Daily", planned_chunk_count(config, config.daily_chunk_days), len(symbols))
    for start_day, end_day_exclusive in date_chunks(config.start_date, config.end_date, config.daily_chunk_days):
        chunk_index += 1
        if config.max_date_chunks is not None and chunk_index > config.max_date_chunks:
            break
        missing = symbols if config.refresh_data_cache else cache.uncovered_symbols(
            "daily",
            symbols,
            start_day.isoformat(),
            end_day_exclusive.isoformat(),
            feed=daily_cache_feed(config),
            adjustment=daily_cache_adjustment(config),
        )
        skipped += len(symbols) - len(missing)
        if not missing:
            print(f"Daily cache hit: {start_day} -> {end_day_exclusive} symbols={len(symbols)}", flush=True)
            append_summary(rows, "daily", start_day, end_day_exclusive, len(symbols), 0, 0, "cache_hit")
            progress.print_chunk(chunk_index, start_day, end_day_exclusive, "cache_hit", len(symbols), 0, row_count)
            continue
        request_start = datetime.combine(start_day, time.min, tzinfo=market_tz)
        request_end = datetime.combine(end_day_exclusive, time.min, tzinfo=market_tz)
        print(f"Fetching daily bars: {start_day} -> {end_day_exclusive} symbols={len(missing)} source={config.daily_data_source}", flush=True)
        if config.daily_data_source.lower() == "moomoo":
            bars_by_symbol = fetch_moomoo_daily_bars(
                missing,
                start_day,
                end_day_exclusive,
                MoomooDailyConfig(
                    host=config.moomoo_host,
                    port=config.moomoo_port,
                    security_firm=config.moomoo_security_firm,
                    connect_timeout=config.moomoo_connect_timeout,
                    opend_exe_path=config.moomoo_opend_exe_path,
                    opend_startup_timeout=config.moomoo_opend_startup_timeout,
                    max_requests_per_window=config.moomoo_history_max_requests_per_window,
                    request_window_seconds=config.moomoo_history_request_window_seconds,
                    rate_limit_retry_seconds=config.moomoo_history_rate_limit_retry_seconds,
                    max_retries=config.moomoo_history_max_retries,
                ),
            )
            cache.save_daily_bars(
                bars_by_symbol,
                feed=daily_cache_feed(config),
                range_start=start_day,
                range_end_exclusive=end_day_exclusive,
                covered_symbols=list(bars_by_symbol),
                adjustment=daily_cache_adjustment(config),
            )
            rows_written = sum(len(bars) for bars in bars_by_symbol.values())
            row_count += rows_written
            fetched += len(missing)
            append_summary(rows, "daily", start_day, end_day_exclusive, len(missing), rows_written, len(bars_by_symbol), "fetched:moomoo")
            progress.print_chunk(chunk_index, start_day, end_day_exclusive, "fetched", len(missing), rows_written, row_count)
            continue
        if config.daily_data_source.lower() == "yahoo":
            fetch_result = fetch_yahoo_daily_bars_with_failures(
                missing,
                start_day,
                end_day_exclusive,
                YahooDailyConfig(
                    request_sleep_seconds=config.yahoo_request_sleep_seconds,
                    rate_limit_retry_seconds=config.yahoo_rate_limit_retry_seconds,
                    max_retries=config.yahoo_max_retries,
                ),
            )
            bars_by_symbol = fetch_result.bars_by_symbol
            if fetch_result.failures:
                add_failure_rows(failure_rows, "yahoo", start_day, end_day_exclusive, fetch_result.failures)
                examples = ", ".join(f"{item['symbol']} ({item['error']})" for item in fetch_result.failures[:5])
                print(f"Yahoo daily bars unavailable: count={len(fetch_result.failures)} examples={examples}", flush=True)
            cache.save_daily_bars(
                bars_by_symbol,
                feed=daily_cache_feed(config),
                range_start=start_day,
                range_end_exclusive=end_day_exclusive,
                covered_symbols=list(bars_by_symbol),
                adjustment=daily_cache_adjustment(config),
            )
            rows_written = sum(len(bars) for bars in bars_by_symbol.values())
            row_count += rows_written
            fetched += len(missing)
            append_summary(rows, "daily", start_day, end_day_exclusive, len(missing), rows_written, len(bars_by_symbol), "fetched:yahoo")
            progress.print_chunk(chunk_index, start_day, end_day_exclusive, "fetched", len(missing), rows_written, row_count)
            continue
        if config.daily_data_source.lower() == "massive":
            fetch_result = fetch_massive_grouped_daily_bars_with_retry(
                config,
                cache,
                missing,
                start_day,
                end_day_exclusive,
            )
            bars_by_symbol = fetch_result.bars_by_symbol
            unresolved_failures = fetch_result.failures
            if fetch_result.failures:
                add_failure_rows(failure_rows, "massive", start_day, end_day_exclusive, fetch_result.failures)
                rate_limited = sum(1 for item in fetch_result.failures if is_massive_rate_limit_failure(item))
                other_failed = len(fetch_result.failures) - rate_limited
                examples = ", ".join(f"{item['source_symbol']} ({item['error']})" for item in fetch_result.failures[:5])
                print(
                    f"Massive daily grouped bars incomplete: 429_dates={rate_limited} other_failed_dates={other_failed} examples={examples}",
                    flush=True,
                )
            cache.save_daily_bars(
                bars_by_symbol,
                feed=daily_cache_feed(config),
                range_start=start_day,
                range_end_exclusive=end_day_exclusive,
                covered_symbols=missing if not unresolved_failures else [],
                adjustment=daily_cache_adjustment(config),
            )
            rows_written = sum(len(bars) for bars in bars_by_symbol.values())
            row_count += rows_written
            fetched += len(missing)
            status = "fetched:massive"
            if unresolved_failures:
                status += ":partial"
            append_summary(rows, "daily", start_day, end_day_exclusive, len(missing), rows_written, len(bars_by_symbol), status)
            progress.print_chunk(chunk_index, start_day, end_day_exclusive, "fetched", len(missing), rows_written, row_count)
            continue

        bars_by_feed: dict[str, dict[str, list[DailyBar]]] = {}
        covered_by_feed: dict[str, list[str]] = {}
        for batch in batched(missing, config.batch_size):
            if client is None:
                api_key, secret_key = load_alpaca_credentials()
                client = StockHistoricalDataClient(api_key, secret_key)
            actual_feed = config.data_feed.lower()
            try:
                raw = client.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=batch,
                        timeframe=TimeFrame.Day,
                        start=request_start,
                        end=request_end,
                        limit=len(batch) * max(1, (end_day_exclusive - start_day).days + 1),
                        adjustment=Adjustment.SPLIT,
                        feed=DataFeed(config.data_feed.lower()),
                    )
                ).data
            except Exception as exc:
                if config.data_feed.lower() == "iex":
                    print(f"Daily bars failed, skipped {batch[0]}...{batch[-1]}: {short_error(exc)}", flush=True)
                    continue
                print(f"{config.data_feed.upper()} daily bars failed for {batch[0]}...{batch[-1]}, using IEX: {short_error(exc)}", flush=True)
                try:
                    actual_feed = "iex"
                    raw = client.get_stock_bars(
                        StockBarsRequest(
                            symbol_or_symbols=batch,
                            timeframe=TimeFrame.Day,
                            start=request_start,
                            end=request_end,
                            limit=len(batch) * max(1, (end_day_exclusive - start_day).days + 1),
                            adjustment=Adjustment.SPLIT,
                            feed=DataFeed("iex"),
                        )
                    ).data
                except Exception as fallback_exc:
                    print(f"IEX daily bars failed, skipped {batch[0]}...{batch[-1]}: {short_error(fallback_exc)}", flush=True)
                    continue
            covered_by_feed.setdefault(actual_feed, []).extend(batch)
            for symbol, bars in raw.items():
                parsed = [
                    daily_bar_from_alpaca(to_alpaca_symbol(symbol), bar, datetime.now(market_tz))
                    for bar in bars
                ]
                bars_by_feed.setdefault(actual_feed, {})[to_alpaca_symbol(symbol)] = [
                    bar for bar in parsed if start_day <= bar.date < end_day_exclusive
                ]
        rows_written = 0
        returned_symbols = 0
        for feed_key, bars_by_symbol in bars_by_feed.items():
            cache.save_daily_bars(
                bars_by_symbol,
                feed=feed_key,
                range_start=start_day,
                range_end_exclusive=end_day_exclusive,
                covered_symbols=covered_by_feed.get(feed_key, list(bars_by_symbol)),
                adjustment=ADJUSTMENT_SPLIT,
            )
            rows_written += sum(len(bars) for bars in bars_by_symbol.values())
            returned_symbols += len(bars_by_symbol)
        row_count += rows_written
        fetched += len(missing)
        status = "fetched:" + ",".join(sorted(bars_by_feed)) if bars_by_feed else "fetched_empty"
        append_summary(rows, "daily", start_day, end_day_exclusive, len(missing), rows_written, returned_symbols, status)
        progress.print_chunk(chunk_index, start_day, end_day_exclusive, "fetched", len(missing), rows_written, row_count)
    return {"rows": row_count, "fetched": fetched, "skipped": skipped}


def sync_minute_bars(
    config: DataSyncConfig,
    cache: MarketDataCache,
    symbols: list[str],
    rows: list[dict[str, object]],
) -> dict[str, int]:
    market_tz = ZoneInfo(config.market_timezone)
    fetched = 0
    skipped = 0
    row_count = 0
    chunk_index = 0
    progress = ProgressTracker("1Min", planned_chunk_count(config, config.minute_chunk_days), len(symbols))
    for start_day, end_day_exclusive in date_chunks(config.start_date, config.end_date, config.minute_chunk_days):
        chunk_index += 1
        if config.max_date_chunks is not None and chunk_index > config.max_date_chunks:
            break
        start = datetime.combine(start_day, time.min, tzinfo=market_tz)
        end = datetime.combine(end_day_exclusive, time.min, tzinfo=market_tz)
        missing = symbols if config.refresh_data_cache else cache.uncovered_symbols(
            "minute",
            symbols,
            utc_key(start),
            utc_key(end),
            feed=config.data_feed,
            adjustment=ADJUSTMENT_SPLIT,
        )
        skipped += len(symbols) - len(missing)
        if not missing:
            print(f"1Min cache hit: {start_day} -> {end_day_exclusive} symbols={len(symbols)}", flush=True)
            append_summary(rows, "minute", start_day, end_day_exclusive, len(symbols), 0, 0, "cache_hit")
            progress.print_chunk(chunk_index, start_day, end_day_exclusive, "cache_hit", len(symbols), 0, row_count)
            continue
        print(f"Fetching 1Min bars: {start_day} -> {end_day_exclusive} symbols={len(missing)} feed={config.data_feed}", flush=True)
        bars_by_symbol = fetch_minute_bars(missing, start, end, feed=config.data_feed, batch_size=config.batch_size)
        cache.save_minute_bars(
            bars_by_symbol,
            feed=config.data_feed,
            range_start=start,
            range_end=end,
            covered_symbols=missing,
            adjustment=ADJUSTMENT_SPLIT,
        )
        rows_written = sum(len(bars) for bars in bars_by_symbol.values())
        row_count += rows_written
        fetched += len(missing)
        append_summary(rows, "minute", start_day, end_day_exclusive, len(missing), rows_written, len(bars_by_symbol), "fetched")
        progress.print_chunk(chunk_index, start_day, end_day_exclusive, "fetched", len(missing), rows_written, row_count)
    return {"rows": row_count, "fetched": fetched, "skipped": skipped}


def fetch_massive_grouped_daily_bars_with_retry(
    config: DataSyncConfig,
    cache: MarketDataCache,
    symbols: list[str],
    start_day: date,
    end_day_exclusive: date,
) -> DailyFetchResult:
    combined_bars: dict[str, list[DailyBar]] = {}
    retry_round = 0
    retry_sleep = max(1.0, config.massive_failed_date_retry_sleep_seconds)
    ranges = [(start_day, end_day_exclusive)]
    pending_dates: set[date] | None = None
    last_failures: list[dict[str, str]] = []

    while True:
        round_bars: dict[str, list[DailyBar]] = {}
        round_failures: list[dict[str, str]] = []
        for range_start, range_end in ranges:
            fetch_result = fetch_massive_grouped_daily_bars_with_failures(
                symbols,
                range_start,
                range_end,
                MassiveDailyConfig(
                    api_keys=config.massive_api_keys,
                    max_workers=config.massive_max_workers,
                    request_timeout_seconds=config.massive_request_timeout_seconds,
                    retry_sleep_seconds=config.massive_retry_sleep_seconds,
                    max_retries=config.massive_max_retries,
                    progress_interval_seconds=config.massive_progress_interval_seconds,
                    progress_interval_dates=config.massive_progress_interval_dates,
                ),
            )
            bars = fetch_result.bars_by_symbol
            if pending_dates is not None:
                bars = filter_daily_bars_to_dates(bars, pending_dates)
            round_bars = merge_daily_bars(round_bars, bars)
            round_failures.extend(fetch_result.failures)

        if round_bars:
            combined_bars = merge_daily_bars(combined_bars, round_bars)
            cache.save_daily_bars(
                round_bars,
                feed=daily_cache_feed(config),
                range_start=start_day,
                range_end_exclusive=end_day_exclusive,
                covered_symbols=[],
                adjustment=daily_cache_adjustment(config),
            )
            print(
                f"Massive incremental save: rows={sum(len(bars) for bars in round_bars.values()):,} "
                f"symbols={len(round_bars)} total_rows={sum(len(bars) for bars in combined_bars.values()):,}",
                flush=True,
            )
        if not round_failures:
            if retry_round:
                print(f"Massive retry completed: retry_rounds={retry_round}", flush=True)
            return DailyFetchResult(combined_bars, [])

        last_failures = round_failures
        failed_dates = failure_dates(round_failures)
        rate_limited = sum(1 for item in round_failures if is_massive_rate_limit_failure(item))
        other_failed = len(round_failures) - rate_limited
        if not config.massive_retry_failed_dates_until_complete:
            return DailyFetchResult(combined_bars, last_failures)
        if config.massive_failed_date_max_retry_rounds is not None and retry_round >= config.massive_failed_date_max_retry_rounds:
            print(
                f"Massive retry stopped after max rounds: rounds={retry_round} failed_dates={len(failed_dates)} 429={rate_limited} failed={other_failed}",
                flush=True,
            )
            return DailyFetchResult(combined_bars, last_failures)
        if not failed_dates:
            print("Massive retry cannot identify failed dates; keeping partial result.", flush=True)
            return DailyFetchResult(combined_bars, last_failures)

        retry_round += 1
        examples = ", ".join(day.isoformat() for day in failed_dates[:8])
        print(
            f"Massive retry waiting: round={retry_round} failed_dates={len(failed_dates)} "
            f"429={rate_limited} failed={other_failed} wait={retry_sleep:.0f}s examples={examples}",
            flush=True,
        )
        sleep(retry_sleep)
        pending_dates = set(failed_dates)
        ranges = coalesced_date_ranges(failed_dates)
        retry_sleep = min(
            max(retry_sleep, 1.0) * max(1.0, config.massive_failed_date_retry_sleep_multiplier),
            max(1.0, config.massive_failed_date_retry_max_sleep_seconds),
        )


def fetch_yahoo_fallback_for_failed_massive_dates(
    config: DataSyncConfig,
    symbols: list[str],
    massive_failures: list[dict[str, str]],
) -> DailyFetchResult:
    dates = failure_dates(massive_failures)
    if not dates:
        return DailyFetchResult({}, [])
    date_set = set(dates)
    combined_bars: dict[str, list[DailyBar]] = {}
    combined_failures: list[dict[str, str]] = []
    ranges = coalesced_date_ranges(dates)
    print(f"Yahoo fallback for Massive failed dates: dates={len(dates)} ranges={len(ranges)} symbols={len(symbols)}", flush=True)
    for range_start, range_end_exclusive in ranges:
        fetch_result = fetch_yahoo_daily_bars_with_failures(
            symbols,
            range_start,
            range_end_exclusive,
            YahooDailyConfig(
                request_sleep_seconds=config.yahoo_request_sleep_seconds,
                rate_limit_retry_seconds=config.yahoo_rate_limit_retry_seconds,
                max_retries=config.yahoo_max_retries,
            ),
        )
        filtered = filter_daily_bars_to_dates(fetch_result.bars_by_symbol, date_set)
        combined_bars = merge_daily_bars(combined_bars, filtered)
        combined_failures.extend(fetch_result.failures)
        rows_written = sum(len(bars) for bars in filtered.values())
        print(
            f"Yahoo fallback range done: {range_start}->{range_end_exclusive} rows={rows_written:,} "
            f"symbols={len(filtered)} failures={len(fetch_result.failures)}",
            flush=True,
        )
    total_rows = sum(len(bars) for bars in combined_bars.values())
    print(
        f"Yahoo fallback finished: dates={len(dates)} rows={total_rows:,} "
        f"symbols={len(combined_bars)} failures={len(combined_failures)}",
        flush=True,
    )
    return DailyFetchResult(combined_bars, combined_failures)


def daily_cache_feed(config: DataSyncConfig) -> str:
    source = config.daily_data_source.lower()
    if source == "moomoo":
        return MOOMOO_DAILY_FEED
    if source == "yahoo":
        return YAHOO_DAILY_FEED
    if source == "massive":
        return MASSIVE_DAILY_FEED
    return config.data_feed.lower()


def daily_cache_adjustment(config: DataSyncConfig) -> str:
    source = config.daily_data_source.lower()
    if source == "moomoo":
        return MOOMOO_DAILY_ADJUSTMENT
    if source == "yahoo":
        return YAHOO_DAILY_ADJUSTMENT
    if source == "massive":
        return MASSIVE_DAILY_ADJUSTMENT
    return ADJUSTMENT_SPLIT


class ProgressTracker:
    def __init__(self, label: str, total_chunks: int, symbol_count: int):
        self.label = label
        self.total_chunks = max(1, total_chunks)
        self.symbol_count = symbol_count
        self.started_at = monotonic()

    def print_chunk(
        self,
        chunk_index: int,
        start_day: date,
        end_day_exclusive: date,
        status: str,
        requested_symbols: int,
        rows_written: int,
        cumulative_rows: int,
    ) -> None:
        elapsed = monotonic() - self.started_at
        percent = min(100.0, chunk_index / self.total_chunks * 100.0)
        print(
            f"[{self.label} {chunk_index}/{self.total_chunks} {percent:5.1f}%] "
            f"{start_day}->{end_day_exclusive} status={status} "
            f"symbols={requested_symbols}/{self.symbol_count} rows={rows_written:,} "
            f"total_rows={cumulative_rows:,} elapsed={format_elapsed(elapsed)}",
            flush=True,
        )


def planned_chunk_count(config: DataSyncConfig, chunk_days: int) -> int:
    total_days = (config.end_date - config.start_date).days + 1
    chunks = (total_days + max(1, chunk_days) - 1) // max(1, chunk_days)
    if config.max_date_chunks is not None:
        chunks = min(chunks, config.max_date_chunks)
    return max(0, chunks)


def date_chunks(start_date: date, end_date: date, chunk_days: int):
    chunk_start = start_date
    final_exclusive = end_date + timedelta(days=1)
    while chunk_start < final_exclusive:
        chunk_end = min(chunk_start + timedelta(days=max(1, chunk_days)), final_exclusive)
        yield chunk_start, chunk_end
        chunk_start = chunk_end


def append_summary(
    rows: list[dict[str, object]],
    kind: str,
    start_day: date,
    end_day_exclusive: date,
    requested_symbols: int,
    row_count: int,
    returned_symbols: int,
    status: str,
) -> None:
    rows.append(
        {
            "kind": kind,
            "start_date": start_day.isoformat(),
            "end_date_exclusive": end_day_exclusive.isoformat(),
            "requested_symbols": requested_symbols,
            "returned_symbols": returned_symbols,
            "rows": row_count,
            "status": status,
        }
    )


def write_sync_summary(config: DataSyncConfig, rows: list[dict[str, object]]) -> Path:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    path = config.output_dir / config.summary_csv_name
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["kind", "start_date", "end_date_exclusive", "requested_symbols", "returned_symbols", "rows", "status"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def add_failure_rows(
    rows: list[dict[str, object]],
    source: str,
    start_day: date,
    end_day_exclusive: date,
    failures: list[dict[str, str]],
) -> None:
    for failure in failures:
        rows.append(
            {
                "source": source,
                "symbol": failure.get("symbol", ""),
                "source_symbol": failure.get("source_symbol", ""),
                "start_date": start_day.isoformat(),
                "end_date_exclusive": end_day_exclusive.isoformat(),
                "error": failure.get("error", ""),
            }
        )


def write_sync_failures(config: DataSyncConfig, rows: list[dict[str, object]]) -> Path:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    path = config.output_dir / "data_sync_failures.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["source", "symbol", "source_symbol", "start_date", "end_date_exclusive", "error"],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Data sync failures CSV: {path}", flush=True)
    return path


def batched(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]
