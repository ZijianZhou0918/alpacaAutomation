# 交易复盘看板设计调研

调研日期：2026-07-11。

## 主要参考

- TradeZella Daily Journal：以单日为核心，交易按时间排列，并在同一页保留当天摘要、日记与详情。
  - https://help.tradezella.com/en/articles/5829446-understanding-the-daily-journal-page
- TradeZella Trade Page：列表与右侧详情并存，支持在不离开当天页面的情况下深入单笔交易。
  - https://help.tradezella.com/en/articles/5860216-understanding-the-trade-page
- Tradervue Overview / Interactive Reports：交易日历区分无交易与无数据；点击指标后联动过滤原始交易。
  - https://app.tradervue.com/help/reports_overview
  - https://app.tradervue.com/help/interactive_reports
- Edgewonk：把错过的交易、规则执行与复盘检查清单纳入交易日志，而不是只看盈亏。
  - https://edgewonk.com/
- OpenBB Workspace：以共享日期和 ticker 参数联动表格、图表、笔记及数据来源。
  - https://docs.openbb.co/workspace
- shadcn/ui Dashboard Blocks 与 Tremor Blocks：响应式侧栏、指标、筛选、状态监控和高密度表格的实现骨架。
  - https://ui.shadcn.com/blocks?category=dashboard
  - https://blocks.tremor.so/blocks
- IBM Carbon Dashboard / Status：左上优先展示最高价值信息，联动视图；状态不能只依赖颜色。
  - https://carbondesignsystem.com/data-visualization/dashboards/
  - https://carbondesignsystem.com/patterns/status-indicator-pattern/
- Grafana Logs：证据时间线需要可搜索、可展开、能看到上下文和原始字段。
  - https://grafana.com/docs/grafana/latest/visualizations/explore/logs-integration/

## 落地取舍

- 采用 TradeZella 的“当天入口 + 页内下钻”。
- 采用 OpenBB 的全局日期联动和来源元数据。
- 采用 Tradervue 的点击指标联动筛选。
- 采用 Grafana 的审计型证据时间线。
- 采用 Carbon 的状态文字/图形双编码与优先级。
- 只借鉴 shadcn/Tremor 的组件密度，不引入 Node 构建链或外部依赖。
- 因本仓库存在本地账本与券商事实不一致，最终视觉把“数据冲突”放在利润、策略和行情之前。

