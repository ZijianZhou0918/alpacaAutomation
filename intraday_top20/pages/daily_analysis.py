from __future__ import annotations

import pandas as pd
import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.web.common import csv_bytes, require_result


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("每日分析")
    result = require_result(result)
    if result is None or result.daily_analysis.empty:
        return
    dates = result.daily_analysis["date"].astype(str).tolist()
    selected = st.selectbox("交易日", dates)
    row = result.daily_analysis.loc[result.daily_analysis["date"].astype(str) == selected].iloc[0]
    columns = st.columns(8)
    columns[0].metric("信号", int(row["signals"]))
    columns[1].metric("实际成交", int(row["filled_entries"]))
    columns[2].metric("拒绝", int(row["rejected_signals"]))
    columns[3].metric("资金/容量拒绝", int(row.get("funding_or_capacity_rejections", 0)))
    columns[4].metric("流动性/缺K拒绝", int(row.get("liquidity_or_missing_bar_rejections", 0)))
    columns[5].metric("当日收益", f"{float(row['daily_return']):.2%}")
    columns[6].metric("当日最大回撤", f"{float(row['intraday_max_drawdown']):.2%}")
    columns[7].metric("收盘持仓", int(row["ending_positions"]))

    st.subheader("每个五分钟时刻的动态涨幅榜")
    ranks = result.rankings.loc[result.rankings["date"].astype(str) == selected].copy()
    ranks["timestamp"] = pd.to_datetime(ranks["timestamp"])
    rank_table = ranks.pivot(index="timestamp", columns="rank", values="symbol").sort_index()
    st.dataframe(rank_table, use_container_width=True)
    st.download_button("下载当日完整涨幅榜 CSV", csv_bytes(ranks), f"top_gainers_{selected}.csv", "text/csv")

    st.subheader("信号、成交与未成交原因")
    signals = result.signals.loc[result.signals["trade_date"].astype(str) == selected] if not result.signals.empty else pd.DataFrame()
    rejections = result.rejections.loc[result.rejections["date"].astype(str) == selected] if not result.rejections.empty else pd.DataFrame()
    left, right = st.columns(2)
    with left:
        st.caption("信号")
        st.dataframe(signals, use_container_width=True, hide_index=True)
    with right:
        st.caption("未成交 / 拒绝")
        st.dataframe(rejections, use_container_width=True, hide_index=True)
        if not rejections.empty:
            st.bar_chart(rejections["reason"].value_counts())

    st.subheader("当日持仓变化")
    trades = result.trades.loc[result.trades["date"].astype(str) == selected].copy()
    if trades.empty:
        st.info("当日无成交。")
    else:
        st.dataframe(trades[["symbol", "entry_time", "entry_quantity", "take_profit_time", "take_profit_quantity", "tail_exit_time", "tail_exit_quantity", "net_pnl", "close_reason"]], use_container_width=True, hide_index=True)
