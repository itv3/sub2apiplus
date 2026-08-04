# 变更集 0：Guard 状态机、reason code 与观测

> 对应方案 §8。变更集 1A 落地 observe，1C 切 enforce。

## 一、scope 与三个正交维度

Guard 挂在共享发送栈出口，第一步必须先做 `EgressScope` 判定。它不是 RouteCatalog
查询的别名：同一 `api.openai.com` 同时承载 Codex realtime 与第三方兼容层，不能按
host 闭集猜测。managed 证据只能来自受管命名空间、已登记且非 facade 的业务 binding，
或 Executor 私有签发的可信 FinalizationToken。

以下三者互不覆盖，任何一个都不能冒充另一个：

```
official_egress_guard.unknown_route_policy    = observe | enforce   （全局，只管未登记 route）
official_egress_guard.unregistered_sink_policy = observe | enforce  （全局，只管未登记 sink）

SinkEnforcementState  = legacy_observe | canary_enforce | enforced  （per-sink）
SinkGuardOverride     = observe_until(<到期时间>)                    （per-sink 临时覆盖）
```

`active/previous` 是**画像回滚**，不是 Guard 回滚，两者不得互相代用。

`SinkGuardOverride` 刻意做成独立字段而非第四个 `SinkEnforcementState` 取值：状态机里
多一个"临时 observe"态，会让"该 sink 到底验收过没有"变得不可判定。覆盖是外挂的、
带到期时间的，过期自动恢复原状态。

## 二、Guard 决策顺序

必须按此顺序，先 scope，再 route、sink：

| 顺序 | 条件 | 适用策略 |
|---|---|---|
| 0 | 不属于官方 persona 管辖域 | 记录 `out_of_scope_passthrough` 后原样放行并停止判定 |
| 1 | managed，但物理 `method + host + path + protocol` 未登记 | `UnknownRoutePolicy` |
| 2 | 物理 route 已登记，但 SinkID 缺失 / sink 不在 `SinkCatalog` / 绑定元组不符 | `UnregisteredSinkPolicy` |
| 3 | 使用 Catalog 中的权威 binding 完成 purpose/persona/backend 二次解析并完整匹配 | 该 sink 的 `SinkEnforcementState` |

请求 context 的 purpose 是待验证声明，不能参与第 1 步物理 route 选择。否则错误 purpose
会把本应由 `UnregisteredSinkPolicy=enforce` 拒绝的问题降级成可由
`UnknownRoutePolicy=observe` 放行的 `unknown_route`。正确实现必须先 `MatchPhysical`，再检查
SinkID，最后以登记 binding 做 `ResolveBinding`。

第 0 档保证 Anthropic、Gemini、支付、websearch 等共享栈流量在 1C enforce 后仍不会被
误判为 `unknown_route`。反过来，不能用纯 host 判 out-of-scope：API-Key Codex mimic
与通用 OpenAI 兼容请求可共享 host/path，只能由业务 purpose/binding 区分。

### `unclassified` 不是逃逸口

`unclassified` route **已登记**，因此走第 3 档而不是第 1 档——它绕过了
`UnknownRoutePolicy`。若不额外约束，一条 route 只要标成 `unclassified` 就能永久
停在 `legacy_observe`：既不被 unknown-route 拦，也没人催它举证。

因此附加三条硬约束：

1. `unclassified` persona 的 sink **禁止进入 `canary_enforce` 或 `enforced`**。
   身份未举证就 enforce，等于把一个不知道是什么的东西宣布为合规。
2. 独立指标 `unclassified_persona`，按 sink 分桶上报。
3. **1C 切换前该指标必须归零**——即所有 `unclassified` 必须完成举证归入某个
   persona，或被删除/停用。它不像 legacy sink 那样可以带例外进入 enforce 阶段。

当前有 4 条 `unclassified`（Agent Identity 注册、PAT whoami，各 2 条候选），
两条都是 P0 旁路，1B 修复时必须一并完成 persona 举证。

`SinkBinding` 元组 = `SinkID + RouteKey + Persona + BackendKind`。四项任一不符走第 2 档，
不得借用其他已登记 sink 的状态。

## 三、reason code

稳定、可 grep、进日志与指标。命名不随重构变化。

### 3.1 route 层

| code | 触发 |
|---|---|
| `unknown_route` | 目标 route 不在 `OfficialRouteCatalog` |
| `route_persona_mismatch` | 声明 persona 与 route 解析结果不符 |

### 3.2 sink 层

| code | 触发 |
|---|---|
| `missing_sink_id` | 请求未携带 SinkID |
| `unregistered_sink` | SinkID 不在 `SinkCatalog` |
| `sink_binding_mismatch` | 四元组任一项与登记不符 |

### 3.3 定型层

| code | 触发 |
|---|---|
| `missing_finalization_token` | 请求无定型凭证 |
| `release_digest_mismatch` | token 的 digest 与当前 Release 不符 |
| `request_modified_after_finalize` | token 的 adapter-aware 语义摘要与最终请求不符；覆盖 method/URL/Host/有序 header，明确授权 lowercase、默认 User-Agent 抑制与 WS compression offer 等 normalization plan；replayable body 覆盖内容字节，single-use stream 仅绑定 capability、长度与不可重放约束，不预读或虚构内容哈希 |
| `wrong_executor` | 该 persona 的请求未经其受控出口 |
| `wrong_backend` | backend 不在该 route 的 `AllowedBackends` |
| `conn_pool_identity_mismatch` | 连接池 key 与 profile digest 不一致 |

### 3.4 放行记录

| code | 触发 |
|---|---|
| `out_of_scope_passthrough` | 第 0 档判定为非官方 persona 管辖域，原样放行 |
| `legacy_observe_passthrough` | legacy 基线内的 sink 按原链放行 |
| `unregistered_sink_observed` | 未登记 sink 在 observe 下放行 |
| `unknown_route_observed` | 未登记 route 在 observe 下放行 |

第 0 档在 observe 与 enforce 下都记录 `out_of_scope_passthrough`；其余三项是 observe
放行记录。放行也必须留痕——没有这些记录，1C 的切换条件无从判定。

## 四、日志与指标

**日志只记**：reason code、SinkID、method、**规范化 host/path 模板**、声明 persona、
解析 persona、backend、profile digest。

**禁止记录**：query string、请求/响应 Body、Authorization、Cookie、access/refresh token、
账号密钥、conversation/session/turn 的真实 ID。

path 必须模板化（`/backend-api/files/{file_id}/uploaded`），原始 path 含文件与会话
标识，等同于泄漏用户数据。

样本有界去重（复用现有告警去重与容量上限机制），同时保留**总量**指标——去重后的样本
用于定位，总量用于判断切换条件。

指标维度：`reason_code × sink_id × persona × backend`。

### 1C 的零指标条件只对已迁移 sink 集合成立

必须按集合限定，不能写成全局：主链要到**变更集 3** 才进 Executor，在 1C 时点它必然
持续产生 `wrong_executor` 与 `missing_finalization_token`——按全局口径，1C 永远无法达成
切换条件。

| 指标 | 统计范围 | 1C 要求 |
|---|---|---|
| `unknown_route` | managed scope | 连续为零 |
| `unclassified_persona` | 全局 | **连续为零**（必须全部完成举证或删除） |
| `missing_sink_id` / `unregistered_sink` | 全局 | 连续为零 |
| `wrong_executor` | **仅 `canary_enforce`/`enforced` 集合** | 连续为零 |
| `missing_finalization_token` | **仅 `canary_enforce`/`enforced` 集合** | 连续为零 |
| `release_digest_mismatch` | 同上 | 连续为零 |

`legacy_observe` 集合的 `wrong_executor`/`missing_finalization_token` 是**预期值**，
不是违规——它们恰好是"这些 sink 还没迁"的度量。这两类指标必须按
`enforcement_state` 分桶上报，否则 legacy 的正常噪声会淹没已迁移集合的真实告警。

全部 Codex sink 的零指标要求落在**变更集 3 验收末尾**，不在 1C。

## 五、per-sink 切换条件

单个 sink 进入 `enforced` 必须同时满足（方案 §8）：

1. route / persona / backend 已登记且证据完整；
2. 已通过 Executor 产生 `FinalizationToken`，并有不可手填、可复算的 `MigrationReceipt`
   冻结 binding、Executor/issuer、adapter、wire fixture 和源码候选；
3. persona wire 测试、静态门禁、门禁自测负例全绿；
4. 该 sink 的 canary 无 `wrong_executor` / `missing_finalization_token` / `digest_mismatch`；
5. 对应低频、fallback 与错误恢复分支**已主动演练**。

第 5 条不能用"观察期内没有日志"代替。没有日志既可能是没有违规，也可能是这条路径
根本没被触发过——两者在指标上完全一样，而后者一旦上线 enforce 就是生产事故。

**必须主动演练的低频路径**（观察期内大概率不会自然触发）：

- 定时账号测试
- PAT whoami
- Agent Identity task 失效恢复
- privacy 三端点
- WS → HTTP fallback
- OAuth refresh（含刷新失败重试）
- Spark shadow 账号用量刷新

## 六、回滚

| 动作 | 粒度 | 约束 |
|---|---|---|
| `enforced → canary_enforce` | 单 sink | 常规回滚路径 |
| `SinkGuardOverride=observe_until` | 单 sink | 必须带到期时间、责任人、reason code；过期自动恢复 |
| `UnknownRoutePolicy` 的 `observe_until` 紧急覆盖 | 实例 + route 模板 | 仅覆盖明确目标并记 `unknown_route_observed`；必须带到期时间、责任人和 reason code |
| `UnregisteredSinkPolicy` 的 `observe_until` 紧急覆盖 | 实例 + SinkID/route | 仅覆盖明确绑定问题并记 `unregistered_sink_observed`；必须带到期时间、责任人和 reason code |

1C 进入 enforce 后，禁止把两个全局 policy 直接永久切回 observe；紧急放行只能走上表
的限范围、可到期覆盖。**不提供静默全局 off**。任何回滚状态下违规指标继续记录——
回滚是为了恢复可用性，不是为了让问题消失在监控里。

其他已稳定 `enforced` 的 sink 不随单个 sink 回滚而降级。

## 七、变更集 1A 的初始配置

```
unknown_route_policy      = observe
unregistered_sink_policy  = observe
所有尚未迁移且可达的受控 sink = legacy_observe
```

死代码保持 `pending_removal`，infrastructure 与 out-of-scope 保持 `not_applicable`，不能为了
套用初始配置把它们伪造为 legacy sink。1A 的验收要求“不改变现有业务结果”，因此两个
全局 policy 与尚未迁移的可达受控 sink 必须处于上述宽松态。

一个必须坚持的例外：**bootstrap commit 之后新增的裸 sink 不享受 observe**。运行时无法
区分"变更集 0 漏登记的存量"与"本次部署引入的新旁路"，所以静态门禁从首日 hard-fail
post-bootstrap 新增裸 sink。观察期发现的 pre-bootstrap 遗漏必须由 clean archive 回放
证明它在锚点提交已存在，再补齐 persona/route/backend/责任人和审核引用，才能进入受审
补录清单与必要的 provisional legacy 基线。补录项必须冻结完整候选结构、分类字段、历史
源码 blob 摘要和审核信息；静态回放验证同一事实，生产 Catalog 确定性合并同一份清单。

观察期内 legacy 基线标记为 `provisional`，允许上述受审补录；观察期结束后改为 `sealed`，
从该时点起只减不增。不得在首日提前封存，否则运行时发现的真实 bootstrap 漏项无处安放。
sealed 后的减少也不能直接删除 JSON：候选调用点消失必须有 Removal/MigrationReceipt，
legacy 清单必须是封存 ceiling 的子集，且每个移出 SinkID 都能追溯到机器收据。

Guard 记录器是旁路遥测，不是发送依赖。记录器返回错误或 panic 时保留原 Guard 决策，
只增加独立的 recorder-failure 计数，避免观察设施故障改变业务结果。

反过来说，运行时的 observe 只是过渡：若永久保留，命中已知 route 的新增普通 client 或
半旁路会失去运行时第二道防线。1C 必须把两个 policy 都切到 enforce。
