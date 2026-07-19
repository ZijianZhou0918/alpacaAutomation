from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, time
from typing import Any, Callable

import numpy as np
import pandas as pd

from intraday_top20.data.loader import MarketDataLoader

from .config import BacktestConfig
from .indicators import add_intraday_indicators
from .metrics import add_equity_diagnostics, calculate_metrics, risk_diagnostics
from .models import Signal
from .portfolio import Portfolio
from .result import BacktestResult
from .strategy import SignalStateMachine
from .universe import dynamic_top_gainers

ProgressCallback = Callable[[str, int, int, str], None]


def run_backtest(config: BacktestConfig, progress: ProgressCallback | None = None) -> BacktestResult:
    return IntradayTopGainersBacktester(config).run(progress)


class IntradayTopGainersBacktester:
    """Event-driven five-minute engine; every decision uses only the completed current bar or earlier."""

    def __init__(self, config: BacktestConfig, loader: MarketDataLoader | None = None):
        config.validate()
        self.config = config
        self.loader = loader or MarketDataLoader(config)

    def run(self, progress: ProgressCallback | None = None) -> BacktestResult:
        run_id = self.config.config_hash(self.loader.fingerprint())
        portfolio = Portfolio(self.config)
        pending: dict[datetime, list[Signal]] = defaultdict(list)
        equity_records: list[dict[str, Any]] = []
        daily_records: list[dict[str, Any]] = []
        ranking_records: list[dict[str, Any]] = []
        signal_records: list[dict[str, Any]] = []
        audit_bar_frames: list[pd.DataFrame] = []
        state_records: list[dict[str, Any]] = []
        daily_analysis: list[dict[str, Any]] = []
        last_marks: dict[str, float] = {}
        previous_day_equity = self.config.portfolio.initial_capital
        latest_entry = time.fromisoformat(self.config.strategy.latest_entry_time)
        force_exit = time.fromisoformat(self.config.execution.force_exit_time)
        total = sum(1 for _, day in self.loader.files if self.config.data.start_date <= day.isoformat() <= self.config.data.end_date)

        for day_number, daily in enumerate(self.loader.iter_days(), start=1):
            if progress:
                progress("backtest", day_number, max(total, 1), f"处理 {daily.trade_date.isoformat()}")
            bars = add_intraday_indicators(daily.bars, self.config.strategy)
            states: dict[str, SignalStateMachine] = {}
            current_top_entry: dict[str, datetime] = {}
            emitted_symbols: set[str] = set()
            day_signal_start = len(signal_records)
            day_reject_start = len(portfolio.rejections)
            day_trade_start = len(portfolio.all_positions)
            day_equity: list[float] = [previous_day_equity]
            rank_lookup: dict[tuple[pd.Timestamp, str], int] = {}

            for timestamp, current in bars.groupby("timestamp", sort=True, observed=True):
                timestamp = pd.Timestamp(timestamp)
                current = current.sort_values("symbol")
                bar_by_symbol = {str(row["symbol"]): row.to_dict() for _, row in current.iterrows()}
                for symbol, row in bar_by_symbol.items():
                    last_marks[symbol] = float(row["close"])

                # A position that could not trade at yesterday's 15:55 exits only at a real next available bar.
                overnight_liquidations: set[str] = set()
                for symbol, position in list(portfolio.positions.items()):
                    if position.signal.trade_date < daily.trade_date and symbol in bar_by_symbol:
                        position.forced_overnight = True
                        overnight_liquidations.add(symbol)
                        portfolio.force_exit(symbol, bar_by_symbol[symbol], "HALT_NEXT_AVAILABLE")

                due = pending.pop(timestamp.to_pydatetime(), [])
                executable_signals: list[Signal] = []
                for signal in due:
                    if timestamp.time() > latest_entry:
                        portfolio.reject(signal, "after_latest_entry", time=timestamp.to_pydatetime())
                    elif signal.symbol not in bar_by_symbol:
                        portfolio.reject(signal, "missing_next_bar", time=timestamp.to_pydatetime())
                    elif float(bar_by_symbol[signal.symbol]["dollar_value"]) < self.config.execution.min_five_minute_dollar_volume:
                        portfolio.reject(signal, "entry_bar_liquidity_below_minimum", time=timestamp.to_pydatetime())
                    else:
                        executable_signals.append(signal)
                orders = portfolio.allocate(executable_signals, last_marks)
                for order in orders:
                    portfolio.execute_entry(order, bar_by_symbol[order.signal.symbol])

                if timestamp.time() == force_exit:
                    for symbol in list(portfolio.positions):
                        if symbol in bar_by_symbol:
                            portfolio.force_exit(symbol, bar_by_symbol[symbol], "EOD")
                else:
                    for symbol in list(portfolio.positions):
                        if symbol in bar_by_symbol and symbol not in overnight_liquidations:
                            portfolio.maybe_take_profit(symbol, bar_by_symbol[symbol])

                ranked = dynamic_top_gainers(
                    current,
                    daily.previous_closes,
                    daily.eligibility,
                    self.config.strategy,
                    self.config.execution,
                )
                ranks = ranked.set_index("symbol")["rank"].astype(int).to_dict() if not ranked.empty else {}
                top_symbols = set(ranks)
                for symbol in list(current_top_entry):
                    if symbol not in top_symbols:
                        current_top_entry.pop(symbol, None)
                for symbol in top_symbols:
                    current_top_entry.setdefault(symbol, _dt(bar_by_symbol[symbol]["bar_end"]))
                for row in ranked.to_dict("records"):
                    bar_end = _dt(row["bar_end"])
                    rank_lookup[(pd.Timestamp(row["bar_end"]), str(row["symbol"]))] = int(row["rank"])
                    ranking_records.append(
                        {
                            "date": daily.trade_date.isoformat(),
                            "timestamp": bar_end,
                            "bar_timestamp": _dt(row["timestamp"]),
                            "rank": int(row["rank"]),
                            "symbol": row["symbol"],
                            "gain_pct": float(row["gain_pct"]),
                            "close": float(row["close"]),
                            "previous_close": float(row["previous_close"]),
                            "dollar_value": float(row["dollar_value"]),
                        }
                    )

                # Ranking and signals are evaluated only after this bar is complete.
                for symbol, row in bar_by_symbol.items():
                    machine = states.setdefault(symbol, SignalStateMachine(symbol, daily.trade_date, self.config.strategy))
                    signal, _ = machine.update(
                        row,
                        current_rank=ranks.get(symbol),
                        entered_top_time=current_top_entry.get(symbol),
                    )
                    if signal is None:
                        continue
                    signal_records.append(asdict(signal))
                    emitted_symbols.add(symbol)
                    if signal.intended_entry_time.time() > latest_entry:
                        portfolio.reject(signal, "after_latest_entry", time=signal.intended_entry_time)
                    else:
                        pending[signal.intended_entry_time].append(signal)

                equity = portfolio.mark_equity(last_marks)
                equity_records.append(
                    {
                        "timestamp": timestamp.to_pydatetime(),
                        "date": daily.trade_date.isoformat(),
                        "equity": equity,
                        "cash": portfolio.cash,
                        "positions": len(portfolio.positions),
                    }
                )
                day_equity.append(equity)

            # No bar after the last timestamp means a pending order cannot be filled.
            for pending_time in [key for key in pending if key.date() == daily.trade_date]:
                for signal in pending.pop(pending_time):
                    portfolio.reject(signal, "missing_next_bar", time=pending_time)

            if self.config.output.save_audit_bars and emitted_symbols:
                selected = bars.loc[bars["symbol"].isin(emitted_symbols)].copy()
                selected["date"] = daily.trade_date.isoformat()
                selected["rank"] = [rank_lookup.get((pd.Timestamp(end), str(symbol))) for end, symbol in zip(selected["bar_end"], selected["symbol"])]
                audit_bar_frames.append(selected)
                state_records.extend(_replay_state_audit(selected, rank_lookup, daily.trade_date, self.config))

            ending_equity = portfolio.mark_equity(last_marks)
            daily_return = ending_equity / previous_day_equity - 1.0 if previous_day_equity else 0.0
            peak = np.maximum.accumulate(np.asarray(day_equity, dtype=float))
            within_day_drawdown = np.asarray(day_equity, dtype=float) / peak - 1.0
            day_rejections = portfolio.rejections[day_reject_start:]
            funding_reasons = {"portfolio_or_daily_limit", "insufficient_cash", "already_in_position", "symbol_daily_limit"}
            liquidity_reasons = {"entry_bar_liquidity_below_minimum", "liquidity_zero_fill", "missing_next_bar"}
            daily_records.append(
                {
                    "date": daily.trade_date.isoformat(),
                    "ending_equity": ending_equity,
                    "pnl": ending_equity - previous_day_equity,
                    "return_pct": daily_return,
                    "intraday_max_drawdown": float(within_day_drawdown.min()),
                }
            )
            daily_analysis.append(
                {
                    "date": daily.trade_date.isoformat(),
                    "top_n_snapshots": int(bars["timestamp"].nunique()),
                    "unique_top_symbols": len({row["symbol"] for row in ranking_records if row["date"] == daily.trade_date.isoformat()}),
                    "signals": len(signal_records) - day_signal_start,
                    "filled_entries": len(portfolio.all_positions) - day_trade_start,
                    "rejected_signals": len(portfolio.rejections) - day_reject_start,
                    "funding_or_capacity_rejections": sum(row["reason"] in funding_reasons for row in day_rejections),
                    "liquidity_or_missing_bar_rejections": sum(row["reason"] in liquidity_reasons for row in day_rejections),
                    "ending_positions": len(portfolio.positions),
                    "daily_pnl": ending_equity - previous_day_equity,
                    "daily_return": daily_return,
                    "intraday_max_drawdown": float(within_day_drawdown.min()),
                }
            )
            previous_day_equity = ending_equity

        if portfolio.positions:
            for position in portfolio.positions.values():
                position.warnings.append("no_future_bar_available_for_exit")
        trades = pd.DataFrame(portfolio.trade_records())
        equity_curve, daily_returns = add_equity_diagnostics(pd.DataFrame(equity_records), pd.DataFrame(daily_records))
        metrics = calculate_metrics(trades, equity_curve, daily_returns, self.config.portfolio.initial_capital)
        risk = risk_diagnostics(trades, daily_returns, self.config.random_seed)
        data_quality = dict(self.loader.data_quality)
        forced_overnight = int(trades.get("forced_overnight", pd.Series(dtype=bool)).fillna(False).sum()) if not trades.empty else 0
        unresolved = len(portfolio.positions)
        validation = {
            "future_data_used": False,
            "ranking_uses_completed_current_bar_only": True,
            "signals_use_completed_bar_only": True,
            "entry_is_next_bar_open": _entries_follow_signal(trades),
            "forced_overnight_due_to_halt_or_missing_bar": forced_overnight,
            "open_unresolved_positions": unresolved,
            "data_reliability_gate_passed": bool(data_quality.get("reliable_for_strategy_claim", False)),
            "credible_for_strategy_conclusion": bool(
                data_quality.get("reliable_for_strategy_claim", False) and forced_overnight == 0 and unresolved == 0
            ),
        }
        if progress:
            progress("complete", total, max(total, 1), "回测完成")
        return BacktestResult(
            config=self.config,
            run_id=run_id,
            metrics=metrics,
            risk=risk,
            data_quality=data_quality,
            validation=validation,
            trades=trades,
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            rankings=pd.DataFrame(ranking_records),
            signals=pd.DataFrame(signal_records),
            rejections=pd.DataFrame(portfolio.rejections),
            audit_bars=pd.concat(audit_bar_frames, ignore_index=True) if audit_bar_frames else pd.DataFrame(),
            state_audit=pd.DataFrame(state_records),
            daily_analysis=pd.DataFrame(daily_analysis),
        )


def _replay_state_audit(
    selected: pd.DataFrame,
    rank_lookup: dict[tuple[pd.Timestamp, str], int],
    trade_date: Any,
    config: BacktestConfig,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for symbol, symbol_bars in selected.groupby("symbol", sort=True, observed=True):
        machine = SignalStateMachine(str(symbol), trade_date, config.strategy)
        entered_top: datetime | None = None
        for row in symbol_bars.sort_values("timestamp").to_dict("records"):
            rank = rank_lookup.get((pd.Timestamp(row["bar_end"]), str(symbol)))
            if rank is None:
                entered_top = None
            elif entered_top is None:
                entered_top = _dt(row["bar_end"])
            _, transition = machine.update(row, current_rank=rank, entered_top_time=entered_top)
            records.append(transition)
    return records


def _entries_follow_signal(trades: pd.DataFrame) -> bool:
    if trades.empty:
        return True
    entry = pd.to_datetime(trades["entry_time"], utc=True, errors="coerce")
    signal = pd.to_datetime(trades["signal_time"], utc=True, errors="coerce")
    return bool(((entry - signal) == pd.Timedelta(0)).all())


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return value.to_pydatetime()
