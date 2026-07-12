from __future__ import annotations

from .config import Settings, build_settings
from .openclaw_notify import safe_send_openclaw_messages


def send_console_notification(message: str, *, context: str = "console", settings: Settings | None = None) -> None:
    """Send a concise console/Python status message to the configured Telegram channel."""
    settings = settings or build_settings()
    safe_send_openclaw_messages(settings, [message], context=context)


def notify_print(message: str, *, context: str = "console", settings: Settings | None = None, flush: bool = True) -> None:
    """Print a line locally and send the same line to Telegram."""
    print(message, flush=flush)
    send_console_notification(message, context=context, settings=settings)
