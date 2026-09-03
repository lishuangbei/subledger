"""Cash call: admin reclaims capital from a sub-account.

  issue   -> free cash moves now; remainder = outstanding call (buying frozen)
  sweep   -> free cash arriving later is moved toward the call each cycle
  enforce -> past the deadline, with the market open and the router not
             halted, positions are sold mechanically (largest first, whole
             shares) until the deficit is covered
"""

import unittest
from decimal import Decimal as D

from subledger import Ledger, Router
from subledger.broker.mock import MockBroker
from subledger.models import OrderRequest, OrderSide, OrderType, SubAccount
from subledger.router import OrderRejected

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


class CashCallBase(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker(cash=D("200000"))
        self.ledger = Ledger(":memory:")
        self.router = Router(self.ledger, self.broker)
        self.router.adopt_broker_cash()
        self.router.create_sub_account(SubAccount(id="s1"), initial_allocation=D("100000"))
        self.broker.set_price("AAA", D("100"))
        self.broker.set_price("BBB", D("50"))
        # s1 holds 500 AAA (50k) + 400 BBB (20k), 30k cash left
        self.router.place_order(OrderRequest("s1", "AAA", OrderSide.BUY, D("500"),
                                             order_type=OrderType.MARKET))
        self.router.place_order(OrderRequest("s1", "BBB", OrderSide.BUY, D("400"),
                                             order_type=OrderType.MARKET))
        self.router.refresh_marks()

    def acct(self):
        return self.ledger.get_sub_account("s1")


class IssueTests(CashCallBase):
    def test_free_cash_moves_now_rest_becomes_call(self):
        pool_before = self.ledger.unallocated_cash()
        res = self.ledger.issue_cash_call("s1", D("50000"), FUTURE, "trim")
        self.assertEqual(D(res["moved_now"]), D("30000"))
        self.assertEqual(D(res["deficit"]), D("20000"))
        self.assertEqual(self.ledger.unallocated_cash() - pool_before, D("30000"))
        self.assertEqual(self.acct().cash, D("0"))
        self.assertEqual(self.acct().cash_call, D("20000"))
        self.assertEqual(self.acct().cash_call_deadline, FUTURE)

    def test_fully_covered_call_settles_immediately(self):
        res = self.ledger.issue_cash_call("s1", D("10000"), FUTURE)
        self.assertEqual(D(res["deficit"]), D("0"))
        self.assertEqual(self.acct().cash_call, D("0"))
        self.assertEqual(self.ledger.cash_call_history("s1")[0]["resolution"],
                         "settled_immediately")

    def test_buying_frozen_while_outstanding(self):
        self.ledger.issue_cash_call("s1", D("50000"), FUTURE)
        with self.assertRaises(OrderRejected) as ctx:
            self.router.place_order(OrderRequest("s1", "AAA", OrderSide.BUY, D("1"),
                                                 order_type=OrderType.MARKET))
        self.assertIn("cash call outstanding", str(ctx.exception))
        # selling stays allowed (that's how the call gets repaid)
        self.router.place_order(OrderRequest("s1", "BBB", OrderSide.SELL, D("10"),
                                             order_type=OrderType.MARKET))


class SweepTests(CashCallBase):
    def test_sweep_moves_arriving_cash_and_resolves(self):
        self.ledger.issue_cash_call("s1", D("50000"), FUTURE)
        self.router.place_order(OrderRequest("s1", "BBB", OrderSide.SELL, D("400"),
                                             order_type=OrderType.MARKET))   # +20k
        swept = self.ledger.sweep_cash_call("s1")
        self.assertEqual(swept, D("20000"))
        self.assertEqual(self.acct().cash_call, D("0"))
        self.assertIsNone(self.acct().cash_call_deadline)
        self.assertEqual(self.ledger.cash_call_history("s1")[0]["resolution"], "swept")

    def test_partial_sweep_keeps_call_open(self):
        self.ledger.issue_cash_call("s1", D("50000"), FUTURE)
        self.router.place_order(OrderRequest("s1", "BBB", OrderSide.SELL, D("100"),
                                             order_type=OrderType.MARKET))   # +5k
        self.assertEqual(self.ledger.sweep_cash_call("s1"), D("5000"))
        self.assertEqual(self.acct().cash_call, D("15000"))


class EnforceTests(CashCallBase):
    def test_no_liquidation_before_deadline(self):
        self.ledger.issue_cash_call("s1", D("50000"), FUTURE)
        rep = self.router.enforce_cash_calls(market_open=True)
        self.assertEqual(rep["liquidated"], {})
        self.assertEqual(self.acct().cash_call, D("20000"))

    def test_overdue_liquidates_largest_first_whole_shares(self):
        self.ledger.issue_cash_call("s1", D("50000"), PAST)
        rep = self.router.enforce_cash_calls(market_open=True)
        actions = rep["liquidated"]["s1"]
        self.assertEqual(actions[0]["symbol"], "AAA")          # 50k position first
        self.assertEqual(actions[0]["qty"], "201")             # 20k/100 + 1, whole
        self.assertEqual(len(actions), 1)                      # BBB untouched
        # mock fills instantly -> next enforce sweeps and resolves
        rep2 = self.router.enforce_cash_calls(market_open=True)
        self.assertEqual(self.acct().cash_call, D("0"))
        self.assertEqual(self.ledger.get_position("s1", "AAA").qty, D("299"))

    def test_overdue_but_market_closed_defers(self):
        self.ledger.issue_cash_call("s1", D("50000"), PAST)
        rep = self.router.enforce_cash_calls(market_open=False)
        self.assertEqual(rep["deferred"], ["s1"])
        self.assertEqual(self.acct().cash_call, D("20000"))

    def test_halt_wins_over_enforcement(self):
        self.ledger.issue_cash_call("s1", D("50000"), PAST)
        self.router.halt()
        rep = self.router.enforce_cash_calls(market_open=True)
        self.assertEqual(rep["deferred"], ["s1"])
        self.router.resume()

    def test_liquidation_cancels_protection_on_sold_symbol_only(self):
        from subledger.models import OrderClass, StopLossSpec, TakeProfitSpec, TimeInForce
        for sym, tp, sl in (("AAA", "120", "90"), ("BBB", "60", "45")):
            self.router.place_order(OrderRequest(
                "s1", sym, OrderSide.SELL, self.ledger.get_position("s1", sym).qty,
                order_type=OrderType.LIMIT, order_class=OrderClass.OCO,
                take_profit=TakeProfitSpec(limit_price=D(tp)),
                stop_loss=StopLossSpec(stop_price=D(sl)), time_in_force=TimeInForce.GTC))
        self.ledger.issue_cash_call("s1", D("50000"), PAST)
        self.router.enforce_cash_calls(market_open=True)
        open_syms = {o.symbol for o in self.ledger.list_orders("s1", open_only=True)
                     if o.parent_order_id is None}
        self.assertNotIn("AAA", open_syms)     # sold -> its OCO canceled
        self.assertIn("BBB", open_syms)        # untouched -> still protected

    def test_disabled_account_can_still_be_liquidated(self):
        acct = self.acct()
        acct.active = False
        self.ledger.save_sub_account(acct)
        self.ledger.issue_cash_call("s1", D("50000"), PAST)
        rep = self.router.enforce_cash_calls(market_open=True)
        self.assertNotIn("rejected", rep["liquidated"]["s1"][0])


class RestTests(unittest.TestCase):
    def test_cash_call_endpoint(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")
        from subledger import Reconciler
        from subledger.api import create_app

        broker = MockBroker(cash=D("100000"))
        ledger = Ledger(":memory:")
        router = Router(ledger, broker)
        router.adopt_broker_cash()
        client = TestClient(create_app(stack=(ledger, broker, router, Reconciler(ledger, broker)),
                                       background_loops=False))
        client.post("/accounts", json={"id": "t1", "allocation": "50000"})
        r = client.post("/accounts/t1/cash_call", json={"amount": "60000", "note": "x"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(D(r.json()["moved_now"]), D("50000"))
        self.assertEqual(D(r.json()["deficit"]), D("10000"))
        self.assertIsNotNone(r.json()["deadline"])          # default deadline filled
        view = client.get("/accounts/t1").json()
        self.assertEqual(D(view["cash_call"]), D("10000"))
        self.assertEqual(len(client.get("/cash_calls").json()), 1)
