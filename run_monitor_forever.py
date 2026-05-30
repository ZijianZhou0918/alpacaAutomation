from entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.service import run_forever


def run_forever_alpaca_auto() -> None:
    """PyCharm 可以直接点这个函数左侧箭头，按 .env 里的 key 自动走 paper/live。"""
    run_forever(build_settings())


if __name__ == "__main__":
    run_forever_alpaca_auto()
