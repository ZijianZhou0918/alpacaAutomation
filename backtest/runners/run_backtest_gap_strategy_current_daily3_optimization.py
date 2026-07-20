from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
from typing import Iterable

from alpaca_ma5_service.afterhours_high_low import MinuteBar
from alpaca_ma5_service.config import BASE_DIR
from backtest.engine import (
    BacktestConfig,
    BacktestResult,
    build_historical_watchlists,
    fetch_backtest_daily_bars,
)
from backtest.gap_strategy_current_daily3_optimization import (
    DEVELOPMENT_END,
    DEVELOPMENT_START,
    DIAGNOSTIC_END,
    DIAGNOSTIC_START,
    INITIAL_CASH,
    LOCKED_2026_END,
    LOCKED_2026_START,
    MINIMUM_ROUNDS,
    current_code_baseline,
    family_champions,
    spec_from_dict,
    spec_to_dict,
    stage_one_variants,
    stage_two_variants,
    validate_phase_window,
)
from backtest.gap_strategy_optimization import (
    MINUTE_CACHE_PATH,
    TradeOutcome,
    VariantSpec,
    build_variant_config,
    candidate_pairs,
    ensure_strict_sip_minute_cache,
    load_cached_candidate_minutes,
    run_cached_variant,
    summarize_outcomes,
    trade_outcomes,
)
from backtest.runners.run_backtest import build_final_strategy_config
from backtest.strategy_validation import (
    block_bootstrap_portfolio_return_delta,
    block_bootstrap_win_rate_delta,
    deflated_sharpe_ratio,
    probability_backtest_overfitting_total_return,
)


OUTPUT_DIR = (
    BASE_DIR / "backtest" / "output" / "gap_strategy_current_daily3_optimization"
)
FROZEN_MANIFEST = OUTPUT_DIR / "frozen_selection_manifest.json"
LOCKED_2026_CACHE_PATH = (
    BASE_DIR
    / "backtest"
    / "data"
    / "gap_strategy_2026_frozen_daily3_minute_cache.sqlite"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Optimize the exact current gap strategy with at most three buys "
            "per day, then evaluate one frozen candidate."
        )
    )
    parser.add_argument(
        "--phase",
        choices=("development", "diagnostic", "validate-2026"),
        default="development",
    )
    args = parser.parse_args()

    if args.phase == "development":
        validate_phase_window(args.phase, DEVELOPMENT_START, DEVELOPMENT_END)
        run_development()
    elif args.phase == "diagnostic":
        validate_phase_window(args.phase, DIAGNOSTIC_START, DIAGNOSTIC_END)
        run_frozen_comparison(
            phase=args.phase,
            start=DIAGNOSTIC_START,
            end=DIAGNOSTIC_END,
            year=2025,
            cache_path=MINUTE_CACHE_PATH,
        )
    else:
        validate_phase_window(args.phase, LOCKED_2026_START, LOCKED_2026_END)
        run_frozen_comparison(
            phase=args.phase,
            start=LOCKED_2026_START,
            end=LOCKED_2026_END,
            year=2026,
            cache_path=LOCKED_2026_CACHE_PATH,
        )


def run_development() -> None:
    phase_dir = OUTPUT_DIR / "development"
    rows_path = phase_dir / "development_results.json"
    base, daily_bars, baseline_pairs, cached_minutes, sessions = prepare_period(
        DEVELOPMENT_START,
        DEVELOPMENT_END,
        year=2025,
        cache_path=MINUTE_CACHE_PATH,
        output_dir=phase_dir,
    )

    rows = read_rows(rows_path)
    rows = run_specs(
        stage_one_variants(),
        rows,
        base=base,
        daily_bars=daily_bars,
        cached_minutes=cached_minutes,
        baseline_pairs=baseline_pairs,
        sessions=sessions,
        output_dir=phase_dir,
        rows_path=rows_path,
    )
    champions = family_champions(rows)
    interactions = stage_two_variants(champions)
    if interactions:
        rows = run_specs(
            interactions,
            rows,
            base=base,
            daily_bars=daily_bars,
            cached_minutes=cached_minutes,
            baseline_pairs=baseline_pairs,
            sessions=sessions,
            output_dir=phase_dir,
            rows_path=rows_path,
        )

    manifest = freeze_selection(rows, sessions)
    FROZEN_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    FROZEN_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_development_report(rows, manifest)
    print(f"Frozen selection manifest: {FROZEN_MANIFEST}", flush=True)


def run_frozen_comparison(
    *,
    phase: str,
    start: date,
    end: date,
    year: int,
    cache_path: Path,
) -> None:
    if not FROZEN_MANIFEST.exists():
        raise FileNotFoundError(
            "Development must freeze a selection before any holdout is opened: "
            f"{FROZEN_MANIFEST}"
        )
    manifest = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    phase_dir = OUTPUT_DIR / phase
    results_path = phase_dir / f"{phase}_results.json"
    if results_path.exists():
        print(
            f"{phase} is immutable and already exists; refusing to rerun: "
            f"{results_path}",
            flush=True,
        )
        return

    base, daily_bars, baseline_pairs, cached_minutes, sessions = prepare_period(
        start,
        end,
        year=year,
        cache_path=cache_path,
        output_dir=phase_dir,
    )
    specs: list[tuple[str, VariantSpec]] = [
        ("baseline", current_code_baseline()),
    ]
    selected = manifest.get("selected_candidate")
    if isinstance(selected, dict):
        specs.append(
            (
                "frozen_candidate",
                spec_from_dict(
                    str(selected["name"]),
                    str(selected["description"]),
                    selected["spec"],
                ),
            )
        )
    rows = run_specs(
        tuple(specs),
        [],
        base=base,
        daily_bars=daily_bars,
        cached_minutes=cached_minutes,
        baseline_pairs=baseline_pairs,
        sessions=sessions,
        output_dir=phase_dir,
        rows_path=results_path,
    )
    comparison = comparison_summary(rows)
    (phase_dir / f"{phase}_comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(comparison, ensure_ascii=False, indent=2), flush=True)


def prepare_period(
    start: date,
    end: date,
    *,
    year: int,
    cache_path: Path,
    output_dir: Path,
) -> tuple[
    BacktestConfig,
    dict,
    set[tuple[str, str]],
    dict[str, list[MinuteBar]],
    list[date],
]:
    current = build_final_strategy_config(output_dir / "current", "", year=year)
    base = build_variant_config(
        current,
        current_code_baseline(),
        start_date=start,
        end_date=end,
        output_dir=output_dir / "base",
    )
    daily_bars = fetch_backtest_daily_bars(base, base.symbols)
    baseline_watchlists = build_historical_watchlists(daily_bars, base)
    ensure_strict_sip_minute_cache(
        baseline_watchlists,
        cache_path=cache_path,
    )
    cached_minutes = load_cached_candidate_minutes(
        baseline_watchlists,
        cache_path=cache_path,
    )
    sessions = sorted(
        {
            bar.date
            for bars in daily_bars.values()
            for bar in bars
            if start <= bar.date <= end
        }
    )
    return (
        base,
        daily_bars,
        candidate_pairs(baseline_watchlists),
        cached_minutes,
        sessions,
    )


def run_specs(
    specs: tuple[tuple[str, VariantSpec], ...],
    existing_rows: list[dict[str, object]],
    *,
    base: BacktestConfig,
    daily_bars: dict,
    cached_minutes: dict[str, list[MinuteBar]],
    baseline_pairs: set[tuple[str, str]],
    sessions: list[date],
    output_dir: Path,
    rows_path: Path,
) -> list[dict[str, object]]:
    rows = list(existing_rows)
    completed = {str(row["name"]) for row in rows}
    for index, (family, spec) in enumerate(specs, start=1):
        if spec.name in completed:
            print(f"Resume: {spec.name} already complete", flush=True)
            continue
        print(
            f"Trial {index}/{len(specs)} [{family}]: {spec.name}",
            flush=True,
        )
        config = build_variant_config(
            base,
            spec,
            start_date=base.start_date,
            end_date=base.end_date,
            output_dir=output_dir / spec.name,
        )
        result = run_cached_variant(
            config,
            daily_bars=daily_bars,
            cached_minutes=cached_minutes,
            baseline_pairs=baseline_pairs,
        )
        row = result_row(family, spec, result, sessions)
        rows.append(row)
        completed.add(spec.name)
        write_rows(rows_path, rows)
        print(
            f"{spec.name}: rounds={row['trade_count']} "
            f"win={float(row['win_rate']):.2%} "
            f"return={float(row['backtest_return_pct']):.2%} "
            f"pf={float(row['profit_factor']):.3f}",
            flush=True,
        )
    return rows


def result_row(
    family: str,
    spec: VariantSpec,
    result: BacktestResult,
    sessions: list[date],
) -> dict[str, object]:
    outcomes = trade_outcomes(result.trades)
    overall = summarize_outcomes(outcomes)
    quarters = {
        "q1": summarize_outcomes(
            outcomes,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 3, 31),
        ),
        "q2": summarize_outcomes(
            outcomes,
            start_date=date(2025, 4, 1),
            end_date=date(2025, 6, 30),
        ),
        "q3": summarize_outcomes(
            outcomes,
            start_date=date(2025, 7, 1),
            end_date=date(2025, 9, 30),
        ),
    }
    daily_buys = Counter(
        trade.timestamp.date() for trade in result.trades if trade.side == "BUY"
    )
    block_pnls = contiguous_block_pnls(outcomes, sessions, groups=6)
    return {
        "family": family,
        "name": spec.name,
        "description": spec.description,
        "trade_count": overall.trade_count,
        "win_rate": overall.win_rate,
        "profit_factor": overall.profit_factor,
        "realized_pnl": overall.realized_pnl,
        "backtest_return_pct": result.stats.return_pct,
        "max_drawdown_pct": result.stats.max_drawdown_pct,
        "max_buys_on_one_day": max(daily_buys.values(), default=0),
        "profitable_blocks": sum(value > 0 for value in block_pnls),
        "block_realized_pnl": block_pnls,
        **{
            f"{quarter}_realized_pnl": stats.realized_pnl
            for quarter, stats in quarters.items()
        },
        "daily_returns": daily_returns(outcomes, sessions),
        "outcomes": [
            {
                **asdict(outcome),
                "entry_date": outcome.entry_date.isoformat(),
                "exit_date": outcome.exit_date.isoformat(),
            }
            for outcome in outcomes
        ],
        "spec": spec_to_dict(spec),
    }


def freeze_selection(
    rows: list[dict[str, object]],
    sessions: list[date],
) -> dict[str, object]:
    baseline = next(row for row in rows if row["family"] == "baseline")
    qualified: list[tuple[dict[str, object], dict[str, object]]] = []
    for row in rows:
        if row is baseline:
            continue
        return_bootstrap = block_bootstrap_portfolio_return_delta(
            outcomes_from_row(baseline),
            outcomes_from_row(row),
            initial_cash=INITIAL_CASH,
            samples=5000,
        )
        win_bootstrap = block_bootstrap_win_rate_delta(
            outcomes_from_row(baseline),
            outcomes_from_row(row),
            samples=5000,
        )
        checks = {
            "minimum_rounds": int(row["trade_count"]) >= MINIMUM_ROUNDS,
            "return_improvement": (
                float(row["backtest_return_pct"])
                >= float(baseline["backtest_return_pct"]) + 0.01
            ),
            "win_rate_improvement": (
                float(row["win_rate"]) >= float(baseline["win_rate"]) + 0.005
            ),
            "profit_factor_not_lower": (
                float(row["profit_factor"]) >= float(baseline["profit_factor"])
            ),
            "drawdown_not_materially_worse": (
                float(row["max_drawdown_pct"])
                >= float(baseline["max_drawdown_pct"]) - 0.0025
            ),
            "all_development_quarters_profitable": all(
                float(row[f"{quarter}_realized_pnl"]) > 0
                for quarter in ("q1", "q2", "q3")
            ),
            "five_of_six_blocks_profitable": int(row["profitable_blocks"]) >= 5,
            "bootstrap_return_probability": (
                return_bootstrap.probability_candidate_outperforms >= 0.70
            ),
            "bootstrap_win_lower_bound": (
                win_bootstrap.absolute_ci_low >= -0.02
            ),
            "daily_buy_limit": int(row["max_buys_on_one_day"]) <= 3,
        }
        diagnostics = {
            "checks": checks,
            "return_bootstrap": asdict(return_bootstrap),
            "win_rate_bootstrap": asdict(win_bootstrap),
        }
        if all(checks.values()):
            qualified.append((row, diagnostics))

    selected_pair = (
        max(
            qualified,
            key=lambda item: (
                float(item[0]["backtest_return_pct"]),
                float(item[0]["win_rate"]),
                str(item[0]["name"]),
            ),
        )
        if qualified
        else None
    )
    return_series = {
        str(row["name"]): [float(value) for value in row["daily_returns"]]
        for row in rows
    }
    pbo = probability_backtest_overfitting_total_return(
        return_series,
        groups=10,
    )
    selected_series = (
        [float(value) for value in selected_pair[0]["daily_returns"]]
        if selected_pair
        else [float(value) for value in baseline["daily_returns"]]
    )
    observed_trials = list(return_series.values())
    conservative_trial_count = 69 + len(observed_trials)
    padded_trials = observed_trials + [
        [0.0 for _ in sessions]
        for _ in range(max(0, conservative_trial_count - len(observed_trials)))
    ]
    dsr = deflated_sharpe_ratio(selected_series, padded_trials)

    selected_candidate = None
    selected_diagnostics = None
    if selected_pair:
        row, selected_diagnostics = selected_pair
        selected_candidate = {
            "family": row["family"],
            "name": row["name"],
            "description": row["description"],
            "development_metrics": public_metrics(row),
            "spec": row["spec"],
        }
    return {
        "status": (
            "frozen_candidate_before_any_holdout"
            if selected_candidate
            else "frozen_no_replacement_before_any_holdout"
        ),
        "selection_window": (
            f"{DEVELOPMENT_START.isoformat()}..{DEVELOPMENT_END.isoformat()}"
        ),
        "objective": (
            "Improve both total portfolio return and round-level win rate "
            "versus the exact current-code baseline"
        ),
        "baseline": public_metrics(baseline),
        "selected_candidate": selected_candidate,
        "selected_diagnostics": selected_diagnostics,
        "qualified_candidate_count": len(qualified),
        "tested_candidate_count": len(rows) - 1,
        "multiple_testing": {
            "total_return_pbo": asdict(pbo),
            "deflated_sharpe": asdict(dsr),
            "prior_trials_counted": 69,
            "current_trials_counted": len(observed_trials),
        },
        "locked_2026_window": (
            f"{LOCKED_2026_START.isoformat()}..{LOCKED_2026_END.isoformat()}"
        ),
        "no_retuning_after_holdout": True,
    }


def comparison_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    baseline = next(row for row in rows if row["family"] == "baseline")
    candidate = next(
        (row for row in rows if row["family"] == "frozen_candidate"),
        None,
    )
    return {
        "baseline": public_metrics(baseline),
        "candidate": public_metrics(candidate) if candidate else None,
        "candidate_beats_baseline_on_both": bool(
            candidate
            and float(candidate["backtest_return_pct"])
            > float(baseline["backtest_return_pct"])
            and float(candidate["win_rate"]) > float(baseline["win_rate"])
        ),
        "retuning_permitted": False,
    }


def public_metrics(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        key: row[key]
        for key in (
            "name",
            "trade_count",
            "win_rate",
            "profit_factor",
            "realized_pnl",
            "backtest_return_pct",
            "max_drawdown_pct",
            "max_buys_on_one_day",
            "profitable_blocks",
            "block_realized_pnl",
            "q1_realized_pnl",
            "q2_realized_pnl",
            "q3_realized_pnl",
        )
        if key in row
    }


def outcomes_from_row(row: dict[str, object]) -> list[TradeOutcome]:
    outcomes: list[TradeOutcome] = []
    for raw in row.get("outcomes", []):
        if not isinstance(raw, dict):
            continue
        outcomes.append(
            TradeOutcome(
                symbol=str(raw["symbol"]),
                entry_date=date.fromisoformat(str(raw["entry_date"])),
                exit_date=date.fromisoformat(str(raw["exit_date"])),
                realized_pnl=float(raw["realized_pnl"]),
                invested=float(raw["invested"]),
            )
        )
    return outcomes


def daily_returns(
    outcomes: Iterable[TradeOutcome],
    sessions: list[date],
) -> list[float]:
    pnl_by_day: Counter[date] = Counter()
    for outcome in outcomes:
        pnl_by_day[outcome.exit_date] += outcome.realized_pnl
    return [pnl_by_day[session] / INITIAL_CASH for session in sessions]


def contiguous_block_pnls(
    outcomes: Iterable[TradeOutcome],
    sessions: list[date],
    *,
    groups: int,
) -> list[float]:
    pnl_by_day: Counter[date] = Counter()
    for outcome in outcomes:
        pnl_by_day[outcome.exit_date] += outcome.realized_pnl
    base_size, remainder = divmod(len(sessions), groups)
    block_pnls: list[float] = []
    start = 0
    for index in range(groups):
        size = base_size + int(index < remainder)
        selected = sessions[start : start + size]
        block_pnls.append(sum(pnl_by_day[session] for session in selected))
        start += size
    return block_pnls


def read_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected a result list: {path}")
    return raw


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_development_report(
    rows: list[dict[str, object]],
    manifest: dict[str, object],
) -> None:
    ranked = sorted(
        rows,
        key=lambda row: (
            float(row["backtest_return_pct"]),
            float(row["win_rate"]),
        ),
        reverse=True,
    )
    lines = [
        "# Current-code daily-3 strategy optimization",
        "",
        "Selection used only 2025-01-01 through 2025-09-30.",
        "",
        "| Candidate | Rounds | Win rate | Return | PF | Max drawdown |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in ranked:
        lines.append(
            f"| {row['name']} | {row['trade_count']} | "
            f"{float(row['win_rate']):.2%} | "
            f"{float(row['backtest_return_pct']):.2%} | "
            f"{float(row['profit_factor']):.3f} | "
            f"{float(row['max_drawdown_pct']):.2%} |"
        )
    lines.extend(
        [
            "",
            f"Freeze status: `{manifest['status']}`.",
            "",
            "No 2026 outcomes were read by this phase.",
        ]
    )
    (OUTPUT_DIR / "development_report.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
