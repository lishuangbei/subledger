"""Reconciler: proves the sum of sub-account ledgers equals the real account.

Checks, in order:
  1. Cash:      sum(sub.cash) + unallocated_cash  ==  broker cash (± tolerance)
  2. Positions: per symbol, sum(sub qty)          ==  broker qty
  3. Foreign orders: any order at the broker whose client_order_id does not
     carry the router's "sl." prefix (someone traded the account directly).
  4. Marks: refresh each ledger position's last_price from broker data so
     unrealized P&L and buying-power estimates stay honest.

Run it on a timer (see cli.py / api.py) — every 5–15 minutes is plenty.
`halt_on_drift=True` flips the router's kill switch when a drift is found,
so no new orders go out against a ledger that no longer matches reality.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from decimal import Decimal
from typing import Optional

from .broker.base import BrokerAdapter, BrokerError
from .ledger import Ledger
from .models import ZERO, DriftItem, ReconciliationReport, sub_account_of

logger = logging.getLogger("subledger.reconciler")


class Reconciler:
    def __init__(
        self,
        ledger: Ledger,
        broker: BrokerAdapter,
        cash_tolerance: Decimal = Decimal("0.01"),
        qty_tolerance: Decimal = Decimal("0.000001"),
        halt_on_drift: bool = False,
    ):
        self.ledger = ledger
        self.broker = broker
        self.cash_tolerance = cash_tolerance
        self.qty_tolerance = qty_tolerance
        self.halt_on_drift = halt_on_drift

    def run(self) -> ReconciliationReport:
        at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        drifts = []

        account = self.broker.get_account()

        # 1) cash ------------------------------------------------------
        expected_cash = self.ledger.unallocated_cash()
        for acct in self.ledger.list_sub_accounts():
            expected_cash += acct.cash
        if abs(expected_cash - account.cash) > self.cash_tolerance:
            drifts.append(
                DriftItem(
                    kind="cash",
                    detail="ledger cash total != broker cash "
                    "(deposit/withdrawal, dividend, fee, or manual trade?)",
                    expected=str(expected_cash),
                    actual=str(account.cash),
                )
            )

        # 2) positions -------------------------------------------------
        expected_pos = self.ledger.aggregate_positions()
        broker_pos = {p.symbol: p for p in account.positions}
        symbols = set(expected_pos) | set(broker_pos)
        for symbol in sorted(symbols):
            exp_qty = expected_pos.get(symbol, ZERO)
            act_qty = broker_pos[symbol].qty if symbol in broker_pos else ZERO
            if abs(exp_qty - act_qty) > self.qty_tolerance:
                kind = "unknown_position" if exp_qty == ZERO else "position"
                drifts.append(
                    DriftItem(
                        kind=kind,
                        detail="qty mismatch for {}".format(symbol),
                        expected=str(exp_qty),
                        actual=str(act_qty),
                    )
                )

        # 3) foreign orders --------------------------------------------
        # An order is foreign unless (a) its client id carries our prefix,
        # (b) its broker id is known to the ledger (broker-generated OCO/
        # bracket legs land here), or (c) an operator acknowledged it.
        acknowledged_skipped = 0
        try:
            known_ids = set(self.ledger.known_broker_order_ids())
            acknowledged = set(self.ledger.acknowledged_foreign_ids())
            for state in self.broker.list_orders(open_only=False):
                if state.client_order_id and sub_account_of(state.client_order_id):
                    continue
                if state.broker_order_id in known_ids:
                    continue
                if state.status in ("canceled", "rejected", "expired") and state.filled_qty == ZERO:
                    continue  # dead foreign orders moved nothing
                if state.broker_order_id in acknowledged:
                    acknowledged_skipped += 1
                    continue
                drifts.append(
                    DriftItem(
                        kind="unknown_order",
                        detail="order {} ({} {}) was not placed via the router".format(
                            state.broker_order_id, state.symbol, state.status
                        ),
                        actual=str(state.filled_qty),
                        broker_order_id=state.broker_order_id,
                    )
                )
        except BrokerError as exc:
            logger.warning("could not list broker orders during reconcile: %s", exc)

        # 4) refresh marks ---------------------------------------------
        ledger_positions = self.ledger.list_positions()
        marks = {}
        try:
            marks = self.broker.last_prices(sorted({p.symbol for p in ledger_positions}))
        except BrokerError as exc:
            logger.warning("could not refresh marks: %s", exc)
        for pos in ledger_positions:
            price = marks.get(pos.symbol)
            if price is None and pos.symbol in broker_pos:
                price = broker_pos[pos.symbol].current_price
            if price and price > ZERO:
                pos.last_price = price
                self.ledger.save_position(pos)

        report = ReconciliationReport(
            ok=not drifts,
            at=at,
            expected_cash=expected_cash,
            broker_cash=account.cash,
            drifts=drifts,
            positions_checked=len(symbols),
            acknowledged_orders_skipped=acknowledged_skipped,
        )
        self.ledger.save_reconciliation(
            at,
            report.ok,
            json.dumps(
                {
                    "expected_cash": str(report.expected_cash),
                    "broker_cash": str(report.broker_cash),
                    "positions_checked": report.positions_checked,
                    "drifts": [vars(d) for d in report.drifts],
                }
            ),
        )
        if report.ok:
            logger.info(report.summary())
        else:
            logger.error(report.summary())
            if self.halt_on_drift:
                self.ledger.set_halted(True)
                logger.error("router halted due to reconciliation drift")
        return report

    def attribute_cash_activities(self, after: str = "") -> dict:
        """Book non-trade cash movements (dividends/fees) into the ledger so
        sub-account P&L stays honest and the cash check stays green.

        Attribution policy:
          - dividend with a symbol: credited to the sub-accounts holding that
            symbol, proportional to qty (also counted as realized P&L)
          - anything unattributable (fees without symbol, other): booked to
            the unallocated pool ("unassigned")
        Idempotent: each broker activity id is processed exactly once.
        """
        import json as _json

        processed = set(self.ledger.processed_activity_ids())
        result = {"dividends": 0, "pool": 0, "total_amount": ZERO}
        try:
            activities = self.broker.get_cash_activities(after=after)
        except BrokerError as exc:
            logger.warning("could not fetch cash activities: %s", exc)
            return result

        for activity in activities:
            if not activity.activity_id or activity.activity_id in processed:
                continue
            if activity.kind not in ("dividend", "fee"):
                # Deposits/withdrawals/journals are NEVER auto-booked: the
                # initial funding was already adopted at ledger init, and any
                # later transfer should be an explicit operator decision
                # (absorb_cash_drift). Mark as seen so it isn't re-examined.
                self.ledger.record_processed_activity(
                    activity.activity_id, activity.kind, activity.symbol,
                    activity.amount, _json.dumps({"skipped": "manual-only kind"}),
                )
                logger.info("cash activity %s (%s %s) left for operator absorb",
                            activity.activity_id, activity.kind, activity.amount)
                continue
            allocations = {}
            holders = []
            if activity.kind == "dividend" and activity.symbol:
                holders = [
                    p for p in self.ledger.list_positions()
                    if p.symbol == activity.symbol and p.qty > ZERO
                ]
            if holders:
                total_qty = sum((p.qty for p in holders), ZERO)
                remaining = activity.amount
                for i, pos in enumerate(holders):
                    share = (
                        remaining if i == len(holders) - 1
                        else (activity.amount * pos.qty / total_qty).quantize(Decimal("0.0001"))
                    )
                    remaining -= share
                    acct = self.ledger.get_sub_account(pos.sub_account_id)
                    acct.cash += share
                    acct.realized_pnl += share
                    acct.realized_pnl_today += share
                    self.ledger.save_sub_account(acct)
                    allocations[pos.sub_account_id] = str(share)
                result["dividends"] += 1
                logger.info("dividend %s %s attributed: %s", activity.symbol,
                            activity.amount, allocations)
            else:
                self.ledger.set_unallocated_cash(
                    self.ledger.unallocated_cash() + activity.amount
                )
                allocations["unassigned"] = str(activity.amount)
                result["pool"] += 1
                logger.info("cash activity %s (%s %s) -> unassigned pool",
                            activity.activity_id, activity.kind, activity.amount)
            result["total_amount"] += activity.amount
            self.ledger.record_processed_activity(
                activity.activity_id, activity.kind, activity.symbol,
                activity.amount, _json.dumps(allocations),
            )
        return result

    def acknowledge_foreign_orders(self, report: ReconciliationReport, note: str = "") -> list:
        """Operator action: accept the unknown orders in `report` as explained
        (a deliberate manual trade). They stop appearing as drift; the cash
        they moved still needs absorb_cash_drift()."""
        acked = []
        for drift in report.drifts:
            if drift.kind == "unknown_order" and drift.broker_order_id:
                self.ledger.acknowledge_foreign(drift.broker_order_id, note=note)
                acked.append(drift.broker_order_id)
        if acked:
            logger.warning("acknowledged %s foreign order(s): %s", len(acked), acked)
        return acked

    def absorb_cash_drift(self, note: str = "") -> Decimal:
        """Operator action: accept an external cash change (deposit, dividend,
        fee) into the unallocated pool so the ledger matches reality again."""
        account = self.broker.get_account()
        expected = self.ledger.unallocated_cash()
        for acct in self.ledger.list_sub_accounts():
            expected += acct.cash
        delta = account.cash - expected
        if delta != ZERO:
            self.ledger.set_unallocated_cash(self.ledger.unallocated_cash() + delta)
            logger.warning("absorbed cash drift of %s into unallocated pool %s", delta, note)
        return delta
