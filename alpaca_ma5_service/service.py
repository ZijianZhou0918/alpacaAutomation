from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .broker import AlpacaStockBroker
from .config import Settings, build_settings
from .errors import short_error
from .market_data import build_market_data as build_default_market_data
from .market_time import is_buy_order_time, is_premarket_time, is_realtime_order_time, next_poll_seconds, now_market_time
from .models import MarketSnapshot, Signal, consumes_daily_buy_slot, is_executed_order_status
from .state import append_daily_buy_exclusion, count_today_buy_orders, count_today_symbol_order_errors, count_today_symbol_take_profit_half_sells, is_symbol_daily_buy_excluded
from .strategy import evaluate_buy, evaluate_sell, evaluate_stop_loss
from .watchlist import read_watch_codes


DISPLAY_TIMEZONE = ZoneInfo("America/Los_Angeles")


@dataclass
class StopLossSession:
    """记录本监控进程启动时已有的亏损持仓，避免启动瞬间误清旧仓。"""
    initial_symbols: set[str]
    checked_initial_symbols: set[str] = field(default_factory=set)
    grandfathered_positions: dict[str, tuple[float, float]] = field(default_factory=dict)


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

        market_data = market_data or build_market_data(settings)
        market_data_started = True
        summary = {"watch": len(monitor_codes), "buy": 0, "sell": 0, "hold": 0, "errors": 0}
        buys_used = count_today_buy_orders(settings.output_dir, now_et.date())
        buy_notional, notional_note = buy_notional_for_run(settings, broker)
        buying_paused_for_run = False
        print_run_header(now_et, monitor_codes, broker.source_name())
        if buys_used < settings.max_daily_buys and notional_note:
            print(f"本轮买入金额：{notional_note}")
        for symbol in monitor_codes:
            try:
                position = positions.get(symbol)
                if not position and should_skip_symbol_after_order_errors(settings, symbol, now_et):
                    summary["hold"] += 1
                    continue
                if not position and symbol in watch_set:
                    if should_skip_symbol_after_daily_buy_exclusion(settings, symbol, now_et):
                        summary["hold"] += 1
                        continue
                    if buying_paused_for_run:
                        print(f"\n[{_format_snapshot_time(now_et)}] {symbol}")
                        print("  跳过：上一笔买单仍有未确认风险，本轮不再继续买入")
                        summary["hold"] += 1
                        continue
                    if buys_used >= settings.max_daily_buys:
                        print(f"\n[{_format_snapshot_time(now_et)}] {symbol}")
                        print(f"  跳过：今日买入次数已达上限 {settings.max_daily_buys}，不再检查买入")
                        summary["hold"] += 1
                        continue
                snapshot: MarketSnapshot = market_data.get_snapshot(symbol)
                print_snapshot(snapshot)
                if position:
                    signal = evaluate_sell(position, snapshot, now_et, settings) if symbol in watch_set else evaluate_stop_loss(position, snapshot, settings)
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
                    if symbol in watch_set and signal.action == "SELL_HALF" and take_profit_half_already_done(settings, symbol, now_et):
                        signal = Signal(
                            symbol,
                            "HOLD",
                            f"今日已执行过 {_format_pct(settings.take_profit_half_pct)} 半仓止盈，不重复卖出",
                            snapshot.current_price,
                            diagnostics=signal.diagnostics,
                        )
                    print_signal(signal.reason, symbol, snapshot)
                    if signal.action in {"SELL_ALL", "SELL_HALF"}:
                        if not can_order_now:
                            print("  跳过：当前不在实时价下单时段，不提交真实卖单")
                            summary["hold"] += 1
                            continue
                        if is_stop_loss_signal(signal):
                            limit_price = stop_loss_limit_price_from_signal(signal)
                            if limit_price <= 0:
                                print("  跳过：止损限价无效，不提交卖单")
                                summary["hold"] += 1
                                continue
                            print(f"  下单：SELL LIMIT | 限价 {_format_price(limit_price)} | 数量 {signal.quantity:.6f}")
                            result = broker.place_limit_sell(symbol, signal.quantity, limit_price, signal.reason)
                        else:
                            result = broker.place_market_sell(symbol, signal.quantity, snapshot.current_price, signal.reason)
                        print_order(result.status, result.message, symbol, result.quantity, result.price)
                        if order_executed(result.status):
                            summary["sell"] += 1
                        else:
                            summary["hold"] += 1
                    else:
                        summary["hold"] += 1
                    continue

                if symbol not in watch_set:
                    summary["hold"] += 1
                    continue

                signal = evaluate_buy(snapshot)
                print_signal(signal.reason, symbol, snapshot)
                if should_record_daily_buy_exclusion(signal):
                    append_daily_buy_exclusion(settings.output_dir, symbol, signal.reason, now_et.date(), now_et)
                    print("  记录：该股票今日已排除，后续本日不再检查买入")
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
                        print(f"  跳过：{reason}")
                        summary["hold"] += 1
                        continue
                    limit_price = buy_limit_price_from_signal(signal)
                    if limit_price <= 0:
                        print("  跳过：买点价格无效，不提交买单")
                        summary["hold"] += 1
                        continue
                    if buy_notional <= 0:
                        print("  跳过：本轮买入金额无效，不提交买单")
                        summary["hold"] += 1
                        continue
                    print(f"  下单：BUY LIMIT | 限价 {_format_price(limit_price)} | 金额 ${buy_notional:.2f}")
                    result = broker.place_limit_buy(symbol, buy_notional, limit_price, signal.reason)
                    print_order(result.status, result.message, symbol, result.quantity, result.price)
                    if order_executed(result.status):
                        buys_used += 1
                        summary["buy"] += 1
                    else:
                        if consumes_daily_buy_slot(result.status):
                            buys_used += 1
                            buying_paused_for_run = True
                        summary["hold"] += 1
                else:
                    summary["hold"] += 1
            except Exception as exc:
                summary["errors"] += 1
                print(f"\n[{_format_snapshot_time(now_et)}] {symbol}")
                print(f"  错误：检查失败，已跳过。{type(exc).__name__}: {exc}")
    finally:
        if created_market_data and market_data_started and hasattr(market_data, "close"):
            market_data.close()

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


def buy_notional_for_run(settings: Settings, broker) -> tuple[float, str]:
    """本轮开始时固定每只股票的买入金额，后续订单不再重算。"""
    slots = max(1, settings.max_daily_buys)
    cash = broker_cash(broker)
    if cash is None:
        return settings.buy_notional_usd, f"无法读取 Alpaca cash，按配置金额 ${settings.buy_notional_usd:.2f} 下单"
    if cash <= 0:
        return 0.0, f"Alpaca cash ${cash:.2f}，不提交买单"
    notional = cash / slots
    return notional, f"Alpaca cash ${cash:.2f} / {slots} = 每只 ${notional:.2f}"


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
    return signal.action == "SELL_ALL" and signal.diagnostics.get("sell_rule") == "stop_loss"


def position_fingerprint(position) -> tuple[float, float]:
    return (round(float(position.quantity), 6), round(float(position.avg_price), 6))


def should_skip_symbol_after_order_errors(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """同一股票当天拒单达到阈值后，只停止继续买入，不影响已有持仓卖出。"""
    if settings.max_symbol_order_errors <= 0:
        return False
    error_count = count_today_symbol_order_errors(settings.output_dir, symbol, now_et.date())
    if error_count < settings.max_symbol_order_errors:
        return False
    print(f"\n[{_format_snapshot_time(now_et)}] {symbol}")
    print(f"  跳过：今日下单错误已达 {error_count}/{settings.max_symbol_order_errors} 次，不再提交该股票订单")
    return True


def should_skip_symbol_after_daily_buy_exclusion(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """当天触达 MA5 但未跌到 18% 的股票，后续不再检查买入。"""
    if not is_symbol_daily_buy_excluded(settings.output_dir, symbol, now_et.date()):
        return False
    print(f"\n[{_format_snapshot_time(now_et)}] {symbol}")
    print("  跳过：今日已触达动态MA5但跌幅未到 18%，当天不再考虑买入")
    return True


def should_record_daily_buy_exclusion(signal: Signal) -> bool:
    return signal.diagnostics.get("daily_buy_exclusion") == "ma5_touch_without_18_percent_drop"


def take_profit_half_already_done(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """同一股票当天只执行一次 10% 半仓止盈。"""
    return count_today_symbol_take_profit_half_sells(settings.output_dir, symbol, now_et.date()) > 0


def run_forever(settings: Settings | None = None) -> None:
    """常驻监控入口；单轮异常会记录并继续下一轮。"""
    settings = settings or build_settings()
    market_data = build_market_data(settings)
    broker = None
    while True:
        now_et = datetime.now(ZoneInfo(settings.market_timezone))
        broker = run_forever_once(settings, market_data, broker, now_et)
        sleep_now = datetime.now(ZoneInfo(settings.market_timezone))
        sleep_seconds = next_poll_seconds(settings, sleep_now)
        print(f"下一轮：等待 {sleep_seconds} 秒后继续...")
        time.sleep(sleep_seconds)


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
    print(f"观察列表：{', '.join(watch_codes)}")


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
