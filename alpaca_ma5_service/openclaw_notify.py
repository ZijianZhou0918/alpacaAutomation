from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import Settings


_OPENCLAW_GATEWAY_READY = False
_HERMES_AGENT_PYTHON = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe"


def send_openclaw_telegram_message(settings: Settings, message: str) -> None:
    """通过本机 OpenClaw 或 Hermes 发送一条 Telegram 消息。"""
    errors: list[str] = []
    target = settings.openclaw_telegram_target.strip()

    for kind, command in messaging_commands():
        try:
            if kind == "hermes":
                send_hermes_telegram_message(command, target, message)
            else:
                if not target:
                    errors.append("openclaw: OPENCLAW_TELEGRAM_TARGET is empty")
                    continue
                send_openclaw_message(settings, command[0], target, message)
            return
        except Exception as exc:
            errors.append(f"{kind}: {type(exc).__name__}: {exc}")
    raise RuntimeError("all Telegram notification senders failed: " + " | ".join(errors))


def send_trade_notification(settings: Settings, message: str) -> None:
    if settings.trade_notify_mode == "cloud":
        send_cloud_notify_message(settings, message)
        return
    send_openclaw_telegram_message(settings, message)


def send_cloud_notify_message(settings: Settings, message: str) -> None:
    url = settings.cloud_notify_webhook_url.strip()
    secret = settings.cloud_notify_webhook_secret.strip()
    if not url:
        raise RuntimeError("CLOUD_NOTIFY_WEBHOOK_URL is empty")
    if not secret:
        raise RuntimeError("CLOUD_NOTIFY_WEBHOOK_SECRET is empty")

    body = json.dumps({"message": message}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 400:
            detail = response.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"cloud notify failed: http={response.status} {detail}")


def send_openclaw_message(settings: Settings, openclaw: str, target: str, message: str) -> None:
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


def safe_send_openclaw_messages(settings: Settings, messages: list[str], *, context: str) -> bool:
    """Send each notification; failures are logged and do not stop trading."""
    if not settings.trade_notify_openclaw_enabled:
        return False
    try:
        for message in messages:
            send_trade_notification(settings, message)
        note = f"Trade notify ({settings.trade_notify_mode}) sent: {context}"
        print(note, flush=True)
        write_notify_log(settings, note)
        return True
    except Exception as exc:
        note = f"Trade notify ({settings.trade_notify_mode}) failed; main flow continues: {context}: {type(exc).__name__}: {exc}"
        print(note)
        write_notify_log(settings, note)
        return False


def messaging_command() -> tuple[str, list[str]]:
    """优先兼容旧 OpenClaw；本机没有 openclaw 时使用 Hermes。"""
    commands = messaging_commands()
    if not commands:
        raise FileNotFoundError("openclaw/hermes command not found")
    return commands[0]


def messaging_commands() -> list[tuple[str, list[str]]]:
    """按优先级返回可用发送器；OpenClaw 失败时可继续尝试 Hermes。"""
    commands: list[tuple[str, list[str]]] = []
    openclaw = shutil.which("openclaw.cmd") or shutil.which("openclaw") or shutil.which("openclaw-cn.cmd") or shutil.which("openclaw-cn")
    if openclaw:
        commands.append(("openclaw", [openclaw]))

    hermes = shutil.which("hermes.cmd") or shutil.which("hermes")
    if hermes:
        commands.append(("hermes", [hermes]))

    if _HERMES_AGENT_PYTHON.exists():
        commands.append(("hermes", [str(_HERMES_AGENT_PYTHON), "-m", "hermes_cli.main"]))

    return commands


def openclaw_executable() -> str:
    """定位旧 openclaw 命令行程序，保留给旧调用方和测试使用。"""
    kind, command = messaging_command()
    if kind != "openclaw":
        raise FileNotFoundError("openclaw command not found in PATH")
    return command[0]


def send_hermes_telegram_message(command: list[str], target: str, message: str) -> None:
    """使用 Hermes 当前的 send 命令发送 Telegram 消息。"""
    ensure_hermes_gateway_started(command)
    target = target.strip()
    hermes_target = "telegram" if not target else target if target.startswith("telegram") else f"telegram:{target}"
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


def ensure_hermes_gateway_started(command: list[str]) -> None:
    """Best-effort start of the local Hermes gateway; Telegram send can still work without it."""
    try:
        status = subprocess.run(
            command + ["gateway", "status"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=20,
        )
    except Exception as exc:
        print(f"Hermes gateway status check failed; will still try send. detail={type(exc).__name__}: {exc}", flush=True)
        return
    output = f"{status.stdout}\n{status.stderr}"
    if status.returncode == 0 and "No gateway process detected" not in output:
        return
    start = subprocess.run(
        command + ["gateway", "start"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=60,
    )
    if start.returncode != 0:
        detail = (start.stderr or start.stdout or "").strip()
        print(f"Hermes gateway start failed; will still try send. detail={detail}", flush=True)


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
