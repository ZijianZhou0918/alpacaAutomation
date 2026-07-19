from __future__ import annotations

import html
import json

from intraday_top20.backtest.result import BacktestResult

from .charts import equity_and_drawdown, monthly_return_heatmap, return_histogram


def build_html_report(result: BacktestResult) -> str:
    title = "合成示例回测（非真实收益）" if result.is_example else "日内动态涨幅榜回测报告"
    metric_items = "".join(
        f"<div class='card'><small>{html.escape(name)}</small><strong>{_format_metric(name, value)}</strong></div>"
        for name, value in result.metrics.items()
        if name in {"final_equity", "total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "win_rate", "profit_factor", "total_trades"}
    )
    figures = [
        equity_and_drawdown(result.equity_curve).to_html(full_html=False, include_plotlyjs=True),
        monthly_return_heatmap(result.daily_returns).to_html(full_html=False, include_plotlyjs=False),
    ]
    if not result.trades.empty:
        figures.append(return_histogram(result.trades.dropna(subset=["return_pct"]), "return_pct", "单笔收益分布").to_html(full_html=False, include_plotlyjs=False))
    limitations = "；".join(
        [
            "这是合成示例数据，不能用于判断策略真实收益" if result.is_example else "结果基于用户提供的历史数据",
            f"数据可靠性门禁：{result.validation.get('data_reliability_gate_passed')}",
            f"未解决持仓：{result.validation.get('open_unresolved_positions')}",
        ]
    )
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>{title}</title>
<style>body{{font:15px system-ui;background:#07111f;color:#e5e7eb;margin:30px;max-width:1400px}}h1{{color:#f8fafc}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:#111827;padding:16px;border:1px solid #243244;border-radius:12px}}small{{display:block;color:#94a3b8}}strong{{font-size:22px}}.warning{{background:#3f1d14;padding:14px;border-radius:8px;margin:16px 0}}</style></head>
<body><h1>{title}</h1><div class='warning'>{html.escape(limitations)}</div><div class='grid'>{metric_items}</div>{''.join(figures)}
<h2>配置</h2><pre>{html.escape(json.dumps(result.config.to_dict(), ensure_ascii=False, indent=2))}</pre></body></html>"""


def _format_metric(name: str, value: object) -> str:
    if value is None:
        return "N/A"
    if name in {"total_return", "annualized_return", "max_drawdown", "win_rate"}:
        return f"{float(value):.2%}"
    if name == "final_equity":
        return f"${float(value):,.2f}"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
