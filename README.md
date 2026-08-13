# subledger

Virtual sub-accounts for a single real brokerage account.

Run multiple trading strategies against one Alpaca account, each with its own
capital allocation, margin limit, and P&L — and continuously prove that the
sum of the sub-ledgers matches what the broker actually holds.

**The problem:** a retail broker gives you one account. Trade N strategies
through it and their cash, buying power, and returns blur together
irrecoverably. The institutional fix (Alpaca OmniSub, prime-broker
sub-accounting) requires a partnership agreement. subledger is the
personal-scale version: an execution router plus a subsidiary ledger.

```
strategy A ─┐                                       ┌─> Alpaca (paper/live)
strategy B ─┼─> Router ── risk checks ── adapter ───┤
strategy C ─┘     │                                 └─> IB (5-method adapter)
                  │
            SQLite ledger  <──  Reconciler (periodic, can auto-halt)
```

The core is dependency-free (Python 3.9+, stdlib only). `alpaca-py` and
FastAPI are optional extras. Strategies never see broker credentials — only
the router process holds them.

## Why the books stay correct

Four mechanisms, in order of importance:

1. **Order attribution.** Every order is submitted with
   `client_order_id = sl.<sub_account>.<uuid>`. Any fill maps to its
   sub-account without a lookup, and any order at the broker *without* this
   prefix (a manual trade in the app, another bot) is flagged as foreign by
   the reconciler.
2. **Reserve, then submit.** A buy freezes cash (`reserved_cash`), a sell
   freezes shares (`reserved_qty`) *before* the order reaches the broker.
   Two concurrent orders cannot pass the same buying-power check; cancels
   and rejections release the remainder exactly.
3. **Delta-based fill booking.** Brokers report cumulative filled qty and
   average price. The ledger books only the increment since the last poll,
   so partial fills, repeated polls, and a future trade-update stream are
   all idempotent — no double counting.
4. **Reconciliation invariants**, checked on a timer:
   - `Σ sub-account cash + unallocated pool == broker cash` (±$0.01)
   - per symbol: `Σ sub-account qty == broker qty`
   - no unattributed orders at the broker
   With `halt_on_drift=True`, any violation trips a kill switch that blocks
   all new orders until a human reconciles. Legitimate external changes
   (deposits, dividends, fees) are absorbed with one call:
   `reconciler.absorb_cash_drift()`.

## Per-sub-account limits

| Field | Effect |
|---|---|
| `cash` + reservations | Hard capital ceiling; over-budget buys are rejected |
| `margin_multiplier` | `1` = cash account; `2` ≈ 2x gross leverage (simplified model) |
| `max_order_notional` | Per-order size cap |
| `daily_loss_limit` | Realized loss past limit blocks new buys — closing stays allowed |
| `symbol_whitelist` | Instrument allowlist |
| `allow_short` | Off by default; a sub-account can never sell another's shares |

## Quick start

Zero credentials, zero installs:

```bash
cd subledger
python3 -m unittest discover tests    # 22 tests
python3 examples/demo.py              # isolation, rejections, drift detection
```

Embedded (strategy and router in one process):

```python
from decimal import Decimal as D
from subledger import Ledger, Router, Reconciler, SubAccount, OrderRequest, OrderSide
from subledger.broker.alpaca import AlpacaBroker   # pip install alpaca-py

broker = AlpacaBroker(api_key="...", secret_key="...", paper=True)
router = Router(Ledger("subledger.db"), broker)

router.adopt_broker_cash()   # one-time: claim account cash as the unallocated pool
router.create_sub_account(SubAccount(id="momo", max_order_notional=D("15000")),
                          initial_allocation=D("30000"))

router.place_order(OrderRequest("momo", "AAPL", OrderSide.BUY, D("10")))
router.sync()                # poll fills (run every few seconds)
print(router.equity_snapshot("momo"))

Reconciler(router.ledger, broker, halt_on_drift=True).run()   # cron this
```

Over REST (strategies in any language, keys stay in the router):

```bash
pip install "fastapi[standard]" alpaca-py
export SUBLEDGER_BROKER=alpaca ALPACA_API_KEY=... ALPACA_SECRET_KEY=... ALPACA_PAPER=true
uvicorn subledger.api:create_app --factory

curl -X POST :8000/accounts -d '{"id":"momo","allocation":"30000"}'
curl -X POST :8000/orders   -d '{"sub_account_id":"momo","symbol":"AAPL","side":"buy","qty":"10"}'
curl :8000/accounts/momo              # cash / equity / realized / unrealized
curl :8000/reconcile/latest
curl :8000/clock                      # is_open / next_open / next_close
curl ":8000/marks?symbols=AAPL,MSFT"  # batch marks, keys stay server-side
curl -X POST :8000/halt               # kill switch
```

Fill sync and reconciliation run as background loops inside the API process
(`SUBLEDGER_SYNC_INTERVAL`, `SUBLEDGER_RECONCILE_INTERVAL`).

## Adding a broker (e.g. Interactive Brokers)

Implement five methods from `subledger/broker/base.py` —
`submit_order`, `cancel_order`, `get_order`, `list_orders`, `get_account` —
returning the normalized dataclasses. Router, ledger, and reconciler need no
changes. For IB, the `orderRef` field carries the attribution id.

## What it deliberately does not do

- **No backtesting.** It is a pure execution/accounting layer; pair it with
  any backtest framework (the repo includes a [lumibot-based
  alternative](option1-lumibot/) that has backtesting built in).
- **Simplified margin.** Buying power is `(cash − reserved) × multiplier`;
  maintenance margin is left to the broker's own engine as the backstop.
- **Account-level rules still apply account-wide.** Wash sales, PDT counts,
  and long/short netting across sub-accounts happen at the broker no matter
  what any router does — keep strategy universes disjoint if this matters.
- **Single writer.** One router process owns the ledger. Multi-process
  strategies must go through the REST API, not their own `Router` instances.
- Corporate actions (splits, dividend reinvestment) surface as
  reconciliation drift for a human to resolve — by design, not silently.

## Repository layout

```
subledger/           the router (this project) — see subledger/README.md (中文)
option1-lumibot/     alternative built on lumibot: multi-strategy budgets +
                     hard-guard mixin + cron reconciliation (中文 docs)
docs/comparison.zh-CN.md   research notes: why lumibot/Nautilus/LEAN/OmniSub
                           were or weren't a fit, and a feature comparison
```

Status: tested against paper trading. Run at least a week on
`ALPACA_PAPER=true` and watch `/reconcile/latest` before pointing it at real
money.
