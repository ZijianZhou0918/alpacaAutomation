# 回测运行编排

这里保存根目录 `run_backtest*.py` 背后的命令实现。根目录文件只负责切换项目虚拟环境并调用对应 `main()`，历史筛选、回放和数据处理仍由 `backtest/` 中的业务模块完成。

```text
根目录 run_backtest*.py
    -> backtest/runners/run_backtest*.py
    -> backtest/engine.py、data_*.py、signal_dynamic_ma5.py
```
