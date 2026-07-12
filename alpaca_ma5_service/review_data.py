from __future__ import annotations

import ast
import csv
import json
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .trading_calendar import offline_trading_day_decision


SCHEMA_VERSION = "1.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
BUY_WINDOW_START = time(9, 30)
BUY_WINDOW_END = time(12, 0)

INTRADAY_FIELDS = (
    "symbol",
    "action",
    "current_price",
    "today_open",
    "ma5",
    "open_ma5",
    "signal_gain_pct",
    "current_gain_pct",
    "decision_price",
    "order_status",
    "reason",
)
PREMARKET_FIELDS = (
    "symbol",
    "status",
    "current_price",
    "price_source",
    "ma5",
    "ma5_distance_pct",
    "premarket_gain_pct",
    "signal_gain_pct",
    "reason",
)
AFTERHOURS_FIELDS = (
    "symbol",
    "current_price",
    "price_source",
    "regular_close",
    "drop_pct",
    "alert_price",
    "reference_price",
    "status",
    "reason",
)

_BRACKET_TIME_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?: [A-Z]{2,5})?\]\s*(.*)$")
_RUN_TIME_RE = re.compile(r"运行时间\s*[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_PERCENT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?%")
_TRIGGER_RE = re.compile(r"触发上沿\s*([0-9]+(?:\.[0-9]+)?)")
_REQUIRED_DROP_RE = re.compile(r"(?:需跌幅\s*>=|跌幅未到|跌幅尚未达到)\s*([0-9]+(?:\.[0-9]+)?)%")
_NOTIFY_SYMBOL_RE = re.compile(r"premarket MA5 recommendation\s+(US\.[A-Z0-9.]+)", re.IGNORECASE)

_LOG_CACHE_LOCK = threading.Lock()
_LOG_CACHE: dict[tuple[str, int, int], "ParsedMonitorLog"] = {}
_BROKER_CACHE_LOCK = threading.Lock()
_BROKER_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class Observation:
    phase: str
    observed_at: datetime | None
    source_line: int
    round_id: str
    values: dict[str, str]


@dataclass(frozen=True)
class ParsedRound:
    phase: str
    observed_at: datetime | None
    rows: tuple[Observation, ...]
    complete: bool
    source_line: int


@dataclass(frozen=True)
class ParsedMonitorLog:
    observations: tuple[Observation, ...]
    rounds: tuple[ParsedRound, ...]
    phase_round_counts: dict[str, int]
    phase_ranges: dict[str, tuple[datetime | None, datetime | None]]
    premarket_notification_count: int
    premarket_notification_symbols: tuple[str, ...]
    open_buy_pause_rounds: int
    open_buy_pause_rows: int
    line_count: int


def list_review_dates(base_dir: Path | None = None) -> list[str]:
    root = Path(base_dir or PROJECT_ROOT).resolve()
    output_dir = root / "outputs"
    values: set[date] = set()
    patterns = (
        (output_dir / "logs", re.compile(r"monitor_auto_(\d{8})\.out\.log$"), "%Y%m%d"),
        (output_dir, re.compile(r"orders_(\d{4}-\d{2}-\d{2})\.csv$"), "%Y-%m-%d"),
        (output_dir, re.compile(r"buy_exclusions_(\d{4}-\d{2}-\d{2})\.csv$"), "%Y-%m-%d"),
        (output_dir, re.compile(r"afterhours_candidates_(\d{4}-\d{2}-\d{2})\.csv$"), "%Y-%m-%d"),
    )
    for folder, pattern, fmt in patterns:
        if not folder.exists():
            continue
        for path in folder.iterdir():
            match = pattern.fullmatch(path.name)
            if not match:
                continue
            try:
                values.add(datetime.strptime(match.group(1), fmt).date())
            except ValueError:
                continue
    return [value.isoformat() for value in sorted(values, reverse=True)]


def build_daily_review(
    requested_date: str | date | None = None,
    *,
    include_broker: bool = False,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(base_dir or PROJECT_ROOT).resolve()
    output_dir = root / "outputs"
    now_et = datetime.now(ET)
    requested = _coerce_date(requested_date) if requested_date is not None else now_et.date()
    available = [date.fromisoformat(value) for value in list_review_dates(root)]
    review_day, fallback = _resolve_review_date(requested, available)
    previous_value, next_value = _neighbor_dates(review_day, available)
    trading = offline_trading_day_decision(requested)

    log_path = output_dir / "logs" / f"monitor_auto_{review_day:%Y%m%d}.out.log"
    parsed = parse_monitor_log(log_path)
    signal_day = previous_trading_day(review_day)
    intraday_candidates_path = output_dir / f"watch_candidates_{signal_day:%Y-%m-%d}.csv"
    premarket_candidates_path = output_dir / f"premarket_watch_candidates_{signal_day:%Y-%m-%d}.csv"
    afterhours_candidates_path = output_dir / f"afterhours_candidates_{review_day:%Y-%m-%d}.csv"
    exclusion_path = output_dir / f"buy_exclusions_{review_day:%Y-%m-%d}.csv"
    local_orders_path = output_dir / f"orders_{review_day:%Y-%m-%d}.csv"
    error_log_path = output_dir / "logs" / f"monitor_auto_{review_day:%Y%m%d}.err.log"

    intraday_candidates = _read_csv(intraday_candidates_path)
    premarket_candidates = _read_csv(premarket_candidates_path)
    afterhours_candidates = _read_csv(afterhours_candidates_path)
    exclusions = _read_csv(exclusion_path)
    local_orders, local_file_state = _load_local_orders(local_orders_path)
    strategy_config = _read_runtime_config(root / "monitor_ma5_forever.py")
    required_drop = abs(float(strategy_config.get("MA5_MAX_BUY_TODAY_CURRENT_GAIN_PCT", -0.12)))
    candidate_symbols = _candidate_symbols(intraday_candidates, parsed)
    premarket_symbols = _candidate_symbols(premarket_candidates, parsed, phase="premarket")
    afterhours_symbols = _candidate_symbols(afterhours_candidates, parsed, phase="afterhours")

    intraday_observations = [item for item in parsed.observations if item.phase == "intraday"]
    by_symbol: dict[str, list[Observation]] = defaultdict(list)
    for item in intraday_observations:
        symbol = normalize_symbol(item.values.get("symbol", ""))
        if symbol:
            by_symbol[symbol].append(item)

    exclusion_map = {
        normalize_symbol(row.get("symbol", "")): row
        for row in exclusions
        if normalize_symbol(row.get("symbol", ""))
    }
    position_events, last_observed_positions = _position_events(parsed.rounds, candidate_symbols)
    global_buy_window_best = _best_observation(
        (item for symbol in candidate_symbols for item in by_symbol.get(symbol, [])),
        required_drop,
        window_only=True,
    )
    global_all_day_closest = _best_observation(
        (item for symbol in candidate_symbols for item in by_symbol.get(symbol, [])),
        required_drop,
        window_only=False,
    )

    symbols: dict[str, dict[str, Any]] = {}
    reason_counter: Counter[str] = Counter()
    for symbol in candidate_symbols:
        observations = sorted(by_symbol.get(symbol, []), key=_observation_sort_key)
        latest = observations[-1] if observations else None
        latest_priced = next((item for item in reversed(observations) if _number(item.values.get("current_price")) is not None), None)
        window_best = _best_observation(observations, required_drop, window_only=True)
        all_day_best = _best_observation(observations, required_drop, window_only=False)
        excluded = symbol in exclusion_map or any("今日排除" in item.values.get("reason", "") for item in observations)
        reason_code, reason_label = _strategy_reason(latest_priced or latest, excluded)
        reason_counter[reason_label] += 1
        bucket = "excluded" if excluded else "not_bought"
        if (
            global_all_day_closest is not None
            and normalize_symbol(global_all_day_closest.values.get("symbol", "")) == symbol
            and not _in_buy_window(global_all_day_closest.observed_at)
            and not excluded
        ):
            bucket = "window_outside_closest"
        symbol_payload = {
            "symbol": symbol,
            "ticker": symbol.removeprefix("US."),
            "source_labels": ["策略观察"],
            "bucket": bucket,
            "status_label": "今日排除" if excluded else ("窗口外最接近" if bucket == "window_outside_closest" else "策略未买"),
            "severity": "critical" if excluded else ("warning" if bucket == "window_outside_closest" else "neutral"),
            "reason_code": reason_code,
            "reason": _strategy_reason_text(latest_priced or latest, exclusion_map.get(symbol)),
            "buy_window_best": _snapshot(window_best, required_drop),
            "all_day_closest": _snapshot(all_day_best, required_drop),
            "latest": _snapshot(latest, required_drop),
            "latest_priced": _snapshot(latest_priced, required_drop),
            "orders": [],
            "position_events": [],
            "buy_filled_qty": 0.0,
            "buy_avg_price": None,
            "sell_filled_qty": 0.0,
            "sell_avg_price": None,
            "net_cash_flow": None,
            "current_position_qty": 0.0,
            "local_ledger_match": "not_applicable",
            "evidence": _symbol_evidence(observations, log_path),
        }
        symbols[symbol] = symbol_payload

    for event in position_events:
        symbol = event["symbol"]
        if symbol in symbols:
            symbols[symbol]["position_events"].append(event)
            continue
        symbols[symbol] = {
            "symbol": symbol,
            "ticker": symbol.removeprefix("US."),
            "source_labels": ["监控日志"],
            "bucket": "position_unreconciled",
            "status_label": "持仓变化待核对",
            "severity": "critical",
            "reason_code": "unreconciled_position_change",
            "reason": "监控日志观察到持仓增减，但本地订单账本没有可匹配记录。",
            "buy_window_best": None,
            "all_day_closest": None,
            "latest": _snapshot(by_symbol.get(symbol, [None])[-1] if by_symbol.get(symbol) else None, required_drop),
            "latest_priced": None,
            "orders": [],
            "position_events": [event],
            "buy_filled_qty": 0.0,
            "buy_avg_price": None,
            "sell_filled_qty": 0.0,
            "sell_avg_price": None,
            "net_cash_flow": None,
            "current_position_qty": 0.0,
            "local_ledger_match": "missing" if local_file_state == "missing" else "unmatched",
            "evidence": [],
        }

    broker = {"mode": None, "status": "not_requested", "synced_at": None, "positions": [], "error": None}
    broker_orders: list[dict[str, Any]] = []
    if include_broker:
        try:
            snapshot = load_broker_snapshot(review_day)
            broker = snapshot["broker"]
            broker_orders = snapshot["orders"]
            _merge_broker_symbols(symbols, broker_orders, broker["positions"], local_orders, local_file_state)
        except Exception as exc:
            broker = {
                "mode": None,
                "status": "unavailable",
                "synced_at": datetime.now(ET).isoformat(timespec="seconds"),
                "positions": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

    all_orders = _normalized_local_orders(local_orders) + broker_orders
    attention = _build_attention(
        local_file_state=local_file_state,
        local_orders=local_orders,
        broker=broker,
        broker_orders=broker_orders,
        parsed=parsed,
        position_events=position_events,
    )
    timeline = _build_timeline(
        review_day,
        parsed,
        exclusions,
        broker_orders,
        position_events,
        global_buy_window_best,
        global_all_day_closest,
        required_drop,
        log_path,
    )
    phases = _phase_payloads(parsed, len(premarket_symbols), len(candidate_symbols), len(afterhours_symbols))
    source_manifest = _source_manifest(
        {
            "monitor_auto": log_path,
            "monitor_error": error_log_path,
            "local_orders": local_orders_path,
            "buy_exclusions": exclusion_path,
            "intraday_candidates": intraday_candidates_path,
            "premarket_candidates": premarket_candidates_path,
            "afterhours_candidates": afterhours_candidates_path,
        },
        parsed,
        local_file_state,
    )
    broker_groups = _broker_symbol_groups(broker_orders)
    net_cash = sum((_decimal(order.get("filled_value")) or Decimal(0)) * (Decimal(1) if order.get("side") == "SELL" else Decimal(-1)) for order in broker_orders)
    has_broker_fills = any((_decimal(order.get("filled_qty")) or Decimal(0)) > 0 for order in broker_orders)
    net_cash_value = float(net_cash) if include_broker and has_broker_fills else None
    broker_bought = len(broker_groups["bought"])
    broker_closed = len(broker_groups["closed"])
    broker_unfilled = len(broker_groups["unfilled_buy"])
    excluded_count = len(exclusion_map)
    quality_status = "critical" if any(item["severity"] == "critical" for item in attention) else ("warning" if attention else "healthy")
    broker_status = broker.get("status", "not_requested")

    market_banner = ""
    if fallback:
        reason = "今日休市" if not trading.is_trading_day else "当天尚无完整复盘数据"
        market_banner = f"{reason}，展示最近交易日 {review_day:%Y-%m-%d}"
    headline = _headline(
        broker_status,
        broker_bought,
        broker_closed,
        len(broker_orders),
        local_file_state,
        global_buy_window_best,
        global_all_day_closest,
        required_drop,
    )

    chart_name = f"watch_code_daily_kline_{signal_day:%Y-%m-%d}.html"
    chart_path = output_dir / "watchlist_charts" / chart_name
    current_positions = len([row for row in broker.get("positions", []) if abs(float(row.get("qty") or 0)) > 1e-12])
    result = {
        "schema_version": SCHEMA_VERSION,
        "requested_date": requested.isoformat(),
        "review_date": review_day.isoformat(),
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "market_day": {
            "is_trading_day": offline_trading_day_decision(review_day).is_trading_day,
            "requested_is_trading_day": trading.is_trading_day,
            "is_fallback": fallback,
            "banner": market_banner,
            "previous_date": previous_value.isoformat() if previous_value else None,
            "next_date": next_value.isoformat() if next_value else None,
        },
        "headline": headline,
        "quality": {
            "status": quality_status,
            "broker_status": broker_status,
            "warnings": attention,
        },
        "summary": {
            "watch_counts": {"premarket": len(premarket_symbols), "intraday": len(candidate_symbols), "afterhours": len(afterhours_symbols)},
            "rounds": parsed.phase_round_counts,
            "broker_order_count": len(broker_orders),
            "broker_bought_symbols": broker_bought,
            "broker_closed_symbols": broker_closed,
            "broker_unfilled_buy_symbols": broker_unfilled,
            "current_positions": current_positions,
            "local_order_file_state": local_file_state,
            "local_order_count": len(local_orders) if local_file_state != "missing" else None,
            "excluded_count": excluded_count,
            "net_cash_flow": net_cash_value,
            "premarket_alert_count": parsed.premarket_notification_count,
            "premarket_alert_symbols": list(parsed.premarket_notification_symbols),
            "open_buy_pause_rounds": parsed.open_buy_pause_rounds,
        },
        "strategy": {
            "name": strategy_config.get("STRATEGY_NAME", "MA5"),
            "buy_window": "09:30-12:00 ET",
            "max_daily_buys": strategy_config.get("BUY_STOCK_COUNT"),
            "buy_notional_usd": strategy_config.get("BUY_NOTIONAL_USD"),
            "required_drop_pct": required_drop,
            "buy_trigger_distance_pct": strategy_config.get("MA5_BUY_TRIGGER_DISTANCE_PCT"),
            "buy_window_best": _snapshot(global_buy_window_best, required_drop),
            "all_day_closest": _snapshot(global_all_day_closest, required_drop),
        },
        "phases": phases,
        "funnel": {
            "observed": len(candidate_symbols),
            "window_near": 1 if global_buy_window_best else 0,
            "local_submitted": len([row for row in local_orders if str(row.get("side", "")).upper() == "BUY"]),
            "local_filled": len([row for row in local_orders if str(row.get("side", "")).upper() == "BUY" and _status_has_execution(row.get("status"), row.get("quantity"))]),
            "excluded": excluded_count,
        },
        "reason_distribution": [
            {"code": _reason_slug(label), "label": label, "count": count, "total": len(candidate_symbols)}
            for label, count in reason_counter.most_common()
        ],
        "attention": attention,
        "symbols": sorted(symbols.values(), key=_symbol_sort_key),
        "orders": sorted(all_orders, key=lambda row: str(row.get("submitted_at") or row.get("created_at") or ""), reverse=True),
        "position_events": position_events,
        "timeline": timeline,
        "sources": source_manifest,
        "chart_url": f"/charts/{chart_name}" if chart_path.exists() else None,
        "broker": broker,
    }
    return result


def parse_monitor_log(path: Path) -> ParsedMonitorLog:
    if not path.exists():
        return ParsedMonitorLog((), (), {"premarket": 0, "intraday": 0, "afterhours": 0}, {}, 0, (), 0, 0, 0)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _LOG_CACHE_LOCK:
        cached = _LOG_CACHE.get(key)
        if cached is not None:
            return cached

    observations: list[Observation] = []
    completed_rounds: list[ParsedRound] = []
    phase_markers: dict[str, set[str]] = {"premarket": set(), "intraday": set(), "afterhours": set()}
    phase_times: dict[str, list[datetime]] = defaultdict(list)
    current_phase = ""
    current_time: datetime | None = None
    table_phase = ""
    table_fields: tuple[str, ...] = ()
    table_rows: list[Observation] = []
    table_start_line = 0
    notify_count = 0
    notify_symbols: set[str] = set()
    open_pause_round_ids: set[str] = set()
    open_pause_rows = 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    def finish_table(complete: bool) -> None:
        nonlocal table_phase, table_fields, table_rows, table_start_line
        if table_phase and table_rows:
            completed_rounds.append(
                ParsedRound(table_phase, table_rows[0].observed_at, tuple(table_rows), complete, table_start_line)
            )
        table_phase = ""
        table_fields = ()
        table_rows = []
        table_start_line = 0

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        bracket = _BRACKET_TIME_RE.match(line)
        if bracket:
            current_time = _parse_et(bracket.group(1))
            message = bracket.group(2)
            if "盘前" in message:
                current_phase = "premarket"
            elif "开始检查" in message:
                current_phase = "intraday"
            elif "盘后" in message:
                current_phase = "afterhours"
            if current_phase and current_time:
                marker = f"{current_phase}:{current_time.isoformat()}"
                if (current_phase == "intraday" and "开始检查" in message) or (
                    current_phase == "premarket" and ("推荐检查" in message or "盘前" in message)
                ):
                    phase_markers[current_phase].add(marker)
                    phase_times[current_phase].append(current_time)
        if "盘后提醒信号监控" in line or "盘后 high/low" in line:
            current_phase = "afterhours"
        elif "盘前 MA5" in line and "表" not in line:
            current_phase = "premarket"
        elif "盘中 MA5" in line or "盘中时段" in line:
            current_phase = "intraday"
        run_match = _RUN_TIME_RE.search(line)
        if run_match:
            current_time = _parse_et(run_match.group(1))
            if current_phase and current_time:
                marker = f"{current_phase}:{current_time.isoformat()}"
                phase_markers[current_phase].add(marker)
                phase_times[current_phase].append(current_time)

        if "Trade notify" in line and "premarket MA5 recommendation" in line and "sent" in line:
            notify_count += 1
            notify_match = _NOTIFY_SYMBOL_RE.search(line)
            if notify_match:
                notify_symbols.add(normalize_symbol(notify_match.group(1)))

        detected = _detect_table_header(line)
        if detected:
            finish_table(False)
            table_phase, table_fields = detected
            table_start_line = line_number
            continue
        if table_phase:
            if "本轮完成" in line or "信号触发" in line:
                finish_table(True)
                continue
            if "|" not in raw_line:
                if line and not set(line) <= {"-", "+", "=", " "}:
                    finish_table(False)
                continue
            cells = [cell.strip() for cell in raw_line.strip().strip("|").split("|")]
            if not cells or _separator_cells(cells) or len(cells) < len(table_fields):
                continue
            values = {field: cells[index] for index, field in enumerate(table_fields)}
            symbol = normalize_symbol(values.get("symbol", ""))
            if not symbol:
                continue
            values["symbol"] = symbol
            round_id = f"{table_phase}:{current_time.isoformat() if current_time else 'unknown'}"
            observation = Observation(table_phase, current_time, line_number, round_id, values)
            observations.append(observation)
            table_rows.append(observation)
            if table_phase == "intraday" and "开放买单" in values.get("reason", ""):
                open_pause_round_ids.add(round_id)
                open_pause_rows += 1
    finish_table(False)

    complete_by_phase: dict[str, list[datetime]] = defaultdict(list)
    for round_item in completed_rounds:
        if round_item.complete and round_item.observed_at:
            complete_by_phase[round_item.phase].append(round_item.observed_at)
    ranges = {
        phase: (min(values), max(values))
        for phase, values in complete_by_phase.items()
        if values
    }
    result = ParsedMonitorLog(
        tuple(observations),
        tuple(completed_rounds),
        {
            phase: sum(1 for item in completed_rounds if item.phase == phase and item.complete)
            for phase in ("premarket", "intraday", "afterhours")
        },
        ranges,
        notify_count,
        tuple(sorted(notify_symbols)),
        len(open_pause_round_ids),
        open_pause_rows,
        len(lines),
    )
    with _LOG_CACHE_LOCK:
        _LOG_CACHE.clear()
        _LOG_CACHE[key] = result
    return result


def load_broker_snapshot(review_day: date, *, ttl_seconds: float = 60.0) -> dict[str, Any]:
    import time as monotonic_time

    cache_key = review_day.isoformat()
    now = monotonic_time.monotonic()
    with _BROKER_CACHE_LOCK:
        cached = _BROKER_CACHE.get(cache_key)
        if cached and now - cached[0] <= ttl_seconds:
            return cached[1]

    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    from .alpaca_connection import build_trading_connection

    connection = build_trading_connection()
    start = datetime.combine(review_day, time.min, tzinfo=ET)
    end = start + timedelta(days=1)
    raw_orders = connection.client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, after=start, until=end, nested=True)
    )
    orders = [_normalize_broker_order(item) for item in raw_orders]
    positions = [_normalize_broker_position(item) for item in connection.client.get_all_positions()]
    payload = {
        "orders": orders,
        "broker": {
            "mode": "paper" if connection.paper else "live",
            "status": "verified",
            "synced_at": datetime.now(ET).isoformat(timespec="seconds"),
            "positions": positions,
            "error": None,
        },
    }
    with _BROKER_CACHE_LOCK:
        _BROKER_CACHE[cache_key] = (now, payload)
    return payload


def evidence_context(
    review_date: str | date,
    source_id: str,
    line: int,
    *,
    radius: int = 3,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(base_dir or PROJECT_ROOT).resolve()
    day = _coerce_date(review_date)
    signal_day = previous_trading_day(day)
    output_dir = root / "outputs"
    allowed = {
        "monitor_auto": output_dir / "logs" / f"monitor_auto_{day:%Y%m%d}.out.log",
        "monitor_error": output_dir / "logs" / f"monitor_auto_{day:%Y%m%d}.err.log",
        "local_orders": output_dir / f"orders_{day:%Y-%m-%d}.csv",
        "buy_exclusions": output_dir / f"buy_exclusions_{day:%Y-%m-%d}.csv",
        "intraday_candidates": output_dir / f"watch_candidates_{signal_day:%Y-%m-%d}.csv",
        "premarket_candidates": output_dir / f"premarket_watch_candidates_{signal_day:%Y-%m-%d}.csv",
        "afterhours_candidates": output_dir / f"afterhours_candidates_{day:%Y-%m-%d}.csv",
    }
    path = allowed.get(source_id)
    if path is None or not path.exists():
        raise FileNotFoundError(source_id)
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    if line < 1 or line > len(lines):
        raise ValueError("line is outside the evidence file")
    radius = max(0, min(int(radius), 20))
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    return {
        "source_id": source_id,
        "file": path.name,
        "line": line,
        "start_line": start,
        "end_line": end,
        "lines": [{"line": index, "text": lines[index - 1]} for index in range(start, end + 1)],
    }


def previous_trading_day(value: date) -> date:
    candidate = value - timedelta(days=1)
    while not offline_trading_day_decision(candidate).is_trading_day:
        candidate -= timedelta(days=1)
    return candidate


def normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text if text.startswith("US.") else f"US.{text}"


def _resolve_review_date(requested: date, available: list[date]) -> tuple[date, bool]:
    if requested in available:
        return requested, False
    earlier = [value for value in available if value <= requested]
    if earlier:
        return max(earlier), True
    if available:
        return max(available), True
    return requested, False


def _neighbor_dates(review_day: date, values: list[date]) -> tuple[date | None, date | None]:
    ordered = sorted(values)
    earlier = [value for value in ordered if value < review_day]
    later = [value for value in ordered if value > review_day]
    return (max(earlier) if earlier else None, min(later) if later else None)


def _coerce_date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeError):
        return []


def _load_local_orders(path: Path) -> tuple[list[dict[str, str]], str]:
    if not path.exists():
        return [], "missing"
    rows = _read_csv(path)
    return rows, "present" if rows else "empty"


def _candidate_symbols(rows: list[dict[str, str]], parsed: ParsedMonitorLog, phase: str = "intraday") -> list[str]:
    values: list[str] = []
    for row in rows:
        symbol = normalize_symbol(row.get("symbol", ""))
        if symbol and symbol not in values:
            values.append(symbol)
    if values:
        return values
    for item in parsed.observations:
        if item.phase != phase:
            continue
        symbol = normalize_symbol(item.values.get("symbol", ""))
        action = item.values.get("action", "")
        if phase == "intraday" and action == "持有":
            continue
        if symbol and symbol not in values:
            values.append(symbol)
    return values


def _detect_table_header(line: str) -> tuple[str, tuple[str, ...]] | None:
    if "|" not in line:
        return None
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    joined = "|".join(cells)
    if all(token in joined for token in ("代码", "动作", "当前价", "买/卖点", "原因")):
        return "intraday", INTRADAY_FIELDS
    if all(token in joined for token in ("代码", "状态", "价格来源", "MA5距离", "盘前涨跌幅")):
        return "premarket", PREMARKET_FIELDS
    if all(token in joined for token in ("股票", "当前价", "提醒线", "参考价", "状态")):
        return "afterhours", AFTERHOURS_FIELDS
    return None


def _separator_cells(cells: Iterable[str]) -> bool:
    meaningful = [cell.strip() for cell in cells if cell.strip()]
    return bool(meaningful) and all(not (set(cell) - {"-", "+", " "}) for cell in meaningful)


def _parse_et(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    except ValueError:
        return None


def _number(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "").replace("$", "")
    if not text or text in {"-", "未知", "None", "null"}:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _percent(value: object) -> float | None:
    number = _number(value)
    return number / 100.0 if number is not None else None


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _observation_sort_key(item: Observation) -> tuple[datetime, int]:
    return (item.observed_at or datetime.min.replace(tzinfo=ET), item.source_line)


def _in_buy_window(observed_at: datetime | None) -> bool:
    return bool(observed_at and BUY_WINDOW_START <= observed_at.timetz().replace(tzinfo=None) < BUY_WINDOW_END)


def _best_observation(items: Iterable[Observation], required_drop: float, *, window_only: bool) -> Observation | None:
    best: tuple[tuple[float, float, float, float], Observation] | None = None
    for item in items:
        if window_only and not _in_buy_window(item.observed_at):
            continue
        action = item.values.get("action", "")
        if action in {"持有", "卖出", "卖出未成", "跳过卖出", "错误"}:
            continue
        current_gain = _percent(item.values.get("current_gain_pct"))
        current_price = _number(item.values.get("current_price"))
        if current_gain is None or current_price is None:
            continue
        reason = item.values.get("reason", "")
        required = _required_drop(reason) or required_drop
        drop_gap = max(0.0, required + current_gain)
        trigger = _trigger_upper(reason)
        price_gap = max(0.0, current_price / trigger - 1.0) if trigger and trigger > 0 else 0.0
        unmet = float(drop_gap > 1e-9) + float(price_gap > 1e-9)
        excluded_penalty = 0.05 if "今日排除" in reason or action == "排除" else 0.0
        score = (unmet, drop_gap, price_gap, excluded_penalty)
        # 同分时保留第一次达到该距离的观测，明确回答“何时首次最接近”。
        if best is None or score < best[0]:
            best = (score, item)
    return best[1] if best else None


def _required_drop(reason: str) -> float | None:
    match = _REQUIRED_DROP_RE.search(reason or "")
    return float(match.group(1)) / 100.0 if match else None


def _trigger_upper(reason: str) -> float | None:
    match = _TRIGGER_RE.search(reason or "")
    return float(match.group(1)) if match else None


def _snapshot(item: Observation | None, required_drop: float) -> dict[str, Any] | None:
    if item is None:
        return None
    values = item.values
    reason = values.get("reason", "")
    current_gain = _percent(values.get("current_gain_pct"))
    required = _required_drop(reason) or required_drop
    current_price = _number(values.get("current_price"))
    trigger = _trigger_upper(reason)
    return {
        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
        "source_line": item.source_line,
        "phase": item.phase,
        "action": values.get("action") or values.get("status"),
        "current_price": current_price,
        "today_open": _number(values.get("today_open")),
        "ma5": _number(values.get("ma5")),
        "open_ma5": _number(values.get("open_ma5")),
        "signal_gain_pct": _percent(values.get("signal_gain_pct")),
        "current_gain_pct": current_gain,
        "decision_price": _number(values.get("decision_price")),
        "trigger_upper": trigger,
        "required_drop_pct": required,
        "drop_gap_pct": max(0.0, required + current_gain) if current_gain is not None else None,
        "price_gap_pct": max(0.0, current_price / trigger - 1.0) if current_price and trigger else None,
        "reason": reason,
        "actionable": bool(_in_buy_window(item.observed_at) and "今日排除" not in reason and values.get("action") != "排除"),
        "evidence": {"source_id": "monitor_auto", "line": item.source_line},
    }


def _strategy_reason(item: Observation | None, excluded: bool) -> tuple[str, str]:
    if excluded:
        return "today_excluded", "今日规则排除"
    reason = item.values.get("reason", "") if item else ""
    if "跌幅未到" in reason or "进入买点区间" in reason:
        return "drop_not_reached", "跌幅未达门槛"
    if "高于触发上沿" in reason:
        return "price_above_trigger", "价格未进入买点区间"
    if "开放买单" in reason:
        return "blocked_open_order", "开放买单暂停"
    if "买入次数" in reason or "名额" in reason:
        return "blocked_limit", "买入名额限制"
    if "错误" in reason or "失败" in reason:
        return "data_or_order_error", "行情或订单异常"
    if not item:
        return "no_observation", "缺少有效行情"
    return "condition_not_met", "其他条件未满足"


def _strategy_reason_text(item: Observation | None, exclusion: dict[str, str] | None) -> str:
    if exclusion:
        return exclusion.get("reason") or "今日已被策略排除。"
    if item:
        return item.values.get("reason") or "未达到买入条件。"
    return "没有解析到该股票的有效盘中观察记录。"


def _reason_slug(value: str) -> str:
    mapping = {
        "今日规则排除": "today_excluded",
        "跌幅未达门槛": "drop_not_reached",
        "价格未进入买点区间": "price_above_trigger",
        "开放买单暂停": "blocked_open_order",
        "买入名额限制": "blocked_limit",
        "行情或订单异常": "data_or_order_error",
        "缺少有效行情": "no_observation",
        "其他条件未满足": "condition_not_met",
    }
    return mapping.get(value, "other")


def _position_events(rounds: tuple[ParsedRound, ...], candidate_symbols: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    candidate_set = set(candidate_symbols)
    intraday = [item for item in rounds if item.phase == "intraday" and item.complete and item.observed_at]
    events: list[dict[str, Any]] = []
    previous: set[str] | None = None
    pending_removal: dict[str, dict[str, Any]] = {}
    for index, round_item in enumerate(intraday):
        holdings = {
            normalize_symbol(row.values.get("symbol", ""))
            for row in round_item.rows
            if row.values.get("action") == "持有" and normalize_symbol(row.values.get("symbol", "")) not in candidate_set
        }
        if previous is None:
            for symbol in sorted(holdings):
                events.append(_position_event(symbol, "existing_at_open", round_item, "high"))
            previous = holdings
            continue
        for symbol in sorted(holdings - previous):
            events.append(_position_event(symbol, "added_observed", round_item, "high"))
            pending_removal.pop(symbol, None)
        for symbol in sorted(previous - holdings):
            event = _position_event(symbol, "removed_observed", round_item, "low")
            events.append(event)
            pending_removal[symbol] = event
        for symbol in list(pending_removal):
            if symbol not in holdings and symbol not in previous:
                pending_removal[symbol]["confidence"] = "high"
                pending_removal.pop(symbol, None)
        previous = holdings
    return events, sorted(previous or set())


def _position_event(symbol: str, event_type: str, round_item: ParsedRound, confidence: str) -> dict[str, Any]:
    return {
        "id": f"position:{event_type}:{symbol}:{round_item.observed_at.isoformat() if round_item.observed_at else round_item.source_line}",
        "symbol": symbol,
        "event_type": event_type,
        "label": {
            "existing_at_open": "开盘时已观察到持仓",
            "added_observed": "监控首次观察到持仓",
            "removed_observed": "监控观察到持仓移除",
        }[event_type],
        "occurred_at": round_item.observed_at.isoformat() if round_item.observed_at else None,
        "source": "monitor_auto",
        "source_line": round_item.source_line,
        "confidence": confidence,
    }


def _normalize_broker_order(raw: Any) -> dict[str, Any]:
    side = _enum_text(getattr(raw, "side", ""))
    status = _enum_text(getattr(raw, "status", ""))
    filled_qty = _decimal(getattr(raw, "filled_qty", None)) or Decimal(0)
    filled_avg = _decimal(getattr(raw, "filled_avg_price", None))
    return {
        "source": "alpaca",
        "order_id": str(getattr(raw, "id", "") or ""),
        "client_order_id": str(getattr(raw, "client_order_id", "") or ""),
        "symbol": normalize_symbol(getattr(raw, "symbol", "")),
        "ticker": str(getattr(raw, "symbol", "") or "").upper(),
        "side": side,
        "order_type": _enum_text(getattr(raw, "type", "")),
        "time_in_force": _enum_text(getattr(raw, "time_in_force", "")),
        "status": status,
        "qty": _float_or_none(getattr(raw, "qty", None)),
        "filled_qty": float(filled_qty),
        "filled_avg_price": float(filled_avg) if filled_avg is not None else None,
        "filled_value": float(filled_qty * filled_avg) if filled_avg is not None else 0.0,
        "limit_price": _float_or_none(getattr(raw, "limit_price", None)),
        "submitted_at": _iso(getattr(raw, "submitted_at", None)),
        "filled_at": _iso(getattr(raw, "filled_at", None)),
        "canceled_at": _iso(getattr(raw, "canceled_at", None)),
        "expired_at": _iso(getattr(raw, "expired_at", None)),
        "failed_at": _iso(getattr(raw, "failed_at", None)),
        "origin": "unattributed",
        "is_simulated": False,
    }


def _normalize_broker_position(raw: Any) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(getattr(raw, "symbol", "")),
        "ticker": str(getattr(raw, "symbol", "") or "").upper(),
        "qty": _float_or_none(getattr(raw, "qty", None)) or 0.0,
        "avg_entry_price": _float_or_none(getattr(raw, "avg_entry_price", None)),
        "current_price": _float_or_none(getattr(raw, "current_price", None)),
        "market_value": _float_or_none(getattr(raw, "market_value", None)),
        "unrealized_pl": _float_or_none(getattr(raw, "unrealized_pl", None)),
        "unrealized_plpc": _float_or_none(getattr(raw, "unrealized_plpc", None)),
    }


def _merge_broker_symbols(
    symbols: dict[str, dict[str, Any]],
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    local_orders: list[dict[str, str]],
    local_file_state: str,
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        grouped[normalize_symbol(order.get("symbol", ""))].append(order)
    position_map = {normalize_symbol(row.get("symbol", "")): row for row in positions}
    local_ids = {str(row.get("order_id", "")) for row in local_orders if row.get("order_id")}
    # 当前持仓是查询时点快照，不是复盘日收盘快照。它只作为详情上下文，
    # 不能把复盘日之后重新建的仓位混入历史日期，也不能改变当日买卖结论。
    for symbol in sorted(grouped):
        symbol_orders = sorted(grouped.get(symbol, []), key=lambda row: str(row.get("submitted_at") or ""))
        buy_fills = [row for row in symbol_orders if row.get("side") == "BUY" and float(row.get("filled_qty") or 0) > 0]
        sell_fills = [row for row in symbol_orders if row.get("side") == "SELL" and float(row.get("filled_qty") or 0) > 0]
        buy_qty, buy_value = _fill_totals(buy_fills)
        sell_qty, sell_value = _fill_totals(sell_fills)
        position_qty = float(position_map.get(symbol, {}).get("qty") or 0)
        buy_attempts = [row for row in symbol_orders if row.get("side") == "BUY"]
        if buy_qty > 0 and sell_qty >= buy_qty - 1e-9:
            bucket, label, severity = "broker_closed", "券商已买已卖", "success"
        elif buy_qty > 0:
            bucket, label, severity = "broker_bought", "券商已买入", "warning"
        elif buy_attempts:
            bucket, label, severity = "buy_unfilled", "买入未成", "warning"
        else:
            bucket, label, severity = "broker_activity", "券商活动", "neutral"
        broker_ids = {str(order.get("order_id")) for order in symbol_orders if order.get("order_id")}
        matched_ids = broker_ids & local_ids
        if local_file_state == "missing":
            ledger_match = "missing"
        elif broker_ids and matched_ids == broker_ids:
            ledger_match = "matched"
        elif matched_ids:
            ledger_match = "partial"
        else:
            ledger_match = "unmatched"
        existing = symbols.get(symbol, {})
        existing.update(
            {
                "symbol": symbol,
                "ticker": symbol.removeprefix("US."),
                "source_labels": sorted(set(existing.get("source_labels", [])) | {"Alpaca"}),
                "bucket": bucket,
                "status_label": label,
                # 成交状态与账本一致性是两个独立维度：主状态保持成交/未成
                # 的语义色，冲突由“记录一致性”列和首屏告警表达。
                "severity": severity,
                "reason_code": "broker_local_mismatch" if ledger_match != "matched" else bucket,
                "reason": (
                    f"Alpaca 有成交记录，但本地订单账本仅匹配 {len(matched_ids)}/{len(broker_ids)} 笔券商订单；其余来源尚未归因。"
                    if ledger_match == "partial" and (buy_qty > 0 or sell_qty > 0)
                    else "Alpaca 有成交记录，但本地订单账本没有匹配记录；交易来源尚未归因。"
                    if ledger_match != "matched" and (buy_qty > 0 or sell_qty > 0)
                    else (
                        "买入尝试未成交；本地订单账本没有匹配记录，来源尚未归因。"
                        if ledger_match != "matched" and buy_attempts
                        else "券商订单已与本地账本核对。"
                    )
                ),
                "buy_window_best": existing.get("buy_window_best"),
                "all_day_closest": existing.get("all_day_closest"),
                "latest": existing.get("latest"),
                "latest_priced": existing.get("latest_priced"),
                "orders": symbol_orders,
                "position_events": existing.get("position_events", []),
                "buy_filled_qty": buy_qty,
                "buy_avg_price": buy_value / buy_qty if buy_qty else None,
                "sell_filled_qty": sell_qty,
                "sell_avg_price": sell_value / sell_qty if sell_qty else None,
                "net_cash_flow": sell_value - buy_value if buy_qty or sell_qty else None,
                "current_position_qty": position_qty,
                "local_ledger_match": ledger_match,
                "local_ledger_matched_order_count": len(matched_ids),
                "broker_order_id_count": len(broker_ids),
                "evidence": existing.get("evidence", []),
            }
        )
        symbols[symbol] = existing

    # 让“当前持仓”指标与可筛选股票行保持一致，同时明确它只是查询时点上下文。
    # 没有复盘日订单的当前仓位不能被归因为当日买入；若该股本就在策略表中，
    # 仅补充当前数量，不覆盖它原本的当日策略结论。
    for symbol, position in sorted(position_map.items()):
        position_qty = float(position.get("qty") or 0)
        if abs(position_qty) < 1e-12 or symbol in grouped:
            continue
        existing = symbols.get(symbol)
        if existing is not None:
            existing["source_labels"] = sorted(set(existing.get("source_labels", [])) | {"Alpaca 当前持仓"})
            existing["current_position_qty"] = position_qty
            continue
        symbols[symbol] = {
            "symbol": symbol,
            "ticker": symbol.removeprefix("US."),
            "source_labels": ["Alpaca 当前持仓"],
            "bucket": "current_position_context",
            "status_label": "当前持仓（非当日活动）",
            "severity": "neutral",
            "reason_code": "current_position_context",
            "reason": "券商当前持仓快照中存在该股票，但复盘日没有对应订单；该仓位不归入当日买入或卖出。",
            "buy_window_best": None,
            "all_day_closest": None,
            "latest": None,
            "latest_priced": None,
            "orders": [],
            "position_events": [],
            "buy_filled_qty": 0.0,
            "buy_avg_price": None,
            "sell_filled_qty": 0.0,
            "sell_avg_price": None,
            "net_cash_flow": None,
            "current_position_qty": position_qty,
            "local_ledger_match": "not_applicable",
            "local_ledger_matched_order_count": 0,
            "broker_order_id_count": 0,
            "evidence": [],
        }


def _fill_totals(rows: list[dict[str, Any]]) -> tuple[float, float]:
    qty = sum(float(row.get("filled_qty") or 0) for row in rows)
    value = sum(float(row.get("filled_value") or 0) for row in rows)
    return qty, value


def _broker_symbol_groups(orders: list[dict[str, Any]]) -> dict[str, set[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        grouped[normalize_symbol(order.get("symbol", ""))].append(order)
    bought: set[str] = set()
    closed: set[str] = set()
    unfilled: set[str] = set()
    for symbol, rows in grouped.items():
        buy_qty = sum(float(row.get("filled_qty") or 0) for row in rows if row.get("side") == "BUY")
        sell_qty = sum(float(row.get("filled_qty") or 0) for row in rows if row.get("side") == "SELL")
        buy_attempts = any(row.get("side") == "BUY" for row in rows)
        if buy_qty > 0:
            bought.add(symbol)
            # “已买已卖”是复盘日内订单流的结论；当前持仓可能来自之后的交易。
            if sell_qty >= buy_qty - 1e-9:
                closed.add(symbol)
        elif buy_attempts:
            unfilled.add(symbol)
    return {"bought": bought, "closed": closed, "unfilled_buy": unfilled}


def _normalized_local_orders(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "source": "local",
                "order_id": row.get("order_id", ""),
                "symbol": normalize_symbol(row.get("symbol", "")),
                "ticker": normalize_symbol(row.get("symbol", "")).removeprefix("US."),
                "side": str(row.get("side", "")).upper(),
                "order_type": "",
                "status": str(row.get("status", "")).upper(),
                "qty": _float_or_none(row.get("quantity")),
                "filled_qty": _float_or_none(row.get("quantity")) if _status_has_execution(row.get("status"), row.get("quantity")) else 0.0,
                "filled_avg_price": None,
                "filled_value": 0.0,
                "limit_price": _float_or_none(row.get("price")),
                "submitted_at": row.get("created_at"),
                "created_at": row.get("created_at"),
                "reason": row.get("reason", ""),
                "message": row.get("message", ""),
                "origin": "local_ledger",
                "is_simulated": str(row.get("status", "")).upper() == "DRY_RUN",
            }
        )
    return normalized


def _status_has_execution(status: object, qty: object = None) -> bool:
    text = str(status or "").upper()
    return text == "FILLED" or text.startswith("PARTIALLY_FILLED")


def _build_attention(
    *,
    local_file_state: str,
    local_orders: list[dict[str, str]],
    broker: dict[str, Any],
    broker_orders: list[dict[str, Any]],
    parsed: ParsedMonitorLog,
    position_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if broker.get("status") == "verified" and broker_orders and local_file_state in {"missing", "empty"}:
        filled_symbols = sorted(
            {
                order["symbol"]
                for order in broker_orders
                if float(order.get("filled_qty") or 0) > 0
            }
        )
        items.append(
            {
                "code": "BROKER_ACTIVITY_NOT_IN_LOCAL_LEDGER",
                "severity": "critical",
                "title": "本地记录与券商数据严重不一致",
                "message": f"本地订单账本{('缺失' if local_file_state == 'missing' else '为空')}，但 Alpaca 返回 {len(broker_orders)} 笔订单；{len(filled_symbols)} 只股票有成交，来源尚未归因。",
                "facts": {"local_file_state": local_file_state, "broker_order_count": len(broker_orders), "filled_symbols": filled_symbols},
                "action_label": "查看券商订单",
            }
        )
    elif broker.get("status") == "unavailable":
        items.append(
            {
                "code": "BROKER_SNAPSHOT_UNAVAILABLE",
                "severity": "warning",
                "title": "券商核对暂不可用",
                "message": broker.get("error") or "无法读取 Alpaca 订单与持仓，只显示本地证据。",
                "facts": {},
            }
        )
    if parsed.open_buy_pause_rounds and not local_orders:
        items.append(
            {
                "code": "OPEN_BUY_ORDER_WITHOUT_LOCAL_LEDGER",
                "severity": "critical",
                "title": "检测到未落盘的开放买单",
                "message": f"监控日志有 {parsed.open_buy_pause_rounds} 轮因 Alpaca 开放买单暂停，但本地账本没有对应订单。",
                "facts": {"pause_rounds": parsed.open_buy_pause_rounds, "affected_rows": parsed.open_buy_pause_rows},
            }
        )
    unmatched = [event for event in position_events if event["event_type"] in {"added_observed", "removed_observed"}]
    if unmatched and not local_orders:
        items.append(
            {
                "code": "UNMATCHED_POSITION_CHANGES",
                "severity": "critical",
                "title": "持仓增减缺少本地订单证据",
                "message": "监控日志观察到持仓增减；不能从现有本地文件确认下单来源、数量或成交价。",
                "facts": {"events": unmatched},
            }
        )
    return items


def _build_timeline(
    review_day: date,
    parsed: ParsedMonitorLog,
    exclusions: list[dict[str, str]],
    broker_orders: list[dict[str, Any]],
    position_events: list[dict[str, Any]],
    window_best: Observation | None,
    all_day_best: Observation | None,
    required_drop: float,
    log_path: Path,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    phase_labels = {"premarket": "盘前监控", "intraday": "盘中策略", "afterhours": "盘后提醒"}
    for phase, (start, end) in parsed.phase_ranges.items():
        if start:
            events.append(_timeline_event(start, "phase_start", phase_labels.get(phase, phase) + "开始", "monitor_auto", "neutral"))
        if end:
            events.append(_timeline_event(end, "phase_end", phase_labels.get(phase, phase) + "结束", "monitor_auto", "neutral"))
    for row in exclusions:
        when = _parse_iso_datetime(row.get("created_at"))
        symbol = normalize_symbol(row.get("symbol", ""))
        events.append(
            _timeline_event(when, "strategy_excluded", f"{symbol.removeprefix('US.')} 今日排除", "buy_exclusions", "critical", symbol=symbol, detail=row.get("reason", ""))
        )
    for order in broker_orders:
        if float(order.get("filled_qty") or 0) <= 0 and order.get("status") not in {"CANCELED", "REJECTED", "EXPIRED"}:
            continue
        when = _parse_iso_datetime(order.get("filled_at") or order.get("canceled_at") or order.get("submitted_at"))
        side_label = "买入" if order.get("side") == "BUY" else "卖出"
        status_label = "成交" if float(order.get("filled_qty") or 0) > 0 else order.get("status", "")
        symbol = normalize_symbol(order.get("symbol", ""))
        events.append(
            _timeline_event(
                when,
                "broker_order",
                f"{symbol.removeprefix('US.')} {side_label}{status_label}",
                "alpaca",
                "success" if float(order.get("filled_qty") or 0) > 0 else "warning",
                symbol=symbol,
                detail=f"{order.get('filled_qty') or order.get('qty') or 0:g} 股 @ {order.get('filled_avg_price') or order.get('limit_price') or '-'}",
                order_id=order.get("order_id"),
            )
        )
    for event in position_events:
        when = _parse_iso_datetime(event.get("occurred_at"))
        events.append(_timeline_event(when, "position", f"{event['symbol'].removeprefix('US.')} {event['label']}", "monitor_auto", "warning", symbol=event["symbol"], source_line=event.get("source_line")))
    if window_best:
        symbol = normalize_symbol(window_best.values.get("symbol", ""))
        snap = _snapshot(window_best, required_drop)
        events.append(_timeline_event(window_best.observed_at, "window_best", f"{symbol.removeprefix('US.')} 买入窗口内最接近", "monitor_auto", "warning", symbol=symbol, detail=_snapshot_gap_text(snap), source_line=window_best.source_line))
    if all_day_best and (not window_best or all_day_best.source_line != window_best.source_line):
        symbol = normalize_symbol(all_day_best.values.get("symbol", ""))
        snap = _snapshot(all_day_best, required_drop)
        events.append(_timeline_event(all_day_best.observed_at, "all_day_closest", f"{symbol.removeprefix('US.')} 全天最接近", "monitor_auto", "warning", symbol=symbol, detail=("已过买入窗口；" if not _in_buy_window(all_day_best.observed_at) else "") + _snapshot_gap_text(snap), source_line=all_day_best.source_line))
    events.append(_timeline_event(datetime.combine(review_day, time(20, 0), tzinfo=ET), "review_archived", "复盘归档", "monitor_auto", "neutral"))
    return sorted(events, key=lambda item: str(item.get("occurred_at") or ""))


def _timeline_event(
    when: datetime | None,
    event_type: str,
    title: str,
    source: str,
    severity: str,
    *,
    symbol: str | None = None,
    detail: str = "",
    source_line: int | None = None,
    order_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{event_type}:{symbol or ''}:{when.isoformat() if when else source_line or ''}:{order_id or ''}",
        "occurred_at": when.astimezone(ET).isoformat() if when and when.tzinfo else (when.replace(tzinfo=ET).isoformat() if when else None),
        "event_type": event_type,
        "title": title,
        "detail": detail,
        "source": source,
        "severity": severity,
        "symbol": symbol,
        "source_line": source_line,
        "order_id": order_id,
    }


def _phase_payloads(parsed: ParsedMonitorLog, premarket: int, intraday: int, afterhours: int) -> list[dict[str, Any]]:
    counts = {"premarket": premarket, "intraday": intraday, "afterhours": afterhours}
    labels = {"premarket": "盘前", "intraday": "盘中", "afterhours": "盘后"}
    modes = {"premarket": "只提醒", "intraday": "可下单", "afterhours": "只提醒"}
    result = []
    for phase in ("premarket", "intraday", "afterhours"):
        start, end = parsed.phase_ranges.get(phase, (None, None))
        result.append(
            {
                "phase": phase,
                "label": labels[phase],
                "mode": modes[phase],
                "symbol_count": counts[phase],
                "round_count": parsed.phase_round_counts.get(phase, 0),
                "start_at": start.isoformat() if start else None,
                "end_at": end.isoformat() if end else None,
                "coverage": f"{start:%H:%M}—{end:%H:%M} ET" if start and end else "无完整覆盖数据",
            }
        )
    return result


def _source_manifest(paths: dict[str, Path], parsed: ParsedMonitorLog, local_file_state: str) -> list[dict[str, Any]]:
    labels = {
        "monitor_auto": "自动监控日志",
        "monitor_error": "自动监控错误日志",
        "local_orders": "本地订单账本",
        "buy_exclusions": "今日买入排除",
        "intraday_candidates": "盘中候选快照",
        "premarket_candidates": "盘前候选快照",
        "afterhours_candidates": "盘后候选快照",
    }
    result = []
    for source_id, path in paths.items():
        exists = path.exists()
        stat = path.stat() if exists else None
        status = "missing" if not exists else ("empty" if stat and stat.st_size == 0 else "healthy")
        if source_id == "local_orders":
            status = local_file_state
        result.append(
            {
                "id": source_id,
                "label": labels[source_id],
                "file": path.name,
                "exists": exists,
                "bytes": stat.st_size if stat else 0,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, ET).isoformat(timespec="seconds") if stat else None,
                "status": status,
                "rows_parsed": len(parsed.observations) if source_id == "monitor_auto" else None,
                "line_count": parsed.line_count if source_id == "monitor_auto" else None,
                "timezone": "America/New_York" if source_id == "monitor_auto" else None,
            }
        )
    return result


def _headline(
    broker_status: str,
    bought: int,
    closed: int,
    broker_order_count: int,
    local_file_state: str,
    window_best: Observation | None,
    all_day_best: Observation | None,
    required_drop: float,
) -> dict[str, str]:
    if broker_status == "verified":
        title = f"Alpaca 显示 {bought} 只股票有买入成交，其中 {closed} 只已卖出；券商共 {broker_order_count} 笔订单。"
    elif broker_status == "unavailable":
        title = "券商核对暂不可用；当前仅显示本地监控与账本证据。"
    else:
        title = "本地复盘已加载，正在等待只读券商核对。"
    local_text = "本地订单记录缺失" if local_file_state == "missing" else ("本地订单记录为空" if local_file_state == "empty" else "本地订单记录已读取")
    details = [local_text]
    if window_best:
        snap = _snapshot(window_best, required_drop)
        details.append(f"买入窗口内最接近：{window_best.values.get('symbol', '').removeprefix('US.')} {window_best.observed_at:%H:%M}（{_snapshot_gap_text(snap)}）")
    if all_day_best and (not window_best or all_day_best.source_line != window_best.source_line):
        snap = _snapshot(all_day_best, required_drop)
        suffix = "，已过买入窗口" if not _in_buy_window(all_day_best.observed_at) else ""
        details.append(f"全天最接近：{all_day_best.values.get('symbol', '').removeprefix('US.')} {all_day_best.observed_at:%H:%M}{suffix}（{_snapshot_gap_text(snap)}）")
    return {"title": title, "detail": "；".join(details) + "。"}


def _snapshot_gap_text(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "无有效距离"
    current = snapshot.get("current_gain_pct")
    gap = snapshot.get("drop_gap_pct")
    if current is None:
        return "涨跌幅未知"
    text = f"当前涨跌 {current:.2%}"
    if gap is not None and gap > 0:
        text += f"，距跌幅门槛还差 {gap:.2%}"
    elif gap is not None:
        text += "，跌幅门槛已满足"
    return text


def _read_runtime_config(path: Path) -> dict[str, Any]:
    defaults = {
        "STRATEGY_NAME": "MA5_DIP_STRATEGY_NAME",
        "BUY_STOCK_COUNT": 2,
        "BUY_NOTIONAL_USD": 1500.0,
        "MA5_MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.12,
        "MA5_BUY_TRIGGER_DISTANCE_PCT": 0.03,
    }
    if not path.exists():
        return defaults
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return defaults
    wanted = set(defaults)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if name not in wanted:
            continue
        try:
            defaults[name] = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            if isinstance(node.value, ast.Name):
                defaults[name] = node.value.id
    return defaults


def _symbol_evidence(observations: list[Observation], log_path: Path) -> list[dict[str, Any]]:
    if not observations:
        return []
    selected = [observations[0], observations[-1]]
    return [
        {
            "source_id": "monitor_auto",
            "file": log_path.name,
            "line": item.source_line,
            "observed_at": item.observed_at.isoformat() if item.observed_at else None,
            "confidence": "confirmed",
        }
        for item in {entry.source_line: entry for entry in selected}.values()
    ]


def _symbol_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    priorities = {
        "broker_closed": 0,
        "broker_bought": 1,
        "buy_unfilled": 2,
        "position_unreconciled": 3,
        "excluded": 4,
        "window_outside_closest": 5,
        "not_bought": 6,
        "broker_activity": 7,
    }
    return priorities.get(str(item.get("bucket")), 99), str(item.get("symbol"))


def _parse_iso_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(ET) if parsed.tzinfo else parsed.replace(tzinfo=ET)


def _enum_text(value: object) -> str:
    text = getattr(value, "value", None) or str(value or "")
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return str(text).upper()


def _float_or_none(value: object) -> float | None:
    decimal_value = _decimal(value)
    return float(decimal_value) if decimal_value is not None else None


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(ET).isoformat()
    return str(value) if value else None
