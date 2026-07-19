"""Public click-run entry for the after-hours reminder monitor."""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.workflows.monitoring import afterhours as _workflow


if __name__ == "__main__":
    _workflow.monitor_afterhours()
else:
    sys.modules[__name__] = _workflow
