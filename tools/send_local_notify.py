try:
    from ._bootstrap import ensure_local_venv
except ImportError:
    from _bootstrap import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service.console_notify import notify_print


# PyCharm/click-run test message. Edit this line when you want to send a custom note.
MESSAGE = "MA5 service configured Telegram notification test"


def main() -> None:
    notify_print(MESSAGE, context="manual notification test")


if __name__ == "__main__":
    main()
