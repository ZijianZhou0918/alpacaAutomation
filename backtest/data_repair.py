from __future__ import annotations

import csv
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from alpaca_ma5_service.trading_calendar import offline_trading_day_decision
from alpaca_ma5_service.watchlist_generator import DailyBar
from backtest.data_cache import MarketDataCache, average_tail, normalize_symbols
from backtest.daily_sources import (
    DailyFetchResult,
    MassiveDailyConfig,
    coalesced_date_ranges,
    fetch_massive_grouped_daily_bars_with_failures,
    filter_daily_bars_to_dates,
    is_massive_rate_limit_failure,
)


@dataclass(frozen=True)
class DataRepairConfig:
    symbols: list[str]
    start_date: date
    end_date: date
    ma_warmup_calendar_days: int
    feed: str
    adjustment: str
    data_cache_dir: Path
    data_cache_name: str
    output_dir: Path
    create_backup: bool
    delete_invalid_ohlc_rows: bool
    delete_untrusted_fetch_ranges: bool
    recompute_daily_mas: bool
    backfill_low_coverage_dates: bool
    min_range_date_coverage_ratio: float
    min_daily_symbol_coverage_ratio: float
    max_backfill_dates: int | None
    massive_api_keys: tuple[str, ...]
    massive_max_workers: int
    massive_request_timeout_seconds: float
    massive_retry_sleep_seconds: float
    massive_max_retries: int
    massive_progress_interval_seconds: float
    massive_progress_interval_dates: int


@dataclass(frozen=True)
class DataRepairResult:
    cache_path: Path
    backup_path: Path | None
    audit_csv_path: Path
    invalid_rows_deleted: int
    fetch_ranges_deleted: int
    ma_symbols_recomputed: int
    low_coverage_dates: int
    backfill_rows_written: int
    backfill_failures: int
    target_null_ma_rows: int


def run_data_repair(config: DataRepairConfig) -> DataRepairResult:
    symbols = normalize_symbols(config.symbols)
    if not symbols:
        raise ValueError("No symbols configured for data repair.")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = config.data_cache_dir / config.data_cache_name
    backup_path = backup_database(cache_path, config.output_dir) if config.create_backup and cache_path.exists() else None
    cache = MarketDataCache(cache_path)
    data_start_date = repair_data_start_date(config)

    invalid_rows_deleted = delete_invalid_daily_rows(cache.path, config, data_start_date) if config.delete_invalid_ohlc_rows else 0
    fetch_ranges_deleted = delete_untrusted_daily_fetch_ranges(cache.path, config, data_start_date) if config.delete_untrusted_fetch_ranges else 0
    ma_symbols_recomputed = recompute_daily_mas(cache.path, config) if config.recompute_daily_mas else 0

    repair_audit_rows = audit_daily_coverage(cache.path, config, symbols, data_start_date, config.end_date)
    low_dates_for_repair = [
        row["date"]
        for row in repair_audit_rows
        if row["status"] in {"MISSING", "LOW_COVERAGE"}
    ]

    backfill_rows_written = 0
    backfill_failures = 0
    if config.backfill_low_coverage_dates and low_dates_for_repair:
        selected = low_dates_for_repair[: config.max_backfill_dates] if config.max_backfill_dates is not None else low_dates_for_repair
        rows_written, failures = backfill_dates(cache, config, symbols, [date.fromisoformat(item) for item in selected])
        backfill_rows_written += rows_written
        backfill_failures += failures
        if rows_written:
            ma_symbols_recomputed += recompute_daily_mas(cache.path, config)

    audit_rows = audit_daily_coverage(cache.path, config, symbols, config.start_date, config.end_date)
    audit_csv_path = write_daily_audit_csv(config.output_dir, audit_rows)
    low_dates = [
        row["date"]
        for row in audit_rows
        if row["status"] in {"MISSING", "LOW_COVERAGE"}
    ]
    target_null_ma_rows = count_target_null_mas(cache.path, config)

    return DataRepairResult(
        cache_path=cache.path,
        backup_path=backup_path,
        audit_csv_path=audit_csv_path,
        invalid_rows_deleted=invalid_rows_deleted,
        fetch_ranges_deleted=fetch_ranges_deleted,
        ma_symbols_recomputed=ma_symbols_recomputed,
        low_coverage_dates=len(low_dates),
        backfill_rows_written=backfill_rows_written,
        backfill_failures=backfill_failures,
        target_null_ma_rows=target_null_ma_rows,
    )


def backup_database(cache_path: Path, output_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = output_dir / f"{cache_path.stem}_backup_{timestamp}{cache_path.suffix}"
    shutil.copy2(cache_path, backup_path)
    print(f"DB backup created: {backup_path}", flush=True)
    return backup_path


def repair_data_start_date(config: DataRepairConfig) -> date:
    return config.start_date - timedelta(days=max(0, config.ma_warmup_calendar_days))


@contextmanager
def connect_database(cache_path: Path):
    conn = sqlite3.connect(cache_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def delete_invalid_daily_rows(cache_path: Path, config: DataRepairConfig, start_date: date) -> int:
    with connect_database(cache_path) as conn:
        before = conn.total_changes
        conn.execute(
            """
            DELETE FROM daily_bars
            WHERE feed = ?
              AND adjustment = ?
              AND bar_date >= ?
              AND bar_date <= ?
              AND (
                open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
                OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                OR high < low
                OR high < open OR high < close
                OR low > open OR low > close
              )
            """,
            (config.feed.lower(), config.adjustment, start_date.isoformat(), config.end_date.isoformat()),
        )
        deleted = conn.total_changes - before
    print(f"Invalid OHLC rows deleted: {deleted}", flush=True)
    return deleted


def delete_untrusted_daily_fetch_ranges(cache_path: Path, config: DataRepairConfig, start_date: date) -> int:
    groups = fetch_range_groups(cache_path, config, start_date)
    deleted = 0
    with connect_database(cache_path) as conn:
        for range_start, range_end in groups:
            expected = expected_trading_days(date.fromisoformat(range_start), date.fromisoformat(range_end))
            if expected <= 0:
                continue
            actual = conn.execute(
                """
                SELECT COUNT(DISTINCT bar_date)
                FROM daily_bars
                WHERE feed = ?
                  AND adjustment = ?
                  AND bar_date >= ?
                  AND bar_date < ?
                """,
                (config.feed.lower(), config.adjustment, range_start, range_end),
            ).fetchone()[0]
            ratio = actual / expected
            if ratio >= config.min_range_date_coverage_ratio:
                continue
            before = conn.total_changes
            conn.execute(
                """
                DELETE FROM fetch_ranges
                WHERE kind = 'daily'
                  AND feed = ?
                  AND adjustment = ?
                  AND start_key = ?
                  AND end_key = ?
                """,
                (config.feed.lower(), config.adjustment, range_start, range_end),
            )
            count = conn.total_changes - before
            deleted += count
            print(
                f"Deleted untrusted fetch range: {range_start}->{range_end} "
                f"date_coverage={actual}/{expected} ratio={ratio:.1%} rows={count}",
                flush=True,
            )
    print(f"Untrusted fetch ranges deleted: {deleted}", flush=True)
    return deleted


def fetch_range_groups(cache_path: Path, config: DataRepairConfig, start_date: date) -> list[tuple[str, str]]:
    with connect_database(cache_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT start_key, end_key
            FROM fetch_ranges
            WHERE kind = 'daily'
              AND feed = ?
              AND adjustment = ?
              AND start_key < ?
              AND end_key > ?
            ORDER BY start_key, end_key
            """,
            (
                config.feed.lower(),
                config.adjustment,
                (config.end_date + timedelta(days=1)).isoformat(),
                start_date.isoformat(),
            ),
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def recompute_daily_mas(cache_path: Path, config: DataRepairConfig) -> int:
    with connect_database(cache_path) as conn:
        symbols = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT symbol
                FROM daily_bars
                WHERE feed = ?
                  AND adjustment = ?
                ORDER BY symbol
                """,
                (config.feed.lower(), config.adjustment),
            ).fetchall()
        ]
        for symbol in symbols:
            rows = conn.execute(
                """
                SELECT bar_date, close
                FROM daily_bars
                WHERE symbol = ? AND feed = ? AND adjustment = ?
                ORDER BY bar_date
                """,
                (symbol, config.feed.lower(), config.adjustment),
            ).fetchall()
            closes: list[float] = []
            updates: list[tuple[float | None, float | None, float | None, str, str, str, str]] = []
            for bar_date, close in rows:
                closes.append(close)
                updates.append(
                    (
                        average_tail(closes, 5),
                        average_tail(closes, 10),
                        average_tail(closes, 20),
                        symbol,
                        bar_date,
                        config.feed.lower(),
                        config.adjustment,
                    )
                )
            conn.executemany(
                """
                UPDATE daily_bars
                SET ma5 = ?, ma10 = ?, ma20 = ?
                WHERE symbol = ? AND bar_date = ? AND feed = ? AND adjustment = ?
                """,
                updates,
            )
    print(f"Daily MA recomputed for symbols: {len(symbols)}", flush=True)
    return len(symbols)


def audit_daily_coverage(
    cache_path: Path,
    config: DataRepairConfig,
    symbols: list[str],
    start_date: date,
    end_date: date,
) -> list[dict[str, object]]:
    wanted = set(symbols)
    rows: list[dict[str, object]] = []
    with connect_database(cache_path) as conn:
        day = start_date
        while day <= end_date:
            if not is_repair_trading_day(day):
                day += timedelta(days=1)
                continue
            present_symbols: set[str] = set()
            for batch in batched(symbols, 800):
                present_symbols.update(
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT DISTINCT symbol
                        FROM daily_bars
                        WHERE feed = ?
                          AND adjustment = ?
                          AND bar_date = ?
                          AND symbol IN ({})
                        """.format(",".join("?" for _ in batch)),
                        [config.feed.lower(), config.adjustment, day.isoformat(), *batch],
                    ).fetchall()
                )
            present_rows = len(present_symbols)
            coverage = present_rows / max(1, len(wanted))
            if present_rows == 0:
                status = "MISSING"
            elif coverage < config.min_daily_symbol_coverage_ratio:
                status = "LOW_COVERAGE"
            else:
                status = "OK"
            rows.append(
                {
                    "date": day.isoformat(),
                    "symbols_expected": len(wanted),
                    "symbols_present": present_rows,
                    "missing_symbols_estimate": max(0, len(wanted) - present_rows),
                    "coverage_pct": f"{coverage:.2%}",
                    "status": status,
                }
            )
            day += timedelta(days=1)
    low = sum(1 for row in rows if row["status"] != "OK")
    print(f"Daily coverage audit: checked={len(rows)} low_or_missing={low}", flush=True)
    return rows


def write_daily_audit_csv(output_dir: Path, rows: list[dict[str, object]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "daily_cache_integrity_report.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "symbols_expected",
                "symbols_present",
                "missing_symbols_estimate",
                "coverage_pct",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Daily integrity CSV: {path}", flush=True)
    return path


def backfill_dates(
    cache: MarketDataCache,
    config: DataRepairConfig,
    symbols: list[str],
    dates: list[date],
) -> tuple[int, int]:
    if not dates:
        return 0, 0
    rows_written = 0
    failure_count = 0
    ranges = coalesced_date_ranges(dates)
    date_set = set(dates)
    print(f"Backfill low-coverage dates: dates={len(dates)} ranges={len(ranges)}", flush=True)
    for range_start, range_end_exclusive in ranges:
        fetch_result = fetch_massive_grouped_daily_bars_with_failures(
            symbols,
            range_start,
            range_end_exclusive,
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
        bars_by_symbol = filter_daily_bars_to_dates(fetch_result.bars_by_symbol, date_set)
        unresolved_failures = fetch_result.failures
        if fetch_result.failures:
            rate_limited = sum(1 for item in fetch_result.failures if is_massive_rate_limit_failure(item))
            print(
                f"Massive repair backfill incomplete: {range_start}->{range_end_exclusive} "
                f"429_dates={rate_limited} failed_dates={len(fetch_result.failures) - rate_limited}",
                flush=True,
            )
            print("Repair backfill is Massive-only; failed dates remain incomplete for the next repair run.", flush=True)
        cache.save_daily_bars(
            bars_by_symbol,
            feed=config.feed,
            adjustment=config.adjustment,
            range_start=range_start,
            range_end_exclusive=range_end_exclusive,
            covered_symbols=symbols if not unresolved_failures else [],
        )
        range_rows = sum(len(bars) for bars in bars_by_symbol.values())
        rows_written += range_rows
        failure_count += len(unresolved_failures)
        print(
            f"Backfill range saved: {range_start}->{range_end_exclusive} rows={range_rows:,} "
            f"unresolved_failures={len(unresolved_failures)}",
            flush=True,
        )
    return rows_written, failure_count


def count_target_null_mas(cache_path: Path, config: DataRepairConfig) -> int:
    with connect_database(cache_path) as conn:
        return conn.execute(
            """
            SELECT COUNT(*)
            FROM daily_bars
            WHERE feed = ?
              AND adjustment = ?
              AND bar_date >= ?
              AND bar_date <= ?
              AND (ma5 IS NULL OR ma10 IS NULL OR ma20 IS NULL)
            """,
            (config.feed.lower(), config.adjustment, config.start_date.isoformat(), config.end_date.isoformat()),
        ).fetchone()[0]


def is_repair_trading_day(day: date) -> bool:
    return offline_trading_day_decision(day).is_trading_day


def expected_trading_days(start_date: date, end_date_exclusive: date) -> int:
    count = 0
    day = start_date
    while day < end_date_exclusive:
        if is_repair_trading_day(day):
            count += 1
        day += timedelta(days=1)
    return count


def batched(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]
