from __future__ import annotations

import math
import uuid
from datetime import datetime

from .alpaca_connection import build_trading_connection
from .config import Settings
from .errors import short_error
from .market_time import is_premarket_time, is_realtime_order_time, is_regular_market_time, now_market_time
from .models import OrderResult, Position
from .order_guard import FINAL_STATUSES, cancel_unfilled_order, normalize_order_status, wait_for_fill_or_cancel
from .state import append_order, load_positions, save_positions
from .trade_notifications import notify_order_submitted, record_order_and_notify
from .watchlist import normalize_symbol, to_alpaca_symbol


class DryRunStockBroker:
    """本地模拟交易通道，用来验证策略和测试，不会提交真实订单。"""

    def __init__(self, settings: Settings):
        """保存 dry-run 读写本地状态所需的配置。"""
        self.settings = settings

    def source_name(self) -> str:
        """返回控制台和通知里展示的通道名称。"""
        return "dry-run"

    def get_positions(self) -> dict[str, Position]:
        """读取本地模拟持仓，供卖出规则判断使用。"""
        return load_positions(self.settings.state_file)

    def place_market_buy(
        self,
        symbol: str,
        notional_usd: float,
        current_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """按金额模拟买入，并同步写入本地持仓和订单记录。"""
        if current_price <= 0:
            return OrderResult("", symbol, "BUY", 0, current_price, "REJECTED", "当前价格无效")
        quantity = notional_usd / current_price
        if not self.settings.allow_fractional_shares:
            quantity = math.floor(quantity)
        quantity = round(quantity, 6)
        if quantity <= 0:
            return OrderResult("", symbol, "BUY", 0, current_price, "REJECTED", "买入金额不足 1 股")

        positions = self.get_positions()
        existing = positions.get(symbol)
        if existing:
            total_qty = existing.quantity + quantity
            avg_price = ((existing.quantity * existing.avg_price) + (quantity * current_price)) / total_qty
            positions[symbol] = Position(symbol, total_qty, round(avg_price, 6), existing.opened_at)
        else:
            positions[symbol] = Position(symbol, quantity, current_price, datetime.now().isoformat(timespec="seconds"))
        save_positions(self.settings.state_file, positions)

        result = OrderResult(str(uuid.uuid4()), symbol, "BUY", quantity, current_price, "DRY_RUN", "模拟买入，未提交真实订单")
        order_time = now_market_time(self.settings)
        append_order(self.settings.output_dir, result, reason, day=order_time.date(), created_at=order_time)
        return result

    def place_market_sell(
        self,
        symbol: str,
        quantity: float,
        current_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """模拟卖出，并从本地持仓中扣减对应数量。"""
        positions = self.get_positions()
        existing = positions.get(symbol)
        sell_qty = min(quantity, existing.quantity) if existing else 0
        if sell_qty <= 0:
            return OrderResult("", symbol, "SELL", 0, current_price, "REJECTED", "没有可卖模拟持仓")

        remaining = round(existing.quantity - sell_qty, 6)
        if remaining > 0:
            positions[symbol] = Position(symbol, remaining, existing.avg_price, existing.opened_at)
        else:
            positions.pop(symbol, None)
        save_positions(self.settings.state_file, positions)

        result = OrderResult(str(uuid.uuid4()), symbol, "SELL", sell_qty, current_price, "DRY_RUN", "模拟卖出，未提交真实订单")
        order_time = now_market_time(self.settings)
        append_order(self.settings.output_dir, result, reason, day=order_time.date(), created_at=order_time)
        return result

    def place_limit_buy(
        self,
        symbol: str,
        notional_usd: float,
        limit_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """模拟固定限价买入，供 OpenClaw 指令测试链路复用。"""
        return self.place_market_buy(symbol, notional_usd, limit_price, reason)

    def place_limit_sell(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """模拟固定限价卖出，供 OpenClaw 指令测试链路复用。"""
        return self.place_market_sell(symbol, quantity, limit_price, reason)


class AlpacaStockBroker:
    """真实 Alpaca 股票交易通道，paper/live 由当前 key 自动识别。"""

    def __init__(self, settings: Settings):
        """建立交易连接并保存当前账户模式。"""
        self.settings = settings
        connection = build_trading_connection()
        self.client = connection.client
        self.account = connection.account
        self.paper = connection.paper

    def get_positions(self) -> dict[str, Position]:
        """读取 Alpaca 真实持仓，并转换成策略统一使用的 Position。"""
        positions: dict[str, Position] = {}
        for raw in self.client.get_all_positions():
            symbol = normalize_symbol(getattr(raw, "symbol", ""))
            qty = float(getattr(raw, "qty", 0) or 0)
            if not symbol or qty <= 0:
                continue
            avg_price = float(getattr(raw, "avg_entry_price", 0) or 0)
            positions[symbol] = Position(symbol, qty, avg_price, "alpaca", source=self.source_name())
        return positions

    def place_market_buy(
        self,
        symbol: str,
        notional_usd: float,
        current_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """按目标金额换算股数后提交买单；不支持碎股时自动向下取整。"""
        qty = self._buy_qty(symbol, notional_usd, current_price)
        if qty <= 0:
            result = OrderResult("", symbol, "BUY", 0, current_price, "REJECTED", "买入金额不足")
            return self._record_result(result, reason)
        result = self._submit_order(symbol, "BUY", qty, current_price, reason, skip_time_validation=skip_time_validation)
        order_time = now_market_time(self.settings)
        record_order_and_notify(self.settings, result, reason, broker_name=self.source_name(), order_time=order_time)
        return result

    def place_limit_buy(
        self,
        symbol: str,
        notional_usd: float,
        limit_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """提交用户指定价格的 BUY LIMIT；金额按限价换算成股数。"""
        qty = self._buy_qty(symbol, notional_usd, limit_price)
        if qty <= 0:
            result = OrderResult("", symbol, "BUY", 0, limit_price, "REJECTED", "买入金额不足")
            return self._record_result(result, reason)
        result = self._submit_fixed_limit_order(
            symbol,
            "BUY",
            qty,
            limit_price,
            reason,
            skip_time_validation=skip_time_validation,
        )
        return self._record_result(result, reason)

    def place_market_sell(
        self,
        symbol: str,
        quantity: float,
        current_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """提交真实卖单；失败会返回 REJECTED 而不是抛出 traceback。"""
        if quantity <= 0:
            result = OrderResult("", symbol, "SELL", 0, current_price, "REJECTED", "没有可卖持仓")
            return self._record_result(result, reason)
        result = self._submit_order(symbol, "SELL", quantity, current_price, reason, skip_time_validation=skip_time_validation)
        order_time = now_market_time(self.settings)
        record_order_and_notify(self.settings, result, reason, broker_name=self.source_name(), order_time=order_time)
        return result

    def place_limit_sell(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """提交用户指定价格的 SELL LIMIT。"""
        if quantity <= 0:
            result = OrderResult("", symbol, "SELL", 0, limit_price, "REJECTED", "没有可卖持仓")
            return self._record_result(result, reason)
        result = self._submit_fixed_limit_order(
            symbol,
            "SELL",
            quantity,
            limit_price,
            reason,
            skip_time_validation=skip_time_validation,
        )
        return self._record_result(result, reason)

    def cancel_order(self, order_id: str, reason: str, raw_order=None) -> OrderResult:
        """按订单号撤单；撤单失败只返回结果，不中断 OpenClaw 指令服务。"""
        try:
            raw_order = raw_order or self.client.get_order_by_id(order_id)
        except Exception as exc:
            result = OrderResult(order_id, "", "CANCEL", 0, 0, "REJECTED", short_error(exc))
            return self._record_result(result, reason)

        symbol = _raw_order_symbol(raw_order)
        side = _raw_order_side(raw_order)
        quantity = _raw_order_quantity(raw_order)
        price = _raw_order_price(raw_order)
        status = normalize_order_status(raw_order)
        if status in FINAL_STATUSES:
            result = OrderResult(order_id, symbol, side, quantity, price, status, "订单已是最终状态，无需撤单")
            return self._record_result(result, reason)

        result = cancel_unfilled_order(
            self.client,
            order_id,
            symbol,
            side,
            quantity,
            price,
            0,
            "手动撤单请求已发送。",
            "手动撤单请求",
        )
        return self._record_result(result, reason)

    def cancel_open_orders(self, symbol: str = "", reason: str = "") -> list[OrderResult]:
        """取消指定股票的挂单；symbol 为空时取消全部挂单。"""
        try:
            orders = self._get_open_orders(symbol)
        except Exception as exc:
            target = symbol or "ALL"
            result = OrderResult("", target, "CANCEL", 0, 0, "REJECTED", short_error(exc))
            return [self._record_result(result, reason)]
        if not orders:
            target = symbol or "ALL"
            result = OrderResult("", target, "CANCEL", 0, 0, "NO_OPEN_ORDERS", "没有找到可撤挂单")
            return [self._record_result(result, reason)]
        return [self.cancel_order(str(getattr(raw, "id", "") or ""), reason, raw_order=raw) for raw in orders]

    def _submit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        current_price: float,
        reason: str = "",
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """根据交易时段选择 market 或 extended-hours limit，并调用 Alpaca。"""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        alpaca_symbol = to_alpaca_symbol(symbol)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        now_et = now_market_time(self.settings)
        if not skip_time_validation and side == "BUY" and is_premarket_time(now_et):
            return OrderResult("", symbol, side, quantity, current_price, "REJECTED", "盘前时段不买入，已跳过真实买单")
        if not skip_time_validation and not is_realtime_order_time(now_et):
            return OrderResult("", symbol, side, quantity, current_price, "REJECTED", "当前不在实时价下单时段，已跳过真实下单")

        # 非常规盘只能用 extended-hours limit；常规盘使用 market order。
        if is_regular_market_time(now_et):
            request = MarketOrderRequest(symbol=alpaca_symbol, qty=quantity, side=order_side, time_in_force=TimeInForce.DAY)
        else:
            if not skip_time_validation and not self.settings.extended_hours_orders_enabled:
                return OrderResult("", symbol, side, quantity, current_price, "REJECTED", "当前不在常规盘，且未开启盘前/盘后下单")
            request = LimitOrderRequest(
                symbol=alpaca_symbol,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=self._extended_limit_price(side, current_price),
                extended_hours=True,
            )

        try:
            raw = self.client.submit_order(order_data=request)
        except Exception as exc:
            return OrderResult("", symbol, side, quantity, current_price, "REJECTED", short_error(exc))

        submitted = OrderResult(
            str(getattr(raw, "id", "") or ""),
            symbol,
            side,
            quantity,
            current_price,
            normalize_order_status(raw) or "SUBMITTED",
            f"Alpaca {self.source_name()} order submitted",
        )
        notify_order_submitted(self.settings, submitted, reason, broker_name=self.source_name())

        return wait_for_fill_or_cancel(
            self.client,
            raw,
            symbol,
            side,
            quantity,
            current_price,
            self.source_name(),
            timeout_seconds=self.settings.order_cancel_after_seconds,
            poll_seconds=self.settings.order_status_poll_seconds,
        )

    def _submit_fixed_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        limit_price: float,
        reason: str = "",
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """提交用户明确指定价格的限价单；OpenClaw 手动单可跳过本地时段保护。"""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        alpaca_symbol = to_alpaca_symbol(symbol)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        now_et = now_market_time(self.settings)
        if not skip_time_validation and side == "BUY" and is_premarket_time(now_et):
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", "盘前时段不买入，已跳过真实买单")
        if not skip_time_validation and not is_realtime_order_time(now_et):
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", "当前不在实时价下单时段，已跳过真实下单")
        if not skip_time_validation and not is_regular_market_time(now_et) and not self.settings.extended_hours_orders_enabled:
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", "当前不在常规盘，且未开启盘前/盘后下单")

        request = LimitOrderRequest(
            symbol=alpaca_symbol,
            qty=quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            extended_hours=not is_regular_market_time(now_et),
        )
        try:
            raw = self.client.submit_order(order_data=request)
        except Exception as exc:
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", short_error(exc))

        submitted = OrderResult(
            str(getattr(raw, "id", "") or ""),
            symbol,
            side,
            quantity,
            limit_price,
            normalize_order_status(raw) or "SUBMITTED",
            f"Alpaca {self.source_name()} fixed limit order submitted",
        )
        notify_order_submitted(self.settings, submitted, reason, broker_name=self.source_name())
        return wait_for_fill_or_cancel(
            self.client,
            raw,
            symbol,
            side,
            quantity,
            limit_price,
            self.source_name(),
            timeout_seconds=self.settings.order_cancel_after_seconds,
            poll_seconds=self.settings.order_status_poll_seconds,
        )

    def _get_open_orders(self, symbol: str):
        """读取 Alpaca open orders，撤单指令会用它按股票代码定位挂单。"""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        symbols = [to_alpaca_symbol(symbol)] if symbol else None
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=symbols)
        return self.client.get_orders(filter=request)

    def _record_result(self, result: OrderResult, reason: str) -> OrderResult:
        """统一写 CSV 和通知，避免手动指令绕过记录链路。"""
        order_time = now_market_time(self.settings)
        record_order_and_notify(self.settings, result, reason, broker_name=self.source_name(), order_time=order_time)
        return result

    def _buy_qty(self, symbol: str, notional_usd: float, current_price: float) -> float:
        """把买入金额换算成下单股数，必要时退回整数股。"""
        if current_price <= 0:
            return 0.0
        qty = notional_usd / current_price
        if not self._can_buy_fractional(symbol):
            return float(math.floor(qty))
        return round(qty, 6)

    def _can_buy_fractional(self, symbol: str) -> bool:
        """查询 Alpaca 碎股权限；查询失败时保守用整数股，减少拒单。"""
        if not self.settings.allow_fractional_shares:
            return False
        try:
            asset = self.client.get_asset(to_alpaca_symbol(symbol))
            return bool(getattr(asset, "fractionable", False))
        except Exception as exc:
            print(f"{symbol}: 查询 Alpaca 碎股权限失败，改用整数股。{short_error(exc)}", flush=True)
            return False

    def _extended_limit_price(self, side: str, current_price: float) -> float:
        """生成盘前/盘后限价单的保护价。"""
        buffer = self.settings.extended_hours_limit_buffer_pct
        if side == "BUY":
            return round(current_price * (1.0 + buffer), 2)
        return round(current_price * (1.0 - buffer), 2)

    def source_name(self) -> str:
        """返回当前真实交易通道名称。"""
        return "alpaca-paper" if self.paper else "alpaca-live"


def _raw_order_symbol(raw_order) -> str:
    """从 Alpaca order 里读取并标准化股票代码。"""
    return normalize_symbol(getattr(raw_order, "symbol", "") or "")


def _raw_order_side(raw_order) -> str:
    """从 Alpaca order 里读取 BUY/SELL。"""
    value = getattr(raw_order, "side", "") or ""
    value = getattr(value, "value", value)
    value = str(value).split(".")[-1].upper()
    return "SELL" if value == "SELL" else "BUY"


def _raw_order_quantity(raw_order) -> float:
    """从 Alpaca order 里读取下单股数。"""
    try:
        return float(getattr(raw_order, "qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _raw_order_price(raw_order) -> float:
    """优先显示限价，其次显示成交均价；没有价格时返回 0。"""
    for field in ("limit_price", "filled_avg_price", "stop_price"):
        try:
            value = float(getattr(raw_order, field, 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0
