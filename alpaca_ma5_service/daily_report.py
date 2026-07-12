from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .afterhours_high_low import afterhours_watch_codes_path
from .afterhours_monitor import AFTERHOURS_DROP_SIGNAL_THRESHOLD, AFTERHOURS_RANGE_RATIO_THRESHOLD
from .config import Settings, build_settings
from .models import is_executed_order_status
from .openclaw_notify import safe_send_openclaw_messages
from .premarket_monitor import PREMARKET_ALERT_DISTANCE_PCT, PREMARKET_MIN_DROP_PCT
from .premarket_watchlist import premarket_watch_codes_path
from .run_lock import acquire_run_lock
from .state import orders_file
from .strategy_ma5_dip import MAX_BUY_TODAY_CURRENT_GAIN_PCT
from .watchlist import read_watch_codes


DAILY_REPORT_STATE_NAME = "daily_monitor_report_state.json"
DAILY_REPORT_LOCK_NAME = "daily_monitor_report.lock"
REPORT_LINE = "===================="
SECTION_LINE = "--------------------"

PREMARKET_HEADERS = ["代码", "状态", "当前价", "价格来源", "MA5", "MA5距离", "盘前涨跌幅", "信号日涨幅", "说明"]
INTRADAY_HEADERS = ["代码", "动作", "当前价", "开盘", "MA5", "开盘MA5", "信号涨幅", "当前涨幅", "买/卖点", "订单", "原因"]
AFTERHOURS_HEADERS = ["股票", "当前价", "来源", "收盘", "跌幅", "提醒线", "参考价", "状态", "说明"]


@dataclass(frozen=True)
class ParsedRow:
    kind: str
    values: dict[str, str]


@dataclass(frozen=True)
class ClosestSignal:
    symbol: str
    summary: str
    reason: str
    score: float = 0.0


@dataclass(frozen=True)
class OrderStats:
    rows: list[dict[str, str]]
    executed_buys: list[dict[str, str]]
    executed_sells: list[dict[str, str]]
    buy_attempts: list[dict[str, str]]
    sell_attempts: list[dict[str, str]]


def send_daily_monitor_report(
    settings: Settings | None = None,
    *,
    now_et: datetime | None = None,
    force: bool = False,
) -> bool:
    """Build and send the end-of-day MA5 monitor report to the cloud agent once per day."""
    settings = settings or build_settings(trade_notify_mode="cloud")
    settings = settings if settings.trade_notify_mode == "cloud" else _replace_notify_mode(settings, "cloud")
    now_et = now_et or datetime.now(ZoneInfo(settings.market_timezone))
    report_day = now_et.date()

    lock = acquire_run_lock(settings.output_dir, DAILY_REPORT_LOCK_NAME, "MA5 每日监控报告")
    try:
        state = load_report_state(settings.output_dir)
        day_key = report_day.isoformat()
        if not force and day_key in set(state.get("sent_dates", [])):
            print(f"MA5 每日监控报告已发送过：{day_key}", flush=True)
            return False

        report = build_daily_monitor_report(settings, report_day, now_et=now_et)
        write_daily_report_file(settings.output_dir, report_day, report)
        sent = safe_send_openclaw_messages(
            settings,
            [report],
            context=f"daily MA5 monitor report {day_key}",
        )
        if sent is False:
            return False
        mark_report_sent(settings.output_dir, report_day, now_et)
        return True
    finally:
        lock.close()


def build_daily_monitor_report(
    settings: Settings,
    report_day: date,
    *,
    now_et: datetime | None = None,
) -> str:
    now_et = now_et or datetime.now(ZoneInfo(settings.market_timezone))
    log_path = monitor_auto_log_path(settings.output_dir, report_day)
    log_lines = read_text_lines(log_path)
    parsed_rows = parse_monitor_tables(log_lines)
    orders = load_order_stats(settings.output_dir, report_day)

    premarket_rows = [row for row in parsed_rows if row.kind == "premarket"]
    intraday_rows = [row for row in parsed_rows if row.kind == "intraday"]
    afterhours_rows = [row for row in parsed_rows if row.kind == "afterhours"]

    premarket_watch_path = premarket_watch_codes_path(settings)
    intraday_watch_path = settings.watch_codes_file
    afterhours_watch_path = afterhours_watch_codes_path(settings)
    premarket_watch_count = len(read_watch_codes(premarket_watch_path))
    intraday_watch_count = len(read_watch_codes(intraday_watch_path))
    afterhours_watch_count = len(read_watch_codes(afterhours_watch_path))
    afterhours_candidate_count = csv_row_count(settings.output_dir / f"afterhours_candidates_{report_day:%Y-%m-%d}.csv")

    premarket_sent_count = count_lines_containing(log_lines, "premarket MA5 recommendation")
    afterhours_alert_batches = count_lines_containing(log_lines, "afterhours high/low alert signal")
    premarket_alert_symbols = unique_symbols_from_rows(
        premarket_rows,
        symbol_field="代码",
        predicate=lambda values: values.get("状态", "") not in {"观察", ""},
    )
    afterhours_alert_symbols = unique_symbols_from_rows(
        afterhours_rows,
        symbol_field="股票",
        predicate=lambda values: "提醒" in values.get("状态", ""),
    )
    intraday_round_count = count_intraday_rounds(log_lines)

    premarket_closest = closest_premarket_signal(premarket_rows)
    intraday_closest = closest_intraday_buy_signal(intraday_rows)
    afterhours_closest = closest_afterhours_signal(afterhours_rows)

    intraday_no_buy_reason = intraday_no_buy_explanation(orders, intraday_closest, intraday_rows)
    intraday_no_buy_short = intraday_no_buy_headline(orders, intraday_closest, intraday_rows)
    order_summary = render_order_summary(orders)
    order_short = render_order_summary_short(orders)
    afterhours_count = afterhours_candidate_count if afterhours_candidate_count is not None else afterhours_watch_count

    lines = [
        f"【MA5 每日复盘】{report_day:%Y-%m-%d}",
        REPORT_LINE,
        "重点",
        f"1. 订单：{order_short}",
        f"2. 盘中未买原因：{intraday_no_buy_short}",
        f"3. 最接近买入：{format_signal_headline(intraday_closest)}",
        f"4. 盘前/盘后：只提醒，不下单。",
        REPORT_LINE,
        "总览",
        format_report_table(
            ["时段", "模式", "数量", "结果", "重点"],
            [
                ["盘前", "提醒-only", f"{premarket_watch_count}只", f"提醒{premarket_sent_count}次", format_signal_headline(premarket_closest, limit=44)],
                ["盘中", "可下单", f"{intraday_watch_count}只", order_short, format_signal_headline(intraday_closest, limit=44)],
                ["盘后", "提醒-only", f"{afterhours_count}只", f"提醒{afterhours_alert_batches}批", format_signal_headline(afterhours_closest, limit=44)],
            ],
        ),
        REPORT_LINE,
        "盘前",
        format_report_table(
            ["项目", "内容"],
            [
                ["观察池", f"Top50 / {premarket_watch_count}只"],
                ["提醒规则", f"跌幅>={PREMARKET_MIN_DROP_PCT:.0%}；低于/上穿/上方{PREMARKET_ALERT_DISTANCE_PCT:.0%}内靠近MA5"],
                ["提醒结果", f"{premarket_sent_count}次；{format_symbol_list(premarket_alert_symbols, limit=8)}"],
                ["最接近", format_signal_with_reason(premarket_closest, limit=90)],
                ["下单", "不下单，只发云端提醒"],
            ],
        ),
        SECTION_LINE,
        "盘中",
        format_report_table(
            ["项目", "内容"],
            [
                ["观察池", f"{intraday_watch_count}只"],
                ["买入窗口", "09:30-12:00 ET"],
                ["买入门槛", f"当前跌幅约>={abs(MAX_BUY_TODAY_CURRENT_GAIN_PCT):.0%}"],
                ["订单结果", order_summary],
                ["未买原因", intraday_no_buy_short],
                ["最接近", format_signal_with_reason(intraday_closest, limit=100)],
            ],
        ),
        SECTION_LINE,
        "盘后",
        format_report_table(
            ["项目", "内容"],
            [
                ["候选池", f"{afterhours_count}只；high/low>{AFTERHOURS_RANGE_RATIO_THRESHOLD:g}"],
                ["提醒规则", f"盘后跌幅>{AFTERHOURS_DROP_SIGNAL_THRESHOLD:.0%}"],
                ["提醒结果", f"{afterhours_alert_batches}批；{format_symbol_list(afterhours_alert_symbols, limit=8)}"],
                ["最接近", format_signal_with_reason(afterhours_closest, limit=90)],
                ["下单", "不下单，只发云端提醒"],
            ],
        ),
        REPORT_LINE,
        "证据",
        format_report_table(
            ["文件", "状态"],
            [
                ["日志", log_path.name if log_path.exists() else "未找到当天 monitor_auto 日志"],
                ["订单CSV", orders_file(settings.output_dir, report_day).name if orders.rows else "无本地订单CSV"],
                ["盘后候选", f"afterhours_candidates_{report_day:%Y-%m-%d}.csv"],
            ],
        ),
        f"报告生成时间：{now_et:%Y-%m-%d %H:%M:%S %Z}",
    ]
    return "\n".join(lines)


def _replace_notify_mode(settings: Settings, mode: str) -> Settings:
    from dataclasses import replace

    return replace(settings, trade_notify_mode=mode)


def monitor_auto_log_path(output_dir: Path, report_day: date) -> Path:
    return output_dir / "logs" / f"monitor_auto_{report_day:%Y%m%d}.out.log"


def read_text_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def parse_monitor_tables(lines: list[str]) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    current_kind = ""
    current_headers: list[str] = []
    for line in lines:
        cells = split_table_line(line)
        if not cells:
            if current_kind and is_plain_separator_line(line):
                continue
            current_kind = ""
            current_headers = []
            continue
        matched = table_kind_for_headers(cells)
        if matched:
            current_kind, current_headers = matched
            continue
        if not current_kind or is_separator_cells(cells):
            continue
        if len(cells) < len(current_headers):
            continue
        values = {header: cells[index].strip() for index, header in enumerate(current_headers)}
        rows.append(ParsedRow(current_kind, values))
    return rows


def split_table_line(line: str) -> list[str]:
    if "|" not in line:
        return []
    return [cell.strip() for cell in line.strip().split("|")]


def table_kind_for_headers(cells: list[str]) -> tuple[str, list[str]] | None:
    candidates = [
        ("premarket", PREMARKET_HEADERS),
        ("intraday", INTRADAY_HEADERS),
        ("afterhours", AFTERHOURS_HEADERS),
    ]
    for kind, headers in candidates:
        if cells[: len(headers)] == headers:
            return kind, headers
    return None


def is_separator_cells(cells: list[str]) -> bool:
    for cell in cells:
        stripped = cell.strip()
        if not stripped:
            continue
        if set(stripped) - {"-", "+"}:
            return False
    return True


def is_plain_separator_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not (set(stripped) - {"-", "+", " "})


def count_lines_containing(lines: list[str], needle: str) -> int:
    return sum(1 for line in lines if needle in line and "Trade notify" in line and "sent" in line)


def count_intraday_rounds(lines: list[str]) -> int:
    return sum(1 for line in lines if line.startswith("本轮完成：观察 ") and "买入" in line and "卖出" in line)


def unique_symbols_from_rows(rows: list[ParsedRow], *, symbol_field: str, predicate) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        values = row.values
        symbol = values.get(symbol_field, "").strip()
        if not symbol or symbol in seen or not predicate(values):
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def closest_premarket_signal(rows: list[ParsedRow]) -> ClosestSignal | None:
    best: tuple[float, ClosestSignal] | None = None
    for row in rows:
        values = row.values
        symbol = values.get("代码", "")
        if not symbol:
            continue
        current_gain = parse_percent(values.get("盘前涨跌幅", ""))
        ma5_distance = parse_percent(values.get("MA5距离", ""))
        status = values.get("状态", "")
        reason = values.get("说明", "")
        if current_gain is None:
            continue
        drop = max(0.0, -current_gain)
        if status not in {"观察", ""}:
            priority = 0
        elif drop >= PREMARKET_MIN_DROP_PCT and ma5_distance is not None and (ma5_distance < 0 or ma5_distance <= PREMARKET_ALERT_DISTANCE_PCT):
            priority = 1
        elif drop >= PREMARKET_MIN_DROP_PCT:
            priority = 2
        else:
            priority = 3
        distance_gap = 0.0 if ma5_distance is None else max(0.0, ma5_distance - PREMARKET_ALERT_DISTANCE_PCT)
        drop_gap = max(0.0, PREMARKET_MIN_DROP_PCT - drop)
        score = priority * 100.0 + drop_gap + distance_gap
        summary = (
            f"{symbol}，盘前涨跌 {format_pct(current_gain)}，"
            f"MA5距离 {format_optional_pct(ma5_distance)}"
        )
        candidate = ClosestSignal(symbol, summary, reason or "无说明", score)
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1] if best else None


def closest_intraday_buy_signal(rows: list[ParsedRow]) -> ClosestSignal | None:
    best: tuple[float, ClosestSignal] | None = None
    required_drop = intraday_required_drop(rows)
    for row in rows:
        values = row.values
        symbol = values.get("代码", "")
        if not symbol:
            continue
        action = values.get("动作", "")
        if action in {"持有", "卖出", "卖出未成", "跳过卖出"}:
            continue
        current_gain = parse_percent(values.get("当前涨幅", ""))
        current_price = parse_float(values.get("当前价", ""))
        reason = values.get("原因", "")
        if current_gain is None:
            continue
        drop = max(0.0, -current_gain)
        priority = 4
        score_detail = max(0.0, required_drop - drop)
        if "跌幅未到" in reason:
            priority = 0
        elif "当前价高于触发上沿" in reason:
            priority = 1
            trigger_price = parse_trigger_upper(reason)
            if trigger_price and current_price:
                score_detail += max(0.0, current_price / trigger_price - 1.0)
        elif "买入次数达到上限" in reason or "开放买单" in reason or "暂停" in reason:
            priority = 2
        elif "信号日涨幅" in reason or "买入要求" in reason:
            priority = 3
        score = priority * 100.0 + score_detail
        summary = f"{symbol}，当前涨跌 {format_pct(current_gain)}，动作 {action or '-'}"
        if required_drop > 0 and drop < required_drop:
            summary += f"，还差 {format_pct(required_drop - drop)} 才到 {format_pct(required_drop)} 买入跌幅"
        candidate = ClosestSignal(symbol, summary, reason or "无说明", score)
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1] if best else None


def closest_afterhours_signal(rows: list[ParsedRow]) -> ClosestSignal | None:
    best: tuple[float, ClosestSignal] | None = None
    for row in rows:
        values = row.values
        symbol = values.get("股票", "")
        drop = parse_percent(values.get("跌幅", ""))
        if not symbol or drop is None:
            continue
        status = values.get("状态", "")
        reason = values.get("说明", "")
        priority = 0 if "提醒" in status else 1
        score = priority * 100.0 + max(0.0, AFTERHOURS_DROP_SIGNAL_THRESHOLD - drop)
        summary = f"{symbol}，盘后跌幅 {format_pct(drop)}，状态 {status or '-'}"
        if drop < AFTERHOURS_DROP_SIGNAL_THRESHOLD:
            summary += f"，还差 {format_pct(AFTERHOURS_DROP_SIGNAL_THRESHOLD - drop)} 到提醒线"
        candidate = ClosestSignal(symbol, summary, reason or "无说明", score)
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1] if best else None


def intraday_required_drop(rows: list[ParsedRow]) -> float:
    for row in rows:
        match = re.search(r"需跌幅\s*>=\s*([0-9.]+)%", row.values.get("原因", ""))
        if match:
            return float(match.group(1)) / 100.0
        match = re.search(r"跌幅未到\s*([0-9.]+)%", row.values.get("原因", ""))
        if match:
            return float(match.group(1)) / 100.0
    return abs(MAX_BUY_TODAY_CURRENT_GAIN_PCT)


def parse_trigger_upper(reason: str) -> float | None:
    match = re.search(r"触发上沿\s*([0-9]+(?:\.[0-9]+)?)", reason)
    return float(match.group(1)) if match else None


def intraday_no_buy_explanation(orders: OrderStats, closest: ClosestSignal | None, rows: list[ParsedRow]) -> str:
    if orders.executed_buys:
        return f"已有 {len(orders.executed_buys)} 笔本地成交买入。"
    if orders.buy_attempts:
        statuses = ", ".join(f"{row.get('symbol')} {row.get('status')}" for row in orders.buy_attempts[:4])
        return f"有买入尝试但未成交：{statuses}。"
    if closest:
        reason = compact_text(closest.reason)
        return f"没有本地成交买入；最接近的是 {closest.summary}；原因：{reason}"
    if rows:
        return "没有本地成交买入；盘中表格有记录，但未解析到有效买入候选。"
    return "没有本地成交买入；未解析到盘中监控明细，优先检查当天 monitor_auto 日志是否完整。"


def intraday_no_buy_headline(orders: OrderStats, closest: ClosestSignal | None, rows: list[ParsedRow]) -> str:
    if orders.executed_buys:
        return f"已有{len(orders.executed_buys)}笔成交买入"
    if orders.buy_attempts:
        statuses = ", ".join(f"{row.get('symbol')} {row.get('status')}" for row in orders.buy_attempts[:3])
        return f"有买入尝试但未成交：{statuses}"
    if closest:
        return f"未达到买入条件；{format_signal_headline(closest, limit=76)}"
    if rows:
        return "未买；有盘中记录但未解析到有效买入候选"
    return "未买；当天盘中日志不完整或未解析到明细"


def load_order_stats(output_dir: Path, report_day: date) -> OrderStats:
    path = orders_file(output_dir, report_day)
    if not path.exists():
        return OrderStats([], [], [], [], [])
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    buy_attempts = [row for row in rows if row.get("side") == "BUY"]
    sell_attempts = [row for row in rows if row.get("side") == "SELL"]
    executed_buys = [row for row in buy_attempts if is_executed_order_status(row.get("status", ""))]
    executed_sells = [row for row in sell_attempts if is_executed_order_status(row.get("status", ""))]
    return OrderStats(rows, executed_buys, executed_sells, buy_attempts, sell_attempts)


def csv_row_count(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def render_order_summary(orders: OrderStats) -> str:
    if not orders.rows:
        return "无本地订单记录"
    return (
        f"买入成交 {len(orders.executed_buys)}/{len(orders.buy_attempts)} 笔，"
        f"卖出成交 {len(orders.executed_sells)}/{len(orders.sell_attempts)} 笔"
    )


def render_order_summary_short(orders: OrderStats) -> str:
    if not orders.rows:
        return "无本地订单"
    return f"买入{len(orders.executed_buys)}/{len(orders.buy_attempts)}；卖出{len(orders.executed_sells)}/{len(orders.sell_attempts)}"


def render_order_detail(orders: OrderStats) -> str:
    if not orders.rows:
        return "无本地订单 CSV"
    parts = []
    for row in orders.rows[:6]:
        parts.append(
            f"{row.get('created_at', '')} {row.get('side', '')} {row.get('symbol', '')} "
            f"{row.get('status', '')} qty={row.get('quantity', '')} price={row.get('price', '')}"
        )
    suffix = f"；另 {len(orders.rows) - len(parts)} 笔" if len(orders.rows) > len(parts) else ""
    return "；".join(parts) + suffix


def format_report_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        safe_row = [compact_table_cell(value) for value in row]
        lines.append("| " + " | ".join(safe_row) + " |")
    return "\n".join(lines)


def compact_table_cell(value: object) -> str:
    return " ".join(str(value or "-").replace("|", "/").split())


def format_symbol_list(symbols: list[str], *, limit: int = 10) -> str:
    if not symbols:
        return "无"
    shown = ", ".join(symbols[:limit])
    return shown if len(symbols) <= limit else f"{shown} ... 另 {len(symbols) - limit}只"


def format_closest(signal: ClosestSignal | None) -> str:
    if signal is None:
        return "无可解析记录"
    return f"{signal.summary}；说明：{compact_text(signal.reason)}"


def format_signal_headline(signal: ClosestSignal | None, *, limit: int = 80) -> str:
    if signal is None:
        return "无"
    return compact_text(signal.summary.replace("，", " / "), limit=limit)


def format_signal_with_reason(signal: ClosestSignal | None, *, limit: int = 100) -> str:
    if signal is None:
        return "无"
    summary = format_signal_headline(signal, limit=limit)
    reason = compact_text(first_reason_clause(signal.reason), limit=max(30, limit // 2))
    return f"{summary}；{reason}"


def first_reason_clause(reason: str) -> str:
    text = compact_text(reason, limit=200)
    for separator in ("；", "。", ". "):
        if separator in text:
            first = text.split(separator, 1)[0].strip()
            if first:
                return first
    return text


def parse_percent(value: str) -> float | None:
    text = value.strip().replace(",", "")
    if not text or text in {"-", "未知"}:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text) / 100.0
    except ValueError:
        return None


def parse_float(value: str) -> float | None:
    text = value.strip().replace(",", "")
    if not text or text in {"-", "未知"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def format_pct(value: float) -> str:
    return f"{value:.2%}"


def format_optional_pct(value: float | None) -> str:
    return "-" if value is None else format_pct(value)


def compact_text(value: str, *, limit: int = 180) -> str:
    text = " ".join(str(value or "-").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def report_state_path(output_dir: Path) -> Path:
    return output_dir / DAILY_REPORT_STATE_NAME


def load_report_state(output_dir: Path) -> dict:
    path = report_state_path(output_dir)
    if not path.exists():
        return {"sent_dates": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sent_dates": []}
    if not isinstance(state.get("sent_dates"), list):
        state["sent_dates"] = []
    return state


def mark_report_sent(output_dir: Path, report_day: date, now_et: datetime) -> None:
    state = load_report_state(output_dir)
    sent_dates = [str(value) for value in state.get("sent_dates", [])]
    day_key = report_day.isoformat()
    if day_key not in sent_dates:
        sent_dates.append(day_key)
    state["sent_dates"] = sorted(sent_dates)
    state["last_sent_at"] = now_et.isoformat(timespec="seconds")
    path = report_state_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_daily_report_file(output_dir: Path, report_day: date, report: str) -> Path:
    path = output_dir / "daily_reports" / f"ma5_daily_report_{report_day:%Y-%m-%d}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    settings = build_settings(trade_notify_mode="cloud")
    now = datetime.now(ZoneInfo(settings.market_timezone))
    print(build_daily_monitor_report(settings, now.date(), now_et=now))
