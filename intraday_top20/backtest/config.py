from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, time
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DataConfig:
    data_dir: str = "intraday_top20/example_data"
    file_glob: str = "*.csv*"
    security_master_path: str = "intraday_top20/example_data/security_master.csv"
    splits_path: str = ""
    source_label: str = "synthetic_example"
    start_date: str = "2025-01-03"
    end_date: str = "2025-02-07"
    source_adjusted: bool = False
    contains_delisted: bool = False
    example_mode: bool = True
    csv_chunksize: int = 500_000


@dataclass(frozen=True)
class StrategyConfig:
    rank_top_n: int = 20
    continuous_below_minutes: int = 20
    take_profit_pct: float = 0.20
    latest_entry_time: str = "15:00"
    indicator: str = "vwap"
    moving_average_window: int = 5
    require_volume_expansion: bool = False
    volume_lookback_bars: int = 5
    volume_expansion_multiplier: float = 1.50
    allow_repeat_symbol: bool = False
    max_trades_per_symbol_per_day: int = 1

    @property
    def required_below_bars(self) -> int:
        """Number of complete five-minute bars needed to be strictly over the threshold."""
        return self.continuous_below_minutes // 5 + 1


@dataclass(frozen=True)
class PortfolioConfig:
    initial_capital: float = 100_000.0
    max_position_pct: float = 0.10
    max_concurrent_positions: int = 10
    max_daily_entries: int = 10
    fractional_shares: bool = True


@dataclass(frozen=True)
class ExecutionConfig:
    min_price: float = 1.0
    min_five_minute_dollar_volume: float = 100_000.0
    max_volume_participation: float = 0.01
    commission_per_share: float = 0.005
    minimum_commission: float = 1.0
    base_slippage_bps: float = 8.0
    assumed_spread_bps: float = 10.0
    low_price_threshold: float = 5.0
    low_price_extra_slippage_bps: float = 20.0
    high_volatility_range_pct: float = 0.08
    high_volatility_extra_slippage_bps: float = 15.0
    take_profit_fraction: float = 0.50
    force_exit_time: str = "15:55"


@dataclass(frozen=True)
class OutputConfig:
    output_root: str = "intraday_top20/outputs"
    cache_results: bool = True
    save_audit_bars: bool = True


@dataclass(frozen=True)
class BacktestConfig:
    data: DataConfig = field(default_factory=DataConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    random_seed: int = 20260716

    def validate(self) -> None:
        start = date.fromisoformat(self.data.start_date)
        end = date.fromisoformat(self.data.end_date)
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        if self.strategy.rank_top_n < 1:
            raise ValueError("rank_top_n must be positive")
        if self.strategy.continuous_below_minutes < 0:
            raise ValueError("continuous_below_minutes cannot be negative")
        time.fromisoformat(self.strategy.latest_entry_time)
        time.fromisoformat(self.execution.force_exit_time)
        if self.strategy.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive")
        if self.strategy.indicator not in {"vwap", "sma"}:
            raise ValueError("indicator must be 'vwap' or 'sma'")
        if self.strategy.moving_average_window < 1:
            raise ValueError("moving_average_window must be positive")
        for name, value in {
            "max_position_pct": self.portfolio.max_position_pct,
            "max_volume_participation": self.execution.max_volume_participation,
            "take_profit_fraction": self.execution.take_profit_fraction,
        }.items():
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.portfolio.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.portfolio.max_concurrent_positions < 1 or self.portfolio.max_daily_entries < 1:
            raise ValueError("position and daily entry limits must be positive")
        if self.execution.min_price < 0 or self.execution.min_five_minute_dollar_volume < 0:
            raise ValueError("price and dollar-volume filters cannot be negative")
        if min(
            self.execution.commission_per_share,
            self.execution.minimum_commission,
            self.execution.base_slippage_bps,
            self.execution.assumed_spread_bps,
        ) < 0:
            raise ValueError("cost assumptions cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self, data_fingerprint: str = "") -> str:
        payload = {"config": self.to_dict(), "data_fingerprint": data_fingerprint, "engine_version": 2}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def with_updates(self, **sections: dict[str, Any]) -> "BacktestConfig":
        value = self
        for section, updates in sections.items():
            current = getattr(value, section)
            value = replace(value, **{section: replace(current, **updates)})
        value.validate()
        return value


def load_config(path: str | Path) -> BacktestConfig:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency error is surfaced in the app
        raise RuntimeError("PyYAML is required to load the YAML configuration") from exc

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    config = BacktestConfig(
        data=DataConfig(**raw.get("data", {})),
        strategy=StrategyConfig(**raw.get("strategy", {})),
        portfolio=PortfolioConfig(**raw.get("portfolio", {})),
        execution=ExecutionConfig(**raw.get("execution", {})),
        output=OutputConfig(**raw.get("output", {})),
        random_seed=int(raw.get("random_seed", 20260716)),
    )
    config.validate()
    return config


def save_config(config: BacktestConfig, path: str | Path) -> None:
    Path(path).write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
