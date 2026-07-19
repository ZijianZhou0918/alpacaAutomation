"""盘中策略监控的公开点击入口；实现位于 workflows/monitoring/intraday.py。"""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.workflows.monitoring import intraday as _workflow


if __name__ == "__main__":
    _workflow.monitor_ma5_forever()
else:
    sys.modules[__name__] = _workflow
