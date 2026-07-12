from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alpaca_ma5_service.config import build_settings
from alpaca_ma5_service.daily_report import build_daily_monitor_report, send_daily_monitor_report


class DailyMonitorReportTests(TestCase):
    def make_settings(self, root: Path):
        settings = build_settings(trade_notify_mode="cloud")
        output_dir = root / "outputs"
        return replace(
            settings,
            watch_codes_file=root / "watch_codes.txt",
            output_dir=output_dir,
            state_file=output_dir / "state.json",
            trade_notify_openclaw_enabled=True,
            cloud_notify_webhook_url="https://example.test/webhook",
            cloud_notify_webhook_secret="secret",
        )

    def test_daily_report_summarizes_no_buy_and_closest_intraday_symbol(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.output_dir.mkdir(parents=True)
            (settings.output_dir / "logs").mkdir()
            settings.watch_codes_file.write_text("US.RNAZ\nUS.RYOJ\n", encoding="utf-8")
            settings.watch_codes_file.with_name("watch_codes_premarket.txt").write_text("US.AAAA\n", encoding="utf-8")
            settings.watch_codes_file.with_name("watch_code_afterhours.txt").write_text("US.HAO\n", encoding="utf-8")
            (settings.output_dir / "afterhours_candidates_2026-05-28.csv").write_text(
                "symbol,signal_date,regular_open,regular_high,regular_low,regular_close,range_ratio,buy_limit,target_sell_price\n"
                "HAO,2026-05-28,1,3,1,2,3,1.6,1.76\n",
                encoding="utf-8",
            )
            log = "\n".join(
                [
                    "Trade notify (cloud) sent: premarket MA5 recommendation US.AAAA",
                    "本轮股票明细：",
                    "代码 | 动作 | 当前价 | 开盘 | MA5 | 开盘MA5 | 信号涨幅 | 当前涨幅 | 买/卖点 | 订单 | 原因",
                    "---- | ---- | ------ | ---- | --- | ------- | -------- | -------- | ------- | ---- | ----",
                    "US.RNAZ | 排除 | 8.4800 | 9.0000 | 8.4000 | 8.3000 | 55.00% | -11.62% | 8.4742 | - | 触达动态MA5但跌幅未到 12.00%；动作：今日排除不再买；有效分段买点 8.4742，触发上沿 8.7284；当前价 8.4800，当前涨跌 -11.62%",
                    "US.RYOJ | 观察 | 3.4601 | 4.0000 | 3.3000 | 3.2000 | 40.00% | -10.82% | 3.1000 | - | 当前价高于触发上沿 3.2000；动作：观察不买。需跌幅 >= 12.00%",
                    "本轮完成：观察 2 | 买入 0 | 卖出 0 | 持有/跳过 2 | 错误 0",
                    "股票      | 当前价   | 来源        | 收盘     | 跌幅    | 提醒线   | 参考价   | 状态       | 说明",
                    "----------+----------+-------------+----------+---------+----------+----------+------------+-----------------------------",
                    "HAO       | 1.8000   | alpaca      | 2.0000   | 10.00%  | 1.7000   | 1.6000   | 等待信号   | 未超过 15%; 提醒价<=1.7000",
                ]
            )
            (settings.output_dir / "logs" / "monitor_auto_20260528.out.log").write_text(log, encoding="utf-8")

            report = build_daily_monitor_report(
                settings,
                date(2026, 5, 28),
                now_et=datetime(2026, 5, 28, 20, 1, tzinfo=ZoneInfo("America/New_York")),
            )

            self.assertIn("【MA5 每日复盘】2026-05-28", report)
            self.assertIn("| 时段 | 模式 | 数量 | 结果 | 重点 |", report)
            self.assertIn("盘中未买原因", report)
            self.assertIn("US.RNAZ / 当前涨跌 -11.62%", report)
            self.assertIn("还差 0.38% 才到 12.00% 买入跌幅", report)
            self.assertIn("HAO / 盘后跌幅 10.00%", report)
            self.assertIn("不下单，只发云端提醒", report)

    def test_daily_report_sends_only_once_per_day(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.output_dir.mkdir(parents=True)
            now_et = datetime(2026, 5, 28, 20, 1, tzinfo=ZoneInfo("America/New_York"))

            with patch("alpaca_ma5_service.daily_report.safe_send_openclaw_messages", return_value=True) as fake_send:
                self.assertTrue(send_daily_monitor_report(settings, now_et=now_et))
                self.assertFalse(send_daily_monitor_report(settings, now_et=now_et))

            self.assertEqual(fake_send.call_count, 1)
