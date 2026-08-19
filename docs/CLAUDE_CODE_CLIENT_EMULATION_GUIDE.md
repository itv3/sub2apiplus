# Claude Code 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 Anthropic OAuth（`authMethod=claude.ai`、`apiProvider=firstParty`）
> 出站时的 Claude Code 客户端仿真
> **共享框架**：[`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)
> **当前取证目标**：`claude-code 2.1.226`——第二部分 110 条活动规则与 Vircs 官方客户端 P／R／M
> 证据的绑定版本；`2.1.220` 只保留为同一 Schema／Compiler 下的历史 baseline fixture
> **机器台账**：`tools/official_client_capture/claude_fw_f_v21_finalize.py`、两份 FW-F v4／v3 策略及
> `local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v5-final/`；逐规则状态以不可变台账为准，
> 正文规则集合由门禁强制对账
> **文档定位**：本文是 Claude Code 官方事实、版本画像、Sub2API 实现、环境职责和版本演进的唯一
> 人类可读权威入口；机器证据和 JSON 台账不得形成第二套规范
> **证据边界**：本文没有未压缩 TS 源码可用，静态规则均从官方生产 bundle 逆向建立；材料、证据、
> 规则与结论全部独立取自 Claude Code 自身，不继承 Codex 的任何事实结论
> **运行时状态**：本文已经建档 Claude Persona 目标，但当前 strict 多 Persona 运行时尚未登记或激活
> Claude；现有 service finalizer 仍属于遗留局部仿真
> **末次更新**：2026-08-19

---

# 第一部分 目标与当前边界

## 1.1 目标、范围与证据模式

本文只定义 Claude Code persona 的专属事实；跨客户端目标、等价标准和共享控制链以
[`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 为准。当前规则绑定
`claude-code 2.1.226`、Anthropic firstParty OAuth（`authMethod=claude.ai`、
`apiProvider=firstParty`），覆盖 TLS、HTTP、Header、Body、端点、连接、重试和跨请求状态。

| 范围 | 处理 |
|---|---|
| Claude Code firstParty OAuth | 本文对齐目标 |
| API Key、Bedrock、Vertex、Foundry | 范围外；只可作为差分对照 |
| 遥测与非必要流量 | 仅记录配置与可达性，不进入行为一致性维度；官方允许关闭，零流量不得计为差异 |
| 机器环境 | 是证据身份；具体职责与限制见第六部分 |

当前全部目标运行证据使用 `essential-traffic` 与 `no-telemetry` 模式。官方客户端的隐私状态为：

| 模式 | 触发 | 证据含义 |
|---|---|---|
| `essential-traffic` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | 关闭非必要流量；当前端点结论只覆盖此模式 |
| `no-telemetry` | `DISABLE_TELEMETRY=1` 或 `DO_NOT_TRACK` | 关闭遥测，但不等同于 essential-only |
| `default` | 上述条件均不成立 | 会放行额外请求，必须另建证据范围 |

这些都是官方原生配置，不是候选规避取证：`DISABLE_TELEMETRY` 经 `isAnalyticsDisabled()` 关闭
Datadog 与第一方 `event_logging`（`src/services/analytics/`）；
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 经 `isEssentialTrafficOnly()` 门控 mcp-registry、
policy_limits、grove、releaseNotes、feedback、modelCapabilities、referral 等非必要请求，
`privacyLevel.ts` 的定义即“关闭全部非必要网络流量”。因此，流量类别是否出现这一行为层不作一致性
对比维度；`essential-traffic` 只界定当前 strict wire／PAIR 的取证范围，不是行为维度。场景被调用时
仍须逐规则核对实际 essential 请求；外围流量只记录，零遥测／零非必要流量不得计为一致性差异。

bundle 中 essential gate 有 53 个调用点、非 default gate 有 7 个调用点；这些数字只证明静态门控面，
不能代替运行端点全集。每条规则仍须绑定二进制摘要、平台、入口、模型、账号／feature 条件与观测通道。

## 1.2 Persona 链路与阅读顺序

```text
官方 Claude Code／第三方标准 API
→ IngressProtocolAdapter
→ CanonicalRequest + TranslationReport
→ Claude PersonaPlanner + ClaudeIdentityFacts
→ Claude Code active ReleaseArtifact／ReleaseBundle
→ Claude DialectCompiler → CompiledEnvelope
→ Claude Executor authority 实例 + 共享 Executor 实现／Guard
→ Anthropic OAuth 上游
```

`IngressProtocolAdapter` 按 Anthropic Messages、OpenAI Responses 等入站协议实现，并输出统一的
语义和无损／降级／拒绝报告；Claude PersonaPlanner 只消费该合同，不复制各协议解析器。出站画像函数
为 `ClaudeProfile(规范化语义请求, 会话状态, 平台与入口条件, 服务端特性状态)`；入站客户端名称、UA
和自报版本不是函数输入。第二部分记录官方规则，第三部分记录 Claude 实现与迁移，第四部分只补充
Claude 特有的换版流程，第五部分处理维护，第六部分固定环境职责。

---

# 第二部分 Claude Code 2.1.226 实测规则画像

本部分是当前 Claude Persona 的唯一活动规则画像。活动集合由原子化实测结果确定为 **110 条
request-egress 规则**；每条规则都由 Claude Code 2.1.226 的官方客户端真实运行断言通过。普通规则
绑定 R（等长脱敏的原始请求／必要响应字节）与 M（版本、二进制、运行、连接、干预和隐私条件），
TLS 规则绑定原生 P 与 M。2.1.220、2.1.88 与 HitCC 只用于
历史差分、线索审计和探针设计；FW-F v1 的 154 条集合只保留为失效审计历史，不能替代 2.1.226 实测证据。

## 2.1 目标身份、范围与结论

| 维度 | 冻结值 |
|---|---|
| 官方版本 | Claude Code `2.1.226` |
| 目标二进制 SHA-256 | `4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555` |
| 取证执行源 SHA-256 | `78fae770cbb54af5e9192ae6557516d9fd78187914fbb6399a359e1a75573c06` |
| 平台 | Linux／amd64 |
| 入口 | `sdk-cli`；真实交互入口 `cli` |
| 认证 | Claude.ai OAuth，first-party provider |
| 模型 | `claude-sonnet-5`；TUI 标题 `claude-haiku-4-5-20251001`；fallback `claude-haiku-4-5` |
| 隐私模式 | `DISABLE_TELEMETRY=1`；`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |
| 上游 | `api.anthropic.com:443` |
| 完整场景矩阵 | 77／77 场景、395 条官方请求、49／49 维度；含 TUI／sdk-cli／Agent、TLS、工具往返和故障重试 |
| strict egress | messages、hello、policy limits、settings、OAuth profile、count_tokens、OAuth refresh、MCP servers，共 8 类 |
| 活动规则 | 110 条，全部独立 `PAIR-*` 正负断言通过；107 条 R/M、3 条 TLS P/M |
| 当前等级 | 110 条 `observed`、0 条 `verified` |
| 批准用途 | `validation_only`；不是 production replacement |

`observed` 不等于“没有实测”。它表示规则已在冻结的 2.1.226 官方客户端运行中命中并通过断言，但尚未
通过 FW-G 的独立复测、实现后逐字节对拍与生产替换门禁，因此不能改名为 `verified`。

当前 SupportEnvelope 覆盖策略文件列出的五组能力：`sdk-cli`／`cli` 的条件 system、cache、metadata、
session、Agent／background／hook／remote、工具往返与附件；真实 TUI 的 OAuth profile、标题、
count_tokens 与 MCP 目录；隔离故障矩阵的 retry、timeout、stream／model fallback；过期凭据的隔离 OAuth
refresh；以及原生 TLS／ALPN。八类 strict egress 的规则分布分别为 messages 98 条、hello 6 条、
policy limits 4 条、settings 4 条、OAuth profile 2 条、count_tokens 3 条、OAuth refresh 3 条、MCP
servers 2 条。跨端点规则会同时计入多个 egress，全局唯一规则仍为 110 条。范围外能力必须 fail-close
或留在明确的 `non_persona_managed／retained_legacy` 边界，不能从这 110 条规则外推。

## 2.2 证据通道与内容寻址

### 2.2.1 权威证据

| 别名 | 内容 |
|---|---|
| `M-ID` | FW-E `campaign/identity.json`：版本、二进制 SHA、隐私环境、目标 host 和生产零变更 |
| `M-INDEX` | FW-E `campaign/indexes/relay-index.json`：基础目标／基线 run 与二进制身份 |
| `R-a1/s1/s2/s4` | FW-E 四个目标 run 的等长脱敏 `client_to_upstream.bin` |
| `M-a1/s1/s2/s4` | 四个基础 run 的 `relay-manifest.json` 与 `relay/relay.json` |
| `R-v4-*` | v21 每个 attempt 的 `connNNN.client_to_upstream.bin`；故障规则还绑定必要的 `upstream_to_client.bin` |
| `P-v4-*` | 原生 TLS attempt 的 `tls-clienthello.pcap`，用于 CipherSuite 与 ALPN 正负例 |
| `M-v4-*` | v21 的 manifest、relay、intervention、invocation、summary、场景目录、秘密扫描与 cleanup |
| `PAIR-*` | v21 最终化器生成的逐规则独立正例与负例断言 |

基础四个 run 位于：

`local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/runtime-relay-205d7f58f/campaign/`

覆盖 77 个真实场景的 v21 正式 Campaign 位于：

`local-analysis/fw-f/claude-code-2.1.226/complete-v21-78fae770cbb5/`

其中 77／77 场景、395 条请求、49／49 维度和原生 TLS P 通道均由最终化器逐项复算；目录集合、执行源
摘要、目标二进制、采集模式、等长脱敏、秘密扫描和 cleanup 也必须通过。机器权威账本为：

`local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v5-final/measured-rule-ledger.json`

账本为每条规则保存 `egress_ids`、适用条件、完整分母、证据文件 `path/sha256/bytes/channel`、命中
run、连接号、流偏移和原始请求摘要。下表的每个 `R-<run> + M`／`P-<run> + M` 都能在该规则的 `evidence_refs` 中
逐文件反向解析；Authorization 已等长脱敏，不保存 OAuth secret。

### 2.2.2 证据边界与零流量事实

当前 Campaign 已在原生 TLS attempt 中取得 ClientHello pcap，`SPEC-TLS-001/002/003` 使用 P/M；普通
HTTP、Header、Body、状态与工具规则仍使用 R/M，不允许以 relay 元数据冒充原生 TLS 证据。

行为层不作对比维度。官方配置已合法关闭遥测与非必要流量，候选“零遥测”不是“不像官方客户端”的
判据，也不生成 `traceparent`／span 规则。响应解析属于 downstream compatibility，不进入客户端请求
出站画像。

## 2.3 规则准入合同

命题只有同时满足以下条件才能进入本部分：

1. 目标版本、二进制、平台、入口、认证、模型／模型转换和隐私条件必须与 M 一致；
2. 普通规则必须有可复算的目标 R 原始字节；TLS 规则必须有原生 P；不接受仅有旧源码、bundle 字符串
   或历史 wire；
3. 必须有独立 `PAIR-<SPEC-ID>` 断言，对完整具名分母执行后为 `passed`；
4. 必须声明适用范围、正负例、物理 `egress_ids` 和证据等级；
5. 必须是 request-egress 命题；响应兼容、遥测关闭事实和未触发功能只能进入支撑／边界事实；
6. 合法零流量的 telemetry、nonessential、usage、models、dispatch-id、usage-limit 只记录支撑事实；
   只有场景被真实触发且具备完整正负例的命题才能成为规则。

FW-F v1 把 32 个候选机械拆成 97 条规则提案的做法无效，现已逐条撤回。v21 对旧 88 条候选作实质
重测，并由新增场景原子化产生 22 条新规则，最终形成 110 条；规则数由实测断言决定，不由发现项、
探针或旧版行数预设。

## 2.4 实测 wire 与端点向量

### 2.4.1 messages 推理核心

基础 `sdk-cli` 请求行为固定为：

- 请求行：`POST /v1/messages?beta=true HTTP/1.1`，Host 为 `api.anthropic.com`；
- 基础 Header 按实测大小写和顺序发送；条件 Header、自定义 Header、gzip、TUI、retry 和 fallback
  只按 `2.5` 对应规则插入或变化；
- 基础 UA 为 `claude-cli/2.1.226 (external, sdk-cli)`；真实 TUI 使用 `external, cli`，获准条件段按
  agent-sdk、client-app、workload 的实测顺序追加；
- Stainless 基础向量为 `x64/js/Linux/0.94.0/retry 0/node/v26.3.0/timeout 600`；
- 基础 Body 顶层顺序为 `model/messages/system/tools/metadata/max_tokens/thinking/`
  `context_management/output_config/stream`；条件变体以逐条规则为准；
- request-id、Session-Id、agent lineage、resume／fork、retry、non-stream 与模型 fallback 均按实测
  生命周期生成，不能用入站自报客户端身份补造。

### 2.4.2 八类 strict 端点

| egress | method／target | 已冻结 wire 重点 |
|---|---|---|
| `egress-claude-lifecycle-hello` | `HEAD /api/hello` | 无 Body；`Connection/User-Agent/Accept/Host/Accept-Encoding` 五项 Header |
| `egress-claude-messages-inference` | `POST /v1/messages?beta=true` | JSON 或 gzip JSON；Header、Body、状态和重试由画像规则编译 |
| `egress-claude-policy-limits` | `GET /api/claude_code/policy_limits` | OAuth、`oauth-2025-04-20`、`claude-code/2.1.226` UA，共七项 Header |
| `egress-claude-settings` | `GET /api/claude_code/settings` | 在 policy limits 基础上增加 `Cache-Control:no-cache`、`Pragma:no-cache`，共九项 Header |
| `egress-claude-oauth-profile` | `GET /api/oauth/profile` | TUI；`axios/1.15.2` UA、JSON Content-Type、OAuth，共八项 Header |
| `egress-claude-count-tokens` | `POST /v1/messages/count_tokens?beta=true` | TUI token 计数；有序 Claude SDK Header 与 `model/messages/tools` Body |
| `egress-claude-oauth-token-refresh` | `POST https://platform.claude.com/v1/oauth/token` | 隔离过期凭据场景；axios Header 与等长脱敏 refresh Body |
| `egress-claude-mcp-servers` | `GET /v1/mcp_servers?limit=1000` | TUI MCP 目录；OAuth、MCP capabilities 与 protocol version |

`sdk-cli` essential 顺序为 hello → policy limits／settings（两者不规定先后）→ 零或多个 messages；
真实 TUI 为 hello → OAuth profile → Haiku 标题 → Sonnet 主推理。每个端点有独立 route、Sink、
真实 TUI 还会在对应场景调用 count_tokens 与 MCP 目录；OAuth refresh 只在过期凭据条件下触发。每个
端点有独立 route、Sink、binding、画像视图和纵向 CompiledEnvelope，不得把全部 110 条伪挂到
messages egress。

## 2.5 110 条活动规则

以下每一行均为 `assertion_result=passed`、`evidence_level=observed`、
`compatibility_class=request_egress`。证据列列出实际命中的 R run；`+ M` 表示同一行还绑定
`M-ID/M-INDEX` 与对应 run 的 manifest／relay／intervention／invocation／summary。精确文件摘要和流
偏移以 v21 MeasuredRuleLedger 为准。

<!-- FW-F-ACTIVE-RULES-BEGIN -->

### 2.5.1 TLS、协议、端点与连接：25 条

| 规则 | 实测命题 | 完整分母／适用范围 | strict egress | 精确 P/R/M run |
|---|---|---|---|---|
| `SPEC-CONN-002` | 无 Retry-After 的应用层重试，首轮实测等待落在 500–700ms，第二轮落在 1000–1250ms。 | 4/4 retry-interval；v3-retry-limit 与 v3-fallback-model | `messages-inference` | `R-v3-fallback-model, v3-retry-limit + M` |
| `SPEC-CONN-010` | 隔离状态矩阵中 401、408、409、429、500、502、503、529 各重试一次；400、403 不重试。 | 10/10 fault-scenario；10 个单状态隔离故障运行 | `messages-inference` | `R-v3-nonretry-400, v3-nonretry-403, v3-retry-401, v3-retry-408, v3-retry-409, v3-retry-429, v3-retry-500, v3-retry-502, v3-retry-503, v3-retry-529 + M` |
| `SPEC-CONN-016` | Retry-After:1 使重试间隔约 1030ms；未来 HTTP-date 在当前实现中未按日期等待，约 564ms 后走默认退避。 | 2/2 retry-after-scenario；两个 Retry-After 隔离故障运行 | `messages-inference` | `R-v3-retry-after-date, v3-retry-after-seconds + M` |
| `SPEC-CONN-018` | 创建流收到 404 时总会转 non-stream；已建立流中断默认转 non-stream，disable flag 只阻止中断后的转换，不阻止创建 404 转换。 | 4/4 fault-scenario；四个 streaming fallback 隔离故障运行 | `messages-inference` | `R-v3-stream-404-disable-flag, v3-stream-404-fallback, v3-stream-interrupt, v3-stream-interrupt-no-fallback + M` |
| `SPEC-CONN-019` | a1、s2、s4 的多轮推理请求复用同一条有效 HTTP/1.1 连接。 | 3/3 multi-request-run；3 个多请求运行 | `messages-inference` | `R-a1, s2, s4 + M` |
| `SPEC-CONN-020` | 首个 messages 连接无响应断开后官方客户端重试；重发请求省略 Connection Header。 | 1/1 retry-transition；v3-disconnect-retry | `messages-inference` | `R-v3-disconnect-retry + M` |
| `SPEC-CONN-021` | 应用层重试保持 Body、Session-Id 与主体 attribution，重新生成 x-client-request-id，X-Stainless-Retry-Count 始终为 0。 | 15/15 retry-transition；状态重试、Retry-After、断连与 retry-limit 运行 | `messages-inference` | `R-v3-disconnect-retry, v3-fallback-model, v3-retry-401, v3-retry-408, v3-retry-409, v3-retry-429, v3-retry-500, v3-retry-502, v3-retry-503, v3-retry-529, v3-retry-after-date, v3-retry-after-seconds, v3-retry-limit + M` |
| `SPEC-CONN-022` | CLAUDE_CODE_MAX_RETRIES=2 时每个模型最多发送三次，配置 fallback model 时可再发一次 Haiku；值为 0 时不重试。 | 3/3 configured-run；v3-retry-limit、v3-fallback-model 与 v3-timeout | `messages-inference` | `R-v3-fallback-model, v3-retry-limit, v3-timeout + M` |
| `SPEC-CONN-023` | 配置 fallback model 且主 Sonnet 连续三次 529 后，第四次请求切换到 Haiku。 | 1/1 fallback-transition；v3-fallback-model | `messages-inference` | `R-v3-fallback-model + M` |
| `SPEC-CONN-024` | API_TIMEOUT_MS=1000 映射为 X-Stainless-Timeout:1；配合 max retries 0 时 stalled 请求只发送一次。 | 1/1 configured-run；v3-timeout | `messages-inference` | `R-v3-timeout + M` |
| `SPEC-EP-001` | 推理请求的 request-target 恰为 /v1/messages?beta=true。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-EP-002` | 每次运行先通过独立连接发送无 Body 的 HEAD /api/hello，并使用固定五项 Header。 | 4/4 request；4 次运行的生命周期请求 | `lifecycle-hello` | `R-a1, s1, s2, s4 + M` |
| `SPEC-EP-003` | 推理请求的方法恰为 POST。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-EP-004` | 推理请求的 Host 恰为 api.anthropic.com。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-EP-005` | sdk-cli 启动阶段发送 GET /api/claude_code/policy_limits；请求使用 first-party OAuth、oauth beta、claude-code/2.1.226 UA 和实测七项基础 Header。 | 55/55 request；除真实 TUI 外的 2.1.226 v3 官方运行 | `policy-limits` | `R-v3-adaptive-thinking-disabled, v3-additional-protection, v3-agent-sdk, v3-append-system, v3-attribution-disabled, v3-baseline, v3-beta-deduplicate, v3-cache-disabled, v3-cache-one-hour, v3-cache-sonnet-disabled, v3-client-app, v3-custom-agent, v3-custom-agent-safe-mode, v3-custom-header-grammar, v3-custom-header-invalid-name, v3-custom-system, v3-disconnect-retry, v3-effort-low, v3-effort-max, v3-effort-medium, v3-effort-xhigh, v3-exclude-dynamic-system, v3-extra-body, v3-extra-metadata, v3-fallback-model, v3-gzip-request, v3-header-combination, v3-json-schema, v3-max-output-tokens, v3-nonretry-400, v3-nonretry-403, v3-remote-container, v3-remote-session, v3-retry-401, v3-retry-408, v3-retry-409, v3-retry-429, v3-retry-500, v3-retry-502, v3-retry-503, v3-retry-529, v3-retry-after-date, v3-retry-after-seconds, v3-retry-limit, v3-session-fork, v3-session-resume, v3-stream-404-disable-flag, v3-stream-404-fallback, v3-stream-interrupt, v3-stream-interrupt-no-fallback, v3-thinking-disabled, v3-timeout, v3-workload + M` |
| `SPEC-EP-006` | sdk-cli 启动阶段发送 GET /api/claude_code/settings；请求在 policy_limits 基础上增加 Cache-Control:no-cache 与 Pragma:no-cache。 | 55/55 request；除真实 TUI 外的 2.1.226 v3 官方运行 | `settings` | `R-v3-adaptive-thinking-disabled, v3-additional-protection, v3-agent-sdk, v3-append-system, v3-attribution-disabled, v3-baseline, v3-beta-deduplicate, v3-cache-disabled, v3-cache-one-hour, v3-cache-sonnet-disabled, v3-client-app, v3-custom-agent, v3-custom-agent-safe-mode, v3-custom-header-grammar, v3-custom-header-invalid-name, v3-custom-system, v3-disconnect-retry, v3-effort-low, v3-effort-max, v3-effort-medium, v3-effort-xhigh, v3-exclude-dynamic-system, v3-extra-body, v3-extra-metadata, v3-fallback-model, v3-gzip-request, v3-header-combination, v3-json-schema, v3-max-output-tokens, v3-nonretry-400, v3-nonretry-403, v3-remote-container, v3-remote-session, v3-retry-401, v3-retry-408, v3-retry-409, v3-retry-429, v3-retry-500, v3-retry-502, v3-retry-503, v3-retry-529, v3-retry-after-date, v3-retry-after-seconds, v3-retry-limit, v3-session-fork, v3-session-resume, v3-stream-404-disable-flag, v3-stream-404-fallback, v3-stream-interrupt, v3-stream-interrupt-no-fallback, v3-thinking-disabled, v3-timeout, v3-workload + M` |
| `SPEC-EP-007` | 真实 TUI 启动阶段发送 GET /api/oauth/profile，使用 axios/1.15.2 UA、JSON Content-Type、OAuth Authorization 与实测八项 Header。 | 1/1 request；v3-tui 的 cli 入口 | `oauth-profile` | `R-v3-tui + M` |
| `SPEC-EP-008` | sdk-cli 每次调用的 essential 请求序列为 hello→policy/settings→零或多个 messages；真实 TUI 为 hello→oauth/profile→Haiku 标题→Sonnet 主推理，policy 与 settings 不规定彼此先后。 | 54/54 run；34 个真实上游探针与 20 个隔离故障探针 | `lifecycle-hello, messages-inference, oauth-profile, policy-limits, settings` | `R-v3-adaptive-thinking-disabled, v3-additional-protection, v3-agent-sdk, v3-append-system, v3-attribution-disabled, v3-baseline, v3-beta-deduplicate, v3-cache-disabled, v3-cache-one-hour, v3-cache-sonnet-disabled, v3-client-app, v3-custom-agent, v3-custom-agent-safe-mode, v3-custom-header-grammar, v3-custom-header-invalid-name, v3-custom-system, v3-disconnect-retry, v3-effort-low, v3-effort-max, v3-effort-medium, v3-effort-xhigh, v3-exclude-dynamic-system, v3-extra-body, v3-extra-metadata, v3-fallback-model, v3-gzip-request, v3-header-combination, v3-json-schema, v3-max-output-tokens, v3-nonretry-400, v3-nonretry-403, v3-remote-container, v3-remote-session, v3-retry-401, v3-retry-408, v3-retry-409, v3-retry-429, v3-retry-500, v3-retry-502, v3-retry-503, v3-retry-529, v3-retry-after-date, v3-retry-after-seconds, v3-retry-limit, v3-session-fork, v3-session-resume, v3-stream-404-disable-flag, v3-stream-404-fallback, v3-stream-interrupt, v3-stream-interrupt-no-fallback, v3-thinking-disabled, v3-timeout, v3-tui, v3-workload + M` |
| `SPEC-PROTO-001` | 推理与生命周期请求的应用层请求线均为 HTTP/1.1。 | 12/12 request；8 条推理请求与 4 条生命周期请求 | `lifecycle-hello, messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-EP-009` | 真实 TUI 的 token 计数请求为 POST /v1/messages/count_tokens?beta=true，Host 为 api.anthropic.com。 | 37/37 official-example；36 个 count_tokens 正例与 sdk-cli 基线负例 | `count-tokens` | `R-v4-tui-count-tokens, v4-replay-baseline + M` |
| `SPEC-EP-010` | 过期 OAuth 凭据触发 POST https://platform.claude.com/v1/oauth/token。 | 2/2 official-example；隔离 OAuth refresh 正例与普通推理负例 | `oauth-token-refresh` | `R-v4-oauth-refresh, v4-replay-baseline + M` |
| `SPEC-EP-011` | 真实 TUI 的 MCP 目录请求为 GET /v1/mcp_servers?limit=1000，Host 为 api.anthropic.com。 | 5/5 official-example；4 个 TUI MCP 目录正例与 sdk-cli 基线负例 | `mcp-servers` | `R-v4-tui-attachment, v4-tui-compact, v4-tui-count-tokens, v4-tui-usage, v4-replay-baseline + M` |
| `SPEC-TLS-001` | 四个目标原生 ClientHello 使用同一实测 CipherSuite 有序序列。 | 8/8 official-example；4 个 ClientHello 正例与 4 个负例 | `lifecycle-hello, messages-inference, policy-limits, settings` | `P-v4-native-tls-baseline + M` |
| `SPEC-TLS-002` | hello 与 messages ClientHello 提供 ALPN http/1.1；policy_limits 与 settings 的官方负例省略 ALPN 扩展。 | 4/4 official-example；同一原生 TLS attempt 的 2 个正例和 2 个负例 | `lifecycle-hello, messages-inference, policy-limits, settings` | `P-v4-native-tls-baseline + M` |
| `SPEC-TLS-003` | 目标连接在 relay 握手元数据中使用 SNI api.anthropic.com。 | 8/8 connection；8 个承载已选请求的真实 TLS 连接 | `lifecycle-hello, messages-inference` | `P-v4-native-tls-baseline + M` |

### 2.5.2 Header、认证与 beta：33 条

| 规则 | 实测命题 | 完整分母／适用范围 | strict egress | 精确 R/M run |
|---|---|---|---|---|
| `SPEC-AUTH-002` | firstParty OAuth 推理请求发送 Bearer Authorization；证据中的 token 已等长脱敏。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BETA-008` | ANTHROPIC_BETAS 按逗号拆分、修剪并丢弃空项后插入官方 effort/extended-cache 之前；与官方项及自身重复的值均原样保留，不去重。 | 2/2 request；v3-beta-deduplicate 与基线 | `messages-inference` | `R-v3-baseline, v3-beta-deduplicate + M` |
| `SPEC-HDR-001` | 推理请求按实测大小写和相对顺序发送 22 项基础 Header；子代理只在 x-app 与 x-client-request-id 之间插入 agent-id。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-002` | User-Agent 基础为 claude-cli/2.1.226；sdk-cli 与真实 TUI 分别使用 external,sdk-cli 和 external,cli，并按已批准条件追加 agent-sdk、client-app、workload 段。 | 82/82 request；旧 8 条推理样本与 v3 全部 messages 请求 | `messages-inference` | `R-a1, s1, s2, s4, v3-adaptive-thinking-disabled, v3-additional-protection, v3-agent-sdk, v3-append-system, v3-attribution-disabled, v3-baseline, v3-beta-deduplicate, v3-cache-disabled, v3-cache-one-hour, v3-cache-sonnet-disabled, v3-client-app, v3-custom-agent, v3-custom-header-grammar, v3-custom-system, v3-disconnect-retry, v3-effort-low, v3-effort-max, v3-effort-medium, v3-effort-xhigh, v3-exclude-dynamic-system, v3-extra-body, v3-extra-metadata, v3-fallback-model, v3-gzip-request, v3-header-combination, v3-json-schema, v3-max-output-tokens, v3-nonretry-400, v3-nonretry-403, v3-remote-container, v3-remote-session, v3-retry-401, v3-retry-408, v3-retry-409, v3-retry-429, v3-retry-500, v3-retry-502, v3-retry-503, v3-retry-529, v3-retry-after-date, v3-retry-after-seconds, v3-retry-limit, v3-session-fork, v3-session-resume, v3-stream-404-disable-flag, v3-stream-404-fallback, v3-stream-interrupt, v3-stream-interrupt-no-fallback, v3-thinking-disabled, v3-timeout, v3-tui, v3-workload + M` |
| `SPEC-HDR-003` | 主请求发送实测的 9 项 anthropic-beta 有序序列；一级子代理省略末尾 extended-cache-ttl。 | 8/8 request；7 条主请求与 1 条子代理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-004` | 推理请求发送 anthropic-version: 2023-06-01。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-005` | 推理请求发送 Accept-Encoding: gzip, deflate, br, zstd。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-006` | Linux/amd64 样本发送实测的 Stainless 架构、语言、系统、SDK、重试、运行时和超时向量。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-007` | sdk-cli 前台推理请求发送 dangerous-direct-browser-access=true 与 x-app=cli。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-009` | additional-protection 条件成立时插入 x-anthropic-additional-protection:true，位置在 anthropic-version 与 x-app 之间；条件不成立时省略。 | 3/3 request；v3-additional-protection、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-additional-protection, v3-baseline, v3-header-combination + M` |
| `SPEC-HDR-012` | 每条推理请求的 x-client-request-id 是 UUID，且 8 条样本内不复用。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-013` | 同一多请求运行复用同一个 X-Claude-Code-Session-Id。 | 3/3 multi-request-run；a1、s2、s4 三个多请求运行 | `messages-inference` | `R-a1, s2, s4 + M` |
| `SPEC-HDR-014` | 仅一级子代理请求携带 17 位小写十六进制 x-claude-code-agent-id，位置在 x-app 之后。 | 8/8 request；1 条子代理正例与 7 条非子代理负例 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-015` | 一级子代理请求复用对应主请求的 X-Claude-Code-Session-Id。 | 2/2 request；a1 的子代理正例与对应主请求负例 | `messages-inference` | `R-a1 + M` |
| `SPEC-HDR-016` | client-app 条件成立时发送 x-client-app；未设置时省略。 | 3/3 request；v3-client-app、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-baseline, v3-client-app, v3-header-combination + M` |
| `SPEC-HDR-017` | remote-container 条件成立时发送 x-claude-remote-container-id；未设置时省略。 | 3/3 request；v3-remote-container、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-baseline, v3-header-combination, v3-remote-container + M` |
| `SPEC-HDR-018` | remote-session 条件成立时发送 x-claude-remote-session-id；未设置时省略。 | 3/3 request；v3-remote-session、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-baseline, v3-header-combination, v3-remote-session + M` |
| `SPEC-HDR-021` | x-client-app 的值逐字节等于受控 client-app 输入。 | 2/2 request；v3-client-app 与 v3-header-combination | `messages-inference` | `R-v3-client-app, v3-header-combination + M` |
| `SPEC-HDR-022` | client-app 同时生成 x-client-app 与 User-Agent 的 client-app/<值> 后缀，两处来自同一受控值。 | 3/3 request；v3-client-app、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-baseline, v3-client-app, v3-header-combination + M` |
| `SPEC-HDR-023` | x-claude-remote-container-id 的值逐字节等于受控 container ID。 | 2/2 request；v3-remote-container 与 v3-header-combination | `messages-inference` | `R-v3-header-combination, v3-remote-container + M` |
| `SPEC-HDR-024` | x-claude-remote-session-id 的值逐字节等于受控 remote session ID。 | 2/2 request；v3-remote-session 与 v3-header-combination | `messages-inference` | `R-v3-header-combination, v3-remote-session + M` |
| `SPEC-HDR-026` | 多条件同时成立时，additional-protection、x-app、remote-container、remote-session、client-app、request-id 按实测顺序合并。 | 1/1 request；v3-header-combination | `messages-inference` | `R-v3-header-combination + M` |
| `SPEC-HDR-029` | ANTHROPIC_CUSTOM_HEADERS 按行解析，在首个冒号处分割并修剪名称和值；空行和无冒号行忽略，值内后续冒号保留。 | 1/1 request；v3-custom-header-grammar | `messages-inference` | `R-v3-custom-header-grammar + M` |
| `SPEC-HDR-030` | 空自定义 Header 名在发送 messages 前本地 fail-close，完整 relay 中 messages 数为零。 | 1/1 run；v3-custom-header-invalid-name | `messages-inference` | `R-v3-custom-header-invalid-name + M` |
| `SPEC-HDR-031` | 自定义 x-client-request-id 不能覆盖官方生成的 UUID。 | 1/1 request；v3-custom-header-grammar | `messages-inference` | `R-v3-custom-header-grammar + M` |
| `SPEC-HDR-032` | 获准自定义 Header 保持输入顺序，插入 X-Claude-Code-Session-Id 与 X-Stainless-Arch 之间。 | 1/1 request；v3-custom-header-grammar | `messages-inference` | `R-v3-custom-header-grammar + M` |
| `SPEC-HDR-042` | agent-sdk 条件成立时 User-Agent 追加 agent-sdk/<受控版本> 段；基线不追加。 | 3/3 request；v3-agent-sdk、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-agent-sdk, v3-baseline, v3-header-combination + M` |
| `SPEC-HDR-043` | workload 条件成立时 User-Agent 追加 workload/<受控值> 段；基线不追加。 | 3/3 request；v3-workload、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-baseline, v3-header-combination, v3-workload + M` |
| `SPEC-HDR-044` | Content-Length 等于实际序列化 JSON Body 的字节数。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-HDR-045` | count_tokens 使用实测有序 Header 向量、Bearer OAuth、会话/request-id 与 Claude SDK 身份字段。 | 37/37 official-example；36 个 count_tokens 正例与基线负例 | `count-tokens` | `R-v4-tui-count-tokens, v4-replay-baseline + M` |
| `SPEC-HDR-046` | OAuth refresh 使用 axios/1.15.2 七项有序 Header，Body 长度明确且不发送 Authorization。 | 2/2 official-example；OAuth refresh 正例与基线负例 | `oauth-token-refresh` | `R-v4-oauth-refresh, v4-replay-baseline + M` |
| `SPEC-HDR-047` | MCP 目录请求使用实测有序 Header，并携带 OAuth、anthropic beta/version、MCP client capabilities 与 MCP-Protocol-Version。 | 5/5 official-example；4 个 TUI MCP 目录正例与基线负例 | `mcp-servers` | `R-v4-tui-attachment, v4-tui-compact, v4-tui-count-tokens, v4-tui-usage, v4-replay-baseline + M` |
| `SPEC-HDR-048` | 二级及更深子代理在 agent-id 后发送 x-claude-code-parent-agent-id，值等于同一运行中直接父代理的 17 位 ID；一级子代理省略。 | 7/7 official-example；depth2/depth3 正例与 depth1 负例 | `messages-inference` | `R-v4-agent-depth1, v4-agent-depth2, v4-agent-depth3 + M` |

### 2.5.3 Body、缓存、metadata、状态与工具：52 条

| 规则 | 实测命题 | 完整分母／适用范围 | strict egress | 精确 R/M run |
|---|---|---|---|---|
| `SPEC-BODY-001` | 推理 Body 顶层键按 model、messages、system、tools、metadata、max_tokens、thinking、context_management、output_config、stream 排列。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-002` | metadata 仅含 user_id；其内嵌 JSON 恰含 device_id、account_uuid、session_id，且 session_id 等于会话 Header。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-003` | 主请求和子代理分别使用实测的四段/三段 system 结构、产品身份块与 cache_control 形态。 | 8/8 request；7 条主请求与 1 条子代理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-004` | 首轮、续轮、resume、fork、子代理与真实 TUI 分别使用实测 messages 角色序列。 | 14/14 request；旧 8 条推理样本、v3-session-* 与 v3-tui | `messages-inference` | `R-a1, s1, s2, s4, v3-session-fork, v3-session-resume, v3-tui + M` |
| `SPEC-BODY-005` | tools 字段始终存在；无工具场景为 []，Agent/Bash 场景发送对应实测 JSON Schema。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-007` | system[0].text 以 x-anthropic-billing-header: 开头并承载 attribution。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-008` | 当前具名样本的 model 恰为 claude-sonnet-5。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-009` | 当前具名样本的 max_tokens 恰为整数 64000。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-010` | 当前具名样本的 thinking 恰为 {"type":"adaptive"}。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-011` | 当前具名样本发送实测 clear_thinking_20251015 context_management。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-012` | output_config.effort 精确映射 low、medium、high、xhigh、max；基线为 high。 | 13/13 request；五种 effort 值的 v3 正例 | `messages-inference` | `R-a1, s1, s2, s4, v3-baseline, v3-effort-low, v3-effort-max, v3-effort-medium, v3-effort-xhigh + M` |
| `SPEC-BODY-013` | 当前具名样本的 stream 恰为 JSON 布尔值 true。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-014` | attribution 中 cc_version 匹配 2.1.226.<3 位小写十六进制>。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-015` | billing attribution 的 cc_entrypoint 与实际入口一致：print 为 sdk-cli，真实 TUI 为 cli。 | 81/81 request；sdk-cli 与 v3-tui messages | `messages-inference` | `R-a1, s1, s2, s4, v3-adaptive-thinking-disabled, v3-additional-protection, v3-agent-sdk, v3-append-system, v3-baseline, v3-beta-deduplicate, v3-cache-disabled, v3-cache-one-hour, v3-cache-sonnet-disabled, v3-client-app, v3-custom-agent, v3-custom-header-grammar, v3-custom-system, v3-disconnect-retry, v3-effort-low, v3-effort-max, v3-effort-medium, v3-effort-xhigh, v3-exclude-dynamic-system, v3-extra-body, v3-extra-metadata, v3-fallback-model, v3-gzip-request, v3-header-combination, v3-json-schema, v3-max-output-tokens, v3-nonretry-400, v3-nonretry-403, v3-remote-container, v3-remote-session, v3-retry-401, v3-retry-408, v3-retry-409, v3-retry-429, v3-retry-500, v3-retry-502, v3-retry-503, v3-retry-529, v3-retry-after-date, v3-retry-after-seconds, v3-retry-limit, v3-session-fork, v3-session-resume, v3-stream-404-disable-flag, v3-stream-404-fallback, v3-stream-interrupt, v3-stream-interrupt-no-fallback, v3-thinking-disabled, v3-timeout, v3-tui, v3-workload + M` |
| `SPEC-BODY-016` | attribution 中 cch 匹配 5 位小写十六进制。 | 8/8 request；8 条推理请求 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-017` | CLAUDE_CODE_ATTRIBUTION_HEADER=false 时移除首个 billing attribution system block，其余 system block 前移且内容保持。 | 2/2 request；v3-attribution-disabled 与基线 | `messages-inference` | `R-v3-attribution-disabled, v3-baseline + M` |
| `SPEC-BODY-018` | 存在前序请求关系的续轮、resume 与 fork 请求携带 cc_prev_req=req_<标识>；首轮省略。 | 12/12 request；旧续轮样本与 v3-session-* 正负例 | `messages-inference` | `R-a1, s1, s2, s4, v3-session-fork, v3-session-resume + M` |
| `SPEC-BODY-019` | 仅子代理请求在 attribution 中携带 cc_is_subagent=true。 | 8/8 request；1 条子代理正例与 7 条非子代理负例 | `messages-inference` | `R-a1, s1, s2, s4 + M` |
| `SPEC-BODY-032` | non-stream fallback 省略 stream 顶层键、把 X-Stainless-Timeout 从 600 改为 300，重新生成 request-id 与 attribution cch；其余 Body 语义与会话身份保持。 | 3/3 fallback-transition；三个产生 non-stream fallback 的隔离故障运行 | `messages-inference` | `R-v3-stream-404-disable-flag, v3-stream-404-fallback, v3-stream-interrupt + M` |
| `SPEC-BODY-039` | workload 条件成立时 billing attribution 在 cch 后追加 cc_workload=<受控值>;，未设置时省略。 | 3/3 request；v3-workload、v3-header-combination 与基线负例 | `messages-inference` | `R-v3-baseline, v3-header-combination, v3-workload + M` |
| `SPEC-BODY-040` | CLAUDE_CODE_EXTRA_BODY.max_tokens 与 CLAUDE_CODE_MAX_OUTPUT_TOKENS 均把 max_tokens 从 64000 覆盖为整数 2048，其他基础字段保持。 | 3/3 request；v3-extra-body、v3-max-output-tokens 与基线 | `messages-inference` | `R-v3-baseline, v3-extra-body, v3-max-output-tokens + M` |
| `SPEC-BODY-041` | CLAUDE_CODE_DISABLE_THINKING=1 同时省略 thinking 与 context_management，顶层其余字段保持顺序。 | 2/2 request；v3-thinking-disabled 与基线 | `messages-inference` | `R-v3-baseline, v3-thinking-disabled + M` |
| `SPEC-BODY-042` | 在当前 Sonnet 5 范围，CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1 不改变 thinking、context_management 及其余非动态 Body 语义。 | 2/2 request；v3-adaptive-thinking-disabled 与基线 | `messages-inference` | `R-v3-adaptive-thinking-disabled, v3-baseline + M` |
| `SPEC-BODY-043` | 启用请求 gzip 时在 request-id 后插入 Content-Encoding:gzip；Content-Length 等于 gzip wire 字节数，且解压后是完整可解析的目标 JSON 结构。 | 1/1 request；v3-gzip-request | `messages-inference` | `R-v3-gzip-request + M` |
| `SPEC-BODY-044` | --system-prompt 使用 attribution、产品身份和自定义文本三个 system block，后两块使用 1h ephemeral cache_control。 | 1/1 request；v3-custom-system | `messages-inference` | `R-v3-custom-system + M` |
| `SPEC-BODY-045` | --append-system-prompt 生成四段 system：attribution、CLI-within-SDK 产品身份、核心提示和动态提示；追加文本放入最后一个 1h ephemeral block。 | 2/2 request；v3-append-system | `messages-inference` | `R-v3-append-system, v3-baseline + M` |
| `SPEC-BODY-046` | --exclude-dynamic-system-prompt-sections 只改变最后一个动态 system block，保留前述 block、顺序与 cache_control。 | 2/2 request；v3-exclude-dynamic-system 与基线 | `messages-inference` | `R-v3-baseline, v3-exclude-dynamic-system + M` |
| `SPEC-BODY-047` | 获准自定义顶层 agent 使用 attribution、产品身份、自定义 agent prompt 三段 system，后两段使用 1h ephemeral cache_control。 | 1/1 request；v3-custom-agent | `messages-inference` | `R-v3-custom-agent + M` |
| `SPEC-BODY-048` | 真实 TUI 会先发 Haiku 标题请求：固定标题模型、32000 max_tokens、thinking disabled、temperature 1、JSON Schema output_config、空 tools 与单 user message。 | 1/1 request；v3-tui 的标题请求 | `messages-inference` | `R-v3-tui + M` |
| `SPEC-BODY-049` | 真实 TUI 的 Sonnet 主请求使用 cli attribution、TUI beta 序列、单 user message和 TUI 四段 system 形态。 | 2/2 request；v3-tui 的主推理请求 | `messages-inference` | `R-v3-tui + M` |
| `SPEC-BODY-050` | fallback model 请求切换为 claude-haiku-4-5、max_tokens 32000、enabled thinking budget 31999、Haiku beta 与单 user message，省略 output_config，并保持会话身份。 | 4/4 request；v3-fallback-model 的第四次 messages | `messages-inference` | `R-v3-fallback-model + M` |
| `SPEC-CACHE-005` | DISABLE_PROMPT_CACHING=1 或 Sonnet 专用禁用条件均移除全部 system cache_control；其他 block 内容保持。 | 3/3 request；v3-cache-disabled、v3-cache-sonnet-disabled 与基线 | `messages-inference` | `R-v3-baseline, v3-cache-disabled, v3-cache-sonnet-disabled + M` |
| `SPEC-CACHE-006` | 当前 Sonnet 基线默认即使用两个 1h system 缓存点；ENABLE_PROMPT_CACHING_1H=1 与基线的 system 文本及 cache_control 形态相同。 | 2/2 request；v3-cache-one-hour 与基线 | `messages-inference` | `R-v3-baseline, v3-cache-one-hour + M` |
| `SPEC-META-001` | CLAUDE_CODE_EXTRA_METADATA 为合法对象时参与 metadata.user_id 内嵌 JSON 构造。 | 2/2 request；v3-extra-metadata | `messages-inference` | `R-v3-baseline, v3-extra-metadata + M` |
| `SPEC-META-002` | 额外 metadata 以浅合并方式置于 device_id、account_uuid、session_id 之前，嵌套对象原样保留。 | 1/1 request；v3-extra-metadata | `messages-inference` | `R-v3-extra-metadata + M` |
| `SPEC-STATE-005` | safe-mode 下未获准的自定义 agent 在本地拒绝，完整 relay 中 messages 数为零。 | 1/1 run；v3-custom-agent-safe-mode | `messages-inference` | `R-v3-custom-agent-safe-mode + M` |
| `SPEC-STATE-006` | --resume 复用原 Session-Id，metadata.session_id 与 Header 同值，并携带历史角色序列和 cc_prev_req。 | 1/1 session-transition；v3-session-resume | `messages-inference` | `R-v3-session-resume + M` |
| `SPEC-STATE-007` | --fork-session 为第二次调用生成新 Session-Id，同时携带原会话历史角色序列和 cc_prev_req。 | 1/1 session-transition；v3-session-fork | `messages-inference` | `R-v3-session-fork + M` |
| `SPEC-TOOL-018` | --json-schema 把输入 schema 包装为唯一 StructuredOutput 工具，使用固定说明和 input_schema，output_config.effort 仍为 high。 | 1/1 request；v3-json-schema | `messages-inference` | `R-v3-json-schema + M` |
| `SPEC-BODY-051` | count_tokens Body 顶层严格按 model、messages、tools 排列，messages 与 tools 均为数组。 | 37/37 official-example；36 个 count_tokens 正例与基线负例 | `count-tokens` | `R-v4-tui-count-tokens, v4-replay-baseline + M` |
| `SPEC-BODY-052` | OAuth refresh Body 按 grant_type、refresh_token、client_id、scope 排列，grant_type 固定为 refresh_token；凭据只保留等长脱敏 R。 | 2/2 official-example；隔离 OAuth refresh 正例与普通推理负例 | `oauth-token-refresh` | `R-v4-oauth-refresh, v4-replay-baseline + M` |
| `SPEC-BODY-053` | 项目 CLAUDE.md 的受控指令文本进入 messages 上下文；无该 fixture 的基线不含该文本。 | 2/2 official-example；context fixture 正例与基线负例 | `messages-inference` | `R-v4-context-claude-md, v4-replay-baseline + M` |
| `SPEC-BODY-054` | 真实 TUI @file 附件把文件名和受控文件正文写入 user content；非附件基线无此内容。 | 3/3 official-example；TUI 附件正例与 sdk-cli 基线负例 | `messages-inference` | `R-v4-tui-attachment, v4-replay-baseline + M` |
| `SPEC-STATE-008` | Agent 深度 1、2、3 分别产生 1、2、3 个唯一 agent-id；所有子代理 attribution 携带 cc_is_subagent=true，前台基线无 agent-id。 | 7/7 official-example；三层 Agent 正例与前台负例 | `messages-inference` | `R-v4-agent-depth1, v4-agent-depth2, v4-agent-depth3, v4-replay-baseline + M` |
| `SPEC-STATE-009` | 官方 background 会话使用 x-app=cli-bg、cc_entrypoint=cli，并按 Haiku/Sonnet 后台请求形态发送；前台 sdk-cli 使用 x-app=cli。 | 5/5 official-example；background 正例与 sdk-cli 前台负例 | `messages-inference` | `R-v4-background, v4-replay-baseline + M` |
| `SPEC-STATE-010` | PreToolUse hook 返回的 additionalContext 进入后续 messages；普通 Bash 负例不含该上下文。 | 4/4 official-example；hook 正例与 Bash 负例 | `messages-inference` | `R-v4-hook, v4-bash + M` |
| `SPEC-TOOL-019` | Agent tool_use 与同 ID tool_result 成对，并派生带 agent-id 的子代理请求；无工具基线不含 Agent。 | 2/2 official-example；Agent depth1 正例与基线负例 | `messages-inference` | `R-v4-agent-depth1, v4-replay-baseline + M` |
| `SPEC-TOOL-020` | Bash tool_use 与同 ID tool_result 进入续轮；无工具基线不含 Bash。 | 2/2 official-example；Bash 正例与基线负例 | `messages-inference` | `R-v4-bash, v4-replay-baseline + M` |
| `SPEC-TOOL-021` | stdio MCP tools 以 mcp__claude-fw-f-v4__ 前缀及 input_schema 进入 messages，并完成同 ID tool_use/tool_result 往返。 | 2/2 official-example；MCP tool 正例与无工具基线负例 | `messages-inference` | `R-v4-mcp-tool, v4-replay-baseline + M` |
| `SPEC-TOOL-022` | deferred MCP 场景完整暴露 32 个 deferred_probe 工具和 probe_echo，目录不得按首尾样本截断。 | 34/34 official-example；33 个 MCP 工具正例与无工具基线负例 | `messages-inference` | `R-v4-mcp-deferred, v4-replay-baseline + M` |
| `SPEC-TOOL-023` | advisor 仅在显式启用时以 type=advisor_20260301、model=claude-fable-5 进入 tools；默认官方负例省略。 | 2/2 official-example；advisor 显式正例与默认负例 | `messages-inference` | `R-v4-advisor-enabled-positive, v4-advisor-default-negative + M` |
| `SPEC-TOOL-024` | WebSearch 外层工具调用派生单独的 server web_search 请求；该请求携带 web_search tool descriptor 与 tool_choice。 | 4/4 official-example；web_search 三请求正例与无工具基线负例 | `messages-inference` | `R-v4-web-search, v4-replay-baseline + M` |

<!-- FW-F-ACTIVE-RULES-END -->

分组计数为 25 + 33 + 52 = **110**。任何新增、删除、改写或 egress 迁移都必须取得新的目标 P／R／M、
更新独立正负断言并签发新的 ApprovalFact，不能直接编辑列表。

## 2.6 发现项、候选与历史材料的终态

FW-E 的 7,368 个发现项没有删除。FW-F v21／v5 追加终态账本并达到：

- 7,368／7,368 个 discovery 均有已解决记录，缺失、额外、重复、空绑定和孤儿引用均为 0；
- 331 个目标发送点、102 个 2.1.88 源码机制、71 个 HitCC 线索、57 条历史规则与 32 个语义候选族
  组成 593 个正交候选，593／593 均有唯一终态；32／32 个语义候选族也全部闭合；
- 4,523 个历史上下文原子全部归属：128 个 Markdown 导航由结构证据证明为非出站，4,395 个绑定到
  精确文档、标题和语义事实；
- FW-F v1 的 97 条机械规则提案全部撤回，活动数为 0；
- 发现项清零不等于把 7,368 项变成规则；只有本部分 110 条通过目标 P／R／M 正负断言的命题进入画像。

| 材料 | 当前职责 | 能否单独支撑 2.1.226 活动规则 |
|---|---|---|
| `claude-code-2.1.220-official/` | baseline fixture、历史差分和探针设计 | 否 |
| `claude-code-2.1.88/` | 老源码机制线索和版本漂移核对 | 否 |
| `hitcc-2.1.197/` | 发送面与条件分支的线索地图 | 否 |

历史 v1～v4 制品继续作为不可变审计历史；`discovery-clearance-v5-final/withdrawn-rule-proposals.json` 保存
97 条提案的逐项终态，不能被删除或重新作为活动规则消费。

## 2.7 机器复算与强制门禁

先对 v21 Campaign 复算 77 个场景、395 条请求、49 个维度和 593 个候选，动态生成原子规则，再执行
7,368 项发现清账。旧目录只读保留，每次复算必须写新目录：

```bash
python3 tools/official_client_capture/claude_fw_f_v21_finalize.py \
  --campaign-root local-analysis/fw-f/claude-code-2.1.226/complete-v21-78fae770cbb5 \
  --prior-measured-rules local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v3/measured-rule-ledger.json \
  --prior-candidate-resolutions local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v3/candidate-resolution-ledger.json \
  --output-dir local-analysis/fw-f/claude-code-2.1.226/final-v21-110-rules-pair-complete

python3 tools/official_client_capture/claude_fw_f_discovery_clearance.py \
  --discovery-inventory local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/semantic-closure-v1-e577e144a/discovery-inventory.json \
  --semantic-candidates local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/semantic-closure-v1-e577e144a/semantic-candidates.json \
  --rule-assessments local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/rule-assessments-v5-e577e144a/rule-assessments.json \
  --document-atoms local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/semantic-closure-v1-e577e144a/document-atoms.json \
  --egress-inventory local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/control-store-v3-e577e144a/objects/egress_disposition_inventory/47b1c1a62dbc4964cf3b4fca6101113b94e8cc9b26bffead9ec051ce6bb1848e.json \
  --measured-rules local-analysis/fw-f/claude-code-2.1.226/final-v21-110-rules-pair-complete/measured-rule-ledger.json \
  --candidate-dispositions local-analysis/fw-f/claude-code-2.1.226/final-v21-110-rules-pair-complete/candidate-disposition-ledger.json \
  --prior-rule-additions local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v1/rule-ledger-additions.json \
  --policy tools/official_client_capture/claude_fw_f_discovery_policy_2_1_226.json \
  --output-dir local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v5-final
```

退出条件固定为：

- `source_discovery_count = resolved_record_count = 7368`；
- `candidate_resolution_count = orthogonal_candidate_count = 593`，且 32 个语义候选族全部闭合；
- 本版本由原子化实测动态得到 `measured_rule_count = 110`，不得在策略中预设规则数；
- `withdrawn_v1_proposal_count = 97`；
- 全部 `gate_counts = 0`；
- 3 条 TLS 规则均严格绑定 P/M，107 条普通规则均严格绑定 R/M；
- 每条活动规则均有非空且已批准的 `egress_ids`、独立 `PAIR-*`、官方正例和非零官方负例；
- 八类 strict egress 均至少有规则，且 Approval 中分别绑定自身 SPEC 集合；
- 不存在 `unmeasured_feature_boundary`；指南、RuleLedger、Snapshot、EvidencePackage 与
  SupportEnvelope 的 110 个规则 ID 必须完全一致；
- telemetry、nonessential、usage、models、dispatch-id、usage-limit 的合法零流量不得生成规则。

回归测试入口：

```bash
python3 -m unittest \
  tools.official_client_capture.tests.test_claude_fw_f_measured_rules \
  tools.official_client_capture.tests.test_claude_fw_f_discovery_clearance \
  tools.official_client_capture.tests.test_claude_fw_f_profile \
  tools.official_client_capture.tests.test_claude_fw_f_v3 \
  tools.official_client_capture.tests.test_claude_fw_f_v4 \
  tools.official_client_capture.tests.test_claude_fw_f_complete_runner
```

## 2.8 后续版本的更新方法

Claude Code 换版时必须新建 Campaign，并重复以下顺序：

1. 冻结最新 stable 的官方产物、二进制、平台、入口、账号类型、模型和隐私配置；
2. 以目标 bundle 原生发现为主，使用 2.1.220、2.1.88、HitCC 和前一批准版本只补线索，不继承结论；
3. 对每个拟活动命题构造可达正负场景；真实上游 run 在 Vircs 采集，故障语义在隔离 relay 注入；
4. 采集目标 R/M；涉及 ClientHello／ALPN 时另采原生 P；
5. 运行逐规则断言，只把通过断言的 request-egress 命题写入新的 MeasuredRuleLedger；
6. 对全部发现、候选和旧规则逐项 disposition，未决数必须为 0；
7. target-first 生成新 Snapshot／Release，再用同一 Schema／Compiler 表达历史 fixture；
8. 证据不足的能力留在明确边界，不得补造规则、缩小数字或用旧版本 wire 提升等级。
# 第三部分 Sub2API 实现与迁移

本部分只记录 Claude 方言和当前代码差距；共享控制契约见 Framework。硬性目标是：画像能表达的
换版只追加画像、证据和发布图，不修改执行代码；只有新协议、新状态机制或 Schema 缺口才能修改
共享引擎，并回归受影响 Persona 的 production active／rollback 与 validation candidate。

## 3.1 Persona 与执行链

```text
官方 Claude Code／第三方标准 API
→ IngressProtocolAdapter → CanonicalRequest + TranslationReport
→ Claude PersonaPlanner + ClaudeIdentityFacts + ClaudeEgressPlan
→ Claude active ReleaseArtifact／ReleaseBundle
→ Claude DialectCompiler → CompiledEnvelope
→ Claude Executor authority 实例 + FinalizationToken + 受信 adapter
→ Runtime Guard → Anthropic
```

入站只提交协议、模型、工具、语义和受信条件；Key、Group、账号路由与计费仍由业务系统管理。
调用方不能选择 Persona、生产版本、transport ID 或最终 wire。只有 `TranslationReport=lossless` 且
请求位于本 Release 的 `SupportEnvelope` 内，才能进入 strict Compiler；范围外请求必须 fail-close。

FW-E 的 `ProductionIngressInventory` 至少从以下当前已知逻辑入口开始，不能把本表当作已经完成的物理
路由闭集：

| 逻辑入口 | 盘点要求 |
|---|---|
| `/v1/messages` | 记录 Anthropic Messages 语义适配器、全部前缀／路由别名和 Claude OAuth 调用点 |
| `/v1/chat/completions` | 记录 Chat Completions 到 CanonicalRequest 的映射、拒绝条件和全部物理别名 |
| `/v1/responses` | 记录 Responses 的 HTTP／其他实际可达形态、子路径和全部物理别名 |
| `/v1/messages/count_tokens` | 作为独立生命周期入口和出站目的盘点，不从 `/v1/messages` 的结论自动继承 |

只有最终路由到 `claude-code` firstParty OAuth 的调用才属于本闭集；同名 API Key、Antigravity 或其他
Persona 路径必须标记为 `rerouted` 并指向其真实产品边界。代码中的前缀别名、WebSocket／compact
分支、内部调用和 handler 转发均须展开到物理入口，不能只登记上述四个字符串。每项同时记录当前处置
和目标处置；strict 链实际接管前保持 `retained_legacy`。

| persona | 边界 |
|---|---|
| `claude-code` | firstParty OAuth；由同一 Bundle 驱动 URL、Header、Body、状态和 transport |
| API Key mimic | 独立产品路径，禁止套用 Claude Code 画像 |
| `transport_only` | OAuth code 交换，只复用已举证的传输事实 |
| `unclassified`／未登记 | 不得凭官方 host 自动归类；enforce 时 fail-close |

| 入站事实 | 处理 |
|---|---|
| 用户消息、system、工具、模型和 stream 语义 | 无损进入 `CanonicalRequest`；角色、顺序和内容变化必须反映在 TranslationReport |
| Persona 固有 system blocks | 只能由 active 画像和官方规则派生；不得覆盖或冒充用户 system 语义 |
| metadata、device、session、agent | 只能由 Identity Authority 从受信锚点派生，并记录 source、reason、scope、lifecycle 和冲突处置 |
| UA、版本、`x-app`、Stainless | 入站值不拥有 wire 身份；由 active ReleaseBundle 提供或按其规则派生 |
| 身份冲突 | 丢弃冲突值，从同一受信生命周期派生 |
| 条件缺失／冲突 | 按条件不成立处理，不伪造 Header 或空值 |
| 画像闭集外字段、未消费 Header | 删除并去重告警，不透传上游 |
| `system → user message` 等角色改写 | 不是无损映射；strict 拒绝，只能在单独批准的 compatibility 模式记录并隔离 |

当前 `gateway_claude_oauth_body.go` 注入 Persona system、重排用户 system，以及
`official_egress_anthropic.go` 派生 device／session 的实现属于待迁移遗留语义层。Persona 固有内容和
身份派生可以保留其有证据的意图，但必须迁入上述权威来源与派生记录；角色重排不能以 `lossless` 进入
strict。现有输出只用于 FW-A 基线记录、FW-E 盘点和影子诊断，不是目标 stable 的正确性证据。

## 3.2 内容寻址画像

Claude 画像目标路径为：

```text
backend/internal/officialegress/catalogdata/claude/profiles/<version>/<digest>.json
```

`version + digest` 是不可变 ReleaseArtifact 坐标；加载、Schema 或摘要校验失败时，该 Claude Persona
不得获得可执行 route。运行时只读，按请求修改的数据必须深拷贝。Discovery、Evidence、Approval、
Validation candidate、production active／rollback 和实际 Deployment 是正交事实，不能合并成一个
遗留 `active／previous` 状态枚举。

| 画像段 | 责任 |
|---|---|
| `Version`／`RequiredRules`／`SupportEnvelope` | 版本身份、UA 来源、目标 SPEC 集合、平台／入口／功能范围和范围外拒绝条件 |
| `Transports` | 有证据的 TLS、ALPN、SNI、HTTP 和连接行为；未证明字段保持缺失 |
| `Endpoints` | method、host、path、query、内容类型、压缩和 client 生命周期闭集 |
| `HeaderSlots` | WireName、值来源、条件、互斥组和相对顺序 |
| `BetaPolicy` | beta 序列、插入位置和条件项 |
| `BodyShape` | 顶层键、system、cache_control 与 metadata 编码 |
| `RetryPolicy` | 起步、指数、封顶、抖动与终止边界 |
| `PrivacyMode`／`Digest` | 证据隐私模式与画像摘要 |

条件 Header 必须由槽位表达，不得散落为版本 `if`。槽位至少包含 `Slot`、`Name`、`WireName`、
`Value`、`Source`、`Condition`、`AlternateGroup`；条件只读取规范化语义和受信上下文，不能读取
第三方同名 Header。条件不成立时整条省略。

ReleaseArtifact Store 保存全部不可变 Release；Runtime Selector 只保存 Claude production active／rollback
引用，Validation candidate 使用独立的 immutable release reference，不借用 rollback。Claude 首次激活前
必须提供可验证的真实回退目标，可以是上一正式 Release 或冻结的遗留实现；不得复制 active 摘要伪造
回滚对。新增版本只追加快照与发布图节点。版本泄漏 baseline 只登记既有债务，不能通过新增版本常量
换取门禁通过。

Claude 每次生产切换还必须分别冻结以下范围，不能只在 candidate 上写一个 SupportEnvelope：

| 范围 | Claude 口径 |
|---|---|
| `ActiveSupportEnvelope` | 将成为或已经成为 production active 的 Release 经批准并通过 strict 断言的范围 |
| `RollbackOperationalEnvelope` | rollback 镜像、selector、配置、依赖和入口已真实演练可运行的范围 |
| `DeploymentTrafficEnvelope` | 本次实际从遗留链切入 Claude strict active 的生产流量范围 |

必须满足：

```text
DeploymentTrafficEnvelope
⊆ ActiveSupportEnvelope
∩ RollbackOperationalEnvelope
```

2.1.220 当前只作为同一 Schema／Compiler 下的 baseline fixture，并没有 strict rollback ApprovalFact；
因此不能把它声明为目标 stable 全范围的 strict rollback。未来若为它取得独立批准与回退演练，只能在
其自身证据覆盖的窄范围内使用，并同步收窄 DeploymentTrafficEnvelope；也可使用 FW-A 冻结的遗留部署
承担 operational rollback，但遗留 wire 始终是 diagnostic-only。

## 3.3 Claude 方言终态合同

`ClaudeIdentityFacts` 绑定账号、会话、agent、平台和入口事实；版本、UA、`x-app`、entrypoint 与
Stainless 向量来自 ReleaseBundle。`DialectCompiler` 只允许画像声明的 URL、Header 与 Body，并把
结果封装为最小 `CompiledEnvelope`：

- URL 拒绝 opaque、userinfo、fragment、显式端口和非精确 `https`，host／path／query 必须命中闭集；
- HTTP/1.1 写出前定型 Header 大小写、顺序、host 和长度；
- Body 保持键闭集、稳定顺序、JSON 数值、system、cache_control 与 metadata 契约；
- 会话状态按 invocation 隔离，无可信锚点时不得跨请求复用上游状态；
- `persona_strict` 的连接、重试和端点只从画像执行；`non_persona_managed` 只从已登记管理策略执行，
  两者均不得在业务代码或裸 client 中旁路。

当前已知 Claude OAuth 出站按以下初始口径进入 FW-E／FW-F 清单；后续官方证据可以通过新的
ApprovalFact 收窄或晋升，但不能静默换类：

| 出站族 | 初始处置 | 要求 |
|---|---|---|
| `POST /v1/messages?beta=true` 推理及 `HEAD /api/hello` 生命周期探测 | `persona_strict` | 纳入画像、SPEC／PAIR、SupportEnvelope 和 Guard |
| `GET /api/claude_code/policy_limits`、`GET /api/claude_code/settings`、`GET /api/oauth/profile` | `persona_strict` | 作为三个独立 egress 纳入画像、SPEC／PAIR、SupportEnvelope 和 Guard |
| `/v1/messages/count_tokens`、`POST platform.claude.com/v1/oauth/token`、`GET /v1/mcp_servers?limit=1000` | `persona_strict` | v21 已取得正负例并纳入独立 egress、SPEC／PAIR、SupportEnvelope 和 Guard；当前只批准 validation-only |
| usage、OAuth exchange、cookie authorize／organizations、account test、upstream models，以及遗留 token-count／OAuth-refresh 别名 | `non_persona_managed` | 登记 route／Sink、认证、endpoint、client、超时、重试、秘密与审计，不把遗留别名冒充新的 official-client strict 身份 |
| 未登记 Claude OAuth 路径 | `denied` | enforce 时 fail-close |

`non_persona_managed` 是受管第三态，不是 `out_of_scope_passthrough`。它不计入当前 110 条活动规则及
SupportEnvelope 的逐规则分母，但必须有独立的 source-to-sink 闭集、运行断言和失败策略；未来若要求
仿真官方客户端在该端点的 wire，必须先取得证据、原子化规则并正式晋升为 `persona_strict`。

Envelope 只向共享 Executor 暴露 Persona／Release 摘要、Route／Sink、method／protocol、attempt、Body
可重放性、TransportCapability、Identity／Dialect attestation digest 和受保护请求能力。共享层不得
解释 Claude beta、Header 槽位、BodyShape、agent 层级、Stainless 或重试语义，也不得要求 Claude 填充
Codex IdentityMode、HeaderPolicy、BodyPolicy、BehaviorPolicy 或 fallback 字段。

| 规则组 | 数量 | 画像／执行落点 |
|---|---:|---|
| TLS／协议／端点／连接 | 25 | `Transports`、`Endpoints`、client lifecycle + adapter |
| Header／认证／Beta | 33 | `HeaderSlots`、`BetaPolicy` + Claude Compiler |
| Body／缓存／metadata／状态／工具 | 52 | `BodyShape`、IdentityFacts + Claude Compiler |

110 条均具有画像／执行落点、2.1.226 目标 P／R／M 和独立 `PAIR-<SPEC-ID>` 正负例。原生 TLS／ALPN、
故障重试、真实 TUI、Agent／background／hook、custom Header／beta／metadata、remote、附件、MCP、
advisor、web_search、count_tokens 与隔离 OAuth refresh 已纳入；合法零流量仍只作支撑事实。调度、计费
和服务级节奏不由画像改写。

## 3.4 当前差距与 FW-A～FW-H 迁移

当前只有遗留局部仿真，strict 画像化迁移尚未开始：

| 差距 | 当前事实 |
|---|---|
| 版本身份硬编码且散布 | `internal/pkg/claude/constants.go` 及多个业务文件读取 2.1.220、UA、Stainless、beta 常量 |
| 画像由 Go 构造 | `buildAnthropicClientProfileCatalog()` 不能内容寻址或追加换版 |
| 无 Claude Snapshot／发布图 | `catalogdata/` 只有 Codex；遗留 Claude `active／previous` 只是代码构造指针 |
| 无条件 Header Schema | 遗留画像只有 `StaticHeaders` 与 `BetaHeader`，无法表达当前实测的条件 Header 责任 |
| finalizer 位于 service | `official_egress_anthropic.go` 未进入共享 Executor／Token／Guard 终态链 |
| 无 Claude Persona／Compiler／Guard | 当前 `officialegress` persona 和 authority 仍是 Codex 专用 |
| 入口与出站目标尚未实现 | FW-F 已按 `validation_only` 批准 5 个逻辑入口和 16 项 OAuth 出站身份的目标处置（8 strict、8 managed）；当前运行态仍保持 FW-E 冻结事实 |
| 三方语义补全未受管 | Persona system／identity 派生与有损 system 角色改写混在遗留 body 重写器中 |
| 版本门禁不完整 | 版本注释已漂移，业务代码尚可继续泄漏版本指纹 |

| 阶段 | 变更 | 完成判据 |
|---|---|---|
| `FW-A` | 只读冻结 Codex、Claude 2.1.220 证据、生产运行态和遗留发送面基线；不新增 Claude 代码或绑定 | 两类基线可复算；遗留输出只标 diagnostic-only；没有生产写入 |
| `FW-B` | 按 Framework 抽取 Codex 已证明的暂定共享合同并保留 Codex facade | 共享内核无 Codex 专用策略字段；Codex active／rollback final wire 零差异；不宣称多 Persona 已冻结 |
| `FW-C` | 验证并发布 Codex-only 正式制品，完成回滚、恢复和稳定观察 | 本轮没有新增 Claude Persona／画像／strict 注册；Codex 发布与激活收据闭环 |
| `FW-D` | 建设 Campaign、正交事实、两段式批准、Snapshot／Release Store、candidate／PAIR、晋升与激活工具链 | 只用 Codex／合成数据自测；越权、摘要变化、范围缺口和收据不匹配均由机器阻断 |
| `FW-E` | 第一步冻结最新 stable；从目标 bundle 原生发现发送点，取 2.1.88、HitCC、2.1.220／前一批准 stable 与目标发现的并集，分开建立 DiscoveryInventory、SemanticRuleCandidate 和 RuleLedger，完成差分、P／R／J／M 和 Evidence 封存，再建立两个 Inventory 与 observation-only Sink | 目标版本、完整 sink／discovery inventory、语义候选、只含 SPEC 的规则台账和 EvidencePackage 可复算；没有截断或未分类项；停在 `evidence_recorded`，尚不定义目标 Schema／Snapshot 或签发 Evidence 批准 |
| `FW-F` | 先把 FW-E 全部发现和语义候选逐项收敛到可审计终态并清零未决项，再由最新 stable 证据生成 Schema、目标 Snapshot、Persona 和不可部署样例；随后用同一 Schema／Compiler 表达 2.1.220 rollback fixture，批准 Profile、范围和多 Persona 合同 | 全量 DiscoveryDispositionLedger 无缺失、重复或未决项；target-first 样例与跨 Persona 负例通过；ApprovalFact 完整；Codex 生产收据对应最终合同；selector 未改变 |
| `FW-G` | 实现本次已批准的 110 条最新 stable 实测规则，完成受管语义层、辅助出站三态、全部 strict 入口 PAIR、独立复测、DMIT candidate 和 rollback 验收 | 110 条规则达到后继 production-replacement ApprovalFact 要求；范围内断言、范围外拒绝和回退通过 |
| `FW-H` | 灰度、回滚、激活；逐入口迁移并在闭集完成后退休遗留链 | DeploymentFact 与运行态一致；无 `retained_legacy` 才能签发迁移完成的 RemovalReceipt |

最新 stable 是目标 Schema、Snapshot 和实现的唯一设计权威。FW-E 只冻结目标规则证据，不预先用
2.1.220 建画像；FW-F 完成发现项语义清零后，必须先生成目标 stable 画像和样例，随后才把 2.1.220 表达为差分基线、
conformance fixture 或受范围约束的 rollback。遗留 final wire 只用于盘点和诊断，不能决定任何画像
字段或提升证据等级。FW-F 两套 fixture 永久保留，但不得注册到 Codex-only 运行时镜像。

若 FW-G 的目标 stable 机制暴露真正的共享控制合同缺口，当前 candidate 作废，返回 FW-B 建立后继
合同，并重新执行 Codex 零差异、FW-C Codex-only 发布闭环及 FW-F 的 target-first／2.1.220 fixture；
不得在 Claude 方言中绕过或原位扩张接口。`constants.go` 若仍服务 API Key 产品语义，只退休
`claude-code` OAuth Persona 的版本面。

## 3.5 包边界、Guard 与策略

```text
service → officialegress core ← repository 窄 port
wiring → 闭集 adapter 注册
```

`officialegress` core 不得 import `service` 或 `repository`。Claude 与 Codex 只共享 Artifact Store、
Runtime Selector、编译注册、Executor 实现、Token、Guard 和 adapter 注册；Plan、IdentityFacts、
ProfileSchema、DialectCompiler 及 transport 参数独立。Codex 和 Claude 各自拥有 Executor authority、
Token issuer 和 invocation 状态；共享 Executor 实现只消费闭集 `CompiledEnvelope`。

Guard 按 route、persona、Sink、binding、Release、Profile digest、adapter、FinalizationToken 和
最终摘要校验，状态按 `legacy_observe → canary_enforce → enforced` 推进。未知 route、无效 binding
或终态篡改在 canary／enforced 必须 fail-close；`legacy_observe` 只允许在 FW-E 对已冻结遗留路径报告
事实，不能作为长期 `out_of_scope_passthrough` 或 strict 验收通过。版本、UA 和 Stainless 指纹只能
存在于画像及加载器。

重试、隐私模式、缓存、辅助请求和 client 生命周期均由画像或显式策略声明；响应 SSE 属下游兼容，
不能反向影响请求定型。

## 3.6 当前可执行边界

源码存在不能证明 production active。FW-A～FW-F 已完成：目标 stable 2.1.226 的 FW-E Campaign 为
`claude-code-2_1_226-fw-e-semantic-20260818-e577e144a`，其 Store 停在 `evidence_recorded`。旧 Campaign
`claude-code-2_1_226-fw-e-final-20260818-93f2edbc9` 及其“7,425 条规则”收据只保留为历史错误事实，由
[语义规则纠正收据](egress/maintenance/fw-e-semantic-rule-correction/receipt.json)替代，禁止继续作为
FW-F 输入。FW-F 后继 Campaign 为 `claude-code-2_1_226-fw-f-measured-profile-v4-20260819`，Store 已到
`profile_approved`：7,368 个发现、593 个正交候选和 32 个语义候选族未决数均为 0，2.1.226 目标画像／
Release 与 2.1.220 fixture 已按 target-first 顺序生成，EvidenceApprovalFact 和 `validation_only`
ProfileApprovalFact 已签发。110 条活动规则都有 Vircs 上 2.1.226 官方客户端的真实 P／R／M、非零正负例
且断言通过，但当前仍为 `observed`，所以没有
production-replacement ApprovalFact、candidate、Runtime Selector、DeploymentFact 或环境变更。事实见
[清零收据](egress/maintenance/fw-f-discovery-clearance/receipt.json)和
[FW-F 画像批准收据](egress/maintenance/fw-f-profile-approval/receipt.json)；下一步固定为 FW-G。

---

# 第四部分 Claude Code 换版 Campaign

Framework 定义通用 Campaign、candidate、验收、激活和回滚；本部分只补充 Claude 没有官方源码、
依赖生产 bundle 逆向的取证边界。

| 流程检查点 | Claude 产物 | 退出事实 |
|---|---|---|
| 1. 预检 | 目标版本、官方发行物、环境和生产隔离清单 | 身份完整且 Vircs 生产不受影响 |
| 2. 官方取证 | A1、P／R／J／M、规则差分 | EvidencePackage 已封存 |
| 3. 语义清零 | 覆盖全部发现和语义候选的 DiscoveryDispositionLedger | 原始发现保留，缺失、重复记录和语义未决项均为 0 |
| 4. 批准 | SupportEnvelope、两个 Inventory、Persona 派生、迁移决策、目标规则、画像、场景、断言与计划范围 | ApprovalFact 已绑定 DiscoveryDispositionLedger 并签发 |
| 5. Candidate | 独立 Release 引用、源码、测试、镜像与 inventory | ValidationCandidate 已封存 |
| 6. 比较验收 | strict 入口的逐规则结果、非迁移入口隔离和 managed 出站断言 | AcceptanceFact 或 validation-only 结论 |
| 7. 生产闭环 | 三个 Envelope、晋升、正式镜像、canary、切换、回滚、恢复和收据 | DeploymentFact 达到 `restored_active` |

## 4.1 身份、正交事实与当前状态

Campaign、candidate、attempt 的通用边界见 Framework §3.2。Claude 还必须冻结：

| 身份维度 | 要求 |
|---|---|
| 官方产物 | npm URL、integrity、tgz／二进制／bundle SHA、内嵌 Bun／SDK、平台与架构 |
| 隐私模式 | `essential-traffic`／`no-telemetry`／`default` 不得混用 |
| entrypoint | `sdk-cli` 与真实 TTY 的 `cli` 分开取证；`-p` 不能伪造 TUI |
| 平台角色 | Linux x86_64 是当前主基准，Darwin arm64 只作交叉复核 |
| 工具身份 | 采集、relay、脱敏、提取、编排、收据和环境快照的摘要 |
| 账号与条件 | OAuth 组织、模型、feature、scope、故障注入和场景矩阵 |

Campaign 只是绑定目标版本、官方产物和环境身份的不可变容器，不使用单一状态枚举同时表达证据、批准、
验证和部署。以下事实分别追加并通过摘要引用，不得互相替代：

| 维度 | Claude Campaign 必须记录的事实 |
|---|---|
| Discovery | 发现版本、发现时间和来源；不得改变任何 Runtime Selector |
| Evidence | 每条规则独立的 `verified／observed／blocked／regressed_evidence` 与 EvidencePackage |
| Approval | 固定的 SupportEnvelope、两个 Inventory、Persona 派生、三个 Envelope 计划、目标规则全集、迁移决策、画像和验收清单 |
| Validation | 独立 candidate ID、不可变 Release 引用、源码／测试／镜像摘要和 AcceptanceFact |
| Runtime Selector | Claude `production_active` 与 `production_rollback`；不保存 validation candidate |
| Deployment | `accepted_not_activated → canary_passed → active → rollback_verified → restored_active`、实际入口处置、三个 Envelope 与收据 |

`official_sealed`、`profile_approved`、`candidate_sealed`、`compared` 和 `ready` 只可作为上述事实齐备后
计算出的流程检查点，不能作为规则证据等级或生产角色。`ready`、最大 candidate 编号或测试通过都不
等于 production active。

| 能力 | 当前状态 |
|---|---|
| bundle 提取、锚点和 sink 窗口 | 已实现；窗口不是数据流证明 |
| 目标 AST／词法 sink 并集与四方矩阵 | 已实现；发现、语义候选和 SPEC 三层隔离；语义审查后的目标新增机制可显式生成 `add`，任何未分类项阻断封存 |
| 全 host／path 运行 inventory | 2.1.226 Campaign 已关闭 host 预筛；v21 以 Vircs 官方运行闭合 8 类 strict egress；换版必须新建 Campaign |
| 规则／覆盖门禁和完整场景矩阵 | 已实现；77／77 场景、395 条请求、49／49 维度均完整 |
| P／R 通道与条件 Header 探针 | 已实现；TLS 使用原生 P，普通规则使用 R |
| TUI／`cli` 驱动 | 已以 Vircs 真实 TTY 纳入 v21；与 `sdk-cli` 分开绑定 entrypoint、模型和证据 |
| Campaign 编排、正交事实账本和两段式批准 | FW-D 已实现；FW-F 已签发 EvidenceApprovalFact 和 `validation_only` ProfileApprovalFact |
| 发现项与语义候选终态清零 | 7,368 个发现、593 个正交候选和 32 个语义候选族均已逐项收敛，全部清零门禁为 0 |
| Claude Snapshot、ReleaseArtifact Store 和画像暂存 | 2.1.226 目标 Snapshot／Release 与 2.1.220 fixture 已按同一 Schema／Compiler 封存 |
| candidate 冻结、四阶段封存和逐规则断言 | 通用能力已实现；当前批准仅为 `validation_only`，Claude candidate 为 0 |
| 第三方入口集合 | 5 个逻辑入口和 14 个物理别名的当前事实已封存；各入口目标处置已在 validation-only 批准中冻结 |
| Persona 派生与 compatibility 语义账本 | Planner、Plan、Identity、Compiler、transport 及有损入口边界已批准，具体代码留给 FW-G |
| 辅助出站三态与运行断言 | 16 项已知 OAuth 出站身份的目标三态已批准：8 strict、8 managed；当前运行态仍保持 `legacy_observe`，FW-G 才实现与验收 |
| 晋升、正式镜像和 production active／rollback 收据 | 通用能力已实现；Claude 尚无晋升、激活或部署事实 |

FW-F 已按 `validation_only` 闭合，不等于生产就绪。下一步固定为 FW-G：实现 110 条目标画像规则，用
独立官方运行与候选对拍把所需规则升级到 `verified`，签发后继 `production_replacement` ApprovalFact，
再建立并在 DMIT 验收 candidate。范围外能力继续 fail-close 或保持受管／遗留处置。

## 4.2 官方取证、分类与批准

### 4.2.1 P0 与官方证据

开始前只读确认：目标版本未混入当前基线、官方包来源可复算、Vircs 生产容器健康且不会被采集改动、
证据目录和端口隔离、DMIT 可承载 candidate、ARM64 构建职责已核实。身份或恢复条件不完整时停止。

FW-E 的第一项动作必须是查询官方 `stable` 并冻结版本、发行物和环境身份；在此之前不得定义 Claude
目标 ProfileSchema、Snapshot 或 candidate。2.1.220 只用于随后差分，不是目标版本选择权威。

Claude 没有可用的目标版本官方源码，必须以官方生产 bundle 为主：

1. 验证来源、integrity、二进制与 bundle 摘要，确定性提取 Bun SEA；
2. 解析目标 bundle 的全部文本模块，从入口独立枚举网络 sink、请求构造、Header／Body 写入、环境与
   feature gate、端点、重试和跨请求状态；不得只围绕旧规则或固定字面量探针搜索；
3. 取 2.1.88 真源码候选、HitCC 线索、2.1.220／前一批准 stable 规则和目标原生发现的并集，逐项比较
   语义锚点、source-to-sink、条件和运行依赖；
4. 由静态条件反向生成 baseline、条件 Header、agent、retry、OAuth、端点、异常和 entrypoint 哨兵；
5. 全 host／path 观测官方进程出站，不得用待证 endpoint 预筛；每个 run 同时封存 P／R／J／M、实际
   argv／环境、工具摘要、宿主回执、秘密扫描和恢复事实；
6. 产物、平台、隐私模式或产出侧工具变化时新建 Campaign，临时网络失败才新建 attempt。

官方 Bun SEA 主模块是无 sourcemap 的 minify JavaScript，但语法完整且承载实际生产控制流；它不是
原始 TypeScript，却是目标客户端行为的直接静态权威。词法窗口、附近 sink 和 minify 符号只能用于
定位，不能代替完整语法树、调用关系、反向数据流和运行闭环。Bun／原生模块、动态调用或宿主 transport
无法由 JavaScript 独立证明时，必须登记为显式边界并由进程级、网络级运行证据补足。

### 4.2.2 目标原生闭集与跨来源矩阵

每次换版都先生成目标 `SinkInventory`，再生成规则迁移台账。首次受管 Campaign 的永久历史语料是
2.1.88 真源码、HitCC 2.1.197 和 2.1.220 官方 bundle；后继 Campaign 另加入最近批准 stable，但目标
规则全集始终由下列并集决定，不由任一旧台账决定：

```text
HistoricalSourceCandidates
∪ HitCCClues
∪ HistoricalOfficialRules
∪ PreviousApprovedStableRules
∪ TargetNativeDiscoveries
```

跨来源闭集分三层保存，禁止混算数量：

1. `DiscoveryInventory` 无截断保存目标 AST 调用、2.1.88 原子命题、HitCC clue 和 Markdown 原子；
2. `SemanticRuleCandidate` 用 `source_ids` 把同一语义的多个发现归并为稳定的 `CAND-*` 工作项；
3. `RuleLedger` 只保存已经原子化、可执行并可逐项断言的 `SPEC-*`。

一个发现跨越多个语义时可以引用多个候选，但发现与候选必须双向闭合。目标原生发现没有历史对应项，
也不能直接按发现 ID 创建 SPEC；只有语义审查明确 retained claim、适用条件、所需通道、场景和断言后，
才能显式新建 SPEC 并分类为 `add`。历史规则在目标不可达时仍保留编号，并以静态不可达和运行负例共同
支持 `delete`。

证据不足不等于允许永久保留 `unclassified`。已取得稳定身份和原始证据、但尚不能证明目标语义或 wire
的发现使用 `mapped_validation` 归入语义候选；候选保留真实 `observed／blocked`，固定
`rule_ledger_membership=denied` 和 `production_eligibility=denied`，没有规则迁移决策，也不得自动生成
SPEC。目标 AST 的 `observed` 只表示精确调用在目标 bundle 中存在；2.1.88／HitCC 的 `blocked` 只表示
历史命题已登记。两者都不能进入 production SupportEnvelope。

`SinkInventory` 至少覆盖 `fetch`、Anthropic SDK resource 调用、Node／Bun HTTP、TLS、socket、
WebSocket／EventSource、动态 wrapper 和可能启动外部网络客户端的子进程。每个 sink 均须保存完整列表，
禁止数量上限或抽样；同一低层 sink 的多个调用点可归并为一条规则，但每个调用点都必须反向引用唯一
disposition。下列任一条件均阻止 FW-E 退出：

1. 目标 sink 未分类、被截断、重复身份冲突或没有可复算来源；
2. 2.1.88 候选、HitCC 直接线索或历史官方规则没有唯一处置；
3. 已完成语义审查的目标新增机制无法显式生成 `add`，或工具把发现／`CAND-*` 自动混入 RuleLedger；
4. strict 候选缺少目标版本运行场景，或运行观测出现 inventory 外 host／path／sink；
5. `DISABLE_TELEMETRY`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 的实际值、读取点和 gate 未绑定
   本次目标产物。关闭后的 telemetry／nonessential 分支仍登记可达性和 `record_only` 处置，其不发流量
   不计一致性差异，但不得因此删除可能影响 essential 请求构造的共享状态。

现有三份历史机器台账只证明其自身路径、ID 和当前映射一致，不自动证明语义穷尽。2.1.88 必须覆盖
`src／node_modules／vendor` 的网络相关反向切片；HitCC 每篇 `clue_source` 文档必须映射到一条或多条
原子命题，或以可复算理由标为范围外。尚未抽成 clue 的文档必须无截断枚举非代码区 Markdown 列表项，
保存原文路径、行号、最近 heading、内容摘要和稳定发现 ID，再逐项归入现有语义候选、既有 SPEC 或
可追溯的 `catalogued_context`；后者只表示当前固定词表没有给出规则映射，不是范围外证明，已映射文档
反向绑定其 clue。扫描器无法作出语义判断时先保留
`unclassified`，不得按词法未命中自动排除；只有发现身份、证据、处置及候选双向引用同时闭合后，才能
把它改为 `mapped_validation`，且不得因此新增规则。

`catalogued_context` 只允许作为 FW-E 的不可变取证结果，不是 FW-F 的终态。FW-F 必须在不改写该结果的
前提下追加 `DiscoveryDispositionLedger`，覆盖包括 `catalogued_context` 在内的全部发现；任何“下次换版
再判断”的项目都计入未决并阻止画像批准。

机器执行顺序固定如下，`analyze-bundles` 会对每个平台自动运行锁定 TypeScript 解析器，并把 AST
调用点与无截断词法候选合并为 `target-sink-inventory.json`：

```text
claude_fw_e.py analyze-bundles
→ claude_fw_e_validation_closure.py
→ claude_fw_e_crosswalk.py --capture-index ... --require-closed
→ claude_fw_e.py rule-assessments --cross-source-matrix ... --completeness-closure ...
→ claude_fw_e.py seal
```

第二步的 dispositions 必须分别覆盖目标 sink、2.1.88 候选、HitCC 线索／文档和全 host／path 运行
观测。`traffic_class=nonessential／telemetry` 只能使用 `record_only_disabled` 或保持
`unclassified`；不得用 `delete`、范围外或零流量事实静默移除。`seal` 使用 v3 计划并直接绑定目标
inventory、矩阵、closure 和 capture index；closure 非 `passed`、运行样本仍使用 host 预筛、四者摘要
不一致，或 DiscoveryInventory／SemanticRuleCandidate／RuleLedger 三层数量和双向引用不闭合时，Store
不得写入 `evidence_recorded`。语义候选只封存发现与待验证语义，不签发 EvidenceApprovalFact 或
ProfileApprovalFact，也不允许 FW-F／FW-G 把它当成已批准规则。

补强工具或产出侧身份发生变化后，旧 Campaign 及旧 FW-E 收据只保留为历史事实，不能原位续写或补签。
必须在隔离目录新建 Campaign，并同时设置 `DISABLE_TELEMETRY=1`、
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 和全 host 捕获开关；不得用旧的目标-host 预筛样本
冒充闭集证据。

### 4.2.3 分类与批准清单

FW-F 必须先完成发现项语义清零，再设计目标画像。允许按语义聚类提高审查效率，但每个
`discovery_id` 必须有且只有一个已解决记录，并绑定已审查的规范簇及下列至少一种终态；跨越多个语义
时可以保存多个终态绑定，不能用关键词未命中批量排除：

| 终态 | 必须绑定 |
|---|---|
| `rule_bound` | 一个或多个既有／新建 `SPEC-*`，以及该发现对规则的证据角色 |
| `supporting_fact_bound` | 所属规则、画像事实或状态／条件／动态值事实及其稳定身份 |
| `managed_egress_bound` | `EgressDispositionInventory` 中的受管出站身份和处置 |
| `non_egress_proven` | 目标证据、可复算理由及为何不影响客户端出站 |
| `target_absent_proven` | 目标 stable 的静态不可达／不存在证据；需要时补运行负例 |
| `duplicate_bound` | 规范发现 ID；引用链必须无环并最终落到以上非重复终态 |

`DiscoveryDispositionLedger` 必须绑定 FW-E `DiscoveryInventory` 摘要，机器验证总数一致、ID 集合完全
相等、每项恰好一个已解决记录并至少有一个终态绑定、所有引用存在且双向闭合。`unclassified`、`mapped_validation`、
`catalogued_context`、未收敛 `CAND-*`、无主支撑事实、缺失项、重复已解决记录和循环引用的计数必须全部为
0。32 个 `SemanticRuleCandidate` 必须分别收敛到既有 SPEC、新建／拆分 SPEC、支撑事实、受管出站、
非出站或目标版本缺失，不得原位改名为规则。聚类只能减少审查工作量，不能减少逐项覆盖；
SupportEnvelope 缩小不能替代发现项处置。

| 维度 | 值 | 准入 |
|---|---|---|
| 迁移决策 | `inherit` | 工具证明目标语义、依赖和 sink 未变，且最小运行哨兵仍成立 |
| 迁移决策 | `change`／`condition_change` | 目标版本重新取得对应静态与运行证据 |
| 迁移决策 | `add` | 新建原子 SPEC、场景、画像落点和 PAIR 断言 |
| 迁移决策 | `delete` | 证明旧路径不可达且运行负例成立，保留历史编号 |
| 证据等级 | `verified` | 规则、条件、官方静态／运行证据和复算链闭环 |
| 证据等级 | `observed` | 仅证明有限样本；补证前不能承担 strict 对齐责任 |
| 证据等级 | `blocked` | 目标证据不足，不能进入 strict production replacement |
| 证据等级 | `regressed_evidence` | 旧 verified 在目标版本仅取得较低证据，只能 validation-only |

字符串相同、minify 符号相同或相邻版本都不能独立支持 `inherit`。迁移决策、证据等级、规则生命周期
和兼容类别必须分别记录。批准清单必须冻结 SupportEnvelope 内的目标规则全集、这些正交维度、
EvidencePackage、Profile digest、场景、端点、断言、隐私模式和用途；批准后官方产物或环境身份变化
必须新建 Campaign；SupportEnvelope、目标规则、迁移决策、画像、场景、断言或批准用途变化必须签发
新 ApprovalFact 并建立 candidate；源码、测试、构建或镜像变化只需建立新 candidate。任何旧事实均
不得原位改写。

Claude 的 ApprovalFact 还必须绑定同一摘要的 `DiscoveryDispositionLedger`、
`ProductionIngressInventory`、`EgressDispositionInventory`、Persona 派生清单、compatibility 模式边界、预期
DeploymentTrafficEnvelope 和 rollback 目标的 RollbackOperationalEnvelope。缩小 SupportEnvelope
不能替代现有生产入口的处置：每个入口仍须明确选择 `migrated_strict／retained_legacy／
explicitly_retired／rerouted`；每个已知 OAuth 出站仍须选择 §3.3 三态。

## 4.3 画像、Candidate 与制品

在不改变 production active／rollback 的前提下，把批准画像生成到全新绝对路径，并形成摘要、
inventory 和 selector 未变证明。入库只允许：

1. 先由 FW-E 冻结的最新 stable 证据生成目标 Schema、Snapshot 和 ReleaseArtifact；
2. 再使用同一 Schema／DialectCompiler 表达 2.1.220 baseline／rollback fixture，不得反向用旧版限制目标设计；
3. 版本、Header、Body、重试及 transport 事实来自画像，不新增业务代码版本分支；
4. ValidationCandidate 保存独立 immutable Release 引用，不借用 production rollback 槽位；
5. candidate 必须显式选择目标画像，连接池与 fallback 不跨画像混用；
6. candidate 必须冻结 SupportEnvelope，范围外条件由 Planner／Compiler fail-close；
7. 构建记录 source tree、测试树、Go／Node 依赖、目标架构、image ID 与 OCI digest。

当前 FW-F 已生成 target-first Snapshot／样例、2.1.220 fixture 和最终合同，但批准用途仅为
`validation_only`。110 条活动规则全部绑定 Vircs 上 2.1.226 官方客户端的真实 P／R／M、独立正负例
逐项断言通过，7,368 个发现与 593 个候选未决数为 0；证据等级
仍是 `observed`，取得后继 `production_replacement` ApprovalFact 前不得形成生产替换 candidate。

## 4.4 候选验证与正式验收

Candidate 只能在 DMIT 或等价隔离环境运行，不能在 Vircs 换镜像试验。真实请求前原子冻结源码、
测试、镜像、画像、配置、代理／CA／DNS、工具和 attempt nonce；恢复或秘密扫描失败不得继续。

最低场景矩阵以已批准 SupportEnvelope 的 strict 场景为核心，并必须包含关联的边界、遗留与 managed
处置验证：

- `s1/s2/s4/a1` 的推理与 `HEAD /api/hello` 生命周期 wire；
- 主请求、续轮和一级子代理的 Header、Body、session、agent、attribution 与连接关系；
- 空工具、Agent 与 Bash 的实测工具形态；
- `persona_strict` 端点、隐私模式和目标 entrypoint，以及 `non_persona_managed` 出站的独立策略断言；
- 每个目标处置为 `migrated_strict` 的官方／第三方标准 API 入口，以及所有范围外入口／功能的 fail-close；
- `retained_legacy／explicitly_retired／rerouted` 的隔离、退休或目标路由断言；
- Persona system／identity 派生记录、跨 Persona／跨 Release 拒绝，以及 compatibility 模式不会混入 strict
  的负例。

原生 TLS／ALPN、故障重试、真实 TUI、remote、custom Header／beta／metadata、Agent 层级、background、
hook、server／deferred tools、附件、count_tokens、OAuth refresh 与 MCP servers 已有本次 P／R／M 并进入
SupportEnvelope。telemetry、nonessential、usage、models、dispatch-id 与 usage-limit 的合法零流量只作
支撑事实；FW-G 不得为这些零流量生成规则，也不得把未获新证据的能力塞入 candidate。

每个场景按 `prepare → capture → seal → approve` 封存；结果必须绑定原始证据 inventory，失败 attempt
不得覆盖。只有 TranslationReport 为 lossless 的官方入口与第三方入口，才对同一规范化语义执行同一
`PAIR-<SPEC-ID>`；动态值比较来源、格式、关系和生命周期。Persona 固有 system／identity 事实按已
批准派生记录比较；有损 compatibility 请求不得混入 strict PAIR 分母。

FW-G 验收前至少重放本次 FW-F Store：

```bash
python3 -m tools.official_client_control replay \
  --store "$PWD/local-analysis/fw-f/claude-code-2.1.226/profile-approval-v4/control-store" \
  --external-root "$PWD" \
  --require-external
```

`claude_21220/check_coverage.py` 是 2.1.220 历史工具树完整性检查，只能在其冻结工具身份对应的 worktree
运行，或先追加工具变更审阅链；不得拿当前已经改为全 host 捕获的 MITM／提取器源码与旧摘要直接比较，
再把预期的工具演进误报成目标画像规则失败。2.1.220 fixture 的当前准入以本次内容寻址 Store 重放为准。

逐规则结果必须唯一覆盖 SupportEnvelope 内的 110 条目标全集。`ready` 只证明固定 release candidate 通过
已批准规则，不表示画像已晋升、正式镜像已构建或生产已切换。当前 FW-E 三层历史输入为：

- `DiscoveryInventory`：7,368 个发现项，其中目标 AST 331、2.1.88 命题 29、HitCC clue 71、Markdown
  原子 6,937；2,830 项归入语义候选，15 项映射既有规则，4,523 项登记为可重分类的
  `catalogued_context`；
- `SemanticRuleCandidate`：32 个语义候选，其中 10 个 `observed`、22 个 `blocked`，全部禁止加入
  RuleLedger 和生产；
- `RuleLedger`：57 条 SPEC，其中 45 条 `observed`、12 条 `regressed_evidence`、0 条 `verified`，没有
  因 7,368 个发现项自动新增规则。

FW-F 清零制品在不改写上述 FW-E 事实的前提下追加：

- `DiscoveryDispositionLedger`：7,368／7,368 个发现均有唯一已解决记录；缺失、额外、重复、未决、
  空绑定、孤儿引用和循环引用均为 0；
- 4,523／4,523 个 `catalogued_context` 均有终态，其中 128 个 Markdown 导航链接由结构证据证明为
  非出站，4,395 个绑定到精确文档、标题和语义支撑事实；原始记录仍永久保留；
- 593／593 个正交候选和 32／32 个语义候选族已收敛；FW-F v1 机械生成的 97 条规则提案全部撤回并保留逐项审计；
- `MeasuredRuleLedger` 只保留 110 条经 Vircs 上 2.1.226 官方客户端真实 P／R／M 正负断言通过的
  request-egress 规则；0 条响应兼容、
  遥测／traceparent 或无当前运行证据的规则进入 SupportEnvelope。

语义清零只表示每个发现已经确定“是什么、归到哪里”，不表示可以进生产。110 条活动规则虽已有目标
P／R／M 和通过断言，仍须在 FW-G 通过独立复测、实现后对拍与隔离验收达到后继批准要求。不得把
`observed` 改名、用遗留 wire 佐证，或为了缩小数字从发现清单删除。

FW-F 已封存 Claude `validation_only` ProfileApprovalFact、ReleaseArtifact、SupportEnvelope、两个
Inventory 的目标处置和 110 条 request-egress 断言计划；八类 strict egress 分别绑定自身规则集合，
响应兼容只作为支撑事实留在兼容边界。当前
缺少的是 FW-G 的完整实现、独立补证、后继 `production_replacement` ApprovalFact 和 DMIT 验收，因此
尚不能形成生产替换 candidate 或受管 `ready`。

## 4.5 生产、回滚与证据归档

只有 production-replacement candidate 达到受管 `ready`、FW-G 退出条件满足并进入 FW-H，才能依次执行：

首次生产激活前必须冻结并验证真实 rollback 目标：可以是已独立验收的旧 Release，也可以是当前生产
遗留实现及其固定镜像／配置。遗留实现作为操作回退点时，其 wire 仍是 diagnostic-only，不会因此成为
官方证据或 approved Profile；不得复制 active 摘要伪造 rollback Release。

2.1.220 当前只是 baseline fixture；只有未来取得独立窄 SupportEnvelope ApprovalFact 后才能作为 strict
rollback，不能把目标 stable 的完整范围自动交给它回退。激活前必须冻结并验证
ActiveSupportEnvelope、RollbackOperationalEnvelope 和 DeploymentTrafficEnvelope，且满足 §3.2
不变量；不满足时收窄本次流量，不能把“可能 fail-close”当成有效回滚。

1. 只读记录生产镜像、compose、production active／rollback、画像摘要、两个 Inventory、三个 Envelope、activation fact、依赖和回滚点；
2. 离线晋升已验收画像，生成 promotion receipt 和 candidate→production 差异 inventory；
3. 在最终 production tree 重跑门禁，构建并固定正式镜像 manifest digest；
4. 用隔离账号、数据库、网络和配置执行 production active canary，禁止强制 candidate override；
5. 只替换应用容器，保持数据库、缓存、挂载和网络；禁止 `compose down` 或无范围 prune；
6. 正式切换后，对完整 DeploymentTrafficEnvelope 切回冻结 rollback 镜像及其匹配的 selector／配置验证，再恢复目标镜像和 selector，并复核业务、Guard 和 activation fact；
7. 原子追加实际入口处置和 DeploymentFact，生成绑定验收、晋升、镜像、canary、切换、回滚、恢复及范围摘要的不可覆盖激活收据。

运行容器 digest、production active／rollback、画像摘要、三个 Envelope 与 activation fact 不一致时
状态为 `production_unverified`。回滚不得删除 Campaign、覆盖画像或重建生产数据服务。只要 Inventory
仍有 `retained_legacy`、未知物理别名或未处置出站，就不得删除旧 finalizer／旁路或生成表示迁移完成的
RemovalReceipt。

| 私有材料 | 处理 |
|---|---|
| 原始 J、未脱敏 R、原始 P | 只进权限受限的私有归档；未脱敏 R 不离开采集机 |
| 官方发行物与提取 bundle | 只保存来源和摘要，不进公开仓库 |
| token、账号和 OAuth 凭据 | 不得归档或提交 |

私有归档必须有逐文件路径、大小和 SHA-256 清单，并在另一存储位置完成复算和收据重放。只有本地
权威源码、正式发布镜像、production tree 与私有证据归档全部闭合，才能按 dry-run 清单清理远端；
生产数据库、Redis、配置、当前／回滚镜像和唯一证据副本永远不在清理范围。

当前边界：Claude 已有 `validation_only` ProfileApprovalFact，但尚无 production-replacement candidate、
晋升或激活收据。2.1.220 Campaign／run／提取物已被 FW-F fixture 内容寻址引用，引用有效期间不得删除。

---

# 第五部分 非版本维护

上游合并、实现调整和兼容代码退休不得夹带进 Claude 换版 Campaign。

| 变化 | 路径 | 最低证明 |
|---|---|---|
| 规则、画像、wire、场景和工具身份均不变 | 独立维护变更集 | overlay 可复算，production active／rollback 空 wire 差异，完整门禁 |
| 实现变化但批准规则不变 | 第四部分的新 candidate | 独立封存、验收；替换生产还需新激活收据 |
| 规则、画像、断言、场景或产出工具变化 | 同版本后继 Campaign | 重新批准和重放受影响证据 |
| 兼容代码退休 | 独立退休变更集 | 消费者闭集、fail-close、空 wire 差异和 RemovalReceipt |

## 5.1 Sub2API 上游更新

1. 冻结 upstream commit、发送面、production active／rollback、release 与画像摘要；
2. 合并后复算 source-to-sink／overlay，审计 route、persona、Sink 和新增裸 client；
3. 按上表选择普通维护、新 candidate 或后继 Campaign；
4. 运行完整测试、静态／版本泄漏门禁和 production active／rollback 空 wire 比较；
5. production replacement 必须重新执行第四部分生产闭环，不复用旧激活收据。

当前 overlay 台账的 86 个文件中 Claude／Anthropic 条目为 0，无法阻止上游静默改动
`official_egress_anthropic.go`、`official_client_profile_registry.go` 或 `pkg/claude/constants.go`。
在 §3.4 的 FW-E 发送面闭集和 FW-G 入口迁移完成前，每次上游合并必须人工列出这些发送面变化；
Claude overlay 覆盖应纳入 FW-E。

## 5.2 兼容代码退休

退休顺序为：证明全部消费者和必须保留的产品语义 → 迁移真实消费者 → 让旧入口 fail-close →
验证 production active／rollback、fallback、辅助端点和负例 → 删除旧能力并生成 RemovalReceipt。不得删除仍
服务回滚、非 `claude-code` persona 或 API Key 产品语义的代码。

§3.4 的 FW-G 只建立替代实现和迁移候选，FW-H 才是首个允许 Claude 遗留退休的阶段。
`internal/pkg/claude` 当前有 17 个非测试文件消费者，必须先建立闭集，只退休 Claude Code 的版本、
UA、Header 和 beta 面。现有兼容退休台账没有 Claude 条目；在每个消费者被标记为“已退休”或“因
产品语义保留”，且 `ProductionIngressInventory` 不再含 `retained_legacy` 前，不得宣称清理完成或
签发迁移完成的 RemovalReceipt。

---

# 第六部分 取证、测试与生产环境

本部分记录当前稳定机器角色和安全边界，不构成 Claude Code 客户端规则证据。每次正式 Campaign
仍须在 manifest 中冻结实际主机、网络、镜像、工具和环境摘要；环境发生变化时按 Framework §3.2
判断新建 attempt、candidate 或 Campaign。

## 6.1 当前拓扑与代理边界

```text
ImmortalWrt 192.168.9.1
└── Mac 192.168.9.99
    ├── HomeProxy：明确绕过该 Mac
    └── Surge：决定该 Mac 的代理与实际出口

Mac／控制端
├── SSH／运维 → Vircs x86_64（生产与唯一官方 Claude Code 采集机）
├── SSH／测试 → DMIT x86_64（候选验证机）
└── SSH／构建 → ARM64（具体构建职责待核实）
```

HomeProxy 的绕过只说明路由器不代理该 Mac；Mac 是否直连仍由 Surge、系统代理、增强模式、CA、DNS
和具体规则决定。Mac 参与测试时必须记录这些状态及配置摘要。路径不明时，Mac 的 ClientHello、
ALPN、连接复用和 HTTP 协议不得作为官方客户端直连证据，只能作为带代理条件的功能样本。

本地 Claude Desktop 与 Claude Code CLI 是不同 persona；Codex Desktop 也不属于 Claude Code。
桌面客户端不能为 Claude Code 提供默认 UA、Body、Header 或 TLS 结论。Mac 的代理状态也不会自动
影响 Vircs、DMIT 或 ARM64，服务器侧代理、CA、DNS 和转发条件必须分别冻结。

## 6.2 机器职责

| 节点 | 固定角色 | 可以执行 | 禁止或不能证明 |
|---|---|---|---|
| Mac `192.168.9.99` | 控制端、第三方入口和桌面测试端 | SSH 编排、协议入口验证、必要的 UI／TTY 驱动 | 默认直连 TLS 权威证据；混用 Desktop 与 Code persona |
| Vircs x86_64 | Sub2API 生产机、唯一官方 Claude Code 机、官方 P／R／J／M 主证据源 | 隔离取证、只读生产核对、经批准的正式切换与回滚 | 候选换镜像、数据库试验、破坏性故障注入、为抓包中断生产 |
| DMIT x86_64 | 独立 Sub2API 候选验证机，1 核／1.9G／20G | 重启、换候选镜像、修改测试库、故障注入、成对入口验收 | 真实用户流量；官方 Claude Code 权威证据源 |
| ARM64 | 候选构建或跨架构验证的待核实节点 | 核实构建链后执行批准任务 | 未核实前承担 x86_64 权威构建、官方取证或 DMIT 验收 |

“好像曾用于编译并把镜像传到 DMIT”不是正式角色。首次受管 Campaign 前须以 P0 只读检查确认
构建工具、buildx／交叉编译方式、目标 manifest 架构、产物传输路径和摘要链。

## 6.3 Vircs 官方采集纪律

Vircs 的硬约束是任何取证不得改变或中断 Sub2API 生产服务：

1. 官方二进制按版本和 SHA 独立保存，不原位覆盖唯一封存版本；
2. 使用独立用户目录、证据目录、端口和可证明的网络隔离；
3. 不修改宿主级 `hosts`、系统 CA、默认路由、iptables、全局代理或生产 compose；
4. 采集进程设置资源限制，避免争抢生产 CPU、内存、磁盘和连接；
5. 开始前后记录生产容器 digest、健康、端口、网络、挂载、依赖和错误日志摘要；
6. after 与 before 不一致时，本 attempt 失败并先恢复环境。

必须修改宿主全局状态或重启生产服务的场景不得在 Vircs 执行，应改造为隔离方案或登记为阻断。
Vircs 上官方 Claude Code 生成的材料才可作为当前 Linux x86_64 官方 wire 主证据；DMIT 上 Sub2API
生成的是 candidate 证据；Darwin arm64 发行物只作静态交叉复核；故障注入样本必须与自然失败分开。

## 6.4 DMIT 候选验证纪律

DMIT 是候选采集、故障注入和破坏性验证的默认机器。每个 candidate 必须绑定固定的 linux/amd64
OCI digest、image ID、源码树摘要、画像摘要、构建 ID 和 attempt nonce。

受 1 核／1.9G／20G 限制，不在 DMIT 执行 Go／Node 全量编译；开跑前检查磁盘下限，避免 pcap、
R 字节和镜像层填满系统盘。证据完成 inventory、秘密扫描和摘要后同步到受控私有归档；同步和独立
复算完成前不得删除唯一副本。数据库修改只作用于测试实例，不导入生产真实用户数据。

环境允许破坏不等于可以省略 before／after、秘密扫描、恢复记录或固定 digest。

## 6.5 构建与架构边界

正式构建机尚未固定。无论使用 Mac、ARM64 或其他节点，都必须满足：

1. 构建源、依赖锁、工具版本和参数可复算；
2. 目标产物明确为 DMIT／Vircs 所需的 `linux/amd64`；
3. 多架构 manifest 的平台 digest 可分别核对；
4. candidate 与 production 镜像身份分开；
5. 构建机只证明制品同源性，不产生官方客户端规则事实。

ARM64 原生构建成功不能证明产生了可供 x86_64 服务器运行的镜像，必须核对 registry manifest、
运行容器架构和实际 image ID。

## 6.6 Campaign 环境冻结

每个正式 Campaign／candidate／attempt 至少冻结：

| 类别 | 必须记录 |
|---|---|
| 主机 | 角色、标识、OS、内核、架构、时区和资源限制 |
| 客户端 | 包来源、版本、二进制 SHA、entrypoint、隐私模式和用户目录 |
| Sub2API | Git／tree、构建 ID、部署版本、镜像 digest、画像 ID／digest |
| 网络 | 代理、CA、DNS、hosts、端口、namespace 及 Surge／HomeProxy 影响边界 |
| 工具 | 采集、relay、MITM addon、脱敏、分析、finalizer 和环境探针摘要 |
| 服务 | 生产／测试标识、数据库、缓存、依赖、挂载和启动配置摘要 |
| 安全 | 目录权限、秘密扫描、等长脱敏、inventory 和清理结果 |

代理／CA／DNS、官方二进制、entrypoint、隐私模式、账号条件、candidate 源码／镜像／画像、产出侧
工具或 before／after 恢复能力变化，至少需要新 attempt；改变冻结身份或证据含义时必须新建
candidate 或 Campaign。

## 6.7 生产发布与证据清理

生产发布只替换 Vircs 的应用容器，不执行 `compose down`、无范围 `prune`，不重建数据库、Redis、
keeper、挂载或网络。切换前冻结旧镜像和配置，切换后执行旧版回滚及目标恢复，形成独立激活收据。

原始 MITM、未脱敏 R 字节、pcap、官方 bundle 和凭据不得进入公开仓库。清理前必须证明私有归档可
解包复算、收据可重放、生产与回滚镜像仍可用、删除清单不含唯一证据或生产依赖。任一条件不满足时，
只停止空闲采集进程，不删除证据。
