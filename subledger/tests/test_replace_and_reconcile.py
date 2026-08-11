import unittest
from decimal import Decimal as D

from subledger import (
    Ledger,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderType,
    Reconciler,
    Router,
    StopLossSpec,
    SubAccount,
    TakeProfitSpec,
)
from subledger.broker.mock import MockBroker
from subledger.router import OrderRejected


def make_stack():
    broker = MockBroker(cash=D("100000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    router.create_sub_account(SubAccount(id="s1"), initial_allocation=D("50000"))
    return broker, ledger, router


class ReplaceTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))

    def test_replace_limit_price_adjusts_reserve_and_broker_id(self):
        order = self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.BUY, D("10"),
            order_type=OrderType.LIMIT, limit_price=D("95")))
        old_broker_id = order.broker_order_id
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.reserved_cash, D("950"))

        replaced = self.router.replace_order(order.id, limit_price=D("98"))
        self.assertNotEqual(replaced.broker_order_id, old_broker_id)
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.reserved_cash, D("980"))

        self.broker.set_price("AAPL", D("97"))
        self.router.sync()
        final = self.ledger.get_order(order.id)
        self.assertEqual(final.status.value, "filled")
        self.assertEqual(final.filled_avg_price, D("98"))
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.reserved_cash, D("0"))

    def test_replace_beyond_buying_power_rejected(self):
        order = self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.BUY, D("10"),
            order_type=OrderType.LIMIT, limit_price=D("95")))
        with self.assertRaises(OrderRejected):
            self.router.replace_order(order.id, qty=D("10000"))

    def test_replace_terminal_rejected(self):
        order = self.router.place_order(OrderRequest("s1", "AAPL", OrderSide.BUY, D("1")))
        with self.assertRaises(OrderRejected):
            self.router.replace_order(order.id, limit_price=D("98"))


class ReconcilerV2Tests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.reconciler = Reconciler(self.ledger, self.broker)
        self.broker.set_price("AAPL", D("100"))

    def test_oco_legs_are_not_foreign(self):
        self.router.place_order(OrderRequest("s1", "AAPL", OrderSide.BUY, D("10")))
        self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.SELL, D("10"),
            order_type=OrderType.LIMIT,
            order_class=OrderClass.OCO,
            take_profit=TakeProfitSpec(limit_price=D("120")),
            stop_loss=StopLossSpec(stop_price=D("90")),
        ))
        report = self.reconciler.run()
        self.assertTrue(report.ok, report.summary())

    def test_bracket_leg_fill_stays_reconciled(self):
        self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.BUY, D("10"),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitSpec(limit_price=D("120")),
            stop_loss=StopLossSpec(stop_price=D("90")),
        ))
        self.broker.set_price("AAPL", D("121"))
        self.router.sync()
        report = self.reconciler.run()
        self.assertTrue(report.ok, report.summary())

    def test_acknowledged_foreign_order_stops_drifting(self):
        oid = self.broker.inject_external_order("TSLA", D("5"), "manual-app")
        self.broker.set_price("TSLA", D("200"))   # fills the foreign order

        report = self.reconciler.run()
        self.assertFalse(report.ok)
        kinds = {d.kind for d in report.drifts}
        self.assertIn("unknown_order", kinds)
        drift = next(d for d in report.drifts if d.kind == "unknown_order")
        self.assertEqual(drift.broker_order_id, oid)

        self.reconciler.acknowledge_foreign_orders(report, note="manual app trade")
        self.reconciler.absorb_cash_drift(note="manual app trade")
        # position drift remains until the foreign shares leave the account;
        # simulate the human selling them back.
        self.broker.inject_external_position("TSLA", D("-5"), D("200"))
        self.broker.inject_external_cash(D("-1000"))  # undo sale proceeds for a clean slate
        self.reconciler.absorb_cash_drift(note="round trip")

        report = self.reconciler.run()
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(report.acknowledged_orders_skipped, 1)


if __name__ == "__main__":
    unittest.main()
