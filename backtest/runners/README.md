# 日线数据重建入口

根目录 `run_backtest_daily_history_rebuild.py` 只负责切换项目虚拟环境并委托这里的同名 runner。
正式 SIP/split 日线库的下载、校验、备份与原子替换由 `backtest/daily_history_rebuild.py` 实现。

经过明确授权的独立日线研究入口：

- `run_backtest_kdj_volume_reversal.py`：只读正式日线库的 KDJ(81,3,3) 极端量价反转策略；信号在收盘后确认，下一交易日开盘成交，不连接券商、不写 WatchCode。

其他旧策略回测、参数优化、数据修复/同步/抽查入口已经移除；新增研究必须与真实交易链路隔离。
