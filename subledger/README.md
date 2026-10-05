# subledger — 自研方案：单一真实账户的虚拟子账户路由器

一个真实的券商账户（Alpaca 或 Interactive Brokers），切分成多个**虚拟子账户**。
每个策略只面对自己的子账户；路由器（Router）是唯一接触真实账户的组件。

```
策略 A ──┐                                      ┌─> Alpaca (broker/alpaca.py)
策略 B ──┼─> Router ──风控──> BrokerAdapter ────┤
策略 C ──┘      │                               └─> IB Gateway/TWS (broker/ib.py)
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

**现金按结算精度记账**：券商按分结算，adapter 声明 `cash_precision`（Alpaca/IB = 0.01），
router 把每张订单的累计成交额取整到分再记差额，账本里不产生券商没有的分以下尾数。

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
5. 可选 `halt_on_drift=True`：发现偏差自动拉闸（粘性，需人工 `ops resume`）；
   可选 `halt_min_cash_drift`（env `SUBLEDGER_HALT_MIN_CASH_DRIFT`）：仅现金偏差且小于
   该金额时只报告不拉闸——券商的逐笔规费当时扣现金、活动记录晚几个小时才发布，
   每笔成交都会造成一两分钱的瞬时偏差
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
    base.py       # BrokerAdapter 接口
    alpaca.py     # Alpaca 实现（alpaca-py，惰性导入）
    ib.py         # Interactive Brokers 实现（ib_async，惰性导入；见下文）
    mock.py       # 内存模拟券商（测试/演示用）
  stream.py       # 推送入账：Alpaca trade_updates / IB 订单事件
  api.py          # 可选 REST 层（FastAPI），策略可跨进程/跨语言接入
  daemon.py       # 通用 booking daemon（repair + REST + 推送流 + 净值/备份）
  cli.py          # 运维 CLI（accounts/positions/equity/returns/orders/ops）
tests/            # 150 个测试，核心零依赖可跑（alpaca/ib 相关未装库时自动跳过）
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

# 实盘/纸盘（Interactive Brokers：本机先跑起 TWS / IB Gateway，API 端口开启）
pip install "subledger[ib]"       # ib_async
export SUBLEDGER_BROKER=ib IB_PORT=4002 IB_CLIENT_ID=11 IB_PAPER=true
python -m subledger.daemon        # 或 uvicorn subledger.api:create_app --factory
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

## Interactive Brokers（`broker/ib.py`，2026-09-22）

`IBBroker` 基于 [ib_async](https://github.com/ib-api-reloaded/ib_async)，
连接本机 TWS / IB Gateway 的 API 端口（paper 4002 / 7497，live 4001 / 7496）。
ib_async 是 asyncio 库且非线程安全，而 router / reconciler / REST / daemon
会从多个线程调适配器——所以适配器自带一个私有事件循环线程，所有方法对
调用方**同步且线程安全**。

```bash
SUBLEDGER_BROKER=ib
IB_HOST=127.0.0.1  IB_PORT=4002  IB_CLIENT_ID=11   # 写者用的 client id
IB_ACCOUNT=        # 留空 = 会话的第一个账户；FA 多账户时填账户号
IB_PAPER=true      # 硬守卫：true 要求账户号 D 开头(paper)，false 要求 U 开头(真金)
IB_MARKET_DATA_TYPE=3   # 1 实时(需 API 行情订阅) 3 延迟(默认) 4 延迟冻结
IB_CASH_CURRENCY=USD    # 账本币种:只取这一种货币的现金余额(多币种账户的
                        # TotalCashValue 是按汇率折算的总额,会随汇率每几分钟漂移)
```

| subledger 概念 | IB 映射 |
|---|---|
| `broker_order_id` | **permId**（跨会话、跨 API client 稳定；orderId 是 client 私有序号，不用） |
| `client_order_id` | **orderRef**（所有 client 可见，成交回报也带 → 归属跨重启） |
| OCO（保护多头） | 两张 SELL 同一 `ocaGroup`（= client id）、`ocaType=1` |
| bracket / OTO | 父单 + 附属子单（`parentId` + transmit 链），一腿成交另一腿由 IB 撤 |
| replace | **真改单**（同 permId，返回的 id 不变；orderRef 不换） |
| TIF `cls` | MOC / LOC 订单类型；`opg` → tif OPG |
| `extended_hours` | outsideRth（盘前 04:00 起、盘后到 20:00 ET）；**夜盘**（20:00–03:50 ET）开着时，简单 DAY 限价单改路由到 IB 的 OVERNIGHT 场所——SPY/QQQ 这类标的近乎全天可交易。路由策略：**默认 SMART，只在 SMART 无法成交的时段（夜盘）直连**；直连被 Gateway 的 API 预防性设置拒绝（10329）时自动退回 SMART + outsideRth，一小时后再试直连（勾上设置后无需重启）。SMART + OVT/OND 时效虽被接受，实测仍等到 04:00 ET 才送交易所 |
| notional | 不支持（整股账本策略下由 `get_asset(fractionable=False)` 在 router 层拒绝） |
| clock | `SPY` 合约的 liquidHours（按日缓存），语义同 Alpaca |
| marks | `reqTickers`（默认延迟行情）；拿不到的用持仓市值价兜底 |
| 手续费 | 每笔成交的 commission 以 `fee` 活动上报，带 `order_client_id` → **记到所属子账户**（realized P&L），对账保持绿 |
| 分红 / 利息 | TWS API 不提供 → 表现为 cash drift，`ops absorb` |
| 推送入账 | `IBBroker.add_order_listener` / `stream.IBOrderStream`（daemon 自动接） |

**client id 规则**：IB 每个 client id 只允许一个连接。写者（daemon、动资金的
CLI 命令）固定用 `IB_CLIENT_ID`；只读视图命令自动用 `900 + pid % 100`
（`IB_READONLY_CLIENT_ID_BASE`），互不冲突。只有下单的 client（或 Gateway
配置的 master client）能改/撤自己的单——本项目里写者只有一个，天然满足。

**边界**：
- 只做美股（`Stock(sym, "SMART", "USD")`）；账户里的其它品种（期货等）在
  `get_account()` 里以 localSymbol 出现，对账会报 `unknown_position`——
  一账户一账本，别人在同一账户交易本来就该被看见
- `reqCompletedOrders` / `reqExecutions` 只覆盖当天：daemon 跨日停机期间成交
  的挂单，重启后数量可从 completed orders 补齐，但成交均价可能缺 → 表现为
  cash drift，人工处理（正常运行时推送 + 5 秒轮询都在，不会走到这条）
- `Inactive` 视为 rejected；IB 对每次撤单（含我方撤单）回 error 202，适配器
  把 ib_async 的这条 ERROR 日志降为 INFO
- ib_async 建模缺陷：改单被拒时它会把本地状态标成 Cancelled，适配器改单失败
  后会强制 `reqAllOpenOrders` 刷新真实状态
- 同一 IB 用户名同一时刻只能有一个 Gateway/TWS 会话（paper 与 live 是两套
  用户名，可各开一个 Gateway 实例，端口默认就不同：4002 / 4001）

## 执行器心跳（`executors.py`，2026-10-05）

规则：**每个 active 子账户都必须有一个在运行的执行器**。执行器就是替这个子账户下单的东西：
REST 上的独立程序（strategy-executer、hermes live4731），或 daemon 进程内托管的策略模块（trial）。

- 每个执行器**自己**报心跳：`POST /executors/heartbeat`
  `{executor_id, sub_account_id, name, kind, pid, status: ok|error, message, interval_s, expect_next_by}`；
  进程内策略由宿主 daemon 调 `Ledger.record_executor_heartbeat()` 代报。每次心跳承诺下一次的最晚时间
  （默认 now + interval_s，定时任务可直接给 `expect_next_by`）
- **统一判定**（`subledger.executors.evaluate`，纯函数，CLI / REST / watchdog 共用）：
  执行器 ok / stale（承诺的下一次心跳过了 60 s 宽限还没来）/ error（按时来了但报告上次运行失败）；
  子账户 ok / problem（任一执行器 stale 或 error）/ missing（没有执行器报心跳）/ waived（明确登记为无人值守）；
  为不存在或已停用子账户报心跳的执行器是 orphan。已停用（inactive）的子账户不检查
- `subld status`：每个子账户一列 executor，有问题标红并在表下列出；`--json` 带 `executor` 与 `executor_problems`
- `subld executors`：列出全部执行器；`retire <executor_id>` 下线一个执行器（它再报心跳就自动回来）；
  `waive <sub> <理由>` / `unwaive <sub>` 登记 / 取消"无人值守"。都是轻写，daemon 运行中可用
- 告警：trial 的 `ops/watchdog.py` 只读各账本做同一判定并推送（实盘盘中 / 开盘前 critical，其余 warn）
- 纯元数据：心跳从不影响下单、记账和对账；豁免单独一张表，daemon 的整行写不会把它冲掉

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
5. **手续费按券商活动记账，不进成交均价**：Alpaca 零佣金（SEC/TAF 卖出费以微小
   cash drift 出现，容差吸收或 `absorb_cash_drift`）；IB 每笔 commission 由
   `attribute_cash_activities` 记到所属子账户的现金与 realized P&L，持仓 avg_cost
   保持纯成交价（与 IB 自己含佣金的 avgCost 不同，对账只比数量不比成本）。
6. **单进程写入**：ledger 假设 router 是唯一写者。多进程部署时策略必须走 REST，
   不能各自开 Router 实例。
7. 洗售（wash sale）、PDT 规则、公司行动（拆股/分红再投资）不处理——拆股会表现为
   position drift，需要人工用 ledger 修正后 resume。
