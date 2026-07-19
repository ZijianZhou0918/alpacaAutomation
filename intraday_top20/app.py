from __future__ import annotations

import streamlit as st

from intraday_top20.pages import (
    configuration,
    daily_analysis,
    overview,
    performance,
    risk_report,
    robustness,
    trade_review,
    trades,
)
from intraday_top20.web.common import bootstrap, render_data_banner

st.set_page_config(page_title="Intraday Top Gainers Lab", page_icon="📈", layout="wide")
st.markdown(
    """
<style>
.stApp {background: radial-gradient(circle at 12% 0%, #10203a 0%, #07111f 34%, #050b14 100%);color:#e7eef8;}
[data-testid="stMetric"] {background:#0d1929;border:1px solid #203149;padding:14px;border-radius:12px;}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p {color:#9fb1c8!important;}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] div {color:#f5f9ff!important;}
[data-testid="stSidebar"] {background:#07101d;border-right:1px solid #1c2a3d;}
[data-testid="stSidebarNav"] {display:none;}
.stApp h1,.stApp h2,.stApp h3 {color:#edf4ff;letter-spacing:-0.02em;}
.stApp [data-testid="stMarkdownContainer"] p,.stApp [data-testid="stMarkdownContainer"] li {color:#c6d3e3;}
.block-container {padding-top:1.4rem;max-width:1500px;}
</style>
""",
    unsafe_allow_html=True,
)

config, result = bootstrap()
st.sidebar.caption("INTRADAY RESEARCH / ET")
page = st.sidebar.radio(
    "导航",
    ["策略概览", "参数与运行", "核心绩效", "交易明细", "单笔复盘", "每日分析", "稳健性测试", "风险与结论"],
)
st.sidebar.divider()
st.sidebar.write(f"数据：{config.data.source_label}")
st.sidebar.write(f"区间：{config.data.start_date} → {config.data.end_date}")
st.sidebar.write(f"Top {config.strategy.rank_top_n} / {config.strategy.indicator.upper()}")
render_data_banner(result)

routes = {
    "策略概览": overview.render,
    "参数与运行": configuration.render,
    "核心绩效": performance.render,
    "交易明细": trades.render,
    "单笔复盘": trade_review.render,
    "每日分析": daily_analysis.render,
    "稳健性测试": robustness.render,
    "风险与结论": risk_report.render,
}
routes[page](config, result)
