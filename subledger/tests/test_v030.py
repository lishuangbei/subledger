import unittest
from decimal import Decimal as D

from subledger import (
    Ledger,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderType,
    Router,
    StopLossSpec,
    SubAccount,
    TakeProfitSpec,
)
from subledger.broker.base import BrokerAsset
from subledger.broker.mock import MockBroker
from subledger.models import OrderStatus, TimeInForce
from subledger.router import OrderRejected


def make_stack():
    broker = MockBroker(cash=D("100000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    router.create_sub_account(SubAccount(id="s1"), initial_allocation=D("50000"))
    return broker, ledger, router


class TifTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))

    def test_opg_market_accepted(self):
        order = self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.BUY, D("1"), time_in_force=TimeInForce.OPG))
        self.assertTrue(order.status.value in ("open", "filled"))

    def test_cls_bracket_rejected(self):
        with self.assertRaises(OrderRejected):
            self.router.place_order(OrderRequest(
                "s1", "AAPL", OrderSide.BUY, D("1"),
                time_in_force=TimeInForce.CLS,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitSpec(limit_price=D("120")),
                stop_loss=StopLossSpec(stop_price=D("90"))))

    def test_fok_accepted(self):
        order = self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.BUY, D("1"), time_in_force=TimeInForce.FOK))
        self.assertTrue(order.status.value in ("open", "filled"))


class AssetCheckTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("HALT", D("10"))
        self.broker.set_price("BRK", D("100"))

    def test_untradable_rejected(self):
        self.broker.assets["HALT"] = BrokerAsset(symbol="HALT", tradable=False)
        with self.assertRaises(OrderRejected):
            self.router.place_order(OrderRequest("s1", "HALT", OrderSide.BUY, D("1")))

    def test_fractional_on_unfractionable_rejected(self):
        self.broker.assets["BRK"] = BrokerAsset(symbol="BRK", fractionable=False)
        with self.assertRaises(OrderRejected):
            self.router.place_order(OrderRequest("s1", "BRK", OrderSide.BUY, D("0.5")))
        # whole shares still fine
        order = self.router.place_order(OrderRequest("s1", "BRK", OrderSide.BUY, D("1")))
        self.assertEqual(order.status.value, "filled")


class LegReplaceTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("s1", "AAPL", OrderSide.BUY, D("10")))
        self.parent = self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.SELL, D("10"),
            order_type=OrderType.LIMIT, order_class=OrderClass.OCO,
            take_profit=TakeProfitSpec(limit_price=D("120")),
            stop_loss=StopLossSpec(stop_price=D("90")),
            time_in_force=TimeInForce.GTC))

    def test_tighten_stop_without_unprotected_window(self):
        leg = self.ledger.list_legs(self.parent.id)[0]
        old_broker_id = leg.broker_order_id
        replaced = self.router.replace_order(leg.id, stop_price=D("95"))
        self.assertEqual(replaced.stop_price, D("95"))
        self.assertNotEqual(replaced.broker_order_id, old_broker_id)
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.reserved_qty, D("10"))     # reserve never dropped
        # new stop level actually live at the broker
        self.broker.set_price("AAPL", D("94"))
        self.router.sync()
        self.assertEqual(self.ledger.get_order(leg.id).status.value, "filled")

    def test_move_take_profit_on_parent(self):
        replaced = self.router.replace_order(self.parent.id, limit_price=D("115"))
        self.assertEqual(replaced.limit_price, D("115"))
        self.broker.set_price("AAPL", D("116"))
        self.router.sync()
        self.assertEqual(self.ledger.get_order(self.parent.id).status.value, "filled")

    def test_leg_qty_replace_rejected(self):
        leg = self.ledger.list_legs(self.parent.id)[0]
        with self.assertRaises(OrderRejected):
            self.router.replace_order(leg.id, qty=D("5"))


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))

    def _orphan(self, submit_to_broker: bool):
        """Simulate a crash between save_order(PENDING) and broker-id save."""
        from subledger.models import OrderRecord, new_client_order_id
        import uuid

        client_id = new_client_order_id("s1", "orphan-test")
        record = OrderRecord(
            id=uuid.uuid4().hex[:16],
            client_order_id=client_id,
            broker_order_id=None,
            sub_account_id="s1",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=D("5"),
            order_type=OrderType.LIMIT,
            limit_price=D("95"),
            time_in_force=TimeInForce.GTC,
            status=OrderStatus.PENDING,
            reserved=D("475"),
        )
        acct = self.ledger.get_sub_account("s1")
        acct.reserved_cash += D("475")
        self.ledger.save_sub_account(acct)
        self.ledger.save_order(record)
        if submit_to_broker:
            req = OrderRequest("s1", "AAPL", OrderSide.BUY, D("5"),
                               order_type=OrderType.LIMIT, limit_price=D("95"),
                               time_in_force=TimeInForce.GTC)
            self.broker.submit_order(req, client_id)
        return record

    def test_repair_releases_never_submitted_orphan(self):
        record = self._orphan(submit_to_broker=False)
        report = self.router.repair()
        self.assertIn(record.client_order_id, report["released"])
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.reserved_cash, D("0"))
        self.assertEqual(self.ledger.get_order(record.id).status.value, "rejected")

    def test_repair_adopts_submitted_orphan(self):
        record = self._orphan(submit_to_broker=True)
        report = self.router.repair()
        self.assertIn(record.client_order_id, report["adopted"])
        adopted = self.ledger.get_order(record.id)
        self.assertIsNotNone(adopted.broker_order_id)
        self.assertEqual(adopted.status.value, "open")
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.reserved_cash, D("475"))  # reserve stays until fill/cancel
        # and the adopted order is live: it fills when price crosses
        self.broker.set_price("AAPL", D("94"))
        self.router.sync()
        self.assertEqual(self.ledger.get_order(record.id).status.value, "filled")


class StreamStateTests(unittest.TestCase):
    def test_apply_stream_state_books_fill_without_polling(self):
        from subledger.broker.base import BrokerOrderState

        broker, ledger, router = make_stack()
        broker.set_price("AAPL", D("100"))
        broker.fill_market_orders = False       # order rests at the broker
        order = router.place_order(OrderRequest("s1", "AAPL", OrderSide.BUY, D("5")))
        self.assertEqual(order.status.value, "open")

        state = BrokerOrderState(
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            status="filled",
            filled_qty=D("5"),
            filled_avg_price=D("100.5"),
            symbol="AAPL",
        )
        booked = router.apply_stream_state(state)
        self.assertTrue(booked)
        final = ledger.get_order(order.id)
        self.assertEqual(final.status.value, "filled")
        pos = ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("5"))
        # duplicate delivery (stream + poll) is a no-op
        self.assertFalse(router.apply_stream_state(state))


if __name__ == "__main__":
    unittest.main()


class CashActivityAttributionTests(unittest.TestCase):
    def setUp(self):
        from subledger import Reconciler
        self.broker, self.ledger, self.router = make_stack()
        self.router.create_sub_account(SubAccount(id="s2"), initial_allocation=D("20000"))
        self.reconciler = Reconciler(self.ledger, self.broker)
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("s1", "AAPL", OrderSide.BUY, D("30")))
        self.router.place_order(OrderRequest("s2", "AAPL", OrderSide.BUY, D("10")))

    def test_dividend_attributed_by_holdings(self):
        self.broker.inject_cash_activity("dividend", D("40"), symbol="AAPL")
        result = self.reconciler.attribute_cash_activities()
        self.assertEqual(result["dividends"], 1)
        s1 = self.ledger.get_sub_account("s1")
        s2 = self.ledger.get_sub_account("s2")
        self.assertEqual(s1.realized_pnl, D("30"))   # 30/40 shares
        self.assertEqual(s2.realized_pnl, D("10"))   # 10/40 shares
        # cash total matches broker again -> reconcile clean
        report = self.reconciler.run()
        self.assertTrue(report.ok, report.summary())

    def test_fee_goes_to_unassigned_pool(self):
        pool_before = self.ledger.unallocated_cash()
        self.broker.inject_cash_activity("fee", D("-1.23"))
        result = self.reconciler.attribute_cash_activities()
        self.assertEqual(result["pool"], 1)
        self.assertEqual(self.ledger.unallocated_cash(), pool_before + D("-1.23"))
        self.assertTrue(self.reconciler.run().ok)

    def test_idempotent_processing(self):
        self.broker.inject_cash_activity("dividend", D("40"), symbol="AAPL")
        self.reconciler.attribute_cash_activities()
        result = self.reconciler.attribute_cash_activities()   # second pass
        self.assertEqual(result["dividends"], 0)
        self.assertEqual(self.ledger.get_sub_account("s1").realized_pnl, D("30"))


    def test_deposit_never_auto_booked(self):
        pool_before = self.ledger.unallocated_cash()
        self.broker.inject_cash_activity("other", D("1000000"))   # a deposit
        result = self.reconciler.attribute_cash_activities()
        self.assertEqual(result["pool"], 0)
        self.assertEqual(self.ledger.unallocated_cash(), pool_before)  # untouched
        # marked as seen: second pass does not revisit it
        result = self.reconciler.attribute_cash_activities()
        self.assertEqual(result["pool"], 0)
