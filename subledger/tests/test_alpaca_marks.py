import unittest
from decimal import Decimal as D

try:
    import alpaca  # noqa: F401
    HAVE_ALPACA = True
except ImportError:
    HAVE_ALPACA = False


@unittest.skipUnless(HAVE_ALPACA, "alpaca-py not installed")
class AlpacaLastPricesTests(unittest.TestCase):
    """Regression for defect report #9: the base-class last_prices only knows
    held positions, so /marks (and market-order sizing over REST) starved on
    any symbol the real account didn't already hold."""

    def setUp(self):
        from subledger.broker.alpaca import AlpacaBroker
        from subledger.broker.base import BrokerAccountState

        self.broker = AlpacaBroker("key-id", "secret", paper=True)
        # keep the position-derived last resort off the network
        self.broker.get_account = lambda: BrokerAccountState(
            cash=D("0"), equity=D("0"), positions=[])

    class _FakeDataClient:
        def __init__(self, trades):
            self._trades = trades

        def get_stock_latest_trade(self, request):
            class Trade:
                def __init__(self, price):
                    self.price = price

            return {s: Trade(p) for s, p in self._trades.items()}

    def test_batch_marks_for_unheld_symbols(self):
        self.broker._data_client = self._FakeDataClient({"AAPL": 123.45, "MSFT": 500.5})
        marks = self.broker.last_prices(["AAPL", "MSFT", "NOPE"])
        self.assertEqual(marks, {"AAPL": D("123.45"), "MSFT": D("500.5")})

    def test_empty_symbol_list(self):
        self.assertEqual(self.broker.last_prices([]), {})

    def test_data_api_failure_falls_back_to_positions(self):
        from subledger.broker.base import BrokerAccountState, BrokerPosition

        class Boom:
            def get_stock_latest_trade(self, request):
                raise RuntimeError("data API down")

        self.broker._data_client = Boom()
        self.broker.get_account = lambda: BrokerAccountState(
            cash=D("0"), equity=D("100"),
            positions=[BrokerPosition(symbol="QQQ", qty=D("1"), avg_entry_price=D("700"),
                                      current_price=D("733"))])
        self.assertEqual(self.broker.last_prices(["QQQ", "AAPL"]), {"QQQ": D("733")})
