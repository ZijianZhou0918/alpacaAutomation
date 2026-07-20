# Alpaca MA5 项目运行与修改准则

本文是本项目的长期运行手册。每次开始新的排查、开发、测试、部署或实盘检查前都必须完整阅读；修改代码前还必须完整阅读根目录 [`CODE_MODIFICATION_RULES.md`](../CODE_MODIFICATION_RULES.md)。项目根目录 `AGENTS.md` 会强制 Codex/代理执行这两项要求。

## 1. 项目性质与最高风险

- 这是会连接 Alpaca 券商的自动交易项目，不是纯展示项目。
- `.env` 中填入 Paper key 或 Live key 后，程序会自动识别账户类型；不能根据文件名或开发环境主观假定一定是 Paper。
- 行情、账户、日历、持仓和订单查询属于只读验证；任何 `submit_order`、手动测试单、监控真实买入入口都可能改变真实账户。
- 未获得用户对“真实下单”的明确授权时，只允许进行只读验证和使用 Fake/DryRun broker 的测试。
- `tools/run_test_order.py` 会提交订单，不属于普通连接测试，禁止在常规回归中运行。
- 永远不要重启这台电脑。

## 2. 每次开始工作的固定顺序

1. 完整阅读本文件。
2. 运行 `git status --short`，保留用户已有改动和运行时文件。
3. 查看项目相关进程和网页健康状态，判断是否已经有生成器、监控器或服务运行。
4. 明确这次工作是否会接触 Live 账户、定时任务、WatchCode、订单账本或复盘计算。
5. 优先做最小的只读诊断；确认根因后再修改。
6. 代码修改后执行与风险相称的测试、运行态验证和服务重启。

## 3. Python 环境与密钥

- 默认解释器：`C:\Users\zzj\Desktop\alpaca_ma5_service\.venv\Scripts\python.exe`。
- 根目录 `monitor_*.py`、`watchcode_*.py`、`run_backtest*.py` 和 `open_daily_review.py` 只保留公开运行入口；实际编排分别位于 `alpaca_ma5_service/workflows/` 与 `backtest/runners/`。
- 盘中可编辑运行参数位于 `alpaca_ma5_service/workflows/monitoring/intraday.py`；根目录 `monitor_ma5_forever.py` 继续作为点击运行入口。
- `.env` 已被 `.gitignore` 排除。不得输出、复制、提交或在文档中记录真实 key。
- 检查连接时只打印模式、账户状态和阻塞标志等非敏感信息；不要打印凭据或完整账户对象。
- Moomoo 实时行情依赖本机 OpenD 时，应先确认 OpenD 已启动且端口与 `.env` 一致；行情源故障不能通过放宽下单保护来绕过。
- PowerShell 控制台出现中文乱码时，先判断是否只是控制台编码问题，禁止未经核实批量重写源文件编码。

## 4. WatchCode 的语义和生成顺序

完整日流程图和入口导航见 [`architecture/PROJECT_FLOW.md`](architecture/PROJECT_FLOW.md)。最短链路是：

`monitor_auto.py` → 交易日/时段 → 对应 WatchCode → 盘前提醒、盘中交易或盘后提醒 → 账本/通知/复盘。

### 文件职责

- `data/watchcodes/watch_codes.txt`：盘中自动交易观察池，会参与盘中买入判断。
- `data/watchcodes/watch_codes_premarket.txt`：盘前推荐观察池，只用于推荐提醒，不参与盘中自动买入。
- `data/watchcodes/watch_code_afterhours.txt`：盘后策略观察池，必须按具体入口确认是提醒模式还是可下单模式。
- 这些文件是运行时产物，可能在工作区显示为已修改；没有用户要求时不要回滚、清空或用旧文件覆盖。
- 默认 `ma5_dip` WatchCode 筛选要求信号日收盘价比包含该日收盘价计算的当日 `MA5` 至少高 15 个点（`close/MA5 >= 1.15`），但不要求 `MA5 > MA10 > MA20`；三条均线仍会计算并写入候选报告和图表。
- `watchcode_ma5.py`、`monitor_auto.py` 和盘中监控必须共用 `alpaca_ma5_service.workflows.monitoring.intraday.build_monitor_settings()`；不得在生成器中另写固定策略名，避免观察池和实际买入策略静默漂移。

### 日期规则

- 文件顶部 `signal_date` 必须对应最近一个已完成的美股交易日。
- 交易日必须通过 `alpaca_ma5_service/trading_calendar.py` 判断，包含周末和标准美股节假日；禁止只使用 `weekday() < 5`。
- 当天日线未准备好时使用前一个交易日；日线准备完成后才允许使用当天。
- 看板、自动监控和 04:00 健康检查必须使用同一套日期规则。

### 并发规则

- 盘前生成任务名为 `watchcode_premarket`，盘中生成任务名为 `watchcode_ma5`。
- 启动任一监控前，如果对应 WatchCode 缺失或过期，必须先生成。
- 如果任一盘前/盘中生成器已经运行，启动监控的进程必须等待它结束，不能在 5 秒后再启动第二个生成器。
- 网页、PyCharm、直接 Python 和 Windows 定时任务都可能启动任务，不能只检查当前网页进程内的状态。

## 5. 监控阶段与真实下单边界

- 盘中外部入口固定为：`monitor_ma5_forever.py` → `workflows/monitoring/intraday.py` → `service.run_forever()` → `service.run_once()`。`run_once()` 内逐股直接执行 `check_buy → execute_buy → notify_buy → check_sell → execute_sell → notify_sell → check_cancel → execute_cancel → notify_cancel`，不再经过 `process_*` 中间编排。WatchCode 进入买入三阶段，券商全部持仓进入卖出三阶段；默认 Broker 提交后内部完成撤单/终态确认，服务层撤单三阶段只兜底自定义 Broker 返回的开放订单。四类实现目录见 `alpaca_ma5_service/strategy_framework/README.md`。
- `alpaca_ma5_service/workflows/monitoring/intraday.py` 顶部先选择基础 `STRATEGY_NAME`，再分别选择 `WATCHLIST_STRATEGY_NAME`、`BUY_STRATEGY_NAME`、`SELL_STRATEGY_NAME` 和 `CANCEL_STRATEGY_NAME`。四类可以独立组合，空环境变量则继承 profile。
- `.env` 可用 `STRATEGY_PROFILE`、`WATCHLIST_STRATEGY`、`BUY_STRATEGY`、`SELL_STRATEGY`、`CANCEL_STRATEGY`；入口显式值优先，旧 `strategy_name` 仍兼容为 profile 名。
- 启动时必须先解析完整 profile 和四类策略；未知名称、重复注册、缺少接口或 profile 缺少组件时应在行情、账户和订单 I/O 前失败关闭。
- 自定义实现只允许在 `alpaca_ma5_service/strategy_framework/extensions.py` 中显式、可信注册；禁止让 `.env` 或网页值成为任意 Python 导入路径。配置切换需重启对应运行进程。
- `monitor_auto.py` 是统一自动入口，负责根据时间进入盘前、盘中或盘后阶段。
- 盘前 `monitor_premarket_ma5.py` 是推荐提醒链路，不提交买单。
- 盘中 `monitor_ma5_forever.py` 才会依据策略提交真实订单。
- 盘后存在不同入口；修改前必须沿调用链确认当前入口是提醒模式、DryRun、Paper 还是允许真实订单，不能仅凭文件名判断。
- 真实盘中买入只允许在美股交易日 `09:30 <= t < 12:00 ET`。
- 休市日即使是周一到周五，也必须阻止盘前、盘中和盘后的下单窗口。
- 自动买入使用最终买点作为 BUY LIMIT，不使用当前价追单。
- 卖出风控会检查券商当前持仓，可能包含不在 WatchCode 中的手动持仓。
- 每轮卖出前还会查询券商开放卖单；同一股票已有开放卖单，或查询失败无法确认时，本轮不得再次提交卖单。
- 同一股票的拒单/错误保护、每日买入上限、未完成订单和重复订单保护不得在修复 UI 时绕开。
- 默认自动撤单策略 `timeout_cancel_confirmed` 会按设置等待、请求撤单并复查最终订单状态；切换取消策略不得跳过最终状态确认或降低真实账户保护。
- 部分成交后只要剩余数量尚未确认终态，仍按未确认订单处理；同一 `order_id` 的完整生命周期只计一次当日买入名额。
- 订单提交后即使终态等待、撤单或本地 CSV 记录失败，也必须保留真实 `order_id` 并暂停后续自动买入。服务层只接受与原订单 `order_id`、方向和股票一致的撤单结果，不能用无归属的撤单错误解除暂停。
- 真实订单使用唯一 `client_order_id`。提交超时或网络异常时先按该标识恢复券商订单；既无法恢复、又不能确认是明确拒单时，按 `SUBMIT_UNCONFIRMED` 暂停后续自动买入，不得盲目重试。
- `DONE_FOR_DAY` 仍可能在下一交易日更新，`REPLACED` 表示暴露已经转移到替换订单；监控必须沿 `replaced_by` 跟踪当前订单，不能把两者当成安全终态。

## 6. 网页看板准则

### 服务与接口

- 每日复盘入口：`open_daily_review.py` / `open_daily_review.cmd`。
- 默认地址：`http://127.0.0.1:8788/`。
- 健康检查：`GET /api/review/health`。
- 任务控制状态：`GET /api/actions/status`。
- 图表服务通常使用 `8766`，但必须根据健康检查和实际端口确认，不能假定旧 URL 仍指向当前目录。

### 日期展示

- 用户选择哪一天，就只展示那一天。
- “今日”必须始终允许进入，即使今日休市、停牌、没有订单或数据尚未生成。
- 空数据应显示明确的空状态、休市或待生成原因，不能回退并混入前几个交易日的记录。

### 资金与盈亏

- 净资金流水是现金流入减现金流出，不等于当日已实现收益或账户盈亏。
- 买入会造成较大的负向现金流，卖出会造成正向现金流；不能把成交金额直接显示成盈利。
- 收益指标应明确区分：已实现盈亏、未实现盈亏、费用、净资金流水和账户净值变化。
- 本地订单记录与券商数据不一致时，优先判断是否为用户手动买入/卖出。这是正常解释项，不应自动修复或伪造本地记录。

### 实时任务和日志

- Python、PyCharm、定时任务和网页按钮启动的任务都应通过 `monitor_runtime` 登记，网页才能发现并实时显示。
- 任务刚启动时应立即登记，不能等第一批行情或扫描完成才出现。
- 空闲轮询可以较慢；发现任务后应切换到实时更新模式。
- 日志中的 HTTP/HTTPS URL 可以被识别成可点击链接，但不得自动打开不受信任或非本项目地址。
- 停止按钮只能结束已登记且命令行属于本项目的任务进程树。

## 7. Windows 定时任务

- `AlpacaMA5-2200-GenerateWatchcode-PyCharm`：本地时间 22:00，为下一个交易日生成盘中和盘前 WatchCode。
- `AlpacaMA5-0050-EnsureMonitor-PyCharm`：本地时间 00:50，确保 `monitor_auto.py` 已启动。
- `AlpacaMA5-0400-HealthCheck-PyCharm`：本地时间 04:00，检查监控进程和两个 WatchCode。
- 22:00 检查“明天”是否为交易日；00:50 和 04:00 检查“今天”。
- `LastTaskResult` 是上一次运行的历史结果，不能单独作为当前故障结论；必须同时读取当天对应日志。
- 定时任务返回 `1` 时，先检查 `.venv\pyvenv.cfg`、解释器路径、当天任务日志和交易日日历输出。
- WatchCode 日志跟随窗口是辅助界面；主任务结束后必须在 `finally` 中关闭，不能因尾随窗口仍存在而让任务看起来一直运行。
- 不要为了刷新历史返回码而随意手动运行 00:50 或 04:00 脚本，因为它们可能启动真实监控或重新生成大量数据。

## 8. 通知链路

- 交易主流程不得因为 Telegram、Hermes、OpenClaw 或云端通知失败而崩溃。
- 通知错误必须记录为独立告警，并保留交易任务原本的成功/失败语义。
- 外部 Hermes 环境、Telegram target 等不属于本项目时，未经用户要求不要跨项目修改。
- 通知成功不代表订单成功；订单状态必须以 Alpaca 返回和后续订单查询为准。

## 9. 进程和文件安全

- 禁止使用 `Get-Process python | Stop-Process` 一类批量命令。
- 结束进程前必须检查 `Win32_Process.CommandLine`，同时匹配本项目绝对路径和具体入口模块。
- 重启网页时只结束 `alpaca_ma5_service.review_web` 对应进程树，不要结束图表、监控、PyCharm或其他项目。
- 修改前后都要保留无关 dirty worktree；尤其不要回滚 `watch_codes*.txt` 等用户运行产物。
- `.env`、`outputs/`、回测数据和临时附件不应被加入 Git。
- 代码编辑使用小范围补丁；不得通过大规模格式化掩盖真实修改。
- 本机重型任务必须全局串行，禁止并行运行全量测试、回测、数据修复、大规模生成或构建任务。

## 10. 修改后的最低验收清单

根据改动范围执行以下项目，禁止为了“验证”提交真实订单。

### 代码与测试

```powershell
git diff --check
.\.venv\Scripts\python.exe -c "import sys, unittest; suite=unittest.defaultTestLoader.discover('tests', pattern='test_*.py'); result=unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(0 if result.wasSuccessful() else 1)"
```

- 只修改局部模块时可以先运行定向测试，但交易、调度、日期、订单或网页接口改动最终应跑完整测试。
- PowerShell 定时脚本改动后，用 PowerShell AST Parser 做语法检查。
- 日期逻辑至少覆盖一个周末和一个美股节假日回归用例。
- 订单逻辑测试必须使用 Fake/DryRun broker，并断言没有调用真实提交接口。

### 运行态

1. 重启受影响的项目服务。
2. 检查 `/api/review/health` 返回 `ok=true`、项目目录和端口正确。
3. 检查 `/api/actions/status`，确认两个 WatchCode 的日期、数量、生成状态和监控状态合理。
4. 如需验证 Alpaca，只读取账户模式、ACTIVE 状态、阻塞标志、交易日历和必要的订单状态。
5. 明确记录验证期间是否提交订单；默认答案必须是“没有”。

## 11. 何时必须同步更新本文

以下内容变化时，代码修改不能单独完成，必须同步更新本文：

- Paper/Live 识别或下单授权规则。
- 交易日、买入窗口、盘前/盘中/盘后边界。
- WatchCode 文件名、信号日期或生成流程。
- 网页端口、API 路由、任务登记和停止逻辑。
- Windows 定时任务名称、时间或目标日期。
- 资金流水、盈亏和手动交易对账口径。
- 测试、部署、重启或运行态验证命令。

## 12. 两年全普通股日线 SQLite

### 数据口径

- 正式路径：`backtest/data/market_data.sqlite`；重建 staging：`backtest/data/market_data.sqlite.rebuild`。
- 所有依赖日线的回测必须优先只读正式库。覆盖不足时必须明确报告缺口，不得静默切换到其他日线库；确需补数据时使用正式重建入口，或在用户明确授权后写入独立缓存。
- 来源为 Alpaca Market Data SIP，周期 `1Day`，`split` 复权，字段包含 OHLC、volume、VWAP、transactions、timestamp 和 MA5/10/20。
- 日期由 Alpaca calendar 和统一离线交易日历交叉校验，正式库只写 `daily_bars`，`minute_bars` 必须保持 0 行。
- 统一离线日历包含 2025-01-09 全国哀悼日休市，且重建会与 Alpaca 日历交叉校验。
- 股票池合并当前 Alpaca active/inactive US equity 快照，并用当前 Nasdaq Trader 的 ETF/Test Issue 和证券名称标记先排除 ETF、ETN、权证、权利、单位、优先股、债券、基金、SPAC、非经营性 Trust、结构化证券、OTC 和测试证券，再纳入 NYSE/NASDAQ/AMEX 上其余上市股。普通股名称不需要显式包含 `Common Stock`；REIT 和 Property Trust 作为经营性上市股保留。
- 当前目录不是权威历史时点证券主表。inactive/退市候选会纳入，但不能宣称已彻底消除幸存者偏差；以 manifest 的 `survivorship_bias_fully_eliminated=false` 为准。

### 重建和替换

```powershell
.\.venv\Scripts\python.exe run_backtest_daily_history_rebuild.py --start-date 2024-07-17 --end-date 2026-07-16
```

- 单个重建进程默认使用 4 路只读 HTTP 下载流，SQLite 写入保持串行；不得同时启动第二个回填、回测、构建或数据修复任务。
- 每个股票批次完整下载后才写入 staging，并为全部候选（包括区间内没有返回日线的证券）记录 `daily` 覆盖范围。
- 所有批次完成后核对 metadata、`daily_bars` 实际行数、交易日范围、OHLC、全股票池覆盖标记、空 `minute_bars` 和 `PRAGMA quick_check`。
- 正式库已存在时先用 SQLite backup API 写入 `backtest/output/market_data_before_daily_replace_*.sqlite` 并校验，再用同卷原子替换 staging。
- 完成后更新 `backtest/output/daily_history_rebuild_manifest.json` 和日志；manifest 固定记录 `timeframe=1Day`、`minute_rows=0` 和证券池局限。

该入口只读取资产目录、交易日历和历史行情，不读取持仓、不查询或提交订单，也不启动 WatchCode、监控、网页或计划任务。全量回填属于重型任务，必须全局串行。

## 13. 信号日强势 + 动态 MA5 回测

入口：`run_backtest_signal_dynamic_ma5.py`。

- 信号日使用正式日线库筛选全普通股：`MA5 > MA10 > MA20`，相对前收涨幅严格大于 `10%`，`close / open - 1` 严格大于 `10%` 且收阳。
- 买入候选必须是全局下一交易日，并且该日正式日线开盘价相对信号日收盘价严格上涨；开盘涨幅等于或小于 0 时排除，缺失下一交易日日线时不得顺延到后续日期。
- 动态 MA5 固定为“信号日及其之前共 4 个已完成交易日收盘价 + 当前已完成 1 分钟 K 线收盘价”除以 5。
- 买入触发固定为当前已完成 1 分钟 K 线收盘价小于或等于动态 MA5，且该收盘价相对买入日开盘价跌幅严格大于 `15%`；等于 `15%`、高于动态 MA5 或全天未达到条件时不得买入。触发后在下一根真实 1 分钟 K 线开盘成交，且该实际入场价相对买入日开盘价跌幅仍须严格大于 `15%`；反弹后不满足时继续等待窗口内后续有效触发。实际成交时间必须满足 `09:30 <= t < 12:00 ET`；12:00 ET 及以后禁止买入，窗口内触发但缺少窗口内下一根真实分钟线时不得伪造成交。
- 盈利 `5% / 10% / 15%` 各卖出原始仓位 `1/3`；亏损 `10%` 卖出全部剩余仓位；最后一根常规盘分钟线收盘卖出剩余仓位。同一分钟高低价同时触及止损和止盈时止损优先。
- 正式 `backtest/data/market_data.sqlite` 全程只读且 `minute_bars` 必须保持 0 行。候选分钟线仅写入 `backtest/data/signal_dynamic_ma5_minute_cache.sqlite`，feed 固定 SIP、复权固定 `split`；SIP 失败必须中止，不得静默混入 IEX。
- 输出固定为 `backtest/output/signal_dynamic_ma5/` 下的 JSON、CSV 和 HTML。默认每笔候选独立使用 `$10,000` 名义本金，佣金和滑点为 0；总盈亏不能描述为资金容量受限的组合收益。
- 股票池虽包含重建时能识别的 active/inactive 普通股，仍不能宣称完全消除幸存者偏差。

运行命令：

```powershell
.\.venv\Scripts\python.exe run_backtest_signal_dynamic_ma5.py
```

该入口只读取本地日线和历史 SIP 分钟行情；不得读取账户、持仓或订单，不得调用任何提交订单入口，也不启动 WatchCode、监控、网页或计划任务。分钟下载和回测属于重型任务，必须全局串行。

最低验证：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_signal_dynamic_ma5_backtest -v
```

## 13.1 Gap pullback 受限优化与验证

入口：

```powershell
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase baseline
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase stage1
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase stage2
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase holdout --start-date 2025-10-01 --end-date 2025-12-31
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase robustness --start-date 2025-01-01 --end-date 2025-12-31
.\.venv\Scripts\python.exe run_backtest_gap_strategy_validation.py

# 总收益目标（10bp/成交，固定 $100,000，无杠杆）
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase return_signal
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase return_sizing
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase return_holdout --start-date 2025-10-01 --end-date 2025-12-31
.\.venv\Scripts\python.exe run_backtest_gap_strategy_optimization.py --phase return_robustness --start-date 2025-01-01 --end-date 2025-12-31
.\.venv\Scripts\python.exe run_backtest_gap_strategy_return_validation.py
```

- 开发窗口固定为 2025-01-01..09-30；stage1 固定 25 个单因素试验，stage2 固定 6 个相邻组合。冻结参数为相对信号日收盘回撤 `-8%..-5%` 买入、盈利 `4%` 全部卖出。
- 2025-Q4 是一次性留出集；`frozen_selection_manifest.json` 必须在任何 Q4 分钟下载前写出，留出结果不得用于继续调参。2026 固定为用户外部交叉验证集，研究入口对任何 2026 日期硬失败。
- 日线只读 `backtest/data/market_data.sqlite`；分钟线仅写 `backtest/data/gap_strategy_2025_minute_cache.sqlite`，feed 固定 SIP、复权固定 `split`，不得回退 IEX。
- 结果在 `backtest/output/gap_strategy_optimization/`，包括逐阶段 JSON/CSV、逐笔交易、冻结清单、验证 JSON/Markdown 和可执行 notebook。零成本、10bp/笔和 25bp/笔滑点结果必须同时保留。
- 该入口只读历史行情，不读取账户、持仓或订单，不启动监控、WatchCode、网页或计划任务。回测和下载属于重型任务，必须全局串行。
- 当前盘中默认已按用户明确要求切换为 gap profile，运行时使用 `$2,500`、每日最多 3 笔/3 个持仓、`-8%` 止损和 `+4%` 全部止盈；WatchCode 同样执行信号日振幅不超过 `30%`。`ma5_dip` 仍保留为可选 profile，但不是 `workflows/monitoring/intraday.py` 的当前默认值。
- 当前代码的 daily-3 优化入口为 `run_backtest_gap_strategy_current_daily3_optimization.py`：`development` 只读 2025-01-01..09-30 并先冻结，`diagnostic` 只读 2025-Q4，`validate-2026` 只允许一次读取 2026-01-01..07-17 且拒绝覆盖已有结果。研究使用正式只读日线库、独立 SIP 分钟缓存和每次成交 `10bp` 成本，不读取账户或订单。
- 总收益阶段先在相同 `$3,500` 仓位测试 12 个受限信号，再对冻结信号测试 7 个现金配置；候选必须至少 500 笔、利润因子不低于 1.10、开发期最大回撤不差于 `-15%`，且 Q1/Q2/Q3 都盈利。
- 总收益候选为 `$20,000 × 5`、回撤至少 4%、信号日收盘位置至少 60%、4% 全部止盈；它的 Q4 绝对收益和利润因子未通过，因此不得写入 profile、不得启动 Paper/Live，必须等待冻结标准后的 2026 盲测。
- 总收益产物在 `backtest/output/gap_strategy_return_optimization/`；`return_frozen_selection_manifest.json` 必须先于该候选 Q4 结果，事后 matched-sizing 对照只能做归因，不能改变候选。

最低验证：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_gap_strategy_optimization tests.test_strategy_validation tests.test_gap_strategy_validation_report tests.test_intraday_workflow_config -v
.\.venv\Scripts\python.exe run_backtest_gap_strategy_return_validation.py
```

## 14. 日内动态涨幅榜回测网页

### 边界

- 源码、配置、测试和网页都在 `intraday_top20/`。
- 该模块只读取本地历史行情，不需要项目 `.env`，不连接 Paper/Live 账户，也没有订单入口。
- 它不复用或重启 review web、WatchCode 生成器、监控器和计划任务。对该模块的修改无需重启现有 MA5 服务。
- 合成样例只用于功能验收。真实策略结论必须通过网页显示的数据可靠性门禁。

### 安装、生成、回测和网页

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m intraday_top20.data.sample_data
.\.venv\Scripts\python.exe -m intraday_top20.run_backtest --force
.\.venv\Scripts\python.exe -m streamlit run intraday_top20\app.py
```

稳健性测试：

```powershell
.\.venv\Scripts\python.exe -m intraday_top20.run_backtest --robustness
```

测试：

```powershell
.\.venv\Scripts\python.exe -m pytest intraday_top20\tests -q
```

回测、完整稳健性测试和完整测试均属于本机重型任务，必须服从全局串行和 heavy-task guard；不得与 Gradle、Android、Electron 构建或其他回测并发。

### 数据和缓存

- 默认配置：`intraday_top20/config/default_config.yaml`。
- 真实行情目录由 `data.data_dir` 指定；文件名必须含交易日，字段、证券主表和拆股表格式见 `intraday_top20/README.md`。
- `intraday_top20/example_data/` 和 `intraday_top20/outputs/` 是运行生成目录，已忽略。前者只放行情和证券参考数据；后者包含行情 Parquet 缓存、结果压缩 CSV、manifest、配置、日志和稳健性结果。
- 参数不变且数据指纹不变时加载结果缓存；参数变化只重跑事件回测，清洗后的日行情缓存可以复用。

### 最低运行验收

1. 全部 `intraday_top20/tests` 通过，特别是动态排名、连续跌破、重新站上、下一根开盘、止盈半仓、15:55、成交量参与率、成本和停牌缺 K 线测试。
2. CLI 基准回测成功保存结果，`future_data_used=false`，示例数据 `data_reliability_gate_passed=false`。
3. 临时启动 Streamlit 后首页返回 HTTP 200；浏览器可见合成数据警示、Plotly 图和交易表。
4. 验证完成只停止本次明确启动的 Streamlit 进程，不影响端口 8766/8788 或其他项目服务。
5. 明确记录：没有读取账户，没有提交订单，没有修改 WatchCode、定时任务或现有服务。

## 15. 通用交互式回测报告

`backtest/reporting/` 是与具体策略解耦的报告能力。`InteractiveReportDocument` 只描述标题、徽标、数据门禁、章节和数据集；`renderer.py` 负责把模板、CSS、JavaScript 和转义后的 JSON 合并成可直接打开的主 HTML。`backtest/engine.py` 只负责把 `BacktestResult`、交易轮次和行情证据适配成该文档，新的历史研究模块也可以直接复用报告包而不依赖账户、订单或 MA5 实盘链路。大体量分钟行情按股票写入 `symbol_details/*.minute.js`，仅在日期下钻时加载，主 HTML 不嵌入全量分钟数据。

当前交互报告提供：

- 权益曲线、汇总统计、按股票证据、逐笔交易、审计和配置章节；
- 股票搜索、盈利/亏损/多轮筛选、按已实现盈亏排序；
- 逐股证据按每只股票最近一次交易时间默认从新到旧排列，可切换最早优先，筛选后保持当前时间顺序；
- 买入日附近日 K、MA5/MA10/MA20、成交量、信号/买入/卖出事件轨；
- 买卖箭头精确锚定成交日期和成交价，并给出成交价位于日 K 实体、影线、区间外或缺失的核验说明；
- 轮次已实现收益与股票累计已实现收益并列展示，避免把单轮结果误认为股票总结果；
- 点击日 K 或事件日期下钻当天 1 分钟 K，按完整成交时间和成交价标注买卖点；缺数据时不回退日期、不吸附相邻 K 线；
- 多轮交易前后切换、URL 深链、键盘左右键、焦点管理、打印和窄屏布局；
- Plotly 不可用时仍保留可读的表格和图表降级提示。

报告层修改的最低验证：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_backtest_reporting tests.test_backtest.BacktestTests.test_symbol_detail_table_defaults_to_latest_activity_first tests.test_backtest.BacktestTests.test_backtest_reuses_strategy_and_writes_html_report -v
```

然后用真实浏览器至少检查一个桌面视口和一个手机视口，验证章节导航、筛选、股票详情、多轮切换、日 K 点击、分钟 K 按需加载、精确买卖标记、缺数状态、深链和键盘关闭。报告生成只处理历史结果，不读取账户、不提交订单，也不需要重启现有 review web、监控或 WatchCode 进程。
