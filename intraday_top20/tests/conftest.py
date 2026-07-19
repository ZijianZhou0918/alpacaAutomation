from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

import pandas as pd

from intraday_top20.data.loader import DailyMarketData

EASTERN = ZoneInfo("America/New_York")


class InMemoryLoader:
    def __init__(self, days: list[DailyMarketData]):
        self.days = days
        self.files = [(Path(f"{day.trade_date}.csv"), day.trade_date) for day in days]
        self.data_quality = {
            "source_label": "unit_test",
            "example_mode": True,
            "reliable_for_strategy_claim": False,
            "start_date": days[0].trade_date.isoformat() if days else "",
            "end_date": days[-1].trade_date.isoformat() if days else "",
            "five_minute_bar_count": sum(len(day.bars) for day in days),
        }

    def fingerprint(self) -> str:
        return "unit-test"

    def iter_days(self) -> Iterator[DailyMarketData]:
        yield from self.days


def signal_day(
    target_date: date,
    *,
    include_entry_bar: bool = True,
    include_eod_bar: bool = True,
    take_profit_high: bool = False,
) -> DailyMarketData:
    closes = [11.0, 9.0, 9.0, 9.0, 9.0, 9.0, 11.0]
    rows = []
    for index, close in enumerate(closes):
        timestamp = datetime.combine(target_date, time(9, 30), EASTERN) + timedelta(minutes=5 * index)
        rows.append(_bar(timestamp, close, high=max(close, 11.1)))
    if include_entry_bar:
        entry_time = datetime.combine(target_date, time(10, 5), EASTERN)
        rows.append(_bar(entry_time, 10.0, high=13.0 if take_profit_high else 10.5, open_price=10.0))
    if include_eod_bar:
        eod = datetime.combine(target_date, time(15, 55), EASTERN)
        rows.append(_bar(eod, 10.2, high=10.3, open_price=10.2))
    return DailyMarketData(
        target_date,
        pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True),
        {"AAA": 8.0},
        {"AAA": True},
    )


def quiet_day(target_date: date) -> DailyMarketData:
    rows = [
        _bar(datetime.combine(target_date, time(9, 30), EASTERN), 10.0, high=10.1),
        _bar(datetime.combine(target_date, time(15, 55), EASTERN), 10.1, high=10.2, open_price=10.1),
    ]
    return DailyMarketData(target_date, pd.DataFrame(rows), {"AAA": 10.0}, {"AAA": True})


def _bar(timestamp: datetime, close: float, *, high: float, open_price: float | None = None) -> dict[str, object]:
    open_price = close if open_price is None else open_price
    volume = 100_000.0
    return {
        "symbol": "AAA",
        "timestamp": timestamp,
        "open": open_price,
        "high": max(high, open_price, close),
        "low": min(open_price, close) * 0.99,
        "close": close,
        "volume": volume,
        "dollar_value": 10.0 * volume,
        "transactions": 1_000,
    }


def pytest_sessionfinish(session, exitstatus: int) -> None:
    target = Path(__file__).resolve().parents[1] / "outputs" / "validation" / "test_summary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "passed": exitstatus == 0,
                "status": "全部通过" if exitstatus == 0 else f"失败，pytest exit={exitstatus}",
                "tests_collected": session.testscollected,
                "completed_at": datetime.now().astimezone().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
