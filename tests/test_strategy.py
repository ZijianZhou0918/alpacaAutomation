from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import unittest

from alpaca_ma5_service.broker import AlpacaStockBroker, DryRunStockBroker
from alpaca_ma5_service.config import Settings
from alpaca_ma5_service.manual_order import discounted_limit_price, place_test_order, quantity_for_notional
from alpaca_ma5_service.market_data import _SnapshotBar, _requires_realtime_price, _snapshot_inputs
from alpaca_ma5_service.market_time import is_realtime_order_time, next_poll_seconds
from alpaca_ma5_service.models import MarketSnapshot, OrderResult, Position
from alpaca_ma5_service.order_guard import wait_for_fill_or_cancel
from alpaca_ma5_service.service import print_snapshot, run_forever_once, run_once
from alpaca_ma5_service.state import append_order, count_today_buy_orders
from alpaca_ma5_service.strategy import evaluate_buy, evaluate_sell
from alpaca_ma5_service.watchlist import read_watch_codes
from alpaca_ma5_service.watchlist_generator import DailyBar, WatchCandidate, request_end_datetime, screen_candidates, validate_candidates, write_watch_codes


class FakeMarketData:
    def __init__(self, snapshots):
        """测试用行情源：按 symbol 返回预设快照。"""
        self.snapshots = snapshots

    def get_snapshot(self, symbol):
        """模拟真实行情源的 get_snapshot 接口。"""
        return self.snapshots[symbol]


class FakeAlpacaClient:
    def __init__(self):
        """测试用 Alpaca client，记录收到的订单请求。"""
        self.order_data = None
        self.cancelled_order_id = None

    def submit_order(self, order_data):
        """模拟 Alpaca 接受订单。"""
        self.order_data = order_data
        return type("RawOrder", (), {"id": "test-order-1", "status": "filled", "qty": order_data.qty})()

    def get_order_by_id(self, order_id):
        """模拟订单已成交。"""
        return type("RawOrder", (), {"id": order_id, "status": "filled", "qty": self.order_data.qty})()

    def cancel_order_by_id(self, order_id):
        """记录取消请求；已成交订单不会走到这里。"""
        self.cancelled_order_id = order_id


class PendingAlpacaClient(FakeAlpacaClient):
    def submit_order(self, order_data):
        """模拟订单提交成功但一直没有成交。"""
        self.order_data = order_data
        return type("RawOrder", (), {"id": "pending-order-1", "status": "accepted", "qty": order_data.qty, "filled_qty": "0"})()

    def get_order_by_id(self, order_id):
        """模拟订单仍在挂单状态。"""
        if self.cancelled_order_id == order_id:
            return type("RawOrder", (), {"id": order_id, "status": "canceled", "qty": self.order_data.qty, "filled_qty": "0"})()
        return type("RawOrder", (), {"id": order_id, "status": "accepted", "qty": self.order_data.qty, "filled_qty": "0"})()


class PartialFillAlpacaClient(PendingAlpacaClient):
    def submit_order(self, order_data):
        """模拟订单提交后已经部分成交。"""
        self.order_data = order_data
        return type("RawOrder", (), {"id": "pending-order-1", "status": "partially_filled", "qty": order_data.qty, "filled_qty": "0.25"})()

    def get_order_by_id(self, order_id):
        """模拟订单只成交一部分，剩余仍未成交。"""
        if self.cancelled_order_id == order_id:
            return type("RawOrder", (), {"id": order_id, "status": "canceled", "qty": self.order_data.qty, "filled_qty": "0.25"})()
        return type("RawOrder", (), {"id": order_id, "status": "partially_filled", "qty": self.order_data.qty, "filled_qty": "0.25"})()


class UnconfirmedCancelAlpacaClient(PendingAlpacaClient):
    def get_order_by_id(self, order_id):
        """模拟撤单请求发出后，Alpaca 仍未确认最终取消。"""
        return type("RawOrder", (), {"id": order_id, "status": "accepted", "qty": self.order_data.qty, "filled_qty": "0"})()


class RejectingAlpacaClient:
    def submit_order(self, order_data):
        """模拟 Alpaca 因购买力不足拒单。"""
        raise Exception('{"buying_power":"0","code":40310000,"message":"insufficient buying power"}')


class FailingPositionsBroker:
    def source_name(self):
        """模拟真实 broker 名称。"""
        return "alpaca-live"

    def get_positions(self):
        """模拟查询持仓接口临时失败。"""
        raise Exception('{"code":50010000,"message":"temporary alpaca failure"}')


class CancelingBuyBroker:
    def __init__(self):
        """记录买入尝试次数，确认未确认撤单不会继续买下一只。"""
        self.buy_calls = 0

    def source_name(self):
        """模拟下单后超时撤单的真实 broker。"""
        return "alpaca-paper"

    def get_positions(self):
        """没有持仓，触发买入判断。"""
        return {}

    def place_market_buy(self, symbol, notional_usd, current_price, reason):
        """模拟买单未成交后已请求取消。"""
        self.buy_calls += 1
        return OrderResult("order-1", symbol, "BUY", 1.0, current_price, "CANCEL_REQUESTED", "not filled; cancel requested")


class RecordingBuyBroker(CancelingBuyBroker):
    def place_market_buy(self, symbol, notional_usd, current_price, reason):
        """只记录买入尝试，不返回成交。"""
        self.buy_calls += 1
        return OrderResult("order-1", symbol, "BUY", 1.0, current_price, "FILLED", "filled")


def make_snapshot(symbol="US.TEST", current=9.8, closes=None):
    """快速生成策略测试用行情快照。"""
    closes = closes or [10.0, 10.0, 10.0, 11.0]
    return MarketSnapshot(symbol, current, closes, datetime(2026, 5, 28, 10, 0))


def make_settings(root: Path) -> Settings:
    """生成隔离到临时目录的测试配置，避免污染真实 outputs。"""
    output_dir = root / "outputs"
    return Settings(
        watch_codes_file=root / "watch_codes.txt",
        output_dir=output_dir,
        state_file=output_dir / "state.json",
        buy_notional_usd=300.0,
        max_daily_buys=1,
        stop_loss_pct=-0.15,
        close_liquidation_start=time(15, 55),
        close_liquidation_end=time(16, 0),
        regular_poll_seconds=60,
        idle_poll_seconds=300,
        market_timezone="America/New_York",
        allow_fractional_shares=False,
        extended_hours_orders_enabled=True,
        extended_hours_limit_buffer_pct=0.003,
        order_cancel_after_seconds=0,
        order_status_poll_seconds=1,
    )


def make_screen_bars(symbol="TEST", signal_day=date(2026, 1, 20), passes=True):
    """生成 watchlist 选股测试用的 20 根日线。"""
    bars = []
    for index in range(19):
        bars.append(DailyBar(symbol, date(2026, 1, index + 1), float(index + 1), float(index + 1), float(index + 1), float(index + 1)))

    if passes:
        bars.append(DailyBar(symbol, signal_day, 24.0, 28.0, 23.0, 25.0))
    else:
        bars.append(DailyBar(symbol, signal_day, 20.0, 21.0, 19.0, 20.0))
    return bars


class StrategyTests(unittest.TestCase):
    def test_buy_when_current_price_is_below_today_ma5(self):
        """当前价低于今日动态 MA5 时应触发买入。"""
        signal = evaluate_buy(make_snapshot(current=9.8, closes=[10.0, 10.0, 10.0, 11.0]))
        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["today_ma5"], 10.16)

    def test_hold_when_current_price_is_at_today_ma5(self):
        """当前价等于 MA5 时不买入。"""
        signal = evaluate_buy(make_snapshot(current=10.0, closes=[10.0, 10.0, 10.0, 10.0]))
        self.assertEqual(signal.action, "HOLD")

    def test_sell_on_15_percent_loss(self):
        """持仓亏损达到 15% 时应卖出全部。"""
        position = Position("US.TEST", 10, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 12, 0)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=8.5), now, settings)
        self.assertEqual(signal.action, "SELL_ALL")

    def test_sell_near_regular_close(self):
        """临近常规盘收盘时应卖出全部。"""
        position = Position("US.TEST", 10, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 15, 56)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=10.2), now, settings)
        self.assertEqual(signal.action, "SELL_ALL")


class ServiceTests(unittest.TestCase):
    def test_watchlist_text_normalizes_and_deduplicates_symbols(self):
        """watchlist 文本读取会标准化、去重并忽略注释。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_codes.txt"
            path.write_text("# comment\nAAPL\nUS.AAPL\nTSLA, note\n", encoding="utf-8")
            self.assertEqual(read_watch_codes(path), ["US.AAPL", "US.TSLA"])

    def test_run_once_buys_only_symbols_from_file(self):
        """单轮监控只处理 watch_codes.txt 里的代码。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.8)})
            broker = DryRunStockBroker(settings)

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["buy"], 1)
            self.assertIn("US.TEST", broker.get_positions())

    def test_run_once_blocks_more_buys_when_cancel_is_unconfirmed(self):
        """买单撤单未确认时，不算买成功，但会阻止本轮继续买下一只。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_snapshot("US.TEST", current=9.8),
                "US.NEXT": make_snapshot("US.NEXT", current=9.8),
            })
            broker = CancelingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 2)
            self.assertEqual(broker.buy_calls, 1)

    def test_run_once_skips_orders_outside_realtime_price_window(self):
        """周末/深夜只打印判断，不用日线收盘价提交真实订单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.8)})
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 30, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 0)

    def test_run_once_sells_watch_position_on_stop_loss(self):
        """单轮监控会对 watchlist 内持仓执行止损卖出。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=8.5)})

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 12, 0),
            )

            self.assertEqual(summary["sell"], 1)
            self.assertNotIn("US.TEST", broker.get_positions())

    def test_discounted_limit_helpers(self):
        """测试下单限价和股数计算保持稳定。"""
        self.assertEqual(discounted_limit_price(100.0, 0.9), 90.0)
        self.assertEqual(quantity_for_notional(5.0, 90.0), 0.055556)

    def test_manual_test_order_uses_discounted_limit(self):
        """测试下单使用当前价折扣后的 BUY LIMIT 请求。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = FakeAlpacaClient()

            result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(result.price, 90.0)
            self.assertEqual(result.quantity, 0.055556)
            self.assertEqual(client.order_data.limit_price, 90.0)
            self.assertIsNone(client.cancelled_order_id)

    def test_manual_test_order_cancels_when_unfilled_after_timeout(self):
        """测试下单超过等待时间仍未成交时，应请求取消订单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = PendingAlpacaClient()

            result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "CANCELED")
            self.assertEqual(result.order_id, "pending-order-1")
            self.assertEqual(client.cancelled_order_id, "pending-order-1")
            self.assertIn("Not filled within 0s", result.message)

    def test_manual_test_order_marks_partial_fill_before_cancel(self):
        """测试下单部分成交后超时撤单，应保留已成交数量。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = PartialFillAlpacaClient()

            result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "PARTIALLY_FILLED_CANCELED")
            self.assertEqual(result.quantity, 0.25)
            self.assertEqual(client.cancelled_order_id, "pending-order-1")

    def test_order_guard_preserves_partial_fill_on_final_cancel_status(self):
        """如果 Alpaca 最终状态已取消但有成交数量，仍按部分成交处理。"""
        client = FakeAlpacaClient()
        raw_order = type("RawOrder", (), {"id": "order-1", "status": "canceled", "qty": 1.0, "filled_qty": "0.25"})()

        result = wait_for_fill_or_cancel(client, raw_order, "US.AAPL", "BUY", 1.0, 100.0, "alpaca-paper", timeout_seconds=0)

        self.assertEqual(result.status, "PARTIALLY_FILLED_CANCELED")
        self.assertEqual(result.quantity, 0.25)
        self.assertIsNone(client.cancelled_order_id)

    def test_manual_test_order_marks_unconfirmed_cancel_as_risky(self):
        """撤单请求未确认最终状态时，保留 CANCEL_REQUESTED 供买入名额风控使用。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = UnconfirmedCancelAlpacaClient()

            result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "CANCEL_REQUESTED")
            self.assertIn("latest_status=ACCEPTED", result.message)

    def test_manual_test_order_returns_rejected_on_alpaca_error(self):
        """Alpaca 拒单时返回 REJECTED，不抛 traceback。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})

            result = place_test_order(settings=settings, market_data=market_data, client=RejectingAlpacaClient())

            self.assertEqual(result.status, "REJECTED")
            self.assertIn("insufficient buying power", result.message)
            self.assertIn("buying_power=0", result.message)

    def test_alpaca_broker_cancels_unfilled_order_after_timeout(self):
        """真实 broker 链路提交后未成交，也会在超时后请求取消。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = PendingAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                result = broker._submit_order("US.AAPL", "BUY", 1.0, 100.0)

            self.assertEqual(result.status, "CANCELED")
            self.assertEqual(client.cancelled_order_id, "pending-order-1")

    def test_alpaca_broker_rejects_orders_outside_realtime_window(self):
        """broker 自身也保护非实时价时段，避免绕过 service 后下单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = PendingAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 30, 10, 0)):
                result = broker._submit_order("US.AAPL", "BUY", 1.0, 100.0)

            self.assertEqual(result.status, "REJECTED")
            self.assertIsNone(client.order_data)

    def test_daily_buy_count_tracks_executed_and_risky_orders(self):
        """确认取消/拒单不计数，已成交和未确认撤单会占用买入名额。"""
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "outputs"
            append_order(output_dir, OrderResult("1", "US.AAPL", "BUY", 1, 10, "CANCELED", "cancel"), "test")
            append_order(output_dir, OrderResult("2", "US.AAPL", "BUY", 1, 10, "REJECTED", "reject"), "test")
            append_order(output_dir, OrderResult("3", "US.AAPL", "BUY", 1, 10, "FILLED", "filled"), "test")
            append_order(output_dir, OrderResult("4", "US.AAPL", "BUY", 0.25, 10, "PARTIALLY_FILLED_CANCEL_REQUESTED", "partial"), "test")
            append_order(output_dir, OrderResult("5", "US.AAPL", "BUY", 1, 10, "CANCEL_REQUESTED", "unconfirmed"), "test")
            append_order(output_dir, OrderResult("6", "US.AAPL", "BUY", 1, 10, "CANCEL_FAILED", "risky"), "test")

            self.assertEqual(count_today_buy_orders(output_dir), 4)

    def test_order_log_uses_market_day_when_provided(self):
        """订单文件可按美东交易日写入，避免本地时区跨日导致买入次数错位。"""
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "outputs"
            append_order(
                output_dir,
                OrderResult("1", "US.AAPL", "BUY", 1, 10, "FILLED", "filled"),
                "test",
                day=date(2026, 5, 29),
                created_at=datetime(2026, 5, 28, 21, 30),
            )

            self.assertEqual(count_today_buy_orders(output_dir, date(2026, 5, 29)), 1)
            self.assertEqual(count_today_buy_orders(output_dir, date(2026, 5, 28)), 0)

    def test_run_forever_once_keeps_running_after_round_error(self):
        """forever 单轮失败时返回 None，让下一轮重建 broker 继续。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.8)})

            broker = run_forever_once(settings, market_data, FailingPositionsBroker(), datetime(2026, 5, 28, 10, 0))

            self.assertIsNone(broker)

    def test_print_snapshot_outputs_ma_inputs(self):
        """监控输出应包含当前价、前 4 个收盘价和今日 MA5。"""
        buffer = StringIO()

        with redirect_stdout(buffer):
            print_snapshot(make_snapshot("US.TEST", current=9.8, closes=[10.0, 10.0, 10.0, 11.0]))

        output = buffer.getvalue()
        self.assertIn("current_price=9.8000", output)
        self.assertIn("previous_4_closes=[10.0000, 10.0000, 10.0000, 11.0000]", output)
        self.assertIn("today_ma5=10.1600", output)

    def test_alpaca_snapshot_uses_last_close_outside_regular_session(self):
        """非交易时段使用最新日线收盘价，不用 latest trade。"""
        bars = [
            _SnapshotBar(date(2026, 5, 22), 4.91),
            _SnapshotBar(date(2026, 5, 26), 4.60),
            _SnapshotBar(date(2026, 5, 27), 4.70),
            _SnapshotBar(date(2026, 5, 28), 4.68),
            _SnapshotBar(date(2026, 5, 29), 8.69),
        ]

        current_price, previous_closes = _snapshot_inputs(bars, datetime(2026, 5, 30, 17, 0), latest_trade_price=8.70)

        self.assertEqual(current_price, 8.69)
        self.assertEqual(previous_closes[-4:], [4.91, 4.60, 4.70, 4.68])

    def test_alpaca_snapshot_uses_latest_trade_during_regular_session(self):
        """可交易时段使用 latest trade，并用之前 4 个完成日线收盘价算 MA5。"""
        bars = [
            _SnapshotBar(date(2026, 5, 22), 4.91),
            _SnapshotBar(date(2026, 5, 26), 4.60),
            _SnapshotBar(date(2026, 5, 27), 4.70),
            _SnapshotBar(date(2026, 5, 28), 4.68),
        ]

        current_price, previous_closes = _snapshot_inputs(bars, datetime(2026, 5, 29, 10, 0), latest_trade_price=8.70)

        self.assertEqual(current_price, 8.70)
        self.assertEqual(previous_closes[-4:], [4.91, 4.60, 4.70, 4.68])

    def test_alpaca_snapshot_requires_realtime_price_during_extended_hours(self):
        """盘前/盘后也必须使用实时价，避免用日线 close 冒充当前价。"""
        self.assertTrue(_requires_realtime_price(datetime(2026, 5, 29, 8, 0)))
        self.assertTrue(_requires_realtime_price(datetime(2026, 5, 29, 19, 59)))
        self.assertFalse(_requires_realtime_price(datetime(2026, 5, 30, 10, 0)))

    def test_market_time_polling_uses_realtime_order_window(self):
        """盘前/盘后也用常规轮询频率；周末使用空闲轮询频率。"""
        settings = make_settings(Path("."))

        self.assertTrue(is_realtime_order_time(datetime(2026, 5, 29, 8, 0)))
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 8, 0)), settings.regular_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 30, 10, 0)), settings.idle_poll_seconds)

    def test_watchlist_generator_filters_strategy_rules(self):
        """选股生成器使用涨幅、上影线、均线多头和 open>MA5 筛选股票。"""
        now_et = datetime(2026, 1, 21, 10, 0)
        candidates = screen_candidates(
            {
                "PASS": make_screen_bars("PASS", passes=True),
                "FAIL": make_screen_bars("FAIL", passes=False),
            },
            now_et,
        )

        self.assertEqual([candidate.symbol for candidate in candidates], ["PASS"])
        self.assertGreater(candidates[0].gain_pct, 0.20)
        self.assertGreater(candidates[0].upper_shadow_pct, 0.05)
        self.assertGreater(candidates[0].ma5, candidates[0].ma10)
        self.assertGreater(candidates[0].ma10, candidates[0].ma20)

    def test_watchlist_generator_uses_global_signal_date_and_writes_codes(self):
        """所有股票共用最近已收盘 signal_date，并写成 US. 前缀 watchlist。"""
        with TemporaryDirectory() as tmp:
            now_et = datetime(2026, 1, 22, 10, 0)
            candidates = screen_candidates(
                {
                    "OLD": make_screen_bars("OLD", signal_day=date(2026, 1, 20), passes=True),
                    "NEW": make_screen_bars("NEW", signal_day=date(2026, 1, 21), passes=True),
                },
                now_et,
            )
            path = Path(tmp) / "watch_codes.txt"
            write_watch_codes(path, candidates)

            self.assertEqual([candidate.symbol for candidate in candidates], ["NEW"])
            self.assertEqual(read_watch_codes(path), ["US.NEW"])

    def test_watchlist_generator_rejects_invalid_ma_order_before_write(self):
        """写入前再次强校验，MA5 不是最大时直接拒绝。"""
        candidate = WatchCandidate("BAD", date(2026, 1, 20), 0.3, 0.1, 8.0, 9.0, 10.0, 12.0, 13.0, 12.5)

        with self.assertRaisesRegex(RuntimeError, "MA5>MA10>MA20"):
            validate_candidates([candidate])

    def test_watchlist_generator_uses_daily_request_boundary(self):
        """日线请求 end 使用日期边界，避免 SIP recent 限制。"""
        saturday = datetime(2026, 5, 30, 17, 30)
        friday_after_close = datetime(2026, 5, 29, 16, 30)

        self.assertEqual(request_end_datetime(saturday).date(), date(2026, 5, 30))
        self.assertEqual(request_end_datetime(friday_after_close).date(), date(2026, 5, 30))


if __name__ == "__main__":
    unittest.main()
