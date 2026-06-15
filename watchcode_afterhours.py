from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.afterhours_monitor import generate_afterhours_monitor_stocks


def generate_afterhours_watchcode() -> None:
    """Click-run entry: generate watch_code_afterhours.txt."""
    generate_afterhours_monitor_stocks()


if __name__ == "__main__":
    generate_afterhours_watchcode()
