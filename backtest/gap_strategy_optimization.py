"""Reproducible, leakage-aware research helpers for the gap pullback strategy.

This module is intentionally isolated from the live monitor.  It reads the
official daily database, stores SIP minute bars in a separate cache, and runs
historical variants through the existing backtest engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from alpaca_ma5_service.afterhours_high_low import MinuteBar, regular_session_bounds
from alpaca_ma5_service.config import BASE_DIR
from alpaca_ma5_service.watchlist import to_alpaca_symbol
from backtest.data_cache import ADJUSTMENT_SPLIT, MarketDataCache
from backtest.engine import (
    BacktestConfig,
    BacktestResult,
    TradeRecord,
    build_historical_watchlists,
    run_backtest,
)
from backtest.signal_dynamic_ma5 import StrictAlpacaMinuteFetcher


RESEARCH_YEAR = 2025
DEVELOPMENT_START = date(2025, 1, 1)
DEVELOPMENT_END = date(2025, 9, 30)
HOLDOUT_START = date(2025, 10, 1)
HOLDOUT_END = date(2025, 12, 31)
LOCKED_CROSS_VALIDATION_YEAR = 2026
FROZEN_CANDIDATE_NAME = "pullback_5_take_profit_4"
RETURN_PRIMARY_SLIPPAGE_PCT = 0.0010
RETURN_STRESS_SLIPPAGE_PCT = 0.0025
RETURN_MIN_TRADE_COUNT = 500
RETURN_MIN_PROFIT_FACTOR = 1.10
RETURN_MAX_DRAWDOWN_PCT = -0.15
MARKET_TZ = ZoneInfo("America/New_York")
MINUTE_CACHE_PATH = (
    BASE_DIR / "backtest" / "data" / "gap_strategy_2025_minute_cache.sqlite"
)


@dataclass(frozen=True)
class VariantSpec:
    """One deliberately bounded strategy variant."""

    name: str
    description: str
    watchlist_signal_params: dict[str, float] = field(default_factory=dict)
    buy_signal_params: dict[str, float] = field(default_factory=dict)
    optimization_rules: dict[str, object] = field(default_factory=dict)
    stop_loss_pct: float | None = None
    stop_loss_limit_pct: float | None = None
    take_profit_pct: float | None = None
    take_profit_sell_fraction: float | None = None
    buy_notional_usd: float | None = None
    max_positions: int | None = None
    max_daily_buys: int | None = None
    commission_per_order: float | None = None
    slippage_pct: float | None = None


@dataclass(frozen=True)
class TradeOutcome:
    symbol: str
    entry_date: date
    exit_date: date
    realized_pnl: float
    invested: float

    @property
    def return_pct(self) -> float:
        return self.realized_pnl / self.invested if self.invested > 0 else 0.0


@dataclass(frozen=True)
class OutcomeStats:
    trade_count: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float
    realized_pnl: float
    average_pnl: float
    average_return_pct: float
    profit_factor: float
    average_win: float
    average_loss: float
    payoff_ratio: float


def stage_one_variants() -> tuple[VariantSpec, ...]:
    """Pre-registered, low-dimensional development variants.

    Every non-baseline variant either tightens an existing rule or changes one
    execution/risk parameter.  Combinations are deliberately excluded from this
    first stage so their marginal effects remain inspectable.
    """
    return (
        VariantSpec("baseline", "Frozen pre-optimization baseline"),
        VariantSpec(
            "pullback_3",
            "Require at least a 3% pullback from the signal close",
            buy_signal_params={"MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.03},
        ),
        VariantSpec(
            "pullback_4",
            "Require at least a 4% pullback from the signal close",
            buy_signal_params={"MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.04},
        ),
        VariantSpec(
            "pullback_5",
            "Require at least a 5% pullback from the signal close",
            buy_signal_params={"MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.05},
        ),
        VariantSpec(
            "pullback_6",
            "Require at least a 6% pullback from the signal close",
            buy_signal_params={"MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.06},
        ),
        VariantSpec(
            "close_position_60",
            "Signal close must finish in the top 40% of its daily range",
            optimization_rules={"min_signal_close_position_pct": 0.60},
        ),
        VariantSpec(
            "close_position_75",
            "Signal close must finish in the top 25% of its daily range",
            optimization_rules={"min_signal_close_position_pct": 0.75},
        ),
        VariantSpec(
            "close_position_90",
            "Signal close must finish in the top 10% of its daily range",
            optimization_rules={"min_signal_close_position_pct": 0.90},
        ),
        VariantSpec(
            "signal_rvol_1",
            "Signal-day volume must be at least its prior-20-day average",
            optimization_rules={"min_signal_volume_to_avg20": 1.0},
        ),
        VariantSpec(
            "signal_rvol_2",
            "Signal-day volume must be at least 2x its prior-20-day average",
            optimization_rules={"min_signal_volume_to_avg20": 2.0},
        ),
        VariantSpec(
            "signal_rvol_5",
            "Signal-day volume must be at least 5x its prior-20-day average",
            optimization_rules={"min_signal_volume_to_avg20": 5.0},
        ),
        VariantSpec(
            "signal_range_min_10",
            "Signal-day true high-low range must be at least 10%",
            optimization_rules={"min_signal_range_pct": 0.10},
        ),
        VariantSpec(
            "signal_dollar_volume_5m",
            "Signal-day dollar volume must be at least $5 million",
            optimization_rules={"min_signal_dollar_volume": 5_000_000.0},
        ),
        VariantSpec(
            "atr20_min_5pct",
            "Twenty-day average true range must be at least 5%",
            optimization_rules={"min_signal_atr20_pct": 0.05},
        ),
        VariantSpec(
            "buy_after_0945",
            "Do not enter during the first 15 minutes",
            optimization_rules={"buy_time_start": "09:45"},
        ),
        VariantSpec(
            "buy_after_1000",
            "Do not enter during the first 30 minutes",
            optimization_rules={"buy_time_start": "10:00"},
        ),
        VariantSpec(
            "take_profit_7",
            "Seven-percent all-out profit target with the baseline stop",
            take_profit_pct=0.07,
        ),
        VariantSpec(
            "take_profit_6",
            "Six-percent all-out profit target with the baseline stop",
            take_profit_pct=0.06,
        ),
        VariantSpec(
            "take_profit_5",
            "Five-percent all-out profit target with the baseline stop",
            take_profit_pct=0.05,
        ),
        VariantSpec(
            "symmetric_6",
            "Six-percent stop and six-percent all-out profit target",
            stop_loss_pct=-0.06,
            stop_loss_limit_pct=-0.045,
            take_profit_pct=0.06,
        ),
        VariantSpec(
            "max_buys_5",
            "Concentrate on at most five entries per day",
            max_positions=5,
            max_daily_buys=5,
        ),
        VariantSpec(
            "max_buys_3",
            "Concentrate on at most three entries per day",
            max_positions=3,
            max_daily_buys=3,
        ),
        VariantSpec(
            "sort_upper_shadow",
            "At most five entries, prioritizing the smallest upper shadow",
            optimization_rules={"candidate_sort": "upper_asc_gain_desc"},
            max_positions=5,
            max_daily_buys=5,
        ),
        VariantSpec(
            "sort_close_to_ma5",
            "At most five entries, prioritizing less extended signal closes",
            optimization_rules={"candidate_sort": "close_to_ma5_asc_gain_desc"},
            max_positions=5,
            max_daily_buys=5,
        ),
        VariantSpec(
            "sort_body",
            "At most five entries, prioritizing the strongest bullish body",
            optimization_rules={"candidate_sort": "body_desc_upper_asc"},
            max_positions=5,
            max_daily_buys=5,
        ),
    )


def legacy_baseline_variant() -> VariantSpec:
    """The exact pre-optimization strategy named by the user's request."""
    return VariantSpec(
        name="baseline",
        description="Frozen pre-optimization baseline",
        buy_signal_params={"MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.02},
        take_profit_pct=0.08,
    )


def stage_two_variants() -> tuple[VariantSpec, ...]:
    """Six pre-registered combinations on the two broad stage-one plateaus.

    Stage one found monotonic improvement across 4%-6% pullbacks and across
    lower all-out profit targets.  This deliberately small rectangular grid
    tests their interaction without adding any of the weaker filters.
    """
    return tuple(
        VariantSpec(
            name=f"pullback_{pullback}_take_profit_{take_profit}",
            description=(
                f"Require a {pullback}% pullback and sell all at a "
                f"{take_profit}% profit target"
            ),
            buy_signal_params={
                "MAX_BUY_TODAY_CURRENT_GAIN_PCT": -pullback / 100.0,
            },
            take_profit_pct=take_profit / 100.0,
        )
        for take_profit in (5, 4)
        for pullback in (4, 5, 6)
    )


def frozen_candidate_variant() -> VariantSpec:
    """Return the candidate frozen before the 2025-Q4 holdout was opened."""
    matches = [
        variant
        for variant in stage_two_variants()
        if variant.name == FROZEN_CANDIDATE_NAME
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Frozen candidate must resolve exactly once: {FROZEN_CANDIDATE_NAME}"
        )
    return matches[0]


def holdout_variants() -> tuple[VariantSpec, ...]:
    """The untouched baseline and one frozen candidate, with no Q4 selection."""
    return (
        legacy_baseline_variant(),
        frozen_candidate_variant(),
    )


def robustness_variants() -> tuple[VariantSpec, ...]:
    """Post-selection transaction-cost checks; never used for selection."""
    frozen = frozen_candidate_variant()
    return (
        replace(
            frozen,
            name="frozen_zero_cost",
            description="Frozen candidate with the baseline zero-cost assumption",
        ),
        replace(
            frozen,
            name="frozen_slippage_10bps",
            description="Frozen candidate with 10 bps slippage on each fill",
            slippage_pct=0.0010,
        ),
        replace(
            frozen,
            name="frozen_slippage_25bps",
            description="Frozen candidate with 25 bps slippage on each fill",
            slippage_pct=0.0025,
        ),
    )


def return_signal_variants() -> tuple[VariantSpec, ...]:
    """Pre-registered return-focused signal grid selected net of 10 bps/fill.

    The grid only combines the two development-only plateaus supported by the
    earlier one-factor study: a baseline/4% pullback, an optional 60% signal
    close-position filter, and 4%/5%/8% all-out profit targets.  It deliberately
    excludes weaker filters and intermediate values to keep the second research
    objective bounded after the win-rate study.
    """
    variants: list[VariantSpec] = []
    for pullback in (2, 4):
        for close_position in (None, 0.60):
            for take_profit in (4, 5, 8):
                close_name = "none" if close_position is None else "60"
                rules = (
                    {}
                    if close_position is None
                    else {"min_signal_close_position_pct": close_position}
                )
                variants.append(
                    VariantSpec(
                        name=(
                            f"return_pb{pullback}_close{close_name}"
                            f"_take_profit_{take_profit}"
                        ),
                        description=(
                            f"{pullback}% pullback, "
                            f"{'no close-position filter' if close_position is None else 'signal close in the top 40% of its range'}, "
                            f"{take_profit}% all-out target, 10 bps slippage per fill"
                        ),
                        buy_signal_params={
                            "MAX_BUY_TODAY_CURRENT_GAIN_PCT": -pullback / 100.0,
                        },
                        optimization_rules=rules,
                        take_profit_pct=take_profit / 100.0,
                        buy_notional_usd=3_500.0,
                        max_positions=10,
                        max_daily_buys=10,
                        slippage_pct=RETURN_PRIMARY_SLIPPAGE_PCT,
                    )
                )
    return tuple(variants)


def select_return_row(rows: Iterable[dict[str, object]]) -> dict[str, object]:
    """Select maximum net development return subject to frozen guardrails."""
    eligible: list[dict[str, object]] = []
    for row in rows:
        if int(row.get("trade_count", 0)) < RETURN_MIN_TRADE_COUNT:
            continue
        if float(row.get("profit_factor", 0.0)) < RETURN_MIN_PROFIT_FACTOR:
            continue
        if float(row.get("max_drawdown_pct", -1.0)) < RETURN_MAX_DRAWDOWN_PCT:
            continue
        if any(
            float(row.get(f"{quarter}_realized_pnl", 0.0)) <= 0.0
            for quarter in ("q1", "q2", "q3")
        ):
            continue
        eligible.append(row)
    if not eligible:
        raise RuntimeError(
            "No return candidate satisfies the pre-registered trade-count, "
            "profit-factor, drawdown, and positive-quarter guardrails"
        )
    return max(
        eligible,
        key=lambda row: (
            float(row.get("backtest_return_pct", float("-inf"))),
            float(row.get("max_drawdown_pct", float("-inf"))),
            int(row.get("trade_count", 0)),
            str(row.get("name", "")),
        ),
    )


def variant_from_result_row(row: dict[str, object]) -> VariantSpec:
    """Rehydrate a variant from a persisted research result row."""
    raw = row.get("spec")
    if not isinstance(raw, dict):
        raise ValueError("Research result row is missing its variant spec")
    return VariantSpec(
        name=str(row["name"]),
        description=str(row.get("description", row["name"])),
        watchlist_signal_params=dict(raw.get("watchlist_signal_params", {})),
        buy_signal_params=dict(raw.get("buy_signal_params", {})),
        optimization_rules=dict(raw.get("optimization_rules", {})),
        stop_loss_pct=raw.get("stop_loss_pct"),
        stop_loss_limit_pct=raw.get("stop_loss_limit_pct"),
        take_profit_pct=raw.get("take_profit_pct"),
        take_profit_sell_fraction=raw.get("take_profit_sell_fraction"),
        buy_notional_usd=raw.get("buy_notional_usd"),
        max_positions=raw.get("max_positions"),
        max_daily_buys=raw.get("max_daily_buys"),
        commission_per_order=raw.get("commission_per_order"),
        slippage_pct=raw.get("slippage_pct"),
    )


def return_sizing_variants(signal_variant: VariantSpec) -> tuple[VariantSpec, ...]:
    """Pre-registered cash-only capital allocations for one frozen signal."""
    allocations = (
        ("notional_3500_slots_10", 3_500.0, 10),
        ("notional_5000_slots_10", 5_000.0, 10),
        ("notional_7500_slots_10", 7_500.0, 10),
        ("notional_10000_slots_10", 10_000.0, 10),
        ("notional_10000_slots_5", 10_000.0, 5),
        ("notional_15000_slots_5", 15_000.0, 5),
        ("notional_20000_slots_5", 20_000.0, 5),
    )
    return tuple(
        replace(
            signal_variant,
            name=f"{signal_variant.name}_{suffix}",
            description=(
                f"{signal_variant.description}; ${notional:,.0f} per entry, "
                f"{slots} cash-only concurrent/daily slots"
            ),
            buy_notional_usd=notional,
            max_positions=slots,
            max_daily_buys=slots,
            slippage_pct=RETURN_PRIMARY_SLIPPAGE_PCT,
        )
        for suffix, notional, slots in allocations
    )


def return_holdout_variants(frozen: VariantSpec) -> tuple[VariantSpec, ...]:
    """Cost-matched baseline and frozen candidate for one Q4 evaluation."""
    return (
        replace(
            legacy_baseline_variant(),
            name="return_baseline_primary_10bps",
            description="Legacy baseline with 10 bps slippage per fill",
            slippage_pct=RETURN_PRIMARY_SLIPPAGE_PCT,
        ),
        replace(
            frozen,
            name="return_frozen_primary_10bps",
            description=(
                f"{frozen.description}; frozen candidate at 10 bps per fill"
            ),
            slippage_pct=RETURN_PRIMARY_SLIPPAGE_PCT,
        ),
    )


def return_robustness_variants(frozen: VariantSpec) -> tuple[VariantSpec, ...]:
    """Full-year cost sensitivity for the frozen return candidate."""
    return (
        replace(
            legacy_baseline_variant(),
            name="return_baseline_primary_10bps",
            description="Legacy baseline with 10 bps slippage per fill",
            slippage_pct=RETURN_PRIMARY_SLIPPAGE_PCT,
        ),
        replace(
            legacy_baseline_variant(),
            name="return_baseline_matched_20000_slots_5",
            description=(
                "Post-selection attribution control: legacy signals with the "
                "frozen candidate's $20,000 notional, five slots, and 10 bps "
                "slippage per fill"
            ),
            buy_notional_usd=20_000.0,
            max_positions=5,
            max_daily_buys=5,
            slippage_pct=RETURN_PRIMARY_SLIPPAGE_PCT,
        ),
        replace(
            frozen,
            name="return_frozen_zero_cost",
            description="Frozen return candidate with zero modeled slippage",
            slippage_pct=0.0,
        ),
        replace(
            frozen,
            name="return_frozen_primary_10bps",
            description="Frozen return candidate with 10 bps slippage per fill",
            slippage_pct=RETURN_PRIMARY_SLIPPAGE_PCT,
        ),
        replace(
            frozen,
            name="return_frozen_stress_25bps",
            description="Frozen return candidate with 25 bps slippage per fill",
            slippage_pct=RETURN_STRESS_SLIPPAGE_PCT,
        ),
    )


def validate_research_period(
    phase: str,
    start_date: date,
    end_date: date,
) -> None:
    """Fail closed on accidental holdout or 2026 cross-validation access."""
    if start_date > end_date:
        raise ValueError("Research start date must not be after end date")
    if end_date >= date(LOCKED_CROSS_VALIDATION_YEAR, 1, 1):
        raise ValueError(
            f"{LOCKED_CROSS_VALIDATION_YEAR} is locked for external "
            "cross-validation and cannot be opened by this research runner"
        )
    expected = {
        "baseline": (DEVELOPMENT_START, DEVELOPMENT_END),
        "stage1": (DEVELOPMENT_START, DEVELOPMENT_END),
        "stage2": (DEVELOPMENT_START, DEVELOPMENT_END),
        "holdout": (HOLDOUT_START, HOLDOUT_END),
        "robustness": (DEVELOPMENT_START, HOLDOUT_END),
        "return_signal": (DEVELOPMENT_START, DEVELOPMENT_END),
        "return_sizing": (DEVELOPMENT_START, DEVELOPMENT_END),
        "return_holdout": (HOLDOUT_START, HOLDOUT_END),
        "return_robustness": (DEVELOPMENT_START, HOLDOUT_END),
    }
    if phase not in expected:
        raise ValueError(f"Unknown research phase: {phase}")
    expected_start, expected_end = expected[phase]
    if (start_date, end_date) != (expected_start, expected_end):
        raise ValueError(
            f"{phase} must use its frozen window "
            f"{expected_start.isoformat()}..{expected_end.isoformat()}"
        )


def build_variant_config(
    base: BacktestConfig,
    spec: VariantSpec,
    *,
    start_date: date,
    end_date: date,
    output_dir: Path,
    html_report_name: str = "",
) -> BacktestConfig:
    """Apply one variant without mutating process-global strategy constants."""
    stop_loss_pct = (
        base.strategy_settings.stop_loss_pct
        if spec.stop_loss_pct is None
        else spec.stop_loss_pct
    )
    stop_loss_limit_pct = (
        base.strategy_settings.stop_loss_limit_pct
        if spec.stop_loss_limit_pct is None
        else spec.stop_loss_limit_pct
    )
    take_profit_pct = (
        base.strategy_settings.take_profit_half_pct
        if spec.take_profit_pct is None
        else spec.take_profit_pct
    )
    take_profit_sell_fraction = (
        base.strategy_settings.take_profit_sell_fraction
        if spec.take_profit_sell_fraction is None
        else spec.take_profit_sell_fraction
    )
    buy_notional_usd = (
        base.buy_notional_usd
        if spec.buy_notional_usd is None
        else spec.buy_notional_usd
    )
    max_positions = base.max_positions if spec.max_positions is None else spec.max_positions
    max_daily_buys = (
        base.max_daily_buys if spec.max_daily_buys is None else spec.max_daily_buys
    )
    strategy_settings = replace(
        base.strategy_settings,
        output_dir=output_dir,
        state_file=output_dir / "state.json",
        buy_notional_usd=buy_notional_usd,
        max_daily_buys=max_daily_buys,
        stop_loss_pct=stop_loss_pct,
        stop_loss_limit_pct=stop_loss_limit_pct,
        take_profit_half_pct=take_profit_pct,
        take_profit_sell_fraction=take_profit_sell_fraction,
        take_profit_remainder_stop_pct=(
            take_profit_pct if take_profit_sell_fraction >= 1.0 else None
        ),
    )
    return replace(
        base,
        start_date=start_date,
        end_date=end_date,
        output_dir=output_dir,
        html_report_name=html_report_name,
        buy_notional_usd=buy_notional_usd,
        max_positions=max_positions,
        max_daily_buys=max_daily_buys,
        commission_per_order=(
            base.commission_per_order
            if spec.commission_per_order is None
            else spec.commission_per_order
        ),
        slippage_pct=(
            base.slippage_pct if spec.slippage_pct is None else spec.slippage_pct
        ),
        strategy_settings=strategy_settings,
        watchlist_signal_params={
            **base.watchlist_signal_params,
            **spec.watchlist_signal_params,
        },
        buy_signal_params={
            **base.buy_signal_params,
            **spec.buy_signal_params,
        },
        stop_params={
            **base.stop_params,
            "stop_loss_pct": stop_loss_pct,
            "stop_loss_limit_pct": stop_loss_limit_pct,
            "take_profit_half_pct": take_profit_pct,
            "take_profit_sell_fraction": take_profit_sell_fraction,
            "take_profit_remainder_stop_pct": (
                take_profit_pct if take_profit_sell_fraction >= 1.0 else None
            ),
        },
        optimization_rules={
            **base.optimization_rules,
            **spec.optimization_rules,
        },
        strategy_variant_name=spec.name,
        strategy_variant_description=spec.description,
    )


def candidate_pairs(watchlists: dict[str, list[str]]) -> set[tuple[str, str]]:
    return {
        (day_key, to_alpaca_symbol(symbol))
        for day_key, symbols in watchlists.items()
        for symbol in symbols
        if to_alpaca_symbol(symbol)
    }


def require_candidate_subset(
    watchlists: dict[str, list[str]],
    baseline_pairs: set[tuple[str, str]],
) -> None:
    """Fail closed if a research variant expands beyond the cached baseline."""
    extras = candidate_pairs(watchlists).difference(baseline_pairs)
    if extras:
        preview = ", ".join(f"{day}:{symbol}" for day, symbol in sorted(extras)[:10])
        raise ValueError(
            "Optimization variant expands beyond the pre-registered baseline "
            f"candidate universe: extras={len(extras)} sample=[{preview}]"
        )


def ensure_strict_sip_minute_cache(
    watchlists: dict[str, list[str]],
    *,
    cache_path: Path = MINUTE_CACHE_PATH,
    progress: Callable[[str], None] | None = print,
    minute_fetcher: Callable[
        [list[str], object, object], dict[str, list[MinuteBar]]
    ]
    | None = None,
) -> None:
    """Cache regular-session SIP minutes without an IEX fallback."""
    cache = MarketDataCache(cache_path)
    fetcher = minute_fetcher or StrictAlpacaMinuteFetcher(feed="sip", batch_size=100)
    active_days = [
        (date.fromisoformat(day_key), sorted(set(symbols)))
        for day_key, symbols in sorted(watchlists.items())
        if symbols
    ]
    for day_index, (session_date, symbols) in enumerate(active_days, start=1):
        start, end = regular_session_bounds(session_date, MARKET_TZ)
        start_key = start.astimezone(UTC).isoformat(timespec="seconds")
        end_key = end.astimezone(UTC).isoformat(timespec="seconds")
        missing = cache.uncovered_symbols(
            "minute",
            symbols,
            start_key,
            end_key,
            feed="sip",
            adjustment=ADJUSTMENT_SPLIT,
        )
        if not missing:
            continue
        if progress:
            progress(
                f"SIP minute cache miss {session_date}: {len(missing):,} symbols "
                f"({day_index}/{len(active_days)})"
            )
        fetched = fetcher(missing, start, end)
        cache.save_minute_bars(
            fetched,
            feed="sip",
            range_start=start,
            range_end=end,
            covered_symbols=missing,
            adjustment=ADJUSTMENT_SPLIT,
        )


def load_cached_candidate_minutes(
    watchlists: dict[str, list[str]],
    *,
    cache_path: Path = MINUTE_CACHE_PATH,
) -> dict[str, list[MinuteBar]]:
    cache = MarketDataCache(cache_path, read_only=True)
    bars_by_symbol: dict[str, list[MinuteBar]] = {}
    for day_key, symbols in sorted(watchlists.items()):
        if not symbols:
            continue
        session_date = date.fromisoformat(day_key)
        start, end = regular_session_bounds(session_date, MARKET_TZ)
        loaded = cache.load_minute_bars(
            sorted(set(symbols)),
            start,
            end,
            feed="sip",
            adjustment=ADJUSTMENT_SPLIT,
        )
        for symbol, bars in loaded.items():
            bars_by_symbol.setdefault(symbol, []).extend(bars)
    return bars_by_symbol


def run_cached_variant(
    config: BacktestConfig,
    *,
    daily_bars,
    cached_minutes: dict[str, list[MinuteBar]],
    baseline_pairs: set[tuple[str, str]],
) -> BacktestResult:
    watchlists = build_historical_watchlists(daily_bars, config)
    require_candidate_subset(watchlists, baseline_pairs)
    return run_backtest(
        config,
        bars_by_symbol=cached_minutes,
        daily_bars=daily_bars,
        historical_watchlists=watchlists,
    )


def trade_outcomes(trades: Iterable[TradeRecord]) -> list[TradeOutcome]:
    """Aggregate partial exits into one outcome per completed position."""
    open_rounds: dict[str, dict[str, object]] = {}
    outcomes: list[TradeOutcome] = []
    for trade in sorted(trades, key=lambda item: (item.timestamp, item.symbol, item.side)):
        symbol = to_alpaca_symbol(trade.symbol)
        if trade.side == "BUY":
            open_rounds[symbol] = {
                "entry_date": trade.timestamp.date(),
                "quantity": float(trade.quantity),
                "remaining": float(trade.quantity),
                "invested": float(trade.gross_value + trade.fee),
                "pnl": 0.0,
            }
            continue
        if trade.side != "SELL" or symbol not in open_rounds:
            continue
        current = open_rounds[symbol]
        current["remaining"] = max(
            0.0,
            float(current["remaining"]) - float(trade.quantity),
        )
        current["pnl"] = float(current["pnl"]) + float(trade.realized_pnl)
        if float(current["remaining"]) > 1e-6:
            continue
        outcomes.append(
            TradeOutcome(
                symbol=symbol,
                entry_date=current["entry_date"],  # type: ignore[arg-type]
                exit_date=trade.timestamp.date(),
                realized_pnl=float(current["pnl"]),
                invested=float(current["invested"]),
            )
        )
        del open_rounds[symbol]
    return outcomes


def summarize_outcomes(
    outcomes: Iterable[TradeOutcome],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> OutcomeStats:
    selected = [
        outcome
        for outcome in outcomes
        if (start_date is None or outcome.exit_date >= start_date)
        and (end_date is None or outcome.exit_date <= end_date)
    ]
    pnls = [outcome.realized_pnl for outcome in selected]
    returns = [outcome.return_pct for outcome in selected]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_win = sum(wins) / len(wins) if wins else 0.0
    average_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    return OutcomeStats(
        trade_count=len(selected),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(selected) - len(wins) - len(losses),
        win_rate=len(wins) / len(selected) if selected else 0.0,
        realized_pnl=sum(pnls),
        average_pnl=sum(pnls) / len(pnls) if pnls else 0.0,
        average_return_pct=sum(returns) / len(returns) if returns else 0.0,
        profit_factor=(
            gross_profit / gross_loss
            if gross_loss > 0
            else float("inf") if gross_profit > 0 else 0.0
        ),
        average_win=average_win,
        average_loss=average_loss,
        payoff_ratio=average_win / average_loss if average_loss > 0 else 0.0,
    )
