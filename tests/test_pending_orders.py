from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alpaca_ma5_service.broker import (
    BROKER_PROTECTIVE_STOP_ACTION,
    AlpacaStockBroker,
    normalize_limit_price,
)
from alpaca_ma5_service.ladder import LadderStateStore
from alpaca_ma5_service.models import MarketSnapshot, Position, Signal
from alpaca_ma5_service.pending_orders import PendingOrderEvent, PendingOrderStore
from alpaca_ma5_service.service import SymbolTradeCycle, execute_sell, reconcile_managed_pending_orders


NOW = datetime.fromisoformat("2026-07-24T10:00:00-04:00")


class ManagedOrderClient:
    def __init__(self):
        self.status = "accepted"
        self.filled_qty = "0"
        self.filled_avg_price = None
        self.get_calls = 0
        self.cancel_calls = 0
        self.order_data = None

    def submit_order(self, order_data):
        self.order_data = order_data
        return self._raw()

    def get_order_by_id(self, order_id):
        self.get_calls += 1
        return self._raw(order_id)

    def cancel_order_by_id(self, order_id):
        self.cancel_calls += 1
        self.status = "canceled"

    def _raw(self, order_id="managed-order-1"):
        return SimpleNamespace(
            id=order_id,
            status=self.status,
            qty=str(getattr(self.order_data, "qty", 10) or 10),
            filled_qty=self.filled_qty,
            filled_avg_price=self.filled_avg_price,
            replaced_by=None,
        )


class ProtectiveOrderClient:
    def __init__(self, *, cancel_immediately=True):
        self.orders = {}
        self.submit_calls = 0
        self.replace_calls = 0
        self.cancel_calls = 0
        self.cancel_immediately = cancel_immediately
        self.last_submit_request = None
        self.last_replace_request = None

    def submit_order(self, order_data):
        self.submit_calls += 1
        self.last_submit_request = order_data
        order_id = f"stop-{self.submit_calls}"
        raw = self._raw(
            order_id,
            qty=order_data.qty,
            stop_price=order_data.stop_price,
            client_order_id=order_data.client_order_id,
        )
        self.orders[order_id] = raw
        return raw

    def get_order_by_id(self, order_id):
        return self.orders[order_id]

    def get_orders(self, filter=None):
        return [
            order
            for order in self.orders.values()
            if str(order.status).upper() not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "REPLACED"}
        ]

    def replace_order_by_id(self, order_id, order_data):
        self.replace_calls += 1
        self.last_replace_request = order_data
        old = self.orders[order_id]
        replacement_id = f"replacement-{self.replace_calls}"
        replacement = self._raw(
            replacement_id,
            qty=order_data.qty,
            stop_price=order_data.stop_price,
            client_order_id=order_data.client_order_id,
        )
        old.status = "replaced"
        old.replaced_by = replacement_id
        self.orders[replacement_id] = replacement
        return replacement

    def cancel_order_by_id(self, order_id):
        self.cancel_calls += 1
        self.orders[order_id].status = "canceled" if self.cancel_immediately else "pending_cancel"

    @staticmethod
    def _raw(order_id, *, qty, stop_price, client_order_id):
        return SimpleNamespace(
            id=order_id,
            symbol="TEST",
            side="sell",
            status="accepted",
            qty=str(qty),
            stop_price=str(stop_price),
            client_order_id=client_order_id,
            filled_qty="0",
            filled_avg_price=None,
            replaced_by=None,
            submitted_at=NOW,
        )


def make_broker(root: Path, client: ManagedOrderClient) -> AlpacaStockBroker:
    broker = AlpacaStockBroker.__new__(AlpacaStockBroker)
    broker.settings = SimpleNamespace(
        output_dir=root,
        extended_hours_orders_enabled=True,
        order_cancel_after_seconds=600,
        market_timezone="America/New_York",
        broker_protective_stop_pct=-0.08,
    )
    broker.client = client
    broker.paper = True
    broker.order_safety_error = ""
    broker.order_recording_error = ""
    broker.pending_order_store = PendingOrderStore(root)
    broker._record_result = lambda result, _reason: result
    return broker


class PendingOrderTests(unittest.TestCase):
    def test_native_protective_stop_is_gtc_stop_market_and_not_timed_out(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ProtectiveOrderClient()
            broker = make_broker(root, client)

            with patch("alpaca_ma5_service.broker.notify_order_submitted"):
                broker.ensure_protective_stops(
                    {"US.TEST": Position("US.TEST", 10.0, 10.0, "test")},
                    {"US.TEST"},
                    -0.08,
                    NOW,
                )

            request = client.last_submit_request
            self.assertEqual((request.side.value, request.time_in_force.value), ("sell", "gtc"))
            self.assertEqual(request.type.value, "stop")
            self.assertEqual((float(request.qty), float(request.stop_price)), (10.0, 9.2))
            pending = next(iter(broker.pending_order_store.orders.values()))
            self.assertEqual(pending.strategy_action, BROKER_PROTECTIVE_STOP_ACTION)

            broker.reconcile_pending_orders(NOW + timedelta(seconds=3600))
            self.assertEqual(client.cancel_calls, 0)

    def test_protective_stop_replaces_quantity_and_price_after_scale_in(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ProtectiveOrderClient()
            broker = make_broker(root, client)
            with patch("alpaca_ma5_service.broker.notify_order_submitted"):
                broker.ensure_protective_stops(
                    {"US.TEST": Position("US.TEST", 10.0, 10.0, "test")},
                    {"US.TEST"},
                    -0.08,
                    NOW,
                )
                broker.ensure_protective_stops(
                    {"US.TEST": Position("US.TEST", 12.0, 9.0, "test")},
                    {"US.TEST"},
                    -0.08,
                    NOW + timedelta(seconds=10),
                )

            self.assertEqual(client.submit_calls, 1)
            self.assertEqual(client.replace_calls, 1)
            self.assertEqual(
                (float(client.last_replace_request.qty), float(client.last_replace_request.stop_price)),
                (12.0, 8.28),
            )
            pending = next(iter(broker.pending_order_store.orders.values()))
            self.assertEqual((pending.requested_quantity, pending.requested_price), (12.0, 8.28))
            event = broker.reconcile_pending_orders(NOW + timedelta(seconds=20))[0]
            self.assertEqual(event.active_order_id, "replacement-1")

    def test_waiting_protection_does_not_block_exit_but_partial_fill_does(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ProtectiveOrderClient()
            broker = make_broker(root, client)
            with patch("alpaca_ma5_service.broker.notify_order_submitted"):
                broker.ensure_protective_stops(
                    {"US.TEST": Position("US.TEST", 10.0, 10.0, "test")},
                    {"US.TEST"},
                    -0.08,
                    NOW,
                )
            self.assertEqual(broker.get_open_strategy_exit_order_symbols(), set())
            raw = next(iter(client.orders.values()))
            raw.status = "partially_filled"
            raw.filled_qty = "1"
            raw.filled_avg_price = "9.19"
            self.assertEqual(broker.get_open_strategy_exit_order_symbols(), {"US.TEST"})

    def test_active_exit_waits_until_protection_cancel_is_confirmed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ProtectiveOrderClient(cancel_immediately=False)
            broker = make_broker(root, client)
            with patch("alpaca_ma5_service.broker.notify_order_submitted"):
                broker.ensure_protective_stops(
                    {"US.TEST": Position("US.TEST", 10.0, 10.0, "test")},
                    {"US.TEST"},
                    -0.08,
                    NOW,
                )

            released, message = broker.release_protective_stop("US.TEST", NOW + timedelta(seconds=10))
            self.assertFalse(released)
            self.assertIn("撤销处理中", message)
            raw = next(iter(client.orders.values()))
            raw.status = "canceled"
            released, _ = broker.release_protective_stop("US.TEST", NOW + timedelta(seconds=20))
            self.assertTrue(released)
            self.assertEqual(broker.pending_order_store.orders, {})

    def test_service_never_submits_second_sell_while_stop_cancel_is_pending(self):
        class Broker:
            def __init__(self):
                self.sell_calls = 0

            def release_protective_stop(self, _symbol, _now):
                return False, "保护单撤销处理中"

            def place_market_sell_nonblocking(self, *_args, **_kwargs):
                self.sell_calls += 1
                raise AssertionError("must not submit a second sell")

        broker = Broker()
        snapshot = MarketSnapshot("US.TEST", 11.0, [8, 9, 9, 10], NOW)
        cycle = SymbolTradeCycle(
            "US.TEST",
            route="SELL",
            snapshot=snapshot,
            signal=Signal("US.TEST", "SELL_ALL", "test exit", 11.0, 10.0),
        )
        trading_round = SimpleNamespace(
            broker=broker,
            now_et=NOW,
            can_order_now=True,
            ladder_store=None,
            settings=SimpleNamespace(),
            buying_paused=False,
        )

        execute_sell(trading_round, cycle)

        self.assertEqual(broker.sell_calls, 0)
        self.assertEqual(cycle.outcome, "hold")
        self.assertEqual(cycle.row.action, "等待保护单")

    def test_restart_adopts_existing_managed_stop_without_duplicate_submit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ProtectiveOrderClient()
            client.orders["existing-stop"] = client._raw(
                "existing-stop",
                qty=10,
                stop_price=9.2,
                client_order_id="ma5-stop-existing",
            )
            broker = make_broker(root, client)
            broker.ensure_protective_stops(
                {"US.TEST": Position("US.TEST", 10.0, 10.0, "test")},
                {"US.TEST"},
                -0.08,
                NOW,
            )
            self.assertEqual(client.submit_calls, 0)
            pending = broker.pending_order_store.orders["existing-stop"]
            self.assertEqual(pending.strategy_action, BROKER_PROTECTIVE_STOP_ACTION)

    def test_orphaned_managed_stop_is_canceled_instead_of_left_at_broker(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ProtectiveOrderClient()
            client.orders["orphan-stop"] = client._raw(
                "orphan-stop",
                qty=10,
                stop_price=9.2,
                client_order_id="ma5-stop-orphan",
            )
            broker = make_broker(root, client)

            broker.ensure_protective_stops({}, set(), -0.08, NOW)

            self.assertEqual(client.cancel_calls, 1)
            self.assertEqual(client.orders["orphan-stop"].status, "canceled")
            self.assertEqual(broker.pending_order_store.orders, {})

    def test_protective_fill_stops_plan_and_requests_pending_buy_cancel(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LadderStateStore(root)
            plan = store.create(
                "US.TEST",
                NOW.date(),
                300.0,
                10.0,
                (0.0, -0.01, -0.02),
                (0.0, 0.01, 0.02),
                NOW,
            )
            plan.filled_quantity = 10.0
            plan.filled_notional = 100.0
            plan.broker_stop_enabled = True
            store.save(NOW)
            event = PendingOrderEvent(
                "stop-1", "stop-1", "US.TEST", "SELL", 10.0, 9.2, "stop",
                BROKER_PROTECTIVE_STOP_ACTION, 0.0, "FILLED", 10.0, 9.1, True,
            )

            class Broker:
                order_safety_error = ""
                order_recording_error = ""

                def __init__(self):
                    self.canceled_symbols = []
                    self.acknowledged = []

                def reconcile_pending_orders(self, _now):
                    return [event]

                def cancel_managed_buy_orders_for_symbol(self, symbol, _now):
                    self.canceled_symbols.append(symbol)

                def acknowledge_pending_order(self, order_id, _now):
                    self.acknowledged.append(order_id)

            broker = Broker()
            reconcile_managed_pending_orders(broker, store, NOW)
            self.assertEqual(store.get("US.TEST").status, "stopped")
            self.assertEqual(broker.canceled_symbols, ["US.TEST"])
            self.assertEqual(broker.acknowledged, ["stop-1"])

    def test_service_applies_terminal_fill_before_acknowledging_order(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LadderStateStore(root)
            store.create(
                "US.TEST",
                NOW.date(),
                300.0,
                10.0,
                (0.0, -0.01, -0.02),
                (0.0, 0.01, 0.02),
                NOW,
            )
            event = PendingOrderEvent(
                "managed-order-1",
                "managed-order-1",
                "US.TEST",
                "BUY",
                10.0,
                10.0,
                "test",
                "buy_leg_0",
                100.0,
                "FILLED",
                10.0,
                9.8,
                True,
            )

            class Broker:
                order_safety_error = ""
                order_recording_error = ""

                def __init__(self):
                    self.acknowledged = []

                def reconcile_pending_orders(self, _now):
                    return [event]

                def acknowledge_pending_order(self, order_id, _now):
                    plan = LadderStateStore(root).get("US.TEST")
                    self.acknowledged.append((order_id, plan.filled_quantity))

            broker = Broker()
            reconcile_managed_pending_orders(broker, store, NOW)

            self.assertEqual(broker.acknowledged, [("managed-order-1", 10.0)])
            self.assertAlmostEqual(LadderStateStore(root).get("US.TEST").filled_notional, 98.0)

    def test_low_price_limit_precision_keeps_three_ladder_levels(self):
        prices = [normalize_limit_price(value) for value in (0.5000, 0.4950, 0.4900)]
        self.assertEqual(prices, [0.5, 0.495, 0.49])
        self.assertEqual(normalize_limit_price(10.785), 10.79)

    def test_nonblocking_submit_returns_without_polling_and_persists_order(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ManagedOrderClient()
            broker = make_broker(root, client)
            notification_ordering = []

            def observe_notification(*_args, **_kwargs):
                notification_ordering.append("managed-order-1" in PendingOrderStore(root).orders)

            with patch("alpaca_ma5_service.broker.now_market_time", return_value=NOW):
                with patch("alpaca_ma5_service.broker.notify_order_submitted", side_effect=observe_notification):
                    result = broker.place_limit_buy_nonblocking(
                        "US.TEST",
                        100.0,
                        10.0,
                        "test",
                        strategy_action="buy_leg_0",
                    )

            self.assertEqual(result.status, "SUBMITTED")
            self.assertEqual(client.get_calls, 0)
            self.assertEqual(client.cancel_calls, 0)
            self.assertEqual(notification_ordering, [True])
            loaded = PendingOrderStore(root).orders["managed-order-1"]
            self.assertEqual((loaded.symbol, loaded.strategy_action), ("US.TEST", "buy_leg_0"))

    def test_reconcile_reports_cumulative_late_fill_and_is_restart_safe(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ManagedOrderClient()
            broker = make_broker(root, client)
            broker.pending_order_store.register(
                order_id="managed-order-1",
                symbol="US.TEST",
                side="BUY",
                requested_quantity=10.0,
                requested_price=10.0,
                reason="test",
                strategy_action="buy_leg_0",
                strategy_notional=100.0,
                submitted_at=NOW,
                status="SUBMITTED",
            )

            client.status = "partially_filled"
            client.filled_qty = "4"
            client.filled_avg_price = "9.90"
            partial = broker.reconcile_pending_orders(NOW + timedelta(seconds=30))[0]
            self.assertEqual((partial.status, partial.filled_quantity, partial.terminal), ("PARTIALLY_FILLED", 4.0, False))

            # Re-open the persisted store as a fresh process would, then observe the late fill.
            restarted = make_broker(root, client)
            client.status = "filled"
            client.filled_qty = "10"
            client.filled_avg_price = "9.80"
            final = restarted.reconcile_pending_orders(NOW + timedelta(seconds=60))[0]
            self.assertEqual((final.status, final.filled_quantity, final.filled_avg_price), ("FILLED", 10.0, 9.8))
            restarted.acknowledge_pending_order(final.tracking_order_id, NOW + timedelta(seconds=60))
            self.assertEqual(PendingOrderStore(root).orders, {})

    def test_overdue_order_is_canceled_without_sleeping(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ManagedOrderClient()
            broker = make_broker(root, client)
            broker.pending_order_store.register(
                order_id="managed-order-1",
                symbol="US.TEST",
                side="SELL",
                requested_quantity=5.0,
                requested_price=10.0,
                reason="test",
                strategy_action="close_liquidation",
                strategy_notional=0.0,
                submitted_at=NOW,
                status="SUBMITTED",
            )

            event = broker.reconcile_pending_orders(NOW + timedelta(seconds=600))[0]
            self.assertEqual(client.cancel_calls, 1)
            self.assertEqual((event.status, event.terminal), ("CANCELED", True))

    def test_terminal_fill_keeps_same_side_symbol_guarded_for_current_round(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ManagedOrderClient()
            broker = make_broker(root, client)
            broker._get_open_orders = lambda _symbol: []
            broker.pending_order_store.register(
                order_id="managed-order-1",
                symbol="US.TEST",
                side="SELL",
                requested_quantity=5.0,
                requested_price=10.0,
                reason="test",
                strategy_action="absolute_stop_market",
                strategy_notional=0.0,
                submitted_at=NOW,
                status="SUBMITTED",
            )
            client.status = "filled"
            client.filled_qty = "5"
            client.filled_avg_price = "9.90"

            event = broker.reconcile_pending_orders(NOW + timedelta(seconds=30))[0]
            broker.acknowledge_pending_order(event.tracking_order_id, NOW + timedelta(seconds=30))
            self.assertEqual(PendingOrderStore(root).orders, {})
            self.assertEqual(broker.get_open_sell_order_symbols(), {"US.TEST"})

            broker.reconcile_pending_orders(NOW + timedelta(seconds=60))
            self.assertEqual(broker.get_open_sell_order_symbols(), set())

    def test_corrupt_pending_state_fails_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pending_orders.json").write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "无法读取待确认订单状态"):
                PendingOrderStore(root)

    def test_invalid_pending_state_version_fails_with_runtime_error(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pending_orders.json").write_text('{"version":"bad","orders":{}}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "版本无效"):
                PendingOrderStore(root)


if __name__ == "__main__":
    unittest.main()
