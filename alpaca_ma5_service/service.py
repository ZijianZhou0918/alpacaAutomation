from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .broker import AlpacaStockBroker
from .config import Settings, build_settings
from .errors import short_error
from .market_data import AlpacaMarketData
from .market_time import next_poll_seconds, now_market_time
from .models import MarketSnapshot
from .state import count_today_buy_orders
from .strategy import evaluate_buy, evaluate_sell
from .watchlist import read_watch_codes


def build_broker(settings: Settings):
    """构建真实 Alpaca broker；测试时可以传入 fake broker 替代。"""
    return AlpacaStockBroker(settings)


def build_market_data(settings: Settings):
    """构建默认 Alpaca 行情源；测试时可以传入 fake market_data 替代。"""
    return AlpacaMarketData(settings.market_timezone)


def run_once(settings: Settings | None = None, market_data=None, broker=None, now: datetime | None = None) -> dict[str, int]:
    """执行一轮盯盘：读取 watchlist、评估买卖信号、提交订单并汇总结果。"""
    settings = settings or build_settings()
    market_data = market_data or build_market_data(settings)
    broker = broker or build_broker(settings)
    now_et = now or now_market_time(settings)

    watch_codes = read_watch_codes(settings.watch_codes_file)
    if not watch_codes:
        print(f"watch_codes 文件为空或不存在：{settings.watch_codes_file}")
        return {"watch": 0, "buy": 0, "sell": 0, "hold": 0, "errors": 0}

    # 用户要求只针对文件 watch code 盯盘，所以持仓也只检查当前文件里的代码。
    watch_set = set(watch_codes)
    positions = {symbol: pos for symbol, pos in broker.get_positions().items() if symbol in watch_set}
    buys_used = count_today_buy_orders(settings.output_dir)
    summary = {"watch": len(watch_codes), "buy": 0, "sell": 0, "hold": 0, "errors": 0}

    print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 开始检查 watch_codes={watch_codes} | broker={broker.source_name()}")
    for symbol in watch_codes:
        try:
            snapshot: MarketSnapshot = market_data.get_snapshot(symbol)
            print_snapshot(snapshot)
            position = positions.get(symbol)
            if position:
                signal = evaluate_sell(position, snapshot, now_et, settings)
                print_signal(signal.reason, symbol, snapshot)
                if signal.action == "SELL_ALL":
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
                print(f"{symbol}: 今日买入次数已达上限 {settings.max_daily_buys}，跳过买入检查")
                summary["hold"] += 1
                continue

            signal = evaluate_buy(snapshot)
            print_signal(signal.reason, symbol, snapshot)
            if signal.action == "BUY":
                result = broker.place_market_buy(symbol, settings.buy_notional_usd, snapshot.current_price, signal.reason)
                print_order(result.status, result.message, symbol, result.quantity, result.price)
                if order_executed(result.status):
                    buys_used += 1
                    summary["buy"] += 1
                else:
                    summary["hold"] += 1
            else:
                summary["hold"] += 1
        except Exception as exc:
            summary["errors"] += 1
            print(f"{symbol}: 检查失败，已跳过。{type(exc).__name__}: {exc}")

    print(f"本轮完成：{summary}")
    return summary


def order_executed(status: str) -> bool:
    """只有真实成交或 dry-run 成功才计入买/卖成功，撤单不算成功。"""
    return status.upper() in {"FILLED", "DRY_RUN"}


def run_forever(settings: Settings | None = None) -> None:
    """常驻盯盘入口；每轮出错只记录原因，然后继续下一轮。"""
    settings = settings or build_settings()
    market_data = build_market_data(settings)
    broker = None
    while True:
        now_et = datetime.now(ZoneInfo(settings.market_timezone))
        broker = run_forever_once(settings, market_data, broker, now_et)
        sleep_seconds = next_poll_seconds(settings, now_et)
        print(f"等待 {sleep_seconds} 秒后继续...")
        time.sleep(sleep_seconds)


def run_forever_once(settings: Settings, market_data, broker, now_et: datetime):
    """run_forever 的安全单轮包装；普通异常不会让常驻程序退出。"""
    try:
        broker = broker or build_broker(settings)
        run_once(settings, market_data=market_data, broker=broker, now=now_et)
        return broker
    except Exception as exc:
        print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 本轮失败，继续等待下一轮。{short_error(exc)}")
        return None


def print_signal(reason: str, symbol: str, snapshot: MarketSnapshot) -> None:
    """打印策略判断结果，方便 PyCharm 控制台直接查看。"""
    print(f"{symbol}: {reason} | current={snapshot.current_price:.4f} today_ma5={snapshot.today_ma5:.4f}")


def print_snapshot(snapshot: MarketSnapshot) -> None:
    """打印策略使用的行情原始数字，方便核对 MA5。"""
    closes = ", ".join(f"{close:.4f}" for close in snapshot.previous_closes[-4:])
    print(f"{snapshot.symbol}: current_price={snapshot.current_price:.4f} previous_4_closes=[{closes}] today_ma5={snapshot.today_ma5:.4f}")


def print_order(status: str, message: str, symbol: str, quantity: float, price: float) -> None:
    """打印订单提交/拒单结果，避免用户必须先打开 CSV。"""
    print(f"{symbol}: order_status={status} qty={quantity} price={price:.4f} | {message}")
