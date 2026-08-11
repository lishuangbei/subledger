"""Core data model. Pure stdlib; money is Decimal everywhere."""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

ZERO = Decimal("0")

# client_order_id prefix that marks an order as router-originated. Anything at
# the broker without this prefix is, by definition, foreign to the router and
# gets flagged by the reconciler (unless its broker id is known to the ledger,
# e.g. a broker-generated OCO/bracket child leg, or it has been acknowledged).
CLIENT_ID_PREFIX = "sl"

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def new_client_order_id(sub_account_id: str, tag: Optional[str] = None) -> str:
    """"sl.<sub>.<tag-or-uuid12>" — fits Alpaca's 128-char client_order_id
    limit and lets any fill be attributed to its sub-account without a DB
    lookup. A caller-supplied tag makes the id deterministic (idempotent
    submits, audit-friendly ids like "alpha-20260807-AAPL-entry")."""
    if tag is not None:
        if not _TAG_RE.match(tag):
            raise ValueError(
                "client tag must match [A-Za-z0-9_-]{{1,64}}, got: {!r}".format(tag)
            )
        suffix = tag
    else:
        suffix = uuid.uuid4().hex[:12]
    return "{}.{}.{}".format(CLIENT_ID_PREFIX, sub_account_id, suffix)


def sub_account_of(client_order_id: str) -> Optional[str]:
    parts = client_order_id.split(".")
    if len(parts) == 3 and parts[0] == CLIENT_ID_PREFIX:
        return parts[1]
    return None


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class OrderClass(str, enum.Enum):
    SIMPLE = "simple"
    # OCO: two linked SELL exits (take-profit limit + stop loss) protecting an
    # existing long position; at most one fills, the other auto-cancels.
    OCO = "oco"
    # BRACKET: entry order plus both exit legs in one submission.
    BRACKET = "bracket"
    # OTO: entry order plus a single exit leg (take-profit OR stop-loss).
    OTO = "oto"


class TimeInForce(str, enum.Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"   # fill-or-kill
    OPG = "opg"   # execute in the opening auction (MOO/LOO)
    CLS = "cls"   # execute in the closing auction (MOC/LOC)


class OrderStatus(str, enum.Enum):
    # Local lifecycle; broker states map onto these.
    PENDING = "pending"          # accepted by router, submitted to broker
    OPEN = "open"                # live at the broker (includes held bracket legs)
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


# Exit-leg roles on OCO/BRACKET/OTO orders.
LEG_TAKE_PROFIT = "take_profit"
LEG_STOP_LOSS = "stop_loss"


@dataclass(frozen=True)
class TakeProfitSpec:
    limit_price: Decimal


@dataclass(frozen=True)
class StopLossSpec:
    stop_price: Decimal
    limit_price: Optional[Decimal] = None  # None -> stop-market


@dataclass
class SubAccount:
    id: str
    name: str = ""
    # Capital currently attributed to this sub-account. Decreases on buys,
    # increases on sells. May go negative only when margin_multiplier > 1.
    cash: Decimal = ZERO
    # Notional reserved by open buy orders (released on fill/cancel).
    reserved_cash: Decimal = ZERO
    # 1.0 = cash account behaviour; 2.0 = Reg-T style intraday margin.
    margin_multiplier: Decimal = Decimal("1")
    # Optional risk limits; None disables the check.
    max_order_notional: Optional[Decimal] = None
    daily_loss_limit: Optional[Decimal] = None
    symbol_whitelist: Optional[List[str]] = None
    allow_short: bool = False
    active: bool = True
    realized_pnl: Decimal = ZERO
    realized_pnl_today: Decimal = ZERO


@dataclass
class Position:
    sub_account_id: str
    symbol: str
    qty: Decimal = ZERO
    avg_cost: Decimal = ZERO
    # Shares committed to open sell orders (released on fill/cancel).
    reserved_qty: Decimal = ZERO
    # Last known mark, refreshed from broker data during sync/reconcile.
    last_price: Decimal = ZERO

    @property
    def market_value(self) -> Decimal:
        px = self.last_price if self.last_price > ZERO else self.avg_cost
        return self.qty * px

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.last_price <= ZERO:
            return ZERO
        return (self.last_price - self.avg_cost) * self.qty


@dataclass
class OrderRequest:
    sub_account_id: str
    symbol: str
    side: OrderSide
    # qty XOR notional: exactly one must be positive. Notional orders are
    # dollar-sized (broker may fill fractional shares).
    qty: Decimal = ZERO
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None            # stop / stop_limit
    trail_percent: Optional[Decimal] = None         # trailing_stop (either)
    trail_price: Optional[Decimal] = None           # trailing_stop (or)
    notional: Optional[Decimal] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    extended_hours: bool = False                    # limit+day only (broker rule)
    order_class: OrderClass = OrderClass.SIMPLE
    take_profit: Optional[TakeProfitSpec] = None    # oco / bracket / oto
    stop_loss: Optional[StopLossSpec] = None        # oco / bracket / oto
    # Optional deterministic client-id suffix ([A-Za-z0-9_-]{1,64}); the full
    # client_order_id becomes "sl.<sub>.<tag>". Reusing a tag is an idempotency
    # error surfaced by the broker (duplicate client id).
    client_tag: Optional[str] = None


@dataclass
class OrderRecord:
    id: str
    client_order_id: str
    broker_order_id: Optional[str]
    sub_account_id: str
    symbol: str
    side: OrderSide
    qty: Decimal
    order_type: OrderType
    limit_price: Optional[Decimal]
    time_in_force: TimeInForce
    status: OrderStatus
    stop_price: Optional[Decimal] = None
    trail_percent: Optional[Decimal] = None
    trail_price: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    extended_hours: bool = False
    order_class: OrderClass = OrderClass.SIMPLE
    # Exit legs are their own OrderRecords linked to the parent submission.
    parent_order_id: Optional[str] = None
    leg_role: str = ""                     # "" | take_profit | stop_loss
    filled_qty: Decimal = ZERO
    filled_avg_price: Decimal = ZERO
    # Cash (buys) or shares (sells) still reserved for the unfilled remainder.
    # For OCO/bracket exit groups the share reserve lives on ONE record (the
    # group's reserve holder); see Router._reserve_holder.
    reserved: Decimal = ZERO
    reject_reason: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class DriftItem:
    kind: str      # "cash" | "position" | "unknown_order" | "unknown_position"
    detail: str
    expected: str = ""
    actual: str = ""
    broker_order_id: str = ""


@dataclass
class ReconciliationReport:
    ok: bool
    at: str
    expected_cash: Decimal = ZERO
    broker_cash: Decimal = ZERO
    drifts: List[DriftItem] = field(default_factory=list)
    positions_checked: int = 0
    acknowledged_orders_skipped: int = 0

    def summary(self) -> str:
        if self.ok:
            extra = (
                " ({} acknowledged foreign order(s) skipped)".format(self.acknowledged_orders_skipped)
                if self.acknowledged_orders_skipped else ""
            )
            return "reconciliation OK (cash {} == {}, {} symbols checked){}".format(
                self.expected_cash, self.broker_cash, self.positions_checked, extra
            )
        lines = ["reconciliation FAILED with {} drift(s):".format(len(self.drifts))]
        for d in self.drifts:
            lines.append(
                "  [{}] {} (expected={} actual={})".format(d.kind, d.detail, d.expected, d.actual)
            )
        return "\n".join(lines)


@dataclass
class EquitySnapshot:
    """Per-sub-account equity view, derived from ledger state."""

    sub_account_id: str
    cash: Decimal
    positions_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal

    @property
    def equity(self) -> Decimal:
        return self.cash + self.positions_value
