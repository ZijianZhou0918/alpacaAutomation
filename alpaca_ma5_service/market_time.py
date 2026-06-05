from __future__ import annotations

from datetime import datetime, time, timedelta
from math import ceil
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
    """判断是否处于美股常规盘：09:30 <= t < 16:00 ET。"""
    return now_et.weekday() < 5 and REGULAR_OPEN <= now_et.time() < REGULAR_CLOSE


def is_realtime_order_time(now_et: datetime) -> bool:
    """判断是否处于允许使用实时价下单的窗口：04:00 <= t < 20:00 ET。"""
    return now_et.weekday() < 5 and REALTIME_ORDER_OPEN <= now_et.time() < REALTIME_ORDER_CLOSE


def is_premarket_time(now_et: datetime) -> bool:
    """判断是否为盘前；本策略盘前不买入。"""
    return now_et.weekday() < 5 and REALTIME_ORDER_OPEN <= now_et.time() < REGULAR_OPEN


def regular_open_has_started(now_et: datetime) -> bool:
    """常规盘开盘后，今日开盘价才有稳定含义。"""
    return now_et.weekday() < 5 and now_et.time() >= REGULAR_OPEN


def is_buy_order_time(now_et: datetime) -> bool:
    """真实买入窗口：实时价窗口内，但排除盘前。"""
    return is_realtime_order_time(now_et) and not is_premarket_time(now_et)


def next_poll_seconds(settings: Settings, now_et: datetime) -> int:
    """常规盘快速轮询；其他时间动态靠近下一次 9:30 开盘。"""
    if is_regular_market_time(now_et):
        return settings.regular_poll_seconds

    seconds_to_open = seconds_until_next_regular_market_open(now_et)
    return max(1, min(settings.idle_poll_seconds, seconds_to_open))


def seconds_until_next_regular_market_open(now_et: datetime) -> int:
    """计算距离下一个工作日 09:30 ET 的秒数。"""
    return max(0, ceil((next_regular_market_open(now_et) - now_et).total_seconds()))


def next_regular_market_open(now_et: datetime) -> datetime:
    """返回下一个工作日 09:30 ET。"""
    day = now_et.date()
    if now_et.weekday() < 5 and now_et.time() < REGULAR_OPEN:
        return datetime.combine(day, REGULAR_OPEN, tzinfo=now_et.tzinfo)

    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, REGULAR_OPEN, tzinfo=now_et.tzinfo)
