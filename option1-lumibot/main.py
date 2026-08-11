"""入口：一个 Alpaca 连接 + 多个策略，每个策略独立 budget。

    pip install -r requirements.txt
    cp .env.example .env   # 填入凭据，先用 ALPACA_PAPER=true
    python main.py
"""

import os

from dotenv import load_dotenv
from lumibot.brokers import Alpaca
from lumibot.traders import Trader

from strategies.dca import DollarCostAverage
from strategies.momentum import Momentum

load_dotenv()

ALPACA_CONFIG = {
    "API_KEY": os.environ["ALPACA_API_KEY"],
    "API_SECRET": os.environ["ALPACA_API_SECRET"],
    "PAPER": os.environ.get("ALPACA_PAPER", "true").lower() != "false",
}


def main():
    trader = Trader()
    broker = Alpaca(ALPACA_CONFIG)  # 一个真实账户连接，多个策略共享

    # 每个策略 = 一个"子账户"：独立 name（用于订单归属和日志）+ 独立 budget
    momentum = Momentum(name="momentum", broker=broker, budget=30000)
    dca = DollarCostAverage(name="dca", broker=broker, budget=10000)

    trader.add_strategy(momentum)
    trader.add_strategy(dca)
    trader.run_all()  # 每个策略跑在自己的线程里


if __name__ == "__main__":
    main()
