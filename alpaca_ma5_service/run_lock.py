from __future__ import annotations

import os
from pathlib import Path


class RunLock:
    """Atomic file lock used by click-run and scheduled monitor scripts."""

    def __init__(self, path: Path, lock_file, pid: int):
        self.path = path
        self.lock_file = lock_file
        self.pid = pid

    def close(self) -> None:
        if not self.lock_file.closed:
            self.lock_file.close()
        try:
            if read_lock_pid(self.path) == self.pid:
                self.path.unlink()
        except OSError:
            pass


def acquire_run_lock(output_dir: Path, filename: str, label: str) -> RunLock:
    """Allow only one copy of a monitor script for a given output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / filename
    pid = os.getpid()
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing_pid = read_lock_pid(lock_path)
            if existing_pid is None or not pid_is_running(existing_pid):
                remove_stale_lock(lock_path)
                continue
            raise RuntimeError(f"{label}已经在运行：{lock_path} pid={existing_pid}") from exc
        lock_file = os.fdopen(fd, "w", encoding="utf-8")
        lock_file.write(f"pid={pid}\n")
        lock_file.flush()
        return RunLock(lock_path, lock_file, pid)
    raise RuntimeError(f"{label}锁文件被占用，无法创建：{lock_path}")


def read_lock_pid(path: Path) -> int | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("pid="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def remove_stale_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def windows_pid_is_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
