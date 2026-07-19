from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

EASTERN = "America/New_York"
REQUIRED_COLUMNS = {"symbol", "timestamp", "open", "high", "low", "close", "volume"}


def standardize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize Massive-style or conventional aggregate columns without altering prices."""
    aliases = {
        "ticker": "symbol",
        "window_start": "timestamp",
        "t": "timestamp",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "vw": "source_vwap",
        "n": "transactions",
    }
    normalized = frame.rename(columns={column: aliases.get(column.lower(), column.lower()) for column in frame.columns})
    missing = REQUIRED_COLUMNS.difference(normalized.columns)
    if missing:
        raise ValueError(f"行情文件缺少必需字段: {sorted(missing)}")

    result = normalized.copy()
    result["symbol"] = result["symbol"].astype(str).str.upper().str.strip()
    result["timestamp"] = _parse_timestamp(result["timestamp"])
    for column in ["open", "high", "low", "close", "volume", "source_vwap", "dollar_value", "transactions"]:
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["symbol", "timestamp", "open", "high", "low", "close", "volume"])
    result = result.loc[
        (result["symbol"] != "")
        & (result[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (result["volume"] >= 0)
    ]
    local_time = result["timestamp"].dt.tz_convert(EASTERN)
    minutes = local_time.dt.hour * 60 + local_time.dt.minute
    result = result.loc[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)].copy()
    result["timestamp"] = local_time.loc[result.index]
    return result


def aggregate_to_five_minutes(chunks: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Aggregate chunks to completed, regular-session five-minute bars."""
    partials: list[pd.DataFrame] = []
    for raw in chunks:
        clean = standardize_columns(raw)
        if clean.empty:
            continue
        clean["bucket"] = clean["timestamp"].dt.floor("5min")
        typical = (clean["high"] + clean["low"] + clean["close"]) / 3.0
        if "dollar_value" in clean:
            clean["bar_dollar_value"] = clean["dollar_value"].fillna(typical * clean["volume"])
        elif "source_vwap" in clean:
            clean["bar_dollar_value"] = clean["source_vwap"].fillna(typical) * clean["volume"]
        else:
            clean["bar_dollar_value"] = typical * clean["volume"]
        if "transactions" not in clean:
            clean["transactions"] = np.nan
        clean = clean.sort_values(["symbol", "timestamp"])
        grouped = clean.groupby(["symbol", "bucket"], sort=False, observed=True)
        partials.append(
            grouped.agg(
                first_timestamp=("timestamp", "first"),
                last_timestamp=("timestamp", "last"),
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                dollar_value=("bar_dollar_value", "sum"),
                transactions=("transactions", "sum"),
            ).reset_index()
        )
    if not partials:
        return _empty_bars()
    merged = pd.concat(partials, ignore_index=True).sort_values(["symbol", "bucket", "first_timestamp"])
    grouped = merged.groupby(["symbol", "bucket"], sort=False, observed=True)
    result = grouped.agg(
        first_timestamp=("first_timestamp", "first"),
        last_timestamp=("last_timestamp", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        dollar_value=("dollar_value", "sum"),
        transactions=("transactions", "sum"),
    ).reset_index()
    result = result.rename(columns={"bucket": "timestamp"}).drop(columns=["first_timestamp", "last_timestamp"])
    result["bar_end"] = result["timestamp"] + pd.Timedelta(minutes=5)
    return result.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def _parse_timestamp(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().mean() > 0.95:
        median = float(numeric.dropna().abs().median())
        unit = "ns" if median >= 1e17 else "ms" if median >= 1e12 else "s"
        return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    return pd.to_datetime(values, utc=True, errors="coerce")


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "timestamp", "open", "high", "low", "close", "volume", "dollar_value", "transactions", "bar_end"]
    )
