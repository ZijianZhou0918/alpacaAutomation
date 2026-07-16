from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.afterhours_high_low import (
    afterhours_signal_day,
    afterhours_watch_codes_path,
    read_afterhours_watch_metadata,
)
from alpaca_ma5_service.afterhours_monitor import AFTERHOURS_RANGE_RATIO_THRESHOLD, generate_afterhours_monitor_stocks
from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.daily_report import send_daily_monitor_report
from alpaca_ma5_service.market_time import DAILY_BAR_READY, REALTIME_ORDER_CLOSE, REGULAR_CLOSE, REGULAR_OPEN
from alpaca_ma5_service.monitor_runtime import monitor_runtime
from alpaca_ma5_service.premarket_watchlist import premarket_watch_codes_path
from alpaca_ma5_service.run_lock import acquire_run_lock
from alpaca_ma5_service.trading_calendar import latest_trading_day_on_or_before
from alpaca_ma5_service.watchlist import read_watch_codes
from monitor_afterhours import monitor_afterhours
from monitor_ma5_forever import monitor_ma5_forever
from monitor_premarket_ma5 import monitor_premarket_ma5
from watchcode_ma5 import generate_ma5_watchcode
from watchcode_premarket import generate_premarket_watchcode


class TeeStream:
    """Write monitor output to both the visible console and a log file."""

    def __init__(self, *streams: TextIO):
        self.streams = streams
        self.encoding = getattr(streams[0], "encoding", "utf-8") if streams else "utf-8"

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)


def configure_console_logging() -> None:
    """Keep one Python window visible while still writing durable log files."""
    settings = build_settings()
    log_dir = settings.output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    now_et = datetime.now(ZoneInfo(settings.market_timezone))
    stamp = now_et.strftime("%Y%m%d")
    out_path = log_dir / f"monitor_auto_{stamp}.out.log"
    err_path = log_dir / f"monitor_auto_{stamp}.err.log"

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    out_file = out_path.open("a", encoding="utf-8", buffering=1)
    err_file = err_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = TeeStream(sys.stdout, out_file)
    sys.stderr = TeeStream(sys.stderr, err_file)
    print(f"自动监控日志：{out_path}", flush=True)
    print(f"自动监控错误日志：{err_path}", flush=True)


def monitor_auto(*, now_provider=None, sleep=time.sleep) -> None:
    """Single scheduled entrypoint: prepare watchcodes, then run the active session monitor."""
    settings = build_settings()
    now_provider = now_provider or (lambda: datetime.now(ZoneInfo(settings.market_timezone)))
    with monitor_runtime(settings.output_dir, "monitor_auto", "auto"):
        run_lock = acquire_run_lock(settings.output_dir, "auto_ma5_monitor.lock", "自动 MA5 监控入口")
        try:
            while True:
                now_et = now_provider()
                now_time = now_et.time()

                if now_time < REGULAR_OPEN:
                    ensure_premarket_watchcode(now_et)
                    print("当前处于盘前准备/盘前时段，进入盘前推荐监控。", flush=True)
                    monitor_premarket_ma5(sleep=sleep, now_provider=now_provider)
                    continue

                if now_time < REGULAR_CLOSE:
                    ensure_intraday_watchcode(now_et)
                    print("当前处于盘中时段，进入盘中 MA5 监控。", flush=True)
                    monitor_ma5_forever()
                    continue

                if now_time < REALTIME_ORDER_CLOSE:
                    ensure_afterhours_watchcode(now_et)
                    print("当前处于盘后时段，进入盘后 high/low 监控。", flush=True)
                    monitor_afterhours(sleep=sleep, now_provider=now_provider, stop_at_afterhours_end=True)
                    send_daily_monitor_report(settings, now_et=now_provider())
                    return

                print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 已到 20:00 ET，盘前/盘中/盘后自动监控入口退出。", flush=True)
                send_daily_monitor_report(settings, now_et=now_et)
                return
        finally:
            run_lock.close()


def ensure_current_session_watchcode(now_et: datetime) -> str:
    """Prepare the watchcode for the session that monitor_auto will enter."""
    now_time = now_et.time()
    if now_time < REGULAR_OPEN:
        ensure_premarket_watchcode(now_et)
        return "premarket"
    if now_time < REGULAR_CLOSE:
        ensure_intraday_watchcode(now_et)
        return "intraday"
    if now_time < REALTIME_ORDER_CLOSE:
        ensure_afterhours_watchcode(now_et)
        return "afterhours"
    print("当前已到 20:00 ET，本次启动不再生成 WatchCode。", flush=True)
    return "closed"


def ensure_intraday_watchcode(now_et: datetime) -> None:
    settings = build_settings()
    watch_path = settings.watch_codes_file
    if watchcode_ready_for_session(watch_path, now_et):
        print(f"盘中 watchcode 已就绪：{watch_path}", flush=True)
        return
    print(f"盘中 watchcode 缺失或过期，开始生成：{watch_path}", flush=True)
    generate_ma5_watchcode()


def ensure_premarket_watchcode(now_et: datetime) -> None:
    settings = build_settings()
    watch_path = premarket_watch_codes_path(settings)
    if watchcode_ready_for_session(watch_path, now_et):
        print(f"盘前 watchcode 已就绪：{watch_path}", flush=True)
        return
    print(f"盘前 watchcode 缺失或过期，开始生成：{watch_path}", flush=True)
    generate_premarket_watchcode()


def ensure_afterhours_watchcode(now_et: datetime) -> None:
    settings = build_settings()
    watch_path = afterhours_watch_codes_path(settings)
    if afterhours_watchcode_ready_for_session(watch_path, now_et):
        print(f"盘后 watchcode 已就绪：{watch_path}", flush=True)
        return
    print(f"盘后 watchcode 缺失或过期，开始生成：{watch_path}", flush=True)
    generate_afterhours_monitor_stocks(settings=settings, now_et=now_et)


def watchcode_ready_for_session(path: Path, now_et: datetime) -> bool:
    return watchcode_ready_for_signal_date(path, expected_signal_date(now_et))


def watchcode_ready_for_signal_date(path: Path, expected_signal_date_value) -> bool:
    if not path.exists():
        return False
    if not read_watch_codes(path):
        return False
    signal_date = read_watchcode_signal_date(path)
    return signal_date == expected_signal_date_value


def afterhours_watchcode_ready_for_session(path: Path, now_et: datetime) -> bool:
    if not path.exists():
        return False
    try:
        signal_day, threshold, _symbols = read_afterhours_watch_metadata(path)
    except Exception:
        return False
    if signal_day != afterhours_signal_day(now_et):
        return False
    if threshold is None:
        return False
    return math.isclose(threshold, AFTERHOURS_RANGE_RATIO_THRESHOLD, rel_tol=0, abs_tol=1e-9)


def read_watchcode_signal_date(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[:10]
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("# signal_date="):
            raw = line.split("=", 1)[1].strip()
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


def expected_signal_date(now_et: datetime):
    candidate = now_et.date() if now_et.time() >= DAILY_BAR_READY else now_et.date() - timedelta(days=1)
    return latest_trading_day_on_or_before(candidate)


if __name__ == "__main__":
    configure_console_logging()
    monitor_auto()
