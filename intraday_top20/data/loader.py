from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from alpaca_ma5_service.trading_calendar import offline_trading_day_decision
from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.universe import security_eligibility

from .cleaner import aggregate_to_five_minutes

DATE_PATTERN = re.compile(r"(?P<date>20\d{2}[-_]?[01]\d[-_]?[0-3]\d)")


@dataclass
class DailyMarketData:
    trade_date: date
    bars: pd.DataFrame
    previous_closes: dict[str, float]
    eligibility: dict[str, bool]


class MarketDataLoader:
    """Chunked reader for one full-market aggregate file per trading date."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.data_config = config.data
        self.data_dir = Path(self.data_config.data_dir)
        self.market_cache_dir = Path(config.output.output_root) / "market_cache"
        self.security_master = self._load_optional_csv(self.data_config.security_master_path)
        self.splits = self._load_optional_csv(self.data_config.splits_path)
        self.files = self._discover_files()
        self.data_quality: dict[str, Any] = {}

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path, day in self.files:
            stat = path.stat()
            digest.update(f"{path.resolve()}|{day}|{stat.st_size}|{stat.st_mtime_ns}".encode())
        for raw in (self.data_config.security_master_path, self.data_config.splits_path):
            if raw and Path(raw).exists():
                stat = Path(raw).stat()
                digest.update(f"{Path(raw).resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode())
        return digest.hexdigest()[:16]

    def iter_days(self) -> Iterator[DailyMarketData]:
        if not self.files:
            raise FileNotFoundError(
                f"{self.data_dir} 中没有带 YYYY-MM-DD 日期的 {self.data_config.file_glob} 行情文件"
            )
        start = date.fromisoformat(self.data_config.start_date)
        end = date.fromisoformat(self.data_config.end_date)
        previous_closes: dict[str, float] = {}
        selected_days: list[date] = []
        symbols_seen: set[str] = set()
        missing_previous = 0
        symbols_without_previous: set[str] = set()
        incomplete_symbol_sessions = 0
        total_rows = 0
        for path, file_date in self.files:
            if file_date > end:
                break
            bars = self._read_daily_file(path)
            if bars.empty:
                continue
            actual_dates = set(bars["timestamp"].dt.date.unique())
            if len(actual_dates) != 1:
                raise ValueError(f"{path} 必须只包含一个美东交易日，实际为 {sorted(actual_dates)}")
            actual_date = next(iter(actual_dates))
            if actual_date != file_date:
                raise ValueError(f"{path.name} 文件名日期 {file_date} 与行情美东日期 {actual_date} 不一致")
            adjusted_previous = self._adjust_previous_for_splits(actual_date, previous_closes)
            if start <= actual_date <= end:
                eligible_frame = security_eligibility(self.security_master, actual_date)
                eligibility = eligible_frame.set_index("symbol")["eligible"].astype(bool).to_dict()
                if not self.security_master.empty:
                    for symbol in bars["symbol"].unique():
                        eligibility.setdefault(str(symbol), False)
                selected_days.append(actual_date)
                symbols_seen.update(bars["symbol"].unique())
                total_rows += len(bars)
                missing_previous += int((~bars["symbol"].isin(adjusted_previous)).sum())
                symbols_without_previous.update(set(bars.loc[~bars["symbol"].isin(adjusted_previous), "symbol"]))
                incomplete_symbol_sessions += int((bars.groupby("symbol", observed=True).size() < 78).sum())
                yield DailyMarketData(actual_date, bars, adjusted_previous.copy(), eligibility)
            # Preserve the last official regular-session close for symbols
            # with no bar today (for example a full-day halt).  Symbols that
            # did trade receive today's final close for tomorrow's ranking.
            daily_closes = (
                bars.sort_values("timestamp").groupby("symbol", observed=True)["close"].last().astype(float).to_dict()
            )
            previous_closes.update(daily_closes)

        self.data_quality = self._quality_summary(
            selected_days,
            symbols_seen,
            total_rows,
            missing_previous,
            symbols_without_previous,
            incomplete_symbol_sessions,
        )

    def _discover_files(self) -> list[tuple[Path, date]]:
        if not self.data_dir.exists():
            return []
        ignored = {
            Path(value).resolve()
            for value in [self.data_config.security_master_path, self.data_config.splits_path]
            if value
        }
        found: list[tuple[Path, date]] = []
        for path in self.data_dir.glob(self.data_config.file_glob):
            if not path.is_file() or path.resolve() in ignored:
                continue
            match = DATE_PATTERN.search(path.name)
            if not match:
                continue
            digits = match.group("date").replace("_", "-")
            if "-" not in digits:
                digits = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
            found.append((path, date.fromisoformat(digits)))
        return sorted(found, key=lambda item: (item[1], item[0].name))

    def _read_daily_file(self, path: Path) -> pd.DataFrame:
        stat = path.stat()
        cache_key = hashlib.sha256(f"v1|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:20]
        cache_path = self.market_cache_dir / f"{cache_key}.parquet"
        if cache_path.exists():
            return pd.read_parquet(cache_path)
        suffixes = "".join(path.suffixes).lower()
        if suffixes.endswith(".parquet"):
            result = aggregate_to_five_minutes([pd.read_parquet(path)])
        else:
            chunks = pd.read_csv(path, chunksize=self.data_config.csv_chunksize)
            result = aggregate_to_five_minutes(chunks)
        self.market_cache_dir.mkdir(parents=True, exist_ok=True)
        result.to_parquet(cache_path, index=False)
        return result

    def _adjust_previous_for_splits(self, day: date, closes: dict[str, float]) -> dict[str, float]:
        adjusted = closes.copy()
        if self.data_config.source_adjusted or self.splits.empty:
            return adjusted
        splits = self.splits.copy()
        date_column = next((name for name in ["execution_date", "ex_date", "date"] if name in splits), None)
        if not date_column or "symbol" not in splits:
            return adjusted
        matches = splits.loc[pd.to_datetime(splits[date_column], errors="coerce").dt.date == day]
        for row in matches.to_dict("records"):
            symbol = str(row["symbol"]).upper()
            split_from = float(row.get("split_from", row.get("from", 1.0)))
            split_to = float(row.get("split_to", row.get("to", 1.0)))
            if symbol in adjusted and split_from > 0 and split_to > 0:
                adjusted[symbol] *= split_from / split_to
        return adjusted

    def _quality_summary(
        self,
        selected_days: list[date],
        symbols: set[str],
        total_rows: int,
        missing_previous: int,
        symbols_without_previous: set[str],
        incomplete_symbol_sessions: int,
    ) -> dict[str, Any]:
        splits_file_present = bool(
            self.data_config.splits_path and Path(self.data_config.splits_path).is_file()
        )
        point_in_time_master = bool(
            not self.security_master.empty
            and "effective_date" in self.security_master.columns
            and ("start_date" in self.security_master.columns or "end_date" in self.security_master.columns)
        )
        selected_set = set(selected_days)
        missing_days: list[str] = []
        cursor = date.fromisoformat(self.data_config.start_date)
        end = date.fromisoformat(self.data_config.end_date)
        while cursor <= end:
            if offline_trading_day_decision(cursor).is_trading_day and cursor not in selected_set:
                missing_days.append(cursor.isoformat())
            cursor += timedelta(days=1)
        return {
            "source_label": self.data_config.source_label,
            "example_mode": self.data_config.example_mode,
            "source_adjusted": self.data_config.source_adjusted,
            "contains_delisted": self.data_config.contains_delisted,
            "security_master_present": not self.security_master.empty,
            "point_in_time_security_master": point_in_time_master,
            # A schema-correct but empty corporate-action file is valid for a
            # period in which no split occurred.  Track file coverage rather
            # than requiring at least one event row.
            "splits_present": splits_file_present,
            "file_count": len(self.files),
            "data_updated_at": (
                datetime.fromtimestamp(max(path.stat().st_mtime for path, _ in self.files)).astimezone().isoformat()
                if self.files
                else ""
            ),
            "selected_day_count": len(selected_days),
            "start_date": min(selected_days).isoformat() if selected_days else "",
            "end_date": max(selected_days).isoformat() if selected_days else "",
            "symbol_count": len(symbols),
            "five_minute_bar_count": total_rows,
            "bar_rows_without_previous_close": missing_previous,
            "symbols_without_previous_close_count": len(symbols_without_previous),
            "symbol_sessions_with_fewer_than_78_bars": incomplete_symbol_sessions,
            "missing_trading_days": missing_days,
            "session": "09:30-16:00 America/New_York",
            "reliable_for_strategy_claim": bool(
                selected_days
                and not self.data_config.example_mode
                and self.data_config.contains_delisted
                and point_in_time_master
                and (self.data_config.source_adjusted or splits_file_present)
                and not missing_days
            ),
        }

    @staticmethod
    def _load_optional_csv(raw_path: str) -> pd.DataFrame:
        if not raw_path:
            return pd.DataFrame()
        path = Path(raw_path)
        return pd.read_csv(path) if path.exists() else pd.DataFrame()
