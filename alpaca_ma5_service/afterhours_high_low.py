from __future__ import annotations

import csv
import json
import math
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .alpaca_connection import build_trading_connection, load_alpaca_credentials
from .config import Settings, build_settings
from .errors import short_error
from .models import OrderResult
from .order_guard import normalize_order_status, wait_for_fill_or_cancel
from .state import append_order, orders_file
from .watchlist import normalize_symbol, to_alpaca_symbol
from .watchlist_generator import batched, load_tradable_symbols


REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTERHOURS_DATA_CLOSE = time(20, 0)
RANGE_RATIO_THRESHOLD = 1.8
DROP_SIGNAL_THRESHOLD = 0.15
BUY_LIMIT_MULTIPLIER = 0.8
PROFIT_TARGET_MULTIPLIER = 1.1
LOG_WIDTH = 72
BUY_MONITOR_COLUMNS = [
    ("symbol", "股票", 9),
    ("current", "当前价", 8),
    ("source", "来源", 11),
    ("close", "收盘", 8),
    ("drop", "跌幅", 7),
    ("limit", "买入价", 8),
    ("qty", "数量", 9),
    ("status", "状态", 10),
    ("note", "说明", 28),
]
AFTERHOURS_ORDER_REASON_MARKER = "盘后 high/low"


def print_section(title: str) -> None:
    """打印盘后策略主标题，方便在 PyCharm console 里扫日志。"""
    print("", flush=True)
    print("=" * LOG_WIDTH, flush=True)
    print(title, flush=True)
    print("=" * LOG_WIDTH, flush=True)


def print_step(step: str, title: str) -> None:
    """打印一个清晰的步骤标题。"""
    print("", flush=True)
    print(f"[{step}] {title}", flush=True)


def print_detail(label: str, value: object) -> None:
    """打印一行缩进详情，避免长句堆在一行。"""
    print(f"  {label}: {value}", flush=True)


def print_warning(title: str, message: str) -> None:
    """打印可读性更好的提示/失败信息。"""
    print("", flush=True)
    print(f"[提示] {title}", flush=True)
    print_detail("说明", message)


def print_afterhours_header(
    now_et: datetime,
    signal_day: date,
    *,
    dry_run: bool,
    feed: str,
    range_ratio_threshold: float,
    drop_signal_threshold: float,
    buy_notional_usd: float,
    require_paper: bool,
) -> None:
    """打印盘后策略启动摘要。"""
    print_section("盘后 high/low 策略")
    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
    print_detail("交易日", signal_day)
    order_mode = "dry-run 复盘" if dry_run else "真实下单 (强制 Paper)" if require_paper else "真实下单"
    print_detail("运行模式", order_mode)
    print_detail("行情源", feed.upper())
    print_detail("筛选条件", f"常规盘 high/low > {range_ratio_threshold}")
    print_detail("下单信号", f"当前跌幅 > {drop_signal_threshold:.0%}")
    print_detail("单笔金额", f"${buy_notional_usd:,.2f}")
    print_detail("Paper 保护", "开启" if require_paper else "关闭")


def print_candidate_preview(candidates: list["AfterHoursCandidate"]) -> None:
    """打印候选股票预览，避免 5000 只扫描后的输出太乱。"""
    if not candidates:
        print_detail("候选预览", "无")
        return
    symbols = [normalize_symbol(candidate.symbol) for candidate in candidates]
    shown = ", ".join(symbols[:12])
    if len(symbols) > 12:
        shown = f"{shown} ... 另 {len(symbols) - 12} 只"
    print_detail("候选预览", shown)


def print_order_signal(symbol: str, title: str, details: list[tuple[str, object]]) -> None:
    """打印单只股票的实时信号检查结果。"""
    print("", flush=True)
    print(f"[{title}] {symbol}", flush=True)
    for label, value in details:
        print_detail(label, value)


def print_order_result(result: OrderResult) -> None:
    """打印盘后订单最终状态。"""
    print_detail("订单状态", result.status)
    print_detail("数量", f"{result.quantity:.6f}")
    print_detail("价格", f"{result.price:.4f}")
    if result.order_id:
        print_detail("订单号", result.order_id)
    if result.message:
        print_detail("结果", result.message)


def print_table_header(columns: list[tuple[str, str, int]]) -> None:
    """打印监控表头，让每轮候选股状态集中在一张表里。"""
    labels = [pad_table_cell(label, width) for _, label, width in columns]
    separator = "-+-".join("-" * width for _, _, width in columns)
    print("", flush=True)
    print("  " + " | ".join(labels), flush=True)
    print("  " + separator, flush=True)


def print_table_row(columns: list[tuple[str, str, int]], row: dict[str, object]) -> None:
    """打印一行监控表数据。"""
    values = [pad_table_cell(row.get(key, ""), width, truncate=key not in {"note", "reason"}) for key, _, width in columns]
    print("  " + " | ".join(values), flush=True)


def pad_table_cell(value: object, width: int, *, truncate: bool = True) -> str:
    """按显示宽度裁剪/补齐，避免中文列名把表格撑乱。"""
    text = shorten_table_text(str(value), width) if truncate else " ".join(str(value).split())
    return text + " " * max(width - display_width(text), 0)


def shorten_table_text(text: str, width: int) -> str:
    """把过长内容裁剪成适合表格的一格。"""
    if display_width(text) <= width:
        return text
    suffix = "..."
    target = max(width - display_width(suffix), 1)
    out = ""
    used = 0
    for char in text:
        char_width = display_width(char)
        if used + char_width > target:
            break
        out += char
        used += char_width
    return out + suffix


def display_width(text: str) -> int:
    """估算终端显示宽度；中文按 2 格处理。"""
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1 for char in text)


def short_price_source(source: str) -> str:
    """压缩价格来源，方便放进表格列。"""
    compact = (source or "未知").replace("moomoo_snapshot:", "moomoo:").replace("alpaca_latest_trade:", "alpaca:")
    return compact.replace("_price", "")


def format_price(value: float) -> str:
    """表格里的价格格式。"""
    return f"{value:.4f}" if value > 0 else "-"


def format_quantity(value: float) -> str:
    """表格里的数量格式。"""
    if value <= 0:
        return "-"
    if math.isclose(value, round(value), rel_tol=0, abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.6f}"


def order_status_label(status: str) -> str:
    """把订单状态转成更适合监控表的中文短标签。"""
    normalized = status.upper()
    if normalized == "NO_SIGNAL":
        return "等待信号"
    if normalized == "REJECTED":
        return "拒绝/跳过"
    if normalized in {"ALREADY_BOUGHT_TODAY", "EXISTING_POSITION"}:
        return "已持有/已买"
    if normalized == "OPEN_BUY_ORDER":
        return "已有买单"
    if normalized == "RISK_BLOCKED":
        return "风控暂停"
    if normalized == "CANCELED":
        return "未成交撤单"
    if normalized == "CANCEL_REQUESTED":
        return "撤单待确认"
    if "PARTIALLY_FILLED" in normalized:
        return "部分成交"
    if normalized == "FILLED":
        return "已成交"
    if normalized in {"SUBMITTED", "ACCEPTED", "NEW"}:
        return "已提交"
    return status or "未知"


def print_buy_monitor_row(
    symbol: str,
    candidate: "AfterHoursCandidate",
    *,
    current_price: float = 0.0,
    current_price_source: str = "",
    drop_pct: float | None = None,
    quantity: float = 0.0,
    status: str = "",
    note: str = "",
) -> None:
    """打印盘后买入监控表的一行。"""
    print_table_row(
        BUY_MONITOR_COLUMNS,
        {
            "symbol": normalize_symbol(symbol),
            "current": format_price(current_price),
            "source": short_price_source(current_price_source),
            "close": format_price(candidate.regular_close),
            "drop": f"{drop_pct:.2%}" if drop_pct is not None else "-",
            "limit": format_price(candidate.buy_limit),
            "qty": format_quantity(quantity),
            "status": order_status_label(status),
            "note": note,
        },
    )


@dataclass(frozen=True)
class MinuteBar:
    """盘后策略统一使用的 1m bar。"""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class RegularSessionSummary:
    """常规盘 09:30-16:00 汇总结果。"""

    symbol: str
    signal_date: date
    open: float
    high: float
    low: float
    close: float

    @property
    def range_ratio(self) -> float:
        """常规盘最高价 / 最低价。"""
        if self.low <= 0:
            return 0.0
        return self.high / self.low


@dataclass(frozen=True)
class AfterHoursCandidate:
    """常规盘波动超过阈值后，盘后准备挂单的股票。"""

    symbol: str
    signal_date: date
    regular_open: float
    regular_high: float
    regular_low: float
    regular_close: float
    range_ratio: float
    buy_limit: float
    target_sell_price: float


@dataclass(frozen=True)
class AfterHoursFill:
    """按盘后 1m bar 推导出的策略成交结果。"""

    symbol: str
    filled: bool
    status: str
    fill_price: float
    fill_time: datetime | None
    reason: str


@dataclass(frozen=True)
class ExistingBuyExposure:
    """提交前从 Alpaca 读取到的当前持仓和开放买单。"""

    position_symbols: set[str]
    open_buy_order_symbols: set[str]


def run_afterhours_high_low_strategy(
    settings: Settings | None = None,
    *,
    symbols: list[str] | None = None,
    max_symbols: int | None = None,
    buy_notional_usd: float | None = None,
    max_orders: int | None = None,
    dry_run: bool = True,
    feed: str = "sip",
    batch_size: int = 100,
    range_ratio_threshold: float | None = None,
    drop_signal_threshold: float = DROP_SIGNAL_THRESHOLD,
    order_timeout_seconds: int = 300,
    order_status_poll_seconds: int = 5,
    require_paper: bool | None = None,
    now_et: datetime | None = None,
) -> list[AfterHoursCandidate]:
    """盘中不买；盘后筛选 high/low>1.8 的股票并按 close*0.8 准备买入。"""
    settings = settings or build_settings()
    now_et = now_et or datetime.now(ZoneInfo(settings.market_timezone))
    buy_notional_usd = buy_notional_usd or settings.buy_notional_usd
    range_ratio_threshold = range_ratio_threshold if range_ratio_threshold is not None else RANGE_RATIO_THRESHOLD
    require_paper = True if require_paper is None else require_paper

    if is_regular_session(now_et):
        print_section("盘后策略待命")
        print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
        print_detail("当前状态", "常规盘中")
        print_detail("执行动作", "只做卖出管理，本文件不会盘中买入")
        manage_afterhours_sells(settings, now_et, dry_run=dry_run)
        return []
    if not dry_run and not is_afterhours_buy_time(now_et):
        status, action = afterhours_idle_status(now_et)
        print_section("盘后策略待命")
        print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
        print_detail("当前状态", status)
        print_detail("执行动作", f"{action}；跳过真实挂单")
        return []

    signal_day = afterhours_signal_day(now_et)
    print_afterhours_header(
        now_et,
        signal_day,
        dry_run=dry_run,
        feed=feed,
        range_ratio_threshold=range_ratio_threshold,
        drop_signal_threshold=drop_signal_threshold,
        buy_notional_usd=buy_notional_usd,
        require_paper=require_paper,
    )

    candidates = scan_afterhours_candidates(
        settings,
        now_et,
        symbols=symbols,
        max_symbols=max_symbols,
        feed=feed,
        batch_size=batch_size,
        range_ratio_threshold=range_ratio_threshold,
    )

    # 3. dry-run 时用当前已出现的盘后 1m bar 推导策略成交，便于复盘。
    afterhours_start, afterhours_end = afterhours_session_bounds(signal_day, now_et.tzinfo)
    replay_end = min(now_et, afterhours_end)
    if dry_run:
        print_step("3/4", "dry-run 盘后成交复盘")
        print_detail("复盘区间", f"{afterhours_start:%H:%M} - {replay_end:%H:%M} {afterhours_start:%Z}")
        afterhours_bars = fetch_minute_bars([candidate.symbol for candidate in candidates], afterhours_start, replay_end, feed=feed, batch_size=batch_size) if candidates else {}
        fills = [simulate_afterhours_fill(candidate, afterhours_bars.get(candidate.symbol, [])) for candidate in candidates]
        fill_path = write_afterhours_fill_report(settings.output_dir, candidates, fills, signal_day, buy_notional_usd)
        filled_count = sum(1 for fill in fills if fill.filled)
        print_detail("模拟成交", f"{filled_count}/{len(fills)}")
        print_detail("复盘文件", fill_path)
        print_detail("下单状态", "dry-run 不提交真实订单")
        print_step("4/4", "完成")
        print_detail("提示", "真实盘后挂单时，把 alpaca_ma5_service/afterhours_monitor.py 里的 AFTERHOURS_DRY_RUN 改成 False")
        return candidates

    # 4. 真实模式先确认当前跌幅信号，再提交 5 分钟限价单；未成交就取消，等待下一轮信号。
    order_candidates = candidates[:max_orders] if max_orders is not None else candidates
    print_step("3/4", "实时信号检查和 5 分钟订单监控")
    print_detail("待检查", f"{len(order_candidates)} 只")
    results = submit_afterhours_limit_buys(
        settings,
        order_candidates,
        buy_notional_usd,
        now_et,
        drop_signal_threshold=drop_signal_threshold,
        timeout_seconds=order_timeout_seconds,
        poll_seconds=order_status_poll_seconds,
        require_paper=require_paper,
    )
    submitted_count = sum(1 for result in results if result.status != "NO_SIGNAL")
    print_step("4/4", "完成")
    print_detail("信号触发", f"{submitted_count}/{len(results)}")
    print_detail("订单结果", f"{len(results)} 条记录")
    return candidates


def scan_afterhours_candidates(
    settings: Settings,
    now_et: datetime,
    *,
    symbols: list[str] | None = None,
    max_symbols: int | None = None,
    feed: str = "sip",
    batch_size: int = 100,
    range_ratio_threshold: float | None = None,
) -> list[AfterHoursCandidate]:
    """全量扫描当日常规盘 high/low 候选池；持续监控时每天只需要跑一次。"""
    signal_day = afterhours_signal_day(now_et)
    range_ratio_threshold = range_ratio_threshold if range_ratio_threshold is not None else RANGE_RATIO_THRESHOLD
    use_daily_cache = symbols is None and max_symbols is None
    if use_daily_cache:
        cached = load_cached_afterhours_candidates(settings, signal_day, range_ratio_threshold)
        if cached is not None:
            print_step("1/4", "复用当天候选池缓存")
            print_detail("候选数量", f"{len(cached)} 个")
            print_candidate_preview(cached)
            print_detail("观察文件", afterhours_watch_codes_path(settings))
            print_detail("候选文件", afterhours_candidates_path(settings.output_dir, signal_day))
            return cached

    # 1. 构建股票池：默认全量 Alpaca active/tradable 普通股。
    symbol_pool = [to_alpaca_symbol(symbol) for symbol in symbols] if symbols else load_afterhours_symbol_pool(max_symbols)
    if max_symbols is not None:
        symbol_pool = symbol_pool[:max_symbols]
    print_step("1/4", "股票池准备")
    print_detail("股票数量", f"{len(symbol_pool)} 只")

    # 2. 读取常规盘 1m bar，精确计算 09:30-16:00 的 high/low/close。
    regular_start, regular_end = regular_session_bounds(signal_day, now_et.tzinfo)
    print_step("2/4", "常规盘分钟线扫描")
    print_detail("扫描区间", f"{regular_start:%H:%M} - {regular_end:%H:%M} {regular_start:%Z}")
    regular_bars = fetch_minute_bars(symbol_pool, regular_start, regular_end, feed=feed, batch_size=batch_size)
    candidates = screen_afterhours_candidates(regular_bars, signal_day, range_ratio_threshold=range_ratio_threshold)
    candidate_path = write_afterhours_candidates(settings.output_dir, candidates, signal_day)
    watch_code_path = write_afterhours_watch_codes(afterhours_watch_codes_path(settings), candidates, signal_day, range_ratio_threshold)
    print_detail("候选数量", f"{len(candidates)} 个")
    print_candidate_preview(candidates)
    print_detail("候选文件", candidate_path)
    print_detail("观察文件", watch_code_path)
    return candidates


def is_regular_session(now_et: datetime) -> bool:
    """判断当前是否处于常规盘；本策略在这段时间不买。"""
    return now_et.weekday() < 5 and REGULAR_OPEN <= now_et.time() < REGULAR_CLOSE


def is_afterhours_buy_time(now_et: datetime) -> bool:
    """盘后真实监控/挂单窗口：16:00 <= t < 20:00 ET。"""
    return now_et.weekday() < 5 and REGULAR_CLOSE <= now_et.time() < AFTERHOURS_DATA_CLOSE


def afterhours_signal_day(now_et: datetime) -> date:
    """返回最近一个已经完成常规盘的交易日；周末运行时回看上一个周五。"""
    if now_et.weekday() < 5 and now_et.time() >= REGULAR_CLOSE:
        return now_et.date()
    day = now_et.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def afterhours_idle_status(now_et: datetime) -> tuple[str, str]:
    """给等待状态打印准确原因，避免周末或 20:00 后还显示等待当天收盘。"""
    if now_et.weekday() >= 5:
        return "非交易日", "等待下一个交易日常规盘收盘"
    if now_et.time() < REGULAR_OPEN:
        return "常规盘未开盘", "等待今天常规盘收盘"
    if is_regular_session(now_et):
        return "常规盘中", "等待今天常规盘收盘"
    if now_et.time() >= AFTERHOURS_DATA_CLOSE:
        return "盘后交易已结束", "等待下一个交易日常规盘收盘"
    return "盘后交易窗口", "继续监控盘后买入和卖出"


def regular_session_bounds(day: date, tzinfo) -> tuple[datetime, datetime]:
    """返回常规盘分钟线边界。"""
    return datetime.combine(day, REGULAR_OPEN, tzinfo=tzinfo), datetime.combine(day, REGULAR_CLOSE, tzinfo=tzinfo)


def afterhours_session_bounds(day: date, tzinfo) -> tuple[datetime, datetime]:
    """返回 Alpaca 盘后 1m bar 常规 extended-hours 边界。"""
    return datetime.combine(day, REGULAR_CLOSE, tzinfo=tzinfo), datetime.combine(day, AFTERHOURS_DATA_CLOSE, tzinfo=tzinfo)


def load_afterhours_symbol_pool(max_symbols: int | None = None) -> list[str]:
    """复用盘中 watch code 的 active/tradable 普通股股票池。"""
    return load_tradable_symbols(max_symbols=max_symbols)


def fetch_minute_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
    *,
    feed: str = "sip",
    batch_size: int = 100,
) -> dict[str, list[MinuteBar]]:
    """分批读取 1m bar；SIP 失败时按批次降级 IEX。"""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    if not symbols or end <= start:
        return {}

    api_key, secret_key = load_alpaca_credentials()
    client = StockHistoricalDataClient(api_key, secret_key)
    bars_by_symbol: dict[str, list[MinuteBar]] = {}
    normalized_symbols = [to_alpaca_symbol(symbol) for symbol in symbols]

    for batch in batched(normalized_symbols, batch_size):
        try:
            raw_bars = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame.Minute,
                    start=start,
                    end=end,
                    adjustment=Adjustment.SPLIT,
                    feed=DataFeed(feed.lower()),
                )
            ).data
        except Exception as exc:
            if feed.lower() == "iex":
                print_warning("1m bar 读取失败", f"跳过 {batch[0]}...{batch[-1]}；{short_error(exc)}")
                continue
            print_warning("1m bar 读取失败", f"{feed.upper()} 批次 {batch[0]}...{batch[-1]} 失败，改用 IEX；{short_error(exc)}")
            try:
                raw_bars = client.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=batch,
                        timeframe=TimeFrame.Minute,
                        start=start,
                        end=end,
                        adjustment=Adjustment.SPLIT,
                        feed=DataFeed("iex"),
                    )
                ).data
            except Exception as fallback_exc:
                print_warning("IEX 1m bar 读取失败", f"跳过 {batch[0]}...{batch[-1]}；{short_error(fallback_exc)}")
                continue

        for symbol, bars in raw_bars.items():
            parsed = [minute_bar_from_alpaca(symbol.upper(), bar, start.tzinfo) for bar in bars]
            bars_by_symbol[symbol.upper()] = [bar for bar in parsed if start <= bar.timestamp < end]

    return bars_by_symbol


def minute_bar_from_alpaca(symbol: str, bar, tzinfo) -> MinuteBar:
    """把 alpaca-py bar 转成内部 1m bar。"""
    return MinuteBar(
        symbol=symbol,
        timestamp=bar.timestamp.astimezone(tzinfo),
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
    )


def screen_afterhours_candidates(
    bars_by_symbol: dict[str, list[MinuteBar]],
    signal_day: date,
    *,
    range_ratio_threshold: float = RANGE_RATIO_THRESHOLD,
) -> list[AfterHoursCandidate]:
    """筛选常规盘 high/low > 1.8 的股票，并计算 close*0.8 买入价。"""
    candidates: list[AfterHoursCandidate] = []
    for symbol, bars in bars_by_symbol.items():
        summary = summarize_regular_session(symbol, bars, signal_day)
        if not summary or summary.range_ratio <= range_ratio_threshold:
            continue
        buy_limit = round(summary.close * BUY_LIMIT_MULTIPLIER, 2)
        candidates.append(
            AfterHoursCandidate(
                symbol=summary.symbol,
                signal_date=summary.signal_date,
                regular_open=summary.open,
                regular_high=summary.high,
                regular_low=summary.low,
                regular_close=summary.close,
                range_ratio=summary.range_ratio,
                buy_limit=buy_limit,
                target_sell_price=round(buy_limit * PROFIT_TARGET_MULTIPLIER, 2),
            )
        )
    return sorted(candidates, key=lambda item: item.range_ratio, reverse=True)


def summarize_regular_session(symbol: str, bars: list[MinuteBar], signal_day: date) -> RegularSessionSummary | None:
    """从常规盘 1m bar 汇总 open/high/low/close。"""
    session_bars = sorted([bar for bar in bars if bar.timestamp.date() == signal_day and bar.low > 0], key=lambda item: item.timestamp)
    if not session_bars:
        return None
    return RegularSessionSummary(
        symbol=to_alpaca_symbol(symbol),
        signal_date=signal_day,
        open=session_bars[0].open,
        high=max(bar.high for bar in session_bars),
        low=min(bar.low for bar in session_bars),
        close=session_bars[-1].close,
    )


def simulate_afterhours_fill(candidate: AfterHoursCandidate, bars: list[MinuteBar]) -> AfterHoursFill:
    """按用户规则用盘后 1m bar 判断是否成交。"""
    for bar in sorted(bars, key=lambda item: item.timestamp):
        if bar.open <= candidate.buy_limit:
            return AfterHoursFill(candidate.symbol, True, "FILLED_OPEN", bar.open, bar.timestamp, "1m open <= 买入限价，按 open 成交")
        if bar.low <= candidate.buy_limit:
            return AfterHoursFill(candidate.symbol, True, "FILLED_LIMIT", candidate.buy_limit, bar.timestamp, "1m low <= 买入限价，按限价成交")
    return AfterHoursFill(candidate.symbol, False, "NOT_TOUCHED", 0.0, None, "盘后 1m bar 未碰到买入价")


def submit_afterhours_limit_buys(
    settings: Settings,
    candidates: list[AfterHoursCandidate],
    buy_notional_usd: float,
    now_et: datetime,
    *,
    drop_signal_threshold: float = DROP_SIGNAL_THRESHOLD,
    timeout_seconds: int = 300,
    poll_seconds: int = 5,
    require_paper: bool = True,
) -> list[OrderResult]:
    """当前价跌幅超过阈值后提交 BUY LIMIT；5 分钟不成交就取消。"""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    if not is_afterhours_buy_time(now_et):
        status, _ = afterhours_idle_status(now_et)
        if candidates:
            print_table_header(BUY_MONITOR_COLUMNS)
        results: list[OrderResult] = []
        for candidate in candidates:
            result = OrderResult("", normalize_symbol(candidate.symbol), "BUY", 0, candidate.buy_limit, "NO_SIGNAL", f"不在盘后买入窗口：{status}")
            print_buy_monitor_row(result.symbol, candidate, status=result.status, note=result.message)
            results.append(result)
        return results

    connection = build_trading_connection()
    if require_paper and not connection.paper:
        raise RuntimeError("盘后策略配置为只允许 Paper 下单，但当前 Alpaca key 被识别为 LIVE，已停止提交订单。")
    client = connection.client
    broker_name = "alpaca-paper" if connection.paper else "alpaca-live"
    results: list[OrderResult] = []
    already_bought_symbols = load_afterhours_executed_buy_symbols(settings, now_et.date())
    existing_exposure = load_existing_buy_exposure(client)
    price_source = None
    if candidates:
        print_table_header(BUY_MONITOR_COLUMNS)

    try:
        if existing_exposure is None:
            for candidate in candidates:
                symbol = normalize_symbol(candidate.symbol)
                result = OrderResult("", symbol, "BUY", 0, candidate.buy_limit, "RISK_BLOCKED", "无法确认 Alpaca 当前持仓/开放买单，已跳过本轮买入")
                print_buy_monitor_row(symbol, candidate, status=result.status, note=result.message)
                results.append(result)
            return results

        for candidate in candidates:
            symbol = normalize_symbol(candidate.symbol)
            exposure_status, exposure_note = existing_buy_exposure_status(existing_exposure, symbol)
            if symbol in already_bought_symbols:
                result = OrderResult("", symbol, "BUY", 0, candidate.buy_limit, "ALREADY_BOUGHT_TODAY", "本地订单记录显示今天已买成，跳过防重复")
                print_buy_monitor_row(symbol, candidate, status=result.status, note=result.message)
                results.append(result)
                continue
            if exposure_status:
                result = OrderResult("", symbol, "BUY", 0, candidate.buy_limit, exposure_status, exposure_note)
                print_buy_monitor_row(symbol, candidate, status=result.status, note=result.message)
                results.append(result)
                continue
            try:
                if price_source is None:
                    price_source = build_afterhours_price_source(settings)
                current_price, current_price_source = latest_trade_price_quote(symbol, settings, price_source=price_source, now_et=now_et)
            except Exception as exc:
                error = short_error(exc)
                result = OrderResult("", symbol, "BUY", 0, candidate.buy_limit, "NO_SIGNAL", f"当前价格读取失败：{error}")
                print_buy_monitor_row(
                    symbol,
                    candidate,
                    status=result.status,
                    note=f"取价失败: {error}",
                )
                results.append(result)
                continue
            drop_pct = current_drop_pct(candidate, current_price)
            signal_price = candidate.regular_close * (1.0 - drop_signal_threshold)
            if current_price <= 0:
                result = OrderResult("", symbol, "BUY", 0, candidate.buy_limit, "NO_SIGNAL", "当前价格无效，未提交订单")
                print_buy_monitor_row(
                    symbol,
                    candidate,
                    current_price=current_price,
                    current_price_source=current_price_source,
                    drop_pct=drop_pct,
                    status=result.status,
                    note="当前价无效",
                )
                results.append(result)
                continue
            if drop_pct <= drop_signal_threshold:
                message = (
                    f"当前跌幅 {drop_pct:.2%} 未超过 {drop_signal_threshold:.0%}，"
                    f"当前价 {current_price:.4f} > 信号价 {signal_price:.4f}，等待下一次信号"
                )
                result = OrderResult("", symbol, "BUY", 0, candidate.buy_limit, "NO_SIGNAL", message)
                print_buy_monitor_row(
                    symbol,
                    candidate,
                    current_price=current_price,
                    current_price_source=current_price_source,
                    drop_pct=drop_pct,
                    status=result.status,
                    note=f"未超过 {drop_signal_threshold:.0%}; 信号价<={signal_price:.4f}",
                )
                results.append(result)
                continue

            qty = buy_quantity(client, symbol, buy_notional_usd, candidate.buy_limit, settings.allow_fractional_shares)
            reason = (
                f"盘后 high/low>1.8 买入；range={candidate.range_ratio:.4f} "
                f"close={candidate.regular_close:.4f} current={current_price:.4f} "
                f"source={current_price_source or 'unknown'} "
                f"drop={drop_pct:.2%} limit=close*0.8；5分钟不成交撤单"
            )
            if qty <= 0:
                result = OrderResult("", symbol, "BUY", 0, candidate.buy_limit, "REJECTED", "买入金额不足 1 股")
                print_buy_monitor_row(
                    symbol,
                    candidate,
                    current_price=current_price,
                    current_price_source=current_price_source,
                    drop_pct=drop_pct,
                    status=result.status,
                    note="买入金额不足1股",
                )
                record_afterhours_order(settings, result, reason, order_time=now_et)
                results.append(result)
                continue

            try:
                raw = client.submit_order(
                    order_data=LimitOrderRequest(
                        symbol=to_alpaca_symbol(symbol),
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                        limit_price=candidate.buy_limit,
                        extended_hours=True,
                    )
                )
                submitted = OrderResult(
                    str(getattr(raw, "id", "") or ""),
                    symbol,
                    "BUY",
                    qty,
                    candidate.buy_limit,
                    normalize_order_status(raw) or "SUBMITTED",
                    "盘后 BUY LIMIT 已提交",
                )
                record_afterhours_order(settings, submitted, reason, order_time=now_et)
                result = wait_for_fill_or_cancel(
                    client,
                    raw,
                    symbol,
                    "BUY",
                    qty,
                    candidate.buy_limit,
                    broker_name,
                    timeout_seconds=timeout_seconds,
                    poll_seconds=poll_seconds,
                )
            except Exception as exc:
                result = OrderResult("", symbol, "BUY", qty, candidate.buy_limit, "REJECTED", short_error(exc))

            record_afterhours_order(settings, result, reason, order_time=now_et)
            note = result.message or "订单完成"
            if result.order_id:
                note = f"{note}; id={result.order_id}"
            print_buy_monitor_row(
                symbol,
                candidate,
                current_price=current_price,
                current_price_source=current_price_source,
                drop_pct=drop_pct,
                quantity=result.quantity or qty,
                status=result.status,
                note=note,
            )
            results.append(result)
    finally:
        close_afterhours_price_source(price_source)

    return results


def current_drop_pct(candidate: AfterHoursCandidate, current_price: float) -> float:
    """当前价相对常规盘收盘价的跌幅。"""
    if candidate.regular_close <= 0 or current_price <= 0:
        return 0.0
    return 1.0 - current_price / candidate.regular_close


def record_afterhours_order(settings: Settings, result: OrderResult, reason: str, *, order_time: datetime) -> None:
    """盘后策略只写本地订单记录，不输出 OpenClaw 通知。"""
    append_order(settings.output_dir, result, reason, day=order_time.date(), created_at=order_time)


def load_afterhours_executed_buy_symbols(settings: Settings, signal_day: date) -> set[str]:
    """从本地订单记录读取当天已经买成过的盘后策略股票。"""
    path = orders_file(settings.output_dir, signal_day)
    if not path.exists():
        return set()

    symbols: set[str] = set()
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            symbol = afterhours_executed_buy_symbol(row)
            if symbol:
                symbols.add(symbol)
    return symbols


def load_recent_afterhours_executed_buy_symbols(output_dir: Path, max_files: int = 10) -> set[str]:
    """读取最近订单记录里本策略真实买成过的股票，用于卖出管理。"""
    symbols: set[str] = set()
    paths = sorted(output_dir.glob("orders_*.csv"), reverse=True)[:max_files]
    for path in paths:
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    symbol = afterhours_executed_buy_symbol(row)
                    if symbol:
                        symbols.add(symbol)
        except OSError as exc:
            print_warning("订单记录读取失败", f"{path.name}: {short_error(exc)}")
    return symbols


def afterhours_executed_buy_symbol(row: dict[str, str]) -> str:
    """从订单 CSV 行中提取本策略已成交买入股票；撤单/拒单不算。"""
    reason = row.get("reason", "")
    if row.get("side", "").upper() != "BUY":
        return ""
    if AFTERHOURS_ORDER_REASON_MARKER not in reason or "买入" not in reason:
        return ""
    if not is_executed_afterhours_status(row.get("status", "")):
        return ""
    return normalize_symbol(row.get("symbol", ""))


def is_executed_afterhours_status(status: str) -> bool:
    """盘后策略认为已经买成的状态。"""
    normalized = status.upper()
    return normalized == "FILLED" or normalized.startswith("PARTIALLY_FILLED")


def load_existing_buy_exposure(client) -> ExistingBuyExposure | None:
    """从 Alpaca 查当前持仓和开放买单；失败时返回 None 让买入暂停。"""
    try:
        position_symbols = current_position_symbols(client)
        open_buy_order_symbols = current_open_buy_order_symbols(client)
    except Exception as exc:
        print_warning("买入风控检查失败", f"无法确认 Alpaca 当前持仓/开放买单：{short_error(exc)}")
        return None
    return ExistingBuyExposure(position_symbols, open_buy_order_symbols)


def current_position_symbols(client) -> set[str]:
    """读取 Alpaca 当前持仓股票；测试假客户端没有该接口时视为无持仓。"""
    if not hasattr(client, "get_all_positions"):
        return set()
    symbols: set[str] = set()
    for raw in client.get_all_positions():
        symbol = normalize_symbol(getattr(raw, "symbol", ""))
        try:
            qty = float(getattr(raw, "qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if symbol and qty > 0:
            symbols.add(symbol)
    return symbols


def current_open_buy_order_symbols(client) -> set[str]:
    """读取 Alpaca 当前开放买单股票；测试假客户端没有该接口时视为无开放买单。"""
    return current_open_order_symbols(client, "BUY")


def current_open_sell_order_symbols(client) -> set[str]:
    """读取 Alpaca 当前开放卖单股票；用于卖出管理防重复。"""
    return current_open_order_symbols(client, "SELL")


def current_open_order_symbols(client, side_filter: str) -> set[str]:
    """读取 Alpaca 当前指定方向的开放订单股票。"""
    if not hasattr(client, "get_orders"):
        return set()

    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    symbols: set[str] = set()
    for raw in orders:
        side = raw_order_side(raw)
        symbol = normalize_symbol(getattr(raw, "symbol", ""))
        if side == side_filter.upper() and symbol:
            symbols.add(symbol)
    return symbols


def raw_order_side(raw_order) -> str:
    """从 Alpaca order 读取 BUY/SELL。"""
    value = getattr(raw_order, "side", "") or ""
    value = getattr(value, "value", value)
    return str(value).split(".")[-1].upper()


def existing_buy_exposure_status(exposure: ExistingBuyExposure, symbol: str) -> tuple[str, str]:
    """判断某只股票是否已有买入暴露。"""
    normalized = normalize_symbol(symbol)
    if normalized in exposure.position_symbols:
        return "EXISTING_POSITION", "Alpaca 当前已有持仓，跳过防重复"
    if normalized in exposure.open_buy_order_symbols:
        return "OPEN_BUY_ORDER", "Alpaca 当前已有开放买单，跳过防重复"
    return "", ""


def disable_afterhours_openclaw_output(settings: Settings) -> Settings:
    """盘后策略复用通用 broker 时，强制关闭 OpenClaw 输出。"""
    if not settings.trade_notify_openclaw_enabled:
        return settings
    return replace(settings, trade_notify_openclaw_enabled=False)


def manage_afterhours_sells(settings: Settings, now_et: datetime, *, dry_run: bool = True) -> list[OrderResult]:
    """常规盘管理卖出：盈利 10% 卖一半，尾盘卖剩余；不设置止损。"""
    from .broker import AlpacaStockBroker

    managed_symbols = load_recent_afterhours_executed_buy_symbols(settings.output_dir)
    if not managed_symbols:
        print_warning("卖出管理跳过", "没有找到本策略真实成交的盘后买入记录")
        return []

    print_step("卖出", "持仓管理")
    print_detail("管理范围", f"{len(managed_symbols)} 只本策略已买成股票")
    broker = AlpacaStockBroker(disable_afterhours_openclaw_output(settings))
    positions = broker.get_positions()
    state = load_afterhours_sell_state(settings.output_dir)
    results: list[OrderResult] = []
    open_sell_order_symbols = load_open_sell_order_symbols(broker) if not dry_run else set()
    if open_sell_order_symbols is None:
        return results
    price_source = build_afterhours_price_source(settings)

    try:
        for symbol in sorted(managed_symbols):
            position = positions.get(normalize_symbol(symbol))
            if not position or position.quantity <= 0:
                continue
            if not dry_run and normalize_symbol(symbol) in open_sell_order_symbols:
                print_order_signal(
                    normalize_symbol(symbol),
                    "卖出跳过",
                    [
                        ("原因", "Alpaca 当前已有开放卖单"),
                        ("动作", "跳过本轮卖出，防止重复卖出"),
                    ],
                )
                continue

            try:
                current_price, current_price_source = latest_trade_price_quote(symbol, settings, price_source=price_source, now_et=now_et)
            except Exception as exc:
                print_order_signal(
                    normalize_symbol(symbol),
                    "卖出跳过",
                    [
                        ("原因", f"当前价格读取失败：{short_error(exc)}"),
                        ("动作", "跳过卖出管理"),
                    ],
                )
                continue
            if current_price <= 0:
                print_order_signal(
                    normalize_symbol(symbol),
                    "卖出跳过",
                    [
                        ("原因", "当前价无效"),
                        ("价格来源", current_price_source or "未知"),
                        ("动作", "跳过卖出管理"),
                    ],
                )
                continue

            if is_close_sell_time(settings, now_et):
                reason = f"盘后 high/low 策略尾盘卖出剩余持仓；current={current_price:.4f} source={current_price_source or 'unknown'}；无止损"
                result = preview_or_sell(broker, symbol, position.quantity, current_price, reason, dry_run)
                results.append(result)
                state.pop(normalize_symbol(symbol), None)
                continue

            state_key = normalize_symbol(symbol)
            already_half_sold = bool(state.get(state_key, {}).get("half_sold"))
            target_price = position.avg_price * PROFIT_TARGET_MULTIPLIER
            if not already_half_sold and current_price >= target_price:
                quantity = round(position.quantity / 2.0, 6)
                reason = (
                    f"盘后 high/low 策略盈利 10% 卖出一半；entry={position.avg_price:.4f} "
                    f"current={current_price:.4f} source={current_price_source or 'unknown'}；无止损"
                )
                result = preview_or_sell(broker, symbol, quantity, current_price, reason, dry_run)
                results.append(result)
                if not dry_run and is_executed_afterhours_status(result.status):
                    state[state_key] = {"half_sold": True, "updated_at": now_et.isoformat(timespec="seconds")}
    finally:
        close_afterhours_price_source(price_source)

    if not dry_run:
        save_afterhours_sell_state(settings.output_dir, state)
    print_detail("卖出结果", f"{len(results)} 笔{' dry-run 预览' if dry_run else ''}")
    return results


def load_open_sell_order_symbols(broker) -> set[str] | None:
    """真实卖出前读取开放卖单；失败时暂停卖出，避免重复卖。"""
    client = getattr(broker, "client", None)
    if client is None:
        return set()
    try:
        return current_open_sell_order_symbols(client)
    except Exception as exc:
        print_warning("卖出风控检查失败", f"无法确认 Alpaca 当前开放卖单：{short_error(exc)}")
        return None


def preview_or_sell(broker, symbol: str, quantity: float, current_price: float, reason: str, dry_run: bool) -> OrderResult:
    """dry-run 时只打印预览；真实模式才提交卖单。"""
    if dry_run:
        result = OrderResult("", normalize_symbol(symbol), "SELL", quantity, current_price, "DRY_RUN", "卖出预览，未提交真实订单")
        print_order_signal(
            result.symbol,
            "卖出预览",
            [
                ("数量", f"{quantity:.6f}"),
                ("参考价", f"{current_price:.4f}"),
                ("原因", reason),
            ],
        )
        return result
    return broker.place_market_sell(symbol, quantity, current_price, reason)


def is_close_sell_time(settings: Settings, now_et: datetime) -> bool:
    """尾盘卖出剩余持仓窗口。"""
    return settings.close_liquidation_start <= now_et.time() < settings.close_liquidation_end


def afterhours_sell_state_file(output_dir: Path) -> Path:
    """记录哪些股票已经卖过一半，避免重复触发 10% 止盈。"""
    return output_dir / "afterhours_sell_state.json"


def load_afterhours_sell_state(output_dir: Path) -> dict[str, dict[str, object]]:
    """读取盘后策略卖出状态；文件不存在表示没有卖过一半。"""
    path = afterhours_sell_state_file(output_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    raw = data.get("positions", {})
    return raw if isinstance(raw, dict) else {}


def save_afterhours_sell_state(output_dir: Path, state: dict[str, dict[str, object]]) -> None:
    """保存盘后策略卖出状态。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    afterhours_sell_state_file(output_dir).write_text(json.dumps({"positions": state}, indent=2, ensure_ascii=False), encoding="utf-8")


def latest_trade_price_quote(symbol: str, settings: Settings, feed: str = "iex", *, price_source=None, now_et: datetime | None = None) -> tuple[float, str]:
    """读取订单判断用实时价；默认使用 Moomoo OpenD，只有配置为 alpaca 时才走 Alpaca。"""
    if settings.realtime_price_source.lower() != "alpaca":
        source = price_source
        should_close = False
        if source is None:
            source = build_afterhours_price_source(settings)
            should_close = True
        try:
            if hasattr(source, "latest_price_quote"):
                quote = source.latest_price_quote(normalize_symbol(symbol))
                validate_realtime_quote_date(symbol, quote, now_et)
                return float(getattr(quote, "price", 0.0) or 0.0), str(getattr(quote, "source", "") or type(source).__name__)
            return float(source.latest_price(normalize_symbol(symbol)) or 0.0), type(source).__name__
        finally:
            if should_close:
                close_afterhours_price_source(source)

    return latest_alpaca_trade_price_quote(symbol, feed=feed)


def validate_realtime_quote_date(symbol: str, quote, now_et: datetime | None) -> None:
    """真实下单判断必须使用当前交易日快照，避免上一天价格误触发。"""
    as_of = getattr(quote, "as_of", None)
    if now_et is None or as_of is None:
        return
    if as_of.date() != now_et.date():
        raise RuntimeError(f"{normalize_symbol(symbol)} 实时快照日期 {as_of.date()} != 当前交易日 {now_et.date()}，跳过下单")


def build_afterhours_price_source(settings: Settings):
    """构建盘后订单判断使用的实时价源。"""
    from .market_data import build_realtime_price_source

    return build_realtime_price_source(settings)


def close_afterhours_price_source(price_source) -> None:
    """关闭实时价源，避免 OpenD context 在脚本结束后残留。"""
    if price_source is not None and hasattr(price_source, "close"):
        price_source.close()


def latest_alpaca_trade_price_quote(symbol: str, feed: str = "iex") -> tuple[float, str]:
    """读取 Alpaca latest trade；仅作为明确配置 REALTIME_PRICE_SOURCE=alpaca 时的兜底。"""
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    api_key, secret_key = load_alpaca_credentials()
    client = StockHistoricalDataClient(api_key, secret_key)
    request = StockLatestTradeRequest(symbol_or_symbols=[to_alpaca_symbol(symbol)], feed=DataFeed(feed.lower()))
    trade = client.get_stock_latest_trade(request).get(to_alpaca_symbol(symbol))
    try:
        return float(getattr(trade, "price", 0) or 0), f"alpaca_latest_trade:{feed.lower()}"
    except (TypeError, ValueError):
        return 0.0, f"alpaca_latest_trade:{feed.lower()}"


def buy_quantity(client, symbol: str, notional_usd: float, limit_price: float, allow_fractional_shares: bool) -> float:
    """按限价把金额换成整数股；allow_fractional_shares 参数保留为兼容入口。"""
    if limit_price <= 0:
        return 0.0
    return float(math.floor(notional_usd / limit_price))


def can_buy_fractional(client, symbol: str) -> bool:
    """查询 Alpaca 是否允许碎股；失败时保守退回整数股。"""
    try:
        asset = client.get_asset(to_alpaca_symbol(symbol))
        return bool(getattr(asset, "fractionable", False))
    except Exception as exc:
        print_warning("碎股权限查询失败", f"{normalize_symbol(symbol)} 改用整数股；{short_error(exc)}")
        return False


def write_afterhours_candidates(output_dir: Path, candidates: list[AfterHoursCandidate], signal_day: date) -> Path:
    """写出带日期的盘后候选文件。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = afterhours_candidates_path(output_dir, signal_day)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "symbol",
                "signal_date",
                "regular_open",
                "regular_high",
                "regular_low",
                "regular_close",
                "range_ratio",
                "buy_limit",
                "target_sell_price",
            ],
        )
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.__dict__)
    return path


def afterhours_candidates_path(output_dir: Path, signal_day: date) -> Path:
    """返回指定交易日的盘后候选 CSV 文件。"""
    return output_dir / f"afterhours_candidates_{signal_day:%Y-%m-%d}.csv"


def afterhours_watch_codes_path(settings: Settings) -> Path:
    """返回盘后观察池 txt 文件。"""
    return settings.watch_codes_file.with_name("watch_code_afterhours.txt")


def load_cached_afterhours_candidates(settings: Settings, signal_day: date, range_ratio_threshold: float) -> list[AfterHoursCandidate] | None:
    """当天 txt 和候选 CSV 都有效时，直接复用，避免重复扫常规盘分钟线。"""
    watch_path = afterhours_watch_codes_path(settings)
    watch_day, watch_threshold, watch_symbols = read_afterhours_watch_metadata(watch_path)
    if watch_day != signal_day:
        return None
    if watch_threshold is not None and not math.isclose(watch_threshold, range_ratio_threshold, rel_tol=0, abs_tol=1e-9):
        print_warning("候选缓存失效", f"{watch_path.name} 阈值 {watch_threshold:g} != 当前配置 {range_ratio_threshold:g}，重新扫描")
        return None

    candidate_path = afterhours_candidates_path(settings.output_dir, signal_day)
    if not candidate_path.exists():
        print_warning("候选缓存缺失", f"{watch_path.name} 是当天文件，但 {candidate_path.name} 不存在，重新扫描补齐")
        return None

    try:
        candidates = read_afterhours_candidates(candidate_path)
    except Exception as exc:
        print_warning("候选缓存读取失败", f"{candidate_path.name}: {short_error(exc)}，重新扫描")
        return None

    if any(candidate.signal_date != signal_day for candidate in candidates):
        print_warning("候选缓存失效", f"{candidate_path.name} 内日期不是 {signal_day}，重新扫描")
        return None
    candidate_symbols = [normalize_symbol(candidate.symbol) for candidate in candidates]
    if watch_symbols and watch_symbols != candidate_symbols:
        print_warning("候选缓存不一致", f"{watch_path.name} 和 {candidate_path.name} 股票列表不一致，重新扫描")
        return None
    return candidates


def read_afterhours_watch_metadata(path: Path) -> tuple[date | None, float | None, list[str]]:
    """读取盘后观察池 txt 的日期、阈值和股票列表。"""
    if not path.exists():
        return None, None, []

    signal_day: date | None = None
    threshold: float | None = None
    symbols: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# signal_date="):
            try:
                signal_day = date.fromisoformat(line.split("=", 1)[1].strip())
            except ValueError:
                signal_day = None
            continue
        if line.startswith("# Rules:") and ">" in line:
            try:
                threshold = float(line.rsplit(">", 1)[1].strip())
            except ValueError:
                threshold = None
            continue
        if not line.startswith("#"):
            symbols.append(normalize_symbol(line))
    return signal_day, threshold, symbols


def read_afterhours_candidates(path: Path) -> list[AfterHoursCandidate]:
    """从缓存 CSV 读取盘后候选。"""
    candidates: list[AfterHoursCandidate] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            candidates.append(
                AfterHoursCandidate(
                    symbol=normalize_symbol(row.get("symbol", "")),
                    signal_date=date.fromisoformat(str(row.get("signal_date", "")).strip()),
                    regular_open=float(row.get("regular_open", 0) or 0),
                    regular_high=float(row.get("regular_high", 0) or 0),
                    regular_low=float(row.get("regular_low", 0) or 0),
                    regular_close=float(row.get("regular_close", 0) or 0),
                    range_ratio=float(row.get("range_ratio", 0) or 0),
                    buy_limit=float(row.get("buy_limit", 0) or 0),
                    target_sell_price=float(row.get("target_sell_price", 0) or 0),
                )
            )
    return candidates


def write_afterhours_watch_codes(
    path: Path,
    candidates: list[AfterHoursCandidate],
    signal_day: date,
    range_ratio_threshold: float = RANGE_RATIO_THRESHOLD,
) -> Path:
    """写出盘后策略专用观察池。"""
    lines = [
        "# Auto-generated by watchcode_afterhours.py",
        f"# Rules: regular-session high / low > {range_ratio_threshold:g}",
        f"# signal_date={signal_day:%Y-%m-%d}",
    ]
    lines.extend(normalize_symbol(candidate.symbol) for candidate in candidates)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_afterhours_fill_report(
    output_dir: Path,
    candidates: list[AfterHoursCandidate],
    fills: list[AfterHoursFill],
    signal_day: date,
    buy_notional_usd: float,
) -> Path:
    """写出盘后 1m 成交推导和卖出计划。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"afterhours_fills_{signal_day:%Y-%m-%d}.csv"
    fill_by_symbol = {fill.symbol: fill for fill in fills}
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "symbol",
                "signal_date",
                "buy_limit",
                "fill_status",
                "fill_price",
                "fill_time",
                "fill_reason",
                "estimated_quantity",
                "sell_half_at",
                "sell_rest_rule",
                "stop_loss",
            ],
        )
        writer.writeheader()
        for candidate in candidates:
            fill = fill_by_symbol.get(candidate.symbol)
            fill_price = fill.fill_price if fill and fill.filled else 0.0
            quantity = float(math.floor(buy_notional_usd / fill_price)) if fill_price > 0 else 0.0
            writer.writerow(
                {
                    "symbol": candidate.symbol,
                    "signal_date": candidate.signal_date,
                    "buy_limit": candidate.buy_limit,
                    "fill_status": fill.status if fill else "NOT_CHECKED",
                    "fill_price": fill_price,
                    "fill_time": fill.fill_time.isoformat(timespec="seconds") if fill and fill.fill_time else "",
                    "fill_reason": fill.reason if fill else "",
                    "estimated_quantity": quantity,
                    "sell_half_at": round(fill_price * PROFIT_TARGET_MULTIPLIER, 2) if fill_price > 0 else "",
                    "sell_rest_rule": "尾盘卖出剩余一半；若已手动卖出则不再重复卖",
                    "stop_loss": "无",
                }
            )
    return path
