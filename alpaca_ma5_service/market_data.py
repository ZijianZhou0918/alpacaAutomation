from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .alpaca_connection import load_alpaca_credentials
from .config import Settings
from .errors import short_error
from .market_time import is_realtime_order_time, regular_open_has_started
from .models import MarketSnapshot
from .moomoo_market_data import MoomooRealtimePriceSource
from .watchlist import normalize_symbol, to_alpaca_symbol


class AlpacaMarketData:
    """组合行情源：Alpaca 读日线，外部实时源读当前价。"""

    def __init__(
        self,
        market_timezone: str = "America/New_York",
        bars_feed: str = "sip",
        trade_feed: str = "iex",
        realtime_price_source=None,
    ):
        """初始化 Alpaca 日线 client；实时价优先由 Moomoo 注入。"""
        from alpaca.data.historical import StockHistoricalDataClient

        api_key, secret_key = load_alpaca_credentials()
        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.market_tz = ZoneInfo(market_timezone)
        self.bars_feed = bars_feed
        self.trade_feed = trade_feed
        self.realtime_price_source = realtime_price_source
        self._last_daily_feed = bars_feed.lower()

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        """返回监控所需的当前价、今日开盘价和前 4 个完成日收盘价。"""
        normalized_symbol = normalize_symbol(symbol)
        alpaca_symbol = to_alpaca_symbol(symbol)
        now = datetime.now(self.market_tz)
        bars = self._daily_bars(alpaca_symbol, now)
        latest_trade_price, current_price_source, today_open, today_open_source = self._current_price(normalized_symbol, alpaca_symbol, now)
        current_price, completed_closes = _snapshot_inputs(bars, now, latest_trade_price)
        completed_opens = _snapshot_previous_opens(bars, now)
        if not current_price_source:
            current_price_source = f"alpaca_daily_close:{self._last_daily_feed}"
        today_open, today_open_source = _usable_today_open(now, today_open, today_open_source)
        if today_open <= 0:
            today_open, today_open_source = _snapshot_today_open(bars, now, f"alpaca_daily_open:{self._last_daily_feed}")
        if current_price <= 0:
            raise RuntimeError(f"{symbol} 当前价格无效")
        if len(completed_closes) < 4:
            raise RuntimeError(f"{symbol} 少于 4 个已完成日线收盘价")
        return MarketSnapshot(
            symbol=normalized_symbol,
            current_price=current_price,
            previous_closes=completed_closes[-4:],
            as_of=now,
            current_price_source=current_price_source,
            today_open=today_open,
            today_open_source=today_open_source,
            previous_opens=completed_opens[-4:],
        )

    def _current_price(self, normalized_symbol: str, alpaca_symbol: str, now: datetime) -> tuple[float, str, float, str]:
        """优先读注入的实时源；未配置时才回退到 Alpaca latest trade。"""
        if self.realtime_price_source is not None:
            if hasattr(self.realtime_price_source, "latest_price_quote"):
                quote = self.realtime_price_source.latest_price_quote(normalized_symbol)
                return quote.price, quote.source, getattr(quote, "today_open", 0.0), getattr(quote, "today_open_source", "")
            return self.realtime_price_source.latest_price(normalized_symbol), type(self.realtime_price_source).__name__, 0.0, ""
        if not _requires_realtime_price(now):
            return 0.0, "", 0.0, ""
        return self._latest_trade_price(alpaca_symbol), f"alpaca_latest_trade:{self.trade_feed.lower()}", 0.0, ""

    def _daily_bars(self, symbol: str, now: datetime):
        """读取 Alpaca 日线；SIP 权限失败时降级 IEX，避免监控中断。"""
        try:
            self._last_daily_feed = self.bars_feed.lower()
            return self._fetch_daily_bars(symbol, now, self.bars_feed)
        except Exception as exc:
            if self.bars_feed.lower() == "iex":
                raise
            print(f"{symbol}: {self.bars_feed.upper()} 日线读取失败，改用 IEX。{short_error(exc)}", flush=True)
            self._last_daily_feed = "iex"
            return self._fetch_daily_bars(symbol, now, "iex")

    def _fetch_daily_bars(self, symbol: str, now: datetime, feed: str):
        """按指定 feed 读取拆股调整后的日线。"""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = _daily_request_end(now, feed)
        start = end - timedelta(days=20)
        request = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.SPLIT,
            feed=DataFeed(feed.lower()),
        )
        return [
            _SnapshotBar(bar.timestamp.astimezone(self.market_tz).date(), float(bar.close), float(bar.open))
            for bar in self.client.get_stock_bars(request).data.get(symbol, [])
        ]

    def _latest_trade_price(self, symbol: str) -> float:
        """读取 Alpaca latest trade；仅作为未启用 Moomoo 时的兜底。"""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        feed_label = self.trade_feed.upper()
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=[symbol], feed=DataFeed(self.trade_feed.lower()))
            trade = self.client.get_stock_latest_trade(request).get(symbol)
            price = float(getattr(trade, "price", 0.0) or 0.0)
        except Exception as exc:
            raise RuntimeError(f"{symbol} 无法读取 {feed_label} 实时成交价：{exc}") from exc
        if price <= 0:
            raise RuntimeError(f"{symbol} {feed_label} 实时成交价无效")
        return price

    def close(self) -> None:
        """关闭外部实时行情连接，避免点箭头脚本结束后进程卡住。"""
        if self.realtime_price_source is not None and hasattr(self.realtime_price_source, "close"):
            self.realtime_price_source.close()


class _SnapshotBar:
    """内部轻量日线对象，只保留策略需要的字段。"""

    def __init__(self, date, close: float, open: float = 0.0):
        """保存一根日线的日期、收盘价和开盘价。"""
        self.date = date
        self.close = close
        self.open = open


def _daily_request_end(now: datetime, feed: str = "sip") -> datetime:
    """计算日线请求 end；SIP 需要避开最近数据权限窗口。"""
    if now.weekday() < 5 and (now.hour > 16 or (now.hour == 16 and now.minute >= 15)):
        end_date = now.date() + timedelta(days=1)
    else:
        end_date = now.date()
    boundary = datetime.combine(end_date, time.min, tzinfo=now.tzinfo)
    return _stale_sip_end(now, boundary) if feed.lower() == "sip" else boundary


def _stale_sip_end(now: datetime, boundary: datetime) -> datetime:
    """把 SIP 请求时间压到 20 分钟前，避开 recent data 权限限制。"""
    stale_cutoff = now - timedelta(minutes=20)
    if boundary <= stale_cutoff:
        return boundary
    close_ready = datetime.combine(now.date(), time(16, 15), tzinfo=now.tzinfo)
    if stale_cutoff.date() == now.date() and stale_cutoff < close_ready:
        return datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    return stale_cutoff


def _snapshot_inputs(bars: list[_SnapshotBar], now: datetime, latest_trade_price: float) -> tuple[float, list[float]]:
    """确定动态 MA5 的当前价和参与计算的完成日收盘价。"""
    if latest_trade_price > 0:
        return latest_trade_price, [bar.close for bar in bars if bar.date < now.date() and bar.close > 0]
    if not bars:
        return 0.0, []
    current_bar = bars[-1]
    previous_closes = [bar.close for bar in bars if bar.date < current_bar.date and bar.close > 0]
    return current_bar.close, previous_closes


def _snapshot_previous_opens(bars: list[_SnapshotBar], now: datetime) -> list[float]:
    """取今日开盘 MA5 需要的前 4 个完成日开盘价。"""
    return [bar.open for bar in bars if bar.date < now.date() and bar.open > 0]


def _snapshot_today_open(bars: list[_SnapshotBar], now: datetime, source: str) -> tuple[float, str]:
    """实时源没有今日开盘价时，用今日日线 open 兜底。"""
    if not regular_open_has_started(now):
        return 0.0, ""
    today_bars = [bar for bar in bars if bar.date == now.date() and getattr(bar, "open", 0.0) > 0]
    if not today_bars:
        return 0.0, ""
    return float(today_bars[-1].open), source


def _usable_today_open(now: datetime, today_open: float, today_open_source: str) -> tuple[float, str]:
    """盘前还没有常规盘开盘价，忽略行情源提前给出的 open。"""
    if not regular_open_has_started(now) or today_open <= 0:
        return 0.0, ""
    return today_open, today_open_source


def _requires_realtime_price(now: datetime) -> bool:
    """可交易窗口内必须有实时价，不能用日线 close 冒充当前价。"""
    return is_realtime_order_time(now)


def build_market_data(settings: Settings) -> AlpacaMarketData:
    """构建真实监控行情源：Moomoo 当前价 + Alpaca 日线。"""
    return AlpacaMarketData(settings.market_timezone, realtime_price_source=build_realtime_price_source(settings))


def build_realtime_price_source(settings: Settings):
    """构建监控和测试脚本共用的实时价格源。"""
    source_name = settings.realtime_price_source.lower()
    if source_name == "moomoo":
        return MoomooRealtimePriceSource(
            host=settings.moomoo_host,
            port=settings.moomoo_port,
            security_firm=settings.moomoo_security_firm,
            connect_timeout=settings.moomoo_connect_timeout,
            opend_exe_path=settings.moomoo_opend_exe_path,
            opend_startup_timeout=settings.moomoo_opend_startup_timeout,
        )
    if source_name == "alpaca":
        return None
    raise RuntimeError(f"Unknown REALTIME_PRICE_SOURCE={settings.realtime_price_source}")
