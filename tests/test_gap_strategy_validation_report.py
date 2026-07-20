from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backtest.gap_strategy_validation_report import read_trade_csv


class GapStrategyValidationReportTests(unittest.TestCase):
    def test_read_trade_csv_preserves_fields(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            path.write_text(
                "timestamp,symbol,side,quantity,price,gross_value,fee,"
                "cash_after,realized_pnl,price_change_pct,signal_day,rule,reason\n"
                "2025-01-02T09:35:00-05:00,US.TEST,BUY,10,5,50,0,"
                "99950,0,-0.05,2024-12-31,buy_limit,entry\n",
                encoding="utf-8",
            )
            trades = read_trade_csv(path)

        self.assertEqual(len(trades), 1)
        self.assertEqual(
            trades[0].timestamp,
            datetime.fromisoformat("2025-01-02T09:35:00-05:00"),
        )
        self.assertEqual(trades[0].signal_day, date(2024, 12, 31))
        self.assertEqual(trades[0].rule, "buy_limit")
        self.assertEqual(trades[0].reason, "entry")


if __name__ == "__main__":
    unittest.main()
