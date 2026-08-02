from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from alpaca_ma5_service.config import BASE_DIR
from backtest.kdj_volume_reversal import (
    KdjVolumeReversalConfig,
    run_kdj_volume_reversal_backtest,
    write_backtest_outputs,
)
from backtest.paths import OFFICIAL_DAILY_DB_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 KDJ(81,3,3) 极端量价反转日线回测。")
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--database", type=Path, default=OFFICIAL_DAILY_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR / "backtest" / "output")
    parser.add_argument("--notional", type=float, default=2_500.0)
    parser.add_argument(
        "--allow-ma5-signal-day",
        action="store_true",
        help="不排除 close/MA5 >= 1.15 的原 MA5 信号日。",
    )
    args = parser.parse_args()
    result = run_kdj_volume_reversal_backtest(
        args.database,
        KdjVolumeReversalConfig(
            start_date=args.start_date,
            end_date=args.end_date,
            notional_per_trade=args.notional,
            require_no_ma5_signal=not args.allow_ma5_signal_day,
        ),
    )
    json_path, csv_path = write_backtest_outputs(result, args.output_dir)
    print(f"Signals: {result.raw_signal_count}")
    print(f"Ignored while already holding: {result.ignored_signal_while_holding}")
    print(f"Trades: {result.entered_trade_count} (closed={result.closed_trade_count}, marked_open={result.marked_open_trade_count})")
    print(f"Wins/Losses/Flat: {result.winning_trade_count}/{result.losing_trade_count}/{result.flat_trade_count}")
    print(f"P&L: ${result.pnl_total:,.2f}")
    print(f"Aggregate return: {result.aggregate_return_pct:.2f}%")
    print(f"Average/median trade return: {result.average_trade_return_pct:.2f}% / {result.median_trade_return_pct:.2f}%")
    print(f"JSON: {json_path}")
    print(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
