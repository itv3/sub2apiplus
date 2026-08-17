# 官方 OAuth 客户端出站仿真共享框架

> **适用范围**：Sub2APIPlus 对官方 OAuth 客户端出站形态进行画像化实现时的共同控制框架
> **当前已登记客户端**：Codex CLI、Claude Code
> **扩展模型**：一份共享框架加若干客户端专属手册；Grok、Gemini 等只能在独立取证和审核后登记
> **架构原则**：只抽取已被多个 persona 证明稳定的控制面不变量，不预先设计万能协议、统一画像或统一 Plan
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
→ 入口协议与语义归一化
→ OAuth Provider + OfficialClientPersona + 账号路由
→ persona 的 production active ReleaseBundle
→ 客户端方言编译
→ Executor + 受信 transport adapter
→ Runtime Guard
→ 官方 OAuth 上游
```

第三方入口只负责把协议、模型、工具和请求意图转换成规范化语义；官方入口也必须经过同一终态链。
语义等价且受信条件相同的两类入站，最终必须命中同一 ReleaseBundle 并通过同一组 wire 断言。

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
| 入口归一化 | 把不同入站协议转换为规范化语义请求和有类型条件 | 选择生产版本或保留入站 wire 身份 |
| Persona Registry | 依据认证类型、官方产品和 route 解析 persona | 仅凭 host、UA 或客户端自报值分类 |
| Identity Authority | 生成受信账号、会话、agent、平台和请求事实 | 从第三方同名 Header 复制官方身份 |
| ReleaseCatalog | 解析 persona 的 active／previous ReleaseBundle | 让自动发现或入站版本直接激活 |
| 编译控制外壳 | 按 persona 闭集调用对应 `DialectCompiler`，并校验统一的编译终态契约 | 把所有厂商塞进联合 Plan／联合 Schema，或包含厂商版本常量和业务账号规则 |
| 客户端方言 | 以该 persona 专属 Plan、IdentityFacts 和画像解释 URL、Header、Body、条件与状态语义 | 复用另一客户端的 Plan、Schema、Compiler 或 wire 事实，或绕过共享终态链 |
| Executor | 签发 PreparedRequest／FinalizationToken 并选择 adapter | 签发后允许普通业务代码改写 wire |
| transport adapter | 执行画像声明的 TLS、HTTP、WS、压缩和连接行为 | 执行 Token 未授权的语义变化 |
| Runtime Guard | 校验 route、persona、Sink、Release、画像和最终摘要 | 对未知 route 或终态篡改静默放行 |

这里的“共享”只指已经被多个 persona 证明稳定的控制面和终态不变量，不表示存在一个能描述所有
厂商协议的万能实现。当前共享边界以已登记的 Codex CLI 与 Claude Code 的真实需求为上限；不得为了
尚未取证的 Grok、Gemini 或其他客户端，提前加入猜测字段、空接口、全局条件分支或联合类型。

Header 集合、Body 方言、协议能力、状态机、重试参数及 transport 细节均属于客户端画像或方言层。
`CodexEgressPlan`、`ClaudeEgressPlan` 及其 IdentityFacts、ProfileSchema、DialectCompiler 保持独立；
共享 Executor 只消费经过闭集登记的编译结果和终态元数据。现有抽象无法表达新机制时，才能修改
共享引擎，并回归全部受影响 persona 的 active、previous 和目标 candidate。

## 2.3 客户端接入契约

新增 Grok、Gemini 或其他官方客户端时，必须以独立变更集登记以下扩展点：

| 扩展点 | 必须提供的内容 |
|---|---|
| `PersonaDescriptor` | provider、官方产品、OAuth family、route／Sink 闭集和明确排除项 |
| `SemanticAdapter` | 支持的第三方入站协议、语义映射、丢失信息与拒绝条件 |
| `TypedEgressPlan` | 该 persona 独立的规范化语义和业务事实；不得扩张 Codex、Claude 或全局联合 Plan 来容纳新厂商 |
| `IdentityFacts` | 该 persona 独立的受信身份来源、类型、生命周期和冲突处置 |
| `ProfileSchema` | 版本、端点、Header、Body、状态、transport 和条件的可表达范围 |
| `DialectCompiler` | 从该 persona 的 TypedEgressPlan、IdentityFacts 与画像生成已登记编译结果的规则 |
| `TransportCapability` | 可复用 adapter 与确需新增的窄 adapter；不得把客户端差异塞进全局分支 |
| `EvidencePackage` | 官方产物、源码／bundle、P／R／J／M、摘要、适用范围和规则台账 |
| `AcceptancePackage` | 官方入口及每类第三方入口的成对断言、candidate 验收、发布和回滚收据 |

完成上述登记只表示架构可承载该客户端，不表示仿真已经成立。客户端必须拥有独立手册和证据；Codex、
Claude、Grok、Gemini 之间不得交叉继承 wire 事实。

第三个及后续 persona 的接入顺序固定为：先完成独立取证并用专属 Plan、Schema 和 DialectCompiler
表达；再识别它与既有 persona 重复且稳定的机制；最后才提交共享层扩展方案。只在一个客户端出现的
机制留在该客户端内，不能仅因“未来可能复用”而上提。共享层发生变化时，必须同时回归所有受影响
persona；仅新增客户端画像和方言时，不得无故改写既有 Codex 或 Claude 的 final wire。

---

# 第三部分 版本画像与证据生命周期

## 3.1 画像与版本坐标

版本画像必须是内容寻址、不可变、可复算的数据。`persona + version + profile digest` 构成不可变
坐标；ReleaseBundle 绑定画像、端点、transport、状态和策略；ReleaseCatalog 只保存经过批准的关系。

| 坐标 | 含义 |
|---|---|
| `discovered` | 自动或人工发现的上游版本，仅供评估 |
| `evidence_baseline` | 已封存官方证据绑定的版本 |
| `approved_profile` | 已批准但不一定部署的画像版本 |
| `production_active` | 由生产激活事实证明正在生效的版本 |
| `production_previous` | 已冻结并完成回滚证明的上一版本 |

旧画像不得原位覆盖，证据、发布图和收据只追加；自动发现和入站自报版本均不得改变生产 active。
源码常量、测试通过或 Campaign 达到 `ready`，都不能单独证明 `production_active`。现有 Schema 能
表达时只追加数据；出现新协议、新状态机制或 Schema 缺口时，才独立修改共享引擎并执行多版本回归。

## 3.2 证据与变更身份

客户端规则至少声明版本、产物摘要、平台、入口、认证、模型、配置、网络条件、观测通道、正负例和
适用边界。只能从证据实际可见的层次得出结论，pcap、原始应用层字节、MITM 解析、源码或 bundle
控制流不能互相替代。

规则状态使用 `verified`、`observed`、`candidate`、`blocked`、`regressed_evidence`、
`response_compat` 或 `superseded`。只有 `verified` 表示规则、条件、运行证据与复算链闭环；其余状态
必须保留有限样本、阻断、证据退化、下游兼容或被取代的真实边界。

| 单元 | 身份变化边界 |
|---|---|
| Campaign | 官方目标版本、产物、平台、默认条件、批准规则、场景、画像或产出侧工具变化 |
| candidate | Sub2APIPlus 源码、测试树、构建、镜像、画像或用途变化 |
| attempt | 上述身份不变，只因临时网络、额度或运行失败重试 |

三者均只写追加。新 attempt 不覆盖旧失败；candidate 验收通过不代表生产已经部署。

## 3.3 高频版本闭环

1. 冻结目标官方产物、依赖、运行时和平台身份；
2. 比较每条规则的语义锚点、sink 绑定和运行时依赖；
3. 分类为 `inherit`、`change`、`add`、`delete`、`condition_change`、`blocked` 或
   `regressed_evidence`；
4. 始终重跑最小 P／R／J／M 哨兵场景；
5. 对受影响规则、动态条件、远程 gate、TLS／运行时变化和负例重新采集；
6. 生成候选画像后，在所有规定入口执行同一组逐规则断言。

字符串相同、窗口邻近或 minify 标识符相同都不足以证明规则未变。只有工具能证明语义片段、依赖和
sink 关系时，静态锚点才可支持继承；能力不足时使用目标版本重新观察的保守路径。

---

# 第四部分 验收、发布与回滚

## 4.1 成对验收与 strict 门槛

每个目标规则建立 `PAIR-<SPEC-ID>`。官方入口和每类第三方标准 API 入口提交语义等价请求，并在同一
candidate、画像和条件下验证最终 wire 收敛。动态字段比较来源、格式、关系和生命周期，不比较一次
抓包的随机字面值。

| 用途 | 允许终态 |
|---|---|
| `validation_only` | 可保留已记录的 `blocked`／`regressed_evidence`，止于诊断或 provisional |
| `production_replacement` | 所有目标规则达到批准等级，不存在阻断 strict wire 的未决项 |

第三方入口按协议类别登记；无法无损映射官方语义时必须拒绝或明确记录降级。任何对齐责任规则仍为
`blocked`，或已验证规则降为 `regressed_evidence` 时，candidate 不得成为 strict active；验收通过
也不会自动提升官方规则的证据等级。

## 4.2 生产与回滚

生产启用必须绑定同一 candidate 的验收、画像晋升、正式镜像、终态门禁、独立 canary、正式切换、
旧版回滚和目标恢复。只替换应用容器，不重建数据库、缓存和依赖服务。运行容器 digest、active／
previous、画像摘要和 activation fact 不一致时，状态为 `production_unverified`。

---

# 第五部分 文档职责与客户端登记

当前人类可读权威文档为：

| 文档 | 责任 |
|---|---|
| [`CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](CODEX_CLI_CLIENT_EMULATION_GUIDE.md) | Codex CLI 官方事实、画像、实现和版本流程 |
| [`CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md) | Claude Code 官方事实、画像、环境、实现和版本流程 |

未来新增官方客户端时采用“一客户端一专属手册”，并在本节登记。Grok、Gemini 目前只是架构可扩展性
示例，不表示已经确认其官方客户端、OAuth 路径、规则或仿真实现。机器证据、JSON 台账、Schema 和
收据可以分目录保存，但不得形成第二套人类可读规范。

---

# 第六部分 首轮实施步骤：Codex 骨架抽取与 Claude 重建

本轮改造必须先保护已经成熟的 Codex 执行链，再重建 Claude 仿真；不得先修补 Claude 遗留实现，
也不得以遗留 Claude 输出反向设计共享框架。各步骤按独立变更集依次推进，前一步未达到退出条件时，
不得进入下一步。

1. **冻结现状基线。** 冻结 Codex active／previous 的官方证据、画像和 final wire，作为后续重构的
   正确性基线；冻结 Claude Code 2.1.220 官方证据为 `evidence_baseline`，供新架构生成 previous 画像和
   差分判定。Claude 遗留链路的 final wire 只用于接入点盘点、差异定位和回归诊断，不得作为正确规格、
   期望输出或画像晋升依据，也不得先修复遗留架构再升级画像。
2. **抽取最小多 Persona 共享控制层。** 从 Codex 专用执行链中只抽取 Persona Registry、
   ReleaseCatalog 及 active／previous、Executor 控制外壳、FinalizationToken、Runtime Guard、
   Route／Sink 防旁路，以及激活、回滚和收据。不得在此阶段泛化 Codex 协议事实，也不得为了兼容
   Claude 遗留代码扩张共享接口。
3. **证明 Codex 零差异并独立完成生产验证。** 先在本地完成全部门禁及 Codex active／previous
   回归，再将固定 `linux/amd64` digest 的 Codex-only candidate 部署到 DMIT，验证画像选择、编译结果、
   Route／Sink 归属、FinalizationToken、Runtime Guard、final wire 和回滚路径均与冻结基线一致。
   准备发布的正式镜像还必须在 DMIT 完成隔离 active canary、旧版回滚和目标恢复演练。全部通过后，
   在 Vircs 只读冻结旧镜像、compose、配置和依赖作为回滚点，再以固定 digest 仅替换生产应用容器，
   并核对 health、日志、运行镜像、active 画像、activation fact 和 Guard；只有部署异常时才执行真实
   回滚，不得为演练在 Vircs 额外切换生产容器。该阶段不得进行探索性测试，也不得夹带 Claude 代码
   或画像。DMIT 演练收据、Vircs 部署收据和稳定观察未完成前，共享控制层不得接入 Claude，也不得
   进入下一步。
4. **冻结 Claude 目标稳定版并差分取证。** 开始 Claude 实现时查询官方 `stable`，冻结准确版本、官方
   产物、摘要、平台和默认条件为本轮 candidate；再以 2.1.220 为基线执行语义锚点比较、最小
   P／R／J／M 哨兵运行和逐规则分类。复用能够证明继承的既有证据，只对变化项、证据缺口、动态条件
   和版本敏感项定向补证，不进行无依据的全量重采。
5. **登记独立 Claude Persona。** 根据冻结后的 candidate 证据建立 Claude 自有的
   `ClaudeEgressPlan`、`IdentityFacts`、`ProfileSchema`、`DialectCompiler`、画像版本和
   Route／SinkBinding。Claude 不得继承或扩张 `CodexEgressPlan`；遗留 Claude 实现只能帮助识别
   现有接入点和待删除旁路。
6. **直接按 candidate 重写 Claude 仿真。** 以 Claude 专属手册和本轮官方取证为规范，使用新架构独立
   实现 Header、Body、状态机、transport 和条件规则；2.1.220 由同一新 Schema 和 Compiler 表达为
   previous，不修复或迁移遗留 Claude 架构。新链路先与遗留链路影子比较，但差异只由官方证据裁决，
   不能因为旧代码已有某种输出而保留错误行为。
7. **在 DMIT 完成成对入口验收。** 对官方 Claude Code 入站和每类第三方标准 API 入站提交语义等价
   请求，验证它们经 Claude OAuth 出站时收敛到 candidate 对应的官方 Claude Code final wire；同时
   验证 2.1.220 previous 的冻结、解析和回滚路径。
8. **灰度切换并淘汰旧链路。** candidate 验收通过后执行生产 canary、正式激活、回滚证明和目标恢复；
   稳定观察完成后，才删除 Claude 旧 finalizer、版本常量、代码画像构造器及其他旁路。

本轮 candidate 一经冻结，不得因开发期间官方 `stable` 再次变化而中途换版；新版本只登记为
`discovered`，待当前 candidate 完成验收和激活后，再通过下一轮画像升级流程处理。共享框架不得写死
某个当时的 Claude Code 版本号。

该顺序只把已被 Codex 证明稳定的控制机制抽成共享骨架；Codex 与 Claude 的协议画像、编译器和证据
始终独立。以后接入 Grok、Gemini 时仍先完成独立取证和专属实现，只有被多个 Persona 证明稳定的机制
才允许继续上提到共享层。
