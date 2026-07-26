# Alpaca MA5 项目运行手册

本项目可连接 Alpaca Live/Paper。每次检查、修改、测试或部署前，必须完整阅读本文件和根目录 `CODE_MODIFICATION_RULES.md`，再执行 `git status --short`。

## 1. 不可越过的边界

- 未获得用户对本次真实下单的明确授权，只允许源码检查、只读账户/日历/持仓/订单查询和 Fake/DryRun 测试。
- 不得运行 `tools/run_test_order.py`，不得启动可能自动下单的监控作为普通验证。
- 永远不要重启电脑。
- 不得输出 `.env`、API key、完整账户信息、通知 secret 或目标。
- 不得批量结束所有 Python、PowerShell、PyCharm 或浏览器进程。
- 不得未经核对删除 WatchCode、订单记录、排除记录、运行日志、状态文件或正式 SQLite。
- 美股交易日必须复用 `alpaca_ma5_service/trading_calendar.py`。
- 真实盘中买入窗口固定为交易日 `09:30 <= t < 12:00 ET`。

## 2. 每次任务的启动顺序

1. 完整阅读两份准则。
2. 运行 `git status --short`，区分用户改动、运行产物和本次文件。
3. 确认 `.env` 实际连接 Paper 还是 Live；只输出模式和非敏感阻断状态。
4. 检查项目生成器、监控、复盘网页、计划任务和本机重型任务。
5. 沿真实入口确认副作用，先做最小只读诊断。
6. 修改后按风险矩阵测试；只重启受影响的项目服务。

若任一准则缺失、不可完整读取或互相冲突，停止会改变状态的操作。

## 3. 当前运行链路

### 全天入口

`monitor_auto.py` 按交易所日历和 ET 时段进入：

1. 盘前：生成/读取 `watch_codes_premarket.txt`，只发送推荐提醒；
2. 盘中：生成/读取 `watch_codes.txt`，运行 MA5 自动交易；
3. 盘后：生成/读取 `watch_code_afterhours.txt`，公开监控入口只提醒；
4. 休市或阶段结束：退出或生成报告，不得用工作日判断代替交易所日历。

### 盘中真实交易

```text
monitor_ma5_forever.py
  -> workflows/monitoring/intraday.py
  -> build_monitor_settings()
  -> resolve_strategy_runtime()
  -> service.run_forever()
  -> service.run_once()
```

`run_once()` 逐股执行买入、卖出、撤单的 check/execute/notify 阶段。真实写入集中在 `broker.py` 和 `order_guard.py`；策略组件只产生决策。

必须保持：

- WatchCode 候选只参与买入；卖出检查券商全部持仓；
- 四类策略在任何行情、账户或订单 I/O 前完整解析，未知名称失败关闭；
- BUY 使用策略最终买点的限价，不追价、不改市价；
- 开放订单、重复订单、每日名额、连续错误、止损和排除记录不能绕过；
- 订单提交后即使等待、撤单、账本或通知失败，也保留原 `order_id` 和暴露；
- 提交超时先按唯一 `client_order_id` 恢复；无法确认时暂停后续自动买入；
- 部分成交在剩余数量终态前仍是未确认暴露；
- `DONE_FOR_DAY`、`REPLACED` 不能直接当安全终态；
- 卖出前查询开放卖单，查询失败或已有卖单时不重复卖出。

## 4. 当前唯一策略

内置 profile、WatchCode 和买入策略只有 `ma5_dip`。权威参数位于 `alpaca_ma5_service/workflows/monitoring/intraday.py`：

- WatchCode：信号日 `close / MA5 >= 1.15`；
- 每股 `$2,500`、每日最多 3 只；
- 动态 MA5 分档买点，当前价相对信号日收盘至少下跌 12%；
- 止损触发 `-10%`，止损限价 `-8%`；
- `+10%` 卖出 50%，剩余仓保护默认关闭；
- 15:55–16:00 ET 清仓；
- 订单 600 秒超时后进入确认撤单。

任何可影响交易结果的默认值变化，都要单独说明、覆盖边界测试，并且不得自动启动 Live 监控。

## 5. WatchCode

- `data/watchcodes/watch_codes.txt`：盘中自动交易观察池；
- `data/watchcodes/watch_codes_premarket.txt`：盘前推荐池，不得提交买单；
- `data/watchcodes/watch_code_afterhours.txt`：盘后观察池。

文件顶部 `signal_date` 必须对应最近一个已完成日线的交易日。启动监控前必须确认当日文件；已有盘前/盘中生成器时等待其完成，不能重复启动。网页、PyCharm、命令行和计划任务都可能是任务来源。

WatchCode 是运行产物。除非用户明确要求，不得回滚、清空或用旧文件覆盖。

## 6. 复盘网页

- 入口：`open_daily_review.py` / `open_daily_review.cmd`
- 默认起始端口：`127.0.0.1:8788`
- 健康检查：`GET /api/review/health`
- 任务状态：`GET /api/actions/status`

用户选择哪一天就只展示哪一天；休市、停牌或无交易可以为空，不得回退日期。净资金流水不是收益，必须区分已实现盈亏、未实现盈亏、费用、现金流和权益变化。券商与本地账本不一致时优先保留“手动交易”解释，不自动改账。

POST 动作必须限制在回环地址、白名单、空请求体、同源和 `X-MA5-Action: 1`。停止监控只停止本项目进程，不自动撤销券商订单。

## 7. 通知

项目统一调用 `alpaca_ma5_service.openclaw_notify.safe_send_notification()`。通知失败不得改变交易结果或中断主循环；通知成功也不代表订单成功。未经授权不得发送真实测试消息或修改外部通知项目。

## 8. 进程与计划任务

结束进程前必须读取 `Win32_Process.CommandLine`，同时匹配本项目绝对路径和具体入口。陈旧 PID、runtime 文件或历史任务结果不能单独证明进程仍在运行。

主要任务语义：

- `AlpacaMA5-2200-GenerateWatchcode-PyCharm`：22:00 检查下一交易日并生成 WatchCode；
- `AlpacaMA5-0050-EnsureMonitor-PyCharm`：00:50 检查当天并确保监控；
- `AlpacaMA5-0400-HealthCheck-PyCharm`：04:00 检查监控和 WatchCode。

未经用户授权不得创建、删除、启停或立即运行计划任务。不要为了刷新历史返回码手动运行 00:50/04:00 任务。

## 9. 正式日线 SQLite

正式路径：`backtest/data/market_data.sqlite`。

- 来源固定 Alpaca SIP、`1Day`、`split`；
- 正式库只保留日线，`minute_bars` 必须为 0；
- 股票池包含可识别的 active/inactive 普通股，但不能宣称完全消除幸存者偏差；
- 新的日线研究只能优先只读此库，覆盖不足必须报告；
- 分钟行情不得写入正式库。

唯一保留的数据维护入口：

```powershell
.\.venv\Scripts\python.exe run_backtest_daily_history_rebuild.py `
  --start-date 2024-07-17 --end-date 2026-07-16
```

它先写 staging，校验元数据、OHLC、覆盖、空分钟表和 `PRAGMA quick_check`，再备份并原子替换正式库。重建属于重型任务，必须全局串行；它不读取持仓/订单、不下单、不启动监控。

`backtest/reporting/` 仅保留为通用历史报告组件；仓库不再保存旧策略、旧优化器、旧分钟缓存或旧报告。

## 10. 验证矩阵

### 通用

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

### 按风险补充

- 策略/订单：目标测试 + 全量测试；Fake broker 断言零真实提交；
- 日历/时间：交易日、周末、节假日、半日市和窗口边界；
- WatchCode：信号日、目标文件、空候选、过期、并发和原子写；
- 复盘/Web：精确日期、空日、GET/POST/404/405、同源、路径穿越和进程归属；
- SQLite：临时数据库、schema、只读、覆盖、幂等和 quick check；
- PowerShell：AST 解析；未经授权不注册或运行任务；
- 通知：patch 外发，验证异常不影响交易。

修改运行逻辑后，重启并检查受影响服务。监控修改只能先做 Fake/DryRun；启动真实监控仍需明确授权。验证报告必须明确是否提交过订单，默认答案为“没有”。

## 11. 文档同步

入口、交易窗口、策略语义、WatchCode、数据结构、环境变量、外部依赖、任务、Web API 或验证命令变化时，同次更新本文件、`CODE_MODIFICATION_RULES.md` 和必要 README。
