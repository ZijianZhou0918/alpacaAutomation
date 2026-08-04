# Alpaca MA5 自动交易

这是一个本地运行、可连接 Alpaca Live/Paper 的 MA5 自动交易项目。仓库只保留当前真实运行链路、必要运维工具、复盘网页、测试，以及正式日线数据库的重建能力。

> 默认验证必须使用只读查询或 Fake/DryRun。除非用户明确授权本次真实下单，否则不得启动会提交订单的入口，也不得运行 `tools/run_test_order.py`。

## 当前策略

当前盘中入口启用 `ma5_dip_ladder` profile；它复用 `ma5_dip` 的 WatchCode 与首次买入信号。旧 `ma5_dip` profile 仅保留为显式回滚/对照选项：

- 信号日 `close / MA5 >= 1.15` 才进入盘中 WatchCode；
- 盘中依据动态 MA5、信号日涨幅分档、开盘保护和当日深度回撤产生首次买入信号，并以该刻当前价建立固定锚点；今日常规盘开盘价缺失时失败关闭；
- 真实买入只允许美股交易日 `09:30 <= t < 12:00 ET`；
- 每只股票最多 `$2,500`，按锚点、锚点 `-1%`、锚点 `-2%` 三档分配预算，每日最多启动 3 只；每轮至多提交一档 BUY LIMIT；
- 已有成交且价格触及第二档后，如未买满又回到首档，则按首档价补足剩余预算；整数股约束导致不足一股的尾差不再下单；
- 买入三档完成或到 12:00 ET 后继续持有；相对全部实际成交的加权平均成本盈利 `10%` 时，只把首次应止盈的 `50%` 仓位按触发锚点、`+1%`、`+2%` 三档 MARKET 卖出；前两小档完成后若回落到锚点，也只卖这笔半仓额度的余量；
- 每次新买入成交后，立即按 Alpaca 最新加权平均成本挂一张全仓 `-8%`、`GTC` 的原生 `STOP MARKET` 保护单；后续补仓或部分卖出会按最新数量和成本替换。策略主动止盈、止损或尾盘清仓前，必须先确认保护单已零成交撤销，避免两张卖单竞态；
- 未纳入首次半仓止盈的剩余仓位继续持有到 15:55–16:00 ET；软件层仍保留相对券商实际加权平均成本 `-10%` 的 MARKET 全部止损作为第二道退出；
- 自动监控提交后立即返回主循环，不同步等待单笔订单；订单按唯一 ID 持久化监督，每轮累计对账成交，普通订单默认 10 分钟未完成时请求撤单并继续确认终态，券商保护 STOP 不适用该超时；刚确认终态成交的股票当轮仍保持同向订单保护，下一轮重新读取持仓后才继续买卖。

分档进度原子保存到 `outputs/ladder_state.json`，未终态订单保存到 `outputs/pending_orders.json`；两者损坏都会失败关闭。低于 `$1` 的限价保留四位小数，`$1` 及以上保留两位，避免三档价位被错误合并。权威可编辑配置位于 `alpaca_ma5_service/workflows/monitoring/intraday.py`。根目录入口不复制策略参数。

## 核心入口

| 目的 | 入口 | 副作用 |
| --- | --- | --- |
| 全天按阶段运行 | `monitor_auto.py` | 盘中可能真实下单 |
| 盘中 MA5 监控 | `monitor_ma5_forever.py` | 可能真实下单 |
| 盘前持仓波动 | `monitor_premarket_ma5.py` | 只读当前持仓；60 秒涨跌 3% 提醒 |
| 盘后监控 | `monitor_afterhours.py` | 当前公开入口只提醒 |
| 生成盘中 WatchCode | `watchcode_ma5.py` | 写观察池和候选文件 |
| 旧盘前 WatchCode 入口 | `watchcode_premarket.py` | 已停用；只输出说明，不筛选、不写池 |
| 生成盘后 WatchCode | `watchcode_afterhours.py` | 写盘后观察池 |
| 每日复盘网页 | `open_daily_review.cmd` | 本地只读网页；可显式启动/停止任务 |
| 重建正式日线库 | `run_backtest_daily_history_rebuild.py` | 重型历史数据任务，不下单 |
| KDJ 极端量价回测 | `run_backtest_kdj_volume_reversal.py` | 只读日线研究，不连接券商、不下单 |

仓库只提供一个 Windows 自动化任务：`AlpacaMA5-0050-GenerateWatchcodes` 在本机时间每天 00:50 仅生成盘中 WatchCode。它通过 `tools/install_daily_watchcode_task.ps1` 安装、调用 `tools/run_daily_watchcodes.ps1`，更新盘中观察池和候选报告；不再运行任何盘前选股。任务不启动监控、不读取持仓、不提交订单。任务使用当前用户的交互登录身份；错过触发时间时由 `StartWhenAvailable` 在可运行后补跑。`monitor_auto.py` 和 `monitor_ma5_forever.py` 仍只会在用户通过命令行或复盘网页明确启动后运行。

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
- `data/watchcodes/watch_codes_premarket.txt`：仅保留为历史运行文件；当前盘前链路完全忽略，不再生成；
- `data/watchcodes/watch_code_afterhours.txt`：盘后观察池；是否能下单必须看具体调用链；
- `signal_date` 必须是最近一个已完成日线的交易日；
- 盘中池的规范化 `# Rules:` 必须与当前 `ma5_dip` 选股规则完全一致；规则变更后同日旧池也会被拒绝并重新生成；
- 所有交易日判断复用 `alpaca_ma5_service/trading_calendar.py`；
- 统一入口和直接盘中入口都会核对 WatchCode 日期与规则；已有生成任务时等待，不重复启动。

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

必须保留：每日买入上限、重复订单保护、开放订单检查、唯一 `client_order_id`、提交状态不明时暂停、部分成交暴露、替换订单追踪和撤单终态确认。自动订单的累计成交量和实际成交均价按订单 ID 幂等应用，进程重启后继续监督；终态成交写入策略状态后才能移除待确认订单。`ma5-stop-*` 券商保护单会在重启后从 Alpaca 开放订单收编；保护单部分成交或撤单待确认时禁止再发主动卖单，正常等待中的保护单本身不阻断策略退出。STOP MARKET 的 `-8%` 是触发价而非保证成交价，跳空、停牌或流动性不足时可能在更低价格成交，且不覆盖扩展时段。卖出风控会检查券商全部持仓，包括手动持仓；不得自动篡改本地账本强行对齐。给 Agent 的买卖通知采用固定字段 Markdown，首行直接显示买单/卖单及中文状态，并突出真实/Paper/DryRun 账户、数量、价格、估算金额、订单号与下一步；已提交不等于已成交。

盘前不再筛选、推荐或读取 WatchCode，只通过只读 Alpaca 持仓接口取得当前持仓；没有持仓时不读取任何股票行情，也不发送提醒。有持仓时每 10 秒读取其安全校验后的盘前实时报价，按行情时间维护滚动 60 秒窗口：当前价相对窗口低点上涨达到 3%，或相对窗口高点下跌达到 3%，才发送持仓波动提醒；重复行情时间不重复计算。该提醒没有冷冻期：提醒成功后以当前价建立新基线，同方向每再累计一段 3% 继续提醒，方向反转达到 3% 也独立提醒；通知失败不推进基线，下一条新行情重试。Moomoo 与 Alpaca 实时源发生切换时先重建基线，不跨源比较制造假波动。该链路没有任何订单方法。行情仍使用同一 Alpaca feed 的 `RAW/SPLIT` 口径校验，避免拆股/反向拆股产生虚假 3% 提醒；盘前没有新成交或报价时只等待，不用日线价格计算波动。

## 复盘网页

双击 `open_daily_review.cmd`，或从 QuickTools 打开“每日复盘”，服务才会按需启动并从 `http://127.0.0.1:8788/` 起寻找可用端口。连续 10 分钟没有 HTTP 请求时监听自动关闭；网页保持打开时的正常轮询会维持服务。

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

`backtest/reporting/` 是保留的通用 Interactive HTML 报告器。经明确授权新增的 `run_backtest_kdj_volume_reversal.py` 使用正式库研究 KDJ(81,3,3) 极端量价反转：当日 `high/low > 1.5`、`high/前收 > 1.25`、收盘涨幅 `< 20%`、成交量/前日成交量 `> 100`、J `< 0` 且不是 `close/MA5 >= 1.15` 的原 MA5 信号日时，下一交易日开盘买入；J `> 100` 后下一交易日开盘卖出。样本末仍未卖出的仓位仅按末日收盘估值。该入口不连接券商、不写 WatchCode，也不属于 Live 策略注册表。

## 测试

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

订单测试必须使用 Fake/DryRun，并断言没有真实提交。修改运行逻辑后只重启受影响的项目服务；禁止重启电脑、批量结束 Python/PowerShell/PyCharm 或浏览器进程。

完整运维要求见 `docs/PROJECT_OPERATIONS.md`，修改授权和验收矩阵见 `CODE_MODIFICATION_RULES.md`。
