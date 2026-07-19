from __future__ import annotations

import pandas as pd

from .config import StrategyConfig


def add_intraday_indicators(bars: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """Add only backward-looking intraday indicators to five-minute bars."""
    if bars.empty:
        return bars.copy()
    out = bars.sort_values(["symbol", "timestamp"]).copy()
    out["typical_price"] = (out["high"] + out["low"] + out["close"]) / 3.0
    if "dollar_value" not in out:
        out["dollar_value"] = out["typical_price"] * out["volume"]
    grouped = out.groupby("symbol", sort=False, group_keys=False)
    out["cumulative_dollar_value"] = grouped["dollar_value"].cumsum()
    out["cumulative_volume"] = grouped["volume"].cumsum()
    out["intraday_vwap"] = out["cumulative_dollar_value"] / out["cumulative_volume"].where(out["cumulative_volume"] > 0)
    if config.indicator == "vwap":
        out["indicator"] = out["intraday_vwap"]
        out["indicator_name"] = "VWAP"
    else:
        out["indicator"] = grouped["close"].transform(
            lambda values: values.rolling(config.moving_average_window, min_periods=config.moving_average_window).mean()
        )
        out["indicator_name"] = f"SMA{config.moving_average_window}"
    out["prior_volume_average"] = grouped["volume"].transform(
        lambda values: values.shift(1).rolling(config.volume_lookback_bars, min_periods=config.volume_lookback_bars).mean()
    )
    out["bar_end"] = out["timestamp"] + pd.Timedelta(minutes=5)
    return out
