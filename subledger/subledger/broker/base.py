"""Broker adapter interface.

Every venue (Alpaca now, IB later) implements this small surface. The router
and reconciler depend only on this module, never on a concrete SDK.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from ..models import OrderRequest


@dataclass
class BrokerOrderState:
    broker_order_id: str
    client_order_id: str
    # Normalized status: one of "open", "partially_filled", "filled",
    # "canceled", "rejected", "expired".
    status: str
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Decimal = Decimal("0")
    symbol: str = ""


@dataclass
class BrokerCashActivity:
    """A non-trade cash movement on the real account (dividend, fee, ...)."""

    activity_id: str
    kind: str                 # "dividend" | "fee" | "other"
    amount: Decimal           # signed: dividends positive, fees negative
    symbol: str = ""          # dividends carry the paying symbol; fees may not
    at: str = ""


@dataclass
class BrokerClock:
    is_open: bool
    next_open: str = ""
    next_close: str = ""


@dataclass
class BrokerAsset:
    symbol: str
    tradable: bool = True
    fractionable: bool = True
    shortable: bool = False
    easy_to_borrow: bool = False


@dataclass
class BrokerLeg:
    """A broker-generated child order of an OCO/bracket/OTO submission."""

    broker_order_id: str
    role: str  # "take_profit" | "stop_loss"
    client_order_id: str = ""


@dataclass
class BrokerSubmitResult:
    broker_order_id: str
    legs: List["BrokerLeg"] = field(default_factory=list)


@dataclass
class BrokerPosition:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")


@dataclass
class BrokerAccountState:
    cash: Decimal
    equity: Decimal
    buying_power: Decimal = Decimal("0")
    positions: List[BrokerPosition] = field(default_factory=list)
    # The venue's account identifier — lets the router verify it is talking
    # to the account the operator intended (paper keys in a live server, or
    # vice versa, refuse to start).
    account_id: str = ""


class BrokerError(Exception):
    pass


class BrokerAdapter(abc.ABC):
    """Minimal surface a venue must provide."""

    name: str = "abstract"

    @abc.abstractmethod
    def submit_order(self, req: OrderRequest, client_order_id: str) -> BrokerSubmitResult:
        """Submit and return the broker order id plus any broker-generated
        child legs (OCO/bracket/OTO). Raise BrokerError on synchronous
        rejection."""

    @abc.abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        """Request cancellation (async: final state arrives via get_order).
        Canceling an OCO/bracket parent must cancel its live legs too."""

    def replace_order(
        self,
        broker_order_id: str,
        qty: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> str:
        """Replace price/qty of a live order; returns the (possibly new)
        broker order id. Venues without replace support raise BrokerError."""
        raise BrokerError("{} does not support replace_order".format(self.name))

    @abc.abstractmethod
    def get_order(self, broker_order_id: str) -> BrokerOrderState:
        """Current state of one order."""

    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrderState]:
        """Look an order up by our client id; None if the broker never saw it.
        Crash recovery (Router.repair) depends on this. Default: linear scan."""
        for state in self.list_orders(open_only=False):
            if state.client_order_id == client_order_id:
                return state
        return None

    def get_clock(self) -> BrokerClock:
        """Market session state. Venues without a clock report always-open."""
        return BrokerClock(is_open=True)

    def get_asset(self, symbol: str) -> Optional[BrokerAsset]:
        """Tradability metadata; None when the venue can't say (skip checks)."""
        return None

    def describe(self) -> dict:
        """Cheap, offline identity of this venue connection (no API call):
        at least {"broker": ...}; adapters add e.g. {"mode": "paper"|"live"}.
        Used by status surfaces to tell subledger instances apart."""
        return {"broker": self.name}

    def get_cash_activities(self, after: str = "") -> List["BrokerCashActivity"]:
        """Non-trade cash movements (dividends, fees) since `after` (ISO
        date). Venues without support return [] and the reconciler falls back
        to plain cash-drift reporting."""
        return []

    @abc.abstractmethod
    def list_orders(self, open_only: bool = False) -> List[BrokerOrderState]:
        """All (recent) orders on the account, including ones not placed by
        the router — the reconciler needs to see those too."""

    @abc.abstractmethod
    def get_account(self) -> BrokerAccountState:
        """Cash, equity and positions of the real account."""

    def last_prices(self, symbols: List[str]) -> Dict[str, Decimal]:
        """Best-effort marks for the given symbols. Default: derive from
        current positions; venues with a data API can override."""
        marks: Dict[str, Decimal] = {}
        for pos in self.get_account().positions:
            if pos.symbol in symbols and pos.current_price > Decimal("0"):
                marks[pos.symbol] = pos.current_price
        return marks
