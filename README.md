# Alpaca MA5 自动交易

这是一个本地运行、可连接 Alpaca Live/Paper 的 MA5 自动交易项目。仓库只保留当前真实运行链路、必要运维工具、复盘网页、测试，以及正式日线数据库的重建能力。

> 默认验证必须使用只读查询或 Fake/DryRun。除非用户明确授权本次真实下单，否则不得启动会提交订单的入口，也不得运行 `tools/run_test_order.py`。

## 当前策略

当前唯一内置 profile、WatchCode 和买入策略是 `ma5_dip`：

- 信号日 `close / MA5 >= 1.15` 才进入盘中 WatchCode；
- 盘中依据动态 MA5、信号日涨幅分档、开盘保护和当日深度回撤计算最终 BUY LIMIT；
- 真实买入只允许美股交易日 `09:30 <= t < 12:00 ET`；
- 默认每股 `$2,500`，每日最多 3 只；
- 默认亏损 10% 触发全部止损，止损限价为成本下方 8%；
- 默认盈利 10% 卖出一半，剩余仓保护默认关闭；
- 15:55–16:00 ET 清仓剩余仓位；
- 默认 10 分钟未完成订单进入确认撤单流程。

权威可编辑配置位于 `alpaca_ma5_service/workflows/monitoring/intraday.py`。根目录入口不复制策略参数。

## 核心入口

| 目的 | 入口 | 副作用 |
| --- | --- | --- |
| 全天按阶段运行 | `monitor_auto.py` | 盘中可能真实下单 |
| 盘中 MA5 监控 | `monitor_ma5_forever.py` | 可能真实下单 |
| 盘前推荐 | `monitor_premarket_ma5.py` | 只提醒 |
| 盘后监控 | `monitor_afterhours.py` | 当前公开入口只提醒 |
| 生成盘中 WatchCode | `watchcode_ma5.py` | 写观察池和候选文件 |
| 生成盘前 WatchCode | `watchcode_premarket.py` | 写盘前观察池 |
| 生成盘后 WatchCode | `watchcode_afterhours.py` | 写盘后观察池 |
| 每日复盘网页 | `open_daily_review.cmd` | 本地只读网页；可显式启动/停止任务 |
| 重建正式日线库 | `run_backtest_daily_history_rebuild.py` | 重型历史数据任务，不下单 |

## 目录

```text
alpaca_ma5_service/       交易、行情、策略、风控、复盘与工作流
backtest/                 正式日线库重建和通用 HTML 报告器
data/watchcodes/          盘前、盘中、盘后观察池运行文件
web/review_dashboard/     无构建步骤的复盘前端
tools/                    自检、计划任务和受控运维入口
tests/                    Fake/DryRun 回归测试
docs/                     运行准则与核心架构
```

策略注册按 WatchCode、买入、卖出、撤单四类拆分，详见 `alpaca_ma5_service/strategy_framework/README.md`。完整流程见 `docs/architecture/PROJECT_FLOW.md` 和 `docs/architecture/TRADE_ORDER_FLOW.md`。

## 安装

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `.env` 填入本机凭据。不要把 `.env`、密钥、账户详情或通知 secret 写入日志、测试、提交或回复。

连接检查是只读操作：

```powershell
.\.venv\Scripts\python.exe tools\check_alpaca_connection.py
```

## WatchCode 与时间边界

- `data/watchcodes/watch_codes.txt`：盘中自动交易观察池；
- `data/watchcodes/watch_codes_premarket.txt`：盘前推荐专用，不得买入；
- `data/watchcodes/watch_code_afterhours.txt`：盘后观察池；是否能下单必须看具体调用链；
- `signal_date` 必须是最近一个已完成日线的交易日；
- 所有交易日判断复用 `alpaca_ma5_service/trading_calendar.py`；
- 启动监控前必须确认当日 WatchCode，已有生成任务时等待，不重复启动。

## 订单安全

盘中主链路：

```text
monitor_ma5_forever.py
  -> workflows/monitoring/intraday.py
  -> service.run_forever()
  -> service.run_once()
  -> check / execute / notify buy, sell, cancel
  -> broker.py / order_guard.py
```

必须保留：每日买入上限、重复订单保护、开放订单检查、唯一 `client_order_id`、提交状态不明时暂停、部分成交暴露、替换订单追踪和撤单终态确认。卖出风控会检查券商全部持仓，包括手动持仓；不得自动篡改本地账本强行对齐。

## 复盘网页

双击 `open_daily_review.cmd`，默认从 `http://127.0.0.1:8788/` 起寻找可用端口。

- 健康检查：`GET /api/review/health`
- 任务状态：`GET /api/actions/status`
- 选择哪一天就只展示哪一天；无数据时不得回退到上一交易日。
- 净资金流水不等于当日收益；已实现、未实现、现金流和权益变化必须分开。

网页写动作仅允许回环地址、白名单路径、空请求体和 `X-MA5-Action: 1`。停止本地监控不会自动撤销券商挂单。

## 正式日线数据库

正式库固定为 `backtest/data/market_data.sqlite`：Alpaca SIP、`1Day`、`split` 复权，正式库的 `minute_bars` 必须为 0。

```powershell
.\.venv\Scripts\python.exe run_backtest_daily_history_rebuild.py `
  --start-date 2024-07-17 --end-date 2026-07-16
```

重建先写 staging，校验完成后备份并原子替换。该任务只读取历史行情，不读取持仓/订单，也不下单；它是重型任务，不能与回测、构建或其他数据任务并发。

`backtest/reporting/` 是保留的通用 Interactive HTML 报告器，供以后经过明确授权的新研究复用；旧策略与旧报告不再保存在本仓库。

## 测试

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

订单测试必须使用 Fake/DryRun，并断言没有真实提交。修改运行逻辑后只重启受影响的项目服务；禁止重启电脑、批量结束 Python/PowerShell/PyCharm 或浏览器进程。

完整运维要求见 `docs/PROJECT_OPERATIONS.md`，修改授权和验收矩阵见 `CODE_MODIFICATION_RULES.md`。
