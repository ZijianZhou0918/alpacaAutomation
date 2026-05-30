from __future__ import annotations

import os
from dataclasses import dataclass

from .config import BASE_DIR
from .envfile import load_env_file


@dataclass(frozen=True)
class AlpacaConnection:
    """保存已识别好的 Alpaca client 和当前账户模式。"""

    client: object
    paper: bool
    account: object | None = None


def build_trading_connection() -> AlpacaConnection:
    """根据 .env 里的 key 自动连接可用的 paper/live endpoint。"""
    from alpaca.trading.client import TradingClient

    api_key, secret_key = load_alpaca_credentials()

    errors: list[str] = []
    for paper in (True, False):
        mode = "paper" if paper else "live"
        client = TradingClient(api_key, secret_key, paper=paper)
        try:
            account = client.get_account()
            return AlpacaConnection(client=client, paper=paper, account=account)
        except Exception as exc:
            errors.append(f"{mode}: {type(exc).__name__}: {exc}")

    raise RuntimeError("无法识别 Alpaca API key 是 paper 还是 live；" + " | ".join(errors))


def load_alpaca_credentials() -> tuple[str, str]:
    """读取 .env 或环境变量里的 Alpaca key，供连接和下单共用。"""
    env = load_env_file(BASE_DIR / ".env")
    api_key = os.getenv("APCA_API_KEY_ID") or env.get("APCA_API_KEY_ID", "")
    secret_key = os.getenv("APCA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise RuntimeError("缺少 Alpaca API key：请在 .env 填写 APCA_API_KEY_ID / APCA_API_SECRET_KEY。")
    return api_key, secret_key
