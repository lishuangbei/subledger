import io
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal as D

from subledger import Ledger, OrderRequest, OrderSide, Reconciler, Router, SubAccount
from subledger.broker.mock import MockBroker
from subledger.cli import run


def make_stack():
    broker = MockBroker(cash=D("100000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    reconciler = Reconciler(ledger, broker)
    return (ledger, broker, router, reconciler)


def invoke(stack, *argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = run(stack, list(argv))
    return code, out.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.ledger, self.broker, self.router, _ = self.stack

    def test_create_update_allocate_delete_lifecycle(self):
        code, out = invoke(self.stack, "--json", "accounts", "create", "alpha",
                           "--allocation", "30000", "--margin", "1.5",
                           "--whitelist", "AAPL,MSFT")
        self.assertEqual(code, 0)
        acct = json.loads(out)
        self.assertEqual(acct["cash"], "30000")
        self.assertEqual(acct["symbol_whitelist"], ["AAPL", "MSFT"])

        code, out = invoke(self.stack, "--json", "accounts", "update", "alpha",
                           "--daily-loss-limit", "500", "--whitelist", "none",
                           "--inactive")
        acct = json.loads(out)
        self.assertEqual(acct["daily_loss_limit"], "500")
        self.assertIsNone(acct["symbol_whitelist"])
        self.assertFalse(acct["active"])

        code, out = invoke(self.stack, "--json", "accounts", "allocate", "alpha", "-10000")
        self.assertEqual(json.loads(out)["cash"], "20000")

        code, out = invoke(self.stack, "--json", "accounts", "delete", "alpha")
        payload = json.loads(out)
        self.assertEqual(payload["cash_returned_to_pool"], "20000")
        self.assertEqual(payload["unallocated_cash"], "100000")

    def test_update_settings_does_not_touch_cash(self):
        invoke(self.stack, "accounts", "create", "alpha", "--allocation", "10000")
        self.ledger.update_sub_account_settings("alpha", margin_multiplier=D("2"))
        acct = self.ledger.get_sub_account("alpha")
        self.assertEqual(acct.cash, D("10000"))
        self.assertEqual(acct.margin_multiplier, D("2"))

    def test_delete_refuses_when_holding(self):
        invoke(self.stack, "accounts", "create", "alpha", "--allocation", "10000")
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("alpha", "AAPL", OrderSide.BUY, D("5")))
        with self.assertRaises(ValueError):
            self.ledger.delete_sub_account("alpha")

    def test_history_shows_fills_with_filters(self):
        invoke(self.stack, "accounts", "create", "alpha", "--allocation", "50000")
        self.broker.set_price("AAPL", D("100"))
        self.broker.set_price("MSFT", D("400"))
        self.router.place_order(OrderRequest("alpha", "AAPL", OrderSide.BUY, D("5")))
        self.router.place_order(OrderRequest("alpha", "MSFT", OrderSide.BUY, D("2")))
        self.router.place_order(OrderRequest("alpha", "AAPL", OrderSide.SELL, D("5")))

        code, out = invoke(self.stack, "--json", "history", "--sub", "alpha")
        rows = json.loads(out)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(D(r["filled_qty"]) > 0 for r in rows))

        code, out = invoke(self.stack, "--json", "history", "--symbol", "AAPL")
        rows = json.loads(out)
        self.assertEqual({r["symbol"] for r in rows}, {"AAPL"})
        self.assertEqual(len(rows), 2)

        code, out = invoke(self.stack, "--json", "orders", "--open")
        self.assertEqual(json.loads(out), [])

    def test_positions_output(self):
        invoke(self.stack, "accounts", "create", "alpha", "--allocation", "50000")
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("alpha", "AAPL", OrderSide.BUY, D("5")))
        code, out = invoke(self.stack, "--json", "positions", "--sub", "alpha")
        rows = json.loads(out)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[0]["qty"], "5")


if __name__ == "__main__":
    unittest.main()


class AllocationCashOnlyTests(unittest.TestCase):
    """Moving capital may only touch FREE CASH — value tied up in positions
    or reserved by open orders must be refused."""

    def setUp(self):
        self.stack = make_stack()
        self.ledger, self.broker, self.router, _ = self.stack
        invoke(self.stack, "accounts", "create", "alpha", "--allocation", "10000")
        self.broker.set_price("AAPL", D("100"))
        # 60% of the cash goes into a position: free cash = 4000
        self.router.place_order(OrderRequest("alpha", "AAPL", OrderSide.BUY, D("60")))

    def test_withdraw_beyond_free_cash_refused(self):
        code, out = invoke(self.stack, "--json", "accounts", "allocate", "alpha", "-9000")
        self.assertEqual(code, 1)
        self.assertIn("free cash", json.loads(out)["error"])
        self.assertEqual(self.ledger.get_sub_account("alpha").cash, D("4000"))

    def test_withdraw_within_free_cash_ok(self):
        code, out = invoke(self.stack, "--json", "accounts", "allocate", "alpha", "-4000")
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger.get_sub_account("alpha").cash, D("0"))

    def test_reserved_cash_also_locked(self):
        from subledger.models import OrderRequest as OR, OrderType, TimeInForce
        # resting limit buy reserves 2000 of the remaining 4000
        self.router.place_order(OR("alpha", "AAPL", OrderSide.BUY, D("20"),
                                   order_type=OrderType.LIMIT, limit_price=D("100"),
                                   time_in_force=TimeInForce.GTC))
        code, out = invoke(self.stack, "--json", "accounts", "allocate", "alpha", "-3000")
        self.assertEqual(code, 1)          # only 2000 free now
        code, out = invoke(self.stack, "--json", "accounts", "allocate", "alpha", "-2000")
        self.assertEqual(code, 0)

    def test_margin_negative_cash_blocks_all_withdrawals(self):
        invoke(self.stack, "accounts", "create", "levered", "--allocation", "10000",
               "--margin", "2")
        self.router.place_order(OrderRequest("levered", "AAPL", OrderSide.BUY, D("150")))
        self.assertTrue(self.ledger.get_sub_account("levered").cash < 0)
        code, out = invoke(self.stack, "--json", "accounts", "allocate", "levered", "-1")
        self.assertEqual(code, 1)

    def test_pool_overdraw_refused(self):
        code, out = invoke(self.stack, "--json", "accounts", "allocate", "alpha", "999999")
        self.assertEqual(code, 1)
        self.assertIn("unallocated", json.loads(out)["error"])


class EquityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.ledger, self.broker, self.router, _ = self.stack
        invoke(self.stack, "accounts", "create", "alpha", "--allocation", "30000")
        invoke(self.stack, "accounts", "create", "beta", "--allocation", "10000")
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("alpha", "AAPL", OrderSide.BUY, D("50")))

    def test_snapshot_and_query(self):
        rows_written = self.router.snapshot_equity_history()
        self.assertEqual(rows_written, 2)
        self.broker.set_price("AAPL", D("110"))
        pos = self.ledger.get_position("alpha", "AAPL")
        pos.last_price = D("110")
        self.ledger.save_position(pos)
        self.router.snapshot_equity_history()

        history = self.ledger.equity_history("alpha")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["equity"], "30500")   # 25000 + 50x110
        self.assertEqual(history[1]["equity"], "30000")
        self.assertEqual(history[0]["unrealized_pnl"], "500")

        code, out = invoke(self.stack, "--json", "equity", "--sub", "alpha", "--limit", "1")
        rows = json.loads(out)
        self.assertEqual(rows[0]["sub_account_id"], "alpha")
        self.assertEqual(rows[0]["equity"], "30500")

    def test_eager_sync_off_defers_booking_to_stream(self):
        from subledger import Router as R
        router2 = R(self.ledger, self.broker, eager_sync=False)
        order = router2.place_order(OrderRequest("beta", "AAPL", OrderSide.BUY, D("5")))
        self.assertEqual(order.status.value, "open")      # mock filled it broker-side...
        router2.sync()                                     # ...booking arrives via poll/stream
        self.assertEqual(self.ledger.get_order(order.id).status.value, "filled")

    def test_refresh_marks_updates_last_price(self):
        self.broker.set_price("AAPL", D("123"))
        updated = self.router.refresh_marks()
        self.assertEqual(updated, 1)
        pos = self.ledger.get_position("alpha", "AAPL")
        self.assertEqual(pos.last_price, D("123"))
        self.router.snapshot_equity_history()
        row = self.ledger.equity_history("alpha", limit=1)[0]
        self.assertEqual(row["positions_value"], "6150")   # 50 x 123


class AccountVerificationTests(unittest.TestCase):
    def test_matching_account_passes(self):
        from subledger import Ledger, Router
        broker = MockBroker(cash=D("1000"))
        router = Router(Ledger(":memory:"), broker,
                        expected_account_id="mock-account-1")
        self.assertIsNotNone(router)

    def test_mismatched_account_refuses_to_start(self):
        from subledger import Ledger, Router
        from subledger.router import AccountMismatch
        broker = MockBroker(cash=D("1000"))
        broker.account_id = "someone-elses-live-account"
        with self.assertRaises(AccountMismatch):
            Router(Ledger(":memory:"), broker,
                   expected_account_id="mock-account-1")


class StatusCommandTests(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        self.ledger, self.broker, self.router, _ = self.stack
        invoke(self.stack, "accounts", "create", "alpha", "--allocation", "30000",
               "--margin", "1.5")
        self.broker.set_price("AAPL", D("100"))
        self.router.place_order(OrderRequest("alpha", "AAPL", OrderSide.BUY, D("50")))

    def test_single_account_status(self):
        code, out = invoke(self.stack, "--json", "status", "alpha")
        self.assertEqual(code, 0)
        view = json.loads(out)
        self.assertEqual(view["equity"], "30000")
        # bp = equity x 1.5 - gross - reserved = 45000 - 5000 = 40000
        self.assertEqual(view["buying_power"], "40000.0")
        self.assertEqual(view["positions"][0]["symbol"], "AAPL")
        self.assertEqual(view["open_orders"], [])

    def test_all_accounts_status(self):
        code, out = invoke(self.stack, "--json", "status", "--all")
        payload = json.loads(out)
        self.assertEqual(len(payload["accounts"]), 1)
        self.assertEqual(payload["unallocated_cash"], "70000")
        self.assertEqual(payload["total_equity"], "100000")
        self.assertFalse(payload["halted"])

    def test_status_without_target_errors(self):
        code, out = invoke(self.stack, "--json", "status")
        self.assertEqual(code, 1)
        self.assertIn("--all", json.loads(out)["error"])

    def test_status_shows_instance_identity(self):
        code, out = invoke(self.stack, "--json", "status", "--all")
        identity = json.loads(out)["instance"]
        self.assertEqual(identity["broker"], "mock")
        self.assertEqual(identity["mode"], "sim")
        self.assertEqual(identity["account_id"], "mock-account-1")
        self.assertEqual(identity["ledger"], ":memory:")


class WriterLockTests(unittest.TestCase):
    def test_second_writer_refused(self):
        import tempfile, os
        from subledger.ledger import Ledger as L, LedgerLocked
        db = os.path.join(tempfile.mkdtemp(), "lock.db")
        first = L(db)                                  # holds the lock
        with self.assertRaises(LedgerLocked) as caught:
            L(db)                                      # second writer refused
        self.assertIn("ONE booking writer", str(caught.exception))
        # read/light-op mode coexists
        reader = L(db, exclusive_writer=False)
        self.assertIsNotNone(reader)

    def test_memory_ledgers_never_lock(self):
        from subledger.ledger import Ledger as L
        a, b = L(":memory:"), L(":memory:")
        self.assertIsNotNone(a); self.assertIsNotNone(b)
