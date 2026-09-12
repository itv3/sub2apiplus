# Claude Code 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 Anthropic OAuth（`authMethod=claude.ai`、
> `apiProvider=firstParty`）出站时的 Claude Code 客户端仿真
>
> **当前规则基线**：目标版本为 `claude-code 2.1.226`；`2.1.220` 仅作为同一
> Schema／Compiler 下的历史 baseline fixture。完整身份、规则和模型能力见第二、第三部分。
>
> **权威入口**：共享目标、运行架构、证据生命周期、版本演进、发布与回滚以
> [`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)
> 为准；本文是 Claude Code 规则、画像、实现、环境及专用流程增量的唯一人类可读入口，
> 机器证据和 JSON 台账只用于支撑与复算，不得形成第二套规范。
>
> **证据边界**：目标版本没有可审计的未压缩 TypeScript 源码；静态事实由官方生产 bundle
> 逆向和 P／R／M 实测证据建立，材料、规则与结论均独立取自 Claude Code，不继承 Codex CLI
> 的事实结论。
>
> **当前状态**：`official-client-only` 仍为候选，尚未完成 DMIT 在线验收，不主张已经生产激活；
> 当前可执行边界和续作检查点见 §3.6、§3.6.1。

---

# 第一部分 目标、边界与链路

## 1.1 Claude 专用目标与范围

共享仿真目标和最终 wire 等价标准见 Framework §1.1～§1.3。本文只定义 Claude Code
投影：使用 Anthropic firstParty OAuth 出站时，只有完整命中 OfficialIngressCatalog 的
官方客户端才能进入 Claude Persona；最终 wire 由选定的 ReleaseBundle 定型。准入层只验证
官方来源并规范化 Messages／count_tokens 语义，不改变 Key、Group、账号路由或计费归属，
也不能选择生产版本或画像。

| 范围 | 内容 |
|---|---|
| 直接覆盖 | OfficialIngressCatalog 登记的 Claude Code、Claude Desktop 和 Claude Code for VS Code；正向逻辑入口仅为 Messages 与 count_tokens，最终 TLS、HTTP、Header、Body、端点、连接、重试和状态均按 Claude 画像定型 |
| 条件覆盖 | 平台、entrypoint、模型能力、隐私模式、agent、工具和 fallback；只有对应 Catalog、证据与画像事实已经冻结时才进入条件分支 |
| 不覆盖 | 第三方 API／IDE／Agent、curl、Chat Completions、Responses、API Key、Bedrock、Vertex 和 Foundry；按产品边界在 OAuth 凭据读取前拒绝或转交其他 Persona |

当前规则绑定 `claude-code 2.1.226`，运行证据采用
`essential-traffic + no-telemetry` 模式。具体身份、规则与证据边界见第二部分，机器环境及
职责见第六部分。

## 1.2 版本演进与请求运行链路

```text
版本演进：官方生产 bundle 与真实 P／R／M → 客户端规则画像 → 目标 Snapshot／Release
         → ValidationCandidate → 定向验收 → 候选交付／受管生产激活

请求运行：已登记 Claude 官方客户端 → OfficialIngressCatalog 准入 → CanonicalRequest
         → Claude ReleaseBundle → 方言编译与执行 → Anthropic OAuth 上游
```

两条链路使用同一套规则、画像和内容寻址事实：版本演进链决定“什么可以交付或发布”，
请求运行链决定“什么请求可以进入 Claude Persona，以及如何最终出站”。OfficialIngressCatalog
只拥有入站准入权，不能选择 Release；未登记入口或无法无损规范化的请求必须 fail-close。

完整组件和所有权见 Framework §1.3 与本文第三部分；第二部分定义 Claude Code 官方规则，
第四部分执行版本演进，第五部分补充兼容代码退休门禁，第六部分固定环境职责。

---

# 第二部分 Claude Code 客户端规则画像

本部分是当前 Claude Persona 的唯一活动规则画像。按照 Codex CLI 第二部分的规则粒度，活动集合为
**40 条 RequiredRules**；证据层保留 **110 条原子断言**，其中 106 条完整且唯一地映射到这 40 条
画像规则，4 条只描述官方客户端本地上下文装配／本地拒绝场景，不属于 Sub2API 出站实现责任。
普通原子断言绑定 R（等长脱敏的原始请求／必要响应字节）与 M（版本、二进制、运行、连接、干预和
隐私条件），TLS 原子断言绑定原生 P 与 M。2.1.220、2.1.88 与 HitCC 只用于
历史差分、线索审计和探针设计；FW-F v1 的 154 条集合只保留为失效审计历史，不能替代 2.1.226 实测证据。

## 2.1 规则证据与准入标准

### 2.1.1 目标身份与权威证据

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

当前规则使用的权威证据如下：

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

### 2.1.2 规则准入与观测边界

当前 Campaign 已在原生 TLS attempt 中取得 ClientHello pcap，3 条 TLS 原子断言使用 P/M；普通
HTTP、Header、Body、状态与工具原子断言仍使用 R/M，不允许以 relay 元数据冒充原生 TLS 证据。

当前全部目标运行证据冻结为 `essential-traffic + no-telemetry`。`DISABLE_TELEMETRY` 经
`isAnalyticsDisabled()` 关闭 Datadog 与第一方 `event_logging`（`src/services/analytics/`）；
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 经 `isEssentialTrafficOnly()` 门控 mcp-registry、
policy_limits、grove、releaseNotes、feedback、modelCapabilities、referral 等非必要请求，
`privacyLevel.ts` 明确定义为关闭全部非必要网络流量。这些是官方配置，不是 candidate 规避取证。

bundle 中 essential gate 有 53 个调用点、非 default gate 有 7 个调用点；数字只证明静态门控面，不能
代替运行端点全集。按 Framework §1.2、§3.2，在上述同一冻结模式内，候选“零遥测／零非必要流量”
不计为仿真差异，也不生成 `traceparent`／span 或其他 RequiredRule，只能登记为 supporting-fact／
`record_only`。零流量不能用于删除发现项、Sink 或 essential 请求依赖的共享状态；场景实际触发的
essential 请求仍须逐规则核对。响应解析属于 downstream compatibility，不进入客户端请求出站画像。

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

### 2.1.3 机器复算与强制门禁

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

## 2.2 编号项分组与验收口径

本节只说明编号项如何分组、哪些进入验收分母。共 **40 条 RequiredRules**，每条继续使用“范围—规则／
机制—源码—实测—实现—状态”六字段；每条“实测”均反向绑定机器清单中的原子断言，精确 P／R／M
文件摘要、条件、分母和流偏移以 AtomicAssertionLedger 为准。

| 分组 | 条数 | 当前验证状态 | 当前 SupportEnvelope 必验项 |
|---|---:|---|---:|
| TLS（`SPEC-TLS-*`） | 2 | ✅ 2 | 2 |
| 协议与连接（`SPEC-PROTO-*`、`SPEC-CONN-*`） | 6 | ✅ 6 | 6 |
| Header 与身份（`SPEC-HDR-*`） | 7 | ✅ 7 | 7 |
| Body、缓存与状态（`SPEC-BODY-*`、`SPEC-CACHE-*`、`SPEC-STATE-*`） | 11 | ✅ 11 | 11 |
| 工具（`SPEC-TOOL-*`） | 5 | ✅ 5 | 5 |
| 端点与辅助链（`SPEC-EP-*`） | 9 | ✅ 9 | 9 |
| **合计** | **40** | **✅ 40** | **40** |

```text
当前验收：40 条 RequiredRules 全部进入 SupportEnvelope 逐规则验收分母
条件规则：仍计入上述 40 条；验收时必须同时验证适用条件、正例与不适用分支
范围外项：客户端本地场景证据不属于 Sub2API 出站责任，不进入 RequiredRules
```

规则数与证据数采用不同计数口径：

| 原子断言用途 | 条数 | 与规则验收分母的关系 |
|---|---:|---|
| 画像规则证据 | 106 | 完整且唯一地支撑 40 条 RequiredRules；不按原子断言重复计数 |
| 客户端本地场景证据 | 4 | 属于 2 个 scenario-only 场景组，不进入规则验收分母 |
| **合计** | **110** | **证据断言总数，不得解释为 110 条画像规则** |

40 条 RequiredRules 及其 106 条画像证据的唯一映射见
[`claude_required_rules_2_1_226.json`](../tools/official_client_capture/claude_required_rules_2_1_226.json)；
110 条原子断言的证据明细见 §2.1.1 所列 AtomicAssertionLedger。

### 2.2.1 messages 推理核心

基础 `sdk-cli` 请求行为固定为：

- 请求行：`POST /v1/messages?beta=true HTTP/1.1`，Host 为 `api.anthropic.com`；
- 基础 Header 按实测大小写和顺序发送；条件 Header、自定义 Header、gzip、TUI、retry 和 fallback
  分别按 §2.4～§2.8 的对应规则插入或变化；
- 基础 UA 为 `claude-cli/2.1.226 (external, sdk-cli)`；真实 TUI 使用 `external, cli`，获准条件段按
  agent-sdk、client-app、workload 的实测顺序追加；
- Stainless 基础向量为 `x64/js/Linux/0.94.0/retry 0/node/v26.3.0/timeout 600`；
- Sonnet 基础 Body 顶层顺序为 `model/messages/system/tools/metadata/max_tokens/thinking/`
  `context_management/output_config/stream`；Opus／Fable 仅在实测场景插入 `fallbacks`。Fable 请求返回
  画像声明的 `claude-opus-5` 时不锁存；只有实际 `claude-opus-4-8` server fallback 才按实测条件在后续
  请求插入成对锁存 Header；所有模型的具体顺序均从能力目录场景生成；
- request-id、Session-Id、agent lineage、resume／fork、retry、non-stream 与模型 fallback 均按实测
  生命周期生成，不能用入站自报客户端身份补造。

### 2.2.2 八类 strict 端点

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

<!-- FW-F-ACTIVE-RULES-BEGIN -->

## 2.3 TLS

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

## 2.4 协议与连接

### SPEC-PROTO-001 HTTP/1.1 与端点 ALPN

- **范围**：hello、messages、policy-limits 与 settings；Linux/amd64 原生 TLS。
- **规则／机制**：应用层使用 HTTP/1.1；hello/messages ClientHello offer http/1.1，policy-limits/settings 的实测分支省略 ALPN。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 P/R/M 断言。
- **实测**：1 条 R/M 与 1 条 P/M 原子断言通过；12 条应用层请求及同一原生 TLS attempt 的四端点对照闭合。
- **实现**：TransportProfile 按 egress 选择 ALPN 行为并只建立 HTTP/1.1 执行能力。
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

## 2.5 Header 与身份

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

## 2.6 Body、缓存与状态

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

### SPEC-STATE-009 background 请求身份与形态

- **范围**：messages-inference；官方 background 会话。
- **规则／机制**：background 使用 x-app=cli-bg、cc_entrypoint=cli，并按已实测 Haiku／Sonnet 后台请求形态输出；前台保持 cli。
- **源码**：未取得可审计原源码；官方 2.1.226 二进制与 bundle 只作定位，规则权威来自映射的 R/M 断言。
- **实测**：1 条 R/M 原子断言通过；background 正例与 sdk-cli 前台条件对照确认身份及 Body 形态。
- **实现**：只接受可信 background 状态事实；第三方入站不得通过 x-app 触发。
- **状态**：verified；FW-G 独立复测、候选对拍与隔离验收通过。

## 2.7 工具

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

## 2.8 端点与辅助链

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

<!-- FW-F-ACTIVE-RULES-END -->

## 2.9 发现项、候选与历史材料的终态

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

## 2.10 后续版本的更新方法

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

# 第三部分 Claude 画像、方言与 Sub2API 实现

本部分只记录共享合同在 Claude Persona 中的实现投影、Claude 方言和当前迁移事实；共享运行架构、
Release 身份及共享层变更分别以 Framework §2、§3.1 和 §5.4 为准。

## 3.1 共享架构落地映射与 Persona 边界

共享执行链、代码依赖和 Guard 合同以 Framework §2 为唯一权威。本 Persona 的落地映射为：

| 共享层 | Claude 实现投影 | Claude 专属责任 |
|---|---|---|
| 入站准入与适配 | `OfficialIngressCatalog`、`IngressProtocolAdapter` | 只接受内容摘要匹配的 Claude 官方客户端并生成 TranslationReport |
| Persona 规划 | Claude PersonaPlanner、`ClaudeIdentityFacts`、`ClaudeEgressPlan` | 生成 Claude 身份、agent、会话、条件和端点计划 |
| Release 控制 | Claude Release Catalog、active `ReleaseArtifact／ReleaseBundle` | 解析 Claude production active／rollback，不接受入站版本选择 |
| 方言编译 | Claude `DialectCompiler`、`CompiledEnvelope` | 定型 Claude URL、Header、Body、beta、状态、重试和 transport |
| 执行与保护 | Claude Executor authority、FinalizationToken、受信 adapter、Runtime Guard | 使用独立 issuer／状态执行最终请求并阻止跨 Persona 旁路 |

Key、Group、账号路由与计费沿用 Framework §1.3 的业务所有权。只有 `TranslationReport=lossless` 且
请求位于本 Release 的 `SupportEnvelope` 内，才能进入 strict Compiler；范围外请求必须 fail-close。

**第三方客户端与工具目录。** Claude Persona 采用 `official-client-only` 准入策略，只接受
`OfficialIngressCatalog` 已登记且内容摘要匹配的 Claude 官方客户端。KiloCode／zlfcode 等第三方
客户端及其工具目录必须在读取 OAuth 凭据前拒绝；Anthropic API 支持自定义工具，不代表该入口或工具
属于 Claude Code 官方画像。

本 Persona 不定义第三方工具映射或 MCP bridge，也不将第三方入口纳入 `SupportEnvelope`／
RequiredRules。若未来需要改为 `canonical-semantic`，必须新建独立 Campaign，重新批准
IngressPolicy／SupportEnvelope，并完成官方取证、正负 PAIR 和最终 wire 对拍后，才能实现或激活。
Claude Desktop 与 VS Code 同样必须分别命中各自实测的 Catalog 条目，不能自动继承 Claude Code wire。

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

Release 的内容寻址、正交事实和 selector 规则以 Framework §3.1 为准。Claude Catalog 逐个绑定
Profile／Wire 路径与原文字节摘要、Release／Bundle 摘要、规则／断言／端点数量和模型闭集；validation
candidate 使用独立不可变引用，production selector 只保存 active／rollback。active 必须可加载且带
Approval；当前 rollback 是显式 `operational-deployment`，只记录提交、镜像 digest 和已演练收据，
不能伪造第二个 Release。

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

Claude 首次激活前必须提供可验证的真实回退目标，可以是上一正式 Release 或冻结的遗留实现；不得复制
active 摘要伪造回滚对。新增版本只追加快照与发布图节点。版本泄漏 baseline 只登记既有债务，不能通过
新增版本常量换取门禁通过；业务代码不得按版本维护第二套选择事实。

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

`CompiledEnvelope` 的公共闭集以 Framework §2.3 为准。Claude beta、HeaderSlots、BodyShape、agent
层级、Stainless 和重试语义只能留在 Claude 画像与 Compiler 中，不得进入共享层或填充 Codex 专属字段。

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

## 3.5 Claude 方言隔离、Guard 与策略

共享包依赖、共享／私有组件边界和 Guard 状态机见 Framework §2.5。Claude 只向闭集 wiring 注册自己的
Plan、IdentityFacts、ProfileSchema、DialectCompiler 和 adapter；不得读取或填充 Codex 方言字段。
`legacy_observe` 在 Claude 轨道中只允许 FW-E 对已冻结遗留路径报告事实，不能作为长期
`out_of_scope_passthrough` 或 strict 验收通过；版本、UA 和 Stainless 指纹只能存在于画像及加载器。

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
该显示差异：若它影响已冻结 Candidate 身份，返回 VC-4 建立新 Candidate；只有证据证明它不参与身份、
路由或行为判定时，才可在收据中登记为非身份显示字段。VC-5／VC-6 不得为修正角标重新构建候选镜像。

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
5. 在 DMIT 切回已冻结回退镜像验证，再恢复上述 OCI digest；随后只执行 VC-6 交接清单预先冻结的交付
   smoke，完成稳定观察、容器／依赖身份、日志、磁盘、网络和 3x-ui 核对，不重跑完整 VC-5 规则矩阵；
6. 清理本轮临时传输包和构建临时目录，但不执行 Docker prune、不删除回退镜像或验收日志；随后追加
   `candidate_delivery_recorded` 四阶段事实，封存交付包与私有归档清单，再用 §4.6.4 的 finalizer 签发并
   独立重放 `ready_for_operator_release` 收据；缺少任一现场事实时保持当前未就绪状态，不得手写收据。

---

# 第四部分 Claude Code 版本演进流程

Framework §5.3 是升级总操作入口并规定 `VC-0～VC-6` 顺序；本部分是 Claude 轨道的参数、证据和门禁
权威，只补充没有目标版本官方源码、依赖生产 bundle 逆向、隐私模式、OfficialIngressCatalog、模型能力
目录及专用机器职责的取证边界。历史 FW 阶段和工具状态只记录已发生的迁移事实，不得重定义 Framework
的通用状态语义。

## VC-0～VC-6 执行导航

下表只负责导航和展示阶段交接链，不重复建立另一套执行规范。每个锚点直接落到唯一详细章节；FW-E～FW-H
和具体版本状态仅为历史事实，不能代替这些阶段入口。

| 阶段 | 步骤 | 核心封存输出 | checkpoint／终态 | 唯一详细章节 |
|---|---|---|---|---|
| VC-0 | 冻结升级输入 | P0 收据、Campaign 总计划和首批动作清单 | 允许创建 Formal Campaign | [§4.0](#claude-vc-0) |
| VC-1 | 收集目标证据 | EvidencePackage、CaptureIndex、SinkInventory 和 DiscoveryInventory | `evidence_recorded` | [§4.1](#claude-vc-1) |
| VC-2 | 逐规则判定差异 | RuleMigrationLedger、AtomicAssertionLedger 和 `rule_classification_recorded` | 分类事实封存；checkpoint 仍为 `evidence_recorded` | [§4.2](#claude-vc-2) |
| VC-3 | 生成目标画像 | EvidenceApprovalFact、ProfileApprovalFact 和内容寻址画像 | `official_sealed → profile_approved` | [§4.3](#claude-vc-3) |
| VC-4 | 实现固定 Candidate | CandidateBuildReceipt 与 `candidate_frozen` | `candidate_sealed` | [§4.4](#claude-vc-4) |
| VC-5 | 定向验证 | CandidateEvidencePackage、逐规则结果与 AcceptanceFact | `validation_only／ready`；production state 为 `not_activated` | [§4.5](#claude-vc-5) |
| VC-6 | 交付或生产激活 | 候选交付收据；获授权时另有 DeploymentFact | `ready_for_operator_release／production_active_upgraded` | [§4.6](#claude-vc-6) |

主交接链固定为：

```text
P0 收据 → EvidencePackage → RuleMigrationLedger／AtomicAssertionLedger
        → rule_classification_recorded → EvidenceApprovalFact／ProfileApprovalFact
        → CandidateBuildReceipt／candidate_frozen → AcceptanceFact
        → 候选交付收据／DeploymentFact
```

## 第四部分公共执行约定（非独立阶段）

以下约定同时适用于 VC-0～VC-6，不产生独立阶段状态。各详细章节只补充 Claude 专用输入、动作和门禁；
相同规则冲突时以 Framework 为准，不能用历史 FW 工具或当前版本实例建立第二套恢复语义。

### Claude 阶段依赖、执行闭集与恢复

每个阶段只能消费前序阶段已经封存的输出。Campaign 总计划在 VC-0 冻结阶段依赖、预算和最终用途；每个
阶段或恢复批次再从最近合法 checkpoint 编译不可变动作清单，分别列出 `execute_items`、`reuse_items`、
逐项输入、环境身份和直接依赖。未来才产生的 Approval、Candidate、attempt、镜像或收据 ID／摘要不得预填，
批次启动后也不得补写。

Claude 的逐项结果键固定为：

```text
result_key = SHA256(canonical_json({
  item_id,
  input_sha256,
  environment_sha256,
  direct_dependency_sha256
}))
```

逐文件依赖必须归入 `producer／evaluator／control／scenario／runtime／environment／network／gate` 之一，并
形成无环下游图。未知文件或未登记依赖在执行前按数据面变化失败关闭，不能退化为无依据全量重跑。执行集合
为空时，必须在启动运行时、环境探针、深度扫描或 live 请求之前生成 `incremental-noop` 证明，记录
`scanned_bytes=0`、`live_request_count=0` 后立即结束。

恢复统一从最近合法 checkpoint 重新计算：

| 变化或失败 | 身份处理与唯一恢复入口 |
|---|---|
| 身份与依赖不变的临时失败 | 保留 Campaign、Candidate 和 attempt，只执行 `failed／pending` |
| 目标、基线、用途、账号权限、官方产物或证据语义变化 | 建立新 Campaign，从 VC-0 重新冻结并在 VC-1 补齐必要事实 |
| 规则分类或目标画像变化 | 建立后继 Campaign，分别返回 VC-2 或 VC-3；仍有效官方证据只读复用 |
| Candidate 源码、测试、依赖、画像、构建或镜像变化 | 保留 Campaign，建立新 Candidate，从 VC-4 执行受影响闭集 |
| 仅控制面或 evaluator 变化 | 不改变数据面身份，只重跑受影响离线门禁 |
| 环境或网络语义变化 | 返回 VC-0 复核环境，再按直接依赖决定下游失效范围 |

任何恢复都不得覆盖历史事实、改名或复制 Campaign 来绕过失败、重发已可信封存的官方请求，或以新身份重置
时间预算。同一根因连续失败两次必须停线；现有工具若不能计算结果键、执行／复用闭集、no-op 和恢复计划，
属于 P0 工具阻断。

### Claude 受管入口、连续监督与文档依赖

`python3 -m tools.official_client_control` 是对象、事实、状态和既有收据的低级只写追加／只读重放入口，不是
正式 Campaign 的父监督器。阶段 producer／finalizer 只能生成动作清单声明的 payload，再由受管入口校验和
追加；操作员不得手写事实、绕过前序门禁或直接把历史聚合脚本当作后继版本入口。

用途、权限和状态是四个正交维度：

| 维度 | 取值与边界 |
|---|---|
| `campaign_purpose` | `validation_only／production_replacement`；VC-0 冻结后不得改写 |
| 批准与执行权限 | ProfileApprovalFact／SupportEnvelope 决定批准范围；P0 单独冻结主机与动作权限，候选交付权限不等于生产激活权限 |
| workflow checkpoint | `campaign_created → discovered → evidence_recorded → official_sealed → profile_approved → candidate_sealed → validation_only／ready`；生产分支再进入 `accepted_not_activated → active → restored_active` |
| production／delivery state | `not_activated／production_unverified／verified_active` 与 `not_ready_for_operator_release／ready_for_operator_release` 分开计算 |

因此 `ready／not_activated` 表示 checkpoint 与 production state 的组合，不是新枚举；
`ready_for_operator_release` 也不能替代 `production_active_upgraded`。

正式批次的状态机固定为 `dispatching → executing → evaluating → terminal`。P0 必须冻结父监督器、动作超时、
worker 心跳间隔、失联阈值、批次派发阈值、阶段交接窗口和不可后移的总 deadline；具体数值写入本次批准的
Campaign 计划。监督器连续落盘动作开始／结束／失败、等待原因、execute／reuse、live 请求量、扫描量和
`planning／active／waiting` 时间区间。进程重启、新 attempt、Candidate 或恢复执行均不得重置时间账本；
worker 失联、动作超时、派发超时、无法分类的时间区间或需要人工补派时立即停线。

现有 `claude_fw_e_complete_campaign.py`、`claude_fw_f_complete_runner.py` 及 FW-G 聚合 finalizer 只解释各自
冻结的历史实例，不能证明已具备上述通用父监督能力。后继 Formal Campaign 若没有经 P0 演练的受管 runner、
状态持久化、超时／失联处理和 checkpoint 格式，必须登记工具阻断，不能依靠人工连续执行脚本。

受管工具运行时读取的 Framework、本指南、policy 或 Schema 都是直接依赖。部署清单必须登记规范相对路径、
内容摘要及“不存在”基线，与工具树在同一可回滚事务中切换，并从实际运行根完成重放；只在完整开发工作树
通过不代表部署完整。当前 `official-client-control-campaign/v1` 尚未直接冻结 `campaign_purpose`、执行权限、
阶段依赖、预算和 deadline；后继 Campaign 必须先把这些字段纳入所有阶段共同引用的不可变 P0 计划，或升级
Campaign Schema 和门禁。仅写在手册、环境变量或操作记录中均属于 P0 工具阻断。

<a id="claude-vc-0"></a>
## 4.0 VC-0 冻结升级输入

- **输入**：目标版本与官方发行通道、当前 Active／Rollback 或明确的用户管理边界、隐私模式、entrypoint、
  平台角色、账号与模型条件、Campaign 用途、批准范围、执行权限、预算、证据复用计划和回退点。
- **操作与工具**：只读冻结 npm／二进制／bundle 身份，核验 Vircs 官方取证、DMIT candidate、Mac 构建控制
  和 ARM64 镜像封装边界，演练受管执行、恢复与文档部署后生成 P0 记录。
- **产物**：P0 收据、Campaign 总计划、目标与基线身份、环境／工具／文档摘要、证据复用计划、阶段依赖、
  监督参数、时间账本和 VC-1 首批动作清单。
- **完成标志**：身份、用途、批准范围与执行权限完整，工具阻断为零、live 请求为零、预算与回退点有效、
  取证及候选环境可用，且用户管理的 Vircs 生产服务未被修改。
- **失败恢复**：正式 Campaign 前停线；工具或环境缺口拆成独立变更，修复并冻结新摘要后只重跑 VC-0。

VC-0 只回答“本次升级是否具备安全开工条件”。本阶段可以查询官方发行元数据和执行只读环境检查，
但不得运行目标客户端产生 live 证据，不得定义目标 ProfileSchema／Snapshot，不得创建 Candidate，也不得
修改 Runtime Selector 或生产服务；这些工作分别从 VC-1、VC-3、VC-4 和 VC-6 开始。

### 4.0.1 P0 冻结清单与执行边界

通用冻结、身份恢复、环境安全和时间控制分别以 Framework §5.3.1、§5.3.4、§5.1.4～§5.1.5 和
§5.3.5 为准。本节只列 Claude Campaign 必须在 P0 中落盘的专用输入：

| 冻结面 | Claude P0 必须记录 |
|---|---|
| 目标与基线 | 目标版本／发行通道、npm URL 与 integrity、tgz／二进制／bundle SHA、内嵌 Bun／SDK、平台与架构；当前 Active／Rollback 的 Release、Profile、镜像和回退事实，或明确的 `operator_managed／unverified／not_touched` 边界 |
| Campaign 身份 | Campaign ID、`validation_only／production_replacement` 用途与终点、批准范围、生产执行权限、证据根、目标场景、entrypoint、隐私模式和受管工具版本 |
| 账号与模型 | OAuth 组织与非秘密账号标识、权限／scope、模型能力目录、精确别名、effort、feature、故障条件和场景矩阵；OAuth secret 不进入 P0 收据 |
| 执行环境 | §6 的四类机器角色、OS／内核／架构、网络／代理／CA／DNS、端口、目录、容器、资源水位、构建链和生产隔离边界 |
| 证据策略 | baseline 已封存证据及摘要、允许只读复用的事实、目标版本仍需补齐的事实、定向取证清单和禁止复用项 |
| 控制策略 | Campaign 总 deadline、阶段预算、同根因重试上限、资源水位、阶段交接窗口、回退点、结果键、直接依赖图、首批 `execute／reuse` 范围及 no-op 语义 |
| 工具身份 | stable 冻结、bundle 提取、静态分析、采集、relay、脱敏、finalizer、环境快照、受管 runner、状态持久化、Schema、受管文档、源码树和测试树摘要；目标 generation policy 在 VC-3 生成，VC-0 不预填 |

FW-E 首先使用 `claude_fw_e.py freeze` 查询并冻结官方 `stable`；输出必须写入新的冻结目录，不能覆盖
旧版本材料。2.1.220 等历史版本只能提供 baseline 和证据复用线索，不能选择目标版本。最低离线预演
应证明 bundle 提取、发现、采集编排、环境快照、脱敏、finalizer 和收据链能处理冻结夹具，并执行：

```bash
make test-capture-tools
make check-egress-spec
```

预演只证明工具能力，不生成目标版本证据。测试跳过必须登记为 `approved_skip／unexpected_skip`；正式
P0 要求 `unexpected_skip=0`。工具、场景或证据标签残留旧版本硬编码，或必须依赖临时安装、修改宿主网络
和生产配置才能运行时，均视为工具阻断。最小历史夹具还必须证明父监督、结果键、执行／复用闭集、
`incremental-noop`、时间账本、超时／失联停线、checkpoint 恢复及受管文档部署；任一能力只能靠人工保证时，
不得创建 Formal Campaign。

### 4.0.2 Claude 专用身份、用途与环境角色

Campaign、Candidate 和 attempt 的身份边界见 Framework §3.3、§5.1。Claude P0 额外冻结：

| 身份维度 | 冻结要求 |
|---|---|
| 官方产物 | npm URL、integrity、tgz／二进制／bundle SHA、内嵌 Bun／SDK、平台和架构必须联合判定，任一变化均为新目标身份 |
| 隐私模式 | `essential-traffic`／`no-telemetry`／`default` 不得混用；复用证据必须与目标模式一致 |
| entrypoint | `sdk-cli` 与真实 TTY 的 `cli` 分开取证；`-p` 不能伪造 TUI，Desktop 与 VS Code 也不能继承 CLI 身份 |
| 平台 | Linux x86_64 是当前主基准；Darwin arm64 只作交叉复核，不替代目标平台 wire |
| 账号与模型 | OAuth 组织、权限、scope、模型目录、精确别名、effort 和可见性共同构成证据条件，不能在验收后追认 |
| 环境角色 | Vircs 只承担隔离官方取证且不得扰动其生产服务；DMIT 只验收固定 Candidate；Mac 负责控制与构建；ARM64 只封装 linux/amd64 镜像，详细边界见 §6 |

每个 Campaign 必须在 P0 选择一个用途，进入正式 Campaign 后不得更换：

| 用途 | VC-6 终点 |
|---|---|
| `validation_only` | 交付已验收候选并停在 `ready_for_operator_release`；不改变或声称验证用户管理的生产环境 |
| `production_replacement` | 先达到 `ready_for_operator_release`；具备明确生产权限、可复算 Active／Rollback 和有效回退点后，继续 canary、切流、实际回滚与目标恢复 |

Campaign 用途描述最终目标，执行权限描述本次操作可以推进到哪里，两者不得互相替代。当前 2.1.226
official-client-only Approval 已冻结为 `production_replacement`，而当前任务的权限只覆盖 DMIT 候选交付；
因此可以推进到 `ready_for_operator_release` 后交给用户，但不得改写用途、进入 Vircs 或声明 VC-6 的生产
分支完成。后续生产操作必须取得独立授权并从 VC-0 重新冻结生产输入；新 Campaign 若本来就不以生产替换为
目标，才选择 `validation_only`。

Discovery、Evidence、Approval、Candidate、Acceptance 和 Deployment 均是后续阶段的追加事实，VC-0 不得
预填其 ID、摘要或状态。2.1.226 的当前进度与恢复检查点只见 §3.6，不写入新版本 VC-0 冻结清单。

### 4.0.3 退出条件与阻断处置

VC-0 的退出条件固定为：

```text
P0 收据通过
∧ 目标、基线、Campaign 用途、批准范围、执行权限、账号与模型条件完整
∧ 工具阻断 = 0
∧ unexpected_skip = 0
∧ live_request_count = 0
∧ 受管 runner、阶段依赖、监督参数、时间账本和文档部署预演通过
∧ 环境、目录、预算、资源水位和回退点有效
∧ Vircs 生产状态未变化
⇒ 封存 VC-0 输出并进入 VC-1
```

目标、基线、用途、账号权限、模型可见性或官方产物身份变化时，当前冻结结果失效，必须从 VC-0 建立
新的 Campaign 身份。工具、Schema、场景、网络、目录、资源、预算或回退能力存在缺口时，不得启动正式
Campaign；先记录最后合法检查点、根因和唯一下一动作，再独立修复并重跑受影响的 P0 检查。全部条件通过
后应立即封存 P0 收据、Campaign 总计划和 VC-1 首批输入，不得以准备工作为由停留在 VC-0。

<a id="claude-vc-1"></a>
## 4.1 VC-1 收集目标证据

- **输入**：VC-0 收据与正式 Campaign 身份、目标生产 bundle、证据复用计划、历史稳定来源、目标场景
  清单及模型能力轨道。
- **操作与工具**：先判定复用或补证范围，再验证 integrity、确定性提取 Bun SEA、全量分析网络 sink，
  并为缺失目标事实采集 P／R／J／M 和全 host／path 出站。
- **产物**：目标身份与 P／R／J／M、CaptureIndex、SinkInventory、DiscoveryInventory、跨来源矩阵、
  EvidencePackage 和 `evidence_recorded` checkpoint。
- **完成标志**：目标身份完整，目标发现无截断，证据逐项可定位、可解析、可复算，安全与环境恢复门禁
  全部通过，官方证据包已封存。
- **失败恢复**：只为缺失目标事实及其直接依赖场景定向补证；已可信封存的官方请求不得重发，身份变化
  返回 VC-0。

VC-1 只回答“目标版本实际会产生什么行为”。本阶段不判定相对基线的最终迁移类型，不生成目标画像，
也不签发 ProfileApprovalFact；`inherit/change/condition_change/add/delete`、最终发现处置和
AtomicAssertionLedger 均在 VC-2 完成。

### 4.1.1 证据来源与复用判定

先按 VC-0 冻结的证据策略逐项判定，不能把“已有文件”直接解释为“可以复用”：

| 情况 | VC-1 动作 |
|---|---|
| 同版本、同官方产物／平台／entrypoint／隐私模式／账号权限／模型可见性，且证据通道与语义均未变化 | 只读引用已封存证据及其摘要，不复制、不覆盖，也不重发官方请求 |
| 可信证据只缺少某项目标事实 | 只对该事实、触发条件和直接依赖场景定向取证，其余证据继续只读复用 |
| 目标、基线、Campaign 用途、官方产物、平台、entrypoint、隐私模式、账号权限或模型可见性变化 | 停止当前 Campaign，返回 VC-0 重新冻结身份和证据策略 |
| 仅报告、控制面、evaluator 或 Candidate 变化，目标证据语义未变 | 只重跑受影响的离线派生与门禁，不得据此重发官方请求 |
| 采集、relay、脱敏或其他产出侧工具改变证据字节或语义 | 冻结新工具身份，建立后继 Campaign，并只重采受影响的证据闭集 |

纯复用批次必须记录 `live_request_count=0`；工具、报告或 Candidate 变化本身不是官方重采理由。目标事实
仍取下列五类输入的并集，但只有目标原生发现和同身份目标运行证据能够成为当前版本权威：

| 输入 | 每版动作 | 权威边界 |
|---|---|---|
| 目标原生发现 | 全量 AST／词法发现并与全 host／path 运行 inventory 对照 | 目标版本的静态与运行权威 |
| 前一批准 stable | 比较规则、条件、sink 和最小运行哨兵 | 只提供迁移候选和复用来源 |
| 2.1.220 官方材料 | 重放历史规则与 baseline fixture | 只作差分和探针设计 |
| 2.1.88 真源码 | 重放已冻结的 102 个源码机制候选 | 只作机制线索 |
| HitCC 2.1.197 | 重放已冻结的 71 个线索 | 只作发送面和分支线索 |

首次历史基线必须绑定原始目录、工具、覆盖合同、稳定 ID、原文位置和收据。2.1.88 全量覆盖
`src／node_modules／vendor` 的网络反向切片；HitCC 每篇 `clue_source` 和未抽成 clue 的 Markdown 项均须
无截断登记，不能按关键词未命中排除。后继 Campaign 必须重放全部稳定 ID；可以复用提取结果，不能复用
针对新目标的迁移结论，旧资料也不能单独支持任何活动规则。

仅在原始摘要／引用无法复算、提取能力或覆盖合同实质变化、目标出现无法解释的新网络机制、目标证据与
历史候选冲突／闭集缺口，或候选粒度不足以唯一映射时，才全量重审原始资料。重审必须建立新的内容寻址
基线和追加式收据，记录触发原因、摘要、ID 迁移和覆盖差异；旧基线不得覆盖。

### 4.1.2 Bundle 分析与官方取证

Claude 没有可审计的目标版本未压缩源码，必须以官方生产 bundle 和目标运行证据为主：

1. 核对 VC-0 冻结的 npm 来源、integrity、tgz／二进制／bundle 摘要和平台，确定性提取 Bun SEA；
2. 解析目标 bundle 的全部文本模块，从官方入口独立枚举网络 sink、请求构造、Header／Body 写入、环境与
   feature gate、端点、重试和跨请求状态，不得只围绕旧规则或固定字面量搜索；
3. 将 AST 调用点、无截断词法候选、历史稳定 ID 和运行待证事实合并，冻结目标场景、条件、模型轨道和
   证据通道；场景清单冻结后不得现场补写；
4. 在 Vircs 隔离环境按冻结清单运行官方客户端，全 host／path 观察进程出站，不得用待证 endpoint、host
   或历史规则预筛；
5. 每个 run 同时封存 P／R／J／M、实际 argv／环境、工具摘要、宿主回执、连接索引、秘密扫描、目录权限和
   before／after 恢复事实；
6. 生成 CaptureIndex，将静态发现、触发场景、连接、host／path、P／R／J／M 和环境身份双向绑定，再执行
   跨来源闭合与 EvidencePackage 封存。

`SinkInventory` 至少覆盖 `fetch`、Anthropic SDK resource、Node／Bun HTTP、TLS、socket、
WebSocket／EventSource、动态 wrapper 和外部网络子进程；禁止数量上限或抽样。无 sourcemap 的 minify
Bun SEA 是目标静态权威；词法窗口、附近 sink 和 minify 符号只用于定位，不能代替 AST 调用关系、
source-to-sink、反向数据流和运行闭环。JavaScript 无法证明的 Bun／原生模块、动态调用或宿主 transport
必须登记证据边界，并由进程级或网络级证据补足。

若目标版本拟登记多个主模型，基础轨道先证明公共行为；随后为每个模型分别覆盖目标能力目录实际声明的
全部 effort 值、thinking 开关及适用的 TUI、Agent、background／subagent、WebSearch、count_tokens、
官方 fallback 和续轮状态。每个场景保存原始请求、必要响应、manifest、runtime receipt 和生产零差异
证明。只观察到模型字符串相同不足以继承；缺少目标场景或差异证据的模型不得加入 active 能力目录。

取证必须使用 P0 冻结的隐私模式。当前 `essential-traffic + no-telemetry` 模式同时设置
`DISABLE_TELEMETRY=1` 与 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`；telemetry／nonessential 的合法
零流量只登记为 supporting fact，不能证明相关代码不存在，也不能用于删除影响 essential 请求的共享状态。

### 4.1.3 目标闭集、封存与退出

VC-1 的跨来源产物只保存事实、来源和待审查候选，不形成最终规则结论：

| 层 | 内容与阶段边界 |
|---|---|
| `SinkInventory` | 无截断保存目标发送点、transport、host／path 和触发条件，并与运行 inventory 对照 |
| `DiscoveryInventory` | 保存目标调用、历史原子命题、clue 和 Markdown 原子；发现项不能直接生成规则 |
| `SemanticRuleCandidate` | 以 `source_ids` 多对一归并稳定 `CAND-*`，只作为 VC-2 输入；`observed／blocked` 均不是生产规则 |
| 跨来源矩阵 | 绑定目标发现、历史来源、CaptureIndex 和证据缺口；只证明来源覆盖，不签发迁移结论 |
| `EvidencePackage v3` | 只保存有序 `evidence_items`；每项绑定稳定 `evidence_id`、`source_ids`、外部证据摘要和适用条件，不含规则、证据等级或迁移结论 |

一个发现可以关联多个语义候选，但来源引用必须双向闭合。`claude_fw_e_seal_plan.py` 为兼容 2.1.226
历史输入仍读取 `claude_fw_e.py rule-assessments`，但 v4 seal plan 只提取 `source_ids`、证据路径和适用条件，
并生成不含规则结论的 `evidence_items`。历史输入中的 `migration_decision`、证据等级、生命周期和兼容类别
不得进入 EvidencePackage v3，也不得自动写入 RuleMigrationLedger、AtomicAssertionLedger、Profile 或
生产清单。

机器执行顺序固定为：

```text
claude_fw_e.py analyze-bundles
→ claude_sink_containment.mjs
→ 冻结目标场景并执行官方 Campaign／relay 取证
→ claude_fw_e.py capture-index
→ claude_fw_e_dispositions.py
→ claude_fw_e_validation_closure.py
→ claude_fw_e_crosswalk.py --require-explicit
→ claude_fw_e_crosswalk.py --require-closed
→ claude_fw_e.py rule-assessments（仅历史 seal 兼容预评估）
→ claude_fw_e_seal_plan.py
→ claude_fw_e.py seal
```

`analyze-bundles` 对每个平台运行锁定 TypeScript 解析器，并把 AST 调用点与无截断词法候选合并为
`target-sink-inventory.json`。`--require-closed` 中的 `unclassified=0` 只表示来源覆盖和证据缺口已闭合，
不等于已经完成 VC-2 的规则迁移分类。`seal` 必须绑定目标 inventory、矩阵、closure、CaptureIndex 和
工具摘要，并封存 `official-client-evidence-package/v3`；该包只有 `evidence_items`，不得包含 `rules` 或
`migration_decision`，也不得签发 Evidence／Profile ApprovalFact。

VC-1 的退出条件固定为：

```text
目标身份与 VC-0 完全一致
∧ SinkInventory、DiscoveryInventory 和跨来源引用无截断、无缺失
∧ CaptureIndex 对 P／R／J／M、场景、连接和环境身份闭合
∧ 运行未出现 inventory 外 host／path／sink
∧ 隐私条件、目录权限、秘密扫描、before／after 恢复和 finalizer 全部通过
∧ Store checkpoint = evidence_recorded
⇒ 进入 VC-2
```

VC-2 若发现某项迁移判断仍缺目标事实，只返回 VC-1 补采该项及其直接依赖；其他已封存 Job、原始请求和
证据继续只读复用。临时网络失败只建立新 attempt；目标身份或产出侧证据语义变化时建立后继 Campaign；
仅控制面、报告或 evaluator 工具变化时只重跑受影响的离线门禁，三种情况均不得覆盖历史事实。

<a id="claude-vc-2"></a>
## 4.2 VC-2 逐规则判定差异

- **输入**：VC-1 封存的目标身份、SinkInventory、DiscoveryInventory、语义候选、跨来源矩阵、
  EvidencePackage，以及当前基线 RequiredRules 和 Campaign 用途。
- **操作与工具**：在全离线条件下生成分类草案，逐项完成 discovery／candidate 终态处置，判定
  `inherit/change/condition_change/add/delete`，生成原子断言并按实现责任归并为目标 RequiredRules。
- **产物**：DiscoveryDispositionLedger、CandidateResolutionLedger、RuleMigrationLedger、目标
  RequiredRules manifest、AtomicAssertionLedger、`affected_rules／inherited_rules`、清零收据和
  `rule_classification_recorded` 事实。
- **完成标志**：发现、候选、迁移、原子断言及其归属全部闭合；缺失、重复、孤儿引用、循环引用、
  `blocked` 和其他未决项均为零。
- **失败恢复**：事实不足返回 VC-1 定向补证；已封存分类或规则映射变化时建立后继 Campaign，从 VC-2
  继续并只读复用仍有效的官方证据。

VC-2 只回答“目标版本的每项出站责任相对基线如何变化”。本阶段不生成 Profile、Release 或 Candidate，
不修改 SupportEnvelope 或生产 selector，也不发送任何官方或 candidate 请求。

### 4.2.1 执行步骤与阶段产物

1. 校验 VC-1 `evidence_recorded` checkpoint、目标身份、SinkInventory、DiscoveryInventory、CaptureIndex、
   跨来源矩阵和 EvidencePackage 摘要，并加载当前基线 RequiredRules；任一输入漂移都不得开始分类。
2. 在新的只写一次目录生成分类草案。FW-E `rule-assessments` 中的历史兼容预评估只作为编辑线索；既有规则
   默认保持 `blocked`，不得根据旧结论、相同字符串或摘要相近自动填为 `inherit`。
3. 从已封存 P／R／J／M 离线复算可独立验证的原子断言，逐项绑定适用条件、官方正例、条件对照或零违规
   分母、证据通道、场景、连接和原始字节位置；本步骤不得补发请求。
4. 逐项处置全部 discovery 和 `CAND-*`，使每个源项落到规则、支撑事实、受管出站、非出站、目标缺失或
   规范重复项之一；聚类不能减少源项分母。
5. 按第二部分的复合实现责任粒度，把一条或多条原子断言归并到 RequiredRule；客户端本地命题只能进入
   scenario-only 场景组，不能伪装成出站规则。
6. 对每条基线规则和目标新增规则定稿迁移决定，生成目标规则全集、RuleMigrationLedger，并派生
   `affected_rules／inherited_rules`；两组必须互斥且覆盖完整迁移分母。
7. 执行数量、引用、证据、归属和边界门禁；先以内容寻址对象封存 RuleMigrationLedger 与
   AtomicAssertionLedger，再通过 `rule-classification-record` 把二者、原 VC-1 EvidenceFact 和
   EvidencePackage 绑定为同一 Campaign 的 `rule_classification_recorded`。失败草案不得追加事实或进入
   画像生成与批准流程。

2.1.226 的现有实现由 `claude_fw_f_v21_finalize.py`、`claude_fw_f_discovery_clearance.py` 和
`claude_required_rules_2_1_226.json` 完成实测复算、发现清零及 110→40 映射，具体命令见 §2.1.3。
这些文件绑定当前版本、历史分母和固定计数，只是本阶段合同的已完成实例；后继版本必须使用新策略、
新输出目录和由目标证据动态产生的计数，不能把 2.1.226 的结果直接当成新版本分类。

### 4.2.2 发现终态与迁移分类

每个 `discovery_id` 必须恰有一个已解决记录，并绑定下列至少一种终态；一个发现跨多个语义时可以绑定
多个终态，但不得复制源项或减少原始分母：

| 终态 | 必须绑定 |
|---|---|
| `rule_bound` | 一个或多个既有／新建 `SPEC-*`，以及该发现对规则的证据角色 |
| `supporting_fact_bound` | 所属规则、画像事实或状态／条件／动态值事实及其稳定身份 |
| `managed_egress_bound` | `EgressDispositionInventory` 中的受管出站身份和处置 |
| `non_egress_proven` | 目标证据、可复算理由及为何不影响客户端出站 |
| `target_absent_proven` | 目标 stable 的静态不可达／不存在证据；适用时补充条件感知的运行负例 |
| `duplicate_bound` | 规范发现 ID；引用链必须无环并最终落到以上非重复终态 |

`DiscoveryDispositionLedger` 必须绑定 VC-1 的 DiscoveryInventory、CaptureIndex、跨来源矩阵和
EvidencePackage 摘要；总数与 ID 集合完全一致，每项唯一解决且所有引用存在并双向闭合。
`unclassified`、`mapped_validation`、`catalogued_context`、未收敛 `CAND-*`、无主事实、缺失、重复和
循环引用必须全为 0。SupportEnvelope 缩小不能替代发现项处置，发现项也不得原位改名为规则。

规则迁移与 discovery 终态是两个维度：前者描述 RequiredRule 如何变化，后者描述原始发现如何归属。
每条基线规则必须恰有一个迁移决定，每条目标新增规则必须恰有一个 `add` 来源：

| 迁移决定 | 含义与最低证据 |
|---|---|
| `inherit` | 规则编号、可见行为、条件、依赖和 sink 均未变化，目标静态链与最小运行哨兵成立；旧版本结论或 bundle 字符串相同不能单独支持继承 |
| `change` | 同一实现责任仍存在，但可见 Header、Body、TLS、连接、端点或状态行为变化；必须重新生成规则正文和受影响原子断言 |
| `condition_change` | 行为机制仍存在，但适用条件、分支或状态转换变化；必须具有目标条件成立与不成立的对照证据 |
| `add` | 目标出现既有 RequiredRules 无法表达的新 Sub2API request-egress 责任；新增一条复合 `SPEC-*` 及其一条或多条原子断言 |
| `delete` | 基线责任在目标中静态不可达，且覆盖原触发条件的目标运行负例成立；保留历史编号和引用，实际退休留待后续验收与收据 |
| `blocked` | 证据不足的草案状态；不得进入 VC-3，必须返回 VC-1 补证或明确终止 Campaign |

迁移决定、证据等级、生命周期和兼容类别必须分字段保存，不能互相代替：

| 证据等级 | 使用边界 |
|---|---|
| `verified` | 规则、条件、目标静态／运行证据和复算链闭环，可进入对应用途的后续批准 |
| `observed` | 只证明有限样本；可在 `validation_only` 中显式保留，但不能承担 production replacement 的 strict 责任 |
| `blocked` | 目标事实不足；任何用途均不得通过 VC-2 |
| `regressed_evidence` | 旧 verified 在目标只取得较低等级；可以保留历史事实，但 production replacement 必须先返回 VC-1 补证 |

### 4.2.3 RequiredRules 粒度与原子断言归属

RequiredRule 是 Sub2API 必须复现的一项复合出站责任，继续使用“范围、规则／机制、源码、实测、实现、
状态”六字段；AtomicAssertion 是绑定 P／R／M 和独立断言结果的最小可测试命题。两者计数口径不同：

```text
一条 RequiredRule ← 一条或多条画像原子断言
一条原子断言 → 恰好一个 RequiredRule，或恰好一个 scenario-only 场景组
RequiredRule 数量 ≠ AtomicAssertion 数量
```

每条 RequiredRule 至少有一条画像原子断言，每条原子断言只能有一个 owner；共享同一证据文件不等于可以
跨规则重复归属。映射必须同时被目标规则 manifest、RuleMigrationLedger、AtomicAssertionLedger 和后续
SupportEnvelope 引用；AtomicAssertionLedger 中的证据项 ID 必须能在只读 EvidencePackage v3 中解析，
但不得反向把规则 ID 或迁移决定补写进 EvidencePackage。

下列变化必须先判断责任归属，不能机械增加规则：

| 目标变化 | 归属 |
|---|---|
| 新增或改变 Sub2API 必须复现的 request-egress 机制 | 新增或修改 RequiredRule，并绑定对应原子断言 |
| 仅模型名、精确别名、effort、模型特有 `fallbacks`、辅助模型或字段顺序变化 | 记录为 VC-3 的 ModelCapabilityCatalog 差异输入；只有引入新出站责任时才影响规则 |
| 客户端本地文件、Hook、本地拒绝或上下文装配 | scenario-only 场景或 supporting fact，不进入 RequiredRules |
| 响应解析和 downstream compatibility | compatibility 事实，不进入请求出站规则 |
| telemetry／nonessential 关闭后的合法零流量 | `record_only` supporting fact，不能生成“零流量规则” |
| 只有字符串、静态线索或尚未实测的功能 | 保持候选或 `blocked`，不得创建占位 RequiredRule |

目标版本的 RequiredRules、画像原子断言和 scenario-only 断言数量均由通过的目标证据与唯一映射动态产生，
不得预设为当前 2.1.226 的 40／106／4。迁移集合按下式派生：

```text
inherited_rules = inherit
affected_rules = change ∪ condition_change ∪ add ∪ delete
```

`affected_rules` 和 `inherited_rules` 必须互斥；前者在 VC-3 生成画像差异并在 VC-4～VC-5 执行受影响闭集，
后者只允许重放来源与验收收据。依赖扩圈必须显式登记直接依赖和理由，不能把全部规则伪装成 affected。

正式 RuleMigrationLedger 至少冻结 Campaign／Persona、baseline／target、原 EvidencePackage 引用、
`baseline_spec_ids／target_spec_ids`、逐规则迁移决定／证据等级／生命周期／兼容类别／证据项／适用条件、
两个派生规则集合和可复算身份摘要。正式 AtomicAssertionLedger 必须引用该 RuleMigrationLedger，并逐项冻结
断言 ID、唯一 owner 类型与 ID、证据项、条件、P／R／J／M 通道、结果和可复算身份摘要；每个目标
RequiredRule 至少有一条画像断言，全部 scenario-only owner 也必须闭合。

### 4.2.4 清零门禁、退出与恢复

| 门禁 | 必须满足 |
|---|---|
| 输入身份 | 目标、Campaign、VC-1 checkpoint、inventory、矩阵和 EvidencePackage 摘要一致 |
| 发现与候选 | 每个 discovery／candidate 唯一解决，反向引用完整，无缺失、额外项、重复、孤儿或循环 |
| 迁移闭集 | 每条基线规则唯一分类，每条目标新增规则具有唯一 `add` 来源，目标规则全集可由迁移清单复算 |
| 原子证据 | 普通断言绑定 R／M，TLS 断言绑定 P／M；适用条件、正例与条件对照或零违规分母完整，断言全部通过 |
| 唯一归属 | 每条原子断言恰好归属一条 RequiredRule 或一个 scenario-only 组；每条 RequiredRule 至少有一条画像断言 |
| 责任边界 | 客户端本地、响应兼容、合法零流量和未实测候选没有混入 RequiredRules |
| 输出一致性 | RuleMigrationLedger、目标规则 manifest、AtomicAssertionLedger、DiscoveryDispositionLedger 和两个规则集合的 ID、计数及摘要一致；其中全部证据项引用均在只读 EvidencePackage 中存在 |
| 执行副作用 | 全程离线，`live_request_count=0`，未修改 Profile、Candidate、Runtime Selector 或生产环境 |

VC-2 的退出条件固定为：

```text
VC-1 checkpoint = evidence_recorded
∧ baseline_rules = inherit ∪ change ∪ condition_change ∪ delete
∧ target_rules = inherit ∪ change ∪ condition_change ∪ add
∧ affected_rules ∩ inherited_rules = ∅
∧ 全部 discovery、candidate、原子断言和 owner 映射闭合
∧ blocked、unclassified、missing、duplicate、orphan、cycle = 0
∧ live_request_count = 0
∧ rule_classification_recorded 绑定原 EvidenceFact／EvidencePackage 和两个 Ledger
⇒ 在同一 Campaign 封存 VC-2 分类事实并进入 VC-3
```

VC-2 不建立画像状态 checkpoint；`rule_classification_recorded` 归属 evidence dimension，Campaign 在 VC-3
签发 ApprovalFact 前仍保持 `evidence_recorded`。机器入口固定为先用 `artifact-seal` 封存两个 Ledger，再用
`rule-classification-record` 追加分类事实；不得再次执行 `evidence-record` 或替换 EvidencePackage。
封存前修订必须写入新的 draft revision，不能覆盖旧草案；封存后若迁移决定、RequiredRules 或断言归属
变化，必须建立后继 Campaign 并从 VC-2 继续。若只是缺少目标事实，则返回 VC-1 定向补证，其他官方证据
继续只读复用；任何恢复分支都不得在 VC-2 发送请求或提前修改画像和实现。

<a id="claude-vc-3"></a>
## 4.3 VC-3 生成目标画像

- **输入**：VC-2 封存的 DiscoveryDispositionLedger、RuleMigrationLedger、目标 RequiredRules、
  AtomicAssertionLedger、`affected_rules／inherited_rules`、模型能力差异，以及 VC-0 冻结的当前
  Active／Rollback、Campaign 用途和已封存 EvidencePackage。
- **操作与工具**：从当前 Active 画像派生目标草案，只应用版本身份、`affected_rules` 和已批准模型能力
  差异，生成并联合核对 ProfileSchema、Snapshot、Wire、ReleaseArtifact、ModelCapabilityCatalog、
  SupportEnvelope、两个 Inventory、场景、断言与 Persona 派生；先以 EvidenceApprovalFact 批准原
  EvidencePackage 与 VC-2 分类事实的组合，再签发 ProfileApprovalFact，全程不改写 EvidencePackage。
- **产物**：阶段专用 Profile policy、目标画像及内容寻址 Release、联合审批包、EvidenceApprovalFact、
  ProfileApprovalFact、`official_sealed → profile_approved` checkpoint 和未入生产的暂存 Catalog。
- **完成标志**：Evidence 与画像批准顺序闭合，全部画像差异具有规则或模型事实来源，联合制品 ID、计数和
  摘要一致，生产 Active／Rollback 与 selector 未改变，且尚未创建 Candidate。
- **失败恢复**：迁移或规则错误返回 VC-2，官方事实不足返回 VC-1；批准前只新建 draft revision，批准后
  画像或批准闭集变化时建立后继 Campaign。

VC-3 只回答“如何把已经分类的目标责任表达成可批准画像”。本阶段不修改实现或生产 Catalog，不创建
Candidate，不运行 official／candidate PAIR，也不签发 AcceptanceFact；这些工作分别属于 VC-4～VC-6。

### 4.3.1 画像派生与差异约束

开始生成前，必须校验 `rule_classification_recorded` 对两个 Ledger、原 EvidenceFact／EvidencePackage 的绑定，
并复算当前 Active／Rollback 和 Campaign 用途的摘要。
目标画像从当前 Active 的同一 Schema／DialectCompiler 派生，目标证据拥有行为选择权；“target-first”
表示不能让旧版 fixture 限制目标事实，并不表示丢弃 Active 结构后从空白画像重新拼装：

```text
target_profile = current_active_profile
               + version_identity_patch
               + rule_patches[affected_rules]
               + approved_model_capability_patch

profile_diff_paths
⊆ version_identity_paths
 ∪ rule_field_paths[affected_rules]
 ∪ approved_model_capability_paths
```

每个画像补丁必须记录唯一 owner、JSON path、`before／after`、迁移决定和证据引用。`inherited_rules` 对应
字段必须保持不变；`add／change／condition_change` 只能修改自身画像落点，`delete` 只能从目标选择面移除
已证明不可达的绑定，不能删除历史 Release、规则编号或证据。依赖扩圈必须回指 VC-2 已登记的直接依赖，
不得把完整画像重写伪装成版本升级。

目标生成完成后，再用同一 Schema／DialectCompiler 表达 VC-0 冻结的 Active／Rollback fixture。当前
2.1.226 Campaign 中的 2.1.220 只是该次升级的历史 baseline fixture；后继版本必须使用当次 VC-0 身份，
不能永久把 2.1.220 当成升级基线或目标设计权威。

VC-3 使用只包含本阶段已知事实的 Profile policy，至少冻结目标与基线身份、Campaign 用途、VC-2 输入
摘要、动态规则／断言计数、Schema／Compiler、模型能力差异和生成工具身份。不得预填尚未产生的
Candidate ID、源码／镜像摘要、独立官方复测、DMIT 收据或 AcceptanceFact。

当前 2.1.226 曾使用下列历史聚合入口：

```bash
python3 tools/official_client_capture/claude_fw_f_profile.py \
  --fw-e-store /绝对路径/VC-1-control-store \
  --fw-e-campaign <VC-1-campaign-id> \
  --clearance-dir /绝对路径/VC-2-clearance \
  --clearance-receipt /绝对路径/VC-2-clearance-receipt.json \
  --baseline-rules /绝对路径/baseline-rules.json \
  --rule-manifest /绝对路径/claude_required_rules_<version>.json \
  --policy /绝对路径/claude_fw_f_profile_policy_<version>.json \
  --output-dir /仓库根目录绝对路径/新的-VC-3-output \
  --source-commit <完整提交> \
  --issued-at <冻结时间>
```

`--output-dir` 必须指向仓库内尚不存在的目录；工具禁止向仓库外写入，也禁止覆盖已有目录。

该工具会初始化新的 Store 和 Campaign，不能承接 VC-1 已存在的 Campaign，因此只作为 2.1.226 已完成实例
和历史重放入口，不能作为后继版本的正式 VC-3 producer。正式流程必须在原 Store 中依次追加
`rule_classification_recorded`、`evidence_approved` 和 `profile_approved`；如果新的阶段 producer 尚不能绑定
RuleMigrationLedger、AtomicAssertionLedger、`affected_rules／inherited_rules` 和模型能力差异，则属于
工具阻断。当前
`claude_fw_g_generation_policy_2_1_226_v2.json` 同时包含 `official_finalize` 与 `acceptance` 的后续阶段身份，
只作为不可变历史聚合制品读取，禁止复制成新 Campaign 的 VC-3 policy。若现有 Schema 强制要求未来字段，
必须先独立拆分工具或策略 Schema，再重新执行 P0；不得用占位 Candidate／收据绕过。

### 4.3.2 联合制品与一致性审核

生成顺序固定为：目标 ProfileSchema／Snapshot → ModelCapabilityCatalog → Wire／ReleaseArtifact →
SupportEnvelope → ProductionIngressInventory／EgressDispositionInventory → 场景与断言绑定 → Persona 派生、
compatibility 边界和三个运行 Envelope。每项产物写入新的只写一次目录并内容寻址，不能改写当前 Active、
Rollback 或历史对象。

联合审批包至少包含：

| 制品组 | 必须核对 |
|---|---|
| 规则权威 | 目标 RequiredRules、RuleMigrationLedger、DiscoveryDispositionLedger、AtomicAssertionLedger、`affected_rules／inherited_rules` 的 ID、计数、owner 和摘要 |
| 画像与发布 | ProfileSchema、Snapshot、Wire、ReleaseArtifact 的版本、画像摘要、端点、transport、状态和内容寻址引用 |
| 模型能力 | 目标模型、精确别名、effort、模型特有字段／fallback、辅助模型、场景证据和 ModelCapabilityCatalog 摘要 |
| 支持与处置 | SupportEnvelope、ProductionIngressInventory、EgressDispositionInventory、strict／managed／denied 处置及每个 egress 的规则集合 |
| 场景与断言 | 官方／candidate 场景计划、原子断言 owner、正例、条件对照、负例边界和画像落点 |
| Persona 与运行边界 | Persona 派生、compatibility 边界、ActiveSupportEnvelope、DeploymentTrafficEnvelope、RollbackOperationalEnvelope 和 fail-close 行为 |

RuleMigrationLedger、目标 RequiredRules、Snapshot、SupportEnvelope 和场景计划中的目标规则 ID 必须一致；
EvidencePackage 只参与证据项引用与摘要闭合，不承担规则清单。原子断言映射必须完整且唯一；每个
`affected_rule` 必须有画像补丁和后续验证目标，每个 `inherited_rule` 必须有不变证明和可重放来源。
SupportEnvelope 只能按 Campaign 用途纳入证据等级满足要求的规则和条件，并显式列出排除项；缩小范围不能
替代两个 Inventory 的逐项处置。

每个生产入口仍须选择 `migrated_strict／retained_legacy／explicitly_retired／rerouted`，每个已知 OAuth
出站仍须选择 §3.3 三态。ModelCapabilityCatalog、Profile、Wire 和 Release 必须共享同一目标身份；模型
目录变化不能在 ProfileApprovalFact 之后追加。只有现有 ProfileSchema 或 DialectCompiler 无法表达
VC-2 已批准的新责任时，才停止 VC-3，按 Framework §5.4 独立修改共享合同并复验受影响 Persona；不得在
Claude 运行代码中增加临时版本分支。

VC-3 只生成未入库的暂存 Release／Catalog。纳入同源 Candidate 源码树属于 VC-4，生产 Catalog 注册和
selector 切换属于 VC-6。`generate_claude_fw_g_profile.py` 只有在其最终 Profile、Wire、模型目录和 Release
全部进入同一份批准摘要时，才能作为本阶段的确定性投影工具；当前历史输出只按 §3.4／§3.6 审计。

### 4.3.3 EvidenceApprovalFact、ProfileApprovalFact、退出与恢复

VC-2 分类事实封存后，审核者签发 EvidenceApprovalFact；该事实必须同时引用原 `evidence_recorded` 事实、
未改写的 EvidencePackage v3 和同一 Campaign 的 `rule_classification_recorded`。分类事实再绑定
RuleMigrationLedger 与 AtomicAssertionLedger，因此无需、也禁止把 VC-2 结果补写进 EvidencePackage。
批准通过后 Campaign checkpoint 达到 `official_sealed`；缺少 EvidenceApprovalFact 时不得签发画像批准。
EvidencePackage v1/v2 及不含 `classification_fact_ref` 的旧 EvidenceApprovalFact 仅保留只读重放兼容；新的
正式 Campaign 必须使用 v3 路径，不得借历史格式绕过 VC-2。

完成草案生成、联合复算、跨 Release／跨 Persona 负例和人工审核后，才能一次性签发
ProfileApprovalFact。该事实必须引用上述 EvidenceApprovalFact，并以同一联合摘要冻结目标规则全集、迁移
与发现清单、EvidencePackage、Profile、Wire、Release、ModelCapabilityCatalog、SupportEnvelope、两个
Inventory、场景、断言、Persona 派生、compatibility 边界、三个运行 Envelope、隐私模式和 Campaign 用途。

ProfileApprovalFact 不得引用未来 Candidate、构建或镜像，不得预填 official rerun、DMIT 或生产收据，
也不得更新 Runtime Selector。`claude_fw_g_official_finalize.py` 与 `claude_fw_g_acceptance.py` 分别消费独立
官方复测、Candidate 和 DMIT 结果，属于 VC-5 的验证与 AcceptanceFact 封存入口，VC-3 禁止执行。

VC-3 的退出条件固定为：

```text
VC-2 rule_classification_recorded 已在同一 Campaign 封存
∧ EvidenceApprovalFact 引用同一 EvidenceFact／EvidencePackage／classification fact
∧ checkpoint = official_sealed
∧ profile_diff_paths
  ⊆ version_identity_paths
   ∪ rule_field_paths[affected_rules]
   ∪ approved_model_capability_paths
∧ 联合制品的规则、断言、端点、模型、计数和摘要一致
∧ ProfileApprovalFact 绑定联合摘要且 purpose = VC-0 campaign_purpose
∧ Campaign checkpoint = profile_approved
∧ Candidate 未创建且 live_request_count = 0
∧ Active、Rollback 和 production selector 均未变化
⇒ 进入 VC-4
```

批准前的生成或复算失败只写新的 draft revision，输入未变时可留在 VC-3 重试。发现规则分类错误返回
VC-2，缺少官方事实返回 VC-1；ProfileApprovalFact 封存后若规则、迁移、画像、模型目录、SupportEnvelope、
场景、断言、用途或联合摘要变化，必须建立后继 Campaign，并从最早受影响阶段继续。仅后续源码、测试、
构建或镜像变化时不改写本批准，在 VC-4 建立新的 Candidate。2.1.226 当前批准与续作状态只见
§3.4／§3.6，不在本节重复维护。

<a id="claude-vc-4"></a>
## 4.4 VC-4 实现固定 Candidate

- **输入**：VC-3 的 ProfileApprovalFact 与联合摘要、未入生产的 Profile／Wire／Release／
  ModelCapabilityCatalog、SupportEnvelope、两个 Inventory、`affected_rules／inherited_rules`、原子断言、
  Campaign 用途，以及 VC-0 冻结的构建环境和目标架构。
- **操作与工具**：只实现 `affected_rules` 及其直接依赖，把批准画像、实现与测试纳入同一最终源码树，
  从该树构建不可变镜像，生成构建收据并独立冻结 ValidationCandidate。
- **产物**：候选源码树、测试树、依赖锁、CandidateBuildReceipt、制品 inventory、`candidate_frozen` 事实和
  VC-5 执行／复用计划。
- **完成标志**：实现闭集通过，源码、测试、依赖、画像、Release、模型目录、构建和镜像身份可复算，
  Campaign 到达 `candidate_sealed`，生产 Active／Rollback／selector 未改变且未发起候选请求。
- **失败恢复**：只变更源码、测试、构建或镜像时保留 Campaign 并建立新 Candidate；规则或画像变化时建立
  后继 Campaign，分别返回 VC-2 或 VC-3。

VC-4 只回答“如何把已批准画像实现并冻结成可验证的 Candidate”。本阶段不重写 VC-3 事实，不执行
official／candidate PAIR，不创建 attempt 或 run nonce，不连接 DMIT 运行 Candidate，也不签发
AcceptanceFact。

### 4.4.1 入库与实现边界

开始实现前必须复算 ProfileApprovalFact、联合摘要和全部输入清单，并确认 `approval_purpose` 与 Campaign
用途一致。将 VC-3 暂存的 Profile、Wire、Release、ModelCapabilityCatalog 及其只读引用纳入候选源码树或
确定性构建上下文；只能追加目标对象，不能改写当前 Active、Rollback、历史 Release 或生产 Catalog。

实现闭集必须满足：

1. 每项代码、测试、画像装载和路由变化都能回指 RuleMigrationLedger 中的 `affected_rule`、对应原子断言
   及直接依赖；依赖扩圈必须记录完整路径，不能借升级实施夹入无关重构；
2. `inherited_rules` 对应目标语义和画像保持不变，只重放来源、既有测试和验收收据；共享实现锚点仅在已登记
   直接依赖要求时允许变化，并必须补做继承规则回归。执行计划分别记录规则总数、受影响数、继承数、实际
   执行数和复用数；
3. ValidationCandidate 直接绑定独立 immutable ReleaseArtifact，不借用 production active、rollback 或
   `previous` selector；生产默认选择器在 VC-4 全程不变；
4. Candidate 显式绑定目标 Profile、内容寻址 ModelCapabilityCatalog 和 SupportEnvelope；Planner／Compiler
   对范围外入口、模型、条件和出站 fail-close，连接池、fallback 和状态不得跨 Profile／Release／模型身份；
5. 版本、endpoint、Header、Body、TLS、重试、状态和工具语义优先由批准画像与 Claude DialectCompiler
   表达；只有现有共享合同无法表达时，才按 Framework §5.4 停线处理，禁止增加临时版本分支；
6. 发现批准闭集外的新行为时停止实现：规则或迁移分类错误返回 VC-2，目标画像或模型目录需要变化时返回
   VC-3；不能修改已封存事实来迁就当前代码。

实现测试和受影响闭集测试全部通过后，才能确定最终源码树。此后任何会进入 Candidate 数据面的代码、测试、
画像、生成资产或依赖变化，都会使原构建失效；纯控制面 evaluator 变化按 Framework §5.1.3 处理，但若其
进入候选源码树、测试树或镜像，仍必须建立新 Candidate。

### 4.4.2 同源构建

同一最终源码树是源码摘要、测试摘要、依赖摘要、二进制和镜像的唯一输入。禁止从不同提交、未登记 dirty
worktree、临时复制文件或多个节点的未封存输出拼装 Candidate。当前构建拓扑执行 §6.2／§6.5 的“Mac 编译、
ARM64 只封装、DMIT 只运行”；后继 Campaign 若改变节点或架构，必须先在 VC-0 重新冻结，不能沿用历史
`linux/amd64` 常量。

本文将本阶段生成的只写一次联合构建收据称为 CandidateBuildReceipt，至少冻结：

| 身份组 | 必须记录 |
|---|---|
| Campaign 与批准 | Campaign ID／摘要、用途、目标版本、ProfileApprovalFact 引用及联合摘要 |
| 源码与测试 | Git commit／tree、完整 source tree 摘要、test tree 摘要、Go／Node 依赖锁摘要和 clean 状态 |
| 画像与范围 | Profile／Wire／Release／ModelCapabilityCatalog 摘要、SupportEnvelope 引用及执行／复用计划摘要 |
| 构建 | build ID、deployed version、目标架构、工具链、构建参数、前端与后端制品摘要及来源关系 |
| 镜像与清单 | 内容寻址镜像引用、image ID、OCI manifest digest，以及逐文件路径、摘要、大小和总数 |

镜像交接必须使用可复算的 OCI manifest digest，不能只写可变 tag。VC-5 和 VC-6 必须运行本阶段冻结的同一
镜像；后续阶段重编译、重新封装或向镜像追加文件都会产生新 Candidate，不能以“源码相同”为由复用旧身份。
使用受管 finalizer 生成并只写一次保存 CandidateBuildReceipt；`receipt-replay` 会从批准事实、内容寻址对象和
收据中的冻结输入重新构造同一字节，拒绝手工散列、可变 tag、历史 generation policy 或事后
AcceptanceFact：

```bash
python3 -m tools.official_client_control candidate-build-finalize \
  --store /绝对路径/VC-3-control-store \
  --campaign <campaign-id> \
  --input /绝对路径/candidate-build-input.json
```

### 4.4.3 Candidate 身份冻结与 VC-5 交接

CandidateBuildReceipt 通过后，按
`tools/official_client_control/schemas/validation-candidate.schema.json` 生成 payload。ValidationCandidate
必须一次冻结以下字段：

| 身份层 | `candidate_frozen` 必须冻结的字段 |
|---|---|
| Candidate | `schema_version=official-client-validation-candidate/v2`、`candidate_id`、`candidate_purpose`、`candidate_build_receipt_ref` |
| 批准与发布 | `profile_approval_ref`、`release_artifact_ref`、`support_envelope_ref` |
| 源码 | `source_tree_sha256`、`test_tree_sha256`、`dependency_lock_sha256` |
| 构建与镜像 | `target_architecture`、`build_id`、`image_digest` |
| 联合身份 | 对上述字段计算的 `identity_sha256` |

`candidate_purpose` 必须与 ProfileApprovalFact 的 `approval_purpose` 完全一致，ReleaseArtifact 和
SupportEnvelope 必须直接来自同一批准事实；CandidateBuildReceipt 中更完整的 commit、tree、二进制、
构建参数、ModelCapabilityCatalog、image ID 和镜像引用也必须与 payload 一致。使用共享控制面追加事实：

```bash
python3 -m tools.official_client_control candidate-freeze \
  --store /绝对路径/VC-3-control-store \
  --campaign <campaign-id> \
  --input /绝对路径/validation-candidate-payload.json \
  --issued-at <冻结时间>
```

严格 v2 payload 必须直接引用刚生成的 `candidate_build` 收据；受管入口会逐项核对批准引用、用途、Persona、
source／test／dependency、build ID、架构和 OCI digest。后继 Campaign 必须在首个 VC-5 attempt 和候选真实
请求之前独立取得 `candidate_frozen`。`claude_fw_g_acceptance.py` 将 Candidate 与 AcceptanceFact 聚合生成，
只用于解释历史 2.1.226 制品，不能作为后继版本的 VC-4 入口。

VC-4 的退出条件固定为：

```text
ProfileApprovalFact 与 VC-3 联合摘要可复算
∧ 实现范围 = affected_rules ∪ 已登记直接依赖
∧ inherited_rules 的目标语义和画像变化数 = 0
∧ source／test／dependency／profile／release／model／build／image 身份一致
∧ CandidateBuildReceipt 与 inventory 可复算
∧ 唯一 candidate_frozen 事实通过且 Campaign checkpoint = candidate_sealed
∧ attempt_count = 0 ∧ live_request_count = 0
∧ production Active、Rollback 和 selector 均未变化
⇒ 进入 VC-5
```

冻结前的实现或构建失败，在输入身份不变时可以留在 VC-4 重试；冻结后源码、测试、依赖、构建参数、二进制、
镜像或运行时 Profile 任一变化，必须保留旧事实并建立新 Candidate。历史 Sonnet-only 与三模型 Candidate 的
精确身份分别以 [FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)和
[三模型 FW-G 验收收据](egress/maintenance/claude-fw-g-three-model-acceptance.json)为准，当前续作状态见
§3.6；这些历史身份不直接代表当前入站准入、候选交付或生产状态。

<a id="claude-vc-5"></a>
## 4.5 VC-5 定向验证

- **输入**：VC-4 固定的 ValidationCandidate、CandidateBuildReceipt、VC-1 官方 EvidencePackage、VC-2
  RuleMigrationLedger／AtomicAssertionLedger、VC-3 ProfileApprovalFact／SupportEnvelope／场景计划，以及
  执行／复用闭集和 VC-0 冻结的候选环境。
- **操作与工具**：复算 Candidate 身份并创建 attempt，在 DMIT 运行定向正负场景，四阶段
  封存候选证据，离线生成官方投影和 PAIR，执行逐规则断言与外部门禁，再签发 AcceptanceFact。
- **产物**：attempt 与运行环境记录、CandidateEvidencePackage、场景批准链、逐规则 PAIR／断言结果、
  门禁收据、AcceptanceFact 和 VC-6 交接清单。
- **完成标志**：受影响规则及直接依赖全部通过，继承规则来源与收据可重放；workflow checkpoint 按用途
  达到 `ready` 或 `validation_only`，production state 保持 `not_activated`，且生产 Active／Rollback／
  selector 未改变。
- **失败恢复**：按 Framework §5.3.4 从最近合法 checkpoint 继续，只执行失败、未完成或依赖变化项；
  已封存官方请求不得因 Candidate、工具或报告变化而重发。

严格合同由 `validation-workflow.schema.json`、`scenario-pair.schema.json` 和追加式 Store 共同执行。
`validation-attempt-create` 冻结 Candidate、CandidateBuildReceipt、执行计划、run nonce、环境／工具／配置／
网络／账号模型条件、生产 selector 前态和 Vircs 未触碰状态；四阶段场景、CandidateEvidencePackage、v2
PAIR、外部门禁收据和 v2 Acceptance 必须全部闭合后，`validation-finalize` 才能追加
`validation_completed`。只有这条完成事实会把严格 Acceptance 推导为 `ready／validation_only`；单独写入
Acceptance 只会停在 `validation_pending`。

`claude_fw_g_official_finalize.py`、`claude_fw_g_candidate_pair.py` 和 `claude_fw_g_acceptance.py` 仍只复算各自
冻结的 2.1.226 历史实例，不得代替上述通用链路。

VC-5 只回答“固定 Candidate 是否满足已批准规则和边界”。它可以让已登记官方客户端驱动 DMIT 上的
Candidate，但这属于候选验收流量，不是重新采集官方上游证据。Vircs 不运行 Candidate、不换镜像，也不因
本阶段连接或修改生产服务。最终候选切换、实际回退、目标恢复、稳定观察和交付收据属于 VC-6。

| 步骤 | 工作 | 检查点 |
|---|---|---|
| 1 | 编译 `execute／reuse` 闭集，重放 VC-1～VC-4 输入并创建 attempt | 验证计划与运行身份冻结 |
| 2 | 按 `prepare → capture` 执行定向场景、模型和入口边界 | 必需运行坐标取得 capture checkpoint |
| 3 | 完成 `seal → approve` 并封存 Candidate 证据 | CandidateEvidencePackage 与场景批准链闭合 |
| 4 | 只读生成官方投影并离线比较 | comparison 完整且只读 |
| 5 | 生成逐规则 PAIR 并执行机器断言 | RequiredRules 与原子断言完整覆盖 |
| 6 | 执行受影响闭集门禁并签发 AcceptanceFact | `ready` 或 `validation_only` |
| 7 | 登记完成事件并只读交接 VC-6 | 生产 selector 未变化 |

### 4.5.1 执行闭集、运行前检查与 attempt 冻结

先从 `affected_rules`、已登记直接依赖、场景依赖和最近合法 checkpoint 编译执行计划。完整场景计划定义
验收全集，不表示每次都全量重跑；`inherited_rules` 只能重放 VC-2 批准的来源和验收收据。计划必须分别
记录规则总数、受影响数、继承数、实际执行数、复用数和依赖扩圈路径。`execute=[]` 时不得创建空 attempt
或发送请求，只生成零请求完成记录并进入离线复算。

存在执行项时，任何 Candidate 请求和环境修改之前必须完成：

1. 重放 ProfileApprovalFact、`candidate_frozen`、CandidateBuildReceipt、Release、Profile、
   ModelCapabilityCatalog、SupportEnvelope 及其摘要，确认 purpose、目标版本和执行计划一致；
2. 复核实际镜像的内容寻址引用、image ID／OCI digest、源码树、测试树、依赖锁、build ID、目标架构和
   运行时 Profile；任何字段漂移都不得创建 attempt；
3. 复核 DMIT 的 Compose、应用容器、依赖容器、挂载、网络、磁盘、回退材料和资源水位；只允许替换隔离
   Candidate 应用，不得重建数据库、缓存、网络、挂载或其他依赖；
4. 复核账号状态、OAuth 权限、三个来源客户端身份、模型可见性、代理／CA／DNS、隐私模式、秘密扫描器和
   实际 runner；敏感值只进入权限受限的运行态，不写入仓库、日志或公开收据；
5. 确认 Vircs 未连接、生产 Active／Rollback／selector 未变化，且 VC-1 官方 EvidencePackage 仍可重放。

全部通过后才原子创建 attempt，冻结 Candidate ID／身份摘要、执行计划摘要、环境与工具摘要、配置、
代理／CA／DNS、账号与模型条件、`attempt_id`、`run_nonce` 和开始时间。VC-4 已冻结的数据面身份只能复算，
不能在这里重新定义。仅运行坐标变化时须符合 Framework §5.3.4。先封存
`validation_execution_plan` 对象，再通过类型化入口创建 attempt：

```bash
python3 -m tools.official_client_control validation-attempt-create \
  --store /绝对路径/Campaign-control-store \
  --campaign <campaign-id> \
  --input /绝对路径/validation-attempt-payload.json \
  --issued-at <创建时间>
```

### 4.5.2 定向场景、模型与入口边界

Candidate 服务只在 DMIT 或等价隔离候选环境运行；Mac 上已冻结身份的 Claude Code、Claude Desktop 与
Claude Code for VS Code 可以按场景计划驱动该 Candidate。每个 `subject × scenario` 使用独立运行坐标，
只执行计划中的 `execute_item_ids`；通过项立即建立 checkpoint，恢复时不得重发。

场景由 SupportEnvelope、AtomicAssertionLedger 和两个 Inventory 动态派生。当前 2.1.226 实例至少覆盖：

| 场景组 | 必须验证 |
|---|---|
| 生命周期与 wire | `s1／s2／s4／a1` 推理、`HEAD /api/hello`、TLS／ALPN、HTTP/1.1、故障重试和连接关系 |
| 会话与状态 | 主轮／续轮／Agent、TUI 标题、WebSearch 往返、request-id、fallback 锁存、重启恢复及状态存储 fail-close |
| 模型与工具 | 每个已登记模型的基础、effort、thinking、background、WebSearch、count_tokens、fallback，以及未知模型／别名和跨模型复用负例 |
| 入口与处置 | 三类已登记官方客户端的 Messages／count_tokens 正例；第三方、未知版本及非法 Header／Body／工具目录在 OAuth 凭据读取前拒绝 |
| Persona 与隔离 | strict／managed／legacy 三态、隐私模式、system／identity 派生、跨 Persona／Release 拒绝和 Codex final wire 零差异 |

原生 TLS、重试、TUI、remote、custom Header／beta／metadata、Agent 层级、background、hook、
server／deferred tools、附件、count_tokens、OAuth refresh 和 MCP servers 只有在目标 SupportEnvelope 内才
进入本轮计划。合法零流量只能作为支撑事实，不能自动生成规则或 Candidate 能力。

只有 OfficialIngressCatalog 已登记且 TranslationReport 为 lossless 的官方来源进入正向
`PAIR-<SPEC-ID>`；第三方入口只进入凭据前拒绝负例。动态字段必须比较来源、格式、关系和生命周期，
Persona system／identity 只按 VC-3 批准的派生事实比较。每个必需运行坐标完成后立即建立 checkpoint，
最后一个请求完成后关闭发送面；后续封存、比较、报告修复和 evaluator 更新均不得追加请求。

### 4.5.3 Candidate 证据封存

每个场景严格按 `prepare → capture → seal → approve` 追加事实：prepare 必须在请求前冻结前置条件；
capture 绑定本次运行的原始候选证据和 attempt；请求关闭后，seal 才能完成 inventory、秘密扫描、环境
after 探针与内容摘要；approve 最后由审核者绑定封存摘要。失败 attempt、失败场景和原始证据均只读保留，
不能覆盖成通过。应用重启、状态恢复等规则场景可在隔离 attempt 内执行；面向交付的最终候选切换、实际
回退和稳定观察不得在本阶段提前宣称完成。

CandidateEvidencePackage 必须绑定 Candidate／attempt 身份、场景与 checkpoint 全集、原始证据 inventory、
环境 before／after、恢复结果、秘密扫描和联合摘要。封存过程只能读取本 attempt 已登记的证据根，不能遍历
无关历史目录或用离线生成结果补写 capture 事实。

候选运行、checkpoint 和四阶段封存必须由 Campaign 冻结的 runner／finalizer 生成。§3.6.1 记录的外部
live-matrix 只能续作其已绑定 Candidate，不能复制成后继版本的通用入口；新 Campaign 若没有可复算的
attempt、证据封存和恢复工具链，属于 P0／VC-5 工具阻断。

### 4.5.4 离线比较

官方侧只读取 VC-1 已封存证据。`claude_fw_g_official_finalize.py` 是当前 2.1.226 的离线投影实例：它只读
复算 Vircs Campaign，不发送请求，也不单独授予 `verified`。后继版本只有在输入 policy 已拆成 VC-5
阶段专用事实、规则与断言计数改为动态且不包含 Candidate／Acceptance 结果后，才能复用；缺少官方事实时
返回 VC-1 定向补证，不能在 VC-5 隐式重采。

Candidate 侧只读取 CandidateEvidencePackage、同源测试结果和 VC-4 身份。离线比较先复算两侧目标版本、
Profile、Release、场景覆盖、证据 inventory 和条件集合，生成内容寻址 comparison；不得在比较阶段发送
请求、修改事实或补齐缺失场景。

comparison 完整只表示两侧身份、覆盖和比较输入闭合，不等于行为相同或规则通过。即使 surface 集合
`equal=true`，仍必须进入 4.5.5；因场景计划不同而 `equal=false` 时，只要差异均有批准分类、覆盖无缺口且
比较保持 `offline_only`，也不能据此直接判定失败或通过。

### 4.5.5 逐规则机器断言

comparison 完成后，为每条目标规则唯一生成 `PAIR-<SPEC-ID>`，并绑定：

- 同一 ProfileApprovalFact、ReleaseArtifact、Candidate 和条件摘要；
- 官方侧与 Candidate 侧的来源、证据 inventory 和场景批准引用；
- `dual_wire` 或 `candidate_profile` 判定及动态字段四维比较；
- 对应 AtomicAssertionLedger owner、实现／测试锚点和执行或复用来源。

逐规则结果必须唯一覆盖目标 RequiredRules 全集，affected 项执行本轮机器断言，inherited 项只重放批准
收据；不得出现重复、缺失、N／A、手写通过或无 inventory 绑定的证据。当前 2.1.226 分母为 40 条
RequiredRules、106 条画像原子断言和 4 条客户端本地场景断言；后继版本必须读取 VC-2 封存计数，不能固化
这些数字。

`claude_fw_g_candidate_pair.py` 当前固定 `2.1.226／40／106／110`，并以三个 Go package 的测试结果生成历史
PAIR，只能复算该实例；它不能代替 DMIT CandidateEvidencePackage，也不能直接用于下一版本。后继版本由
`pair-record` 的严格 v2 合同读取 RuleMigrationLedger／AtomicAssertionLedger 的动态集合，并强制保存
Candidate 结果、实现／测试锚点和 `execute／reuse` 来源。

### 4.5.6 外部门禁与 AcceptanceFact

在 CandidateBuildReceipt 绑定的同一源码树和目标平台执行 VC-4 计划中的外部门禁。至少包含规则／Catalog
一致性、受影响实现测试、目标平台运行检查、strict／managed／legacy 边界、跨 Persona／Release 负例、
秘密扫描和证据 inventory 复算；继承门禁只重放收据。每项结果必须绑定 Candidate、attempt、命令、工作
目录、主机、架构、起止时间、退出码、输出摘要和前序失败收据；失败、跳过或身份漂移均不得签发 AcceptanceFact。
首次执行的 `previous_failed_receipt_ref` 为 `null`；同一 gate 失败后不得在原 attempt 覆盖为通过，新 attempt
必须引用最近一份失败收据。`reuse` 项必须逐项重放来源收据，并保持 requirement、命令、工作目录、主机和
架构不变。

全部场景、PAIR、逐规则结果和外部门禁通过后，AcceptanceFact 必须直接引用 VC-3 ProfileApprovalFact、
VC-4 `candidate_frozen`、唯一 `PAIR-<SPEC-ID>` 集合、边界断言和 inventory 断言。用途与结果固定为：

| Campaign 用途 | `acceptance_purpose` | `result` | workflow checkpoint／production state |
|---|---|---|---|
| `validation_only` | `validation_only` | `validation_only` | `validation_only／not_activated` |
| `production_replacement` | `production_replacement` | `accepted` | `ready／not_activated` |

每个外部门禁先生成不可覆盖收据，再写入严格 v2 AcceptanceFact：

```bash
python3 -m tools.official_client_control validation-gate-finalize \
  --store /绝对路径/Campaign-control-store \
  --campaign <campaign-id> \
  --attempt-ref /绝对路径/validation-attempt-ref.json \
  --input /绝对路径/gate-result.json

python3 -m tools.official_client_control acceptance-record \
  --store /绝对路径/Campaign-control-store \
  --campaign <campaign-id> \
  --input /绝对路径/acceptance-fact-payload.json \
  --issued-at <验收时间>

python3 -m tools.official_client_control validation-finalize \
  --store /绝对路径/Campaign-control-store \
  --campaign <campaign-id> \
  --acceptance-ref /绝对路径/acceptance-ref.json \
  --selector-after-ref /绝对路径/selector-after-ref.json \
  --issued-at <完成时间>
```

`validation-finalize` 不执行外部命令或补写证据；它从 Acceptance、attempt、执行计划、
CandidateEvidencePackage、PAIR 和门禁收据复算完整闭集，确认 ScenarioPlan、RequiredRules、原子断言、
Inventory、selector 前后态及 Vircs 状态后，只追加 `validation_completed`。当前
`claude_fw_g_acceptance.py` 仍只解释历史 2.1.226 聚合制品。

AcceptanceFact 不晋升 Release、不注册生产 Catalog、不更新 selector，也不证明候选已经交付。HTTP 200、
模型正例、单条负例、测试全绿或 wire `equal` 均不能替代完整逐规则与边界验收。

### 4.5.7 验收终态与 VC-6 交接

VC-5 的退出条件固定为：

```text
VC-4 CandidateBuildReceipt 与 candidate_frozen 可复算
∧ execute_items 全部通过 ∧ reused_items 全部可重放
∧ 必需场景均完成 prepare → capture → seal → approve
∧ CandidateEvidencePackage、秘密扫描和 inventory 完整
∧ PAIR 数 = 目标 RequiredRules 数且原子断言归属完整唯一
∧ 受影响外部门禁全部通过且继承门禁收据可重放
∧ AcceptanceFact 的 Candidate、Profile、Release、用途和摘要一致
∧ production Active、Rollback、selector 与 Vircs 均未变化
⇒ 登记 VC-5 完成事件并进入 VC-6
```

恢复先执行本部分公共约定，再应用本阶段边界：临时失败只补做 `failed／pending`；Candidate 数据面身份
变化返回 VC-4，画像／范围、规则／断言和官方事实缺口分别返回 VC-3、VC-2、VC-1。已封存请求、历史
attempt 和原始时间账本始终只读。

workflow checkpoint `ready` 或 `validation_only` 只证明固定 Candidate 在批准范围内完成验收，且此时
production state 仍为 `not_activated`；两个用途都必须进入 VC-6 才能形成候选交付终态。当前执行权限不含
Vircs，不建立 Codex 专属 canonical-import，也不在本阶段宣称生产激活。

历史 Store 必须使用其收据登记的 external root 重放；FW-G 的冻结 Store 使用同目录
`external-replay-view`，不能拿当前工作区替代。`claude_21220/check_coverage.py` 只能在其冻结工具身份对应的
worktree 运行，不能把当前 MITM／提取器演进误报成历史规则失败。2.1.226 的发现统计、清零结果、历史
Sonnet-only／三模型 Candidate 和当前续作状态统一见 §3.4／§3.6、
[FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)及
[三模型 FW-G 验收收据](egress/maintenance/claude-fw-g-three-model-acceptance.json)，不在本节重复维护。

<a id="claude-vc-6"></a>
## 4.6 VC-6 交付或生产激活

- **输入**：VC-5 完成事件、同一 ValidationCandidate、CandidateBuildReceipt、`candidate_frozen`、
  CandidateEvidencePackage、AcceptanceFact、`campaign_purpose`、VC-6 交接清单，以及 DMIT 环境与回退材料；
  生产激活另需明确生产权限和 P0 冻结的生产输入。
- **操作与工具**：只读复算交付输入，按用途分流；在 DMIT 使用 VC-4 已冻结的同一镜像完成
  候选切换、实际回退、目标恢复和稳定观察，随后封装候选并签发交付收据。只有具备生产权限时才进入
  独立生产激活分支。
- **产物**：候选交付包和 `ready_for_operator_release` 收据；生产分支另生成正式生产身份、激活／回滚／恢复
  证据和 DeploymentFact。
- **完成标志**：候选交付收据与 VC-5 AcceptanceFact、VC-4 Candidate 及 DMIT 回退／恢复事实闭合；生产
  分支还须满足 Framework §5.6.2。当前任务不得改变或声称验证 Vircs。
- **失败恢复**：保留失败事实并从最近合法 checkpoint 续跑 `failed／pending`；身份或批准输入变化时返回
  最早受影响阶段，禁止为取得通过而无依据重跑 VC-0～VC-5。

严格交付合同由 `candidate-delivery.schema.json`、`candidate-delivery-record`、
`candidate-delivery-finalize` 和 `receipt-replay` 实现。四个 checkpoint 都使用专用
`candidate_delivery_recorded` fact kind，并在 payload 中保存 stage；其中 `rollback_verified` 不会与通用生产
链的同名 fact kind 混用。finalizer 对 `validation_only` 和 `production_replacement` 一视同仁地先生成
`release_state=ready_for_operator_release`，不要求或暗示生产恢复终态。Codex `deliver-candidate` 及历史
`claude-fw-h-*.json` 不属于本入口，仍不得改名或复制复用。

VC-6 只回答“已验收 Candidate 是否已经形成可交付终态，以及获授权时是否完成生产激活”。它不重新证明
全部画像规则，也不重新定义或构建 Candidate。候选交付与生产激活必须沿同一 Candidate／Acceptance 链
推进，但两者是不同终点。

| 步骤 | 工作 | 检查点 |
|---|---|---|
| 1 | 重放 VC-4／VC-5 输入并按用途分流 | Candidate、Acceptance 与用途一致 |
| 2 | 只读冻结 DMIT 现状并执行交付专用门禁 | 固定 Candidate 镜像可部署，真实回退点可用 |
| 3 | 候选切换、实际回退、目标恢复和稳定观察 | 四阶段证据闭合 |
| 4 | 封装交付材料并签发候选交付收据 | `ready_for_operator_release` |
| 5 | 具备权限时进入独立生产激活分支 | DeploymentFact；当前任务不执行 |
| 6 | 复算终态、归档并登记完成或失败恢复 | 收据可重放，未授权环境未改变 |

### 4.6.1 用途分流与交付输入复算

进入本阶段先重放 VC-5 完成事件与 AcceptanceFact，并核对 Campaign ID／用途、Candidate ID／联合身份、
CandidateBuildReceipt、`candidate_frozen`、源码／测试／依赖摘要、Profile／Wire／Release／
ModelCapabilityCatalog、SupportEnvelope、CandidateEvidencePackage、逐规则结果、门禁收据、目标架构和 OCI
manifest digest。所有引用必须来自 VC-4／VC-5 封存的同一条链；缺失、重复或摘要漂移均不得开始 DMIT
切换。

用途与执行终点固定为：

| `campaign_purpose` | VC-6 必做终点 | 后续边界 |
|---|---|---|
| `validation_only` | 完成 DMIT 候选交付并签发 `ready_for_operator_release` | 立即结束，不创建生产事实 |
| `production_replacement` | 先完成同一候选交付终点 | 仅在生产权限和 P0 输入齐备时继续 §4.6.5 |

Campaign 用途不得在 VC-6 改写。当前 2.1.226 任务虽已取得面向生产替换的 official-client-only Approval，
但执行权限只覆盖 DMIT 候选交付，因此在 `ready_for_operator_release` 停止；这不是用途降级，也不表示 Vircs
已经进入或完成生产激活。

### 4.6.2 DMIT 最终交付门禁

任何写操作前，只读冻结 DMIT 当前应用镜像、Compose／override、依赖容器、数据库、缓存、挂载、网络、
代理／CA／DNS、账号可用性、磁盘与资源水位，并将当前可运行应用镜像及配置登记为本轮 DMIT 回退点。
2.1.220 只是 baseline fixture；除非它正是本轮冻结并实测可恢复的运行对象，否则不得自动充当 strict
rollback。

VC-6 必须直接取得并核验 VC-4 CandidateBuildReceipt 记录的内容寻址镜像。**不得在本阶段重编译、重新封装、
追加文件或生成新的候选镜像**；VC-5 与 VC-6 必须使用同一 OCI manifest digest、image ID、build ID、
源码树、测试树、依赖锁和运行时 Profile。精确镜像不可取得、架构不符或任一身份字段变化时，保留当前失败
事实并返回 VC-4 建立新 Candidate，不能用“源码相同”或可变 tag 继续。

本阶段门禁只覆盖交付环境与终态完整性，例如镜像身份、Compose 差异、启动与 health、依赖连通、入口路由、
秘密扫描、证据 inventory、回退可用性和稳定观察条件。VC-5 的正负场景、逐规则 PAIR、原子断言和外部门禁
只重放封存收据；不得重跑完整 Candidate／规则矩阵，不得因生成交付报告而重发官方请求。确有必要的交付
smoke 必须预先列入 VC-6 交接清单，且不能扩展 SupportEnvelope 或替代 VC-5 验收。

### 4.6.3 候选切换、实际回退、恢复与稳定观察

交付门禁通过后，仅把 DMIT 应用服务切换到 VC-4 冻结的 Candidate 镜像，不重建数据库、缓存、挂载、网络
或其他依赖。切换前后均复核实际 image ID／RepoDigest、Compose、health、依赖、入口、运行时 Profile 和
关键业务事件，禁止以强制 mode、临时环境变量或未登记配置掩盖默认行为。

随后按同一计划依次形成四个不可覆盖 checkpoint：

1. `candidate_active`：固定 Candidate 在 DMIT 启动并通过交付 smoke；
2. `rollback_verified`：只替换应用容器，切回 §4.6.2 冻结的真实回退镜像并验证 health、鉴权、
   数据和依赖；
3. `candidate_restored`：恢复同一 Candidate digest，复核 Profile、入口和运行事实未漂移；
4. `stable_observed`：在冻结观察窗内无身份、安全、数据、状态或跨 Persona／Release 混用异常。

每个 checkpoint 都必须绑定时间、主机与架构、镜像摘要、Compose 摘要、运行条件、检查结果及前序事实。
任一步失败立即恢复已冻结回退镜像，保留失败现场和日志；不得在故障容器上修改代码、画像、依赖或配置后
继续沿用原 Candidate。`candidate_delivery_recorded.result` 只能为 `pass／failed`；`failed` 必须绑定显式
失败证据并终止该阶段链，后续阶段不得承接失败事实。只有四阶段全部为 `pass` 才能封装交付包。

### 4.6.4 `ready_for_operator_release` 收据与交付归档

四阶段通过后，封装不可变候选引用、部署配置、公开证据索引、逐规则结果、AcceptanceFact、回退材料和私有
证据归档清单。交付包不得复制秘密或未脱敏原始字节；私有证据按 §6.7 单独归档，并以逐文件路径、大小和
SHA-256 清单连接。`artifact_count` 只统计一份部署配置、逐份公开证据索引和逐份回退材料；私有归档内部
文件数由 `private_archive_manifest.entry_count` 独立计算，二者不得混计。

候选交付收据至少绑定：

- Campaign ID／用途、目标版本和 ProfileApprovalFact；
- Candidate ID、`candidate_frozen`、CandidateBuildReceipt、制品 inventory 与精确 OCI digest；
- CandidateEvidencePackage、VC-5 完成事件、AcceptanceFact 及其摘要；
- DMIT 环境冻结事实和四阶段 checkpoint；
- 交付包、公开索引、回退材料与私有归档清单摘要；
- `release_state=ready_for_operator_release`、Vircs 状态和收据自摘要。

先封存 `candidate_delivery_plan`，按顺序追加四阶段事实，再封存
`private_archive_manifest` 与 `candidate_delivery_package`；最后由 finalizer 复算并签发收据：

```bash
python3 -m tools.official_client_control candidate-delivery-record \
  --store /绝对路径/Campaign-control-store \
  --campaign <campaign-id> \
  --stage <candidate_active|rollback_verified|candidate_restored|stable_observed> \
  --input /绝对路径/candidate-delivery-stage-payload.json \
  --issued-at <阶段时间>

python3 -m tools.official_client_control candidate-delivery-finalize \
  --store /绝对路径/Campaign-control-store \
  --campaign <campaign-id> \
  --package-ref /绝对路径/candidate-delivery-package-ref.json
```

收据只能追加并通过 `receipt-replay` 独立重放，不得写成 DeploymentFact，也不得暗示用户管理的生产环境
已经验证或修改。finalizer 强制核对 VC-4 固定镜像、VC-5 完成事实、Acceptance、DMIT 主机／架构／
Compose／环境、真实回退镜像、观察窗、交付包、私有归档清单和 Vircs 未触碰状态。

### 4.6.5 生产权限边界与可选激活分支

只有 `campaign_purpose=production_replacement`、候选交付收据可重放，并且 P0 已冻结明确生产权限、
ActiveSupportEnvelope、RollbackOperationalEnvelope、DeploymentTrafficEnvelope、真实 Active／rollback、
生产主机、运行依赖和原子切换入口时，才允许按 Framework §5.6.2 进入生产激活。

生产 Release 与正式运行产物必须从已验收 Candidate 生成或晋升，并以 promotion／差异收据连接两种身份。
这不是重新构建 Candidate：不得改写 CandidateBuildReceipt、`candidate_frozen` 或候选镜像摘要，也不得把新
生产镜像冒充 VC-4 固定镜像。随后只能使用已冻结的正式生产产物执行隔离 canary、原子切换、真实回滚、
目标恢复和稳定观察，最终签发独立 DeploymentFact／activation receipt。

当前任务无 Vircs 生产权限，必须保持 `operator_managed／unverified／not_touched`，不得连接、修改、部署或
验证其 Sub2API 服务，也不得预签 DeploymentFact。Claude Candidate 是独立对象，不使用 Codex `previous`
selector、canonical-import、Catalog promotion 命令或 Kilo；以后由用户执行 Vircs 替换时，必须另开获授权
的生产执行链，不能把 DMIT 收据改名复用。

### 4.6.6 退出条件与失败恢复

候选交付终态固定为：

```text
VC-4 CandidateBuildReceipt 与 candidate_frozen 可复算
∧ VC-5 完成事件与 AcceptanceFact 可重放
∧ VC-5 规则／场景／门禁收据完整且未被重跑改写
∧ DMIT 运行镜像摘要 = VC-4 固定 Candidate 镜像摘要
∧ candidate_active → rollback_verified → candidate_restored → stable_observed 全部通过
∧ 候选交付包、回退材料和归档清单完整
∧ 候选交付收据可独立重放
∧ Vircs = operator_managed／unverified／not_touched
⇒ ready_for_operator_release
```

只有另行完成 §4.6.5 的正式产物、canary、生产切换、实际回滚、目标恢复和 DeploymentFact，才能声明
`production_active_upgraded`；`ready_for_operator_release`、DMIT 运行成功或历史 DeploymentFact 均不能
替代该结论。

恢复先执行本部分公共约定。AcceptanceFact 重放失败时停在交付前并返回 VC-5 定位，不盲目重跑完整矩阵；
DMIT 临时失败只从最近合法 checkpoint 补做 `failed／pending`，Candidate 身份漂移则返回 VC-4 建立新
Candidate。生产分支失败须立即恢复真实 Active 并保持 `production_unverified`。所有失败 attempt 和历史
FW-H／Vircs／DMIT 事实只读保留，归档与清理统一遵守 §6.7。

---

# 第五部分 Claude 兼容代码退休附加门禁

## 5.1 兼容代码退休

先执行 Framework §5.5.2，再补充以下 Claude 约束：

- 历史 FW-H 退休旧 OAuth 构造器、finalizer 和旁路的事实只读保留，不自动授权后继合并继续删除；
- 官方 Messages／count_tokens 保持 `migrated_strict`；第三方 Messages／count_tokens、Chat
  Completions 和 Responses 保持 `explicitly_retired + denied_before_oauth`；
- Setup Token 保持 `non_persona_managed`，API Key 与 Service Account 产品语义必须保留，
  Codex direct 继续按真实产品边界 `rerouted`；
- Claude Desktop、Claude Code for VS Code 和 CLI 只凭各自 OfficialIngressCatalog 条目准入，不能因
  删除兼容代码而互相继承 UA、Header、System 或工具目录；
- 任意未登记客户端、版本、协议或工具目录继续 fail-close，不得为退休旧路径重新开放第三方动态工具透传。

只有上述处置、active／rollback、官方正例、第三方负例、managed 出站和跨 Persona 隔离均通过，才可
签发 RemovalReceipt。

---

# 第六部分 Claude Code 取证、测试与生产环境

本部分记录当前稳定机器角色和安全边界，不构成 Claude Code 客户端规则证据。每次正式 Campaign
仍须在 manifest 中冻结实际主机、网络、镜像、工具和环境摘要；环境发生变化时按 Framework §3.3
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

## 6.7 证据归档与清理

DMIT 候选交付和 Vircs 生产边界见 §4.6；本节只规定私有证据的保存与清理。

| 私有材料 | 处理 |
|---|---|
| 原始 J、未脱敏 R、原始 P、MITM 与 pcap | 只进权限受限的私有归档；未脱敏 R 不离开采集机 |
| 官方发行物与提取 bundle | 只保存来源和摘要，不进公开仓库 |
| token、账号和 OAuth 凭据 | 不得归档或提交 |

私有归档必须包含逐文件路径、大小和 SHA-256 清单，并在另一存储位置完成解包复算和收据重放。
2.1.220 Campaign／run／提取物已被 FW-F fixture 内容寻址引用，引用有效期间不得删除。清理不得执行
`compose down` 或无范围 `prune`，也不得触及生产数据库、Redis、keeper、配置、当前／回滚镜像或唯一
证据副本。只有本地权威源码、正式发布镜像、production tree 与私有归档全部闭合，且删除清单不含
生产依赖时，才可按 dry-run 清单清理；任一条件不满足时，只停止空闲采集进程，不删除证据。
