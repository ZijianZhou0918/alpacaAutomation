# Alpaca MA5 自动交易服务

这个项目参考 `StockAPI` 的文件观察池思路：买入只读取 `data/watchcodes/watch_codes.txt`，卖出风控会额外检查当前账户持仓，并通过 Alpaca API 下单。

`.env` 里填 Paper key 就连接 Alpaca Paper，填 Live key 就连接 Alpaca Live；程序会自动识别。

> [!IMPORTANT]
> 运行或排查本项目之前，必须先完整阅读 [`docs/PROJECT_OPERATIONS.md`](docs/PROJECT_OPERATIONS.md)；修改任何代码、脚本、测试、配置或前端文件前，还必须完整阅读 [`CODE_MODIFICATION_RULES.md`](CODE_MODIFICATION_RULES.md)。项目根目录的 `AGENTS.md` 会强制 Codex/代理在每次任务中执行对应门禁。

## 30 秒看懂流程

```text
monitor_auto.py
  -> 判断交易日和美股时段
  -> 生成/确认对应 WatchCode
  -> 盘前推荐提醒 | 盘中策略交易 | 盘后 high/low 提醒
  -> 订单记录、通知和每日复盘
```

盘中策略交易再拆成两条清晰链路：

```text
WatchCode 内无持仓股票 -> 买入策略 -> Broker -> 撤单/终态确认
券商当前全部持仓       -> 卖出策略 -> Broker -> 撤单/终态确认
```

- 全天自动运行从 `monitor_auto.py` 开始。
- 运行盘中监控从根目录 `monitor_ma5_forever.py` 开始；策略参数在 `alpaca_ma5_service/workflows/monitoring/intraday.py` 顶部配置。
- 四类策略代码分别在 `strategy_framework/components/watchcode.py`、`buy.py`、`sell.py`、`cancel.py`。
- 完整流程图见 [`docs/architecture/PROJECT_FLOW.md`](docs/architecture/PROJECT_FLOW.md)。
- 想直接定位买入、卖出、撤单和真正 Alpaca 写入位置，先看 [`docs/architecture/TRADE_ORDER_FLOW.md`](docs/architecture/TRADE_ORDER_FLOW.md)。
- 新增策略导航见 [`alpaca_ma5_service/strategy_framework/README.md`](alpaca_ma5_service/strategy_framework/README.md)。

## 策略

下列规则是默认 `ma5_dip` 组合的当前行为。运行时框架会把 WatchCode、买入、卖出和自动撤单分别解析为独立组件；可整体切换组合，也可只替换某一类策略。

- 买入：当前价格距离分段买点 `2%` 内时触发。
  - 计算方式：`today_ma5 = (前 4 个已完成交易日收盘价之和 + 当前价) / 5`
  - 风控过滤：当前价相对信号日收盘价跌幅必须达到 `10%` 或更多，才允许触发买入。
  - 风控过滤：如果当前价已经触达今日动态 MA5，但当前跌幅还没有达到 `10%`，这只股票当天不再考虑买入。
  - 风控过滤：今日常规盘开盘价相对信号日收盘价跌幅达到 `40%` 或更多，本轮不下单。
  - 风控过滤：今日常规盘开盘价如果低于开盘价 MA5 `10%` 或更多，这只股票当天不买入。
  - 风控过滤：今日常规盘开盘价如果低于今日动态 MA5，本轮不下单。
  - 开盘价 MA5：`(前 4 个已完成交易日开盘价之和 + 今日常规盘开盘价) / 5`
  - 信号日涨幅 `15% ~ 40%`：基础买点 `MA5 + 0.5%`
  - 信号日涨幅 `40% ~ 100%`：基础买点 `MA5 + 3%`
  - 信号日涨幅 `>100%`：基础买点 `MA5 + 4%`
  - 当天开盘涨幅 `>15%`：再加 `2%`
  - 当天开盘涨幅 `5% ~ 15%`：再加 `1%`
  - 当天开盘涨幅 `<5%` 或盘前开盘价未知：不加成
  - 最终买点：`today_ma5 * (1 + 基础买点加成 + 开盘加成)`
  - 自动下单使用 BUY LIMIT，限价固定为最终买点，不使用当前价。
  - 真实买入只允许常规盘开盘后前 `2.5` 小时，即 `09:30-12:00 ET`。
  - `alpaca_ma5_service/workflows/monitoring/intraday.py` 里的 `BUY_STOCK_COUNT` 控制本轮最多买入股票数，`BUY_NOTIONAL_USD` 控制每只股票入场金额；如果入口不覆盖金额，默认每只股票 `$1500`；所有买入只提交整数股。
  - 同一只股票当天下单错误累计 `3` 次后，当天不再对这只股票继续提交订单。
- 卖出：
  - 美股常规盘临近收盘，默认 `15:55-16:00 ET`
  - 持仓亏损 `10%`：按成本价亏损 `8%` 的价格提交 SELL LIMIT 卖出全部；这条会检查当前账户所有持仓，不只限 `data/watchcodes/watch_codes.txt` 或当天买入的股票
  - 监控进程启动后第一次检查时，如果某个旧持仓已经亏损 `10%` 或更多，本次监控会话不会自动清仓；监控启动后新出现的持仓，或旧持仓成本/数量变化后，再跌到 `10%` 会触发限价清仓
  - 持仓收益 `10%`：卖出一半；当天已成交过一次后不重复触发
  - 半仓止盈已成交后，剩余仓回落到成本价 `+5%`：用保护 SELL LIMIT 卖出剩余全部
- 范围：买入只处理 `data/watchcodes/watch_codes.txt` 中的代码。

### 动态策略组合

默认组合和四类策略都在 `alpaca_ma5_service/workflows/monitoring/intraday.py` 顶部配置区选择；根目录 `monitor_ma5_forever.py` 的点击运行/PyCharm 运行方式不变：

```python
STRATEGY_NAME = GAP_CONFIRMED_PULLBACK_STRATEGY
WATCHLIST_STRATEGY_NAME = STRATEGY_NAME
BUY_STRATEGY_NAME = STRATEGY_NAME
SELL_STRATEGY_NAME = DEFAULT_SELL_STRATEGY_NAME
CANCEL_STRATEGY_NAME = DEFAULT_CANCEL_STRATEGY_NAME
```

- `STRATEGY_NAME` 是基础组合，提供四类组件和止损/止盈等运行默认值。
- 当前盘中默认组合已切换为 gap profile：固定单笔 `$2,500`、每日最多 3 笔/3 个持仓、`-8%` 止损和 `+4%` 全部止盈；信号日振幅上限 `30%` 在线上 WatchCode 与回测中一致执行。`ma5_dip` 仍保留为可选 profile。2025 开发集冻结的新候选在 Q4 和 2026 交叉验证中均未同时超过当前信号，因此没有替换 gap 的信号规则。
- 四个分类变量可以独立覆盖基础组合；例如 WatchCode 用缺口策略、买入仍用 MA5。
- `watchcode_ma5.py`、`monitor_auto.py` 和盘中监控共用 `build_monitor_settings()`，不会再分别硬编码两套策略。
- 所有名称会在行情、账户和订单 I/O 之前统一解析；名称不存在、接口不完整或组合缺组件时直接终止。
- 也可通过 `.env` 的 `STRATEGY_PROFILE`、`WATCHLIST_STRATEGY`、`BUY_STRATEGY`、`SELL_STRATEGY`、`CANCEL_STRATEGY` 配置；入口显式配置优先。
- 新策略实现和注册方法见 [`docs/architecture/STRATEGY_FRAMEWORK.md`](docs/architecture/STRATEGY_FRAMEWORK.md)。

## 入口与目录

根目录中的 Python 文件全部是薄运行入口；实现、配置和运行数据分别进入对应目录：

```text
项目根目录
├─ monitor_*.py / watchcode_*.py / run_backtest*.py  # 公开运行入口
├─ open_daily_review.py / open_daily_review.cmd       # 复盘入口
├─ alpaca_ma5_service/
│  ├─ workflows/          # 监控、WatchCode、复盘编排
│  ├─ strategy_framework/ # 四类策略组件和组合
│  └─ *.py                # 核心服务、行情、Broker、复盘 API
├─ backtest/
│  ├─ runners/            # 回测命令实现
│  ├─ reporting/          # 通用交互式报告模型、模板和静态资源
│  └─ *.py                # 回测引擎和数据模块
├─ data/watchcodes/       # 三个时段的 WatchCode 运行数据
├─ tools/                 # 运维、自检和任务脚本
├─ tests/                 # 回归测试
└─ docs/                  # 架构与运行手册
```

先按目的找入口：

| 目的 | 打开 |
| --- | --- |
| 全天自动运行 | `monitor_auto.py` |
| 只运行盘中监控 | `monitor_ma5_forever.py` |
| 配置盘中策略 | `alpaca_ma5_service/workflows/monitoring/intraday.py` |
| 生成盘中 WatchCode | `watchcode_ma5.py` |
| 查看每日复盘 | `open_daily_review.cmd` |
| 新增或组合策略 | `alpaca_ma5_service/strategy_framework/` |
| 找到买入、卖出、撤单真实执行代码 | `docs/architecture/TRADE_ORDER_FLOW.md` |
| 运行历史回测 | `run_backtest_*.py` |

- `data/watchcodes/watch_codes.txt`：唯一盘中盯盘股票文件。
- `data/watchcodes/watch_codes_premarket.txt`：盘前推荐专用股票文件，只给推荐提醒使用，不参与自动下单。
- `watchcode_afterhours.py`：点击运行，生成盘后监控股票池。
- `monitor_afterhours.py`：点击运行，自动生成盘后股票池并持续监控价格提醒，不提交买单或卖单。
- `watchcode_premarket.py`：点击运行，生成最近已收盘交易日涨幅前 50 的盘前推荐股票池。
- `monitor_premarket_ma5.py`：点击运行，盘前监控 `data/watchcodes/watch_codes_premarket.txt`，靠近动态 MA5 时发送云端推荐提醒，不下单。
- `monitor_ma5_forever.py`：MA5 持续轮询工具。
- `alpaca_ma5_service/strategy_framework/`：四类策略组件、profile、契约、注册表、运行时解析和扩展注册入口。
- `monitor_auto.py`：每天自动监控的单一入口，自动检查当前是盘前、盘中还是盘后，缺 watchcode 时先生成，再运行对应监控。
- `watchcode_ma5.py`：用 Alpaca 日线数据生成 `data/watchcodes/watch_codes.txt`。
- `watchcode_chart.py`：按文件顶部 `CHART_SESSION` 选择盘前/盘中/盘后 watchcode，单独刷新同款 HTML 图表，不重新筛选股票。
- `tools/start_ma5_monitor_pycharm_gui.ps1`：通过 PyCharm GUI 和 `.venv` 兜底启动 `monitor_auto.py` 单一入口。
- `tools/start_ma5_watchcode_pycharm_gui.ps1`：通过 PyCharm GUI 和 `.venv` 兜底生成盘中与盘前 watchcode。
- `tools/check_ma5_trading_day.py`：给 Windows 定时任务判断目标日期是否为美股交易日，优先 Alpaca calendar，失败时用内置 NYSE/Nasdaq 节假日表兜底。
- `tools/install_ma5_pycharm_tasks.ps1`：安装每天 22:00 生成 watch code、00:50 兜底启动监控、04:00 健康检查的 Windows 定时任务。
- `tools/run_test_order.py`：提交一笔很小的 Alpaca 限价测试单，限价为当前价的 90%。
- `tools/run_self_tests.py`：运行本地测试。
- `tools/check_alpaca_connection.py`：检查 Alpaca API key 是否能连通。
- `alpaca_ma5_service/workflows/monitoring/intraday.py`：持续监控的常用运行参数都在文件顶部配置区改，不用命令行参数。
- `outputs/orders_YYYY-MM-DD.csv`：订单记录。
- `open_daily_review.cmd`：双击打开“MA5 每日复盘”网页；服务只读，不会下单或修改观察池。

## MA5 每日复盘网页

双击项目根目录的：

```text
open_daily_review.cmd
```

也可以在 PowerShell 里运行：

```powershell
.\.venv\Scripts\python.exe .\open_daily_review.py
```

网页会先用本地日志、候选 CSV、排除记录和订单账本秒开当天复盘，然后自动通过服务端对 Alpaca 做只读核对。页面重点区分：

- 券商实际买入、卖出、取消和当前持仓。
- MA5 策略在真实买入窗口内的最佳机会、全天最接近与日终状态。
- 已买、买入未成、条件未满足、今日排除、窗口外最接近和来源未核对。
- 本地账本与 Alpaca 订单不一致、开放订单、持仓增减和缺失数据。
- 盘前、盘中、盘后覆盖范围、事件时间线、原始证据和数据新鲜度。

服务默认只绑定 `127.0.0.1`，端口从 `8788` 开始自动选择；不会把 `.env` 或 API key 发给浏览器。历史页面中的“当日成交净现金流”只按买卖成交额计算，未含费用，也不等同于券商口径的已实现盈亏。

真实 Alpaca 买入、卖出和撤单结果会先写入本地 CSV，再按 `.env` 里的通知通道发送 Telegram 通知；通知失败只打印错误，不会中断监控或下单流程。

## Telegram 本地通知

通知通道由 `.env` 的 `TRADE_NOTIFY_MODE` 控制：

- `TRADE_NOTIFY_MODE=local`：走本机 OpenClaw/Hermes。OpenClaw gateway 没启动时会先尝试 `gateway probe/start/run`，再发送 Telegram；Hermes `send` 会直接使用本机 Hermes 配置。
- `TRADE_NOTIFY_MODE=cloud`：走云端 Hermes webhook，需要同时配置 `CLOUD_NOTIFY_WEBHOOK_URL` 和 `CLOUD_NOTIFY_WEBHOOK_SECRET`。

点击运行测试：

```powershell
.\.venv\Scripts\python.exe tools\send_local_notify.py
```

Python 里如果想让一条控制台消息同步发 Telegram：

```python
from alpaca_ma5_service.console_notify import notify_print

notify_print("任务完成了", context="manual task")
```

## 第一次使用

1. 打开 PowerShell：

```powershell
cd C:\Users\zzj\Desktop\alpaca_ma5_service
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

2. 去 Alpaca 后台生成 API key：

- API Key ID
- Secret Key

3. 复制 `.env.example` 为 `.env`，填写：

```text
APCA_API_KEY_ID=你的 API Key ID
APCA_API_SECRET_KEY=你的 Secret Key
TRADE_NOTIFY_OPENCLAW_ENABLED=true
TRADE_NOTIFY_MODE=local
CLOUD_NOTIFY_WEBHOOK_URL=
CLOUD_NOTIFY_WEBHOOK_SECRET=
OPENCLAW_TELEGRAM_TARGET=你的 Telegram target
OPENCLAW_GATEWAY_PORT=18789
REALTIME_PRICE_SOURCE=moomoo
MOOMOO_HOST=127.0.0.1
MOOMOO_PORT=11111
```

4. 检查 Alpaca 连接：

```powershell
.\.venv\Scripts\python.exe tools\check_alpaca_connection.py
```

监控实时价格会连接本机 Moomoo OpenD，请先启动并登录 OpenD，确认 API 端口是 `11111`。

5. 编辑 `data/watchcodes/watch_codes.txt`，一行一个代码，例如：

```text
US.AAPL
US.TSLA
NVDA
```

6. 先跑测试：

```powershell
.\.venv\Scripts\python.exe tools\run_self_tests.py
```

7. 启动盘中持续盯盘：

```powershell
.\.venv\Scripts\python.exe monitor_ma5_forever.py
```

8. 生成 watchlist：

```powershell
.\.venv\Scripts\python.exe watchcode_ma5.py
```

只筛选 Alpaca `US_EQUITY` 里的常规普通股；会排除权证、单位、优先股、ETF/基金、ADR/ADS、纯 5 字母 ticker，以及 `Class B`/`Series` 等特殊股本类别。
当前 `ma5_dip` 筛选规则：最近一个已收盘交易日涨幅必须达到运行配置的阈值，并高于信号日 `MA5` 涨幅；信号日收盘价必须比包含该日收盘价计算的当日 `MA5` 至少高 15 个点（`close/MA5 >= 1.15`），但不要求 `MA5 > MA10 > MA20`。`MA5`、`MA10`、`MA20` 仍会计算并写入候选 CSV 和图表，供诊断与复盘使用。
默认日线优先使用 Alpaca `sip` 全市场历史数据，并自动避开最近 15 分钟权限限制；读取失败时降级到 `iex`。
候选诊断会写入 `outputs/watch_candidates_YYYY-MM-DD.csv`。

只按当前 `data/watchcodes/watch_codes.txt` 重新制图，不重新筛选股票：

```powershell
.\.venv\Scripts\python.exe watchcode_chart.py
```

监控当前价和 MA5 也默认使用 Alpaca Market Data，不再依赖 yfinance。
美股可交易时段内用 Moomoo OpenD 快照作为实时当前价；日线 MA 数据优先使用 Alpaca `sip` 全市场历史日线。
真实监控只会在美股交易日 `04:00-20:00 ET` 这个实时价窗口内提交订单；但盘前 `04:00-09:30 ET` 明确不买入，只允许已有持仓触发卖出风控。周末、节假日或深夜只打印判断，不会用日线 close 冒充当前价下单。

9. 持续盯盘：

```powershell
.\.venv\Scripts\python.exe monitor_ma5_forever.py
```

10. 测试下单：

```powershell
.\.venv\Scripts\python.exe tools\run_test_order.py
```

默认会提交 `AAPL` 买入限价单，金额约 `$5`，限价为当前价 `* 0.9`。如果 `.env` 里是 live key，它就提交 live 限价单。
测试下单读取当前价时也使用 Alpaca Market Data，和真实监控链路保持一致。
订单提交后默认等待 `600` 秒（10 分钟），未完全成交就请求取消，并再查一次订单状态确认是否真的取消。测试下单参数在 `tools/run_test_order.py` 最下面的 `run_test_limit_order(...)` 里改。

## OpenClaw 对话下单

OpenClaw 的对话控制配置在 `C:\Users\zzj\.openclaw\workspace`，不需要启动本仓库里的 HTTP 服务。
OpenClaw 在直接聊天里识别到买入、卖出或撤单请求时，会调用：

```powershell
C:\Users\zzj\.openclaw\workspace\openclaw-alpaca-trade.ps1
```

然后这个包装脚本再调用本仓库的 `tools/run_openclaw_trade_command.py`，复用同一套 Alpaca 下单、撤单、记录和通知逻辑。

常见消息例子：

- `帮我买3000刀的AAPL，购买价格固定211`：提交固定 BUY LIMIT，金额按限价换算整数股。
- `帮我买3000刀的NTAP，购买价格为当前价*0.95`：读取当前价后提交 BUY LIMIT。
- `帮我买3000刀的NTAP，市价买入`：明确市价时才走 market buy。
- `帮我卖出AAPL，限价210`：未写股数时默认卖出当前 AAPL 全部持仓。
- `帮我卖出10股AAPL，价格为当前价*1.05`：读取当前价后提交 SELL LIMIT。
- `帮我卖出10股AAPL，市价卖出`：明确市价时才走 market sell。
- `撤单AAPL`：取消 AAPL 当前 open orders。
- `撤单 订单号: FU1C9F9AADE3E56000`：按订单号撤单。
- `全部撤单`：取消当前账户全部 open orders。

如果消息像交易指令但不符合这些格式，脚本会在下单前直接打回，并输出常见可用格式。这样可以避免 OpenClaw 猜错金额、价格或下单类型。

OpenClaw 手动单只做格式校验，不走自动监控策略里的 MA5、盘前买入等筛选；是否接受订单以 Alpaca 实际返回为准。订单结果会写入 `outputs/orders_YYYY-MM-DD.csv`，并复用当前 OpenClaw 通知逻辑。

## PyCharm 点箭头运行

把解释器设成：

```text
C:\Users\zzj\Desktop\alpaca_ma5_service\.venv\Scripts\python.exe
```

然后点这些函数左边的绿色箭头：

- `monitor_afterhours.py` 里的 `monitor_afterhours`
- `watchcode_afterhours.py` 里的 `generate_afterhours_watchcode`
- `monitor_ma5_forever.py` 里的 `monitor_ma5_forever`
- `watchcode_ma5.py`
- `watchcode_chart.py` 里的 `refresh_current_watchcode_chart`
- `tools/run_test_order.py`
- `tools/run_self_tests.py` 里的 `test_run_all_local_tests`
- `tools/check_alpaca_connection.py` 里的 `check_alpaca_connection`

## PyCharm 自动任务

安装每天自动运行的 Windows 定时任务：

```powershell
.\tools\install_ma5_pycharm_tasks.ps1
```

安装后会注册三个任务：

- `AlpacaMA5-2200-GenerateWatchcode-PyCharm`：每天本地时间 `22:00` 打开 PyCharm 到 `watchcode_ma5.py`，同时用 `.venv` 直接运行 `watchcode_ma5.py` 和 `watchcode_premarket.py`，生成 `data/watchcodes/watch_codes.txt` 与 `data/watchcodes/watch_codes_premarket.txt`。
- `AlpacaMA5-0050-EnsureMonitor-PyCharm`：每天本地时间 `00:50` 检查 `monitor_auto.py` 是否已运行；如果没有，就打开 PyCharm 到该文件，同时用 `.venv` 直接启动单一入口，并打开一个 UTF-8 日志跟随窗口显示输出。
- `AlpacaMA5-0400-HealthCheck-PyCharm`：每天本地时间 `04:00` 再检查 `monitor_auto.py` 是否运行，并检查 `data/watchcodes/watch_codes.txt` 与 `data/watchcodes/watch_codes_premarket.txt` 是否足够新；如果监控缺失或 watchcode 过旧，就调用现有脚本重新启动/重新生成。

每个任务真正执行前都会先做交易日判断：`22:00` 生成 watchcode 检查“明天”是否为美股交易日；`00:50` 启动监控和 `04:00` 健康检查检查“今天”是否为美股交易日。若目标日期是周末或节假日，任务会写日志并正常退出，不生成、不启动、不报失败。

日志写在 `outputs/logs/pycharm_watchcode_task_YYYYMMDD.log`、`outputs/logs/pycharm_gui_task_YYYYMMDD.log`、`outputs/logs/ma5_0400_health_YYYYMMDD.log` 和 `outputs/logs/ma5_pycharm_tasks_install.log`；自动监控会另外打开一个 UTF-8 PowerShell 日志跟随窗口，直接显示 Python 输出。

这些任务需要 Windows 用户已登录才能自动打开 PyCharm；实际运行不依赖窗口焦点或快捷键。

## 盘前/盘后

盘前推荐是独立提醒链路，不会提交 Alpaca 订单：

- 先运行 `watchcode_premarket.py`，按最近已收盘交易日涨幅排序，写出前 50 到 `data/watchcodes/watch_codes_premarket.txt`。
- 再运行 `monitor_premarket_ma5.py`，只在盘前 `04:00-09:30 ET` 发送云端提醒；到 `09:30 ET` 自动退出。
- 提醒条件：盘前当前价相对最近已收盘日收盘价跌幅至少 `15%`，且当前价在动态 MA5 上方 `0%` 到 `3%` 内；同一股票同一天按 `3%/2%/1%/0%` 距离档位去重，价格更靠近 MA5 时可再次提醒。

自动监控买入始终使用分段买点 BUY LIMIT。盘前不提交买单；止损卖出使用成本价亏损 8% 的 SELL LIMIT；非止损卖出在常规盘内使用 market order，盘前卖出或盘后买卖会自动改用 Alpaca extended-hours limit order：

- `extended_hours=True`
- `time_in_force=DAY`
- 自动监控买入限价 = 最终买点
- 其他买入路径若需要盘前/盘后保护限价，则为当前价上浮 `0.3%`
- 非止损盘前/盘后卖出限价 = 当前价下浮 `0.3%`

这些参数在 `alpaca_ma5_service/workflows/monitoring/intraday.py` 文件顶部的配置区里改。
真实监控链路也会在订单提交后最多等待 `order_cancel_after_seconds=600` 秒（10 分钟）；未完全成交时自动请求取消订单。被 Alpaca 拒单不占用每日买入名额，只累计到该股票自己的三次错误保护；未确认撤单或撤单失败仍按风险占用，防止同一轮继续重复买入。
常规盘按 `regular_poll_seconds=10` 秒轮询；盘前/盘后通常使用 `idle_poll_seconds`，临近 9:30 ET 会自动缩短等待时间。盘中监控到 `16:00 ET` 自动退出。

## 两年全普通股日线数据

使用专用入口重建 `backtest/data/market_data.sqlite`：

```powershell
.\.venv\Scripts\python.exe run_backtest_daily_history_rebuild.py --start-date 2024-07-17 --end-date 2026-07-16
```

数据来自 Alpaca SIP，周期为 `1Day`、`split` 复权，保存 OHLC、成交量、VWAP、成交笔数、时间戳和 MA5/10/20。股票池合并当前 active/inactive US equity，先排除 ETF、ETN、权证、权利、单位、优先股、债券、基金、SPAC、非经营性 Trust、结构化证券、OTC 和测试证券，再纳入 NYSE/NASDAQ/AMEX 上其余上市股；不要求证券名称必须显式包含 `Common Stock`。正式库不写入一分钟数据。

以后所有依赖日线的回测均优先读取正式库 `backtest/data/market_data.sqlite`。正式库覆盖不足时必须明确报出缺口；不得静默切换到其他日线库，也不得把分钟线写入正式库。需要分钟数据的策略使用独立分钟缓存。

任务先写 `backtest/data/market_data.sqlite.rebuild`；全部批次下载并校验通过后先备份旧 SQLite，再原子替换正式库。当前证券目录不是权威历史时点主表，因此虽包含 inactive/退市候选，仍不能视为完全消除幸存者偏差。详细替换和校验规则见 [`docs/PROJECT_OPERATIONS.md`](docs/PROJECT_OPERATIONS.md)。

## 通用交互式回测报告

标准回测引擎生成的 HTML 由 `backtest/reporting/` 统一渲染。主报告可直接打开；包含权益曲线、数据可靠性门禁、股票搜索与盈亏筛选、按股票交易轮次、买入日前后日 K、MA5/MA10/MA20、成交量、事件轨、深链和手机布局。逐股证据默认按每只股票最近一次交易时间从新到旧排列，也可以一键切换为最早优先；搜索和筛选不会打乱当前时间顺序。日 K 买卖箭头以实际成交日期和成交价为锚点，并同时显示成交价位于实体、上下影线或日 K 区间外的核验结果；轮次已实现收益和该股票累计收益分开显示。

点击日 K 或事件日期可下钻当天 1 分钟 K，买卖点按完整成交时间和成交价标注。分钟行情按股票写入 `symbol_details/*.minute.js` 并在用户下钻时按需加载，以免把全部分钟数据塞进主 HTML；缺少当天或成交时刻分钟 K 时明确提示，不回退日期，也不把成交点吸附到相邻 K 线。新回测只需把结果适配成 `InteractiveReportDocument`，无需复制整套 HTML/CSS/JavaScript。

报告是历史研究证据，不连接 Paper/Live 账户，也不会提交或取消订单。Plotly 图表默认通过 CDN 加载；网络不可用时，表格和文字证据仍可阅读。

## 信号日强势 + 动态 MA5 回测

双击或在 PyCharm 运行 `run_backtest_signal_dynamic_ma5.py`。策略先从正式全普通股日线库筛选：信号日 `MA5 > MA10 > MA20`、涨幅严格大于 `10%`、阳线实体严格大于 `10%`；下一交易日开盘价相对信号日收盘价严格上涨后，才进入动态 MA5 观察。

买入日的动态 MA5 定义为“前 4 个已完成交易日收盘价 + 当前已完成 1 分钟 K 线收盘价”除以 5。只有当前已完成 1 分钟 K 线收盘价小于或等于动态 MA5，且相对买入日开盘价跌幅严格大于 `15%` 时才触发；为避免前视，统一在下一根 1 分钟 K 线开盘成交，且该实际入场价相对买入日开盘价的跌幅仍须严格大于 `15%`。实际成交时间必须满足 `09:30 <= t < 12:00 ET`，12:00 ET 及以后禁止买入；全天未达到条件就不买。盈利 `5% / 10% / 15%` 时各卖出原始仓位的 `1/3`，亏损 `10%` 清仓，剩余仓位在常规盘最后一分钟收盘清仓；同一分钟同时触发止损和止盈时按止损优先。

候选日分钟线只从 Alpaca SIP 读取并保存到独立缓存 `backtest/data/signal_dynamic_ma5_minute_cache.sqlite`，不会写入正式日线库。结果写入 `backtest/output/signal_dynamic_ma5/`。默认每个候选独立使用 `$10,000` 名义本金、零佣金和零滑点，因此汇总盈亏不是受资金容量与并发持仓约束的组合收益。该入口只读历史行情，不读取账户、持仓或订单，也不启动监控与 WatchCode。

## Gap pullback 2025 受限优化

`run_backtest_gap_strategy_optimization.py` 使用正式 SIP/split 日线库和独立
gap 分钟缓存。开发集固定为 2025-01-01..09-30，Q4 只在候选冻结后打开一次，
2026 由代码硬锁并留给外部交叉验证。原胜率研究候选把买入回撤区间从
`-8%..-2%` 收窄为 `-8%..-5%`，把全仓止盈从 `8%` 改为 `4%`；策略旧名称
为兼容已有配置继续保留。

阶段结果、逐笔证据、冻结清单、滑点压力、验证 Markdown 和 notebook 位于
`backtest/output/gap_strategy_optimization/`。用
`run_backtest_gap_strategy_validation.py` 可从已保存逐笔成交重算置信区间、
PBO 和 Deflated Sharpe。研究入口不读取账户或订单，也不会自动把当前
`ma5_dip` 监控切换为 gap。

总收益研究使用同一入口的 `return_signal`、`return_sizing`、
`return_holdout` 和 `return_robustness` 阶段，以 `$100,000` 初始现金、
无杠杆、每次成交 `10bp` 滑点后的组合收益为主目标。开发期冻结候选为：
至少回撤 `4%`、信号日收盘位于当日区间上方 `40%`、盈利 `4%` 全部卖出、
每笔 `$20,000`、每日/并发最多 5 笔。它在开发期收益为 `145.38%`，但
Q4 为 `-3.29%`、利润因子 `0.964`、单独最大回撤 `-18.42%`，所以只保留
为研究候选，不更新 Live profile。用
`run_backtest_gap_strategy_return_validation.py` 从已保存证据重算报告；产物
位于 `backtest/output/gap_strategy_return_optimization/`。2026 仍是唯一
干净的外部交叉验证集。

## 切换 Paper / Live

不用改代码。把 `.env` 换成 Paper key 就走 Paper，把 `.env` 换成 Live key 就走 Live。
运行 `tools/check_alpaca_connection.py` 会打印当前识别到的模式。

官方文档：

- Alpaca Trading API: https://docs.alpaca.markets/docs/trading-api
- Alpaca Orders: https://docs.alpaca.markets/docs/working-with-orders
- Alpaca extended-hours orders: https://docs.alpaca.markets/docs/orders-at-alpaca

# 日内动态涨幅榜回测网页

新增的独立历史研究模块位于 [`intraday_top20`](intraday_top20/README.md)。它实现动态盘中 Top N、VWAP/SMA 状态机、下一根五分钟 K 线成交、成交量参与率、成本、尾盘/停牌处理、稳健性测试和 Streamlit 多页面报告，不连接账户或订单接口。

启动：

```powershell
.\.venv\Scripts\python.exe -m streamlit run intraday_top20\app.py
```

仓库当前只附带明确标记的合成验收数据，不能据此判断策略真实收益；真实数据字段、证券主表、拆股表和可信度门禁见模块 README。
