"""示例策略 1：极简动量轮动 —— 每周买入近 22 日涨幅最高的 ETF。
仅作演示子账户机制之用，不构成任何投资建议。"""

from lumibot.strategies import Strategy

from ledger_mixin import SubAccountMixin


class Momentum(SubAccountMixin, Strategy):
    parameters = {
        "universe": ["SPY", "QQQ", "IWM", "GLD"],
        "lookback_days": 22,
    }

    def initialize(self):
        self.sleeptime = "1D"

    def on_trading_iteration(self):
        universe = self.parameters["universe"]
        lookback = self.parameters["lookback_days"]

        # 排名：近 lookback 日收益
        returns = {}
        for symbol in universe:
            bars = self.get_historical_prices(symbol, lookback + 1, "day")
            if bars is None or bars.df.empty:
                continue
            closes = bars.df["close"]
            returns[symbol] = closes.iloc[-1] / closes.iloc[0] - 1
        if not returns:
            self.snapshot()
            return
        winner = max(returns, key=returns.get)

        # 换仓：卖掉不是 winner 的持仓（只动本策略自己的持仓）
        for pos in self.get_positions():
            symbol = pos.asset.symbol
            if symbol != winner and float(pos.quantity) > 0:
                self.guarded_sell(symbol, pos.quantity)

        # 用本策略的 cash 买入 winner
        price = self.get_last_price(winner)
        if price:
            qty = int(self.get_cash() // float(price))
            if qty > 0:
                self.guarded_buy(winner, qty)

        self.snapshot()
