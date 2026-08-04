# 变更集 1C 验收记录

## 范围

本变更集只提升变更集 1B 已迁移的四个 Sink，不提前强制尚未迁移的主链：

- `codex.admin_test.compact`
- `codex.admin_test.responses`
- `codex.alpha_search.pat_fallback`
- `codex.usage.probe`

四个 Sink 均保留 canary 前序收据、观察记录和 acceptance 产物，再由收据链提升为
`enforced`。其余已登记主链继续保持 `legacy_observe`。

## 主动演练

[`active-exercises.json`](active-exercises.json) 固化定时账号测试、PAT whoami、Agent task
恢复、privacy、WS/HTTP fallback、OAuth refresh 与 Spark shadow 的主动演练证据。DMIT 没有
active PAT 或适合安全制造流量的 Spark shadow 时，按方案使用确定性主动演练，不伪造线上流量。

## 运行时回滚

`gateway.official_egress_guard.sink_controls_json` 是严格 JSON 输入，只允许：

1. 单个已验收 `enforced` Sink 临时降为 `canary_enforce`；
2. 设置同时具有截止时间、责任人和 reason code 的 `observe_until` 覆盖。

存在 `canary_enforce` 控制时，`canary_percent` 必须为 `1..100`；生产 wiring 和 Guard
构造层都会拒绝 0%。需要全量 observe 时只能使用独立的限时覆盖。

配置变更后重启同一镜像即可生效。控制清单不能升级 legacy Sink、不能增加 SinkID、不能改写
MigrationReceipt；连接池 enforcement identity 会随控制边界变化，避免旧连接跨状态复用。

`gateway.official_egress_guard.policy_overrides_json` schema 2 只用于 unknown/unregistered
策略的紧急 observe。每项必须绑定当前 `instance_id`、精确 method/host/path/protocol、具体
SinkID（缺失 SinkID 使用保留值 `@missing`）、截止时间、责任人和 reason code。unknown
route 还必须绑定最终 `EscapedPath` 的小写 SHA-256；安全 path 模板仅供日志使用，不能让同一
命名空间的兄弟未知路径共享覆盖。生产基础策略只能保持 `enforce`，不能再通过配置永久全局
回退到 observe。

## 运行时策略

DMIT 当前最终态同时使用：

- `UnknownRoutePolicy=enforce`
- `UnregisteredSinkPolicy=enforce`
- `instance_id=DMIT`
- 空的 `sink_controls_json`
- 空的 `policy_overrides_json`

同镜像回滚和限时覆盖演练完成后必须恢复空控制清单，PostgreSQL 与 Redis 不参与重启。

## 审核状态

[`verification.json`](verification.json) 与 [`guard-events.json`](guard-events.json) 固化 1C
整改后的观察窗口、DMIT 镜像、Guard 事件和回滚行为。当前状态为 Codex 技术复审完成、等待
用户最终验收；在用户明确验收前不得写成用户已批准。
