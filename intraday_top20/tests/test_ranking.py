from __future__ import annotations

import pandas as pd

from intraday_top20.backtest.config import ExecutionConfig, StrategyConfig
from intraday_top20.backtest.universe import dynamic_top_gainers, security_eligibility


def test_dynamic_ranking_uses_only_current_completed_snapshot() -> None:
    current = pd.DataFrame(
        [
            {"symbol": "AAA", "close": 12.0, "volume": 100_000, "dollar_value": 1_200_000},
            {"symbol": "BBB", "close": 10.5, "volume": 100_000, "dollar_value": 1_050_000},
        ]
    )
    earlier = dynamic_top_gainers(current, {"AAA": 10.0, "BBB": 10.0}, {"AAA": True, "BBB": True}, StrategyConfig(rank_top_n=1), ExecutionConfig())
    assert earlier.iloc[0]["symbol"] == "AAA"
    future = current.copy()
    future.loc[future["symbol"] == "BBB", "close"] = 20.0
    later = dynamic_top_gainers(future, {"AAA": 10.0, "BBB": 10.0}, {"AAA": True, "BBB": True}, StrategyConfig(rank_top_n=1), ExecutionConfig())
    assert earlier.iloc[0]["symbol"] == "AAA"
    assert later.iloc[0]["symbol"] == "BBB"


def test_security_master_excludes_non_common_otc_and_untradable() -> None:
    master = pd.DataFrame(
        [
            {"symbol": "AAA", "asset_type": "COMMON_STOCK", "primary_exchange": "NASDAQ", "tradable": "true", "active": "true"},
            {"symbol": "ETF", "asset_type": "ETF", "primary_exchange": "NYSE", "tradable": True, "active": True},
            {"symbol": "OTC", "asset_type": "COMMON_STOCK", "primary_exchange": "OTC", "tradable": True, "active": True},
            {"symbol": "NOPE", "asset_type": "COMMON_STOCK", "primary_exchange": "NYSE", "tradable": "false", "active": True},
        ]
    )
    result = security_eligibility(master, pd.Timestamp("2025-01-02").date()).set_index("symbol")
    assert bool(result.loc["AAA", "eligible"])
    assert not bool(result.loc["ETF", "eligible"])
    assert not bool(result.loc["OTC", "eligible"])
    assert not bool(result.loc["NOPE", "eligible"])


def test_point_in_time_master_does_not_fallback_for_unknown_symbol() -> None:
    current = pd.DataFrame(
        [
            {"symbol": "KNOWN", "close": 11.0, "volume": 100_000, "dollar_value": 1_100_000},
            {"symbol": "MISSING", "close": 20.0, "volume": 100_000, "dollar_value": 2_000_000},
        ]
    )
    ranked = dynamic_top_gainers(
        current,
        {"KNOWN": 10.0, "MISSING": 10.0},
        {"KNOWN": True},
        StrategyConfig(rank_top_n=20),
        ExecutionConfig(),
    )
    assert ranked["symbol"].tolist() == ["KNOWN"]


def test_point_in_time_master_handles_empty_end_dates_and_listing_boundaries() -> None:
    master = pd.DataFrame(
        [
            {
                "symbol": "ACTIVE",
                "asset_type": "COMMON_STOCK",
                "primary_exchange": "NASDAQ",
                "tradable": True,
                "active": True,
                "effective_date": "2025-01-01",
                "start_date": "2020-01-01",
                "end_date": "",
            },
            {
                "symbol": "FUTURE",
                "asset_type": "COMMON_STOCK",
                "primary_exchange": "NASDAQ",
                "tradable": True,
                "active": True,
                "effective_date": "2025-01-01",
                "start_date": "2025-02-01",
                "end_date": "",
            },
        ]
    )
    result = security_eligibility(master, pd.Timestamp("2025-01-03").date()).set_index("symbol")
    assert bool(result.loc["ACTIVE", "eligible"])
    assert not bool(result.loc["FUTURE", "eligible"])
