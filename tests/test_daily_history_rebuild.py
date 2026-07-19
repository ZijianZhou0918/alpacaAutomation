from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from alpaca_ma5_service.trading_calendar import trading_day_decision
from backtest.daily_history_rebuild import (
    DailyHistoryRebuildConfig,
    parse_daily_bar,
    validate_config,
)
from backtest.history_rebuild_common import classify_common_stock


def test_parse_daily_bar_maps_ohlcv_and_timestamp() -> None:
    parsed = parse_daily_bar(
        "AAPL",
        {
            "t": "2026-07-16T04:00:00Z",
            "o": 210.0,
            "h": 215.0,
            "l": 209.0,
            "c": 214.0,
            "v": 12345,
            "vw": 212.5,
            "n": 987,
        },
        {date(2026, 7, 16)},
    )

    assert parsed is not None
    assert parsed.symbol == "AAPL"
    assert parsed.date == date(2026, 7, 16)
    assert parsed.volume == 12345.0
    assert parsed.vwap == 212.5
    assert parsed.transactions == 987
    assert parsed.timestamp_ms == int(
        datetime(2026, 7, 16, 4, tzinfo=UTC).timestamp() * 1000
    )


def test_parse_daily_bar_skips_non_calendar_date() -> None:
    parsed = parse_daily_bar(
        "AAPL",
        {
            "t": "2026-07-16T04:00:00Z",
            "o": 210.0,
            "h": 215.0,
            "l": 209.0,
            "c": 214.0,
        },
        {date(2026, 7, 15)},
    )

    assert parsed is None


def test_parse_daily_bar_rejects_invalid_ohlc() -> None:
    with pytest.raises(RuntimeError, match="非法 OHLC"):
        parse_daily_bar(
            "AAPL",
            {
                "t": "2026-07-16T04:00:00Z",
                "o": 210.0,
                "h": 208.0,
                "l": 209.0,
                "c": 214.0,
            },
            {date(2026, 7, 16)},
        )


def test_validate_config_rejects_same_staging_and_final(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    with pytest.raises(ValueError, match="staging_path"):
        validate_config(
            DailyHistoryRebuildConfig(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2),
                final_path=path,
                staging_path=path,
                output_dir=tmp_path,
            )
        )


def test_special_2025_national_day_of_mourning_is_closed() -> None:
    decision = trading_day_decision(date(2025, 1, 9), use_alpaca=False)

    assert decision.is_trading_day is False
    assert "Jimmy Carter" in decision.reason


@pytest.mark.parametrize(
    ("symbol", "name"),
    [
        ("GOOG", "Alphabet Inc. Class C Capital Stock"),
        ("V", "VISA Inc."),
        ("T", "AT&T Inc."),
        ("NEM", "Newmont Corporation"),
        ("TSM", "Taiwan Semiconductor Manufacturing Company Ltd."),
    ],
)
def test_classifier_includes_listed_common_stocks_without_common_keyword(
    symbol: str,
    name: str,
) -> None:
    accepted, reason = classify_common_stock(
        {"symbol": symbol, "name": name, "exchange": "NYSE"},
        {"ETF": "N", "Test Issue": "N", "Security Name": name},
    )

    assert accepted is True
    assert reason in {"common_name", "company_name", "listed_equity_fallback"}


@pytest.mark.parametrize(
    ("symbol", "name", "reason"),
    [
        ("KTF", "DWS Municipal Income Trust", "non_operating_trust"),
        (
            "GJR",
            "Synthetic Fixed Income STRATS Trust Securities",
            "excluded_security_type",
        ),
        ("SPAC", "Example Acquisition Corp. Common Stock", "excluded_security_type"),
        ("PREF", "Example Corp. 7% Preferred Stock", "excluded_security_type"),
    ],
)
def test_classifier_still_excludes_non_common_securities(
    symbol: str,
    name: str,
    reason: str,
) -> None:
    accepted, actual_reason = classify_common_stock(
        {"symbol": symbol, "name": name, "exchange": "NYSE"},
        {"ETF": "N", "Test Issue": "N", "Security Name": name},
    )

    assert accepted is False
    assert actual_reason == reason


@pytest.mark.parametrize(
    ("symbol", "name"),
    [
        (
            "GOOD",
            "Gladstone Commercial Corporation Real Estate Investment Trust",
        ),
        ("ABR", "Arbor Realty Trust, Inc. Common Stock"),
        ("BXMT", "Blackstone Mortgage Trust, Inc. Common Stock"),
        ("SBR", "Sabine Royalty Trust Common Stock"),
        ("WASH", "Washington Trust Bancorp, Inc. Common Stock"),
        ("RWT", "Redwood Trust, Inc. Common Stock"),
    ],
)
def test_classifier_keeps_operating_listed_trusts(
    symbol: str,
    name: str,
) -> None:
    accepted, reason = classify_common_stock(
        {
            "symbol": symbol,
            "name": name,
            "exchange": "NYSE",
        },
        {
            "ETF": "N",
            "Test Issue": "N",
            "Security Name": name,
        },
    )

    assert accepted is True
    assert reason in {"common_name", "company_name", "listed_equity_fallback"}


def test_classifier_excludes_closed_end_trust_common_stock() -> None:
    accepted, reason = classify_common_stock(
        {
            "symbol": "RVT",
            "name": "Royce Small-Cap Trust, Inc.",
            "exchange": "NYSE",
        },
        {
            "ETF": "N",
            "Test Issue": "N",
            "Security Name": "Royce Small-Cap Trust, Inc. Common Stock",
        },
    )

    assert accepted is False
    assert reason == "non_operating_trust"
