"""SQLite-backed ledger: sub-accounts, positions, orders, master allocation.

All monetary values are stored as TEXT and parsed as Decimal — never float.
A single connection with a process-wide lock is enough here: the router is
the only writer, and order flow for a personal account is low-frequency.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from decimal import Decimal
from typing import Dict, List, Optional

from .models import (
    ZERO,
    OrderClass,
    OrderRecord,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    SubAccount,
    TimeInForce,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS master (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    unallocated_cash TEXT NOT NULL DEFAULT '0',
    halted INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO master (id) VALUES (1);

CREATE TABLE IF NOT EXISTS sub_accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    cash TEXT NOT NULL DEFAULT '0',
    reserved_cash TEXT NOT NULL DEFAULT '0',
    margin_multiplier TEXT NOT NULL DEFAULT '1',
    max_order_notional TEXT,
    daily_loss_limit TEXT,
    symbol_whitelist TEXT,
    allow_short INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    realized_pnl_today TEXT NOT NULL DEFAULT '0'
);

CREATE TABLE IF NOT EXISTS positions (
    sub_account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty TEXT NOT NULL DEFAULT '0',
    avg_cost TEXT NOT NULL DEFAULT '0',
    reserved_qty TEXT NOT NULL DEFAULT '0',
    last_price TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY (sub_account_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    broker_order_id TEXT,
    sub_account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    order_type TEXT NOT NULL,
    limit_price TEXT,
    time_in_force TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_qty TEXT NOT NULL DEFAULT '0',
    filled_avg_price TEXT NOT NULL DEFAULT '0',
    reserved TEXT NOT NULL DEFAULT '0',
    reject_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_sub ON orders (sub_account_id);

CREATE TABLE IF NOT EXISTS reconciliations (
    at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acknowledged_foreign (
    broker_order_id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS processed_activities (
    activity_id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    amount TEXT NOT NULL,
    allocations_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    at TEXT NOT NULL,
    sub_account_id TEXT NOT NULL,
    cash TEXT NOT NULL,
    positions_value TEXT NOT NULL,
    equity TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_sub_at ON equity_snapshots (sub_account_id, at);
"""

# Columns added after the original schema; applied via ALTER TABLE so an
# existing ledger.db upgrades in place without data loss.
_ORDER_MIGRATIONS = [
    ("stop_price", "TEXT"),
    ("trail_percent", "TEXT"),
    ("trail_price", "TEXT"),
    ("notional", "TEXT"),
    ("extended_hours", "INTEGER NOT NULL DEFAULT 0"),
    ("order_class", "TEXT NOT NULL DEFAULT 'simple'"),
    ("parent_order_id", "TEXT"),
    ("leg_role", "TEXT NOT NULL DEFAULT ''"),
]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _dec(v: Optional[str]) -> Optional[Decimal]:
    return None if v is None else Decimal(v)


class LedgerLocked(RuntimeError):
    """Another process holds this ledger's write lock. One real account has
    exactly one booking writer; register as a strategy module in that process
    (or stop it) instead of opening the ledger directly."""


class Ledger:
    def __init__(self, path: str = ":memory:", exclusive_writer: bool = True):
        """exclusive_writer (default True for file-backed ledgers): acquire an
        OS-level advisory lock (flock) on <path>.lock for this process's
        lifetime. A second writer process gets LedgerLocked at open — the
        hard form of the single-writer rule (the discipline-only version lost
        a share to a concurrent test process on 2026-08-10). Pass False ONLY
        for read/light-op tooling (status queries, halt/ack/absorb) that never
        books fills. :memory: ledgers never lock."""
        self.path = path
        self._lock_fd = None
        if exclusive_writer and path != ":memory:":
            self._acquire_writer_lock(path)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # WAL + NORMAL sync: cuts per-commit fsync cost on the order hot path
        # (several small commits per order) while staying crash-safe.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass  # :memory: or read-only media
        with self._lock:
            self._conn.executescript(_SCHEMA)
            existing = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(orders)").fetchall()
            }
            for column, decl in _ORDER_MIGRATIONS:
                if column not in existing:
                    self._conn.execute(
                        "ALTER TABLE orders ADD COLUMN {} {}".format(column, decl)
                    )
            self._conn.commit()

    def backup(self, path: str) -> None:
        """Consistent online snapshot of the ledger (SQLite backup API)."""
        with self._lock:
            dest = sqlite3.connect(path)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()

    def _acquire_writer_lock(self, path: str) -> None:
        try:
            import fcntl
        except ImportError:      # non-POSIX platform: fall back to discipline
            return
        import os

        lock_path = path + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                with open(lock_path) as fh:
                    holder = fh.read().strip()
            except OSError:
                pass
            os.close(fd)
            raise LedgerLocked(
                "ledger {} is write-locked by another process{} — one account "
                "has ONE booking writer. Register as a strategy module inside "
                "that process, use its CLI/REST surface, or stop it first.".format(
                    path, " (pid {})".format(holder) if holder else "")
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd       # held until process exit

    # -- master ---------------------------------------------------------

    def unallocated_cash(self) -> Decimal:
        row = self._conn.execute("SELECT unallocated_cash FROM master WHERE id=1").fetchone()
        return Decimal(row["unallocated_cash"])

    def set_unallocated_cash(self, value: Decimal) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE master SET unallocated_cash=? WHERE id=1", (str(value),)
            )
            self._conn.commit()

    def is_halted(self) -> bool:
        row = self._conn.execute("SELECT halted FROM master WHERE id=1").fetchone()
        return bool(row["halted"])

    def set_halted(self, halted: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE master SET halted=? WHERE id=1", (int(halted),))
            self._conn.commit()

    # -- sub-accounts ---------------------------------------------------

    def create_sub_account(self, acct: SubAccount) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sub_accounts
                   (id, name, cash, reserved_cash, margin_multiplier,
                    max_order_notional, daily_loss_limit, symbol_whitelist,
                    allow_short, active, realized_pnl, realized_pnl_today)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    acct.id,
                    acct.name,
                    str(acct.cash),
                    str(acct.reserved_cash),
                    str(acct.margin_multiplier),
                    None if acct.max_order_notional is None else str(acct.max_order_notional),
                    None if acct.daily_loss_limit is None else str(acct.daily_loss_limit),
                    None if acct.symbol_whitelist is None else json.dumps(acct.symbol_whitelist),
                    int(acct.allow_short),
                    int(acct.active),
                    str(acct.realized_pnl),
                    str(acct.realized_pnl_today),
                ),
            )
            self._conn.commit()

    def save_sub_account(self, acct: SubAccount) -> None:
        with self._lock:
            cur = self._conn.execute(
                """UPDATE sub_accounts SET
                   name=?, cash=?, reserved_cash=?, margin_multiplier=?,
                   max_order_notional=?, daily_loss_limit=?, symbol_whitelist=?,
                   allow_short=?, active=?, realized_pnl=?, realized_pnl_today=?
                   WHERE id=?""",
                (
                    acct.name,
                    str(acct.cash),
                    str(acct.reserved_cash),
                    str(acct.margin_multiplier),
                    None if acct.max_order_notional is None else str(acct.max_order_notional),
                    None if acct.daily_loss_limit is None else str(acct.daily_loss_limit),
                    None if acct.symbol_whitelist is None else json.dumps(acct.symbol_whitelist),
                    int(acct.allow_short),
                    int(acct.active),
                    str(acct.realized_pnl),
                    str(acct.realized_pnl_today),
                    acct.id,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError("unknown sub-account: {}".format(acct.id))
            self._conn.commit()

    def get_sub_account(self, sub_id: str) -> SubAccount:
        row = self._conn.execute(
            "SELECT * FROM sub_accounts WHERE id=?", (sub_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown sub-account: {}".format(sub_id))
        return self._row_to_sub(row)

    def list_sub_accounts(self) -> List[SubAccount]:
        rows = self._conn.execute("SELECT * FROM sub_accounts ORDER BY id").fetchall()
        return [self._row_to_sub(r) for r in rows]

    @staticmethod
    def _row_to_sub(row: sqlite3.Row) -> SubAccount:
        wl = row["symbol_whitelist"]
        return SubAccount(
            id=row["id"],
            name=row["name"],
            cash=Decimal(row["cash"]),
            reserved_cash=Decimal(row["reserved_cash"]),
            margin_multiplier=Decimal(row["margin_multiplier"]),
            max_order_notional=_dec(row["max_order_notional"]),
            daily_loss_limit=_dec(row["daily_loss_limit"]),
            symbol_whitelist=None if wl is None else json.loads(wl),
            allow_short=bool(row["allow_short"]),
            active=bool(row["active"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            realized_pnl_today=Decimal(row["realized_pnl_today"]),
        )

    def update_sub_account_settings(self, sub_id: str, **fields) -> SubAccount:
        """Update ONLY the settings columns (name, margin_multiplier,
        max_order_notional, daily_loss_limit, symbol_whitelist, allow_short,
        active). Never touches cash/reserved/PnL, so it is safe to run while
        the router is booking fills."""
        allowed = {
            "name": lambda v: str(v),
            "margin_multiplier": lambda v: str(Decimal(str(v))),
            "max_order_notional": lambda v: None if v is None else str(Decimal(str(v))),
            "daily_loss_limit": lambda v: None if v is None else str(Decimal(str(v))),
            "symbol_whitelist": lambda v: None if v is None else json.dumps(list(v)),
            "allow_short": lambda v: int(bool(v)),
            "active": lambda v: int(bool(v)),
        }
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("not a settings field: {}".format(key))
            sets.append("{}=?".format(key))
            params.append(allowed[key](value))
        if not sets:
            return self.get_sub_account(sub_id)
        params.append(sub_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sub_accounts SET {} WHERE id=?".format(", ".join(sets)), params
            )
            if cur.rowcount == 0:
                raise KeyError("unknown sub-account: {}".format(sub_id))
            self._conn.commit()
        return self.get_sub_account(sub_id)

    def delete_sub_account(self, sub_id: str) -> Decimal:
        """Delete a sub-account. Requires it to be flat: no positions, no
        reserved cash/shares, no open orders. Its remaining cash returns to
        the unallocated pool; order history is kept for audit. Returns the
        cash that was returned."""
        with self._lock:
            acct = self.get_sub_account(sub_id)
            positions = [p for p in self.list_positions(sub_id) if p.qty != ZERO or p.reserved_qty != ZERO]
            if positions:
                raise ValueError("sub-account {} still holds positions: {}".format(
                    sub_id, [p.symbol for p in positions]))
            open_orders = self.list_orders(sub_id, open_only=True)
            if open_orders:
                raise ValueError("sub-account {} has {} open order(s)".format(
                    sub_id, len(open_orders)))
            if acct.reserved_cash != ZERO:
                raise ValueError("sub-account {} has reserved cash {}".format(
                    sub_id, acct.reserved_cash))
            returned = acct.cash
            self.set_unallocated_cash(self.unallocated_cash() + returned)
            self._conn.execute("DELETE FROM positions WHERE sub_account_id=?", (sub_id,))
            self._conn.execute("DELETE FROM sub_accounts WHERE id=?", (sub_id,))
            self._conn.commit()
            return returned

    # -- allocation moves ----------------------------------------------

    def allocate(self, sub_id: str, amount: Decimal) -> None:
        """Move cash from the unallocated pool into a sub-account (or back,
        with a negative amount). Total across the ledger is invariant."""
        with self._lock:
            pool = self.unallocated_cash()
            acct = self.get_sub_account(sub_id)
            if amount > pool:
                raise ValueError(
                    "cannot allocate {}: only {} unallocated".format(amount, pool)
                )
            if amount < ZERO and acct.cash - acct.reserved_cash < -amount:
                raise ValueError("cannot deallocate more than free cash")
            acct.cash += amount
            self.set_unallocated_cash(pool - amount)
            self.save_sub_account(acct)

    # -- positions ------------------------------------------------------

    def get_position(self, sub_id: str, symbol: str) -> Position:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE sub_account_id=? AND symbol=?",
            (sub_id, symbol),
        ).fetchone()
        if row is None:
            return Position(sub_account_id=sub_id, symbol=symbol)
        return Position(
            sub_account_id=row["sub_account_id"],
            symbol=row["symbol"],
            qty=Decimal(row["qty"]),
            avg_cost=Decimal(row["avg_cost"]),
            reserved_qty=Decimal(row["reserved_qty"]),
            last_price=Decimal(row["last_price"]),
        )

    def save_position(self, pos: Position) -> None:
        with self._lock:
            if pos.qty == ZERO and pos.reserved_qty == ZERO:
                self._conn.execute(
                    "DELETE FROM positions WHERE sub_account_id=? AND symbol=?",
                    (pos.sub_account_id, pos.symbol),
                )
            else:
                self._conn.execute(
                    """INSERT INTO positions
                       (sub_account_id, symbol, qty, avg_cost, reserved_qty, last_price)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(sub_account_id, symbol) DO UPDATE SET
                       qty=excluded.qty, avg_cost=excluded.avg_cost,
                       reserved_qty=excluded.reserved_qty, last_price=excluded.last_price""",
                    (
                        pos.sub_account_id,
                        pos.symbol,
                        str(pos.qty),
                        str(pos.avg_cost),
                        str(pos.reserved_qty),
                        str(pos.last_price),
                    ),
                )
            self._conn.commit()

    def list_positions(self, sub_id: Optional[str] = None) -> List[Position]:
        if sub_id is None:
            rows = self._conn.execute("SELECT * FROM positions").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE sub_account_id=?", (sub_id,)
            ).fetchall()
        return [
            Position(
                sub_account_id=r["sub_account_id"],
                symbol=r["symbol"],
                qty=Decimal(r["qty"]),
                avg_cost=Decimal(r["avg_cost"]),
                reserved_qty=Decimal(r["reserved_qty"]),
                last_price=Decimal(r["last_price"]),
            )
            for r in rows
        ]

    def aggregate_positions(self) -> Dict[str, Decimal]:
        """Net qty per symbol across all sub-accounts — what the real
        brokerage account should be holding on our behalf."""
        out: Dict[str, Decimal] = {}
        for pos in self.list_positions():
            out[pos.symbol] = out.get(pos.symbol, ZERO) + pos.qty
        return {s: q for s, q in out.items() if q != ZERO}

    # -- orders ---------------------------------------------------------

    def save_order(self, order: OrderRecord) -> None:
        with self._lock:
            now = _now()
            if not order.created_at:
                order.created_at = now
            order.updated_at = now
            self._conn.execute(
                """INSERT INTO orders
                   (id, client_order_id, broker_order_id, sub_account_id, symbol,
                    side, qty, order_type, limit_price, time_in_force, status,
                    filled_qty, filled_avg_price, reserved, reject_reason,
                    created_at, updated_at, stop_price, trail_percent, trail_price,
                    notional, extended_hours, order_class, parent_order_id, leg_role)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                   broker_order_id=excluded.broker_order_id,
                   qty=excluded.qty,
                   limit_price=excluded.limit_price,
                   stop_price=excluded.stop_price,
                   status=excluded.status,
                   filled_qty=excluded.filled_qty,
                   filled_avg_price=excluded.filled_avg_price,
                   reserved=excluded.reserved,
                   reject_reason=excluded.reject_reason,
                   updated_at=excluded.updated_at""",
                (
                    order.id,
                    order.client_order_id,
                    order.broker_order_id,
                    order.sub_account_id,
                    order.symbol,
                    order.side.value,
                    str(order.qty),
                    order.order_type.value,
                    None if order.limit_price is None else str(order.limit_price),
                    order.time_in_force.value,
                    order.status.value,
                    str(order.filled_qty),
                    str(order.filled_avg_price),
                    str(order.reserved),
                    order.reject_reason,
                    order.created_at,
                    order.updated_at,
                    None if order.stop_price is None else str(order.stop_price),
                    None if order.trail_percent is None else str(order.trail_percent),
                    None if order.trail_price is None else str(order.trail_price),
                    None if order.notional is None else str(order.notional),
                    int(order.extended_hours),
                    order.order_class.value,
                    order.parent_order_id,
                    order.leg_role,
                ),
            )
            self._conn.commit()

    def get_order(self, order_id: str) -> OrderRecord:
        row = self._conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise KeyError("unknown order: {}".format(order_id))
        return self._row_to_order(row)

    def list_orders(
        self, sub_id: Optional[str] = None, open_only: bool = False
    ) -> List[OrderRecord]:
        q = "SELECT * FROM orders"
        clauses, params = [], []
        if sub_id is not None:
            clauses.append("sub_account_id=?")
            params.append(sub_id)
        if open_only:
            clauses.append("status IN ('pending','open','partially_filled')")
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at"
        return [self._row_to_order(r) for r in self._conn.execute(q, params).fetchall()]

    def known_client_order_ids(self) -> List[str]:
        rows = self._conn.execute("SELECT client_order_id FROM orders").fetchall()
        return [r["client_order_id"] for r in rows]

    def known_broker_order_ids(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL"
        ).fetchall()
        return [r["broker_order_id"] for r in rows]

    def get_order_by_client_id(self, client_order_id: str) -> Optional[OrderRecord]:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        return None if row is None else self._row_to_order(row)

    def list_legs(self, parent_order_id: str) -> List[OrderRecord]:
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE parent_order_id=? ORDER BY created_at, id",
            (parent_order_id,),
        ).fetchall()
        return [self._row_to_order(r) for r in rows]

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=row["id"],
            client_order_id=row["client_order_id"],
            broker_order_id=row["broker_order_id"],
            sub_account_id=row["sub_account_id"],
            symbol=row["symbol"],
            side=OrderSide(row["side"]),
            qty=Decimal(row["qty"]),
            order_type=OrderType(row["order_type"]),
            limit_price=_dec(row["limit_price"]),
            time_in_force=TimeInForce(row["time_in_force"]),
            status=OrderStatus(row["status"]),
            stop_price=_dec(row["stop_price"]),
            trail_percent=_dec(row["trail_percent"]),
            trail_price=_dec(row["trail_price"]),
            notional=_dec(row["notional"]),
            extended_hours=bool(row["extended_hours"]),
            order_class=OrderClass(row["order_class"]),
            parent_order_id=row["parent_order_id"],
            leg_role=row["leg_role"],
            filled_qty=Decimal(row["filled_qty"]),
            filled_avg_price=Decimal(row["filled_avg_price"]),
            reserved=Decimal(row["reserved"]),
            reject_reason=row["reject_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -- acknowledged foreign orders ------------------------------------

    def acknowledge_foreign(self, broker_order_id: str, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO acknowledged_foreign (broker_order_id, at, note)
                   VALUES (?,?,?)
                   ON CONFLICT(broker_order_id) DO UPDATE SET note=excluded.note""",
                (broker_order_id, _now(), note),
            )
            self._conn.commit()

    def acknowledged_foreign_ids(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT broker_order_id FROM acknowledged_foreign"
        ).fetchall()
        return [r["broker_order_id"] for r in rows]

    # -- processed cash activities (dividends / fees) -------------------

    def processed_activity_ids(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT activity_id FROM processed_activities"
        ).fetchall()
        return [r["activity_id"] for r in rows]

    def record_processed_activity(
        self, activity_id: str, kind: str, symbol: str, amount: Decimal, allocations_json: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO processed_activities
                   (activity_id, at, kind, symbol, amount, allocations_json)
                   VALUES (?,?,?,?,?,?)""",
                (activity_id, _now(), kind, symbol, str(amount), allocations_json),
            )
            self._conn.commit()

    # -- equity history -------------------------------------------------

    def record_equity_snapshot(self, at: str, sub_account_id: str, cash: Decimal,
                               positions_value: Decimal, realized_pnl: Decimal,
                               unrealized_pnl: Decimal) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO equity_snapshots
                   (at, sub_account_id, cash, positions_value, equity,
                    realized_pnl, unrealized_pnl) VALUES (?,?,?,?,?,?,?)""",
                (at, sub_account_id, str(cash), str(positions_value),
                 str(cash + positions_value), str(realized_pnl), str(unrealized_pnl)),
            )
            self._conn.commit()

    def equity_history(self, sub_account_id: Optional[str] = None,
                       since: Optional[str] = None, limit: int = 500) -> List[dict]:
        q = "SELECT * FROM equity_snapshots"
        clauses, params = [], []
        if sub_account_id is not None:
            clauses.append("sub_account_id=?")
            params.append(sub_account_id)
        if since is not None:
            clauses.append("at >= ?")
            params.append(since)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # -- reconciliation history ----------------------------------------

    def save_reconciliation(self, at: str, ok: bool, report_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO reconciliations (at, ok, report_json) VALUES (?,?,?)",
                (at, int(ok), report_json),
            )
            self._conn.commit()

    def latest_reconciliation(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM reconciliations ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {"at": row["at"], "ok": bool(row["ok"]), "report": json.loads(row["report_json"])}
