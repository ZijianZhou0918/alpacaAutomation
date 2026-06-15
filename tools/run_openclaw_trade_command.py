from __future__ import annotations

import os
import sys

try:
    from ._bootstrap import ensure_local_venv
except ImportError:
    from _bootstrap import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.openclaw_trade_control import execute_trade_command, render_trade_command_response


def run_openclaw_trade_command(message: str | None = None) -> None:
    """给 OpenClaw agent 调用：读取一句交易指令并执行买入、卖出或撤单。"""
    message = message or os.getenv("OPENCLAW_TRADE_MESSAGE", "").strip() or sys.stdin.read().strip()
    if not message:
        print("没有收到交易指令。请通过 OPENCLAW_TRADE_MESSAGE 或 stdin 传入一句话。")
        return

    try:
        response = execute_trade_command(message)
    except ValueError as exc:
        print(f"OpenClaw 交易指令无法执行：{exc}")
        return
    print(render_trade_command_response(response))


if __name__ == "__main__":
    run_openclaw_trade_command()
