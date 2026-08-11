import unittest
from decimal import Decimal

from subledger import Ledger, OrderRequest, OrderSide, Reconciler, Router, SubAccount
from subledger.broker.mock import MockBroker

D = Decimal


class ReconcilerTest(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker(cash=D("100000"))
        self.ledger = Ledger(":memory:")
        self.router = Router(self.ledger, self.broker)
        self.router.adopt_broker_cash()
        self.router.create_sub_account(SubAccount(id="alpha"), initial_allocation=D("30000"))
        self.reconciler = Reconciler(self.ledger, self.broker)
        self.broker.set_price("AAPL", D("200"))

    def trade(self, qty):
        self.router.place_order(
            OrderRequest(sub_account_id="alpha", symbol="AAPL", side=OrderSide.BUY, qty=D(qty))
        )

    def test_clean_after_trading(self):
        self.trade("10")
        report = self.reconciler.run()
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(report.expected_cash, D("98000"))
        self.assertEqual(report.broker_cash, D("98000"))

    def test_detects_external_cash_change(self):
        self.broker.inject_external_cash(D("-500"))  # e.g. a fee hit the account
        report = self.reconciler.run()
        self.assertFalse(report.ok)
        self.assertEqual(report.drifts[0].kind, "cash")

    def test_detects_external_position(self):
        # A human buys 5 TSLA in the same real account, outside the router.
        self.broker.set_price("TSLA", D("300"))
        self.broker.inject_external_position("TSLA", D("5"), D("300"))
        report = self.reconciler.run()
        self.assertFalse(report.ok)
        kinds = {d.kind for d in report.drifts}
        self.assertIn("unknown_position", kinds)
        self.assertIn("cash", kinds)  # the purchase also moved cash

    def test_detects_foreign_order(self):
        self.broker.inject_external_order("NVDA", D("3"), client_order_id="manual-123")
        report = self.reconciler.run()
        self.assertFalse(report.ok)
        self.assertIn("unknown_order", {d.kind for d in report.drifts})

    def test_halt_on_drift(self):
        reconciler = Reconciler(self.ledger, self.broker, halt_on_drift=True)
        self.broker.inject_external_cash(D("999"))
        reconciler.run()
        self.assertTrue(self.ledger.is_halted())

    def test_absorb_cash_drift(self):
        self.broker.inject_external_cash(D("1000"))  # deposit arrived
        delta = self.reconciler.absorb_cash_drift(note="august deposit")
        self.assertEqual(delta, D("1000"))
        self.assertTrue(self.reconciler.run().ok)
        self.assertEqual(self.ledger.unallocated_cash(), D("71000"))

    def test_marks_refreshed(self):
        self.trade("10")
        self.broker.set_price("AAPL", D("240"))
        self.reconciler.run()
        pos = self.ledger.get_position("alpha", "AAPL")
        self.assertEqual(pos.last_price, D("240"))
        self.assertEqual(pos.unrealized_pnl, D("400"))

    def test_report_persisted(self):
        self.reconciler.run()
        latest = self.ledger.latest_reconciliation()
        self.assertIsNotNone(latest)
        self.assertTrue(latest["ok"])


if __name__ == "__main__":
    unittest.main()
