# 官方 OAuth 客户端出站仿真共享框架

> **适用范围**：Sub2APIPlus 对官方 OAuth 客户端出站形态进行画像化实现时的共同控制框架
> **当前规范已建档客户端**：Codex CLI、Claude Code
> **当前运行时状态**：Codex CLI 已有生产 strict 链；Claude Code 仍是遗留局部仿真，尚未登记为
> 多 Persona 运行时的可激活 Persona
> **扩展模型**：一份共享框架加若干客户端专属手册；Grok、Gemini 等只能在独立取证和审核后登记
> **架构原则**：共享内核只解释厂商无关的终态控制事实；协议、身份、状态和 wire 事实由 Persona
> 自有方言负责。候选共享接口必须经 Codex 零差异和至少一个不可部署的第二 Persona 纵向样例证明后才能冻结
> **文档边界**：本文不定义任何客户端的具体 Header、Body、TLS、端点、版本或状态事实

---

# 第一部分 唯一目标与等价标准

## 1.1 唯一目标

无论入站来自官方客户端还是第三方标准 API 客户端，只要最终使用某个已登记的官方 OAuth persona
出站，Sub2APIPlus 都必须先把入站转换为规范化语义请求，再由该 persona 的生产 active
ReleaseBundle 生成最终 wire。最终出站形态应与对应版本的官方客户端在相同平台、入口、配置、账号、
模型和触发条件下直接使用同类 OAuth 时一致。

入站客户端名称、版本、User-Agent、Header 顺序和自报身份均不拥有生产画像选择权，也不得直接透传
为官方身份。账号、Group、计费和调度仍由原业务系统管理；画像只拥有官方上游可见的最终出站形态。

## 1.2 “一致”的可验收定义

“一致”不是把一次抓包中的随机字节永久写死，而是按可观测层次分别比较：

| 类型 | 一致性要求 |
|---|---|
| 静态 wire | method、URL、Header 名称／大小写／顺序／值、Body 字段／类型／顺序、压缩和帧形态一致 |
| transport | 在证据覆盖的平台与条件下，TLS、ALPN、HTTP／WS、连接复用和重试行为一致 |
| 动态事实 | 生成来源、格式、相等关系、作用域、复用和生命周期一致，不比较某次随机字面值 |
| 条件行为 | 相同受信条件下产生相同结果；条件不成立时按官方行为省略或采用对应分支 |
| 跨请求状态 | 会话、turn、agent、retry 等状态的建立、消费、回送和失效关系一致 |

每条结论都必须声明适用版本、平台、入口、认证、模型、配置和证据边界。没有证据覆盖的产品、端点、
平台或后继版本不得使用“完全一致”等全称表述。

## 1.3 统一链路

```text
官方客户端入口／第三方标准 API 入口
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

`IngressProtocolAdapter` 按入站协议实现，而不是按 Persona 重复实现；它只把消息、工具、模型意图、
流式请求和明确条件转换为厂商无关的 `CanonicalRequest`。`TranslationReport` 必须记录映射是否无损、
丢失了什么语义以及拒绝原因。Persona 自有 `PersonaPlanner` 再把规范化语义和受信事实转换为独立的
`TypedEgressPlan`。这两层分离后，增加一种入站协议和增加一个 Persona 不会形成协议数乘 Persona 数
的适配器矩阵。

官方入口也必须经过对应的入站适配器和同一终态链。只有 `TranslationReport=lossless`、语义等价且
受信条件相同的两类入站，才要求命中同一 ReleaseArtifact 并通过同一组 wire 断言。无法无损表达的
官方语义必须拒绝，或在批准的非 strict 用途中显式记录降级；不得静默补造客户端状态。

`TranslationReport=lossless` 只证明用户提交的消息角色、顺序、system、工具、模型意图和流式语义
无损进入 `CanonicalRequest`，不表示最终请求只能包含入站字段。官方客户端固有且已有证据的 system
blocks、metadata、设备或会话事实，可以由 PersonaPlanner／Identity Authority 受管派生；每项派生都
必须记录目标字段、权威来源、证据或规则、派生原因、作用域、生命周期和冲突处置，并纳入
Identity／Dialect attestation。此类 Persona 固有内容不得回写或冒充用户语义，也不得由
IngressProtocolAdapter 临时猜测。

角色重排、删除用户 system，或把 system 改写为 user message，均属于语义变化，不能标记为
`lossless`。strict 路径必须拒绝这类请求；确有历史兼容需要时，只能进入明确批准的 compatibility
模式，记录降级内容和用途，并与 strict Release、验收结果和生产流量范围隔离。本文所称“不得静默
补造客户端状态”，禁止的是无权威来源或无派生记录的伪造，不禁止上述 evidence-derived Persona 事实。

---

# 第二部分 最小可扩展的共享运行架构

## 2.1 核心身份

一个 persona 至少由以下身份共同确定，不能只用 provider 名称或上游 host 代替：

```text
OfficialClientPersona =
  provider + official_product + auth_family + upstream_route_family
```

例如，官方 CLI、官方桌面应用和 API Key mimic 即使访问同一厂商，也可能是不同 persona。只有官方
客户端确实使用可取证的官方 OAuth 路径时，才属于本文当前目标；其他认证方式必须另立范围，不能
伪装成 OAuth persona。

## 2.2 责任分层

| 层 | 共同责任 | 禁止事项 |
|---|---|---|
| IngressProtocolAdapter | 按入站协议生成 `CanonicalRequest + TranslationReport` | 选择 Persona／生产版本，或保留入站 wire 身份 |
| 规范化语义 | 表达消息、工具、模型意图、流式语义和有类型条件 | 演变成厂商 wire 联合 Schema，或容纳 Header／TLS／版本常量 |
| Persona Registry | 依据受信认证类型、官方产品和 route 解析 Persona | 仅凭 host、UA 或客户端自报值分类 |
| PersonaPlanner + Identity Authority | 生成 Persona 自有 Plan，以及受信账号、会话、agent、平台和请求事实 | 从第三方同名 Header 复制官方身份，或选择生产版本 |
| ReleaseArtifact Store | 保存内容寻址、不可变的 Persona 画像和 ReleaseBundle | 保存 active／rollback／candidate 等可变部署角色 |
| Runtime Selector | 独立解析 Persona 的 production active 和 rollback 引用 | 让自动发现、入站版本或 validation candidate 直接激活 |
| 客户端方言 | 以 Persona 专属 Plan、IdentityFacts 和画像解释 URL、Header、Body、条件与状态语义 | 复用另一客户端的 Plan、Schema、Compiler 或 wire 事实 |
| DialectCompiler | 把专属 Plan 编译成最小 `CompiledEnvelope` | 要求共享内核理解厂商 Header／Body／IdentityMode／fallback 语义 |
| Executor | 以每 Persona 独立 authority 实例校验 envelope、管理 attempt、签发 FinalizationToken 并选择 adapter | 签发后允许普通业务代码改写 wire，或在不同 Persona 间复用 issuer／状态 |
| transport adapter | 执行画像声明的 TLS、HTTP、WS、压缩和连接行为 | 执行 Token 未授权的语义变化 |
| Runtime Guard | 校验 route、persona、Sink、Release、画像和最终摘要 | 对未知 route 或终态篡改静默放行 |

这里的“共享 Executor”指共享实现，不指一个跨 Persona 复用 authority、issuer、invocation 状态或
连接状态的全局实例。每个 Persona 必须拥有独立 Executor authority 和 Token issuer；共享 adapter
也必须按明确 `TransportCapability` 登记，不能因此共享 Persona 状态。

共享内核消费的 `CompiledEnvelope` 只允许包含以下厂商无关事实：

| 事实 | 作用 |
|---|---|
| Persona 与 Release／Profile／Bundle digest | 绑定编译归属和不可变画像 |
| Sink、Route、Endpoint、method、protocol | 绑定最终物理出口和 Guard 闭集 |
| invocation／attempt 身份与 Body 可重放性 | 管理一次调用、重试预算和 single-use 能力 |
| `TransportCapability` 引用 | 选择受信 adapter，而不是由业务代码选择具体 client |
| Identity／Dialect attestation digest | 把 Persona 自有身份、Header、Body、状态策略整体纳入签名，但共享层不解释其内部字段 |
| Prepared request capability | 供 Executor 计算 final request digest 并签发 Token；普通业务代码不能拆解后重组 |

共享内核不得出现 `CodexIdentityMode`、`ClaudeBetaPolicy`、某厂商 Header／Body Policy、版本常量或
只有单一 Persona 使用的 fallback／状态字段。Header 集合、Body 方言、协议能力、状态机、重试语义
及 transport 参数均属于客户端画像或方言层；只有 attempt 上限、可重放性、终态归属和签名等确实
需要由控制内核执行的结果，才投影为上述最小事实。

`CodexEgressPlan`、`ClaudeEgressPlan` 及其 IdentityFacts、ProfileSchema、DialectCompiler 保持独立。
方言代码应位于 Persona 自有模块；闭集由 composition root、Registry、构造能力和测试门禁建立，不得
仅依靠包私有接口迫使所有方言堆积在共享核心包。候选共享接口只有同时通过 Codex 零差异和不可部署的
第二 Persona 纵向样例后才能冻结。现有抽象无法表达新机制时，先判断该机制是否应留在 Persona 内；
只有确属控制面不变量时才修改共享引擎，并回归全部受影响 Persona 的 active、rollback 和 candidate。

## 2.3 客户端接入契约

新增 Grok、Gemini 或其他官方客户端时，必须以独立变更集登记以下扩展点：

| 扩展点 | 必须提供的内容 |
|---|---|
| `PersonaDescriptor` | provider、官方产品、OAuth family、route／Sink 闭集和明确排除项 |
| `SupportEnvelope` | 平台、入口、隐私模式、模型、功能、端点及明确不支持条件；范围外请求必须 fail-close |
| `ProductionIngressInventory` | 当前生产的逻辑入口、全部物理别名、调用方和处置状态，以及入口到 adapter／route 的绑定 |
| `EgressDispositionInventory` | Persona 相关的推理、生命周期和辅助出站闭集，以及每项的运行时处置类别 |
| `PersonaPlanner` | 从已有 `CanonicalRequest + TranslationReport` 生成专属 Plan；不得复制每种入站协议的解析器 |
| `TypedEgressPlan` | 该 Persona 独立的规范化业务事实；不得扩张 Codex、Claude 或全局联合 Plan 来容纳新厂商 |
| `IdentityFacts` | 该 persona 独立的受信身份来源、类型、生命周期和冲突处置 |
| `ProfileSchema` | 版本、端点、Header、Body、状态、transport 和条件的可表达范围 |
| `DialectCompiler` | 从该 Persona 的 TypedEgressPlan、IdentityFacts 与画像生成 `CompiledEnvelope` 的规则 |
| `TransportCapability` | 可复用 adapter 与确需新增的窄 adapter；不得把客户端差异塞进全局分支 |
| `EvidencePackage` | 官方产物、源码／bundle、P／R／J／M、摘要、适用范围和规则台账 |
| `AcceptancePackage` | 聚合引用 AcceptanceFact、DeploymentFact，以及官方入口／每类第三方入口的成对断言、candidate 验收、发布和回滚收据 |

只有出现新的入站协议时才登记新的 `IngressProtocolAdapter`。新增 Persona 默认复用已有协议适配器；
其官方专属入口若含标准协议无法表达的语义，应新增窄入口适配器并输出同一个 CanonicalRequest 合同，
不能把解析逻辑藏进 Persona Compiler。

`ProductionIngressInventory` 中每个入口必须取以下且仅以下一种处置，不得用缩小
`SupportEnvelope` 隐藏现存生产入口：

| 入口处置 | 运行语义 |
|---|---|
| `migrated_strict` | 已进入目标 Persona strict 链，且属于其批准的 SupportEnvelope 和成对断言范围 |
| `retained_legacy` | 继续走已冻结遗留链；必须保持独立可观测、可回滚，且阻止遗留代码退休 |
| `explicitly_retired` | 已证明无生产消费者或完成有意下线；保留不可覆盖的退休证据 |
| `rerouted` | 已迁往明确的非本 Persona 产品路径；目标 route、认证和失败行为均已验证 |

Inventory 必须把 `current_disposition` 与 ApprovalFact 中的 `target_disposition` 分开记录。目标计划不能
覆盖当前事实；只有真实切换、退休或改路完成并签发 DeploymentFact 后，才能追加新的当前处置。

逻辑入口必须展开到所有物理路由、别名和内部调用点；未知别名、无处置入口或新增裸调用都使闭集门禁
失败。只有 `migrated_strict` 才计入 Persona 的 strict SupportEnvelope；其余三态不能伪装成 strict
验收成功。

`EgressDispositionInventory` 中每个已知出站必须取以下运行时处置之一：

| 出站处置 | 运行语义 |
|---|---|
| `persona_strict` | 属于官方客户端仿真责任；必须由画像、Compiler、Executor 和 Guard 管理，并纳入适用 SPEC／PAIR 与 SupportEnvelope |
| `non_persona_managed` | 不主张官方客户端 wire 等价，但必须登记 route／Sink、认证、endpoint、client、超时、重试、秘密和审计策略 |
| `denied` | 未批准或不应发出的路径；运行时 fail-close |

Inventory 还必须独立记录 `current_guard_state`：无发送源为 `source_absent`，未纳管历史事实为
`out_of_scope_passthrough`，已纳管路径按 `legacy_observe → canary_enforce → enforced` 记录。该字段只陈述
当前事实，不能由目标计划覆盖；FW-E 封存时，有发送源的已知 Persona OAuth 出站不得仍是
`out_of_scope_passthrough`，只能绑定 observation-only Sink 后记为 `legacy_observe`。

明确排除项不是 `out_of_scope_passthrough` 的同义词。Persona 相关 OAuth 出站即使不属于 strict wire
责任，也必须进入 `non_persona_managed` 或 `denied`；未知项默认 `denied`。只有与该 Persona 及其
OAuth 生命周期确实无关的其他产品流量，才可由更外层、另有闭集的产品策略处置。

完成上述登记只表示架构可承载该客户端，不表示仿真已经成立。客户端必须拥有独立手册和证据；Codex、
Claude、Grok、Gemini 之间不得交叉继承 wire 事实。

第三个及后续 persona 的接入顺序固定为：先完成独立取证并用专属 Plan、Schema 和 DialectCompiler
表达；再识别它与既有 persona 重复且稳定的机制；最后才提交共享层扩展方案。只在一个客户端出现的
机制留在该客户端内，不能仅因“未来可能复用”而上提。共享层发生变化时，必须同时回归所有受影响
persona；仅新增客户端画像和方言时，不得无故改写既有 Codex 或 Claude 的 final wire。

---

# 第三部分 版本画像与证据生命周期

## 3.1 不可变制品与正交状态

版本画像必须是内容寻址、不可变、可复算的数据。`persona + version + profile digest` 构成
`ReleaseArtifact` 的不可变坐标；ReleaseBundle 绑定画像、端点、transport、状态和 Persona 自有策略。
发现、证据、批准、验证和生产角色是正交事实，不能压缩成一个状态枚举：

| 维度 | 事实 |
|---|---|
| Discovery | 自动或人工发现的上游版本，仅供评估 |
| Evidence | 官方产物和证据绑定，以及 `verified／observed／blocked／regressed_evidence` 等充分度 |
| Approval | 画像是否经过批准，以及批准的 `SupportEnvelope` 和目标规则集合 |
| Validation | candidate ID、固定源码／镜像／画像和验收结果；只允许按不可变 Release 引用选择 |
| Runtime Selector | 每个 Persona 的 `production_active` 与 `production_rollback` 引用 |
| Deployment | canary、正式切换、回滚、恢复和 activation receipt 证明的实际运行事实 |

Validation candidate 不得借用或伪装成 production rollback。当前 Codex 工具在隔离候选 RuntimeCatalog
中使用 `previous` 槽位承载 candidate，只是待退休的兼容实现，不属于新的多 Persona 合同，也不得据此
改变生产 rollback 引用。新 Persona 首次登记时可以尚无 production rollback；但首次生产激活前必须
提供经过启动、数据兼容和回退验证的真实目标，可以是上一正式 Release 或冻结的遗留实现，不能复制
active 摘要伪造回滚对。

旧画像不得原位覆盖，证据、发布图、selector 变更和收据只追加；自动发现和入站自报版本均不得改变
production active。源码常量、测试通过或 Campaign 达到 `ready`，都不能单独证明生产激活。现有 Persona
Schema 能表达时只追加数据；出现新协议、新状态机制或 Schema 缺口时，优先修改该 Persona 方言，只有
最小 `CompiledEnvelope` 或共享终态控制确有缺口时才修改共享引擎并执行多 Persona 回归。

## 3.2 证据与变更身份

客户端规则至少声明版本、产物摘要、平台、入口、认证、模型、配置、网络条件、观测通道、正负例和
适用边界。只能从证据实际可见的层次得出结论，pcap、原始应用层字节、MITM 解析、源码或 bundle
控制流不能互相替代。

规则同样不得用一个枚举混合不同维度：

| 维度 | 允许值示例 |
|---|---|
| `evidence_level` | `verified`、`observed`、`blocked`、`regressed_evidence` |
| `rule_lifecycle` | `candidate`、`active`、`superseded` |
| `compatibility_class` | `request_egress`、`response_compat`、`not_applicable` |
| `migration_decision` | `inherit`、`change`、`add`、`delete`、`condition_change` |

只有 `evidence_level=verified` 表示规则、条件、运行证据与复算链闭环；其他维度不能提升证据等级。
旧 JSON 台账在迁移期可以保留单字段兼容投影，但新的 Persona Schema 和 Campaign 必须保存上述正交事实。

`mapped_validation` 是发现项到 `SemanticRuleCandidate` 的处置关系，不是规则、迁移决策或新的证据
等级，也不表示目标 wire 已验证。FW-E 必须分开保存三层数据：`DiscoveryInventory` 无截断保留每个
原始发现，`SemanticRuleCandidate` 用 `source_ids` 把同一语义的多个发现归并为待验证工作项，
`RuleLedger` 只保存已经原子化、可执行并可逐项断言的 `SPEC-*`。一个发现跨越多个语义时可以引用多个
候选，但每个引用必须双向闭合。

语义候选保留真实的 `observed／blocked`，并固定 `rule_ledger_membership=denied` 与
`production_eligibility=denied`；不得用发现 ID 或 `CAND-*` 自动生成 `SPEC-*`。只有后续语义审查把
候选收敛成可执行规则时，才能显式新建或修改 SPEC，再独立登记迁移决策、场景和断言。
`mapped_validation` 可以消除发现分类账中的 `unclassified`，使 EvidencePackage 在
`evidence_recorded` 封存当前事实，但不得进入 production SupportEnvelope、不得签发
production-replacement ApprovalFact，也不得生成生产画像。

| 单元 | 身份变化边界 |
|---|---|
| Campaign | 官方目标版本、产物、平台、入口、默认条件或产出 EvidencePackage 的工具身份变化 |
| ApprovalFact | SupportEnvelope、目标规则、迁移决策、画像、场景、断言或批准用途变化 |
| candidate | ApprovalFact 引用、Sub2APIPlus 源码、测试树、构建、镜像或候选用途变化 |
| attempt | 上述身份不变，只因临时网络、额度或运行失败重试 |

四者均只写追加。新 ApprovalFact 不覆盖旧批准，新 attempt 不覆盖旧失败；candidate 验收通过不代表
生产已经部署。

## 3.3 高频版本闭环

1. 冻结目标官方产物、依赖、运行时和平台身份；
2. 比较每条规则的语义锚点、sink 绑定和运行时依赖；
3. 独立记录 `inherit／change／add／delete／condition_change` 迁移决策，以及
   `verified／observed／blocked／regressed_evidence` 证据等级；
4. 始终重跑最小 P／R／J／M 哨兵场景；
5. 对受影响规则、动态条件、远程 gate、TLS／运行时变化和负例重新采集；
6. 签发绑定 SupportEnvelope、目标规则、画像、场景和断言的不可变 ApprovalFact；
7. 生成 candidate ReleaseArtifact 后，用不可变引用在隔离验证目录选择，不改写 production selector；
8. 在 `SupportEnvelope` 内的所有规定入口执行同一组逐规则断言。

字符串相同、窗口邻近或 minify 标识符相同都不足以证明规则未变。只有工具能证明语义片段、依赖和
sink 关系时，静态锚点才可支持继承；能力不足时使用目标版本重新观察的保守路径。

---

# 第四部分 验收、发布与回滚

## 4.1 成对验收与 strict 门槛

每个目标规则建立 `PAIR-<SPEC-ID>`。官方入口和每类第三方标准 API 入口提交语义等价请求，并在同一
candidate、画像和条件下验证最终 wire 收敛。动态字段比较来源、格式、关系和生命周期，不比较一次
抓包的随机字面值。

每个 candidate 必须先冻结 `SupportEnvelope`。目标规则全集是该范围内所有 request-egress 规则，
而不是把尚未支持的平台、入口、隐私模式或功能静默算作成功。范围外条件必须在 PersonaPlanner 或
Compiler fail-close；只有 validation-only 才能显式记录非无损降级。

`SupportEnvelope` 的批准还必须绑定同一时点的 `ProductionIngressInventory`。每个
`migrated_strict` 入口都必须位于该 Envelope 内，并按官方入口或对应第三方协议类别完成逐规则断言；
每个 `retained_legacy`、`explicitly_retired` 或 `rerouted` 入口都必须分别证明遗留隔离、退休或目标
路由事实。只要仍有 `retained_legacy`，candidate 可以在较小范围灰度，但不得据此删除旧 finalizer、
旁路或画像构造器，也不得生成表示迁移完成的 RemovalReceipt。

| 用途 | 允许终态 |
|---|---|
| `validation_only` | 可保留已记录的 `blocked`／`regressed_evidence`，止于诊断或 provisional |
| `production_replacement` | SupportEnvelope 内全部目标规则达到批准等级，不存在阻断 strict wire 的未决项；范围外请求已证明 fail-close |

第三方入口按协议类别登记；无法无损映射官方语义时必须拒绝或明确记录降级。任何对齐责任规则仍为
`blocked`，或已验证规则降为 `regressed_evidence` 时，candidate 不得成为 strict active；验收通过
也不会自动提升官方规则的证据等级。

## 4.2 生产与回滚

生产启用必须绑定同一 candidate 的验收、画像晋升、正式镜像、终态门禁、独立 canary、正式切换、
旧版回滚和目标恢复。只替换应用容器，不重建数据库、缓存和依赖服务。运行容器 digest、active／
rollback、画像摘要和 activation fact 不一致时，状态为 `production_unverified`。

Runtime Catalog 应按 Persona 独立构造和原子发布。candidate 或未启用 Persona 的画像无效时，不得影响
其他 Persona 的 production active；已启用 Persona 的 active 无法验证时，该 Persona 的 route 必须
fail-close，且不得自动借用另一 Persona 或 rollback 画像继续发送。部署可以选择让整个进程停止，或让
故障 Persona 单独不可用，但必须在部署策略中显式固定，并证明不会跨 Persona 级联或静默降级。

每次生产激活或扩大灰度前，必须分别冻结：

| 范围 | 含义 |
|---|---|
| `ActiveSupportEnvelope` | production active Release 经 ApprovalFact 批准并已通过 strict 验收的能力范围 |
| `RollbackOperationalEnvelope` | rollback 镜像、selector、配置、依赖和路由已完成真实回退演练的可运行范围 |
| `DeploymentTrafficEnvelope` | 本次准备切入该 active deployment 的实际生产流量范围 |

必须满足以下不变量：

```text
DeploymentTrafficEnvelope
⊆ ActiveSupportEnvelope
∩ RollbackOperationalEnvelope
```

不满足时禁止激活或扩大流量，只能收窄 DeploymentTrafficEnvelope、补足 active／rollback 证明，或让
范围外入口继续保持 `retained_legacy`／`rerouted`。`RollbackOperationalEnvelope` 只证明操作回退后
请求不会因范围缺口突然断线，不会提升 rollback 的官方 wire 证据或 strict SupportEnvelope；冻结遗留
部署可以作为 operational rollback，但其输出仍是 diagnostic-only。

---

# 第五部分 文档职责与客户端登记

当前人类可读权威文档为：

| 文档 | 责任 |
|---|---|
| 本文 | 稳定的共享运行架构、终态合同、证据维度和 Persona 接入不变量 |
| [`CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](CODEX_CLI_CLIENT_EMULATION_GUIDE.md) | Codex CLI 官方事实、画像、实现和版本流程 |
| [`CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md) | Claude Code 官方事实、画像、环境、实现和版本流程 |

未来新增官方客户端时采用“一客户端一专属手册”，并在本节登记。Grok、Gemini 目前只是架构可扩展性
示例，不表示已经确认其官方客户端、OAuth 路径、规则或仿真实现。机器证据、JSON 台账、Schema 和
收据可以分目录保存，但不得形成第二套人类可读规范。

---

# 第六部分 首轮迁移实施方案

本部分把现有 Codex 专用 strict 执行链收窄为多 Persona 共享控制内核，并在不把 Claude 遗留输出
当作规格的前提下重建 Claude Code firstParty OAuth 仿真。DMIT 用于隔离候选验证和回滚演练；Vircs
用于生产切换与稳定观察。

## 6.1 当前状态与本轮目标

Codex active／rollback 及 final wire 是零差异基线；Claude 2.1.220 只作为差分证据和候选 rollback，
遗留 Claude 输出只作诊断。先完成 Codex-only 发布，再开始 Claude 工作；Grok、Gemini 不在本轮范围。

Claude 的实现权威是 FW-E 冻结的最新 stable。ProfileSchema、目标 Snapshot 和实现必须先由该版本证据
驱动；2.1.220 只能随后由同一 Schema／Compiler 表达为基线 fixture 或受范围约束的 rollback，不得反向
决定目标设计。candidate 冻结后，即使官方 stable 再变化也不得中途换版。

生产事实以镜像 digest、selector、activation fact 和不可覆盖收据为准；缺失或不一致时统一标记
`production_unverified`。

## 6.2 现有代码处置

| 处置 | 必须满足 |
|---|---|
| 保留 | Persona 身份、Route／Sink 闭集、摘要绑定、每 Persona 独立 authority／issuer／状态、Token 和 Guard |
| 收窄 | `CompiledEnvelope` 只含厂商无关事实；移除 Codex 专用 Policy／fallback；candidate 与 active／rollback selector 分离 |
| 隔离 | 各 Persona 的 Plan、IdentityFacts、Schema、Compiler 和 transport 参数留在自有模块；Codex facade 保持 final wire 零差异 |
| 收据 | 历史 transition／fixture／receipt 不覆盖；源码、测试、画像或镜像变化均建立后继事实 |

## 6.3 实施顺序

`FW-A～FW-H` 是唯一阶段编号，并按以下顺序执行：

```text
FW-A 基线 → FW-B 暂定合同 → FW-C Codex-only 发布 → FW-D 通用工具链
→ FW-E 最新 stable 取证 → FW-F target-first 画像与最终合同
→ FW-G 完整实现与验收 → FW-H 生产迁移与遗留退休
```

每阶段是独立变更集。输入必须引用前序不可变制品；输出只写追加；退出门禁未满足时不得开始下一阶段。

### 6.3.1 FW-A：只读冻结基线

- **前置输入**：当前仓库、Codex 官方证据与 production active／rollback、Vircs 运行态，以及 Claude
  2.1.220 证据目录和遗留发送面；本阶段只需只读权限。
- **必做动作**：冻结 Codex 源码、测试、画像、selector、镜像、final wire 和生产恢复点；冻结 Claude
  2.1.220 的官方产物、P／R／J／M、规则台账，以及遗留入口、出站和运行输出。
- **输出制品**：两类基线的摘要清单、Codex active／rollback fixture、生产恢复资料、Claude
  2.1.220 evidence baseline，以及标为 `diagnostic-only` 的遗留观察结果。
- **退出门禁**：Codex 两个 Release 和 final wire 可复算；Claude 证据来源、规则计数和缺口可复算；
  只读前后生产镜像、selector、配置和依赖一致。
- **禁止／回退**：不得改代码、画像、selector 或生产；不得把 Claude 遗留输出当 expected wire。任一
  基线不完整时留在 FW-A 补齐。

### 6.3.2 FW-B：抽取暂定共享合同

- **前置输入**：FW-A 的 Codex 基线、现有 Codex strict 执行链及其本地门禁。
- **必做动作**：只抽取 Persona 身份／Registry、ReleaseArtifact Store／Runtime Selector 的只读运行时
  合同、Executor authority、FinalizationToken、Guard、Route／Sink 闭集和既有 Codex 收据校验接口；
  保留 Codex facade，并把 Codex 专用 Plan、Identity、Policy、Schema 和 Compiler 留在 Codex 方言。
- **输出制品**：标为 `Codex-compatible` 的暂定共享合同、对应实现，以及覆盖 active／rollback 的
  Codex 零差异回归。
- **退出门禁**：共享 `CompiledEnvelope` 不含厂商 Header／Body／版本／fallback 字段；Codex 全部门禁
  通过，active／rollback 的画像选择和 final wire 与 FW-A 逐字节零差异。
- **禁止／回退**：不得登记 Claude Persona、Route、Sink、画像或 production binding；不得为未来厂商
  预造协议抽象。非零差异在 FW-B 修正，不得进入 FW-C。

### 6.3.3 FW-C：Codex-only 验证与生产发布

- **前置输入**：FW-B 的固定源码与测试树、FW-A 的 Codex fixture 和 Vircs 恢复点。
- **必做动作**：构建固定 digest 的 `linux/amd64` Codex-only candidate；在 DMIT 验证 active／rollback、
  Compiler、Route／Sink、Token、Guard、final wire，并完成 canary、回滚和目标恢复；随后只读冻结
  Vircs，再仅替换应用容器，核对 health、日志、镜像、selector、activation fact 和 Guard，完成稳定观察。
- **输出制品**：DMIT 验收／回滚／恢复收据，Vircs 发布收据、运行事实和稳定观察结果，以及可恢复的
  旧生产镜像、配置和依赖引用。
- **退出门禁**：Codex final wire 零差异；DMIT 回滚与恢复成功；Vircs 运行态与发布收据一致且观察期
  通过；本轮镜像中没有新增 Claude Persona、画像或 strict 注册。
- **禁止／回退**：不得在 Vircs 做探索性测试或为演练切换生产；不得夹带 Claude 实现。发布异常按
  FW-A 恢复点回滚；FW-C 后共享运行时代码再变化，必须返回 FW-B 并重走 FW-C。

### 6.3.4 FW-D：建设通用受管工具链

- **前置输入**：FW-C 已稳定的 Codex 生产收据和暂定共享合同；测试只使用 Codex 与合成数据。
- **必做动作**：围绕 FW-B 的只读运行时合同实现 Campaign 编排、正交事实账本、Evidence／Profile
  两段式批准、Snapshot／ReleaseArtifact 的写入／封存／复算、独立 ValidationCandidate 引用、
  `PAIR-<SPEC-ID>`、两个 Inventory、SupportEnvelope、ActiveSupportEnvelope、
  RollbackOperationalEnvelope、DeploymentTrafficEnvelope，以及晋升、激活和不可覆盖收据的生成与复算。
- **输出制品**：可执行工具、Schema／Store、状态转换门禁、正负例测试和收据重放能力；此阶段不产生
  Claude Profile、Snapshot 或 Runtime 注册。
- **退出门禁**：工具能机器阻断越权转换、原位覆盖、摘要漂移、candidate 借用 production selector、
  入口别名遗漏、未处置出站、Envelope 范围缺口和收据不匹配；追加事实可独立重放。
- **禁止／回退**：不得查询并预选 Claude 目标 stable，不得接入 Claude 生产 route／Sink。若工具实现
  改动共享运行时代码，先返回 FW-B／FW-C 重新证明 Codex，再继续 FW-D。

### 6.3.5 FW-E：冻结最新 stable、取证并封存发送面

- **前置输入**：FW-D 工具链、FW-A 的 2.1.220 evidence baseline／遗留观察面，以及 FW-C 的生产收据。
- **必做动作**：第一步查询官方 `stable`，立即冻结准确版本、发行物来源／integrity／摘要、平台、
  entrypoint、隐私模式、默认条件和工具身份；先从目标官方产物独立枚举全部网络 sink、请求构造、
  条件 gate 和跨请求状态，再取目标原生发现、历史规则、旧源码／线索账本及前一批准 stable 的候选
  并集，逐项执行语义差分、P／R／J／M 哨兵和 `inherit／change／add／delete／condition_change` 分类，
  并独立记录 evidence level。首次 Claude Campaign 以 2.1.220 为历史比较基线；后继换版以最近批准
  stable 为增量基线，但不得因此丢弃永久历史候选。最后完成逻辑入口、物理别名、消费者和全部 OAuth
  出站的 source-to-sink 盘点，只在冻结遗留路径启用 `legacy_observe`。
- **闭集发现要求**：目标规则全集不得由旧规则台账枚举后迁移得到，也不得截断 sink、只扫描已知
  host／path 或只运行固定字面量探针。每个目标 sink 必须有稳定身份、可复算来源、可达性边界和唯一
  disposition。原始发现先进入无截断 `DiscoveryInventory`，再按语义归并为候选；只有形成可执行语义、
  适用条件和断言的项才进入 `RuleLedger`。目标独有机制经语义审查后必须能显式形成 `add`，不得按
  “一个发现一条规则”自动生成。仍不知道下一步如何处置的项保持 `unclassified` 并阻止闭集；已经取得
  稳定身份和证据绑定、但尚缺目标语义或运行证明的项使用 `mapped_validation` 进入语义候选，保留真实
  `observed／blocked` 并禁止加入规则台账和生产。静态分析只能把精确调用存在性记为 `observed`，不能
  据此证明触发条件、流量类别或 wire；strict 命题仍须由目标版本运行证据闭环。
- **行为层边界**：是否产生某类流量不作为独立的一致性对比维度。`essential traffic` 只界定后续
  strict wire／PAIR 的场景范围；场景被调用时仍须按 §1.2 核对实际请求的 wire、transport、动态事实、
  条件分支和跨请求状态。官方可配置关闭的 telemetry 与 nonessential traffic 只记录配置、可达性和
  观测事实，其缺失属于合法状态，不得计为一致性差异或“不像官方客户端”的依据。Claude 的
  `DISABLE_TELEMETRY` 与 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 是该判据的官方实现依据。
- **输出制品**：目标 Campaign 身份、不可变 EvidencePackage、目标 sink inventory、
  `DiscoveryInventory`、`SemanticRuleCandidate`、只含 SPEC 的规则迁移台账、跨来源矩阵及三层间的双向
  来源绑定，以及两个 Inventory 的当前事实；拟议 `target_disposition`／出站处置只作为未批准提案，
  不覆盖当前事实。
- **退出门禁**：官方产物和目标版本可复算；每条现有或新增 SPEC 都有迁移决策、证据等级和适用边界；
  历史候选和目标原生发现均有唯一处置，目标 sink inventory 无截断、重复或未分类项，运行观测没有
  出现 inventory 外 host／path／sink；所有发现均在 DiscoveryInventory，所有语义候选均禁止加入
  RuleLedger／生产且与发现双向绑定，RuleLedger 不含 `CAND-*` 或发现 ID；两个 Inventory 覆盖全部已知
  别名／调用方／出站并能报告未知项；Store 只追加
  `discovery_recorded／evidence_recorded`，没有 Evidence／Profile 批准；观察过程未改变生产流量。
- **禁止／回退**：本阶段不得定义目标 ProfileSchema、Snapshot、Persona 实现或 production strict
  binding；只允许为已冻结遗留调用点追加 `unclassified + legacy_observe` 的 observation-only Sink，且不得
  主张 Persona 或 wire 等价。不得用 2.1.220 或遗留输出补足目标 stable 证据；证据不足时缩小候选范围
  或归入禁止生产的语义候选；不得为了闭合发现清单自动生成规则。

### 6.3.6 FW-F：target-first 画像、样例与最终合同

- **前置输入**：FW-E 的 EvidencePackage／两个 Inventory、FW-D 工具链，以及 FW-C 对应当前共享合同的
  Codex 生产收据。
- **必做动作**：先由最新 stable 证据定义 Claude PersonaDescriptor、PersonaPlanner、ClaudeEgressPlan、
  IdentityFacts、ProfileSchema 和 DialectCompiler，登记 IngressProtocolAdapter 的复用／新增结论及
  TransportCapability，生成目标 Snapshot／ReleaseArtifact 与不可部署纵向样例；再用同一 Schema／
  Compiler 生成 2.1.220 baseline／rollback fixture。通过样例后才冻结最终共享合同，并在 ApprovalFact
  中批准 Persona 派生、compatibility 边界、SupportEnvelope、两个 Inventory 的 `target_disposition`
  及三个 Envelope 计划。
- **输出制品**：目标 stable 和 2.1.220 两个不可变 ReleaseArtifact／fixture、target-first 样例、最终
  多 Persona 合同，以及绑定规则、画像、场景、断言、Inventory 和范围的目标 stable ApprovalFact。
  只有计划把 2.1.220 用作 strict rollback 时，才为其自身规则和窄 SupportEnvelope 另签独立 ApprovalFact；
  否则它只保持 baseline／conformance fixture 身份。
- **退出门禁**：目标样例、跨 Persona／跨 Release 负例和 Codex 零差异通过；Schema 能表达两个版本且
  不以 2.1.220 限制目标设计；Codex 生产收据对应最终合同；所有 production selector 保持不变。
- **禁止／回退**：不得先构造 2.1.220 画像，不得把样例注册到 production Runtime Catalog。Persona
  自有缺口留在 Claude 方言；共享控制合同确有缺口时作废本阶段批准，返回 FW-B 并重走 FW-C、FW-F。

### 6.3.7 FW-G：完整实现与隔离验收

- **前置输入**：FW-F 的目标 stable ApprovalFact、可选的 2.1.220 rollback ApprovalFact、最终合同、
  两个 ReleaseArtifact／fixture 和 FW-D 工具链。
- **必做动作**：完整实现最新 stable 的 Header、Body、状态、transport、受管 Persona system／identity
  派生及有损 compatibility 隔离；所有入口经过 §1.3 统一链路，Claude 使用独立 Executor authority、
  Token issuer 和 invocation 状态；`persona_strict` 全部进入 Compiler／Executor／Guard，
  `non_persona_managed` 进入独立受管策略，未知 OAuth 出站 `denied`。封存 ValidationCandidate，并在
  DMIT 对全部 strict 入口执行 PAIR、范围外拒绝、managed 出站和 rollback／恢复验收。
- **输出制品**：固定源码／测试／镜像／Release 引用的 ValidationCandidate、逐规则 PAIR 结果、
  AcceptanceFact，以及 rollback 演练和 Codex 回归结果。
- **退出门禁**：SupportEnvelope 内目标规则唯一全覆盖，官方入口和每类 lossless 第三方入口均通过；
  范围外 fail-close、三种出站处置、统一链路、Persona 派生、独立 authority／issuer／状态、跨 Persona
  隔离、compatibility 隔离和 rollback 均通过；candidate 达到 `production-replacement ready`，且 Codex
  final wire 零差异。
- **禁止／回退**：不得修改 production selector 或删除遗留链；`observed／blocked／regressed_evidence`
  只要仍位于已批准 SupportEnvelope，就必须留在 strict 分母并阻止 production-replacement ready；只有
  新 ApprovalFact 真实收窄范围且范围外 fail-close 后才能移出。证据不足止于 validation-only；共享合同
  缺口按 FW-F 回退，任何批准内容或构建变化均建立新的 ApprovalFact 或 ValidationCandidate，不复用
  AcceptanceFact。

### 6.3.8 FW-H：生产迁移与遗留退休

- **前置输入**：FW-G 的 production-replacement AcceptanceFact、不可变 candidate／Release、FW-F 的
  两个 Inventory／三个 Envelope 计划，以及 FW-A 的生产恢复点和已演练 rollback 目标。
- **必做动作**：只读冻结生产后离线晋升 Release，重跑终态门禁并构建固定正式镜像；canary 前根据
  ApprovalFact、AcceptanceFact 和真实 rollback 演练冻结实际 `ActiveSupportEnvelope`、
  `RollbackOperationalEnvelope`、`DeploymentTrafficEnvelope` 并验证 §4.2 不变量；再执行隔离 canary，
  仅让 `DeploymentTrafficEnvelope` 内流量切入 active，完成正式切换、真实 rollback、目标恢复和稳定
  观察。签发 DeploymentFact 后，才依据该事实追加实际 `current_disposition`；只有闭集完成后，才以
  独立退休变更删除旧 finalizer、版本常量、画像构造器和旁路。
- **输出制品**：晋升／镜像／canary／切换／回滚／恢复收据，达到 `restored_active` 的 DeploymentFact、
  更新后的两个 Inventory、activation fact，以及聚合 AcceptanceFact、DeploymentFact 和全部验收／发布／
  回滚收据的 AcceptancePackage；满足退休门禁时另行签发 RemovalReceipt。
- **退出门禁**：运行镜像、production active／rollback、画像摘要、三个 Envelope、Inventory 和收据一致；
  Runtime Catalog 按 Persona 原子生效，Claude active 无法验证时其 route fail-close，不影响 Codex，也不
  发生跨 Persona fallback；完整
  `DeploymentTrafficEnvelope` 回滚与恢复成功并通过稳定观察；无 `retained_legacy`、未知入口或未处置
  出站后，遗留退休验证通过。
- **禁止／回退**：不得重建数据库／缓存／依赖服务，不得提前删除遗留能力或覆盖历史事实。范围不变量
  或生产一致性失败时禁止激活／扩流，标记 `production_unverified` 并恢复冻结目标。

2.1.220 只有在其独立 ApprovalFact 和真实演练共同覆盖 `DeploymentTrafficEnvelope` 时，才能作为 strict
rollback；否则使用 FW-A 冻结的遗留部署承担 operational rollback，其 wire 仍为 diagnostic-only。

## 6.4 失败与回退规则

以下规则优先于阶段内的正常推进：

| 触发条件 | 必须动作 |
|---|---|
| Codex final wire 非零差异，或共享运行时代码变化 | 返回 FW-B，建立后继合同并重新完成 FW-C |
| FW-D 不能机器阻断越权、范围缺口或收据不匹配 | 停在 FW-D，不得启动 stable Campaign |
| FW-E 存在未分类／被截断的目标 sink、未处置历史发现，或语义候选与发现未双向闭合 | 停在 FW-E，补目标原生发现和运行证据，或把证据不足项登记为禁止进入 RuleLedger／生产的 `mapped_validation` 语义候选；不得按发现数量生成 SPEC，也不得以遗留输出补证。候选可随 EvidencePackage 封存，但不能据此进入 production replacement |
| FW-F／FW-G 发现共享合同缺口 | 作废相关 ApprovalFact／candidate，返回 FW-B；重走 FW-C，并重验两个 fixture |
| 官方产物、批准内容、源码、测试、画像或镜像变化 | 按 §3.2 建立新 Campaign、ApprovalFact 或 candidate；旧验收和收据不得复用 |
| §4.2 不变量或生产一致性失败 | 禁止激活／扩流，标记 `production_unverified`，恢复冻结目标 |
| 仍有 `retained_legacy`、未知入口或未处置出站 | 不得退休遗留链；未知 OAuth 出站保持 `denied` |
