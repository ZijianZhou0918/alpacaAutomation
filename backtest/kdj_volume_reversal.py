from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class KdjVolumeReversalConfig:
    start_date: date
    end_date: date
    notional_per_trade: float = 2_500.0
    kdj_period: int = 81
    high_low_ratio: float = 1.5
    high_previous_close_ratio: float = 1.25
    maximum_close_gain: float = 0.20
    volume_ratio: float = 100.0
    buy_j_below: float = 0.0
    sell_j_above: float = 100.0
    require_no_ma5_signal: bool = True


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    signal_date: str
    entry_date: str
    entry_price: float
    entry_j: float
    exit_signal_date: str | None
    exit_date: str
    exit_price: float
    exit_j: float | None
    exit_reason: str
    quantity: int
    invested: float
    pnl: float
    return_pct: float


@dataclass(frozen=True)
class KdjVolumeReversalResult:
    config: KdjVolumeReversalConfig
    data_start: str
    data_end: str
    raw_signal_count: int
    entered_trade_count: int
    ignored_signal_while_holding: int
    skipped_no_next_bar: int
    closed_trade_count: int
    marked_open_trade_count: int
    winning_trade_count: int
    losing_trade_count: int
    flat_trade_count: int
    invested_total: float
    pnl_total: float
    aggregate_return_pct: float
    average_trade_return_pct: float
    median_trade_return_pct: float
    trades: tuple[BacktestTrade, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["config"]["start_date"] = self.config.start_date.isoformat()
        value["config"]["end_date"] = self.config.end_date.isoformat()
        return value


@dataclass(frozen=True)
class _Bar:
    bar_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    ma5: float | None


@dataclass(frozen=True)
class _CalculatedBar:
    bar: _Bar
    previous_close: float | None
    previous_volume: float | None
    j: float | None
    buy_signal: bool


def run_kdj_volume_reversal_backtest(
    database: Path,
    config: KdjVolumeReversalConfig,
) -> KdjVolumeReversalResult:
    if config.start_date > config.end_date:
        raise ValueError("start_date must not be after end_date")
    if config.kdj_period < 2:
        raise ValueError("kdj_period must be at least 2")
    if config.notional_per_trade <= 0:
        raise ValueError("notional_per_trade must be positive")

    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    try:
        metadata = connection.execute(
            """
            SELECT status, feed, timeframe, adjustment, start_date, end_date
            FROM daily_dataset_metadata
            """
        ).fetchone()
        if metadata is None:
            raise RuntimeError("official daily dataset metadata is missing")
        status, feed, timeframe, adjustment, data_start, data_end = metadata
        if (status, feed, timeframe, adjustment) != ("complete", "sip", "1Day", "split"):
            raise RuntimeError(
                "official daily dataset must be complete Alpaca SIP/1Day/split data"
            )
        if config.start_date.isoformat() < data_start or config.end_date.isoformat() > data_end:
            raise RuntimeError(
                f"requested window {config.start_date}..{config.end_date} is outside "
                f"dataset coverage {data_start}..{data_end}"
            )
        if connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] != 0:
            raise RuntimeError("official daily database unexpectedly contains minute bars")

        rows = connection.execute(
            """
            SELECT symbol, bar_date, open, high, low, close, volume, ma5
            FROM daily_bars
            WHERE feed = 'sip' AND adjustment = 'split' AND bar_date <= ?
            ORDER BY symbol, bar_date
            """,
            (config.end_date.isoformat(),),
        )
        trades, raw_signals, ignored, skipped = _replay_symbols(rows, config)
    finally:
        connection.close()

    returns = sorted(trade.return_pct for trade in trades)
    invested_total = sum(trade.invested for trade in trades)
    pnl_total = sum(trade.pnl for trade in trades)
    median = _median(returns)
    return KdjVolumeReversalResult(
        config=config,
        data_start=data_start,
        data_end=data_end,
        raw_signal_count=raw_signals,
        entered_trade_count=len(trades),
        ignored_signal_while_holding=ignored,
        skipped_no_next_bar=skipped,
        closed_trade_count=sum(t.exit_reason == "J_ABOVE_100" for t in trades),
        marked_open_trade_count=sum(t.exit_reason == "END_OF_WINDOW_MARK" for t in trades),
        winning_trade_count=sum(t.pnl > 0 for t in trades),
        losing_trade_count=sum(t.pnl < 0 for t in trades),
        flat_trade_count=sum(t.pnl == 0 for t in trades),
        invested_total=invested_total,
        pnl_total=pnl_total,
        aggregate_return_pct=(pnl_total / invested_total * 100.0) if invested_total else 0.0,
        average_trade_return_pct=(sum(returns) / len(returns)) if returns else 0.0,
        median_trade_return_pct=median,
        trades=tuple(trades),
    )


def write_backtest_outputs(result: KdjVolumeReversalResult, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"kdj_volume_reversal_{result.config.start_date}_{result.config.end_date}"
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}_trades.csv"
    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fields = list(BacktestTrade.__dataclass_fields__)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in result.trades)
    return json_path, csv_path


def _replay_symbols(
    rows: Iterable[tuple[object, ...]],
    config: KdjVolumeReversalConfig,
) -> tuple[list[BacktestTrade], int, int, int]:
    trades: list[BacktestTrade] = []
    raw_signals = 0
    ignored = 0
    skipped = 0
    symbol = None
    bars: list[_Bar] = []
    for row in rows:
        row_symbol = str(row[0])
        if symbol is not None and row_symbol != symbol:
            symbol_trades, symbol_signals, symbol_ignored, symbol_skipped = _replay_symbol(symbol, bars, config)
            trades.extend(symbol_trades)
            raw_signals += symbol_signals
            ignored += symbol_ignored
            skipped += symbol_skipped
            bars = []
        symbol = row_symbol
        bars.append(
            _Bar(
                bar_date=str(row[1]),
                open=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                close=float(row[5]),
                volume=None if row[6] is None else float(row[6]),
                ma5=None if row[7] is None else float(row[7]),
            )
        )
    if symbol is not None:
        symbol_trades, symbol_signals, symbol_ignored, symbol_skipped = _replay_symbol(symbol, bars, config)
        trades.extend(symbol_trades)
        raw_signals += symbol_signals
        ignored += symbol_ignored
        skipped += symbol_skipped
    return trades, raw_signals, ignored, skipped


def _replay_symbol(
    symbol: str,
    bars: list[_Bar],
    config: KdjVolumeReversalConfig,
) -> tuple[list[BacktestTrade], int, int, int]:
    calculated = _calculate_bars(bars, config)
    trades: list[BacktestTrade] = []
    raw_signals = sum(item.buy_signal for item in calculated)
    ignored = 0
    skipped = 0
    position: tuple[_CalculatedBar, _Bar] | None = None

    for index, item in enumerate(calculated):
        if position is not None and item.j is not None and item.j > config.sell_j_above:
            if index + 1 < len(calculated) and calculated[index + 1].bar.bar_date <= config.end_date.isoformat():
                signal, entry_bar = position
                exit_bar = calculated[index + 1].bar
                trades.append(_make_trade(symbol, signal, entry_bar, item, exit_bar, "J_ABOVE_100", config))
                position = None
                continue
        if position is not None and item.buy_signal:
            ignored += 1
        elif position is None and item.buy_signal:
            if index + 1 >= len(calculated) or calculated[index + 1].bar.bar_date > config.end_date.isoformat():
                skipped += 1
            else:
                position = (item, calculated[index + 1].bar)

    if position is not None:
        signal, entry_bar = position
        last_bar = next(item.bar for item in reversed(calculated) if item.bar.bar_date <= config.end_date.isoformat())
        trades.append(_make_trade(symbol, signal, entry_bar, None, last_bar, "END_OF_WINDOW_MARK", config))
    return trades, raw_signals, ignored, skipped


def _calculate_bars(
    bars: list[_Bar],
    config: KdjVolumeReversalConfig,
) -> list[_CalculatedBar]:
    lows: deque[float] = deque(maxlen=config.kdj_period)
    highs: deque[float] = deque(maxlen=config.kdj_period)
    previous_close = None
    previous_volume = None
    k = 50.0
    d = 50.0
    result: list[_CalculatedBar] = []
    for bar in bars:
        lows.append(bar.low)
        highs.append(bar.high)
        j = None
        if len(lows) == config.kdj_period:
            lowest = min(lows)
            highest = max(highs)
            rsv = 50.0 if highest == lowest else (bar.close - lowest) / (highest - lowest) * 100.0
            k = (2.0 * k + rsv) / 3.0
            d = (2.0 * d + k) / 3.0
            j = 3.0 * k - 2.0 * d
        in_window = config.start_date.isoformat() <= bar.bar_date <= config.end_date.isoformat()
        no_ma5_signal = bar.ma5 is None or bar.close / bar.ma5 < 1.15
        buy_signal = bool(
            in_window
            and previous_close is not None
            and previous_volume is not None
            and previous_volume > 0
            and bar.volume is not None
            and bar.low > 0
            and j is not None
            and bar.high / bar.low > config.high_low_ratio
            and bar.high / previous_close > config.high_previous_close_ratio
            and bar.close / previous_close - 1.0 < config.maximum_close_gain
            and bar.volume / previous_volume > config.volume_ratio
            and j < config.buy_j_below
            and (no_ma5_signal or not config.require_no_ma5_signal)
        )
        result.append(_CalculatedBar(bar, previous_close, previous_volume, j, buy_signal))
        previous_close = bar.close
        previous_volume = bar.volume
    return result


def _make_trade(
    symbol: str,
    signal: _CalculatedBar,
    entry_bar: _Bar,
    exit_signal: _CalculatedBar | None,
    exit_bar: _Bar,
    reason: str,
    config: KdjVolumeReversalConfig,
) -> BacktestTrade:
    entry_price = entry_bar.open
    exit_price = exit_bar.open if reason == "J_ABOVE_100" else exit_bar.close
    quantity = math.floor(config.notional_per_trade / entry_price)
    invested = quantity * entry_price
    pnl = quantity * (exit_price - entry_price)
    return BacktestTrade(
        symbol=symbol,
        signal_date=signal.bar.bar_date,
        entry_date=entry_bar.bar_date,
        entry_price=entry_price,
        entry_j=float(signal.j),
        exit_signal_date=None if exit_signal is None else exit_signal.bar.bar_date,
        exit_date=exit_bar.bar_date,
        exit_price=exit_price,
        exit_j=None if exit_signal is None else float(exit_signal.j),
        exit_reason=reason,
        quantity=quantity,
        invested=invested,
        pnl=pnl,
        return_pct=(exit_price / entry_price - 1.0) * 100.0,
    )


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0
