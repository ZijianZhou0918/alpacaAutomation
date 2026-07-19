from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .config import StrategyConfig
from .models import Signal


@dataclass
class SignalState:
    has_been_above: bool = False
    below_bars: int = 0
    below_start_time: datetime | None = None
    previous_close: float | None = None
    previous_indicator: float | None = None
    last_bar_end: datetime | None = None
    signals_emitted: int = 0


class SignalStateMachine:
    def __init__(self, symbol: str, trade_date: date, config: StrategyConfig):
        self.symbol = symbol
        self.trade_date = trade_date
        self.config = config
        self.state = SignalState()

    def update(
        self,
        bar: dict[str, Any],
        *,
        current_rank: int | None,
        entered_top_time: datetime | None,
    ) -> tuple[Signal | None, dict[str, Any]]:
        close = float(bar["close"])
        indicator = bar.get("indicator")
        bar_start = _as_datetime(bar["timestamp"])
        bar_end = _as_datetime(bar["bar_end"])
        gap_reset = self.state.last_bar_end is not None and bar_start != self.state.last_bar_end
        transition: dict[str, Any] = {
            "date": self.trade_date.isoformat(),
            "symbol": self.symbol,
            "time": bar_end.isoformat(),
            "close": close,
            "indicator": None if indicator is None else float(indicator),
            "state_before": self._state_name(),
            "rank": current_rank,
            "event": "",
            "gap_reset": gap_reset,
        }
        if gap_reset:
            # A missing five-minute bar may be a halt or data outage.  Never
            # let a below-VWAP streak or cross bridge that unknown interval.
            self.state.below_bars = 0
            self.state.below_start_time = None
            self.state.previous_close = None
            self.state.previous_indicator = None
        if indicator is None or float(indicator) != float(indicator):
            transition["event"] = "indicator_unavailable"
            transition["state_after"] = self._state_name()
            self.state.last_bar_end = bar_end
            return None, transition
        indicator = float(indicator)
        is_above = close > indicator
        signal: Signal | None = None

        if is_above:
            crossed_up = (
                self.state.previous_close is not None
                and self.state.previous_indicator is not None
                and self.state.previous_close <= self.state.previous_indicator
            )
            eligible_cross = (
                crossed_up
                and self.state.has_been_above
                and self.state.below_bars >= self.config.required_below_bars
            )
            if eligible_cross:
                transition["event"] = "reclaim_qualified" if current_rank is not None else "reclaim_outside_top_n"
                if current_rank is not None and entered_top_time is not None and self._volume_filter_passes(bar):
                    repeat_limit = self.config.max_trades_per_symbol_per_day if self.config.allow_repeat_symbol else 1
                    if self.state.signals_emitted < repeat_limit:
                        signal = Signal(
                            trade_date=self.trade_date,
                            symbol=self.symbol,
                            rank=current_rank,
                            entered_top_time=entered_top_time,
                            below_start_time=self.state.below_start_time or bar_end,
                            below_bars=self.state.below_bars,
                            below_minutes=self.state.below_bars * 5,
                            signal_time=bar_end,
                            intended_entry_time=bar_end,
                            indicator_name=str(bar.get("indicator_name") or self.config.indicator.upper()),
                            indicator_value=indicator,
                            signal_close=close,
                        )
                        self.state.signals_emitted += 1
                elif current_rank is not None and not self._volume_filter_passes(bar):
                    transition["event"] = "reclaim_volume_filter_failed"
            elif not self.state.has_been_above:
                transition["event"] = "first_above"
            elif self.state.below_bars:
                transition["event"] = "below_timer_reset"
            self.state.has_been_above = True
            self.state.below_bars = 0
            self.state.below_start_time = None
        else:
            if self.state.has_been_above:
                if self.state.below_bars == 0:
                    self.state.below_start_time = bar_end
                    transition["event"] = "below_started"
                else:
                    transition["event"] = "below_continues"
                self.state.below_bars += 1
            else:
                transition["event"] = "below_before_first_above"

        self.state.previous_close = close
        self.state.previous_indicator = indicator
        self.state.last_bar_end = bar_end
        transition["state_after"] = self._state_name()
        transition["below_bars"] = self.state.below_bars
        transition["signal"] = signal is not None
        return signal, transition

    def _volume_filter_passes(self, bar: dict[str, Any]) -> bool:
        if not self.config.require_volume_expansion:
            return True
        prior_average = bar.get("prior_volume_average")
        if prior_average is None or float(prior_average) != float(prior_average) or float(prior_average) <= 0:
            return False
        return float(bar["volume"]) >= float(prior_average) * self.config.volume_expansion_multiplier

    def _state_name(self) -> str:
        if self.state.below_bars:
            return f"below_{self.state.below_bars}"
        return "above_seen" if self.state.has_been_above else "waiting_first_above"


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return value.to_pydatetime()
