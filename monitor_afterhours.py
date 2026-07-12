from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.afterhours_monitor import AFTERHOURS_REQUIRE_PAPER, monitor_afterhours_trades


def monitor_afterhours(
    *,
    max_loops: int | None = None,
    sleep=None,
    now_provider=None,
    stop_at_afterhours_end: bool = False,
) -> None:
    """Click-run entry: generate candidates and send afterhours alerts only."""
    kwargs = {
        "max_loops": max_loops,
        "now_provider": now_provider,
        "stop_at_afterhours_end": stop_at_afterhours_end,
    }
    if sleep is not None:
        kwargs["sleep"] = sleep
    monitor_afterhours_trades(require_paper=AFTERHOURS_REQUIRE_PAPER, **kwargs)


if __name__ == "__main__":
    monitor_afterhours()
