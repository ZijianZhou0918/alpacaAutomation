from __future__ import annotations

import os
from pathlib import Path


class RunLock:
    """Small cross-platform file lock used by click-run monitor scripts."""

    def __init__(self, path: Path, lock_file, backend: str):
        self.path = path
        self.lock_file = lock_file
        self.backend = backend

    def close(self) -> None:
        if self.lock_file.closed:
            return
        try:
            self.lock_file.seek(0)
            if self.backend == "windows":
                import msvcrt

                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock_file, fcntl.LOCK_UN)
        finally:
            self.lock_file.close()


def acquire_run_lock(output_dir: Path, filename: str, label: str) -> RunLock:
    """Allow only one copy of a monitor script for a given output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / filename
    lock_file = lock_path.open("a+", encoding="utf-8")
    backend = "windows" if os.name == "nt" else "posix"
    try:
        lock_file.seek(0)
        if backend == "windows":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        lock_file.close()
        raise RuntimeError(f"{label}已经在运行：{lock_path}") from exc

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()}\n")
    lock_file.flush()
    return RunLock(lock_path, lock_file, backend)
