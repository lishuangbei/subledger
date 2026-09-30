"""Cash booked at the broker's settlement precision, and the opt-in threshold
that keeps small cash-only drifts from halting (liveAlpa incident 2026-09-25:
a sub-cent fill residue plus a one-cent CAT fee published hours later tripped
halt_on_drift, and the sticky halt then blocked a stop-loss re-arm)."""

import unittest
from decimal import ROUND_HALF_UP
from decimal import Decimal as D

from subledger import Ledger, OrderRequest, OrderSide, OrderType, Reconciler, Router, SubAccount
from subledger.broker.base import BrokerOrderState
from subledger.broker.mock import MockBroker

CENT = D("0.01")


class CentsBroker(MockBroker):
    """Settles every fill's cash in cents, like Alpaca and IB."""

    cash_precision = CENT

    def _apply_fill_to_account(self, symbol, signed_qty, price):
        pos = self.positions.setdefault(symbol, {"qty": D("0"), "avg": D("0")})
        new_qty = pos["qty"] + signed_qty
        if signed_qty > 0 and new_qty != 0:
            pos["avg"] = (pos["qty"] * pos["avg"] + signed_qty * price) / new_qty
        pos["qty"] = new_qty
        self.cash -= (signed_qty * price).quantize(CENT, rounding=ROUND_HALF_UP)


class UnawareCentsBroker(CentsBroker):
    """Same settlement, but the adapter doesn't declare it (pre-fix router)."""

    cash_precision = None


def stack(broker_cls, **reconciler_kwargs):
    broker = broker_cls(cash=D("50000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    router.create_sub_account(SubAccount(id="4757"), initial_allocation=D("5000"))
    broker.set_price("META", D("763.3153"))
    reconciler = Reconciler(ledger, broker, **reconciler_kwargs)
    return broker, ledger, router, reconciler


def buy(router, qty="5"):
    return router.place_order(OrderRequest("4757", "META", OrderSide.BUY, D(qty)))


class CashSettlementTests(unittest.TestCase):
    def test_buy_books_settled_cents(self):
        broker, ledger, router, reconciler = stack(CentsBroker)
        buy(router)
        acct = ledger.get_sub_account("4757")
        self.assertEqual(acct.cash, D("5000") - D("3816.58"))          # not 3816.5765
        self.assertEqual(ledger.get_position("4757", "META").avg_cost, D("763.316"))
        report = reconciler.run()
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(report.expected_cash, report.broker_cash)     # exact, no residue

    def test_round_trip_realized_matches_cash(self):
        broker, ledger, router, reconciler = stack(CentsBroker)
        buy(router)
        broker.set_price("META", D("770.0049"))
        router.place_order(OrderRequest("4757", "META", OrderSide.SELL, D("5")))
        acct = ledger.get_sub_account("4757")
        self.assertEqual(acct.cash, D("5000") - D("3816.58") + D("3850.02"))
        self.assertEqual(acct.realized_pnl, D("33.44"))                 # == cash gained
        self.assertTrue(reconciler.run().ok)

    def test_partial_fills_round_the_cumulative_notional(self):
        broker, ledger, router, reconciler = stack(CentsBroker)
        broker.set_price("XYZ", D("100"))
        order = router.place_order(OrderRequest(
            "4757", "XYZ", OrderSide.BUY, D("5"), order_type=OrderType.LIMIT, limit_price=D("99")))
        cash0 = ledger.get_sub_account("4757").cash
        for cum_qty, avg in (("3", "100.005"), ("5", "100.004")):
            router.apply_stream_state(BrokerOrderState(
                broker_order_id=order.broker_order_id, client_order_id=order.client_order_id,
                status="partially_filled" if cum_qty == "3" else "filled",
                filled_qty=D(cum_qty), filled_avg_price=D(avg), symbol="XYZ"))
        # 3 @ 100.005 = 300.015 -> 300.02; cumulative 5 @ 100.004 = 500.02 -> +200.00
        self.assertEqual(cash0 - ledger.get_sub_account("4757").cash, D("500.02"))

    def test_incident_2026_09_25_reproduced_and_fixed(self):
        # before: unrounded booking (0.0035 residue) + unpublished 1-cent fee -> halt
        broker, ledger, router, reconciler = stack(UnawareCentsBroker, halt_on_drift=True)
        buy(router)
        broker.inject_external_cash(D("-0.01"))
        self.assertFalse(reconciler.run().ok)
        self.assertTrue(ledger.is_halted())
        # after: cents booking leaves exactly the 1-cent fee gap -> within tolerance
        broker, ledger, router, reconciler = stack(CentsBroker, halt_on_drift=True)
        buy(router)
        broker.inject_external_cash(D("-0.01"))
        self.assertTrue(reconciler.run().ok)
        self.assertFalse(ledger.is_halted())


class HaltThresholdTests(unittest.TestCase):
    def test_default_halts_on_any_cash_drift(self):
        broker, ledger, router, reconciler = stack(CentsBroker, halt_on_drift=True)
        broker.inject_external_cash(D("-0.02"))          # two fills' fees, not yet published
        self.assertFalse(reconciler.run().ok)
        self.assertTrue(ledger.is_halted())

    def test_small_cash_drift_reported_not_halted(self):
        broker, ledger, router, reconciler = stack(
            CentsBroker, halt_on_drift=True, halt_min_cash_drift=D("1.00"))
        broker.inject_external_cash(D("-0.02"))
        report = reconciler.run()
        self.assertFalse(report.ok)                       # still visible as drift
        self.assertFalse(ledger.is_halted())
        broker.inject_external_cash(D("-5"))              # a real cash problem
        reconciler.run()
        self.assertTrue(ledger.is_halted())

    def test_position_drift_halts_regardless_of_threshold(self):
        broker, ledger, router, reconciler = stack(
            CentsBroker, halt_on_drift=True, halt_min_cash_drift=D("1000000"))
        broker.set_price("TSLA", D("300"))
        broker.inject_external_position("TSLA", D("1"), D("300"))
        reconciler.run()
        self.assertTrue(ledger.is_halted())


if __name__ == "__main__":
    unittest.main()
