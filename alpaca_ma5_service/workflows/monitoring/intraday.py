"""盘中自动交易的可编辑配置和运行入口。

用户通常只需要在本文件顶部修改策略组合、金额和风控参数。根目录
``monitor_ma5_forever.py`` 只负责转入这里；实际单轮买卖编排在 ``service.py``，
真实券商写入在 ``broker.py``，撤单写入在 ``order_guard.py``。
"""

from datetime import time

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service import final_strategy, strategy_ma5_dip
from alpaca_ma5_service.config import MA5_DIP_STRATEGY_NAME, build_settings
from alpaca_ma5_service.final_strategy import STRATEGY_NAME as GAP_CONFIRMED_PULLBACK_STRATEGY
from alpaca_ma5_service.monitor_runtime import monitor_runtime
from alpaca_ma5_service.service import run_forever
from alpaca_ma5_service.strategy_framework import (
    DEFAULT_CANCEL_STRATEGY_NAME,
    DEFAULT_SELL_STRATEGY_NAME,
)


# 这是盘中监控的可编辑配置文件；根目录 monitor_ma5_forever.py 只负责启动。
# 盘中主流程（完整说明见 docs/architecture/PROJECT_FLOW.md）：
# 1. 本入口 -> service.run_forever -> service.run_once，常驻入口直接进入核心循环。
# 2. 核心循环按 check/execute/notify 的买入、卖出、撤单九阶段逐股展开。
# 3. 默认 Broker 在买卖执行内部完成终态等待/超时撤单，再写订单账本和外部通知。
#
# ==============================
# 常用运行配置：你主要改这里
# ==============================
# 使用规则：
# 1. 只改等号右边的值，不要改变量名。
# 2. 百分比都用小数写：0.15 表示 15%，-0.10 表示下跌 10%。
# 3. 金额单位是美元；时间使用美股东部时间 ET。

# 选择基础策略组合。它提供四类策略的默认值。
# 用法：保留 MA5_DIP_STRATEGY_NAME 就是老的 5 日均线低吸；
# 如果要切到新策略，把右边改成 GAP_CONFIRMED_PULLBACK_STRATEGY。
# gap 组合当前使用 $2,500、每日最多 3 只、
# -8% 止损和 +4% 全部止盈；下面的 MA5 金额和风控值不会覆盖它。
STRATEGY_NAME = GAP_CONFIRMED_PULLBACK_STRATEGY

# 分别选择 WatchCode、买入、卖出和自动撤单策略。
# 默认保持同一组合；也可以只替换其中一项，启动时会先验证名称和组合是否有效。
WATCHLIST_STRATEGY_NAME = STRATEGY_NAME
BUY_STRATEGY_NAME = STRATEGY_NAME
SELL_STRATEGY_NAME = DEFAULT_SELL_STRATEGY_NAME
CANCEL_STRATEGY_NAME = DEFAULT_CANCEL_STRATEGY_NAME

# 每天最多买入几只股票。
# 用法：想试水就设小，比如 1 到 3；想放大覆盖面再调高。
# 注意：这里控制“成交买入名额”，不是 watch_codes.txt 的观察数量。
BUY_STOCK_COUNT = 3

# 每只股票本轮最多使用多少美元买入。
# 用法：1500.0 表示每只最多 1500 美元；账户现金不足时会按剩余名额动态压低单只金额。
# 注意：不要写成字符串，也不要带 $ 符号。
BUY_NOTIONAL_USD = 2_500.0

# 老 MA5 低吸策略：信号日最低涨幅。
# 用法：0.15 表示信号日涨幅至少 15% 才有买点；想放宽就调低，想严格就调高。
MA5_MIN_SIGNAL_DAY_GAIN_PCT = 0.15

# 老 MA5 低吸策略：中档信号日涨幅分界。
# 用法：信号日涨幅达到 40% 后，基础买点从 MA5+0.5% 提高到 MA5+3%。
MA5_MID_SIGNAL_DAY_GAIN_PCT = 0.40

# 老 MA5 低吸策略：高档信号日涨幅分界。
# 用法：信号日涨幅超过 100% 后，基础买点提高到 MA5+4%。
MA5_HIGH_SIGNAL_DAY_GAIN_PCT = 1.00

# 老 MA5 低吸策略：今日开盘中档加成分界。
# 用法：今日开盘相对信号日收盘涨幅达到 5% 后，买点额外加 1%。
MA5_MID_OPEN_GAIN_PCT = 0.05

# 老 MA5 低吸策略：今日开盘高档加成分界。
# 用法：今日开盘相对信号日收盘涨幅超过 15% 后，买点额外加 2%。
MA5_HIGH_OPEN_GAIN_PCT = 0.15

# 老 MA5 低吸策略：触发上沿。
# 用法：0.03 表示当前价在最终买点上方 3% 以内也允许触发；调小会更保守，调大会更容易买到。
MA5_BUY_TRIGGER_DISTANCE_PCT = 0.03

# 老 MA5 低吸策略：今日开盘极端低开保护。
# 用法：-0.40 表示如果今日开盘相对信号日收盘跌 40% 或更多，本轮不买。
MA5_MIN_TODAY_OPEN_GAIN_PCT = -0.40

# 老 MA5 低吸策略：今日开盘价相对开盘 MA5 的保护。
# 用法：-0.10 表示如果今日开盘价低于开盘 MA5 达 10% 或更多，当天不买这只。
MA5_MIN_TODAY_OPEN_VS_OPEN_MA5_PCT = -0.10

# 老 MA5 低吸策略：真正允许买入前必须达到的当前跌幅。
# 用法：-0.12 表示当前价相对信号日收盘必须跌 12% 或更多才允许买；你要改“跌幅 10%/12%”主要改这里。
MA5_MAX_BUY_TODAY_CURRENT_GAIN_PCT = -0.12

# 止损触发线。
# 用法：-0.10 表示持仓亏损达到 10% 时触发卖出全部。
STOP_LOSS_PCT = -0.10

# 止损卖出限价。
# 用法：-0.08 表示止损触发后，用成本价下方 8% 的限价卖出；越接近 0 越保守但越可能不成交。
STOP_LOSS_LIMIT_PCT = -0.08

# 半仓止盈触发线。
# 用法：0.10 表示持仓盈利达到 10% 时触发卖出一部分。
TAKE_PROFIT_HALF_PCT = 0.10

# 半仓止盈卖出比例。
# 用法：0.50 表示止盈时卖出一半；1.0 表示全部卖出；0 表示不卖。
TAKE_PROFIT_SELL_FRACTION = 0.50

# 半仓止盈后，剩余仓的保护卖出线。
# 用法：None 表示关闭；如果设成 0.05，表示半仓止盈后，剩余仓回落到盈利 5% 时卖出剩余全部。
TAKE_PROFIT_REMAINDER_STOP_PCT = None

# 尾盘强制清仓开始时间，东部时间 ET。
# 用法：time(15, 55) 表示 15:55 开始进入尾盘清仓窗口。
CLOSE_LIQUIDATION_START = time(15, 55)

# 尾盘强制清仓结束时间，东部时间 ET。
# 用法：time(16, 0) 表示 16:00 结束尾盘清仓窗口。
CLOSE_LIQUIDATION_END = time(16, 0)

# 同一只股票当天最多允许几次下单错误。
# 用法：3 表示同一股票被拒单/错误累计 3 次后，当天跳过这只，避免反复打单。
MAX_SYMBOL_ORDER_ERRORS = 3

# 是否允许小数股。
# 用法：False 表示只买整数股；True 表示允许小数股，但仍取决于券商和股票是否支持。
ALLOW_FRACTIONAL_SHARES = False

# 是否允许扩展时段订单。
# 用法：True 表示支持盘前/盘后可用的 extended-hours limit order；买入时间窗口仍由策略风控限制。
EXTENDED_HOURS_ORDERS_ENABLED = True

# 扩展时段保护限价缓冲。
# 用法：0.003 表示 0.3% 缓冲；主要用于非普通常规盘订单，降低市价式订单失控风险。
EXTENDED_HOURS_LIMIT_BUFFER_PCT = 0.003

# 下单后等待成交的最长秒数。
# 用法：600 表示最多等 10 分钟；到时仍未完全成交会请求撤单。
ORDER_CANCEL_AFTER_SECONDS = 600

# 查询订单状态的间隔秒数。
# 用法：5 表示每 5 秒查一次订单是否成交/取消。
ORDER_STATUS_POLL_SECONDS = 5

# 常规盘轮询间隔秒数。
# 用法：10 表示常规盘中每 10 秒检查一次 watch_codes 和持仓。
REGULAR_POLL_SECONDS = 10

# 非活跃时段轮询间隔秒数。
# 用法：1200 表示盘前/盘后/等待时每 20 分钟检查一次；临近开盘时底层会自动缩短等待。
IDLE_POLL_SECONDS = 1200

# 实时价格来源。
# 用法："moomoo" 表示优先使用本机 Moomoo OpenD 实时价；如果你不用 Moomoo，再考虑改成项目支持的其他来源。
REALTIME_PRICE_SOURCE = "moomoo"

# Telegram 通知通道。
# 用法："cloud" 表示走云端 Hermes webhook；"local" 表示走本机通知通道。
TRADE_NOTIFY_MODE = "cloud"


def apply_ma5_dip_config() -> None:
    """把本文件中的 MA5 参数注入 ``ma5_dip`` 买入策略模块。"""
    strategy_ma5_dip.configure(
        min_signal_day_gain_pct=MA5_MIN_SIGNAL_DAY_GAIN_PCT,
        mid_signal_day_gain_pct=MA5_MID_SIGNAL_DAY_GAIN_PCT,
        high_signal_day_gain_pct=MA5_HIGH_SIGNAL_DAY_GAIN_PCT,
        mid_open_gain_pct=MA5_MID_OPEN_GAIN_PCT,
        high_open_gain_pct=MA5_HIGH_OPEN_GAIN_PCT,
        buy_trigger_distance_pct=MA5_BUY_TRIGGER_DISTANCE_PCT,
        min_today_open_gain_pct=MA5_MIN_TODAY_OPEN_GAIN_PCT,
        min_today_open_vs_open_ma5_pct=MA5_MIN_TODAY_OPEN_VS_OPEN_MA5_PCT,
        max_buy_today_current_gain_pct=MA5_MAX_BUY_TODAY_CURRENT_GAIN_PCT,
    )


def build_monitor_settings():
    """构建 WatchCode 和盘中监控共用的唯一运行配置。

    这里完成四类策略名及风控参数装配，但不会读取行情、连接券商或下单。
    """
    if MA5_DIP_STRATEGY_NAME in {WATCHLIST_STRATEGY_NAME, BUY_STRATEGY_NAME}:
        apply_ma5_dip_config()

    gap_profile_selected = STRATEGY_NAME == GAP_CONFIRMED_PULLBACK_STRATEGY
    risk_overrides = (
        {}
        if gap_profile_selected
        else {
            "stop_loss_pct": STOP_LOSS_PCT,
            "stop_loss_limit_pct": STOP_LOSS_LIMIT_PCT,
            "take_profit_half_pct": TAKE_PROFIT_HALF_PCT,
            "take_profit_sell_fraction": TAKE_PROFIT_SELL_FRACTION,
            "take_profit_remainder_stop_pct": TAKE_PROFIT_REMAINDER_STOP_PCT,
        }
    )
    return build_settings(
        strategy_profile_name=STRATEGY_NAME,
        watchlist_strategy_name=WATCHLIST_STRATEGY_NAME,
        buy_strategy_name=BUY_STRATEGY_NAME,
        sell_strategy_name=SELL_STRATEGY_NAME,
        cancel_strategy_name=CANCEL_STRATEGY_NAME,
        buy_stock_count=None if gap_profile_selected else BUY_STOCK_COUNT,
        buy_notional_usd=(
            final_strategy.BUY_NOTIONAL_USD
            if gap_profile_selected
            else BUY_NOTIONAL_USD
        ),
        max_symbol_order_errors=MAX_SYMBOL_ORDER_ERRORS,
        close_liquidation_start=CLOSE_LIQUIDATION_START,
        close_liquidation_end=CLOSE_LIQUIDATION_END,
        regular_poll_seconds=REGULAR_POLL_SECONDS,
        idle_poll_seconds=IDLE_POLL_SECONDS,
        allow_fractional_shares=ALLOW_FRACTIONAL_SHARES,
        extended_hours_orders_enabled=EXTENDED_HOURS_ORDERS_ENABLED,
        extended_hours_limit_buffer_pct=EXTENDED_HOURS_LIMIT_BUFFER_PCT,
        order_cancel_after_seconds=ORDER_CANCEL_AFTER_SECONDS,
        order_status_poll_seconds=ORDER_STATUS_POLL_SECONDS,
        realtime_price_source=REALTIME_PRICE_SOURCE,
        trade_notify_mode=TRADE_NOTIFY_MODE,
        **risk_overrides,
    )


def monitor_ma5_forever() -> None:
    """盘中自动交易入口：获取进程互斥锁后启动常驻监控。

    ``run_forever`` 会循环调用 ``service.run_once``；只要使用真实 Broker，
    满足条件时就可能买入、卖出，并按撤单策略处理超时挂单。
    """
    settings = build_monitor_settings()
    with monitor_runtime(settings.output_dir, "monitor_ma5", "intraday"):
        run_forever(settings)


if __name__ == "__main__":
    monitor_ma5_forever()
