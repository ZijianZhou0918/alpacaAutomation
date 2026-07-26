# 日线数据重建入口

根目录 `run_backtest_daily_history_rebuild.py` 只负责切换项目虚拟环境并委托这里的同名 runner。
正式 SIP/split 日线库的下载、校验、备份与原子替换由 `backtest/daily_history_rebuild.py` 实现。

旧策略回测、参数优化、数据修复/同步/抽查入口已经移除；新增研究不得继续堆放在真实交易仓库中。
