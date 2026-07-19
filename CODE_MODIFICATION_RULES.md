# Alpaca MA5 代码修改准则

> 状态：长期有效，适用于本仓库及全部子目录  
> 最后一次架构核对：2026-07-18
> 执行入口：项目根目录 `AGENTS.md`  
> 运行手册：`docs/PROJECT_OPERATIONS.md`

本文件是本项目代码修改的强制准则。它既记录当前架构和数据边界，也规定修改、验证与交付方式。任何人或自动化代理在修改 Python、JavaScript、CSS、HTML、PowerShell、测试、配置样例或任务脚本前，都必须完整阅读本文件和 `docs/PROJECT_OPERATIONS.md`；不得只依赖历史记忆、README 摘要或文件名推断行为。

## 1. 修改前强制门禁

在写入任何代码或配置前，依次完成：

1. 完整阅读 `docs/PROJECT_OPERATIONS.md`。
2. 完整阅读本文件，不得只读局部章节。
3. 运行 `git status --short`，标记用户已有改动、运行时产物和本次拟修改文件；不得覆盖、回滚或格式化无关改动。
4. 判断本次修改是否触及以下任一高风险面：Live/Paper 账户、订单提交或撤单、持仓、交易日和时间窗口、WatchCode、监控进程、Windows 定时任务、运行时账本、SQLite 数据修复、LAN 写接口、通知外发、密钥或认证。
5. 沿真实调用链确认入口、实现和副作用。对交易代码尤其不能仅凭函数名、常量名、注释或 `dry_run` 默认值判断是否会下单。
6. 检查本项目现有生成器、监控、看板和定时任务进程，避免重复启动；文档修改不需要打断正在运行的服务。
7. 明确最小验证集合和是否需要重新启动受影响服务，再开始编辑。

首次进度说明必须明确：已阅读两份准则；本次工作是否可能触及 Live 账户、下单、监控、WatchCode 或定时任务。如果任一准则文件缺失、不可读或相互冲突，停止会改变状态的操作，先报告并修复规则入口。

## 2. 授权边界

未经用户对本次操作的明确授权，禁止：

- 提交、替换、修改或取消任何真实券商订单，包括 Paper 和 Live 测试单。
- 启动可能自动下单的监控入口，或把只提醒链路改成自动交易链路。
- 运行 `tools/run_test_order.py`、OpenClaw 买卖命令或任何可到达 `submit_order` 的“连接测试”。
- 新建、删除、启用、禁用或立即运行 Windows 定时任务。
- 删除/覆盖 WatchCode、订单 CSV、排除记录、运行日志、状态文件、SQLite 数据或备份。
- 执行数据修复、批量回填、全量 WatchCode 生成或大规模回测。
- 扩大服务监听范围、开放公网/LAN 写接口、降低同源/请求头/路径校验或放宽 CORS。
- 向 webhook、Telegram、OpenClaw、Hermes 或其他外部目标发送测试消息。
- 输出、复制、提交或记录 `.env` 密钥、账户详情、webhook secret、通知目标和完整券商对象。
- 终止不确定归属的 Python、PowerShell、PyCharm、浏览器或系统进程。
- 重启本机。

只读源码分析、读取非敏感运行状态、查看进程命令行、读取账户模式/市场日历/订单状态，以及使用 Fake/DryRun broker 的隔离测试通常不需要额外授权，但仍不得泄露敏感信息或改变外部状态。

## 3. 项目架构与目录职责

本项目是本地运行的 Python 自动交易、观察池生成、交易复盘和网页看板系统，不是纯前端项目，也没有传统云端应用服务器、用户账户系统或支付模块。

| 路径 | 主要职责 | 修改注意点 |
| --- | --- | --- |
| `alpaca_ma5_service/` | 核心交易、行情、策略、运行时、复盘和 Web API | 任何交易/日期/账本改动均为高风险 |
| `alpaca_ma5_service/strategy_framework/components/` | 按 WatchCode、买入、卖出、自动撤单分开的策略实现 | 一个组件只负责一个阶段，不得绕过统一安全层 |
| `alpaca_ma5_service/strategy_framework/` | profile、契约、注册表、运行时解析和扩展入口 | 新实现必须显式注册，完整组合必须在任何外部 I/O 前解析成功 |
| `alpaca_ma5_service/workflows/` | 监控、WatchCode 和复盘的运行编排 | 内部模块不得反向依赖根目录入口 |
| `backtest/runners/` | 根目录回测命令背后的参数解析与编排 | 不承载策略算法或数据库实现 |
| `backtest/reporting/` | 可复用的交互式回测报告文档模型、模板和静态资源 | 不依赖具体策略或账户；引擎只负责把结果适配成报告文档 |
| `data/watchcodes/` | 盘前、盘中、盘后三个观察池运行文件 | 可能是用户当日结果，不得擅自覆盖 |
| `web/review_dashboard/` | 无构建步骤的静态 HTML/CSS/JS 看板 | 必须保持后端 API 契约和安全请求头 |
| `backtest/` | 历史数据缓存、修复、校验和回测引擎 | 默认数据源与线上实时链路不同，禁止静默混用 |
| `tests/` | Python `unittest` 回归测试 | 新增规则或修复必须覆盖对应风险面 |
| `tools/` | 自检、任务安装、健康检查、真实测试单、图表服务等运维入口 | 文件名不能代表安全级别，执行前读完整调用链 |
| `outputs/` | 订单、排除记录、日志、运行时心跳、图表等运行时产物 | 通常被忽略；不得把旧数据当源码回滚 |
| `docs/` | 长期运行手册和补充文档 | 架构或运行方式变化时同步维护 |
| 根目录 `monitor_*.py` | 盘前、盘中、盘后和统一监控的薄启动入口 | 可启动长期任务或真实交易，必须只委托对应 workflow |
| 根目录 `watchcode_*.py` | 不同阶段观察池的薄启动入口 | 必须只委托对应 workflow |
| 根目录 `run_backtest*.py` | 回测和历史数据任务的薄启动入口 | 必须只委托 `backtest/runners/` |
| `.env` / `.env.example` | 本机密钥与非敏感配置样例 | `.env` 永不提交；新增变量同步样例和文档 |

默认解释器是 `.venv\Scripts\python.exe`。

## 4. 核心业务流程

总流程和用户入口见 [`docs/architecture/PROJECT_FLOW.md`](docs/architecture/PROJECT_FLOW.md)。策略代码导航见 [`alpaca_ma5_service/strategy_framework/README.md`](alpaca_ma5_service/strategy_framework/README.md)。

### 4.1 统一监控阶段

`monitor_auto.py` 按美东时间和交易日切换阶段：

1. 盘前：确认/生成 `data/watchcodes/watch_codes_premarket.txt`，运行盘前推荐监控；该链路只提醒，不买入。
2. 盘中：确认/生成 `data/watchcodes/watch_codes.txt`，启动 `monitor_ma5_forever.py`；这是主要自动下单链路。
3. 盘后：确认/生成 `data/watchcodes/watch_code_afterhours.txt`，运行盘后监控并生成日报。
4. 非交易日或阶段结束：按当前入口规则退出或仅生成报告，不得因为是工作日就绕过交易所日历。

修改阶段切换时必须同时验证：节假日、半日市、跨日、夏令时、当日日线是否完成，以及生成任务已经存在时的等待行为。

### 4.2 WatchCode 生成与消费

- `data/watchcodes/watch_codes.txt`：盘中自动交易观察池。
- `data/watchcodes/watch_codes_premarket.txt`：盘前推荐观察池，不得直接变成买入池。
- `data/watchcodes/watch_code_afterhours.txt`：盘后观察池，是否可下单取决于实际入口调用链。
- `watchcode_ma5.py` 默认使用的 `ma5_dip` 筛选要求信号日收盘价比包含该日收盘价计算的当日 `MA5` 至少高 15 个点（`close/MA5 >= 1.15`），但不要求 `MA5 > MA10 > MA20`；MA5/MA10/MA20 仍作为诊断字段保留。实际 WatchCode 策略必须来自与盘中监控相同的 `build_monitor_settings()`。
- `signal_date` 必须来自最近一个已完成日线的交易日；交易日统一使用 `alpaca_ma5_service/trading_calendar.py`。
- 当天日线尚未完成时使用前一完成交易日；日线完成后才允许使用当天。
- 启动监控前如文件缺失或过期，先生成；如盘前或盘中任一生成器已在运行，等待现有任务完成，不得重复生成。
- 网页、PyCharm、直接 Python 和 Windows 定时任务都可能是任务来源，进程发现不能只看网页进程内登记状态。

### 4.3 盘中交易循环

核心链路为：

`monitor_ma5_forever.py`（薄入口）→ `workflows/monitoring/intraday.py` → `build_monitor_settings()` → `strategy_framework.resolve_strategy_runtime()` → `service.run_forever()` → `service.run_once()`。`run_once()` 的逐股核心循环必须直接按 `check_buy → execute_buy → notify_buy → check_sell → execute_sell → notify_sell → check_cancel → execute_cancel → notify_cancel` 展开，不得再增加掩盖主流程的 `process_*` 编排包装。真实买卖仍由 `AlpacaStockBroker` 提交；默认 Broker 内部等待订单终态并调用已选择的自动撤单策略，服务层撤单阶段只兜底处理自定义 Broker 直接返回的开放订单；最终进入本地订单记录与通知。

必须保持的边界：

- 买入候选只来自当日有效的盘中 WatchCode；卖出风控会检查券商全部持仓，包括用户手动买入的股票。
- WatchCode、买入、卖出和自动撤单是四个独立命名空间；基础 profile 给出完整默认组合，各分类允许显式覆盖。配置必须在任何行情、账户或订单 I/O 前完成全量解析，未知名称和缺失接口一律失败关闭。
- 旧 `strategy_name` 继续作为 profile 名兼容；实时主链路不得再通过进程全局可变策略名切换，旧上下文切换只保留给兼容回测调用。
- 真实买入仅允许在交易日 `09:30 <= t < 12:00 ET`。
- 实时订单总窗口与买入窗口不是同一概念；不得因盘前/盘后可报价就放宽买入窗口。
- 买入使用最终策略买点的限价单，不得为了提高成交率静默改成追价或市价单。
- 未完成订单、每日买入上限、重复订单、连续拒单/错误、止损和排除记录都是安全保护，不得为修复 UI 或提高速度而绕过。
- 买入或卖出订单一旦已提交，即使终态等待、自动撤单、本地记录或通知随后异常，也必须保留原始 `order_id` 和未确认暴露并失败关闭；不得把“后处理失败”误当成“没有订单”继续交易。
- 每次真实订单提交必须携带唯一 `client_order_id`；提交请求发生超时或网络异常时，必须先按该标识向券商恢复订单。无法确认“已接受”或“明确拒绝”时按 `SUBMIT_UNCONFIRMED` 失败关闭并暂停后续自动买入，禁止直接重试造成重复订单。
- 部分成交订单在剩余数量确认终态前仍属于未确认暴露；同一 `order_id` 的部分成交、撤单请求和最终撤单记录只能占用一次当日买入名额。
- `DONE_FOR_DAY` 和 `REPLACED` 不是可直接解除暴露的最终状态；替换订单必须沿 `replaced_by` 追踪到当前订单，再等待或撤销当前订单。
- 卖出前必须查询券商开放卖单；同一股票已有开放卖单，或查询失败无法确认时，必须跳过新的自动卖出。
- 服务层撤单结果只有在 `order_id`、方向和股票与原订单一致时才能替换原暴露；无法确认归属的撤单错误不得解除交易暂停。
- 手动持仓属于正常业务输入；不得自动篡改本地记录强行与券商对齐，也不得把“本地与券商不一致”一律视为错误。

### 4.4 盘后入口差异

当前盘后存在不同行为的入口：

- `monitor_afterhours.py` / `afterhours_monitor.run_afterhours_high_low_buyer` 当前是提醒链路，不提交订单。
- `afterhours_high_low.run_afterhours_high_low_strategy` 在 `dry_run=False` 时可提交盘后买单。

修改盘后代码必须逐层追踪实际调用方、broker 实例和参数传播，并分别测试提醒、DryRun、Paper/Live 阻断。不得把某个入口的安全结论套用到另一个入口，也不得根据陈旧常量判断实际行为。

### 4.5 复盘数据流

`review_data.build_daily_review()` 按用户选择的准确日期合并：

- 当日本地监控日志、订单 CSV、买入排除记录和盘后候选；
- 对应信号日的盘中/盘前候选；
- 可选的 Alpaca 只读订单和当前持仓快照。

复盘页面必须只展示所选日期。无交易、休市、停牌或无日志时可以为空，不得自动退回前一个有数据日期。

券商已成交但本地账本缺失的订单应保留“可能为手动交易”的解释路径。净资金流水是卖出流入减买入流出，不等于当日收益、已实现盈亏或账户权益变化；任何页面文字、颜色和汇总都不得混淆这些口径。

### 4.6 回测流程

回测链路读取本地 SQLite 日线/分钟线缓存，通过 Massive/Polygon、Yahoo、Alpaca 历史接口或其他明确数据源同步、修复和抽查，再由 `backtest/engine.py` 运行策略。通用交互式报告由 `backtest/reporting/` 渲染；引擎把策略结果、交易、权益曲线和股票证据适配成 `InteractiveReportDocument`，模板层不得反向导入具体策略或账户实现。分钟 K 证据应按股票拆分并按需加载；成交标记必须使用实际成交时间与成交价，缺失时明确标记且禁止吸附到相邻 K 线或回退到其他日期。线上实时行情默认可能来自 Moomoo，不能假定线上和回测数据源天然等价。

更改回测数据源、复权、时区、日期边界或缓存主键时，必须记录数据来源，验证重复写入的幂等性，并先备份数据库。回测结果不得直接证明明日一定会成交或盈利。

## 5. 主要模块及职责

| 模块 | 职责 | 关键约束 |
| --- | --- | --- |
| `config.py`, `envfile.py` | 读取 `.env`、构建运行设置 | 保持默认值、入口覆盖和环境变量优先级清晰 |
| `trading_calendar.py`, `market_time.py` | 交易日、阶段和下单窗口 | 所有入口复用，禁止 `weekday() < 5` 替代 |
| `service.py` | 单轮/循环交易编排 | 保护顺序、买卖边界和账本写入不可绕过 |
| `broker.py`, `alpaca_connection.py` | Alpaca 账户、订单、持仓适配 | 提交/撤单是外部写操作，测试必须隔离 |
| `market_data.py`, `moomoo_market_data.py` | 历史/实时行情 | 行情故障不能通过放宽交易保护兜底 |
| `strategy_framework/components/*` | WatchCode、买入、卖出、自动撤单的内置实现 | 各阶段职责独立；安全窗口和订单防重不得下沉为可绕过组件 |
| `strategy_framework/*` | 四类策略契约、独立注册表、profile 组合、运行时解析和扩展注册 | 禁止按配置任意导入模块；注册失败不得留下半初始化全局状态 |
| `strategy*.py`, `final_strategy.py` | 内置买卖信号和旧调用兼容层 | 修改需固定样例、边界值和回归测试 |
| `watchlist*.py`, `premarket_watchlist.py` | 观察池生成、读取和图表 | 日期、目标文件和阶段语义必须一致 |
| `order_guard.py`, `state.py` | 订单防重、错误保护和本地状态 | 不得用空值/异常默认放行 |
| `monitor_runtime.py`, `run_lock.py` | 心跳、日志镜像、任务发现和互斥 | 遥测失败不能终止交易；互斥不能误杀进程 |
| `dashboard_actions.py` | 看板任务启动/停止和子进程登记 | 只允许白名单动作，停止仅限本项目进程树 |
| `review_data.py`, `review_web.py` | 复盘聚合、静态页面和 HTTP API | 日期精确、券商读取可选、密钥不下发浏览器 |
| `trade_notifications.py`, `openclaw_notify.py` | 异步通知 | 通知失败不得改变订单结果或阻塞主循环 |
| `openclaw_trade_control.py`, `manual_order.py` | 手动自然语言/命令交易 | 可跳过自动时间和策略过滤，属于最高风险入口 |
| `daily_report.py` | 日报生成 | 指标名称必须对应真实财务口径 |
| `backtest/*` | 历史缓存、修复、校验和回测 | 与实时链路分离，数据修复默认备份 |
| `backtest/reporting/*` | 通用交互报告模型、HTML 模板、CSS 和 JavaScript | 主报告保持可直接打开；大体量分钟数据允许按股票拆分并按需加载；保持数据安全嵌入、键盘访问和窄屏可用 |

## 6. 前后端接口关系

前端是 `web/review_dashboard/` 下的静态文件，由 `review_web.py` 的 `ThreadingHTTPServer` 提供，无 Node/npm 构建步骤。浏览器不得直接接触券商密钥。

### 6.1 读取接口

| 方法与路径 | 用途 | 参数/约束 |
| --- | --- | --- |
| `GET /api/review/health` | 看板健康检查 | 无副作用 |
| `GET /api/review/dates` | 可选日期 | 不得替用户自动切换日期 |
| `GET /api/review` | 某日复盘 | `date`；`broker=1` 时后端执行券商只读查询 |
| `GET /api/review/evidence` | 日志证据 | 日期、来源、行号必须白名单和边界校验 |
| `GET /api/runtime/tasks` | 本机运行任务和日志 | 不得泄露命令行密钥或无关进程 |
| `GET /api/actions/status` | 任务控制状态 | 状态需融合网页登记和外部进程发现 |

### 6.2 写操作接口

当前前端可调用的 POST 动作包括生成盘中/盘前 WatchCode、启动盘中/盘前监控和停止监控。修改接口时必须保留：

- 仅接受明确白名单路径和空请求体；未知动作返回错误。
- 请求来自回环地址，并携带 `X-MA5-Action: 1`；浏览器提供 `Sec-Fetch-Site` 时必须为同源。
- Host、query、method 和静态文件路径均需严格校验。
- `start-monitor` 的确认文案必须说明可能真实下单；盘前监控必须说明只提醒。
- “停止监控”只停止本项目任务，不撤销券商已存在订单，界面必须明确这一点。
- 保留 CSP、`nosniff`、禁止嵌入、referrer/permissions/CORP 等安全响应头。
- 前后端 API 路径、字段或状态枚举变化必须在同一次修改中更新两端和测试。

### 6.3 LAN 图表服务的已知安全边界

`tools/serve_watchlist_charts_lan.py` 可监听 `0.0.0.0:8766`。当前服务带有允许删除观察池代码的 `POST /api/watchlist/delete`，且使用宽松 CORS；它不是纯只读图表服务。这是现状说明，不代表安全认可。

未经用户明确授权不得扩大暴露范围或增加新的写操作。触及该服务时必须验证：受信任网络边界、来源/认证、CORS、路径穿越、删除目标白名单、并发写文件和失败恢复。禁止将其直接暴露到公网。

## 7. 数据存储、数据库与数据流

### 7.1 运行时文件

| 数据 | 路径/模式 | 生产者与消费者 |
| --- | --- | --- |
| 模拟/本地状态 | `outputs/state.json` | broker/state 层读写 |
| 订单记录 | `outputs/orders_YYYY-MM-DD.csv` | 交易服务写入，复盘读取 |
| 买入排除 | `outputs/buy_exclusions_YYYY-MM-DD.csv` | 风控写入，复盘读取 |
| 盘中/盘前/盘后候选 | `outputs/*candidates*.csv` | WatchCode 生成器写入，复盘按信号日/复盘日读取 |
| 统一监控日志 | `outputs/logs/monitor_auto_YYYYMMDD.*.log` | 监控写入，看板读取 |
| 任务心跳和日志 | `outputs/monitor_runtime/<task-id>.json/.log` | 运行时登记，看板轮询 |
| 图表 | `outputs/watchlist_charts/*.html` | 生成器写入，Web/图表服务读取 |
| 观察池 | `data/watchcodes/watch_codes*.txt`、`data/watchcodes/watch_code_afterhours.txt` | 生成器写入，监控读取 |

运行时 JSON 状态采用临时文件加原子替换。修改时必须保持崩溃恢复和部分写入保护；心跳/日志镜像错误应降级记录，不能终止交易主循环。

### 7.2 SQLite 缓存

`backtest/data/market_data.sqlite` 是回测缓存，启用 WAL，通常不提交。主要表：

- `daily_bars`：以 `(symbol, bar_date, feed, adjustment)` 为主键，保存 OHLC、成交量、VWAP、交易数、时间戳和 MA5/10/20。
- `minute_bars`：以 `(symbol, timestamp_utc, feed, adjustment)` 为复合主键，保存分钟 OHLC。
- `fetch_ranges`：记录不同 kind/symbol/feed/adjustment 的已抓取区间，避免无边界重复抓取。
- `daily_dataset_metadata`、`security_master`：记录两年全普通股日线数据集的口径、构建进度和候选证券。

Schema、主键、复权或时区变更必须提供迁移/兼容方案，先备份再运行，验证 WAL、索引、upsert 幂等性和旧数据库读取。不得把数据修复脚本作为普通单元测试执行。

两年全普通股日线库通过 `run_backtest_daily_history_rebuild.py` 重建。口径固定为 Alpaca SIP、`1Day`、`split` 复权，并用 Alpaca 日历和统一离线日历交叉校验交易日。重建先写 `market_data.sqlite.rebuild`；完整校验 `daily_bars`、覆盖标记、OHLC、日期范围、空分钟表和 `PRAGMA quick_check` 后，先备份已有正式库，再原子替换。

任何新增或修改的日线回测入口，都必须优先只读 `backtest/data/market_data.sqlite`，并复用 `backtest.paths.OFFICIAL_DAILY_DB_PATH`，不得另行硬编码正式库路径。正式库覆盖不足时必须明确失败或报告缺口；不得静默改用其他日线库，也不得把分钟线写入正式库。分钟行情必须使用独立缓存。

证券池由当前 Alpaca active/inactive US equity 快照和当前 Nasdaq Trader ETF/Test Issue 标记共同分类。分类先排除 ETF、基金、优先股、权证、SPAC、非经营性 Trust 和结构化证券，再纳入 NYSE/NASDAQ/AMEX 上其余上市股；不得要求证券名称必须显式包含 `Common Stock`，REIT 和 Property Trust 必须保留。它尽量包含区间内已退市候选，但不是权威的历史时点证券主表；manifest 必须保留 `survivorship_bias_fully_eliminated=false`，不得把该口径描述成已彻底消除幸存者偏差。

## 8. 配置文件与环境变量

配置来源优先级必须保持可追踪：具体运行入口中的显式覆盖 → `build_settings()`/配置对象 → `.env` → 代码默认值。修改默认值时要检查根目录各监控入口是否再次覆盖。

主要变量分组：

| 分组 | 变量 |
| --- | --- |
| Alpaca | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` |
| 实时行情 | `REALTIME_PRICE_SOURCE`, `MOOMOO_HOST`, `MOOMOO_PORT`, `MOOMOO_SECURITY_FIRM`, `MOOMOO_CONNECT_TIMEOUT`, `MOOMOO_OPEND_EXE_PATH`, `MOOMOO_OPEND_STARTUP_TIMEOUT` |
| 轮询 | `REGULAR_POLL_SECONDS`, `IDLE_POLL_SECONDS` |
| 通知 | `TRADE_NOTIFY_OPENCLAW_ENABLED`, `TRADE_NOTIFY_MODE`, `CLOUD_NOTIFY_WEBHOOK_URL`, `CLOUD_NOTIFY_WEBHOOK_SECRET`, `OPENCLAW_TELEGRAM_TARGET`, `OPENCLAW_GATEWAY_PORT` |
| 通知兼容别名 | `WEBHOOK_URL`, `WEBHOOK_SECRET`, `WATCHLIST_TELEGRAM_TARGET` |
| 图表服务 | `WATCHLIST_CHART_LAN_HOST`, `WATCHLIST_CHART_LAN_PORT` |
| 历史数据 | `MASSIVE_API_KEYS` 或 `POLYGON_API_KEYS` |
| 策略组合 | `STRATEGY_PROFILE`, `WATCHLIST_STRATEGY`, `BUY_STRATEGY`, `SELL_STRATEGY`, `CANCEL_STRATEGY` |

当前 `.env.example` 只列出常用变量，不是全部运行配置的唯一来源。新增/重命名变量时必须：更新 `.env.example`（仅占位符）、更新本文件或运行手册、保留必要兼容别名或给出迁移错误、增加缺失值/非法值测试。禁止把真实值写进样例、日志或测试。

策略配置的优先级为：workflow 传入的分类覆盖 → workflow 传入的 profile/兼容 `strategy_name` → 对应环境变量 → `ma5_dip` 默认组合。`alpaca_ma5_service/workflows/monitoring/intraday.py` 顶部配置区是直接点击运行、PyCharm 和统一监控链路的权威配置；根目录 `monitor_ma5_forever.py` 只负责启动。新增策略、组合和依赖方向见 [`docs/architecture/STRATEGY_FRAMEWORK.md`](docs/architecture/STRATEGY_FRAMEWORK.md)。

## 9. 第三方服务和外部依赖

- Alpaca Trading API：账户、持仓、订单和真实/Paper 交易。
- Alpaca Historical Data：历史行情读取。
- Moomoo OpenD：默认实时行情来源之一，可由代码启动本地 OpenD 进程。
- Massive/Polygon、Yahoo：回测/历史数据来源；切换或 fallback 必须显式记录。
- OpenClaw/Hermes/Telegram 和签名 webhook：交易/WatchCode 通知。
- Plotly CDN：生成的部分 HTML 报告依赖。
- Python 包：`alpaca-py`、`moomoo-api==10.5.6508`、`pandas`、`tzdata`、`urllib3<2`。

外部依赖失败应有清晰、可观测的降级，不得以放宽交易保护、伪造成功状态或悄悄更换数据口径作为 fallback。依赖版本变更需验证 API 兼容、时区/数值类型和订单模型。

## 10. 定时、后台和异步流程

### 10.1 Windows 定时任务

当前主要任务语义：

- `AlpacaMA5-2200-GenerateWatchcode-PyCharm`：22:00 生成/检查下一交易日 WatchCode。
- `AlpacaMA5-0050-EnsureMonitor-PyCharm`：00:50 确认当日监控。
- `AlpacaMA5-0400-HealthCheck-PyCharm`：04:00 健康检查。

任务创建/更新脚本必须复用 `tools/check_ma5_trading_day.py` 和交易日模块，覆盖周末、节假日、跨年和夏令时。修改计划任务后需检查命令、工作目录、解释器、触发时间、历史结果和当日日志；历史 `LastTaskResult` 不能作为唯一成功证据。

### 10.2 后台任务

- `monitor_runtime.py` 使用心跳线程和 JSON/日志镜像向看板暴露状态。
- `dashboard_actions.py` 通过子进程启动生成器/监控，登记 PID，并发现 IDE/直接 Python 启动的外部任务。
- WatchCode 生成可能等待外部生成器完成，不能用固定短超时误判并重复启动。
- `trade_notifications.py` 异步发送通知；失败必须 fail-open，不改变交易结果。
- Moomoo/OpenClaw 可启动辅助子进程；终止前必须核对命令行和父子关系。
- review Web 和图表服务均使用多线程 HTTP 服务；共享文件写入要考虑并发。
- `run_lock.py` 和运行时发现共同防止重复任务，不能只依赖内存状态。

修改轮询和实时日志时，优先使用事件/心跳状态和短时快速轮询再退避，而不是无上限高频读取；需测试任务刚启动、运行中、退出、崩溃、外部启动和陈旧心跳六种状态。

## 11. 高风险功能专项规则

### 11.1 订单与账户

- 自动交易和手动命令最终都可能实例化真实 `AlpacaStockBroker`。
- `openclaw_trade_control.execute_trade_command` 和相关手动入口可使用 `skip_time_validation=True`，绕过自动监控的时间/MA5 过滤，因此属于最高风险。
- 新增订单入口必须集中经过 broker、显式标注 Paper/Live、可审计，并默认拒绝不完整参数。
- 任何异常不得默认“允许下单”；失败应阻断该次交易并记录非敏感原因。
- 单元测试必须使用 fake broker/patch，断言没有真实网络提交。

### 11.2 交易日期与时间

- 所有交易日判断复用 `trading_calendar.py`。
- 所有阶段和订单窗口复用 `market_time.py` 或其公共函数。
- 日期必须明确 ET/UTC/本地时区，不得混用 naive datetime。
- 测试至少覆盖交易日、周末、标准节假日、半日市、窗口前一刻、边界时刻和窗口后一刻。

### 11.3 复盘与财务口径

- 页面日期精确匹配用户选择；不回退到前一有数据日。
- 订单、成交、持仓快照、净流水、已实现盈亏、未实现盈亏和权益变化必须分别命名。
- 手动券商交易是允许的业务场景，不自动纠正为本地机器人交易。
- 当前持仓快照不是历史收盘持仓；页面必须说明时点。

### 11.4 进程和任务控制

- 停止操作只能作用于命令行明确属于本仓库且任务名/PID 可核验的进程树。
- 禁止按可执行文件名批量杀死所有 Python、PowerShell 或 IDE 进程。
- PID 可能复用；陈旧 runtime 文件不能单独证明进程归属。
- 停止监控不会自动撤销券商挂单，除非用户另行明确授权撤单。

### 11.5 Web 与网络安全

- 复盘控制接口默认仅回环访问，并保留同源、确认头、Host、method、query 和路径校验。
- 静态文件/证据/图表读取必须限制在允许目录和文件名集合内，防止路径穿越。
- 日志 API 必须屏蔽密钥、账户详情和无关进程命令行。
- 任何 LAN/公网监听变化都属于安全架构变更，需要用户授权和专项测试。
- 本项目没有支付模块和多用户认证系统；不要因此把本机控制接口误认为天然安全。

## 12. 实施代码修改的通用规则

1. 先定位真实入口和调用链，再修改最小必要表面；避免顺手重构无关模块。
2. 根目录 Python 文件只允许作为公开运行入口，不放业务函数、类或数据；实现进入 `alpaca_ma5_service/workflows/` 或 `backtest/runners/`。
3. 保留用户已有改动和运行时产物；不使用 `git reset --hard`、`git checkout --` 或等价破坏性操作。
4. 业务规则只保留一个权威实现。日期、时间窗、WatchCode 就绪、订单保护和指标口径禁止在 UI/脚本中复制简化版。
5. 不以吞掉异常、无限等待、扩大超时或强制重试掩盖根因；重试必须有上限、幂等和可观察状态。
6. 跨线程/跨进程共享文件采用原子写、锁或现有运行时协议，避免半文件和并发覆盖。
7. 前后端字段变化采用兼容迁移；不在同一版本让旧前端得到无解释的 404/503。
8. 注释解释“为什么有此安全边界”，避免重复代码表面行为；删除保护前必须证明替代机制。
9. 任何可影响交易结果的默认值变化，都要在变更报告中单独列出。
10. 不把回测盈利、单次模拟或当前 key 存在视为真实交易一定会成交/盈利的证明。
11. 文档与代码不一致时，以经过调用链和测试确认的实际行为为依据，并在同次修改修正文档。

## 13. 测试、构建和验证矩阵

本项目使用 Python `unittest`；前端为静态资源，没有 Node 打包步骤。默认测试命令：

```powershell
.\.venv\Scripts\python.exe -c "import sys, unittest; suite=unittest.defaultTestLoader.discover('tests', pattern='test_*.py'); result=unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(0 if result.wasSuccessful() else 1)"
```

也可使用 `tools/run_self_tests.py`。测试和其他重型工作必须遵守本机全局串行限制。

| 修改区域 | 最低验证要求 |
| --- | --- |
| 纯文档 | 链接存在、Markdown 内容检查、`git diff --check`；无需重启服务 |
| 策略/订单/broker | 目标单测 + 全量交易相关测试；fake broker 断言下单参数、阻断条件和零真实网络调用 |
| 日历/时间窗/定时任务 | 交易日、周末、节假日、半日市、边界时间测试；PowerShell AST 解析；核对任务命令但未经授权不注册/运行 |
| WatchCode | 信号日、文件目标、空候选、过期、并发已有任务、原子写和异常恢复测试 |
| 复盘数据 | 精确日期、空日、手动交易、本地/券商合并、净流水与 P&L 分离测试 |
| Web API | `test_review_web.py` / `test_dashboard_actions.py`；GET/POST/405/404、同源/确认头、路径穿越、进程归属 |
| 前端 | 目标日期、空态、运行态切换、按钮确认/结束、日志快速出现、隐藏页退避；实际浏览器或 HTTP 联调 |
| 运行时/并发 | 外部 Python/IDE 启动、父子进程、陈旧 PID、心跳中断、任务退出和重复启动测试 |
| SQLite/回测 | 临时数据库 schema/upsert/覆盖范围测试；报告改动运行 `tests.test_backtest_reporting` 和对应引擎报告测试，并用真实浏览器检查桌面/窄屏；真实修复前备份，未经授权不运行批量修复 |
| 通知 | patch 外发；验证异常不影响交易主流程，未经授权不发送真实测试消息 |

若相关测试因环境或外部服务无法运行，变更报告必须写明未验证项和具体原因，不得以“应该可以”代替证据。

## 14. 运行、部署与重启

- 本项目主要部署形态是本地 Python 进程、PyCharm 入口和 Windows Task Scheduler，不存在独立前端编译产物。
- 修改运行逻辑后，按影响范围重启对应看板/图表/监控服务并验证健康状态；禁止重启本机。
- 重启前核对进程命令行、工作目录和父子关系，只停止本项目目标进程。
- 修改看板后至少验证 `/api/review/health`、`/api/actions/status` 及相关页面交互。
- 修改监控后优先用 Fake/DryRun 和隔离输入验证；启动真实监控需要用户明确授权。
- 修改定时任务脚本后只做静态解析和参数验证，除非用户明确授权更新系统任务。
- 文档、注释或测试数据的修改无需重启正在运行的交易服务。

## 15. 文档同步规则

以下变化必须在同一次修改中同步更新本文件、`docs/PROJECT_OPERATIONS.md` 和必要的 README：

- 新增/删除运行入口、模块、Web API、数据文件或数据库表。
- 交易窗口、交易日规则、策略语义、WatchCode 日期或任务并发规则变化。
- 环境变量、依赖、解释器、端口、计划任务名称/时间或部署方式变化。
- Paper/Live、订单、通知、网络暴露、认证/权限或数据删除风险变化。
- 测试命令、健康检查或交付验证方式变化。

架构事实变化时更新“最后一次架构核对”日期。仅修改文字表达但不改变行为时，不要伪造新的运行验证结论。

## 16. 明令禁止的修改方式

- 为了让测试通过而删除/放宽交易保护、日期限制、安全请求校验或进程归属检查。
- 以“今天没成交”为由直接增加下单概率、追价、买入窗口或资金规模。
- 在前端、日志或 API 返回中暴露密钥、完整账户对象或通知 secret。
- 在休市/停牌/无数据时偷偷展示上一交易日数据。
- 把净资金流水显示成收益或把当前持仓当成历史时点持仓。
- 用宽泛 `taskkill`、进程名匹配或全局 Python 清理解决任务状态问题。
- 未备份即修改/修复真实 SQLite 缓存，或未经授权删除运行时证据。
- 未确认调用链即把盘前/盘后提醒改为可下单。
- 通过禁用互斥、Hook、安全检查或另换等价命令绕过本机重型任务限制。
- 未经明确要求创建提交、推送、发布、外部消息或系统任务。

## 17. 每次交付的变更报告格式

完成修改后按以下顺序报告：

1. **结果**：一句话说明已完成什么，是否已在真实运行路径验证。
2. **变更文件**：逐个列出文件及职责变化。
3. **业务影响**：说明对交易、WatchCode、复盘、任务、Web 或数据口径的影响；无影响也要明确写出。
4. **安全边界**：说明是否触及 Paper/Live、订单、外部通知、定时任务、数据库或网络暴露。
5. **验证证据**：列出实际执行的测试、健康检查、浏览器/运行态结果。
6. **未执行项**：列出因无授权、环境阻塞或不适用而未执行的操作。
7. **剩余风险**：只列真实存在且尚未验证的风险，不写泛泛建议。

报告不得包含密钥、完整账户数据或敏感通知目标；不得把代码检查描述成已完成真实下单验证。

## 18. 日内动态涨幅榜历史研究模块

`intraday_top20/` 是独立历史研究和 Streamlit 报告模块，不属于 MA5 实盘推荐、WatchCode、监控或下单链路。该目录内不得新增账户读取、订单提交或复用实盘买入入口；历史回测验证不构成真实下单授权。

### 固定口径

- 股票池必须在每根已完成五分钟 K 线后，用当前收盘价和前一交易日收盘价动态重排；禁止使用日终 Top N 回填盘中股票池。
- 信号只能使用当前完成 K 线及之前数据；默认买入时间为信号 K 线结束时间，即下一根五分钟 K 线开始时间。
- 默认 20 分钟阈值必须解释为严格超过 20 分钟，因此至少 5 根完整五分钟 K 线收于均线下方。
- 停牌、缺下一根 K 线或缺 15:55 K 线不得产生理想化成交；必须拒单、保留未解决状态或等待下一根真实可用 K 线，并降低可信度。
- 合成数据结果必须在网页、导出报告和交付结论中明确标记，禁止描述为历史策略收益。

### 变更验证矩阵

- 修改排名、指标或状态机：运行 `test_ranking.py`、`test_strategy.py` 和 `test_no_lookahead.py`。
- 修改成交、仓位、成本或退出：运行 `test_execution.py` 和 `test_no_lookahead.py`。
- 修改数据清洗或加载：补充时区、正常时段、前收、拆股、缺失 K 线或证券过滤测试，并运行全部 `intraday_top20/tests`。
- 修改网页、图表或导出：先运行全部模块测试，再临时启动 Streamlit，验证首页、至少一个 Plotly 图和至少一个交易表；只终止本次明确启动的进程。
- 修改依赖、入口、数据字段、可信度门禁或部署方法：同次更新 `intraday_top20/README.md`、根 README 和 `docs/PROJECT_OPERATIONS.md`。

完整测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest intraday_top20\tests -q
```

## 19. 信号日强势 + 动态 MA5 历史回测

`backtest/signal_dynamic_ma5.py` 和 `run_backtest_signal_dynamic_ma5.py` 是独立历史研究入口，不属于实盘 MA5 监控、WatchCode、账户或订单链路。

- 日线筛选只能读取已完成日线。信号日固定要求 `MA5 > MA10 > MA20`、涨幅严格大于 `10%`、阳线实体严格大于 `10%`；买入日固定为全局下一交易日且开盘涨幅严格大于 0，等于 0 时不得进入分钟回放。
- 动态 MA5 固定使用前 4 个已完成交易日收盘价和当前已完成分钟收盘价；只有当前已完成分钟收盘价小于或等于动态 MA5，且相对买入日开盘价跌幅严格大于 `15%` 时才能触发，等于 `15%` 或全天未达到条件时不得买入。成交必须落在下一根真实分钟线开盘，该实际入场价相对买入日开盘价跌幅仍须严格大于 `15%`；反弹后不满足时继续等待窗口内后续有效触发。实际成交时间必须满足 `09:30 <= t < 12:00 ET`，12:00 ET 及以后禁止买入，禁止使用触发分钟收盘价回填成交。
- 分档止盈按原始仓位各卖 `1/3`；同一分钟止损优先；缺下一分钟或缺尾盘分钟线时不得伪造成交或价格。
- 正式日线库保持只读且不得写入分钟线。候选分钟线使用独立缓存、SIP、`split`；不得静默回退到其他 feed。
- 修改信号、动态均线、成交时序、退出或成本时，必须运行 `tests.test_signal_dynamic_ma5_backtest`，再运行全项目测试，并执行真实历史回测核对 JSON、CSV 和 HTML 三类产物。
- 历史回测不得读取账户或调用订单入口。零成本、固定名义本金、非组合容量约束和不能完全消除的幸存者偏差必须在报告和交付结论中明确披露。
