from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .config import BacktestConfig


@dataclass
class BacktestResult:
    config: BacktestConfig
    run_id: str
    metrics: dict[str, Any]
    risk: dict[str, Any]
    data_quality: dict[str, Any]
    validation: dict[str, Any]
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    daily_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    rankings: pd.DataFrame = field(default_factory=pd.DataFrame)
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)
    rejections: pd.DataFrame = field(default_factory=pd.DataFrame)
    audit_bars: pd.DataFrame = field(default_factory=pd.DataFrame)
    state_audit: pd.DataFrame = field(default_factory=pd.DataFrame)
    daily_analysis: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def is_example(self) -> bool:
        return bool(self.data_quality.get("example_mode", False))
