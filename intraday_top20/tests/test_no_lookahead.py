from __future__ import annotations

from datetime import date

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.engine import IntradayTopGainersBacktester

from conftest import InMemoryLoader, quiet_day, signal_day


def test_missing_next_bar_does_not_create_false_fill() -> None:
    day = signal_day(date(2025, 1, 3), include_entry_bar=False, include_eod_bar=True)
    result = IntradayTopGainersBacktester(BacktestConfig(), InMemoryLoader([day])).run()
    assert result.trades.empty
    assert "missing_next_bar" in set(result.rejections["reason"])


def test_missing_eod_bar_waits_for_real_next_available_bar_and_flags_overnight() -> None:
    first = signal_day(date(2025, 1, 3), include_entry_bar=True, include_eod_bar=False)
    second = quiet_day(date(2025, 1, 6))
    result = IntradayTopGainersBacktester(BacktestConfig(), InMemoryLoader([first, second])).run()
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert bool(trade["forced_overnight"])
    assert trade["tail_exit_time"].startswith("2025-01-06T09:30")
    assert "HALT_NEXT_AVAILABLE" in trade["close_reason"]
    assert result.validation["forced_overnight_due_to_halt_or_missing_bar"] == 1


def test_engine_declares_completed_bar_ranking_and_no_future_use() -> None:
    result = IntradayTopGainersBacktester(BacktestConfig(), InMemoryLoader([signal_day(date(2025, 1, 3))])).run()
    assert result.validation["future_data_used"] is False
    assert result.validation["ranking_uses_completed_current_bar_only"] is True
    assert result.validation["signals_use_completed_bar_only"] is True
