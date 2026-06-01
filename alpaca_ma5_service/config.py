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
    stop_loss_pct: float
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
    trade_notify_openclaw_enabled: bool
    openclaw_telegram_target: str
    openclaw_gateway_port: int


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
        buy_notional_usd=300.0,
        max_daily_buys=1,
        stop_loss_pct=-0.15,
        close_liquidation_start=time(15, 55),
        close_liquidation_end=time(16, 0),
        regular_poll_seconds=60,
        idle_poll_seconds=300,
        market_timezone="America/New_York",
        allow_fractional_shares=True,
        extended_hours_orders_enabled=True,
        extended_hours_limit_buffer_pct=0.003,
        order_cancel_after_seconds=60,
        order_status_poll_seconds=5,
        trade_notify_openclaw_enabled=env_bool(env, "TRADE_NOTIFY_OPENCLAW_ENABLED", True),
        openclaw_telegram_target=env_value(env, "OPENCLAW_TELEGRAM_TARGET") or env_value(env, "WATCHLIST_TELEGRAM_TARGET"),
        openclaw_gateway_port=int(env_value(env, "OPENCLAW_GATEWAY_PORT") or "18789"),
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
