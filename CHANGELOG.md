# Changelog

## unreleased — 2026-08-31

- REST `_order_view` 增加 `time_in_force` 字段(账本本就存储,此前未序列化)。
  背景:liveAlpa 实例接入 hermes live4731 执行器时,其 OCO 保护判定要求
  读到 `time_in_force == "gtc"`。106 测试通过;对现有消费者为纯增量
- CLI 纯视图命令(status/positions/equity/returns/history/orders、
  accounts list/show)自动以非排他读者打开账本(`SUBLEDGER_READONLY=1`),
  daemon 持写锁时视图不再 LedgerLocked;动资金命令保持排他写者路径
  (锁挡住即护栏)。背景:liveAlpa 起 daemon 后 vendor CLI 视图全挂
  ——trial 有 trial.cli 绕过,首个"vendor CLI + 常驻 daemon"组合暴露此缺口

## v0.4.2 — 2026-08-10

硬性单写者。

- **Ledger 排他写者锁(flock)**:file 账本默认在 `<db>.lock` 上持有
  OS 级排他锁直至进程退出;第二个写者进程在构造时抛 `LedgerLocked`
  (带持锁 pid 与指引)。`exclusive_writer=False` 供只读/轻操作工具
  (状态查询、halt/ack/absorb)与写者共存;`:memory:` 不加锁。
  背景:2026-08-10 一个绕过 daemon 的测试进程与 daemon 并发入账,
  持仓行发生丢失更新(少记 1 股)——纪律升级为物理约束
- trial:`build_alpaca_context(write=)` 接线;cycle.py 补 daemon 互斥自检;
  CLI 动资金命令持写锁、读命令免锁

## v0.4.1 — 2026-08-09

净值历史与延迟。

- **净值历史**:`equity_snapshots` 表 + `Router.snapshot_equity_history()`
  (trial daemon 开盘时段每分钟一行:先 refresh_marks 刷现价再快照)+ `ledger.equity_history()` 查询
  + CLI `equity [--sub] [--since] [--limit]`(`--json` 可出时间序列)
- **延迟**:实测并优化下单热路径。SQLite 切 WAL + synchronous=NORMAL;
  `Router(eager_sync=False)` 供流式模式省掉提交后的确认查询(成交由
  WebSocket 毫秒级入账);place_order 日志带 router/broker 分段计时。
  基准工具 `python -m subledger.bench`:**router 自身开销 p50 0.07ms /
  p95 0.09ms,流式入账 p50 0.04ms**(盘面 SQLite,300 单)——相对券商
  REST ~100–250ms 往返可忽略
- 测试:78 → 80

## v0.4.0 — 2026-08-08

保证金模型、CLI、现金活动归属。

- **equity 基准购买力**:`bp = equity × multiplier − gross_exposure −
  reserved`(equity = cash + 多头市值)。亏损自动收缩额度、盈利扩张,
  名义倍数可用满(旧现金基准公式下 1.5x 账户实际只能做到 ~1.125x);
  multiplier=1 时精确退化为现金账户规则。router 在每次买入/改单检查时
  注入该子账户的持仓市值。仍不建模维持保证金/利息(券商侧兜底)
- **CLI**(`python -m subledger.cli` / `subledger`):accounts
  list/create/update/allocate/delete、positions、history(成交史,
  按子账户/标的/日期过滤)、orders;`--json` 结构化输出;错误以
  `{"error": ...}` 返回。资金划转仅限自由现金(持仓市值、挂单预留、
  保证金负债均不可动);`update` 走 settings-only 列更新,与成交入账
  并发安全;`delete` 要求已清仓,现金归还未分配池
- **分红/费用归属**:`Reconciler.attribute_cash_activities()` — 带
  symbol 的分红按持仓比例记入持有者(计入 realized PnL),无法归属的
  进未分配池;按 activity id 幂等。Alpaca `/v2/account/activities`
  裸签名请求实现(alpaca-py 无封装)+ mock 注入
- **`Ledger.backup(path)`**:SQLite 在线一致性快照(供每日备份)
- 测试:59 → 78

## v0.3.0 — 2026-08-07

运行面与健壮性。

- **TimeInForce**: 新增 `opg` / `cls`(开、收盘竞价)与 `fok`
- **腿级改单**: OCO/bracket 退出腿支持价格 replace(收紧止损/移动止盈
  无"撤单重挂"的无保护窗口);券商侧配对关系保持
- **`BrokerAdapter.get_clock()` / `get_asset()`**: 开闭市状态、标的元数据;
  router 下单前自动做 tradable/fractionable 防呆
- **`Router.repair()`**: 启动崩溃恢复——对"已预留但没拿到 broker id"的
  孤儿单,按 client id 向券商核实:提交过则收养,没提交则释放预留
- **`subledger.stream.TradeUpdateStream`**: Alpaca trade_updates WebSocket
  推送直接入账(事件自带累计成交状态,零额外 API 调用);轮询 `sync()`
  退居安全网;delta 记账保证双路投递无副作用;断线自动重连
- **Alpaca adapter**: 瞬时错误(429/5xx/断连)指数退避重试;订单历史分页
  (突破单页 500 上限)

实测:盘后真实回合 submit→成交事件→入账 755–854ms(记账环节 <10ms)。

## v0.2.0 — 2026-08-07

订单面补全(此前仅 market/limit)。

- **订单类型**: `stop` / `stop_limit` / `trailing_stop`
- **组合单**: `oco`(止盈+止损保护已有多头)/ `bracket` / `oto`,
  退出腿共享同一份股份预留(exit-group 记账),一腿成交另一腿自动取消
- **notional** 按金额下单(仅限简单市价买;卖侧会绕过持仓校验故禁止)
- **extended_hours**(仅简单 DAY limit,盘前盘后)
- **`Router.replace_order()`**: 改价/改量,预留原子调整,接管券商新订单 id
- **确定性 client id**: `client_tag` → `sl.<sub>.<tag>` +
  `ledger.get_order_by_client_id`(幂等提交)
- **Reconciler**: 券商代生成的 OCO/bracket 子腿按账本 broker id 归属,
  不再误报外来单;新增 `acknowledged_foreign` 表 +
  `acknowledge_foreign_orders()`(人工确认的手动交易不再永久报警)
- **Ledger**: 现有 SQLite 无损就地迁移(ALTER TABLE 增列)

测试:22 → 59 个,全绿。

## v0.1.0 — 2026-08-07

初版:虚拟子账户 router(market/limit、资金/卖出隔离、预留记账、
对账器、SQLite 账本、FastAPI REST 层、MockBroker、22 测试)。
