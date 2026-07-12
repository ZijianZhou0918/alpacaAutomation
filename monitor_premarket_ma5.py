from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.premarket_monitor import run_premarket_recommendations_forever


# 点运行箭头只改这里。
REALTIME_PRICE_SOURCE = "moomoo"
TRADE_NOTIFY_MODE = "cloud"


def monitor_premarket_ma5(*, max_loops: int | None = None, sleep=None, now_provider=None) -> None:
    """Click-run entry: monitor premarket MA5 recommendations without placing orders."""
    settings = build_settings(
        realtime_price_source=REALTIME_PRICE_SOURCE,
        trade_notify_mode=TRADE_NOTIFY_MODE,
    )
    kwargs = {"settings": settings, "max_loops": max_loops, "now_provider": now_provider}
    if sleep is not None:
        kwargs["sleep"] = sleep
    run_premarket_recommendations_forever(**kwargs)


if __name__ == "__main__":
    monitor_premarket_ma5()
