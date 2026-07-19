from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .config import BacktestConfig
from .engine import IntradayTopGainersBacktester


@dataclass(frozen=True)
class RobustnessScenario:
    name: str
    group: str
    updates: dict[str, dict[str, Any]]


def default_scenarios() -> list[RobustnessScenario]:
    scenarios: list[RobustnessScenario] = []
    for rank in (10, 20, 30):
        for below in (15, 20, 25, 30):
            scenarios.append(
                RobustnessScenario(
                    f"rank_{rank}_below_{below}", "rank_below_grid", {"strategy": {"rank_top_n": rank, "continuous_below_minutes": below}}
                )
            )
    for take_profit in (0.10, 0.15, 0.20, 0.25):
        for latest in ("14:30", "15:00", "15:30"):
            scenarios.append(
                RobustnessScenario(
                    f"tp_{take_profit:.2f}_latest_{latest.replace(':', '')}",
                    "take_profit_entry_grid",
                    {"strategy": {"take_profit_pct": take_profit, "latest_entry_time": latest}},
                )
            )
    scenarios.extend(
        [
            RobustnessScenario("cost_low", "cost", {"execution": {"commission_per_share": 0.0, "minimum_commission": 0.0, "base_slippage_bps": 3.0, "assumed_spread_bps": 6.0}}),
            RobustnessScenario("cost_base", "cost", {}),
            RobustnessScenario("cost_stress", "cost", {"execution": {"commission_per_share": 0.01, "minimum_commission": 1.0, "base_slippage_bps": 25.0, "assumed_spread_bps": 30.0}}),
            RobustnessScenario("vwap", "indicator", {"strategy": {"indicator": "vwap"}}),
            RobustnessScenario("sma_3", "indicator", {"strategy": {"indicator": "sma", "moving_average_window": 3}}),
            RobustnessScenario("sma_5", "indicator", {"strategy": {"indicator": "sma", "moving_average_window": 5}}),
            RobustnessScenario("sma_10", "indicator", {"strategy": {"indicator": "sma", "moving_average_window": 10}}),
            RobustnessScenario("volume_filter_off", "volume_filter", {"strategy": {"require_volume_expansion": False}}),
            RobustnessScenario("volume_filter_on", "volume_filter", {"strategy": {"require_volume_expansion": True}}),
        ]
    )
    return scenarios


def run_robustness(
    base: BacktestConfig,
    *,
    scenarios: list[RobustnessScenario] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> pd.DataFrame:
    scenarios = scenarios or default_scenarios()
    records: list[dict[str, Any]] = []
    cache: dict[str, dict[str, Any]] = {}
    for index, scenario in enumerate(scenarios, start=1):
        config = base.with_updates(**scenario.updates) if scenario.updates else base
        key = config.config_hash()
        if progress:
            progress(index, len(scenarios), scenario.name)
        if key not in cache:
            result = IntradayTopGainersBacktester(config).run()
            cache[key] = {
                **result.metrics,
                **{f"risk_{name}": value for name, value in result.risk.items()},
                "credible": result.validation.get("credible_for_strategy_conclusion", False),
            }
        records.append(
            {
                "scenario": scenario.name,
                "group": scenario.group,
                "rank_top_n": config.strategy.rank_top_n,
                "below_minutes": config.strategy.continuous_below_minutes,
                "take_profit_pct": config.strategy.take_profit_pct,
                "latest_entry_time": config.strategy.latest_entry_time,
                "indicator": config.strategy.indicator,
                "moving_average_window": config.strategy.moving_average_window,
                "volume_filter": config.strategy.require_volume_expansion,
                "commission_per_share": config.execution.commission_per_share,
                "base_slippage_bps": config.execution.base_slippage_bps,
                "assumed_spread_bps": config.execution.assumed_spread_bps,
                **cache[key],
            }
        )
    return pd.DataFrame(records)
