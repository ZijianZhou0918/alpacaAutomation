"""CLI for the bounded 2025 gap-strategy research workflow."""

from __future__ import annotations

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

import argparse
import csv
from datetime import date
import json
from pathlib import Path

from alpaca_ma5_service.config import BASE_DIR
from backtest.engine import build_historical_watchlists, fetch_backtest_daily_bars
from backtest.gap_strategy_optimization import (
    DEVELOPMENT_END,
    DEVELOPMENT_START,
    FROZEN_CANDIDATE_NAME,
    HOLDOUT_END,
    HOLDOUT_START,
    LOCKED_CROSS_VALIDATION_YEAR,
    MINUTE_CACHE_PATH,
    RETURN_MAX_DRAWDOWN_PCT,
    RETURN_MIN_PROFIT_FACTOR,
    RETURN_MIN_TRADE_COUNT,
    RETURN_PRIMARY_SLIPPAGE_PCT,
    build_variant_config,
    candidate_pairs,
    ensure_strict_sip_minute_cache,
    holdout_variants,
    legacy_baseline_variant,
    load_cached_candidate_minutes,
    return_holdout_variants,
    return_robustness_variants,
    return_signal_variants,
    return_sizing_variants,
    robustness_variants,
    run_cached_variant,
    select_return_row,
    stage_one_variants,
    stage_two_variants,
    summarize_outcomes,
    trade_outcomes,
    validate_research_period,
    variant_from_result_row,
)
from backtest.runners.run_backtest import build_final_strategy_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one bounded 2025 gap-strategy research phase."
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=DEVELOPMENT_START,
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=DEVELOPMENT_END,
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Populate the strict SIP minute cache without running the baseline.",
    )
    parser.add_argument(
        "--phase",
        choices=(
            "baseline",
            "stage1",
            "stage2",
            "holdout",
            "robustness",
            "return_signal",
            "return_sizing",
            "return_holdout",
            "return_robustness",
        ),
        default="baseline",
        help="Run the baseline or one pre-registered optimization stage.",
    )
    args = parser.parse_args()
    validate_research_period(args.phase, args.start_date, args.end_date)

    output_name = (
        "gap_strategy_return_optimization"
        if args.phase.startswith("return_")
        else "gap_strategy_optimization"
    )
    output_dir = BASE_DIR / "backtest" / "output" / output_name
    if args.phase in {"holdout", "robustness"}:
        write_frozen_selection_manifest(output_dir)
    if args.phase in {"return_holdout", "return_robustness"}:
        write_return_frozen_selection_manifest(output_dir)
    if args.phase == "robustness":
        holdout_path = output_dir / "holdout" / "holdout_results.json"
        if not holdout_path.exists():
            raise FileNotFoundError(
                "Holdout must finish before post-selection robustness checks: "
                f"{holdout_path}"
            )
    if args.phase == "return_robustness":
        holdout_path = (
            output_dir / "return_holdout" / "return_holdout_results.json"
        )
        if not holdout_path.exists():
            raise FileNotFoundError(
                "Return holdout must finish before robustness checks: "
                f"{holdout_path}"
            )
    current = build_final_strategy_config(output_dir / "current", "", year=2025)
    base = build_variant_config(
        current,
        legacy_baseline_variant(),
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=output_dir / "base",
    )
    baseline = build_variant_config(
        base,
        legacy_baseline_variant(),
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=output_dir / "baseline",
    )
    daily_bars = fetch_backtest_daily_bars(baseline, baseline.symbols)
    baseline_watchlists = build_historical_watchlists(daily_bars, baseline)
    ensure_strict_sip_minute_cache(
        baseline_watchlists,
        cache_path=MINUTE_CACHE_PATH,
    )
    if args.cache_only:
        print(f"Strict SIP minute cache ready: {MINUTE_CACHE_PATH}")
        return

    cached_minutes = load_cached_candidate_minutes(
        baseline_watchlists,
        cache_path=MINUTE_CACHE_PATH,
    )
    baseline_candidate_pairs = candidate_pairs(baseline_watchlists)
    if args.phase == "baseline":
        result = run_cached_variant(
            baseline,
            daily_bars=daily_bars,
            cached_minutes=cached_minutes,
            baseline_pairs=baseline_candidate_pairs,
        )
        outcome_stats = summarize_outcomes(trade_outcomes(result.trades))
        print(
            "Baseline complete: "
            f"trades={outcome_stats.trade_count} "
            f"win_rate={outcome_stats.win_rate:.2%} "
            f"profit_factor={outcome_stats.profit_factor:.3f} "
            f"realized_pnl=${outcome_stats.realized_pnl:,.2f}"
        )
        return

    if args.phase == "stage1":
        phase_specs = stage_one_variants()
    elif args.phase == "stage2":
        phase_specs = stage_two_variants()
    elif args.phase == "holdout":
        phase_specs = holdout_variants()
    elif args.phase == "robustness":
        phase_specs = robustness_variants()
    elif args.phase == "return_signal":
        phase_specs = return_signal_variants()
    elif args.phase == "return_sizing":
        signal_rows = read_result_rows(
            output_dir / "return_signal" / "return_signal_results.json"
        )
        phase_specs = return_sizing_variants(
            variant_from_result_row(select_return_row(signal_rows))
        )
    elif args.phase == "return_holdout":
        phase_specs = return_holdout_variants(
            load_return_frozen_variant(output_dir)
        )
    else:
        phase_specs = return_robustness_variants(
            load_return_frozen_variant(output_dir)
        )
    rows: list[dict[str, object]] = []
    for trial_index, spec in enumerate(phase_specs, start=1):
        print(
            f"{args.phase.title()} trial {trial_index}/{len(phase_specs)}: {spec.name}",
            flush=True,
        )
        config = build_variant_config(
            base,
            spec,
            start_date=args.start_date,
            end_date=args.end_date,
            output_dir=output_dir / args.phase / spec.name,
        )
        result = run_cached_variant(
            config,
            daily_bars=daily_bars,
            cached_minutes=cached_minutes,
            baseline_pairs=baseline_candidate_pairs,
        )
        outcomes = trade_outcomes(result.trades)
        overall = summarize_outcomes(outcomes)
        q1 = summarize_outcomes(
            outcomes,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 3, 31),
        )
        q2 = summarize_outcomes(
            outcomes,
            start_date=date(2025, 4, 1),
            end_date=date(2025, 6, 30),
        )
        q3 = summarize_outcomes(
            outcomes,
            start_date=date(2025, 7, 1),
            end_date=date(2025, 9, 30),
        )
        q4 = summarize_outcomes(
            outcomes,
            start_date=HOLDOUT_START,
            end_date=HOLDOUT_END,
        )
        row = {
            "trial_index": trial_index,
            "name": spec.name,
            "description": spec.description,
            "trade_count": overall.trade_count,
            "win_rate": overall.win_rate,
            "profit_factor": overall.profit_factor,
            "realized_pnl": overall.realized_pnl,
            "average_pnl": overall.average_pnl,
            "average_return_pct": overall.average_return_pct,
            "payoff_ratio": overall.payoff_ratio,
            "backtest_return_pct": result.stats.return_pct,
            "max_drawdown_pct": result.stats.max_drawdown_pct,
            "q1_trades": q1.trade_count,
            "q1_win_rate": q1.win_rate,
            "q1_profit_factor": q1.profit_factor,
            "q1_realized_pnl": q1.realized_pnl,
            "q2_trades": q2.trade_count,
            "q2_win_rate": q2.win_rate,
            "q2_profit_factor": q2.profit_factor,
            "q2_realized_pnl": q2.realized_pnl,
            "q3_trades": q3.trade_count,
            "q3_win_rate": q3.win_rate,
            "q3_profit_factor": q3.profit_factor,
            "q3_realized_pnl": q3.realized_pnl,
            "q4_trades": q4.trade_count,
            "q4_win_rate": q4.win_rate,
            "q4_profit_factor": q4.profit_factor,
            "q4_realized_pnl": q4.realized_pnl,
            "spec": {
                "watchlist_signal_params": spec.watchlist_signal_params,
                "buy_signal_params": spec.buy_signal_params,
                "optimization_rules": spec.optimization_rules,
                "stop_loss_pct": spec.stop_loss_pct,
                "stop_loss_limit_pct": spec.stop_loss_limit_pct,
                "take_profit_pct": spec.take_profit_pct,
                "take_profit_sell_fraction": spec.take_profit_sell_fraction,
                "buy_notional_usd": spec.buy_notional_usd,
                "max_positions": spec.max_positions,
                "max_daily_buys": spec.max_daily_buys,
                "commission_per_order": spec.commission_per_order,
                "slippage_pct": spec.slippage_pct,
            },
        }
        rows.append(row)
        print(
            f"{spec.name}: trades={overall.trade_count} "
            f"win={overall.win_rate:.2%} pf={overall.profit_factor:.3f} "
            f"return={result.stats.return_pct:.2%} "
            f"q1/q2/q3_pnl=${q1.realized_pnl:,.0f}/"
            f"${q2.realized_pnl:,.0f}/${q3.realized_pnl:,.0f}",
            flush=True,
        )

    stage_dir = output_dir / args.phase
    stage_dir.mkdir(parents=True, exist_ok=True)
    json_path = stage_dir / f"{args.phase}_results.json"
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    csv_path = stage_dir / f"{args.phase}_results.csv"
    csv_fields = [key for key in rows[0] if key != "spec"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(
            {key: value for key, value in row.items() if key != "spec"}
            for row in rows
        )
    print(f"{args.phase.title()} JSON: {json_path}")
    print(f"{args.phase.title()} CSV: {csv_path}")


def read_result_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise FileNotFoundError(f"Required development results are missing: {path}")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"Research result file has no rows: {path}")
    return rows


def write_return_frozen_selection_manifest(output_dir: Path) -> Path:
    """Freeze the net-return winner before evaluating its Q4 outcomes."""
    signal_rows = read_result_rows(
        output_dir / "return_signal" / "return_signal_results.json"
    )
    sizing_rows = read_result_rows(
        output_dir / "return_sizing" / "return_sizing_results.json"
    )
    signal = select_return_row(signal_rows)
    candidate = select_return_row(sizing_rows)
    spec = variant_from_result_row(candidate)
    if (
        float(spec.buy_notional_usd or 0.0)
        * int(spec.max_positions or 0)
        > 100_000.0
    ):
        raise RuntimeError("Frozen return candidate exceeds the cash-only budget")
    manifest = {
        "selection_status": "frozen_before_return_holdout",
        "objective": (
            "Maximum 2025-01-01..2025-09-30 portfolio return after 10 bps "
            "slippage per fill, subject to frozen robustness guardrails"
        ),
        "selected_from": "2025-01-01..2025-09-30 development only",
        "signal_candidate_name": signal["name"],
        "candidate_name": candidate["name"],
        "candidate_trade_count": candidate["trade_count"],
        "candidate_win_rate": candidate["win_rate"],
        "candidate_profit_factor": candidate["profit_factor"],
        "candidate_backtest_return_pct": candidate["backtest_return_pct"],
        "candidate_max_drawdown_pct": candidate["max_drawdown_pct"],
        "candidate_quarter_pnl": {
            quarter: candidate[f"{quarter}_realized_pnl"]
            for quarter in ("q1", "q2", "q3")
        },
        "candidate_spec": candidate["spec"],
        "guardrails": {
            "initial_cash_usd": 100_000.0,
            "leverage_allowed": False,
            "primary_slippage_per_fill": RETURN_PRIMARY_SLIPPAGE_PCT,
            "minimum_trade_count": RETURN_MIN_TRADE_COUNT,
            "minimum_profit_factor": RETURN_MIN_PROFIT_FACTOR,
            "maximum_drawdown_floor_pct": RETURN_MAX_DRAWDOWN_PCT,
            "all_development_quarters_must_be_profitable": True,
        },
        "holdout_window": f"{HOLDOUT_START.isoformat()}..{HOLDOUT_END.isoformat()}",
        "holdout_adjustment_policy": "No parameter changes after holdout results",
        "holdout_reuse_note": (
            "Q4 was previously opened only for the legacy baseline and the "
            "separate win-rate candidate; this return candidate was frozen "
            "without reading its Q4 outcomes"
        ),
        "locked_cross_validation_year": LOCKED_CROSS_VALIDATION_YEAR,
        "return_signal_trial_count": len(return_signal_variants()),
        "return_sizing_trial_count": len(
            return_sizing_variants(variant_from_result_row(signal))
        ),
    }
    path = output_dir / "return_frozen_selection_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Frozen return selection manifest: {path}", flush=True)
    return path


def load_return_frozen_variant(output_dir: Path):
    path = output_dir / "return_frozen_selection_manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"Frozen return manifest is missing: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return variant_from_result_row(
        {
            "name": manifest["candidate_name"],
            "description": manifest["objective"],
            "spec": manifest["candidate_spec"],
        }
    )


def write_frozen_selection_manifest(output_dir: Path) -> Path:
    """Persist the development-only selection before any Q4 minute fetch."""
    stage2_path = output_dir / "stage2" / "stage2_results.json"
    if not stage2_path.exists():
        raise FileNotFoundError(
            "Stage-two development results must exist before opening Q4: "
            f"{stage2_path}"
        )
    stage2_rows = json.loads(stage2_path.read_text(encoding="utf-8"))
    matches = [
        row for row in stage2_rows if row.get("name") == FROZEN_CANDIDATE_NAME
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one frozen-candidate row, found {len(matches)}"
        )
    candidate = matches[0]
    manifest = {
        "selection_status": "frozen_before_holdout",
        "selected_from": "2025-01-01..2025-09-30 development only",
        "candidate_name": FROZEN_CANDIDATE_NAME,
        "candidate_trade_count": candidate["trade_count"],
        "candidate_win_rate": candidate["win_rate"],
        "candidate_profit_factor": candidate["profit_factor"],
        "candidate_max_drawdown_pct": candidate["max_drawdown_pct"],
        "candidate_spec": candidate["spec"],
        "holdout_window": f"{HOLDOUT_START.isoformat()}..{HOLDOUT_END.isoformat()}",
        "holdout_adjustment_policy": "No parameter changes after holdout results",
        "locked_cross_validation_year": LOCKED_CROSS_VALIDATION_YEAR,
        "stage_one_trial_count": len(stage_one_variants()),
        "stage_two_trial_count": len(stage_two_variants()),
    }
    path = output_dir / "frozen_selection_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Frozen selection manifest: {path}", flush=True)
    return path


if __name__ == "__main__":
    main()
