from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def ensure_local_venv() -> None:
    """点箭头时如果 PyCharm 用错解释器，自动切回项目 .venv。"""
    project_dir = Path(__file__).resolve().parent
    venv_python = project_dir / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        return
    if Path(sys.executable).resolve() == venv_python.resolve():
        return
    script = Path(sys.argv[0]).resolve()

    # PyCharm on Windows may hide output after os.execv; subprocess keeps the console visible.
    result = subprocess.run([str(venv_python), str(script), *sys.argv[1:]], cwd=str(project_dir))
    raise SystemExit(result.returncode)
