from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .alpaca_connection import load_alpaca_credentials
from .config import Settings
from .errors import short_error
from .market_time import (
    daily_request_end,
    is_premarket_time,
    is_realtime_order_time,
    is_regular_market_time,
    regular_open_has_started,
    stale_sip_daily_end,
)
from .models import MarketSnapshot
from .moomoo_market_data import MoomooRealtimePriceSource
from .watchlist import normalize_symbol, to_alpaca_symbol


REGULAR_REALTIME_MAX_AGE = timedelta(minutes=5)
EXTENDED_REALTIME_MAX_AGE = timedelta(minutes=30)
REALTIME_FUTURE_TOLERANCE = timedelta(minutes=2)
ADJUSTMENT_FACTOR_REL_TOLERANCE = 0.005
SNAPSHOT_PURPOSE_AUTOMATIC = "automatic"
SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION = "premarket_observation"
VALID_SNAPSHOT_PURPOSES = {
    SNAPSHOT_PURPOSE_AUTOMATIC,
    SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION,
}
PREMARKET_SESSION_OPEN = time(4, 0)


class MarketDataSafetyError(RuntimeError):
    """行情日期或公司行动口径无法证明一致时，阻断该股票自动动作。"""


class CorporateActionBasisError(MarketDataSafetyError):
    """最近完成日线 RAW/SPLIT 口径不同，当前交易日不得混价。"""

    def __init__(self, symbol: str, bar_date, raw_close: float, split_close: float, factor: float):
        self.symbol = symbol
        self.bar_date = bar_date
        self.raw_close = raw_close
        self.split_close = split_close
        self.factor = factor
        super().__init__(
            f"{symbol} 最新完成日线 {bar_date} 正在切换公司行动口径："
            f"RAW={raw_close:.6f}，SPLIT={split_close:.6f}，倍数={factor:.6f}；"
            "已禁止本轮盘前提醒及盘中买入、补仓、止盈、止损"
        )


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
        self._last_realtime_as_of: datetime | None = None

    def get_snapshot(
        self,
        symbol: str,
        *,
        purpose: str = SNAPSHOT_PURPOSE_AUTOMATIC,
    ) -> MarketSnapshot:
        """返回监控所需的当前价、今日开盘价和前 4 个完成日收盘价。"""
        if purpose not in VALID_SNAPSHOT_PURPOSES:
            raise ValueError(f"未知行情快照用途：{purpose}")
        normalized_symbol = normalize_symbol(symbol)
        alpaca_symbol = to_alpaca_symbol(symbol)
        now = datetime.now(self.market_tz)
        bars, raw_bars = self._daily_bars_pair(alpaca_symbol, now)
        validate_latest_completed_bar_basis(normalized_symbol, bars, raw_bars, now)
        try:
            latest_trade_price, current_price_source, today_open, today_open_source = self._current_price(
                normalized_symbol,
                alpaca_symbol,
                now,
                require_current_session=True,
                allow_sparse_premarket=(purpose == SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION),
            )
        except Exception:
            if purpose != SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION:
                raise
            # 盘前推荐只观察、不下单。若当日 04:00 后完全没有成交或报价，
            # 允许返回日线参考快照；调用方必须识别 daily source，不得计算
            # 盘前涨跌幅或发送推荐。自动交易用途永远不会进入这个降级分支。
            latest_trade_price = 0.0
            current_price_source = ""
            today_open = 0.0
            today_open_source = ""
            self._last_realtime_as_of = None
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
            current_price_as_of=self._last_realtime_as_of,
            signal_day_gain_pct_override=(
                _latest_completed_gain_pct(bars, now)
                if purpose == SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION and not current_price_source.startswith(("moomoo_snapshot:", "alpaca_latest_quote:", "alpaca_latest_trade:"))
                else None
            ),
        )

    def _current_price(
        self,
        normalized_symbol: str,
        alpaca_symbol: str,
        now: datetime,
        *,
        require_current_session: bool = False,
        allow_sparse_premarket: bool = False,
    ) -> tuple[float, str, float, str]:
        """优先读注入的实时源；未配置时才回退到 Alpaca latest trade。"""
        self._last_realtime_as_of = None
        if self.realtime_price_source is not None:
            try:
                if hasattr(self.realtime_price_source, "latest_price_quote"):
                    quote = self.realtime_price_source.latest_price_quote(normalized_symbol, now=now)
                    quote_as_of = getattr(quote, "as_of", None)
                    if require_current_session:
                        quote_as_of = validate_realtime_price_as_of(
                            normalized_symbol,
                            str(getattr(quote, "source", "") or type(self.realtime_price_source).__name__),
                            quote_as_of,
                            now,
                            allow_sparse_premarket=allow_sparse_premarket,
                        )
                    self._last_realtime_as_of = quote_as_of
                    return quote.price, quote.source, getattr(quote, "today_open", 0.0), getattr(quote, "today_open_source", "")
                price = self.realtime_price_source.latest_price(normalized_symbol)
                if require_current_session:
                    validate_realtime_price_as_of(
                        normalized_symbol,
                        type(self.realtime_price_source).__name__,
                        None,
                        now,
                        allow_sparse_premarket=allow_sparse_premarket,
                    )
                return price, type(self.realtime_price_source).__name__, 0.0, ""
            except Exception as exc:
                if not _requires_realtime_price(now):
                    raise
                return self._fallback_realtime_price(
                    alpaca_symbol,
                    now,
                    exc,
                    require_current_session=require_current_session,
                    allow_sparse_premarket=allow_sparse_premarket,
                )
        if not _requires_realtime_price(now):
            return 0.0, "", 0.0, ""
        return self._fallback_realtime_price(
            alpaca_symbol,
            now,
            require_current_session=require_current_session,
            allow_sparse_premarket=allow_sparse_premarket,
        )

    def _fallback_realtime_price(
        self,
        symbol: str,
        now: datetime,
        source_error: Exception | None = None,
        *,
        require_current_session: bool = False,
        allow_sparse_premarket: bool = False,
    ) -> tuple[float, str, float, str]:
        """Moomoo 无可用快照时切到 Alpaca；盘前优先 quote，盘中优先 trade。"""
        errors: list[str] = []
        fallback_order = ("quote", "trade") if is_premarket_time(now) else ("trade", "quote")
        if source_error is not None:
            errors.append(f"Moomoo: {short_error(source_error)}")
        for source in fallback_order:
            try:
                self._last_realtime_as_of = None
                if source == "quote":
                    price, price_source = self._latest_quote_price(symbol)
                else:
                    price = self._latest_trade_price(symbol)
                    price_source = f"alpaca_latest_trade:{self.trade_feed.lower()}"
                if require_current_session:
                    self._last_realtime_as_of = validate_realtime_price_as_of(
                        normalize_symbol(symbol),
                        price_source,
                        self._last_realtime_as_of,
                        now,
                        allow_sparse_premarket=allow_sparse_premarket,
                    )
                return price, price_source, 0.0, ""
            except Exception as exc:
                errors.append(f"Alpaca {source}: {short_error(exc)}")
        raise RuntimeError(f"{symbol} 无法从备用实时数据源取得价格；" + " | ".join(errors))

    def _daily_bars(self, symbol: str, now: datetime):
        """兼容旧调用，只返回已经通过 RAW/SPLIT 一致性检查的数据。"""
        split_bars, raw_bars = self._daily_bars_pair(symbol, now)
        validate_latest_completed_bar_basis(normalize_symbol(symbol), split_bars, raw_bars, now)
        return split_bars

    def _daily_bars_pair(self, symbol: str, now: datetime) -> tuple[list[_SnapshotBar], list[_SnapshotBar]]:
        """同一 feed 读取 SPLIT/RAW 日线；任一口径失败都不允许继续自动交易。"""
        from alpaca.data.enums import Adjustment

        feeds = [self.bars_feed.lower()]
        if self.bars_feed.lower() != "iex":
            feeds.append("iex")
        errors: list[str] = []
        for feed in feeds:
            try:
                # 公司行动供应商可能在开盘前后更新 adjustment；安全校验不能复用
                # 跨轮缓存，否则刚生效的拆股仍可能用上一轮 factor=1 的旧结果放行。
                split_bars = self._fetch_daily_bars(symbol, now, feed, adjustment=Adjustment.SPLIT)
                raw_bars = self._fetch_daily_bars(symbol, now, feed, adjustment=Adjustment.RAW)
                if feed != self.bars_feed.lower():
                    print(
                        f"{symbol}: {self.bars_feed.upper()} RAW/SPLIT 日线读取失败，已统一改用 IEX。",
                        flush=True,
                    )
                self._last_daily_feed = feed
                return split_bars, raw_bars
            except Exception as exc:
                errors.append(f"{feed.upper()}: {short_error(exc)}")
        raise MarketDataSafetyError(
            f"{normalize_symbol(symbol)} 无法同时取得同一 feed 的 RAW/SPLIT 日线，已禁止本轮自动动作；"
            + " | ".join(errors)
        )

    def _fetch_daily_bars(self, symbol: str, now: datetime, feed: str, *, adjustment=None):
        """按指定 feed/adjustment 读取日线。"""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        adjustment = adjustment or Adjustment.SPLIT
        end = _daily_request_end(now, feed)
        start = end - timedelta(days=20)
        request = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=adjustment,
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
            self._last_realtime_as_of = getattr(trade, "timestamp", None)
        except Exception as exc:
            raise RuntimeError(f"{symbol} 无法读取 {feed_label} 实时成交价：{exc}") from exc
        if price <= 0:
            raise RuntimeError(f"{symbol} {feed_label} 实时成交价无效")
        return price

    def _latest_quote_price(self, symbol: str) -> tuple[float, str]:
        """读取 Alpaca latest quote；盘前 Moomoo 没有 pre_price 时优先用 bid/ask。"""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest

        feed = self.trade_feed.lower()
        feed_label = self.trade_feed.upper()
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=[symbol], feed=DataFeed(feed))
            quote = self.client.get_stock_latest_quote(request).get(symbol)
            bid = _positive_float(getattr(quote, "bid_price", 0.0))
            ask = _positive_float(getattr(quote, "ask_price", 0.0))
            self._last_realtime_as_of = getattr(quote, "timestamp", None)
        except Exception as exc:
            raise RuntimeError(f"{symbol} 无法读取 {feed_label} 实时报价：{exc}") from exc
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2.0, 6), f"alpaca_latest_quote:midpoint:{feed}"
        if bid > 0:
            return bid, f"alpaca_latest_quote:bid:{feed}"
        if ask > 0:
            return ask, f"alpaca_latest_quote:ask:{feed}"
        raise RuntimeError(f"{symbol} {feed_label} 实时报价无效")

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


def validate_latest_completed_bar_basis(
    symbol: str,
    split_bars: list[_SnapshotBar],
    raw_bars: list[_SnapshotBar],
    now: datetime,
) -> None:
    """拆股切换日禁止把旧实时价与新复权日线混入任何自动决策。"""
    completed_split = {bar.date: bar for bar in split_bars if bar.date < now.date() and bar.close > 0}
    completed_raw = {bar.date: bar for bar in raw_bars if bar.date < now.date() and bar.close > 0}
    if not completed_split:
        raise MarketDataSafetyError(f"{symbol} 没有可用于 RAW/SPLIT 口径校验的完成日线")
    latest_date = max(completed_split)
    split_bar = completed_split[latest_date]
    raw_bar = completed_raw.get(latest_date)
    if raw_bar is None:
        raise MarketDataSafetyError(
            f"{symbol} 最新完成日线 {latest_date} 缺少 RAW 口径，已禁止本轮自动动作"
        )
    factor = split_bar.close / raw_bar.close
    if math.isclose(
        factor,
        1.0,
        rel_tol=ADJUSTMENT_FACTOR_REL_TOLERANCE,
        abs_tol=ADJUSTMENT_FACTOR_REL_TOLERANCE,
    ):
        return
    raise CorporateActionBasisError(
        symbol,
        latest_date,
        raw_bar.close,
        split_bar.close,
        factor,
    )


def validate_realtime_price_as_of(
    symbol: str,
    source: str,
    as_of: datetime | None,
    now: datetime,
    *,
    allow_sparse_premarket: bool = False,
) -> datetime:
    """实时自动动作必须使用当前交易日且没有明显滞后的行情。"""
    if as_of is None:
        raise MarketDataSafetyError(f"{symbol} {source or 'unknown'} 实时价缺少行情时间，已禁止本轮自动动作")
    if not isinstance(as_of, datetime):
        raise MarketDataSafetyError(f"{symbol} {source or 'unknown'} 行情时间格式无效，已禁止本轮自动动作")
    normalized = _market_timestamp(as_of, now)
    if normalized.date() != now.date():
        raise MarketDataSafetyError(
            f"{symbol} {source or 'unknown'} 行情日期 {normalized.date()} != 当前交易日 {now.date()}，"
            "已禁止本轮自动动作"
        )
    age = now - normalized
    if age < -REALTIME_FUTURE_TOLERANCE:
        raise MarketDataSafetyError(
            f"{symbol} {source or 'unknown'} 行情时间晚于本机市场时间，已禁止本轮自动动作"
        )
    if allow_sparse_premarket and is_premarket_time(now):
        session_open = datetime.combine(now.date(), PREMARKET_SESSION_OPEN, tzinfo=now.tzinfo)
        if normalized < session_open:
            raise MarketDataSafetyError(
                f"{symbol} {source or 'unknown'} 最新价格早于当日 04:00 ET 盘前会话，"
                "不能用于盘前观察"
            )
        # 盘前推荐不会提交订单。成交稀疏的股票可以沿用当日 04:00 ET 后
        # 最后一笔盘前成交，但必须把真实行情时间交给表格和 Agent 展示。
        return normalized
    max_age = REGULAR_REALTIME_MAX_AGE if is_regular_market_time(now) else EXTENDED_REALTIME_MAX_AGE
    if age > max_age:
        raise MarketDataSafetyError(
            f"{symbol} {source or 'unknown'} 行情已滞后 {age.total_seconds():.0f} 秒，"
            f"超过 {max_age.total_seconds():.0f} 秒安全上限，已禁止本轮自动动作"
        )
    return normalized


def _market_timestamp(value: datetime, now: datetime) -> datetime:
    """把 Moomoo 的无时区市场时间和 Alpaca 的 UTC 时间统一到 now 的时区。"""
    if now.tzinfo is None:
        return value.replace(tzinfo=None)
    if value.tzinfo is None:
        return value.replace(tzinfo=now.tzinfo)
    return value.astimezone(now.tzinfo)


def _daily_request_end(now: datetime, feed: str = "sip") -> datetime:
    """计算日线请求 end；SIP 需要避开最近数据权限窗口。"""
    return daily_request_end(now, feed)


def _stale_sip_end(now: datetime, boundary: datetime) -> datetime:
    """把 SIP 请求时间压到 20 分钟前，避开 recent data 权限限制。"""
    return stale_sip_daily_end(now, boundary)


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


def _latest_completed_gain_pct(bars: list[_SnapshotBar], now: datetime) -> float:
    """日线参考快照仍按最近两个完成交易日计算真正的信号日涨幅。"""
    completed = [bar.close for bar in bars if bar.date < now.date() and bar.close > 0]
    if len(completed) < 2 or completed[-2] <= 0:
        return 0.0
    return completed[-1] / completed[-2] - 1.0


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


def _positive_float(value) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


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
