from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
import time as time_module
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import urllib3

from alpaca_ma5_service.trading_calendar import trading_day_decision


ALPACA_ASSETS_URL = "https://api.alpaca.markets/v2/assets"
ALPACA_CALENDAR_URL = "https://api.alpaca.markets/v2/calendar"
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
NASDAQ_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
MARKET_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

_EXCLUDED_SECURITY_NAME = re.compile(
    r"\b(ETFS?|ETNS?|ETPS?|WARRANTS?|RIGHTS?|UNITS?|PREFERRED|PREFERENCE|"
    r"FUNDS?|NOTES?|BONDS?|DEBENTURES?|INDEX|NEXTSHARES|ACQUISITION|SPAC|"
    r"BLANK CHECK|SHELL COMPAN(?:Y|IES)|STRUCTURED PRODUCTS?|STRATS|"
    r"ASSET[- ]BACKED|CORPORATE BACKED|REPACKAGED|PPLUS|CORTS|"
    r"CAPITAL SECURITIES|FLOATING RATE|FIXED INCOME|DEPOSITOR)\b",
    re.IGNORECASE,
)
_TRUST_SECURITY_NAME = re.compile(r"\bTRUST\b", re.IGNORECASE)
_OPERATING_TRUST_NAME = re.compile(
    r"\b(REIT|REAL ESTATE(?: INVESTMENT)? TRUST|"
    r"(?:REALTY|PROPERTY|PROPERTIES|ASSETS|HOSPITALITY|LODGING|MORTGAGE|"
    r"HOUSING|HEALTHCARE|HOMES|INFRASTRUCTURE|ROYALTY)(?:\s+[A-Z]+){0,3}\s+TRUST|"
    r"TRUST (?:BANCORP|BANK|FINANCIAL))\b",
    re.IGNORECASE,
)
_CLOSED_END_TRUST_NAME = re.compile(
    r"\b(BLACKROCK|INVESCO|EATON VANCE|ROYCE|DWS|GAMCO|MUNICIPAL|MUNI|"
    r"TARGET TERM|TERM TRUST|INCOME TRUST|ENHANCED .+ TRUST|"
    r"ALLOCATION .+ TRUST|SCIENCE AND TECHNOLOGY .+ TRUST|"
    r"RESOURCES .+ TRUST|DIVIDEND .+ TRUST|CREDIT .+ TRUST|"
    r"LIMITED DURATION|OPPORTUNIT(?:Y|IES) TRUST|HEALTH SCIENCES .+ TRUST)\b",
    re.IGNORECASE,
)
_INCLUDED_SECURITY_NAME = re.compile(
    r"\b(COMMON STOCK|COMMON SHARES?|ORDINARY SHARES?|AMERICAN DEPOSITARY "
    r"(?:SHARES?|RECEIPTS?)|ADSS?|ADRS?|CLASS [A-Z0-9]+ (?:STOCK|SHARES?)|"
    r"CAPITAL STOCK|SHARES? OF COMMON STOCK)\b",
    re.IGNORECASE,
)
_COMPANY_NAME = re.compile(
    r"\b(INC(?:ORPORATED)?|CORP(?:ORATION)?|COMPAN(?:Y|IES)|CO|LTD|LIMITED|"
    r"PLC|LLC|HOLDINGS?|GROUP|INDUSTRIES|RESOURCES|SERVICES|TECHNOLOGIES)\b",
    re.IGNORECASE,
)
_VALID_TICKER = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z])?$")
_PRIMARY_LISTING_EXCHANGES = {"NYSE", "NASDAQ", "AMEX"}


@dataclass(frozen=True)
class TradingSession:
    session_date: date
    open_utc: str
    close_utc: str


@dataclass(frozen=True)
class CandidateAsset:
    symbol: str
    name: str
    exchange: str
    source_status: str
    tradable: bool
    classification_reason: str


class HistoricalHttpClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        max_attempts: int,
        pool_size: int,
        logger: Callable[[str], None],
    ):
        self._auth_headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept-Encoding": "gzip",
        }
        self._max_attempts = max(1, max_attempts)
        self._logger = logger
        self._logger_lock = threading.Lock()
        self._pool = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=15.0, read=120.0),
            retries=False,
            maxsize=max(1, pool_size),
            block=True,
        )

    def get_json(
        self,
        url: str,
        fields: dict[str, object] | None = None,
        *,
        authenticated: bool = True,
    ):
        return json.loads(self._request(url, fields, authenticated=authenticated))

    def get_text(self, url: str, *, authenticated: bool = False) -> str:
        return self._request(url, None, authenticated=authenticated)

    def _request(
        self,
        url: str,
        fields: dict[str, object] | None,
        *,
        authenticated: bool,
    ) -> str:
        headers = self._auth_headers if authenticated else None
        for attempt in range(self._max_attempts):
            try:
                response = self._pool.request(
                    "GET",
                    url,
                    fields=fields,
                    headers=headers,
                )
            except Exception as exc:
                if attempt + 1 >= self._max_attempts:
                    raise RuntimeError(
                        f"历史行情网络请求失败: {type(exc).__name__}: {exc}"
                    ) from exc
                delay = min(30.0, float(2**attempt))
                self._log(f"网络异常，{delay:.1f}s 后重试: {type(exc).__name__}")
                time_module.sleep(delay)
                continue

            try:
                body = response.data.decode("utf-8", errors="replace")
                if response.status == 200:
                    return body
                if response.status == 429 or 500 <= response.status < 600:
                    if attempt + 1 >= self._max_attempts:
                        raise RuntimeError(f"HTTP {response.status}: {body[:500]}")
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = (
                            float(retry_after)
                            if retry_after
                            else min(30.0, float(2**attempt))
                        )
                    except ValueError:
                        delay = min(30.0, float(2**attempt))
                    self._log(f"HTTP {response.status}，{delay:.1f}s 后重试")
                    time_module.sleep(delay)
                    continue
                raise RuntimeError(f"HTTP {response.status}: {body[:500]}")
            finally:
                response.release_conn()
        raise RuntimeError("历史行情请求重试耗尽")

    def _log(self, message: str) -> None:
        with self._logger_lock:
            self._logger(message)


def load_common_stock_universe(
    http: HistoricalHttpClient,
    *,
    logger: Callable[[str], None],
) -> tuple[list[CandidateAsset], dict[str, int]]:
    logger("读取 Alpaca active/inactive US equity 资产目录（只读）")
    active_assets = http.get_json(
        ALPACA_ASSETS_URL,
        {"status": "active", "asset_class": "us_equity"},
    )
    inactive_assets = http.get_json(
        ALPACA_ASSETS_URL,
        {"status": "inactive", "asset_class": "us_equity"},
    )
    logger("读取 Nasdaq Trader 上市目录，用 ETF/Test Issue 字段排除非普通股")
    directory = load_nasdaq_directory(http)

    counts: Counter[str] = Counter()
    candidates_by_symbol: dict[str, CandidateAsset] = {}
    for status, assets in (("active", active_assets), ("inactive", inactive_assets)):
        for asset in assets:
            directory_row = directory.get(normalize_symbol(asset.get("symbol", "")), {})
            ok, reason = classify_common_stock(asset, directory_row)
            counts[f"{status}_{reason}"] += 1
            if not ok:
                continue
            symbol = normalize_symbol(asset.get("symbol", ""))
            candidate = CandidateAsset(
                symbol=symbol,
                name=str(asset.get("name", "") or ""),
                exchange=str(asset.get("exchange", "") or "").upper(),
                source_status=status,
                tradable=bool(asset.get("tradable")),
                classification_reason=reason,
            )
            existing = candidates_by_symbol.get(symbol)
            if existing is None or existing.source_status == "inactive":
                candidates_by_symbol[symbol] = candidate
    candidates = [candidates_by_symbol[symbol] for symbol in sorted(candidates_by_symbol)]
    logger(
        f"普通股候选 {len(candidates):,} 只："
        f"active={sum(asset.source_status == 'active' for asset in candidates):,}，"
        f"inactive={sum(asset.source_status == 'inactive' for asset in candidates):,}"
    )
    return candidates, dict(sorted(counts.items()))


def classify_common_stock(
    asset: dict,
    directory_row: dict[str, str],
) -> tuple[bool, str]:
    symbol = normalize_symbol(asset.get("symbol", ""))
    exchange = str(asset.get("exchange", "") or "").upper()
    if not symbol or exchange == "OTC":
        return False, "otc_or_blank"
    if not _VALID_TICKER.fullmatch(symbol):
        return False, "invalid_ticker_syntax"
    if re.search(r"(?:[.\-]WI)$", symbol, re.IGNORECASE):
        return False, "when_issued"
    if (directory_row.get("ETF") or "").upper() == "Y":
        return False, "etf"
    if (directory_row.get("Test Issue") or "").upper() == "Y":
        return False, "test_issue"
    combined = " | ".join(
        value
        for value in (
            str(asset.get("name", "") or ""),
            directory_row.get("Security Name", ""),
        )
        if value
    )
    if _EXCLUDED_SECURITY_NAME.search(combined):
        return False, "excluded_security_type"
    if _TRUST_SECURITY_NAME.search(combined):
        operating_trust = _OPERATING_TRUST_NAME.search(combined)
        listed_trust_company = (
            _COMPANY_NAME.search(combined)
            and re.search(r"\bCOMMON STOCK\b", combined, re.IGNORECASE)
            and not _CLOSED_END_TRUST_NAME.search(combined)
        )
        if not operating_trust and not listed_trust_company:
            return False, "non_operating_trust"
    if _INCLUDED_SECURITY_NAME.search(combined):
        return True, "common_name"
    if _COMPANY_NAME.search(combined):
        return True, "company_name"
    if (
        directory_row
        and (directory_row.get("ETF") or "").upper() == "N"
        and re.search(r"\bSHARES?\b", combined, re.IGNORECASE)
    ):
        return True, "shares_fallback"
    if exchange in _PRIMARY_LISTING_EXCHANGES:
        return True, "listed_equity_fallback"
    return False, "ambiguous_type"


def load_nasdaq_directory(http: HistoricalHttpClient) -> dict[str, dict[str, str]]:
    directory: dict[str, dict[str, str]] = {}
    for row in parse_pipe_directory(http.get_text(NASDAQ_LISTED_URL)):
        symbol = normalize_symbol(row.get("Symbol", ""))
        if symbol:
            directory[symbol] = row
    for row in parse_pipe_directory(http.get_text(NASDAQ_OTHER_URL)):
        for field in ("ACT Symbol", "CQS Symbol", "NASDAQ Symbol"):
            symbol = normalize_symbol(row.get(field, ""))
            if symbol:
                directory.setdefault(symbol, row)
    return directory


def parse_pipe_directory(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in csv.DictReader(text.splitlines(), delimiter="|"):
        if not row:
            continue
        first = next(iter(row.values()), "") or ""
        if first.startswith("File Creation Time"):
            continue
        rows.append(
            {
                str(key): (value or "").strip()
                for key, value in row.items()
                if key is not None
            }
        )
    return rows


def load_trading_sessions(
    http: HistoricalHttpClient,
    start_date: date,
    end_date: date,
) -> list[TradingSession]:
    payload = http.get_json(
        ALPACA_CALENDAR_URL,
        {"start": start_date.isoformat(), "end": end_date.isoformat()},
    )
    sessions_by_date: dict[date, TradingSession] = {}
    for raw in payload:
        session_date = date.fromisoformat(str(raw["date"]))
        open_et = datetime.combine(
            session_date,
            time.fromisoformat(str(raw["open"])),
            tzinfo=MARKET_TZ,
        )
        close_et = datetime.combine(
            session_date,
            time.fromisoformat(str(raw["close"])),
            tzinfo=MARKET_TZ,
        )
        sessions_by_date[session_date] = TradingSession(
            session_date=session_date,
            open_utc=open_et.astimezone(UTC_TZ).isoformat(timespec="seconds"),
            close_utc=close_et.astimezone(UTC_TZ).isoformat(timespec="seconds"),
        )

    expected_dates: list[date] = []
    current = start_date
    while current <= end_date:
        if trading_day_decision(current, use_alpaca=False).is_trading_day:
            expected_dates.append(current)
        current += timedelta(days=1)
    expected_set = set(expected_dates)
    missing = [value for value in expected_dates if value not in sessions_by_date]
    unexpected = [value for value in sessions_by_date if value not in expected_set]
    if missing or unexpected:
        raise RuntimeError(
            "交易日日历不一致，拒绝静默缺日："
            f"missing={','.join(map(str, missing[:10])) or '-'} "
            f"unexpected={','.join(map(str, unexpected[:10])) or '-'}"
        )
    return [sessions_by_date[value] for value in expected_dates]


def backup_sqlite_database(source_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(source_path)) as source,
        closing(sqlite3.connect(backup_path)) as target,
    ):
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"SQLite 备份校验失败: {backup_path}")


def database_size_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (
            path,
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
        )
        if candidate.exists()
    )


def normalize_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def batched(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
