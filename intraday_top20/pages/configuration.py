from __future__ import annotations

from datetime import date

import streamlit as st

from intraday_top20.backtest.config import BacktestConfig
from intraday_top20.backtest.result import BacktestResult
from intraday_top20.data.cache import ResultStore
from intraday_top20.web.common import run_or_load


def render(config: BacktestConfig, result: BacktestResult | None) -> None:
    st.title("参数配置与运行")
    st.caption("参数变化会生成新的确定性缓存键；相同数据与参数默认直接加载已完成结果。")
    with st.form("backtest_parameters"):
        st.subheader("数据")
        data_columns = st.columns(3)
        data_dir = data_columns[0].text_input("行情目录", config.data.data_dir)
        source_label = data_columns[1].text_input("数据来源标签", config.data.source_label)
        date_range = data_columns[2].date_input(
            "回测区间",
            value=(date.fromisoformat(config.data.start_date), date.fromisoformat(config.data.end_date)),
        )
        reference_columns = st.columns(3)
        file_glob = reference_columns[0].text_input("行情文件匹配", config.data.file_glob)
        security_master_path = reference_columns[1].text_input("点时证券主表", config.data.security_master_path)
        splits_path = reference_columns[2].text_input("拆股事件表（复权数据可留空）", config.data.splits_path)
        data_flags = st.columns(3)
        example_mode = data_flags[0].checkbox("合成示例模式", config.data.example_mode)
        source_adjusted = data_flags[1].checkbox("数据已复权", config.data.source_adjusted)
        contains_delisted = data_flags[2].checkbox("包含退市股票", config.data.contains_delisted)

        st.subheader("信号")
        signal_columns = st.columns(4)
        rank_top_n = signal_columns[0].number_input("涨幅排名数量", 1, 100, config.strategy.rank_top_n)
        below_minutes = signal_columns[1].number_input("连续跌破分钟数", 0, 120, config.strategy.continuous_below_minutes, step=5)
        take_profit_pct = signal_columns[2].number_input("止盈比例", 0.01, 2.0, config.strategy.take_profit_pct, step=0.05, format="%.2f")
        latest_entry_time = signal_columns[3].selectbox("最晚开仓 ET", ["14:30", "15:00", "15:30"], index=["14:30", "15:00", "15:30"].index(config.strategy.latest_entry_time) if config.strategy.latest_entry_time in ["14:30", "15:00", "15:30"] else 1)
        indicator_columns = st.columns(4)
        indicator = indicator_columns[0].selectbox("分时均线", ["vwap", "sma"], index=0 if config.strategy.indicator == "vwap" else 1)
        moving_average_window = indicator_columns[1].number_input("SMA 周期（根）", 2, 50, config.strategy.moving_average_window)
        volume_filter = indicator_columns[2].checkbox("重新站上需成交量放大", config.strategy.require_volume_expansion)
        volume_multiplier = indicator_columns[3].number_input("成交量放大倍数", 1.0, 10.0, config.strategy.volume_expansion_multiplier, step=0.1)
        repeat_columns = st.columns(2)
        allow_repeat = repeat_columns[0].checkbox("允许同一股票当日重复交易", config.strategy.allow_repeat_symbol)
        max_symbol_trades = repeat_columns[1].number_input("每股每日最多买入次数", 1, 20, config.strategy.max_trades_per_symbol_per_day)

        st.subheader("资金与容量")
        portfolio_columns = st.columns(4)
        initial_capital = portfolio_columns[0].number_input("初始资金（美元）", 1_000.0, 1_000_000_000.0, config.portfolio.initial_capital, step=10_000.0)
        max_position_pct = portfolio_columns[1].number_input("单笔最大仓位", 0.001, 1.0, config.portfolio.max_position_pct, step=0.01, format="%.3f")
        max_positions = portfolio_columns[2].number_input("最大同时持仓", 1, 100, config.portfolio.max_concurrent_positions)
        max_daily = portfolio_columns[3].number_input("每日最大开仓次数", 1, 500, config.portfolio.max_daily_entries)

        st.subheader("流动性与成本")
        execution_columns = st.columns(4)
        min_price = execution_columns[0].number_input("最低股价", 0.01, 1_000.0, config.execution.min_price, step=0.25)
        min_dollar_volume = execution_columns[1].number_input("最低五分钟成交额", 0.0, 1_000_000_000.0, config.execution.min_five_minute_dollar_volume, step=50_000.0)
        participation = execution_columns[2].number_input("最大成交量参与率", 0.0001, 1.0, config.execution.max_volume_participation, step=0.005, format="%.4f")
        commission = execution_columns[3].number_input("每股佣金", 0.0, 1.0, config.execution.commission_per_share, step=0.001, format="%.4f")
        cost_columns = st.columns(3)
        minimum_commission = cost_columns[0].number_input("最低单笔佣金", 0.0, 100.0, config.execution.minimum_commission, step=0.25)
        base_slippage = cost_columns[1].number_input("基础滑点（bps）", 0.0, 1_000.0, config.execution.base_slippage_bps, step=1.0)
        spread = cost_columns[2].number_input("假设买卖价差（bps）", 0.0, 2_000.0, config.execution.assumed_spread_bps, step=1.0)

        submitted = st.form_submit_button("运行回测", type="primary", use_container_width=True)

    if submitted:
        try:
            if not isinstance(date_range, tuple) or len(date_range) != 2:
                raise ValueError("请选择完整的回测起止日期")
            updated = config.with_updates(
                data={
                    "data_dir": data_dir,
                    "file_glob": file_glob,
                    "security_master_path": security_master_path,
                    "splits_path": splits_path,
                    "source_label": source_label,
                    "start_date": date_range[0].isoformat(),
                    "end_date": date_range[1].isoformat(),
                    "example_mode": example_mode,
                    "source_adjusted": source_adjusted,
                    "contains_delisted": contains_delisted,
                },
                strategy={
                    "rank_top_n": int(rank_top_n),
                    "continuous_below_minutes": int(below_minutes),
                    "take_profit_pct": float(take_profit_pct),
                    "latest_entry_time": latest_entry_time,
                    "indicator": indicator,
                    "moving_average_window": int(moving_average_window),
                    "require_volume_expansion": volume_filter,
                    "volume_expansion_multiplier": float(volume_multiplier),
                    "allow_repeat_symbol": allow_repeat,
                    "max_trades_per_symbol_per_day": int(max_symbol_trades),
                },
                portfolio={
                    "initial_capital": float(initial_capital),
                    "max_position_pct": float(max_position_pct),
                    "max_concurrent_positions": int(max_positions),
                    "max_daily_entries": int(max_daily),
                },
                execution={
                    "min_price": float(min_price),
                    "min_five_minute_dollar_volume": float(min_dollar_volume),
                    "max_volume_participation": float(participation),
                    "commission_per_share": float(commission),
                    "minimum_commission": float(minimum_commission),
                    "base_slippage_bps": float(base_slippage),
                    "assumed_spread_bps": float(spread),
                },
            )
            with st.status("正在运行事件驱动回测", expanded=True) as status:
                run_or_load(updated)
                status.update(label="回测完成", state="complete")
            st.rerun()
        except Exception as exc:
            st.error(f"回测失败：{type(exc).__name__}: {exc}")
            st.exception(exc)

    st.subheader("已缓存结果")
    runs = ResultStore(config.output.output_root).list_runs()
    if runs.empty:
        st.info("暂无缓存结果。")
    else:
        st.dataframe(runs, use_container_width=True, hide_index=True)
        selected = st.selectbox("加载运行", runs["run_id"].tolist())
        if st.button("加载所选结果"):
            loaded = ResultStore(config.output.output_root).load(selected)
            st.session_state.intraday_result = loaded
            st.session_state.intraday_config = loaded.config
            st.rerun()
