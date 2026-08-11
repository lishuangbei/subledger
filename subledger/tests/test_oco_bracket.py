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
from subledger.broker.mock import MockBroker
from subledger.models import TimeInForce
from subledger.router import OrderRejected


def make_stack():
    broker = MockBroker(cash=D("100000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    router.create_sub_account(SubAccount(id="s1"), initial_allocation=D("50000"))
    router.create_sub_account(SubAccount(id="s2"), initial_allocation=D("20000"))
    return broker, ledger, router


def place_oco(router, sub="s1", qty="10", tp="120", sl="90"):
    return router.place_order(OrderRequest(
        sub, "AAPL", OrderSide.SELL, D(qty),
        order_type=OrderType.LIMIT,
        order_class=OrderClass.OCO,
        take_profit=TakeProfitSpec(limit_price=D(tp)),
        stop_loss=StopLossSpec(stop_price=D(sl)),
        time_in_force=TimeInForce.GTC,
    ))


class OcoTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("s1", "AAPL", OrderSide.BUY, D("10")))

    def test_oco_reserves_shares_once(self):
        parent = place_oco(self.router)
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.reserved_qty, D("10"))     # one reserve for two legs
        legs = self.ledger.list_legs(parent.id)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].leg_role, "stop_loss")
        # protected shares cannot be sold by anything else
        with self.assertRaises(OrderRejected):
            self.router.place_order(
                OrderRequest("s1", "AAPL", OrderSide.SELL, D("1")), est_price=D("100"))

    def test_take_profit_path(self):
        parent = place_oco(self.router)
        self.broker.set_price("AAPL", D("121"))
        self.router.sync()
        parent = self.ledger.get_order(parent.id)
        leg = self.ledger.list_legs(parent.id)[0]
        self.assertEqual(parent.status.value, "filled")
        self.assertEqual(self.ledger.get_order(leg.id).status.value, "canceled")
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("0"))
        self.assertEqual(pos.reserved_qty, D("0"))
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.realized_pnl, D("200"))   # (120-100) x 10

    def test_stop_loss_path(self):
        parent = place_oco(self.router)
        self.broker.set_price("AAPL", D("89"))
        self.router.sync()
        parent = self.ledger.get_order(parent.id)
        leg = self.ledger.list_legs(parent.id)[0]
        self.assertEqual(self.ledger.get_order(leg.id).status.value, "filled")
        self.assertEqual(parent.status.value, "canceled")
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("0"))
        self.assertEqual(pos.reserved_qty, D("0"))
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.realized_pnl, D("-110"))  # (89-100) x 10

    def test_cancel_parent_releases_reserve(self):
        parent = place_oco(self.router)
        self.router.cancel_order(parent.id)
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("10"))
        self.assertEqual(pos.reserved_qty, D("0"))
        for record in [self.ledger.get_order(parent.id)] + self.ledger.list_legs(parent.id):
            self.assertTrue(record.status.is_terminal)

    def test_oco_needs_owned_shares(self):
        with self.assertRaises(OrderRejected):
            place_oco(self.router, sub="s2")            # s2 holds no AAPL


class BracketTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))

    def place_bracket(self, qty="10", tp="120", sl="90"):
        return self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.BUY, D(qty),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitSpec(limit_price=D(tp)),
            stop_loss=StopLossSpec(stop_price=D(sl)),
            time_in_force=TimeInForce.GTC,
        ))

    def test_bracket_entry_reserves_exit_shares(self):
        entry = self.place_bracket()
        entry = self.ledger.get_order(entry.id)
        self.assertEqual(entry.status.value, "filled")
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("10"))
        self.assertEqual(pos.reserved_qty, D("10"))     # protected by the legs
        legs = self.ledger.list_legs(entry.id)
        self.assertEqual({l.leg_role for l in legs}, {"take_profit", "stop_loss"})
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.reserved_cash, D("0"))    # entry reserve released

    def test_bracket_take_profit_fill(self):
        entry = self.place_bracket()
        self.broker.set_price("AAPL", D("121"))
        self.router.sync()
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("0"))
        self.assertEqual(pos.reserved_qty, D("0"))
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.realized_pnl, D("200"))
        statuses = {l.leg_role: self.ledger.get_order(l.id).status.value
                    for l in self.ledger.list_legs(entry.id)}
        self.assertEqual(statuses["take_profit"], "filled")
        self.assertEqual(statuses["stop_loss"], "canceled")

    def test_bracket_stop_fill(self):
        self.place_bracket()
        self.broker.set_price("AAPL", D("88"))
        self.router.sync()
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("0"))
        self.assertEqual(pos.reserved_qty, D("0"))
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.realized_pnl, D("-120"))  # (88-100) x 10

    def test_oto_single_leg(self):
        entry = self.router.place_order(OrderRequest(
            "s1", "AAPL", OrderSide.BUY, D("10"),
            order_class=OrderClass.OTO,
            stop_loss=StopLossSpec(stop_price=D("90")),
        ))
        legs = self.ledger.list_legs(entry.id)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].leg_role, "stop_loss")
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.reserved_qty, D("10"))


if __name__ == "__main__":
    unittest.main()
