"""Intraday dynamic top-gainers backtest and Streamlit dashboard."""

from .backtest.config import BacktestConfig, load_config

__all__ = ["BacktestConfig", "load_config"]
