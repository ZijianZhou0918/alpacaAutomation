from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import ModuleType

from . import strategy_gap_confirmed_pullback, strategy_ma5_dip
from .config import Settings
from .models import MarketSnapshot, Position, Signal


DEFAULT_STRATEGY_NAME = strategy_ma5_dip.STRATEGY_NAME
GAP_CONFIRMED_PULLBACK_STRATEGY_NAME = strategy_gap_confirmed_pullback.STRATEGY_NAME

STRATEGY_MODULES: dict[str, ModuleType] = {
    strategy_ma5_dip.STRATEGY_NAME: strategy_ma5_dip,
    strategy_gap_confirmed_pullback.STRATEGY_NAME: strategy_gap_confirmed_pullback,
}

MIN_SIGNAL_DAY_GAIN_PCT = strategy_ma5_dip.MIN_SIGNAL_DAY_GAIN_PCT
MID_SIGNAL_DAY_GAIN_PCT = strategy_ma5_dip.MID_SIGNAL_DAY_GAIN_PCT
HIGH_SIGNAL_DAY_GAIN_PCT = strategy_ma5_dip.HIGH_SIGNAL_DAY_GAIN_PCT
MID_OPEN_GAIN_PCT = strategy_ma5_dip.MID_OPEN_GAIN_PCT
HIGH_OPEN_GAIN_PCT = strategy_ma5_dip.HIGH_OPEN_GAIN_PCT
BUY_TRIGGER_DISTANCE_PCT = strategy_ma5_dip.BUY_TRIGGER_DISTANCE_PCT
MIN_TODAY_OPEN_GAIN_PCT = strategy_ma5_dip.MIN_TODAY_OPEN_GAIN_PCT
MIN_TODAY_OPEN_VS_OPEN_MA5_PCT = strategy_ma5_dip.MIN_TODAY_OPEN_VS_OPEN_MA5_PCT
MAX_BUY_TODAY_CURRENT_GAIN_PCT = strategy_ma5_dip.MAX_BUY_TODAY_CURRENT_GAIN_PCT

STOP_LOSS_COMPARE_EPS = 1e-9
TAKE_PROFIT_COMPARE_EPS = 1e-9

_ACTIVE_STRATEGY_NAME = DEFAULT_STRATEGY_NAME


def available_strategy_names() -> tuple[str, ...]:
    return tuple(STRATEGY_MODULES)


def normalize_strategy_name(strategy_name: str | None) -> str:
    name = (strategy_name or DEFAULT_STRATEGY_NAME).strip()
    if name not in STRATEGY_MODULES:
        choices = ", ".join(available_strategy_names())
        raise ValueError(f"Unknown strategy_name={name!r}; choose one of: {choices}")
    return name


def active_buy_module() -> ModuleType:
    return strategy_module(_ACTIVE_STRATEGY_NAME)


def set_active_strategy(strategy_name: str | None) -> None:
    global _ACTIVE_STRATEGY_NAME
    _ACTIVE_STRATEGY_NAME = normalize_strategy_name(strategy_name)


def strategy_module(strategy_name: str | None = None) -> ModuleType:
    return STRATEGY_MODULES[normalize_strategy_name(strategy_name or _ACTIVE_STRATEGY_NAME)]


@contextmanager
def use_strategy(strategy_name: str | None):
    global _ACTIVE_STRATEGY_NAME
    previous = _ACTIVE_STRATEGY_NAME
    _ACTIVE_STRATEGY_NAME = normalize_strategy_name(strategy_name)
    try:
        yield
    finally:
        _ACTIVE_STRATEGY_NAME = previous


def max_buy_today_current_gain_pct(strategy_name: str | None = None) -> float:
    return float(getattr(strategy_module(strategy_name), "MAX_BUY_TODAY_CURRENT_GAIN_PCT"))


def evaluate_buy(snapshot: MarketSnapshot) -> Signal:
    return active_buy_module().evaluate_buy(snapshot)


def signal_day_buy_point_pct(signal_day_gain_pct: float) -> float | None:
    return active_buy_module().signal_day_buy_point_pct(signal_day_gain_pct)


def open_gain_bonus_pct(today_open_gain_pct: float) -> float:
    return active_buy_module().open_gain_bonus_pct(today_open_gain_pct)


def evaluate_stop_loss(position: Position, snapshot: MarketSnapshot, settings: Settings) -> Signal:
    current_price = snapshot.current_price
    if current_price <= 0:
        return Signal(position.symbol, "HOLD", "当前价格无效", current_price)

    gain_pct = current_price / position.avg_price - 1.0 if position.avg_price > 0 else 0.0
    diagnostics = sell_diagnostics(position, current_price, gain_pct, settings)
    if gain_pct <= settings.stop_loss_pct + STOP_LOSS_COMPARE_EPS:
        return Signal(
            position.symbol,
            "SELL_ALL",
            (
                f"持仓亏损达到 {_format_pct(abs(settings.stop_loss_pct))}，"
                f"按亏损 {_format_pct(abs(settings.stop_loss_limit_pct))} 限价卖出全部"
            ),
            current_price,
            position.quantity,
            with_sell_rule(diagnostics, "stop_loss"),
        )
    return Signal(
        position.symbol,
        "HOLD",
        f"未触发 -{_format_pct(abs(settings.stop_loss_pct))} 持仓止损",
        current_price,
        diagnostics=with_sell_rule(diagnostics, "hold"),
    )


def evaluate_sell(position: Position, snapshot: MarketSnapshot, now_et: datetime, settings: Settings) -> Signal:
    current_price = snapshot.current_price
    if current_price <= 0:
        return Signal(position.symbol, "HOLD", "当前价格无效", current_price)

    gain_pct = current_price / position.avg_price - 1.0 if position.avg_price > 0 else 0.0
    diagnostics = sell_diagnostics(position, current_price, gain_pct, settings)

    if settings.close_liquidation_start <= now_et.time() <= settings.close_liquidation_end:
        return Signal(position.symbol, "SELL_ALL", "临近常规盘收盘，卖出全部", current_price, position.quantity, with_sell_rule(diagnostics, "close_liquidation"))
    if gain_pct <= settings.stop_loss_pct + STOP_LOSS_COMPARE_EPS:
        return Signal(
            position.symbol,
            "SELL_ALL",
            (
                f"持仓亏损达到 {_format_pct(abs(settings.stop_loss_pct))}，"
                f"按亏损 {_format_pct(abs(settings.stop_loss_limit_pct))} 限价卖出全部"
            ),
            current_price,
            position.quantity,
            with_sell_rule(diagnostics, "stop_loss"),
        )
    if gain_pct + TAKE_PROFIT_COMPARE_EPS >= settings.take_profit_half_pct:
        sell_fraction = min(1.0, max(0.0, settings.take_profit_sell_fraction))
        sell_qty = round(position.quantity * sell_fraction, 6)
        action = "SELL_ALL" if sell_fraction >= 1.0 else "SELL_HALF"
        sell_text = "止盈一半" if abs(sell_fraction - 0.5) <= 1e-9 else f"止盈 {sell_fraction:.0%}"
        sell_rule = "take_profit_all" if action == "SELL_ALL" else "take_profit_half"
        return Signal(position.symbol, action, f"持仓收益达到 {_format_pct(settings.take_profit_half_pct)}，{sell_text}", current_price, sell_qty, with_sell_rule(diagnostics, sell_rule))
    return Signal(
        position.symbol,
        "HOLD",
        f"未触发收盘卖出、-{_format_pct(abs(settings.stop_loss_pct))} 止损或 {_format_pct(settings.take_profit_half_pct)} 止盈",
        current_price,
        diagnostics=with_sell_rule(diagnostics, "hold"),
    )


def evaluate_take_profit_remainder_stop(position: Position, snapshot: MarketSnapshot, settings: Settings) -> Signal:
    current_price = snapshot.current_price
    if current_price <= 0:
        return Signal(position.symbol, "HOLD", "当前价格无效", current_price)

    gain_pct = current_price / position.avg_price - 1.0 if position.avg_price > 0 else 0.0
    diagnostics = sell_diagnostics(position, current_price, gain_pct, settings)
    if settings.take_profit_remainder_stop_pct is None:
        return Signal(
            position.symbol,
            "HOLD",
            "当前策略不启用半仓止盈后的剩余仓保护线",
            current_price,
            diagnostics=with_sell_rule(diagnostics, "hold"),
        )

    stop_price = take_profit_remainder_stop_price(position, settings)
    limit_price = take_profit_remainder_stop_limit_price(position, current_price, settings)
    diagnostics = {
        **diagnostics,
        "take_profit_remainder_stop_pct": settings.take_profit_remainder_stop_pct,
        "take_profit_remainder_stop_price": stop_price,
        "stop_loss_limit_price": limit_price,
    }
    if gain_pct <= settings.take_profit_remainder_stop_pct + TAKE_PROFIT_COMPARE_EPS:
        return Signal(
            position.symbol,
            "SELL_ALL",
            (
                f"半仓止盈后剩余持仓回落到 {_format_pct(settings.take_profit_remainder_stop_pct)}，"
                f"按保护限价 {limit_price:.4f} 卖出全部"
            ),
            current_price,
            position.quantity,
            with_sell_rule(diagnostics, "take_profit_remainder_stop"),
        )
    return Signal(
        position.symbol,
        "HOLD",
        f"半仓止盈后剩余持仓仍高于 {_format_pct(settings.take_profit_remainder_stop_pct)} 保护线",
        current_price,
        diagnostics=with_sell_rule(diagnostics, "hold"),
    )


def sell_diagnostics(position: Position, current_price: float, gain_pct: float, settings: Settings) -> dict[str, float | str | None]:
    return {
        "current_price": current_price,
        "avg_price": position.avg_price,
        "gain_pct": gain_pct,
        "stop_loss_pct": settings.stop_loss_pct,
        "stop_loss_limit_pct": settings.stop_loss_limit_pct,
        "stop_loss_limit_price": stop_loss_limit_price(position, settings),
        "take_profit_half_pct": settings.take_profit_half_pct,
        "take_profit_sell_fraction": settings.take_profit_sell_fraction,
    }


def stop_loss_limit_price(position: Position, settings: Settings) -> float:
    if position.avg_price <= 0:
        return 0.0
    return round(position.avg_price * (1.0 + settings.stop_loss_limit_pct), 4)


def take_profit_remainder_stop_price(position: Position, settings: Settings) -> float:
    if position.avg_price <= 0 or settings.take_profit_remainder_stop_pct is None:
        return 0.0
    return round(position.avg_price * (1.0 + settings.take_profit_remainder_stop_pct), 4)


def take_profit_remainder_stop_limit_price(position: Position, current_price: float, settings: Settings) -> float:
    stop_price = take_profit_remainder_stop_price(position, settings)
    if stop_price <= 0 or current_price <= 0:
        return 0.0
    return round(min(stop_price, current_price), 4)


def with_sell_rule(diagnostics: dict[str, float | str | None], rule: str) -> dict[str, float | str | None]:
    return {**diagnostics, "sell_rule": rule}


def _format_pct(value: float) -> str:
    return f"{value:.2%}"
