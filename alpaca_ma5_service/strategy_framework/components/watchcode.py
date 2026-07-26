from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ... import strategy_ma5_dip

if TYPE_CHECKING:
    from ..registry import StrategyRegistry


@dataclass(frozen=True)
class BuiltinWatchlistStrategy:
    """把内置筛选规则工厂适配为 WatchCode 契约；只选规则，不生成文件。"""

    name: str
    description: str

    def screen_rules(self):
        """返回所选策略的选股参数，供 WatchCode 生成工作流读取。"""
        # 延迟导入使策略注册不依赖数据库、行情或网络初始化。
        from ... import watchlist_generator

        if self.name == strategy_ma5_dip.STRATEGY_NAME:
            return watchlist_generator.ma5_dip_watchlist_rules()
        raise ValueError(f"No built-in WatchCode rule factory for {self.name!r}")


def register_builtin_watchcode_strategies(registry: StrategyRegistry) -> None:
    """注册内置 MA5 WatchCode 选股规则。"""
    registry.register_watchlist(
        BuiltinWatchlistStrategy(
            strategy_ma5_dip.STRATEGY_NAME,
            "信号日强势且收盘相对动态 MA5 足够高的普通股观察池",
        )
    )
