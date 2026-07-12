from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from alpaca_ma5_service.monitor_runtime import monitor_runtime, read_monitor_tasks


class MonitorRuntimeTests(TestCase):
    def test_runtime_session_mirrors_output_and_reports_running_then_stopped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs"

            with monitor_runtime(output_dir, "monitor_ma5", "intraday") as session:
                print("unit runtime output", flush=True)
                payload = read_monitor_tasks(root)
                self.assertEqual(payload["active_count"], 1)
                self.assertEqual(payload["tasks"][0]["instance_id"], session.instance_id)
                self.assertEqual(payload["tasks"][0]["status"], "running")
                self.assertIn("unit runtime output", payload["tasks"][0]["log"])

            payload = read_monitor_tasks(root)
            self.assertEqual(payload["active_count"], 0)
            self.assertEqual(payload["tasks"][0]["status"], "stopped")
            self.assertEqual(payload["tasks"][0]["task_label"], "盘中 MA5 盯盘")

    def test_nested_monitor_reuses_one_task_and_updates_phase(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs"

            with monitor_runtime(output_dir, "monitor_auto", "auto") as outer:
                with monitor_runtime(output_dir, "monitor_ma5", "intraday") as inner:
                    self.assertIs(inner, outer)
                    payload = read_monitor_tasks(root)
                    self.assertEqual(len(payload["tasks"]), 1)
                    self.assertEqual(payload["tasks"][0]["phase_label"], "盘中监控")
                payload = read_monitor_tasks(root)
                self.assertEqual(payload["tasks"][0]["phase_label"], "自动判断时段")

    def test_runtime_events_group_repeated_polling_and_prioritize_state_changes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs"

            with monitor_runtime(output_dir, "monitor_ma5", "intraday"):
                print("[盘中检查 001] US.RNAZ | 当前价 8.7100 | 进入买点区间 | 继续监控", flush=True)
                print("[盘中检查 002] US.RNAZ | 当前价 8.7200 | 进入买点区间 | 继续监控", flush=True)
                print("[盘中检查 003] US.TDTH | 当前价 0.8060 | 买单已取消 | 继续监控", flush=True)
                print("[盘中检查 004] US.FXHO | 当前价 16.7250 | 距跌幅门槛 1.44% | 继续监控", flush=True)
                payload = read_monitor_tasks(root)

            events = payload["tasks"][0]["events"]
            by_symbol = {event["symbol"]: event for event in events if event["symbol"]}
            self.assertEqual(by_symbol["US.RNAZ"]["title"], "进入买入触发区")
            self.assertEqual(by_symbol["US.RNAZ"]["count"], 2)
            self.assertEqual(by_symbol["US.TDTH"]["severity"], "critical")
            self.assertEqual(by_symbol["US.TDTH"]["action"], "查看券商订单")
            self.assertEqual(by_symbol["US.FXHO"]["kind"], "threshold")
