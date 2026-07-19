from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...models import OrderResult
from ..names import DEFAULT_CANCEL_STRATEGY_NAME

if TYPE_CHECKING:
    from ..registry import StrategyRegistry


@dataclass(frozen=True)
class TimeoutCancelConfirmedStrategy:
    """默认撤单策略：等待订单，超时撤单，再确认最终状态。"""

    name: str = DEFAULT_CANCEL_STRATEGY_NAME
    description: str = "等待配置时长，未完全成交时撤单并复查最终状态"

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
        """把已提交订单交给终态保护层；超时可能产生真实撤单写操作。"""
        from ...order_guard import wait_for_fill_or_cancel

        # 【自动撤单策略入口】
        # 本组件只决定采用“等待终态、超时撤单、撤单后复查”的标准流程；
        # 轮询、部分成交识别和唯一 SDK 撤单写入统一留在 order_guard.py，
        # 防止不同策略各自实现撤单后遗漏最终状态确认。
        return wait_for_fill_or_cancel(
            client,
            raw_order,
            symbol,
            side,
            quantity,
            price,
            source_name,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )

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
        """执行显式撤单；自动超时和手动指令最终共用同一底层函数。"""
        from ...order_guard import cancel_unfilled_order

        # 【显式撤单策略入口】
        # Broker 已完成订单读取和终态短路；这里把经过确认的订单元数据传给
        # 唯一底层撤单函数。下一层调用 client.cancel_order_by_id 后仍会复查状态。
        return cancel_unfilled_order(
            client,
            order_id,
            symbol,
            side,
            quantity,
            price,
            timeout_seconds,
            success_message,
            failure_prefix,
        )


def register_builtin_cancel_strategies(registry: StrategyRegistry) -> None:
    """注册默认的超时撤单并确认终态策略。"""
    registry.register_cancel(TimeoutCancelConfirmedStrategy())
