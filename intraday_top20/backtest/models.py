from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class Signal:
    trade_date: date
    symbol: str
    rank: int
    entered_top_time: datetime
    below_start_time: datetime
    below_bars: int
    below_minutes: int
    signal_time: datetime
    intended_entry_time: datetime
    indicator_name: str
    indicator_value: float
    signal_close: float


@dataclass(frozen=True)
class PendingOrder:
    signal: Signal
    target_notional: float


@dataclass
class Position:
    trade_id: str
    signal: Signal
    entry_time: datetime
    entry_reference_price: float
    entry_price: float
    initial_quantity: float
    remaining_quantity: float
    entry_commission: float
    entry_slippage_cost: float
    target_notional: float
    fill_ratio: float
    take_profit_time: datetime | None = None
    take_profit_reference_price: float | None = None
    take_profit_price: float | None = None
    take_profit_quantity: float = 0.0
    take_profit_proceeds: float = 0.0
    tail_exit_time: datetime | None = None
    tail_exit_reference_price: float | None = None
    tail_exit_price: float | None = None
    tail_exit_quantity: float = 0.0
    tail_exit_proceeds: float = 0.0
    exit_commission: float = 0.0
    exit_slippage_cost: float = 0.0
    exit_proceeds: float = 0.0
    last_fill_price: float = 0.0
    close_reason: str = ""
    forced_overnight: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.remaining_quantity <= 1e-9

    @property
    def entry_cost(self) -> float:
        return self.entry_price * self.initial_quantity + self.entry_commission

    @property
    def net_pnl(self) -> float:
        return self.exit_proceeds - self.entry_price * self.initial_quantity - self.entry_commission - self.exit_commission

    @property
    def return_pct(self) -> float:
        return self.net_pnl / self.entry_cost if self.entry_cost else 0.0

    def to_record(self) -> dict[str, Any]:
        exit_time = self.tail_exit_time or self.take_profit_time
        hold_minutes = (exit_time - self.entry_time).total_seconds() / 60 if exit_time else None
        return {
            "trade_id": self.trade_id,
            "date": self.signal.trade_date.isoformat(),
            "symbol": self.signal.symbol,
            "rank_at_signal": self.signal.rank,
            "entered_top_time": self.signal.entered_top_time.isoformat(),
            "below_start_time": self.signal.below_start_time.isoformat(),
            "below_bars": self.signal.below_bars,
            "below_minutes": self.signal.below_minutes,
            "signal_time": self.signal.signal_time.isoformat(),
            "signal_close": self.signal.signal_close,
            "indicator_name": self.signal.indicator_name,
            "indicator_value": self.signal.indicator_value,
            "entry_time": self.entry_time.isoformat(),
            "entry_reference_price": self.entry_reference_price,
            "entry_price": self.entry_price,
            "entry_quantity": self.initial_quantity,
            "target_notional": self.target_notional,
            "fill_ratio": self.fill_ratio,
            "take_profit_time": self.take_profit_time.isoformat() if self.take_profit_time else "",
            "take_profit_reference_price": self.take_profit_reference_price,
            "take_profit_price": self.take_profit_price,
            "take_profit_quantity": self.take_profit_quantity,
            "tail_exit_time": self.tail_exit_time.isoformat() if self.tail_exit_time else "",
            "tail_exit_reference_price": self.tail_exit_reference_price,
            "tail_exit_price": self.tail_exit_price,
            "tail_exit_quantity": self.tail_exit_quantity,
            "commission": self.entry_commission + self.exit_commission,
            "slippage_cost": self.entry_slippage_cost + self.exit_slippage_cost,
            "return_pct": self.return_pct if self.is_closed else None,
            "net_pnl": self.net_pnl if self.is_closed else None,
            "holding_minutes": hold_minutes,
            "close_reason": self.close_reason or "OPEN_UNRESOLVED",
            "hit_take_profit": self.take_profit_quantity > 0,
            "forced_overnight": self.forced_overnight,
            "is_closed": self.is_closed,
            "warnings": " | ".join(self.warnings),
        }
