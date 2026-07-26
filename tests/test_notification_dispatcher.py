from __future__ import annotations

from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest import mock
import unittest

from alpaca_ma5_service import openclaw_notify


class NotificationDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            trade_notify_openclaw_enabled=True,
            trade_notify_mode="cloud",
        )

    def test_remote_sender_receives_generic_event(self):
        with mock.patch.object(
            openclaw_notify,
            "send_trade_notification",
        ) as remote, mock.patch.object(
            openclaw_notify,
            "write_notify_log",
        ):
            result = openclaw_notify.safe_send_notification(
                self.settings,
                ["hello"],
                context="unit",
                event="alpaca_daily_report",
            )

        self.assertTrue(result)
        remote.assert_called_once_with(
            self.settings,
            "hello",
            event="alpaca_daily_report",
        )

    def test_remote_failure_uses_shared_windows_fallback(self):
        with mock.patch.object(
            openclaw_notify,
            "send_trade_notification",
            side_effect=RuntimeError("remote down"),
        ), mock.patch.object(
            openclaw_notify,
            "send_windows_notification_messages",
            return_value=True,
        ) as windows, mock.patch.object(
            openclaw_notify,
            "write_notify_log",
        ):
            result = openclaw_notify.safe_send_notification(
                self.settings,
                ["one", "two"],
                context="unit",
                windows_title="Unit title",
            )

        self.assertTrue(result)
        windows.assert_called_once_with(
            ["one", "two"],
            title="Unit title",
        )

    def test_partial_remote_failure_only_falls_back_unsent_messages(self):
        with mock.patch.object(
            openclaw_notify,
            "send_trade_notification",
            side_effect=[None, RuntimeError("second failed")],
        ), mock.patch.object(
            openclaw_notify,
            "send_windows_notification_messages",
            return_value=True,
        ) as windows, mock.patch.object(
            openclaw_notify,
            "write_notify_log",
        ):
            result = openclaw_notify.safe_send_notification(
                self.settings,
                ["sent", "unsent", "remaining"],
                context="unit",
            )

        self.assertTrue(result)
        windows.assert_called_once_with(
            ["unsent", "remaining"],
            title=openclaw_notify.DEFAULT_WINDOWS_NOTIFICATION_TITLE,
        )

    def test_disabled_notification_does_not_call_any_sender(self):
        settings = SimpleNamespace(
            trade_notify_openclaw_enabled=False,
            trade_notify_mode="cloud",
        )
        with mock.patch.object(
            openclaw_notify,
            "send_trade_notification",
        ) as remote, mock.patch.object(
            openclaw_notify,
            "send_windows_notification_messages",
        ) as windows:
            result = openclaw_notify.safe_send_notification(
                settings,
                ["hello"],
                context="unit",
            )

        self.assertFalse(result)
        remote.assert_not_called()
        windows.assert_not_called()

    def test_windows_sender_uses_generic_title_and_compact_body(self):
        completed = CompletedProcess(["powershell"], 0, "", "")
        with mock.patch.object(
            openclaw_notify.os,
            "name",
            "nt",
        ), mock.patch.object(
            openclaw_notify.shutil,
            "which",
            return_value="powershell.exe",
        ), mock.patch.object(
            openclaw_notify.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = openclaw_notify.send_windows_notification_messages(
                ["one\n\ntwo"],
                title="Shared title",
            )

        self.assertTrue(result)
        self.assertEqual(run.call_args.kwargs["env"]["ALPACA_NOTIFY_TITLE"], "Shared title")
        self.assertEqual(run.call_args.kwargs["env"]["ALPACA_NOTIFY_BODY"], "one | two")

    def test_event_validation_rejects_unbounded_values(self):
        with self.assertRaises(ValueError):
            openclaw_notify.normalize_notification_event("bad event")

    def test_invalid_event_is_rejected_before_any_delivery(self):
        with mock.patch.object(
            openclaw_notify,
            "send_trade_notification",
        ) as remote, mock.patch.object(
            openclaw_notify,
            "send_windows_notification_messages",
        ) as windows:
            with self.assertRaises(ValueError):
                openclaw_notify.safe_send_notification(
                    self.settings,
                    ["hello"],
                    context="unit",
                    event="bad event",
                )

        remote.assert_not_called()
        windows.assert_not_called()

    def test_cloud_configuration_is_checked_before_monitoring(self):
        settings = SimpleNamespace(
            trade_notify_openclaw_enabled=True,
            trade_notify_mode="cloud",
            cloud_notify_webhook_url="https://example.invalid/hook",
            cloud_notify_webhook_secret="",
        )

        with self.assertRaisesRegex(RuntimeError, "SECRET"):
            openclaw_notify.validate_notification_configuration(settings)

    def test_plain_http_configuration_returns_security_warning(self):
        settings = SimpleNamespace(
            trade_notify_openclaw_enabled=True,
            trade_notify_mode="cloud",
            cloud_notify_webhook_url="http://127.0.0.1:8644/hook",
            cloud_notify_webhook_secret="unit-secret",
        )

        warnings = openclaw_notify.validate_notification_configuration(settings)

        self.assertEqual(len(warnings), 1)
        self.assertIn("HTTP", warnings[0])


if __name__ == "__main__":
    unittest.main()
