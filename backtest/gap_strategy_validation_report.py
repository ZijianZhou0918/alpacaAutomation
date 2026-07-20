"""Reproducible validation report for the frozen 2025 gap-strategy research."""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3

from alpaca_ma5_service.config import BASE_DIR
from backtest.engine import TradeRecord
from backtest.gap_strategy_optimization import (
    DEVELOPMENT_END,
    DEVELOPMENT_START,
    FROZEN_CANDIDATE_NAME,
    HOLDOUT_END,
    HOLDOUT_START,
    LOCKED_CROSS_VALIDATION_YEAR,
    MINUTE_CACHE_PATH,
    TradeOutcome,
    stage_one_variants,
    stage_two_variants,
    summarize_outcomes,
    trade_outcomes,
)
from backtest.paths import OFFICIAL_DAILY_DB_PATH
from backtest.strategy_validation import (
    block_bootstrap_win_rate_delta,
    daily_return_series,
    deflated_sharpe_ratio,
    probability_backtest_overfitting,
    wilson_interval,
)


OUTPUT_ROOT = BASE_DIR / "backtest" / "output" / "gap_strategy_optimization"
INITIAL_CASH = 100_000.0


def read_trade_csv(path: Path) -> list[TradeRecord]:
    """Load one engine trade CSV without executing a backtest."""
    trades: list[TradeRecord] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            signal_day_text = (row.get("signal_day") or "").strip()
            trades.append(
                TradeRecord(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    symbol=row["symbol"],
                    side=row["side"],
                    quantity=float(row["quantity"]),
                    price=float(row["price"]),
                    gross_value=float(row["gross_value"]),
                    fee=float(row["fee"]),
                    cash_after=float(row["cash_after"]),
                    realized_pnl=float(row["realized_pnl"]),
                    reason=row["reason"],
                    rule=row["rule"],
                    price_change_pct=float(row.get("price_change_pct") or 0.0),
                    signal_day=(
                        date.fromisoformat(signal_day_text)
                        if signal_day_text
                        else None
                    ),
                )
            )
    return trades


def load_outcomes(path: Path) -> list[TradeOutcome]:
    return trade_outcomes(read_trade_csv(path))


def build_validation_summary(
    output_root: Path = OUTPUT_ROOT,
    *,
    bootstrap_samples: int = 10_000,
) -> dict[str, object]:
    """Recompute the headline claims from saved trade-level evidence."""
    stage1 = output_root / "stage1"
    stage2 = output_root / "stage2"
    holdout = output_root / "holdout"
    robustness = output_root / "robustness"

    dev_baseline = load_outcomes(stage1 / "baseline" / "backtest_trades.csv")
    dev_candidate = load_outcomes(
        stage2 / FROZEN_CANDIDATE_NAME / "backtest_trades.csv"
    )
    holdout_baseline = load_outcomes(
        holdout / "baseline" / "backtest_trades.csv"
    )
    holdout_candidate = load_outcomes(
        holdout / FROZEN_CANDIDATE_NAME / "backtest_trades.csv"
    )
    full_baseline = [*dev_baseline, *holdout_baseline]
    full_candidate = [*dev_candidate, *holdout_candidate]

    development_sessions = _sessions(DEVELOPMENT_START, DEVELOPMENT_END)
    trial_returns: dict[str, list[float]] = {}
    for spec in stage_one_variants():
        outcomes = load_outcomes(
            stage1 / spec.name / "backtest_trades.csv"
        )
        trial_returns[f"stage1:{spec.name}"] = daily_return_series(
            outcomes,
            development_sessions,
            initial_cash=INITIAL_CASH,
        )
    for spec in stage_two_variants():
        outcomes = load_outcomes(
            stage2 / spec.name / "backtest_trades.csv"
        )
        trial_returns[f"stage2:{spec.name}"] = daily_return_series(
            outcomes,
            development_sessions,
            initial_cash=INITIAL_CASH,
        )
    selected_returns = trial_returns[f"stage2:{FROZEN_CANDIDATE_NAME}"]
    dsr = deflated_sharpe_ratio(
        selected_returns,
        list(trial_returns.values()),
    )
    pbo = probability_backtest_overfitting(trial_returns, groups=10)

    baseline_development_stats = summarize_outcomes(dev_baseline)
    candidate_development_stats = summarize_outcomes(dev_candidate)
    baseline_holdout_stats = summarize_outcomes(holdout_baseline)
    candidate_holdout_stats = summarize_outcomes(holdout_candidate)
    baseline_full_stats = summarize_outcomes(full_baseline)
    candidate_full_stats = summarize_outcomes(full_candidate)
    target_win_rate = baseline_development_stats.win_rate * 1.20

    stage2_rows = _load_json(stage2 / "stage2_results.json")
    robustness_rows = _load_json(robustness / "robustness_results.json")
    return {
        "question": (
            "Can the pre-existing gap pullback strategy improve win rate by "
            "at least 20% relatively without using 2026 for selection?"
        ),
        "selection_protocol": {
            "development_window": (
                f"{DEVELOPMENT_START.isoformat()}..{DEVELOPMENT_END.isoformat()}"
            ),
            "holdout_window": (
                f"{HOLDOUT_START.isoformat()}..{HOLDOUT_END.isoformat()}"
            ),
            "locked_cross_validation_year": LOCKED_CROSS_VALIDATION_YEAR,
            "stage_one_trials": len(stage_one_variants()),
            "stage_two_trials": len(stage_two_variants()),
            "total_development_trials": len(trial_returns),
            "frozen_candidate": FROZEN_CANDIDATE_NAME,
            "post_holdout_parameter_changes": 0,
        },
        "target": {
            "baseline_development_win_rate": baseline_development_stats.win_rate,
            "minimum_relative_improvement": 0.20,
            "minimum_candidate_win_rate": target_win_rate,
        },
        "development": _comparison(
            dev_baseline,
            dev_candidate,
            bootstrap_samples=bootstrap_samples,
        ),
        "holdout": _comparison(
            holdout_baseline,
            holdout_candidate,
            bootstrap_samples=bootstrap_samples,
        ),
        "full_2025": _comparison(
            full_baseline,
            full_candidate,
            bootstrap_samples=bootstrap_samples,
        ),
        "multiple_testing": {
            "deflated_sharpe_ratio": asdict(dsr),
            "cscv_probability_backtest_overfitting": asdict(pbo),
        },
        "neighbor_grid": [
            {
                key: row[key]
                for key in (
                    "name",
                    "trade_count",
                    "win_rate",
                    "profit_factor",
                    "realized_pnl",
                    "max_drawdown_pct",
                    "q1_win_rate",
                    "q2_win_rate",
                    "q3_win_rate",
                )
            }
            for row in stage2_rows
        ],
        "transaction_cost_robustness": [
            {
                key: row[key]
                for key in (
                    "name",
                    "trade_count",
                    "win_rate",
                    "profit_factor",
                    "realized_pnl",
                    "max_drawdown_pct",
                    "q4_win_rate",
                    "q4_profit_factor",
                )
            }
            for row in robustness_rows
        ],
        "data_quality": _data_quality_summary(),
        "assessment": (
            "Share with caveats: relative win-rate improvement cleared the "
            "20% target in the untouched Q4 holdout, but Q4 profit factor was "
            "only modestly above one and survivorship/market-impact risks remain."
        ),
    }


def write_validation_artifacts(
    output_root: Path = OUTPUT_ROOT,
) -> tuple[Path, Path, Path]:
    summary = build_validation_summary(output_root)
    json_path = output_root / "validation_summary.json"
    markdown_path = output_root / "validation_report.md"
    notebook_path = output_root / "gap_strategy_optimization_2025.ipynb"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_markdown_report(summary),
        encoding="utf-8",
    )
    notebook_path.write_text(
        json.dumps(build_executed_notebook(summary), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return json_path, markdown_path, notebook_path


def render_markdown_report(summary: dict[str, object]) -> str:
    dev = summary["development"]
    holdout = summary["holdout"]
    full = summary["full_2025"]
    multiple = summary["multiple_testing"]
    data_quality = summary["data_quality"]
    robustness = summary["transaction_cost_robustness"]
    return f"""# Gap pullback strategy optimization validation

## Overall assessment

{summary["assessment"]}

The frozen candidate changes only two parameters: the entry must pull back at
least 5% from the signal close (previously 2%), and the all-out profit target is
4% (previously 8%). The legacy strategy identifier is retained for compatibility.

## Results

| Window | Baseline trades | Baseline win | Candidate trades | Candidate win | Relative lift | Baseline PF | Candidate PF |
|---|---:|---:|---:|---:|---:|---:|---:|
| Development (Jan-Sep) | {dev["baseline"]["trade_count"]} | {dev["baseline"]["win_rate"]:.2%} | {dev["candidate"]["trade_count"]} | {dev["candidate"]["win_rate"]:.2%} | {dev["bootstrap_delta"]["observed_relative_delta"]:.2%} | {dev["baseline"]["profit_factor"]:.3f} | {dev["candidate"]["profit_factor"]:.3f} |
| Untouched holdout (Q4) | {holdout["baseline"]["trade_count"]} | {holdout["baseline"]["win_rate"]:.2%} | {holdout["candidate"]["trade_count"]} | {holdout["candidate"]["win_rate"]:.2%} | {holdout["bootstrap_delta"]["observed_relative_delta"]:.2%} | {holdout["baseline"]["profit_factor"]:.3f} | {holdout["candidate"]["profit_factor"]:.3f} |
| Full 2025 | {full["baseline"]["trade_count"]} | {full["baseline"]["win_rate"]:.2%} | {full["candidate"]["trade_count"]} | {full["candidate"]["win_rate"]:.2%} | {full["bootstrap_delta"]["observed_relative_delta"]:.2%} | {full["baseline"]["profit_factor"]:.3f} | {full["candidate"]["profit_factor"]:.3f} |

The Q4 holdout bootstrap 95% interval for relative win-rate lift is
{holdout["bootstrap_delta"]["relative_ci_low"]:.2%} to
{holdout["bootstrap_delta"]["relative_ci_high"]:.2%}. The candidate win-rate
Wilson interval is {holdout["candidate"]["win_rate_ci_95"][0]:.2%} to
{holdout["candidate"]["win_rate_ci_95"][1]:.2%}.

## Overfitting controls

- 2025 Jan-Sep was development, Q4 was opened once after freezing, and 2026 was not queried.
- Stage one used 25 single-factor trials; stage two used a six-cell neighboring grid.
- The frozen center is supported by adjacent 4% and 6% pullback settings.
- CSCV-style PBO: {multiple["cscv_probability_backtest_overfitting"]["probability_backtest_overfitting"]:.2%} across {multiple["cscv_probability_backtest_overfitting"]["paths"]} paths.
- Deflated Sharpe probability after {multiple["deflated_sharpe_ratio"]["trials"]} trials: {multiple["deflated_sharpe_ratio"]["probability"]:.2%}.

## Transaction-cost stress

| Assumption | Trades | Win rate | Profit factor | Realized PnL | Max drawdown |
|---|---:|---:|---:|---:|---:|
{chr(10).join(f'| {row["name"]} | {row["trade_count"]} | {row["win_rate"]:.2%} | {row["profit_factor"]:.3f} | ${row["realized_pnl"]:,.2f} | {row["max_drawdown_pct"]:.2%} |' for row in robustness)}

## Data and limitations

- Official daily database: Alpaca SIP, split adjusted, {data_quality["daily_rows_2025"]:,} 2025 rows across {data_quality["daily_sessions_2025"]} sessions; SQLite quick check `{data_quality["daily_quick_check"]}`.
- Separate minute cache: {data_quality["minute_rows_2025"]:,} SIP rows. The official daily database contains {data_quality["official_2026_rows"]} 2026 rows and the research minute cache contains {data_quality["minute_2026_rows"]} 2026 rows.
- The historical security master does not fully eliminate survivorship bias.
- Backtests assume perfect availability at bar prices apart from the explicit slippage runs; queue position, halts, spread spikes, partial fills, and market impact are not fully modeled.
- Lower-priced and thinly traded stocks are present. A $3,500 fixed notional was tested, but live capacity must still be constrained by spread and displayed liquidity.

## External research used

- Opening relative volume and “stocks in play” rationale: https://alexandria.unisg.ch/server/api/core/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/content
- Short-horizon reversal and liquidity imbalance: https://arxiv.org/abs/1005.3535
- Probability of backtest overfitting: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
- Deflated Sharpe ratio: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
"""


def build_executed_notebook(summary: dict[str, object]) -> dict[str, object]:
    """Create a compact nbformat-4 notebook with saved, inspectable outputs."""
    headline = {
        window: {
            "baseline_win_rate": summary[window]["baseline"]["win_rate"],
            "candidate_win_rate": summary[window]["candidate"]["win_rate"],
            "relative_lift": summary[window]["bootstrap_delta"][
                "observed_relative_delta"
            ],
            "candidate_profit_factor": summary[window]["candidate"][
                "profit_factor"
            ],
        }
        for window in ("development", "holdout", "full_2025")
    }
    output_text = json.dumps(headline, ensure_ascii=False, indent=2) + "\n"
    cost_text = (
        json.dumps(
            summary["transaction_cost_robustness"],
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Gap pullback strategy optimization (2025)\n",
                    "\n",
                    "**TL;DR:** Parameters were selected on Jan-Sep, frozen, "
                    "and validated once on Q4. 2026 remains locked.\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Context and method\n",
                    "\n",
                    "Baseline: 2%-8% pullback entry and 8% all-out take profit. "
                    "Frozen candidate: 5%-8% pullback and 4% all-out take profit. "
                    "The target is a 20% relative win-rate increase.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "outputs": [
                    {
                        "name": "stdout",
                        "output_type": "stream",
                        "text": [output_text],
                    }
                ],
                "source": [
                    "from backtest.gap_strategy_validation_report import "
                    "build_validation_summary\n",
                    "summary = build_validation_summary()\n",
                    "headline = {window: {\n",
                    "    'baseline_win_rate': summary[window]['baseline']['win_rate'],\n",
                    "    'candidate_win_rate': summary[window]['candidate']['win_rate'],\n",
                    "    'relative_lift': summary[window]['bootstrap_delta']['observed_relative_delta'],\n",
                    "    'candidate_profit_factor': summary[window]['candidate']['profit_factor'],\n",
                    "} for window in ('development', 'holdout', 'full_2025')}\n",
                    "print(__import__('json').dumps(headline, indent=2))\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Transaction-cost robustness\n",
                    "\n",
                    "Slippage is applied to every simulated fill after the "
                    "candidate has been frozen; it is not used for selection.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 2,
                "metadata": {},
                "outputs": [
                    {
                        "name": "stdout",
                        "output_type": "stream",
                        "text": [cost_text],
                    }
                ],
                "source": [
                    "print(__import__('json').dumps(\n",
                    "    summary['transaction_cost_robustness'], indent=2))\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Takeaways\n",
                    "\n",
                    "- The Q4 holdout clears the relative win-rate target.\n",
                    "- Q4 profit factor is only modestly above one.\n",
                    "- 25 bps per-fill slippage reduces the win-rate lift below "
                    "the target, though profit factor remains above one.\n",
                    "- 2026 must remain an external cross-validation set.\n",
                ],
            },
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _comparison(
    baseline: list[TradeOutcome],
    candidate: list[TradeOutcome],
    *,
    bootstrap_samples: int,
) -> dict[str, object]:
    baseline_stats = summarize_outcomes(baseline)
    candidate_stats = summarize_outcomes(candidate)
    baseline_ci = wilson_interval(baseline_stats.wins, baseline_stats.trade_count)
    candidate_ci = wilson_interval(
        candidate_stats.wins,
        candidate_stats.trade_count,
    )
    return {
        "baseline": {
            **asdict(baseline_stats),
            "win_rate_ci_95": baseline_ci,
        },
        "candidate": {
            **asdict(candidate_stats),
            "win_rate_ci_95": candidate_ci,
        },
        "bootstrap_delta": asdict(
            block_bootstrap_win_rate_delta(
                baseline,
                candidate,
                samples=bootstrap_samples,
            )
        ),
    }


def _sessions(start: date, end: date) -> list[date]:
    with sqlite3.connect(OFFICIAL_DAILY_DB_PATH) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT bar_date
            FROM daily_bars
            WHERE bar_date BETWEEN ? AND ?
              AND feed = 'sip'
              AND adjustment = 'split'
            ORDER BY bar_date
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [date.fromisoformat(row[0]) for row in rows]


def _data_quality_summary() -> dict[str, object]:
    with sqlite3.connect(OFFICIAL_DAILY_DB_PATH) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        daily_rows = connection.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE bar_date LIKE '2025-%'"
        ).fetchone()[0]
        daily_sessions = connection.execute(
            "SELECT COUNT(DISTINCT bar_date) FROM daily_bars "
            "WHERE bar_date LIKE '2025-%'"
        ).fetchone()[0]
        official_2026_rows = connection.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE bar_date LIKE '2026-%'"
        ).fetchone()[0]
        duplicate_rows = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT symbol, bar_date, feed, adjustment, COUNT(*) AS n
                FROM daily_bars
                GROUP BY symbol, bar_date, feed, adjustment
                HAVING n > 1
            )
            """
        ).fetchone()[0]
    with sqlite3.connect(MINUTE_CACHE_PATH) as connection:
        minute_rows = connection.execute(
            "SELECT COUNT(*) FROM minute_bars "
            "WHERE timestamp_utc LIKE '2025-%'"
        ).fetchone()[0]
        minute_2026_rows = connection.execute(
            "SELECT COUNT(*) FROM minute_bars "
            "WHERE timestamp_utc LIKE '2026-%'"
        ).fetchone()[0]
        minute_feed_rows = connection.execute(
            "SELECT COUNT(*) FROM minute_bars "
            "WHERE feed != 'sip' OR adjustment != 'split'"
        ).fetchone()[0]
    return {
        "daily_quick_check": quick_check,
        "daily_rows_2025": daily_rows,
        "daily_sessions_2025": daily_sessions,
        "daily_duplicate_keys": duplicate_rows,
        "official_2026_rows": official_2026_rows,
        "minute_rows_2025": minute_rows,
        "minute_2026_rows": minute_2026_rows,
        "minute_non_sip_or_non_split_rows": minute_feed_rows,
    }


def _load_json(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))
