try:
    from ._bootstrap import ensure_local_venv
except ImportError:
    from _bootstrap import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.errors import short_error
from alpaca_ma5_service.market_data import build_realtime_price_source
from alpaca_ma5_service.market_time import now_market_time, regular_open_has_started
from alpaca_ma5_service.moomoo_market_data import MoomooRealtimePriceSource
from alpaca_ma5_service.watchlist import read_watch_codes


def run_test_watch_realtime_prices() -> None:
    """读取 watch_codes.txt，逐只测试真实监控使用的 Moomoo 实时价。"""
    settings = build_settings()
    watch_codes = read_watch_codes(settings.watch_codes_file)

    print("=== 观察池 Moomoo 实时价测试 ===", flush=True)
    print(f"观察文件：{settings.watch_codes_file}", flush=True)
    print(f"观察数量：{len(watch_codes)}", flush=True)
    print("说明：真实监控链路使用同一个 Moomoo 实时价源", flush=True)
    now_et = now_market_time(settings)
    regular_open_started = regular_open_has_started(now_et)

    if not watch_codes:
        print("观察文件为空。", flush=True)
        print("=== 完成 ===", flush=True)
        return

    realtime_source = build_realtime_price_source(settings)
    try:
        if not isinstance(realtime_source, MoomooRealtimePriceSource):
            raise RuntimeError(f"Monitor realtime source is not Moomoo: REALTIME_PRICE_SOURCE={settings.realtime_price_source}")

        for symbol in watch_codes:
            try:
                # 使用与真实监控完全相同的实时价源，方便对照控制台输出。
                quote = realtime_source.latest_price_quote(symbol)
                today_open = f"{quote.today_open:.4f}" if regular_open_started and quote.today_open > 0 else "未知"
                today_open_source = quote.today_open_source if regular_open_started and quote.today_open > 0 else "未知"
                print(
                    f"{symbol}：当前价 {quote.price:.4f}（来源：{quote.source}） | "
                    f"今日开盘 {today_open}（来源：{today_open_source}）",
                    flush=True,
                )
            except Exception as exc:
                print(f"{symbol}：Moomoo 实时价读取失败，已跳过。{short_error(exc)}", flush=True)
    finally:
        if hasattr(realtime_source, "close"):
            realtime_source.close()

    print("=== 完成 ===", flush=True)


if __name__ == "__main__":
    run_test_watch_realtime_prices()
