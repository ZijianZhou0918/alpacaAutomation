from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import Settings


REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
REALTIME_ORDER_OPEN = time(4, 0)
REALTIME_ORDER_CLOSE = time(20, 0)


def now_market_time(settings: Settings) -> datetime:
    """返回配置市场时区下的当前时间。"""
    return datetime.now(ZoneInfo(settings.market_timezone))


def is_regular_market_time(now_et: datetime) -> bool:
    """判断当前美东时间是否在美股常规交易时段。"""
    return now_et.weekday() < 5 and REGULAR_OPEN <= now_et.time() <= REGULAR_CLOSE


def is_realtime_order_time(now_et: datetime) -> bool:
    """判断当前是否有实时价可支撑监控下单；本项目不使用日线 close 去下单。"""
    return now_et.weekday() < 5 and REALTIME_ORDER_OPEN <= now_et.time() <= REALTIME_ORDER_CLOSE


def next_poll_seconds(settings: Settings, now_et: datetime) -> int:
    """根据是否处在可下单观察时段决定下一轮轮询等待多久。"""
    return settings.regular_poll_seconds if is_realtime_order_time(now_et) else settings.idle_poll_seconds
