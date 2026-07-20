from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backtest.engine import TradeRecord
from backtest.gap_strategy_optimization import (
    VariantSpec,
    build_variant_config,
    frozen_candidate_variant,
    holdout_variants,
    legacy_baseline_variant,
    require_candidate_subset,
    return_holdout_variants,
    return_robustness_variants,
    return_signal_variants,
    return_sizing_variants,
    robustness_variants,
    select_return_row,
    stage_one_variants,
    stage_two_variants,
    summarize_outcomes,
    trade_outcomes,
    validate_research_period,
)


@dataclass(frozen=True)
class FakeSettings:
    output_dir: Path
    state_file: Path
    buy_notional_usd: float = 3_500.0
    max_daily_buys: int = 10
    stop_loss_pct: float = -0.08
    stop_loss_limit_pct: float = -0.06
    take_profit_half_pct: float = 0.08
    take_profit_sell_fraction: float = 1.0
    take_profit_remainder_stop_pct: float | None = 0.08


@dataclass(frozen=True)
class FakeConfig:
    start_date: date
    end_date: date
    output_dir: Path
    html_report_name: str
    buy_notional_usd: float
    max_positions: int
    max_daily_buys: int
    commission_per_order: float
    slippage_pct: float
    strategy_settings: FakeSettings
    watchlist_signal_params: dict[str, float]
    buy_signal_params: dict[str, float]
    stop_params: dict[str, float]
    optimization_rules: dict[str, object]
    strategy_variant_name: str
    strategy_variant_description: str


class GapStrategyOptimizationTests(unittest.TestCase):
    def test_variant_config_changes_only_explicit_research_fields(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = FakeConfig(
                start_date=date(2025, 1, 1),
                end_date=date(2025, 9, 30),
                output_dir=root / "base",
                html_report_name="",
                buy_notional_usd=3_500.0,
                max_positions=10,
                max_daily_buys=10,
                commission_per_order=0.0,
                slippage_pct=0.0,
                strategy_settings=FakeSettings(
                    output_dir=root / "base",
                    state_file=root / "base" / "state.json",
                ),
                watchlist_signal_params={"MIN_SIGNAL_GAIN_PCT": 0.08},
                buy_signal_params={"MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.02},
                stop_params={"stop_loss_pct": -0.08},
                optimization_rules={"min_signal_close_position_pct": 0.50},
                strategy_variant_name="baseline",
                strategy_variant_description="baseline",
            )
            spec = VariantSpec(
                "deeper",
                "deeper pullback",
                buy_signal_params={"MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.05},
                optimization_rules={"min_signal_close_position_pct": 0.90},
                max_positions=3,
                max_daily_buys=3,
                take_profit_pct=0.06,
            )
            output = root / "deeper"
            result = build_variant_config(
                base,  # type: ignore[arg-type]
                spec,
                start_date=date(2025, 2, 1),
                end_date=date(2025, 8, 31),
                output_dir=output,
            )

            self.assertEqual(result.start_date, date(2025, 2, 1))
            self.assertEqual(result.end_date, date(2025, 8, 31))
            self.assertEqual(result.max_daily_buys, 3)
            self.assertEqual(result.strategy_settings.max_daily_buys, 3)
            self.assertEqual(result.strategy_settings.take_profit_half_pct, 0.06)
            self.assertEqual(
                result.buy_signal_params["MAX_BUY_TODAY_CURRENT_GAIN_PCT"],
                -0.05,
            )
            self.assertEqual(
                result.optimization_rules["min_signal_close_position_pct"],
                0.90,
            )
            self.assertEqual(result.strategy_variant_name, "deeper")
            self.assertEqual(base.max_daily_buys, 10)

    def test_candidate_subset_fails_closed_on_expansion(self):
        baseline = {("2025-01-02", "AAPL"), ("2025-01-03", "MSFT")}
        require_candidate_subset(
            {"2025-01-02": ["US.AAPL"]},
            baseline,
        )
        with self.assertRaisesRegex(ValueError, "expands beyond"):
            require_candidate_subset(
                {"2025-01-02": ["US.AAPL", "US.NVDA"]},
                baseline,
            )

    def test_partial_exits_are_one_trade_outcome(self):
        entered = datetime.fromisoformat("2025-01-02T10:00:00-05:00")
        exited = datetime.fromisoformat("2025-01-02T15:55:00-05:00")
        trades = [
            TradeRecord(
                entered,
                "US.TEST",
                "BUY",
                10.0,
                100.0,
                1_000.0,
                1.0,
                9_000.0,
                0.0,
                "buy",
                "buy_limit",
            ),
            TradeRecord(
                exited,
                "US.TEST",
                "SELL",
                4.0,
                110.0,
                440.0,
                0.0,
                9_440.0,
                40.0,
                "partial",
                "take_profit_half",
            ),
            TradeRecord(
                exited,
                "US.TEST",
                "SELL",
                6.0,
                98.0,
                588.0,
                0.0,
                10_028.0,
                -10.0,
                "close",
                "close_liquidation",
            ),
        ]

        outcomes = trade_outcomes(trades)
        stats = summarize_outcomes(outcomes)

        self.assertEqual(len(outcomes), 1)
        self.assertAlmostEqual(outcomes[0].realized_pnl, 30.0)
        self.assertAlmostEqual(outcomes[0].invested, 1_001.0)
        self.assertEqual(stats.trade_count, 1)
        self.assertEqual(stats.wins, 1)
        self.assertEqual(stats.win_rate, 1.0)

    def test_stage_one_variants_are_unique_and_bounded(self):
        variants = stage_one_variants()
        names = [variant.name for variant in variants]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], "baseline")
        self.assertLessEqual(len(variants), 25)
        self.assertTrue(
            all(
                not variant.watchlist_signal_params
                for variant in variants
            )
        )

    def test_stage_two_is_a_bounded_neighbor_grid(self):
        variants = stage_two_variants()
        self.assertEqual(len(variants), 6)
        self.assertEqual(len({variant.name for variant in variants}), 6)
        self.assertEqual(
            {
                variant.buy_signal_params["MAX_BUY_TODAY_CURRENT_GAIN_PCT"]
                for variant in variants
            },
            {-0.04, -0.05, -0.06},
        )
        self.assertEqual(
            {variant.take_profit_pct for variant in variants},
            {0.04, 0.05},
        )
        self.assertTrue(
            all(not variant.optimization_rules for variant in variants)
        )

    def test_frozen_candidate_and_period_guards(self):
        legacy = legacy_baseline_variant()
        self.assertEqual(
            legacy.buy_signal_params["MAX_BUY_TODAY_CURRENT_GAIN_PCT"],
            -0.02,
        )
        self.assertEqual(legacy.take_profit_pct, 0.08)
        frozen = frozen_candidate_variant()
        self.assertEqual(frozen.name, "pullback_5_take_profit_4")
        self.assertEqual(frozen.take_profit_pct, 0.04)
        self.assertEqual(
            frozen.buy_signal_params["MAX_BUY_TODAY_CURRENT_GAIN_PCT"],
            -0.05,
        )
        self.assertEqual(
            [variant.name for variant in holdout_variants()],
            ["baseline", "pullback_5_take_profit_4"],
        )
        validate_research_period(
            "stage2",
            date(2025, 1, 1),
            date(2025, 9, 30),
        )
        validate_research_period(
            "holdout",
            date(2025, 10, 1),
            date(2025, 12, 31),
        )
        with self.assertRaisesRegex(ValueError, "frozen window"):
            validate_research_period(
                "stage2",
                date(2025, 1, 1),
                date(2025, 10, 1),
            )
        with self.assertRaisesRegex(ValueError, "locked"):
            validate_research_period(
                "holdout",
                date(2026, 1, 1),
                date(2026, 3, 31),
            )
        robustness = robustness_variants()
        self.assertEqual(
            [variant.slippage_pct for variant in robustness],
            [None, 0.001, 0.0025],
        )
        validate_research_period(
            "robustness",
            date(2025, 1, 1),
            date(2025, 12, 31),
        )

    def test_return_signal_grid_is_bounded_and_cost_aware(self):
        variants = return_signal_variants()
        self.assertEqual(len(variants), 12)
        self.assertEqual(len({variant.name for variant in variants}), 12)
        self.assertEqual(
            {
                variant.buy_signal_params["MAX_BUY_TODAY_CURRENT_GAIN_PCT"]
                for variant in variants
            },
            {-0.02, -0.04},
        )
        self.assertEqual(
            {variant.take_profit_pct for variant in variants},
            {0.04, 0.05, 0.08},
        )
        self.assertTrue(all(variant.slippage_pct == 0.001 for variant in variants))
        self.assertTrue(all(variant.buy_notional_usd == 3_500 for variant in variants))

    def test_return_sizing_grid_never_exceeds_cash_budget(self):
        signal = return_signal_variants()[0]
        variants = return_sizing_variants(signal)
        self.assertEqual(len(variants), 7)
        self.assertEqual(len({variant.name for variant in variants}), 7)
        self.assertTrue(
            all(
                float(variant.buy_notional_usd or 0)
                * int(variant.max_positions or 0)
                <= 100_000
                for variant in variants
            )
        )
        self.assertTrue(
            all(variant.max_positions == variant.max_daily_buys for variant in variants)
        )
        holdout = return_holdout_variants(variants[-1])
        self.assertEqual(len(holdout), 2)
        self.assertTrue(all(variant.slippage_pct == 0.001 for variant in holdout))
        robustness = return_robustness_variants(variants[-1])
        self.assertEqual(
            [variant.slippage_pct for variant in robustness],
            [0.001, 0.001, 0.0, 0.001, 0.0025],
        )
        self.assertEqual(robustness[1].buy_notional_usd, 20_000)
        self.assertEqual(robustness[1].max_positions, 5)

    def test_return_selection_maximizes_net_return_with_guardrails(self):
        base = {
            "trade_count": 600,
            "profit_factor": 1.2,
            "max_drawdown_pct": -0.10,
            "q1_realized_pnl": 1.0,
            "q2_realized_pnl": 1.0,
            "q3_realized_pnl": 1.0,
        }
        rows = [
            {**base, "name": "eligible_lower", "backtest_return_pct": 0.30},
            {**base, "name": "eligible_higher", "backtest_return_pct": 0.40},
            {
                **base,
                "name": "negative_quarter",
                "backtest_return_pct": 0.90,
                "q2_realized_pnl": -1.0,
            },
            {
                **base,
                "name": "excess_drawdown",
                "backtest_return_pct": 1.20,
                "max_drawdown_pct": -0.20,
            },
        ]
        self.assertEqual(select_return_row(rows)["name"], "eligible_higher")
        with self.assertRaisesRegex(RuntimeError, "No return candidate"):
            select_return_row(rows[2:])

    def test_return_periods_keep_2026_locked(self):
        validate_research_period(
            "return_signal",
            date(2025, 1, 1),
            date(2025, 9, 30),
        )
        validate_research_period(
            "return_sizing",
            date(2025, 1, 1),
            date(2025, 9, 30),
        )
        validate_research_period(
            "return_holdout",
            date(2025, 10, 1),
            date(2025, 12, 31),
        )
        with self.assertRaisesRegex(ValueError, "locked"):
            validate_research_period(
                "return_robustness",
                date(2026, 1, 1),
                date(2026, 12, 31),
            )


if __name__ == "__main__":
    unittest.main()
