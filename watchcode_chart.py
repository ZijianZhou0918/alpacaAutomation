"""Public click-run entry for WatchCode chart refresh."""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.workflows.watchcode import chart as _workflow


if __name__ == "__main__":
    _workflow.refresh_current_watchcode_chart()
else:
    sys.modules[__name__] = _workflow
