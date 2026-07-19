from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from alpaca_ma5_service.alpaca_connection import load_alpaca_credentials
from alpaca_ma5_service.watchlist_generator import DailyBar
from backtest.data_cache import MarketDataCache
from backtest.history_rebuild_common import (
    ALPACA_BARS_URL,
    CandidateAsset,
    HistoricalHttpClient,
    batched,
    backup_sqlite_database,
    database_size_bytes,
    load_common_stock_universe,
    load_trading_sessions,
    normalize_symbol,
)


MARKET_TZ = ZoneInfo("America/New_York")
DEFAULT_FEED = "sip"
DEFAULT_ADJUSTMENT = "split"


@dataclass(frozen=True)
class DailyHistoryRebuildConfig:
    start_date: date
    end_date: date
    final_path: Path
    staging_path: Path
    output_dir: Path
    feed: str = DEFAULT_FEED
    adjustment: str = DEFAULT_ADJUSTMENT
    batch_size: int = 900
    request_limit: int = 10_000
    http_workers: int = 4
    max_attempts: int = 8
    symbols_override: tuple[str, ...] = ()
    replace_on_complete: bool = True


@dataclass(frozen=True)
class DailyBatchResult:
    requested_symbols: tuple[str, ...]
    bars_by_symbol: dict[str, list[DailyBar]]
    request_pages: int
    skipped_rows: int


@dataclass(frozen=True)
class DailyHistoryRebuildResult:
    database_path: Path
    candidate_symbols: int
    observed_symbols: int
    trading_sessions: int
    total_rows: int
    request_pages: int
    database_bytes: int
    backup_path: Path | None
    report_path: Path


def run_daily_history_rebuild(
    config: DailyHistoryRebuildConfig,
    *,
    logger: Callable[[str], None] = print,
) -> DailyHistoryRebuildResult:
    validate_config(config)
    api_key, secret_key = load_alpaca_credentials()
    http = HistoricalHttpClient(
        api_key,
        secret_key,
        max_attempts=config.max_attempts,
        pool_size=config.http_workers,
        logger=logger,
    )

    if config.symbols_override:
        symbols = sorted(
            {
                normalize_symbol(value)
                for value in config.symbols_override
                if normalize_symbol(value)
            }
        )
        candidates = [
            CandidateAsset(
                symbol=symbol,
                name="manual smoke-test symbol",
                exchange="",
                source_status="manual",
                tradable=True,
                classification_reason="manual_override",
            )
            for symbol in symbols
        ]
        classification_counts = {"manual_override": len(symbols)}
        security_master_method = "Explicit symbol override; not a full-market universe."
    else:
        candidates, classification_counts = load_common_stock_universe(
            http,
            logger=logger,
        )
        symbols = [candidate.symbol for candidate in candidates]
        security_master_method = (
            "Current Alpaca active/inactive US-equity snapshots plus current Nasdaq Trader "
            "ETF/Test Issue flags and security-name classification. Inactive candidates are "
            "included, but this is not an authoritative point-in-time security master."
        )
    if not symbols:
        raise RuntimeError("普通股候选池为空，拒绝创建空数据集。")

    sessions = load_trading_sessions(http, config.start_date, config.end_date)
    if not sessions:
        raise RuntimeError("指定日期范围内没有交易日。")
    valid_dates = {session.session_date for session in sessions}
    universe_digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()

    logger(
        f"日线重建范围 {config.start_date}..{config.end_date}，"
        f"{len(sessions)} 个交易日，{len(symbols):,} 只普通股候选，"
        f"feed={config.feed} adjustment={config.adjustment}"
    )
    initialize_staging_database(
        config,
        candidates,
        len(sessions),
        universe_digest,
        classification_counts,
        security_master_method,
    )
    cache = MarketDataCache(config.staging_path)
    symbol_batches = list(batched(symbols, config.batch_size))
    completed_batches = 0
    total_rows = 0
    request_pages = 0
    skipped_rows = 0

    with ThreadPoolExecutor(max_workers=config.http_workers) as executor:
        futures = {
            executor.submit(
                download_daily_batch,
                http,
                batch,
                config,
                valid_dates,
            ): index
            for index, batch in enumerate(symbol_batches, start=1)
        }
        for future in as_completed(futures):
            batch_index = futures[future]
            result = future.result()
            cache.save_daily_bars(
                result.bars_by_symbol,
                feed=config.feed,
                range_start=config.start_date,
                range_end_exclusive=config.end_date + timedelta(days=1),
                covered_symbols=list(result.requested_symbols),
                adjustment=config.adjustment,
            )
            completed_batches += 1
            batch_rows = sum(len(rows) for rows in result.bars_by_symbol.values())
            total_rows += batch_rows
            request_pages += result.request_pages
            skipped_rows += result.skipped_rows
            update_progress(
                config.staging_path,
                completed_batches=completed_batches,
                total_rows=total_rows,
                request_pages=request_pages,
                skipped_rows=skipped_rows,
            )
            logger(
                f"日线批次 {completed_batches}/{len(symbol_batches)} 完成"
                f"（源批次 {batch_index}）：rows={batch_rows:,}，"
                f"observed={len(result.bars_by_symbol):,}，pages={result.request_pages}"
            )

    observed_symbols, actual_rows = finalize_and_validate(
        config,
        expected_symbols=len(symbols),
        expected_sessions=len(sessions),
        valid_dates=valid_dates,
        expected_batches=len(symbol_batches),
    )
    backup_path: Path | None = None
    database_path = config.staging_path
    if config.replace_on_complete:
        backup_path = replace_database_atomically(config, logger=logger)
        database_path = config.final_path

    report_path = write_rebuild_report(
        config,
        database_path=database_path,
        candidate_symbols=len(symbols),
        observed_symbols=observed_symbols,
        trading_sessions=len(sessions),
        total_rows=actual_rows,
        request_pages=request_pages,
        classification_counts=classification_counts,
        security_master_method=security_master_method,
        backup_path=backup_path,
    )
    return DailyHistoryRebuildResult(
        database_path=database_path,
        candidate_symbols=len(symbols),
        observed_symbols=observed_symbols,
        trading_sessions=len(sessions),
        total_rows=actual_rows,
        request_pages=request_pages,
        database_bytes=database_size_bytes(database_path),
        backup_path=backup_path,
        report_path=report_path,
    )


def validate_config(config: DailyHistoryRebuildConfig) -> None:
    if config.start_date > config.end_date:
        raise ValueError("start_date must be <= end_date")
    if not 1 <= config.batch_size <= 1_000:
        raise ValueError("batch_size must be between 1 and 1000")
    if not 1 <= config.request_limit <= 10_000:
        raise ValueError("request_limit must be between 1 and 10000")
    if not 1 <= config.http_workers <= 8:
        raise ValueError("http_workers must be between 1 and 8")
    if config.final_path.resolve() == config.staging_path.resolve():
        raise ValueError("staging_path must differ from final_path")


def initialize_staging_database(
    config: DailyHistoryRebuildConfig,
    candidates: list[CandidateAsset],
    expected_sessions: int,
    universe_digest: str,
    classification_counts: dict[str, int],
    security_master_method: str,
) -> None:
    config.staging_path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (
        config.staging_path,
        Path(str(config.staging_path) + "-wal"),
        Path(str(config.staging_path) + "-shm"),
    ):
        if candidate.exists():
            candidate.unlink()
    MarketDataCache(config.staging_path)
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(sqlite3.connect(config.staging_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE daily_dataset_metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                adjustment TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                expected_sessions INTEGER NOT NULL,
                candidate_symbols INTEGER NOT NULL,
                completed_batches INTEGER NOT NULL,
                total_rows INTEGER NOT NULL,
                request_pages INTEGER NOT NULL,
                skipped_rows INTEGER NOT NULL,
                universe_sha256 TEXT NOT NULL,
                classification_counts_json TEXT NOT NULL,
                security_master_method TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE security_master (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                source_status TEXT NOT NULL,
                tradable INTEGER NOT NULL,
                classification_reason TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        conn.execute(
            """
            INSERT INTO daily_dataset_metadata(
                id, status, source, feed, timeframe, adjustment,
                start_date, end_date, expected_sessions, candidate_symbols,
                completed_batches, total_rows, request_pages, skipped_rows,
                universe_sha256, classification_counts_json,
                security_master_method, created_at
            )
            VALUES (1, 'building', 'Alpaca Market Data API', ?, '1Day', ?, ?, ?, ?, ?,
                    0, 0, 0, 0, ?, ?, ?, ?)
            """,
            (
                config.feed.lower(),
                config.adjustment.lower(),
                config.start_date.isoformat(),
                config.end_date.isoformat(),
                expected_sessions,
                len(candidates),
                universe_digest,
                json.dumps(classification_counts, ensure_ascii=False, sort_keys=True),
                security_master_method,
                created_at,
            ),
        )
        conn.executemany(
            """
            INSERT INTO security_master(
                symbol, name, exchange, source_status, tradable, classification_reason
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    candidate.symbol,
                    candidate.name,
                    candidate.exchange,
                    candidate.source_status,
                    int(candidate.tradable),
                    candidate.classification_reason,
                )
                for candidate in candidates
            ],
        )
        conn.commit()


def download_daily_batch(
    http: HistoricalHttpClient,
    symbols: list[str],
    config: DailyHistoryRebuildConfig,
    valid_dates: set[date],
) -> DailyBatchResult:
    bars_by_symbol: dict[str, list[DailyBar]] = defaultdict(list)
    token = ""
    request_pages = 0
    skipped_rows = 0
    while True:
        fields: dict[str, object] = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": f"{config.start_date.isoformat()}T00:00:00Z",
            "end": f"{(config.end_date + timedelta(days=1)).isoformat()}T00:00:00Z",
            "limit": config.request_limit,
            "feed": config.feed.lower(),
            "adjustment": config.adjustment.lower(),
            "sort": "asc",
        }
        if token:
            fields["page_token"] = token
        payload = http.get_json(ALPACA_BARS_URL, fields)
        request_pages += 1
        for raw_symbol, raw_rows in (payload.get("bars") or {}).items():
            symbol = normalize_symbol(raw_symbol)
            for raw in raw_rows:
                parsed = parse_daily_bar(symbol, raw, valid_dates)
                if parsed is None:
                    skipped_rows += 1
                    continue
                bars_by_symbol[symbol].append(parsed)
        token = str(payload.get("next_page_token") or "")
        if not token:
            break
    return DailyBatchResult(
        requested_symbols=tuple(symbols),
        bars_by_symbol=dict(bars_by_symbol),
        request_pages=request_pages,
        skipped_rows=skipped_rows,
    )


def parse_daily_bar(
    symbol: str,
    raw: dict,
    valid_dates: set[date],
) -> DailyBar | None:
    timestamp_text = str(raw.get("t", ""))
    timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
    bar_date = timestamp.astimezone(MARKET_TZ).date()
    if bar_date not in valid_dates:
        return None
    values = [float(raw[field]) for field in ("o", "h", "l", "c")]
    open_price, high, low, close = values
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise RuntimeError(f"{symbol} {timestamp_text} 返回了非正或非有限 OHLC")
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        raise RuntimeError(f"{symbol} {timestamp_text} 返回了非法 OHLC 关系")
    volume = optional_float(raw.get("v"))
    vwap = optional_float(raw.get("vw"))
    transactions = optional_int(raw.get("n"))
    return DailyBar(
        symbol=symbol,
        date=bar_date,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap=vwap,
        transactions=transactions,
        timestamp_ms=int(timestamp.timestamp() * 1000),
    )


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def update_progress(
    path: Path,
    *,
    completed_batches: int,
    total_rows: int,
    request_pages: int,
    skipped_rows: int,
) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            UPDATE daily_dataset_metadata
            SET completed_batches = ?, total_rows = ?, request_pages = ?, skipped_rows = ?
            WHERE id = 1
            """,
            (completed_batches, total_rows, request_pages, skipped_rows),
        )
        conn.commit()


def finalize_and_validate(
    config: DailyHistoryRebuildConfig,
    *,
    expected_symbols: int,
    expected_sessions: int,
    valid_dates: set[date],
    expected_batches: int,
) -> tuple[int, int]:
    completed_at = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(sqlite3.connect(config.staging_path)) as conn:
        conn.execute(
            """
            UPDATE daily_dataset_metadata
            SET status = 'complete', completed_at = ?
            WHERE id = 1
            """,
            (completed_at,),
        )
        conn.commit()
        metadata = conn.execute(
            """
            SELECT timeframe, expected_sessions, candidate_symbols,
                   completed_batches, total_rows
            FROM daily_dataset_metadata WHERE id = 1
            """
        ).fetchone()
        if metadata[:4] != ("1Day", expected_sessions, expected_symbols, expected_batches):
            raise RuntimeError(f"日线 metadata 不完整: {metadata}")
        actual_rows = int(conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])
        if actual_rows != int(metadata[4]) or actual_rows <= 0:
            raise RuntimeError(
                f"daily_bars 行数不一致或为空: actual={actual_rows}, metadata={metadata[4]}"
            )
        observed_symbols = int(
            conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bars").fetchone()[0]
        )
        range_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM fetch_ranges
                WHERE kind = 'daily' AND feed = ? AND adjustment = ?
                  AND start_key = ? AND end_key = ?
                """,
                (
                    config.feed.lower(),
                    config.adjustment.lower(),
                    config.start_date.isoformat(),
                    (config.end_date + timedelta(days=1)).isoformat(),
                ),
            ).fetchone()[0]
        )
        if range_count != expected_symbols:
            raise RuntimeError(
                f"日线缓存覆盖标记不完整: ranges={range_count}, candidates={expected_symbols}"
            )
        minute_rows = int(conn.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0])
        if minute_rows != 0:
            raise RuntimeError(f"最终日线 staging 意外包含 {minute_rows} 行分钟数据")
        invalid_ohlc = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM daily_bars
                WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                   OR high < open OR high < low OR high < close
                   OR low > open OR low > high OR low > close
                """
            ).fetchone()[0]
        )
        if invalid_ohlc:
            raise RuntimeError(f"日线包含 {invalid_ohlc} 行非法 OHLC")
        stored_dates = {
            date.fromisoformat(row[0])
            for row in conn.execute("SELECT DISTINCT bar_date FROM daily_bars")
        }
        invalid_dates = stored_dates - valid_dates
        if invalid_dates:
            raise RuntimeError(
                f"日线包含非交易日: {sorted(invalid_dates)[:10]}"
            )
        if min(stored_dates) < config.start_date or max(stored_dates) > config.end_date:
            raise RuntimeError(
                f"日线日期越界: {min(stored_dates)}..{max(stored_dates)}"
            )
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"SQLite quick_check 失败: {quick_check}")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise RuntimeError(f"SQLite WAL checkpoint 失败: {checkpoint}")
    return observed_symbols, actual_rows


def replace_database_atomically(
    config: DailyHistoryRebuildConfig,
    *,
    logger: Callable[[str], None],
) -> Path | None:
    backup_path: Path | None = None
    if config.final_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = config.output_dir / f"market_data_before_daily_replace_{timestamp}.sqlite"
        backup_sqlite_database(config.final_path, backup_path)
        logger(f"原 SQLite 已备份到 {backup_path}")
    config.final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(config.staging_path, config.final_path)
    return backup_path


def write_rebuild_report(
    config: DailyHistoryRebuildConfig,
    *,
    database_path: Path,
    candidate_symbols: int,
    observed_symbols: int,
    trading_sessions: int,
    total_rows: int,
    request_pages: int,
    classification_counts: dict[str, int],
    security_master_method: str,
    backup_path: Path | None,
) -> Path:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = config.output_dir / "daily_history_rebuild_manifest.json"
    payload = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "complete",
        "database_path": str(database_path),
        "database_bytes": database_size_bytes(database_path),
        "backup_path": str(backup_path) if backup_path else None,
        "source": "Alpaca Market Data API",
        "feed": config.feed.lower(),
        "timeframe": "1Day",
        "adjustment": config.adjustment.lower(),
        "start_date": config.start_date.isoformat(),
        "end_date": config.end_date.isoformat(),
        "candidate_symbols": candidate_symbols,
        "observed_symbols": observed_symbols,
        "trading_sessions": trading_sessions,
        "total_rows": total_rows,
        "request_pages": request_pages,
        "minute_rows": 0,
        "classification_counts": classification_counts,
        "security_master_method": security_master_method,
        "survivorship_bias_fully_eliminated": False,
    }
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, report_path)
    return report_path
