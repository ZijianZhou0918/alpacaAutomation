from __future__ import annotations

import re
from datetime import date

import pandas as pd

from .config import ExecutionConfig, StrategyConfig


COMMON_TYPES = {"CS", "COMMON", "COMMON_STOCK", "COMMON STOCK", "STOCK"}
EXCLUDED_TYPES = {"ETF", "ETN", "WARRANT", "RIGHT", "PREFERRED", "PREFERRED_STOCK", "UNIT", "FUND"}
OTC_EXCHANGES = {"OTC", "OTCQX", "OTCQB", "PINK"}


def security_eligibility(master: pd.DataFrame, target_date: date) -> pd.DataFrame:
    if master.empty:
        return pd.DataFrame(columns=["symbol", "eligible", "exclusion_reason"])
    frame = master.copy()
    target_timestamp = pd.Timestamp(target_date)
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    if "effective_date" in frame:
        frame["effective_date"] = pd.to_datetime(frame["effective_date"], errors="coerce").dt.normalize()
        frame = frame.loc[frame["effective_date"].isna() | (frame["effective_date"] <= target_timestamp)]
        frame = frame.sort_values(["symbol", "effective_date"], na_position="first").drop_duplicates("symbol", keep="last")
    for column in ("start_date", "end_date"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.normalize()
    eligible = pd.Series(True, index=frame.index)
    reason = pd.Series("", index=frame.index, dtype="object")
    if "start_date" in frame:
        mask = frame["start_date"].notna() & (frame["start_date"] > target_timestamp)
        eligible &= ~mask
        reason.mask(mask, "not_yet_listed", inplace=True)
    if "end_date" in frame:
        mask = frame["end_date"].notna() & (frame["end_date"] < target_timestamp)
        eligible &= ~mask
        reason.mask(mask, "delisted", inplace=True)
    if "asset_type" in frame:
        types = frame["asset_type"].fillna("").astype(str).str.upper()
        mask = types.isin(EXCLUDED_TYPES) | (~types.isin(COMMON_TYPES) & types.ne(""))
        eligible &= ~mask
        reason.mask(mask, "non_common_stock", inplace=True)
    if "primary_exchange" in frame:
        mask = frame["primary_exchange"].fillna("").astype(str).str.upper().isin(OTC_EXCHANGES)
        eligible &= ~mask
        reason.mask(mask, "otc", inplace=True)
    for column, label in (("tradable", "not_tradable"), ("active", "inactive")):
        if column in frame:
            values = frame[column].map(_as_bool).fillna(False)
            mask = ~values
            eligible &= ~mask
            reason.mask(mask, label, inplace=True)
    frame["eligible"] = eligible
    frame["exclusion_reason"] = reason
    return frame[["symbol", "eligible", "exclusion_reason"]].drop_duplicates("symbol", keep="last")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "active"}


def fallback_symbol_is_eligible(symbol: str) -> bool:
    """Conservative syntax fallback when point-in-time reference data is absent."""
    symbol = symbol.upper()
    if not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", symbol):
        return False
    return not symbol.endswith(("W", "WS", "R", "U", "P"))


def dynamic_top_gainers(
    current_bars: pd.DataFrame,
    previous_closes: dict[str, float],
    eligibility: dict[str, bool],
    strategy: StrategyConfig,
    execution: ExecutionConfig,
) -> pd.DataFrame:
    """Rank only symbols with a completed bar at the current timestamp."""
    if current_bars.empty:
        return current_bars.assign(gain_pct=pd.Series(dtype=float), rank=pd.Series(dtype=int))
    frame = current_bars.copy()
    frame["previous_close"] = frame["symbol"].map(previous_closes)
    frame["gain_pct"] = frame["close"] / frame["previous_close"] - 1.0
    frame["eligible_reference"] = frame["symbol"].map(eligibility)
    use_symbol_syntax_fallback = not eligibility
    frame["eligible_reference"] = frame.apply(
        lambda row: (
            fallback_symbol_is_eligible(row["symbol"])
            if pd.isna(row["eligible_reference"]) and use_symbol_syntax_fallback
            else bool(row["eligible_reference"])
            if not pd.isna(row["eligible_reference"])
            else False
        ),
        axis=1,
    )
    mask = (
        frame["eligible_reference"]
        & frame["previous_close"].notna()
        & (frame["previous_close"] > 0)
        & (frame["close"] >= execution.min_price)
        & (frame["dollar_value"] >= execution.min_five_minute_dollar_volume)
        & (frame["volume"] > 0)
    )
    ranked = frame.loc[mask].sort_values(["gain_pct", "dollar_value", "symbol"], ascending=[False, False, True]).head(strategy.rank_top_n).copy()
    ranked["rank"] = range(1, len(ranked) + 1)
    return ranked
