from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import unittest

from alpaca_ma5_service.trading_calendar import alpaca_trading_day_decisions


class FakeCalendarClient:
    def __init__(self):
        self.calls = []

    def get_calendar(self, request):
        self.calls.append(request)
        return [
            SimpleNamespace(
                date=date(2025, 7, 2),
                open="2025-07-02 09:30:00",
                close="2025-07-02 16:00:00",
            ),
            SimpleNamespace(
                date=date(2025, 7, 3),
                open="2025-07-03 09:30:00",
                close="2025-07-03 13:00:00",
            ),
        ]


class TradingCalendarRangeTests(unittest.TestCase):
    def test_range_uses_one_client_call_and_preserves_early_close(self):
        client = FakeCalendarClient()

        decisions = alpaca_trading_day_decisions(
            date(2025, 7, 2),
            date(2025, 7, 4),
            client_factory=lambda _paper: client,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(decisions[date(2025, 7, 2)].is_trading_day)
        self.assertEqual(decisions[date(2025, 7, 3)].close_time, "2025-07-03 13:00:00")
        self.assertFalse(decisions[date(2025, 7, 4)].is_trading_day)


if __name__ == "__main__":
    unittest.main()
