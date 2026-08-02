# 运行工作流

根目录的 Python 文件只负责启动，实际编排按用途放在这里：

```text
workflows/
├─ monitoring/
│  ├─ auto.py        # 全天时段路由
│  ├─ intraday.py    # 盘中配置和监控
│  ├─ premarket.py   # 盘前仅持仓 60 秒 3% 波动监控
│  └─ afterhours.py  # 盘后提醒监控
├─ watchcode/
│  ├─ intraday.py    # 盘中 WatchCode
│  ├─ premarket.py   # 已停用的盘前 WatchCode 兼容入口
│  ├─ afterhours.py  # 盘后 WatchCode
│  └─ chart.py       # 观察池图表刷新
└─ review/
   └─ launcher.py    # 每日复盘启动
```

调用方向固定为：

```text
根目录薄入口 -> workflows -> 核心 service/strategy/broker
```

内部模块不得反向导入根目录脚本。外部仍可使用原来的文件名和函数名；根目录薄入口会把导入兼容到对应工作流模块。

盘中可编辑参数位于 `monitoring/intraday.py` 顶部，运行入口仍是根目录 `monitor_ma5_forever.py`。
