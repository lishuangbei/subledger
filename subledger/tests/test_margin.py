import unittest
from decimal import Decimal as D

from subledger import Ledger, OrderRequest, OrderSide, Router, SubAccount
from subledger.broker.mock import MockBroker
from subledger.models import Position, ZERO
from subledger.risk import buying_power
from subledger.router import OrderRejected


class BuyingPowerFormulaTests(unittest.TestCase):
    def test_cash_account_reduces_to_free_cash(self):
        acct = SubAccount(id="a", cash=D("10000"), reserved_cash=D("2000"),
                          margin_multiplier=D("1"))
        # with positions or without, cash account bp == cash - reserved
        self.assertEqual(buying_power(acct), D("8000"))
        self.assertEqual(buying_power(acct, gross_exposure=D("50000")), D("8000"))

    def test_margin_bp_shrinks_with_losses(self):
        acct = SubAccount(id="a", cash=D("0"), margin_multiplier=D("1.5"))
        # equity 10k in positions: bp = 15k - 10k = 5k
        self.assertEqual(buying_power(acct, D("10000")), D("5000"))
        # positions lose 20% -> equity 8k: bp = 12k - 8k = 4k  (shrunk)
        self.assertEqual(buying_power(acct, D("8000")), D("4000"))
        # positions gain -> equity 12k: bp = 18k - 12k = 6k    (grew)
        self.assertEqual(buying_power(acct, D("12000")), D("6000"))

    def test_nominal_multiplier_fully_reachable(self):
        # all-cash 10k at 1.5x: max gross = 15k (bp hits 0 exactly there)
        acct = SubAccount(id="a", cash=D("-5000"), margin_multiplier=D("1.5"))
        self.assertEqual(buying_power(acct, D("15000")), ZERO)


class MarginIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker(cash=D("100000"))
        self.ledger = Ledger(":memory:")
        self.router = Router(self.ledger, self.broker)
        self.router.adopt_broker_cash()
        self.router.create_sub_account(
            SubAccount(id="lev", margin_multiplier=D("1.5")),
            initial_allocation=D("30000"))
        self.broker.set_price("AAPL", D("100"))

    def test_three_sequential_buys_reach_full_leverage(self):
        # 3 x 15k = 45k gross on 30k equity = exactly 1.5x — previously
        # impossible under the cash-based formula (third order was rejected).
        for _ in range(3):
            self.router.place_order(OrderRequest("lev", "AAPL", OrderSide.BUY, D("150")))
        pos = self.ledger.get_position("lev", "AAPL")
        self.assertEqual(pos.qty, D("450"))
        acct = self.ledger.get_sub_account("lev")
        self.assertEqual(acct.cash, D("-15000"))

    def test_beyond_nominal_leverage_rejected(self):
        for _ in range(3):
            self.router.place_order(OrderRequest("lev", "AAPL", OrderSide.BUY, D("150")))
        with self.assertRaises(OrderRejected):
            self.router.place_order(OrderRequest("lev", "AAPL", OrderSide.BUY, D("1")))

    def test_loss_blocks_new_exposure(self):
        for _ in range(3):
            self.router.place_order(OrderRequest("lev", "AAPL", OrderSide.BUY, D("150")))
        # sell 150 to free capacity: cash -15k + 15k = 0, gross 30k
        self.router.place_order(OrderRequest("lev", "AAPL", OrderSide.SELL, D("150")))
        # bp = 30k*1.5 - 30k = 15k -> a 10k buy would fit at current prices
        # ...but a 40% crash first: gross 18k, equity 3k, bp = 4.5k-... 
        self.broker.set_price("AAPL", D("60"))
        self.router.sync()
        # refresh marks like the reconciler would
        pos = self.ledger.get_position("lev", "AAPL")
        pos.last_price = D("60")
        self.ledger.save_position(pos)
        # equity = 0 + 18k = 18k; bp = 27k - 18k = 9k: 10k buy now rejected
        with self.assertRaises(OrderRejected):
            self.router.place_order(OrderRequest("lev", "AAPL", OrderSide.BUY, D("167")))
        # a smaller buy within shrunken bp still passes
        order = self.router.place_order(OrderRequest("lev", "AAPL", OrderSide.BUY, D("100")))
        self.assertEqual(order.status.value, "filled")


if __name__ == "__main__":
    unittest.main()
