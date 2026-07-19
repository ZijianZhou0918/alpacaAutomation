from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.visualization.charts import (
    equity_and_drawdown,
    monthly_return_heatmap,
    return_histogram,
    rolling_diagnostics,
)
from intraday_top20.visualization.report import build_html_report
from intraday_top20.web.common import csv_bytes, metric_card, plot_config, require_result


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("核心绩效")
    result = require_result(result)
    if result is None:
        return
    metrics = result.metrics
    metric_specs = [
        ("最终资产", "final_equity", "money"),
        ("总收益率", "total_return", "percent"),
        ("年化收益率", "annualized_return", "percent"),
        ("最大回撤", "max_drawdown", "percent"),
        ("夏普比率", "sharpe_ratio", "number"),
        ("交易次数", "total_trades", "integer"),
        ("胜率", "win_rate", "percent"),
        ("盈亏比", "payoff_ratio", "number"),
        ("Profit Factor", "profit_factor", "number"),
        ("平均每笔收益", "average_trade_return", "percent"),
        ("止盈交易比例", "take_profit_trade_ratio", "percent"),
        ("平均持仓分钟", "average_holding_minutes", "number"),
    ]
    for row_start in range(0, len(metric_specs), 4):
        columns = st.columns(4)
        for column, (label, key, kind) in zip(columns, metric_specs[row_start : row_start + 4]):
            with column:
                metric_card(label, metrics.get(key), kind)

    st.plotly_chart(equity_and_drawdown(result.equity_curve), use_container_width=True, config=plot_config())
    left, right = st.columns(2)
    with left:
        st.plotly_chart(monthly_return_heatmap(result.daily_returns), use_container_width=True, config=plot_config())
    with right:
        st.plotly_chart(return_histogram(result.daily_returns, "return_pct", "每日收益分布"), use_container_width=True, config=plot_config())
    left, right = st.columns(2)
    with left:
        if not result.trades.empty:
            st.plotly_chart(return_histogram(result.trades.dropna(subset=["return_pct"]), "return_pct", "单笔交易收益分布"), use_container_width=True, config=plot_config())
    with right:
        st.plotly_chart(rolling_diagnostics(result.daily_returns), use_container_width=True, config=plot_config())

    st.subheader("结果导出")
    buttons = st.columns(4)
    buttons[0].download_button("交易明细 CSV", csv_bytes(result.trades), "trades.csv", "text/csv", use_container_width=True)
    buttons[1].download_button("每日收益 CSV", csv_bytes(result.daily_returns), "daily_returns.csv", "text/csv", use_container_width=True)
    buttons[2].download_button("资金曲线 CSV", csv_bytes(result.equity_curve), "equity_curve.csv", "text/csv", use_container_width=True)
    buttons[3].download_button("独立 HTML 报告", build_html_report(result).encode("utf-8"), "backtest_report.html", "text/html", use_container_width=True)
    more = st.columns(3)
    more[0].download_button("参数配置 JSON", json.dumps(result.config.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"), "config.json", "application/json", use_container_width=True)
    more[1].download_button("未成交原因 CSV", csv_bytes(result.rejections), "rejections.csv", "text/csv", use_container_width=True)
    log_path = Path(result.config.output.output_root) / result.run_id / "run.log"
    if log_path.exists():
        more[2].download_button("回测日志", log_path.read_bytes(), "run.log", "text/plain", use_container_width=True)
