# 方案一：基于 Lumibot（开源框架）的多策略子账户

## 为什么选 Lumibot

调研结论（2026-08）：

| 候选 | 结论 |
|---|---|
| **Alpaca OmniSub / Broker API** | 官方原生 sub-account，但需要和 Alpaca 签 Broker API 合作协议（面向 fintech 公司），个人账户用不了 |
| **[Lumibot](https://github.com/Lumiwealth/lumibot)** | ✅ 一个 broker 连接可挂多个策略，每个策略独立 `budget` 和订单归属；原生支持 **Alpaca、IB**、Tradier 等，换券商基本不改策略代码 |
| **NautilusTrader** | 架构最强（多策略、每策略独立记账），但 Alpaca adapter 还在 [RFC 阶段](https://github.com/nautechsystems/nautilus_trader/issues/3374)，暂时接不上 |
| **QuantConnect LEAN** | 有 Alpaca 插件，但一个引擎实例跑一个算法，多策略要靠 LEAN 的组合框架，本地部署重 |
| **OpenAlgo** | 多券商路由做得好，但只支持印度券商，无子账户资金隔离 |

Lumibot 的多策略模型天然贴合"子账户"：

```
Trader
 ├─ Momentum(name="momentum", budget=30000)  ──┐
 └─ DCA(name="dca", budget=10000)             ─┼─> 同一个 Alpaca 连接 -> 真实账户
                                               ┘
```

- 每个策略独立线程、独立 `budget`（初始资金划分）、独立 cash 追踪
- 订单带策略归属，日志和统计按策略分开
- 以后接 IB：`broker = Alpaca(...)` 换成 `broker = InteractiveBrokers(...)`

## Lumibot 缺什么，本项目补什么

Lumibot 的 budget 是**软约束**（初始划分 + 各策略独立记账），它不会硬性阻止
一个策略花超预算，也没有跨策略持仓保护和对账。本项目补了两个部件：

1. **`ledger_mixin.py` — SubAccountMixin**
   - `guarded_buy()`：买单前检查本策略 cash，超预算直接拦截并告警
   - `guarded_sell()`：只允许卖本策略自己账本上的持仓（防止 A 策略卖掉 B 策略的股票）
   - `snapshot()`：每次迭代把本策略 cash/持仓写到 `state/<name>.json`
2. **`reconcile.py` — 定时对账**（cron 每 15 分钟）
   - Σ(策略持仓) vs Alpaca 实际持仓，逐 symbol 比较
   - Σ(策略市值) + 未分配基准 vs 账户 equity，漂移超 $50 报警
   - 快照陈旧检测（策略进程挂了也能发现）
   - 退出码非 0 → 接你的告警渠道（邮件/Telegram/PagerDuty）

## 使用

```bash
pip install -r requirements.txt
cp .env.example .env        # 填 Alpaca key，先 PAPER=true 跑纸盘
python main.py              # 启动全部策略
python reconcile.py         # 手动对账一次；建议加进 crontab
```

添加你自己的策略：继承 `SubAccountMixin, Strategy`，下单用
`guarded_buy/guarded_sell`，迭代末尾调 `self.snapshot()`，再到 `main.py` 里
`trader.add_strategy(YourStrategy(name="yours", broker=broker, budget=...))`。

## 局限（review 时注意）

1. **budget 隔离是记账层面的约束**，靠 mixin 拦截，不是券商级隔离。绕过
   `guarded_buy` 直接 `submit_order` 就能花超 —— 纪律靠 code review 保证。
2. **所有策略跑在一个 Python 进程里**：一个策略崩溃可能影响整个进程
   （lumibot 的策略线程有一定隔离，但如 OOM 就是全体阵亡）。
3. **多策略同持一只股票时**，券商侧是合并持仓；lumibot 按策略分账，但如果
   进程重启，lumibot 从 broker 恢复状态时对"哪些股属于哪个策略"的归属恢复
   并不完全可靠（这是这个方案最大的软肋，重启后建议立刻跑 reconcile.py 核对）。
4. 对账粒度到"策略快照 vs 账户"，没有订单级 client_order_id 归属审计
   （方案二有）。
5. lumibot 是活跃项目，API 偶有 breaking change，升级前先在纸盘验证。

这些局限正是方案二（`subledger/`）自研 router 想解决的问题 ——
两个方案对比 review 见 `docs/comparison.zh-CN.md`。
