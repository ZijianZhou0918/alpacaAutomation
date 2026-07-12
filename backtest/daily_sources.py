from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from collections import deque
from time import monotonic, sleep

from alpaca_ma5_service.moomoo_market_data import MoomooRealtimePriceSource
from alpaca_ma5_service.watchlist import normalize_symbol, to_alpaca_symbol
from alpaca_ma5_service.watchlist_generator import DailyBar


MOOMOO_DAILY_FEED = "moomoo"
MOOMOO_DAILY_ADJUSTMENT = "qfq"
YAHOO_DAILY_FEED = "yahoo"
YAHOO_DAILY_ADJUSTMENT = "adj"
MASSIVE_DAILY_FEED = "massive"
MASSIVE_DAILY_ADJUSTMENT = "adj"


@dataclass(frozen=True)
class MoomooDailyConfig:
    host: str
    port: int
    security_firm: str
    connect_timeout: float
    opend_exe_path: str
    opend_startup_timeout: float
    max_requests_per_window: int = 50
    request_window_seconds: float = 30.0
    rate_limit_retry_seconds: float = 31.0
    max_retries: int = 3


@dataclass(frozen=True)
class YahooDailyConfig:
    request_sleep_seconds: float = 0.05
    rate_limit_retry_seconds: float = 10.0
    max_retries: int = 3
    user_agent: str = "Mozilla/5.0"


@dataclass(frozen=True)
class MassiveDailyConfig:
    api_keys: tuple[str, ...]
    max_workers: int = 12
    request_timeout_seconds: float = 30.0
    retry_sleep_seconds: float = 3.0
    max_retries: int = 3
    base_url: str = "https://api.massive.com"
    progress_enabled: bool = True
    progress_interval_seconds: float = 10.0
    progress_interval_dates: int = 20


@dataclass(frozen=True)
class DailyFetchResult:
    bars_by_symbol: dict[str, list[DailyBar]]
    failures: list[dict[str, str]]


@dataclass(frozen=True)
class MassiveGroupedDailyRow:
    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    vwap: float | None = None
    transactions: int | None = None
    timestamp_ms: int | None = None


def split_api_keys(value: str) -> tuple[str, ...]:
    keys = [item.strip() for item in value.replace("\n", ",").replace(";", ",").split(",")]
    return tuple(dict.fromkeys(key for key in keys if key))


def load_massive_api_keys(env: dict[str, str] | None = None) -> tuple[str, ...]:
    raw = os.getenv("MASSIVE_API_KEYS") or os.getenv("POLYGON_API_KEYS") or ""
    if not raw and env:
        raw = env.get("MASSIVE_API_KEYS", "") or env.get("POLYGON_API_KEYS", "")
    return split_api_keys(raw)


def fetch_massive_grouped_daily_bars(
    symbols: list[str],
    start_date: date,
    end_date_exclusive: date,
    config: MassiveDailyConfig,
) -> dict[str, list[DailyBar]]:
    return fetch_massive_grouped_daily_bars_with_failures(symbols, start_date, end_date_exclusive, config).bars_by_symbol


def fetch_massive_grouped_daily_bars_with_failures(
    symbols: list[str],
    start_date: date,
    end_date_exclusive: date,
    config: MassiveDailyConfig,
) -> DailyFetchResult:
    if not config.api_keys:
        raise RuntimeError("Missing Massive API keys. Set MASSIVE_API_KEYS in environment or .env.")

    wanted = {normalize_massive_ticker(symbol): to_alpaca_symbol(symbol) for symbol in symbols}
    wanted = {source: target for source, target in wanted.items() if source and target}
    out: dict[str, list[DailyBar]] = {symbol: [] for symbol in wanted.values()}
    failures: list[dict[str, str]] = []
    days = [day for day in iter_calendar_days(start_date, end_date_exclusive) if day.weekday() < 5]
    workers = max(1, min(config.max_workers, len(days) or 1))
    completed_dates = 0
    success_dates = 0
    rate_limited_dates = 0
    failed_dates = 0
    matched_rows = 0
    raw_rows = 0
    started_at = monotonic()
    last_progress_at = started_at

    if config.progress_enabled:
        print(
            f"Massive grouped daily start: dates={len(days)} symbols={len(wanted)} "
            f"workers={workers} keys={len(config.api_keys)} range={start_date}->{end_date_exclusive}",
            flush=True,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for index, day in enumerate(days):
            api_key = config.api_keys[index % len(config.api_keys)]
            futures[executor.submit(fetch_one_massive_grouped_day, day, api_key, config)] = day

        for future in as_completed(futures):
            day = futures[future]
            try:
                rows, failure = future.result()
            except Exception as exc:
                completed_dates += 1
                failed_dates += 1
                failures.append({"symbol": "*", "source_symbol": day.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
                last_progress_at = maybe_print_massive_progress(
                    config,
                    completed_dates,
                    len(days),
                    success_dates,
                    rate_limited_dates,
                    failed_dates,
                    matched_rows,
                    raw_rows,
                    started_at,
                    last_progress_at,
                    force=completed_dates == len(days),
                )
                continue
            if failure is not None:
                completed_dates += 1
                if is_massive_rate_limit_failure(failure):
                    rate_limited_dates += 1
                else:
                    failed_dates += 1
                failures.append(failure)
                last_progress_at = maybe_print_massive_progress(
                    config,
                    completed_dates,
                    len(days),
                    success_dates,
                    rate_limited_dates,
                    failed_dates,
                    matched_rows,
                    raw_rows,
                    started_at,
                    last_progress_at,
                    force=completed_dates == len(days),
                )
                continue
            completed_dates += 1
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
            last_progress_at = maybe_print_massive_progress(
                config,
                completed_dates,
                len(days),
                success_dates,
                rate_limited_dates,
                failed_dates,
                matched_rows,
                raw_rows,
                started_at,
                last_progress_at,
                force=completed_dates == len(days),
            )

    cleaned = {
        symbol: sorted(bars, key=lambda bar: bar.date)
        for symbol, bars in out.items()
        if bars
    }
    return DailyFetchResult(cleaned, failures)


def maybe_print_massive_progress(
    config: MassiveDailyConfig,
    completed_dates: int,
    total_dates: int,
    success_dates: int,
    rate_limited_dates: int,
    failed_dates: int,
    matched_rows: int,
    raw_rows: int,
    started_at: float,
    last_progress_at: float,
    *,
    force: bool = False,
) -> float:
    if not config.progress_enabled:
        return last_progress_at
    now = monotonic()
    by_dates = config.progress_interval_dates > 0 and completed_dates % config.progress_interval_dates == 0
    by_time = now - last_progress_at >= max(1.0, config.progress_interval_seconds)
    if not force and not by_dates and not by_time:
        return last_progress_at
    percent = completed_dates / max(1, total_dates) * 100.0
    print(
        f"Massive grouped daily progress: dates={completed_dates}/{total_dates} {percent:5.1f}% "
        f"success={success_dates} 429={rate_limited_dates} failed={failed_dates} "
        f"matched_rows={matched_rows:,} raw_rows={raw_rows:,} elapsed={format_elapsed(monotonic() - started_at)}",
        flush=True,
    )
    return now


def is_massive_rate_limit_failure(failure: dict[str, str]) -> bool:
    text = failure.get("error", "").lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def failure_dates(failures: list[dict[str, str]]) -> list[date]:
    out: list[date] = []
    for failure in failures:
        try:
            out.append(date.fromisoformat(failure.get("source_symbol", "")))
        except ValueError:
            continue
    return sorted(set(out))


def coalesced_date_ranges(dates: list[date]) -> list[tuple[date, date]]:
    ordered = sorted(set(dates))
    if not ordered:
        return []
    ranges: list[tuple[date, date]] = []
    start = ordered[0]
    end = ordered[0]
    for day in ordered[1:]:
        if (day - end).days <= 3:
            end = day
            continue
        ranges.append((start, end + timedelta(days=1)))
        start = day
        end = day
    ranges.append((start, end + timedelta(days=1)))
    return ranges


def filter_daily_bars_to_dates(bars_by_symbol: dict[str, list[DailyBar]], dates: set[date]) -> dict[str, list[DailyBar]]:
    return {
        symbol: [bar for bar in bars if bar.date in dates]
        for symbol, bars in bars_by_symbol.items()
        if any(bar.date in dates for bar in bars)
    }


def merge_daily_bars(primary: dict[str, list[DailyBar]], fallback: dict[str, list[DailyBar]]) -> dict[str, list[DailyBar]]:
    merged: dict[str, dict[date, DailyBar]] = {}
    for source in (primary, fallback):
        for symbol, bars in source.items():
            by_date = merged.setdefault(to_alpaca_symbol(symbol), {})
            for bar in bars:
                by_date[bar.date] = bar
    return {
        symbol: [by_date[day] for day in sorted(by_date)]
        for symbol, by_date in merged.items()
        if by_date
    }


def fetch_one_massive_grouped_day(
    day: date,
    api_key: str,
    config: MassiveDailyConfig,
) -> tuple[list[MassiveGroupedDailyRow], dict[str, str] | None]:
    params = urllib.parse.urlencode({"adjusted": "true", "include_otc": "false", "apiKey": api_key})
    url = f"{config.base_url.rstrip('/')}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}?{params}"
    last_error: object = None
    for attempt in range(config.max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "alpaca-ma5-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=config.request_timeout_seconds) as response:
                payload = json.load(response)
            return massive_grouped_daily_rows_from_payload(payload), None
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                return [], None
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= config.max_retries:
                break
            sleep(config.retry_sleep_seconds * (attempt + 1))
        except Exception as exc:
            last_error = exc
            if attempt >= config.max_retries:
                break
            sleep(config.retry_sleep_seconds * (attempt + 1))
    return [], {"symbol": "*", "source_symbol": day.isoformat(), "error": f"{type(last_error).__name__}: {last_error}"}


def massive_grouped_daily_rows_from_payload(payload: dict) -> list[MassiveGroupedDailyRow]:
    rows: list[MassiveGroupedDailyRow] = []
    for item in payload.get("results") or []:
        ticker = str(item.get("T") or "").strip().upper()
        try:
            open_price = item["o"]
            high = item["h"]
            low = item["l"]
            close = item["c"]
        except KeyError:
            continue
        if not ticker or open_price is None or high is None or low is None or close is None:
            continue
        rows.append(
            MassiveGroupedDailyRow(
                ticker=ticker,
                open=float(open_price),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=optional_float(item.get("v")),
                vwap=optional_float(item.get("vw")),
                transactions=optional_int(item.get("n")),
                timestamp_ms=optional_int(item.get("t")),
            )
        )
    return rows


def optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_massive_ticker(symbol: str) -> str:
    return normalize_symbol(symbol).removeprefix("US.").replace(".", "-").upper()


def iter_calendar_days(start_date: date, end_date_exclusive: date):
    day = start_date
    while day < end_date_exclusive:
        yield day
        day += timedelta(days=1)


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def fetch_yahoo_daily_bars(
    symbols: list[str],
    start_date: date,
    end_date_exclusive: date,
    config: YahooDailyConfig,
) -> dict[str, list[DailyBar]]:
    return fetch_yahoo_daily_bars_with_failures(symbols, start_date, end_date_exclusive, config).bars_by_symbol


def fetch_yahoo_daily_bars_with_failures(
    symbols: list[str],
    start_date: date,
    end_date_exclusive: date,
    config: YahooDailyConfig,
) -> DailyFetchResult:
    out: dict[str, list[DailyBar]] = {}
    failures: list[dict[str, str]] = []
    for symbol in symbols:
        alpaca_symbol = to_alpaca_symbol(symbol)
        yahoo_symbol = alpaca_symbol.replace(".", "-")
        rows, failure = fetch_one_yahoo_daily_bars(yahoo_symbol, alpaca_symbol, start_date, end_date_exclusive, config)
        if rows:
            out[alpaca_symbol] = rows
        elif failure is not None:
            failures.append(failure)
        sleep(max(0.0, config.request_sleep_seconds))
    return DailyFetchResult(out, failures)


def fetch_one_yahoo_daily_bars(
    yahoo_symbol: str,
    output_symbol: str,
    start_date: date,
    end_date_exclusive: date,
    config: YahooDailyConfig,
) -> tuple[list[DailyBar], dict[str, str] | None]:
    period1 = int(datetime.combine(start_date, time.min, tzinfo=UTC).timestamp())
    period2 = int(datetime.combine(end_date_exclusive, time.min, tzinfo=UTC).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
        f"?period1={period1}&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    )
    last_error: object = None
    for attempt in range(config.max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": config.user_agent})
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.load(response)
            rows = yahoo_daily_bars_from_payload(output_symbol, payload, start_date, end_date_exclusive)
            if rows:
                return rows, None
            return [], {"symbol": output_symbol, "source_symbol": yahoo_symbol, "error": "empty_result"}
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 502, 503, 504} or attempt >= config.max_retries:
                break
            wait_seconds = config.rate_limit_retry_seconds * (attempt + 1)
            print(f"Yahoo daily bars retry {yahoo_symbol}: HTTP {exc.code}; waiting {wait_seconds:.1f}s", flush=True)
            sleep(wait_seconds)
        except Exception as exc:
            last_error = exc
            break
    return [], {"symbol": output_symbol, "source_symbol": yahoo_symbol, "error": f"{type(last_error).__name__}: {last_error}"}


def yahoo_daily_bars_from_payload(symbol: str, payload: dict, start_date: date, end_date_exclusive: date) -> list[DailyBar]:
    result = (((payload.get("chart") or {}).get("result") or [None])[0])
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    rows: list[DailyBar] = []
    for index, ts in enumerate(timestamps):
        try:
            day = datetime.fromtimestamp(int(ts), UTC).date()
            open_price = opens[index]
            high = highs[index]
            low = lows[index]
            close = closes[index]
        except (IndexError, TypeError, ValueError):
            continue
        if not (start_date <= day < end_date_exclusive):
            continue
        if open_price is None or high is None or low is None or close is None:
            continue
        rows.append(DailyBar(symbol, day, float(open_price), float(high), float(low), float(close)))
    return sorted(rows, key=lambda bar: bar.date)


def fetch_moomoo_daily_bars(
    symbols: list[str],
    start_date: date,
    end_date_exclusive: date,
    config: MoomooDailyConfig,
) -> dict[str, list[DailyBar]]:
    source = MoomooRealtimePriceSource(
        host=config.host,
        port=config.port,
        security_firm=config.security_firm,
        connect_timeout=config.connect_timeout,
        opend_exe_path=config.opend_exe_path,
        opend_startup_timeout=config.opend_startup_timeout,
    )
    out: dict[str, list[DailyBar]] = {}
    try:
        source._connect()
        mm = source.mm
        end_inclusive = end_date_exclusive - timedelta(days=1)
        limiter = MoomooHistoryRateLimiter(config.max_requests_per_window, config.request_window_seconds)
        for symbol in symbols:
            code = normalize_symbol(symbol)
            ret, data, page_req_key = request_history_kline_with_retry(
                source,
                limiter,
                code=code,
                start=start_date,
                end=end_inclusive,
                page_req_key=None,
                config=config,
            )
            if ret != mm.RET_OK:
                print(f"Moomoo daily bars failed, skipped {code}: {data}", flush=True)
                continue
            rows = daily_bars_from_moomoo_frame(to_alpaca_symbol(code), data, start_date, end_date_exclusive)
            page_failed = False
            while page_req_key is not None:
                ret, data, page_req_key = request_history_kline_with_retry(
                    source,
                    limiter,
                    code=code,
                    start=start_date,
                    end=end_inclusive,
                    page_req_key=page_req_key,
                    config=config,
                )
                if ret != mm.RET_OK:
                    print(f"Moomoo daily bars page failed, skipped {code}: {data}", flush=True)
                    page_failed = True
                    break
                rows.extend(daily_bars_from_moomoo_frame(to_alpaca_symbol(code), data, start_date, end_date_exclusive))
            if page_failed:
                continue
            out[to_alpaca_symbol(code)] = sorted(rows, key=lambda bar: bar.date)
    finally:
        source.close()
    return out


class MoomooHistoryRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max(1, max_requests)
        self.window_seconds = max(1.0, window_seconds)
        self.calls = deque()

    def wait(self) -> None:
        now = monotonic()
        while self.calls and now - self.calls[0] >= self.window_seconds:
            self.calls.popleft()
        if len(self.calls) >= self.max_requests:
            wait_seconds = self.window_seconds - (now - self.calls[0]) + 0.2
            if wait_seconds > 0:
                print(f"Moomoo history rate limit guard: waiting {wait_seconds:.1f}s", flush=True)
                sleep(wait_seconds)
            now = monotonic()
            while self.calls and now - self.calls[0] >= self.window_seconds:
                self.calls.popleft()
        self.calls.append(monotonic())

    def clear(self) -> None:
        self.calls.clear()


def request_history_kline_with_retry(
    source: MoomooRealtimePriceSource,
    limiter: MoomooHistoryRateLimiter,
    *,
    code: str,
    start: date,
    end: date,
    page_req_key,
    config: MoomooDailyConfig,
):
    mm = source.mm
    last_data = None
    for attempt in range(config.max_retries + 1):
        limiter.wait()
        ret, data, next_page_req_key = source.quote_ctx.request_history_kline(
            code,
            start=start.isoformat(),
            end=end.isoformat(),
            ktype=mm.KLType.K_DAY,
            autype=mm.AuType.QFQ,
            fields=[mm.KL_FIELD.ALL],
            max_count=None,
            page_req_key=page_req_key,
        )
        if ret == mm.RET_OK:
            return ret, data, next_page_req_key
        last_data = data
        if not is_moomoo_rate_limit_error(data) or attempt >= config.max_retries:
            return ret, data, next_page_req_key
        print(
            f"Moomoo history rate limited for {code}; waiting {config.rate_limit_retry_seconds:.1f}s then retrying ({attempt + 1}/{config.max_retries})",
            flush=True,
        )
        limiter.clear()
        sleep(max(1.0, config.rate_limit_retry_seconds))
    return -1, last_data, None


def is_moomoo_rate_limit_error(data) -> bool:
    text = str(data)
    lowered = text.lower()
    return (
        "频率" in text
        or "rate" in lowered
        or "frequency" in lowered
        or ("30" in text and "60" in text)
    )


def daily_bars_from_moomoo_frame(symbol: str, data, start_date: date, end_date_exclusive: date) -> list[DailyBar]:
    if data is None or getattr(data, "empty", False):
        return []
    rows: list[DailyBar] = []
    for _, row in data.iterrows():
        day = moomoo_day(row.get("time_key"))
        if day is None or not (start_date <= day < end_date_exclusive):
            continue
        rows.append(
            DailyBar(
                symbol=symbol,
                date=day,
                open=float(row.get("open")),
                high=float(row.get("high")),
                low=float(row.get("low")),
                close=float(row.get("close")),
            )
        )
    return rows


def moomoo_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text).date()
