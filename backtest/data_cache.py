from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from alpaca_ma5_service.afterhours_high_low import MinuteBar
from alpaca_ma5_service.watchlist import to_alpaca_symbol
from alpaca_ma5_service.watchlist_generator import DailyBar


ADJUSTMENT_SPLIT = "split"


class MarketDataCache:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def load_daily_bars(
        self,
        symbols: list[str],
        start_date: date,
        end_date_exclusive: date,
        *,
        feed: str,
        adjustment: str = ADJUSTMENT_SPLIT,
    ) -> dict[str, list[DailyBar]]:
        out: dict[str, list[DailyBar]] = {}
        with self._connect() as conn:
            for batch in batched(normalize_symbols(symbols), 800):
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT symbol, bar_date, open, high, low, close, volume, vwap, transactions, timestamp_ms
                    FROM daily_bars
                    WHERE feed = ?
                      AND adjustment = ?
                      AND bar_date >= ?
                      AND bar_date < ?
                      AND symbol IN ({placeholders})
                    ORDER BY symbol, bar_date
                    """,
                    [feed.lower(), adjustment, start_date.isoformat(), end_date_exclusive.isoformat(), *batch],
                ).fetchall()
                for symbol, bar_date, open_price, high, low, close, volume, vwap, transactions, timestamp_ms in rows:
                    out.setdefault(symbol, []).append(
                        DailyBar(symbol, date.fromisoformat(bar_date), open_price, high, low, close, volume, vwap, transactions, timestamp_ms)
                    )
        return out

    def save_daily_bars(
        self,
        bars_by_symbol: dict[str, list[DailyBar]],
        *,
        feed: str,
        range_start: date,
        range_end_exclusive: date,
        covered_symbols: list[str] | None = None,
        adjustment: str = ADJUSTMENT_SPLIT,
    ) -> None:
        feed_key = feed.lower()
        symbols = normalize_symbols(covered_symbols or list(bars_by_symbol))
        bar_symbols = normalize_symbols(list(bars_by_symbol))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as conn:
            for symbol, bars in bars_by_symbol.items():
                alpaca_symbol = to_alpaca_symbol(symbol)
                conn.executemany(
                    """
                    INSERT INTO daily_bars(symbol, bar_date, feed, adjustment, open, high, low, close, volume, vwap, transactions, timestamp_ms, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, bar_date, feed, adjustment) DO UPDATE SET
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        volume = excluded.volume,
                        vwap = excluded.vwap,
                        transactions = excluded.transactions,
                        timestamp_ms = excluded.timestamp_ms,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (
                            alpaca_symbol,
                            bar.date.isoformat(),
                            feed_key,
                            adjustment,
                            bar.open,
                            bar.high,
                            bar.low,
                            bar.close,
                            getattr(bar, "volume", None),
                            getattr(bar, "vwap", None),
                            getattr(bar, "transactions", None),
                            getattr(bar, "timestamp_ms", None),
                            now,
                        )
                        for bar in bars
                    ],
                )
            self._mark_ranges(conn, "daily", symbols, range_start.isoformat(), range_end_exclusive.isoformat(), feed_key, adjustment, now)
            for symbol in bar_symbols:
                self._recompute_daily_mas(conn, symbol, feed_key, adjustment)

    def load_minute_bars(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        *,
        feed: str,
        adjustment: str = ADJUSTMENT_SPLIT,
    ) -> dict[str, list[MinuteBar]]:
        out: dict[str, list[MinuteBar]] = {}
        start_key = utc_key(start)
        end_key = utc_key(end)
        tzinfo = start.tzinfo
        with self._connect() as conn:
            for batch in batched(normalize_symbols(symbols), 800):
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT symbol, timestamp_utc, open, high, low, close
                    FROM minute_bars
                    WHERE feed = ?
                      AND adjustment = ?
                      AND timestamp_utc >= ?
                      AND timestamp_utc < ?
                      AND symbol IN ({placeholders})
                    ORDER BY symbol, timestamp_utc
                    """,
                    [feed.lower(), adjustment, start_key, end_key, *batch],
                ).fetchall()
                for symbol, timestamp_utc, open_price, high, low, close in rows:
                    timestamp = datetime.fromisoformat(timestamp_utc).astimezone(tzinfo)
                    out.setdefault(symbol, []).append(MinuteBar(symbol, timestamp, open_price, high, low, close))
        return out

    def save_minute_bars(
        self,
        bars_by_symbol: dict[str, list[MinuteBar]],
        *,
        feed: str,
        range_start: datetime,
        range_end: datetime,
        covered_symbols: list[str] | None = None,
        adjustment: str = ADJUSTMENT_SPLIT,
    ) -> None:
        feed_key = feed.lower()
        symbols = normalize_symbols(covered_symbols or list(bars_by_symbol))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as conn:
            for symbol, bars in bars_by_symbol.items():
                alpaca_symbol = to_alpaca_symbol(symbol)
                conn.executemany(
                    """
                    INSERT INTO minute_bars(symbol, timestamp_utc, feed, adjustment, open, high, low, close, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timestamp_utc, feed, adjustment) DO UPDATE SET
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (
                            alpaca_symbol,
                            utc_key(bar.timestamp),
                            feed_key,
                            adjustment,
                            bar.open,
                            bar.high,
                            bar.low,
                            bar.close,
                            now,
                        )
                        for bar in bars
                    ],
                )
            self._mark_ranges(conn, "minute", symbols, utc_key(range_start), utc_key(range_end), feed_key, adjustment, now)

    def uncovered_symbols(
        self,
        kind: str,
        symbols: list[str],
        range_start: str,
        range_end: str,
        *,
        feed: str,
        adjustment: str = ADJUSTMENT_SPLIT,
    ) -> list[str]:
        normalized = normalize_symbols(symbols)
        if not normalized or range_start >= range_end:
            return []
        covered: set[str] = set()
        with self._connect() as conn:
            for batch in batched(normalized, 800):
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT symbol, start_key, end_key
                    FROM fetch_ranges
                    WHERE kind = ?
                      AND feed = ?
                      AND adjustment = ?
                      AND start_key < ?
                      AND end_key > ?
                      AND symbol IN ({placeholders})
                    ORDER BY symbol, start_key
                    """,
                    [kind, feed.lower(), adjustment, range_end, range_start, *batch],
                ).fetchall()
                ranges_by_symbol: dict[str, list[tuple[str, str]]] = {}
                for symbol, start_key, end_key in rows:
                    ranges_by_symbol.setdefault(symbol, []).append((start_key, end_key))
                for symbol, ranges in ranges_by_symbol.items():
                    if ranges_cover_request(ranges, range_start, range_end):
                        covered.add(symbol)
        return [symbol for symbol in normalized if symbol not in covered]

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS daily_bars (
                    symbol TEXT NOT NULL,
                    bar_date TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    vwap REAL,
                    transactions INTEGER,
                    timestamp_ms INTEGER,
                    ma5 REAL,
                    ma10 REAL,
                    ma20 REAL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, bar_date, feed, adjustment)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_bars_lookup
                    ON daily_bars(feed, adjustment, symbol, bar_date);

                CREATE TABLE IF NOT EXISTS minute_bars (
                    symbol TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, timestamp_utc, feed, adjustment)
                );
                CREATE INDEX IF NOT EXISTS idx_minute_bars_lookup
                    ON minute_bars(feed, adjustment, symbol, timestamp_utc);

                CREATE TABLE IF NOT EXISTS fetch_ranges (
                    kind TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    start_key TEXT NOT NULL,
                    end_key TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(kind, symbol, feed, adjustment, start_key, end_key)
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_ranges_lookup
                    ON fetch_ranges(kind, feed, adjustment, symbol, start_key, end_key);
                """
            )
            ensure_column(conn, "daily_bars", "volume", "REAL")
            ensure_column(conn, "daily_bars", "vwap", "REAL")
            ensure_column(conn, "daily_bars", "transactions", "INTEGER")
            ensure_column(conn, "daily_bars", "timestamp_ms", "INTEGER")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _mark_ranges(
        self,
        conn: sqlite3.Connection,
        kind: str,
        symbols: list[str],
        start_key: str,
        end_key: str,
        feed: str,
        adjustment: str,
        updated_at: str,
    ) -> None:
        conn.executemany(
            """
            INSERT INTO fetch_ranges(kind, symbol, feed, adjustment, start_key, end_key, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, symbol, feed, adjustment, start_key, end_key) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            [(kind, symbol, feed, adjustment, start_key, end_key, updated_at) for symbol in symbols],
        )

    def _recompute_daily_mas(self, conn: sqlite3.Connection, symbol: str, feed: str, adjustment: str) -> None:
        rows = conn.execute(
            """
            SELECT bar_date, close
            FROM daily_bars
            WHERE symbol = ? AND feed = ? AND adjustment = ?
            ORDER BY bar_date
            """,
            (symbol, feed, adjustment),
        ).fetchall()
        closes: list[float] = []
        updates: list[tuple[float | None, float | None, float | None, str, str, str, str]] = []
        for bar_date, close in rows:
            closes.append(close)
            ma5 = average_tail(closes, 5)
            ma10 = average_tail(closes, 10)
            ma20 = average_tail(closes, 20)
            updates.append((ma5, ma10, ma20, symbol, bar_date, feed, adjustment))
        conn.executemany(
            """
            UPDATE daily_bars
            SET ma5 = ?, ma10 = ?, ma20 = ?
            WHERE symbol = ? AND bar_date = ? AND feed = ? AND adjustment = ?
            """,
            updates,
        )


def normalize_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        alpaca_symbol = to_alpaca_symbol(symbol)
        if alpaca_symbol and alpaca_symbol not in seen:
            seen.add(alpaca_symbol)
            out.append(alpaca_symbol)
    return out


def utc_key(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def average_tail(values: list[float], size: int) -> float | None:
    if not values:
        return None
    tail = values[-size:]
    return sum(tail) / len(tail)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def ranges_cover_request(ranges: list[tuple[str, str]], range_start: str, range_end: str) -> bool:
    covered_until = ""
    for start_key, end_key in sorted(ranges):
        if end_key <= range_start:
            continue
        if start_key >= range_end:
            break
        if not covered_until:
            if start_key > range_start:
                return False
            covered_until = end_key
        elif start_key <= covered_until:
            covered_until = max(covered_until, end_key)
        else:
            return False
        if covered_until >= range_end:
            return True
    return False


def batched(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]
