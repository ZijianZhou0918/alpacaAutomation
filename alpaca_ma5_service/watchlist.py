from __future__ import annotations

import csv
from pathlib import Path


def normalize_symbol(raw: str) -> str:
    """把用户输入的股票代码标准化成内部使用的 US.AAPL 形式。"""
    symbol = str(raw).strip().upper()
    if not symbol:
        return ""
    if "." not in symbol and "-" not in symbol:
        symbol = f"US.{symbol}"
    return symbol


def read_watch_codes(path: Path) -> list[str]:
    """读取唯一盯盘文件；支持 txt/csv，返回去重后的标准代码。"""
    if not path.exists():
        return []
    if path.suffix.lower() == ".csv":
        return _read_csv_watch_codes(path)
    return _read_text_watch_codes(path)


def _read_text_watch_codes(path: Path) -> list[str]:
    """读取一行一个代码的文本 watchlist，忽略空行和 # 注释。"""
    seen: set[str] = set()
    out: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        symbol = normalize_symbol(value.split(",", 1)[0])
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def _read_csv_watch_codes(path: Path) -> list[str]:
    """读取 csv watchlist，优先使用 code 列，否则使用第一列。"""
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    field = "code" if "code" in rows[0] else next(iter(rows[0]), "")
    return [normalize_symbol(row.get(field, "")) for row in rows if normalize_symbol(row.get(field, ""))]


def to_alpaca_symbol(symbol: str) -> str:
    """把内部 US.AAPL 形式转换成 Alpaca API 使用的 AAPL。"""
    symbol = normalize_symbol(symbol)
    if symbol.startswith("US."):
        return symbol[3:]
    return symbol
