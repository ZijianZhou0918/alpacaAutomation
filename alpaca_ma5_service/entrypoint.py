from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def ensure_local_venv() -> None:
    """点箭头运行时，如果解释器不对，就自动切回项目 .venv。"""
    project_dir = Path(__file__).resolve().parent.parent
    venv_dir = project_dir / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    if not venv_python.exists():
        return
    if same_path(Path(sys.prefix), venv_dir) or same_path(Path(sys.executable), venv_python):
        return
    script = Path(sys.argv[0]).resolve()

    # Windows/PyCharm 下用 subprocess 重启，控制台输出更稳定。
    result = subprocess.run([str(venv_python), str(script), *sys.argv[1:]], cwd=str(project_dir))
    raise SystemExit(result.returncode)


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False
