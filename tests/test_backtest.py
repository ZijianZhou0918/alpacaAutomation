from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import unittest
from unittest.mock import patch

from alpaca_ma5_service import strategy_gap_confirmed_pullback as gap_strategy_module
from alpaca_ma5_service import watchlist_generator as watchlist_module
from alpaca_ma5_service.afterhours_high_low import MinuteBar
from alpaca_ma5_service.watchlist_generator import DailyBar
from backtest.data_cache import MarketDataCache
from backtest.daily_sources import DailyFetchResult, coalesced_date_ranges, failure_dates, massive_grouped_daily_rows_from_payload, split_api_keys
from backtest.data_repair import DataRepairConfig, run_data_repair
from backtest.data_spotcheck import DataSpotcheckConfig, run_data_spotcheck
from backtest.engine import (
    BacktestConfig,
    TradeRecord,
    build_symbol_detail_payload,
    build_symbol_minute_payload,
    build_daily_bars,
    build_historical_watchlists,
    daily_request_end_date_exclusive,
    fetch_backtest_daily_bars,
    fetch_candidate_day_minute_bars,
    run_backtest,
    sorted_watch_candidates,
    symbol_detail_table,
    trade_kline_location,
)
from tests.test_strategy import make_settings


def trading_days(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def minute_bar(day: date, at: time, open_price: float, high: float, low: float, close: float, symbol: str = "TEST") -> MinuteBar:
    timestamp = datetime.combine(day, at, tzinfo=ZoneInfo("America/New_York"))
    return MinuteBar(symbol, timestamp, open_price, high, low, close)


def make_backtest_config(root: Path, trade_day: date) -> BacktestConfig:
    settings = make_settings(root)
    output_dir = root / "backtest_output"
    settings = replace(
        settings,
        output_dir=output_dir,
        state_file=output_dir / "state.json",
        close_liquidation_start=time(15, 55),
        close_liquidation_end=time(16, 0),
    )
    return BacktestConfig(
        symbols=["US.TEST"],
        start_date=trade_day,
        end_date=trade_day,
        timeframe="1Min",
        initial_cash=10_000.0,
        buy_notional_usd=1_000.0,
        buy_position_pct=0.0,
        max_positions=1,
        max_daily_buys=2,
        commission_per_order=0.0,
        slippage_pct=0.0,
        allow_repeat_buys=False,
        allow_overnight_holding=False,
        allow_fractional_shares=False,
        data_feed="iex",
        daily_data_source="alpaca",
        batch_size=100,
        data_chunk_days=14,
        use_data_cache=False,
        cache_daily_bars=True,
        cache_minute_bars=True,
        refresh_data_cache=False,
        data_cache_dir=root / "backtest_data",
        data_cache_name="market_data.sqlite",
        warmup_calendar_days=0,
        market_timezone=settings.market_timezone,
        order_timeout_seconds=600,
        report_max_points_per_series=500,
        report_max_price_symbols=0,
        report_price_context_days=5,
        stock_pool_description="unit-test symbols",
        require_buy_day_open_below_signal_reference=False,
        output_dir=output_dir,
        html_report_name="backtest_report.html",
        strategy_name=gap_strategy_module.STRATEGY_NAME,
        strategy_settings=settings,
        watchlist_signal_params={
            "MIN_SIGNAL_GAIN_PCT": watchlist_module.MIN_SIGNAL_GAIN_PCT,
            "MIN_SIGNAL_GAIN_OVER_MA5_GAIN_PCT": watchlist_module.MIN_SIGNAL_GAIN_OVER_MA5_GAIN_PCT,
            "MIN_OPEN_TO_MA5_RATIO": watchlist_module.MIN_OPEN_TO_MA5_RATIO,
            "MIN_CLOSE_TO_MA5_RATIO": watchlist_module.MIN_CLOSE_TO_MA5_RATIO,
        },
        buy_signal_params={
            "MIN_SIGNAL_DAY_GAIN_PCT": gap_strategy_module.MIN_SIGNAL_DAY_GAIN_PCT,
            "MID_SIGNAL_DAY_GAIN_PCT": gap_strategy_module.MID_SIGNAL_DAY_GAIN_PCT,
            "HIGH_SIGNAL_DAY_GAIN_PCT": gap_strategy_module.HIGH_SIGNAL_DAY_GAIN_PCT,
            "MID_OPEN_GAIN_PCT": gap_strategy_module.MID_OPEN_GAIN_PCT,
            "HIGH_OPEN_GAIN_PCT": gap_strategy_module.HIGH_OPEN_GAIN_PCT,
            "BUY_TRIGGER_DISTANCE_PCT": gap_strategy_module.BUY_TRIGGER_DISTANCE_PCT,
            "MIN_TODAY_OPEN_GAIN_PCT": gap_strategy_module.MIN_TODAY_OPEN_GAIN_PCT,
            "MAX_TODAY_OPEN_GAIN_PCT": gap_strategy_module.MAX_TODAY_OPEN_GAIN_PCT,
            "MIN_TODAY_OPEN_VS_OPEN_MA5_PCT": gap_strategy_module.MIN_TODAY_OPEN_VS_OPEN_MA5_PCT,
            "MIN_TODAY_CURRENT_GAIN_PCT": gap_strategy_module.MIN_TODAY_CURRENT_GAIN_PCT,
            "MAX_BUY_TODAY_CURRENT_GAIN_PCT": gap_strategy_module.MAX_BUY_TODAY_CURRENT_GAIN_PCT,
            "MIN_CURRENT_VS_TODAY_MA5_PCT": gap_strategy_module.MIN_CURRENT_VS_TODAY_MA5_PCT,
        },
        sell_signal_params={
            "close_liquidation_start": settings.close_liquidation_start,
            "close_liquidation_end": settings.close_liquidation_end,
        },
        stop_params={
            "stop_loss_pct": settings.stop_loss_pct,
            "stop_loss_limit_pct": settings.stop_loss_limit_pct,
            "take_profit_half_pct": settings.take_profit_half_pct,
            "take_profit_sell_fraction": settings.take_profit_sell_fraction,
            "take_profit_remainder_stop_pct": settings.take_profit_remainder_stop_pct,
        },
    )


def make_signal_and_trade_bars() -> tuple[date, dict[str, list[MinuteBar]]]:
    days = trading_days(date(2026, 1, 1), 21)
    bars: list[MinuteBar] = []
    for day in days[:19]:
        bars.append(minute_bar(day, time(9, 30), 10.0, 10.0, 10.0, 10.0))
        bars.append(minute_bar(day, time(15, 59), 10.0, 10.0, 10.0, 10.0))

    signal_day = days[19]
    bars.append(minute_bar(signal_day, time(9, 30), 12.0, 12.2, 11.9, 12.1))
    bars.append(minute_bar(signal_day, time(15, 59), 12.8, 13.2, 12.7, 13.0))

    trade_day = days[20]
    bars.append(minute_bar(trade_day, time(9, 30), 13.0, 13.1, 12.9, 13.0))
    bars.append(minute_bar(trade_day, time(10, 0), 12.3, 12.35, 12.25, 12.3))
    bars.append(minute_bar(trade_day, time(10, 1), 12.3, 12.4, 12.25, 12.35))
    bars.append(minute_bar(trade_day, time(12, 0), 12.3, 12.45, 12.25, 12.4))
    bars.append(minute_bar(trade_day, time(15, 55), 12.6, 12.65, 12.55, 12.6))
    return trade_day, {"TEST": bars}


def clone_bars(symbol: str, bars: list[MinuteBar]) -> list[MinuteBar]:
    return [
        MinuteBar(symbol, bar.timestamp, bar.open, bar.high, bar.low, bar.close)
        for bar in bars
    ]


class BacktestTests(unittest.TestCase):
    def test_symbol_detail_table_defaults_to_latest_activity_first(self):
        timezone = ZoneInfo("America/New_York")

        def trade(symbol: str, day: int, hour: int, side: str = "BUY") -> TradeRecord:
            return TradeRecord(
                timestamp=datetime(2026, 7, day, hour, 30, tzinfo=timezone),
                symbol=symbol,
                side=side,
                quantity=1.0,
                price=10.0,
                gross_value=10.0,
                fee=0.0,
                cash_after=1_000.0,
                realized_pnl=2.0 if side == "SELL" else 0.0,
                reason="test",
                rule="test",
            )

        content = symbol_detail_table(
            [
                trade("US.OLD", 1, 10),
                trade("US.NEW", 15, 9),
                trade("US.MID", 8, 11),
                trade("US.NEW", 16, 14, "SELL"),
            ]
        )

        self.assertLess(content.index("data-symbol='NEW'"), content.index("data-symbol='MID'"))
        self.assertLess(content.index("data-symbol='MID'"), content.index("data-symbol='OLD'"))
        self.assertIn("data-latest-time='2026-07-16T14:30-04:00'", content)
        self.assertIn("本轮最新时间", content)
        self.assertIn("data-row-rank", content)

    def test_symbol_detail_separates_round_and_cumulative_realized_pnl(self):
        timezone = ZoneInfo("America/New_York")
        first_day = date(2026, 4, 23)
        second_day = date(2026, 7, 16)

        def trade(day: date, at: time, side: str, price: float, realized_pnl: float = 0.0) -> TradeRecord:
            return TradeRecord(
                timestamp=datetime.combine(day, at, tzinfo=timezone),
                symbol="US.NVNI",
                side=side,
                quantity=1.0,
                price=price,
                gross_value=price,
                fee=0.0,
                cash_after=1_000.0,
                realized_pnl=realized_pnl,
                reason="test",
                rule="test",
                signal_day=day - timedelta(days=1),
            )

        trades = [
            trade(first_day, time(9, 53), "BUY", 1.41),
            trade(first_day, time(15, 55), "SELL", 1.38, -74.46),
            trade(second_day, time(9, 47), "BUY", 1.55),
            trade(second_day, time(15, 55), "SELL", 1.575, 56.45),
        ]
        daily_bars = {
            "NVNI": [
                DailyBar("NVNI", first_day, 1.35, 1.50, 1.30, 1.38),
                DailyBar("NVNI", second_day, 1.48, 1.62, 1.45, 1.58),
            ]
        }
        minute_bars = [
            minute_bar(first_day, time(9, 53), 1.40, 1.43, 1.39, 1.41, "NVNI"),
            minute_bar(first_day, time(15, 55), 1.39, 1.40, 1.37, 1.38, "NVNI"),
            minute_bar(second_day, time(9, 47), 1.54, 1.57, 1.53, 1.55, "NVNI"),
            minute_bar(second_day, time(15, 55), 1.57, 1.59, 1.56, 1.575, "NVNI"),
        ]
        with TemporaryDirectory() as tmp:
            result = SimpleNamespace(
                trades=trades,
                config=make_backtest_config(Path(tmp), second_day),
            )

            payload = build_symbol_detail_payload("US.NVNI", result, daily_bars, minute_bars)

        self.assertEqual(payload["symbol_realized_pnl"], -18.01)
        self.assertEqual([window["realized_pnl"] for window in payload["windows"]], [-74.46, 56.45])
        self.assertEqual(payload["minute_days"], ["2026-04-23", "2026-07-16"])
        first_buy = payload["windows"][0]["trades"][0]
        self.assertTrue(first_buy["minute_kline_location"]["exact"])
        self.assertEqual(first_buy["minute_kline_location"]["bar_time"], "2026-04-23T09:53-04:00")

        table = symbol_detail_table(trades)
        self.assertEqual(table.count("<tr data-symbol='NVNI'"), 2)
        self.assertIn("data-window-index='0'", table)
        self.assertIn("data-window-index='1'", table)
        self.assertIn("<th>买入日</th><th>卖出日</th>", table)
        self.assertIn("2026-04-23", table)
        self.assertIn("2026-07-16", table)
        self.assertIn("$-74.46", table)
        self.assertIn("$56.45", table)
        self.assertNotIn("$-18.01", table)

    def test_trade_kline_location_reports_exact_region_and_outside_prices(self):
        bar = DailyBar("TEST", date(2026, 7, 16), 10.0, 12.0, 9.0, 11.0)

        inside = trade_kline_location(10.5, bar)
        outside = trade_kline_location(12.5, bar)
        missing = trade_kline_location(10.5, None)

        self.assertEqual(inside["position"], "日 K 实体")
        self.assertEqual(inside["range_position_pct"], 50.0)
        self.assertEqual(outside["status"], "outside")
        self.assertEqual(outside["position"], "高于日 K 最高价")
        self.assertFalse(missing["matched"])

    def test_minute_payload_preserves_full_timestamp_and_exact_ohlc(self):
        day = date(2026, 7, 16)
        bars = [
            minute_bar(day, time(9, 30), 1.234567, 1.345678, 1.2, 1.3, "TEST"),
            minute_bar(day, time(9, 31), 1.3, 1.4, 1.25, 1.35, "TEST"),
        ]

        payload = build_symbol_minute_payload("US.TEST", [], bars)

        self.assertEqual(payload["symbol"], "TEST")
        self.assertEqual(payload["days"]["2026-07-16"][0], [
            "2026-07-16T09:30-04:00",
            1.234567,
            1.345678,
            1.2,
            1.3,
        ])

    def test_massive_key_and_grouped_daily_payload_parsing(self):
        self.assertEqual(split_api_keys("key1,key2; key1\nkey3"), ("key1", "key2", "key3"))
        payload = {
            "results": [
                {"T": "AAPL", "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.5},
                {"T": "BAD", "o": 1.0, "h": 2.0, "l": 0.5},
            ]
        }

        rows = massive_grouped_daily_rows_from_payload(payload)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ticker, "AAPL")
        self.assertEqual(rows[0].close, 10.5)

    def test_massive_failure_dates_coalesce_for_yahoo_fallback(self):
        failures = [
            {"source_symbol": "2026-07-02", "error": "HTTPError: HTTP Error 429"},
            {"source_symbol": "2026-07-03", "error": "HTTPError: HTTP Error 429"},
            {"source_symbol": "2026-07-06", "error": "HTTPError: HTTP Error 429"},
            {"source_symbol": "not-a-date", "error": "other"},
        ]

        dates = failure_dates(failures)
        ranges = coalesced_date_ranges(dates)

        self.assertEqual(dates, [date(2026, 7, 2), date(2026, 7, 3), date(2026, 7, 6)])
        self.assertEqual(ranges, [(date(2026, 7, 2), date(2026, 7, 7))])

    def test_daily_request_end_date_exclusive_handles_midnight_and_after_close_end(self):
        market_tz = ZoneInfo("America/New_York")

        self.assertEqual(
            daily_request_end_date_exclusive(datetime(2026, 6, 3, 0, 0, tzinfo=market_tz)),
            date(2026, 6, 3),
        )
        self.assertEqual(
            daily_request_end_date_exclusive(datetime(2026, 6, 3, 16, 20, tzinfo=market_tz)),
            date(2026, 6, 4),
        )

    def test_market_data_cache_round_trips_daily_and_minute_bars(self):
        with TemporaryDirectory() as tmp:
            cache = MarketDataCache(Path(tmp) / "market_data.sqlite")
            daily = {
                "TEST": [
                    DailyBar("TEST", date(2026, 1, 1), 10.0, 11.0, 9.5, 10.5, 12345.0, 10.25, 88, 1767225600000),
                    DailyBar("TEST", date(2026, 1, 2), 10.5, 12.0, 10.0, 11.5),
                ]
            }
            minutes = {
                "TEST": [
                    minute_bar(date(2026, 1, 2), time(9, 30), 10.5, 10.8, 10.4, 10.7),
                    minute_bar(date(2026, 1, 2), time(9, 31), 10.7, 10.9, 10.6, 10.8),
                ]
            }

            cache.save_daily_bars(daily, feed="iex", range_start=date(2026, 1, 1), range_end_exclusive=date(2026, 1, 3))
            cache.save_minute_bars(
                minutes,
                feed="iex",
                range_start=datetime(2026, 1, 2, 0, 0, tzinfo=ZoneInfo("America/New_York")),
                range_end=datetime(2026, 1, 3, 0, 0, tzinfo=ZoneInfo("America/New_York")),
            )

            loaded_daily = cache.load_daily_bars(["US.TEST"], date(2026, 1, 1), date(2026, 1, 3), feed="iex")
            loaded_minutes = cache.load_minute_bars(
                ["US.TEST"],
                datetime(2026, 1, 2, 0, 0, tzinfo=ZoneInfo("America/New_York")),
                datetime(2026, 1, 3, 0, 0, tzinfo=ZoneInfo("America/New_York")),
                feed="iex",
            )

            self.assertEqual([bar.close for bar in loaded_daily["TEST"]], [10.5, 11.5])
            self.assertEqual(loaded_daily["TEST"][0].volume, 12345.0)
            self.assertEqual(loaded_daily["TEST"][0].vwap, 10.25)
            self.assertEqual(loaded_daily["TEST"][0].transactions, 88)
            self.assertEqual(loaded_daily["TEST"][0].timestamp_ms, 1767225600000)
            self.assertEqual([bar.close for bar in loaded_minutes["TEST"]], [10.7, 10.8])
            self.assertEqual(
                cache.uncovered_symbols("daily", ["TEST"], "2026-01-01", "2026-01-03", feed="iex"),
                [],
            )

    def test_market_data_cache_treats_adjacent_ranges_as_covered(self):
        with TemporaryDirectory() as tmp:
            cache = MarketDataCache(Path(tmp) / "market_data.sqlite")
            cache.save_daily_bars(
                {"TEST": [DailyBar("TEST", date(2026, 1, 1), 10.0, 10.0, 10.0, 10.0)]},
                feed="iex",
                range_start=date(2026, 1, 1),
                range_end_exclusive=date(2026, 1, 2),
            )
            cache.save_daily_bars(
                {"TEST": [DailyBar("TEST", date(2026, 1, 2), 11.0, 11.0, 11.0, 11.0)]},
                feed="iex",
                range_start=date(2026, 1, 2),
                range_end_exclusive=date(2026, 1, 3),
            )

            self.assertEqual(cache.uncovered_symbols("daily", ["TEST"], "2026-01-01", "2026-01-03", feed="iex"), [])
            self.assertEqual(cache.uncovered_symbols("daily", ["TEST"], "2026-01-01", "2026-01-04", feed="iex"), ["TEST"])

    def test_market_data_cache_read_only_mode_never_writes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_data.sqlite"
            writable = MarketDataCache(path)
            writable.save_daily_bars(
                {"TEST": [DailyBar("TEST", date(2026, 1, 2), 10.0, 11.0, 9.0, 10.5)]},
                feed="iex",
                range_start=date(2026, 1, 2),
                range_end_exclusive=date(2026, 1, 3),
            )
            read_only = MarketDataCache(path, read_only=True)

            loaded = read_only.load_daily_bars(
                ["TEST"],
                date(2026, 1, 2),
                date(2026, 1, 3),
                feed="iex",
            )

            self.assertEqual(loaded["TEST"][0].close, 10.5)
            with self.assertRaises(PermissionError):
                read_only.save_daily_bars(
                    {"TEST": [DailyBar("TEST", date(2026, 1, 2), 10.0, 11.0, 9.0, 10.5)]},
                    feed="iex",
                    range_start=date(2026, 1, 2),
                    range_end_exclusive=date(2026, 1, 3),
                )

    def test_data_repair_backfills_warmup_and_recomputes_target_mas(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = MarketDataCache(root / "market_data.sqlite")
            target_day = date(2026, 2, 2)
            cache.save_daily_bars(
                {"TEST": [DailyBar("TEST", target_day, 21.0, 21.0, 21.0, 21.0)]},
                feed="massive",
                adjustment="adj",
                range_start=target_day,
                range_end_exclusive=target_day + timedelta(days=1),
            )
            config = DataRepairConfig(
                symbols=["US.TEST"],
                start_date=target_day,
                end_date=target_day,
                ma_warmup_calendar_days=45,
                feed="massive",
                adjustment="adj",
                data_cache_dir=root,
                data_cache_name="market_data.sqlite",
                output_dir=root / "output",
                create_backup=False,
                delete_invalid_ohlc_rows=True,
                delete_untrusted_fetch_ranges=True,
                recompute_daily_mas=True,
                backfill_low_coverage_dates=True,
                min_range_date_coverage_ratio=0.98,
                min_daily_symbol_coverage_ratio=0.65,
                max_backfill_dates=None,
                massive_api_keys=("unit-test",),
                massive_max_workers=1,
                massive_request_timeout_seconds=1.0,
                massive_retry_sleep_seconds=0.0,
                massive_max_retries=0,
                massive_progress_interval_seconds=0.0,
                massive_progress_interval_dates=0,
            )

            def fake_fetch(symbols, range_start, range_end_exclusive, massive_config):
                bars: list[DailyBar] = []
                day = range_start
                close = 1.0
                while day < range_end_exclusive:
                    if day.weekday() < 5:
                        bars.append(DailyBar("TEST", day, close, close, close, close))
                    close += 1.0
                    day += timedelta(days=1)
                return DailyFetchResult({"TEST": bars}, [])

            with patch("backtest.data_repair.fetch_massive_grouped_daily_bars_with_failures", side_effect=fake_fetch):
                result = run_data_repair(config)

            self.assertEqual(result.target_null_ma_rows, 0)
            with cache._connect() as conn:
                ma5, ma10, ma20 = conn.execute(
                    """
                    SELECT ma5, ma10, ma20
                    FROM daily_bars
                    WHERE symbol = 'TEST' AND bar_date = ? AND feed = 'massive' AND adjustment = 'adj'
                    """,
                    (target_day.isoformat(),),
                ).fetchone()
            self.assertIsNotNone(ma5)
            self.assertIsNotNone(ma10)
            self.assertIsNotNone(ma20)

    def test_data_repair_does_not_backfill_market_holidays(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            holiday = date(2026, 7, 3)
            config = DataRepairConfig(
                symbols=["US.TEST"],
                start_date=holiday,
                end_date=holiday,
                ma_warmup_calendar_days=0,
                feed="massive",
                adjustment="adj",
                data_cache_dir=root,
                data_cache_name="market_data.sqlite",
                output_dir=root / "output",
                create_backup=False,
                delete_invalid_ohlc_rows=True,
                delete_untrusted_fetch_ranges=True,
                recompute_daily_mas=True,
                backfill_low_coverage_dates=True,
                min_range_date_coverage_ratio=0.98,
                min_daily_symbol_coverage_ratio=0.65,
                max_backfill_dates=None,
                massive_api_keys=("unit-test",),
                massive_max_workers=1,
                massive_request_timeout_seconds=1.0,
                massive_retry_sleep_seconds=0.0,
                massive_max_retries=0,
                massive_progress_interval_seconds=0.0,
                massive_progress_interval_dates=0,
            )

            with patch("backtest.data_repair.fetch_massive_grouped_daily_bars_with_failures") as fetch:
                result = run_data_repair(config)

            fetch.assert_not_called()
            self.assertEqual(result.low_coverage_dates, 0)
            self.assertEqual(result.backfill_rows_written, 0)

    def test_data_spotcheck_reports_null_ma_and_remote_mismatch(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = MarketDataCache(root / "market_data.sqlite")
            days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
            local_bars = [
                DailyBar("TEST", days[0], 10.0, 10.5, 9.5, 10.0, 1000.0, 10.1, 10, 1767571200000),
                DailyBar("TEST", days[1], 11.0, 11.5, 10.5, 11.0, 1100.0, 11.1, 11, 1767657600000),
                DailyBar("TEST", days[2], 12.0, 12.5, 11.5, 12.0, 1200.0, 12.1, 12, 1767744000000),
            ]
            cache.save_daily_bars(
                {"TEST": local_bars},
                feed="massive",
                adjustment="adj",
                range_start=days[0],
                range_end_exclusive=days[-1] + timedelta(days=1),
            )
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE daily_bars SET ma5 = NULL WHERE symbol = 'TEST' AND bar_date = ?",
                    (days[1].isoformat(),),
                )

            config = DataSpotcheckConfig(
                symbols=["US.TEST"],
                start_date=days[0],
                end_date=days[-1],
                sample_size=100,
                sample_seed=1,
                feed="massive",
                adjustment="adj",
                data_cache_dir=root,
                data_cache_name="market_data.sqlite",
                output_dir=root / "output",
                issue_csv_name="issues.csv",
                summary_csv_name="summary.csv",
                sampled_symbols_csv_name="sampled.csv",
                massive_api_keys=("unit-test",),
                massive_max_workers=1,
                massive_request_timeout_seconds=1.0,
                massive_retry_sleep_seconds=0.0,
                massive_max_retries=0,
                massive_request_spacing_seconds=0.0,
                massive_progress_interval_seconds=0.0,
                massive_progress_interval_dates=0,
            )
            remote_bars = [
                local_bars[0],
                DailyBar("TEST", days[1], 11.0, 11.5, 10.5, 22.0, 1100.0, 11.1, 11, 1767657600000),
                local_bars[2],
            ]

            with patch(
                "backtest.data_spotcheck.fetch_spotcheck_massive_daily_bars",
                return_value=DailyFetchResult({"TEST": remote_bars}, []),
            ):
                result = run_data_spotcheck(config)

            issue_text = result.issue_csv_path.read_text(encoding="utf-8-sig")
            self.assertIn("NULL_VALUE", issue_text)
            self.assertIn("MA_MISMATCH", issue_text)
            self.assertIn("REMOTE_MISMATCH", issue_text)
            self.assertGreaterEqual(result.issue_count, 3)

    def test_backtest_reuses_strategy_and_writes_html_report(self):
        trade_day, bars = make_signal_and_trade_bars()
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual(result.minute_bar_count, len(bars["TEST"]))
            self.assertEqual(result.watchlist_by_day[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 1)
            self.assertGreaterEqual([trade.side for trade in result.trades].count("SELL"), 1)
            self.assertGreater(result.stats.final_equity, result.stats.initial_cash)
            self.assertTrue(result.report_path.exists())
            content = result.report_path.read_text(encoding="utf-8")
            self.assertIn("资金曲线", content)
            self.assertIn("股票 K 线详情", content)
            self.assertIn("data-detail-url='symbol_details/TEST.html'", content)
            self.assertIn("data-minute-url='symbol_details/TEST.minute.js'", content)
            self.assertIn("data-symbol='TEST'", content)
            self.assertIn("detail-modal", content)
            self.assertIn("detail-modal-back", content)
            self.assertIn("detail-copy-link", content)
            self.assertIn("round-prev", content)
            self.assertIn("round-next", content)
            self.assertIn("symbol-search", content)
            self.assertIn("data-sort-symbol-time", content)
            self.assertIn("最新优先", content)
            self.assertIn('sortSymbolRows("desc")', content)
            self.assertIn("data-latest-time=", content)
            self.assertIn("data-symbol-filter='profit'", content)
            self.assertIn("data-symbol-filter='loss'", content)
            self.assertIn("data-symbol-filter='multi'", content)
            self.assertIn("EVENT RAIL", content)
            self.assertIn("TIMEFRAME / DAY DRILLDOWN", content)
            self.assertIn("本轮已实现收益", content)
            self.assertIn("股票累计收益", content)
            self.assertIn("plotly_click", content)
            self.assertIn("不吸附到相邻 K 线", content)
            self.assertIn('"details":{"TEST"', content)
            self.assertIn("MA5 / MA10 / MA20", content)
            self.assertIn('data-report-version="2"', content)
            self.assertIn("report-nav", content)
            self.assertIn("Interactive v2", content)
            self.assertIn("updateDeepLink", content)
            self.assertIn("data-realized-pnl=", content)
            self.assertIn("data-rounds=", content)
            self.assertIn("收益统计表", content)
            self.assertIn("时间顺序与收益核验", content)
            self.assertIn("现金流水核对", content)
            self.assertIn("每笔交易明细", content)
            self.assertIn("trades-table", content)
            self.assertIn("data-sort-realized-pnl", content)
            self.assertIn("data-realized-pnl", content)
            self.assertIn("当前回测配置摘要", content)
            self.assertIn("1Min", content)
            detail = result.report_path.parent / "symbol_details" / "TEST.html"
            self.assertTrue(detail.exists())
            detail_content = detail.read_text(encoding="utf-8")
            self.assertIn("candlestick", detail_content)
            self.assertIn('name: "MA5"', detail_content)
            self.assertIn('name: "MA10"', detail_content)
            self.assertIn('name: "MA20"', detail_content)
            self.assertIn('increasing: {line: {color: "#c0392b"}', detail_content)
            self.assertIn('decreasing: {line: {color: "#137b4b"}', detail_content)
            self.assertIn("日K", detail_content)
            self.assertIn("Buy", detail_content)
            self.assertIn("Sell", detail_content)
            minute_detail = result.report_path.parent / "symbol_details" / "TEST.minute.js"
            self.assertTrue(minute_detail.exists())
            minute_content = minute_detail.read_text(encoding="utf-8")
            self.assertIn('window.__BACKTEST_MINUTE_DETAILS__["TEST"]=', minute_content)
            self.assertIn(f'"{trade_day:%Y-%m-%d}"', minute_content)
            payload_start = detail_content.index("const windows = ") + len("const windows = ")
            payload_end = detail_content.index(";\n\n    function pctText", payload_start)
            windows = json.loads(detail_content[payload_start:payload_end])
            self.assertGreaterEqual(len(windows[0]["bars"]), 6)
            self.assertIn(f"{trade_day:%Y-%m-%d}", [bar["timestamp"] for bar in windows[0]["bars"]])
            self.assertTrue(any(bar["ma5"] is not None for bar in windows[0]["bars"]))
            self.assertTrue(any(bar["ma10"] is not None for bar in windows[0]["bars"]))
            self.assertTrue(any(bar["ma20"] is not None for bar in windows[0]["bars"]))
            self.assertIn("volume", windows[0]["bars"][0])
            self.assertEqual(windows[0]["buy_day"], f"{trade_day:%Y-%m-%d}")
            self.assertTrue(windows[0]["signal_day"])
            buy_trade = next(trade for trade in windows[0]["trades"] if trade["side"] == "BUY")
            sell_trade = next(trade for trade in windows[0]["trades"] if trade["side"] == "SELL")
            self.assertEqual(buy_trade["timestamp"], f"{trade_day:%Y-%m-%d}")
            self.assertEqual(sell_trade["timestamp"], f"{trade_day:%Y-%m-%d}")
            self.assertGreater(buy_trade["price"], 0)
            self.assertGreater(sell_trade["price"], 0)
            self.assertIn("kline_location", buy_trade)
            self.assertIn("minute_kline_location", buy_trade)
            self.assertTrue(buy_trade["minute_kline_location"]["exact"])

    def test_backtest_forces_day_end_sell_without_close_liquidation_bar(self):
        trade_day, bars = make_signal_and_trade_bars()
        bars["TEST"] = [
            bar
            for bar in bars["TEST"]
            if not (bar.timestamp.date() == trade_day and bar.timestamp.time() == time(15, 55))
        ]
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual(result.stats.open_position_count, 0)
            self.assertTrue(any(trade.rule == "end_of_day_forced_liquidation" for trade in result.trades))

    def test_backtest_prefilters_historical_candidates_before_minute_fetch(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            with patch("backtest.engine.fetch_backtest_daily_bars", return_value=daily_bars) as daily_fetch, patch(
                "backtest.engine.fetch_candidate_day_minute_bars", return_value=bars
            ) as minute_fetch:
                result = run_backtest(config)

            daily_fetch.assert_called_once()
            minute_fetch.assert_called_once()
            watchlists = minute_fetch.call_args.args[1]
            self.assertEqual(watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 1)

    def test_backtest_does_not_fill_new_buy_signal_on_same_minute(self):
        trade_day, bars = make_signal_and_trade_bars()
        adjusted = []
        for bar in bars["TEST"]:
            if bar.timestamp.date() == trade_day and bar.timestamp.time() == time(10, 1):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 12.4, 12.45, 12.35, 12.4))
            elif bar.timestamp.date() == trade_day and bar.timestamp.time() == time(12, 0):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 12.4, 12.45, 12.35, 12.4))
            else:
                adjusted.append(bar)
        bars["TEST"] = adjusted
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 0)

    def test_candidate_minute_fetch_only_loads_signal_day(self):
        trade_day, _ = make_signal_and_trade_bars()
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            with patch("backtest.engine.fetch_minute_bars_for_range", return_value={}) as fetch:
                fetch_candidate_day_minute_bars(config, {f"{trade_day:%Y-%m-%d}": ["US.TEST"]})

            _, _, start, end = fetch.call_args.args
            self.assertEqual(start.date(), trade_day)
            self.assertEqual(end.date(), trade_day + timedelta(days=1))

    def test_backtest_counts_pending_buy_orders_against_daily_limit(self):
        trade_day, bars = make_signal_and_trade_bars()
        adjusted = []
        for bar in bars["TEST"]:
            if bar.timestamp.date() == trade_day and bar.timestamp.time() == time(10, 0):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 10.9, 10.95, 10.85, 10.9))
            elif bar.timestamp.date() == trade_day and bar.timestamp.time() == time(12, 0):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 10.7, 10.8, 10.6, 10.7))
            else:
                adjusted.append(bar)
        multi_symbol_bars = {
            "AAA": clone_bars("AAA", adjusted),
            "BBB": clone_bars("BBB", adjusted),
            "CCC": clone_bars("CCC", adjusted),
        }
        with TemporaryDirectory() as tmp:
            config = replace(make_backtest_config(Path(tmp), trade_day), symbols=["US.AAA", "US.BBB", "US.CCC"])

            result = run_backtest(config, bars_by_symbol=multi_symbol_bars)

            buy_dates = [
                trade.timestamp.date()
                for trade in result.trades
                if trade.side == "BUY"
            ]
            self.assertLessEqual(buy_dates.count(trade_day), config.max_daily_buys)

    def test_backtest_keeps_ma5_touch_daily_buy_exclusion(self):
        trade_day, bars = make_signal_and_trade_bars()
        adjusted = []
        for bar in bars["TEST"]:
            if bar.timestamp.date() < trade_day and date(2026, 1, 21) <= bar.timestamp.date() < date(2026, 1, 27):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 12.8, 12.8, 12.8, 12.8))
            elif bar.timestamp.date() == trade_day and bar.timestamp.time() == time(9, 30):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 11.9, 12.0, 11.8, 11.9))
            elif bar.timestamp.date() == trade_day and bar.timestamp.time() == time(10, 0):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 12.1, 12.15, 12.05, 12.1))
            elif bar.timestamp.date() == trade_day and bar.timestamp.time() == time(12, 0):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 10.4, 10.45, 10.35, 10.4))
            else:
                adjusted.append(bar)
        bars["TEST"] = adjusted
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 0)

    def test_backtest_rejects_buy_day_gap_down_open(self):
        trade_day, bars = make_signal_and_trade_bars()
        adjusted = []
        for bar in bars["TEST"]:
            if bar.timestamp.date() == trade_day and bar.timestamp.time() == time(9, 30):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 12.0, 12.1, 11.9, 12.0))
            else:
                adjusted.append(bar)
        bars["TEST"] = adjusted
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 0)

    def test_backtest_allows_red_signal_when_next_day_holds_and_pulls_back(self):
        trade_day, bars = make_signal_and_trade_bars()
        adjusted = []
        for bar in bars["TEST"]:
            if bar.timestamp.date() == trade_day and bar.timestamp.time() == time(9, 30):
                adjusted.append(MinuteBar("TEST", bar.timestamp, 13.0, 13.1, 12.9, 13.0))
            elif bar.timestamp.date() < trade_day and bar.timestamp.time() == time(9, 30) and bar.open == 12.0:
                adjusted.append(MinuteBar("TEST", bar.timestamp, 13.1, 13.2, 13.0, 13.1))
            elif bar.timestamp.date() < trade_day and bar.timestamp.time() == time(15, 59) and bar.close == 13.0:
                adjusted.append(MinuteBar("TEST", bar.timestamp, 12.8, 13.2, 12.7, 13.0))
            else:
                adjusted.append(bar)
        bars["TEST"] = adjusted
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 1)

    def test_backtest_optimization_rules_filter_signal_body(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)
            filtered = replace(config, optimization_rules={"min_signal_body_pct": 0.20})

            baseline_watchlists = build_historical_watchlists(daily_bars, config)
            filtered_watchlists = build_historical_watchlists(daily_bars, filtered)

            self.assertEqual(baseline_watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual(filtered_watchlists[f"{trade_day:%Y-%m-%d}"], [])

    def test_backtest_optimization_rules_filter_signal_close_position(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)
            filtered = replace(config, optimization_rules={"min_signal_close_position_pct": 0.99})

            baseline_watchlists = build_historical_watchlists(daily_bars, config)
            filtered_watchlists = build_historical_watchlists(daily_bars, filtered)

            self.assertEqual(baseline_watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual(filtered_watchlists[f"{trade_day:%Y-%m-%d}"], [])

    def test_backtest_optimization_rules_filter_max_signal_dollar_volume(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        daily_bars["TEST"] = [
            DailyBar(
                bar.symbol,
                bar.date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                100.0,
                bar.vwap,
                bar.transactions,
                bar.timestamp_ms,
            )
            for bar in daily_bars["TEST"]
        ]
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)
            filtered = replace(
                config,
                optimization_rules={"max_signal_dollar_volume": 1.0},
            )

            baseline_watchlists = build_historical_watchlists(daily_bars, config)
            filtered_watchlists = build_historical_watchlists(daily_bars, filtered)

            self.assertEqual(baseline_watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual(filtered_watchlists[f"{trade_day:%Y-%m-%d}"], [])

    def test_sorted_watch_candidates_applies_ranking_and_pool_limit(self):
        signal_day = date(2025, 1, 2)
        candidates = [
            watchlist_module.WatchCandidate(
                "US.AAA",
                signal_day,
                0.20,
                0.01,
                10.0,
                9.0,
                8.0,
                11.0,
                14.0,
                12.0,
                10.0,
            ),
            watchlist_module.WatchCandidate(
                "US.BBB",
                signal_day,
                0.15,
                0.01,
                10.0,
                9.0,
                8.0,
                11.0,
                13.0,
                12.5,
                10.0,
            ),
        ]

        ranked = sorted_watch_candidates(
            candidates,
            {
                "candidate_sort": "close_position_desc_gain_desc",
                "max_watchlist_candidates": 1,
            },
        )

        self.assertEqual([candidate.symbol for candidate in ranked], ["US.BBB"])

    def test_backtest_optimization_rules_filter_bollinger_z20(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)
            filtered = replace(config, optimization_rules={"min_signal_bollinger_z20": 10.0})

            baseline_watchlists = build_historical_watchlists(daily_bars, config)
            filtered_watchlists = build_historical_watchlists(daily_bars, filtered)

            self.assertEqual(baseline_watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual(filtered_watchlists[f"{trade_day:%Y-%m-%d}"], [])

    def test_backtest_optimization_rules_filter_atr20(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)
            filtered = replace(config, optimization_rules={"min_signal_atr20_pct": 2.0})

            baseline_watchlists = build_historical_watchlists(daily_bars, config)
            filtered_watchlists = build_historical_watchlists(daily_bars, filtered)

            self.assertEqual(baseline_watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual(filtered_watchlists[f"{trade_day:%Y-%m-%d}"], [])

    def test_backtest_optimization_rules_filter_prior_trend_days(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)
            filtered = replace(config, optimization_rules={"min_prior_up_days": 3})

            baseline_watchlists = build_historical_watchlists(daily_bars, config)
            filtered_watchlists = build_historical_watchlists(daily_bars, filtered)

            self.assertEqual(baseline_watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual(filtered_watchlists[f"{trade_day:%Y-%m-%d}"], [])

    def test_backtest_optimization_rules_filter_volume_avg5_to_avg20(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        daily_bars["TEST"] = [
            DailyBar(
                bar.symbol,
                bar.date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                100.0,
                bar.vwap,
                bar.transactions,
                bar.timestamp_ms,
            )
            for bar in daily_bars["TEST"]
        ]
        with TemporaryDirectory() as tmp:
            config = make_backtest_config(Path(tmp), trade_day)
            filtered = replace(config, optimization_rules={"min_volume_avg5_to_avg20": 2.0})

            baseline_watchlists = build_historical_watchlists(daily_bars, config)
            filtered_watchlists = build_historical_watchlists(daily_bars, filtered)

            self.assertEqual(baseline_watchlists[f"{trade_day:%Y-%m-%d}"], ["US.TEST"])
            self.assertEqual(filtered_watchlists[f"{trade_day:%Y-%m-%d}"], [])

    def test_backtest_optimization_rules_filter_buy_day_open_gap(self):
        trade_day, bars = make_signal_and_trade_bars()
        with TemporaryDirectory() as tmp:
            config = replace(
                make_backtest_config(Path(tmp), trade_day),
                optimization_rules={"min_buy_day_open_gain_pct": 0.01},
            )

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 0)

    def test_backtest_optimization_rules_filter_buy_time_start(self):
        trade_day, bars = make_signal_and_trade_bars()
        with TemporaryDirectory() as tmp:
            config = replace(
                make_backtest_config(Path(tmp), trade_day),
                optimization_rules={"buy_time_start": "12:30"},
            )

            result = run_backtest(config, bars_by_symbol=bars)

            self.assertEqual([trade.side for trade in result.trades].count("BUY"), 0)

    def test_final_strategy_backtest_config_keeps_single_fixed_strategy(self):
        from alpaca_ma5_service.final_strategy import (
            BUY_NOTIONAL_USD,
            MAX_DAILY_BUYS,
            MAX_POSITIONS,
            OPTIMIZATION_RULES,
            SLIPPAGE_PCT,
            STOP_PARAMS,
            STRATEGY_NAME,
            TARGET_RETURN_PCT,
            TARGET_YEARS,
        )
        from backtest.paths import OFFICIAL_DAILY_DB_PATH
        from run_backtest import backtest_date_range_for_year, build_final_strategy_config, strategy_rule_summary

        with TemporaryDirectory() as tmp:
            config = build_final_strategy_config(Path(tmp), "report.html", year=2024)

        self.assertEqual(TARGET_RETURN_PCT, 0.20)
        self.assertEqual(TARGET_YEARS, [2024, 2025, 2026])
        self.assertEqual(config.strategy_variant_name, f"{STRATEGY_NAME}_2024")
        self.assertEqual(config.start_date, date(2024, 1, 1))
        self.assertEqual(config.end_date, date(2024, 12, 31))
        self.assertEqual(
            backtest_date_range_for_year(2026, date(2026, 7, 2), today=date(2026, 7, 5)),
            (date(2026, 1, 1), date(2026, 7, 2)),
        )
        self.assertEqual(config.buy_notional_usd, BUY_NOTIONAL_USD)
        self.assertEqual(config.strategy_settings.buy_notional_usd, BUY_NOTIONAL_USD)
        self.assertEqual(config.buy_position_pct, 0.0)
        self.assertEqual(config.max_daily_buys, MAX_DAILY_BUYS)
        self.assertEqual(config.max_positions, MAX_POSITIONS)
        self.assertEqual(config.slippage_pct, SLIPPAGE_PCT)
        self.assertEqual(
            config.data_cache_dir / config.data_cache_name,
            OFFICIAL_DAILY_DB_PATH,
        )
        self.assertEqual(config.daily_data_source, "alpaca")
        self.assertTrue(config.use_data_cache)
        self.assertTrue(config.cache_daily_bars)
        self.assertFalse(config.cache_minute_bars)
        self.assertFalse(config.refresh_data_cache)
        self.assertTrue(config.require_daily_cache_coverage)
        self.assertEqual(config.optimization_rules, OPTIMIZATION_RULES)
        self.assertEqual(config.stop_params["take_profit_sell_fraction"], STOP_PARAMS["take_profit_sell_fraction"])
        self.assertNotIn("blocked_signal_months", config.optimization_rules)
        self.assertNotIn("blocked_buy_hour_candidate_count_ranges", config.optimization_rules)
        self.assertEqual(strategy_rule_summary()["config"]["buy_notional_usd"], BUY_NOTIONAL_USD)

    def test_required_daily_database_coverage_does_not_fall_back_to_network(self):
        trade_day = date(2025, 1, 6)
        with TemporaryDirectory() as tmp:
            config = replace(
                make_backtest_config(Path(tmp), trade_day),
                use_data_cache=True,
                require_daily_cache_coverage=True,
            )
            with patch(
                "backtest.engine.fetch_backtest_daily_bars_from_source"
            ) as remote_fetch:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Official daily database coverage gap",
                ):
                    fetch_backtest_daily_bars(config, ["TEST"])

            remote_fetch.assert_not_called()

    def test_daily_backtest_uses_daily_bars_without_minute_fetch(self):
        trade_day, bars = make_signal_and_trade_bars()
        daily_bars = build_daily_bars(bars)
        with TemporaryDirectory() as tmp:
            config = replace(make_backtest_config(Path(tmp), trade_day), timeframe="1Day")

            with patch("backtest.engine.fetch_backtest_daily_bars", return_value=daily_bars) as daily_fetch, patch(
                "backtest.engine.fetch_candidate_day_minute_bars"
            ) as candidate_minute_fetch, patch("backtest.engine.fetch_backtest_bars") as full_minute_fetch:
                result = run_backtest(config)

            daily_fetch.assert_called_once()
            candidate_minute_fetch.assert_not_called()
            full_minute_fetch.assert_not_called()
            self.assertGreater(result.minute_bar_count, 0)

    def test_backtest_rejects_unknown_timeframe(self):
        trade_day, bars = make_signal_and_trade_bars()
        with TemporaryDirectory() as tmp:
            config = replace(make_backtest_config(Path(tmp), trade_day), timeframe="5Min")

            with self.assertRaisesRegex(ValueError, "1Min"):
                run_backtest(config, bars_by_symbol=bars)


if __name__ == "__main__":
    unittest.main()
