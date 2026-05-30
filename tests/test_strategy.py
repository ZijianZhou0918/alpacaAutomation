from datetime import datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory

import unittest

from alpaca_ma5_service.broker import DryRunStockBroker
from alpaca_ma5_service.config import Settings
from alpaca_ma5_service.manual_order import discounted_limit_price, place_test_order, quantity_for_notional
from alpaca_ma5_service.models import MarketSnapshot, Position
from alpaca_ma5_service.service import run_forever_once, run_once
from alpaca_ma5_service.strategy import evaluate_buy, evaluate_sell
from alpaca_ma5_service.watchlist import read_watch_codes


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

    def submit_order(self, order_data):
        """模拟 Alpaca 接受订单。"""
        self.order_data = order_data
        return type("RawOrder", (), {"id": "test-order-1", "status": "accepted", "qty": order_data.qty})()


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
    )


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

            self.assertEqual(result.status, "ACCEPTED")
            self.assertEqual(result.price, 90.0)
            self.assertEqual(result.quantity, 0.055556)
            self.assertEqual(client.order_data.limit_price, 90.0)

    def test_manual_test_order_returns_rejected_on_alpaca_error(self):
        """Alpaca 拒单时返回 REJECTED，不抛 traceback。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})

            result = place_test_order(settings=settings, market_data=market_data, client=RejectingAlpacaClient())

            self.assertEqual(result.status, "REJECTED")
            self.assertIn("insufficient buying power", result.message)
            self.assertIn("buying_power=0", result.message)

    def test_run_forever_once_keeps_running_after_round_error(self):
        """forever 单轮失败时返回 None，让下一轮重建 broker 继续。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.8)})

            broker = run_forever_once(settings, market_data, FailingPositionsBroker(), datetime(2026, 5, 28, 10, 0))

            self.assertIsNone(broker)


if __name__ == "__main__":
    unittest.main()
