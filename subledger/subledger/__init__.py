"""subledger: virtual sub-account router for a single real brokerage account.

One real account (Alpaca today, IB later) is partitioned into virtual
sub-accounts, each with its own capital allocation, buying-power limits and
risk rules. Strategies submit orders to the Router, which validates them
against the owning sub-account's ledger, forwards them to the broker, books
fills back to the sub-account, and periodically reconciles the sum of all
sub-account ledgers against the real account.
"""

from .models import (
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderType,
    StopLossSpec,
    SubAccount,
    TakeProfitSpec,
    TimeInForce,
)
from .ledger import Ledger
from .router import Router
from .reconciler import Reconciler

__all__ = [
    "Ledger",
    "OrderClass",
    "OrderRequest",
    "OrderSide",
    "OrderType",
    "Reconciler",
    "Router",
    "StopLossSpec",
    "SubAccount",
    "TakeProfitSpec",
    "TimeInForce",
]

__version__ = "0.3.0"
