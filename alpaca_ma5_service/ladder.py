from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from .models import OrderResult, Position, Signal, is_executed_order_status
from .pending_orders import PendingOrderEvent
from .watchlist import normalize_symbol


STATE_VERSION = 4
LADDER_PROFILE_NAME = "ma5_dip_ladder"
POSITION_RECONCILE_GRACE_SECONDS = 120


@dataclass
class LadderPlan:
    symbol: str
    session_date: str
    target_notional: float
    buy_anchor_price: float
    buy_offsets: tuple[float, float, float]
    sell_offsets: tuple[float, float, float]
    buy_leg_filled: list[bool] = field(default_factory=lambda: [False, False, False])
    filled_notional: float = 0.0
    filled_quantity: float = 0.0
    seen_second_tier: bool = False
    buy_closed: bool = False
    sell_anchor_price: float = 0.0
    take_profit_target_quantity: float = 0.0
    sell_leg_filled_quantity: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    sell_stage: int = 0
    status: str = "active"
    applied_order_filled_quantity: dict[str, float] = field(default_factory=dict)
    applied_order_filled_value: dict[str, float] = field(default_factory=dict)
    broker_stop_enabled: bool = False
    updated_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "LadderPlan":
        plan = cls(
            symbol=normalize_symbol(str(raw.get("symbol", ""))),
            session_date=str(raw.get("session_date", "")),
            target_notional=float(raw.get("target_notional", 0.0) or 0.0),
            buy_anchor_price=float(raw.get("buy_anchor_price", 0.0) or 0.0),
            buy_offsets=tuple(float(value) for value in raw.get("buy_offsets", (0.0, -0.01, -0.02))),
            sell_offsets=tuple(float(value) for value in raw.get("sell_offsets", (0.0, 0.01, 0.02))),
            buy_leg_filled=[bool(value) for value in raw.get("buy_leg_filled", [False, False, False])],
            filled_notional=float(raw.get("filled_notional", 0.0) or 0.0),
            filled_quantity=float(raw.get("filled_quantity", 0.0) or 0.0),
            seen_second_tier=bool(raw.get("seen_second_tier", False)),
            buy_closed=bool(raw.get("buy_closed", False)),
            sell_anchor_price=float(raw.get("sell_anchor_price", 0.0) or 0.0),
            take_profit_target_quantity=float(raw.get("take_profit_target_quantity", 0.0) or 0.0),
            sell_leg_filled_quantity=[
                float(value) for value in raw.get("sell_leg_filled_quantity", [0.0, 0.0, 0.0])
            ],
            sell_stage=int(raw.get("sell_stage", 0) or 0),
            status=str(raw.get("status", "active") or "active"),
            applied_order_filled_quantity={
                str(key): float(value)
                for key, value in dict(raw.get("applied_order_filled_quantity", {}) or {}).items()
            },
            applied_order_filled_value={
                str(key): float(value)
                for key, value in dict(raw.get("applied_order_filled_value", {}) or {}).items()
            },
            broker_stop_enabled=bool(raw.get("broker_stop_enabled", False)),
            updated_at=str(raw.get("updated_at", "")),
        )
        validate_plan(plan)
        return plan

    def to_dict(self) -> dict[str, object]:
        raw = asdict(self)
        raw["buy_offsets"] = list(self.buy_offsets)
        raw["sell_offsets"] = list(self.sell_offsets)
        return raw


@dataclass(frozen=True)
class LadderBuyInstruction:
    limit_price: float
    notional_usd: float
    action: str
    reason: str
    counts_daily_slot: bool


@dataclass(frozen=True)
class LadderSellInstruction:
    quantity: float
    action: str
    reason: str

    def to_signal(self, symbol: str, current_price: float) -> Signal:
        full_exit_actions = {"absolute_stop_market", "close_liquidation", "closing_retry_market"}
        return Signal(
            symbol,
            "SELL_ALL" if self.action in full_exit_actions else "SELL_HALF",
            self.reason,
            current_price,
            self.quantity,
            diagnostics={"sell_rule": self.action},
        )


class LadderStateStore:
    def __init__(self, output_dir: Path):
        self.path = output_dir / "ladder_state.json"
        self.plans = self._load()

    def get(self, symbol: str) -> LadderPlan | None:
        return self.plans.get(normalize_symbol(symbol))

    def create(
        self,
        symbol: str,
        session_date: date,
        target_notional: float,
        anchor_price: float,
        buy_offsets: tuple[float, float, float],
        sell_offsets: tuple[float, float, float],
        now: datetime,
    ) -> LadderPlan:
        normalized = normalize_symbol(symbol)
        existing = self.plans.get(normalized)
        if existing is not None and existing.status != "closed":
            return existing
        if existing is not None and existing.session_date == session_date.isoformat():
            return existing
        plan = LadderPlan(
            symbol=normalized,
            session_date=session_date.isoformat(),
            target_notional=round(float(target_notional), 2),
            buy_anchor_price=round(float(anchor_price), 4),
            buy_offsets=buy_offsets,
            sell_offsets=sell_offsets,
            updated_at=now.isoformat(),
        )
        validate_plan(plan)
        self.plans[normalized] = plan
        self.save(now)
        return plan

    def save(self, now: datetime) -> None:
        for plan in self.plans.values():
            validate_plan(plan)
        payload = {
            "version": STATE_VERSION,
            "updated_at": now.isoformat(),
            "plans": {symbol: plan.to_dict() for symbol, plan in sorted(self.plans.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def _load(self) -> dict[str, LadderPlan]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取分档状态 {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("分档状态根节点必须是对象")
        try:
            version = int(raw.get("version", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"分档状态版本无效：{raw.get('version')}") from exc
        if version not in {2, 3, STATE_VERSION}:
            raise RuntimeError(f"不支持的分档状态版本：{raw.get('version')}")
        plans = raw.get("plans", {})
        if not isinstance(plans, dict):
            raise RuntimeError("分档状态 plans 必须是对象")
        loaded: dict[str, LadderPlan] = {}
        try:
            for symbol, value in plans.items():
                if not isinstance(value, dict):
                    raise ValueError(f"plan {symbol} must be an object")
                normalized = normalize_symbol(str(symbol))
                plan = LadderPlan.from_dict(value)
                if normalized != plan.symbol:
                    raise ValueError(f"plan key {normalized} does not match symbol {plan.symbol}")
                loaded[normalized] = plan
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"分档状态内容无效：{exc}") from exc
        return loaded


def validate_plan(plan: LadderPlan) -> None:
    if not plan.symbol:
        raise ValueError("ladder plan symbol is required")
    date.fromisoformat(plan.session_date)
    if not math.isfinite(plan.target_notional) or plan.target_notional <= 0:
        raise ValueError("ladder target_notional must be positive")
    if not math.isfinite(plan.buy_anchor_price) or plan.buy_anchor_price <= 0:
        raise ValueError("ladder buy_anchor_price must be positive")
    if len(plan.buy_offsets) != 3 or len(plan.sell_offsets) != 3:
        raise ValueError("ladder plans require three buy and sell offsets")
    if any(not math.isfinite(value) or not -1.0 < value <= 0.0 for value in plan.buy_offsets):
        raise ValueError("ladder buy offsets must be finite and greater than -1")
    if not (plan.buy_offsets[0] == 0.0 and plan.buy_offsets[0] > plan.buy_offsets[1] > plan.buy_offsets[2]):
        raise ValueError("ladder buy offsets must start at 0 and strictly descend")
    if any(not math.isfinite(value) or value < 0.0 for value in plan.sell_offsets):
        raise ValueError("ladder sell offsets must be finite and non-negative")
    if not (plan.sell_offsets[0] == 0.0 and plan.sell_offsets[0] < plan.sell_offsets[1] < plan.sell_offsets[2]):
        raise ValueError("ladder sell offsets must start at 0 and strictly ascend")
    if len(plan.buy_leg_filled) != 3:
        raise ValueError("ladder buy_leg_filled must have three entries")
    if not math.isfinite(plan.filled_notional) or plan.filled_notional < 0:
        raise ValueError("ladder filled_notional must be finite and non-negative")
    if not math.isfinite(plan.filled_quantity) or plan.filled_quantity < 0:
        raise ValueError("ladder filled_quantity must be finite and non-negative")
    if not math.isfinite(plan.sell_anchor_price) or plan.sell_anchor_price < 0:
        raise ValueError("ladder sell_anchor_price must be finite and non-negative")
    if not math.isfinite(plan.take_profit_target_quantity) or plan.take_profit_target_quantity < 0:
        raise ValueError("ladder take_profit_target_quantity must be finite and non-negative")
    if len(plan.sell_leg_filled_quantity) != 3:
        raise ValueError("ladder sell_leg_filled_quantity must have three entries")
    if any(not math.isfinite(value) or value < 0 for value in plan.sell_leg_filled_quantity):
        raise ValueError("ladder sell_leg_filled_quantity must be finite and non-negative")
    if sum(plan.sell_leg_filled_quantity) > plan.take_profit_target_quantity + 1e-9:
        raise ValueError("ladder take-profit fills cannot exceed the first-half target")
    if plan.sell_stage not in {0, 1, 2, 3}:
        raise ValueError("ladder sell_stage must be 0..3")
    if plan.status not in {"active", "closing", "closed", "stopped"}:
        raise ValueError(f"invalid ladder status: {plan.status}")
    if set(plan.applied_order_filled_quantity) != set(plan.applied_order_filled_value):
        raise ValueError("ladder applied order fill maps must use identical order ids")
    for order_id, quantity in plan.applied_order_filled_quantity.items():
        value = plan.applied_order_filled_value.get(order_id, -1.0)
        if not order_id or not math.isfinite(quantity) or quantity < 0:
            raise ValueError("ladder applied order quantities must use non-empty ids and non-negative finite values")
        if not math.isfinite(value) or value < 0:
            raise ValueError("ladder applied order values must be finite and non-negative")
    datetime.fromisoformat(plan.updated_at)


def is_ladder_profile(settings: object) -> bool:
    return str(getattr(settings, "strategy_profile_name", "")) == LADDER_PROFILE_NAME


def count_today_started_plans(store: LadderStateStore, target_date: date) -> int:
    day = target_date.isoformat()
    return sum(plan.session_date == day and plan.filled_quantity > 0 for plan in store.plans.values())


def reconcile_plan(plan: LadderPlan, position: Position | None, now: datetime) -> bool:
    changed = False
    if position is None:
        should_close = plan.status in {"closing", "stopped"}
        if plan.status == "active" and plan.filled_quantity > 0:
            try:
                updated_at = datetime.fromisoformat(plan.updated_at)
                elapsed_seconds = (now - updated_at).total_seconds()
            except (TypeError, ValueError):
                elapsed_seconds = POSITION_RECONCILE_GRACE_SECONDS
            should_close = elapsed_seconds >= POSITION_RECONCILE_GRACE_SECONDS
        if plan.filled_quantity > 0 and should_close:
            plan.status = "closed"
            plan.sell_stage = 3
            changed = True
    else:
        if plan.filled_quantity <= 0:
            plan.filled_quantity = float(position.quantity)
            plan.filled_notional = float(position.quantity) * float(position.avg_price)
            plan.buy_leg_filled[0] = True
            changed = True
    if changed:
        plan.updated_at = now.isoformat()
    return changed


def close_expired_buy_window(plan: LadderPlan, now: datetime) -> bool:
    if plan.buy_closed:
        return False
    if now.date().isoformat() != plan.session_date or now.time() >= time(12, 0):
        plan.buy_closed = True
        plan.updated_at = now.isoformat()
        return True
    return False


def next_buy_instruction(plan: LadderPlan, current_price: float) -> LadderBuyInstruction | None:
    if plan.status != "active" or plan.buy_closed or current_price <= 0:
        return None
    second_price = plan.buy_anchor_price * (1.0 + plan.buy_offsets[1])
    if current_price <= second_price + 1e-9:
        plan.seen_second_tier = True
    remaining = max(0.0, plan.target_notional - plan.filled_notional)
    if remaining <= 0.01:
        plan.buy_closed = True
        return None

    if plan.seen_second_tier and plan.filled_quantity > 0 and current_price >= plan.buy_anchor_price - 1e-9:
        return LadderBuyInstruction(
            limit_price=plan.buy_anchor_price,
            notional_usd=remaining,
            action="buy_recovery_anchor",
            reason="低档未全部成交后价格回到首档，按首档价补足剩余预算",
            counts_daily_slot=plan.filled_quantity <= 0,
        )

    for index, filled in enumerate(plan.buy_leg_filled):
        if filled:
            continue
        target_price = plan.buy_anchor_price * (1.0 + plan.buy_offsets[index])
        if current_price > target_price + 1e-9:
            return None
        cumulative_target = plan.target_notional * (index + 1) / 3.0
        notional = remaining if index == 2 else min(remaining, max(0.0, cumulative_target - plan.filled_notional))
        if notional + 1e-9 < target_price:
            plan.buy_leg_filled[index] = True
            if all(plan.buy_leg_filled):
                plan.buy_closed = True
            return None
        return LadderBuyInstruction(
            limit_price=target_price,
            notional_usd=notional,
            action=f"buy_leg_{index}",
            reason=f"三档买入第 {index + 1} 档，锚点偏移 {plan.buy_offsets[index]:+.2%}",
            counts_daily_slot=plan.filled_quantity <= 0,
        )
    plan.buy_closed = True
    return None


def record_buy_result(plan: LadderPlan, instruction: LadderBuyInstruction, result: OrderResult, now: datetime) -> bool:
    if not is_executed_order_status(result.status):
        return False
    quantity = max(0.0, float(result.quantity))
    price = max(0.0, float(result.price))
    if quantity <= 0 or price <= 0:
        raise RuntimeError("成交买单缺少有效数量或价格，分档状态停止推进")
    plan.filled_quantity += quantity
    plan.filled_notional += quantity * price
    plan.broker_stop_enabled = True
    expected_quantity = math.floor(instruction.notional_usd / instruction.limit_price + 1e-9)
    instruction_completed = quantity + 1e-9 >= expected_quantity
    if instruction.action.startswith("buy_leg_"):
        index = int(instruction.action.rsplit("_", 1)[1])
        if instruction_completed:
            plan.buy_leg_filled[index] = True
    elif instruction.action == "buy_recovery_anchor":
        if instruction_completed:
            plan.buy_leg_filled = [True, True, True]
    remaining = max(0.0, plan.target_notional - plan.filled_notional)
    if all(plan.buy_leg_filled) or remaining < min(plan.buy_anchor_price * (1.0 + value) for value in plan.buy_offsets):
        plan.buy_closed = True
    plan.updated_at = now.isoformat()
    return True


def prepare_sell_anchor(
    plan: LadderPlan,
    position: Position,
    current_price: float,
    now: datetime,
    *,
    take_profit_half_pct: float,
    take_profit_sell_fraction: float,
) -> bool:
    if not plan.buy_closed or plan.sell_anchor_price > 0:
        return False
    if position.avg_price <= 0 or current_price / position.avg_price - 1.0 < take_profit_half_pct - 1e-9:
        return False
    plan.sell_anchor_price = round(float(current_price), 4)
    position_integer_quantity = int(math.floor(max(0.0, float(position.quantity)) + 1e-9))
    plan.take_profit_target_quantity = float(
        math.floor(position_integer_quantity * min(1.0, max(0.0, take_profit_sell_fraction)) + 1e-9)
    )
    if plan.take_profit_target_quantity <= 0:
        plan.sell_stage = 3
    plan.updated_at = now.isoformat()
    return True


def next_sell_instruction(
    plan: LadderPlan,
    position: Position,
    current_price: float,
    now: datetime,
    *,
    absolute_stop_loss_pct: float,
    take_profit_half_pct: float,
    take_profit_sell_fraction: float,
    close_start: time,
    close_end: time,
) -> LadderSellInstruction | None:
    if current_price <= 0 or position.quantity <= 0:
        return None
    gain_pct = current_price / position.avg_price - 1.0 if position.avg_price > 0 else 0.0
    if gain_pct <= absolute_stop_loss_pct + 1e-9:
        return LadderSellInstruction(
            float(position.quantity),
            "absolute_stop_market",
            f"相对实际加权成本亏损达到 {abs(absolute_stop_loss_pct):.2%}，MARKET 全部止损",
        )
    if close_start <= now.time() <= close_end:
        return LadderSellInstruction(float(position.quantity), "close_liquidation", "15:55 ET 尾盘 MARKET 清仓")
    if plan.status in {"closing", "stopped"}:
        return LadderSellInstruction(
            float(position.quantity),
            "closing_retry_market",
            "上一笔清仓仅部分成交，继续 MARKET 卖出券商剩余持仓",
        )
    if not plan.buy_closed or plan.status != "active":
        return None
    if plan.sell_anchor_price <= 0:
        prepare_sell_anchor(
            plan,
            position,
            current_price,
            now,
            take_profit_half_pct=take_profit_half_pct,
            take_profit_sell_fraction=take_profit_sell_fraction,
        )
    if plan.sell_anchor_price <= 0 or plan.take_profit_target_quantity <= 0:
        return None

    if plan.sell_stage >= 2 and current_price <= plan.sell_anchor_price + 1e-9:
        sold_quantity = sum(plan.sell_leg_filled_quantity)
        remaining_take_profit_quantity = max(0.0, plan.take_profit_target_quantity - sold_quantity)
        quantity = min(float(position.quantity), remaining_take_profit_quantity)
        if quantity <= 0:
            plan.sell_stage = 3
            return None
        return LadderSellInstruction(
            quantity,
            "take_profit_fallback",
            "50% 止盈额度的前两档已卖、第三档未成，回到止盈锚点时只卖该额度余量",
        )
    if plan.sell_stage >= 3:
        return None
    target_price = plan.sell_anchor_price * (1.0 + plan.sell_offsets[plan.sell_stage])
    if current_price + 1e-9 < target_price:
        return None
    quantities = split_integer_quantity(plan.take_profit_target_quantity)
    target_quantity = quantities[plan.sell_stage]
    remaining_leg_quantity = max(0.0, target_quantity - plan.sell_leg_filled_quantity[plan.sell_stage])
    quantity = min(float(position.quantity), remaining_leg_quantity)
    if quantity <= 0:
        plan.sell_stage += 1
        return None
    return LadderSellInstruction(
        quantity,
        f"sell_leg_{plan.sell_stage}",
        (
            f"首次 50% 止盈额度第 {plan.sell_stage + 1} 档，"
            f"相对止盈锚点偏移 {plan.sell_offsets[plan.sell_stage]:+.2%}"
        ),
    )


def split_integer_quantity(quantity: float) -> tuple[float, float, float]:
    integer_quantity = int(math.floor(max(0.0, quantity) + 1e-9))
    base, remainder = divmod(integer_quantity, 3)
    return (
        float(base + (1 if remainder >= 1 else 0)),
        float(base + (1 if remainder >= 2 else 0)),
        float(base),
    )


def record_sell_result(plan: LadderPlan, instruction: LadderSellInstruction, result: OrderResult, now: datetime) -> bool:
    if not is_executed_order_status(result.status):
        return False
    if instruction.action.startswith("sell_leg_"):
        index = int(instruction.action.rsplit("_", 1)[1])
        plan.sell_leg_filled_quantity[index] += max(0.0, float(result.quantity))
        target_quantity = split_integer_quantity(plan.take_profit_target_quantity)[index]
        if plan.sell_leg_filled_quantity[index] + 1e-9 >= target_quantity:
            plan.sell_stage = max(plan.sell_stage, index + 1)
    elif instruction.action == "take_profit_fallback":
        plan.sell_leg_filled_quantity[2] += max(0.0, float(result.quantity))
        if sum(plan.sell_leg_filled_quantity) + 1e-9 >= plan.take_profit_target_quantity:
            plan.sell_stage = 3
    else:
        plan.status = "stopped" if instruction.action == "absolute_stop_market" else "closing"
        plan.sell_stage = 3
    plan.updated_at = now.isoformat()
    return True


def apply_pending_order_event(plan: LadderPlan, event: PendingOrderEvent, now: datetime) -> bool:
    """Apply cumulative broker fills exactly once, including fills that arrive after cancel."""

    if normalize_symbol(event.symbol) != plan.symbol:
        raise RuntimeError(
            f"待确认订单 {event.tracking_order_id} 股票 {event.symbol} 与分档计划 {plan.symbol} 不一致"
        )
    order_id = str(event.tracking_order_id or "")
    if not order_id:
        raise RuntimeError("待确认订单缺少 tracking_order_id，分档状态停止推进")

    cumulative_quantity = max(0.0, float(event.filled_quantity))
    fill_price = float(event.filled_avg_price or event.requested_price)
    if cumulative_quantity > 0 and fill_price <= 0:
        raise RuntimeError(f"待确认订单 {order_id} 成交价格无效，分档状态停止推进")
    cumulative_value = cumulative_quantity * max(0.0, fill_price)
    previous_quantity = float(plan.applied_order_filled_quantity.get(order_id, 0.0))
    previous_value = float(plan.applied_order_filled_value.get(order_id, 0.0))
    if cumulative_quantity + 1e-9 < previous_quantity:
        raise RuntimeError(
            f"待确认订单 {order_id} 累计成交量倒退：{cumulative_quantity} < {previous_quantity}"
        )

    delta_quantity = max(0.0, cumulative_quantity - previous_quantity)
    delta_value = cumulative_value - previous_value
    if delta_quantity <= 1e-9 and abs(delta_value) <= 1e-9:
        return False

    plan.applied_order_filled_quantity[order_id] = cumulative_quantity
    plan.applied_order_filled_value[order_id] = cumulative_value

    action = str(event.strategy_action or "")
    order_complete = cumulative_quantity + 1e-9 >= float(event.requested_quantity)
    if event.side.upper() == "BUY":
        plan.filled_quantity += delta_quantity
        plan.filled_notional = max(0.0, plan.filled_notional + delta_value)
        if delta_quantity > 0:
            plan.broker_stop_enabled = True
        if action.startswith("buy_leg_"):
            index = int(action.rsplit("_", 1)[1])
            if order_complete:
                plan.buy_leg_filled[index] = True
        elif action == "buy_recovery_anchor" and order_complete:
            plan.buy_leg_filled = [True, True, True]
        remaining = max(0.0, plan.target_notional - plan.filled_notional)
        if all(plan.buy_leg_filled) or remaining < min(
            plan.buy_anchor_price * (1.0 + value) for value in plan.buy_offsets
        ):
            plan.buy_closed = True
    elif event.side.upper() == "SELL":
        if action.startswith("sell_leg_"):
            index = int(action.rsplit("_", 1)[1])
            plan.sell_leg_filled_quantity[index] += delta_quantity
            target_quantity = split_integer_quantity(plan.take_profit_target_quantity)[index]
            if plan.sell_leg_filled_quantity[index] + 1e-9 >= target_quantity:
                plan.sell_stage = max(plan.sell_stage, index + 1)
        elif action == "take_profit_fallback":
            plan.sell_leg_filled_quantity[2] += delta_quantity
            if sum(plan.sell_leg_filled_quantity) + 1e-9 >= plan.take_profit_target_quantity:
                plan.sell_stage = 3
        elif delta_quantity > 0:
            plan.status = "stopped" if action in {"absolute_stop_market", "broker_protective_stop"} else "closing"
            plan.sell_stage = 3
    else:
        raise RuntimeError(f"待确认订单 {order_id} 方向无效：{event.side}")

    plan.updated_at = now.isoformat()
    validate_plan(plan)
    return True
