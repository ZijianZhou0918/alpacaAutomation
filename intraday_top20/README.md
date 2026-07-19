# 日内动态涨幅榜 VWAP Reclaim 回测与网页

这是一个与现有 MA5 实盘/监控链路完全隔离的历史研究模块。它不会读取账户、持仓或订单，也没有任何下单入口。

> 当前仓库没有覆盖全市场的一年分钟数据，也没有可直接下载该数据的 Massive flat-file 凭据。仓库内置的 `example_data` 是确定性合成数据，只用于验证代码、测试、图表和导出；其收益率不代表真实策略表现。

## 目录

```text
intraday_top20/
├── app.py                         # Streamlit 入口
├── run_backtest.py                # CLI 回测/稳健性入口
├── backtest/
│   ├── config.py                  # 参数模型与校验
│   ├── engine.py                  # 五分钟事件驱动主循环
│   ├── universe.py                # 点时证券过滤与动态 Top N
│   ├── indicators.py              # 盘中 VWAP / SMA
│   ├── strategy.py                # 信号状态机
│   ├── execution.py               # 滑点、价差、佣金、成交量参与率
│   ├── portfolio.py               # 等权资金分配、止盈、尾盘退出
│   ├── metrics.py                 # 绩效与统计诊断
│   └── robustness.py              # 参数敏感性场景
├── data/
│   ├── loader.py                  # 分块读取与前收/拆股处理
│   ├── cleaner.py                 # UTC→ET、RTH 过滤、五分钟聚合
│   ├── cache.py                   # 行情和结果缓存
│   └── sample_data.py             # 明确标记的合成数据生成器
├── pages/                         # 八个网页模块
├── visualization/                 # Plotly 图表与独立 HTML 报告
├── config/default_config.yaml
├── tests/
└── outputs/                       # 运行生成，已忽略
```

## 策略与时序口径

1. 每个交易日只保留 09:30–16:00 `America/New_York` 正常时段数据，并聚合成以开始时间标记的五分钟 K 线。
2. 每根 K 线收盘后，使用该根收盘价除以前一交易日最后一个正常时段收盘价减一，重新排列当时有完整 K 线、满足证券和流动性过滤的 Top N。没有使用当天最终排名。
3. 默认均线为当日累计 `成交额 / 成交量`。若源数据没有成交额，按 `((高+低+收)/3) × 成交量` 近似。SMA 是可选对照参数。
4. 状态机必须先见到 `close > indicator`；之后 `close <= indicator` 连续保持严格超过配置分钟数。五分钟数据下使用 `minutes // 5 + 1`，因此默认 20 分钟实际要求 5 根完整 K 线，即 25 分钟。
5. 当前 K 线满足 `前收 <= 前均线` 且 `当前收 > 当前均线` 时确认重新站上。只有当前仍在 Top N 才发出信号；实际买入时间是当前 `bar_end`，也就是下一根 K 线的开始时间。
6. 同一时刻信号先按排名排序，再在现金、单笔最大仓位、持仓上限和每日开仓上限内等权分配。默认不借钱、不做空。
7. 买入、止盈和退出均受到五分钟成交量参与率限制。成交价包含半边价差、基础滑点、低价附加滑点和高波动附加滑点；佣金单独计入。
8. 实际买入价上涨到止盈比例时，以目标价为参考并扣除卖出滑点，最多卖出初始数量的 50%。剩余仓位在 15:55 K 线开盘处理。
9. 停牌或缺少 15:55 K 线时不假设理想成交。仓位保留为异常未解决状态，仅在下一根真实正常时段 K 线开盘继续按参与率退出，并标记 `forced_overnight`，从而使可信度门禁失败。
10. 任意缺失的五分钟 K 线都会重置“连续跌破”计时和跨线比较；停牌或数据断档前后的 K 线不能拼成买入信号。

## 历史数据放置

推荐使用全市场、逐日的分钟或五分钟文件。Massive 的美股分钟 flat files 是每日一个全市场文件、时间戳为 UTC；官方也说明 flat files 为未复权数据并包含盘前、正常时段和盘后，因此本模块显式转换时区、过滤正常时段并通过拆股表对前收做边界调整。参考：

- [Massive Stocks Minute Aggregates](https://massive.com/docs/flat-files/stocks/minute-aggregates)
- [Massive Stocks Flat Files Overview](https://massive.com/docs/flat-files/stocks/overview?assetClass=stocks&license=personal&name=stocks_basic)
- [Massive Custom Bars REST Documentation](https://massive.com/docs/rest/stocks/aggregates/custom-bars)

文件名必须含 `YYYY-MM-DD`，支持 CSV、CSV.GZ 和 Parquet。最小字段可使用常规命名或 Massive 简写：

```text
ticker/window_start/open/high/low/close/volume
# 或
symbol/timestamp/o/h/l/c/v
```

`timestamp` 可为 ISO UTC 字符串，或秒/毫秒/纳秒 Unix 时间。回测开始日前还必须至少提供一个交易日文件作为前收预热，否则首日对应股票无法进入排名。

### 点时证券主表

`security_master.csv` 至少应有：

```text
symbol,asset_type,primary_exchange,tradable,active,effective_date,start_date,end_date
```

`asset_type` 默认只接受普通股；ETF、ETN、权证、权利、优先股、Unit 和基金被排除，OTC 交易所被排除。`effective_date` 让同一股票的历史状态按回测日选择，`start_date/end_date` 用于上市和退市边界。没有点时主表时只能做保守代码格式过滤，可靠性门禁不会通过。

### 拆股表与复权

未复权数据使用：

```text
symbol,execution_date,split_from,split_to
```

拆股生效日将前收乘以 `split_from / split_to`，使涨幅分子分母处于同一股本口径。若行情供应商已对全历史复权，则将 `source_adjusted: true` 且不要重复应用拆股。除权、分红和代码变更仍需由数据供应商或更完整的公司行动表保证。

## 安装与启动

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m intraday_top20.data.sample_data
.\.venv\Scripts\streamlit.exe run intraday_top20\app.py
```

网页启动命令也可以写成：

```powershell
.\.venv\Scripts\python.exe -m streamlit run intraday_top20\app.py
```

CLI：

```powershell
.\.venv\Scripts\python.exe -m intraday_top20.run_backtest --generate-example --force
.\.venv\Scripts\python.exe -m intraday_top20.run_backtest --robustness
```

测试：

```powershell
.\.venv\Scripts\python.exe -m pytest intraday_top20\tests -q
```

## 本机验收结果（仅合成数据，不代表真实收益）

本机使用确定性合成数据完成了一次默认回测和 33 组串行稳健性扫描。数据覆盖 2025-01-03 至 2025-02-07，共 25 个回测交易日、36 个合成代码和 70,176 根五分钟 K 线；其中包含被证券主表排除的 ETF、权证、优先股和 OTC 示例，以及一个故意制造的停牌/缺失 K 线场景。运行编号为 `437ecd597a12646c`。

默认参数的合成结果为：初始资金 $100,000，最终资产 $80,021.25，总收益 -19.98%，最大回撤 -21.49%，夏普 -8.67，胜率 43.33%，Profit Factor 0.55，共 210 笔已平仓交易。止盈交易占 3.81%，平均持仓 304.74 分钟；最大连续盈利 6 笔、最大连续亏损 9 笔。停牌/缺失 K 线造成 5 次强制隔夜处理和 4 个样本结束时仍未解决的仓位，因此可信度门禁按设计失败。

33 个敏感性场景全部为负。表现最高的是 SMA5（-1.63%，最大回撤 -3.71%，212 笔），成本压力场景为 -27.02%；默认 VWAP 为 -19.98%。最佳 1%、5%、10% 盈利交易分别贡献全部正盈利的 11.15%、35.71%、52.32%；每日平均收益 bootstrap 95% 区间为 -1.48% 至 -0.26%，均值为正的概率为 0.35%。这些数字只能证明引擎、成本模型、异常路径、网页和导出能够处理亏损结果，不能据此判断真实市场中的策略有效性或统计显著性。

自动化验收为模块测试 19 项全部通过；仓库现有 `unittest` 回归 277 项全部通过。完整结果位于被 Git 忽略的 `intraday_top20/outputs/437ecd597a12646c/`，网页会直接加载同一份结果，而不是使用手填指标。

## 网页模块

- 策略概览：数据口径、假设、覆盖范围、可信度和测试摘要。
- 参数与运行：数据、信号、资金、流动性和成本参数；显示进度、当前日期和完整异常。
- 核心绩效：指标卡、资金/回撤、月度热力图、收益分布、滚动夏普/回撤及导出。
- 交易明细：日期、代码、盈亏、止盈、收益、持仓时间、入场时间和排名过滤，分页并导出 CSV/Excel。
- 单笔复盘：五分钟 K 线、均线、Top N、跌破区间、信号、买入、止盈和退出标记，以及状态审计表。
- 每日分析：每个五分钟时刻的 Top N、信号、成交、拒绝原因、收益和持仓变化。
- 稳健性测试：Top N×跌破分钟、止盈×最晚入场网格，以及成本、VWAP/SMA、成交量过滤场景。
- 风险与结论：极端盈利贡献、去掉最佳交易、bootstrap、分月/年/价格表现、数据偏差和模拟盘门禁。

## 缓存、输出与部署

- 清洗后的单日行情按源文件路径、大小和修改时间缓存为 Parquet；参数变化不重复清洗行情。
- 回测结果键包含全部参数、数据指纹和引擎版本；表格保存为压缩 CSV，并附 `manifest.json`、`config.json` 和 `run.log`。
- 网页可下载交易、日收益、资金曲线、拒绝原因、配置、稳健性 CSV、Excel、独立 HTML 和日志；Plotly 工具栏可下载图表 PNG。
- 本地或服务器部署均使用同一 Streamlit 命令。真实历史数据通常很大，应把 `data_dir` 和 `output_root` 指向持久磁盘，不要提交行情、缓存或任何 API 密钥。
- 本应用不需要 Alpaca 凭据。部署时不要复制项目 `.env`，并在反向代理层增加认证；它是研究网页，不是交易服务。

## 可信度门禁和主要风险

只有同时满足“非示例数据、覆盖退市股、存在证券主表、已复权或有拆股表、交易日无缺失、没有强制隔夜或未解决持仓”时，网页才允许把结果标记为可用于策略结论。即使通过，五分钟 OHLCV 仍无法精确模拟盘口队列、逐笔顺序、LULD 停牌、新闻延迟和券商风控；应继续做样本外验证和小规模模拟盘，不能从回测直接进入实盘。
