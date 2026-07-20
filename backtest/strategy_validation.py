"""Small dependency-free statistics for backtest selection-bias checks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
import math
import random
from statistics import NormalDist
from typing import Iterable, Sequence

from backtest.gap_strategy_optimization import TradeOutcome


_NORMAL = NormalDist()
_EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class BootstrapDelta:
    observed_absolute_delta: float
    observed_relative_delta: float
    absolute_ci_low: float
    absolute_ci_high: float
    relative_ci_low: float
    relative_ci_high: float
    samples: int


@dataclass(frozen=True)
class PortfolioReturnBootstrap:
    baseline_return: float
    candidate_return: float
    observed_delta: float
    delta_ci_low: float
    delta_ci_high: float
    probability_candidate_outperforms: float
    samples: int


@dataclass(frozen=True)
class DeflatedSharpeResult:
    sharpe_ratio: float
    expected_max_sharpe: float
    probability: float
    observations: int
    trials: int


@dataclass(frozen=True)
class PboResult:
    probability_backtest_overfitting: float
    paths: int
    groups: int


def wilson_interval(
    successes: int,
    observations: int,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    if observations <= 0:
        return 0.0, 0.0
    alpha = 1.0 - confidence
    z = _NORMAL.inv_cdf(1.0 - alpha / 2.0)
    proportion = successes / observations
    denominator = 1.0 + z * z / observations
    center = (proportion + z * z / (2.0 * observations)) / denominator
    half_width = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / observations
            + z * z / (4.0 * observations * observations)
        )
        / denominator
    )
    return max(0.0, center - half_width), min(1.0, center + half_width)


def block_bootstrap_win_rate_delta(
    baseline: Iterable[TradeOutcome],
    candidate: Iterable[TradeOutcome],
    *,
    samples: int = 10_000,
    seed: int = 20250719,
    confidence: float = 0.95,
) -> BootstrapDelta:
    baseline_days = _daily_win_counts(baseline)
    candidate_days = _daily_win_counts(candidate)
    days = sorted(set(baseline_days) | set(candidate_days))
    if not days:
        return BootstrapDelta(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, samples)

    baseline_rate = _rate_from_counts(baseline_days.values())
    candidate_rate = _rate_from_counts(candidate_days.values())
    observed_absolute = candidate_rate - baseline_rate
    observed_relative = (
        candidate_rate / baseline_rate - 1.0 if baseline_rate > 0 else 0.0
    )
    rng = random.Random(seed)
    absolute_samples: list[float] = []
    relative_samples: list[float] = []
    for _ in range(samples):
        selected_days = [rng.choice(days) for _ in days]
        base_rate = _rate_from_counts(
            baseline_days.get(day, (0, 0)) for day in selected_days
        )
        candidate_rate_sample = _rate_from_counts(
            candidate_days.get(day, (0, 0)) for day in selected_days
        )
        absolute_samples.append(candidate_rate_sample - base_rate)
        relative_samples.append(
            candidate_rate_sample / base_rate - 1.0 if base_rate > 0 else 0.0
        )
    alpha = 1.0 - confidence
    return BootstrapDelta(
        observed_absolute_delta=observed_absolute,
        observed_relative_delta=observed_relative,
        absolute_ci_low=_quantile(absolute_samples, alpha / 2.0),
        absolute_ci_high=_quantile(absolute_samples, 1.0 - alpha / 2.0),
        relative_ci_low=_quantile(relative_samples, alpha / 2.0),
        relative_ci_high=_quantile(relative_samples, 1.0 - alpha / 2.0),
        samples=samples,
    )


def block_bootstrap_portfolio_return_delta(
    baseline: Iterable[TradeOutcome],
    candidate: Iterable[TradeOutcome],
    *,
    initial_cash: float,
    samples: int = 10_000,
    seed: int = 20250719,
    confidence: float = 0.95,
) -> PortfolioReturnBootstrap:
    """Resample exit days to compare fixed-capital portfolio returns."""
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    baseline_days = _daily_pnl(baseline)
    candidate_days = _daily_pnl(candidate)
    days = sorted(set(baseline_days) | set(candidate_days))
    if not days:
        return PortfolioReturnBootstrap(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, samples)

    baseline_return = sum(baseline_days.values()) / initial_cash
    candidate_return = sum(candidate_days.values()) / initial_cash
    observed_delta = candidate_return - baseline_return
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        selected_days = [rng.choice(days) for _ in days]
        delta = sum(
            candidate_days.get(day, 0.0) - baseline_days.get(day, 0.0)
            for day in selected_days
        ) / initial_cash
        deltas.append(delta)
    alpha = 1.0 - confidence
    return PortfolioReturnBootstrap(
        baseline_return=baseline_return,
        candidate_return=candidate_return,
        observed_delta=observed_delta,
        delta_ci_low=_quantile(deltas, alpha / 2.0),
        delta_ci_high=_quantile(deltas, 1.0 - alpha / 2.0),
        probability_candidate_outperforms=(
            sum(value > 0 for value in deltas) / len(deltas)
            if deltas
            else 0.0
        ),
        samples=samples,
    )


def deflated_sharpe_ratio(
    selected_returns: Sequence[float],
    trial_return_series: Sequence[Sequence[float]],
) -> DeflatedSharpeResult:
    """Bailey/Lopez de Prado DSR using unannualized period returns."""
    selected = [float(value) for value in selected_returns if math.isfinite(value)]
    trial_sharpes = [
        sharpe_ratio(values)
        for values in trial_return_series
        if len([value for value in values if math.isfinite(value)]) >= 2
    ]
    observed = sharpe_ratio(selected)
    trials = max(1, len(trial_sharpes))
    trial_dispersion = sample_stddev(trial_sharpes)
    expected_max = expected_max_sharpe(trials, trial_dispersion)
    if len(selected) < 3:
        probability = 0.0
    else:
        skewness, kurtosis = sample_skewness_kurtosis(selected)
        denominator_squared = (
            1.0
            - skewness * observed
            + ((kurtosis - 1.0) / 4.0) * observed * observed
        )
        if denominator_squared <= 0:
            probability = 0.0
        else:
            z_score = (
                (observed - expected_max)
                * math.sqrt(len(selected) - 1.0)
                / math.sqrt(denominator_squared)
            )
            probability = _NORMAL.cdf(z_score)
    return DeflatedSharpeResult(
        sharpe_ratio=observed,
        expected_max_sharpe=expected_max,
        probability=probability,
        observations=len(selected),
        trials=trials,
    )


def expected_max_sharpe(trials: int, trial_sharpe_stddev: float) -> float:
    if trials <= 1 or trial_sharpe_stddev <= 0:
        return 0.0
    first = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return trial_sharpe_stddev * (
        (1.0 - _EULER_GAMMA) * first + _EULER_GAMMA * second
    )


def probability_backtest_overfitting(
    returns_by_variant: dict[str, Sequence[float]],
    *,
    groups: int = 10,
) -> PboResult:
    """Combinatorially symmetric cross-validation over contiguous time groups."""
    if groups < 4 or groups % 2:
        raise ValueError("groups must be an even integer of at least 4")
    if len(returns_by_variant) < 2:
        return PboResult(0.0, 0, groups)
    lengths = {len(values) for values in returns_by_variant.values()}
    if len(lengths) != 1:
        raise ValueError("all variant return series must have the same length")
    observations = lengths.pop()
    if observations < groups:
        raise ValueError("return series is shorter than the requested group count")

    group_indexes = _contiguous_groups(observations, groups)
    variant_names = tuple(sorted(returns_by_variant))
    negative_logits = 0
    paths = 0
    for train_groups in combinations(range(groups), groups // 2):
        train_set = set(train_groups)
        train_indexes = [
            index
            for group_index, indexes in enumerate(group_indexes)
            if group_index in train_set
            for index in indexes
        ]
        test_indexes = [
            index
            for group_index, indexes in enumerate(group_indexes)
            if group_index not in train_set
            for index in indexes
        ]
        in_sample = {
            name: sharpe_ratio([returns_by_variant[name][index] for index in train_indexes])
            for name in variant_names
        }
        selected_name = max(variant_names, key=lambda name: (in_sample[name], name))
        out_sample = {
            name: sharpe_ratio([returns_by_variant[name][index] for index in test_indexes])
            for name in variant_names
        }
        ordered = sorted(variant_names, key=lambda name: (out_sample[name], name))
        rank = ordered.index(selected_name) + 1
        percentile = rank / (len(ordered) + 1.0)
        logit = math.log(percentile / (1.0 - percentile))
        negative_logits += int(logit <= 0.0)
        paths += 1
    return PboResult(
        probability_backtest_overfitting=negative_logits / paths if paths else 0.0,
        paths=paths,
        groups=groups,
    )


def probability_backtest_overfitting_total_return(
    returns_by_variant: dict[str, Sequence[float]],
    *,
    groups: int = 10,
) -> PboResult:
    """CSCV-style PBO aligned to a total-return selection objective."""
    if groups < 4 or groups % 2:
        raise ValueError("groups must be an even integer of at least 4")
    if len(returns_by_variant) < 2:
        return PboResult(0.0, 0, groups)
    lengths = {len(values) for values in returns_by_variant.values()}
    if len(lengths) != 1:
        raise ValueError("all variant return series must have the same length")
    observations = lengths.pop()
    if observations < groups:
        raise ValueError("return series is shorter than the requested group count")

    group_indexes = _contiguous_groups(observations, groups)
    variant_names = tuple(sorted(returns_by_variant))
    negative_logits = 0
    paths = 0
    for train_groups in combinations(range(groups), groups // 2):
        train_set = set(train_groups)
        train_indexes = [
            index
            for group_index, indexes in enumerate(group_indexes)
            if group_index in train_set
            for index in indexes
        ]
        test_indexes = [
            index
            for group_index, indexes in enumerate(group_indexes)
            if group_index not in train_set
            for index in indexes
        ]
        in_sample = {
            name: sum(returns_by_variant[name][index] for index in train_indexes)
            for name in variant_names
        }
        selected_name = max(variant_names, key=lambda name: (in_sample[name], name))
        out_sample = {
            name: sum(returns_by_variant[name][index] for index in test_indexes)
            for name in variant_names
        }
        ordered = sorted(variant_names, key=lambda name: (out_sample[name], name))
        rank = ordered.index(selected_name) + 1
        percentile = rank / (len(ordered) + 1.0)
        logit = math.log(percentile / (1.0 - percentile))
        negative_logits += int(logit <= 0.0)
        paths += 1
    return PboResult(
        probability_backtest_overfitting=negative_logits / paths if paths else 0.0,
        paths=paths,
        groups=groups,
    )


def daily_return_series(
    outcomes: Iterable[TradeOutcome],
    sessions: Sequence,
    *,
    initial_cash: float,
) -> list[float]:
    pnl_by_day: dict[object, float] = defaultdict(float)
    for outcome in outcomes:
        pnl_by_day[outcome.exit_date] += outcome.realized_pnl
    return [
        pnl_by_day.get(session, 0.0) / initial_cash
        for session in sessions
    ]


def sharpe_ratio(values: Sequence[float]) -> float:
    cleaned = [float(value) for value in values if math.isfinite(value)]
    deviation = sample_stddev(cleaned)
    return sum(cleaned) / len(cleaned) / deviation if deviation > 0 else 0.0


def sample_stddev(values: Sequence[float]) -> float:
    cleaned = [float(value) for value in values if math.isfinite(value)]
    if len(cleaned) < 2:
        return 0.0
    average = sum(cleaned) / len(cleaned)
    return math.sqrt(
        sum((value - average) ** 2 for value in cleaned) / (len(cleaned) - 1)
    )


def sample_skewness_kurtosis(values: Sequence[float]) -> tuple[float, float]:
    cleaned = [float(value) for value in values if math.isfinite(value)]
    if len(cleaned) < 3:
        return 0.0, 3.0
    average = sum(cleaned) / len(cleaned)
    variance = sum((value - average) ** 2 for value in cleaned) / len(cleaned)
    if variance <= 0:
        return 0.0, 3.0
    deviation = math.sqrt(variance)
    skewness = sum(((value - average) / deviation) ** 3 for value in cleaned) / len(cleaned)
    kurtosis = sum(((value - average) / deviation) ** 4 for value in cleaned) / len(cleaned)
    return skewness, kurtosis


def _daily_win_counts(
    outcomes: Iterable[TradeOutcome],
) -> dict[object, tuple[int, int]]:
    mutable: dict[object, list[int]] = defaultdict(lambda: [0, 0])
    for outcome in outcomes:
        mutable[outcome.exit_date][0] += int(outcome.realized_pnl > 0)
        mutable[outcome.exit_date][1] += 1
    return {day: (counts[0], counts[1]) for day, counts in mutable.items()}


def _daily_pnl(outcomes: Iterable[TradeOutcome]) -> dict[object, float]:
    pnl: dict[object, float] = defaultdict(float)
    for outcome in outcomes:
        pnl[outcome.exit_date] += outcome.realized_pnl
    return dict(pnl)


def _rate_from_counts(counts: Iterable[tuple[int, int]]) -> float:
    wins = 0
    observations = 0
    for count_wins, count_observations in counts:
        wins += count_wins
        observations += count_observations
    return wins / observations if observations else 0.0


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _contiguous_groups(observations: int, groups: int) -> list[list[int]]:
    base_size, remainder = divmod(observations, groups)
    out: list[list[int]] = []
    start = 0
    for group_index in range(groups):
        size = base_size + int(group_index < remainder)
        out.append(list(range(start, start + size)))
        start += size
    return out
