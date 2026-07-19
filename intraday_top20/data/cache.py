from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from intraday_top20.backtest.config import (
    BacktestConfig,
    DataConfig,
    ExecutionConfig,
    OutputConfig,
    PortfolioConfig,
    StrategyConfig,
)
from intraday_top20.backtest.result import BacktestResult

FRAME_NAMES = [
    "trades",
    "equity_curve",
    "daily_returns",
    "rankings",
    "signals",
    "rejections",
    "audit_bars",
    "state_audit",
    "daily_analysis",
]


class ResultStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def has(self, run_id: str) -> bool:
        return (self.root / run_id / "manifest.json").exists()

    def save(self, result: BacktestResult) -> Path:
        target = self.root / result.run_id
        target.mkdir(parents=True, exist_ok=True)
        metadata = {
            "run_id": result.run_id,
            "config": result.config.to_dict(),
            "metrics": _json_safe(result.metrics),
            "risk": _json_safe(result.risk),
            "data_quality": _json_safe(result.data_quality),
            "validation": _json_safe(result.validation),
            "frames": {},
        }
        for name in FRAME_NAMES:
            frame = getattr(result, name)
            file_name = f"{name}.csv.gz"
            frame.to_csv(target / file_name, index=False, compression="gzip")
            metadata["frames"][name] = {"file": file_name, "rows": len(frame), "columns": frame.columns.tolist()}
        (target / "config.json").write_text(
            json.dumps(result.config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (target / "manifest.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        (target / "run.log").write_text(
            "\n".join(
                [
                    f"run_id={result.run_id}",
                    f"source={result.data_quality.get('source_label', '')}",
                    f"example_mode={result.is_example}",
                    f"range={result.data_quality.get('start_date', '')}..{result.data_quality.get('end_date', '')}",
                    f"bars={result.data_quality.get('five_minute_bar_count', 0)}",
                    f"trades={result.metrics.get('total_trades', 0)}",
                    f"total_return={result.metrics.get('total_return', 0)}",
                    f"credible={result.validation.get('credible_for_strategy_conclusion', False)}",
                    f"validation={json.dumps(_json_safe(result.validation), ensure_ascii=False)}",
                ]
            ),
            encoding="utf-8",
        )
        return target

    def load(self, run_id: str) -> BacktestResult:
        target = self.root / run_id
        metadata = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        frames: dict[str, pd.DataFrame] = {}
        for name in FRAME_NAMES:
            descriptor = metadata.get("frames", {}).get(name)
            if not descriptor:
                frames[name] = pd.DataFrame()
                continue
            try:
                frames[name] = pd.read_csv(target / descriptor["file"])
            except pd.errors.EmptyDataError:
                frames[name] = pd.DataFrame(columns=descriptor.get("columns", []))
        return BacktestResult(
            config=_config_from_dict(metadata["config"]),
            run_id=run_id,
            metrics=metadata["metrics"],
            risk=metadata["risk"],
            data_quality=metadata["data_quality"],
            validation=metadata["validation"],
            **frames,
        )

    def list_runs(self) -> pd.DataFrame:
        records: list[dict[str, Any]] = []
        if not self.root.exists():
            return pd.DataFrame()
        for manifest in self.root.glob("*/manifest.json"):
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
                records.append(
                    {
                        "run_id": raw["run_id"],
                        "source": raw["data_quality"].get("source_label", ""),
                        "example_mode": raw["data_quality"].get("example_mode", False),
                        "start_date": raw["data_quality"].get("start_date", ""),
                        "end_date": raw["data_quality"].get("end_date", ""),
                        "total_return": raw["metrics"].get("total_return", 0.0),
                        "trades": raw["metrics"].get("total_trades", 0),
                    }
                )
            except (OSError, ValueError, KeyError):
                continue
        return pd.DataFrame(records).sort_values("run_id", ascending=False) if records else pd.DataFrame()


def _config_from_dict(raw: dict[str, Any]) -> BacktestConfig:
    return BacktestConfig(
        data=DataConfig(**raw.get("data", {})),
        strategy=StrategyConfig(**raw.get("strategy", {})),
        portfolio=PortfolioConfig(**raw.get("portfolio", {})),
        execution=ExecutionConfig(**raw.get("execution", {})),
        output=OutputConfig(**raw.get("output", {})),
        random_seed=int(raw.get("random_seed", 20260716)),
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and (pd.isna(value) or value in (float("inf"), float("-inf"))):
        return None
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return value
