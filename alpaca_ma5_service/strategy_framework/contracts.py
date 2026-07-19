from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, Mapping, Protocol

from ..models import MarketSnapshot, OrderResult, Position, Signal

if TYPE_CHECKING:
    from ..config import Settings


RuntimeDefault = float | int | None


class WatchlistStrategy(Protocol):
    """WatchCode 选股契约：只提供筛选规则，不读取行情、不生成文件、不下单。"""

    name: str
    description: str

    def screen_rules(self):
        ...


class BuyStrategy(Protocol):
    """买入策略契约：把一份行情快照转换成 BUY/HOLD 信号，本身不提交订单。"""

    name: str
    description: str

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        ...

    def max_buy_today_current_gain_pct(self) -> float:
        ...

    def legacy_module(self) -> ModuleType:
        ...


class SellStrategy(Protocol):
    """卖出策略契约：把持仓和行情转换成卖出/持有信号，本身不提交订单。"""

    name: str
    description: str

    def evaluate(self, position: Position, snapshot: MarketSnapshot, now_et: datetime, settings: Settings) -> Signal:
        ...

    def evaluate_stop_loss(self, position: Position, snapshot: MarketSnapshot, settings: Settings) -> Signal:
        ...

    def evaluate_take_profit_remainder_stop(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        settings: Settings,
    ) -> Signal:
        ...


class CancelStrategy(Protocol):
    """撤单策略契约：订单提交后等待终态，并在规则满足时执行撤单。"""

    name: str
    description: str

    def wait_for_terminal(
        self,
        client,
        raw_order,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        source_name: str,
        *,
        timeout_seconds: int,
        poll_seconds: int,
    ) -> OrderResult:
        ...

    def cancel_order(
        self,
        client,
        order_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        *,
        timeout_seconds: int,
        success_message: str,
        failure_prefix: str,
    ) -> OrderResult:
        ...


@dataclass(frozen=True)
class StrategyProfile:
    """可复用的策略组合：同时给出 WatchCode、买入、卖出和撤单默认实现。"""

    name: str
    watchlist_strategy_name: str
    buy_strategy_name: str
    sell_strategy_name: str
    cancel_strategy_name: str
    runtime_defaults: Mapping[str, RuntimeDefault]
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_defaults", MappingProxyType(dict(self.runtime_defaults)))


@dataclass(frozen=True)
class StrategySelection:
    """从 profile 和单项覆盖中解析出的四个最终策略名称。"""

    profile_name: str
    watchlist_strategy_name: str
    buy_strategy_name: str
    sell_strategy_name: str
    cancel_strategy_name: str


@dataclass(frozen=True)
class StrategyRuntime:
    """交易前一次性解析并冻结的四类策略对象，避免运行中途切换实现。"""

    selection: StrategySelection
    watchlist: WatchlistStrategy
    buy: BuyStrategy
    sell: SellStrategy
    cancel: CancelStrategy
