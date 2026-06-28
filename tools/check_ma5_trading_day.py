from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from alpaca_ma5_service.trading_calendar import trading_day_decision


def main() -> int:
    target_date = parse_target_date()
    decision = trading_day_decision(target_date)
    print(
        "target_date={0} is_trading_day={1} source={2} reason={3} open={4} close={5}".format(
            decision.target_date.isoformat(),
            str(decision.is_trading_day).lower(),
            decision.source,
            decision.reason,
            decision.open_time or "-",
            decision.close_time or "-",
        ),
        flush=True,
    )
    return 0 if decision.is_trading_day else 2


def parse_target_date() -> date:
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        return datetime.strptime(sys.argv[1].strip(), "%Y-%m-%d").date()
    return date.today() + timedelta(days=1)


if __name__ == "__main__":
    raise SystemExit(main())
