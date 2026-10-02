"""Same-day broker fees not yet published are not reconciliation drift.

Alpaca takes CAT / REG / TAF fees out of cash at execution but publishes the
FEE activities around 20:30 ET, so every trading day with fills used to show
a cent-level cash drift for hours (2026-10-02: 0.0135 after one SNPS buy),
which also made the executors refuse to trade (require_reconciliation)."""

import datetime as dt
import unittest
from decimal import Decimal as D
from unittest import mock

from subledger import Ledger, OrderRequest, OrderSide, Reconciler, Router, SubAccount
from subledger.broker.mock import MockBroker

UTC = dt.timezone.utc
TRADE_AT = dt.datetime(2026, 10, 2, 16, 16, tzinfo=UTC)        # 12:16 ET
BEFORE_CUTOFF = dt.datetime(2026, 10, 2, 19, 0, tzinfo=UTC)    # 15:00 ET
AFTER_CUTOFF = dt.datetime(2026, 10, 3, 1, 30, tzinfo=UTC)     # 21:30 ET


class FeeLagBroker(MockBroker):
    fee_posting_cutoff_et = dt.time(21, 0)

    def pending_fee_allowance(self, bought_shares, sold_shares, sold_notional):
        return (D("0.02") + D("0.0001") * (bought_shares + sold_shares)
                + D("0.00005") * sold_notional + D("0.0003") * sold_shares)


def stack(broker_cls=FeeLagBroker, trade=True):
    broker = broker_cls(cash=D("50000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    router.create_sub_account(SubAccount(id="4757"), initial_allocation=D("5000"))
    broker.set_price("SNPS", D("489.96"))
    if trade:
        with mock.patch("subledger.ledger._now", lambda: TRADE_AT.isoformat()):
            router.place_order(OrderRequest("4757", "SNPS", OrderSide.BUY, D("2")))
    reconciler = Reconciler(ledger, broker, halt_on_drift=True)
    return broker, ledger, reconciler


def run_at(reconciler, when):
    reconciler._now = lambda: when
    return reconciler.run()


class PendingFeeTests(unittest.TestCase):
    def test_same_day_fee_gap_is_pending_until_publication(self):
        broker, ledger, reconciler = stack()
        broker.inject_external_cash(D("-0.02"))          # CAT fees left cash at execution (> the 0.01 tolerance)
        report = run_at(reconciler, BEFORE_CUTOFF)
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(report.pending_fees, D("0.02"))
        self.assertFalse(ledger.is_halted())
        self.assertTrue(ledger.latest_reconciliation()["ok"])
        self.assertEqual(ledger.latest_reconciliation()["report"]["pending_fees"], "0.02")

    def test_gap_still_there_after_publication_time_is_drift(self):
        broker, ledger, reconciler = stack()
        broker.inject_external_cash(D("-0.02"))
        report = run_at(reconciler, AFTER_CUTOFF)
        self.assertFalse(report.ok)
        self.assertEqual([d.kind for d in report.drifts], ["cash"])

    def test_gap_larger_than_the_fee_estimate_is_drift(self):
        broker, ledger, reconciler = stack()
        broker.inject_external_cash(D("-5"))
        self.assertFalse(run_at(reconciler, BEFORE_CUTOFF).ok)

    def test_broker_ahead_of_ledger_is_drift(self):
        broker, ledger, reconciler = stack()
        broker.inject_external_cash(D("0.02"))           # fees never ADD cash
        self.assertFalse(run_at(reconciler, BEFORE_CUTOFF).ok)

    def test_no_fills_since_last_publication_is_drift(self):
        broker, ledger, reconciler = stack(trade=False)
        broker.inject_external_cash(D("-0.02"))
        self.assertFalse(run_at(reconciler, BEFORE_CUTOFF).ok)

    def test_venue_without_fee_lag_reports_every_gap(self):
        broker, ledger, reconciler = stack(broker_cls=MockBroker)
        broker.inject_external_cash(D("-0.02"))
        self.assertFalse(run_at(reconciler, BEFORE_CUTOFF).ok)

    def test_position_drift_is_never_explained_by_fees(self):
        broker, ledger, reconciler = stack()
        broker.inject_external_cash(D("-0.02"))
        broker.set_price("TSLA", D("300"))
        broker.inject_external_position("TSLA", D("1"), D("300"))
        report = run_at(reconciler, BEFORE_CUTOFF)
        self.assertFalse(report.ok)
        self.assertIn("unknown_position", [d.kind for d in report.drifts])


try:
    import alpaca  # noqa: F401
    HAVE_ALPACA = True
except ImportError:
    HAVE_ALPACA = False


@unittest.skipUnless(HAVE_ALPACA, "alpaca-py not installed")
class AlpacaFeeLagTests(unittest.TestCase):
    def test_alpaca_declares_its_fee_lag(self):
        from subledger.broker.alpaca import AlpacaBroker
        self.assertEqual(AlpacaBroker.fee_posting_cutoff_et, dt.time(21, 0))
        allowance = AlpacaBroker.pending_fee_allowance(None, D("2"), D("0"), D("0"))
        self.assertGreaterEqual(allowance, D("0.02"))
        # 2026-09-01: two sells, 235 shares, $33,156.51 -> fees 0.75 posted that evening
        self.assertGreater(AlpacaBroker.pending_fee_allowance(None, D("0"), D("235"), D("33156.51")), D("0.75"))


if __name__ == "__main__":
    unittest.main()
