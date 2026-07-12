from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .models import OrderResult, Position, consumes_daily_buy_slot, is_executed_order_status, is_order_error_status
from .watchlist import normalize_symbol


def load_positions(path: Path) -> dict[str, Position]:
    """读取 dry-run 本地持仓；文件不存在表示无持仓。"""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {symbol: Position(**payload) for symbol, payload in data.get("positions", {}).items()}


def save_positions(path: Path, positions: dict[str, Position]) -> None:
    """保存 dry-run 本地持仓，供下一轮继续使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"positions": {symbol: asdict(position) for symbol, position in positions.items()}}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def orders_file(output_dir: Path, day: date | None = None) -> Path:
    """返回指定交易日的订单记录 CSV 路径。"""
    day = day or datetime.now().date()
    return output_dir / f"orders_{day:%Y-%m-%d}.csv"


def daily_buy_exclusions_file(output_dir: Path, day: date | None = None) -> Path:
    """返回当天买入排除记录 CSV 路径。"""
    day = day or datetime.now().date()
    return output_dir / f"buy_exclusions_{day:%Y-%m-%d}.csv"


def append_order(output_dir: Path, result: OrderResult, reason: str, day: date | None = None, created_at: datetime | None = None) -> None:
    """把一次订单结果追加到 outputs/orders_YYYY-MM-DD.csv。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    created_at = created_at or datetime.now()
    path = orders_file(output_dir, day or created_at.date())
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
                "created_at": created_at.isoformat(timespec="seconds"),
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


def count_today_buy_orders(output_dir: Path, day: date | None = None) -> int:
    """统计当天实际买成的买单；拒单、撤单、未确认撤单都不占名额。"""
    path = orders_file(output_dir, day)
    if not path.exists():
        return 0
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return sum(
            1
            for row in csv.DictReader(f)
            if row.get("side") == "BUY" and consumes_daily_buy_slot(row.get("status", ""))
        )


def count_today_symbol_order_errors(output_dir: Path, symbol: str, day: date | None = None) -> int:
    """统计单股当天拒单次数，用于三次后停止继续买入。"""
    path = orders_file(output_dir, day)
    if not path.exists():
        return 0
    target = normalize_symbol(symbol)
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return sum(
            1
            for row in csv.DictReader(f)
            if normalize_symbol(row.get("symbol", "")) == target and is_order_error_status(row.get("status", ""))
        )


def is_symbol_daily_buy_excluded(output_dir: Path, symbol: str, day: date | None = None) -> bool:
    """判断某只股票当天是否已被买入规则排除。"""
    path = daily_buy_exclusions_file(output_dir, day)
    if not path.exists():
        return False
    target = normalize_symbol(symbol)
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return any(normalize_symbol(row.get("symbol", "")) == target for row in csv.DictReader(f))


def append_daily_buy_exclusion(
    output_dir: Path,
    symbol: str,
    reason: str,
    day: date | None = None,
    created_at: datetime | None = None,
) -> None:
    """记录某只股票当天不再考虑买入；重复记录会被跳过。"""
    created_at = created_at or datetime.now()
    day = day or created_at.date()
    if is_symbol_daily_buy_excluded(output_dir, symbol, day):
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    path = daily_buy_exclusions_file(output_dir, day)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["created_at", "symbol", "reason"])
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "created_at": created_at.isoformat(timespec="seconds"),
                "symbol": normalize_symbol(symbol),
                "reason": reason,
            }
        )


def count_today_symbol_take_profit_half_sells(output_dir: Path, symbol: str, day: date | None = None) -> int:
    """统计单股当天已成交的分批止盈卖单，避免每轮重复卖出。"""
    path = orders_file(output_dir, day)
    if not path.exists():
        return 0
    target = normalize_symbol(symbol)
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return sum(
            1
            for row in csv.DictReader(f)
            if normalize_symbol(row.get("symbol", "")) == target
            and row.get("side") == "SELL"
            and "止盈" in row.get("reason", "")
            and is_executed_order_status(row.get("status", ""))
        )
