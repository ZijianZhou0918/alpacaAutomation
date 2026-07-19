from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING

from ... import strategy_gap_confirmed_pullback, strategy_ma5_dip
from ...models import MarketSnapshot, Signal

if TYPE_CHECKING:
    from ..registry import StrategyRegistry


@dataclass(frozen=True)
class ModuleBuyStrategy:
    """把现有策略模块适配为统一买入契约；只做决策，不提交订单。"""

    module: ModuleType
    description: str

    @property
    def name(self) -> str:
        return str(self.module.STRATEGY_NAME)

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        """调用所选策略的 ``evaluate_buy``，返回 BUY/HOLD 给服务层。"""
        # 【买入决策，不下单】实际规则位于 module 对应的 strategy_*.py。
        return self.module.evaluate_buy(snapshot)

    def max_buy_today_current_gain_pct(self) -> float:
        """返回买入策略要求的最大当日涨跌幅，用于服务层排除提示。"""
        return float(getattr(self.module, "MAX_BUY_TODAY_CURRENT_GAIN_PCT"))

    def legacy_module(self) -> ModuleType:
        """暴露旧模块接口，供尚未迁移的兼容调用读取辅助函数。"""
        return self.module


def register_builtin_buy_strategies(registry: StrategyRegistry) -> None:
    """注册项目自带的两套买入信号实现。"""
    registry.register_buy(ModuleBuyStrategy(strategy_ma5_dip, "动态 MA5 回撤买入"))
    registry.register_buy(
        ModuleBuyStrategy(strategy_gap_confirmed_pullback, "缺口确认后的回撤买入")
    )
