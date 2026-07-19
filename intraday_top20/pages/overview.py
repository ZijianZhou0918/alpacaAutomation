from __future__ import annotations

import pandas as pd
import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.web.common import require_result, test_summary


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("日内动态涨幅榜 · VWAP Reclaim")
    st.caption("五分钟事件驱动回测｜动态 Top N｜下一根 K 线开盘成交｜无隔夜设计")
    st.markdown(
        """
策略按每根已完成五分钟 K 线重新计算 `当前价 / 前一交易日收盘价 - 1`，仅让当时仍在涨幅榜 Top N 的普通股触发买入。
标的必须先有效站上盘中累计 VWAP，随后至少连续 5 根完整 K 线收于 VWAP 下方，再从下向上重新站上；信号在收盘确认，下一根五分钟 K 线开盘成交。
默认实际成交价上涨 20% 时卖出初始仓位的一半，15:55 ET 处理剩余仓位。
"""
    )
    columns = st.columns(4)
    columns[0].metric("动态股票池", f"Top {config.strategy.rank_top_n}")
    columns[1].metric("跌破要求", f"> {config.strategy.continuous_below_minutes} 分钟 / {config.strategy.required_below_bars} 根")
    columns[2].metric("止盈", f"{config.strategy.take_profit_pct:.0%} 后减半")
    columns[3].metric("最晚开仓", f"{config.strategy.latest_entry_time} ET")

    st.subheader("执行口径")
    st.markdown(
        f"""
- 正常交易时段固定为 09:30–16:00 ET；盘前盘后数据在清洗阶段剔除。
- VWAP 使用当日累计成交额 / 累计成交量；缺少成交额时以 `(高+低+收)/3 × 成交量` 近似。
- 最低价格 `${config.execution.min_price:.2f}`，最低五分钟成交额 `${config.execution.min_five_minute_dollar_volume:,.0f}`，单笔最多参与五分钟成交量的 `{config.execution.max_volume_participation:.2%}`。
- 买卖成交价包含半边价差、基础滑点，以及低价股和高波动附加滑点；佣金另计。
- 15:55 缺失或停牌时不假定成交；仅在下一根真实可用正常时段 K 线开盘尝试退出，并标记强制隔夜。
"""
    )

    result = require_result(result)
    if result is None:
        return
    st.subheader("数据覆盖与可信度")
    quality = pd.DataFrame([result.data_quality]).T.reset_index()
    quality.columns = ["检查项", "值"]
    st.dataframe(quality, use_container_width=True, hide_index=True)
    st.subheader("验证信息")
    validation = {**result.validation, "automated_tests": test_summary()}
    st.json(validation, expanded=True)
    if not result.validation.get("credible_for_strategy_conclusion", False):
        st.warning("当前结果不能用于得出策略有效性结论。请以覆盖退市股、拆股和全市场证券主表的真实分钟数据重跑。")
