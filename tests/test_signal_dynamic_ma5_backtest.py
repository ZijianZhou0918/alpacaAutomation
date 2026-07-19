from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo
import math
import sqlite3

from alpaca_ma5_service.afterhours_high_low import MinuteBar
from alpaca_ma5_service.trading_calendar import offline_trading_day_decision
from backtest.data_cache import MarketDataCache
from backtest.signal_dynamic_ma5 import (
    MinuteRow,
    SignalDynamicMa5Config,
    SignalDynamicMa5Simulator,
    SignalSnapshot,
    TradeCandidate,
    inspect_daily_database,
    run_signal_dynamic_ma5_backtest,
    screen_daily_candidates,
    validate_daily_dataset,
)


ET = ZoneInfo("America/New_York")


class SignalDynamicMa5SimulatorTests(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = SignalDynamicMa5Config(
            database_path=root / "daily.sqlite",
            minute_cache_path=root / "minute.sqlite",
            output_dir=root / "output",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_trigger_uses_completed_bar_and_enters_at_next_bar_open(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute("2026-01-21T14:30:00+00:00", 104, 104, 99, 100),
                minute("2026-01-21T14:31:00+00:00", 100, 115, 99, 110),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 1)
        self.assertEqual(len(simulator.trades), 1)
        trade = simulator.trades[0]
        self.assertEqual(trade.entry_timestamp_utc, "2026-01-21T14:31:00+00:00")
        self.assertAlmostEqual(trade.entry_price, 100.0)
        self.assertEqual(trade.target_hits, (0.05, 0.10, 0.15))
        self.assertEqual(trade.exit_reason, "targets_complete")
        self.assertAlmostEqual(trade.return_pct, 0.10)

    def test_stop_loss_wins_when_stop_and_targets_hit_same_minute(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute("2026-01-21T14:30:00+00:00", 103, 103, 99, 100),
                minute("2026-01-21T14:31:00+00:00", 100, 120, 89, 110),
            ],
        )

        trade = simulator.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertEqual(trade.target_hits, ())
        self.assertAlmostEqual(trade.return_pct, -0.10)

    def test_last_buy_window_minute_trigger_has_no_lookahead_fill(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [minute("2026-01-21T16:59:00+00:00", 103, 103, 99, 100)],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 1)
        self.assertEqual(simulator.trades, [])

    def test_entry_at_1159_et_is_allowed(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute("2026-01-21T16:58:00+00:00", 100, 100, 99, 100),
                minute("2026-01-21T16:59:00+00:00", 100, 100, 99, 100),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 1)
        self.assertEqual(len(simulator.trades), 1)
        self.assertEqual(
            simulator.trades[0].entry_timestamp_utc,
            "2026-01-21T16:59:00+00:00",
        )

    def test_entry_at_noon_et_is_rejected(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute("2026-01-21T16:59:00+00:00", 100, 100, 99, 100),
                minute("2026-01-21T17:00:00+00:00", 100, 100, 99, 100),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 1)
        self.assertEqual(simulator.trades, [])

    def test_touch_at_noon_et_is_not_a_buy_trigger(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute("2026-01-21T17:00:00+00:00", 100, 100, 99, 100),
                minute("2026-01-21T17:01:00+00:00", 100, 100, 99, 100),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 0)
        self.assertEqual(simulator.trades, [])

    def test_price_above_dynamic_ma5_does_not_trigger(self) -> None:
        just_above_ma5 = math.nextafter(100.0, math.inf)
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute(
                    "2026-01-21T14:30:00+00:00",
                    just_above_ma5,
                    just_above_ma5,
                    100,
                    just_above_ma5,
                ),
                minute(
                    "2026-01-21T14:31:00+00:00",
                    just_above_ma5,
                    just_above_ma5,
                    100,
                    just_above_ma5,
                ),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 0)
        self.assertEqual(simulator.trades, [])

    def test_price_below_dynamic_ma5_counts_as_already_touched(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute("2026-01-21T14:30:00+00:00", 95, 95, 90, 90),
                minute("2026-01-21T14:31:00+00:00", 92, 93, 91, 92),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 1)
        self.assertEqual(len(simulator.trades), 1)
        self.assertEqual(simulator.trades[0].exit_reason, "market_close")

    def test_exactly_fifteen_percent_drop_does_not_trigger(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(previous_four_closes=(110.0, 110.0, 110.0, 110.0)),
            [
                minute("2026-01-21T14:30:00+00:00", 102, 102, 102, 102),
                minute("2026-01-21T14:31:00+00:00", 102, 102, 102, 102),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 0)
        self.assertEqual(simulator.trades, [])

    def test_drop_strictly_greater_than_fifteen_percent_triggers(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(previous_four_closes=(110.0, 110.0, 110.0, 110.0)),
            [
                minute(
                    "2026-01-21T14:30:00+00:00",
                    101.99,
                    101.99,
                    101.99,
                    101.99,
                ),
                minute(
                    "2026-01-21T14:31:00+00:00",
                    101.99,
                    101.99,
                    101.99,
                    101.99,
                ),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 1)
        self.assertEqual(len(simulator.trades), 1)

    def test_rebound_above_fifteen_percent_drop_is_not_bought(self) -> None:
        simulator = SignalDynamicMa5Simulator(self.config)
        simulator.simulate_candidate(
            make_candidate(),
            [
                minute("2026-01-21T14:30:00+00:00", 100, 100, 100, 100),
                minute("2026-01-21T14:31:00+00:00", 103, 103, 103, 103),
            ],
        )

        self.assertEqual(simulator.dynamic_ma5_trigger_days, 1)
        self.assertEqual(simulator.trades, [])


class SignalDynamicMa5DailyScreenTests(TestCase):
    def test_daily_screen_finds_strict_signal_and_records_open_diagnostic(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.sqlite"
            build_daily_database(path, next_day_open=111.0)
            config = make_config(Path(tmp), path)
            metadata, sessions = inspect_daily_database(path)
            validate_daily_dataset(config, metadata, sessions)

            result = screen_daily_candidates(config, metadata, sessions)

            self.assertEqual(result.signal_days, 1)
            self.assertEqual(result.next_day_symbol_sessions, 1)
            self.assertEqual(result.positive_gap_days, 1)
            self.assertEqual(len(result.candidates), 1)
            candidate = result.candidates[0]
            self.assertEqual(candidate.signal.previous_four_closes, (96.0, 97.0, 98.0, 110.0))
            self.assertAlmostEqual(candidate.open_gain_pct, 1 / 110)

    def test_equal_open_is_not_positive(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.sqlite"
            build_daily_database(path, next_day_open=110.0)
            config = make_config(Path(tmp), path)
            metadata, sessions = inspect_daily_database(path)

            result = screen_daily_candidates(config, metadata, sessions)

            self.assertEqual(result.signal_days, 1)
            self.assertEqual(result.positive_gap_days, 0)
            self.assertEqual(result.candidates, ())

    def test_lower_open_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.sqlite"
            build_daily_database(path, next_day_open=99.0)
            config = make_config(Path(tmp), path)
            metadata, sessions = inspect_daily_database(path)

            result = screen_daily_candidates(config, metadata, sessions)

            self.assertEqual(result.signal_days, 1)
            self.assertEqual(result.positive_gap_days, 0)
            self.assertEqual(result.candidates, ())

    def test_reliability_gate_rejects_incomplete_dataset(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.sqlite"
            build_daily_database(path, next_day_open=111.0, status="building")
            config = make_config(Path(tmp), path)
            metadata, sessions = inspect_daily_database(path)

            with self.assertRaisesRegex(RuntimeError, "status='building'"):
                validate_daily_dataset(config, metadata, sessions)

    def test_runner_fetches_candidate_minutes_once_then_reuses_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "daily.sqlite"
            build_daily_database(path, next_day_open=120.0)
            config = make_config(root, path)
            calls: list[tuple[list[str], datetime, datetime]] = []

            def fetcher(
                symbols: list[str],
                start: datetime,
                end: datetime,
            ) -> dict[str, list[MinuteBar]]:
                calls.append((symbols, start, end))
                return {
                    "AAA": [
                        MinuteBar("AAA", start, 104, 104, 99, 100),
                        MinuteBar(
                            "AAA",
                            start + timedelta(minutes=1),
                            100,
                            106,
                            99,
                            105,
                        ),
                    ]
                }

            first = run_signal_dynamic_ma5_backtest(
                config,
                progress=None,
                minute_fetcher=fetcher,
            )
            second = run_signal_dynamic_ma5_backtest(
                config,
                progress=None,
                minute_fetcher=lambda *_: self.fail("cache should prevent a refetch"),
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(first.summary.trades, 1)
            self.assertEqual(second.summary.trades, 1)
            self.assertTrue(first.summary_json_path.exists())
            self.assertTrue(first.trades_csv_path.exists())
            self.assertTrue(first.html_report_path.exists())


def make_config(root: Path, database_path: Path) -> SignalDynamicMa5Config:
    return SignalDynamicMa5Config(
        database_path=database_path,
        minute_cache_path=root / "minute.sqlite",
        output_dir=root / "output",
    )


def make_candidate(
    *,
    buy_day_open: float = 120.0,
    previous_four_closes: tuple[float, float, float, float] = (
        100.0,
        100.0,
        100.0,
        100.0,
    ),
) -> TradeCandidate:
    signal = SignalSnapshot(
        signal_date="2026-01-20",
        next_session_date="2026-01-21",
        signal_close=100.0,
        signal_gain_pct=0.12,
        signal_body_pct=0.11,
        ma5=99.0,
        ma10=98.0,
        ma20=97.0,
        previous_four_closes=previous_four_closes,
    )
    return TradeCandidate(
        symbol="AAA",
        signal=signal,
        buy_date="2026-01-21",
        buy_day_open=buy_day_open,
        open_gain_pct=buy_day_open / 100.0 - 1.0,
    )


def minute(
    timestamp_utc: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> MinuteRow:
    return MinuteRow(
        symbol="AAA",
        timestamp_utc=timestamp_utc,
        open=open_price,
        high=high,
        low=low,
        close=close,
    )


def build_daily_database(
    path: Path,
    *,
    next_day_open: float,
    status: str = "complete",
) -> None:
    MarketDataCache(path)
    start = date(2025, 12, 1)
    sessions: list[date] = []
    candidate_session = start
    while len(sessions) < 21:
        if offline_trading_day_decision(candidate_session).is_trading_day:
            sessions.append(candidate_session)
        candidate_session += timedelta(days=1)
    closes = [float(value) for value in range(80, 99)] + [110.0, 109.0]
    rows = []
    for index, (session, close) in enumerate(zip(sessions, closes)):
        open_price = close - 1.0
        if index == 19:
            open_price = 90.0
        elif index == 20:
            open_price = next_day_open
        rows.append(
            (
                "AAA",
                session.isoformat(),
                "sip",
                "split",
                open_price,
                max(open_price, close) + 1,
                min(open_price, close) - 1,
                close,
                "2026-01-01T00:00:00+00:00",
            )
        )

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE daily_dataset_metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                adjustment TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                expected_sessions INTEGER NOT NULL,
                candidate_symbols INTEGER NOT NULL,
                completed_batches INTEGER NOT NULL,
                total_rows INTEGER NOT NULL,
                request_pages INTEGER NOT NULL,
                skipped_rows INTEGER NOT NULL,
                universe_sha256 TEXT NOT NULL,
                classification_counts_json TEXT NOT NULL,
                security_master_method TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE security_master (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                source_status TEXT NOT NULL,
                tradable INTEGER NOT NULL,
                classification_reason TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        conn.execute(
            """
            INSERT INTO daily_dataset_metadata
            VALUES (1, ?, 'test', 'sip', '1Day', 'split', ?, ?, 21, 1, 1, 21,
                    1, 0, 'hash', '{}', 'test', ?, ?)
            """,
            (
                status,
                sessions[0].isoformat(),
                sessions[-1].isoformat(),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00" if status == "complete" else None,
            ),
        )
        conn.execute(
            "INSERT INTO security_master VALUES ('AAA', 'AAA Corp', 'NASDAQ', 'active', 1, 'common')"
        )
        conn.executemany(
            """
            INSERT INTO daily_bars(
                symbol, bar_date, feed, adjustment, open, high, low, close, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
