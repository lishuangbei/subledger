"""Generic booking daemon: the single ledger writer for one subledger instance.

    set -a; source .env; set +a
    python -m subledger.daemon

Builds the stack from the environment (SUBLEDGER_BROKER=alpaca|ib|mock, see
api._build_stack), then:

  - runs Router.repair() once (crash recovery)
  - hosts the REST surface on 127.0.0.1:$SUBLEDGER_REST_PORT (default 8000;
    loopback only, no auth) with the vendor background loop: fill sync every
    SUBLEDGER_SYNC_INTERVAL s, dividend/fee attribution + reconcile + cash-call
    enforcement every SUBLEDGER_RECONCILE_INTERVAL s
  - attaches the broker's push stream when one exists (IB order events,
    Alpaca trade_updates) so fills book at event latency; polling stays the
    safety net
  - refreshes marks + records the equity history every
    SUBLEDGER_EQUITY_INTERVAL s (default 900) while the market is open
  - if SUBLEDGER_BACKUP_DIR is set: one ledger snapshot per calendar day,
    keeping the newest SUBLEDGER_BACKUP_KEEP (default 14)
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import time

logger = logging.getLogger("subledger.daemon")


def _start_stream(stack) -> None:
    ledger, broker, router, reconciler = stack
    if hasattr(broker, "add_order_listener"):
        from .stream import IBOrderStream

        IBOrderStream(router, broker).start()
        return
    if os.environ.get("SUBLEDGER_BROKER") == "alpaca" and os.environ.get("ALPACA_API_KEY"):
        from .stream import TradeUpdateStream

        TradeUpdateStream(
            router,
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=os.environ.get("ALPACA_PAPER", "true").lower() != "false",
        ).start()


def _equity_loop(stack, interval: float) -> None:
    ledger, broker, router, reconciler = stack
    while True:
        try:
            if broker.get_clock().is_open:
                router.refresh_marks()
                router.snapshot_equity_history()
        except Exception:
            logger.exception("equity loop error")
        time.sleep(interval)


def _backup_loop(ledger, backup_dir: str, keep: int) -> None:
    os.makedirs(backup_dir, exist_ok=True)
    while True:
        try:
            stamp = _dt.date.today().isoformat()
            target = os.path.join(backup_dir, "ledger_{}.db".format(stamp))
            if not os.path.exists(target):
                ledger.backup(target)
                logger.info("ledger backup written: %s", target)
                snapshots = sorted(
                    f for f in os.listdir(backup_dir)
                    if f.startswith("ledger_") and f.endswith(".db"))
                for old in snapshots[:-keep] if keep > 0 else []:
                    os.remove(os.path.join(backup_dir, old))
        except Exception:
            logger.exception("backup loop error")
        time.sleep(3600)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    from .api import _build_stack, create_app

    stack = _build_stack()
    ledger, broker, router, reconciler = stack
    logger.info("daemon starting: %s ledger=%s", broker.describe(), getattr(ledger, "path", "?"))

    report = router.repair()
    if report["adopted"] or report["released"]:
        logger.warning("crash repair: %s", report)

    _start_stream(stack)
    threading.Thread(
        target=_equity_loop,
        args=(stack, float(os.environ.get("SUBLEDGER_EQUITY_INTERVAL", "900"))),
        daemon=True, name="subledger-equity").start()
    backup_dir = os.environ.get("SUBLEDGER_BACKUP_DIR")
    if backup_dir:
        threading.Thread(
            target=_backup_loop,
            args=(ledger, backup_dir, int(os.environ.get("SUBLEDGER_BACKUP_KEEP", "14"))),
            daemon=True, name="subledger-backup").start()

    app = create_app(stack=stack, background_loops=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SUBLEDGER_REST_PORT", "8000")),
                log_level="info")


if __name__ == "__main__":
    main()
