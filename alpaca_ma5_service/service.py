"""盘中自动交易编排层：连接策略判断、统一风控和 Broker 下单。

本模块自己不直接调用 Alpaca SDK。它先让买入/卖出策略产生 ``Signal``，
再检查交易时段、每日次数、重复订单等统一风控，最后才调用 ``broker``。
真正的 Alpaca 写入在 ``broker.py``，超时撤单在 ``order_guard.py``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .broker import AlpacaStockBroker
from .config import Settings, build_settings
from .errors import short_error
from .market_data import build_market_data as build_default_market_data
from .market_time import (
    is_buy_order_time,
    is_intraday_monitor_finished,
    is_premarket_time,
    is_realtime_order_time,
    next_poll_seconds,
    now_market_time,
    seconds_until_intraday_monitor_end,
)
from .models import MarketSnapshot, OrderResult, Signal, has_unconfirmed_order_status, is_executed_order_status
from .openclaw_notify import safe_send_openclaw_messages
from .run_lock import acquire_run_lock
from .state import append_daily_buy_exclusion, count_today_buy_orders, count_today_symbol_order_errors, count_today_symbol_take_profit_half_sells, is_symbol_daily_buy_excluded
from .strategy_framework import StrategyRuntime, resolve_strategy_runtime
from .watchlist import normalize_symbol, read_watch_codes


DISPLAY_TIMEZONE = ZoneInfo("America/Los_Angeles")


@dataclass
class StopLossSession:
    """记录本监控进程启动时已有的亏损持仓，避免启动瞬间误清旧仓。"""
    initial_symbols: set[str]
    checked_initial_symbols: set[str] = field(default_factory=set)
    grandfathered_positions: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class MonitorTableRow:
    symbol: str
    action: str
    has_market_data: bool = False
    current_price: float = 0.0
    today_open: float = 0.0
    today_ma5: float = 0.0
    today_open_ma5: float = 0.0
    signal_gain_pct: float = 0.0
    current_gain_pct: float = 0.0
    order_price: float = 0.0
    order_status: str = ""
    reason: str = ""


@dataclass
class TradingRoundContext:
    """一轮交易检查共享的全部状态，避免关键函数之间传递大量零散参数。"""

    settings: Settings
    strategies: StrategyRuntime
    now_et: datetime
    broker: object
    market_data: object | None
    watch_codes: list[str]
    watch_symbols: set[str]
    positions: dict[str, object]
    symbols: list[str]
    open_buy_order_symbols: set[str] | None
    open_sell_order_symbols: set[str] | None
    stop_loss_session: StopLossSession
    can_order_now: bool
    can_buy_now: bool
    summary: dict[str, int]
    table_rows: list[MonitorTableRow] = field(default_factory=list)
    buys_used: int = 0
    buy_notional: float = 0.0
    buying_paused: bool = False
    owns_market_data: bool = False


@dataclass
class SymbolTradeCycle:
    """单只股票在九阶段核心循环中的共享状态。

    ``run_once`` 只创建一次该对象，然后依次交给买入、卖出和撤单三组函数。
    每个阶段只读写这里的显式字段，避免再通过 ``process_*`` 包装函数层层跳转。
    """

    symbol: str
    position: object | None = None
    route: str = ""
    snapshot: MarketSnapshot | None = None
    signal: Signal | None = None
    result: OrderResult | None = None
    row: MonitorTableRow | None = None
    outcome: str = ""
    exclusion_reason: str = ""
    notification_deferred: bool = False
    cancel_required: bool = False
    cancel_result: OrderResult | None = None
    paused_buying: bool = False
    buy_slot_counted: bool = False
    notified: bool = False


_STOP_LOSS_SESSIONS: dict[str, StopLossSession] = {}


def build_broker(settings: Settings):
    """创建真实交易通道；单测会传入 fake broker 避免触碰 Alpaca。"""
    return AlpacaStockBroker(settings)


def build_market_data(settings: Settings):
    """创建默认行情源：Moomoo 负责实时价，Alpaca 负责日线。"""
    return build_default_market_data(settings)


def run_once(
    settings: Settings | None = None,
    market_data=None,
    broker=None,
    now: datetime | None = None,
) -> dict[str, int]:
    """执行一轮盘中交易；核心逐股流程直接平铺为九个可检索阶段。"""

    # 【核心交易入口 1/4：准备本轮上下文】
    # 这里先解析并冻结 WatchCode/买入/卖出/撤单四类策略，再读取观察池和持仓。
    # 调用方没有注入 broker 时，prepare_trading_round 会创建 AlpacaStockBroker；
    # 因此 run_once 不是纯策略计算函数，而是可能连接当前 Paper/Live 账户的交易入口。
    trading_round = prepare_trading_round(settings or build_settings(), market_data, broker, now)
    if trading_round is None:
        return {"watch": 0, "buy": 0, "sell": 0, "hold": 0, "errors": 0}

    try:
        # 【核心交易入口 2/4：建立行情和本轮资金预算】
        # 这里只创建行情源、读取今日已买次数，并计算每个候选可用金额，尚未下单。
        start_trading_round(trading_round)

        # 【核心交易入口 3/4：九阶段逐股循环】
        # 这是盘中自动交易最核心、也最应该先看到的调用顺序。买入、卖出、撤单
        # 不再藏在 process_symbol -> process_* -> execute_* 的多层包装中。
        for symbol in trading_round.symbols:
            cycle = SymbolTradeCycle(
                symbol=symbol,
                position=trading_round.positions.get(symbol),
            )
            try:
                # 买入三阶段：统一风控与策略判断 -> 真实提交 -> 本轮结果通知。
                check_buy(trading_round, cycle)
                execute_buy(trading_round, cycle)
                notify_buy(trading_round, cycle)

                # 卖出三阶段：持仓退出判断 -> 真实提交 -> 本轮结果通知。
                check_sell(trading_round, cycle)
                execute_sell(trading_round, cycle)
                notify_sell(trading_round, cycle)

                # 撤单三阶段：检查未确认暴露 -> 必要时兜底撤单 -> 写入最终展示结果。
                check_cancel(trading_round, cycle)
                execute_cancel(trading_round, cycle)
                notify_cancel(trading_round, cycle)

                # symbols 正常只来自 WatchCode 或当前持仓；保留失败关闭分支，防止
                # 将来调用方传入第三类股票后悄悄漏掉监控结果。
                if not cycle.notified:
                    record_result(
                        trading_round,
                        "hold",
                        monitor_row(symbol, "跳过", reason="不在观察池且无持仓"),
                    )
            except Exception as exc:
                # 单只股票失败不打断本轮其他股票；若前面已经落表，则不重复计数。
                if not cycle.notified:
                    record_result(
                        trading_round,
                        "errors",
                        monitor_row(symbol, "错误", reason=f"{type(exc).__name__}: {short_error(exc)}"),
                    )

        # 汇总只统计本轮观察、买入、卖出、持有和错误数量，不改变订单状态。
        return finish_trading_round(trading_round)
    finally:
        # 【核心交易入口 4/4：释放本轮自建资源】
        # 即使单股行情或订单处理抛出异常，也会关闭本轮自行创建的行情对象；
        # 已提交订单的终态等待和超时撤单已在 Broker 返回前处理，不在这里补做。
        close_trading_round(trading_round)


def prepare_trading_round(
    settings: Settings,
    market_data,
    broker,
    now: datetime | None,
) -> TradingRoundContext | None:
    """准备策略、持仓和观察池；配置错误会在行情和订单写入前失败。"""

    strategies = resolve_strategy_runtime(settings)
    now_et = now or now_market_time(settings)
    watch_codes = read_watch_codes(settings.watch_codes_file)
    watch_symbols = set(watch_codes)
    broker = broker or build_broker(settings)
    positions = broker.get_positions()
    symbols = watch_codes + [symbol for symbol in positions if symbol not in watch_symbols]

    if not symbols:
        print(f"watch_codes 文件为空或不存在：{settings.watch_codes_file}；当前没有持仓需要风控")
        return None

    # 保留原有初始化顺序：先建立旧仓止损保护，再查询开放买单和开放卖单。
    # 两类查询任一失败时不会猜测“没有挂单”；对应方向会在检查阶段失败关闭。
    stop_loss_session = stop_loss_session_for(settings, positions)
    open_buy_order_symbols = open_buy_order_symbols_for_run(broker) if watch_codes else set()
    open_sell_order_symbols = open_sell_order_symbols_for_run(broker) if positions else set()

    return TradingRoundContext(
        settings=settings,
        strategies=strategies,
        now_et=now_et,
        broker=broker,
        market_data=market_data,
        watch_codes=watch_codes,
        watch_symbols=watch_symbols,
        positions=positions,
        symbols=symbols,
        open_buy_order_symbols=open_buy_order_symbols,
        open_sell_order_symbols=open_sell_order_symbols,
        stop_loss_session=stop_loss_session,
        can_order_now=is_realtime_order_time(now_et),
        can_buy_now=is_buy_order_time(now_et),
        summary={"watch": len(symbols), "buy": 0, "sell": 0, "hold": 0, "errors": 0},
    )


def start_trading_round(trading_round: TradingRoundContext) -> None:
    """启动行情源并计算本轮统一使用的买入金额。"""

    if trading_round.market_data is None:
        trading_round.market_data = build_market_data(trading_round.settings)
        trading_round.owns_market_data = True

    trading_round.buys_used = count_today_buy_orders(
        trading_round.settings.output_dir,
        trading_round.now_et.date(),
    )
    buy_slots = remaining_buy_slots_for_run(
        trading_round.settings,
        trading_round.buys_used,
        trading_round.watch_codes,
        trading_round.positions,
    )
    trading_round.buy_notional, notional_note = buy_notional_for_run(
        trading_round.settings,
        trading_round.broker,
        buy_slots,
    )
    print_run_header(
        trading_round.now_et,
        trading_round.symbols,
        trading_round.broker.source_name(),
    )
    if trading_round.buys_used < trading_round.settings.max_daily_buys and notional_note:
        print(f"本轮买入金额：{notional_note}")


def check_buy(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【买入检查】集中完成分流、统一风控、行情读取和 BUY/HOLD 策略判断。"""

    # 【买入检查 1/7：持仓分流】
    # 已有持仓绝不能再次进入买入判断；不在这里调用卖出函数，而是让后面的
    # check_sell 按核心循环的可见顺序接手。
    if cycle.position is not None:
        return
    cycle.route = "BUY"

    # 【买入检查 2/7：观察池边界】
    # 正常 symbols 已由 WatchCode + 持仓组成；这条保护避免未来额外传入的股票
    # 绕过观察池直接读取行情或提交买单。
    if cycle.symbol not in trading_round.watch_symbols:
        cycle.outcome = "hold"
        cycle.row = monitor_row(cycle.symbol, "跳过", reason="不在观察池且无持仓")
        return

    settings = trading_round.settings

    # 【买入检查 3/7：单股错误上限】
    # 当天该股票提交阶段错误达到配置阈值后，不再继续尝试买入；已有持仓卖出
    # 不使用此限制，所以这项检查只存在于买入阶段。
    if should_skip_symbol_after_order_errors(settings, cycle.symbol, trading_round.now_et):
        cycle.outcome = "hold"
        cycle.row = monitor_row(cycle.symbol, "跳过", reason="今日下单错误达到上限")
        return

    # 【买入检查 4/7：开放买单去重】
    # 券商若已有任意未确认买单，本轮保守暂停新买入；同股挂单会给出更具体原因。
    open_order_reason = open_buy_order_pause_reason(
        trading_round.open_buy_order_symbols,
        cycle.symbol,
    )
    if open_order_reason:
        cycle.outcome = "hold"
        cycle.row = monitor_row(cycle.symbol, "跳过买入", reason=open_order_reason)
        return

    # 【买入检查 5/7：当日排除与本轮串行保护】
    # 已写入当日排除表的股票不再重复算买点；上一笔买单状态未确认时，也不允许
    # 继续向后面的股票提交订单，防止网络竞态造成重复仓位。
    if should_skip_symbol_after_daily_buy_exclusion(settings, cycle.symbol, trading_round.now_et):
        required_drop = trading_round.strategies.buy.max_buy_today_current_gain_pct()
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过",
            reason=f"今日已触达MA5但跌幅未到{_format_pct(abs(required_drop))}",
        )
        return
    if trading_round.buying_paused:
        cycle.outcome = "hold"
        cycle.row = monitor_row(cycle.symbol, "跳过", reason="上一笔买单未确认，本轮暂停后续买入")
        return

    # 【买入检查 6/7：每日次数上限】
    # buys_used 只在订单确认至少部分成交后递增，撤单、拒单和未知状态不占名额。
    if trading_round.buys_used >= settings.max_daily_buys:
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过",
            reason=f"今日买入次数达到上限 {settings.max_daily_buys}",
        )
        return

    # 【买入检查 7/7：行情与策略】
    # 这里仅读取快照并产生信号，不触发券商写入。只有明确 BUY 才把执行权留给
    # execute_buy；普通 HOLD 以及当日排除都直接准备监控行。
    cycle.snapshot = trading_round.market_data.get_snapshot(cycle.symbol)
    cycle.signal = trading_round.strategies.buy.evaluate(cycle.snapshot)
    if should_record_daily_buy_exclusion(cycle.signal):
        cycle.exclusion_reason = cycle.signal.reason
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "排除",
            cycle.snapshot,
            cycle.signal,
            reason="今日已记录排除",
        )
        return
    if cycle.signal.action != "BUY":
        cycle.outcome = "hold"
        cycle.row = monitor_row(cycle.symbol, "观察", cycle.snapshot, cycle.signal)


def execute_buy(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【执行买入】只在 ``check_buy`` 留下明确 BUY 信号时提交 BUY LIMIT。"""

    # 已被检查阶段阻断、已生成展示行或属于卖出路径时，本阶段严格不做任何事。
    if cycle.route != "BUY" or cycle.row is not None:
        return
    if cycle.signal is None or cycle.snapshot is None or cycle.signal.action != "BUY":
        return

    # 【买入执行 1/5：硬时间窗口】
    # 策略信号不能绕过交易时段：真实买入必须位于交易日
    # 09:30 <= t < 12:00 ET，同时处于实时价允许下单时段。
    if not trading_round.can_buy_now:
        if is_premarket_time(trading_round.now_et):
            reason = "盘前时段不买入，跳过真实买单"
        elif not trading_round.can_order_now:
            reason = "当前不在实时价下单时段，跳过真实买单"
        else:
            reason = "买入只允许常规盘开盘后前 2.5 小时，跳过真实买单"
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过买入",
            cycle.snapshot,
            cycle.signal,
            reason=reason,
        )
        return

    # 【买入执行 2/5：策略限价】
    # 自动买入只接受策略 diagnostics 中的 final_buy_point。价格缺失或无效时
    # 失败关闭，禁止退化为市价单，也禁止拿当前价静默替代。
    limit_price = buy_limit_price_from_signal(cycle.signal)
    if limit_price <= 0:
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过买入",
            cycle.snapshot,
            cycle.signal,
            reason="买点价格无效",
        )
        return

    # 【买入执行 3/5：资金预算】
    # buy_notional 已在本轮开始时根据现金、剩余名额和单股上限计算；这里不重新
    # 放大金额，也不因某一只股票失败而临时改用另一套金额。
    if trading_round.buy_notional <= 0:
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过买入",
            cycle.snapshot,
            cycle.signal,
            reason="本轮买入金额无效",
        )
        return

    # 【买入执行 4/5：真实订单边界】
    # 对 AlpacaStockBroker 而言，下面一行会换算整数股、调用 submit_order，
    # 再由当前 CancelStrategy 等待终态并在超时后撤销剩余挂单。单元测试必须
    # 注入 fake/dry-run broker，绝不能把本函数当成纯计算入口。
    cycle.result = trading_round.broker.place_limit_buy(
        cycle.symbol,
        trading_round.buy_notional,
        limit_price,
        cycle.signal.reason,
    )

    # 【买入执行 5/5：订单状态转为本轮状态】
    # 只有确认至少部分成交才占用每日买入名额。未确认状态会暂停后续买入，并
    # 在本股票后面的 check_cancel/execute_cancel 阶段判断是否需要兜底撤单。
    if order_executed(cycle.result.status):
        trading_round.buys_used += 1
        cycle.buy_slot_counted = True
        cycle.outcome = "buy"
        action = "买入"
    else:
        cycle.outcome = "hold"
        action = "买入未成"

    # “部分成交 + 剩余撤单待确认”既算一次真实买入，又仍有开放余量。它必须同时
    # 占用每日名额并暂停后续买单，不能被上面的 executed 分支提前放行。
    if has_unconfirmed_order_status(cycle.result.status):
        trading_round.buying_paused = True
        cycle.paused_buying = True

    # 真实结果若没有成功写进本地订单账本，下一笔买单会基于错误的每日次数运行。
    # Broker 锁存该故障；service 在当前轮立即失败关闭，保留卖出风控继续工作。
    if broker_order_safety_error(trading_round.broker):
        trading_round.buying_paused = True
        cycle.paused_buying = True
    cycle.row = monitor_row(
        cycle.symbol,
        action,
        cycle.snapshot,
        cycle.signal,
        order_status=cycle.result.status,
        order_price=cycle.result.price,
        reason=cycle.result.message or cycle.signal.reason,
    )


def notify_buy(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【买入通知】写入当日排除或本轮监控结果，不重复发送 Broker 外部通知。"""

    if cycle.route != "BUY" or cycle.row is None or cycle.notified:
        return

    # 当日排除文件属于买入决策结果，统一放在 notify 阶段落盘；后续轮询会先读
    # 此记录并跳过该股票，保持原有“当天只检查一次”的业务语义。
    if cycle.exclusion_reason:
        append_daily_buy_exclusion(
            trading_round.settings.output_dir,
            cycle.symbol,
            cycle.exclusion_reason,
            trading_round.now_et.date(),
            trading_round.now_et,
        )

    # 未确认订单先交给后面的 cancel 三阶段。Broker 自己负责订单提交/终态的
    # OpenClaw 外部通知；这里的 notify 仅指监控表格和本轮汇总，避免重复推送。
    if cycle.result is not None and has_unconfirmed_order_status(cycle.result.status):
        cycle.notification_deferred = True
        return
    record_result(trading_round, cycle.outcome, cycle.row)
    cycle.notified = True


def check_sell(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【卖出检查】集中完成持仓行情、退出策略、旧仓和半仓止盈保护。"""

    # 买入路径已经认领无持仓股票；卖出检查只处理本轮开始时真实存在的持仓。
    if cycle.route or cycle.position is None:
        return
    cycle.route = "SELL"

    # 【卖出检查 1/4：开放卖单去重】
    # 持仓在卖单部分成交或撤单待确认时仍会出现在 get_positions；若不先查询
    # 券商开放卖单，下一轮可能再次卖出同一持仓。查询失败也必须保守暂停卖出，
    # 不能把“未知”当作“没有挂单”。
    if trading_round.open_sell_order_symbols is None:
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过卖出",
            reason="无法确认 Alpaca 当前开放卖单，本轮暂停该持仓自动卖出",
        )
        return
    if normalize_symbol(cycle.symbol) in trading_round.open_sell_order_symbols:
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过卖出",
            reason="Alpaca 当前已有同股开放卖单，等待成交或取消确认",
        )
        return

    cycle.snapshot = trading_round.market_data.get_snapshot(cycle.symbol)

    # 【卖出检查 2/4：策略判断】
    # WatchCode 内持仓执行完整卖出策略；池外持仓仍保留止损保护，但不套用池内
    # 的止盈/尾盘退出规则。这里仅产生 Signal，不写入券商。
    if cycle.symbol in trading_round.watch_symbols:
        signal = trading_round.strategies.sell.evaluate(
            cycle.position,
            cycle.snapshot,
            trading_round.now_et,
            trading_round.settings,
        )
    else:
        signal = trading_round.strategies.sell.evaluate_stop_loss(
            cycle.position,
            cycle.snapshot,
            trading_round.settings,
        )

    # 【卖出检查 3/4：监控启动旧仓保护】
    # 若监控启动前该持仓已经达到深度止损线，本会话先保留它，避免进程刚启动
    # 就误清旧仓；只有成本或数量变化后才重新交回正常止损。
    settings = trading_round.settings
    if should_hold_initial_stop_loss(
        trading_round.stop_loss_session,
        cycle.symbol,
        cycle.position,
        signal,
    ):
        signal = Signal(
            cycle.symbol,
            "HOLD",
            (
                f"监控启动时该持仓已亏损达到 {_format_pct(abs(settings.stop_loss_pct))}，"
                "本次监控会话不自动清仓；成本或数量变化后会重新启用止损"
            ),
            cycle.snapshot.current_price,
            diagnostics=signal.diagnostics,
        )

    # 【卖出检查 4/4：半仓止盈去重与余仓止损】
    # 同一股票当天已做过一次半仓止盈时，不重复卖一半；此后只检查剩余仓位的
    # 专用止损。该判断仍位于 execute_sell 前，所以不会产生多余订单。
    half_profit_done = (
        cycle.symbol in trading_round.watch_symbols
        and take_profit_half_already_done(settings, cycle.symbol, trading_round.now_et)
    )
    if half_profit_done and signal.action in {"SELL_HALF", "HOLD"}:
        remainder_signal = trading_round.strategies.sell.evaluate_take_profit_remainder_stop(
            cycle.position,
            cycle.snapshot,
            settings,
        )
        if signal.action == "SELL_HALF" and remainder_signal.action == "HOLD":
            signal = Signal(
                cycle.symbol,
                "HOLD",
                f"今日已执行过 {_format_pct(settings.take_profit_half_pct)} 半仓止盈，不重复卖出",
                cycle.snapshot.current_price,
                diagnostics=remainder_signal.diagnostics,
            )
        else:
            signal = remainder_signal

    cycle.signal = signal
    if signal.action not in {"SELL_ALL", "SELL_HALF"}:
        cycle.outcome = "hold"
        cycle.row = monitor_row(cycle.symbol, "持有", cycle.snapshot, signal)


def execute_sell(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【执行卖出】只在 ``check_sell`` 留下明确卖出信号时提交卖单。"""

    if cycle.route != "SELL" or cycle.row is not None:
        return
    if cycle.signal is None or cycle.snapshot is None:
        return
    if cycle.signal.action not in {"SELL_ALL", "SELL_HALF"}:
        return

    # 【卖出执行 1/4：硬时间窗口】
    # 策略产生 SELL 信号不等于可以写入券商；实时价下单窗口未开放时直接阻断，
    # 且该保护位于所有 SellStrategy 外部，任何策略组件都不能绕过。
    if not trading_round.can_order_now:
        cycle.outcome = "hold"
        cycle.row = monitor_row(
            cycle.symbol,
            "跳过卖出",
            cycle.snapshot,
            cycle.signal,
            reason="当前不在实时价下单时段",
        )
        return

    # 【卖出执行 2/4：选择订单形态】
    # 止损使用带保护价格的 SELL LIMIT，避免异常行情下无边界成交；普通止盈和
    # 尾盘退出沿用 SELL MARKET。两条路径最终都进入同一 Broker。
    if is_stop_loss_signal(cycle.signal):
        limit_price = stop_loss_limit_price_from_signal(cycle.signal)
        if limit_price <= 0:
            cycle.outcome = "hold"
            cycle.row = monitor_row(
                cycle.symbol,
                "跳过卖出",
                cycle.snapshot,
                cycle.signal,
                reason="止损限价无效",
            )
            return

        # 【卖出执行 3/4：真实订单边界——止损限价卖出】
        # AlpacaStockBroker 会校验可卖数量、提交订单、等待终态，并让当前
        # CancelStrategy 在超时后撤销剩余挂单。
        cycle.result = trading_round.broker.place_limit_sell(
            cycle.symbol,
            cycle.signal.quantity,
            limit_price,
            cycle.signal.reason,
        )
    else:
        # 【卖出执行 3/4：真实订单边界——市价卖出】
        # 下面一行可能写入当前 Paper/Live 账户；测试必须注入 fake broker。
        cycle.result = trading_round.broker.place_market_sell(
            cycle.symbol,
            cycle.signal.quantity,
            cycle.snapshot.current_price,
            cycle.signal.reason,
        )

    # 【卖出执行 4/4：订单状态转为本轮状态】
    # 不能把“已提交”直接统计成卖出；只有 FILLED 或明确部分成交才算 sell，
    # 其他状态保持“卖出未成”，并让未确认状态继续进入 cancel 三阶段。
    if order_executed(cycle.result.status):
        cycle.outcome = "sell"
        action = "卖出"
    else:
        cycle.outcome = "hold"
        action = "卖出未成"

    # 卖单本身仍按结果继续处理，但本地订单账本一旦失真，就不能再开新仓扩大风险。
    if broker_order_safety_error(trading_round.broker):
        trading_round.buying_paused = True
        cycle.paused_buying = True

    cycle.row = monitor_row(
        cycle.symbol,
        action,
        cycle.snapshot,
        cycle.signal,
        order_status=cycle.result.status,
        order_price=cycle.result.price,
        reason=cycle.result.message or cycle.signal.reason,
    )


def notify_sell(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【卖出通知】写入本轮监控结果；真实订单通知继续由 Broker 统一发送。"""

    if cycle.route != "SELL" or cycle.row is None or cycle.notified:
        return
    if cycle.result is not None and has_unconfirmed_order_status(cycle.result.status):
        cycle.notification_deferred = True
        return
    record_result(trading_round, cycle.outcome, cycle.row)
    cycle.notified = True


def check_cancel(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【撤单检查】识别 Broker 返回后仍未进入终态且尚未请求撤单的订单。"""

    if not cycle.notification_deferred or cycle.result is None:
        return

    # 默认 AlpacaStockBroker 在 execute_buy/execute_sell 内已经等待终态，并在
    # 超时后调用 CancelStrategy；CANCEL_REQUESTED/PENDING_CANCEL 表示已经请求，
    # 绝不能在这里重复撤单。此兜底只处理自定义 Broker 直接返回的开放状态。
    status = cycle.result.status.upper()
    residual_status = status.removeprefix("PARTIALLY_FILLED_")
    cancelable_open_statuses = {
        "ACCEPTED",
        "ACCEPTED_FOR_BIDDING",
        "CALCULATED",
        "DONE_FOR_DAY",
        "HELD",
        "NEW",
        "PARTIALLY_FILLED",
        "PENDING_NEW",
        "REPLACED",
        "STOPPED",
        "SUBMITTED",
        "SUSPENDED",
    }
    cancel_order = getattr(trading_round.broker, "cancel_order", None)
    cycle.cancel_required = (
        residual_status in cancelable_open_statuses
        and bool(cycle.result.order_id)
        and callable(cancel_order)
    )


def execute_cancel(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【执行撤单】只对检查阶段确认的开放订单执行一次服务层兜底撤单。"""

    if not cycle.cancel_required or cycle.result is None:
        return

    # 【撤单执行：真实订单边界】
    # 默认 Broker 正常路径不会走到这里；该调用主要保护返回 SUBMITTED/NEW 等
    # 非终态的自定义 Broker 适配器。仍按唯一 order_id 撤单，不按股票猜测订单。
    reason = (
        f"自动监控发现 {cycle.result.side} 订单仍为 {cycle.result.status}，"
        "执行核心循环兜底撤单"
    )
    cycle.cancel_result = trading_round.broker.cancel_order(
        cycle.result.order_id,
        reason,
    )


def notify_cancel(trading_round: TradingRoundContext, cycle: SymbolTradeCycle) -> None:
    """【撤单通知】合并撤单竞态后的最终状态，并只写一次本轮表格和汇总。"""

    if not cycle.notification_deferred or cycle.notified or cycle.row is None:
        return

    # cancel_order 可能确认 CANCELED，也可能发现撤单竞态中已经成交；后者必须
    # 按真实成交计为 buy/sell，不能因发起过撤单就一律记成未成交。
    if cycle.cancel_result is not None:
        original_result = cycle.result

        # 撤单查询失败时，默认 Broker 会返回 side=CANCEL、symbol 为空的拒绝结果。
        # 该结果只说明“没能查询/撤销”，不能覆盖原订单的开放状态，更不能解除暂停。
        if not cancel_result_matches_order(original_result, cycle.cancel_result):
            cancel_message = cycle.cancel_result.message or cycle.cancel_result.status
            cycle.row = replace(
                cycle.row,
                reason=(
                    f"{cycle.row.reason}; 撤单结果未能确认原订单 "
                    f"{original_result.order_id}：{cancel_message}"
                ),
            )
            record_result(trading_round, cycle.outcome, cycle.row)
            cycle.notified = True
            return

        cycle.result = cycle.cancel_result
        if order_executed(cycle.result.status):
            if cycle.route == "BUY":
                # execute_buy 可能已按 PARTIALLY_FILLED 计过一次；撤单阶段确认最终
                # 状态时只补记尚未计数的订单，避免同一 order_id 占两次每日名额。
                if not cycle.buy_slot_counted:
                    trading_round.buys_used += 1
                    cycle.buy_slot_counted = True
                cycle.outcome = "buy"
                action = "买入"
            else:
                cycle.outcome = "sell"
                action = "卖出"
        else:
            cycle.outcome = "hold"
            action = "买入未成" if cycle.route == "BUY" else "卖出未成"

        # 当前买单已确认成交或取消后，可以解除由这一笔订单设置的本轮暂停；
        # 若撤单结果仍是未确认状态，则继续暂停，等待下一轮重读开放订单。
        if (
            cycle.paused_buying
            and not has_unconfirmed_order_status(cycle.result.status)
            and not broker_order_safety_error(trading_round.broker)
        ):
            trading_round.buying_paused = False
            cycle.paused_buying = False

        cycle.row = replace(
            cycle.row,
            action=action,
            order_status=cycle.result.status,
            order_price=cycle.result.price or cycle.row.order_price,
            reason=cycle.result.message or cycle.row.reason,
        )

    # 如果 Broker 已经返回 CANCEL_REQUESTED/PENDING_CANCEL，或没有安全的按单号
    # 撤单能力，这里保留原始未确认状态；下一只买入仍会被 buying_paused 阻断。
    record_result(trading_round, cycle.outcome, cycle.row)
    cycle.notified = True


def record_result(
    trading_round: TradingRoundContext,
    outcome: str,
    row: MonitorTableRow,
) -> None:
    """同时记录表格行和本轮计数，避免各分支漏记结果。"""

    trading_round.table_rows.append(row)
    trading_round.summary[outcome] += 1


def finish_trading_round(trading_round: TradingRoundContext) -> dict[str, int]:
    """输出本轮明细和汇总。"""

    print_monitor_table(trading_round.table_rows)
    print_summary(trading_round.summary)
    return trading_round.summary


def close_trading_round(trading_round: TradingRoundContext) -> None:
    """只关闭本轮自己创建的行情源，不影响常驻监控复用的行情连接。"""

    if trading_round.owns_market_data and hasattr(trading_round.market_data, "close"):
        trading_round.market_data.close()


def order_executed(status: str) -> bool:
    """只有成交或 dry-run 才算成功；拒单、撤单和未确认撤单都不算。"""
    return is_executed_order_status(status)


def cancel_result_matches_order(original: OrderResult, cancel_result: OrderResult) -> bool:
    """确认撤单结果确实描述原订单，避免查询失败结果覆盖真实开放暴露。"""

    if not original.order_id or not cancel_result.order_id:
        return False
    if original.order_id != cancel_result.order_id:
        return False
    if original.side.upper() != cancel_result.side.upper():
        return False
    return normalize_symbol(original.symbol) == normalize_symbol(cancel_result.symbol)


def broker_order_safety_error(broker) -> str:
    """返回会使本地订单暴露或每日名额不可信的锁存错误。"""

    return str(
        getattr(broker, "order_safety_error", "")
        or getattr(broker, "order_recording_error", "")
        or ""
    )


def buy_limit_price_from_signal(signal: Signal) -> float:
    """自动买入只使用策略算出的最终买点作为 BUY LIMIT 价格。"""
    try:
        return float(signal.diagnostics.get("final_buy_point", 0.0))
    except (TypeError, ValueError):
        return 0.0


def stop_loss_limit_price_from_signal(signal: Signal) -> float:
    """读取卖出策略写入诊断信息的止损限价；缺失或无效时返回 0 阻止下单。"""
    try:
        return float(signal.diagnostics.get("stop_loss_limit_price", 0.0))
    except (TypeError, ValueError):
        return 0.0


def remaining_buy_slots_for_run(settings: Settings, buys_used: int, watch_codes: list[str], positions: dict[str, object]) -> int:
    """按日内剩余次数和当前观察池，估算本轮最多还会开几笔新仓。"""
    remaining_daily_slots = max(0, settings.max_daily_buys - buys_used)
    open_watch_symbols = [symbol for symbol in watch_codes if symbol not in positions]
    return min(remaining_daily_slots, len(open_watch_symbols))


def buy_notional_for_run(settings: Settings, broker, remaining_buy_slots: int) -> tuple[float, str]:
    """动态控制单笔金额：现金充足时不超过配置上限，现金不足时按剩余可买槽位均分。"""
    if remaining_buy_slots <= 0:
        return 0.0, ""
    cash = broker_cash(broker)
    if cash is None:
        return settings.buy_notional_usd, f"cannot read Alpaca cash; using configured fixed buy notional ${settings.buy_notional_usd:.2f}"
    if cash <= 0:
        return 0.0, f"Alpaca cash ${cash:.2f}; no buy orders this run"
    notional = min(settings.buy_notional_usd, cash / remaining_buy_slots)
    return round(notional, 2), (
        f"dynamic buy notional ${notional:.2f}; Alpaca cash ${cash:.2f}; "
        f"remaining buy slots {remaining_buy_slots}; per-stock cap ${settings.buy_notional_usd:.2f}"
    )


def open_buy_order_symbols_for_run(broker) -> set[str] | None:
    """读取当前开放买单，用于阻止同股或同轮重复买入。

    返回 ``None`` 表示无法确认券商状态，调用方会保守暂停本轮新买入。
    """
    # 订单账本写入失败后，即使券商当前恰好没有开放买单，每日名额也已不可信。
    # 在当前 Broker 生命周期内持续失败关闭，等待人工核对后通过受控重启恢复。
    if broker_order_safety_error(broker):
        return None
    getter = getattr(broker, "get_open_buy_order_symbols", None)
    if getter is None:
        return set()
    try:
        return {normalize_symbol(symbol) for symbol in getter() if normalize_symbol(symbol)}
    except Exception as exc:
        print(f"[提示] 无法确认 Alpaca 当前开放买单，本轮暂停新买入：{short_error(exc)}")
        return None


def open_sell_order_symbols_for_run(broker) -> set[str] | None:
    """读取当前开放卖单；无法确认时由卖出检查失败关闭。"""

    getter = getattr(broker, "get_open_sell_order_symbols", None)
    if getter is None:
        print("[提示] Broker 缺少开放卖单查询能力，本轮暂停自动卖出")
        return None
    try:
        return {normalize_symbol(symbol) for symbol in getter() if normalize_symbol(symbol)}
    except Exception as exc:
        print(f"[提示] 无法确认 Alpaca 当前开放卖单，本轮暂停自动卖出：{short_error(exc)}")
        return None


def open_buy_order_pause_reason(open_buy_order_symbols: set[str] | None, symbol: str) -> str:
    """把开放买单状态转换成可展示的暂停原因；空字符串表示允许继续判断。"""
    if open_buy_order_symbols is None:
        return "无法确认 Alpaca 当前开放买单，本轮暂停新买入"
    if not open_buy_order_symbols:
        return ""
    if symbol in open_buy_order_symbols:
        return "Alpaca 当前已有同股开放买单，跳过防重复"
    return "Alpaca 当前已有开放买单，等待成交或取消确认，本轮暂停新买入"


def broker_cash(broker) -> float | None:
    """安全读取券商现金余额；无法读取时返回 ``None`` 并回退到配置金额。"""
    account = getattr(broker, "account", None)
    value = getattr(account, "cash", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stop_loss_session_for(settings: Settings, positions: dict[str, object]) -> StopLossSession:
    """取得当前进程的止损会话，保护启动前已经处于深亏状态的旧持仓。"""
    key = str(settings.output_dir.resolve())
    session = _STOP_LOSS_SESSIONS.get(key)
    if session is None:
        session = StopLossSession(initial_symbols=set(positions))
        _STOP_LOSS_SESSIONS[key] = session
    prune_stop_loss_session(session, positions)
    return session


def prune_stop_loss_session(session: StopLossSession, positions: dict[str, object]) -> None:
    current_symbols = set(positions)
    session.initial_symbols.intersection_update(current_symbols)
    session.checked_initial_symbols.intersection_update(current_symbols)
    for symbol, fingerprint in list(session.grandfathered_positions.items()):
        position = positions.get(symbol)
        if position is None or position_fingerprint(position) != fingerprint:
            session.grandfathered_positions.pop(symbol, None)
            session.initial_symbols.discard(symbol)
            session.checked_initial_symbols.discard(symbol)


def should_hold_initial_stop_loss(
    session: StopLossSession,
    symbol: str,
    position,
    signal: Signal,
) -> bool:
    """判断是否暂缓启动时已深亏的旧仓，防止监控刚启动就意外清仓。

    只有持仓数量或成本发生变化后，才会把该持仓重新交回正常止损逻辑。
    """
    fingerprint = position_fingerprint(position)
    grandfathered = session.grandfathered_positions.get(symbol)
    if grandfathered == fingerprint:
        return is_stop_loss_signal(signal)
    if grandfathered is not None:
        session.grandfathered_positions.pop(symbol, None)
        session.initial_symbols.discard(symbol)
        session.checked_initial_symbols.discard(symbol)

    if symbol not in session.initial_symbols or symbol in session.checked_initial_symbols:
        return False

    session.checked_initial_symbols.add(symbol)
    if not is_stop_loss_signal(signal):
        return False
    session.grandfathered_positions[symbol] = fingerprint
    return True


def is_stop_loss_signal(signal: Signal) -> bool:
    return signal.action == "SELL_ALL" and signal.diagnostics.get("sell_rule") in {"stop_loss", "take_profit_remainder_stop"}


def position_fingerprint(position) -> tuple[float, float]:
    return (round(float(position.quantity), 6), round(float(position.avg_price), 6))


def should_skip_symbol_after_order_errors(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """同一股票当天拒单达到阈值后，只停止继续买入，不影响已有持仓卖出。"""
    if settings.max_symbol_order_errors <= 0:
        return False
    error_count = count_today_symbol_order_errors(settings.output_dir, symbol, now_et.date())
    if error_count < settings.max_symbol_order_errors:
        return False
    return True


def should_skip_symbol_after_daily_buy_exclusion(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """当天触达 MA5 但未跌到指定跌幅的股票，后续不再检查买入。"""
    if not is_symbol_daily_buy_excluded(settings.output_dir, symbol, now_et.date()):
        return False
    return True


def should_record_daily_buy_exclusion(signal: Signal) -> bool:
    return signal.diagnostics.get("daily_buy_exclusion") == "ma5_touch_without_required_drop"


def take_profit_half_already_done(settings: Settings, symbol: str, now_et: datetime) -> bool:
    """同一股票当天只执行一次 10% 半仓止盈。"""
    return count_today_symbol_take_profit_half_sells(settings.output_dir, symbol, now_et.date()) > 0


def run_forever(
    settings: Settings | None = None,
    *,
    max_loops: int | None = None,
    sleep=time.sleep,
    now_provider=None,
) -> None:
    """常驻盘中监控入口，按配置频率重复调用 :func:`run_once`。

    本函数负责进程锁、行情源生命周期、轮询和 16:00 ET 停止；具体买卖判断
    与订单提交都在 ``run_once`` 内完成。单轮普通异常不会打停整个监控进程。
    """
    settings = settings or build_settings()
    now_provider = now_provider or (lambda: datetime.now(ZoneInfo(settings.market_timezone)))
    start_now = now_provider()
    if is_intraday_monitor_finished(start_now):
        print(f"[{start_now:%Y-%m-%d %H:%M:%S %Z}] 已到盘中结束时间 16:00 ET，盘中监控不启动。", flush=True)
        return

    run_lock = acquire_run_lock(settings.output_dir, "intraday_ma5_monitor.lock", "盘中 MA5 监控")
    market_data = None
    broker = None
    loop_count = 0
    try:
        notify_intraday_monitor_started(settings)
        market_data = build_market_data(settings)
        while True:
            now_et = now_provider()
            if is_intraday_monitor_finished(now_et):
                print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 盘中监控到达 16:00 ET，退出。", flush=True)
                break

            loop_count += 1
            # 【常驻入口直达核心循环】
            # 运行链路在这里直接进入 run_once，不再先跳到 run_forever_once 才能找到
            # 核心买卖循环。单轮异常仍只丢弃当前 Broker，下一轮重新连接后继续。
            try:
                broker = broker or build_broker(settings)
                run_once(settings, market_data=market_data, broker=broker, now=now_et)
            except Exception as exc:
                print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 本轮失败，继续等待下一轮：{short_error(exc)}")
                broker = None
            if max_loops is not None and loop_count >= max_loops:
                print(f"已完成测试轮数 {max_loops}，退出。", flush=True)
                break

            sleep_now = now_provider()
            if is_intraday_monitor_finished(sleep_now):
                print(f"[{sleep_now:%Y-%m-%d %H:%M:%S %Z}] 盘中监控到达 16:00 ET，退出。", flush=True)
                break
            sleep_seconds = min(next_poll_seconds(settings, sleep_now), seconds_until_intraday_monitor_end(sleep_now))
            if sleep_seconds <= 0:
                print(f"[{sleep_now:%Y-%m-%d %H:%M:%S %Z}] 盘中监控到达 16:00 ET，退出。", flush=True)
                break
            print(f"下一轮：等待 {sleep_seconds} 秒后继续...", flush=True)
            sleep(sleep_seconds)
    finally:
        if market_data is not None and hasattr(market_data, "close"):
            market_data.close()
        run_lock.close()


def notify_intraday_monitor_started(settings: Settings) -> None:
    safe_send_openclaw_messages(
        settings,
        [render_intraday_monitor_start_message(settings)],
        context="intraday MA5 monitor started",
    )


def render_intraday_monitor_start_message(settings: Settings) -> str:
    selection = resolve_strategy_runtime(settings).selection
    return "\n".join(
        [
            "【盘中 MA5 监控启动】",
            "结论：开始盘中监控。",
            "动作：按策略检测买入/卖出信号；满足条件时会提交 Alpaca 订单，并发送订单提交与最终状态通知。",
            "",
            "监控配置",
            f"- 策略组合：{selection.profile_name}",
            f"- WatchCode：{selection.watchlist_strategy_name}",
            f"- 买入：{selection.buy_strategy_name}",
            f"- 卖出：{selection.sell_strategy_name}",
            f"- 自动撤单：{selection.cancel_strategy_name}",
            f"- 观察文件：{settings.watch_codes_file}",
            f"- 买入上限：今日最多 {settings.max_daily_buys} 支",
            f"- 单股金额：最多 ${settings.buy_notional_usd:.2f}",
            f"- 轮询频率：盘中每 {settings.regular_poll_seconds} 秒一轮",
            "",
            "风控规则",
            f"- 单笔订单超时：{settings.order_cancel_after_seconds} 秒未完全成交会请求撤单",
            f"- 同一股票下单错误上限：{settings.max_symbol_order_errors} 次",
        ]
    )


def run_forever_once(settings: Settings, market_data, broker, now_et: datetime):
    """兼容旧调用方的一轮容错包装；常驻入口已直接调用 :func:`run_once`。

    新代码应从 ``run_forever`` 直接阅读到 ``run_once``。保留此函数只为避免外部
    脚本或测试的旧导入失效，不再把它放在正式监控的活跃调用链上。
    """
    try:
        broker = broker or build_broker(settings)
        run_once(settings, market_data=market_data, broker=broker, now=now_et)
        return broker
    except Exception as exc:
        print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 本轮失败，继续等待下一轮：{short_error(exc)}")
        return None


def print_run_header(now_et: datetime, watch_codes: list[str], broker_name: str) -> None:
    """打印本轮监控概览，长观察池也保持可读。"""
    print(f"[{now_et:%Y-%m-%d %H:%M:%S %Z}] 开始检查")
    print(f"观察数量：{len(watch_codes)} | 交易通道：{broker_name}")


def print_summary(summary: dict[str, int]) -> None:
    """按固定顺序打印统计，方便快速确认买入、卖出和错误数量。"""
    print(
        "本轮完成："
        f"观察 {summary['watch']} | "
        f"买入 {summary['buy']} | "
        f"卖出 {summary['sell']} | "
        f"持有/跳过 {summary['hold']} | "
        f"错误 {summary['errors']}"
    )


def monitor_row(
    symbol: str,
    action: str,
    snapshot: MarketSnapshot | None = None,
    signal: Signal | None = None,
    *,
    order_status: str = "",
    order_price: float = 0.0,
    reason: str = "",
) -> MonitorTableRow:
    selected_reason = reason or (signal.reason if signal else "")
    selected_price = order_price or signal_price(signal)
    if snapshot is None:
        return MonitorTableRow(symbol=symbol, action=action, order_status=order_status, order_price=selected_price, reason=selected_reason)
    return MonitorTableRow(
        symbol=symbol,
        action=action,
        has_market_data=True,
        current_price=snapshot.current_price,
        today_open=snapshot.today_open,
        today_ma5=snapshot.today_ma5,
        today_open_ma5=snapshot.today_open_ma5,
        signal_gain_pct=snapshot.signal_day_gain_pct,
        current_gain_pct=snapshot.today_current_gain_pct,
        order_price=selected_price,
        order_status=order_status,
        reason=selected_reason,
    )


def signal_price(signal: Signal | None) -> float:
    if signal is None:
        return 0.0
    for key in ("final_buy_point", "stop_loss_limit_price", "take_profit_price"):
        try:
            value = float(signal.diagnostics.get(key, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return signal.current_price if signal.current_price > 0 else 0.0


def print_monitor_table(rows: list[MonitorTableRow]) -> None:
    if not rows:
        return
    print("本轮股票明细：")
    headers = ["代码", "动作", "当前价", "开盘", "MA5", "开盘MA5", "信号涨幅", "当前涨幅", "买/卖点", "订单", "原因"]
    table = [
        [
            row.symbol,
            row.action,
            _format_price(row.current_price),
            _format_price(row.today_open),
            _format_price(row.today_ma5),
            _format_price(row.today_open_ma5),
            _format_pct(row.signal_gain_pct) if row.has_market_data else "-",
            _format_pct(row.current_gain_pct) if row.has_market_data else "-",
            _format_price(row.order_price),
            row.order_status or "-",
            _reason_text(row.reason),
        ]
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for row in table:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print(_format_table_line(headers, widths))
    print(_format_table_line(["-" * width for width in widths], widths))
    for row in table:
        print(_format_table_line(row, widths))


def _format_table_line(values: list[str], widths: list[int]) -> str:
    cells = []
    for value, width in zip(values, widths):
        cells.append(value.ljust(width))
    return " | ".join(cells)


def _reason_text(reason: str) -> str:
    return " ".join(str(reason or "-").split())


def print_signal(reason: str, symbol: str, snapshot: MarketSnapshot) -> None:
    """打印策略判断和核心价格，便于直接在 PyCharm 控制台核对。"""
    print(
        "  判断："
        f"{reason} | 当前价 {_format_price(snapshot.current_price)} | "
        f"今日开盘 {_format_price(snapshot.today_open)} | "
        f"开盘MA5 {_format_price(snapshot.today_open_ma5)} | "
        f"开盘偏离 {_format_pct(snapshot.today_open_vs_open_ma5_pct) if snapshot.today_open_ma5 > 0 else '未知'} | "
        f"今日动态MA5 {_format_price(snapshot.today_ma5)} | "
        f"信号日涨幅 {_format_pct(snapshot.signal_day_gain_pct)} | "
        f"当前涨幅 {_format_pct(snapshot.today_current_gain_pct)} | "
        f"开盘涨幅 {_format_pct(snapshot.today_open_gain_pct) if snapshot.today_open > 0 else '未知'}"
    )


def print_snapshot(snapshot: MarketSnapshot) -> None:
    """打印参与 MA5 计算的原始行情数字。"""
    closes = ", ".join(f"{close:.4f}" for close in snapshot.previous_closes[-4:])
    opens = ", ".join(f"{open_price:.4f}" for open_price in snapshot.previous_opens[-4:]) or "不足4日"
    print(f"\n[{_format_snapshot_time(snapshot.as_of)}] {snapshot.symbol}")
    print(
        "  行情："
        f"当前价 {_format_price(snapshot.current_price)}（来源：{_format_source(snapshot.current_price_source)}） | "
        f"今日开盘 {_format_price(snapshot.today_open)}（来源：{_format_source(snapshot.today_open_source)}）"
    )
    print(
        "  均线："
        f"前4日收盘 [{closes}] + 当前价 {_format_price(snapshot.current_price)} "
        f"=> 今日动态MA5 {_format_price(snapshot.today_ma5)}"
    )
    print(
        "  开盘："
        f"前4日开盘 [{opens}] + 今日开盘 {_format_price(snapshot.today_open)} "
        f"=> 开盘MA5 {_format_price(snapshot.today_open_ma5)} | "
        f"开盘偏离 {_format_pct(snapshot.today_open_vs_open_ma5_pct) if snapshot.today_open_ma5 > 0 else '未知'}"
    )
    print(
        "  买点输入："
        f"信号日涨幅 {_format_pct(snapshot.signal_day_gain_pct)} | "
        f"当前涨幅 {_format_pct(snapshot.today_current_gain_pct)} | "
        f"当天开盘涨幅 {_format_pct(snapshot.today_open_gain_pct) if snapshot.today_open > 0 else '未知'}"
    )


def print_order(status: str, message: str, symbol: str, quantity: float, price: float) -> None:
    """打印订单结果摘要，避免必须打开 CSV 才能知道发生了什么。"""
    print(f"  订单：{symbol} | 状态 {status} | 数量 {quantity:.6f} | 参考价 {_format_price(price)} | {message}")


def _format_price(value: float) -> str:
    return f"{value:.4f}" if value > 0 else "未知"


def _format_source(source: str) -> str:
    return source or "未知"


def _format_snapshot_time(value: datetime) -> str:
    display_time = value.astimezone(DISPLAY_TIMEZONE) if value.tzinfo else value.replace(tzinfo=DISPLAY_TIMEZONE)
    return f"{display_time:%Y-%m-%d %H:%M:%S} {display_time.tzname()}"


def _format_pct(value: float) -> str:
    return f"{value:.2%}"
