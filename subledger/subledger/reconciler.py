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
`halt_min_cash_drift` (optional) keeps small CASH-only drifts from halting:
brokers charge per-trade regulatory fees (CAT/TAF, a cent or two) to cash at
execution but publish the activity hours later, so every fill opens a
transient cent-level gap. Such drifts are still reported (ok=False); any
position/order drift, or a cash gap at or above the threshold, still halts.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import time
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
        halt_min_cash_drift: Optional[Decimal] = None,
        confirm_delay_s: float = 0.0,
    ):
        self.ledger = ledger
        self.broker = broker
        self.cash_tolerance = cash_tolerance
        self.qty_tolerance = qty_tolerance
        self.halt_on_drift = halt_on_drift
        self.halt_min_cash_drift = halt_min_cash_drift
        # > 0: a failing check is confirmed by a second one this many seconds
        # later before it is recorded or can halt (see run()). Daemons set it.
        self.confirm_delay_s = float(confirm_delay_s or 0)

    def run(self) -> ReconciliationReport:
        """Compare ledger and broker, record the result, halt if warranted.

        A fill the broker already reports but the order stream has not
        booked yet, or a commission charged with a fill but not attributed
        yet, looks exactly like drift for a split second (liveIB 2026-10-06
        09:31 ET: the 5-minute reconcile met a SHOP fill mid-booking, saw an
        unknown position and halted, 7 ms before the fill's protection order,
        which the halt then rejected). With confirm_delay_s set, a failing
        check waits that long, books newly published cash activities, and
        checks again from fresh snapshots; only that second result is
        recorded and can halt. Real drift is still there the second time."""
        report = self._check()
        if not report.ok and self.confirm_delay_s > 0:
            first = "; ".join("[{}] {}".format(d.kind, d.detail) for d in report.drifts)[:300]
            logger.warning("reconcile: %s drift(s) found; re-checking in %.0fs before acting: %s",
                           len(report.drifts), self.confirm_delay_s, first)
            time.sleep(self.confirm_delay_s)
            try:
                self.attribute_cash_activities()
            except Exception as exc:            # attribution must not block the re-check
                logger.warning("reconcile re-check: could not attribute cash activities: %s", exc)
            report = self._check()
            if report.ok:
                logger.warning("reconcile: transient drift cleared on re-check "
                               "(fill or fee in flight): %s", first)
        self._record(report)
        return report

    def _check(self) -> ReconciliationReport:
        now = self._now()
        at = now.isoformat()
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
        # Prices come from the network OUTSIDE the booking lock; the write is
        # a FRESH read + price-only change UNDER it. Saving the rows listed
        # here would write a stale qty/reserve back over any fill booked while
        # the prices were fetched (or resurrect a position closed meanwhile).
        ledger_positions = self.ledger.list_positions()
        marks = {}
        try:
            marks = self.broker.last_prices(sorted({p.symbol for p in ledger_positions}))
        except BrokerError as exc:
            logger.warning("could not refresh marks: %s", exc)
        with self._booking_lock():
            for listed in ledger_positions:
                price = marks.get(listed.symbol)
                if price is None and listed.symbol in broker_pos:
                    price = broker_pos[listed.symbol].current_price
                if not price or price <= ZERO:
                    continue
                pos = self.ledger.get_position(listed.sub_account_id, listed.symbol)
                if (pos.qty != ZERO or pos.reserved_qty != ZERO) and pos.last_price != price:
                    pos.last_price = price
                    self.ledger.save_position(pos)

        pending_fees = self._pending_fee_gap(drifts, expected_cash, account.cash, now)
        if pending_fees is not None:
            drifts = [d for d in drifts if d.kind != "cash"]
            logger.info("cash gap %s treated as same-day broker fees not yet published", pending_fees)
        report = ReconciliationReport(
            ok=not drifts,
            at=at,
            expected_cash=expected_cash,
            broker_cash=account.cash,
            drifts=drifts,
            positions_checked=len(symbols),
            acknowledged_orders_skipped=acknowledged_skipped,
            pending_fees=pending_fees or ZERO,
        )
        return report

    def _record(self, report: ReconciliationReport) -> None:
        at = report.at
        self.ledger.save_reconciliation(
            at,
            report.ok,
            json.dumps(
                {
                    "expected_cash": str(report.expected_cash),
                    "broker_cash": str(report.broker_cash),
                    "positions_checked": report.positions_checked,
                    "pending_fees": str(report.pending_fees),
                    "drifts": [vars(d) for d in report.drifts],
                }
            ),
        )
        if report.ok:
            logger.info(report.summary())
        else:
            logger.error(report.summary())
            if self.halt_on_drift and self._warrants_halt(report):
                self.ledger.set_halted(True)
                logger.error("router halted due to reconciliation drift")
            elif self.halt_on_drift:
                logger.warning("cash drift %s is below the halt threshold %s: reported, not halted",
                               report.expected_cash - report.broker_cash, self.halt_min_cash_drift)

    @staticmethod
    def _now() -> "_dt.datetime":
        return _dt.datetime.now(_dt.timezone.utc)

    def _pending_fee_gap(self, drifts, expected_cash: Decimal, broker_cash: Decimal,
                         now: "_dt.datetime") -> Optional[Decimal]:
        """The cash gap, if it is explained by same-day fees the broker has
        charged but not yet published; None if it is (or may be) real drift.
        All must hold: the venue declares a fee lag, the only drift is cash,
        the ledger is AHEAD of the broker (fees lower broker cash), the time
        is before today's publication cutoff, the window since yesterday's
        cutoff has fills, and the gap fits the venue's fee estimate."""
        cutoff = getattr(self.broker, "fee_posting_cutoff_et", None)
        if cutoff is None or not drifts or any(d.kind != "cash" for d in drifts):
            return None
        gap = expected_cash - broker_cash
        if gap <= ZERO:
            return None
        from zoneinfo import ZoneInfo
        from .models import OrderSide

        et = now.astimezone(ZoneInfo("America/New_York"))
        today_cutoff = et.replace(hour=cutoff.hour, minute=cutoff.minute, second=0, microsecond=0)
        if et >= today_cutoff:
            return None
        since = (today_cutoff - _dt.timedelta(days=1)).astimezone(_dt.timezone.utc).isoformat()
        fills = [o for o in self.ledger.list_orders()
                 if o.filled_qty > ZERO and (o.updated_at or "") >= since]
        if not fills:
            return None
        bought = sum((o.filled_qty for o in fills if o.side == OrderSide.BUY), ZERO)
        sold = sum((o.filled_qty for o in fills if o.side == OrderSide.SELL), ZERO)
        sold_notional = sum((o.filled_qty * o.filled_avg_price for o in fills
                             if o.side == OrderSide.SELL), ZERO)
        allowance = self.broker.pending_fee_allowance(bought, sold, sold_notional) + self.cash_tolerance
        return gap if gap <= allowance else None

    def _booking_lock(self):
        """The ledger's shared writer lock (see Ledger.booking_lock); a
        private one for ledgers that predate it."""
        lock = getattr(self.ledger, "booking_lock", None)
        if lock is None:
            import threading
            lock = self.__dict__.setdefault("_fallback_lock", threading.RLock())
        return lock

    def _warrants_halt(self, report: ReconciliationReport) -> bool:
        if self.halt_min_cash_drift is None:
            return True
        for drift in report.drifts:
            if drift.kind != "cash":
                return True
            if abs(report.expected_cash - report.broker_cash) >= self.halt_min_cash_drift:
                return True
        return False

    def attribute_cash_activities(self, after: str = "") -> dict:
        """Book non-trade cash movements (dividends/fees) into the ledger so
        sub-account P&L stays honest and the cash check stays green.

        Attribution policy:
          - dividend with a symbol: credited to the sub-accounts holding that
            symbol, proportional to qty (also counted as realized P&L)
          - fee tagged with a router client id (per-order commissions, e.g.
            IB): charged to the sub-account that placed the order (realized
            P&L)
          - anything unattributable (fees without symbol, other): booked to
            the unallocated pool ("unassigned")
        Idempotent: each broker activity id is processed exactly once.
        `after` defaults to the ledger's inception instant (adopt time).
        """
        import json as _json

        processed = set(self.ledger.processed_activity_ids())
        result = {"dividends": 0, "fees": 0, "pool": 0, "total_amount": ZERO}
        if not after:
            # Activities dated before the ledger adopted the account's cash
            # are already inside that balance (same-day commissions, etc.).
            after = self.ledger.inception_at() or ""
        try:
            activities = self.broker.get_cash_activities(after=after)
        except BrokerError as exc:
            logger.warning("could not fetch cash activities: %s", exc)
            return result

        for activity in activities:
            with self._booking_lock():
                self._apply_activity(activity, processed, result, _json)
        return result

    def _apply_activity(self, activity, processed, result, _json) -> None:
        """One broker cash activity, applied with fresh reads under the
        booking lock so a fill booked concurrently is never overwritten."""
        if not activity.activity_id or activity.activity_id in processed:
            return
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
            return
        allocations = {}
        holders = []
        owner = None
        if activity.kind == "fee" and activity.order_client_id:
            owner_id = sub_account_of(activity.order_client_id)
            if owner_id is not None:
                try:
                    owner = self.ledger.get_sub_account(owner_id)
                except KeyError:
                    owner = None
        if activity.kind == "dividend" and activity.symbol:
            holders = [
                p for p in self.ledger.list_positions()
                if p.symbol == activity.symbol and p.qty > ZERO
            ]
        if owner is not None:
            owner.cash += activity.amount
            owner.realized_pnl += activity.amount
            owner.realized_pnl_today += activity.amount
            self.ledger.save_sub_account(owner)
            allocations[owner.id] = str(activity.amount)
            result["fees"] += 1
            logger.info("fee %s %s (%s) charged to sub-account %s",
                        activity.symbol, activity.amount, activity.order_client_id, owner.id)
        elif holders:
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
        with self._booking_lock():
            expected = self.ledger.unallocated_cash()
            for acct in self.ledger.list_sub_accounts():
                expected += acct.cash
            delta = account.cash - expected
            if delta != ZERO:
                self.ledger.set_unallocated_cash(self.ledger.unallocated_cash() + delta)
                logger.warning("absorbed cash drift of %s into unallocated pool %s", delta, note)
        return delta
