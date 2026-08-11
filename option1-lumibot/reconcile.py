"""定时对账脚本：各策略快照总和 vs Alpaca 真实账户。

    python reconcile.py            # 跑一次，drift 超容差时退出码=1
    # crontab 每 15 分钟一次，失败时邮件/通知:
    # */15 * * * * cd /path/to/option1-lumibot && python reconcile.py || <告警命令>

核对内容：
  1. Σ(策略 portfolio_value) vs 账户 equity（差额 = 账户里未分配给策略的资金）
  2. Σ(策略持仓 qty) vs 账户持仓，逐 symbol 比较
  3. 快照新鲜度（策略挂掉导致的陈旧快照也要报警）
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
QTY_TOLERANCE = 1e-6
# 策略快照总额与账户 equity 允许的"未分配资金"最大变化（对账基准）
BASELINE_FILE = os.path.join(STATE_DIR, "_baseline.json")
DRIFT_TOLERANCE = float(os.environ.get("RECONCILE_DRIFT_TOLERANCE", "50"))  # 美元
STALE_AFTER = timedelta(hours=26)  # 隔一个交易日还没更新就算陈旧


def load_snapshots():
    snaps = []
    for path in sorted(glob.glob(os.path.join(STATE_DIR, "*.json"))):
        if os.path.basename(path).startswith("_"):
            continue
        with open(path) as f:
            snaps.append(json.load(f))
    return snaps


def get_alpaca_account():
    from alpaca.trading.client import TradingClient

    client = TradingClient(
        os.environ["ALPACA_API_KEY"],
        os.environ["ALPACA_API_SECRET"],
        paper=os.environ.get("ALPACA_PAPER", "true").lower() != "false",
    )
    account = client.get_account()
    positions = {p.symbol: float(p.qty) for p in client.get_all_positions()}
    return float(account.equity), float(account.cash), positions


def main():
    problems = []
    snaps = load_snapshots()
    if not snaps:
        print("没有找到策略快照（state/*.json）—— 策略是否在运行？")
        sys.exit(1)

    equity, cash, broker_positions = get_alpaca_account()

    # 1) 新鲜度
    now = datetime.now(timezone.utc)
    for s in snaps:
        age = now - datetime.fromisoformat(s["at"])
        if age > STALE_AFTER:
            problems.append(f"策略 {s['strategy']} 的快照已 {age} 未更新（策略挂了?）")

    # 2) 持仓逐 symbol 核对
    strat_positions = {}
    for s in snaps:
        for p in s["positions"]:
            strat_positions[p["symbol"]] = strat_positions.get(p["symbol"], 0.0) + p["qty"]
    for symbol in sorted(set(strat_positions) | set(broker_positions)):
        expected = strat_positions.get(symbol, 0.0)
        actual = broker_positions.get(symbol, 0.0)
        if abs(expected - actual) > QTY_TOLERANCE:
            problems.append(
                f"持仓不一致 {symbol}: 策略账本合计 {expected}, Alpaca 实际 {actual}"
                + ("（账户里有策略之外的持仓？）" if expected == 0 else "")
            )

    # 3) 资金：策略总额 + 未分配 == 账户 equity。
    #    未分配部分取首次运行时的基准，之后基准漂移超容差就报警。
    strat_total = sum(s["portfolio_value"] for s in snaps)
    unallocated = equity - strat_total
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE) as f:
            baseline = json.load(f)["unallocated"]
        drift = unallocated - baseline
        if abs(drift) > DRIFT_TOLERANCE:
            problems.append(
                f"资金漂移 ${drift:,.2f}: 策略合计 ${strat_total:,.2f}, "
                f"账户 equity ${equity:,.2f}, 未分配 ${unallocated:,.2f} "
                f"(基准 ${baseline:,.2f})。入金/出金/手动交易/费用？"
                f"确认合法后删除 {BASELINE_FILE} 重建基准。"
            )
    else:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(BASELINE_FILE, "w") as f:
            json.dump({"unallocated": unallocated, "at": now.isoformat()}, f)
        print(f"首次运行：记录未分配资金基准 ${unallocated:,.2f}")

    # 汇报
    print(f"账户 equity ${equity:,.2f} | cash ${cash:,.2f} | 策略数 {len(snaps)}")
    for s in snaps:
        print(f"  - {s['strategy']:12s} 市值 ${s['portfolio_value']:>12,.2f}  cash ${s['cash']:>12,.2f}")
    if problems:
        print("\n对账失败：")
        for p in problems:
            print("  !!", p)
        sys.exit(1)
    print("对账通过 ✔")


if __name__ == "__main__":
    main()
