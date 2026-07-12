from __future__ import annotations

import json
import math
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings, build_settings
from .errors import short_error
from .market_data import build_market_data as build_default_market_data
from .market_time import is_premarket_monitor_finished, is_premarket_time, seconds_until_premarket_monitor_end
from .models import MarketSnapshot
from .openclaw_notify import safe_send_openclaw_messages
from .premarket_watchlist import premarket_watch_codes_path, read_premarket_watch_metadata
from .run_lock import acquire_run_lock
from .watchlist import read_watch_codes


PREMARKET_ALERT_DISTANCE_PCT = 0.03
PREMARKET_MIN_DROP_PCT = 0.15
PREMARKET_BELOW_MA5_ALERT_LIMIT = 2
PREMARKET_MONITOR_POLL_SECONDS = 120
PREMARKET_WAIT_POLL_SECONDS = 300
PREMARKET_ALERT_STATE_NAME = "premarket_ma5_alert_state.json"
ALERT_TYPE_NEAR_ABOVE = "near_above"
ALERT_TYPE_BELOW_MA5 = "below_ma5"
ALERT_TYPE_CROSS_UP_MA5 = "cross_up_ma5"
MA5_POSITION_BELOW = "below"
MA5_POSITION_ABOVE = "above"


@dataclass(frozen=True)
class PremarketRecommendation:
    symbol: str
    current_price: float
    today_ma5: float
    ma5_distance_pct: float
    current_gain_pct: float
    signal_gain_pct: float
    price_source: str
    alert_bucket_pct: int
    as_of: datetime
    alert_type: str = ALERT_TYPE_NEAR_ABOVE
    below_alert_number: int = 0
    notification_note: str = ""


@dataclass(frozen=True)
class PremarketObservation:
    symbol: str
    current_price: float
    today_ma5: float
    ma5_distance_pct: float | None
    current_gain_pct: float | None
    signal_gain_pct: float
    price_source: str
    reason: str


def run_premarket_recommendation_once(
    settings: Settings | None = None,
    *,
    market_data=None,
    now: datetime | None = None,
    watch_codes_file: Path | None = None,
    alert_distance_pct: float = PREMARKET_ALERT_DISTANCE_PCT,
    min_drop_pct: float = PREMARKET_MIN_DROP_PCT,
    notify: bool = True,
    alert_state_path: Path | None = None,
) -> dict[str, int]:
    """执行一轮盘前 MA5 推荐监控；只提醒，不下单。"""
    settings = settings or build_settings()
    now_et = now or datetime.now(ZoneInfo(settings.market_timezone))
    watch_path = watch_codes_file or premarket_watch_codes_path(settings)
    signal_day, watch_codes = read_premarket_watch_metadata(watch_path)
    if not watch_codes:
        watch_codes = read_watch_codes(watch_path)

    summary = {"watch": len(watch_codes), "alert": 0, "sent": 0, "hold": 0, "errors": 0}
    print_premarket_header(now_et, watch_path, signal_day, len(watch_codes), alert_distance_pct, min_drop_pct)
    if not watch_codes:
        print("盘前观察池为空，请先运行 watchcode_premarket.py。", flush=True)
        return summary

    state_path = alert_state_path or settings.output_dir / PREMARKET_ALERT_STATE_NAME
    alert_state = load_alert_state(state_path, now_et)
    created_market_data = market_data is None
    rows: list[PremarketRecommendation | PremarketObservation | tuple[str, str]] = []
    in_premarket = is_premarket_time(now_et)
    can_notify = notify and in_premarket

    if not in_premarket:
        print("当前不是盘前 04:00-09:30 ET，本轮不计算盘前跌幅，也不检测推荐。", flush=True)
        summary["hold"] = len(watch_codes)
        rows = [(symbol, "非盘前时段，盘前跌幅未计算，等待 04:00-09:30 ET") for symbol in watch_codes]
        print_premarket_rows(rows)
        print(
            f"本轮完成：观察 {summary['watch']} | 推荐 {summary['alert']} | 已发提醒 {summary['sent']} | "
            f"未触发 {summary['hold']} | 错误 {summary['errors']}",
            flush=True,
        )
        return summary

    market_data = market_data or build_default_market_data(settings)

    try:
        for symbol in watch_codes:
            try:
                snapshot: MarketSnapshot = market_data.get_snapshot(symbol)
                distance = ma5_distance_pct(snapshot)
                if not has_realtime_price(snapshot):
                    summary["hold"] += 1
                    rows.append(
                        PremarketObservation(
                            symbol=snapshot.symbol,
                            current_price=snapshot.current_price,
                            today_ma5=snapshot.today_ma5,
                            ma5_distance_pct=distance,
                            current_gain_pct=None,
                            signal_gain_pct=snapshot.signal_day_gain_pct,
                            price_source=snapshot.current_price_source,
                            reason=f"未取得盘前实时价；当前价来源 {snapshot.current_price_source or 'unknown'}，不计算盘前跌幅",
                        )
                    )
                    continue
                previous_position = last_ma5_position(alert_state, snapshot.symbol)
                recommendation = evaluate_premarket_ma5_recommendation(
                    snapshot,
                    alert_distance_pct=alert_distance_pct,
                    min_drop_pct=min_drop_pct,
                    previous_ma5_position=previous_position,
                )
                if recommendation is None:
                    summary["hold"] += 1
                    rows.append(
                        PremarketObservation(
                            symbol=snapshot.symbol,
                            current_price=snapshot.current_price,
                            today_ma5=snapshot.today_ma5,
                            ma5_distance_pct=distance,
                            current_gain_pct=snapshot.today_current_gain_pct,
                            signal_gain_pct=snapshot.signal_day_gain_pct,
                            price_source=snapshot.current_price_source,
                            reason=format_hold_reason(snapshot, distance, alert_distance_pct, min_drop_pct),
                        )
                    )
                    update_alert_ma5_position(alert_state, snapshot.symbol, distance)
                    continue

                summary["alert"] += 1
                recommendation = recommendation_with_state_details(alert_state, recommendation)
                if not can_notify:
                    if not notify:
                        note = f"本轮设置为不发送，仅打印；{recommendation_alert_note(recommendation)}"
                    else:
                        note = f"非盘前时段，仅打印不发送；{recommendation_alert_note(recommendation)}"
                elif not should_send_alert(alert_state, recommendation):
                    note = duplicate_alert_note(alert_state, recommendation)
                else:
                    safe_send_openclaw_messages(
                        settings,
                        [render_premarket_recommendation_message(recommendation, signal_day)],
                        context=f"premarket MA5 recommendation {recommendation.symbol}",
                    )
                    mark_alert_sent(alert_state, recommendation)
                    summary["sent"] += 1
                    note = f"已调用发送；{recommendation_alert_note(recommendation)}"
                update_alert_ma5_position(alert_state, snapshot.symbol, distance)
                rows.append(replace(recommendation, notification_note=note))
            except Exception as exc:
                summary["errors"] += 1
                rows.append((symbol, f"错误: {type(exc).__name__}: {short_error(exc)}"))
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()

    save_alert_state(state_path, alert_state)
    print_premarket_rows(rows)
    print(
        f"本轮完成：观察 {summary['watch']} | 推荐 {summary['alert']} | 已发提醒 {summary['sent']} | "
        f"未触发 {summary['hold']} | 错误 {summary['errors']}",
        flush=True,
    )
    return summary


def run_premarket_recommendations_forever(
    settings: Settings | None = None,
    *,
    max_loops: int | None = None,
    sleep=time.sleep,
    now_provider=None,
) -> None:
    """持续运行盘前推荐监控；到 09:30 ET 自动停止。"""
    settings = settings or build_settings(trade_notify_mode="cloud")
    now_provider = now_provider or (lambda: datetime.now(ZoneInfo(settings.market_timezone)))
    start_now = now_provider()
    if is_premarket_monitor_finished(start_now):
        print(f"[{start_now:%Y-%m-%d %H:%M:%S %Z}] 已到盘前结束时间 09:30 ET，盘前推荐监控不启动。", flush=True)
        return

    run_lock = acquire_run_lock(settings.output_dir, "premarket_ma5_monitor.lock", "盘前 MA5 推荐监控")
    market_data = None
    loop_count = 0
    try:
        market_data = build_default_market_data(settings)
        print("盘前 MA5 推荐监控启动：只发送推荐提醒，不提交任何订单。", flush=True)
        notify_premarket_monitor_started(settings)
        while True:
            now_et = now_provider()
            if is_premarket_monitor_finished(now_et):
                print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 盘前推荐监控到达 09:30 ET，退出。", flush=True)
                break

            loop_count += 1
            try:
                run_premarket_recommendation_once(settings, market_data=market_data, now=now_et)
            except KeyboardInterrupt:
                print("盘前 MA5 推荐监控已停止。", flush=True)
                break
            except Exception as exc:
                print(f"本轮盘前推荐监控失败，继续等待下一轮：{short_error(exc)}", flush=True)

            if max_loops is not None and loop_count >= max_loops:
                print(f"已完成测试轮数 {max_loops}，退出。", flush=True)
                break

            sleep_now = now_provider()
            if is_premarket_monitor_finished(sleep_now):
                print(f"[{sleep_now:%Y-%m-%d %H:%M:%S %Z}] 盘前推荐监控到达 09:30 ET，退出。", flush=True)
                break
            poll_seconds = premarket_loop_poll_seconds(sleep_now)
            if poll_seconds <= 0:
                print(f"[{sleep_now:%Y-%m-%d %H:%M:%S %Z}] 盘前推荐监控到达 09:30 ET，退出。", flush=True)
                break
            print(f"下一轮：{poll_seconds}s 后继续监控。", flush=True)
            sleep(poll_seconds)
    finally:
        if market_data is not None and hasattr(market_data, "close"):
            market_data.close()
        run_lock.close()


def notify_premarket_monitor_started(settings: Settings) -> None:
    safe_send_openclaw_messages(
        settings,
        [render_premarket_monitor_start_message(settings)],
        context="premarket MA5 monitor started",
    )


def render_premarket_monitor_start_message(settings: Settings) -> str:
    return "\n".join(
        [
            "【盘前 MA5 推荐监控启动】",
            "结论：开始盘前监控。",
            "动作：只发送推荐提醒，不提交任何 Alpaca 订单。",
            "",
            "观察范围",
            "- 股票池：最近已收盘交易日涨幅 Top 50",
            f"- 观察文件：{premarket_watch_codes_path(settings)}",
            "",
            "提醒条件",
            f"- 盘前跌幅：>= {PREMARKET_MIN_DROP_PCT:.2%}",
            f"- MA5 位置：低于动态 MA5、从下方上穿 MA5，或在动态 MA5 上方 0%-{PREMARKET_ALERT_DISTANCE_PCT:.2%}",
            f"- 低于 MA5 去重：每支股票每天最多提醒 {PREMARKET_BELOW_MA5_ALERT_LIMIT} 次",
            f"- 轮询频率：每 {PREMARKET_MONITOR_POLL_SECONDS} 秒一轮，09:30 ET 自动停止",
        ]
    )


def evaluate_premarket_ma5_recommendation(
    snapshot: MarketSnapshot,
    *,
    alert_distance_pct: float = PREMARKET_ALERT_DISTANCE_PCT,
    min_drop_pct: float = PREMARKET_MIN_DROP_PCT,
    previous_ma5_position: str | None = None,
) -> PremarketRecommendation | None:
    """盘前跌幅达标后，按低于 MA5、上穿 MA5、上方接近 MA5 三类生成推荐。"""
    distance = ma5_distance_pct(snapshot)
    if distance is None:
        return None
    if premarket_drop_pct(snapshot) < min_drop_pct:
        return None
    alert_type = ALERT_TYPE_NEAR_ABOVE
    if distance < 0:
        bucket = 0
        alert_type = ALERT_TYPE_BELOW_MA5
    elif previous_ma5_position == MA5_POSITION_BELOW:
        bucket = alert_bucket_pct(distance)
        alert_type = ALERT_TYPE_CROSS_UP_MA5
    elif distance <= alert_distance_pct:
        bucket = alert_bucket_pct(distance)
    else:
        return None
    return PremarketRecommendation(
        symbol=snapshot.symbol,
        current_price=snapshot.current_price,
        today_ma5=snapshot.today_ma5,
        ma5_distance_pct=distance,
        current_gain_pct=snapshot.today_current_gain_pct,
        signal_gain_pct=snapshot.signal_day_gain_pct,
        price_source=snapshot.current_price_source,
        alert_bucket_pct=bucket,
        as_of=snapshot.as_of,
        alert_type=alert_type,
    )


def ma5_distance_pct(snapshot: MarketSnapshot) -> float | None:
    today_ma5 = snapshot.today_ma5
    if today_ma5 <= 0 or snapshot.current_price <= 0:
        return None
    return snapshot.current_price / today_ma5 - 1.0


def has_realtime_price(snapshot: MarketSnapshot) -> bool:
    source = (snapshot.current_price_source or "").lower()
    return (
        source.startswith("moomoo_snapshot:pre_price")
        or source.startswith("alpaca_latest_quote:")
        or source.startswith("alpaca_latest_trade:")
    )


def premarket_drop_pct(snapshot: MarketSnapshot) -> float:
    """盘前跌幅 = 当前价相对最近已收盘日收盘价的跌幅。"""
    return max(0.0, -snapshot.today_current_gain_pct)


def alert_bucket_pct(distance_pct: float) -> int:
    """把 0%-3% 的距离归到 0/1/2/3 档，价格更靠近时可再次提醒。"""
    return max(0, int(math.ceil(distance_pct * 100 - 1e-9)))


def render_premarket_recommendation_message(recommendation: PremarketRecommendation, signal_day) -> str:
    signal_day_text = f"{signal_day:%Y-%m-%d}" if signal_day else "unknown"
    as_of_text = recommendation.as_of.strftime("%Y-%m-%d %H:%M:%S %Z").strip() or str(recommendation.as_of)
    alert_title = recommendation_alert_title(recommendation)
    alert_note = recommendation_alert_note(recommendation)
    ma5_position = ma5_position_text(recommendation.ma5_distance_pct)
    premarket_drop = premarket_drop_pct_from_gain(recommendation.current_gain_pct)
    return "\n".join(
        [
            f"【盘前 MA5 推荐提醒】{recommendation.symbol}",
            "",
            f"结论：{alert_title}；{alert_note}。",
            "动作建议：加入重点观察；本系统只提醒，不提交 Alpaca 订单。",
            "",
            "核心价位",
            f"- 当前价：{recommendation.current_price:.4f}",
            f"- 动态 MA5：{recommendation.today_ma5:.4f}",
            f"- MA5 位置：{ma5_position}",
            "",
            "触发依据",
            f"- 股票池：最近已收盘交易日涨幅 Top 50（signal_date={signal_day_text}）",
            f"- 盘前跌幅：{premarket_drop:.2%}（要求 >= {PREMARKET_MIN_DROP_PCT:.2%}）",
            f"- 盘前涨跌幅：{recommendation.current_gain_pct:.2%}",
            f"- 信号日涨幅：{recommendation.signal_gain_pct:.2%}",
            "",
            "数据",
            f"- 价格来源：{recommendation.price_source or 'unknown'}",
            f"- 行情时间：{as_of_text}",
            "",
            "提醒规则",
            f"- 靠近 MA5 阈值：上方 0%-{PREMARKET_ALERT_DISTANCE_PCT:.2%}",
            f"- 低于 MA5：每天最多提醒 {PREMARKET_BELOW_MA5_ALERT_LIMIT} 次；上穿 MA5 会单独提醒",
        ]
    )


def load_alert_state(path: Path, now_et: datetime) -> dict:
    """读取当日提醒去重状态；跨日自动清空。"""
    day = f"{now_et.date():%Y-%m-%d}"
    if not path.exists():
        return {"day": day, "alerts": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"day": day, "alerts": {}}
    if state.get("day") != day:
        return {"day": day, "alerts": {}}
    if not isinstance(state.get("alerts"), dict):
        state["alerts"] = {}
    return state


def save_alert_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def symbol_alert_state(state: dict, symbol: str) -> dict:
    alerts = state.setdefault("alerts", {})
    raw = alerts.get(symbol)
    if isinstance(raw, dict):
        entry = raw
    elif isinstance(raw, list):
        entry = {"above_buckets": [str(value) for value in raw]}
    else:
        entry = {}

    entry.setdefault("above_buckets", [])
    entry.setdefault("below_count", 0)
    entry.setdefault("cross_up_sent", False)
    entry.setdefault("last_position", "")
    entry["above_buckets"] = [str(value) for value in entry.get("above_buckets", [])]
    try:
        entry["below_count"] = int(entry.get("below_count", 0))
    except (TypeError, ValueError):
        entry["below_count"] = 0
    entry["cross_up_sent"] = bool(entry.get("cross_up_sent", False))
    alerts[symbol] = entry
    return entry


def last_ma5_position(state: dict, symbol: str) -> str:
    return str(symbol_alert_state(state, symbol).get("last_position") or "")


def update_alert_ma5_position(state: dict, symbol: str, distance: float | None) -> None:
    position = ma5_position_from_distance(distance)
    if not position:
        return
    symbol_alert_state(state, symbol)["last_position"] = position


def ma5_position_from_distance(distance: float | None) -> str:
    if distance is None:
        return ""
    return MA5_POSITION_BELOW if distance < 0 else MA5_POSITION_ABOVE


def recommendation_with_state_details(state: dict, recommendation: PremarketRecommendation) -> PremarketRecommendation:
    if recommendation.alert_type != ALERT_TYPE_BELOW_MA5:
        return recommendation
    entry = symbol_alert_state(state, recommendation.symbol)
    return replace(recommendation, below_alert_number=int(entry.get("below_count", 0)) + 1)


def should_send_alert(state: dict, recommendation: PremarketRecommendation) -> bool:
    entry = symbol_alert_state(state, recommendation.symbol)
    if recommendation.alert_type == ALERT_TYPE_BELOW_MA5:
        return int(entry.get("below_count", 0)) < PREMARKET_BELOW_MA5_ALERT_LIMIT
    if recommendation.alert_type == ALERT_TYPE_CROSS_UP_MA5:
        return not bool(entry.get("cross_up_sent", False))
    return str(recommendation.alert_bucket_pct) not in {str(value) for value in entry.get("above_buckets", [])}


def mark_alert_sent(state: dict, recommendation: PremarketRecommendation) -> None:
    entry = symbol_alert_state(state, recommendation.symbol)
    if recommendation.alert_type == ALERT_TYPE_BELOW_MA5:
        entry["below_count"] = min(PREMARKET_BELOW_MA5_ALERT_LIMIT, int(entry.get("below_count", 0)) + 1)
        return
    if recommendation.alert_type == ALERT_TYPE_CROSS_UP_MA5:
        entry["cross_up_sent"] = True

    buckets = [str(value) for value in entry.get("above_buckets", [])]
    bucket = str(recommendation.alert_bucket_pct)
    if bucket not in buckets:
        buckets.append(bucket)
    entry["above_buckets"] = sorted(buckets, key=lambda value: int(value))


def duplicate_alert_note(state: dict, recommendation: PremarketRecommendation) -> str:
    entry = symbol_alert_state(state, recommendation.symbol)
    if recommendation.alert_type == ALERT_TYPE_BELOW_MA5:
        return f"今日低于 MA5 已提醒 {int(entry.get('below_count', 0))} 次，跳过重复提醒"
    if recommendation.alert_type == ALERT_TYPE_CROSS_UP_MA5:
        return "今日已发过从 MA5 下方上穿提醒，跳过重复提醒"
    return f"今日已发过 <= {recommendation.alert_bucket_pct}% 档，跳过重复提醒"


def recommendation_alert_note(recommendation: PremarketRecommendation) -> str:
    if recommendation.alert_type == ALERT_TYPE_BELOW_MA5:
        return f"低于 MA5 第 {recommendation.below_alert_number}/{PREMARKET_BELOW_MA5_ALERT_LIMIT} 次"
    if recommendation.alert_type == ALERT_TYPE_CROSS_UP_MA5:
        return "从 MA5 下方上穿"
    return f"提醒档 <= {recommendation.alert_bucket_pct}%"


def recommendation_alert_title(recommendation: PremarketRecommendation) -> str:
    if recommendation.alert_type == ALERT_TYPE_BELOW_MA5:
        suffix = f"（第 {recommendation.below_alert_number}/{PREMARKET_BELOW_MA5_ALERT_LIMIT} 次）" if recommendation.below_alert_number else ""
        return f"当前价低于动态 MA5{suffix}"
    if recommendation.alert_type == ALERT_TYPE_CROSS_UP_MA5:
        return "当前价从动态 MA5 下方上穿"
    return f"当前价在动态 MA5 上方 0%-{PREMARKET_ALERT_DISTANCE_PCT:.2%}"


def recommendation_status(recommendation: PremarketRecommendation) -> str:
    if recommendation.alert_type == ALERT_TYPE_BELOW_MA5:
        return "低于MA5"
    if recommendation.alert_type == ALERT_TYPE_CROSS_UP_MA5:
        return "上穿MA5"
    return "推荐"


def ma5_position_text(distance: float) -> str:
    if distance < 0:
        return f"MA5下方 {abs(distance):.2%}"
    return f"MA5上方 {distance:.2%}"


def premarket_drop_pct_from_gain(current_gain_pct: float) -> float:
    return max(0.0, -current_gain_pct)


def format_hold_reason(snapshot: MarketSnapshot, distance: float | None, alert_distance_pct: float, min_drop_pct: float) -> str:
    drop_pct = premarket_drop_pct(snapshot)
    if drop_pct < min_drop_pct:
        return f"盘前跌幅 {drop_pct:.2%}，低于提醒要求 {min_drop_pct:.2%}"
    if distance is None:
        return "MA5 无效，跳过"
    if distance < 0:
        return f"当前价低于动态 MA5 {abs(distance):.2%}"
    return f"距离动态 MA5 {distance:.2%}，高于提醒阈值 {alert_distance_pct:.2%}"


def print_premarket_header(now_et: datetime, watch_path: Path, signal_day, count: int, alert_distance_pct: float, min_drop_pct: float) -> None:
    signal_day_text = f"{signal_day:%Y-%m-%d}" if signal_day else "unknown"
    print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 盘前 MA5 推荐检查", flush=True)
    print(
        f"观察文件: {watch_path} | signal_date={signal_day_text} | 股票数={count} | "
        f"MA5距离阈值={alert_distance_pct:.2%} | 盘前跌幅要求={min_drop_pct:.2%}",
        flush=True,
    )


def print_premarket_rows(rows: list[PremarketRecommendation | PremarketObservation | tuple[str, str]]) -> None:
    if not rows:
        return
    print("盘前推荐明细：", flush=True)
    headers = ["代码", "状态", "当前价", "价格来源", "MA5", "MA5距离", "盘前涨跌幅", "信号日涨幅", "说明"]
    table: list[list[str]] = []
    for row in rows:
        if isinstance(row, PremarketRecommendation):
            table.append(
                [
                    row.symbol,
                    recommendation_status(row),
                    f"{row.current_price:.4f}",
                    short_price_source(row.price_source),
                    f"{row.today_ma5:.4f}",
                    f"{row.ma5_distance_pct:.2%}",
                    f"{row.current_gain_pct:.2%}",
                    f"{row.signal_gain_pct:.2%}",
                    row.notification_note or recommendation_alert_note(row),
                ]
            )
        elif isinstance(row, PremarketObservation):
            table.append(
                [
                    row.symbol,
                    "观察",
                    f"{row.current_price:.4f}" if row.current_price > 0 else "-",
                    short_price_source(row.price_source),
                    f"{row.today_ma5:.4f}" if row.today_ma5 > 0 else "-",
                    f"{row.ma5_distance_pct:.2%}" if row.ma5_distance_pct is not None else "-",
                    f"{row.current_gain_pct:.2%}" if row.current_gain_pct is not None else "-",
                    f"{row.signal_gain_pct:.2%}",
                    row.reason,
                ]
            )
        else:
            symbol, reason = row
            table.append([symbol, "观察", "-", "-", "-", "-", "-", "-", reason])

    widths = [len(header) for header in headers]
    for row in table:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], display_width(value))
    print(format_table_line(headers, widths), flush=True)
    print(format_table_line(["-" * width for width in widths], widths), flush=True)
    for row in table:
        print(format_table_line(row, widths), flush=True)


def format_table_line(values: list[str], widths: list[int]) -> str:
    return " | ".join(pad_display_width(value, width) for value, width in zip(values, widths))


def short_price_source(source: str) -> str:
    source = source or "unknown"
    return (
        source.replace("moomoo_snapshot:", "moomoo:")
        .replace("alpaca_latest_quote:", "alpaca:quote:")
        .replace("alpaca_latest_trade:", "alpaca:trade:")
        .replace("alpaca_daily_close:", "alpaca:daily:")
    )


def pad_display_width(value: str, width: int) -> str:
    text = str(value)
    return text + (" " * max(0, width - display_width(text)))


def display_width(value: str) -> int:
    """PowerShell/terminal tables need CJK characters counted as two cells."""
    width = 0
    for char in str(value):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def premarket_loop_poll_seconds(now_et: datetime) -> int:
    seconds_to_end = seconds_until_premarket_monitor_end(now_et)
    if seconds_to_end <= 0:
        return 0
    if is_premarket_time(now_et):
        return max(1, min(PREMARKET_MONITOR_POLL_SECONDS, seconds_to_end))
    return max(1, min(PREMARKET_WAIT_POLL_SECONDS, seconds_to_end))
