"""The router: the only component that talks to the real brokerage account.

Order path:
    strategy -> Router.place_order()
             -> risk checks against the sub-account ledger
             -> reserve cash/shares
             -> BrokerAdapter.submit_order(client_order_id="sl.<sub>.<suffix>")
    Router.sync() polls the broker and books fills back into the ledger.

Booking is delta-based (broker reports cumulative filled_qty / avg price), so
partial fills and out-of-order polls are handled naturally.

Exit groups (OCO / bracket / OTO)
---------------------------------
The two exit legs of a protected position cover the SAME shares, so the share
reserve lives on exactly one record — the group's "reserve holder":
  - OCO: the parent (the take-profit limit sell) holds the reserve, taken at
    placement time.
  - bracket / OTO: the first exit leg holds the reserve, taken incrementally
    as the entry fills.
Either leg's fill books against the holder's reserve; when every member of
the group is terminal, the remainder is released.
"""

from __future__ import annotations

import logging
import threading
import uuid
from decimal import Decimal
from typing import List, Optional

from . import risk
from .broker.base import BrokerAdapter, BrokerError, BrokerOrderState, BrokerSubmitResult
from .ledger import Ledger
from .models import (
    ZERO,
    EquitySnapshot,
    OrderClass,
    OrderRecord,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    SubAccount,
    new_client_order_id,
)

logger = logging.getLogger("subledger.router")

_BROKER_TO_LOCAL = {
    "open": OrderStatus.OPEN,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


class OrderRejected(Exception):
    pass


class AccountMismatch(Exception):
    """The broker answered with a different account than the operator
    configured — most likely the wrong credentials file. Fail closed."""


class Router:
    def __init__(self, ledger: Ledger, broker: BrokerAdapter, eager_sync: bool = True,
                 expected_account_id: Optional[str] = None):
        """eager_sync: poll the order once right after submit to book an
        immediate fill. Costs one extra broker round trip per order; turn it
        OFF when a trade-update stream is booking fills (the event arrives
        within milliseconds anyway) to halve submit-path latency.

        expected_account_id: when set, the broker's account id is verified
        immediately (one get_account call); a mismatch raises AccountMismatch
        before any order can flow — the guard against paper keys in a live
        server and vice versa."""
        self.ledger = ledger
        self.broker = broker
        self.eager_sync = eager_sync
        self._lock = threading.RLock()
        self._pending_verification: Optional[str] = None
        if expected_account_id:
            try:
                self.verify_account(expected_account_id)
            except BrokerError as exc:
                # Broker unreachable at construction (outage) is NOT a
                # mismatch: boot anyway, but fail closed — every order is
                # blocked until verification succeeds on a later attempt.
                self._pending_verification = expected_account_id
                logger.warning("account verification deferred (broker "
                               "unreachable): %s — orders blocked until verified", exc)

    def verify_account(self, expected_account_id: str) -> str:
        actual = self.broker.get_account().account_id
        if actual != expected_account_id:
            raise AccountMismatch(
                "broker answered account {}… but operator expects {}… — "
                "wrong credentials? refusing to route orders".format(
                    actual[:8], expected_account_id[:8]))
        logger.info("account verified: %s…", actual[:8])
        self._pending_verification = None
        return actual

    # -- account management --------------------------------------------

    def adopt_broker_cash(self) -> Decimal:
        """One-time bootstrap: pull the real account's current cash into the
        unallocated pool. Refuses to run twice (that would double-count)."""
        with self._lock:
            if self.ledger.unallocated_cash() != ZERO or self.ledger.list_sub_accounts():
                raise ValueError("ledger already initialized; adopt refused")
            cash = self.broker.get_account().cash
            self.ledger.set_unallocated_cash(cash)
            return cash

    def create_sub_account(self, acct: SubAccount, initial_allocation: Decimal = ZERO) -> SubAccount:
        with self._lock:
            self.ledger.create_sub_account(acct)
            if initial_allocation > ZERO:
                self.ledger.allocate(acct.id, initial_allocation)
            return self.ledger.get_sub_account(acct.id)

    def halt(self) -> None:
        """Kill switch: reject all new orders until resume()."""
        self.ledger.set_halted(True)
        logger.warning("router HALTED")

    def resume(self) -> None:
        self.ledger.set_halted(False)
        logger.info("router resumed")

    def reset_daily(self) -> None:
        """Zero out per-day counters; run once before each session."""
        with self._lock:
            for acct in self.ledger.list_sub_accounts():
                acct.realized_pnl_today = ZERO
                self.ledger.save_sub_account(acct)

    # -- trading --------------------------------------------------------

    def place_order(self, req: OrderRequest, est_price: Optional[Decimal] = None) -> OrderRecord:
        """Validate, reserve, and forward one order. Raises OrderRejected if
        any risk check fails or the broker rejects synchronously."""
        import time as _time

        t0 = _time.perf_counter()
        with self._lock:
            if self._pending_verification:
                try:
                    self.verify_account(self._pending_verification)
                except BrokerError as exc:
                    raise OrderRejected(
                        "account not yet verified (broker unreachable): {}".format(exc)
                    ) from exc
            acct = self.ledger.get_sub_account(req.sub_account_id)
            pos = self.ledger.get_position(req.sub_account_id, req.symbol)

            price = self._estimate_price(req, pos, est_price)
            try:
                reserve = risk.check_order(
                    acct, pos, req, price, halted=self.ledger.is_halted(),
                    gross_exposure=self._gross_exposure(req.sub_account_id),
                )
                self._check_asset(req)
            except risk.RiskViolation as exc:
                self._record_rejection(req, str(exc))
                raise OrderRejected(str(exc)) from exc

            try:
                client_order_id = new_client_order_id(req.sub_account_id, req.client_tag)
            except ValueError as exc:
                self._record_rejection(req, str(exc))
                raise OrderRejected(str(exc)) from exc

            order = OrderRecord(
                id=uuid.uuid4().hex[:16],
                client_order_id=client_order_id,
                broker_order_id=None,
                sub_account_id=req.sub_account_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                limit_price=(
                    req.take_profit.limit_price
                    if req.order_class == OrderClass.OCO and req.take_profit is not None
                    else req.limit_price
                ),
                stop_price=req.stop_price,
                trail_percent=req.trail_percent,
                trail_price=req.trail_price,
                notional=req.notional,
                extended_hours=req.extended_hours,
                order_class=req.order_class,
                time_in_force=req.time_in_force,
                status=OrderStatus.PENDING,
                reserved=reserve if req.side == OrderSide.BUY else req.qty,
            )

            # Reserve before submitting: never let two concurrent orders both
            # pass the same buying-power check. For OCO the qty is reserved
            # once and shared by both legs.
            if req.side == OrderSide.BUY:
                acct.reserved_cash += reserve
                self.ledger.save_sub_account(acct)
            else:
                pos.reserved_qty += req.qty
                self.ledger.save_position(pos)
            self.ledger.save_order(order)
            t_local = _time.perf_counter()

            try:
                result = self.broker.submit_order(req, client_order_id)
            except BrokerError as exc:
                order.status = OrderStatus.REJECTED
                order.reject_reason = str(exc)
                self._release_remaining(order, acct, pos)
                self.ledger.save_order(order)
                self.ledger.save_sub_account(acct)
                self.ledger.save_position(pos)
                raise OrderRejected(str(exc)) from exc

            order.broker_order_id = result.broker_order_id
            order.status = OrderStatus.OPEN
            self.ledger.save_order(order)
            self._register_legs(order, req, result)
            t_submit = _time.perf_counter()
            logger.info(
                "order %s: %s %s %s %s x %s for sub-account %s -> broker id %s "
                "(router %.1fms + broker %.0fms)",
                order.id, req.side.value, req.symbol, req.order_class.value,
                req.order_type.value, req.qty if req.notional is None else "${}".format(req.notional),
                req.sub_account_id, order.broker_order_id,
                (t_local - t0) * 1000, (t_submit - t_local) * 1000,
            )
            if self.eager_sync:
                # Book any immediate fill (market orders often fill at once).
                self._sync_one(order)
                for leg in self.ledger.list_legs(order.id):
                    self._sync_one(leg)
            return self.ledger.get_order(order.id)

    def cancel_order(self, order_id: str) -> OrderRecord:
        """Cancel an order; for OCO/bracket parents the broker cancels the
        legs too and the next sync books everything."""
        with self._lock:
            order = self.ledger.get_order(order_id)
            if order.broker_order_id and not order.status.is_terminal:
                self.broker.cancel_order(order.broker_order_id)
                self._sync_one(order)
            for leg in self.ledger.list_legs(order_id):
                self._sync_one(leg)
            return self.ledger.get_order(order_id)

    def replace_order(
        self,
        order_id: str,
        qty: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
    ) -> OrderRecord:
        """Replace price/qty of a live simple order (reprice without losing
        queue position at the broker). Reserves are adjusted atomically in
        the ledger; the broker returns a NEW order id which replaces the old
        one on the record."""
        with self._lock:
            order = self.ledger.get_order(order_id)
            if order.status.is_terminal:
                raise OrderRejected("cannot replace a terminal order")
            if order.broker_order_id is None:
                raise OrderRejected("order has no broker id")
            if order.notional is not None:
                raise OrderRejected("cannot replace a notional order")

            # Exit-group members (OCO parent / any exit leg): price-only
            # replace. The share reserve is untouched, so no accounting moves;
            # this lets a strategy tighten a stop or move a take-profit
            # WITHOUT the cancel-and-relist unprotected window.
            if order.order_class != OrderClass.SIMPLE:
                if qty is not None:
                    raise OrderRejected("exit-group replace is price-only (no qty)")
                if limit_price is None and stop_price is None:
                    raise OrderRejected("nothing to replace")
                new_broker_id = self.broker.replace_order(
                    order.broker_order_id, limit_price=limit_price, stop_price=stop_price
                )
                if limit_price is not None:
                    order.limit_price = limit_price
                if stop_price is not None:
                    order.stop_price = stop_price
                order.broker_order_id = new_broker_id
                self.ledger.save_order(order)
                logger.info("exit-group order %s repriced -> broker id %s (limit=%s stop=%s)",
                            order.id, new_broker_id, order.limit_price, order.stop_price)
                self._sync_one(order)
                return self.ledger.get_order(order_id)

            acct = self.ledger.get_sub_account(order.sub_account_id)
            pos = self.ledger.get_position(order.sub_account_id, order.symbol)
            new_qty = qty if qty is not None else order.qty
            new_limit = limit_price if limit_price is not None else order.limit_price
            new_stop = stop_price if stop_price is not None else order.stop_price
            if new_qty <= order.filled_qty:
                raise OrderRejected("replacement qty must exceed filled qty")

            remaining = new_qty - order.filled_qty
            if order.side == OrderSide.BUY:
                ref_price = new_limit if new_limit is not None else new_stop
                if ref_price is None or ref_price <= ZERO:
                    raise OrderRejected("replace needs a positive limit/stop price")
                new_reserve = remaining * ref_price
                delta = new_reserve - order.reserved
                if delta > ZERO and delta > risk.buying_power(
                    acct, self._gross_exposure(order.sub_account_id)
                ):
                    raise OrderRejected(
                        "insufficient buying power for replacement (+{})".format(delta)
                    )
            else:
                new_reserve = remaining
                delta = new_reserve - order.reserved
                if delta > ZERO and delta > pos.qty - pos.reserved_qty:
                    raise OrderRejected("insufficient sellable shares for replacement")

            new_broker_id = self.broker.replace_order(
                order.broker_order_id, qty=qty, limit_price=limit_price, stop_price=stop_price
            )

            if order.side == OrderSide.BUY:
                acct.reserved_cash += delta
                self.ledger.save_sub_account(acct)
            else:
                pos.reserved_qty += delta
                self.ledger.save_position(pos)
            order.reserved = new_reserve
            order.qty = new_qty
            order.limit_price = new_limit
            order.stop_price = new_stop
            order.broker_order_id = new_broker_id
            self.ledger.save_order(order)
            logger.info("order %s replaced -> broker id %s (qty=%s limit=%s stop=%s)",
                        order.id, new_broker_id, new_qty, new_limit, new_stop)
            self._sync_one(order)
            return self.ledger.get_order(order_id)

    def sync(self) -> int:
        """Poll the broker for every non-terminal local order (legs included)
        and book any state changes. Returns the number of orders updated. Run
        this on a short interval or wire it to a trade-update stream."""
        updated = 0
        with self._lock:
            for order in self.ledger.list_orders(open_only=True):
                if self._sync_one(order):
                    updated += 1
        return updated

    def apply_stream_state(self, state: BrokerOrderState) -> bool:
        """Book a trade-update event pushed by the broker's websocket. The
        payload already carries the cumulative fill state, so no extra API
        round trip is needed — booking latency is the event latency."""
        with self._lock:
            order = None
            if state.client_order_id:
                order = self.ledger.get_order_by_client_id(state.client_order_id)
            if order is None and state.broker_order_id:
                for candidate in self.ledger.list_orders(open_only=True):
                    if candidate.broker_order_id == state.broker_order_id:
                        order = candidate
                        break
            if order is None:
                logger.debug("stream event for unknown order %s (foreign?)",
                             state.broker_order_id)
                return False
            return self._apply_state(order, state)

    def repair(self) -> dict:
        """Crash recovery — run once at startup.

        A crash between save_order(PENDING) and the broker-id write-back
        leaves an order that sync() can never advance, with its cash/share
        reserve stuck forever. For each such order, ask the broker whether the
        client id ever arrived: adopt it if yes, release the reserve if no.
        Finishes with a full sync."""
        report = {"adopted": [], "released": [], "synced": 0}
        with self._lock:
            for order in self.ledger.list_orders(open_only=True):
                if order.broker_order_id is not None:
                    continue
                try:
                    state = self.broker.get_order_by_client_id(order.client_order_id)
                except BrokerError as exc:
                    logger.error("repair lookup failed for %s: %s", order.client_order_id, exc)
                    continue
                acct = self.ledger.get_sub_account(order.sub_account_id)
                pos = self.ledger.get_position(order.sub_account_id, order.symbol)
                if state is not None:
                    order.broker_order_id = state.broker_order_id
                    order.status = OrderStatus.OPEN
                    self.ledger.save_order(order)
                    self._sync_one(order)
                    report["adopted"].append(order.client_order_id)
                    logger.warning("repair: adopted orphan %s -> broker id %s",
                                   order.client_order_id, state.broker_order_id)
                else:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = "never reached the broker (crash repair)"
                    self._release_remaining(order, acct, pos)
                    self.ledger.save_order(order)
                    self.ledger.save_sub_account(acct)
                    self.ledger.save_position(pos)
                    report["released"].append(order.client_order_id)
                    logger.warning("repair: released orphan %s (reserve returned)",
                                   order.client_order_id)
            report["synced"] = self.sync()
        return report

    # -- views ----------------------------------------------------------

    def refresh_marks(self) -> int:
        """Lightweight mark refresh: one get_account() call updates
        last_price for every ledger position the broker currently holds.
        Pair with snapshot_equity_history() for fine-grained equity curves
        without waiting for the next full reconcile."""
        try:
            broker_positions = {p.symbol: p.current_price
                                for p in self.broker.get_account().positions}
        except BrokerError as exc:
            logger.warning("refresh_marks failed: %s", exc)
            return 0
        updated = 0
        with self._lock:
            for pos in self.ledger.list_positions():
                price = broker_positions.get(pos.symbol)
                if price and price > ZERO and price != pos.last_price:
                    pos.last_price = price
                    self.ledger.save_position(pos)
                    updated += 1
        return updated

    def snapshot_equity_history(self) -> int:
        """Append one equity-history row per sub-account (call on a cadence —
        the trial daemon does every 15 minutes during market hours). Returns
        the number of rows written."""
        import datetime as _dt

        at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        count = 0
        with self._lock:
            for acct in self.ledger.list_sub_accounts():
                snap = self.equity_snapshot(acct.id)
                self.ledger.record_equity_snapshot(
                    at, acct.id, snap.cash, snap.positions_value,
                    snap.realized_pnl, snap.unrealized_pnl,
                )
                count += 1
        return count

    def equity_snapshot(self, sub_id: str) -> EquitySnapshot:
        acct = self.ledger.get_sub_account(sub_id)
        positions = self.ledger.list_positions(sub_id)
        return EquitySnapshot(
            sub_account_id=sub_id,
            cash=acct.cash,
            positions_value=sum((p.market_value for p in positions), ZERO),
            realized_pnl=acct.realized_pnl,
            unrealized_pnl=sum((p.unrealized_pnl for p in positions), ZERO),
        )

    # -- internals ------------------------------------------------------

    def _register_legs(self, parent: OrderRecord, req: OrderRequest, result: BrokerSubmitResult) -> None:
        """Record broker-generated exit legs as child OrderRecords so sync
        tracks them and the reconciler recognizes their broker ids."""
        for broker_leg in result.legs:
            spec = req.stop_loss if broker_leg.role == "stop_loss" else req.take_profit
            leg = OrderRecord(
                id=uuid.uuid4().hex[:16],
                client_order_id=(
                    broker_leg.client_order_id
                    or new_client_order_id(parent.sub_account_id)
                ),
                broker_order_id=broker_leg.broker_order_id,
                sub_account_id=parent.sub_account_id,
                symbol=parent.symbol,
                side=OrderSide.SELL,
                qty=parent.qty,
                order_type=(
                    OrderType.LIMIT if broker_leg.role == "take_profit"
                    else (OrderType.STOP_LIMIT if getattr(spec, "limit_price", None) is not None
                          else OrderType.STOP)
                ),
                limit_price=getattr(spec, "limit_price", None),
                stop_price=getattr(spec, "stop_price", None),
                time_in_force=parent.time_in_force,
                status=OrderStatus.OPEN,
                order_class=parent.order_class,
                parent_order_id=parent.id,
                leg_role=broker_leg.role,
                reserved=ZERO,
            )
            self.ledger.save_order(leg)

    def _exit_group(self, order: OrderRecord) -> Optional[List[OrderRecord]]:
        """If `order` sells shares protected by a shared reserve, return every
        member of its group (holder first); otherwise None."""
        if order.order_class == OrderClass.SIMPLE:
            return None
        if order.parent_order_id is not None:
            parent = self.ledger.get_order(order.parent_order_id)
        else:
            parent = order
        legs = self.ledger.list_legs(parent.id)
        if parent.order_class == OrderClass.OCO:
            return [parent] + legs           # holder: OCO parent (the TP sell)
        if order.parent_order_id is None:
            return None                       # bracket/OTO entry is a plain buy
        return legs if legs else None         # holder: first exit leg

    @staticmethod
    def _holder(group: List[OrderRecord]) -> OrderRecord:
        return group[0]

    def _estimate_price(
        self, req: OrderRequest, pos, est_price: Optional[Decimal]
    ) -> Decimal:
        if req.order_type == OrderType.LIMIT and req.limit_price is not None:
            return req.limit_price
        if req.order_class == OrderClass.OCO and req.take_profit is not None:
            return req.take_profit.limit_price
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price is not None:
            return req.stop_price
        if est_price is not None:
            return est_price
        marks = self.broker.last_prices([req.symbol])
        if req.symbol in marks:
            return marks[req.symbol]
        if pos.last_price > ZERO:
            return pos.last_price
        return ZERO  # risk check will reject with a clear message

    def _record_rejection(self, req: OrderRequest, reason: str) -> None:
        self.ledger.save_order(
            OrderRecord(
                id=uuid.uuid4().hex[:16],
                client_order_id=new_client_order_id(req.sub_account_id),
                broker_order_id=None,
                sub_account_id=req.sub_account_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                limit_price=req.limit_price,
                stop_price=req.stop_price,
                notional=req.notional,
                order_class=req.order_class,
                time_in_force=req.time_in_force,
                status=OrderStatus.REJECTED,
                reserved=ZERO,
                reject_reason=reason,
            )
        )

    def _gross_exposure(self, sub_id: str) -> Decimal:
        """Current long market value of one sub-account (equity-based margin)."""
        return sum(
            (p.market_value for p in self.ledger.list_positions(sub_id)), ZERO
        )

    def _check_asset(self, req: OrderRequest) -> None:
        """Tradability pre-check when the venue can answer (None = skip)."""
        info = self.broker.get_asset(req.symbol)
        if info is None:
            return
        if not info.tradable:
            raise risk.RiskViolation("{} is not tradable (halted/delisted?)".format(req.symbol))
        wants_fraction = (req.notional is not None and req.notional > ZERO) or (
            req.qty > ZERO and req.qty != req.qty.to_integral_value()
        )
        if wants_fraction and not info.fractionable:
            raise risk.RiskViolation("{} is not fractionable".format(req.symbol))

    def _sync_one(self, order: OrderRecord) -> bool:
        """Fetch broker state for one order and book the delta. Returns True
        if anything changed."""
        if order.broker_order_id is None or order.status.is_terminal:
            return False
        try:
            state = self.broker.get_order(order.broker_order_id)
        except BrokerError as exc:
            logger.error("sync failed for order %s: %s", order.id, exc)
            return False
        return self._apply_state(order, state)

    def _apply_state(self, order: OrderRecord, state: BrokerOrderState) -> bool:
        """Book one order's new broker state (from polling or the stream)."""
        if order.status.is_terminal:
            return False
        new_status = _BROKER_TO_LOCAL.get(state.status, OrderStatus.OPEN)
        changed = (
            new_status != order.status or state.filled_qty != order.filled_qty
        )
        if not changed:
            return False

        acct = self.ledger.get_sub_account(order.sub_account_id)
        pos = self.ledger.get_position(order.sub_account_id, order.symbol)
        group = self._exit_group(order)

        delta_qty = state.filled_qty - order.filled_qty
        if delta_qty > ZERO:
            # Cumulative avg -> notional delta for exactly this increment.
            delta_notional = (
                state.filled_qty * state.filled_avg_price
                - order.filled_qty * order.filled_avg_price
            )
            fill_price = delta_notional / delta_qty
            if group is not None:
                self._book_group_sell(order, group, acct, pos, delta_qty, fill_price, delta_notional)
            else:
                self._book_fill(order, acct, pos, delta_qty, fill_price, delta_notional)
            order.filled_qty = state.filled_qty
            order.filled_avg_price = state.filled_avg_price

        order.status = new_status
        if new_status.is_terminal:
            if group is not None:
                self.ledger.save_order(order)  # persist before group inspection
                self._maybe_release_group(order, group, pos)
            else:
                self._release_remaining(order, acct, pos)
            if new_status == OrderStatus.REJECTED:
                order.reject_reason = order.reject_reason or "rejected by broker"

        self.ledger.save_sub_account(acct)
        self.ledger.save_position(pos)
        self.ledger.save_order(order)

        # Entry fill of a bracket/OTO: move the bought shares straight into
        # the exit group's shared reserve so nothing else can sell them.
        if (
            delta_qty > ZERO
            and order.side == OrderSide.BUY
            and order.order_class in (OrderClass.BRACKET, OrderClass.OTO)
            and order.parent_order_id is None
        ):
            legs = self.ledger.list_legs(order.id)
            if legs:
                holder = legs[0]
                holder.reserved += delta_qty
                pos = self.ledger.get_position(order.sub_account_id, order.symbol)
                pos.reserved_qty += delta_qty
                self.ledger.save_order(holder)
                self.ledger.save_position(pos)
        return True

    def _book_fill(
        self,
        order: OrderRecord,
        acct: SubAccount,
        pos,
        delta_qty: Decimal,
        fill_price: Decimal,
        delta_notional: Decimal,
    ) -> None:
        if order.side == OrderSide.BUY:
            # Release the reserved slice covering this fill, then pay cash.
            if order.notional is not None and order.notional > ZERO:
                share = delta_notional / order.notional
                release = min(order.reserved, order.reserved * share + Decimal("0.0000001"))
            else:
                remaining_qty = order.qty - order.filled_qty
                release = (
                    order.reserved
                    if delta_qty >= remaining_qty
                    else order.reserved * delta_qty / remaining_qty
                )
            order.reserved -= release
            acct.reserved_cash -= release
            acct.cash -= delta_notional
            new_qty = pos.qty + delta_qty
            if new_qty != ZERO:
                pos.avg_cost = (pos.qty * pos.avg_cost + delta_notional) / new_qty
            pos.qty = new_qty
        else:
            realized = (fill_price - pos.avg_cost) * delta_qty
            acct.cash += delta_notional
            acct.realized_pnl += realized
            acct.realized_pnl_today += realized
            pos.qty -= delta_qty
            pos.reserved_qty -= delta_qty
            order.reserved -= delta_qty
            if pos.qty == ZERO:
                pos.avg_cost = ZERO
        pos.last_price = fill_price
        logger.info(
            "fill: %s %s %s @ %s (sub-account %s, realized so far %s)",
            order.side.value, delta_qty, order.symbol, fill_price,
            acct.id, acct.realized_pnl,
        )

    def _book_group_sell(
        self,
        order: OrderRecord,
        group: List[OrderRecord],
        acct: SubAccount,
        pos,
        delta_qty: Decimal,
        fill_price: Decimal,
        delta_notional: Decimal,
    ) -> None:
        """Sell fill on an exit-group member: book cash/P&L normally but take
        the share release from the group's shared reserve holder."""
        holder = self._holder(group)
        realized = (fill_price - pos.avg_cost) * delta_qty
        acct.cash += delta_notional
        acct.realized_pnl += realized
        acct.realized_pnl_today += realized
        pos.qty -= delta_qty
        pos.reserved_qty -= delta_qty
        if pos.qty == ZERO:
            pos.avg_cost = ZERO
        release = min(holder.reserved, delta_qty)
        holder.reserved -= release
        if holder.id == order.id:
            order.reserved = holder.reserved
        else:
            self.ledger.save_order(holder)
        pos.last_price = fill_price
        logger.info(
            "exit-group fill: %s %s %s @ %s (%s, sub-account %s, realized %s)",
            order.leg_role or order.order_class.value, delta_qty, order.symbol,
            fill_price, order.id, acct.id, realized,
        )

    def _maybe_release_group(self, current: OrderRecord, group: List[OrderRecord], pos) -> None:
        """When every member of an exit group is terminal, hand back whatever
        share reserve remains on the holder."""
        members = []
        for member in group:
            members.append(current if member.id == current.id else self.ledger.get_order(member.id))
        if not all(m.status.is_terminal for m in members):
            return
        holder = self._holder(members)
        if holder.reserved > ZERO:
            pos.reserved_qty -= holder.reserved
            holder.reserved = ZERO
            if holder.id == current.id:
                current.reserved = ZERO
            self.ledger.save_order(holder)

    @staticmethod
    def _release_remaining(order: OrderRecord, acct: SubAccount, pos) -> None:
        """On cancel/reject/expire of a simple order, hand back whatever is
        still reserved for the unfilled remainder."""
        if order.reserved <= ZERO:
            order.reserved = ZERO
            return
        if order.side == OrderSide.BUY:
            acct.reserved_cash -= order.reserved
        else:
            pos.reserved_qty -= order.reserved
        order.reserved = ZERO
