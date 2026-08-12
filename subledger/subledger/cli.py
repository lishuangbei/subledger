"""subledger CLI — agent-friendly operations on the ledger.

    python -m subledger.cli [--json] <command> ...

Commands
  accounts list
  accounts create <id> [--allocation N] [--name S] [--margin M]
                       [--max-order-notional N] [--daily-loss-limit N]
                       [--whitelist A,B,C] [--allow-short]
  accounts update <id> [--name S] [--margin M] [--max-order-notional N|none]
                       [--daily-loss-limit N|none] [--whitelist A,B|none]
                       [--allow-short|--no-allow-short] [--active|--inactive]
  accounts allocate <id> <amount>        (negative moves cash back to pool;
                                          ONLY free cash can move — value tied
                                          up in positions or reserved by open
                                          orders is refused)
  accounts delete <id>                   (must be flat; cash returns to pool)
  status <ID> | status --all
           one sub-account in depth (balances, buying power, positions,
           open orders) — or every account plus pool, totals and the
           latest reconciliation
  positions [--sub ID]
  equity   [--sub ID] [--since YYYY-MM-DD] [--limit N]
           equity history (per sub-account time series; the daemon records
           a row every minute during market hours)
  history  [--sub ID] [--symbol S] [--since YYYY-MM-DD] [--limit N]
           trade history: orders with fills, newest first
  orders   [--sub ID] [--open] [--limit N]
           full order log incl. rejected/canceled and OCO legs

Stack comes from the environment (same as the REST layer):
  SUBLEDGER_DB (default subledger.db), SUBLEDGER_BROKER=mock|alpaca,
  ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER

`--json` prints machine-readable output; without it, human tables.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from typing import Optional

from .ledger import Ledger
from .models import SubAccount
from .router import Router


def _dec_or_none(value: Optional[str]) -> Optional[Decimal]:
    if value is None or value.lower() == "none":
        return None
    return Decimal(value)


def _acct_dict(ledger: Ledger, router: Router, acct: SubAccount) -> dict:
    snap = router.equity_snapshot(acct.id)
    return {
        "id": acct.id,
        "name": acct.name,
        "active": acct.active,
        "cash": str(acct.cash),
        "reserved_cash": str(acct.reserved_cash),
        "positions_value": str(snap.positions_value),
        "equity": str(snap.equity),
        "realized_pnl": str(acct.realized_pnl),
        "unrealized_pnl": str(snap.unrealized_pnl),
        "margin_multiplier": str(acct.margin_multiplier),
        "max_order_notional": None if acct.max_order_notional is None else str(acct.max_order_notional),
        "daily_loss_limit": None if acct.daily_loss_limit is None else str(acct.daily_loss_limit),
        "symbol_whitelist": acct.symbol_whitelist,
        "allow_short": acct.allow_short,
    }


def _order_dict(o) -> dict:
    return {
        "id": o.id,
        "created_at": o.created_at,
        "updated_at": o.updated_at,
        "sub_account_id": o.sub_account_id,
        "symbol": o.symbol,
        "side": o.side.value,
        "qty": str(o.qty),
        "notional": None if o.notional is None else str(o.notional),
        "type": o.order_type.value,
        "order_class": o.order_class.value,
        "leg_role": o.leg_role,
        "limit_price": None if o.limit_price is None else str(o.limit_price),
        "stop_price": None if o.stop_price is None else str(o.stop_price),
        "time_in_force": o.time_in_force.value,
        "status": o.status.value,
        "filled_qty": str(o.filled_qty),
        "filled_avg_price": str(o.filled_avg_price),
        "client_order_id": o.client_order_id,
        "broker_order_id": o.broker_order_id,
        "reject_reason": o.reject_reason,
    }


def _emit(payload, as_json: bool, table_fn=None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif table_fn is not None:
        table_fn(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def _money(value, dash_zero: bool = False) -> str:
    """Human display: thousands separators, 2 decimals. JSON keeps full precision."""
    if value is None:
        return "-"
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    if dash_zero and d == 0:
        return "-"
    return "{:,.2f}".format(d)


def _accounts_table(rows):
    fmt = "{:6s} {:>14s} {:>14s} {:>14s} {:>11s} {:>11s} {:>5s}  {}"
    print(fmt.format("id", "cash", "positions", "equity", "realized", "unreal", "mult", "name"))
    for r in rows:
        print(fmt.format(
            r["id"], _money(r["cash"]), _money(r["positions_value"], dash_zero=True),
            _money(r["equity"]), _money(r["realized_pnl"], dash_zero=True),
            _money(r["unrealized_pnl"], dash_zero=True),
            r["margin_multiplier"], r["name"]))


def _orders_table(rows):
    fmt = "{:19s} {:8s} {:6s} {:5s} {:>9s} {:>10s} {:10s} {:11s} {}"
    print(fmt.format("created", "sub", "symbol", "side", "qty", "avg_price",
                     "status", "class/leg", "client_order_id"))
    for r in rows:
        cls = r["order_class"] + ("/" + r["leg_role"] if r["leg_role"] else "")
        print(fmt.format(r["created_at"][:19], r["sub_account_id"], r["symbol"],
                         r["side"], r["filled_qty"] or r["qty"],
                         _money(r["filled_avg_price"]),
                         r["status"], cls, r["client_order_id"]))


def run(stack, argv) -> int:
    """stack = (ledger, broker, router, reconciler); argv excludes program name."""
    ledger, broker, router, reconciler = stack

    parser = argparse.ArgumentParser(prog="subledger", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p_accounts = sub.add_parser("accounts")
    acc_sub = p_accounts.add_subparsers(dest="subcommand", required=True)
    acc_sub.add_parser("list")

    p_create = acc_sub.add_parser("create")
    p_create.add_argument("id")
    p_create.add_argument("--allocation", default="0")
    p_create.add_argument("--name", default="")
    p_create.add_argument("--margin", default="1")
    p_create.add_argument("--max-order-notional", default=None)
    p_create.add_argument("--daily-loss-limit", default=None)
    p_create.add_argument("--whitelist", default=None, help="comma-separated symbols")
    p_create.add_argument("--allow-short", action="store_true")

    p_update = acc_sub.add_parser("update")
    p_update.add_argument("id")
    p_update.add_argument("--name", default=None)
    p_update.add_argument("--margin", default=None)
    p_update.add_argument("--max-order-notional", default=None)
    p_update.add_argument("--daily-loss-limit", default=None)
    p_update.add_argument("--whitelist", default=None, help="comma-separated, or 'none'")
    p_update.add_argument("--allow-short", dest="allow_short", action="store_true", default=None)
    p_update.add_argument("--no-allow-short", dest="allow_short", action="store_false")
    p_update.add_argument("--active", dest="active", action="store_true", default=None)
    p_update.add_argument("--inactive", dest="active", action="store_false")

    p_alloc = acc_sub.add_parser("allocate")
    p_alloc.add_argument("id")
    p_alloc.add_argument("amount")

    p_delete = acc_sub.add_parser("delete")
    p_delete.add_argument("id")

    p_status = sub.add_parser("status")
    p_status.add_argument("id", nargs="?", default=None)
    p_status.add_argument("--all", dest="all_accounts", action="store_true")

    p_positions = sub.add_parser("positions")
    p_positions.add_argument("--sub", default=None)
    p_positions.add_argument("--broker", action="store_true",
                             help="the REAL account's net positions (live broker query), with ledger aggregate for comparison")

    p_equity = sub.add_parser("equity")
    p_equity.add_argument("--sub", default=None)
    p_equity.add_argument("--since", default=None, help="YYYY-MM-DD")
    p_equity.add_argument("--limit", type=int, default=50)

    p_history = sub.add_parser("history")
    p_history.add_argument("--sub", default=None)
    p_history.add_argument("--symbol", default=None)
    p_history.add_argument("--since", default=None, help="YYYY-MM-DD")
    p_history.add_argument("--limit", type=int, default=50)

    p_orders = sub.add_parser("orders")
    p_orders.add_argument("--sub", default=None)
    p_orders.add_argument("--open", action="store_true")
    p_orders.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)

    try:
        return _dispatch(args, ledger, broker, router)
    except (ValueError, KeyError) as exc:
        message = str(exc).strip("'")
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        else:
            print("error: {}".format(message), file=sys.stderr)
        return 1


def _dispatch(args, ledger, broker, router) -> int:
    if args.command == "accounts":
        if args.subcommand == "list":
            rows = [_acct_dict(ledger, router, a) for a in ledger.list_sub_accounts()]
            payload = {"accounts": rows, "unallocated_cash": str(ledger.unallocated_cash())}
            _emit(payload if args.json else rows, args.json, _accounts_table)
            if not args.json:
                print("unallocated pool: {}".format(ledger.unallocated_cash()))
        elif args.subcommand == "create":
            acct = SubAccount(
                id=args.id,
                name=args.name,
                margin_multiplier=Decimal(args.margin),
                max_order_notional=_dec_or_none(args.max_order_notional),
                daily_loss_limit=_dec_or_none(args.daily_loss_limit),
                symbol_whitelist=None if args.whitelist is None else args.whitelist.split(","),
                allow_short=args.allow_short,
            )
            created = router.create_sub_account(acct, Decimal(args.allocation))
            _emit(_acct_dict(ledger, router, created), args.json)
        elif args.subcommand == "update":
            fields = {}
            if args.name is not None:
                fields["name"] = args.name
            if args.margin is not None:
                fields["margin_multiplier"] = Decimal(args.margin)
            if args.max_order_notional is not None:
                fields["max_order_notional"] = _dec_or_none(args.max_order_notional)
            if args.daily_loss_limit is not None:
                fields["daily_loss_limit"] = _dec_or_none(args.daily_loss_limit)
            if args.whitelist is not None:
                fields["symbol_whitelist"] = (
                    None if args.whitelist.lower() == "none" else args.whitelist.split(","))
            if args.allow_short is not None:
                fields["allow_short"] = args.allow_short
            if args.active is not None:
                fields["active"] = args.active
            updated = ledger.update_sub_account_settings(args.id, **fields)
            _emit(_acct_dict(ledger, router, updated), args.json)
        elif args.subcommand == "allocate":
            ledger.allocate(args.id, Decimal(args.amount))
            _emit({"id": args.id, "moved": args.amount,
                   "cash": str(ledger.get_sub_account(args.id).cash),
                   "unallocated_cash": str(ledger.unallocated_cash())}, args.json)
        elif args.subcommand == "delete":
            returned = ledger.delete_sub_account(args.id)
            _emit({"deleted": args.id, "cash_returned_to_pool": str(returned),
                   "unallocated_cash": str(ledger.unallocated_cash())}, args.json)

    elif args.command == "positions":
        if args.broker:
            ledger_agg = ledger.aggregate_positions()
            rows = []
            for p in broker.get_account().positions:
                rows.append({
                    "symbol": p.symbol,
                    "broker_qty": str(p.qty),
                    "ledger_qty": str(ledger_agg.pop(p.symbol, "0")),
                    "avg_entry": str(p.avg_entry_price),
                    "current": str(p.current_price),
                    "market_value": str(p.qty * p.current_price),
                })
            for symbol, qty in ledger_agg.items():   # ledger-only leftovers
                rows.append({"symbol": symbol, "broker_qty": "0",
                             "ledger_qty": str(qty), "avg_entry": "-",
                             "current": "-", "market_value": "-"})
            if args.json:
                _emit(rows, True)
            else:
                fmt = "{:6s} {:>10s} {:>10s} {:>12s} {:>12s} {:>14s}  {}"
                print(fmt.format("symbol", "broker", "ledger", "avg_entry",
                                 "current", "mkt_value", ""))
                for r in rows:
                    flag = "" if r["broker_qty"] == r["ledger_qty"] else "  <-- MISMATCH"
                    print(fmt.format(r["symbol"], r["broker_qty"], r["ledger_qty"],
                                     _money(r["avg_entry"]), _money(r["current"]),
                                     _money(r["market_value"]), flag))
            return 0
        rows = [{
            "sub_account_id": p.sub_account_id, "symbol": p.symbol,
            "qty": str(p.qty), "reserved_qty": str(p.reserved_qty),
            "avg_cost": str(p.avg_cost), "last_price": str(p.last_price),
            "unrealized_pnl": str(p.unrealized_pnl),
        } for p in ledger.list_positions(args.sub) if p.qty != 0 or p.reserved_qty != 0]
        _emit(rows, args.json)

    elif args.command == "status":
        import os as _os

        from . import risk as _risk
        from .models import ZERO as _ZERO

        def instance_identity() -> dict:
            identity = {"label": _os.environ.get("SUBLEDGER_LABEL") or None,
                        "ledger": getattr(ledger, "path", "?")}
            identity.update(broker.describe())
            try:
                identity["account_id"] = broker.get_account().account_id
            except Exception:
                identity["account_id"] = "(broker unreachable)"
            return identity

        def print_identity(identity: dict) -> None:
            print("instance: {}  broker={} {}  account={}  ledger={}".format(
                identity.get("label") or "-", identity.get("broker"),
                identity.get("mode", ""), str(identity.get("account_id"))[:13],
                identity.get("ledger")))
            print("-" * 72)

        def account_status(acct) -> dict:
            view = _acct_dict(ledger, router, acct)
            gross = sum(
                (p.market_value for p in ledger.list_positions(acct.id)), _ZERO)
            view["buying_power"] = str(_risk.buying_power(acct, gross))
            view["positions"] = [{
                "symbol": p.symbol, "qty": str(p.qty),
                "reserved_qty": str(p.reserved_qty), "avg_cost": str(p.avg_cost),
                "last_price": str(p.last_price),
                "unrealized_pnl": str(p.unrealized_pnl),
            } for p in ledger.list_positions(acct.id) if p.qty != 0 or p.reserved_qty != 0]
            view["open_orders"] = [
                _order_dict(o) for o in ledger.list_orders(acct.id, open_only=True)]
            return view

        if args.all_accounts:
            accounts = [account_status(a) for a in ledger.list_sub_accounts()]
            payload = {
                "instance": instance_identity(),
                "accounts": accounts,
                "unallocated_cash": str(ledger.unallocated_cash()),
                "total_equity": str(sum(
                    (Decimal(a["equity"]) for a in accounts), _ZERO)
                    + ledger.unallocated_cash()),
                "halted": ledger.is_halted(),
                "latest_reconciliation": ledger.latest_reconciliation(),
            }
            if args.json:
                _emit(payload, True)
            else:
                print_identity(payload["instance"])
                _accounts_table(accounts)
                print("unallocated pool: {}   total equity: {}   halted: {}".format(
                    _money(payload["unallocated_cash"]), _money(payload["total_equity"]),
                    payload["halted"]))
                latest = payload["latest_reconciliation"]
                if latest:
                    print("latest reconciliation: ok={} at={}".format(
                        latest.get("ok"), latest.get("at")))
        elif args.id:
            view = account_status(ledger.get_sub_account(args.id))
            view["instance"] = instance_identity()
            if args.json:
                _emit(view, True)
            else:
                print_identity(view["instance"])
                money_keys = {"cash", "reserved_cash", "positions_value", "equity",
                              "buying_power", "realized_pnl", "unrealized_pnl",
                              "max_order_notional", "daily_loss_limit"}
                for key in ("id", "name", "active", "cash", "reserved_cash",
                            "positions_value", "equity", "buying_power",
                            "realized_pnl", "unrealized_pnl", "margin_multiplier",
                            "max_order_notional", "daily_loss_limit",
                            "symbol_whitelist", "allow_short"):
                    value = _money(view[key]) if key in money_keys else view[key]
                    print("{:20s} {}".format(key, value))
                if view["positions"]:
                    print("\npositions:")
                    pfmt = "  {:6s} {:>8s} {:>9s} {:>12s} {:>12s} {:>12s}"
                    print(pfmt.format("symbol", "qty", "reserved", "avg_cost", "last", "uPnL"))
                    for p in view["positions"]:
                        print(pfmt.format(
                            p["symbol"], p["qty"], p["reserved_qty"],
                            _money(p["avg_cost"]), _money(p["last_price"]),
                            _money(p["unrealized_pnl"])))
                if view["open_orders"]:
                    print("\nopen orders:")
                    for o in view["open_orders"]:
                        print("  {} {} {} x{} {} {}".format(
                            o["side"], o["symbol"], o["type"], o["qty"],
                            o["status"], o["client_order_id"]))
        else:
            raise ValueError("status needs a sub-account id or --all")

    elif args.command == "equity":
        rows = ledger.equity_history(args.sub, since=args.since, limit=args.limit)
        if args.json:
            _emit(rows, True)
        else:
            fmt = "{:20s} {:6s} {:>14s} {:>14s} {:>14s} {:>11s} {:>11s}"
            print(fmt.format("at", "sub", "cash", "positions", "equity",
                             "realized", "unrealized"))
            for r in rows:
                print(fmt.format(r["at"][:19], r["sub_account_id"], _money(r["cash"]),
                                 _money(r["positions_value"], dash_zero=True),
                                 _money(r["equity"]),
                                 _money(r["realized_pnl"], dash_zero=True),
                                 _money(r["unrealized_pnl"], dash_zero=True)))

    elif args.command in ("history", "orders"):
        orders = ledger.list_orders(
            getattr(args, "sub", None), open_only=getattr(args, "open", False))
        if args.command == "history":
            orders = [o for o in orders if o.filled_qty > 0]
            if args.symbol:
                orders = [o for o in orders if o.symbol == args.symbol.upper()]
            if args.since:
                orders = [o for o in orders if o.updated_at[:10] >= args.since]
        orders.sort(key=lambda o: o.updated_at, reverse=True)
        rows = [_order_dict(o) for o in orders[:args.limit]]
        _emit(rows, args.json, _orders_table)

    return 0


def _build_stack_from_env():
    from .api import _build_stack

    return _build_stack()


def main() -> None:
    sys.exit(run(_build_stack_from_env(), sys.argv[1:]))


if __name__ == "__main__":
    main()
