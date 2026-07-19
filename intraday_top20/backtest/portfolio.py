from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any

from .config import BacktestConfig
from .execution import CostModel, executable_quantity
from .models import PendingOrder, Position, Signal


class Portfolio:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.cash = config.portfolio.initial_capital
        self.positions: dict[str, Position] = {}
        self.all_positions: list[Position] = []
        self.daily_entries: Counter[date] = Counter()
        self.symbol_daily_entries: Counter[tuple[date, str]] = Counter()
        self.cost_model = CostModel(config.execution)
        self.rejections: list[dict[str, Any]] = []

    def allocate(self, signals: list[Signal], marks: dict[str, float]) -> list[PendingOrder]:
        if not signals:
            return []
        trade_date = signals[0].trade_date
        remaining_daily = self.config.portfolio.max_daily_entries - self.daily_entries[trade_date]
        remaining_slots = self.config.portfolio.max_concurrent_positions - len(self.positions)
        capacity = max(0, min(remaining_daily, remaining_slots))
        accepted: list[Signal] = []
        for signal in sorted(signals, key=lambda item: (item.rank, item.symbol)):
            if len(accepted) >= capacity:
                self.reject(signal, "portfolio_or_daily_limit")
                continue
            if signal.symbol in self.positions:
                self.reject(signal, "already_in_position")
                continue
            repeat_limit = self.config.strategy.max_trades_per_symbol_per_day if self.config.strategy.allow_repeat_symbol else 1
            if self.symbol_daily_entries[(trade_date, signal.symbol)] >= repeat_limit:
                self.reject(signal, "symbol_daily_limit")
                continue
            accepted.append(signal)
        if not accepted:
            return []
        equity = self.mark_equity(marks)
        max_each = equity * self.config.portfolio.max_position_pct
        equal_cash = self.cash / len(accepted)
        target = min(max_each, equal_cash)
        return [PendingOrder(signal, target) for signal in accepted]

    def execute_entry(self, order: PendingOrder, bar: dict[str, Any]) -> Position | None:
        signal = order.signal
        reference = float(bar["open"])
        provisional_bps = self.cost_model.slippage_bps(reference, float(bar["high"]), float(bar["low"]))
        estimated_fill = reference * (1 + provisional_bps / 10_000)
        quantity, fill_ratio = executable_quantity(
            order.target_notional,
            estimated_fill,
            float(bar["volume"]),
            self.config.execution.max_volume_participation,
            fractional_shares=self.config.portfolio.fractional_shares,
        )
        if quantity <= 0:
            self.reject(signal, "liquidity_zero_fill")
            return None
        fill = self.cost_model.buy(reference, quantity, float(bar["high"]), float(bar["low"]))
        affordable = max(0.0, (self.cash - fill.commission) / fill.fill_price)
        if quantity > affordable:
            quantity = int(affordable * 1_000) / 1_000 if self.config.portfolio.fractional_shares else int(affordable)
            if quantity <= 0:
                self.reject(signal, "insufficient_cash")
                return None
            fill = self.cost_model.buy(reference, quantity, float(bar["high"]), float(bar["low"]))
            desired = order.target_notional / fill.fill_price
            fill_ratio = quantity / desired if desired > 0 else 0.0
        self.cash -= fill.fill_price * quantity + fill.commission
        trade_id = f"{signal.trade_date.isoformat()}-{signal.symbol}-{self.symbol_daily_entries[(signal.trade_date, signal.symbol)] + 1}"
        position = Position(
            trade_id=trade_id,
            signal=signal,
            entry_time=_dt(bar["timestamp"]),
            entry_reference_price=reference,
            entry_price=fill.fill_price,
            initial_quantity=quantity,
            remaining_quantity=quantity,
            entry_commission=fill.commission,
            entry_slippage_cost=fill.slippage_cost,
            target_notional=order.target_notional,
            fill_ratio=fill_ratio,
        )
        if fill_ratio < 0.999:
            position.warnings.append("partial_fill_due_to_volume_or_cash")
        self.positions[signal.symbol] = position
        self.all_positions.append(position)
        self.daily_entries[signal.trade_date] += 1
        self.symbol_daily_entries[(signal.trade_date, signal.symbol)] += 1
        return position

    def maybe_take_profit(self, symbol: str, bar: dict[str, Any]) -> bool:
        position = self.positions.get(symbol)
        target_quantity = position.initial_quantity * self.config.execution.take_profit_fraction if position else 0.0
        if not position or position.take_profit_quantity >= target_quantity - 1e-9:
            return False
        target = position.entry_price * (1.0 + self.config.strategy.take_profit_pct)
        if float(bar["high"]) < target:
            return False
        quantity = min(position.remaining_quantity, target_quantity - position.take_profit_quantity)
        sold = self._sell(position, quantity, target, bar, "TAKE_PROFIT")
        if sold <= 0:
            position.warnings.append("take_profit_unfilled_due_to_volume")
            return False
        position.take_profit_time = position.take_profit_time or _dt(bar["timestamp"])
        position.take_profit_reference_price = target
        position.take_profit_quantity += sold
        position.take_profit_proceeds += position.last_fill_price * sold
        position.take_profit_price = position.take_profit_proceeds / position.take_profit_quantity
        return True

    def force_exit(self, symbol: str, bar: dict[str, Any], reason: str = "EOD") -> bool:
        position = self.positions.get(symbol)
        if not position:
            return False
        quantity = position.remaining_quantity
        sold = self._sell(position, quantity, float(bar["open"]), bar, reason)
        if sold <= 0:
            position.warnings.append(f"{reason.lower()}_unfilled_due_to_volume")
            return False
        position.tail_exit_time = _dt(bar["timestamp"])
        position.tail_exit_reference_price = float(bar["open"])
        position.tail_exit_quantity += sold
        position.tail_exit_proceeds += position.last_fill_price * sold
        position.tail_exit_price = position.tail_exit_proceeds / position.tail_exit_quantity
        if position.is_closed:
            position.close_reason = reason if position.take_profit_quantity == 0 else f"TAKE_PROFIT+{reason}"
        else:
            position.warnings.append(f"partial_{reason.lower()}_fill_due_to_volume")
        position.forced_overnight = position.forced_overnight or reason == "HALT_NEXT_AVAILABLE"
        if position.is_closed:
            self.positions.pop(symbol, None)
        return True

    def _sell(self, position: Position, quantity: float, reference: float, bar: dict[str, Any], reason: str) -> float:
        volume_cap = max(0.0, float(bar["volume"]) * self.config.execution.max_volume_participation)
        quantity = min(quantity, volume_cap)
        if self.config.portfolio.fractional_shares:
            quantity = int(quantity * 1_000) / 1_000
        else:
            quantity = int(quantity)
        if quantity <= 0:
            return 0.0
        fill = self.cost_model.sell(reference, quantity, float(bar["high"]), float(bar["low"]))
        proceeds = fill.fill_price * quantity
        self.cash += proceeds - fill.commission
        position.remaining_quantity = max(0.0, position.remaining_quantity - quantity)
        position.exit_proceeds += proceeds
        position.exit_commission += fill.commission
        position.exit_slippage_cost += fill.slippage_cost
        position.last_fill_price = fill.fill_price
        if reason == "TAKE_PROFIT" and position.is_closed:
            position.close_reason = "TAKE_PROFIT"
            self.positions.pop(position.signal.symbol, None)
        return quantity

    def reject(self, signal: Signal, reason: str, *, time: datetime | None = None) -> None:
        self.rejections.append(
            {
                "date": signal.trade_date.isoformat(),
                "time": (time or signal.intended_entry_time).isoformat(),
                "symbol": signal.symbol,
                "rank": signal.rank,
                "reason": reason,
                "signal_time": signal.signal_time.isoformat(),
            }
        )

    def mark_equity(self, marks: dict[str, float]) -> float:
        value = self.cash
        for symbol, position in self.positions.items():
            value += position.remaining_quantity * marks.get(symbol, position.entry_price)
        return value

    def trade_records(self) -> list[dict[str, Any]]:
        return [position.to_record() for position in self.all_positions]


def _dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else value.to_pydatetime()
