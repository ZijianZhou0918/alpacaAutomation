# Alpaca MA5 自动交易服务

这个项目参考 `StockAPI` 的文件观察池思路：只读取 `watch_codes.txt` 里的股票，按你的规则盯盘，并通过 Alpaca API 下单。

`.env` 里填 Paper key 就连接 Alpaca Paper，填 Live key 就连接 Alpaca Live；程序会自动识别。

## 策略

- 买入：当前价格距离分段买点 `2%` 内时触发。
  - 计算方式：`today_ma5 = (前 4 个已完成交易日收盘价之和 + 当前价) / 5`
  - 风控过滤：今日常规盘开盘价相对信号日收盘价跌幅达到 `40%` 或更多，本轮不下单。
  - 风控过滤：今日常规盘开盘价如果低于开盘价 MA5 `10%` 或更多，这只股票当天不买入。
  - 风控过滤：今日常规盘开盘价如果低于今日动态 MA5，本轮不下单。
  - 开盘价 MA5：`(前 4 个已完成交易日开盘价之和 + 今日常规盘开盘价) / 5`
  - 信号日涨幅 `20% ~ 40%`：基础买点 `MA5 + 1.5%`
  - 信号日涨幅 `40% ~ 100%`：基础买点 `MA5 + 3%`
  - 信号日涨幅 `>100%`：基础买点 `MA5 + 4%`
  - 当天开盘涨幅 `>15%`：再加 `2%`
  - 当天开盘涨幅 `5% ~ 15%`：再加 `1%`
  - 当天开盘涨幅 `<5%` 或盘前开盘价未知：不加成
  - 最终买点：`today_ma5 * (1 + 基础买点加成 + 开盘加成)`
  - 自动下单使用 BUY LIMIT，限价固定为最终买点，不使用当前价。
  - 默认每次买入金额：`$3500`
  - 同一只股票当天下单错误累计 `3` 次后，当天不再对这只股票继续提交订单。
- 卖出：
  - 美股常规盘临近收盘，默认 `15:55-16:00 ET`
  - 持仓亏损 `10%`：卖出全部
  - 持仓收益 `10%`：卖出一半；当天已成交过一次后不重复触发
- 范围：只处理 `watch_codes.txt` 文件中的代码。

## 文件

- `watch_codes.txt`：唯一盯盘股票文件。
- `watchcode_afterhours.py`：点击运行，生成盘后监控股票池。
- `monitor_afterhours.py`：点击运行，自动生成盘后股票池并持续监控买入/卖出。
- `monitor_ma5_forever.py`：MA5 持续轮询工具。
- `watchcode_ma5.py`：用 Alpaca 日线数据生成 `watch_codes.txt`。
- `tools/start_ma5_monitor_pycharm_gui.ps1`：通过 PyCharm GUI 启动 `monitor_ma5_forever.py`。
- `tools/start_ma5_watchcode_pycharm_gui.ps1`：通过 PyCharm GUI 启动 `watchcode_ma5.py`。
- `tools/install_ma5_pycharm_tasks.ps1`：安装每天 22:00 生成 watch code、23:50 兜底启动监控的 Windows 定时任务。
- `tools/run_test_order.py`：提交一笔很小的 Alpaca 限价测试单，限价为当前价的 90%。
- `tools/run_self_tests.py`：运行本地测试。
- `tools/check_alpaca_connection.py`：检查 Alpaca API key 是否能连通。
- `alpaca_ma5_service/config.py`：运行参数都在 `build_settings()` 里改，不用命令行参数。
- `outputs/orders_YYYY-MM-DD.csv`：订单记录。

真实 Alpaca 买入、卖出和撤单结果会先写入本地 CSV，再通过本机 OpenClaw 发送 Telegram 通知；通知失败只打印错误，不会中断监控或下单流程。

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

5. 编辑 `watch_codes.txt`，一行一个代码，例如：

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

只筛选 Alpaca `US_EQUITY` 里的普通股；会排除权证、单位、优先股、ETF/基金、ADR/ADS 等特殊证券。
筛选规则：最近一个已收盘交易日涨幅 `>20%`，且必须比信号日 `MA5` 涨幅高 `10` 个百分点以上；同时要求 `MA5 > MA10 > MA20`、信号日收盘价 `close / MA5 > 1.10`，且当天 `open / MA5 > 0.95`。信号日 `MA5` 涨幅按 `信号日 MA5 / 前一交易日 MA5 - 1` 计算。
默认日线优先使用 Alpaca `sip` 全市场历史数据，并自动避开最近 15 分钟权限限制；读取失败时降级到 `iex`。
候选诊断会写入 `outputs/watch_candidates_YYYY-MM-DD.csv`。

监控当前价和 MA5 也默认使用 Alpaca Market Data，不再依赖 yfinance。
美股可交易时段内用 Moomoo OpenD 快照作为实时当前价；日线 MA 数据优先使用 Alpaca `sip` 全市场历史日线。
真实监控只会在美东工作日 `04:00-20:00` 这个实时价窗口内提交订单；但盘前 `04:00-09:30 ET` 明确不买入，只允许已有持仓触发卖出风控。周末或深夜只打印判断，不会用日线 close 冒充当前价下单。

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

- `帮我买3000刀的AAPL，购买价格固定211`：提交固定 BUY LIMIT，金额按限价换算股数。
- `帮我买5刀的NTAP，购买价格为当前价*0.95`：读取当前价后提交 BUY LIMIT。
- `帮我买5刀的NTAP，市价买入`：明确市价时才走 market buy。
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
- `tools/run_test_order.py`
- `tools/run_self_tests.py` 里的 `test_run_all_local_tests`
- `tools/check_alpaca_connection.py` 里的 `check_alpaca_connection`

## PyCharm 自动任务

安装每天自动运行的 Windows 定时任务：

```powershell
.\tools\install_ma5_pycharm_tasks.ps1
```

安装后会注册两个任务：

- `AlpacaMA5-2200-GenerateWatchcode-PyCharm`：每天本地时间 `22:00` 打开 PyCharm 到 `watchcode_ma5.py`，同时用 `.venv` 直接运行筛选，生成第二天盘中使用的 `watch_codes.txt`。
- `AlpacaMA5-2350-EnsureMonitor-PyCharm`：每天本地时间 `23:50` 检查 `monitor_ma5_forever.py` 是否已运行；如果没有，就打开 PyCharm 到该文件，同时用 `.venv` 直接启动持续监控。

日志写在 `outputs/logs/pycharm_watchcode_task_YYYYMMDD.log`、`outputs/logs/pycharm_gui_task_YYYYMMDD.log` 和 `outputs/logs/ma5_pycharm_tasks_install.log`。

这些任务需要 Windows 用户已登录才能自动打开 PyCharm；实际运行不依赖窗口焦点或快捷键。

## 盘前/盘后

自动监控买入始终使用分段买点 BUY LIMIT。盘前不提交买单；卖出常规盘内使用 market order，盘前卖出或盘后买卖会自动改用 Alpaca extended-hours limit order：

- `extended_hours=True`
- `time_in_force=DAY`
- 自动监控买入限价 = 最终买点
- 其他买入路径若需要盘前/盘后保护限价，则为当前价上浮 `0.3%`
- 卖出限价 = 当前价下浮 `0.3%`

这些参数在 `alpaca_ma5_service/config.py` 的 `build_settings()` 里改。
真实监控链路也会在订单提交后最多等待 `order_cancel_after_seconds=600` 秒（10 分钟）；未完全成交时自动请求取消订单。被 Alpaca 拒单不占用每日买入名额，只累计到该股票自己的三次错误保护；未确认撤单或撤单失败仍按风险占用，防止同一轮继续重复买入。
常规盘按 `regular_poll_seconds=10` 秒轮询；盘前/盘后通常使用 `idle_poll_seconds`，临近 9:30 ET 会自动缩短等待时间。

## 切换 Paper / Live

不用改代码。把 `.env` 换成 Paper key 就走 Paper，把 `.env` 换成 Live key 就走 Live。
运行 `tools/check_alpaca_connection.py` 会打印当前识别到的模式。

官方文档：

- Alpaca Trading API: https://docs.alpaca.markets/docs/trading-api
- Alpaca Orders: https://docs.alpaca.markets/docs/working-with-orders
- Alpaca extended-hours orders: https://docs.alpaca.markets/docs/orders-at-alpaca
