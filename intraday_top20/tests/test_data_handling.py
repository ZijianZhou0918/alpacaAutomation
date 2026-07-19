from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.data.cleaner import aggregate_to_five_minutes, standardize_columns
from intraday_top20.data.loader import MarketDataLoader


def test_regular_session_filter_and_utc_to_eastern_conversion() -> None:
    raw = pd.DataFrame(
        [
            _raw_bar("2025-01-03T14:25:00Z", 9.0),   # 09:25 ET, premarket
            _raw_bar("2025-01-03T14:30:00Z", 10.0),  # 09:30 ET, included
            _raw_bar("2025-01-03T21:00:00Z", 11.0),  # 16:00 ET, after RTH
        ]
    )
    result = standardize_columns(raw)
    assert len(result) == 1
    timestamp = result.iloc[0]["timestamp"]
    assert timestamp.hour == 9 and timestamp.minute == 30
    assert str(timestamp.tzinfo) == "America/New_York"


def test_typical_price_dollar_value_is_aggregated_without_future_rows() -> None:
    raw = pd.DataFrame(
        [
            {**_raw_bar("2025-01-03T14:30:00Z", 10.0), "h": 12.0, "l": 9.0, "v": 100.0},
            {**_raw_bar("2025-01-03T14:31:00Z", 11.0), "h": 13.0, "l": 10.0, "v": 200.0},
        ]
    )
    result = aggregate_to_five_minutes([raw])
    expected = ((12.0 + 9.0 + 10.0) / 3.0) * 100.0 + ((13.0 + 10.0 + 11.0) / 3.0) * 200.0
    assert len(result) == 1
    assert result.iloc[0]["dollar_value"] == pytest.approx(expected)
    assert result.iloc[0]["open"] == 10.0
    assert result.iloc[0]["close"] == 11.0


def test_split_adjusts_previous_close_on_execution_date(tmp_path) -> None:
    split_path = tmp_path / "splits.csv"
    pd.DataFrame(
        [{"symbol": "AAA", "execution_date": "2025-01-03", "split_from": 1.0, "split_to": 2.0}]
    ).to_csv(split_path, index=False)
    config = BacktestConfig().with_updates(
        data={"data_dir": str(tmp_path), "security_master_path": "", "splits_path": str(split_path)}
    )
    loader = MarketDataLoader(config)
    adjusted = loader._adjust_previous_for_splits(date(2025, 1, 3), {"AAA": 100.0})
    assert adjusted["AAA"] == pytest.approx(50.0)


def _raw_bar(timestamp: str, close: float) -> dict[str, object]:
    return {
        "ticker": "AAA",
        "window_start": timestamp,
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": 1_000.0,
    }
