from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


SPECIAL_US_EQUITY_MARKET_CLOSURES = {
    date(2025, 1, 9): "National Day of Mourning for President Jimmy Carter",
}


@dataclass(frozen=True)
class TradingDayDecision:
    target_date: date
    is_trading_day: bool
    source: str
    reason: str
    open_time: str = ""
    close_time: str = ""


def trading_day_decision(target_date: date, *, use_alpaca: bool = True) -> TradingDayDecision:
    """判断目标日期是否为美股交易日；优先 Alpaca calendar，失败后用本地节假日表兜底。"""
    if use_alpaca:
        try:
            return alpaca_trading_day_decision(target_date)
        except Exception as exc:
            fallback = offline_trading_day_decision(target_date)
            return TradingDayDecision(
                target_date=target_date,
                is_trading_day=fallback.is_trading_day,
                source="offline_after_alpaca_error",
                reason=f"{fallback.reason}; Alpaca calendar unavailable: {type(exc).__name__}: {exc}",
            )
    return offline_trading_day_decision(target_date)


def alpaca_trading_day_decision(target_date: date) -> TradingDayDecision:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    from .alpaca_connection import load_alpaca_credentials

    api_key, secret_key = load_alpaca_credentials()
    errors: list[str] = []
    for paper in (True, False):
        mode = "paper" if paper else "live"
        client = TradingClient(api_key, secret_key, paper=paper)
        try:
            sessions = client.get_calendar(GetCalendarRequest(start=target_date, end=target_date))
        except Exception as exc:
            errors.append(f"{mode}: {type(exc).__name__}: {exc}")
            continue

        if not sessions:
            return TradingDayDecision(target_date, False, f"alpaca_{mode}", "Alpaca calendar returned no trading session")

        session = sessions[0]
        return TradingDayDecision(
            target_date=target_date,
            is_trading_day=True,
            source=f"alpaca_{mode}",
            reason="Alpaca calendar returned a trading session",
            open_time=str(getattr(session, "open", "") or ""),
            close_time=str(getattr(session, "close", "") or ""),
        )

    raise RuntimeError(" | ".join(errors) or "Alpaca calendar request failed")


def offline_trading_day_decision(target_date: date) -> TradingDayDecision:
    if target_date.weekday() >= 5:
        return TradingDayDecision(target_date, False, "offline", "Weekend")

    holiday = us_equity_holiday_name(target_date)
    if holiday:
        return TradingDayDecision(target_date, False, "offline", holiday)

    return TradingDayDecision(target_date, True, "offline", "Weekday and not a standard US equity market holiday")


def latest_trading_day_on_or_before(target_date: date) -> date:
    """Return the latest standard US equity trading day on or before ``target_date``."""
    candidate = target_date
    while not offline_trading_day_decision(candidate).is_trading_day:
        candidate -= timedelta(days=1)
    return candidate


def us_equity_holiday_name(target_date: date) -> str:
    holidays: dict[date, str] = {}
    for year in (target_date.year - 1, target_date.year, target_date.year + 1):
        holidays.update(us_equity_holidays_for_year(year))
    return holidays.get(target_date, "")


def us_equity_holidays_for_year(year: int) -> dict[date, str]:
    holidays = {
        observed_fixed_holiday(year, 1, 1): "New Year's Day",
        nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        nth_weekday(year, 2, 0, 3): "Presidents' Day",
        easter_sunday(year) - timedelta(days=2): "Good Friday",
        last_weekday(year, 5, 0): "Memorial Day",
        observed_fixed_holiday(year, 6, 19): "Juneteenth",
        observed_fixed_holiday(year, 7, 4): "Independence Day",
        nth_weekday(year, 9, 0, 1): "Labor Day",
        nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        observed_fixed_holiday(year, 12, 25): "Christmas Day",
    }
    holidays.update(
        {
            closure_date: name
            for closure_date, name in SPECIAL_US_EQUITY_MARKET_CLOSURES.items()
            if closure_date.year == year
        }
    )
    return holidays


def observed_fixed_holiday(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:
        return actual - timedelta(days=1)
    if actual.weekday() == 6:
        return actual + timedelta(days=1)
    return actual


def nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    candidate = date(year, month, 1)
    days_until_weekday = (weekday - candidate.weekday()) % 7
    return candidate + timedelta(days=days_until_weekday + 7 * (nth - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        candidate = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        candidate = date(year, month + 1, 1) - timedelta(days=1)
    return candidate - timedelta(days=(candidate.weekday() - weekday) % 7)


def easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)
