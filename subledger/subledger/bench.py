"""Router latency benchmark — measures what subledger ADDS to an order.

    python -m subledger.bench [N]

Uses MockBroker (zero network) so every number is pure router+ledger cost:
risk checks, reservation, SQLite writes, fill booking. Broker RTT and
matching are the venue's business; this isolates ours. Runs against a
REAL on-disk SQLite file (like production), not :memory:.
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from decimal import Decimal as D

from .broker.base import BrokerOrderState
from .broker.mock import MockBroker
from .ledger import Ledger
from .models import OrderRequest, OrderSide, SubAccount
from .router import Router


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def fmt(label, values):
    ms = [v * 1000 for v in values]
    return "{:32s} p50={:6.2f}ms  p95={:6.2f}ms  p99={:6.2f}ms  max={:6.2f}ms".format(
        label, statistics.median(ms), pct(ms, 0.95), pct(ms, 0.99), max(ms))


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    db = os.path.join(tempfile.mkdtemp(prefix="subledger-bench-"), "bench.db")
    broker = MockBroker(cash=D("100000000"))
    ledger = Ledger(db)
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    router.create_sub_account(SubAccount(id="b"), initial_allocation=D("50000000"))
    broker.set_price("AAPL", D("100"))
    broker.fill_market_orders = False   # orders rest; booking measured apart

    # -- submit path: risk + reserve + SQLite, minus broker ---------------
    submit = []
    orders = []
    for _ in range(n):
        t0 = time.perf_counter()
        order = router.place_order(OrderRequest("b", "AAPL", OrderSide.BUY, D("10")))
        submit.append(time.perf_counter() - t0)
        orders.append(order)

    # -- booking path: one stream event -> ledger booked ------------------
    booking = []
    for order in orders:
        state = BrokerOrderState(
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            status="filled", filled_qty=D("10"), filled_avg_price=D("100"),
            symbol="AAPL")
        t0 = time.perf_counter()
        router.apply_stream_state(state)
        booking.append(time.perf_counter() - t0)

    print("subledger router overhead (MockBroker, on-disk SQLite, n={})".format(n))
    print(fmt("place_order (risk+reserve+DB)", submit))
    print(fmt("stream fill -> booked", booking))
    print("\nnot included (the venue's side): broker REST RTT ~100-250ms,")
    print("matching time, and trade_update delivery ~300-800ms end to end.")


if __name__ == "__main__":
    main()
