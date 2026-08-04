# 变更集 1A：Guard observe 接入与过渡执行核心

## 一、范围与初始状态

- 四类 terminal 栈统一接入 Guard：共享 `net/http`、`HTTPUpstream`、`req/v3`、
  WebSocket 握手。
- Guard 的第 0 档先判 `EgressScope`。非官方 persona 管辖流量记录
  `out_of_scope_passthrough` 后原样放行，不进入 unknown-route 判定。
- 运行时 `LegacyManifest` 精确包含 27 个可达业务 SinkID，全部为 `legacy_observe`。
  Evidence Catalog 另保留 3 个 facade、2 个 dead-code 和 2 个受审 scope-exclusion：facade、
  dead-code 及 API-Key-only 历史误分类都禁止成为 binding key；总证据条目仍为 34。
- 初始 Catalog 没有任何 `MigrationReceipt`，因此机器断言禁止任何可达 SinkID
  进入 `canary_enforce` 或 `enforced`。

## 二、发送栈边界

Executor 在适配前签发绑定“语义摘要 + WireNormalizationPlan”的私有 Token；Guard 挂在
最终 wire normalization 与 socket 之间：

```text
业务调用点绑定 SinkID
  → 共享 facade/backend
  → header casing / WebSocket 握手定型
  → Guard
  → socket
```

`HTTPUpstream` 的 lowercase wrapper 位于 Guard 外层，使 Guard 看到最终 header casing；
WebSocket 的现有 `officialEgressWebSocketRoundTripper` 同样先完成握手头定型，再交给内层
Guard。摘要只允许计划内的 header lowercase、Go 默认 User-Agent 抑制哨兵和 Codex WS
compression offer 变换；其他修改仍以 `request_modified_after_finalize` 拒绝。

连接池和 dialer 不生成 facade ID。每次 Acquire 都按当前 invocation 执行 admission Guard，
校验 Token 的 Sink、route、persona、backend、authority、issuer 和 InvocationID；只有新拨号的
terminal Guard 再校验握手 wire 摘要。旧式无 Token 后台预热只允许 `legacy_observe` Sink；
`canary_enforce/enforced` 在调度层和 worker 拨号层双重 fail-close，只能由携带当前 Token 的
显式 Acquire 创建连接。连接兼容 key 同时包含 SinkBinding、状态和迁移收据的 enforcement
identity，既禁止跨 Sink/状态复用，也禁止 canary invocation 借用未经过终端验证的预热连接。

共享 `net/http` Guard 无条件挂在 pool transport 上，而不是只挂
`validatedTransport`：usage probe、PAT whoami 与 Agent task register 都通过
`httpclient.GetClient`，但不会启用后者。

## 三、RouteCatalog 与 ReleaseGraph

RouteCatalog 以“物理 method/host/path/protocol → 已登记 SinkBinding → 完整 purpose/persona/
backend”两阶段解析，绝不先信任请求 context 中的 purpose。画像闭集共 16 条业务 route；
受审 route amendment 把此前只有画像、未连接业务 Sink 的 Responses `/compact` 纳入绑定，
全量 route 共同映射到 ReleaseGraph 已有的 2 个 registry purpose：
`openai_oauth_responses_http` 与 `openai_oauth_responses_ws`，各保留 active/previous，合计
4 个 release 节点。选择层使用更清晰的 family 名 `openai_oauth_http/ws`，不制造 16 个
虚构发布 purpose。

ReleaseGraph 的 `wire.endpoint=responses` 表示发布族的 wire 身份，不参与业务 endpoint
相等比较。models、files、images、WHAM、OAuth、realtime 等 endpoint 的唯一性由
ProfileSpec 与 RouteCatalog 共同承担；过渡 Provider 的测试显式覆盖 models route 映射。

只有 `endpoint_evidence=codex_profile` 能生成 EndpointID/ReleaseSelection；OAuth code
exchange 的 `transport_only` 只登记物理 route，永久禁止组合成 refresh Body 契约或进入
canary。API-Key mimic/自定义 base URL 不绑定 OAuth SinkID；管理端 Responses/Compact 也
只在 OAuth 分支绑定，API-Key-only Chat Completions 与 Keeper 通过受审 scope amendment
退出运行时 Catalog。OAuth Responses 仅允许根路由和已举证的 `/compact`，其他资源子路径
在业务层发送前明确拒绝。

## 四、bootstrap 与 legacy 生命周期

- 锚点提交固定为 `38a9929eac35a39c86de2f27de8f7a805d7dae52`。
- `bootstrap-inventory-lock.json` 同时冻结 commit tree、历史 inventory 原文摘要与当前受审
  扫描算法摘要；`make check-egress-bootstrap-replay` 使用该扫描器扫描锚点的 clean archive，
  不读取当前脏工作区。
- post-bootstrap 新增裸 sink 立即失败；变更集 1A 新增的 Guard terminal delegation
  只能进入精确的基础设施闭集，不能取得 legacy 状态。
- pre-bootstrap 遗漏只能写入 `pre-bootstrap-supplements.json`。每份收据冻结完整候选
  （file/function/callee、AST 指纹、persona/route/backend/owner）、历史源码 blob 摘要和
  审核信息；clean archive 回放验证结构与 blob，生产 Catalog 确定性合并同一份嵌入清单。
- `ImmutableBootstrapInventory`、`CurrentCandidateInventory`、`SinkCatalog`、
  `LegacyManifest`、`SupplementReceipt` 与 `Removal/MigrationReceipt` 独立校验。候选消失
  只有携带冻结原候选的 `removal-receipts.json` 才合法；迁移移除必须引用真实存在、覆盖
  对应 candidate/replacement Sink 的 MigrationReceipt，陈旧、未知或虚构摘要直接失败。
- `catalog-amendments.json` 的 source candidate 与历史源码 blob 在 clean bootstrap archive
  中重新匹配和计算，不能只填一个格式合法的 SHA。
- `legacy-baseline.json` 与 `legacy-ceiling.json` 在观察期为 `provisional`，允许受审补录；
  观察期结束后同步改为 `sealed` 并冻结 `sealed_at`。此后当前 legacy 必须是 ceiling 子集，
  每次减少都必须有 MigrationReceipt/RemovalReceipt，新增立即失败。
- `legacy-seal-receipt.json` 在正式封存时冻结 ceiling 与 supplements 原文 SHA-256、bootstrap
  commit/tree、实际受保护 base commit、`sealed_at` 和审核信息。当前仍为 provisional，
  不提前生成虚假封存摘要。
  sealed 后本地门禁先复算两份摘要，CI 再用 GitHub 事件提供的受保护目标分支 commit 作为
  工作区外信任锚：基准一旦 sealed，receipt、ceiling 和 supplements 必须逐字节不变；缺少
  `EGRESS_SEAL_BASE_REF` 直接失败。Git object ID 使用统一判据，兼容 SHA-1 的 40 位和
  SHA-256 的 64 位完整小写 ID；首次 sealed receipt 必须精确绑定 CLI 实际解析出的 base ID。
  seal 门禁同时读取当前与受保护版本的 `legacy-baseline.json`，强制当前集合是受保护集合的
  子集；因此 baseline 能凭迁移/移除收据继续减少，但已删除 SinkID 永远不能重新加入。

`MigrationReceipt` v2 按 persona 区分 authority：`codex-cli` 必须使用
`codex_profile + codex_executor`，`chatgpt-web` 必须使用
`external_persona + chatgpt_web_client`，`transport_only/unclassified` 永久禁止升级。收据为
binding 的每条 route 分别冻结 authority/TokenIssuer、Adapter/Transport、画像证据、实际
wire fixture 路径和执行验证产物路径；运行时和 CI 都读取文件并重算 SHA，再把实际 Token
身份与 route claim 逐项比较。`enforced` 收据必须引用同 Sink 的 canary 收据摘要及实际
canary acceptance 产物，禁止直接升级。其构造字段不向业务调用者导出，替代可手填的
“已通过 Executor”枚举。

Executor Body 同时支持 replayable bytes 与 single-use stream；后者不预读、不复制，只绑定
私有 capability/长度，且强制一次 attempt、不可重放。RequestCompiler 必须原样交还同一个
私有 capability，同长度的替换 stream 也会被拒绝。

## 五、验收门禁

1. 四类发送栈各有 before/after 对照，覆盖 method、URL、body、header、错误链、取消/
   deadline、redirect 和响应生命周期；HTTPUpstream 另覆盖解压、inFlight 与 Body.Close。
2. Anthropic、Gemini、支付、websearch 在接入前后逐字节一致，且不产生
   `unknown_route`。
3. facade 运行时绑定、未知 route、错误 purpose、缺失/未知/错配 SinkID、错误 backend、
   缺凭证、定型后修改、WS 跨 Sink/同 Sink 无 Token 复用、虚构或缺 route 收据、直接
   enforced，以及 single-use 二次消费/同长度 capability 偷换均有负例；canary Sink 在
   `MinIdlePerAccount > 0` 时也不得让无 Token 预热连接进入池。
4. 日志与指标只含安全字段，不记录 query、body、Authorization、Cookie 或 token；记录器
   返回错误或 panic 不改变发送决策，并由独立故障计数暴露。
5. 封存门禁包含 coordinated-rewrite 反例：同时重算当前 receipt、ceiling 和 supplements
   仍会因偏离受保护 sealed base 而失败。
6. 首次封存使用仓库真实 40 位 base SHA 完成正例；受保护 baseline 从 `[A,B]` 减为 `[B]`
   后，当前重新加入 A 必须失败，而继续减少仍可进入 MigrationReceipt/RemovalReceipt 校验。
7. `go test -race ./internal/officialegress/...`、相关发送栈测试、`make check-egress-spec` 与
   全量生产构建全部通过。
