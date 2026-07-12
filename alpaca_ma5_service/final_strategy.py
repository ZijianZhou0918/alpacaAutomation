from __future__ import annotations

from datetime import date


STRATEGY_NAME = "gap_confirmed_pullback_g8_r30_b10_st8_tp8"
STRATEGY_DESCRIPTION = (
    "Final gap-confirmed pullback strategy: fixed single buy $3,500; "
    "max_daily_buys=10; max_positions=10; signal gain >8%; signal gain beats MA5 by >2%; "
    "signal close is in the upper half of the signal-day range; signal-day range <=30%; "
    "MA5 > MA10 > MA20; buy only after a non-gap-down open when current price pulls back "
    "to -8%..-2% from the signal close and remains at/above the dynamic MA5; "
    "stop loss 8%; take profit at 8% selling all; no overnight holding."
)

TARGET_YEARS = [2024, 2025, 2026]
TARGET_RETURN_PCT = 0.20
INITIAL_CASH = 100_000.0
BUY_NOTIONAL_USD = 3_500.0
MAX_DAILY_BUYS = 10
MAX_POSITIONS = 10
SLIPPAGE_PCT = 0.0

WATCHLIST_SIGNAL_PARAMS = {
    "MIN_SIGNAL_GAIN_PCT": 0.08,
    "MIN_SIGNAL_GAIN_OVER_MA5_GAIN_PCT": 0.02,
    "MIN_OPEN_TO_MA5_RATIO": 0.95,
    "MIN_CLOSE_TO_MA5_RATIO": 1.08,
}

BUY_SIGNAL_PARAMS = {
    "MIN_SIGNAL_DAY_GAIN_PCT": 0.08,
    "MID_SIGNAL_DAY_GAIN_PCT": 0.40,
    "HIGH_SIGNAL_DAY_GAIN_PCT": 1.00,
    "MID_OPEN_GAIN_PCT": 0.05,
    "HIGH_OPEN_GAIN_PCT": 0.15,
    "BUY_TRIGGER_DISTANCE_PCT": 0.00,
    "MIN_TODAY_OPEN_GAIN_PCT": 0.00,
    "MAX_TODAY_OPEN_GAIN_PCT": 0.35,
    "MIN_TODAY_OPEN_VS_OPEN_MA5_PCT": -0.18,
    "MIN_TODAY_CURRENT_GAIN_PCT": -0.08,
    "MAX_BUY_TODAY_CURRENT_GAIN_PCT": -0.02,
    "MIN_CURRENT_VS_TODAY_MA5_PCT": 0.00,
}

STOP_PARAMS = {
    "stop_loss_pct": -0.08,
    "stop_loss_limit_pct": -0.06,
    "take_profit_half_pct": 0.08,
    "take_profit_sell_fraction": 1.00,
    "take_profit_remainder_stop_pct": 0.08,
}

OPTIMIZATION_RULES = {
    "require_ma5_gt_ma10_gt_ma20": True,
    "min_close_to_ma5_ratio": 1.00,
    "max_close_to_ma5_ratio": 3.40,
    "min_signal_close_position_pct": 0.50,
    "max_signal_range_pct": 0.30,
}

VALIDATION_START_DATE = date(2024, 1, 1)
VALIDATION_END_DATE = date(2026, 12, 31)
