from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import date, datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from .afterhours_high_low import (
    AfterHoursCandidate,
    afterhours_idle_status,
    afterhours_signal_day,
    afterhours_watch_codes_path,
    build_afterhours_price_source,
    close_afterhours_price_source,
    current_drop_pct,
    is_afterhours_buy_time,
    is_regular_session,
    load_afterhours_executed_buy_symbols,
    latest_trade_price_quote,
    print_detail,
    print_section,
    print_table_header,
    print_table_row,
    print_warning,
    scan_afterhours_candidates,
)
from .config import Settings, build_settings
from .errors import short_error
from .openclaw_notify import safe_send_openclaw_messages
from .run_lock import acquire_run_lock
from .watchlist import normalize_symbol


# 点箭头运行只改这里：盘后策略配置全部放在这个 Python 文件里。
AFTERHOURS_REQUIRE_PAPER = False
AFTERHOURS_DRY_RUN = False
AFTERHOURS_BUY_NOTIONAL_USD = 3400.0
AFTERHOURS_MAX_ORDERS = None
AFTERHOURS_MAX_SYMBOLS = None
AFTERHOURS_FEED = "sip"
AFTERHOURS_BATCH_SIZE = 100
AFTERHOURS_RANGE_RATIO_THRESHOLD = 1.8
AFTERHOURS_DROP_SIGNAL_THRESHOLD = 0.15
AFTERHOURS_ORDER_TIMEOUT_SECONDS = 300
AFTERHOURS_ORDER_STATUS_POLL_SECONDS = 5
AFTERHOURS_MONITOR_POLL_SECONDS = 60
AFTERHOURS_WAIT_POLL_SECONDS = 300
AFTERHOURS_TAIL_SELL_START = clock_time(19, 55)
AFTERHOURS_TAIL_SELL_END = clock_time(20, 0)


@dataclass(frozen=True)
class AfterHoursAlertResult:
    symbol: str
    current_price: float = 0.0
    current_price_source: str = ""
    drop_pct: float | None = None
    signal_price: float = 0.0
    status: str = "NO_SIGNAL"
    message: str = ""
    should_notify: bool = False


AFTERHOURS_ALERT_COLUMNS = [
    ("symbol", "股票", 9),
    ("current", "当前价", 8),
    ("source", "来源", 11),
    ("close", "收盘", 8),
    ("drop", "跌幅", 7),
    ("signal", "提醒线", 8),
    ("reference", "参考价", 8),
    ("status", "状态", 10),
    ("note", "说明", 28),
]


def run_afterhours_high_low_buyer(
    require_paper: bool = AFTERHOURS_REQUIRE_PAPER,
    *,
    max_loops: int | None = None,
    sleep=time.sleep,
    now_provider=None,
    stop_at_afterhours_end: bool = False,
) -> None:
    """盘后 high/low>1.8 持续监控入口：先扫池，再循环检测买卖信号。"""
    # 1. 当前文件顶部是盘后策略配置，方便 PyCharm 点箭头前直接改。
    # 2. 当前入口是提醒-only；require_paper 只为兼容旧调用签名保留，不会用于提交订单。
    # 3. 默认一直运行；Ctrl+C 停止。测试时才传 max_loops。
    # 4. 每天盘后只全量扫一次股票池，后续循环复用候选池监控提醒信号。
    # 5. 盘中不会买入；16:00 ET 后自动筛选、监控买入和卖出。
    # 6. 当前价相对常规盘收盘价跌幅 > 15% 才提醒/触发，订单 5 分钟不成交就取消。
    # 7. 每只股票盘后最多买成一次；盈利 10% 卖一半，19:55-20:00 ET 卖剩余。
    settings = afterhours_monitor_settings(build_settings())
    run_lock = acquire_afterhours_run_lock(settings.output_dir)
    try:
        now_provider = now_provider or (lambda: datetime.now(ZoneInfo(settings.market_timezone)))
        scanned_signal_day = None
        candidates: list[AfterHoursCandidate] = []
        alerted_symbols: set[str] = set()
        loop_count = 0

        print_section("盘后 high/low 持续监控启动")
        print_detail("监控间隔", f"{AFTERHOURS_MONITOR_POLL_SECONDS}s")
        print_detail("等待间隔", f"{AFTERHOURS_WAIT_POLL_SECONDS}s")
        print_detail("尾盘卖出", f"{AFTERHOURS_TAIL_SELL_START:%H:%M}-{AFTERHOURS_TAIL_SELL_END:%H:%M} ET")
        print_detail("停止方式", "Ctrl+C")
        monitor_start_notified = False

        while True:
            loop_count += 1
            now_et = now_provider()
            if not monitor_start_notified:
                notify_afterhours_monitor_started(settings, now_et, require_paper=require_paper)
                monitor_start_notified = True
            if stop_at_afterhours_end and now_et.time() >= AFTERHOURS_TAIL_SELL_END:
                print_section("盘后 high/low 监控结束")
                print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
                print_detail("执行动作", "已到 20:00 ET，退出自动监控入口")
                break
            try:
                if is_regular_session(now_et):
                    status, action = afterhours_idle_status(now_et)
                    print_section("盘后 high/low 监控待命")
                    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
                    print_detail("当前状态", status)
                    print_detail("执行动作", f"{action}；盘后入口只提醒，不做盘中卖出管理")
                elif is_afterhours_buy_time(now_et):
                    signal_day = afterhours_signal_day(now_et)
                    if scanned_signal_day != signal_day:
                        alerted_symbols = set()
                        candidates = generate_afterhours_monitor_stocks(settings=settings, now_et=now_et)
                        scanned_signal_day = signal_day
                    monitor_afterhours_buy_signals(
                        settings,
                        candidates,
                        now_et,
                        require_paper=require_paper,
                        alerted_symbols=alerted_symbols,
                    )
                else:
                    status, action = afterhours_idle_status(now_et)
                    print_section("盘后策略待命")
                    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
                    print_detail("当前状态", status)
                    print_detail("执行动作", f"{action}，持续等待")
            except KeyboardInterrupt:
                print_warning("监控已停止", "收到 Ctrl+C，结束持续监控")
                break
            except Exception as exc:
                print_warning("本轮监控失败", short_error(exc))

            if max_loops is not None and loop_count >= max_loops:
                print_detail("监控状态", f"已完成测试轮数 {max_loops}，退出")
                break

            poll_seconds = loop_poll_seconds(now_et)
            print_detail("下一轮", f"{poll_seconds}s 后继续监控")
            sleep(poll_seconds)
    finally:
        run_lock.close()


def acquire_afterhours_run_lock(output_dir: Path):
    """同一输出目录只允许运行一个盘后监控，防止脚本多开重复提醒。"""
    return acquire_run_lock(output_dir, "afterhours_high_low_buyer.lock", "盘后监控")


def generate_afterhours_monitor_stocks(settings=None, now_et: datetime | None = None) -> list[AfterHoursCandidate]:
    """生成盘后监控股票池，并写出 watch_code_afterhours.txt 和候选 CSV。"""
    settings = afterhours_monitor_settings(settings or build_settings())
    now_et = now_et or datetime.now(ZoneInfo(settings.market_timezone))
    signal_day = afterhours_signal_day(now_et)

    print_section("盘后候选池扫描")
    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
    print_detail("使用交易日", signal_day)
    print_detail("执行动作", "生成 watch_code_afterhours.txt，并写出候选 CSV")
    candidates = scan_afterhours_candidates(
        settings,
        now_et,
        max_symbols=AFTERHOURS_MAX_SYMBOLS,
        feed=AFTERHOURS_FEED,
        batch_size=AFTERHOURS_BATCH_SIZE,
        range_ratio_threshold=AFTERHOURS_RANGE_RATIO_THRESHOLD,
    )
    notify_afterhours_scan_result(settings, now_et, signal_day, candidates)
    return candidates


def monitor_afterhours_trades(
    require_paper: bool = AFTERHOURS_REQUIRE_PAPER,
    *,
    max_loops: int | None = None,
    sleep=time.sleep,
    now_provider=None,
    stop_at_afterhours_end: bool = False,
) -> None:
    """启动盘后自动监控：生成股票池并发送价格提醒，不自动下单。"""
    run_afterhours_high_low_buyer(
        require_paper=require_paper,
        max_loops=max_loops,
        sleep=sleep,
        now_provider=now_provider,
        stop_at_afterhours_end=stop_at_afterhours_end,
    )


def afterhours_monitor_settings(settings):
    """点一个监控入口时，把尾盘卖出窗口切到盘后 19:55-20:00。"""
    return replace(
        settings,
        close_liquidation_start=AFTERHOURS_TAIL_SELL_START,
        close_liquidation_end=AFTERHOURS_TAIL_SELL_END,
    )


def notify_afterhours_monitor_started(settings: Settings, now_et: datetime, *, require_paper: bool) -> None:
    safe_send_openclaw_messages(
        settings,
        [render_afterhours_monitor_start_message(settings, now_et, require_paper=require_paper)],
        context="afterhours high/low monitor started",
    )


def render_afterhours_monitor_start_message(settings: Settings, now_et: datetime, *, require_paper: bool) -> str:
    return "\n".join(
        [
            "【盘后 high/low 监控启动】",
            "",
            "结论：盘后监控已开始。本入口只发送提醒，不会提交 Alpaca 买单或卖单。",
            f"运行时间：{now_et:%Y-%m-%d %H:%M:%S %Z}",
            "运行模式：提醒-only，不下单",
            "",
            "监控范围",
            f"- 观察文件：{afterhours_watch_codes_path(settings)}",
            f"- 筛选条件：常规盘 high / low > {AFTERHOURS_RANGE_RATIO_THRESHOLD:g}",
            f"- 提醒信号：盘后当前价相对常规盘收盘价跌幅 > {AFTERHOURS_DROP_SIGNAL_THRESHOLD:.0%}",
            "",
            "执行规则",
            "- 动作：只发云端提醒；不创建订单；不撤单；不做尾盘卖出",
            "- 重复控制：同一轮盘后监控内，每支股票首次触发时提醒一次",
            f"- 轮询频率：盘后每 {AFTERHOURS_MONITOR_POLL_SECONDS} 秒一轮",
        ]
    )


def notify_afterhours_scan_result(
    settings: Settings,
    now_et: datetime,
    signal_day: date,
    candidates: list[AfterHoursCandidate],
) -> None:
    safe_send_openclaw_messages(
        settings,
        [render_afterhours_scan_result_message(settings, now_et, signal_day, candidates)],
        context="afterhours high/low scan result",
    )


def render_afterhours_scan_result_message(
    settings: Settings,
    now_et: datetime,
    signal_day: date,
    candidates: list[AfterHoursCandidate],
) -> str:
    preview = render_afterhours_candidate_preview(candidates)
    return "\n".join(
        [
            "【盘后 high/low 筛选结果】",
            "",
            f"结论：筛选出 {len(candidates)} 只盘后监控候选股。",
            f"运行时间：{now_et:%Y-%m-%d %H:%M:%S %Z}",
            f"使用交易日：{signal_day:%Y-%m-%d}",
            "",
            "筛选规则",
            f"- 常规盘 high / low > {AFTERHOURS_RANGE_RATIO_THRESHOLD:g}",
            f"- 后续提醒信号：盘后跌幅 > {AFTERHOURS_DROP_SIGNAL_THRESHOLD:.0%}",
            "- 后续动作：只提醒，不提交 Alpaca 订单",
            f"- 观察文件：{afterhours_watch_codes_path(settings)}",
            "",
            "候选预览",
            preview,
        ]
    )


def render_afterhours_candidate_preview(candidates: list[AfterHoursCandidate], limit: int = 12) -> str:
    if not candidates:
        return "- 暂无候选股；本轮盘后只继续等待下一次扫描/监控。"
    rows = []
    for candidate in candidates[:limit]:
        rows.append(
            "- "
            f"{normalize_symbol(candidate.symbol)} | "
            f"high/low={candidate.range_ratio:.2f} | "
            f"收盘={candidate.regular_close:.4f} | "
            f"提醒线={candidate.regular_close * (1.0 - AFTERHOURS_DROP_SIGNAL_THRESHOLD):.4f} | "
            f"参考价={candidate.buy_limit:.4f}"
        )
    if len(candidates) > limit:
        rows.append(f"- 其余 {len(candidates) - limit} 只已写入观察文件。")
    return "\n".join(rows)


def print_afterhours_alert_row(
    symbol: str,
    candidate: AfterHoursCandidate,
    *,
    current_price: float = 0.0,
    current_price_source: str = "",
    drop_pct: float | None = None,
    status: str = "",
    note: str = "",
) -> None:
    print_table_row(
        AFTERHOURS_ALERT_COLUMNS,
        {
            "symbol": normalize_symbol(symbol),
            "current": format_afterhours_alert_price(current_price),
            "source": short_afterhours_alert_source(current_price_source),
            "close": format_afterhours_alert_price(candidate.regular_close),
            "drop": f"{drop_pct:.2%}" if drop_pct is not None else "-",
            "signal": format_afterhours_alert_price(candidate.regular_close * (1.0 - AFTERHOURS_DROP_SIGNAL_THRESHOLD)),
            "reference": format_afterhours_alert_price(candidate.buy_limit),
            "status": afterhours_alert_status_label(status),
            "note": note,
        },
    )


def format_afterhours_alert_price(value: float) -> str:
    return f"{value:.4f}" if value > 0 else "-"


def short_afterhours_alert_source(source: str) -> str:
    compact = (source or "未知").replace("moomoo_snapshot:", "moomoo:").replace("alpaca_latest_trade:", "alpaca:")
    return compact.replace("_price", "")


def afterhours_alert_status_label(status: str) -> str:
    normalized = status.upper()
    if normalized == "ALERT":
        return "提醒已发"
    if normalized == "ALERT_SENT":
        return "已提醒"
    if normalized == "NO_SIGNAL":
        return "等待信号"
    return status or "未知"


def notify_afterhours_alert_signal_results(
    settings: Settings,
    now_et: datetime,
    candidates: list[AfterHoursCandidate],
    results: list[AfterHoursAlertResult],
) -> None:
    messages = [
        render_afterhours_alert_signal_message(candidate, result, now_et)
        for candidate, result in zip(candidates, results)
        if result.should_notify
    ]
    if messages:
        safe_send_openclaw_messages(settings, messages, context="afterhours high/low alert signal")


def render_afterhours_alert_signal_message(
    candidate: AfterHoursCandidate,
    result: AfterHoursAlertResult,
    now_et: datetime,
) -> str:
    drop_text = f"{result.drop_pct:.2%}" if result.drop_pct is not None else "-"
    return "\n".join(
        [
            f"【盘后 high/low 提醒】{normalize_symbol(candidate.symbol)}",
            "",
            f"结论：盘后跌幅已超过 {AFTERHOURS_DROP_SIGNAL_THRESHOLD:.0%} 提醒线。这里只提醒，不下单。",
            f"运行时间：{now_et:%Y-%m-%d %H:%M:%S %Z}",
            "",
            "核心价格",
            f"- 常规盘收盘：{candidate.regular_close:.4f}",
            f"- 当前盘后价：{result.current_price:.4f}",
            f"- 当前跌幅：{drop_text}",
            f"- 提醒线：{result.signal_price:.4f}",
            f"- 行情来源：{result.current_price_source or 'unknown'}",
            "",
            "策略参考",
            f"- 常规盘 high/low：{candidate.range_ratio:.2f}",
            f"- 参考价 close*0.8：{candidate.buy_limit:.4f}（仅展示，不自动提交）",
            f"- 说明：{result.message or '-'}",
        ]
    )


def check_afterhours_alert_signals(
    settings: Settings,
    candidates: list[AfterHoursCandidate],
    now_et: datetime,
    *,
    alerted_symbols: set[str] | None = None,
    drop_signal_threshold: float = AFTERHOURS_DROP_SIGNAL_THRESHOLD,
) -> list[AfterHoursAlertResult]:
    """检测盘后价格提醒，不创建 Alpaca connection，也不提交任何订单。"""
    alerted_symbols = alerted_symbols if alerted_symbols is not None else set()
    results: list[AfterHoursAlertResult] = []
    if candidates:
        print_table_header(AFTERHOURS_ALERT_COLUMNS)

    if not is_afterhours_buy_time(now_et):
        status, _ = afterhours_idle_status(now_et)
        for candidate in candidates:
            symbol = normalize_symbol(candidate.symbol)
            result = AfterHoursAlertResult(
                symbol=symbol,
                signal_price=candidate.regular_close * (1.0 - drop_signal_threshold),
                status="NO_SIGNAL",
                message=f"不在盘后提醒窗口：{status}",
            )
            print_afterhours_alert_row(symbol, candidate, status=result.status, note=result.message)
            results.append(result)
        return results

    price_source = None
    try:
        for candidate in candidates:
            symbol = normalize_symbol(candidate.symbol)
            signal_price = candidate.regular_close * (1.0 - drop_signal_threshold)
            try:
                if price_source is None:
                    price_source = build_afterhours_price_source(settings)
                current_price, current_price_source = latest_trade_price_quote(
                    symbol,
                    settings,
                    price_source=price_source,
                    now_et=now_et,
                )
            except Exception as exc:
                error = short_error(exc)
                result = AfterHoursAlertResult(
                    symbol=symbol,
                    signal_price=signal_price,
                    status="NO_SIGNAL",
                    message=f"当前价格读取失败：{error}",
                )
                print_afterhours_alert_row(symbol, candidate, status=result.status, note=f"取价失败: {error}")
                results.append(result)
                continue

            drop_pct = current_drop_pct(candidate, current_price)
            if current_price <= 0:
                result = AfterHoursAlertResult(
                    symbol=symbol,
                    current_price=current_price,
                    current_price_source=current_price_source,
                    drop_pct=drop_pct,
                    signal_price=signal_price,
                    status="NO_SIGNAL",
                    message="当前价格无效，未触发提醒",
                )
                print_afterhours_alert_row(
                    symbol,
                    candidate,
                    current_price=current_price,
                    current_price_source=current_price_source,
                    drop_pct=drop_pct,
                    status=result.status,
                    note="当前价格无效",
                )
                results.append(result)
                continue

            if drop_pct <= drop_signal_threshold:
                result = AfterHoursAlertResult(
                    symbol=symbol,
                    current_price=current_price,
                    current_price_source=current_price_source,
                    drop_pct=drop_pct,
                    signal_price=signal_price,
                    status="NO_SIGNAL",
                    message=f"当前跌幅 {drop_pct:.2%} 未超过 {drop_signal_threshold:.0%}",
                )
                print_afterhours_alert_row(
                    symbol,
                    candidate,
                    current_price=current_price,
                    current_price_source=current_price_source,
                    drop_pct=drop_pct,
                    status=result.status,
                    note=f"未超过 {drop_signal_threshold:.0%}; 提醒价<={signal_price:.4f}",
                )
                results.append(result)
                continue

            already_alerted = symbol in alerted_symbols
            status = "ALERT_SENT" if already_alerted else "ALERT"
            result = AfterHoursAlertResult(
                symbol=symbol,
                current_price=current_price,
                current_price_source=current_price_source,
                drop_pct=drop_pct,
                signal_price=signal_price,
                status=status,
                message=(
                    f"当前跌幅 {drop_pct:.2%} 已超过 {drop_signal_threshold:.0%}；"
                    "仅发送提醒，不下单"
                ),
                should_notify=not already_alerted,
            )
            if result.should_notify:
                alerted_symbols.add(symbol)
            print_afterhours_alert_row(
                symbol,
                candidate,
                current_price=current_price,
                current_price_source=current_price_source,
                drop_pct=drop_pct,
                status=result.status,
                note="提醒已发" if result.should_notify else "已提醒过",
            )
            results.append(result)
    finally:
        close_afterhours_price_source(price_source)

    return results


def monitor_afterhours_buy_signals(
    settings,
    candidates: list[AfterHoursCandidate],
    now_et: datetime,
    *,
    require_paper: bool,
    bought_symbols: set[str] | None = None,
    alerted_symbols: set[str] | None = None,
) -> None:
    """复用当天候选池持续检测提醒信号；保留函数名兼容旧入口，但不会下单。"""
    print_section("盘后提醒信号监控")
    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
    print_detail("候选数量", f"{len(candidates)} 个")
    print_detail("运行模式", "只提醒，不提交 Alpaca 订单")
    if not candidates:
        print_detail("执行动作", "暂无候选股，等待下一轮")
        return

    results = check_afterhours_alert_signals(
        settings,
        candidates,
        now_et,
        alerted_symbols=alerted_symbols,
        drop_signal_threshold=AFTERHOURS_DROP_SIGNAL_THRESHOLD,
    )
    notify_afterhours_alert_signal_results(settings, now_et, candidates, results)
    triggered = sum(1 for result in results if result.status in {"ALERT", "ALERT_SENT"})
    print_detail("信号触发", f"{triggered}/{len(results)}")


def monitor_afterhours_sell_signals(settings, now_et: datetime) -> None:
    """盘后 monitor 是提醒-only；这里保留兼容入口但不提交卖单。"""
    print_section("盘后卖出信号监控")
    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
    print_detail("执行动作", "只提醒，不做盘后卖出管理")


def loop_poll_seconds(now_et: datetime) -> int:
    """盘中/盘后高频监控，其他时间低频等待。"""
    if is_regular_session(now_et) or is_afterhours_buy_time(now_et):
        return AFTERHOURS_MONITOR_POLL_SECONDS
    return AFTERHOURS_WAIT_POLL_SECONDS


def load_afterhours_bought_symbols(settings, signal_day: date) -> set[str]:
    """从当天订单记录恢复已成交股票，脚本重启后也不重复买成。"""
    return load_afterhours_executed_buy_symbols(settings, signal_day)


if __name__ == "__main__":
    run_afterhours_high_low_buyer(require_paper=AFTERHOURS_REQUIRE_PAPER)
