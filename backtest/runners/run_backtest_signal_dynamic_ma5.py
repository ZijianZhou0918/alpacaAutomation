"""Signal-day dynamic-MA5 backtest command implementation."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import BASE_DIR
from backtest.signal_dynamic_ma5 import (
    SignalDynamicMa5Config,
    run_signal_dynamic_ma5_backtest,
)
from backtest.paths import (
    OFFICIAL_DAILY_DB_PATH,
    SIGNAL_DYNAMIC_MA5_MINUTE_CACHE_PATH,
)


def main() -> None:
    result = run_signal_dynamic_ma5_backtest(
        SignalDynamicMa5Config(
            database_path=OFFICIAL_DAILY_DB_PATH,
            minute_cache_path=SIGNAL_DYNAMIC_MA5_MINUTE_CACHE_PATH,
            output_dir=(
                BASE_DIR / "backtest" / "output" / "signal_dynamic_ma5"
            ),
            start_date=None,
            end_date=None,
            min_signal_gain_pct=0.10,
            min_signal_body_pct=0.10,
            ma5_proximity_pct=0.0,
            min_intraday_drop_pct=0.15,
            profit_targets_pct=(0.05, 0.10, 0.15),
            stop_loss_pct=0.10,
            notional_per_trade=10_000.0,
            commission_per_order=0.0,
            slippage_bps=0.0,
            expected_feed="sip",
            expected_adjustment="split",
            minute_fetch_batch_size=100,
        )
    )
    summary = result.summary
    print("Signal dynamic-MA5 backtest finished.")
    print(f"Range: {summary.start_date} -> {summary.end_date}")
    print(
        "Signals / positive-gap candidates / MA5 triggers / trades: "
        f"{summary.signal_days:,} / {summary.positive_gap_days:,} / "
        f"{summary.dynamic_ma5_trigger_days:,} / {summary.trades:,}"
    )
    print(
        f"Wins / losses / flat: {summary.wins:,} / "
        f"{summary.losses:,} / {summary.flat:,}"
    )
    print(f"Win rate: {summary.win_rate:.2%}")
    print(f"Average / median return: {summary.average_return_pct:.2%} / "
          f"{summary.median_return_pct:.2%}")
    print(
        "Fixed-notional total PnL (not a capital-constrained portfolio): "
        f"${summary.fixed_notional_total_pnl:,.2f}"
    )
    print(f"Summary: {result.summary_json_path}")
    print(f"Trades: {result.trades_csv_path}")
    print(f"Report: {result.html_report_path}")


if __name__ == "__main__":
    main()
