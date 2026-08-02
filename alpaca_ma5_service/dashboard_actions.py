from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .monitor_runtime import read_monitor_tasks
from .paths import watchcode_dir
from .watchlist import read_watch_codes
from .watchlist_generator import watchcode_matches_rules


ACTION_GENERATE_WATCHCODE = "generate-watchcode"
ACTION_START_MONITOR = "start-monitor"
ACTION_GENERATE_PREMARKET_WATCHCODE = "generate-premarket-watchcode"
ACTION_START_PREMARKET_MONITOR = "start-premarket-monitor"
ACTION_STOP_MONITOR = "stop-monitor"
ALLOWED_ACTIONS = {
    ACTION_GENERATE_WATCHCODE,
    ACTION_START_MONITOR,
    ACTION_GENERATE_PREMARKET_WATCHCODE,
    ACTION_START_PREMARKET_MONITOR,
    ACTION_STOP_MONITOR,
}
STOPPABLE_TASK_NAMES = {"monitor_auto", "monitor_ma5", "monitor_premarket", "watchcode_ma5"}
WATCHCODE_TASK_NAMES = {"watchcode_ma5"}

_ACTION_LOCK = threading.RLock()
_ACTION_PROCESSES: dict[str, subprocess.Popen] = {}


def action_status(base_dir: Path | str) -> dict[str, Any]:
    root = Path(base_dir).resolve()
    watch_dir = watchcode_dir(root)
    watch_path = watch_dir / "watch_codes.txt"
    expected = _expected_signal_date()
    signal_date = _read_watchcode_signal_date(watch_path)
    try:
        symbol_count = len(read_watch_codes(watch_path)) if watch_path.is_file() else 0
    except Exception:
        symbol_count = 0
    try:
        rules_match = watchcode_matches_rules(watch_path, _current_intraday_watchlist_rules())
    except Exception:
        # 策略配置无效时绝不能把股票池标成可启动；具体错误由启动入口报告。
        rules_match = False
    modified_at = None
    try:
        modified_at = datetime.fromtimestamp(watch_path.stat().st_mtime, ZoneInfo("America/New_York")).isoformat(timespec="seconds")
    except OSError:
        pass
    tasks_payload = read_monitor_tasks(root)
    running_tasks = [task for task in tasks_payload["tasks"] if task["status"] == "running"]
    monitor_running = any(task.get("task_name") in {"monitor_auto", "monitor_ma5", "monitor_premarket"} for task in running_tasks)
    premarket_monitor_running = any(
        task.get("task_name") == "monitor_premarket"
        or (task.get("task_name") == "monitor_auto" and task.get("phase") == "premarket")
        for task in running_tasks
    )
    intraday_generator_running = any(task.get("task_name") == "watchcode_ma5" for task in running_tasks)
    premarket_generator_running = False
    generator_running = intraday_generator_running
    with _ACTION_LOCK:
        _discard_finished_processes()
        pending_actions = sorted(action for action, process in _ACTION_PROCESSES.items() if process.poll() is None)
    return {
        "ok": True,
        "watchcode": {
            "path": str(watch_path),
            "exists": watch_path.is_file(),
            "ready": bool(symbol_count and signal_date == expected and rules_match),
            "expected_signal_date": expected.isoformat(),
            "signal_date": signal_date.isoformat() if signal_date else None,
            "symbol_count": symbol_count,
            "rules_match": rules_match,
            "modified_at": modified_at,
        },
        "premarket_watchcode": {
            "path": None,
            "exists": False,
            "ready": True,
            "expected_signal_date": None,
            "signal_date": None,
            "symbol_count": 0,
            "modified_at": None,
            "mode": "positions_only",
        },
        "monitor_running": monitor_running,
        "premarket_monitor_running": premarket_monitor_running,
        "generator_running": generator_running,
        "intraday_generator_running": intraday_generator_running,
        "premarket_generator_running": premarket_generator_running,
        "pending_actions": pending_actions,
    }


def launch_action(
    action: str,
    *,
    base_dir: Path | str,
    popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    stop_process_tree: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    if action not in ALLOWED_ACTIONS:
        raise ValueError("unsupported dashboard action")
    root = Path(base_dir).resolve()
    if action == ACTION_STOP_MONITOR:
        return stop_monitor_tasks(root, stop_process_tree=stop_process_tree)
    if action == ACTION_GENERATE_PREMARKET_WATCHCODE:
        return {
            "ok": True,
            "status": "disabled",
            "action": action,
            "message": "盘前 WatchCode 已停用；盘前只读取 Alpaca 当前持仓。",
            "watchcode": {"ready": True, "mode": "positions_only", "symbol_count": 0},
        }
    status = action_status(root)
    start_actions = {ACTION_START_MONITOR, ACTION_START_PREMARKET_MONITOR}
    generate_actions = {ACTION_GENERATE_WATCHCODE}
    selected_watchcode = status["premarket_watchcode"] if "premarket" in action else status["watchcode"]
    if action in start_actions and (
        status["monitor_running"] or any(item in status["pending_actions"] for item in start_actions)
    ):
        return {"ok": True, "status": "already_running", "action": action, "message": "盯盘任务已经在运行。", "watchcode": selected_watchcode}
    if action in generate_actions and (
        status["generator_running"] or any(item in status["pending_actions"] for item in generate_actions)
    ):
        return {"ok": True, "status": "already_running", "action": action, "message": "WatchCode 生成任务已经在运行。", "watchcode": selected_watchcode}

    python_executable = _python_executable(root)
    log_dir = root / "outputs" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    log_path = log_dir / f"dashboard_actions_{stamp}.log"
    command = [str(python_executable), "-u", "-m", "alpaca_ma5_service.dashboard_actions", "run", action, "--base-dir", str(root)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["MA5_DASHBOARD_ACTION"] = "1"
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)

    with _ACTION_LOCK:
        _discard_finished_processes()
        existing = _ACTION_PROCESSES.get(action)
        if existing is not None and existing.poll() is None:
            return {"ok": True, "status": "already_running", "action": action, "message": "任务正在启动，请稍候。", "watchcode": selected_watchcode}
        log_handle = log_path.open("a", encoding="utf-8", buffering=1)
        try:
            process = popen_factory(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
                close_fds=True,
            )
        finally:
            log_handle.close()
        _ACTION_PROCESSES[action] = process

    session_label = "盘前" if "premarket" in action else "盘中"
    watch_message = (
        f"{session_label} WatchCode 已就绪，将直接启动盯盘。"
        if selected_watchcode["ready"]
        else f"{session_label} WatchCode 缺失或过期，将先生成，成功后再启动盯盘。"
    )
    if action == ACTION_START_PREMARKET_MONITOR:
        watch_message = "盘前持仓监控将直接启动；不读取或生成任何 WatchCode。"
    message = f"{session_label} WatchCode 生成任务已启动。" if action in generate_actions else watch_message
    return {
        "ok": True,
        "status": "started",
        "action": action,
        "pid": int(process.pid),
        "message": message,
        "watchcode": selected_watchcode,
    }


def stop_monitor_tasks(
    base_dir: Path | str,
    *,
    stop_process_tree: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    root = Path(base_dir).resolve()
    stopper = stop_process_tree or _stop_process_tree
    candidate_pids: list[int] = []

    with _ACTION_LOCK:
        _discard_finished_processes()
        for action in (
            ACTION_START_MONITOR,
            ACTION_GENERATE_WATCHCODE,
            ACTION_START_PREMARKET_MONITOR,
            ACTION_GENERATE_PREMARKET_WATCHCODE,
        ):
            process = _ACTION_PROCESSES.pop(action, None)
            if process is not None and process.poll() is None:
                candidate_pids.append(int(process.pid))

    runtime_tasks = read_monitor_tasks(root).get("tasks", [])
    for task in runtime_tasks:
        if task.get("status") != "running" or task.get("task_name") not in STOPPABLE_TASK_NAMES:
            continue
        try:
            pid = int(task.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            candidate_pids.append(pid)

    stopped_pids: list[int] = []
    failed_pids: list[int] = []
    for pid in dict.fromkeys(candidate_pids):
        if pid <= 0 or pid == os.getpid():
            continue
        try:
            stopped = bool(stopper(pid))
        except (OSError, subprocess.SubprocessError):
            stopped = False
        if stopped:
            stopped_pids.append(pid)
        else:
            failed_pids.append(pid)

    if failed_pids:
        raise RuntimeError(f"could not stop MA5 task processes: {', '.join(map(str, failed_pids))}")
    if not stopped_pids:
        return {
            "ok": True,
            "status": "not_running",
            "action": ACTION_STOP_MONITOR,
            "message": "当前没有运行中的盯盘任务。",
            "stopped_pids": [],
        }
    return {
        "ok": True,
        "status": "stopped",
        "action": ACTION_STOP_MONITOR,
        "message": f"已结束盯盘任务（{len(stopped_pids)} 个进程）。",
        "stopped_pids": stopped_pids,
    }


def _stop_process_tree(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            creationflags=creationflags,
        )
        if completed.returncode == 0:
            return True
        from .monitor_runtime import _pid_is_running

        return not _pid_is_running(pid)
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return True
    return True


def run_action(action: str, *, base_dir: Path | str) -> None:
    root = Path(base_dir).resolve()
    os.chdir(root)
    if action == ACTION_GENERATE_WATCHCODE:
        from .workflows.watchcode.intraday import generate_ma5_watchcode

        generate_ma5_watchcode()
        return
    if action == ACTION_GENERATE_PREMARKET_WATCHCODE:
        print("盘前 WatchCode 已停用；盘前只监控 Alpaca 当前持仓。", flush=True)
        return
    if action == ACTION_START_MONITOR:
        from .config import build_settings
        from .workflows.monitoring.auto import (
            configure_console_logging,
            ensure_current_session_watchcode,
            monitor_auto,
        )

        configure_console_logging()
        settings = build_settings()
        now_et = datetime.now(ZoneInfo(settings.market_timezone))
        _wait_for_watchcode_generation(root)
        ensure_current_session_watchcode(now_et)
        monitor_auto()
        return
    if action == ACTION_START_PREMARKET_MONITOR:
        from .workflows.monitoring.auto import configure_console_logging
        from .workflows.monitoring.premarket import monitor_premarket_ma5

        configure_console_logging()
        monitor_premarket_ma5()
        return
    raise ValueError("unsupported dashboard action")


def _python_executable(root: Path) -> Path:
    candidate = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate if candidate.is_file() else Path(sys.executable).resolve()


def _expected_signal_date():
    from .config import build_settings
    from .workflows.monitoring.auto import expected_signal_date

    settings = build_settings()
    return expected_signal_date(datetime.now(ZoneInfo(settings.market_timezone)))


def _current_intraday_watchlist_rules():
    """按盘中 workflow 的真实配置解析规则，保持看板与启动入口一致。"""
    from .strategy_framework import resolve_strategy_runtime
    from .workflows.monitoring.intraday import build_monitor_settings

    settings = build_monitor_settings()
    return resolve_strategy_runtime(settings).watchlist.screen_rules()


def _read_watchcode_signal_date(path: Path):
    from .workflows.monitoring.auto import read_watchcode_signal_date

    return read_watchcode_signal_date(path)


def _discard_finished_processes() -> None:
    finished = [action for action, process in _ACTION_PROCESSES.items() if process.poll() is not None]
    for action in finished:
        _ACTION_PROCESSES.pop(action, None)


def _wait_for_watchcode_generation(root: Path, *, timeout_seconds: float = 7200.0, discovery_seconds: float = 5.0) -> None:
    started = time.monotonic()
    deadline = started + timeout_seconds
    saw_generator = False
    while time.monotonic() < deadline:
        tasks = read_monitor_tasks(root)["tasks"]
        generator_running = any(
            task["status"] == "running"
            and (task.get("task_name") in WATCHCODE_TASK_NAMES or task.get("phase") == "prepare")
            for task in tasks
        )
        if generator_running:
            saw_generator = True
            time.sleep(2.0)
            continue
        if saw_generator or time.monotonic() - started >= discovery_seconds:
            return
        time.sleep(0.5)
    raise TimeoutError("等待正在运行的 WatchCode 生成任务超时")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run allowlisted MA5 dashboard actions")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("action", choices=sorted(ALLOWED_ACTIONS))
    run_parser.add_argument("--base-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    run_action(args.action, base_dir=args.base_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
