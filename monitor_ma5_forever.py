from datetime import time

from alpaca_ma5_service.entrypoint import ensure_local_venv

ensure_local_venv()

from alpaca_ma5_service import strategy_ma5_dip
from alpaca_ma5_service.config import MA5_DIP_STRATEGY_NAME, build_settings
from alpaca_ma5_service.final_strategy import STRATEGY_NAME as GAP_CONFIRMED_PULLBACK_STRATEGY
from alpaca_ma5_service.service import run_forever


# ==============================
# 常用运行配置：你主要改这里
# ==============================
# 使用规则：
# 1. 只改等号右边的值，不要改变量名。
# 2. 百分比都用小数写：0.15 表示 15%，-0.10 表示下跌 10%。
# 3. 金额单位是美元；时间使用美股东部时间 ET。

# 选择运行哪套策略。
# 用法：保留 MA5_DIP_STRATEGY_NAME 就是老的 5 日均线低吸；
# 如果要切到新策略，把右边改成 GAP_CONFIRMED_PULLBACK_STRATEGY。
STRATEGY_NAME = MA5_DIP_STRATEGY_NAME

# 每天最多买入几只股票。
# 用法：想试水就设小，比如 1 到 3；想放大覆盖面再调高。
# 注意：这里控制“成交买入名额”，不是 watch_codes.txt 的观察数量。
BUY_STOCK_COUNT = 2

# 每只股票本轮最多使用多少美元买入。
# 用法：1500.0 表示每只最多 1500 美元；账户现金不足时会按剩余名额动态压低单只金额。
# 注意：不要写成字符串，也不要带 $ 符号。
BUY_NOTIONAL_USD = 2_000.0

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


def monitor_ma5_forever() -> None:
    if STRATEGY_NAME == MA5_DIP_STRATEGY_NAME:
        apply_ma5_dip_config()

    run_forever(
        build_settings(
            strategy_name=STRATEGY_NAME,
            buy_stock_count=BUY_STOCK_COUNT,
            buy_notional_usd=BUY_NOTIONAL_USD,
            max_symbol_order_errors=MAX_SYMBOL_ORDER_ERRORS,
            stop_loss_pct=STOP_LOSS_PCT,
            stop_loss_limit_pct=STOP_LOSS_LIMIT_PCT,
            take_profit_half_pct=TAKE_PROFIT_HALF_PCT,
            take_profit_sell_fraction=TAKE_PROFIT_SELL_FRACTION,
            take_profit_remainder_stop_pct=TAKE_PROFIT_REMAINDER_STOP_PCT,
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
        )
    )


if __name__ == "__main__":
    monitor_ma5_forever()
