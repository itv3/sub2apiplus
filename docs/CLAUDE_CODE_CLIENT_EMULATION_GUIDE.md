# Claude Code 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 Anthropic OAuth（`authMethod=claude.ai`、`apiProvider=firstParty`）
> 出站时的 Claude Code 客户端仿真
> **共享框架**：[`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)
> **当前取证目标**：`claude-code 2.1.226`——第二部分 40 条跨模型 RequiredRules、110 条基础
> P／R／M 原子断言，以及独立的 Sonnet／Opus／Fable 模型能力目录；`2.1.220` 只保留为同一
> Schema／Compiler 下的历史 baseline fixture
> **机器台账**：FW-F 的 `claude_fw_f_v21_finalize.py`、两份 v4／v3 策略和清零目录保留历史
> `observed／validation_only` 事实；`claude_fw_g_acceptance.py` 及 Control Store 冻结历史 Sonnet 验收，
> 当前三模型 `verified／production_replacement` 状态由追加式 AcceptanceFact 收据和摘要门禁承接，正文
> 规则 ID 集合由门禁强制对账
> **文档定位**：本文是 Claude Code 官方事实、版本画像、Sub2API 实现、环境职责和版本演进的唯一
> 人类可读权威入口；机器证据和 JSON 台账不得形成第二套规范
> **证据边界**：本文没有未压缩 TS 源码可用，静态规则均从官方生产 bundle 逆向建立；材料、证据、
> 规则与结论全部独立取自 Claude Code 自身，不继承 Codex 的任何事实结论
> **运行时状态**：历史 Candidate、Vircs／DMIT 验收和生产收据均作为不可变事实保留，但不再代表当前
> 目标。当前 Claude Persona 为 `official-client-only` 候选，只接受 Catalog 登记的 Claude Code
> 2.1.226、Claude Desktop 2.1.237 和 Claude Code for VS Code 2.1.239；正向逻辑入口仅为 Messages 与
> count_tokens。KiloCode、zlfcode、curl、Chat Completions、Responses、未登记版本或工具目录均在读取
> OAuth 凭据前拒绝。DMIT 只做候选验收；Vircs 是用户管理的生产机，本任务不连接、不部署、不验证
> **当前执行点**：official-client-only 候选已部署 DMIT，但账号 `#100` 的 Anthropic OAuth 凭据被上游
> 撤销，完整在线矩阵尚未完成，状态仍为 `candidate_deployed／not_ready_for_operator_release`；续作检查点
> 见 §3.6.1
> **末次更新**：2026-08-22

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
| 已登记 Claude 官方入口 | Claude Code 2.1.226、Claude Desktop 2.1.237、Claude Code for VS Code 2.1.239；必须同时命中 OfficialIngressCatalog 的版本、平台和 wire 摘要条件 |
| 第三方 API／IDE／Agent | 不属于 Claude Persona 正向 SupportEnvelope；在读取 OAuth 凭据前拒绝 |
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
已登记 Claude 官方客户端
→ official-client-only／OfficialIngressCatalog 准入
→ IngressProtocolAdapter
→ CanonicalRequest + TranslationReport
→ Claude PersonaPlanner + ClaudeIdentityFacts
→ Claude Code active ReleaseArtifact／ReleaseBundle
→ Claude DialectCompiler → CompiledEnvelope
→ Claude Executor authority 实例 + 共享 Executor 实现／Guard
→ Anthropic OAuth 上游
```

Claude 当前只复用 Anthropic Messages／count_tokens 的窄入站适配器。准入层先用内容寻址 Catalog
验证官方产品、版本、平台、UA、固定 Header、System 和工具目录，再输出规范化语义；Chat Completions、
Responses 和第三方 Anthropic 客户端不进入转换链。出站画像函数为
`ClaudeProfile(规范化语义请求, 会话状态, 平台与入口条件, 服务端特性状态)`；入站自报身份本身没有
Persona 选择权，只有完整 Catalog 命中才形成准入事实。第二部分记录官方规则，第三部分记录 Claude
实现与迁移，第四部分只补充 Claude 特有的换版流程，第五部分处理维护，第六部分固定环境职责。

---

# 第二部分 Claude Code 2.1.226 实测规则画像

本部分是当前 Claude Persona 的唯一活动规则画像。按照 Codex CLI 第二部分的规则粒度，活动集合为
**40 条 RequiredRules**；证据层保留 **110 条原子断言**，其中 106 条完整且唯一地映射到这 40 条
画像规则，4 条只描述官方客户端本地上下文装配／本地拒绝场景，不属于 Sub2API 出站实现责任。
普通原子断言绑定 R（等长脱敏的原始请求／必要响应字节）与 M（版本、二进制、运行、连接、干预和
隐私条件），TLS 原子断言绑定原生 P 与 M。2.1.220、2.1.88 与 HitCC 只用于
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
| 主模型能力目录 | `claude-sonnet-5`、`claude-opus-5`、`claude-fable-5`；只接受目录中显式登记的精确别名 |
| 辅助模型形态 | TUI 标题 `claude-haiku-4-5-20251001`；Sonnet retry fallback `claude-haiku-4-5`；Fable server fallback `claude-opus-4-8` |
| 隐私模式 | `DISABLE_TELEMETRY=1`；`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |
| 上游 | `api.anthropic.com:443` |
| 完整场景矩阵 | Sonnet 基础 Campaign 77／77 场景、394 条官方请求、49／49 维度；Opus／Fable 模型能力补充 Campaign 42 个成功 attempt，3 个历史失败 attempt 只读保留 |
| strict egress | messages、hello、policy limits、settings、OAuth profile、count_tokens、OAuth refresh、MCP servers，共 8 类 |
| RequiredRules | 40 条；按 Codex 的“范围、规则／机制、源码、实测、实现、状态”六字段维护 |
| 原子断言 | 110 条全部通过；107 条 R/M、3 条 TLS P/M；106 条画像证据、4 条客户端本地场景 |
| 当前等级 | 40 条公共 RequiredRules 在三模型出站 SupportEnvelope 内为 `verified`；official-client-only 后继尚须取得 DMIT 候选验收事实 |
| 批准用途 | 2.1.226 Release 已取得 `production_replacement_official_client_only` Approval；本任务只交付 DMIT `ready_for_operator_release`，不主张 Vircs 已激活 |

历史 Sonnet `verified` 结论由 [FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)
冻结；当前三模型结论由独立 ValidationCandidate、三模型 API／Desktop 正例、边界门禁及 DMIT
回滚／恢复共同支撑，公开摘要见
[三模型 FW-G 验收收据](egress/maintenance/claude-fw-g-three-model-acceptance.json)。Profile、Wire、
Release、源码或镜像任一变化，仍必须冻结新的 ValidationCandidate 并重新验收，不能复用本次 `ready`。

当前 SupportEnvelope 覆盖策略文件列出的五组能力：`sdk-cli`／`cli` 的条件 system、cache、metadata、
session、Agent／background／hook／remote、工具往返与附件；真实 TUI 的 OAuth profile、标题、
count_tokens 与 MCP 目录；隔离故障矩阵的 retry、timeout、stream／model fallback；过期凭据的隔离 OAuth
refresh；以及原生 TLS／ALPN。八类 strict egress 的 RequiredRules 分布分别为 messages 33 条、hello
5 条、policy limits 4 条、settings 4 条、OAuth profile 2 条、count_tokens 1 条、OAuth refresh 1 条、
MCP servers 1 条。跨端点规则会同时计入多个 egress，全局唯一规则仍为 40 条。范围外能力必须
fail-close 或留在明确的 `non_persona_managed／retained_legacy` 边界，不能从这 40 条规则外推。

40 条 RequiredRules 描述三个主模型共同遵守的出站责任，不按模型复制。模型名、显式别名、可用 effort、
场景 Body／Header 顺序、`fallbacks`、辅助模型和锁存状态保存在独立的内容寻址
`ModelCapabilityCatalog`。新增模型只有在官方 P／R／M 证明其可复用公共规则、并补齐全部模型差异后，
才能向目录追加；未知模型、未登记别名或缺少差异证据一律 fail-close。当前目录摘要为
`d34ca049ec851b220f06a4701c951a8be270d4a498b8012229c7f62cb8183df1`。

## 2.2 证据通道与内容寻址

### 2.2.1 权威证据

| 别名 | 内容 |
|---|---|
| `M-ID` | FW-E `campaign/identity.json`：版本、二进制 SHA、隐私环境、目标 host 和生产零变更 |
| `M-INDEX` | FW-E `campaign/indexes/relay-index.json`：基础目标／基线 run 与二进制身份 |
| `R-a1/s1/s2/s4` | FW-E 四个目标 run 的等长脱敏 `client_to_upstream.bin` |
| `M-a1/s1/s2/s4` | 四个基础 run 的 `relay-manifest.json` 与 `relay/relay.json` |
| `R-v4-*` | v21 每个 attempt 的 `connNNN.client_to_upstream.bin`；故障规则还绑定必要的 `upstream_to_client.bin` |
| `P-v4-*` | 原生 TLS attempt 的 `tls-clienthello.pcap`，用于 CipherSuite 与 ALPN 条件对照 |
| `M-v4-*` | v21 的 manifest、relay、intervention、invocation、summary、场景目录、秘密扫描与 cleanup |
| `PAIR-*` | v21 最终化器生成的 110 条原子正例及条件对照／零违规分母断言 |
| `G-P/R/M` | FW-G 独立官方复测的原生 TLS、请求字节与身份／运行元数据，用于把 40 条规则升级为 `verified` |
| `G-PAIR-*` | 固定 Candidate 对 40 条 RequiredRules 唯一生成的 `PAIR-<SPEC-ID>` 结果及九个场景批准链 |
| `G-ACCEPT` | `production_replacement` ApprovalFact、ValidationCandidate 与 AcceptanceFact；公开摘要见 FW-G 收据 |
| `R/M-MODEL` | Opus／Fable 的 42 个成功 attempt、逐场景原始请求、运行回执、生产零差异证明和 3 个只读历史失败，用于生成独立模型能力目录 |

基础四个 run 位于：

`local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/runtime-relay-205d7f58f/campaign/`

覆盖 77 个真实场景的 v21 正式 Campaign 位于：

`local-analysis/fw-f/claude-code-2.1.226/complete-v21-78fae770cbb5/`

Opus／Fable 模型能力补充 Campaign 位于：

`local-analysis/fw-f/claude-code-2.1.226/model-capability-v1-20260821/`

其中 77／77 场景、394 条请求、49／49 维度和原生 TLS P 通道均由最终化器逐项复算；目录集合、执行源
摘要、目标二进制、采集模式、等长脱敏、秘密扫描和 cleanup 也必须通过。原子证据账本与
RequiredRules 映射清单分别为：

`local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v5-final/measured-rule-ledger.json`

`tools/official_client_capture/claude_required_rules_2_1_226.json`

前者沿用历史文件名，但语义是 `AtomicAssertionLedger`：为 110 条原子断言保存 `egress_ids`、
适用条件、完整分母、证据文件 `path/sha256/bytes/channel`、命中 run、连接号、流偏移和原始请求摘要。
后者保存 40→106 与 2 个本地场景组→4 的唯一映射，并作为指南、Snapshot、EvidencePackage 和
SupportEnvelope 对账权威。Authorization 已等长脱敏，不保存 OAuth secret。

### 2.2.2 证据边界与零流量事实

当前 Campaign 已在原生 TLS attempt 中取得 ClientHello pcap，3 条 TLS 原子断言使用 P/M；普通
HTTP、Header、Body、状态与工具原子断言仍使用 R/M，不允许以 relay 元数据冒充原生 TLS 证据。

行为层不作对比维度。官方配置已合法关闭遥测与非必要流量，候选“零遥测”不是“不像官方客户端”的
判据，也不生成 `traceparent`／span 规则。响应解析属于 downstream compatibility，不进入客户端请求
出站画像。

## 2.3 规则准入合同

RequiredRule 只有同时满足以下条件才能进入本部分：

1. 目标版本、二进制、平台、入口、认证、模型／模型转换和隐私条件必须与 M 一致；
2. 普通规则必须有可复算的目标 R 原始字节；TLS 规则必须有原生 P；不接受仅有旧源码、bundle 字符串
   或历史 wire；
3. 必须由一条或多条已通过的独立原子 `PAIR-*` 断言完整支撑，并在机器映射中恰好归属一次；
4. 必须按 Codex 标准声明范围、规则／机制、源码、实测、实现和状态，并绑定物理 `egress_ids`；
5. 条件规则必须有官方条件成立／不成立样本；无条件规则不伪造“官方负例”，而以多个适用官方样本、
   零违规分母断言和 FW-G candidate mutation／不匹配负断言闭环；
6. 必须是 Sub2API 负责复现的 request-egress 命题；响应兼容、客户端本地文件／Hook／本地拒绝、
   遥测关闭事实和未触发功能只能进入场景或支撑／边界事实；
7. 合法零流量的 telemetry、nonessential、usage、models、dispatch-id、usage-limit 只记录支撑事实；
   只有场景被真实触发且具备完整实测分母的命题才能成为规则。

FW-F v1 把 32 个候选机械拆成 97 条规则提案的做法无效，现已逐条撤回。v21 对旧 88 条原子断言作
实质重测，并由新增场景产生 22 条新原子断言，得到 110 条证据断言；随后按 Codex 复合规则粒度归并为
40 条 RequiredRules。原子断言数由实测决定，RequiredRules 数由实现责任与复合机制边界决定，两者
不得混为一个数字。

模型能力变化也不得机械生成新 RequiredRule。只有它引入新的 Sub2API 出站责任或改变既有公共机制，
才按上述准入合同新增或修改规则；仅模型值、显式别名、模型特有 `fallbacks`、辅助模型、字段顺序或
状态分支变化时，更新 `ModelCapabilityCatalog` 并绑定逐场景官方证据，公共规则数保持不变。

## 2.4 实测 wire 与端点向量

### 2.4.1 messages 推理核心

基础 `sdk-cli` 请求行为固定为：

- 请求行：`POST /v1/messages?beta=true HTTP/1.1`，Host 为 `api.anthropic.com`；
- 基础 Header 按实测大小写和顺序发送；条件 Header、自定义 Header、gzip、TUI、retry 和 fallback
  只按 `2.5` 对应规则插入或变化；
- 基础 UA 为 `claude-cli/2.1.226 (external, sdk-cli)`；真实 TUI 使用 `external, cli`，获准条件段按
  agent-sdk、client-app、workload 的实测顺序追加；
- Stainless 基础向量为 `x64/js/Linux/0.94.0/retry 0/node/v26.3.0/timeout 600`；
- Sonnet 基础 Body 顶层顺序为 `model/messages/system/tools/metadata/max_tokens/thinking/`
  `context_management/output_config/stream`；Opus／Fable 仅在实测场景插入 `fallbacks`。Fable 请求返回
  画像声明的 `claude-opus-5` 时不锁存；只有实际 `claude-opus-4-8` server fallback 才按实测条件在后续
  请求插入成对锁存 Header；所有模型的具体顺序均从能力目录场景生成；
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
端点有独立 route、Sink、binding、画像视图和纵向 CompiledEnvelope，不得把全部 40 条伪挂到
messages egress。

## 2.5 40 条 RequiredRules

以下 40 条采用 Codex CLI 规则画像的同一六字段格式。每条“实测”均反向绑定机器清单中的原子断言；
精确 P／R／M 文件摘要、条件、分母和流偏移以 AtomicAssertionLedger 为准。

<!-- FW-F-ACTIVE-RULES-BEGIN -->

### SPEC-BODY-001 推理 Body 顶层序列化合同

- **范围**：messages-inference；Claude Code 2.1.226 的 sdk-cli／cli 推理请求。
- **规则／机制**：Body 顶层字段按已实测顺序序列化；字段存在性和类型由同一 Release 画像约束。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；基础四组官方推理样本逐字节确认顶层键顺序。
- **实现**：由 Claude BodyShape 和 DialectCompiler 定型，Ingress 不得直接生成最终 wire。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-002 metadata 身份与扩展合并

- **范围**：messages-inference；基础身份及获准 EXTRA_METADATA 条件。
- **规则／机制**：metadata.user_id 内嵌 device、account、session 身份；额外 metadata 只按实测浅合并规则加入，session 必须与 Header 同源。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：3 条 R/M 原子断言通过；基础与 extra-metadata 正交场景覆盖默认值、浅合并和嵌套对象。
- **实现**：由 ClaudeIdentityFacts 提供可信身份，Planner 和 Compiler 组合；禁止从第三方入站静默补造客户端状态。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-003 system 结构与自定义提示分支

- **范围**：messages-inference；主请求、子代理、自定义 system、追加／排除动态 system 和获准自定义 agent。
- **规则／机制**：按入口和受信条件选择已实测的 system block 数量、顺序、文本角色和 cache_control 形态。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：5 条 R/M 原子断言通过；基础、custom-system、append、exclude-dynamic 和 custom-agent 场景均有独立 wire。
- **实现**：Persona 画像保存结构和派生规则；用户文本只作为规范化语义输入，不写入静态画像。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-004 消息历史与会话续接状态

- **范围**：messages-inference；首轮、续轮、resume、fork、子代理和真实 TUI。
- **规则／机制**：messages 角色序列、Session-Id、metadata.session_id 与 cc_prev_req 按实测会话转换共同演进；fork 必须生成新会话身份。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：4 条 R/M 原子断言通过；基础多轮、resume、fork 与 TUI 场景覆盖正反状态转换。
- **实现**：由 invocation 隔离的 Persona 状态机生成；没有可信会话锚点时 fail-close，不复用入站伪身份。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-005 基础 tools 字段与工具 Schema

- **范围**：messages-inference；无工具、Agent 与 Bash 基础场景。
- **规则／机制**：tools 字段始终存在；无工具为数组空值，内置工具按实测名称、说明和 JSON Schema 输出。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；基础四组官方样本覆盖空工具、Agent 与 Bash。
- **实现**：由 Claude ToolPolicy 编译规范化工具；不接受入站伪造官方工具身份。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-007 billing attribution 身份块

- **范围**：messages-inference；默认 attribution 与官方关闭条件。
- **规则／机制**：首个 system block 承载版本和动态 cch attribution；关闭条件成立时整块移除，其余 system 保持相对顺序。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：4 条 R/M 原子断言通过；基础请求和 attribution-disabled 条件场景确认格式、动态值与省略行为。
- **实现**：版本来自 Release，动态值来自 Persona 身份派生；不得从入站原样透传 attribution。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-008 模型与生成参数组合

- **范围**：messages-inference；已登记 Sonnet 5／Opus 5／Fable 5 及 max_tokens、effort、thinking、thinking.display、adaptive-thinking、fallbacks 条件。
- **规则／机制**：model、max_tokens、thinking、context_management、effort、fallbacks 和 stream 必须作为一个条件化 Body 合同生成；adaptive thinking 覆盖缺省 display、summarized 和 omitted 三态，display 存在时对象字段顺序固定为 type、display；公共机制由本规则约束，模型值、别名和模型特有字段由同 Release 的 ModelCapabilityCatalog 决定。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：基础账本 9 条 R/M 原子断言通过；Sonnet 的基线、五档 effort、输出上限、thinking 开关及 SPEC-BODY-010 三态均已对拍；Opus／Fable 的五档 effort、thinking 关闭和逐场景 fallbacks／字段顺序由模型能力补充 Campaign 实测。
- **实现**：由 ModelIntent、BodyShape 与内容寻址 ModelCapabilityCatalog 联合编译；CanonicalRequest 显式保存 display 语义，Compiler 按 ReleaseBundle 重建 thinking／fallbacks，只接受已登记模型、精确别名、受信配置和批准闭集。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-043 请求 gzip wire

- **范围**：messages-inference；官方请求 gzip 条件成立时。
- **规则／机制**：插入 Content-Encoding:gzip，Content-Length 计算压缩后的 wire 字节；解压后必须是完整目标 JSON。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；gzip 正交场景保存压缩原始请求并验证可逆解析。
- **实现**：DialectCompiler 先定型 JSON 再压缩并计算长度，Executor 不得二次改写。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-BODY-048 真实 TUI 标题与主推理请求

- **范围**：messages-inference；真实 cli 交互入口及已登记主模型。
- **规则／机制**：TUI 先生成 Haiku 标题请求，再生成所选主模型请求；两者的模型、beta、system、tools、thinking、fallbacks 和 output_config 分别按能力目录中的已实测形态输出。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；真实 TUI 运行取得标题请求与主推理请求的完整 wire。
- **实现**：仅在可信 cli entrypoint 条件下由 Persona 状态机生成；第三方入站不得自报 TUI 身份。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-CACHE-005 system prompt cache_control

- **范围**：messages-inference；Sonnet 基线、一小时缓存和禁用缓存条件。
- **规则／机制**：默认使用实测的两个一小时 system 缓存点；禁用条件移除全部 cache_control 而不改变 block 内容。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；基线、1h 开关和两类禁用条件均有完整 Body 对比。
- **实现**：由 CachePolicy 在 system 结构定型后应用，不能由兼容层任意插入缓存标记。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-CONN-010 重试分类、退避、预算与超时

- **范围**：messages-inference；HTTP 状态、Retry-After、断连、最大重试数和 API_TIMEOUT_MS 条件。
- **规则／机制**：按已实测状态集合决定是否重试，执行分级退避、Retry-After、断连重试、每模型预算和超时 Header；预算为零时不重试。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：6 条 R/M 原子断言通过；十状态故障矩阵、两种 Retry-After、断连、retry-limit 和 timeout 场景闭合。
- **实现**：Claude RetryPolicy 生成 attempt；共享 Executor 只执行已编译 attempt，不解释厂商状态语义。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-CONN-018 streaming 失败与 non-stream fallback

- **范围**：messages-inference；建流 404、已建流中断及 disable fallback 条件。
- **规则／机制**：按实测条件切换 non-stream；fallback 请求省略 stream、调整超时并刷新 request-id／cch，同时保持会话和其余 Body 语义。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；四个隔离 streaming 故障场景覆盖建流和中断分支。
- **实现**：由 Claude 流状态机和 Body 编译器共同产生新的 attempt，禁止业务层旁路修改。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-CONN-019 HTTP/1.1 连接复用

- **范围**：messages-inference；同一官方多请求运行。
- **规则／机制**：同一运行的连续推理请求复用有效 HTTP/1.1 连接；跨画像、跨 Persona 和失效连接不得复用。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；a1、s2、s4 三个多请求运行确认连接身份关系。
- **实现**：连接池按 Persona、Release、route 和 transport capability 隔离。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-CONN-021 重试中的请求状态再生成

- **范围**：messages-inference；应用层状态重试、Retry-After、断连和 retry-limit。
- **规则／机制**：重试保持 Body、Session-Id 和主体 attribution，重新生成 x-client-request-id；Stainless retry count 保持实测值。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；15 个 retry transition 对动态与稳定字段逐项比较。
- **实现**：Attempt 状态由 Persona RetryPolicy 派生，Body 可重放性由 CompiledEnvelope 保护。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-CONN-023 模型 fallback 转换

- **范围**：messages-inference；Sonnet 客户端 retry fallback，或 Fable 上游拒绝后触发的 server fallback。
- **规则／机制**：Sonnet 仅在配置启用且失败预算耗尽后切换到 Haiku，并整体切换 max_tokens、thinking、beta、message 和 output_config；Fable 返回已批准的 Opus 4.8 fallback 模型后，以响应 request-id 锁存会话，后续请求省略 fallbacks 并生成 `x-cc-fallback-latched-by` 与 `x-is-refusal-fallback:true`。Opus／Fable 不得复用 Sonnet 的 Haiku retry fallback。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：基础账本 2 条 R/M 原子断言通过；隔离场景取得三次 Sonnet 与第四次 Haiku 完整转换；Fable 官方请求另取得 Opus 4.8 响应后的成对锁存 Header 和后续 Body 形态。
- **实现**：FallbackPolicy 与 ModelCapabilityCatalog 共同选择目标 BodyShape；Fable 锁存由会话状态机按实际响应模型提交，第三方入站无权直接声明 fallback Header 或 fallbacks；不得只替换 model 字符串。
- **响应闭集**：`claude-fable-5` 请求返回画像中声明的 `claude-opus-5` 属于同次请求的允许响应，不建立 server fallback 会话锁存；只有实际响应模型为 `claude-opus-4-8` 才进入上述锁存状态。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-001 messages 推理端点坐标

- **范围**：messages-inference；first-party OAuth。
- **规则／机制**：使用 api.anthropic.com 的 POST /v1/messages?beta=true，method、host、path 和 query 均为闭集。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：3 条 R/M 原子断言通过；基础八条推理请求逐项确认端点坐标。
- **实现**：EndpointProfile 与 route／Sink 共同定型；未知 host、path、query 或端口 fail-close。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-002 hello 生命周期探测

- **范围**：lifecycle-hello；每次 sdk-cli／cli 运行。
- **规则／机制**：在独立连接发送无 Body 的 HEAD /api/hello，并使用已实测五项 Header。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；四个基础运行均取得一条 hello 请求。
- **实现**：作为独立 strict egress、route、Sink 和画像视图执行，不在 messages 代码中旁路。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-005 policy limits 辅助端点

- **范围**：policy-limits；sdk-cli 启动阶段。
- **规则／机制**：发送 GET /api/claude_code/policy_limits，使用 first-party OAuth、oauth beta、Release UA 和七项有序 Header。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；55 个非 TUI 官方运行确认端点与 Header 合同。
- **实现**：作为独立 persona_strict egress 编译；其出现仍受官方隐私与入口条件控制。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-006 settings 辅助端点

- **范围**：settings；sdk-cli 启动阶段。
- **规则／机制**：发送 GET /api/claude_code/settings，在 policy-limits 身份向量上增加 no-cache Header。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；55 个非 TUI 官方运行确认端点与九项 Header。
- **实现**：作为独立 persona_strict egress 编译；不得复用裸 client 绕过 Persona。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-007 OAuth profile 辅助端点

- **范围**：oauth-profile；真实 TUI cli 启动。
- **规则／机制**：发送 GET /api/oauth/profile，使用 axios UA、JSON Content-Type、OAuth Authorization 与八项有序 Header。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；真实 TUI 运行取得完整请求。
- **实现**：仅在可信 cli entrypoint 下作为独立 strict egress 生成。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-008 essential 请求生命周期顺序

- **范围**：hello、policy-limits、settings、oauth-profile 与 messages；sdk-cli／cli。
- **规则／机制**：sdk-cli 和 TUI 分别遵循已实测的 essential 请求偏序；policy-limits 与 settings 不伪造固定先后。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；54 个正式运行对生命周期请求序列复算。
- **实现**：Persona 生命周期状态机调度独立 egress；关闭流量本身不作为一致性比较维度。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-009 count_tokens 完整 wire

- **范围**：count-tokens；真实 TUI token 计数条件。
- **规则／机制**：POST /v1/messages/count_tokens?beta=true，使用 Claude SDK 身份 Header，并按 model、messages、tools 顺序发送 Body；model 必须来自同一 Release 的显式能力目录。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：3 条 R/M 原子断言通过；36 个正例与 sdk-cli 基线条件对照覆盖 endpoint、Header 和 Body。
- **实现**：作为独立 persona_strict egress 编译，不与遗留 token-count 别名混同。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-010 OAuth token refresh 完整 wire

- **范围**：oauth-token-refresh；过期 OAuth 凭据条件。
- **规则／机制**：POST platform.claude.com/v1/oauth/token，使用 axios 七项 Header 且不发送 Authorization；Body 字段顺序和 grant_type 固定，凭据只在运行时注入。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：3 条 R/M 原子断言通过；隔离 refresh 正例与普通推理基线条件对照闭合。
- **实现**：作为独立 persona_strict credential-lifecycle egress；秘密不得进入画像、日志或收据。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-EP-011 MCP server 目录完整 wire

- **范围**：mcp-servers；真实 TUI 的 MCP 目录条件。
- **规则／机制**：GET /v1/mcp_servers?limit=1000，使用 OAuth、anthropic beta/version、MCP capabilities 与 protocol version 的有序 Header。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；四个 TUI 正例与 sdk-cli 基线条件对照确认 endpoint 和 Header。
- **实现**：作为独立 persona_strict egress；只有已批准 TUI／MCP 条件可触发。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-HDR-001 messages 基础 Header 与 OAuth 身份

- **范围**：messages-inference；Linux/amd64、first-party OAuth 基线。
- **规则／机制**：按实测大小写和顺序输出基础 Header，包括 Bearer OAuth、anthropic-version、压缩能力、Stainless 平台向量、x-app 与准确 Content-Length。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：7 条 R/M 原子断言通过；基础八条推理请求覆盖 Header 序列、值和 Body 长度关系。
- **实现**：HeaderSlots 由 Release、可信账号和 Body wire 派生；入站 Header 不得覆盖官方身份槽。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-HDR-002 User-Agent 与入口 attribution

- **范围**：messages-inference；sdk-cli、真实 cli 及获准 UA 条件段。
- **规则／机制**：UA 版本来自 Release，入口段与 billing cc_entrypoint 同源；agent-sdk、client-app、workload 只按受信条件追加。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；sdk-cli、TUI 与条件 UA 场景覆盖两处入口身份关系。
- **实现**：Release 和 TrustedEntrypointFacts 共同派生；禁止消费入站 UA 或自报版本。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-HDR-003 anthropic-beta 有序组合

- **范围**：messages-inference；主请求、子代理和 ANTHROPIC_BETAS 条件。
- **规则／机制**：基础 beta 按实测顺序输出；子代理按规则省略末项；额外 beta 修剪空项后插入指定位置且不去重。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；基础主／子代理和 beta-deduplicate 条件场景确认顺序与重复保留。
- **实现**：BetaPolicy 由 Persona Compiler 执行；未知 beta 或范围外条件 fail-close。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-HDR-009 条件身份 Header 与 attribution 联动

- **范围**：messages-inference；additional-protection、client-app、remote container/session、agent-sdk 与 workload 条件。
- **规则／机制**：条件 Header、UA 段与 workload attribution 必须从同一受信事实按实测值和槽位共同生成；条件不成立时省略。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：12 条 R/M 原子断言通过；单条件和组合条件矩阵覆盖出现、省略、值传递、顺序与跨字段一致性。
- **实现**：ClaudeIdentityFacts 和 HeaderSlots 联合派生；不允许第三方请求直接声明官方条件身份。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-HDR-012 请求与会话标识生命周期

- **范围**：messages-inference；单请求与同一多请求会话。
- **规则／机制**：每个请求生成不复用的 UUID request-id；同一会话的多请求复用 Session-Id。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；八条推理请求与三个多请求运行确认唯一性和复用边界。
- **实现**：request-id 为 attempt 级，Session-Id 为 Persona 会话级；跨 Persona／Release 禁止复用。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-HDR-014 子代理身份与父子谱系

- **范围**：messages-inference；一级至三级 Agent 请求。
- **规则／机制**：子代理使用 17 位 agent-id、复用会话并标记 subagent attribution；二级及更深追加直接父 agent-id，层级唯一性与链路关系必须一致。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：5 条 R/M 原子断言通过；depth1／2／3 与前台基线覆盖 Header、Body、会话和谱系关系。
- **实现**：由可信 AgentLineageFacts 派生；无父链事实时不得补造 agent 身份。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-HDR-029 自定义 Header 语法与保护槽

- **范围**：messages-inference；获准 ANTHROPIC_CUSTOM_HEADERS 条件。
- **规则／机制**：按行和首冒号解析并保持输入顺序；空名称 fail-close；自定义项不得覆盖官方 request-id，插入位置固定。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：4 条 R/M 原子断言通过；合法语法、无效名称和受保护槽位场景均已实测。
- **实现**：由 Compiler 的受控扩展槽处理；Header 名和值先验证再进入最终 wire。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-PROTO-001 HTTP/1.1 与端点 ALPN

- **范围**：hello、messages、policy-limits 与 settings；Linux/amd64 原生 TLS。
- **规则／机制**：应用层使用 HTTP/1.1；hello/messages ClientHello offer http/1.1，policy-limits/settings 的实测分支省略 ALPN。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 P/R/M 断言。
- **实测**：1 条 R/M 与 1 条 P/M 原子断言通过；12 条应用层请求及同一原生 TLS attempt 的四端点对照闭合。
- **实现**：TransportProfile 按 egress 选择 ALPN 行为并只建立 HTTP/1.1 执行能力。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-STATE-009 background 请求身份与形态

- **范围**：messages-inference；官方 background 会话。
- **规则／机制**：background 使用 x-app=cli-bg、cc_entrypoint=cli，并按已实测 Haiku／Sonnet 后台请求形态输出；前台保持 cli。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；background 正例与 sdk-cli 前台条件对照确认身份及 Body 形态。
- **实现**：只接受可信 background 状态事实；第三方入站不得通过 x-app 触发。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-TLS-001 原生 ClientHello CipherSuite 顺序

- **范围**：hello、messages、policy-limits 与 settings；Linux/amd64 原生 TLS。
- **规则／机制**：四类目标连接使用同一实测 CipherSuite 有序序列；不得以 relay 或服务端重建冒充 ClientHello。
- **源码**：未取得可审计原源码；TLS 行为以官方 2.1.226 原生二进制的 P/M 为唯一权威。
- **实测**：1 条 P/M 原子断言通过；v4-native-tls-baseline 中四个 ClientHello 正例及四个条件对照均已解析。
- **实现**：TransportCapability 必须在目标平台产生等价原生 TLS wire；平台变化需重建画像。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-TLS-003 TLS SNI

- **范围**：hello 与 messages；目标 TLS 连接。
- **规则／机制**：握手使用 api.anthropic.com SNI，并与已批准 Sink 一致。
- **源码**：未取得可审计原源码；规则权威来自官方 2.1.226 原生连接的 P/M。
- **实测**：1 条 P/M 原子断言通过；8 个承载选定请求的真实 TLS 连接确认 SNI。
- **实现**：SNI 由 EndpointProfile 的可信 Sink 派生，禁止从任意入站 Host 透传。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-TOOL-018 StructuredOutput 工具

- **范围**：messages-inference；json-schema 条件。
- **规则／机制**：把输入 schema 包装为唯一 StructuredOutput 工具，使用固定说明和 input_schema，同时保持实测 effort。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；json-schema 正交场景取得完整工具描述。
- **实现**：ToolPolicy 只消费规范化 schema，官方工具名称和说明来自 Release。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-TOOL-019 内置 Agent 与 Bash 工具往返

- **范围**：messages-inference；Agent 和 Bash 工具调用。
- **规则／机制**：tool_use 与同 ID tool_result 成对进入续轮；Agent 还派生带可信 agent-id 的子代理请求。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；Agent depth1、Bash 与无工具基线条件对照闭合。
- **实现**：工具往返由规范化消息和 Persona 状态共同编译，tool ID 关系必须保真。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-TOOL-021 MCP 工具与 deferred 全目录

- **范围**：messages-inference；stdio MCP 与 deferred MCP 条件。
- **规则／机制**：MCP 工具按实测前缀、名称和 input_schema 输出并完成同 ID 往返；deferred 场景必须保留全量目录，不得截断。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：2 条 R/M 原子断言通过；普通 MCP、33 项 deferred 工具与无工具基线条件对照闭合。
- **实现**：ToolPolicy 对全量已批准目录确定性编译；未知 MCP 能力不得自动进入 SupportEnvelope。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-TOOL-023 advisor 工具条件分支

- **范围**：messages-inference；显式启用 advisor 条件。
- **规则／机制**：仅在条件成立时加入已实测 advisor type 和 model；默认分支省略。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；显式正例与默认官方条件对照各一组。
- **实现**：由受信 feature 条件选择 ToolPolicy；入站工具声明不能冒充官方 advisor。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

### SPEC-TOOL-024 server web_search 派生请求

- **范围**：messages-inference；WebSearch 外层工具调用。
- **规则／机制**：外层调用派生独立的 server web_search 请求，携带已实测 tool descriptor 与 tool_choice。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；三条 web_search 请求与无工具基线条件对照闭合。
- **实现**：由 Persona ToolPolicy 生成派生请求；跨请求关系和会话身份必须保持。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

<!-- FW-F-ACTIVE-RULES-END -->

分组计数为 transport／协议／端点／连接 17 条、Header／身份 7 条、Body／缓存／状态／工具 16 条，
合计 **40 条 RequiredRules**。底层 110 条原子断言仍全部保留：106 条支撑画像，3 条本地上下文装配
和 1 条本地拒绝组成 2 个 scenario-only 组。任何新增、删除、改写或 egress 迁移都必须取得新的目标
P／R／M、更新原子断言和映射并签发新的 ApprovalFact，不能直接编辑列表。

## 2.6 发现项、候选与历史材料的终态

FW-E 的 7,368 个发现项没有删除。FW-F v21／v5 追加终态账本并达到：

- 7,368／7,368 个 discovery 均有已解决记录，缺失、额外、重复、空绑定和孤儿引用均为 0；
- 331 个目标发送点、102 个 2.1.88 源码机制、71 个 HitCC 线索、57 条历史规则与 32 个语义候选族
  组成 593 个正交候选，593／593 均有唯一终态；32／32 个语义候选族也全部闭合；
- 4,523 个历史上下文原子全部归属：128 个 Markdown 导航由结构证据证明为非出站，4,395 个绑定到
  精确文档、标题和语义事实；
- FW-F v1 的 97 条机械规则提案全部撤回，活动数为 0；
- 发现项清零不等于把 7,368 项变成规则；110 条实测原子断言经责任边界归并后，只有 40 条
  RequiredRules 进入画像，4 条客户端本地断言只保留为场景证据。

| 材料 | 当前职责 | 能否单独支撑 2.1.226 活动规则 |
|---|---|---|
| `claude-code-2.1.220-official/` | baseline fixture、历史差分和探针设计 | 否 |
| `claude-code-2.1.88/` | 老源码机制线索和版本漂移核对 | 否 |
| `hitcc-2.1.197/` | 发送面与条件分支的线索地图 | 否 |

历史 v1～v4 制品继续作为不可变审计历史；`discovery-clearance-v5-final/withdrawn-rule-proposals.json` 保存
97 条提案的逐项终态，不能被删除或重新作为活动规则消费。

## 2.7 机器复算与强制门禁

先对 v21 Campaign 复算 77 个场景、394 条请求、49 个维度和 593 个候选，动态生成原子断言，再执行
7,368 项发现清账和 110→40 规范化。旧目录只读保留，每次复算必须写新目录：

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
- 历史字段 `measured_rule_count = 110` 表示 AtomicAssertionLedger 的原子断言数，不再表示画像规则数；
- `withdrawn_v1_proposal_count = 97`；
- 全部 `gate_counts = 0`；
- 3 条 TLS 原子断言均严格绑定 P/M，107 条普通原子断言均严格绑定 R/M；
- 110 条原子断言均有非空 `egress_ids`、独立 `PAIR-*` 和官方正例；条件命题需要条件对照，
  无条件命题使用零违规分母，不得伪称独立官方负例；
- 110 条原子断言必须恰好一次归属：106 条映射到 40 条 RequiredRules，4 条映射到 2 个
  scenario-only 客户端本地组；
- 八类 strict egress 均至少有规则，且 Approval 中分别绑定自身 SPEC 集合；
- 不存在 `unmeasured_feature_boundary`；指南、RequiredRules manifest、Snapshot、
  EvidencePackage 与 SupportEnvelope 的 40 个规则 ID 必须完全一致；
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
2. 以目标 bundle 原生发现为主，加载 2.1.220／前一批准版本以及已冻结的 2.1.88、HitCC 历史候选
   台账补充线索，不继承其目标版本结论；
3. 对每个拟活动命题构造可达正负场景；真实上游 run 在 Vircs 采集，故障语义在隔离 relay 注入；
4. 采集目标 R/M；涉及 ClientHello／ALPN 时另采原生 P；
5. 运行原子断言，把通过断言的 request-egress 命题写入新的 AtomicAssertionLedger；
6. 按 Codex 六字段和 Sub2API 实现责任归并 RequiredRules，客户端本地行为进入 scenario-only；
7. 对全部发现、候选和旧规则逐项 disposition，未决数必须为 0；
8. 先冻结跨模型 RequiredRules，再为每个拟登记主模型采集基线、effort、thinking、TUI、Agent、
   background、WebSearch、count_tokens、fallback 与状态续接差异，生成独立 ModelCapabilityCatalog；
9. target-first 生成新 Snapshot／Release，再用同一 Schema／Compiler 表达历史 fixture；
10. 证据不足的模型或能力留在明确边界，不得复制公共规则、补造别名、缩小数字或用旧版本 wire
    提升等级。

同一版本新增模型时，先比较其全部公共规则适用条件；公共机制未变时不得把 40 条规则复制一份。
只向能力目录追加有逐场景官方证据的模型差异，并重新生成 Profile／Wire／Release／Bundle 内容摘要。
目录的模型和别名均为显式闭集；官方或第三方入站请求未知值时必须 fail-close。若发现新的共享出站
责任、字段语义或状态机机制，则返回规则准入流程新增／修改 RequiredRule，不能把机制缺口伪装成
“模型配置”。

2.1.88 真源码和 HitCC 2.1.197 必须在首次纳管时完成全量提取，并冻结原始目录摘要、提取工具摘要、
覆盖范围、稳定候选 ID、原文位置和提取收据。原始目录与提取合同未变化时，后继 Campaign 只重放该
不可变候选基线，不重复扫描 2.1.88 的 `src／node_modules／vendor` 或逐篇重新提取 HitCC；但每个候选
仍须结合本次目标原生发现、前一批准版本和运行证据重新取得唯一 disposition，历史 disposition 不得
直接复制为新版结论。出现目标证据冲突、无法解释的新 host／sink／wrapper／feature、候选缺失或孤儿、
覆盖漏洞，或者影响提取语义、覆盖面或稳定 ID 的工具变化时，才回查原始资料并全量重审；重审必须建立
新的内容寻址候选基线和追加式收据，旧基线只读保留。

# 第三部分 Sub2API 实现与迁移

本部分只记录 Claude 方言和当前代码差距；共享控制契约见 Framework。硬性目标是：画像能表达的
换版只追加画像、证据和发布图，不修改执行代码；只有新协议、新状态机制或 Schema 缺口才能修改
共享引擎，并回归受影响 Persona 的 production active／rollback 与 validation candidate。

## 3.1 Persona 与执行链

```text
已登记 Claude Code／Claude Desktop／Claude Code for VS Code
→ OfficialIngressCatalog 准入
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

**第三方 Agent 工具目录（仅作未来方案归档，当前不实现）。** KiloCode／zlfcode 等 Agent 自带的工具名称、说明、Schema 和顺序不因
Anthropic API 接受自定义工具而自动成为 Claude Code 画像。与目标版本官方内置工具无损等价的项，
可在单独批准后使用 Framework §2.3 的 `official_builtin_lossless` 双向映射；`kilo_local_recall`、
`todowrite`、`background_process` 等没有已批准内置等价项的能力，只能作为未来可选的
`official_mcp_bridge` 处理：

1. 在独立 Campaign 中固定 Kilo 产品／版本和完整工具目录摘要；
2. 让真正的目标版本 Claude Code 加载冻结的 `kilo_bridge` MCP 配置，实测
   `mcp__kilo_bridge__<tool>`、说明／截断、Schema、顺序、deferred／ToolSearch 和多轮工具往返；
3. 将官方目录与双向名称、参数、结果、ID、并行、流式和错误映射冻结进专属 ReleaseArtifact，并对
   官方入口与该第三方入口执行 RequiredRules／PAIR 和最终 wire 对拍；
4. 只有目录摘要和全部断言命中时进入 strict；未知版本、新增／变更工具、无法无损转换或证据不足均
   fail-close。

该扩展只有在未来把 IngressPolicy 另行改回 `canonical-semantic`、重新取证并批准后才可启动；当前
official-client-only 方案不需要、也不允许建设。KiloCode／zlfcode 不属于当前 40 条 RequiredRules 和
SupportEnvelope，运行语义必须保持 fail-close。Claude Desktop 与 VS Code 也不能自动继承 Claude Code
wire；它们因各自实测 Catalog 条目才获得准入，随后由目标 Persona 重新生成最终出站。

当前 `ProductionIngressInventory` 的逻辑入口与目标处置如下；每项仍必须展开全部物理别名：

| 逻辑入口 | 目标处置 |
|---|---|
| `official-messages-oauth`：`/v1/messages` | `migrated_strict`；完整 Catalog 命中后进入官方正向链 |
| `official-count-tokens-oauth`：`/v1/messages/count_tokens` | `migrated_strict`；独立 Catalog／语义门禁 |
| `third-party-messages-oauth`、`third-party-count-tokens-oauth` | `explicitly_retired + denied_before_oauth` |
| `chat-completions-oauth`：bare／v1 | `explicitly_retired + denied_before_oauth` |
| `responses-oauth`：bare／v1 HTTP、subpath、WS | `explicitly_retired + denied_before_oauth` |

只有最终路由到 `claude-code` firstParty OAuth 的调用才属于本闭集；同名 API Key、Antigravity 或其他
Persona 路径必须标记为 `rerouted` 并指向其真实产品边界。代码中的前缀别名、WebSocket／compact
分支、内部调用和 handler 转发均须展开到物理入口，不能只登记上述四个字符串。每项同时记录当前处置
和目标处置；不得用缩小 SupportEnvelope 隐藏仍可达的物理别名。

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

Claude 运行选择的唯一清单为：

```text
backend/internal/officialegress/catalogdata/claude/release-catalog.json
```

Claude 入站准入的唯一清单为：

```text
backend/internal/officialegress/catalogdata/claude/official-ingress-catalog.json
```

该 Catalog 与 Release Catalog 正交：前者只证明某个入站 wire 属于已登记官方产品／版本的批准形态，
后者决定最终出站使用哪个 Persona Release。入站账号 UUID、device_id、OAuth Header 和自报版本不能
越过调度边界；最终账号身份与官方 wire 均由被选中的 OAuth 账号和 active Release 重新生成。HTTP
自报身份不具备二进制级密码学证明能力，因此本文只主张“未命中完整 Catalog 的客户端必拒绝”，不把
单独 UA 命中写成官方客户端证明。

清单逐个绑定 Profile／Wire 的规范内容寻址路径与原文字节摘要、Release／Bundle 摘要、规则／断言／
端点数量和模型闭集，再分别保存 `validation_candidate`、`production_active` 与
`production_rollback`。active 必须是可加载并带 Approval 的 Release；当前 rollback 是显式
`operational-deployment`，只记录提交、镜像 digest 和已演练收据，不能伪造第二个 Release。

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
| `ModelCapabilityCatalog` | 已登记主模型、精确别名、effort、逐场景 Body／Header 顺序、fallbacks、辅助模型和锁存状态；不得复制跨模型 RequiredRules |
| `RetryPolicy` | 起步、指数、封顶、抖动与终止边界 |
| `PrivacyMode`／`Digest` | 证据隐私模式与画像摘要 |

条件 Header 必须由槽位表达，不得散落为版本 `if`。槽位至少包含 `Slot`、`Name`、`WireName`、
`Value`、`Source`、`Condition`、`AlternateGroup`；条件只读取规范化语义和受信上下文，不能读取
第三方同名 Header。条件不成立时整条省略。

ReleaseArtifact Store 保存全部不可变 Release；Runtime Selector 只保存 Claude production active／rollback
引用，Validation candidate 使用独立的 immutable release reference，不借用 rollback。Claude 首次激活前
必须提供可验证的真实回退目标，可以是上一正式 Release 或冻结的遗留实现；不得复制 active 摘要伪造
回滚对。新增版本只追加快照与发布图节点。版本泄漏 baseline 只登记既有债务，不能通过新增版本常量
换取门禁通过。进程启动时统一加载 Catalog；Profile、Wire、Approval、changeset、状态命名空间、连接池
和执行 policy ID 均从所选 Release 派生，业务代码不得按版本维护第二套选择事实。

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
| `/v1/messages/count_tokens`、`POST platform.claude.com/v1/oauth/token`、`GET /v1/mcp_servers?limit=1000` | `persona_strict` | v21 已取得条件成立／不成立样本并纳入独立 egress、SPEC／PAIR、SupportEnvelope 和 Guard；当前只批准 validation-only |
| usage、OAuth exchange、cookie authorize／organizations、account test、upstream models，以及遗留 token-count／OAuth-refresh 别名 | `non_persona_managed` | 登记 route／Sink、认证、endpoint、client、超时、重试、秘密与审计，不把遗留别名冒充新的 official-client strict 身份 |
| 未登记 Claude OAuth 路径 | `denied` | enforce 时 fail-close |

`non_persona_managed` 是受管第三态，不是 `out_of_scope_passthrough`。它不计入当前 40 条 RequiredRules 及
SupportEnvelope 的逐规则分母，但必须有独立的 source-to-sink 闭集、运行断言和失败策略；未来若要求
仿真官方客户端在该端点的 wire，必须先取得证据、原子化规则并正式晋升为 `persona_strict`。

Envelope 只向共享 Executor 暴露 Persona／Release 摘要、Route／Sink、method／protocol、attempt、Body
可重放性、TransportCapability、Identity／Dialect attestation digest 和受保护请求能力。共享层不得
解释 Claude beta、Header 槽位、BodyShape、agent 层级、Stainless 或重试语义，也不得要求 Claude 填充
Codex IdentityMode、HeaderPolicy、BodyPolicy、BehaviorPolicy 或 fallback 字段。

| 规则组 | 数量 | 画像／执行落点 |
|---|---:|---|
| TLS／协议／端点／连接 | 17 | `Transports`、`Endpoints`、client lifecycle + adapter |
| Header／认证／Beta | 7 | `HeaderSlots`、`BetaPolicy` + Claude Compiler |
| Body／缓存／metadata／状态／工具 | 16 | `BodyShape`、IdentityFacts + Claude Compiler |

40 条 RequiredRules 均具有画像／执行落点，并由 106 条目标 P／R／M 原子断言支撑；另外 4 条
客户端本地断言只进入场景层。原生 TLS／ALPN、
故障重试、真实 TUI、Agent／background／hook、custom Header／beta／metadata、remote、附件、MCP、
advisor、web_search、count_tokens 与隔离 OAuth refresh 已纳入；合法零流量仍只作支撑事实。调度、计费
和服务级节奏不由画像改写。

## 3.4 当前实现与 FW-A～FW-H 迁移

FW-G 的历史 Sonnet Candidate 与三模型后继均已完成各自范围的隔离验收。当前后继变更收窄入口
准入和本轮交付边界；历史 FW-H 收据保持不可变，但不再作为当前目标状态：

| 层 | 当前事实 |
|---|---|
| 画像与发布图 | 2.1.226 Profile、Wire、Snapshot、ReleaseArtifact／Bundle 均已内容寻址并登记到 Claude Release Catalog；2.1.220 只保留同一 Schema／Compiler 下的 baseline fixture |
| 统一执行链 | Claude production active 已接入共享 Persona Release Selector；PersonaPlanner、IdentityFacts、Compiler、authority、issuer 与状态仍属 Claude 方言，Codex facade 和 final wire 保持零差异 |
| strict 入口 | 正向只有 `official-messages-oauth` 与 `official-count-tokens-oauth`；共享物理路由先执行 OfficialIngressCatalog 门禁。第三方 Messages／count_tokens、Chat Completions 的 bare／v1 别名、Responses 的 bare／v1 HTTP／subpath／WS 全部是凭据前拒绝负例，不进入 RequiredRules 正向分母 |
| strict／managed 出站 | 八类 `persona_strict` 进入 Compiler／Executor／Guard；`non_persona_managed` 进入独立策略，未知 OAuth 出站保持 `denied` |
| 语义与兼容 | 只对 Catalog 已登记官方来源生成规范化语义；Persona system／identity 由批准事实派生。第三方兼容和工具桥不属于当前实现范围 |
| 候选验收 | 历史三模型验收继续证明 2.1.226 出站画像；当前 official-client-only 后继必须在 DMIT 重跑三类官方客户端正例、全部第三方／未知形态负例、回退／恢复和 Codex 隔离 |
| 交付边界 | DMIT 只承担候选验收，最高状态为 `ready_for_operator_release`；Vircs 为用户管理的生产机，状态固定为 `operator_managed／unverified／not_touched`，本任务不得签发 Vircs DeploymentFact |

| 阶段 | 变更 | 完成判据 |
|---|---|---|
| `FW-A` | 只读冻结 Codex、Claude 2.1.220 证据、生产运行态和遗留发送面基线；不新增 Claude 代码或绑定 | 两类基线可复算；遗留输出只标 diagnostic-only；没有生产写入 |
| `FW-B` | 按 Framework 抽取 Codex 已证明的暂定共享合同并保留 Codex facade | 共享内核无 Codex 专用策略字段；Codex active／rollback final wire 零差异；不宣称多 Persona 已冻结 |
| `FW-C` | 验证并发布 Codex-only 正式制品，完成回滚、恢复和稳定观察 | 本轮没有新增 Claude Persona／画像／strict 注册；Codex 发布与激活收据闭环 |
| `FW-D` | 建设 Campaign、正交事实、两段式批准、Snapshot／Release Store、candidate／PAIR、晋升与激活工具链 | 只用 Codex／合成数据自测；越权、摘要变化、范围缺口和收据不匹配均由机器阻断 |
| `FW-E` | 第一步冻结最新 stable；从目标 bundle 原生发现发送点，加载已冻结的 2.1.88／HitCC 候选台账和 2.1.220／前一批准 stable，与目标发现组成并集，分开建立 DiscoveryInventory、SemanticRuleCandidate 和 AtomicAssertionLedger，完成差分、P／R／J／M 和 Evidence 封存，再建立两个 Inventory 与 observation-only Sink | 目标版本、完整 sink／discovery inventory、语义候选、只含 SPEC 的原子断言台账和 EvidencePackage 可复算；没有截断或未分类项；停在 `evidence_recorded`，尚不定义目标 Schema／Snapshot 或签发 Evidence 批准 |
| `FW-F` | 先把 FW-E 全部发现和语义候选逐项收敛到可审计终态并清零未决项，再由最新 stable 证据生成 Schema、目标 Snapshot、Persona 和不可部署样例；随后用同一 Schema／Compiler 表达 2.1.220 rollback fixture，批准 Profile、范围和多 Persona 合同 | 全量 DiscoveryDispositionLedger 无缺失、重复或未决项；target-first 样例与跨 Persona 负例通过；ApprovalFact 完整；Codex 生产收据对应最终合同；selector 未改变 |
| `FW-G` | 实现本次已批准的 40 条最新 stable RequiredRules，完成受管语义层、辅助出站三态、全部 strict 入口原子断言、独立复测、DMIT candidate 和 rollback 验收 | 40 条规则、106 条画像原子断言和 4 条客户端本地场景断言通过；三模型正例、范围外拒绝和回退闭环；签发 AcceptanceFact |
| `FW-H` | 构建固定正式候选镜像，仅在 DMIT 完成 official-client-only 正负矩阵、回退／恢复和稳定观察后交付 | DMIT 签发 `ready_for_operator_release`；Vircs 不连接、不部署、不验证，生产替换由用户自行执行 |

历史 Sonnet FW-G 完成事实以 [FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)为准；
[三模型验收尝试收据](egress/maintenance/claude-fw-g-three-model-acceptance-attempt.json)只保留首次账号权益
阻断的历史事实，当前结论以
[三模型 FW-G 验收收据](egress/maintenance/claude-fw-g-three-model-acceptance.json)为准。FW-G 已完成，
历史 FW-H、request-id、Catalog DeploymentFact 和生产验收收据保持不可变，只说明各自签发时的运行
事实，不证明当前 Vircs 或当前 official-client-only 候选已经部署。当前结论必须以后继
OfficialIngressCatalog／Approval、源码 transition 和 DMIT `ready_for_operator_release` 收据共同判定。

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

源码存在不能证明 production active。FW-A～FW-G 已完成：目标 stable 2.1.226 的 FW-E Campaign 为
`claude-code-2_1_226-fw-e-semantic-20260818-e577e144a`，其 Store 停在 `evidence_recorded`。旧 Campaign
`claude-code-2_1_226-fw-e-final-20260818-93f2edbc9` 及其“7,425 条规则”收据只保留为历史错误事实，由
[语义规则纠正收据](egress/maintenance/fw-e-semantic-rule-correction/receipt.json)替代，禁止继续作为
FW-F 输入。FW-F 后继 Campaign 为 `claude-code-2_1_226-fw-f-required-rules-v5-20260819`，Store 已到
`profile_approved`：7,368 个发现、593 个正交候选和 32 个语义候选族未决数均为 0，2.1.226 目标画像／
Release 与 2.1.220 fixture 已按 target-first 顺序生成，EvidenceApprovalFact 和 `validation_only`
ProfileApprovalFact 已签发。40 条 RequiredRules 由 Vircs 上 2.1.226 官方客户端的 106 条画像
P／R／M 原子断言支撑，另有 4 条客户端本地场景断言；110 条均通过。事实见
[清零收据](egress/maintenance/fw-f-discovery-clearance/receipt.json)和
[FW-F RequiredRules 规范化收据](egress/maintenance/fw-f-required-rules-normalization/receipt.json)。

历史 FW-G Campaign、三模型后继和 FW-H／Catalog 收据完整保留，继续证明 2.1.226 出站画像与当时的
运行事实，但不再授予第三方入口或当前生产状态。当前源码追加 official-client-only Approval 和
OfficialIngressCatalog：只有 Claude Code 2.1.226、Claude Desktop 2.1.237、Claude Code for VS Code
2.1.239 的已登记 wire 可进入 Messages／count_tokens 正向链；第三方与未知形态统一在 OAuth 凭据读取前
拒绝。该后继在 DMIT 验收前状态为 `candidate_sealed／not_ready_for_operator_release`；DMIT 通过后最高
状态为 `ready_for_operator_release`。Vircs 始终保持 `operator_managed／unverified／not_touched`。

### 3.6.1 FW-H 当前续作检查点（2026-08-22）

本节只记录当前 official-client-only 后继的可恢复执行点，不改写历史 FW-G／FW-H 收据。当前状态为
`candidate_deployed／not_ready_for_operator_release`，尚未签发本轮 `ready_for_operator_release`：

| 项目 | 当前事实 |
|---|---|
| 源码 | 提交 `8a33e1c902f5b5bf911b4625dea6f35b70321183`（`feat: Claude OAuth 仅接受官方客户端`）；提交前 `go test ./... -count=1`、`make check-egress-spec`、`go vet ./...` 与 `git diff --check` 均通过 |
| 构建链 | Mac 原生构建前端并交叉编译 `linux/amd64` 后端，ARM64 只封装 amd64 镜像；后端二进制 SHA-256 为 `5b93521312a1625dd661860b074aae6aaa0e79ccc0bb4e1964a2337bb21b9bc2` |
| DMIT 镜像 | 标签 `sub2apiplus:official-client-only-8a33e1c90`，OCI image／运行摘要均为 `sha256:1de00f4b89a0aa16184186a923856ea13ee88cd504681c0233bd03d77b7f9ad4` |
| DMIT 容器 | 容器 ID `184fce0d30d164e6e39d7949d37abe21c4d05d57aa15910aace8579746c3c35b`，`running／healthy`，重启数 `0`；依赖容器未替换 |
| Compose | `/root/Docker/sub2apiplus/app/docker-compose.claude-official-client-only.yml`，SHA-256 `46ec692020f01b3769f2dc8c34c91ebd27ca88aae2313ff13f564f3f5b2e6279`；旧 override、正式镜像和回退镜像均保留 |
| 在线矩阵 | 脚本 `/root/Docker/sub2apiplus/app/data/deployment-evidence/claude-official-client-only-8a33e1c90-live-matrix.sh`，SHA-256 `6f7a5239ab6eaf1ab58b9fb2ec689f78c8a4efd19081d65461417bf7c6ec933d`；wire 文件 `claude-wire-a7d2c91f.json`，SHA-256 `a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d` |
| 当前阻断 | 矩阵脚本已补齐 `x-stainless-timeout`；`attempt-002` 已通过 official-client-only 准入并到达 Anthropic，但上游返回 `401`。通过 DMIT 后台对账号 `#100` 执行正常“刷新令牌”又得到 `invalid_grant: Refresh token not found or invalid`，证明 access token 与 refresh token 均需重新授权 |
| 账号边界 | 只处理账号 `#100`；账号 `#101` 保持暂停／不可调度，不得改变。DMIT 后台已生成 `#100` 的重新授权流程，授权 URL、PKCE verifier、授权码和 OAuth Token 不写入本文或仓库 |
| Vircs | 未连接、未部署、未验证、未修改，保持 `operator_managed／unverified／not_touched` |

后台页面的版本角标仍显示历史构建字符串 `v0.1.177-4-fw-h-final-e2c80213a`，不能据此判定当前候选身份；
本轮续作必须以源码提交、OCI digest、容器 ID、Compose 摘要和收据联合判定。最终收据签发前应明确处置
该显示差异：重新构建正确版本字符串，或在收据中把它登记为非身份字段，不能保持歧义。

恢复执行时按以下顺序继续，中间不得把账号故障误判为画像通过或失败：

1. 由用户在已生成的 Claude OAuth 页面完成账号 `#100` 登录授权，把授权码通过 DMIT 后台正常流程
   持久化；禁止直接修改数据库，并复核 `#101` 未变化；
2. 运行在线矩阵 `attempt-003`，验证 Claude Code 2.1.226、Claude Desktop 2.1.237、Claude Code for
   VS Code 2.1.239 的 Messages／流式响应／工具往返／标题／count_tokens 正例，以及 Sonnet、Opus、
   Fable 的登记模型能力；
3. 验证 KiloCode、zlfcode、curl、第三方 Messages／count_tokens、Chat Completions、Responses、未知
   版本、Header、metadata、System、工具目录和跨 Persona 请求均在读取 OAuth 凭据前拒绝，并复核
   Codex final wire 隔离；
4. 使用本机真实 Claude Code、Claude Desktop、Claude Code for VS Code 完成正例；测试前只读确认
   客户端版本与 OfficialIngressCatalog 一致；
5. 在 DMIT 切回已冻结回退镜像验证，再恢复上述 OCI digest，并重跑核心矩阵；完成稳定观察、容器／
   依赖身份、日志、磁盘、网络和 3x-ui 核对；
6. 清理本轮临时传输包和构建临时目录，但不执行 Docker prune、不删除回退镜像或验收日志；随后追加
   独立的 `ready_for_operator_release` 收据，跑全量门禁并完成文档闭环。

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
| 7. 候选交付 | 固定正式候选镜像、DMIT 正负矩阵、回退、恢复和收据 | `ready_for_operator_release`；生产 DeploymentFact 由用户另行签发 |

## 4.1 身份、正交事实与当前状态

Campaign、candidate、attempt 的通用边界见 Framework §3.2。Claude Campaign 还必须冻结：

| 身份维度 | 要求 |
|---|---|
| 官方产物 | npm URL、integrity、tgz／二进制／bundle SHA、内嵌 Bun／SDK、平台与架构 |
| 隐私模式 | `essential-traffic`／`no-telemetry`／`default` 不得混用 |
| entrypoint | `sdk-cli` 与真实 TTY 的 `cli` 分开取证；`-p` 不能伪造 TUI |
| 平台角色 | Linux x86_64 是当前主基准，Darwin arm64 只作交叉复核 |
| 工具身份 | 采集、relay、脱敏、提取、编排、收据和环境快照的摘要 |
| 生成策略 | `claude_fw_g_generation_policy_<version>.json` 的摘要；版本、输入摘要、规则／断言计数、模型／effort、TLS 向量和迁移输入只能从该文件加载 |
| 账号与条件 | OAuth 组织、模型能力目录、精确别名、feature、scope、故障注入和场景矩阵 |

Campaign 只绑定不可变身份。下列事实分别追加并以摘要引用，不得由单一状态枚举互相替代：

| 维度 | Claude Campaign 必须记录的事实 |
|---|---|
| Discovery | 发现版本、时间、来源、`SinkInventory` 和 `DiscoveryInventory`；不得改变 Runtime Selector |
| Evidence | 每条规则独立的 `verified／observed／blocked／regressed_evidence` 与 EvidencePackage |
| Approval | 固定的 SupportEnvelope、两个 Inventory、Persona 派生、三个 Envelope 计划、目标规则全集、迁移决策、画像和验收清单 |
| Validation | 独立 candidate ID、不可变 Release 引用、源码／测试／镜像摘要和 AcceptanceFact |
| Runtime Selector | Claude `production_active` 与 `production_rollback`；不保存 validation candidate |
| Deployment | `accepted_not_activated → canary_passed → active → rollback_verified → restored_active`、实际入口处置、三个 Envelope 与收据 |

`official_sealed`、`profile_approved`、`candidate_sealed`、`compared` 和 `ready` 只是流程检查点；它们
不是规则证据等级或生产角色，测试通过也不等于 production active。

当前权威状态见 §2.1、§3.6 及后继收据：2.1.226 的发现与候选已清零，40 条 RequiredRules 已验证；
official-client-only Approval 把正向 SupportEnvelope 收窄为两类官方逻辑入口，第三方入口均显式退休并
在运行时凭据前拒绝。历史 DeploymentFact／RemovalReceipt 不覆盖。当前任务只允许在 DMIT 形成
`ready_for_operator_release`，不能把候选测试写成 Vircs production active。

## 4.2 官方取证、分类与批准

### 4.2.1 P0 与官方证据

P0 只读确认目标版本未混入基线、官方包可复算、Vircs 官方取证环境和 DMIT 候选验收环境均不被采集改动、
证据目录和端口隔离、独立 candidate 环境可用、构建职责明确；身份或恢复条件不完整时停止。FW-E 首先查询并冻结官方
`stable`、发行物和环境身份；此前不得定义目标 ProfileSchema、Snapshot 或 candidate，2.1.220 也没有
目标版本选择权。

Claude 没有可用的目标版本官方源码，必须以官方生产 bundle 为主：

1. 验证来源、integrity、二进制与 bundle 摘要，确定性提取 Bun SEA；
2. 解析目标 bundle 的全部文本模块，从入口独立枚举网络 sink、请求构造、Header／Body 写入、环境与
   feature gate、端点、重试和跨请求状态；不得只围绕旧规则或固定字面量探针搜索；
3. 取已冻结的 2.1.88 真源码候选、HitCC 线索台账、2.1.220／前一批准 stable 规则和目标原生发现的
   并集，逐项比较语义锚点、source-to-sink、条件和运行依赖；
4. 由静态条件反向生成 baseline、条件 Header、agent、retry、OAuth、端点、异常和 entrypoint 哨兵；
5. 全 host／path 观测官方进程出站，不得用待证 endpoint 预筛；每个 run 同时封存 P／R／J／M、实际
   argv／环境、工具摘要、宿主回执、秘密扫描和恢复事实；
6. 产物、平台、隐私模式或产出侧工具变化时新建 Campaign，临时网络失败才新建 attempt。

若目标版本拟登记多个主模型，基础 Campaign 先证明公共规则；随后对每个模型建立正交能力轨道，至少
覆盖基线、五档 effort、thinking 关闭、TUI、Agent、background／background subagent、WebSearch、
count_tokens、官方 fallback 与续轮状态。每个场景保存原始请求、必要响应、manifest、runtime receipt
和生产零差异证明。只观察到模型字符串相同不足以继承；缺失场景的模型不得加入 active 能力目录。

无 sourcemap 的 minify Bun SEA 是目标静态权威；词法窗口、附近 sink 和 minify 符号只用于定位，不能
代替完整语法树、调用关系、反向数据流和运行闭环。JavaScript 无法证明的 Bun／原生模块、动态调用或
宿主 transport 必须登记边界，并由进程级、网络级证据补足。

### 4.2.2 目标原生闭集与跨来源矩阵

每次换版先生成目标 `SinkInventory`。目标规则候选是下表五类输入的并集，不由旧台账单独决定：

| 输入 | 每版动作 | 权威边界 |
|---|---|---|
| 目标原生发现 | 全量 AST／词法发现并与全 host／path 运行 inventory 对照 | 目标规则的静态与运行权威 |
| 前一批准 stable | 比较规则、条件、sink 和最小运行哨兵 | 只提供迁移候选 |
| 2.1.220 官方材料 | 重放历史规则与 baseline fixture | 只作差分和探针设计 |
| 2.1.88 真源码 | 重放已冻结的 102 个源码机制候选 | 只作机制线索 |
| HitCC 2.1.197 | 重放已冻结的 71 个线索 | 只作发送面和分支线索 |

首次历史基线必须绑定原始目录、工具、覆盖合同、稳定 ID、原文位置和收据。2.1.88 全量覆盖
`src／node_modules／vendor` 的网络反向切片；HitCC 每篇 `clue_source` 和未抽成 clue 的 Markdown 项均
须无截断登记并映射，不能按关键词未命中排除。后继 Campaign 重放全部稳定 ID 并重新计算目标
disposition；可以复用提取结果，不能复用目标结论，旧资料也不能单独支持任何迁移决策或活动规则。

仅在原始摘要／引用无法复算、提取能力或覆盖合同实质变化、目标出现无法解释的新网络机制、目标证据
与历史候选冲突／闭集缺口，或候选粒度不足以唯一处置时，才全量重审原始资料。重审建立新的内容寻址
基线和追加式收据，记录触发原因、摘要、ID 迁移和覆盖差异；旧基线不得覆盖。

跨来源事实分三层保存：

| 层 | 内容与限制 |
|---|---|
| `DiscoveryInventory` | 无截断保存目标调用、历史原子命题、clue 和 Markdown 原子；不直接生成规则 |
| `SemanticRuleCandidate` | 以 `source_ids` 归并稳定 `CAND-*`；`observed／blocked` 均禁止进入生产 |
| `AtomicAssertionLedger` | 只保存经语义审查、可执行且可逐项断言的 `SPEC-*`；不得混入发现或候选 |

一个发现可关联多个语义候选，但必须双向闭合。目标新增发现只有明确命题、条件、证据通道、场景和
断言后才能分类为 `add`；历史规则只有静态不可达和运行负例同时成立才可 `delete`。扫描器不能判断时
保留 `unclassified`；已有稳定证据但语义未定时使用 `mapped_validation`，仍不得进入 SPEC 或生产。
`catalogued_context` 只允许作为 FW-E 不可变结果，FW-F 必须追加处置，不能留到下次换版。

`SinkInventory` 至少覆盖 `fetch`、Anthropic SDK resource、Node／Bun HTTP、TLS、socket、
WebSocket／EventSource、动态 wrapper 和外部网络子进程；禁止上限或抽样，每个调用点必须反向绑定唯一
disposition。下列门禁任一失败都阻止 FW-E 退出：

| 门禁 | 失败条件 |
|---|---|
| Sink 闭集 | 未分类、截断、身份冲突、来源不可复算，或运行出现 inventory 外 host／path／sink |
| 历史闭集 | 2.1.88、HitCC、历史规则或前一批准规则没有唯一处置 |
| 规则准入 | 新机制不能显式 `add`、strict 候选无目标场景，或发现／候选被自动写成 SPEC |
| 隐私边界 | `DISABLE_TELEMETRY`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 的值、读取点或 gate 未绑定目标产物；关闭分支未登记 `record_only`，或据零流量删除影响 essential 请求的共享状态 |
| 三层闭合 | 数量、双向引用、摘要或身份不一致，或仍有未允许的 `unclassified` |

机器执行顺序固定如下，`analyze-bundles` 会对每个平台自动运行锁定 TypeScript 解析器，并把 AST
调用点与无截断词法候选合并为 `target-sink-inventory.json`：

```text
claude_fw_e.py analyze-bundles
→ claude_fw_e_validation_closure.py
→ claude_fw_e_crosswalk.py --capture-index ... --require-closed
→ claude_fw_e.py rule-assessments --cross-source-matrix ... --completeness-closure ...
→ claude_fw_e.py seal
```

disposition 必须覆盖目标 sink、历史候选和全 host／path 观测。telemetry／nonessential 只能
`record_only_disabled` 或保持阻断性的 `unclassified`，不得用零流量事实删除。`seal` 使用 v3 计划并
直接绑定目标 inventory、矩阵、closure 和 capture index；只有上表门禁通过、closure 为 `passed`、未
使用 host 预筛且摘要一致时，Store 才可写入 `evidence_recorded`。该状态不签发 Evidence／Profile
ApprovalFact。

产出工具或身份变化时必须在隔离目录建立后继 Campaign，旧事实只读保留；取证仍须设置两个隐私变量和
全 host 捕获开关，不得复用目标-host 预筛样本。

### 4.2.3 分类与批准清单

FW-F 先清零发现项，再设计目标画像。聚类不能减少逐项覆盖；每个 `discovery_id` 必须恰有一个已解决
记录，并绑定下列至少一种终态，跨语义时可有多个终态绑定：

| 终态 | 必须绑定 |
|---|---|
| `rule_bound` | 一个或多个既有／新建 `SPEC-*`，以及该发现对规则的证据角色 |
| `supporting_fact_bound` | 所属规则、画像事实或状态／条件／动态值事实及其稳定身份 |
| `managed_egress_bound` | `EgressDispositionInventory` 中的受管出站身份和处置 |
| `non_egress_proven` | 目标证据、可复算理由及为何不影响客户端出站 |
| `target_absent_proven` | 目标 stable 的静态不可达／不存在证据；需要时补运行负例 |
| `duplicate_bound` | 规范发现 ID；引用链必须无环并最终落到以上非重复终态 |

`DiscoveryDispositionLedger` 必须绑定 FW-E inventory 摘要；总数和 ID 集合相等，每项唯一解决且至少有
一个终态，所有引用存在并双向闭合。`unclassified`、`mapped_validation`、`catalogued_context`、未收敛
`CAND-*`、无主事实、缺失、重复和循环引用必须全为 0；全部语义候选须收敛到规则、事实、受管出站、
非出站或目标缺失，不得原位改名为规则。SupportEnvelope 缩小不能替代发现项处置。

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

字符串、minify 符号或相邻版本相同均不能单独支持 `inherit`；迁移、证据、生命周期和兼容类别分别
记录。ApprovalFact 必须冻结目标规则全集、SupportEnvelope、EvidencePackage、Profile、场景、端点、
断言、隐私模式和用途，并以同一摘要绑定 `DiscoveryDispositionLedger`、`ProductionIngressInventory`、
`EgressDispositionInventory`、Persona 派生、compatibility 边界、ActiveSupportEnvelope、
DeploymentTrafficEnvelope 和 RollbackOperationalEnvelope。

产物或环境身份变化新建 Campaign；批准范围、规则、迁移、画像、场景、断言或用途变化签发新
ApprovalFact 并建立 candidate；仅源码、测试、构建或镜像变化建立新 candidate。旧事实不得覆盖。
每个生产入口仍须选择 `migrated_strict／retained_legacy／explicitly_retired／rerouted`，每个已知 OAuth
出站仍须选择 §3.3 三态；缩小 SupportEnvelope 不能代替这些处置。

## 4.3 画像、Candidate 与制品

在不改变 production active／rollback 的前提下，把批准画像生成到全新绝对路径，并形成摘要、
inventory 和 selector 未变证明。入库只允许：

1. 先由 FW-E 冻结的最新 stable 证据生成目标 Schema、Snapshot 和 ReleaseArtifact；
2. 再使用同一 Schema／DialectCompiler 表达 2.1.220 baseline／rollback fixture，不得反向用旧版限制目标设计；
3. 版本、Header、Body、重试及 transport 事实来自画像，不新增业务代码版本分支；
4. ValidationCandidate 保存独立 immutable Release 引用，不借用 production rollback 槽位；
5. candidate 必须显式选择目标画像和内容寻址模型能力目录，连接池与 fallback 不跨画像或模型状态混用；
6. candidate 必须冻结 SupportEnvelope，范围外条件由 Planner／Compiler fail-close；
7. 构建记录 source tree、测试树、Go／Node 依赖、目标架构、image ID 与 OCI digest。

普通换版先复制并更新版本专属 generation policy，再显式传给同一个生成器：

```bash
python3 tools/generate_claude_fw_g_profile.py \
  --generation-policy tools/official_client_capture/claude_fw_g_generation_policy_<version>.json \
  ...
```

generation policy 与输入制品均须内容冻结；只有 Profile Schema、新协议或跨请求状态机制无法表达时才改
生成器控制流。生成后向 Claude Release Catalog 追加新 Release，再通过独立 candidate／Approval 更新
selector，禁止改写旧路径或在 Go 运行代码中增加版本摘要。

FW-F 已生成 target-first Snapshot／样例、2.1.220 fixture 和最终合同，其历史批准用途保持
`validation_only`。历史 FW-G 在不覆盖旧事实的前提下追加 `production_replacement` ApprovalFact，并冻结
Sonnet-only ValidationCandidate：提交 `651ccd518d97c53bb3089860a0fdf80009c1be9e`、Git tree
`71eccef8c9498de12bafaa7006108c10996cd10d`、source tree
`2792b9d29e57b66a12bc80f576e02dd06306eac467b8dda73e2dbd7a69b19d5b`、test tree
`6ef5c064a3e489579e4f471d3ad954de132e1e8260058bb1438c74d81905f3e3`、dependency lock
`bad9c6d5cd2e48d916e8c1f217f43951984be6d8cd0892ef9e22d8e43e071339`，以及固定 `linux/amd64` OCI digest
`sha256:9b923fd1a60835fa8474712764befba34a02f06e8642c5ac3af1aa9967464566`。三类树摘要分别由固定提交的
全量 tracked blob、测试路径子集和依赖清单／锁文件子集复算，不能从当前工作区临时拼接。

Candidate 冻结后的 TLS／HTTP/1.1 捕获测试与 Acceptance finalizer 测试属于追加的验收证据工具，不进入
该 Candidate 的源码树或测试树，也不改变已构建镜像。三模型实现改变了源码、画像、Wire、Release 和
镜像，因此已建立独立 ValidationCandidate `claude-code-2_1_226-fw-g-three-model-2f224831f` 并重新签发
AcceptanceFact；历史 Candidate 只保留为 Sonnet 范围的不可变验收事实。这些均是后继
official-client-only 变更的证据前序，不直接代表当前入站准入或生产状态。

## 4.4 候选验证与正式验收

Candidate 只能在 DMIT 或等价隔离环境运行，不能在 Vircs 换镜像试验。真实请求前原子冻结源码、
测试、镜像、画像、配置、代理／CA／DNS、工具和 attempt nonce；恢复或秘密扫描失败不得继续。

最低场景矩阵以已批准 SupportEnvelope 的 strict 场景为核心，并必须包含关联的边界、遗留与 managed
处置验证：

- `s1/s2/s4/a1` 的推理与 `HEAD /api/hello` 生命周期 wire；
- 主请求、续轮和一级子代理的 Header、Body、session、agent、attribution 与连接关系；
- 长历史中历史 WebSearch 成功／失败往返后的当前普通轮，以及当前 WebSearch 成功／失败续轮；工具阶段只能由最后一轮关系判定；
- TUI 标题、主轮、WebSearch 三请求链、request-id 所有权和 fallback 锁存在 Runtime 重建及应用容器重启后的恢复；
- Persona 状态存储不可用、损坏、CAS 冲突、并发租约和无效 finalization 的 fail-close／租约释放；
- 每个已登记主模型的基础、effort、thinking、TUI、Agent、background、WebSearch、count_tokens、
  fallback／锁存正例，以及未知模型、未登记别名、错误 fallbacks 和跨模型状态复用负例；
- 空工具、Agent 与 Bash 的实测工具形态；
- `persona_strict` 端点、隐私模式和目标 entrypoint，以及 `non_persona_managed` 出站的独立策略断言；
- `official-messages-oauth`、`official-count-tokens-oauth` 的三类已登记官方客户端正例；第三方
  Messages／count_tokens、Chat Completions、Responses、未登记版本／Header／metadata／System／工具
  目录必须在 OAuth 凭据读取前 fail-close；
- `retained_legacy／explicitly_retired／rerouted` 的隔离、退休或目标路由断言；
- Persona system／identity 派生记录、跨 Persona／跨 Release 拒绝，以及 compatibility 模式不会混入 strict
  的负例。

原生 TLS／ALPN、故障重试、真实 TUI、remote、custom Header／beta／metadata、Agent 层级、background、
hook、server／deferred tools、附件、count_tokens、OAuth refresh 与 MCP servers 已有本次 P／R／M 并进入
SupportEnvelope。telemetry、nonessential、usage、models、dispatch-id 与 usage-limit 的合法零流量只作
支撑事实；FW-G 不得为这些零流量生成规则，也不得把未获新证据的能力塞入 candidate。

每个场景按 `prepare → capture → seal → approve` 封存；结果必须绑定原始证据 inventory，失败 attempt
不得覆盖。只有 OfficialIngressCatalog 批准且 TranslationReport 为 lossless 的官方入口进入正向
`PAIR-<SPEC-ID>`；第三方入口只进入凭据前拒绝负例。动态值比较来源、格式、关系和生命周期。Persona
固有 system／identity 事实按已批准派生记录比较。

FW-G 已在验收前重放本次 FW-F Store；后续复核仍须先执行：

```bash
python3 -m tools.official_client_control replay \
  --store "$PWD/local-analysis/fw-f/claude-code-2.1.226/profile-approval-v5/control-store" \
  --external-root "$PWD" \
  --require-external
```

已封存的 FW-G Store 必须使用同目录的历史外部视图重放；后续文档或工具更新不得冒充 Candidate 漂移：

```bash
python3 -m tools.official_client_control replay \
  --store "$PWD/local-analysis/fw-g/claude-code-2.1.226/acceptance-v2-651ccd518/control-store" \
  --external-root "$PWD/local-analysis/fw-g/claude-code-2.1.226/acceptance-v2-651ccd518/external-replay-view" \
  --require-external
```

`claude_21220/check_coverage.py` 是 2.1.220 历史工具树完整性检查，只能在其冻结工具身份对应的 worktree
运行，或先追加工具变更审阅链；不得拿当前已经改为全 host 捕获的 MITM／提取器源码与旧摘要直接比较，
再把预期的工具演进误报成目标画像规则失败。2.1.220 fixture 的当前准入以本次内容寻址 Store 重放为准。

逐规则结果必须唯一覆盖 SupportEnvelope 内的 40 条 RequiredRules 及其 106 条画像原子断言。`ready`
只证明固定 Release Candidate 通过已批准规则，不表示画像已晋升、生产镜像已构建或生产已切换。
当前 FW-E 三层历史输入为：

- `DiscoveryInventory`：7,368 个发现项，其中目标 AST 331、2.1.88 命题 29、HitCC clue 71、Markdown
  原子 6,937；2,830 项归入语义候选，15 项映射既有规则，4,523 项登记为可重分类的
  `catalogued_context`；
- `SemanticRuleCandidate`：32 个语义候选，其中 10 个 `observed`、22 个 `blocked`，全部禁止加入
  AtomicAssertionLedger 和生产；
- 历史 `RuleLedger`：57 条 SPEC，其中 45 条 `observed`、12 条 `regressed_evidence`、0 条 `verified`，没有
  因 7,368 个发现项自动新增规则。

FW-F 清零制品在不改写上述 FW-E 事实的前提下追加：

- `DiscoveryDispositionLedger`：7,368／7,368 个发现均有唯一已解决记录；缺失、额外、重复、未决、
  空绑定、孤儿引用和循环引用均为 0；
- 4,523／4,523 个 `catalogued_context` 均有终态，其中 128 个 Markdown 导航链接由结构证据证明为
  非出站，4,395 个绑定到精确文档、标题和语义支撑事实；原始记录仍永久保留；
- 593／593 个正交候选和 32／32 个语义候选族已收敛；FW-F v1 机械生成的 97 条规则提案全部撤回并保留逐项审计；
- 历史名为 `MeasuredRuleLedger` 的 AtomicAssertionLedger 保留 110 条经 Vircs 上 2.1.226 官方客户端
  真实 P／R／M 通过的原子断言；106 条支撑 40 条 RequiredRules，4 条归入客户端本地场景；0 条响应兼容、
  遥测／traceparent 或无当前运行证据的规则进入 SupportEnvelope。

语义清零只表示每个发现已经确定“是什么、归到哪里”，不表示可以进生产。FW-F 时的 40 条规则仍为
`observed`；FW-G 通过新的官方复测证据、Candidate 对拍和隔离验收追加后继事实，才将其升级为
`verified`。不得原位改写 FW-F 台账、用遗留 wire 佐证，或为了缩小数字从发现清单删除。

历史 FW-G 已封存 `production_replacement` ProfileApprovalFact、固定 Sonnet-only ValidationCandidate、九个场景的
`prepare → capture → seal → approve` 链、40 个唯一 `PAIR-<SPEC-ID>` 和 AcceptanceFact。范围内 strict
入口、范围外 fail-close、managed／遗留隔离、TLS／HTTP/1.1、DMIT rollback／恢复及 Codex final wire
零差异均通过；完整 Store 重放为 2 个 Campaign、42 个对象、86 个事实和 9,360 个外部绑定，状态为
`ready／not_activated`。公开摘要见 [FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)。
当前三模型后继已按独立 Candidate 完成本节正负矩阵、API／Desktop 正例和回滚／恢复，新的状态为
`ready／not_activated`；历史 Sonnet Store 与收据保持不可变。

## 4.5 DMIT 候选交付、生产边界与证据归档

本任务的 FW-H 终点是 DMIT `ready_for_operator_release`，不是 Vircs production active。只有
production-replacement candidate 达到受管 `ready` 且 FW-G 退出条件满足后，才能依次执行：

1. 只读冻结 DMIT 应用镜像、compose、依赖、账号可用性、回退镜像和磁盘余量；
2. 在最终源码树重跑门禁，按“Mac 编译、ARM64 仅封装”构建固定 `linux/amd64` 镜像；
3. 只替换 DMIT 应用容器，不重建数据库、缓存、挂载、网络或其他依赖；
4. 执行三类已登记官方客户端的 Messages／count_tokens 正例，以及所有第三方／未知形态的凭据前拒绝负例；
5. 切回冻结的 DMIT 回退镜像验证，再恢复候选镜像并完成稳定观察；
6. 追加绑定源码、二进制、镜像、Catalog、Approval、Inventory、正负矩阵和回退恢复事实的
   `ready_for_operator_release` 收据。

不得连接、修改或部署 Vircs，也不得把 DMIT 收据写成 DeploymentFact。用户以后自行替换 Vircs 时，
必须另行冻结 ActiveSupportEnvelope、RollbackOperationalEnvelope、DeploymentTrafficEnvelope、真实
生产回退目标和运行依赖，并签发独立 DeploymentFact；本手册不预先声称该操作已完成。2.1.220 仍只是
baseline fixture，不能自动成为 strict rollback。

| 私有材料 | 处理 |
|---|---|
| 原始 J、未脱敏 R、原始 P | 只进权限受限的私有归档；未脱敏 R 不离开采集机 |
| 官方发行物与提取 bundle | 只保存来源和摘要，不进公开仓库 |
| token、账号和 OAuth 凭据 | 不得归档或提交 |

私有归档必须有逐文件路径、大小和 SHA-256 清单，并在另一存储位置完成复算和收据重放。只有本地
权威源码、正式发布镜像、production tree 与私有证据归档全部闭合，才能按 dry-run 清单清理远端；
生产数据库、Redis、配置、当前／回滚镜像和唯一证据副本永远不在清理范围。

当前边界：Claude 历史 Sonnet Candidate、三模型后继、历史 Vircs／DMIT 验收与 DeploymentFact 均以
追加事实保留。当前目标正向范围只有两类官方逻辑入口；第三方入口均为
`explicitly_retired + denied_before_oauth`。DMIT 只形成候选验收事实，Vircs 状态固定为
`operator_managed／unverified／not_touched`。2.1.220 Campaign／run／提取物已被 FW-F fixture 内容寻址
引用，引用有效期间不得删除。

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

当前 FW-H source／test transition、生产批准和最终聚合收据已冻结 Claude active 发送面及退休事实。
若通用 upstream overlay 尚未完整纳入 Claude／Anthropic 路径，每次上游合并仍必须人工列出
`official_egress_anthropic.go`、`official_client_profile_registry.go`、`pkg/claude/constants.go` 及其消费者
变化，并由新的 transition 或 Campaign 承接；不得把已完成 FW-H 当作后续合并的永久豁免。

## 5.2 兼容代码退休

退休顺序为：证明全部消费者和必须保留的产品语义 → 迁移真实消费者 → 让旧入口 fail-close →
验证 production active／rollback、fallback、辅助端点和负例 → 删除旧能力并生成 RemovalReceipt。不得删除仍
服务回滚、非 `claude-code` persona 或 API Key 产品语义的代码。

历史 FW-H 已退休旧 OAuth 构造器、finalizer 和旁路，该事实保持不可变。当前后继只改变 Claude OAuth
Persona 的入站准入：官方 Messages／count_tokens 为 `migrated_strict`；第三方 Messages／count_tokens、
Chat Completions 和 Responses 为 `explicitly_retired + denied_before_oauth`。Setup Token 继续进入
`non_persona_managed`，API Key 与 Service Account 产品语义保留，Codex direct 保持 `rerouted`。因此
第三方动态工具原样透传不再是待实现 P0；任意未登记工具目录直接触发准入拒绝。

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
├── SSH／候选验收 → DMIT x86_64
├── SSH／官方取证 → Vircs x86_64（唯一官方 Claude Code 主采集机；同时为用户管理的生产机）
└── 产物封装 → ARM64（接收 linux/amd64 二进制并封装 amd64 Docker 镜像）
```

HomeProxy 的绕过只说明路由器不代理该 Mac；Mac 是否直连仍由 Surge、系统代理、增强模式、CA、DNS
和具体规则决定。Mac 参与测试时必须记录这些状态及配置摘要。路径不明时，Mac 的 ClientHello、
ALPN、连接复用和 HTTP 协议不得作为官方客户端直连证据，只能作为带代理条件的功能样本。

本地 Claude Desktop、Claude Code CLI 和 Claude Code for VS Code 是三个独立官方来源产品；当前由
OfficialIngressCatalog 分别登记，再规范化到同一个 Claude Code 2.1.226 目标 Persona。它们的 UA、
Header、System 和工具目录不能互相继承，也不能为目标 Linux TLS 提供证据。Codex Desktop 不属于该
Persona。Mac 的代理状态也不会自动影响 Vircs、DMIT 或 ARM64，服务器侧代理、CA、DNS 和转发条件
必须分别冻结。

## 6.2 机器职责

| 节点 | 固定角色 | 可以执行 | 禁止或不能证明 |
|---|---|---|---|
| Mac `192.168.9.99` | 控制端、源码与前端构建端、官方 Desktop／Code／VS Code 测试端 | SSH 编排、原生构建前端、交叉编译 linux/amd64 Go 后端、官方入口验证和必要的 UI／TTY 驱动 | 默认直连 TLS 权威证据；把三个来源客户端的 wire 身份互相继承 |
| Vircs x86_64 | 唯一官方 Claude Code 主取证机、用户管理的 Sub2API 生产机 | 隔离取证和取证前后环境核对；生产替换仅由用户另行执行 | 本任务连接、修改、部署或验证其 Sub2API 服务；为抓包中断既有服务 |
| DMIT x86_64 | Sub2API 候选验收机，1 核／1.9G／20G | 固定镜像正负矩阵、应用回退／恢复、稳定观察和只读依赖核对 | 现场编译、声称生产已激活、数据库或依赖重建；官方 Claude Code 权威证据源 |
| ARM64 | linux/amd64 Docker 镜像封装端 | 接收 Mac 已编译的 linux/amd64 后端和前端产物，封装并核对 amd64 镜像 | 重新编译源码、生成官方规则证据、代替 DMIT 运行验收 |

当前已验证的构建链是“Mac 原生前端 → Mac 交叉编译 linux/amd64 Go 后端 → ARM64 仅封装 amd64
Docker 镜像 → DMIT 仅加载和运行”。每次构建仍须冻结工具、参数、产物传输路径和摘要链。

## 6.3 Vircs 官方采集纪律

Vircs 的硬约束是任何取证不得改变或中断该机由用户管理的 Sub2API 生产服务：

1. 官方二进制按版本和 SHA 独立保存，不原位覆盖唯一封存版本；
2. 使用独立用户目录、证据目录、端口和可证明的网络隔离；
3. 不修改宿主级 `hosts`、系统 CA、默认路由、iptables、全局代理或生产 compose；
4. 采集进程设置资源限制，避免争抢生产 CPU、内存、磁盘和连接；
5. 开始前后记录生产容器 digest、健康、端口、网络、挂载、依赖和错误日志摘要；
6. after 与 before 不一致时，本 attempt 失败并先恢复环境。

必须修改宿主全局状态或重启生产服务的场景不得在 Vircs 执行，应改造为隔离方案或登记为阻断。
Vircs 上官方 Claude Code 生成的材料才可作为当前 Linux x86_64 官方 wire 主证据；DMIT 上 Sub2API
生成的是实现与候选验收证据，不能反向证明官方规则；Darwin arm64 发行物只作静态交叉复核；故障注入
样本必须与自然失败分开。

## 6.4 DMIT 候选验收纪律

DMIT 不是生产权威。每次候选验收必须绑定固定的 linux/amd64 OCI digest、image ID、源码树摘要、
画像摘要、构建 ID、AcceptanceFact 和可运行回退镜像；只替换应用容器，不重建依赖服务。

当前候选运行入口是 `/root/Docker/sub2apiplus/app/docker-compose.yml` 与
`/root/Docker/sub2apiplus/app/docker-compose.claude-persona-catalog.yml` 的组合；后者固定 active 镜像
digest、Catalog Release 摘要和验收 fact 路径。临时 override、容器当前状态或裸标签都不能替代
这两个 Compose 文件及其候选验收收据。

受 1 核／1.9G／20G 限制，不在 DMIT 执行 Go／Node 编译；开跑前检查磁盘下限，避免 pcap、R 字节和
镜像层填满系统盘。故障注入和破坏性验证必须与 DMIT 的数据库、网络和账号隔离；不得把候选验收机
当作可破坏测试机。证据完成 inventory、秘密扫描和摘要后
同步到受控私有归档；同步和独立复算完成前不得删除唯一副本。

另行隔离环境允许破坏不等于可以省略 before／after、秘密扫描、恢复记录或固定 digest。

## 6.5 构建与架构边界

当前正式构建职责为 Mac 编译、ARM64 封装；无论后续是否更换节点，都必须满足：

1. 构建源、依赖锁、工具版本和参数可复算；
2. 目标产物明确为 DMIT 所需的 `linux/amd64`；
3. 多架构 manifest 的平台 digest 可分别核对；
4. candidate 与 production 镜像身份分开；
5. 构建机只证明制品同源性，不产生官方客户端规则事实。

ARM64 只封装 Mac 已交叉编译的 amd64 后端，不在该机重新编译；必须核对镜像 platform、DMIT 运行
容器架构和实际 image ID，不能用 ARM64 宿主架构推断镜像架构。

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

## 6.7 候选交付与证据清理

本任务只在 DMIT 替换应用容器并形成 `ready_for_operator_release` 收据，不执行 `compose down`、无范围
`prune`，不重建数据库、Redis、keeper、挂载或网络。Vircs 由用户自行替换；没有独立生产
DeploymentFact 时，任何文档或标签都不得声称 Vircs 已部署或验证。

原始 MITM、未脱敏 R 字节、pcap、官方 bundle 和凭据不得进入公开仓库。清理前必须证明私有归档可
解包复算、收据可重放、生产与回滚镜像仍可用、删除清单不含唯一证据或生产依赖。任一条件不满足时，
只停止空闲采集进程，不删除证据。
