"""Policy (2026-08-31): order qty must be whole shares. Sole exception:
selling an entire existing position (legacy fractional sleeve remainders)."""

import unittest
from decimal import Decimal as D

from subledger import Ledger, Router
from subledger.broker.mock import MockBroker
from subledger.models import OrderRequest, OrderSide, OrderType, SubAccount
from subledger.router import OrderRejected


class WholeShareTests(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker(cash=D("100000"))
        ledger = Ledger(":memory:")
        self.router = Router(ledger, self.broker)
        self.router.adopt_broker_cash()
        self.router.create_sub_account(SubAccount(id="s1"), initial_allocation=D("50000"))
        self.broker.set_price("QQQ", D("700"))
        self.ledger = ledger

    def test_fractional_buy_rejected(self):
        with self.assertRaises(OrderRejected):
            self.router.place_order(OrderRequest(
                "s1", "QQQ", OrderSide.BUY, D("1.5"), order_type=OrderType.MARKET))

    def test_fractional_partial_sell_rejected(self):
        self.router.place_order(OrderRequest(   # notional buy -> fractional position
            "s1", "QQQ", OrderSide.BUY, D("0"), notional=D("10000"),
            order_type=OrderType.MARKET))
        pos = self.ledger.get_position("s1", "QQQ")
        self.assertNotEqual(pos.qty, pos.qty.to_integral_value())
        with self.assertRaises(OrderRejected):
            self.router.place_order(OrderRequest(
                "s1", "QQQ", OrderSide.SELL, pos.qty / 2, order_type=OrderType.MARKET))

    def test_full_fractional_closeout_allowed(self):
        self.router.place_order(OrderRequest(
            "s1", "QQQ", OrderSide.BUY, D("0"), notional=D("10000"),
            order_type=OrderType.MARKET))
        pos = self.ledger.get_position("s1", "QQQ")
        order = self.router.place_order(OrderRequest(
            "s1", "QQQ", OrderSide.SELL, pos.qty, order_type=OrderType.MARKET))
        self.assertEqual(order.qty, pos.qty)            # entire remainder sells

    def test_integer_orders_unaffected(self):
        order = self.router.place_order(OrderRequest(
            "s1", "QQQ", OrderSide.BUY, D("10"), order_type=OrderType.MARKET))
        self.assertEqual(order.filled_qty, D("10"))
