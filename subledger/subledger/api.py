"""Optional REST layer (FastAPI) so strategies in any language/process can
talk to the router over HTTP.

    pip install "fastapi[standard]"
    uvicorn subledger.api:create_app --factory

Strategies only ever see their sub-account; the real account credentials
live solely in the router process.
"""

from __future__ import annotations

import os
import threading
import time
from decimal import Decimal
from typing import Optional

from .ledger import Ledger
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
from .reconciler import Reconciler
from .router import OrderRejected, Router

try:
    from pydantic import BaseModel as _BaseModel
    _HAVE_PYDANTIC = True
except ImportError:            # zero-dep core: models only exist with REST deps
    _HAVE_PYDANTIC = False

if _HAVE_PYDANTIC:
    # NOTE: these MUST live at module level. With `from __future__ import
    # annotations` every endpoint annotation is a string that FastAPI
    # resolves against module globals — function-local classes are invisible
    # there, and FastAPI silently degrades the body param to a required
    # query field (every write endpoint 422s).

    class TakeProfitBody(_BaseModel):
        limit_price: str

    class StopLossBody(_BaseModel):
        stop_price: str
        limit_price: Optional[str] = None

    class CreateAccountBody(_BaseModel):
        id: str
        name: str = ""
        allocation: str = "0"
        margin_multiplier: str = "1"
        max_order_notional: Optional[str] = None
        daily_loss_limit: Optional[str] = None
        symbol_whitelist: Optional[list] = None
        allow_short: bool = False

    class OrderBody(_BaseModel):
        sub_account_id: str
        symbol: str
        side: str
        qty: str = "0"
        notional: Optional[str] = None
        order_type: str = "market"
        limit_price: Optional[str] = None
        stop_price: Optional[str] = None
        trail_percent: Optional[str] = None
        trail_price: Optional[str] = None
        time_in_force: str = "day"
        extended_hours: bool = False
        order_class: str = "simple"
        take_profit: Optional[TakeProfitBody] = None
        stop_loss: Optional[StopLossBody] = None
        client_tag: Optional[str] = None

    class AllocateBody(_BaseModel):
        amount: str

    class ReplaceBody(_BaseModel):
        qty: Optional[str] = None
        limit_price: Optional[str] = None
        stop_price: Optional[str] = None


def _build_stack():
    """Assemble ledger/broker/router from environment variables."""
    ledger = Ledger(os.environ.get("SUBLEDGER_DB", "subledger.db"))
    broker_kind = os.environ.get("SUBLEDGER_BROKER", "mock")
    if broker_kind == "alpaca":
        from .broker.alpaca import AlpacaBroker

        broker = AlpacaBroker(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=os.environ.get("ALPACA_PAPER", "true").lower() != "false",
        )
    else:
        from .broker.mock import MockBroker

        broker = MockBroker()
    router = Router(
        ledger,
        broker,
        expected_account_id=os.environ.get("SUBLEDGER_EXPECTED_ACCOUNT_ID") or None,
    )
    reconciler = Reconciler(
        ledger,
        broker,
        halt_on_drift=os.environ.get("SUBLEDGER_HALT_ON_DRIFT", "false").lower() == "true",
    )
    return ledger, broker, router, reconciler


def create_app(stack=None, background_loops: bool = True):
    """stack: pass an existing (ledger, broker, router, reconciler) to host
    the API inside the process that already owns the ledger's writer lock
    (e.g. a trading daemon). background_loops=False skips the built-in
    sync/reconcile timers when the host process runs its own."""
    from fastapi import FastAPI, HTTPException

    ledger, broker, router, reconciler = stack if stack is not None else _build_stack()
    app = FastAPI(title="subledger", version="0.5.0")

    # Background loops: fill sync every few seconds, reconcile on a timer.
    sync_interval = float(os.environ.get("SUBLEDGER_SYNC_INTERVAL", "5"))
    reconcile_interval = float(os.environ.get("SUBLEDGER_RECONCILE_INTERVAL", "300"))

    def _loop():
        last_reconcile = 0.0
        while True:
            try:
                router.sync()
                now = time.monotonic()
                if now - last_reconcile >= reconcile_interval:
                    reconciler.run()
                    last_reconcile = now
            except Exception:  # keep the loop alive; errors are logged upstream
                import logging

                logging.getLogger("subledger.api").exception("background loop error")
            time.sleep(sync_interval)

    if background_loops:
        threading.Thread(target=_loop, daemon=True).start()

    @app.post("/accounts")
    def create_account(body: CreateAccountBody):
        acct = SubAccount(
            id=body.id,
            name=body.name,
            margin_multiplier=Decimal(body.margin_multiplier),
            max_order_notional=None
            if body.max_order_notional is None
            else Decimal(body.max_order_notional),
            daily_loss_limit=None
            if body.daily_loss_limit is None
            else Decimal(body.daily_loss_limit),
            symbol_whitelist=body.symbol_whitelist,
            allow_short=body.allow_short,
        )
        try:
            created = router.create_sub_account(acct, Decimal(body.allocation))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))
        return _acct_view(created)

    @app.get("/accounts")
    def list_accounts():
        return [_acct_view(a) for a in ledger.list_sub_accounts()]

    @app.get("/accounts/{sub_id}")
    def get_account(sub_id: str):
        try:
            acct = ledger.get_sub_account(sub_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        snap = router.equity_snapshot(sub_id)
        view = _acct_view(acct)
        view["equity"] = str(snap.equity)
        view["positions_value"] = str(snap.positions_value)
        view["unrealized_pnl"] = str(snap.unrealized_pnl)
        return view

    @app.post("/accounts/{sub_id}/allocate")
    def allocate(sub_id: str, body: AllocateBody):
        try:
            ledger.allocate(sub_id, Decimal(body.amount))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))
        return _acct_view(ledger.get_sub_account(sub_id))

    @app.get("/accounts/{sub_id}/positions")
    def positions(sub_id: str):
        return [
            {
                "symbol": p.symbol,
                "qty": str(p.qty),
                "avg_cost": str(p.avg_cost),
                "last_price": str(p.last_price),
                "unrealized_pnl": str(p.unrealized_pnl),
            }
            for p in ledger.list_positions(sub_id)
        ]

    def _dec_or_none(v: Optional[str]) -> Optional[Decimal]:
        return None if v is None else Decimal(v)

    @app.post("/orders")
    def place_order(body: OrderBody):
        req = OrderRequest(
            sub_account_id=body.sub_account_id,
            symbol=body.symbol.upper(),
            side=OrderSide(body.side),
            qty=Decimal(body.qty),
            notional=_dec_or_none(body.notional),
            order_type=OrderType(body.order_type),
            limit_price=_dec_or_none(body.limit_price),
            stop_price=_dec_or_none(body.stop_price),
            trail_percent=_dec_or_none(body.trail_percent),
            trail_price=_dec_or_none(body.trail_price),
            time_in_force=TimeInForce(body.time_in_force),
            extended_hours=body.extended_hours,
            order_class=OrderClass(body.order_class),
            take_profit=(
                None if body.take_profit is None
                else TakeProfitSpec(limit_price=Decimal(body.take_profit.limit_price))
            ),
            stop_loss=(
                None if body.stop_loss is None
                else StopLossSpec(
                    stop_price=Decimal(body.stop_loss.stop_price),
                    limit_price=_dec_or_none(body.stop_loss.limit_price),
                )
            ),
            client_tag=body.client_tag,
        )
        try:
            order = router.place_order(req)
        except OrderRejected as exc:
            raise HTTPException(422, str(exc))
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return _order_view(order)

    @app.patch("/orders/{order_id}")
    def replace_order(order_id: str, body: ReplaceBody):
        try:
            return _order_view(router.replace_order(
                order_id,
                qty=_dec_or_none(body.qty),
                limit_price=_dec_or_none(body.limit_price),
                stop_price=_dec_or_none(body.stop_price),
            ))
        except OrderRejected as exc:
            raise HTTPException(422, str(exc))
        except KeyError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/orders/{order_id}/legs")
    def order_legs(order_id: str):
        return [_order_view(leg) for leg in ledger.list_legs(order_id)]

    @app.post("/reconcile/acknowledge")
    def acknowledge_foreign(note: str = ""):
        report = reconciler.run()
        acked = reconciler.acknowledge_foreign_orders(report, note=note)
        return {"acknowledged": acked}

    @app.get("/orders")
    def list_orders(sub_account_id: Optional[str] = None, open_only: bool = False):
        return [_order_view(o) for o in ledger.list_orders(sub_account_id, open_only)]

    @app.delete("/orders/{order_id}")
    def cancel(order_id: str):
        try:
            return _order_view(router.cancel_order(order_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/reconcile")
    def reconcile_now():
        report = reconciler.run()
        return {
            "ok": report.ok,
            "at": report.at,
            "expected_cash": str(report.expected_cash),
            "broker_cash": str(report.broker_cash),
            "drifts": [vars(d) for d in report.drifts],
        }

    @app.get("/reconcile/latest")
    def reconcile_latest():
        latest = ledger.latest_reconciliation()
        if latest is None:
            raise HTTPException(404, "no reconciliation has run yet")
        return latest

    @app.post("/halt")
    def halt():
        router.halt()
        return {"halted": True}

    @app.post("/resume")
    def resume():
        router.resume()
        return {"halted": False}

    def _acct_view(a: SubAccount) -> dict:
        return {
            "id": a.id,
            "name": a.name,
            "cash": str(a.cash),
            "reserved_cash": str(a.reserved_cash),
            "margin_multiplier": str(a.margin_multiplier),
            "realized_pnl": str(a.realized_pnl),
            "realized_pnl_today": str(a.realized_pnl_today),
            "active": a.active,
        }

    def _order_view(o) -> dict:
        return {
            "id": o.id,
            "sub_account_id": o.sub_account_id,
            "symbol": o.symbol,
            "side": o.side.value,
            "qty": str(o.qty),
            "notional": None if o.notional is None else str(o.notional),
            "type": o.order_type.value,
            "order_class": o.order_class.value,
            "limit_price": None if o.limit_price is None else str(o.limit_price),
            "stop_price": None if o.stop_price is None else str(o.stop_price),
            "parent_order_id": o.parent_order_id,
            "leg_role": o.leg_role,
            "status": o.status.value,
            "filled_qty": str(o.filled_qty),
            "filled_avg_price": str(o.filled_avg_price),
            "reject_reason": o.reject_reason,
        }

    return app
