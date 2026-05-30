from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .models import OrderResult, Position


def load_positions(path: Path) -> dict[str, Position]:
    """读取本地 dry-run 持仓状态；没有文件时表示无持仓。"""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {symbol: Position(**payload) for symbol, payload in data.get("positions", {}).items()}


def save_positions(path: Path, positions: dict[str, Position]) -> None:
    """保存本地 dry-run 持仓状态，供下一轮测试继续使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"positions": {symbol: asdict(position) for symbol, position in positions.items()}}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def orders_file(output_dir: Path, day: date | None = None) -> Path:
    """返回某一天的订单记录 CSV 路径。"""
    day = day or datetime.now().date()
    return output_dir / f"orders_{day:%Y-%m-%d}.csv"


def append_order(output_dir: Path, result: OrderResult, reason: str) -> None:
    """把每次提交/拒单结果追加到 outputs/orders_YYYY-MM-DD.csv。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = orders_file(output_dir)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["created_at", "order_id", "symbol", "side", "quantity", "price", "status", "message", "reason"],
        )
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "order_id": result.order_id,
                "symbol": result.symbol,
                "side": result.side,
                "quantity": result.quantity,
                "price": result.price,
                "status": result.status,
                "message": result.message,
                "reason": reason,
            }
        )


def count_today_buy_orders(output_dir: Path) -> int:
    """统计当天已记录的买入次数，用于执行每日买入上限。"""
    path = orders_file(output_dir)
    if not path.exists():
        return 0
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return sum(1 for row in csv.DictReader(f) if row.get("side") == "BUY")
