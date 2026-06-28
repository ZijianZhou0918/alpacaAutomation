from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import Settings


_OPENCLAW_GATEWAY_READY = False
_HERMES_AGENT_PYTHON = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe"


def send_openclaw_telegram_message(settings: Settings, message: str) -> None:
    """通过本机 OpenClaw 或 Hermes 发送一条 Telegram 消息。"""
    target = settings.openclaw_telegram_target.strip()
    if not target:
        raise ValueError("OPENCLAW_TELEGRAM_TARGET is empty")

    kind, command = messaging_command()
    if kind == "hermes":
        send_hermes_telegram_message(command, target, message)
        return

    openclaw = command[0]
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
        note = f"OpenClaw/Hermes 通知已发送: {context}"
        print(note, flush=True)
        write_notify_log(settings, note)
    except Exception as exc:
        note = f"OpenClaw/Hermes 通知失败，不影响主流程 {context}：{type(exc).__name__}: {exc}"
        print(note)
        write_notify_log(settings, note)


def messaging_command() -> tuple[str, list[str]]:
    """优先兼容旧 OpenClaw；本机没有 openclaw 时使用 Hermes。"""
    openclaw = shutil.which("openclaw.cmd") or shutil.which("openclaw") or shutil.which("openclaw-cn.cmd") or shutil.which("openclaw-cn")
    if openclaw:
        return "openclaw", [openclaw]

    hermes = shutil.which("hermes.cmd") or shutil.which("hermes")
    if hermes:
        return "hermes", [hermes]

    if _HERMES_AGENT_PYTHON.exists():
        return "hermes", [str(_HERMES_AGENT_PYTHON), "-m", "hermes_cli.main"]

    raise FileNotFoundError("openclaw/hermes command not found")


def openclaw_executable() -> str:
    """定位旧 openclaw 命令行程序，保留给旧调用方和测试使用。"""
    kind, command = messaging_command()
    if kind != "openclaw":
        raise FileNotFoundError("openclaw command not found in PATH")
    return command[0]


def send_hermes_telegram_message(command: list[str], target: str, message: str) -> None:
    """使用 Hermes 当前的 send 命令发送 Telegram 消息。"""
    hermes_target = target if target.startswith("telegram") else f"telegram:{target}"
    result = subprocess.run(
        command + ["send", "--to", hermes_target, message, "--json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"hermes Telegram send failed: {detail}")


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


def write_notify_log(settings: Settings, message: str) -> None:
    """后台任务没有控制台时，把通知结果也落到文件。"""
    log_dir = settings.output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"trade_notify_{datetime.now().strftime('%Y%m%d')}.log"
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line)
