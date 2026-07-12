# MA5 每日复盘前后端契约

后端公开函数：

```python
list_review_dates(base_dir: Path | None = None) -> list[str]
build_daily_review(requested_date=None, *, include_broker=False, base_dir=None) -> dict
evidence_context(review_date, source_id, line, *, radius=3, base_dir=None) -> dict
```

HTTP：

- `GET /api/review/health`
- `GET /api/review/dates`
- `GET /api/review?date=YYYY-MM-DD&broker=0|1`
- `GET /api/review/evidence?date=YYYY-MM-DD&source=<id>&line=<n>`
- `GET /charts/<approved html filename>`

`build_daily_review()` 返回以下稳定顶层字段：

```json
{
  "schema_version": "1.0",
  "requested_date": "2026-07-11",
  "review_date": "2026-07-10",
  "generated_at": "ISO-8601",
  "market_day": {
    "is_trading_day": true,
    "is_fallback": true,
    "banner": "今日休市，展示最近交易日 2026-07-10",
    "previous_date": "...",
    "next_date": null
  },
  "headline": {"title": "...", "detail": "..."},
  "quality": {
    "status": "healthy|warning|critical",
    "broker_status": "not_requested|loading|verified|unavailable",
    "warnings": []
  },
  "summary": {
    "watch_counts": {"premarket": 0, "intraday": 0, "afterhours": 0},
    "rounds": {"premarket": 0, "intraday": 0, "afterhours": 0},
    "broker_order_count": 0,
    "broker_bought_symbols": 0,
    "broker_closed_symbols": 0,
    "broker_unfilled_buy_symbols": 0,
    "current_positions": 0,
    "local_order_file_state": "missing|empty|present",
    "local_order_count": null,
    "excluded_count": 0,
    "net_cash_flow": null
  },
  "strategy": {},
  "phases": [],
  "funnel": {},
  "reason_distribution": [],
  "attention": [],
  "symbols": [],
  "orders": [],
  "position_events": [],
  "timeline": [],
  "sources": [],
  "chart_url": null,
  "broker": {"mode": null, "synced_at": null, "positions": []}
}
```

每个 `symbols[]` 至少包含：

```json
{
  "symbol": "US.HAO",
  "ticker": "HAO",
  "source_labels": ["Alpaca", "监控日志"],
  "bucket": "broker_closed|broker_bought|buy_unfilled|excluded|not_bought|window_outside_closest|position_unreconciled|current_position_context",
  "status_label": "券商已买已卖",
  "severity": "neutral|success|warning|critical",
  "reason_code": "...",
  "reason": "...",
  "buy_window_best": null,
  "all_day_closest": null,
  "latest": null,
  "orders": [],
  "position_events": [],
  "buy_filled_qty": 0,
  "buy_avg_price": null,
  "sell_filled_qty": 0,
  "sell_avg_price": null,
  "net_cash_flow": null,
  "current_position_qty": 0,
  "local_ledger_match": "matched|partial|missing|unmatched|not_applicable",
  "local_ledger_matched_order_count": 0,
  "broker_order_id_count": 0,
  "evidence": []
}
```

前端要求：先请求 `broker=0` 秒开本地结果，再自动请求 `broker=1` 进行只读 Alpaca 核对并整体替换数据；券商失败不能阻塞本地复盘。
