from __future__ import annotations

import os
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from alpaca_ma5_service import strategy
from alpaca_ma5_service.config import (
    GAP_CONFIRMED_PULLBACK_STRATEGY_NAME,
    MA5_DIP_STRATEGY_NAME,
    build_settings,
)
from alpaca_ma5_service.models import OrderResult, Signal
from alpaca_ma5_service.strategy_framework import (
    DEFAULT_CANCEL_STRATEGY_NAME,
    DEFAULT_SELL_STRATEGY_NAME,
    StrategyProfile,
    StrategyRegistry,
    get_strategy_registry,
    resolve_strategy_runtime,
)
from alpaca_ma5_service.strategy_framework.builtins import (
    TimeoutCancelConfirmedStrategy as LegacyTimeoutCancelConfirmedStrategy,
)
from alpaca_ma5_service.strategy_framework.components.cancel import (
    TimeoutCancelConfirmedStrategy,
)


class StubWatchlist:
    name = "shared_name"
    description = "stub watchlist"

    def screen_rules(self):
        return {"kind": "stub"}


class StubBuy:
    name = "shared_name"
    description = "stub buy"

    def evaluate(self, snapshot):
        return Signal(snapshot.symbol, "HOLD", "stub", snapshot.current_price)

    def max_buy_today_current_gain_pct(self):
        return -0.15

    def legacy_module(self):
        return ModuleType("stub_buy")


class StubSell:
    name = "shared_name"
    description = "stub sell"

    def evaluate(self, position, snapshot, now_et, settings):
        return Signal(position.symbol, "HOLD", "stub", snapshot.current_price)

    def evaluate_stop_loss(self, position, snapshot, settings):
        return Signal(position.symbol, "HOLD", "stub", snapshot.current_price)

    def evaluate_take_profit_remainder_stop(self, position, snapshot, settings):
        return Signal(position.symbol, "HOLD", "stub", snapshot.current_price)


class StubCancel:
    name = "shared_name"
    description = "stub cancel"

    def wait_for_terminal(
        self,
        client,
        raw_order,
        symbol,
        side,
        quantity,
        price,
        source_name,
        *,
        timeout_seconds,
        poll_seconds,
    ):
        return OrderResult("stub", symbol, side, quantity, price, "CANCELED", "stub")

    def cancel_order(
        self,
        client,
        order_id,
        symbol,
        side,
        quantity,
        price,
        *,
        timeout_seconds,
        success_message,
        failure_prefix,
    ):
        return OrderResult(order_id, symbol, side, quantity, price, "CANCELED", "stub")


PROFILE_DEFAULTS = {
    "max_daily_buys": 1,
    "stop_loss_pct": -0.10,
    "stop_loss_limit_pct": -0.08,
    "take_profit_half_pct": 0.10,
    "take_profit_sell_fraction": 0.50,
    "take_profit_remainder_stop_pct": None,
}


class StrategyRegistryTests(TestCase):
    def test_each_strategy_category_has_an_independent_namespace(self):
        registry = StrategyRegistry()
        registry.register_watchlist(StubWatchlist())
        registry.register_buy(StubBuy())
        registry.register_sell(StubSell())
        registry.register_cancel(StubCancel())
        registry.register_profile(
            StrategyProfile(
                name="composed",
                watchlist_strategy_name="shared_name",
                buy_strategy_name="shared_name",
                sell_strategy_name="shared_name",
                cancel_strategy_name="shared_name",
                runtime_defaults=PROFILE_DEFAULTS,
            )
        )

        self.assertEqual(registry.profile("composed").buy_strategy_name, "shared_name")
        self.assertIsInstance(registry.watchlist("shared_name"), StubWatchlist)
        self.assertIsInstance(registry.buy("shared_name"), StubBuy)
        self.assertIsInstance(registry.sell("shared_name"), StubSell)
        self.assertIsInstance(registry.cancel("shared_name"), StubCancel)

    def test_duplicate_name_in_the_same_category_is_rejected(self):
        registry = StrategyRegistry()
        registry.register_buy(StubBuy())

        with self.assertRaisesRegex(ValueError, "Duplicate buy strategy"):
            registry.register_buy(StubBuy())

    def test_profile_referencing_a_missing_component_is_rejected(self):
        registry = StrategyRegistry()
        registry.register_watchlist(StubWatchlist())
        registry.register_buy(StubBuy())
        registry.register_sell(StubSell())

        with self.assertRaisesRegex(ValueError, "Unknown cancel strategy"):
            registry.register_profile(
                StrategyProfile(
                    name="broken",
                    watchlist_strategy_name="shared_name",
                    buy_strategy_name="shared_name",
                    sell_strategy_name="shared_name",
                    cancel_strategy_name="missing",
                    runtime_defaults=PROFILE_DEFAULTS,
                )
            )


class StrategyConfigurationTests(TestCase):
    def test_split_components_keep_legacy_builtin_imports_compatible(self):
        self.assertIs(LegacyTimeoutCancelConfirmedStrategy, TimeoutCancelConfirmedStrategy)

    def test_builtin_profiles_resolve_all_four_categories(self):
        registry = get_strategy_registry()

        self.assertEqual(
            registry.available_profile_names(),
            (MA5_DIP_STRATEGY_NAME, GAP_CONFIRMED_PULLBACK_STRATEGY_NAME),
        )
        settings = build_settings(strategy_name=GAP_CONFIRMED_PULLBACK_STRATEGY_NAME)
        runtime = resolve_strategy_runtime(settings)

        self.assertEqual(runtime.selection.profile_name, GAP_CONFIRMED_PULLBACK_STRATEGY_NAME)
        self.assertEqual(
            runtime.selection.watchlist_strategy_name,
            GAP_CONFIRMED_PULLBACK_STRATEGY_NAME,
        )
        self.assertEqual(runtime.selection.buy_strategy_name, GAP_CONFIRMED_PULLBACK_STRATEGY_NAME)
        self.assertEqual(runtime.selection.sell_strategy_name, DEFAULT_SELL_STRATEGY_NAME)
        self.assertEqual(runtime.selection.cancel_strategy_name, DEFAULT_CANCEL_STRATEGY_NAME)

    def test_component_overrides_are_independent(self):
        settings = build_settings(
            strategy_profile_name=MA5_DIP_STRATEGY_NAME,
            watchlist_strategy_name=GAP_CONFIRMED_PULLBACK_STRATEGY_NAME,
            buy_strategy_name=MA5_DIP_STRATEGY_NAME,
            sell_strategy_name=DEFAULT_SELL_STRATEGY_NAME,
            cancel_strategy_name=DEFAULT_CANCEL_STRATEGY_NAME,
        )

        self.assertEqual(settings.strategy_name, MA5_DIP_STRATEGY_NAME)
        self.assertEqual(settings.watchlist_strategy_name, GAP_CONFIRMED_PULLBACK_STRATEGY_NAME)
        self.assertEqual(settings.buy_strategy_name, MA5_DIP_STRATEGY_NAME)

    def test_invalid_component_is_rejected_while_building_settings(self):
        with self.assertRaisesRegex(ValueError, "Unknown buy strategy"):
            build_settings(buy_strategy_name="does_not_exist")

    def test_env_profile_is_supported_and_explicit_legacy_name_wins(self):
        with patch.dict(os.environ, {"STRATEGY_PROFILE": GAP_CONFIRMED_PULLBACK_STRATEGY_NAME}):
            self.assertEqual(
                build_settings().strategy_profile_name,
                GAP_CONFIRMED_PULLBACK_STRATEGY_NAME,
            )
            self.assertEqual(
                build_settings(strategy_name=MA5_DIP_STRATEGY_NAME).strategy_profile_name,
                MA5_DIP_STRATEGY_NAME,
            )

    def test_runtime_selection_does_not_mutate_legacy_backtest_context(self):
        strategy.set_active_strategy(MA5_DIP_STRATEGY_NAME)
        settings = build_settings(strategy_name=GAP_CONFIRMED_PULLBACK_STRATEGY_NAME)

        runtime = resolve_strategy_runtime(settings)

        self.assertEqual(runtime.buy.name, GAP_CONFIRMED_PULLBACK_STRATEGY_NAME)
        self.assertEqual(strategy.active_buy_module().STRATEGY_NAME, MA5_DIP_STRATEGY_NAME)
