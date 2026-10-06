"""A failing reconcile is confirmed before it is recorded or halts: fills
and fees in flight must not halt a live account (liveIB 2026-10-06)."""

import unittest
from decimal import Decimal as D
from unittest import mock

from subledger import Ledger, OrderRequest, OrderSide, Reconciler, Router, SubAccount
from subledger.broker.mock import MockBroker
import subledger.reconciler as recmod


class ReconcileConfirmTests(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker(cash=D("100000"))
        self.ledger = Ledger(":memory:")
        self.router = Router(self.ledger, self.broker)
        self.router.adopt_broker_cash()
        self.router.create_sub_account(SubAccount(id="alpha"), initial_allocation=D("30000"))
        self.broker.set_price("AAPL", D("200"))

    def reconciler(self, **kw):
        return Reconciler(self.ledger, self.broker, halt_on_drift=True, confirm_delay_s=0.01, **kw)

    def test_fill_still_being_booked_does_not_halt(self):
        # the broker already shows 10 AAPL; the stream books them during the re-check wait
        self.broker.inject_external_position("AAPL", D("10"), D("200"))

        def book_fill(_seconds):
            pos = self.ledger.get_position("alpha", "AAPL")
            pos.qty, pos.avg_cost, pos.last_price = D("10"), D("200"), D("200")
            self.ledger.save_position(pos)
            acct = self.ledger.get_sub_account("alpha")
            acct.cash -= D("2000")
            self.ledger.save_sub_account(acct)

        with mock.patch.object(recmod.time, "sleep", side_effect=book_fill):
            report = self.reconciler().run()
        self.assertTrue(report.ok, report.summary())
        self.assertFalse(self.ledger.is_halted())
        self.assertTrue(self.ledger.latest_reconciliation()["ok"])

    def test_commission_in_flight_is_booked_before_the_recheck(self):
        self.router.place_order(OrderRequest(sub_account_id="alpha", symbol="AAPL",
                                             side=OrderSide.BUY, qty=D("2")))
        order = self.ledger.list_orders("alpha")[0]
        # IB charges the $1.00 minimum with the fill; attribution has not run yet
        self.broker.inject_cash_activity("fee", D("-1.00"), symbol="AAPL",
                                         order_client_id=order.client_order_id)
        report = self.reconciler(halt_min_cash_drift=D("1.00")).run()
        self.assertTrue(report.ok, report.summary())
        self.assertFalse(self.ledger.is_halted())
        self.assertEqual(self.ledger.get_sub_account("alpha").realized_pnl, D("-1.00"))

    def test_real_drift_still_halts_and_is_recorded_once(self):
        self.broker.inject_external_cash(D("999"))
        with mock.patch.object(recmod.time, "sleep") as slept:
            report = self.reconciler().run()
        slept.assert_called_once()
        self.assertFalse(report.ok)
        self.assertTrue(self.ledger.is_halted())
        rows = self.ledger._conn.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_off_by_default(self):
        self.broker.inject_external_cash(D("999"))
        with mock.patch.object(recmod.time, "sleep") as slept:
            Reconciler(self.ledger, self.broker, halt_on_drift=True).run()
        slept.assert_not_called()
        self.assertTrue(self.ledger.is_halted())


if __name__ == "__main__":
    unittest.main()
