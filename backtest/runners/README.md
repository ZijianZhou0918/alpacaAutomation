# 回测运行编排

这里保存根目录 `run_backtest*.py` 背后的命令实现。根目录文件只负责切换项目虚拟环境并调用对应 `main()`，历史筛选、回放和数据处理仍由 `backtest/` 中的业务模块完成。

```text
根目录 run_backtest*.py
    -> backtest/runners/run_backtest*.py
    -> backtest/engine.py、data_*.py、signal_dynamic_ma5.py
```

`run_backtest_gap_strategy_optimization.py` 是 2025 gap pullback 的受限研究入口：
`baseline/stage1/stage2` 只能读取 2025-01-01..09-30，`holdout` 只能读取
2025-Q4，`robustness` 只能在参数冻结和留出完成后读取全年。入口对 2026
硬失败，SIP 分钟线只写独立缓存。`run_backtest_gap_strategy_validation.py`
从已保存的逐笔成交重算置信区间、多重试验校正、Markdown 报告和 notebook。

总收益研究复用同一入口的 `return_signal/return_sizing/return_holdout/
return_robustness` 四个阶段。主目标是固定 `$100,000`、无杠杆、每次成交
`10bp` 滑点后的组合收益；候选冻结后才读取 Q4。结果写入
`backtest/output/gap_strategy_return_optimization/`，并由
`run_backtest_gap_strategy_return_validation.py` 重算总收益 bootstrap、
总收益 PBO、成本压力、Markdown 和 notebook。该候选 Q4 绝对收益为负，
因此报告入口不会把它写入 Live profile。

`run_backtest_gap_strategy_current_daily3_optimization.py` 研究当前代码在
`$3,500` 单笔、每日最多 3 笔/3 个持仓和每次成交 `10bp` 成本下的受限
改进。`development` 只能在 2025-01-01..09-30 选择并写冻结清单；
`diagnostic` 读取 2025-Q4 但不得重调；`validate-2026` 只验证清单中的
唯一冻结候选，并在已有结果时拒绝重跑。结果位于
`backtest/output/gap_strategy_current_daily3_optimization/`。
