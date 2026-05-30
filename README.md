# Alpaca MA5 自动交易服务

这个项目参考 `StockAPI` 的文件观察池思路：只读取 `watch_codes.txt` 里的股票，按你的规则盯盘，并通过 Alpaca API 下单。

`.env` 里填 Paper key 就连接 Alpaca Paper，填 Live key 就连接 Alpaca Live；程序会自动识别。

## 策略

- 买入：当前价格 `<` 包含今日当前价的 MA5。
  - 计算方式：`today_ma5 = (前 4 个已完成交易日收盘价之和 + 当前价) / 5`
- 卖出：满足任一条件卖出全部。
  - 美股常规盘临近收盘，默认 `15:55-16:00 ET`
  - 持仓亏损 `15%`
- 范围：只处理 `watch_codes.txt` 文件中的代码。

## 文件

- `watch_codes.txt`：唯一盯盘股票文件。
- `run_monitor_once.py`：点击运行，只检查一轮。
- `run_monitor_forever.py`：点击运行，持续轮询。
- `run_generate_watch_codes.py`：点击运行，用 Alpaca 日线数据生成 `watch_codes.txt`。
- `run_test_order.py`：点击运行，提交一笔很小的 Alpaca 限价测试单，限价为当前价的 90%。
- `run_self_tests.py`：点击运行本地测试。
- `check_alpaca_connection.py`：点击检查 Alpaca API key 是否能连通。
- `alpaca_ma5_service/config.py`：运行参数都在 `build_settings()` 里改，不用命令行参数。
- `outputs/orders_YYYY-MM-DD.csv`：订单记录。

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
```

4. 检查 Alpaca 连接：

```powershell
.\.venv\Scripts\python.exe check_alpaca_connection.py
```

5. 编辑 `watch_codes.txt`，一行一个代码，例如：

```text
US.AAPL
US.TSLA
NVDA
```

6. 先跑测试：

```powershell
.\.venv\Scripts\python.exe run_self_tests.py
```

7. 检查一轮盯盘：

```powershell
.\.venv\Scripts\python.exe run_monitor_once.py
```

8. 生成 watchlist：

```powershell
.\.venv\Scripts\python.exe run_generate_watch_codes.py
```

只筛选 Alpaca `US_EQUITY` 美股股票，不读取期权、crypto 或其他资产。
筛选规则：最近一个已收盘交易日涨幅 `>20%`，上影线幅度 `>5%`，`MA5 > MA10 > MA20`，且当天 `open > MA5`。
默认全部使用 Alpaca `sip` 美股数据，避免 `iex` 局部成交导致均线失真。
候选诊断会写入 `outputs/watch_candidates_YYYY-MM-DD.csv`。

监控当前价和 MA5 也默认使用 Alpaca Market Data，不再依赖 yfinance。
美股可交易时段内用 Alpaca `iex` 最新成交价作为当前价；日线 MA 数据继续使用 `sip`。

9. 持续盯盘：

```powershell
.\.venv\Scripts\python.exe run_monitor_forever.py
```

10. 测试下单：

```powershell
.\.venv\Scripts\python.exe run_test_order.py
```

默认会提交 `AAPL` 买入限价单，金额约 `$5`，限价为当前价 `* 0.9`。如果 `.env` 里是 live key，它就提交 live 限价单。
测试下单读取当前价时也使用 Alpaca Market Data，和真实监控链路保持一致。
订单提交后默认等待 `60` 秒，未完全成交就请求取消，并再查一次订单状态确认是否真的取消。测试下单参数在 `run_test_order.py` 最下面的 `run_test_limit_order(...)` 里改。

## PyCharm 点箭头运行

把解释器设成：

```text
C:\Users\zzj\Desktop\alpaca_ma5_service\.venv\Scripts\python.exe
```

然后点这些函数左边的绿色箭头：

- `run_monitor_once.py` 里的 `run_once_alpaca_auto`
- `run_monitor_forever.py` 里的 `run_forever_alpaca_auto`
- `run_generate_watch_codes.py`
- `run_test_order.py`
- `run_self_tests.py` 里的 `test_run_all_local_tests`
- `check_alpaca_connection.py` 里的 `check_alpaca_connection`

## 盘前/盘后

常规盘内使用 market order。盘前/盘后会自动改用 Alpaca extended-hours limit order：

- `extended_hours=True`
- `time_in_force=DAY`
- 买入限价 = 当前价上浮 `0.3%`
- 卖出限价 = 当前价下浮 `0.3%`

这些参数在 `alpaca_ma5_service/config.py` 的 `build_settings()` 里改。
真实监控链路也会在订单提交后最多等待 `order_cancel_after_seconds=60` 秒；未完全成交时自动请求取消订单。只有确认取消的订单不占用每日买入名额，未确认撤单或取消失败会按风险占用，防止继续重复买入。

## 切换 Paper / Live

不用改代码。把 `.env` 换成 Paper key 就走 Paper，把 `.env` 换成 Live key 就走 Live。
运行 `check_alpaca_connection.py` 会打印当前识别到的模式。

官方文档：

- Alpaca Trading API: https://docs.alpaca.markets/docs/trading-api
- Alpaca Orders: https://docs.alpaca.markets/docs/working-with-orders
- Alpaca extended-hours orders: https://docs.alpaca.markets/docs/orders-at-alpaca
