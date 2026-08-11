"""Trade-update streaming: push-based fill booking via Alpaca's websocket.

    stream = TradeUpdateStream(router, api_key=..., secret_key=..., paper=True)
    stream.add_listener(lambda event, state: ...)   # optional strategy hook
    stream.start()                                   # background thread

Every trade_updates event carries the order's cumulative fill state, so the
router books it directly (Router.apply_stream_state) with no extra API call —
ledger latency is the websocket latency (typically well under half a second).

Polling `router.sync()` stays valid as a safety net: booking is delta-based,
so double delivery (stream + poll) has no effect.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Callable, List, Optional

from .broker.base import BrokerOrderState
from .router import Router

logger = logging.getLogger("subledger.stream")

# Same normalization the Alpaca adapter uses for REST payloads.
from .broker.alpaca import _STATUS_MAP  # noqa: E402


def _event_to_state(order) -> BrokerOrderState:
    return BrokerOrderState(
        broker_order_id=str(order.id),
        client_order_id=str(getattr(order, "client_order_id", "") or ""),
        status=_STATUS_MAP.get(str(order.status).split(".")[-1].lower(), "open"),
        filled_qty=Decimal(str(order.filled_qty or "0")),
        filled_avg_price=Decimal(str(order.filled_avg_price or "0")),
        symbol=str(order.symbol),
    )


class TradeUpdateStream:
    def __init__(self, router: Router, api_key: str, secret_key: str, paper: bool = True):
        self.router = router
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._listeners: List[Callable] = []
        self._stream = None
        self._thread: Optional[threading.Thread] = None

    def add_listener(self, callback: Callable) -> None:
        """callback(event: str, state: BrokerOrderState) — called after the
        ledger is updated, on the stream thread. Keep it fast; hand heavy work
        to your own queue/loop."""
        self._listeners.append(callback)

    async def _handle(self, data) -> None:
        try:
            event = str(getattr(data, "event", ""))
            state = _event_to_state(data.order)
            booked = self.router.apply_stream_state(state)
            logger.info("trade_update %s %s %s filled=%s booked=%s",
                        event, state.symbol, state.status, state.filled_qty, booked)
            for callback in self._listeners:
                try:
                    callback(event, state)
                except Exception:
                    logger.exception("stream listener failed")
        except Exception:
            logger.exception("failed to handle trade update")

    def start(self) -> None:
        """Run the websocket in a daemon thread; reconnects on drops."""
        from alpaca.trading.stream import TradingStream

        def _run():
            import time
            while True:
                try:
                    self._stream = TradingStream(
                        self._api_key, self._secret_key, paper=self._paper
                    )
                    self._stream.subscribe_trade_updates(self._handle)
                    logger.info("trade-update stream connecting (paper=%s)", self._paper)
                    self._stream.run()          # blocks until closed/error
                except Exception:
                    logger.exception("trade-update stream dropped; reconnecting in 5s")
                time.sleep(5)

        self._thread = threading.Thread(target=_run, name="subledger-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
