from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """读取简单 KEY=VALUE 格式的 .env 文件，不存在时返回空字典。"""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, raw = value.split("=", 1)
        out[key.strip()] = raw.strip().strip('"').strip("'")
    return out
