"""缺口确认回撤买入策略。

本模块只判断行情并返回 BUY/HOLD ``Signal``。BUY 信号把当前价写入
``diagnostics['final_buy_point']``，真正的 BUY LIMIT 仍由服务层和 Broker 提交。
"""

from __future__ import annotations

from .final_strategy import BUY_SIGNAL_PARAMS, STRATEGY_DESCRIPTION, STRATEGY_NAME
from .models import MarketSnapshot, Signal


MIN_SIGNAL_DAY_GAIN_PCT = BUY_SIGNAL_PARAMS["MIN_SIGNAL_DAY_GAIN_PCT"]
MID_SIGNAL_DAY_GAIN_PCT = BUY_SIGNAL_PARAMS["MID_SIGNAL_DAY_GAIN_PCT"]
HIGH_SIGNAL_DAY_GAIN_PCT = BUY_SIGNAL_PARAMS["HIGH_SIGNAL_DAY_GAIN_PCT"]
MID_OPEN_GAIN_PCT = BUY_SIGNAL_PARAMS["MID_OPEN_GAIN_PCT"]
HIGH_OPEN_GAIN_PCT = BUY_SIGNAL_PARAMS["HIGH_OPEN_GAIN_PCT"]
BUY_TRIGGER_DISTANCE_PCT = BUY_SIGNAL_PARAMS["BUY_TRIGGER_DISTANCE_PCT"]
MIN_TODAY_OPEN_GAIN_PCT = BUY_SIGNAL_PARAMS["MIN_TODAY_OPEN_GAIN_PCT"]
MAX_TODAY_OPEN_GAIN_PCT = BUY_SIGNAL_PARAMS["MAX_TODAY_OPEN_GAIN_PCT"]
MIN_TODAY_OPEN_VS_OPEN_MA5_PCT = BUY_SIGNAL_PARAMS["MIN_TODAY_OPEN_VS_OPEN_MA5_PCT"]
MIN_TODAY_CURRENT_GAIN_PCT = BUY_SIGNAL_PARAMS["MIN_TODAY_CURRENT_GAIN_PCT"]
MAX_BUY_TODAY_CURRENT_GAIN_PCT = BUY_SIGNAL_PARAMS["MAX_BUY_TODAY_CURRENT_GAIN_PCT"]
MIN_CURRENT_VS_TODAY_MA5_PCT = BUY_SIGNAL_PARAMS["MIN_CURRENT_VS_TODAY_MA5_PCT"]


def evaluate_buy(snapshot: MarketSnapshot) -> Signal:
    """检查信号日、开盘涨幅、动态 MA5 和回撤区间，产生 BUY/HOLD。

    该函数不读取账户、不检查每日名额，也不调用 Alpaca；这些统一风控由
    ``service.run_once`` 在收到 BUY 信号后继续执行。
    """
    if snapshot.current_price <= 0:
        return Signal(snapshot.symbol, "HOLD", "当前价格无效", snapshot.current_price)
    if len(snapshot.previous_closes) < 4:
        return Signal(snapshot.symbol, "HOLD", "少于 4 个已完成日线收盘价，无法计算今日 MA5", snapshot.current_price)

    today_ma5 = snapshot.today_ma5
    signal_day_gain_pct = snapshot.signal_day_gain_pct
    today_open_ma5 = snapshot.today_open_ma5
    today_open_vs_open_ma5_pct = snapshot.today_open_vs_open_ma5_pct
    today_open_vs_today_ma5_pct = snapshot.today_open_vs_today_ma5_pct
    today_open_gain_pct = snapshot.today_open_gain_pct
    today_current_gain_pct = snapshot.today_current_gain_pct
    current_vs_today_ma5_pct = snapshot.current_price / today_ma5 - 1.0 if today_ma5 > 0 else 0.0
    final_buy_point = snapshot.current_price
    diagnostics = {
        "current_price": snapshot.current_price,
        "today_ma5": today_ma5,
        "today_open": snapshot.today_open,
        "today_open_ma5": today_open_ma5,
        "today_open_vs_open_ma5_pct": today_open_vs_open_ma5_pct,
        "today_open_vs_today_ma5_pct": today_open_vs_today_ma5_pct,
        "min_today_open_vs_open_ma5_pct": MIN_TODAY_OPEN_VS_OPEN_MA5_PCT,
        "prev4_open_sum": sum(snapshot.previous_opens[-4:]),
        "prev4_close_sum": sum(snapshot.previous_closes[-4:]),
        "signal_day_gain_pct": signal_day_gain_pct,
        "today_open_gain_pct": today_open_gain_pct,
        "today_current_gain_pct": today_current_gain_pct,
        "min_today_current_gain_pct": MIN_TODAY_CURRENT_GAIN_PCT,
        "max_buy_today_current_gain_pct": MAX_BUY_TODAY_CURRENT_GAIN_PCT,
        "min_today_open_gain_pct": MIN_TODAY_OPEN_GAIN_PCT,
        "max_today_open_gain_pct": MAX_TODAY_OPEN_GAIN_PCT,
        "current_vs_today_ma5_pct": current_vs_today_ma5_pct,
        "min_current_vs_today_ma5_pct": MIN_CURRENT_VS_TODAY_MA5_PCT,
        "final_buy_point": final_buy_point,
    }

    if signal_day_gain_pct < MIN_SIGNAL_DAY_GAIN_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"信号日涨幅 {_format_pct(signal_day_gain_pct)} 低于 {_format_pct(MIN_SIGNAL_DAY_GAIN_PCT)}，没有有效买入信号",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    if snapshot.today_open <= 0:
        return Signal(snapshot.symbol, "HOLD", "今日开盘价未知，不下单", snapshot.current_price, diagnostics=diagnostics)

    if today_open_gain_pct < MIN_TODAY_OPEN_GAIN_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"今日低开，开盘涨幅 {_format_pct(today_open_gain_pct)} 低于 {_format_pct(MIN_TODAY_OPEN_GAIN_PCT)}，不下单",
            snapshot.current_price,
            diagnostics=diagnostics,
        )
    if today_open_gain_pct > MAX_TODAY_OPEN_GAIN_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"今日开盘涨幅 {_format_pct(today_open_gain_pct)} 高于 {_format_pct(MAX_TODAY_OPEN_GAIN_PCT)}，不追高",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    if snapshot.today_open > 0 and snapshot.today_open < today_ma5:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"今日开盘价低于当前动态MA5，不下单；今日开盘 {snapshot.today_open:.4f}，今日动态MA5 {today_ma5:.4f}，偏离 {_format_pct(today_open_vs_today_ma5_pct)}",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    if today_open_ma5 > 0 and today_open_vs_open_ma5_pct <= MIN_TODAY_OPEN_VS_OPEN_MA5_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"今日开盘价低于开盘MA5 {_format_pct(abs(MIN_TODAY_OPEN_VS_OPEN_MA5_PCT))}，当天不买入；"
            f"今日开盘 {snapshot.today_open:.4f}，开盘MA5 {today_open_ma5:.4f}，偏离 {_format_pct(today_open_vs_open_ma5_pct)}",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    if today_current_gain_pct < MIN_TODAY_CURRENT_GAIN_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"当前回撤 {_format_pct(today_current_gain_pct)} 已深于 {_format_pct(MIN_TODAY_CURRENT_GAIN_PCT)}，不接下跌过深的回撤",
            snapshot.current_price,
            diagnostics=diagnostics,
        )
    if today_current_gain_pct > MAX_BUY_TODAY_CURRENT_GAIN_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            (
                f"当前回撤 {_format_pct(today_current_gain_pct)} 未进入 "
                f"{_format_pct(MIN_TODAY_CURRENT_GAIN_PCT)}..{_format_pct(MAX_BUY_TODAY_CURRENT_GAIN_PCT)} 买入区间"
            ),
            snapshot.current_price,
            diagnostics=diagnostics,
        )
    if current_vs_today_ma5_pct < MIN_CURRENT_VS_TODAY_MA5_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"当前价低于动态MA5，偏离 {_format_pct(current_vs_today_ma5_pct)}，不下单",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    reason = (
        f"信号日涨幅 {_format_pct(signal_day_gain_pct)}；今日未低开，开盘涨幅 {_format_pct(today_open_gain_pct)}；"
        f"当前回撤 {_format_pct(today_current_gain_pct)} 位于买入区间；"
        f"当前价相对动态MA5 {_format_pct(current_vs_today_ma5_pct)}；"
        f"用当前价 {final_buy_point:.4f} 挂 BUY LIMIT"
    )
    # 【买入决策，不下单】服务层会读取 final_buy_point 并提交 BUY LIMIT。
    return Signal(snapshot.symbol, "BUY", reason, snapshot.current_price, diagnostics=diagnostics)


def signal_day_buy_point_pct(signal_day_gain_pct: float) -> float | None:
    if signal_day_gain_pct > HIGH_SIGNAL_DAY_GAIN_PCT:
        return 0.04
    if signal_day_gain_pct >= MID_SIGNAL_DAY_GAIN_PCT:
        return 0.03
    if signal_day_gain_pct >= MIN_SIGNAL_DAY_GAIN_PCT:
        return 0.005
    return None


def open_gain_bonus_pct(today_open_gain_pct: float) -> float:
    if today_open_gain_pct > HIGH_OPEN_GAIN_PCT:
        return 0.02
    if today_open_gain_pct >= MID_OPEN_GAIN_PCT:
        return 0.01
    return 0.0


def _format_pct(value: float) -> str:
    return f"{value:.2%}"
