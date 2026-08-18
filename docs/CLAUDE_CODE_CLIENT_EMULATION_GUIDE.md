# Claude Code 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 Anthropic OAuth（`authMethod=claude.ai`、`apiProvider=firstParty`）
> 出站时的 Claude Code 客户端仿真
> **共享框架**：[`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)
> **当前取证基线**：`claude-code 2.1.220`——第一、二部分全部规则与证据的绑定版本；发行物身份、
> 摘要与运行时线索见 §2.1.1，后继升级目标由第四部分的受管 Campaign 冻结
> **机器台账**：`tools/official_client_capture/claude_21220/` 的规则、HitCC 覆盖与 2.1.88 覆盖
> 三份 JSON；逐规则状态以台账为准，正文数字由 §2.16 门禁强制对账
> **文档定位**：本文是 Claude Code 官方事实、版本画像、Sub2API 实现、环境职责和版本演进的唯一
> 人类可读权威入口；机器证据和 JSON 台账不得形成第二套规范
> **证据边界**：本文没有未压缩 TS 源码可用，静态规则均从官方生产 bundle 逆向建立；材料、证据、
> 规则与结论全部独立取自 Claude Code 自身，不继承 Codex 的任何事实结论
> **运行时状态**：本文已经建档 Claude Persona 目标，但当前 strict 多 Persona 运行时尚未登记或激活
> Claude；现有 service finalizer 仍属于遗留局部仿真
> **末次更新**：2026-08-17

---

# 第一部分 目标与当前边界

## 1.1 目标、范围与证据模式

本文只定义 Claude Code persona 的专属事实；跨客户端目标、等价标准和共享控制链以
[`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 为准。当前规则绑定
`claude-code 2.1.220`、Anthropic firstParty OAuth（`authMethod=claude.ai`、
`apiProvider=firstParty`），覆盖 TLS、HTTP、Header、Body、端点、连接、重试和跨请求状态。

| 范围 | 处理 |
|---|---|
| Claude Code firstParty OAuth | 本文对齐目标 |
| API Key、Bedrock、Vertex、Foundry | 范围外；只可作为差分对照 |
| 遥测与非必要流量 | 仅记录配置与可达性，不进入行为一致性维度；官方允许关闭，零流量不得计为差异 |
| 机器环境 | 是证据身份；具体职责与限制见第六部分 |

当前全部运行证据使用 `essential-traffic` 模式。2.1.220 bundle 的隐私状态为：

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

# 第二部分 Claude Code 2.1.220 规则画像

本部分定义规则成立所需的证据标准、观测边界和当前基线的 57 个编号项。它回答「什么行为可以成为
客户端规则」；目标版本如何执行取证并生成新规则，只由第四部分规定。

## 2.1 证据、准入与复算

### 2.1.1 权威材料与观测通道

| 等级 | 权威材料 | 能证明 | 不能证明 |
|---|---|---|---|
| A1 主 | 2.1.220 Linux 官方 npm 发行物与生产 bundle | 当前控制流、常量、条件、sink 和语义锚点 | 未触发分支的运行结果；跨版本／跨平台符号同一性 |
| A1 交叉 | 2.1.220 Darwin 官方发行物 | 跨平台语义复核 | Linux wire 或符号级结论 |
| A2 | 已锁定且能证明对应的 Bun／SDK 源码 | 自动 Header、序列化、连接和 TLS 机制 | 未证明对应构建的当前行为 |
| A3 | Claude Code 2.1.88 TS 源码 | 历史机制词典 | 2.1.220 当前规则 |
| A4 | HitCC 2.1.197 | 近期线索地图 | 独立规则证据 |

| 通道 | 证明边界 |
|---|---|
| P | 原始 pcap 中的 ClientHello、SNI、TLS 扩展、ALPN、连接和时序 |
| R | 等长脱敏的应用层原始字节，可证明请求线、Header 编码、Body、压缩和帧 |
| J | MITM／解析记录；J-raw 只证明解析后的 H1 Header 字面、顺序、重复项和 Body 语义 |
| M | 环境、产物、场景、工具、结果和摘要的身份绑定 |

`strings`、旧源码和 HitCC 只用于定位；静态肯定结论必须回到当前 bundle 的控制流和网络 sink，并由
对应 P／R／J 运行正例闭环。J 不是 wire 字节，MITM 会改变 TLS，原始 MITM JSONL 属 `raw_private`。

| 角色 | npm 包 | 二进制 SHA-256 |
|---|---|---|
| Linux 主静态与 wire 基准 | `@anthropic-ai/claude-code-linux-x64@2.1.220` | `674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863` |
| Darwin 交叉复核 | `@anthropic-ai/claude-code-darwin-arm64@2.1.220` | `8addc857f3fe64d5a0368af9ee50321b50afb4a6918ba3ef018ab84f5dbbe081` |

来源清单还必须绑定 registry URL、`dist.integrity`、下载时间、tgz、平台、架构和内嵌构建；SHA-256
只能证明文件同一。两平台内嵌 Bun 均为 `1.4.0+f6d0fcd24`；J 上报的
`X-Stainless-Package-Version: 0.94.0` 只证明 Header 值，不能单独锁定 SDK 实现。

Bun SEA 提取必须从 Mach-O `__BUN/__bun` 或 ELF `.bun` section 校验 trailer、Offsets 和模块记录。
正式身份由产物锚点（来源、平台、二进制／bundle／提取器摘要）与语义锚点（稳定字面、sink 和
标识符 α-归一化子树摘要）共同构成；minify 符号、行号和字节跨度只作定位。

### 2.1.2 准入与观测边界

`SPEC-*` 只是命题 ID。只有原子命题同时满足以下条件，才能标为 `verified`：

1. 官方来源、二进制、平台、入口、配置、采集器和运行环境完整绑定；
2. 静态解释来自当前产物或已证明等价的依赖，并确认到网络 sink；
3. 使用能观察该命题的 P／R／J／M，条件项具有正负例、单变量对照和独立重复；
4. 全称结论具有无目标字段预筛的发现、静态枚举和声明范围内的场景覆盖；
5. 规则、证据、画像、实现断言和秘密扫描可复算且无未分类项。

| 命题类型 | 最低闭环 |
|---|---|
| TLS／SNI／ALPN | 同平台、同二进制的 direct P 与独立重复 |
| H1 Header 大小写／顺序 | J-raw，并明确客户端到 MITM 的解析边界；原始编码仍需 R |
| Header／Body 存在和值 | 当前 sink 可达证据加 J 或 R；条件项必须差分 |
| 连接、重试、跨请求状态 | P／R／J 流级关联、成功／失败输入和重复 |
| 端点全集／全称否定 | 静态 sink 枚举、无 host 预过滤发现和明确场景边界 |

当前观测边界：

- Linux 同 SHA 产物是主基准，Darwin 只作交叉复核；minify 符号不得跨平台引用。
- `claude -p` 只代表 `sdk-cli`；`cli` 需要真实 TTY。logout 会重置 onboarding，不能把首次引导误判为采集失败。
- J-raw 不证明原始请求行、分隔符或 Body 字节；这些命题必须使用 R。
- direct BPF 的 host 预过滤不能证明端点全集；端点发现必须使用 `CAPTURE_BPF_ALL_HOSTS=1`。
- `--fault-spec` 只证明客户端收到受控故障后的反应，不能与自然错误样本混用。
- 账号、scope、远程 feature、模型能力、响应和隐私模式都是自变量。
- 当前证据均为 `essential-traffic`；`default` 模式的端点集合必须另采。
- 原采集组织失效后更换了组织；现有样本相容不等于账号或订阅档位全称无影响。

### 2.1.3 复算入口与工具能力

日常门禁见 §2.16；它只读但依赖被 `.gitignore` 排除的完整 `local-analysis/` 证据树。主要复算入口：

```bash
python3 tools/official_client_capture/pcap_clienthello.py --by-subdir \
  --dir local-analysis/captures/official-egress-final-review-fix-20260727-094500/official-client/oauth-oauth-p0p2-zstd-final-20260726T1420Z/direct/claude-http

python3 tools/official_client_capture/extract_claude_bundle.py \
  --binary local-analysis/sources/claude-code-2.1.220-official/package-linux-x64/claude \
  --expected-sha256 674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863 \
  --out-dir <提取目录> \
  --emit-anchors local-analysis/sources/claude-code-2.1.220-official/bundle-anchors-linux-x64.json \
  --emit-reachability-index local-analysis/sources/claude-code-2.1.220-official/reachability-index-linux-x64.json
```

`extract_claude_bundle.py` 会拒绝摘要不符、非 Bun SEA 和 trailer 篡改；Darwin 用 `--check-only`。
`claude_bundle_reachability.py` 只证明“最近顶层声明加前后 20 KB 窗口内存在 sink”，不是调用图或
数据流证明。声明分段和顶层符号识别均为启发式；静态锚点可否证跨版本同一性，但不能单独证明
运行时存在或缺失。

当前仍需补：TUI、compact、其他非主会话、更多条件变量、500 之外状态码、`Retry-After`、精确重试
终止边界，以及现有 R 的完整 M、无 host 预过滤发现、全生命周期 flow ID 和实现断言映射。

## 2.2 当前规则分组与验收范围

本章当前共 57 个编号项。“Sub2API 需对齐项”表示该编号属于实现 §1.1 目标时必须处理的责任；
“当前验证状态”表示官方行为证据的充分度，不代表 Sub2API 已经实现。规则正文统一使用 §2.17 的
四字段规则卡；本节总表和责任清单决定实现边界，机器台账保存详细运行与分母。

<!-- CLAUDE_ALIGNMENT_SUMMARY_START -->
| 分组 | 条数 | 当前验证状态 | Sub2API 需对齐项 |
|---|---:|---|---:|
| **① TLS** | **3** | 🟡 3 | **3** |
| **② HTTP 协议** | **1** | 🟡 1 | **1** |
| **③ Header** | **26** | ✅ 10；🟡 15；排除 1 | **25** |
| **④ Body** | **16** | 🟡 16 | **16** |
| **⑤ 端点** | **4** | 🟡 4 | **4** |
| **⑥ 连接与重试** | **3** | ✅ 2；🟡 1 | **3** |
| **⑦ Beta 条件机制** | **1** | 🟡 1 | **1** |
| **⑧ 响应兼容** | **3** | 下游响应记录 | **0** |
| **合计** | **57** | **✅ 12；🟡 41；排除 4** | **53** |
<!-- CLAUDE_ALIGNMENT_SUMMARY_END -->

对固定转发到 Anthropic firstParty OAuth 上游的实现口径如下：

```text
官方客户端入站 ─┐
                 ├─→ 规范化语义请求 → Claude220Profile
第三方 API 入站 ─┘                         ↓
                     Lifecycle／State／Endpoint／Header／Body／Connection／Transport
                                               ↓
                                      Anthropic OAuth 上游
```

官方客户端与第三方标准 API 的入站必须先归一化为等价语义请求，再进入同一个不可变版本画像和同一组
最终定型层。入站 `User-Agent`、客户端版本、Header 顺序或客户端类别不得直接决定出站形态。条件规则
只在画像条件成立时输出；动态值只对齐格式、来源、关联关系和生命周期，不固定某次抓包值；未被画像
消费的第三方入站 Header 不得直接透传到官方上游。

对固定转发到 Anthropic firstParty OAuth 上游，Sub2API 当前需要对齐 **53 项**：2.2.1 的 32 项主画像
责任，加 2.2.2 的 21 项条件责任。12 项已经达到 §2.1 准入，可直接作为硬验收合同；41 项只有有限
运行观察，只能作为带范围的暂行画像，实施后仍不得声称“53/53 已验证”或“完整画像”。4 项排除项
不属于客户端出站实现。

本轮原 5 个待补证编号已全部原子化并闭环，所以**已编号待补证为 0**。这不表示发现工作已经穷尽：
HitCC、2.1.88 源码和未编号 `CAND-*` 仍有未闭合线索。尤其是 `CAND-AUTH-BEARER`、完整
ClientHello、原始 HTTP Header 合同、工具／compact／其他非主会话、辅助端点，以及 500 之外的
错误类别和 `Retry-After` 仍会阻止 §1.1 最终封口。

机器责任枚举仍为
`egress_required／egress_conditional／egress_pending_evidence／downstream_response_compat／none_superseded`，
本轮数量依次为 32／21／0／3／1；前两类合计即 Sub2API 需对齐的 53 项。

<!-- CLAUDE_ALIGNMENT_REQUIRED_START -->
### 2.2.1 主画像必需：32 项

`SPEC-TLS-001`、`SPEC-TLS-002`、`SPEC-TLS-003`、`SPEC-PROTO-001`、
`SPEC-HDR-001`、`SPEC-HDR-002`、`SPEC-HDR-003`、`SPEC-HDR-004`、`SPEC-HDR-005`、
`SPEC-HDR-006`、`SPEC-HDR-007`、`SPEC-HDR-008`、`SPEC-HDR-012`、`SPEC-HDR-013`、
`SPEC-HDR-020`、`SPEC-BODY-001`、`SPEC-BODY-002`、`SPEC-BODY-003`、`SPEC-BODY-007`、
`SPEC-BODY-008`、`SPEC-BODY-009`、`SPEC-BODY-010`、`SPEC-BODY-011`、`SPEC-BODY-012`、
`SPEC-BODY-013`、`SPEC-BODY-014`、`SPEC-BODY-015`、`SPEC-BODY-016`、`SPEC-EP-001`、
`SPEC-EP-002`、`SPEC-EP-003`、`SPEC-EP-004`。
<!-- CLAUDE_ALIGNMENT_REQUIRED_END -->

“必需”表示属于主画像或主画像生命周期，不表示每个推理请求都出现该现象。例如 `SPEC-HDR-013`
需要跨请求才能观察复用；`SPEC-EP-002` 是生命周期责任，不能据此在每个推理请求前机械发送探测。
这 32 项当前均为 🟡「已观察」，可以做带范围的暂行实现，但还不是正式验收完成数。

<!-- CLAUDE_ALIGNMENT_CONDITIONAL_START -->
### 2.2.2 条件触发时必需：21 项

`SPEC-HDR-009`、`SPEC-HDR-010`、`SPEC-HDR-014`、`SPEC-HDR-015`、`SPEC-BODY-004`、
`SPEC-BODY-005`、`SPEC-BODY-006`、`SPEC-CONN-001`、`SPEC-BETA-001`、
`SPEC-HDR-016`、`SPEC-HDR-021`、`SPEC-HDR-022`、`SPEC-HDR-017`、`SPEC-HDR-023`、
`SPEC-HDR-018`、`SPEC-HDR-024`、`SPEC-HDR-019`、`SPEC-HDR-025`、`SPEC-HDR-026`、
`SPEC-CONN-002`、`SPEC-CONN-003`。
<!-- CLAUDE_ALIGNMENT_CONDITIONAL_END -->

触发条件分别来自官方画像环境、子 agent、轮次、工具状态、服务端故障和 beta。第三方入站只有在
规范化语义明确表达同一条件时才能触发，不能因为携带同名 Header 就绕过画像条件。
其中 `HDR-016/017/018/019/021..026`、`CONN-002/003` 共 12 项为 ✅「已验证」；其余 9 项为
🟡「已观察」。条件未成立时必须省略相应形态，不能为了固定 Header 数而发送空值。

<!-- CLAUDE_ALIGNMENT_PENDING_START -->
### 2.2.3 已编号待补证：0 项

原 5 个候选已拆成 12 个原子编号并由本轮 22 个完整 M 运行闭环；当前没有
`egress_pending_evidence` 编号。未编号线索仍保留在 2.12 候选台账，不得把“0 项”解释为候选清零。
<!-- CLAUDE_ALIGNMENT_PENDING_END -->

<!-- CLAUDE_ALIGNMENT_EXCLUDED_START -->
### 2.2.4 不计入客户端 egress：4 项

`SPEC-HDR-011`、`SPEC-RESP-001`、`SPEC-RESP-002`、`SPEC-RESP-003`。
<!-- CLAUDE_ALIGNMENT_EXCLUDED_END -->

其中 `SPEC-HDR-011` 已由 `SPEC-HDR-020` 与 `SPEC-BODY-007` 取代；三条 `RESP` 规则属于下游响应
兼容责任。这里的正式排除项只有上述 4 个编号；“辅助端点”没有被 §2.2.4 排除。当前
`CAND-EP-COUNTTOKENS`、`CAND-EP-AUXILIARY` 和 `CAND-EP-FULLSET` 仍是未编号候选，必须在 FW-E
完成发现并按 Framework 的 `persona_strict／non_persona_managed／denied` 三态定性。只有晋升为
`persona_strict` 的候选才新增原子 SPEC、`PAIR-<SPEC-ID>` 并进入对应 SupportEnvelope；
`non_persona_managed` 虽不计入 53 项 wire 对齐责任，仍必须进入受管 route／Sink 和运行时门禁。

每个 `target_1_1=true` 的编号预留成对验收编号 `PAIR-<SPEC-ID>`。实现阶段必须让官方入口和第三方
入口分别执行该断言；对于相同规范化语义与画像条件，两侧必须命中相同的可见结果。动态字段按规则声明的格式、
相等关系和生命周期比较，不比较一次抓包中的字面值。当前实现位置和断言代码尚未审计，统一记为
`not_assessed`。证据已验证只表示官方规则闭环，不表示 Sub2API 已实现或通过成对验收。

## 2.3 当前证据状态与具名范围

权威范围为 Linux x86_64、二进制 SHA `674f61f2…`、`sdk-cli`、firstParty OAuth、官方 base URL 和
`essential-traffic`。当前正式已验证为以下 **12 项**：
`SPEC-HDR-016/017/018/019/021/022/023/024/025/026`、`SPEC-CONN-002/003`；其余 41 项仍是
有限样本「已观察」。这证明部分原子规则闭环，不代表完整 Claude Code 2.1.220 画像。

| 证据根 | 状态与边界 |
|---|---|
| `claude-spec-baseline-20260801/` | 22 个 manifest：18 complete、4 failed；旧 P／J 缺完整 M，R 只作补充观察 |
| `claude-code-2.1.220-pending-evidence-20260801/runs/` | 22 个运行全部 complete，只闭环上述 12 项；不能替旧 P／R／J 自动补 M |

22-run 分析摘要为 `d7b6681e79531b0a84600c83bb2116eeb3c3932955e8e7f86e292e23e810ea11`，
Campaign 绑定摘要为 `b2bdc967114b9bd4001ee8da0feec0f79e5a2490fc9bf03dc59780298a454d98`；终态秘密
扫描器摘要为 `af062e03f0e45315562577c07580185232d48b7db13be36cc31cba7d0626c035`，22 份回执
均通过。采集执行源按各自 host receipt 闭合为 12／8／1／1 四组摘要，不能写成全部运行同源。

规则正文用以下范围名消除重复；范围名只复用条件，不继承其他规则的结论：

| 范围 | 精确定义或关键限制 |
|---|---|
| `common_scope` | Linux 主 SHA、`sdk-cli`、firstParty OAuth、essential-traffic 和 Sonnet 5 的共享证据身份；不是任何规则的自动分母 |
| `SCOPE-BASELINE` | Linux 主 SHA、`sdk-cli`、firstParty OAuth、官方 base、essential-traffic、Sonnet 5、前台主会话和已采 s1／s2／s4；是抓包基线，**不是“CLI 无参数默认值”**。采集器会显式构造参数，但旧 manifest 没有归档 argv |
| `SCOPE-DIRECT` | `SCOPE-BASELINE` 的非 MITM TLS 连接 |
| `SCOPE-J6` | 四个具名 J-raw 文件中的全部 6 个官方推理记录；manifest 预设 `target_hosts=[api.anthropic.com]`，并按 `/messages` 路由，故 EP-001／004 无独立分母；另外 16 条有限样本规则仍可声明独立于各自待证字段的分母 |
| `SCOPE-P8` | 四个具名 direct pcap 的全部 8 个 ClientHello，不按 ALPN、SNI 或扩展值筛选 |
| `SCOPE-ALLHOSTS-P4` | 两个 all-hosts direct pcap 的全部 4 个 ClientHello |
| `SCOPE-BASELINE-NO-TOOLS` | 已采 s1／s2 的 `tools: []` 记录；argv 仍受旧 M 缺口限制 |
| `SCOPE-SUBAGENT-L1` | 一层 general-purpose 子 agent；当前只有缺完整 M 的 R 观察 |
| `SCOPE-FAULT` | baseline 加 manifest 记录的单一受控故障 |
| `SCOPE-PND-NEG2` | 两轮无 client-app、container、remote-session 注入的完整 M 负例 |
| `SCOPE-PND-HDR016/017/018` | client-app、container ID、remote-session ID 各两轮单变量完整 M 正例 |
| `SCOPE-PND-COMBO2` | 两轮三环境 Header 同开且 agent 深度 `0→1→2→1→0` |
| `SCOPE-PND-AGENT6` | 深度 1、2、3 各两轮往返链 |
| `SCOPE-PND-RETRY6` | 500、无 `Retry-After`、count=3／5／9 各两轮 |
| `SCOPE-RESPONSE` | baseline 对应的官方上游响应，只属于 response compatibility |

### 2.3.1 规则卡引用的精确向量

以下内容补足规则卡中的“文档所列”或计数引用，不能从标题自行推断：

- `SPEC-HDR-001` 基础 Header 顺序为：`Accept`、`Authorization`、`Content-Type`、`User-Agent`、
  `X-Claude-Code-Session-Id`、`X-Stainless-Arch`、`X-Stainless-Lang`、`X-Stainless-OS`、
  `X-Stainless-Package-Version`、`X-Stainless-Retry-Count`、`X-Stainless-Runtime`、
  `X-Stainless-Runtime-Version`、`X-Stainless-Timeout`、`anthropic-beta`、
  `anthropic-dangerous-direct-browser-access`、`anthropic-version`、`x-app`、
  `x-client-request-id`、`Connection`、`Host`、`Accept-Encoding`、`Content-Length`。R 另观察到
  `POST /v1/messages?beta=true HTTP/1.1`、名值间冒号加一个空格、行尾 CRLF；R 缺完整 M，仍属 observed。
- `SPEC-HDR-003` 的 Sonnet 5 九项 beta 顺序为：`claude-code-20250219`、`oauth-2025-04-20`、
  `interleaved-thinking-2025-05-14`、`thinking-token-count-2026-05-13`、
  `context-management-2025-06-27`、`prompt-caching-scope-2026-01-05`、
  `mid-conversation-system-2026-04-07`、`effort-2025-11-24`、`extended-cache-ttl-2025-04-11`。
- `SPEC-BODY-001` 的十键集合为：`context_management`、`max_tokens`、`messages`、`metadata`、
  `model`、`output_config`、`stream`、`system`、`thinking`、`tools`。R 观察到序列化顺序为
  `model → messages → system → tools → metadata → max_tokens → thinking → context_management →
  output_config → stream`；该顺序仍受 R 缺完整 M 的边界约束。
- `SPEC-EP-002` 的 `HEAD /api/hello HTTP/1.1` 依次携带 `Connection`、`User-Agent`、`Accept`、
  `Host`、`Accept-Encoding`，无 Body；两轮均先于推理请求并使用不同连接。

`verification_denominators` 只表示捕获／路由没有按待证字段筛选，不代表 `verified`；正式状态只读
`status.disposition`。机器台账为：

- `tools/official_client_capture/claude_21220/rules_2_1_220.json`；
- `tools/official_client_capture/claude_21220/hitcc_2_1_197_coverage.json`；
- `tools/official_client_capture/claude_21220/source_2_1_88_coverage.json`。

<!-- CLAUDE_RULE_CARDS_START -->

## 2.4 TLS

### SPEC-TLS-001 ClientHello 扩展类型序列固定

- **命题与范围**：在已列 complete 的 Linux x86_64、sdk-cli、firstParty OAuth 样本中，ClientHello 扩展类型序列均为 0,23,65281,10,11,35,16,5,13,18,51,45,43,21。 范围：common_scope；只保留样本内一致性，不推出未采集模型、账号或 provider 的全称不变性。
- **证据与缺口**：通道 P／M；`BASELINE-P`、`HISTORICAL-220`；缺口：原子拆分与完整分母。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：同平台复刻该扩展序；不得把其他平台或其他 TLS 栈的画像套用于此。
- **状态**：🟡 **已观察**；已选 P 样本支持 retained_claim，但原 ID 同时包含精确序列与跨场景全称不变性，非原子命题，故只计 observed。

### SPEC-TLS-002 只 offer `http/1.1` 一种 ALPN

- **命题与范围**：已声明 direct 样本的 ClientHello 只 offer http/1.1。 范围：SCOPE-P8；不推出其他 provider 或平台永不 offer h2。
- **证据与缺口**：通道 P／M；`BASELINE-P`、`HISTORICAL-220`；缺口：完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该样本画像只 offer `http/1.1`；出现 `h2` 与当前观察不一致。
- **状态**：🟡 **已观察**；选定 pcap 的全部 8 个 ClientHello 均只 offer http/1.1；但运行 manifest 未满足 §2.1 完整 M 准入，故不升格。

### SPEC-TLS-003 所选 allhosts 样本的 SNI

- **命题与范围**：SCOPE-ALLHOSTS-P4 的每个 ClientHello 的 SNI 恰为 api.anthropic.com。 范围：SCOPE-ALLHOSTS-P4；只覆盖 essential-traffic、tcp/443、s1 两轮，不是 host 或端点全集。
- **证据与缺口**：通道 P／M；`ALLHOSTS`；缺口：完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该有限画像的 TLS SNI 使用 `api.anthropic.com`。
- **状态**：🟡 **已观察**；两个 allhosts pcap 中的全部 4 个 ClientHello 均携带 api.anthropic.com SNI；但运行 manifest 未满足 §2.1 完整 M 准入。

## 2.5 协议与连接

### SPEC-PROTO-001 应用层协议为 HTTP/1.1

- **命题与范围**：已声明推理请求的应用层协议为 HTTP/1.1。 范围：SCOPE-J6；限已枚举的六个解析记录。
- **证据与缺口**：通道 J／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该有限画像使用 HTTP/1.1。
- **状态**：🟡 **已观察**；六个选定 J-raw 请求均解析为 HTTP/1.1；但运行 manifest 未满足 §2.1 完整 M 准入，故不升格。

## 2.6 Header

### SPEC-HDR-001 基线路径的 22 项 header 名与顺序

- **命题与范围**：基线 Sonnet 推理路径发送文档所列 22 项 header 名称、大小写和相对顺序；条件插槽不在本条正式命题内。 范围：common_scope；基线无探针、主会话推理请求。
- **证据与缺口**：通道 J；`WIRE-DEFAULT`、`BASELINE-J`；缺口：hdr-combo-r1、hdr-combo-r2。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：按此名称、大小写与相对顺序输出；`x-client-request-id` 与 `X-Claude-Code-Session-Id` 每请求变化，只固定位置不固定值。
- **状态**：🟡 **已观察**；基线样本支持 22 项名称、大小写和相对顺序，但原 ID 合并 H1 字节格式与条件插槽且 R 缺完整 M，故只计 observed。

### SPEC-HDR-002 User-Agent

- **命题与范围**：基线 sdk-cli 样本发送 User-Agent: claude-cli/2.1.220 (external, sdk-cli)。 范围：SCOPE-J6；不覆盖 TUI、client-app、workload 或其他可选 UA 段。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`、`A1-CANDIDATES`、`A1-SOURCE-MANIFEST`；缺口：UA 字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：`sdk-cli` 有限画像按此值输出；不得把该值用于其他 entrypoint。
- **状态**：🟡 **已观察**；六个选定 J-raw 请求均复验精确 UA；但 A1 只有局部 sink 近邻且运行 manifest 未满足 §2.1 完整 M 准入。

### SPEC-HDR-003 Sonnet 5 基线 `anthropic-beta` 九项序列

- **命题与范围**：SCOPE-BASELINE 的 Sonnet 5 请求发送文档所列 9 项 anthropic-beta 序列。 范围：SCOPE-BASELINE；Opus 仅一轮观察，Haiku 没有成功正例，二者均不进入本条有限 Sonnet retained claim。
- **证据与缺口**：通道 J；`BASELINE-J`；缺口：relay-haiku-r1、relay-haiku-r2。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：Sonnet 5 按此九项与顺序输出；不得把 33 条注册表全量发送。
- **状态**：🟡 **已观察**；Sonnet 基线样本支持九项序列，但原 ID 同时合并 Opus 与 Haiku 分支且非原子，故只计 observed。

### SPEC-HDR-004 `anthropic-version: 2023-06-01`

- **命题与范围**：已声明推理请求发送 anthropic-version: 2023-06-01。 范围：common_scope。
- **证据与缺口**：通道 A1／J／M；`BASELINE-J`、`WIRE-DEFAULT`；缺口：构造点到请求 sink 的数据流、完整 M 与有限分母。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：按此值输出。
- **状态**：🟡 **已观察**；J 样本支持该固定值，但尚未把当前构造点到请求 sink 的静态闭环与完整样本分母同时绑定，故只计 observed。

### SPEC-HDR-005 `Accept-Encoding: gzip, deflate, br, zstd`

- **命题与范围**：基线推理请求发送 Accept-Encoding: gzip, deflate, br, zstd。 范围：common_scope；绑定当前 Bun/平台。
- **证据与缺口**：通道 A1／J／M；`WIRE-DEFAULT`、`BASELINE-J`；缺口：当前 Bun/SDK 构造来源、完整 M 与有限分母。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：按此字面输出，包括分隔符空格与 `zstd` 的存在。
- **状态**：🟡 **已观察**；J 样本支持解析值，R 仅作补充观察；当前构造点到 sink 的静态闭环和完整分母未同时绑定，故只计 observed。

### SPEC-HDR-006 `X-Stainless-*` 八项取值

- **命题与范围**：当前 Linux/Bun 基线样本发送 Arch=x64、Lang=js、OS=Linux、Package-Version=0.94.0、Retry-Count=0、Runtime=node、Runtime-Version=v26.3.0、Timeout=600。 范围：common_scope；平台、运行时和 SDK 版本变化即失效。
- **证据与缺口**：通道 J／M／L；`BASELINE-J`、`RETRY-FAULT`、`RETRY-KILL`；缺口：八个 Header 原子拆分、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：按此输出。`Retry-Count` 在应用层重试时保持 `0`，不得为了「看起来像重试」而递增。
- **状态**：🟡 **已观察**；已采样本支持八项取值，但原 ID 合并八个 Header 与重试分支，非原子命题，故只计 observed。

### SPEC-HDR-007 `x-app` 与 `anthropic-dangerous-direct-browser-access`

- **命题与范围**：SCOPE-BASELINE 的前台样本同时发送 anthropic-dangerous-direct-browser-access:true 与 x-app:cli。 范围：common_scope；只覆盖前台。
- **证据与缺口**：通道 J／A1；`BASELINE-J`、`WIRE-DEFAULT`、`A1-REACHABILITY`；缺口：cli-bg 运行正例。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：前台输出上述两个值；后台画像必须等待 `CAND-BG-SESSION` 闭环。
- **状态**：🟡 **已观察**；前台基线样本支持两个可见值，但原 ID 合并多个 Header 与未采 cli-bg 分支，非原子命题，故只计 observed。

### SPEC-HDR-008 基线不发送 `anthropic-dispatch-id`

- **命题与范围**：基线 firstParty OAuth 推理请求不发送 anthropic-dispatch-id。 范围：common_scope；只证明环境注入路径负例，不否定远程 gate 正例。
- **证据与缺口**：通道 J／M／A1；`PROBE-DISPATCH`、`A1-CANDIDATES`；缺口：tengu_cedar_lattice=true 正例。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：基线不发送该 header；远程 gate 画像取得正例前不得臆造取值。
- **状态**：🟡 **已观察**；基线负例支持 Header 缺席，但原 ID 合并环境可达性、远程 gate 与重试移除机制，非原子命题，故只计 observed。

### SPEC-HDR-009 `CLAUDE_CODE_ADDITIONAL_PROTECTION` 控制的条件 header

- **命题与范围**：CLAUDE_CODE_ADDITIONAL_PROTECTION=1 的两轮正例发送 x-anthropic-additional-protection:true，基线负例不发送。 范围：common_scope；只验证真值字符串 1。
- **证据与缺口**：通道 J／M／A1；`PROBE-PROTECTION`、`BASELINE-J`、`A1-CANDIDATES`；缺口：触发、值与槽位原子拆分。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：仅在该条件成立时输出，并保持第 16 位；条件不成立时不得出现。
- **状态**：🟡 **已观察**；两轮受控输入支持存在性和值，但原 ID 同时合并槽位与分支机制，非原子命题，故只计 observed。

### SPEC-HDR-010 单行 `ANTHROPIC_CUSTOM_HEADERS` 注入

- **命题与范围**：单行 X-Egress-Probe: probe-alpha 在两轮注入中出现于 X-Claude-Code-Session-Id 之后、X-Stainless-Arch 之前；基线负例不出现。 范围：common_scope；仅单行单 header。
- **证据与缺口**：通道 J／M／A1；`PROBE-CUSTOM`、`A1-CANDIDATES`；缺口：多行 ANTHROPIC_CUSTOM_HEADERS 正例、同名覆盖正例。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该变量未设置时不得出现任何额外 header；产品若不开放此能力，无需实现。
- **状态**：🟡 **已观察**；两轮单行正例支持存在性和值，但原 ID 合并解析、槽位与覆盖优先级，非原子命题，故只计 observed。

### SPEC-HDR-011 已取代：billing 载体混淆

- **命题与范围**：基线样本的 HTTP header 列表不含 x-anthropic-billing-header；但请求体 system 中存在以该字面开头的 attribution 文本，含 cc_version、cc_entrypoint、经 Bun 改写后的 cch，并可条件包含 cc_prev_req 或 cc_is_subagent。 范围：common_scope；明确区分 HTTP header 层与 JSON body 的 system 文本层。
- **证据与缺口**：通道 J；`BASELINE-J`、`WIRE-DEFAULT`、`WIRE-SUBAGENT`、`A1-CANDIDATES`；缺口：WIRE-SUBAGENT 的完整 M 绑定、会话生命周期与嵌套差分。详细证明边界见机器台账。
- **Sub2API 实现要求**：不实现本编号；编号不得复用，也不得重新合并 Header 缺席与 Body 存在两个命题。
- **状态**：**已取代／已观察**；旧规则正确观察到 HTTP header 集合无同名字段，却错误推出 billing attribution 不出站；R 证明该字符串位于请求体 system 文本块。

### SPEC-HDR-020 基线 HTTP Header 不含 `x-anthropic-billing-header`

- **命题与范围**：SCOPE-J6 的主会话推理请求中，HTTP header 集合不包含 x-anthropic-billing-header。 范围：SCOPE-J6；缺完整 M 的子 agent R 观察不纳入本条，也不推出 TUI、workload 或其他 provider 路径。
- **证据与缺口**：通道 J／M；`BASELINE-J`、`PROBE-BASELINE-J`、`A1-CANDIDATES`、`A1-SOURCE-MANIFEST`；缺口：完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该有限画像不新增同名 HTTP Header，并与 `SPEC-BODY-007` 的 Body 载体分开处理。
- **状态**：🟡 **已观察**；六个选定 J-raw 请求的完整 header 数组均不含该 HTTP Header；但运行 manifest 未满足 §2.1 完整 M 准入，故不升格。

### SPEC-HDR-012 `x-client-request-id` 样本内无碰撞

- **命题与范围**：已采集请求的 x-client-request-id 均为 UUID 形态且样本内未复用。 范围：common_scope；结论只到样本内唯一，不推出生成算法。
- **证据与缺口**：通道 J／A1；`BASELINE-J`；缺口：2.1.220 生成器到网络 sink 的数据流证明。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：实现应每请求生成新值；在当前 bundle 的生成器可达链或统计停止门槛闭环前， 不把“永不复用”写成已验证全称。
- **状态**：🟡 **已观察**；J 中可以复算样本内唯一性，但未静态证明生成器与每次调用边界。

### SPEC-HDR-013 同一会话复用 `X-Claude-Code-Session-Id`

- **命题与范围**：已采集 s2/s4 内，同一会话的多个请求共用 X-Claude-Code-Session-Id。 范围：common_scope；不覆盖 compact、resume、后台或进程重启边界。
- **证据与缺口**：通道 J／M；`BASELINE-J`；缺口：会话生命周期边界证明。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：在会话内保持同值；跨会话生成策略仍须与 `CAND-HDR-SESSION-RANDOM` 一起补证。
- **状态**：🟡 **已观察**；已声明 s2/s4 支持会话内共享，但原 ID 合并会话内复用、跨会话差异与生成机制，非原子命题，故只计 observed。

### SPEC-HDR-016 `x-client-app` 的条件存在性

- **命题与范围**：在已采 firstParty OAuth print 路径中，未设置 CLAUDE_AGENT_SDK_CLIENT_APP 时 x-client-app 缺席；设置为非空值时 x-client-app 存在。 范围：2.1.220 Linux x86_64；s1；两轮未设置负例与两轮 probe-app-21220 正例。只验证存在性条件，不合并值或 UA 命题。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-HDR-NEGATIVE-COMPLETE`、`PROBE-HDR-CLIENTAPP-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：只从受控 `Claude220Profile.client_app` 生成该 Header；该画像字段未设置时省略。 不得把官方或第三方入站携带的同名 Header 直接透传到 OAuth 上游。
- **状态**：✅ **已验证**；目标版本静态构造点唯一命中；两轮未设置负例与两轮非空唯一值正例均以完整 M 归档。

### SPEC-HDR-021 `x-client-app` 等于受控 client-app 值

- **命题与范围**：CLAUDE_AGENT_SDK_CLIENT_APP 为非空值时，x-client-app 的值与该 client_app 值逐字节相同。 范围：2.1.220 Linux x86_64；firstParty OAuth print s1；两轮 probe-app-21220 正例。只约束已采非空值的等值关系。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-HDR-CLIENTAPP-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：从 `Claude220Profile.client_app` 取值并写入；不得从入站 Header 猜值或另行改写。 对未覆盖字符集或长度的输入应由画像校验拒绝，不能把本条外推为任意字节透传规则。
- **状态**：✅ **已验证**；目标版本静态构造点约束同值展开，两轮完整 M 正例均观察到 x-client-app 原值透传。

### SPEC-HDR-022 client-app 与 User-Agent 联动

- **命题与范围**：client_app 缺席时 User-Agent 为 claude-cli/2.1.220 (external, sdk-cli)；client_app=probe-app-21220 时变为 claude-cli/2.1.220 (external, sdk-cli, client-app/probe-app-21220)。 范围：2.1.220 Linux x86_64；firstParty OAuth print s1；只约束 client-app 段的有无和值联动，不外推 TUI、workload 或独立 agent-sdk 段。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-HDR-NEGATIVE-COMPLETE`、`PROBE-HDR-CLIENTAPP-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：`User-Agent` 与 `x-client-app` 必须由同一受控 `Claude220Profile.client_app` 一次生成； 不能分别读取两个入站值，避免出现 Header 与 UA 后缀不一致。
- **状态**：✅ **已验证**；两轮完整 M 正例与两轮负例同时验证同一 client_app 值对 User-Agent 可选段的联动。

### SPEC-HDR-017 `x-claude-remote-container-id` 的条件存在性

- **命题与范围**：在已采 firstParty OAuth print 路径中，未设置 CLAUDE_CODE_CONTAINER_ID 时 x-claude-remote-container-id 缺席；设置为非空值时该 Header 存在。 范围：2.1.220 Linux x86_64；s1；受控环境变量映射。只验证环境变量到 Header 的存在性条件，不声称等同真实 remote-container 产品环境。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-HDR-NEGATIVE-COMPLETE`、`PROBE-HDR-CONTAINER-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：只从受控 `Claude220Profile.remote_container_id` 生成；字段未设置时省略，不得透传 任一入站同名 Header。
- **状态**：✅ **已验证**；目标版本静态构造点唯一命中；两轮未设置负例与两轮受控环境变量正例均以完整 M 归档。

### SPEC-HDR-023 remote-container Header 的精确值

- **命题与范围**：CLAUDE_CODE_CONTAINER_ID 为非空值时，x-claude-remote-container-id 的值与该 container ID 逐字节相同。 范围：2.1.220 Linux x86_64；受控环境变量两轮 probe-container-21220 正例；不等同真实远程容器生命周期。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-HDR-CONTAINER-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：从 `Claude220Profile.remote_container_id` 取值；不得从第三方请求 Header 推断或复制。
- **状态**：✅ **已验证**；目标版本静态构造点约束同值展开，两轮完整 M 正例均观察到容器 ID 原值透传。

### SPEC-HDR-018 `x-claude-remote-session-id` 的条件存在性

- **命题与范围**：在已采 firstParty OAuth print 路径中，未设置 CLAUDE_CODE_REMOTE_SESSION_ID 时 x-claude-remote-session-id 缺席；设置为非空值时该 Header 存在。 范围：2.1.220 Linux x86_64；s1；受控环境变量映射。只验证环境变量到 Header 的存在性条件，不声称等同真实 remote-session 产品环境。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-HDR-NEGATIVE-COMPLETE`、`PROBE-HDR-REMOTESESSION-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：只从受控 `Claude220Profile.remote_session_id` 生成；字段未设置时省略，不得透传 任一入站同名 Header。
- **状态**：✅ **已验证**；目标版本静态构造点唯一命中；两轮未设置负例与两轮受控环境变量正例均以完整 M 归档。

### SPEC-HDR-024 remote-session Header 的精确值

- **命题与范围**：CLAUDE_CODE_REMOTE_SESSION_ID 为非空值时，x-claude-remote-session-id 的值与该 remote session ID 逐字节相同。 范围：2.1.220 Linux x86_64；受控环境变量两轮 probe-remote-session-21220 正例；不等同真实远程会话生命周期。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-HDR-REMOTESESSION-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：从 `Claude220Profile.remote_session_id` 取值；不得从第三方请求 Header 推断或复制。
- **状态**：✅ **已验证**；目标版本静态构造点约束同值展开，两轮完整 M 正例均观察到 remote session ID 原值透传。

### SPEC-HDR-014 子会话请求携带 `x-claude-code-agent-id`

- **命题与范围**：两轮一层子 agent 原始请求携带 x-claude-code-agent-id，主 agent 请求不携带；该字段位于 x-app 之后、x-client-request-id 之前。 范围：common_scope；只覆盖内置 general-purpose 的一层子 agent。
- **证据与缺口**：通道 R／M；`WIRE-SUBAGENT`；缺口：relay-sub-c、relay-sub-c2、WIRE-SUBAGENT 的完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：主会话不得输出该 Header；子会话由内部 agent 上下文生成， 不得接受入站 Header 覆盖。
- **状态**：🟡 **已观察**；实际 subagent-r1/r2 支持一层子 agent 的存在性与相对位置，但旧引用名不存在，值格式和唯一性尚未闭环。

### SPEC-HDR-019 `x-claude-code-parent-agent-id` 的层级存在性

- **命题与范围**：在已采 a1/a2/a3 请求链中，深度 0 的主请求和深度 1 的第一层 agent 请求不带 x-claude-code-parent-agent-id；深度 2 与 3 的嵌套 agent 请求携带该 Header。 范围：2.1.220 Linux x86_64；general-purpose 子 agent；每个 a1/a2/a3 场景两轮。这里只验证按已采深度的存在性，不合并父值相等或 Header 顺序。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-AGENT-DEPTHS-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：由 Sub2API 内部 agent 栈判断；当前 agent 有直接父 agent 时生成，主请求和一层子 agent 请求省略。不得以入站同名 Header 作为层级来源。
- **状态**：✅ **已验证**；a1、a2、a3 各两轮完整 M 运行把请求 Header、task_started 与 tool_use 链绑定到确定深度。

### SPEC-HDR-025 parent-agent-id 等于直接父级 agent-id

- **命题与范围**：在已采深度 2 与 3 的嵌套 agent 请求中，x-claude-code-parent-agent-id 等于该请求直接父 agent 请求的 x-claude-code-agent-id。 范围：2.1.220 Linux x86_64；general-purpose a2/a3 各两轮；值按每轮动态 task/tool 链复算，不固定任一抓包字面 ID。
- **证据与缺口**：通道 A1／J／M；`A1-REACHABILITY`、`PROBE-AGENT-DEPTHS-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：内部创建 agent 时保存不可变 `agent_id` 和 `parent_agent_id`；Header 只写直接父级 ID， 不从客户端入站或整条祖先数组拼接。
- **状态**：✅ **已验证**；六轮 a1/a2/a3 完整 M 运行通过 task_started.task_id、tool_use_id 与请求 Header 建立直接父子绑定。

### SPEC-HDR-026 条件 Header 的组合顺序

- **命题与范围**：当 a2 深度 2 请求同时触发 agent、parent-agent、remote-container、remote-session 与 client-app 时，将实际 HTTP/1.1 Header 序列投影到七个相关名称，顺序为 x-app → x-claude-code-agent-id → x-claude-code-parent-agent-id → x-claude-remote-container-id → x-claude-remote-session-id → x-client-app → x-client-request-id。 范围：2.1.220 Linux x86_64；两个组合 a2 完整 M 运行；该顺序是七字段投影，不声称它们在完整 Header 列表中相邻。
- **证据与缺口**：通道 J／M；`PROBE-HDR-COMBO-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：最终 Header 定型器按上述顺序生成条件槽；先由内部 agent 上下文写 agent 两项，再写 remote container、remote session、client-app，最后写 request-id。不得按字典序排序，也不得沿用 旧的 container-first 顺序。
- **状态**：✅ **已验证**；两个组合 a2 场景在同一深度 2 请求中同时触发五个条件 Header，并给出相同的 J 层序列投影。

### SPEC-HDR-015 子会话共用主会话的 `X-Claude-Code-Session-Id`

- **命题与范围**：subagent-r1/r2 中一层子 agent 与对应主 agent 的 X-Claude-Code-Session-Id 相同。 范围：common_scope；一层子 agent 首请求。
- **证据与缺口**：通道 R／M；`WIRE-SUBAGENT`；缺口：WIRE-SUBAGENT 的完整 M 绑定、会话生命周期与嵌套差分。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：子会话不得另生成 session id。
- **状态**：🟡 **已观察**；两轮 R 支持一层主/子请求共享 session id；更广的会话边界未覆盖。

## 2.7 Body

### SPEC-BODY-001 请求体顶层键集合

- **命题与范围**：基线 Sonnet 主会话样本的 body 顶层键集合恰为文档所列十键。 范围：common_scope；仅基线无额外条件字段路径。
- **证据与缺口**：通道 J；`BASELINE-J`、`WIRE-DEFAULT`；缺口：条件 body 字段正例矩阵。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：基线 Sonnet 场景输出这十个顶层键；条件字段出现时走候选分支，不能仍断言“恰为十项”。
- **状态**：🟡 **已观察**；基线样本支持十键集合，但原 ID 合并键集合、序列化顺序与条件字段边界，非原子命题，故只计 observed。

### SPEC-BODY-002 `metadata.user_id` 是内嵌 JSON 字符串

- **命题与范围**：基线样本 metadata 只有 user_id；该值是可再次解析且恰含 device_id、account_uuid、session_id 三键的 JSON 字符串，内层 session_id 与 X-Claude-Code-Session-Id 相等。 范围：common_scope；未设置额外 metadata 的基线路径。
- **证据与缺口**：通道 J；`BASELINE-J`、`WIRE-SUBAGENT`；缺口：CLAUDE_CODE_EXTRA_METADATA 正负例、跨机器与跨账号差分。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：基线路径必须构造出这个内嵌 JSON 并保持三个键；`session_id` 必须与 `X-Claude-Code-Session-Id` 同值——两者不一致是极易被察觉的形态破绽。 不得把 `user_id` 实现成裸字符串。
- **状态**：🟡 **已观察**；基线样本支持内嵌 JSON、三键与跨层相等，但这些是多个独立命题，原 ID 非原子，故只计 observed。

### SPEC-BODY-007 `system[0].text` 携带 billing attribution

- **命题与范围**：SCOPE-J6 的每个请求中，system[0].text 是以字面 `x-anthropic-billing-header:` 开头的字符串。 范围：SCOPE-J6；只约束文本前缀，不合并 block type、cc_version、cc_entrypoint、cch、字段顺序或动态性。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`、`A1-CANDIDATES`、`A1-SOURCE-MANIFEST`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该有限画像生成这个 Body 文本前缀，同时遵守 `SPEC-HDR-020` 的载体区分。
- **状态**：🟡 **已观察**；六个选定 J-raw 请求均出现相同 attribution 前缀；但字段级 A1 数据流与 §2.1 完整 M 准入均未闭合。

### SPEC-BODY-008 基线 `model`

- **命题与范围**：SCOPE-J6 的已采请求体 model 恰为 claude-sonnet-5；不推出 CLI 无参数默认模型。 范围：SCOPE-J6；不外推 Opus、Haiku、子 agent 模型选择或 fallback。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该基线画像发送该精确字符串；模型选择分支必须另建规则。
- **状态**：🟡 **已观察**；两个 complete run 的六个已采基线请求均发送相同 model 值。

### SPEC-BODY-009 基线 `max_tokens`

- **命题与范围**：SCOPE-J6 的请求体 max_tokens 恰为整数 64000。 范围：SCOPE-J6；不外推其他模型、context overflow 改写或 quota 请求。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：发送数字 `64000`，不得发送字符串。
- **状态**：🟡 **已观察**；两个 complete run 的六个基线请求均发送整数 64000。

### SPEC-BODY-010 基线 `thinking`

- **命题与范围**：SCOPE-J6 的请求体 thinking 恰为 {"type":"adaptive"}。 范围：SCOPE-J6；不外推禁用 thinking、固定 budget 或其他模型路径。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：保持对象结构和值，不得只保证 `type` 键存在。
- **状态**：🟡 **已观察**；两个 complete run 的六个基线请求均发送唯一键 type=adaptive。

### SPEC-BODY-011 基线 `context_management`

- **命题与范围**：SCOPE-J6 的请求体 context_management 恰为 {"edits":[{"type":"clear_thinking_20251015","keep":"all"}]}。 范围：SCOPE-J6；不外推长上下文、compact 或服务端动态策略。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：保持单项 `edits` 数组、键名和值。
- **状态**：🟡 **已观察**；两个 complete run 的六个基线请求均发送同一个单项 edits 对象。

### SPEC-BODY-012 基线 `output_config`

- **命题与范围**：SCOPE-J6 的请求体 output_config 恰为 {"effort":"high"}。 范围：SCOPE-J6；不外推用户显式 effort、其他模型或远程配置。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：保持对象结构与字符串值 `high`。
- **状态**：🟡 **已观察**；两个 complete run 的六个基线请求均发送唯一键 effort=high。

### SPEC-BODY-013 基线 `stream`

- **命题与范围**：SCOPE-J6 的请求体 stream 恰为 JSON 布尔值 true。 范围：SCOPE-J6；不外推异常后的 non-stream fallback 或辅助请求。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：发送布尔值 `true`，不得发送字符串。
- **状态**：🟡 **已观察**；两个 complete run 的六个基线请求均发送 JSON 布尔值 true。

### SPEC-BODY-014 attribution 的 `cc_version`

- **命题与范围**：SCOPE-J6 的 attribution 分段中，cc_version 标量匹配 ^2\.1\.220\.[^;\s]+$。 范围：SCOPE-J6；只证明字段值形态，不证明后缀生成算法、稳定性或唯一性。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`、`A1-CANDIDATES`、`A1-SOURCE-MANIFEST`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：版本前缀与目标二进制一致；后缀不得固定为某次样本。
- **状态**：🟡 **已观察**；SCOPE-J6 的 6 条 attribution 均含符合 2.1.220.<非空后缀> 形态的 cc_version；字段级 A1 数据流与完整 M 尚未闭合。

### SPEC-BODY-015 attribution 的 `cc_entrypoint`

- **命题与范围**：SCOPE-J6 的 attribution 分段中，cc_entrypoint 标量恰为 sdk-cli。 范围：SCOPE-J6；不外推 TUI、agent-sdk、client-app 或 workload。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`、`A1-CANDIDATES`、`A1-SOURCE-MANIFEST`；缺口：字段级 A1 数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：只在该有限 `sdk-cli` 画像发送此值。
- **状态**：🟡 **已观察**；SCOPE-J6 的 6 条 attribution 中 cc_entrypoint 均为 sdk-cli；字段级 A1 数据流与完整 M 尚未闭合。

### SPEC-BODY-016 attribution 的 `cch`

- **命题与范围**：SCOPE-J6 的 attribution 分段中，cch 标量匹配 ^[A-Za-z0-9]{5}$。 范围：SCOPE-J6；只证明字符形态和长度，不证明随机性、唯一性或生成算法。
- **证据与缺口**：通道 J／A1／M；`BASELINE-J`、`PROBE-BASELINE-J`、`A1-CANDIDATES`、`A1-SOURCE-MANIFEST`；缺口：cch 改写数据流、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：输出五字符值，不固定某次样本，也不声称已复刻内部 attestation 算法。
- **状态**：🟡 **已观察**；SCOPE-J6 的 6 条 attribution 中 cch 均为五字符字母数字标量；字段级 A1 数据流与完整 M 尚未闭合。

### SPEC-BODY-003 主会话基线 `system` 四段结构

- **命题与范围**：基线主会话样本的 system 为四个 text block，索引 2、3 含 cache_control；不适用于子 agent。 范围：common_scope 的主 agent 基线首轮/续轮；明确排除 subagent、compact、MCP 和长上下文。
- **证据与缺口**：通道 J；`BASELINE-J`、`WIRE-DEFAULT`、`WIRE-SUBAGENT`；缺口：compact、MCP、长上下文缓存断点矩阵。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：输出四段 text，缓存断点落在后两段；段数或断点位置不同即形态不一致。
- **状态**：🟡 **已观察**；基线主 agent 样本支持四段与两个缓存断点，但原 ID 合并段数、类型和两个位置命题，非原子，故只计 observed。

### SPEC-BODY-004 多轮会话的 `messages` 角色序列

- **命题与范围**：已采集主会话首轮 messages 角色为 [user,system]，第二轮配对样本为 [user,system,assistant,user,system]。 范围：common_scope；只覆盖主会话两轮。
- **证据与缺口**：通道 J；`BASELINE-J`；缺口：第三轮、compact 后和 resume 后序列。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：实现需保留角色边界；补足两轮独立配对后再把精确五项序列升格。
- **状态**：🟡 **已观察**；J 支持已采集的两轮序列，但只各有有限配对，无法推出更长会话状态机。

### SPEC-BODY-005 基线无工具时保留 `tools: []`

- **命题与范围**：基线未启用工具时，请求体显式发送 tools:[]，而不是省略该字段。 范围：SCOPE-BASELINE-NO-TOOLS；只对应已采 s1/s2 的 tools:[]，不包含显式启用 Bash 的 s4，也不覆盖 MCP、ToolSearch、deferred 或 server tool。
- **证据与缺口**：通道 J；`BASELINE-J`；缺口：ToolSearch/deferred tool 正例、server tool 与 MCP schema 正例。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：未启用工具时输出空数组。简单工具的三键形态只算已观察；`strict`、 `eager_input_streaming`、`defer_loading`、MCP、ToolSearch 与 server tool 均按候选处理。
- **状态**：🟡 **已观察**；s1/s2 的 3 个无工具请求支持 tools:[]，但原 ID 同时合并单工具 schema，非原子命题，故只计 observed。

### SPEC-BODY-006 子会话请求的 `system` 与 `messages` 形态

- **命题与范围**：subagent-r1/r2 的一层子 agent 首请求观察到 system 三段、messages 仅一条 user；同轮主 agent 形态不同。 范围：一层内置 general-purpose 子 agent 的首请求。
- **证据与缺口**：通道 R；`WIRE-SUBAGENT`；缺口：子 agent 第二轮及更多轮。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：子会话请求按 3 段 system、单条 user 消息构造，不得套用主会话的 4 段结构。
- **状态**：🟡 **已观察**；两轮 R 观察到一层子 agent 首请求的 system 段数与 messages 角色，但 R 目录缺完整 M 绑定。

## 2.8 端点

### SPEC-EP-001 推理请求 path

- **命题与范围**：SCOPE-J6 的每个请求中，request.path 标量恰为 /v1/messages?beta=true。 范围：SCOPE-J6；claude-http 文件已按 path 含 /messages 分类。本条只记录该预筛类别内的精确 path+query，不是无预过滤发现，也不是端点全集。
- **证据与缺口**：通道 J／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：无 path 预过滤的 HTTP 发现、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该有限画像按此路径与查询串发送。
- **状态**：🟡 **已观察**；六个已按 /messages 路由的 J-raw 请求，其精确 request.path 均为同一值；这是受部分 path 预筛的观察，且完整 M 未闭合。

### SPEC-EP-003 推理请求方法

- **命题与范围**：SCOPE-J6 的每个请求中，request.method 标量恰为 POST。 范围：SCOPE-J6；不继承 path、HTTP host、scheme、port 或 TLS SNI。
- **证据与缺口**：通道 J／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该有限画像使用 `POST`。
- **状态**：🟡 **已观察**；SCOPE-J6 的 6 条独立枚举记录中 request.method 均为 POST；但运行 manifest 未满足 §2.1 完整 M 准入。

### SPEC-EP-004 推理请求 HTTP host

- **命题与范围**：SCOPE-J6 的每个请求中，解析后的 request.host 标量恰为 api.anthropic.com。 范围：SCOPE-J6；manifest 预设 target_hosts=[api.anthropic.com]，故本条不能用于 host 发现或全称验证；它也不等同于 TLS SNI。
- **证据与缺口**：通道 J／M；`BASELINE-J`、`PROBE-BASELINE-J`；缺口：无 host 预过滤的 HTTP 发现、完整 M 绑定。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该有限画像使用该 HTTP host。
- **状态**：🟡 **已观察**；六个 J-raw 记录的解析后 host 均为 api.anthropic.com，但采集器本就按同一 TARGET_HOSTS allowlist 过滤；这是预条件观察，不是独立验证。

### SPEC-EP-002 启动时的 `HEAD /api/hello` 连通性探测

- **命题与范围**：relay-r1/r2 均记录一个 HEAD /api/hello HTTP/1.1 请求，其五项 header 顺序与无 body 可由 R 验证；两轮中它先于推理请求并走不同连接。 范围：两轮 WIRE-DEFAULT；不推出所有 entrypoint/会话必发。
- **证据与缺口**：通道 R／A1；`WIRE-DEFAULT`；缺口：2.1.220 构造点与启动调用链。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：补齐 R 的完整 manifest，并分别验证请求字节、先后关系和独立连接后再作为硬规则。
- **状态**：🟡 **已观察**；端点存在、先后顺序、五项 header 顺序、无 body 和独立连接均被合并。

## 2.9 连接与重试

### SPEC-CONN-001 单次 500 重试不递增 `X-Stainless-Retry-Count`

- **命题与范围**：两轮受控单次 500 后均观察到客户端重发，且原请求与重发请求的 X-Stainless-Retry-Count 均为 0。 范围：common_scope；仅受控一次故障，不等同自然失败链。
- **证据与缺口**：通道 J／L／M；`RETRY-FAULT`；缺口：429 第二轮独立重复。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：500 应用层重试保持 `Retry-Count: 0`；“重试位于 SDK 之上”是静态解释，不是 wire 命题。
- **状态**：🟡 **已观察**；两轮受控 500 支持 retry-count 观察，但原 ID 合并重发、Header 值及多种失败分支，非原子命题，故只计 observed。

### SPEC-CONN-002 `500` 且无 `Retry-After` 时的指数退避

- **命题与范围**：普通应用层重试在没有可用 Retry-After 时，第 attempt 次等待为 round(base + random×0.25×base) ms，其中 base=min(500×2^(attempt-1), 32000)。因此 attempt 1–6 的区间依次为 500–625、1000–1250、2000–2500、4000–5000、8000–10000、16000–20000 ms，attempt 7 起为 32000–40000 ms。 范围：2.1.220 firstParty OAuth print 路径；MITM 仅对模型 POST 注入普通 500、无 Retry-After；故障数 3/5/9 各两轮。只约束 delay 公式，不合并重试次数上限或 retryable 判定。
- **证据与缺口**：通道 A1／J／L／M；`A1-REACHABILITY`、`RETRY-500-COUNT3-COMPLETE`、`RETRY-500-COUNT5-COMPLETE`、`RETRY-500-COUNT9-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：仅在本条 `500`／无 `Retry-After` 分支按公式调度；封顶前基数递增，封顶后允许随机 回落。不得写成固定 35 秒，也不得把同一曲线未经证据套到其他状态码、断连或 `Retry-After` 分支。
- **状态**：✅ **已验证**；2.1.220 A1 唯一定位 delay 公式与常量；3/5/9 连续 500 各两轮的 api_retry 延迟及单调时钟间隔均逐 attempt 复算通过。

### SPEC-CONN-003 九次 `500` 后仍发送第十个请求

- **命题与范围**：在普通 500、无 Retry-After 的已采路径中，Claude Code 2.1.220 至少执行 9 次应用层重试；连续 9 个 500 后仍发送第 10 个模型请求。 范围：2.1.220 firstParty OAuth print s1；MITM 只故障注入模型 POST；两轮 count=9。只证明重试次数下界，不证明默认终止边界恰为 10。
- **证据与缺口**：通道 A1／J／L／M；`A1-REACHABILITY`、`RETRY-500-COUNT9-COMPLETE`；缺口：无登记缺口。详细证明边界见机器台账。
- **实现**：本画像的 500 重试预算不得少于 9 次；在 count=10／11 终止探针闭环前，不把 “恰好重试 10 次”或“无限重试”写成实现合同。
- **状态**：✅ **已验证**；连续 9 个受控模型 POST 500 的两轮完整 M 运行均发出第 10 个请求并取得 200 成功。

## 2.10 响应兼容（不计客户端 egress）

`SPEC-RESP-001/002/003` 只约束下游响应传递；**响应兼容不计入 egress**，不得反向证明请求形态。

### SPEC-RESP-001 成功响应的流式传输形态

- **命题与范围**：已观察的成功流式响应同时为 SSE、chunked 和 gzip。 范围：服务端到客户端的 200 stream:true 响应；不得计入 client egress 规则完成率。
- **证据与缺口**：通道 J／R；`BASELINE-J`、`WIRE-DEFAULT`；缺口：无登记缺口。详细证明边界见机器台账。
- **响应兼容要求**：向下游保持 SSE + 分块 + gzip 的等价形态；不得把流式响应缓冲成整体 JSON 再下发。
- **状态**：**响应兼容**；这是服务端响应/代理下游兼容要求，不属于官方客户端出站请求画像。

### SPEC-RESP-002 响应 header 的稳定性分级

- **命题与范围**：当前样本记录了响应 header 的取值变化和至少两种 wire 顺序；只能作为响应兼容观察。 范围：服务端/CDN 到客户端的成功响应；不得计入 client egress 规则完成率。
- **证据与缺口**：通道 J／R；`BASELINE-J`、`WIRE-DEFAULT`；缺口：按组织、CDN PoP 和时间分层的稳定性统计。详细证明边界见机器台账。
- **响应兼容要求**：向下游透传时按类处理——每次必变项须重新生成或透传上游值，不得缓存复用； 不得钉死响应 header 顺序。
- **状态**：**响应兼容**；这是服务端/CDN 响应画像，不属于客户端出站；稳定性分类还混合多个统计命题。

### SPEC-RESP-003 响应体为外层 chunked、内层 gzip

- **命题与范围**：WIRE-DEFAULT 的 200 流式响应呈现外层 HTTP chunked、chunk 内容为 gzip 流。 范围：服务端到客户端的成功流式响应；不得计入 client egress 规则完成率。
- **证据与缺口**：通道 R；`WIRE-DEFAULT`；缺口：无登记缺口。详细证明边界见机器台账。
- **响应兼容要求**：保持同样的嵌套次序；先分块再压缩会产生不同的 wire 形态。
- **状态**：**响应兼容**；R 能观察 gzip 后再 chunked 的响应封装，但方向是服务端出站。

## 2.11 Beta 机制

### SPEC-BETA-001 `ANTHROPIC_BETAS` 的追加位置

- **命题与范围**：在 Sonnet 5 基线序列中注入单个合法 context-1m-2025-08-07 时，两轮 J 均观察到该值位于 mid-conversation-system 之后、effort 之前；基线负例无该项。 范围：common_scope；只覆盖单个合法 beta。
- **证据与缺口**：通道 J／M／A1；`PROBE-BETAS`、`A1-CANDIDATES`；缺口：多值、重复值、空项运行正例。详细证明边界见机器台账。
- **Sub2API 实现要求（暂定）**：该变量未设置时不得改变基线观察到的 9 项。
- **状态**：🟡 **已观察**；两轮单值正例支持存在性和位置，但原 ID 合并解析、多值、重复与非法值行为，非原子命题，故只计 observed。

<!-- CLAUDE_RULE_CARDS_END -->

## 2.12 候选台账（未闭环）

锚点列为 `CANDIDATE_EVIDENCE.json` 中该候选 Linux 侧 α-归一化摘要的前 12 位；「跨平台」列记录该
锚点与 Darwin 交叉样本是否一致。窗口 sink 按 §2.1.3 的边界解读，不构成数据流证明。

| 编号 | 候选命题 | 当前依据 | 锚点／跨平台 | 主要缺口／状态 |
|---|---|---|---|---|
| `CAND-UA-CLI` | `cli`（TUI）入口的 UA 尾段为 `cli` 而非 `sdk-cli` | [A1] `Z.set("CLAUDE_CODE_ENTRYPOINT", e?"sdk-cli":"cli")`——由启动模式自设，带 `-p` 时即使外部预设 `cli` 也会被改回 `sdk-cli`，故只能真跑 TUI | `a20d7b8f44d9`／差异仅为 `BUILD_SOURCEMAP_GROUP` | 引导可用 `.claude.json` 的 `hasCompletedOnboarding=true` 跳过（该字段在 logout 时被重置为 false）；但 `pty` 在 `docker exec` 无 TTY 环境下父进程会卡在 select 且不响应 SIGTERM，八次尝试均未取得正例。需在有真实 TTY 的环境（宿主机 tmux／screen）重试；**候选** |
| `CAND-HDR-USAGE-LIMIT` | depth>0、非 auxiliary 或 compact、firstParty 官方 base、`cachedExtraUsageDisabledReason != null` 且 `tengu_lantern_spool` 为真时发送 `anthropic-usage-limit: extended` | Linux bundle 完整条件 | `2fa1d386deb5`／一致 | gate 无环境变量覆盖路径，触发与否由服务端 Statsig 决定，无法构造正例；**候选** |
| `CAND-BODY-BILLING-CONDITIONS` | attribution text 条件追加 `cc_prev_req`、`cc_is_subagent`、`cc_workload` | A1；`subagent-r1/r2` 已观察前两项 | `d29b00f5d6f5`／一致 | R 缺完整 M，workload 入口不可达；**部分观察／候选** |
| `CAND-BODY-CCH-REWRITE` | 内部 attestation 机制把 `cch` 占位值改写为五字符 token | 2.1.88 注释解释机制；2.1.220 wire 已看到动态五字符结果 | 未建数据流探针 | 五字符样本结果由 `SPEC-BODY-016` 记录，具体内部改写点仍不可观测；**不可观测机制** |
| `CAND-AUTH-BEARER` | `claude.ai` OAuth 路径使用 `Authorization: Bearer` 且不给 SDK apiKey | 2.1.88／HitCC 机制与 R 字节观察 | 未建当前数据流锚点 | R 缺完整 M，需把认证选择链绑定到 2.1.220 sink；**已观察待闭环** |
| `CAND-HDR-TRACEPARENT` | active span 下条件发送 `traceparent` | HitCC 2.1.197 条件说明 | 未建当前锚点 | 需 2.1.220 A1 可达链及有／无 span 单变量正负例；**候选** |
| `CAND-HDR-DISPATCH-RETRY` | 首事件前失败时移除 `anthropic-dispatch-id` 再重试 | HitCC 2.1.197 | 未建探针 | 先要取得远程 gate 正例，再注入指定失败；**候选** |
| `CAND-HDR-CUSTOM-MATRIX` | custom header 的多行、首冒号、重复名、覆盖与非法行规则 | A1 与 2.1.88 旧机制 | `1a63a4012f4a`／一致 | 当前只验证单行唯一名；**候选** |
| `CAND-HDR-REMOTE-MATRIX` | 条件 Header 的异常输入、真实远程来源与生命周期 | `SPEC-HDR-016/017/018/021/022/023/024/026` 已闭环受控未设置／ASCII 值、UA 联动及组合顺序 | 四个跨平台构造锚点＋22-run campaign | 已闭环部分不再是候选；仍需空串／纯空白／非 ASCII／超长值，以及真实 remote container/session 的分配、轮换、回收和与主 session 的关系；**剩余候选族** |
| `CAND-HDR-SESSION-RANDOM` | 不同会话 session-id 不复用及其生成算法 | 当前样本无碰撞 | 未建生成器锚点 | 定义会话边界、跨进程／compact／子 agent 差分与统计门槛；**候选** |
| `CAND-METADATA-EXTRA` | `CLAUDE_CODE_EXTRA_METADATA` 合并 JSON object，非法值报错 | 2.1.88 与 HitCC | 未建当前锚点 | 需合法／非 object／非法 JSON 差分及 wire 正例；**候选** |
| `CAND-BODY-CONDITIONAL-FIELDS` | 条件出现 `temperature`、`tool_choice`、`speed`、`fallbacks`、`fallback_credit_token`、`diagnostics` | HitCC request builder；2.1.88 部分同源 | 未建完整锚点 | 每个字段须拆成独立触发矩阵，不能由“默认十键”否定；**候选族** |
| `CAND-SYSTEM-SEMANTICS` | 四段依次为 billing attribution、产品身份、主 prompt、request-level addition | HitCC；默认 J-raw/R | 未建稳定摘要矩阵 | 需按段建立归一化摘要、动态字段规则及至少两轮重复；**部分覆盖** |
| `CAND-CACHE-MESSAGE` | 最后一个非 `api_system` message、`skipCacheWrite` 与 fork point 决定 message cache breakpoint | HitCC 2.1.197 | 未建当前锚点 | 需默认／skip／fork 三组正负例；**候选** |
| `CAND-CACHE-SYSTEM-SCOPE` | system cache scope 为 null/org/global，受动态边界与 MCP schema 影响 | HitCC 2.1.197 | 未建当前锚点 | 需 2.1.220 gate 可达链及 scope 差分；**候选** |
| `CAND-TOOLS-EXTENDED` | tool schema 可含 `strict`、`eager_input_streaming`、`defer_loading` | 2.1.88 与 HitCC | 未建当前锚点 | 当前简单工具三键不能外推全部工具；**候选** |
| `CAND-TOOLS-DEFERRED` | ToolSearch 与 deferred tool 改变 tools 数组及 beta | HitCC 2.1.197 | 未建当前锚点 | 需可触发 ToolSearch 的 TUI／MCP 场景；**候选** |
| `CAND-SERVER-WEBSEARCH` | `web_search_20250305`、domains、`max_uses:8` 与条件 tool_choice | HitCC 2.1.197 | 未建当前锚点 | 需功能 gate 正例；**候选** |
| `CAND-SERVER-ADVISOR` | `advisor_20260301` server-tool schema | HitCC 2.1.197 | 未建当前锚点 | 先判定 2.1.220 是否可达；**候选** |
| `CAND-QUOTA-PROBE` | quota_check 使用 `messages:[quota]`、`max_tokens:1` 与 metadata | HitCC 2.1.197 | 未建当前锚点 | 需构造 quota 触发并与普通 messages 分流；**候选** |
| `CAND-STREAM-NONSTREAM` | 缺 message_start、无完整 block、watchdog、stream exception 或 404 后重发 `stream:false` | 2.1.88 与 HitCC | 未建完整故障矩阵 | 每种触发原因至少两轮，并关联原请求 id；**候选族** |
| `CAND-FALLBACK-PROTOCOL` | fallback betas、`fallbacks`、credit token 与 400 strip/retry | HitCC 2.1.197 | 未建当前锚点 | 需账号／gate、合法 token 与受控 400 对照；**候选族** |
| `CAND-CACHE-DIAGNOSIS` | `cache-diagnosis-2026-04-07` 与 `diagnostics.previous_message_id` | HitCC 2.1.197 | 未建当前锚点 | 需关联前一响应 id 的两轮差分；**候选** |
| `CAND-RETRY-MATRIX` | 401／403／408／409／429／529、其他 5xx、Retry-After、x-should-retry、OAuth 刷新与精确终止边界 | 2.1.88 `withRetry.ts`；HitCC；`SPEC-CONN-002/003` | 500／无 Retry-After 的当前 A1 锚点与六轮完整 M | 普通 500 的公式和至少 9 次已闭环；仍需 count=10/11、各状态码、有／无／非法 Retry-After、断连及刷新分支；**剩余候选族** |
| `CAND-RETRY-529-FALLBACK` | 连续 529 达阈值后切换模型，context overflow 时改写 max_tokens | 2.1.88 与 HitCC | 未建当前锚点 | 需长链受控注入与模型／body 差分；**候选** |
| `CAND-NONMAIN-THREADS` | fork、side question、hook_prompt、hook_agent、compact summarize、后台与并发 agent 的专用形态 | HitCC 2.1.197；`SPEC-HDR-019/025/026` | general-purpose 深度 1／2／3 已有六轮完整 M | general-purpose 的 parent 存在、直接父级相等与组合顺序已闭环；其他非主会话类型、并发及各自 Body／Header 差分仍未采；**剩余候选族** |
| `CAND-BETA-CUSTOM` | 自定义 beta 与 API Key／OAuth 路径存在条件差异 | 当前警告文本与 2.1.88 参数指向“API Key 用户可用” | 未建探针 | API Key 路径不在对齐范围；**范围外** |
| `CAND-EP-COUNTTOKENS` | `/v1/messages/count_tokens?beta=true` 端点及其四项 beta 子集 | [A1] `i.filter(d => Aji.has(d))`；调用点为 `beta.messages.countTokens` | 未建探针 | 该端点在现有场景未触发；需构造 token 计数及 Haiku fallback 对照；**候选** |
| `CAND-EP-SDK-SURFACE` | batches、models、files 等 SDK surface 是否被 Claude Code 2.1.220 实际调用 | HitCC 端点清单 | 未建 reachability | “wrapper 存在”不等于业务可达；逐端点做当前调用链证明；**候选族** |
| `CAND-EP-AUXILIARY` | bootstrap、settings、policy_limits、OAuth refresh/profile/roles 等必要辅助请求 | 2.1.88 与 HitCC | 未建全量发现 | 在 default/essential 两种隐私模式下无 host 预过滤发现；**候选族** |
| `CAND-EP-FULLSET` | 客户端在本版本使用的端点全集 | `SPEC-EP-001` 记录推理 path，`SPEC-EP-002` 记录 HEAD 观察，`SPEC-TLS-003` 才是无 host 预过滤的 TLS 样本 | 未建探针 | 全部证据取自 `essential-traffic` 模式，`default` 模式下 53 处门控请求会放行；还需覆盖非 443、OAuth 刷新与其他场景；**候选** |
| `CAND-BG-SESSION` | 后台 agent 会话（`--bg`）的 `x-app` 为 `cli-bg` | [A1] `"x-app": rs()?"cli-bg":"cli"`，`rs()=Wot()==="bg"` | 未建探针 | **与 `CAND-UA-CLI` 同源**：客户端明确拒绝 `--bg` 与 `-p` 并用（"--bg and --print conflict: --print never starts the interactive session"），因此后台会话只能经 TUI 驱动，卡在同一个 TTY 障碍上；**候选** |
| `CAND-WORKLOAD` | attribution text 的 `cc_workload` 字段随 workload 变化 | [A1] `HYn()` 读 `AsyncLocalStorage`，已知值之一为 `cron` | 未建探针 | workload 由进程内路径设定，环境变量注入不了；当前 `sdk-cli` 入口不可达；**不可观测机制** |
| `CAND-WIRE-H2` | HTTP/2 帧、SETTINGS 与 HPACK 形态 | SPEC-TLS-002 表明基线只 offer `http/1.1` | 不适用 | 基线路径不协商 h2，无从观测；**不可观测机制**（除非出现 h2 分支） |

正文候选表按可执行抓包族合并，原子级完整清单在两份来源覆盖 JSON 中。锚点相同只说明两平台
出现同一段逻辑，不说明它在默认 OAuth 路径可达。

### 2.12.1 明确版本漂移、冲突与范围外

| 项目 | 处置 | 理由 |
|---|---|---|
| HitCC `currentDate` 三 bit marker | **已移除／不得迁入** | HitCC 自述从 2.1.201 起已看不到；不能定义 2.1.220 |
| HitCC “API messages 只含 user/assistant” | **已变化** | 2.1.220 抓包出现 mid-conversation `system` role |
| 2.1.88 自定义 header 覆盖优先级 | **已变化／待重验** | 旧 SDK 0.74.0，当前为 0.94.0，不能继承展开顺序 |
| 2.1.88 `GET /api/hello`／`GET /v1/oauth/hello` | **已变化** | 当前只观察 `HEAD /api/hello`，未观察 oauth hello |
| 2.1.88 beta 列表与排列 | **已变化** | 当前 A1 注册表、默认序列与插入点均不同 |
| API Key、Bedrock、Vertex、Foundry、Mantle、Gateway | **范围外** | 不属于 `claude.ai`／firstParty OAuth 对齐目标 |
| telemetry、GrowthBook、voice、transcript share、MCP proxy、update/plugin download | **外围出站** | 已按类别登记；具体机制与端点尚未全量原子化，不纳入模型数据面发包画像；隐私模式决定可达性 |
| CA／proxy 配置 | **机制信息** | 影响连接建立，不能推出 2.1.220 ClientHello wire 指纹 |

### 2.12.2 最小补抓顺序

client-app、container、remote-session、深度 1／2／3 agent、组合 Header 顺序，以及 500 的
count=3／5／9 已由本轮 22 个完整 M 运行补齐，**不再列为待补证**。剩余最小顺序为：

1. 为 `relay-r1/r2`、`subagent-r1/r2` 补齐二进制 SHA、argv、完整环境、采集器摘要、状态、脱敏与
   秘密扫描，再晋级原始 HTTP wire、HEAD 和旧复合子 agent 规则。
2. 补 remote/client-app 的空串／纯空白／非 ASCII／超长值，并在真实 remote container/session
   环境核对来源和生命周期；另补 retry count=10/11，确定精确终止边界。
3. 补 custom-header 多行／覆盖、beta 多值／重复、extra metadata、traceparent。
4. 补三轮会话、compact、fork／hook、简单多工具、MCP、ToolSearch、server tool、后台与并发 agent。
5. 按 401／403／408／409／429／529、其他 5xx、断连、`Retry-After` 与 OAuth 刷新分支补完整
   retry/fallback 矩阵；不能用已验证的 500／无 Retry-After 曲线替代。
6. 在 `essential-traffic` 与 `default` 两种隐私模式下，无 host 预过滤发现 count_tokens、quota、
   OAuth 与辅助端点。
7. 在真实 TTY 上采集 TUI 与 `cli-bg`；`-p` 会强制 `sdk-cli`，不能用环境变量伪造入口。

## 2.13 36 个历史编号迁移审计

下表只回答旧编号如何处置，不表示 Sub2API 实施优先级；当前对齐责任以 §2.1、§2.2 和机器台账为准。
`已验证` 仅指“保留命题”中的收窄命题，不继承旧标题或旧正文中的其他全称断言。

<!-- CLAUDE_HISTORICAL_AUDIT_START -->

| 历史编号 | 状态 | 保留命题／处置 | 移出的命题与缺口 |
|---|---|---|---|
| `SPEC-TLS-001` | 已观察 | 该 Linux SHA 的已采 direct 场景出现同一 ClientHello 扩展序列 | 原 ID 合并序列与跨模型不变性；旧聚合分母未绑定 |
| `SPEC-TLS-002` | 已观察 | 所选 pcap 的全部 8 个 ClientHello 只 offer `http/1.1` | 完整 M 未闭合；不外推其他平台、provider 或失败链 |
| `SPEC-PROTO-001` | 已观察 | 所选 6 个 J 请求的解析结果为 HTTP/1.1 | 完整 M 未闭合；原始请求行另由 R 观察 |
| `SPEC-HDR-001` | 已观察 | 基线 Sonnet 推理请求的 22 项 header 名、大小写及相对顺序 | 原 ID 复合且旧聚合分母未绑定；条件组合顺序已拆到已验证的 `HDR-026` |
| `SPEC-HDR-002` | 已观察 | 所选 6 个 J 请求的 `sdk-cli` UA 精确值 | A1 只有局部 sink 近邻，完整 M 未闭合；可选段留候选 |
| `SPEC-HDR-003` | 已观察 | Sonnet 5 基线 9 项 beta 序列 | 序列为复合值；Opus 一轮，Haiku 正例不存在 |
| `SPEC-HDR-004` | 已观察 | 基线路径出现 `anthropic-version: 2023-06-01` | 旧聚合分母未绑定，缺当前 sink 静态闭环 |
| `SPEC-HDR-005` | 已观察 | 解析值为 `gzip, deflate, br, zstd` | 旧聚合分母未绑定；wire 字面只由缺 M 的 R 观察 |
| `SPEC-HDR-006` | 已观察 | 基线 Linux 的八项 Stainless 值向量 | 复合八字段；旧聚合分母未绑定 |
| `SPEC-HDR-007` | 已观察 | 前台基线出现 dangerous=`true`、`x-app=cli` | 两个独立 Header 合并；`cli-bg` 无正例 |
| `SPEC-HDR-008` | 已观察 | 基线路径未见 `anthropic-dispatch-id` | 复合负例与远程 gate；正例不可达 |
| `SPEC-HDR-009` | 已观察 | 变量值 `1` 时出现 additional-protection | 触发、值、位置和负例尚未拆号 |
| `SPEC-HDR-010` | 已观察 | 单行 `X-Egress-Probe: probe-alpha` 的出现、值与位置 | 多个可独立失败结果合并；扩展矩阵未采 |
| `SPEC-HDR-011` | 已取代 | 不再作为活动规则 | 混淆 HTTP Header 与 Body 文本；由 `HDR-020`、`BODY-007` 替代 |
| `SPEC-HDR-012` | 已观察 | 现有样本未见 request-id 碰撞 | 缺当前生成器可达证据与明确统计门槛 |
| `SPEC-HDR-013` | 已观察 | 同一已采会话内复用 session-id | 跨请求状态；旧聚合分母未机器绑定 |
| `SPEC-HDR-014` | 已观察 | 一层 `general-purpose` 子 agent 出现 agent-id | 证据实为 `subagent-r1/r2`，R 缺完整 M |
| `SPEC-HDR-015` | 已观察 | 已采主／子 agent 共用 Header session-id | metadata 相等属于 Body；R 缺完整 M |
| `SPEC-HDR-016` | 已验证 | 受控 client-app 未设置时 Header 缺席，设置为已采非空值时存在 | 精确值拆到 `HDR-021`，UA 联动拆到 `HDR-022`；异常输入仍候选 |
| `SPEC-HDR-017` | 已验证 | 受控 container ID 未设置时 Header 缺席，设置为已采非空值时存在 | 精确值拆到 `HDR-023`；真实远程来源与生命周期未证 |
| `SPEC-HDR-018` | 已验证 | 受控 remote session ID 未设置时 Header 缺席，设置为已采非空值时存在 | 精确值拆到 `HDR-024`；与主 session 的同源关系未证 |
| `SPEC-HDR-019` | 已验证 | `general-purpose` 深度 0／1 缺席、深度 2／3 存在 parent-agent-id | 直接父级相等拆到 `HDR-025`；其他 agent 类型、并发与更深层级未证 |
| `SPEC-BODY-001` | 已观察 | 基线 Sonnet 请求体顶层十键集合 | 键集合与序列化顺序合并；分母未绑定 |
| `SPEC-BODY-002` | 已观察 | metadata 的 user_id 内嵌 JSON 与 session-id 相等 | 编码、键集、生命周期、跨层相等四个命题合并；25 分母未绑定 |
| `SPEC-BODY-003` | 已观察 | 主会话基线为四段 text，索引 2、3 带 cache_control | 段数、类型和缓存位置未拆号；22 分母未绑定 |
| `SPEC-BODY-004` | 已观察 | 已采两轮会话出现两项→五项角色序列 | 每种续轮配对不足两次；三轮与 compact 未采 |
| `SPEC-BODY-005` | 已观察 | s1/s2 基线无工具时 `tools: []` 且字段不省略 | 须使用 `SCOPE-BASELINE-NO-TOOLS`；工具 schema 另拆 |
| `SPEC-BODY-006` | 已观察 | 子 agent 首请求为三段 system、单条 user message | R 缺完整 M；子 agent 续轮未采 |
| `SPEC-EP-001` | 已观察 | 已按 `/messages` 路由的 6 个 J 请求，其精确 path 为 `/v1/messages?beta=true` | path 部分预筛；method、HTTP host、TLS SNI 已拆号；完整 M 未闭合 |
| `SPEC-EP-002` | 已观察 | 启动前观察到 `HEAD /api/hello` 及五项 header | R 缺完整 M；先后关系、独立连接和响应应拆分 |
| `SPEC-CONN-001` | 已观察 | 两轮单次 500 后重发且 Stainless retry-count 仍为 0 | 重发与 Header 两结果合并，且缺 P/R/J 流级关联 |
| `SPEC-CONN-002` | 已验证 | `500` 且无 `Retry-After` 时为 500 ms 起步、2 倍指数、32 s 基数封顶、最高 25% 抖动 | 九次失败后的第十次请求拆到 `CONN-003`；其他错误、Retry-After、精确终止边界未证 |
| `SPEC-RESP-001` | 响应兼容 | 成功响应的 SSE／chunked／gzip 形态 | 不计客户端 egress |
| `SPEC-RESP-002` | 响应兼容 | 响应 header 动态性观察 | 旧聚合口径尚无机器全表，不得固定 CDN 顺序 |
| `SPEC-RESP-003` | 响应兼容 | 观察到先 gzip、后 chunked | R 缺完整 M，不计客户端 egress |
| `SPEC-BETA-001` | 已观察 | Sonnet 下单个合法 beta 位于零基索引 7（第 8 项） | 存在、位置、负例与解析算法未拆号 |

<!-- CLAUDE_HISTORICAL_AUDIT_END -->

## 2.14 HitCC 2.1.197 线索审计

HitCC 固定为 commit `f4556e5b18a65232023998219e53c2598cc17d82`，只有 112 个 Markdown，没有
文档提及的 pretty bundle，因此属于 A4 线索地图。`hitcc_2_1_197_coverage.json` 已抽取 88 条原子
线索：覆盖 9、部分覆盖 65、缺失 4、范围外 10；`covered` 表示已由正式规则或候选承接，不等于
`verified`。同名历史规则只覆盖当前 `retained_claim`，不能再由同名历史 ID 反向继承其余语义。

文档级盘点为：18 篇直接线索源已映射、53 篇直接线索源尚未逐条抽取、33 篇仅交叉引用、8 篇没有
独立出站线索。因此现有 88 条不是 HitCC 全量线索，禁止宣称“全部覆盖”。路径、clue ID 与反向映射
可机器复核；`clue_source`／`cross_reference_only`／`no_egress_clue` 及是否仍有独立线索
属于**人工语义判断**，checker 不能从 Markdown 自动证明分类正确或抽取穷尽。
## 2.15 Claude Code 2.1.88 源码审计

2.1.88 取证根含 4,756 个文件：`src/` 1,902、`node_modules/` 2,850、`vendor/` 4；完整根摘要为
`dafc1b37756e0f6bb312a8bf5c98c115c40a65d5d87cc1aa80910cf6e956878f`。Anthropic SDK 是
`node_modules/` 的 51 文件子集，摘要为
`0a1e18ded2ef751f5c8ff6e7d4199d4f855f916cc3094e69096ebd49447c1c30`，版本为 0.74.0。
该根缺官方来源 manifest，而当前样本为 `x-stainless-package-version: 0.94.0`，所以只能作 A3 机制词典。

`source_2_1_88_coverage.json` 只覆盖 `src/`：102 条种子机制的处置为
正式运行证实 4、仅静态／样本相容 39、已变化 3、未证实 38、范围外 18。四条正式承接仅为
`SRC2188-HDR-005/006/007` 和 `SRC2188-RETRY-006`；它们分别由当前条件 Header 正负例及
500／无可用 `Retry-After` 的当前重试曲线闭环，不能外推旧命题的其他条件。

`src/` 中 21 个文件是种子直接来源、237 个是导入层支持、1,644 个尚未闭合；依赖源码只完成
确定性快照，**尚未做语义规则抽取**。其中 458 个“命中词法候选信号”与 1186 个“未人工排除”只是
台账现有 `scan_signals` 字段的内部计数；扫描器规则未归档，不能从 2.1.88 源码独立复算该词法分组。
因此该台账不是完整数据流证明，也不能宣称全部旧源码规则已被当前版本证实。
## 2.16 机器门禁

运行：

```bash
python3 tools/official_client_capture/claude_21220/check_coverage.py
```

该命令是本地取证门禁，要求被 `.gitignore` 排除的 `local-analysis/` 证据树完整存在；它不是脱离证据
归档即可运行的普通 CI 单测。

为保持 2.1.220 已封存取证工具的 SHA 不变，三份工具源码注释仍保留迁移前的
`docs/Claude_code_21220_EGRESS_SPEC.md` 历史定位文字；该字符串不是活动文档入口。checker、规则台账
与 HitCC 台账均已绑定本文，禁止只为改注释而重写既有证据摘要。

门禁至少检查：57 个编号完整覆盖且唯一；`32 + 21 + 0 + 3 + 1` 的责任集合无重叠；文档、JSON 与
checker 同时对账目标 53、已验证 12、已观察 41、已编号待补证 0、排除 4；历史 36 ID 全处置且
`HDR-021..026`、`CONN-003` 不复用旧 ID；三份 JSON 可解析且枚举合法；
本轮 22 个 manifest 均为 `complete`、完整 M、运行镜像已验证、运行内及终态精确秘密扫描无命中且清理成功；
campaign 绑定摘要为 `b2bdc967114b9bd4001ee8da0feec0f79e5a2490fc9bf03dc59780298a454d98`，并按各自
host receipt 接受 12／8／1／1 四组采集执行源摘要，不要求错误的单一源码摘要；
四个条件 Header A1 锚点在 Linux／Darwin 各唯一命中且 α 摘要一致；正负例、agent 深度序列、
parent 直接相等关系、组合 Header 顺序及 500 count=3／5／9 的请求／故障／重试计数从原始 J 与
生命周期记录复算；
追溯 `SCOPE-J6` 的 host allowlist、`/messages` 路由和当前 addon 摘要，禁止 `EP-001/004` 声明
独立验证分母，并对其余 16 条有限样本规则复核待证字段独立性；所选 P 分母从原始 pcap 重算；实际 A1 Linux 二进制摘要与
SOURCE_MANIFEST 一致；HitCC 工作树 clean，112 篇 Markdown 的路径＋内容摘要与文档盘点闭合；
HitCC 已抽取统计为 covered 9、partial 65、missing 4、out_of_scope 10，且仍保留 53 篇直接线索源
未逐条抽取的边界；
`SCOPE-J6` 的 Stainless package version 为 0.94.0；2.1.88 完整取证根、`src/`、`node_modules/`、
`vendor/` 与 Anthropic SDK 的文件数和路径＋内容摘要可复算；`runtime_verified` 集合与人工复核清单
双向等于 `SRC2188-HDR-005/006/007`、`SRC2188-RETRY-006`，来源统计为 4／39／3／38／18，且每条
SPEC 引用单向属于当前 verified 集合；正文不得给出与 §2.14／§2.15 相反的肯定结论，也不得声称
“36/36 已验证”“全部 HitCC 已覆盖”或“全部 2.1.88 源码规则已证实”。门禁同时强制记录
当前 M、A1 数据流、依赖语义抽取和扫描器复算边界；它不把这些未完成项伪装成机器已证明。

## 2.17 规则卡合同

每个活动 `SPEC-*` 必须保留四项：命题与范围、证据通道／台账引用／缺口、Sub2API 可见实现要求、
证据状态。详细运行号、分母、支持理由和下一探针只写入机器台账，正文不重复；规则状态与实现状态
分开，`verified` 不表示 Sub2API 已实现。新增或拆分规则时同步更新 §2.2 清单、机器台账和
`PAIR-<SPEC-ID>`，再通过 §2.16 门禁。

---

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

2.1.220 当前权威证据只覆盖 `sdk-cli`、Linux x86_64、`essential-traffic`、Sonnet 5，且 53 项中只有
12 项 verified；因此不能默认把它声明为目标 stable 全范围的 strict rollback。若其独立批准与回退演练
只覆盖窄范围，就必须同步收窄 DeploymentTrafficEnvelope；也可使用 FW-A 冻结的遗留部署承载更宽的
operational rollback，但遗留 wire 始终是 diagnostic-only，不能扩大 2.1.220 的官方证据或批准范围。

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
| `/v1/messages` 推理及已编号的 `HEAD /api/hello` 生命周期探测 | `persona_strict` | 纳入画像、SPEC／PAIR、SupportEnvelope 和 Guard |
| `/v1/messages/count_tokens` | `non_persona_managed` | 证据闭环前使用独立受管策略；闭环后可新增 SPEC／PAIR 并晋升 `persona_strict` |
| usage／额度查询、OAuth refresh、account test、upstream models | `non_persona_managed` | 登记 route／Sink、认证、endpoint、client、超时、重试、秘密与审计，不主张官方 wire 等价 |
| 未登记 Claude OAuth 路径 | `denied` | enforce 时 fail-close |

`non_persona_managed` 是受管第三态，不是 `out_of_scope_passthrough`。它不计入当前 53 项及
SupportEnvelope 的逐规则分母，但必须有独立的 source-to-sink 闭集、运行断言和失败策略；未来若要求
仿真官方客户端在该端点的 wire，必须先取得证据、原子化规则并正式晋升为 `persona_strict`。

Envelope 只向共享 Executor 暴露 Persona／Release 摘要、Route／Sink、method／protocol、attempt、Body
可重放性、TransportCapability、Identity／Dialect attestation digest 和受保护请求能力。共享层不得
解释 Claude beta、Header 槽位、BodyShape、agent 层级、Stainless 或重试语义，也不得要求 Claude 填充
Codex IdentityMode、HeaderPolicy、BodyPolicy、BehaviorPolicy 或 fallback 字段。

| 规则组 | 数量 | 画像／执行落点 |
|---|---:|---|
| TLS／HTTP | 4 | `Transports` + adapter |
| Header／Beta | 26 | `HeaderSlots`、`BetaPolicy` + Claude Compiler |
| Body | 16 | `BodyShape` + Claude Compiler |
| 端点 | 4 | `Endpoints` + URL 闭集 |
| 连接／重试 | 3 | Claude `RetryPolicy`、client lifecycle；只把最终 attempt 预算与可重放性投影给 Executor |

53 项均须同时具有画像／执行落点、官方与候选证据及 `PAIR-<SPEC-ID>`；调度、计费和服务级节奏不由
画像改写。

## 3.4 当前差距与 FW-A～FW-H 迁移

当前只有遗留局部仿真，strict 画像化迁移尚未开始：

| 差距 | 当前事实 |
|---|---|
| 版本身份硬编码且散布 | `internal/pkg/claude/constants.go` 及多个业务文件读取 2.1.220、UA、Stainless、beta 常量 |
| 画像由 Go 构造 | `buildAnthropicClientProfileCatalog()` 不能内容寻址或追加换版 |
| 无 Claude Snapshot／发布图 | `catalogdata/` 只有 Codex；遗留 Claude `active／previous` 只是代码构造指针 |
| 无条件 Header Schema | 遗留画像只有 `StaticHeaders` 与 `BetaHeader`，无法表达 21 项条件责任 |
| finalizer 位于 service | `official_egress_anthropic.go` 未进入共享 Executor／Token／Guard 终态链 |
| 无 Claude Persona／Compiler／Guard | 当前 `officialegress` persona 和 authority 仍是 Codex 专用 |
| 入口与出站闭集未封存 | 三类推理入口、物理别名、count_tokens 和管理辅助请求尚无统一 Inventory 与三态处置 |
| 三方语义补全未受管 | Persona system／identity 派生与有损 system 角色改写混在遗留 body 重写器中 |
| 版本门禁不完整 | 版本注释已漂移，业务代码尚可继续泄漏版本指纹 |

| 阶段 | 变更 | 完成判据 |
|---|---|---|
| `FW-A` | 只读冻结 Codex、Claude 2.1.220 证据、生产运行态和遗留发送面基线；不新增 Claude 代码或绑定 | 两类基线可复算；遗留输出只标 diagnostic-only；没有生产写入 |
| `FW-B` | 按 Framework 抽取 Codex 已证明的暂定共享合同并保留 Codex facade | 共享内核无 Codex 专用策略字段；Codex active／rollback final wire 零差异；不宣称多 Persona 已冻结 |
| `FW-C` | 验证并发布 Codex-only 正式制品，完成回滚、恢复和稳定观察 | 本轮没有新增 Claude Persona／画像／strict 注册；Codex 发布与激活收据闭环 |
| `FW-D` | 建设 Campaign、正交事实、两段式批准、Snapshot／Release Store、candidate／PAIR、晋升与激活工具链 | 只用 Codex／合成数据自测；越权、摘要变化、范围缺口和收据不匹配均由机器阻断 |
| `FW-E` | 第一步冻结最新 stable；从目标 bundle 原生发现规则，取 2.1.88、HitCC、2.1.220／前一批准 stable 与目标发现的候选并集，完成差分、P／R／J／M 和 Evidence 封存，再建立两个 Inventory 与 observation-only Sink | 目标版本、完整 sink inventory、跨来源候选矩阵和 EvidencePackage 可复算；没有截断或未分类项；停在 `evidence_recorded`，尚不定义目标 Schema／Snapshot 或签发 Evidence 批准 |
| `FW-F` | 先由最新 stable 证据生成 Schema、目标 Snapshot、Persona 和不可部署样例，再用同一 Schema／Compiler 表达 2.1.220 rollback fixture；批准 Profile、范围和多 Persona 合同 | target-first 样例与跨 Persona 负例通过；ApprovalFact 完整；Codex 生产收据对应最终合同；selector 未改变 |
| `FW-G` | 直接完整实现最新 stable 画像，完成受管语义层、辅助出站三态、全部 strict 入口 PAIR、DMIT candidate 和 rollback 验收 | candidate 达到 production-replacement ready；范围内断言和范围外拒绝通过 |
| `FW-H` | 灰度、回滚、激活；逐入口迁移并在闭集完成后退休遗留链 | DeploymentFact 与运行态一致；无 `retained_legacy` 才能签发迁移完成的 RemovalReceipt |

最新 stable 是目标 Schema、Snapshot 和实现的唯一设计权威。FW-E 只冻结目标规则证据，不预先用
2.1.220 建画像；FW-F 必须先生成目标 stable 画像和样例，随后才把 2.1.220 表达为差分基线、
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

源码存在不能证明 production active。当前 Claude 尚无内容寻址 Snapshot、DialectCompiler、独立
Executor authority 落点、Guard 覆盖和激活收据：FW-C 完成前不得启动任何 Claude 迁移实现；FW-D
完成前不能启动受管 stable Campaign；FW-E 完成前不能生成目标画像；FW-F 完成前不能最终冻结多
Persona 接口或建立 production candidate；FW-H 前不能晋升生产或退休遗留链。当前只能完成 FW-A
只读基线和人工规则比较；Framework FW-D 工具阻断解除后，才能从 FW-E 查询并冻结目标 stable。

---

# 第四部分 Claude Code 换版 Campaign

Framework 定义通用 Campaign、candidate、验收、激活和回滚；本部分只补充 Claude 没有官方源码、
依赖生产 bundle 逆向以及当前工具尚未齐备的差异。

| 流程检查点 | Claude 产物 | 退出事实 |
|---|---|---|
| 1. 预检 | 目标版本、官方发行物、环境和生产隔离清单 | 身份完整且 Vircs 生产不受影响 |
| 2. 官方取证 | A1、P／R／J／M、规则差分 | EvidencePackage 已封存 |
| 3. 批准 | SupportEnvelope、两个 Inventory、Persona 派生、迁移决策、目标规则、画像、场景、断言与计划范围 | ApprovalFact 已签发 |
| 4. Candidate | 独立 Release 引用、源码、测试、镜像与 inventory | ValidationCandidate 已封存 |
| 5. 比较验收 | strict 入口的逐规则结果、非迁移入口隔离和 managed 出站断言 | AcceptanceFact 或 validation-only 结论 |
| 6. 生产闭环 | 三个 Envelope、晋升、正式镜像、canary、切换、回滚、恢复和收据 | DeploymentFact 达到 `restored_active` |

## 4.1 身份、正交事实与当前工具阻断

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
| 目标 AST／词法 sink 并集与四方矩阵 | 已实现；目标新增项可生成 `add`，任何未分类项阻断封存 |
| 全 host／path 运行 inventory | 工具已支持；旧 Campaign 使用目标 host 预筛，必须新建 Campaign 重采后才能通过补强门禁 |
| 规则／覆盖门禁和 22-run 分析 | 已实现 |
| R 通道与条件 Header 探针 | 已实现 |
| TUI／`cli` 驱动 | 未攻克；无 TTY 的 `docker exec` 会卡住 |
| Campaign 编排、正交事实账本和两段式批准 | **未受管实现**；由 FW-D 建设 |
| Claude Snapshot、ReleaseArtifact Store 和画像暂存 | 受管 Store 工具由 FW-D 建设；最新 stable 目标 Snapshot 与 2.1.220 rollback fixture 尚未在 FW-F 生成 |
| candidate 冻结、四阶段封存和逐规则断言 | **未受管实现**；由 FW-D 建设 |
| 第三方入口集合 | 已知逻辑入口已列于 §3.1；物理别名、消费者、当前／目标处置尚未在 FW-E 封存，FW-F 尚未批准 |
| Persona 派生与 compatibility 语义账本 | **未受管实现**；FW-D 建工具，FW-F 批准边界，FW-G 迁移实现 |
| 辅助出站三态与运行断言 | 初始定性见 §3.3；完整 source-to-sink 尚未由 FW-E 封存，FW-F 尚未批准 |
| 晋升、正式镜像和 production active／rollback 收据 | **未受管实现**；FW-D 建设能力，FW-H 执行 |

因此当前只能完成官方取证和人工规则比较；不能把终端记录或手写 JSON 当作批准、验收或激活事实。
补齐顺序以 Framework 为准：FW-A 只读基线 → FW-B 暂定共享合同 → FW-C Codex-only 发布 →
FW-D 受管工具链 → FW-E 最新 stable 规则取证／闭集 → FW-F target-first 画像／2.1.220 rollback fixture →
FW-G 完整实现／逐规则断言 → FW-H 晋升／激活收据。

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

跨来源矩阵的每一行必须是原子命题，并记录历史源码位置、HitCC clue、历史／前一 stable 静态锚点、
目标 AST／source-to-sink、privacy gate、适用条件、目标运行场景、迁移决策和 evidence level。某来源
没有对应项时明确记 `absent`，不得省略整行。目标原生发现没有历史对应项时创建稳定的新 SPEC ID 并
分类为 `add`；历史项在目标不可达时仍保留编号并以静态不可达和运行负例共同支持 `delete`。

证据不足不等于允许永久保留 `unclassified`。已经能形成原子命题、稳定身份、原始证据路径和
`source_ids`，但尚不能证明目标 wire 的项使用 `mapped_validation`：迁移决策固定为 `add`，生命周期为
`candidate`，证据等级保留真实 `observed／blocked`，并声明 `approval_scope=validation_only`、
`production_eligibility=denied` 和明确 `validation_scope`。目标 AST 的 `observed` 只表示精确调用在目标
bundle 中存在；2.1.88／HitCC 的 `blocked` 只表示历史命题已登记。两者都不能进入 production
SupportEnvelope。disposition 与 candidate 必须通过各自身份双向引用，不能用一个笼统候选吞并未知项。

`SinkInventory` 至少覆盖 `fetch`、Anthropic SDK resource 调用、Node／Bun HTTP、TLS、socket、
WebSocket／EventSource、动态 wrapper 和可能启动外部网络客户端的子进程。每个 sink 均须保存完整列表，
禁止数量上限或抽样；同一低层 sink 的多个调用点可归并为一条规则，但每个调用点都必须反向引用唯一
disposition。下列任一条件均阻止 FW-E 退出：

1. 目标 sink 未分类、被截断、重复身份冲突或没有可复算来源；
2. 2.1.88 候选、HitCC 直接线索或历史官方规则没有唯一处置；
3. 目标新增项无法生成 `add`，或规则生成器只接受旧版本 ID；
4. strict 候选缺少目标版本运行场景，或运行观测出现 inventory 外 host／path／sink；
5. `DISABLE_TELEMETRY`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 的实际值、读取点和 gate 未绑定
   本次目标产物。关闭后的 telemetry／nonessential 分支仍登记可达性和 `record_only` 处置，其不发流量
   不计一致性差异，但不得因此删除可能影响 essential 请求构造的共享状态。

现有三份历史机器台账只证明其自身路径、ID 和当前映射一致，不自动证明语义穷尽。2.1.88 必须覆盖
`src／node_modules／vendor` 的网络相关反向切片；HitCC 每篇 `clue_source` 文档必须映射到一条或多条
原子命题，或以可复算理由标为范围外。尚未抽成 clue 的文档必须无截断枚举非代码区 Markdown 列表项，
保存原文路径、行号、最近 heading、内容摘要和稳定 ID，再逐项登记为 validation-only；已映射文档则
反向绑定其未闭合 clue candidate。扫描器无法作出语义判断时先保留 `unclassified`，不得按词法未命中
自动排除；只有上述原子化和禁止生产边界同时成立后，才能把它改为 `mapped_validation`。

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
`unclassified`；不得用 `delete`、范围外或零流量事实静默移除。`seal` 使用 v2 计划并直接绑定目标
inventory、矩阵、closure 和 capture index；closure 非 `passed`、运行样本仍使用 host 预筛、四者摘要
不一致，或 `blocked` 未同时满足 validation-only／禁止生产／明确边界／candidate+add 时，Store 不得写入
`evidence_recorded`。满足这些限制的 `blocked` 只封存发现事实，不签发 EvidenceApprovalFact 或
ProfileApprovalFact，也不允许 FW-F／FW-G 把它当成已批准规则。

补强工具或产出侧身份发生变化后，旧 Campaign 及旧 FW-E 收据只保留为历史事实，不能原位续写或补签。
必须在隔离目录新建 Campaign，并同时设置 `DISABLE_TELEMETRY=1`、
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 和全 host 捕获开关；不得用旧的目标-host 预筛样本
冒充闭集证据。

### 4.2.3 分类与批准清单

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

Claude 的 ApprovalFact 还必须绑定同一摘要的 `ProductionIngressInventory`、
`EgressDispositionInventory`、Persona 派生清单、compatibility 模式边界、预期
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

当前阻断：FW-D 尚未形成受管 Store 和独立 candidate 引用落点，FW-E 尚未冻结最新 stable 规则证据，
FW-F 尚未生成 target-first Snapshot／样例并完成多 Persona 合同冻结；三者完成前，本节不能形成受管
candidate。

## 4.4 候选验证与正式验收

Candidate 只能在 DMIT 或等价隔离环境运行，不能在 Vircs 换镜像试验。真实请求前原子冻结源码、
测试、镜像、画像、配置、代理／CA／DNS、工具和 attempt nonce；恢复或秘密扫描失败不得继续。

最低场景矩阵以已批准 SupportEnvelope 的 strict 场景为核心，并必须包含关联的边界、遗留与 managed
处置验证：

- baseline TLS／Header／Body／端点；
- client-app、remote-container、remote-session 的正负例与组合顺序；
- agent 深度、session／parent 状态和跨请求关系；
- 500 重试曲线及目标版本新增的错误分支；
- `persona_strict` 端点、隐私模式和目标 entrypoint，以及 `non_persona_managed` 出站的独立策略断言；
- 每个目标处置为 `migrated_strict` 的官方／第三方标准 API 入口，以及所有范围外入口／功能的 fail-close；
- `retained_legacy／explicitly_retired／rerouted` 的隔离、退休或目标路由断言；
- Persona system／identity 派生记录，以及 compatibility 模式不会混入 strict 的负例。

每个场景按 `prepare → capture → seal → approve` 封存；结果必须绑定原始证据 inventory，失败 attempt
不得覆盖。只有 TranslationReport 为 lossless 的官方入口与第三方入口，才对同一规范化语义执行同一
`PAIR-<SPEC-ID>`；动态值比较来源、格式、关系和生命周期。Persona 固有 system／identity 事实按已
批准派生记录比较；有损 compatibility 请求不得混入 strict PAIR 分母。

验收前至少运行：

```bash
python3 tools/official_client_capture/claude_21220/check_coverage.py
```

逐规则结果必须唯一覆盖 SupportEnvelope 内的目标全集。`blocked` 或 `regressed_evidence` 只能形成
validation-only；`ready` 只证明固定 candidate 通过已批准规则，不表示画像已晋升、正式镜像已构建或
生产已切换。当前 41 项 `observed` 必须逐项选择并留痕：补足目标版本证据达到批准等级；缩小
SupportEnvelope，使该规则确实不再承担范围内的出站对齐责任并为范围外请求建立 fail-close；或保持
validation-only。不得把 `observed` 改名、用遗留 wire 佐证，或在仍可到达该规则时仅从清单删除。

当前阻断：Claude 尚无 candidate 编排、四阶段封存、PAIR 断言和 accept 工具，本节不能形成受管 ready。

## 4.5 生产、回滚与证据归档

只有 production-replacement candidate 达到受管 `ready`、FW-G 退出条件满足并进入 FW-H，才能依次执行：

首次生产激活前必须冻结并验证真实 rollback 目标：可以是已独立验收的旧 Release，也可以是当前生产
遗留实现及其固定镜像／配置。遗留实现作为操作回退点时，其 wire 仍是 diagnostic-only，不会因此成为
官方证据或 approved Profile；不得复制 active 摘要伪造 rollback Release。

2.1.220 只能在其独立批准的窄 SupportEnvelope 内作为 strict rollback；当前 12 verified／41 observed
状态不支持把目标 stable 的完整范围自动交给它回退。激活前必须冻结并验证
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

当前阻断：Claude 尚无晋升和激活收据，2.1.220 Campaign／run／提取物仍是唯一证据时不得删除。

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
