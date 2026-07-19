# WatchCode 运行数据

这里集中保存三个时段的观察池：

- `watch_codes.txt`：盘中自动交易候选。
- `watch_codes_premarket.txt`：盘前推荐候选，只提醒。
- `watch_code_afterhours.txt`：盘后候选，是否可下单由具体入口决定。

根目录的 `watchcode_*.py` 仍是公开运行入口。生成器会直接更新本目录中的对应文件，不再把运行数据散落在项目根目录。
