from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .alpaca_connection import load_alpaca_credentials
from .market_time import is_realtime_order_time
from .models import MarketSnapshot
from .watchlist import to_alpaca_symbol


class AlpacaMarketData:
    """使用 Alpaca Market Data 读取股票当前价和日线。"""

    def __init__(self, market_timezone: str = "America/New_York", bars_feed: str = "sip", trade_feed: str = "iex"):
        """初始化 Alpaca 行情 client；日线用 SIP，实时当前价用 IEX。"""
        from alpaca.data.historical import StockHistoricalDataClient

        api_key, secret_key = load_alpaca_credentials()
        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.market_tz = ZoneInfo(market_timezone)
        self.bars_feed = bars_feed
        self.trade_feed = trade_feed

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        """获取当前价和前 4 个完成交易日收盘价，供监控策略计算 MA5。"""
        alpaca_symbol = to_alpaca_symbol(symbol)
        now = datetime.now(self.market_tz)
        bars = self._daily_bars(alpaca_symbol, now)
        latest_trade_price = self._latest_trade_price(alpaca_symbol) if _requires_realtime_price(now) else 0.0
        current_price, completed_closes = _snapshot_inputs(bars, now, latest_trade_price)
        if current_price <= 0:
            raise RuntimeError(f"{symbol} 当前价格无效")
        if len(completed_closes) < 4:
            raise RuntimeError(f"{symbol} 少于 4 个已完成日线收盘价")
        return MarketSnapshot(symbol=symbol, current_price=current_price, previous_closes=completed_closes[-4:], as_of=now)

    def _daily_bars(self, symbol: str, now: datetime):
        """读取 Alpaca SIP 日线。"""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = _daily_request_end(now)
        start = end - timedelta(days=20)
        request = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.SPLIT,
            feed=DataFeed(self.bars_feed),
        )
        return [
            _SnapshotBar(bar.timestamp.astimezone(self.market_tz).date(), float(bar.close))
            for bar in self.client.get_stock_bars(request).data.get(symbol, [])
        ]

    def _latest_trade_price(self, symbol: str) -> float:
        """读取 Alpaca IEX 最新成交价；可交易时段拿不到就让本轮跳过该股票。"""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        feed_label = self.trade_feed.upper()
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=[symbol], feed=DataFeed(self.trade_feed))
            trade = self.client.get_stock_latest_trade(request).get(symbol)
            price = float(getattr(trade, "price", 0.0) or 0.0)
        except Exception as exc:
            raise RuntimeError(f"{symbol} 无法读取 {feed_label} 实时成交价：{exc}") from exc
        if price <= 0:
            raise RuntimeError(f"{symbol} {feed_label} 实时成交价无效")
        return price


class _SnapshotBar:
    """内部轻量日线对象，只保存日期和收盘价。"""

    def __init__(self, date, close: float):
        """保存一根日线的完成日期和收盘价。"""
        self.date = date
        self.close = close


def _daily_request_end(now: datetime) -> datetime:
    """日线请求使用日期边界，避免 SIP recent 查询限制。"""
    if now.weekday() < 5 and (now.hour > 16 or (now.hour == 16 and now.minute >= 15)):
        end_date = now.date() + timedelta(days=1)
    else:
        end_date = now.date()
    return datetime.combine(end_date, time.min, tzinfo=now.tzinfo)


def _snapshot_inputs(bars: list[_SnapshotBar], now: datetime, latest_trade_price: float) -> tuple[float, list[float]]:
    """按交易时段选择 current_price，并返回它之前的 4 个完成收盘价。"""
    if latest_trade_price > 0 and _requires_realtime_price(now):
        return latest_trade_price, [bar.close for bar in bars if bar.date < now.date() and bar.close > 0]
    if not bars:
        return 0.0, []
    current_bar = bars[-1]
    previous_closes = [bar.close for bar in bars if bar.date < current_bar.date and bar.close > 0]
    return current_bar.close, previous_closes


def _requires_realtime_price(now: datetime) -> bool:
    """美股可交易时段必须使用实时成交价，不能用日线 close 冒充当前价。"""
    return is_realtime_order_time(now)
