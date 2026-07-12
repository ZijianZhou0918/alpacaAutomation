from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from alpaca_ma5_service.entrypoint import ensure_local_venv
from alpaca_ma5_service.review_web import (
    DEFAULT_HOST,
    DEFAULT_PORT_END,
    DEFAULT_PORT_START,
    PROJECT_ROOT,
    fetch_review_health,
    find_running_review_server,
    review_dashboard_url,
    wait_for_review_server,
    write_review_server_state,
)


def launch_daily_review(
    *,
    base_dir: Path | str | None = None,
    host: str = DEFAULT_HOST,
    port_start: int = DEFAULT_PORT_START,
    port_end: int = DEFAULT_PORT_END,
    timeout_seconds: float = 12.0,
    open_browser: bool = True,
) -> str:
    """Reuse the right local service or start one hidden, then open its real URL."""
    root = Path(base_dir or PROJECT_ROOT).resolve()
    port = find_running_review_server(
        base_dir=root,
        host=host,
        port_start=port_start,
        port_end=port_end,
    )
    child = None
    stdout_path = root / "outputs" / "logs" / "daily_review_server.out.log"
    stderr_path = root / "outputs" / "logs" / "daily_review_server.err.log"

    if port is None:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "alpaca_ma5_service.review_web",
            "--base-dir",
            str(root),
            "--host",
            host,
            "--port-start",
            str(port_start),
            "--port-end",
            str(port_end),
        ]
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        try:
            child = subprocess.Popen(
                command,
                cwd=str(root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=creationflags,
                close_fds=True,
            )
        finally:
            # Windows keeps temp/worktree files busy if the parent retains these handles.
            stdout_handle.close()
            stderr_handle.close()

        port = wait_for_review_server(
            base_dir=root,
            host=host,
            port_start=port_start,
            port_end=port_end,
            timeout_seconds=timeout_seconds,
        )
        if port is None:
            exit_code = child.poll() if child is not None else None
            detail = f"; service exit code={exit_code}" if exit_code is not None else ""
            _stop_started_child(child)
            raise RuntimeError(
                "MA5 daily review service did not become ready"
                f"{detail}. Check {stdout_path} and {stderr_path}."
            )

    health = fetch_review_health(port, host=host, timeout=1.0) or {}
    url = review_dashboard_url(port, host=host)
    write_review_server_state(
        root,
        {
            "host": host,
            "port": port,
            "url": url,
            "pid": health.get("pid") or (child.pid if child is not None else None),
            "started_at": health.get("started_at"),
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "launcher_pid": os.getpid(),
        },
    )

    if open_browser:
        opened = webbrowser.open(url, new=2)
        if not opened and hasattr(os, "startfile"):
            os.startfile(url)  # type: ignore[attr-defined]

    print(f"MA5 每日复盘已打开：{url}", flush=True)
    return url


def _stop_started_child(child) -> None:
    """Stop only the service process created by this launcher after failed startup."""
    if child is None:
        return
    try:
        if child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2.0)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        # The process may have exited between poll/terminate, or Windows may
        # still be releasing the handle. The original readiness error is more
        # useful to the user than a cleanup error.
        return


def main() -> int:
    ensure_local_venv()
    try:
        launch_daily_review()
    except Exception as exc:
        print(f"无法打开 MA5 每日复盘：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
