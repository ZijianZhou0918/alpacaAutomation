from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest import TestCase

from alpaca_ma5_service.config import build_settings


ROOT = Path(__file__).resolve().parent.parent
ROOT_PYTHON_ENTRIES = {
    "monitor_afterhours.py",
    "monitor_auto.py",
    "monitor_ma5_forever.py",
    "monitor_premarket_ma5.py",
    "open_daily_review.py",
    "run_backtest_daily_history_rebuild.py",
    "run_backtest_kdj_volume_reversal.py",
    "watchcode_afterhours.py",
    "watchcode_chart.py",
    "watchcode_ma5.py",
    "watchcode_premarket.py",
}


class EntrypointLayoutTests(TestCase):
    def test_root_python_files_are_public_run_entries_only(self):
        root_python_files = {path.name for path in ROOT.glob("*.py")}

        self.assertEqual(root_python_files, ROOT_PYTHON_ENTRIES)
        for name in sorted(root_python_files):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)
            definitions = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            self.assertEqual(definitions, [], f"{name} must remain a thin public run entry")

    def test_root_entry_imports_keep_existing_module_names_compatible(self):
        public_module = importlib.import_module("monitor_auto")
        workflow_module = importlib.import_module(
            "alpaca_ma5_service.workflows.monitoring.auto"
        )

        self.assertIs(public_module, workflow_module)

    def test_watchcode_runtime_files_live_under_data_directory(self):
        settings = build_settings()

        self.assertEqual(
            settings.watch_codes_file,
            ROOT / "data" / "watchcodes" / "watch_codes.txt",
        )
        self.assertFalse((ROOT / "watch_codes.txt").exists())
        self.assertFalse((ROOT / "watch_codes_premarket.txt").exists())
        self.assertFalse((ROOT / "watch_code_afterhours.txt").exists())
