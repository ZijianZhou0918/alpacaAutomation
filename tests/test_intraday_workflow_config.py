from __future__ import annotations

from unittest.mock import patch
import unittest

from alpaca_ma5_service.config import MA5_DIP_LADDER_STRATEGY_NAME, MA5_DIP_STRATEGY_NAME
from alpaca_ma5_service.workflows.monitoring import intraday


class IntradayWorkflowConfigTests(unittest.TestCase):
    @patch("alpaca_ma5_service.config.load_env_file", return_value={})
    def test_checked_in_default_selects_ma5_ladder_profile(self, _load_env):
        settings = intraday.build_monitor_settings()

        self.assertEqual(intraday.STRATEGY_NAME, MA5_DIP_LADDER_STRATEGY_NAME)
        self.assertEqual(intraday.WATCHLIST_STRATEGY_NAME, MA5_DIP_STRATEGY_NAME)
        self.assertEqual(intraday.BUY_STRATEGY_NAME, MA5_DIP_STRATEGY_NAME)
        self.assertEqual(settings.strategy_name, MA5_DIP_LADDER_STRATEGY_NAME)
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
        self.assertEqual(settings.buy_ladder_offsets, (0.0, -0.01, -0.02))
        self.assertEqual(settings.sell_ladder_offsets, (0.0, 0.01, 0.02))
        self.assertEqual(settings.absolute_stop_loss_pct, -0.10)
        self.assertEqual(settings.broker_protective_stop_pct, -0.08)


if __name__ == "__main__":
    unittest.main()
