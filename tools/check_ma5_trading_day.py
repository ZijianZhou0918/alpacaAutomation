from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from alpaca_ma5_service.trading_calendar import latest_trading_day_on_or_before, trading_day_decision


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--latest-on-or-before":
        if len(sys.argv) < 3 or not sys.argv[2].strip():
            raise SystemExit("--latest-on-or-before requires YYYY-MM-DD")
        target_date = datetime.strptime(sys.argv[2].strip(), "%Y-%m-%d").date()
        print(latest_trading_day_on_or_before(target_date).isoformat(), flush=True)
        return 0

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
