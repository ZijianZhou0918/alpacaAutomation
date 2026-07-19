from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.web.common import csv_bytes, excel_bytes, require_result


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("交易明细")
    result = require_result(result)
    if result is None or result.trades.empty:
        st.info("当前结果没有交易记录。")
        return
    frame = result.trades.copy()
    frame["date_value"] = pd.to_datetime(frame["date"]).dt.date
    frame["entry_datetime"] = pd.to_datetime(frame["entry_time"])
    st.subheader("筛选")
    row1 = st.columns(5)
    selected_dates = row1[0].date_input("日期范围", (frame["date_value"].min(), frame["date_value"].max()), key="trade_dates")
    symbols = row1[1].multiselect("股票代码", sorted(frame["symbol"].unique()))
    pnl_filter = row1[2].selectbox("盈亏", ["全部", "盈利", "亏损"])
    tp_filter = row1[3].selectbox("止盈状态", ["全部", "已止盈", "未止盈"])
    rank_limit = row1[4].slider("信号排名", 1, max(1, int(frame["rank_at_signal"].max())), (1, max(1, int(frame["rank_at_signal"].max()))))
    row2 = st.columns(3)
    finite_returns = frame["return_pct"].dropna()
    return_bounds = (float(finite_returns.min()), float(finite_returns.max())) if not finite_returns.empty else (0.0, 0.0)
    return_range = row2[0].slider("单笔收益率", return_bounds[0], return_bounds[1], return_bounds)
    hold_max = max(1.0, float(frame["holding_minutes"].dropna().max() if frame["holding_minutes"].notna().any() else 1.0))
    holding_range = row2[1].slider("持仓分钟数", 0.0, hold_max, (0.0, hold_max))
    entry_period = row2[2].selectbox("买入时间段", ["全部", "09:30–11:00", "11:00–13:30", "13:30以后"])

    filtered = frame.copy()
    if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
        filtered = filtered.loc[filtered["date_value"].between(selected_dates[0], selected_dates[1])]
    if symbols:
        filtered = filtered.loc[filtered["symbol"].isin(symbols)]
    if pnl_filter == "盈利":
        filtered = filtered.loc[filtered["net_pnl"] > 0]
    elif pnl_filter == "亏损":
        filtered = filtered.loc[filtered["net_pnl"] < 0]
    if tp_filter == "已止盈":
        filtered = filtered.loc[filtered["hit_take_profit"].astype(bool)]
    elif tp_filter == "未止盈":
        filtered = filtered.loc[~filtered["hit_take_profit"].astype(bool)]
    filtered = filtered.loc[
        filtered["rank_at_signal"].between(*rank_limit)
        & filtered["return_pct"].fillna(0).between(*return_range)
        & filtered["holding_minutes"].fillna(0).between(*holding_range)
    ]
    minutes = filtered["entry_datetime"].dt.hour * 60 + filtered["entry_datetime"].dt.minute
    if entry_period == "09:30–11:00":
        filtered = filtered.loc[(minutes >= 570) & (minutes < 660)]
    elif entry_period == "11:00–13:30":
        filtered = filtered.loc[(minutes >= 660) & (minutes < 810)]
    elif entry_period == "13:30以后":
        filtered = filtered.loc[minutes >= 810]

    st.caption(f"筛选后 {len(filtered):,} 笔 / 全部 {len(frame):,} 笔")
    pagination = st.columns(2)
    rows_per_page = pagination[0].selectbox("每页行数", [25, 50, 100, 250], index=1)
    pages = max(1, math.ceil(len(filtered) / rows_per_page))
    page = pagination[1].number_input("页码", 1, pages, 1)
    start = (int(page) - 1) * rows_per_page
    display_columns = [
        "trade_id", "date", "symbol", "rank_at_signal", "entered_top_time", "below_start_time", "below_minutes",
        "signal_time", "entry_time", "entry_price", "entry_quantity", "take_profit_time", "take_profit_price",
        "take_profit_quantity", "tail_exit_time", "tail_exit_price", "commission", "slippage_cost", "return_pct",
        "net_pnl", "holding_minutes", "close_reason", "fill_ratio", "warnings",
    ]
    st.dataframe(
        filtered.iloc[start : start + rows_per_page][[column for column in display_columns if column in filtered]],
        use_container_width=True,
        hide_index=True,
        column_config={"return_pct": st.column_config.NumberColumn(format="percent"), "net_pnl": st.column_config.NumberColumn(format="$%.2f")},
    )
    downloads = st.columns(2)
    clean = filtered.drop(columns=["date_value", "entry_datetime"], errors="ignore")
    downloads[0].download_button("下载筛选结果 CSV", csv_bytes(clean), "filtered_trades.csv", "text/csv", use_container_width=True)
    downloads[1].download_button("下载筛选结果 Excel", excel_bytes(clean), "filtered_trades.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    st.subheader("打开单笔复盘")
    selected_trade = st.selectbox("选择 trade_id", filtered["trade_id"].tolist() if not filtered.empty else frame["trade_id"].tolist())
    if st.button("设为当前复盘交易"):
        st.session_state.selected_trade_id = selected_trade
        st.success("已选择。请打开左侧“单笔复盘”。")
