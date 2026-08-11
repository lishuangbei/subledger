"""示例策略 2：定投 —— 每个交易日买固定金额的 SPY。
仅作演示子账户机制之用。"""

from lumibot.strategies import Strategy

from ledger_mixin import SubAccountMixin


class DollarCostAverage(SubAccountMixin, Strategy):
    parameters = {
        "symbol": "SPY",
        "daily_amount": 200,  # 每天投入的美元
    }

    def initialize(self):
        self.sleeptime = "1D"

    def on_trading_iteration(self):
        symbol = self.parameters["symbol"]
        amount = self.parameters["daily_amount"]
        price = self.get_last_price(symbol)
        if price:
            qty = round(amount / float(price), 4)  # Alpaca 支持碎股
            if qty > 0:
                self.guarded_buy(symbol, qty)
        self.snapshot()
