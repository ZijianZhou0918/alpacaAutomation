from __future__ import annotations

import unittest

from alpaca_ma5_service.models import OrderResult
from alpaca_ma5_service.trade_notifications import (
    render_order_submitted_message,
    render_trade_order_messages,
)


class TradeNotificationFormatTests(unittest.TestCase):
    def test_buy_submission_has_fixed_agent_friendly_field_order(self):
        result = OrderResult("buy-order-1", "US.AAPL", "BUY", 2, 123.45, "ACCEPTED", "accepted")

        message = render_order_submitted_message(result, "首档限价买入", broker_name="alpaca-live")

        self.assertEqual(
            message,
            "\n".join(
                [
                    "【买单｜等待成交】",
                    "股票：US.AAPL",
                    "账户：Alpaca LIVE（真实账户）",
                    "状态：🟡 已提交，等待成交（ACCEPTED）",
                    "",
                    "订单信息",
                    "- 方向：买入（BUY）",
                    "- 数量：2 股",
                    "- 价格：$123.4500",
                    "- 估算金额：约 $246.90",
                    "- 订单号：buy-order-1",
                    "",
                    "策略原因",
                    "- 首档限价买入",
                    "",
                    "下一步",
                    "- 订单已提交至 Alpaca，但尚未证明成交；最终成交、撤单或拒单状态会另行通知。",
                ]
            ),
        )

    def test_sell_fill_puts_result_before_long_explanation(self):
        result = OrderResult("sell-order-1", "US.TSLA", "SELL", 1.5, 250, "FILLED", "filled at broker")

        message = render_trade_order_messages(result, "绝对止损卖出", broker_name="alpaca-paper")[0]

        self.assertTrue(message.startswith("【卖单｜已成交】\n股票：US.TSLA"))
        self.assertIn("账户：Alpaca PAPER（模拟账户）", message)
        self.assertIn("状态：✅ 已成交（FILLED）", message)
        self.assertIn("- 数量：1.5 股", message)
        self.assertIn("- 估算金额：约 $375.00", message)
        self.assertLess(message.index("订单信息"), message.index("策略原因"))
        self.assertLess(message.index("策略原因"), message.index("执行结果"))
        self.assertLess(message.index("执行结果"), message.index("下一步"))

    def test_partial_fill_distinguishes_open_and_terminal_remainder(self):
        open_result = OrderResult("order-1", "US.AAPL", "BUY", 1, 100, "PARTIALLY_FILLED", "one filled")
        done_result = OrderResult(
            "order-2",
            "US.AAPL",
            "BUY",
            1,
            100,
            "PARTIALLY_FILLED_CANCELED",
            "remainder canceled",
        )

        open_message = render_trade_order_messages(open_result, "补档", broker_name="alpaca-live")[0]
        done_message = render_trade_order_messages(done_result, "补档", broker_name="alpaca-live")[0]

        self.assertIn("【买单｜部分成交｜余单待确认】", open_message)
        self.assertIn("未成交余量仍待确认", open_message)
        self.assertIn("【买单｜部分成交｜余单已结束】", done_message)
        self.assertIn("未成交余量已经结束", done_message)

    def test_unknown_submit_and_dry_run_are_not_presented_as_real_fills(self):
        unknown = OrderResult("order-1", "US.AAPL", "BUY", 1, 100, "SUBMIT_UNCONFIRMED", "timeout")
        dry_run = OrderResult("order-2", "US.AAPL", "SELL", 1, 100, "DRY_RUN", "simulation")

        unknown_message = render_trade_order_messages(unknown, "首档", broker_name="alpaca-live")[0]
        dry_run_message = render_trade_order_messages(dry_run, "测试", broker_name="dry-run")[0]

        self.assertIn("【买单｜提交状态未知】", unknown_message)
        self.assertIn("不要重试下单", unknown_message)
        self.assertIn("【卖单｜模拟完成】", dry_run_message)
        self.assertIn("DryRun（本地模拟，不下单）", dry_run_message)
        self.assertIn("没有向 Paper 或 Live 账户提交订单", dry_run_message)

    def test_rejection_labels_execution_message_as_failure_reason(self):
        result = OrderResult("", "US.AAPL", "BUY", 1, 100, "REJECTED", "buying power 不足")

        message = render_trade_order_messages(result, "手动限价买入", broker_name="alpaca-live")[0]

        self.assertIn("【买单｜下单失败】", message)
        self.assertIn("状态：❌ 已拒绝，未建立新订单（REJECTED）", message)
        self.assertIn("失败原因\n- buying power 不足", message)
        self.assertNotIn("执行结果\n- buying power 不足", message)

    def test_pending_cancel_and_canceled_have_distinct_titles(self):
        pending = OrderResult("order-1", "US.AAPL", "SELL", 1, 100, "CANCEL_REQUESTED", "requested")
        canceled = OrderResult("order-1", "US.AAPL", "SELL", 1, 100, "CANCELED", "canceled")

        pending_message = render_trade_order_messages(pending, "撤销卖单", broker_name="alpaca-live")[0]
        canceled_message = render_trade_order_messages(canceled, "撤销卖单", broker_name="alpaca-live")[0]

        self.assertIn("【卖单｜撤单处理中】", pending_message)
        self.assertIn("不要重复撤单或反向下单", pending_message)
        self.assertIn("【卖单｜已取消】", canceled_message)
        self.assertIn("当前没有继续等待的该笔挂单", canceled_message)


if __name__ == "__main__":
    unittest.main()
