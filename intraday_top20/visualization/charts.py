from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def equity_and_drawdown(equity: pd.DataFrame) -> go.Figure:
    frame = equity.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05)
    figure.add_trace(go.Scatter(x=frame["timestamp"], y=frame["equity"], name="资金", line={"color": "#22c55e"}), row=1, col=1)
    figure.add_trace(go.Scatter(x=frame["timestamp"], y=frame["drawdown"], name="回撤", fill="tozeroy", line={"color": "#ef4444"}), row=2, col=1)
    figure.update_yaxes(tickformat="$,.0f", row=1, col=1)
    figure.update_yaxes(tickformat=".1%", row=2, col=1)
    return _style(figure, 620)


def monthly_return_heatmap(daily: pd.DataFrame) -> go.Figure:
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    monthly = frame.set_index("date")["return_pct"].resample("ME").apply(lambda values: (1 + values).prod() - 1)
    table = monthly.rename("return").to_frame()
    table["year"] = table.index.year
    table["month"] = table.index.month
    pivot = table.pivot(index="year", columns="month", values="return").reindex(columns=range(1, 13))
    figure = px.imshow(
        pivot,
        text_auto=".1%",
        aspect="auto",
        color_continuous_scale=["#991b1b", "#111827", "#166534"],
        color_continuous_midpoint=0,
        labels={"color": "月收益"},
    )
    figure.update_xaxes(tickmode="array", tickvals=list(range(1, 13)), ticktext=[f"{month}月" for month in range(1, 13)])
    return _style(figure, 350)


def return_histogram(frame: pd.DataFrame, column: str, title: str) -> go.Figure:
    figure = px.histogram(frame, x=column, nbins=40, title=title, color_discrete_sequence=["#38bdf8"])
    figure.update_xaxes(tickformat=".2%")
    return _style(figure, 380)


def rolling_diagnostics(daily: pd.DataFrame) -> go.Figure:
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06)
    figure.add_trace(go.Scatter(x=frame["date"], y=frame["rolling_sharpe_20"], name="20日滚动夏普"), row=1, col=1)
    figure.add_trace(
        go.Scatter(x=frame["date"], y=frame["rolling_max_drawdown_20"], name="20日滚动最大回撤", fill="tozeroy"),
        row=2,
        col=1,
    )
    figure.update_yaxes(tickformat=".1%", row=2, col=1)
    return _style(figure, 520)


def trade_review_chart(bars: pd.DataFrame, trade: pd.Series) -> go.Figure:
    frame = bars.copy().sort_values("timestamp")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["bar_end"] = pd.to_datetime(frame["bar_end"])
    figure = go.Figure()
    figure.add_trace(
        go.Candlestick(
            x=frame["timestamp"], open=frame["open"], high=frame["high"], low=frame["low"], close=frame["close"], name="5分钟K线"
        )
    )
    figure.add_trace(go.Scatter(x=frame["timestamp"], y=frame["indicator"], name=str(frame["indicator_name"].iloc[0]), line={"color": "#f59e0b"}))
    markers = [
        ("entered_top_time", None, "进入Top N", "#a78bfa"),
        ("below_start_time", None, "跌破开始", "#fb7185"),
        ("signal_time", "signal_close", "重新站上信号", "#38bdf8"),
        ("entry_time", "entry_price", "实际买入", "#22c55e"),
        ("take_profit_time", "take_profit_price", "止盈", "#eab308"),
        ("tail_exit_time", "tail_exit_price", "尾盘/最终退出", "#ef4444"),
    ]
    for time_column, price_column, label, color in markers:
        raw_time = trade.get(time_column)
        if pd.isna(raw_time) or raw_time in (None, ""):
            continue
        event_time = pd.to_datetime(raw_time)
        raw_price = trade.get(price_column) if price_column else None
        if raw_price is None or pd.isna(raw_price):
            matching = frame.loc[frame["bar_end"] == event_time]
            if matching.empty:
                matching = frame.loc[frame["timestamp"] == event_time]
            if matching.empty:
                continue
            raw_price = matching["close"].iloc[0]
        figure.add_trace(
            go.Scatter(
                x=[event_time], y=[float(raw_price)], mode="markers+text", text=[label], textposition="top center",
                marker={"size": 11, "color": color, "symbol": "diamond"}, name=label,
            )
        )
    below_start, signal_time = pd.to_datetime(trade.get("below_start_time")), pd.to_datetime(trade.get("signal_time"))
    if not pd.isna(below_start) and not pd.isna(signal_time):
        figure.add_vrect(x0=below_start, x1=signal_time, fillcolor="#ef4444", opacity=0.08, line_width=0)
    figure.update_layout(xaxis_rangeslider_visible=False)
    return _style(figure, 680)


def robustness_heatmap(frame: pd.DataFrame, x: str, y: str, value: str, title: str) -> go.Figure:
    pivot = frame.pivot_table(index=y, columns=x, values=value, aggfunc="first")
    text_auto = ".1%" if value in {"total_return", "max_drawdown"} else ".2f"
    figure = px.imshow(
        pivot,
        text_auto=text_auto,
        aspect="auto",
        color_continuous_scale=["#991b1b", "#111827", "#166534"],
        color_continuous_midpoint=0,
        title=title,
    )
    return _style(figure, 420)


def _style(figure: go.Figure, height: int) -> go.Figure:
    figure.update_layout(
        height=height,
        margin={"l": 24, "r": 24, "t": 48, "b": 24},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.04, "x": 0},
        template="plotly_dark",
    )
    return figure
