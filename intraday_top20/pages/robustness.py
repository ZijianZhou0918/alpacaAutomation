from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.backtest.robustness import default_scenarios, run_robustness
from intraday_top20.visualization.charts import robustness_heatmap
from intraday_top20.web.common import csv_bytes, plot_config, require_result


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("稳健性测试")
    result = require_result(result)
    if result is None:
        return
    output = Path(config.output.output_root) / result.run_id / "robustness.csv"
    robustness = pd.read_csv(output) if output.exists() else pd.DataFrame()
    scenarios = default_scenarios()
    st.caption(f"完整方案共 {len(scenarios)} 个场景，串行运行；行情清洗缓存会复用，但每个参数组合独立执行事件回测。")
    if st.button("运行完整稳健性测试", type="primary"):
        progress = st.progress(0.0, text="准备运行")
        message = st.empty()

        def update(current: int, total: int, name: str) -> None:
            progress.progress(current / total, text=f"{current}/{total}: {name}")
            message.info(f"正在运行 {name}")

        try:
            robustness = run_robustness(config, scenarios=scenarios, progress=update)
            output.parent.mkdir(parents=True, exist_ok=True)
            robustness.to_csv(output, index=False)
            progress.progress(1.0, text="稳健性测试完成")
            message.success("结果已缓存")
        except Exception as exc:
            st.error(f"稳健性测试失败：{type(exc).__name__}: {exc}")
            st.exception(exc)
            return

    if robustness.empty:
        st.info("尚未运行稳健性测试。页面不会用占位数字填充结果。")
        return
    robustness = _score(robustness)
    st.download_button("下载稳健性结果 CSV", csv_bytes(robustness), "robustness.csv", "text/csv")
    st.subheader("参数结果与综合排名")
    st.caption("综合分同时考虑总收益、夏普、最大回撤和交易数量；不会只按收益率挑选最优参数。")
    display = robustness.sort_values("robustness_score", ascending=False)
    st.dataframe(
        display[["scenario", "group", "total_return", "max_drawdown", "sharpe_ratio", "total_trades", "profit_factor", "robustness_score", "credible"]],
        use_container_width=True,
        hide_index=True,
    )

    rank_grid = robustness.loc[robustness["group"] == "rank_below_grid"]
    if not rank_grid.empty:
        st.subheader("Top N × 连续跌破分钟")
        columns = st.columns(3)
        columns[0].plotly_chart(robustness_heatmap(rank_grid, "rank_top_n", "below_minutes", "total_return", "总收益率"), use_container_width=True, config=plot_config())
        columns[1].plotly_chart(robustness_heatmap(rank_grid, "rank_top_n", "below_minutes", "max_drawdown", "最大回撤"), use_container_width=True, config=plot_config())
        columns[2].plotly_chart(robustness_heatmap(rank_grid, "rank_top_n", "below_minutes", "sharpe_ratio", "夏普比率"), use_container_width=True, config=plot_config())
    tp_grid = robustness.loc[robustness["group"] == "take_profit_entry_grid"]
    if not tp_grid.empty:
        st.subheader("止盈比例 × 最晚开仓")
        st.plotly_chart(robustness_heatmap(tp_grid, "take_profit_pct", "latest_entry_time", "total_return", "总收益率"), use_container_width=True, config=plot_config())

    st.subheader("交易次数、成本、均线与成交量过滤")
    trade_counts = robustness.sort_values(["group", "scenario"])
    trade_count_chart = px.bar(
        trade_counts,
        x="scenario",
        y="total_trades",
        color="group",
        title="各参数场景交易次数",
        labels={"scenario": "参数场景", "total_trades": "交易次数", "group": "场景组"},
    )
    trade_count_chart.update_layout(
        template="plotly_white",
        xaxis_tickangle=-45,
        hovermode="x unified",
        legend_title_text="场景组",
    )
    st.plotly_chart(trade_count_chart, use_container_width=True, config=plot_config())
    for group in ["cost", "indicator", "volume_filter"]:
        subset = robustness.loc[robustness["group"] == group]
        if not subset.empty:
            st.caption(group)
            st.dataframe(subset, use_container_width=True, hide_index=True)


def _score(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    total_trades = pd.to_numeric(result["total_trades"], errors="coerce").fillna(0)
    trade_score = np.log1p(total_trades).rank(pct=True)
    result["robustness_score"] = (
        pd.to_numeric(result["total_return"], errors="coerce").rank(pct=True)
        + pd.to_numeric(result["sharpe_ratio"], errors="coerce").rank(pct=True)
        + pd.to_numeric(result["max_drawdown"], errors="coerce").rank(pct=True)
        + trade_score
    ) / 4.0
    return result
