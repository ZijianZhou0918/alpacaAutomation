from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from alpaca_ma5_service.paths import intraday_monitor_config_path
from alpaca_ma5_service.review_data import (
    ParsedMonitorLog,
    _build_attention,
    _merge_broker_symbols,
    build_daily_review,
    evidence_context,
    parse_monitor_log,
)


HEADER = "代码 | 动作 | 当前价 | 开盘 | MA5 | 开盘MA5 | 信号涨幅 | 当前涨幅 | 买/卖点 | 订单 | 原因"
SEPARATOR = "---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ----"


def round_text(timestamp: str, rows: list[str]) -> str:
    return "\n".join(
        [
            f"[{timestamp} EDT] 开始检查",
            "观察数量：2 | 交易通道：alpaca-live",
            "本轮股票明细：",
            HEADER,
            SEPARATOR,
            *rows,
            "本轮完成：观察 2 | 买入 0 | 卖出 0 | 持有/跳过 2 | 错误 0",
        ]
    )


class ReviewDataTests(TestCase):
    def make_root(self, root: Path) -> Path:
        (root / "outputs" / "logs").mkdir(parents=True)
        (root / "outputs" / "watchlist_charts").mkdir(parents=True)
        config_path = intraday_monitor_config_path(root)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "BUY_STOCK_COUNT = 2\n"
            "BUY_NOTIONAL_USD = 2000.0\n"
            "MA5_MAX_BUY_TODAY_CURRENT_GAIN_PCT = -0.12\n"
            "MA5_BUY_TRIGGER_DISTANCE_PCT = 0.03\n",
            encoding="utf-8",
        )
        return root

    def test_build_review_separates_window_best_all_day_and_latest(self):
        with TemporaryDirectory() as temp:
            root = self.make_root(Path(temp))
            log = "\n".join(
                [
                    round_text(
                        "2026-07-10 11:59:30",
                        [
                            "US.FXHO | 排除 | 16.7250 | 18.0000 | 16.9000 | 16.8000 | 28.78% | -10.56% | 17.0036 | - | 触达动态MA5但跌幅未到 12.00%；动作：今日排除不再买；有效分段买点 17.0036，触发上沿 17.5137",
                            "US.RNAZ | 观察 | 8.7100 | 9.4000 | 8.4780 | 8.5000 | 21.84% | -9.22% | 8.5204 | - | 进入买点区间但跌幅未到 12.00%；触发上沿 8.7760",
                            "US.FRMI | 持有 | 6.0000 | 6.0000 | 5.9000 | 5.9000 | - | 2.00% | 5.4000 | - | 未触发 -10.00% 持仓止损",
                        ],
                    ),
                    round_text(
                        "2026-07-10 15:16:59",
                        [
                            "US.FXHO | 跳过 | 未知 | 未知 | 未知 | 未知 | - | - | 未知 | - | 今日已记录排除",
                            "US.RNAZ | 观察 | 8.4800 | 9.4000 | 8.4400 | 8.5000 | 21.84% | -11.62% | 8.4742 | - | 进入买点区间但跌幅未到 12.00%；触发上沿 8.7284",
                            "US.HAO | 持有 | 0.3525 | 1.6000 | 0.9365 | 1.1380 | - | -67.06% | 0.3367 | - | 未触发 -10.00% 持仓止损",
                        ],
                    ),
                    round_text(
                        "2026-07-10 15:59:59",
                        [
                            "US.FXHO | 跳过 | 未知 | 未知 | 未知 | 未知 | - | - | 未知 | - | 今日已记录排除",
                            "US.RNAZ | 观察 | 8.7100 | 9.4000 | 8.4780 | 8.5000 | 21.84% | -9.22% | 8.5204 | - | 进入买点区间但跌幅未到 12.00%；触发上沿 8.7760",
                            "US.HAO | 持有 | 0.3525 | 1.6000 | 0.9365 | 1.1380 | - | -67.06% | 0.3367 | - | 未触发 -10.00% 持仓止损",
                        ],
                    ),
                ]
            )
            (root / "outputs" / "logs" / "monitor_auto_20260710.out.log").write_text(log, encoding="utf-8")
            (root / "outputs" / "watch_candidates_2026-07-09.csv").write_text(
                "symbol,signal_date,gain_pct\nFXHO,2026-07-09,0.28\nRNAZ,2026-07-09,0.21\n",
                encoding="utf-8",
            )
            (root / "outputs" / "buy_exclusions_2026-07-10.csv").write_text(
                "created_at,symbol,reason\n2026-07-10T11:59:30-04:00,US.FXHO,跌幅未到 12%\n",
                encoding="utf-8",
            )

            review = build_daily_review("2026-07-10", base_dir=root)

            self.assertEqual(review["review_date"], "2026-07-10")
            self.assertFalse(review["market_day"]["is_fallback"])
            self.assertEqual(review["summary"]["rounds"]["intraday"], 3)
            self.assertEqual(review["strategy"]["buy_window_best"]["observed_at"], "2026-07-10T11:59:30-04:00")
            self.assertEqual(review["strategy"]["buy_window_best"]["current_gain_pct"], -0.1056)
            self.assertAlmostEqual(review["strategy"]["buy_window_best"]["drop_gap_pct"], 0.0144)
            self.assertEqual(review["strategy"]["all_day_closest"]["observed_at"], "2026-07-10T15:16:59-04:00")
            rnaz = next(item for item in review["symbols"] if item["symbol"] == "US.RNAZ")
            self.assertEqual(rnaz["bucket"], "window_outside_closest")
            self.assertEqual(rnaz["latest"]["observed_at"], "2026-07-10T15:59:59-04:00")
            for item in review["symbols"]:
                event_ids = [event["id"] for event in item["position_events"]]
                self.assertEqual(event_ids, list(dict.fromkeys(event_ids)))
            self.assertEqual(review["summary"]["local_order_file_state"], "missing")
            self.assertTrue(any(item["code"] == "UNMATCHED_POSITION_CHANGES" for item in review["attention"]))

    def test_missing_or_closed_date_never_reuses_another_days_records(self):
        with TemporaryDirectory() as temp:
            root = self.make_root(Path(temp))
            (root / "outputs" / "logs" / "monitor_auto_20260710.out.log").write_text(
                round_text(
                    "2026-07-10 10:00:00",
                    ["US.OLD | 观察 | 10 | 11 | 9 | 10 | 20% | -9% | 9.9 | - | 旧记录"],
                ),
                encoding="utf-8",
            )
            (root / "outputs" / "watch_candidates_2026-07-10.csv").write_text(
                "symbol,signal_date,gain_pct\nOLD,2026-07-10,0.20\n",
                encoding="utf-8",
            )
            (root / "outputs" / "orders_2026-07-10.csv").write_text(
                "symbol,side,status,quantity\nUS.OLD,BUY,FILLED,1\n",
                encoding="utf-8",
            )

            review = build_daily_review("2026-07-11", base_dir=root)

            self.assertEqual(review["requested_date"], "2026-07-11")
            self.assertEqual(review["review_date"], "2026-07-11")
            self.assertFalse(review["market_day"]["is_fallback"])
            self.assertFalse(review["market_day"]["has_records"])
            self.assertFalse(review["market_day"]["requested_is_trading_day"])
            self.assertIn("不会加载其他日期", review["market_day"]["banner"])
            self.assertEqual(review["symbols"], [])
            self.assertEqual(review["orders"], [])
            self.assertEqual(review["timeline"], [])
            self.assertIsNone(review["chart_url"])
            self.assertFalse(any(source["file"].endswith("2026-07-10.csv") for source in review["sources"]))

    def test_broker_only_activity_defaults_to_manual_without_critical_alarm(self):
        parsed = ParsedMonitorLog(
            observations=(),
            rounds=(),
            phase_round_counts={"premarket": 0, "intraday": 0, "afterhours": 0},
            phase_ranges={},
            premarket_notification_count=0,
            premarket_notification_symbols=(),
            open_buy_pause_rounds=3,
            open_buy_pause_rows=12,
            line_count=20,
        )
        broker_orders = [
            {"symbol": "US.HAO", "side": "BUY", "status": "FILLED", "filled_qty": 100.0},
        ]
        position_events = [
            {"event_type": "added_observed", "symbol": "US.HAO"},
        ]

        attention = _build_attention(
            local_file_state="missing",
            local_orders=[],
            broker={"status": "verified"},
            broker_orders=broker_orders,
            parsed=parsed,
            position_events=position_events,
        )

        self.assertFalse(any(item["severity"] == "critical" for item in attention))
        by_code = {item["code"]: item for item in attention}
        self.assertEqual(by_code["BROKER_ACTIVITY_NOT_IN_LOCAL_LEDGER"]["severity"], "info")
        self.assertEqual(by_code["BROKER_ACTIVITY_NOT_IN_LOCAL_LEDGER"]["facts"]["assumed_origin"], "manual")
        self.assertEqual(by_code["OPEN_BUY_ORDER_WITHOUT_LOCAL_LEDGER"]["severity"], "warning")
        self.assertIn("手动交易", by_code["UNMATCHED_POSITION_CHANGES"]["title"])

    def test_parse_monitor_log_caches_complete_rounds_and_source_lines(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "monitor.log"
            path.write_text(
                round_text(
                    "2026-07-10 09:30:01",
                    [
                        "US.AAAA | 观察 | 10.0 | 11.0 | 9.8 | 9.9 | 20% | -9% | 9.9 | - | 当前价高于触发上沿 9.95；需跌幅 >= 12.00%",
                        "US.BBBB | 观察 | 5.0 | 6.0 | 4.9 | 5.1 | 18% | -8% | 4.9 | - | 当前价高于触发上沿 5.00；需跌幅 >= 12.00%",
                    ],
                ),
                encoding="utf-8",
            )
            first = parse_monitor_log(path)
            second = parse_monitor_log(path)
            self.assertIs(first, second)
            self.assertEqual(first.phase_round_counts["intraday"], 1)
            self.assertEqual(len(first.observations), 2)
            self.assertGreater(first.observations[0].source_line, 0)
            self.assertEqual(first.observations[0].observed_at.isoformat(), "2026-07-10T09:30:01-04:00")

    def test_broker_merge_prioritizes_fills_over_canceled_orders(self):
        symbols = {}
        broker_orders = [
            {"source": "alpaca", "order_id": "buy", "symbol": "US.AAAA", "side": "BUY", "status": "FILLED", "filled_qty": 10.0, "filled_value": 100.0, "submitted_at": "2026-07-10T10:00:00-04:00"},
            {"source": "alpaca", "order_id": "cancel", "symbol": "US.AAAA", "side": "BUY", "status": "CANCELED", "filled_qty": 0.0, "filled_value": 0.0, "submitted_at": "2026-07-10T10:05:00-04:00"},
            {"source": "alpaca", "order_id": "sell", "symbol": "US.AAAA", "side": "SELL", "status": "FILLED", "filled_qty": 10.0, "filled_value": 112.0, "submitted_at": "2026-07-10T15:00:00-04:00"},
        ]
        _merge_broker_symbols(symbols, broker_orders, [], [], "missing")
        item = symbols["US.AAAA"]
        self.assertEqual(item["bucket"], "broker_closed")
        self.assertEqual(item["buy_filled_qty"], 10.0)
        self.assertEqual(item["sell_filled_qty"], 10.0)
        self.assertEqual(item["net_cash_flow"], 12.0)
        self.assertEqual(item["local_ledger_match"], "missing")

    def test_broker_merge_uses_review_day_orders_not_current_positions(self):
        symbols = {}
        broker_orders = [
            {"order_id": "buy", "symbol": "US.AAAA", "side": "BUY", "filled_qty": 10.0, "filled_value": 100.0},
            {"order_id": "sell", "symbol": "US.AAAA", "side": "SELL", "filled_qty": 10.0, "filled_value": 110.0},
        ]
        current_positions = [
            {"symbol": "US.AAAA", "qty": 7.0},
            {"symbol": "US.LATER", "qty": 3.0},
        ]

        _merge_broker_symbols(symbols, broker_orders, current_positions, [], "missing")

        self.assertEqual(symbols["US.AAAA"]["bucket"], "broker_closed")
        self.assertEqual(symbols["US.AAAA"]["current_position_qty"], 7.0)
        self.assertEqual(symbols["US.LATER"]["bucket"], "current_position_context")
        self.assertEqual(symbols["US.LATER"]["current_position_qty"], 3.0)
        self.assertIn("不归入当日买入或卖出", symbols["US.LATER"]["reason"])

    def test_broker_merge_marks_partial_ledger_match(self):
        symbols = {}
        broker_orders = [
            {"order_id": "buy", "symbol": "US.AAAA", "side": "BUY", "filled_qty": 10.0, "filled_value": 100.0},
            {"order_id": "sell", "symbol": "US.AAAA", "side": "SELL", "filled_qty": 10.0, "filled_value": 110.0},
        ]
        local_orders = [{"order_id": "buy"}]

        _merge_broker_symbols(symbols, broker_orders, [], local_orders, "present")

        item = symbols["US.AAAA"]
        self.assertEqual(item["local_ledger_match"], "partial")
        self.assertEqual(item["local_ledger_matched_order_count"], 1)
        self.assertEqual(item["broker_order_id_count"], 2)
        self.assertIn("1/2", item["reason"])

    def test_timeline_uses_runtime_drop_threshold(self):
        with TemporaryDirectory() as temp:
            root = self.make_root(Path(temp))
            intraday_monitor_config_path(root).write_text(
                "BUY_STOCK_COUNT = 2\n"
                "BUY_NOTIONAL_USD = 2000.0\n"
                "MA5_MAX_BUY_TODAY_CURRENT_GAIN_PCT = -0.15\n"
                "MA5_BUY_TRIGGER_DISTANCE_PCT = 0.03\n",
                encoding="utf-8",
            )
            (root / "outputs" / "logs" / "monitor_auto_20260710.out.log").write_text(
                round_text(
                    "2026-07-10 11:59:30",
                    [
                        "US.AAAA | 观察 | 9.0000 | 10.0000 | 8.9000 | 9.0000 | 20.00% | -10.00% | 8.9500 | - | 进入买点区间但跌幅未到 15.00%",
                    ],
                ),
                encoding="utf-8",
            )
            (root / "outputs" / "watch_candidates_2026-07-09.csv").write_text(
                "symbol,signal_date,gain_pct\nAAAA,2026-07-09,0.20\n",
                encoding="utf-8",
            )

            review = build_daily_review("2026-07-10", base_dir=root)

            self.assertEqual(review["strategy"]["required_drop_pct"], 0.15)
            self.assertAlmostEqual(review["strategy"]["buy_window_best"]["drop_gap_pct"], 0.05)
            event = next(item for item in review["timeline"] if item["event_type"] == "window_best")
            self.assertIn("5.00%", event["detail"])

    def test_evidence_context_only_reads_approved_date_source(self):
        with TemporaryDirectory() as temp:
            root = self.make_root(Path(temp))
            path = root / "outputs" / "logs" / "monitor_auto_20260710.out.log"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            result = evidence_context("2026-07-10", "monitor_auto", 2, radius=1, base_dir=root)
            self.assertEqual([item["text"] for item in result["lines"]], ["one", "two", "three"])
            with self.assertRaises(FileNotFoundError):
                evidence_context("2026-07-10", ".env", 1, base_dir=root)
