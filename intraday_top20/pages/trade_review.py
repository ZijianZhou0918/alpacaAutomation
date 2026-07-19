from __future__ import annotations

import pandas as pd
import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.visualization.charts import trade_review_chart
from intraday_top20.web.common import plot_config, require_result


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("单笔交易复盘")
    result = require_result(result)
    if result is None or result.trades.empty:
        st.info("没有可复盘交易。")
        return
    ids = result.trades["trade_id"].tolist()
    preferred = st.session_state.get("selected_trade_id", ids[0])
    index = ids.index(preferred) if preferred in ids else 0
    trade_id = st.selectbox("交易", ids, index=index)
    st.session_state.selected_trade_id = trade_id
    trade = result.trades.loc[result.trades["trade_id"] == trade_id].iloc[0]
    bars = result.audit_bars.loc[
        (result.audit_bars["symbol"] == trade["symbol"]) & (result.audit_bars["date"].astype(str) == str(trade["date"]))
    ].copy()
    if bars.empty:
        st.error("该交易缺少审计 K 线；请以 save_audit_bars=true 重新运行。")
        return
    st.plotly_chart(trade_review_chart(bars, trade), use_container_width=True, config=plot_config())
    summary_columns = st.columns(5)
    summary_columns[0].metric("信号排名", int(trade["rank_at_signal"]))
    summary_columns[1].metric("连续跌破", f"{int(trade['below_minutes'])} 分钟")
    summary_columns[2].metric("买入", f"${float(trade['entry_price']):.4f}")
    summary_columns[3].metric("收益率", f"{float(trade['return_pct']):.2%}" if pd.notna(trade["return_pct"]) else "未平仓")
    summary_columns[4].metric("净盈亏", f"${float(trade['net_pnl']):,.2f}" if pd.notna(trade["net_pnl"]) else "未平仓")
    st.subheader("完整交易字段")
    st.dataframe(trade.rename("值").to_frame(), use_container_width=True)
    st.subheader("信号状态变化")
    audit = result.state_audit.loc[
        (result.state_audit["symbol"] == trade["symbol"]) & (result.state_audit["date"].astype(str) == str(trade["date"]))
    ]
    st.dataframe(audit, use_container_width=True, hide_index=True)
