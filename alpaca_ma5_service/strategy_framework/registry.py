from __future__ import annotations

from threading import RLock
from typing import TypeVar

from .contracts import BuyStrategy, CancelStrategy, SellStrategy, StrategyProfile, WatchlistStrategy


StrategyT = TypeVar("StrategyT")


class StrategyRegistry:
    """四类策略和 profile 的类型化注册表。

    WatchCode、买入、卖出和撤单使用独立命名空间，因此可以分别替换；
    注册时会校验名称唯一性和关键方法是否齐全。
    """

    def __init__(self) -> None:
        self._watchlist: dict[str, WatchlistStrategy] = {}
        self._buy: dict[str, BuyStrategy] = {}
        self._sell: dict[str, SellStrategy] = {}
        self._cancel: dict[str, CancelStrategy] = {}
        self._profiles: dict[str, StrategyProfile] = {}

    def register_watchlist(self, strategy: WatchlistStrategy) -> None:
        """注册一个 WatchCode 规则提供者。"""
        self._register("watchlist", self._watchlist, strategy, ("screen_rules",))

    def register_buy(self, strategy: BuyStrategy) -> None:
        """注册一个只产生 BUY/HOLD 信号的买入策略。"""
        self._register(
            "buy",
            self._buy,
            strategy,
            ("evaluate", "max_buy_today_current_gain_pct", "legacy_module"),
        )

    def register_sell(self, strategy: SellStrategy) -> None:
        """注册一个只产生卖出/持有信号的卖出策略。"""
        self._register(
            "sell",
            self._sell,
            strategy,
            ("evaluate", "evaluate_stop_loss", "evaluate_take_profit_remainder_stop"),
        )

    def register_cancel(self, strategy: CancelStrategy) -> None:
        """注册一个订单提交后的等待与撤单策略。"""
        self._register(
            "cancel",
            self._cancel,
            strategy,
            ("wait_for_terminal", "cancel_order"),
        )

    def register_profile(self, profile: StrategyProfile) -> None:
        """注册策略组合，并验证四类引用和必需运行参数完整。"""
        name = self._normalized_name(profile.name, "profile")
        if name in self._profiles:
            raise ValueError(f"Duplicate strategy profile: {name!r}")
        self._resolve("watchlist", self._watchlist, profile.watchlist_strategy_name)
        self._resolve("buy", self._buy, profile.buy_strategy_name)
        self._resolve("sell", self._sell, profile.sell_strategy_name)
        self._resolve("cancel", self._cancel, profile.cancel_strategy_name)
        required_defaults = {
            "max_daily_buys",
            "stop_loss_pct",
            "stop_loss_limit_pct",
            "take_profit_half_pct",
            "take_profit_sell_fraction",
            "take_profit_remainder_stop_pct",
        }
        missing_defaults = sorted(required_defaults.difference(profile.runtime_defaults))
        if missing_defaults:
            raise ValueError(
                f"Strategy profile {name!r} is missing runtime defaults: "
                f"{', '.join(missing_defaults)}"
            )
        self._profiles[name] = profile

    def watchlist(self, name: str) -> WatchlistStrategy:
        return self._resolve("watchlist", self._watchlist, name)

    def buy(self, name: str) -> BuyStrategy:
        return self._resolve("buy", self._buy, name)

    def sell(self, name: str) -> SellStrategy:
        return self._resolve("sell", self._sell, name)

    def cancel(self, name: str) -> CancelStrategy:
        return self._resolve("cancel", self._cancel, name)

    def profile(self, name: str) -> StrategyProfile:
        return self._resolve("profile", self._profiles, name)

    def available_watchlist_names(self) -> tuple[str, ...]:
        return tuple(self._watchlist)

    def available_buy_names(self) -> tuple[str, ...]:
        return tuple(self._buy)

    def available_sell_names(self) -> tuple[str, ...]:
        return tuple(self._sell)

    def available_cancel_names(self) -> tuple[str, ...]:
        return tuple(self._cancel)

    def available_profile_names(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    @staticmethod
    def _register(
        kind: str,
        target: dict[str, StrategyT],
        strategy: StrategyT,
        required_methods: tuple[str, ...],
    ) -> None:
        """校验策略名称与接口后写入对应分类；重复名称会直接报错。"""
        name = StrategyRegistry._normalized_name(getattr(strategy, "name", ""), kind)
        if name in target:
            raise ValueError(f"Duplicate {kind} strategy: {name!r}")
        missing_methods = [
            method_name
            for method_name in required_methods
            if not callable(getattr(strategy, method_name, None))
        ]
        if missing_methods:
            raise TypeError(
                f"{kind} strategy {name!r} is missing methods: {', '.join(missing_methods)}"
            )
        target[name] = strategy

    @staticmethod
    def _resolve(kind: str, source: dict[str, StrategyT], name: str) -> StrategyT:
        """按名称取得已注册实现；未知名称会列出当前可选项。"""
        normalized = StrategyRegistry._normalized_name(name, kind)
        try:
            return source[normalized]
        except KeyError as exc:
            choices = ", ".join(source) or "(none registered)"
            raise ValueError(f"Unknown {kind} strategy {normalized!r}; choose one of: {choices}") from exc

    @staticmethod
    def _normalized_name(name: str, kind: str) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError(f"{kind} strategy name must not be empty")
        return normalized


_REGISTRY: StrategyRegistry | None = None
_BOOTSTRAP_LOCK = RLock()


def get_strategy_registry() -> StrategyRegistry:
    """取得进程级注册表，并按固定顺序装载内置策略和自定义扩展。

    使用候选对象完整注册成功后才发布，避免扩展注册失败时留下半成品全局状态。
    """
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    with _BOOTSTRAP_LOCK:
        if _REGISTRY is not None:
            return _REGISTRY
        from .builtins import register_builtin_strategies
        from .extensions import register_custom_strategies

        # Build off to the side so a broken extension cannot leave a half-filled
        # process-global registry behind for a later retry.
        candidate = StrategyRegistry()
        register_builtin_strategies(candidate)
        register_custom_strategies(candidate)
        _REGISTRY = candidate
        return candidate
