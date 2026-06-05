from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from .config import Settings


_OPENCLAW_GATEWAY_READY = False


def send_openclaw_telegram_message(settings: Settings, message: str) -> None:
    """通过本机 OpenClaw gateway 发送一条 Telegram 消息。"""
    target = settings.openclaw_telegram_target.strip()
    if not target:
        raise ValueError("OPENCLAW_TELEGRAM_TARGET is empty")

    openclaw = openclaw_executable()
    ensure_openclaw_gateway_running(settings, openclaw)
    result = subprocess.run(
        [
            openclaw,
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            target,
            "--message",
            message,
            "--json",
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"openclaw Telegram send failed: {detail}")


def safe_send_openclaw_messages(settings: Settings, messages: list[str], *, context: str) -> None:
    """逐条发送通知；失败只打印，不能影响交易主流程。"""
    if not settings.trade_notify_openclaw_enabled:
        return
    try:
        for message in messages:
            send_openclaw_telegram_message(settings, message)
        print(f"OpenClaw 通知已发送: {context}", flush=True)
    except Exception as exc:
        print(f"OpenClaw 通知失败，不影响主流程 {context}：{type(exc).__name__}: {exc}")


def openclaw_executable() -> str:
    """定位 openclaw 命令行程序。"""
    openclaw = shutil.which("openclaw.cmd") or shutil.which("openclaw") or shutil.which("openclaw-cn.cmd") or shutil.which("openclaw-cn")
    if openclaw is None:
        raise FileNotFoundError("openclaw command not found in PATH")
    return openclaw


def ensure_openclaw_gateway_running(settings: Settings, openclaw: str) -> None:
    """确保 OpenClaw gateway 可用；沿用 StockAPI 的 probe/start 顺序。"""
    global _OPENCLAW_GATEWAY_READY
    if _OPENCLAW_GATEWAY_READY:
        return
    if openclaw_gateway_is_running(openclaw):
        _OPENCLAW_GATEWAY_READY = True
        return

    start_openclaw_gateway_service(openclaw)
    if wait_for_openclaw_gateway(openclaw, timeout_seconds=12.0):
        _OPENCLAW_GATEWAY_READY = True
        return

    start_openclaw_gateway_process(settings, openclaw)
    if wait_for_openclaw_gateway(openclaw, timeout_seconds=20.0):
        _OPENCLAW_GATEWAY_READY = True
        return
    raise RuntimeError("OpenClaw gateway did not become ready after start")


def openclaw_gateway_is_running(openclaw: str) -> bool:
    """用 gateway probe 判断本机网关是否 ready。"""
    result = subprocess.run(
        [openclaw, "gateway", "probe", "--json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False
    return bool(payload.get("ok"))


def wait_for_openclaw_gateway(openclaw: str, *, timeout_seconds: float) -> bool:
    """等待 gateway 进入 ready 状态。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if openclaw_gateway_is_running(openclaw):
            return True
        time.sleep(1.0)
    return False


def start_openclaw_gateway_service(openclaw: str) -> None:
    """优先使用 OpenClaw 自带的 gateway start。"""
    result = subprocess.run(
        [openclaw, "gateway", "start", "--json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print(f"OpenClaw gateway service start failed; will try background run. detail={detail}")


def start_openclaw_gateway_process(settings: Settings, openclaw: str) -> None:
    """gateway start 失败时，后台拉起 gateway run 兜底。"""
    log_dir = settings.output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "openclaw_gateway.out.log").open("ab")
    stderr = (log_dir / "openclaw_gateway.err.log").open("ab")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [openclaw, "gateway", "run", "--port", str(settings.openclaw_gateway_port)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    finally:
        stdout.close()
        stderr.close()
