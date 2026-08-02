# 策略框架导航

这里把一套交易策略拆成四个可独立替换的组件：

```text
WatchCode 选股 -> 买入判断 -> Broker 下单 -> 撤单/终态确认
券商全部持仓  -> 卖出判断 -> Broker 下单 -> 撤单/终态确认
                                      |
                                      v
                              账本、通知、复盘
```

`StrategyProfile` 是四类组件和运行默认值的组合名。程序启动时先解析并校验整个组合，再创建行情或 Broker；名称错误、组件缺失或接口不完整都会直接终止。

当前盘中入口选择 `ma5_dip_ladder`：复用 `ma5_dip` 的 WatchCode/首次买入信号，在 `service.py` 与 `ladder.py` 中执行有状态三档建仓，以及仅针对首次 `+10%` 半仓止盈额度的三档卖出；整仓 `-10%` 止损以券商实际加权平均成本为基准。`ma5_dip` profile 保留为显式回滚/对照。分档状态和自动未终态订单分别从 `outputs/ladder_state.json`、`outputs/pending_orders.json` 校验装载；自动提交不阻塞逐股循环，后续轮次按订单 ID 对账并幂等应用累计成交。

## 目录

```text
strategy_framework/
├─ components/
│  ├─ watchcode.py   # WatchCode 筛选组件
│  ├─ buy.py         # 买入信号组件
│  ├─ sell.py        # 卖出信号组件
│  └─ cancel.py      # 等待、撤单、终态确认组件
├─ profiles.py       # 把四类组件组成可选的完整策略
├─ contracts.py      # 四类协议和不可变 profile/runtime 数据
├─ registry.py       # 分类注册、重名和引用校验
├─ runtime.py        # profile + 单项覆盖 -> StrategyRuntime
├─ extensions.py     # 可信自定义策略的唯一显式注册点
├─ builtins.py       # 内置组件注册顺序和旧导入兼容层
└─ names.py          # 跨模块共享的内置名称
```

## 想改什么，就去哪里

| 目标 | 修改位置 |
| --- | --- |
| 改当前使用的组合或只替换某一类策略 | `alpaca_ma5_service/workflows/monitoring/intraday.py` 顶部配置区 |
| 新增 WatchCode 筛选 | `components/watchcode.py` 或独立实现，再到 `extensions.py` 显式注册 |
| 新增买入逻辑 | `components/buy.py` 或独立实现，再到 `extensions.py` 显式注册 |
| 新增卖出逻辑 | `components/sell.py` 或独立实现，再到 `extensions.py` 显式注册 |
| 新增撤单和订单终态处理 | `components/cancel.py` 或独立实现，再到 `extensions.py` 显式注册 |
| 新增一整套可选组合 | `profiles.py`；自定义组合则在 `extensions.py` 注册 |
| 修改交易日、买入时间窗、订单防重等安全规则 | 不在策略组件内修改；沿 `service.py` 和现有安全层处理 |

完整项目日流程见 [`docs/architecture/PROJECT_FLOW.md`](../../docs/architecture/PROJECT_FLOW.md)，
买入、卖出、撤单和真正 Alpaca 写入点见
[`docs/architecture/TRADE_ORDER_FLOW.md`](../../docs/architecture/TRADE_ORDER_FLOW.md)，
扩展契约和稳定性要求见
[`docs/architecture/STRATEGY_FRAMEWORK.md`](../../docs/architecture/STRATEGY_FRAMEWORK.md)。
