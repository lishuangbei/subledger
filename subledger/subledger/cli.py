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
  status   app/runtime health: instance identity, booking-writer process,
           market clock, halt flag, latest reconciliation, equity-history
           freshness, open-order count — plus each sub-account's
           CONFIGURATION (margin, limits, whitelist; not balances)
  accounts show <ID>
           one account in depth: balances, live buying power, positions,
           open orders, settings
  positions [--sub ID] [--broker]
           ledger positions grouped by sub-account (with per-account value
           subtotals); --broker shows the REAL account's net positions with
           the ledger aggregate for comparison
  equity   [--sub ID] performance view: current equity, today's P&L,
           total P&L, max drawdown, sparkline (built from the per-minute
           history the daemon records). --raw dumps the raw time series
           [--since YYYY-MM-DD] [--limit N]
  returns  [--sub ID]
           per-account returns from past anchors to now, near -> far:
           today, 1W, 1M, 6M, YTD, 1Y (gain%%; windows predating the
           data fall back to inception, marked *)
  history  [--sub ID] [--symbol S] [--since YYYY-MM-DD] [--limit N]
           trade history: orders with fills, newest first
  orders   [--sub ID] [--open] [--limit N]
           full order log incl. rejected/canceled and OCO legs

Stack comes from the environment (same as the REST layer):
  SUBLEDGER_DB (default subledger.db), SUBLEDGER_BROKER=mock|alpaca,
  ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER

`--json` prints machine-readable output; without it, human tables.
`--<subid>` (all digits) is shorthand for `--sub <subid>`, e.g. `positions --1001`.
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
                         r["side"],
                         r["filled_qty"] if r["filled_qty"] not in ("0", "0E-10") else r["qty"],
                         _money(r["filled_avg_price"]),
                         r["status"], cls, r["client_order_id"]))


def run(stack, argv) -> int:
    """stack = (ledger, broker, router, reconciler); argv excludes program name."""
    import re as _re

    ledger, broker, router, reconciler = stack
    expanded = []
    for token in argv:
        m = _re.fullmatch(r"--(\d+)", token)
        expanded += ["--sub", m.group(1)] if m else [token]
    argv = expanded

    import os as _os_prog

    parser = argparse.ArgumentParser(prog=_os_prog.environ.get("SUBLEDGER_PROG", "subledger"),
                                     description=__doc__,
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

    sub.add_parser("status")

    p_show = acc_sub.add_parser("show")
    p_show.add_argument("id")

    p_positions = sub.add_parser("positions")
    p_positions.add_argument("--sub", default=None)
    p_positions.add_argument("--broker", action="store_true",
                             help="the REAL account's net positions (live broker query), with ledger aggregate for comparison")

    p_equity = sub.add_parser("equity")
    p_equity.add_argument("--sub", default=None)
    p_equity.add_argument("--since", default=None, help="YYYY-MM-DD")
    p_equity.add_argument("--limit", type=int, default=50)
    p_equity.add_argument("--raw", action="store_true",
                          help="raw per-minute rows instead of the performance summary")

    p_returns = sub.add_parser("returns")
    p_returns.add_argument("--sub", default=None)

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
            from .models import ZERO as _Z
            rows = [_acct_dict(ledger, router, a) for a in ledger.list_sub_accounts()]
            total = sum((Decimal(r["equity"]) for r in rows), _Z) + ledger.unallocated_cash()
            payload = {"accounts": rows,
                       "unallocated_cash": str(ledger.unallocated_cash()),
                       "total_equity": str(total),
                       "halted": ledger.is_halted()}
            _emit(payload if args.json else rows, args.json, _accounts_table)
            if not args.json:
                print("unallocated pool: {}   total equity: {}   halted: {}".format(
                    _money(ledger.unallocated_cash()), _money(total), ledger.is_halted()))
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
        elif args.subcommand == "show":
            from . import risk as _risk
            from .models import ZERO as _ZERO

            acct = ledger.get_sub_account(args.id)
            view = _acct_dict(ledger, router, acct)
            gross = sum((p.market_value for p in ledger.list_positions(acct.id)), _ZERO)
            view["buying_power"] = str(_risk.buying_power(acct, gross))
            view["positions"] = [{
                "symbol": p.symbol, "qty": str(p.qty),
                "reserved_qty": str(p.reserved_qty), "avg_cost": str(p.avg_cost),
                "last_price": str(p.last_price),
                "unrealized_pnl": str(p.unrealized_pnl),
            } for p in ledger.list_positions(acct.id) if p.qty != 0 or p.reserved_qty != 0]
            view["open_orders"] = [
                _order_dict(o) for o in ledger.list_orders(acct.id, open_only=True)]
            if args.json:
                _emit(view, True)
            else:
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
                    for pp in view["positions"]:
                        print(pfmt.format(pp["symbol"], pp["qty"], pp["reserved_qty"],
                                          _money(pp["avg_cost"]), _money(pp["last_price"]),
                                          _money(pp["unrealized_pnl"])))
                if view["open_orders"]:
                    print("\nopen orders:")
                    for o in view["open_orders"]:
                        print("  {} {} {} x{} {} {}".format(
                            o["side"], o["symbol"], o["type"], o["qty"],
                            o["status"], o["client_order_id"]))
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

        def _positions_table(items):
            from collections import OrderedDict
            groups = OrderedDict()
            for r in items:
                groups.setdefault(r["sub_account_id"], []).append(r)
            fmt = "  {:7s} {:>8s} {:>9s} {:>12s} {:>12s} {:>13s} {:>13s}"
            accounts = [a for a in ledger.list_sub_accounts()
                        if args.sub in (None, a.id)]
            known = {a.id for a in accounts}
            # cash is part of the picture: every (filtered) account shows,
            # including cash-only ones with zero positions
            for acct in accounts:
                rows_ = groups.get(acct.id, [])
                value = sum(Decimal(r["qty"]) * Decimal(r["last_price"]) for r in rows_)
                upnl = sum(Decimal(r["unrealized_pnl"]) for r in rows_)
                print("[{}]  {} 仓位  市值 {}  现金 {}  合计 {}  浮盈 {}".format(
                    acct.id, len(rows_), _money(value), _money(acct.cash),
                    _money(value + acct.cash), _money(upnl)))
                if rows_:
                    print(fmt.format("symbol", "qty", "reserved",
                                     "avg_cost", "last", "mkt_value", "uPnL"))
                for r in rows_:
                    print(fmt.format(r["symbol"], r["qty"], r["reserved_qty"],
                                     _money(r["avg_cost"]), _money(r["last_price"]),
                                     _money(Decimal(r["qty"]) * Decimal(r["last_price"])),
                                     _money(r["unrealized_pnl"])))
                print()
            for sub_id, rows_ in groups.items():   # rows for unknown accounts
                if sub_id not in known:
                    print("[{}]  (账户元数据缺失) {} 仓位".format(sub_id, len(rows_)))
            if args.sub is None:
                print("未分配池现金: {}".format(_money(ledger.unallocated_cash())))

        _emit(rows, args.json, _positions_table)

    elif args.command == "status":
        import os as _os

        from . import risk as _risk
        from .models import ZERO as _ZERO

        identity = {"label": _os.environ.get("SUBLEDGER_LABEL") or None,
                    "ledger": getattr(ledger, "path", "?")}
        identity.update(broker.describe())
        try:
            identity["account_id"] = broker.get_account().account_id
        except Exception:
            identity["account_id"] = "(broker unreachable)"

        writer = {"running": False, "pid": None}
        lock_path = str(getattr(ledger, "path", "")) + ".lock"
        try:
            pid = int(open(lock_path).read().strip())
            _os.kill(pid, 0)
            writer = {"running": True, "pid": pid}
        except (OSError, ValueError):
            pass

        try:
            clock = broker.get_clock()
            market = {"is_open": clock.is_open, "next_open": clock.next_open,
                      "next_close": clock.next_close}
        except Exception:
            market = {"is_open": None}

        latest_recon = ledger.latest_reconciliation()
        eq = ledger.equity_history(limit=1)
        configs = [{
            "id": a.id, "name": a.name, "active": a.active,
            "margin_multiplier": str(a.margin_multiplier),
            "max_order_notional": None if a.max_order_notional is None else str(a.max_order_notional),
            "daily_loss_limit": None if a.daily_loss_limit is None else str(a.daily_loss_limit),
            "whitelist_size": None if a.symbol_whitelist is None else len(a.symbol_whitelist),
            "allow_short": a.allow_short,
        } for a in ledger.list_sub_accounts()]
        payload = {
            "instance": identity,
            "writer": writer,
            "market": market,
            "halted": ledger.is_halted(),
            "open_orders": len(ledger.list_orders(open_only=True)),
            "latest_reconciliation": None if latest_recon is None else {
                "ok": latest_recon.get("ok"), "at": latest_recon.get("at")},
            "latest_equity_snapshot_at": eq[0]["at"] if eq else None,
            "accounts_config": configs,
        }
        if args.json:
            _emit(payload, True)
        else:
            print("instance: {}  broker={} {}  account={}".format(
                identity.get("label") or "-", identity.get("broker"),
                identity.get("mode", ""), str(identity.get("account_id"))[:13]))
            print("ledger:   {}".format(identity.get("ledger")))
            print("writer:   {}".format(
                "RUNNING (pid {})".format(writer["pid"]) if writer["running"]
                else "NOT RUNNING — no booking process holds the ledger"))
            print("market:   {}".format(
                "OPEN" if market.get("is_open") else "closed (next open {})".format(
                    market.get("next_open", "?")) if market.get("is_open") is not None
                else "unknown (broker unreachable)"))
            print("halted:   {}    open orders: {}".format(payload["halted"], payload["open_orders"]))
            lr = payload["latest_reconciliation"]
            print("reconcile: {}".format(
                "ok={} at={}".format(lr["ok"], lr["at"]) if lr else "never ran"))
            print("equity history: last row {}".format(payload["latest_equity_snapshot_at"] or "none"))
            print()
            fmt = "{:6s} {:6s} {:>5s} {:>14s} {:>12s} {:>9s} {:>6s}  {}"
            print(fmt.format("id", "active", "mult", "max_notional",
                             "loss_limit", "whitelist", "short", "name"))
            for c in configs:
                print(fmt.format(
                    c["id"], str(c["active"]), c["margin_multiplier"],
                    _money(c["max_order_notional"]), _money(c["daily_loss_limit"]),
                    "-" if c["whitelist_size"] is None else str(c["whitelist_size"]),
                    str(c["allow_short"]), c["name"]))

    elif args.command == "equity":
        if args.raw:
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
            return 0

        BLOCKS = "▁▂▃▄▅▆▇█"

        def spark(values, width):
            if not values:
                return ""
            if len(values) > width:            # sample down to width points
                step = len(values) / width
                values = [values[int(i * step)] for i in range(width)]
            lo, hi = min(values), max(values)
            if hi == lo:
                return BLOCKS[0] * len(values)
            return "".join(BLOCKS[int((v - lo) / (hi - lo) * (len(BLOCKS) - 1))]
                           for v in values)

        def perf(sub_id):
            rows = list(reversed(ledger.equity_history(sub_id, limit=5000)))
            if not rows:
                return None
            pnl = [Decimal(r["realized_pnl"]) + Decimal(r["unrealized_pnl"]) for r in rows]
            latest = rows[-1]
            day = latest["at"][:10]
            day_rows = [i for i, r in enumerate(rows) if r["at"][:10] == day]
            day_pnl = pnl[-1] - pnl[day_rows[0]] if day_rows else Decimal("0")
            peak_eq, dd = Decimal(rows[0]["equity"]), Decimal("0")
            run_max = pnl[0]
            for i, v in enumerate(pnl):
                run_max = max(run_max, v)
                peak_eq = max(peak_eq, Decimal(rows[i]["equity"]))
                dd = min(dd, v - run_max)
            eq = Decimal(latest["equity"])
            return {
                "sub_account_id": sub_id,
                "equity": str(eq),
                "day_pnl": str(day_pnl),
                "total_pnl": str(pnl[-1]),
                "max_drawdown": str(dd),
                "max_drawdown_pct": str((dd / peak_eq * 100).quantize(Decimal("0.01"))) if peak_eq else "0",
                "rows": len(rows),
                "since": rows[0]["at"][:16],
                "_spark_day": spark([float(pnl[i]) for i in day_rows], 24),
                "_spark_all": spark([float(v) for v in pnl], 60),
            }

        subs = [args.sub] if args.sub else [a.id for a in ledger.list_sub_accounts()]
        reports = [r for r in (perf(x) for x in subs) if r]
        if args.json:
            _emit([{k: v for k, v in r.items() if not k.startswith("_")}
                   for r in reports], True)
        elif args.sub and reports:
            r = reports[0]
            print("[{}]  since {}  ({} rows)".format(r["sub_account_id"], r["since"], r["rows"]))
            print("equity    {:>14s}".format(_money(r["equity"])))
            print("today     {:>14s}".format(_money(r["day_pnl"])))
            print("total P&L {:>14s}".format(_money(r["total_pnl"])))
            print("max DD    {:>14s}  ({}%)".format(_money(r["max_drawdown"]), r["max_drawdown_pct"]))
            print("P&L curve {}".format(r["_spark_all"]))
        else:
            fmt = "{:6s} {:>14s} {:>11s} {:>11s} {:>11s} {:>7s}  {}"
            print(fmt.format("id", "equity", "today", "total", "maxDD", "DD%", "today's P&L"))
            for r in reports:
                print(fmt.format(r["sub_account_id"], _money(r["equity"]),
                                 _money(r["day_pnl"], dash_zero=True),
                                 _money(r["total_pnl"], dash_zero=True),
                                 _money(r["max_drawdown"], dash_zero=True),
                                 r["max_drawdown_pct"], r["_spark_day"]))

    elif args.command == "returns":
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            now_et = now.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            now_et = now
        windows = [
            ("today", now_et.replace(hour=0, minute=0, second=0, microsecond=0)
                            .astimezone(_dt.timezone.utc)),
            ("1W", now - _dt.timedelta(days=7)),
            ("1M", now - _dt.timedelta(days=30)),
            ("6M", now - _dt.timedelta(days=182)),
            ("YTD", now_et.replace(month=1, day=1, hour=0, minute=0, second=0,
                                   microsecond=0).astimezone(_dt.timezone.utc)),
            ("1Y", now - _dt.timedelta(days=365)),
        ]

        def account_returns(sub_id):
            rows = list(reversed(ledger.equity_history(sub_id, limit=100000)))
            if not rows:
                return None
            def pnl(r):
                return Decimal(r["realized_pnl"]) + Decimal(r["unrealized_pnl"])
            latest = rows[-1]
            out = {"sub_account_id": sub_id, "equity": latest["equity"], "windows": []}
            for name, target in windows:
                tgt = target.isoformat()
                base, clipped = None, False
                for r in rows:                    # last row at-or-before target
                    if r["at"] <= tgt:
                        base = r
                    else:
                        break
                if base is None:
                    base, clipped = rows[0], True
                base_eq = Decimal(base["equity"])
                gain = pnl(latest) - pnl(base)
                ret = (gain / base_eq * 100) if base_eq else Decimal("0")
                # Cash flow inside the window: equity moves that P&L does not
                # explain (allocations, dividends, manual repairs). Any flow
                # distorts the percentage's denominator -> not reliable.
                net_flow = (Decimal(latest["equity"]) - base_eq) - gain
                reliable = (not clipped) and abs(net_flow) < Decimal("1")
                out["windows"].append({
                    "window": name, "clipped": clipped,
                    "gain": str(gain), "return_pct": str(ret.quantize(Decimal("0.01"))),
                    "net_flow": str(net_flow.quantize(Decimal("0.01"))),
                    "reliable": reliable,
                    "base_at": base["at"][:16],
                })
            return out

        subs = [args.sub] if args.sub else [a.id for a in ledger.list_sub_accounts()]
        reports = [r for r in (account_returns(x) for x in subs) if r]
        if args.json:
            _emit(reports, True)
        else:
            names = [w[0] for w in windows]
            colour = _returns_colour_enabled()
            head = "{:6s} {:>14s} " + " ".join(["{:>10s}"] * len(names))
            print(head.format("id", "equity", *names))
            for r in reports:
                cells = []
                for w in r["windows"]:
                    txt = "{:>9s}%".format(w["return_pct"])
                    if colour:
                        cells.append(_paint_return(txt, w["window"],
                                                   Decimal(w["return_pct"]), w["reliable"]))
                    else:
                        cells.append(txt + ("" if w["reliable"] else "?"))
                print(head.format(r["sub_account_id"], _money(r["equity"]), *cells))
            if colour:
                print(_returns_legend())
            else:
                print("(? = 不可信:窗口内有资金变动或持有期短于窗口;涨幅=ΔP&L/期初净值)")

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


# Returns colouring: green/red by sign, intensity by magnitude on a PER-WINDOW
# scale (1% in a day is news; 1% over a year is noise). White background marks
# a percentage that cannot be trusted — cash moved inside the window or the
# account is younger than the window. Thresholds are |return| in percent.
_RETURN_SCALE = {
    "today": (0.3, 0.8, 1.5, 3.0),
    "1W":    (0.8, 2.0, 4.0, 8.0),
    "1M":    (1.5, 4.0, 8.0, 15.0),
    "6M":    (4.0, 10.0, 20.0, 35.0),
    "YTD":   (5.0, 12.0, 25.0, 45.0),
    "1Y":    (6.0, 15.0, 30.0, 55.0),
}
_GREENS = (22, 28, 34, 40, 46)     # ANSI-256 backgrounds, dark -> bright
_REDS = (52, 88, 124, 160, 196)


def _returns_colour_enabled() -> bool:
    import os as _os
    if _os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty() or _os.environ.get("SUBLEDGER_COLOR") == "1"


def _paint_return(text: str, window: str, pct, reliable: bool) -> str:
    if not reliable:
        return "\033[48;5;231m\033[38;5;232m{}\033[0m".format(text)   # white bg
    thresholds = _RETURN_SCALE.get(window, _RETURN_SCALE["1M"])
    level = sum(1 for t in thresholds if abs(pct) >= t)               # 0..4
    if level == 0 and pct == 0:
        return text
    palette = _GREENS if pct > 0 else _REDS
    fg = 15 if level >= 2 or pct < 0 else 0
    return "\033[48;5;{}m\033[38;5;{}m{}\033[0m".format(palette[level], fg, text)


def _returns_legend() -> str:
    swatches = " ".join(
        "\033[48;5;{}m  \033[0m".format(c) for c in reversed(_REDS)) + " 0 " + " ".join(
        "\033[48;5;{}m  \033[0m".format(c) for c in _GREENS)
    return "{}   \033[48;5;231m\033[38;5;232m 白底 \033[0m = 不可信(窗口内有资金变动/持有期短于窗口)".format(swatches)


_VIEW_COMMANDS = {"status", "positions", "equity", "returns", "history", "orders"}


def _build_stack_from_env():
    from .api import _build_stack

    # Pure view commands open the ledger as a non-exclusive reader so they
    # work while a daemon holds the writer lock. Anything that can write
    # (accounts create/update/allocate/delete) keeps the exclusive-writer
    # path — LedgerLocked while a daemon runs is the guard, not a bug.
    import os

    words = [a for a in sys.argv[1:] if not a.startswith("-")]
    cmd = words[0] if words else ""
    sub = words[1] if len(words) > 1 else ""
    if cmd in _VIEW_COMMANDS or (cmd == "accounts" and sub in ("list", "show")):
        os.environ.setdefault("SUBLEDGER_READONLY", "1")
    return _build_stack()


def main() -> None:
    sys.exit(run(_build_stack_from_env(), sys.argv[1:]))


if __name__ == "__main__":
    main()
