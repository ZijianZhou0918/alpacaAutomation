"""订单执行层：把服务层的买卖意图转换成 Alpaca 订单。

阅读交易写入逻辑时先看 ``AlpacaStockBroker``：
- ``place_*`` 是业务层调用入口；
- ``_submit_order`` / ``_submit_fixed_limit_order`` 构造并真实提交订单；
- ``cancel_order`` 把手动撤单交给可配置撤单策略；
- 自动监控使用持久化非阻塞订单监督；手动入口仍可同步等待终态。
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .alpaca_connection import build_trading_connection
from .config import Settings
from .errors import short_error
from .market_time import is_buy_order_time, is_premarket_time, is_realtime_order_time, is_regular_market_time, now_market_time
from .models import OrderResult, Position, has_unconfirmed_order_status
from .order_guard import FINAL_STATUSES, filled_quantity, normalize_order_status
from .pending_orders import PendingOrderEvent, PendingOrderStore
from .state import append_order, load_positions, save_positions
from .strategy_framework import resolve_strategy_runtime
from .trade_notifications import notify_order_submitted, record_order_and_notify
from .watchlist import normalize_symbol, to_alpaca_symbol


BROKER_PROTECTIVE_STOP_ACTION = "broker_protective_stop"
BROKER_PROTECTIVE_STOP_CLIENT_PREFIX = "ma5-stop-"


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

    def get_open_buy_order_symbols(self) -> set[str]:
        """dry-run 没有真实开放买单。"""
        return set()

    def get_open_sell_order_symbols(self) -> set[str]:
        """dry-run 没有真实开放卖单。"""
        return set()

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
        quantity = float(math.floor(notional_usd / current_price))
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
    """真实 Alpaca 股票交易通道。

    该类是自动监控、OpenClaw 手动指令和部分盘后流程共用的订单边界。
    paper/live 由当前 key 自动识别；调用 ``place_*`` 可能产生真实外部写操作。
    """

    manages_pending_orders = True

    def __init__(self, settings: Settings):
        """建立交易连接并保存当前账户模式。"""
        self.settings = settings
        self.cancel_strategy = resolve_strategy_runtime(settings).cancel
        # 必须先校验本地未决订单状态，再连接券商；损坏状态不能在外部 I/O 后才暴露。
        self.pending_order_store = PendingOrderStore(settings.output_dir)
        connection = build_trading_connection()
        self.client = connection.client
        self.account = connection.account
        self.paper = connection.paper
        # 一旦真实订单结果无法写入本地账本，后续每日次数和复盘都不再可信。
        # 该标记在当前 Broker 生命周期内保持锁存，由 service 失败关闭后续自动买入。
        self.order_recording_error = ""
        # submit_order 网络异常时，券商可能已经收单。无法用 client_order_id
        # 查清结果就锁存该风险，禁止后续自动买入扩大未知暴露。
        self.order_safety_error = ""
        self.protective_stop_error = ""
        # 每轮刚确认过终态成交的股票继续按开放订单保护一轮，避免持仓接口短暂
        # 滞后时立刻补买或重复卖出。下一轮 reconcile 会重新清空并按最新状态建立。
        self.recently_reconciled_buy_symbols: set[str] = set()
        self.recently_reconciled_sell_symbols: set[str] = set()
        # 主动退出前已确认撤销保护单的股票，本轮不重新补挂；下一轮由真实持仓和
        # 普通卖单状态重新决定，避免“刚撤保护单、同轮又补挂”的竞态。
        self.recently_released_protective_symbols: set[str] = set()

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

    def get_open_buy_order_symbols(self) -> set[str]:
        """读取 Alpaca 当前开放买单，用于自动监控防重复下单。"""
        symbols = {
            order.symbol
            for order in self._pending_orders().orders.values()
            if order.side == "BUY"
        }
        symbols.update(getattr(self, "recently_reconciled_buy_symbols", set()))
        for raw in self._get_open_orders(""):
            if _raw_order_side(raw) != "BUY":
                continue
            symbol = normalize_symbol(getattr(raw, "symbol", ""))
            if symbol:
                symbols.add(symbol)
        return symbols

    def get_open_sell_order_symbols(self) -> set[str]:
        """读取 Alpaca 当前开放卖单，避免下一轮对同一持仓重复卖出。"""
        symbols = {
            order.symbol
            for order in self._pending_orders().orders.values()
            if order.side == "SELL"
        }
        symbols.update(getattr(self, "recently_reconciled_sell_symbols", set()))
        for raw in self._get_open_orders(""):
            if _raw_order_side(raw) != "SELL":
                continue
            symbol = normalize_symbol(getattr(raw, "symbol", ""))
            if symbol:
                symbols.add(symbol)
        return symbols

    def get_open_strategy_exit_order_symbols(self) -> set[str]:
        """返回会阻止新退出单的卖单，正常等待中的保护 STOP 不算主动退出。

        保护单已经部分成交或处于撤单待确认时仍必须阻断新卖单，否则可能卖空。
        非本程序创建的卖单也始终视为冲突，不能擅自忽略用户手工订单。
        """

        symbols = set(getattr(self, "recently_reconciled_sell_symbols", set()))
        protective_ids: set[str] = set()
        for order in self._pending_orders().orders.values():
            if order.side != "SELL":
                continue
            if order.strategy_action != BROKER_PROTECTIVE_STOP_ACTION:
                symbols.add(order.symbol)
                continue
            protective_ids.add(order.active_order_id)
            status = str(order.last_status or "").upper()
            if order.cancel_requested_at or "PARTIALLY_FILLED" in status:
                symbols.add(order.symbol)

        for raw in self._get_open_orders(""):
            if _raw_order_side(raw) != "SELL":
                continue
            symbol = _raw_order_symbol(raw)
            if not symbol:
                continue
            raw_id = str(getattr(raw, "id", "") or "")
            if raw_id in protective_ids or _is_managed_protective_raw_order(raw):
                status = normalize_order_status(raw)
                if filled_quantity(raw) > 0 or status == "PENDING_CANCEL":
                    symbols.add(symbol)
                continue
            symbols.add(symbol)
        return symbols

    def ensure_protective_stops(
        self,
        positions: dict[str, Position],
        eligible_symbols: set[str],
        stop_pct: float,
        now_et: datetime,
    ) -> None:
        """为策略持仓创建或校准唯一的 Alpaca GTC STOP MARKET 保护单。"""

        if not -1.0 < float(stop_pct) < 0.0:
            raise ValueError("broker protective stop pct must be between -1 and 0")
        eligible = {normalize_symbol(value) for value in eligible_symbols if normalize_symbol(value)}
        released = getattr(self, "recently_released_protective_symbols", set())
        open_orders = list(self._get_open_orders(""))
        for raw in open_orders:
            if _is_managed_protective_raw_order(raw):
                self._adopt_protective_order(raw, now_et)

        # 计划已关闭、持仓已消失或保护功能未启用时，券商端遗留的本程序 STOP
        # 必须撤掉；否则未来同代码重新持仓时可能被旧单意外卖出。
        for order in list(self._pending_orders().orders.values()):
            if order.strategy_action != BROKER_PROTECTIVE_STOP_ACTION or order.symbol in eligible:
                continue
            released_ok, release_reason = self.release_protective_stop(order.symbol, now_et)
            if not released_ok and "撤销处理中" not in release_reason:
                self._latch_protective_stop_error(order.symbol, release_reason)

        for symbol in sorted(eligible):
            position = positions.get(symbol)
            if position is None or float(position.quantity) <= 0 or float(position.avg_price) <= 0:
                continue
            if symbol in released:
                continue
            managed = self._protective_pending_orders(symbol)
            if len(managed) > 1:
                self._latch_protective_stop_error(symbol, "检测到多张托管保护单，已停止自动调整")
                continue

            conflicting = [
                raw
                for raw in open_orders
                if _raw_order_symbol(raw) == symbol
                and _raw_order_side(raw) == "SELL"
                and not _is_managed_protective_raw_order(raw)
            ]
            has_pending_exit = any(
                order.symbol == symbol
                and order.side == "SELL"
                and order.strategy_action != BROKER_PROTECTIVE_STOP_ACTION
                for order in self._pending_orders().orders.values()
            )
            if conflicting or has_pending_exit:
                # 普通卖单可能正在止盈/清仓；此时补一张全仓 STOP 会使总卖量超过持仓。
                if managed:
                    self.release_protective_stop(symbol, now_et)
                continue

            quantity = self._sell_qty(symbol, float(position.quantity))
            stop_price = normalize_limit_price(float(position.avg_price) * (1.0 + float(stop_pct)))
            if quantity <= 0 or stop_price <= 0:
                self._latch_protective_stop_error(symbol, "无法按当前持仓生成有效保护单数量或价格")
                continue
            if not managed:
                self._submit_protective_stop(symbol, quantity, stop_price, now_et)
                continue

            order = managed[0]
            try:
                raw = self.client.get_order_by_id(order.active_order_id)
            except Exception as exc:
                self._latch_protective_stop_error(
                    symbol,
                    f"无法确认保护单 {order.active_order_id}：{short_error(exc)}",
                )
                continue
            status = normalize_order_status(raw)
            if status in FINAL_STATUSES or order.cancel_requested_at or filled_quantity(raw) > 0:
                # 终态/部分成交必须先经过统一对账，不在这里猜测剩余持仓或替换数量。
                continue
            current_quantity = _raw_order_quantity(raw) or order.requested_quantity
            current_stop = _raw_order_stop_price(raw) or order.requested_price
            if _same_order_quantity(current_quantity, quantity) and math.isclose(
                current_stop,
                stop_price,
                rel_tol=0.0,
                abs_tol=0.00005,
            ):
                continue
            self._replace_protective_stop(order, quantity, stop_price, now_et)

    def release_protective_stop(self, symbol: str, now_et: datetime) -> tuple[bool, str]:
        """主动卖出前撤掉保护 STOP；只有确认零成交终态后才允许新卖单。"""

        normalized = normalize_symbol(symbol)
        managed = self._protective_pending_orders(normalized)
        if len(managed) > 1:
            return False, "检测到多张保护单，拒绝再提交主动卖单"
        if not managed:
            # 兼容重启后本地状态缺失但券商订单仍存在的情况，先收编再撤。
            try:
                raw_orders = [
                    raw
                    for raw in self._get_open_orders(normalized)
                    if _is_managed_protective_raw_order(raw)
                ]
            except Exception as exc:
                return False, f"无法确认券商保护单：{short_error(exc)}"
            for raw in raw_orders:
                self._adopt_protective_order(raw, now_et)
            managed = self._protective_pending_orders(normalized)
        if not managed:
            return True, "没有开放保护单"
        if len(managed) > 1:
            return False, "检测到多张保护单，拒绝再提交主动卖单"

        order = managed[0]
        try:
            raw = self.client.get_order_by_id(order.active_order_id)
            status = normalize_order_status(raw)
            if status not in FINAL_STATUSES:
                self.client.cancel_order_by_id(order.active_order_id)
                order.cancel_requested_at = now_et.isoformat()
                order.updated_at = now_et.isoformat()
                self._pending_orders().save(now_et)
                raw = self.client.get_order_by_id(order.active_order_id)
                status = normalize_order_status(raw)
        except Exception as exc:
            self._latch_protective_stop_error(
                normalized,
                f"保护单撤销结果无法确认：{short_error(exc)}",
            )
            return False, "保护单撤销结果无法确认，本轮禁止重复卖出"

        filled = filled_quantity(raw)
        if status in FINAL_STATUSES and filled <= 0:
            self._pending_orders().remove(order.tracking_order_id, now_et)
            released = getattr(self, "recently_released_protective_symbols", None)
            if released is None:
                released = set()
                self.recently_released_protective_symbols = released
            released.add(normalized)
            return True, "保护单已确认撤销"
        if filled > 0:
            return False, f"保护单已成交 {filled:g} 股，等待持仓对账后再决定"
        return False, "保护单撤销处理中，等待券商确认后再主动卖出"

    def cancel_managed_buy_orders_for_symbol(self, symbol: str, now_et: datetime) -> None:
        """保护 STOP 成交后立即请求撤销同股尚未终态的自动买单。"""

        normalized = normalize_symbol(symbol)
        changed = False
        for order in self._pending_orders().orders.values():
            if order.symbol != normalized or order.side != "BUY" or order.cancel_requested_at:
                continue
            try:
                raw = self.client.get_order_by_id(order.active_order_id)
                if normalize_order_status(raw) in FINAL_STATUSES:
                    continue
                self.client.cancel_order_by_id(order.active_order_id)
                order.cancel_requested_at = now_et.isoformat()
                order.updated_at = now_et.isoformat()
                changed = True
            except Exception as exc:
                self._latch_protective_stop_error(
                    normalized,
                    f"保护单成交后无法撤销买单 {order.active_order_id}：{short_error(exc)}",
                )
        if changed:
            self._pending_orders().save(now_et)

    def place_market_buy(
        self,
        symbol: str,
        notional_usd: float,
        current_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """按目标金额换算整数股后提交买单。

        常规盘构造 MARKET，扩展时段构造带保护价的 LIMIT；实际写入由
        ``_submit_order`` 完成，随后记录结果并发送通知。
        """
        qty = self._buy_qty(symbol, notional_usd, current_price)
        if qty <= 0:
            result = OrderResult("", symbol, "BUY", 0, current_price, "REJECTED", "买入金额不足")
            return self._record_result(result, reason)
        result = self._submit_order(symbol, "BUY", qty, current_price, reason, skip_time_validation=skip_time_validation)
        return self._record_result(result, reason)

    def place_limit_buy(
        self,
        symbol: str,
        notional_usd: float,
        limit_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """提交指定价格的真实 BUY LIMIT；金额先按限价换算为整数股。

        手动/兼容调用会同步等待终态；自动监控改用
        ``place_limit_buy_nonblocking``，避免阻塞逐股循环。
        """
        # 【限价买入 1/2：金额转整数股】
        # 自动监控按美元预算下单；这里用限价计算可买整数股，避免提交分数股。
        # 预算不足 1 股时只返回 REJECTED，不会尝试扩大金额或改成市价单。
        qty = self._buy_qty(symbol, notional_usd, limit_price)
        if qty <= 0:
            result = OrderResult("", symbol, "BUY", 0, limit_price, "REJECTED", "买入金额不足")
            return self._record_result(result, reason)

        # 【限价买入 2/2：进入同步真实提交与终态保护】
        # _submit_fixed_limit_order 负责时间窗复核、构造 Alpaca 请求、submit_order，
        # 以及成交等待/超时撤单；本方法只在其返回后统一记录最终结果。
        result = self._submit_fixed_limit_order(
            symbol,
            "BUY",
            qty,
            limit_price,
            reason,
            skip_time_validation=skip_time_validation,
        )
        return self._record_result(result, reason)

    def place_limit_buy_nonblocking(
        self,
        symbol: str,
        notional_usd: float,
        limit_price: float,
        reason: str,
        *,
        strategy_action: str = "",
    ) -> OrderResult:
        """Submit an automatic BUY LIMIT and persist it without waiting in the symbol loop."""

        quantity = self._buy_qty(symbol, notional_usd, limit_price)
        if quantity <= 0:
            return self._record_result(
                OrderResult("", symbol, "BUY", 0, limit_price, "REJECTED", "买入金额不足"),
                reason,
            )
        result = self._submit_fixed_limit_order(
            symbol,
            "BUY",
            quantity,
            limit_price,
            reason,
            wait_for_terminal=False,
        )
        self._register_pending_result(
            result,
            reason=reason,
            strategy_action=strategy_action,
            strategy_notional=notional_usd,
        )
        notify_order_submitted(self.settings, result, reason, broker_name=self.source_name())
        return self._record_result(result, reason)

    def place_market_sell_nonblocking(
        self,
        symbol: str,
        quantity: float,
        current_price: float,
        reason: str,
        *,
        strategy_action: str = "",
    ) -> OrderResult:
        """Submit an automatic market-style SELL and let later rounds supervise it."""

        if quantity <= 0:
            return self._record_result(
                OrderResult("", symbol, "SELL", 0, current_price, "REJECTED", "没有可卖持仓"),
                reason,
            )
        result = self._submit_order(
            symbol,
            "SELL",
            quantity,
            current_price,
            reason,
            wait_for_terminal=False,
        )
        self._register_pending_result(
            result,
            reason=reason,
            strategy_action=strategy_action,
            strategy_notional=0.0,
        )
        notify_order_submitted(self.settings, result, reason, broker_name=self.source_name())
        return self._record_result(result, reason)

    def place_limit_sell_nonblocking(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
        reason: str,
        *,
        strategy_action: str = "",
    ) -> OrderResult:
        """Submit an automatic SELL LIMIT and let later rounds supervise it."""

        if quantity <= 0:
            return self._record_result(
                OrderResult("", symbol, "SELL", 0, limit_price, "REJECTED", "没有可卖持仓"),
                reason,
            )
        result = self._submit_fixed_limit_order(
            symbol,
            "SELL",
            quantity,
            limit_price,
            reason,
            wait_for_terminal=False,
        )
        self._register_pending_result(
            result,
            reason=reason,
            strategy_action=strategy_action,
            strategy_notional=0.0,
        )
        notify_order_submitted(self.settings, result, reason, broker_name=self.source_name())
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
        """提交真实卖单；常规盘用 MARKET，扩展时段用保护 LIMIT。

        自动止盈和尾盘清仓会走这里。失败会返回 REJECTED，而不是把异常抛到监控层。
        """
        if quantity <= 0:
            result = OrderResult("", symbol, "SELL", 0, current_price, "REJECTED", "没有可卖持仓")
            return self._record_result(result, reason)
        result = self._submit_order(symbol, "SELL", quantity, current_price, reason, skip_time_validation=skip_time_validation)
        return self._record_result(result, reason)

    def place_limit_sell(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
        reason: str,
        *,
        skip_time_validation: bool = False,
    ) -> OrderResult:
        """提交指定价格的真实 SELL LIMIT。

        自动止损及半仓止盈后的剩余仓保护会走这里。
        """
        # 【限价卖出 1/2：拒绝无效数量】
        # 更精确的可卖数量还会在 _submit_fixed_limit_order 中按券商当前持仓收敛；
        # 此处先阻止明显为 0/负数的请求进入真实 SDK。
        if quantity <= 0:
            result = OrderResult("", symbol, "SELL", 0, limit_price, "REJECTED", "没有可卖持仓")
            return self._record_result(result, reason)

        # 【限价卖出 2/2：进入统一真实提交与终态保护】
        # 自动止损和 OpenClaw 手动限价卖出都复用这里，但 skip_time_validation
        # 只有明确手动授权路径才会传 True；普通监控必须保留全部本地时间保护。
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
        """按订单号执行手动撤单。

        先读取并检查订单终态，再调用当前 ``CancelStrategy``。策略最终会进入
        ``order_guard.cancel_unfilled_order``，在那里调用 Alpaca 撤单接口。
        撤单失败只返回结果，不中断 OpenClaw 指令服务。
        """
        # 【显式撤单 1/3：读取券商最新订单】
        # cancel_open_orders 已拿到 raw_order 时直接复用，按订单号调用时则现场查询；
        # 查询失败不能假定订单仍开放，必须返回 REJECTED 并停止。
        try:
            raw_order = raw_order or self.client.get_order_by_id(order_id)
        except Exception as exc:
            result = OrderResult(order_id, "", "CANCEL", 0, 0, "REJECTED", short_error(exc))
            return self._record_result(result, reason)

        # REPLACED 只终结旧订单；replaced_by 指向仍可能开放的新订单。沿替换链
        # 定位当前订单，避免对旧 id 短路为“无需撤单”后留下真实挂单。
        seen_order_ids = {order_id}
        while normalize_order_status(raw_order) == "REPLACED":
            replacement_id = str(getattr(raw_order, "replaced_by", "") or "")
            if not replacement_id or replacement_id in seen_order_ids:
                break
            seen_order_ids.add(replacement_id)
            try:
                raw_order = self.client.get_order_by_id(replacement_id)
                order_id = replacement_id
            except Exception as exc:
                result = OrderResult(
                    order_id,
                    _raw_order_symbol(raw_order),
                    _raw_order_side(raw_order),
                    _raw_order_quantity(raw_order),
                    _raw_order_price(raw_order),
                    "REPLACED",
                    f"无法确认替换订单 {replacement_id}：{short_error(exc)}",
                )
                return self._record_result(result, reason)

        symbol = _raw_order_symbol(raw_order)
        side = _raw_order_side(raw_order)
        quantity = _raw_order_quantity(raw_order)
        price = _raw_order_price(raw_order)
        status = normalize_order_status(raw_order)

        # 【显式撤单 2/3：终态短路】
        # 已成交、已取消、被拒绝或其他最终状态都不再发送 cancel_order_by_id，
        # 防止撤单竞态覆盖真实成交结论。
        if status in FINAL_STATUSES:
            # 订单可能在撤单请求与本次查询之间部分成交后取消。此时 Alpaca 的
            # 顶层状态是 CANCELED，但 filled_qty 仍代表真实成交，必须保留下来。
            partial_qty = filled_quantity(raw_order)
            result_status = f"PARTIALLY_FILLED_{status}" if partial_qty > 0 and status != "FILLED" else status
            result_quantity = partial_qty if partial_qty > 0 else quantity
            result = OrderResult(
                order_id,
                symbol,
                side,
                result_quantity,
                price,
                result_status,
                "订单已是最终状态，无需撤单",
            )
            return self._record_result(result, reason)

        # 【显式撤单 3/3：进入可配置撤单策略】
        # timeout_seconds=0 表示这是立即显式撤单，不执行自动订单等待；策略仍必须
        # 复用 order_guard.cancel_unfilled_order 发请求并只读复查最终状态。
        # 真正的 Alpaca cancel_order_by_id 写入位于 order_guard.py。
        result = self._configured_cancel_strategy().cancel_order(
            self.client,
            order_id,
            symbol,
            side,
            quantity,
            price,
            timeout_seconds=0,
            success_message="手动撤单请求已发送。",
            failure_prefix="手动撤单请求",
        )
        return self._record_result(result, reason)

    def cancel_open_orders(self, symbol: str = "", reason: str = "") -> list[OrderResult]:
        """查找并取消开放挂单；指定 symbol 时只处理该股票，为空时处理全部。"""
        # 查询范围在这里一次性确定：symbol 非空只查该股票，空字符串查全部开放单。
        # 查询本身是只读；真正撤单由下方逐笔 cancel_order 执行并再次检查终态。
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
        # 每一笔订单独立返回 OrderResult；其中一笔撤单失败不会伪造其余订单成功。
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
        wait_for_terminal: bool = True,
    ) -> OrderResult:
        """根据交易时段构造 MARKET 或 extended-hours LIMIT 并提交。

        这是市价式买卖路径的真实券商写入函数。同步调用提交成功后进入撤单
        策略；自动监控传入 ``wait_for_terminal=False``，由持久化监督器在后续
        轮次查询状态并在超时后撤销未成交部分。
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        alpaca_symbol = to_alpaca_symbol(symbol)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        if side == "SELL":
            quantity = self._sell_qty(symbol, quantity)
            if quantity <= 0:
                return OrderResult("", symbol, side, 0, current_price, "REJECTED", "卖出数量不足 1 股")
        now_et = now_market_time(self.settings)
        if not skip_time_validation and side == "BUY" and is_premarket_time(now_et):
            return OrderResult("", symbol, side, quantity, current_price, "REJECTED", "盘前时段不买入，已跳过真实买单")
        if not skip_time_validation and side == "BUY" and not is_buy_order_time(now_et):
            return OrderResult("", symbol, side, quantity, current_price, "REJECTED", "买入只允许常规盘开盘后前 2.5 小时，已跳过真实买单")
        if not skip_time_validation and not is_realtime_order_time(now_et):
            return OrderResult("", symbol, side, quantity, current_price, "REJECTED", "当前不在实时价下单时段，已跳过真实下单")

        # 非常规盘只能用 extended-hours limit；常规盘使用 market order。
        if is_regular_market_time(now_et):
            request = MarketOrderRequest(
                symbol=alpaca_symbol,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=self._new_client_order_id(side),
            )
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
                client_order_id=self._new_client_order_id(side),
            )

        try:
            # 【真实券商写入：买入/卖出】
            # 这是 MARKET 或扩展时段保护 LIMIT 最终进入 Alpaca Trading API 的位置。
            # request 已包含股票、方向、数量、订单类型和有效期；此调用成功只代表
            # 券商接收订单，不代表成交；自动监控会先持久化订单身份，再由后续
            # 轮次确认最终状态，手动兼容调用则在本函数继续同步等待。
            raw = self.client.submit_order(order_data=request)
        except Exception as exc:
            raw = self._recover_submitted_order(request.client_order_id, exc)
            if raw is None:
                status = "REJECTED" if self._is_definitive_submit_rejection(exc) else "SUBMIT_UNCONFIRMED"
                return OrderResult("", symbol, side, quantity, current_price, status, short_error(exc))

        submitted = OrderResult(
            str(getattr(raw, "id", "") or ""),
            symbol,
            side,
            quantity,
            current_price,
            normalize_order_status(raw) or "SUBMITTED",
            f"Alpaca {self.source_name()} order submitted",
        )
        if wait_for_terminal:
            notify_order_submitted(self.settings, submitted, reason, broker_name=self.source_name())

        if not wait_for_terminal:
            return _nonblocking_submit_result(raw, submitted, self.source_name())

        # 【订单终态/自动撤单】
        # submit_order 返回后立即交给本轮固定的 CancelStrategy：轮询至最终状态；
        # 超时只撤销未成交剩余量，并再次读取订单，区分成交、部分成交、已撤和未确认。
        try:
            return self._configured_cancel_strategy().wait_for_terminal(
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
        except Exception as exc:
            # submit_order 已经成功，后续策略异常绝不能抛掉 order_id 或伪装成拒单。
            # 保留“已提交且终态未知”的暴露，service 才能暂停后续买入并按唯一订单号
            # 执行兜底撤单；调用方也能据此人工核对券商状态。
            return OrderResult(
                submitted.order_id,
                submitted.symbol,
                submitted.side,
                submitted.quantity,
                submitted.price,
                submitted.status or "SUBMITTED",
                f"{submitted.message}; terminal handling failed, exposure remains unconfirmed: {short_error(exc)}",
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
        wait_for_terminal: bool = True,
    ) -> OrderResult:
        """构造并提交固定价格的 BUY/SELL LIMIT。

        自动监控买入、自动止损卖出以及 OpenClaw 固定限价单都会进入这里。
        自动监控使用非阻塞模式，OpenClaw/兼容调用默认同步等待终态。
        ``skip_time_validation=True`` 只供明确的手动指令使用，不代表不经过券商校验。
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        alpaca_symbol = to_alpaca_symbol(symbol)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        if side == "SELL":
            quantity = self._sell_qty(symbol, quantity)
            if quantity <= 0:
                return OrderResult("", symbol, side, 0, limit_price, "REJECTED", "卖出数量不足 1 股")
        now_et = now_market_time(self.settings)
        if not skip_time_validation and side == "BUY" and is_premarket_time(now_et):
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", "盘前时段不买入，已跳过真实买单")
        if not skip_time_validation and side == "BUY" and not is_buy_order_time(now_et):
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", "买入只允许常规盘开盘后前 2.5 小时，已跳过真实买单")
        if not skip_time_validation and not is_realtime_order_time(now_et):
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", "当前不在实时价下单时段，已跳过真实下单")
        if not skip_time_validation and not is_regular_market_time(now_et) and not self.settings.extended_hours_orders_enabled:
            return OrderResult("", symbol, side, quantity, limit_price, "REJECTED", "当前不在常规盘，且未开启盘前/盘后下单")

        broker_limit_price = normalize_limit_price(limit_price)
        request = LimitOrderRequest(
            symbol=alpaca_symbol,
            qty=quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=broker_limit_price,
            extended_hours=not is_regular_market_time(now_et),
            client_order_id=self._new_client_order_id(side),
        )
        try:
            # 【真实券商写入：固定限价买入/卖出】
            # 自动 BUY LIMIT、自动止损 SELL LIMIT 和手动固定限价单最终都在这里
            # 进入 Alpaca Trading API。返回订单对象仍只是“已受理”，不是成交证明。
            raw = self.client.submit_order(order_data=request)
        except Exception as exc:
            raw = self._recover_submitted_order(request.client_order_id, exc)
            if raw is None:
                status = "REJECTED" if self._is_definitive_submit_rejection(exc) else "SUBMIT_UNCONFIRMED"
                return OrderResult("", symbol, side, quantity, limit_price, status, short_error(exc))

        submitted = OrderResult(
            str(getattr(raw, "id", "") or ""),
            symbol,
            side,
            quantity,
            broker_limit_price,
            normalize_order_status(raw) or "SUBMITTED",
            f"Alpaca {self.source_name()} fixed limit order submitted",
        )
        if wait_for_terminal:
            notify_order_submitted(self.settings, submitted, reason, broker_name=self.source_name())
        if not wait_for_terminal:
            return _nonblocking_submit_result(raw, submitted, self.source_name())
        # 【订单终态/自动撤单】
        # 固定限价单与 MARKET 路径使用同一终态保护，避免限价单超时后继续裸露；
        # Broker 只有拿到策略确认后的 OrderResult 才返回 service/手动调用方。
        try:
            return self._configured_cancel_strategy().wait_for_terminal(
                self.client,
                raw,
                symbol,
                side,
                quantity,
                broker_limit_price,
                self.source_name(),
                timeout_seconds=self.settings.order_cancel_after_seconds,
                poll_seconds=self.settings.order_status_poll_seconds,
            )
        except Exception as exc:
            # 固定限价单也必须保留 submit_order 已返回的订单身份和开放状态。
            # 这里返回未确认结果而不是继续抛异常，后面的九阶段循环才有机会安全撤单。
            return OrderResult(
                submitted.order_id,
                submitted.symbol,
                submitted.side,
                submitted.quantity,
                submitted.price,
                submitted.status or "SUBMITTED",
                f"{submitted.message}; terminal handling failed, exposure remains unconfirmed: {short_error(exc)}",
            )

    def _configured_cancel_strategy(self):
        """取得当前配置的撤单策略；惰性分支用于兼容绕过 ``__init__`` 的隔离测试。"""
        cancel_strategy = getattr(self, "cancel_strategy", None)
        if cancel_strategy is None:
            cancel_strategy = resolve_strategy_runtime(self.settings).cancel
            self.cancel_strategy = cancel_strategy
        return cancel_strategy

    def _pending_orders(self) -> PendingOrderStore:
        store = getattr(self, "pending_order_store", None)
        if store is None:
            store = PendingOrderStore(self.settings.output_dir)
            self.pending_order_store = store
        return store

    def _register_pending_result(
        self,
        result: OrderResult,
        *,
        reason: str,
        strategy_action: str,
        strategy_notional: float,
    ) -> None:
        if not has_unconfirmed_order_status(result.status):
            return
        if not result.order_id:
            self.order_safety_error = "automatic order returned an unconfirmed status without order_id"
            return
        submitted_at = now_market_time(self.settings)
        try:
            self._pending_orders().register(
                order_id=result.order_id,
                symbol=result.symbol,
                side=result.side,
                requested_quantity=result.quantity,
                requested_price=result.price,
                reason=reason,
                strategy_action=strategy_action,
                strategy_notional=strategy_notional,
                submitted_at=submitted_at,
                status=result.status,
            )
        except Exception as exc:
            self.order_safety_error = (
                f"submitted order {result.order_id} could not be persisted for supervision: {short_error(exc)}"
            )
            print(
                "[严重] 订单已提交但无法写入待确认订单状态；后续自动买入暂停，"
                f"必须按订单号人工核对：{self.order_safety_error}",
                flush=True,
            )

    def _protective_pending_orders(self, symbol: str):
        normalized = normalize_symbol(symbol)
        return [
            order
            for order in self._pending_orders().orders.values()
            if order.symbol == normalized
            and order.side == "SELL"
            and order.strategy_action == BROKER_PROTECTIVE_STOP_ACTION
        ]

    def _submit_protective_stop(
        self,
        symbol: str,
        quantity: float,
        stop_price: float,
        now_et: datetime,
    ) -> OrderResult:
        """提交券商原生 GTC STOP MARKET，并在任何后续动作前持久化订单身份。"""

        from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        client_order_id = f"{BROKER_PROTECTIVE_STOP_CLIENT_PREFIX}{uuid.uuid4().hex}"
        request = StopOrderRequest(
            symbol=to_alpaca_symbol(symbol),
            qty=quantity,
            side=OrderSide.SELL,
            type=OrderType.STOP,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price,
            client_order_id=client_order_id,
        )
        reason = f"券商端保护止损：加权成本下方 {abs(self.settings.broker_protective_stop_pct):.2%} STOP MARKET"
        try:
            raw = self.client.submit_order(order_data=request)
        except Exception as exc:
            raw = self._recover_submitted_order(client_order_id, exc)
            if raw is None:
                status = "REJECTED" if self._is_definitive_submit_rejection(exc) else "SUBMIT_UNCONFIRMED"
                result = OrderResult("", symbol, "SELL", quantity, stop_price, status, short_error(exc))
                self._latch_protective_stop_error(symbol, f"保护单提交失败：{result.message}")
                return self._record_result(result, reason)

        result = _nonblocking_submit_result(
            raw,
            OrderResult(
                str(getattr(raw, "id", "") or ""),
                symbol,
                "SELL",
                quantity,
                stop_price,
                normalize_order_status(raw) or "SUBMITTED",
                f"Alpaca {self.source_name()} protective stop submitted",
            ),
            self.source_name(),
        )
        if not result.order_id:
            self._latch_protective_stop_error(symbol, "保护单已提交但券商未返回订单号")
            return self._record_result(result, reason)
        try:
            # 即便 submit 响应已是终态也保留一轮，统一对账才能把可能的立即成交
            # 应用到三档计划，不能因 has_unconfirmed_order_status=False 丢掉成交。
            self._pending_orders().register(
                order_id=result.order_id,
                symbol=symbol,
                side="SELL",
                requested_quantity=quantity,
                requested_price=stop_price,
                reason=reason,
                strategy_action=BROKER_PROTECTIVE_STOP_ACTION,
                strategy_notional=0.0,
                submitted_at=now_et,
                status=result.status,
            )
        except Exception as exc:
            self._latch_protective_stop_error(
                symbol,
                f"保护单 {result.order_id} 无法写入监督状态：{short_error(exc)}",
            )
        notify_order_submitted(self.settings, result, reason, broker_name=self.source_name())
        return self._record_result(result, reason)

    def _replace_protective_stop(self, order, quantity: float, stop_price: float, now_et: datetime) -> None:
        """按最新持仓数量和加权成本替换保护单，旧单身份留给替换链对账。"""

        from alpaca.trading.requests import ReplaceOrderRequest

        client_order_id = f"{BROKER_PROTECTIVE_STOP_CLIENT_PREFIX}{uuid.uuid4().hex}"
        request = ReplaceOrderRequest(
            qty=quantity,
            stop_price=stop_price,
            client_order_id=client_order_id,
        )
        try:
            raw = self.client.replace_order_by_id(order.active_order_id, order_data=request)
        except Exception as exc:
            raw = self._recover_submitted_order(client_order_id, exc)
            if raw is None:
                self._latch_protective_stop_error(
                    order.symbol,
                    f"保护单 {order.active_order_id} 替换结果未知：{short_error(exc)}",
                )
                return
        replacement_id = str(getattr(raw, "id", "") or "")
        if not replacement_id:
            self._latch_protective_stop_error(order.symbol, "保护单替换成功但券商未返回新订单号")
            return
        # 不直接跳到 replacement_id：下一轮从旧单的 REPLACED/replaced_by 链推进，
        # 可累计替换瞬间旧订单可能发生的真实成交，避免少算卖出数量。
        order.requested_quantity = quantity
        order.requested_price = stop_price
        order.cancel_requested_at = ""
        order.updated_at = now_et.isoformat()
        self._pending_orders().save(now_et)
        reason = f"按最新持仓更新 -8% 券商保护单；replacement={replacement_id}"
        result = OrderResult(
            replacement_id,
            order.symbol,
            "SELL",
            quantity,
            stop_price,
            normalize_order_status(raw) or "SUBMITTED",
            "Alpaca protective stop replaced",
        )
        notify_order_submitted(self.settings, result, reason, broker_name=self.source_name())
        self._record_result(result, reason)

    def _adopt_protective_order(self, raw, now_et: datetime) -> None:
        """收编券商端仍开放但本地状态缺失的 ma5-stop 订单，支持进程重启恢复。"""

        order_id = str(getattr(raw, "id", "") or "")
        if not order_id:
            return
        store = self._pending_orders()
        if order_id in store.orders or any(order.active_order_id == order_id for order in store.orders.values()):
            return
        symbol = _raw_order_symbol(raw)
        quantity = _raw_order_quantity(raw)
        stop_price = _raw_order_stop_price(raw)
        if not symbol or quantity <= 0 or stop_price <= 0:
            self._latch_protective_stop_error(symbol or "UNKNOWN", "券商保护单缺少股票、数量或 stop_price")
            return
        submitted_at = _raw_order_datetime(raw, "submitted_at") or now_et
        try:
            store.register(
                order_id=order_id,
                symbol=symbol,
                side="SELL",
                requested_quantity=quantity,
                requested_price=stop_price,
                reason="重启后收编券商端 -8% 保护 STOP MARKET",
                strategy_action=BROKER_PROTECTIVE_STOP_ACTION,
                strategy_notional=0.0,
                submitted_at=submitted_at,
                status=normalize_order_status(raw) or "SUBMITTED",
            )
        except Exception as exc:
            self._latch_protective_stop_error(symbol, f"无法收编券商保护单 {order_id}：{short_error(exc)}")

    def _latch_protective_stop_error(self, symbol: str, message: str) -> None:
        self.protective_stop_error = f"{normalize_symbol(symbol)} protective stop unsafe: {message}"
        print(f"[严重] {self.protective_stop_error}；后续自动买入暂停，请核对 Alpaca 订单。", flush=True)

    def reconcile_pending_orders(self, now_et: datetime) -> list[PendingOrderEvent]:
        """Poll every managed order once and request overdue cancellation without blocking."""

        store = self._pending_orders()
        self.recently_reconciled_buy_symbols = set()
        self.recently_reconciled_sell_symbols = set()
        self.recently_released_protective_symbols = set()
        events: list[PendingOrderEvent] = []
        changed = False
        for order in list(store.orders.values()):
            try:
                raw = self.client.get_order_by_id(order.active_order_id)
                raw, replacement_changed = self._follow_pending_replacements(order, raw, now_et)
                changed = replacement_changed or changed
            except Exception as exc:
                print(
                    f"[提示] 待确认订单 {order.active_order_id} 状态查询失败，保留风险锁：{short_error(exc)}",
                    flush=True,
                )
                continue

            raw_status = normalize_order_status(raw) or order.last_status or "SUBMITTED"
            terminal = raw_status in FINAL_STATUSES
            cancel_requested_now = False
            if (
                not terminal
                and order.strategy_action != BROKER_PROTECTIVE_STOP_ACTION
                and _pending_order_cancel_due(order, now_et, self.settings.order_cancel_after_seconds)
            ):
                try:
                    self.client.cancel_order_by_id(order.active_order_id)
                    order.cancel_requested_at = now_et.isoformat()
                    cancel_requested_now = True
                    changed = True
                except Exception as exc:
                    print(
                        f"[提示] 待确认订单 {order.active_order_id} 自动撤单失败，将继续监督：{short_error(exc)}",
                        flush=True,
                    )
                try:
                    raw = self.client.get_order_by_id(order.active_order_id)
                    raw_status = normalize_order_status(raw) or raw_status
                    terminal = raw_status in FINAL_STATUSES
                except Exception:
                    pass

            active_filled_quantity = filled_quantity(raw)
            active_fill_price = _filled_avg_price(raw, order.requested_price)
            cumulative_quantity = order.active_order_base_quantity + active_filled_quantity
            cumulative_value = order.active_order_base_value + active_filled_quantity * active_fill_price
            cumulative_avg_price = (
                cumulative_value / cumulative_quantity if cumulative_quantity > 0 else 0.0
            )
            effective_status = _pending_effective_status(
                raw_status,
                cumulative_quantity,
                cancel_requested=bool(order.cancel_requested_at) or cancel_requested_now,
            )
            event = PendingOrderEvent(
                tracking_order_id=order.tracking_order_id,
                active_order_id=order.active_order_id,
                symbol=order.symbol,
                side=order.side,
                requested_quantity=order.requested_quantity,
                requested_price=order.requested_price,
                reason=order.reason,
                strategy_action=order.strategy_action,
                strategy_notional=order.strategy_notional,
                status=effective_status,
                filled_quantity=cumulative_quantity,
                filled_avg_price=cumulative_avg_price,
                terminal=terminal,
            )

            order.last_status = effective_status
            order.last_filled_quantity = cumulative_quantity
            order.last_filled_avg_price = cumulative_avg_price
            order.updated_at = now_et.isoformat()
            changed = True
            if event.record_key() != (
                order.recorded_status,
                round(float(order.recorded_filled_quantity), 9),
            ):
                result_quantity = cumulative_quantity if cumulative_quantity > 0 else order.requested_quantity
                result_price = cumulative_avg_price if cumulative_avg_price > 0 else order.requested_price
                self._record_result(
                    OrderResult(
                        order.active_order_id,
                        order.symbol,
                        order.side,
                        result_quantity,
                        result_price,
                        effective_status,
                        "Alpaca managed order state reconciled",
                    ),
                    order.reason,
                )
                if not (getattr(self, "order_safety_error", "") or getattr(self, "order_recording_error", "")):
                    order.recorded_status, order.recorded_filled_quantity = event.record_key()
            events.append(event)
            if (
                event.strategy_action == BROKER_PROTECTIVE_STOP_ACTION
                and event.terminal
                and event.filled_quantity <= 0
            ):
                self.recently_released_protective_symbols.add(event.symbol)
            if event.terminal and event.filled_quantity > 0:
                recently_reconciled = (
                    self.recently_reconciled_buy_symbols
                    if event.side == "BUY"
                    else self.recently_reconciled_sell_symbols
                )
                recently_reconciled.add(event.symbol)

        if changed:
            store.save(now_et)
        return events

    def acknowledge_pending_order(self, tracking_order_id: str, now_et: datetime) -> None:
        """Remove a terminal order only after the service has applied its cumulative fill."""

        self._pending_orders().remove(tracking_order_id, now_et)

    def _follow_pending_replacements(self, order, raw, now_et: datetime):
        changed = False
        seen = {order.active_order_id}
        for _ in range(8):
            if normalize_order_status(raw) != "REPLACED":
                return raw, changed
            replacement_id = str(getattr(raw, "replaced_by", "") or "")
            if not replacement_id or replacement_id in seen:
                raise RuntimeError("替换订单链缺少有效 replaced_by")
            active_quantity = filled_quantity(raw)
            active_price = _filled_avg_price(raw, order.requested_price)
            order.active_order_base_quantity += active_quantity
            order.active_order_base_value += active_quantity * active_price
            order.active_order_id = replacement_id
            order.updated_at = now_et.isoformat()
            seen.add(replacement_id)
            changed = True
            raw = self.client.get_order_by_id(replacement_id)
        raise RuntimeError("替换订单链超过 8 层，停止自动推进")

    def _get_open_orders(self, symbol: str):
        """读取 Alpaca open orders，撤单指令会用它按股票代码定位挂单。"""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        symbols = [to_alpaca_symbol(symbol)] if symbol else None
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=symbols)
        return self.client.get_orders(filter=request)

    def _record_result(self, result: OrderResult, reason: str) -> OrderResult:
        """统一写 CSV 和通知；落盘失败时锁存风险但保留真实订单结果。"""
        order_time = now_market_time(self.settings)
        try:
            record_order_and_notify(
                self.settings,
                result,
                reason,
                broker_name=self.source_name(),
                order_time=order_time,
            )
        except Exception as exc:
            # 订单可能已经在券商成交，不能因本地 CSV 写失败而把它改成 REJECTED，
            # 也不能让异常越过 service 的计数逻辑后继续下下一单。锁存错误后，
            # 当前轮和后续轮都会暂停自动买入，直到进程重启并人工核对本地账本。
            self.order_recording_error = short_error(exc)
            self.order_safety_error = self.order_recording_error
            print(
                "[严重] 订单结果无法写入本地账本；为防止每日买入限额失真，"
                f"后续自动买入将暂停：{self.order_recording_error}",
                flush=True,
            )
        return result

    def _new_client_order_id(self, side: str) -> str:
        """生成可供提交异常后反查的唯一客户端订单号。"""

        return f"ma5-{side.lower()}-{uuid.uuid4().hex}"

    def _recover_submitted_order(self, client_order_id: str, submit_error: Exception):
        """submit 异常后按 client_order_id 反查，区分明确拒单和未知收单。"""

        if self._is_definitive_submit_rejection(submit_error):
            return None
        getter = getattr(self.client, "get_order_by_client_id", None)
        if callable(getter):
            try:
                raw_order = getter(client_order_id)
                if getattr(raw_order, "id", None):
                    return raw_order
            except Exception:
                pass
        self.order_safety_error = (
            f"submit outcome unknown; client_order_id={client_order_id}: "
            f"{short_error(submit_error)}"
        )
        print(
            "[严重] Alpaca 下单请求结果无法确认；为防止重复下单，"
            f"后续自动买入将暂停：{self.order_safety_error}",
            flush=True,
        )
        return None

    @staticmethod
    def _is_definitive_submit_rejection(exc: Exception) -> bool:
        """4xx 参数/资金类错误是明确拒单；超时、限流、冲突和服务错误仍可能已收单。"""

        try:
            status_code = int(getattr(exc, "status_code", 0) or 0)
        except (TypeError, ValueError):
            return False
        return 400 <= status_code < 500 and status_code not in {408, 409, 425, 429}

    def _buy_qty(self, symbol: str, notional_usd: float, current_price: float) -> float:
        """把买入金额换算成整数股下单股数。"""
        if current_price <= 0:
            return 0.0
        return float(math.floor(notional_usd / current_price))

    def _sell_qty(self, symbol: str, quantity: float) -> float:
        """卖出前按 Alpaca 碎股权限规整数量，避免非碎股资产被拒单。"""
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(qty) or qty <= 0:
            return 0.0

        rounded_qty = round(qty)
        if math.isclose(qty, rounded_qty, rel_tol=0.0, abs_tol=1e-6):
            return float(rounded_qty)
        if self._can_sell_fractional(symbol):
            return round(qty, 6)
        return float(math.floor(qty))

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

    def _can_sell_fractional(self, symbol: str) -> bool:
        """卖出碎股只看 Alpaca asset 权限；查询失败时保守改用整数股。"""
        try:
            asset = self.client.get_asset(to_alpaca_symbol(symbol))
            return bool(getattr(asset, "fractionable", False))
        except Exception as exc:
            print(f"{symbol}: 查询 Alpaca 碎股权限失败，卖出改用整数股。{short_error(exc)}", flush=True)
            return False

    def _extended_limit_price(self, side: str, current_price: float) -> float:
        """生成盘前/盘后限价单的保护价。"""
        buffer = self.settings.extended_hours_limit_buffer_pct
        if side == "BUY":
            return normalize_limit_price(current_price * (1.0 + buffer))
        return normalize_limit_price(current_price * (1.0 - buffer))

    def source_name(self) -> str:
        """返回当前真实交易通道名称。"""
        return "alpaca-paper" if self.paper else "alpaca-live"


def normalize_limit_price(price: float) -> float:
    """Use cents at $1+, and four decimal places below $1, without collapsing low-price tiers."""

    value = Decimal(str(price))
    if not value.is_finite() or value <= 0:
        raise ValueError("limit price must be finite and positive")
    quantum = Decimal("0.01") if value >= Decimal("1") else Decimal("0.0001")
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _nonblocking_submit_result(raw_order, submitted: OrderResult, source_name: str) -> OrderResult:
    """Return immediately while preserving an already-terminal fill from the submit response."""

    status = normalize_order_status(raw_order) or submitted.status or "SUBMITTED"
    if status not in FINAL_STATUSES:
        return OrderResult(
            submitted.order_id,
            submitted.symbol,
            submitted.side,
            submitted.quantity,
            submitted.price,
            "SUBMITTED",
            f"Alpaca {source_name} order accepted for non-blocking supervision; broker_status={status}",
        )
    quantity = filled_quantity(raw_order)
    price = _filled_avg_price(raw_order, submitted.price)
    if quantity > 0 and status != "FILLED":
        status = f"PARTIALLY_FILLED_{status}"
    return OrderResult(
        submitted.order_id,
        submitted.symbol,
        submitted.side,
        quantity if quantity > 0 else submitted.quantity,
        price,
        status,
        f"Alpaca {source_name} order returned terminal status={status} at submit",
    )


def _filled_avg_price(raw_order, fallback: float) -> float:
    try:
        value = float(getattr(raw_order, "filled_avg_price", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else float(fallback)


def _pending_order_cancel_due(order, now_et: datetime, timeout_seconds: int) -> bool:
    submitted_at = datetime.fromisoformat(order.submitted_at)
    now_value = _compatible_datetime(now_et, submitted_at)
    if (now_value - submitted_at).total_seconds() < max(0, timeout_seconds):
        return False
    if not order.cancel_requested_at:
        return True
    requested_at = datetime.fromisoformat(order.cancel_requested_at)
    now_value = _compatible_datetime(now_et, requested_at)
    return (now_value - requested_at).total_seconds() >= 30.0


def _compatible_datetime(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def _pending_effective_status(raw_status: str, filled_qty: float, *, cancel_requested: bool) -> str:
    status = str(raw_status or "SUBMITTED").upper()
    if status in FINAL_STATUSES:
        if filled_qty > 0 and status != "FILLED":
            return f"PARTIALLY_FILLED_{status}"
        return status
    if cancel_requested:
        return "PARTIALLY_FILLED_CANCEL_REQUESTED" if filled_qty > 0 else "CANCEL_REQUESTED"
    if filled_qty > 0:
        return "PARTIALLY_FILLED"
    return status


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


def _raw_order_stop_price(raw_order) -> float:
    try:
        value = float(getattr(raw_order, "stop_price", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _raw_order_client_order_id(raw_order) -> str:
    return str(getattr(raw_order, "client_order_id", "") or "")


def _is_managed_protective_raw_order(raw_order) -> bool:
    return (
        _raw_order_side(raw_order) == "SELL"
        and _raw_order_client_order_id(raw_order).startswith(BROKER_PROTECTIVE_STOP_CLIENT_PREFIX)
    )


def _same_order_quantity(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-6)


def _raw_order_datetime(raw_order, field: str) -> datetime | None:
    value = getattr(raw_order, field, None)
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
