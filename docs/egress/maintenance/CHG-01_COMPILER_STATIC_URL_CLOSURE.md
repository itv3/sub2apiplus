# CHG-01：Compiler 静态 URL 封闭 — 实施方案

> 状态：已完成（2026-08-06，两轮三方复审后审核通过；实施、测试与复审记录见 §10）
> 基线：`9c7adb8eb3`
> 清单：[OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md](OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md)（已按完成规则移出当前变更集表）

## 1. 目标与定性

`validateCompilerTarget` 的静态分支从「原样克隆调用方 URL」改为「以当前 Bundle 画像为权威，
校验调用方 URL 后 fail-close」：scheme、authority 和 path 使用规范形态封闭；query 封闭键集合、
必需性、单值性、来源和值语义，并拒绝空 query 标记与空 component。

同时把 `ForceQuery` 纳入最终请求摘要，保证签发后新增或删除裸 `?` 会被 Guard 判定为终态篡改。
完成后，偏离上述契约的静态 URL 不会产生 `CompiledExecution`，Executor 不会为其签发
`FinalizationToken` 或调用 adapter。

定性：兑现规格已承诺的 Compiler/Executor「active strict wire」URL 权威边界，不是当前故障
修复。现存生产调用方均使用画像生成函数或受控常量，当前未发现公网输入可直接构造该失败链，
本方案不将其描述为可从公网利用的漏洞。

## 2. 已验证的现状事实

| # | 事实 | 位置 |
|---|---|---|
| 1 | `validateCompilerTarget` 的静态分支只拒绝误提交的 `ReturnedURL`，随后原样克隆调用方 URL | `compiler.go:376-389` |
| 2 | `ResolveEndpointPlan` 只匹配 sink、method、protocol、规范化 hostname 和 `EscapedPath()`；不匹配 scheme、显式端口、query、fragment 或 `ForceQuery` | `bundle.go:193-213`、`route_catalog.go:160-171,289-319` |
| 3 | path 形态错误通常已在 Plan 解析阶段失败；userinfo 会在后续 `normalizeValidatedAuthority` 失败，但该函数接受 `http`/`ws` 和任意合法显式端口，也不检查 query、fragment 或 `ForceQuery` | `bundle.go:264-287`、`compiler.go:1115-1129` |
| 4 | Guard 以 Executor 签发时的 `requestDigest` 为基准，只能发现摘要覆盖字段在签发后的变化；当前摘要包含 `RawQuery`，但未包含 `ForceQuery` | `guard.go:479-497`、`types.go:1102-1120` |
| 5 | 静态校验所需的 endpoint Host/Path/Query、route Protocol 等事实均已冻结在 `ResolvedEndpointPlan.template` 内，包内可直接读取 | `bundle.go:66-92` |
| 6 | 生产 URL 来源已盘点：辅助端点使用 `buildOfficialCodexEndpointURL`，Responses 主路径及受控变体使用 `chatgptCodexURL` 和已收紧的 suffix；OAuth WebSocket 构造侧在进入 Compiler 前生成 `wss` | `official_egress_codex_engine.go`、`openai_ws_forwarder_payload.go`、`openai_gateway_request_body.go` |

## 3. 静态端点 URL 形态全集

当前 0.145.0 两份 snapshot 一致，共 16 个端点：15 个静态端点和 1 个 ReturnedURL 动态端点。

| 端点 | Host | Path | Query | Protocol |
|---|---|---|---|---|
| responses_http / responses_compact / alpha_search / images_generations / images_edits / wham_usage / wham_rate_limit_reset_credits(+consume) / files_create | `chatgpt.com` | 固定字面 | 无 | HTTP |
| models | `chatgpt.com` | 固定字面 | `client_version[constant,required]` | HTTP |
| realtime_calls | `chatgpt.com` | 固定字面 | `intent[constant,required]` + `architecture[constant,required]` | HTTP |
| responses_ws | `chatgpt.com` | 固定字面 | 无 | WebSocket |
| realtime_sideband | `api.openai.com` | 固定字面 | `intent[constant,required]` + `call_id[server_response,required]` | WebSocket |
| oauth_refresh | `auth.openai.com` | 固定字面 | 无 | HTTP |
| files_uploaded | `chatgpt.com` | `/backend-api/files/{file_id}/uploaded` | 无 | HTTP |
| files_blob_upload | `*.oaiusercontent.com` | `{server_returned_path}` | `*[server_response]` | HTTP／动态 |

`files_blob_upload` 的 `HostFromResponse=true`，继续使用既有 ReturnedURL 验证模型，不进入本方案
的静态 URL 校验。

## 4. 验证与摘要规则

### 4.1 静态 URL 结构

在 `validateCompilerTarget` 静态分支调用新增的包内私有函数：

```go
validateStaticCompilerTarget(template EndpointPlanTemplate, target *url.URL) error
```

规则以 `template.endpoint` 和 `template.route.Protocol` 为当前 Bundle 权威：

| 成分 | 规则 |
|---|---|
| Opaque / userinfo / fragment | `Opaque != ""`、`User != nil`、`Fragment != ""` 或 `RawFragment != ""` 一律拒绝 |
| ForceQuery | `ForceQuery == true` 一律拒绝 |
| scheme | HTTP 只接受精确小写 `https`；WebSocket 只接受精确小写 `wss`；不在 Compiler 输入侧使用 `EqualFold` 或 `canonicalRequestScheme` 放宽 |
| 端口 | `target.Port() != ""` 一律拒绝，包括 `:443` |
| host | `target.Host` 与 `endpoint.Host` 逐字相等，不做大小写、尾点或端口归一 |
| path | `target.EscapedPath()` 与 `endpoint.Path` 按模板段匹配：段数相等、字面段逐字相等、`{param}` 段非空 |
| query | 按 §4.2 做结构化语义封闭；合法 RawQuery 原表示保留，不在本变更集 canonical 化 |

`canonicalRequestScheme` 只继续承担签发后的受信 WebSocket adapter `wss → https`、`ws → http`
摘要等价，不参与 Compiler 输入合法性判断。

### 4.2 静态 query

先验证画像 query 定义：

1. 静态 endpoint 禁止空名称、`*` 或重复名称；
2. 当前静态 Compiler 只支持 `constant` 和 `server_response` 两种 query source，其他 source
   在新增明确执行语义前 fail-close；
3. `constant + required` 的画像值不得为空。

再验证调用方 `RawQuery`：

1. 画像 `Query` 为空时，要求 `RawQuery == ""` 且 `ForceQuery == false`；
2. `RawQuery != ""` 时按 `&` 切分，前导 `&`、尾随 `&` 或连续 `&&` 形成的空 component
   一律拒绝；
3. `url.ParseQuery(target.RawQuery)` 失败即拒绝；
4. 任一解码后的键出现多个值即拒绝；
5. 调用方键集合必须是画像 query 名称闭集的子集；
6. `Source == constant`：出现时，解码后的值必须与画像 `Value` 逐字相等；
7. `Source == server_response`：出现时值必须非空，且必须与
   `EndpointDynamicInputs.ServerResponseQuery` 提交的可信输入逐字相等；缺少可信输入即拒绝
   （复审 P1-1：仅“非空”不构成来源与值语义封闭）；
8. 任一 `Required` 键缺失即拒绝。

`ServerResponseQuery` 是静态端点 server_response query 的唯一可信通道：键必须落在画像
`server_response` 名称闭集内，值必须来自受信服务器响应链（realtime sideband 由
`dialLiveSideband` 提交 `record.CallID`），不得取自调用方 URL 本身。ReturnedURL 动态端点的
query 权威仍是服务器返回 URL 整体，提交 `ServerResponseQuery` 一律 fail-close。

query 的键顺序与合法等价转义不在本变更集做逐字节唯一化。Compiler 保留通过验证的原始
`RawQuery`；生产 wire 的唯一生成形态继续由 `buildOfficialCodexEndpointURL` 按画像声明顺序和
`url.QueryEscape` 产生。空 component 不属于合法等价表示。

### 4.3 最终请求摘要

修改 `requestDigest`，在现有 method、canonical scheme、Host、EscapedPath、RawQuery 等字段之外，
显式绑定 `req.URL.ForceQuery` 布尔值。签发后仅改变 `ForceQuery` 时摘要必须变化；现有 WebSocket
受信 adapter 的 `wss/https` 等价仍保持不变。

### 4.4 拒绝语义

- `Compiler.Compile` 返回 error 和零值 `CompiledExecution`；
- `Executor.Prepare` 不返回可消费的 `PreparedRequest`，因此不产生 `FinalizationToken`；
- `Executor.Execute` 在 `compiler.compile` 阶段返回 `official_egress.compiler.rejected`，adapter/port
  调用次数保持为零。

## 5. 实施步骤

### 第一步：前置验证

1. 在实施起点记录 HEAD；若已不是本文基线，先确认 §2 涉及的生产文件和 capture 资产差异；
2. 运行 changeset6 生成器，确认 56 个 capture 与冻结 manifest 严格比较通过；
3. 记录新生成 manifest、secret-scan 和 receipt 摘要；冻结 receipt 只作历史资产校验，不要求
   与当前生成器因目录迁移产生的新 receipt 字节一致；
4. 逐端点确认 capture target 满足精确 scheme、host、port、path、`ForceQuery` 和 query 空
   component 规则；不相容项必须在编码前定位为生产规则错误或测试构造错误。

### 第二步：编码

1. `backend/internal/officialegress/compiler.go`
   - 新增 `validateStaticCompilerTarget` 和静态 query 私有校验；
   - 静态分支接入；动态 ReturnedURL 分支既有验证模型不改，仅对新增的
     `ServerResponseQuery` 可信通道 fail-close；
   - `EndpointDynamicInputs` 扩展 `ServerResponseQuery map[string]string` 可信通道
     （复审 P1-1 要求，推翻本节初版“不修改 EndpointDynamicInputs”的约束）；
   - `validateCompilerTarget` 在入口统一执行 `dynamic.clone()`，所有分支只读取
     克隆值，可信事实与调用方可变 map 解除别名（复审 P2）。
2. `backend/internal/officialegress/types.go`
   - `requestDigest` 显式绑定 `ForceQuery`；
   - 保持现有 WebSocket scheme normalization。
3. service 可信值接缝（复审 P1-1）
   - `openai_ws_pool.go`：`openAIWSAcquireRequest` 增加 `ServerResponseQuery` 字段，
     `cloneOpenAIWSAcquireRequest` 对其深拷贝，`lastAcquire` 等池状态持有完整快照
     （复审 P2）；
   - `official_egress_websocket_invocation.go`：`executeAcquire` 把该字段作为
     `EndpointDynamicInputs` 传给 `ExecuteAttempt`；
   - `openai_live.go`：`dialLiveSideband` 提交 `{"call_id": record.CallID}`；
   - 不修改生产 service URL builder、Guard 策略或 adapter 行为。
4. 测试文件
   - 新增或扩展 officialegress 的 Compiler、Executor、摘要测试；
   - 将 `executor_invocation_test.go`、`openai_ws_pool_test.go` 等实际提交给 WS Compiler/Executor
     的测试 target 从 `https` 修正为 `wss`；只验证路由解析或签发后 adapter 形态的测试不改；
   - changeset3 capture 构造为静态 server_response query 同步生成可信动态事实。
5. 不新增导出符号，不修改 `ResolveEndpointPlan` 或 ReturnedURL 产品语义。

## 6. 测试矩阵

| 层 | 用例 | 断言 |
|---|---|---|
| 静态 helper | 全部结构负例：Opaque、userinfo、Fragment/RawFragment、ForceQuery、HTTP 的 `http`、WS 的 `https/ws/http`、`:443/:8443`、host 大小写/尾点、path 段增删/字面改写/空参数段 | 每项命中新 helper 的明确拒绝路径 |
| 静态 helper | query 负例：画像外键、constant 改写、required 缺失、重复键、解析失败、空画像配 RawQuery、前导/尾随 `&`、连续 `&&`、静态 `*`、重复/空画像名、未知 source | 全部拒绝 |
| 静态 helper | server_response 可信值负例：值为空、非空但与可信输入不一致、缺少可信输入、可信输入含画像外键；动态 ReturnedURL 端点提交 `ServerResponseQuery` | 全部拒绝；合法 ReturnedURL 对照行为不变 |
| Compiler | Active/Previous 下全部 15 个静态端点按画像构造合法 URL；WS 只用 `wss` | `Compile` 成功；`Scheme/Host/EscapedPath/RawQuery/ForceQuery` 与合法输入一致 |
| Compiler | query 合法等价表示：键顺序不同、合法转义不同 | 结构语义合法时成功，并保留调用方 RawQuery |
| Compiler | `files_uploaded` 的 `{file_id}` 分别包含 `/`、空格、`%`，使用合法 PathEscape/RawPath 表示 | escaped 参数保持单个非空模板段并成功编译 |
| Executor | 对各 URL 成分类别提交代表性非法 Plan（含非空改写与缺可信输入的 `call_id`），使用真实 `Executor + NewCompiler() + counting adapter/port` | 返回 `compiler_rejected`；无 PreparedRequest/Token；adapter/port 调用为 0 |
| 摘要/Guard | 仅 `ForceQuery` 不同的两个请求；已签发 `wss` 请求经受信 adapter 转成 `https` | 前者摘要不同；后者摘要等价且 Guard 继续允许 |
| 动态端点 | 合法与非法 ReturnedURL 既有矩阵 | 行为与改动前一致 |
| service | 现有各端点真实构造函数到 `invocation.Execute` 的集成测试 | 全绿，证明生产 URL 构造路径未被误伤 |
| final-wire | changeset6 pre/post，Active/Previous 共 56 个 capture | manifest 和 secret-scan 零差异；合法 target 全部通过；允许列表为空 |

路径、userinfo 等当前可能在新 helper 前后已有其他拒绝点；helper 直接单测负责证明新增规则本身，
Compiler/Executor 测试负责证明整体控制流和签发边界。

## 7. 验收门禁

在 `backend/` 目录运行：

```bash
go test ./internal/officialegress/... ./internal/service/... -count=1
```

在仓库根目录运行：

```bash
make check-egress-spec
```

改动前、改动后分别在 `backend/` 目录生成临时证据：

```bash
CHANGESET6_POST_FINAL_WIRE_OUTPUT=<绝对临时目录> go test ./internal/service -run '^TestGenerateChangeset6PostFinalWire$' -count=1
```

比较规则：

1. pre/post 新生成的 manifest、secret-scan 逐字节一致；
2. pre/post receipt 可逐字节比较，但不要求新生成 receipt 与历史冻结 receipt 一致；
3. manifest 与冻结 changeset5 post manifest 使用既有严格比较器和空允许列表；
4. 任一 capture、route 数量或摘要异常即停止本变更集，单独排查。

changeset6 捕获链真实经过：

```text
invocation.Execute
→ Executor
→ Compiler.Compile
→ validateCompilerTarget
```

因此它用于证明 56 个合法 target 在新增校验下仍能执行，并证明 capture 已记录的 host、path、
connection、Header、Body、TLS 等 final-wire 字段不变。capture 不记录 `RawQuery` 或 `ForceQuery`，
这两个字段由 §6 的 Compiler URL tuple 和摘要专项测试承担验收，不以 final-wire manifest 替代。

## 8. 非目标

- 不修改 ReturnedURL 动态端点模型；SAS 上传 URL 对 port/query/fragment 的既有产品语义保持不变。
- 不修改 `normalizeValidatedAuthority` 对 `http`/`ws` 的兼容处理；静态 Compiler 在其之前完成
  更严格的输入封闭。
- 不在本变更集统一 query 的键顺序或合法等价转义表示。
- 不修改 OAuth custom base URL、第三方 API Key、非 Codex persona 或画像外 route 的产品语义。
- 不调整 Header、Body、TLS 画像、业务路由、连接生命周期或 adapter 行为。
- 不删除旧路径或兼容层，不触发规格 §5.2 退休流程。

## 9. 风险、侵入面与回滚

| 风险 | 处置 |
|---|---|
| WS `https` 测试夹具被新规则拒绝 | 将所有进入 Compiler/Executor 的测试输入修正为生产实际使用的 `wss`；只验证路由解析或签发后 adapter 形态的 `https` 夹具保留 |
| query 合法表示被误伤 | 明确采用结构化语义封闭；顺序和合法转义保持宽容，只拒绝空 component、闭集外键和错误值 |
| `{file_id}` 的 escaped 字符被错误拆段 | 增加 `/`、空格、`%` 的 PathEscape/RawPath 正例 |
| `ForceQuery` 摘要绑定影响合法请求 | 合法 URL 均为 `ForceQuery=false`；运行摘要与 Guard 配对测试及 final-wire 空差异门禁 |
| changeset6 receipt 与冻结摘要不同 | 记录目录迁移造成的已知元数据差异；wire 比较只使用 manifest/secret-scan 和严格 capture 比较器 |
| Previous 被误伤 | Active/Previous 均执行 15 个静态端点正例和 56 面 final-wire |

生产代码侵入面：fork 自有 `backend/internal/officialegress/compiler.go` 与 `types.go`，以及
复审 P1-1 要求的 service 可信值接缝（`openai_ws_pool.go` 请求字段、
`official_egress_websocket_invocation.go` 透传、`openai_live.go` 提交 `record.CallID`）；其余仅
调整实际进入 Compiler/Executor 的测试夹具与 `.gitignore` 的 `.DS_Store` 再排除（复审 P1-2），
不修改生产 URL builder。无数据、配置、导出 API 或运行机器资产变更。

回滚时撤销上述生产文件及配套测试变更即可；不涉及数据迁移或配置回滚。

## 10. 跟踪

- 前置验证：当前 HEAD `9c7adb8eb3` 已通过 changeset6 生成器，56 个 capture 比较通过；生成
  manifest=`169152bcd97bd3ce63983b850f08ac4031597c2a8b855f27304d1fc4b6285e5f`，
  secret-scan=`64b960366f3a14efdb3aa4b1e0418c154a9d40079be1c985e3dbf942722484d3`；
  当前生成 receipt=`50dd2ca7ce0b9b3dca551ba715ab969b503cb3bdbba225f8a488b9d0472641aa`，
  与历史冻结 receipt 的差异仅为目录路径元数据。
- 实现：已完成。生产代码改
  `backend/internal/officialegress/compiler.go`（新增 `validateStaticCompilerTarget`、
  `validateStaticCompilerPath`、`validateStaticCompilerQuery`，静态分支接入，动态
  ReturnedURL 分支既有验证模型不变；`EndpointDynamicInputs` 扩展 `ServerResponseQuery`
  可信通道并在动态分支 fail-close）、`backend/internal/officialegress/types.go`
  （`requestDigest` 以独立 `\x00` 分隔字段绑定 `strconv.FormatBool(req.URL.ForceQuery)`），
  以及复审 P1-1 要求的 service 可信值接缝：`openai_ws_pool.go`
  （`openAIWSAcquireRequest.ServerResponseQuery`）、
  `official_egress_websocket_invocation.go`（`executeAcquire` 透传为
  `EndpointDynamicInputs`）、`openai_live.go`（`dialLiveSideband` 提交
  `{"call_id": record.CallID}`）。复审 P1-2：删除 `docs/egress/.DS_Store` 并在
  `.gitignore` 的 `!docs/egress/**` 之后再排除 `docs/egress/**/.DS_Store`。
  compiler.go、official_egress_websocket_invocation.go 与
  official_egress_changeset3_production_final_wire_test.go 属 changeset3 冻结源码，按既有
  惯例经 `docs/egress/maintenance/compiler-static-url-closure-source-transition.json`
  三条 transitions 承接摘要，frozen 测试同步接入该层。
- 测试：已完成。新增
  `backend/internal/officialegress/compiler_static_url_closure_test.go`（helper 结构与
  query 负例、server_response 可信值负例——值为空/非空改写/缺可信输入/可信输入画像外键、
  动态端点提交 `ServerResponseQuery` 拒绝且合法 ReturnedURL 对照不变、Active/Previous 15
  静态端点 Compile 正例含 URL tuple 断言、query 合法等价表示、`{file_id}` 含 `/`
  （`%2F` 单段）/空格/`%` 的 RawPath 正例、真实 `Executor.Execute` 负例——含非空改写与缺
  可信输入的 `call_id`——断言 `official_egress.compiler.rejected` 且 adapter/port 调用为 0、
  ForceQuery 摘要配对、受信 wss→https 别名 Guard 允许 + 签发后 ForceQuery 篡改拒绝）。
  实际提交 WS Compiler/Executor 的夹具修正：officialegress 的
  `executor_invocation_test.go`（target `wss`，WS→HTTP fallback 显式换 `https` target）、
  `changeset2_synthetic_rollback_test.go`；service 的 `openai_ws_pool_test.go`（入站
  semantic 请求保留 `https`，出站 Plan target 用 `wss`）、`openai_forward_plan_test.go`、
  `openai_live_test.go`（sideband dial 按新契约提交 `call_id` 可信值）、changeset3 capture
  构造同步生成可信动态事实。`backend/` 下
  `go test ./internal/officialegress/... ./internal/service/... -count=1` 全绿；仓库根
  `make check-egress-spec` 通过（含维护工作区 transition 重新生成）。
- final-wire 证据：post 与 pre 三件套逐字节一致（manifest=`169152bc…`、
  secret-scan=`64b96036…`、receipt=`50dd2ca7…`），56 个合法 capture 在新校验下全部经
  `invocation.Execute → Compiler.Compile → validateCompilerTarget` 真实链通过，允许列表为空；
  复审 P1-1 修复后（realtime_sideband 走 `ServerResponseQuery` 可信通道）重新生成，三件套
  摘要不变。
- 复审（2026-08-06，`changes_requested`，两项 P1 均已修复）：
  P1-1——`server_response` query 原实现仅校验非空，未绑定可信来源；已按审核建议扩展
  `EndpointDynamicInputs.ServerResponseQuery` 可信通道并逐字封闭（见 §4.2 规则 7 与
  §5 第二步）。P1-2——`.gitignore` 的 `!docs/egress/**` 重新纳入 Finder 生成的
  `.DS_Store`，破坏维护工作区 transition 确定性复算；已删除该文件并在重新包含之后再次
  排除 `docs/egress/**/.DS_Store`。
- 二轮复审（2026-08-06，`changes_requested`，P1 均关闭，一项 P2 已修复）：
  P2——可信 map 的 clone 闭环不完整：静态有效路径未执行新增深拷贝（clone 只在动态
  分支迟到调用），`cloneOpenAIWSAcquireRequest` 浅拷贝 `ServerResponseQuery` 使
  `lastAcquire` 池快照与调用方共用可变 map。已按审核建议：`validateCompilerTarget`
  入口统一 `dynamic.clone()`；`cloneOpenAIWSAcquireRequest` 深拷贝该 map；新增两个
  mutation 测试（`TestEndpointDynamicInputsCloneDetachesServerResponseQuery`、
  `TestCloneOpenAIWSAcquireRequestDetachesServerResponseQuery`）证明修改原 map 不影响
  克隆快照。
- 完成日期：2026-08-06。
