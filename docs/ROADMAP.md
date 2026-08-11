# subledger 开发计划(平台)

状态基线:v0.4.2(见 CHANGELOG)。89 单测全绿(Python 3.9/3.12/3.14)。
部署侧(策略、运营、迁移)的计划维护在各部署自己的私有工作区,不在本仓。

## 已完成的平台里程碑

- 订单面:market/limit/stop/stop_limit/trailing_stop、OCO/bracket/OTO
  (exit-group 共享预留)、notional、extended_hours、replace(含腿级改价)
- 运行面:WebSocket 流式入账、崩溃恢复 repair()、equity 基准购买力、
  分红/费用归属、净值历史、账户校验(expected_account_id)、
  排他写者锁(flock 硬性单写者)、CLI、每日备份、WAL 低延迟
- 实测:router 自身开销 p50 0.07ms / p95 0.09ms(`python -m subledger.bench`)

## 待办(按需触发)

| 项 | 触发条件 |
|---|---|
| `halt_on_drift` 生产演练 | 任何部署转真实资金前 |
| REST 层鉴权 + SSE 事件推送 | 策略以独立进程接入时 |
| IB adapter(`BrokerAdapter` 五方法 + `orderRef` 归属) | 接入 IB 时 |
| 空头完整支持(买回腿共享现金预留、卖空记账) | 出现做空策略时 |
| 公司行动工具(拆股调账) | 遇到拆股时 |
| CI 恢复(`docs/ci-tests.yml.hold` → `.github/workflows/`) | push token 获得 workflow 权限后 |
| lint/typecheck(ruff + mypy) | 随 CI |

## 明确不做(non-goals)

- 期权/加密:资产类别级工程,超出"个人股票账户分账"定位
- 行情面:策略自接数据源,router 不做行情转发
- 回测:纯执行层,与任何回测框架并存
