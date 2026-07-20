"""Pre-registered optimization space for the current three-buy gap strategy.

The development runner may inspect only 2025-01-01 through 2025-09-30.
It freezes at most one candidate before Q4 or the locked 2026 comparison is
opened.  Every candidate either tightens or reorders the current-code universe.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from itertools import combinations

from backtest.gap_strategy_optimization import VariantSpec


DEVELOPMENT_START = date(2025, 1, 1)
DEVELOPMENT_END = date(2025, 9, 30)
DIAGNOSTIC_START = date(2025, 10, 1)
DIAGNOSTIC_END = date(2025, 12, 31)
LOCKED_2026_START = date(2026, 1, 1)
LOCKED_2026_END = date(2026, 7, 17)

INITIAL_CASH = 100_000.0
BUY_NOTIONAL_USD = 3_500.0
MAX_DAILY_BUYS = 3
MAX_POSITIONS = 3
SLIPPAGE_PCT = 0.001
MINIMUM_ROUNDS = 300


def current_code_baseline() -> VariantSpec:
    """Current signal/exit code under the user's fixed capital constraints."""
    return VariantSpec(
        name="current_code_daily3_10bps",
        description=(
            "Exact current gap strategy; $3,500 notional, at most three daily "
            "buys/positions, cash only, and 10 bps slippage per fill"
        ),
        buy_notional_usd=BUY_NOTIONAL_USD,
        max_positions=MAX_POSITIONS,
        max_daily_buys=MAX_DAILY_BUYS,
        slippage_pct=SLIPPAGE_PCT,
    )


def stage_one_variants() -> tuple[tuple[str, VariantSpec], ...]:
    """One-factor families exactly matching the pre-registration manifest."""
    baseline = current_code_baseline()
    variants: list[tuple[str, VariantSpec]] = [("baseline", baseline)]

    for ratio in (1.10, 1.12, 1.15, 1.18, 1.20):
        variants.append(
            (
                "signal_strength",
                _with_rules(
                    baseline,
                    f"min_close_ma5_{ratio:.2f}".replace(".", "_"),
                    f"Require signal close/MA5 >= {ratio:.2f}",
                    min_close_to_ma5_ratio=ratio,
                ),
            )
        )
    for gain in (0.20, 0.25, 0.30, 0.35):
        variants.append(
            (
                "signal_strength",
                _with_rules(
                    baseline,
                    f"max_signal_gain_{int(gain * 100)}",
                    f"Reject signal-day gains above {gain:.0%}",
                    max_signal_gain_pct=gain,
                ),
            )
        )
    for signal_range in (0.10, 0.15):
        variants.append(
            (
                "signal_strength",
                _with_rules(
                    baseline,
                    f"min_signal_range_{int(signal_range * 100)}",
                    f"Require signal-day range >= {signal_range:.0%}",
                    min_signal_range_pct=signal_range,
                ),
            )
        )
    for dollar_volume in (2_000_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000):
        variants.append(
            (
                "liquidity",
                _with_rules(
                    baseline,
                    f"max_dollar_volume_{dollar_volume // 1_000_000}m",
                    f"Reject signal-day dollar volume above ${dollar_volume:,.0f}",
                    max_signal_dollar_volume=float(dollar_volume),
                ),
            )
        )
    for sort_name in (
        "gain_asc_upper_asc",
        "upper_asc_gain_desc",
        "close_to_ma5_asc_gain_desc",
        "close_to_ma5_desc_gain_desc",
        "close_position_desc_gain_desc",
        "range_desc_gain_desc",
        "range_asc_gain_desc",
        "body_desc_upper_asc",
    ):
        variants.append(
            (
                "candidate_ranking",
                _with_rules(
                    baseline,
                    f"sort_{sort_name}",
                    f"Rank eligible candidates by {sort_name}",
                    candidate_sort=sort_name,
                ),
            )
        )
    for limit in (3, 5, 10, 20):
        variants.append(
            (
                "ranked_pool_capacity",
                _with_rules(
                    baseline,
                    f"watchlist_limit_{limit}",
                    f"Keep only the top {limit} signal-day candidates",
                    max_watchlist_candidates=limit,
                ),
            )
        )
    for premium in (0.01, 0.02):
        variants.append(
            (
                "entry_quality",
                replace(
                    baseline,
                    name=f"entry_above_dynamic_ma5_{int(premium * 100)}",
                    description=(
                        f"Require entry price to remain at least {premium:.0%} "
                        "above the dynamic MA5"
                    ),
                    buy_signal_params={
                        "MIN_CURRENT_VS_TODAY_MA5_PCT": premium,
                    },
                ),
            )
        )
    return tuple(variants)


ALLOWED_STAGE_TWO_FAMILIES = (
    ("signal_strength", "liquidity"),
    ("signal_strength", "candidate_ranking"),
    ("signal_strength", "ranked_pool_capacity"),
    ("liquidity", "candidate_ranking"),
    ("liquidity", "ranked_pool_capacity"),
    ("candidate_ranking", "ranked_pool_capacity"),
    ("signal_strength", "liquidity", "candidate_ranking"),
    (
        "signal_strength",
        "liquidity",
        "candidate_ranking",
        "ranked_pool_capacity",
    ),
)


def family_champions(
    rows: list[dict[str, object]],
) -> dict[str, VariantSpec]:
    """Pick one development-only champion per family under fixed eligibility."""
    baseline = next(row for row in rows if row["family"] == "baseline")
    eligible: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        family = str(row["family"])
        if family == "baseline":
            continue
        if int(row["trade_count"]) < MINIMUM_ROUNDS:
            continue
        if float(row["backtest_return_pct"]) <= float(baseline["backtest_return_pct"]):
            continue
        if float(row["win_rate"]) <= float(baseline["win_rate"]):
            continue
        if any(float(row[f"{quarter}_realized_pnl"]) <= 0 for quarter in ("q1", "q2", "q3")):
            continue
        eligible.setdefault(family, []).append(row)
    champions: dict[str, VariantSpec] = {}
    for family, family_rows in eligible.items():
        winner = max(
            family_rows,
            key=lambda row: (
                float(row["backtest_return_pct"]),
                float(row["win_rate"]),
                str(row["name"]),
            ),
        )
        champions[family] = spec_from_dict(
            str(winner["name"]),
            str(winner["description"]),
            winner["spec"],
        )
    return champions


def stage_two_variants(
    champions: dict[str, VariantSpec],
) -> tuple[tuple[str, VariantSpec], ...]:
    """Merge only the pre-registered family combinations, capped at eight."""
    variants: list[tuple[str, VariantSpec]] = []
    for families in ALLOWED_STAGE_TWO_FAMILIES:
        if not all(family in champions for family in families):
            continue
        pieces = [champions[family] for family in families]
        name = "combo__" + "__".join(piece.name for piece in pieces)
        variants.append(
            (
                "+".join(families),
                merge_specs(
                    name,
                    "; ".join(piece.description for piece in pieces),
                    pieces,
                ),
            )
        )
    return tuple(variants[:8])


def merge_specs(
    name: str,
    description: str,
    specs: list[VariantSpec],
) -> VariantSpec:
    baseline = current_code_baseline()
    watchlist_signal_params: dict[str, float] = {}
    buy_signal_params: dict[str, float] = {}
    optimization_rules: dict[str, object] = {}
    for spec in specs:
        watchlist_signal_params.update(spec.watchlist_signal_params)
        buy_signal_params.update(spec.buy_signal_params)
        optimization_rules.update(spec.optimization_rules)
    return replace(
        baseline,
        name=name,
        description=description,
        watchlist_signal_params=watchlist_signal_params,
        buy_signal_params=buy_signal_params,
        optimization_rules=optimization_rules,
    )


def spec_to_dict(spec: VariantSpec) -> dict[str, object]:
    return {
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
    }


def spec_from_dict(
    name: str,
    description: str,
    raw: object,
) -> VariantSpec:
    values = dict(raw) if isinstance(raw, dict) else {}
    return VariantSpec(
        name=name,
        description=description,
        watchlist_signal_params=dict(values.get("watchlist_signal_params") or {}),
        buy_signal_params=dict(values.get("buy_signal_params") or {}),
        optimization_rules=dict(values.get("optimization_rules") or {}),
        stop_loss_pct=values.get("stop_loss_pct"),
        stop_loss_limit_pct=values.get("stop_loss_limit_pct"),
        take_profit_pct=values.get("take_profit_pct"),
        take_profit_sell_fraction=values.get("take_profit_sell_fraction"),
        buy_notional_usd=values.get("buy_notional_usd"),
        max_positions=values.get("max_positions"),
        max_daily_buys=values.get("max_daily_buys"),
        commission_per_order=values.get("commission_per_order"),
        slippage_pct=values.get("slippage_pct"),
    )


def validate_phase_window(phase: str, start: date, end: date) -> None:
    expected = {
        "development": (DEVELOPMENT_START, DEVELOPMENT_END),
        "diagnostic": (DIAGNOSTIC_START, DIAGNOSTIC_END),
        "validate-2026": (LOCKED_2026_START, LOCKED_2026_END),
    }
    if phase not in expected:
        raise ValueError(f"Unknown phase: {phase}")
    if (start, end) != expected[phase]:
        raise ValueError(
            f"{phase} is frozen to {expected[phase][0]}..{expected[phase][1]}"
        )
    if phase == "development" and end >= LOCKED_2026_START:
        raise ValueError("Development selection may not read the locked 2026 data")


def _with_rules(
    baseline: VariantSpec,
    name: str,
    description: str,
    **rules: object,
) -> VariantSpec:
    return replace(
        baseline,
        name=name,
        description=description,
        optimization_rules=dict(rules),
    )
