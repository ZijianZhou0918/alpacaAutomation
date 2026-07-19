"""订单执行层：把服务层的买卖意图转换成 Alpaca 订单。

阅读交易写入逻辑时先看 ``AlpacaStockBroker``：
- ``place_*`` 是业务层调用入口；
- ``_submit_order`` / ``_submit_fixed_limit_order`` 构造并真实提交订单；
- ``cancel_order`` 把手动撤单交给可配置撤单策略；
- 每笔提交后都会等待终态，超时撤单的实际 SDK 调用位于 ``order_guard.py``。
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime

from .alpaca_connection import build_trading_connection
from .config import Settings
from .errors import short_error
from .market_time import is_buy_order_time, is_premarket_time, is_realtime_order_time, is_regular_market_time, now_market_time
from .models import OrderResult, Position
from .order_guard import FINAL_STATUSES, filled_quantity, normalize_order_status
from .state import append_order, load_positions, save_positions
from .strategy_framework import resolve_strategy_runtime
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

    def __init__(self, settings: Settings):
        """建立交易连接并保存当前账户模式。"""
        self.settings = settings
        self.cancel_strategy = resolve_strategy_runtime(settings).cancel
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
        symbols: set[str] = set()
        for raw in self._get_open_orders(""):
            if _raw_order_side(raw) != "BUY":
                continue
            symbol = normalize_symbol(getattr(raw, "symbol", ""))
            if symbol:
                symbols.add(symbol)
        return symbols

    def get_open_sell_order_symbols(self) -> set[str]:
        """读取 Alpaca 当前开放卖单，避免下一轮对同一持仓重复卖出。"""
        symbols: set[str] = set()
        for raw in self._get_open_orders(""):
            if _raw_order_side(raw) != "SELL":
                continue
            symbol = normalize_symbol(getattr(raw, "symbol", ""))
            if symbol:
                symbols.add(symbol)
        return symbols

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

        自动监控的买入最终会到这里，再进入 ``_submit_fixed_limit_order``。
        """
        # 【限价买入 1/2：金额转整数股】
        # 自动监控按美元预算下单；这里用限价计算可买整数股，避免提交分数股。
        # 预算不足 1 股时只返回 REJECTED，不会尝试扩大金额或改成市价单。
        qty = self._buy_qty(symbol, notional_usd, limit_price)
        if qty <= 0:
            result = OrderResult("", symbol, "BUY", 0, limit_price, "REJECTED", "买入金额不足")
            return self._record_result(result, reason)

        # 【限价买入 2/2：进入统一真实提交与终态保护】
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
    ) -> OrderResult:
        """根据交易时段构造 MARKET 或 extended-hours LIMIT 并提交。

        这是市价式买卖路径的真实券商写入函数；提交成功后立即进入撤单策略，
        轮询订单状态，并在配置超时时间到达后请求撤销未成交部分。
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
            # 券商接收订单，不代表成交，所以下面必须继续等待并确认最终状态。
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
        notify_order_submitted(self.settings, submitted, reason, broker_name=self.source_name())

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
    ) -> OrderResult:
        """构造并提交固定价格的 BUY/SELL LIMIT。

        自动监控买入、自动止损卖出以及 OpenClaw 固定限价单都会进入这里。
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

        request = LimitOrderRequest(
            symbol=alpaca_symbol,
            qty=quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
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
            limit_price,
            normalize_order_status(raw) or "SUBMITTED",
            f"Alpaca {self.source_name()} fixed limit order submitted",
        )
        notify_order_submitted(self.settings, submitted, reason, broker_name=self.source_name())
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
                limit_price,
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
