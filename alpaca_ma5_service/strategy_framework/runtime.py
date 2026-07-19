from __future__ import annotations

from typing import TYPE_CHECKING

from .contracts import StrategyRuntime, StrategySelection
from .registry import get_strategy_registry

if TYPE_CHECKING:
    from ..config import Settings


def resolve_strategy_selection(
    profile_name: str,
    *,
    watchlist_strategy_name: str | None = None,
    buy_strategy_name: str | None = None,
    sell_strategy_name: str | None = None,
    cancel_strategy_name: str | None = None,
) -> StrategySelection:
    """合并 profile 默认值和四类单项覆盖，得到最终策略名称。

    本函数只处理配置，不读行情也不下单；返回前会验证四个名称都已注册，
    因而错误配置会在任何交易侧 I/O 发生之前立即失败。
    """
    registry = get_strategy_registry()
    profile = registry.profile(profile_name)
    selection = StrategySelection(
        profile_name=profile.name,
        watchlist_strategy_name=_override_or_default(
            watchlist_strategy_name, profile.watchlist_strategy_name
        ),
        buy_strategy_name=_override_or_default(buy_strategy_name, profile.buy_strategy_name),
        sell_strategy_name=_override_or_default(sell_strategy_name, profile.sell_strategy_name),
        cancel_strategy_name=_override_or_default(cancel_strategy_name, profile.cancel_strategy_name),
    )
    # Resolve every component now so invalid configuration fails before any I/O.
    registry.watchlist(selection.watchlist_strategy_name)
    registry.buy(selection.buy_strategy_name)
    registry.sell(selection.sell_strategy_name)
    registry.cancel(selection.cancel_strategy_name)
    return selection


def resolve_strategy_runtime(settings: Settings) -> StrategyRuntime:
    """按照 Settings 装配本轮实际使用的 WatchCode、买入、卖出和撤单对象。

    ``service.run_once`` 和 ``AlpacaStockBroker`` 都从这里取得一致的组件；
    这里不执行策略判断，也不产生券商外部写入。
    """
    selection = _selection_from_settings(settings)
    registry = get_strategy_registry()
    return StrategyRuntime(
        selection=selection,
        watchlist=registry.watchlist(selection.watchlist_strategy_name),
        buy=registry.buy(selection.buy_strategy_name),
        sell=registry.sell(selection.sell_strategy_name),
        cancel=registry.cancel(selection.cancel_strategy_name),
    )


def _selection_from_settings(settings: Settings) -> StrategySelection:
    """把项目配置字段转换成统一的 ``StrategySelection``。"""
    profile_name = settings.strategy_profile_name or settings.strategy_name
    return resolve_strategy_selection(
        profile_name,
        watchlist_strategy_name=settings.watchlist_strategy_name or None,
        buy_strategy_name=settings.buy_strategy_name or None,
        sell_strategy_name=settings.sell_strategy_name or None,
        cancel_strategy_name=settings.cancel_strategy_name or None,
    )


def _override_or_default(value: str | None, default: str) -> str:
    """有显式单项配置时优先使用，否则沿用 profile 默认策略名。"""
    return (value or default).strip()
