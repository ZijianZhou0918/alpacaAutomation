from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    current_price: float
    previous_closes: list[float]
    as_of: datetime

    @property
    def today_ma5(self) -> float:
        """用前 4 个完成交易日收盘价和当前价计算今日动态 MA5。"""
        return (sum(self.previous_closes[-4:]) + self.current_price) / 5.0


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_price: float
    opened_at: str
    source: str = "dry-run"


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: str
    reason: str
    current_price: float
    quantity: float = 0.0
    diagnostics: dict[str, float | str] = field(default_factory=dict)

    @property
    def should_order(self) -> bool:
        """判断信号是否需要提交真实/模拟订单。"""
        return self.action in {"BUY", "SELL_ALL"}


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    status: str
    message: str
