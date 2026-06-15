try:
    from ._bootstrap import ensure_local_venv
except ImportError:
    from _bootstrap import ensure_local_venv

ensure_local_venv()

import sys

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.broker import AlpacaStockBroker


def check_alpaca_connection() -> None:
    """PyCharm 可以直接点这个函数左侧箭头，检查 Alpaca paper/live API key。"""
    print("=== Alpaca connection check ===", flush=True)
    print(f"Python: {sys.executable}", flush=True)

    settings = build_settings()
    print("Loading API key from .env and detecting paper/live mode ...", flush=True)

    broker = AlpacaStockBroker(settings)
    print(f"Mode: Alpaca {'PAPER' if broker.paper else 'LIVE'}", flush=True)

    account = broker.account or broker.client.get_account()
    print("Connection OK.", flush=True)
    print(f"Account number: {account.account_number}", flush=True)
    print(f"Status: {account.status}", flush=True)
    print(f"Trading blocked: {account.trading_blocked}", flush=True)
    print(f"Account blocked: {account.account_blocked}", flush=True)
    print(f"Currency: {account.currency}", flush=True)
    print(f"Cash: {account.cash}", flush=True)
    print(f"Buying power: {account.buying_power}", flush=True)
    print(f"Portfolio value: {account.portfolio_value}", flush=True)
    print("=== done ===", flush=True)


if __name__ == "__main__":
    check_alpaca_connection()
