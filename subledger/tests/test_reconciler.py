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

    def test_fee_with_order_attribution_charged_to_owner(self):
        # IB-style commission: the activity names the order's client id, so
        # the owning sub-account pays it (realized P&L), not the pool.
        self.trade("10")
        pool_before = self.ledger.unallocated_cash()
        self.broker.inject_cash_activity("fee", D("-1.05"), symbol="AAPL",
                                         order_client_id="sl.alpha.entry1")
        result = self.reconciler.attribute_cash_activities()
        self.assertEqual(result["fees"], 1)
        acct = self.ledger.get_sub_account("alpha")
        self.assertEqual(acct.cash, D("27998.95"))
        self.assertEqual(acct.realized_pnl, D("-1.05"))
        self.assertEqual(self.ledger.unallocated_cash(), pool_before)
        self.assertTrue(self.reconciler.run().ok)
        # unknown owner falls back to the pool
        self.broker.inject_cash_activity("fee", D("-0.5"), symbol="AAPL",
                                         order_client_id="sl.ghost.x")
        self.reconciler.attribute_cash_activities()
        self.assertEqual(self.ledger.unallocated_cash(), pool_before - D("0.5"))
        self.assertTrue(self.reconciler.run().ok)

    def test_activities_before_inception_are_not_booked(self):
        # adopt happened in setUp; a commission dated before it was already
        # inside the adopted cash balance and must be ignored.
        inception = self.ledger.inception_at()
        self.assertIsNotNone(inception)
        self.broker.activities.append(__import__("subledger.broker.base", fromlist=["BrokerCashActivity"]).BrokerCashActivity(
            activity_id="old-fee", kind="fee", amount=D("-34"), symbol="MNQZ6",
            at="2000-01-01T00:00:00+00:00"))
        pool = self.ledger.unallocated_cash()
        result = self.reconciler.attribute_cash_activities()
        self.assertEqual((result["fees"], result["pool"]), (0, 0))
        self.assertEqual(self.ledger.unallocated_cash(), pool)
        self.broker.inject_cash_activity("fee", D("-2"), symbol="AAPL", at="2999-01-01T00:00:00+00:00")
        self.reconciler.attribute_cash_activities()
        self.assertEqual(self.ledger.unallocated_cash(), pool - D("2"))

    def test_report_persisted(self):
        self.reconciler.run()
        latest = self.ledger.latest_reconciliation()
        self.assertIsNotNone(latest)
        self.assertTrue(latest["ok"])


if __name__ == "__main__":
    unittest.main()
