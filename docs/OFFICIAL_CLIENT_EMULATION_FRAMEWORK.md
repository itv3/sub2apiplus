# 官方 OAuth 客户端出站仿真共享框架

> **适用范围**：Sub2APIPlus 官方 OAuth 客户端出站仿真的共享控制框架，当前覆盖 Codex CLI 和 Claude Code。
> **文档职责**：本文定义共享架构、版本画像的可信证明及公共维护流程；客户端事实与专用步骤见各自手册。
> **文档边界**：本文不定义具体客户端的 wire、版本、机器环境或当前状态；共享合同冲突时以本文为准。
> **文档体系**：人类可读权威规范仅本文、[`CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](CODEX_CLI_CLIENT_EMULATION_GUIDE.md)
> 和 [`CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md)；机器证据、JSON 台账、
> Schema、Campaign Store 和收据不得形成第二套规范。新增 Persona 必须扩展本文和最接近的客户端手册，
> 即使重新划分两份专属手册的职责，也不得新增第四份人类可读规范。

---

# 第一部分 仿真目标与等价边界

## 1.1 两类客户端的仿真目标

当业务系统最终选择官方 OAuth 账号出站时，入站客户端不拥有最终 wire。兼容层只负责协议、模型、
工具和请求语义转换；Key、Group、账号路由、调度及计费归属仍由原业务系统管理。

| 仿真对象 | 允许的正向入站 | 最终出站规则 | 拒绝边界 |
|---|---|---|---|
| Codex CLI | 官方 Codex CLI，以及通过 Codex、Compatible、Responses 等接口接入且能够无损转换的第三方客户端 | 只要最终使用 OpenAI OAuth 账号出站，最终 wire 均由 Codex CLI 的 production active 版本画像统一生成 | 无法无损转换、未登记协议或范围外语义不得进入 strict 出站 |
| Claude Code | 仅接受已登记的 Claude 官方客户端 | 使用 Anthropic firstParty OAuth 出站时，最终 wire 均由 Claude Code 的 production active 版本画像统一生成 | 第三方客户端、未登记版本或不匹配的官方客户端形态在读取 OAuth 凭据前直接拒绝 |

两者共享同一出站所有权模型，关键策略差异在入站准入范围；各自的 wire 事实仍完全独立。请求通过
准入后，均由对应 Persona 的 active ReleaseBundle、方言编译器和受信执行链生成最终 wire。入站名称、
版本、User-Agent、Header、协议形态或自报身份均不能选择生产画像，也不能直接透传为官方客户端身份。

## 1.2 最终 wire 的等价标准

“一致”不是固化某次抓包中的随机字节，而是在相同平台、入口、配置、账号、模型和触发条件下，按以下
可观测维度比较目标版本官方客户端与 Sub2APIPlus 的最终出站：

| 验收维度 | 等价要求 |
|---|---|
| 静态 wire | method、URL、Header 名称／大小写／顺序／值、Body 字段／类型／顺序、压缩和帧形态一致 |
| transport | 在证据覆盖的平台与条件下，TLS、ALPN、HTTP／WS、连接复用和重试行为一致 |
| 动态事实 | 生成来源、格式、相等关系、作用域、复用和生命周期一致，不比较单次随机字面值 |
| 条件行为 | 相同受信条件产生相同分支；条件不成立时按官方行为省略或采用对应结果 |
| 跨请求状态 | 会话、turn、agent、retry 等状态的建立、消费、回送和失效关系一致 |
| 合法关闭的外围流量 | 在同一已冻结配置下，官方允许关闭的遥测与非必要流量不进入 strict 等价分母，其零流量不计为差异；实际触发的 essential 请求仍逐规则比较 |

该规则不表示“行为层整体不比较”；条件分支、重试和跨请求状态仍按上表验收。隐私／遥测配置属于证据
身份，官方与 candidate 配置不同、配置未冻结或关闭能力未绑定目标版本时，均不得据零流量得出一致结论。

Codex CLI 的所有批准正向入口都必须使用语义等价请求执行最终 wire 成对验收。Claude Code 只对已登记
官方客户端执行正向验收；第三方客户端只进入凭据前拒绝的负例分母，不建立正向 wire 对拍责任。

每条结论都必须声明适用版本、平台、入口、认证、模型、配置和证据边界。没有证据覆盖的产品、端点、
平台或后继版本不得使用“完全一致”等全称表述。

## 1.3 统一链路与所有权边界

```text
入站请求
→ Persona IngressPolicy／OfficialIngressCatalog 准入
→ IngressProtocolAdapter
→ CanonicalRequest + TranslationReport
→ 受信账号路由 + Persona Registry
→ PersonaPlanner + Identity Authority
→ Persona 的 production active ReleaseArtifact／ReleaseBundle
→ Persona DialectCompiler
→ CompiledEnvelope
→ 该 Persona 的 Executor authority 实例 + 受信 transport adapter
→ Runtime Guard
→ 官方 OAuth 上游
```

该链路只有三项核心所有权：

1. `IngressProtocolAdapter` 只转换请求语义，不选择账号、Persona、生产版本或最终 wire；
2. `PersonaPlanner + Identity Authority` 只能从规范化语义和受信事实生成 Persona 专属计划与身份；
3. active ReleaseBundle、DialectCompiler、Executor 和 Guard 共同拥有最终出站定型，后续普通业务代码不得改写。

语义无损判定、Persona 固有事实派生和 compatibility 隔离的详细合同见 §2.3。

---

# 第二部分 共享运行架构

## 2.1 Persona 身份与边界

一个 Persona 至少由以下事实共同确定，不能只用 provider、host 或入站自报身份代替：

```text
OfficialClientPersona =
  provider + official_product + auth_family + upstream_route_family
```

官方 CLI、桌面应用、API Key mimic 或其他产品即使访问同一厂商，也可能属于不同 Persona。只有官方
客户端确实使用可取证的官方 OAuth 路径时，才属于本文范围；其他认证方式必须另立产品边界。

## 2.2 分层执行架构

| 层 | 核心责任 | 禁止事项 |
|---|---|---|
| 入站准入与适配 | 由 IngressPolicy／OfficialIngressCatalog 决定准入，并生成 `CanonicalRequest + TranslationReport` | 选择账号、Persona、生产版本，或保留入站 wire 身份 |
| Persona 规划与状态 | Registry 解析 Persona；Planner、Identity Authority 和私有 State Store 生成专属 Plan、身份及跨请求状态 | 从不可信 Header 复制官方身份、跨 Persona 复用状态，或选择生产版本 |
| Release 控制 | ReleaseArtifact Store 保存不可变画像；Runtime Selector 独立解析 production active／rollback | 原位覆盖 Release，或让自动发现、入站版本和 validation candidate 直接激活 |
| 方言编译 | Persona 自有 Plan、IdentityFacts、ProfileSchema 和 DialectCompiler 生成最小 `CompiledEnvelope` | 共享另一 Persona 的 Schema／Compiler，或把厂商 wire 事实塞入共享内核 |
| 执行与传输 | 每 Persona 的 Executor authority 管理 attempt、签发 Token，并调用受信 transport adapter | Token 签发后改写 wire，或跨 Persona 复用 issuer、invocation、连接和状态 |
| Runtime Guard | 校验 route、Persona、Sink、Release、画像和最终请求摘要 | 对未知路径、身份冲突或终态篡改静默放行 |

`IngressProtocolAdapter` 按入站协议实现，而不是按 Persona 复制。只有出现现有协议无法表达的新入站
协议时才新增 adapter；新增 Persona 默认复用已有 adapter，从而避免形成“协议数量 × Persona 数量”的
适配器矩阵。

## 2.3 语义、状态与最终定型不变量

**语义转换。** `TranslationReport=lossless` 只证明用户提交的消息角色、顺序、system、工具、模型
意图和流式语义无损进入 `CanonicalRequest`。只有被 IngressPolicy 批准且转换无损的入口才能进入
strict 正向链；`official-client-only` Persona 的第三方入口只作为准入负例。

**Persona 固有事实。** 官方客户端固有且已有证据的 system blocks、metadata、设备或会话事实，可以由
PersonaPlanner／Identity Authority 受管派生。每项派生必须记录目标字段、权威来源、证据或规则、派生
原因、作用域、生命周期和冲突处置，并纳入 Identity／Dialect attestation；不得回写或冒充用户语义，
也不得由协议适配器临时猜测。

**有损转换。** 角色重排、删除用户 system 或把 system 改写为 user message 均不能标记为 `lossless`。
strict 路径必须拒绝；确有历史兼容需要时，只能进入明确批准的 compatibility 模式，并与 strict Release、
验收结果和生产流量范围隔离。禁止的是无权威来源或无派生记录的伪造，不禁止上述 evidence-derived 事实。

**状态与 authority 隔离。** 会话、工具往返、agent 谱系、fallback 和 request-id 等跨请求状态必须保存
在 Persona／Release 私有的持久命名空间，通过 CAS、有限租约、TTL 和关联身份原子提交。存储不可用、
状态损坏、CAS 冲突耗尽或租约非法时 fail-close；进程内缓存不能成为生产权威。每个 Persona 的 Executor
authority、Token issuer、invocation 和连接身份必须独立，共享的是实现而不是运行状态。

共享内核消费的 `CompiledEnvelope` 只允许包含以下厂商无关事实：

| 事实组 | 允许内容 |
|---|---|
| 归属与证明 | Persona、Release／Profile／Bundle digest，以及 Identity／Dialect attestation digest |
| 出口与能力 | Sink、Route、Endpoint、method、protocol 和 `TransportCapability` 引用 |
| 调用与重放 | invocation／attempt 身份、重试预算和 Body 可重放性 |
| 最终请求能力 | Prepared request capability、最终请求摘要和 single-use FinalizationToken 所需事实 |

共享内核不得出现厂商 Header／Body Policy、版本常量、`CodexIdentityMode`、
`ClaudeBetaPolicy` 或单一 Persona 的 fallback／状态字段。Header、Body、协议、状态机、重试及
transport 参数均属于 Persona 画像和方言；共享接口的新增与冻结按 §5.4 执行。

## 2.4 Persona 接入合同

新增或重建 Persona 时，必须登记以下五组合同：

| 合同组 | 必须提供的内容 |
|---|---|
| 身份与准入 | PersonaDescriptor、IngressPolicy、SupportEnvelope；official-client-only 还必须提供内容寻址 OfficialIngressCatalog |
| 语义与方言 | adapter 复用／新增结论、PersonaPlanner、TypedEgressPlan、IdentityFacts、ProfileSchema 和 DialectCompiler |
| 状态与执行 | 私有状态命名空间、Route／Sink 闭集、Executor authority、TransportCapability 和 Guard 绑定 |
| 画像与证据 | 不可变 ReleaseArtifact、EvidencePackage、AtomicAssertionLedger 和 RequiredRules 映射 |
| 验收与生产范围 | ProductionIngressInventory、EgressDispositionInventory、正负 PAIR、AcceptancePackage 及 active／rollback／deployment 范围 |

第三方 Agent 工具只适用于已批准第三方入口的 `canonical-semantic` Persona。工具目录必须冻结
产品、版本和摘要，并选择有官方证据的内置无损映射、经官方 MCP 取证的双向 bridge 或 `denied`；
不得原样透传第三方工具。`official-client-only` Persona 一律拒绝第三方工具目录。Codex 的具体映射
合同见其专属手册。

完成合同登记只表示共享架构能够承载该 Persona，不表示仿真已经成立。必须先取得独立官方证据并用
专属 Plan、Schema 和 DialectCompiler 表达，再按 §5.4 判断是否需要扩展共享层；客户端之间不得交叉
继承 wire 事实。

## 2.5 共享代码边界与 Guard 合同

§2.2 的分层合同在当前仓库中统一映射为：

```text
service ───────────────────→ officialegress core ←──────── repository 窄 port
officialegress/adapter/* ───→ core + 受信物理资源
wiring ────────────────────→ 闭集 adapter 注册
```

`officialegress` core 不得 import `service` 或 `repository`。可共享的是 Artifact Store、Runtime Selector、
编译注册、Executor 实现、FinalizationToken、Guard 和 adapter 注册合同；每个 Persona 的 IngressPolicy、
OfficialIngressCatalog、Plan、IdentityFacts、ProfileSchema、DialectCompiler、Executor authority、Token
issuer、invocation／状态命名空间及 transport 参数必须独立。业务层只提交规范化语义和受信业务事实，
repository 只提供窄资源能力；两者均不得选择 Persona Release 或改写最终 wire。

Guard 统一校验 method、route、Persona、Sink、binding、Release／Profile、adapter、FinalizationToken 和
最终请求摘要。状态只允许 `legacy_observe → canary_enforce → enforced` 单调推进，`enforced` 仅能受控
回退到 `canary_enforce`；`legacy_observe` 只记录已冻结遗留基线，不能成为长期 passthrough。未知 route、
无效 binding、跨 Persona 身份或终态篡改在 canary／enforced 必须 fail-close。

---

# 第三部分 版本画像的建立与可信证明

本部分回答：运行时采用的官方客户端版本画像凭什么可以被信任、批准并进入生产。这里只定义事实关系
和身份边界，具体换版步骤见 §5.3。

## 3.1 画像如何成为不可变 Release

版本画像必须内容寻址、不可变且可复算。`persona + version + profile digest` 构成 `ReleaseArtifact`
坐标；`ReleaseBundle` 绑定画像、端点、transport、状态机制和 Persona 自有策略。以下事实相互正交，
不能压缩成单一状态：

| 维度 | 事实 |
|---|---|
| Discovery | 自动或人工发现的上游版本，仅供评估 |
| Evidence | 官方产物、证据绑定及 `verified／observed／blocked／regressed_evidence` 等充分度 |
| Approval | 已批准的画像、`SupportEnvelope` 和目标规则集合 |
| Validation | candidate ID、固定源码／镜像／Release 引用和验收结果 |
| Runtime Selector | 每个 Persona 的 `production_active` 与 `production_rollback` 引用 |
| Deployment | canary、切换、回滚、恢复和 activation receipt 证明的运行事实 |

Runtime Selector 只保存带类型引用：active 必须指向可加载的不可变 Release；rollback 可以指向另一
Release，或只含 revision、镜像 digest 和回退收据的已演练 `operational_deployment`，后者不能取得
Profile 或参与 final wire 编译。Validation candidate 使用独立 Release 引用，不得占用或伪装成
production rollback；首次生产激活的回退要求见 §4.2。

Catalog 在启动时统一校验 manifest、Profile／Wire 内容摘要和 Approval。所选 Release 必须派生
changeset、状态命名空间、连接池和执行 policy 身份，业务代码不得另存版本或摘要常量。旧画像不得
原位覆盖；自动发现、入站自报版本、测试通过或 Campaign 状态均不能改变或证明 production active。

## 3.2 证据如何形成批准

客户端规则至少绑定版本、产物摘要、平台、入口、认证、模型、配置、网络条件、观测通道、实测分母、
条件对照和适用边界。条件规则采集官方正反样本；无条件规则使用多个适用样本、零违规分母和 candidate
负断言闭环，不伪造官方负例。pcap、原始应用层字节、MITM、源码或 bundle 控制流只能证明各自可见
的事实，不能互相替代。

合法零流量必须绑定目标版本／产物摘要、配置值、读取点或 gate、适用端点和运行场景，只能登记为
supporting-fact 或客户端专用的 record-only 处置，不能生成 RequiredRule，也不计为 candidate 差异。
零流量不能证明 Sink 不存在、删除发现项、缩小 SupportEnvelope 或移除 essential 请求依赖的共享状态；
场景实际触发的 essential 出站仍须取得正常正反证据并逐规则验收。

| 维度 | 允许值示例 |
|---|---|
| `evidence_level` | `verified`、`observed`、`blocked`、`regressed_evidence` |
| `rule_lifecycle` | `candidate`、`active`、`superseded` |
| `compatibility_class` | `request_egress`、`response_compat`、`not_applicable` |
| `migration_decision` | `inherit`、`change`、`add`、`delete`、`condition_change` |

证据链固定为 `DiscoveryInventory → SemanticRuleCandidate → AtomicAssertionLedger → RequiredRules`。
Inventory 无截断保存且封存后不可改写；候选仅按 `source_ids` 归并待验证语义。`mapped_validation` 只能
支持封存当前 EvidencePackage，不是证据等级、迁移决策或终态处置，也不能进入生产批准。

不得从发现 ID 或 `CAND-*` 机械生成 `SPEC-*`。候选收敛前保持规则台账和生产资格均为 `denied`；每条
原子断言必须恰好归属一个 RequiredRule 或明确的 scenario-only／supporting-fact 组，客户端本地行为
不能因结果出现在请求中就自动成为网关 RequiredRule。

`DiscoveryDispositionLedger` 基于冻结 Inventory 只写追加，并为每个发现提供唯一终态；未分类、映射
待验证、上下文登记和未收敛候选均阻断 ApprovalFact。终态必须解析到可执行规则、明确支撑事实、受管
出站处置、目标证据证明的非出站／历史缺失事实或规范重复项；缩小 SupportEnvelope、词法未命中和
“留待后续版本”均不是终态。ApprovalFact 必须同时绑定该 Ledger、RequiredRules 和 SupportEnvelope。

只有 `evidence_level=verified` 表示证据与复算链闭环。字符串相同、窗口邻近或 minify 标识符相同不能
单独支持 `inherit`；语义片段、依赖和 sink 关系无法证明时，必须重新观察目标版本。VC 操作统一见 §5.3。

## 3.3 变更如何生成新身份

| 单元 | 必须新建身份的变化 |
|---|---|
| Campaign | 官方目标版本、产物、平台、入口、默认条件、正式／预检模式、升级用途、冻结规则／场景输入或影响 EvidencePackage 语义的产出工具变化 |
| ApprovalFact | DiscoveryDispositionLedger 摘要、SupportEnvelope、目标规则、迁移决策、画像、场景、断言或批准用途变化 |
| candidate | ApprovalFact 引用、Sub2APIPlus 源码、测试树、构建、镜像或候选用途变化 |
| attempt | 上述身份不变，仅因临时网络、额度或运行失败重试 |

四者及其证据、发布图、selector 变更和收据均只写追加。新 ApprovalFact 不覆盖旧批准，新 attempt
不覆盖旧失败；candidate 验收通过不表示已经部署，也不能改变 production selector。源码常量、测试
结果或流程状态均不能替代 DeploymentFact 和实际激活收据。

现有 Persona Schema 能表达变化时只追加画像数据；需要新协议、新状态机制或 Schema 字段时，优先扩展
该 Persona 方言。只有最小 `CompiledEnvelope` 或共享终态控制确有缺口时，才按 §5.4 修改共享引擎并
回归全部受影响 Persona；具体变更分类以 §5.1 为唯一入口。

---

# 第四部分 候选验收与生产启用

本部分回答：候选达到什么条件才能承接生产流量，以及如何保证能够安全回退。这里只定义验收门槛和
生产不变量；换版阶段见 §5.3，候选交付、实际激活和回滚步骤见 §5.6。

## 4.1 候选如何通过 strict 验收

每个 candidate 必须冻结用途和 `SupportEnvelope`。`validation_only` 可以保留已记录的
`blocked／regressed_evidence`，但止于诊断；`production_replacement` 必须覆盖范围内全部
request-egress 规则并达到批准等级，范围外条件由 PersonaPlanner／Compiler fail-close。

每条原子断言建立独立 `PAIR-*`，RequiredRule 的结果由其全部原子断言合取。批准的正向入口必须在同一
candidate、画像和条件下以语义等价请求证明最终 wire 收敛；未批准入口只进入凭据前拒绝的负例分母。
动态字段比较来源、格式、关系和生命周期；scenario-only 只验证客户端输入边界。

`SupportEnvelope` 必须绑定同一时点的 `ProductionIngressInventory`，每个逻辑入口及其全部物理别名取得
以下且仅以下一种处置：

| 入口处置 | 验收语义 |
|---|---|
| `migrated_strict` | 位于批准范围内，并按入口类别完成全部逐规则断言 |
| `retained_legacy` | 继续走独立可观测、可回滚的冻结遗留链，并阻止遗留代码退休 |
| `explicitly_retired` | 已证明无生产消费者或完成有意下线，并保留不可覆盖的退休证据 |
| `rerouted` | 已迁往明确的非本 Persona 产品路径，并验证 route、认证和失败行为 |

Inventory 分开记录 `current_disposition` 与 ApprovalFact 的 `target_disposition`，目标计划不得覆盖当前事实。
`official-client-only` 的第三方入口使用 `explicitly_retired + denied_before_oauth`；未知别名、无处置入口或
新增裸调用均阻断验收。仍有 `retained_legacy` 时可以缩小灰度，但不得签发 RemovalReceipt 或删除遗留链。

`EgressDispositionInventory` 为每个已知出站选择以下且仅以下一种处置：

| 出站处置 | 验收语义 |
|---|---|
| `persona_strict` | 由画像、Compiler、Executor 和 Guard 管理，并纳入适用 SPEC／PAIR 与 SupportEnvelope |
| `non_persona_managed` | 不主张官方 wire 等价，但登记 route／Sink、认证、endpoint、client、超时、重试、秘密和审计策略 |
| `denied` | 未批准、未知或不应发出的路径，运行时 fail-close |

Persona OAuth 出站即使不属于 strict，也必须进入 `non_persona_managed` 或 `denied`。入口准入沿用 §2.3；
任何目标规则仍为 `blocked／regressed_evidence` 时，candidate 不得成为 strict active，验收结果也不能
反向提升官方证据等级。

SupportEnvelope 含跨请求状态时，必须在状态转换间重建 Runtime、重启应用并复核后续 wire、状态消费和
startup 去重；同时覆盖长历史、工具成功／失败续轮、并发 CAS、租约释放及状态存储不可用。单进程连续
基础请求不足以证明该状态机制达到 production replacement 等级。

## 4.2 如何安全激活与回滚

只有通过 §4.1 的 `production_replacement` 才能申请生产激活。生产事实必须绑定同一 candidate 的
AcceptanceFact、active Release／画像、正式镜像、selector、终态门禁和 DeploymentFact／activation
receipt；任一不一致均为 `production_unverified`，不得继续或扩大流量。具体操作顺序见 §5.6。

Runtime Catalog 按 Persona 独立构造并原子发布。一个 candidate 或未启用 Persona 的画像无效，不得影响
其他 Persona；已启用 Persona 的 active 无法验证时，其 route 必须 fail-close，不能借用另一 Persona 或
rollback 画像发送。进程级停止或 Persona 级隔离必须由部署策略预先固定，禁止静默降级和跨 Persona 级联。

每次生产激活或扩大灰度前，必须冻结：

| 范围 | 含义 |
|---|---|
| `ActiveSupportEnvelope` | active Release 经 ApprovalFact 批准并通过 strict 验收的能力范围 |
| `RollbackOperationalEnvelope` | rollback 镜像、selector、配置、依赖和路由经真实回退演练的可运行范围 |
| `DeploymentTrafficEnvelope` | 本次准备切入 active deployment 的实际生产流量范围 |

```text
DeploymentTrafficEnvelope
⊆ ActiveSupportEnvelope
∩ RollbackOperationalEnvelope
```

不满足时禁止激活或扩大流量，只能收窄 DeploymentTrafficEnvelope、补足 active／rollback 证明，或让
范围外入口保持 `retained_legacy／rerouted`。RollbackOperationalEnvelope 只证明操作回退可运行，不会
提升 rollback 的官方 wire 证据；冻结遗留部署可作为 operational rollback，但输出仍是 diagnostic-only。

---

# 第五部分 版本与系统变化时如何维护

本部分回答：版本或系统发生变化时，应走哪条维护路径、生成哪些新身份，以及如何交付或回退。所有
变更先经 §5.1 分类；同时包含 Sub2API 上游更新和官方客户端升级时，必须先执行 §5.2，再执行 §5.3。
客户端手册只能补充证据来源、画像差异、场景、命令和环境约束，不得改写共享状态语义；当前版本、
机器、账号、候选进度和历史收据只在对应客户端手册及其机器制品中记录。

## 5.1 先判断属于哪种变更

开始修改前必须先选择以下且仅以下一种主变更类型；若一个需求同时命中多类，必须拆成按依赖顺序执行
的独立变更集：

| 主变更类型 | 识别条件 | 公共流程入口 | 最低身份变化 |
|---|---|---|---|
| Sub2API 上游更新 | 合并新的 upstream commit，目标官方客户端版本不变 | §5.2 | 独立 upstream changeset；按影响判断新 candidate 或同版本后继 Campaign |
| 官方客户端换版 | 官方产品版本、发行物、依赖、平台、默认条件或目标画像变化 | §5.3 | 新 Campaign；后续生成新 ApprovalFact 和 candidate |
| 共享合同／运行时变化 | Registry、Store、Selector、Executor、Token、Guard 或最小 CompiledEnvelope 合同变化 | §5.4 | 后继共享合同和全部受影响 Persona 的新验证事实 |
| 同版本实现变化 | 官方规则、画像、场景和产出侧证据工具不变，仅实现、测试或构建变化 | §5.5.1 | 原 Campaign 下的新 candidate |
| 旧运行画像／兼容代码退休 | 删除不再被 active／rollback 引用的运行画像，或遗留入口、finalizer、旁路、类型与构造接线 | §5.5.2 | 独立退休变更集和 RemovalReceipt |
| 纯文档澄清 | 不改变规范语义、机器事实、代码、画像、场景或门禁 | 对应文档直接修订 | 不复用或覆盖任何历史收据 |

官方客户端换版不得夹带 Sub2API 上游合并或无关重构；上游更新也不得顺手改变目标客户端版本。两类
变化同时存在时，必须先按 §5.2 完成独立上游变更集，证明现有 Persona 的 production active／rollback
无非预期 final-wire 差异并冻结新基线，再按 §5.3 新建客户端换版 Campaign；两者不得共享 Campaign、
candidate 或批准事实。

若规则、SupportEnvelope、画像、断言、场景或产出侧证据工具发生变化，即使官方版本字符串不变，也
必须建立同版本后继 Campaign，不能按普通实现变更处理。Campaign、ApprovalFact、candidate 与 attempt
的身份边界以 §3.3 为准，所有事实只写追加。

### 5.1.1 全章公共执行约束

除纯文档澄清外，每个独立变更集必须在首个动作前冻结墙钟预算、同根因重试上限、资源水位、适用时的
证据复用决定和只追加计时台账。计时连续覆盖运行、等待、诊断、审批和恢复；组合变更分别记账，并从
首个受管阶段（如 `U-0`／`VC-0`）到最终交付或激活保留端到端墙钟。

- 同一根因连续失败两次即停线；独立修复、离线回归和干净预检通过前，禁止第三次 live attempt 或换
  Campaign／candidate 绕过。
- 身份及环境连续时只读承接并重放已通过门禁，只补跑失败项；发生变化时只重跑受影响闭集。无法证明
  连续性即停线，不得复制收据或反复全量碰撞。规范明确要求的独立终态全量重放不属于失败补跑。
- 读取、构建和清理由显式 manifest 限界；达到资源水位后只清理未被收据引用的可再生资产，禁止无界
  递归和删除历史证据。

预算到期必须保存最后合法身份并输出阶段、根因、墙钟、重试／live 请求计数、资源水位、最后成功收据及
唯一下一动作。缺少可重放的计时、连续性或资源收据时只能停在首阶段；客户端升级的固定预算见 §5.3.5。

## 5.2 合并 Sub2API 上游更新

### 5.2.1 目标、边界与完成条件

本节是 Sub2API 上游合并的共享操作规程，只更新受维护的源码基线，不改变 Codex CLI 或 Claude Code
目标版本。每个 upstream commit 必须使用独立 changeset、计划和证据目录；
`tools/upstream_merge/` 负责 `U-0～U-6` 状态约束，不负责选择上游目标、代替人工判断、生成客户端专用
证据、推送远端或部署生产。

```text
Framework §5.2 的 U-0～U-6 全部完成
∧ Codex 三类门禁（active wire／rollback wire／ingress matrix）全部 passed
∧ Claude 三类门禁（active wire／rollback wire／ingress matrix）全部 passed
∧ §5.1.1 公共执行收据完整
∧ 当前工具阻断数 = 0
⇒ upstream_source_baseline_updated
```

完整 v2 计划、阶段制品、客户端收据、12 类门禁、冲突独立复算或最终重放任一缺失／失败，阻断数
即大于 0；只有 `finalize` 与 `replay` 均成功才封存 `current_tool_blocker_count=0`。该状态只证明
本地受维护分支完成受审合并，不表示 candidate 已交付或生产已更新。

### 5.2.2 准备计划、门禁与人工输入

1. 在干净的受维护本地分支上获取并人工确认目标 tag 的完整 commit；不得使用浮动远端分支，工具也不联网
   猜测 latest。
2. 按 `tools/upstream_merge_request.schema.json` 编写只读 `UpstreamMergeRequest v1`，绑定 upstream、
   受维护分支、目标版本和 commit、active／rollback、两类 Inventory、运行状态、恢复点、受保护路径，
   以及不存在的隔离 worktree 和空且权限为 `0700` 的证据目录；要求排序的字段必须保持排序。
3. `gates` 按 `id` 排序并恰好覆盖下表六类客户端门禁，以及 `cross_persona`、`inventory_closure`、
   `original_business`、`secret_scan`、`shared_full_regression`、`shared_static` 六类共享门禁。客户端收据
   使用 `receipt_replay` 并显式消费 `{receipt}`。
4. 门禁以 `argv` 数组执行，不经 shell；只允许 `{candidate_commit}`、`{candidate_tree}`、`{evidence_root}`、
   `{plan}`、`{receipt}`、`{repository}` 占位符。六类客户端门禁按下表定义，不再由客户端手册另设上游
   更新步骤。

| 门禁 | 必须重放的客户端事实 |
|---|---|
| `codex_active_wire` | 当前 Active Release／Profile 的 final-wire 空允许列表；覆盖 HTTP／WS、realtime、compact、images、文件上传、OAuth refresh、WHAM、Route／Sink 与 turn-state |
| `codex_rollback_wire` | 当前 Previous Release／Profile 的 final-wire 空允许列表，以及 rollback 路由和选择器可恢复性 |
| `codex_ingress_matrix` | 官方及已批准第三方入口的完整正负矩阵，以及入口到批准 Persona／Profile 的唯一绑定 |
| `claude_active_wire` | 当前 production active Release／Profile、OfficialIngressCatalog、ModelCapabilityCatalog 和全部 strict egress 的 final-wire 空允许列表 |
| `claude_rollback_wire` | 当前 rollback Release／Profile、Catalog、模型能力与 strict egress 的 final-wire 空允许列表及可恢复性 |
| `claude_ingress_matrix` | 官方 Messages／count_tokens 正例，以及第三方 Messages／count_tokens、Chat Completions、Responses、未登记版本、System／工具目录的凭据前拒绝负例；同时重放当前受审 RequiredRules、原子断言、模型能力和 Catalog 摘要 |

`U-2` 还必须复核 Codex 手册 §3.5.2 的高风险接缝，以及 Claude／Anthropic source-to-sink、入口别名和
Persona Guard。新增官方出站不得使用裸 client；ReleaseCatalog／SnapshotCatalog、版本泄漏 baseline、
账号 UA 和 `discovered_latest` 不得因本次合并取得 active 选择权。`U-4` 的共享门禁必须包含
`check_ledger_completeness.py`、`check_version_leak.py` 和完整 `make check-egress-spec`；范围无法可靠收窄时，
执行相应客户端手册第四部分的完整候选验收。

人工输入均为只写一次的严格 JSON：先编写不含 `identity_sha256` 的草稿，再用 `identity-seal` 写入新文件；
禁止手填自摘要或覆盖旧输入。

| 输入 | 使用条件 | 核心内容 |
|---|---|---|
| `ConflictResolutionInput v1` | 合并有冲突 | 每个冲突路径的 `fork／upstream／manual` 决策、理由和解决后 blob |
| `SourceChangeInput v1` | 自动 Codex overlay 之外还有变化 | merge commit、全部额外路径及理由；不得重复登记自动 overlay |
| `SurfaceDecision v1` | route 或 source-to-sink 有 delta | 每个 delta 的唯一处置、理由和候选 Inventory 条目 |
| `ChangeDecision v1` | 每次合并 | 逐文件／逐 delta 分类、动作及身份／证据语义变化判定 |
| `CandidateDispositionInput v1` | 12 类门禁全通过 | 两个 Persona 的处置模式及 Campaign／candidate／Approval／Acceptance、共享合同和原业务收据 |

`UpstreamMergePlan v1` 与已封存早期台账只用于历史重放。新合并只允许 v2；计划生成后若工具、Schema
或门禁漂移，必须作废计划，先独立补齐并测试工具，再从 `U-0` 重新开始。

### 5.2.3 按 U-0～U-6 执行

顺序固定且 CLI 会验证前置状态：`U-0` 冻结计划；`U-1` 形成可重放的双父 merge commit；`U-2` 唯一写入
新 Codex overlay 并闭合发送面；`U-3` 完成逐项影响分类；`U-4` 要求 12 类门禁全部通过，任一失败或
污染均生成 `blocked` 收据；`U-5` 处置必须与 `U-3` 推导一致并绑定共享／原业务收据；`U-6` 仅以
`--ff-only` 快进本地分支，并通过 finalizer 与独立 replay。

以下变量均为绝对路径；`PLAN` 固定为请求中的 `evidence_root/plan.json`。先按标准路径执行；出现冲突、
额外源码变化或发送面 delta 时，用后面的条件命令替换对应步骤。

```bash
#U-0：生成并复算完整计划
python3 -m tools.upstream_merge plan-create --repository "$REPO" --request "$REQUEST"
python3 -m tools.upstream_merge plan-validate --repository "$REPO" --plan "$PLAN"

#U-1：标准路径无冲突
python3 -m tools.upstream_merge merge-start --repository "$REPO" --plan "$PLAN"
python3 -m tools.upstream_merge merge-seal --repository "$REPO" --plan "$PLAN"

#U-2：标准路径无额外源码变化，四个发送面均为零差异
python3 -m tools.upstream_merge source-seal --repository "$REPO" --plan "$PLAN"
python3 -m tools.upstream_merge surface-scan --repository "$REPO" --plan "$PLAN"
python3 -m tools.upstream_merge inventory-carry-forward --repository "$REPO" --plan "$PLAN" --client claude --kind ingress
python3 -m tools.upstream_merge inventory-carry-forward --repository "$REPO" --plan "$PLAN" --client claude --kind egress
python3 -m tools.upstream_merge inventory-carry-forward --repository "$REPO" --plan "$PLAN" --client codex --kind ingress
python3 -m tools.upstream_merge inventory-carry-forward --repository "$REPO" --plan "$PLAN" --client codex --kind egress
python3 -m tools.upstream_merge surface-seal --repository "$REPO" --plan "$PLAN"

#U-3：生成影响分母并封存逐项决策
python3 -m tools.upstream_merge impact-generate --repository "$REPO" --plan "$PLAN"
python3 -m tools.upstream_merge identity-seal --input "$CHANGE_DRAFT" --output "$CHANGE_INPUT"
python3 -m tools.upstream_merge impact-seal --repository "$REPO" --plan "$PLAN" --decision "$CHANGE_INPUT"

#U-4：attempt id 必须全新
python3 -m tools.upstream_merge gates-run --repository "$REPO" --plan "$PLAN" --attempt-id attempt-001

#U-5：封存两个 Persona 及共享／原业务回归的处置
python3 -m tools.upstream_merge identity-seal --input "$DISPOSITION_DRAFT" --output "$DISPOSITION_INPUT"
python3 -m tools.upstream_merge disposition-seal \
  --repository "$REPO" --plan "$PLAN" --input "$DISPOSITION_INPUT" \
  --verification-receipt "$VERIFICATION_RECEIPT"

#U-6：仅快进本地受维护分支，然后最终化并独立重放
python3 -m tools.upstream_merge apply --repository "$REPO" --plan "$PLAN"
python3 -m tools.upstream_merge finalize --repository "$REPO" --plan "$PLAN"
python3 -m tools.upstream_merge replay --repository "$REPO" --plan "$PLAN" --receipt "$UPSTREAM_RECEIPT"
```

条件分支只替换对应的标准命令：

```bash
#U-1：有冲突时逐项解决并 git add，再替换 merge-seal；fork/upstream 的 index blob 必须匹配冲突阶段
python3 -m tools.upstream_merge identity-seal --input "$CONFLICT_DRAFT" --output "$CONFLICT_INPUT"
python3 -m tools.upstream_merge merge-seal --repository "$REPO" --plan "$PLAN" --conflict-decisions "$CONFLICT_INPUT"

#U-2：有额外源码变化时，封存 SourceChangeInput 并替换 source-seal
python3 -m tools.upstream_merge identity-seal --input "$SOURCE_CHANGE_DRAFT" --output "$SOURCE_CHANGE_INPUT"
python3 -m tools.upstream_merge source-seal --repository "$REPO" --plan "$PLAN" --source-changes "$SOURCE_CHANGE_INPUT"

#U-2：有 delta 时只 carry-forward 零差异项；重建其余 Inventory，四份齐备后替换 surface-seal
python3 -m tools.upstream_merge identity-seal --input "$SURFACE_DRAFT" --output "$SURFACE_INPUT"
python3 -m tools.upstream_merge surface-seal --repository "$REPO" --plan "$PLAN" --decisions "$SURFACE_INPUT"
```

### 5.2.4 失败恢复与最终状态

- 门禁失败时保留原 attempt，修复后使用新 attempt id；普通阶段只能写入尚不存在的输出。若不可变制品已使
  当前计划无法继续，保留证据并从 `U-0` 新建 changeset。
- 需要复证当前环境时，以全新 attempt id 运行下列命令；它从 source commit 创建临时 detached worktree，
  重跑 12 类门禁并清理，不替换 `U-5` 原收据：

```bash
python3 -m tools.upstream_merge replay \
  --repository "$REPO" --plan "$PLAN" --receipt "$UPSTREAM_RECEIPT" \
  --rerun-gates post-final-001
```

- `current_guard_state` 只允许 `source_absent`、`out_of_scope_passthrough`、`legacy_observe`、
  `canary_enforce`、`enforced` 并仅陈述当前事实；目标计划不得覆盖。已知 Persona OAuth 发送源封存前至少为 observation-only
  `legacy_observe`，未知 OAuth 出站始终为 `denied`。
- merge commit 或 candidate tree 不等于交付或生产更新；`accepted_not_activated` 也只表示候选可交付。
  `U-6` 只更新本地受审源码基线；只有另行取得部署权限并完成 §5.6，才能声明生产已更新。

## 5.3 官方客户端升级

### 5.3.1 冻结目标、基线和升级终点

本节是官方客户端升级的总操作入口。主文档定义执行顺序和统一状态；Codex、Claude 手册第四部分只提供
对应轨道的参数、证据和专用门禁，不能建立另一套升级流程。一次 Campaign 只绑定一个客户端、一个目标
stable 和一个用途；Codex 与 Claude 同时换版时必须建立两个 Campaign，不得混用画像或收据。

开始前先冻结：

1. 目标官方身份。Codex 绑定版本、源码、`Cargo.lock`／依赖、二进制与摘要；Claude 绑定 npm identity、
   integrity、tgz／二进制／bundle、平台、entrypoint 和隐私模式。
2. 当前基线。绑定 production active／rollback Release、Profile、final wire、两个 Inventory、Runtime
   Selector、运行镜像、环境、最近有效激活／回滚收据和恢复点。
3. 升级用途。`validation_only` 只交付固定候选；`production_replacement` 还必须具备部署授权并执行 §5.6。
4. 受管坐标。Campaign、candidate、attempt、证据根和输出均使用新的持久绝对路径，冻结工具与 Schema
   摘要；历史制品只读，不得覆盖。
5. 执行预算。冻结本次升级的阶段墙钟预算、重试上限、证据复用决定、资源水位和
   `UpgradeTimingLedger` 输出路径；预算从首次 DOC-PRE/P0 动作开始连续计算，等待、诊断、审批和重试
   均不得从墙钟时间中扣除。

`VC-0／P0` 必须先证明当前工具能从输入身份读取目标版本、active／rollback 和规则全集。任何旧目标版本
常量、缺少必填 policy／Schema／生成器、不可独立重放的收据或缺失 mutation 门禁都计为工具阻断；先以
独立工具变更修复并重跑 P0，阻断清零前不得创建正式 Campaign。

```text
VC-0～VC-6 公共状态全部完成
∧ 所选客户端轨道第四部分的专用制品与门禁全部完成
∧ 当前工具阻断数 = 0
∧ UpgradeTimingLedger 完整且不存在未关闭的超时停线
⇒ ready_for_operator_release

ready_for_operator_release
∧ 已取得部署授权
∧ Framework §5.6 与客户端生产轨道全部完成
∧ production Catalog／selector promotion receipt 可独立重放
∧ post-promotion gate receipt 可独立重放
∧ DeploymentFact／activation receipt 可独立重放
⇒ production_active_upgraded
```

### 5.3.2 按 VC-0～VC-6 推进

| 阶段 | 操作 | 必须输出 | 退出条件 |
|---|---|---|---|
| `VC-0` 预检与基线 | 冻结 §5.3.1 全部输入，运行客户端 P0 | `UpgradePlan + CampaignIdentity`、工具就绪收据、时间与资源基线 | 当前生产可复算，工具阻断为 0；退出后才可创建正式 Campaign |
| `VC-1` 目标取证 | 按 P0 决定只读导入并重放可信目标证据，或从目标官方产物重新发现并采集适用 P／R／J／M | 受管导入收据或新 EvidencePackage、DiscoveryInventory、SinkInventory | 目标身份完整，复用链可重放或新发现无预设截断，未知项保持未分类 |
| `VC-2` 语义清零 | 对每个发现作唯一终态处置，建立原子断言、迁移决策和 RequiredRules 映射 | DiscoveryDispositionLedger、AtomicAssertionLedger、RequiredRules manifest | 未决、遗漏、重复和无主断言均为 0，不从发现数量机械生成 SPEC |
| `VC-3` 画像与批准 | target-first 生成 ProfileSchema、ReleaseArtifact、SupportEnvelope、Persona 派生和验收矩阵 | ApprovalFact、ReleaseArtifact、目标 ingress／egress Inventory | 画像、规则、断言、范围和证据摘要一致；范围外 fail-close，production selector 未改变 |
| `VC-4` 实现与候选 | 在 Persona 方言内实现差异，冻结源码、测试、构建、镜像和独立 Release 引用 | ValidationCandidate、candidate inventory | 同源身份闭合，无厂商事实泄入共享层，跨 Persona 隔离通过 |
| `VC-5` 成对验收 | 批准入口执行逐规则 PAIR，拒绝入口执行凭据前负例，并覆盖状态重建、故障和回退 | AcceptanceFact、逐规则结果、回退／恢复收据 | final wire 对拍通过，无未决 strict 项；失败 attempt 只读保留 |
| `VC-6` 交付或激活 | 无生产权限时封存固定候选；有权限时继续 §5.6 的晋升、门禁、canary、切换、回滚和恢复 | `ready_for_operator_release`；生产激活另有 promotion、post-promotion gate 和 activation 收据 | 交付边界明确；生产激活还须满足 §4.2 三个 Envelope 及 §5.6 收据链 |

目标证据是新画像的唯一设计权威；未收敛发现和证据不足能力保持 `denied`，只有 IngressPolicy 批准的
正向入口进入 PAIR。`VC-4` 若暴露共享合同缺口，作废当前 candidate，先执行 §5.4。目标 stable、阶段
制品、工具或身份变化时按 §3.3 新建 Campaign／candidate／attempt，不得借用旧事实跨阶段继续。

### 5.3.3 选择并执行客户端轨道

先选择且只选择一条轨道。下表给出从 P0 到交付的实际顺序；完整参数合同和证据格式分别以
[`CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](CODEX_CLI_CLIENT_EMULATION_GUIDE.md) 第四部分和
[`CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md) 第四部分为准。

| 阶段 | Codex CLI 轨道 | Claude Code 轨道 |
|---|---|---|
| `VC-0` | 按 Codex 手册复算 Active／Previous，冻结模式、用途并清零工具阻断 | 按 Claude 手册冻结 generation policy、active／rollback 并清零工具阻断 |
| `VC-1` | `reuse` 时只读导入并重放官方阶段；`recapture` 时重新采集并封存 | 按复用决定重放可信证据，或重新完成目标原生取证与封存 |
| `VC-2` | 新换版或分类纠正时重新分类并审核；运行时身份后继可承接已批准分类 | 清零 DiscoveryDispositionLedger 和语义候选，形成 RequiredRules、原子断言及迁移决定 |
| `VC-3` | 生成并批准五份清单，只追加候选 Catalog，不切 Active | 由目标 policy 生成画像并签发 ApprovalFact／ReleaseArtifact，不改 production selector |
| `VC-4` | 将批准画像纳入同源 candidate，构建固定镜像并封存候选及批准第三方入口证据 | 实现批准差异并用目标 policy 完成固定 candidate PAIR |
| `VC-5` | 离线比较、逐规则断言、外部门禁重放和 Acceptance finalizer | 重放官方事实，完成正向 PAIR、凭据前负例、回退／恢复和 Acceptance finalizer |
| `VC-6` | `validation_only` 停在 `accepted_not_activated`；生产替换继续客户端生产轨道和 §5.6 | 按用途交付固定候选，生产替换另获授权并继续客户端生产轨道和 §5.6 |

具体命令、机器、账号、模型、当前版本和工具阻断状态只在客户端手册记录。两条轨道的 P0 都必须拒绝
历史版本常量、隐式用途和不可重放收据；`ready` 或 AcceptanceFact 均不得冒充生产激活事实。

### 5.3.4 失败恢复

- P0 失败：不创建 Campaign；独立修复工具、Schema 或环境后从 `VC-0` 重来。
- 官方产物、目标 stable、画像、规则、断言、用途或证据语义变化：新建 Campaign；源码、测试、构建、
  镜像或 Release 引用变化：新建 candidate；身份不变的临时采集失败：保留旧 attempt 并新建 attempt。
- 前序官方证据仍可信时，后继必须以受管收据只读承接：仅修正 candidate 运行时身份时从 `VC-4` 继续；
  分类事实纠正时从 `VC-2` 重做分类与批准。缺少必要官方事实、官方身份变化或证据语义失真才返回 `VC-1`
  重新取证。
- 任一阶段失败或摘要漂移时保留旧制品和收据，按状态机回到最近合法身份；不得覆盖、跳过门禁或手工清除
  阻断。升级完成状态只按 §5.3.1 的两个公式判定。
- 同一根因连续失败两次即按 §5.3.5 停线。独立工具修复、离线回归和新的干净 P0 全部通过前，禁止
  第三次 live attempt，也不得通过新 Campaign 或 candidate 绕过阻断。

### 5.3.5 时间预算、停线与资源收敛

官方客户端升级的正常墙钟目标为 4～6 小时；该目标用于识别流程是否已漂移为工具研发、环境排障或无界
重跑，不降低证据标准。`production_replacement` 从首次 DOC-PRE/P0 到
`production_active_upgraded` 的默认预算如下，客户端手册只能收紧：

| 阶段 | 墙钟停线预算 | 到期动作 |
|---|---:|---|
| `VC-0` | 45 分钟 | 不创建 Formal；冻结阻断并把工具／环境修复拆为独立变更集 |
| `VC-1～VC-3` | 75 分钟 | 停止新增官方请求；复核归档复用决定、目标身份和未决分类 |
| `VC-4` | 90 分钟 | 恢复环境并保留 attempt；区分实现变化、工具变化和临时失败 |
| `VC-5` | 75 分钟 | 只保留可重放门禁；禁止用新 candidate 或 Campaign 重跑相同失败 |
| `VC-6` | 75 分钟 | 保持或恢复旧 Active；不得在未闭合回滚时继续扩大流量 |

任一阶段预算或总计 6 小时先到即按 §5.1.1 `stop_the_line`。本流程的计时台账固定命名为
`UpgradeTimingLedger`；P0 还须冻结唯一 `reuse／recapture` 决定。身份、摘要、原始字节、安全和场景覆盖
均可信时只读复用；仅缺少必要事实、官方身份变化或证据语义失真允许重抓，candidate、账号或模型变化
不构成理由。

## 5.4 修改共享合同或共享运行时

正常官方客户端升级不得进入本节。客户端专属的 Header、Body、IdentityMode、fallback、状态机、重试和
transport 事实，应由该 Persona 的 Plan、Schema 与 DialectCompiler 表达，并按 §5.3 升级。只有现有
Persona 方言无法承载，且缺口属于厂商无关的共享控制面时，才允许修改共享合同或运行时。

| 变化 | 操作入口 |
|---|---|
| 合并 Sub2API 上游，且共享合同语义不变 | §5.2 |
| 官方客户端画像、规则或方言变化 | §5.3 |
| 同版本实现修改，且共享合同语义不变 | §5.5.1 |
| `CompiledEnvelope`，或 Registry、Store、Selector、Executor、Token、Guard 的合同或语义变化 | 本节；若由上游合并触发，还必须同时满足 §5.2 |

新增 Persona 时，先以专属 Plan、Schema 和 DialectCompiler 表达其事实。只有证据证明某项机制在至少两个
Persona 间重复、稳定且属于共享控制面，才可进入本节；单客户端机制和仅为未来复用的设想不得上提。

共享合同或运行时的批准顺序固定为：

1. 只读冻结全部受影响 Persona 的 active／rollback Release、final wire、Runtime Selector、运行镜像、
   回退收据，以及引用旧合同的 validation candidate。
2. 证明共享变更的必要性，定义后继合同、影响分母和失败关闭行为；厂商 wire 事实不得进入共享合同。
3. 为全部受影响 Persona 的 active／rollback 建立逐字节零差异基线；主张共享抽象时，至少使用两个
   Persona 证明接口可表达性及 authority、issuer、状态和连接隔离。
4. 生成后继合同与测试，为全部受影响实现建立新 candidate，并完成 route／Sink、Release、Token、Guard、
   状态、连接和跨 Persona 负例；旧 candidate 不得借新合同继续验收。
5. 全部受影响 Persona 的验收与回退事实闭合后，才允许按 §5.6 发布；此前 production selector 保持不变，
   任何非预期 final-wire 差异都阻止发布。

`CompiledEnvelope` 仍只能包含 §2.3 的厂商无关事实。共享实现可以复用代码，但每个 Persona 的 authority、
Token issuer、invocation、状态命名空间和连接身份必须独立。

## 5.5 同版本实现修改、旧画像与兼容代码退休

### 5.5.1 同版本修改

仅当官方产物、目标规则、SupportEnvelope、画像、场景、断言和产出侧证据工具均未变化时，才允许在
原 Campaign 下建立新 candidate。新 candidate 必须重新绑定源码、测试、构建、镜像和用途，重跑受影响
规则及公共终态门禁；准备替换生产时继续 §5.6，不得复用旧 candidate 的交付、激活、回滚或恢复事实。

实现过程中一旦发现现有规则或 Schema 不能表达真实官方行为，立即停止普通 candidate 路径，建立
同版本后继 Campaign；不得修改旧 ApprovalFact 或用实现测试提升官方证据等级。
若冲突可由既有官方原始证据和源码充分判定，后继只读承接官方阶段并重新执行分类、画像和断言批准，
不得重复采集官方流量；只有既有原始证据缺少必要事实或官方身份／证据语义不再可信时，才重新取证。

### 5.5.2 退休旧运行画像或兼容代码

每次退休使用独立变更集，并按以下顺序执行：

1. 用 Catalog／selector 引用、类型扫描、调用图及两个 Inventory 证明全部生产与回滚消费者，区分
   `migrated_strict`、`retained_legacy`、`explicitly_retired` 与 `rerouted`。
2. 旧运行画像仅在新 Active 稳定、Previous／rollback 已冻结且均不再引用它后退出运行 Catalog；迁移真实
   消费者并让旧入口明确 fail-close，未知入口或出站保持 `denied`，不得先删除 Guard。
3. 验证全部受影响 Persona 的 active／rollback、HTTP／WS／fallback、辅助端点、状态恢复和跨 Persona
   负例，以空 wire 允许列表比较前后。
4. 只删除当前运行投影及无消费者的旧类型、字段、构造接线、finalizer、旁路和实现测试；历史 Release、
   原始证据、Approval／Acceptance、promotion／activation／rollback 收据及其重放夹具继续只读保留。
5. 生成不可覆盖的 RemovalReceipt 和机器退休收据并完成公共门禁；需要部署时继续 §5.6，不得以退休
   收据代替生产激活事实。

只要仍有 `retained_legacy`、未知消费者、未处置出站或回滚依赖，就不得签发 RemovalReceipt。服务 API
Key、其他 Persona、业务认证、平滑升级或已演练回滚的代码不能因名称相似而随官方 OAuth 兼容层删除。

## 5.6 候选交付、生产激活与回滚

<a id="638-fw-h生产迁移与遗留退休"></a>

该兼容锚点只供历史 bootstrap 收据解析，不属于当前流程。

每次执行必须先固定一个终点：

| 终点 | 必须完成 | 不得声明 |
|---|---|---|
| 候选交付 | 固定 candidate、隔离验收、应用回退／恢复、稳定观察及 `ready_for_operator_release` 收据 | production selector 已改变、DeploymentFact 已签发或生产已激活 |
| 生产激活 | 已取得部署授权，并完成下列六步及可重放的 promotion、post-promotion gate、DeploymentFact／activation 收据 | 在任一步骤或身份尚未闭合时声明生产升级完成 |

所有生产激活，无论 candidate 来自换版、上游更新、共享合同还是同版本实现变化，都必须执行：

1. 写操作前只读冻结生产镜像、compose／配置、selector、Release、画像、activation fact、数据和依赖；
2. 从已验收 candidate 生成并晋升 production Catalog，签发 promotion receipt；构建正式目标架构镜像，
   以 promotion 后源码重跑终态门禁并签发 post-promotion gate receipt；
3. 在隔离环境以默认 production active 运行独立 canary，不得通过强制 candidate／rollback mode 命中目标；
4. 只替换应用容器完成正式切换，不重建数据库、缓存、挂载、网络或其他依赖；
5. 切回冻结回退镜像验证真实入口和数据兼容，再恢复目标镜像并完成稳定观察；
6. 生成消费 Acceptance、promotion、post-promotion gate，并绑定 Campaign／candidate／Approval、源码、
   镜像、Release、三个 Envelope、canary、切换、回滚和恢复事实的不可覆盖 activation receipt。

客户端手册必须声明部署权限、执行终点以及对应的工具、环境和专用收据。运行镜像、selector、Release、
画像或收据任一不一致时，状态保持 `production_unverified` 并执行已冻结回退，不得在故障实例上补画像
或修改 selector。

## 5.7 失败路由速查

本表只确定返回入口；失败制品和收据仍须只读保留，具体恢复动作以目标章节为准。

| 失败类型 | 返回入口 |
|---|---|
| 墙钟预算、同根因重试、连续性收据或资源水位不满足 | §5.1.1；停线并保留最后合法身份 |
| Sub2API 上游合并计划、隔离工作树、冲突处置、影响分母或门禁不完整 | §5.2.4 |
| 官方客户端取证、规则收敛、画像、candidate 身份或换版门禁失败 | §5.3.4；身份变化同时按 §3.3 建立新事实 |
| 共享合同出现厂商事实、隔离破坏或非预期 final-wire 差异 | §5.4 |
| 同版本实现不再满足原 Campaign 条件，或兼容代码退休无法闭合 | §5.5 |
| PAIR、状态重建、负例或候选专用门禁失败 | §4.1 及产生该 candidate 的流程；不得进入生产激活 |
| 生产镜像、Catalog、Release、selector、Envelope、canary、切换、回滚或恢复失败 | §5.6；保持 `production_unverified` 并使用冻结回退点 |
