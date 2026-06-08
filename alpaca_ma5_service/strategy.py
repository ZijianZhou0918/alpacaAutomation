from __future__ import annotations

from datetime import datetime

from .config import Settings
from .models import MarketSnapshot, Position, Signal


MIN_SIGNAL_DAY_GAIN_PCT = 0.20
MID_SIGNAL_DAY_GAIN_PCT = 0.40
HIGH_SIGNAL_DAY_GAIN_PCT = 1.00
MID_OPEN_GAIN_PCT = 0.05
HIGH_OPEN_GAIN_PCT = 0.15
BUY_TRIGGER_DISTANCE_PCT = 0.02
MIN_TODAY_OPEN_VS_OPEN_MA5_PCT = -0.10
STOP_LOSS_COMPARE_EPS = 1e-9


def evaluate_buy(snapshot: MarketSnapshot) -> Signal:
    """买入规则：按信号日涨幅和当天开盘涨幅计算分段买点。"""
    if snapshot.current_price <= 0:
        return Signal(snapshot.symbol, "HOLD", "当前价格无效", snapshot.current_price)
    if len(snapshot.previous_closes) < 4:
        return Signal(snapshot.symbol, "HOLD", "少于 4 个已完成日线收盘价，无法计算今日 MA5", snapshot.current_price)

    today_ma5 = snapshot.today_ma5
    signal_day_gain_pct = snapshot.signal_day_gain_pct
    today_open_ma5 = snapshot.today_open_ma5
    today_open_vs_open_ma5_pct = snapshot.today_open_vs_open_ma5_pct
    base_buy_point_pct = signal_day_buy_point_pct(signal_day_gain_pct)
    today_open_gain_pct = snapshot.today_open_gain_pct
    open_bonus_pct = open_gain_bonus_pct(today_open_gain_pct) if snapshot.today_open > 0 else 0.0
    final_buy_point_pct = (base_buy_point_pct or 0.0) + open_bonus_pct
    final_buy_point = today_ma5 * (1.0 + final_buy_point_pct)
    # 当前价靠近买点 2% 内就触发，但真正下单价格仍固定为买点。
    buy_trigger_price = final_buy_point * (1.0 + BUY_TRIGGER_DISTANCE_PCT)
    current_vs_buy_point_pct = snapshot.current_price / final_buy_point - 1.0 if final_buy_point > 0 else 0.0
    diagnostics = {
        "current_price": snapshot.current_price,
        "today_ma5": today_ma5,
        "today_open": snapshot.today_open,
        "today_open_ma5": today_open_ma5,
        "today_open_vs_open_ma5_pct": today_open_vs_open_ma5_pct,
        "min_today_open_vs_open_ma5_pct": MIN_TODAY_OPEN_VS_OPEN_MA5_PCT,
        "prev4_open_sum": sum(snapshot.previous_opens[-4:]),
        "prev4_close_sum": sum(snapshot.previous_closes[-4:]),
        "signal_day_gain_pct": signal_day_gain_pct,
        "today_open_gain_pct": today_open_gain_pct,
        "base_buy_point_pct": base_buy_point_pct if base_buy_point_pct is not None else 0.0,
        "open_bonus_pct": open_bonus_pct,
        "final_buy_point_pct": final_buy_point_pct,
        "final_buy_point": final_buy_point,
        "buy_trigger_distance_pct": BUY_TRIGGER_DISTANCE_PCT,
        "buy_trigger_price": buy_trigger_price,
        "current_vs_buy_point_pct": current_vs_buy_point_pct,
    }

    # 今日开盘显著低于开盘 MA5 时，整天不买这只，避免低开破位后反复触发买点。
    if today_open_ma5 > 0 and today_open_vs_open_ma5_pct <= MIN_TODAY_OPEN_VS_OPEN_MA5_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"今日开盘价低于开盘MA5 {_format_pct(abs(MIN_TODAY_OPEN_VS_OPEN_MA5_PCT))}，当天不买入；"
            f"今日开盘 {snapshot.today_open:.4f}，开盘MA5 {today_open_ma5:.4f}，偏离 {_format_pct(today_open_vs_open_ma5_pct)}",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    if base_buy_point_pct is None:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"信号日涨幅 {_format_pct(signal_day_gain_pct)} 低于 20%，没有有效分段买点",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    reason = (
        f"信号日涨幅 {_format_pct(signal_day_gain_pct)}，基础买点 +{_format_pct(base_buy_point_pct)}；"
        f"当天开盘涨幅 {_format_pct(today_open_gain_pct) if snapshot.today_open > 0 else '未知'}，加成 +{_format_pct(open_bonus_pct)}；"
        f"最终买点 {final_buy_point:.4f}；触发上沿 {buy_trigger_price:.4f}"
    )
    if snapshot.current_price <= buy_trigger_price:
        prefix = "当前价小于等于分段买点" if snapshot.current_price <= final_buy_point else "当前价在分段买点上方 2% 内"
        return Signal(snapshot.symbol, "BUY", f"{prefix}，用买点价挂 BUY LIMIT。{reason}", snapshot.current_price, diagnostics=diagnostics)
    return Signal(snapshot.symbol, "HOLD", f"当前价超过分段买点上方 2%。{reason}", snapshot.current_price, diagnostics=diagnostics)


def signal_day_buy_point_pct(signal_day_gain_pct: float) -> float | None:
    """按信号日涨幅返回基础买点加成；低于 20% 不给买点。"""
    if signal_day_gain_pct > HIGH_SIGNAL_DAY_GAIN_PCT:
        return 0.03
    if signal_day_gain_pct >= MID_SIGNAL_DAY_GAIN_PCT:
        return 0.02
    if signal_day_gain_pct >= MIN_SIGNAL_DAY_GAIN_PCT:
        return 0.005
    return None


def open_gain_bonus_pct(today_open_gain_pct: float) -> float:
    """按当天开盘涨幅返回买点加成。"""
    if today_open_gain_pct > HIGH_OPEN_GAIN_PCT:
        return 0.02
    if today_open_gain_pct >= MID_OPEN_GAIN_PCT:
        return 0.01
    return 0.0


def evaluate_sell(position: Position, snapshot: MarketSnapshot, now_et: datetime, settings: Settings) -> Signal:
    """卖出规则：临近收盘清仓优先，其次检查配置里的亏损止损线。"""
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

    # 收盘清仓优先，避免盘末价格跳动让止损判断抢先返回。
    if settings.close_liquidation_start <= now_et.time() <= settings.close_liquidation_end:
        return Signal(position.symbol, "SELL_ALL", "临近常规盘收盘，卖出全部", current_price, position.quantity, diagnostics)
    if gain_pct <= settings.stop_loss_pct + STOP_LOSS_COMPARE_EPS:
        return Signal(position.symbol, "SELL_ALL", f"持仓亏损达到 {_format_pct(abs(settings.stop_loss_pct))}，卖出全部", current_price, position.quantity, diagnostics)
    return Signal(position.symbol, "HOLD", f"未触发收盘卖出或 -{_format_pct(abs(settings.stop_loss_pct))} 止损", current_price, diagnostics=diagnostics)


def _format_pct(value: float) -> str:
    return f"{value:.2%}"
