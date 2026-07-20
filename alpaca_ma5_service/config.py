from __future__ import annotations

import os
import math
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

from .envfile import load_env_file
from . import final_strategy
from .paths import BASE_DIR, INTRADAY_WATCH_CODES_PATH


BUY_NOTIONAL_USD = 1_500.0
MA5_DIP_STRATEGY_NAME = "ma5_dip"
GAP_CONFIRMED_PULLBACK_STRATEGY_NAME = final_strategy.STRATEGY_NAME
DEFAULT_STRATEGY_NAME = MA5_DIP_STRATEGY_NAME
DEFAULT_BUY_STOCK_COUNT = 2
DEFAULT_GAP_CONFIRMED_BUY_STOCK_COUNT = 3
MAX_BUY_STOCK_COUNT = final_strategy.MAX_DAILY_BUYS
ALLOWED_BUY_STOCK_COUNTS = set(range(1, MAX_BUY_STOCK_COUNT + 1))
_UNSET: Any = object()


@dataclass(frozen=True)
class Settings:
    watch_codes_file: Path
    output_dir: Path
    state_file: Path
    buy_notional_usd: float
    max_daily_buys: int
    max_symbol_order_errors: int
    stop_loss_pct: float
    stop_loss_limit_pct: float
    take_profit_half_pct: float
    take_profit_sell_fraction: float
    take_profit_remainder_stop_pct: float | None
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
    trade_notify_mode: str
    cloud_notify_webhook_url: str
    cloud_notify_webhook_secret: str
    openclaw_telegram_target: str
    openclaw_gateway_port: int
    watchlist_chart_lan_host: str
    watchlist_chart_lan_port: int
    strategy_name: str = DEFAULT_STRATEGY_NAME
    strategy_profile_name: str = ""
    watchlist_strategy_name: str = ""
    buy_strategy_name: str = ""
    sell_strategy_name: str = ""
    cancel_strategy_name: str = ""


def build_settings(
    *,
    buy_stock_count: int | None = None,
    buy_notional_usd: float | None = None,
    strategy_name: str | None = None,
    strategy_profile_name: str | None = None,
    watchlist_strategy_name: str | None = None,
    buy_strategy_name: str | None = None,
    sell_strategy_name: str | None = None,
    cancel_strategy_name: str | None = None,
    max_symbol_order_errors: int | None = None,
    stop_loss_pct: float | None = None,
    stop_loss_limit_pct: float | None = None,
    take_profit_half_pct: float | None = None,
    take_profit_sell_fraction: float | None = None,
    take_profit_remainder_stop_pct: Any = _UNSET,
    close_liquidation_start: time | None = None,
    close_liquidation_end: time | None = None,
    regular_poll_seconds: int | None = None,
    idle_poll_seconds: int | None = None,
    allow_fractional_shares: bool | None = None,
    extended_hours_orders_enabled: bool | None = None,
    extended_hours_limit_buffer_pct: float | None = None,
    order_cancel_after_seconds: int | None = None,
    order_status_poll_seconds: int | None = None,
    realtime_price_source: str | None = None,
    trade_notify_mode: str | None = None,
) -> Settings:
    """
    这里集中放运行参数，方便你在 PyCharm 点箭头运行时直接生效。
    不使用 argparse，也不要求从命令行传参数。
    """
    env = load_env_file(BASE_DIR / ".env")
    output_dir = BASE_DIR / "outputs"
    selection = build_strategy_selection(
        env,
        strategy_name=strategy_name,
        strategy_profile_name=strategy_profile_name,
        watchlist_strategy_name=watchlist_strategy_name,
        buy_strategy_name=buy_strategy_name,
        sell_strategy_name=sell_strategy_name,
        cancel_strategy_name=cancel_strategy_name,
    )
    defaults = strategy_runtime_defaults(selection.profile_name)
    max_daily_buys = defaults["max_daily_buys"] if buy_stock_count is None else validate_buy_stock_count(buy_stock_count)
    buy_notional = (
        float(defaults.get("buy_notional_usd", BUY_NOTIONAL_USD))
        if buy_notional_usd is None
        else validate_buy_notional_usd(buy_notional_usd)
    )
    realtime_source = (
        realtime_price_source
        if realtime_price_source is not None
        else env_value(env, "REALTIME_PRICE_SOURCE") or "moomoo"
    ).strip()
    notify_mode = (
        trade_notify_mode
        if trade_notify_mode is not None
        else env_value(env, "TRADE_NOTIFY_MODE") or "local"
    )
    return Settings(
        watch_codes_file=INTRADAY_WATCH_CODES_PATH,
        output_dir=output_dir,
        state_file=output_dir / "state.json",
        buy_notional_usd=buy_notional,
        max_daily_buys=max_daily_buys,
        max_symbol_order_errors=validate_positive_int(
            "max_symbol_order_errors",
            3 if max_symbol_order_errors is None else max_symbol_order_errors,
        ),
        stop_loss_pct=validate_finite_number(
            "stop_loss_pct", defaults["stop_loss_pct"] if stop_loss_pct is None else stop_loss_pct
        ),
        stop_loss_limit_pct=validate_finite_number(
            "stop_loss_limit_pct", defaults["stop_loss_limit_pct"] if stop_loss_limit_pct is None else stop_loss_limit_pct
        ),
        take_profit_half_pct=validate_finite_number(
            "take_profit_half_pct",
            defaults["take_profit_half_pct"] if take_profit_half_pct is None else take_profit_half_pct,
        ),
        take_profit_sell_fraction=validate_fraction(
            "take_profit_sell_fraction",
            defaults["take_profit_sell_fraction"]
            if take_profit_sell_fraction is None
            else take_profit_sell_fraction,
        ),
        take_profit_remainder_stop_pct=resolve_optional_number(
            "take_profit_remainder_stop_pct",
            defaults["take_profit_remainder_stop_pct"],
            take_profit_remainder_stop_pct,
        ),
        strategy_name=selection.profile_name,
        strategy_profile_name=selection.profile_name,
        watchlist_strategy_name=selection.watchlist_strategy_name,
        buy_strategy_name=selection.buy_strategy_name,
        sell_strategy_name=selection.sell_strategy_name,
        cancel_strategy_name=selection.cancel_strategy_name,
        close_liquidation_start=close_liquidation_start or time(15, 55),
        close_liquidation_end=close_liquidation_end or time(16, 0),
        regular_poll_seconds=validate_positive_int(
            "regular_poll_seconds",
            regular_poll_seconds if regular_poll_seconds is not None else int(env_value(env, "REGULAR_POLL_SECONDS") or "10"),
        ),
        idle_poll_seconds=validate_positive_int(
            "idle_poll_seconds",
            idle_poll_seconds if idle_poll_seconds is not None else int(env_value(env, "IDLE_POLL_SECONDS") or "1200"),
        ),
        market_timezone="America/New_York",
        allow_fractional_shares=validate_bool("allow_fractional_shares", False if allow_fractional_shares is None else allow_fractional_shares),
        extended_hours_orders_enabled=validate_bool(
            "extended_hours_orders_enabled",
            True if extended_hours_orders_enabled is None else extended_hours_orders_enabled,
        ),
        extended_hours_limit_buffer_pct=validate_finite_number(
            "extended_hours_limit_buffer_pct",
            0.003 if extended_hours_limit_buffer_pct is None else extended_hours_limit_buffer_pct,
        ),
        order_cancel_after_seconds=validate_positive_int(
            "order_cancel_after_seconds",
            600 if order_cancel_after_seconds is None else order_cancel_after_seconds,
        ),
        order_status_poll_seconds=validate_positive_int(
            "order_status_poll_seconds",
            5 if order_status_poll_seconds is None else order_status_poll_seconds,
        ),
        realtime_price_source=realtime_source or "moomoo",
        moomoo_host=env_value(env, "MOOMOO_HOST") or "127.0.0.1",
        moomoo_port=int(env_value(env, "MOOMOO_PORT") or "11111"),
        moomoo_security_firm=(env_value(env, "MOOMOO_SECURITY_FIRM") or "FUTUINC").upper(),
        moomoo_connect_timeout=float(env_value(env, "MOOMOO_CONNECT_TIMEOUT") or "3"),
        moomoo_opend_exe_path=env_value(env, "MOOMOO_OPEND_EXE_PATH") or r"%APPDATA%\moomoo_OpenD\moomoo_OpenD.exe",
        moomoo_opend_startup_timeout=float(env_value(env, "MOOMOO_OPEND_STARTUP_TIMEOUT") or "30"),
        trade_notify_openclaw_enabled=env_bool(env, "TRADE_NOTIFY_OPENCLAW_ENABLED", True),
        trade_notify_mode=validate_trade_notify_mode(notify_mode),
        cloud_notify_webhook_url=env_value(env, "CLOUD_NOTIFY_WEBHOOK_URL") or env_value(env, "WEBHOOK_URL"),
        cloud_notify_webhook_secret=env_value(env, "CLOUD_NOTIFY_WEBHOOK_SECRET") or env_value(env, "WEBHOOK_SECRET"),
        openclaw_telegram_target=env_value(env, "OPENCLAW_TELEGRAM_TARGET") or env_value(env, "WATCHLIST_TELEGRAM_TARGET"),
        openclaw_gateway_port=int(env_value(env, "OPENCLAW_GATEWAY_PORT") or "18789"),
        watchlist_chart_lan_host=env_value(env, "WATCHLIST_CHART_LAN_HOST"),
        watchlist_chart_lan_port=int(env_value(env, "WATCHLIST_CHART_LAN_PORT") or "8766"),
    )


def validate_trade_notify_mode(mode: str) -> str:
    mode = (mode or "local").strip().lower()
    if mode not in {"local", "cloud"}:
        raise ValueError("TRADE_NOTIFY_MODE must be 'local' or 'cloud'")
    return mode


def validate_buy_stock_count(buy_stock_count: int) -> int:
    """Monitor entry allows 1 through the highest selectable strategy daily buy cap."""
    if isinstance(buy_stock_count, bool) or not isinstance(buy_stock_count, int):
        raise ValueError(f"buy_stock_count must be an integer from 1 to {MAX_BUY_STOCK_COUNT}")
    if buy_stock_count not in ALLOWED_BUY_STOCK_COUNTS:
        raise ValueError(f"buy_stock_count must be between 1 and {MAX_BUY_STOCK_COUNT}")
    return buy_stock_count


def validate_buy_notional_usd(buy_notional_usd: float) -> float:
    """Monitor entry allows any positive finite per-stock notional."""
    notional = validate_finite_number("buy_notional_usd", buy_notional_usd)
    if not math.isfinite(notional) or notional <= 0:
        raise ValueError("buy_notional_usd must be a positive finite number")
    return notional


def validate_finite_number(name: str, value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def validate_fraction(name: str, value: float | int) -> float:
    fraction = validate_finite_number(name, value)
    if fraction < 0 or fraction > 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return fraction


def resolve_optional_number(name: str, default: float | int | None, value: Any) -> float | None:
    if value is _UNSET:
        return None if default is None else validate_finite_number(name, default)
    if value is None:
        return None
    return validate_finite_number(name, value)


def validate_positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def validate_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def build_strategy_selection(
    env: dict[str, str],
    *,
    strategy_name: str | None,
    strategy_profile_name: str | None,
    watchlist_strategy_name: str | None,
    buy_strategy_name: str | None,
    sell_strategy_name: str | None,
    cancel_strategy_name: str | None,
):
    """Resolve legacy/profile/component settings and reject invalid mixes before I/O."""
    from .strategy_framework import resolve_strategy_selection

    legacy_name = (strategy_name or "").strip()
    explicit_profile_name = (strategy_profile_name or "").strip()
    if legacy_name and explicit_profile_name and legacy_name != explicit_profile_name:
        raise ValueError(
            "strategy_name and strategy_profile_name must match when both are provided"
        )
    profile_name = (
        explicit_profile_name
        or legacy_name
        or env_value(env, "STRATEGY_PROFILE")
        or DEFAULT_STRATEGY_NAME
    )
    return resolve_strategy_selection(
        profile_name,
        watchlist_strategy_name=_strategy_override(
            watchlist_strategy_name, env_value(env, "WATCHLIST_STRATEGY")
        ),
        buy_strategy_name=_strategy_override(
            buy_strategy_name, env_value(env, "BUY_STRATEGY")
        ),
        sell_strategy_name=_strategy_override(
            sell_strategy_name, env_value(env, "SELL_STRATEGY")
        ),
        cancel_strategy_name=_strategy_override(
            cancel_strategy_name, env_value(env, "CANCEL_STRATEGY")
        ),
    )


def _strategy_override(explicit_value: str | None, env_value_text: str) -> str | None:
    if explicit_value is not None:
        return explicit_value.strip()
    return env_value_text or None


def validate_strategy_name(strategy_name: str) -> str:
    """Backward-compatible profile validation for older callers."""
    from .strategy_framework import get_strategy_registry

    name = (strategy_name or DEFAULT_STRATEGY_NAME).strip()
    return get_strategy_registry().profile(name).name


def strategy_runtime_defaults(strategy_name: str) -> dict[str, float | int | None]:
    from .strategy_framework import get_strategy_registry

    profile = get_strategy_registry().profile(validate_strategy_name(strategy_name))
    return dict(profile.runtime_defaults)


def env_value(env: dict[str, str], key: str) -> str:
    """优先读取系统环境变量，其次读取项目 .env。"""
    return (os.getenv(key) or env.get(key, "")).strip()


def env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    """读取 bool 开关；空值使用 default。"""
    value = env_value(env, key).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}
