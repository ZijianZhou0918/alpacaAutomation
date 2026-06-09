from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from .envfile import load_env_file


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    watch_codes_file: Path
    output_dir: Path
    state_file: Path
    buy_notional_usd: float
    max_daily_buys: int
    max_symbol_order_errors: int
    stop_loss_pct: float
    take_profit_half_pct: float
    close_liquidation_start: time
    close_liquidation_end: time
    regular_poll_seconds: int
    idle_poll_seconds: int
    market_timezone: str
    allow_fractional_shares: bool
    extended_hours_orders_enabled: bool
    extended_hours_limit_buffer_pct: float
    order_cancel_after_seconds: int
    order_status_poll_seconds: int
    realtime_price_source: str
    moomoo_host: str
    moomoo_port: int
    moomoo_security_firm: str
    moomoo_connect_timeout: float
    moomoo_opend_exe_path: str
    moomoo_opend_startup_timeout: float
    trade_notify_openclaw_enabled: bool
    openclaw_telegram_target: str
    openclaw_gateway_port: int
    watchlist_chart_lan_host: str
    watchlist_chart_lan_port: int


def build_settings() -> Settings:
    """
    这里集中放运行参数，方便你在 PyCharm 点箭头运行时直接生效。
    不使用 argparse，也不要求从命令行传参数。
    """
    env = load_env_file(BASE_DIR / ".env")
    output_dir = BASE_DIR / "outputs"
    return Settings(
        watch_codes_file=BASE_DIR / "watch_codes.txt",
        output_dir=output_dir,
        state_file=output_dir / "state.json",
        buy_notional_usd=3500.0,
        max_daily_buys=1,
        max_symbol_order_errors=3,
        stop_loss_pct=-0.10,
        take_profit_half_pct=0.10,
        close_liquidation_start=time(15, 55),
        close_liquidation_end=time(16, 0),
        regular_poll_seconds=10,
        idle_poll_seconds=300,
        market_timezone="America/New_York",
        allow_fractional_shares=True,
        extended_hours_orders_enabled=True,
        extended_hours_limit_buffer_pct=0.003,
        order_cancel_after_seconds=600,
        order_status_poll_seconds=5,
        realtime_price_source=env_value(env, "REALTIME_PRICE_SOURCE") or "moomoo",
        moomoo_host=env_value(env, "MOOMOO_HOST") or "127.0.0.1",
        moomoo_port=int(env_value(env, "MOOMOO_PORT") or "11111"),
        moomoo_security_firm=(env_value(env, "MOOMOO_SECURITY_FIRM") or "FUTUINC").upper(),
        moomoo_connect_timeout=float(env_value(env, "MOOMOO_CONNECT_TIMEOUT") or "3"),
        moomoo_opend_exe_path=env_value(env, "MOOMOO_OPEND_EXE_PATH") or r"%APPDATA%\moomoo_OpenD\moomoo_OpenD.exe",
        moomoo_opend_startup_timeout=float(env_value(env, "MOOMOO_OPEND_STARTUP_TIMEOUT") or "30"),
        trade_notify_openclaw_enabled=env_bool(env, "TRADE_NOTIFY_OPENCLAW_ENABLED", True),
        openclaw_telegram_target=env_value(env, "OPENCLAW_TELEGRAM_TARGET") or env_value(env, "WATCHLIST_TELEGRAM_TARGET"),
        openclaw_gateway_port=int(env_value(env, "OPENCLAW_GATEWAY_PORT") or "18789"),
        watchlist_chart_lan_host=env_value(env, "WATCHLIST_CHART_LAN_HOST"),
        watchlist_chart_lan_port=int(env_value(env, "WATCHLIST_CHART_LAN_PORT") or "8766"),
    )


def env_value(env: dict[str, str], key: str) -> str:
    """优先读取系统环境变量，其次读取项目 .env。"""
    return (os.getenv(key) or env.get(key, "")).strip()


def env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    """读取 bool 开关；空值使用 default。"""
    value = env_value(env, key).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}
