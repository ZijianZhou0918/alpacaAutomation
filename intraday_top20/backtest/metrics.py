from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def calculate_metrics(
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
    daily_returns: pd.DataFrame,
    initial_capital: float,
) -> dict[str, Any]:
    if equity_curve.empty:
        return _empty_metrics(initial_capital)
    equity = equity_curve.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    final_equity = float(equity["equity"].iloc[-1])
    total_return = final_equity / initial_capital - 1.0
    start = pd.Timestamp(equity["timestamp"].iloc[0])
    end = pd.Timestamp(equity["timestamp"].iloc[-1])
    elapsed_days = max((end - start).total_seconds() / 86_400, 1.0)
    annualized_return = (final_equity / initial_capital) ** (365.25 / elapsed_days) - 1.0
    running_peak = equity["equity"].cummax()
    drawdown = equity["equity"] / running_peak - 1.0
    daily = daily_returns.get("return_pct", pd.Series(dtype=float)).dropna().astype(float)
    sharpe = float(np.sqrt(252) * daily.mean() / daily.std(ddof=1)) if len(daily) > 1 and daily.std(ddof=1) > 0 else 0.0

    closed = trades.loc[trades.get("is_closed", pd.Series(False, index=trades.index)).fillna(False)].copy()
    pnl = closed.get("net_pnl", pd.Series(dtype=float)).dropna().astype(float)
    returns = closed.get("return_pct", pd.Series(dtype=float)).dropna().astype(float)
    winners, losers = pnl[pnl > 0], pnl[pnl < 0]
    avg_win = float(winners.mean()) if not winners.empty else 0.0
    avg_loss = abs(float(losers.mean())) if not losers.empty else 0.0
    profit_factor = float(winners.sum() / abs(losers.sum())) if not losers.empty else (float("inf") if not winners.empty else 0.0)
    streaks = _streaks(pnl)
    return {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": float(drawdown.min()),
        "sharpe_ratio": sharpe,
        "win_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "payoff_ratio": avg_win / avg_loss if avg_loss else (float("inf") if avg_win else 0.0),
        "profit_factor": profit_factor,
        "total_trades": int(len(closed)),
        "open_unresolved_trades": int((~trades.get("is_closed", pd.Series(dtype=bool)).fillna(False)).sum()) if not trades.empty else 0,
        "average_trade_return": float(returns.mean()) if not returns.empty else 0.0,
        "average_trade_pnl": float(pnl.mean()) if not pnl.empty else 0.0,
        "average_holding_minutes": float(closed.get("holding_minutes", pd.Series(dtype=float)).dropna().mean()) if not closed.empty else 0.0,
        "take_profit_trade_ratio": float(closed.get("hit_take_profit", pd.Series(False, index=closed.index)).mean()) if not closed.empty else 0.0,
        "tail_exit_trade_ratio": float(closed.get("tail_exit_time", pd.Series("", index=closed.index)).astype(bool).mean()) if not closed.empty else 0.0,
        "max_consecutive_wins": streaks[0],
        "max_consecutive_losses": streaks[1],
        "daily_return_mean": float(daily.mean()) if len(daily) else 0.0,
        "daily_return_std": float(daily.std(ddof=1)) if len(daily) > 1 else 0.0,
        "daily_return_min": float(daily.min()) if len(daily) else 0.0,
        "daily_return_max": float(daily.max()) if len(daily) else 0.0,
    }


def add_equity_diagnostics(equity_curve: pd.DataFrame, daily_returns: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    equity = equity_curve.sort_values("timestamp").copy()
    if not equity.empty:
        equity["running_peak"] = equity["equity"].cummax()
        equity["drawdown"] = equity["equity"] / equity["running_peak"] - 1.0
    daily = daily_returns.sort_values("date").copy()
    if not daily.empty:
        daily["rolling_sharpe_20"] = (
            daily["return_pct"].rolling(20, min_periods=5).mean()
            / daily["return_pct"].rolling(20, min_periods=5).std(ddof=1)
            * np.sqrt(252)
        )
        wealth = (1.0 + daily["return_pct"]).cumprod()
        daily["rolling_drawdown"] = wealth / wealth.cummax() - 1.0
        daily["rolling_max_drawdown_20"] = wealth.rolling(20, min_periods=5).apply(
            lambda values: float((values / np.maximum.accumulate(values) - 1.0).min()),
            raw=True,
        )
    return equity, daily


def risk_diagnostics(trades: pd.DataFrame, daily_returns: pd.DataFrame, seed: int) -> dict[str, Any]:
    closed = trades.loc[trades.get("is_closed", pd.Series(False, index=trades.index)).fillna(False)].copy()
    pnl = closed.get("net_pnl", pd.Series(dtype=float)).dropna().astype(float).sort_values(ascending=False)
    positive_total = float(pnl[pnl > 0].sum())
    contribution: dict[str, float] = {}
    for fraction in (0.01, 0.05, 0.10):
        count = max(1, int(np.ceil(len(pnl) * fraction))) if len(pnl) else 0
        contribution[f"top_{int(fraction * 100)}pct_profit_contribution"] = (
            float(pnl.head(count).clip(lower=0).sum() / positive_total) if positive_total > 0 else 0.0
        )
    rng = np.random.default_rng(seed)
    daily = daily_returns.get("return_pct", pd.Series(dtype=float)).dropna().to_numpy(dtype=float)
    boot_means = (
        rng.choice(daily, size=(2_000, len(daily)), replace=True).mean(axis=1) if len(daily) >= 5 else np.array([], dtype=float)
    )
    return {
        **contribution,
        "pnl_without_best_trade": float(pnl.iloc[1:].sum()) if len(pnl) > 1 else 0.0,
        "pnl_without_best_5_trades": float(pnl.iloc[5:].sum()) if len(pnl) > 5 else 0.0,
        "bootstrap_mean_daily_return_ci_low": float(np.quantile(boot_means, 0.025)) if len(boot_means) else None,
        "bootstrap_mean_daily_return_ci_high": float(np.quantile(boot_means, 0.975)) if len(boot_means) else None,
        "bootstrap_probability_mean_positive": float((boot_means > 0).mean()) if len(boot_means) else None,
        "statistically_significant_95pct": bool(len(boot_means) and np.quantile(boot_means, 0.025) > 0),
    }


def _streaks(pnl: pd.Series) -> tuple[int, int]:
    wins = losses = max_wins = max_losses = 0
    for value in pnl:
        if value > 0:
            wins += 1
            losses = 0
        elif value < 0:
            losses += 1
            wins = 0
        else:
            wins = losses = 0
        max_wins, max_losses = max(max_wins, wins), max(max_losses, losses)
    return max_wins, max_losses


def _empty_metrics(initial_capital: float) -> dict[str, Any]:
    keys = [
        "total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "win_rate", "payoff_ratio",
        "profit_factor", "average_trade_return", "average_trade_pnl", "average_holding_minutes",
        "take_profit_trade_ratio", "tail_exit_trade_ratio", "daily_return_mean", "daily_return_std",
        "daily_return_min", "daily_return_max",
    ]
    result: dict[str, Any] = {key: 0.0 for key in keys}
    result.update({"initial_capital": initial_capital, "final_equity": initial_capital, "total_trades": 0, "open_unresolved_trades": 0, "max_consecutive_wins": 0, "max_consecutive_losses": 0})
    return result
