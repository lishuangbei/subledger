"""IB adapter tests against a fake ib_async.IB (no gateway). Skipped when
ib_async is not installed; the order/status/clock helpers are exercised with
real ib_async dataclasses so the mapping is the one production uses."""

import datetime as dt
import threading
import time
import unittest
from decimal import Decimal as D

try:
    import ib_async  # noqa: F401
    HAVE_IB = True
except ImportError:
    HAVE_IB = False

from subledger.broker.base import BrokerError
from subledger.models import (
    OrderClass, OrderRequest, OrderSide, OrderType, StopLossSpec, TakeProfitSpec, TimeInForce,
)

UTC = dt.timezone.utc
HOURS = ("20260922:0930-20260922:1600;20260923:0930-20260923:1600;"
         "20260926:CLOSED;20260927:CLOSED;20260928:0930-20260928:1600")


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _Client:
    def __init__(self):
        self._next = 100

    def getReqId(self):
        self._next += 1
        return self._next


def _now():
    return dt.datetime.now(UTC)


class FakeIB:
    """The slice of ib_async.IB the adapter touches. Orders are acknowledged
    (permId assigned) as soon as they are transmitted; symbol REJECT is
    rejected with error 201, symbol SLOW is never acknowledged, market orders
    fill immediately at `price`."""

    def __init__(self):
        from ib_async import Contract

        self.client = _Client()
        self.orderStatusEvent = _Event()
        self.openOrderEvent = _Event()
        self.disconnectedEvent = _Event()
        self.accounts = ["DU123"]
        self.clientId = None
        self.connected = False
        self.market_data_type = None
        self.price = 100.0
        self.summary = {"TotalCashValue": "50000", "NetLiquidation": "61000", "BuyingPower": "200000"}
        self.values = {}                # (tag, currency) -> value, for accountValues()
        self._trades = []
        self._fills = []
        self._positions = []
        self._portfolio = []
        self.tickers = {}
        self.unknown = set()
        self.overnight_hours = ""       # set to a window covering "now" to open the overnight venue
        self.block_direct_routing = False   # emulate the gateway's API precaution (error 10329)
        self.modify_error = None
        self.cancelled = []
        self.placed = []
        self._pending = []
        self._perm = 7000
        self._Contract = Contract

    # connection ---------------------------------------------------------
    async def connectAsync(self, host, port, clientId=1, timeout=4, readonly=False, account=""):
        self.connected = True
        self.clientId = clientId

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def managedAccounts(self):
        return list(self.accounts)

    def reqMarketDataType(self, kind):
        self.market_data_type = kind

    # state --------------------------------------------------------------
    def trades(self):
        return list(self._trades)

    def fills(self):
        return list(self._fills)

    def positions(self, account=""):
        return list(self._positions)

    def portfolio(self, account=""):
        return list(self._portfolio)

    async def accountSummaryAsync(self, account=""):
        from ib_async import AccountValue

        return [AccountValue(account, tag, value, "USD", "") for tag, value in self.summary.items()]

    def accountValues(self, account=""):
        from ib_async import AccountValue

        return [AccountValue(account, tag, value, ccy, "") for (tag, ccy), value in self.values.items()]

    async def reqAllOpenOrdersAsync(self):
        return [t for t in self._trades if t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive")]

    async def reqCompletedOrdersAsync(self, apiOnly):
        return [t for t in self._trades if t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")]

    def contract_for(self, symbol):
        return self._Contract(secType="STK", conId=abs(hash(symbol)) % 100000 + 1, symbol=symbol,
                              exchange="SMART", primaryExchange="ARCA", currency="USD")

    async def reqContractDetailsAsync(self, contract):
        from ib_async import ContractDetails

        if contract.symbol in self.unknown:
            return []
        if contract.exchange == "OVERNIGHT":
            if not self.overnight_hours:
                return []
            c = self.contract_for(contract.symbol)
            c.exchange = "OVERNIGHT"
            return [ContractDetails(contract=c, tradingHours=self.overnight_hours, timeZoneId="US/Eastern")]
        return [ContractDetails(contract=self.contract_for(contract.symbol), liquidHours=HOURS,
                                tradingHours=HOURS, timeZoneId="US/Eastern")]

    async def reqTickersAsync(self, *contracts):
        from ib_async import Ticker

        out = []
        for contract in contracts:
            ticker = Ticker(contract=contract)      # tick fields are not init args
            for name, value in self.tickers.get(contract.symbol, {}).items():
                setattr(ticker, name, value)
            out.append(ticker)
        return out

    # orders -------------------------------------------------------------
    def placeOrder(self, contract, order):
        from ib_async import Trade, OrderStatus
        from ib_async.objects import TradeLogEntry

        existing = next((t for t in self._trades
                         if t.order.orderId == order.orderId and t.order.clientId == self.clientId), None)
        if existing is not None:
            assert order.transmit, "a modify must be transmitted"
            existing.log.append(TradeLogEntry(_now(), existing.orderStatus.status, "Modify"))
            if self.modify_error:
                code, message = self.modify_error
                existing.log.append(TradeLogEntry(_now(), "Submitted", message, code))
            else:
                # IB echoes the accepted values back through openOrder
                existing.order.totalQuantity = order.totalQuantity
                existing.order.lmtPrice = order.lmtPrice
                existing.order.auxPrice = order.auxPrice
                self.openOrderEvent.emit(existing)
            return existing
        order.clientId = self.clientId
        trade = Trade(contract, order, OrderStatus(orderId=order.orderId, status="PendingSubmit"), [], [])
        self._trades.append(trade)
        self.placed.append(trade)
        self._pending.append(trade)
        if order.transmit:
            for pending in self._pending:
                self._ack(pending)
            self._pending = []
        return trade

    def _ack(self, trade):
        from ib_async.objects import TradeLogEntry

        symbol = trade.contract.symbol
        if symbol == "SLOW":
            return
        self._perm += 1
        trade.order.permId = self._perm
        trade.orderStatus.permId = self._perm
        rejected = symbol == "REJECT" or (symbol == "REJECTLEG" and trade.order.orderType.startswith("STP"))
        if trade.contract.exchange == "OVERNIGHT" and self.block_direct_routing:
            trade.orderStatus.status = "Cancelled"
            trade.log.append(TradeLogEntry(_now(), "Cancelled",
                                           "Error 10329, reqId 1: This order will be directly routed to OVERNIGHT. "
                                           "Restriction is specified in Precautionary Settings", 10329))
        elif rejected:
            trade.orderStatus.status = "Cancelled"
            trade.log.append(TradeLogEntry(_now(), "Cancelled",
                                           "Error 201, reqId 1: Order rejected - reason:no cash", 201))
        elif trade.order.orderType == "MKT" and not trade.order.parentId:
            trade.orderStatus.status = "Filled"
            trade.orderStatus.filled = trade.order.totalQuantity
            trade.orderStatus.avgFillPrice = self.price
        else:
            trade.orderStatus.status = "Submitted"
        self.openOrderEvent.emit(trade)
        self.orderStatusEvent.emit(trade)

    def cancelOrder(self, order, manualCancelOrderTime=""):
        from ib_async.objects import TradeLogEntry

        trade = next(t for t in self._trades
                     if t.order.orderId == order.orderId and t.order.clientId == order.clientId)
        self.cancelled.append(order.orderId)
        victims = [trade] + [t for t in self._trades            # IB cancels attached children
                             if t.order.parentId == order.orderId and t.order.clientId == order.clientId]
        for victim in victims:
            if victim.orderStatus.status in ("Filled", "Cancelled"):
                continue
            victim.orderStatus.status = "Cancelled"
            victim.log.append(TradeLogEntry(_now(), "Cancelled", "Error 202, reqId 1: Order Canceled - reason:", 202))
            self.orderStatusEvent.emit(victim)
        return trade

    # helpers for tests --------------------------------------------------
    def add_foreign_trade(self, symbol, status="Filled", client_id=55, order_id=9, perm_id=None,
                          order_ref="", filled_quantity=None, fills=None):
        from ib_async import Order, OrderStatus, Trade

        self._perm += 1
        perm_id = perm_id or self._perm
        order = Order(orderId=order_id, clientId=client_id, permId=perm_id, action="BUY",
                      totalQuantity=5.0, orderType="LMT", lmtPrice=90.0, orderRef=order_ref)
        if filled_quantity is not None:
            order.filledQuantity = filled_quantity
        trade = Trade(self.contract_for(symbol), order, OrderStatus(orderId=order_id, status=status, permId=perm_id), [], [])
        self._trades.append(trade)
        for shares, price in fills or []:
            self.add_fill(symbol, perm_id, shares, price, order_ref=order_ref)
        return trade

    def add_fill(self, symbol, perm_id, shares, price, commission=1.0, order_ref="", exec_id=None):
        from ib_async import CommissionReport, Execution, Fill

        exec_id = exec_id or "exec-{}".format(len(self._fills) + 1)
        execution = Execution(execId=exec_id, permId=perm_id, orderRef=order_ref, shares=shares,
                              price=price, acctNumber="DU123", side="BOT")
        fill = Fill(self.contract_for(symbol), execution, CommissionReport(execId=exec_id, commission=commission, currency="USD"), _now())
        self._fills.append(fill)
        return fill


def order(symbol="AAPL", side=OrderSide.BUY, qty="10", **kwargs):
    return OrderRequest(sub_account_id="alpha", symbol=symbol, side=side, qty=D(qty), **kwargs)


@unittest.skipUnless(HAVE_IB, "ib_async not installed")
class OrderPlanTests(unittest.TestCase):
    def plan(self, req, cid="sl.alpha.t1"):
        from subledger.broker.ib import build_order_plan
        return build_order_plan(req, cid, account="DU123")

    def test_simple_types(self):
        p = self.plan(order()).parent
        self.assertEqual((p.action, p.orderType, p.totalQuantity, p.tif, p.orderRef, p.account),
                         ("BUY", "MKT", 10.0, "DAY", "sl.alpha.t1", "DU123"))
        p = self.plan(order(order_type=OrderType.LIMIT, limit_price=D("99.5"),
                            time_in_force=TimeInForce.GTC, extended_hours=True)).parent
        self.assertEqual((p.orderType, p.lmtPrice, p.tif, p.outsideRth), ("LMT", 99.5, "GTC", True))
        p = self.plan(order(side=OrderSide.SELL, order_type=OrderType.STOP, stop_price=D("95"))).parent
        self.assertEqual((p.action, p.orderType, p.auxPrice), ("SELL", "STP", 95.0))
        p = self.plan(order(order_type=OrderType.STOP_LIMIT, stop_price=D("95"), limit_price=D("94.5"))).parent
        self.assertEqual((p.orderType, p.auxPrice, p.lmtPrice), ("STP LMT", 95.0, 94.5))
        p = self.plan(order(order_type=OrderType.TRAILING_STOP, trail_percent=D("2"))).parent
        self.assertEqual((p.orderType, p.trailingPercent), ("TRAIL", 2.0))
        p = self.plan(order(order_type=OrderType.TRAILING_STOP, trail_price=D("1.5"))).parent
        self.assertEqual((p.orderType, p.auxPrice), ("TRAIL", 1.5))

    def test_auction_tifs(self):
        p = self.plan(order(time_in_force=TimeInForce.CLS)).parent
        self.assertEqual((p.orderType, p.tif), ("MOC", "DAY"))
        p = self.plan(order(order_type=OrderType.LIMIT, limit_price=D("100"), time_in_force=TimeInForce.CLS)).parent
        self.assertEqual((p.orderType, p.tif), ("LOC", "DAY"))
        p = self.plan(order(time_in_force=TimeInForce.OPG)).parent
        self.assertEqual((p.orderType, p.tif), ("MKT", "OPG"))
        p = self.plan(order(time_in_force=TimeInForce.IOC)).parent
        self.assertEqual(p.tif, "IOC")

    def test_notional_refused(self):
        with self.assertRaises(BrokerError):
            self.plan(OrderRequest("alpha", "AAPL", OrderSide.BUY, notional=D("500")))

    def test_oco(self):
        plan = self.plan(order(side=OrderSide.SELL, order_type=OrderType.LIMIT, order_class=OrderClass.OCO,
                               take_profit=TakeProfitSpec(D("110")), stop_loss=StopLossSpec(D("90")),
                               time_in_force=TimeInForce.GTC))
        self.assertFalse(plan.attached)
        tp, (sl, role) = plan.parent, plan.legs[0]
        self.assertEqual((tp.action, tp.orderType, tp.lmtPrice, tp.ocaGroup, tp.ocaType), ("SELL", "LMT", 110.0, "sl.alpha.t1", 1))
        self.assertEqual((sl.action, sl.orderType, sl.auxPrice, sl.ocaGroup, sl.orderRef, role),
                         ("SELL", "STP", 90.0, "sl.alpha.t1", "sl.alpha.t1-sl", "stop_loss"))
        self.assertEqual(sl.tif, "GTC")

    def test_bracket_and_oto(self):
        plan = self.plan(order(order_type=OrderType.LIMIT, limit_price=D("100"), order_class=OrderClass.BRACKET,
                               take_profit=TakeProfitSpec(D("110")), stop_loss=StopLossSpec(D("90"), D("89"))))
        self.assertTrue(plan.attached)
        self.assertEqual([role for _, role in plan.legs], ["take_profit", "stop_loss"])
        sl = plan.legs[1][0]
        self.assertEqual((sl.orderType, sl.auxPrice, sl.lmtPrice, sl.action), ("STP LMT", 90.0, 89.0, "SELL"))
        oto = self.plan(order(order_class=OrderClass.OTO, stop_loss=StopLossSpec(D("90"))))
        self.assertEqual(len(oto.legs), 1)
        self.assertEqual(oto.legs[0][1], "stop_loss")


@unittest.skipUnless(HAVE_IB, "ib_async not installed")
class ClockParserTests(unittest.TestCase):
    def parse(self, when):
        from subledger.broker.ib import parse_ib_hours
        return parse_ib_hours(HOURS, "US/Eastern", when)

    def test_open_during_session(self):
        clock = self.parse(dt.datetime(2026, 9, 22, 15, 0, tzinfo=UTC))   # 11:00 ET
        self.assertTrue(clock.is_open)
        self.assertTrue(clock.next_close.startswith("2026-09-22T16:00:00-04:00"))
        self.assertTrue(clock.next_open.startswith("2026-09-23T09:30:00-04:00"))

    def test_closed_after_hours_and_weekend(self):
        clock = self.parse(dt.datetime(2026, 9, 22, 21, 0, tzinfo=UTC))   # 17:00 ET
        self.assertFalse(clock.is_open)
        self.assertTrue(clock.next_open.startswith("2026-09-23T09:30:00-04:00"))
        self.assertTrue(clock.next_close.startswith("2026-09-23T16:00:00-04:00"))
        weekend = self.parse(dt.datetime(2026, 9, 26, 15, 0, tzinfo=UTC))
        self.assertFalse(weekend.is_open)
        self.assertTrue(weekend.next_open.startswith("2026-09-28T09:30:00-04:00"))

    def test_empty_spec(self):
        from subledger.broker.ib import parse_ib_hours
        clock = parse_ib_hours("", "US/Eastern", dt.datetime.now(UTC))
        self.assertFalse(clock.is_open)
        self.assertEqual(clock.next_open, "")


@unittest.skipUnless(HAVE_IB, "ib_async not installed")
class IBBrokerTests(unittest.TestCase):
    def setUp(self):
        from subledger.broker.ib import IBBroker

        self.fake = FakeIB()
        self.broker = IBBroker(port=4002, client_id=7, paper=True, ib_factory=lambda: self.fake,
                               ack_timeout=0.5, call_timeout=5)

    def tearDown(self):
        self.broker.close()

    def test_describe_is_offline(self):
        self.assertEqual(self.broker.describe()["mode"], "paper")
        self.assertFalse(self.fake.connected)

    def test_submit_simple(self):
        result = self.broker.submit_order(order(order_type=OrderType.LIMIT, limit_price=D("95")), "sl.alpha.abc")
        self.assertEqual(result.broker_order_id, "7001")
        self.assertEqual(result.legs, [])
        trade = self.fake.placed[0]
        self.assertEqual((trade.order.orderRef, trade.order.clientId, trade.order.account), ("sl.alpha.abc", 7, "DU123"))
        self.assertEqual(self.fake.market_data_type, 3)
        state = self.broker.get_order("7001")
        self.assertEqual((state.status, state.client_order_id, state.symbol), ("open", "sl.alpha.abc", "AAPL"))

    def test_extended_hours_routes_to_overnight_when_open(self):
        req = order(order_type=OrderType.LIMIT, limit_price=D("95"), extended_hours=True)
        self.broker.submit_order(req, "sl.alpha.x1")
        self.assertEqual(self.fake.placed[-1].contract.exchange, "SMART")   # venue closed
        self.assertTrue(self.fake.placed[-1].order.outsideRth)
        now = dt.datetime.now(dt.timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
        start = (now - dt.timedelta(hours=1)).strftime("%Y%m%d:%H%M")
        end = (now + dt.timedelta(hours=1)).strftime("%Y%m%d:%H%M")
        self.fake.overnight_hours = "{}-{}".format(start, end)
        self.broker._overnight.clear()
        self.broker.submit_order(req, "sl.alpha.x2")
        self.assertEqual(self.fake.placed[-1].contract.exchange, "OVERNIGHT")
        # not for GTC / non-limit / exit groups
        self.broker.submit_order(order(order_type=OrderType.LIMIT, limit_price=D("95"), extended_hours=True,
                                       time_in_force=TimeInForce.GTC), "sl.alpha.x3")
        self.assertEqual(self.fake.placed[-1].contract.exchange, "SMART")

    def test_overnight_blocked_falls_back_to_smart(self):
        now = dt.datetime.now(dt.timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
        self.fake.overnight_hours = "{}-{}".format((now - dt.timedelta(hours=1)).strftime("%Y%m%d:%H%M"),
                                                   (now + dt.timedelta(hours=1)).strftime("%Y%m%d:%H%M"))
        self.fake.block_direct_routing = True
        req = order(order_type=OrderType.LIMIT, limit_price=D("95"), extended_hours=True)
        result = self.broker.submit_order(req, "sl.alpha.ovn")
        attempts = [t for t in self.fake.placed if t.order.orderRef == "sl.alpha.ovn"]
        self.assertEqual([t.contract.exchange for t in attempts], ["OVERNIGHT", "SMART"])
        self.assertEqual(result.broker_order_id, str(attempts[1].order.permId))
        self.assertEqual(self.broker.get_order(result.broker_order_id).status, "open")
        # repair lookup must find the live SMART order, not the rejected attempt
        self.assertEqual(self.broker.get_order_by_client_id("sl.alpha.ovn").broker_order_id, result.broker_order_id)
        # remembered: the next order goes straight to SMART ...
        self.broker.submit_order(req, "sl.alpha.ovn2")
        self.assertEqual(self.fake.placed[-1].contract.exchange, "SMART")
        # ... until the retry window elapses (operator may have fixed the gateway)
        self.broker._overnight_blocked_until = 0.0
        self.fake.block_direct_routing = False
        self.broker.submit_order(req, "sl.alpha.ovn3")
        self.assertEqual(self.fake.placed[-1].contract.exchange, "OVERNIGHT")

    def test_market_fill_reported(self):
        self.fake.price = 123.456789
        result = self.broker.submit_order(order(qty="3"), "sl.alpha.m1")
        state = self.broker.get_order(result.broker_order_id)
        self.assertEqual((state.status, state.filled_qty, state.filled_avg_price), ("filled", D("3"), D("123.456789")))

    def test_rejection_raises(self):
        with self.assertRaises(BrokerError) as ctx:
            self.broker.submit_order(order(symbol="REJECT"), "sl.alpha.r1")
        self.assertIn("rejected", str(ctx.exception))

    def test_ack_timeout_cancels(self):
        with self.assertRaises(BrokerError):
            self.broker.submit_order(order(symbol="SLOW"), "sl.alpha.s1")
        self.assertEqual(len(self.fake.cancelled), 1)

    def test_oco_submission_and_rollback(self):
        req = order(side=OrderSide.SELL, order_type=OrderType.LIMIT, order_class=OrderClass.OCO,
                    take_profit=TakeProfitSpec(D("110")), stop_loss=StopLossSpec(D("90")))
        result = self.broker.submit_order(req, "sl.alpha.oco1")
        self.assertEqual(result.broker_order_id, "7001")
        self.assertEqual([(leg.broker_order_id, leg.role, leg.client_order_id) for leg in result.legs],
                         [("7002", "stop_loss", "sl.alpha.oco1-sl")])
        # a rejected stop leg rolls the take-profit back
        req = OrderRequest("alpha", "REJECTLEG", OrderSide.SELL, D("10"), order_type=OrderType.LIMIT,
                           order_class=OrderClass.OCO, take_profit=TakeProfitSpec(D("110")),
                           stop_loss=StopLossSpec(D("90")))
        with self.assertRaises(BrokerError):
            self.broker.submit_order(req, "sl.alpha.oco2")
        tp = next(t for t in self.fake.placed if t.order.orderRef == "sl.alpha.oco2")
        self.assertEqual(tp.orderStatus.status, "Cancelled")

    def test_bracket_wiring(self):
        req = order(order_type=OrderType.LIMIT, limit_price=D("100"), order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitSpec(D("110")), stop_loss=StopLossSpec(D("90")))
        result = self.broker.submit_order(req, "sl.alpha.br1")
        parent, tp, sl = self.fake.placed[-3:]
        self.assertEqual((parent.order.transmit, tp.order.transmit, sl.order.transmit), (False, False, True))
        self.assertEqual((tp.order.parentId, sl.order.parentId), (parent.order.orderId, parent.order.orderId))
        self.assertEqual([leg.role for leg in result.legs], ["take_profit", "stop_loss"])
        self.assertEqual(result.broker_order_id, str(parent.order.permId))

    def test_cancel_oco_cancels_sibling(self):
        req = order(side=OrderSide.SELL, order_type=OrderType.LIMIT, order_class=OrderClass.OCO,
                    take_profit=TakeProfitSpec(D("110")), stop_loss=StopLossSpec(D("90")))
        result = self.broker.submit_order(req, "sl.alpha.oco1")
        self.broker.cancel_order(result.broker_order_id)
        self.assertEqual(len(self.fake.cancelled), 2)
        self.assertEqual(self.broker.get_order(result.legs[0].broker_order_id).status, "canceled")

    def test_cancel_bracket_child_cancels_sibling_only(self):
        req = order(order_type=OrderType.LIMIT, limit_price=D("100"), order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitSpec(D("110")), stop_loss=StopLossSpec(D("90")))
        result = self.broker.submit_order(req, "sl.alpha.br1")
        self.broker.cancel_order(result.legs[0].broker_order_id)
        parent, tp, sl = self.fake.placed[-3:]
        self.assertEqual(sorted(self.fake.cancelled), sorted([tp.order.orderId, sl.order.orderId]))
        self.assertEqual(self.broker.get_order(result.broker_order_id).status, "open")

    def test_cancel_bracket_parent_leaves_children_to_ib(self):
        req = order(order_type=OrderType.LIMIT, limit_price=D("100"), order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitSpec(D("110")), stop_loss=StopLossSpec(D("90")))
        result = self.broker.submit_order(req, "sl.alpha.br1")
        self.broker.cancel_order(result.broker_order_id)
        parent = self.fake.placed[-3]
        self.assertEqual(self.fake.cancelled, [parent.order.orderId])    # no explicit child cancels
        for leg in result.legs:
            self.assertEqual(self.broker.get_order(leg.broker_order_id).status, "canceled")

    def test_replace_is_a_modify(self):
        result = self.broker.submit_order(order(order_type=OrderType.LIMIT, limit_price=D("95")), "sl.alpha.rp")
        new_id = self.broker.replace_order(result.broker_order_id, qty=D("12"), limit_price=D("96"),
                                           client_order_id="sl.alpha.rp-r1")
        self.assertEqual(new_id, result.broker_order_id)
        trade = self.fake.placed[0]
        self.assertEqual((trade.order.totalQuantity, trade.order.lmtPrice, trade.order.orderRef), (12.0, 96.0, "sl.alpha.rp"))
        with self.assertRaises(BrokerError):
            self.broker.replace_order(result.broker_order_id, stop_price=D("90"))   # LMT has no stop

    def test_replace_refused_restores(self):
        result = self.broker.submit_order(order(order_type=OrderType.LIMIT, limit_price=D("95")), "sl.alpha.rp")
        self.fake.modify_error = (110, "Error 110: price does not conform to the minimum price variation")
        with self.assertRaises(BrokerError):
            self.broker.replace_order(result.broker_order_id, limit_price=D("95.001"))
        self.assertEqual(self.fake.placed[0].order.lmtPrice, 95.0)
        self.assertEqual(self.broker.get_order(result.broker_order_id).status, "open")

    def test_foreign_orders_cannot_be_modified_or_cancelled(self):
        trade = self.fake.add_foreign_trade("TSLA", status="Submitted")
        with self.assertRaises(BrokerError):
            self.broker.replace_order(str(trade.order.permId), limit_price=D("1"))
        with self.assertRaises(BrokerError):
            self.broker.cancel_order(str(trade.order.permId))

    def test_status_normalization(self):
        from ib_async.objects import TradeLogEntry

        rejected = self.fake.add_foreign_trade("A", status="Cancelled", order_ref="sl.alpha.x1")
        rejected.log.append(TradeLogEntry(_now(), "Cancelled", "Error 201", 201))
        cancelled = self.fake.add_foreign_trade("B", status="Cancelled", order_ref="sl.alpha.x2")
        cancelled.log.append(TradeLogEntry(_now(), "Cancelled", "Error 202", 202))
        partial = self.fake.add_foreign_trade("C", status="Submitted", order_ref="sl.alpha.x3")
        partial.orderStatus.filled = 2.0
        partial.orderStatus.avgFillPrice = 50.0
        inactive = self.fake.add_foreign_trade("E", status="Inactive", order_ref="sl.alpha.x4")
        inactive.log.append(TradeLogEntry(_now(), "Inactive", "Error 10147", 10147))
        self.assertEqual(self.broker.get_order(str(rejected.order.permId)).status, "rejected")
        self.assertEqual(self.broker.get_order(str(cancelled.order.permId)).status, "canceled")
        state = self.broker.get_order(str(partial.order.permId))
        self.assertEqual((state.status, state.filled_qty, state.filled_avg_price), ("partially_filled", D("2"), D("50")))
        self.assertEqual(self.broker.get_order(str(inactive.order.permId)).status, "rejected")

    def test_completed_order_fill_from_executions(self):
        # reqCompletedOrders rebuilds a Trade with an empty orderStatus; the
        # fill state must come from the executions.
        trade = self.fake.add_foreign_trade("MSFT", status="Filled", order_ref="sl.alpha.done",
                                            fills=[(3.0, 100.0), (2.0, 110.0)])
        state = self.broker.get_order(str(trade.order.permId))
        self.assertEqual((state.status, state.filled_qty, state.filled_avg_price), ("filled", D("5"), D("104")))

    def test_list_orders_includes_foreign(self):
        self.broker.submit_order(order(order_type=OrderType.LIMIT, limit_price=D("95")), "sl.alpha.mine")
        self.fake.add_foreign_trade("MNQZ6", status="Filled", order_ref="", filled_quantity=25.0)
        states = self.broker.list_orders(open_only=False)
        by_ref = {s.client_order_id: s for s in states}
        self.assertIn("sl.alpha.mine", by_ref)
        foreign = by_ref[""]
        self.assertEqual((foreign.status, foreign.filled_qty), ("filled", D("25")))
        self.assertEqual(len(self.broker.list_orders(open_only=True)), 1)

    def test_get_order_by_client_id_and_fill_fallback(self):
        result = self.broker.submit_order(order(order_type=OrderType.LIMIT, limit_price=D("95")), "sl.alpha.k1")
        self.assertEqual(self.broker.get_order_by_client_id("sl.alpha.k1").broker_order_id, result.broker_order_id)
        self.assertIsNone(self.broker.get_order_by_client_id("sl.alpha.nope"))
        self.fake.add_fill("NVDA", 999, 4.0, 200.0, order_ref="sl.alpha.aged")
        state = self.broker.get_order_by_client_id("sl.alpha.aged")
        self.assertEqual((state.broker_order_id, state.status, state.filled_qty, state.filled_avg_price),
                         ("999", "filled", D("4"), D("200")))

    def test_get_account(self):
        from ib_async import PortfolioItem, Position

        contract = self.fake.contract_for("AAPL")
        self.fake._positions.append(Position("DU123", contract, 10.0, 150.5))
        self.fake._portfolio.append(PortfolioItem(contract, 10.0, 160.25, 1602.5, 150.5, 97.5, 0.0, "DU123"))
        self.fake._positions.append(Position("DU123", self.fake._Contract(secType="FUT", symbol="MNQ", localSymbol="MNQZ6", conId=42), 2.0, 31000.0))
        state = self.broker.get_account()
        self.assertEqual((state.cash, state.equity, state.buying_power, state.account_id),
                         (D("50000"), D("61000"), D("200000"), "DU123"))
        self.assertEqual([(p.symbol, p.qty, p.avg_entry_price, p.current_price) for p in state.positions],
                         [("AAPL", D("10"), D("150.5"), D("160.25")), ("MNQZ6", D("2"), D("31000"), D("31000"))])

    def test_cash_scoped_to_ledger_currency(self):
        # TotalCashValue is the base-currency total of every currency held
        # (drifts with FX); the ledger only wants the USD balance.
        self.fake.values = {("CashBalance", "BASE"): "514805.63", ("CashBalance", "HKD"): "122185.23",
                            ("CashBalance", "USD"): "499227.70",
                            ("NetLiquidationByCurrency", "USD"): "499827.45"}
        state = self.broker.get_account()
        self.assertEqual((state.cash, state.equity), (D("499227.70"), D("499827.45")))
        from subledger.broker.ib import IBBroker
        total = IBBroker(client_id=8, paper=True, ib_factory=lambda: self.fake, call_timeout=5, cash_currency="")
        try:
            self.assertEqual(total.get_account().cash, D("50000"))
        finally:
            total.close()

    def test_paper_guard(self):
        from subledger.broker.ib import IBBroker

        self.fake.accounts = ["U999"]
        broker = IBBroker(client_id=8, paper=True, ib_factory=lambda: self.fake, call_timeout=5)
        try:
            with self.assertRaises(BrokerError) as ctx:
                broker.get_account()
            self.assertIn("LIVE", str(ctx.exception))
            with self.assertRaises(BrokerError):      # fails closed from now on
                broker.get_account()
        finally:
            broker.close()

    def test_cash_activities_are_commissions_with_attribution(self):
        self.fake.add_fill("AAPL", 7001, 10.0, 100.0, commission=1.050003, order_ref="sl.alpha.abc", exec_id="e1")
        self.fake.add_fill("AAPL", 7002, 10.0, 100.0, commission=0.0, order_ref="sl.alpha.zero", exec_id="e2")
        activities = self.broker.get_cash_activities()
        self.assertEqual(len(activities), 1)
        act = activities[0]
        self.assertEqual((act.activity_id, act.kind, act.amount, act.symbol, act.order_client_id),
                         ("ib-comm-e1", "fee", D("-1.05"), "AAPL", "sl.alpha.abc"))

    def test_last_prices_fallbacks(self):
        from ib_async import Position

        self.fake.tickers = {"AAPL": {"last": 101.0, "bid": 100.9, "ask": 101.1},
                             "MSFT": {"close": 300.0},
                             "NOPE": {}}
        self.fake._positions.append(Position("DU123", self.fake.contract_for("QQQ"), 1.0, 700.0))
        marks = self.broker.last_prices(["AAPL", "MSFT", "NOPE", "QQQ"])
        self.assertEqual(marks, {"AAPL": D("101"), "MSFT": D("300"), "QQQ": D("700")})

    def test_clock_and_asset(self):
        clock = self.broker.get_clock()
        self.assertIn(clock.is_open, (True, False))
        self.assertTrue(self.broker.get_asset("AAPL").tradable)
        self.assertFalse(self.broker.get_asset("AAPL").fractionable)
        self.fake.unknown.add("ZZZZ")
        self.assertFalse(self.broker.get_asset("ZZZZ").tradable)

    def test_order_listener(self):
        seen = []
        done = threading.Event()

        def listener(state):
            seen.append(state)
            done.set()

        self.broker.add_order_listener(listener)
        self.broker.submit_order(order(qty="2"), "sl.alpha.ev")
        self.assertTrue(done.wait(2))
        self.assertEqual((seen[0].client_order_id, seen[0].status, seen[0].filled_qty), ("sl.alpha.ev", "filled", D("2")))


@unittest.skipUnless(HAVE_IB, "ib_async not installed")
class IBStreamTests(unittest.TestCase):
    def test_stream_books_into_router(self):
        from subledger import Ledger, Router, SubAccount
        from subledger.broker.ib import IBBroker
        from subledger.stream import IBOrderStream

        fake = FakeIB()
        broker = IBBroker(client_id=9, paper=True, ib_factory=lambda: fake, call_timeout=5)
        try:
            ledger = Ledger(":memory:")
            router = Router(ledger, broker, eager_sync=False)
            router.adopt_broker_cash()
            router.create_sub_account(SubAccount(id="alpha"), initial_allocation=D("20000"))
            events = []
            stream = IBOrderStream(router, broker)
            stream.add_listener(lambda event, state: events.append(event))
            stream.start()
            fake.price = 100.0
            record = router.place_order(order(qty="5"), est_price=D("100"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and ledger.get_order(record.id).filled_qty == 0:
                time.sleep(0.02)
            booked = ledger.get_order(record.id)
            self.assertEqual((booked.status.value, booked.filled_qty), ("filled", D("5")))
            self.assertEqual(ledger.get_sub_account("alpha").cash, D("19500"))
            self.assertIn("fill", events)
        finally:
            broker.close()


if __name__ == "__main__":
    unittest.main()
