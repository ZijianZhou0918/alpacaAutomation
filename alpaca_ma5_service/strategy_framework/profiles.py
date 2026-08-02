from __future__ import annotations

from .. import strategy_ma5_dip
from ..config import MA5_DIP_LADDER_STRATEGY_NAME
from .contracts import StrategyProfile
from .names import DEFAULT_CANCEL_STRATEGY_NAME, DEFAULT_SELL_STRATEGY_NAME
from .registry import StrategyRegistry


def register_builtin_profiles(registry: StrategyRegistry) -> None:
    """把已注册的四类组件组合成用户可选择的内置 profile。

    profile 只提供默认策略名和运行参数；用户仍可在配置中单独覆盖任一组件。
    """
    registry.register_profile(
        StrategyProfile(
            name=strategy_ma5_dip.STRATEGY_NAME,
            watchlist_strategy_name=strategy_ma5_dip.STRATEGY_NAME,
            buy_strategy_name=strategy_ma5_dip.STRATEGY_NAME,
            sell_strategy_name=DEFAULT_SELL_STRATEGY_NAME,
            cancel_strategy_name=DEFAULT_CANCEL_STRATEGY_NAME,
            runtime_defaults={
                "max_daily_buys": 2,
                "stop_loss_pct": -0.10,
                "stop_loss_limit_pct": -0.08,
                "take_profit_half_pct": 0.10,
                "take_profit_sell_fraction": 0.50,
                "take_profit_remainder_stop_pct": None,
            },
            description="现有 MA5 低吸完整组合",
        )
    )
    registry.register_profile(
        StrategyProfile(
            name=MA5_DIP_LADDER_STRATEGY_NAME,
            watchlist_strategy_name=strategy_ma5_dip.STRATEGY_NAME,
            buy_strategy_name=strategy_ma5_dip.STRATEGY_NAME,
            sell_strategy_name=DEFAULT_SELL_STRATEGY_NAME,
            cancel_strategy_name=DEFAULT_CANCEL_STRATEGY_NAME,
            runtime_defaults={
                "max_daily_buys": 3,
                "stop_loss_pct": -0.10,
                "stop_loss_limit_pct": -0.10,
                "take_profit_half_pct": 0.10,
                "take_profit_sell_fraction": 0.50,
                "take_profit_remainder_stop_pct": None,
                "absolute_stop_loss_pct": -0.10,
            },
            description="MA5 three-tier buys, split first half take-profit, and weighted-cost market stop",
        )
    )
