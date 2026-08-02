"""Deprecated premarket WatchCode entry; premarket monitors positions only."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

def generate_premarket_watchcode() -> None:
    """Compatibility no-op: never screen or write a premarket stock pool."""
    print("盘前 WatchCode 已停用：盘前只监控 Alpaca 当前持仓，不筛选、不写入股票池。", flush=True)


if __name__ == "__main__":
    generate_premarket_watchcode()
