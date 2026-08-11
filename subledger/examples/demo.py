"""End-to-end demo against the in-memory MockBroker — no credentials needed.

    cd subledger && python3 examples/demo.py

Shows: allocation, two isolated strategies trading the same account, a risk
rejection, an external (manual) trade, and the reconciler catching it.
"""

import logging
import sys
from decimal import Decimal

sys.path.insert(0, ".")

from subledger import Ledger, OrderRequest, OrderSide, Reconciler, Router, SubAccount

from subledger.broker.mock import MockBroker
from subledger.router import OrderRejected

D = Decimal
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")


def show(router, ledger, sub_id):
    snap = router.equity_snapshot(sub_id)
    acct = ledger.get_sub_account(sub_id)
    print(
        "  {:8s} cash={:>10} positions={:>10} equity={:>10} realized={:>8} unrealized={:>8}".format(
            sub_id,
            snap.cash,
            snap.positions_value,
            snap.equity,
            acct.realized_pnl,
            snap.unrealized_pnl,
        )
    )


def main():
    broker = MockBroker(cash=D("100000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    reconciler = Reconciler(ledger, broker)

    print("== 1. bootstrap: adopt broker cash into the unallocated pool ==")
    print("adopted:", router.adopt_broker_cash())

    print("\n== 2. create two sub-accounts (strategies) ==")
    router.create_sub_account(
        SubAccount(id="momo", name="momentum", max_order_notional=D("15000")),
        initial_allocation=D("30000"),
    )
    router.create_sub_account(
        SubAccount(id="mrev", name="mean-reversion", daily_loss_limit=D("1000")),
        initial_allocation=D("20000"),
    )
    print("unallocated pool:", ledger.unallocated_cash())

    broker.set_price("AAPL", D("200"))
    broker.set_price("SPY", D("500"))

    print("\n== 3. both strategies trade the SAME real account, isolated ledgers ==")
    router.place_order(OrderRequest("momo", "AAPL", OrderSide.BUY, D("50")))
    router.place_order(OrderRequest("mrev", "SPY", OrderSide.BUY, D("20")))
    router.place_order(OrderRequest("mrev", "AAPL", OrderSide.BUY, D("10")))
    show(router, ledger, "momo")
    show(router, ledger, "mrev")

    print("\n== 4. risk engine: momo tries to blow past its per-order cap ==")
    try:
        router.place_order(OrderRequest("momo", "AAPL", OrderSide.BUY, D("100")))
    except OrderRejected as exc:
        print("  REJECTED:", exc)

    print("\n== 5. mrev cannot sell momo's shares ==")
    try:
        router.place_order(OrderRequest("mrev", "AAPL", OrderSide.SELL, D("30")))
    except OrderRejected as exc:
        print("  REJECTED:", exc)

    print("\n== 6. price moves; momo takes profit ==")
    broker.set_price("AAPL", D("230"))
    router.sync()
    router.place_order(OrderRequest("momo", "AAPL", OrderSide.SELL, D("50")))
    show(router, ledger, "momo")
    show(router, ledger, "mrev")

    print("\n== 7. reconcile: ledger vs broker ==")
    print(" ", reconciler.run().summary())

    print("\n== 8. someone trades the real account manually... ==")
    broker.set_price("TSLA", D("300"))
    broker.inject_external_position("TSLA", D("5"), D("300"))
    report = reconciler.run()
    print(report.summary())


if __name__ == "__main__":
    main()
