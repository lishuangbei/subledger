"""Concurrent writers must never write stale rows over a fill.

Regression for trial 2026-10-02: a 319-share NVDA buy filled 155 / 131 / 33;
the reconciler listed positions after the first fill, fetched marks over the
network while the second fill was booked, then saved its stale rows and wiped
131 shares from the ledger (cash stayed right, qty did not). Each test books a
fill from inside the broker call the reconciler is waiting on, which is where
the stream thread lands in production."""

import unittest
from decimal import Decimal as D

from subledger import Ledger, OrderRequest, OrderSide, OrderType, Reconciler, Router, SubAccount
from subledger.broker.base import BrokerCashActivity, BrokerOrderState
from subledger.broker.mock import MockBroker


class HookBroker(MockBroker):
    """MockBroker that runs a callback in the middle of the reconciler's
    network calls, like a fill arriving on the stream thread."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.during_marks = None
        self.during_activities = None
        self.extra_activities = []

    def last_prices(self, symbols):
        if self.during_marks:
            hook, self.during_marks = self.during_marks, None
            hook()
        return super().last_prices(symbols)

    def get_cash_activities(self, after=""):
        if self.during_activities:
            hook, self.during_activities = self.during_activities, None
            hook()
        return list(self.extra_activities)


class BookingRaceTests(unittest.TestCase):
    def setUp(self):
        self.broker = HookBroker(cash=D("100000"))
        self.ledger = Ledger(":memory:")
        self.router = Router(self.ledger, self.broker)
        self.router.adopt_broker_cash()
        self.router.create_sub_account(SubAccount(id="4755"), initial_allocation=D("90000"))
        self.broker.set_price("NVDA", D("240"))
        self.reconciler = Reconciler(self.ledger, self.broker)
        # a resting limit buy the mock won't fill by itself
        self.order = self.router.place_order(OrderRequest(
            "4755", "NVDA", OrderSide.BUY, D("319"), order_type=OrderType.LIMIT, limit_price=D("237.5")))

    def fill(self, cum_qty, avg, status="partially_filled"):
        self.router.apply_stream_state(BrokerOrderState(
            broker_order_id=self.order.broker_order_id, client_order_id=self.order.client_order_id,
            status=status, filled_qty=D(cum_qty), filled_avg_price=D(avg), symbol="NVDA"))

    def test_fill_during_mark_refresh_is_kept(self):
        self.fill("155", "237.5")
        self.broker.during_marks = lambda: self.fill("286", "237.5")
        self.reconciler.run()
        pos = self.ledger.get_position("4755", "NVDA")
        self.assertEqual(pos.qty, D("286"))                 # not 155
        self.assertEqual(pos.last_price, D("240"))          # the mark still lands

    def test_position_closed_during_mark_refresh_is_not_resurrected(self):
        self.fill("319", "237.5", status="filled")
        sell = self.router.place_order(OrderRequest(
            "4755", "NVDA", OrderSide.SELL, D("319"), order_type=OrderType.LIMIT, limit_price=D("999")))
        def close():
            self.router.apply_stream_state(BrokerOrderState(
                broker_order_id=sell.broker_order_id, client_order_id=sell.client_order_id,
                status="filled", filled_qty=D("319"), filled_avg_price=D("250"), symbol="NVDA"))
        self.broker.during_marks = close
        self.reconciler.run()
        self.assertEqual(self.ledger.get_position("4755", "NVDA").qty, D("0"))
        self.assertNotIn("NVDA", self.ledger.aggregate_positions())

    def test_fill_during_fee_attribution_keeps_both(self):
        self.fill("155", "237.5")
        cash_before = self.ledger.get_sub_account("4755").cash
        self.broker.extra_activities = [BrokerCashActivity(
            activity_id="fee-1", kind="fee", amount=D("-1.00"), symbol="NVDA",
            order_client_id=self.order.client_order_id)]
        self.broker.during_activities = lambda: self.fill("286", "237.5")
        self.reconciler.attribute_cash_activities()
        acct = self.ledger.get_sub_account("4755")
        # the 131-share fill (-31112.50) AND the fee (-1.00) are both there
        self.assertEqual(acct.cash, cash_before - D("31112.50") - D("1.00"))

    def test_router_and_ledger_share_one_lock(self):
        self.assertIs(self.router._lock, self.ledger.booking_lock)
        self.assertIs(self.reconciler._booking_lock(), self.ledger.booking_lock)


if __name__ == "__main__":
    unittest.main()
