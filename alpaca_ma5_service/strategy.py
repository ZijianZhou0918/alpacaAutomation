from __future__ import annotations

from datetime import datetime

from .config import Settings
from .models import MarketSnapshot, Position, Signal


def evaluate_buy(snapshot: MarketSnapshot) -> Signal:
    """根据当前价和今日动态 MA5 判断是否触发买入。"""
    if snapshot.current_price <= 0:
        return Signal(snapshot.symbol, "HOLD", "当前价格无效", snapshot.current_price)
    if len(snapshot.previous_closes) < 4:
        return Signal(snapshot.symbol, "HOLD", "少于 4 个已完成日线收盘价，无法计算今日 MA5", snapshot.current_price)

    today_ma5 = snapshot.today_ma5
    diagnostics = {
        "current_price": snapshot.current_price,
        "today_ma5": today_ma5,
        "prev4_close_sum": sum(snapshot.previous_closes[-4:]),
    }
    if snapshot.current_price < today_ma5:
        return Signal(snapshot.symbol, "BUY", "当前价低于包含今日当前价的 MA5", snapshot.current_price, diagnostics=diagnostics)
    return Signal(snapshot.symbol, "HOLD", "当前价未低于包含今日当前价的 MA5", snapshot.current_price, diagnostics=diagnostics)


def evaluate_sell(position: Position, snapshot: MarketSnapshot, now_et: datetime, settings: Settings) -> Signal:
    """根据收盘清仓和止损规则判断当前持仓是否卖出。"""
    current_price = snapshot.current_price
    if current_price <= 0:
        return Signal(position.symbol, "HOLD", "当前价格无效", current_price)

    gain_pct = current_price / position.avg_price - 1.0 if position.avg_price > 0 else 0.0
    diagnostics = {
        "current_price": current_price,
        "avg_price": position.avg_price,
        "gain_pct": gain_pct,
        "stop_loss_pct": settings.stop_loss_pct,
    }

    # 收盘清仓优先，避免因为盘末价格跳动错过收盘退出。
    if settings.close_liquidation_start <= now_et.time() <= settings.close_liquidation_end:
        return Signal(position.symbol, "SELL_ALL", "临近常规盘收盘，卖出全部", current_price, position.quantity, diagnostics)
    if gain_pct <= settings.stop_loss_pct:
        return Signal(position.symbol, "SELL_ALL", "持仓亏损达到 15%，卖出全部", current_price, position.quantity, diagnostics)
    return Signal(position.symbol, "HOLD", "未触发收盘卖出或 -15% 止损", current_price, diagnostics=diagnostics)
