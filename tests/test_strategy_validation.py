from __future__ import annotations

from datetime import date
import unittest

from backtest.gap_strategy_optimization import TradeOutcome
from backtest.strategy_validation import (
    block_bootstrap_portfolio_return_delta,
    block_bootstrap_win_rate_delta,
    deflated_sharpe_ratio,
    probability_backtest_overfitting,
    probability_backtest_overfitting_total_return,
    wilson_interval,
)


class StrategyValidationTests(unittest.TestCase):
    def test_wilson_interval_contains_observed_rate(self):
        low, high = wilson_interval(60, 100)
        self.assertLess(low, 0.60)
        self.assertGreater(high, 0.60)

    def test_block_bootstrap_is_deterministic(self):
        baseline = [
            TradeOutcome("A", date(2025, 1, 2), date(2025, 1, 2), -1.0, 100.0),
            TradeOutcome("B", date(2025, 1, 3), date(2025, 1, 3), 1.0, 100.0),
        ]
        candidate = [
            TradeOutcome("A", date(2025, 1, 2), date(2025, 1, 2), 1.0, 100.0),
            TradeOutcome("B", date(2025, 1, 3), date(2025, 1, 3), 1.0, 100.0),
        ]
        first = block_bootstrap_win_rate_delta(
            baseline,
            candidate,
            samples=200,
            seed=7,
        )
        second = block_bootstrap_win_rate_delta(
            baseline,
            candidate,
            samples=200,
            seed=7,
        )
        self.assertEqual(first, second)
        self.assertAlmostEqual(first.observed_absolute_delta, 0.5)
        self.assertAlmostEqual(first.observed_relative_delta, 1.0)

    def test_portfolio_return_bootstrap_is_deterministic(self):
        baseline = [
            TradeOutcome("A", date(2025, 1, 2), date(2025, 1, 2), -10.0, 100.0),
            TradeOutcome("B", date(2025, 1, 3), date(2025, 1, 3), 5.0, 100.0),
        ]
        candidate = [
            TradeOutcome("A", date(2025, 1, 2), date(2025, 1, 2), 5.0, 100.0),
            TradeOutcome("B", date(2025, 1, 3), date(2025, 1, 3), 10.0, 100.0),
        ]
        first = block_bootstrap_portfolio_return_delta(
            baseline,
            candidate,
            initial_cash=100.0,
            samples=200,
            seed=11,
        )
        second = block_bootstrap_portfolio_return_delta(
            baseline,
            candidate,
            initial_cash=100.0,
            samples=200,
            seed=11,
        )
        self.assertEqual(first, second)
        self.assertAlmostEqual(first.baseline_return, -0.05)
        self.assertAlmostEqual(first.candidate_return, 0.15)
        self.assertAlmostEqual(first.observed_delta, 0.20)

    def test_deflated_sharpe_penalizes_many_trials(self):
        returns = [0.01, 0.02, -0.005, 0.015, 0.01, 0.02, -0.003, 0.014]
        few = deflated_sharpe_ratio(returns, [returns])
        many = deflated_sharpe_ratio(
            returns,
            [
                [value * scale for value in returns]
                for scale in (0.2, 0.4, 0.6, 0.8, 1.0, 1.2)
            ],
        )
        self.assertGreaterEqual(few.probability, many.probability)

    def test_pbo_rejects_invalid_shapes_and_returns_probability(self):
        with self.assertRaisesRegex(ValueError, "same length"):
            probability_backtest_overfitting(
                {"a": [0.1] * 10, "b": [0.1] * 9},
                groups=4,
            )
        result = probability_backtest_overfitting(
            {
                "a": [0.01, -0.01] * 10,
                "b": [-0.01, 0.01] * 10,
                "c": [0.005, 0.0] * 10,
            },
            groups=4,
        )
        self.assertEqual(result.paths, 6)
        self.assertGreaterEqual(result.probability_backtest_overfitting, 0.0)
        self.assertLessEqual(result.probability_backtest_overfitting, 1.0)
        total_return_result = probability_backtest_overfitting_total_return(
            {
                "a": [0.01, -0.01] * 10,
                "b": [-0.01, 0.01] * 10,
                "c": [0.005, 0.0] * 10,
            },
            groups=4,
        )
        self.assertEqual(total_return_result.paths, 6)
        self.assertGreaterEqual(
            total_return_result.probability_backtest_overfitting,
            0.0,
        )
        self.assertLessEqual(
            total_return_result.probability_backtest_overfitting,
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
