# Changelog

## unreleased — 2026-10-05:执行器必须独立于 subld daemon

- 规则(owner 决定):执行器只能是 REST 上的独立进程，daemon 不再托管策略。`kind=in-process` 的心跳
  在 `subledger.executors` 里一律记为问题(子账户标红，watchdog 告警)，防止回退。195 tests

## unreleased — 2026-10-05:执行器心跳，每个子账户都要有在运行的执行器

- 起因:trial 的 4731 一直拿着 QQQ，被怀疑执行器没了；查下来执行器在跑，但谁也看不出来，
  watchdog 的手写清单只覆盖两个实盘 4757 执行器
- 新增 `executors` / `executor_waivers` 两张表(启动时自动建，旧账本原地升级);`Ledger.record_executor_heartbeat`、
  `list_executors`、`retire_executor`、`set/clear_executor_waiver`、`executor_health`
- REST:`POST /executors/heartbeat`、`GET /executors`、`DELETE /executors/{id}`
- `subledger.executors`:统一判定(ok / stale / error;子账户 ok / problem / missing / waived;orphan)
- CLI:`status` 每个子账户标上执行器，有问题标红并逐条列出;新命令 `executors [retire|waive|unwaive]`
- 纯元数据，不碰下单、记账和对账。12 个新测试。194 tests

## unreleased — 2026-10-02:IB 报价只认订阅后的新 tick;个别无数据标的不再拖满等待

- ib_async 每个合约只有一个 Ticker,线路被 LRU 挤掉后再订阅,会把**旧** Ticker 连同旧价格还回来;
  旧代码看到价格 > 0 立即返回,可能是几分钟到几小时前的价(4757 盘前扫描拿它判断突破)。
  现在每条线路记订阅时刻,只认 `ticker.timestamp` 不早于订阅时刻的价格(`_fresh_price`),断线时一并清空
- 新线路等首 tick:全部到齐或 3 s 封顶;另外,过了 1 s 宽限、半个宽限期内没有新线路出价、且未出价的
  不超过新线路的 1/10(至少 2 条)时提前结束。旧代码会为一个没数据的标的把每批 3 s 等满,
  冷启动 100 标的实测 9.62 s,贴着执行器的 10 s 超时
- 测试替身改为像 ib_async 一样复用 Ticker、给 tick 打时间戳,可设"永不出价"的标的;
  两个新测试在旧代码上失败(返回陈旧的 10;等满 5.1 s)
- 一次取价调用总时长预算 `_marks_budget` = 7 s(含连接、合约查询):盘后延时行情首 tick 要好几秒,
  两批各等满 3 s 再加合约查询会到 ~10 s,执行器超时就一个价也拿不到;现在到点带着已有的价返回。
  有新线路的调用记一行 `ib marks: 已出价/请求数, 新线路数, 耗时 (合约查询耗时)`,便于核对盘中实际耗时
- 已在订阅中的标的排在前面:100 个标的 / 90 条线路的重复调用只开 10 条新线路(原先 20 条)
- 断线时记住在用的线路,重连成功后在后台按 20 条/秒重新订阅(`_reopen_lines`):Gateway 每晚
  重启会断掉全部线路,否则次日盘前扫描要在 10 s 内新开 ~100 条线路。日志 `ib: re-opened N quote lines`。182 tests

## unreleased — 2026-10-02:IB 标的数超过线路预算时分批取价(盘前扫描 0 报价)

- 4757 执行器的盘前扫描一次要 ~100 个标的的价,超过 `MARKET_DATA_LINES`(90)。旧代码先全部订阅再读,
  前面的线路被同一次调用自己挤掉,读取时 KeyError,整个调用失败,执行器拿到空结果:
  liveIB 自 09-28 起每天 `quoted 0`,突破信号一个都看不到
- 改为每批最多 90 个:订阅、等首 tick、从本批自己的 Ticker 读价;挤线路时绝不挤本批要用的。
  实测冷启动 97/100、9.62 s(热调用 0.02 s 有一部分是复用旧 Ticker 的陈旧价,见上一条)。177 tests

## unreleased — 2026-10-02:当日未发布的券商费用不算对账偏差

- Alpaca 在成交时就从现金扣 CAT/REG/TAF 费用,但 FEE 活动每天约 20:30 ET 才发布(实测 20:23–20:39),
  于是每个有成交的交易日都有几个小时的分级现金偏差;执行器 `require_reconciliation` 因此拒绝当天后续下单
- `BrokerAdapter.fee_posting_cutoff_et` + `pending_fee_allowance()`:Alpaca = 21:00 ET,额度按当日成交估算
  (约为实测费率的 2 倍)。IB 不声明(佣金随成交回报)
- 对账器:仅现金偏差、账本比券商多、未到截止时间、自昨日截止以来有成交、且在额度内 → 记为
  `pending_fees`,报告 ok;过截止时间仍在、超额、方向相反、无成交、或有任何持仓/外来单偏差 → 照常报告
- 报告/持久化/REST `/reconcile` 带 `pending_fees`。176 tests

## unreleased — 2026-10-02:对账器不再用旧快照覆盖成交(trial NVDA 丢 131 股)

- 根因:`Reconciler.run()` 刷新市价时先 `list_positions()`,再走网络取价,最后把**整行**旧快照
  `save_position` 回去,且不持路由器的锁;取价期间 stream 线程入账的成交被覆盖(trial 4755 NVDA
  155/131/33 三笔部分成交,131 股的持仓更新丢失,现金正确)。同一写法还会让取价期间清仓的持仓"复活"
- 修复:`Ledger.booking_lock` = 账本自身的写锁,Router 用它作为路由锁,Reconciler 的三处写
  (刷新市价、分红/费用归属、absorb)都在它之下**重新读取**后只改该改的字段;网络调用留在锁外
- 新测试在旧代码上复现事故(155 != 286、319 复活),新代码通过。168 tests

## unreleased — 2026-10-02:REST 持仓视图补 reserved_qty(保护单被执行器自己撤掉的根因)

- `GET /accounts/{id}/positions` 增加 `reserved_qty`;`GET /accounts/{id}` 增加 `max_order_notional`。
  纯增量。背景:executor-kernel 的 REST 客户端用 `qty - reserved_qty` 判断 Protect 是否已布防,
  缺字段时恒为 qty,Protect 永远"进行中";次日新计划 `abandon` 过期批次时把它的 GTC OCO 当在途单撤掉。
  实际发生:liveAlpa 4757 META(09-26 周六撤单,Alpaca 09-28 开盘前生效)、liveIB 4757 PANW(10-02);
  liveIB META 09-26 那次因 IB Gateway 宕机撤单失败而幸存。in-process 宿主(trial)不受影响。164 tests

## unreleased — 2026-09-30:现金按结算精度记账 + 小额现金偏差可不急停

背景:liveAlpa 2026-09-25 06:35 PT 被 halt_on_drift 急停,账本比券商多 $0.0135 =
成交尾数 $0.0035(5 x 763.3153 记 3816.5765,Alpaca 扣 3816.58)+ 一笔成交当时就从
现金扣掉、收盘后才出现在活动记录里的 $0.01 费用(CAT 类)。急停是粘性的,5 天无人解除,
期间 Alpaca 撤掉了 4757 的 META OCO,执行器两次重挂保护都被急停拒绝。

- **`BrokerAdapter.cash_precision`**:Alpaca / IB 为 `0.01`。Router 把每张订单的
  **累计**成交额按该精度(ROUND_HALF_UP)取整后记差额,部分成交不累积舍入误差;
  现金、成本价、已实现盈亏同口径。mock 默认 None(精确),既有测试不变
- **`Reconciler(halt_min_cash_drift=...)` / `SUBLEDGER_HALT_MIN_CASH_DRIFT`**(默认不设 =
  旧行为):只有现金一种偏差且金额小于阈值时,照常报告(ok=False)但不急停;任何持仓/
  外来单偏差、或现金偏差达到阈值,照旧急停。用于吸收"费用当时扣、活动晚发布"造成的
  分级别瞬时偏差
- 已有账本里的历史尾数不追溯(在容差内)
- 162 tests(+7:结算取整、部分成交累计取整、往返盈亏=现金、09-25 事故复现/修复、阈值)

## unreleased — 2026-09-22:Interactive Brokers 适配器

- **`broker/ib.py` — `IBBroker`**(ib_async ≥ 2.0,`pip install "subledger[ib]"`,
  惰性导入):BrokerAdapter 全表面——market/limit/stop/stop_limit/trailing_stop、
  OCO(OCA 组)/bracket/OTO(附属子单)、day/gtc/ioc/fok/opg/cls(MOC/LOC)、
  extended_hours(outsideRth)、真改单(同 permId)、按 orderRef 反查(crash
  repair)、全 client 列单(对账外来单)、账户/持仓、延迟行情 marks、SPY
  liquidHours 时钟、commission → fee 活动(带 orderRef)。私有事件循环线程,
  同步线程安全;断连自动重连(指数退避);`IB_PAPER` 账户号前缀硬守卫;
  只读视图 client id 按 pid 派生,不与写者冲突
- `extended_hours` 在 IB 上覆盖夜盘:OVERNIGHT 会话(20:00–03:50 ET)开着时,
  简单 DAY 限价单路由到 OVERNIGHT 场所(会话时段取自该场所的合约详情),否则
  SMART + outsideRth。直连场所需 Gateway 的 API 预防性设置放行
  (Bypass Order Precautions for API Orders / IBC `BypassOrderPrecautions=yes`),
  否则 10329 拒单——router 记为 rejected,预留释放,无漂移(实测);
  SMART + OVT/OND 时效 IB 接受但实测仍延到 04:00 ET 才送交易所
- 现金按账本币种取值(`IB_CASH_CURRENCY`,默认 USD):IB 多币种账户的
  TotalCashValue 是折算总额,随汇率漂移(实测:paper 账户里的 HKD 余额让对账每
  几分钟红一次);改取该币种的 CashBalance / NetLiquidationByCurrency
- marks:改用**常驻行情线**(reqMktData 流式,LRU 最多 90 条,断线后惰性重订),
  合约查询并发化——此前 reqTickers 快照在延迟数据下每批 10–13 秒,executor 逐票
  取价 10 秒超时;现在首次约 3 秒、之后毫秒级
- **`BrokerCashActivity.order_client_id`** + Reconciler:带 client id 的
  fee 记到所属子账户(cash 与 realized P&L),不再进池子;mock
  `inject_cash_activity(order_client_id=)`
- **`stream.IBOrderStream`**:IB 订单事件直接 `Router.apply_stream_state`
- **`subledger.daemon`**:通用 booking daemon(repair + REST + 推送流 + 开盘
  时段净值快照 + 每日备份),`python -m subledger.daemon`;新实例不再各写一份
- **CLI `ops reconcile|halt|resume|ack|absorb|attribute`**:非排他写者,daemon
  运行中可用(把 trial.ops 的能力搬进 vendor,所有实例通用)
- **账本起始时间**:`adopt_broker_cash` 记录 `master.inception_at`(就地迁移增列),
  `attribute_cash_activities` 默认只取起始之后的活动——收编前同日的佣金/费用已在
  收编现金里,不再重复记账(IB 实测踩到:同账户别的 client 当天期货佣金被二次入账)
- `_build_stack` 识别 `SUBLEDGER_BROKER=ib`;pyproject extras `ib` / `all`
- 150 tests(+32:假 Gateway 覆盖下单/组合单/改单/撤单/状态映射/手续费/
  时钟解析/事件流;reconciler 手续费归属)
- 实测(IB Gateway 10.37 paper,账户 DU…):只读探针;挂单 → 反查 → 列单 →
  原地改单 → 撤单 → bracket 三腿 → 子腿改单 → 撤父单 全链路;
  开盘时段成交 + 佣金归属实弹待下一交易日

## unreleased — 2026-09-03:cash call(强制资金收回)

admin 从子账户收回资金的执法机制,三段式:
- **发起**:`accounts cashcall <id> <amount> [--deadline ISO] [--note]` /
  `POST /accounts/{id}/cash_call`。可用现金即刻划回池;差额登记为欠款
  (`cash_call`,`cash_call_deadline`,审计表 `cash_calls`)。CLI 在 daemon
  持锁时自动经 REST 提交(`SUBLEDGER_REST_URL` / `TRIAL_REST_PORT`)。
  默认 deadline = 下一个完整交易日收盘。
- **冻结**:欠款存续期间风控拒绝该子账户一切买入;卖出照常。
- **划扣**:`Router.enforce_cash_calls()` 每 cycle 把回笼的可用现金划入池,
  欠款归零即解冻并结案(审计 resolution=swept)。
- **强清**:deadline 过 + 市场开盘 + 未 halt → 机械清算:按市值从大到小、
  整数股(碎股尾巴整仓例外)、只撤被卖标的的挂单、市价卖到覆盖为止,
  tag `cashcall-<sym>`。**halt 优先于强清**(存疑账本上不强卖),闭市顺延。
- 账户视图(REST/CLI)含 `cash_call` / `cash_call_deadline`;`GET /cash_calls`
  审计历史。
- 语义变更:`active=False` 现在只禁止**买入**,卖出保留——停用账户必须
  仍可清仓(关账户 / 强清路径)。
- 118 tests。


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
