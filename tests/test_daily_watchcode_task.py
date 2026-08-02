from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DailyWatchcodeTaskTests(unittest.TestCase):
    def test_runner_generates_only_intraday_watchcode_without_monitor_entry(self):
        text = (ROOT / "tools" / "run_daily_watchcodes.ps1").read_text(encoding="utf-8")

        self.assertNotIn("watchcode_premarket.py", text)
        self.assertIn("watchcode_ma5.py", text)
        self.assertIn("Order=intraday", text)
        self.assertIn("Premarket screening is disabled", text)
        self.assertNotIn("monitor_auto.py", text)
        self.assertNotIn("monitor_ma5_forever.py", text)
        self.assertIn("AlpacaMA5DailyWatchcodes", text)
        self.assertIn('$env:PYTHONUTF8 = "1"', text)
        self.assertIn('$env:PYTHONIOENCODING = "utf-8"', text)
        self.assertIn('$ErrorActionPreference = "Continue"', text)

    def test_installer_uses_dynamic_project_path_and_daily_0050_trigger(self):
        text = (ROOT / "tools" / "install_daily_watchcode_task.ps1").read_text(encoding="utf-8")

        self.assertIn('TaskName = "AlpacaMA5-0050-GenerateWatchcodes"', text)
        self.assertIn('New-ScheduledTaskTrigger -Daily -At "00:50"', text)
        self.assertIn('LegacyTaskNames = @("AlpacaMA5-0005-GenerateWatchcodes")', text)
        self.assertIn("Unregister-ScheduledTask", text)
        self.assertIn('ProjectDir = Split-Path -Parent $PSScriptRoot', text)
        self.assertIn("run_daily_watchcodes.ps1", text)
        self.assertIn("StartWhenAvailable", text)
        self.assertIn("IgnoreNew", text)
        self.assertNotIn("monitor_auto.py", text)


if __name__ == "__main__":
    unittest.main()
