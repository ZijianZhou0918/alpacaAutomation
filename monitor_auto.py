"""全天自动时段路由的公开点击入口；实现位于 workflows/monitoring/auto.py。"""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.workflows.monitoring import auto as _workflow


if __name__ == "__main__":
    _workflow.configure_console_logging()
    _workflow.monitor_auto()
else:
    sys.modules[__name__] = _workflow
