from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from intraday_top20.backtest.config import StrategyConfig
from intraday_top20.backtest.strategy import SignalStateMachine

EASTERN = ZoneInfo("America/New_York")


def test_requires_five_complete_below_bars_for_strictly_over_twenty_minutes() -> None:
    machine = SignalStateMachine("AAA", date(2025, 1, 3), StrategyConfig(continuous_below_minutes=20))
    assert _update(machine, 0, 11.0) is None
    for index in range(1, 6):
        assert _update(machine, index, 9.0) is None
    signal = _update(machine, 6, 11.0)
    assert signal is not None
    assert signal.below_bars == 5
    assert signal.below_minutes == 25


def test_four_below_bars_are_not_enough() -> None:
    machine = SignalStateMachine("AAA", date(2025, 1, 3), StrategyConfig(continuous_below_minutes=20))
    _update(machine, 0, 11.0)
    for index in range(1, 5):
        _update(machine, index, 9.0)
    assert _update(machine, 5, 11.0) is None


def test_reclaim_resets_below_timer_before_new_sequence() -> None:
    machine = SignalStateMachine("AAA", date(2025, 1, 3), StrategyConfig(continuous_below_minutes=20))
    _update(machine, 0, 11.0)
    for index in range(1, 4):
        _update(machine, index, 9.0)
    assert _update(machine, 4, 11.0) is None
    for index in range(5, 10):
        _update(machine, index, 9.0)
    signal = _update(machine, 10, 11.0)
    assert signal is not None
    assert signal.below_start_time == datetime.combine(date(2025, 1, 3), time(9, 30), EASTERN) + timedelta(minutes=30)


def test_missing_bar_resets_continuous_below_sequence() -> None:
    machine = SignalStateMachine("AAA", date(2025, 1, 3), StrategyConfig(continuous_below_minutes=20))
    _update(machine, 0, 11.0)
    for index in range(1, 4):
        _update(machine, index, 9.0)
    # Index 4 is missing. The post-gap bars cannot complete the old streak.
    _update(machine, 5, 9.0)
    _update(machine, 6, 9.0)
    assert _update(machine, 7, 11.0) is None


def _update(machine: SignalStateMachine, index: int, close: float):
    timestamp = datetime.combine(machine.trade_date, time(9, 30), EASTERN) + timedelta(minutes=5 * index)
    signal, _ = machine.update(
        {"timestamp": timestamp, "bar_end": timestamp + timedelta(minutes=5), "close": close, "indicator": 10.0, "indicator_name": "VWAP", "volume": 1_000},
        current_rank=1,
        entered_top_time=datetime.combine(machine.trade_date, time(9, 35), EASTERN),
    )
    return signal
