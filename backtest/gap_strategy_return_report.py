"""Saved-evidence report for the 2025 total-return gap-strategy study."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
from typing import Iterable

from alpaca_ma5_service.config import BASE_DIR
from backtest.gap_strategy_optimization import (
    DEVELOPMENT_END,
    DEVELOPMENT_START,
    HOLDOUT_END,
    HOLDOUT_START,
    LOCKED_CROSS_VALIDATION_YEAR,
    RETURN_PRIMARY_SLIPPAGE_PCT,
    RETURN_STRESS_SLIPPAGE_PCT,
    TradeOutcome,
    return_signal_variants,
)
from backtest.gap_strategy_validation_report import (
    _data_quality_summary,
    _sessions,
    load_outcomes,
)
from backtest.strategy_validation import (
    block_bootstrap_portfolio_return_delta,
    daily_return_series,
    deflated_sharpe_ratio,
    probability_backtest_overfitting_total_return,
)


OUTPUT_ROOT = (
    BASE_DIR / "backtest" / "output" / "gap_strategy_return_optimization"
)
INITIAL_CASH = 100_000.0
LEGACY_SIGNAL_NAME = "return_pb2_closenone_take_profit_8"
SELECTED_SIGNAL_NAME = "return_pb4_close60_take_profit_4"
MATCHED_BASELINE_NAME = "return_baseline_matched_20000_slots_5"
PRIMARY_CANDIDATE_NAME = "return_frozen_primary_10bps"


def build_return_validation_summary(
    output_root: Path = OUTPUT_ROOT,
    *,
    bootstrap_samples: int = 10_000,
) -> dict[str, object]:
    """Recompute total-return claims from persisted rows and trade ledgers."""
    manifest = _load_json_object(
        output_root / "return_frozen_selection_manifest.json"
    )
    signal_rows = _load_json_rows(
        output_root / "return_signal" / "return_signal_results.json"
    )
    sizing_rows = _load_json_rows(
        output_root / "return_sizing" / "return_sizing_results.json"
    )
    holdout_rows = _load_json_rows(
        output_root / "return_holdout" / "return_holdout_results.json"
    )
    robustness_rows = _load_json_rows(
        output_root / "return_robustness" / "return_robustness_results.json"
    )

    development_sessions = _sessions(DEVELOPMENT_START, DEVELOPMENT_END)
    trial_returns: dict[str, list[float]] = {}
    for spec in return_signal_variants():
        outcomes = _phase_outcomes(output_root, "return_signal", spec.name)
        trial_returns[spec.name] = daily_return_series(
            outcomes,
            development_sessions,
            initial_cash=INITIAL_CASH,
        )
    selected_signal_returns = trial_returns[SELECTED_SIGNAL_NAME]
    dsr = deflated_sharpe_ratio(
        selected_signal_returns,
        list(trial_returns.values()),
    )
    pbo = probability_backtest_overfitting_total_return(
        trial_returns,
        groups=10,
    )

    dev_legacy = _phase_outcomes(
        output_root,
        "return_signal",
        LEGACY_SIGNAL_NAME,
    )
    dev_selected_signal = _phase_outcomes(
        output_root,
        "return_signal",
        SELECTED_SIGNAL_NAME,
    )
    frozen_name = str(manifest["candidate_name"])
    dev_frozen = _phase_outcomes(output_root, "return_sizing", frozen_name)
    holdout_baseline = _phase_outcomes(
        output_root,
        "return_holdout",
        "return_baseline_primary_10bps",
    )
    holdout_frozen = _phase_outcomes(
        output_root,
        "return_holdout",
        PRIMARY_CANDIDATE_NAME,
    )
    full_matched_baseline = _phase_outcomes(
        output_root,
        "return_robustness",
        MATCHED_BASELINE_NAME,
    )
    full_primary_candidate = _phase_outcomes(
        output_root,
        "return_robustness",
        PRIMARY_CANDIDATE_NAME,
    )
    q4_matched_baseline = _filter_outcomes(
        full_matched_baseline,
        HOLDOUT_START,
        HOLDOUT_END,
    )
    q4_primary_candidate = _filter_outcomes(
        full_primary_candidate,
        HOLDOUT_START,
        HOLDOUT_END,
    )

    signal_delta = block_bootstrap_portfolio_return_delta(
        dev_legacy,
        dev_selected_signal,
        initial_cash=INITIAL_CASH,
        samples=bootstrap_samples,
    )
    holdout_delta = block_bootstrap_portfolio_return_delta(
        holdout_baseline,
        holdout_frozen,
        initial_cash=INITIAL_CASH,
        samples=bootstrap_samples,
    )
    matched_holdout_delta = block_bootstrap_portfolio_return_delta(
        q4_matched_baseline,
        q4_primary_candidate,
        initial_cash=INITIAL_CASH,
        samples=bootstrap_samples,
    )

    sizing_winner = _row_by_name(sizing_rows, frozen_name)
    holdout_candidate = _row_by_name(holdout_rows, PRIMARY_CANDIDATE_NAME)
    primary_full = _row_by_name(robustness_rows, PRIMARY_CANDIDATE_NAME)
    stress_full = _row_by_name(
        robustness_rows,
        "return_frozen_stress_25bps",
    )
    matched_full = _row_by_name(robustness_rows, MATCHED_BASELINE_NAME)
    legacy_full = _row_by_name(
        robustness_rows,
        "return_baseline_primary_10bps",
    )

    monthly_rows = _monthly_comparison_rows(
        {
            "legacy_original_sizing": _phase_outcomes(
                output_root,
                "return_robustness",
                "return_baseline_primary_10bps",
            ),
            "legacy_matched_sizing": full_matched_baseline,
            "frozen_candidate_10bps": full_primary_candidate,
        }
    )
    return {
        "question": (
            "Which bounded, cash-only configuration maximizes net portfolio "
            "return on 2025 development data without using 2026?"
        ),
        "decision": (
            "Keep as research-only. The frozen candidate maximized development "
            "return, but its absolute Q4 return and Q4 profit factor failed; "
            "do not promote it to Live before a blind 2026 cross-validation."
        ),
        "selection_protocol": {
            "development_window": (
                f"{DEVELOPMENT_START.isoformat()}..{DEVELOPMENT_END.isoformat()}"
            ),
            "holdout_window": (
                f"{HOLDOUT_START.isoformat()}..{HOLDOUT_END.isoformat()}"
            ),
            "locked_cross_validation_year": LOCKED_CROSS_VALIDATION_YEAR,
            "primary_cost_per_fill": RETURN_PRIMARY_SLIPPAGE_PCT,
            "stress_cost_per_fill": RETURN_STRESS_SLIPPAGE_PCT,
            "initial_cash": INITIAL_CASH,
            "leverage_allowed": False,
            "signal_trials": len(signal_rows),
            "sizing_trials": len(sizing_rows),
            "frozen_candidate": frozen_name,
            "post_holdout_parameter_changes": 0,
            "q4_reuse_limitation": manifest["holdout_reuse_note"],
        },
        "frozen_parameters": {
            "minimum_pullback_from_signal_close": -0.04,
            "minimum_signal_close_position": 0.60,
            "take_profit_all_out": 0.04,
            "stop_loss": -0.08,
            "buy_notional_usd": 20_000.0,
            "max_positions": 5,
            "max_daily_buys": 5,
            "maximum_nominal_concurrent_capital": 100_000.0,
            "overnight_holding": False,
        },
        "headline": {
            "development_return": sizing_winner["backtest_return_pct"],
            "development_win_rate": sizing_winner["win_rate"],
            "development_profit_factor": sizing_winner["profit_factor"],
            "development_max_drawdown": sizing_winner["max_drawdown_pct"],
            "holdout_return": holdout_candidate["backtest_return_pct"],
            "holdout_profit_factor": holdout_candidate["profit_factor"],
            "holdout_max_drawdown": holdout_candidate["max_drawdown_pct"],
            "full_2025_primary_return": primary_full["backtest_return_pct"],
            "full_2025_primary_profit_factor": primary_full["profit_factor"],
            "full_2025_stress_return": stress_full["backtest_return_pct"],
            "matched_baseline_full_return": matched_full["backtest_return_pct"],
        },
        "signal_attribution": {
            "legacy_3500_10bps": _row_by_name(signal_rows, LEGACY_SIGNAL_NAME),
            "selected_signal_3500_10bps": _row_by_name(
                signal_rows,
                SELECTED_SIGNAL_NAME,
            ),
            "bootstrap_return_delta": asdict(signal_delta),
        },
        "sizing_curve": [
            _select_metrics(row)
            for row in sorted(
                sizing_rows,
                key=lambda item: float(item["backtest_return_pct"]),
            )
        ],
        "holdout": {
            "legacy_original_sizing": _select_metrics(
                _row_by_name(
                    holdout_rows,
                    "return_baseline_primary_10bps",
                )
            ),
            "frozen_candidate": _select_metrics(holdout_candidate),
            "bootstrap_return_delta": asdict(holdout_delta),
        },
        "matched_risk_attribution": {
            "legacy_matched_sizing_full_2025": _select_metrics(matched_full),
            "frozen_candidate_full_2025": _select_metrics(primary_full),
            "q4_candidate_minus_matched_baseline": asdict(
                matched_holdout_delta
            ),
            "interpretation": (
                "At the same $20,000 x 5 capital cap, the legacy signal "
                "slightly exceeded the frozen candidate on full-year return "
                "but incurred materially larger drawdown. Signal changes did "
                "not robustly dominate; most headline return came from capital "
                "deployment."
            ),
        },
        "transaction_cost_robustness": [
            _select_metrics(row) for row in robustness_rows
        ],
        "monthly_pnl": monthly_rows,
        "multiple_testing": {
            "scope": (
                "Twelve equal-sized signal variants only; sizing variants are "
                "excluded because scale mechanically changes returns."
            ),
            "deflated_sharpe_ratio": asdict(dsr),
            "cscv_total_return_pbo": asdict(pbo),
        },
        "data_quality": _data_quality_summary(),
        "limitations": [
            (
                "The frozen return candidate had a negative Q4 and a Q4 "
                "standalone drawdown above the development guardrail."
            ),
            (
                "The security master does not fully remove survivorship bias; "
                "halts, queue priority, spread spikes, partial fills, and "
                "market impact are not fully modeled."
            ),
            (
                "$20,000 entries can be unrealistic in low-priced or thin "
                "stocks even when a uniform 25 bps stress remains profitable."
            ),
            (
                "Q4 had already been opened for two variants during the earlier "
                "win-rate study, so 2026 remains the only clean external "
                "cross-validation year."
            ),
        ],
        "live_status": {
            "candidate_promoted": False,
            "orders_submitted": False,
            "monitor_started": False,
            "reason": "Failed absolute Q4 holdout; 2026 is still locked.",
        },
    }


def write_return_validation_artifacts(
    output_root: Path = OUTPUT_ROOT,
) -> tuple[Path, Path, Path]:
    summary = build_return_validation_summary(output_root)
    json_path = output_root / "return_validation_summary.json"
    markdown_path = output_root / "return_validation_report.md"
    notebook_path = output_root / "gap_strategy_return_optimization_2025.ipynb"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_markdown_report(summary),
        encoding="utf-8",
    )
    notebook_path.write_text(
        json.dumps(
            build_executed_notebook(summary),
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return json_path, markdown_path, notebook_path


def render_markdown_report(summary: dict[str, object]) -> str:
    headline = summary["headline"]
    holdout = summary["holdout"]
    matched = summary["matched_risk_attribution"]
    multiple = summary["multiple_testing"]
    data_quality = summary["data_quality"]
    costs = summary["transaction_cost_robustness"]
    sizing = summary["sizing_curve"]
    return f"""# Gap strategy total-return research

## Executive Summary

- **Decision: research-only, not Live.** {summary["decision"]}
- **Development winner:** 10 bps per fill, $20,000 per entry, five slots,
  4% pullback, signal close in the upper 40% of its range, and a 4% all-out
  target produced {headline["development_return"]:.2%} on Jan-Sep with
  {headline["development_max_drawdown"]:.2%} max drawdown.
- **Holdout failed in absolute terms:** Q4 returned
  {headline["holdout_return"]:.2%}, profit factor
  {headline["holdout_profit_factor"]:.3f}, and standalone max drawdown
  {headline["holdout_max_drawdown"]:.2%}.
- **Most of the headline gain came from capital deployment:** at matched
  $20,000 x 5 sizing, the legacy signal returned
  {headline["matched_baseline_full_return"]:.2%} for full 2025 versus
  {headline["full_2025_primary_return"]:.2%} for the frozen signal, but with
  much larger drawdown.

## What maximized development return

The selection objective was portfolio return on a fixed $100,000 cash account
after 10 bps slippage on every simulated fill. Leverage and overnight holding
were disabled. Candidates also needed at least 500 completed trades, profit
factor >= 1.10, max drawdown no worse than -15%, and positive PnL in Q1, Q2,
and Q3.

| Entry notional / slots | Trades | Net return | Win rate | Profit factor | Max drawdown |
|---|---:|---:|---:|---:|---:|
{chr(10).join(f'| ${row["buy_notional_usd"]:,.0f} / {row["max_positions"]} | {row["trade_count"]} | {row["backtest_return_pct"]:.2%} | {row["win_rate"]:.2%} | {row["profit_factor"]:.3f} | {row["max_drawdown_pct"]:.2%} |' for row in sizing)}

## Holdout changed the decision

The frozen candidate beat the original-size baseline in Q4 by
{holdout["bootstrap_return_delta"]["observed_delta"]:.2%}, but both were
negative. The 95% day-block bootstrap interval for that difference was
{holdout["bootstrap_return_delta"]["delta_ci_low"]:.2%} to
{holdout["bootstrap_return_delta"]["delta_ci_high"]:.2%}; this is not strong
evidence of stable outperformance.

At matched $20,000 x 5 sizing, the Q4 candidate-minus-baseline difference was
{matched["q4_candidate_minus_matched_baseline"]["observed_delta"]:.2%}.
That result and the candidate's Q4 profit factor below one are why the candidate
was not promoted.

## Cost and attribution checks

| Configuration | Trades | Full-2025 return | Q4 PnL / initial cash | Profit factor | Max drawdown |
|---|---:|---:|---:|---:|---:|
{chr(10).join(f'| {row["name"]} | {row["trade_count"]} | {row["backtest_return_pct"]:.2%} | {row["q4_realized_pnl"] / 100000.0:.2%} | {row["profit_factor"]:.3f} | {row["max_drawdown_pct"]:.2%} |' for row in costs)}

Uniform 25 bps slippage per fill still left the candidate profitable for full
2025, but Q4 fell to {next(row for row in costs if row["name"] == "return_frozen_stress_25bps")["q4_realized_pnl"] / 100000.0:.2%}.
This is a capacity warning, not proof that $20,000 orders are executable in
thin names.

## Overfitting controls

- 12 equal-sized signal variants and 7 cash-only sizing variants were tested;
  parameters were frozen before calculating this candidate's Q4 outcomes.
- Total-return CSCV-style PBO on the equal-sized signal grid:
  {multiple["cscv_total_return_pbo"]["probability_backtest_overfitting"]:.2%}
  across {multiple["cscv_total_return_pbo"]["paths"]} paths.
- Deflated-Sharpe probability across the same 12 signal trials:
  {multiple["deflated_sharpe_ratio"]["probability"]:.2%}.
- No 2026 rows were used; 2026 remains reserved for external cross-validation.

## Data quality and next step

The official SIP/split-adjusted daily database contains
{data_quality["daily_rows_2025"]:,} 2025 rows across
{data_quality["daily_sessions_2025"]} sessions and passed SQLite
`{data_quality["daily_quick_check"]}`. The separate SIP minute cache contains
{data_quality["minute_rows_2025"]:,} 2025 rows. Both stores contain zero 2026
research rows.

The next valid decision is a blind 2026 paper-only replay of the frozen
candidate and the matched-size legacy control. Do not tune on those results
before recording the pass/fail criteria, and do not activate $20,000 entries
without spread/liquidity capacity limits.

## External research

- Probability of backtest overfitting:
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
- Deflated Sharpe ratio:
  https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- Intraday reversal and liquidity imbalance:
  https://arxiv.org/abs/1005.3535
"""


def build_executed_notebook(summary: dict[str, object]) -> dict[str, object]:
    headline_text = (
        json.dumps(summary["headline"], ensure_ascii=False, indent=2) + "\n"
    )
    sizing_text = (
        json.dumps(summary["sizing_curve"], ensure_ascii=False, indent=2) + "\n"
    )
    holdout_text = (
        json.dumps(summary["holdout"], ensure_ascii=False, indent=2) + "\n"
    )
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Gap strategy total-return research (2025)\n",
                    "\n",
                    "**TL;DR:** The development winner produced high net "
                    "return but failed the absolute Q4 holdout. It remains "
                    "research-only; 2026 is locked.\n",
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
                        "text": [headline_text],
                    }
                ],
                "source": [
                    "from backtest.gap_strategy_return_report import "
                    "build_return_validation_summary\n",
                    "summary = build_return_validation_summary()\n",
                    "print(__import__('json').dumps("
                    "summary['headline'], indent=2))\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Cash-only sizing curve\n",
                    "\n",
                    "All rows use the same frozen signal and 10 bps slippage "
                    "per fill. Maximum nominal concurrent capital never "
                    "exceeds the $100,000 initial cash balance.\n",
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
                        "text": [sizing_text],
                    }
                ],
                "source": [
                    "print(__import__('json').dumps("
                    "summary['sizing_curve'], indent=2))\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Frozen Q4 holdout\n",
                    "\n",
                    "The candidate was not changed after this result.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 3,
                "metadata": {},
                "outputs": [
                    {
                        "name": "stdout",
                        "output_type": "stream",
                        "text": [holdout_text],
                    }
                ],
                "source": [
                    "print(__import__('json').dumps("
                    "summary['holdout'], indent=2))\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Decision\n",
                    "\n",
                    "- Do not promote the return candidate to Live.\n",
                    "- Keep 2026 untouched until pass/fail criteria are frozen.\n",
                    "- Compare against the legacy signal at matched sizing so "
                    "capital utilization is not mistaken for signal alpha.\n",
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


def _phase_outcomes(
    output_root: Path,
    phase: str,
    name: str,
) -> list[TradeOutcome]:
    return load_outcomes(
        output_root / phase / name / "backtest_trades.csv"
    )


def _filter_outcomes(
    outcomes: Iterable[TradeOutcome],
    start: date,
    end: date,
) -> list[TradeOutcome]:
    return [
        outcome
        for outcome in outcomes
        if start <= outcome.exit_date <= end
    ]


def _monthly_comparison_rows(
    series: dict[str, Iterable[TradeOutcome]],
) -> list[dict[str, object]]:
    pnl: dict[tuple[str, str], float] = defaultdict(float)
    for name, outcomes in series.items():
        for outcome in outcomes:
            pnl[(outcome.exit_date.strftime("%Y-%m"), name)] += (
                outcome.realized_pnl
            )
    months = [f"2025-{month:02d}" for month in range(1, 13)]
    rows: list[dict[str, object]] = []
    for month in months:
        row: dict[str, object] = {"month": month}
        for name in series:
            row[f"{name}_pnl"] = pnl.get((month, name), 0.0)
            row[f"{name}_return_on_initial_cash"] = (
                pnl.get((month, name), 0.0) / INITIAL_CASH
            )
        rows.append(row)
    return rows


def _select_metrics(row: dict[str, object]) -> dict[str, object]:
    fields = (
        "name",
        "description",
        "trade_count",
        "win_rate",
        "profit_factor",
        "realized_pnl",
        "backtest_return_pct",
        "max_drawdown_pct",
        "q1_realized_pnl",
        "q2_realized_pnl",
        "q3_realized_pnl",
        "q4_realized_pnl",
    )
    selected = {key: row.get(key) for key in fields}
    spec = row.get("spec")
    if isinstance(spec, dict):
        selected["buy_notional_usd"] = spec.get("buy_notional_usd")
        selected["max_positions"] = spec.get("max_positions")
        selected["max_daily_buys"] = spec.get("max_daily_buys")
        selected["slippage_pct"] = spec.get("slippage_pct")
    return selected


def _row_by_name(
    rows: list[dict[str, object]],
    name: str,
) -> dict[str, object]:
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one row named {name}, found {len(matches)}")
    return matches[0]


def _load_json_rows(path: Path) -> list[dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"Expected a JSON list: {path}")
    return value


def _load_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value
