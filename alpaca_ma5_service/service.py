from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .broker import AlpacaStockBroker
from .config import Settings, build_settings
from .errors import short_error
from .market_data import build_market_data as build_default_market_data
from .market_time import is_buy_order_time, is_premarket_time, is_realtime_order_time, next_poll_seconds, now_market_time
from .models import MarketSnapshot, Signal, consumes_daily_buy_slot, is_executed_order_status
from .state import count_today_buy_orders, count_today_symbol_order_errors
from .strategy import evaluate_buy, evaluate_sell
from .watchlist import read_watch_codes


def build_broker(settings: Settings):
    """创建真实交易通道；单测会传入 fake broker 避免触碰 Alpaca。"""
    return AlpacaStockBroker(settings)


def build_market_data(settings: Settings):
    """创建默认行情源：Moomoo 负责实时价，Alpaca 负责日线。"""
    return build_default_market_data(settings)


def run_once(settings: Settings | None = None, market_data=None, broker=None, now: datetime | None = None) -> dict[str, int]:
    """执行一轮监控：只处理 watch_codes 文件里的股票，并返回本轮统计。"""
    settings = settings or build_settings()
    now_et = now or now_market_time(settings)
    watch_codes = read_watch_codes(settings.watch_codes_file)
    if not watch_codes:
        print(f"watch_codes 文件为空或不存在：{settings.watch_codes_file}")
        return {"watch": 0, "buy": 0, "sell": 0, "hold": 0, "errors": 0}

    created_market_data = market_data is None
    market_data = market_data or build_market_data(settings)
    broker = broker or build_broker(settings)

    summary = {"watch": len(watch_codes), "buy": 0, "sell": 0, "hold": 0, "errors": 0}
    can_order_now = is_realtime_order_time(now_et)
    can_buy_now = is_buy_order_time(now_et)

    try:
        # 监控范围以 watch_codes.txt 为准，持仓也只处理当前观察池里的股票。
        watch_set = set(watch_codes)
        positions = {symbol: pos for symbol, pos in broker.get_positions().items() if symbol in watch_set}
        buys_used = count_today_buy_orders(settings.output_dir, now_et.date())
        print_run_header(now_et, watch_codes, broker.source_name())
        for symbol in watch_codes:
            try:
                position = positions.get(symbol)
                if not position and should_skip_symbol_after_order_errors(settings, symbol, now_et):
                    summary["hold"] += 1
                    continue
                snapshot: MarketSnapshot = market_data.get_snapshot(symbol)
                print_snapshot(snapshot)
                if position:
                    signal = evaluate_sell(position, snapshot, now_et, settings)
                    print_signal(signal.reason, symbol, snapshot)
                    if signal.action == "SELL_ALL":
                        if not can_order_now:
                            print("  跳过：当前不在实时价下单时段，不提交真实卖单")
                            summary["hold"] += 1
                            continue
                        result = broker.place_market_sell(symbol, signal.quantity, snapshot.current_price, signal.reason)
                        print_order(result.status, result.message, symbol, result.quantity, result.price)
                        if order_executed(result.status):
                            summary["sell"] += 1
                        else:
                            summary["hold"] += 1
                    else:
                        summary["hold"] += 1
                    continue

                if buys_used >= settings.max_daily_buys:
                    print(f"  跳过：今日买入次数已达上限 {settings.max_daily_buys}，不再检查买入")
                    summary["hold"] += 1
                    continue

                signal = evaluate_buy(snapshot)
                print_signal(signal.reason, symbol, snapshot)
                if signal.action == "BUY":
                    if not can_buy_now:
                        reason = "盘前时段不买入，跳过真实买单" if is_premarket_time(now_et) else "当前不在实时价下单时段，跳过真实买单"
                        print(f"  跳过：{reason}")
                        summary["hold"] += 1
                        continue
                    limit_price = buy_limit_price_from_signal(signal)
                    if limit_price <= 0:
                        print("  跳过：买点价格无效，不提交买单")
                        summary["hold"] += 1
                        continue
                    print(f"  下单：BUY LIMIT | 限价 {_format_price(limit_price)} | 金额 ${settings.buy_notional_usd:.2f}")
                    result = broker.place_limit_buy(symbol, settings.buy_notional_usd, limit_price, signal.reason)
                    print_order(result.status, result.message, symbol, result.quantity, result.price)
                    if order_executed(result.status):
                        buys_used += 1
                        summary["buy"] += 1
                    else:
                        if consumes_daily_buy_slot(result.status):
                            buys_used += 1
                        summary["hold"] += 1
                else:
                    summary["hold"] += 1
            except Exception as exc:
                summary["errors"] += 1
                print(f"\n{symbol}")
                print(f"  错误：检查失败，已跳过。{type(exc).__name__}: {exc}")
    finally:
        if created_market_data and hasattr(market_data, "close"):
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


def should_skip_symbol_after_order_errors(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """同一股票当天拒单达到阈值后，只停止继续买入，不影响已有持仓卖出。"""
    if settings.max_symbol_order_errors <= 0:
        return False
    error_count = count_today_symbol_order_errors(settings.output_dir, symbol, now_et.date())
    if error_count < settings.max_symbol_order_errors:
        return False
    print(f"\n{symbol}")
    print(f"  跳过：今日下单错误已达 {error_count}/{settings.max_symbol_order_errors} 次，不再提交该股票订单")
    return True


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
        f"今日动态MA5 {_format_price(snapshot.today_ma5)} | "
        f"信号日涨幅 {_format_pct(snapshot.signal_day_gain_pct)} | "
        f"开盘涨幅 {_format_pct(snapshot.today_open_gain_pct) if snapshot.today_open > 0 else '未知'}"
    )


def print_snapshot(snapshot: MarketSnapshot) -> None:
    """打印参与 MA5 计算的原始行情数字。"""
    closes = ", ".join(f"{close:.4f}" for close in snapshot.previous_closes[-4:])
    print(f"\n{snapshot.symbol}")
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
        "  买点输入："
        f"信号日涨幅 {_format_pct(snapshot.signal_day_gain_pct)} | "
        f"当天开盘涨幅 {_format_pct(snapshot.today_open_gain_pct) if snapshot.today_open > 0 else '未知'}"
    )


def print_order(status: str, message: str, symbol: str, quantity: float, price: float) -> None:
    """打印订单结果摘要，避免必须打开 CSV 才能知道发生了什么。"""
    print(f"  订单：{symbol} | 状态 {status} | 数量 {quantity:.6f} | 参考价 {_format_price(price)} | {message}")


def _format_price(value: float) -> str:
    return f"{value:.4f}" if value > 0 else "未知"


def _format_source(source: str) -> str:
    return source or "未知"


def _format_pct(value: float) -> str:
    return f"{value:.2%}"
