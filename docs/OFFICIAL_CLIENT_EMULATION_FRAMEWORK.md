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

每个 Job 和门禁必须声明逐文件直接依赖，形成有向无环图。结果键为：

```text
result_key = item_id + input_sha256 + environment_sha256 + direct_dependency_sha256
```

组件至少分为：

- `producer`：采集、中继、脱敏；
- `evaluator`：manifest、分类、断言和报告；
- `control`：状态机、租约、watchdog 和计时；
- `scenario`：单个场景合同；
- `runtime`：源码、二进制、镜像和容器；
- `network`：固定出口及 MTU；
- `gate`：单个验收或部署门禁。

全局工具树摘要只用于审计，不能作为失效依据。变化文件必须映射到明确组件和下游项；未知或未登记文件在
执行前失败关闭，不得退化为全量重跑。

| 变化 | 允许动作 |
|---|---|
| 文档、报告、监督器、状态查询 | 不重跑规则或 Job |
| 单个 evaluator／gate | 只重跑该离线项及下游报告 |
| 单个 producer／scenario | 只重跑直接依赖它且尚无可信结果的 Job |
| runtime 代码 | 只重跑受影响规则及公共终态门禁 |
| network 语义 | 重做 P0；仅当结果安全性受影响时使直接依赖 Job 失效 |
| 官方原始请求证据 | 默认只读；确实缺失时须人工批准新取证 |

#### 恢复算法

所有新 Campaign 只允许一种恢复方式，不得创建 `successor`、`control epoch`、`runtime repair`、多槽
`evaluation transition` 或递增 `vN` 来绕过失败。

恢复步骤固定如下：

1. 读取同一 Campaign 最近一个合法 checkpoint。
2. 校验 Campaign、ApprovalFact、candidate、环境和来源收据身份。
3. 计算 `execute = failed ∪ pending ∪ changed_dependencies 的下游闭集`。
4. 从执行集合移除已有可信通过结果且依赖未变化的项。
5. 输出并封存 `RecoveryPlan`，明确 execute、reuse、原因、扫描和 live 请求预算。
6. 若 `execute=[]`，在 reservation 前写 `incremental-noop` 并立即成功退出。
7. 否则按拓扑序只执行 execute；每项结束立即写 checkpoint。

`incremental-noop` 不启动容器、不做环境探针、不读大证据、不发请求，且必须记录
`scanned_bytes=0`、`live_request_count=0`。

同一根因连续失败两次即停线。修复必须先在冻结的最小历史夹具上完整跑通从故障点到最终阶段，再允许恢复；
禁止把正式 ARM64 Campaign 当作工具集成测试环境。

历史 Campaign 如使用旧恢复类型，只允许一次导入为当前规范的 canonical checkpoint。导入只重放摘要和
来源，不复制证据、不改变身份、不执行 Job；导入失败即保持历史 Campaign 停线，不再新增兼容分支。
Kilo 导入必须直接校验已封存事实的真实结构：`observations` 是仅含 `kilo-compatible` 和
`kilo-responses` 的对象；不得把它臆造为数组，也不得因此重发 Kilo。

### 5.1.3 控制面、环境面与数据面独立失效

| 身份面 | 内容 | 变化后的最大影响 |
|---|---|---|
| 控制面 | 状态机、租约、watchdog、计时、状态查询和只读收据解析 | 只重跑控制面离线门禁 |
| 环境面 | ARM64容器、路由、出口、MTU、磁盘和依赖事实 | 重做P0，并按语义差异决定直接下游 |
| 数据面 | 规则实现、场景、producer、二进制、镜像和协议证据 | 只重跑对应规则及其下游闭集 |

三种身份必须分别计算摘要和失效集合。控制面修复不得改变数据面 `result_key`，环境 producer 的代码摘要
不得冒充环境语义变化；任何工具都不得把三者重新合成全局失效开关。

canonical 调度与生产收据文件（`codex_upgrade_campaign_run.schema.json`、
`codex_upgrade_legacy_boundary.py`、`codex_upgrade_gate_receipt.py`、
`production_activation_receipt.py`、`production_activation_receipt.schema.json`、
`profile_rule_patches_0_151_0.json`）属于控制／评估面，只能重放对应门禁，不能触发已封存请求重跑。
新增文件必须先登记到白名单；未登记文件继续按产出面 fail-close。

#### 连续监督与时间账本

新 Campaign 的正式执行入口固定为
`tools/official_client_capture/codex_upgrade_supervisor.py campaign-run`。它读取一个不可变的
`codex-upgrade-campaign-run/v1` 动作清单，在同一个父监督器内按声明顺序自动执行全部动作；动作之间不得
依赖人工再次派发。清单为 `no_op=true` 且动作集合为空时，直接写入 `incremental-noop` 并结束。
`campaign-start`、`campaign-mark`、`campaign-exec` 仅保留给监督器离线回归和历史收据兼容，不得作为新正式
升级的编排入口。

`campaign-run` 派发动作时注入父 `run_dir`、Campaign 身份、owner nonce 和原始
deadline；动作内的 `codex_upgrade.py` 只能附加到该父监督器，禁止再创建
`CampaignLease`、`.supervisor/run-*` 或重新起算 deadline。动作清单可同时声明
`execute_items` 与 `reuse_items`：前者才允许执行，后者只能读取既有 checkpoint；
前者为空时必须生成 `no_op=true` 并立即结束。`successor`、`control-epoch`、
`evaluation-transition`、`terminal-transition-preflight` 以及监督器的旧写入入口
在正式 `campaign-run` 上下文中于取得 lease 前硬拒绝，不产生新的写入收据。
0.151.0 formal 的 capture、classify、profile、compare、accept、resume 和
canonical 写入命令若未携带父上下文同样硬拒绝；`status` 等只读命令不受此限制。

唯一的廉价修复例外是候选已经进入 `awaiting_receipts`、全部 Candidate Job 均为
`reused/complete`、`executed_job_ids`／`failed_job_ids`／`pending_job_ids` 为空，且
尚未生成 `evidence-manifest`、seal draft 或 seal preview。此时若当前工具变化只落在
`control`、`evaluator`、`orchestrator` 三个组件，`campaign-run` 可登记
`metadata_only_seal_repair` 并继续 seal；不得重发请求、不得创建旧
`evaluation-transition`，也不得把该例外用于已有失败或已开始深度扫描的 attempt。
若该 attempt 绑定的 UpgradeTimingLedger 已在 VC-0 因 `permanent-stop-*` 停线，
只有冻结 checkpoint 仍为 active、停线后的唯一新增事件使 `head_sequence` 恰好加一且
当前 `live_request_count=0` 时，才可只读承接该 Ledger；其他 stopped／stop_required
状态、多个新增事件或非零 live 请求都必须 fail-close，不能通过 active 门禁。
若当前使用的是历史 control epoch，且该 epoch 的 `boundary` 全零、其 Ledger 仅因
预算到期进入 `stop_required`（`same_root_cause_failures` 为空、`live_request_count=0`），
则 metadata-only seal 可回退到上述 Campaign 冻结控制收据；该回退不得承接失败重试，
不得创建新的 epoch／successor，且仍须通过冻结 VC-0 Ledger 的唯一 `permanent-stop-*`
校验。任何非零边界、失败计数、live 请求或其他 `stop_required` 原因继续 fail-close。

metadata-only seal 还必须校验来源 attempt 的 `environment/after` 目录及其
`after_probe` 绑定，并将 `probe-manifest.json` 与五份状态快照逐文件以不可覆盖副本
写入当前 attempt；当前已有的 `environment/client-after` 只能与该来源 after 生成恢复收据。
该过程不得执行环境探针、发送请求或改写来源文件；来源摘要漂移、目录缺项、额外文件或目标文件不一致均立即 fail-close。

VC-6 还必须在监督器层面硬拒绝旧入口：只要命令阶段是 `VC-6`，即使没有父上下文，
`campaign-start`、`campaign-mark`、`campaign-exec`、`campaign-stop`、`campaign-owner`
和 `run` 也不得启动或改写状态；唯一允许的正式入口是带完整不可变 manifest 的
`campaign-run`。manifest 在创建父 run 前完成校验并封存摘要；不得只登记一个
planning 动作后再等待人工补派。

每个 Campaign 从首个动作到生产验证结束使用一个独立父监督器，所有命令必须通过统一包装器执行。监督器
独立于升级任务运行，并实时落盘：

- 动作开始、结束、失败事件立即追加并 `fsync`；
- 每5秒记录监督器和 worker 心跳；
- 每60秒生成时间账本，将区间分类为 `planning`、`active` 或 `waiting`；`orchestrator-idle` 只允许兼容读取旧收据，
  新流程不得登记该分类；
- worker 失联20秒、动作超时或编排器15秒未派发下一动作立即停线；
- 正常停止、SIGTERM、SIGKILL、会话断开和主机失联均留下可审计终态或明确缺口。

Campaign 使用一个不可后移的全局 deadline；进程重启、attempt、恢复或监督器换代均不得重置。新监督器
必须绑定前序终态、摘要和时间缺口。任何分钟无法分类即 `audit-incomplete`，禁止部署。

每阶段结束后立即汇总开始时间、结束时间、耗时、execute、reuse、失败项、live 请求数、扫描次数、扫描／
复用字节和下一动作。不得在升级结束后补写。

编排器必须按有限状态机运行：`dispatching → executing → evaluating → terminal`。
正式 `campaign-run` 直接按预声明队列连续执行，不得登记 `planning:dispatch-next-action`，
也不得依赖人工补派；动作成功后立即进入队列中的下一项。15 秒派发窗口只保留给历史
离线兼容回归，若它出现在 VC-6 正式账本中必须停线并标记为入口违规。禁止使用无 Job 的
`post-action-idle` 保持心跳。评估后若 `failed_items=[]` 且 `pending_items=[]`，必须立即请求终态或
下一阶段；若集合为空但当前阶段未满足退出条件，必须立即失败，不能继续等待、重试或创建新 Campaign。

### 5.1.4 ARM64固定环境

Codex CLI 的抓包、测试和部署必须在 ARM64 完成。以下网络事实是不可修改的环境合同：

```text
sub2apiplus = 172.25.0.3
capture-cli = 172.30.0.10
公网出口 = 179.255.100.158
wg1 MTU = 1420
```

P0、attempt 和部署前后均须核对容器 IP、默认路由、公网出口、宿主持久 MTU、运行 MTU 和对端 MTU。
不一致时在任何外部请求前停线；禁止修改网络、WireGuard、iptables／nftables 或容器 IP 来迁就测试。

只有明确的采集传输故障且已由 pcap 证明时，辅助程序才可在自身 socket 设置已批准的 TCP 参数；不得改变
官方画像或删除 TLS 字段掩盖网络问题。

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

### 5.3.1 P0冻结

开始前冻结：

1. 目标版本、官方产物、依赖、平台、入口和摘要；
2. 当前 active／rollback Release、Profile、selector、镜像和回退收据；
3. `validation_only` 或 `production_replacement` 终点；
4. Campaign、candidate、attempt、证据根和工具版本；
5. 全局墙钟预算、阶段预算、重试上限、资源水位和复用决定；
6. 明确的账号与 API Key 数据库 ID，禁止脚本默认选择或回退到其他 Key；
7. ARM64固定网络合同；
8. 宿主机 `project_root`、`data_root`、容器内运行根、兼容根、Compose 工作目录和完整挂载映射。

P0 必须完全离线验证升级器、Schema、监督器、最小历史夹具和生产部署预演。任何工具阻断必须在创建正式
Campaign 前解决。正式 Campaign 开始后不得修改框架工具；发现缺陷按 §5.1.2 停线并拆分修复。

#### 5.3.1.1 宿主目录与输出边界

每个采集 Docker 项目必须登记唯一宿主机目录坐标。默认 `project_root` 为
`/root/docker/<project-id>`，保存 Compose、镜像构建输入和受管配置；唯一允许运行时产出写入的 `data_root` 为
`<project_root>/data`，权限必须为 `0700` 并从版本控制中排除。偏离默认布局必须有写一次的目录坐标
收据、必要性和退出期限，不能由脚本当前目录或历史习惯隐式决定。

输出边界固定如下：

| 对象 | 允许位置 |
|---|---|
| Compose、Dockerfile、镜像构建输入和受管配置 | `project_root` 内、`data_root` 外的已登记路径 |
| state、runtime、work、Campaign、control、audit、staging 和 archive | `data_root` 的对应子目录 |
| 短期测试夹具 | `data_root/staging`；只有操作系统能力测试确实要求时才可使用 `/tmp` |
| 历史绝对路径兼容 | 收据登记的 `compatibility_root`，仅允许只读重放和迁移验证 |

创建目录、打开输出文件和启动容器前，工具必须解析已有路径组件并验证最终位置仍位于允许根内；`..`、
符号链接、相对路径、未登记 bind source 和大小写近似目录均不得绕过该检查。禁止直接在 `/root` 生成
抓包、源码树、构建包、bundle、patch、worktree、恢复树或临时目录，也不得先写到 `/root` 再移动到
受管根来规避门禁。

Compose 必须从登记的 `project_root` 执行并冻结显式项目名。P0 须解析合并后的 Compose 配置，逐项核验
build context、env file 和宿主 bind source；项目数据必须位于 `data_root`，系统级挂载必须逐项登记用途、
读写模式和最小权限。随后以 `docker inspect` 复核实际挂载源、目标、传播模式、网络和镜像身份，不能只
审核 YAML 文本。

受管工具在运行时直接读取的仓库文档属于工具直接依赖。部署清单必须逐文件登记其规范相对路径和摘要，
并与工具树在同一可回滚事务中切换；首次纳管的缺失文件也必须能恢复为不存在。P0 必须从生产运行根执行
相关重放测试，禁止只在完整开发工作树通过后把不完整文档子集发布到 ARM64。

P0 和每个 attempt 的前后都必须执行有界宿主污染检查：只枚举 `/root` 第一层并与开始前 inventory、
项目注册表和明确保留项比较，不递归扫描整个根目录。发现新的未登记采集对象立即停线。临时对象只能按
冻结 manifest 精确清理；清理前必须完成引用、占用、挂载和摘要复核，清理后生成包含对象清单、字节数、
前后磁盘水位、排除项及复验结果的收据。原始证据、canonical checkpoint 和仍被收据引用的对象禁止清理。

历史项目迁入标准根时，须先封存旧、新目录和 Compose 摘要，再停写、迁移、逐项复验并仅重建受影响容器。
需要让旧绝对路径继续可解析时，只能建立登记过的只读兼容 bind mount，禁止复制出第二份可写事实或用
符号链接冒充证据根。目录变化属于运行坐标变化，按 §5.3.4 登记覆盖收据；兼容消费者清零并生成移除
收据后才能撤销旧挂载。迁移闭合前不得创建目标版本 Formal Campaign。

### 5.3.2 VC-0～VC-6

| 阶段 | 操作 | 退出条件 |
|---|---|---|
| VC-0 | 完成 P0，冻结基线、目标、预算和复用计划 | 工具阻断为零，监督器、网络和回退点有效 |
| VC-1 | 只读导入可信目标证据；仅缺事实时定向取证 | 目标身份完整，DiscoveryInventory 封存 |
| VC-2 | 逐规则生成迁移决策和原子断言 | `inherit/change/condition_change/add/delete` 完整且未决为零 |
| VC-3 | 复制当前active画像，只应用版本字段和`affected_rules`补丁，生成新Profile、Release、SupportEnvelope和ApprovalFact | Profile diff全部映射到版本身份或差异规则，selector未改变 |
| VC-4 | 只实现 `affected_rules`，生成固定 candidate | 代码与测试改动均能追溯到规则或直接依赖 |
| VC-5 | 定向 PAIR、画像diff门禁、负例、状态和回退验收 | 受影响项通过，继承字段及收据可重放，AcceptanceFact完整 |
| VC-6 | 交付候选或按 §5.6 激活生产 | 候选可交付，或生产切流、回滚、恢复和收据全部完成 |

每个阶段必须在60秒内写完成事件并进入下一阶段。任何执行计划必须同时报告：

```text
total_rule_count
affected_rule_ids
inherited_rule_ids
execute_item_ids
reused_item_ids
```

若 `affected_rule_ids` 很小但 `execute_item_ids` 异常扩大，必须在执行前停止并解释依赖路径；不得以“更安全”
为由全量运行。

### 5.3.3 取证与候选限制

任何调用官方客户端的 Job 都必须冻结二进制绝对路径，并在 reservation 前逐字核验 `--version`。禁止依赖
`PATH`、通用软链接或旧版本默认值。

MITM／代理 Job 使用独占空配置目录，显式关闭插件、MCP发现、更新检查和遥测。每个场景独立 checkpoint；
失败只重跑该场景。直连成功而采集代理失败时，只允许一次受管复现，随后停线诊断，禁止重跑全矩阵碰运气。

新版本取证只覆盖：

- `change`、`condition_change`、`add` 的必要正反事实；
- `delete` 的不存在性和消费者闭合证明；
- `inherit` 尚缺少的目标语义证明。

不得为继承规则重新生成 candidate 流量。官方请求证据一旦可信封存，工具修复、报告变化和 candidate 变化
均不得成为重发理由；此后需要新 Campaign 时，用 `reuse-official-evidence` 把已封存官方阶段只读导入，
只重新批准分类，不重发官方请求。

VC-5 的增量边界固定如下：

- `seal` 只聚合 canonical checkpoint 和本轮新增的小型收据，不遍历历史证据根；
- `compare` 只比较 checkpoint 摘要、画像差异和 `affected_rules` 收据，历史原始证据扫描量必须为零；
- `accept` 只执行或重放 `affected_rules` 的断言；`inherited_rules` 必须逐条重放导入时封存的迁移收据；
- 任一步骤发现执行集合含继承规则、九项已复用 Candidate Job 或新 live 请求，立即停线。

### 5.3.4 失败恢复

官方客户端升级失败统一执行 §5.1.2，不再定义版本专用恢复分支：

- 身份不变的临时失败保留原 attempt，只执行失败或未完成项；
- 规则、画像、用途或官方产物变化时停止当前 Campaign，并从相应 VC 阶段建立新身份；
- candidate 源码、构建或镜像变化时建立新 candidate，但只执行受影响闭集；
- 采集账号、容器名、二进制路径或 compose 坐标等运行坐标变化时不新建 Campaign，也不新建 candidate；
  在该候选首个 attempt 前登记候选层运行坐标覆盖收据，run 与 seal 读取同一份收据；
- 控制面工具变化按 §5.1.3 生成 evaluator run，不使规则、证据或 Candidate Job 失效；
- 已封存官方请求只读复用，任何恢复均不得自动重发；
- 执行集合为空时写 `incremental-noop` 并立即退出。

旧 Campaign 的 `successor／epoch／repair／transition` 只作为历史记录读取，不得继续追加，也不得作为新流程
前置条件。需要承接时按 §5.1.2 一次性导入 canonical checkpoint。

### 5.3.5 时间预算与停线

正常官方客户端增量升级目标为4～6小时；差异很小时应明显短于该目标。默认阶段预算：

| 阶段 | 上限 |
|---|---:|
| VC-0 | 45分钟 |
| VC-1～VC-3 | 75分钟 |
| VC-4 | 90分钟 |
| VC-5 | 75分钟 |
| VC-6 | 75分钟 |

阶段或总预算先到即停线。预算到期不得创建新 Campaign、candidate、successor 或控制收据来重置计时。
停线报告必须给出最后合法 checkpoint、根因、已耗墙钟、execute／reuse、live 请求数、扫描字节和唯一下一动作。

若差异不超过3条，P0 应给出2小时内完成定向实现、验收和部署的计划；无法满足时必须在正式取证前说明具体
阻断，禁止进入无界执行。

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

生产激活只执行以下六步：

1. 只读冻结当前镜像、compose、selector、Release、画像、数据和依赖，并将当前active确定为本次rollback。
2. 从已验收candidate生成不可变production Release和正式ARM64镜像；不得修改或覆盖旧Release。
3. 在隔离环境用默认 production selector 运行 canary，禁止强制 candidate mode。
4. 在同一原子事务中将`production_rollback`指向原active、将`production_active`指向目标Release，并只替换应用容器；不得重建数据库、缓存、网络或挂载。
5. 通过selector切回rollback验证真实入口和数据兼容，再原子恢复目标active并稳定观察。
6. 签发 activation receipt，绑定 AcceptanceFact、源码、镜像、Release、selector、canary、切换、回滚和恢复。

VC-6 只能从最新 canonical checkpoint 续跑，唯一入口为：

```text
canonical-advance production-activation --step-receipt <activation-receipt>
canonical-advance rollback-verification --step-receipt <activation-receipt>
canonical-advance retire --retire-version <旧-previous-version> --step-receipt <removal-receipt>
```

上面三条仅是 `campaign-run` 动作的 operation 示例，不得作为独立 CLI 写入入口。

三个步骤都必须校验收据后追加 checkpoint，不得改写旧 checkpoint。`production-activation` 必须先绑定
本轮 canonical acceptance、正式镜像和目标恢复终态；`rollback-verification` 只重放同一激活收据中的
回滚与恢复事实；动态生成的 `retire-<旧-previous-version>` 仅在前两项完成且消费者扫描为零后执行。
`--retire-version` 必须在 canonical 初始化时显式冻结，且不得等于本轮 active rollback 或目标版本。失败只保留当前步骤为待执行，
禁止回退到 VC-0～VC-5、重建 Candidate、重跑九项 Job 或重发 Kilo。

ARM64部署包必须同时包含受管工具和两份活动文档，拒绝 AppleDouble 文件；暂存树先验证摘要、属主和权限，
再原子交换。部署包不是完整源码树；受影响项和公共终态门禁必须在完整只读源码快照执行，不能因包内缺
文件扩大测试集合、修改业务工具或重跑已通过项。

监督器执行子进程时 stdin 固定关闭；禁止用 heredoc、管道输入或交互命令传入部署逻辑。部署脚本必须先
作为有 SHA-256 的普通文件落盘，再作为 `campaign-run` 的预声明动作执行；执行后必须核对活动文件摘要和命令帮助，
若命令返回 0 但摘要未变化，按 `supervised-noop` 失败，不得视为部署成功。

部署后必须验证：

- active 版本、Release、Profile 和 selector 一致；
- ARM64固定容器 IP、DMIT出口和 MTU 未变化；
- 所有受影响规则通过生产最小检查；
- 未受影响规则的冻结收据和依赖摘要仍可重放，不重新发送请求；
- 回滚和恢复均成功；
- 父监督器账本完整且 `audit-incomplete=false`。

VC-6 的测试集合固定为 `affected_rules` 的实现测试和公共终态门禁；继承规则只重放 checkpoint 摘要。
禁止在 promotion、正式构建、canary、切换或回滚阶段重新执行全量 Candidate／全量规则回归。

任一失败立即恢复旧 Active，状态保持 `production_unverified`，不得在故障实例上补画像、改网络或继续扩流。

## 5.7 完成定义

候选交付完成：

```text
RuleMigrationManifest 完整
∧ affected_items 全部通过
∧ inherited_items 全部可重放
∧ AcceptanceFact 完整
⇒ ready_for_operator_release
```

生产升级完成：

```text
ready_for_operator_release
∧ ARM64 production Release 已激活
∧ canary、切换、回滚和恢复通过
∧ activation receipt 可重放
∧ 审计账本完整
⇒ production_active_upgraded
```

除这两个公式外，不得以版本号、candidate、测试通过、镜像存在或收据数量宣称升级完成。

自 0.151 起 `production_active_upgraded` 由门禁机器判定：终态收据 `docs/egress/maintenance/CODEX_CLI_<前版>_TO_<本版>_TERMINAL_STATE_RECEIPT.json` 入库后，`make check-egress-spec-ci` 与 `officialegress`/`service` 冻结测试校验其自摘要、四份阶段收据与审计索引逐字在库、审计索引复核通过、退休画像已不存在、Runtime Catalog 的 source 落在其 Campaign 链上；Runtime Catalog 的 active 版本没有终态收据即失败。
