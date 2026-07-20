from __future__ import annotations

from unittest.mock import patch
import unittest

from alpaca_ma5_service.config import MA5_DIP_STRATEGY_NAME
from alpaca_ma5_service.final_strategy import (
    BUY_NOTIONAL_USD as GAP_BUY_NOTIONAL_USD,
    MAX_DAILY_BUYS as GAP_MAX_DAILY_BUYS,
    STOP_PARAMS,
    STRATEGY_NAME as GAP_STRATEGY_NAME,
)
from alpaca_ma5_service.workflows.monitoring import intraday


class IntradayWorkflowConfigTests(unittest.TestCase):
    @patch("alpaca_ma5_service.config.load_env_file", return_value={})
    def test_checked_in_default_selects_gap_profile_at_2500(self, _load_env):
        settings = intraday.build_monitor_settings()

        self.assertEqual(intraday.STRATEGY_NAME, GAP_STRATEGY_NAME)
        self.assertEqual(intraday.WATCHLIST_STRATEGY_NAME, GAP_STRATEGY_NAME)
        self.assertEqual(intraday.BUY_STRATEGY_NAME, GAP_STRATEGY_NAME)
        self.assertEqual(settings.strategy_name, GAP_STRATEGY_NAME)
        self.assertEqual(settings.max_daily_buys, 3)
        self.assertEqual(settings.buy_notional_usd, 2_500.0)

    @patch("alpaca_ma5_service.config.load_env_file", return_value={})
    def test_default_ma5_profile_keeps_explicit_runtime_values(self, _load_env):
        with patch.multiple(
            intraday,
            STRATEGY_NAME=MA5_DIP_STRATEGY_NAME,
            WATCHLIST_STRATEGY_NAME=MA5_DIP_STRATEGY_NAME,
            BUY_STRATEGY_NAME=MA5_DIP_STRATEGY_NAME,
        ):
            settings = intraday.build_monitor_settings()

        self.assertEqual(settings.max_daily_buys, intraday.BUY_STOCK_COUNT)
        self.assertEqual(settings.buy_notional_usd, intraday.BUY_NOTIONAL_USD)
        self.assertEqual(settings.stop_loss_pct, intraday.STOP_LOSS_PCT)
        self.assertEqual(
            settings.take_profit_half_pct,
            intraday.TAKE_PROFIT_HALF_PCT,
        )
        self.assertEqual(
            settings.take_profit_sell_fraction,
            intraday.TAKE_PROFIT_SELL_FRACTION,
        )
        self.assertIsNone(settings.take_profit_remainder_stop_pct)

    @patch("alpaca_ma5_service.config.load_env_file", return_value={})
    def test_gap_profile_uses_the_frozen_backtest_runtime_values(self, _load_env):
        with patch.multiple(
            intraday,
            STRATEGY_NAME=GAP_STRATEGY_NAME,
            WATCHLIST_STRATEGY_NAME=GAP_STRATEGY_NAME,
            BUY_STRATEGY_NAME=GAP_STRATEGY_NAME,
        ):
            settings = intraday.build_monitor_settings()

        self.assertEqual(settings.max_daily_buys, GAP_MAX_DAILY_BUYS)
        self.assertEqual(settings.buy_notional_usd, GAP_BUY_NOTIONAL_USD)
        self.assertEqual(settings.stop_loss_pct, STOP_PARAMS["stop_loss_pct"])
        self.assertEqual(
            settings.stop_loss_limit_pct,
            STOP_PARAMS["stop_loss_limit_pct"],
        )
        self.assertEqual(
            settings.take_profit_half_pct,
            STOP_PARAMS["take_profit_half_pct"],
        )
        self.assertEqual(
            settings.take_profit_sell_fraction,
            STOP_PARAMS["take_profit_sell_fraction"],
        )
        self.assertEqual(
            settings.take_profit_remainder_stop_pct,
            STOP_PARAMS["take_profit_remainder_stop_pct"],
        )


if __name__ == "__main__":
    unittest.main()
