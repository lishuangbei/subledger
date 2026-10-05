"""Executor heartbeats: every live sub-account must have an active executor.

An executor is whatever places orders for a sub-account: an out-of-process
program on the REST surface (strategy-executer, hermes live4731) or a
strategy module hosted inside the daemon (trial). Each one reports its OWN
heartbeat to its instance (POST /executors/heartbeat, or
Ledger.record_executor_heartbeat in-process), and every heartbeat promises
the next one by `expect_next_by`. One rule, here, judges them all, so
`subld status`, the REST view and the watchdog always agree:

  ok       the heartbeat is on time and the executor reports status ok
  stale    the promised next heartbeat is overdue: process gone or hung,
           or its scheduler stopped running it
  error    on time, but the executor reports that its last run failed

Executors must run as independent processes, never inside the subld
daemon (owner rule, 2026-10-05): a heartbeat of kind `in-process` is a
problem in itself, whatever its timing.

A sub-account is `missing` when no executor reports for it, unless it is
explicitly waived (`subld executors waive <sub> <reason>`), and `problem`
when any of its executors is stale or in error. Executors reporting for a
sub-account that does not exist, or is inactive, are orphans.

This module is pure (no database, no network): the watchdog feeds it rows
it reads with SQLite mode=ro.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, Iterable, List, Optional

GRACE_S = 60                       # slack on top of every promised heartbeat
DEFAULT_INTERVAL_S = 300
MIN_INTERVAL_S = 30
MAX_INTERVAL_S = 8 * 24 * 3600     # a weekly scheduled job, plus slack

OK, STALE, ERROR, RETIRED = "ok", "stale", "error", "retired"
MISSING, WAIVED, PROBLEM = "missing", "waived", "problem"
EXECUTOR_STATUSES = ("ok", "error")
IN_PROCESS = "in-process"          # hosted inside a daemon: not allowed


def parse_ts(value) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def clamp_interval(interval_s) -> int:
    try:
        value = int(interval_s)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, value))


def executor_state(row: dict, now: Optional[_dt.datetime] = None) -> str:
    now = now or utc_now()
    if row.get("retired_at"):
        return RETIRED
    due = parse_ts(row.get("expect_next_by"))
    if due is None or now > due + _dt.timedelta(seconds=GRACE_S):
        return STALE
    if (row.get("status") or "ok") != "ok":
        return ERROR
    return OK


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "{}s".format(seconds)
    if seconds < 90 * 60:
        return "{}m".format(round(seconds / 60))
    if seconds < 36 * 3600:
        return "{:.1f}h".format(seconds / 3600).replace(".0h", "h")
    return "{:.1f}d".format(seconds / 86400).replace(".0d", "d")


def _describe(row: dict, now: _dt.datetime) -> dict:
    state = executor_state(row, now)
    last = parse_ts(row.get("last_heartbeat_at"))
    age = (now - last).total_seconds() if last else None
    out = dict(row)
    out.update({"state": state, "age_s": None if age is None else int(age)})
    name = row.get("name") or row.get("executor_id")
    if state == STALE:
        out["problem"] = "{} 心跳超时：最后一次 {} 前（承诺每 {} 一次）".format(
            name, age_text(age or 0), age_text(clamp_interval(row.get("interval_s"))))
    elif state == ERROR:
        out["problem"] = "{} 报告出错：{}".format(name, row.get("message") or "(无说明)")
    if state in (OK, ERROR) and (row.get("kind") or "") == IN_PROCESS:
        text = "{} 跑在 subld daemon 进程内：执行器必须是独立进程".format(name)
        out["problem"] = "{}；{}".format(out["problem"], text) if out.get("problem") else text
    return out


def evaluate(sub_accounts: Iterable[dict], executors: Iterable[dict],
             waivers: Optional[Dict[str, str]] = None,
             now: Optional[_dt.datetime] = None) -> dict:
    """sub_accounts: dicts with `id` and `active`; executors: executor rows;
    waivers: sub_account_id -> reason. Returns

      {"accounts": {sub_id: {"state", "executors": [...], "waiver", "summary",
                             "problems": [str]}},
       "orphans": [executor rows], "problems": [(scope, text)]}

    Inactive sub-accounts are not checked (they are being closed)."""
    now = now or utc_now()
    waivers = dict(waivers or {})
    rows = [_describe(r, now) for r in executors]
    live = [r for r in rows if r["state"] != RETIRED]
    subs = [s for s in sub_accounts if s.get("active", True)]
    known = {str(s["id"]) for s in subs}

    accounts: Dict[str, dict] = {}
    problems: List[tuple] = []
    for sub in subs:
        sub_id = str(sub["id"])
        mine = [r for r in live if str(r.get("sub_account_id")) == sub_id]
        entry = {"executors": mine, "waiver": waivers.get(sub_id), "problems": []}
        if not mine:
            if entry["waiver"]:
                entry["state"] = WAIVED
                entry["summary"] = "– 无人值守：{}".format(entry["waiver"])
            else:
                entry["state"] = MISSING
                entry["summary"] = "✗ 无执行器"
                entry["problems"].append("子账户 {} 没有任何执行器在报心跳".format(sub_id))
        else:
            entry["problems"] = [r["problem"] for r in mine if r.get("problem")]
            entry["state"] = PROBLEM if entry["problems"] else OK
            entry["summary"] = _summary(mine)
        problems += [(sub_id, text) for text in entry["problems"]]
        accounts[sub_id] = entry

    orphans = [r for r in live if str(r.get("sub_account_id")) not in known]
    for r in orphans:
        problems.append((r["executor_id"], "执行器 {} 在为不存在或已停用的子账户 {} 报心跳".format(
            r.get("name") or r["executor_id"], r.get("sub_account_id"))))
    return {"accounts": accounts, "orphans": orphans, "problems": problems,
            "at": now.isoformat()}


def _summary(rows: List[dict]) -> str:
    """One cell per sub-account: executors grouped by display name, worst
    state first."""
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("name") or r["executor_id"], []).append(r)
    parts = []
    for name, members in sorted(groups.items()):
        bad = [m for m in members if m.get("problem")]
        count = " ×{}".format(len(members)) if len(members) > 1 else ""
        if bad:
            if any(m["state"] == STALE for m in bad):
                detail = "心跳超时"
            elif any(m["state"] == ERROR for m in bad):
                detail = "出错"
            else:
                detail = "在 daemon 内"
            parts.append("✗ {}{} {}".format(name, count, detail))
        else:
            freshest = min((m["age_s"] for m in members if m["age_s"] is not None), default=0)
            parts.append("✓ {}{} · {}".format(name, count, age_text(freshest)))
    return "；".join(parts)
