"""In-memory broker for tests and dry runs.

Simulates the full order surface the router supports:
  - market / limit / stop / stop_limit / trailing_stop
  - notional (dollar-sized market) orders with fractional fills
  - OCO / bracket / OTO order classes with sibling-cancel semantics
  - replace (returns a new broker order id, like Alpaca)

Market orders fill immediately at the current price; conditional orders rest
until `set_price` crosses them. External activity (a human trading in the
same real account) can be simulated with `inject_external_*`, which is what
the reconciler tests use.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Dict, List, Optional

from ..models import (
    LEG_STOP_LOSS,
    LEG_TAKE_PROFIT,
    ZERO,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderType,
)
from .base import (
    BrokerAccountState,
    BrokerAdapter,
    BrokerAsset,
    BrokerCashActivity,
    BrokerClock,
    BrokerError,
    BrokerLeg,
    BrokerOrderState,
    BrokerPosition,
    BrokerSubmitResult,
)


class MockBroker(BrokerAdapter):
    name = "mock"

    def __init__(self, cash: Decimal = Decimal("100000")):
        self.cash = cash
        self.positions: Dict[str, Dict[str, Decimal]] = {}  # symbol -> {qty, avg}
        self.prices: Dict[str, Decimal] = {}
        self.orders: Dict[str, dict] = {}
        self._seq = itertools.count(1)
        self.fill_market_orders = True
        self.clock_open = True
        self.assets: Dict[str, BrokerAsset] = {}   # per-symbol overrides for tests
        self.activities: List[BrokerCashActivity] = []
        self.account_id = "mock-account-1"

    # -- test helpers ---------------------------------------------------

    def set_price(self, symbol: str, price: Decimal) -> None:
        self.prices[symbol] = price
        for o in list(self.orders.values()):
            if o["status"] in ("open",) and o["symbol"] == symbol:
                self._try_fill(o)

    def inject_external_position(self, symbol: str, qty: Decimal, price: Decimal) -> None:
        self._apply_fill_to_account(symbol, qty, price)

    def inject_external_cash(self, delta: Decimal) -> None:
        self.cash += delta

    def inject_cash_activity(self, kind: str, amount: Decimal, symbol: str = "") -> str:
        """Simulate a dividend/fee posting: moves real cash and shows up in
        get_cash_activities, like Alpaca's activities endpoint."""
        activity_id = "act-{}".format(next(self._seq))
        self.cash += amount
        self.activities.append(BrokerCashActivity(
            activity_id=activity_id, kind=kind, amount=amount, symbol=symbol))
        return activity_id

    def get_cash_activities(self, after: str = ""):
        return list(self.activities)

    def inject_external_order(self, symbol: str, qty: Decimal, client_order_id: str) -> str:
        """An order placed outside the router (e.g. via the broker's app)."""
        oid = "mock-{}".format(next(self._seq))
        self.orders[oid] = self._order_dict(
            oid, client_order_id, symbol, "buy", qty, "market"
        )
        return oid

    # -- BrokerAdapter --------------------------------------------------

    def last_prices(self, symbols: List[str]) -> Dict[str, Decimal]:
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def submit_order(self, req: OrderRequest, client_order_id: str) -> BrokerSubmitResult:
        needs_mark = req.order_type == OrderType.MARKET or (
            req.notional is not None and req.notional > ZERO
        )
        if needs_mark and req.symbol not in self.prices:
            raise BrokerError("no market price for {}".format(req.symbol))
        if req.order_type == OrderType.TRAILING_STOP and req.symbol not in self.prices:
            raise BrokerError("no market price for {}".format(req.symbol))

        oid = "mock-{}".format(next(self._seq))
        order = self._order_dict(
            oid,
            client_order_id,
            req.symbol,
            req.side.value,
            req.qty,
            req.order_type.value,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
            notional=req.notional,
            trail_percent=req.trail_percent,
            trail_price=req.trail_price,
        )
        legs: List[BrokerLeg] = []

        if req.order_class == OrderClass.OCO:
            # Parent is the take-profit limit sell; child is the stop leg.
            order["limit_price"] = req.take_profit.limit_price
            leg_id = "mock-{}".format(next(self._seq))
            leg = self._order_dict(
                leg_id, "", req.symbol, "sell", req.qty,
                "stop_limit" if req.stop_loss.limit_price is not None else "stop",
                limit_price=req.stop_loss.limit_price,
                stop_price=req.stop_loss.stop_price,
            )
            order["sibling"] = leg_id
            leg["sibling"] = oid
            self.orders[oid] = order
            self.orders[leg_id] = leg
            legs.append(BrokerLeg(broker_order_id=leg_id, role=LEG_STOP_LOSS))
            self._try_fill(order)
            self._try_fill(leg)
            return BrokerSubmitResult(broker_order_id=oid, legs=legs)

        if req.order_class in (OrderClass.BRACKET, OrderClass.OTO):
            self.orders[oid] = order
            leg_ids = []
            for spec, role in ((req.take_profit, LEG_TAKE_PROFIT), (req.stop_loss, LEG_STOP_LOSS)):
                if spec is None:
                    continue
                leg_id = "mock-{}".format(next(self._seq))
                if role == LEG_TAKE_PROFIT:
                    leg = self._order_dict(
                        leg_id, "", req.symbol, "sell", req.qty, "limit",
                        limit_price=spec.limit_price,
                    )
                else:
                    leg = self._order_dict(
                        leg_id, "", req.symbol, "sell", req.qty,
                        "stop_limit" if spec.limit_price is not None else "stop",
                        limit_price=spec.limit_price, stop_price=spec.stop_price,
                    )
                leg["status"] = "held"  # activates when the entry fills
                leg["entry"] = oid
                self.orders[leg_id] = leg
                leg_ids.append(leg_id)
                legs.append(BrokerLeg(broker_order_id=leg_id, role=role))
            if len(leg_ids) == 2:
                self.orders[leg_ids[0]]["sibling"] = leg_ids[1]
                self.orders[leg_ids[1]]["sibling"] = leg_ids[0]
            order["exit_legs"] = leg_ids
            self._try_fill(order)
            return BrokerSubmitResult(broker_order_id=oid, legs=legs)

        self.orders[oid] = order
        self._try_fill(order)
        return BrokerSubmitResult(broker_order_id=oid)

    def cancel_order(self, broker_order_id: str) -> None:
        order = self.orders.get(broker_order_id)
        if order is None:
            raise BrokerError("unknown order {}".format(broker_order_id))
        if order["status"] in ("open", "held"):
            order["status"] = "canceled"
            for key in ("sibling",):
                sib = self.orders.get(order.get(key) or "")
                if sib is not None and sib["status"] in ("open", "held"):
                    sib["status"] = "canceled"
            for leg_id in order.get("exit_legs", []):
                leg = self.orders.get(leg_id)
                if leg is not None and leg["status"] in ("open", "held"):
                    leg["status"] = "canceled"

    def replace_order(
        self,
        broker_order_id: str,
        qty: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> str:
        old = self.orders.get(broker_order_id)
        if old is None:
            raise BrokerError("unknown order {}".format(broker_order_id))
        if old["status"] not in ("open",):
            raise BrokerError("cannot replace order in status {}".format(old["status"]))
        new_id = "mock-{}".format(next(self._seq))
        new = dict(old)
        new["id"] = new_id
        if client_order_id:
            new["client_order_id"] = client_order_id
        if qty is not None:
            new["qty"] = qty
        if limit_price is not None:
            new["limit_price"] = limit_price
        if stop_price is not None:
            new["stop_price"] = stop_price
        old["status"] = "replaced"
        self.orders[new_id] = new
        self._try_fill(new)
        return new_id

    def get_order(self, broker_order_id: str) -> BrokerOrderState:
        o = self.orders.get(broker_order_id)
        if o is None:
            raise BrokerError("unknown order {}".format(broker_order_id))
        return self._to_state(o)

    def get_order_by_client_id(self, client_order_id: str):
        for o in self.orders.values():
            if o["client_order_id"] == client_order_id:
                return self._to_state(o)
        return None

    def describe(self) -> dict:
        return {"broker": "mock", "mode": "sim"}

    def get_clock(self) -> BrokerClock:
        return BrokerClock(is_open=self.clock_open)

    def get_asset(self, symbol: str) -> BrokerAsset:
        return self.assets.get(symbol) or BrokerAsset(symbol=symbol)

    def list_orders(self, open_only: bool = False) -> List[BrokerOrderState]:
        out = []
        for o in self.orders.values():
            if open_only and o["status"] not in ("open", "held", "partially_filled"):
                continue
            out.append(self._to_state(o))
        return out

    def get_account(self) -> BrokerAccountState:
        positions = [
            BrokerPosition(
                symbol=s,
                qty=p["qty"],
                avg_entry_price=p["avg"],
                current_price=self.prices.get(s, p["avg"]),
            )
            for s, p in self.positions.items()
            if p["qty"] != ZERO
        ]
        equity = self.cash + sum(
            (p.qty * p.current_price for p in positions), ZERO
        )
        return BrokerAccountState(
            cash=self.cash, equity=equity, buying_power=self.cash,
            positions=positions, account_id=self.account_id,
        )

    # -- internals ------------------------------------------------------

    @staticmethod
    def _order_dict(oid, client_order_id, symbol, side, qty, order_type, **extra) -> dict:
        base = {
            "id": oid,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "type": order_type,
            "limit_price": None,
            "stop_price": None,
            "notional": None,
            "trail_percent": None,
            "trail_price": None,
            "trail_hwm": None,
            "triggered": False,
            "status": "open",
            "filled_qty": ZERO,
            "filled_avg_price": ZERO,
        }
        base.update(extra)
        return base

    def _try_fill(self, order: dict) -> None:
        if order["status"] not in ("open",):
            return
        px = self.prices.get(order["symbol"])
        if px is None:
            return
        otype = order["type"]

        if otype in ("stop", "stop_limit") and not order["triggered"]:
            sp = order["stop_price"]
            crossed = (order["side"] == "sell" and px <= sp) or (
                order["side"] == "buy" and px >= sp
            )
            if not crossed:
                return
            order["triggered"] = True
            if otype == "stop":
                otype = "market"
            else:
                otype = "limit"

        if otype == "trailing_stop":
            hwm = order["trail_hwm"]
            if order["side"] == "sell":
                hwm = px if hwm is None else max(hwm, px)
                order["trail_hwm"] = hwm
                trigger = (
                    hwm * (1 - order["trail_percent"] / 100)
                    if order["trail_percent"] is not None
                    else hwm - order["trail_price"]
                )
                if px > trigger:
                    return
            else:
                hwm = px if hwm is None else min(hwm, px)
                order["trail_hwm"] = hwm
                trigger = (
                    hwm * (1 + order["trail_percent"] / 100)
                    if order["trail_percent"] is not None
                    else hwm + order["trail_price"]
                )
                if px < trigger:
                    return
            otype = "market"

        if otype == "limit":
            lp = order["limit_price"]
            crossed = (order["side"] == "buy" and px <= lp) or (
                order["side"] == "sell" and px >= lp
            )
            if not crossed:
                return
            px = lp
        elif not self.fill_market_orders:
            return

        if order.get("notional") is not None and order["qty"] == ZERO:
            order["qty"] = order["notional"] / px  # fractional fill

        signed_qty = order["qty"] if order["side"] == "buy" else -order["qty"]
        self._apply_fill_to_account(order["symbol"], signed_qty, px)
        order["status"] = "filled"
        order["filled_qty"] = order["qty"]
        order["filled_avg_price"] = px

        # sibling-cancel (OCO / bracket exits)
        sib = self.orders.get(order.get("sibling") or "")
        if sib is not None and sib["status"] in ("open", "held"):
            sib["status"] = "canceled"
        # entry filled -> activate held exit legs
        for leg_id in order.get("exit_legs", []):
            leg = self.orders.get(leg_id)
            if leg is not None and leg["status"] == "held":
                leg["status"] = "open"
                self._try_fill(leg)

    def _apply_fill_to_account(self, symbol: str, signed_qty: Decimal, price: Decimal) -> None:
        pos = self.positions.setdefault(symbol, {"qty": ZERO, "avg": ZERO})
        new_qty = pos["qty"] + signed_qty
        if signed_qty > ZERO and new_qty != ZERO:
            pos["avg"] = (pos["qty"] * pos["avg"] + signed_qty * price) / new_qty
        pos["qty"] = new_qty
        self.cash -= signed_qty * price

    @staticmethod
    def _to_state(o: dict) -> BrokerOrderState:
        status = o["status"]
        if status in ("held", "replaced"):
            status = "open" if status == "held" else "canceled"
        return BrokerOrderState(
            broker_order_id=o["id"],
            client_order_id=o["client_order_id"],
            status=status,
            filled_qty=o["filled_qty"],
            filled_avg_price=o["filled_avg_price"],
            symbol=o["symbol"],
        )
