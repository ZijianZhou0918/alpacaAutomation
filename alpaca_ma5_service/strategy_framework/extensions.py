from __future__ import annotations

from .registry import StrategyRegistry


def register_custom_strategies(registry: StrategyRegistry) -> None:
    """可信本地策略扩展的唯一显式注册入口。

    在这里依次注册新增组件，再注册组合这些组件的 profile。重复名称、缺少关键
    方法或引用未注册组件都会在启动阶段报错，早于行情读取和 Broker 连接。
    当前没有自定义扩展，因此保持空实现。
    """
    return None
