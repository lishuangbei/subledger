"""Alpaca adapter built on alpaca-py (imported lazily so the core package
works without it — e.g. in tests with MockBroker).

    pip install alpaca-py
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Dict, List, Optional

from ..models import (
    LEG_STOP_LOSS,
    LEG_TAKE_PROFIT,
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

_TRANSIENT_MARKERS = ("429", "500", "502", "503", "504", "timed out", "timeout",
                      "Connection", "connection", "Temporary")

REQUEST_TIMEOUT_S = 15


def _install_timeout(client, seconds: float = REQUEST_TIMEOUT_S) -> None:
    """alpaca-py issues REST calls with NO timeout — a stalled broker
    connection blocks the caller forever (observed during the 2026-08-13
    Alpaca degradation: SSL_read hung indefinitely). Inject a default
    timeout into the underlying requests.Session; the websocket stream is a
    separate client and is unaffected."""
    import functools

    session = getattr(client, "_session", None)
    if session is None or getattr(session.request, "__wrapped_timeout__", False):
        return
    original = session.request

    @functools.wraps(original)
    def with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return original(*args, **kwargs)

    with_timeout.__wrapped_timeout__ = True
    session.request = with_timeout

# Alpaca order statuses -> our normalized statuses.
_STATUS_MAP = {
    "new": "open",
    "accepted": "open",
    "pending_new": "open",
    "accepted_for_bidding": "open",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "pending_cancel": "open",
    "pending_replace": "open",
    "done_for_day": "canceled",
    "expired": "expired",
    "replaced": "canceled",
    "rejected": "rejected",
    "stopped": "open",
    "suspended": "open",
    "calculated": "open",
    "held": "open",
}


class AlpacaBroker(BrokerAdapter):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 retries: int = 3, retry_base_delay: float = 0.5):
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise BrokerError("alpaca-py is not installed: pip install alpaca-py") from exc
        self._client = TradingClient(api_key, secret_key, paper=paper)
        _install_timeout(self._client)
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._retries = retries
        self._retry_base_delay = retry_base_delay
        self._asset_cache: Dict[str, BrokerAsset] = {}
        self._sip_entitled: Optional[bool] = None  # unknown until first lookup

    def _call(self, label: str, fn, *args, **kwargs):
        """Call an alpaca-py client method with backoff on transient errors
        (rate limits, 5xx, connection drops). Non-transient errors raise
        immediately as BrokerError."""
        delay = self._retry_base_delay
        for attempt in range(self._retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                message = str(exc)
                transient = any(marker in message for marker in _TRANSIENT_MARKERS)
                if not transient or attempt == self._retries - 1:
                    raise BrokerError("alpaca {} failed: {}".format(label, exc)) from exc
                time.sleep(delay)
                delay *= 2

    # -- BrokerAdapter --------------------------------------------------

    def submit_order(self, req: OrderRequest, client_order_id: str) -> BrokerSubmitResult:
        from alpaca.trading.enums import OrderClass as AClass
        from alpaca.trading.enums import OrderSide as ASide
        from alpaca.trading.enums import OrderType as AType
        from alpaca.trading.enums import TimeInForce as ATif
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            OrderRequest as AOrderRequest,
            StopLimitOrderRequest,
            StopLossRequest,
            StopOrderRequest,
            TakeProfitRequest,
            TrailingStopOrderRequest,
        )

        side = ASide.BUY if req.side == OrderSide.BUY else ASide.SELL
        tif = {
            TimeInForce.DAY: ATif.DAY,
            TimeInForce.GTC: ATif.GTC,
            TimeInForce.IOC: ATif.IOC,
            TimeInForce.FOK: ATif.FOK,
            TimeInForce.OPG: ATif.OPG,
            TimeInForce.CLS: ATif.CLS,
        }[req.time_in_force]
        common = dict(
            symbol=req.symbol,
            side=side,
            time_in_force=tif,
            client_order_id=client_order_id,
            extended_hours=req.extended_hours or None,
        )
        if req.notional is not None and req.notional > 0:
            common["notional"] = float(req.notional)
        else:
            common["qty"] = float(req.qty)

        if req.order_class != OrderClass.SIMPLE:
            # OCO / bracket / OTO go through the generic request so the class
            # and exit legs are attached.
            kwargs = dict(common)
            kwargs["order_class"] = AClass(req.order_class.value)
            if req.take_profit is not None:
                kwargs["take_profit"] = TakeProfitRequest(
                    limit_price=float(req.take_profit.limit_price)
                )
            if req.stop_loss is not None:
                kwargs["stop_loss"] = StopLossRequest(
                    stop_price=float(req.stop_loss.stop_price),
                    limit_price=(
                        None if req.stop_loss.limit_price is None
                        else float(req.stop_loss.limit_price)
                    ),
                )
            a_type = {
                OrderType.MARKET: AType.MARKET,
                OrderType.LIMIT: AType.LIMIT,
                OrderType.STOP: AType.STOP,
                OrderType.STOP_LIMIT: AType.STOP_LIMIT,
            }.get(req.order_type, AType.MARKET)
            if req.limit_price is not None:
                kwargs["limit_price"] = float(req.limit_price)
            if req.stop_price is not None:
                kwargs["stop_price"] = float(req.stop_price)
            request = AOrderRequest(type=a_type, **kwargs)
        elif req.order_type == OrderType.LIMIT:
            request = LimitOrderRequest(limit_price=float(req.limit_price), **common)
        elif req.order_type == OrderType.STOP:
            request = StopOrderRequest(stop_price=float(req.stop_price), **common)
        elif req.order_type == OrderType.STOP_LIMIT:
            request = StopLimitOrderRequest(
                stop_price=float(req.stop_price),
                limit_price=float(req.limit_price),
                **common,
            )
        elif req.order_type == OrderType.TRAILING_STOP:
            request = TrailingStopOrderRequest(
                trail_percent=(
                    None if req.trail_percent is None else float(req.trail_percent)
                ),
                trail_price=(
                    None if req.trail_price is None else float(req.trail_price)
                ),
                **common,
            )
        else:
            request = MarketOrderRequest(**common)

        order = self._call("submit_order", self._client.submit_order, order_data=request)

        legs: List[BrokerLeg] = []
        for leg in getattr(order, "legs", None) or []:
            leg_type = str(getattr(leg, "type", getattr(leg, "order_type", ""))).split(".")[-1].lower()
            role = LEG_STOP_LOSS if "stop" in leg_type else LEG_TAKE_PROFIT
            legs.append(
                BrokerLeg(
                    broker_order_id=str(leg.id),
                    role=role,
                    client_order_id=str(getattr(leg, "client_order_id", "") or ""),
                )
            )
        return BrokerSubmitResult(broker_order_id=str(order.id), legs=legs)

    def describe(self) -> dict:
        return {"broker": "alpaca", "mode": "paper" if self._paper else "LIVE"}

    def cancel_order(self, broker_order_id: str) -> None:
        self._call("cancel", self._client.cancel_order_by_id, broker_order_id)

    def replace_order(
        self,
        broker_order_id: str,
        qty: Optional[Decimal] = None,
        limit_price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> str:
        """Alpaca replace = atomic cancel + NEW order. Without an explicit
        client_order_id the replacement gets a broker-generated UUID and our
        attribution loses the name (the REGN incident) — always pass one."""
        from alpaca.trading.requests import ReplaceOrderRequest

        request = ReplaceOrderRequest(
            qty=None if qty is None else int(qty),
            limit_price=None if limit_price is None else float(limit_price),
            stop_price=None if stop_price is None else float(stop_price),
            client_order_id=client_order_id,
        )
        order = self._call("replace", self._client.replace_order_by_id,
                           broker_order_id, order_data=request)
        return str(order.id)

    def get_order(self, broker_order_id: str) -> BrokerOrderState:
        order = self._call("get_order", self._client.get_order_by_id, broker_order_id)
        return self._to_state(order)

    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrderState]:
        try:
            order = self._call("get_order_by_client_id",
                               self._client.get_order_by_client_id, client_order_id)
        except BrokerError as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise
        return None if order is None else self._to_state(order)

    def list_orders(self, open_only: bool = False) -> List[BrokerOrderState]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        status = QueryOrderStatus.OPEN if open_only else QueryOrderStatus.ALL
        out: List[BrokerOrderState] = []
        until = None
        for _page in range(5):  # up to 2500 orders; plenty for a personal account
            orders = self._call(
                "list_orders",
                self._client.get_orders,
                filter=GetOrdersRequest(status=status, limit=500, nested=True, until=until),
            )
            for o in orders:
                out.append(self._to_state(o))
                for leg in getattr(o, "legs", None) or []:
                    out.append(self._to_state(leg))
            if len(orders) < 500:
                break
            until = getattr(orders[-1], "submitted_at", None)
            if until is None:
                break
        return out

    @property
    def _data(self):
        """Lazy market-data client (same keys). Only touched by last_prices,
        so trading-only deployments never import the data stack."""
        if getattr(self, "_data_client", None) is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._data_client = StockHistoricalDataClient(self._api_key, self._secret_key)
            _install_timeout(self._data_client)
        return self._data_client

    def last_prices(self, symbols: List[str]) -> Dict[str, Decimal]:
        """Batch latest-trade marks for arbitrary symbols (one call per feed,
        progressive fill). Real-time consolidated SIP first — sizing decides
        buying power, and a 15-minute-stale print can misjudge it by the full
        move since then. Keys without the SIP subscription fall back to
        real-time-but-thin IEX, then delayed SIP, then held positions."""
        wanted = [s for s in symbols if s]
        if not wanted:
            return {}
        marks: Dict[str, Decimal] = {}
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockLatestTradeRequest

            for feed in (DataFeed.SIP, DataFeed.IEX, DataFeed.DELAYED_SIP):
                if feed is DataFeed.SIP and self._sip_entitled is False:
                    continue
                missing = [s for s in wanted if s not in marks]
                if not missing:
                    break
                try:
                    trades = self._data.get_stock_latest_trade(
                        StockLatestTradeRequest(symbol_or_symbols=missing, feed=feed)
                    )
                    if feed is DataFeed.SIP:
                        self._sip_entitled = True
                except Exception as exc:
                    if feed is DataFeed.SIP and "subscription" in str(exc).lower():
                        self._sip_entitled = False  # remember; outages stay retryable
                    continue
                for symbol in missing:
                    price = getattr(trades.get(symbol), "price", None)
                    if price and float(price) > 0:
                        marks[symbol] = Decimal(str(price))
        except Exception:  # data API unavailable: fall through to positions
            pass
        if len(marks) < len(wanted):
            for symbol, price in super().last_prices(wanted).items():
                marks.setdefault(symbol, price)
        return marks

    def get_clock(self) -> BrokerClock:
        clock = self._call("get_clock", self._client.get_clock)
        return BrokerClock(
            is_open=bool(clock.is_open),
            next_open=str(getattr(clock, "next_open", "") or ""),
            next_close=str(getattr(clock, "next_close", "") or ""),
        )

    def get_asset(self, symbol: str) -> Optional[BrokerAsset]:
        cached = self._asset_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            asset = self._call("get_asset", self._client.get_asset, symbol)
        except BrokerError:
            return None  # unknown symbol or API hiccup: skip the pre-check
        info = BrokerAsset(
            symbol=symbol,
            tradable=bool(getattr(asset, "tradable", True)),
            fractionable=bool(getattr(asset, "fractionable", False)),
            shortable=bool(getattr(asset, "shortable", False)),
            easy_to_borrow=bool(getattr(asset, "easy_to_borrow", False)),
        )
        self._asset_cache[symbol] = info
        return info

    def get_cash_activities(self, after: str = "") -> List[BrokerCashActivity]:
        """GET /v2/account/activities via signed raw request — alpaca-py's
        TradingClient has no wrapper for the trading-API activities endpoint."""
        import json as _json
        import urllib.parse
        import urllib.request

        raw = getattr(self._client, "_base_url", "")
        base = str(getattr(raw, "value", "") or raw or "")   # BaseURL enum -> str
        if not base.startswith("http"):
            base = ("https://paper-api.alpaca.markets" if self._paper
                    else "https://api.alpaca.markets")
        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }
        out: List[BrokerCashActivity] = []
        page_token = None
        for _page in range(10):
            params = {"direction": "asc", "page_size": "100"}
            if after:
                params["after"] = after
            if page_token:
                params["page_token"] = page_token
            url = "{}/v2/account/activities?{}".format(base, urllib.parse.urlencode(params))
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=15) as resp:
                    rows = _json.loads(resp.read().decode())
            except Exception as exc:
                raise BrokerError("alpaca activities failed: {}".format(exc)) from exc
            for row in rows:
                atype = str(row.get("activity_type", "")).upper()
                if atype in ("FILL", "FILLQ"):
                    continue  # trade fills are booked by the router, not here
                kind = "dividend" if atype.startswith("DIV") else (
                    "fee" if atype in ("FEE", "REG", "TAF", "PTC") else "other")
                amount = row.get("net_amount", row.get("price", "0")) or "0"
                out.append(BrokerCashActivity(
                    activity_id=str(row.get("id", "")),
                    kind=kind,
                    amount=Decimal(str(amount)),
                    symbol=str(row.get("symbol", "") or ""),
                    at=str(row.get("date", row.get("transaction_time", "")) or ""),
                ))
            if len(rows) < 100:
                break
            page_token = rows[-1].get("id")
        return out

    def get_account(self) -> BrokerAccountState:
        acct = self._call("get_account", self._client.get_account)
        raw_positions = self._call("get_all_positions", self._client.get_all_positions)
        positions = [
            BrokerPosition(
                symbol=p.symbol,
                qty=Decimal(str(p.qty)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                current_price=Decimal(str(p.current_price or p.avg_entry_price)),
            )
            for p in raw_positions
        ]
        return BrokerAccountState(
            cash=Decimal(str(acct.cash)),
            equity=Decimal(str(acct.equity)),
            buying_power=Decimal(str(acct.buying_power)),
            positions=positions,
            account_id=str(getattr(acct, "id", "") or ""),
        )

    @staticmethod
    def _to_state(order) -> BrokerOrderState:
        return BrokerOrderState(
            broker_order_id=str(order.id),
            client_order_id=order.client_order_id or "",
            status=_STATUS_MAP.get(str(order.status).split(".")[-1].lower(), "open"),
            filled_qty=Decimal(str(order.filled_qty or "0")),
            filled_avg_price=Decimal(str(order.filled_avg_price or "0")),
            symbol=order.symbol,
        )
