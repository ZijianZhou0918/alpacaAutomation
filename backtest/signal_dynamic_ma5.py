from __future__ import annotations

from collections import Counter, defaultdict, deque
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from statistics import mean, median
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo
import csv
import json
import sqlite3

from alpaca_ma5_service.afterhours_high_low import (
    MinuteBar,
    minute_bar_from_alpaca,
    regular_session_bounds,
)
from alpaca_ma5_service.alpaca_connection import load_alpaca_credentials
from alpaca_ma5_service.market_time import is_buy_order_time
from alpaca_ma5_service.watchlist import to_alpaca_symbol
from backtest.data_cache import ADJUSTMENT_SPLIT, MarketDataCache


MARKET_TZ = ZoneInfo("America/New_York")
_PCT_EPSILON = 1e-12


def is_buy_order_timestamp(timestamp_utc: str) -> bool:
    timestamp_et = datetime.fromisoformat(timestamp_utc).astimezone(MARKET_TZ)
    return is_buy_order_time(timestamp_et)


@dataclass(frozen=True)
class SignalDynamicMa5Config:
    database_path: Path
    minute_cache_path: Path
    output_dir: Path
    start_date: date | None = None
    end_date: date | None = None
    min_signal_gain_pct: float = 0.10
    min_signal_body_pct: float = 0.10
    ma5_proximity_pct: float = 0.0
    min_intraday_drop_pct: float = 0.15
    profit_targets_pct: tuple[float, ...] = (0.05, 0.10, 0.15)
    stop_loss_pct: float = 0.10
    notional_per_trade: float = 10_000.0
    commission_per_order: float = 0.0
    slippage_bps: float = 0.0
    expected_feed: str = "sip"
    expected_adjustment: str = ADJUSTMENT_SPLIT
    minute_fetch_batch_size: int = 100
    progress_every_candidate_days: int = 25
    html_trade_row_limit: int = 2_000

    def __post_init__(self) -> None:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if not 0 < self.min_signal_gain_pct < 1:
            raise ValueError("min_signal_gain_pct must be between 0 and 1")
        if not 0 < self.min_signal_body_pct < 1:
            raise ValueError("min_signal_body_pct must be between 0 and 1")
        if not 0 <= self.ma5_proximity_pct < 1:
            raise ValueError("ma5_proximity_pct must be between 0 and 1")
        if not 0 < self.min_intraday_drop_pct < 1:
            raise ValueError("min_intraday_drop_pct must be between 0 and 1")
        if not 0 < self.stop_loss_pct < 1:
            raise ValueError("stop_loss_pct must be between 0 and 1")
        if not self.profit_targets_pct or any(
            target <= 0 for target in self.profit_targets_pct
        ):
            raise ValueError("profit_targets_pct must contain positive values")
        if tuple(sorted(self.profit_targets_pct)) != self.profit_targets_pct:
            raise ValueError("profit_targets_pct must be strictly ascending")
        if len(set(self.profit_targets_pct)) != len(self.profit_targets_pct):
            raise ValueError("profit_targets_pct must not contain duplicates")
        if self.notional_per_trade <= 0:
            raise ValueError("notional_per_trade must be positive")
        if self.commission_per_order < 0 or self.slippage_bps < 0:
            raise ValueError("cost settings must not be negative")
        if self.minute_fetch_batch_size <= 0:
            raise ValueError("minute_fetch_batch_size must be positive")


@dataclass(frozen=True)
class DailyDatasetMetadata:
    source: str
    feed: str
    timeframe: str
    adjustment: str
    start_date: date
    end_date: date
    status: str
    expected_sessions: int
    candidate_symbols: int
    completed_batches: int
    total_rows: int
    observed_symbols: int
    observed_sessions: int
    security_master_symbols: int
    minute_rows: int
    completed_at: str
    survivorship_bias_fully_eliminated: bool = False


@dataclass(frozen=True)
class SignalSnapshot:
    signal_date: str
    next_session_date: str
    signal_close: float
    signal_gain_pct: float
    signal_body_pct: float
    ma5: float
    ma10: float
    ma20: float
    previous_four_closes: tuple[float, float, float, float]


@dataclass(frozen=True)
class TradeCandidate:
    symbol: str
    signal: SignalSnapshot
    buy_date: str
    buy_day_open: float
    open_gain_pct: float


@dataclass(frozen=True)
class DailyScreenResult:
    metadata: DailyDatasetMetadata
    sessions: tuple[date, ...]
    candidates: tuple[TradeCandidate, ...]
    daily_rows_scanned: int
    symbols_scanned: int
    signal_days: int
    next_day_symbol_sessions: int
    positive_gap_days: int


@dataclass(frozen=True)
class MinuteRow:
    symbol: str
    timestamp_utc: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ExitFill:
    reason: str
    timestamp_utc: str
    quantity: float
    price: float


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    signal_date: str
    buy_date: str
    signal_close: float
    buy_day_open: float
    open_gain_pct: float
    signal_gain_pct: float
    signal_body_pct: float
    ma5: float
    ma10: float
    ma20: float
    trigger_timestamp_utc: str
    trigger_close: float
    trigger_dynamic_ma5: float
    trigger_distance_pct: float
    entry_timestamp_utc: str
    entry_price: float
    initial_quantity: float
    exit_timestamp_utc: str
    exit_reason: str
    gross_proceeds: float
    commissions: float
    pnl: float
    return_pct: float
    target_hits: tuple[float, ...]
    exit_fills: tuple[ExitFill, ...]


@dataclass(frozen=True)
class BacktestSummary:
    start_date: str
    end_date: str
    daily_rows_scanned: int
    symbols_scanned: int
    signal_days: int
    next_day_symbol_sessions: int
    positive_gap_days: int
    candidates_with_minute_bars: int
    candidates_without_minute_bars: int
    minute_rows_scanned: int
    dynamic_ma5_trigger_days: int
    trades: int
    wins: int
    losses: int
    flat: int
    win_rate: float
    average_return_pct: float
    median_return_pct: float
    sum_return_pct: float
    fixed_notional_total_pnl: float
    best_return_pct: float
    worst_return_pct: float
    exit_reason_counts: dict[str, int]
    target_hit_counts: dict[str, int]


@dataclass(frozen=True)
class SignalDynamicMa5Result:
    config: SignalDynamicMa5Config
    metadata: DailyDatasetMetadata
    summary: BacktestSummary
    trades: tuple[TradeRecord, ...]
    summary_json_path: Path | None = None
    trades_csv_path: Path | None = None
    html_report_path: Path | None = None


@dataclass
class _OpenPosition:
    candidate: TradeCandidate
    trigger_timestamp_utc: str
    trigger_close: float
    trigger_dynamic_ma5: float
    trigger_distance_pct: float
    entry_timestamp_utc: str
    entry_price: float
    initial_quantity: float
    remaining_quantity: float
    commissions: float
    gross_proceeds: float = 0.0
    target_index: int = 0
    target_hits: list[float] = field(default_factory=list)
    fills: list[ExitFill] = field(default_factory=list)


class SignalDynamicMa5Simulator:
    """Replay one independent fixed-notional trade candidate at a time."""

    def __init__(self, config: SignalDynamicMa5Config) -> None:
        self.config = config
        self.minute_rows_scanned = 0
        self.dynamic_ma5_trigger_days = 0
        self.trades: list[TradeRecord] = []

    def simulate_candidate(
        self,
        candidate: TradeCandidate,
        rows: Iterable[MinuteRow],
    ) -> None:
        ordered_rows = sorted(rows, key=lambda row: row.timestamp_utc)
        if not ordered_rows:
            return

        previous_close_sum = sum(candidate.signal.previous_four_closes)
        trigger_counted = False
        entry_completed = False
        pending_entry: tuple[str, float, float, float] | None = None
        position: _OpenPosition | None = None

        for row in ordered_rows:
            self.minute_rows_scanned += 1
            if (
                pending_entry is not None
                and position is None
                and not entry_completed
            ):
                (
                    trigger_timestamp,
                    trigger_close,
                    trigger_dynamic_ma5,
                    trigger_distance_pct,
                ) = pending_entry
                pending_entry = None
                position = self._open_position(
                    candidate,
                    row,
                    trigger_timestamp,
                    trigger_close,
                    trigger_dynamic_ma5,
                    trigger_distance_pct,
                )
                if position is not None:
                    entry_completed = True

            if position is not None:
                position = self._process_position_bar(position, row)

            if (
                position is None
                and not entry_completed
                and pending_entry is None
                and is_buy_order_timestamp(row.timestamp_utc)
            ):
                dynamic_ma5 = (previous_close_sum + row.close) / 5.0
                distance_pct = row.close / dynamic_ma5 - 1.0
                intraday_drop_pct = 1.0 - row.close / candidate.buy_day_open
                trigger_ceiling = dynamic_ma5 * (
                    1.0 + self.config.ma5_proximity_pct
                )
                if (
                    row.close <= trigger_ceiling
                    and intraday_drop_pct - self.config.min_intraday_drop_pct
                    > _PCT_EPSILON
                ):
                    if not trigger_counted:
                        trigger_counted = True
                        self.dynamic_ma5_trigger_days += 1
                    pending_entry = (
                        row.timestamp_utc,
                        row.close,
                        dynamic_ma5,
                        distance_pct,
                    )

        if position is not None:
            self._sell_remaining(
                position,
                raw_price=ordered_rows[-1].close,
                timestamp_utc=ordered_rows[-1].timestamp_utc,
                reason="market_close",
            )

    def _open_position(
        self,
        candidate: TradeCandidate,
        row: MinuteRow,
        trigger_timestamp_utc: str,
        trigger_close: float,
        trigger_dynamic_ma5: float,
        trigger_distance_pct: float,
    ) -> _OpenPosition | None:
        if not is_buy_order_timestamp(row.timestamp_utc) or row.open <= 0:
            return None
        entry_price = apply_buy_slippage(row.open, self.config.slippage_bps)
        entry_drop_pct = 1.0 - entry_price / candidate.buy_day_open
        if (
            entry_drop_pct - self.config.min_intraday_drop_pct
            <= _PCT_EPSILON
        ):
            return None
        quantity = self.config.notional_per_trade / entry_price
        return _OpenPosition(
            candidate=candidate,
            trigger_timestamp_utc=trigger_timestamp_utc,
            trigger_close=trigger_close,
            trigger_dynamic_ma5=trigger_dynamic_ma5,
            trigger_distance_pct=trigger_distance_pct,
            entry_timestamp_utc=row.timestamp_utc,
            entry_price=entry_price,
            initial_quantity=quantity,
            remaining_quantity=quantity,
            commissions=self.config.commission_per_order,
        )

    def _process_position_bar(
        self,
        position: _OpenPosition,
        row: MinuteRow,
    ) -> _OpenPosition | None:
        stop_price = position.entry_price * (1.0 - self.config.stop_loss_pct)
        if row.low <= stop_price:
            raw_fill = min(row.open, stop_price) if row.open <= stop_price else stop_price
            self._sell_remaining(
                position,
                raw_price=raw_fill,
                timestamp_utc=row.timestamp_utc,
                reason="stop_loss",
            )
            return None

        while position.target_index < len(self.config.profit_targets_pct):
            target_pct = self.config.profit_targets_pct[position.target_index]
            target_price = position.entry_price * (1.0 + target_pct)
            if row.high < target_price:
                break
            raw_fill = max(row.open, target_price) if row.open >= target_price else target_price
            quantity = min(
                position.initial_quantity / len(self.config.profit_targets_pct),
                position.remaining_quantity,
            )
            self._sell(
                position,
                quantity=quantity,
                raw_price=raw_fill,
                timestamp_utc=row.timestamp_utc,
                reason=f"take_profit_{target_pct:.4f}",
            )
            position.target_hits.append(target_pct)
            position.target_index += 1
            if position.remaining_quantity <= position.initial_quantity * 1e-10:
                self._complete_position(position, "targets_complete")
                return None
        return position

    def _sell_remaining(
        self,
        position: _OpenPosition,
        *,
        raw_price: float,
        timestamp_utc: str,
        reason: str,
    ) -> None:
        self._sell(
            position,
            quantity=position.remaining_quantity,
            raw_price=raw_price,
            timestamp_utc=timestamp_utc,
            reason=reason,
        )
        self._complete_position(position, reason)

    def _sell(
        self,
        position: _OpenPosition,
        *,
        quantity: float,
        raw_price: float,
        timestamp_utc: str,
        reason: str,
    ) -> None:
        if quantity <= 0:
            return
        quantity = min(quantity, position.remaining_quantity)
        fill_price = apply_sell_slippage(raw_price, self.config.slippage_bps)
        position.remaining_quantity -= quantity
        position.gross_proceeds += quantity * fill_price
        position.commissions += self.config.commission_per_order
        position.fills.append(
            ExitFill(
                reason=reason,
                timestamp_utc=timestamp_utc,
                quantity=quantity,
                price=fill_price,
            )
        )

    def _complete_position(self, position: _OpenPosition, exit_reason: str) -> None:
        if not position.fills:
            return
        invested = position.entry_price * position.initial_quantity
        signal = position.candidate.signal
        pnl = position.gross_proceeds - invested - position.commissions
        self.trades.append(
            TradeRecord(
                symbol=position.candidate.symbol,
                signal_date=signal.signal_date,
                buy_date=position.candidate.buy_date,
                signal_close=signal.signal_close,
                buy_day_open=position.candidate.buy_day_open,
                open_gain_pct=position.candidate.open_gain_pct,
                signal_gain_pct=signal.signal_gain_pct,
                signal_body_pct=signal.signal_body_pct,
                ma5=signal.ma5,
                ma10=signal.ma10,
                ma20=signal.ma20,
                trigger_timestamp_utc=position.trigger_timestamp_utc,
                trigger_close=position.trigger_close,
                trigger_dynamic_ma5=position.trigger_dynamic_ma5,
                trigger_distance_pct=position.trigger_distance_pct,
                entry_timestamp_utc=position.entry_timestamp_utc,
                entry_price=position.entry_price,
                initial_quantity=position.initial_quantity,
                exit_timestamp_utc=position.fills[-1].timestamp_utc,
                exit_reason=exit_reason,
                gross_proceeds=position.gross_proceeds,
                commissions=position.commissions,
                pnl=pnl,
                return_pct=pnl / invested,
                target_hits=tuple(position.target_hits),
                exit_fills=tuple(position.fills),
            )
        )


class StrictAlpacaMinuteFetcher:
    """Fetch the configured feed without silently mixing in an IEX fallback."""

    def __init__(self, *, feed: str, batch_size: int) -> None:
        self.feed = feed.lower()
        self.batch_size = batch_size
        self._client = None

    def __call__(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[MinuteBar]]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if not symbols or end <= start:
            return {}
        if self._client is None:
            api_key, secret_key = load_alpaca_credentials()
            self._client = StockHistoricalDataClient(api_key, secret_key)

        normalized = [to_alpaca_symbol(symbol) for symbol in symbols]
        bars_by_symbol: dict[str, list[MinuteBar]] = {}
        for offset in range(0, len(normalized), self.batch_size):
            batch = normalized[offset : offset + self.batch_size]
            try:
                raw_bars = self._client.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=batch,
                        timeframe=TimeFrame.Minute,
                        start=start,
                        end=end,
                        adjustment=Adjustment.SPLIT,
                        feed=DataFeed(self.feed),
                    )
                ).data
            except Exception as exc:
                raise RuntimeError(
                    f"{self.feed.upper()} 1-minute fetch failed for "
                    f"{batch[0]}..{batch[-1]} on {start.date()}"
                ) from exc
            for symbol, bars in raw_bars.items():
                parsed = [
                    minute_bar_from_alpaca(symbol.upper(), bar, start.tzinfo)
                    for bar in bars
                ]
                bars_by_symbol[symbol.upper()] = [
                    bar for bar in parsed if start <= bar.timestamp < end
                ]
        return bars_by_symbol


MinuteFetcher = Callable[
    [list[str], datetime, datetime],
    dict[str, list[MinuteBar]],
]


def run_signal_dynamic_ma5_backtest(
    config: SignalDynamicMa5Config,
    *,
    progress: Callable[[str], None] | None = print,
    minute_fetcher: MinuteFetcher | None = None,
) -> SignalDynamicMa5Result:
    metadata, sessions = inspect_daily_database(config.database_path)
    validate_daily_dataset(config, metadata, sessions)
    effective_start = config.start_date or metadata.start_date
    effective_end = config.end_date or metadata.end_date
    if effective_start < metadata.start_date or effective_end > metadata.end_date:
        raise ValueError(
            f"Requested range {effective_start} -> {effective_end} exceeds dataset "
            f"{metadata.start_date} -> {metadata.end_date}"
        )
    effective_config = SignalDynamicMa5Config(
        **{
            **asdict(config),
            "database_path": config.database_path,
            "minute_cache_path": config.minute_cache_path,
            "output_dir": config.output_dir,
            "start_date": effective_start,
            "end_date": effective_end,
        }
    )

    if progress:
        progress("Screening the complete local daily dataset...")
    screen = screen_daily_candidates(effective_config, metadata, sessions)
    if progress:
        progress(
            f"Daily screen complete: rows={screen.daily_rows_scanned:,}; "
            f"symbols={screen.symbols_scanned:,}; signals={screen.signal_days:,}; "
            f"positive-gap candidates={len(screen.candidates):,}"
        )

    simulator = SignalDynamicMa5Simulator(effective_config)
    candidates_with_bars, candidates_without_bars = fetch_and_simulate_candidates(
        effective_config,
        screen.candidates,
        simulator,
        progress=progress,
        minute_fetcher=minute_fetcher,
    )
    summary = build_summary(
        config=effective_config,
        screen=screen,
        simulator=simulator,
        candidates_with_minute_bars=candidates_with_bars,
        candidates_without_minute_bars=candidates_without_bars,
    )
    result = SignalDynamicMa5Result(
        config=effective_config,
        metadata=metadata,
        summary=summary,
        trades=tuple(simulator.trades),
    )
    return write_result_files(result)


def inspect_daily_database(
    path: Path,
) -> tuple[DailyDatasetMetadata, tuple[date, ...]]:
    if not path.exists():
        raise FileNotFoundError(f"Daily database not found: {path}")
    with closing(open_readonly_database(path)) as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'daily_dataset_metadata',
                      'daily_bars',
                      'minute_bars',
                      'security_master'
                  )
                """
            )
        }
        required = {
            "daily_dataset_metadata",
            "daily_bars",
            "minute_bars",
            "security_master",
        }
        if table_names != required:
            missing = ", ".join(sorted(required - table_names))
            raise RuntimeError(f"Managed daily dataset is incomplete; missing: {missing}")
        row = conn.execute(
            """
            SELECT
                source, feed, timeframe, adjustment, start_date, end_date,
                status, expected_sessions, candidate_symbols,
                completed_batches, total_rows, completed_at
            FROM daily_dataset_metadata
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("daily_dataset_metadata row id=1 is missing")
        sessions = tuple(
            date.fromisoformat(value)
            for (value,) in conn.execute(
                """
                SELECT DISTINCT bar_date
                FROM daily_bars
                WHERE feed = ? AND adjustment = ?
                ORDER BY bar_date
                """,
                (str(row[1]).lower(), str(row[3]).lower()),
            )
        )
        observed_symbols = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT symbol)
                FROM daily_bars
                WHERE feed = ? AND adjustment = ?
                """,
                (str(row[1]).lower(), str(row[3]).lower()),
            ).fetchone()[0]
        )
        security_master_symbols = int(
            conn.execute("SELECT COUNT(*) FROM security_master").fetchone()[0]
        )
        minute_rows = int(conn.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0])
    return (
        DailyDatasetMetadata(
            source=str(row[0]),
            feed=str(row[1]),
            timeframe=str(row[2]),
            adjustment=str(row[3]),
            start_date=date.fromisoformat(str(row[4])),
            end_date=date.fromisoformat(str(row[5])),
            status=str(row[6]),
            expected_sessions=int(row[7]),
            candidate_symbols=int(row[8]),
            completed_batches=int(row[9]),
            total_rows=int(row[10]),
            observed_symbols=observed_symbols,
            observed_sessions=len(sessions),
            security_master_symbols=security_master_symbols,
            minute_rows=minute_rows,
            completed_at=str(row[11] or ""),
        ),
        sessions,
    )


def validate_daily_dataset(
    config: SignalDynamicMa5Config,
    metadata: DailyDatasetMetadata,
    sessions: Sequence[date],
) -> None:
    problems: list[str] = []
    if metadata.status != "complete":
        problems.append(f"status={metadata.status!r}")
    if metadata.timeframe.lower() != "1day":
        problems.append(f"timeframe={metadata.timeframe!r}")
    if metadata.feed.lower() != config.expected_feed.lower():
        problems.append(f"feed={metadata.feed!r}")
    if metadata.adjustment.lower() != config.expected_adjustment.lower():
        problems.append(f"adjustment={metadata.adjustment!r}")
    if metadata.expected_sessions != len(sessions):
        problems.append(
            f"expected_sessions={metadata.expected_sessions}, observed={len(sessions)}"
        )
    if metadata.security_master_symbols != metadata.candidate_symbols:
        problems.append(
            "security_master count does not match candidate_symbols "
            f"({metadata.security_master_symbols} != {metadata.candidate_symbols})"
        )
    if metadata.total_rows <= 0 or metadata.observed_symbols <= 0:
        problems.append("daily dataset is empty")
    if metadata.minute_rows != 0:
        problems.append(f"formal daily database contains {metadata.minute_rows} minute rows")
    if not sessions:
        problems.append("no daily sessions found")
    elif sessions[0] < metadata.start_date or sessions[-1] > metadata.end_date:
        problems.append("observed session range exceeds metadata range")
    if problems:
        raise RuntimeError("Daily dataset reliability gate failed: " + "; ".join(problems))


def screen_daily_candidates(
    config: SignalDynamicMa5Config,
    metadata: DailyDatasetMetadata,
    sessions: Sequence[date],
) -> DailyScreenResult:
    start_text = (config.start_date or metadata.start_date).isoformat()
    end_text = (config.end_date or metadata.end_date).isoformat()
    normalized_sessions = [value.isoformat() for value in sessions]
    next_session_by_date = {
        current: following
        for current, following in zip(normalized_sessions, normalized_sessions[1:])
    }

    daily_rows_scanned = 0
    symbols_scanned = 0
    signal_days = 0
    next_day_symbol_sessions = 0
    positive_gap_days = 0
    candidates: list[TradeCandidate] = []
    current_symbol = ""
    closes: deque[float] = deque(maxlen=20)
    pending_signal: SignalSnapshot | None = None

    with closing(open_readonly_database(config.database_path)) as conn:
        cursor = conn.execute(
            """
            SELECT symbol, bar_date, open, close
            FROM daily_bars
            WHERE feed = ?
              AND adjustment = ?
              AND bar_date <= ?
            ORDER BY symbol, bar_date
            """,
            (metadata.feed.lower(), metadata.adjustment.lower(), end_text),
        )
        for symbol, bar_date, open_price, close_price in cursor:
            daily_rows_scanned += 1
            symbol = str(symbol)
            bar_date = str(bar_date)
            open_price = float(open_price)
            close_price = float(close_price)
            if symbol != current_symbol:
                current_symbol = symbol
                symbols_scanned += 1
                closes.clear()
                pending_signal = None

            if pending_signal is not None:
                if bar_date == pending_signal.next_session_date:
                    if start_text <= bar_date <= end_text:
                        next_day_symbol_sessions += 1
                        open_gain_pct = open_price / pending_signal.signal_close - 1.0
                        if open_gain_pct > _PCT_EPSILON:
                            positive_gap_days += 1
                            candidates.append(
                                TradeCandidate(
                                    symbol=symbol,
                                    signal=pending_signal,
                                    buy_date=bar_date,
                                    buy_day_open=open_price,
                                    open_gain_pct=open_gain_pct,
                                )
                            )
                    pending_signal = None
                elif bar_date > pending_signal.next_session_date:
                    pending_signal = None

            previous_close = closes[-1] if closes else None
            closes.append(close_price)
            if (
                previous_close is None
                or len(closes) < 20
                or open_price <= 0
                or previous_close <= 0
            ):
                continue
            ma5 = mean(list(closes)[-5:])
            ma10 = mean(list(closes)[-10:])
            ma20 = mean(closes)
            gain_pct = close_price / previous_close - 1.0
            body_pct = close_price / open_price - 1.0
            next_session = next_session_by_date.get(bar_date)
            if (
                next_session is None
                or not start_text <= next_session <= end_text
                or not (
                    ma5 > ma10 > ma20
                    and gain_pct - config.min_signal_gain_pct > _PCT_EPSILON
                    and body_pct - config.min_signal_body_pct > _PCT_EPSILON
                    and close_price > open_price
                )
            ):
                continue
            signal_days += 1
            previous_four = tuple(list(closes)[-4:])
            pending_signal = SignalSnapshot(
                signal_date=bar_date,
                next_session_date=next_session,
                signal_close=close_price,
                signal_gain_pct=gain_pct,
                signal_body_pct=body_pct,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                previous_four_closes=previous_four,  # type: ignore[arg-type]
            )

    return DailyScreenResult(
        metadata=metadata,
        sessions=tuple(sessions),
        candidates=tuple(candidates),
        daily_rows_scanned=daily_rows_scanned,
        symbols_scanned=symbols_scanned,
        signal_days=signal_days,
        next_day_symbol_sessions=next_day_symbol_sessions,
        positive_gap_days=positive_gap_days,
    )


def fetch_and_simulate_candidates(
    config: SignalDynamicMa5Config,
    candidates: Sequence[TradeCandidate],
    simulator: SignalDynamicMa5Simulator,
    *,
    progress: Callable[[str], None] | None,
    minute_fetcher: MinuteFetcher | None,
) -> tuple[int, int]:
    if not candidates:
        return 0, 0
    grouped: dict[str, list[TradeCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.buy_date].append(candidate)
    cache = MarketDataCache(config.minute_cache_path)
    fetcher = minute_fetcher or StrictAlpacaMinuteFetcher(
        feed=config.expected_feed,
        batch_size=config.minute_fetch_batch_size,
    )
    candidates_with_bars = 0
    candidates_without_bars = 0
    total_days = len(grouped)

    for day_index, (buy_date, day_candidates) in enumerate(
        sorted(grouped.items()),
        start=1,
    ):
        session_date = date.fromisoformat(buy_date)
        start, end = regular_session_bounds(session_date, MARKET_TZ)
        symbols = sorted({to_alpaca_symbol(item.symbol) for item in day_candidates})
        start_key = start.astimezone(UTC).isoformat(timespec="seconds")
        end_key = end.astimezone(UTC).isoformat(timespec="seconds")
        missing = cache.uncovered_symbols(
            "minute",
            symbols,
            start_key,
            end_key,
            feed=config.expected_feed,
            adjustment=config.expected_adjustment,
        )
        if missing:
            if progress:
                progress(
                    f"Minute cache miss {buy_date}: {len(missing):,} symbols "
                    f"({day_index}/{total_days})"
                )
            fetched = fetcher(missing, start, end)
            cache.save_minute_bars(
                fetched,
                feed=config.expected_feed,
                range_start=start,
                range_end=end,
                covered_symbols=missing,
                adjustment=config.expected_adjustment,
            )
        loaded = cache.load_minute_bars(
            symbols,
            start,
            end,
            feed=config.expected_feed,
            adjustment=config.expected_adjustment,
        )
        for candidate in day_candidates:
            symbol_key = to_alpaca_symbol(candidate.symbol)
            minute_bars = loaded.get(symbol_key, [])
            if minute_bars:
                candidates_with_bars += 1
            else:
                candidates_without_bars += 1
            simulator.simulate_candidate(
                candidate,
                (
                    MinuteRow(
                        symbol=candidate.symbol,
                        timestamp_utc=bar.timestamp.astimezone(UTC).isoformat(
                            timespec="seconds"
                        ),
                        open=float(bar.open),
                        high=float(bar.high),
                        low=float(bar.low),
                        close=float(bar.close),
                    )
                    for bar in minute_bars
                ),
            )
        if (
            progress
            and config.progress_every_candidate_days > 0
            and (
                day_index % config.progress_every_candidate_days == 0
                or day_index == total_days
            )
        ):
            progress(
                f"Minute replay {day_index}/{total_days} days; "
                f"rows={simulator.minute_rows_scanned:,}; "
                f"triggers={simulator.dynamic_ma5_trigger_days:,}; "
                f"trades={len(simulator.trades):,}"
            )
    return candidates_with_bars, candidates_without_bars


def open_readonly_database(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )


def build_summary(
    *,
    config: SignalDynamicMa5Config,
    screen: DailyScreenResult,
    simulator: SignalDynamicMa5Simulator,
    candidates_with_minute_bars: int,
    candidates_without_minute_bars: int,
) -> BacktestSummary:
    returns = [trade.return_pct for trade in simulator.trades]
    wins = sum(value > 1e-12 for value in returns)
    losses = sum(value < -1e-12 for value in returns)
    flat = len(returns) - wins - losses
    exit_reason_counts = Counter(trade.exit_reason for trade in simulator.trades)
    target_hit_counts: Counter[str] = Counter()
    for trade in simulator.trades:
        for target in trade.target_hits:
            target_hit_counts[f"{target:.2%}"] += 1
    return BacktestSummary(
        start_date=(config.start_date or screen.metadata.start_date).isoformat(),
        end_date=(config.end_date or screen.metadata.end_date).isoformat(),
        daily_rows_scanned=screen.daily_rows_scanned,
        symbols_scanned=screen.symbols_scanned,
        signal_days=screen.signal_days,
        next_day_symbol_sessions=screen.next_day_symbol_sessions,
        positive_gap_days=screen.positive_gap_days,
        candidates_with_minute_bars=candidates_with_minute_bars,
        candidates_without_minute_bars=candidates_without_minute_bars,
        minute_rows_scanned=simulator.minute_rows_scanned,
        dynamic_ma5_trigger_days=simulator.dynamic_ma5_trigger_days,
        trades=len(simulator.trades),
        wins=wins,
        losses=losses,
        flat=flat,
        win_rate=wins / len(returns) if returns else 0.0,
        average_return_pct=mean(returns) if returns else 0.0,
        median_return_pct=median(returns) if returns else 0.0,
        sum_return_pct=sum(returns),
        fixed_notional_total_pnl=sum(trade.pnl for trade in simulator.trades),
        best_return_pct=max(returns, default=0.0),
        worst_return_pct=min(returns, default=0.0),
        exit_reason_counts=dict(sorted(exit_reason_counts.items())),
        target_hit_counts=dict(sorted(target_hit_counts.items())),
    )


def apply_buy_slippage(price: float, slippage_bps: float) -> float:
    return price * (1.0 + slippage_bps / 10_000.0)


def apply_sell_slippage(price: float, slippage_bps: float) -> float:
    return price * (1.0 - slippage_bps / 10_000.0)


def write_result_files(result: SignalDynamicMa5Result) -> SignalDynamicMa5Result:
    output_dir = result.config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_json_path = output_dir / "signal_dynamic_ma5_summary.json"
    trades_csv_path = output_dir / "signal_dynamic_ma5_trades.csv"
    html_report_path = output_dir / "signal_dynamic_ma5_report.html"

    payload = {
        "strategy": strategy_description(result.config),
        "config": config_to_json(result.config),
        "dataset": metadata_to_json(result.metadata),
        "summary": asdict(result.summary),
    }
    summary_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_trades_csv(trades_csv_path, result.trades)
    html_report_path.write_text(render_html_report(result), encoding="utf-8")
    return SignalDynamicMa5Result(
        config=result.config,
        metadata=result.metadata,
        summary=result.summary,
        trades=result.trades,
        summary_json_path=summary_json_path,
        trades_csv_path=trades_csv_path,
        html_report_path=html_report_path,
    )


def write_trades_csv(path: Path, trades: Sequence[TradeRecord]) -> None:
    fields = [
        "symbol",
        "signal_date",
        "buy_date",
        "signal_close",
        "buy_day_open",
        "open_gain_pct",
        "signal_gain_pct",
        "signal_body_pct",
        "ma5",
        "ma10",
        "ma20",
        "trigger_timestamp_utc",
        "trigger_close",
        "trigger_dynamic_ma5",
        "trigger_distance_pct",
        "entry_timestamp_utc",
        "entry_price",
        "initial_quantity",
        "exit_timestamp_utc",
        "exit_reason",
        "gross_proceeds",
        "commissions",
        "pnl",
        "return_pct",
        "target_hits",
        "exit_fills",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            row = asdict(trade)
            row["target_hits"] = json.dumps(trade.target_hits)
            row["exit_fills"] = json.dumps(
                [asdict(fill) for fill in trade.exit_fills],
                ensure_ascii=False,
            )
            writer.writerow(row)


def render_html_report(result: SignalDynamicMa5Result) -> str:
    summary = result.summary
    rows = []
    for trade in result.trades[: result.config.html_trade_row_limit]:
        entry_drop_pct = 1.0 - trade.entry_price / trade.buy_day_open
        rows.append(
            "<tr>"
            f"<td>{escape(trade.symbol)}</td>"
            f"<td>{trade.signal_date}</td>"
            f"<td>{trade.buy_date}</td>"
            f"<td>{trade.open_gain_pct:.2%}</td>"
            f"<td>{trade.trigger_distance_pct:.2%}</td>"
            f"<td>{entry_drop_pct:.2%}</td>"
            f"<td>{trade.entry_price:.4f}</td>"
            f"<td>{trade.return_pct:.2%}</td>"
            f"<td>{escape(trade.exit_reason)}</td>"
            "</tr>"
        )
    caveat = (
        "每笔候选独立使用固定名义本金；总盈亏不是受资金容量、并发持仓数量约束的组合收益。"
        "股票池包含重建时仍可从目录识别的 active/inactive 普通股，但不能完全消除幸存者偏差。"
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>动态 MA5 回测</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#1f2937}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.card{{border:1px solid #d1d5db;border-radius:8px;padding:14px}}
table{{border-collapse:collapse;width:100%;margin-top:18px}}
th,td{{border:1px solid #d1d5db;padding:7px;text-align:right}}
th:first-child,td:first-child{{text-align:left}}
.note{{background:#fff7ed;border-left:4px solid #f97316;padding:12px}}
</style></head><body>
<h1>信号日强势 + 次日动态 MA5 买入回测</h1>
<p>{escape(strategy_description(result.config))}</p>
<div class="grid">
<div class="card">信号数<br><strong>{summary.signal_days:,}</strong></div>
<div class="card">正开盘候选<br><strong>{summary.positive_gap_days:,}</strong></div>
<div class="card">MA5 + 跌幅触发<br><strong>{summary.dynamic_ma5_trigger_days:,}</strong></div>
<div class="card">成交笔数<br><strong>{summary.trades:,}</strong></div>
<div class="card">胜率<br><strong>{summary.win_rate:.2%}</strong></div>
<div class="card">平均单笔<br><strong>{summary.average_return_pct:.2%}</strong></div>
<div class="card">固定名义总盈亏<br><strong>${summary.fixed_notional_total_pnl:,.2f}</strong></div>
</div>
<p class="note">{escape(caveat)}</p>
<h2>成交明细</h2>
<table><thead><tr><th>代码</th><th>信号日</th><th>买入日</th><th>开盘涨幅</th>
<th>触发距 MA5</th><th>买入时较开盘跌幅</th><th>入场价</th><th>收益</th><th>退出</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>"""


def strategy_description(config: SignalDynamicMa5Config) -> str:
    targets = "/".join(f"{value:.0%}" for value in config.profit_targets_pct)
    return (
        "信号日要求 MA5 > MA10 > MA20、相对前收涨幅严格大于 "
        f"{config.min_signal_gain_pct:.0%}、阳线实体严格大于 "
        f"{config.min_signal_body_pct:.0%}；下一交易日开盘相对信号日收盘严格上涨。"
        "动态 MA5 = 前四个已完成交易日收盘价与当前已完成 1 分钟 K 线收盘价的均值；"
        "当前已完成 1 分钟 K 线收盘价小于或等于动态 MA5，且相对买入日开盘价跌幅严格大于 "
        f"{config.min_intraday_drop_pct:.0%} 时触发；下一根 1 分钟 K 线开盘价相对买入日开盘价"
        f"跌幅仍须严格大于 {config.min_intraday_drop_pct:.0%} 才买入，"
        "且实际成交时间必须满足 09:30 <= t < 12:00 ET。"
        f"盈利 {targets} 各卖出原始仓位 1/{len(config.profit_targets_pct)}，"
        f"亏损 {config.stop_loss_pct:.0%} 清仓，常规盘最后一分钟收盘清仓；"
        "同一分钟同时触发止损和止盈时按止损优先。"
    )


def config_to_json(config: SignalDynamicMa5Config) -> dict[str, object]:
    payload = asdict(config)
    payload["database_path"] = str(config.database_path)
    payload["minute_cache_path"] = str(config.minute_cache_path)
    payload["output_dir"] = str(config.output_dir)
    payload["start_date"] = config.start_date.isoformat() if config.start_date else None
    payload["end_date"] = config.end_date.isoformat() if config.end_date else None
    return payload


def metadata_to_json(metadata: DailyDatasetMetadata) -> dict[str, object]:
    payload = asdict(metadata)
    payload["start_date"] = metadata.start_date.isoformat()
    payload["end_date"] = metadata.end_date.isoformat()
    return payload
