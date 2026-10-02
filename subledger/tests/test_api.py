import unittest
from decimal import Decimal as D

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_REST = True
except ImportError:
    HAVE_REST = False


@unittest.skipUnless(HAVE_REST, "fastapi not installed (REST extras)")
class RestWritePathTests(unittest.TestCase):
    """Regression for the function-local pydantic models bug: with
    `from __future__ import annotations`, FastAPI resolved endpoint body
    annotations against module globals, missed the function-local classes,
    and degraded every body param to a required query field (all write
    endpoints 422 'query.body missing')."""

    def setUp(self):
        from subledger import Ledger, Reconciler, Router
        from subledger.api import create_app
        from subledger.broker.mock import MockBroker

        self.broker = MockBroker(cash=D("100000"))
        ledger = Ledger(":memory:")
        router = Router(ledger, self.broker)
        router.adopt_broker_cash()
        self.broker.set_price("AAPL", D("100"))
        self.client = TestClient(create_app(
            stack=(ledger, self.broker, router, Reconciler(ledger, self.broker)),
            background_loops=False))

    def test_write_endpoints_parse_json_bodies(self):
        r = self.client.post("/accounts", json={"id": "t1", "allocation": "50000"})
        self.assertEqual(r.status_code, 200, r.text)

        r = self.client.post("/orders", json={
            "sub_account_id": "t1", "symbol": "AAPL", "side": "buy", "qty": "5",
            "order_type": "limit", "limit_price": "99", "time_in_force": "gtc"})
        self.assertEqual(r.status_code, 200, r.text)
        oid = r.json()["id"]

        r = self.client.patch("/orders/{}".format(oid), json={"limit_price": "98"})
        self.assertEqual(r.status_code, 200, r.text)

        r = self.client.post("/orders", json={
            "sub_account_id": "t1", "symbol": "AAPL", "side": "buy", "qty": "3",
            "order_class": "bracket",
            "take_profit": {"limit_price": "120"},
            "stop_loss": {"stop_price": "90"}})
        self.assertEqual(r.status_code, 200, r.text)   # nested models resolve

        r = self.client.post("/accounts/t1/allocate", json={"amount": "-1000"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_clock_and_marks_read_through(self):
        r = self.client.get("/clock")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("is_open", r.json())

        self.broker.set_price("MSFT", D("500.5"))
        r = self.client.get("/marks", params={"symbols": "aapl, MSFT,NOPE"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"AAPL": "100", "MSFT": "500.5"})  # unknown omitted

        r = self.client.get("/marks", params={"symbols": " , "})
        self.assertEqual(r.status_code, 422)

    def test_order_view_exposes_client_order_id(self):
        self.client.post("/accounts", json={"id": "t3", "allocation": "20000"})
        r = self.client.post("/orders", json={
            "sub_account_id": "t3", "symbol": "AAPL", "side": "buy", "qty": "1",
            "client_tag": "idem-check"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["client_order_id"], "sl.t3.idem-check")

    def test_business_rejection_is_422_with_string_detail(self):
        self.client.post("/accounts", json={"id": "t2", "allocation": "10"})
        r = self.client.post("/orders", json={
            "sub_account_id": "t2", "symbol": "AAPL", "side": "buy", "qty": "999"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("buying power", r.json()["detail"])   # app-level, not validation


@unittest.skipUnless(HAVE_REST, "fastapi not installed (REST extras)")
class RestViewContractTests(unittest.TestCase):
    """Fields REST executors depend on. Regression for 2026-10-02: the
    positions view lacked reserved_qty, so the executor kernel computed
    free = qty - 0, never judged a Protect armed, abandoned the batch at the
    next plan and canceled the protective OCO with it (liveAlpa META on
    09-26, liveIB PANW on 10-02)."""

    def setUp(self):
        from subledger import Ledger, Reconciler, Router
        from subledger.api import create_app
        from subledger.broker.mock import MockBroker

        self.broker = MockBroker(cash=D("100000"))
        ledger = Ledger(":memory:")
        router = Router(ledger, self.broker)
        router.adopt_broker_cash()
        self.broker.set_price("PANW", D("400"))
        self.client = TestClient(create_app(
            stack=(ledger, self.broker, router, Reconciler(ledger, self.broker)),
            background_loops=False))
        self.client.post("/accounts", json={"id": "4757", "allocation": "5000"})

    def test_positions_carry_reserved_qty_once_protected(self):
        r = self.client.post("/orders", json={"sub_account_id": "4757", "symbol": "PANW",
                                              "side": "buy", "qty": "2"})
        self.assertEqual(r.status_code, 200, r.text)
        pos = {p["symbol"]: p for p in self.client.get("/accounts/4757/positions").json()}
        self.assertEqual((pos["PANW"]["qty"], pos["PANW"]["reserved_qty"]), ("2", "0"))
        r = self.client.post("/orders", json={
            "sub_account_id": "4757", "symbol": "PANW", "side": "sell", "qty": "2",
            "order_type": "limit", "order_class": "oco", "time_in_force": "gtc",
            "take_profit": {"limit_price": "472.13"}, "stop_loss": {"stop_price": "369.41"}})
        self.assertEqual(r.status_code, 200, r.text)
        pos = {p["symbol"]: p for p in self.client.get("/accounts/4757/positions").json()}
        self.assertEqual(D(pos["PANW"]["qty"]) - D(pos["PANW"]["reserved_qty"]), D("0"))

    def test_account_view_carries_max_order_notional(self):
        view = self.client.get("/accounts/4757").json()
        self.assertIn("max_order_notional", view)
        self.assertIsNone(view["max_order_notional"])
