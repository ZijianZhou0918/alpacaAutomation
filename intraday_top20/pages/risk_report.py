from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.web.common import require_result


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("风险与结论")
    result = require_result(result)
    if result is None:
        return
    risk = result.risk
    columns = st.columns(5)
    columns[0].metric("前 1% 盈利贡献", _pct(risk.get("top_1pct_profit_contribution")))
    columns[1].metric("前 5% 盈利贡献", _pct(risk.get("top_5pct_profit_contribution")))
    columns[2].metric("前 10% 盈利贡献", _pct(risk.get("top_10pct_profit_contribution")))
    columns[3].metric("去掉最佳交易 PnL", _money(risk.get("pnl_without_best_trade")))
    columns[4].metric("Bootstrap 均值为正概率", _pct(risk.get("bootstrap_probability_mean_positive")))

    st.subheader("极端交易依赖与统计显著性")
    st.json(risk)
    if result.is_example:
        st.error("合成样例的人为价格路径决定了收益分布；任何极端交易贡献或显著性数字都没有市场统计含义。")
    elif risk.get("statistically_significant_95pct"):
        st.success("在当前日收益样本的简单 bootstrap 下，平均日收益 95% 区间下界大于 0；这不等于独立同分布假设成立，也不替代样本外验证。")
    else:
        st.warning("当前结果未通过简单的 95% bootstrap 正收益门槛，不能声称具有统计显著性。")

    st.subheader("分组表现")
    closed = result.trades.loc[result.trades.get("is_closed", pd.Series(False, index=result.trades.index)).astype(bool)].copy()
    if closed.empty:
        st.info("没有已平仓交易可用于分组。")
    else:
        closed["date"] = pd.to_datetime(closed["date"])
        closed["month"] = closed["date"].dt.to_period("M").astype(str)
        closed["year"] = closed["date"].dt.year
        closed["price_bucket"] = pd.cut(closed["entry_price"], [0, 2, 5, 10, 20, 50, float("inf")], right=False)
        tabs = st.tabs(["按月", "按年", "按入场价格"])
        with tabs[0]: st.dataframe(_group(closed, "month"), use_container_width=True, hide_index=True)
        with tabs[1]: st.dataframe(_group(closed, "year"), use_container_width=True, hide_index=True)
        with tabs[2]: st.dataframe(_group(closed, "price_bucket"), use_container_width=True, hide_index=True)
    st.info("市场环境分组需要同期基准指数和波动率数据；当前输入没有该数据，页面明确留空，不用价格路径臆造牛熊市标签。")

    robustness_path = Path(config.output.output_root) / result.run_id / "robustness.csv"
    st.subheader("成本压力")
    if robustness_path.exists():
        robust = pd.read_csv(robustness_path)
        st.dataframe(robust.loc[robust["group"] == "cost"], use_container_width=True, hide_index=True)
    else:
        st.info("尚未运行成本压力场景。")

    st.subheader("数据与实盘差异")
    st.markdown(
        """
- 全市场动态排名需要同一时点覆盖所有普通股；缺股、退市股遗漏或仅使用当前成分股会产生幸存者偏差。
- 五分钟 OHLCV 无法还原盘口队列、逐笔成交顺序和停牌公告；模型用价差、滑点、参与率与缺 K 线拒单保守近似。
- 拆股日必须让前收价和当日价格处于同一口径；否则涨幅榜会出现虚假极值。
- 止盈仅在 K 线最高价触及后按目标参考价减去卖出滑点；真实限价单可能排队、部分成交或完全未成交。
- 小盘暴涨股的可借/可交易状态、LULD 暂停、消息延迟和券商风控会让实盘成交显著差于回测。
"""
    )
    st.subheader("结论")
    if result.is_example:
        st.error("只能确认实现链路可运行，不能判断策略是否有效，也不建议据此进入模拟盘。下一步必须加载至少一年、含退市股与拆股事件的全市场分钟数据。")
    elif not result.validation.get("credible_for_strategy_conclusion", False):
        st.error("数据可靠性或成交完整性门禁未通过，暂不建议模拟盘验证。")
    elif result.metrics.get("total_return", 0) > 0 and risk.get("statistically_significant_95pct"):
        st.success("当前样本在成本和数据门禁下呈正向且通过简单显著性检查，可进入小规模模拟盘；仍不建议直接实盘。")
    else:
        st.warning("加入成本与成交限制后没有足够证据支持策略有效，暂不建议进入模拟盘。")


def _group(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    return frame.groupby(column, observed=True).agg(trades=("trade_id", "count"), pnl=("net_pnl", "sum"), average_return=("return_pct", "mean"), win_rate=("net_pnl", lambda values: (values > 0).mean())).reset_index()


def _pct(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.2%}"


def _money(value: object) -> str:
    return "N/A" if value is None else f"${float(value):,.2f}"
