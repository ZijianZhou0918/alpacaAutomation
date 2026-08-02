from __future__ import annotations

import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.kdj_volume_reversal import (
    KdjVolumeReversalConfig,
    run_kdj_volume_reversal_backtest,
)


class KdjVolumeReversalBacktestTests(unittest.TestCase):
    def test_signal_enters_and_exits_on_next_bar_without_future_close_fill(self) -> None:
        with TemporaryDirectory() as tmp:
            database = Path(tmp) / "bars.sqlite"
            self._make_database(database)
            result = run_kdj_volume_reversal_backtest(
                database,
                KdjVolumeReversalConfig(
                    start_date=date(2025, 1, 1),
                    end_date=date(2025, 5, 31),
                    kdj_period=2,
                ),
            )

            self.assertEqual(result.raw_signal_count, 1)
            self.assertEqual(result.entered_trade_count, 1)
            trade = result.trades[0]
            self.assertEqual(trade.signal_date, "2025-01-03")
            self.assertEqual(trade.entry_date, "2025-01-04")
            self.assertEqual(trade.entry_price, 7.0)
            self.assertEqual(trade.exit_signal_date, "2025-01-06")
            self.assertEqual(trade.exit_date, "2025-01-07")
            self.assertEqual(trade.exit_price, 20.0)

    def test_requested_range_outside_metadata_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            database = Path(tmp) / "bars.sqlite"
            self._make_database(database)
            with self.assertRaisesRegex(RuntimeError, "outside dataset coverage"):
                run_kdj_volume_reversal_backtest(
                    database,
                    KdjVolumeReversalConfig(
                        start_date=date(2024, 1, 1),
                        end_date=date(2025, 1, 2),
                    ),
                )

    @staticmethod
    def _make_database(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE daily_dataset_metadata (
                status TEXT, feed TEXT, timeframe TEXT, adjustment TEXT,
                start_date TEXT, end_date TEXT
            );
            INSERT INTO daily_dataset_metadata VALUES
                ('complete', 'sip', '1Day', 'split', '2025-01-01', '2025-05-31');
            CREATE TABLE minute_bars (symbol TEXT);
            CREATE TABLE daily_bars (
                symbol TEXT, bar_date TEXT, feed TEXT, adjustment TEXT,
                open REAL, high REAL, low REAL, close REAL, volume REAL, ma5 REAL
            );
            """
        )
        rows = [
            ("TEST", "2025-01-01", 10, 11, 9, 10, 1_000, 10),
            ("TEST", "2025-01-02", 10, 10, 8, 8, 1_000, 9),
            ("TEST", "2025-01-03", 9, 13, 5, 5, 101_000, 8),
            ("TEST", "2025-01-04", 7, 8, 6, 7, 2_000, 7),
            ("TEST", "2025-01-05", 15, 30, 15, 30, 2_000, 20),
            ("TEST", "2025-01-06", 30, 30, 15, 30, 2_000, 25),
            ("TEST", "2025-01-07", 20, 21, 19, 20, 2_000, 20),
        ]
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?, 'sip', 'split', ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
        connection.close()


if __name__ == "__main__":
    unittest.main()
