from __future__ import annotations

import math
import uuid
from datetime import datetime

from .alpaca_connection import build_trading_connection
from .config import Settings
from .errors import short_error
from .market_time import is_regular_market_time, now_market_time
from .models import OrderResult, Position
from .order_guard import wait_for_fill_or_cancel
from .state import append_order, load_positions, save_positions
from .watchlist import normalize_symbol, to_alpaca_symbol


class DryRunStockBroker:
    """本地 dry-run broker，只用于测试或不用 Alpaca key 的演练。"""

    def __init__(self, settings: Settings):
        """保存 dry-run 需要的本地文件路径配置。"""
        self.settings = settings

    def source_name(self) -> str:
        """返回日志中显示的 broker 名称。"""
        return "dry-run"

    def get_positions(self) -> dict[str, Position]:
        """读取本地模拟持仓，供策略卖出判断使用。"""
        return load_positions(self.settings.state_file)

    def place_market_buy(self, symbol: str, notional_usd: float, current_price: float, reason: str) -> OrderResult:
        """模拟买入并写入本地持仓/订单记录，不提交到 Alpaca。"""
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
        append_order(self.settings.output_dir, result, reason)
        return result

    def place_market_sell(self, symbol: str, quantity: float, current_price: float, reason: str) -> OrderResult:
        """模拟卖出并更新本地持仓/订单记录，不提交到 Alpaca。"""
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
        append_order(self.settings.output_dir, result, reason)
        return result


class AlpacaStockBroker:
    """Alpaca 官方股票交易适配器，根据 .env 里的 key 自动连接 paper/live。"""

    def __init__(self, settings: Settings):
        """启动时识别 .env key 的 paper/live 模式并保存交易 client。"""
        self.settings = settings
        connection = build_trading_connection()
        self.client = connection.client
        self.account = connection.account
        self.paper = connection.paper

    def get_positions(self) -> dict[str, Position]:
        """从 Alpaca 读取真实持仓，并转换成策略统一使用的 Position。"""
        positions: dict[str, Position] = {}
        for raw in self.client.get_all_positions():
            symbol = normalize_symbol(getattr(raw, "symbol", ""))
            qty = float(getattr(raw, "qty", 0) or 0)
            if not symbol or qty <= 0:
                continue
            avg_price = float(getattr(raw, "avg_entry_price", 0) or 0)
            positions[symbol] = Position(symbol, qty, avg_price, "alpaca", source=self.source_name())
        return positions

    def place_market_buy(self, symbol: str, notional_usd: float, current_price: float, reason: str) -> OrderResult:
        """按金额计算股数后提交 Alpaca 买单；失败时返回 REJECTED。"""
        qty = self._buy_qty(notional_usd, current_price)
        if qty <= 0:
            return OrderResult("", symbol, "BUY", 0, current_price, "REJECTED", "买入金额不足")
        result = self._submit_order(symbol, "BUY", qty, current_price)
        append_order(self.settings.output_dir, result, reason)
        return result

    def place_market_sell(self, symbol: str, quantity: float, current_price: float, reason: str) -> OrderResult:
        """提交 Alpaca 卖单；失败时返回 REJECTED。"""
        if quantity <= 0:
            return OrderResult("", symbol, "SELL", 0, current_price, "REJECTED", "没有可卖持仓")
        result = self._submit_order(symbol, "SELL", quantity, current_price)
        append_order(self.settings.output_dir, result, reason)
        return result

    def _submit_order(self, symbol: str, side: str, quantity: float, current_price: float) -> OrderResult:
        """根据盘中/盘前盘后选择订单类型，并真正调用 Alpaca submit_order。"""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        alpaca_symbol = to_alpaca_symbol(symbol)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        now_et = now_market_time(self.settings)

        # 盘前/盘后必须使用 extended-hours limit order，常规盘用 market order。
        if is_regular_market_time(now_et):
            request = MarketOrderRequest(symbol=alpaca_symbol, qty=quantity, side=order_side, time_in_force=TimeInForce.DAY)
        else:
            if not self.settings.extended_hours_orders_enabled:
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

    def _buy_qty(self, notional_usd: float, current_price: float) -> float:
        """把买入金额换算成股数，按配置决定是否允许碎股。"""
        if current_price <= 0:
            return 0.0
        qty = notional_usd / current_price
        if not self.settings.allow_fractional_shares:
            return float(math.floor(qty))
        return round(qty, 6)

    def _extended_limit_price(self, side: str, current_price: float) -> float:
        """盘前/盘后限价单使用的小幅保护价格。"""
        buffer = self.settings.extended_hours_limit_buffer_pct
        if side == "BUY":
            return round(current_price * (1.0 + buffer), 2)
        return round(current_price * (1.0 - buffer), 2)

    def source_name(self) -> str:
        """返回日志中显示的 Alpaca paper/live 名称。"""
        return "alpaca-paper" if self.paper else "alpaca-live"
