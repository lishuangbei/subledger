"""Executor heartbeats: every active sub-account must have an active
executor; `subld status` marks problems red, the watchdog alerts on them."""

import datetime as dt
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal as D
from unittest import mock

from subledger import Ledger, Reconciler, Router, SubAccount
from subledger import executors as ex
from subledger.broker.mock import MockBroker
from subledger.cli import run

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 5, 21, 0, tzinfo=UTC)

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_REST = True
except ImportError:
    HAVE_REST = False


def row(executor_id="e1", sub="a1", *, beat=T0, interval=300, status="ok", message="",
        name="", retired=None, expect=None):
    return {"executor_id": executor_id, "sub_account_id": sub, "name": name, "kind": "external",
            "pid": 1, "status": status, "message": message, "interval_s": interval,
            "expect_next_by": (expect or beat + dt.timedelta(seconds=interval)).isoformat(),
            "first_seen_at": beat.isoformat(), "last_heartbeat_at": beat.isoformat(),
            "retired_at": retired}


class HealthRuleTests(unittest.TestCase):
    def test_executor_states(self):
        r = row()
        self.assertEqual(ex.executor_state(r, T0 + dt.timedelta(seconds=200)), ex.OK)
        # the promised next heartbeat is overdue past the grace
        self.assertEqual(ex.executor_state(r, T0 + dt.timedelta(seconds=300 + ex.GRACE_S - 1)), ex.OK)
        self.assertEqual(ex.executor_state(r, T0 + dt.timedelta(seconds=300 + ex.GRACE_S + 1)), ex.STALE)
        self.assertEqual(ex.executor_state(row(status="error"), T0), ex.ERROR)
        self.assertEqual(ex.executor_state(row(retired=T0.isoformat()), T0), ex.RETIRED)
        # staleness wins over a reported error: the report itself is old news
        self.assertEqual(ex.executor_state(row(status="error"), T0 + dt.timedelta(hours=1)), ex.STALE)

    def test_sub_account_states(self):
        subs = [{"id": "a1", "active": True}, {"id": "a2", "active": True},
                {"id": "a3", "active": True}, {"id": "a4", "active": True},
                {"id": "old", "active": False}]
        execs = [row("e1", "a1", name="exec one"), row("e2", "a2", status="error", message="boom"),
                 row("e9", "ghost"), row("e8", "old")]
        out = ex.evaluate(subs, execs, {"a4": "manual account"}, now=T0 + dt.timedelta(seconds=30))
        acc = out["accounts"]
        self.assertEqual(acc["a1"]["state"], ex.OK)
        self.assertIn("exec one", acc["a1"]["summary"])
        self.assertEqual(acc["a2"]["state"], ex.PROBLEM)
        self.assertIn("boom", acc["a2"]["problems"][0])
        self.assertEqual(acc["a3"]["state"], ex.MISSING)
        self.assertEqual(acc["a4"]["state"], ex.WAIVED)
        self.assertNotIn("old", acc)                       # inactive: not checked
        self.assertEqual(sorted(r["executor_id"] for r in out["orphans"]), ["e8", "e9"])
        scopes = sorted(scope for scope, _text in out["problems"])
        self.assertEqual(scopes, ["a2", "a3", "e8", "e9"])

    def test_one_stale_member_makes_the_group_a_problem(self):
        subs = [{"id": "a1", "active": True}]
        execs = [row("hermes/oco", "a1", name="hermes live4731", beat=T0),
                 row("hermes/open", "a1", name="hermes live4731", beat=T0 - dt.timedelta(days=2),
                     interval=86400)]
        out = ex.evaluate(subs, execs, now=T0 + dt.timedelta(seconds=10))
        entry = out["accounts"]["a1"]
        self.assertEqual(entry["state"], ex.PROBLEM)
        self.assertIn("×2", entry["summary"])
        self.assertIn("心跳超时", entry["summary"])

    def test_waiver_does_not_hide_a_broken_executor(self):
        subs = [{"id": "a1", "active": True}]
        out = ex.evaluate(subs, [row(status="error")], {"a1": "x"}, now=T0)
        self.assertEqual(out["accounts"]["a1"]["state"], ex.PROBLEM)


class LedgerHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.ledger.create_sub_account(SubAccount(id="a1"))

    def test_upsert_keeps_first_seen_and_unretires(self):
        first = self.ledger.record_executor_heartbeat("e1", "a1", name="n", at=T0.isoformat())
        self.assertTrue(self.ledger.retire_executor("e1"))
        self.assertFalse(self.ledger.retire_executor("e1"))
        later = T0 + dt.timedelta(minutes=5)
        second = self.ledger.record_executor_heartbeat("e1", "a1", name="n", pid=42,
                                                       at=later.isoformat())
        self.assertEqual(second["first_seen_at"], first["first_seen_at"])
        self.assertEqual(second["last_heartbeat_at"], later.isoformat())
        self.assertIsNone(second["retired_at"])
        self.assertEqual(second["pid"], 42)

    def test_interval_expect_and_status_normalisation(self):
        r = self.ledger.record_executor_heartbeat("e1", "a1", interval_s=1, at=T0.isoformat())
        self.assertEqual(r["interval_s"], ex.MIN_INTERVAL_S)
        due = T0 + dt.timedelta(hours=20)
        r = self.ledger.record_executor_heartbeat("e1", "a1", expect_next_by=due.isoformat(),
                                                  at=T0.isoformat())
        self.assertEqual(r["expect_next_by"], due.isoformat())
        r = self.ledger.record_executor_heartbeat("e1", "a1", status="weird", message="m",
                                                  at=T0.isoformat())
        self.assertEqual(r["status"], "error")
        self.assertIn("weird", r["message"])
        with self.assertRaises(ValueError):
            self.ledger.record_executor_heartbeat("", "a1")

    def test_waivers(self):
        self.ledger.set_executor_waiver("a1", "manual")
        self.assertEqual(self.ledger.executor_waivers(), {"a1": "manual"})
        self.assertEqual(self.ledger.executor_health()["accounts"]["a1"]["state"], ex.WAIVED)
        self.assertTrue(self.ledger.clear_executor_waiver("a1"))
        self.assertEqual(self.ledger.executor_health()["accounts"]["a1"]["state"], ex.MISSING)
        with self.assertRaises(KeyError):
            self.ledger.set_executor_waiver("nope", "x")
        with self.assertRaises(ValueError):
            self.ledger.set_executor_waiver("a1", "  ")

    def test_existing_ledger_file_gains_the_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.db")
            Ledger(path).create_sub_account(SubAccount(id="a1"))
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE executors")
            conn.execute("DROP TABLE executor_waivers")
            conn.commit()
            conn.close()
            reopened = Ledger(path, exclusive_writer=False)
            reopened.record_executor_heartbeat("e1", "a1")
            self.assertEqual(reopened.executor_health()["accounts"]["a1"]["state"], ex.OK)


def make_stack():
    broker = MockBroker(cash=D("100000"))
    ledger = Ledger(":memory:")
    router = Router(ledger, broker)
    router.adopt_broker_cash()
    return (ledger, broker, router, Reconciler(ledger, broker))


def invoke(stack, *argv, colour=False):
    out = io.StringIO()
    env = {"SUBLEDGER_COLOR": "1"} if colour else {}
    with mock.patch.dict(os.environ, env), redirect_stdout(out):
        code = run(stack, list(argv))
    return code, out.getvalue()


class CliExecutorTests(unittest.TestCase):
    def setUp(self):
        self.stack = make_stack()
        ledger = self.stack[0]
        ledger.create_sub_account(SubAccount(id="1001", name="alpha"))
        ledger.create_sub_account(SubAccount(id="1002", name="beta"))
        ledger.record_executor_heartbeat("com.example.exec-1001", "1001", name="exec 1001")

    def test_status_marks_each_sub_account_with_its_executor(self):
        code, out = invoke(self.stack, "--json", "status")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        cfg = {c["id"]: c for c in payload["accounts_config"]}
        self.assertEqual(cfg["1001"]["executor"]["state"], "ok")
        self.assertEqual(cfg["1001"]["executor"]["executors"][0]["executor_id"], "com.example.exec-1001")
        self.assertEqual(cfg["1002"]["executor"]["state"], "missing")
        self.assertEqual([p["scope"] for p in payload["executor_problems"]], ["1002"])

        code, out = invoke(self.stack, "status", colour=True)
        line_1001 = next(l for l in out.splitlines() if l.startswith("1001"))
        line_1002 = next(l for l in out.splitlines() if l.startswith("1002"))
        self.assertIn("exec 1001", line_1001)
        self.assertNotIn("\033[1;31m", line_1001)
        self.assertIn("无执行器", line_1002)
        self.assertIn("\033[1;31m", line_1002)             # red
        self.assertIn("执行器问题 1 项", out)

    def test_no_colour_when_not_asked(self):
        code, out = invoke(self.stack, "status")
        self.assertNotIn("\033[", out)

    def test_waive_retire_and_list(self):
        code, out = invoke(self.stack, "executors", "waive", "1002", "manual", "test", "account")
        self.assertEqual(code, 0)
        code, out = invoke(self.stack, "--json", "status")
        cfg = {c["id"]: c for c in json.loads(out)["accounts_config"]}
        self.assertEqual(cfg["1002"]["executor"]["state"], "waived")
        self.assertEqual(cfg["1002"]["executor"]["waiver"], "manual test account")

        code, out = invoke(self.stack, "executors")
        self.assertIn("com.example.exec-1001", out)
        self.assertIn("无人值守", out)

        code, out = invoke(self.stack, "executors", "retire", "com.example.exec-1001")
        self.assertEqual(code, 0)
        code, out = invoke(self.stack, "--json", "status")
        cfg = {c["id"]: c for c in json.loads(out)["accounts_config"]}
        self.assertEqual(cfg["1001"]["executor"]["state"], "missing")
        code, _out = invoke(self.stack, "executors", "retire", "com.example.exec-1001")
        self.assertEqual(code, 1)
        code, _out = invoke(self.stack, "executors", "unwaive", "1002")
        self.assertEqual(code, 0)


@unittest.skipUnless(HAVE_REST, "fastapi not installed (REST extras)")
class RestExecutorTests(unittest.TestCase):
    def setUp(self):
        from subledger.api import create_app

        ledger, broker, router, reconciler = make_stack()
        ledger.create_sub_account(SubAccount(id="4757"))
        self.client = TestClient(create_app(stack=(ledger, broker, router, reconciler),
                                            background_loops=False))

    def test_heartbeat_list_and_retire(self):
        r = self.client.post("/executors/heartbeat", json={
            "executor_id": "com.strategy-executer.4757", "sub_account_id": "4757",
            "name": "strategy-executer 4757", "pid": 99, "interval_s": 300,
            "message": "next preclose 15:55 ET"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["sub_account_known"])
        health = self.client.get("/executors").json()
        self.assertEqual(health["accounts"]["4757"]["state"], "ok")

        r = self.client.post("/executors/heartbeat", json={"executor_id": "x", "sub_account_id": "9999"})
        self.assertFalse(r.json()["sub_account_known"])
        health = self.client.get("/executors").json()
        self.assertEqual([o["executor_id"] for o in health["orphans"]], ["x"])

        self.assertEqual(self.client.delete("/executors/x").status_code, 200)
        self.assertEqual(self.client.delete("/executors/x").status_code, 404)
        r = self.client.post("/executors/heartbeat", json={"executor_id": " ", "sub_account_id": "4757"})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
