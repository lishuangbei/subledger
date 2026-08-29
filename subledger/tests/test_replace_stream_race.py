"""Regression: the REGN incident (2026-08-19).

Alpaca's replace = cancel + NEW order (new broker id, and without an explicit
client_order_id, a broker-generated UUID name). Two compounding defects lost
a $196k fill: (1) the old order's "replaced" cancel event matched the row by
client id and terminal-ized it 91ms after the row was re-pointed to the
replacement; (2) fills of the replacement arrived under an unknown UUID name
and the broker-id fallback only scanned open orders.
"""

import unittest
from decimal import Decimal as D

from subledger import Ledger, Router
from subledger.broker.base import BrokerOrderState
from subledger.broker.mock import MockBroker
from subledger.models import OrderRequest, OrderSide, OrderType, SubAccount


class ReplaceStreamRaceTests(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker(cash=D("300000"))
        self.ledger = Ledger(":memory:")
        self.router = Router(self.ledger, self.broker)
        self.router.adopt_broker_cash()
        self.router.create_sub_account(SubAccount(id="4757"), initial_allocation=D("250000"))
        self.broker.set_price("REGN", D("830"))   # above both limits: stays open
        self.order = self.router.place_order(OrderRequest(
            "4757", "REGN", OrderSide.BUY, D("240"),
            order_type=OrderType.LIMIT, limit_price=D("820"),
            client_tag="4757-2026-08-18-001-REGN-buy"))
        self.old_broker_id = self.ledger.get_order(self.order.id).broker_order_id

    def _replace(self):
        self.router.replace_order(self.order.id, limit_price=D("822.79"))
        return self.ledger.get_order(self.order.id).broker_order_id

    def test_stale_cancel_event_is_ignored_after_replace(self):
        new_id = self._replace()
        self.assertNotEqual(new_id, self.old_broker_id)
        booked = self.router.apply_stream_state(BrokerOrderState(
            broker_order_id=self.old_broker_id,
            client_order_id=self.order.client_order_id,   # old name, old id
            status="canceled", filled_qty=D("0"),
            filled_avg_price=D("0"), symbol="REGN"))
        self.assertFalse(booked)                          # stale incarnation
        row = self.ledger.get_order(self.order.id)
        self.assertFalse(row.status.is_terminal)          # row survives
        self.assertGreater(row.reserved, D("0"))          # reserve intact

    def test_replacement_fill_books_under_unknown_client_id(self):
        new_id = self._replace()
        # incident ordering: stale cancel first, then the real fill
        self.router.apply_stream_state(BrokerOrderState(
            broker_order_id=self.old_broker_id,
            client_order_id=self.order.client_order_id,
            status="canceled", filled_qty=D("0"),
            filled_avg_price=D("0"), symbol="REGN"))
        booked = self.router.apply_stream_state(BrokerOrderState(
            broker_order_id=new_id,
            client_order_id="c55e61c9-broker-generated-uuid",
            status="filled", filled_qty=D("240"),
            filled_avg_price=D("818.077375"), symbol="REGN"))
        self.assertTrue(booked)
        pos = self.ledger.get_position("4757", "REGN")
        self.assertEqual(pos.qty, D("240"))               # the $196k books

    def test_replacement_carries_derived_client_id(self):
        self._replace()
        sent = self.broker.orders[self.ledger.get_order(self.order.id).broker_order_id]
        cid = str(sent.get("client_order_id") or "")
        self.assertTrue(cid.startswith(self.order.client_order_id + "-r"),
                        "replacement client id should derive from the original: {}".format(cid))
