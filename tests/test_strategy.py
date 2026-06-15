from datetime import date, datetime, time
from inspect import signature
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import unittest

from alpaca_ma5_service import openclaw_notify
from alpaca_ma5_service.afterhours_high_low import (
    AfterHoursCandidate,
    MinuteBar,
    afterhours_signal_day,
    disable_afterhours_openclaw_output,
    is_afterhours_buy_time,
    is_regular_session,
    latest_trade_price_quote,
    load_afterhours_sell_state,
    manage_afterhours_sells,
    run_afterhours_high_low_strategy,
    scan_afterhours_candidates,
    screen_afterhours_candidates,
    simulate_afterhours_fill,
    submit_afterhours_limit_buys,
    write_afterhours_candidates,
    write_afterhours_watch_codes,
)
from alpaca_ma5_service.broker import AlpacaStockBroker, DryRunStockBroker
from alpaca_ma5_service.config import Settings
from alpaca_ma5_service.manual_order import build_test_order_preview, discounted_limit_price, place_test_order, quantity_for_notional
from alpaca_ma5_service.market_data import AlpacaMarketData, build_realtime_price_source, _SnapshotBar, _daily_request_end, _requires_realtime_price, _snapshot_inputs, _snapshot_previous_opens, _snapshot_today_open, _usable_today_open
from alpaca_ma5_service.market_time import is_buy_order_time, is_premarket_time, is_realtime_order_time, is_regular_market_time, next_poll_seconds, regular_open_has_started
from alpaca_ma5_service.moomoo_market_data import MoomooRealtimePriceSource, snapshot_open_from_row, snapshot_price_from_row, snapshot_update_time_from_row
from alpaca_ma5_service.models import MarketSnapshot, OrderResult, Position
from alpaca_ma5_service.openclaw_trade_control import execute_trade_command, parse_trade_command, render_trade_command_response
from alpaca_ma5_service.order_guard import wait_for_fill_or_cancel
from alpaca_ma5_service.service import _format_snapshot_time, print_snapshot, run_forever_once, run_once
from alpaca_ma5_service.state import append_order, count_today_buy_orders, count_today_symbol_order_errors, count_today_symbol_take_profit_half_sells
from alpaca_ma5_service.strategy import evaluate_buy, evaluate_sell
from alpaca_ma5_service.trade_notifications import render_order_submitted_message, render_trade_order_messages
from alpaca_ma5_service.watchlist import read_watch_codes
from alpaca_ma5_service.watchlist_charts import delete_watch_codes_from_watchlist, write_watchlist_chart_page
from alpaca_ma5_service.watchlist_generator import DailyBar, WatchCandidate, generate_watch_codes, is_common_stock_asset, refresh_watchlist_chart_from_watch_codes, request_end_datetime, screen_candidates, validate_candidates, write_watch_codes
from alpaca_ma5_service.afterhours_monitor import afterhours_monitor_settings, generate_afterhours_monitor_stocks, load_afterhours_bought_symbols, run_afterhours_high_low_buyer
from monitor_afterhours import monitor_afterhours
from tools.serve_watchlist_charts_lan import settings_for_watch_file


class FakeMarketData:
    def __init__(self, snapshots):
        """测试用行情源：按 symbol 返回预设快照。"""
        self.snapshots = snapshots
        self.calls = []

    def get_snapshot(self, symbol):
        """模拟真实行情源的 get_snapshot 接口。"""
        self.calls.append(symbol)
        return self.snapshots[symbol]


class FakeRealtimePriceSource:
    def __init__(self, price=16.2, source="moomoo_snapshot:last_price", as_of=None):
        """测试用实时价源，模拟 Moomoo OpenD 快照返回。"""
        self.price = price
        self.source = source
        self.as_of = as_of
        self.symbols = []

    def latest_price_quote(self, symbol):
        """记录查询代码，并返回带来源的价格。"""
        self.symbols.append(symbol)
        return type("Quote", (), {"price": self.price, "source": self.source, "as_of": self.as_of})()


class FakeAlpacaClient:
    def __init__(self, *, fractionable=True):
        """测试用 Alpaca client，记录收到的订单请求。"""
        self.order_data = None
        self.cancelled_order_id = None
        self.fractionable = fractionable

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

    def get_asset(self, symbol):
        """模拟 Alpaca asset 元数据。"""
        return type("RawAsset", (), {"symbol": symbol, "fractionable": self.fractionable})()


class ExistingPositionAlpacaClient(FakeAlpacaClient):
    def get_all_positions(self):
        """模拟 Alpaca 账户里已经持有该股票。"""
        return [type("RawPosition", (), {"symbol": "AAA", "qty": "1", "avg_entry_price": "16"})()]

    def get_orders(self, filter=None):
        """没有开放买单。"""
        return []


class OpenBuyOrderAlpacaClient(FakeAlpacaClient):
    def get_all_positions(self):
        """没有持仓。"""
        return []

    def get_orders(self, filter=None):
        """模拟 Alpaca 里已经有同股开放买单。"""
        return [type("RawOrder", (), {"symbol": "AAA", "side": "buy"})()]


class FailingExposureAlpacaClient(FakeAlpacaClient):
    def get_all_positions(self):
        """模拟风控检查持仓失败。"""
        raise Exception("temporary positions failure")


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
        self.last_limit_price = 0.0

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

    def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
        """模拟自动监控提交 BUY LIMIT 后超时撤单。"""
        self.buy_calls += 1
        self.last_limit_price = limit_price
        return OrderResult("order-1", symbol, "BUY", 1.0, limit_price, "CANCEL_REQUESTED", "not filled; cancel requested")


class RecordingBuyBroker(CancelingBuyBroker):
    def place_market_buy(self, symbol, notional_usd, current_price, reason):
        """只记录买入尝试，不返回成交。"""
        self.buy_calls += 1
        return OrderResult("order-1", symbol, "BUY", 1.0, current_price, "FILLED", "filled")

    def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
        """记录自动监控使用的买点限价。"""
        self.buy_calls += 1
        self.last_limit_price = limit_price
        return OrderResult("order-1", symbol, "BUY", 1.0, limit_price, "FILLED", "filled")


class RejectingThenBuyingBroker(CancelingBuyBroker):
    def __init__(self):
        """第一只股票拒单，第二只股票成交，用来验证错误单不占全局买入次数。"""
        super().__init__()
        self.symbols = []

    def place_market_buy(self, symbol, notional_usd, current_price, reason):
        """按调用顺序返回拒单/成交，模拟一只失败后继续买下一只。"""
        self.buy_calls += 1
        self.symbols.append(symbol)
        if self.buy_calls == 1:
            return OrderResult("reject-1", symbol, "BUY", 1.0, current_price, "REJECTED", "reject")
        return OrderResult("fill-2", symbol, "BUY", 1.0, current_price, "FILLED", "filled")

    def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
        """自动监控限价买入也按同样顺序模拟拒单/成交。"""
        self.buy_calls += 1
        self.symbols.append(symbol)
        self.last_limit_price = limit_price
        if self.buy_calls == 1:
            return OrderResult("reject-1", symbol, "BUY", 1.0, limit_price, "REJECTED", "reject")
        return OrderResult("fill-2", symbol, "BUY", 1.0, limit_price, "FILLED", "filled")


class FakeOpenClawCommandBroker:
    def __init__(self):
        """记录 OpenClaw 手动指令最终调用到的 broker 方法。"""
        self.calls = []
        self.positions = {"US.AAPL": Position("US.AAPL", 12.5, 200.0, "alpaca", source="alpaca-paper")}

    def source_name(self):
        """模拟真实 Alpaca 通道名称。"""
        return "alpaca-paper"

    def get_positions(self):
        """返回卖出指令默认使用的持仓。"""
        return self.positions

    def place_limit_buy(self, symbol, notional_usd, limit_price, reason, *, skip_time_validation=False):
        """记录固定限价买入调用。"""
        self.calls.append(("limit_buy", symbol, notional_usd, limit_price, reason, skip_time_validation))
        return OrderResult("buy-1", symbol, "BUY", round(notional_usd / limit_price, 6), limit_price, "SUBMITTED", "submitted")

    def place_market_buy(self, symbol, notional_usd, current_price, reason, *, skip_time_validation=False):
        """记录明确市价买入调用。"""
        self.calls.append(("market_buy", symbol, notional_usd, current_price, reason, skip_time_validation))
        return OrderResult("buy-1", symbol, "BUY", round(notional_usd / current_price, 6), current_price, "SUBMITTED", "submitted")

    def place_limit_sell(self, symbol, quantity, limit_price, reason, *, skip_time_validation=False):
        """记录固定限价卖出调用。"""
        self.calls.append(("limit_sell", symbol, quantity, limit_price, reason, skip_time_validation))
        return OrderResult("sell-1", symbol, "SELL", quantity, limit_price, "SUBMITTED", "submitted")

    def place_market_sell(self, symbol, quantity, current_price, reason, *, skip_time_validation=False):
        """记录明确市价卖出调用。"""
        self.calls.append(("market_sell", symbol, quantity, current_price, reason, skip_time_validation))
        return OrderResult("sell-1", symbol, "SELL", quantity, current_price, "SUBMITTED", "submitted")

    def cancel_open_orders(self, symbol="", reason=""):
        """记录按股票或全部挂单撤单调用。"""
        self.calls.append(("cancel_open", symbol, reason))
        return [OrderResult("cancel-1", symbol or "ALL", "CANCEL", 0, 0, "CANCELED", "cancel requested")]

    def cancel_order(self, order_id, reason):
        """记录按订单号撤单调用。"""
        self.calls.append(("cancel_order", order_id, reason))
        return OrderResult(order_id, "US.AAPL", "BUY", 1.0, 211.0, "CANCELED", "cancel requested")


def make_snapshot(symbol="US.TEST", current=9.8, closes=None, source="unit-test", today_open=0.0, today_open_source="", opens=None):
    """快速生成策略测试用行情快照。"""
    closes = closes or [10.0, 10.0, 10.0, 13.0]
    opens = opens or []
    return MarketSnapshot(symbol, current, closes, datetime(2026, 5, 28, 10, 0), source, today_open, today_open_source, opens)


def make_settings(root: Path) -> Settings:
    """生成隔离到临时目录的测试配置，避免污染真实 outputs。"""
    output_dir = root / "outputs"
    return Settings(
        watch_codes_file=root / "watch_codes.txt",
        output_dir=output_dir,
        state_file=output_dir / "state.json",
        buy_notional_usd=3400.0,
        max_daily_buys=1,
        max_symbol_order_errors=3,
        stop_loss_pct=-0.10,
        take_profit_half_pct=0.10,
        close_liquidation_start=time(15, 55),
        close_liquidation_end=time(16, 0),
        regular_poll_seconds=10,
        idle_poll_seconds=300,
        market_timezone="America/New_York",
        allow_fractional_shares=False,
        extended_hours_orders_enabled=True,
        extended_hours_limit_buffer_pct=0.003,
        order_cancel_after_seconds=0,
        order_status_poll_seconds=1,
        realtime_price_source="moomoo",
        moomoo_host="127.0.0.1",
        moomoo_port=11111,
        moomoo_security_firm="FUTUINC",
        moomoo_connect_timeout=3.0,
        moomoo_opend_exe_path=r"%APPDATA%\moomoo_OpenD\moomoo_OpenD.exe",
        moomoo_opend_startup_timeout=30.0,
        trade_notify_openclaw_enabled=False,
        openclaw_telegram_target="",
        openclaw_gateway_port=18789,
        watchlist_chart_lan_host="",
        watchlist_chart_lan_port=8766,
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


def make_minute_bar(symbol="TEST", hour=9, minute=30, open=10.0, high=10.0, low=10.0, close=10.0):
    """生成盘后策略测试用 1m bar。"""
    timestamp = datetime(2026, 5, 28, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    return MinuteBar(symbol, timestamp, open, high, low, close)


class StrategyTests(unittest.TestCase):
    def test_buy_when_signal_gain_20_to_40_adds_one_and_half_percent(self):
        """信号日涨幅 20%~40% 时，基础买点为 MA5+1.5%。"""
        signal = evaluate_buy(make_snapshot(current=10.7, closes=[10.0, 10.0, 10.0, 13.0]))
        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["today_ma5"], 10.74)
        self.assertAlmostEqual(signal.diagnostics["signal_day_gain_pct"], 0.30)
        self.assertAlmostEqual(signal.diagnostics["base_buy_point_pct"], 0.015)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point_pct"], 0.015)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point"], 10.9011)

    def test_hold_when_signal_gain_20_to_40_price_is_above_trigger_band(self):
        """信号日涨幅 20%~40% 时，当前价超过买点上方 2% 才不买。"""
        signal = evaluate_buy(make_snapshot(current=11.3, closes=[10.0, 10.0, 10.0, 13.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("超过分段买点上方 2%", signal.reason)

    def test_buy_when_current_price_is_within_two_percent_above_buy_point(self):
        """当前价在买点上方 2% 内时触发买入，供服务层用买点价挂单。"""
        signal = evaluate_buy(make_snapshot(current=11.1, closes=[10.0, 10.0, 10.0, 13.0]))

        self.assertEqual(signal.action, "BUY")
        self.assertGreater(signal.diagnostics["current_vs_buy_point_pct"], 0)
        self.assertLessEqual(signal.diagnostics["current_vs_buy_point_pct"], 0.02)
        self.assertAlmostEqual(signal.diagnostics["buy_trigger_distance_pct"], 0.02)
        self.assertIn("上方 2% 内", signal.reason)

    def test_buy_when_signal_gain_40_to_100_adds_three_percent(self):
        """信号日涨幅 40%~100% 时，基础买点为 MA5+3%。"""
        signal = evaluate_buy(make_snapshot(current=11.5, closes=[10.0, 10.0, 10.0, 15.0]))

        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["base_buy_point_pct"], 0.03)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point_pct"], 0.03)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point"], 11.639, places=3)

    def test_buy_when_signal_gain_above_100_adds_four_percent(self):
        """信号日涨幅大于 100% 时，基础买点为 MA5+4%。"""
        signal = evaluate_buy(make_snapshot(current=13.4, closes=[10.0, 10.0, 10.0, 22.0]))

        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["base_buy_point_pct"], 0.04)

    def test_open_gain_5_to_15_adds_one_percent(self):
        """当天开盘涨幅 5%~15% 时，最终买点再加 1%。"""
        signal = evaluate_buy(make_snapshot(current=10.85, closes=[10.0, 10.0, 10.0, 13.0], today_open=13.65))

        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["today_open_gain_pct"], 0.05)
        self.assertAlmostEqual(signal.diagnostics["open_bonus_pct"], 0.01)

    def test_open_gain_above_15_adds_two_percent(self):
        """当天开盘涨幅大于 15% 时，最终买点再加 2%。"""
        signal = evaluate_buy(make_snapshot(current=10.9, closes=[10.0, 10.0, 10.0, 13.0], today_open=15.0))

        self.assertEqual(signal.action, "BUY")
        self.assertGreater(signal.diagnostics["today_open_gain_pct"], 0.15)
        self.assertAlmostEqual(signal.diagnostics["open_bonus_pct"], 0.02)

    def test_hold_all_day_when_today_open_is_ten_percent_below_open_ma5(self):
        """今日开盘价低于开盘MA5 10% 时，整天不买这只股票。"""
        signal = evaluate_buy(make_snapshot(current=10.7, today_open=8.7, opens=[10.0, 10.0, 10.0, 10.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("当天不买入", signal.reason)
        self.assertLessEqual(signal.diagnostics["today_open_vs_open_ma5_pct"], -0.10)
        self.assertAlmostEqual(signal.diagnostics["today_open_ma5"], 9.74)

    def test_hold_when_today_open_drops_forty_percent(self):
        """今日开盘价相对信号日收盘跌幅达到 40% 时不下单。"""
        signal = evaluate_buy(make_snapshot(current=10.7, today_open=7.7))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("开盘跌幅", signal.reason)
        self.assertLessEqual(signal.diagnostics["today_open_gain_pct"], -0.40)

    def test_hold_when_today_open_is_below_dynamic_ma5(self):
        """今日开盘价低于当前动态MA5 时不下单。"""
        signal = evaluate_buy(make_snapshot(current=10.7, today_open=10.6, opens=[10.0, 10.0, 10.0, 10.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("低于当前动态MA5", signal.reason)
        self.assertLess(signal.diagnostics["today_open_vs_today_ma5_pct"], 0)

    def test_missing_today_open_uses_base_buy_point_only(self):
        """拿不到今日开盘价时，只使用基础买点。"""
        signal = evaluate_buy(make_snapshot(current=11.3, closes=[10.0, 10.0, 10.0, 13.0], today_open=0.0))

        self.assertEqual(signal.action, "HOLD")
        self.assertAlmostEqual(signal.diagnostics["open_bonus_pct"], 0.0)

    def test_hold_when_signal_day_gain_is_below_20_percent(self):
        """信号日涨幅低于 20% 时，没有有效分段买点。"""
        signal = evaluate_buy(make_snapshot(current=9.8, closes=[10.0, 10.0, 10.0, 11.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("低于 20%", signal.reason)

    def test_hold_when_current_price_is_at_today_ma5(self):
        """信号日涨幅低于 20% 时，即使当前价等于 MA5 也不买入。"""
        signal = evaluate_buy(make_snapshot(current=10.0, closes=[10.0, 10.0, 10.0, 10.0]))
        self.assertEqual(signal.action, "HOLD")

    def test_sell_on_10_percent_loss(self):
        """持仓亏损达到 10% 时应卖出全部。"""
        position = Position("US.TEST", 10, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 12, 0)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=9.0), now, settings)
        self.assertEqual(signal.action, "SELL_ALL")
        self.assertIn("10.00%", signal.reason)

    def test_hold_when_loss_is_less_than_stop_loss(self):
        """持仓亏损未到 10% 时不触发自动止损。"""
        position = Position("US.TEST", 10, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 12, 0)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=9.1), now, settings)
        self.assertEqual(signal.action, "HOLD")

    def test_sell_half_on_10_percent_gain(self):
        """持仓收益达到 10% 时应止盈一半。"""
        position = Position("US.TEST", 11, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 12, 0)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=11.0), now, settings)

        self.assertEqual(signal.action, "SELL_HALF")
        self.assertEqual(signal.quantity, 5.5)
        self.assertIn("止盈一半", signal.reason)

    def test_sell_near_regular_close(self):
        """临近常规盘收盘时应卖出全部。"""
        position = Position("US.TEST", 10, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 15, 56)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=10.2), now, settings)
        self.assertEqual(signal.action, "SELL_ALL")


class OpenClawTradeCommandTests(unittest.TestCase):
    def test_parse_buy_limit_command_from_chinese_message(self):
        """OpenClaw 消息应解析出买入金额、股票代码和固定限价。"""
        command = parse_trade_command("帮我买3000刀的AAPL，购买价格固定211")

        self.assertEqual(command.action, "BUY")
        self.assertEqual(command.symbol, "US.AAPL")
        self.assertEqual(command.notional_usd, 3000.0)
        self.assertEqual(command.limit_price, 211.0)

    def test_execute_buy_limit_command_uses_fixed_limit_path(self):
        """固定限价买入指令应调用 broker.place_limit_buy。"""
        broker = FakeOpenClawCommandBroker()
        settings = make_settings(Path("."))

        response = execute_trade_command("帮我买3000刀的AAPL，购买价格固定211", settings=settings, broker=broker)

        self.assertTrue(response.ok)
        self.assertEqual(broker.calls[0][:4], ("limit_buy", "US.AAPL", 3000.0, 211.0))
        self.assertIn("OpenClaw手动指令", broker.calls[0][4])
        self.assertTrue(broker.calls[0][5])

    def test_execute_buy_dynamic_limit_uses_current_price_multiplier(self):
        """当前价*倍率应先读实时价，再按计算出的限价提交 BUY LIMIT。"""
        broker = FakeOpenClawCommandBroker()
        settings = make_settings(Path("."))
        market_data = FakeMarketData({"US.NTAP": make_snapshot("US.NTAP", current=200.0)})

        response = execute_trade_command("帮我买5刀的NTAP，购买价格为当前价*0.95", settings=settings, broker=broker, market_data=market_data)

        self.assertTrue(response.ok)
        self.assertEqual(response.command.limit_price, 0.0)
        self.assertEqual(response.command.limit_price_multiplier, 0.95)
        self.assertEqual(market_data.calls, ["US.NTAP"])
        self.assertEqual(broker.calls[0][:4], ("limit_buy", "US.NTAP", 5.0, 190.0))
        self.assertIn("动态限价", broker.calls[0][4])
        self.assertTrue(broker.calls[0][5])

    def test_execute_buy_market_requires_explicit_market_phrase(self):
        """没有限价时，必须明确写市价，才会走 market buy。"""
        broker = FakeOpenClawCommandBroker()
        settings = make_settings(Path("."))
        market_data = FakeMarketData({"US.NTAP": make_snapshot("US.NTAP", current=200.0)})

        response = execute_trade_command("帮我买5刀的NTAP，市价买入", settings=settings, broker=broker, market_data=market_data)

        self.assertTrue(response.ok)
        self.assertEqual(broker.calls[0][:4], ("market_buy", "US.NTAP", 5.0, 200.0))
        self.assertTrue(broker.calls[0][5])

    def test_execute_sell_dynamic_limit_uses_current_price_multiplier(self):
        """卖出也支持当前价*倍率，计算后提交 SELL LIMIT。"""
        broker = FakeOpenClawCommandBroker()
        settings = make_settings(Path("."))
        market_data = FakeMarketData({"US.NTAP": make_snapshot("US.NTAP", current=200.0)})

        response = execute_trade_command("帮我卖出10股NTAP，价格为当前价*1.05", settings=settings, broker=broker, market_data=market_data)

        self.assertTrue(response.ok)
        self.assertEqual(response.command.limit_price_multiplier, 1.05)
        self.assertEqual(broker.calls[0][:4], ("limit_sell", "US.NTAP", 10.0, 210.0))
        self.assertTrue(broker.calls[0][5])

    def test_rejects_unsupported_trade_format_with_examples(self):
        """格式不在模板内时直接打回，并给出可用格式。"""
        with self.assertRaisesRegex(ValueError, "交易指令格式不支持"):
            parse_trade_command("帮我买5刀的NTAP")

        try:
            parse_trade_command("帮我买5刀的NTAP")
        except ValueError as exc:
            message = str(exc)
        self.assertIn("帮我买5刀的NTAP，购买价格固定160", message)
        self.assertIn("帮我买5刀的NTAP，购买价格为当前价*0.95", message)
        self.assertIn("帮我买5刀的NTAP，市价买入", message)

    def test_openclaw_response_prints_reject_reason(self):
        """OpenClaw 回复里要直接显示失败原因，不让 agent 自己猜。"""

        class RejectBroker(FakeOpenClawCommandBroker):
            def place_limit_buy(self, symbol, notional_usd, limit_price, reason, *, skip_time_validation=False):
                self.calls.append(("limit_buy", symbol, notional_usd, limit_price, reason, skip_time_validation))
                return OrderResult("", symbol, "BUY", 1.0, limit_price, "REJECTED", "Alpaca 拒单: buying power 不足")

        response = execute_trade_command("帮我买3000刀的AAPL，购买价格固定211", settings=make_settings(Path(".")), broker=RejectBroker())
        text = render_trade_command_response(response)

        self.assertIn("失败原因：Alpaca 拒单: buying power 不足", text)

    def test_execute_sell_limit_command_defaults_to_full_position(self):
        """卖出未指定股数时，默认卖出该股票当前全部持仓。"""
        broker = FakeOpenClawCommandBroker()
        settings = make_settings(Path("."))

        response = execute_trade_command("帮我卖出AAPL，限价210", settings=settings, broker=broker)

        self.assertTrue(response.ok)
        self.assertEqual(broker.calls[0][:4], ("limit_sell", "US.AAPL", 12.5, 210.0))
        self.assertTrue(broker.calls[0][5])

    def test_execute_cancel_command_by_symbol_or_order_id(self):
        """撤单指令可按股票代码，也可按订单号定位挂单。"""
        broker = FakeOpenClawCommandBroker()
        settings = make_settings(Path("."))

        by_symbol = execute_trade_command("撤单AAPL", settings=settings, broker=broker)
        by_order = execute_trade_command("撤单 订单号: FU1C9F9AADE3E56000", settings=settings, broker=broker)

        self.assertTrue(by_symbol.ok)
        self.assertTrue(by_order.ok)
        self.assertEqual(broker.calls[0][0:2], ("cancel_open", "US.AAPL"))
        self.assertEqual(broker.calls[1][0:2], ("cancel_order", "FU1C9F9AADE3E56000"))


class ServiceTests(unittest.TestCase):
    def test_watchlist_text_normalizes_and_deduplicates_symbols(self):
        """watchlist 文本读取会标准化、去重并忽略注释。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_codes.txt"
            path.write_text("# comment\nAAPL\nUS.AAPL\nTSLA, note\n", encoding="utf-8")
            self.assertEqual(read_watch_codes(path), ["US.AAPL", "US.TSLA"])

    def test_run_once_empty_watchlist_does_not_build_clients(self):
        """watch_codes 为空时直接返回，不启动行情源或 broker 连接。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("", encoding="utf-8")

            with patch("alpaca_ma5_service.service.build_market_data", side_effect=AssertionError("market data should not start")):
                with patch("alpaca_ma5_service.service.build_broker", side_effect=AssertionError("broker should not start")):
                    summary = run_once(settings, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary, {"watch": 0, "buy": 0, "sell": 0, "hold": 0, "errors": 0})

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

    def test_run_once_places_limit_buy_at_buy_point_when_price_is_nearby(self):
        """当前价在买点上方 2% 内时，自动监控用买点价提交 BUY LIMIT。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=10.9)})
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 1)
            self.assertEqual(broker.buy_calls, 1)
            self.assertAlmostEqual(broker.last_limit_price, 10.9417)

    def test_run_once_skips_buy_when_today_open_breaks_open_ma5_limit(self):
        """今日开盘价低于开盘MA5 10% 时，完整监控链路不提交买单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_snapshot("US.TEST", current=9.8, today_open=8.7, opens=[10.0, 10.0, 10.0, 10.0]),
            })
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 0)

    def test_run_once_skips_buy_when_today_open_is_below_dynamic_ma5(self):
        """今日开盘价低于当前动态MA5 时，完整监控链路不提交买单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_snapshot("US.TEST", current=9.8, today_open=10.0, opens=[10.0, 10.0, 10.0, 10.0]),
            })
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 0)

    def test_run_once_skips_buy_when_today_open_drops_forty_percent(self):
        """今日开盘价相对信号日收盘跌幅达到 40% 时，完整监控链路不提交买单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_snapshot("US.TEST", current=9.8, today_open=7.7),
            })
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 0)

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

    def test_run_once_order_error_does_not_block_next_symbol_buy(self):
        """错误下单不占每日买入次数，不能影响其他股票继续下单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.FAIL\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.FAIL": make_snapshot("US.FAIL", current=9.8),
                "US.NEXT": make_snapshot("US.NEXT", current=9.8),
            })
            broker = RejectingThenBuyingBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 1)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 2)
            self.assertEqual(broker.symbols, ["US.FAIL", "US.NEXT"])

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

    def test_run_once_skips_buys_during_premarket(self):
        """run_forever 真实链路在盘前只判断，不提交买单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.8)})
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 29, 8, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 0)

    def test_run_once_skips_symbol_after_three_order_errors(self):
        """同一只股票当天真实下单错误达到 3 次后，不再拉行情或提交订单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            for index in range(3):
                append_order(
                    settings.output_dir,
                    OrderResult(f"reject-{index}", "US.TEST", "BUY", 1, 10, "REJECTED", "reject"),
                    "unit-test reject",
                    day=date(2026, 5, 28),
                    created_at=datetime(2026, 5, 28, 9, index),
                )
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.8)})
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(summary["errors"], 0)
            self.assertEqual(market_data.calls, [])
            self.assertEqual(broker.buy_calls, 0)

    def test_order_error_limit_does_not_block_position_sell(self):
        """三次买入错误保护不能挡住已有持仓的止损/收盘卖出。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            for index in range(3):
                append_order(
                    settings.output_dir,
                    OrderResult(f"reject-{index}", "US.TEST", "BUY", 1, 10, "REJECTED", "reject"),
                    "unit-test reject",
                    day=date(2026, 5, 28),
                    created_at=datetime(2026, 5, 28, 9, index),
                )
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=8.0)})

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 0))

            self.assertEqual(summary["sell"], 1)
            self.assertEqual(summary["hold"], 0)
            self.assertEqual(market_data.calls, ["US.TEST"])

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

    def test_run_once_sells_half_watch_position_on_take_profit(self):
        """单轮监控会对 watchlist 内收益达到 10% 的持仓卖出一半。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=11.0)})

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 12, 0),
            )

            self.assertEqual(summary["sell"], 1)
            self.assertAlmostEqual(broker.get_positions()["US.TEST"].quantity, 15.0)

    def test_run_once_sells_half_only_once_per_day_on_take_profit(self):
        """10% 半仓止盈当天已成交后，后续轮询不重复卖一半。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=11.0)})

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 28, 12, 0)):
                first = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 0))
                second = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 1))

            self.assertEqual(first["sell"], 1)
            self.assertEqual(second["sell"], 0)
            self.assertEqual(second["hold"], 1)
            self.assertAlmostEqual(broker.get_positions()["US.TEST"].quantity, 15.0)

    def test_discounted_limit_helpers(self):
        """测试下单限价和股数计算保持稳定。"""
        self.assertEqual(discounted_limit_price(100.0, 0.9), 90.0)
        self.assertEqual(quantity_for_notional(5.0, 90.0), 0.055556)

    def test_test_order_preview_reads_same_snapshot_inputs(self):
        """测试下单预览应暴露提交前使用的价格和 MA 输入。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})

            preview = build_test_order_preview(settings=settings, market_data=market_data)

            self.assertEqual(preview.snapshot.symbol, "US.AAPL")
            self.assertEqual(preview.snapshot.current_price, 100.0)
            self.assertEqual(preview.snapshot.current_price_source, "unit-test")
            self.assertEqual(preview.snapshot.previous_closes[-4:], [10.0, 10.0, 10.0, 13.0])
            self.assertAlmostEqual(preview.snapshot.today_ma5, 28.6)
            self.assertEqual(preview.limit_price, 90.0)
            self.assertEqual(preview.quantity, 0.055556)
            self.assertEqual(market_data.calls, ["US.AAPL"])

    def test_manual_test_order_uses_shared_preview_reader(self):
        """真实测试下单和不提交预览应共用同一个行情读取逻辑。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = FakeAlpacaClient()

            with patch("alpaca_ma5_service.manual_order.build_test_order_preview", wraps=build_test_order_preview) as preview_fn:
                result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(result.price, 90.0)
            self.assertEqual(preview_fn.call_count, 1)
            self.assertEqual(market_data.calls, ["US.AAPL"])

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

    def test_manual_test_order_uses_shared_notification_tail(self):
        """测试下单和真实 broker 共用写订单记录后通知这段尾部逻辑。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = FakeAlpacaClient()
            events = []

            def fake_notify(settings_arg, result, reason, *, broker_name):
                events.append(("notify", result.status, count_today_buy_orders(settings_arg.output_dir, date(2026, 5, 29)), broker_name, reason))

            with patch("alpaca_ma5_service.manual_order.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                with patch("alpaca_ma5_service.manual_order.notify_order_submitted"):
                    with patch("alpaca_ma5_service.trade_notifications.notify_trade_order_event", side_effect=fake_notify):
                        result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(events, [("notify", "FILLED", 1, "alpaca-client", "manual test limit buy at 90% of current price")])

    def test_manual_test_order_notifies_when_rejected(self):
        """测试单只要尝试下单，即使被 Alpaca 拒绝也会通知。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            events = []

            def fake_notify(settings_arg, result, reason, *, broker_name):
                events.append((result.status, result.symbol, broker_name, reason))

            with patch("alpaca_ma5_service.trade_notifications.notify_trade_order_event", side_effect=fake_notify):
                result = place_test_order(settings=settings, market_data=market_data, client=RejectingAlpacaClient())

            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(events, [("REJECTED", "US.AAPL", "alpaca-client", "manual test limit buy at 90% of current price")])

    def test_manual_test_order_notifies_when_canceled(self):
        """测试单超时撤单也会通知，不要求成交成功。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = PendingAlpacaClient()
            events = []

            def fake_notify(settings_arg, result, reason, *, broker_name):
                events.append((result.status, result.symbol, broker_name, reason))

            with patch("alpaca_ma5_service.manual_order.notify_order_submitted"):
                with patch("alpaca_ma5_service.trade_notifications.notify_trade_order_event", side_effect=fake_notify):
                    result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "CANCELED")
            self.assertEqual(events, [("CANCELED", "US.AAPL", "alpaca-client", "manual test limit buy at 90% of current price")])

    def test_manual_test_order_notifies_immediately_after_submit(self):
        """测试单 submit_order 成功后马上发下单通知，不等最终撤单结果。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = PendingAlpacaClient()
            events = []

            def fake_submitted(settings_arg, result, reason, *, broker_name):
                events.append((result.status, result.order_id, result.symbol, broker_name, reason))

            with patch("alpaca_ma5_service.manual_order.notify_order_submitted", side_effect=fake_submitted):
                result = place_test_order(settings=settings, market_data=market_data, client=client)

            self.assertEqual(result.status, "CANCELED")
            self.assertEqual(events, [("ACCEPTED", "pending-order-1", "US.AAPL", "alpaca-client", "manual test limit buy at 90% of current price")])

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

    def test_alpaca_broker_cancels_unfilled_limit_buy_after_timeout(self):
        """自动监控 BUY LIMIT 未成交时，也复用超时撤单保护。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = PendingAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                result = broker.place_limit_buy("US.AAPL", 100.0, 10.78, "unit-test limit buy")

            self.assertEqual(result.status, "CANCELED")
            self.assertEqual(client.order_data.limit_price, 10.78)
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

    def test_alpaca_broker_rejects_premarket_buy(self):
        """即使绕过 service 直接调用 broker，盘前 BUY 也不会提交到 Alpaca。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = PendingAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 8, 0)):
                result = broker._submit_order("US.AAPL", "BUY", 1.0, 100.0)

            self.assertEqual(result.status, "REJECTED")
            self.assertIn("盘前", result.message)
            self.assertIsNone(client.order_data)

    def test_openclaw_manual_order_can_skip_time_validation(self):
        """OpenClaw 手动单跳过本地时段保护，直接交给 Alpaca 返回结果。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = FakeAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.notify_order_submitted"):
                with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 30, 10, 0)):
                    result = broker._submit_fixed_limit_order(
                        "US.AAPL",
                        "BUY",
                        1.0,
                        211.0,
                        "OpenClaw手动指令",
                        skip_time_validation=True,
                    )

            self.assertEqual(result.status, "FILLED")
            self.assertIsNotNone(client.order_data)

    def test_alpaca_broker_notifies_after_order_log(self):
        """真实买入链路先写本地订单记录，再通过 OpenClaw 通知。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
            client = FakeAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True
            events = []

            def fake_notify(settings_arg, result, reason, *, broker_name):
                events.append(("notify", result.status, count_today_buy_orders(settings.output_dir, date(2026, 5, 29)), broker_name, reason))

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                with patch("alpaca_ma5_service.broker.notify_order_submitted"):
                    with patch("alpaca_ma5_service.trade_notifications.notify_trade_order_event", side_effect=fake_notify):
                        result = broker.place_market_buy("US.AAPL", 100.0, 100.0, "unit-test buy")

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(events, [("notify", "FILLED", 1, "alpaca-paper", "unit-test buy")])

    def test_alpaca_broker_uses_fractional_qty_when_asset_allows(self):
        """Alpaca 资产明确支持碎股时，按买入金额计算小数股。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "allow_fractional_shares": True})
            client = FakeAlpacaClient(fractionable=True)
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                result = broker.place_market_buy("US.ANY", 2000.0, 3.86, "unit-test buy")

            self.assertEqual(result.status, "FILLED")
            self.assertAlmostEqual(float(client.order_data.qty), 518.134715)

    def test_alpaca_broker_uses_integer_qty_when_asset_is_not_fractionable(self):
        """Alpaca 资产不支持碎股时，真实买单自动向下取整，避免 not fractionable 拒单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "allow_fractional_shares": True})
            client = FakeAlpacaClient(fractionable=False)
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                result = broker.place_market_buy("US.ANY", 2000.0, 3.86, "unit-test buy")

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(float(client.order_data.qty), 518.0)

    def test_alpaca_broker_notifies_rejected_order_attempt(self):
        """真实 broker 尝试下单后即使被拒，也会写记录并通知。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = RejectingAlpacaClient()
            broker.paper = True
            events = []

            def fake_notify(settings_arg, result, reason, *, broker_name):
                events.append((result.status, count_today_buy_orders(settings_arg.output_dir, date(2026, 5, 29)), broker_name, reason))

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                with patch("alpaca_ma5_service.trade_notifications.notify_trade_order_event", side_effect=fake_notify):
                    result = broker.place_market_buy("US.AAPL", 100.0, 100.0, "unit-test buy")

            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(events, [("REJECTED", 0, "alpaca-paper", "unit-test buy")])

    def test_alpaca_broker_notifies_immediately_after_submit(self):
        """真实 broker submit_order 成功后马上发下单通知，不等最终成交/撤单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = PendingAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True
            events = []

            def fake_submitted(settings_arg, result, reason, *, broker_name):
                events.append((result.status, result.order_id, result.symbol, broker_name, reason))

            with patch("alpaca_ma5_service.broker.notify_order_submitted", side_effect=fake_submitted):
                with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                    result = broker._submit_order("US.AAPL", "BUY", 1.0, 100.0, "unit-test buy")

            self.assertEqual(result.status, "CANCELED")
            self.assertEqual(events, [("ACCEPTED", "pending-order-1", "US.AAPL", "alpaca-paper", "unit-test buy")])

    def test_alpaca_broker_ignores_notification_failure(self):
        """OpenClaw 发送失败只打印，不影响下单结果返回。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings = Settings(**{**settings.__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
            client = FakeAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                with patch("alpaca_ma5_service.broker.notify_order_submitted"):
                    with patch("alpaca_ma5_service.trade_notifications.notify_trade_order_event", side_effect=RuntimeError("boom")):
                        with redirect_stdout(StringIO()):
                            result = broker.place_market_buy("US.AAPL", 100.0, 100.0, "unit-test buy")

            self.assertEqual(result.status, "FILLED")

    def test_trade_notification_message_includes_sell_status(self):
        """卖出通知消息包含方向、状态、数量和原因。"""
        result = OrderResult("order-1", "US.AAPL", "SELL", 0.25, 100.0, "FILLED", "filled")

        messages = render_trade_order_messages(result, "止损卖出", broker_name="alpaca-live")

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertIn("Alpaca交易卖出已成交: US.AAPL", message)
        self.assertIn("状态: FILLED", message)
        self.assertIn("数量: 0.25", message)
        self.assertIn("原因: 止损卖出", message)

    def test_submitted_notification_uses_chinese_single_message(self):
        """即时下单通知也用一条中文消息，方便和最终状态区分。"""
        result = OrderResult("order-1", "US.AAPL", "BUY", 1.25, 100.0, "ACCEPTED", "submitted")

        message = render_order_submitted_message(result, "unit-test buy", broker_name="alpaca-live")

        self.assertIn("Alpaca交易买入已提交: US.AAPL", message)
        self.assertIn("账户: alpaca-live", message)
        self.assertIn("数量: 1.25", message)
        self.assertIn("状态: ACCEPTED", message)
        self.assertIn("原因: unit-test buy", message)

    def test_openclaw_send_starts_gateway_before_message(self):
        """本机 OpenClaw gateway 未就绪时，先启动 gateway 再发送消息。"""
        settings = Settings(**{**make_settings(Path(".")).__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
        calls = []
        results = iter(
            [
                CompletedProcess(["probe"], 1, "", "down"),
                CompletedProcess(["start"], 0, '{"ok":true}', ""),
                CompletedProcess(["probe"], 0, '{"ok":true}', ""),
                CompletedProcess(["message"], 0, '{"ok":true}', ""),
            ]
        )

        def fake_run(args, **kwargs):
            calls.append(args[1:])
            return next(results)

        with patch.object(openclaw_notify, "_OPENCLAW_GATEWAY_READY", False):
            with patch.object(openclaw_notify.shutil, "which", lambda name: "openclaw.cmd" if name == "openclaw.cmd" else None):
                with patch.object(openclaw_notify.subprocess, "run", fake_run):
                    openclaw_notify.send_openclaw_telegram_message(settings, "hello")

        self.assertEqual(
            calls,
            [
                ["gateway", "probe", "--json"],
                ["gateway", "start", "--json"],
                ["gateway", "probe", "--json"],
                ["message", "send", "--channel", "telegram", "--target", "123456", "--message", "hello", "--json"],
            ],
        )

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

    def test_symbol_order_error_count_tracks_rejected_status_only(self):
        """只统计同一股票同一天的拒单，撤单失败不算下单错误。"""
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "outputs"
            day = date(2026, 5, 29)
            append_order(output_dir, OrderResult("1", "US.AAPL", "BUY", 1, 10, "CANCELED", "cancel"), "test", day=day)
            append_order(output_dir, OrderResult("2", "AAPL", "BUY", 1, 10, "REJECTED", "reject"), "test", day=day)
            append_order(output_dir, OrderResult("3", "US.AAPL", "SELL", 1, 10, "CANCEL_FAILED", "failed"), "test", day=day)
            append_order(output_dir, OrderResult("4", "US.AAPL", "BUY", 1, 10, "FILLED", "filled"), "test", day=day)
            append_order(output_dir, OrderResult("5", "US.TSLA", "BUY", 1, 10, "REJECTED", "reject"), "test", day=day)
            append_order(output_dir, OrderResult("6", "US.AAPL", "BUY", 1, 10, "REJECTED", "old"), "test", day=date(2026, 5, 28))

            self.assertEqual(count_today_symbol_order_errors(output_dir, "US.AAPL", day), 1)
            self.assertEqual(count_today_symbol_order_errors(output_dir, "AAPL", day), 1)
            self.assertEqual(count_today_symbol_order_errors(output_dir, "US.TSLA", day), 1)
            self.assertEqual(count_today_symbol_order_errors(output_dir, "US.AAPL", date(2026, 5, 28)), 1)

    def test_take_profit_half_sell_count_tracks_executed_today_only(self):
        """半仓止盈去重只统计当天已成交的止盈卖单。"""
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "outputs"
            day = date(2026, 5, 29)
            append_order(output_dir, OrderResult("1", "US.AAPL", "SELL", 5, 11, "DRY_RUN", "sell"), "持仓收益达到 10.00%，止盈一半", day=day)
            append_order(output_dir, OrderResult("2", "US.AAPL", "SELL", 5, 11, "REJECTED", "reject"), "持仓收益达到 10.00%，止盈一半", day=day)
            append_order(output_dir, OrderResult("3", "US.AAPL", "SELL", 5, 11, "DRY_RUN", "sell"), "普通卖出", day=day)
            append_order(output_dir, OrderResult("4", "US.AAPL", "SELL", 5, 11, "DRY_RUN", "old"), "持仓收益达到 10.00%，止盈一半", day=date(2026, 5, 28))

            self.assertEqual(count_today_symbol_take_profit_half_sells(output_dir, "AAPL", day), 1)

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
            print_snapshot(make_snapshot("US.TEST", current=9.8, closes=[10.0, 10.0, 10.0, 11.0], today_open=9.9, opens=[10.0, 10.0, 10.0, 10.0]))

        output = buffer.getvalue()
        self.assertIn("[2026-05-28 10:00:00 PDT] US.TEST", output)
        self.assertIn("US.TEST", output)
        self.assertIn("当前价 9.8000（来源：unit-test）", output)
        self.assertIn("今日开盘 9.9000（来源：未知）", output)
        self.assertIn("前4日收盘 [10.0000, 10.0000, 10.0000, 11.0000]", output)
        self.assertIn("今日动态MA5 10.1600", output)
        self.assertIn("前4日开盘 [10.0000, 10.0000, 10.0000, 10.0000]", output)
        self.assertIn("开盘MA5 9.9800", output)
        self.assertIn("开盘偏离 -0.80%", output)
        self.assertIn("信号日涨幅 10.00%", output)
        self.assertIn("当天开盘涨幅 -10.00%", output)

    def test_snapshot_time_displays_pacific_time(self):
        """行情块标题时间统一显示为美西时间。"""
        eastern = datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))

        self.assertEqual(_format_snapshot_time(eastern), "2026-05-28 07:00:00 PDT")

    def test_alpaca_snapshot_uses_last_close_when_realtime_price_missing(self):
        """没有实时价时使用最新日线收盘价。"""
        bars = [
            _SnapshotBar(date(2026, 5, 22), 4.91),
            _SnapshotBar(date(2026, 5, 26), 4.60),
            _SnapshotBar(date(2026, 5, 27), 4.70),
            _SnapshotBar(date(2026, 5, 28), 4.68),
            _SnapshotBar(date(2026, 5, 29), 8.69),
        ]

        current_price, previous_closes = _snapshot_inputs(bars, datetime(2026, 5, 30, 17, 0), latest_trade_price=0.0)

        self.assertEqual(current_price, 8.69)
        self.assertEqual(previous_closes[-4:], [4.91, 4.60, 4.70, 4.68])

    def test_alpaca_snapshot_uses_realtime_price_even_outside_order_window(self):
        """Moomoo 当前价可用时，非下单时段也使用当前价展示和计算动态 MA5。"""
        bars = [
            _SnapshotBar(date(2026, 5, 22), 4.91),
            _SnapshotBar(date(2026, 5, 26), 4.60),
            _SnapshotBar(date(2026, 5, 27), 4.70),
            _SnapshotBar(date(2026, 5, 28), 4.68),
            _SnapshotBar(date(2026, 5, 29), 8.69),
        ]

        current_price, previous_closes = _snapshot_inputs(bars, datetime(2026, 5, 30, 17, 0), latest_trade_price=8.70)

        self.assertEqual(current_price, 8.70)
        self.assertEqual(previous_closes[-4:], [4.60, 4.70, 4.68, 8.69])

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

    def test_snapshot_previous_opens_uses_completed_days_only(self):
        """开盘MA5 只取今日之前的完成日开盘价。"""
        bars = [
            _SnapshotBar(date(2026, 5, 26), 4.60, 4.50),
            _SnapshotBar(date(2026, 5, 27), 4.70, 4.55),
            _SnapshotBar(date(2026, 5, 28), 4.68, 4.62),
            _SnapshotBar(date(2026, 5, 29), 8.69, 8.00),
        ]

        previous_opens = _snapshot_previous_opens(bars, datetime(2026, 5, 29, 10, 0))

        self.assertEqual(previous_opens, [4.50, 4.55, 4.62])

    def test_today_open_is_unknown_before_regular_open(self):
        """盘前还没有今日常规盘开盘价，不能使用 snapshot/open bar 的 open。"""
        bars = [_SnapshotBar(date(2026, 5, 29), 8.69, 9.99)]
        premarket = datetime(2026, 5, 29, 8, 0)

        self.assertFalse(regular_open_has_started(premarket))
        self.assertEqual(_usable_today_open(premarket, 9.99, "moomoo_snapshot:open_price"), (0.0, ""))
        self.assertEqual(_snapshot_today_open(bars, premarket, "alpaca_daily_open:sip"), (0.0, ""))

    def test_today_open_is_available_after_regular_open(self):
        """常规盘开盘后，今日开盘价才参与买入过滤。"""
        bars = [_SnapshotBar(date(2026, 5, 29), 8.69, 9.99)]
        regular = datetime(2026, 5, 29, 10, 0)

        self.assertTrue(regular_open_has_started(regular))
        self.assertEqual(_usable_today_open(regular, 9.99, "moomoo_snapshot:open_price"), (9.99, "moomoo_snapshot:open_price"))
        self.assertEqual(_snapshot_today_open(bars, regular, "alpaca_daily_open:sip"), (9.99, "alpaca_daily_open:sip"))

    def test_alpaca_snapshot_requires_realtime_price_during_extended_hours(self):
        """盘前/盘后也必须使用实时价，避免用日线 close 冒充当前价。"""
        self.assertTrue(_requires_realtime_price(datetime(2026, 5, 29, 8, 0)))
        self.assertTrue(_requires_realtime_price(datetime(2026, 5, 29, 19, 59)))
        self.assertFalse(_requires_realtime_price(datetime(2026, 5, 30, 10, 0)))

    def test_market_time_polling_uses_realtime_order_window(self):
        """只有常规盘用 10 秒；盘前盘后用 300 秒，临近 9:30 再缩短。"""
        settings = make_settings(Path("."))

        self.assertTrue(is_realtime_order_time(datetime(2026, 5, 29, 8, 0)))
        self.assertTrue(is_premarket_time(datetime(2026, 5, 29, 8, 0)))
        self.assertFalse(is_buy_order_time(datetime(2026, 5, 29, 8, 0)))
        self.assertTrue(is_regular_market_time(datetime(2026, 5, 29, 15, 59, 59)))
        self.assertFalse(is_regular_market_time(datetime(2026, 5, 29, 16, 0)))
        self.assertTrue(is_buy_order_time(datetime(2026, 5, 29, 10, 0)))
        self.assertTrue(is_buy_order_time(datetime(2026, 5, 29, 16, 30)))
        self.assertFalse(is_realtime_order_time(datetime(2026, 5, 29, 20, 0)))
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 8, 0)), settings.idle_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 3, 59, 50)), settings.idle_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 9, 29, 45)), 15)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 10, 0)), settings.regular_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 16, 30)), settings.idle_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 30, 10, 0)), settings.idle_poll_seconds)

    def test_watchlist_generator_filters_strategy_rules(self):
        """选股生成器使用涨幅、均线多头和 open/MA5>0.97 筛选股票。"""
        now_et = datetime(2026, 1, 21, 10, 0)
        low_shadow_bars = make_screen_bars("LOW_SHADOW", passes=True)
        low_shadow_bars[-1] = DailyBar("LOW_SHADOW", date(2026, 1, 20), 18.6, 25.5, 18.5, 25.0)
        weak_open_bars = make_screen_bars("WEAK_OPEN", passes=True)
        weak_open_bars[-1] = DailyBar("WEAK_OPEN", date(2026, 1, 20), 18.3, 25.5, 18.0, 25.0)
        candidates = screen_candidates(
            {
                "LOW_SHADOW": low_shadow_bars,
                "WEAK_OPEN": weak_open_bars,
                "FAIL": make_screen_bars("FAIL", passes=False),
            },
            now_et,
        )

        self.assertEqual([candidate.symbol for candidate in candidates], ["LOW_SHADOW"])
        self.assertGreater(candidates[0].gain_pct, 0.20)
        self.assertLess(candidates[0].upper_shadow_pct, 0.05)
        self.assertGreater(candidates[0].ma5, candidates[0].ma10)
        self.assertGreater(candidates[0].ma10, candidates[0].ma20)
        self.assertLess(candidates[0].open, candidates[0].ma5)
        self.assertGreater(candidates[0].open / candidates[0].ma5, 0.97)

    def test_market_data_defaults_to_sip_daily_and_moomoo_realtime(self):
        """日线默认用全市场 SIP，当前价默认用 Moomoo OpenD。"""
        market_data_defaults = signature(AlpacaMarketData.__init__).parameters
        watchlist_defaults = signature(generate_watch_codes).parameters
        settings = make_settings(Path("."))

        self.assertEqual(market_data_defaults["bars_feed"].default, "sip")
        self.assertEqual(market_data_defaults["trade_feed"].default, "iex")
        self.assertEqual(watchlist_defaults["feed"].default, "sip")
        self.assertEqual(settings.realtime_price_source, "moomoo")
        self.assertIsInstance(build_realtime_price_source(settings), MoomooRealtimePriceSource)

    def test_common_stock_asset_filter_excludes_special_securities(self):
        """选股池只保留普通股，排除 US_EQUITY 里的权证/单位/ETF/ADR 等。"""
        def asset(symbol, name, tradable=True):
            return type("FakeAsset", (), {"symbol": symbol, "name": name, "tradable": tradable})()

        self.assertTrue(is_common_stock_asset(asset("AAPL", "Apple Inc. Common Stock")))
        self.assertTrue(is_common_stock_asset(asset("GOOGL", "Alphabet Inc. Class A Common Stock")))
        self.assertTrue(is_common_stock_asset(asset("XYZ", "Example Ltd. Ordinary Shares")))
        self.assertTrue(is_common_stock_asset(asset("CMTY", "Community Bank System, Inc. Common Stock")))
        self.assertFalse(is_common_stock_asset(asset("HUBCW", "Hub Cyber Security Ltd. Warrant")))
        self.assertFalse(is_common_stock_asset(asset("ABCDU", "Example Acquisition Corp. Units")))
        self.assertFalse(is_common_stock_asset(asset("XYZR", "Example Corp. Rights")))
        self.assertFalse(is_common_stock_asset(asset("PREF", "Example Inc. Preferred Stock")))
        self.assertFalse(is_common_stock_asset(asset("SPY", "SPDR S&P 500 ETF Trust")))
        self.assertFalse(is_common_stock_asset(asset("BABA", "Alibaba Group Holding Limited American Depositary Shares")))
        self.assertFalse(is_common_stock_asset(asset("AAPL", "Apple Inc. Common Stock", tradable=False)))

    def test_moomoo_snapshot_price_uses_stockapi_field_priority(self):
        """Moomoo 快照价格字段优先级沿用 StockAPI 的 last/盘前/盘后/买卖价路径。"""
        row = {"last_price": 0, "nominal_price": 0, "pre_price": 9.62, "bid_price": 9.50, "ask_price": 9.70, "open_price": 9.10, "update_time": "2026-05-28 20:15:01.123"}

        self.assertEqual(snapshot_price_from_row(row), 9.62)
        self.assertEqual(snapshot_open_from_row(row), 9.10)
        self.assertEqual(snapshot_update_time_from_row(row), datetime(2026, 5, 28, 20, 15, 1, 123000))

    def test_moomoo_realtime_price_source_reads_snapshot(self):
        """确认 Moomoo 实时价源调用 get_market_snapshot，并读取有效价格。"""
        import pandas as pd

        class FakeMM:
            RET_OK = 0

        class FakeQuoteCtx:
            def __init__(self):
                self.codes = []

            def get_market_snapshot(self, codes):
                self.codes = codes
                return 0, pd.DataFrame([{"code": codes[0], "last_price": 12.34, "open_price": 11.11, "update_time": "2026-05-28 20:15:01.123"}])

        source = MoomooRealtimePriceSource()
        source.mm = FakeMM()
        source.quote_ctx = FakeQuoteCtx()

        self.assertEqual(source.latest_price("AAPL"), 12.34)
        quote = source.latest_price_quote("AAPL")
        self.assertEqual(quote.source, "moomoo_snapshot:last_price")
        self.assertEqual(quote.today_open, 11.11)
        self.assertEqual(quote.today_open_source, "moomoo_snapshot:open_price")
        self.assertEqual(quote.as_of, datetime(2026, 5, 28, 20, 15, 1, 123000))
        self.assertEqual(source.quote_ctx.codes, ["US.AAPL"])

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

    def test_watchlist_chart_page_writes_latest_html(self):
        """制图必须以 watch_codes.txt 为基准写出 latest HTML 页面。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = WatchCandidate("DEMO", date(2026, 1, 20), 0.3, 0.1, 8.0, 7.0, 6.0, 8.5, 10.0, 9.0)
            extra = WatchCandidate("SKIP", date(2026, 1, 20), 0.4, 0.1, 8.0, 7.0, 6.0, 8.5, 10.0, 9.0)
            settings.watch_codes_file.write_text("# signal_date=2026-01-20\nUS.DEMO\n", encoding="utf-8")

            page = write_watchlist_chart_page(settings, [candidate, extra], {"DEMO": make_screen_bars("DEMO"), "SKIP": make_screen_bars("SKIP")}, days=10)

            latest = settings.output_dir / "watchlist_charts" / "watch_code_daily_kline_latest.html"
            self.assertTrue(page.exists())
            self.assertTrue(latest.exists())
            html = latest.read_text(encoding="utf-8")
            self.assertIn("生成日期：2026-01-20", html)
            self.assertIn("US.DEMO", html)
            self.assertNotIn("US.SKIP", html)
            self.assertIn('id="stockSelect"', html)
            self.assertIn('data-stock-card data-code="US.DEMO"', html)
            self.assertIn('data-delete-code="US.DEMO"', html)
            self.assertIn('class="ma-line ma5-line"', html)
            self.assertIn("/api/watchlist/delete", html)

    def test_refresh_watchlist_chart_reads_watch_codes_file(self):
        """服务刷新图表时读取当前 watch_codes.txt，而不是旧候选列表。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("# signal_date=2026-01-20\nUS.AAA\nUS.BBB\n", encoding="utf-8")

            with patch(
                "alpaca_ma5_service.watchlist_generator.fetch_daily_bars",
                return_value={"AAA": make_screen_bars("AAA"), "BBB": make_screen_bars("BBB")},
            ) as fake_fetch:
                page = refresh_watchlist_chart_from_watch_codes(settings=settings, lookback_days=60, batch_size=50, feed="sip")

            latest = settings.output_dir / "watchlist_charts" / "watch_code_daily_kline_latest.html"
            self.assertTrue(page.exists())
            self.assertTrue(latest.exists())
            fake_fetch.assert_called_once()
            args = fake_fetch.call_args.args
            self.assertEqual(args[0], ["AAA", "BBB"])
            self.assertEqual(args[2], 60)
            self.assertEqual(args[3], 50)
            self.assertEqual(args[4], "sip")
            html = latest.read_text(encoding="utf-8")
            self.assertIn("US.AAA", html)
            self.assertIn("US.BBB", html)

    def test_refresh_watchlist_chart_can_use_named_watch_file(self):
        """图表服务可按文件名切换到盘后观察池。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("# signal_date=2026-01-20\nUS.DEFAULT\n", encoding="utf-8")
            afterhours_file = settings.watch_codes_file.with_name("watch_code_afterhours.txt")
            afterhours_file.write_text("# signal_date=2026-06-09\nUS.PAVS\nUS.MTEN\n", encoding="utf-8")
            chart_settings = settings_for_watch_file(settings, "watch_code_afterhours.txt")

            with patch(
                "alpaca_ma5_service.watchlist_generator.fetch_daily_bars",
                return_value={"PAVS": make_screen_bars("PAVS"), "MTEN": make_screen_bars("MTEN")},
            ) as fake_fetch:
                refresh_watchlist_chart_from_watch_codes(settings=chart_settings, lookback_days=60, batch_size=50, feed="sip")

            args = fake_fetch.call_args.args
            self.assertEqual(args[0], ["PAVS", "MTEN"])
            latest = chart_settings.output_dir / "watchlist_charts" / "watch_code_daily_kline_latest.html"
            html = latest.read_text(encoding="utf-8")
            self.assertIn("US.PAVS", html)
            self.assertIn("US.MTEN", html)
            self.assertNotIn("US.DEFAULT", html)

    def test_watchlist_chart_delete_updates_watch_codes_file(self):
        """图表页删除按钮应复用同一个 watch_codes.txt 删除逻辑。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("# keep comment\nUS.AAA\nUS.BBB, note\nUS.CCC\n", encoding="utf-8")

            result = delete_watch_codes_from_watchlist(settings, ["BBB", "US.CCC"])

            self.assertTrue(result["ok"])
            self.assertTrue(result["removed"])
            self.assertEqual(result["codes"], ["US.BBB", "US.CCC"])
            self.assertEqual(read_watch_codes(settings.watch_codes_file), ["US.AAA"])

    def test_generate_watch_codes_prints_http_chart_url(self):
        """完整生成 watch_codes 后应生成 HTML 并打印 HTTP URL。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            output = StringIO()
            server_started = []

            with patch(
                "alpaca_ma5_service.watchlist_generator.fetch_daily_bars",
                return_value={"PASS": make_screen_bars("PASS", passes=True)},
            ):
                with patch("alpaca_ma5_service.watchlist_generator.ensure_watchlist_chart_server_running", lambda settings_arg: server_started.append(settings_arg)):
                    with patch("alpaca_ma5_service.watchlist_generator.watchlist_chart_http_url", return_value="http://10.0.0.168:8766/watch_code_daily_kline_latest.html"):
                        with redirect_stdout(output):
                            candidates = generate_watch_codes(settings=settings, symbols=["PASS"], lookback_days=60, batch_size=100, feed="sip")

            latest = settings.output_dir / "watchlist_charts" / "watch_code_daily_kline_latest.html"
            self.assertEqual([candidate.symbol for candidate in candidates], ["PASS"])
            self.assertTrue(latest.exists())
            self.assertEqual(server_started, [settings])
            self.assertIn("Watchlist chart HTTP URL: http://10.0.0.168:8766/watch_code_daily_kline_latest.html", output.getvalue())

    def test_watchlist_generator_rejects_invalid_ma_order_before_write(self):
        """写入前再次强校验，MA5 不是最大时直接拒绝。"""
        candidate = WatchCandidate("BAD", date(2026, 1, 20), 0.3, 0.1, 8.0, 9.0, 10.0, 12.0, 13.0, 12.5)

        with self.assertRaisesRegex(RuntimeError, "MA5>MA10>MA20"):
            validate_candidates([candidate])

    def test_watchlist_generator_rejects_weak_open_ratio_before_write(self):
        """写入前再次强校验，open/MA5 必须大于 0.97。"""
        candidate = WatchCandidate("BAD", date(2026, 1, 20), 0.3, 0.1, 10.0, 9.0, 8.0, 9.7, 13.0, 12.5)

        with self.assertRaisesRegex(RuntimeError, "open/MA5>0.97"):
            validate_candidates([candidate])

    def test_watchlist_generator_uses_safe_sip_daily_request_end(self):
        """SIP 日线 end 使用 20 分钟旧数据，避免 recent 权限错误。"""
        saturday = datetime(2026, 5, 30, 17, 30)
        friday_after_close = datetime(2026, 5, 29, 16, 30)
        friday_ready = datetime(2026, 5, 29, 16, 40)

        self.assertEqual(request_end_datetime(saturday).date(), date(2026, 5, 30))
        self.assertEqual(request_end_datetime(friday_after_close, "sip"), datetime(2026, 5, 29, 0, 0))
        self.assertEqual(request_end_datetime(friday_after_close, "iex"), datetime(2026, 5, 30, 0, 0))
        self.assertEqual(request_end_datetime(friday_ready, "sip"), datetime(2026, 5, 29, 16, 20))
        self.assertEqual(_daily_request_end(friday_ready, "sip"), datetime(2026, 5, 29, 16, 20))


class AfterHoursHighLowTests(unittest.TestCase):
    def test_afterhours_time_windows_block_regular_session_buy(self):
        """盘中只能管理卖出，不允许触发盘后买入策略。"""
        regular = datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        afterhours = datetime(2026, 5, 28, 16, 1, tzinfo=ZoneInfo("America/New_York"))
        afterhours_end = datetime(2026, 5, 28, 20, 0, tzinfo=ZoneInfo("America/New_York"))
        late_evening = datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York"))

        self.assertTrue(is_regular_session(regular))
        self.assertFalse(is_afterhours_buy_time(regular))
        self.assertTrue(is_afterhours_buy_time(afterhours))
        self.assertFalse(is_afterhours_buy_time(afterhours_end))
        self.assertFalse(is_afterhours_buy_time(late_evening))

    def test_afterhours_signal_day_uses_latest_completed_regular_session(self):
        """生成盘后观察池时，周末或收盘前使用最近一个已完成常规盘。"""
        friday_after_close = datetime(2026, 6, 12, 16, 1, tzinfo=ZoneInfo("America/New_York"))
        saturday_evening = datetime(2026, 6, 13, 20, 28, tzinfo=ZoneInfo("America/New_York"))
        monday_morning = datetime(2026, 6, 15, 8, 0, tzinfo=ZoneInfo("America/New_York"))

        self.assertEqual(afterhours_signal_day(friday_after_close), date(2026, 6, 12))
        self.assertEqual(afterhours_signal_day(saturday_evening), date(2026, 6, 12))
        self.assertEqual(afterhours_signal_day(monday_morning), date(2026, 6, 12))

    def test_afterhours_screen_uses_regular_high_low_ratio(self):
        """常规盘 high/low > 2.5 才进入盘后候选，并按 close*0.8 算买入价。"""
        bars_by_symbol = {
            "AAA": [
                make_minute_bar("AAA", 9, 30, open=10.0, high=12.0, low=10.0, close=11.0),
                make_minute_bar("AAA", 15, 59, open=19.0, high=26.0, low=18.0, close=20.0),
            ],
            "BBB": [
                make_minute_bar("BBB", 9, 30, open=10.0, high=12.0, low=10.0, close=11.0),
                make_minute_bar("BBB", 15, 59, open=15.0, high=24.0, low=15.0, close=18.0),
            ],
        }

        candidates = screen_afterhours_candidates(bars_by_symbol, date(2026, 5, 28))

        self.assertEqual([candidate.symbol for candidate in candidates], ["AAA"])
        self.assertEqual(candidates[0].regular_high, 26.0)
        self.assertEqual(candidates[0].regular_low, 10.0)
        self.assertEqual(candidates[0].buy_limit, 16.0)
        self.assertEqual(candidates[0].target_sell_price, 17.6)

    def test_afterhours_fill_prefers_open_then_limit(self):
        """盘后 1m bar 成交：open 先触发用 open，否则 low 触发用限价。"""
        candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 21.0, 10.0, 20.0, 2.1, 16.0, 17.6)

        open_fill = simulate_afterhours_fill(candidate, [make_minute_bar("AAA", 16, 1, open=15.5, high=16.5, low=15.0, close=16.0)])
        limit_fill = simulate_afterhours_fill(candidate, [make_minute_bar("AAA", 16, 2, open=16.8, high=17.0, low=15.8, close=16.2)])
        no_fill = simulate_afterhours_fill(candidate, [make_minute_bar("AAA", 16, 3, open=16.8, high=17.0, low=16.1, close=16.5)])

        self.assertEqual(open_fill.status, "FILLED_OPEN")
        self.assertEqual(open_fill.fill_price, 15.5)
        self.assertEqual(limit_fill.status, "FILLED_LIMIT")
        self.assertEqual(limit_fill.fill_price, 16.0)
        self.assertEqual(no_fill.status, "NOT_TOUCHED")

    def test_afterhours_candidate_file_includes_signal_date(self):
        """候选文件名和内容都带交易日期，方便复盘当天筛选结果。"""
        with TemporaryDirectory() as tmp:
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 21.0, 10.0, 20.0, 2.1, 16.0, 17.6)

            path = write_afterhours_candidates(Path(tmp), [candidate], candidate.signal_date)

            self.assertEqual(path.name, "afterhours_candidates_2026-05-28.csv")
            content = path.read_text(encoding="utf-8-sig")
            self.assertIn("signal_date", content)
            self.assertIn("2026-05-28", content)
            self.assertIn("AAA", content)

    def test_afterhours_watch_code_file_uses_us_prefix(self):
        """盘后观察池写到项目根目录专用 txt，并沿用 US. 前缀。"""
        with TemporaryDirectory() as tmp:
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 21.0, 10.0, 20.0, 2.1, 16.0, 17.6)

            path = write_afterhours_watch_codes(Path(tmp) / "watch_code_afterhours.txt", [candidate], candidate.signal_date)

            self.assertEqual(path.name, "watch_code_afterhours.txt")
            self.assertEqual(read_watch_codes(path), ["US.AAA"])
            self.assertIn("signal_date=2026-05-28", path.read_text(encoding="utf-8"))

    def test_afterhours_scan_reuses_latest_watch_file_and_candidate_csv(self):
        """当天 txt 和候选 CSV 都有效时，直接复用缓存，不重新拉常规盘分钟线。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            now_et = datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", signal_day, 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            write_afterhours_candidates(settings.output_dir, [candidate], signal_day)
            write_afterhours_watch_codes(settings.watch_codes_file.with_name("watch_code_afterhours.txt"), [candidate], signal_day, 2.5)

            with patch("alpaca_ma5_service.afterhours_high_low.load_afterhours_symbol_pool") as fake_pool:
                with patch("alpaca_ma5_service.afterhours_high_low.fetch_minute_bars") as fake_fetch:
                    cached = scan_afterhours_candidates(settings, now_et, range_ratio_threshold=2.5)

            self.assertEqual(len(cached), 1)
            self.assertEqual(cached[0].symbol, "US.AAA")
            self.assertEqual(cached[0].signal_date, signal_day)
            self.assertEqual(cached[0].buy_limit, 16.0)
            fake_pool.assert_not_called()
            fake_fetch.assert_not_called()

    def test_afterhours_scan_regenerates_when_watch_file_is_stale(self):
        """txt 不是当天时不能复用旧缓存，需要重新扫描当天常规盘分钟线。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            old_day = date(2026, 5, 27)
            signal_day = date(2026, 5, 28)
            now_et = datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York"))
            old_candidate = AfterHoursCandidate("AAA", old_day, 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            write_afterhours_candidates(settings.output_dir, [old_candidate], old_day)
            write_afterhours_watch_codes(settings.watch_codes_file.with_name("watch_code_afterhours.txt"), [old_candidate], old_day, 2.5)
            bars = {
                "AAA": [
                    make_minute_bar("AAA", 9, 30, open=10.0, high=12.0, low=10.0, close=11.0),
                    make_minute_bar("AAA", 15, 59, open=19.0, high=26.0, low=18.0, close=20.0),
                ]
            }

            with patch("alpaca_ma5_service.afterhours_high_low.load_afterhours_symbol_pool", return_value=["AAA"]) as fake_pool:
                with patch("alpaca_ma5_service.afterhours_high_low.fetch_minute_bars", return_value=bars) as fake_fetch:
                    candidates = scan_afterhours_candidates(settings, now_et, range_ratio_threshold=2.5)

            self.assertEqual([candidate.symbol for candidate in candidates], ["AAA"])
            fake_pool.assert_called_once()
            fake_fetch.assert_called_once()
            self.assertTrue((settings.output_dir / "afterhours_candidates_2026-05-28.csv").exists())

    def test_afterhours_scan_on_weekend_uses_previous_friday(self):
        """周末手动生成盘后观察池时，扫描最近一个完成交易日的常规盘。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 6, 12)
            now_et = datetime(2026, 6, 13, 20, 28, tzinfo=ZoneInfo("America/New_York"))
            bars = {
                "AAA": [
                    MinuteBar("AAA", datetime(2026, 6, 12, 9, 30, tzinfo=ZoneInfo("America/New_York")), 10.0, 12.0, 10.0, 11.0),
                    MinuteBar("AAA", datetime(2026, 6, 12, 15, 59, tzinfo=ZoneInfo("America/New_York")), 19.0, 26.0, 18.0, 20.0),
                ]
            }

            with patch("alpaca_ma5_service.afterhours_high_low.load_afterhours_symbol_pool", return_value=["AAA"]):
                with patch("alpaca_ma5_service.afterhours_high_low.fetch_minute_bars", return_value=bars) as fake_fetch:
                    candidates = scan_afterhours_candidates(settings, now_et, range_ratio_threshold=2.5)

            self.assertEqual([candidate.symbol for candidate in candidates], ["AAA"])
            self.assertEqual(candidates[0].signal_date, signal_day)
            self.assertEqual(fake_fetch.call_args.args[1].date(), signal_day)
            self.assertEqual(fake_fetch.call_args.args[2].date(), signal_day)
            self.assertTrue((settings.output_dir / "afterhours_candidates_2026-06-12.csv").exists())

    def test_afterhours_scan_ignores_cache_for_custom_symbols(self):
        """手动传入股票列表时不复用全量缓存，避免调试扫描被旧 txt 干扰。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            now_et = datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York"))
            cached_candidate = AfterHoursCandidate("AAA", signal_day, 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            write_afterhours_candidates(settings.output_dir, [cached_candidate], signal_day)
            write_afterhours_watch_codes(settings.watch_codes_file.with_name("watch_code_afterhours.txt"), [cached_candidate], signal_day, 2.5)
            bars = {
                "BBB": [
                    make_minute_bar("BBB", 9, 30, open=10.0, high=12.0, low=10.0, close=11.0),
                    make_minute_bar("BBB", 15, 59, open=19.0, high=26.0, low=18.0, close=20.0),
                ]
            }

            with patch("alpaca_ma5_service.afterhours_high_low.fetch_minute_bars", return_value=bars) as fake_fetch:
                candidates = scan_afterhours_candidates(settings, now_et, symbols=["BBB"], range_ratio_threshold=2.5)

            self.assertEqual([candidate.symbol for candidate in candidates], ["BBB"])
            fake_fetch.assert_called_once()

    def test_afterhours_real_orders_require_paper_by_default(self):
        """盘后真实下单默认必须是 Paper 账户，防止误用 live key。"""
        candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

        class FakeConnection:
            paper = False
            client = object()

        with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=FakeConnection()):
            with self.assertRaisesRegex(RuntimeError, "只允许 Paper"):
                submit_afterhours_limit_buys(
                    make_settings(Path(".")),
                    [candidate],
                    3400.0,
                    datetime(2026, 5, 28, 16, 1, tzinfo=ZoneInfo("America/New_York")),
                    require_paper=True,
                )

    def test_afterhours_order_price_uses_moomoo_source(self):
        """盘后订单判断默认用 Moomoo OpenD 实时价，并保留具体价格来源。"""
        settings = make_settings(Path("."))
        price_source = FakeRealtimePriceSource(price=16.2)

        price, source = latest_trade_price_quote("AAA", settings, price_source=price_source)

        self.assertEqual(price, 16.2)
        self.assertEqual(source, "moomoo_snapshot:last_price")
        self.assertEqual(price_source.symbols, ["US.AAA"])

    def test_afterhours_order_waits_for_drop_signal_before_submit(self):
        """当前跌幅未超过 18% 时不提交订单，只等待下一次信号。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = FakeAlpacaClient()
            connection = type("FakeConnection", (), {"paper": True, "client": client})()
            output = StringIO()

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote", return_value=(16.6, "moomoo_snapshot:last_price")):
                    with patch("alpaca_ma5_service.afterhours_high_low.append_order") as fake_append:
                        with redirect_stdout(output):
                            results = submit_afterhours_limit_buys(
                                settings,
                                [candidate],
                                3400.0,
                                datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York")),
                            )

            self.assertEqual(results[0].status, "NO_SIGNAL")
            self.assertIsNone(client.order_data)
            self.assertIn("股票", output.getvalue())
            self.assertIn("US.AAA", output.getvalue())
            self.assertIn("等待信号", output.getvalue())
            self.assertNotIn("[等待信号] US.AAA", output.getvalue())
            fake_append.assert_not_called()

    def test_afterhours_order_skips_previous_day_moomoo_quote(self):
        """Moomoo 快照日期不是当前交易日时，即使价格触发也不提交订单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = FakeAlpacaClient()
            connection = type("FakeConnection", (), {"paper": True, "client": client})()
            price_source = FakeRealtimePriceSource(price=16.0, as_of=datetime(2026, 5, 27, 20, 15))

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.build_afterhours_price_source", return_value=price_source):
                    with patch("alpaca_ma5_service.afterhours_high_low.append_order") as fake_append:
                        results = submit_afterhours_limit_buys(
                            settings,
                            [candidate],
                            3400.0,
                            datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York")),
                        )

            self.assertEqual(results[0].status, "NO_SIGNAL")
            self.assertIsNone(client.order_data)
            fake_append.assert_not_called()

    def test_afterhours_order_cancels_after_five_minutes_when_not_filled(self):
        """当前跌幅超过 18% 时才提交订单，并使用 300 秒未成交撤单保护。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = FakeAlpacaClient(fractionable=False)
            expected = OrderResult("order-1", "US.AAA", "BUY", 212.0, 16.0, "CANCELED", "not filled; cancel requested")
            connection = type("FakeConnection", (), {"paper": True, "client": client})()

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote", return_value=(16.2, "moomoo_snapshot:last_price")):
                    with patch("alpaca_ma5_service.afterhours_high_low.wait_for_fill_or_cancel", return_value=expected) as fake_wait:
                        with patch("alpaca_ma5_service.afterhours_high_low.append_order") as fake_append:
                            with patch("alpaca_ma5_service.trade_notifications.safe_send_openclaw_messages") as fake_openclaw:
                                results = submit_afterhours_limit_buys(
                                    settings,
                                    [candidate],
                                    3400.0,
                                    datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York")),
                                    timeout_seconds=300,
                                    poll_seconds=5,
                                )

            self.assertEqual(results, [expected])
            self.assertEqual(float(client.order_data.limit_price), 16.0)
            self.assertEqual(float(client.order_data.qty), 212.0)
            self.assertEqual(fake_wait.call_args.kwargs["timeout_seconds"], 300)
            self.assertEqual(fake_wait.call_args.kwargs["poll_seconds"], 5)
            self.assertEqual(fake_append.call_count, 2)
            fake_openclaw.assert_not_called()

    def test_afterhours_order_skips_when_local_buy_already_filled_today(self):
        """本地订单记录显示今天已经买成时，即使重启或手动调用也不重复下单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            candidate = AfterHoursCandidate("AAA", signal_day, 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = FakeAlpacaClient()
            connection = type("FakeConnection", (), {"paper": True, "client": client})()
            append_order(
                settings.output_dir,
                OrderResult("fill-1", "US.AAA", "BUY", 1.0, 16.0, "FILLED", "filled"),
                "盘后 high/low>2.5 买入；range=2.6",
                day=signal_day,
            )

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.build_afterhours_price_source", side_effect=AssertionError("price source should not start")):
                    with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote") as fake_price:
                        results = submit_afterhours_limit_buys(
                            settings,
                            [candidate],
                            3400.0,
                            datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York")),
                        )

            self.assertEqual(results[0].status, "ALREADY_BOUGHT_TODAY")
            self.assertIsNone(client.order_data)
            fake_price.assert_not_called()

    def test_afterhours_order_skips_when_alpaca_position_exists(self):
        """Alpaca 当前已有持仓时跳过买入，防止脚本多开或本地记录丢失后重复买。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = ExistingPositionAlpacaClient()
            connection = type("FakeConnection", (), {"paper": True, "client": client})()

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote") as fake_price:
                    results = submit_afterhours_limit_buys(
                        settings,
                        [candidate],
                        3400.0,
                        datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York")),
                    )

            self.assertEqual(results[0].status, "EXISTING_POSITION")
            self.assertIsNone(client.order_data)
            fake_price.assert_not_called()

    def test_afterhours_order_skips_when_open_buy_order_exists(self):
        """Alpaca 当前已有开放买单时跳过买入，避免同一股票挂出多个买单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = OpenBuyOrderAlpacaClient()
            connection = type("FakeConnection", (), {"paper": True, "client": client})()

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote") as fake_price:
                    results = submit_afterhours_limit_buys(
                        settings,
                        [candidate],
                        3400.0,
                        datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York")),
                    )

            self.assertEqual(results[0].status, "OPEN_BUY_ORDER")
            self.assertIsNone(client.order_data)
            fake_price.assert_not_called()

    def test_afterhours_order_blocks_when_exposure_check_fails(self):
        """无法确认 Alpaca 持仓/开放买单时，本轮暂停买入而不是冒险提交订单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = FailingExposureAlpacaClient()
            connection = type("FakeConnection", (), {"paper": True, "client": client})()

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote") as fake_price:
                    results = submit_afterhours_limit_buys(
                        settings,
                        [candidate],
                        3400.0,
                        datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York")),
                    )

            self.assertEqual(results[0].status, "RISK_BLOCKED")
            self.assertIsNone(client.order_data)
            fake_price.assert_not_called()

    def test_afterhours_settings_disable_openclaw_output(self):
        """盘后策略内部关闭 OpenClaw 输出，但不修改原始 settings。"""
        settings = make_settings(Path("."))
        settings = Settings(**{**settings.__dict__, "trade_notify_openclaw_enabled": True})

        afterhours_settings = disable_afterhours_openclaw_output(settings)

        self.assertTrue(settings.trade_notify_openclaw_enabled)
        self.assertFalse(afterhours_settings.trade_notify_openclaw_enabled)

    def test_afterhours_entry_accepts_require_paper_parameter(self):
        """入口函数把是否强制 Paper 下单放在参数里。"""
        settings = make_settings(Path("."))
        now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
        candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

        with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
            with patch("alpaca_ma5_service.afterhours_monitor.load_afterhours_bought_symbols", return_value=set()):
                with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]):
                    with patch("alpaca_ma5_service.afterhours_monitor.submit_afterhours_limit_buys", return_value=[]) as fake_submit:
                        with patch("alpaca_ma5_service.afterhours_monitor.manage_afterhours_sells", return_value=[]):
                            run_afterhours_high_low_buyer(require_paper=False, max_loops=1, sleep=lambda seconds: None, now_provider=lambda: now_et)

        self.assertFalse(fake_submit.call_args.kwargs["require_paper"])
        self.assertEqual(fake_submit.call_args.args[2], 3400.0)
        self.assertEqual(fake_submit.call_args.kwargs["drop_signal_threshold"], 0.18)
        self.assertEqual(fake_submit.call_args.kwargs["timeout_seconds"], 300)

    def test_afterhours_entry_keeps_monitoring_after_daily_scan(self):
        """入口默认是持续监控：当天只扫一次池，下一轮复用候选池检查买入信号。"""
        settings = make_settings(Path("."))
        now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
        candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
        result = OrderResult("", "US.AAA", "BUY", 0, 16.0, "NO_SIGNAL", "waiting")

        with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
            with patch("alpaca_ma5_service.afterhours_monitor.load_afterhours_bought_symbols", return_value=set()):
                with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]) as fake_scan:
                    with patch("alpaca_ma5_service.afterhours_monitor.submit_afterhours_limit_buys", return_value=[result]) as fake_submit:
                        with patch("alpaca_ma5_service.afterhours_monitor.manage_afterhours_sells", return_value=[]) as fake_sells:
                            run_afterhours_high_low_buyer(require_paper=True, max_loops=2, sleep=lambda seconds: None, now_provider=lambda: now_et)

        fake_scan.assert_called_once()
        self.assertEqual(fake_submit.call_count, 2)
        self.assertEqual(fake_submit.call_args.args[1], [candidate])
        self.assertEqual(fake_sells.call_count, 2)

    def test_afterhours_monitor_keeps_watching_after_canceled_order(self):
        """撤单不算买入次数，下一轮继续等待同一只股票的新信号。"""
        settings = make_settings(Path("."))
        now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
        candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
        canceled = OrderResult("order-1", "US.AAA", "BUY", 218.0, 16.0, "CANCELED", "not filled; cancel requested")

        with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
            with patch("alpaca_ma5_service.afterhours_monitor.load_afterhours_bought_symbols", return_value=set()):
                with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]):
                    with patch("alpaca_ma5_service.afterhours_monitor.submit_afterhours_limit_buys", return_value=[canceled]) as fake_submit:
                        with patch("alpaca_ma5_service.afterhours_monitor.manage_afterhours_sells", return_value=[]):
                            run_afterhours_high_low_buyer(require_paper=True, max_loops=2, sleep=lambda seconds: None, now_provider=lambda: now_et)

        self.assertEqual(fake_submit.call_count, 2)

    def test_afterhours_monitor_skips_symbol_after_filled_buy(self):
        """每只股票盘后最多买成一次；成交后不再重复买同一只。"""
        settings = make_settings(Path("."))
        now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
        candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
        filled = OrderResult("order-1", "US.AAA", "BUY", 218.0, 16.0, "FILLED", "filled")

        with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
            with patch("alpaca_ma5_service.afterhours_monitor.load_afterhours_bought_symbols", return_value=set()):
                with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]):
                    with patch("alpaca_ma5_service.afterhours_monitor.submit_afterhours_limit_buys", return_value=[filled]) as fake_submit:
                        with patch("alpaca_ma5_service.afterhours_monitor.manage_afterhours_sells", return_value=[]):
                            run_afterhours_high_low_buyer(require_paper=True, max_loops=2, sleep=lambda seconds: None, now_provider=lambda: now_et)

        fake_submit.assert_called_once()

    def test_afterhours_restore_bought_symbols_ignores_canceled_orders(self):
        """重启恢复时撤单不算；只有实际成交的盘后买单会跳过。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            append_order(
                settings.output_dir,
                OrderResult("cancel-1", "US.AAA", "BUY", 1.0, 16.0, "CANCELED", "not filled"),
                "盘后 high/low>2.5 买入；range=2.6",
                day=signal_day,
                created_at=datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York")),
            )
            append_order(
                settings.output_dir,
                OrderResult("fill-1", "US.BBB", "BUY", 1.0, 16.0, "FILLED", "filled"),
                "盘后 high/low>2.5 买入；range=2.6",
                day=signal_day,
                created_at=datetime(2026, 5, 28, 20, 16, tzinfo=ZoneInfo("America/New_York")),
            )

            symbols = load_afterhours_bought_symbols(settings, signal_day)

        self.assertEqual(symbols, {"US.BBB"})

    def test_afterhours_sell_skips_when_open_sell_order_exists(self):
        """Alpaca 当前已有开放卖单时，不重复提交卖出。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            candidate = AfterHoursCandidate("AAA", signal_day, 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            write_afterhours_candidates(settings.output_dir, [candidate], signal_day)
            append_order(
                settings.output_dir,
                OrderResult("buy-1", "US.AAA", "BUY", 10.0, 16.0, "FILLED", "filled"),
                "盘后 high/low>2.5 买入；range=2.6",
                day=signal_day,
            )
            client = type("OpenSellClient", (), {"get_orders": lambda self, filter=None: [type("RawOrder", (), {"symbol": "AAA", "side": "sell"})()]})()

            class Broker:
                def __init__(self, settings_arg):
                    self.client = client
                    self.sell_calls = 0

                def get_positions(self):
                    return {"US.AAA": Position("US.AAA", 10.0, 16.0, "alpaca")}

                def place_market_sell(self, symbol, quantity, current_price, reason):
                    self.sell_calls += 1
                    return OrderResult("sell-1", symbol, "SELL", quantity, current_price, "FILLED", "filled")

            with patch("alpaca_ma5_service.broker.AlpacaStockBroker", Broker):
                with patch("alpaca_ma5_service.afterhours_high_low.build_afterhours_price_source", return_value=None):
                    with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote") as fake_price:
                        results = manage_afterhours_sells(
                            settings,
                            datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York")),
                            dry_run=False,
                        )

            self.assertEqual(results, [])
            fake_price.assert_not_called()

    def test_afterhours_sell_ignores_candidate_without_strategy_buy_record(self):
        """候选池里有股票但本策略没有真实买成记录时，不能卖掉账户原有持仓。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            candidate = AfterHoursCandidate("AAA", signal_day, 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            write_afterhours_candidates(settings.output_dir, [candidate], signal_day)

            with patch("alpaca_ma5_service.broker.AlpacaStockBroker", side_effect=AssertionError("broker should not start")):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote") as fake_price:
                    results = manage_afterhours_sells(
                        settings,
                        datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York")),
                        dry_run=False,
                    )

            self.assertEqual(results, [])
            fake_price.assert_not_called()

    def test_afterhours_sell_does_not_mark_half_sold_when_canceled(self):
        """卖出半仓被取消时不写入 half_sold，下一轮还能继续检查卖出信号。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            candidate = AfterHoursCandidate("AAA", signal_day, 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            write_afterhours_candidates(settings.output_dir, [candidate], signal_day)
            append_order(
                settings.output_dir,
                OrderResult("buy-1", "US.AAA", "BUY", 10.0, 16.0, "FILLED", "filled"),
                "盘后 high/low>2.5 买入；range=2.6",
                day=signal_day,
            )
            client = type("NoOpenOrdersClient", (), {"get_orders": lambda self, filter=None: []})()

            class Broker:
                def __init__(self, settings_arg):
                    self.client = client

                def get_positions(self):
                    return {"US.AAA": Position("US.AAA", 10.0, 16.0, "alpaca")}

                def place_market_sell(self, symbol, quantity, current_price, reason):
                    return OrderResult("sell-1", symbol, "SELL", quantity, current_price, "CANCELED", "not filled")

            with patch("alpaca_ma5_service.broker.AlpacaStockBroker", Broker):
                with patch("alpaca_ma5_service.afterhours_high_low.build_afterhours_price_source", return_value=None):
                    with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote", return_value=(18.0, "moomoo_snapshot:last_price")):
                        results = manage_afterhours_sells(
                            settings,
                            datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York")),
                            dry_run=False,
                        )

            self.assertEqual(results[0].status, "CANCELED")
            self.assertEqual(load_afterhours_sell_state(settings.output_dir), {})

    def test_afterhours_runner_skips_regular_session_without_fetching(self):
        """常规盘运行时直接退出，不拉行情也不提交买单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))

            with patch("alpaca_ma5_service.afterhours_high_low.fetch_minute_bars") as fake_fetch:
                with patch("alpaca_ma5_service.afterhours_high_low.manage_afterhours_sells", return_value=[]) as fake_sells:
                    candidates = run_afterhours_high_low_strategy(settings=settings, symbols=["AAA"], dry_run=True, now_et=now_et)

            self.assertEqual(candidates, [])
            fake_fetch.assert_not_called()
            fake_sells.assert_called_once()

    def test_afterhours_monitor_settings_use_afterhours_tail_window(self):
        """自动监控入口把尾盘卖出窗口切到盘后 19:55-20:00。"""
        settings = make_settings(Path("."))

        monitor_settings = afterhours_monitor_settings(settings)

        self.assertEqual(monitor_settings.close_liquidation_start, time(19, 55))
        self.assertEqual(monitor_settings.close_liquidation_end, time(20, 0))
        self.assertEqual(settings.close_liquidation_start, time(15, 55))

    def test_generate_afterhours_monitor_stocks_calls_scan(self):
        """生成盘后监控股票函数只负责筛选并返回候选池。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

            with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]) as fake_scan:
                candidates = generate_afterhours_monitor_stocks(settings=settings, now_et=now_et)

            self.assertEqual(candidates, [candidate])
            self.assertEqual(fake_scan.call_args.args[0].output_dir, settings.output_dir)
            self.assertEqual(fake_scan.call_args.args[1], now_et)

    def test_generate_afterhours_monitor_stocks_does_not_wait_for_time_window(self):
        """生成 watch code 不看当前时间段；是否入选只由筛选条件决定。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))

            with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[]) as fake_scan:
                candidates = generate_afterhours_monitor_stocks(settings=settings, now_et=now_et)

            self.assertEqual(candidates, [])
            fake_scan.assert_called_once()
            self.assertEqual(fake_scan.call_args.args[1], now_et)

    def test_afterhours_monitor_wrapper_runs_shared_monitor(self):
        """新点击入口直接启动自动筛选/买入/卖出监控。"""
        now_et = datetime(2026, 5, 28, 15, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("monitor_afterhours.monitor_afterhours_trades") as fake_monitor:
            monitor_afterhours(max_loops=1, sleep=lambda seconds: None, now_provider=lambda: now_et)

        self.assertEqual(fake_monitor.call_args.kwargs["max_loops"], 1)
        self.assertIs(fake_monitor.call_args.kwargs["now_provider"](), now_et)


if __name__ == "__main__":
    unittest.main()
