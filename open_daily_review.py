"""Public click-run entry for the daily-review website."""

import sys

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.workflows.review import launcher as _workflow


if __name__ == "__main__":
    raise SystemExit(_workflow.main())
else:
    sys.modules[__name__] = _workflow
