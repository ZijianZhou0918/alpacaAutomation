# 项目总流程

## 30 秒看懂

```text
monitor_auto.py
    |
    v
交易日历 + 当前美股时段
    |
    +-- 04:00-09:30 ET -> 只读取 Alpaca 当前持仓 -> 60 秒涨跌 3% 提醒（不选股、不下单）
    |
    +-- 09:30-16:00 ---> 生成/确认盘中 WatchCode -> 策略监控（可真实交易）
    |
    +-- 16:00-20:00 ---> 生成/确认盘后 WatchCode -> high/low 监控（统一入口只提醒）
    |
    +-- 20:00 ET 后 ----> 发送日报 -> 退出
```

项目只有一个全天自动入口：`monitor_auto.py`。它先通过统一交易日历判断阶段，再保证对应 WatchCode 已准备好，最后进入该阶段的监控；盘中池需同时匹配目标信号日和当前规范化选股规则。直接盘中入口复用同一校验并在旧池上失败关闭。

## 盘中交易主链路

```text
workflows/monitoring/intraday.py 顶部配置
    |
    v
基础 StrategyProfile + 四类独立覆盖
    |
    v
启动前全量校验 -> 不可变 StrategyRuntime
    |
    v
service.run_forever() 直接调用 service.run_once()
    |
    v
prepare_trading_round：按订单 ID 对账 pending_orders.json，读取持仓并同步 -8% 券商保护 STOP
    |
    +-- 部分/迟到成交 -> 幂等更新 ladder_state.json
    +-- 超过 600 秒仍开放 -> 请求撤单，继续跨轮监督
    |
    v
for symbol in trading_round.symbols
    |
    +-- check_buy ----> execute_buy ----> notify_buy
    |
    +-- check_sell ---> execute_sell ---> notify_sell
    |
    +-- check_cancel -> execute_cancel -> notify_cancel
                           |
                           +-- 买入执行：Broker 提交并持久化订单 ID；成交后立即挂/更新保护 STOP
                           +-- 卖出执行：先确认撤销保护 STOP，再提交策略卖单
                           |             -> 下一轮先查询/对账/必要时撤单
                           |
                           +-- 撤单执行：仅兜底自定义 Broker 返回的开放订单
                                         -> Broker 按唯一 order_id 撤单
                           |
                           v
                    账本、通知、复盘
```

核心循环不再经过 `process_symbol` 或其他 `process_*` 编排包装。普通已有持仓由
`check_buy` 跳过并交给 `check_sell`；活动三档计划的持仓可在买入窗口继续补档，绝对止损和尾盘退出仍优先交给卖出阶段。无持仓 WatchCode 在买入三阶段处理。默认
Broker 不在逐股循环等待订单终态：订单身份先写入 `pending_orders.json`，后续轮次
在新决策前按 ID 对账，达到时限再撤单。服务层撤单三阶段只兜底自定义 Broker。

四类策略的职责边界：

| 阶段 | 组件 | 只负责 |
| --- | --- | --- |
| 选股 | `WatchlistStrategy` | 生成候选筛选规则 |
| 买入 | `BuyStrategy` | 根据单只股票行情返回买入或观察信号 |
| 卖出 | `SellStrategy` | 根据持仓和行情返回卖出或持有信号 |
| 撤单 | 自动订单监督器；手动兼容路径使用 `CancelStrategy` | 自动订单跨轮查询与超时撤单；手动订单同步等待终态 |

交易日、`09:30 <= t < 12:00 ET` 真实买入窗口、每日买入上限、重复订单、拒单保护和订单账本不属于某个可替换策略，继续由统一服务和安全层强制执行。

想逐行定位“只做决策”和“真正写入券商”的边界，请看
[`TRADE_ORDER_FLOW.md`](TRADE_ORDER_FLOW.md)。其中列出了自动监控、OpenClaw、
真实测试单和盘后策略的全部买入、卖出与撤单写入点。

## 三个时段的边界

| 时段 | WatchCode | 统一入口行为 | 是否自动下单 |
| --- | --- | --- | --- |
| 盘前，04:00-09:30 ET | Alpaca 当前持仓 | 滚动 60 秒上涨/下跌达到 3% 时提醒 | 否 |
| 盘中，09:30-16:00 ET | `data/watchcodes/watch_codes.txt` | 日期与规则双校验后运行买入、卖出和订单保护 | 仅买入窗口和风控允许时 |
| 盘后，16:00-20:00 ET | `data/watchcodes/watch_code_afterhours.txt` | `monitor_auto.py` 进入 high/low 提醒监控 | 否 |

盘后另有可下单的直接策略入口 `afterhours_high_low.run_afterhours_high_low_strategy(dry_run=False)`；它不等同于统一入口的提醒链路，执行前必须单独确认 Paper/Live 和授权。

## 用户从哪里开始

| 目的 | 入口 |
| --- | --- |
| 全天自动运行 | `monitor_auto.py` |
| 只运行盘中监控 | `monitor_ma5_forever.py` |
| 选择完整策略或分别选择四类组件 | `alpaca_ma5_service/workflows/monitoring/intraday.py` 顶部配置区 |
| 只生成盘中股票池 | `watchcode_ma5.py` |
| 查看每日复盘 | 双击 `open_daily_review.cmd` |
| 新增或扩展策略 | `alpaca_ma5_service/strategy_framework/README.md` |
| 定位买入、卖出、撤单实际代码 | `docs/architecture/TRADE_ORDER_FLOW.md` |
| 重建正式日线库 | `run_backtest_daily_history_rebuild.py`；与实盘订单链路隔离 |

## 修改后的检查顺序

```text
修改对应组件
 -> 注册组件
 -> 需要时新增/调整 profile
 -> 启动前解析测试
 -> 定向测试
 -> 全量 unittest
 -> 只读复核运行服务和监控状态
```

详细运行和安全要求以 [`../PROJECT_OPERATIONS.md`](../PROJECT_OPERATIONS.md) 与根目录 [`../../CODE_MODIFICATION_RULES.md`](../../CODE_MODIFICATION_RULES.md) 为准。
