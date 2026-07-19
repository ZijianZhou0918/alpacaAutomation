"""Public run entry for rebuilding the official daily-history database."""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from backtest.runners import run_backtest_daily_history_rebuild as _runner


if __name__ == "__main__":
    _runner.main()
else:
    sys.modules[__name__] = _runner
