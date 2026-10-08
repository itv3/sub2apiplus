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
> **当前状态**：当前规则证据仍为 2.1.226；升级控制面与真实执行／发布认证的完成度见 §3.6。
> 生产 Active 由 selector 与实际部署收据联合判定；旧 FW-H 运行坐标只供历史审计，不作为当前续跑入口。

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
| 当前等级 | 40 条公共 RequiredRules 在历史三模型出站 SupportEnvelope 内为 `verified`；新 Candidate 与新入站准入边界仍须独立取得本轮验收事实 |
| 批准用途 | 2.1.226 Release 已取得 `production_replacement_official_client_only` Approval；批准不等于当前部署，候选交付与生产激活按 §3.6、§4.6 分开判定 |

历史 Sonnet `verified` 结论由 [FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)
冻结；历史三模型结论由独立 ValidationCandidate、三模型 API／Desktop 正例、边界门禁及当时 DMIT
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
3. 对每个拟活动命题构造可达正负场景；按第六部分冻结本轮取证环境，故障语义在隔离 relay 注入；历史 2.1.226 的上游 run 位于当时 Vircs；
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

## 3.4 当前实现与历史证据边界

当前实现、历史验收和实际部署分别以对应事实判定：

| 层 | 当前事实与边界 |
|---|---|
| 画像与发布图 | 2.1.226 Profile、Wire、Snapshot、ReleaseArtifact／Bundle 已登记到 Claude Release Catalog；2.1.220 保留为同一 Schema／Compiler 下的历史 fixture。第二部分的规则与计数只描述这一证据范围 |
| 统一执行链 | 源码已接入共享 Persona Release Selector；PersonaPlanner、IdentityFacts、Compiler、authority、issuer 与状态仍属 Claude 方言。生产身份须按 §3.6 联合判定 |
| strict 入口 | 正向只有 `official-messages-oauth` 与 `official-count-tokens-oauth`，共享物理路由先执行 OfficialIngressCatalog 门禁；第三方与未知形态属于 OAuth 凭据前拒绝负例 |
| strict／managed 出站 | 八类 `persona_strict` 进入 Compiler／Executor／Guard；`non_persona_managed` 进入独立策略，未知 OAuth 出站保持 `denied` |
| 语义与兼容 | 只对 Catalog 已登记官方来源生成规范化语义；Persona system／identity 由批准事实派生，第三方兼容和工具桥不在当前 Claude Persona 范围 |
| 升级控制面 | 官方取证、分类、画像、候选 revision、候选采集／封存、compare／accept 与候选交付控制面已有入口；各阶段的当前操作见第四部分，完整升级认证状态见 §3.6、§4.0.5 |
| 候选与生产 | 当前候选环境为 ARM64 隔离环境；`ready_for_operator_release` 只证明候选交付。生产分支 B4-08 另批，不能以候选事实替代生产 DeploymentFact |

历史 Sonnet 与三模型 FW-G 继续证明各自批准范围内的 2.1.226 出站画像，公开凭证为
[FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)和
[三模型 FW-G 验收收据](egress/maintenance/claude-fw-g-three-model-acceptance.json)。FW-E～FW-H 的迁移条款、
旧机器职责和旧 official-client-only 续作记录集中到[历史审计文档](CLAUDE_CODE_CLIENT_EMULATION_HISTORY_AUDIT.md)；
它们不再承担当前运行入口或生产状态判据。

目标 stable 仍是本轮 Schema、Snapshot 和实现的设计权威：先完成目标证据与发现清零，再生成目标画像，
随后表达历史差分／conformance fixture；遗留 final wire 只供盘点和诊断。共享合同确有缺口时按 Framework
建立后继合同并复验跨 Persona 隔离，不能在 Claude 方言中绕过；`constants.go` 等仍服务 API Key 产品语义的
代码不得因退休 Claude OAuth 旧版本面而一并删除。

## 3.5 Claude 方言隔离、Guard 与策略

共享包依赖、共享／私有组件边界和 Guard 状态机见 Framework §2.5。Claude 只向闭集 wiring 注册自己的
Plan、IdentityFacts、ProfileSchema、DialectCompiler 和 adapter；不得读取或填充 Codex 方言字段。
`legacy_observe` 在 Claude 轨道中只允许 FW-E 对已冻结遗留路径报告事实，不能作为长期
`out_of_scope_passthrough` 或 strict 验收通过；版本、UA 和 Stainless 指纹只能存在于画像及加载器。

重试、隐私模式、缓存、辅助请求和 client 生命周期均由画像或显式策略声明；响应 SSE 属下游兼容，
不能反向影响请求定型。

## 3.6 当前可执行边界

Claude 的生产 Active 身份必须同时满足
[`claude_release_catalog.go`](../backend/internal/officialegress/claude_release_catalog.go)解析的
[`release-catalog.json`](../backend/internal/officialegress/catalogdata/claude/release-catalog.json)中
`production_active` selector 与实际生产部署收据一致。源码中存在 selector、历史 DeploymentFact、候选验收
通过均不能单独证明当前线上已激活；还须核验部署镜像 digest、主机、Release 和批准链。

2.1.226 的 FW-F Campaign `claude-code-2_1_226-fw-f-required-rules-v5-20260819` Store 停在
`profile_approved`。已封存官方证据、40 条 RequiredRules、106 条画像原子断言与 4 条客户端本地断言保持原
效力，可作后继回归夹具；相关发现清零与纠正事实见[历史审计文档](CLAUDE_CODE_CLIENT_EMULATION_HISTORY_AUDIT.md)。
历史导入默认只读，不因迁移工具就授予新版本正式证据资格。

本次工具升级的能力边界如下，源码入口存在与完整现场认证分别记录：

| 范围 | 当前边界 |
|---|---|
| VC-0～VC-4 | 受管入口、官方取证／恢复、分类批准、画像暂存和候选 revision／构建已接入；正式开工仍需 §4.0.5 的发布认证 |
| VC-5 | 候选采集与封存、compare、accept、评估基线及 evaluator-defect／candidate-source／approval-inputs 三类裁定已有实现；Mac 客户端收据已有离线准备、核验与导入合同，三官方客户端真账号采集及正式 recorder 仍未接通 |
| 封存后临时环境故障 | B4-05b 的 `ar<k>` 恢复段读写、对账与增量封存仍在实现／待验收；不能把封存前 resume 当作其替代入口 |
| VC-6 候选交付 | 四阶段、`dr<k>`、阶段失效与 finalize 控制面已接入；ARM64 四阶段真实交付执行器尚未完成，当前不能宣称已完成真实切换、回退或稳定观察 |
| 完整链与认证 | B5b-02 来源快照与 VC-0→VC-1 连续驱动源码已接入，仅限 `fixture_only／regression_fixture`；连续链、完整链与故障链仍须按实际串行执行结果验收，B5b-04 当前发布认证未签发 |
| 生产激活 | B4-08 另批；候选交付与生产激活的权限、收据及终态分开，见 §4.6 |

旧 §3.6.1 的 DMIT／Vircs 运行坐标已失去续跑资格，原记录完整保存在
[历史审计文档](CLAUDE_CODE_CLIENT_EMULATION_HISTORY_AUDIT.md)。当前环境与证据归档以第六部分为准。

---

# 第四部分 Claude Code 版本演进流程

Framework §5.3 是升级总操作入口并规定 `VC-0～VC-6` 顺序；本部分是 Claude 轨道的参数、证据和门禁
权威，只补充没有目标版本官方源码、依赖生产 bundle 逆向、隐私模式、OfficialIngressCatalog、模型能力
目录及专用机器职责的取证边界。历史 FW 阶段和工具状态只记录已发生的迁移事实，不得重定义 Framework
的通用状态语义。脚本参数、安装及实现细节统一见[驱动 README](../tools/arm64_capture_driver/README.md)；两个客户端
共用的控制面实现见[官方客户端升级控制面](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md)。

## VC-0～VC-6 执行导航

每阶段完成后先重放收据与 checkpoint，再推进下一阶段；脚本成功提示仅供定位，不能替代验收。下表为唯一阶段
入口表，每个阶段锚点直接落到详细章节；FW-E～FW-H 和具体版本状态仅为历史事实，不能代替这些阶段入口。

| 阶段 | 必备输入 | 首跑入口 | 完成标志 | 续跑入口 |
|---|---|---|---|---|
| [VC-0](#claude-vc-0) 冻结输入 | 目标／基线、用途、官方制品冻结、环境、预算和回退依据 | 开工 `entry-dryrun.sh … --opening`；新目标 `entry.sh`；同目标官方证据复用见 §4.0.4 | P0 收据、发布认证与 VC-0 checkpoint 可重放，Formal Campaign 已建立并派发 VC-1 首批；正式取证前 live 请求为零 | 重跑同一 `entry.sh` |
| [VC-1](#claude-vc-1) 目标取证 | VC-0、冻结的官方二进制、场景清单与证据标签 | VC-0 收口派发首批 `capture-official run`；随后 `vc1-claude.sh`（静态分析与 seal 预览）→ `vc1-claude.sh <review_sha256>`（seal 批准） | EvidencePackage v3 封存、Store `evidence_recorded` 与 VC-1 checkpoint | 有未收口预约：`reconcile-attempt` → 批准恢复预览 → `resume --rerun-failed`；否则 `reconcile-supervisor-run` 对账后按结论处理 |
| [VC-2](#claude-vc-2) 分类批准 | VC-1 封存、基线 RequiredRules | `vc-batch.sh` 派发 `classify` 草案、修订与批准批次 | `rule_classification_recorded`、清零收据与 VC-2 checkpoint；Store 仍为 `evidence_recorded` | 对账后按批次身份重派 |
| [VC-3](#claude-vc-3) 生成画像 | VC-2 批准、冻结的 Active 目录与画像 | `vc-batch.sh` 派发 `prepare-profile` 与 `stage-profile` 批次 | `official_sealed → profile_approved`、暂存 Release 与 VC-3 checkpoint；Active 未变 | 对账后重派；ProfileApprovalFact 之前只追加 revision |
| [VC-4](#claude-vc-4) 固定候选 | VC-3 暂存 Release、批准实现闭集 | ARM64 `vc4-requirements.sh` → 本机 `local-claude-candidate-chain.sh` → ARM64 `arm64-vc4-gates.sh` → `vc4-all.sh` | 同源构建收据、`candidate_sealed` 与当前 revision 的 VC-4 checkpoint | 重入 `vc4-all.sh`（上传中断用 `--resume-from upload-wait`）；候选真实变化先 `invalidate-candidate`，再 `revision-open --supersedes` |
| [VC-5](#claude-vc-5) 定向验收 | 固定候选与 VC-4 收据、VC-5 准入 | `vc5-all.sh` 按客户端派发采集、seal／compare、assert／accept；缺收据或批准时暂停 | AcceptanceFact、当前基线 VC-5 checkpoint；`validation_only／ready`、`not_activated` | 封存前对账→批准→授权→`vc5-claude-recover.sh`；封存后 `evaluation-recover`，完整 ar 链待验收 |
| [VC-6](#claude-vc-6) 交付／激活 | VC-5、交付环境／回退点；生产另需权限 | `deliver-candidate run`／批准版 `finalize` 分两批；ARM64 真实执行器待接 | 候选交付收据；validation_only 完成 VC-6，生产用途等 B4-08 | 对账后同 run 续 `dr<k>`；成功阶段先批准失效；finalize 幂等 |

主交接链固定为：

```text
P0 收据 → EvidencePackage → RuleMigrationLedger／AtomicAssertionLedger
        → rule_classification_recorded → EvidenceApprovalFact／ProfileApprovalFact
        → CandidateBuildReceipt／candidate_frozen → AcceptanceFact
        → 候选交付收据／DeploymentFact
```

## 第四部分公共执行约定（非独立阶段）

以下约定同时适用于 VC-0～VC-6，不产生独立阶段状态。各详细章节只补充 Claude 专用输入、动作和门禁；
恢复、暂停与对账机制以 [Framework §5.3.4](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md#534-失败恢复与身份边界)、
[§5.3.6](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md#536-共享控制面合同) 为唯一定义，本部分只登记 Claude 的参数与入口，不能用
历史 FW 工具或当前版本实例建立第二套恢复语义。

1. **身份与顺序**：VC-0 冻结 Campaign 总计划、阶段依赖、用途、预算和原始 deadline。后续批次在前序
   checkpoint 封存后编译，继承同一 Campaign 和账本；不能预填尚未产生的 Candidate、批准、attempt、镜像或
   收据摘要，批次启动后也不得补写。
2. **派发**：阶段动作只由父监督器 `campaign-run` 执行。VC-1 首批由 VC-0 收口在同一进程内派发，其余批次
   由 `claude_upgrade.py compile-and-run-vc-batch` 原子编译并派发；`capture-official`、`classify`、`accept`、
   `deliver-candidate` 等均为批次动作，不能绕过监督器直接执行，只读预览及有明确合同的控制入口除外。VC-0
   预检 `plan`、VC-0 收口、`import-predecessor-evidence` 和批次编译入口本身不进入阶段动作队列；
   `python3 -m tools.official_client_control` 只是 Store 的低级追加与重放入口，不是父监督器。
3. **恢复范围**：只执行失败、未完成或依赖变化的闭集；执行集合为空时在取得资源或产生副作用前写入
   `incremental-noop` 证明。承接须有完整来源、身份、环境、依赖与有效批准；文件存在、旧日志绿色或提交
   相同均不足以放行，工具不支持或证明不足时按原合同重跑。
4. **身份变化**：按 Framework §5.3.4 身份表处理，Claude 触发条件见 §4.7。候选源码或构建真实变化且画像
   不变时，同一 Campaign 开新 candidate revision；单纯改名、改路径或 build ID 不算真实变化。仅断言
   selector 的批准修正在原 Campaign 以 approval revision 零请求重评；仅控制面或 evaluator 变化只重跑离线
   门禁。
5. **完成、批准与暂停**：两个用途均须完成各自的 VC-6 终点。预演不代表批准，已有授权按精确输入形成
   凭证，输入漂移重新审核。出问题先暂停；永久停线仅按 Framework §5.3.6 判定完整性异常、未被合法演进
   承接的有效 wire／policy 等身份变化、账本已停线或已完成、官方已封存证据受污染或显式放弃。工具缺陷、
   预算、账务、环境与根因上限按 §4.7 放行后继续，不能用换 Campaign 重置累计。

**每轮参数只维护一份。** Claude 轮次与 Codex 共用同一套驱动参数文件格式与上下文工具：按
[env.example.sh](../tools/arm64_capture_driver/driver/env.example.sh) 中 Claude 的写法填写本轮参数（`CLIENT=claude`），
由驱动安全解析，不直接 `source`。Claude 参数文件只认通用键 `CLIENT_BIN`／`CLIENT_BIN_SHA256`／`CLIENT_ACCOUNT_ID`，
出现 Codex 专属键（Kilo 三项、`CODEX_*`、`TARGET_CODE_MODE_HOST_SHA256`）即拒绝；随客户端变化的模块、合同、
目录与文件名一律取驱动客户端表 `client_table.py`。`round_context.py` 从正式账本取 Campaign／Candidate／revision，
`phase_context.py` 取当前评估基线、读来源、写目标和唯一有效 attempt，`cleanup_context.py` 取收尾路径和收据，三者都按
`CLIENT` 取编排器。显式 ID 仅作断言，不按“最新目录”猜测，不复制上一轮路径。

**证据目录统一。** 本机升级证据、临时工作材料及归档统一置于
`/Users/czs/Developer/sub2apiplus-evidence/claude-code/<版本>/` 的版本／轮次子目录，与 `codex-cli/<版本>/`
同一规则，不散放在 `Developer` 根目录。服务器活动 Campaign 使用冻结的宿主数据根，不能以本机归档路径
替换。仓库 `local-analysis/` 下的 2.1.226 历史证据只读保留；迁移时保留完整 SHA-256 清单和旧→新路径
映射，历史收据原字节不改。

**Claude 新受管 Campaign 采用 Framework §5.3.6 的对账合同。** 中断后按未收口预约分流，先取得对账结论，
再按批准范围续跑；这项操作合同不代表 §4.0.5 的发布认证已经通过。失败矩阵、暂停放行、身份策略、批次和
驱动入口统一见 [§4.7](#claude-4-7)，不再使用历史 FW-H 检查点恢复新 Campaign。

用途、权限和状态是四个正交维度：

| 维度 | 取值与边界 |
|---|---|
| `campaign_purpose` | `validation_only／production_replacement`；VC-0 冻结后不得改写 |
| 批准与执行权限 | ProfileApprovalFact／SupportEnvelope 决定批准范围；P0 单独冻结主机与动作权限，候选交付权限不等于生产激活权限 |
| workflow checkpoint | `campaign_created → discovered → evidence_recorded → official_sealed → profile_approved → candidate_sealed → validation_only／ready`；生产分支再进入 `accepted_not_activated → active → restored_active` |
| production／delivery state | `not_activated／production_unverified／verified_active` 与 `not_ready_for_operator_release／ready_for_operator_release` 分开计算 |

因此 `ready／not_activated` 表示 checkpoint 与 production state 的组合，不是新枚举；
`ready_for_operator_release` 也不能替代 `production_active_upgraded`。

正式 Campaign 的不可变计划是两份同源对象，建立时逐项核对一致：

| 对象 | 冻结内容 |
|---|---|
| 编排器总计划 `official-client-upgrade-campaign/v1` | `client`、模式与用途、目标与基线版本、官方身份（冻结收据的 npm、原生分发、`binary_sha256` 与等价结论）、工具身份、冻结输入副本、取证坐标、控制收据、VC 控制与前序复用绑定 |
| Store Campaign `official-client-control-campaign/v2` | 目标版本、官方产物、平台、entrypoint、默认条件（含 `privacy=`）之外，另冻结 `client`、`campaign_purpose`、OAuth 组织、模型目录摘要、执行权限（主机与动作）、阶段依赖、阶段预算、绝对截止、监督参数、项目总账引用与七项工具身份 |

父监督器、事务和收据实现见[官方客户端升级控制面](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md)。
`claude_fw_e_complete_campaign.py`、`claude_fw_f_complete_runner.py` 与 FW-G 聚合 finalizer 只解释各自冻结的
2.1.226 历史实例，不是后继版本入口。

<a id="claude-vc-0"></a>
## 4.0 VC-0 冻结升级输入

- **输入**：目标版本与官方发行通道、当前 Active／Rollback 或明确的用户管理边界、隐私模式、entrypoint、
  平台、账号与模型条件、Campaign 用途、批准范围、执行权限、预算、证据复用决定和回退依据。
- **操作与工具**：`claude_upgrade.py freeze` 只读冻结官方制品；受监督部署受管工具与文档；开工空跑后由入口
  编排器 `entry.sh` 依次完成便宜检查、认证、入口门禁、演练、建账本、环境收据、预检 Campaign、Job 演练、
  启动探测、发布认证、P0 收据与 VC-0 原子收口。
- **产物**：官方冻结收据、P0 收据、发布认证、Campaign 总计划与 Store Campaign、计时账本、环境收据和 VC-1 首批。
- **完成标志**：身份、用途、批准范围与执行权限完整，工具阻断为零，正式取证前 live 请求为零，预算与回退
  依据有效，Formal Campaign 已建立并派发 VC-1 首批，用户管理的生产环境未被修改。
- **失败恢复**：
  - 失败层：环境、工具、批准输入或预算账务；Formal Campaign 建立前不产生 live 请求。
  - 对账入口：Formal Campaign 建立前没有父监督器，按入口步骤记录定位失败步骤；首批派发之后的中断走 VC-1
    的对账入口。
  - 续跑入口：修好后重跑同一 `entry.sh`，已通过且输入未变的步骤沿用，从失败的那一步接着做（§4.0.3）。

VC-0 只回答“本次升级是否具备安全开工条件”。本阶段可以查询官方发行元数据和执行只读环境检查，
但不得运行目标客户端产生 live 证据，不得定义目标 ProfileSchema／Snapshot，不得创建 Candidate，也不得
修改 Runtime Selector 或生产服务；这些工作分别从 VC-1、VC-3、VC-4 和 VC-6 开始。

先准备本轮参数、干净工具提交与 bundle、目标版本的官方冻结目录（数据根 `official/claude-code-<版本>/`）、
有效回退依据与批准预算，并按冻结的 `binary_sha256` 装好目标客户端（§4.0.3）。受管工具与文档用
`tools/arm64_supervised_deploy.py` 部署：交换共享受管根时两客户端都签部署收据，Claude 另覆盖
`tools/official_client_control`。随后按[驱动 README](../tools/arm64_capture_driver/README.md) 安装并复验驱动；部署
收据与实际执行副本一致后才开工。

新目标顺序固定：开工空跑 → `entry.sh` → VC-0 原子收口 → VC-1 首批。开工空跑
`entry-dryrun.sh start <bundle> <分支> <提交> --opening` 在数据根 staging 下的演练根与 `fixture_only` 演练总账里
执行，重新执行全集并审计读集，零真实请求，不写正式 Campaign，最远到 P0 收据；结论文件名带 `claude-` 前缀。
演练通过不代表承接已获批准。`entry.sh --plan` 只展示判定，`--from`／`--to` 指定起止步骤。

### 4.0.1 DOC-PRE 与 P0：冻结清单与离线验证

入口编排器负责以下步骤，操作员按输出处理阻塞，不逐项手填命令：

| 顺序 | 入口负责的工作 | 验收要点 |
|---|---|---|
| 1 | 便宜检查、策略兼容／激活认证 | 必填参数、派发守卫、部署身份、宿主与两个采集容器的目标客户端身份、官方冻结目录复核、`claude_upgrade.py plan --audit-only` 与环境试采一次报全；依赖失败项标阻塞 |
| 2 | 入口门禁与 pre-A3、零请求 smoke、atomic-double | 同一次门禁产出 P0 证据与 Claude pre-A3 认证；全部通过才建账本 |
| 3 | 计时账本、ARM64 环境收据、checkpoint、预检 Campaign | `preflight_only` 零请求建立，总计划与 Store Campaign 同源；账本创建后沿用，不重建计时 |
| 4 | 完整 Job 演练、客户端启动探测 | 宿主与两个采集容器的二进制、执行副本与步骤启动器可运行，失败生命周期探针通过；TUI 组合在本地替身上发出带口令的首个 `/v1/messages` 请求 |
| 5 | 发布认证、P0 finalize／replay | 当前部署、认证、门禁、环境和回退依据绑定一致 |
| 6 | 原子 VC-0 收口 | Formal 创建与 VC-1 首批派发由同一入口完成 |

客户端表登记为尚未接入的步骤执行即失败并写明原因，不以 Codex 的实现顶替，`entry.sh --plan` 同样列出。

P0 必须包含 `make test-capture-tools`、`make check-egress-spec` 的完整结论，`unexpected_skip=0`；两份证据取自入口
门禁那一次运行，subject 取 Claude 计时账本计划。测试跳过必须登记为 `approved_skip／unexpected_skip`。工具、
场景或证据标签残留旧版本硬编码，或必须依赖临时安装、修改宿主网络和生产配置才能运行时，均视为工具阻断。ARM64
后台运行使用 `setsid -f … < /dev/null`；`nohup` 继承的 SIGHUP 忽略状态会破坏挂断用例。

通用冻结、身份恢复、环境安全和时间控制分别以 Framework §5.3.1、§5.3.4、§5.1.4～§5.1.5 和
§5.3.5 为准。Claude Campaign 在 P0 中落盘的专用输入：

| 冻结面 | Claude P0 必须记录 |
|---|---|
| 目标与基线 | 目标版本、官方冻结收据（npm 平台包与原生分发两条来源的联合登记）、二进制 SHA 与平台；当前 Active／Rollback 的 Release、Profile、镜像和回退事实，或明确的 `operator_managed／unverified／not_touched` 边界 |
| Campaign 身份 | Campaign ID、`validation_only／production_replacement` 用途与终点、批准范围、生产执行权限、证据根、目标场景、entrypoint、隐私模式和受管工具版本 |
| 账号与模型 | OAuth 组织与非秘密账号标识、权限／scope、模型能力目录、精确别名、effort、feature、故障条件和场景矩阵；OAuth secret 不进入 P0 收据 |
| 执行环境 | §4.0.3 的 ARM64 核对项、OS／内核／架构、网络／代理／CA／DNS、端口、目录、容器、资源水位、构建链和生产隔离边界 |
| 证据策略 | 证据决定（重新取证或只读复用前序）、baseline 已封存证据及摘要、允许只读复用的事实、目标版本仍需补齐的事实、定向取证清单和禁止复用项 |
| 控制策略 | Campaign 总 deadline、阶段预算、同根因重试上限、资源水位、阶段交接窗口、回退点、结果键、直接依赖图、首批 `execute／reuse` 范围及 no-op 语义 |
| 工具身份 | Claude 身份策略（`claude_tool_identity_policy_v2.json`，`client: claude`）按多根受管清单计算的 `client` 与五摘要：`policy_sha256`、`wire_producer_sha256`、`evidence_semantics_sha256`、`control_sha256`、`tool_files_sha256`；清单覆盖 `tools/official_client_capture` 与 `tools/official_client_control` 两个根（条目键带根目录名，后缀含 `.mjs`），运行时依赖 node 与 TypeScript 登记在策略 `runtime_bindings` 并计入 `control_sha256`；另记 Claude 发布认证收据，受管文档随部署收据核对。Store 的 `tool_bundle_sha256` 只是 Store 自身摘要，不代表工具身份；整树摘要不直接作为失效判据，wire、evidence 与 policy 摘要是门禁（Framework §5.3.1 第 2 条）。目标 generation policy 在 VC-3 生成，VC-0 不预填 |

官方制品冻结用 `claude_upgrade.py freeze --output-root <此前不存在的绝对路径>`（缺省 `--dist-tag stable`、
`--platform linux/arm64`）。它是离线工具，不依赖采集容器里的二进制、不发模型请求；产物先写同级暂存目录、完成后
整体改名，收据 `freeze.json` 为 `claude-code-fw-e-official-freeze/v2`。登记与校验顺序：

1. 版本只取 npm 主包 `dist-tags.stable` 的精确三段版本；原生分发的 `latest`／`stable` 端点只记录、用于核对，不参与
   选版本；冻结结束时复查 npm 标签，期间漂移即作废本轮；
2. npm 渠道：主包与平台包 `@anthropic-ai/claude-code-linux-arm64` 核 sha512 SRI、sha1 与 Registry 签名，主包
   `optionalDependencies` 精确绑定平台包，从平台包解出二进制；
3. 原生分发渠道：同一精确版本的 `manifest.json` 取平台 `linux-arm64` 的 checksum 与 size，另记 commit、buildDate
   与清单签名执行状态，下载同版本二进制核对 sha256 与字节数；
4. 两份二进制都必须是 aarch64 ELF64，按 `PT_INTERP` 区分 glibc 与 musl，`linux/arm64` 只接受 glibc；
5. 两渠道二进制逐字节比较：相同则登记唯一的 `binary_sha256` 并绑定两渠道证明；不同则两份都存、`binary_equivalence`
   记 `divergent`、`binary_sha256` 为空，冻结退出码 2，VC-0 不得据此建立 Formal Campaign。

冻结目录放到数据根 `official/claude-code-<版本>/`。入口便宜检查的 `official-package` 一项与 `plan --audit-only` 只读
复核它（`claude_upgrade_freeze verify`）：收据合同与平台、目标版本、两渠道等价、`binary_sha256` 等于本轮
`CLIENT_BIN_SHA256`，以及目录里每个登记文件的摘要与字节数。冻结只证明官方产物身份；entrypoint、隐私模式、账号
与模型条件由 VC-0 总计划与 Store Campaign 冻结。`claude_fw_e.py freeze`（v1，`linux/amd64` 与 `darwin/arm64`）只读
保留，只解释 2.1.226 的历史冻结。

Campaign、Candidate 和 attempt 的身份边界见 Framework §3.3、§5.1。Claude P0 另冻结以下身份维度：

| 身份维度 | 冻结要求 |
|---|---|
| 官方产物 | 官方冻结收据、两渠道证明、二进制 SHA 与平台联合判定，任一变化均为新目标身份 |
| 隐私模式 | `essential-traffic`／`no-telemetry`／`default` 不得混用；复用证据必须与目标模式一致 |
| entrypoint | `sdk-cli` 与真实 TTY 的 `cli` 分开取证；`-p` 不能伪造 TUI，Desktop 与 VS Code 也不能继承 CLI 身份 |
| 平台 | Linux arm64 是当前官方客户端平台；x86_64 证据只读作历史 baseline，不能证明 arm64 目标 |
| 账号与模型 | OAuth 组织、权限、scope、模型目录、精确别名、effort 和可见性共同构成证据条件，不能在验收后追认 |

### 4.0.2 用途

每个 Campaign 必须在 P0 选择一个用途，进入正式 Campaign 后不得更换：

| 用途 | VC-6 终点 |
|---|---|
| `validation_only` | 交付已验收候选并停在 `ready_for_operator_release`；不改变或声称验证用户管理的生产环境 |
| `production_replacement` | 先达到 `ready_for_operator_release`；具备明确生产权限、可复算 Active／Rollback 和有效回退点后，继续 canary、切流、实际回滚与目标恢复 |

Campaign 用途描述最终目标，执行权限描述本次操作可以推进到哪里，两者不得互相替代。Store Campaign 冻结的执行
权限是主机 `arm64` 与动作 `official_capture`、`candidate_build`、`candidate_capture`，不含生产动作；执行权限只覆盖
候选交付时，可以推进到 `ready_for_operator_release` 后交给用户，但不得改写用途或声明 VC-6 的生产分支完成。生产
操作必须取得独立授权并从 VC-0 重新冻结生产输入。Claude 轮次参数用 `CAMPAIGN_PURPOSE` 选择用途，
可取 `validation_only` 或 `production_replacement`，缺省为后者；入口编排器建计时账本、预检 Campaign 与执行
`plan --audit-only` 时使用同一取值，Formal 收口及后续阶段从冻结的总计划读取。用途、账号／模型能力和证据
语义在 VC-0 冻结，验收后不能追认或修改。

Discovery、Evidence、Approval、Candidate、Acceptance 和 Deployment 均是后续阶段的追加事实，VC-0 不得
预填其 ID、摘要或状态。2.1.226 的当前进度与恢复检查点只见 §3.6，不写入新版本 VC-0 冻结清单。

### 4.0.3 ARM64 环境

官方取证、各类门禁、候选构建与候选验收在 ARM64 完成；本机负责权威源码、候选提交链与三个官方客户端的本机正例
（机器职责见 §6.2）。

| 必须核对 | 要求 |
|---|---|
| 数据和权限 | 宿主 `/root/docker/capture-cli/data`，与 Codex 同一数据根；活动数据只放其下，目录 `0700`、文件 `0600`；禁止向 `/root` 解包 |
| 目标客户端 | 宿主 `/opt/claude-<v>/claude`（实体目录）与数据根 `runtime/claude-<v>`（只读挂载到 `capture-cli`、`capture-cli-synthetic` 的 `/opt/claude-<v>`），三处摘要等于冻结的 `binary_sha256`，容器内 `--version` 自报目标版本 |
| 镜像与依赖 | 断网容器 `capture-cli-synthetic` 与 `capture-cli` 同镜像，`network_mode: none`，挂载逐项相同（去掉 Docker 控制面），逐容器文件不共用来源；受管运行时 `runtime/claude-tooling` 只读挂载到 `/opt/claude-tooling`，node 与 TypeScript 的摘要等于身份策略 `runtime_bindings` |
| 出口 | 读取受限运维策略，`capture-cli` 与 `sub2apiplus` 两端保护通过实时准入；断网容器不在出口准入里；禁止自动学习出口、回退直连或改网络迁就检查 |
| 凭据 | 正式取证要求两个采集容器都有 Claude OAuth 凭据（容器内 `/root/.claude`，数据根 `state/claude`）、CA 与全局状态文件，且两边全局状态内容相同；Job 演练与启动探测不读凭据内容，未登录时也能通过 |
| 资源 | 根盘已用 ≤69%、可用 ≥30 GiB；驱动派发前可用 ≥40 GiB（参数 `MIN_FREE_GIB` 只能调高），与 Codex 同一条停线。清理只针对未被收据引用的缓存和临时产物 |

网络配置、接口与切换步骤见[运行时出口运维说明](egress/runtime-egress-operations.md)，本节不重复具体拓扑。环境
producer `claude_upgrade_arm64_environment_receipt` 按当前出口策略、boot ID、租期、两个采集容器与目标客户端二进制
核验。TLS 就绪由容器内 curl 对 `https://api.anthropic.com/api/hello` 发无凭据 `HEAD`（期望 200）证明；Claude 二进制
自身 TLS 栈经当前出口的就绪性没有无凭据可观察手段，收据不伪造这一项。旧环境收据不能替代当前准入；经批准的出口
策略变更重新准入即可。

建账本后的失败重跑同一 `entry.sh`：完整步骤按绑定承接，半成品使用新坐标（`-r2`、`-r3` …）并保留原记录。
Formal 尚未建立时，便宜检查与启动探测每次重新执行，入口门禁每次重核当前上下文、承接凭证时效及撤销状态，
不能以旧步骤记录跳过本轮准入。受控逐单元承接时，只重跑按实际输入声明失效或没有有效记录的单元；没有合法
承接批准时重新执行全集，不能手工缩小输入范围。Job 演练上次失败且留下收据时只重跑失败的作业。
Formal 已建后创建链冻结，只补收口；已派发首批不得再次派发，转对账恢复。

### 4.0.4 退出条件与建 Campaign

VC-0 的退出条件固定为：

```text
P0 收据通过
∧ 目标、基线、Campaign 用途、批准范围、执行权限、账号与模型条件完整
∧ 官方冻结两渠道逐字节相同
∧ 工具阻断 = 0
∧ unexpected_skip = 0
∧ live_request_count = 0
∧ 发布认证、两端保护、指定出口、目录与资源有效
∧ 回退依据有效
∧ 用户管理的生产环境未变化
⇒ VC-0 收口建立 Formal Campaign 并派发 VC-1 首批
```

- **新目标**：`entry.sh` 最后一步调用 `claude_upgrade_vc0_closeout`，先只读判定现场。Formal 未建时重放预检
  Campaign、ARM64 环境收据、Claude 部署收据、Claude 发布认证与 P0 收据，要求计时账本处于 active VC-0 且剩余至少
  300 秒，向统一调度执行器申请整机后，在同一进程内建立 Formal（总计划与 Store Campaign v2 同源，参数全部从预检
  Campaign 恢复）并派发 VC-1 首批；Formal 已建未派发时只补缺的事件并派发首批；首批已派发时不再派发，交给 VC-1 的
  对账恢复链。需要批准时（同根因到上限后的账本恢复、首批恢复预览）这一步退出 3 并打印下一条命令，批准后带
  `--approve-sha256`／`--approved-by`（上限后的恢复另带 `--reason`）重跑同一 `entry.sh`。命令行
  `claude_upgrade.py plan --campaign-mode formal` 一律拒绝；演练根里不做收口。
- **同目标可信官方证据复用**：参数文件写 `EVIDENCE_DECISION=reuse` 与前序 Store、Campaign
  （`PREDECESSOR_STORE`、`PREDECESSOR_STORE_CAMPAIGN`），走同一条 `entry.sh`：计时账本以 `reuse` 建立，预检 plan 在
  总计划冻结 `predecessor`（只冻结前序 Campaign 身份，不冻结 Store 位置），收口建 Formal 后首批是零请求 no-op（复用
  项为全部 official Job）。随后用 `claude_upgrade.py import-predecessor-evidence --mode read_only_reuse` 导入前序的
  VC-1 官方证据——七项身份全部可证且逐项相等才签发导入收据——再按 §4.1.2 派发 VC-1 预览批次，seal 在同一批次里
  零请求完成，并补齐 VC-0 完成、VC-1 开始与完成的计时事件（已登记的跳过）。复用决定只能在 VC-0 建 Campaign 时
  冻结，不能建完再挑；`--mode regression_fixture` 只登记回归夹具，不授予任何正式引用权限。

目标、基线、用途、账号权限、模型可见性或官方产物身份变化时，当前冻结结果失效，必须从 VC-0 建立
新的 Campaign 身份。工具、Schema、场景、网络、目录、资源、预算或回退能力存在缺口时，不得启动正式
Campaign；先记录最后合法检查点、根因和唯一下一动作，再独立修复并重跑受影响的入口步骤。全部条件通过
后应立即收口，不得以准备工作为由停留在 VC-0。

### 4.0.5 受管工具发布认证

认证链：策略兼容（策略版本变化时）→ 当前部署的策略激活 → pre-A3 → 发布认证 issue／verify，四个入口都带
`--client claude`。兼容收据与激活认证与 Codex 同一合同，身份按 Claude v3 引擎计算；pre-A3 签
`pre-a3-path-certification/v3`，缺省坐标为 `control/policy-certification/claude-pre-a3-path-certification-<stamp>.json`；
发布认证签 `tool-release-certification/v2`，绑定 Claude 部署收据的五摘要、pre-A3 认证、完整 Job 演练
（`official-client-job-rehearsal-receipt/v1`，含失败生命周期探针）与 atomic-double 收据，只能在具备 Go 与 Docker
的 Linux ARM64 上签发。按客户端分组的实现见[官方客户端升级控制面](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md)。

Claude 的 pre-A3 按注册表执行全部已登记真实链，单元 ID 为 `pre-a3:claude:<场景>`，与 Codex 单元同一记录库、互不承接。
发布准入另要求下列八条必需链全部齐备；尚未登记、未执行、失败或跳过的必需链均阻止签发，不能以已登记子集通过替代完整覆盖：

| 真实链 | 覆盖 |
|---|---|
| `vc-chain.claude-reconcile` | 首批采集失败 → `reconcile-attempt` → 批准恢复预览 → `resume` 只重跑失败 Job；无预约时 `reconcile-supervisor-run` → N+1 重派 |
| `vc-chain.claude-deadline` | 阶段预算到期两本账暂停 → `deadline-extend` 预览与批准（两本账之间被强杀后补齐收敛）→ 原 Campaign 续跑 |
| `vc-chain.claude-root-cause` | 同根因累计达冻结上限只暂停、门禁只挡该根因 → 修复收据放行 → 续跑 |
| `vc-chain.claude-tool-evolution` | 部署新工具 → 未登记即拒绝写入 → 登记演进 → 有效身份更新 → Store finalizer 通过 → 旧收据按形成期重放 |
| `vc-chain.claude-vc1-capture` | 2.1.226 历史来源保持原身份与原字节，经真实 VC-0 收口、监督复制、VC-1 seal 与 checkpoint；源码存在不等于该链已认证 |
| `vc-chain.claude-full-validation-only` | VC-0～VC-6 的 `validation_only` 连续控制链；须独立执行并核对终态，不能拼接局部通过记录 |
| `vc-chain.claude-recovery-chain` | 批次／官方取证／多根部署／前序导入故障链，追加候选对账、来源复用与演进场景；完整收口由 B5b-03 验收 |
| `vc-chain.claude-late-stage-faults` | 候选及交付后段的故障与恢复链；须具备对应完整执行结果，不能用成功主链代替 |

这些链零请求，在两个受管根的副本树上运行，Linux root 上进入 `unshare -m -n` 私有命名空间（临时根 `/var/tmp`）；
登记的真实链必须完整执行，失败、缺失或 skip 均拒签。只改 `tools/official_client_control` 时 Claude 场景单元失效
重跑，Codex 场景单元不受影响。跨部署沿用 pre-A3 时由 `record-reuse` 登记复用收据并纳入发布认证，无有效证明就
重跑。旧认证按冻结合同只读重放，不追改历史字节。

Claude 的 atomic-double 由同一共享收据模块加 `--client claude` 执行，在 `capture-cli` 里两个独立新根中各建一个合成
Formal Campaign，经原子入口完成一次 VC-0→VC-1 与失败父 run 收口及四个负例，零请求，签
`official-client-atomic-vc0-vc1-rehearsal/v1`；发布认证按 Claude 合同重放它，容器内重放追加 `--client claude`。

Claude 发布认证：**待认证**。上述入口、源码与离线合同不等于已经签发认证；八条必需链的实际执行结果、B5b-03
后段故障链及 B5b-04 的当前部署 `certify_release issue --client claude`／`verify` 全部通过后，才能更新此结论。
录制链中的发布循环前置与静态控制投影是明确标记的测试夹具，不能反向证明八链已通过；局部 VC-0→VC-1
通过也不能证明 VC-0～VC-6 全链通过。Mac 真账号采集与 ARM64 真实交付执行器的现场验收仍须分别完成。
本次文档同步只登记结构与实际入口，不补签认证或把旧 pre-A3 收据视为当前发布证明。

<a id="claude-vc-1"></a>
## 4.1 VC-1 收集目标证据

- **输入**：VC-0 收据与 Formal Campaign 身份、冻结的官方二进制与冻结目录、证据复用决定、历史候选基线、
  目标场景清单（含模型能力作业）、证据标签与生产现状清单。
- **操作与工具**：首批 `capture-official run` 按冻结场景清单取证；之后的 VC-1 批次先做静态分析 `analyze-target`
  （Bun 模块图、多模块 AST、目标 SinkInventory 与 sink containment），再经 `capture-official seal` 预览、审核与
  批准封存；只读复用前序时零请求封存。
- **产物**：attempt 收据、P／R／J／M、静态分析收据、CaptureIndex、SinkInventory、DiscoveryInventory、语义候选与
  候选分母、跨来源矩阵、EvidencePackage v3、`official/result.json` 与 VC-1 checkpoint。
- **完成标志**：目标身份完整，目标发现无截断，证据逐项可定位、可解析、可复算，安全与环境恢复门禁
  全部通过，Store 到 `evidence_recorded`。
- **失败恢复**：
  - 失败层：环境、工具或预算账务；目标、账号权限或官方产物身份变化按 Framework §5.3.4 返回 VC-0。
  - 对账入口：有尚未收口的采集预约用 `reconcile-attempt`，否则用 `reconcile-supervisor-run`。
  - 续跑入口：批准零请求恢复预览后，`resume --rerun-failed --recovery-preview` 只补失败或未完成的 Job；静态
    分析与 seal 批次失败先对账，按对账结论处理；已可信封存的官方请求不得重发。

VC-1 只回答“目标版本实际会产生什么行为”。本阶段不判定相对基线的最终迁移类型，不生成目标画像，
也不签发 ProfileApprovalFact；`inherit/change/condition_change/add/delete`、最终发现处置和
AtomicAssertionLedger 均在 VC-2 完成。

### 4.1.1 证据来源与复用判定

先按 VC-0 冻结的证据决定逐项判定，不能把“已有文件”直接解释为“可以复用”：

| 情况 | VC-1 动作 |
|---|---|
| 同版本、同官方产物／平台／entrypoint／隐私模式／账号权限／模型可见性，且证据通道与语义均未变化 | 只读引用已封存证据及其摘要，不复制、不覆盖，也不重发官方请求 |
| 可信证据只缺少某项目标事实 | 只对该事实、触发条件和直接依赖场景定向取证，其余证据继续只读复用 |
| 目标、基线、Campaign 用途、官方产物、平台、entrypoint、隐私模式、账号权限或模型可见性变化 | 停止当前 Campaign，返回 VC-0 重新冻结身份和证据策略 |
| 仅报告、控制面、evaluator 或 Candidate 变化，目标证据语义未变 | 只重跑受影响的离线派生与门禁，不得据此重发官方请求 |
| 采集、relay 或其他产出侧工具改变证据字节（wire producer 变化） | 官方未封存时在原 Campaign 以 `tool-evolution` 登记演进：预览按作业声明的工具依赖算出受影响 Job（编排器 wire 闭包变化或变化映射不到作业时全部受影响）并逐作业作废，`resume` 只重采受影响 Job；官方已封存后仍有 Job 受影响即拒绝登记，Campaign 内不再重采官方证据 |
| 提取、脱敏或其他证据处理工具改变证据解释（evidence semantics 变化） | 原 Campaign 以 `tool-evolution` 登记演进并按评估纪元重评，只重跑离线处理，原始字节不重采 |

同目标只读复用经 `import-predecessor-evidence --mode read_only_reuse` 进入本 Campaign（§4.0.4）：七项身份（目标
版本、官方产物、平台、entrypoint、隐私模式、OAuth 组织、模型目录摘要）全部可证且逐项相等才签发导入收据，导入的
EvidencePackage 才获正式引用权限；`regression_fixture` 收据只供测试与回放，不授予正式引用。纯复用批次必须记录
`live_request_count=0`；工具、报告或 Candidate 变化本身不是官方重采理由。

目标事实仍取下列五类输入的并集，但只有目标原生发现和同身份目标运行证据能够成为当前版本权威：

| 输入 | 每版动作 | 权威边界 |
|---|---|---|
| 目标原生发现 | 全量 AST／词法发现并与全 host／path 运行 inventory 对照 | 目标版本的静态与运行权威 |
| 前一批准 stable | 比较规则、条件、sink 和最小运行哨兵 | 只提供迁移候选和复用来源 |
| 2.1.220 官方材料 | 重放历史规则与 baseline fixture | 只作差分和探针设计 |
| 2.1.88 真源码 | 重放已冻结的 102 个源码机制候选 | 只作机制线索 |
| HitCC 2.1.197 | 重放已冻结的 71 个线索 | 只作发送面和分支线索 |

历史资料的提取结果冻结为受管根内的历史候选基线 `claude_fw_e_historical_candidate_baseline_v1.json`：它从 2.1.226 的
语义闭合与完整 Campaign 产物只读生成，带提取收据（输入产物与提取工具的摘要、2.1.88 与 HitCC 原始目录摘要、语义
候选目录摘要），`claude_upgrade_discovery.py freeze-baseline` 可逐字节复现，属于 Claude 身份的 evidence 层。seal 逐项
重放这份基线，不重扫 2.1.88 的 `src／node_modules／vendor`、不逐篇重提 HitCC；后继 Campaign 必须重放全部稳定 ID，
历史处置不复制成新版本结论，旧资料也不能单独支持任何活动规则。

仅在原始摘要／引用无法复算、提取能力或覆盖合同实质变化、目标出现无法解释的新网络机制、目标证据与
历史候选冲突／闭集缺口，或候选粒度不足以唯一映射时，才全量重审原始资料。重审必须建立新的内容寻址
基线和追加式收据，记录触发原因、摘要、ID 迁移和覆盖差异；旧基线不得覆盖。

### 4.1.2 Bundle 分析、抓包与封存

Claude 没有可审计的目标版本未压缩源码，以官方生产 bundle 和目标运行证据为主。VC-1 的动作全部由父监督器
`campaign-run` 派发，是受监督动作；参数取自 Formal 总计划冻结的输入（场景清单、证据标签、静态基线、生产现状
清单）与取证坐标，不现场补写：

| 批次与动作 | 做什么 | 失败层与续跑 |
|---|---|---|
| 首批 `capture-official`（VC-0 收口派发） | `capture-official run --acknowledge-live-requests` 按冻结场景清单逐 Job 取证 | 环境、工具或预算账务；有未收口预约走 `reconcile-attempt` → 批准恢复预览 → `resume --rerun-failed --recovery-preview`，否则 `reconcile-supervisor-run` 后按提示以 N+1 批次重派 |
| 预览批次 `vc1-01-analyze-target` | `analyze-target`：对冻结的官方二进制做 bundle 分析与 sink containment，产物在 `official/static-analysis/` | 工具；`reconcile-supervisor-run` 对账后按结论处理；规范产物已存在时只重放收据，不重复分析 |
| 预览批次 `vc1-02-seal-preview` | `capture-official seal` 预览：生成封存草稿、唯一一次深度扫描，给出 `review_sha256`；停在等批准是批次内的合法暂停点 | 工具或证据标签；`reconcile-supervisor-run` 对账后按结论处理；工具演进或输入变化得到新代号，旧代预览不能被批准 |
| 批准批次 `vc1-03-seal-approve` | 带审核过的 `review_sha256` 批准：Store 幂等追加到 `evidence_recorded`，写阶段结果与 VC-1 checkpoint | 工具；`reconcile-supervisor-run` 对账后按结论处理；同一批准命令重跑收敛，Store 事实数不增 |

首批完成、官方 attempt 处于 `awaiting_receipts` 之后，在采集主机运行 `vc1-claude.sh` 派发预览批次；审核
`claude_vc1.py summary` 打印的预览（闭合状态与 `review_sha256`）后，运行 `vc1-claude.sh <review_sha256>` 派发批准批次。
发现项需要人工处置时用环境变量 `DISCOVERY_DISPOSITIONS` 给出 Campaign 内的处置文件，预览与批准给同一份。只读复用
前序的 Campaign 没有采集 attempt，预览批次里 seal 直接零请求完成。封存后 `account-sealed-official` 把官方阶段的
模型请求写入项目总账（零请求、按 attempt 幂等）。

**取证 Job。** 每个 Job 的每一步都是 `docker exec <容器> python3 <工具根>/claude_fw_e_relay.py capture --execute …`：
中继在容器内完成 hosts override、leaf cert、relay 与客户端运行，经 Campaign 租约的独立监督器执行。预约之前依次核对
模式、污染标记、是否已封存、工具身份、冻结副本、坐标布局、执行树、宿主与两个采集容器里的二进制、凭据与 CA、预算；
原子预约之后每一步在宿主签发运行收据，并核对容器内逐字可见。真实上游步骤只在 `capture-cli` 执行，合成步骤只在
断网容器 `capture-cli-synthetic` 执行（plan 时钉定，执行时按冻结的上游 IP 复核）。每个 Job 前后对两个采集容器采
探针，快照漂移或残留取证进程即判环境污染、不补跑、停止后续 Job；失败作业按冻结上限补跑，失败证据归档为
`.failed-attempt<N>`；每个 Job 终态做一次证据权限收口并写 Job 闭环收据与 checkpoint，run 末再收口一次，写只写一次的
`attempt.json`。模型能力作业以“<模型>-<probe>”命名，逐模型取证。

**静态分析。** `analyze-target` 用 `claude_fw_e.analyze_bundles` 提取 Bun 模块图（入口按模块间的加载关系确认，入口
关系未决的模块、动态加载边与 sink 单列为未决项），对全部 JS 模块做 AST 与可达性分析并合并为目标 SinkInventory，
与 Campaign 冻结的 2.1.220 词法基线做静态差异，再用 `claude_sink_containment.mjs` 给出逐 sink 的 AST 包含关系证据。
node 与 TypeScript 是受管运行时依赖：取宿主数据根 `runtime/claude-tooling/` 下的两件（采集容器内挂载为
`/opt/claude-tooling`），摘要必须等于 Claude 身份策略 `runtime_bindings` 的登记值；默认路径缺失即失败，不在 `PATH`
里另找。在本机单独运行 `claude_fw_e.py analyze-bundles` 必须显式给出 `--node-binary` 与 `--typescript-module`，
TypeScript 无论来源都对照锁定摘要。产物先写临时目录、完成后整体改名，收据 `claude-upgrade-static-analysis/v1` 绑定
冻结收据、基线、运行时与全部产物。

**封存。** seal 先写客户端身份 checkpoint，再生成草稿：按证据标签逐连接匹配中继的有效连接得到 capture manifest，
逐 Job／probe 复核中继清单得到 CaptureIndex，从目标 SinkInventory、入口未决项与历史候选基线生成 DiscoveryInventory、
语义候选与正交候选分母，再生成跨来源矩阵；预览时唯一一次深度扫描，写证据清单、封存草稿与预览。闭合门禁有四种阻断
原因——CaptureIndex 复核失败、标签错位、发现项未分类、规则缺口——任一存在都不能批准；闭合只表示来源覆盖和证据
缺口已闭合，不等于已经完成 VC-2 的规则迁移分类。批准是幂等写入：被强杀后重跑同一命令收敛，Store 事实数不增。
取证、封存与恢复的实现落点见[官方客户端升级控制面](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md)第 11 节。

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

### 4.1.3 目标事实整理与退出

VC-1 的跨来源产物只保存事实、来源和待审查候选，不形成最终规则结论：

| 层 | 内容与阶段边界 |
|---|---|
| `SinkInventory` | 无截断保存目标发送点、所在模块、transport、host／path 和触发条件，并与运行 inventory 对照；入口关系未决的模块、动态加载边与 sink 单列 |
| `DiscoveryInventory` | 目标 SinkInventory 的每个 sink、入口未决项，以及历史候选基线逐项重放的历史原子命题、clue 和 Markdown 原子；发现项不能直接生成规则 |
| `SemanticRuleCandidate` | 32 个稳定 `CAND-*` 族一律列出，以 `source_ids` 多对一归并并与发现项双向闭合，只作为 VC-2 输入；`observed／blocked` 均不是生产规则 |
| 正交候选分母 | 目标发送点、语义候选族与三组历史候选共五组，由输入计算、不写死，供 VC-2 逐项处置 |
| 跨来源矩阵 | 逐条必需规则绑定覆盖它的场景、probe 与证据项，缺证据即缺口；只证明来源覆盖，不签发迁移结论 |
| `EvidencePackage v3` | 每个 probe 一个证据项（`EVIDENCE-<probe>`），只含 `source_ids`、外部证据绑定和适用条件，不含规则、证据等级或迁移结论 |

一个发现可以关联多个语义候选，但来源引用必须双向闭合。DiscoveryInventory、语义候选、候选分母与生产现状清单都
绑定进完整性清单，VC-2 直接从 VC-1 读取。`claude_fw_e_seal_plan.py`、`claude_fw_e.py rule-assessments` 与
`claude_fw_e_crosswalk.py` 只解释 2.1.226 的历史输入；历史输入里的 `migration_decision`、证据等级、生命周期和兼容
类别不得进入 EvidencePackage v3，也不得自动写入 RuleMigrationLedger、AtomicAssertionLedger、Profile 或生产清单。
seal 只封存 `official-client-evidence-package/v3`，不签发 Evidence／Profile ApprovalFact。

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
证据继续只读复用。临时网络失败只建立新 attempt；目标身份变化时建立新 Campaign；产出侧工具变化按 §4.1.1 的
复用判定登记演进；仅控制面、报告或 evaluator 工具变化时只重跑受影响的离线门禁。各种情况均不得覆盖历史事实。

<a id="claude-vc-2"></a>
## 4.2 VC-2 逐规则判定差异

- **输入**：VC-1 封存的目标身份、SinkInventory、DiscoveryInventory、语义候选与候选分母、跨来源矩阵、
  生产现状清单、EvidencePackage，以及当前基线 RequiredRules 和 Campaign 用途。
- **操作与工具**：`classify` 在全离线条件下生成机器草案，审核人修订后批准，三种调用都是父监督器派发的 VC-2
  批次动作：逐项完成 discovery／candidate 终态处置，判定 `inherit/change/condition_change/add/delete`，生成原子
  断言并按实现责任归并为目标 RequiredRules。
- **产物**：`classification/draft/<rev>/` 下的六个文件（目标 RequiredRules manifest、RuleMigrationLedger、
  AtomicAssertionLedger、DiscoveryDispositionLedger、场景清单与画像补丁计划）、批准副本、清零收据
  `classification/result.json`、`rule_classification_recorded` 事实与 VC-2 checkpoint。
- **完成标志**：发现、候选、迁移、原子断言及其归属全部闭合；缺失、重复、孤儿引用、循环引用、
  `blocked` 和其他未决项均为零。
- **失败恢复**：
  - 失败层：工具或批准输入；缺少目标事实属于 VC-1 证据缺口。
  - 对账入口：`reconcile-supervisor-run`。
  - 续跑入口：对账后按批次身份重派；事实不足返回 VC-1 定向补证；已封存分类或规则映射变化按
    Framework §5.3.4 建立后继 Campaign，从 VC-2 继续并只读复用仍有效的官方证据。

VC-2 只回答“目标版本的每项出站责任相对基线如何变化”。本阶段不生成 Profile、Release 或 Candidate，
不修改 SupportEnvelope 或生产 selector，也不发送任何官方或 candidate 请求。

### 4.2.1 执行步骤与阶段产物

VC-2 的三种调用都由 `vc-batch.sh <NEW> <IN> VC-2 <序号> VC-1 <计划>` 派发为 VC-2 批次（动作计划用
`claude_upgrade_batch.action_plan` 生成，命令是受管 Python 直接调 `claude_upgrade.py`），全程离线零请求：

1. **机器草案**（`classify --campaign-dir <C>`）：输入一律取 VC-1 封存——VC-1 checkpoint 逐级重放到 VC-0，Store
   检查点必须是 `evidence_recorded`、有效工具身份等于当前受管代码，DiscoveryInventory、候选分母、CaptureIndex、
   跨来源矩阵、目标 SinkInventory、场景清单与生产现状清单都经 EvidencePackage 的完整性清单取得并逐个核摘要；基线
   取总计划冻结的 `inputs.baseline_rules`。在 `classification/draft/rev-NNNN/` 写只写一次的 revision：每条基线规则的
   迁移决定与证据等级都是 `blocked`，每个发现项、候选与只记录事实的终态绑定都为空；只预填不构成结论的线索（跨来源
   矩阵给出的规则证据项、这些证据项共有的适用条件、VC-1 处置与 SPEC 关联）。同一输入重跑返回同一 revision。
2. **审核人修订**（`classify --from-revision <rev> --revision-input <目录>`）：把某个 revision 的六个文件复制出去逐项
   处置后整目录交回，按字节写成新 revision，旧 revision 不覆盖；父 revision 的输入已漂移时拒绝，须重新生成机器草案。
   审核人在修订中完成：
   - 从已封存 P／R／J／M 离线复算可独立验证的原子断言，逐项绑定适用条件、官方正例、条件对照或零违规分母、证据
     通道、场景、连接和原始字节位置；不得补发请求；
   - 逐项处置全部 discovery、`CAND-*` 与只记录事实，使每个源项落到规则、支撑事实、受管出站、非出站、目标缺失或
     规范重复项之一；聚类不能减少源项分母；
   - 按第二部分的复合实现责任粒度，把一条或多条原子断言归并到 RequiredRule；客户端本地命题只能进入 scenario-only
     场景组，不能伪装成出站规则；
   - 对每条基线规则和目标新增规则定稿迁移决定，生成目标规则全集与 RuleMigrationLedger，并派生
     `affected_rules／inherited_rules`；两组必须互斥且覆盖完整迁移分母。
3. 草案与修订调用都按当前工具对该 revision 复算 §4.2.4 的清零门禁并给出 `review_sha256`（直接执行时退出码 2；在
   父批次里是合法暂停点，退出 0）。门禁结果不写进 revision，工具演进后重跑即得到新口径下的结论与摘要。
4. **批准**（`classify --approve-revision <rev> --approve-review-sha256 <sha>`）：核对 revision 未被改动、输入未漂移、
   门禁 closed、审核摘要与当前复算一致；先以 `artifact-seal` 封存 RuleMigrationLedger 与 AtomicAssertionLedger，再用
   `rule-classification-record` 把二者、原 VC-1 EvidenceFact 和 EvidencePackage 绑定为同一 Campaign 的
   `rule_classification_recorded`，随后写 `classification/approved/`（被批准 revision 的逐字副本）、清零收据
   `classification/result.json` 与 VC-2 checkpoint（执行项为空、复用项为 `inherited_rules`、零请求）。失败草案不得
   追加事实或进入画像生成与批准流程。

分母一律由输入计算：发现项数等于 DiscoveryInventory 条目数，候选数等于候选分母各组候选 ID 之并，原子断言数与规则数
以基线清单为起点，只记录事实数等于封存证据里识别出的条目数；当前 Campaign 的计数从清零收据
`classification/result.json` 读取。

2.1.226 已完成实例：`claude_fw_f_v21_finalize.py`、`claude_fw_f_discovery_clearance.py` 与
`claude_required_rules_2_1_226.json` 完成了实测复算、发现清零及规则映射——发现项 7368、候选 593、基线规则 110 映射
为 40 条 RequiredRules，命令见 §2.1.3。这些文件绑定当时的版本、历史分母和固定计数，只是本阶段合同的已完成实例；
后继版本用 `classify`，计数由目标证据动态产生，不能把 2.1.226 的结果直接当成新版本分类。

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

`DiscoveryDispositionLedger`（`discovery-disposition-ledger.json`）分三个分区：VC-1 DiscoveryInventory 的全部发现项、
候选分母的全部 `CAND-*` 等候选（候选处置合在这里，不另成文件）、FW-F v5 的只记录事实。它必须绑定 VC-1 的
DiscoveryInventory、CaptureIndex、跨来源矩阵和 EvidencePackage 摘要；总数与 ID 集合完全一致，每项唯一解决且所有
引用存在并双向闭合。`unclassified`、`mapped_validation`、`catalogued_context`、未收敛 `CAND-*`、无主事实、缺失、
重复和循环引用必须全为 0。`managed_egress_bound` 的出站 ID 必须在 VC-1 封存的生产现状清单里，`duplicate_bound`
只能指向同分区另一项。SupportEnvelope 缩小不能替代发现项处置，发现项也不得原位改名为规则。

只记录事实来自 v5 场景目录：两个灰度 Header 与 `/api/oauth/usage` 用量查询只记录、不让取证失败，每条事实以稳定
ID `OBSERVED-<probe>-<维度>` 进入只记录事实分区，原样保留是否可观测、是否出现、出现的取值、请求计数与条件。冻结
场景清单里以只记录维度命名的场景，其每个 probe 都必须产出对应事实，缺一即失败关闭。未出现（合法零流量）只能绑定
`supporting_fact_bound` 或 `target_absent_proven`，不得 `rule_bound`；不可观测的一律阻断、返回 VC-1 补证；出现了的
事实可以绑定到规则或支撑事实。对应规则是否因此 `change`／`condition_change`／`delete`，由审核人在迁移台账里逐条判定。

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
签发 ApprovalFact 前仍保持 `evidence_recorded`。`classify` 批准在同一动作里先用 `artifact-seal` 封存两个 Ledger，
再用 `rule-classification-record` 追加分类事实；不得再次执行 `evidence-record` 或替换 EvidencePackage。批准在任一步
被强杀后，对账并重跑同一批准命令逐步复用、事实数不增；VC-2 批次有阶段幂等重派合同，对账后按批次身份重派。
封存前修订必须写入新的 draft revision，不能覆盖旧草案；批准之后再批准同一 revision 只逐字复用，批准另一 revision
或再生成草案一律拒绝；迁移决定、RequiredRules 或断言归属需要变化时，必须建立后继 Campaign 并从 VC-2 继续。若只是
缺少目标事实，则返回 VC-1 定向补证，其他官方证据继续只读复用；任何恢复分支都不得在 VC-2 发送请求或提前修改画像
和实现。

<a id="claude-vc-3"></a>
## 4.3 VC-3 生成目标画像

- **输入**：VC-2 封存的 DiscoveryDispositionLedger、RuleMigrationLedger、目标 RequiredRules、
  AtomicAssertionLedger、`affected_rules／inherited_rules`、模型能力差异，以及 VC-0 冻结的当前
  Active／Rollback、Campaign 用途和已封存 EvidencePackage。
- **操作与工具**：`prepare-profile` 从当前 Active 画像派生目标草案，只应用版本身份、`affected_rules` 和已批准
  模型能力差异，第一次准备批准时签发 EvidenceApprovalFact；`stage-profile` 生成并联合核对 ProfileSchema、
  Snapshot、Wire、ReleaseArtifact、ModelCapabilityCatalog、SupportEnvelope、两个 Inventory、场景、断言与 Persona
  派生，批准时签发 ProfileApprovalFact。两者都是父监督器派发的 VC-3 批次动作，全程离线零请求，不改写
  EvidencePackage。
- **产物**：阶段专用 Profile policy、准备包与目标画像、联合清单与内容寻址联合对象、EvidenceApprovalFact、
  ProfileApprovalFact、`official_sealed → profile_approved` checkpoint、暂存 Release（`profile/release/`）、阶段结果
  `profile/result.json` 与 VC-3 checkpoint。
- **完成标志**：Evidence 与画像批准顺序闭合，全部画像差异具有规则或模型事实来源，联合制品 ID、计数和
  摘要一致，生产 Active／Rollback 与 selector 未改变，且尚未创建 Candidate。
- **失败恢复**：
  - 失败层：工具或批准输入。
  - 对账入口：`reconcile-supervisor-run`。
  - 续跑入口：对账后重派，批准前只新建 draft revision；迁移或规则错误返回 VC-2，官方事实不足返回
    VC-1；批准后画像或批准闭集变化按 Framework §5.3.4 建立后继 Campaign。

VC-3 只回答“如何把已经分类的目标责任表达成可批准画像”。本阶段不修改实现或生产 Catalog，不创建
Candidate，不运行 official／candidate PAIR，也不签发 AcceptanceFact；这些工作分别属于 VC-4～VC-6。

### 4.3.1 画像派生与差异约束

VC-3 在 VC-1／VC-2 的同一个受管 Store 里继续，`prepare-profile` 与 `stage-profile` 都由 `vc-batch.sh <NEW> <IN> VC-3
<序号> VC-2 <计划>` 派发。开始生成前，必须校验 `rule_classification_recorded` 对两个 Ledger、原
EvidenceFact／EvidencePackage 的绑定，并复算当前 Active／Rollback 和 Campaign 用途的摘要。当前 Active 一律取总计划
冻结的 `inputs.active_release_catalog` 与 `inputs.active_profile`，命令行不得另给；Release 目录 `production_active`
选择器指向的 Release，其画像摘要必须等于冻结的画像文件。`prepare-profile` 有三种调用：

1. **机器草案**（`prepare-profile --campaign-dir <C>`）：在 `profile/prepare/draft/rev-NNNN/` 写只写一次的 revision，含
   `profile-policy.json`（阶段专用 Profile policy，由输入派生、只读）、`rule-patches.json`（受影响规则的画像补丁，
   审核人逐条填）、`model-capability-patch.json`（已批准的模型能力差异，可为空）、`joint-inputs.json`（联合制品里不能
   由输入推出的事实：生产入口与出站清单、目标处置、支持范围、边界断言、Persona 派生、兼容边界与部署计划）与
   `wire-projection-policy.json`（Active 版本的 Wire 投影策略，只换版本号）；补丁一律为空，门禁因此 blocked。
2. **修订**（`--from-revision <rev> --revision-input <目录>`）：审核人处置五个文件后整目录交回，写成新 revision。
3. **批准**（`--approve-revision <rev> --approve-review-sha256 <sha> --approved-by <人>`）：核对 revision 未被改动、
   输入未漂移、门禁 closed、审核摘要一致；第一次准备批准时签发 EvidenceApprovalFact（Store 到 `official_sealed`），
   之后写准备包 `profile/prepare/approved/<rev>/` 与准备结果 `profile/prepare/results/<rev>.json`。

目标画像从当前 Active 的同一 Schema／DialectCompiler 派生，目标证据拥有行为选择权；“target-first”
表示不能让旧版 fixture 限制目标事实，并不表示丢弃 Active 结构后从空白画像重新拼装：

```text
target_profile = current_active_profile
               + version_identity_patch
               + model_identity_from_capability_catalog
               + rule_patches[affected_rules]
               + approved_model_capability_patch

profile_diff_paths
⊆ version_identity_paths
 ∪ model_identity_paths
 ∪ rule_field_paths[affected_rules]
 ∪ approved_model_capability_paths
```

版本身份替换只做精确值替换：Active 的版本、平台与二进制摘要，以及 `version=`／`platform=`／`binary_sha256=` 三种
条件串，替换成 Campaign 的目标值，证据路径与说明文字不做子串替换。模型能力目录按批准的 Wire 投影策略从 VC-1 证据
投影，画像 `identity` 里的支持模型、别名策略、未知模型策略与模型能力目录摘要随目录派生（`model_identity_paths`），
补丁不得再碰这四个落点；模型能力补丁只能落在模型闭集、别名与未知模型策略、能力目录摘要和模型特有 `fallbacks` 之内。

每个画像补丁必须记录唯一 owner、JSON path、`before／after`、迁移决定和证据引用；路径用 JSON Pointer，对象数组有
唯一键字段时段写成 `<字段>=<值>`（例如 `/rules/spec_id=SPEC-BODY-001/title`）。每条受影响规则至少一个操作，路径必须
落在 VC-2 画像补丁计划为该规则登记的落点之内，不同 owner 的操作不得重叠，每个操作都必须产生差异。`inherited_rules`
对应字段必须保持不变；`add／change／condition_change` 只能修改自身画像落点，`delete` 只能从目标选择面移除已证明
不可达的绑定，不能删除历史 Release、规则编号或证据。目标画像的规则全集必须等于 VC-2 目标规则，严格端点与
`feature_matrix` 不得引用目标规则之外的 SPEC。依赖扩圈必须回指 VC-2 已登记的直接依赖，不得把完整画像重写伪装成
版本升级。`profile_diff_paths` 写进派生收据，批准后随准备包冻结。

目标生成完成后，再用同一 Schema／DialectCompiler 表达 VC-0 冻结的 Active／Rollback fixture。当前
2.1.226 Campaign 中的 2.1.220 只是该次升级的历史 baseline fixture；后继版本必须使用当次 VC-0 身份，
不能永久把 2.1.220 当成升级基线或目标设计权威。

VC-3 使用只包含本阶段已知事实的 Profile policy（`claude-upgrade-profile-policy/v1`），冻结目标与基线身份、Campaign
用途、Active 目录与画像及投影策略的绑定、VC-2 输入摘要、动态规则／断言计数、版本身份替换、模型能力落点和生成器与
投影模块；生成时的有效工具身份冻结在准备结果里。不得预填尚未产生的 Candidate ID、源码／镜像摘要、独立官方复测、
候选环境收据或 AcceptanceFact。

历史 `claude_fw_f_profile.py` 会初始化新 Store／Campaign，故只保留为 2.1.226 的历史重放工具；原命令与
聚合 policy 限制见[历史审计文档](CLAUDE_CODE_CLIENT_EMULATION_HISTORY_AUDIT.md)。当前 `prepare-profile`／
`stage-profile` 已在原 Store 内依次承接 `rule_classification_recorded`、`evidence_approved` 与
`profile_approved`，动态绑定规则／原子集合、执行或继承范围及模型差异。新轮次使用本节的
`claude-upgrade-profile-policy/v1`，不复制包含未来 Candidate／Acceptance 身份的历史 FW-G policy。

### 4.3.2 联合制品与一致性审核

`stage-profile` 有两种调用。**预览**（`stage-profile --campaign-dir <C>`）从最新批准的准备包确定性构造联合制品——
PersonaDescriptor、ProfileSchema、Snapshot、ReleaseBundle（绑定 Wire、模型能力目录与 Release 的摘要）、
ReleaseArtifact、ModelCapabilityCatalog、入口与出站的 Observation／Inventory、边界断言、SupportEnvelope、
PersonaDerivation、CompatibilityBoundary、ScenarioPlan 与 DeploymentPlan——按 Store 的内容寻址算法预先算出引用，写
`profile/stage/<代号>/joint-manifest.json` 与 `stage-preview.json`，给出联合摘要 `joint_sha256`（直接执行时退出码 2，
在父批次里是合法暂停点）。**批准**（`--approve-joint-sha256 <sha> --approved-by <人>`）按依赖顺序以
`artifact-seal --allow-existing` 封存全部联合制品，再用 `profile-approve` 追加 `profile_approved`，随后写暂存 Release
`profile/release/`（布局对应仓库 `catalogdata/claude/`）、阶段结果 `profile/result.json` 与 VC-3 checkpoint（执行项为空、
复用项为 `inherited_rules`、零请求）。每项产物内容寻址、只写一次，不能改写当前 Active、Rollback 或历史对象；任一步
被强杀后重跑同一命令逐步复用、事实数不增。

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
Claude 运行代码中增加临时版本分支。生产入口清单、出站处置清单与目标处置由审核人在 `joint-inputs.json` 提供，草案
阶段即按 Store 合同与 `profile_approved` 写入门禁的跨对象一致性逐项核对：入口别名与调用方闭合、目标处置覆盖完整
Inventory、SupportEnvelope 只覆盖 `migrated_strict` 入口、DeploymentPlan 的 Active 范围等于 SupportEnvelope、
ScenarioPlan 精确覆盖目标规则全集与 `migrated_strict` 协议类别、严格端点以 `persona_strict` 绑定同一组 SPEC，
`production_replacement` 用途下目标规则的证据等级必须是 `verified`。

VC-3 只生成未入库的暂存 Release／Catalog。纳入同源 Candidate 源码树属于 VC-4，生产 Catalog 注册和
selector 切换属于 VC-6。Wire 与模型能力目录由受管模块 `claude_upgrade_wire_projection.py` 按 Wire 投影策略
`claude_upgrade_wire_projection_<版本下划线>.json` 从 VC-1 证据确定性投影；仓库根的 `tools/generate_claude_fw_g_profile.py`
只重放 2.1.226 的历史生成（投影逻辑从受管模块导入），不是本阶段入口，历史输出只按 §3.4／§3.6 审计。

### 4.3.3 EvidenceApprovalFact、ProfileApprovalFact、退出与恢复

VC-2 分类事实封存后，第一次 `prepare-profile` 批准签发 EvidenceApprovalFact（每个 Campaign 只有一份，之后批准新的
准备 revision 时沿用）；该事实必须同时引用原 `evidence_recorded` 事实、
未改写的 EvidencePackage v3 和同一 Campaign 的 `rule_classification_recorded`。分类事实再绑定
RuleMigrationLedger 与 AtomicAssertionLedger，因此无需、也禁止把 VC-2 结果补写进 EvidencePackage。
批准通过后 Campaign checkpoint 达到 `official_sealed`；缺少 EvidenceApprovalFact 时不得签发画像批准。
EvidencePackage v1/v2 及不含 `classification_fact_ref` 的旧 EvidenceApprovalFact 仅保留只读重放兼容；新的
正式 Campaign 必须使用 v3 路径，不得借历史格式绕过 VC-2。

完成草案生成、联合复算、跨 Release／跨 Persona 负例和人工审核后，才能一次性签发
ProfileApprovalFact。该事实必须引用上述 EvidenceApprovalFact，并以同一联合摘要冻结目标规则全集、迁移
与发现清单、EvidencePackage、Profile、Wire、Release、ModelCapabilityCatalog、SupportEnvelope、两个
Inventory、场景、断言、Persona 派生、compatibility 边界、三个运行 Envelope、隐私模式和 Campaign 用途。

准备批准之后、`stage-profile` 批准之前，审核人仍可起草、修订并批准新的准备 revision，`stage-profile` 一律采用最新
批准的一份。ProfileApprovalFact 只批准一次：以不同批准人或不同联合摘要重复批准一律拒绝，之后再起草或批准准备
revision 也都拒绝。

ProfileApprovalFact 不得引用未来 Candidate、构建或镜像，不得预填 official rerun、候选环境或生产收据，
也不得更新 Runtime Selector。`claude_fw_g_official_finalize.py` 与 `claude_fw_g_acceptance.py` 是 2.1.226 的历史
聚合入口，VC-3 禁止执行。

VC-3 的退出条件固定为：

```text
VC-2 rule_classification_recorded 已在同一 Campaign 封存
∧ EvidenceApprovalFact 引用同一 EvidenceFact／EvidencePackage／classification fact
∧ checkpoint = official_sealed
∧ profile_diff_paths
  ⊆ version_identity_paths
   ∪ model_identity_paths
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
构建或镜像变化时不改写本批准，在 VC-4 以新 revision 建立新的 Candidate。2.1.226 当前批准与续作状态只见
§3.4／§3.6，不在本节重复维护。

<a id="claude-vc-4"></a>
## 4.4 VC-4 实现固定 Candidate

- **输入**：VC-3 的 ProfileApprovalFact 与联合摘要、暂存 Release（Profile／Wire／ModelCapabilityCatalog）、
  SupportEnvelope、两个 Inventory、`affected_rules／inherited_rules`、原子断言、Campaign 用途，以及 VC-0 冻结的
  构建环境和目标架构。
- **操作与工具**：只实现 `affected_rules` 及其直接依赖，把批准画像、实现与测试纳入同一最终源码树，从该树
  构建不可变镜像；`plan-candidate-gates` 与 `record-candidate-build` 两个 VC-4 批次生成门禁计划、复算构建、签发
  构建收据并在 Store 冻结 ValidationCandidate。
- **产物**：候选源码树、测试树、依赖锁、门禁计划、CandidateBuildReceipt、制品 inventory、`candidate_frozen` 事实、
  revision seal、候选构建收据和 VC-4 checkpoint。
- **完成标志**：实现闭集通过，源码、测试、依赖、画像、Release、模型目录、构建和镜像身份可复算，
  Campaign 到达 `candidate_sealed`，生产 Active／Rollback／selector 未改变且未发起候选请求。
- **失败恢复**：
  - 失败层：工具、候选源码或预算账务。
  - 对账入口：`reconcile-supervisor-run`；候选作废用 `invalidate-candidate`。
  - 续跑入口：候选未变时同 revision 续作（重入 `vc4-all.sh`，VC-4 批次有阶段幂等重派合同）；源码、测试、依赖、
    构建或镜像真实变化时，在同一 Campaign 内以 `revision-open` 开新 revision，新 revision 对应新 Candidate ID，新候选的
    抓包从新 attempt 完整执行、不沿用旧镜像证据；规则或画像变化按 Framework §5.3.4 建立后继 Campaign，返回 VC-2
    或 VC-3。

VC-4 只回答“如何把已批准画像实现并冻结成可验证的 Candidate”。本阶段不重写 VC-3 事实，不执行
official／candidate PAIR，不创建 attempt 或 run nonce，不运行 Candidate，也不签发 AcceptanceFact。

### 4.4.1 入库与实现边界

开始实现前必须复算 ProfileApprovalFact、联合摘要和全部输入清单，并确认 `approval_purpose` 与 Campaign
用途一致：Store 检查点必须是 `profile_approved`，Store 唯一的 ProfileApprovalFact 就是 VC-3 阶段结果登记的那一条，
目标规则等于 VC-2 目标规则，批准核心与冻结的联合清单逐字相同；计时账本处于 `active`。只能追加目标对象，不能改写
当前 Active、Rollback、历史 Release 或生产 Catalog。候选链按以下顺序执行（脚本参数见驱动 README）：

1. 采集主机 `vc4-requirements.sh`：派发守卫 → 登记当前候选 revision（按参数 `CAND`：没有 revision 时
   `revision-open --initial` 建显式 r1，候选已变且旧候选已作废时 `revision-open --supersedes <旧候选>`，同一参数重跑
   幂等）→ 门禁需求预览写 `$RUNROOT/vc4-gate-requirements.json`。门禁需求由 VC-2、VC-3 阶段结果确定性推出（受影响与
   继承规则分区、联合摘要、迁移台账副本），`plan-candidate-gates` 只给 Campaign 与候选时是零写入的需求预览。
2. 本机 `local-claude-candidate-chain.sh`：只读取回 Campaign 总计划、VC-3 产物与需求预览，按摘要链核验后装配三段
   提交：
   - A 段（资产）：暂存 Release 的画像与 Wire（`backend/internal/officialegress/catalogdata/claude/profiles/<版本>/` 与
     `…/wire/<版本>/` 下的内容寻址文件，已有的只能逐字复用）、Wire 投影策略
     `tools/official_client_capture/claude_upgrade_wire_projection_<版本下划线>.json`、lifecycle 目录
     `docs/egress/lifecycle/claude-<版本去点>-candidate/` 的 `catalog-stage/`（暂存收据与 Release 条目，恰好两个文件）、
     门禁映射 `gate-mapping.json` 与门禁计划 `gate-plan.json`；
   - C 段：只有 `catalogdata/claude/release-catalog.json`——VC-3 冻结的 Active 目录逐项保留，`releases` 末尾追加暂存
     Release，`selectors.validation_candidate` 指向它；写完先用候选源码的 Go 加载器核对，任一项不过即还原、停下；
   - D 段：`freeze-successor-generate --before A --after C` 的承接收据，要求摘要边恰好只有 `release-catalog.json`。

   仓库当前提交的 Claude 目录必须就是 VC-3 冻结的 Active 目录，否则候选链停下。投影策略随 A 段进入受管树，部署含它的
   候选树之前先登记一次 control 层工具演进。
3. 采集主机 `arm64-vc4-gates.sh`，确认门禁收据有效后再运行 `vc4-all.sh`，两者不并行（§4.4.2）。

实现闭集必须满足：

1. 每项代码、测试、画像装载和路由变化都能回指 RuleMigrationLedger 中的 `affected_rule`、对应原子断言
   及直接依赖；依赖扩圈必须记录完整路径，不能借升级实施夹入无关重构；
2. `inherited_rules` 对应目标语义和画像保持不变，只重放来源、既有测试和验收收据；共享实现锚点仅在已登记
   直接依赖要求时允许变化，并必须补做继承规则回归。执行计划分别记录规则总数、受影响数、继承数、实际
   执行数和复用数；
3. ValidationCandidate 直接绑定独立 immutable ReleaseArtifact，由 `validation_candidate` 选择器显式选择，不借用
   `production_active`、`production_rollback` 选择器；生产默认选择器在 VC-4 全程不变；
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
worktree、临时复制文件或多个节点的未封存输出拼装 Candidate。构建在 ARM64 执行（§6.5）：`vc4-all.sh` 从同一干净提交
准备源码、测试、构建与 Docker context，构建前端、linux/arm64 后端与 `docker build --platform linux/arm64` 镜像，再跑
实现测试；续跑凭证与实现门禁按客户端取 Claude 的绑定，最后一步派发 `vc4-claude.sh`。改变节点或架构必须先在 VC-0
重新冻结。Claude 不复用实现测试：每轮全量重跑，r≥2 的候选也一样。

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

`vc4-claude.sh` 派发两个 VC-4 批次，动作计划都用受管动作生成、按 VC-4 阶段幂等重派合同自检：

1. `plan-candidate-gates`：生成模式给出候选源码树、树内门禁映射与树内尚不存在的输出路径，计划只写一次、落盘后按
   VC-3 门禁需求复算（门禁需求合同与 Codex 相同，计划沿用共享的 `build_gate_plan`）。派发前在批次外以同一组旗标预演
   一次，与提交内 `gate-plan.json` 逐字节比对。
2. `record-candidate-build`：十九个旗标与 Codex 同名同义、全部必需。全部复算在任何写入之前：用途、部署版本与架构、
   不可变镜像引用、独立干净的候选源码树与三个树摘要、严格构建参数、候选树里的 Claude 目录数据逐字节来自 VC-3 暂存
   Release、树外可复算的 source transition、树内门禁计划、实现测试收据，以及经 docker／go 复算的构建实物；r≥2 时
   另做 revision seal 预检。写入顺序：四件机器收据 → Store CandidateBuildReceipt（内容寻址，同输入复用）→ Store
   `candidate_frozen` → revision seal → 候选构建收据（`official-client-upgrade-candidate-build-receipt/v1`）→ VC-4
   checkpoint；任一步被强杀后重跑同一命令收敛。派发前用 `--preview` 在批次外做零写入预览（退出码 2），前后比对
   Campaign 写域。

Store 收据由 `receipt-replay` 从批准事实、内容寻址对象和收据中的冻结输入重新构造同一字节，拒绝手工散列、可变
tag、历史 generation policy 或事后 AcceptanceFact；Store 的 `candidate-build-finalize`、`candidate-freeze` 只由
`record-candidate-build` 在同一动作里调用，操作员不直接执行。

### 4.4.3 Candidate 身份冻结与 VC-5 交接

`record-candidate-build` 按 `tools/official_client_control/schemas/validation-candidate.schema.json` 生成 payload，
在 Store 一次冻结以下字段：

| 身份层 | `candidate_frozen` 必须冻结的字段 |
|---|---|
| Candidate | `schema_version=official-client-validation-candidate/v2`、`candidate_id`、`candidate_purpose`、`candidate_build_receipt_ref` |
| 批准与发布 | `profile_approval_ref`、`release_artifact_ref`、`support_envelope_ref` |
| 源码 | `source_tree_sha256`、`test_tree_sha256`、`dependency_lock_sha256` |
| 构建与镜像 | `target_architecture`、`build_id`、`image_digest` |
| 联合身份 | 对上述字段计算的 `identity_sha256` |

`candidate_purpose` 必须与 ProfileApprovalFact 的 `approval_purpose` 完全一致，ReleaseArtifact 和
SupportEnvelope 必须直接来自同一批准事实；CandidateBuildReceipt 中更完整的 commit、tree、二进制、
构建参数、ModelCapabilityCatalog、image ID 和镜像引用也必须与 payload 一致。严格 v2 payload 直接引用刚生成的
`candidate_build` 收据；受管入口会逐项核对批准引用、用途、Persona、source／test／dependency、build ID、架构和
OCI digest。后继 Campaign 必须在首个 VC-5 attempt 和候选真实请求之前独立取得 `candidate_frozen`。
`claude_fw_g_acceptance.py` 将 Candidate 与 AcceptanceFact 聚合生成，只用于解释历史 2.1.226 制品，不能作为后继
版本的 VC-4 入口。

候选 revision 有编排器与 Store 两份登记，必须一致；Claude 不认隐含 r1，候选级动作要求先建显式 r1。候选源码、测试、
依赖、构建或镜像真实变化时：

1. `invalidate-candidate preview` 生成诊断草案与 `review_sha256`，审核后 `invalidate-candidate apply` 写编排器作废
   记录、根因与账本事件，并在 Store 写作废对象（证据先封存为运营证据）；预算暂停与永久停线不写 Store；
2. 作废冻结整个当前 revision：候选级事实、绑定候选的收据与新执行计划一律拒绝，直到开新 revision；已有
   `validation_completed` 的候选不得作废；
3. 换新的 `CAND` 重跑 `vc4-requirements.sh`，由 `revision-open --supersedes <旧候选>` 开 r(N+1)，再走候选链；
4. `record-candidate-build` 写 revision seal：r≥2 以被取代候选冻结的身份快照为基准，源码层与构建层全部相同即拒绝
   （不是新候选），被取代候选写失效标记 `superseded-by.json`。

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
镜像或运行时 Profile 任一变化，必须保留旧事实，按上面的作废与 `revision-open` 建立新 Candidate。历史 Sonnet-only
与三模型 Candidate 的精确身份分别以 [FW-G 隔离验收收据](egress/maintenance/claude-fw-g-acceptance.json)和
[三模型 FW-G 验收收据](egress/maintenance/claude-fw-g-three-model-acceptance.json)为准，当前续作状态见
§3.6；这些历史身份不直接代表当前入站准入、候选交付或生产状态。

<a id="claude-vc-5"></a>
## 4.5 VC-5 定向验证

- **输入**：VC-4 固定 Candidate 与构建收据、VC-1 官方 EvidencePackage、VC-2 分类／原子断言归属、VC-3
  批准画像与场景计划，以及本轮执行／复用闭集和隔离候选环境。
- **操作与工具**：`vc5-all.sh` 按 `CLIENT=claude` 分派 `vc5-claude-start.sh`、`vc5-claude-seal.sh`、
  `vc5-claude-accept.sh`；内部生成候选采集、封存、compare、机器断言与 accept 批次。
- **产物**：候选 attempt、CandidateEvidencePackage、场景批准链、比较与断言结果、PAIR、外部门禁收据、
  AcceptanceFact、VC-5 checkpoint 及 VC-6 交接输入。
- **完成标志**：当前基线的执行项全部通过、承接来源可重放，Store 达到 `validation_only／ready`，生产状态
  保持 `not_activated`；脚本退出 0 或出现暂停提示均不代表完成。
- **恢复**：封存前用预约／父 run 对账；封存后用 §4.5.8 的评估恢复。只补失败、未完成或依赖变化项。

```bash
ARM64_VC_ENV="$VC_ENV" bash "$DRIVER/vc5-all.sh"
```

`VC_ENV` 是本轮唯一参数文件，`DRIVER` 是已安装驱动目录；下文变量均取该参数文件与受管上下文工具。
`capture-candidate`、`compare`、`assert-rules`、`accept` 是父批次中的动作，操作员通过驱动或
`compile-and-run-vc-batch` 派发。Store 低级写入命令由编排器调用，不作为人工拼接事实的操作步骤。

**当前限制**：Mac 客户端收据已有离线准备、核验与导入合同，但三官方客户端真账号采集尚未接通，正式
recorder／独立捕获来源登记仍为空。缺少 Desktop／VS Code 收据或 seal 批准时，驱动写
`vc5-pause.json` 并暂停。B4-05b 的封存后 `ar<k>` 恢复段仍在实现／待验收，完整录制回放与发布认证也未完成，
见 §3.6。以下入口和合同不能替代这些现场验收。

### 4.5.1 执行闭集、运行前检查与 attempt 冻结

1. 从 VC-2 的 `affected_rules`、`inherited_rules`、直接依赖和合法 checkpoint 编译本轮执行／复用计划。
   规则总数、受影响数、继承数、实际执行数和复用数分别登记；空执行集合须先给出零请求证明。
2. 重放 VC-3 批准、VC-4 CandidateBuildReceipt、Release／Profile／模型目录、镜像 digest、源码／测试／
   依赖摘要和架构。候选身份只从 VC-4 收据读取；Claude 不接受用 Codex 的 `--runtime-image`、`--build-id`
   等参数重新描述同一候选。
3. 复核 ARM64 隔离网关、Compose、依赖、网络、磁盘、账号权限、代理／CA／DNS、隐私模式、回退材料与
   实际 runner。候选网关恢复原值在第一处修改之前落盘，运行后恢复报告须与该 Job 绑定。
4. 确认 production Active／Rollback／selector 未变化，官方 EvidencePackage 可重放，项目总账、时间账本、
   VC-5 准入和当前部署允许执行。通过后才预约 attempt、冻结 `run_nonce`、执行计划及环境 before 身份。

首跑动作是 `capture-candidate run --campaign-dir <Campaign> --candidate-id <Candidate>
--acknowledge-live-requests`；由 `vc5-claude-start.sh` 生成并派发，不另起无人监督的采集进程。通过的 Job
立即写结果与 checkpoint，恢复时按来源收据复核，不能仅按日志绿色承接。

**失败从哪里接着跑，以候选证据是否已经封存为分界。** 封存前有尚未完整收口的采集预约时：
`reconcile-attempt` → 批准恢复预览 → 授权 → 受监督 `resume --candidate-id … --rerun-failed
--recovery-preview …`；采集已收口而 seal／compare 批次失败时用 `reconcile-supervisor-run`。封存后保留采集
原件，按 §4.5.8 新建评估基线或候选恢复段，不能删封存重跑。

跨 Campaign 复用同一候选构建时，`resume` 另带 `--candidate-reuse-source-campaign-dir`、
`--candidate-reuse-source-candidate-id`、`--candidate-reuse-source-attempt-id`；先 `--preview-recovery`，
审核后以 `--approve-candidate-reuse-sha256` 绑定预览。来源可为合格的已完成或已封存 attempt；已封存来源按
`producer.predecessor_sealed_cover`、原封存清单与元数据重绑链只读复核，保留最初产出者和请求身份，不复制
重封旧证据根。前序计划、批准、环境连续性或逐 Job 来源证明不闭合时不得承接。

### 4.5.2 定向场景、模型与入口边界

候选服务在 §6.4 的 ARM64 隔离网关运行；Mac 上已冻结身份的 Claude Code、Claude Desktop 与 Claude Code
for VS Code 按批准计划驱动候选。Claude 按 ModelCapabilityCatalog 枚举模型与条件，不套用 Codex 的模型双轨。
只执行计划中的 `execute_item_ids`，`subject × scenario` 分别登记运行坐标。

| 场景组 | 核对范围 |
|---|---|
| 生命周期与 wire | 推理、hello、TLS／ALPN、HTTP/1.1、重试及连接关系 |
| 会话与状态 | 主轮／续轮／Agent、标题、工具往返、request-id、fallback、重启与状态存储 |
| 模型与工具 | 登记模型的 effort、thinking、background、WebSearch、count_tokens、fallback，及未知模型／别名负例 |
| 入口与处置 | 三官方客户端正例；第三方、未知版本、非法 Header／Body／工具目录在 OAuth 凭据前拒绝 |
| Persona 隔离 | strict／managed／legacy 边界、隐私与身份派生、跨 Persona／Release 拒绝、Codex final wire 隔离 |

具体集合由本轮 SupportEnvelope、场景计划、两个 Inventory 与 AtomicAssertionLedger 推出；2.1.226 的
`40／106／110` 仅是历史实例。真实 TUI、remote、hook、附件、MCP、OAuth refresh 等按批准条件进入计划，
合法零流量不自动授予规则或能力。

只有 OfficialIngressCatalog 登记且 TranslationReport 为 lossless 的来源进入正向 `PAIR-<SPEC-ID>`。
第三方拒绝与 Codex 隔离由 `capture-candidate boundary-check` 的 `third-party-ingress-rejection`、
`codex-final-wire-isolation` 两项单独留证；后者需要冻结的 Codex 账号／API Key／模型、生产 commit 及真实请求
确认。驱动按顺序派发边界核对，不能用手工 HTTP 正例或伪造客户端 Header 替代真实客户端收据。

Mac 客户端材料通过 `capture-candidate client-evidence prepare／verify／import` 冻结任务、核验原始附件并导入。
这组入口目前只具备离线合同能力：没有已登记的真实 recorder 就不能预约正式客户端运行；
`regression_fixture` 只归档历史回归材料，不能满足正式 `--client-evidence` 或真账号验收。后续须接通
真实客户端、独立捕获来源与请求前预算准入，再按同一合同形成可消费的正式收据。

### 4.5.3 Candidate 证据封存

场景事实按 `prepare → capture → seal → approve` 闭合：运行前条件、原始采集、环境恢复与秘密扫描、审核
摘要分别绑定同一 attempt。失败 attempt、失败场景和原始字节不可覆盖；VC-6 的最终切换、实际回退与稳定
观察不能提前记成本阶段完成。

`vc5-claude-seal.sh` 依次核对两项边界收据、Mac 客户端收据和封存预览，再等待批准；输入完整后：

```bash
ARM64_VC_ENV="$VC_ENV" bash "$DRIVER/vc5-claude-seal.sh" "$ATTEMPT" \
  --approve-seal-sha256 "$SEAL_SHA256" --approved-by "$REVIEWER"
ARM64_VC_ENV="$VC_ENV" bash "$DRIVER/vc5-all.sh"
```

预览由 `capture-candidate seal` 生成，批准参数是 `--approve-seal-sha256` 与 `--approved-by`；驱动同时冻结
`--gate-execution-overrides` 的实际门禁主机／架构。客户端材料通过重复的 `--client-evidence` 绑定；缺材料、
批准漂移或闭合门禁 blocked 时停止，不能把批准人姓名当作批准凭证。

封存由当前 `claude_upgrade_candidate_seal.py` 写 Store 的执行计划、ValidationAttempt、场景四阶段、
CandidateEvidencePackage 与评估 manifest，绑定候选、attempt、原始 inventory、前后环境、恢复报告、秘密
扫描和摘要。只扫描已登记证据根；旧 FW-G 聚合脚本和[历史审计文档](CLAUDE_CODE_CLIENT_EMULATION_HISTORY_AUDIT.md)
里的 live-matrix 均不承担后继版本封存。封存后仅 mtime／ctime／inode 漂移可走 §4.7 的元数据重绑，内容或
权限变化不能借此放行。

### 4.5.4 离线比较

批次动作 `compare --campaign-dir <Campaign> --candidate-id <Candidate>` 由
`claude_upgrade_compare.py` 读取 VC-1 EvidencePackage v3 与当前 CandidateEvidencePackage，复核目标身份、
批准断言画像、规则覆盖、surface、inventory 和条件集合，写一次性比较收据及 Claude builder 配置。

官方评估投影只从已经封存的 VC-1 证据确定性派生；候选评估 manifest 在 seal 时生成，compare 只读。
不能因比较失败重新采集官方请求、补写 capture 事实，或把历史 `claude_fw_g_official_finalize.py` 的
2.1.226 聚合结果用于新目标。

comparison 完整只证明身份、覆盖与比较输入闭合。surface 的 `equal=true` 不代替逐规则断言；
`equal=false` 也须按批准差异与实际断言解释，不能单凭集合差异决定验收。

### 4.5.5 逐规则机器断言

`vc5-claude-accept.sh` 从当前基线比较收据取得 builder 配置，派发 `assert-rules`；Claude checker、builder、
批准断言画像与运行审计分别由 `claude_rule_assertion.py`、`claude_build_rule_assertion_results.py`、
`claude_upgrade_assertion_profile.py` 和 `claude_build_runtime_audit.py` 实现。builder 记录逐规则依赖投影与
`evaluation-run.json`，没有复用授权的来源明确记为 `reuse_authority=none`。

每条 RequiredRule 唯一生成 `PAIR-<SPEC-ID>`，绑定批准画像、Release、Candidate、条件摘要、官方与候选来源、
原子 owner、实现／测试锚点、场景引用与执行／复用来源。动态字段比较来源、格式、关系和生命周期；
`dual_wire`／`candidate_profile` 按批准合同选择，不得由测试全绿或手写通过替代。

PAIR 必须完整唯一覆盖本轮 VC-2 封存集合；当前基线使用严格 v3 PAIR／Acceptance，带评估基线绑定，旧 v2
按形成期只读重放。2.1.226 的 40 条规则与 106 条画像原子断言只是已完成实例，4 条客户端本地断言不进入
出站画像分母。升级时更新批准模板，不把这些计数固化成通用入口条件。

### 4.5.6 外部门禁与 AcceptanceFact

在 CandidateBuildReceipt 绑定的源码树与平台上生成并重放外部门禁收据；共享的三条候选门禁为
`check-egress-spec`、`full-regression`、`target-platform`，其 subject 由 Claude 当前候选身份推导。
执行／承接项绑定命令、工作目录、主机、架构、起止时间、退出码、输出摘要及唯一前序失败收据。
`vc5-claude-accept.sh` 开始时要求当前部署的独立后台验证已经通过。

验收动作的参数为 `accept --campaign-dir <Campaign> --candidate-id <Candidate> --assertions <结果>
--external-gate-root <门禁根> --external-gate-receipt <收据>`。它复核机器断言、三官方客户端与第三方拒绝
证据（CLI 正例来自 PAIR 候选证据，Desktop／VS Code 使用各自收据）、候选激活事实的运行审计及门禁，再写 Store：

```text
pair-record → validation-gate-finalize → acceptance-record（当前基线 v3）
            → validation-finalize → VC-5 checkpoint
```

外部门禁有失败时，accept 固化本轮失败收据并返回 `gate_failed`；修复后由受监督
`capture-candidate seal … --revalidate` 建新 `v<N>` ValidationAttempt，只承接首轮封存原件、零请求且不重扫，
按门禁闭集续作。不能覆盖前次失败或把整个候选采集重跑一遍。

| Campaign 用途 | `acceptance_purpose` | `result` | workflow checkpoint／production state |
|---|---|---|---|
| `validation_only` | `validation_only` | `validation_only` | `validation_only／not_activated` |
| `production_replacement` | `production_replacement` | `accepted` | `ready／not_activated` |

`validation-finalize` 从执行计划、证据、PAIR、门禁、selector 前后态与用户管理的生产状态复算闭集后追加
`validation_completed`；单有 AcceptanceFact 仍可停在 `validation_pending`。AcceptanceFact 不晋升 Release、
不更新 production selector，也不证明候选已经交付。

### 4.5.7 验收终态与 VC-6 交接

```text
VC-4 CandidateBuildReceipt 与 candidate_frozen 可复算
∧ execute_items 全部通过 ∧ reuse_items 全部可重放
∧ 必需场景 prepare → capture → seal → approve 闭合
∧ CandidateEvidencePackage、秘密扫描与 inventory 完整
∧ PAIR 覆盖本轮 RequiredRules，原子断言归属完整唯一
∧ 当前基线外部门禁与 AcceptanceFact 通过且 VC-5 checkpoint 可重放
∧ production Active、Rollback、selector 与未授权环境未变化
⇒ 进入 VC-6
```

Claude 不建立 Codex 的 canonical-import，VC-6 沿同一 Store Candidate／Acceptance 链交付。
`ready／validation_only` 是验收 checkpoint；候选交付的 `ready_for_operator_release` 和生产激活终态分别
见 §4.6。两个用途都不能在 VC-5 提前声明完成。

历史 Store 使用其收据登记的 external root 和形成期工具重放，不能拿当前工作区替换再把差异误判为历史
规则失败。历史凭证与已失效的运行坐标统一见[历史审计文档](CLAUDE_CODE_CLIENT_EMULATION_HISTORY_AUDIT.md)。

### 4.5.8 评估失败的分类与局部恢复

封存前按 §4.5.1 对账；封存后的断言失败先由控制入口 `evaluation-recover preview` 生成诊断与审核摘要：

```bash
python3 -m tools.official_client_capture.claude_upgrade evaluation-recover preview \
  --campaign-dir "$CAMPAIGN" --candidate-id "$CANDIDATE" --reviewer "$REVIEWER"
```

| `root-cause-class` | 修复与续跑入口 | 当前边界 |
|---|---|---|
| `evaluator-defect` | 修 evaluator、定向回归、受监督部署并登记演进；`evaluation-recover apply` 绑定修复 commit、部署收据与批准摘要，开新 `b<K>` | 复用原始采集，仅重评受影响部分 |
| `transient-environment` | 从失败断言沿候选 manifest 的 `source_artifacts` 回溯非空 Job 集 J*；经批准开 `ar<k>`，补采后增量封存、重评 | B4-05b 仍在实现／待验收；当前不得把命令参数存在解释为全链可用 |
| `candidate-source` | 对账、`invalidate-candidate preview／apply`，再 `revision-open --supersedes` | 新候选从 VC-4 起，保留可信 VC-0～VC-3 |
| `approval-inputs` | 仅 selector 修正走 `evaluation-recover approval-revision`；规则集、模式、场景或目标身份变化建后继 Campaign | 新批准基线下旧验收不能直接沿用 |

evaluator 修复的批准入口为：

```bash
python3 -m tools.official_client_capture.claude_upgrade evaluation-recover apply \
  --campaign-dir "$CAMPAIGN" --candidate-id "$CANDIDATE" --reviewer "$REVIEWER" \
  --root-cause-class evaluator-defect --fix-commit "$FIX_COMMIT" \
  --deployment-receipt "$DEPLOYMENT_RECEIPT" --approve-sha256 "$REVIEW_SHA256"
```

主动重评用 `evaluation-recover reevaluate`，selector 修订用 `evaluation-recover approval-revision
--assertion-profile <修订画像>`；两者不带 `--approve-sha256` 时只预览，再用同样输入加摘要应用。
**`approval-revision` 是 `evaluation-recover` 的动作，不是独立子命令。**

新基线按 COMMIT、时间账本 `evaluation_baseline` 和 Store `evaluation_baseline_recorded` 联合生效，
`evaluation_reopened`／`reopen-b<K>` 记录重开原因；阶段 `local／reuse` 来源由基线指定。缺少
`evaluation-run.json` 时，Claude 明确拒绝从历史 b0 报告猜造派生事实，应按对账结论在同一基线逐字重派未完成
批次。旧基线与失败结果始终只读。

<a id="claude-vc-6"></a>
## 4.6 VC-6 交付或生产激活

- **输入**：VC-5 checkpoint、同一 Candidate／构建收据／Acceptance、候选证据与用途，隔离环境快照、真实
  回退点、交付计划和归档输入；生产用途还需独立的生产权限及 P0 冻结输入。
- **操作与工具**：`deliver-candidate run` 控制四阶段，`finalize` 单独审核交付包；阶段重跑通过
  `delivery-stage-invalidate` 与 `dr<k>` 记录。阶段动作仍由父批次派发。
- **产物与终点**：候选交付收据 `ready_for_operator_release`；`validation_only` 完成 VC-6，
  `production_replacement` 还须完成生产分支，目前停 `production_activation_pending` 等待 B4-08。

**当前 ARM64 四阶段真实交付执行器尚未完成，`ENVIRONMENT_EXECUTORS` 尚无登记项。** 控制面、Store 合同与
恢复测试不能替代真实切换证据；以下流程是当前交付操作合同，在执行器接入、验收及本轮准入齐备前不得
宣称已完成候选交付。生产分支 §4.6.3～§4.6.8 随 B4-08 另批。

### 4.6.1 用途分流、交付快照与回滚点

先重放 VC-5 完成事件与 AcceptanceFact，并核三处同源：accept 引用、VC-4 构建收据、固定镜像 digest。
再核 Candidate、源码／测试／依赖、Profile／Wire／Release／模型目录、SupportEnvelope、候选证据、门禁、
目标架构与用途。任一缺失或漂移都在写入前拒绝。

| `campaign_purpose` | 候选交付 | 后续终点 |
|---|---|---|
| `validation_only` | 四阶段及归档闭合，签发 `ready_for_operator_release` | 写 VC-6 checkpoint，不创建生产事实 |
| `production_replacement` | 先完成完全相同的候选交付 | 生产权限与 P0 输入齐备后继续生产激活；当前等待 B4-08，不能改用途绕过 |

切换前只读冻结 §6.4 ARM64 环境的应用镜像、Compose／override、依赖容器、数据库、缓存、挂载、网络、
代理／CA／DNS、账号、磁盘与资源水位，登记实际可恢复的应用镜像和配置为本轮回退点。2.1.220 只是历史
fixture，只有它本身就是本轮已冻结且验证可恢复的运行对象时，才可充当 operational rollback。

VC-6 始终使用 VC-4／VC-5 同一固定镜像，不能重编译、重封装或按可变 tag 替换。候选身份真实变化返回
VC-4；交付环境身份变化则保留旧交付，开新 `delivery_id`。

交付恢复有两种锚点：阶段失败事实 `failure`，或批准的阶段失效对象 `approved_invalidation`。失败阶段之前
身份和依赖未变的有效 pass 可复用；从某阶段重跑时，该阶段及全部下游旧 pass 失效，新 `dr<k>` 必须覆盖
到 `stable_observed`。一份交付同一时刻只允许一个未收口恢复段，finalize 后不得再失效或改写。

### 4.6.2 ARM64 隔离候选环境四阶段交付

交付门禁只检查镜像身份、Compose 差异、启动与 health、依赖连通、入口路由、秘密扫描、inventory、回退
可用性与冻结观察窗。VC-5 正负矩阵、PAIR、断言和外部门禁只重放封存收据；必要 smoke 预先列入交付计划，
不能扩大 SupportEnvelope 或为报告重发官方请求。

| 顺序 | checkpoint | 现场要求 |
|---|---|---|
| 1 | `candidate_active` | 固定候选在隔离网关启动，通过交付 smoke |
| 2 | `rollback_verified` | 仅切换应用容器到真实回退镜像，验证 health、鉴权、数据与依赖 |
| 3 | `candidate_restored` | 恢复同一候选 digest，核 Profile、入口与运行身份 |
| 4 | `stable_observed` | 冻结观察窗内无身份、安全、数据、状态或跨 Persona／Release 混用异常 |

每阶段绑定时间、主机、架构、镜像、Compose、前序事实和实测材料，`candidate_delivery_recorded.result`
只能是 `pass／failed`。失败须保留证据并恢复已冻结回退镜像；后续阶段不能承接失败事实，也不能在故障
容器内改代码后沿用原候选。

工具流程分成两批，不把审核夹在一个无人监督动作中：

1. 注册的执行器就绪后，用 `claude_upgrade_delivery.run_action`／`vc6_action_plan` 生成
   `deliver-candidate run` 动作，经 `compile-and-run-vc-batch` 派发。动作冻结 `--candidate-id`、
   `--environment-executor` 与 `--observation-window-seconds`；默认观察窗为 900 秒，以交付计划为准。
2. 全 pass 停在 `awaiting_finalize`（入口返回 2），失败停在 `stage_failed`（入口返回 3）；父批次将二者
   作为合法暂停点处理，退出 0 不表示交付完成。失败先对账，再重入同一 run 命令，由有效链推导 `dr<k>`。
3. 用不带批准摘要的 `deliver-candidate finalize --campaign-dir <Campaign> --candidate-id <Candidate>`
   只读查看交付包审核内容；批准后由 `finalize_action` 生成带 `--approve-sha256` 的独立 finalize 批次。
   重复 finalize 返回原收据，不重新签发。

未 finalize 时主动重跑成功阶段，先在批次之间执行控制命令：

```bash
python3 -m tools.official_client_capture.claude_upgrade delivery-stage-invalidate preview \
  --campaign-dir "$CAMPAIGN" --candidate-id "$CANDIDATE" \
  --stage rollback_verified --reason "$REASON" --evidence "$EVIDENCE"
python3 -m tools.official_client_capture.claude_upgrade delivery-stage-invalidate apply \
  --campaign-dir "$CAMPAIGN" --candidate-id "$CANDIDATE" \
  --stage rollback_verified --reason "$REASON" --evidence "$EVIDENCE" \
  --approve-sha256 "$INVALIDATION_REVIEW_SHA256" --approved-by "$REVIEWER"
```

随后派 run 批次，`dr<k>` 以批准对象为锚覆盖第二至第四阶段。`--rerun-from` 仅核对推导的起点，不能替代
批准来强制重跑。阶段事实先写 Store 后写时间账本，重入时自动补账；不手写恢复事件。

候选交付收据绑定 Candidate／构建收据、VC-5 完成与 Acceptance、环境快照、有效四阶段链、失败／失效／
恢复段引用、交付包与私有归档清单。Store 字段仍名 `vircs_state`，在本合同中表示“用户管理的生产环境状态
`operator_managed／unverified／not_touched`”，不是当前机器地址。

```text
VC-4 固定候选与 VC-5 当前基线验收可重放
∧ 实际运行镜像 = 固定候选镜像
∧ 有效链 candidate_active → rollback_verified → candidate_restored → stable_observed 全 pass
∧ 交付包、回退材料和私有归档可独立恢复
∧ ready_for_operator_release 收据可重放，未授权生产环境未触碰
⇒ 候选交付终态
```

`validation_only` 再写 VC-6 checkpoint；生产用途不在此签发 DeploymentFact。

### 4.6.3 Catalog 晋升与 production tree（B4-08 另批）

Claude 使用 `backend/internal/officialegress/catalogdata/claude/release-catalog.json` 的
`production_active`、`validation_candidate`、`production_rollback` 三个 selector。
生产晋升须从已验收 Candidate 生成或晋升正式 Release，批准目录转换并记录 transition／差异收据；不能使用
Codex 的 canonical-import 或把新 production tree 冒充原 VC-4 候选。该生产写入流程尚未完成。

### 4.6.4 动态 post-promotion 门禁（B4-08 另批）

正式源码树变动后的门禁应从晋升差异、规则与依赖闭集动态派生，绑定生产源码、目标平台和批准 transition；
候选门禁只作为有证明的来源，不能直接改名复用。失败保持未激活，按受影响范围补跑；本节尚无已验收的
Claude 生产操作入口。

### 4.6.5 权威源码、切换镜像与正式发版（B4-08 另批）

生产顺序固定为：目录转换与批准 → 构建发布 GHCR 多架构镜像 → 实际部署及验收 → DeploymentFact →
ProductionAcceptance。正式产物须以 promotion 收据连接原 Candidate，保留原构建／候选事实不变。
权威源码、发布镜像 digest、生产权限与回滚材料在执行前冻结，不能先签生产事实再补部署。

### 4.6.6 独立 Active canary（B4-08 另批）

正式镜像在独立 canary 中验证生产 Active 身份、已批准流量与回滚操作范围；与候选验收使用不同运行事实，
禁止直接拿 VC-5 或隔离候选交付的 pass 代替。具体执行器、观察窗和验收入口待本批实现并冻结。

### 4.6.7 原子生产切换（B4-08 另批）

仅在 `production_replacement`、可重放候选交付收据、生产权限和 P0 生产输入齐备时，按 Framework §5.6.2
执行原子切换。实际部署及验收完成后，DeploymentFact 绑定实际主机、镜像 digest、Release 与批准链；
历史 Go 校验不作为新版生产事实的通用签发器。

### 4.6.8 回滚、目标恢复与终态（B4-08 另批）

生产分支须完成真实回滚、目标恢复、稳定观察和独立生产验收，才能声明 `production_active_upgraded`；失败
恢复真实 Active 并保持 `production_unverified`。候选 `ready_for_operator_release` 不是生产完成。

`claude_closeout.py` 与 Claude 终态收据校验已接入工具，终态合同为
`official-client-claude-<ver>-terminal-state/v1`，校验 production_active 与生产凭证；收尾工具存在不补齐
尚缺的生产部署链。既有 2.1.226 的精确历史豁免按实现只读解释，不给后继版本补签或外推豁免。

### 4.6.9 私有归档与远端清理

候选交付按 §6.7 归档：交付包引用不可变候选、部署配置、公开证据索引、逐规则结果、Acceptance 和回退材料；
私有证据另存逐文件 `path／size／sha256` 清单。`artifact_count` 统计部署配置、公开索引与回退材料，
`private_archive_manifest.entry_count` 统计私有归档内部文件，两者不混计。收据绑定归档清单后才算终态。

清理前验证交付包与私有归档能独立恢复，并登记清理决定。不能删除仍承担当前运行、回退、历史重放或唯一
证据责任的产物；中断先对账，不盲目重派删除。生产 promotion／activation／removal 的归档及远端清理随
B4-08 另批，候选清理批准不扩大为生产清理权限。

<a id="claude-4-7"></a>
## 4.7 公共控制面与恢复约定

<a id="claude-fix-and-continue"></a>
### 修好接着跑：出问题先暂停，修好后在原 Campaign 接着跑

Claude 采用 [Framework §5.3.6](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md#536-共享控制面合同) 的对账与暂停
合同。操作顺序是：读诊断 → 修复最小范围 → 定向复核 → 受监督部署并登记工具／环境变化 → 对账 →
批准恢复范围并授权 → 续派 → 重放完成收据。永久身份边界按
[Framework §5.3.4](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md#534-失败恢复与身份边界) 办理；可信原件保留原
字节和来源，不能通过换目录、换 ID 或重建 Campaign 绕过累计与失败事实。

```bash
bash "$DRIVER/fix-and-continue.sh" "$ROUND_PARAMS" --dry-run
bash "$DRIVER/fix-and-continue.sh" "$ROUND_PARAMS" --list
bash "$DRIVER/fix-and-continue.sh" "$ROUND_PARAMS" --from "$STEP"
```

该脚本按 `CLIENT=claude` 调用 Claude 编排器，十五步为 `deploy → postdeploy → regression → item-tests →
background-validate → evolution → pre-extend → reconcile-runs → reconcile-attempt → repair → approve →
authorize → extend → accepted → recover`。它覆盖 **VC-5 封存前、非 `recovery_revision` 的候选修复轮**，
各步记录输入、输出与幂等键。部署、演进、延期、批准与授权分别消费独立凭证；仅填 `APPROVER` 不会代签。

修复通过定向回归后受监督部署，随后复核与 regression，启动独立后台验证；采集期间按资源准入让路，批次
边界发现后台失败／中止则暂停。VC-5 accept 要求当前部署后台验证通过；recover 只负责启动补跑，须等
`vc5-run-batch.out` 的 `RUN_BATCH_DONE` 与收据都成立后，再重入 `vc5-all.sh`。

<a id="claude-failure-matrix"></a>
#### 失败矩阵

本次父 run 有尚未完整收口的采集预约用 `reconcile-attempt`，否则用 `reconcile-supervisor-run`；目录里有
attempt 不足以选前者。下表的“已接入”指当前入口，完整现场认证状态仍以 §3.6、§4.0.5 为准。

| 现象 | 对账／诊断入口 | 修复后动作 | 从哪里继续 | 脚本覆盖 |
|---|---|---|---|---|
| VC-0 未完成 | `entry.sh --plan` 与步骤诊断 | 修输入／工具并重入同一入口 | 最近合法步骤，已建账本继续使用 | `entry.sh` 已接入；正式发布认证待完成 |
| VC-1 采集失败／孤儿预约 | `reconcile-attempt` | 批准预览、授权、`resume --rerun-failed` | 失败／未完成／依赖变化 Job | 已接入 |
| VC-1 seal 链或 VC-2／VC-3 父动作失败 | `reconcile-supervisor-run` | 复验阶段幂等证明后重派 | 同 attempt seal 或 N+1 批次 | `vc1-claude.sh`／`vc23-claude.sh` 已接入 |
| VC-4 构建／上传中断 | 父 run 对账 | 原身份且幂等证明成立时续作 | `vc4-all.sh`；上传用 `--resume-from upload-wait` | 已接入 |
| VC-5 未封存采集失败 | 预约对账 | 批准、授权、`vc5-claude-recover.sh` | 保留可信 Job，只补执行闭集 | `fix-and-continue.sh` 已接入 |
| 网关残留／取证进程残留 | 预约对账与 `environment-cleanup preview` | 批准清理、干净环境复核，必要时隔离 | 原恢复轮，再批准补跑 | 清理工具已接入，需独立批准 |
| 客户端收据缺失或 seal 待批准 | `vc5-pause.json`／seal 预览 | 取得真实收据或批准摘要 | `vc5-claude-seal.sh` → `vc5-all.sh` | 暂停点与离线收据合同已接；Mac 真账号采集及正式 recorder 未接 |
| 边界核对／seal／compare 批次失败 | 父 run 对账 | 按失败码修复与登记演进 | 同一候选的未完成动作 | seal／compare 已接；不合法既有边界收据先人工审计 |
| 已封存 evaluator 缺陷 | `evaluation-recover preview` | 修复、部署、演进、批准 `evaluator-defect` | 新 `b<K>`，只重评影响项 | 离线恢复已接入 |
| 已封存临时环境故障 | 评估诊断沿 `source_artifacts` 定位 J* | `transient-environment`，补采后增量封存 | `ar<k>` → 新基线 compare／accept | B4-05b 实现／验收中 |
| 恢复段中断 | 已预约用 `reconcile-attempt --recovery-revision ar<k>`；否则父 run 对账 | 按段合同批准后继段或重派 | 原段／后继段的合法闭集 | B4-05b 实现／验收中 |
| 候选源码／构建真实变化 | 对账及作废预览 | `invalidate-candidate` → `revision-open --supersedes` | VC-4 新 revision | 已接入，不能原地覆盖 |
| 批准 selector 有误 | `evaluation-recover approval-revision` 预览 | 批准新画像／基线 | 零请求重评 | 已接入；规则／场景变化另建后继 Campaign |
| 仅封存证据元数据漂移 | `harden-evidence-permissions rebind-boundary` | 核原字节／路径／类型／权限／属主，追加重绑 | 原基线只读重放 | 已接入，必须指定候选 |
| 外部门禁失败 | 父 run 对账与失败门禁收据 | `capture-candidate seal --revalidate` | 新 `v<N>` 只补门禁，再 accept | 工具已接，零请求、不重扫 |
| VC-6 阶段失败 | 父 run 对账、`delivery-chain` | 同命令重入，自动开 `dr<k>` | 失败阶段至第四阶段 | 控制面已接；ARM64 真实执行器待完成 |
| VC-6 成功阶段需重跑 | `delivery-stage-invalidate preview／apply` | 批准失效，再派 run | 指定阶段及全部下游 | 未 finalize 才允许 |
| VC-6 四阶段完成／审核中断 | 无批准 `deliver-candidate finalize` | 独立批准 finalize 批次 | 原交付包；重复幂等 | 控制面已接；生产激活 B4-08 另批 |
| 预算、账务、污染或根因上限 | 相应对账入口 | 按下表解除暂停 | 原 Campaign 当前阶段 | 不由修复脚本包办 |
| 父进程／owner 丢失 | 按预约状态分流 | 工具补收账，按 COMMIT 与许可续派 | 最近合法 checkpoint | 共享监督器已接入 |
| 完整性异常或永久身份失效 | 对账终态 | 原链只读，按身份边界建立后继 | Framework 指定阶段 | 不允许强制续跑 |

#### 暂停种类与放行

| `pause_kinds` | Claude 放行入口 |
|---|---|
| `deadline` | `deadline-extend preview／apply`；项目／Campaign／阶段分别批准 |
| `request_budget` | `request-budget-extend preview／apply` |
| `accounting` | 所属 Campaign 的 `accounting-resolve` |
| `environment` | 干净环境复核与 `environment-isolate`；有残留时先批准 `environment-cleanup` |
| `root_cause_repair` | 项目层 `record-root-cause-repair`；Campaign 账本要求停止时另走 `campaign-resume` |

`fix-and-continue` 不代做补账、环境隔离、请求预算扩展或 campaign-resume。批准输入／账本 head 漂移时重新
预览，不能复用旧批准。对账输出中的 `next_command` 与许可决定下一步，`status` 查询不能替代放行结论。

### Claude 项目总账、工具身份策略 v3、对账与只读导入

Claude 项目总账默认目录为 `<数据根>/upgrade-project-ledger-claude`，Campaign 时间账本路径从
`control_receipts.upgrade_timing` 读取。两个入口仍是共享模块 `codex_upgrade_project_ledger` 和
`codex_upgrade_timing_ledger`，创建时必须 `--client claude`；名称中的 codex 不表示可以使用 Codex 身份。

| 参数 | 当前默认值与权威输入 |
|---|---|
| VC-0～VC-6 阶段预算（分钟） | `165／120／210／75／165／210／165`，取共享计时账本 `DEFAULT_STAGE_BUDGETS_BY_CLIENT["claude"]` |
| Campaign 总预算 | 默认 360 分钟；绑定项目总账后由项目绝对截止裁剪，不能把阶段预算相加当成预计耗时 |
| 单动作墙钟与心跳 | 动作默认 90 分钟、上限 6 小时；心跳默认 5 秒，合法上限 300 秒；父批次仍取冻结的绝对 deadline |
| 同根因／同目标开放 Formal 上限 | 默认各 2；最终取项目总账冻结计划 |
| 请求预算 | 没有统一固定请求数；取项目总账冻结值及独立批准扩展 |

这些是源码默认上限，尚不是首轮真实升级耗时；首次完整升级后依据时间账本校准本表，不能为达成时长估计
削掉恢复余量或重置消耗。请求账务使用 `live-request-provenance/v2`，Claude 的计量单位为 `http_post_anthropic_model_endpoint`，按
`/v1/messages`、`/v1/messages/count_tokens`、`/v1/messages/batches` 的实际 POST 请求身份去重；复用根沿 producer 来源核账，未知请求如实保留 `estimated／unresolved`，不能以零退出码宣称零请求。

根因码表为 [`claude_root_cause_codes.json`](../tools/official_client_capture/claude_root_cause_codes.json)。
同目标根因跨 Campaign／revision／基线累计，稳定维度不包含本轮 Campaign ID；码表迁移继承旧计数。
修复凭证闭合后按前表放行，不能通过换 revision 清零。

Claude 策略 **schema 是 v3，文件名仍为**
[`claude_tool_identity_policy_v2.json`](../tools/official_client_capture/claude_tool_identity_policy_v2.json)，
由 `claude_tool_identity_policy.py` 计算双受管根 `tools/official_client_capture`、`tools/official_client_control`
及运行时绑定、符号闭包和五摘要。`tool-evolution preview／apply` 登记 wire／evidence／control 变化，policy
变化另绑定兼容、激活与受监督部署凭证；Claude 不使用 Codex 的 wire-transition 命令。

| Claude 变化 | 身份处理入口 |
|---|---|
| 目标／基线／用途、官方制品、平台／入口、隐私、账号权限或模型目录改变 | 按 Framework §5.3.4 重新冻结 Campaign |
| RequiredRules、场景、原子归属或批准画像改变 | 从最早受影响阶段建后继；仅 selector 修正走批准 revision |
| 候选源码、测试、依赖或构建真实改变 | `invalidate-candidate` → `revision-open --supersedes` |
| wire producer 或 evidence semantics 改变 | `tool-evolution` 映射影响项；已封存官方证据不得受污染 |
| 仅 control／evaluator 改变 | 登记演进，补离线门禁／评估基线 |
| 环境角色、出口、路由或网络语义改变 | 先复核环境，再按直接依赖确定失效范围 |

官方前序导入入口为 `import-predecessor-evidence --predecessor-store … --predecessor-campaign …
--mode read_only_reuse --approved-by …`，并指定目标 `--campaign-dir`；只读复用须满足本轮身份与批准绑定。
`regression_fixture` 只登记夹具，不能成为正式证据来源。旧证据与收据按形成期身份重放，新写入按当前有效
身份核对；具体事务与合同见[共享实现文档](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md)。

### Claude 依赖键、监督器与恢复

Claude 的结果键为 `SHA256(canonical_json({item_id, input_sha256, environment_sha256,
direct_dependency_sha256}))`。依赖按 `producer／evaluator／control／scenario／runtime／environment／
network／gate` 登记；由分析器和受管计划计算逐 Job 闭集，缺少证明就重跑受影响项，不能手工缩短依赖列表。

候选身份来自当前 revision、官方证据来自封存／正式导入链；候选复用同时检查执行合同、环境连续性、Job
闭环、恢复报告、封存 cover 与 producer 原始来源。Claude 不接收 Kilo／Codex 模型字段，也不把 B-09／B-10
的可选承接机制当成默认跳过门禁的开关。

#### 父 run、批次与重派

父批次只有 `compile-and-run-vc-batch` 原子入口：

```bash
python3 -m tools.official_client_capture.claude_upgrade compile-and-run-vc-batch \
  --campaign-dir "$CAMPAIGN" --phase VC-5 --sequence "$NEXT_SEQUENCE" \
  --predecessor-checkpoint "$VC4_CHECKPOINT" --action-plan "$ACTION_PLAN" \
  --state-dir "$SUPERVISOR_STATE"
```

该入口没有 `--max-wall-seconds`，截止取 Campaign 冻结值。VC-1 首批由 VC-0 同进程收口派发；其它阶段按
直接前序 checkpoint 编译，`revision-open`、演进、延期、评估恢复、交付阶段失效等控制命令在批次之间执行。
批次事务、P1～P4 崩溃和 N+1 重派机制以 Framework §5.3.6 与共享实现文档为准，不手写 COMMIT／账本事件。

有未收口预约时，四步不能省略：

```bash
python3 -m tools.official_client_capture.claude_upgrade reconcile-attempt \
  --campaign-dir "$CAMPAIGN" --attempt-id "$ATTEMPT"
python3 -m tools.official_client_capture.claude_upgrade reconcile-attempt \
  --campaign-dir "$CAMPAIGN" --attempt-id "$ATTEMPT" --approve-recovery-sha256 "$REVIEW_SHA256"
python3 -m tools.official_client_capture.claude_upgrade reconcile-attempt \
  --campaign-dir "$CAMPAIGN" --attempt-id "$ATTEMPT" --authorize-recovery-preview "$RECOVERY_PREVIEW"
ARM64_VC_ENV="$VC_ENV" bash "$DRIVER/vc5-claude-recover.sh" "$RECOVERY_PREVIEW"
```

采集已收口或尚未预约时，用 `reconcile-supervisor-run --campaign-dir … --run-dir …`，按对账许可重派；
不可依据 `status` 未显示暂停就直接启动。已发布的官方 bundle 只读复验，seal 重派不重新发布。

### Claude 评估失败局部恢复与断点续跑

当前基线为 `b<K>`，取时间账本、Store 与 COMMIT 的一致结果；`stage_sources` 指定各阶段 local／reuse，
不可写来源不能强制改为本地。当前 compare／assertions／accept 的路径由 `phase_context.py` 读取，不能按
“最新目录”猜测。四类评估裁定与精确入口见 §4.5.8。

封存前候选续跑使用普通 attempt；封存后的补采段使用 `ar<k>`，交付恢复另用 `dr<k>`，三者不能混用。
B4-05b 完整恢复段仍在实现／待验收：目标合同是 J* 定向补采、独立 ValidationAttempt、增量封存与新基线
重评；已预约段中断走 `reconcile-attempt --recovery-revision ar<k>`，未预约走父 run 对账。完成验收前保留
暂停与诊断，不以普通 resume 重采整份已封存候选。

仅封存元数据漂移时，`harden-evidence-permissions rebind-boundary --candidate-id …` 在原文件内容、相对
路径、类型、权限与属主均相同的前提下记录新边界；mtime／ctime／inode 可变，证据字节不可变。源 cover、
重绑清单和逐跳来源摘要一起保留，避免把归档复制当成重新采集。

### Claude 连续监督、时间账本与文档部署

监督器按冻结参数记录动作开始／结束／失败、等待原因、execute／reuse、请求与扫描量；默认每 5 秒心跳，
每 60 秒分类 `planning／active／waiting`。准备工作、阶段间等待、修复与恢复连续计时，worker 失联、超时、
账务或环境异常按对账合同暂停。环境恢复不会自动授权旧 run 续跑，受影响请求与 Job 窗口先核清。

| 工作 | Claude 驱动／控制入口 |
|---|---|
| 开工、部署与 VC-0 | `entry-dryrun.sh`、`entry.sh`，客户端表按 `CLIENT=claude` 选模块 |
| VC-1、VC-2／VC-3 | `vc1-claude.sh`、`vc23-claude.sh` |
| VC-4 | `vc4-requirements.sh`、`local-claude-candidate-chain.sh`、`arm64-vc4-gates.sh`、`vc4-all.sh` |
| VC-5 主链 | `vc5-all.sh` → `vc5-claude-start.sh`／`vc5-claude-seal.sh`／`vc5-claude-accept.sh` |
| VC-5 修复与补跑 | `fix-and-continue.sh`、`vc5-claude-recover.sh`，仅封存前普通 attempt |
| 封存后评估恢复 | `evaluation-recover`，真实 `ar<k>` 全链仍待验收 |
| VC-6 | `claude_upgrade_delivery` 动作计划 + `compile-and-run-vc-batch`；真实环境执行器待接入 |

Framework、本指南与共享实现文档都是受管依赖。部署清单登记完整路径、摘要与回滚点，与工具树在同一
事务切换；源码修改不等于服务器已部署。部署后重新安装／复验驱动，核对 Git 可执行位、数据根和测试树的
文档／工具／部署收据一致，详情见[共享实现文档](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md)。

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
仍须在总计划、环境收据和部署收据中冻结实际主机、网络、镜像、工具和环境摘要；环境发生变化时按
Framework §3.3 判断新建 attempt、candidate 或 Campaign。

## 6.1 当前拓扑与代理边界

```text
ImmortalWrt 192.168.9.1
└── Mac 192.168.9.99
    ├── HomeProxy：明确绕过该 Mac
    └── Surge：决定该 Mac 的代理与实际出口

Mac／控制端
├── 本机：权威源码、候选提交链，以及 Claude Desktop、Claude Code CLI、Claude Code for VS Code 本机正例
└── SSH → ARM64（官方取证、候选验收、构建与部署统一环境；数据根 /root/docker/capture-cli/data）
          └── capture-cli 与 sub2apiplus 两个受保护容器经指定出口 DMIT 69.63.195.102 出网

DMIT：线上 Sub2API 生产机，兼作 ARM64 指定出口的出口端；不做候选验收
```

HomeProxy 的绕过只说明路由器不代理该 Mac；Mac 是否直连仍由 Surge、系统代理、增强模式、CA、DNS
和具体规则决定。Mac 参与测试时必须记录这些状态及配置摘要。路径不明时，Mac 的 ClientHello、
ALPN、连接复用和 HTTP 协议不得作为官方客户端直连证据，只能作为带代理条件的功能样本。

本地 Claude Desktop、Claude Code CLI 和 Claude Code for VS Code 是三个独立官方来源产品；当前由
OfficialIngressCatalog 分别登记，再规范化到同一个 Claude Code 2.1.226 目标 Persona。它们的 UA、
Header、System 和工具目录不能互相继承，也不能为目标 Linux TLS 提供证据。Codex Desktop 不属于该
Persona。Mac 的代理状态也不会自动影响 ARM64 或 DMIT，服务器侧代理、CA、DNS 和转发条件必须分别冻结。

## 6.2 机器职责

| 节点 | 固定角色 | 可以执行 | 禁止或不能证明 |
|---|---|---|---|
| Mac `192.168.9.99` | 控制端；权威源码与候选提交链；三个官方客户端的本机正例驱动 | SSH 编排、本机候选提交链与 bundle、官方入口验证和必要的 UI／TTY 驱动 | 默认直连 TLS 权威证据；把三个来源客户端的 wire 身份互相继承；代替 ARM64 取证、构建或候选验收 |
| ARM64 | 官方取证、候选验收、构建与部署统一环境，与 Codex 同一数据根与出口策略（§4.0.3） | 受监督部署受管工具与文档；在 `capture-cli` 与断网容器 `capture-cli-synthetic` 里隔离取证；构建 linux/arm64 候选镜像；在 sub2apiplus 候选容器里验收 | 绕过出口守护准入；为取证修改宿主全局网络、CA 或生产 compose；向 `/root` 解包 |
| DMIT | 线上 Sub2API 生产机；ARM64 两个受保护容器的指定出口 | 用户管理的生产服务与出口守护的出口端 | 候选验收、官方取证；本任务连接、修改、部署或验证其 Sub2API 服务 |

每次构建仍须冻结工具、参数、产物传输路径和摘要链。

## 6.3 ARM64 官方采集纪律

官方取证在数据根 `/root/docker/capture-cli/data` 下的隔离容器里进行，不得改变宿主全局状态：

1. 真实上游步骤只在采集容器 `capture-cli`（`capture-network` 固定 IPv4 `172.30.0.10`）执行，经指定出口 DMIT
   `69.63.195.102` 出网；采集前后都通过出口守护的实时准入，旧环境收据不能替代当前准入；
2. 合成步骤只在断网容器 `capture-cli-synthetic` 执行：`network_mode: none`，与 `capture-cli` 同镜像、挂载逐项相同
   （去掉 Docker 控制面），hosts 等逐容器文件不共用来源；它不在出口准入里；
3. 目标版本按 VC-0 冻结的 `binary_sha256` 装两处：宿主 `/opt/claude-<v>/claude`（实体目录）与数据根
   `runtime/claude-<v>`（只读挂载到两个采集容器的 `/opt/claude-<v>`），宿主与两个容器内的摘要相同；官方二进制按
   版本与 SHA 独立保存，不原位覆盖；
4. 受管运行时 `runtime/claude-tooling`（node 与 TypeScript）只读挂载到 `/opt/claude-tooling`，摘要等于 Claude 身份
   策略 `runtime_bindings` 的登记值；
5. 凭据在数据根 `state/claude`（容器内 `/root/.claude`）；正式取证要求两个采集容器的凭据、CA 与全局状态文件齐全，
   且两边全局状态内容相同；
6. 不修改宿主 `hosts`、系统 CA、默认路由、防火墙、全局代理或生产 compose；hosts override 与 leaf cert 由中继在
   容器内完成并逐步恢复；
7. 每个 Job 前后对两个采集容器各采一次探针，任一快照漂移或残留取证进程即判环境污染：不补跑、停止后续 Job；
8. 活动数据目录 `0700`、文件 `0600`；证据根在数据根 `runs/` 下（容器坐标 `/capture/runs`）。

必须修改宿主全局状态的场景不得执行，应改造为隔离方案或登记为阻断。ARM64 上官方 Claude Code 生成的材料才是
当前 Linux arm64 官方 wire 主证据；候选容器里 Sub2API 生成的是实现与候选验收证据，不能反向证明官方规则；
2.1.226 的 x86_64 官方证据只读作历史 baseline；故障注入样本必须与自然失败分开。

## 6.4 ARM64 候选容器纪律

候选在 ARM64 的 sub2apiplus compose 项目里运行：应用容器 `sub2apiplus`、依赖容器 `sub2apiplus-keeper`、
`sub2apiplus-postgres`、`sub2apiplus-redis` 与采集容器 `capture-cli` 在 sub2apiplus 网络里使用固定 IP；`sub2apiplus`
另在 `proxy-network` 使用固定 IPv4 `172.25.0.3`，两个受保护容器的网络合同登记在环境收据里。候选容器不是生产
权威。每次候选验收必须绑定固定的 linux/arm64 OCI digest、image ID、源码树摘要、画像摘要、构建 ID、
AcceptanceFact 和可运行回退镜像；只替换应用容器，不重建依赖服务，也不改变五个容器的固定 IP。

验收过程中不现场编译；开跑前按驱动守卫检查根盘水位，避免 pcap、R 字节和镜像层填满系统盘。故障注入和破坏性
验证必须与候选环境的数据库、网络和账号隔离。证据完成 inventory、秘密扫描和摘要后同步到受控私有归档；同步和
独立复算完成前不得删除唯一副本。另行隔离环境允许破坏不等于可以省略 before／after、秘密扫描、恢复记录或固定
digest。

## 6.5 构建与架构边界

三个平台分别冻结、分别验证：

| 平台 | 要求 |
|---|---|
| 官方客户端 | linux/arm64：npm 平台包与原生分发同一精确版本、两份二进制逐字节相同、aarch64 glibc ELF；x86_64 证据只读作历史 baseline，不能证明 arm64 目标 |
| 候选运行 | linux/arm64 候选容器：ARM64 从同一干净提交建树、构建前端与 arm64 后端、`docker build --platform linux/arm64`；核对镜像 platform、运行容器架构和实际 image ID |
| 生产交付 | 按各生产机架构单独验证对应制品；不能用候选镜像的 arm64 身份推断生产镜像 |

无论哪个平台，都必须满足：构建源、依赖锁、工具版本和参数可复算；多架构 manifest 的平台 digest 可分别核对；
candidate 与 production 镜像身份分开；构建机只证明制品同源性，不产生官方客户端规则事实；不能用宿主架构推断
镜像架构。

## 6.6 Campaign 环境冻结

每个正式 Campaign／candidate／attempt 至少冻结：

| 类别 | 必须记录 |
|---|---|
| 主机 | 角色、标识、OS、内核、架构、时区和资源限制 |
| 客户端 | 包来源（npm 平台包与原生分发）、版本、二进制 SHA、entrypoint、隐私模式和用户目录 |
| 运行时 | node 与 TypeScript 的路径、版本与摘要（Claude 身份策略 `runtime_bindings`） |
| Sub2API | Git／tree、构建 ID、部署版本、镜像 digest、画像 ID／digest |
| 网络 | 代理、CA、DNS、hosts、端口、namespace 及 Surge／HomeProxy 影响边界 |
| 出口 | 出口策略 revision 与摘要、守护状态的 boot ID 与内核租期（环境收据记录） |
| 工具 | 采集、relay、MITM addon、脱敏、分析、finalizer 和环境探针摘要 |
| 服务 | 生产／测试标识、数据库、缓存、依赖、挂载和启动配置摘要 |
| 安全 | 目录权限、秘密扫描、等长脱敏、inventory 和清理结果 |

代理／CA／DNS、官方二进制、entrypoint、隐私模式、账号条件、candidate 源码／镜像／画像、产出侧
工具或 before／after 恢复能力变化，至少需要新 attempt；改变冻结身份或证据含义时必须新建
candidate 或 Campaign。

## 6.7 证据归档与清理

候选交付与生产边界见 §4.6；本节只规定私有证据的保存与清理。

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
