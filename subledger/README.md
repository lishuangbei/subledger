# subledger — 自研方案：单一真实账户的虚拟子账户路由器

一个真实的 Alpaca 账户，切分成多个**虚拟子账户**。每个策略只面对自己的子账户；
路由器（Router）是唯一接触真实账户的组件。

```
策略 A ──┐                                      ┌─> Alpaca (real money)
策略 B ──┼─> Router ──风控──> BrokerAdapter ────┤
策略 C ──┘      │                               └─> IB (以后加 adapter 即可)
                │
           Ledger (SQLite)          Reconciler (定时核对)
        子账户资金/持仓/PnL      ledger 总和 == 真实账户?
```

## 核心机制

**归属追踪**：每笔订单的 `client_order_id` 编码为 `sl.<子账户>.<uuid>`。
任何成交都能无歧义归属到子账户；真实账户上出现**没有这个前缀的订单**
（比如你手动在 App 里下的单），对账器会立刻标记为 `unknown_order`——
券商代生成的 OCO/bracket 子腿除外，它们的 broker id 已登记在账本里，
不会误报。人工确认过的外来单用 `reconciler.acknowledge_foreign_orders(report)`
落进账本（`acknowledged_foreign` 表），之后不再报告。`client_tag` 可指定
确定性 client id 后缀，配合 `ledger.get_order_by_client_id` 实现幂等提交。

**订单能力（v0.3.0）**：

| 维度 | 支持 |
|---|---|
| 类型 | market / limit / stop / stop_limit / trailing_stop |
| 组合单 | **OCO**（止盈+止损保护已有多头）/ **bracket**（入场+双腿）/ **OTO** |
| 期限 | day / gtc / ioc / fok / **opg / cls**（开/收盘竞价），可选 `extended_hours`（限 DAY limit） |
| 数量 | 整股 / 碎股 / `notional` 按金额（仅市价买） |
| 改单 | `router.replace_order(qty/limit_price/stop_price)`，预留同步调整；**OCO/bracket 的腿支持价格改单**（收紧止损/移动止盈无"撤单重挂"的无保护窗口） |

**运行面（v0.3.0）**：

- `broker.get_clock()` / `broker.get_asset(symbol)`——开闭市状态与标的元数据；
  router 下单前自动做 tradable/fractionable 防呆（venue 答不上来则跳过）
- `router.repair()`——启动时崩溃恢复：对"已预留但没拿到 broker id"的孤儿单,
  按 client id 问券商"到底提交没有"，提交了收养、没提交释放预留
- `subledger.stream.TradeUpdateStream`——Alpaca trade_updates WebSocket 推送
  直接入账（事件自带累计成交状态，零额外 API 调用），轮询 `sync()` 退居
  安全网；delta 记账保证双路投递无副作用
- Alpaca adapter 自带瞬时错误重试（429/5xx/断连，指数退避）与订单历史分页

OCO/bracket 的两条退出腿**共享同一份股份预留**（一腿成交、另一腿自动取消，
预留随之释放），由 router 的 exit-group 记账保证任何一腿成交都正确计入
子账户 PnL。止损/止盈因此挂在**券商侧**，轮询延迟只影响记账时点，
不影响成交本身。

**资金记账（复式思想）**：总资金 = 未分配池 + Σ(子账户 cash)，这个总和
只有在成交/入金时变动。下买单时先**预留**（reserved_cash），成交后转为持仓、
取消后退回——两个策略并发下单不可能同时通过同一笔购买力检查。

**子账户风控**（`risk.py`，全部下单前检查）：

| 限制 | 字段 | 行为 |
|---|---|---|
| 资金上限 | `cash` + 预留机制 | 买单超出可用购买力 → 拒单 |
| 保证金倍数 | `margin_multiplier` | equity 基准：`bp = equity×m − 持仓市值 − 预留`。亏损自动收缩额度，名义倍数可用满；m=1 精确等于现金账户（cash−预留）。维持保证金/利息不建模，券商侧兜底 |
| 单笔上限 | `max_order_notional` | 超额拒单 |
| 当日止损 | `daily_loss_limit` | 当日已实现亏损超限 → 禁止开新仓（仍可平仓） |
| 标的白名单 | `symbol_whitelist` | 白名单外拒单 |
| 卖出隔离 | 持仓账本 | **A 策略不能卖掉 B 策略的持仓**（防止 PnL 串账） |
| 总闸 | `halt()` | kill switch，全局拒单 |

**对账器（Reconciler）**，定时跑（建议 5–15 分钟）：
1. Σ(子账户 cash) + 未分配池 == 真实账户 cash（容差 $0.01）
2. 每个 symbol：Σ(子账户持仓) == 真实账户持仓
3. 扫描真实账户订单，标记非路由器下的"外来订单"
4. 顺带刷新持仓 mark price（unrealized PnL 保持诚实）
5. 可选 `halt_on_drift=True`：发现偏差自动拉闸
6. 入金/分红等合法外部变动，用 `absorb_cash_drift()` 吸收进未分配池

## 目录

```
subledger/
  models.py       # 数据模型（Decimal 记账，绝不用 float）
  ledger.py       # SQLite 账本：子账户/持仓/订单/对账历史
  risk.py         # 下单前风控检查
  router.py       # 核心：下单->风控->预留->转发->记账
  reconciler.py   # 定时核对
  broker/
    base.py       # BrokerAdapter 接口（对接 IB 只需实现这 5 个方法）
    alpaca.py     # Alpaca 实现（alpaca-py，惰性导入）
    mock.py       # 内存模拟券商（测试/演示用）
  api.py          # 可选 REST 层（FastAPI），策略可跨进程/跨语言接入
tests/            # 22 个测试，零依赖可跑
examples/demo.py  # 端到端演示，无需任何凭据
```

## 快速开始

```bash
# 核心零依赖，直接跑测试和 demo（Python 3.9+）
python3 -m unittest discover tests
python3 examples/demo.py

# 实盘/纸盘（Alpaca）
pip install "subledger[all]"      # 或 pip install alpaca-py fastapi uvicorn
export SUBLEDGER_BROKER=alpaca ALPACA_API_KEY=... ALPACA_SECRET_KEY=... ALPACA_PAPER=true
uvicorn subledger.api:create_app --factory
```

Python 内嵌用法（策略和路由器同进程）：

```python
from decimal import Decimal as D
from subledger import Ledger, Router, Reconciler, SubAccount, OrderRequest, OrderSide
from subledger.broker.alpaca import AlpacaBroker

broker = AlpacaBroker(api_key="...", secret_key="...", paper=True)
ledger = Ledger("subledger.db")
router = Router(ledger, broker)

router.adopt_broker_cash()                       # 一次性：把账户现金收进未分配池
router.create_sub_account(SubAccount(id="momo"), initial_allocation=D("30000"))

router.place_order(OrderRequest("momo", "AAPL", OrderSide.BUY, D("10")))
router.sync()                                    # 轮询成交（几秒一次，或接 trade stream）
print(router.equity_snapshot("momo"))

Reconciler(ledger, broker, halt_on_drift=True).run()   # 定时核对
```

REST 用法（策略只知道自己的子账户 id，拿不到 Alpaca 密钥）：

```bash
curl -X POST :8000/accounts -d '{"id":"momo","allocation":"30000","max_order_notional":"15000"}'
curl -X POST :8000/orders   -d '{"sub_account_id":"momo","symbol":"AAPL","side":"buy","qty":"10"}'
curl :8000/accounts/momo    # cash / equity / realized / unrealized
curl :8000/reconcile/latest
curl -X POST :8000/halt     # kill switch
```

## 对接 IB 的路径

`broker/base.py` 只要求 5 个方法：`submit_order / cancel_order / get_order /
list_orders / get_account`。写一个基于 `ib_insync`（或官方 `ibapi`）的
`IBBroker` 即可，router/ledger/reconciler 零改动。IB 的 `orderRef` 字段可以
承载我们的 client_order_id 归属编码。

## 已知简化与边界（review 时重点看这里）

1. **保证金模型是 Reg-T 简化版**（v0.4.0 起 equity 基准）：
   `buying_power = equity × multiplier − 持仓市值 − reserved`。亏损收缩、
   盈利扩张，但不模拟维持保证金线/强平/融资利息——真实账户在 Alpaca 端的
   保证金引擎是最终兜底。
2. **市价单的风控用估价**（限价单用限价，市价单用最近 mark）。跳空时实际成交
   可能超预留，差额会体现在子账户 cash 上（可能轻微透支），对账不受影响。
3. **默认禁止做空**；`allow_short=True` 时用简化购买力检查。两个子账户一多一空
   同一 symbol 会在真实账户层面净额抵消——账本仍各自正确，但真实账户持仓是净头寸，
   对账器按净额比较（`aggregate_positions`），这是设计行为。
4. **成交靠轮询** `router.sync()`（默认 5 秒）。要更低延迟可接 Alpaca 的
   trade_updates WebSocket，回调里调 `sync()` 即可，记账逻辑不变（delta 记账，重复
   触发无副作用）。
5. **手续费/借券费未建模**（Alpaca 股票零佣金，问题不大；SEC/TAF 卖出费会以微小
   cash drift 形式出现，被 $0.01 容差吸收或由 `absorb_cash_drift` 处理）。
6. **单进程写入**：ledger 假设 router 是唯一写者。多进程部署时策略必须走 REST，
   不能各自开 Router 实例。
7. 洗售（wash sale）、PDT 规则、公司行动（拆股/分红再投资）不处理——拆股会表现为
   position drift，需要人工用 ledger 修正后 resume。
