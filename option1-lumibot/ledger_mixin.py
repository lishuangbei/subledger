"""SubAccountMixin: 给 lumibot Strategy 加上"虚拟子账户"约束和状态快照。

lumibot 的 budget 参数只是初始资金划分，不做硬性风控；这个 mixin 补上：
  - 硬性资金上限（超出 budget 的买单直接跳过并告警）
  - 每次迭代把本策略的 cash/持仓/PnL 快照写到 state/<策略名>.json，
    供 reconcile.py 定时核对使用
"""

import json
import os
from datetime import datetime, timezone

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")


class SubAccountMixin:
    """混入到 lumibot Strategy。子类在 on_trading_iteration 里用
    self.guarded_buy(...) 代替直接 submit_order，结束时调 self.snapshot()。"""

    # -- 硬性资金约束 --------------------------------------------------

    def guarded_buy(self, symbol, qty, **order_kwargs):
        """只在本策略自己的 cash 足够时才下买单。lumibot 在多策略共享一个
        broker 时会按策略维护 cash，但不会阻止你花超 —— 这里补硬约束。"""
        price = self.get_last_price(symbol)
        if price is None:
            self.log_message(f"[sub-account] no price for {symbol}, skip")
            return None
        notional = float(price) * float(qty)
        cash = self.get_cash()
        if notional > cash:
            self.log_message(
                f"[sub-account] BLOCKED: buy {qty} {symbol} needs "
                f"${notional:,.2f} but strategy cash is ${cash:,.2f}"
            )
            return None
        order = self.create_order(symbol, qty, "buy", **order_kwargs)
        return self.submit_order(order)

    def guarded_sell(self, symbol, qty, **order_kwargs):
        """只卖本策略账本上的持仓，防止卖掉其他策略的股票。"""
        pos = self.get_position(symbol)
        held = float(pos.quantity) if pos is not None else 0.0
        if float(qty) > held:
            self.log_message(
                f"[sub-account] BLOCKED: sell {qty} {symbol} but this "
                f"strategy only holds {held}"
            )
            return None
        order = self.create_order(symbol, qty, "sell", **order_kwargs)
        return self.submit_order(order)

    # -- 状态快照（对账用） --------------------------------------------

    def snapshot(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        positions = []
        for pos in self.get_positions():
            symbol = pos.asset.symbol if hasattr(pos.asset, "symbol") else str(pos.asset)
            last = self.get_last_price(pos.asset)
            positions.append(
                {
                    "symbol": symbol,
                    "qty": float(pos.quantity),
                    "last_price": None if last is None else float(last),
                }
            )
        data = {
            "strategy": self.name,
            "at": datetime.now(timezone.utc).isoformat(),
            "cash": float(self.get_cash()),
            "portfolio_value": float(self.get_portfolio_value()),
            "positions": positions,
        }
        path = os.path.join(STATE_DIR, f"{self.name}.json")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
