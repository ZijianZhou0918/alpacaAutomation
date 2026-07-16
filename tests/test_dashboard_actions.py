from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from alpaca_ma5_service import dashboard_actions


class DashboardActionsTests(TestCase):
    def tearDown(self) -> None:
        dashboard_actions._ACTION_PROCESSES.clear()

    def test_action_status_marks_matching_watchcode_ready(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch_path = root / "watch_codes.txt"
            watch_path.write_text("# signal_date=2026-07-10\nUS.HAO\nUS.RNAZ\n", encoding="utf-8")
            with patch.object(dashboard_actions, "_expected_signal_date", return_value=date(2026, 7, 10)):
                with patch.object(dashboard_actions, "_read_watchcode_signal_date", return_value=date(2026, 7, 10)):
                    status = dashboard_actions.action_status(root)

        self.assertTrue(status["watchcode"]["ready"])
        self.assertEqual(status["watchcode"]["symbol_count"], 2)
        self.assertFalse(status["monitor_running"])

    def test_action_status_reports_premarket_watchcode_separately(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watch_codes_premarket.txt"
            path.write_text("# signal_date=2026-07-10\nUS.HAO\nUS.RNAZ\n", encoding="utf-8")
            with patch.object(dashboard_actions, "_expected_signal_date", return_value=date(2026, 7, 10)):
                status = dashboard_actions.action_status(root)

        self.assertTrue(status["premarket_watchcode"]["ready"])
        self.assertEqual(status["premarket_watchcode"]["symbol_count"], 2)
        self.assertFalse(status["premarket_monitor_running"])

    def test_launch_action_uses_only_allowlisted_module_command(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = MagicMock()
            child.pid = 4321
            child.poll.return_value = None
            status = {
                "watchcode": {"ready": False, "expected_signal_date": "2026-07-10"},
                "monitor_running": False,
                "generator_running": False,
                "pending_actions": [],
            }
            with patch.object(dashboard_actions, "action_status", return_value=status):
                result = dashboard_actions.launch_action(
                    dashboard_actions.ACTION_START_MONITOR,
                    base_dir=root,
                    popen_factory=MagicMock(return_value=child),
                )

        self.assertEqual(result["status"], "started")
        self.assertIn("先生成", result["message"])
        process = dashboard_actions._ACTION_PROCESSES[dashboard_actions.ACTION_START_MONITOR]
        self.assertIs(process, child)

    def test_start_monitor_action_checks_watchcode_before_monitor(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[str] = []
            settings = SimpleNamespace(market_timezone="America/New_York")
            previous = Path.cwd()
            try:
                with patch("monitor_auto.configure_console_logging", side_effect=lambda: calls.append("logging")):
                    with patch("monitor_auto.ensure_current_session_watchcode", side_effect=lambda _now: calls.append("watchcode")):
                        with patch("monitor_auto.monitor_auto", side_effect=lambda: calls.append("monitor")):
                            with patch("alpaca_ma5_service.config.build_settings", return_value=settings):
                                with patch.object(dashboard_actions, "_wait_for_watchcode_generation", side_effect=lambda _root: calls.append("wait")):
                                    dashboard_actions.run_action(dashboard_actions.ACTION_START_MONITOR, base_dir=root)
            finally:
                os.chdir(previous)

        self.assertLess(calls.index("wait"), calls.index("watchcode"))
        self.assertLess(calls.index("watchcode"), calls.index("monitor"))

    def test_start_premarket_monitor_prepares_premarket_watchcode_first(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[str] = []
            settings = SimpleNamespace(market_timezone="America/New_York")
            previous = Path.cwd()
            try:
                with patch("monitor_auto.configure_console_logging", side_effect=lambda: calls.append("logging")):
                    with patch("monitor_auto.ensure_premarket_watchcode", side_effect=lambda _now: calls.append("premarket_watchcode")):
                        with patch("monitor_premarket_ma5.monitor_premarket_ma5", side_effect=lambda: calls.append("premarket_monitor")):
                            with patch("alpaca_ma5_service.config.build_settings", return_value=settings):
                                with patch.object(dashboard_actions, "_wait_for_watchcode_generation", side_effect=lambda _root: calls.append("wait")):
                                    dashboard_actions.run_action(dashboard_actions.ACTION_START_PREMARKET_MONITOR, base_dir=root)
            finally:
                os.chdir(previous)

        self.assertLess(calls.index("wait"), calls.index("premarket_watchcode"))
        self.assertLess(calls.index("premarket_watchcode"), calls.index("premarket_monitor"))

    def test_wait_for_watchcode_generation_tracks_premarket_generator_until_finished(self):
        running = {
            "tasks": [
                {"status": "running", "task_name": "watchcode_premarket", "phase": "screen"},
            ]
        }
        with patch.object(dashboard_actions, "read_monitor_tasks", side_effect=[running, {"tasks": []}]) as reader:
            with patch.object(dashboard_actions.time, "monotonic", side_effect=[0.0, 0.0, 1.0]):
                with patch.object(dashboard_actions.time, "sleep") as sleep:
                    dashboard_actions._wait_for_watchcode_generation(Path("."))

        self.assertEqual(reader.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_stop_monitor_action_ends_tracked_and_runtime_processes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = MagicMock()
            child.pid = 4321
            child.poll.return_value = None
            dashboard_actions._ACTION_PROCESSES[dashboard_actions.ACTION_START_MONITOR] = child
            runtime_payload = {
                "tasks": [
                    {"status": "running", "task_name": "monitor_auto", "pid": 9876},
                    {"status": "running", "task_name": "unrelated_task", "pid": 2468},
                ]
            }
            stopped: list[int] = []
            with patch.object(dashboard_actions, "read_monitor_tasks", return_value=runtime_payload):
                result = dashboard_actions.launch_action(
                    dashboard_actions.ACTION_STOP_MONITOR,
                    base_dir=root,
                    stop_process_tree=lambda pid: stopped.append(pid) or True,
                )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(stopped, [4321, 9876])
        self.assertEqual(result["stopped_pids"], [4321, 9876])
        self.assertNotIn(dashboard_actions.ACTION_START_MONITOR, dashboard_actions._ACTION_PROCESSES)

    def test_stop_monitor_action_is_idempotent_when_nothing_is_running(self):
        with TemporaryDirectory() as tmp:
            with patch.object(dashboard_actions, "read_monitor_tasks", return_value={"tasks": []}):
                result = dashboard_actions.launch_action(
                    dashboard_actions.ACTION_STOP_MONITOR,
                    base_dir=Path(tmp),
                    stop_process_tree=lambda _pid: self.fail("stopper should not be called"),
                )

        self.assertEqual(result["status"], "not_running")
        self.assertEqual(result["stopped_pids"], [])
