# 官方 OAuth 客户端出站仿真共享框架

> **适用范围**：Sub2APIPlus 对官方 OAuth 客户端出站行为的仿真，当前覆盖 Codex CLI 和 Claude Code。
> **本文职责**：定义共享架构、增量升级、候选验收、生产激活、失败恢复和审计规则。
> **客户端手册**：具体版本事实、场景、账号、命令和当前进度分别记录在
> [`CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](CODEX_CLI_CLIENT_EMULATION_GUIDE.md) 与
> [`CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md)。

本文只保留可长期执行的规范，不保存某次事故的补丁步骤。历史 Campaign、收据和故障报告是只读证据，
不得反向扩展本文，也不得形成第二套升级流程。

---

# 第一部分 目标与边界

## 1.1 仿真目标

当业务系统最终选择官方 OAuth 账号出站时，最终 wire 由目标 Persona 的生产画像统一生成。入站适配层只
负责协议和请求语义转换；Key、Group、账号路由、调度和计费仍由业务系统管理。

| Persona | 正向入站 | 最终出站 | 拒绝边界 |
|---|---|---|---|
| Codex CLI | 官方 Codex CLI，以及能够无损转换的已批准第三方入口 | 使用 OpenAI OAuth 时，由 Codex production active 画像定型 | 无损转换失败、未登记协议或范围外语义 fail-close |
| Claude Code | 已登记的 Claude 官方客户端 | 使用 Anthropic firstParty OAuth 时，由 Claude production active 画像定型 | 第三方客户端和未登记形态在读取 OAuth 凭据前拒绝 |

入站名称、版本、User-Agent、Header 或自报身份均不能选择生产画像，也不能原样透传成官方客户端身份。

## 1.2 最终 wire 等价

“等价”指在相同平台、入口、配置、账号、模型和条件下，下列可观测行为与官方目标版本一致：

| 维度 | 要求 |
|---|---|
| 静态 wire | method、URL、Header 名称／大小写／顺序／值、Body 字段／类型／顺序、压缩及帧形态一致 |
| transport | TLS、ALPN、HTTP／WebSocket、连接复用和重试行为一致 |
| 动态字段 | 来源、格式、相等关系、作用域、复用和生命周期一致，不比较一次性随机值 |
| 条件行为 | 相同受信条件进入相同分支，条件不成立时按官方行为省略或输出 |
| 跨请求状态 | 会话、turn、agent、retry 等状态的建立、消费、回送和失效一致 |

合法关闭的遥测和非必要流量不进入 strict 分母，但配置必须冻结；实际触发的 essential 请求仍须验收。
每条结论必须声明版本、平台、入口、认证、模型、配置和证据边界，未覆盖范围不得宣称完全一致。

## 1.3 统一链路

```text
IngressPolicy
→ IngressProtocolAdapter
→ CanonicalRequest + TranslationReport
→ 受信账号路由 + Persona Registry
→ PersonaPlanner + Identity Authority
→ production active ReleaseBundle
→ Persona DialectCompiler
→ CompiledEnvelope
→ Persona Executor + transport adapter
→ Runtime Guard
→ 官方 OAuth 上游
```

所有权固定如下：

1. Adapter 只做语义转换，不选择账号、Persona、生产版本或最终 wire。
2. Planner 只从规范化语义和受信事实生成 Persona 专属计划。
3. ReleaseBundle、Compiler、Executor 和 Guard 共同拥有最终出站，后续业务代码不得改写。

---

# 第二部分 共享运行架构

## 2.1 Persona 与分层

```text
OfficialClientPersona = provider + official_product + auth_family + upstream_route_family
```

同一厂商、host 或 provider 不代表同一 Persona。官方 CLI、桌面应用、API Key mimic 和其他产品必须分别
建模，只有可取证的官方 OAuth 路径属于本文范围。

| 层 | 责任 | 禁止事项 |
|---|---|---|
| 准入与适配 | 决定入口是否获准，输出规范化请求 | 选择生产画像或保留入站 wire 身份 |
| Persona 规划 | 生成计划、身份和跨请求状态 | 从不可信 Header 复制身份或跨 Persona 复用状态 |
| Release 控制 | 保存不可变画像，解析 active／rollback | 原位覆盖 Release 或让 candidate 自动激活 |
| 方言编译 | 生成 Persona 专属最终请求 | 把厂商字段塞进共享内核 |
| 执行与传输 | 管理 attempt、Token、连接和重试 | Token 签发后改写 wire 或跨 Persona 复用 authority |
| Guard | 校验 route、Sink、Release、画像和请求摘要 | 对未知路径或身份冲突静默放行 |

## 2.2 语义、状态与共享内核

`TranslationReport=lossless` 必须证明消息角色、顺序、system、工具、模型意图和流式语义无损。角色重排、
删除用户 system 或将 system 改为 user 均属于有损转换，strict 路径必须拒绝。

Persona 固有且有证据的 system blocks、metadata、设备或会话事实可以受管派生，但必须记录来源、规则、
作用域、生命周期和冲突处置，不得冒充用户输入。

跨请求状态必须保存在 Persona／Release 私有持久命名空间，使用 CAS、有限租约和 TTL。存储不可用、状态
损坏或冲突耗尽时 fail-close；进程缓存不能成为生产权威。

共享 `CompiledEnvelope` 只允许包含：

- Persona、Release、Profile、Bundle 和 attestation 摘要；
- Sink、Route、Endpoint、method、protocol 和 transport capability；
- invocation、attempt、重试预算及 Body 可重放性；
- prepared request capability、最终请求摘要和 single-use token 所需事实。

共享内核不得出现厂商 Header／Body Policy、版本常量或单一 Persona 的状态字段。共享的是实现，不是
authority、issuer、连接或运行状态。

## 2.3 Release 与 Guard

Release 必须内容寻址、不可变且可复算：

```text
ReleaseArtifact = persona + version + profile_digest
```

Runtime Selector 只保存已验证的 `production_active` 和 `production_rollback` 引用。自动发现、入站版本、
测试通过或 Campaign 状态均不能修改 selector。

Guard 必须校验 method、route、Persona、Sink、binding、Release、Profile、adapter、Token 和最终请求摘要。
状态只允许 `legacy_observe → canary_enforce → enforced` 单调推进；未知 route、无效 binding、跨 Persona 身份
和终态篡改必须 fail-close。

---

# 第三部分 规则、证据与身份

## 3.1 规则迁移是升级的唯一工作分母

每次官方客户端换版必须先生成完整 `RuleMigrationManifest`。旧版每条规则和新版新增规则必须恰好取得一种
决策：

| 决策 | 含义 | 默认动作 |
|---|---|---|
| `inherit` | 语义、条件和适用范围不变 | 复用旧实现与验收，不改代码、不重跑 |
| `change` | 规则语义发生变化 | 只修改该规则及其直接依赖 |
| `condition_change` | 条件、正反分支或适用范围变化 | 只补条件证据并修改对应分支 |
| `add` | 新增规则 | 新增实现与定向验收 |
| `delete` | 删除规则 | 删除运行投影并验证无遗留消费者 |

未分类、重复、无来源或证据不足的规则一律阻断。发现记录数量、文件数量和历史证据体积不得替代规则分母，
也不得机械生成新规则。

执行集合固定为：

```text
affected_rules = change ∪ condition_change ∪ add ∪ delete
affected_items = affected_rules 的代码、场景、测试和门禁下游闭集
```

`inherit` 不进入执行集合。只有直接依赖摘要或安全结论变化时，某个继承项才可由明确依赖边加入闭集；不得
因版本号、文档摘要、全局工具摘要或目录变化将全部规则判为失效。

目标画像必须从当前 `production_active` 画像派生，不得脱离基线重新生成：

```text
target_profile = immutable_copy(production_active_profile)
target_profile.version = target_version
target_profile = apply_rule_patches(target_profile, affected_rules)
```

派生时必须遵守：

1. 为目标画像分配新的版本、内容摘要和 Release 身份，禁止覆盖基线画像。
2. `inherit` 规则对应字段按规范化表示逐字继承，同时保留来源规则和验收收据。
3. 只允许修改目标版本身份字段，以及 `affected_rules` 显式映射的画像字段。
4. 每条允许变化的画像路径必须绑定唯一规则和补丁前后值；无法映射的差异在 candidate 创建前失败关闭。
5. 机器门禁必须验证：

```text
profile_diff_paths
⊆ version_identity_paths ∪ rule_field_paths[affected_rules]
```

该门禁只比较基线与目标画像的小型规范化清单，不扫描或重放历史原始证据。

## 3.2 证据要求

规则至少绑定版本、产物摘要、平台、入口、认证、模型、配置、网络、观测通道、样本分母、条件对照和适用
边界。pcap、应用层字节、MITM、源码和 bundle 控制流只能证明各自可见的事实，不能互相替代。

证据链为：

```text
DiscoveryInventory
→ SemanticRuleCandidate
→ AtomicAssertionLedger
→ RequiredRules
→ ApprovalFact
```

条件变化必须有正反样本；无条件规则必须有多个适用样本和零违规分母。合法零流量只能作为 supporting fact，
不能生成 RequiredRule 或缩小 SupportEnvelope。

`inherit` 必须由目标源码／产物语义和依赖关系证明；仅字符串相同或未观察到差异不够。证明成立后直接复用
旧规则收据，不再重跑其 candidate Job。

## 3.3 身份边界

| 身份 | 何时变化 |
|---|---|
| Campaign | 官方目标版本、产物、平台、用途、规则迁移清单或数据面证据合同变化 |
| ApprovalFact | 目标规则、画像、断言、SupportEnvelope 或迁移决定变化 |
| candidate | 实现源码、测试、构建、镜像或 Release 引用变化 |
| attempt | 上述身份不变，仅因临时执行失败重试 |
| evaluator run | 只因评估器、监督器或报告工具变化 |

数据面事实与控制面工具身份必须解耦。评估器、状态查询、监督器、计时或报告工具变化，只生成新的
`evaluator run`，不得改变 Campaign、ApprovalFact、candidate 或已通过 Job 的身份。

收据必须使用版本化 envelope，并记录 producer 版本和逐项输入摘要。旧收据由对应版本的只读 reader 或
兼容适配器重放；禁止要求旧收据匹配当前工具的全局摘要，禁止用新算法改写旧结论。

所有事实只写追加。不得覆盖历史 Campaign、ApprovalFact、candidate、attempt、证据、selector 或收据。

---

# 第四部分 候选与生产

## 4.1 候选验收

`production_replacement` candidate 必须覆盖 SupportEnvelope 内全部 request-egress 规则，范围外由 Planner／
Compiler fail-close。每条受影响原子断言建立独立 `PAIR-*`；继承断言只重放旧收据和依赖摘要，不重新执行。

批准的正向入口使用语义等价请求比较最终 wire；未批准入口只进入凭据前拒绝的负例。状态规则还必须覆盖
重建 Runtime、并发 CAS、租约释放和存储不可用。

每个入口必须是 `migrated_strict`、`retained_legacy`、`explicitly_retired` 或 `rerouted`；每个出站必须是
`persona_strict`、`non_persona_managed` 或 `denied`。未知项阻断验收。

以下条件全部满足才生成 AcceptanceFact：

- 所有受影响规则及其直接依赖通过；
- 所有继承规则的来源收据和依赖摘要可重放；
- 正向入口、拒绝入口、状态和回退门禁通过；
- `blocked`、`regressed_evidence` 和未决 strict 项为零；
- candidate 源码、镜像、Release 与测试身份一致。

## 4.2 激活与回滚

生产切流必须满足：

```text
DeploymentTrafficEnvelope
⊆ ActiveSupportEnvelope
∩ RollbackOperationalEnvelope
```

candidate 通过不等于已经上线。生产激活还需绑定 AcceptanceFact、Release、正式镜像、selector、canary、
回滚和恢复结果。任一不一致均为 `production_unverified`，保持或恢复旧 Active。

---

# 第五部分 维护流程

## 5.1 变更分类与执行原则

一次只处理一种主变更：

| 类型 | 入口 | 最小范围 |
|---|---|---|
| Sub2API 上游更新 | §5.2 | 上游 changeset 的影响闭集 |
| 官方客户端换版 | §5.3 | `affected_rules` 的依赖闭集 |
| 共享合同／运行时变化 | §5.4 | 全部直接受影响 Persona |
| 同版本实现变化 | §5.5.1 | 对应实现和门禁闭集 |
| 旧画像／兼容代码退休 | §5.5.2 | 已证明无消费者的运行投影 |
| 纯文档澄清 | 直接修订 | 不使任何运行结果失效 |

官方客户端换版不得夹带上游合并、框架重构或无关清理。若执行中发现升级工具缺陷，立即停止 Campaign，
将工具修复拆成独立变更集；修复只通过离线夹具验证，不得在正式 Campaign 上递增补丁试错。

### 5.1.1 公共执行约束

每个变更集必须在首个动作前冻结目标、范围、全局预算、同根因重试上限、资源水位和复用计划。读取、构建、
清理和网络访问均由显式 manifest 限界；历史证据只读，未知输入失败关闭。

同一根因最多执行两次。第一次失败后只能修复已定位的最小组件并运行离线回归；第二次仍失败即停线，不得
换 Campaign、candidate、attempt 或工具包名称继续试错。

### 5.1.2 依赖图与单一恢复算法

每个可执行项必须声明直接输入、环境身份和逐文件直接依赖，形成有向无环图。结果身份至少绑定项目 ID、
输入摘要、环境摘要和直接依赖摘要；具体字段与编码格式由客户端指南定义。全局工具树摘要只用于审计，
不能代替逐项失效计算。

组件至少区分产出、评估、控制、场景、运行时、环境／网络和门禁。变化文件必须映射到明确组件和下游项；
未知或未登记文件在执行前失败关闭，不得退化为无依据的全量重跑。

| 变化 | 允许动作 |
|---|---|
| 文档、报告、监督器、状态查询 | 不重跑规则或 Job |
| 单个 evaluator／gate | 只重跑该离线项及下游报告 |
| 单个 producer／scenario | 只重跑直接依赖它且尚无可信结果的 Job |
| runtime 代码 | 只重跑受影响规则及公共终态门禁 |
| network 语义 | 重做 P0；仅当结果安全性受影响时使直接依赖 Job 失效 |
| 官方原始请求证据 | 默认只读；确实缺失时须人工批准新取证 |

#### 恢复算法

所有客户端使用同一恢复语义：从最近合法 checkpoint 重新计算执行闭集，不通过改名、复制 Campaign 或
增加恢复分支绕过失败。具体步骤为：

1. 读取同一 Campaign 最近一个合法 checkpoint。
2. 校验 Campaign、ApprovalFact、candidate、环境和来源收据身份。
3. 计算 `execute = failed ∪ pending ∪ changed_dependencies 的下游闭集`。
4. 从执行集合移除已有可信通过结果且依赖未变化的项。
5. 封存恢复计划，明确 execute、reuse、原因、扫描和 live 请求预算。
6. 若执行集合为空，在取得资源或产生副作用前写入客户端定义的 no-op 证明并立即退出。
7. 否则按拓扑序只执行 execute；每项结束立即写 checkpoint。

no-op 分支不得启动运行时、执行环境探针、读取大证据或发送请求，并必须证明扫描量和 live 请求量均为零。

同一根因连续失败两次即停线。修复必须先在冻结的最小历史夹具上完整跑通从故障点到最终阶段，再允许恢复；
禁止把正式 Campaign 当作工具集成测试环境。历史恢复类型的导入、兼容读取和退休边界由客户端指南定义；
导入只能重放已封存摘要和来源，不复制或重发官方证据，也不得改变原身份。

### 5.1.3 控制面、环境面与数据面独立失效

| 身份面 | 内容 | 变化后的最大影响 |
|---|---|---|
| 控制面 | 状态机、租约、watchdog、计时、状态查询和只读收据解析 | 只重跑控制面离线门禁 |
| 环境面 | 平台、架构、主机／容器、路由、出口、MTU、磁盘和依赖事实 | 重做 P0，并按语义差异决定直接下游 |
| 数据面 | 规则实现、场景、producer、二进制、镜像和协议证据 | 只重跑对应规则及其下游闭集 |

三种身份必须分别计算摘要和失效集合。控制面修复不得改变数据面结果身份，环境 producer 的代码摘要不得
冒充环境语义变化；任何工具都不得把三者重新合成全局失效开关。

控制面变化只允许重跑受影响的离线控制门禁；环境变化先重新验证环境，再决定哪些数据面结果失效；数据面
变化只执行对应规则及其下游闭集。具体文件到身份面的映射由客户端指南登记，未登记文件按数据面变化
失败关闭，已封存的官方请求不得因控制面或评估器变化而重发。

#### 连续监督与时间账本

正式 Campaign 必须冻结总计划、阶段依赖、身份、预算和不可后移的 deadline；每个阶段或恢复批次只能根据
上一份已封存 checkpoint 生成不可变动作清单。总计划不得预填未来才产生的批准、Candidate、attempt、镜像
或收据身份，动作清单也不得在启动后补写。

客户端指南必须定义受管写入口、执行状态、失联／超时判定和 checkpoint 格式；存在长时间运行的 worker 时，
还必须定义心跳频率。受管控制面连续记录动作开始、结束、失败、等待原因、执行与复用范围、live 请求量和
扫描量；进程重启、attempt 或恢复不得重置时间账本和 deadline。任何时间区间无法分类、执行控制丢失或
需要人工补派时立即停线。

阶段完成后必须在 §5.3.2 冻结的交接窗口内，以封存输出生成下一批次。执行集合为空时直接生成 no-op 证明，
不得用空 Job、空转心跳或新 Campaign 延长运行；阶段退出条件未满足时也不得伪造终态。

#### 受管工具与文档部署

受管工具在运行时直接读取的仓库文档属于工具直接依赖。部署清单必须逐文件登记其规范相对路径和摘要，
并与工具树在同一可回滚事务中切换；首次纳管的缺失文件也必须能恢复为不存在。P0 必须从生产运行根执行
相关重放测试，禁止只在完整开发工作树通过后发布不完整的文档子集。具体清单、打包和部署命令由客户端
指南定义。

### 5.1.4 执行环境冻结与隔离

每个客户端指南必须明确取证、构建、候选验收和生产操作各自使用的平台、架构、主机／容器角色及权限边界，
并冻结网络、路由、出口、DNS、证书、MTU、运行时依赖和资源水位。具体机器、地址和参数只属于该客户端，
不得提升为其他客户端必须继承的共享默认值。

P0、attempt 和交付或部署前后都必须核对本次冻结的环境身份；不一致时在任何外部请求或写操作前停线。
禁止修改宿主网络、隧道、防火墙、容器地址、证书或协议字段来迁就测试。只有采集传输故障已由原始网络证据
定位且不改变官方画像时，才允许使用客户端指南预先批准的局部传输参数。

### 5.1.5 宿主目录与数据治理

每个执行环境必须登记唯一的项目根、数据根、暂存根和必要的只读兼容根；实际路径、权限、容器挂载和编排
方式由客户端指南定义，不能由当前目录、历史习惯或共享框架中的固定宿主路径隐式决定。

输出边界固定如下：

| 对象 | 允许位置 |
|---|---|
| 编排文件、镜像构建输入和受管配置 | 已登记项目根内、数据根外的只读或受管路径 |
| state、runtime、work、Campaign、control、audit 和 archive | 已登记数据根的对应子目录 |
| 短期测试夹具 | 已登记暂存根；使用系统临时目录必须有明确能力需求和清理边界 |
| 历史绝对路径兼容 | 已登记兼容根，仅允许只读重放和迁移验证 |

创建目录、打开输出文件和启动容器前，工具必须解析已有路径组件并验证最终位置仍位于允许根内；`..`、
符号链接、相对路径、未登记挂载源和大小写近似目录均不得绕过检查。声明配置还必须与实际运行时的挂载、
网络和镜像身份逐项核对，不能只审核配置文本。

P0 和每个 attempt 前后都必须在已登记宿主边界内执行有界污染检查，并与开始前 inventory 比较；不得无界
扫描整台主机。发现新的未登记对象立即停线。临时对象只能按冻结 manifest 精确清理；清理前复核引用、
占用、挂载和摘要，清理后记录对象、字节数、前后磁盘水位、排除项和复验结果。原始证据、合法 checkpoint
及仍被收据引用的对象禁止清理。

历史项目迁入标准根时，须先封存旧、新目录和编排配置摘要，再停写、迁移、逐项复验并仅重建受影响运行时。
需要让旧绝对路径继续可解析时，只能建立登记过的只读兼容映射，禁止复制出第二份可写事实或用符号链接
冒充证据根。目录变化属于运行坐标变化，按 §5.3.4 登记；兼容消费者清零并生成移除收据后才能撤销旧映射。
迁移闭合前不得创建目标版本 Formal Campaign。

## 5.2 合并 Sub2API 上游更新

上游更新与官方客户端换版必须分开：七个阶段的目的都保留，反复修一行源码不再重做整套流程，而是在
同一 Plan 追加 revision，并把可确定的重复执行交给工具。上游合并不得改变客户端目标版本，也不得冒充
生产部署；需要上线时继续 §5.6。三条总则：

1. 合并期间不得修改 §5.2.4 第 3 条定义的工具闭集，也不得新增或修改流程规则；流程与工具的修复在
   `plan-create` 之前提交到主仓库并重新封存基线。
2. 冻结摘要台账的 successor 只在最终 revision 由工具一次性生成（§5.2.4 第 2 条），中间 revision 禁止
   生成，也禁止逐套人工登记。
3. U-4 的出口只有 §5.2.3 决策表里的三条；无论走哪条，候选分支上的提交、受维护分支的快进、远端推送
   和生产部署都只能由受管工具按阶段执行，人工不得插手，CI 结果也不能代替任何一份收据。

### 5.2.1 升级前基线验收（权威）

基线验收回答“升级前这棵树的功能事实和证据事实分别是什么”。它在主仓库干净工作树上执行，收据写入
仓库之外的私有 evidence 目录，绑定当前 `HEAD`、tree 和 tool bundle 摘要。时机固定在上一次发版之后、
下一次合并之前的空闲期：

- 每次发版后 24 小时内在干净 `HEAD` 上执行 `baseline-seal` 并归档；合并当天只执行 `baseline-validate`。
- 基线暴露的历史证据漂移、扫描器分类缺口和工具缺陷，一律在 `plan-create` 之前修复并提交到主仓库，
  然后重新封存基线；这些修复不得在候选分支上完成。
- 合并当天 `baseline-validate` 失败时先回到上一条，不得带着失败的基线创建 Plan。

基线检查拆成两组：

| 组别 | 内容 | 失败处理 |
|---|---|---|
| 功能门禁 | `go build`、`go vet`、lint、普通业务测试、官方 egress 关键回归 | 任一失败都阻断；禁止登记为 known drift |
| 证据门禁 | frozen/transition/receipt 摘要、历史台账和可复算性检查 | 只有明确属于历史收据的摘要漂移，且有前后摘要、来源收据和原因，才可登记为 known drift |

`known_drift` 只表示“该条历史证据不能按当前摘要直接复算”，不表示功能通过。后续差异比较使用以下
闭集规则：候选功能失败始终阻断；候选证据失败只有在其唯一 `failure_id` 已被基线 `known_drift` 明确
覆盖时才记为继承漂移；其余失败按候选新增失败处理，必须先归因和修复。

先准备只读的基线输入草稿（不得手改收据），再依次执行 `baseline-seal` 与 `baseline-validate`；
两者的完整参数以 `--help` 为准，本节只规定它们的语义与失败处理。

`baseline-seal` 不可覆盖写入；工作树、提交、tree 或 tool bundle 变化时必须重新封存。正式请求必须使用
`official-egress-upstream-merge-request/v2`，在 `baselines.baseline_acceptance_path` 指向该收据；
`plan-create` 会再次确认收据与计划 fork HEAD/tree 完全一致。

### 5.2.2 计划外预检（非权威）

正式 `plan-create` 前先运行一次离线预检。推荐顺序是“基线验证 → 输入准备 → 预检 → U-0”。输入准备
清单：

- request 从 `tools/upstream_merge/request_template_v2.json` 渲染占位符生成，Active/Rollback 路径与目标
  版本从当前 release catalog 解析，不复制上一轮请求；生成后立即做 JSON 解析和 schema 校验。
- 计划目录只创建 `inputs/` 与 `evidence/`，权限 0700；worktree 目录由 `plan-create` 自行创建。
- 门禁必须是执行组模式（12 类逻辑门禁映射到 5 个物理执行组），不得沿用 `receipt_replay` 类型。
- 门禁命令显式绑定本地只读源码根（如 `CODEX_0_149_1_SOURCE_ROOT`），不依赖被 `.gitignore` 排除的路径
  在候选 worktree 中存在。
- 前端包管理器必须可用且版本与 CI 一致，候选 worktree 的 `frontend/` 先装好依赖；缺失时完整回归组会在
  前端步骤静默中断，其后的检查根本不执行，门禁"通过"没有意义。

`preflight` 的报告写到仓库之外。预检只在临时隔离 worktree 中试合并，依次执行 `egressscan -mode snapshot`、`go build ./...`、
`go vet ./...` 和官方 egress 目标包测试；不写入主仓库、不 fetch、不 push、不产生权威阶段制品，报告中
`non_authoritative` 必须为 `true`。报告的 `report` 对象固定包含五项，任何一项被跳过或失败都标为阻断；
因冲突而 blocked 时其余四项仍必须输出：

| 项 | 内容 |
|---|---|
| 冲突闭集 | 冲突文件数与路径、上游变化文件总数，写入 Plan |
| 模板有效性 | 与标准模板比对 schema 版本、门禁定义、执行组模式、Persona，以及 Active/Rollback 在当前 release catalog 中的绑定 |
| 闭集受扰清单 | 上游对 §5.2.4 第 3 条闭集文件的改动，U-1 前决定处置 |
| 冻结覆盖 | 上游改动命中的冻结台账路径数及注册表要求的额外动作 |
| 扫描器覆盖 | fork 与候选树发送点集合的差异；因冲突 blocked 时标记 deferred，由 U-2 `surface-scan` 承担 |

预检通过后仍必须重新执行 U-0，不能把预检报告当作 U-0 收据。

### 5.2.3 七阶段及增量执行合同

| 阶段 | 必要目的 | 执行合同 |
|---|---|---|
| U-0 | 冻结目标、计划、预算和证据目录 | 先通过 §5.2.1；`plan-create` 只创建一次权威 Plan，冻结 fork HEAD、上游 tag/commit、基线收据、工具闭集和受保护对象。 |
| U-1 | 解决冲突并形成可重放的双父 merge commit | `merge-start`／`merge-seal` 在隔离 worktree 中完成；冲突台账和双父关系不可省略。 |
| U-2 | 闭合 Codex／Claude 入口、出站发送面和 Inventory | 首轮 `source-seal` 生成 revision 001，源码修复后在同一 Plan 追加 revision，旧制品只读保留。 |
| U-3 | 按文件和调用边形成影响闭集 | `impact-generate` 与当前 revision 绑定；`impact-suggest` 只对已知低风险条目给出建议，`impact-seal` 对未决项 fail-close。 |
| U-4 | 证明候选树满足全部必要门禁 | 每个收据固定 12 类逻辑门禁、5 个物理执行组，`skipped_gate_count` 为 0。 |
| U-5 | 封存 candidate、Campaign 和回退处置 | `disposition-seal` 绑定验证收据、原业务回归和受影响 Persona 的后继动作。 |
| U-6 | 快进受维护分支并能独立重放 | U-4 通过后仅由 `apply` 执行 ff-only 快进，随后 `finalize`／`replay`；人工不得合并、推送或部署。 |

补充约束：

- U-1：上游对闭集外文件的改动按普通冲突处置；对闭集内文件的改动按预检既定决定恢复受保护版本并在
  冲突台账登记。已审核的冲突决策可在后继 Plan 机械重放。
- U-2：源码 revision 必须重新执行 `surface-scan`、Inventory 绑定和 `surface-seal`，只有发送面零差异时才
  允许 `inventory-carry-forward`；台账 revision 只有最后一个（§5.2.4 第 2 条）。
- U-3：同 diff 的文件复用本 Plan 或前序 Plan 已封存的决定。
- U-4：同一 `execution_group` 的相同命令只执行一次；复用以 revision 为界，源码一变全部执行组重跑，因为
  冻结测试与业务测试都读取候选树；六类客户端门禁收据由工具生成；`full-regression` 组通过
  `backend/Makefile` 的 `test-gate` 覆盖 CI 的默认、unit 与 integration 三组测试并固定 `-count=1`。
- U-6：`finalize` 与 `replay` 收据是发版前置条件；U-4 未通过时禁止执行。

每个 U-2/U-3 revision 进入 U-4 前先做只读复核，再执行门禁；失败后保留原 attempt，默认只重跑上一轮
失败的执行组：

`--only` 必须覆盖上一 attempt 的全部失败项，且会自动扩展为完整执行组。每个新 attempt 仍生成完整的
逻辑门禁结果与自动客户端收据，并记录执行与复用计数。

source、surface、impact 三条 revision 必须同轮推进：每次 `source-seal` 之后立即执行发送面扫描封存与
影响闭集生成封存，不得跳轮。工具按最新 SourceCandidate revision 编号且要求严格递增一轮，跳轮无法补跑，
只能回退候选提交重做。attempt 复用同样以 revision 为界：源码变化、或仅仅重新签名 ChangeDecision／
SurfaceDecision，都会让验收收据绑定漂移，必须全新 attempt。

#### U-4 出口与冻结台账修复模式

U-4 的结果只有三种出口：

| attempt 结果 | 出口 |
|---|---|
| 存在功能失败（编译、业务测试、lint、前端） | 在同一 Plan 追加源码 revision 修复，重跑受影响执行组 |
| 失败项全部是冻结摘要类且功能门禁已通过 | 由工具一次性生成台账 successor，作为台账 revision 追加后重跑 |
| 同一根因连续失败两次，或台账 revision 后仍不闭合 | 停线复盘，把缺项回流到注册表与本节 |

冻结摘要类指 transition／successor 冻结测试、受管工具树摘要和 `check-egress-spec-ci` 中的摘要门禁，
一律不得逐套人工登记。走第二条出口时，先把待生成收据的仓库相对路径写进注册表指定的门禁显式列表，
再执行 `freeze-successor-generate`：`--extra-worktree-path` 读取工作区当前状态，必须覆盖登记之后的
门禁文件，顺序颠倒会让收据摘要对不上，只能删掉重来。Plan 作废超过一次同样要停线复盘。

#### 受维护分支在合并期间被第三方推进

发版流水线的 VERSION 同步是最常见的来源。`apply` 要求受维护分支 HEAD 精确等于计划 fork HEAD，多一个
提交即拒绝；该提交本身通常还会让主干冻结门禁变红。处置顺序固定：

1. 停止当前 Plan 的 U-6，不要尝试快进，也不要回退已推送的主干；
2. 按 §5.2.1 先在主干为该提交补齐冻结后继收据并推送，再重新封存基线；
3. 新建 Plan，把上一 Plan 的冲突解决与本地修复整体重放到新 worktree；两次候选树应当只差台账部分，
   以此验证重放准确；
4. 冲突决策可沿用上一 Plan 的理由并追加重放说明，但处置类型必须按新 index 对象重新判定。

预防：开工前确认最近一次发版的 VERSION 同步已完成；U-4 通过后尽快执行 U-6，不要跨夜留置。

### 5.2.4 revision、工具闭集和长期台账

1. U-2/U-3 的 revision 文件只能追加，编号从 001 连续递增，`predecessor` 绑定上一轮文件；禁止覆盖旧
   JSON、用软链接冒充制品或跳号。Inventory revision 同样按 Persona/kind 连续追加。
2. 冻结摘要台账的 successor 与 `source-transition` 只在最终 revision 生成一次：全部源码修复、lint 与前端
   检查通过、候选树不再变化之后。收据不进入它描述的提交，也不引用本区间内变化的收据，
   `source-transition` 会拒绝把绑定本区间的收据记进节点，以此消除“移出、重算、加回”的自引用循环。

   `freeze-successor-generate` 按与 Go 门禁相同的规则从 `docs/egress/maintenance/*.json` 抽取已登记的摘要边，对区间内命中冻结
   覆盖的路径生成“已登记摘要 → 当前摘要”的边；前序摘要未登记即链断裂，fail-close。收据的自摘要采用
   Python 工作区门禁的算法，改到 Codex CLI 0.151 worktree successor 覆盖的路径时用 `--after` 指向源码
   提交、用 `--extra-worktree-path` 追加引用该收据的门禁文件，再把收据加进门禁的显式列表与之同提交。
   `docs/egress/maintenance/freeze-registry.json` 只登记通用图之外仍需额外动作的台账（worktree successor
   显式列表、ARM64 受管工具摘要常量、scanner-algorithm-successor 单跳文件、Campaign fact map），命中时
   写入 `required_manual_actions`；Campaign fact map 固定 `manual_required`，须老板确认。修改注册表不属于
   工具闭集变化，但必须重新 `identity-seal`。`source-transition` 节点的 Go 冻结测试仍按上游版本各写
   一份（两包），可从上一版本复制并只改常量。

   `source-transition` 生成节点、`source-transition-validate` 校验链尾；路径、状态与两端摘要一律由
   Git 复算，删除和重命名保留前后路径，历史节点不改写，修复只追加 successor。前序链不连续时不要强接
   `--predecessor-register`，本轮独立成节即可。
3. 受管 tool bundle 的权威定义是 `tools/upstream_merge/gitops.py` 的 `_tool_source_paths`，覆盖
   `tools/upstream_merge/` 的 Python 源、三个 schema、`tools/check_ledger_completeness.py`、`Makefile`
   与 `backend/cmd/egressscan/` 的 Go 源；文档不复制文件清单，以该函数与 Plan 中的 `tool_bundle` 为准。
   标准 request 模板不在闭集内，改它不构成闭集变化，但仍须按 §5.2.2 重新渲染并校验。上游改 Makefile
   与扫描器补分类这两类常见变化不靠拆分闭集回避，而是靠 §5.2.2 的闭集受扰清单提前处置、靠 §5.2.1 把
   分类缺口修在 `plan-create` 之前。闭集任一字节变化都会改变合并事实含义：进行中的 Plan 必须停线并
   新建 Plan；合并期间发现工具缺陷的唯一路径是停线、在主仓库修复并提交、重新封存基线与预检、新建 Plan。
4. 按上游 tag 逐个合并，不跨越 minor 版本，即使两个 tag 间隔不足两周也分别合并。有效工作时间的参考
   值：普通 tag 更新约 3～3.5 小时，跨 minor 或 60 个以上冲突文件约 8～9 小时，涉及官方 wire、Persona
   或共享控制面再加 2～4 小时；冲突人工解决与契约适配随上游规模线性增长，只能靠逐 tag 合并摊薄。
   时间账本显示工具本身几乎不占时间——除 `gates-run` 外的全部子命令合计不到 2 分钟，工期取决于冲突
   解决与被工具拒绝后的往返，因此门禁应稳定在两轮：首轮暴露冻结摘要漂移，台账 revision 后次轮全绿。
   预检报告的冲突文件数与变化文件数写入 Plan。废弃 Plan 的 worktree/evidence 只在完成留档和审计确认后
   清理，不得用清理动作替代收据。
5. 每个 `tools.upstream_merge` 子命令自动追加一行到 `timing-ledger.jsonl`：命令、参数、起止时间、
   耗时、结果与错误。默认落点是 Plan 目录，其次是输出或收据所在目录；推断路径落在 Git 工作树内时
   不写并提示，用 `--timing-ledger` 指定 Plan 目录即可；账本写入失败时成功的命令也按系统错误返回。
   账本无缺口是发版前置条件之一，不得在合并结束后补写。

## 5.3 官方客户端升级

本节是所有官方 OAuth 客户端换版共用的**阶段合同**，不是具体客户端的操作手册。它只规定阶段语义、
共同安全边界、身份恢复原则和时间控制；命令、脚本、Schema、字段、机器职责、目录、网络、部署参数及
客户端专用收据，以对应客户端指南为执行权威。

发生表述冲突时：阶段顺序和共同约束以本节为准，具体执行方法以客户端指南为准。若客户端现有工具无法
满足本节合同，必须先停线并独立修复工具或文档，不得在升级过程中自行改写阶段含义。

### 5.3.1 P0 冻结

VC-0 开始正式 Campaign 前，必须冻结并形成可重放记录：

1. **目标与基线**：目标官方产物身份，以及当前 active／rollback 身份和可用回退点；
2. **Campaign 身份**：升级用途、终点、账号与权限条件、证据根和受管工具版本；
3. **执行边界**：平台与环境角色、网络和目录边界、资源水位、总 deadline、阶段预算及重试上限；
4. **证据策略**：已有可信证据的复用范围，以及仍需补齐的事实。

P0 只做离线预检，不采集目标 live 证据、不创建 Candidate、不修改生产。具体冻结字段、预检命令和收据格式
由客户端指南定义。输入完整、工具与环境可用、预算和回退点有效且工具阻断为零，才允许进入 VC-1。

### 5.3.2 VC-0～VC-6

七个阶段依次回答“升级什么、目标行为是什么、哪些规则变化、目标画像是什么、如何实现、是否验收通过、
是否可以交付或上线”。前一阶段的封存产物是后一阶段的输入，不得跳过、倒序或用后续结果补写前序事实。

| 阶段 | 步骤 | 主要工作 | 完成标志 |
|---|---|---|---|
| VC‑0 | 冻结升级输入 | 冻结目标、基线、用途、身份、执行边界、预算、回退点和证据复用计划 | P0 记录通过，工具阻断为零，环境和回退点有效 |
| VC‑1 | 收集目标证据 | 分析目标版本源码和协议行为；已有同身份可信证据时只读复用，没有证据或缺少必要事实时才定向抓包 | 目标身份完整，`DiscoveryInventory` 与逐项证据封存 |
| VC‑2 | 逐规则判定差异 | 将每项发现归类为 `inherit/change/condition_change/add/delete`，并为每条受影响规则生成可独立验证的原子断言 | 迁移清单和原子断言完整，`blocked` 与其他未决项为零 |
| VC‑3 | 生成目标画像 | 复制当前 active 画像，只修改版本身份和 `affected_rules` 对应字段，生成 Profile、Release、SupportEnvelope 和 ApprovalFact | 所有 Profile 差异均有版本或规则来源，生产 selector 未改变 |
| VC‑4 | 实现固定 Candidate | 只实现 `affected_rules` 及其直接依赖，构建源码、画像、Release 和镜像身份固定的 candidate | 代码和测试变化均可追溯，candidate 身份不可变 |
| VC‑5 | 定向验证 | 对比官方与 candidate 的定向 PAIR，执行画像差异、负例、状态和回退验收；继承规则只重放既有收据 | 受影响项全部通过，继承字段及收据可重放，AcceptanceFact 完整 |
| VC‑6 | 交付或生产激活 | `validation_only` 交付候选；`production_replacement` 按 §5.6 执行 canary、生产切流、实际回滚和目标恢复 | 候选可交付，或生产激活、回滚、恢复及收据全部完成 |

每阶段完成后，必须在 VC-0 冻结的交接窗口内登记完成事件，并以该阶段的封存输出生成下一阶段输入；窗口
只用于登记和派发，具体时限由客户端指南或 Campaign 计划规定，不是阶段执行预算。

具体操作必须进入目标客户端的对应章节，禁止跨客户端照抄参数、工具或机器职责：

| 阶段 | Codex CLI | Claude Code |
|---|---|---|
| VC‑0 | [§4.0](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-0) | [§4.0](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-0) |
| VC‑1 | [§4.1](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-1) | [§4.1](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-1) |
| VC‑2 | [§4.2](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-2) | [§4.2](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-2) |
| VC‑3 | [§4.3](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-3) | [§4.3](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-3) |
| VC‑4 | [§4.4](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-4) | [§4.4](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-4) |
| VC‑5 | [§4.5](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-5) | [§4.5](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-5) |
| VC‑6 | [§4.6](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-6) | [§4.6](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-6) |

### 5.3.3 取证与增量验收边界

1. 官方产物、源码和协议证据必须绑定明确身份并保持不可变；绑定方法和隔离方式由客户端指南定义。
2. 已可信封存的官方证据只读复用。只有缺少完成分类或原子断言所需事实时，才能返回 VC-1 定向补证；
   工具修复、报告变化、Candidate 变化或新 Campaign 承接均不是重发官方请求的理由。
3. VC-4～VC-5 只执行 `affected_rules` 及其直接依赖闭集；`inherited_rules` 只重放来源和验收收据。
   执行计划必须区分规则总数、受影响项、继承项、实际执行项和复用项，异常扩圈须在执行前说明依赖路径。
4. 阶段只能消费前序阶段的封存输出。阶段完成事件、批次编译、checkpoint、具体字段和 runner 均由客户端
   指南定义，但不得依赖人工补派、遍历无关历史证据或用后续结果补写前序事实。

### 5.3.4 失败恢复与身份边界

失败恢复统一执行 §5.1.2。本节只定义共同身份边界；具体 ID、收据和恢复命令由客户端指南定义：

| 变化或失败 | 身份处理 | 恢复动作 |
|---|---|---|
| 身份与依赖均未变化的临时失败 | 保留原 Campaign、candidate 和 attempt | 从最近合法 checkpoint 继续，只执行 `failed` 或 `pending` 项 |
| 目标、基线、用途、账号权限条件或官方产物身份变化 | 停止当前 Campaign，建立新 Campaign | 回到 VC-0 重新冻结；从 VC-1 补齐新身份所需证据 |
| 规则分类或目标画像变化 | 停止当前 Campaign，建立后继 Campaign | 分别从 VC-2 或 VC-3 继续；仍有效的官方证据只读复用 |
| Candidate 的源码、画像或构建产物变化 | 保留 Campaign，建立新 Candidate | 从 VC-4 继续，只执行受影响闭集 |
| 仅运行坐标变化，且产物、权限和环境语义均不变 | 保留 Campaign 和 Candidate | 在首次 attempt 前按客户端指南登记坐标变化；否则重新判定身份 |
| 控制面或 evaluator 工具变化 | 不改变数据面身份 | 按 §5.1.3 只重跑受影响的离线门禁 |

任何恢复分支都不得改写历史实体或自动重发已封存的官方请求；没有实际执行项时不得创建空转身份。

### 5.3.5 时间预算与停线

VC-0 必须冻结总 deadline、阶段预算、同根因重试上限和资源水位；具体数值由客户端指南或本次已批准的
Campaign 计划规定，本框架不为不同客户端设置统一固定时长。

时间账本从准备工作的第一项开始连续记录，不得在正式取证、新 Candidate 或恢复执行时重新起算。阶段预算
或总 deadline 先到即停线；不得为同一工作对象新建 Campaign、Candidate 或控制收据来重置计时。

停线报告至少包含最后合法 checkpoint、根因、已耗墙钟、执行与复用范围、live 请求量、扫描量和唯一下一
动作。`validation_only` 在 VC-6 交付候选与完整证据；`production_replacement` 还必须按 §5.6 完成生产
激活、实际回滚和目标恢复。客户端不具备生产权限时，只能采用前一种终点。

## 5.4 修改共享合同或运行时

客户端 Header、Body、身份、状态机、重试和 transport 事实优先在 Persona 方言内表达。只有现有方言无法
承载且缺口属于厂商无关控制面时，才修改共享合同。

执行顺序：

1. 冻结全部受影响 Persona 的 active／rollback 和回退事实。
2. 证明共享修改必要性，列出直接影响闭集和失败关闭行为。
3. 对全部受影响 Persona 建立修改前 final-wire 基线。
4. 修改共享合同并只运行影响闭集及跨 Persona 隔离负例。
5. 全部验收和回退事实闭合后按 §5.6 发布。

官方客户端换版不得顺便修改共享合同；若确实需要，先停线并建立独立变更集。

## 5.5 同版本修改与旧版本退休

### 5.5.1 同版本实现修改

仅当官方规则、画像、场景和证据合同不变时，才在原 Campaign 下建立新 candidate。只重跑变化实现的规则
闭集和公共终态门禁；不得复用旧 candidate 的激活或回滚事实。

若发现规则或 Schema 不能表达真实官方行为，停止普通实现路径，重新执行 VC-2～VC-3；已有官方证据充分时
只读复用，不重新取证。

### 5.5.2 退休旧画像或兼容代码

旧版本只能在新 Active 完成生产验证、回滚路径已冻结且所有消费者不再引用它后退出 Runtime Catalog。
顺序固定为：

1. 扫描 selector、Catalog、类型、调用图和 Inventory，证明全部消费者。
2. 迁移或退休消费者，未知入口 fail-close。
3. 验证新 active、rollback、HTTP／WebSocket、状态恢复和跨 Persona 负例。
4. 删除无消费者的运行投影和兼容接线。
5. 保留历史 Release、证据和收据，生成 RemovalReceipt。

“删除旧版本”只指退出运行 Catalog 和生产投影；历史只读证据不得恢复成生产选择。
消费者扫描必须覆盖 version-route 收据等间接引用；不得只扫描当前 ReleaseGraph 和 SnapshotCatalog。
若历史收据仍冻结旧画像，旧画像只能移入不可被 selector 选择的只读证明区，并由内容摘要自校验；
当前 Active／Previous 必须独立解析同一路由。漏扫、未知引用或把历史证明重新接回 Runtime Catalog 均立即失败。

旧 `successor／control-epoch／runtime-repair／evaluation-transition` 实现按同一边界处理：
`codex_upgrade_legacy_boundary.py` 是唯一的历史兼容登记和派发入口。`codex_upgrade.py` 中仍保留的
旧函数只服务冻结历史夹具和只读回放；正式 Campaign 在取得租约前拒绝它们。删除旧函数前必须先证明
只读符号不再被 `campaign-run` 的校验链引用，并通过历史收据回放测试；不得为了清理代码删除历史收据。

## 5.6 候选交付、生产激活与回滚

<a id="638-fw-h生产迁移与遗留退休"></a>

VC-0 必须在 `validation_only` 与 `production_replacement` 中选择一个终点；进入正式 Campaign 后不得换用
另一终点。两条路径都必须消费 VC-5 的同一份封存输出，但只有具备明确生产权限的客户端才能进入生产激活。

### 5.6.1 候选交付

`validation_only` 只交付已验收候选，不改变生产 Release、selector、运行镜像或依赖：

1. 重放 AcceptanceFact，核对 Candidate、Profile、Release、规则范围和构建产物身份一致；
2. 在客户端指定的隔离环境完成最终门禁、候选切换、实际回退、目标恢复和稳定观察；
3. 封装不可变候选、配置、公开证据索引、逐规则结果、回退材料和私有证据归档清单；
4. 签发客户端定义的候选交付收据，达到 `ready_for_operator_release`。

候选交付收据不得写成生产 DeploymentFact，也不得暗示用户管理的生产环境已经验证或修改。客户端没有生产
权限、生产回退点不完整或生产环境不在本次范围内时，VC-6 必须在此终止。

### 5.6.2 生产激活

`production_replacement` 必须先满足候选交付条件，再执行以下共享步骤：

1. 只读冻结当前生产 Release、selector、运行产物、数据和依赖，并将当前 active 固定为本次 rollback；
2. 从已验收 Candidate 生成不可变 production Release 和正式产物，以差异清单连接两种身份；
3. 在隔离环境使用默认 production selector 运行 canary，禁止强制 Candidate 模式；
4. 在同一原子事务中更新 active／rollback，并只替换必要的应用运行时，不重建无关数据或依赖；
5. 通过真实入口切回 rollback 验证数据兼容，再原子恢复目标 active 并稳定观察；
6. 签发 activation receipt，绑定 AcceptanceFact、正式产物、Release、selector、canary、切换、回滚和恢复。

VC-6 只执行 `affected_rules` 的生产检查和公共终态门禁；继承规则只重放封存收据。禁止在晋升、构建、
canary、切换或回滚期间重跑完整 Candidate／规则矩阵或重发官方请求。任一失败立即恢复旧 Active，状态保持
`production_unverified`；不得在故障实例上补画像、改环境或继续扩流。

### 5.6.3 客户端执行入口

机器、命令、镜像、编排文件、收据 Schema、归档和清理方式只由客户端指南规定：

| 客户端 | VC-6 执行权威 |
|---|---|
| Codex CLI | [§4.6](CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-vc-6) |
| Claude Code | [§4.6](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-6) |

## 5.7 完成定义

候选交付完成：

```text
RuleMigrationManifest 完整
∧ affected_items 全部通过
∧ inherited_items 全部可重放
∧ AcceptanceFact 完整
∧ 客户端候选交付收据可重放
⇒ ready_for_operator_release
```

生产升级完成：

```text
ready_for_operator_release
∧ production Release 与正式运行产物已激活
∧ 默认 production selector 解析到目标 Release
∧ canary、切换、回滚和恢复通过
∧ activation receipt 可重放
∧ 审计账本完整
⇒ production_active_upgraded
```

这两个公式只定义共享语义，具体状态字段、收据 Schema 和机器门禁以客户端指南为准。客户端没有生产权限时
不得宣称 `production_active_upgraded`；任何客户端都不得仅凭版本号、Candidate、测试通过、产物存在或收据
数量宣称升级完成。
