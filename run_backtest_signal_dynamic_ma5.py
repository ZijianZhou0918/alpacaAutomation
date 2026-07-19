"""Public run entry for the signal-day dynamic-MA5 backtest."""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from backtest.runners import run_backtest_signal_dynamic_ma5 as _runner


if __name__ == "__main__":
    _runner.main()
else:
    sys.modules[__name__] = _runner
