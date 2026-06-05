from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    current_price: float
    previous_closes: list[float]
    as_of: datetime
    current_price_source: str = ""
    today_open: float = 0.0
    today_open_source: str = ""

    @property
    def today_ma5(self) -> float:
        """动态 MA5 = 前 4 个完成日收盘价 + 当前价。"""
        return (sum(self.previous_closes[-4:]) + self.current_price) / 5.0

    @property
    def signal_day_gain_pct(self) -> float:
        """信号日涨幅 = 最近已完成日线收盘价 / 前一日收盘价 - 1。"""
        if len(self.previous_closes) < 2:
            return 0.0
        previous_close = self.previous_closes[-2]
        signal_close = self.previous_closes[-1]
        if previous_close <= 0:
            return 0.0
        return signal_close / previous_close - 1.0

    @property
    def today_open_gain_pct(self) -> float:
        """当天开盘涨幅 = 今日常规盘开盘价 / 信号日收盘价 - 1。"""
        if self.today_open <= 0 or not self.previous_closes:
            return 0.0
        signal_close = self.previous_closes[-1]
        if signal_close <= 0:
            return 0.0
        return self.today_open / signal_close - 1.0


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
        """判断该信号是否需要触发订单动作。"""
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


def is_executed_order_status(status: str) -> bool:
    """判断订单是否至少有成交；撤单和拒单不算成功。"""
    status = status.upper()
    return status in {"FILLED", "DRY_RUN"} or status.startswith("PARTIALLY_FILLED")


def is_order_error_status(status: str) -> bool:
    """判断是否为提交阶段拒单；撤单失败不算拒单。"""
    status = status.upper()
    return status == "REJECTED"


def consumes_daily_buy_slot(status: str) -> bool:
    """判断买单是否占用每日名额；拒单不占，未确认风险占。"""
    status = status.upper()
    if is_order_error_status(status):
        return False
    return is_executed_order_status(status) or status in {
        "ACCEPTED",
        "CANCEL_FAILED",
        "CANCEL_REQUESTED",
        "HELD",
        "NEW",
        "PENDING_CANCEL",
        "PENDING_NEW",
        "SUBMITTED",
    }
