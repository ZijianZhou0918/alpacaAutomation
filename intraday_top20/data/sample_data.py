from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from alpaca_ma5_service.trading_calendar import offline_trading_day_decision

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def generate_example_data(output_dir: str | Path, *, seed: int = 20260716) -> dict[str, object]:
    """Create deterministic synthetic full-market-shaped bars for functional validation only."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    common = [f"SIM{chr(65 + first)}{chr(65 + second)}" for first in range(4) for second in range(8)]
    excluded = ["SIMETF", "SIMW", "SIMP", "SIMOTC"]
    symbols = common + excluded
    metadata = []
    for symbol in common:
        metadata.append(
            {
                "symbol": symbol,
                "asset_type": "COMMON_STOCK",
                "primary_exchange": "NASDAQ",
                "tradable": True,
                "active": True,
                "start_date": "2020-01-01",
                "end_date": "",
                "effective_date": "2025-01-01",
            }
        )
    metadata.extend(
        [
            {"symbol": "SIMETF", "asset_type": "ETF", "primary_exchange": "NYSE", "tradable": True, "active": True},
            {"symbol": "SIMW", "asset_type": "WARRANT", "primary_exchange": "NASDAQ", "tradable": True, "active": True},
            {"symbol": "SIMP", "asset_type": "PREFERRED", "primary_exchange": "NYSE", "tradable": True, "active": True},
            {"symbol": "SIMOTC", "asset_type": "COMMON_STOCK", "primary_exchange": "OTC", "tradable": True, "active": True},
        ]
    )
    pd.DataFrame(metadata).to_csv(root / "security_master.csv", index=False)

    start, end = date(2025, 1, 2), date(2025, 2, 7)
    trading_days: list[date] = []
    cursor = start
    while cursor <= end:
        if offline_trading_day_decision(cursor).is_trading_day:
            trading_days.append(cursor)
        cursor += timedelta(days=1)

    previous = {symbol: 4.0 + index * 0.9 for index, symbol in enumerate(symbols)}
    for day_index, day in enumerate(trading_days):
        candidate = common[day_index % 8]
        rows: list[dict[str, object]] = []
        for symbol_index, symbol in enumerate(symbols):
            prior = previous[symbol]
            baseline_gap = 0.02 + (len(symbols) - symbol_index) * 0.002
            gap = 0.18 if symbol == candidate else baseline_gap + float(rng.normal(0, 0.004))
            values = _price_path(prior, gap, symbol == candidate, day_index, rng)
            for bar_index, (bar_open, high, low, close, volume) in enumerate(values):
                # One synthetic halt validates that a missing 15:55 bar never receives an idealized fill.
                if symbol == common[0] and day_index == 9 and bar_index >= 54:
                    continue
                local_timestamp = datetime.combine(day, time(9, 30), EASTERN) + timedelta(minutes=5 * bar_index)
                rows.append(
                    {
                        "ticker": symbol,
                        "window_start": local_timestamp.astimezone(UTC).isoformat(),
                        "open": round(bar_open, 6),
                        "high": round(high, 6),
                        "low": round(low, 6),
                        "close": round(close, 6),
                        "volume": int(volume),
                        "transactions": max(1, int(volume / 120)),
                    }
                )
            previous[symbol] = values[-1][3]
        pd.DataFrame(rows).sort_values(["window_start", "ticker"]).to_csv(
            root / f"synthetic_full_market_{day.isoformat()}.csv.gz", index=False, compression="gzip"
        )
    return {
        "output_dir": str(root),
        "trading_days": len(trading_days),
        "symbols": len(symbols),
        "files": len(trading_days),
        "label": "SYNTHETIC EXAMPLE - NOT REAL MARKET PERFORMANCE",
    }


def _price_path(
    prior: float,
    gap: float,
    candidate: bool,
    day_index: int,
    rng: np.random.Generator,
) -> list[tuple[float, float, float, float, int]]:
    closes: list[float] = []
    if candidate:
        pattern = [1.18, 1.21, 1.24, 1.25, 1.24, 1.11, 1.10, 1.09, 1.10, 1.11, 1.31]
        closes.extend(prior * value for value in pattern)
        target_finish = prior * (1.03 if day_index % 3 == 0 else 1.28 if day_index % 3 == 1 else 1.12)
        remaining = 78 - len(closes)
        closes.extend(np.linspace(closes[-1], target_finish, remaining))
    else:
        current = prior * (1 + gap)
        for _ in range(78):
            current = max(0.55, current * (1 + float(rng.normal(0, 0.003))))
            closes.append(current)

    bars: list[tuple[float, float, float, float, int]] = []
    previous_close = prior * (1 + gap * 0.8)
    for index, close in enumerate(closes):
        bar_open = previous_close
        high = max(bar_open, close) * (1.003 + abs(float(rng.normal(0, 0.001))))
        low = min(bar_open, close) * (0.997 - abs(float(rng.normal(0, 0.0007))))
        if candidate and day_index % 3 == 1 and index == 22:
            high = max(high, closes[11] * 1.24)
        volume = 160_000 + int(rng.integers(0, 90_000))
        if candidate and index == 10:
            volume *= 3
        bars.append((float(bar_open), float(high), float(low), float(close), volume))
        previous_close = close
    return bars


if __name__ == "__main__":
    print(generate_example_data(Path(__file__).resolve().parents[1] / "example_data"))
