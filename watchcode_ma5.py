"""Public click-run entry for intraday WatchCode generation."""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.workflows.watchcode import intraday as _workflow


if __name__ == "__main__":
    _workflow.generate_ma5_watchcode()
else:
    sys.modules[__name__] = _workflow
