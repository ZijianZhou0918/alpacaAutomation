from __future__ import annotations

import math

from .models import MarketSnapshot, Signal


STRATEGY_NAME = "ma5_dip"
STRATEGY_DESCRIPTION = (
    "Previous MA5 dip-buy strategy: signal day gain >=15%; buy point is dynamic MA5 "
    "plus a gain tier and optional open-gap bonus; require a deep same-day pullback "
    "before placing a BUY LIMIT at the computed MA5 buy point."
)

MIN_SIGNAL_DAY_GAIN_PCT = 0.15
MID_SIGNAL_DAY_GAIN_PCT = 0.40
HIGH_SIGNAL_DAY_GAIN_PCT = 1.00
MID_OPEN_GAIN_PCT = 0.05
HIGH_OPEN_GAIN_PCT = 0.15
BUY_TRIGGER_DISTANCE_PCT = 0.03
MIN_TODAY_OPEN_GAIN_PCT = -0.40
MIN_TODAY_OPEN_VS_OPEN_MA5_PCT = -0.10
MAX_BUY_TODAY_CURRENT_GAIN_PCT = -0.12


def configure(
    *,
    min_signal_day_gain_pct: float | None = None,
    mid_signal_day_gain_pct: float | None = None,
    high_signal_day_gain_pct: float | None = None,
    mid_open_gain_pct: float | None = None,
    high_open_gain_pct: float | None = None,
    buy_trigger_distance_pct: float | None = None,
    min_today_open_gain_pct: float | None = None,
    min_today_open_vs_open_ma5_pct: float | None = None,
    max_buy_today_current_gain_pct: float | None = None,
) -> None:
    global MIN_SIGNAL_DAY_GAIN_PCT
    global MID_SIGNAL_DAY_GAIN_PCT
    global HIGH_SIGNAL_DAY_GAIN_PCT
    global MID_OPEN_GAIN_PCT
    global HIGH_OPEN_GAIN_PCT
    global BUY_TRIGGER_DISTANCE_PCT
    global MIN_TODAY_OPEN_GAIN_PCT
    global MIN_TODAY_OPEN_VS_OPEN_MA5_PCT
    global MAX_BUY_TODAY_CURRENT_GAIN_PCT

    if min_signal_day_gain_pct is not None:
        MIN_SIGNAL_DAY_GAIN_PCT = _finite_config("min_signal_day_gain_pct", min_signal_day_gain_pct)
    if mid_signal_day_gain_pct is not None:
        MID_SIGNAL_DAY_GAIN_PCT = _finite_config("mid_signal_day_gain_pct", mid_signal_day_gain_pct)
    if high_signal_day_gain_pct is not None:
        HIGH_SIGNAL_DAY_GAIN_PCT = _finite_config("high_signal_day_gain_pct", high_signal_day_gain_pct)
    if mid_open_gain_pct is not None:
        MID_OPEN_GAIN_PCT = _finite_config("mid_open_gain_pct", mid_open_gain_pct)
    if high_open_gain_pct is not None:
        HIGH_OPEN_GAIN_PCT = _finite_config("high_open_gain_pct", high_open_gain_pct)
    if buy_trigger_distance_pct is not None:
        BUY_TRIGGER_DISTANCE_PCT = _finite_config("buy_trigger_distance_pct", buy_trigger_distance_pct)
    if min_today_open_gain_pct is not None:
        MIN_TODAY_OPEN_GAIN_PCT = _finite_config("min_today_open_gain_pct", min_today_open_gain_pct)
    if min_today_open_vs_open_ma5_pct is not None:
        MIN_TODAY_OPEN_VS_OPEN_MA5_PCT = _finite_config("min_today_open_vs_open_ma5_pct", min_today_open_vs_open_ma5_pct)
    if max_buy_today_current_gain_pct is not None:
        MAX_BUY_TODAY_CURRENT_GAIN_PCT = _finite_config(
            "max_buy_today_current_gain_pct",
            max_buy_today_current_gain_pct,
        )


def _finite_config(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def evaluate_buy(snapshot: MarketSnapshot) -> Signal:
    if snapshot.current_price <= 0:
        return Signal(snapshot.symbol, "HOLD", "当前价格无效", snapshot.current_price)
    if len(snapshot.previous_closes) < 4:
        return Signal(snapshot.symbol, "HOLD", "少于 4 个已完成日线收盘价，无法计算今日 MA5", snapshot.current_price)

    today_ma5 = snapshot.today_ma5
    signal_day_gain_pct = snapshot.signal_day_gain_pct
    today_open_ma5 = snapshot.today_open_ma5
    today_open_vs_open_ma5_pct = snapshot.today_open_vs_open_ma5_pct
    today_open_vs_today_ma5_pct = snapshot.today_open_vs_today_ma5_pct
    base_buy_point_pct = signal_day_buy_point_pct(signal_day_gain_pct)
    today_open_gain_pct = snapshot.today_open_gain_pct
    today_current_gain_pct = snapshot.today_current_gain_pct
    open_bonus_pct = open_gain_bonus_pct(today_open_gain_pct) if snapshot.today_open > 0 else 0.0
    final_buy_point_pct = (base_buy_point_pct or 0.0) + open_bonus_pct
    final_buy_point = today_ma5 * (1.0 + final_buy_point_pct)
    buy_trigger_price = final_buy_point * (1.0 + BUY_TRIGGER_DISTANCE_PCT)
    current_vs_buy_point_pct = snapshot.current_price / final_buy_point - 1.0 if final_buy_point > 0 else 0.0
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
        "max_buy_today_current_gain_pct": MAX_BUY_TODAY_CURRENT_GAIN_PCT,
        "min_today_open_gain_pct": MIN_TODAY_OPEN_GAIN_PCT,
        "base_buy_point_pct": base_buy_point_pct if base_buy_point_pct is not None else 0.0,
        "open_bonus_pct": open_bonus_pct,
        "final_buy_point_pct": final_buy_point_pct,
        "final_buy_point": final_buy_point,
        "buy_trigger_distance_pct": BUY_TRIGGER_DISTANCE_PCT,
        "buy_trigger_price": buy_trigger_price,
        "current_vs_buy_point_pct": current_vs_buy_point_pct,
        "has_valid_buy_point": base_buy_point_pct is not None,
    }
    market_note = _market_open_note(snapshot)

    if snapshot.today_open > 0 and today_open_gain_pct <= MIN_TODAY_OPEN_GAIN_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"今日开盘跌幅达到 {_format_pct(abs(MIN_TODAY_OPEN_GAIN_PCT))}，不下单；今日开盘涨幅 {_format_pct(today_open_gain_pct)}",
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
    if snapshot.today_open > 0 and snapshot.today_open < today_ma5:
        return Signal(
            snapshot.symbol,
            "HOLD",
            f"今日开盘价低于当前动态MA5，不下单；今日开盘 {snapshot.today_open:.4f}，今日动态MA5 {today_ma5:.4f}，偏离 {_format_pct(today_open_vs_today_ma5_pct)}",
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    if base_buy_point_pct is None:
        return Signal(
            snapshot.symbol,
            "HOLD",
            _append_note(
                (
                    f"信号日涨幅 {_format_pct(signal_day_gain_pct)} < 买入要求 {_format_pct(MIN_SIGNAL_DAY_GAIN_PCT)}，无有效分段买点；"
                    f"动作：观察不买；参考动态MA5价 {final_buy_point:.4f}（非买入限价）；"
                    f"当前价 {snapshot.current_price:.4f}，当前涨跌 {_format_pct(today_current_gain_pct)}"
                ),
                market_note,
            ),
            snapshot.current_price,
            diagnostics=diagnostics,
        )

    if snapshot.current_price <= today_ma5 + 1e-9 and today_current_gain_pct > MAX_BUY_TODAY_CURRENT_GAIN_PCT:
        return Signal(
            snapshot.symbol,
            "HOLD",
            _append_note(
                (
                    f"触达动态MA5但跌幅未到 {_format_pct(abs(MAX_BUY_TODAY_CURRENT_GAIN_PCT))}；"
                    f"动作：今日排除不再买；"
                    f"有效分段买点 {final_buy_point:.4f}，触发上沿 {buy_trigger_price:.4f}；"
                    f"当前价 {snapshot.current_price:.4f}，当前涨跌 {_format_pct(today_current_gain_pct)}"
                ),
                market_note,
            ),
            snapshot.current_price,
            diagnostics={**diagnostics, "daily_buy_exclusion": "ma5_touch_without_required_drop"},
        )

    reason = (
        f"信号日涨幅 {_format_pct(signal_day_gain_pct)} >= {_format_pct(MIN_SIGNAL_DAY_GAIN_PCT)}；"
        f"分段买点 {final_buy_point:.4f}，触发上沿 {buy_trigger_price:.4f}；"
        f"当前价 {snapshot.current_price:.4f}，距买点 {_format_pct(current_vs_buy_point_pct)}，当前涨跌 {_format_pct(today_current_gain_pct)}；"
        f"需跌幅 >= {_format_pct(abs(MAX_BUY_TODAY_CURRENT_GAIN_PCT))}"
    )
    if snapshot.current_price <= buy_trigger_price:
        if today_current_gain_pct > MAX_BUY_TODAY_CURRENT_GAIN_PCT:
            return Signal(
                snapshot.symbol,
                "HOLD",
                _append_note(
                    (
                        f"进入买点区间但跌幅未到 {_format_pct(abs(MAX_BUY_TODAY_CURRENT_GAIN_PCT))}；"
                        f"动作：观察不买。{reason}"
                    ),
                    market_note,
                ),
                snapshot.current_price,
                diagnostics=diagnostics,
            )
        prefix = (
            "当前价小于等于分段买点"
            if snapshot.current_price <= final_buy_point
            else f"当前价在分段买点上方 {_format_pct(BUY_TRIGGER_DISTANCE_PCT)} 内"
        )
        return Signal(
            snapshot.symbol,
            "BUY",
            _append_note(f"{prefix}且跌幅达标；动作：按买点 {final_buy_point:.4f} 挂 BUY LIMIT。{reason}", market_note),
            snapshot.current_price,
            diagnostics=diagnostics,
        )
    return Signal(
        snapshot.symbol,
        "HOLD",
        _append_note(f"当前价高于触发上沿 {buy_trigger_price:.4f}；动作：观察不买。{reason}", market_note),
        snapshot.current_price,
        diagnostics=diagnostics,
    )


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


def _market_open_note(snapshot: MarketSnapshot) -> str:
    if snapshot.today_open > 0:
        return ""
    return "未取得今日开盘价；开盘前服务层只观察不下单"


def _append_note(reason: str, note: str) -> str:
    return f"{reason}；{note}" if note else reason
