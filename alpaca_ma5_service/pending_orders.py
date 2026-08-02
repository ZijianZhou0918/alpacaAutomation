from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .watchlist import normalize_symbol


STATE_VERSION = 1


@dataclass
class PendingOrder:
    """Persisted identity and strategy context for one broker order."""

    tracking_order_id: str
    active_order_id: str
    symbol: str
    side: str
    requested_quantity: float
    requested_price: float
    reason: str
    strategy_action: str
    strategy_notional: float
    submitted_at: str
    active_order_base_quantity: float = 0.0
    active_order_base_value: float = 0.0
    last_status: str = "SUBMITTED"
    last_filled_quantity: float = 0.0
    last_filled_avg_price: float = 0.0
    recorded_status: str = ""
    recorded_filled_quantity: float = -1.0
    cancel_requested_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "PendingOrder":
        order = cls(
            tracking_order_id=str(raw.get("tracking_order_id", "") or ""),
            active_order_id=str(raw.get("active_order_id", "") or ""),
            symbol=normalize_symbol(str(raw.get("symbol", "") or "")),
            side=str(raw.get("side", "") or "").upper(),
            requested_quantity=float(raw.get("requested_quantity", 0.0) or 0.0),
            requested_price=float(raw.get("requested_price", 0.0) or 0.0),
            reason=str(raw.get("reason", "") or ""),
            strategy_action=str(raw.get("strategy_action", "") or ""),
            strategy_notional=float(raw.get("strategy_notional", 0.0) or 0.0),
            submitted_at=str(raw.get("submitted_at", "") or ""),
            active_order_base_quantity=float(raw.get("active_order_base_quantity", 0.0) or 0.0),
            active_order_base_value=float(raw.get("active_order_base_value", 0.0) or 0.0),
            last_status=str(raw.get("last_status", "SUBMITTED") or "SUBMITTED").upper(),
            last_filled_quantity=float(raw.get("last_filled_quantity", 0.0) or 0.0),
            last_filled_avg_price=float(raw.get("last_filled_avg_price", 0.0) or 0.0),
            recorded_status=str(raw.get("recorded_status", "") or "").upper(),
            recorded_filled_quantity=float(raw.get("recorded_filled_quantity", -1.0)),
            cancel_requested_at=str(raw.get("cancel_requested_at", "") or ""),
            updated_at=str(raw.get("updated_at", "") or ""),
        )
        validate_pending_order(order)
        return order


@dataclass(frozen=True)
class PendingOrderEvent:
    tracking_order_id: str
    active_order_id: str
    symbol: str
    side: str
    requested_quantity: float
    requested_price: float
    reason: str
    strategy_action: str
    strategy_notional: float
    status: str
    filled_quantity: float
    filled_avg_price: float
    terminal: bool

    def record_key(self) -> tuple[str, float]:
        return self.status.upper(), round(float(self.filled_quantity), 9)


class PendingOrderStore:
    """Atomic, restart-safe storage for broker orders awaiting a final state."""

    def __init__(self, output_dir: Path):
        self.path = output_dir / "pending_orders.json"
        self.orders = self._load()

    def register(
        self,
        *,
        order_id: str,
        symbol: str,
        side: str,
        requested_quantity: float,
        requested_price: float,
        reason: str,
        strategy_action: str,
        strategy_notional: float,
        submitted_at: datetime,
        status: str,
    ) -> PendingOrder:
        tracking_order_id = str(order_id or "")
        if not tracking_order_id:
            raise ValueError("pending order id is required")
        existing = self.orders.get(tracking_order_id)
        if existing is not None:
            return existing
        order = PendingOrder(
            tracking_order_id=tracking_order_id,
            active_order_id=tracking_order_id,
            symbol=normalize_symbol(symbol),
            side=str(side).upper(),
            requested_quantity=float(requested_quantity),
            requested_price=float(requested_price),
            reason=str(reason or ""),
            strategy_action=str(strategy_action or ""),
            strategy_notional=float(strategy_notional or 0.0),
            submitted_at=submitted_at.isoformat(),
            last_status=str(status or "SUBMITTED").upper(),
            updated_at=submitted_at.isoformat(),
        )
        validate_pending_order(order)
        self.orders[tracking_order_id] = order
        self.save(submitted_at)
        return order

    def remove(self, tracking_order_id: str, now: datetime) -> None:
        if self.orders.pop(str(tracking_order_id), None) is not None:
            self.save(now)

    def save(self, now: datetime) -> None:
        for order in self.orders.values():
            validate_pending_order(order)
        payload = {
            "version": STATE_VERSION,
            "updated_at": now.isoformat(),
            "orders": {key: asdict(value) for key, value in sorted(self.orders.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def _load(self) -> dict[str, PendingOrder]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取待确认订单状态 {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("待确认订单状态根节点必须是对象")
        try:
            version = int(raw.get("version", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"待确认订单状态版本无效：{raw.get('version')}") from exc
        if version != STATE_VERSION:
            raise RuntimeError(f"不支持的待确认订单状态版本：{raw.get('version')}")
        values = raw.get("orders", {})
        if not isinstance(values, dict):
            raise RuntimeError("待确认订单状态 orders 必须是对象")
        loaded: dict[str, PendingOrder] = {}
        try:
            for key, value in values.items():
                if not isinstance(value, dict):
                    raise ValueError(f"pending order {key} must be an object")
                order = PendingOrder.from_dict(value)
                if str(key) != order.tracking_order_id:
                    raise ValueError(f"pending order key {key} does not match tracking id")
                loaded[str(key)] = order
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"待确认订单状态内容无效：{exc}") from exc
        return loaded


def validate_pending_order(order: PendingOrder) -> None:
    if not order.tracking_order_id or not order.active_order_id:
        raise ValueError("pending order ids are required")
    if not order.symbol:
        raise ValueError("pending order symbol is required")
    if order.side not in {"BUY", "SELL"}:
        raise ValueError("pending order side must be BUY or SELL")
    for name, value in (
        ("requested_quantity", order.requested_quantity),
        ("requested_price", order.requested_price),
        ("strategy_notional", order.strategy_notional),
        ("active_order_base_quantity", order.active_order_base_quantity),
        ("active_order_base_value", order.active_order_base_value),
        ("last_filled_quantity", order.last_filled_quantity),
        ("last_filled_avg_price", order.last_filled_avg_price),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"pending order {name} must be finite and non-negative")
    if order.requested_quantity <= 0 or order.requested_price <= 0:
        raise ValueError("pending order requested quantity and price must be positive")
    if not math.isfinite(order.recorded_filled_quantity) or order.recorded_filled_quantity < -1:
        raise ValueError("pending order recorded_filled_quantity must be finite and at least -1")
    datetime.fromisoformat(order.submitted_at)
    if order.cancel_requested_at:
        datetime.fromisoformat(order.cancel_requested_at)
    if order.updated_at:
        datetime.fromisoformat(order.updated_at)
