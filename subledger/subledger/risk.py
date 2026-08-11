"""Pre-trade risk checks, evaluated by the router before any order reaches
the broker. Each check raises RiskViolation with a human-readable reason."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .models import (
    ZERO,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    SubAccount,
)


class RiskViolation(Exception):
    pass


def buying_power(acct: SubAccount, gross_exposure: Decimal = ZERO) -> Decimal:
    """Equity-based buying power (Reg-T style):

        equity = cash + gross_exposure          (long market value)
        bp     = equity x multiplier - gross_exposure - reserved_cash

    Losses shrink equity and therefore shrink buying power; the nominal
    multiplier is fully reachable (equity x m of gross exposure). With
    multiplier 1 the formula reduces exactly to the cash-account rule
    (cash - reserved). Maintenance-margin calls and interest are still NOT
    modeled — the real account's margin engine at the broker is the backstop.
    """
    equity = acct.cash + gross_exposure
    return equity * acct.margin_multiplier - gross_exposure - acct.reserved_cash


def validate_structure(req: OrderRequest) -> None:
    """Shape checks that don't need ledger state."""
    has_qty = req.qty > ZERO
    has_notional = req.notional is not None and req.notional > ZERO
    if has_qty == has_notional:
        raise RiskViolation("exactly one of qty / notional must be positive")
    if has_notional and (
        req.order_type != OrderType.MARKET
        or req.order_class != OrderClass.SIMPLE
        or req.side != OrderSide.BUY
    ):
        # Sell-side notional would bypass the share-ownership check (qty is
        # unknown until fill), so only simple market BUYS may be dollar-sized.
        raise RiskViolation("notional orders must be simple market buys")

    if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
        if req.stop_price is None or req.stop_price <= ZERO:
            raise RiskViolation("{} order requires a positive stop_price".format(req.order_type.value))
    if req.order_type == OrderType.STOP_LIMIT and (req.limit_price is None or req.limit_price <= ZERO):
        raise RiskViolation("stop_limit order requires a positive limit_price")
    if (
        req.order_type == OrderType.LIMIT
        and req.order_class != OrderClass.OCO   # OCO's limit comes from take_profit
        and (req.limit_price is None or req.limit_price <= ZERO)
    ):
        raise RiskViolation("limit order requires a positive limit_price")
    if req.order_type == OrderType.TRAILING_STOP:
        has_pct = req.trail_percent is not None and req.trail_percent > ZERO
        has_px = req.trail_price is not None and req.trail_price > ZERO
        if has_pct == has_px:
            raise RiskViolation("trailing_stop requires exactly one of trail_percent / trail_price")

    if req.extended_hours and (
        req.order_type != OrderType.LIMIT
        or req.time_in_force.value != "day"
        or req.order_class != OrderClass.SIMPLE
    ):
        raise RiskViolation("extended_hours only supports simple DAY limit orders")

    if req.time_in_force.value in ("opg", "cls"):
        # Auction orders: market or limit, simple class only (broker rule).
        if req.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise RiskViolation("opg/cls supports market or limit orders only")
        if req.order_class != OrderClass.SIMPLE:
            raise RiskViolation("opg/cls supports simple orders only")

    if req.order_class == OrderClass.OCO:
        if req.side != OrderSide.SELL:
            raise RiskViolation("oco protects an existing long: side must be sell")
        if req.take_profit is None or req.stop_loss is None:
            raise RiskViolation("oco requires both take_profit and stop_loss")
        if req.order_type != OrderType.LIMIT:
            raise RiskViolation("oco parent must be a limit order (the take-profit leg)")
    elif req.order_class == OrderClass.BRACKET:
        if req.side != OrderSide.BUY:
            raise RiskViolation("bracket entry must be a buy")
        if req.take_profit is None or req.stop_loss is None:
            raise RiskViolation("bracket requires both take_profit and stop_loss")
    elif req.order_class == OrderClass.OTO:
        if req.side != OrderSide.BUY:
            raise RiskViolation("oto entry must be a buy")
        if (req.take_profit is None) == (req.stop_loss is None):
            raise RiskViolation("oto requires exactly one of take_profit / stop_loss")

    for spec, label in ((req.take_profit, "take_profit"), (req.stop_loss, "stop_loss")):
        if spec is None:
            continue
        if getattr(spec, "limit_price", None) is not None and spec.limit_price <= ZERO:
            raise RiskViolation("{} limit_price must be positive".format(label))
        if getattr(spec, "stop_price", None) is not None and spec.stop_price <= ZERO:
            raise RiskViolation("{} stop_price must be positive".format(label))


def check_order(
    acct: SubAccount,
    pos: Position,
    req: OrderRequest,
    est_price: Decimal,
    halted: bool,
    gross_exposure: Decimal = ZERO,
) -> Decimal:
    """Validate `req`; return the notional to reserve (buys) — ZERO for sells.

    `est_price` is the limit price for limit orders, the stop price for stop
    orders, or a recent mark for market orders. `gross_exposure` is the
    sub-account's current long market value (drives equity-based margin).
    """
    if halted:
        raise RiskViolation("router is halted (kill switch engaged)")
    if not acct.active:
        raise RiskViolation("sub-account {} is disabled".format(acct.id))

    validate_structure(req)

    if acct.symbol_whitelist is not None and req.symbol not in acct.symbol_whitelist:
        raise RiskViolation(
            "{} is not in sub-account {}'s symbol whitelist".format(req.symbol, acct.id)
        )

    if req.notional is not None and req.notional > ZERO:
        notional = req.notional
    else:
        if est_price <= ZERO:
            raise RiskViolation(
                "no price available for {} — cannot size the order".format(req.symbol)
            )
        notional = req.qty * est_price

    if acct.max_order_notional is not None and notional > acct.max_order_notional:
        raise RiskViolation(
            "order notional {} exceeds per-order cap {}".format(
                notional, acct.max_order_notional
            )
        )

    if acct.daily_loss_limit is not None and req.side == OrderSide.BUY:
        # Opening new exposure is blocked once today's realized loss breaches
        # the limit; closing (sell) orders stay allowed so the strategy can
        # still get flat.
        if -acct.realized_pnl_today >= acct.daily_loss_limit:
            raise RiskViolation(
                "daily loss limit hit ({} realized today, limit {})".format(
                    acct.realized_pnl_today, acct.daily_loss_limit
                )
            )

    if req.side == OrderSide.BUY:
        bp = buying_power(acct, gross_exposure)
        if notional > bp:
            raise RiskViolation(
                "insufficient buying power: need {}, have {} "
                "(cash {} - reserved {} x {})".format(
                    notional,
                    bp,
                    acct.cash,
                    acct.reserved_cash,
                    acct.margin_multiplier,
                )
            )
        return notional

    # SELL: this sub-account may only sell shares it owns (net of shares
    # already committed to other open sells). Selling shares that belong to
    # another sub-account's ledger would silently transfer P&L between
    # strategies, so it is always rejected unless shorting is enabled.
    # For OCO the two legs cover the SAME shares, so qty is reserved once.
    sellable = pos.qty - pos.reserved_qty
    if req.qty > sellable:
        if not acct.allow_short:
            raise RiskViolation(
                "cannot sell {} {}: sub-account holds {} sellable "
                "(shorting disabled)".format(req.qty, req.symbol, sellable)
            )
        if req.order_class != OrderClass.SIMPLE:
            raise RiskViolation("short selling supports simple orders only")
        short_notional = (req.qty - max(sellable, ZERO)) * est_price
        if short_notional > buying_power(acct, gross_exposure):
            raise RiskViolation(
                "insufficient buying power for short: need {}".format(short_notional)
            )
    return ZERO
