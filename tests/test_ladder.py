from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from alpaca_ma5_service.ladder import (
    LadderStateStore,
    apply_pending_order_event,
    next_buy_instruction,
    next_sell_instruction,
    prepare_sell_anchor,
    reconcile_plan,
    record_buy_result,
    record_sell_result,
)
from alpaca_ma5_service.models import MarketSnapshot, OrderResult, Position
from alpaca_ma5_service.pending_orders import PendingOrderEvent
from alpaca_ma5_service.service import SymbolTradeCycle, check_sell, execute_sell


NOW = datetime(2026, 7, 24, 10, 0)


def create_plan(store: LadderStateStore, *, target_notional: float = 300.0, anchor: float = 100.0):
    return store.create(
        "US.TEST",
        NOW.date(),
        target_notional,
        anchor,
        (0.0, -0.01, -0.02),
        (0.0, 0.01, 0.02),
        NOW,
    )


def filled(side: str, quantity: float, price: float) -> OrderResult:
    return OrderResult("order-1", "US.TEST", side, quantity, price, "FILLED", "filled")


class LadderStateTests(unittest.TestCase):
    def test_pending_buy_late_fill_is_applied_cumulatively_once(self):
        with TemporaryDirectory() as tmp:
            plan = create_plan(LadderStateStore(Path(tmp)), anchor=10.0)
            partial = PendingOrderEvent(
                "buy-order-1",
                "buy-order-1",
                "US.TEST",
                "BUY",
                10.0,
                10.0,
                "test",
                "buy_leg_0",
                100.0,
                "PARTIALLY_FILLED_CANCEL_REQUESTED",
                4.0,
                9.9,
                False,
            )
            final = PendingOrderEvent(
                **{
                    **partial.__dict__,
                    "status": "FILLED",
                    "filled_quantity": 10.0,
                    "filled_avg_price": 9.8,
                    "terminal": True,
                }
            )

            self.assertTrue(apply_pending_order_event(plan, partial, NOW))
            self.assertTrue(apply_pending_order_event(plan, final, NOW + timedelta(seconds=10)))
            self.assertFalse(apply_pending_order_event(plan, final, NOW + timedelta(seconds=20)))

            self.assertEqual(plan.filled_quantity, 10.0)
            self.assertAlmostEqual(plan.filled_notional, 98.0)
            self.assertTrue(plan.buy_leg_filled[0])
            second = next_buy_instruction(plan, 9.9)
            self.assertEqual(second.action, "buy_leg_1")

    def test_pending_sell_late_fill_does_not_oversell_first_leg(self):
        with TemporaryDirectory() as tmp:
            plan = create_plan(LadderStateStore(Path(tmp)))
            plan.buy_closed = True
            plan.filled_quantity = 12.0
            plan.filled_notional = 1_200.0
            position = Position("US.TEST", 12.0, 100.0, NOW.isoformat(), source="fake")
            prepare_sell_anchor(
                plan,
                position,
                110.0,
                NOW,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
            )
            partial = PendingOrderEvent(
                "sell-order-1",
                "sell-order-1",
                "US.TEST",
                "SELL",
                2.0,
                110.0,
                "test",
                "sell_leg_0",
                0.0,
                "PARTIALLY_FILLED_CANCEL_REQUESTED",
                1.0,
                110.0,
                False,
            )
            final = PendingOrderEvent(
                **{
                    **partial.__dict__,
                    "status": "FILLED",
                    "filled_quantity": 2.0,
                    "terminal": True,
                }
            )

            apply_pending_order_event(plan, partial, NOW)
            apply_pending_order_event(plan, final, NOW + timedelta(seconds=10))
            self.assertEqual(plan.sell_leg_filled_quantity[0], 2.0)
            self.assertEqual(plan.sell_stage, 1)

            next_leg = next_sell_instruction(
                plan,
                Position("US.TEST", 10.0, 100.0, NOW.isoformat()),
                111.1,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            self.assertEqual((next_leg.action, next_leg.quantity), ("sell_leg_1", 2.0))

    def test_buy_three_equal_budgets_and_recover_remaining_at_anchor(self):
        with TemporaryDirectory() as tmp:
            store = LadderStateStore(Path(tmp))
            plan = create_plan(store)

            first = next_buy_instruction(plan, 100.0)
            self.assertEqual((first.limit_price, first.notional_usd, first.counts_daily_slot), (100.0, 100.0, True))
            record_buy_result(plan, first, filled("BUY", 1.0, 100.0), NOW)

            second = next_buy_instruction(plan, 99.0)
            self.assertEqual((second.limit_price, second.notional_usd, second.counts_daily_slot), (99.0, 100.0, False))
            record_buy_result(plan, second, filled("BUY", 1.0, 99.0), NOW)

            recovery = next_buy_instruction(plan, 100.0)
            self.assertEqual(recovery.action, "buy_recovery_anchor")
            self.assertEqual(recovery.limit_price, 100.0)
            self.assertEqual(recovery.notional_usd, 101.0)
            record_buy_result(plan, recovery, filled("BUY", 1.0, 100.0), NOW)

            self.assertTrue(plan.buy_closed)
            self.assertEqual(plan.buy_leg_filled, [True, True, True])

    def test_partial_buy_retries_only_unfilled_part_and_does_not_count_new_slot(self):
        with TemporaryDirectory() as tmp:
            plan = create_plan(LadderStateStore(Path(tmp)), anchor=10.0)

            first = next_buy_instruction(plan, 10.0)
            record_buy_result(plan, first, filled("BUY", 4.0, 10.0), NOW)
            retry = next_buy_instruction(plan, 10.0)

            self.assertFalse(plan.buy_leg_filled[0])
            self.assertEqual(retry.action, "buy_leg_0")
            self.assertEqual(retry.notional_usd, 60.0)
            self.assertFalse(retry.counts_daily_slot)

    def test_sell_ladder_splits_only_first_half_then_keeps_remainder(self):
        with TemporaryDirectory() as tmp:
            plan = create_plan(LadderStateStore(Path(tmp)))
            plan.buy_closed = True
            plan.filled_quantity = 12.0
            plan.filled_notional = 1_200.0
            position = Position("US.TEST", 12.0, 100.0, NOW.isoformat(), source="fake")

            self.assertFalse(
                prepare_sell_anchor(
                    plan,
                    position,
                    109.99,
                    NOW,
                    take_profit_half_pct=0.10,
                    take_profit_sell_fraction=0.50,
                )
            )
            self.assertTrue(
                prepare_sell_anchor(
                    plan,
                    position,
                    110.0,
                    NOW,
                    take_profit_half_pct=0.10,
                    take_profit_sell_fraction=0.50,
                )
            )
            self.assertEqual(plan.take_profit_target_quantity, 6.0)

            first = next_sell_instruction(
                plan,
                position,
                110.0,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            self.assertEqual((first.action, first.quantity), ("sell_leg_0", 2.0))
            self.assertEqual(first.to_signal("US.TEST", 110.0).action, "SELL_HALF")
            record_sell_result(plan, first, filled("SELL", 2.0, 110.0), NOW)

            second = next_sell_instruction(
                plan,
                Position("US.TEST", 10.0, 100.0, NOW.isoformat()),
                111.1,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            record_sell_result(plan, second, filled("SELL", 1.0, 111.1), NOW)
            retry = next_sell_instruction(
                plan,
                Position("US.TEST", 9.0, 100.0, NOW.isoformat()),
                111.1,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            self.assertEqual((plan.sell_stage, retry.action, retry.quantity), (1, "sell_leg_1", 1.0))
            record_sell_result(plan, retry, filled("SELL", 1.0, 111.1), NOW)

            fallback = next_sell_instruction(
                plan,
                Position("US.TEST", 8.0, 100.0, NOW.isoformat()),
                110.0,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            self.assertEqual((fallback.action, fallback.quantity), ("take_profit_fallback", 2.0))
            record_sell_result(plan, fallback, filled("SELL", 2.0, 110.0), NOW)
            self.assertEqual((plan.sell_stage, plan.status), (3, "active"))

            hold = next_sell_instruction(
                plan,
                Position("US.TEST", 6.0, 100.0, NOW.isoformat()),
                120.0,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            self.assertIsNone(hold)

    def test_absolute_stop_and_tail_close_preempt_regular_ladder(self):
        with TemporaryDirectory() as tmp:
            plan = create_plan(LadderStateStore(Path(tmp)))
            position = Position("US.TEST", 7.0, 100.0, NOW.isoformat(), source="fake")

            stop = next_sell_instruction(
                plan,
                position,
                90.0,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            tail = next_sell_instruction(
                plan,
                position,
                99.0,
                NOW.replace(hour=15, minute=55),
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )

            self.assertEqual((stop.action, stop.quantity), ("absolute_stop_market", 7.0))
            self.assertEqual((tail.action, tail.quantity), ("close_liquidation", 7.0))

    def test_take_profit_and_stop_use_weighted_average_cost_not_highest_buy_anchor(self):
        with TemporaryDirectory() as tmp:
            plan = create_plan(LadderStateStore(Path(tmp)), anchor=110.0)
            plan.buy_closed = True
            plan.filled_quantity = 12.0
            plan.filled_notional = 1_200.0
            position = Position("US.TEST", 12.0, 100.0, NOW.isoformat(), source="fake")

            no_stop = next_sell_instruction(
                plan,
                position,
                98.0,
                NOW,
                absolute_stop_loss_pct=-0.10,
                take_profit_half_pct=0.10,
                take_profit_sell_fraction=0.50,
                close_start=time(15, 55),
                close_end=time(16, 0),
            )
            self.assertIsNone(no_stop)
            self.assertTrue(
                prepare_sell_anchor(
                    plan,
                    position,
                    110.0,
                    NOW,
                    take_profit_half_pct=0.10,
                    take_profit_sell_fraction=0.50,
                )
            )
            self.assertEqual(plan.sell_anchor_price, 110.0)

    def test_missing_position_uses_grace_before_closing_newly_filled_plan(self):
        with TemporaryDirectory() as tmp:
            plan = create_plan(LadderStateStore(Path(tmp)))
            plan.filled_quantity = 1.0
            plan.filled_notional = 100.0

            self.assertFalse(reconcile_plan(plan, None, NOW + timedelta(seconds=119)))
            self.assertEqual(plan.status, "active")
            self.assertTrue(reconcile_plan(plan, None, NOW + timedelta(seconds=120)))
            self.assertEqual(plan.status, "closed")

    def test_store_round_trip_and_corrupt_state_fail_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LadderStateStore(root)
            plan = create_plan(store)
            plan.take_profit_target_quantity = 6.0
            plan.sell_leg_filled_quantity[0] = 2.0
            store.save(NOW)

            loaded = LadderStateStore(root).get("US.TEST")
            self.assertEqual(loaded.sell_leg_filled_quantity, [2.0, 0.0, 0.0])
            (root / "ladder_state.json").write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "无法读取分档状态"):
                LadderStateStore(root)

    def test_version_two_state_loads_with_empty_order_cursors_and_upgrades_on_save(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LadderStateStore(root)
            create_plan(store)
            state_path = root / "ladder_state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["version"] = 2
            payload["plans"]["US.TEST"].pop("applied_order_filled_quantity")
            payload["plans"]["US.TEST"].pop("applied_order_filled_value")
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            migrated = LadderStateStore(root)
            plan = migrated.get("US.TEST")
            self.assertEqual(plan.applied_order_filled_quantity, {})
            self.assertEqual(plan.applied_order_filled_value, {})
            migrated.save(NOW)
            self.assertFalse(plan.broker_stop_enabled)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["version"], 4)


class RecordingMarketSellBroker:
    def __init__(self):
        self.calls = []

    def place_market_sell(self, symbol, quantity, current_price, reason):
        self.calls.append((symbol, quantity, current_price, reason))
        return filled("SELL", quantity, current_price)


class LadderServiceTests(unittest.TestCase):
    def test_service_absolute_stop_uses_market_and_persists_stopped_state(self):
        with TemporaryDirectory() as tmp:
            store = LadderStateStore(Path(tmp))
            plan = create_plan(store)
            plan.filled_quantity = 9.0
            plan.filled_notional = 900.0
            store.save(NOW)
            position = Position("US.TEST", 9.0, 100.0, NOW.isoformat(), source="fake")
            snapshot = MarketSnapshot("US.TEST", 90.0, [], NOW, "fake")
            broker = RecordingMarketSellBroker()
            trading_round = SimpleNamespace(
                open_buy_order_symbols=set(),
                open_sell_order_symbols=set(),
                ladder_store=store,
                now_et=NOW,
                market_data=SimpleNamespace(get_snapshot=lambda _symbol: snapshot),
                settings=SimpleNamespace(
                    absolute_stop_loss_pct=-0.10,
                    take_profit_half_pct=0.10,
                    take_profit_sell_fraction=0.50,
                    close_liquidation_start=time(15, 55),
                    close_liquidation_end=time(16, 0),
                ),
                can_order_now=True,
                broker=broker,
                buying_paused=False,
            )
            cycle = SymbolTradeCycle("US.TEST", position=position)

            check_sell(trading_round, cycle)
            execute_sell(trading_round, cycle)

            self.assertEqual(len(broker.calls), 1)
            self.assertEqual(broker.calls[0][1:3], (9.0, 90.0))
            self.assertEqual(cycle.signal.diagnostics["sell_rule"], "absolute_stop_market")
            self.assertEqual(LadderStateStore(Path(tmp)).get("US.TEST").status, "stopped")

    def test_service_does_not_sell_while_same_symbol_buy_is_still_open(self):
        with TemporaryDirectory() as tmp:
            store = LadderStateStore(Path(tmp))
            plan = create_plan(store)
            plan.filled_quantity = 9.0
            plan.filled_notional = 900.0
            store.save(NOW)
            position = Position("US.TEST", 9.0, 100.0, NOW.isoformat(), source="fake")
            snapshot = MarketSnapshot("US.TEST", 90.0, [], NOW, "fake")
            broker = RecordingMarketSellBroker()
            trading_round = SimpleNamespace(
                open_buy_order_symbols={"US.TEST"},
                open_sell_order_symbols=set(),
                ladder_store=store,
                now_et=NOW,
                market_data=SimpleNamespace(get_snapshot=lambda _symbol: snapshot),
                settings=SimpleNamespace(
                    absolute_stop_loss_pct=-0.10,
                    take_profit_half_pct=0.10,
                    take_profit_sell_fraction=0.50,
                    close_liquidation_start=time(15, 55),
                    close_liquidation_end=time(16, 0),
                ),
                can_order_now=True,
                broker=broker,
                buying_paused=False,
            )
            cycle = SymbolTradeCycle("US.TEST", position=position)

            check_sell(trading_round, cycle)
            execute_sell(trading_round, cycle)

            self.assertEqual(broker.calls, [])
            self.assertIn("开放买单", cycle.row.reason)


if __name__ == "__main__":
    unittest.main()
