from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alpaca_ma5_service.models import MarketSnapshot, Position
from alpaca_ma5_service.premarket_positions import (
    AlpacaPositionSource,
    PREMARKET_POSITION_MOVE_PCT,
    PremarketPositionTracker,
    run_premarket_positions_once,
)
from alpaca_ma5_service.workflows.watchcode.premarket import generate_premarket_watchcode


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 27, 8, 0, tzinfo=ET)


def make_settings(root: Path):
    return SimpleNamespace(
        output_dir=root,
        market_timezone="America/New_York",
        trade_notify_mode="cloud",
    )


def snapshot(price: float, as_of: datetime, symbol: str = "US.TEST") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        current_price=price,
        previous_closes=[9.0, 9.2, 9.5, 9.8],
        as_of=as_of,
        current_price_source="alpaca_latest_quote:midpoint:iex",
        current_price_as_of=as_of,
    )


class FakePositionSource:
    def __init__(self, positions):
        self.positions = positions
        self.calls = 0

    def get_positions(self):
        self.calls += 1
        return dict(self.positions)


class SequentialMarketData:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = []

    def get_snapshot(self, symbol, *, purpose):
        self.calls.append((symbol, purpose))
        return self.snapshots.pop(0)


class PremarketPositionMonitorTests(unittest.TestCase):
    def test_alpaca_position_source_keeps_long_and_short_holdings(self):
        client = SimpleNamespace(
            get_all_positions=lambda: [
                SimpleNamespace(symbol="LONG", qty="12", avg_entry_price="10.5"),
                SimpleNamespace(symbol="SHORT", qty="-4", avg_entry_price="20"),
                SimpleNamespace(symbol="ZERO", qty="0", avg_entry_price="1"),
            ]
        )
        connection = SimpleNamespace(client=client, paper=False)
        with patch("alpaca_ma5_service.premarket_positions.build_trading_connection", return_value=connection):
            positions = AlpacaPositionSource().get_positions()

        self.assertEqual(set(positions), {"US.LONG", "US.SHORT"})
        self.assertEqual(positions["US.LONG"].quantity, 12)
        self.assertEqual(positions["US.SHORT"].quantity, -4)

    def test_no_position_reads_no_symbol_and_sends_nothing(self):
        with TemporaryDirectory() as tmp:
            source = FakePositionSource({})

            class MarketData:
                def get_snapshot(self, *_args, **_kwargs):
                    raise AssertionError("must not read any symbol without a position")

            with patch("alpaca_ma5_service.premarket_positions.safe_send_openclaw_messages") as notify:
                summary = run_premarket_positions_once(
                    make_settings(Path(tmp)),
                    position_source=source,
                    market_data=MarketData(),
                    tracker=PremarketPositionTracker(),
                    now=NOW,
                )

        self.assertEqual(summary, {"positions": 0, "alerts": 0, "sent": 0, "hold": 0, "errors": 0})
        notify.assert_not_called()

    def test_only_current_position_symbol_is_queried(self):
        with TemporaryDirectory() as tmp:
            position = Position("US.OWN", 12, 10, "alpaca")
            market = SequentialMarketData([snapshot(10.0, NOW, "US.OWN")])
            summary = run_premarket_positions_once(
                make_settings(Path(tmp)),
                position_source=FakePositionSource({"US.OWN": position}),
                market_data=market,
                tracker=PremarketPositionTracker(),
                now=NOW,
                notify=False,
            )

        self.assertEqual(summary["positions"], 1)
        self.assertEqual([call[0] for call in market.calls], ["US.OWN"])

    def test_up_three_percent_within_one_minute_sends_one_clear_alert(self):
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            position = Position("US.TEST", 10, 9.5, "alpaca")
            source = FakePositionSource({"US.TEST": position})
            tracker = PremarketPositionTracker()
            market = SequentialMarketData([
                snapshot(10.0, NOW),
                snapshot(10.31, NOW + timedelta(seconds=30)),
            ])
            with patch("alpaca_ma5_service.premarket_positions.safe_send_openclaw_messages") as notify:
                first = run_premarket_positions_once(settings, position_source=source, market_data=market, tracker=tracker, now=NOW)
                second = run_premarket_positions_once(
                    settings,
                    position_source=source,
                    market_data=market,
                    tracker=tracker,
                    now=NOW + timedelta(seconds=30),
                )

        self.assertEqual(first["alerts"], 0)
        self.assertEqual((second["alerts"], second["sent"]), (1, 1))
        message = notify.call_args.args[1][0]
        self.assertIn("【盘前持仓｜快速上涨】US.TEST", message)
        self.assertIn("+3.10%", message)
        self.assertIn("不提交任何 Alpaca 订单", message)

    def test_down_three_percent_and_duplicate_quote_is_not_realerted(self):
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            position = Position("US.TEST", 10, 10.5, "alpaca")
            source = FakePositionSource({"US.TEST": position})
            tracker = PremarketPositionTracker()
            down_time = NOW + timedelta(seconds=45)
            market = SequentialMarketData([
                snapshot(10.0, NOW),
                snapshot(9.69, down_time),
                snapshot(9.69, down_time),
            ])
            with patch("alpaca_ma5_service.premarket_positions.safe_send_openclaw_messages") as notify:
                run_premarket_positions_once(settings, position_source=source, market_data=market, tracker=tracker, now=NOW)
                alert = run_premarket_positions_once(settings, position_source=source, market_data=market, tracker=tracker, now=down_time)
                duplicate = run_premarket_positions_once(
                    settings,
                    position_source=source,
                    market_data=market,
                    tracker=tracker,
                    now=down_time + timedelta(seconds=10),
                )

        self.assertEqual(alert["alerts"], 1)
        self.assertEqual(duplicate["alerts"], 0)
        self.assertEqual(notify.call_count, 1)
        self.assertIn("快速下跌", notify.call_args.args[1][0])

    def test_continuous_rise_realerts_every_new_three_percent_leg_without_cooldown(self):
        tracker = PremarketPositionTracker()
        position = Position("US.TEST", 10, 9.5, "alpaca")
        first_time = NOW + timedelta(seconds=20)
        second_time = NOW + timedelta(seconds=40)

        self.assertIsNone(tracker.observe(position, price=10.0, as_of=NOW, price_source="alpaca_latest_quote:iex"))
        first = tracker.observe(position, price=10.31, as_of=first_time, price_source="alpaca_latest_quote:iex")
        self.assertIsNotNone(first)
        self.assertEqual((first.direction, first.leg_number, first.continues_previous_direction), ("UP", 1, False))
        tracker.acknowledge(first)

        second = tracker.observe(position, price=10.63, as_of=second_time, price_source="alpaca_latest_quote:iex")
        self.assertIsNotNone(second)
        self.assertEqual((second.direction, second.leg_number, second.continues_previous_direction), ("UP", 2, True))

    def test_notification_path_sends_continuous_up_legs_and_immediate_reversal(self):
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            position = Position("US.TEST", 10, 9.5, "alpaca")
            source = FakePositionSource({"US.TEST": position})
            tracker = PremarketPositionTracker()
            times = [NOW + timedelta(seconds=value) for value in (0, 10, 20, 30)]
            market = SequentialMarketData([
                snapshot(10.0, times[0]),
                snapshot(10.31, times[1]),
                snapshot(10.63, times[2]),
                snapshot(10.30, times[3]),
            ])
            with patch(
                "alpaca_ma5_service.premarket_positions.safe_send_openclaw_messages",
                return_value=True,
            ) as notify:
                summaries = [
                    run_premarket_positions_once(
                        settings,
                        position_source=source,
                        market_data=market,
                        tracker=tracker,
                        now=as_of,
                    )
                    for as_of in times
                ]

        self.assertEqual([summary["sent"] for summary in summaries], [0, 1, 1, 1])
        self.assertEqual(notify.call_count, 3)
        messages = [call.args[1][0] for call in notify.call_args_list]
        self.assertIn("新方向第 1 段快速上涨", messages[0])
        self.assertIn("连续第 2 段快速上涨", messages[1])
        self.assertIn("新方向第 1 段快速下跌", messages[2])
        self.assertTrue(all("没有冷冻期" in message for message in messages))

    def test_direction_reversal_alerts_immediately_as_new_leg(self):
        tracker = PremarketPositionTracker()
        position = Position("US.TEST", 10, 9.5, "alpaca")
        tracker.observe(position, price=10.0, as_of=NOW, price_source="alpaca_latest_quote:iex")
        upward = tracker.observe(
            position,
            price=10.31,
            as_of=NOW + timedelta(seconds=20),
            price_source="alpaca_latest_quote:iex",
        )
        tracker.acknowledge(upward)

        downward = tracker.observe(
            position,
            price=9.99,
            as_of=NOW + timedelta(seconds=30),
            price_source="alpaca_latest_quote:iex",
        )

        self.assertIsNotNone(downward)
        self.assertEqual((downward.direction, downward.leg_number, downward.continues_previous_direction), ("DOWN", 1, False))

    def test_price_source_switch_rebuilds_baseline_instead_of_false_alert(self):
        tracker = PremarketPositionTracker()
        position = Position("US.TEST", 10, 9.5, "alpaca")
        tracker.observe(position, price=10.0, as_of=NOW, price_source="moomoo_snapshot:pre_price")

        switched = tracker.observe(
            position,
            price=10.5,
            as_of=NOW + timedelta(seconds=10),
            price_source="alpaca_latest_quote:iex",
        )
        movement = tracker.observe(
            position,
            price=10.82,
            as_of=NOW + timedelta(seconds=20),
            price_source="alpaca_latest_trade:iex",
        )

        self.assertIsNone(switched)
        self.assertIsNotNone(movement)
        self.assertEqual(movement.anchor_price, 10.5)
        self.assertEqual(movement.direction, "UP")

    def test_move_older_than_sixty_seconds_is_not_compared(self):
        tracker = PremarketPositionTracker()
        position = Position("US.TEST", 10, 10, "alpaca")
        self.assertIsNone(tracker.observe(position, price=10.0, as_of=NOW, price_source="test"))
        movement = tracker.observe(
            position,
            price=10.5,
            as_of=NOW + timedelta(seconds=61),
            price_source="test",
        )
        self.assertIsNone(movement)

    def test_failed_notification_is_not_counted_and_retries_on_next_quote(self):
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            position = Position("US.TEST", 10, 9.5, "alpaca")
            source = FakePositionSource({"US.TEST": position})
            tracker = PremarketPositionTracker()
            market = SequentialMarketData([
                snapshot(10.0, NOW),
                snapshot(10.31, NOW + timedelta(seconds=30)),
                snapshot(10.32, NOW + timedelta(seconds=40)),
            ])
            with patch(
                "alpaca_ma5_service.premarket_positions.safe_send_openclaw_messages",
                return_value=False,
            ) as notify:
                run_premarket_positions_once(settings, position_source=source, market_data=market, tracker=tracker, now=NOW)
                failed = run_premarket_positions_once(
                    settings,
                    position_source=source,
                    market_data=market,
                    tracker=tracker,
                    now=NOW + timedelta(seconds=30),
                )
                retried = run_premarket_positions_once(
                    settings,
                    position_source=source,
                    market_data=market,
                    tracker=tracker,
                    now=NOW + timedelta(seconds=40),
                )

        self.assertEqual(failed["alerts"], 1)
        self.assertEqual(failed["sent"], 0)
        self.assertEqual(retried["alerts"], 1)
        self.assertEqual(retried["sent"], 0)
        self.assertEqual(notify.call_count, 2)

    def test_threshold_is_exactly_three_percent(self):
        tracker = PremarketPositionTracker()
        position = Position("US.TEST", 1, 10, "alpaca")
        tracker.observe(position, price=10.0, as_of=NOW, price_source="test")
        movement = tracker.observe(position, price=10.0 * (1 + PREMARKET_POSITION_MOVE_PCT), as_of=NOW + timedelta(seconds=60), price_source="test")
        self.assertIsNotNone(movement)
        self.assertEqual(movement.direction, "UP")

    def test_legacy_premarket_watchcode_entry_is_noop(self):
        with patch("builtins.print") as printer:
            generate_premarket_watchcode()
        output = " ".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("已停用", output)
        self.assertIn("不筛选", output)


if __name__ == "__main__":
    unittest.main()
