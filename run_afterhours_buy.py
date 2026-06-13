from __future__ import annotations

import fcntl
import os
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.afterhours_high_low import (
    AfterHoursCandidate,
    can_scan_after_regular_close,
    is_regular_session,
    load_afterhours_executed_buy_symbols,
    manage_afterhours_sells,
    print_detail,
    print_section,
    print_warning,
    run_afterhours_high_low_strategy,
    scan_afterhours_candidates,
    submit_afterhours_limit_buys,
)
from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.errors import short_error
from alpaca_ma5_service.models import is_executed_order_status
from alpaca_ma5_service.watchlist import normalize_symbol


# 点箭头运行只改这里：盘后策略配置全部放在这个 Python 文件里。
AFTERHOURS_REQUIRE_PAPER = False
AFTERHOURS_DRY_RUN = False
AFTERHOURS_BUY_NOTIONAL_USD = 3400.0
AFTERHOURS_MAX_ORDERS = None
AFTERHOURS_MAX_SYMBOLS = None
AFTERHOURS_FEED = "sip"
AFTERHOURS_BATCH_SIZE = 100
AFTERHOURS_RANGE_RATIO_THRESHOLD = 2.5
AFTERHOURS_DROP_SIGNAL_THRESHOLD = 0.18
AFTERHOURS_ORDER_TIMEOUT_SECONDS = 300
AFTERHOURS_ORDER_STATUS_POLL_SECONDS = 5
AFTERHOURS_MONITOR_POLL_SECONDS = 60
AFTERHOURS_WAIT_POLL_SECONDS = 300


def run_afterhours_high_low_buyer(require_paper: bool = AFTERHOURS_REQUIRE_PAPER, *, max_loops: int | None = None, sleep=time.sleep, now_provider=None) -> None:
    """盘后 high/low>2.5 持续监控入口：先扫池，再循环检测买卖信号。"""
    # 1. 当前文件顶部是盘后策略配置，方便 PyCharm 点箭头前直接改。
    # 2. require_paper=False 时允许 live key 真实下单；切回 Paper 时把 AFTERHOURS_REQUIRE_PAPER 改 True。
    # 3. 默认一直运行；Ctrl+C 停止。测试时才传 max_loops。
    # 4. 每天盘后只全量扫一次股票池，后续循环复用候选池监控买入信号。
    # 5. 盘中不会买入，只持续管理卖出；16:00 ET 后持续监控买入。
    # 6. 当前价相对常规盘收盘价跌幅 > 18% 才下单，订单 5 分钟不成交就取消。
    # 7. 每只股票盘后最多买成一次；纯撤单不算次数，可以继续等下一次信号。
    settings = build_settings()
    run_lock = acquire_afterhours_run_lock(settings.output_dir)
    try:
        now_provider = now_provider or (lambda: datetime.now(ZoneInfo(settings.market_timezone)))
        scanned_signal_day = None
        candidates: list[AfterHoursCandidate] = []
        bought_symbols: set[str] = set()
        loop_count = 0

        print_section("盘后 high/low 持续监控启动")
        print_detail("监控间隔", f"{AFTERHOURS_MONITOR_POLL_SECONDS}s")
        print_detail("等待间隔", f"{AFTERHOURS_WAIT_POLL_SECONDS}s")
        print_detail("停止方式", "Ctrl+C")

        while True:
            loop_count += 1
            now_et = now_provider()
            try:
                if is_regular_session(now_et):
                    # 盘中只做卖出管理，不触发买入。
                    run_afterhours_high_low_strategy(
                        settings=settings,
                        dry_run=AFTERHOURS_DRY_RUN,
                        buy_notional_usd=AFTERHOURS_BUY_NOTIONAL_USD,
                        max_orders=AFTERHOURS_MAX_ORDERS,
                        max_symbols=AFTERHOURS_MAX_SYMBOLS,
                        feed=AFTERHOURS_FEED,
                        batch_size=AFTERHOURS_BATCH_SIZE,
                        range_ratio_threshold=AFTERHOURS_RANGE_RATIO_THRESHOLD,
                        drop_signal_threshold=AFTERHOURS_DROP_SIGNAL_THRESHOLD,
                        order_timeout_seconds=AFTERHOURS_ORDER_TIMEOUT_SECONDS,
                        order_status_poll_seconds=AFTERHOURS_ORDER_STATUS_POLL_SECONDS,
                        require_paper=require_paper,
                        now_et=now_et,
                    )
                elif can_scan_after_regular_close(now_et):
                    if scanned_signal_day != now_et.date():
                        bought_symbols = load_afterhours_bought_symbols(settings, now_et.date())
                        if AFTERHOURS_DRY_RUN:
                            candidates = run_afterhours_high_low_strategy(
                                settings=settings,
                                dry_run=AFTERHOURS_DRY_RUN,
                                buy_notional_usd=AFTERHOURS_BUY_NOTIONAL_USD,
                                max_orders=AFTERHOURS_MAX_ORDERS,
                                max_symbols=AFTERHOURS_MAX_SYMBOLS,
                                feed=AFTERHOURS_FEED,
                                batch_size=AFTERHOURS_BATCH_SIZE,
                                range_ratio_threshold=AFTERHOURS_RANGE_RATIO_THRESHOLD,
                                drop_signal_threshold=AFTERHOURS_DROP_SIGNAL_THRESHOLD,
                                order_timeout_seconds=AFTERHOURS_ORDER_TIMEOUT_SECONDS,
                                order_status_poll_seconds=AFTERHOURS_ORDER_STATUS_POLL_SECONDS,
                                require_paper=require_paper,
                                now_et=now_et,
                            )
                        else:
                            print_section("盘后候选池扫描")
                            print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
                            print_detail("执行动作", "每天全量扫描一次，后续循环只监控候选池")
                            candidates = scan_afterhours_candidates(
                                settings,
                                now_et,
                                max_symbols=AFTERHOURS_MAX_SYMBOLS,
                                feed=AFTERHOURS_FEED,
                                batch_size=AFTERHOURS_BATCH_SIZE,
                                range_ratio_threshold=AFTERHOURS_RANGE_RATIO_THRESHOLD,
                            )
                            monitor_afterhours_buy_signals(settings, candidates, now_et, require_paper=require_paper, bought_symbols=bought_symbols)
                            monitor_afterhours_sell_signals(settings, now_et)
                        scanned_signal_day = now_et.date()
                    else:
                        monitor_afterhours_buy_signals(settings, candidates, now_et, require_paper=require_paper, bought_symbols=bought_symbols)
                        monitor_afterhours_sell_signals(settings, now_et)
                else:
                    print_section("盘后策略待命")
                    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
                    print_detail("当前状态", "等待常规盘收盘")
                    print_detail("执行动作", "持续等待，不退出")
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
    """同一输出目录只允许运行一个盘后监控，防止脚本多开重复下单。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / "afterhours_high_low_buyer.lock"
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError(f"盘后监控已经在运行：{lock_path}") from exc
    lock_file.write(f"pid={os.getpid()}\n")
    lock_file.flush()
    return lock_file


def monitor_afterhours_buy_signals(
    settings,
    candidates: list[AfterHoursCandidate],
    now_et: datetime,
    *,
    require_paper: bool,
    bought_symbols: set[str] | None = None,
) -> None:
    """复用当天候选池持续检测买入信号，避免每分钟全量重扫股票池。"""
    print_section("盘后买入信号监控")
    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
    print_detail("候选数量", f"{len(candidates)} 个")
    bought_symbols = bought_symbols if bought_symbols is not None else set()
    if AFTERHOURS_DRY_RUN:
        print_detail("运行模式", "dry-run，只保留全量扫描复盘，不重复模拟下单")
        return
    if not candidates:
        print_detail("执行动作", "暂无候选股，等待下一轮")
        return

    available_candidates = [candidate for candidate in candidates if normalize_symbol(candidate.symbol) not in bought_symbols]
    skipped_count = len(candidates) - len(available_candidates)
    if skipped_count:
        print_detail("已成交跳过", f"{skipped_count} 只")
    order_candidates = available_candidates[:AFTERHOURS_MAX_ORDERS] if AFTERHOURS_MAX_ORDERS is not None else available_candidates
    if not order_candidates:
        print_detail("执行动作", "候选股都已成交过或无可检查股票，等待卖出管理")
        return

    results = submit_afterhours_limit_buys(
        settings,
        order_candidates,
        AFTERHOURS_BUY_NOTIONAL_USD,
        now_et,
        drop_signal_threshold=AFTERHOURS_DROP_SIGNAL_THRESHOLD,
        timeout_seconds=AFTERHOURS_ORDER_TIMEOUT_SECONDS,
        poll_seconds=AFTERHOURS_ORDER_STATUS_POLL_SECONDS,
        require_paper=require_paper,
    )
    triggered = sum(1 for result in results if result.status != "NO_SIGNAL")
    for result in results:
        if result.side.upper() == "BUY" and (is_executed_order_status(result.status) or result.status in {"ALREADY_BOUGHT_TODAY", "EXISTING_POSITION"}):
            bought_symbols.add(normalize_symbol(result.symbol))
    print_detail("信号触发", f"{triggered}/{len(results)}")


def monitor_afterhours_sell_signals(settings, now_et: datetime) -> None:
    """盘后也持续检查卖出信号：盈利 10% 卖一半；尾盘卖出由盘中循环处理。"""
    print_section("盘后卖出信号监控")
    print_detail("运行时间", f"{now_et:%Y-%m-%d %H:%M:%S %Z}")
    manage_afterhours_sells(settings, now_et, dry_run=AFTERHOURS_DRY_RUN)


def loop_poll_seconds(now_et: datetime) -> int:
    """盘中/盘后高频监控，其他时间低频等待。"""
    if is_regular_session(now_et) or can_scan_after_regular_close(now_et):
        return AFTERHOURS_MONITOR_POLL_SECONDS
    return AFTERHOURS_WAIT_POLL_SECONDS


def load_afterhours_bought_symbols(settings, signal_day: date) -> set[str]:
    """从当天订单记录恢复已成交股票，脚本重启后也不重复买成。"""
    return load_afterhours_executed_buy_symbols(settings, signal_day)


def run_afterhours_high_low_once(require_paper: bool = AFTERHOURS_REQUIRE_PAPER) -> None:
    """保留一轮执行入口，方便临时复盘或测试。"""
    run_afterhours_high_low_strategy(
        settings=build_settings(),
        dry_run=AFTERHOURS_DRY_RUN,
        buy_notional_usd=AFTERHOURS_BUY_NOTIONAL_USD,
        max_orders=AFTERHOURS_MAX_ORDERS,
        max_symbols=AFTERHOURS_MAX_SYMBOLS,
        feed=AFTERHOURS_FEED,
        batch_size=AFTERHOURS_BATCH_SIZE,
        range_ratio_threshold=AFTERHOURS_RANGE_RATIO_THRESHOLD,
        drop_signal_threshold=AFTERHOURS_DROP_SIGNAL_THRESHOLD,
        order_timeout_seconds=AFTERHOURS_ORDER_TIMEOUT_SECONDS,
        order_status_poll_seconds=AFTERHOURS_ORDER_STATUS_POLL_SECONDS,
        require_paper=require_paper,
    )


if __name__ == "__main__":
    run_afterhours_high_low_buyer(require_paper=AFTERHOURS_REQUIRE_PAPER)
