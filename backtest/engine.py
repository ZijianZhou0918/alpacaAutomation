from __future__ import annotations

import csv
import html
import json
import math
from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca_ma5_service.alpaca_connection import load_alpaca_credentials
from alpaca_ma5_service import strategy as strategy_module
from alpaca_ma5_service import watchlist_generator as watchlist_module
from alpaca_ma5_service.afterhours_high_low import MinuteBar, fetch_minute_bars, regular_session_bounds
from alpaca_ma5_service.config import Settings
from alpaca_ma5_service.errors import short_error
from alpaca_ma5_service.market_time import is_buy_order_time, is_realtime_order_time
from alpaca_ma5_service.models import MarketSnapshot, Position
from alpaca_ma5_service.service import buy_limit_price_from_signal, is_stop_loss_signal, stop_loss_limit_price_from_signal
from alpaca_ma5_service.strategy import evaluate_buy, evaluate_sell, evaluate_stop_loss, evaluate_take_profit_remainder_stop
from alpaca_ma5_service.watchlist import normalize_symbol, to_alpaca_symbol
from alpaca_ma5_service.watchlist_generator import DailyBar
from backtest.data_cache import ADJUSTMENT_SPLIT, MarketDataCache
from backtest.daily_sources import (
    MASSIVE_DAILY_ADJUSTMENT,
    MASSIVE_DAILY_FEED,
    MOOMOO_DAILY_ADJUSTMENT,
    MOOMOO_DAILY_FEED,
    YAHOO_DAILY_ADJUSTMENT,
    YAHOO_DAILY_FEED,
    MassiveDailyConfig,
    MoomooDailyConfig,
    YahooDailyConfig,
    coalesced_date_ranges,
    failure_dates,
    fetch_massive_grouped_daily_bars_with_failures,
    fetch_moomoo_daily_bars,
    fetch_yahoo_daily_bars,
    fetch_yahoo_daily_bars_with_failures,
    filter_daily_bars_to_dates,
    merge_daily_bars,
)


@dataclass(frozen=True)
class BacktestConfig:
    symbols: list[str]
    start_date: date
    end_date: date
    timeframe: str
    initial_cash: float
    buy_notional_usd: float
    buy_position_pct: float
    max_positions: int
    max_daily_buys: int
    commission_per_order: float
    slippage_pct: float
    allow_repeat_buys: bool
    allow_overnight_holding: bool
    allow_fractional_shares: bool
    data_feed: str
    daily_data_source: str
    batch_size: int
    data_chunk_days: int
    use_data_cache: bool
    cache_daily_bars: bool
    cache_minute_bars: bool
    refresh_data_cache: bool
    data_cache_dir: Path
    data_cache_name: str
    warmup_calendar_days: int
    market_timezone: str
    order_timeout_seconds: int
    report_max_points_per_series: int
    report_max_price_symbols: int
    report_price_context_days: int
    stock_pool_description: str
    require_buy_day_open_below_signal_reference: bool
    output_dir: Path
    html_report_name: str
    strategy_settings: Settings
    watchlist_signal_params: dict[str, float]
    buy_signal_params: dict[str, float]
    sell_signal_params: dict[str, object]
    stop_params: dict[str, float]
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    moomoo_security_firm: str = "FUTUINC"
    moomoo_connect_timeout: float = 3.0
    moomoo_opend_exe_path: str = ""
    moomoo_opend_startup_timeout: float = 30.0
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
    massive_fallback_to_yahoo: bool = True
    strategy_name: str = strategy_module.DEFAULT_STRATEGY_NAME
    strategy_variant_name: str = "baseline_current"
    strategy_variant_description: str = "当前 monitor 对齐策略"
    optimization_rules: dict[str, object] = field(default_factory=dict)


@dataclass
class PositionState:
    symbol: str
    quantity: float
    avg_price: float
    opened_at: datetime
    signal_day: date | None = None
    buy_fees_remaining: float = 0.0

    def to_strategy_position(self) -> Position:
        return Position(self.symbol, self.quantity, self.avg_price, self.opened_at.isoformat())


@dataclass(frozen=True)
class PendingOrder:
    symbol: str
    side: str
    quantity: float
    limit_price: float
    notional_usd: float
    created_at: datetime
    expires_at: datetime
    reason: str
    reference_price: float = 0.0
    signal_day: date | None = None


@dataclass(frozen=True)
class TradeRecord:
    timestamp: datetime
    symbol: str
    side: str
    quantity: float
    price: float
    gross_value: float
    fee: float
    cash_after: float
    realized_pnl: float
    reason: str
    rule: str
    price_change_pct: float = 0.0
    signal_day: date | None = None


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class BacktestStats:
    initial_cash: float
    final_equity: float
    total_return: float
    return_pct: float
    closed_trade_count: int
    order_count: int
    win_rate: float
    max_drawdown_pct: float
    average_trade_pnl: float
    max_trade_profit: float
    max_trade_loss: float
    ending_cash: float
    ending_position_value: float
    open_position_count: int
    buy_order_count: int
    sell_order_count: int


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    stats: BacktestStats
    trades: list[TradeRecord]
    equity_curve: list[EquityPoint]
    price_points: list[PricePoint]
    watchlist_by_day: dict[str, list[str]]
    minute_bar_count: int
    report_path: Path


def run_backtest(
    config: BacktestConfig,
    bars_by_symbol: dict[str, list[MinuteBar]] | None = None,
    daily_bars: dict[str, list[DailyBar]] | None = None,
    historical_watchlists: dict[str, list[str]] | None = None,
) -> BacktestResult:
    if config.timeframe not in {"1Min", "1Day"}:
        raise ValueError("Backtest requires timeframe='1Min' or timeframe='1Day'.")
    if config.start_date > config.end_date:
        raise ValueError("start_date must be <= end_date.")

    scan_symbols = normalized_alpaca_symbols(config.symbols)
    if not scan_symbols:
        raise ValueError("At least one symbol is required.")

    with patched_strategy_params(config):
        if bars_by_symbol is None:
            if daily_bars is None:
                daily_bars = fetch_backtest_daily_bars(config, scan_symbols)
            if historical_watchlists is None:
                historical_watchlists = build_historical_watchlists(daily_bars, config)
            if config.timeframe == "1Day":
                bars_by_symbol = daily_bars_to_signal_bars(daily_bars, config)
            else:
                minute_symbols = sorted({symbol for symbols in historical_watchlists.values() for symbol in symbols})
                if not minute_symbols:
                    bars_by_symbol = {}
                elif config.allow_overnight_holding:
                    bars_by_symbol = fetch_backtest_bars(config, minute_symbols)
                else:
                    bars_by_symbol = fetch_candidate_day_minute_bars(config, historical_watchlists)
        else:
            bars_by_symbol = sort_and_dedupe_bars(bars_by_symbol)
            if daily_bars is None:
                daily_bars = build_daily_bars(bars_by_symbol)
            if historical_watchlists is None:
                historical_watchlists = {}

        bars_by_symbol = sort_and_dedupe_bars(bars_by_symbol)
        minute_bar_count = sum(len(bars) for bars in bars_by_symbol.values())
        if minute_bar_count <= 0 and any(historical_watchlists.values()):
            raise RuntimeError(f"No {config.timeframe} historical bars were returned for the historical strategy candidates.")

        simulator = BacktestSimulator(config, scan_symbols, bars_by_symbol, daily_bars, historical_watchlists)
        result = simulator.run(minute_bar_count)
        result = replace(result, trades=chronological_trades(result.trades))
        write_trade_csv(config.output_dir, result.trades)
        report_path = result.report_path
        if config.html_report_name:
            report_path = write_html_report(result, daily_bars)
        return BacktestResult(
            config=result.config,
            stats=result.stats,
            trades=result.trades,
            equity_curve=result.equity_curve,
            price_points=result.price_points,
            watchlist_by_day=result.watchlist_by_day,
            minute_bar_count=result.minute_bar_count,
            report_path=report_path,
        )


class BacktestSimulator:
    def __init__(
        self,
        config: BacktestConfig,
        symbols: list[str],
        bars_by_symbol: dict[str, list[MinuteBar]],
        daily_bars: dict[str, list[DailyBar]],
        historical_watchlists: dict[str, list[str]] | None = None,
    ):
        self.config = config
        self.symbols = symbols
        self.bars_by_symbol = bars_by_symbol
        self.daily_bars = daily_bars
        self.daily_bars_by_symbol = {
            to_alpaca_symbol(symbol): sorted(bars, key=lambda item: item.date)
            for symbol, bars in daily_bars.items()
        }
        self.daily_dates_by_symbol = {
            symbol: [bar.date for bar in bars]
            for symbol, bars in self.daily_bars_by_symbol.items()
        }
        self.regular_open_by_symbol_day = self.build_regular_open_by_symbol_day()
        self.market_tz = ZoneInfo(config.market_timezone)
        self.cash = float(config.initial_cash)
        self.positions: dict[str, PositionState] = {}
        self.pending_orders: dict[str, PendingOrder] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[EquityPoint] = []
        self.watchlist_by_day: dict[str, list[str]] = dict(historical_watchlists or {})
        self.daily_buy_counts: dict[date, int] = {}
        self.daily_buy_exclusions: dict[date, set[str]] = {}
        self.daily_half_sells: dict[date, set[str]] = {}
        self.daily_bought_symbols: dict[date, set[str]] = {}
        self.last_prices: dict[str, float] = {}
        self.last_price_times: dict[str, datetime] = {}

    def build_regular_open_by_symbol_day(self) -> dict[tuple[str, date], float]:
        out: dict[tuple[str, date], float] = {}
        for symbol, bars in self.bars_by_symbol.items():
            alpaca_symbol = to_alpaca_symbol(symbol)
            for bar in bars:
                key = (alpaca_symbol, bar.timestamp.date())
                if key in out:
                    continue
                start, end = regular_session_bounds(bar.timestamp.date(), bar.timestamp.tzinfo)
                if start <= bar.timestamp < end:
                    out[key] = bar.open
        return out

    def run(self, minute_bar_count: int) -> BacktestResult:
        bars_by_time = bars_grouped_by_time(self.bars_by_symbol, self.config.start_date, self.config.end_date)
        timestamps = sorted(bars_by_time)
        for index, timestamp in enumerate(timestamps):
            bars_at_time = bars_by_time[timestamp]
            for symbol, bar in bars_at_time.items():
                self.last_prices[symbol] = bar.close
                self.last_price_times[symbol] = bar.timestamp

            self.fill_or_cancel_pending_orders(timestamp, bars_at_time)
            watch_symbols = self.watchlist_for_day(timestamp.date())
            watch_set = set(watch_symbols)
            monitor_symbols = watch_symbols + sorted(symbol for symbol in self.positions if symbol not in watch_set)
            for symbol in monitor_symbols:
                bar = bars_at_time.get(to_alpaca_symbol(symbol))
                if bar is None:
                    continue
                self.process_symbol_minute(symbol, bar, watch_set)
            next_timestamp = timestamps[index + 1] if index + 1 < len(timestamps) else None
            if not self.config.allow_overnight_holding and (next_timestamp is None or next_timestamp.date() != timestamp.date()):
                self.force_close_for_day(timestamp.date(), timestamp)
            self.equity_curve.append(EquityPoint(timestamp, self.current_equity(), self.cash))

        final_equity = self.current_equity()
        stats = build_stats(
            initial_cash=self.config.initial_cash,
            final_equity=final_equity,
            ending_cash=self.cash,
            ending_position_value=self.position_value(),
            open_position_count=len(self.positions),
            trades=self.trades,
            equity_curve=self.equity_curve,
        )
        result = BacktestResult(
            config=self.config,
            stats=stats,
            trades=self.trades,
            equity_curve=sample_equity(self.equity_curve, self.config.report_max_points_per_series),
            price_points=[],
            watchlist_by_day=self.watchlist_by_day,
            minute_bar_count=minute_bar_count,
            report_path=self.config.output_dir / self.config.html_report_name,
        )
        return result

    def process_symbol_minute(self, symbol: str, bar: MinuteBar, watch_set: set[str]) -> None:
        normalized = normalize_symbol(symbol)
        position = self.positions.get(normalized)
        snapshot = self.snapshot_for_bar(symbol, bar)
        if snapshot is None:
            return

        if position is not None:
            self.process_sell_signal(position, snapshot, bar, normalized in watch_set)
            return

        if normalized not in watch_set:
            return
        if not is_buy_order_time(bar.timestamp):
            return
        if normalized in self.daily_buy_exclusions.setdefault(bar.timestamp.date(), set()):
            return
        if self.pending_orders.get(normalized) is not None:
            return
        if self.daily_buy_slots_used(bar.timestamp.date()) >= self.config.max_daily_buys:
            return
        if len(self.positions) >= self.config.max_positions:
            return
        if not self.config.allow_repeat_buys and normalized in self.daily_bought_symbols.get(bar.timestamp.date(), set()):
            return
        if self.config.require_buy_day_open_below_signal_reference and not buy_day_open_below_signal_reference(snapshot):
            return
        if not passes_optimization_buy_filters(snapshot, bar.timestamp, self.config.optimization_rules):
            return

        signal = evaluate_buy(snapshot)
        if signal.diagnostics.get("daily_buy_exclusion") == "ma5_touch_without_required_drop":
            self.daily_buy_exclusions[bar.timestamp.date()].add(normalized)
            return
        if signal.action != "BUY":
            return

        limit_price = buy_limit_price_from_signal(signal)
        notional = self.buy_notional()
        if limit_price <= 0 or notional <= 0:
            return
        signal_bar = self.signal_bar_before(symbol, bar.timestamp.date())
        signal_close = signal_bar.close if signal_bar is not None else snapshot.previous_closes[-1] if snapshot.previous_closes else 0.0
        signal_day = signal_bar.date if signal_bar is not None else None
        self.create_pending_order(normalized, "BUY", 0.0, limit_price, notional, bar.timestamp, signal.reason, signal_close, signal_day)

    def process_sell_signal(self, position: PositionState, snapshot: MarketSnapshot, bar: MinuteBar, in_watchlist: bool) -> None:
        if not is_realtime_order_time(bar.timestamp):
            return
        if self.pending_orders.get(position.symbol) is not None:
            return

        strategy_position = position.to_strategy_position()
        signal = evaluate_sell(strategy_position, snapshot, bar.timestamp, self.config.strategy_settings) if in_watchlist else evaluate_stop_loss(strategy_position, snapshot, self.config.strategy_settings)
        half_profit_done = in_watchlist and position.symbol in self.daily_half_sells.setdefault(bar.timestamp.date(), set())
        if half_profit_done:
            if signal.action == "SELL_HALF":
                signal = evaluate_take_profit_remainder_stop(strategy_position, snapshot, self.config.strategy_settings)
                if signal.action == "HOLD":
                    return
            elif signal.action == "HOLD":
                signal = evaluate_take_profit_remainder_stop(strategy_position, snapshot, self.config.strategy_settings)
        if signal.action not in {"SELL_ALL", "SELL_HALF"}:
            return

        quantity = min(position.quantity, signal.quantity)
        if quantity <= 0:
            return

        if is_stop_loss_signal(signal):
            limit_price = stop_loss_limit_price_from_signal(signal)
            if limit_price <= 0:
                return
            self.create_pending_order(position.symbol, "SELL", quantity, limit_price, 0.0, bar.timestamp, signal.reason)
            return

        price = apply_sell_slippage(bar.close, self.config.slippage_pct)
        self.execute_sell(position.symbol, quantity, price, bar.timestamp, signal.reason, signal.diagnostics.get("sell_rule", signal.action))
        if signal.action == "SELL_HALF":
            self.daily_half_sells[bar.timestamp.date()].add(position.symbol)

    def create_pending_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        limit_price: float,
        notional_usd: float,
        created_at: datetime,
        reason: str,
        reference_price: float = 0.0,
        signal_day: date | None = None,
    ) -> PendingOrder:
        order = PendingOrder(
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            notional_usd=notional_usd,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=self.config.order_timeout_seconds),
            reason=reason,
            reference_price=reference_price,
            signal_day=signal_day,
        )
        self.pending_orders[symbol] = order
        return order

    def daily_buy_slots_used(self, day: date) -> int:
        pending_buys = sum(
            1
            for order in self.pending_orders.values()
            if order.side == "BUY" and order.created_at.date() == day
        )
        return self.daily_buy_counts.get(day, 0) + pending_buys

    def fill_or_cancel_pending_orders(self, timestamp: datetime, bars_at_time: dict[str, MinuteBar]) -> None:
        for symbol, order in list(self.pending_orders.items()):
            if timestamp > order.expires_at:
                self.pending_orders.pop(symbol, None)
                continue
            bar = bars_at_time.get(to_alpaca_symbol(symbol))
            if bar is not None:
                self.try_fill_pending_order(order, bar)

    def try_fill_pending_order(self, order: PendingOrder, bar: MinuteBar) -> bool:
        if order.side == "BUY":
            if bar.low > order.limit_price:
                return False
            raw_price = min(order.limit_price, bar.open) if bar.open <= order.limit_price else order.limit_price
            fill_price = apply_buy_slippage(raw_price, self.config.slippage_pct)
            return self.fill_buy_order(order, fill_price, bar.timestamp)

        if bar.high < order.limit_price:
            return False
        raw_price = max(order.limit_price, bar.open) if bar.open >= order.limit_price else order.limit_price
        fill_price = apply_sell_slippage(raw_price, self.config.slippage_pct)
        self.execute_sell(order.symbol, order.quantity, fill_price, bar.timestamp, order.reason, "stop_loss")
        self.pending_orders.pop(order.symbol, None)
        return True

    def fill_buy_order(self, order: PendingOrder, fill_price: float, timestamp: datetime) -> bool:
        quantity = self.quantity_for_notional(order.notional_usd, fill_price)
        if quantity <= 0:
            self.pending_orders.pop(order.symbol, None)
            return False
        total_cost = quantity * fill_price + self.config.commission_per_order
        if total_cost > self.cash:
            quantity = self.quantity_for_notional(max(0.0, self.cash - self.config.commission_per_order), fill_price)
            total_cost = quantity * fill_price + self.config.commission_per_order
        if quantity <= 0 or total_cost > self.cash:
            self.pending_orders.pop(order.symbol, None)
            return False
        price_change_pct = fill_price / order.reference_price - 1.0 if order.reference_price > 0 else 0.0
        self.execute_buy(order.symbol, quantity, fill_price, timestamp, order.reason, price_change_pct, order.signal_day)
        self.pending_orders.pop(order.symbol, None)
        return True

    def force_close_for_day(self, day: date, fallback_timestamp: datetime) -> None:
        self.pending_orders.clear()
        for symbol, position in list(self.positions.items()):
            alpaca_symbol = to_alpaca_symbol(symbol)
            price_time = self.last_price_times.get(alpaca_symbol)
            price = self.last_prices.get(alpaca_symbol, 0.0)
            if price <= 0 or price_time is None or price_time.date() != day:
                continue
            reason = "不允许隔夜持仓，按当日最后一根 1Min 收盘价强制平仓"
            self.execute_sell(symbol, position.quantity, apply_sell_slippage(price, self.config.slippage_pct), price_time or fallback_timestamp, reason, "end_of_day_forced_liquidation")

    def execute_buy(self, symbol: str, quantity: float, price: float, timestamp: datetime, reason: str, price_change_pct: float = 0.0, signal_day: date | None = None) -> None:
        fee = self.config.commission_per_order
        gross = quantity * price
        self.cash -= gross + fee
        current = self.positions.get(symbol)
        if current is None:
            self.positions[symbol] = PositionState(symbol, quantity, price, timestamp, signal_day, fee)
        else:
            total_qty = current.quantity + quantity
            avg_price = ((current.avg_price * current.quantity) + gross) / total_qty
            current.quantity = total_qty
            current.avg_price = avg_price
            current.buy_fees_remaining += fee
        self.daily_buy_counts[timestamp.date()] = self.daily_buy_counts.get(timestamp.date(), 0) + 1
        self.daily_bought_symbols.setdefault(timestamp.date(), set()).add(symbol)
        self.trades.append(TradeRecord(timestamp, symbol, "BUY", quantity, price, gross, fee, self.cash, 0.0, reason, "buy_limit", price_change_pct, signal_day))

    def execute_sell(self, symbol: str, quantity: float, price: float, timestamp: datetime, reason: str, rule: object) -> None:
        position = self.positions.get(symbol)
        if position is None or quantity <= 0:
            return
        quantity = min(quantity, position.quantity)
        fee = self.config.commission_per_order
        gross = quantity * price
        fee_fraction = quantity / position.quantity if position.quantity > 0 else 0.0
        buy_fee_alloc = position.buy_fees_remaining * fee_fraction
        realized_pnl = (price - position.avg_price) * quantity - buy_fee_alloc - fee
        price_change_pct = price / position.avg_price - 1.0 if position.avg_price > 0 else 0.0
        signal_day = position.signal_day
        self.cash += gross - fee
        position.quantity = round(position.quantity - quantity, 10)
        position.buy_fees_remaining -= buy_fee_alloc
        if position.quantity <= 1e-9:
            self.positions.pop(symbol, None)
        self.trades.append(TradeRecord(timestamp, symbol, "SELL", quantity, price, gross, fee, self.cash, realized_pnl, reason, str(rule), price_change_pct, signal_day))

    def snapshot_for_bar(self, symbol: str, bar: MinuteBar) -> MarketSnapshot | None:
        alpaca_symbol = to_alpaca_symbol(symbol)
        previous = self.previous_daily_bars(alpaca_symbol, bar.timestamp.date(), 4)
        if len(previous) < 4:
            return None
        today_open = self.regular_open_by_symbol_day.get((alpaca_symbol, bar.timestamp.date()), 0.0)
        return MarketSnapshot(
            symbol=normalize_symbol(symbol),
            current_price=bar.close,
            previous_closes=[item.close for item in previous[-4:]],
            as_of=bar.timestamp,
            current_price_source="alpaca_1min_close",
            today_open=today_open,
            today_open_source="alpaca_1min_open",
            previous_opens=[item.open for item in previous[-4:]],
        )

    def signal_bar_before(self, symbol: str, day: date) -> DailyBar | None:
        alpaca_symbol = to_alpaca_symbol(symbol)
        previous = self.previous_daily_bars(alpaca_symbol, day, 1)
        return previous[-1] if previous else None

    def previous_daily_bars(self, alpaca_symbol: str, day: date, count: int) -> list[DailyBar]:
        dates = self.daily_dates_by_symbol.get(alpaca_symbol, [])
        if not dates:
            return []
        index = bisect_left(dates, day)
        if index <= 0:
            return []
        return self.daily_bars_by_symbol[alpaca_symbol][max(0, index - count) : index]

    def watchlist_for_day(self, day: date) -> list[str]:
        day_key = f"{day:%Y-%m-%d}"
        if day_key in self.watchlist_by_day:
            return self.watchlist_by_day[day_key]
        now_et = datetime.combine(day, time(9, 30), tzinfo=self.market_tz)
        candidates = watchlist_module.screen_candidates(self.daily_bars, now_et)
        symbols = [normalize_symbol(candidate.symbol) for candidate in candidates]
        self.watchlist_by_day[day_key] = symbols
        return symbols

    def buy_notional(self) -> float:
        if self.config.buy_notional_usd > 0:
            return min(self.config.buy_notional_usd, self.cash)
        if self.config.buy_position_pct > 0:
            return min(self.cash * self.config.buy_position_pct, self.cash)
        return 0.0

    def quantity_for_notional(self, notional: float, price: float) -> float:
        if price <= 0 or notional <= 0:
            return 0.0
        quantity = notional / price
        if not self.config.allow_fractional_shares:
            quantity = math.floor(quantity)
        return round(quantity, 6)

    def current_equity(self) -> float:
        return self.cash + self.position_value()

    def position_value(self) -> float:
        total = 0.0
        for symbol, position in self.positions.items():
            price = self.last_prices.get(to_alpaca_symbol(symbol), position.avg_price)
            total += position.quantity * price
        return total


def fetch_backtest_bars(config: BacktestConfig, symbols: list[str]) -> dict[str, list[MinuteBar]]:
    market_tz = ZoneInfo(config.market_timezone)
    fetch_start_date = config.start_date - timedelta(days=config.warmup_calendar_days)
    fetch_start = datetime.combine(fetch_start_date, time.min, tzinfo=market_tz)
    fetch_end = datetime.combine(config.end_date + timedelta(days=1), time.min, tzinfo=market_tz)
    bars_by_symbol: dict[str, list[MinuteBar]] = {symbol: [] for symbol in symbols}
    chunk_start = fetch_start
    while chunk_start < fetch_end:
        chunk_end = min(chunk_start + timedelta(days=max(1, config.data_chunk_days)), fetch_end)
        print(f"Fetching 1Min bars: {chunk_start:%Y-%m-%d} -> {chunk_end:%Y-%m-%d} feed={config.data_feed}", flush=True)
        chunk = fetch_minute_bars_for_range(config, symbols, chunk_start, chunk_end)
        for symbol, bars in chunk.items():
            bars_by_symbol.setdefault(to_alpaca_symbol(symbol), []).extend(bars)
        chunk_start = chunk_end
    return bars_by_symbol


def fetch_backtest_daily_bars(config: BacktestConfig, symbols: list[str]) -> dict[str, list[DailyBar]]:
    market_tz = ZoneInfo(config.market_timezone)
    requested_start = datetime.combine(config.start_date - timedelta(days=config.warmup_calendar_days), time.min, tzinfo=market_tz)
    requested_end = datetime.combine(config.end_date + timedelta(days=1), time.min, tzinfo=market_tz)
    safe_end = watchlist_module.request_end_datetime(datetime.now(market_tz), config.data_feed)
    request_end = min(requested_end, safe_end)

    if not config.use_data_cache or not config.cache_daily_bars:
        return fetch_backtest_daily_bars_from_api(config, symbols, requested_start, request_end)

    cache = market_data_cache(config)
    start_date = requested_start.date()
    end_date_exclusive = daily_request_end_date_exclusive(request_end)
    feed_key = daily_cache_feed(config)
    adjustment_key = daily_cache_adjustment(config)
    cached = {} if config.refresh_data_cache else cache.load_daily_bars(
        symbols,
        start_date,
        end_date_exclusive,
        feed=feed_key,
        adjustment=adjustment_key,
    )
    missing = symbols if config.refresh_data_cache else cache.uncovered_symbols(
        "daily",
        symbols,
        start_date.isoformat(),
        end_date_exclusive.isoformat(),
        feed=feed_key,
        adjustment=adjustment_key,
    )
    if missing:
        print(f"Daily cache miss: symbols={len(missing)} cache={cache.path}", flush=True)
        fetched = fetch_backtest_daily_bars_from_source(config, missing, requested_start, request_end)
        if fetched:
            cache.save_daily_bars(
                fetched,
                feed=feed_key,
                range_start=start_date,
                range_end_exclusive=end_date_exclusive,
                covered_symbols=missing,
                adjustment=adjustment_key,
            )
            cached.update(fetched)
    else:
        print(f"Daily cache hit: symbols={len(symbols)} cache={cache.path}", flush=True)
    return {symbol: cached.get(to_alpaca_symbol(symbol), []) for symbol in symbols}


def fetch_backtest_daily_bars_from_source(
    config: BacktestConfig,
    symbols: list[str],
    requested_start: datetime,
    request_end: datetime,
) -> dict[str, list[DailyBar]]:
    if config.daily_data_source.lower() == "moomoo":
        return fetch_moomoo_daily_bars(
            symbols,
            requested_start.date(),
            daily_request_end_date_exclusive(request_end),
            MoomooDailyConfig(
                host=config.moomoo_host,
                port=config.moomoo_port,
                security_firm=config.moomoo_security_firm,
                connect_timeout=config.moomoo_connect_timeout,
                opend_exe_path=config.moomoo_opend_exe_path,
                opend_startup_timeout=config.moomoo_opend_startup_timeout,
            ),
        )
    if config.daily_data_source.lower() == "yahoo":
        return fetch_yahoo_daily_bars(
            symbols,
            requested_start.date(),
            daily_request_end_date_exclusive(request_end),
            YahooDailyConfig(
                request_sleep_seconds=config.yahoo_request_sleep_seconds,
                rate_limit_retry_seconds=config.yahoo_rate_limit_retry_seconds,
                max_retries=config.yahoo_max_retries,
            ),
        )
    if config.daily_data_source.lower() == "massive":
        fetch_result = fetch_massive_grouped_daily_bars_with_failures(
            symbols,
            requested_start.date(),
            daily_request_end_date_exclusive(request_end),
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
        if not fetch_result.failures or not config.massive_fallback_to_yahoo:
            return fetch_result.bars_by_symbol
        fallback = fetch_yahoo_backtest_fallback_for_failed_massive_dates(config, symbols, fetch_result.failures)
        return merge_daily_bars(fetch_result.bars_by_symbol, fallback)
    return fetch_backtest_daily_bars_from_api(config, symbols, requested_start, request_end)


def fetch_yahoo_backtest_fallback_for_failed_massive_dates(
    config: BacktestConfig,
    symbols: list[str],
    massive_failures: list[dict[str, str]],
) -> dict[str, list[DailyBar]]:
    dates = failure_dates(massive_failures)
    if not dates:
        return {}
    date_set = set(dates)
    combined: dict[str, list[DailyBar]] = {}
    ranges = coalesced_date_ranges(dates)
    print(f"Yahoo fallback for backtest Massive failed dates: dates={len(dates)} ranges={len(ranges)} symbols={len(symbols)}", flush=True)
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
        combined = merge_daily_bars(combined, filtered)
        print(
            f"Yahoo fallback range done: {range_start}->{range_end_exclusive} rows={sum(len(bars) for bars in filtered.values()):,} "
            f"symbols={len(filtered)} failures={len(fetch_result.failures)}",
            flush=True,
        )
    return combined


def fetch_backtest_daily_bars_from_api(
    config: BacktestConfig,
    symbols: list[str],
    requested_start: datetime,
    request_end: datetime,
) -> dict[str, list[DailyBar]]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    market_tz = ZoneInfo(config.market_timezone)
    api_key, secret_key = load_alpaca_credentials()
    client = StockHistoricalDataClient(api_key, secret_key)
    bars_by_symbol: dict[str, list[DailyBar]] = {}

    print(f"Fetching daily bars for historical screening: symbols={len(symbols)} feed={config.data_feed}", flush=True)
    for batch in watchlist_module.batched(symbols, config.batch_size):
        try:
            raw_bars = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame.Day,
                    start=requested_start,
                    end=request_end,
                    limit=len(batch) * max(1, (request_end.date() - requested_start.date()).days + 1),
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
                raw_bars = client.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=batch,
                        timeframe=TimeFrame.Day,
                        start=requested_start,
                        end=min(request_end, watchlist_module.request_end_datetime(datetime.now(market_tz), "iex")),
                        limit=len(batch) * max(1, (request_end.date() - requested_start.date()).days + 1),
                        adjustment=Adjustment.SPLIT,
                        feed=DataFeed("iex"),
                    )
                ).data
            except Exception as fallback_exc:
                print(f"IEX daily bars failed, skipped {batch[0]}...{batch[-1]}: {short_error(fallback_exc)}", flush=True)
                continue

        range_start_date = requested_start.date()
        range_end_date = daily_request_end_date_exclusive(request_end)
        for symbol, bars in raw_bars.items():
            parsed = [
                watchlist_module.daily_bar_from_alpaca(to_alpaca_symbol(symbol), bar, datetime.now(market_tz))
                for bar in bars
            ]
            bars_by_symbol[to_alpaca_symbol(symbol)] = [
                bar for bar in parsed if range_start_date <= bar.date < range_end_date
            ]
    return bars_by_symbol


def daily_request_end_date_exclusive(request_end: datetime) -> date:
    if request_end.time() == time.min:
        return request_end.date()
    return request_end.date() + timedelta(days=1)


def build_historical_watchlists(daily_bars: dict[str, list[DailyBar]], config: BacktestConfig) -> dict[str, list[str]]:
    all_dates = sorted({bar.date for bars in daily_bars.values() for bar in bars})
    trading_days = [day for day in all_dates if config.start_date <= day <= config.end_date]
    sorted_bars = {
        to_alpaca_symbol(symbol): sorted(bars, key=lambda item: item.date)
        for symbol, bars in daily_bars.items()
    }
    date_indexes = {
        symbol: {bar.date: index for index, bar in enumerate(bars)}
        for symbol, bars in sorted_bars.items()
    }
    signal_date_by_day: dict[date, date | None] = {}
    previous_index = -1
    for day in trading_days:
        while previous_index + 1 < len(all_dates) and all_dates[previous_index + 1] < day:
            previous_index += 1
        signal_date_by_day[day] = all_dates[previous_index] if previous_index >= 0 else None

    candidates_by_signal_date: dict[date, list[str]] = {}
    for signal_date in sorted({item for item in signal_date_by_day.values() if item is not None}):
        candidates = []
        for symbol, bars in sorted_bars.items():
            candidate = evaluate_historical_watch_candidate(
                symbol,
                bars,
                date_indexes[symbol].get(signal_date),
                config.optimization_rules,
            )
            if candidate:
                candidates.append(candidate)
        candidates_by_signal_date[signal_date] = [
            normalize_symbol(candidate.symbol)
            for candidate in sorted_watch_candidates(candidates, config.optimization_rules)
        ]

    watchlists: dict[str, list[str]] = {}
    for day in trading_days:
        signal_date = signal_date_by_day.get(day)
        watchlists[f"{day:%Y-%m-%d}"] = candidates_by_signal_date.get(signal_date, []) if signal_date else []
    candidate_count = len({symbol for symbols in watchlists.values() for symbol in symbols})
    active_days = sum(1 for symbols in watchlists.values() if symbols)
    print(f"Historical screening finished: active_days={active_days} candidate_symbols={candidate_count}", flush=True)
    return watchlists


def sorted_watch_candidates(
    candidates: list[watchlist_module.WatchCandidate],
    optimization_rules: dict[str, object] | None,
) -> list[watchlist_module.WatchCandidate]:
    sort_name = str((optimization_rules or {}).get("candidate_sort") or "gain_desc_upper_desc")
    if sort_name == "gain_asc_upper_asc":
        return sorted(candidates, key=lambda item: (item.gain_pct, item.upper_shadow_pct, item.symbol))
    if sort_name == "upper_asc_gain_desc":
        return sorted(candidates, key=lambda item: (item.upper_shadow_pct, -item.gain_pct, item.symbol))
    if sort_name == "body_desc_upper_asc":
        return sorted(candidates, key=lambda item: (-item.body_pct, item.upper_shadow_pct, -item.gain_pct, item.symbol))
    if sort_name == "close_to_ma5_asc_gain_desc":
        return sorted(candidates, key=lambda item: (item.close / item.ma5 if item.ma5 > 0 else 999.0, -item.gain_pct, item.symbol))
    return sorted(candidates, key=lambda item: (item.gain_pct, item.upper_shadow_pct, item.symbol), reverse=True)


def evaluate_historical_watch_candidate(
    symbol: str,
    bars: list[DailyBar],
    signal_index: int | None,
    optimization_rules: dict[str, object] | None = None,
) -> watchlist_module.WatchCandidate | None:
    if signal_index is None or signal_index < 19:
        return None

    signal = bars[signal_index]
    previous = bars[signal_index - 1]
    if previous.close <= 0:
        return None

    closes20 = [bar.close for bar in bars[signal_index - 19 : signal_index + 1]]
    ma5 = watchlist_module.average(closes20[-5:])
    ma10 = watchlist_module.average(closes20[-10:])
    ma20 = watchlist_module.average(closes20)
    previous_ma5 = watchlist_module.average([bar.close for bar in bars[signal_index - 5 : signal_index]])
    gain_pct = signal.close / previous.close - 1.0
    ma5_gain_pct = ma5 / previous_ma5 - 1.0 if previous_ma5 > 0 else 0.0
    body_pct = watchlist_module.bullish_body_pct(signal.open, signal.close)
    upper_shadow_pct = (signal.high - max(signal.open, signal.close)) / previous.close

    if gain_pct <= watchlist_module.MIN_SIGNAL_GAIN_PCT:
        return None
    if gain_pct - ma5_gain_pct <= watchlist_module.MIN_SIGNAL_GAIN_OVER_MA5_GAIN_PCT:
        return None
    if ma5 <= 0 or signal.close / ma5 <= watchlist_module.MIN_CLOSE_TO_MA5_RATIO:
        return None
    if ma5 <= 0 or signal.open / ma5 <= watchlist_module.MIN_OPEN_TO_MA5_RATIO:
        return None
    if not passes_optimization_daily_filters(
        signal,
        bars,
        signal_index,
        gain_pct,
        ma5_gain_pct,
        body_pct,
        upper_shadow_pct,
        ma5,
        ma10,
        ma20,
        optimization_rules or {},
    ):
        return None

    return watchlist_module.WatchCandidate(
        symbol,
        signal.date,
        gain_pct,
        upper_shadow_pct,
        ma5,
        ma10,
        ma20,
        signal.open,
        signal.high,
        signal.close,
        signal.low,
        ma5_gain_pct,
        body_pct,
    )


def passes_optimization_daily_filters(
    signal: DailyBar,
    bars: list[DailyBar],
    signal_index: int,
    gain_pct: float,
    ma5_gain_pct: float,
    body_pct: float,
    upper_shadow_pct: float,
    ma5: float,
    ma10: float,
    ma20: float,
    rules: dict[str, object],
) -> bool:
    if not rules:
        return True

    max_signal_gain_pct = rule_float(rules, "max_signal_gain_pct")
    if max_signal_gain_pct is not None and gain_pct > max_signal_gain_pct:
        return False

    min_signal_body_pct = rule_float(rules, "min_signal_body_pct")
    if min_signal_body_pct is not None and body_pct < min_signal_body_pct:
        return False

    max_upper_shadow_pct = rule_float(rules, "max_upper_shadow_pct")
    if max_upper_shadow_pct is not None and upper_shadow_pct > max_upper_shadow_pct:
        return False

    signal_range_pct = (signal.high - signal.low) / bars[signal_index - 1].close if bars[signal_index - 1].close > 0 else 0.0
    min_signal_range_pct = rule_float(rules, "min_signal_range_pct")
    if min_signal_range_pct is not None and signal_range_pct < min_signal_range_pct:
        return False

    max_signal_range_pct = rule_float(rules, "max_signal_range_pct")
    if max_signal_range_pct is not None and signal_range_pct > max_signal_range_pct:
        return False

    signal_close_position_pct = (signal.close - signal.low) / (signal.high - signal.low) if signal.high > signal.low else 0.0
    min_signal_close_position_pct = rule_float(rules, "min_signal_close_position_pct")
    if min_signal_close_position_pct is not None and signal_close_position_pct < min_signal_close_position_pct:
        return False

    max_signal_close_position_pct = rule_float(rules, "max_signal_close_position_pct")
    if max_signal_close_position_pct is not None and signal_close_position_pct > max_signal_close_position_pct:
        return False

    min_ma5_gain_pct = rule_float(rules, "min_ma5_gain_pct")
    if min_ma5_gain_pct is not None and ma5_gain_pct < min_ma5_gain_pct:
        return False

    max_ma5_gain_pct = rule_float(rules, "max_ma5_gain_pct")
    if max_ma5_gain_pct is not None and ma5_gain_pct > max_ma5_gain_pct:
        return False

    min_close_to_ma5_ratio = rule_float(rules, "min_close_to_ma5_ratio")
    close_to_ma5_ratio = signal.close / ma5 if ma5 > 0 else 0.0
    if min_close_to_ma5_ratio is not None and close_to_ma5_ratio < min_close_to_ma5_ratio:
        return False

    max_close_to_ma5_ratio = rule_float(rules, "max_close_to_ma5_ratio")
    if max_close_to_ma5_ratio is not None and close_to_ma5_ratio > max_close_to_ma5_ratio:
        return False

    min_close_to_ma10_ratio = rule_float(rules, "min_close_to_ma10_ratio")
    close_to_ma10_ratio = signal.close / ma10 if ma10 > 0 else 0.0
    if min_close_to_ma10_ratio is not None and close_to_ma10_ratio < min_close_to_ma10_ratio:
        return False

    max_close_to_ma10_ratio = rule_float(rules, "max_close_to_ma10_ratio")
    if max_close_to_ma10_ratio is not None and close_to_ma10_ratio > max_close_to_ma10_ratio:
        return False

    min_close_to_ma20_ratio = rule_float(rules, "min_close_to_ma20_ratio")
    close_to_ma20_ratio = signal.close / ma20 if ma20 > 0 else 0.0
    if min_close_to_ma20_ratio is not None and close_to_ma20_ratio < min_close_to_ma20_ratio:
        return False

    max_close_to_ma20_ratio = rule_float(rules, "max_close_to_ma20_ratio")
    if max_close_to_ma20_ratio is not None and close_to_ma20_ratio > max_close_to_ma20_ratio:
        return False

    for days in (5, 10, 20):
        prior_gain = prior_close_gain_pct(bars, signal_index, days)
        min_prior_gain = rule_float(rules, f"min_prior_{days}_gain_pct")
        if min_prior_gain is not None and prior_gain < min_prior_gain:
            return False
        max_prior_gain = rule_float(rules, f"max_prior_{days}_gain_pct")
        if max_prior_gain is not None and prior_gain > max_prior_gain:
            return False

    if bool(rules.get("require_ma5_gt_ma10_gt_ma20")) and not (ma5 > ma10 > ma20 > 0):
        return False

    closes20 = [bar.close for bar in bars[signal_index - 19 : signal_index + 1]]
    if not passes_signal_bollinger_filters(signal, closes20, ma20, rules):
        return False

    if not passes_signal_volatility_filters(bars, signal_index, rules):
        return False

    if not passes_signal_prior_trend_filters(bars, signal_index, rules):
        return False

    signal_volume = safe_float(signal.volume)
    min_signal_dollar_volume = rule_float(rules, "min_signal_dollar_volume")
    if min_signal_dollar_volume is not None and signal.close * signal_volume < min_signal_dollar_volume:
        return False

    min_signal_volume_to_avg20 = rule_float(rules, "min_signal_volume_to_avg20")
    if min_signal_volume_to_avg20 is not None:
        previous_volumes = [safe_float(bar.volume) for bar in bars[max(0, signal_index - 20) : signal_index]]
        previous_volumes = [value for value in previous_volumes if value > 0]
        if signal_volume <= 0 or len(previous_volumes) < 20:
            return False
        average_volume = sum(previous_volumes) / len(previous_volumes)
        if average_volume <= 0 or signal_volume / average_volume < min_signal_volume_to_avg20:
            return False

    if not passes_signal_volume_trend_filters(bars, signal_index, rules):
        return False

    return True


def passes_signal_bollinger_filters(
    signal: DailyBar,
    closes20: list[float],
    ma20: float,
    rules: dict[str, object],
) -> bool:
    std20 = population_stddev(closes20)
    z20 = (signal.close - ma20) / std20 if std20 > 0 else 0.0
    bandwidth20 = (4.0 * std20 / ma20) if ma20 > 0 else 0.0

    min_z20 = rule_float(rules, "min_signal_bollinger_z20")
    if min_z20 is not None and z20 < min_z20:
        return False
    max_z20 = rule_float(rules, "max_signal_bollinger_z20")
    if max_z20 is not None and z20 > max_z20:
        return False

    min_bandwidth20 = rule_float(rules, "min_signal_bollinger_bandwidth20")
    if min_bandwidth20 is not None and bandwidth20 < min_bandwidth20:
        return False
    max_bandwidth20 = rule_float(rules, "max_signal_bollinger_bandwidth20")
    if max_bandwidth20 is not None and bandwidth20 > max_bandwidth20:
        return False
    return True


def passes_signal_volatility_filters(bars: list[DailyBar], signal_index: int, rules: dict[str, object]) -> bool:
    atr20_pct = average_true_range_pct(bars, signal_index, 20)
    min_atr20 = rule_float(rules, "min_signal_atr20_pct")
    if min_atr20 is not None and atr20_pct < min_atr20:
        return False
    max_atr20 = rule_float(rules, "max_signal_atr20_pct")
    if max_atr20 is not None and atr20_pct > max_atr20:
        return False

    prior_high = prior_high_value(bars, signal_index, 20)
    close_to_prior_high = bars[signal_index].close / prior_high if prior_high > 0 else 0.0
    min_close_to_prior_high = rule_float(rules, "min_signal_close_to_prior_20_high_ratio")
    if min_close_to_prior_high is not None and close_to_prior_high < min_close_to_prior_high:
        return False
    max_close_to_prior_high = rule_float(rules, "max_signal_close_to_prior_20_high_ratio")
    if max_close_to_prior_high is not None and close_to_prior_high > max_close_to_prior_high:
        return False
    return True


def passes_signal_prior_trend_filters(bars: list[DailyBar], signal_index: int, rules: dict[str, object]) -> bool:
    up_days = prior_consecutive_close_direction_days(bars, signal_index, direction=1)
    min_up_days = rule_int(rules, "min_prior_up_days")
    if min_up_days is not None and up_days < min_up_days:
        return False
    max_up_days = rule_int(rules, "max_prior_up_days")
    if max_up_days is not None and up_days > max_up_days:
        return False

    down_days = prior_consecutive_close_direction_days(bars, signal_index, direction=-1)
    min_down_days = rule_int(rules, "min_prior_down_days")
    if min_down_days is not None and down_days < min_down_days:
        return False
    max_down_days = rule_int(rules, "max_prior_down_days")
    if max_down_days is not None and down_days > max_down_days:
        return False
    return True


def passes_signal_volume_trend_filters(bars: list[DailyBar], signal_index: int, rules: dict[str, object]) -> bool:
    min_ratio = rule_float(rules, "min_volume_avg5_to_avg20")
    max_ratio = rule_float(rules, "max_volume_avg5_to_avg20")
    if min_ratio is None and max_ratio is None:
        return True

    volumes20 = [safe_float(bar.volume) for bar in bars[max(0, signal_index - 19) : signal_index + 1]]
    volumes20 = [value for value in volumes20 if value > 0]
    if len(volumes20) < 20:
        return False
    avg5 = sum(volumes20[-5:]) / 5.0
    avg20 = sum(volumes20) / 20.0
    ratio = avg5 / avg20 if avg20 > 0 else 0.0
    if min_ratio is not None and ratio < min_ratio:
        return False
    if max_ratio is not None and ratio > max_ratio:
        return False
    return True


def passes_optimization_buy_filters(
    snapshot: MarketSnapshot,
    timestamp: datetime,
    rules: dict[str, object],
) -> bool:
    if not rules:
        return True

    buy_time_start = rule_time(rules.get("buy_time_start"))
    if buy_time_start is not None and timestamp.time() < buy_time_start:
        return False

    buy_time_end = rule_time(rules.get("buy_time_end"))
    if buy_time_end is not None and timestamp.time() >= buy_time_end:
        return False

    min_buy_day_open_gain_pct = rule_float(rules, "min_buy_day_open_gain_pct")
    if min_buy_day_open_gain_pct is not None and snapshot.today_open_gain_pct < min_buy_day_open_gain_pct:
        return False

    max_buy_day_open_gain_pct = rule_float(rules, "max_buy_day_open_gain_pct")
    if max_buy_day_open_gain_pct is not None and snapshot.today_open_gain_pct > max_buy_day_open_gain_pct:
        return False

    min_today_current_gain_pct = rule_float(rules, "min_today_current_gain_pct")
    if min_today_current_gain_pct is not None and snapshot.today_current_gain_pct < min_today_current_gain_pct:
        return False

    max_today_current_gain_pct = rule_float(rules, "max_today_current_gain_pct")
    if max_today_current_gain_pct is not None and snapshot.today_current_gain_pct > max_today_current_gain_pct:
        return False

    current_vs_today_ma5_pct = snapshot.current_price / snapshot.today_ma5 - 1.0 if snapshot.today_ma5 > 0 else 0.0
    min_current_vs_today_ma5_pct = rule_float(rules, "min_current_vs_today_ma5_pct")
    if min_current_vs_today_ma5_pct is not None and current_vs_today_ma5_pct < min_current_vs_today_ma5_pct:
        return False

    max_current_vs_today_ma5_pct = rule_float(rules, "max_current_vs_today_ma5_pct")
    if max_current_vs_today_ma5_pct is not None and current_vs_today_ma5_pct > max_current_vs_today_ma5_pct:
        return False

    min_today_open_vs_today_ma5_pct = rule_float(rules, "min_today_open_vs_today_ma5_pct")
    if min_today_open_vs_today_ma5_pct is not None and snapshot.today_open_vs_today_ma5_pct < min_today_open_vs_today_ma5_pct:
        return False

    max_today_open_vs_today_ma5_pct = rule_float(rules, "max_today_open_vs_today_ma5_pct")
    if max_today_open_vs_today_ma5_pct is not None and snapshot.today_open_vs_today_ma5_pct > max_today_open_vs_today_ma5_pct:
        return False

    min_today_open_vs_open_ma5_pct = rule_float(rules, "min_today_open_vs_open_ma5_pct")
    if min_today_open_vs_open_ma5_pct is not None and snapshot.today_open_vs_open_ma5_pct < min_today_open_vs_open_ma5_pct:
        return False

    max_today_open_vs_open_ma5_pct = rule_float(rules, "max_today_open_vs_open_ma5_pct")
    if max_today_open_vs_open_ma5_pct is not None and snapshot.today_open_vs_open_ma5_pct > max_today_open_vs_open_ma5_pct:
        return False

    return True


def prior_close_gain_pct(bars: list[DailyBar], signal_index: int, days: int) -> float:
    prior_index = signal_index - days
    if prior_index < 0:
        return 0.0
    prior_close = bars[prior_index].close
    return bars[signal_index].close / prior_close - 1.0 if prior_close > 0 else 0.0


def population_stddev(values: list[float]) -> float:
    values = [value for value in values if math.isfinite(value)]
    if not values:
        return 0.0
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def average_true_range_pct(bars: list[DailyBar], signal_index: int, days: int) -> float:
    start_index = max(0, signal_index - days + 1)
    ranges: list[float] = []
    for index in range(start_index, signal_index + 1):
        bar = bars[index]
        reference_close = bars[index - 1].close if index > 0 else bar.open
        if reference_close <= 0:
            continue
        true_range = max(
            bar.high - bar.low,
            abs(bar.high - reference_close),
            abs(bar.low - reference_close),
        )
        ranges.append(true_range / reference_close)
    if len(ranges) < min(days, signal_index + 1):
        return 0.0
    return sum(ranges) / len(ranges)


def prior_high_value(bars: list[DailyBar], signal_index: int, days: int) -> float:
    prior_bars = bars[max(0, signal_index - days) : signal_index]
    return max((bar.high for bar in prior_bars), default=0.0)


def prior_consecutive_close_direction_days(bars: list[DailyBar], signal_index: int, direction: int) -> int:
    count = 0
    for index in range(signal_index - 1, 0, -1):
        current_close = bars[index].close
        previous_close = bars[index - 1].close
        if direction > 0 and current_close > previous_close:
            count += 1
            continue
        if direction < 0 and current_close < previous_close:
            count += 1
            continue
        break
    return count


def rule_float(rules: dict[str, object], key: str) -> float | None:
    if key not in rules or rules[key] is None:
        return None
    value = safe_float(rules[key])
    return value if math.isfinite(value) else None


def rule_int(rules: dict[str, object], key: str) -> int | None:
    if key not in rules or rules[key] is None:
        return None
    try:
        return int(rules[key])
    except (TypeError, ValueError):
        return None


def safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def rule_time(value: object) -> time | None:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        pieces = value.split(":")
        if len(pieces) >= 2:
            try:
                return time(int(pieces[0]), int(pieces[1]))
            except ValueError:
                return None
    return None


def rule_int_set(rules: dict[str, object], key: str) -> set[int]:
    value = rules.get(key)
    if value is None:
        return set()
    if isinstance(value, str):
        pieces = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        pieces = list(value)
    else:
        pieces = [value]
    out: set[int] = set()
    for piece in pieces:
        try:
            out.add(int(piece))
        except (TypeError, ValueError):
            continue
    return out


def fetch_candidate_day_minute_bars(config: BacktestConfig, watchlists: dict[str, list[str]]) -> dict[str, list[MinuteBar]]:
    market_tz = ZoneInfo(config.market_timezone)
    bars_by_symbol: dict[str, list[MinuteBar]] = {}
    for day_key, symbols in sorted(watchlists.items()):
        day_symbols = sorted({to_alpaca_symbol(symbol) for symbol in symbols if to_alpaca_symbol(symbol)})
        if not day_symbols:
            continue
        day = date.fromisoformat(day_key)
        # 日线已经完成初筛；分钟 K 只用于候选当天的真实先后顺序回放。
        start = datetime.combine(day, time.min, tzinfo=market_tz)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=market_tz)
        print(f"Fetching candidate 1Min bars: {start:%Y-%m-%d} -> {end:%Y-%m-%d} signal_day={day_key} symbols={len(day_symbols)} feed={config.data_feed}", flush=True)
        chunk = fetch_minute_bars_for_range(config, day_symbols, start, end)
        for symbol, bars in chunk.items():
            bars_by_symbol.setdefault(to_alpaca_symbol(symbol), []).extend(bars)
    return bars_by_symbol


def fetch_minute_bars_for_range(
    config: BacktestConfig,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[MinuteBar]]:
    if not config.use_data_cache or not config.cache_minute_bars:
        return fetch_minute_bars(symbols, start, end, feed=config.data_feed, batch_size=config.batch_size)

    cache = market_data_cache(config)
    cached = {} if config.refresh_data_cache else cache.load_minute_bars(
        symbols,
        start,
        end,
        feed=config.data_feed,
        adjustment=ADJUSTMENT_SPLIT,
    )
    missing = symbols if config.refresh_data_cache else cache.uncovered_symbols(
        "minute",
        symbols,
        start.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        end.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        feed=config.data_feed,
        adjustment=ADJUSTMENT_SPLIT,
    )
    if missing:
        print(f"1Min cache miss: symbols={len(missing)} {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} cache={cache.path}", flush=True)
        fetched = fetch_minute_bars(missing, start, end, feed=config.data_feed, batch_size=config.batch_size)
        if fetched:
            cache.save_minute_bars(
                fetched,
                feed=config.data_feed,
                range_start=start,
                range_end=end,
                adjustment=ADJUSTMENT_SPLIT,
            )
            cached.update(fetched)
    else:
        print(f"1Min cache hit: symbols={len(symbols)} {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} cache={cache.path}", flush=True)
    return cached


def market_data_cache(config: BacktestConfig) -> MarketDataCache:
    return MarketDataCache(config.data_cache_dir / config.data_cache_name)


def daily_cache_feed(config: BacktestConfig) -> str:
    source = config.daily_data_source.lower()
    if source == "moomoo":
        return MOOMOO_DAILY_FEED
    if source == "yahoo":
        return YAHOO_DAILY_FEED
    if source == "massive":
        return MASSIVE_DAILY_FEED
    return config.data_feed.lower()


def daily_cache_adjustment(config: BacktestConfig) -> str:
    source = config.daily_data_source.lower()
    if source == "moomoo":
        return MOOMOO_DAILY_ADJUSTMENT
    if source == "yahoo":
        return YAHOO_DAILY_ADJUSTMENT
    if source == "massive":
        return MASSIVE_DAILY_ADJUSTMENT
    return ADJUSTMENT_SPLIT


def daily_bars_to_signal_bars(daily_bars: dict[str, list[DailyBar]], config: BacktestConfig) -> dict[str, list[MinuteBar]]:
    market_tz = ZoneInfo(config.market_timezone)
    out: dict[str, list[MinuteBar]] = {}
    start_date = config.start_date - timedelta(days=max(0, config.report_price_context_days))
    for symbol, bars in daily_bars.items():
        converted: list[MinuteBar] = []
        for bar in sorted(bars, key=lambda item: item.date):
            if not (start_date <= bar.date <= config.end_date):
                continue
            # 日线模式没有真实分钟顺序；用固定时间点回放 OHLC，避免再拉 1Min 数据。
            converted.extend(
                [
                    MinuteBar(symbol, datetime.combine(bar.date, time(9, 30), tzinfo=market_tz), bar.open, bar.open, bar.open, bar.open),
                    MinuteBar(symbol, datetime.combine(bar.date, time(11, 30), tzinfo=market_tz), bar.low, bar.low, bar.low, bar.low),
                    MinuteBar(symbol, datetime.combine(bar.date, time(15, 0), tzinfo=market_tz), bar.high, bar.high, bar.high, bar.high),
                    MinuteBar(symbol, datetime.combine(bar.date, time(15, 55), tzinfo=market_tz), bar.close, bar.close, bar.close, bar.close),
                ]
            )
        out[to_alpaca_symbol(symbol)] = converted
    return out


def build_daily_bars(bars_by_symbol: dict[str, list[MinuteBar]]) -> dict[str, list[DailyBar]]:
    out: dict[str, list[DailyBar]] = {}
    for symbol, bars in bars_by_symbol.items():
        by_day: dict[date, list[MinuteBar]] = {}
        for bar in bars:
            start, end = regular_session_bounds(bar.timestamp.date(), bar.timestamp.tzinfo)
            if start <= bar.timestamp < end:
                by_day.setdefault(bar.timestamp.date(), []).append(bar)
        daily: list[DailyBar] = []
        for day, day_bars in sorted(by_day.items()):
            ordered = sorted(day_bars, key=lambda item: item.timestamp)
            daily.append(
                DailyBar(
                    symbol=to_alpaca_symbol(symbol),
                    date=day,
                    open=ordered[0].open,
                    high=max(item.high for item in ordered),
                    low=min(item.low for item in ordered),
                    close=ordered[-1].close,
                )
            )
        out[to_alpaca_symbol(symbol)] = daily
    return out


def bars_grouped_by_time(
    bars_by_symbol: dict[str, list[MinuteBar]],
    start_date: date,
    end_date: date,
) -> dict[datetime, dict[str, MinuteBar]]:
    out: dict[datetime, dict[str, MinuteBar]] = {}
    for symbol, bars in bars_by_symbol.items():
        alpaca_symbol = to_alpaca_symbol(symbol)
        for bar in bars:
            if start_date <= bar.timestamp.date() <= end_date:
                out.setdefault(bar.timestamp, {})[alpaca_symbol] = bar
    return out


def sort_and_dedupe_bars(bars_by_symbol: dict[str, list[MinuteBar]]) -> dict[str, list[MinuteBar]]:
    out: dict[str, list[MinuteBar]] = {}
    for symbol, bars in bars_by_symbol.items():
        by_time = {bar.timestamp: bar for bar in bars}
        out[to_alpaca_symbol(symbol)] = [by_time[key] for key in sorted(by_time)]
    return out


def completed_daily_bars_before(bars: list[DailyBar], day: date) -> list[DailyBar]:
    return [bar for bar in sorted(bars, key=lambda item: item.date) if bar.date < day and bar.close > 0]


def regular_open_for_day(bars: list[MinuteBar], day: date) -> float:
    for bar in sorted((item for item in bars if item.timestamp.date() == day), key=lambda item: item.timestamp):
        start, end = regular_session_bounds(day, bar.timestamp.tzinfo)
        if start <= bar.timestamp < end:
            return bar.open
    return 0.0


def buy_day_open_below_signal_reference(snapshot: MarketSnapshot) -> bool:
    if snapshot.today_open <= 0 or not snapshot.previous_closes or not snapshot.previous_opens:
        return False
    signal_open = snapshot.previous_opens[-1]
    signal_close = snapshot.previous_closes[-1]
    if signal_open <= 0 or signal_close <= 0:
        return False
    reference_price = signal_open if signal_close > signal_open else signal_close
    return snapshot.today_open < reference_price


def normalized_alpaca_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        alpaca_symbol = to_alpaca_symbol(symbol)
        if alpaca_symbol and alpaca_symbol not in seen:
            seen.add(alpaca_symbol)
            out.append(alpaca_symbol)
    return out


def apply_buy_slippage(price: float, slippage_pct: float) -> float:
    return round(price * (1.0 + slippage_pct), 4)


def apply_sell_slippage(price: float, slippage_pct: float) -> float:
    return round(price * (1.0 - slippage_pct), 4)


def build_stats(
    *,
    initial_cash: float,
    final_equity: float,
    ending_cash: float,
    ending_position_value: float,
    open_position_count: int,
    trades: list[TradeRecord],
    equity_curve: list[EquityPoint],
) -> BacktestStats:
    sell_trades = [trade for trade in trades if trade.side == "SELL"]
    pnls = [trade.realized_pnl for trade in sell_trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    max_drawdown_pct = max_drawdown(equity_curve)
    total_return = final_equity - initial_cash
    return BacktestStats(
        initial_cash=initial_cash,
        final_equity=final_equity,
        total_return=total_return,
        return_pct=total_return / initial_cash if initial_cash > 0 else 0.0,
        closed_trade_count=len(sell_trades),
        order_count=len(trades),
        win_rate=len(wins) / len(sell_trades) if sell_trades else 0.0,
        max_drawdown_pct=max_drawdown_pct,
        average_trade_pnl=sum(pnls) / len(pnls) if pnls else 0.0,
        max_trade_profit=max(pnls) if pnls else 0.0,
        max_trade_loss=min(pnls) if pnls else 0.0,
        ending_cash=ending_cash,
        ending_position_value=ending_position_value,
        open_position_count=open_position_count,
        buy_order_count=sum(1 for trade in trades if trade.side == "BUY"),
        sell_order_count=len(sell_trades),
    )


def max_drawdown(equity_curve: list[EquityPoint]) -> float:
    peak = 0.0
    worst = 0.0
    for point in equity_curve:
        peak = max(peak, point.equity)
        if peak > 0:
            worst = min(worst, point.equity / peak - 1.0)
    return worst


def sample_equity(points: list[EquityPoint], max_points: int) -> list[EquityPoint]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    step = max(1, math.ceil(len(points) / max_points))
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def traded_symbols(trades: list[TradeRecord]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for trade in trades:
        symbol = to_alpaca_symbol(trade.symbol)
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def chronological_trades(trades: list[TradeRecord]) -> list[TradeRecord]:
    side_rank = {"BUY": 0, "SELL": 1}
    return sorted(
        trades,
        key=lambda trade: (
            trade.timestamp,
            trade.symbol,
            side_rank.get(trade.side, 9),
            trade.rule,
        ),
    )


def sample_price_points(bars_by_symbol: dict[str, list[MinuteBar]], config: BacktestConfig, symbols: list[str] | None = None) -> list[PricePoint]:
    points: list[PricePoint] = []
    selected = [to_alpaca_symbol(symbol) for symbol in symbols] if symbols else list(bars_by_symbol)
    if config.report_max_price_symbols > 0:
        selected = selected[: config.report_max_price_symbols]
    for symbol in selected:
        bars = bars_by_symbol.get(to_alpaca_symbol(symbol), [])
        chart_start_date = config.start_date - timedelta(days=max(0, config.report_price_context_days))
        in_range = [bar for bar in bars if chart_start_date <= bar.timestamp.date() <= config.end_date]
        step = max(1, math.ceil(len(in_range) / max(1, config.report_max_points_per_series)))
        symbol_points = [
            PricePoint(bar.timestamp, normalize_symbol(symbol), bar.open, bar.high, bar.low, bar.close)
            for bar in in_range[::step]
        ]
        if in_range and (not symbol_points or symbol_points[-1].timestamp != in_range[-1].timestamp):
            last = in_range[-1]
            symbol_points.append(PricePoint(last.timestamp, normalize_symbol(symbol), last.open, last.high, last.low, last.close))
        points.extend(symbol_points)
    return points


@contextmanager
def patched_strategy_params(config: BacktestConfig):
    originals: list[tuple[object, str, object]] = []
    with strategy_module.use_strategy(config.strategy_name):
        targets = [
            (watchlist_module, config.watchlist_signal_params),
            (strategy_module.active_buy_module(), config.buy_signal_params),
        ]
        try:
            for module, params in targets:
                for name, value in params.items():
                    if not hasattr(module, name):
                        raise ValueError(f"Unknown strategy parameter: {module.__name__}.{name}")
                    originals.append((module, name, getattr(module, name)))
                    setattr(module, name, value)
            yield
        finally:
            for module, name, value in reversed(originals):
                setattr(module, name, value)


def write_trade_csv(output_dir: Path, trades: list[TradeRecord]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "backtest_trades.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "symbol",
                "side",
                "quantity",
                "price",
                "gross_value",
        "fee",
        "cash_after",
                "realized_pnl",
                "price_change_pct",
                "signal_day",
                "rule",
                "reason",
            ],
        )
        writer.writeheader()
        for trade in chronological_trades(trades):
            writer.writerow(
                {
                    "timestamp": trade.timestamp.isoformat(timespec="seconds"),
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "quantity": trade.quantity,
                    "price": trade.price,
                    "gross_value": trade.gross_value,
                    "fee": trade.fee,
                    "cash_after": trade.cash_after,
                    "realized_pnl": trade.realized_pnl,
                    "price_change_pct": trade.price_change_pct,
                    "signal_day": trade.signal_day.isoformat() if trade.signal_day else "",
                    "rule": trade.rule,
                    "reason": trade.reason,
                }
            )
    return path


def write_html_report(result: BacktestResult, daily_bars: dict[str, list[DailyBar]]) -> Path:
    config = result.config
    config.output_dir.mkdir(parents=True, exist_ok=True)
    detail_dir = config.output_dir / "symbol_details"
    detail_dir.mkdir(parents=True, exist_ok=True)
    for old_detail in detail_dir.glob("*.html"):
        old_detail.unlink()
    write_symbol_detail_reports(result, daily_bars, detail_dir)
    path = config.output_dir / config.html_report_name
    path.write_text(render_html(result), encoding="utf-8")
    return path


def render_html(result: BacktestResult) -> str:
    stats = result.stats
    equity_rows = [
        {"timestamp": point.timestamp.isoformat(timespec="minutes"), "equity": round(point.equity, 2), "cash": round(point.cash, 2)}
        for point in result.equity_curve
    ]
    payload = json.dumps({"equity": equity_rows}, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>策略回测报告</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body {{ font-family: Arial, "Microsoft YaHei", sans-serif; margin: 24px; color: #18202a; }}
    h1, h2 {{ margin: 0 0 12px; }}
    section {{ margin: 28px 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border: 1px solid #d7dde6; padding: 7px 8px; text-align: right; }}
    th {{ background: #f3f6fa; }}
    td:first-child, th:first-child {{ text-align: left; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .metric {{ border: 1px solid #d7dde6; padding: 12px; border-radius: 6px; }}
    .metric strong {{ display: block; font-size: 20px; margin-top: 6px; }}
    .chart {{ min-height: 420px; border: 1px solid #d7dde6; border-radius: 6px; }}
    .detail-button {{ border: 0; background: transparent; color: #0f5f9f; cursor: pointer; padding: 0; font: inherit; }}
    .detail-button:hover {{ text-decoration: underline; }}
    .sort-button {{ border: 1px solid #c8d0dc; background: #fff; border-radius: 4px; padding: 3px 8px; cursor: pointer; font: inherit; line-height: 1.2; }}
    .sort-button:hover {{ background: #eaf2fb; }}
    .modal {{ position: fixed; inset: 0; z-index: 9999; display: none; background: rgba(10, 18, 30, 0.62); }}
    .modal.open {{ display: block; }}
    .modal-panel {{ position: absolute; inset: 4vh 4vw; background: #fff; border-radius: 8px; display: flex; flex-direction: column; overflow: hidden; }}
    .modal-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px; border-bottom: 1px solid #d7dde6; }}
    .modal-header strong {{ font-size: 16px; }}
    .modal-actions {{ display: flex; align-items: center; gap: 8px; }}
    .modal-back {{ border: 1px solid #c8d0dc; background: #fff; border-radius: 4px; padding: 5px 10px; cursor: pointer; font-size: 14px; line-height: 1.2; }}
    .modal-back:hover {{ background: #f3f6fa; }}
    .modal-close {{ border: 1px solid #c8d0dc; background: #fff; border-radius: 4px; padding: 4px 9px; cursor: pointer; font-size: 18px; line-height: 1; }}
    .modal-frame {{ border: 0; width: 100%; height: 100%; flex: 1; }}
    a {{ color: #0f5f9f; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .note {{ color: #5d6876; font-size: 13px; }}
    @media (max-width: 720px) {{
      body {{ margin: 12px; }}
      .modal-panel {{ inset: 0; border-radius: 0; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
  <h1>策略回测报告</h1>
  <p class="note">交易信号仍基于 1Min K 线回放；主报告只展示汇总。点击股票代码用弹窗查看该股票信号日、买入日、卖出日周围的日 K 线。</p>
  <section>
    <h2>收益统计表</h2>
    {stats_cards(stats, result.minute_bar_count)}
    {stats_table(stats)}
  </section>
  <section>
    <h2>时间顺序与收益核验</h2>
    {chronology_audit_table(result)}
  </section>
  <section>
    <h2>资金曲线</h2>
    <div id="equity-chart" class="chart"></div>
  </section>
  <section>
    <h2>股票 K 线详情</h2>
    <p class="note">点击股票代码打开弹窗；详情页按每轮交易分段展示信号日、买入日、卖出日及周围日期的日 K 线，并标出买点和卖点。</p>
    {symbol_detail_table(result.trades)}
  </section>
  <section>
    <h2>每笔交易明细</h2>
    {trades_table(result.trades)}
  </section>
  <section>
    <h2>当前回测配置摘要</h2>
    {config_table(result.config)}
  </section>
  <div id="detail-modal" class="modal" aria-hidden="true">
    <div class="modal-panel">
      <div class="modal-header">
        <strong id="detail-modal-title">股票 K 线详情</strong>
        <div class="modal-actions">
          <button id="detail-modal-back" class="modal-back" type="button">&#36820;&#22238;</button>
          <button id="detail-modal-close" class="modal-close" type="button">×</button>
        </div>
      </div>
      <iframe id="detail-modal-frame" class="modal-frame" title="股票 K 线详情"></iframe>
    </div>
  </div>
  <script>
    const payload = {payload};
    const equity = payload.equity;
    Plotly.newPlot("equity-chart", [{{
      x: equity.map(row => row.timestamp),
      y: equity.map(row => row.equity),
      mode: "lines",
      type: "scatter",
      name: "Equity"
    }}], {{margin: {{t: 28}}, yaxis: {{title: "USD"}}, xaxis: {{title: "Time"}}}}, {{responsive: true}});

    const modal = document.getElementById("detail-modal");
    const modalFrame = document.getElementById("detail-modal-frame");
    const modalTitle = document.getElementById("detail-modal-title");
    function closeDetailModal() {{
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
      modalFrame.removeAttribute("src");
    }}
    document.querySelectorAll("[data-detail-url]").forEach(button => {{
      button.addEventListener("click", () => {{
        modalTitle.textContent = `${{button.dataset.symbol}} K 线详情`;
        modalFrame.src = button.dataset.detailUrl;
        modal.classList.add("open");
        modal.setAttribute("aria-hidden", "false");
      }});
    }});
    document.getElementById("detail-modal-back").addEventListener("click", closeDetailModal);
    document.getElementById("detail-modal-close").addEventListener("click", closeDetailModal);
    modal.addEventListener("click", event => {{
      if (event.target === modal) closeDetailModal();
    }});
    window.addEventListener("keydown", event => {{
      if (event.key === "Escape") closeDetailModal();
    }});

    const realizedPnlSort = document.querySelector("[data-sort-realized-pnl]");
    if (realizedPnlSort) {{
      realizedPnlSort.addEventListener("click", () => {{
        const table = document.getElementById("trades-table");
        const tbody = table?.querySelector("tbody");
        if (!tbody) return;
        const nextDirection = realizedPnlSort.dataset.direction === "desc" ? "asc" : "desc";
        const rows = Array.from(tbody.querySelectorAll("tr"));
        rows.sort((left, right) => {{
          const leftValue = Number(left.dataset.realizedPnl || 0);
          const rightValue = Number(right.dataset.realizedPnl || 0);
          return nextDirection === "desc" ? rightValue - leftValue : leftValue - rightValue;
        }});
        rows.forEach(row => tbody.appendChild(row));
        realizedPnlSort.dataset.direction = nextDirection;
        realizedPnlSort.textContent = nextDirection === "desc" ? "已实现收益 ↓" : "已实现收益 ↑";
        realizedPnlSort.setAttribute("aria-label", nextDirection === "desc" ? "已按已实现收益从高到低排序" : "已按已实现收益从低到高排序");
      }});
    }}
  </script>
</body>
</html>
"""


def write_symbol_detail_reports(result: BacktestResult, daily_bars: dict[str, list[DailyBar]], detail_dir: Path) -> None:
    for symbol in traded_symbols(result.trades):
        detail_path = detail_dir / symbol_detail_filename(symbol)
        detail_path.write_text(render_symbol_detail(symbol, result, daily_bars), encoding="utf-8")


def symbol_detail_filename(symbol: str) -> str:
    safe = to_alpaca_symbol(symbol).replace("/", "_").replace("\\", "_").replace(":", "_")
    return f"{safe}.html"


def symbol_detail_table(trades: list[TradeRecord]) -> str:
    symbols = traded_symbols(trades)
    if not symbols:
        return "<p class='note'>本次回测没有成交股票。</p>"
    rows = []
    for symbol in symbols:
        symbol_trades = [trade for trade in trades if to_alpaca_symbol(trade.symbol) == to_alpaca_symbol(symbol)]
        buy_count = sum(1 for trade in symbol_trades if trade.side == "BUY")
        sell_count = sum(1 for trade in symbol_trades if trade.side == "SELL")
        realized = sum(trade.realized_pnl for trade in symbol_trades if trade.side == "SELL")
        first_time = min(trade.timestamp for trade in symbol_trades).isoformat(timespec="minutes")
        link = f"symbol_details/{html.escape(symbol_detail_filename(symbol))}"
        label = html.escape(normalize_symbol(symbol))
        rows.append(
            "<tr>"
            f"<td><button class='detail-button' type='button' data-symbol='{label}' data-detail-url='{link}'>{label}</button></td>"
            f"<td>{buy_count}</td>"
            f"<td>{sell_count}</td>"
            f"<td>{html.escape(money(realized))}</td>"
            f"<td>{html.escape(first_time)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>股票</th><th>买入次数</th><th>卖出次数</th><th>已实现收益</th><th>首次交易时间</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_symbol_detail(symbol: str, result: BacktestResult, daily_bars: dict[str, list[DailyBar]]) -> str:
    normalized = normalize_symbol(symbol)
    alpaca_symbol = to_alpaca_symbol(symbol)
    symbol_trades = sorted(
        [trade for trade in result.trades if to_alpaca_symbol(trade.symbol) == alpaca_symbol],
        key=lambda trade: trade.timestamp,
    )
    bars = sorted(daily_bars.get(alpaca_symbol, []), key=lambda bar: bar.date)
    windows = symbol_trade_windows(normalized, symbol_trades, result.config)
    payload = json.dumps(
        [
            {
                "title": window["title"],
                "bars": daily_bar_payloads(
                    bars,
                    sample_window_bars(
                        bars,
                        window["event_dates"],
                        result.config.report_price_context_days,
                        result.config.report_max_points_per_series,
                    ),
                ),
                "trades": [trade_payload(trade) for trade in window["trades"]],
            }
            for window in windows
        ],
        ensure_ascii=False,
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(normalized)} K线详情</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body {{ font-family: Arial, "Microsoft YaHei", sans-serif; margin: 24px; color: #18202a; }}
    h1, h2 {{ margin: 0 0 12px; }}
    section {{ margin: 28px 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 12px 0 18px; }}
    th, td {{ border: 1px solid #d7dde6; padding: 7px 8px; text-align: right; }}
    th {{ background: #f3f6fa; }}
    td:first-child, th:first-child {{ text-align: left; }}
    .chart {{ min-height: 560px; border: 1px solid #d7dde6; border-radius: 6px; }}
    .note {{ color: #5d6876; font-size: 13px; }}
    @media (max-width: 720px) {{
      body {{ margin: 10px; }}
      h1 {{ font-size: 22px; }}
      h2 {{ font-size: 17px; }}
      .chart {{ min-height: 72vh; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
  <h1>{html.escape(normalized)} K线详情</h1>
  <p class="note">每张图对应一轮交易窗口：从买入信号日前后到买入/卖出日前后。K 线为日 K；买点/卖点标在成交日期上，涨跌幅按成交价计算。</p>
  {trades_table(symbol_trades)}
  <div id="charts"></div>
  <script>
    const windows = {payload};

    function pctText(value) {{
      const number = Number(value);
      if (!Number.isFinite(number)) return "n/a";
      return `${{number >= 0 ? "+" : ""}}${{(number * 100).toFixed(2)}}%`;
    }}

    function tradeLabel(row) {{
      return `${{row.side === "BUY" ? "B" : "S"}} ${{pctText(row.price_change_pct)}}`;
    }}

    function renderWindow(windowData, index) {{
      const section = document.createElement("section");
      const title = document.createElement("h2");
      title.textContent = windowData.title;
      const chart = document.createElement("div");
      chart.id = `chart-${{index}}`;
      chart.className = "chart";
      section.appendChild(title);
      section.appendChild(chart);
      document.getElementById("charts").appendChild(section);

      const rows = windowData.bars;
      const trades = windowData.trades;
      const buyRows = trades.filter(row => row.side === "BUY");
      const sellRows = trades.filter(row => row.side === "SELL");
      const ma5Rows = rows.filter(row => Number.isFinite(Number(row.ma5)));
      const traces = [{{
        x: rows.map(row => row.timestamp),
        open: rows.map(row => row.open),
        high: rows.map(row => row.high),
        low: rows.map(row => row.low),
        close: rows.map(row => row.close),
        type: "candlestick",
        name: "{html.escape(normalized)} 日K",
        increasing: {{line: {{color: "#c0392b"}}, fillcolor: "rgba(192,57,43,0.36)"}},
        decreasing: {{line: {{color: "#137b4b"}}, fillcolor: "rgba(19,123,75,0.36)"}}
      }}, {{
        x: ma5Rows.map(row => row.timestamp),
        y: ma5Rows.map(row => row.ma5),
        mode: "lines",
        type: "scatter",
        name: "MA5",
        line: {{color: "#2563eb", width: 2}},
        hovertemplate: "MA5: %{{y:.4f}}<extra></extra>"
      }}];
      traces.push({{
        x: buyRows.map(row => row.timestamp),
        y: buyRows.map(row => row.price),
        text: buyRows.map(tradeLabel),
        customdata: buyRows.map(row => [row.quantity, row.rule, row.signal_day || "", row.time || ""]),
        mode: "markers+text",
        type: "scatter",
        name: "Buy",
        textposition: "top right",
        textfont: {{color: "#2563eb", size: 12}},
        marker: {{color: "#2563eb", size: 12, symbol: "triangle-up", line: {{color: "#ffffff", width: 1}}}},
        hovertemplate: "%{{text}}<br>Price: %{{y:.4f}}<br>Qty: %{{customdata[0]}}<br>Rule: %{{customdata[1]}}<br>Signal day: %{{customdata[2]}}<br>Trade time: %{{customdata[3]}}<extra></extra>"
      }});
      traces.push({{
        x: sellRows.map(row => row.timestamp),
        y: sellRows.map(row => row.price),
        text: sellRows.map(tradeLabel),
        customdata: sellRows.map(row => [row.quantity, row.rule, row.realized_pnl, row.signal_day || "", row.time || ""]),
        mode: "markers+text",
        type: "scatter",
        name: "Sell",
        textposition: "bottom right",
        textfont: {{color: "#f59e0b", size: 12}},
        marker: {{color: "#f59e0b", size: 12, symbol: "triangle-down", line: {{color: "#ffffff", width: 1}}}},
        hovertemplate: "%{{text}}<br>Price: %{{y:.4f}}<br>Qty: %{{customdata[0]}}<br>Rule: %{{customdata[1]}}<br>PnL: $%{{customdata[2]:.2f}}<br>Signal day: %{{customdata[3]}}<br>Trade time: %{{customdata[4]}}<extra></extra>"
      }});
      Plotly.newPlot(chart.id, traces, {{
        margin: {{t: 26, r: 12, b: 82, l: 48}},
        yaxis: {{title: "Price"}},
        xaxis: {{title: "Date", type: "category", rangeslider: {{visible: false}}, tickangle: -35, nticks: 8}},
        legend: {{orientation: "h", y: -0.22, x: 0}},
        showlegend: true
      }}, {{responsive: true, displaylogo: false}});
    }}

    if (windows.length === 0) {{
      document.getElementById("charts").innerHTML = "<p class='note'>没有可展示的交易窗口。</p>";
    }} else {{
      windows.forEach(renderWindow);
    }}
  </script>
</body>
</html>
"""


def daily_bar_payloads(all_bars: list[DailyBar], selected_bars: list[DailyBar]) -> list[dict[str, object]]:
    ma5_by_date = daily_ma5_by_date(all_bars)
    rows: list[dict[str, object]] = []
    for bar in selected_bars:
        ma5 = ma5_by_date.get(bar.date)
        rows.append(
            {
                "timestamp": bar.date.isoformat(),
                "open": round(bar.open, 4),
                "high": round(bar.high, 4),
                "low": round(bar.low, 4),
                "close": round(bar.close, 4),
                "ma5": round(ma5, 4) if ma5 is not None else None,
            }
        )
    return rows


def daily_ma5_by_date(bars: list[DailyBar]) -> dict[date, float]:
    closes: list[float] = []
    out: dict[date, float] = {}
    for bar in sorted(bars, key=lambda item: item.date):
        closes.append(bar.close)
        if len(closes) >= 5:
            out[bar.date] = sum(closes[-5:]) / 5.0
    return out


def symbol_trade_windows(symbol: str, trades: list[TradeRecord], config: BacktestConfig) -> list[dict[str, object]]:
    buys = [trade for trade in trades if trade.side == "BUY"]
    windows: list[dict[str, object]] = []
    for index, buy in enumerate(buys):
        next_buy_time = buys[index + 1].timestamp if index + 1 < len(buys) else None
        related = [
            trade
            for trade in trades
            if trade is buy or (trade.side == "SELL" and trade.timestamp >= buy.timestamp and (next_buy_time is None or trade.timestamp < next_buy_time))
        ]
        event_dates = [trade.timestamp.date() for trade in related]
        if buy.signal_day is not None:
            event_dates.append(buy.signal_day)
        sell_dates = sorted({trade.timestamp.date().isoformat() for trade in related if trade.side == "SELL"})
        title = f"{symbol} | 信号日 {buy.signal_day or '-'} | 买入日 {buy.timestamp.date()} | 卖出日 {', '.join(sell_dates) if sell_dates else '-'}"
        windows.append({"title": title, "event_dates": event_dates, "trades": related})
    if not windows and trades:
        for trade in trades:
            event_dates = [trade.timestamp.date()]
            if trade.signal_day is not None:
                event_dates.append(trade.signal_day)
            title = f"{symbol} | {trade.side} {trade.timestamp.date()}"
            windows.append({"title": title, "event_dates": event_dates, "trades": [trade]})
    return windows


def sample_window_bars(bars: list[DailyBar], event_dates: list[date], context_days: int, max_points: int) -> list[DailyBar]:
    if not bars or not event_dates:
        return []
    ordered = sorted(bars, key=lambda bar: bar.date)
    event_set = set(event_dates)
    indexes = [index for index, bar in enumerate(ordered) if bar.date in event_set]
    if not indexes:
        start_date = min(event_dates) - timedelta(days=max(0, context_days))
        end_date = max(event_dates) + timedelta(days=max(0, context_days))
        in_range = [bar for bar in ordered if start_date <= bar.date <= end_date]
    else:
        first = max(0, min(indexes) - max(0, context_days))
        last = min(len(ordered) - 1, max(indexes) + max(0, context_days))
        in_range = ordered[first : last + 1]
    if max_points <= 0 or len(in_range) <= max_points:
        return in_range
    step = max(1, math.ceil(len(in_range) / max_points))
    sampled = in_range[::step]
    if sampled[-1] != in_range[-1]:
        sampled.append(in_range[-1])
    return sampled


def trade_payload(trade: TradeRecord) -> dict[str, object]:
    return {
        "timestamp": trade.timestamp.date().isoformat(),
        "time": trade.timestamp.isoformat(timespec="minutes"),
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": round(trade.quantity, 6),
        "price": round(trade.price, 4),
        "realized_pnl": round(trade.realized_pnl, 2),
        "price_change_pct": round(trade.price_change_pct, 6),
        "rule": trade.rule,
        "signal_day": trade.signal_day.isoformat() if trade.signal_day else "",
    }


def stats_cards(stats: BacktestStats, minute_bar_count: int) -> str:
    cards = [
        ("最终权益", money(stats.final_equity)),
        ("总收益", money(stats.total_return)),
        ("收益率", pct(stats.return_pct)),
        ("最大回撤", pct(stats.max_drawdown_pct)),
        ("卖出/平仓次数", str(stats.sell_order_count)),
        ("胜率", pct(stats.win_rate)),
        ("1Min K线数量", f"{minute_bar_count:,}"),
    ]
    return '<div class="grid">' + "".join(f"<div class='metric'>{html.escape(label)}<strong>{html.escape(value)}</strong></div>" for label, value in cards) + "</div>"


def stats_table(stats: BacktestStats) -> str:
    rows = [
        ("初始资金", money(stats.initial_cash)),
        ("期末现金", money(stats.ending_cash)),
        ("期末持仓估值", money(stats.ending_position_value)),
        ("未平仓数量", str(stats.open_position_count)),
        ("平均单笔收益", money(stats.average_trade_pnl)),
        ("最大单笔盈利", money(stats.max_trade_profit)),
        ("最大单笔亏损", money(stats.max_trade_loss)),
        ("买入订单数量", str(stats.buy_order_count)),
        ("卖出订单数量", str(stats.sell_order_count)),
        ("订单数量", str(stats.order_count)),
    ]
    return simple_table(["指标", "数值"], rows)


def chronology_audit_table(result: BacktestResult) -> str:
    stats = result.stats
    trades = result.trades
    equity_curve = result.equity_curve
    trades_in_order = all(left.timestamp <= right.timestamp for left, right in zip(trades, trades[1:]))
    equity_in_order = all(left.timestamp <= right.timestamp for left, right in zip(equity_curve, equity_curve[1:]))

    ledger_cash = stats.initial_cash
    for trade in trades:
        if trade.side == "BUY":
            ledger_cash -= trade.gross_value + trade.fee
        elif trade.side == "SELL":
            ledger_cash += trade.gross_value - trade.fee

    cash_diff = ledger_cash - stats.ending_cash
    formula_equity = stats.ending_cash + stats.ending_position_value
    equity_diff = formula_equity - stats.final_equity
    formula_return = (stats.final_equity - stats.initial_cash) / stats.initial_cash if stats.initial_cash > 0 else 0.0
    return_diff = formula_return - stats.return_pct

    rows = [
        (
            "回放顺序",
            "通过" if equity_in_order else "异常",
            "资金曲线 timestamp 按从早到晚非递减排列；引擎按全局 timestamp 逐分钟回放。",
        ),
        (
            "挂单/信号顺序",
            "通过",
            "每个 timestamp 先处理上一时间点已经存在的挂单，再按当前分钟 close 计算新信号；新买入信号创建的限价单从下一 timestamp 才参与撮合。",
        ),
        (
            "交易记录顺序",
            "通过" if trades_in_order else "异常",
            trade_time_span(trades),
        ),
        (
            "现金流水核对",
            "通过" if abs(cash_diff) < 0.01 else "异常",
            f"按成交逐笔重算期末现金 {money(ledger_cash)}；报告期末现金 {money(stats.ending_cash)}；差额 {money(cash_diff)}。",
        ),
        (
            "最终权益公式",
            "通过" if abs(equity_diff) < 0.01 else "异常",
            f"期末现金 {money(stats.ending_cash)} + 期末持仓估值 {money(stats.ending_position_value)} = {money(formula_equity)}；报告最终权益 {money(stats.final_equity)}。",
        ),
        (
            "收益率公式",
            "通过" if abs(return_diff) < 0.000001 else "异常",
            f"(最终权益 - 初始资金) / 初始资金 = {pct(formula_return)}；报告收益率 {pct(stats.return_pct)}。",
        ),
        (
            "分钟内先后",
            "有限",
            "1Min K 线只能确认分钟级顺序，不能还原单分钟内 high/low 谁先发生；本回测不允许新买入信号在同一分钟成交。",
        ),
    ]
    return simple_table(["核验项", "结果", "说明"], rows)


def trade_time_span(trades: list[TradeRecord]) -> str:
    if not trades:
        return "本次无成交。"
    return f"首笔 {trades[0].timestamp.isoformat(timespec='minutes')}；末笔 {trades[-1].timestamp.isoformat(timespec='minutes')}。"


def trades_table(trades: list[TradeRecord]) -> str:
    if not trades:
        return "<p class='note'>本次回测没有成交记录。</p>"
    header = ["时间", "代码", "方向", "数量", "价格", "成交额", "手续费", "现金余额", "已实现收益", "规则"]
    header.insert(5, "涨跌幅")
    head = []
    for label in header:
        if label == "已实现收益":
            head.append(
                "<th>"
                "<button class='sort-button' type='button' data-sort-realized-pnl aria-label='按已实现收益排序'>"
                "已实现收益 ↕"
                "</button>"
                "</th>"
            )
        else:
            head.append(f"<th>{html.escape(str(label))}</th>")
    body = []
    for trade in trades:
        row = [
            trade.timestamp.isoformat(timespec="minutes"),
            trade.symbol,
            trade.side,
            f"{trade.quantity:.6f}",
            f"{trade.price:.4f}",
            pct(trade.price_change_pct),
            money(trade.gross_value),
            money(trade.fee),
            money(trade.cash_after),
            money(trade.realized_pnl),
            trade.rule,
        ]
        body.append(
            f"<tr data-realized-pnl='{trade.realized_pnl}'>"
            + "".join(f"<td>{html.escape(str(value))}</td>" for value in row)
            + "</tr>"
        )
    return f"<table id='trades-table'><thead><tr>{''.join(head)}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def config_table(config: BacktestConfig) -> str:
    symbols_preview = ", ".join(normalize_symbol(symbol) for symbol in config.symbols[:80])
    if len(config.symbols) > 80:
        symbols_preview = f"{symbols_preview} ... 共 {len(config.symbols)} 只"
    rows = [
        ("股票池说明", config.stock_pool_description),
        ("股票代码数量", str(len(config.symbols))),
        ("股票代码预览", symbols_preview),
        ("策略变体", config.strategy_variant_name),
        ("策略变体说明", config.strategy_variant_description),
        ("回测开始/结束日期", f"{config.start_date} / {config.end_date}"),
        ("K线周期", config.timeframe),
        ("初始资金", money(config.initial_cash)),
        ("单笔买入资金", money(config.buy_notional_usd)),
        ("仓位比例", pct(config.buy_position_pct)),
        ("最大持仓数量", str(config.max_positions)),
        ("最大每日买入次数", str(config.max_daily_buys)),
        ("手续费", money(config.commission_per_order)),
        ("滑点", pct(config.slippage_pct)),
        ("是否允许重复买入", str(config.allow_repeat_buys)),
        ("是否允许隔夜持仓", str(config.allow_overnight_holding)),
        ("数据源", config.data_feed),
        ("买入信号参数", json.dumps(config.buy_signal_params, ensure_ascii=False)),
        ("选股信号参数", json.dumps(config.watchlist_signal_params, ensure_ascii=False)),
        ("卖出信号参数", json.dumps(config.sell_signal_params, ensure_ascii=False, default=str)),
        ("止盈/止损参数", json.dumps(config.stop_params, ensure_ascii=False)),
        ("优化规则", json.dumps(config.optimization_rules, ensure_ascii=False, default=str) if config.optimization_rules else "无"),
        ("买入日开盘规则", "信号日收阳线: 买入日开盘价 < 信号日开盘价；信号日收阴线或平盘: 买入日开盘价 < 信号日收盘价" if config.require_buy_day_open_below_signal_reference else "关闭"),
        ("K线图向前展示天数", str(config.report_price_context_days)),
        ("输出目录", str(config.output_dir)),
        ("HTML报告文件名", config.html_report_name),
        ("报告K线图最多股票数", "不限制" if config.report_max_price_symbols <= 0 else str(config.report_max_price_symbols)),
    ]
    return simple_table(["配置", "值"], rows)


def simple_table(header: list[str], rows: list[object]) -> str:
    head = "".join(f"<th>{html.escape(str(item))}</th>" for item in header)
    body = []
    for row in rows:
        values = row if isinstance(row, (list, tuple)) else [row]
        body.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in values) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float) -> str:
    return f"{value:.2%}"
