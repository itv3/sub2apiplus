# 变更集 0：`internal/officialegress` 包边界与证据契约

> 对应主方案 §二、§三「变更集 0」。本文件是当前唯一有效的包设计。
> `package-api-redesign-proposal.md` 及旧 `internal/officialegress/contract` 草案已经作废。

## 一、结论

变更集 0 不实现生产 Executor，也不修改任何请求。它先把四类事实分开，并用可编译、
可复算的 build-tag 契约证明每一层没有丢字段或凭空增加策略：

```text
官方画像快照 ─────────────→ ProfileSpec（0A，纯版本证据）
official-client registry ─→ ReleaseGraph（0B，purpose + mode 发布坐标）
sink-baseline.json ───────→ ReleaseBinding（0C，业务发送面证据）
        三者 + 显式 release purpose/mode
                    └─────→ EvidenceBundle（0D，只做一致性连接）

ExecutionPolicy + DeploymentSupportPolicy
                    └─────→ 后续变更集的可执行 ResolvedRelease
```

这条分层解决了此前九轮草案反复出现的同一个问题：从画像快照中推导它没有记录的
`purpose`、backend、重试次数或平台支持域。今后转换器只能转换数据源明确存在的信息。

## 二、0A：ProfileSpec 只承载画像证据

位置：`backend/internal/officialegress/profilecontract`。

`ProfileSpec` 与官方画像快照一一对应，包含完整 endpoint、Header/Query/Body 规则、
TLS、H1/WS 传输参数、FeatureDefaults 及五段跨端点配置。它明确不包含：

- 业务 purpose；
- backend；
- RetryPolicy；
- Transport selector；
- 部署平台/代理/CA 支持域。

硬验收如下：

1. `canonical(原始 snapshot) == canonical(ProfileSpec → snapshot)`；
2. 2451 个 JSON 叶子逐个做合法变异，2451 个都必须改变完整 SHA-256 digest；
3. 反射修改所有 getter 返回对象的 10477 个叶子，内部 digest 必须不变；
4. 严格拒绝未知字段和尾随 JSON；
5. DTO 与真实源码逐字段一致，不存在曾被凭空加入的 `BodyField.Source`。

画像快照按 `version + digest` 不可变保存：

```text
testdata/snapshots/<version>/<digest>.json
```

禁止 `latest.json`、按版本覆盖旧文件或用版本号代替 digest。

## 三、0B：ReleaseGraph 与双层枚举

位置：

- `backend/internal/officialegress/releasecontract`；
- `backend/internal/officialegress/profilecontract/enumcatalog.go`。

### 3.1 ReleaseGraph

发布图直接从现有 official-client registry 导出，不从版本字符串反推。节点主键是：

```go
type ReleaseCoordinate struct {
    Purpose string
    Mode    ReleaseMode // active | previous
}
```

当前 Codex OAuth 图包含 HTTP/WS × active/previous 四个节点。active 与 previous 都是
`0.145.0`，但 BuildID、User-Agent、wire ID 和 wire digest 不同；因此任何按版本号合并
节点的实现都会被测试拒绝。每个节点引用不可变 `SnapshotReference(version,digest)`，
且构造时复算 official-client registry 的 wire digest。

### 3.2 双层枚举

画像生成器只产出 `ObservedEnumValues`，表示所有不可变快照中实际出现过的值；
`EngineSupportedEnumValues` 由引擎维护者显式审核。门禁是：

```text
ObservedEnumValues ⊆ EngineSupportedEnumValues
```

升级画像只能扩大“观测值”，不能自动把新字符串变成“引擎已支持”。生成器从快照目录
取 active/previous 全集，previous 独有值不会因升级 active 而丢失。

## 四、0C：ReleaseBinding 只承载发送面证据

位置：`backend/internal/officialegress/bindingcontract`。

输入是扫描器生成并人工分类的 `docs/changeset0/sink-baseline.json`。转换结果按
`runtime_sink_id` 聚合，保留：

- 业务 `SinkID`、purpose、persona；
- 端点证据状态：`codex_profile`、`transport_only`、`external_persona`、`missing` 或
  `not_applicable`；
- route 原文及 method/host/path/transport 的无损拆分；
- 当前实际 backend 与目标 backend；
- EnforcementState、owner、迁移变更集和到期条件；
- 每个 factory/facade/terminal 候选的 ScanCandidateID、AST 指纹、源码位置和目标证据；
- 请求构造到发送参数之间可静态证明的 method、完整 target、host 与 path；
- 基线 SHA-256、bootstrap commit、Darwin/Linux 构建上下文、扫描包数及候选统计。

扫描器的数据流边界是有意收窄的：它追踪函数内 `NewRequest/NewRequestWithContext →
变量/WithContext → Do/RoundTrip/HTTPUpstream`，并识别 req/v3、WebSocket 与裸 Dial 的
直接 method/target；不跨 helper 或接口字段猜返回值。无法静态绑定的项保留为
dynamic/unknown，由人工 route 证据与 1A 运行时 Guard 补齐，不能用同函数任意常量代替。

当前所有 in-scope 候选均已进入 RuntimeSinkID；其余候选均有明确分类。具体数量统一由
[sink-stats.md](sink-stats.md) 从基线生成。绑定目录没有 RetryPolicy 字段，因为扫描
基线没有这项证据。

OAuth authorization-code exchange 是必须显式保留的反例：它与 refresh 共用
`POST /oauth/token`，但当前画像只有 refresh 的端点级 Header/Body 证据。因此 exchange
标记为 `transport_only`，共享 OAuth 客户端工厂单列为 facade，不能仅凭 route 相同把
两者合并。

本轮契约反向发现并修正了一条错误分类：
`ProbeOpenAIAPIKeyResponsesSupport` 在 mimic 账号上会提前返回，真实发送分支只处理
非 mimic API Key，并请求账号 baseURL 的 `/v1/responses`。它不属于 Codex OAuth 官方
端点闭集，不能登记为 `chatgpt.com/backend-api/codex/responses`，现已改为 out-of-scope。

## 五、0D：EvidenceBundle 只做证据连接

位置：`backend/internal/officialegress/compositioncontract`。

组合请求必须显式提供：

```go
type CompositionRequest struct {
    SinkID         string
    ReleasePurpose string
    Mode           ReleaseMode
}
```

业务 purpose（例如 `user_request.responses`）与 official-client 发布 purpose（例如
`openai_oauth_responses_http`）不是同一命名空间。组合器禁止依据 backend 自动猜选
发布节点。

组合器依次验证：

1. SinkID 必须存在且 persona 为 `codex-cli`；
2. 共享 facade 不能在中间层生成业务 Bundle；
3. Sink 必须明确具有 `codex_profile` 端点证据，`transport_only` 不得冒充完整画像；
4. 显式 purpose + mode 必须命中发布图；
5. 目标 backend、route transport 与发布 wire transport 一致；
6. 发布节点引用的 version + digest 快照必须存在；
7. 每个业务 route 必须在该 ProfileSpec 中唯一匹配一个 endpoint。

当前具备端点画像的 Codex 业务 Sink 全部通过连接。OAuth exchange 保持
`transport_only` 并由负例证明不能冒充 refresh；其他负例覆盖 HTTP/WS 误接、
Chrome/unclassified 误套 Codex Release、facade 生成业务身份、缺快照和缺 endpoint。

`EvidenceBundle` 不是可执行 `ResolvedRelease`。它没有 RetryPolicy、代理/CA selector、
CONNECT 画像、Credential、连接 key、Body stream 或发送 adapter；缺任何一项都不能
声称“可发送”。

## 六、后续生产包的冻结边界

0A–0D 只验证证据，但已经冻结以下 1A/2/3 不得推翻的边界。

### 6.1 依赖方向

```text
service ───────────────→ internal/officialegress
repository ────────────→ internal/officialegress 的资源/连接池 port
wiring ────────────────→ internal/officialegress/adapter/*
```

`officialegress` 不得 import `service` 或 `repository`，API 不得暴露 Gin、Account、
`*req.Client` 或 repository 领域类型。

wire adapter 是受信任组件：它物理上必须取得 method、URL、Header、Body 并操作 socket。
用一个只能 getter 的 `SealedRequest` 假装 adapter 不可信没有意义。正确约束是：

- adapter 只能位于 `internal/officialegress/adapter/*` 闭集；
- wiring 用 AdapterID/BackendKind/FinalizationToken 做一致性校验；
- 每个 adapter 有 wire 契约测试，证明没有画像外改写；
- source-to-sink 静态门禁阻止另建 client 绕过闭集。

### 6.2 Executor 是逻辑出口，不是单一客户端库

`codex-cli` persona 最终只经一个 Executor 逻辑边界，但内部可保留：

- HTTPUpstream backend；
- req-profile backend（OAuth refresh 不强制换栈）；
- WebSocket backend。

persona 与发送栈是多对多，不能按用了哪个库推断身份。

### 6.3 一次解析和显式策略

一次业务调用开始时按 purpose + mode 解析 Release，跨账号重试和 WS→HTTP 语义 fallback
复用同一发布视图；下游不得再读全局 active。换 route 时 endpoint、transport 与连接 key
可以变化，但 Release 不重解析。

以下策略必须来自单独、可审核的数据源，不能由 ProfileSpec 默认生成：

- `ExecutionPolicy`：幂等性、MaxAttempts、backoff、jitter、行为频率与副作用；
- `DeploymentSupportPolicy`：平台、代理模式、CA 状态及 CONNECT 画像选择；
- `ReleaseSelectionPolicy`：业务身份到 official-client purpose 的显式映射。

### 6.4 请求与连接契约

后续可执行类型至少遵守：

- `TransportSpec` 由 Release/部署策略选择，不允许业务 Plan 指定；
- Body 同时支持 replayable bytes 与 single-use stream，blob 上传不得强制缓存 512 MiB；
- single-use stream 自动限制为一次尝试；
- `InvocationID` 在同一次调用的 retry/rebuild 间复用，`AttemptID` 默认不进连接 key；
- 连接 key 包含已选 TransportID、动态目标、账号、代理身份、CA hash 和画像 digest；
- WHAM 三端点按画像的 ConnectionGroup 共享连接，不能无条件把 endpoint 塞进 key；
- FinalizationToken 绑定 release、sink、route、persona、endpoint、transport、adapter、
  lifecycle 和连接 key digest，adapter 发送前校验；
- redirect 默认禁止；若允许，逐跳重新做 route/host Guard。

## 七、变更集 0 明确不做

- 不创建生产 Executor、Guard 或 RoundTripper；
- 不迁移任何 sink；
- 不改变 active/previous 配置；
- 不给任何端点发明重试次数；
- 不实现 CONNECT 过渡画像或平台代理选择；
- 不删除现有 pairing 安全网；
- 不以 build-tag 契约测试通过宣称生产画像已经生效。

这些项目分别属于 1A、1B、2、3 及后续变更集。

## 八、验收命令

```bash
make check-egress-spec

cd backend
go test -race \
  ./internal/officialegress/profilecontract/ \
  ./internal/officialegress/releasecontract/ \
  ./internal/officialegress/bindingcontract/ \
  ./internal/officialegress/compositioncontract/ \
  ./cmd/egressprofiledump/ \
  ./cmd/egressreleasegraphdump/ \
  ./cmd/egressbindingdump/ \
  ./cmd/egressscan/
```

CI 还会重新导出画像快照、枚举、ReleaseGraph 和 ReleaseBinding，并逐字节比较提交的
生成物，防止测试只读取陈旧 fixture 而“假绿”。
