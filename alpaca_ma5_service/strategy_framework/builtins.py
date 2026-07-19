"""按 WatchCode -> 买入 -> 卖出 -> 撤单 -> profile 的顺序注册内置策略。"""

from __future__ import annotations

from .components import (
    BuiltinWatchlistStrategy,
    ModuleBuyStrategy,
    StandardIntradaySellStrategy,
    TimeoutCancelConfirmedStrategy,
    register_builtin_buy_strategies,
    register_builtin_cancel_strategies,
    register_builtin_sell_strategies,
    register_builtin_watchcode_strategies,
)
from .profiles import register_builtin_profiles
from .registry import StrategyRegistry


def register_builtin_strategies(registry: StrategyRegistry) -> None:
    """先注册四类组件，再注册引用它们的完整组合。

    顺序不能反过来，因为 profile 注册时会立即校验所引用的组件是否存在。
    本函数只建立注册表，不执行任何策略或订单。
    """
    register_builtin_watchcode_strategies(registry)
    register_builtin_buy_strategies(registry)
    register_builtin_sell_strategies(registry)
    register_builtin_cancel_strategies(registry)
    register_builtin_profiles(registry)


__all__ = [
    "BuiltinWatchlistStrategy",
    "ModuleBuyStrategy",
    "StandardIntradaySellStrategy",
    "TimeoutCancelConfirmedStrategy",
    "register_builtin_strategies",
]
