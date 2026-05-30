from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import MarketSnapshot
from .watchlist import to_yfinance_symbol


class YFinanceMarketData:
    """使用 yfinance 做股票行情源；下单和行情解耦，之后可以替换为其他数据源。"""

    def __init__(self, market_timezone: str = "America/New_York"):
        """初始化市场时区，用于区分今日和已完成交易日。"""
        self.market_tz = ZoneInfo(market_timezone)

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        """获取当前价和前 4 个完成交易日收盘价，供策略计算 MA5。"""
        import yfinance as yf

        yf_symbol = to_yfinance_symbol(symbol)
        ticker = yf.Ticker(yf_symbol)
        history = ticker.history(period="14d", interval="1d", auto_adjust=False)
        if history.empty or "Close" not in history:
            raise RuntimeError(f"{symbol} 没有可用日线数据")

        today = datetime.now(self.market_tz).date()
        completed_closes: list[float] = []
        fallback_current = 0.0
        for index, row in history.iterrows():
            close = float(row.get("Close", 0.0) or 0.0)
            if close <= 0:
                continue
            row_date = index.date()
            fallback_current = close
            if row_date < today:
                completed_closes.append(close)

        current_price = _fast_last_price(ticker) or fallback_current
        if current_price <= 0:
            raise RuntimeError(f"{symbol} 当前价格无效")
        if len(completed_closes) < 4:
            raise RuntimeError(f"{symbol} 少于 4 个已完成日线收盘价")

        return MarketSnapshot(symbol=symbol, current_price=current_price, previous_closes=completed_closes[-4:], as_of=datetime.now(self.market_tz))


def _fast_last_price(ticker) -> float:
    """优先读取 yfinance fast_info 的最新价，失败时返回 0 交给调用方兜底。"""
    try:
        fast_info = getattr(ticker, "fast_info", {}) or {}
        for field in ("last_price", "lastPrice", "regular_market_price"):
            value = fast_info.get(field) if hasattr(fast_info, "get") else None
            if value:
                return float(value)
    except Exception:
        return 0.0
    return 0.0
