"""盘前只读持仓波动监控；不筛选股票、不生成候选、不提交订单。"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .alpaca_connection import build_trading_connection
from .config import Settings, build_settings
from .errors import short_error
from .market_data import SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION, build_market_data as build_default_market_data
from .market_time import is_premarket_monitor_finished, is_premarket_time, seconds_until_premarket_monitor_end
from .models import Position
from .openclaw_notify import safe_send_openclaw_messages
from .run_lock import acquire_run_lock
from .watchlist import normalize_symbol


PREMARKET_POSITION_MOVE_PCT = 0.03
PREMARKET_POSITION_WINDOW_SECONDS = 60
PREMARKET_POSITION_POLL_SECONDS = 10
PREMARKET_WAIT_POLL_SECONDS = 300


@dataclass(frozen=True)
class PositionPriceSample:
    price: float
    as_of: datetime
    price_source: str


@dataclass(frozen=True)
class PositionMovement:
    symbol: str
    direction: str
    change_pct: float
    anchor_price: float
    current_price: float
    anchor_as_of: datetime
    current_as_of: datetime
    price_source: str
    quantity: float
    avg_price: float
    leg_number: int
    continues_previous_direction: bool


class AlpacaPositionSource:
    """只读 Alpaca 持仓适配器；本类没有任何订单提交方法。"""

    def __init__(self):
        connection = build_trading_connection()
        self.client = connection.client
        self.paper = connection.paper

    def source_name(self) -> str:
        return "alpaca-paper" if self.paper else "alpaca-live"

    def get_positions(self) -> dict[str, Position]:
        positions: dict[str, Position] = {}
        for raw in self.client.get_all_positions():
            symbol = normalize_symbol(getattr(raw, "symbol", ""))
            quantity = _nonzero_float(getattr(raw, "qty", 0))
            if not symbol or quantity == 0:
                continue
            avg_price = _positive_float(getattr(raw, "avg_entry_price", 0))
            positions[symbol] = Position(symbol, quantity, avg_price, "alpaca", source=self.source_name())
        return positions


class PremarketPositionTracker:
    """保存每个持仓最近 60 秒的新行情，并识别最近一段 3% 单向波动。"""

    def __init__(self, *, threshold_pct: float = PREMARKET_POSITION_MOVE_PCT, window_seconds: int = PREMARKET_POSITION_WINDOW_SECONDS):
        if not 0 < float(threshold_pct) < 1:
            raise ValueError("premarket position threshold must be between 0 and 1")
        if int(window_seconds) <= 0:
            raise ValueError("premarket position window must be positive")
        self.threshold_pct = float(threshold_pct)
        self.window = timedelta(seconds=int(window_seconds))
        self.samples: dict[str, deque[PositionPriceSample]] = {}
        self.last_alert_direction: dict[str, str] = {}
        self.direction_leg_counts: dict[str, int] = {}

    def retain_symbols(self, symbols: set[str]) -> None:
        normalized = {normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)}
        for symbol in list(self.samples):
            if symbol not in normalized:
                self.samples.pop(symbol, None)
                self.last_alert_direction.pop(symbol, None)
                self.direction_leg_counts.pop(symbol, None)

    def acknowledge(self, movement: PositionMovement) -> None:
        """提醒已交付或已明确仅打印后，从当前价格重新累计下一段 3% 波动。"""

        symbol = normalize_symbol(movement.symbol)
        samples = self.samples.setdefault(symbol, deque())
        samples.clear()
        samples.append(PositionPriceSample(movement.current_price, movement.current_as_of, movement.price_source))
        self.last_alert_direction[symbol] = movement.direction
        self.direction_leg_counts[symbol] = movement.leg_number

    def observe(
        self,
        position: Position,
        *,
        price: float,
        as_of: datetime,
        price_source: str,
    ) -> PositionMovement | None:
        symbol = normalize_symbol(position.symbol)
        if not symbol or price <= 0 or as_of is None:
            return None
        samples = self.samples.setdefault(symbol, deque())
        if samples and as_of <= samples[-1].as_of:
            return None

        # 不同实时源可能存在盘口、时点或复权差异。切源时重新建立基线，避免把
        # Moomoo/Alpaca 之间的价差误报为持仓瞬时涨跌。
        if samples and _price_source_family(price_source) != _price_source_family(samples[-1].price_source):
            samples.clear()
            samples.append(PositionPriceSample(float(price), as_of, price_source))
            return None

        cutoff = as_of - self.window
        while samples and samples[0].as_of < cutoff:
            samples.popleft()
        previous = list(samples)
        current = PositionPriceSample(float(price), as_of, price_source)
        samples.append(current)
        if not previous:
            return None

        minimum = min(previous, key=lambda sample: sample.price)
        maximum = max(previous, key=lambda sample: sample.price)
        upward = current.price / minimum.price - 1.0
        downward = current.price / maximum.price - 1.0
        candidates: list[tuple[datetime, str, float, PositionPriceSample]] = []
        if upward + 1e-12 >= self.threshold_pct:
            candidates.append((minimum.as_of, "UP", upward, minimum))
        if downward - 1e-12 <= -self.threshold_pct:
            candidates.append((maximum.as_of, "DOWN", downward, maximum))
        if not candidates:
            return None

        # 剧烈 V 形/倒 V 形可能同时满足两个方向；选择离当前更近的极值，表达最近一段走势。
        _, direction, change_pct, anchor = max(candidates, key=lambda item: item[0])
        previous_direction = self.last_alert_direction.get(symbol, "")
        continues_previous_direction = previous_direction == direction
        leg_number = self.direction_leg_counts.get(symbol, 0) + 1 if continues_previous_direction else 1
        return PositionMovement(
            symbol=symbol,
            direction=direction,
            change_pct=change_pct,
            anchor_price=anchor.price,
            current_price=current.price,
            anchor_as_of=anchor.as_of,
            current_as_of=current.as_of,
            price_source=price_source,
            quantity=float(position.quantity),
            avg_price=float(position.avg_price),
            leg_number=leg_number,
            continues_previous_direction=continues_previous_direction,
        )


def run_premarket_positions_once(
    settings: Settings | None = None,
    *,
    position_source=None,
    market_data=None,
    tracker: PremarketPositionTracker | None = None,
    now: datetime | None = None,
    notify: bool = True,
) -> dict[str, int]:
    """只读取当前持仓并检查 60 秒价格波动；没有持仓时不读取个股行情。"""

    settings = settings or build_settings(trade_notify_mode="cloud")
    now_et = now or datetime.now(ZoneInfo(settings.market_timezone))
    summary = {"positions": 0, "alerts": 0, "sent": 0, "hold": 0, "errors": 0}
    if not is_premarket_time(now_et):
        print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 当前不在 04:00-09:30 ET，盘前持仓监控等待。", flush=True)
        return summary

    position_source = position_source or AlpacaPositionSource()
    tracker = tracker or PremarketPositionTracker()
    created_market_data = False
    rows: list[tuple[str, str, str, str, str]] = []
    try:
        positions = position_source.get_positions()
        tracker.retain_symbols(set(positions))
        summary["positions"] = len(positions)
        if not positions:
            print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 当前 Alpaca 无持仓；盘前不监控任何股票。", flush=True)
            return summary

        if market_data is None:
            market_data = build_default_market_data(settings)
            created_market_data = True

        for symbol, position in sorted(positions.items()):
            try:
                snapshot = market_data.get_snapshot(
                    symbol,
                    purpose=SNAPSHOT_PURPOSE_PREMARKET_OBSERVATION,
                )
                if not _has_premarket_realtime_price(snapshot.current_price_source, snapshot.current_price_as_of):
                    summary["hold"] += 1
                    rows.append((symbol, "等待新行情", "-", "-", "盘前没有可用于一分钟比较的新报价"))
                    continue
                movement = tracker.observe(
                    position,
                    price=snapshot.current_price,
                    as_of=snapshot.current_price_as_of,
                    price_source=snapshot.current_price_source,
                )
                unrealized_pct = (
                    snapshot.current_price / float(position.avg_price) - 1.0
                    if float(position.avg_price) > 0
                    else 0.0
                )
                if movement is None:
                    summary["hold"] += 1
                    rows.append(
                        (
                            symbol,
                            "持仓监控",
                            f"{snapshot.current_price:.4f}",
                            f"{unrealized_pct:+.2%}",
                            f"未达到滚动 {PREMARKET_POSITION_WINDOW_SECONDS}s / {PREMARKET_POSITION_MOVE_PCT:.0%}",
                        )
                    )
                    continue
                summary["alerts"] += 1
                delivered = False
                if notify:
                    delivered = bool(
                        safe_send_openclaw_messages(
                            settings,
                            [render_position_movement_message(movement)],
                            context=f"premarket position movement {movement.symbol} {movement.direction}",
                        )
                    )
                if delivered:
                    summary["sent"] += 1
                    tracker.acknowledge(movement)
                elif not notify:
                    tracker.acknowledge(movement)
                rows.append(
                    (
                        symbol,
                        "快速上涨" if movement.direction == "UP" else "快速下跌",
                        f"{movement.current_price:.4f}",
                        f"{unrealized_pct:+.2%}",
                        f"一分钟波动 {movement.change_pct:+.2%}；"
                        + (f"连续第 {movement.leg_number} 段" if movement.continues_previous_direction else "新方向第 1 段")
                        + ("；已提醒" if delivered else "；发送失败，下一条新行情重试" if notify else "；仅打印"),
                    )
                )
            except Exception as exc:
                summary["errors"] += 1
                rows.append((symbol, "行情错误", "-", "-", short_error(exc)))
    finally:
        if created_market_data and market_data is not None and hasattr(market_data, "close"):
            market_data.close()

    print_position_rows(now_et, rows)
    print(
        f"本轮完成：持仓 {summary['positions']} | 波动触发 {summary['alerts']} | "
        f"已发提醒 {summary['sent']} | 未触发 {summary['hold']} | 错误 {summary['errors']}",
        flush=True,
    )
    return summary


def run_premarket_positions_forever(
    settings: Settings | None = None,
    *,
    max_loops: int | None = None,
    sleep=time.sleep,
    now_provider=None,
    position_source=None,
    market_data=None,
) -> None:
    """持续监控当前持仓的一分钟 3% 波动，到 09:30 ET 自动停止。"""

    settings = settings or build_settings(trade_notify_mode="cloud")
    now_provider = now_provider or (lambda: datetime.now(ZoneInfo(settings.market_timezone)))
    start_now = now_provider()
    if is_premarket_monitor_finished(start_now):
        print(f"[{start_now:%Y-%m-%d %H:%M:%S %Z}] 已到 09:30 ET，盘前持仓监控不启动。", flush=True)
        return

    run_lock = acquire_run_lock(settings.output_dir, "premarket_position_monitor.lock", "盘前持仓波动监控")
    created_market_data = market_data is None
    market_data = market_data or build_default_market_data(settings)
    position_source = position_source or AlpacaPositionSource()
    tracker = PremarketPositionTracker()
    loop_count = 0
    try:
        print(
            "盘前持仓波动监控启动：只读取 Alpaca 当前持仓；滚动 60 秒每累计一段 3% 就提醒，"
            "同方向连续提醒、反转独立提醒、没有冷冻期；不筛选股票、不下单。",
            flush=True,
        )
        while True:
            now_et = now_provider()
            if is_premarket_monitor_finished(now_et):
                print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 到达 09:30 ET，盘前持仓监控退出。", flush=True)
                break
            loop_count += 1
            try:
                run_premarket_positions_once(
                    settings,
                    position_source=position_source,
                    market_data=market_data,
                    tracker=tracker,
                    now=now_et,
                )
            except KeyboardInterrupt:
                print("盘前持仓监控已停止。", flush=True)
                break
            except Exception as exc:
                print(f"本轮盘前持仓监控失败，下一轮重试：{short_error(exc)}", flush=True)
            if max_loops is not None and loop_count >= max_loops:
                break
            sleep_now = now_provider()
            poll_seconds = premarket_position_poll_seconds(sleep_now)
            if poll_seconds <= 0:
                break
            sleep(poll_seconds)
    finally:
        if created_market_data and hasattr(market_data, "close"):
            market_data.close()
        run_lock.close()


def render_position_movement_message(movement: PositionMovement) -> str:
    direction_text = "快速上涨" if movement.direction == "UP" else "快速下跌"
    unrealized_pct = (
        movement.current_price / movement.avg_price - 1.0
        if movement.avg_price > 0
        else 0.0
    )
    elapsed = max(0.0, (movement.current_as_of - movement.anchor_as_of).total_seconds())
    leg_text = (
        f"连续第 {movement.leg_number} 段{direction_text}"
        if movement.continues_previous_direction
        else f"新方向第 {movement.leg_number} 段{direction_text}"
    )
    return "\n".join(
        [
            f"【盘前持仓｜{direction_text}】{movement.symbol}",
            f"结论：{leg_text}；{elapsed:.0f} 秒内变动 {movement.change_pct:+.2%}，达到 3% 提醒线。",
            "动作：仅提醒持仓波动，不提交任何 Alpaca 订单。",
            "提醒机制：没有冷冻期；本段提醒成功后以当前价为新起点，每再累计 3% 会继续提醒，方向反转也独立提醒。",
            "",
            f"- 当前价：${movement.current_price:.4f}",
            f"- 起点价：${movement.anchor_price:.4f}",
            f"- 持仓数量：{movement.quantity:g}",
            f"- 持仓均价：${movement.avg_price:.4f}" if movement.avg_price > 0 else "- 持仓均价：未知",
            f"- 当前相对均价：{unrealized_pct:+.2%}" if movement.avg_price > 0 else "- 当前相对均价：未知",
            f"- 行情来源：{movement.price_source}",
            f"- 起点时间：{movement.anchor_as_of:%H:%M:%S %Z}",
            f"- 当前行情时间：{movement.current_as_of:%H:%M:%S %Z}",
        ]
    )


def print_position_rows(now_et: datetime, rows: list[tuple[str, str, str, str, str]]) -> None:
    if not rows:
        return
    print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 盘前持仓监控", flush=True)
    print("代码 | 状态 | 当前价 | 相对均价 | 说明", flush=True)
    print("-----|------|--------|----------|-----", flush=True)
    for row in rows:
        print(" | ".join(row), flush=True)


def premarket_position_poll_seconds(now_et: datetime) -> int:
    seconds_to_end = seconds_until_premarket_monitor_end(now_et)
    if seconds_to_end <= 0:
        return 0
    interval = PREMARKET_POSITION_POLL_SECONDS if is_premarket_time(now_et) else PREMARKET_WAIT_POLL_SECONDS
    return max(1, min(interval, seconds_to_end))


def _has_premarket_realtime_price(source: str, as_of: datetime | None) -> bool:
    if as_of is None:
        return False
    normalized = str(source or "").lower()
    return normalized.startswith(("moomoo_snapshot:", "alpaca_latest_quote:", "alpaca_latest_trade:"))


def _price_source_family(source: str) -> str:
    normalized = str(source or "").lower()
    if normalized.startswith("moomoo_snapshot:"):
        return "moomoo"
    if normalized.startswith(("alpaca_latest_quote:", "alpaca_latest_trade:")):
        return "alpaca"
    return normalized.split(":", 1)[0]


def _positive_float(value) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _nonzero_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
