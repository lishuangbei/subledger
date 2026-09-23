"""Interactive Brokers adapter built on ib_async (imported lazily so the core
package keeps working without it).

    pip install ib_async            # or: pip install "subledger[ib]"

Talks to a running TWS / IB Gateway on its API port (paper 4002 / 7497,
live 4001 / 7496). ib_async is asyncio-based and NOT thread-safe, while the
router, reconciler, REST handlers and daemon loops call the adapter from
several threads — so the adapter owns a private event loop on a daemon
thread and marshals every call onto it. Every public method is synchronous
and thread-safe from the caller's point of view.

Identity mapping
    broker_order_id  = IB permId — stable across sessions and API clients
                       (orderIds are per-client and useless to a reader)
    client_order_id  = Order.orderRef ("sl.<sub>.<tag>"), visible to every
                       API client and stamped on executions, so attribution
                       survives daemon restarts
    OCO              = two SELL orders in one OCA group (ocaGroup = client id)
    bracket / OTO    = parent + attached children (parentId); IB cancels the
                       sibling exit when one fills
    replace          = a true modify (same permId); the returned id is unchanged

Commissions: every fill costs money at IB, which would show up as cash
drift after each trade. The adapter reports them through
get_cash_activities() as "fee" entries tagged with the order's client id,
so the reconciler charges them to the owning sub-account (see
Reconciler.attribute_cash_activities).

Market data: paper accounts usually have no real-time API entitlement;
last_prices() asks for the configured market-data type (3 = delayed, the
default) and falls back to portfolio marks for held symbols.

Extended hours: `extended_hours=True` sets outsideRth (pre-market 04:00 and
after-hours to 20:00 ET). IB additionally runs an OVERNIGHT session
(20:00-03:50 ET) for US stocks/ETFs on its own venue; a simple DAY limit
order placed with extended_hours while that session is open is routed to
exchange OVERNIGHT instead of SMART, so "extended hours" means "whichever
session is trading right now" — nearly round the clock for SPY/QQQ-class
names. Overnight orders expire at 03:50 ET like any DAY order there.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as _dt
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..models import (
    LEG_STOP_LOSS,
    LEG_TAKE_PROFIT,
    ZERO,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
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

logger = logging.getLogger("subledger.broker.ib")

# IB order-status strings -> normalized. Anything unknown counts as open.
_STATUS_MAP = {
    "PendingSubmit": "open",
    "ApiPending": "open",
    "PreSubmitted": "open",
    "Submitted": "open",
    "PendingCancel": "open",
    "ApiUpdate": "open",
    "ValidationError": "open",   # ib_async: a warning was attached, order still live
    "Filled": "filled",
    "Cancelled": "canceled",
    "ApiCancelled": "canceled",
    "Inactive": "rejected",
}
_DONE_STATES = ("Filled", "Cancelled", "ApiCancelled", "Inactive")

# Error codes IB attaches to orders that STAY live (ib_async's warning set),
# plus 202 "Order Canceled", which accompanies every cancellation including
# our own. Any other error code on a dead order means it was rejected.
_NON_REJECT_CODES = frozenset({105, 110, 165, 202, 321, 329, 399, 404, 434, 492, 10167})

_UNSET = 1e300        # ib_async's UNSET_DOUBLE sentinel is ~1.8e308
OVERNIGHT_RETRY_S = 3600   # after a direct-routing refusal, retry the overnight venue this often
# "This order will be directly routed to X ... Restriction is specified in
# Precautionary Settings": 10311 (generic venue) / 10329 (OVERNIGHT).
_DIRECT_ROUTING_CODES = frozenset({10311, 10329})
_US_PRIMARY = ("NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "ISLAND", "IEX")

_TIF = {
    TimeInForce.DAY: "DAY",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
    TimeInForce.OPG: "OPG",
    TimeInForce.CLS: "DAY",      # closing auction is an order TYPE at IB (MOC/LOC)
}

_TZ_ALIASES = {"US/Eastern": "America/New_York", "US/Central": "America/Chicago",
               "US/Pacific": "America/Los_Angeles", "EST": "America/New_York",
               "EST5EDT": "America/New_York"}


# -- pure helpers (no connection; unit-tested offline) ------------------------

def _num(value) -> float:
    """float() that treats None / NaN / IB's UNSET sentinel as 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f) or abs(f) >= _UNSET:
        return 0.0
    return f


def _dec(value) -> Decimal:
    """Decimal from a broker float, 6 dp, without float noise or 'E+' forms."""
    d = Decimal(str(round(_num(value), 6)))
    if d == d.to_integral_value():
        return d.quantize(Decimal(1))
    return d.quantize(Decimal("0.000001")).normalize()


def _ib_symbol(symbol: str) -> str:
    """Alpaca-style class shares use a dot (BRK.B); IB uses a space."""
    return symbol.upper().replace(".", " ")


def _symbol_of(contract) -> str:
    if getattr(contract, "secType", "") == "STK":
        return str(getattr(contract, "symbol", "") or "").replace(" ", ".")
    return str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "")


def _permid(trade) -> int:
    return int(getattr(trade.order, "permId", 0) or getattr(trade.orderStatus, "permId", 0) or 0)


def _rejected(trade) -> bool:
    for entry in getattr(trade, "log", None) or []:
        code = int(getattr(entry, "errorCode", 0) or 0)
        if code and code not in _NON_REJECT_CODES and not 2100 <= code < 2200:
            return True
    return False


def _last_message(trade) -> str:
    for entry in reversed(getattr(trade, "log", None) or []):
        if entry.message:
            return entry.message
    return trade.orderStatus.status


def _is_done(trade) -> bool:
    return trade.orderStatus.status in _DONE_STATES


def normalize_status(trade, filled: float) -> str:
    """IB status + fill state -> the adapter's normalized vocabulary."""
    status = trade.orderStatus.status
    if status == "Filled":
        return "filled"
    if status in ("Cancelled", "ApiCancelled", "Inactive"):
        if filled <= 0 and _rejected(trade):
            return "rejected"
        return "canceled"
    if filled > 0:
        return "partially_filled"
    return _STATUS_MAP.get(status, "open")


@dataclass
class OrderPlan:
    """What one OrderRequest becomes at IB: a parent Order plus exit legs.
    `attached` legs hang off the parent (bracket/OTO, parentId + transmit
    chain); otherwise the legs are OCA siblings of the parent (OCO)."""

    parent: Any
    legs: List[Tuple[Any, str]] = field(default_factory=list)
    attached: bool = False


def build_order_plan(req: OrderRequest, client_order_id: str, account: str = "") -> OrderPlan:
    """Translate an OrderRequest into ib_async Order objects (no connection)."""
    from ib_async import Order

    if req.notional is not None and req.notional > ZERO:
        raise BrokerError("ib adapter: notional (dollar-sized) orders are not supported; "
                          "size in whole shares")
    if req.qty <= ZERO:
        raise BrokerError("ib adapter: qty must be positive")
    action = "BUY" if req.side == OrderSide.BUY else "SELL"
    tif = _TIF[req.time_in_force]
    leg_tif = "GTC" if req.time_in_force in (TimeInForce.OPG, TimeInForce.CLS) else tif

    def base(action_, qty, ref, tif_=tif):
        return Order(action=action_, totalQuantity=float(qty), orderRef=ref, tif=tif_,
                     outsideRth=bool(req.extended_hours), account=account, transmit=True)

    def stop_leg(qty, ref, spec):
        leg = base("SELL", qty, ref, leg_tif)
        leg.auxPrice = float(spec.stop_price)
        if spec.limit_price is not None:
            leg.orderType = "STP LMT"
            leg.lmtPrice = float(spec.limit_price)
        else:
            leg.orderType = "STP"
        return leg

    if req.order_class == OrderClass.OCO:
        if req.take_profit is None or req.stop_loss is None:
            raise BrokerError("OCO needs both take_profit and stop_loss")
        tp = base("SELL", req.qty, client_order_id)
        tp.orderType = "LMT"
        tp.lmtPrice = float(req.take_profit.limit_price)
        sl = stop_leg(req.qty, client_order_id + "-sl", req.stop_loss)
        for order in (tp, sl):
            order.ocaGroup = client_order_id
            order.ocaType = 1            # cancel the rest, with block
        return OrderPlan(parent=tp, legs=[(sl, LEG_STOP_LOSS)], attached=False)

    parent = base(action, req.qty, client_order_id)
    _apply_type(parent, req)
    if req.order_class == OrderClass.SIMPLE:
        return OrderPlan(parent=parent)

    legs: List[Tuple[Any, str]] = []
    if req.take_profit is not None:
        tp = base("SELL", req.qty, client_order_id + "-tp", leg_tif)
        tp.orderType = "LMT"
        tp.lmtPrice = float(req.take_profit.limit_price)
        legs.append((tp, LEG_TAKE_PROFIT))
    if req.stop_loss is not None:
        legs.append((stop_leg(req.qty, client_order_id + "-sl", req.stop_loss), LEG_STOP_LOSS))
    if not legs:
        raise BrokerError("bracket/OTO needs at least one exit leg")
    return OrderPlan(parent=parent, legs=legs, attached=True)


def _apply_type(order, req: OrderRequest) -> None:
    closing = req.time_in_force == TimeInForce.CLS
    kind = req.order_type
    if kind == OrderType.MARKET:
        order.orderType = "MOC" if closing else "MKT"
    elif kind == OrderType.LIMIT:
        if req.limit_price is None:
            raise BrokerError("limit order needs limit_price")
        order.orderType = "LOC" if closing else "LMT"
        order.lmtPrice = float(req.limit_price)
    elif kind == OrderType.STOP:
        if req.stop_price is None:
            raise BrokerError("stop order needs stop_price")
        order.orderType = "STP"
        order.auxPrice = float(req.stop_price)
    elif kind == OrderType.STOP_LIMIT:
        if req.stop_price is None or req.limit_price is None:
            raise BrokerError("stop_limit order needs stop_price and limit_price")
        order.orderType = "STP LMT"
        order.auxPrice = float(req.stop_price)
        order.lmtPrice = float(req.limit_price)
    elif kind == OrderType.TRAILING_STOP:
        order.orderType = "TRAIL"
        if req.trail_percent is not None:
            order.trailingPercent = float(req.trail_percent)
        elif req.trail_price is not None:
            order.auxPrice = float(req.trail_price)
        else:
            raise BrokerError("trailing_stop needs trail_percent or trail_price")
    else:  # pragma: no cover
        raise BrokerError("unsupported order type {}".format(kind))


def parse_ib_hours(spec: str, tz_name: str, now: _dt.datetime) -> BrokerClock:
    """Turn IB's liquidHours string
    ("20260922:0930-20260922:1600;20260926:CLOSED;...", exchange time zone)
    into a BrokerClock with Alpaca-like semantics: next_close is the current
    session's close while open, otherwise the next session's close."""
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(_TZ_ALIASES.get(tz_name, tz_name))
    except Exception:
        tz = ZoneInfo("America/New_York")
    windows = []
    for part in (spec or "").split(";"):
        part = part.strip()
        if not part or "CLOSED" in part or "-" not in part:
            continue
        start, end = part.split("-", 1)
        try:
            s = _dt.datetime.strptime(start, "%Y%m%d:%H%M").replace(tzinfo=tz)
            e = _dt.datetime.strptime(end, "%Y%m%d:%H%M").replace(tzinfo=tz)
        except ValueError:
            continue
        if e <= s:
            e += _dt.timedelta(days=1)
        windows.append((s, e))
    windows.sort()
    local_now = now.astimezone(tz)
    current = next(((s, e) for s, e in windows if s <= local_now < e), None)
    upcoming = [(s, e) for s, e in windows if s > local_now]
    next_open = upcoming[0][0].isoformat() if upcoming else ""
    if current is not None:
        next_close = current[1].isoformat()
    else:
        next_close = upcoming[0][1].isoformat() if upcoming else ""
    return BrokerClock(is_open=current is not None, next_open=next_open, next_close=next_close)


class _DirectRoutingBlocked(BrokerError):
    """IB error 10329: the gateway's API precautions refuse direct routing."""


class _CancelEchoFilter(logging.Filter):
    """IB answers every cancellation — ours included — with error 202 "Order
    Canceled", which ib_async logs at ERROR. Downgrade that one to INFO so
    daemon logs only shout about real problems."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.levelno >= logging.ERROR and "Error 202," in record.getMessage():
                record.levelno = logging.INFO
                record.levelname = "INFO"
        except Exception:  # pragma: no cover
            pass
        return True


def _quiet_cancel_echo() -> None:
    wrapper_logger = logging.getLogger("ib_async.wrapper")
    if not any(isinstance(f, _CancelEchoFilter) for f in wrapper_logger.filters):
        wrapper_logger.addFilter(_CancelEchoFilter())


# -- the adapter ------------------------------------------------------------

class IBBroker(BrokerAdapter):
    name = "ib"

    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 1,
                 account: str = "", paper: bool = True, market_data_type: int = 3,
                 readonly: bool = False, connect_timeout: float = 10.0,
                 call_timeout: float = 30.0, ack_timeout: float = 10.0,
                 clock_symbol: str = "SPY", cash_currency: str = "USD",
                 ib_factory: Optional[Callable] = None):
        """paper: hard guard — the connected account's code must look like a
        paper account (D...) when True and like a live one when False;
        otherwise every call fails closed (wrong gateway / wrong port).
        readonly: a viewer connection (CLI views): the client id is assumed
        unique per process and no order fetch happens at connect.
        cash_currency: the ledger's currency. IB accounts can hold several
        currencies and its TotalCashValue is the base-currency conversion of
        all of them, which drifts with FX every few minutes; the adapter
        reports the cash balance in this one currency instead (other
        currencies stay outside the ledger). Empty string = TotalCashValue.
        ib_factory: test seam, returns an object with ib_async.IB's surface."""
        self._host = host
        self._port = int(port)
        self._client_id = int(client_id)
        self._account_hint = account or ""
        self._account = account or ""
        self._paper = bool(paper)
        self._mdt = int(market_data_type)
        self._readonly = bool(readonly)
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._ack_timeout = ack_timeout
        self._clock_symbol = clock_symbol
        self._cash_currency = (cash_currency or "").upper()
        self._ib_factory = ib_factory
        self._ib = None
        self._fatal: Optional[str] = None
        self._closing = False
        self._next_attempt = 0.0
        self._backoff = 2.0
        self._contracts: Dict[str, Any] = {}
        self._details: Dict[str, Any] = {}
        self._asset_cache: Dict[str, BrokerAsset] = {}
        self._hours: Optional[Tuple[str, str, str]] = None
        self._overnight: Dict[str, tuple] = {}
        self._overnight_blocked_until = 0.0     # monotonic; direct routing retried after it
        self._open_order_ticks: Dict[int, int] = {}
        self._order_req_lock: Optional[asyncio.Lock] = None
        self._listeners: List[Callable[[BrokerOrderState], None]] = []
        self._events: "queue.Queue[BrokerOrderState]" = queue.Queue()
        self._dispatcher: Optional[threading.Thread] = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="subledger-ib", daemon=True)
        self._thread.start()

    # -- loop plumbing ---------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, fn, *args, timeout: Optional[float] = None):
        """Run coroutine function `fn` on the adapter's loop and wait."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("IBBroker re-entered from its own event-loop thread")
        label = fn.__name__.lstrip("_")
        limit = timeout or self._call_timeout
        future = asyncio.run_coroutine_threadsafe(fn(*args), self._loop)
        try:
            return future.result(limit)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise BrokerError("ib {} timed out after {:.0f}s".format(label, limit))
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError("ib {} failed: {}".format(label, exc)) from exc

    async def _connect(self):
        if self._fatal:
            raise BrokerError(self._fatal)
        if self._ib is None:
            factory = self._ib_factory
            if factory is None:
                try:
                    from ib_async import IB
                except ImportError as exc:  # pragma: no cover
                    raise BrokerError("ib_async is not installed: pip install ib_async") from exc
                factory = IB
                _quiet_cancel_echo()
            self._ib = factory()
            self._order_req_lock = asyncio.Lock()
            self._ib.orderStatusEvent += self._on_order_status
            self._ib.openOrderEvent += self._on_open_order
            self._ib.disconnectedEvent += self._on_disconnected
        if self._ib.isConnected():
            return self._ib
        now = time.monotonic()
        if now < self._next_attempt:
            raise BrokerError("ib gateway {}:{} unreachable (retry in {:.0f}s)".format(
                self._host, self._port, self._next_attempt - now))
        try:
            await self._ib.connectAsync(self._host, self._port, clientId=self._client_id,
                                        timeout=self._connect_timeout, readonly=self._readonly,
                                        account=self._account_hint)
        except Exception as exc:
            self._next_attempt = now + self._backoff
            self._backoff = min(self._backoff * 2, 60.0)
            raise BrokerError("ib connect {}:{} (client {}) failed: {}".format(
                self._host, self._port, self._client_id, exc)) from exc
        self._backoff = 2.0
        accounts = list(self._ib.managedAccounts() or [])
        account = self._account_hint or (accounts[0] if accounts else "")
        if not account or (self._account_hint and self._account_hint not in accounts):
            self._ib.disconnect()
            raise BrokerError("ib: account {!r} is not among the gateway's accounts {}".format(
                self._account_hint, accounts))
        looks_paper = account.startswith("D")
        if looks_paper != self._paper:
            self._fatal = ("ib: account {} looks {} but the adapter is configured paper={} "
                           "— wrong gateway/port? refusing to trade".format(
                               account, "PAPER" if looks_paper else "LIVE", self._paper))
            self._ib.disconnect()
            raise BrokerError(self._fatal)
        self._account = account
        try:
            self._ib.reqMarketDataType(self._mdt)
        except Exception:  # pragma: no cover
            logger.warning("ib: reqMarketDataType(%s) failed", self._mdt)
        logger.info("ib connected %s:%s client %s account %s (%s)", self._host, self._port,
                    self._client_id, account, "paper" if self._paper else "LIVE")
        return self._ib

    def _on_disconnected(self, *_args) -> None:
        if self._closing:
            return
        logger.warning("ib gateway %s:%s disconnected; reconnecting on next call", self._host, self._port)

    def _on_open_order(self, trade) -> None:
        pid = _permid(trade)
        if pid:
            self._open_order_ticks[pid] = self._open_order_ticks.get(pid, 0) + 1

    def _on_order_status(self, trade) -> None:
        if self._listeners and getattr(trade.order, "orderRef", ""):
            try:
                self._events.put(self._to_state(trade))
            except Exception:  # pragma: no cover
                logger.exception("ib: could not normalize order event")

    def add_order_listener(self, callback: Callable[[BrokerOrderState], None]) -> None:
        """Push-based fills: callback(state) runs on a dispatcher thread for
        every order-status event of a router-tagged order. Safe to call the
        adapter from inside the callback."""
        self._listeners.append(callback)
        if self._dispatcher is None:
            self._dispatcher = threading.Thread(target=self._dispatch, name="subledger-ib-events",
                                                daemon=True)
            self._dispatcher.start()
        self._call(self._connect)

    def _dispatch(self) -> None:
        while True:
            state = self._events.get()
            for callback in list(self._listeners):
                try:
                    callback(state)
                except Exception:
                    logger.exception("ib order listener failed")

    def close(self) -> None:
        self._closing = True

        async def _disconnect():
            if self._ib is not None and self._ib.isConnected():
                self._ib.disconnect()
        try:
            self._call(_disconnect, timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    # -- lookups ----------------------------------------------------------

    def _find_trade(self, permid: int):
        for trade in self._ib.trades():
            if _permid(trade) == permid:
                return trade
        return None

    def _scan_ref(self, client_order_id: str):
        matches = [t for t in self._ib.trades()
                   if getattr(t.order, "orderRef", "") == client_order_id]
        if not matches:
            return None
        # A rejected direct-routed attempt and its SMART fallback share the
        # orderRef: prefer a live trade, then the most recent (highest permId).
        matches.sort(key=lambda t: (not _is_done(t), _permid(t)), reverse=True)
        return matches[0]

    async def _refresh_orders(self) -> None:
        async with self._order_req_lock:
            await asyncio.wait_for(self._ib.reqAllOpenOrdersAsync(), self._call_timeout)
            await asyncio.wait_for(self._ib.reqCompletedOrdersAsync(False), self._call_timeout)

    async def _trade_for(self, broker_order_id: str):
        try:
            permid = int(broker_order_id)
        except (TypeError, ValueError):
            raise BrokerError("ib: broker order id must be a permId, got {!r}".format(broker_order_id))
        trade = self._find_trade(permid)
        if trade is None:
            await self._refresh_orders()
            trade = self._find_trade(permid)
        if trade is None:
            raise BrokerError("ib: unknown order {}".format(broker_order_id))
        return trade

    def _fills_for(self, trade, permid: int) -> list:
        fills = list(getattr(trade, "fills", None) or [])
        if not fills and self._ib is not None:
            fills = [f for f in self._ib.fills() if f.execution.permId == permid]
        return fills

    def _to_state(self, trade) -> BrokerOrderState:
        permid = _permid(trade)
        filled = _num(trade.orderStatus.filled)
        avg = _num(trade.orderStatus.avgFillPrice)
        if filled <= 0:
            # Trades rebuilt from reqCompletedOrders carry no fill state in
            # orderStatus; the Order's filledQuantity and the executions do.
            filled = _num(getattr(trade.order, "filledQuantity", 0.0))
        if filled <= 0 or avg <= 0:
            fills = self._fills_for(trade, permid)
            shares = sum(_num(f.execution.shares) for f in fills)
            if shares > 0:
                notional = sum(_num(f.execution.shares) * _num(f.execution.price) for f in fills)
                filled = max(filled, shares)
                if avg <= 0:
                    avg = notional / shares
        return BrokerOrderState(
            broker_order_id=str(permid),
            client_order_id=str(getattr(trade.order, "orderRef", "") or ""),
            status=normalize_status(trade, filled),
            filled_qty=_dec(filled),
            filled_avg_price=_dec(avg),
            symbol=_symbol_of(trade.contract),
        )

    async def _contract(self, symbol: str, strict: bool = True):
        key = symbol.upper()
        cached = self._contracts.get(key)
        if cached is not None:
            return cached
        from ib_async import Stock

        details = await asyncio.wait_for(
            self._ib.reqContractDetailsAsync(Stock(_ib_symbol(key), "SMART", "USD")),
            self._call_timeout)
        details = [d for d in details if getattr(d.contract, "secType", "STK") == "STK"]
        if len(details) > 1:
            preferred = [d for d in details if d.contract.primaryExchange in _US_PRIMARY]
            details = preferred or details
        if not details:
            if strict:
                raise BrokerError("ib: no US stock contract found for {}".format(symbol))
            return None
        self._contracts[key] = details[0].contract
        self._details[key] = details[0]
        return details[0].contract

    async def _overnight_contract(self, symbol: str):
        """The OVERNIGHT-venue contract for `symbol` if IB lists one and its
        session is open right now; else None. Details are cached per day."""
        key = symbol.upper()
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        cached = self._overnight.get(key)
        if cached is None or cached[0] != today:
            from ib_async import Stock

            try:
                details = await asyncio.wait_for(
                    self._ib.reqContractDetailsAsync(Stock(_ib_symbol(key), "OVERNIGHT", "USD")),
                    self._call_timeout)
            except Exception:
                details = []
            if details:
                d = details[0]
                cached = (today, d.contract, d.tradingHours or "", d.timeZoneId or "US/Eastern")
            else:
                cached = (today, None, "", "")
            self._overnight[key] = cached
        _day, contract, hours, tz = cached
        if contract is None:
            return None
        clock = parse_ib_hours(hours, tz, _dt.datetime.now(_dt.timezone.utc))
        return contract if clock.is_open else None

    # -- BrokerAdapter ----------------------------------------------------

    def describe(self) -> dict:
        return {"broker": "ib", "mode": "paper" if self._paper else "LIVE",
                "gateway": "{}:{}".format(self._host, self._port), "client_id": self._client_id}

    def submit_order(self, req: OrderRequest, client_order_id: str) -> BrokerSubmitResult:
        return self._call(self._submit, req, client_order_id)

    async def _submit(self, req: OrderRequest, client_order_id: str) -> BrokerSubmitResult:
        ib = await self._connect()
        contract = await self._contract(req.symbol)
        if (req.extended_hours and time.monotonic() >= self._overnight_blocked_until
                and req.order_class == OrderClass.SIMPLE
                and req.order_type == OrderType.LIMIT and req.time_in_force == TimeInForce.DAY):
            overnight = await self._overnight_contract(req.symbol)
            if overnight is not None:
                try:
                    return await self._place(ib, overnight, req, client_order_id)
                except _DirectRoutingBlocked:
                    # The gateway's API precautions refuse direct routing
                    # (error 10329) until an operator ticks the direct-routing
                    # bypass. Fall back to SMART + outsideRth (rests until
                    # 04:00 ET) and stop trying for an hour, so an operator's
                    # change is picked up without a restart.
                    self._overnight_blocked_until = time.monotonic() + OVERNIGHT_RETRY_S
                    logger.warning("ib: direct routing to OVERNIGHT is blocked by the gateway's "
                                   "API precautions; %s falls back to SMART (pre-market); "
                                   "next direct attempt in %d min", req.symbol, OVERNIGHT_RETRY_S // 60)
        return await self._place(ib, contract, req, client_order_id)

    async def _place(self, ib, contract, req: OrderRequest, client_order_id: str) -> BrokerSubmitResult:
        plan = build_order_plan(req, client_order_id, account=self._account)
        orders = [plan.parent] + [order for order, _ in plan.legs]
        for order in orders:
            order.orderId = ib.client.getReqId()
        if plan.attached:
            for order, _ in plan.legs:
                order.parentId = plan.parent.orderId
            for order in orders[:-1]:
                order.transmit = False
            orders[-1].transmit = True
        trades = []
        try:
            for order in orders:
                trades.append(ib.placeOrder(contract, order))
            await self._wait_ack(trades)
        except BaseException:
            self._cancel_trades(trades)
            raise
        dead = [t for t in trades if _is_done(t) and _num(t.orderStatus.filled) <= 0]
        if dead:
            self._cancel_trades([t for t in trades if t not in dead])
            if getattr(contract, "exchange", "") == "OVERNIGHT" and any(
                    int(getattr(e, "errorCode", 0) or 0) in _DIRECT_ROUTING_CODES for e in dead[0].log):
                raise _DirectRoutingBlocked(_last_message(dead[0]))
            raise BrokerError("ib rejected {} {} {}: {}".format(
                req.side.value, req.qty, req.symbol, _last_message(dead[0])))
        legs = [
            BrokerLeg(broker_order_id=str(_permid(trade)), role=role,
                      client_order_id=str(trade.order.orderRef or ""))
            for trade, (_order, role) in zip(trades[1:], plan.legs)
        ]
        return BrokerSubmitResult(broker_order_id=str(_permid(trades[0])), legs=legs)

    async def _wait_ack(self, trades) -> None:
        deadline = time.monotonic() + self._ack_timeout
        while True:
            if all(_permid(t) or _is_done(t) for t in trades):
                return
            if time.monotonic() > deadline:
                raise BrokerError("ib: order acknowledgement timed out after {:.0f}s".format(
                    self._ack_timeout))
            await asyncio.sleep(0.05)

    def _cancel_trades(self, trades) -> None:
        for trade in trades:
            if _is_done(trade):
                continue
            try:
                self._ib.cancelOrder(trade.order)
            except Exception:  # pragma: no cover
                logger.exception("ib: rollback cancel failed for %s", trade.order.orderRef)

    def cancel_order(self, broker_order_id: str) -> None:
        self._call(self._cancel, broker_order_id)

    def _related(self, trade) -> Tuple[list, list]:
        """(siblings, children): OCA siblings and same-parent siblings must be
        cancelled explicitly (IB does not cancel them for us); attached
        children are cancelled by IB together with their parent, so they are
        only waited for."""
        siblings, children = [], []
        oca = getattr(trade.order, "ocaGroup", "") or ""
        order_id = int(trade.order.orderId or 0)
        parent_id = int(getattr(trade.order, "parentId", 0) or 0)
        client = int(trade.order.clientId or 0)
        for other in self._ib.trades():
            if other is trade:
                continue
            same_client = int(other.order.clientId or 0) == client
            other_parent = int(getattr(other.order, "parentId", 0) or 0)
            if oca and getattr(other.order, "ocaGroup", "") == oca:
                siblings.append(other)
            elif parent_id > 0 and same_client and other_parent == parent_id:
                siblings.append(other)
            elif order_id > 0 and same_client and other_parent == order_id:
                children.append(other)
        return siblings, children

    async def _cancel(self, broker_order_id: str) -> None:
        ib = await self._connect()
        trade = await self._trade_for(broker_order_id)
        if _is_done(trade):
            return
        siblings, children = self._related(trade)
        live = [t for t in [trade] + siblings if not _is_done(t)]
        for t in live:
            if int(t.order.clientId or 0) != self._client_id or int(t.order.orderId or 0) <= 0:
                raise BrokerError("ib: order {} was placed by API client {}; only that client "
                                  "(or the master client) can cancel it".format(
                                      _permid(t), t.order.clientId))
        for t in live:
            ib.cancelOrder(t.order)
        pending = live + [t for t in children if not _is_done(t)]
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and any(not _is_done(t) for t in pending):
            await asyncio.sleep(0.05)

    def replace_order(self, broker_order_id: str, qty: Optional[Decimal] = None,
                      limit_price: Optional[Decimal] = None, stop_price: Optional[Decimal] = None,
                      client_order_id: Optional[str] = None) -> str:
        """A true modify: same permId, so the returned id equals the input.
        The orderRef (our client id) is left untouched — the ledger record
        keeps its name."""
        return self._call(self._replace, broker_order_id, qty, limit_price, stop_price)

    async def _replace(self, broker_order_id, qty, limit_price, stop_price) -> str:
        import copy

        ib = await self._connect()
        trade = await self._trade_for(broker_order_id)
        if _is_done(trade):
            raise BrokerError("ib: cannot replace order in status {}".format(trade.orderStatus.status))
        if int(trade.order.clientId or 0) != self._client_id or int(trade.order.orderId or 0) <= 0:
            raise BrokerError("ib: order {} was placed by API client {}; only that client can "
                              "modify it".format(broker_order_id, trade.order.clientId))
        # Modify = placeOrder with the same orderId. Work on a COPY: ib_async
        # keeps trade.order as IB's view and refreshes it from the openOrder
        # echo, which is what we wait for as the acknowledgement. transmit
        # must be True even for bracket children (placed with False), or IB
        # parks the modification untransmitted.
        modified = copy.copy(trade.order)
        modified.transmit = True
        if qty is not None:
            modified.totalQuantity = float(qty)
        if limit_price is not None:
            if modified.orderType not in ("LMT", "STP LMT", "LOC"):
                raise BrokerError("ib: {} order has no limit price to replace".format(modified.orderType))
            modified.lmtPrice = float(limit_price)
        if stop_price is not None:
            if modified.orderType not in ("STP", "STP LMT"):
                raise BrokerError("ib: {} order has no stop price to replace".format(modified.orderType))
            modified.auxPrice = float(stop_price)
        permid = _permid(trade)
        n_log = len(trade.log)
        ib.placeOrder(trade.contract, modified)

        def echoed() -> bool:
            current = trade.order
            return (_num(current.totalQuantity) == _num(modified.totalQuantity)
                    and _num(current.lmtPrice) == _num(modified.lmtPrice)
                    and _num(current.auxPrice) == _num(modified.auxPrice))

        deadline = time.monotonic() + self._ack_timeout
        last_refresh = time.monotonic()
        while True:
            entries = list(trade.log[n_log + 1:])      # skip ib_async's own "Modify" entry
            error = next((e for e in entries if getattr(e, "errorCode", 0)), None)
            if error is not None:
                try:
                    await self._refresh_orders()      # ib_async may have flagged it cancelled
                except Exception:  # pragma: no cover
                    pass
                raise BrokerError("ib refused modify of {}: {}".format(broker_order_id, error.message))
            if echoed() or any(e.message == "Modified" for e in entries) or _is_done(trade):
                return str(permid)
            now = time.monotonic()
            if now > deadline:
                raise BrokerError("ib: modify of {} not acknowledged within {:.0f}s".format(
                    broker_order_id, self._ack_timeout))
            if now - last_refresh > 1.0:
                # Held orders (bracket children, pre-submitted stops) are not
                # always echoed spontaneously; ask for the open-order list.
                last_refresh = now
                try:
                    await self._refresh_orders()
                except Exception:  # pragma: no cover
                    pass
            await asyncio.sleep(0.05)

    def get_order(self, broker_order_id: str) -> BrokerOrderState:
        return self._call(self._get_order, broker_order_id)

    async def _get_order(self, broker_order_id: str) -> BrokerOrderState:
        await self._connect()
        return self._to_state(await self._trade_for(broker_order_id))

    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrderState]:
        return self._call(self._by_client_id, client_order_id)

    async def _by_client_id(self, client_order_id: str) -> Optional[BrokerOrderState]:
        ib = await self._connect()
        trade = self._scan_ref(client_order_id)
        if trade is None:
            await self._refresh_orders()
            trade = self._scan_ref(client_order_id)
        if trade is not None:
            return self._to_state(trade)
        # Last resort: an order that filled and aged out of the completed
        # list still has executions stamped with the orderRef.
        fills = [f for f in ib.fills() if f.execution.orderRef == client_order_id]
        if not fills:
            return None
        shares = sum(_num(f.execution.shares) for f in fills)
        notional = sum(_num(f.execution.shares) * _num(f.execution.price) for f in fills)
        return BrokerOrderState(
            broker_order_id=str(fills[0].execution.permId), client_order_id=client_order_id,
            status="filled", filled_qty=_dec(shares),
            filled_avg_price=_dec(notional / shares if shares else 0),
            symbol=_symbol_of(fills[0].contract))

    def list_orders(self, open_only: bool = False) -> List[BrokerOrderState]:
        return self._call(self._list_orders, open_only)

    async def _list_orders(self, open_only: bool) -> List[BrokerOrderState]:
        ib = await self._connect()
        async with self._order_req_lock:
            trades = list(await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), self._call_timeout))
            if not open_only:
                trades += list(await asyncio.wait_for(ib.reqCompletedOrdersAsync(False),
                                                      self._call_timeout))
        canonical: Dict[int, Any] = {}
        for trade in ib.trades():
            pid = _permid(trade)
            if pid:
                canonical.setdefault(pid, trade)
        out: List[BrokerOrderState] = []
        seen = set()
        for trade in trades:
            pid = _permid(trade)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            account = getattr(trade.order, "account", "") or ""
            if self._account and account and account != self._account:
                continue
            out.append(self._to_state(canonical.get(pid, trade)))
        return out

    def get_account(self) -> BrokerAccountState:
        return self._call(self._get_account)

    async def _get_account(self) -> BrokerAccountState:
        ib = await self._connect()
        values = await asyncio.wait_for(ib.accountSummaryAsync(self._account), self._call_timeout)

        def pick(tag: str) -> Optional[Decimal]:
            for v in values:
                if v.tag == tag and (not v.account or v.account == self._account) \
                        and v.currency in ("USD", "BASE", ""):
                    try:
                        return Decimal(str(v.value))
                    except Exception:
                        continue
            return None

        cash = pick("TotalCashValue")
        equity = pick("NetLiquidation")
        if self._cash_currency:
            by_ccy = {}
            for v in ib.accountValues(self._account):
                if v.currency == self._cash_currency and v.tag in ("CashBalance", "NetLiquidationByCurrency"):
                    try:
                        by_ccy[v.tag] = Decimal(str(v.value))
                    except Exception:
                        continue
            if "CashBalance" in by_ccy:
                cash = by_ccy["CashBalance"]
                equity = by_ccy.get("NetLiquidationByCurrency", equity)
        if cash is None or equity is None:
            raise BrokerError("ib: account summary incomplete (TotalCashValue / NetLiquidation)")
        marks: Dict[int, float] = {}
        for item in ib.portfolio(self._account):
            price = _num(item.marketPrice)
            if price > 0:
                marks[item.contract.conId] = price
        positions = []
        for pos in ib.positions(self._account):
            qty = _num(pos.position)
            if qty == 0:
                continue
            price = marks.get(pos.contract.conId, 0.0) or _num(pos.avgCost)
            positions.append(BrokerPosition(
                symbol=_symbol_of(pos.contract), qty=_dec(qty),
                avg_entry_price=_dec(pos.avgCost), current_price=_dec(price)))
        return BrokerAccountState(cash=cash, equity=equity, buying_power=pick("BuyingPower") or ZERO,
                                  positions=positions, account_id=self._account)

    def last_prices(self, symbols: List[str]) -> Dict[str, Decimal]:
        wanted = [s for s in symbols if s]
        if not wanted:
            return {}
        marks: Dict[str, Decimal] = {}
        try:
            marks = self._call(self._last_prices, wanted)
        except Exception as exc:
            logger.warning("ib last_prices failed: %s", exc)
        if len(marks) < len(wanted):
            try:
                for symbol, price in super().last_prices(wanted).items():
                    marks.setdefault(symbol, price)
            except Exception:
                pass
        return marks

    async def _last_prices(self, symbols: List[str]) -> Dict[str, Decimal]:
        ib = await self._connect()
        contracts = {}
        for symbol in symbols:
            contract = await self._contract(symbol, strict=False)
            if contract is not None:
                contracts[symbol] = contract
        marks: Dict[str, Decimal] = {}
        if not contracts:
            return marks
        tickers = await asyncio.wait_for(ib.reqTickersAsync(*contracts.values()), self._call_timeout)
        by_conid = {t.contract.conId: t for t in tickers}
        for symbol, contract in contracts.items():
            ticker = by_conid.get(contract.conId)
            if ticker is None:
                continue
            for candidate in (ticker.marketPrice(), ticker.last, ticker.close):
                price = _num(candidate)
                if price > 0:
                    marks[symbol] = _dec(price)
                    break
        return marks

    def get_clock(self) -> BrokerClock:
        return self._call(self._clock)

    async def _clock(self) -> BrokerClock:
        ib = await self._connect()
        now = _dt.datetime.now(_dt.timezone.utc)
        today = now.date().isoformat()
        if self._hours is None or self._hours[0] != today:
            from ib_async import Stock

            details = await asyncio.wait_for(
                ib.reqContractDetailsAsync(Stock(self._clock_symbol, "SMART", "USD")),
                self._call_timeout)
            if not details:
                raise BrokerError("ib: no contract details for clock symbol {}".format(self._clock_symbol))
            d = details[0]
            self._hours = (today, d.liquidHours or d.tradingHours or "", d.timeZoneId or "US/Eastern")
        return parse_ib_hours(self._hours[1], self._hours[2], now)

    def get_asset(self, symbol: str) -> Optional[BrokerAsset]:
        cached = self._asset_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            contract = self._call(self._contract, symbol, False)
        except BrokerError:
            return None                      # gateway hiccup: skip the pre-check
        info = BrokerAsset(symbol=symbol, tradable=contract is not None, fractionable=False,
                           shortable=False, easy_to_borrow=False)
        if contract is not None:
            self._asset_cache[symbol] = info
        return info

    def get_cash_activities(self, after: str = "") -> List[BrokerCashActivity]:
        return self._call(self._cash_activities, after)

    async def _cash_activities(self, after: str) -> List[BrokerCashActivity]:
        ib = await self._connect()
        cutoff = None
        if after:
            try:
                cutoff = _dt.datetime.fromisoformat(after)
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=_dt.timezone.utc)
            except ValueError:
                cutoff = None
        out: List[BrokerCashActivity] = []
        for fill in ib.fills():
            execution = fill.execution
            account = getattr(execution, "acctNumber", "") or ""
            if self._account and account and account != self._account:
                continue
            commission = _num(getattr(fill.commissionReport, "commission", 0.0))
            if commission == 0:
                continue                     # not reported yet (or free)
            when = getattr(fill, "time", None)
            if cutoff is not None and isinstance(when, _dt.datetime) and when.tzinfo is not None \
                    and when < cutoff:
                continue
            out.append(BrokerCashActivity(
                activity_id="ib-comm-{}".format(execution.execId),
                kind="fee",
                # IB books cash in cents; its commission floats carry noise
                # (1.000003) that would otherwise accumulate as sub-cent drift.
                amount=-_dec(commission).quantize(Decimal("0.01")),
                symbol=_symbol_of(fill.contract),
                at=when.isoformat() if isinstance(when, _dt.datetime) else "",
                order_client_id=str(getattr(execution, "orderRef", "") or ""),
            ))
        return out
