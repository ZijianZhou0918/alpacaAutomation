# 买入、卖出与撤单代码导航

这份文档用于回答三个最重要的问题：

1. 哪里只是在判断策略？
2. 哪里决定执行买入、卖出或撤单？
3. 哪一行真正写入 Alpaca Paper/Live 账户？

代码中已统一加入以下可搜索标记：

```text
【买入决策，不下单】
【卖出决策，不下单】
【执行买入订单】
【执行卖出订单】
【执行撤单请求】
【真实券商写入：买入/卖出】
【真实券商写入：撤单】
【订单终态/自动撤单】
```

在项目根目录运行下面的命令，可以一次看到所有关键位置：

```powershell
rg -n "def (check|execute|notify)_(buy|sell|cancel)|【真实订单边界|【真实券商写入|【订单终态" alpaca_ma5_service
```

## 自动盘中交易主链路

```text
monitor_ma5_forever.py
  -> workflows/monitoring/intraday.py::monitor_ma5_forever
  -> service.py::run_forever
  -> service.py::run_once                              常驻入口直接进入核心循环
       -> prepare_trading_round                        准备策略、持仓和观察池
       -> start_trading_round                          启动行情并计算本轮买入金额
       -> for symbol in trading_round.symbols
            -> check_buy                               买入分流、风控、BUY/HOLD 判断
            -> execute_buy                             broker.place_limit_buy
            -> notify_buy                              排除记录、监控表和本轮汇总

            -> check_sell                              持仓退出判断和统一保护
            -> execute_sell                            broker.place_limit_sell/market_sell
            -> notify_sell                             监控表和本轮汇总

            -> check_cancel                            检查 Broker 返回的未确认暴露
            -> execute_cancel                          仅兜底自定义 Broker 的开放订单
            -> notify_cancel                           合并撤单竞态后的最终结果

execute_buy / execute_sell
  -> broker._submit_* -> client.submit_order
  -> CancelStrategy.wait_for_terminal                  默认自动终态等待
  -> order_guard.cancel_unfilled_order
       -> client.cancel_order_by_id                    默认自动超时撤单

execute_cancel
  -> broker.cancel_order
  -> order_guard.cancel_unfilled_order
       -> client.cancel_order_by_id                    服务层兜底撤单
```

## 一眼找到真实执行点

| 动作 | 决策位置 | 服务层执行位置 | 真正 Alpaca 写入 |
| --- | --- | --- | --- |
| 自动买入 | `strategy_framework/components/buy.py::ModuleBuyStrategy.evaluate`，再进入对应 `strategy_*.py::evaluate_buy` | `service.py::execute_buy` 中 `broker.place_limit_buy(...)` | `broker.py::AlpacaStockBroker._submit_fixed_limit_order` 中 `client.submit_order(...)` |
| 自动止损卖出 | `strategy.py::evaluate_stop_loss` / `evaluate_sell` | `service.py::execute_sell` 中 `broker.place_limit_sell(...)` | `broker.py::AlpacaStockBroker._submit_fixed_limit_order` 中 `client.submit_order(...)` |
| 自动止盈/尾盘卖出 | `strategy.py::evaluate_sell` | `service.py::execute_sell` 中 `broker.place_market_sell(...)` | `broker.py::AlpacaStockBroker._submit_order` 中 `client.submit_order(...)` |
| 自动超时撤单 | `strategy_framework/components/cancel.py::wait_for_terminal` | Broker 每次成功提交后自动进入 | `order_guard.py::cancel_unfilled_order` 中 `client.cancel_order_by_id(...)` |
| 服务层兜底撤单 | `service.py::check_cancel` 通过 `models.has_unconfirmed_order_status(...)` 识别开放、部分成交和撤单未确认状态 | `service.py::execute_cancel` 中按唯一 `order_id` 调用 `broker.cancel_order(...)` | `order_guard.py::cancel_unfilled_order` 中 `client.cancel_order_by_id(...)` |
| 手动撤单 | `openclaw_trade_control.py::_execute_cancel` | `broker.cancel_order` / `cancel_open_orders` | `order_guard.py::cancel_unfilled_order` 中 `client.cancel_order_by_id(...)` |

订单生命周期的额外保护：

- Broker 提交成功后，无论终态策略或本地订单记录是否异常，都保留已提交订单的 `order_id` 和当前状态，让服务层继续按未确认暴露处理。
- Broker 为真实提交生成唯一 `client_order_id`；发生提交超时或网络异常时先据此恢复券商订单，无法确定是否已提交时返回 `SUBMIT_UNCONFIRMED` 并锁住后续自动买入。
- 部分成交加撤单失败仍保留已成交数量，并保持未确认状态；同一订单生命周期的买入名额按 `order_id` 去重。
- `DONE_FOR_DAY` 和 `REPLACED` 继续按未确认暴露处理；遇到替换订单时沿 `replaced_by` 追踪并操作当前订单。
- 自动卖出先读取开放卖单；同一股票已有卖单或查询失败时失败关闭，避免重复卖出。
- 服务层撤单结果必须与原订单的 `order_id`、方向和股票一致，才能覆盖原订单状态并解除相应暂停。

## 四层职责边界

### 1. 配置和装配层

- `workflows/monitoring/intraday.py`：用户选择策略组合、金额、止损止盈和撤单等待时间。
- `strategy_framework/runtime.py`：把 profile 和四类单项覆盖解析成不可变的 `StrategyRuntime`。
- `strategy_framework/registry.py`：注册并校验 WatchCode、买入、卖出和撤单实现。

这一层不读取行情、不连接券商、不下单。

### 2. 策略决策层

- `strategy_ma5_dip.py::evaluate_buy`：动态 MA5 回撤买入判断。
- `strategy_gap_confirmed_pullback.py::evaluate_buy`：缺口确认回撤买入判断。
- `strategy.py::evaluate_sell`：尾盘、止损、止盈判断。
- `strategy.py::evaluate_stop_loss`：观察池外持仓的止损判断。
- `strategy.py::evaluate_take_profit_remainder_stop`：半仓止盈后的剩余仓保护。

这一层只返回 `Signal`。即使返回 `BUY`、`SELL_HALF` 或 `SELL_ALL`，也还没有写入券商。

### 3. 服务编排和统一风控层

`service.py::run_once` 直接展示准备、逐股九阶段、汇总和释放资源。逐股核心顺序固定为：

`check_buy → execute_buy → notify_buy → check_sell → execute_sell → notify_sell → check_cancel → execute_cancel → notify_cancel`

不再通过 `process_symbol`、`process_buy_candidate`、`process_position` 等编排包装层转跳。每个阶段职责如下：

- `prepare_trading_round` / `start_trading_round`：准备策略、持仓、观察池、行情和本轮金额；
- `check_buy`：无持仓候选的买入分流、统一保护、行情读取和策略判断；
- `execute_buy`：自动买入唯一的服务层 Broker 调用位置；
- `notify_buy`：写入当日排除、本轮监控表和汇总；外部订单通知仍由 Broker 统一发送；
- `check_sell`：持仓卖出策略、旧仓保护和半仓止盈去重；
- `execute_sell`：自动卖出唯一的服务层 Broker 调用位置；
- `notify_sell`：写入本轮监控表和汇总；不重复发送 Broker 外部通知；
- `check_cancel`：识别 Broker 返回后仍未终态、且尚未请求撤单的订单；
- `execute_cancel`：只对自定义 Broker 直接返回的开放状态执行按单号兜底撤单；
- `notify_cancel`：合并撤单与成交竞态后的状态，只写一次本轮结果；
- `finish_trading_round` / `close_trading_round`：输出结果并只释放本轮创建的资源。

### 4. Broker 与订单保护层

- `broker.py::AlpacaStockBroker.place_*`：对外的买入/卖出入口。
- `broker.py::_submit_order`：常规盘 MARKET 或扩展时段保护 LIMIT。
- `broker.py::_submit_fixed_limit_order`：固定价格 BUY/SELL LIMIT。
- `strategy_framework/components/cancel.py`：选择订单等待与撤单实现。
- `order_guard.py::wait_for_fill_or_cancel`：轮询订单终态。
- `order_guard.py::cancel_unfilled_order`：真正发送撤单请求。

默认 Alpaca Broker 在 `execute_buy` / `execute_sell` 返回前已经完成终态等待或超时撤单。
服务层的 `check_cancel` / `execute_cancel` 不会重复处理 `CANCEL_REQUESTED` 或
`PENDING_CANCEL`；它只兜底保护没有遵守终态等待约定的自定义 Broker 适配器。
只有 Broker/订单保护层会调用 Alpaca 的 `submit_order` 或 `cancel_order_by_id`。

## 其他三条真实订单路径

这些路径不经过 `service.py::run_once`，排查时不能遗漏：

| 路径 | 用途 | 买卖/撤单入口 | 最终写入 |
| --- | --- | --- | --- |
| `manual_order.py` | 小额真实测试 BUY LIMIT | `place_test_order` -> `_submit_limit_buy` | 直接 `client.submit_order`，之后复用撤单策略 |
| `openclaw_trade_control.py` | 明确的手动买、卖、撤单指令 | `_execute_buy` / `_execute_sell` / `_execute_cancel` | 经 `AlpacaStockBroker` 和 `order_guard` |
| `afterhours_high_low.py` | 独立盘后 high/low 实盘策略 | 盘后买入代码块 / `preview_or_sell` | 买入直接 `client.submit_order`；卖出经 Broker |

OpenClaw 手动订单明确使用 `skip_time_validation=True`，表示跳过自动监控的本地时段筛选；
它不跳过 Alpaca 自身的订单校验，也仍然记录订单、通知并执行超时撤单。

## 修改代码时的判断方法

- 想改“什么时候出现 BUY/SELL 信号”：修改策略决策层。
- 想改“有信号后能否真的下单”：修改 `service.py` 的统一风控。
- 想改“使用 MARKET 还是 LIMIT、价格和股数如何提交”：修改 `broker.py`。
- 想改“等待多久、何时撤单、如何确认”：修改 CancelStrategy 和 `order_guard.py`。
- 想新增策略：实现对应契约，在 `strategy_framework/extensions.py` 显式注册，再加入 profile 或单项配置。

任何交易逻辑改动都必须先遵守
[`CODE_MODIFICATION_RULES.md`](../../CODE_MODIFICATION_RULES.md) 和
[`PROJECT_OPERATIONS.md`](../PROJECT_OPERATIONS.md) 的授权与验证要求。
