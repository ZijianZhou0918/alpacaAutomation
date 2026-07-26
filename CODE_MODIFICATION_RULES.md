# Alpaca MA5 代码修改准则

> 适用范围：本仓库及全部子目录
> 最后架构核对：2026-07-25
> 运行手册：`docs/PROJECT_OPERATIONS.md`

本项目连接真实券商。修改任何源码、脚本、测试、配置或前端前，必须完整阅读本文件和运行手册，不得只凭文件名、注释或历史记忆判断副作用。

## 1. 修改前门禁

依次完成：

1. 完整阅读两份准则；
2. 运行 `git status --short`，保护用户已有改动和运行产物；
3. 确认 `.env` 实际连接 Paper 还是 Live；
4. 判断是否涉及账户、订单、持仓、时间窗口、WatchCode、监控、计划任务、账本、SQLite、通知、网络或密钥；
5. 沿真实调用链确认入口、实现和副作用；
6. 检查现有生成器、监控、看板、计划任务和重型进程；
7. 明确验证集合和是否需要重启受影响服务。

首次进度说明必须注明两份准则已完整阅读，以及本次是否可能触及 Live、下单、监控、WatchCode 或计划任务。准则缺失、不可读或冲突时，停止写操作。

## 2. 授权边界

未经用户对本次操作明确授权，禁止：

- 提交、替换、修改或取消 Paper/Live 订单；
- 启动可能自动下单的监控或运行 `tools/run_test_order.py`；
- 创建、删除、启停或立即运行 Windows 计划任务；
- 删除/覆盖 WatchCode、订单 CSV、排除记录、运行日志、状态文件、正式 SQLite 或备份；
- 执行全量数据重建、修复、大规模回测或 WatchCode 生成；
- 发送真实通知测试；
- 扩大网络监听、放宽 CORS/同源/路径校验；
- 输出或记录密钥、完整账户对象和通知 secret；
- 终止归属不明的进程；
- 重启电脑。

源码只读检查、非敏感状态读取和 Fake/DryRun 隔离测试通常无需额外授权。

## 3. 核心架构

| 路径 | 职责 |
| --- | --- |
| `alpaca_ma5_service/` | 交易、行情、策略、风控、账本、复盘和 Web API |
| `alpaca_ma5_service/strategy_framework/` | WatchCode、买入、卖出、撤单契约与注册表 |
| `alpaca_ma5_service/workflows/` | 监控、WatchCode、复盘编排 |
| `backtest/` | 正式日线重建和通用 HTML 报告器 |
| `data/watchcodes/` | 三个时段的运行观察池 |
| `web/review_dashboard/` | 无构建步骤的复盘前端 |
| `outputs/` | 订单、排除、日志、心跳和图表等运行证据 |
| `tools/` | 自检、任务和受控运维入口 |
| `tests/` | Fake/DryRun 回归测试 |

根目录 `monitor_*.py`、`watchcode_*.py`、`open_daily_review.py` 和 `run_backtest_daily_history_rebuild.py` 只能是薄入口，业务实现必须在 package/workflow/runner 中。

## 4. 真实交易链路

```text
monitor_ma5_forever.py
  -> workflows/monitoring/intraday.py
  -> build_monitor_settings()
  -> resolve_strategy_runtime()
  -> service.run_forever()
  -> service.run_once()
  -> broker.py / order_guard.py
```

`run_once()` 必须清晰展开买入、卖出、撤单的 check/execute/notify 阶段。策略只产生信号；真实订单集中经过 broker 和订单保护层。

不可削弱的保护：

- 真实买入仅在交易日 `09:30 <= t < 12:00 ET`；
- 买入候选来自当日盘中 WatchCode；卖出检查券商全部持仓；
- 四类策略必须在外部 I/O 前完整解析，未知或缺失实现失败关闭；
- 使用最终策略买点的 BUY LIMIT，不追价、不静默改市价；
- 每日买入上限、开放/重复订单、连续错误、排除记录和止损不可绕过；
- 每次提交使用唯一 `client_order_id`；状态不明时恢复查询并暂停；
- 部分成交、替换订单和撤单必须跟踪到可证明的终态；
- 卖出前开放卖单查询失败时不得继续提交；
- 本地记录失败或通知失败不能被解释为“订单不存在”。

## 5. 当前策略权威配置

当前唯一内置组合是 `ma5_dip`。四类策略和运行参数的权威入口是 `alpaca_ma5_service/workflows/monitoring/intraday.py`；根目录入口不得重复常量。

策略配置优先级：workflow 显式分类覆盖 → workflow profile → 环境变量 → `ma5_dip` 默认。自定义实现只能在 `strategy_framework/extensions.py` 中显式可信注册；禁止按环境变量任意导入 Python 模块。

改变金额、买入阈值、止损、止盈、清仓时间、扩展时段或撤单超时，必须在交付中单独列出并完成对应边界测试。回测或单次模拟盈利不能作为放宽真实保护的依据。

## 6. WatchCode 与交易日期

- 交易日统一复用 `trading_calendar.py`，不得自己写 `weekday() < 5`；
- `signal_date` 对应最近一个已完成日线的交易日；
- 盘前池只用于推荐，不能直接成为买入池；
- 监控启动前确认当日 WatchCode；已有生成器时等待，不重复启动；
- 文件写入保持原子性，异常时不得把部分结果写成空池；
- 网页、PyCharm、命令行和计划任务都要纳入进程发现。

## 7. 订单、账本与财务口径

- 手动持仓/订单是合法输入，不自动篡改本地账本；
- 订单、成交、持仓快照、现金流、已实现/未实现盈亏和权益变化必须分别命名；
- 净资金流水不是当日收益；
- 当前持仓快照不是历史收盘持仓；
- 本地与券商不一致时保留可审计解释，不伪造对齐。

运行时状态使用临时文件和原子替换；遥测或通知失败不得中断交易主流程。

## 8. Web 与进程安全

复盘服务默认只绑定回环地址。读取和动作接口必须保留 Host、method、query、同源、确认头、空请求体、路径白名单、路径穿越和安全响应头检查。浏览器不得接触券商密钥。

停止操作只能作用于命令行明确属于本仓库、具体任务和 PID 可核验的进程树。禁止按进程名批量结束 Python/PowerShell/IDE；停止监控不会自动撤销券商挂单。

## 9. 正式日线数据库

正式库固定为 `backtest/data/market_data.sqlite`，通过 `backtest.paths.OFFICIAL_DAILY_DB_PATH` 引用。

- 口径为 Alpaca SIP、`1Day`、`split`；
- 正式库的 `minute_bars` 必须保持 0；
- 新日线研究优先只读正式库，覆盖不足必须失败或报告；
- 分钟数据必须使用独立缓存；
- schema、复权、主键或时区变化需要兼容方案和备份；
- 唯一保留的全量维护入口是 `run_backtest_daily_history_rebuild.py`；
- 重建先写 staging，完成完整校验和备份后才能原子替换。

旧策略、旧优化器、旧回测报告和旧分钟缓存不属于核心仓库。新的研究必须先得到明确需求，并与真实订单链路隔离。

## 10. 实施规则

1. 定位真实入口和调用链，只修改必要表面；
2. 保留用户已有改动，不使用 `git reset --hard`、`git checkout --`；
3. 日期、窗口、WatchCode、订单保护和指标口径只保留一个权威实现；
4. 不吞异常、不无限等待、不用无界重试掩盖根因；
5. 跨进程文件使用原子写、锁或现有运行协议；
6. 前后端契约同次更新并保持明确迁移；
7. 删除保护前必须证明替代机制；
8. 不用大规模格式化掩盖修改；
9. 不把构建、API 200 或模拟结果描述成真实交易验证；
10. 文档与代码不一致时，以调用链和测试确认的行为为准并同次修正文档。

## 11. 验证矩阵

通用命令：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

| 修改区域 | 最低验证 |
| --- | --- |
| 策略/订单/broker | 目标测试 + 全量测试；Fake broker、下单参数、阻断和零真实网络提交 |
| 日历/时间 | 交易日、周末、节假日、半日市和窗口边界 |
| WatchCode | 日期、目标文件、空候选、过期、并发、原子写和异常恢复 |
| 复盘数据 | 精确日期、空日、手动交易、账本/券商合并和 P&L 分离 |
| Web/API | GET/POST/404/405、同源、确认头、路径穿越、进程归属和健康检查 |
| SQLite | 临时库 schema、只读、覆盖、幂等、零分钟行和 quick check |
| 通知 | patch 外发；失败不影响交易；不发送真实测试消息 |
| PowerShell/任务 | AST 解析和参数核对；未经授权不注册、不运行 |

相关测试因环境无法运行时，报告具体未验证项和原因。修改运行逻辑后只重启受影响服务；真实监控仍需明确授权。

## 12. 文档与交付

新增/删除入口、模块、API、数据文件、交易窗口、策略语义、WatchCode、环境变量、外部依赖、任务或验证命令时，同次更新本文件、`docs/PROJECT_OPERATIONS.md` 和 README。

交付顺序：

1. 结果；
2. 变更文件和职责；
3. 交易/WatchCode/Web/数据影响；
4. Live、订单、通知、任务、SQLite 和网络安全边界；
5. 实际验证证据；
6. 未执行项；
7. 真实剩余风险。

不得包含密钥、完整账户数据或敏感通知目标，也不得把静态检查描述成真实下单验证。
