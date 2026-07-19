# 动态策略框架

> 想先看整个项目每天怎么运行，请先读 [`PROJECT_FLOW.md`](PROJECT_FLOW.md)；想直接定位代码，请读 [`../../alpaca_ma5_service/strategy_framework/README.md`](../../alpaca_ma5_service/strategy_framework/README.md)。

## 目标与边界

项目采用模块化单体，不拆微服务。WatchCode、买入、卖出和自动撤单分别有独立契约与注册命名空间；`StrategyProfile` 只负责把四类策略和运行默认值组合成一个可复用名称。所有组件在外部行情、账户或订单 I/O 前一次性解析，非法配置失败关闭。

框架覆盖盘中 WatchCode、自动交易和通用回测主链路。盘前推荐仍只提醒；盘后各入口仍保留各自信号与下单边界，但已提交订单后的等待/撤单复用所选择的取消策略。历史专项回测如果有独立策略定义，不会被静默改成实盘策略。

## 目录职责

| 文件 | 职责 |
| --- | --- |
| `contracts.py` | `WatchlistStrategy`、`BuyStrategy`、`SellStrategy`、`CancelStrategy` 协议及不可变 profile/runtime 数据 |
| `registry.py` | 四个独立注册表、profile 注册、接口和引用完整性校验、原子启动 |
| `runtime.py` | profile、分类覆盖和 `Settings` 的统一解析 |
| `components/watchcode.py` | 内置 WatchCode 筛选适配器 |
| `components/buy.py` | 内置买入信号适配器 |
| `components/sell.py` | 标准盘中卖出组件 |
| `components/cancel.py` | 超时撤单和订单终态确认组件 |
| `profiles.py` | 把四类已注册组件组合成完整可选策略 |
| `builtins.py` | 按交易流程注册内置组件，并保留旧类导入兼容 |
| `extensions.py` | 项目可信的自定义策略显式注册点 |
| `names.py` | 跨模块共享的内置策略名 |

架构流向见 [`strategy-framework.mmd`](strategy-framework.mmd)，采用决策见 [`adr/0001-modular-strategy-registry.md`](adr/0001-modular-strategy-registry.md)。

## 配置模型

`STRATEGY_PROFILE` 选择完整基础组合。`WATCHLIST_STRATEGY`、`BUY_STRATEGY`、`SELL_STRATEGY`、`CANCEL_STRATEGY` 可以分别覆盖其中一项。直接运行时以 `alpaca_ma5_service/workflows/monitoring/intraday.py` 顶部的同名 Python 常量为权威；根目录 `monitor_ma5_forever.py` 只负责启动，显式 workflow 值优先于环境变量。

内置选择：

| 分类 | 可选名称 |
| --- | --- |
| profile | `ma5_dip`, `gap_confirmed_pullback_g8_r30_b10_st8_tp8` |
| WatchCode | `ma5_dip`, `gap_confirmed_pullback_g8_r30_b10_st8_tp8` |
| 买入 | `ma5_dip`, `gap_confirmed_pullback_g8_r30_b10_st8_tp8` |
| 卖出 | `standard_intraday_exit` |
| 自动撤单 | `timeout_cancel_confirmed` |

`strategy_name` 作为旧 profile 参数继续兼容。实时主链路使用每个 `Settings` 对应的不可变 `StrategyRuntime`，不会修改进程全局策略；旧全局上下文只供旧回测调用兼容。

## 新增策略

1. 在对应的 `components/` 模块或独立模块实现协议。一个策略只负责一个分类，不直接访问注册表。
2. 为实现提供稳定、非空的 `name` 和可读 `description`。
3. 内置实现由对应 `components/*.py` 注册；自定义实现只在 `extensions.register_custom_strategies()` 中显式调用 `register_watchlist`、`register_buy`、`register_sell` 或 `register_cancel`。
4. 如果要提供整套选择，所有组件先注册，再在 `profiles.py` 或可信扩展入口注册 `StrategyProfile`；profile 必须包含全部运行默认值。
5. 为边界值、拒绝路径、分类组合和实际调用方增加 fake 单测，再执行全量项目测试。
6. 修改 WatchCode 语义、交易窗口、订单保护或配置变量时，同步更新 README、运行手册和代码修改准则。

不允许把环境变量或网页输入解释成任意模块路径。这样会扩大代码执行面，也无法在启动时证明接口完整。

## 稳定性不变量

- 同一分类不允许重复名称；不同分类允许使用相同名称。
- profile 注册时立即验证四个引用和运行默认值。
- 全局注册表在临时候选对象中完整构建，扩展失败不会留下半初始化状态。
- `build_settings()` 先完成所有策略名解析，再创建任何 broker 或行情对象。
- 交易日、`09:30 <= t < 12:00 ET` 买入窗口、重复订单、买入上限、拒单保护、订单最终状态确认均保留在策略之外或现有安全层中，不能由新策略绕过。
- WatchCode 生成和盘中监控共用同一个 `build_monitor_settings()`。
- 回测使用同一运行时买入/卖出/WatchCode组件，但不读取账户、不提交订单。

## 发布与回滚

策略切换在进程启动时生效，因此配置修改后只重启对应项目进程。回滚时恢复上一组 profile/分类名称并重启；不要回滚或覆盖 `watch_codes*.txt`、订单账本和运行日志。真实监控启动仍需要明确授权，回归测试只使用 fake broker。
