from __future__ import annotations

import pandas as pd

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.data.cache import ResultStore


def test_result_cache_round_trip_supports_empty_frames(tmp_path) -> None:
    result = BacktestResult(
        config=BacktestConfig(),
        run_id="empty-run",
        metrics={"total_trades": 0, "profit_factor": float("inf")},
        risk={},
        data_quality={"example_mode": True},
        validation={"credible_for_strategy_conclusion": False},
        trades=pd.DataFrame(columns=["trade_id", "net_pnl"]),
    )
    store = ResultStore(tmp_path)
    store.save(result)
    loaded = store.load("empty-run")
    assert loaded.trades.empty
    assert loaded.trades.columns.tolist() == ["trade_id", "net_pnl"]
    assert loaded.metrics["profit_factor"] is None
