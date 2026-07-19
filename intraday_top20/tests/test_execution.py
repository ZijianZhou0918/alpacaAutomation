from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.engine import IntradayTopGainersBacktester
from intraday_top20.backtest.models import PendingOrder, Signal
from intraday_top20.backtest.portfolio import Portfolio

from conftest import InMemoryLoader, signal_day

EASTERN = ZoneInfo("America/New_York")


def test_signal_fills_at_next_five_minute_open() -> None:
    day = signal_day(date(2025, 1, 3))
    result = IntradayTopGainersBacktester(BacktestConfig(), InMemoryLoader([day])).run()
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["signal_time"] == "2025-01-03T10:05:00-05:00"
    assert trade["entry_time"] == "2025-01-03T10:05:00-05:00"
    assert result.validation["entry_is_next_bar_open"]


def test_take_profit_sells_half_then_eod_sells_remainder() -> None:
    portfolio = Portfolio(BacktestConfig())
    signal = _signal()
    entry_bar = _bar(time(10, 5), open_price=10.0, high=10.2, volume=1_000_000)
    position = portfolio.execute_entry(PendingOrder(signal, 1_000.0), entry_bar)
    assert position is not None
    initial = position.initial_quantity
    target = position.entry_price * 1.20
    tp_bar = _bar(time(11, 0), open_price=target * 0.99, high=target * 1.02, volume=1_000_000)
    assert portfolio.maybe_take_profit("AAA", tp_bar)
    assert position.take_profit_quantity == pytest.approx(initial * 0.5)
    assert position.remaining_quantity == pytest.approx(initial * 0.5)
    assert portfolio.force_exit("AAA", _bar(time(15, 55), open_price=11.0, high=11.1, volume=1_000_000), "EOD")
    assert position.is_closed
    assert position.tail_exit_quantity == pytest.approx(initial * 0.5)
    assert position.close_reason == "TAKE_PROFIT+EOD"


def test_volume_participation_and_costs_are_applied() -> None:
    config = BacktestConfig().with_updates(execution={"max_volume_participation": 0.01})
    portfolio = Portfolio(config)
    position = portfolio.execute_entry(PendingOrder(_signal(), 50_000.0), _bar(time(10, 5), open_price=10.0, high=10.2, volume=1_000))
    assert position is not None
    assert position.initial_quantity <= 10.0
    assert position.fill_ratio < 1.0
    portfolio.force_exit("AAA", _bar(time(15, 55), open_price=10.0, high=10.1, volume=1_000), "EOD")
    assert position.entry_commission > 0
    assert position.exit_commission > 0
    assert position.entry_slippage_cost > 0
    assert position.exit_slippage_cost > 0
    assert position.net_pnl < 0


def test_normal_session_has_no_overnight_position() -> None:
    result = IntradayTopGainersBacktester(BacktestConfig(), InMemoryLoader([signal_day(date(2025, 1, 3))])).run()
    assert result.validation["open_unresolved_positions"] == 0
    assert not result.trades["forced_overnight"].any()
    assert result.trades.iloc[0]["tail_exit_time"].startswith("2025-01-03T15:55")


def _signal() -> Signal:
    day = date(2025, 1, 3)
    entered = datetime.combine(day, time(9, 35), EASTERN)
    return Signal(day, "AAA", 1, entered, datetime.combine(day, time(9, 40), EASTERN), 5, 25, datetime.combine(day, time(10, 5), EASTERN), datetime.combine(day, time(10, 5), EASTERN), "VWAP", 10.0, 11.0)


def _bar(at: time, *, open_price: float, high: float, volume: float) -> dict[str, object]:
    timestamp = datetime.combine(date(2025, 1, 3), at, EASTERN)
    return {"symbol": "AAA", "timestamp": timestamp, "open": open_price, "high": high, "low": open_price * 0.98, "close": open_price, "volume": volume}
