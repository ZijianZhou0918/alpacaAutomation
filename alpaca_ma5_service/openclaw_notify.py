from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import Settings


_OPENCLAW_GATEWAY_READY = False
_HERMES_AGENT_PYTHON = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe"
DEFAULT_NOTIFICATION_EVENT = "alpaca_trade_notify"
DEFAULT_WINDOWS_NOTIFICATION_TITLE = "Alpaca 自动监控提醒"
WINDOWS_NOTIFICATION_BODY_LIMIT = 480
_NOTIFICATION_EVENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_WINDOWS_NOTIFICATION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$title = [System.Security.SecurityElement]::Escape($env:ALPACA_NOTIFY_TITLE)
$body = [System.Security.SecurityElement]::Escape($env:ALPACA_NOTIFY_BODY)
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual><audio src='ms-winsoundevent:Notification.Default'/></toast>")
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Alpaca Automation').Show($toast)
    exit 0
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $icon = New-Object System.Windows.Forms.NotifyIcon
    try {
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
        $icon.BalloonTipTitle = $env:ALPACA_NOTIFY_TITLE
        $icon.BalloonTipText = $env:ALPACA_NOTIFY_BODY
        $icon.Visible = $true
        [System.Media.SystemSounds]::Exclamation.Play()
        $icon.ShowBalloonTip(8000)
        Start-Sleep -Seconds 8
    } finally {
        $icon.Dispose()
    }
}
"""


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


def send_trade_notification(
    settings: Settings,
    message: str,
    *,
    event: str = DEFAULT_NOTIFICATION_EVENT,
) -> None:
    if settings.trade_notify_mode == "cloud":
        send_cloud_notify_message(settings, message, event=event)
        return
    send_openclaw_telegram_message(settings, message)


def send_cloud_notify_message(
    settings: Settings,
    message: str,
    *,
    event: str = DEFAULT_NOTIFICATION_EVENT,
) -> None:
    url = settings.cloud_notify_webhook_url.strip()
    secret = settings.cloud_notify_webhook_secret.strip()
    if not url:
        raise RuntimeError("CLOUD_NOTIFY_WEBHOOK_URL is empty")
    if not secret:
        raise RuntimeError("CLOUD_NOTIFY_WEBHOOK_SECRET is empty")

    event = normalize_notification_event(event)
    body = json.dumps(
        {"event": event, "message": message},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
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


def safe_send_notification(
    settings: Settings,
    messages: list[str],
    *,
    context: str,
    event: str = DEFAULT_NOTIFICATION_EVENT,
    windows_fallback: bool = True,
    windows_title: str = DEFAULT_WINDOWS_NOTIFICATION_TITLE,
) -> bool:
    """通用通知入口：远程发送优先，失败时可回退到当前 Windows 用户桌面。"""
    if not settings.trade_notify_openclaw_enabled:
        return False
    if not messages:
        return True

    event = normalize_notification_event(event)
    for index, message in enumerate(messages):
        try:
            send_trade_notification(settings, message, event=event)
        except Exception as exc:
            note = (
                f"Notify ({settings.trade_notify_mode}) failed; main flow continues: "
                f"{context}: {type(exc).__name__}: {exc}"
            )
            print(note, flush=True)
            write_notify_log(settings, note)
            remaining = messages[index:]
            if windows_fallback and send_windows_notification_messages(
                remaining,
                title=windows_title,
            ):
                fallback_note = f"Notify (windows fallback) sent: {context}"
                write_notify_log(settings, fallback_note)
                return True
            return False

    note = (
        f"Notify ({settings.trade_notify_mode}) sent: "
        f"{context} event={event}"
    )
    print(note, flush=True)
    write_notify_log(settings, note)
    return True


def validate_notification_configuration(settings: Settings) -> tuple[str, ...]:
    """启动前验证通知必需项；只检查配置和本机发送器，不发送消息。"""
    if not settings.trade_notify_openclaw_enabled:
        raise RuntimeError("TRADE_NOTIFY_OPENCLAW_ENABLED 未启用，提醒监控拒绝启动")

    if settings.trade_notify_mode == "cloud":
        url = settings.cloud_notify_webhook_url.strip()
        secret = settings.cloud_notify_webhook_secret.strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("CLOUD_NOTIFY_WEBHOOK_URL 必须是有效的 http/https URL")
        if not secret:
            raise RuntimeError("CLOUD_NOTIFY_WEBHOOK_SECRET 为空")
        if parsed.scheme != "https":
            return ("云端通知当前使用明文 HTTP；HMAC 可验签但不能隐藏内容或阻止重放。",)
        return ()

    commands = messaging_commands()
    if not commands:
        raise RuntimeError("本机未找到可用的 OpenClaw/Hermes 通知发送器")
    target = settings.openclaw_telegram_target.strip()
    if not target and all(kind == "openclaw" for kind, _command in commands):
        raise RuntimeError("OPENCLAW_TELEGRAM_TARGET 为空，且没有可直接发送的 Hermes")
    return ()


def safe_send_openclaw_messages(
    settings: Settings,
    messages: list[str],
    *,
    context: str,
    event: str = DEFAULT_NOTIFICATION_EVENT,
    windows_fallback: bool = True,
    windows_title: str = DEFAULT_WINDOWS_NOTIFICATION_TITLE,
) -> bool:
    """兼容旧调用名；所有主项目调用统一委托给通用通知入口。"""
    return safe_send_notification(
        settings,
        messages,
        context=context,
        event=event,
        windows_fallback=windows_fallback,
        windows_title=windows_title,
    )


def normalize_notification_event(event: str) -> str:
    value = str(event or "").strip().lower()
    if not _NOTIFICATION_EVENT_PATTERN.fullmatch(value):
        raise ValueError(
            "notification event must match "
            "[a-z0-9][a-z0-9_.-]{0,63}"
        )
    return value


def send_windows_notification_messages(
    messages: list[str],
    *,
    title: str = DEFAULT_WINDOWS_NOTIFICATION_TITLE,
) -> bool:
    if os.name != "nt" or not messages:
        return False
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return False

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for message in messages:
        env = os.environ.copy()
        env["ALPACA_NOTIFY_TITLE"] = str(title).strip() or DEFAULT_WINDOWS_NOTIFICATION_TITLE
        env["ALPACA_NOTIFY_BODY"] = compact_notification_message(
            message,
            WINDOWS_NOTIFICATION_BODY_LIMIT,
        )
        try:
            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Sta",
                    "-Command",
                    _WINDOWS_NOTIFICATION_SCRIPT,
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                text=True,
                timeout=20,
                creationflags=creationflags,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(
                f"Windows 本地提醒失败：{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False
        if result.returncode != 0:
            detail = " ".join((result.stderr or result.stdout or "").split())
            print(f"Windows 本地提醒失败：{detail[:240]}", flush=True)
            return False

    print("Windows 本地提醒已发送。", flush=True)
    return True


def compact_notification_message(message: str, limit: int) -> str:
    text = " | ".join(
        line.strip()
        for line in str(message).splitlines()
        if line.strip()
    )
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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
