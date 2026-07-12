from __future__ import annotations

import csv
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

from alpaca_ma5_service.watchlist_generator import DailyBar
from alpaca_ma5_service.trading_calendar import offline_trading_day_decision
from alpaca_ma5_service.watchlist import to_alpaca_symbol
from backtest.data_cache import average_tail, normalize_symbols
from backtest.daily_sources import (
    DailyFetchResult,
    MassiveDailyConfig,
    failure_dates,
    fetch_one_massive_grouped_day,
    format_elapsed,
    normalize_massive_ticker,
)


@dataclass(frozen=True)
class DataSpotcheckConfig:
    symbols: list[str]
    start_date: date
    end_date: date
    sample_size: int
    sample_seed: int | None
    feed: str
    adjustment: str
    data_cache_dir: Path
    data_cache_name: str
    output_dir: Path
    issue_csv_name: str
    summary_csv_name: str
    sampled_symbols_csv_name: str
    massive_api_keys: tuple[str, ...]
    massive_max_workers: int
    massive_request_timeout_seconds: float
    massive_retry_sleep_seconds: float
    massive_max_retries: int
    massive_request_spacing_seconds: float
    massive_progress_interval_seconds: float
    massive_progress_interval_dates: int
    price_tolerance: float = 0.0001
    volume_tolerance: float = 0.5


@dataclass(frozen=True)
class DataSpotcheckResult:
    cache_path: Path
    sampled_symbols_path: Path
    issue_csv_path: Path
    summary_csv_path: Path
    sample_seed: int
    sampled_symbols: list[str]
    local_rows_checked: int
    remote_rows_checked: int
    issue_count: int
    symbols_with_issues: int
    fetch_failures: int


@dataclass(frozen=True)
class LocalDailyRow:
    symbol: str
    bar_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    vwap: float | None
    transactions: int | None
    timestamp_ms: int | None
    ma5: float | None
    ma10: float | None
    ma20: float | None


def run_data_spotcheck(config: DataSpotcheckConfig) -> DataSpotcheckResult:
    symbols = normalize_symbols(config.symbols)
    if not symbols:
        raise ValueError("No symbols configured for data spotcheck.")

    seed = config.sample_seed
    if seed is None:
        seed = int(datetime.now().strftime("%Y%m%d%H%M%S"))
    sampled = sample_symbols(symbols, config.sample_size, seed)
    if not sampled:
        raise ValueError("No symbols selected for data spotcheck.")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = config.data_cache_dir / config.data_cache_name
    trading_dates = repair_trading_dates(config.start_date, config.end_date)
    local_rows = load_local_rows(cache_path, config, sampled)
    remote_result = fetch_spotcheck_massive_daily_bars(sampled, trading_dates, config)
    remote_rows = remote_result.bars_by_symbol

    issues: list[dict[str, object]] = []
    issues.extend(find_local_null_and_invalid_issues(local_rows))
    issues.extend(find_ma_issues(cache_path, config, sampled))
    issues.extend(compare_to_remote(local_rows, remote_rows, config, set(failure_dates(remote_result.failures))))

    summary_rows = build_summary_rows(sampled, local_rows, remote_rows, issues)
    sampled_symbols_path = write_sampled_symbols(config, sampled, seed)
    issue_csv_path = write_issue_csv(config, issues)
    summary_csv_path = write_summary_csv(config, summary_rows)

    symbols_with_issues = len({str(issue["symbol"]) for issue in issues})
    local_count = sum(len(rows) for rows in local_rows.values())
    remote_count = sum(len(rows) for rows in remote_rows.values())
    print(
        f"Data spotcheck finished: sampled={len(sampled)} seed={seed} "
        f"local_rows={local_count:,} remote_rows={remote_count:,} issues={len(issues):,} "
        f"fetch_failures={len(remote_result.failures)}",
        flush=True,
    )
    print(f"Sampled symbols CSV: {sampled_symbols_path}", flush=True)
    print(f"Issues CSV: {issue_csv_path}", flush=True)
    print(f"Summary CSV: {summary_csv_path}", flush=True)

    return DataSpotcheckResult(
        cache_path=cache_path,
        sampled_symbols_path=sampled_symbols_path,
        issue_csv_path=issue_csv_path,
        summary_csv_path=summary_csv_path,
        sample_seed=seed,
        sampled_symbols=sampled,
        local_rows_checked=local_count,
        remote_rows_checked=remote_count,
        issue_count=len(issues),
        symbols_with_issues=symbols_with_issues,
        fetch_failures=len(remote_result.failures),
    )


def sample_symbols(symbols: list[str], sample_size: int, seed: int) -> list[str]:
    normalized = normalize_symbols(symbols)
    if sample_size <= 0 or sample_size >= len(normalized):
        return normalized
    return sorted(random.Random(seed).sample(normalized, sample_size))


def fetch_spotcheck_massive_daily_bars(
    symbols: list[str],
    trading_dates: list[date],
    config: DataSpotcheckConfig,
) -> DailyFetchResult:
    if not config.massive_api_keys:
        raise RuntimeError("Missing Massive API keys. Set MASSIVE_API_KEYS in environment or .env.")

    wanted = {normalize_massive_ticker(symbol): to_alpaca_symbol(symbol) for symbol in symbols}
    wanted = {source: target for source, target in wanted.items() if source and target}
    out: dict[str, list[DailyBar]] = {symbol: [] for symbol in wanted.values()}
    failures: list[dict[str, str]] = []
    daily_config = MassiveDailyConfig(
        api_keys=config.massive_api_keys,
        max_workers=1,
        request_timeout_seconds=config.massive_request_timeout_seconds,
        retry_sleep_seconds=config.massive_retry_sleep_seconds,
        max_retries=config.massive_max_retries,
        progress_enabled=False,
    )
    started_at = monotonic()
    last_progress_at = started_at
    success_dates = 0
    failed_dates = 0
    matched_rows = 0
    raw_rows = 0

    print(
        f"Massive spotcheck daily start: dates={len(trading_dates)} symbols={len(wanted)} "
        f"keys={len(config.massive_api_keys)} spacing={config.massive_request_spacing_seconds}s",
        flush=True,
    )
    for index, day in enumerate(trading_dates):
        api_key = config.massive_api_keys[index % len(config.massive_api_keys)]
        rows, failure = fetch_one_massive_grouped_day(day, api_key, daily_config)
        if failure is not None:
            failures.append(failure)
            failed_dates += 1
        else:
            success_dates += 1
            raw_rows += len(rows)
            matched_for_day = 0
            for row in rows:
                symbol = wanted.get(row.ticker)
                if symbol is None:
                    continue
                out.setdefault(symbol, []).append(
                    DailyBar(
                        symbol,
                        day,
                        row.open,
                        row.high,
                        row.low,
                        row.close,
                        row.volume,
                        row.vwap,
                        row.transactions,
                        row.timestamp_ms,
                    )
                )
                matched_for_day += 1
            matched_rows += matched_for_day

        completed_dates = index + 1
        last_progress_at = maybe_print_spotcheck_progress(
            config,
            completed_dates,
            len(trading_dates),
            success_dates,
            failed_dates,
            matched_rows,
            raw_rows,
            started_at,
            last_progress_at,
            force=completed_dates == len(trading_dates),
        )
        if index + 1 < len(trading_dates) and config.massive_request_spacing_seconds > 0:
            sleep(config.massive_request_spacing_seconds)

    cleaned = {
        symbol: sorted(bars, key=lambda bar: bar.date)
        for symbol, bars in out.items()
        if bars
    }
    return DailyFetchResult(cleaned, failures)


def maybe_print_spotcheck_progress(
    config: DataSpotcheckConfig,
    completed_dates: int,
    total_dates: int,
    success_dates: int,
    failed_dates: int,
    matched_rows: int,
    raw_rows: int,
    started_at: float,
    last_progress_at: float,
    *,
    force: bool = False,
) -> float:
    now = monotonic()
    by_dates = config.massive_progress_interval_dates > 0 and completed_dates % config.massive_progress_interval_dates == 0
    by_time = now - last_progress_at >= max(1.0, config.massive_progress_interval_seconds)
    if not force and not by_dates and not by_time:
        return last_progress_at
    percent = completed_dates / max(1, total_dates) * 100.0
    print(
        f"Massive spotcheck daily progress: dates={completed_dates}/{total_dates} {percent:5.1f}% "
        f"success={success_dates} failed={failed_dates} matched_rows={matched_rows:,} "
        f"raw_rows={raw_rows:,} elapsed={format_elapsed(monotonic() - started_at)}",
        flush=True,
    )
    return now


def repair_trading_dates(start_date: date, end_date: date) -> list[date]:
    out: list[date] = []
    day = start_date
    while day <= end_date:
        if offline_trading_day_decision(day).is_trading_day:
            out.append(day)
        day += timedelta(days=1)
    return out


def load_local_rows(
    cache_path: Path,
    config: DataSpotcheckConfig,
    symbols: list[str],
) -> dict[str, dict[date, LocalDailyRow]]:
    rows_by_symbol: dict[str, dict[date, LocalDailyRow]] = {symbol: {} for symbol in symbols}
    if not cache_path.exists():
        return rows_by_symbol

    with connect_database(cache_path) as conn:
        for batch in batched(symbols, 800):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT symbol, bar_date, open, high, low, close, volume, vwap, transactions, timestamp_ms, ma5, ma10, ma20
                FROM daily_bars
                WHERE feed = ?
                  AND adjustment = ?
                  AND bar_date >= ?
                  AND bar_date <= ?
                  AND symbol IN ({placeholders})
                ORDER BY symbol, bar_date
                """,
                [config.feed.lower(), config.adjustment, config.start_date.isoformat(), config.end_date.isoformat(), *batch],
            ).fetchall()
            for row in rows:
                item = LocalDailyRow(
                    symbol=row[0],
                    bar_date=date.fromisoformat(row[1]),
                    open=row[2],
                    high=row[3],
                    low=row[4],
                    close=row[5],
                    volume=row[6],
                    vwap=row[7],
                    transactions=row[8],
                    timestamp_ms=row[9],
                    ma5=row[10],
                    ma10=row[11],
                    ma20=row[12],
                )
                rows_by_symbol.setdefault(item.symbol, {})[item.bar_date] = item
    return rows_by_symbol


def find_local_null_and_invalid_issues(local_rows: dict[str, dict[date, LocalDailyRow]]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for symbol, rows in local_rows.items():
        for day, row in rows.items():
            for field in ("open", "high", "low", "close", "volume", "vwap", "transactions", "timestamp_ms", "ma5", "ma10", "ma20"):
                if getattr(row, field) is None:
                    issues.append(issue(symbol, day, "NULL_VALUE", field, None, None, "local field is NULL"))
            if row.open is None or row.high is None or row.low is None or row.close is None:
                continue
            if row.open <= 0 or row.high <= 0 or row.low <= 0 or row.close <= 0:
                issues.append(issue(symbol, day, "INVALID_OHLC", "ohlc", None, None, "OHLC must be positive"))
            if row.high < row.low or row.high < row.open or row.high < row.close or row.low > row.open or row.low > row.close:
                issues.append(issue(symbol, day, "INVALID_OHLC", "ohlc", None, None, "OHLC high/low relationship is invalid"))
    return issues


def find_ma_issues(cache_path: Path, config: DataSpotcheckConfig, symbols: list[str]) -> list[dict[str, object]]:
    if not cache_path.exists():
        return []
    wanted = set(symbols)
    issues: list[dict[str, object]] = []
    with connect_database(cache_path) as conn:
        for symbol in symbols:
            rows = conn.execute(
                """
                SELECT bar_date, close, ma5, ma10, ma20
                FROM daily_bars
                WHERE symbol = ? AND feed = ? AND adjustment = ? AND bar_date <= ?
                ORDER BY bar_date
                """,
                (symbol, config.feed.lower(), config.adjustment, config.end_date.isoformat()),
            ).fetchall()
            closes: list[float] = []
            for bar_date_text, close, ma5, ma10, ma20 in rows:
                day = date.fromisoformat(bar_date_text)
                if close is not None:
                    closes.append(close)
                if day < config.start_date or symbol not in wanted:
                    continue
                for field, expected, actual in (
                    ("ma5", average_tail(closes, 5), ma5),
                    ("ma10", average_tail(closes, 10), ma10),
                    ("ma20", average_tail(closes, 20), ma20),
                ):
                    if not nullable_close(expected, actual, config.price_tolerance):
                        issues.append(issue(symbol, day, "MA_MISMATCH", field, actual, expected, "local MA does not match recomputed close average"))
    return issues


def compare_to_remote(
    local_rows: dict[str, dict[date, LocalDailyRow]],
    remote_rows,
    config: DataSpotcheckConfig,
    failed_dates: set[date],
) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    remote_by_symbol = {
        to_alpaca_symbol(symbol): {bar.date: bar for bar in bars}
        for symbol, bars in remote_rows.items()
    }
    for symbol, local_by_date in local_rows.items():
        remote_by_date = remote_by_symbol.get(symbol, {})
        for day, remote in remote_by_date.items():
            local = local_by_date.get(day)
            if local is None:
                issues.append(issue(symbol, day, "MISSING_LOCAL_ROW", "row", None, "present", "Massive has this daily bar but local cache does not"))
                continue
            compare_fields = (
                ("open", local.open, remote.open, config.price_tolerance),
                ("high", local.high, remote.high, config.price_tolerance),
                ("low", local.low, remote.low, config.price_tolerance),
                ("close", local.close, remote.close, config.price_tolerance),
                ("volume", local.volume, remote.volume, config.volume_tolerance),
                ("vwap", local.vwap, remote.vwap, config.price_tolerance),
                ("transactions", local.transactions, remote.transactions, 0.0),
                ("timestamp_ms", local.timestamp_ms, remote.timestamp_ms, 0.0),
            )
            for field, local_value, remote_value, tolerance in compare_fields:
                if not nullable_close(local_value, remote_value, tolerance):
                    issues.append(issue(symbol, day, "REMOTE_MISMATCH", field, local_value, remote_value, "local cache differs from fresh Massive fetch"))

        for day in sorted(set(local_by_date) - set(remote_by_date)):
            if offline_trading_day_decision(day).is_trading_day and day not in failed_dates:
                issues.append(issue(symbol, day, "EXTRA_LOCAL_ROW", "row", "present", None, "local cache has a row absent from fresh Massive fetch"))
    return issues


def nullable_close(left, right, tolerance: float) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return left == right


def build_summary_rows(
    sampled: list[str],
    local_rows: dict[str, dict[date, LocalDailyRow]],
    remote_rows,
    issues: list[dict[str, object]],
) -> list[dict[str, object]]:
    remote_by_symbol = {to_alpaca_symbol(symbol): bars for symbol, bars in remote_rows.items()}
    issue_counts: dict[str, int] = {}
    for item in issues:
        symbol = str(item["symbol"])
        issue_counts[symbol] = issue_counts.get(symbol, 0) + 1
    rows: list[dict[str, object]] = []
    for symbol in sampled:
        rows.append(
            {
                "symbol": symbol,
                "local_rows": len(local_rows.get(symbol, {})),
                "remote_rows": len(remote_by_symbol.get(symbol, [])),
                "issues": issue_counts.get(symbol, 0),
                "status": "OK" if issue_counts.get(symbol, 0) == 0 else "ISSUES",
            }
        )
    return rows


def write_sampled_symbols(config: DataSpotcheckConfig, symbols: list[str], seed: int) -> Path:
    path = config.output_dir / config.sampled_symbols_csv_name
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["sample_seed", "symbol"])
        writer.writeheader()
        writer.writerows({"sample_seed": seed, "symbol": symbol} for symbol in symbols)
    return path


def write_issue_csv(config: DataSpotcheckConfig, issues: list[dict[str, object]]) -> Path:
    path = config.output_dir / config.issue_csv_name
    fieldnames = ["symbol", "date", "issue_type", "field", "local_value", "expected_value", "message"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(issues)
    return path


def write_summary_csv(config: DataSpotcheckConfig, rows: list[dict[str, object]]) -> Path:
    path = config.output_dir / config.summary_csv_name
    fieldnames = ["symbol", "local_rows", "remote_rows", "issues", "status"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def issue(symbol: str, day: date, issue_type: str, field: str, local_value, expected_value, message: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "date": day.isoformat(),
        "issue_type": issue_type,
        "field": field,
        "local_value": local_value,
        "expected_value": expected_value,
        "message": message,
    }


def batched(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


@contextmanager
def connect_database(cache_path: Path):
    conn = sqlite3.connect(cache_path)
    try:
        yield conn
    finally:
        conn.close()
