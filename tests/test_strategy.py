import hashlib
import hmac
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from inspect import signature
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import unittest

from alpaca_ma5_service import strategy as strategy_module, strategy_ma5_dip
from alpaca_ma5_service import openclaw_notify
from alpaca_ma5_service.afterhours_high_low import (
    AfterHoursCandidate,
    BUY_MONITOR_COLUMNS,
    MinuteBar,
    afterhours_signal_day,
    disable_afterhours_openclaw_output,
    is_afterhours_buy_time,
    is_regular_session,
    latest_trade_price_quote,
    load_afterhours_sell_state,
    manage_afterhours_sells,
    print_table_row as print_afterhours_table_row,
    run_afterhours_high_low_strategy,
    scan_afterhours_candidates,
    screen_afterhours_candidates,
    simulate_afterhours_fill,
    submit_afterhours_limit_buys,
    write_afterhours_candidates,
    write_afterhours_watch_codes,
)
from alpaca_ma5_service.broker import AlpacaStockBroker, DryRunStockBroker
from alpaca_ma5_service.config import BUY_NOTIONAL_USD, MA5_DIP_LADDER_STRATEGY_NAME, MA5_DIP_STRATEGY_NAME, Settings, build_settings
from alpaca_ma5_service.manual_order import build_test_order_preview, discounted_limit_price, place_test_order, quantity_for_notional
from alpaca_ma5_service.market_data import AlpacaMarketData, CorporateActionBasisError, MarketDataSafetyError, SNAPSHOT_PURPOSE_AUTOMATIC, SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION, build_realtime_price_source, _SnapshotBar, _daily_request_end, _requires_realtime_price, _snapshot_inputs, _snapshot_previous_opens, _snapshot_today_open, _usable_today_open, validate_latest_completed_bar_basis, validate_realtime_price_as_of
from alpaca_ma5_service.market_time import is_buy_order_time, is_premarket_monitor_finished, is_intraday_monitor_finished, is_premarket_time, is_realtime_order_time, is_regular_market_time, next_poll_seconds, regular_open_has_started
from alpaca_ma5_service.ladder import LadderStateStore
from alpaca_ma5_service.moomoo_market_data import MoomooRealtimePriceSource, snapshot_open_from_row, snapshot_price_from_row, snapshot_update_time_from_row
from alpaca_ma5_service.models import MarketSnapshot, OrderResult, Position, has_unconfirmed_order_status
from alpaca_ma5_service.openclaw_trade_control import execute_trade_command, parse_trade_command, render_trade_command_response
from alpaca_ma5_service.order_guard import wait_for_fill_or_cancel
from alpaca_ma5_service.premarket_monitor import (
    evaluate_premarket_ma5_recommendation,
    premarket_loop_poll_seconds,
    render_premarket_recommendation_message,
    run_premarket_recommendation_once,
    run_premarket_recommendations_forever,
)
from alpaca_ma5_service.premarket_watchlist import generate_premarket_watch_codes, premarket_watch_codes_path, screen_premarket_top_gain_candidates, write_premarket_watch_codes
from alpaca_ma5_service.service import MonitorTableRow, _format_snapshot_time, print_monitor_table, print_snapshot, run_forever, run_forever_once, run_once
from alpaca_ma5_service.state import append_order, count_today_buy_orders, count_today_symbol_order_errors, count_today_symbol_take_profit_half_sells, is_symbol_daily_buy_excluded
from alpaca_ma5_service.strategy import evaluate_buy, evaluate_sell, evaluate_take_profit_remainder_stop
from alpaca_ma5_service.trade_notifications import render_order_submitted_message, render_trade_order_messages
from alpaca_ma5_service.trading_calendar import offline_trading_day_decision, us_equity_holiday_name
from alpaca_ma5_service.watchlist import read_watch_codes
from alpaca_ma5_service.watchlist_charts import delete_watch_codes_from_watchlist, ensure_watchlist_chart_server_running, write_watchlist_chart_page
from alpaca_ma5_service.watchlist_generator import DailyBar, WatchCandidate, fetch_daily_bars, generate_watch_codes, is_common_stock_asset, refresh_watchlist_chart_from_watch_codes, request_end_datetime, screen_candidates, validate_candidates, watchcode_matches_rules, watchlist_rules_header, watchlist_screen_rules, write_watch_codes
from alpaca_ma5_service.afterhours_monitor import (
    AFTERHOURS_RANGE_RATIO_THRESHOLD,
    afterhours_monitor_settings,
    generate_afterhours_monitor_stocks,
    load_afterhours_bought_symbols,
    monitor_afterhours_buy_signals,
    render_afterhours_monitor_start_message,
    run_afterhours_high_low_buyer,
)
from monitor_afterhours import monitor_afterhours
from monitor_auto import (
    afterhours_watchcode_ready_for_session,
    ensure_afterhours_watchcode,
    ensure_current_session_watchcode,
    expected_signal_date,
    intraday_watchcode_ready_for_session,
    monitor_auto as run_monitor_auto,
    watchcode_ready_for_session,
)
from alpaca_ma5_service.workflows.monitoring.intraday import monitor_ma5_forever as run_intraday_monitor
from tools.serve_watchlist_charts_lan import settings_for_watch_file


class FakeMarketData:
    def __init__(self, snapshots):
        """测试用行情源：按 symbol 返回预设快照。"""
        self.snapshots = snapshots
        self.calls = []
        self.purposes = []

    def get_snapshot(self, symbol, *, purpose=SNAPSHOT_PURPOSE_AUTOMATIC):
        """模拟真实行情源的 get_snapshot 接口。"""
        self.calls.append(symbol)
        self.purposes.append(purpose)
        return self.snapshots[symbol]


class SafetyBlockingMarketData:
    def __init__(self, message="unit company-action basis mismatch"):
        """模拟行情口径无法安全确认，所有自动动作都必须失败关闭。"""
        self.message = message
        self.calls = []
        self.purposes = []

    def get_snapshot(self, symbol, *, purpose=SNAPSHOT_PURPOSE_AUTOMATIC):
        self.calls.append(symbol)
        self.purposes.append(purpose)
        raise MarketDataSafetyError(self.message)


class FakeRealtimePriceSource:
    def __init__(self, price=16.2, source="moomoo_snapshot:last_price", as_of=None):
        """测试用实时价源，模拟 Moomoo OpenD 快照返回。"""
        self.price = price
        self.source = source
        self.as_of = as_of
        self.symbols = []
        self.now = None

    def latest_price_quote(self, symbol, *, now=None):
        """记录查询代码，并返回带来源的价格。"""
        self.symbols.append(symbol)
        self.now = now
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
        exc = Exception('{"buying_power":"0","code":40310000,"message":"insufficient buying power"}')
        exc.status_code = 422
        raise exc


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
        self.last_notional_usd = 0.0
        self.last_limit_price = 0.0
        self.notionals = []

    def source_name(self):
        """模拟下单后超时撤单的真实 broker。"""
        return "alpaca-paper"

    def get_positions(self):
        """没有持仓，触发买入判断。"""
        return {}

    def place_market_buy(self, symbol, notional_usd, current_price, reason):
        """模拟买单未成交后已请求取消。"""
        self.buy_calls += 1
        self.last_notional_usd = notional_usd
        self.notionals.append(notional_usd)
        return OrderResult("order-1", symbol, "BUY", 1.0, current_price, "CANCEL_REQUESTED", "not filled; cancel requested")

    def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
        """模拟自动监控提交 BUY LIMIT 后超时撤单。"""
        self.buy_calls += 1
        self.last_notional_usd = notional_usd
        self.last_limit_price = limit_price
        self.notionals.append(notional_usd)
        return OrderResult("order-1", symbol, "BUY", 1.0, limit_price, "CANCEL_REQUESTED", "not filled; cancel requested")


class RecordingBuyBroker(CancelingBuyBroker):
    def place_market_buy(self, symbol, notional_usd, current_price, reason):
        """只记录买入尝试，不返回成交。"""
        self.buy_calls += 1
        self.last_notional_usd = notional_usd
        self.notionals.append(notional_usd)
        return OrderResult("order-1", symbol, "BUY", 1.0, current_price, "FILLED", "filled")

    def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
        """记录自动监控使用的买点限价。"""
        self.buy_calls += 1
        self.last_notional_usd = notional_usd
        self.last_limit_price = limit_price
        self.notionals.append(notional_usd)
        return OrderResult("order-1", symbol, "BUY", 1.0, limit_price, "FILLED", "filled")


class InsufficientBuyingPowerThenBuyingBroker(RecordingBuyBroker):
    def __init__(self):
        super().__init__()

    def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
        self.buy_calls += 1
        self.last_notional_usd = notional_usd
        self.last_limit_price = limit_price
        self.notionals.append(notional_usd)
        if self.buy_calls == 1:
            return OrderResult("", symbol, "BUY", 1.0, limit_price, "REJECTED", "insufficient buying power | code=40310000 buying_power=3200")
        return OrderResult("order-2", symbol, "BUY", 1.0, limit_price, "FILLED", "filled")


class ChangingCashBroker(RecordingBuyBroker):
    def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
        """第一笔下单后改小 cash，用来确认本轮金额没有重算。"""
        result = super().place_limit_buy(symbol, notional_usd, limit_price, reason)
        self.account.cash = "2000"
        return result


class RecordingSellBroker:
    def __init__(self):
        """记录卖出方法，确认止损走限价单而不是市价单。"""
        self.positions = {"US.TEST": Position("US.TEST", 10.0, 10.0, "alpaca", source="alpaca-paper")}
        self.sell_calls = []

    def source_name(self):
        return "alpaca-paper"

    def get_positions(self):
        return self.positions

    def get_open_sell_order_symbols(self):
        """默认没有开放卖单；具体测试可覆盖该返回值。"""
        return set()

    def place_limit_sell(self, symbol, quantity, limit_price, reason):
        self.sell_calls.append(("limit_sell", symbol, quantity, limit_price, reason))
        self.positions.pop(symbol, None)
        return OrderResult("limit-sell-1", symbol, "SELL", quantity, limit_price, "FILLED", "filled")

    def place_market_sell(self, symbol, quantity, current_price, reason):
        self.sell_calls.append(("market_sell", symbol, quantity, current_price, reason))
        self.positions.pop(symbol, None)
        return OrderResult("market-sell-1", symbol, "SELL", quantity, current_price, "FILLED", "filled")


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


class OpenBuyOrderBroker(RecordingBuyBroker):
    def get_open_buy_order_symbols(self):
        """模拟 Alpaca 已有开放买单，自动监控本轮不能继续开新仓。"""
        return {"US.TEST"}


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
        buy_notional_usd=BUY_NOTIONAL_USD,
        max_daily_buys=2,
        max_symbol_order_errors=3,
        stop_loss_pct=-0.10,
        stop_loss_limit_pct=-0.08,
        take_profit_half_pct=0.10,
        take_profit_sell_fraction=0.50,
        take_profit_remainder_stop_pct=None,
        close_liquidation_start=time(15, 55),
        close_liquidation_end=time(16, 0),
        regular_poll_seconds=10,
        idle_poll_seconds=1200,
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
        trade_notify_mode="local",
        cloud_notify_webhook_url="",
        cloud_notify_webhook_secret="",
        openclaw_telegram_target="",
        openclaw_gateway_port=18789,
        watchlist_chart_lan_host="",
        watchlist_chart_lan_port=8766,
        strategy_name=MA5_DIP_STRATEGY_NAME,
    )


def make_screen_bars(symbol="TEST", signal_day=date(2026, 1, 20), passes=True):
    """生成 watchlist 选股测试用的 20 根日线。"""
    bars = []
    for index in range(19):
        bars.append(DailyBar(symbol, date(2026, 1, index + 1), float(index + 1), float(index + 1), float(index + 1), float(index + 1)))
    bars[-4:] = [
        DailyBar(symbol, date(2026, 1, 16), 18.0, 18.0, 18.0, 18.0),
        DailyBar(symbol, date(2026, 1, 17), 18.0, 18.0, 18.0, 18.0),
        DailyBar(symbol, date(2026, 1, 18), 18.0, 18.0, 18.0, 18.0),
        DailyBar(symbol, date(2026, 1, 19), 21.0, 21.0, 21.0, 21.0),
    ]

    if passes:
        bars.append(DailyBar(symbol, signal_day, 20.0, 26.5, 20.0, 25.0))
    else:
        bars.append(DailyBar(symbol, signal_day, 20.0, 21.0, 19.0, 20.0))
    return bars


def make_ma5_dip_close_under_15_pct_bars(symbol="CLOSE_NEAR_MA5", signal_day=date(2026, 1, 20)):
    """涨幅达标且收盘在 MA5 上方，但距离不足 15 个点。"""
    closes = [8.0] * 15 + [10.0, 10.0, 10.0, 9.0]
    bars = [
        DailyBar(symbol, date(2026, 1, index + 1), close, close, close, close)
        for index, close in enumerate(closes)
    ]
    bars.append(DailyBar(symbol, signal_day, 9.0, 11.2, 8.8, 11.0))
    return bars


def make_weak_ma_order_bars(symbol="WEAK_MA_ORDER", signal_day=date(2026, 1, 20)):
    closes = [8.0] * 10 + [10.0] * 5 + [4.0] * 4
    bars = [
        DailyBar(symbol, date(2026, 1, index + 1), close, close, close, close)
        for index, close in enumerate(closes)
    ]
    bars.append(DailyBar(symbol, signal_day, 4.8, 6.4, 4.7, 6.0))
    return bars


def make_minute_bar(symbol="TEST", hour=9, minute=30, open=10.0, high=10.0, low=10.0, close=10.0):
    """生成盘后策略测试用 1m bar。"""
    timestamp = datetime(2026, 5, 28, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    return MinuteBar(symbol, timestamp, open, high, low, close)


def make_buy_snapshot(current=12.3, closes=None, today_open=None, opens=None):
    closes = closes or [10.0, 10.0, 10.0, 13.0]
    today_open = closes[-1] if today_open is None else today_open
    return make_snapshot(current=current, closes=closes, today_open=today_open, opens=opens or [10.0, 10.0, 10.0, 10.0])


def make_buy_snapshot_for_symbol(symbol="US.TEST", current=10.65):
    return make_snapshot(symbol, current=current, closes=[10.0, 10.0, 10.0, 13.0], today_open=13.0, opens=[10.0, 10.0, 10.0, 10.0])


class StrategyTests(unittest.TestCase):
    def setUp(self):
        strategy_module.set_active_strategy("ma5_dip")

    def test_buy_when_signal_gain_15_to_40_adds_half_percent(self):
        signal = evaluate_buy(make_buy_snapshot(current=10.65, closes=[10.0, 10.0, 10.0, 13.0]))
        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["today_ma5"], 10.73)
        self.assertAlmostEqual(signal.diagnostics["signal_day_gain_pct"], 0.30)
        self.assertLessEqual(signal.diagnostics["today_current_gain_pct"], -0.12)
        self.assertAlmostEqual(signal.diagnostics["base_buy_point_pct"], 0.005)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point_pct"], 0.005)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point"], 10.78365)

    def test_hold_when_signal_gain_15_to_40_price_is_above_trigger_band(self):
        signal = evaluate_buy(make_buy_snapshot(current=11.3, closes=[10.0, 10.0, 10.0, 13.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("触发上沿", signal.reason)
        self.assertAlmostEqual(signal.diagnostics["buy_trigger_distance_pct"], 0.03)

    def test_buy_when_current_price_is_within_trigger_band_above_buy_point(self):
        signal = evaluate_buy(make_buy_snapshot(current=11.9, closes=[10.0, 10.0, 10.0, 15.0]))

        self.assertEqual(signal.action, "BUY")
        self.assertGreater(signal.diagnostics["current_vs_buy_point_pct"], 0)
        self.assertLessEqual(signal.diagnostics["current_vs_buy_point_pct"], 0.03)
        self.assertAlmostEqual(signal.diagnostics["buy_trigger_distance_pct"], 0.03)
        self.assertIn("3.00% 内", signal.reason)

    def test_buy_when_signal_gain_40_to_100_adds_three_percent(self):
        signal = evaluate_buy(make_buy_snapshot(current=11.5, closes=[10.0, 10.0, 10.0, 15.0]))

        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["base_buy_point_pct"], 0.03)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point_pct"], 0.03)
        self.assertAlmostEqual(signal.diagnostics["final_buy_point"], 11.639, places=3)

    def test_buy_when_signal_gain_above_100_adds_four_percent(self):
        signal = evaluate_buy(make_buy_snapshot(current=13.4, closes=[10.0, 10.0, 10.0, 22.0]))

        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["base_buy_point_pct"], 0.04)

    def test_open_gain_5_to_15_adds_one_percent(self):
        signal = evaluate_buy(make_snapshot(current=10.6, closes=[10.0, 10.0, 10.0, 13.0], today_open=13.65))

        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.diagnostics["today_open_gain_pct"], 0.05)
        self.assertAlmostEqual(signal.diagnostics["open_bonus_pct"], 0.01)

    def test_open_gain_above_15_adds_two_percent(self):
        signal = evaluate_buy(make_snapshot(current=10.6, closes=[10.0, 10.0, 10.0, 13.0], today_open=15.0))

        self.assertEqual(signal.action, "BUY")
        self.assertGreater(signal.diagnostics["today_open_gain_pct"], 0.15)
        self.assertAlmostEqual(signal.diagnostics["open_bonus_pct"], 0.02)

    def test_hold_all_day_when_today_open_is_ten_percent_below_open_ma5(self):
        signal = evaluate_buy(make_snapshot(current=10.7, today_open=8.7, opens=[10.0, 10.0, 10.0, 10.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("10.00%", signal.reason)
        self.assertLessEqual(signal.diagnostics["today_open_vs_open_ma5_pct"], -0.10)
        self.assertAlmostEqual(signal.diagnostics["today_open_ma5"], 9.74)

    def test_hold_when_today_open_drops_forty_percent(self):
        signal = evaluate_buy(make_snapshot(current=10.7, today_open=7.7))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("40.00%", signal.reason)
        self.assertLessEqual(signal.diagnostics["today_open_gain_pct"], -0.40)

    def test_hold_when_today_open_is_below_dynamic_ma5(self):
        signal = evaluate_buy(make_snapshot(current=10.7, today_open=10.6, opens=[10.0, 10.0, 10.0, 10.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("MA5", signal.reason)
        self.assertLess(signal.diagnostics["today_open_vs_today_ma5_pct"], 0)

    def test_hold_and_exclude_day_when_touch_ma5_before_12_percent_drop(self):
        signal = evaluate_buy(make_buy_snapshot(current=11.0, closes=[12.0, 12.0, 10.0, 12.1]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("12.00%", signal.reason)
        self.assertGreater(signal.diagnostics["today_current_gain_pct"], -0.12)
        self.assertEqual(signal.diagnostics["daily_buy_exclusion"], "ma5_touch_without_required_drop")

    def test_ma5_dip_configure_updates_runtime_buy_threshold(self):
        original = strategy_ma5_dip.MAX_BUY_TODAY_CURRENT_GAIN_PCT
        try:
            strategy_ma5_dip.configure(max_buy_today_current_gain_pct=-0.09)

            signal = evaluate_buy(make_buy_snapshot(current=11.05, closes=[12.0, 12.0, 10.0, 12.1]))

            self.assertEqual(signal.action, "HOLD")
            self.assertIn("9.00%", signal.reason)
            self.assertAlmostEqual(signal.diagnostics["max_buy_today_current_gain_pct"], -0.09)
        finally:
            strategy_ma5_dip.configure(max_buy_today_current_gain_pct=original)

    def test_hold_when_buy_zone_reached_before_12_percent_drop(self):
        signal = evaluate_buy(make_buy_snapshot(current=11.6, closes=[12.0, 12.0, 10.0, 12.1]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("12.00%", signal.reason)
        self.assertGreater(signal.diagnostics["today_current_gain_pct"], -0.12)

    def test_missing_today_open_fails_closed_even_when_other_buy_conditions_match(self):
        signal = evaluate_buy(make_snapshot(current=11.0, closes=[10.0, 10.0, 10.0, 13.0], today_open=0.0))

        self.assertEqual(signal.action, "HOLD")
        self.assertAlmostEqual(signal.diagnostics["open_bonus_pct"], 0.0)
        self.assertIn("失败关闭", signal.reason)

    def test_hold_when_signal_day_gain_is_below_15_percent(self):
        signal = evaluate_buy(make_buy_snapshot(current=9.8, closes=[10.0, 10.0, 10.0, 11.0]))

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("15.00%", signal.reason)
        self.assertIn("动作：观察不买", signal.reason)
        self.assertIn("无有效分段买点", signal.reason)
        self.assertIn("参考动态MA5价", signal.reason)

    def test_hold_when_current_price_is_at_today_ma5(self):
        signal = evaluate_buy(make_snapshot(current=10.0, closes=[10.0, 10.0, 10.0, 10.0]))
        self.assertEqual(signal.action, "HOLD")

    def test_sell_on_10_percent_loss_with_8_percent_limit_price(self):
        position = Position("US.TEST", 10, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 12, 0)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=9.0), now, settings)
        self.assertEqual(signal.action, "SELL_ALL")
        self.assertIn("10.00%", signal.reason)
        self.assertIn("8.00%", signal.reason)
        self.assertEqual(signal.diagnostics["stop_loss_limit_price"], 9.2)

    def test_hold_when_loss_is_less_than_stop_loss(self):
        position = Position("US.TEST", 10, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 12, 0)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=9.3), now, settings)
        self.assertEqual(signal.action, "HOLD")

    def test_sell_half_on_10_percent_gain(self):
        position = Position("US.TEST", 11, 10.0, "2026-05-28T09:35:00")
        now = datetime(2026, 5, 28, 12, 0)
        settings = make_settings(Path("."))
        signal = evaluate_sell(position, make_snapshot(current=11.0), now, settings)

        self.assertEqual(signal.action, "SELL_HALF")
        self.assertAlmostEqual(signal.quantity, 5.5)
        self.assertEqual(signal.diagnostics["sell_rule"], "take_profit_half")
        self.assertIn("10.00%", signal.reason)

    def test_take_profit_remainder_stop_is_disabled(self):
        position = Position("US.TEST", 5, 10.0, "2026-05-28T09:35:00")
        settings = make_settings(Path("."))
        signal = evaluate_take_profit_remainder_stop(position, make_snapshot(current=10.4), settings)

        self.assertEqual(signal.action, "HOLD")
        self.assertEqual(signal.diagnostics["sell_rule"], "hold")
        self.assertIn("不启用", signal.reason)

    def test_sell_near_regular_close(self):
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

        response = execute_trade_command("帮我买3000刀的NTAP，购买价格为当前价*0.95", settings=settings, broker=broker, market_data=market_data)

        self.assertTrue(response.ok)
        self.assertEqual(response.command.limit_price, 0.0)
        self.assertEqual(response.command.limit_price_multiplier, 0.95)
        self.assertEqual(market_data.calls, ["US.NTAP"])
        self.assertEqual(broker.calls[0][:4], ("limit_buy", "US.NTAP", 3000.0, 190.0))
        self.assertIn("动态限价", broker.calls[0][4])
        self.assertTrue(broker.calls[0][5])

    def test_execute_buy_market_requires_explicit_market_phrase(self):
        """没有限价时，必须明确写市价，才会走 market buy。"""
        broker = FakeOpenClawCommandBroker()
        settings = make_settings(Path("."))
        market_data = FakeMarketData({"US.NTAP": make_snapshot("US.NTAP", current=200.0)})

        response = execute_trade_command("帮我买3000刀的NTAP，市价买入", settings=settings, broker=broker, market_data=market_data)

        self.assertTrue(response.ok)
        self.assertEqual(broker.calls[0][:4], ("market_buy", "US.NTAP", 3000.0, 200.0))
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
            parse_trade_command("帮我买3000刀的NTAP")

        try:
            parse_trade_command("帮我买3000刀的NTAP")
        except ValueError as exc:
            message = str(exc)
        self.assertIn("帮我买3000刀的NTAP，购买价格固定160", message)
        self.assertIn("帮我买3000刀的NTAP，购买价格为当前价*0.95", message)
        self.assertIn("帮我买3000刀的NTAP，市价买入", message)

    def test_openclaw_response_prints_reject_reason(self):
        """OpenClaw 回复里要直接显示失败原因，不让 agent 自己猜。"""

        class RejectBroker(FakeOpenClawCommandBroker):
            def place_limit_buy(self, symbol, notional_usd, limit_price, reason, *, skip_time_validation=False):
                self.calls.append(("limit_buy", symbol, notional_usd, limit_price, reason, skip_time_validation))
                return OrderResult("", symbol, "BUY", 1.0, limit_price, "REJECTED", "Alpaca 拒单: buying power 不足")

        response = execute_trade_command("帮我买3000刀的AAPL，购买价格固定211", settings=make_settings(Path(".")), broker=RejectBroker())
        text = render_trade_command_response(response)

        self.assertIn("【Agent 交易指令｜买单】", text)
        self.assertIn("总览：❌ 请求失败或需要处理", text)
        self.assertIn("账户：Alpaca PAPER（模拟账户）", text)
        self.assertIn("【买单｜下单失败】", text)
        self.assertIn("失败原因\n- Alpaca 拒单: buying power 不足", text)

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


class TradingCalendarTests(unittest.TestCase):
    def test_offline_calendar_accepts_regular_trading_day(self):
        """普通工作日且不是节假日时，允许自动任务运行。"""
        decision = offline_trading_day_decision(date(2026, 6, 22))

        self.assertTrue(decision.is_trading_day)
        self.assertEqual(decision.reason, "Weekday and not a standard US equity market holiday")

    def test_offline_calendar_rejects_weekend(self):
        """周末不运行 22:00/00:50/04:00 自动任务。"""
        decision = offline_trading_day_decision(date(2026, 6, 20))

        self.assertFalse(decision.is_trading_day)
        self.assertEqual(decision.reason, "Weekend")

    def test_offline_calendar_rejects_standard_us_market_holidays(self):
        """节假日表能识别常见 NYSE/Nasdaq 休市日。"""
        self.assertEqual(us_equity_holiday_name(date(2026, 6, 19)), "Juneteenth")
        self.assertEqual(us_equity_holiday_name(date(2026, 7, 3)), "Independence Day")
        self.assertEqual(us_equity_holiday_name(date(2026, 4, 3)), "Good Friday")


class ServiceTests(unittest.TestCase):
    def test_watchlist_text_normalizes_and_deduplicates_symbols(self):
        """watchlist 文本读取会标准化、去重并忽略注释。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_codes.txt"
            path.write_text("# comment\nAAPL\nUS.AAPL\nTSLA, note\n", encoding="utf-8")
            self.assertEqual(read_watch_codes(path), ["US.AAPL", "US.TSLA"])

    def test_market_data_safety_error_blocks_intraday_buy_order(self):
        """公司行动口径异常必须在策略和券商提交前阻断买入。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = SafetyBlockingMarketData()
            broker = RecordingBuyBroker()

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["errors"], 1)
            self.assertEqual(summary["buy"], 0)
            self.assertEqual(broker.buy_calls, 0)
            self.assertEqual(market_data.calls, ["US.TEST"])
            self.assertEqual(market_data.purposes, [SNAPSHOT_PURPOSE_AUTOMATIC])

    def test_market_data_safety_error_blocks_intraday_sell_order(self):
        """异常实时价不得触发止盈、止损或任何其他自动卖单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("", encoding="utf-8")
            market_data = SafetyBlockingMarketData("unit stale realtime price")
            broker = RecordingSellBroker()

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["errors"], 1)
            self.assertEqual(summary["sell"], 0)
            self.assertEqual(broker.sell_calls, [])
            self.assertEqual(market_data.calls, ["US.TEST"])
            self.assertEqual(market_data.purposes, [SNAPSHOT_PURPOSE_AUTOMATIC])

    def test_run_once_exposes_the_nine_core_trade_phases_in_order(self):
        """核心逐股循环应直接按买入、卖出、撤单各三阶段执行。"""
        trading_round = type(
            "FakeTradingRound",
            (),
            {
                "symbols": ["US.TEST"],
                "positions": {},
            },
        )()
        events: list[str] = []
        phases = [
            "check_buy",
            "execute_buy",
            "notify_buy",
            "check_sell",
            "execute_sell",
            "notify_sell",
            "check_cancel",
            "execute_cancel",
            "notify_cancel",
        ]

        def record_phase(name):
            def recorder(_trading_round, cycle):
                events.append(name)
                if name == "notify_cancel":
                    cycle.notified = True

            return recorder

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "alpaca_ma5_service.service.prepare_trading_round",
                    return_value=trading_round,
                )
            )
            stack.enter_context(patch("alpaca_ma5_service.service.start_trading_round"))
            for phase in phases:
                stack.enter_context(
                    patch(
                        f"alpaca_ma5_service.service.{phase}",
                        side_effect=record_phase(phase),
                    )
                )
            stack.enter_context(
                patch(
                    "alpaca_ma5_service.service.finish_trading_round",
                    return_value={"watch": 1, "buy": 0, "sell": 0, "hold": 1, "errors": 0},
                )
            )
            stack.enter_context(patch("alpaca_ma5_service.service.close_trading_round"))

            run_once(settings=object())

        self.assertEqual(events, phases)

    def test_run_once_empty_watchlist_without_positions_does_not_build_market_data(self):
        """watch_codes 为空且没有持仓时，不启动行情源。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.write_text("", encoding="utf-8")
            broker = RecordingBuyBroker()

            with patch("alpaca_ma5_service.service.build_market_data", side_effect=AssertionError("market data should not start")):
                summary = run_once(settings, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary, {"watch": 0, "buy": 0, "sell": 0, "hold": 0, "errors": 0})

    def test_run_once_prints_one_compact_table_for_all_symbols(self):
        """单轮监控输出应汇总成一张表，并列出本轮所有股票。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData(
                {
                    "US.TEST": make_snapshot("US.TEST", current=10.6),
                    "US.NEXT": make_snapshot("US.NEXT", current=16.6, closes=[13.35, 12.82, 13.81, 16.62]),
                }
            )
            broker = RecordingBuyBroker()
            broker.account = type("FakeAccount", (), {"cash": "7000"})()
            buffer = StringIO()

            with redirect_stdout(buffer):
                summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            output = buffer.getvalue()
            self.assertEqual(summary["watch"], 2)
            self.assertIn("本轮股票明细", output)
            self.assertIn("代码", output)
            self.assertIn("当前价", output)
            self.assertIn("US.TEST", output)
            self.assertIn("US.NEXT", output)
            self.assertNotIn("本轮详细原因", output)
            self.assertNotIn("…", output)
            self.assertNotIn("行情：", output)
            self.assertNotIn("均线：", output)
            self.assertNotIn("买点输入：", output)

    def test_print_monitor_table_keeps_full_reason_in_reason_column(self):
        """原因列展示完整原因，不再另起详细原因区块或省略字符。"""
        full_reason = "完整原因：当前价高于触发上沿；动作：观察不买；这个原因不能被省略"
        buffer = StringIO()

        with redirect_stdout(buffer):
            print_monitor_table(
                [
                    MonitorTableRow(
                        symbol="US.TEST",
                        action="观察",
                        has_market_data=True,
                        current_price=10.0,
                        today_open=9.8,
                        today_ma5=9.5,
                        today_open_ma5=9.4,
                        signal_gain_pct=0.18,
                        current_gain_pct=-0.12,
                        order_price=9.55,
                        reason=full_reason,
                    )
                ]
            )

        output = buffer.getvalue()
        self.assertIn(full_reason, output)
        self.assertNotIn("本轮详细原因", output)
        self.assertNotIn("…", output)

    def test_run_once_buys_only_symbols_from_file(self):
        """单轮监控只处理 watch_codes.txt 里的代码。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")})
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
        """当前价在买点上方 3% 内时，自动监控用买点价提交 BUY LIMIT。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")})
            broker = RecordingBuyBroker()
            broker.account = type("FakeAccount", (), {"cash": "7000"})()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 1)
            self.assertEqual(broker.buy_calls, 1)
            self.assertEqual(broker.last_notional_usd, 1500.0)
            self.assertAlmostEqual(broker.last_limit_price, 10.78365)

    def test_run_once_ladder_dry_run_splits_only_first_half_take_profit(self):
        """三档 profile 只拆首次半仓止盈额度，不触碰 Alpaca submit_order。"""
        real_submit = self.enterContext(patch.object(AlpacaStockBroker, "_submit_order"))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                make_settings(root),
                strategy_name=MA5_DIP_LADDER_STRATEGY_NAME,
                strategy_profile_name=MA5_DIP_LADDER_STRATEGY_NAME,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                absolute_stop_loss_pct=-0.10,
            )
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            prices = [10.65, 10.5435, 10.65]

            for minute, price in enumerate(prices):
                summary = run_once(
                    settings,
                    market_data=FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST", current=price)}),
                    broker=broker,
                    now=datetime(2026, 5, 28, 10, minute),
                )
                self.assertEqual(summary["buy"], 1)

            full_position = broker.get_positions()["US.TEST"]
            full_quantity = full_position.quantity
            take_profit_price = full_position.avg_price * 1.10
            sell_summary = run_once(
                settings,
                market_data=FakeMarketData(
                    {"US.TEST": make_buy_snapshot_for_symbol("US.TEST", current=take_profit_price)}
                ),
                broker=broker,
                now=datetime(2026, 5, 28, 10, 3),
            )
            remaining_quantity = broker.get_positions()["US.TEST"].quantity
            take_profit_target = int(full_quantity * 0.50)
            base, remainder = divmod(take_profit_target, 3)
            expected_first_leg = base + (1 if remainder >= 1 else 0)

            self.assertEqual(sell_summary["sell"], 1)
            self.assertEqual(full_quantity - remaining_quantity, expected_first_leg)
            state = json.loads((settings.output_dir / "ladder_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["plans"]["US.TEST"]["sell_stage"], 1)
            real_submit.assert_not_called()

    def test_ladder_closed_plan_from_prior_day_allows_new_session_plan(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                make_settings(root),
                strategy_name=MA5_DIP_LADDER_STRATEGY_NAME,
                strategy_profile_name=MA5_DIP_LADDER_STRATEGY_NAME,
            )
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            store = LadderStateStore(settings.output_dir)
            old_now = datetime(2026, 5, 27, 10, 0)
            old_plan = store.create(
                "US.TEST",
                old_now.date(),
                1500.0,
                10.65,
                settings.buy_ladder_offsets,
                settings.sell_ladder_offsets,
                old_now,
            )
            old_plan.buy_closed = True
            old_plan.status = "closed"
            store.save(old_now)

            summary = run_once(
                settings,
                market_data=FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")}),
                broker=DryRunStockBroker(settings),
                now=datetime(2026, 5, 28, 10, 0),
            )

            current = LadderStateStore(settings.output_dir).get("US.TEST")
            self.assertEqual(summary["buy"], 1)
            self.assertEqual(current.session_date, "2026-05-28")
            self.assertGreater(current.filled_quantity, 0)

    def test_run_once_scales_single_buy_when_cash_is_below_fixed_notional(self):
        """现金低于单笔上限时，自动监控按可用现金缩小本轮金额。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")})
            broker = RecordingBuyBroker()
            broker.account = type("FakeAccount", (), {"cash": "1200"})()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 1)
            self.assertEqual(summary["hold"], 0)
            self.assertEqual(broker.buy_calls, 1)
            self.assertEqual(broker.last_notional_usd, 1200.0)

    def test_build_settings_accepts_buy_stock_count_parameter(self):
        """monitor_ma5_forever 入口传入买入股票数后，会改成本轮每日买入上限。"""
        self.assertEqual(build_settings(buy_stock_count=1).max_daily_buys, 1)
        self.assertEqual(build_settings(buy_stock_count=2).max_daily_buys, 2)
        self.assertEqual(build_settings(buy_stock_count=3).max_daily_buys, 3)
        with self.assertRaises(ValueError):
            build_settings(buy_stock_count=0)
        with self.assertRaises(ValueError):
            build_settings(buy_stock_count=4)

    def test_build_settings_accepts_buy_notional_usd_parameter(self):
        self.assertEqual(build_settings(buy_notional_usd=500).buy_notional_usd, 500.0)
        self.assertEqual(build_settings(buy_notional_usd=1500.5).buy_notional_usd, 1500.5)
        with self.assertRaises(ValueError):
            build_settings(buy_notional_usd=0)
        with self.assertRaises(ValueError):
            build_settings(buy_notional_usd=-1)
        with self.assertRaises(ValueError):
            build_settings(buy_notional_usd=float("inf"))
        with self.assertRaises(ValueError):
            build_settings(buy_notional_usd=True)

    def test_build_settings_accepts_monitor_runtime_overrides(self):
        settings = build_settings(
            strategy_name="ma5_dip",
            buy_stock_count=3,
            buy_notional_usd=1500.0,
            max_symbol_order_errors=2,
            stop_loss_pct=-0.12,
            stop_loss_limit_pct=-0.09,
            take_profit_half_pct=0.08,
            take_profit_sell_fraction=0.25,
            take_profit_remainder_stop_pct=None,
            close_liquidation_start=time(15, 50),
            close_liquidation_end=time(15, 59),
            regular_poll_seconds=11,
            idle_poll_seconds=333,
            allow_fractional_shares=True,
            extended_hours_orders_enabled=False,
            extended_hours_limit_buffer_pct=0.004,
            order_cancel_after_seconds=77,
            order_status_poll_seconds=3,
            realtime_price_source="alpaca",
            trade_notify_mode="cloud",
            buy_ladder_offsets=(0.0, -0.02, -0.04),
            sell_ladder_offsets=(0.0, 0.02, 0.04),
            absolute_stop_loss_pct=-0.20,
        )

        self.assertEqual(settings.max_daily_buys, 3)
        self.assertEqual(settings.buy_notional_usd, 1500.0)
        self.assertEqual(settings.max_symbol_order_errors, 2)
        self.assertEqual(settings.stop_loss_pct, -0.12)
        self.assertEqual(settings.stop_loss_limit_pct, -0.09)
        self.assertEqual(settings.take_profit_half_pct, 0.08)
        self.assertEqual(settings.take_profit_sell_fraction, 0.25)
        self.assertIsNone(settings.take_profit_remainder_stop_pct)
        self.assertEqual(settings.close_liquidation_start, time(15, 50))
        self.assertEqual(settings.close_liquidation_end, time(15, 59))
        self.assertEqual(settings.regular_poll_seconds, 11)
        self.assertEqual(settings.idle_poll_seconds, 333)
        self.assertTrue(settings.allow_fractional_shares)
        self.assertFalse(settings.extended_hours_orders_enabled)
        self.assertEqual(settings.extended_hours_limit_buffer_pct, 0.004)
        self.assertEqual(settings.order_cancel_after_seconds, 77)
        self.assertEqual(settings.order_status_poll_seconds, 3)
        self.assertEqual(settings.realtime_price_source, "alpaca")
        self.assertEqual(settings.trade_notify_mode, "cloud")
        self.assertEqual(settings.buy_ladder_offsets, (0.0, -0.02, -0.04))
        self.assertEqual(settings.sell_ladder_offsets, (0.0, 0.02, 0.04))
        self.assertEqual(settings.absolute_stop_loss_pct, -0.20)

    def test_build_settings_rejects_invalid_ladder_parameters(self):
        with self.assertRaises(ValueError):
            build_settings(buy_ladder_offsets=(0.0, -0.02, -0.01))
        with self.assertRaises(ValueError):
            build_settings(sell_ladder_offsets=(0.0, 0.02, 0.01))
        with self.assertRaises(ValueError):
            build_settings(absolute_stop_loss_pct=0.15)

    def test_build_settings_defaults_to_old_ma5_dip_strategy(self):
        settings = build_settings()

        self.assertEqual(settings.strategy_name, "ma5_dip")
        self.assertEqual(settings.max_daily_buys, 2)
        self.assertEqual(settings.buy_notional_usd, 1500.0)

    def test_old_ma5_dip_watchlist_does_not_require_ma_order(self):
        original = strategy_ma5_dip.MIN_SIGNAL_DAY_GAIN_PCT
        try:
            strategy_ma5_dip.configure(min_signal_day_gain_pct=0.12)

            rules = watchlist_screen_rules("ma5_dip")
            candidates = screen_candidates(
                {"WEAK_MA_ORDER": make_weak_ma_order_bars()},
                datetime(2026, 1, 21, 10, 0),
                rules=rules,
            )

            self.assertFalse(rules.require_ma5_gt_ma10_gt_ma20)
            self.assertEqual(rules.min_signal_gain_pct, 0.12)
            self.assertEqual([candidate.symbol for candidate in candidates], ["WEAK_MA_ORDER"])
        finally:
            strategy_ma5_dip.configure(min_signal_day_gain_pct=original)

    def test_ma5_dip_watchlist_requires_close_at_least_15_pct_above_ma5(self):
        rules = watchlist_screen_rules("ma5_dip")
        candidates = screen_candidates(
            {"CLOSE_NEAR_MA5": make_ma5_dip_close_under_15_pct_bars()},
            datetime(2026, 1, 21, 10, 0),
            rules=rules,
        )
        candidate = WatchCandidate(
            "CLOSE_NEAR_MA5",
            date(2026, 1, 20),
            0.22,
            0.02,
            10.0,
            9.0,
            8.0,
            9.0,
            11.2,
            11.0,
        )
        candidate_at_limit = WatchCandidate(
            "CLOSE_AT_LIMIT",
            date(2026, 1, 20),
            0.22,
            0.0,
            10.0,
            9.0,
            8.0,
            9.0,
            11.5,
            11.5,
        )

        self.assertEqual(rules.min_close_to_ma5_ratio, 1.15)
        self.assertTrue(rules.include_min_close_to_ma5_ratio)
        self.assertEqual(candidates, [])
        with self.assertRaisesRegex(RuntimeError, "close/MA5>=1.15"):
            validate_candidates([candidate], rules=rules)
        validate_candidates([candidate_at_limit], rules=rules)

    def test_run_once_splits_cash_across_remaining_buy_slots(self):
        """买入股票数设为 3 时，本轮按剩余名额均分可用现金。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(**{**make_settings(root).__dict__, "max_daily_buys": 3})
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\nUS.THIRD\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_buy_snapshot_for_symbol("US.TEST"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
                "US.THIRD": make_buy_snapshot_for_symbol("US.THIRD"),
            })
            broker = RecordingBuyBroker()
            broker.account = type("FakeAccount", (), {"cash": "3000"})()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 3)
            self.assertEqual(broker.buy_calls, 3)
            self.assertEqual(broker.notionals, [1000.0, 1000.0, 1000.0])
            self.assertEqual(market_data.calls, ["US.TEST", "US.NEXT", "US.THIRD"])

    def test_run_once_rejected_buy_does_not_retry_with_different_notional(self):
        """本轮金额固定后，拒单不会再换一个金额重试。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")})
            broker = InsufficientBuyingPowerThenBuyingBroker()
            broker.account = type("FakeAccount", (), {"cash": "7000"})()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.notionals, [1500.0])

    def test_run_once_reuses_run_notional_after_cash_changes(self):
        """第一笔下单后 cash 变小，后续订单仍复用本轮固定金额。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\nUS.THIRD\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_buy_snapshot_for_symbol("US.TEST"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
                "US.THIRD": make_buy_snapshot_for_symbol("US.THIRD"),
            })
            broker = ChangingCashBroker()
            broker.account = type("FakeAccount", (), {"cash": "2400"})()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 2)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 2)
            self.assertEqual(broker.notionals, [1200.0, 1200.0])
            self.assertEqual(market_data.calls, ["US.TEST", "US.NEXT"])

    def test_run_once_does_not_exclude_symbol_after_non_buy_hold(self):
        """未触发每日排除的普通 HOLD 不妨碍同日后续有效买点。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            first_market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST", current=12.8)})
            second_market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")})
            broker = RecordingBuyBroker()
            broker.account = type("FakeAccount", (), {"cash": "7000"})()
            now = datetime(2026, 5, 28, 10, 0)

            first = run_once(settings, market_data=first_market_data, broker=broker, now=now)
            second = run_once(settings, market_data=second_market_data, broker=broker, now=now)

            self.assertEqual(first["buy"], 0)
            self.assertEqual(first["hold"], 1)
            self.assertFalse(is_symbol_daily_buy_excluded(settings.output_dir, "US.TEST", now.date()))
            self.assertEqual(second["buy"], 1)
            self.assertEqual(second["hold"], 0)
            self.assertEqual(broker.buy_calls, 1)
            self.assertEqual(second_market_data.calls, ["US.TEST"])

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
                "US.TEST": make_buy_snapshot_for_symbol("US.TEST"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
            })
            broker = CancelingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 2)
            self.assertEqual(broker.buy_calls, 1)

    def test_run_once_cancel_phase_handles_adapter_returning_submitted_order(self):
        """自定义 Broker 直接返回 SUBMITTED 时，核心撤单阶段按订单号兜底取消。"""

        class SubmittedBuyBroker(RecordingBuyBroker):
            def __init__(self):
                super().__init__()
                self.cancel_calls: list[tuple[str, str]] = []

            def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
                self.buy_calls += 1
                return OrderResult(
                    "submitted-1",
                    symbol,
                    "BUY",
                    1.0,
                    limit_price,
                    "SUBMITTED",
                    "adapter returned before terminal wait",
                )

            def cancel_order(self, order_id, reason):
                self.cancel_calls.append((order_id, reason))
                return OrderResult(
                    order_id,
                    "US.TEST",
                    "BUY",
                    1.0,
                    9.8,
                    "CANCELED",
                    "fallback cancel confirmed",
                )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")})
            broker = SubmittedBuyBroker()

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 1)
            self.assertEqual(broker.cancel_calls[0][0], "submitted-1")

    def test_run_once_does_not_immediately_cancel_broker_managed_order(self):
        class ManagedSubmittedBuyBroker(RecordingBuyBroker):
            manages_pending_orders = True

            def __init__(self):
                super().__init__()
                self.cancel_calls = 0

            def place_limit_buy_nonblocking(
                self,
                symbol,
                notional_usd,
                limit_price,
                reason,
                *,
                strategy_action="",
            ):
                self.buy_calls += 1
                return OrderResult(
                    "managed-1",
                    symbol,
                    "BUY",
                    1.0,
                    limit_price,
                    "SUBMITTED",
                    f"managed {strategy_action}",
                )

            def cancel_order(self, order_id, reason):
                self.cancel_calls += 1
                raise AssertionError("managed order must not be canceled in the submit round")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = ManagedSubmittedBuyBroker()

            summary = run_once(
                settings,
                market_data=FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")}),
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual((summary["buy"], summary["hold"]), (0, 1))
            self.assertEqual(broker.buy_calls, 1)
            self.assertEqual(broker.cancel_calls, 0)

    def test_run_once_does_not_trust_mismatched_cancel_result(self):
        """撤单查询失败返回通用 CANCEL 结果时，保留原买单暴露并阻止下一笔买入。"""

        class MismatchedCancelBroker(RecordingBuyBroker):
            def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
                self.buy_calls += 1
                return OrderResult(
                    "submitted-1",
                    symbol,
                    "BUY",
                    1.0,
                    limit_price,
                    "SUBMITTED",
                    "adapter returned before terminal wait",
                )

            def cancel_order(self, order_id, reason):
                return OrderResult(
                    order_id,
                    "",
                    "CANCEL",
                    0,
                    0,
                    "REJECTED",
                    "cannot query original order",
                )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_buy_snapshot_for_symbol("US.TEST"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
            })
            broker = MismatchedCancelBroker()

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 2)
            self.assertEqual(broker.buy_calls, 1)

    def test_run_once_counts_partial_buy_once_after_cancel_confirmation(self):
        """部分成交先占一次名额，撤销余量确认后不得重复计数或误挡下一笔。"""

        class PartialThenFilledBroker(RecordingBuyBroker):
            def __init__(self):
                super().__init__()
                self.symbols = []

            def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
                self.buy_calls += 1
                self.symbols.append(symbol)
                if self.buy_calls == 1:
                    return OrderResult(
                        "partial-1",
                        symbol,
                        "BUY",
                        0.25,
                        limit_price,
                        "PARTIALLY_FILLED_SUBMITTED",
                        "partial fill; remainder still open",
                    )
                return OrderResult("filled-2", symbol, "BUY", 1.0, limit_price, "FILLED", "filled")

            def cancel_order(self, order_id, reason):
                return OrderResult(
                    order_id,
                    "US.TEST",
                    "BUY",
                    0.25,
                    9.8,
                    "PARTIALLY_FILLED_CANCELED",
                    "partial fill preserved; remainder canceled",
                )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings = Settings(**{**settings.__dict__, "max_daily_buys": 2})
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_buy_snapshot_for_symbol("US.TEST"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
            })
            broker = PartialThenFilledBroker()

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["buy"], 2)
            self.assertEqual(summary["hold"], 0)
            self.assertEqual(broker.symbols, ["US.TEST", "US.NEXT"])

    def test_run_once_pauses_later_buys_after_order_recording_failure(self):
        """真实订单结果无法落盘时，当前轮后续自动买入必须立即失败关闭。"""

        class RecordingFailureBroker(RecordingBuyBroker):
            def place_limit_buy(self, symbol, notional_usd, limit_price, reason):
                result = super().place_limit_buy(symbol, notional_usd, limit_price, reason)
                self.order_recording_error = "disk full"
                return result

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_buy_snapshot_for_symbol("US.TEST"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
            })
            broker = RecordingFailureBroker()

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["buy"], 1)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.buy_calls, 1)

    def test_run_once_pauses_new_buys_when_open_buy_order_exists(self):
        """Alpaca 已有开放买单时，本轮不再提交新的自动买单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.TEST": make_buy_snapshot_for_symbol("US.TEST"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
            })
            broker = OpenBuyOrderBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))

            self.assertEqual(summary["buy"], 0)
            self.assertEqual(summary["hold"], 2)
            self.assertEqual(broker.buy_calls, 0)
            self.assertEqual(market_data.calls, [])

    def test_run_once_order_error_does_not_block_next_symbol_buy(self):
        """错误下单不占每日买入次数，不能影响其他股票继续下单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.FAIL\nUS.NEXT\n", encoding="utf-8")
            market_data = FakeMarketData({
                "US.FAIL": make_buy_snapshot_for_symbol("US.FAIL"),
                "US.NEXT": make_buy_snapshot_for_symbol("US.NEXT"),
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

    def test_run_once_skips_buys_after_first_two_and_half_regular_hours(self):
        """常规盘 12:00 ET 后即使有买入信号，也不提交买单。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            market_data = FakeMarketData({"US.TEST": make_buy_snapshot_for_symbol("US.TEST")})
            broker = RecordingBuyBroker()

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 30))

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
        """三次买入错误保护不能挡住已布防持仓的止损卖出。"""
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
            first_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.1)})
            first = run_once(settings, market_data=first_market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.0)})

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 0))

            self.assertEqual(first["sell"], 0)
            self.assertEqual(summary["sell"], 1)
            self.assertEqual(summary["hold"], 0)
            self.assertEqual(market_data.calls, ["US.TEST"])

    def test_run_once_stop_loss_uses_limit_sell_at_8_percent_loss_price(self):
        """止损触发后提交 SELL LIMIT，限价为成本价亏损 8%。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = RecordingSellBroker()
            first_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.1)})
            second_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.0)})

            first = run_once(settings, market_data=first_market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))
            second = run_once(settings, market_data=second_market_data, broker=broker, now=datetime(2026, 5, 28, 10, 1))

            self.assertEqual(first["sell"], 0)
            self.assertEqual(second["sell"], 1)
            self.assertEqual(len(broker.sell_calls), 1)
            method, symbol, quantity, limit_price, reason = broker.sell_calls[0]
            self.assertEqual(method, "limit_sell")
            self.assertEqual(symbol, "US.TEST")
            self.assertEqual(quantity, 10.0)
            self.assertEqual(limit_price, 9.2)
            self.assertIn("8.00%", reason)

    def test_run_once_take_profit_still_uses_market_sell(self):
        """MA5 策略的 10% 半仓止盈仍走市价卖出。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = RecordingSellBroker()
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=11.5)})

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 0))

            self.assertEqual(summary["sell"], 1)
            self.assertEqual(len(broker.sell_calls), 1)
            method, symbol, quantity, price, reason = broker.sell_calls[0]
            self.assertEqual(method, "market_sell")
            self.assertEqual(symbol, "US.TEST")
            self.assertEqual(quantity, 5.0)
            self.assertEqual(price, 11.5)
            self.assertIn("10.00%", reason)

    def test_run_once_close_liquidation_still_uses_market_sell(self):
        """尾盘清仓不受止损限价单改动影响，仍走市价卖出。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = RecordingSellBroker()
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=10.2)})

            summary = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 15, 56))

            self.assertEqual(summary["sell"], 1)
            self.assertEqual(len(broker.sell_calls), 1)
            method, symbol, quantity, price, reason = broker.sell_calls[0]
            self.assertEqual(method, "market_sell")
            self.assertEqual(symbol, "US.TEST")
            self.assertEqual(quantity, 10.0)
            self.assertEqual(price, 10.2)
            self.assertIn("临近常规盘收盘", reason)

    def test_run_once_holds_position_already_below_stop_at_monitor_start(self):
        """监控启动第一眼已经低于止损线的旧仓，不自动清仓。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            first_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=8.5)})
            second_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=8.4)})

            first = run_once(
                settings,
                market_data=first_market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 12, 0),
            )
            second = run_once(
                settings,
                market_data=second_market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 12, 1),
            )

            self.assertEqual(first["sell"], 0)
            self.assertEqual(second["sell"], 0)
            self.assertIn("US.TEST", broker.get_positions())

    def test_run_once_rearms_grandfathered_position_after_quantity_changes(self):
        """启动时豁免的旧仓如果后来加仓，成本或数量变化后重新启用止损。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            first_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=8.5)})
            first = run_once(settings, market_data=first_market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))
            broker.place_market_buy("US.TEST", 100.0, 10.0, "manual add")
            second_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=8.5)})

            second = run_once(
                settings,
                market_data=second_market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 1),
            )

            self.assertEqual(first["sell"], 0)
            self.assertEqual(second["sell"], 1)
            self.assertNotIn("US.TEST", broker.get_positions())

    def test_run_once_sells_watch_position_when_loss_reaches_10_after_monitoring_starts(self):
        """监控启动时未触发止损，后面跌到 10% 会限价清仓。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            first_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.1)})
            second_market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.0)})

            first = run_once(
                settings,
                market_data=first_market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )
            second = run_once(
                settings,
                market_data=second_market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 12, 0),
            )

            self.assertEqual(first["sell"], 0)
            self.assertEqual(second["sell"], 1)
            self.assertNotIn("US.TEST", broker.get_positions())

    def test_run_once_sells_position_outside_watchlist_on_10_percent_loss(self):
        """监控启动后新出现的非 watchlist 持仓，亏损达到 10% 会限价清仓。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.WATCH\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            first_market_data = FakeMarketData({"US.WATCH": make_snapshot("US.WATCH", current=12.0)})
            first = run_once(settings, market_data=first_market_data, broker=broker, now=datetime(2026, 5, 28, 10, 0))
            broker.place_market_buy("US.OLD", 300.0, 10.0, "seed old position")
            market_data = FakeMarketData({
                "US.WATCH": make_snapshot("US.WATCH", current=12.0),
                "US.OLD": make_snapshot("US.OLD", current=8.5),
            })

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(first["sell"], 0)
            self.assertEqual(summary["watch"], 2)
            self.assertEqual(summary["sell"], 1)
            self.assertEqual(summary["hold"], 1)
            self.assertNotIn("US.OLD", broker.get_positions())
            self.assertEqual(market_data.calls, ["US.WATCH", "US.OLD"])

    def test_run_once_does_not_take_profit_position_outside_watchlist(self):
        """非 watchlist 既有持仓只应用止损，不触发自动分批止盈。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.OLD", 300.0, 10.0, "seed old position")
            market_data = FakeMarketData({"US.OLD": make_snapshot("US.OLD", current=11.5)})

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 10, 0),
            )

            self.assertEqual(summary["sell"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertAlmostEqual(broker.get_positions()["US.OLD"].quantity, 30.0)

    def test_run_once_sells_fraction_watch_position_on_take_profit(self):
        """单轮监控会对 watchlist 内收益达到 10% 的持仓卖出一半。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=11.5)})

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 12, 0),
            )

            self.assertEqual(summary["sell"], 1)
            self.assertAlmostEqual(broker.get_positions()["US.TEST"].quantity, 15.0)

    def test_run_once_sells_fraction_only_once_per_day_on_take_profit(self):
        """10% 半仓止盈当天已成交后，后续轮询不重复卖出。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = DryRunStockBroker(settings)
            broker.place_market_buy("US.TEST", 300.0, 10.0, "seed")
            market_data = FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=11.5)})

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 28, 12, 0)):
                first = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 0))
                second = run_once(settings, market_data=market_data, broker=broker, now=datetime(2026, 5, 28, 12, 1))

            self.assertEqual(first["sell"], 1)
            self.assertEqual(second["sell"], 0)
            self.assertEqual(second["hold"], 1)
            self.assertAlmostEqual(broker.get_positions()["US.TEST"].quantity, 15.0)

    def test_run_once_skips_duplicate_sell_when_same_symbol_order_is_open(self):
        """持仓仍存在但同股卖单开放时，不得在下一轮重复提交卖单。"""

        class OpenSellOrderBroker(RecordingSellBroker):
            def get_open_sell_order_symbols(self):
                return {"US.TEST"}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.watch_codes_file.write_text("US.TEST\n", encoding="utf-8")
            broker = OpenSellOrderBroker()
            market_data = FakeMarketData({})

            summary = run_once(
                settings,
                market_data=market_data,
                broker=broker,
                now=datetime(2026, 5, 28, 12, 1),
            )

            self.assertEqual(summary["sell"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(broker.sell_calls, [])
            self.assertEqual(market_data.calls, [])

    def test_discounted_limit_helpers(self):
        """测试下单限价和股数计算保持稳定。"""
        self.assertEqual(discounted_limit_price(100.0, 0.9), 90.0)
        self.assertEqual(quantity_for_notional(5.0, 90.0), 0.0)
        self.assertEqual(quantity_for_notional(100.0, 90.0), 1.0)

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
            self.assertEqual(preview.quantity, 0.0)
            self.assertEqual(market_data.calls, ["US.AAPL"])

    def test_manual_test_order_uses_shared_preview_reader(self):
        """真实测试下单和不提交预览应共用同一个行情读取逻辑。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = FakeAlpacaClient()

            with patch("alpaca_ma5_service.manual_order.build_test_order_preview", wraps=build_test_order_preview) as preview_fn:
                result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

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

            result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(result.price, 90.0)
            self.assertEqual(result.quantity, 1.0)
            self.assertEqual(client.order_data.limit_price, 90.0)
            self.assertIsNone(client.cancelled_order_id)

    def test_manual_test_order_cancels_when_unfilled_after_timeout(self):
        """测试下单超过等待时间仍未成交时，应请求取消订单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = PendingAlpacaClient()

            result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

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

            result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

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

    def test_order_guard_preserves_partial_fill_when_cancel_request_fails(self):
        """部分成交后的撤单异常仍同时表达已成交数量和未确认余量。"""

        class PartialCancelFailureClient(PartialFillAlpacaClient):
            def cancel_order_by_id(self, order_id):
                raise RuntimeError("cancel endpoint unavailable")

        client = PartialCancelFailureClient()
        raw_order = client.submit_order(type("Request", (), {"qty": 1.0})())

        result = wait_for_fill_or_cancel(
            client,
            raw_order,
            "US.AAPL",
            "BUY",
            1.0,
            100.0,
            "alpaca-paper",
            timeout_seconds=0,
        )

        self.assertEqual(result.status, "PARTIALLY_FILLED_CANCEL_FAILED")
        self.assertEqual(result.quantity, 0.25)
        self.assertTrue(has_unconfirmed_order_status(result.status))

    def test_partial_order_status_distinguishes_open_remainder_from_terminal(self):
        """部分成交前缀不能掩盖剩余订单仍开放或已经终止。"""
        self.assertTrue(has_unconfirmed_order_status("PARTIALLY_FILLED"))
        self.assertTrue(has_unconfirmed_order_status("PARTIALLY_FILLED_CANCEL_REQUESTED"))
        self.assertTrue(has_unconfirmed_order_status("PARTIALLY_FILLED_CANCEL_FAILED"))
        self.assertTrue(has_unconfirmed_order_status("DONE_FOR_DAY"))
        self.assertTrue(has_unconfirmed_order_status("REPLACED"))
        self.assertTrue(has_unconfirmed_order_status("SUBMIT_UNCONFIRMED"))
        self.assertFalse(has_unconfirmed_order_status("PARTIALLY_FILLED_CANCELED"))
        self.assertFalse(has_unconfirmed_order_status("PARTIALLY_FILLED_FILLED"))

    def test_order_guard_cancels_done_for_day_order_instead_of_treating_it_as_terminal(self):
        """done_for_day 后续交易日仍可能更新，自动保护必须继续按单号撤单。"""

        client = PendingAlpacaClient()
        client.order_data = type("Request", (), {"qty": 1.0})()
        raw_order = type(
            "RawOrder",
            (),
            {"id": "day-order-1", "status": "done_for_day", "qty": 1.0, "filled_qty": "0"},
        )()

        result = wait_for_fill_or_cancel(
            client,
            raw_order,
            "US.AAPL",
            "BUY",
            1.0,
            100.0,
            "alpaca-paper",
            timeout_seconds=0,
        )

        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(client.cancelled_order_id, "day-order-1")

    def test_order_guard_follows_replaced_order_before_canceling(self):
        """旧订单被替换后，必须撤当前 replacement id，不能对旧 id 宣告结束。"""

        class ReplacedOrderClient(PendingAlpacaClient):
            def get_order_by_id(self, order_id):
                if order_id == "replacement-1":
                    if self.cancelled_order_id == order_id:
                        return type(
                            "RawOrder",
                            (),
                            {"id": order_id, "status": "canceled", "qty": 1.0, "filled_qty": "0"},
                        )()
                    return type(
                        "RawOrder",
                        (),
                        {"id": order_id, "status": "accepted", "qty": 1.0, "filled_qty": "0"},
                    )()
                return super().get_order_by_id(order_id)

        client = ReplacedOrderClient()
        client.order_data = type("Request", (), {"qty": 1.0})()
        raw_order = type(
            "RawOrder",
            (),
            {
                "id": "original-1",
                "status": "replaced",
                "replaced_by": "replacement-1",
                "qty": 1.0,
                "filled_qty": "0",
            },
        )()

        result = wait_for_fill_or_cancel(
            client,
            raw_order,
            "US.AAPL",
            "BUY",
            1.0,
            100.0,
            "alpaca-paper",
            timeout_seconds=0,
        )

        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(result.order_id, "replacement-1")
        self.assertEqual(client.cancelled_order_id, "replacement-1")

    def test_alpaca_broker_explicit_cancel_follows_replaced_order(self):
        """显式撤单入口也必须沿 replaced_by 撤销当前订单，而不是只保护自动超时路径。"""

        class ReplacedOrderClient(PendingAlpacaClient):
            def get_order_by_id(self, order_id):
                if order_id == "original-1":
                    return type(
                        "RawOrder",
                        (),
                        {
                            "id": order_id,
                            "symbol": "AAPL",
                            "side": "buy",
                            "status": "replaced",
                            "replaced_by": "replacement-1",
                            "qty": 1.0,
                            "filled_qty": "0",
                        },
                    )()
                if self.cancelled_order_id == order_id:
                    status = "canceled"
                else:
                    status = "accepted"
                return type(
                    "RawOrder",
                    (),
                    {
                        "id": order_id,
                        "symbol": "AAPL",
                        "side": "buy",
                        "status": status,
                        "qty": 1.0,
                        "filled_qty": "0",
                    },
                )()

        with TemporaryDirectory() as tmp:
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = make_settings(Path(tmp))
            broker.client = ReplacedOrderClient()
            broker.paper = True

            with patch("alpaca_ma5_service.broker.record_order_and_notify"):
                result = broker.cancel_order("original-1", "unit-test explicit cancel")

        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(result.order_id, "replacement-1")
        self.assertEqual(broker.client.cancelled_order_id, "replacement-1")

    def test_manual_test_order_marks_unconfirmed_cancel_as_risky(self):
        """撤单请求未确认最终状态时，保留 CANCEL_REQUESTED 供本轮风控使用。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})
            client = UnconfirmedCancelAlpacaClient()

            result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

            self.assertEqual(result.status, "CANCEL_REQUESTED")
            self.assertIn("latest_status=ACCEPTED", result.message)

    def test_manual_test_order_returns_rejected_on_alpaca_error(self):
        """Alpaca 拒单时返回 REJECTED，不抛 traceback。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            market_data = FakeMarketData({"US.AAPL": make_snapshot("US.AAPL", current=100.0)})

            result = place_test_order(settings=settings, market_data=market_data, client=RejectingAlpacaClient(), buy_notional_usd=100.0)

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
                        result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

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
                result = place_test_order(settings=settings, market_data=market_data, client=RejectingAlpacaClient(), buy_notional_usd=100.0)

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
                    result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

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
                result = place_test_order(settings=settings, market_data=market_data, client=client, buy_notional_usd=100.0)

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

    def test_alpaca_broker_preserves_order_identity_when_terminal_strategy_raises(self):
        """submit 已成功后终态策略异常时，必须返回可撤的原订单而不是抛失订单号。"""

        class RaisingCancelStrategy:
            def wait_for_terminal(self, *args, **kwargs):
                raise RuntimeError("terminal strategy crashed")

        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            for fixed_limit in (False, True):
                with self.subTest(fixed_limit=fixed_limit):
                    client = PendingAlpacaClient()
                    broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
                    broker.settings = settings
                    broker.client = client
                    broker.paper = True
                    broker.cancel_strategy = RaisingCancelStrategy()

                    with patch(
                        "alpaca_ma5_service.broker.now_market_time",
                        return_value=datetime(2026, 5, 29, 10, 0),
                    ):
                        if fixed_limit:
                            result = broker._submit_fixed_limit_order(
                                "US.AAPL",
                                "BUY",
                                1.0,
                                99.0,
                            )
                        else:
                            result = broker._submit_order(
                                "US.AAPL",
                                "BUY",
                                1.0,
                                100.0,
                            )

                    self.assertEqual(result.order_id, "pending-order-1")
                    self.assertEqual(result.side, "BUY")
                    self.assertEqual(result.status, "ACCEPTED")
                    self.assertTrue(has_unconfirmed_order_status(result.status))
                    self.assertIn("exposure remains unconfirmed", result.message)

    def test_alpaca_broker_recovers_timeout_by_client_order_id(self):
        """submit 超时但券商已收单时，按 client_order_id 找回订单并完成终态保护。"""

        class RecoveredSubmitClient(PendingAlpacaClient):
            def submit_order(self, order_data):
                self.order_data = order_data
                raise TimeoutError("response lost after submit")

            def get_order_by_client_id(self, client_order_id):
                self.recovered_client_order_id = client_order_id
                return type(
                    "RawOrder",
                    (),
                    {"id": "recovered-order-1", "status": "accepted", "qty": self.order_data.qty, "filled_qty": "0"},
                )()

        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = RecoveredSubmitClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True
            broker.order_safety_error = ""

            with patch(
                "alpaca_ma5_service.broker.now_market_time",
                return_value=datetime(2026, 5, 29, 10, 0),
            ):
                result = broker._submit_fixed_limit_order("US.AAPL", "BUY", 1.0, 99.0)

            self.assertEqual(result.status, "CANCELED")
            self.assertEqual(result.order_id, "recovered-order-1")
            self.assertEqual(client.recovered_client_order_id, client.order_data.client_order_id)
            self.assertEqual(client.cancelled_order_id, "recovered-order-1")
            self.assertEqual(broker.order_safety_error, "")

    def test_alpaca_broker_fails_closed_when_submit_outcome_cannot_be_recovered(self):
        """submit 超时且无法反查时不能伪装成 REJECTED，必须保留未知暴露。"""

        class UnknownSubmitClient(FakeAlpacaClient):
            def submit_order(self, order_data):
                self.order_data = order_data
                raise TimeoutError("submit response lost")

            def get_order_by_client_id(self, client_order_id):
                raise TimeoutError("lookup unavailable")

        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = UnknownSubmitClient()
            broker.paper = True
            broker.order_safety_error = ""

            with patch(
                "alpaca_ma5_service.broker.now_market_time",
                return_value=datetime(2026, 5, 29, 10, 0),
            ):
                with redirect_stdout(StringIO()):
                    result = broker._submit_fixed_limit_order("US.AAPL", "BUY", 1.0, 99.0)

            self.assertEqual(result.status, "SUBMIT_UNCONFIRMED")
            self.assertTrue(has_unconfirmed_order_status(result.status))
            self.assertIn("client_order_id=", broker.order_safety_error)

    def test_alpaca_broker_latches_order_recording_failure_without_losing_fill(self):
        """券商已成交但本地 CSV 写失败时，保留 FILLED 并锁存自动买入风险。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = FakeAlpacaClient()
            broker.paper = True

            with patch(
                "alpaca_ma5_service.broker.now_market_time",
                return_value=datetime(2026, 5, 29, 10, 0),
            ):
                with patch(
                    "alpaca_ma5_service.broker.record_order_and_notify",
                    side_effect=OSError("disk full"),
                ):
                    with redirect_stdout(StringIO()):
                        result = broker.place_market_buy(
                            "US.AAPL",
                            100.0,
                            100.0,
                            "unit-test buy",
                        )

            self.assertEqual(result.status, "FILLED")
            self.assertIn("disk full", broker.order_recording_error)

    def test_alpaca_broker_cancel_keeps_partial_fill_from_terminal_raw_order(self):
        """显式撤单查询到部分成交后已取消时，不能把成交量降级成普通 CANCELED。"""

        class TerminalPartialClient:
            def get_order_by_id(self, order_id):
                return type(
                    "RawOrder",
                    (),
                    {
                        "id": order_id,
                        "symbol": "AAPL",
                        "side": "buy",
                        "qty": "1",
                        "filled_qty": "0.25",
                        "status": "canceled",
                        "limit_price": "99",
                    },
                )()

        with TemporaryDirectory() as tmp:
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = make_settings(Path(tmp))
            broker.client = TerminalPartialClient()
            broker.paper = True

            with patch.object(
                broker,
                "_record_result",
                side_effect=lambda result, reason: result,
            ):
                result = broker.cancel_order("partial-1", "unit-test cancel")

            self.assertEqual(result.status, "PARTIALLY_FILLED_CANCELED")
            self.assertEqual(result.quantity, 0.25)

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

    def test_alpaca_broker_rejects_buy_after_first_two_and_half_regular_hours(self):
        """即使绕过 service 直接调用 broker，12:00 ET 后 BUY 也不会提交到 Alpaca。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = PendingAlpacaClient()
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 12, 30)):
                result = broker._submit_order("US.AAPL", "BUY", 1.0, 100.0)

            self.assertEqual(result.status, "REJECTED")
            self.assertIn("前 2.5 小时", result.message)
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

    def test_alpaca_broker_uses_integer_qty_even_when_asset_allows_fractional(self):
        """即使 Alpaca 资产支持碎股，自动买入也只提交整数股。"""
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
            self.assertEqual(float(client.order_data.qty), 518.0)

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

    def test_alpaca_broker_rounds_fractional_sell_down_when_asset_is_not_fractionable(self):
        """非碎股资产止盈半仓算出小数时，提交前向下取整避免 Alpaca 拒单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = FakeAlpacaClient(fractionable=False)
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                result = broker.place_market_sell("US.DCOY", 227.5, 7.59, "持仓收益达到 10.00%，止盈一半")

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(float(client.order_data.qty), 227.0)
            self.assertEqual(result.quantity, 227.0)

    def test_alpaca_broker_keeps_fractional_sell_when_asset_is_fractionable(self):
        """支持碎股的资产卖出时保留小数数量。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = FakeAlpacaClient(fractionable=True)
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                result = broker.place_market_sell("US.FRAC", 1.5, 10.0, "unit-test fractional sell")

            self.assertEqual(result.status, "FILLED")
            self.assertEqual(float(client.order_data.qty), 1.5)
            self.assertEqual(result.quantity, 1.5)

    def test_alpaca_broker_rejects_sub_one_nonfractionable_sell_before_submit(self):
        """非碎股资产小于 1 股时本地拒绝，不提交会被 Alpaca 拒的订单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = FakeAlpacaClient(fractionable=False)
            broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
            broker.settings = settings
            broker.client = client
            broker.paper = True

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=datetime(2026, 5, 29, 10, 0)):
                result = broker.place_market_sell("US.DCOY", 0.5, 7.59, "unit-test tiny sell")

            self.assertEqual(result.status, "REJECTED")
            self.assertIsNone(client.order_data)
            self.assertIn("不足 1 股", result.message)

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
        self.assertIn("【卖单｜已成交】", message)
        self.assertIn("股票：US.AAPL", message)
        self.assertIn("账户：Alpaca LIVE（真实账户）", message)
        self.assertIn("订单信息", message)
        self.assertIn("状态：✅ 已成交（FILLED）", message)
        self.assertIn("- 数量：0.25 股", message)
        self.assertIn("- 价格：$100.0000", message)
        self.assertIn("- 估算金额：约 $25.00", message)
        self.assertIn("策略原因", message)
        self.assertIn("- 止损卖出", message)
        self.assertIn("下一步", message)

    def test_submitted_notification_uses_chinese_single_message(self):
        """即时下单通知也用一条中文消息，方便和最终状态区分。"""
        result = OrderResult("order-1", "US.AAPL", "BUY", 1.25, 100.0, "ACCEPTED", "submitted")

        message = render_order_submitted_message(result, "unit-test buy", broker_name="alpaca-live")

        self.assertIn("【买单｜等待成交】", message)
        self.assertIn("股票：US.AAPL", message)
        self.assertIn("账户：Alpaca LIVE（真实账户）", message)
        self.assertIn("订单信息", message)
        self.assertIn("状态：🟡 已提交，等待成交（ACCEPTED）", message)
        self.assertIn("- 数量：1.25 股", message)
        self.assertIn("- 价格：$100.0000", message)
        self.assertIn("- 估算金额：约 $125.00", message)
        self.assertIn("策略原因", message)
        self.assertIn("- unit-test buy", message)
        self.assertIn("尚未证明成交", message)

    def test_trade_notifications_keep_full_reason_and_result(self):
        """通知里的原因和结果不省略，只压缩多余空白。"""
        long_reason = "原因：" + "当前价高于触发上沿；动作观察不买；" * 12
        long_message = "结果：" + "not filled; cancel requested; " * 12
        result = OrderResult("order-1", "US.AAPL", "BUY", 1.0, 10.0, "CANCELED", long_message)

        final_message = render_trade_order_messages(result, long_reason, broker_name="alpaca-live")[0]
        submitted_message = render_order_submitted_message(result, long_reason, broker_name="alpaca-live")

        self.assertIn(long_reason, final_message)
        self.assertIn(long_message.strip(), final_message)
        self.assertIn(long_reason, submitted_message)
        self.assertNotIn("...", final_message)
        self.assertNotIn("...", submitted_message)

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

    def test_hermes_send_is_used_when_openclaw_command_is_missing(self):
        """本机没有 openclaw 命令时，通知自动走 Hermes send。"""
        with TemporaryDirectory() as tmp:
            hermes_python = Path(tmp) / "python.exe"
            hermes_python.write_text("", encoding="utf-8")
            settings = Settings(**{**make_settings(Path(".")).__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                return CompletedProcess(args, 0, '{"ok":true}', "")

            with patch.object(openclaw_notify.shutil, "which", return_value=None):
                with patch.object(openclaw_notify, "_HERMES_AGENT_PYTHON", hermes_python):
                    with patch.object(openclaw_notify.subprocess, "run", fake_run):
                        openclaw_notify.send_openclaw_telegram_message(settings, "hello")

            self.assertEqual(
                calls,
                [
                    [str(hermes_python), "-m", "hermes_cli.main", "gateway", "status"],
                    [str(hermes_python), "-m", "hermes_cli.main", "send", "--to", "telegram:123456", "hello", "--json"],
                ],
            )

    def test_hermes_send_is_used_when_openclaw_send_fails(self):
        """OpenClaw 存在但发送失败时，通知自动兜底到 Hermes。"""
        settings = Settings(**{**make_settings(Path(".")).__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": "123456"})
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[0] == "openclaw.cmd":
                return CompletedProcess(args, 1, "", "openclaw down")
            return CompletedProcess(args, 0, '{"ok":true}', "")

        with patch.object(openclaw_notify, "_OPENCLAW_GATEWAY_READY", True):
            with patch.object(
                openclaw_notify,
                "messaging_commands",
                return_value=[("openclaw", ["openclaw.cmd"]), ("hermes", ["hermes.cmd"])],
            ):
                with patch.object(openclaw_notify.subprocess, "run", fake_run):
                    openclaw_notify.send_openclaw_telegram_message(settings, "hello")

        self.assertEqual(
            calls,
            [
                ["openclaw.cmd", "message", "send", "--channel", "telegram", "--target", "123456", "--message", "hello", "--json"],
                ["hermes.cmd", "gateway", "status"],
                ["hermes.cmd", "send", "--to", "telegram:123456", "hello", "--json"],
            ],
        )

    def test_hermes_send_uses_default_telegram_target_when_target_is_empty(self):
        """没有显式 target 时，本机 Hermes send 使用默认 telegram 目标。"""
        with TemporaryDirectory() as tmp:
            hermes_python = Path(tmp) / "python.exe"
            hermes_python.write_text("", encoding="utf-8")
            settings = Settings(**{**make_settings(Path(".")).__dict__, "trade_notify_openclaw_enabled": True, "openclaw_telegram_target": ""})
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                return CompletedProcess(args, 0, '{"ok":true}', "")

            with patch.object(openclaw_notify.shutil, "which", return_value=None):
                with patch.object(openclaw_notify, "_HERMES_AGENT_PYTHON", hermes_python):
                    with patch.object(openclaw_notify.subprocess, "run", fake_run):
                        openclaw_notify.send_openclaw_telegram_message(settings, "hello")

            self.assertEqual(
                calls,
                [
                    [str(hermes_python), "-m", "hermes_cli.main", "gateway", "status"],
                    [str(hermes_python), "-m", "hermes_cli.main", "send", "--to", "telegram", "hello", "--json"],
                ],
            )

    def test_hermes_gateway_starts_when_not_running(self):
        """Hermes gateway 没有运行时，先尝试本机启动，再发送通知。"""
        command = ["hermes.cmd"]
        calls = []
        results = iter(
            [
                CompletedProcess(["status"], 0, "No gateway process detected", ""),
                CompletedProcess(["start"], 0, "started", ""),
                CompletedProcess(["send"], 0, '{"ok":true}', ""),
            ]
        )

        def fake_run(args, **kwargs):
            calls.append(args)
            return next(results)

        with patch.object(openclaw_notify.subprocess, "run", fake_run):
            openclaw_notify.send_hermes_telegram_message(command, "telegram:123456", "hello")

        self.assertEqual(
            calls,
            [
                ["hermes.cmd", "gateway", "status"],
                ["hermes.cmd", "gateway", "start"],
                ["hermes.cmd", "send", "--to", "telegram:123456", "hello", "--json"],
            ],
        )

    def test_cloud_notify_mode_posts_signed_webhook(self):
        """cloud 模式使用和 local-notify 客户端一致的 HMAC webhook 请求。"""
        settings = Settings(
            **{
                **make_settings(Path(".")).__dict__,
                "trade_notify_openclaw_enabled": True,
                "trade_notify_mode": "cloud",
                "cloud_notify_webhook_url": "http://127.0.0.1:8644/webhooks/local-notify",
                "cloud_notify_webhook_secret": "unit-secret",
            }
        )
        seen = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok":true}'

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            seen["body"] = request.data
            seen["signature"] = request.get_header("X-webhook-signature")
            return FakeResponse()

        with patch.object(openclaw_notify.urllib.request, "urlopen", fake_urlopen):
            with patch.object(openclaw_notify, "send_openclaw_telegram_message", side_effect=AssertionError("local sender used")):
                openclaw_notify.safe_send_openclaw_messages(settings, ["hello"], context="unit cloud")

        expected_signature = hmac.new(b"unit-secret", seen["body"], hashlib.sha256).hexdigest()
        self.assertEqual(seen["url"], "http://127.0.0.1:8644/webhooks/local-notify")
        self.assertEqual(seen["timeout"], 30)
        self.assertEqual(
            json.loads(seen["body"].decode("utf-8")),
            {"event": "alpaca_trade_notify", "message": "hello"},
        )
        self.assertEqual(seen["signature"], expected_signature)

    def test_daily_buy_count_tracks_only_executed_orders(self):
        """确认只有实际成交买单才占用每日名额，撤单/拒单/未确认撤单都不占。"""
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "outputs"
            append_order(output_dir, OrderResult("1", "US.AAPL", "BUY", 1, 10, "CANCELED", "cancel"), "test")
            append_order(output_dir, OrderResult("2", "US.AAPL", "BUY", 1, 10, "REJECTED", "reject"), "test")
            append_order(output_dir, OrderResult("3", "US.AAPL", "BUY", 1, 10, "FILLED", "filled"), "test")
            append_order(output_dir, OrderResult("4", "US.AAPL", "BUY", 0.25, 10, "PARTIALLY_FILLED_CANCEL_REQUESTED", "partial"), "test")
            append_order(output_dir, OrderResult("5", "US.AAPL", "BUY", 1, 10, "CANCEL_REQUESTED", "unconfirmed"), "test")
            append_order(output_dir, OrderResult("6", "US.AAPL", "BUY", 1, 10, "CANCEL_FAILED", "risky"), "test")

            self.assertEqual(count_today_buy_orders(output_dir), 2)

    def test_daily_buy_count_deduplicates_one_order_lifecycle(self):
        """同一 order_id 的部分成交与最终状态只能占用一个每日买入名额。"""
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "outputs"
            append_order(
                output_dir,
                OrderResult(
                    "same-order",
                    "US.AAPL",
                    "BUY",
                    0.25,
                    10,
                    "PARTIALLY_FILLED_CANCEL_REQUESTED",
                    "partial",
                ),
                "test",
            )
            append_order(
                output_dir,
                OrderResult(
                    "same-order",
                    "US.AAPL",
                    "BUY",
                    0.25,
                    10,
                    "PARTIALLY_FILLED_CANCELED",
                    "terminal",
                ),
                "test",
            )
            append_order(
                output_dir,
                OrderResult("second-order", "US.MSFT", "BUY", 1, 20, "FILLED", "filled"),
                "test",
            )

            self.assertEqual(count_today_buy_orders(output_dir), 2)

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

    def test_run_forever_sends_cloud_start_notification(self):
        """盘中监控进程启动时，要先发一条云端启动消息。"""
        with TemporaryDirectory() as tmp:
            settings = Settings(
                **{
                    **make_settings(Path(tmp)).__dict__,
                    "trade_notify_openclaw_enabled": True,
                    "trade_notify_mode": "cloud",
                    "cloud_notify_webhook_url": "https://example.invalid/hook",
                    "cloud_notify_webhook_secret": "secret",
                }
            )
            now_et = datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))

            with patch("alpaca_ma5_service.service.safe_send_openclaw_messages") as fake_send:
                with patch("alpaca_ma5_service.service.build_market_data", return_value=FakeMarketData({})):
                    with patch("alpaca_ma5_service.service.build_broker", return_value=RecordingBuyBroker()):
                        with patch("alpaca_ma5_service.service.run_once"):
                            with redirect_stdout(StringIO()):
                                run_forever(
                                    settings,
                                    max_loops=1,
                                    sleep=lambda _: None,
                                    now_provider=lambda: now_et,
                                )

            fake_send.assert_called_once()
            message = fake_send.call_args.args[1][0]
            self.assertIn("开始盘中监控", message)
            self.assertIn("动作：按策略检测买入/卖出信号", message)
            self.assertIn("监控配置", message)
            self.assertIn("风控规则", message)
            self.assertIn(str(settings.watch_codes_file), message)
            self.assertEqual(fake_send.call_args.kwargs["context"], "intraday MA5 monitor started")

    def test_run_forever_stops_at_intraday_close(self):
        """盘中监控到 16:00 ET 后自动退出，不继续睡眠等待。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            times = iter(
                [
                    datetime(2026, 5, 28, 15, 59, 50, tzinfo=ZoneInfo("America/New_York")),
                    datetime(2026, 5, 28, 15, 59, 50, tzinfo=ZoneInfo("America/New_York")),
                    datetime(2026, 5, 28, 16, 0, 0, tzinfo=ZoneInfo("America/New_York")),
                ]
            )
            sleeps: list[int] = []

            with patch("alpaca_ma5_service.service.safe_send_openclaw_messages"):
                with patch("alpaca_ma5_service.service.build_market_data", return_value=FakeMarketData({})):
                    with patch("alpaca_ma5_service.service.build_broker", return_value=RecordingBuyBroker()):
                        with patch("alpaca_ma5_service.service.run_once") as fake_run_once:
                            with redirect_stdout(StringIO()):
                                run_forever(settings, sleep=lambda seconds: sleeps.append(seconds), now_provider=lambda: next(times))

            fake_run_once.assert_called_once()
            self.assertEqual(sleeps, [])
            self.assertFalse(is_intraday_monitor_finished(datetime(2026, 5, 28, 15, 59, 59)))
            self.assertTrue(is_intraday_monitor_finished(datetime(2026, 5, 28, 16, 0)))

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

    def test_latest_completed_bar_basis_rejects_reverse_split_transition(self):
        """最近完成日线 RAW/SPLIT 倍数异常时，不能混用旧实时价继续决策。"""
        now_et = datetime(2026, 7, 27, 8, 0, tzinfo=ZoneInfo("America/New_York"))
        split_bars = [_SnapshotBar(date(2026, 7, 24), 94.18)]
        raw_bars = [_SnapshotBar(date(2026, 7, 24), 0.4709)]

        with self.assertRaisesRegex(MarketDataSafetyError, "倍数=200.000000") as caught:
            validate_latest_completed_bar_basis("US.RCON", split_bars, raw_bars, now_et)

        self.assertIsInstance(caught.exception, CorporateActionBasisError)
        self.assertIn("盘中买入、补仓、止盈、止损", str(caught.exception))

    def test_premarket_observation_falls_back_to_daily_reference_without_session_price(self):
        """当日盘前完全无成交时，只读观察返回昨收参考值，自动用途仍失败关闭。"""
        split_bars = [
            _SnapshotBar(date(2026, 7, 20), 8.0, 7.8),
            _SnapshotBar(date(2026, 7, 21), 8.5, 8.1),
            _SnapshotBar(date(2026, 7, 22), 9.0, 8.7),
            _SnapshotBar(date(2026, 7, 23), 9.5, 9.1),
            _SnapshotBar(date(2026, 7, 24), 10.0, 9.7),
        ]
        raw_bars = [
            _SnapshotBar(bar.date, bar.close, bar.open)
            for bar in split_bars
        ]
        market_data = object.__new__(AlpacaMarketData)
        market_data.market_tz = ZoneInfo("America/New_York")
        market_data._last_daily_feed = "sip"
        market_data._last_realtime_as_of = None
        market_data._daily_bars_pair = lambda _symbol, _now: (split_bars, raw_bars)
        market_data._current_price = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unit no current-session price")
        )

        snapshot = market_data.get_snapshot(
            "US.FEBO",
            purpose=SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION,
        )

        self.assertEqual(snapshot.current_price, 10.0)
        self.assertEqual(snapshot.current_price_source, "alpaca_daily_close:sip")
        self.assertIsNone(snapshot.current_price_as_of)
        self.assertAlmostEqual(snapshot.signal_day_gain_pct, 10.0 / 9.5 - 1.0)
        with self.assertRaisesRegex(RuntimeError, "unit no current-session price"):
            market_data.get_snapshot("US.FEBO")

    def test_latest_completed_bar_basis_accepts_aligned_prices(self):
        """同一完成交易日 RAW/SPLIT 口径一致时允许继续读取实时行情。"""
        now_et = datetime(2026, 7, 27, 8, 0, tzinfo=ZoneInfo("America/New_York"))
        split_bars = [_SnapshotBar(date(2026, 7, 24), 12.34)]
        raw_bars = [_SnapshotBar(date(2026, 7, 24), 12.34)]

        validate_latest_completed_bar_basis("US.TEST", split_bars, raw_bars, now_et)

    def test_realtime_price_timestamp_rejects_cross_day_and_stale_prices(self):
        """实时价必须属于当前市场日，并满足盘中五分钟的新鲜度上限。"""
        now_et = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("America/New_York"))

        accepted = validate_realtime_price_as_of(
            "US.TEST",
            "moomoo_snapshot:last_price",
            now_et - timedelta(minutes=1),
            now_et,
        )
        self.assertEqual(accepted, now_et - timedelta(minutes=1))

        with self.assertRaisesRegex(MarketDataSafetyError, "行情日期"):
            validate_realtime_price_as_of(
                "US.TEST",
                "moomoo_snapshot:last_price",
                now_et - timedelta(days=1),
                now_et,
            )
        with self.assertRaisesRegex(MarketDataSafetyError, "超过 300 秒安全上限"):
            validate_realtime_price_as_of(
                "US.TEST",
                "moomoo_snapshot:last_price",
                now_et - timedelta(minutes=6),
                now_et,
            )

    def test_premarket_observation_accepts_sparse_current_session_trade(self):
        """只提醒的盘前观察允许使用当日04:00后最后成交，不误伤稀疏股票。"""
        now_et = datetime(2026, 7, 27, 5, 30, tzinfo=ZoneInfo("America/New_York"))
        sparse_trade = datetime(2026, 7, 27, 4, 5, tzinfo=ZoneInfo("America/New_York"))

        with self.assertRaisesRegex(MarketDataSafetyError, "超过 1800 秒安全上限"):
            validate_realtime_price_as_of(
                "US.NCT",
                "moomoo_snapshot:pre_price",
                sparse_trade,
                now_et,
            )

        accepted = validate_realtime_price_as_of(
            "US.NCT",
            "moomoo_snapshot:pre_price",
            sparse_trade,
            now_et,
            allow_sparse_premarket=True,
        )

        self.assertEqual(accepted, sparse_trade)

    def test_premarket_observation_still_rejects_pre_session_and_regular_stale_prices(self):
        """盘前专用放宽不能接受04:00前价格，也不能泄漏到盘中自动动作。"""
        premarket_now = datetime(2026, 7, 27, 5, 30, tzinfo=ZoneInfo("America/New_York"))
        regular_now = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("America/New_York"))

        with self.assertRaisesRegex(MarketDataSafetyError, "早于当日 04:00 ET"):
            validate_realtime_price_as_of(
                "US.ORIC",
                "moomoo_snapshot:pre_price",
                datetime(2026, 7, 27, 3, 59, 59, tzinfo=ZoneInfo("America/New_York")),
                premarket_now,
                allow_sparse_premarket=True,
            )
        with self.assertRaisesRegex(MarketDataSafetyError, "超过 300 秒安全上限"):
            validate_realtime_price_as_of(
                "US.ORIC",
                "moomoo_snapshot:last_price",
                regular_now - timedelta(minutes=6),
                regular_now,
                allow_sparse_premarket=True,
            )

    def test_stale_moomoo_price_falls_back_to_fresh_alpaca_trade(self):
        """Moomoo 时间戳过期时只允许切到已证明新鲜的 Alpaca 行情。"""
        now_et = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        market_data = object.__new__(AlpacaMarketData)
        market_data.realtime_price_source = FakeRealtimePriceSource(
            price=0.4709,
            as_of=now_et - timedelta(days=1),
        )
        market_data.trade_feed = "iex"
        market_data._last_realtime_as_of = None

        def fresh_trade(_symbol):
            market_data._last_realtime_as_of = now_et - timedelta(seconds=20)
            return 94.20

        market_data._latest_trade_price = fresh_trade
        market_data._latest_quote_price = lambda _symbol: (_ for _ in ()).throw(RuntimeError("no quote"))

        price, source, _, _ = market_data._current_price(
            "US.RCON",
            "RCON",
            now_et,
            require_current_session=True,
        )

        self.assertEqual(price, 94.20)
        self.assertEqual(source, "alpaca_latest_trade:iex")
        self.assertEqual(market_data._last_realtime_as_of, now_et - timedelta(seconds=20))

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

    def test_moomoo_failure_falls_back_to_alpaca_latest_quote_during_premarket(self):
        """盘前 Moomoo 单股无价时，优先换到 Alpaca latest quote。"""
        class BrokenRealtime:
            def latest_price_quote(self, symbol, *, now=None):
                raise RuntimeError("unit broken realtime")

        market_data = object.__new__(AlpacaMarketData)
        market_data.realtime_price_source = BrokenRealtime()
        market_data.trade_feed = "iex"
        market_data._latest_quote_price = lambda symbol: (12.34, "alpaca_latest_quote:midpoint:iex")
        market_data._latest_trade_price = lambda symbol: 56.78

        output = StringIO()
        with redirect_stdout(output):
            price, source, today_open, today_open_source = market_data._current_price(
                "US.TEST",
                "TEST",
                datetime(2026, 5, 29, 8, 0),
            )

        self.assertEqual(price, 12.34)
        self.assertEqual(source, "alpaca_latest_quote:midpoint:iex")
        self.assertEqual(today_open, 0.0)
        self.assertEqual(today_open_source, "")
        self.assertEqual(output.getvalue(), "")

    def test_alpaca_quote_fallback_uses_trade_when_quote_is_invalid(self):
        """Alpaca quote 也无价时，再切到 latest trade。"""
        class BrokenRealtime:
            def latest_price_quote(self, symbol, *, now=None):
                raise RuntimeError("unit broken realtime")

        market_data = object.__new__(AlpacaMarketData)
        market_data.realtime_price_source = BrokenRealtime()
        market_data.trade_feed = "iex"
        market_data._latest_quote_price = lambda symbol: (_ for _ in ()).throw(RuntimeError("no quote"))
        market_data._latest_trade_price = lambda symbol: 12.34

        price, source, _, _ = market_data._current_price("US.TEST", "TEST", datetime(2026, 5, 29, 8, 0))

        self.assertEqual(price, 12.34)
        self.assertEqual(source, "alpaca_latest_trade:iex")

    def test_market_time_polling_uses_realtime_order_window(self):
        """只有常规盘用 10 秒；盘前盘后用空闲间隔，临近 9:30 再缩短。"""
        settings = make_settings(Path("."))

        self.assertTrue(is_realtime_order_time(datetime(2026, 5, 29, 8, 0)))
        self.assertTrue(is_premarket_time(datetime(2026, 5, 29, 8, 0)))
        self.assertFalse(is_buy_order_time(datetime(2026, 5, 29, 8, 0)))
        self.assertTrue(is_regular_market_time(datetime(2026, 5, 29, 15, 59, 59)))
        self.assertFalse(is_regular_market_time(datetime(2026, 5, 29, 16, 0)))
        self.assertTrue(is_buy_order_time(datetime(2026, 5, 29, 10, 0)))
        self.assertTrue(is_buy_order_time(datetime(2026, 5, 29, 11, 59, 59)))
        self.assertFalse(is_buy_order_time(datetime(2026, 5, 29, 12, 0)))
        self.assertFalse(is_buy_order_time(datetime(2026, 5, 29, 16, 30)))
        self.assertFalse(is_realtime_order_time(datetime(2026, 5, 29, 20, 0)))
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 8, 0)), settings.idle_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 3, 59, 50)), settings.idle_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 9, 29, 45)), 15)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 10, 0)), settings.regular_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 29, 16, 30)), settings.idle_poll_seconds)
        self.assertEqual(next_poll_seconds(settings, datetime(2026, 5, 30, 10, 0)), settings.idle_poll_seconds)

    def test_market_holiday_never_enters_order_windows(self):
        labor_day = datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        near_regular_open = labor_day.replace(hour=9, minute=29, second=45)
        settings = make_settings(Path("."))

        self.assertFalse(is_regular_market_time(labor_day))
        self.assertFalse(is_realtime_order_time(labor_day))
        self.assertFalse(is_premarket_time(labor_day.replace(hour=8)))
        self.assertFalse(is_buy_order_time(labor_day))
        self.assertFalse(regular_open_has_started(labor_day))
        self.assertEqual(next_poll_seconds(settings, near_regular_open), settings.idle_poll_seconds)

    def test_watchlist_generator_filters_strategy_rules(self):
        """MA5 选股要求信号涨幅达标且收盘至少高于 MA5 15%。"""
        now_et = datetime(2026, 1, 21, 10, 0)
        candidates = screen_candidates(
            {
                "PASS": make_screen_bars("PASS", passes=True),
                "CLOSE_NEAR_MA5": make_ma5_dip_close_under_15_pct_bars(),
            },
            now_et,
        )

        self.assertEqual([candidate.symbol for candidate in candidates], ["PASS"])
        candidate = candidates[0]
        self.assertGreater(candidate.gain_pct, 0.15)
        self.assertGreaterEqual(candidate.close / candidate.ma5, 1.15)

    def test_market_data_defaults_to_sip_daily_and_moomoo_realtime(self):
        """日线默认用全市场 SIP，当前价默认用 Moomoo OpenD。"""
        market_data_defaults = signature(AlpacaMarketData.__init__).parameters
        watchlist_defaults = signature(generate_watch_codes).parameters
        settings = make_settings(Path("."))

        self.assertEqual(market_data_defaults["bars_feed"].default, "sip")
        self.assertEqual(market_data_defaults["trade_feed"].default, "iex")
        self.assertEqual(watchlist_defaults["feed"].default, "sip")
        self.assertEqual(settings.realtime_price_source, "moomoo")
        self.assertFalse(settings.allow_fractional_shares)
        self.assertIsInstance(build_realtime_price_source(settings), MoomooRealtimePriceSource)

    def test_common_stock_asset_filter_excludes_special_securities(self):
        """选股池只保留普通股，排除 US_EQUITY 里的权证/单位/ETF/ADR 等。"""
        def asset(symbol, name, tradable=True):
            return type("FakeAsset", (), {"symbol": symbol, "name": name, "tradable": tradable})()

        self.assertTrue(is_common_stock_asset(asset("AAPL", "Apple Inc. Common Stock")))
        self.assertTrue(is_common_stock_asset(asset("XYZ", "Example Ltd. Ordinary Shares")))
        self.assertTrue(is_common_stock_asset(asset("CMTY", "Community Bank System, Inc. Common Stock")))
        self.assertTrue(is_common_stock_asset(asset("SBEV", "Splash Beverage Group, Inc.")))
        self.assertFalse(is_common_stock_asset(asset("GOOGL", "Alphabet Inc. Class A Common Stock")))
        self.assertFalse(is_common_stock_asset(asset("ABCDE", "Example Inc. Common Stock")))
        self.assertFalse(is_common_stock_asset(asset("DGICB", "Donegal Group Inc. Class B Common Stock")))
        self.assertFalse(is_common_stock_asset(asset("ABCD", "Example Inc. Series A Common Stock")))
        self.assertFalse(is_common_stock_asset(asset("AACI", "Armada Acquisition Corp. III Class A Ordinary Share")))
        self.assertFalse(is_common_stock_asset(asset("ACAA", "Averin Capital Acquisition Corp. Class A Ordinary Shares")))
        self.assertFalse(is_common_stock_asset(asset("SPAC", "Example SPAC Class A Ordinary Shares")))
        self.assertFalse(is_common_stock_asset(asset("BLNK", "Example Blank Check Company Common Stock")))
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

    def test_moomoo_realtime_price_source_uses_pre_price_during_premarket(self):
        """盘前快照必须优先用 pre_price，不能用昨收性质的 last_price。"""
        import pandas as pd

        class FakeMM:
            RET_OK = 0

        class FakeQuoteCtx:
            def get_market_snapshot(self, codes):
                return 0, pd.DataFrame(
                    [
                        {
                            "code": codes[0],
                            "last_price": 12.34,
                            "pre_price": 10.25,
                            "open_price": 11.11,
                            "update_time": "2026-05-29 08:15:01.123",
                        }
                    ]
                )

        source = MoomooRealtimePriceSource()
        source.mm = FakeMM()
        source.quote_ctx = FakeQuoteCtx()

        quote = source.latest_price_quote("AAPL", now=datetime(2026, 5, 29, 8, 15))

        self.assertEqual(quote.price, 10.25)
        self.assertEqual(quote.source, "moomoo_snapshot:pre_price")

    def test_market_data_passes_now_to_realtime_price_source(self):
        """行情聚合层要把当前市场时间传给实时价源，以便盘前选择 pre_price。"""
        source = FakeRealtimePriceSource(price=10.25, source="moomoo_snapshot:pre_price")
        market_data = AlpacaMarketData.__new__(AlpacaMarketData)
        market_data.realtime_price_source = source
        now_et = datetime(2026, 5, 29, 8, 15)

        price, price_source, _, _ = market_data._current_price("US.AAPL", "AAPL", now_et)

        self.assertEqual(price, 10.25)
        self.assertEqual(price_source, "moomoo_snapshot:pre_price")
        self.assertEqual(source.now, now_et)

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

    def test_watchlist_atomic_replace_failure_preserves_previous_pool(self):
        """生成完成前替换失败时，实盘仍只能看到上一份完整 WatchCode。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_codes.txt"
            previous = "# signal_date=2026-01-20\nUS.OLD\n"
            path.write_text(previous, encoding="utf-8")

            with patch("alpaca_ma5_service.watchlist_generator.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_watch_codes(path, [])

            self.assertEqual(path.read_text(encoding="utf-8"), previous)
            self.assertEqual(list(path.parent.glob(".watch_codes.txt.*.tmp")), [])

    def test_auto_monitor_checks_watchcode_signal_date(self):
        """单一监控入口只复用当前会话对应 signal_date 的 watchcode。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_codes.txt"
            path.write_text("# signal_date=2026-05-27\nUS.TEST\n", encoding="utf-8")

            self.assertEqual(expected_signal_date(datetime(2026, 5, 28, 8, 0)), date(2026, 5, 27))
            self.assertTrue(watchcode_ready_for_session(path, datetime(2026, 5, 28, 8, 0)))
            self.assertFalse(watchcode_ready_for_session(path, datetime(2026, 5, 29, 8, 0)))

    def test_intraday_watchcode_requires_current_rule_header(self):
        """日期正确但规则过期的盘中股票池也必须判定为不可用。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_codes.txt"
            now_et = datetime(2026, 5, 28, 8, 0)
            path.write_text(
                f"{watchlist_rules_header()}\n# signal_date=2026-05-27\nUS.TEST\n",
                encoding="utf-8",
            )

            self.assertTrue(watchcode_matches_rules(path))
            self.assertTrue(intraday_watchcode_ready_for_session(path, now_et))
            changed_rules = replace(watchlist_screen_rules(), min_signal_gain_pct=0.151)
            self.assertFalse(intraday_watchcode_ready_for_session(path, now_et, rules=changed_rules))

            path.write_text(
                "# Rules: obsolete strategy\n# signal_date=2026-05-27\nUS.TEST\n",
                encoding="utf-8",
            )
            self.assertFalse(watchcode_matches_rules(path))
            self.assertFalse(intraday_watchcode_ready_for_session(path, now_et))

    def test_direct_intraday_monitor_rejects_old_rule_watchcode_before_run_loop(self):
        """绕过 monitor_auto 直启时，也不能让旧规则股票池进入实盘循环。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.parent.mkdir(parents=True, exist_ok=True)
            settings.watch_codes_file.write_text(
                "# Rules: obsolete strategy\n# signal_date=2026-05-27\nUS.TEST\n",
                encoding="utf-8",
            )
            now_et = datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))

            with patch(
                "alpaca_ma5_service.workflows.monitoring.intraday.build_monitor_settings",
                return_value=settings,
            ):
                with patch(
                    "alpaca_ma5_service.workflows.monitoring.intraday.now_market_time",
                    return_value=now_et,
                ):
                    with patch("alpaca_ma5_service.workflows.monitoring.intraday.run_forever") as run_loop:
                        with self.assertRaisesRegex(RuntimeError, "WatchCode"):
                            run_intraday_monitor()

            run_loop.assert_not_called()

    def test_direct_intraday_monitor_accepts_current_watchcode_and_enters_run_loop(self):
        """当前日期与规则都匹配时，直接盘中入口可以进入受锁保护的循环。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.watch_codes_file.parent.mkdir(parents=True, exist_ok=True)
            settings.watch_codes_file.write_text(
                f"{watchlist_rules_header()}\n# signal_date=2026-05-27\nUS.TEST\n",
                encoding="utf-8",
            )
            now_et = datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))

            with patch(
                "alpaca_ma5_service.workflows.monitoring.intraday.build_monitor_settings",
                return_value=settings,
            ):
                with patch(
                    "alpaca_ma5_service.workflows.monitoring.intraday.now_market_time",
                    return_value=now_et,
                ):
                    with patch("alpaca_ma5_service.workflows.monitoring.intraday.monitor_runtime") as runtime:
                        with patch("alpaca_ma5_service.workflows.monitoring.intraday.run_forever") as run_loop:
                            run_intraday_monitor()

            runtime.assert_called_once_with(settings.output_dir, "monitor_ma5", "intraday")
            run_loop.assert_called_once_with(settings)

    def test_auto_monitor_skips_market_holiday_when_resolving_signal_date(self):
        now_et = datetime(2026, 9, 8, 8, 0, tzinfo=ZoneInfo("America/New_York"))

        self.assertEqual(expected_signal_date(now_et), date(2026, 9, 4))

    def test_auto_monitor_checks_afterhours_watchcode_signal_date_and_threshold(self):
        """自动入口只复用当天盘后 high/low 阈值一致的观察池。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_code_afterhours.txt"
            signal_day = date(2026, 5, 28)
            now_et = datetime(2026, 5, 28, 16, 1, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("US.AAA", signal_day, 10.0, 20.0, 8.0, 12.0, 2.5, 9.6, 10.56)
            write_afterhours_watch_codes(path, [candidate], signal_day, AFTERHOURS_RANGE_RATIO_THRESHOLD)

            self.assertTrue(afterhours_watchcode_ready_for_session(path, now_et))

            write_afterhours_watch_codes(path, [candidate], date(2026, 5, 27), AFTERHOURS_RANGE_RATIO_THRESHOLD)
            self.assertFalse(afterhours_watchcode_ready_for_session(path, now_et))

            write_afterhours_watch_codes(path, [candidate], signal_day, AFTERHOURS_RANGE_RATIO_THRESHOLD + 0.1)
            self.assertFalse(afterhours_watchcode_ready_for_session(path, now_et))

    def test_auto_monitor_generates_afterhours_watchcode_when_missing(self):
        """盘后观察池缺失时，统一入口先生成 watch_code_afterhours.txt。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 16, 1, tzinfo=ZoneInfo("America/New_York"))

            with patch("monitor_auto.build_settings", return_value=settings):
                with patch("monitor_auto.generate_afterhours_monitor_stocks", return_value=[]) as fake_generate:
                    ensure_afterhours_watchcode(now_et)

            fake_generate.assert_called_once()
            self.assertIs(fake_generate.call_args.kwargs["settings"], settings)
            self.assertEqual(fake_generate.call_args.kwargs["now_et"], now_et)

    def test_auto_monitor_prepares_watchcode_for_current_session(self):
        timezone = ZoneInfo("America/New_York")
        cases = [
            (datetime(2026, 5, 28, 8, 0, tzinfo=timezone), "premarket", None),
            (datetime(2026, 5, 28, 10, 0, tzinfo=timezone), "intraday", "ensure_intraday_watchcode"),
            (datetime(2026, 5, 28, 17, 0, tzinfo=timezone), "afterhours", "ensure_afterhours_watchcode"),
        ]
        for now_et, expected_session, expected_function in cases:
            with self.subTest(session=expected_session):
                with patch("monitor_auto.ensure_premarket_watchcode") as premarket:
                    with patch("monitor_auto.ensure_intraday_watchcode") as intraday:
                        with patch("monitor_auto.ensure_afterhours_watchcode") as afterhours:
                            actual = ensure_current_session_watchcode(now_et)
                functions = {
                    "ensure_premarket_watchcode": premarket,
                    "ensure_intraday_watchcode": intraday,
                    "ensure_afterhours_watchcode": afterhours,
                }
                self.assertEqual(actual, expected_session)
                if expected_function is None:
                    premarket.assert_not_called()
                    intraday.assert_not_called()
                    afterhours.assert_not_called()
                else:
                    functions[expected_function].assert_called_once_with(now_et)

    def test_auto_monitor_routes_afterhours_session(self):
        """16:00-20:00 ET 的统一入口进入盘后 high/low 监控。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 16, 1, tzinfo=ZoneInfo("America/New_York"))

            with patch("monitor_auto.build_settings", return_value=settings):
                with patch("monitor_auto.ensure_afterhours_watchcode") as fake_ensure:
                    with patch("monitor_auto.monitor_afterhours") as fake_monitor:
                        run_monitor_auto(now_provider=lambda: now_et, sleep=lambda seconds: None)

            fake_ensure.assert_called_once_with(now_et)
            self.assertTrue(fake_monitor.call_args.kwargs["stop_at_afterhours_end"])
            self.assertIs(fake_monitor.call_args.kwargs["now_provider"](), now_et)

    def test_premarket_watchlist_uses_latest_top_gain_symbols(self):
        """盘前观察池只取最近已收盘交易日涨幅排名靠前的股票。"""
        now_et = datetime(2026, 1, 22, 8, 0)
        signal_day = date(2026, 1, 21)

        def bars(symbol, previous_close, close):
            return [
                DailyBar(symbol, date(2026, 1, 20), previous_close, previous_close, previous_close, previous_close),
                DailyBar(symbol, signal_day, close, close, close, close),
            ]

        candidates = screen_premarket_top_gain_candidates(
            {
                "AAA": bars("AAA", 10.0, 11.0),
                "BBB": bars("BBB", 10.0, 15.0),
                "CCC": bars("CCC", 10.0, 13.0),
            },
            now_et,
            top_count=2,
        )

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch_codes_premarket.txt"
            write_premarket_watch_codes(path, candidates, top_count=2)

            self.assertEqual([candidate.symbol for candidate in candidates], ["US.BBB", "US.CCC"])
            self.assertEqual(read_watch_codes(path), ["US.BBB", "US.CCC"])

    def test_generate_premarket_watch_codes_sends_readable_cloud_message(self):
        """盘前 watchcode 生成完成通知也要分块展示，方便云端 Agent 阅读。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 1, 21)
            bars_by_symbol = {
                "AAA": [
                    DailyBar("AAA", date(2026, 1, 20), 10.0, 10.0, 10.0, 10.0),
                    DailyBar("AAA", signal_day, 12.0, 12.0, 12.0, 12.0),
                ]
            }

            with patch("alpaca_ma5_service.watchlist_generator.fetch_daily_bars", return_value=bars_by_symbol):
                with patch("alpaca_ma5_service.premarket_watchlist.send_console_notification") as notify_mock:
                    candidates = generate_premarket_watch_codes(settings=settings, symbols=["AAA"], top_count=1)

            self.assertEqual([candidate.symbol for candidate in candidates], ["US.AAA"])
            notify_mock.assert_called_once()
            message = notify_mock.call_args.args[0]
            self.assertIn("【盘前 watchcode 生成完成】", message)
            self.assertIn("结论：盘前推荐观察池已更新。", message)
            self.assertIn("生成结果", message)
            self.assertIn("候选数量：1", message)
            self.assertIn("不会下单", message)

    def test_premarket_ma5_recommendation_alerts_near_above_or_below_ma5(self):
        """盘前推荐要求跌幅达标，并允许上方接近 MA5 或低于 MA5。"""
        near = make_snapshot(symbol="US.NEAR", current=10.0, closes=[9.0, 9.0, 9.0, 12.0])
        high = make_snapshot(symbol="US.HIGH", current=10.0, closes=[8.0, 8.0, 8.0, 12.0])
        below = make_snapshot(symbol="US.BELOW", current=9.0, closes=[9.0, 9.0, 9.0, 12.0])
        not_dropped = make_snapshot(symbol="US.NOTDROP", current=10.2, closes=[10.0, 10.0, 10.0, 10.0])

        recommendation = evaluate_premarket_ma5_recommendation(near)
        below_recommendation = evaluate_premarket_ma5_recommendation(below)

        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation.symbol, "US.NEAR")
        self.assertEqual(recommendation.alert_bucket_pct, 3)
        self.assertIsNotNone(below_recommendation)
        self.assertEqual(below_recommendation.symbol, "US.BELOW")
        self.assertEqual(below_recommendation.alert_type, "below_ma5")
        self.assertIsNone(evaluate_premarket_ma5_recommendation(high))
        self.assertIsNone(evaluate_premarket_ma5_recommendation(not_dropped))

    def test_premarket_recommendation_reports_actual_realtime_quote_time(self):
        """Agent 展示的行情时间应来自实时源，而不是程序读取快照的本机时间。"""
        quote_time = datetime(2026, 5, 28, 7, 59, 30, tzinfo=ZoneInfo("America/New_York"))
        snapshot = replace(
            make_snapshot(
                symbol="US.TEST",
                current=10.0,
                closes=[9.0, 9.0, 9.0, 12.0],
                source="moomoo_snapshot:pre_price",
            ),
            current_price_as_of=quote_time,
        )

        recommendation = evaluate_premarket_ma5_recommendation(snapshot)

        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation.as_of, quote_time)

    def test_premarket_cloud_recommendation_message_is_readable(self):
        """发给云端 Agent 的盘前提醒要分块展示核心结论和关键数值。"""
        recommendation = evaluate_premarket_ma5_recommendation(
            make_snapshot(symbol="US.NEAR", current=10.0, closes=[9.0, 9.0, 9.0, 12.0], source="moomoo_snapshot:pre_price")
        )

        message = render_premarket_recommendation_message(recommendation, date(2026, 5, 27))

        self.assertIn("【盘前 MA5 推荐提醒】US.NEAR", message)
        self.assertIn("结论：", message)
        self.assertIn("动作建议：", message)
        self.assertIn("核心价位", message)
        self.assertIn("- 当前价：10.0000", message)
        self.assertIn("- 动态 MA5：9.8000", message)
        self.assertIn("触发依据", message)
        self.assertIn("只提醒，不提交 Alpaca 订单", message)
        self.assertIn("提醒规则", message)

    def test_premarket_monitor_sends_cloud_recommendation_once_per_bucket(self):
        """同一股票同一提醒档当天只发一次；价格更靠近 MA5 时允许再次提醒。"""
        with TemporaryDirectory() as tmp:
            settings = Settings(
                **{
                    **make_settings(Path(tmp)).__dict__,
                    "trade_notify_openclaw_enabled": True,
                    "trade_notify_mode": "cloud",
                    "cloud_notify_webhook_url": "https://example.invalid/hook",
                    "cloud_notify_webhook_secret": "secret",
                }
            )
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-05-27\nUS.TEST\n", encoding="utf-8")
            now_et = datetime(2026, 5, 28, 8, 0, tzinfo=ZoneInfo("America/New_York"))
            state_path = settings.output_dir / "unit_premarket_alert_state.json"

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                first = run_premarket_recommendation_once(
                    settings,
                    market_data=FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=10.0, closes=[9.0, 9.0, 9.0, 12.0], source="moomoo_snapshot:pre_price")}),
                    now=now_et,
                    alert_state_path=state_path,
                )
                duplicate = run_premarket_recommendation_once(
                    settings,
                    market_data=FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=10.0, closes=[9.0, 9.0, 9.0, 12.0], source="moomoo_snapshot:pre_price")}),
                    now=now_et,
                    alert_state_path=state_path,
                )
                closer = run_premarket_recommendation_once(
                    settings,
                    market_data=FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=9.9, closes=[9.0, 9.0, 9.0, 12.0], source="moomoo_snapshot:pre_price")}),
                    now=now_et,
                    alert_state_path=state_path,
                )

            self.assertEqual(first["sent"], 1)
            self.assertEqual(duplicate["sent"], 0)
            self.assertEqual(closer["sent"], 1)
            self.assertEqual(fake_send.call_count, 2)

    def test_premarket_market_data_safety_error_never_sends_alert(self):
        """拆股口径或实时价日期异常时，盘前链路只能报错，不能向 Agent 发推荐。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-07-24\nUS.RCON\n", encoding="utf-8")
            market_data = SafetyBlockingMarketData("unit reverse split transition")
            output = StringIO()

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                with redirect_stdout(output):
                    summary = run_premarket_recommendation_once(
                        settings,
                        market_data=market_data,
                        now=datetime(2026, 7, 27, 8, 0, tzinfo=ZoneInfo("America/New_York")),
                        alert_state_path=settings.output_dir / "unit_premarket_alert_state.json",
                    )

            self.assertEqual(summary["errors"], 1)
            self.assertEqual(summary["alert"], 0)
            self.assertEqual(summary["sent"], 0)
            self.assertEqual(market_data.purposes, [SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION])
            self.assertIn("错误: MarketDataSafetyError: unit reverse split transition", output.getvalue())
            self.assertNotIn("MarketDataSafetyError: MarketDataSafetyError", output.getvalue())
            fake_send.assert_not_called()

    def test_premarket_corporate_action_is_a_safety_hold_not_a_generic_error(self):
        """拆股切换股票应清晰标成公司行动保护，不污染普通数据错误统计。"""
        class CorporateActionMarketData:
            def get_snapshot(self, symbol, *, purpose=SNAPSHOT_PURPOSE_AUTOMATIC):
                self.purpose = purpose
                raise CorporateActionBasisError(symbol, date(2026, 7, 24), 0.4709, 94.18, 200.0)

        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-07-24\nUS.RCON\n", encoding="utf-8")
            market_data = CorporateActionMarketData()
            output = StringIO()

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                with redirect_stdout(output):
                    summary = run_premarket_recommendation_once(
                        settings,
                        market_data=market_data,
                        now=datetime(2026, 7, 27, 8, 0, tzinfo=ZoneInfo("America/New_York")),
                        alert_state_path=settings.output_dir / "unit_premarket_alert_state.json",
                    )

            self.assertEqual(summary["errors"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertEqual(market_data.purpose, SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION)
            self.assertIn("公司行动保护", output.getvalue())
            self.assertIn("RAW/SPLIT 200.0000 倍", output.getvalue())
            fake_send.assert_not_called()

    def test_premarket_monitor_limits_below_ma5_alerts_to_two_per_symbol(self):
        """同一股票低于 MA5 时，当天最多发送两次提醒。"""
        with TemporaryDirectory() as tmp:
            settings = Settings(
                **{
                    **make_settings(Path(tmp)).__dict__,
                    "trade_notify_openclaw_enabled": True,
                    "trade_notify_mode": "cloud",
                    "cloud_notify_webhook_url": "https://example.invalid/hook",
                    "cloud_notify_webhook_secret": "secret",
                }
            )
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-05-27\nUS.TEST\n", encoding="utf-8")
            now_et = datetime(2026, 5, 28, 8, 0, tzinfo=ZoneInfo("America/New_York"))
            state_path = settings.output_dir / "unit_premarket_alert_state.json"
            below_snapshot = make_snapshot("US.TEST", current=9.0, closes=[9.0, 9.0, 9.0, 12.0], source="moomoo_snapshot:pre_price")

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                first = run_premarket_recommendation_once(settings, market_data=FakeMarketData({"US.TEST": below_snapshot}), now=now_et, alert_state_path=state_path)
                second = run_premarket_recommendation_once(settings, market_data=FakeMarketData({"US.TEST": below_snapshot}), now=now_et, alert_state_path=state_path)
                third = run_premarket_recommendation_once(settings, market_data=FakeMarketData({"US.TEST": below_snapshot}), now=now_et, alert_state_path=state_path)

            self.assertEqual(first["sent"], 1)
            self.assertEqual(second["sent"], 1)
            self.assertEqual(third["sent"], 0)
            self.assertEqual(fake_send.call_count, 2)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["alerts"]["US.TEST"]["below_count"], 2)
            self.assertEqual(state["alerts"]["US.TEST"]["last_position"], "below")

    def test_premarket_monitor_sends_cross_up_after_below_ma5(self):
        """价格从 MA5 下方上穿 MA5 时，需要单独发送一次提醒。"""
        with TemporaryDirectory() as tmp:
            settings = Settings(
                **{
                    **make_settings(Path(tmp)).__dict__,
                    "trade_notify_openclaw_enabled": True,
                    "trade_notify_mode": "cloud",
                    "cloud_notify_webhook_url": "https://example.invalid/hook",
                    "cloud_notify_webhook_secret": "secret",
                }
            )
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-05-27\nUS.TEST\n", encoding="utf-8")
            now_et = datetime(2026, 5, 28, 8, 0, tzinfo=ZoneInfo("America/New_York"))
            state_path = settings.output_dir / "unit_premarket_alert_state.json"
            below_snapshot = make_snapshot("US.TEST", current=9.0, closes=[9.0, 9.0, 9.0, 12.0], source="moomoo_snapshot:pre_price")
            above_snapshot = make_snapshot("US.TEST", current=10.0, closes=[9.0, 9.0, 9.0, 12.0], source="moomoo_snapshot:pre_price")

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                below = run_premarket_recommendation_once(settings, market_data=FakeMarketData({"US.TEST": below_snapshot}), now=now_et, alert_state_path=state_path)
                cross = run_premarket_recommendation_once(settings, market_data=FakeMarketData({"US.TEST": above_snapshot}), now=now_et, alert_state_path=state_path)
                duplicate_above = run_premarket_recommendation_once(settings, market_data=FakeMarketData({"US.TEST": above_snapshot}), now=now_et, alert_state_path=state_path)

            self.assertEqual(below["sent"], 1)
            self.assertEqual(cross["sent"], 1)
            self.assertEqual(duplicate_above["sent"], 0)
            self.assertEqual(fake_send.call_count, 2)
            self.assertIn("上穿", fake_send.call_args.args[1][0])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(state["alerts"]["US.TEST"]["cross_up_sent"])
            self.assertEqual(state["alerts"]["US.TEST"]["last_position"], "above")

    def test_premarket_forever_sends_cloud_start_notification(self):
        """盘前监控进程启动时，要先发一条云端启动消息。"""
        with TemporaryDirectory() as tmp:
            settings = Settings(
                **{
                    **make_settings(Path(tmp)).__dict__,
                    "trade_notify_openclaw_enabled": True,
                    "trade_notify_mode": "cloud",
                    "cloud_notify_webhook_url": "https://example.invalid/hook",
                    "cloud_notify_webhook_secret": "secret",
                }
            )
            now_et = datetime(2026, 5, 28, 8, 0, tzinfo=ZoneInfo("America/New_York"))

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                with patch("alpaca_ma5_service.premarket_monitor.build_default_market_data", return_value=FakeMarketData({})):
                    with patch("alpaca_ma5_service.premarket_monitor.run_premarket_recommendation_once", return_value={"watch": 0, "alert": 0, "sent": 0, "hold": 0, "errors": 0}):
                        with redirect_stdout(StringIO()):
                            run_premarket_recommendations_forever(
                                settings,
                                max_loops=1,
                                sleep=lambda _: None,
                                now_provider=lambda: now_et,
                            )

            fake_send.assert_called_once()
            message = fake_send.call_args.args[1][0]
            self.assertIn("开始盘前监控", message)
            self.assertIn("动作：只发送推荐提醒，不提交任何 Alpaca 订单", message)
            self.assertIn("观察范围", message)
            self.assertIn("提醒条件", message)
            self.assertIn("不提交任何 Alpaca 订单", message)
            self.assertEqual(fake_send.call_args.kwargs["context"], "premarket MA5 monitor started")

    def test_premarket_forever_stops_at_regular_open(self):
        """盘前推荐监控到 09:30 ET 后自动退出，不继续睡眠等待。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            times = iter(
                [
                    datetime(2026, 5, 28, 9, 29, 50, tzinfo=ZoneInfo("America/New_York")),
                    datetime(2026, 5, 28, 9, 29, 50, tzinfo=ZoneInfo("America/New_York")),
                    datetime(2026, 5, 28, 9, 30, 0, tzinfo=ZoneInfo("America/New_York")),
                ]
            )
            sleeps: list[int] = []

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages"):
                with patch("alpaca_ma5_service.premarket_monitor.build_default_market_data", return_value=FakeMarketData({})):
                    with patch("alpaca_ma5_service.premarket_monitor.run_premarket_recommendation_once", return_value={"watch": 0, "alert": 0, "sent": 0, "hold": 0, "errors": 0}) as fake_run_once:
                        with redirect_stdout(StringIO()):
                            run_premarket_recommendations_forever(settings, sleep=lambda seconds: sleeps.append(seconds), now_provider=lambda: next(times))

            fake_run_once.assert_called_once()
            self.assertEqual(sleeps, [])
            self.assertFalse(is_premarket_monitor_finished(datetime(2026, 5, 28, 9, 29, 59)))
            self.assertTrue(is_premarket_monitor_finished(datetime(2026, 5, 28, 9, 30)))

    def test_premarket_poll_interval_is_two_minutes(self):
        """盘前推荐监控常规轮询为 120 秒，临近 09:30 时不超过剩余时间。"""
        regular_premarket = datetime(2026, 5, 28, 8, 0, tzinfo=ZoneInfo("America/New_York"))
        near_open = datetime(2026, 5, 28, 9, 29, 30, tzinfo=ZoneInfo("America/New_York"))

        self.assertEqual(premarket_loop_poll_seconds(regular_premarket), 120)
        self.assertEqual(premarket_loop_poll_seconds(near_open), 30)

    def test_premarket_monitor_prints_observation_snapshot_values(self):
        """未触发提醒的观察行也要显示已取得的价格和 MA5 数据。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-05-27\nUS.TEST\n", encoding="utf-8")
            output = StringIO()
            quote_time = datetime(2026, 5, 28, 7, 59, 30, tzinfo=ZoneInfo("America/New_York"))
            market_data = FakeMarketData(
                {
                    "US.TEST": replace(
                        make_snapshot(
                            "US.TEST",
                            current=10.5,
                            closes=[10.0, 10.0, 10.0, 10.0],
                            source="moomoo_snapshot:pre_price",
                        ),
                        current_price_as_of=quote_time,
                    )
                }
            )

            with redirect_stdout(output):
                summary = run_premarket_recommendation_once(
                    settings,
                    market_data=market_data,
                    now=datetime(2026, 5, 28, 8, 0, tzinfo=ZoneInfo("America/New_York")),
                    notify=False,
                    alert_state_path=settings.output_dir / "unit_premarket_alert_state.json",
                )

            text = output.getvalue()
            self.assertEqual(summary["hold"], 1)
            self.assertIn("10.5000", text)
            self.assertIn("10.1000", text)
            self.assertIn("3.96%", text)
            self.assertIn("07:59:30", text)
            self.assertEqual(market_data.purposes, [SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION])

    def test_premarket_monitor_explains_alert_not_sent_outside_premarket(self):
        """不在盘前时段时，不用昨收价计算盘前跌幅，也不检测推荐。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-05-27\nUS.TEST\n", encoding="utf-8")
            output = StringIO()

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                with redirect_stdout(output):
                    summary = run_premarket_recommendation_once(
                        settings,
                        market_data=FakeMarketData({"US.TEST": make_snapshot("US.TEST", current=10.0, closes=[9.0, 9.0, 9.0, 12.0])}),
                        now=datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("America/New_York")),
                        alert_state_path=settings.output_dir / "unit_premarket_alert_state.json",
                    )

            self.assertEqual(summary["alert"], 0)
            self.assertEqual(summary["sent"], 0)
            self.assertEqual(summary["hold"], 1)
            self.assertIn("非盘前时段，盘前跌幅未计算", output.getvalue())
            fake_send.assert_not_called()

    def test_premarket_monitor_requires_realtime_price_for_drop(self):
        """盘前跌幅必须基于实时价；日线收盘价不能冒充盘前当前价。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            watch_path = premarket_watch_codes_path(settings)
            watch_path.write_text("# signal_date=2026-05-27\nUS.TEST\n", encoding="utf-8")
            output = StringIO()
            snapshot = make_snapshot(
                "US.TEST",
                current=10.0,
                closes=[9.0, 9.0, 9.0, 12.0],
                source="alpaca_daily_close:sip",
            )

            with patch("alpaca_ma5_service.premarket_monitor.safe_send_openclaw_messages") as fake_send:
                with redirect_stdout(output):
                    summary = run_premarket_recommendation_once(
                        settings,
                        market_data=FakeMarketData({"US.TEST": snapshot}),
                        now=datetime(2026, 5, 28, 8, 0, tzinfo=ZoneInfo("America/New_York")),
                        alert_state_path=settings.output_dir / "unit_premarket_alert_state.json",
                    )

            self.assertEqual(summary["alert"], 0)
            self.assertEqual(summary["sent"], 0)
            self.assertEqual(summary["hold"], 1)
            text = output.getvalue()
            self.assertIn("无盘前行情", text)
            self.assertIn("显示最近完成日收盘参考价", text)
            self.assertIn("不计算盘前涨跌幅、不发送推荐", text)
            self.assertIn("alpaca:daily:sip", text)
            self.assertNotIn("已调用发送", text)
            fake_send.assert_not_called()

    def test_premarket_table_aligns_cjk_columns(self):
        """盘前监控表按终端显示宽度对齐中文列和中文内容。"""
        from alpaca_ma5_service.premarket_monitor import display_width, format_table_line

        widths = [max(display_width("状态"), display_width("观察")), max(display_width("说明"), display_width("距离动态 MA5 78.95%"))]

        header = format_table_line(["状态", "说明"], widths)
        row = format_table_line(["观察", "距离动态 MA5 78.95%"], widths)

        self.assertEqual(display_width(header.split("|", 1)[0]), display_width(row.split("|", 1)[0]))
        self.assertEqual(header.index("|"), row.index("|"))

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
            self.assertIn("信号日收盘距MA5", html)
            self.assertIn("+25.00%", html)
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

    def test_watchcode_chart_session_selects_watch_file(self):
        """点击刷新图表入口可按盘前/盘中/盘后选择 watchcode 文件，默认盘中。"""
        from watchcode_chart import chart_settings_for_session

        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))

            self.assertEqual(chart_settings_for_session(settings).watch_codes_file.name, "watch_codes.txt")
            self.assertEqual(chart_settings_for_session(settings, "盘前").watch_codes_file.name, "watch_codes_premarket.txt")
            self.assertEqual(chart_settings_for_session(settings, "afterhours").watch_codes_file.name, "watch_code_afterhours.txt")

            with self.assertRaisesRegex(ValueError, "CHART_SESSION"):
                chart_settings_for_session(settings, "unknown")

    def test_watchcode_chart_refresh_uses_selected_session_file(self):
        """刷新图表 wrapper 应把选择的 session 文件传给生成和服务链接路径。"""
        import watchcode_chart

        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            chart_path = settings.output_dir / "watchlist_charts" / "watch_code_daily_kline_latest.html"

            with patch.object(watchcode_chart, "build_settings", return_value=settings):
                with patch.object(watchcode_chart, "refresh_watchlist_chart_from_watch_codes", return_value=chart_path) as refresh:
                    with patch.object(watchcode_chart, "ensure_watchlist_chart_server_running", return_value=8877) as server:
                        with patch.object(watchcode_chart, "watchlist_chart_http_url", return_value="http://127.0.0.1:8877/watch_code_daily_kline_latest.html"):
                            watchcode_chart.refresh_current_watchcode_chart(session="premarket")

            selected_settings = refresh.call_args.kwargs["settings"]
            self.assertEqual(selected_settings.watch_codes_file.name, "watch_codes_premarket.txt")
            self.assertEqual(refresh.call_args.kwargs["lookback_days"], 60)
            server.assert_called_once_with(selected_settings)

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

            def fake_start_server(settings_arg):
                server_started.append(settings_arg)
                return 8877

            with patch(
                "alpaca_ma5_service.watchlist_generator.fetch_daily_bars",
                return_value={"PASS": make_screen_bars("PASS", passes=True)},
            ):
                with patch("alpaca_ma5_service.watchlist_generator.ensure_watchlist_chart_server_running", fake_start_server):
                    with patch(
                        "alpaca_ma5_service.watchlist_generator.watchlist_chart_http_url",
                        side_effect=lambda settings_arg, port=None: f"http://10.0.0.168:{port}/watch_code_daily_kline_latest.html",
                    ) as url_mock:
                        with patch("alpaca_ma5_service.watchlist_generator.send_console_notification") as notify_mock:
                            with redirect_stdout(output):
                                candidates = generate_watch_codes(settings=settings, symbols=["PASS"], lookback_days=60, batch_size=100, feed="sip")

            latest = settings.output_dir / "watchlist_charts" / "watch_code_daily_kline_latest.html"
            self.assertEqual([candidate.symbol for candidate in candidates], ["PASS"])
            self.assertTrue(latest.exists())
            self.assertEqual(server_started, [settings])
            url_mock.assert_called_once_with(settings, port=8877)
            self.assertIn("Watchlist chart HTTP URL: http://10.0.0.168:8877/watch_code_daily_kline_latest.html", output.getvalue())
            notify_mock.assert_called_once()
            notify_message = notify_mock.call_args.args[0]
            self.assertIn("watch_codes 生成完成", notify_message)
            self.assertIn("生成结果", notify_message)
            self.assertIn("候选数量：1", notify_message)
            self.assertIn("图表链接：http://10.0.0.168:8877/watch_code_daily_kline_latest.html", notify_message)
            self.assertEqual(notify_mock.call_args.kwargs["context"], "watchcode generated")
            self.assertEqual(notify_mock.call_args.kwargs["settings"], settings)

    def test_fetch_daily_bars_prints_batch_progress(self):
        output = StringIO()
        now_et = datetime(2026, 7, 10, 16, 30, tzinfo=ZoneInfo("America/New_York"))

        with patch("alpaca_ma5_service.watchlist_generator.load_alpaca_credentials", return_value=("key", "secret")):
            with patch("alpaca.data.historical.StockHistoricalDataClient") as client_class:
                original_request = client_class.return_value._session.request
                client_class.return_value.get_stock_bars.return_value.data = {}
                with redirect_stdout(output):
                    result = fetch_daily_bars(["AAA", "BBB", "CCC"], now_et, 60, 2, "sip")

                client_class.return_value._session.request("GET", "https://data.example.test")

        self.assertEqual(result, {})
        self.assertEqual(client_class.return_value.get_stock_bars.call_count, 2)
        original_request.assert_called_once_with(
            "GET",
            "https://data.example.test",
            timeout=(10.0, 45.0),
        )
        self.assertIn("日线读取进度：1/2", output.getvalue())
        self.assertIn("日线读取进度：2/2 完成", output.getvalue())
        self.assertIn("日线读取完成：0/3 只", output.getvalue())

    def test_watchlist_chart_server_uses_alternate_port_when_default_is_stale(self):
        """默认端口被其它服务占用时，应启动当前图表服务到新端口并返回该端口。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))

            with patch("alpaca_ma5_service.watchlist_charts.watchlist_chart_server_ready", return_value=False):
                with patch("alpaca_ma5_service.watchlist_charts.tcp_port_is_open", return_value=True):
                    with patch("alpaca_ma5_service.watchlist_charts.find_running_watchlist_chart_server", return_value=None):
                        with patch("alpaca_ma5_service.watchlist_charts.find_available_tcp_port", return_value=8767):
                            with patch("alpaca_ma5_service.watchlist_charts.wait_for_watchlist_chart_server_ready", return_value=True):
                                with patch("alpaca_ma5_service.watchlist_charts.subprocess.Popen") as popen:
                                    port = ensure_watchlist_chart_server_running(settings)

            self.assertEqual(port, 8767)
            self.assertEqual(popen.call_args.kwargs["env"]["WATCHLIST_CHART_LAN_PORT"], "8767")

    def test_watchlist_chart_server_reuses_existing_alternate_port(self):
        """默认端口被旧服务占用但已有当前项目服务时，应复用已有新端口。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))

            with patch("alpaca_ma5_service.watchlist_charts.watchlist_chart_server_ready", return_value=False):
                with patch("alpaca_ma5_service.watchlist_charts.tcp_port_is_open", return_value=True):
                    with patch("alpaca_ma5_service.watchlist_charts.find_running_watchlist_chart_server", return_value=8767):
                        with patch("alpaca_ma5_service.watchlist_charts.subprocess.Popen") as popen:
                            port = ensure_watchlist_chart_server_running(settings)

            self.assertEqual(port, 8767)
            popen.assert_not_called()

    def test_watchlist_generator_rejects_weak_close_ma5_ratio_before_write(self):
        """写入前再次强校验，信号日收盘价必须比 MA5 高 15 个点以上。"""
        candidate = WatchCandidate("BAD", date(2026, 1, 20), 0.3, 0.1, 10.0, 9.0, 8.0, 11.0, 13.0, 10.5)

        with self.assertRaisesRegex(RuntimeError, "close/MA5>=1.15"):
            validate_candidates([candidate])

    def test_watchlist_generator_allows_red_body_before_write(self):
        """写入前不再要求信号日收阳。"""
        candidate = WatchCandidate("OK", date(2026, 1, 20), 0.3, 0.1, 10.0, 9.0, 8.0, 13.0, 13.5, 12.5)

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
    def test_afterhours_monitor_note_column_keeps_full_text(self):
        """盘后监控表的说明列也不省略原因。"""
        note = "完整说明：当前跌幅未超过阈值；当前价大于信号价；等待下一次信号，不能省略"
        buffer = StringIO()

        with redirect_stdout(buffer):
            print_afterhours_table_row(
                BUY_MONITOR_COLUMNS,
                {
                    "symbol": "US.AAA",
                    "current": "16.2000",
                    "source": "moomoo:last",
                    "close": "20.0000",
                    "drop": "19.00%",
                    "limit": "16.0000",
                    "qty": "212",
                    "status": "等待信号",
                    "note": note,
                },
            )

        output = buffer.getvalue()
        self.assertIn(note, output)
        self.assertNotIn("...", output)

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

    def test_afterhours_windows_and_signal_day_skip_market_holiday(self):
        labor_day_after_close = datetime(2026, 9, 7, 16, 30, tzinfo=ZoneInfo("America/New_York"))

        self.assertFalse(is_regular_session(labor_day_after_close.replace(hour=10)))
        self.assertFalse(is_afterhours_buy_time(labor_day_after_close))
        self.assertEqual(afterhours_signal_day(labor_day_after_close), date(2026, 9, 4))

    def test_afterhours_screen_uses_regular_high_low_ratio(self):
        """常规盘 high/low > 1.8 才进入盘后候选，并按 close*0.8 算买入价。"""
        bars_by_symbol = {
            "AAA": [
                make_minute_bar("AAA", 9, 30, open=10.0, high=12.0, low=10.0, close=11.0),
                make_minute_bar("AAA", 15, 59, open=19.0, high=26.0, low=18.0, close=20.0),
            ],
            "BBB": [
                make_minute_bar("BBB", 9, 30, open=10.0, high=12.0, low=10.0, close=11.0),
                make_minute_bar("BBB", 15, 59, open=15.0, high=17.0, low=15.0, close=16.0),
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
        """当前跌幅未超过 15% 时不提交订单，只等待下一次信号。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = FakeAlpacaClient()
            connection = type("FakeConnection", (), {"paper": True, "client": client})()
            output = StringIO()

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote", return_value=(17.1, "moomoo_snapshot:last_price")):
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
        """当前跌幅超过 15% 时才提交订单，并使用 300 秒未成交撤单保护。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)
            client = FakeAlpacaClient(fractionable=False)
            expected = OrderResult("order-1", "US.AAA", "BUY", 212.0, 16.0, "CANCELED", "not filled; cancel requested")
            connection = type("FakeConnection", (), {"paper": True, "client": client})()

            with patch("alpaca_ma5_service.afterhours_high_low.build_trading_connection", return_value=connection):
                with patch("alpaca_ma5_service.afterhours_high_low.latest_trade_price_quote", return_value=(16.2, "moomoo_snapshot:last_price")):
                    with patch(
                        "alpaca_ma5_service.strategy_framework.builtins.TimeoutCancelConfirmedStrategy.wait_for_terminal",
                        return_value=expected,
                    ) as fake_wait:
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
                "盘后 high/low>1.8 买入；range=2.6",
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
        """盘后入口保留旧参数兼容，但运行时只提醒不下单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

            with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
                with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]):
                    with patch("alpaca_ma5_service.afterhours_monitor.build_afterhours_price_source", return_value=None):
                        with patch("alpaca_ma5_service.afterhours_monitor.latest_trade_price_quote", return_value=(16.2, "moomoo_snapshot:last_price")):
                            with patch("alpaca_ma5_service.afterhours_monitor.safe_send_openclaw_messages") as fake_send:
                                with patch("alpaca_ma5_service.afterhours_high_low.submit_afterhours_limit_buys") as fake_submit:
                                    run_afterhours_high_low_buyer(require_paper=False, max_loops=1, sleep=lambda seconds: None, now_provider=lambda: now_et)

        fake_submit.assert_not_called()
        contexts = [call.kwargs["context"] for call in fake_send.call_args_list]
        self.assertIn("afterhours high/low monitor started", contexts)
        self.assertIn("afterhours high/low scan result", contexts)
        self.assertIn("afterhours high/low alert signal", contexts)

    def test_afterhours_entry_keeps_monitoring_after_daily_scan(self):
        """入口默认持续监控：当天只扫一次池，下一轮复用候选池检查提醒信号。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

            with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
                with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]) as fake_scan:
                    with patch("alpaca_ma5_service.afterhours_monitor.build_afterhours_price_source", return_value=None):
                        with patch("alpaca_ma5_service.afterhours_monitor.latest_trade_price_quote", return_value=(17.1, "moomoo_snapshot:last_price")) as fake_price:
                            with patch("alpaca_ma5_service.afterhours_high_low.submit_afterhours_limit_buys") as fake_submit:
                                run_afterhours_high_low_buyer(require_paper=True, max_loops=2, sleep=lambda seconds: None, now_provider=lambda: now_et)

        fake_scan.assert_called_once()
        self.assertEqual(fake_price.call_count, 2)
        fake_submit.assert_not_called()

    def test_afterhours_monitor_keeps_watching_after_canceled_order(self):
        """触发提醒后继续监控价格，但同一轮盘后不重复推送同一只股票。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

            with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
                with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]):
                    with patch("alpaca_ma5_service.afterhours_monitor.build_afterhours_price_source", return_value=None):
                        with patch("alpaca_ma5_service.afterhours_monitor.latest_trade_price_quote", return_value=(16.2, "moomoo_snapshot:last_price")):
                            with patch("alpaca_ma5_service.afterhours_monitor.safe_send_openclaw_messages") as fake_send:
                                with patch("alpaca_ma5_service.afterhours_high_low.submit_afterhours_limit_buys") as fake_submit:
                                    run_afterhours_high_low_buyer(require_paper=True, max_loops=2, sleep=lambda seconds: None, now_provider=lambda: now_et)

        fake_submit.assert_not_called()
        contexts = [call.kwargs["context"] for call in fake_send.call_args_list]
        self.assertEqual(contexts.count("afterhours high/low alert signal"), 1)

    def test_afterhours_monitor_skips_symbol_after_filled_buy(self):
        """盘后提醒模式不再读取已买入状态，也不会因为本地成交记录去下单。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

            with patch("alpaca_ma5_service.afterhours_monitor.build_settings", return_value=settings):
                with patch("alpaca_ma5_service.afterhours_monitor.load_afterhours_bought_symbols", side_effect=AssertionError("should not read bought symbols")):
                    with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]):
                        with patch("alpaca_ma5_service.afterhours_monitor.build_afterhours_price_source", return_value=None):
                            with patch("alpaca_ma5_service.afterhours_monitor.latest_trade_price_quote", return_value=(17.1, "moomoo_snapshot:last_price")):
                                with patch("alpaca_ma5_service.afterhours_high_low.submit_afterhours_limit_buys") as fake_submit:
                                    run_afterhours_high_low_buyer(require_paper=True, max_loops=2, sleep=lambda seconds: None, now_provider=lambda: now_et)

        fake_submit.assert_not_called()

    def test_afterhours_restore_bought_symbols_ignores_canceled_orders(self):
        """重启恢复时撤单不算；只有实际成交的盘后买单会跳过。"""
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            signal_day = date(2026, 5, 28)
            append_order(
                settings.output_dir,
                OrderResult("cancel-1", "US.AAA", "BUY", 1.0, 16.0, "CANCELED", "not filled"),
                "盘后 high/low>1.8 买入；range=2.6",
                day=signal_day,
                created_at=datetime(2026, 5, 28, 20, 15, tzinfo=ZoneInfo("America/New_York")),
            )
            append_order(
                settings.output_dir,
                OrderResult("fill-1", "US.BBB", "BUY", 1.0, 16.0, "FILLED", "filled"),
                "盘后 high/low>1.8 买入；range=2.6",
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
                "盘后 high/low>1.8 买入；range=2.6",
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
                "盘后 high/low>1.8 买入；range=2.6",
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

    def test_afterhours_start_message_uses_readable_15pct_signal(self):
        """盘后启动报告要清楚写出 high/low 筛选和 15% 跌幅提醒线。"""
        settings = make_settings(Path("."))
        now_et = datetime(2026, 5, 28, 16, 1, tzinfo=ZoneInfo("America/New_York"))

        message = render_afterhours_monitor_start_message(settings, now_et, require_paper=False)

        self.assertIn("盘后 high/low 监控启动", message)
        self.assertIn("high / low > 1.8", message)
        self.assertIn("跌幅 > 15%", message)
        self.assertIn("只发送提醒", message)
        self.assertIn("不创建订单", message)
        self.assertNotIn("满足信号会提交 Alpaca", message)

    def test_afterhours_scan_result_sends_cloud_message(self):
        """盘后筛选出候选池后要把结果发给云端 agent。"""
        with TemporaryDirectory() as tmp:
            settings = Settings(
                **{
                    **make_settings(Path(tmp)).__dict__,
                    "trade_notify_openclaw_enabled": True,
                    "trade_notify_mode": "cloud",
                    "cloud_notify_webhook_url": "https://example.invalid/hook",
                    "cloud_notify_webhook_secret": "secret",
                }
            )
            now_et = datetime(2026, 5, 28, 16, 1, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

            with patch("alpaca_ma5_service.afterhours_monitor.scan_afterhours_candidates", return_value=[candidate]):
                with patch("alpaca_ma5_service.afterhours_monitor.safe_send_openclaw_messages") as fake_send:
                    candidates = generate_afterhours_monitor_stocks(settings=settings, now_et=now_et)

            self.assertEqual(candidates, [candidate])
            fake_send.assert_called_once()
            message = fake_send.call_args.args[1][0]
            self.assertIn("盘后 high/low 筛选结果", message)
            self.assertIn("筛选出 1 只", message)
            self.assertIn("US.AAA", message)
            self.assertIn("跌幅 > 15%", message)
            self.assertEqual(fake_send.call_args.kwargs["context"], "afterhours high/low scan result")

    def test_afterhours_buy_signal_sends_cloud_message(self):
        """盘后提醒信号触发时只发云端消息，不提交 Alpaca 订单。"""
        with TemporaryDirectory() as tmp:
            settings = Settings(
                **{
                    **make_settings(Path(tmp)).__dict__,
                    "trade_notify_openclaw_enabled": True,
                    "trade_notify_mode": "cloud",
                    "cloud_notify_webhook_url": "https://example.invalid/hook",
                    "cloud_notify_webhook_secret": "secret",
                }
            )
            now_et = datetime(2026, 5, 28, 19, 15, tzinfo=ZoneInfo("America/New_York"))
            candidate = AfterHoursCandidate("AAA", date(2026, 5, 28), 10.0, 26.0, 10.0, 20.0, 2.6, 16.0, 17.6)

            with patch("alpaca_ma5_service.afterhours_monitor.build_afterhours_price_source", return_value=None):
                with patch("alpaca_ma5_service.afterhours_monitor.latest_trade_price_quote", return_value=(16.2, "moomoo_snapshot:last_price")):
                    with patch("alpaca_ma5_service.afterhours_monitor.safe_send_openclaw_messages") as fake_send:
                        with patch("alpaca_ma5_service.afterhours_high_low.submit_afterhours_limit_buys") as fake_submit:
                            monitor_afterhours_buy_signals(settings, [candidate], now_et, require_paper=True, alerted_symbols=set())

            fake_submit.assert_not_called()
            fake_send.assert_called_once()
            message = fake_send.call_args.args[1][0]
            self.assertIn("盘后 high/low 提醒", message)
            self.assertIn("US.AAA", message)
            self.assertIn("超过 15%", message)
            self.assertIn("只提醒，不下单", message)
            self.assertEqual(fake_send.call_args.kwargs["context"], "afterhours high/low alert signal")

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
