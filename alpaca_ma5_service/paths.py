"""Canonical project paths shared by runtime workflows and tooling."""

from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WATCHCODE_DIR = DATA_DIR / "watchcodes"
INTRADAY_WATCH_CODES_PATH = WATCHCODE_DIR / "watch_codes.txt"
PREMARKET_WATCH_CODES_PATH = WATCHCODE_DIR / "watch_codes_premarket.txt"
AFTERHOURS_WATCH_CODES_PATH = WATCHCODE_DIR / "watch_code_afterhours.txt"


def watchcode_dir(base_dir: Path | str = BASE_DIR) -> Path:
    """Return the WatchCode directory for a project root."""
    return Path(base_dir).resolve() / "data" / "watchcodes"


def intraday_monitor_config_path(base_dir: Path | str = BASE_DIR) -> Path:
    """Return the editable intraday workflow/config implementation path."""
    return (
        Path(base_dir).resolve()
        / "alpaca_ma5_service"
        / "workflows"
        / "monitoring"
        / "intraday.py"
    )
