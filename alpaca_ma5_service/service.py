from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import strategy
from .broker import AlpacaStockBroker
from .config import Settings, build_settings
from .errors import short_error
from .market_data import build_market_data as build_default_market_data
from .market_time import (
    is_buy_order_time,
    is_intraday_monitor_finished,
    is_premarket_time,
    is_realtime_order_time,
    next_poll_seconds,
    now_market_time,
    seconds_until_intraday_monitor_end,
)
from .models import MarketSnapshot, Signal, has_unconfirmed_order_status, is_executed_order_status
from .openclaw_notify import safe_send_openclaw_messages
from .run_lock import acquire_run_lock
from .state import append_daily_buy_exclusion, count_today_buy_orders, count_today_symbol_order_errors, count_today_symbol_take_profit_half_sells, is_symbol_daily_buy_excluded
from .watchlist import read_watch_codes


DISPLAY_TIMEZONE = ZoneInfo("America/Los_Angeles")


@dataclass
class StopLossSession:
    """记录本监控进程启动时已有的亏损持仓，避免启动瞬间误清旧仓。"""
    initial_symbols: set[str]
    checked_initial_symbols: set[str] = field(default_factory=set)
    grandfathered_positions: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class MonitorTableRow:
    symbol: str
    action: str
    has_market_data: bool = False
    current_price: float = 0.0
    today_open: float = 0.0
    today_ma5: float = 0.0
    today_open_ma5: float = 0.0
    signal_gain_pct: float = 0.0
    current_gain_pct: float = 0.0
    order_price: float = 0.0
    order_status: str = ""
    reason: str = ""


_STOP_LOSS_SESSIONS: dict[str, StopLossSession] = {}


def build_broker(settings: Settings):
    """创建真实交易通道；单测会传入 fake broker 避免触碰 Alpaca。"""
    return AlpacaStockBroker(settings)


def build_market_data(settings: Settings):
    """创建默认行情源：Moomoo 负责实时价，Alpaca 负责日线。"""
    return build_default_market_data(settings)


def run_once(settings: Settings | None = None, market_data=None, broker=None, now: datetime | None = None) -> dict[str, int]:
    """执行一轮监控：watchlist 负责买入，当前全部持仓都会检查止损。"""
    settings = settings or build_settings()
    strategy.set_active_strategy(settings.strategy_name)
    now_et = now or now_market_time(settings)
    watch_codes = read_watch_codes(settings.watch_codes_file)

    created_market_data = market_data is None
    market_data_started = False
    broker = broker or build_broker(settings)
    can_order_now = is_realtime_order_time(now_et)
    can_buy_now = is_buy_order_time(now_et)

    try:
        watch_set = set(watch_codes)
        positions = broker.get_positions()
        stop_loss_session = stop_loss_session_for(settings, positions)
        extra_position_symbols = [symbol for symbol in positions if symbol not in watch_set]
        monitor_codes = watch_codes + extra_position_symbols
        if not monitor_codes:
            print(f"watch_codes 文件为空或不存在：{settings.watch_codes_file}；当前没有持仓需要风控")
            return {"watch": 0, "buy": 0, "sell": 0, "hold": 0, "errors": 0}
        open_buy_order_symbols = open_buy_order_symbols_for_run(broker) if watch_codes else set()

        market_data = market_data or build_market_data(settings)
        market_data_started = True
        summary = {"watch": len(monitor_codes), "buy": 0, "sell": 0, "hold": 0, "errors": 0}
        table_rows: list[MonitorTableRow] = []
        buys_used = count_today_buy_orders(settings.output_dir, now_et.date())
        buy_slots = remaining_buy_slots_for_run(settings, buys_used, watch_codes, positions)
        buy_notional, notional_note = buy_notional_for_run(settings, broker, buy_slots)
        buying_paused_for_run = False
        print_run_header(now_et, monitor_codes, broker.source_name())
        if buys_used < settings.max_daily_buys and notional_note:
            print(f"本轮买入金额：{notional_note}")
        for symbol in monitor_codes:
            try:
                position = positions.get(symbol)
                if not position and should_skip_symbol_after_order_errors(settings, symbol, now_et):
                    table_rows.append(monitor_row(symbol, "跳过", reason="今日下单错误达到上限"))
                    summary["hold"] += 1
                    continue
                if not position and symbol in watch_set:
                    open_order_reason = open_buy_order_pause_reason(open_buy_order_symbols, symbol)
                    if open_order_reason:
                        table_rows.append(monitor_row(symbol, "跳过买入", reason=open_order_reason))
                        summary["hold"] += 1
                        continue
                    if should_skip_symbol_after_daily_buy_exclusion(settings, symbol, now_et):
                        required_drop = strategy.max_buy_today_current_gain_pct(settings.strategy_name)
                        table_rows.append(monitor_row(symbol, "跳过", reason=f"今日已触达MA5但跌幅未到{_format_pct(abs(required_drop))}"))
                        summary["hold"] += 1
                        continue
                    if buying_paused_for_run:
                        table_rows.append(monitor_row(symbol, "跳过", reason="上一笔买单未确认，本轮暂停后续买入"))
                        summary["hold"] += 1
                        continue
                    if buys_used >= settings.max_daily_buys:
                        table_rows.append(monitor_row(symbol, "跳过", reason=f"今日买入次数达到上限 {settings.max_daily_buys}"))
                        summary["hold"] += 1
                        continue
                snapshot: MarketSnapshot = market_data.get_snapshot(symbol)
                if position:
                    signal = strategy.evaluate_sell(position, snapshot, now_et, settings) if symbol in watch_set else strategy.evaluate_stop_loss(position, snapshot, settings)
                    if should_hold_initial_stop_loss(stop_loss_session, symbol, position, signal):
                        signal = Signal(
                            symbol,
                            "HOLD",
                            (
                                f"监控启动时该持仓已亏损达到 {_format_pct(abs(settings.stop_loss_pct))}，"
                                "本次监控会话不自动清仓；成本或数量变化后会重新启用止损"
                            ),
                            snapshot.current_price,
                            diagnostics=signal.diagnostics,
                        )
                    half_profit_done = symbol in watch_set and take_profit_half_already_done(settings, symbol, now_et)
                    if half_profit_done:
                        if signal.action == "SELL_HALF":
                            signal = strategy.evaluate_take_profit_remainder_stop(position, snapshot, settings)
                            if signal.action == "HOLD":
                                signal = Signal(
                                    symbol,
                                    "HOLD",
                                    f"今日已执行过 {_format_pct(settings.take_profit_half_pct)} 半仓止盈，不重复卖出",
                                    snapshot.current_price,
                                    diagnostics=signal.diagnostics,
                                )
                        elif signal.action == "HOLD":
                            signal = strategy.evaluate_take_profit_remainder_stop(position, snapshot, settings)
                    if signal.action in {"SELL_ALL", "SELL_HALF"}:
                        if not can_order_now:
                            table_rows.append(monitor_row(symbol, "跳过卖出", snapshot, signal, reason="当前不在实时价下单时段"))
                            summary["hold"] += 1
                            continue
                        if is_stop_loss_signal(signal):
                            limit_price = stop_loss_limit_price_from_signal(signal)
                            if limit_price <= 0:
                                table_rows.append(monitor_row(symbol, "跳过卖出", snapshot, signal, reason="止损限价无效"))
                                summary["hold"] += 1
                                continue
                            result = broker.place_limit_sell(symbol, signal.quantity, limit_price, signal.reason)
                        else:
                            result = broker.place_market_sell(symbol, signal.quantity, snapshot.current_price, signal.reason)
                        if order_executed(result.status):
                            table_rows.append(monitor_row(symbol, "卖出", snapshot, signal, order_status=result.status, order_price=result.price, reason=result.message or signal.reason))
                            summary["sell"] += 1
                        else:
                            table_rows.append(monitor_row(symbol, "卖出未成", snapshot, signal, order_status=result.status, order_price=result.price, reason=result.message or signal.reason))
                            summary["hold"] += 1
                    else:
                        table_rows.append(monitor_row(symbol, "持有", snapshot, signal))
                        summary["hold"] += 1
                    continue

                if symbol not in watch_set:
                    table_rows.append(monitor_row(symbol, "跳过", snapshot, reason="不在观察池且无持仓"))
                    summary["hold"] += 1
                    continue

                signal = strategy.evaluate_buy(snapshot)
                if should_record_daily_buy_exclusion(signal):
                    append_daily_buy_exclusion(settings.output_dir, symbol, signal.reason, now_et.date(), now_et)
                    table_rows.append(monitor_row(symbol, "排除", snapshot, signal, reason="今日已记录排除"))
                    summary["hold"] += 1
                    continue
                if signal.action == "BUY":
                    if not can_buy_now:
                        if is_premarket_time(now_et):
                            reason = "盘前时段不买入，跳过真实买单"
                        elif not can_order_now:
                            reason = "当前不在实时价下单时段，跳过真实买单"
                        else:
                            reason = "买入只允许常规盘开盘后前 2.5 小时，跳过真实买单"
                        table_rows.append(monitor_row(symbol, "跳过买入", snapshot, signal, reason=reason))
                        summary["hold"] += 1
                        continue
                    limit_price = buy_limit_price_from_signal(signal)
                    if limit_price <= 0:
                        table_rows.append(monitor_row(symbol, "跳过买入", snapshot, signal, reason="买点价格无效"))
                        summary["hold"] += 1
                        continue
                    if buy_notional <= 0:
                        table_rows.append(monitor_row(symbol, "跳过买入", snapshot, signal, reason="本轮买入金额无效"))
                        summary["hold"] += 1
                        continue
                    result = broker.place_limit_buy(symbol, buy_notional, limit_price, signal.reason)
                    if order_executed(result.status):
                        table_rows.append(monitor_row(symbol, "买入", snapshot, signal, order_status=result.status, order_price=result.price, reason=result.message or signal.reason))
                        buys_used += 1
                        summary["buy"] += 1
                    else:
                        if has_unconfirmed_order_status(result.status):
                            buying_paused_for_run = True
                        table_rows.append(monitor_row(symbol, "买入未成", snapshot, signal, order_status=result.status, order_price=result.price, reason=result.message or signal.reason))
                        summary["hold"] += 1
                else:
                    table_rows.append(monitor_row(symbol, "观察", snapshot, signal))
                    summary["hold"] += 1
            except Exception as exc:
                summary["errors"] += 1
                table_rows.append(monitor_row(symbol, "错误", reason=f"{type(exc).__name__}: {short_error(exc)}"))
    finally:
        if created_market_data and market_data_started and hasattr(market_data, "close"):
            market_data.close()

    print_monitor_table(table_rows)
    print_summary(summary)
    return summary


def order_executed(status: str) -> bool:
    """只有成交或 dry-run 才算成功；拒单、撤单和未确认撤单都不算。"""
    return is_executed_order_status(status)


def buy_limit_price_from_signal(signal: Signal) -> float:
    """自动买入只使用策略算出的最终买点作为 BUY LIMIT 价格。"""
    try:
        return float(signal.diagnostics.get("final_buy_point", 0.0))
    except (TypeError, ValueError):
        return 0.0


def stop_loss_limit_price_from_signal(signal: Signal) -> float:
    try:
        return float(signal.diagnostics.get("stop_loss_limit_price", 0.0))
    except (TypeError, ValueError):
        return 0.0


def remaining_buy_slots_for_run(settings: Settings, buys_used: int, watch_codes: list[str], positions: dict[str, object]) -> int:
    """按日内剩余次数和当前观察池，估算本轮最多还会开几笔新仓。"""
    remaining_daily_slots = max(0, settings.max_daily_buys - buys_used)
    open_watch_symbols = [symbol for symbol in watch_codes if symbol not in positions]
    return min(remaining_daily_slots, len(open_watch_symbols))


def buy_notional_for_run(settings: Settings, broker, remaining_buy_slots: int) -> tuple[float, str]:
    """动态控制单笔金额：现金充足时不超过配置上限，现金不足时按剩余可买槽位均分。"""
    if remaining_buy_slots <= 0:
        return 0.0, ""
    cash = broker_cash(broker)
    if cash is None:
        return settings.buy_notional_usd, f"cannot read Alpaca cash; using configured fixed buy notional ${settings.buy_notional_usd:.2f}"
    if cash <= 0:
        return 0.0, f"Alpaca cash ${cash:.2f}; no buy orders this run"
    notional = min(settings.buy_notional_usd, cash / remaining_buy_slots)
    return round(notional, 2), (
        f"dynamic buy notional ${notional:.2f}; Alpaca cash ${cash:.2f}; "
        f"remaining buy slots {remaining_buy_slots}; per-stock cap ${settings.buy_notional_usd:.2f}"
    )


def open_buy_order_symbols_for_run(broker) -> set[str] | None:
    getter = getattr(broker, "get_open_buy_order_symbols", None)
    if getter is None:
        return set()
    try:
        return set(getter())
    except Exception as exc:
        print(f"[提示] 无法确认 Alpaca 当前开放买单，本轮暂停新买入：{short_error(exc)}")
        return None


def open_buy_order_pause_reason(open_buy_order_symbols: set[str] | None, symbol: str) -> str:
    if open_buy_order_symbols is None:
        return "无法确认 Alpaca 当前开放买单，本轮暂停新买入"
    if not open_buy_order_symbols:
        return ""
    if symbol in open_buy_order_symbols:
        return "Alpaca 当前已有同股开放买单，跳过防重复"
    return "Alpaca 当前已有开放买单，等待成交或取消确认，本轮暂停新买入"


def broker_cash(broker) -> float | None:
    account = getattr(broker, "account", None)
    value = getattr(account, "cash", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stop_loss_session_for(settings: Settings, positions: dict[str, object]) -> StopLossSession:
    key = str(settings.output_dir.resolve())
    session = _STOP_LOSS_SESSIONS.get(key)
    if session is None:
        session = StopLossSession(initial_symbols=set(positions))
        _STOP_LOSS_SESSIONS[key] = session
    prune_stop_loss_session(session, positions)
    return session


def prune_stop_loss_session(session: StopLossSession, positions: dict[str, object]) -> None:
    current_symbols = set(positions)
    session.initial_symbols.intersection_update(current_symbols)
    session.checked_initial_symbols.intersection_update(current_symbols)
    for symbol, fingerprint in list(session.grandfathered_positions.items()):
        position = positions.get(symbol)
        if position is None or position_fingerprint(position) != fingerprint:
            session.grandfathered_positions.pop(symbol, None)
            session.initial_symbols.discard(symbol)
            session.checked_initial_symbols.discard(symbol)


def should_hold_initial_stop_loss(
    session: StopLossSession,
    symbol: str,
    position,
    signal: Signal,
) -> bool:
    fingerprint = position_fingerprint(position)
    grandfathered = session.grandfathered_positions.get(symbol)
    if grandfathered == fingerprint:
        return is_stop_loss_signal(signal)
    if grandfathered is not None:
        session.grandfathered_positions.pop(symbol, None)
        session.initial_symbols.discard(symbol)
        session.checked_initial_symbols.discard(symbol)

    if symbol not in session.initial_symbols or symbol in session.checked_initial_symbols:
        return False

    session.checked_initial_symbols.add(symbol)
    if not is_stop_loss_signal(signal):
        return False
    session.grandfathered_positions[symbol] = fingerprint
    return True


def is_stop_loss_signal(signal: Signal) -> bool:
    return signal.action == "SELL_ALL" and signal.diagnostics.get("sell_rule") in {"stop_loss", "take_profit_remainder_stop"}


def position_fingerprint(position) -> tuple[float, float]:
    return (round(float(position.quantity), 6), round(float(position.avg_price), 6))


def should_skip_symbol_after_order_errors(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """同一股票当天拒单达到阈值后，只停止继续买入，不影响已有持仓卖出。"""
    if settings.max_symbol_order_errors <= 0:
        return False
    error_count = count_today_symbol_order_errors(settings.output_dir, symbol, now_et.date())
    if error_count < settings.max_symbol_order_errors:
        return False
    return True


def should_skip_symbol_after_daily_buy_exclusion(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """当天触达 MA5 但未跌到指定跌幅的股票，后续不再检查买入。"""
    if not is_symbol_daily_buy_excluded(settings.output_dir, symbol, now_et.date()):
        return False
    return True


def should_record_daily_buy_exclusion(signal: Signal) -> bool:
    return signal.diagnostics.get("daily_buy_exclusion") == "ma5_touch_without_required_drop"


def take_profit_half_already_done(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """同一股票当天只执行一次 10% 半仓止盈。"""
    return count_today_symbol_take_profit_half_sells(settings.output_dir, symbol, now_et.date()) > 0


def run_forever(
    settings: Settings | None = None,
    *,
    max_loops: int | None = None,
    sleep=time.sleep,
    now_provider=None,
) -> None:
    """常驻监控入口；单轮异常会记录并继续下一轮。"""
    settings = settings or build_settings()
    now_provider = now_provider or (lambda: datetime.now(ZoneInfo(settings.market_timezone)))
    start_now = now_provider()
    if is_intraday_monitor_finished(start_now):
        print(f"[{start_now:%Y-%m-%d %H:%M:%S %Z}] 已到盘中结束时间 16:00 ET，盘中监控不启动。", flush=True)
        return

    run_lock = acquire_run_lock(settings.output_dir, "intraday_ma5_monitor.lock", "盘中 MA5 监控")
    market_data = None
    broker = None
    loop_count = 0
    try:
        notify_intraday_monitor_started(settings)
        market_data = build_market_data(settings)
        while True:
            now_et = now_provider()
            if is_intraday_monitor_finished(now_et):
                print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 盘中监控到达 16:00 ET，退出。", flush=True)
                break

            loop_count += 1
            broker = run_forever_once(settings, market_data, broker, now_et)
            if max_loops is not None and loop_count >= max_loops:
                print(f"已完成测试轮数 {max_loops}，退出。", flush=True)
                break

            sleep_now = now_provider()
            if is_intraday_monitor_finished(sleep_now):
                print(f"[{sleep_now:%Y-%m-%d %H:%M:%S %Z}] 盘中监控到达 16:00 ET，退出。", flush=True)
                break
            sleep_seconds = min(next_poll_seconds(settings, sleep_now), seconds_until_intraday_monitor_end(sleep_now))
            if sleep_seconds <= 0:
                print(f"[{sleep_now:%Y-%m-%d %H:%M:%S %Z}] 盘中监控到达 16:00 ET，退出。", flush=True)
                break
            print(f"下一轮：等待 {sleep_seconds} 秒后继续...", flush=True)
            sleep(sleep_seconds)
    finally:
        if market_data is not None and hasattr(market_data, "close"):
            market_data.close()
        run_lock.close()


def notify_intraday_monitor_started(settings: Settings) -> None:
    safe_send_openclaw_messages(
        settings,
        [render_intraday_monitor_start_message(settings)],
        context="intraday MA5 monitor started",
    )


def render_intraday_monitor_start_message(settings: Settings) -> str:
    return "\n".join(
        [
            "【盘中 MA5 监控启动】",
            "结论：开始盘中监控。",
            "动作：按策略检测买入/卖出信号；满足条件时会提交 Alpaca 订单，并发送订单提交与最终状态通知。",
            "",
            "监控配置",
            f"- 策略：{settings.strategy_name}",
            f"- 观察文件：{settings.watch_codes_file}",
            f"- 买入上限：今日最多 {settings.max_daily_buys} 支",
            f"- 单股金额：最多 ${settings.buy_notional_usd:.2f}",
            f"- 轮询频率：盘中每 {settings.regular_poll_seconds} 秒一轮",
            "",
            "风控规则",
            f"- 单笔订单超时：{settings.order_cancel_after_seconds} 秒未完全成交会请求撤单",
            f"- 同一股票下单错误上限：{settings.max_symbol_order_errors} 次",
        ]
    )


def run_forever_once(settings: Settings, market_data, broker, now_et: datetime):
    """包住单轮监控，避免普通异常把常驻进程打停。"""
    try:
        broker = broker or build_broker(settings)
        run_once(settings, market_data=market_data, broker=broker, now=now_et)
        return broker
    except Exception as exc:
        print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 本轮失败，继续等待下一轮：{short_error(exc)}")
        return None


def print_run_header(now_et: datetime, watch_codes: list[str], broker_name: str) -> None:
    """打印本轮监控概览，长观察池也保持可读。"""
    print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 开始检查")
    print(f"观察数量：{len(watch_codes)} | 交易通道：{broker_name}")


def print_summary(summary: dict[str, int]) -> None:
    """按固定顺序打印统计，方便快速确认买入、卖出和错误数量。"""
    print(
        "本轮完成："
        f"观察 {summary['watch']} | "
        f"买入 {summary['buy']} | "
        f"卖出 {summary['sell']} | "
        f"持有/跳过 {summary['hold']} | "
        f"错误 {summary['errors']}"
    )


def monitor_row(
    symbol: str,
    action: str,
    snapshot: MarketSnapshot | None = None,
    signal: Signal | None = None,
    *,
    order_status: str = "",
    order_price: float = 0.0,
    reason: str = "",
) -> MonitorTableRow:
    selected_reason = reason or (signal.reason if signal else "")
    selected_price = order_price or signal_price(signal)
    if snapshot is None:
        return MonitorTableRow(symbol=symbol, action=action, order_status=order_status, order_price=selected_price, reason=selected_reason)
    return MonitorTableRow(
        symbol=symbol,
        action=action,
        has_market_data=True,
        current_price=snapshot.current_price,
        today_open=snapshot.today_open,
        today_ma5=snapshot.today_ma5,
        today_open_ma5=snapshot.today_open_ma5,
        signal_gain_pct=snapshot.signal_day_gain_pct,
        current_gain_pct=snapshot.today_current_gain_pct,
        order_price=selected_price,
        order_status=order_status,
        reason=selected_reason,
    )


def signal_price(signal: Signal | None) -> float:
    if signal is None:
        return 0.0
    for key in ("final_buy_point", "stop_loss_limit_price", "take_profit_price"):
        try:
            value = float(signal.diagnostics.get(key, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return signal.current_price if signal.current_price > 0 else 0.0


def print_monitor_table(rows: list[MonitorTableRow]) -> None:
    if not rows:
        return
    print("本轮股票明细：")
    headers = ["代码", "动作", "当前价", "开盘", "MA5", "开盘MA5", "信号涨幅", "当前涨幅", "买/卖点", "订单", "原因"]
    table = [
        [
            row.symbol,
            row.action,
            _format_price(row.current_price),
            _format_price(row.today_open),
            _format_price(row.today_ma5),
            _format_price(row.today_open_ma5),
            _format_pct(row.signal_gain_pct) if row.has_market_data else "-",
            _format_pct(row.current_gain_pct) if row.has_market_data else "-",
            _format_price(row.order_price),
            row.order_status or "-",
            _reason_text(row.reason),
        ]
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for row in table:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print(_format_table_line(headers, widths))
    print(_format_table_line(["-" * width for width in widths], widths))
    for row in table:
        print(_format_table_line(row, widths))


def _format_table_line(values: list[str], widths: list[int]) -> str:
    cells = []
    for value, width in zip(values, widths):
        cells.append(value.ljust(width))
    return " | ".join(cells)


def _reason_text(reason: str) -> str:
    return " ".join(str(reason or "-").split())


def print_signal(reason: str, symbol: str, snapshot: MarketSnapshot) -> None:
    """打印策略判断和核心价格，便于直接在 PyCharm 控制台核对。"""
    print(
        "  判断："
        f"{reason} | 当前价 {_format_price(snapshot.current_price)} | "
        f"今日开盘 {_format_price(snapshot.today_open)} | "
        f"开盘MA5 {_format_price(snapshot.today_open_ma5)} | "
        f"开盘偏离 {_format_pct(snapshot.today_open_vs_open_ma5_pct) if snapshot.today_open_ma5 > 0 else '未知'} | "
        f"今日动态MA5 {_format_price(snapshot.today_ma5)} | "
        f"信号日涨幅 {_format_pct(snapshot.signal_day_gain_pct)} | "
        f"当前涨幅 {_format_pct(snapshot.today_current_gain_pct)} | "
        f"开盘涨幅 {_format_pct(snapshot.today_open_gain_pct) if snapshot.today_open > 0 else '未知'}"
    )


def print_snapshot(snapshot: MarketSnapshot) -> None:
    """打印参与 MA5 计算的原始行情数字。"""
    closes = ", ".join(f"{close:.4f}" for close in snapshot.previous_closes[-4:])
    opens = ", ".join(f"{open_price:.4f}" for open_price in snapshot.previous_opens[-4:]) or "不足4日"
    print(f"\n[{_format_snapshot_time(snapshot.as_of)}] {snapshot.symbol}")
    print(
        "  行情："
        f"当前价 {_format_price(snapshot.current_price)}（来源：{_format_source(snapshot.current_price_source)}） | "
        f"今日开盘 {_format_price(snapshot.today_open)}（来源：{_format_source(snapshot.today_open_source)}）"
    )
    print(
        "  均线："
        f"前4日收盘 [{closes}] + 当前价 {_format_price(snapshot.current_price)} "
        f"=> 今日动态MA5 {_format_price(snapshot.today_ma5)}"
    )
    print(
        "  开盘："
        f"前4日开盘 [{opens}] + 今日开盘 {_format_price(snapshot.today_open)} "
        f"=> 开盘MA5 {_format_price(snapshot.today_open_ma5)} | "
        f"开盘偏离 {_format_pct(snapshot.today_open_vs_open_ma5_pct) if snapshot.today_open_ma5 > 0 else '未知'}"
    )
    print(
        "  买点输入："
        f"信号日涨幅 {_format_pct(snapshot.signal_day_gain_pct)} | "
        f"当前涨幅 {_format_pct(snapshot.today_current_gain_pct)} | "
        f"当天开盘涨幅 {_format_pct(snapshot.today_open_gain_pct) if snapshot.today_open > 0 else '未知'}"
    )


def print_order(status: str, message: str, symbol: str, quantity: float, price: float) -> None:
    """打印订单结果摘要，避免必须打开 CSV 才能知道发生了什么。"""
    print(f"  订单：{symbol} | 状态 {status} | 数量 {quantity:.6f} | 参考价 {_format_price(price)} | {message}")


def _format_price(value: float) -> str:
    return f"{value:.4f}" if value > 0 else "未知"


def _format_source(source: str) -> str:
    return source or "未知"


def _format_snapshot_time(value: datetime) -> str:
    display_time = value.astimezone(DISPLAY_TIMEZONE) if value.tzinfo else value.replace(tzinfo=DISPLAY_TIMEZONE)
    return f"{display_time:%Y-%m-%d %H:%M:%S} {display_time.tzname()}"


def _format_pct(value: float) -> str:
    return f"{value:.2%}"
