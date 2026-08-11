"""Router + ledger accounting tests. Pure stdlib: python3 -m unittest discover tests"""

import unittest
from decimal import Decimal

from subledger import Ledger, OrderRequest, OrderSide, OrderType, Router, SubAccount, TimeInForce
from subledger.broker.mock import MockBroker
from subledger.models import OrderStatus
from subledger.router import OrderRejected

D = Decimal


def make_stack(cash="100000"):
    broker = MockBroker(cash=D(cash))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    return broker, ledger, router


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.broker, self.ledger, self.router = make_stack()
        self.router.create_sub_account(
            SubAccount(id="alpha", name="momentum"), initial_allocation=D("30000")
        )
        self.router.create_sub_account(
            SubAccount(id="beta", name="meanrev"), initial_allocation=D("20000")
        )
        self.broker.set_price("AAPL", D("200"))
        self.broker.set_price("SPY", D("500"))

    def buy(self, sub, symbol, qty):
        return self.router.place_order(
            OrderRequest(sub_account_id=sub, symbol=symbol, side=OrderSide.BUY, qty=D(qty))
        )

    def sell(self, sub, symbol, qty):
        return self.router.place_order(
            OrderRequest(sub_account_id=sub, symbol=symbol, side=OrderSide.SELL, qty=D(qty))
        )

    def test_allocation_conservation(self):
        self.assertEqual(self.ledger.unallocated_cash(), D("50000"))
        self.assertEqual(self.ledger.get_sub_account("alpha").cash, D("30000"))
        with self.assertRaises(ValueError):
            self.ledger.allocate("alpha", D("60000"))  # more than pool

    def test_buy_fill_updates_ledger(self):
        order = self.buy("alpha", "AAPL", "10")
        self.assertEqual(order.status, OrderStatus.FILLED)
        acct = self.ledger.get_sub_account("alpha")
        self.assertEqual(acct.cash, D("28000"))  # 30000 - 10*200
        self.assertEqual(acct.reserved_cash, D("0"))
        pos = self.ledger.get_position("alpha", "AAPL")
        self.assertEqual(pos.qty, D("10"))
        self.assertEqual(pos.avg_cost, D("200"))

    def test_avg_cost_across_two_buys(self):
        self.buy("alpha", "AAPL", "10")
        self.broker.set_price("AAPL", D("220"))
        self.buy("alpha", "AAPL", "10")
        pos = self.ledger.get_position("alpha", "AAPL")
        self.assertEqual(pos.qty, D("20"))
        self.assertEqual(pos.avg_cost, D("210"))

    def test_sell_realizes_pnl(self):
        self.buy("alpha", "AAPL", "10")
        self.broker.set_price("AAPL", D("250"))
        self.sell("alpha", "AAPL", "10")
        acct = self.ledger.get_sub_account("alpha")
        self.assertEqual(acct.realized_pnl, D("500"))  # (250-200)*10
        self.assertEqual(acct.cash, D("30500"))
        self.assertEqual(self.ledger.get_position("alpha", "AAPL").qty, D("0"))

    def test_isolation_between_sub_accounts(self):
        """beta cannot sell shares that alpha owns."""
        self.buy("alpha", "AAPL", "10")
        with self.assertRaises(OrderRejected):
            self.sell("beta", "AAPL", "5")
        # and alpha's ledger is untouched by the rejection
        self.assertEqual(self.ledger.get_position("alpha", "AAPL").qty, D("10"))

    def test_buying_power_enforced(self):
        with self.assertRaises(OrderRejected):
            self.buy("beta", "SPY", "41")  # 41*500 = 20500 > 20000
        self.buy("beta", "SPY", "40")  # exactly 20000: fine
        acct = self.ledger.get_sub_account("beta")
        self.assertEqual(acct.cash, D("0"))

    def test_margin_multiplier(self):
        self.router.create_sub_account(
            SubAccount(id="lev", margin_multiplier=D("2")), initial_allocation=D("10000")
        )
        self.buy("lev", "AAPL", "100")  # 20000 notional on 10000 cash: ok at 2x
        self.assertEqual(self.ledger.get_sub_account("lev").cash, D("-10000"))
        with self.assertRaises(OrderRejected):
            self.buy("lev", "AAPL", "1")  # fully levered now

    def test_open_limit_order_reserves_cash_and_release_on_cancel(self):
        req = OrderRequest(
            sub_account_id="alpha",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=D("10"),
            order_type=OrderType.LIMIT,
            limit_price=D("190"),  # below market: rests open
            time_in_force=TimeInForce.GTC,
        )
        order = self.router.place_order(req)
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(self.ledger.get_sub_account("alpha").reserved_cash, D("1900"))
        # reserved cash counts against buying power
        with self.assertRaises(OrderRejected):
            self.buy("alpha", "SPY", "57")  # 28500 > 30000-1900
        self.router.cancel_order(order.id)
        acct = self.ledger.get_sub_account("alpha")
        self.assertEqual(acct.reserved_cash, D("0"))
        self.assertEqual(acct.cash, D("30000"))

    def test_limit_fill_on_price_cross(self):
        req = OrderRequest(
            sub_account_id="alpha",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=D("10"),
            order_type=OrderType.LIMIT,
            limit_price=D("190"),
        )
        order = self.router.place_order(req)
        self.broker.set_price("AAPL", D("185"))  # crosses the limit
        self.router.sync()
        order = self.ledger.get_order(order.id)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(self.ledger.get_position("alpha", "AAPL").avg_cost, D("190"))
        self.assertEqual(self.ledger.get_sub_account("alpha").cash, D("28100"))

    def test_sell_reserves_shares(self):
        self.buy("alpha", "AAPL", "10")
        self.broker.fill_market_orders = False
        self.sell("alpha", "AAPL", "6")  # rests open, reserves 6 shares
        with self.assertRaises(OrderRejected):
            self.sell("alpha", "AAPL", "5")  # only 4 sellable left

    def test_halt_blocks_orders(self):
        self.router.halt()
        with self.assertRaises(OrderRejected):
            self.buy("alpha", "AAPL", "1")
        self.router.resume()
        self.buy("alpha", "AAPL", "1")

    def test_daily_loss_limit_blocks_new_buys(self):
        self.router.create_sub_account(
            SubAccount(id="risky", daily_loss_limit=D("300")), initial_allocation=D("10000")
        )
        self.buy("risky", "AAPL", "10")
        self.broker.set_price("AAPL", D("160"))
        self.sell("risky", "AAPL", "10")  # realizes -400
        self.assertEqual(
            self.ledger.get_sub_account("risky").realized_pnl_today, D("-400")
        )
        with self.assertRaises(OrderRejected):
            self.buy("risky", "AAPL", "1")
        # after daily reset, trading resumes
        self.router.reset_daily()
        self.buy("risky", "AAPL", "1")

    def test_whitelist_and_notional_cap(self):
        self.router.create_sub_account(
            SubAccount(
                id="capped",
                symbol_whitelist=["SPY"],
                max_order_notional=D("5000"),
            ),
            initial_allocation=D("10000"),
        )
        with self.assertRaises(OrderRejected):
            self.buy("capped", "AAPL", "1")  # not whitelisted
        with self.assertRaises(OrderRejected):
            self.buy("capped", "SPY", "11")  # 5500 > 5000 cap
        self.buy("capped", "SPY", "10")

    def test_rejected_orders_are_recorded(self):
        with self.assertRaises(OrderRejected):
            self.buy("alpha", "AAPL", "1000")  # way over budget
        rejected = [
            o
            for o in self.ledger.list_orders("alpha")
            if o.status == OrderStatus.REJECTED
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn("buying power", rejected[0].reject_reason)


if __name__ == "__main__":
    unittest.main()
