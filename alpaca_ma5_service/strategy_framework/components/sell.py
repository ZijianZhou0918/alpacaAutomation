from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ...models import MarketSnapshot, Position, Signal
from ..names import DEFAULT_SELL_STRATEGY_NAME

if TYPE_CHECKING:
    from ...config import Settings
    from ..registry import StrategyRegistry


@dataclass(frozen=True)
class StandardIntradaySellStrategy:
    """标准盘中退出策略适配器：只返回卖出信号，不直接提交订单。"""

    name: str = DEFAULT_SELL_STRATEGY_NAME
    description: str = "尾盘清仓、止损、分批止盈及剩余仓保护"

    def evaluate(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        now_et: datetime,
        settings: Settings,
    ) -> Signal:
        """检查 WatchCode 内持仓的尾盘、止损和止盈规则。"""
        from ... import strategy

        # 【卖出决策，不下单】返回结果由 service.run_once 决定是否交给 Broker。
        return strategy.evaluate_sell(position, snapshot, now_et, settings)

    def evaluate_stop_loss(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        settings: Settings,
    ) -> Signal:
        """只检查止损，确保 WatchCode 外的券商持仓仍受底线风控。"""
        from ... import strategy

        # 【卖出决策，不下单】这里不会调用任何券商接口。
        return strategy.evaluate_stop_loss(position, snapshot, settings)

    def evaluate_take_profit_remainder_stop(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        settings: Settings,
    ) -> Signal:
        """半仓止盈完成后，检查剩余仓是否触发配置的保护线。"""
        from ... import strategy

        # 【卖出决策，不下单】SELL_ALL 信号仍需服务层风控后才能提交。
        return strategy.evaluate_take_profit_remainder_stop(position, snapshot, settings)


def register_builtin_sell_strategies(registry: StrategyRegistry) -> None:
    """注册标准盘中卖出信号组件。"""
    registry.register_sell(StandardIntradaySellStrategy())
