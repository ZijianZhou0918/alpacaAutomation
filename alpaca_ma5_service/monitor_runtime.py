from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO


RUNTIME_DIRECTORY_NAME = "monitor_runtime"
HEARTBEAT_SECONDS = 2.0
STALE_AFTER_SECONDS = 15.0
STATE_WRITE_ATTEMPTS = 6
STATE_WRITE_RETRY_SECONDS = 0.05
MAX_TASKS = 8
MAX_LOG_BYTES = 160_000
MAX_LOG_LINES = 220
MAX_EVENTS = 80

_SYMBOL_PATTERN = re.compile(r"\bUS\.[A-Z0-9.-]{1,12}\b", re.IGNORECASE)
_CLOCK_PATTERN = re.compile(r"(?:^|\[)(\d{2}:\d{2}(?::\d{2})?)(?:\]|\s)")
_LEADING_CONTEXT_PATTERN = re.compile(r"^\[[^\]]+\]\s*")

TASK_LABELS = {
    "monitor_auto": "自动盯盘",
    "monitor_ma5": "盘中 MA5 盯盘",
    "monitor_premarket": "盘前持仓波动监控",
    "monitor_afterhours": "盘后 High / Low 盯盘",
    "watchcode_ma5": "生成盘中 WatchCode",
    "watchcode_premarket": "已停用的盘前 WatchCode 兼容入口",
}

PHASE_LABELS = {
    "auto": "自动判断时段",
    "prepare": "准备 WatchCode",
    "premarket": "盘前监控",
    "intraday": "盘中监控",
    "afterhours": "盘后监控",
}

_SESSION_LOCK = threading.RLock()
_ACTIVE_SESSION: "MonitorRuntimeSession | None" = None


class _MirrorStream:
    """Keep the IDE/terminal stream visible while adding one UTF-8 runtime log."""

    def __init__(self, primary: TextIO, mirror: TextIO):
        self.primary = primary
        self.mirror = mirror
        self.encoding = getattr(primary, "encoding", "utf-8") or "utf-8"
        self.errors = getattr(primary, "errors", "replace") or "replace"
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            primary_result = self.primary.write(text)
            self.primary.flush()
            self.mirror.write(text)
            self.mirror.flush()
        return len(text) if primary_result is None else int(primary_result)

    def flush(self) -> None:
        with self._lock:
            self.primary.flush()
            self.mirror.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.primary, "isatty", lambda: False)())

    def __getattr__(self, name: str):
        return getattr(self.primary, name)


class MonitorRuntimeSession:
    def __init__(self, output_dir: Path, task_name: str, phase: str):
        self.output_dir = Path(output_dir).resolve()
        self.runtime_dir = self.output_dir / RUNTIME_DIRECTORY_NAME
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.task_name = task_name
        self.phase = phase
        self.instance_id = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self.log_path = self.runtime_dir / f"{self.instance_id}.log"
        self.state_path = self.runtime_dir / f"{self.instance_id}.json"
        self.started_at = _utc_now()
        self.ended_at: str | None = None
        self.status = "running"
        self.error: str | None = None
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._log_file: TextIO | None = None
        self._original_stdout: TextIO | None = None
        self._original_stderr: TextIO | None = None
        self._heartbeat: threading.Thread | None = None
        self._state_write_failures = 0

    def start(self) -> None:
        self._log_file = self.log_path.open("a", encoding="utf-8", buffering=1)
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = _MirrorStream(sys.stdout, self._log_file)
        sys.stderr = _MirrorStream(sys.stderr, self._log_file)
        self._write_state()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"ma5-runtime-{self.instance_id}",
            daemon=True,
        )
        self._heartbeat.start()
        print(
            f"[网页看板] 已登记 {TASK_LABELS.get(self.task_name, self.task_name)}，"
            "当前控制台输出会同步显示到网页端。",
            flush=True,
        )

    def set_phase(self, phase: str) -> str:
        previous = self.phase
        self.phase = phase
        self._write_state()
        return previous

    def finish(self, *, error: BaseException | None = None) -> None:
        self.status = "failed" if error is not None else "finished"
        self.error = f"{type(error).__name__}: {error}" if error is not None else None
        self.ended_at = _utc_now()
        self._stop.set()
        try:
            if self._heartbeat is not None:
                self._heartbeat.join(timeout=HEARTBEAT_SECONDS + 0.5)
            self._write_state()
        finally:
            if self._original_stdout is not None:
                sys.stdout = self._original_stdout
            if self._original_stderr is not None:
                sys.stderr = self._original_stderr
            if self._log_file is not None:
                self._log_file.flush()
                self._log_file.close()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self._write_state()
            except Exception as exc:
                # Runtime telemetry must never terminate the trading task's heartbeat.
                self._report_state_write_failure(exc)

    def _write_state(self) -> bool:
        with self._state_lock:
            now = _utc_now()
            payload = {
                "schema_version": "1.0",
                "instance_id": self.instance_id,
                "pid": os.getpid(),
                "task_name": self.task_name,
                "task_label": TASK_LABELS.get(self.task_name, self.task_name),
                "phase": self.phase,
                "phase_label": PHASE_LABELS.get(self.phase, self.phase),
                "status": self.status,
                "started_at": self.started_at,
                "heartbeat_at": now,
                "ended_at": self.ended_at,
                "source": _runtime_source(),
                "command": Path(sys.argv[0]).name if sys.argv else "python",
                "log_file": self.log_path.name,
                "error": self.error,
            }
            encoded = json.dumps(payload, ensure_ascii=False)
            last_error: Exception | None = None
            for attempt in range(1, STATE_WRITE_ATTEMPTS + 1):
                temporary = self.runtime_dir / (
                    f".{self.instance_id}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
                )
                try:
                    temporary.write_text(encoded, encoding="utf-8")
                    os.replace(temporary, self.state_path)
                    self._state_write_failures = 0
                    return True
                except OSError as exc:
                    last_error = exc
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                    if attempt < STATE_WRITE_ATTEMPTS:
                        time.sleep(STATE_WRITE_RETRY_SECONDS * attempt)

            self._report_state_write_failure(last_error or OSError("unknown state write failure"))
            return False

    def _report_state_write_failure(self, error: BaseException) -> None:
        self._state_write_failures += 1
        if self._state_write_failures == 1 or self._state_write_failures % 10 == 0:
            try:
                print(
                    f"[网页看板] 状态同步暂时失败（已重试 {STATE_WRITE_ATTEMPTS} 次），"
                    f"任务继续运行：{type(error).__name__}: {error}",
                    flush=True,
                )
            except Exception:
                pass


@contextlib.contextmanager
def monitor_runtime(output_dir: Path, task_name: str, phase: str) -> Iterator[MonitorRuntimeSession]:
    """Register a monitor even when the entrypoint is launched from an IDE."""
    global _ACTIVE_SESSION
    with _SESSION_LOCK:
        session = _ACTIVE_SESSION
        owns_session = session is None
        if owns_session:
            session = MonitorRuntimeSession(output_dir, task_name, phase)
            _ACTIVE_SESSION = session
            session.start()
            previous_phase = phase
        else:
            previous_phase = session.set_phase(phase)

    try:
        yield session
    except BaseException as exc:
        if owns_session:
            session.finish(error=exc)
        raise
    else:
        if owns_session:
            session.finish()
    finally:
        with _SESSION_LOCK:
            if owns_session:
                _ACTIVE_SESSION = None
            elif _ACTIVE_SESSION is session:
                session.set_phase(previous_phase)


def read_monitor_tasks(base_dir: Path | str) -> dict[str, Any]:
    root = Path(base_dir).resolve()
    runtime_dir = root / "outputs" / RUNTIME_DIRECTORY_NAME
    tasks: list[dict[str, Any]] = []
    if runtime_dir.is_dir():
        state_paths = sorted(runtime_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for state_path in state_paths[: MAX_TASKS * 3]:
            task = _read_task_state(runtime_dir, state_path)
            if task is not None:
                tasks.append(task)

    tasks.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    running = [task for task in tasks if task["status"] == "running"]
    recent = [task for task in tasks if task["status"] != "running"]
    visible = (running + recent)[:MAX_TASKS]
    return {
        "ok": True,
        "generated_at": _utc_now(),
        "active_count": len(running),
        "tasks": visible,
    }


def _read_task_state(runtime_dir: Path, state_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    instance_id = str(payload.get("instance_id") or "")
    if not instance_id or state_path.stem != instance_id:
        return None
    log_name = str(payload.get("log_file") or "")
    if Path(log_name).name != log_name or not log_name.endswith(".log"):
        return None
    pid = _safe_int(payload.get("pid"))
    heartbeat_age = _seconds_since(payload.get("heartbeat_at"))
    reported_status = str(payload.get("status") or "finished")
    process_running = pid > 0 and _pid_is_running(pid)
    is_live = reported_status == "running" and process_running and heartbeat_age <= STALE_AFTER_SECONDS
    if is_live:
        status = "running"
    elif reported_status == "failed":
        status = "failed"
    else:
        status = "stopped"
    log_text, truncated = _tail_text(runtime_dir / log_name)
    return {
        "instance_id": instance_id,
        "pid": pid,
        "task_name": str(payload.get("task_name") or "monitor"),
        "task_label": str(payload.get("task_label") or "本地盯盘"),
        "phase": str(payload.get("phase") or ""),
        "phase_label": str(payload.get("phase_label") or "运行中"),
        "status": status,
        "started_at": payload.get("started_at"),
        "heartbeat_at": payload.get("heartbeat_at"),
        "ended_at": payload.get("ended_at"),
        "source": str(payload.get("source") or "Python"),
        "command": str(payload.get("command") or "python"),
        "error": payload.get("error"),
        "heartbeat_age_seconds": round(heartbeat_age, 1),
        "log": log_text,
        "log_truncated": truncated,
        "events": _extract_runtime_events(log_text),
    }


def _extract_runtime_events(log_text: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for line_number, raw_line in enumerate(log_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        classified = _classify_runtime_line(line)
        if classified is None:
            continue
        symbol_match = _SYMBOL_PATTERN.search(line)
        symbol = symbol_match.group(0).upper() if symbol_match else ""
        message = _LEADING_CONTEXT_PATTERN.sub("", line).strip()
        key = f"{symbol}|{classified['kind']}|{classified['title']}"
        existing = grouped.get(key)
        if existing is None:
            clock_match = _CLOCK_PATTERN.search(line)
            grouped[key] = {
                "id": uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:12],
                "symbol": symbol,
                "severity": classified["severity"],
                "kind": classified["kind"],
                "title": classified["title"],
                "message": message,
                "action": classified["action"],
                "time_label": clock_match.group(1) if clock_match else "最近",
                "line_number": line_number,
                "count": 1,
            }
        else:
            existing["count"] += 1
            existing["line_number"] = line_number
            existing["message"] = message
    return sorted(grouped.values(), key=lambda event: int(event["line_number"]), reverse=True)[:MAX_EVENTS]


def _classify_runtime_line(line: str) -> dict[str, str] | None:
    upper = line.upper()
    if "开始生成 WATCH_CODES" in upper:
        return _event_definition("warning", "generation", "开始生成 WatchCode", "等待生成完成")
    if "日线读取完成" in line:
        return _event_definition("success", "generation_progress", "WatchCode 日线读取完成", "等待筛选候选")
    if "日线读取进度" in line:
        return _event_definition("info", "generation_progress", "WatchCode 日线读取进度", "查看最新批次")
    if "生成完成" in line and "候选" in line:
        return _event_definition("success", "generation", "WatchCode 生成完成", "可以启动盯盘")
    if "状态同步暂时失败" in line:
        return _event_definition("warning", "runtime_sync", "网页状态同步正在重试", "任务仍在本地运行")
    if "[网页看板]" in line or "任务启动" in line or "开始监控" in line:
        return _event_definition("info", "lifecycle", "盯盘任务已启动", "确认运行范围")
    if any(keyword in upper for keyword in ("REJECTED", "ERROR", "EXCEPTION", "TRACEBACK")) or any(
        keyword in line for keyword in ("拒绝", "异常", "错误", "失败", "断开", "缺失")
    ):
        return _event_definition("critical", "error", "运行或订单异常", "立即核对")
    if any(keyword in upper for keyword in ("CANCELLED", "CANCELED")) or any(keyword in line for keyword in ("取消", "撤单")):
        return _event_definition("critical", "order", "订单已取消", "查看券商订单")
    if "FILLED" in upper or any(keyword in line for keyword in ("买入成交", "卖出成交", "已成交")):
        return _event_definition("critical", "order", "订单成交", "核对成交记录")
    if any(keyword in line for keyword in ("进入买点", "买点区间", "触发买入", "满足买入", "达到触发")):
        return _event_definition("warning", "trigger", "进入买入触发区", "检查订单资格")
    if any(keyword in line for keyword in ("接近", "门槛", "触发上沿", "触发下沿", "止损", "止盈")):
        return _event_definition("warning", "threshold", "接近或越过关键阈值", "继续重点观察")
    if _SYMBOL_PATTERN.search(line) and any(keyword in line for keyword in ("继续监控", "继续观察", "当前价")):
        return _event_definition("info", "observation", "持续观察", "无需操作")
    return None


def _event_definition(severity: str, kind: str, title: str, action: str) -> dict[str, str]:
    return {"severity": severity, "kind": kind, "title": title, "action": action}


def _tail_text(path: Path) -> tuple[str, bool]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_LOG_BYTES:
                handle.seek(-MAX_LOG_BYTES, os.SEEK_END)
            raw = handle.read(MAX_LOG_BYTES)
    except OSError:
        return "", False
    text = raw.decode("utf-8", errors="replace")
    if size > MAX_LOG_BYTES:
        text = text.split("\n", 1)[-1]
    lines = text.splitlines()
    line_truncated = len(lines) > MAX_LOG_LINES
    if line_truncated:
        lines = lines[-MAX_LOG_LINES:]
    return "\n".join(lines), size > MAX_LOG_BYTES or line_truncated


def _pid_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _runtime_source() -> str:
    if os.environ.get("MA5_DASHBOARD_ACTION") == "1":
        return "网页看板"
    if os.environ.get("PYCHARM_HOSTED") or "pydevd" in sys.modules:
        return "PyCharm / IDE"
    if os.environ.get("VSCODE_PID"):
        return "VS Code / IDE"
    return "Python 命令行"


def _seconds_since(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
