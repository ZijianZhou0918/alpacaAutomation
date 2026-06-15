from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.service import run_once


def monitor_ma5_once() -> None:
    """Click-run entry: run one MA5 monitor check."""
    run_once(build_settings())


if __name__ == "__main__":
    monitor_ma5_once()
