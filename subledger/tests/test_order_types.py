import unittest
from decimal import Decimal as D

from subledger import Ledger, OrderRequest, OrderSide, OrderType, Router, SubAccount
from subledger.broker.mock import MockBroker
from subledger.models import TimeInForce
from subledger.router import OrderRejected


def make_stack(cash="100000"):
    broker = MockBroker(cash=D(cash))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    router.create_sub_account(SubAccount(id="s1"), initial_allocation=D("50000"))
    return broker, ledger, router


class StopOrderTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("s1", "AAPL", OrderSide.BUY, D("10")))

    def test_stop_sell_triggers_on_price_drop(self):
        order = self.router.place_order(
            OrderRequest("s1", "AAPL", OrderSide.SELL, D("10"),
                         order_type=OrderType.STOP, stop_price=D("95"),
                         time_in_force=TimeInForce.GTC))
        self.assertEqual(order.status.value, "open")
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.reserved_qty, D("10"))

        self.broker.set_price("AAPL", D("96"))
        self.router.sync()
        self.assertEqual(self.ledger.get_order(order.id).status.value, "open")

        self.broker.set_price("AAPL", D("94"))
        self.router.sync()
        order = self.ledger.get_order(order.id)
        self.assertEqual(order.status.value, "filled")
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("0"))
        self.assertEqual(pos.reserved_qty, D("0"))

    def test_stop_limit_fills_at_limit(self):
        order = self.router.place_order(
            OrderRequest("s1", "AAPL", OrderSide.SELL, D("10"),
                         order_type=OrderType.STOP_LIMIT,
                         stop_price=D("95"), limit_price=D("94.5")))
        self.broker.set_price("AAPL", D("94.7"))
        self.router.sync()
        order = self.ledger.get_order(order.id)
        self.assertEqual(order.status.value, "filled")
        self.assertEqual(order.filled_avg_price, D("94.5"))

    def test_trailing_stop_sell(self):
        order = self.router.place_order(
            OrderRequest("s1", "AAPL", OrderSide.SELL, D("10"),
                         order_type=OrderType.TRAILING_STOP, trail_percent=D("5")))
        self.broker.set_price("AAPL", D("120"))   # high-water mark rises
        self.router.sync()
        self.assertEqual(self.ledger.get_order(order.id).status.value, "open")
        self.broker.set_price("AAPL", D("113"))   # 120 * 0.95 = 114 -> triggered
        self.router.sync()
        self.assertEqual(self.ledger.get_order(order.id).status.value, "filled")

    def test_stop_requires_stop_price(self):
        with self.assertRaises(OrderRejected):
            self.router.place_order(
                OrderRequest("s1", "AAPL", OrderSide.SELL, D("10"),
                             order_type=OrderType.STOP))


class NotionalOrderTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("200"))

    def test_notional_buy_fills_fractional(self):
        order = self.router.place_order(
            OrderRequest("s1", "AAPL", OrderSide.BUY, notional=D("1000")))
        order = self.ledger.get_order(order.id)
        self.assertEqual(order.status.value, "filled")
        pos = self.ledger.get_position("s1", "AAPL")
        self.assertEqual(pos.qty, D("5"))  # 1000 / 200
        acct = self.ledger.get_sub_account("s1")
        self.assertEqual(acct.cash, D("49000"))
        self.assertEqual(acct.reserved_cash, D("0"))

    def test_notional_sell_rejected(self):
        with self.assertRaises(OrderRejected):
            self.router.place_order(
                OrderRequest("s1", "AAPL", OrderSide.SELL, notional=D("1000")))

    def test_qty_and_notional_both_rejected(self):
        with self.assertRaises(OrderRejected):
            self.router.place_order(
                OrderRequest("s1", "AAPL", OrderSide.BUY, D("5"), notional=D("1000")))


class ExtendedHoursTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))

    def test_extended_hours_market_rejected(self):
        with self.assertRaises(OrderRejected):
            self.router.place_order(
                OrderRequest("s1", "AAPL", OrderSide.BUY, D("1"), extended_hours=True))

    def test_extended_hours_day_limit_ok(self):
        order = self.router.place_order(
            OrderRequest("s1", "AAPL", OrderSide.BUY, D("1"),
                         order_type=OrderType.LIMIT, limit_price=D("99"),
                         extended_hours=True))
        self.assertEqual(order.status.value, "open")


class ClientTagTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.broker.set_price("AAPL", D("100"))

    def test_deterministic_client_id_and_lookup(self):
        order = self.router.place_order(
            OrderRequest("s1", "AAPL", OrderSide.BUY, D("1"),
                         client_tag="alpha-20260807-AAPL-entry"))
        self.assertEqual(order.client_order_id, "sl.s1.alpha-20260807-AAPL-entry")
        found = self.ledger.get_order_by_client_id("sl.s1.alpha-20260807-AAPL-entry")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, order.id)

    def test_invalid_tag_rejected(self):
        with self.assertRaises(OrderRejected):
            self.router.place_order(
                OrderRequest("s1", "AAPL", OrderSide.BUY, D("1"),
                             client_tag="bad.tag.with.dots"))


if __name__ == "__main__":
    unittest.main()
