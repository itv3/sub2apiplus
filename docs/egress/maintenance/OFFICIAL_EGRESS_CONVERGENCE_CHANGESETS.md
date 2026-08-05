# Official Egress 必要变更与触发式观察清单

> 文档状态：清单已完成必要性复核；各变更集的详细实施方案均未确认  
> 创建日期：2026-08-05  
> 最后更新：2026-08-05  
> 基线提交：`1b80dcc9c`  
> 适用范围：Sub2API 上游小侵入、Codex CLI 画像升级与 official egress 兼容层收敛

## 1. 文档目的

本文只把存在当前证据或明确触发条件的工作保留为可跟踪条目，用于记录方案确认、实现、验证、
回滚和退休证据。原 15 项清单经过必要性复核后，部分条目已删除、合并或降级为观察项；本文
不是“全部必须实施”的承诺，也不替代 `docs/CODEX_CLI_0145_EGRESS_SPEC.md` 中的 wire 契约、
Campaign 规则和 §4.9 兼容代码退休流程。

全部工作遵守以下原则：

1. 一次只实施一个变更集；复合条目必须继续拆成独立子变更集。
2. 每个变更集在编码前单独提交方案，并取得明确确认。
3. 优先修改 Fork 自有文件；触碰上游已有文件时只保留薄 Hook。
4. 不以删除行数为目标，以 final-wire 不漂移、合并成本下降和跨版本可验证为验收标准。
5. 不改写历史画像、摘要或证据；新版本、新结果和新收据只追加。
6. Previous 仍依赖的兼容能力不得退休。

## 2. 状态与完成规则

| 状态 | 含义 |
|---|---|
| 待启动 | 尚未提交该变更集的详细实施方案 |
| 方案审核 | 已完成调研，正在等待方案确认 |
| 实施中 | 方案已经确认，正在编码或补充资产 |
| 验证中 | 代码完成，正在执行测试、wire 比对或实机验收 |
| 阻塞 | 已记录明确阻塞条件，当前无法安全继续 |
| 已完成 | 验收标准全部通过，证据与收据已经归档 |
| 已取消 | 经审核确认不再需要，并记录取消理由 |

“已完成”至少要求：

- 对照本条目的实施范围和非目标完成复核；
- 相关单元、集成、race、final-wire 或工具门禁通过；
- 涉及源码或 overlay 时，重新计算受影响的 upstream overlay 指标；
- 没有扩大未授权的上游修改面；
- 需要退休旧路径时，完整执行规范 §4.9 并生成收据。

## 3. 变更集进度

表中“依赖”是开始实施前的硬依赖；标记为“退休门禁”的条目不阻塞前期调研、测试或兼容实现，
但未完成前不得删除旧路径或正式启用新画像。

当前主线保留 10 个变更集；另有 2 个触发式观察项和 2 个不属于收敛主线的独立 Backlog 子任务。

| ID | 变更集 | 优先级 | 依赖 | 负责人 | 目标日期 | 状态 |
|---|---|---:|---|---|---|---|
| CS-01 | Compiler 静态 URL 封闭 | 高 | 无 | 待分配 | 待定 | 待启动 |
| CS-02 | CompilerRejected 的 attempt/fallback 契约与测试 | 高 | 无 | 待分配 | 待定 | 待启动 |
| CS-03 | 版本中立架构 ADR | 高 | 无 | 待分配 | 待定 | 待启动 |
| CS-04 | 上游入侵面 ratchet | 高 | 无 | 待分配 | 待定 | 待启动 |
| CS-05 | Campaign/candidate 边界修订 | 高 | 无 | 待分配 | 待定 | 待启动 |
| CS-06 | Makefile 与工具链版本参数化 | 中 | CS-05 | 待分配 | 待定 | 待启动 |
| CS-07 | 真实异画像 active/previous 演练 | 高 | CS-05、CS-06 | 待分配 | 待定 | 待启动 |
| CS-09 | alpha-search mode/version 单一权威 | 低 | CS-07 为最终验证门禁 | 待分配 | 待定 | 待启动 |
| CS-10 | Body 差分测试与单一权威门禁 | 中 | CS-03；CS-07 为退休门禁 | 待分配 | 待定 | 待启动 |
| CS-11 | service DTO 字段覆盖与条件退休 | 中 | CS-10；CS-07 为退休门禁 | 待分配 | 待定 | 待启动 |

### 3.1 原清单处置记录

| 原条目 | 处置 | 理由 |
|---|---|---|
| CS-08 | 降级为 OBS-01 | 没有故障或合并成本证据，不预先重构 Forward |
| CS-11B | 删除独立实施任务 | 只作为 CS-11 和 §4.9 的条件退休步骤 |
| CS-12 | 删除 | typed predicate 是未被证据证明的预设方案，需要时在具体热点内局部设计 |
| CS-13 | 合并进 CS-10 | Body 单一权威、差分测试与重复 pass 退休属于同一问题 |
| CS-14 | 降级为 OBS-02 | 只有实际上游冲突证明净收益后才逐文件立项 |
| CS-15 | 移出主线 | WS ordinal 与代理取消是两个独立工程问题，不属于 official egress 收敛关键路径 |

### 3.2 退休门禁跟踪模板

CS-10、CS-11 或任何触发后的观察项只要涉及旧路径删除，都必须逐项填写以下门禁；“不适用”
也要记录理由，不能留空后直接标记完成。

| 门禁 | 状态 | 证据路径 |
|---|---|---|
| 生产消费者与调用图完整 | 待验证 | 待补充 |
| 替代路径和旧入口 fail-close 完成 | 待验证 | 待补充 |
| Active/Previous、HTTP/WS、fallback、辅助端点矩阵通过 | 待验证 | 待补充 |
| 旧类型、接线、scanner 分类和专属测试完成退休 | 待验证 | 待补充 |
| 源码绝迹与 mutation 负例门禁通过 | 待验证 | 待补充 |
| 同一画像下空 wire 允许列表通过 | 待验证 | 待补充 |
| 机器退休收据与真实部署/回滚证据归档 | 待验证 | 待补充 |

只要 Previous、API Key mimic、非 Codex persona、DI/生成接线或业务读取面仍依赖旧能力，对应
退休门禁即为失败；可以完成兼容实现，但不得删除旧路径。

---

## CS-01：Compiler 静态 URL 封闭

**实施收益：**完成后，静态官方端点的 scheme、authority、端口、path 和 query 都由画像最终
约束，即使未来新增调用方漏做 service 前置校验，Compiler 也不会为偏离画像的 URL 签发有效
Token，从而补齐最终权威边界并降低回归风险。

**状态：**待启动  
**优先级：**高  
**依赖：**无

### 实施范围

- 明确静态端点 URL 是“由画像生成”还是“调用方 URL 必须精确等于画像 URL”。
- 在 Compiler 内校验 scheme、hostname、显式端口、userinfo、fragment、固定 path 和固定 query。
- 保留 ReturnedURL 动态端点的独立校验模型，不把它错误收紧成静态 URL。
- Guard 继续独立复核签发后的请求未发生修改。

### 非目标

- 不把该问题描述成当前可从公网利用的 P1 漏洞。
- 不修改 OAuth custom base URL 或第三方 API Key 的产品语义。
- 不调整 Header、Body 或 TLS 画像。

### 验收标准

- `http` 降级、非画像端口、额外或改写 query、userinfo、fragment 均被拒绝。
- 合法静态端点和合法 ReturnedURL 全部通过。
- 被拒绝请求不签发 Token，也不调用 adapter。
- active/previous final-wire 基线无变化。

### 跟踪记录

- 方案：待补充
- 实现：待补充
- 测试与证据：待补充
- 完成日期：待补充

---

## CS-02：CompilerRejected 的 attempt/fallback 契约与测试

**实施收益：**完成后，Compiler 失败是否消费 ordinal、预算和 fallback transition 将有唯一、可测试
的语义，避免后续维护者用简单回滚破坏并发序号、一次性认证或 service/core 状态同步，也为是否
需要两阶段 reservation 提供可靠决策依据。

**状态：**待启动  
**优先级：**高  
**依赖：**无

### 实施范围

- 明确 attempt 从 reservation、Token 签发还是 adapter 发送中的哪个时点开始。
- 明确 CompilerRejected 后 invocation 是可重试、已切换到 fallback，还是进入 terminal failed。
- 增加四类测试：
  1. Compiler 失败后的 attempt 预算；
  2. fallback Compile 失败后的 sink、transition 和后续调用行为；
  3. 并发 reservation 的 ordinal 唯一性和状态一致性；
  4. 一次性 Authentication 的消费与失败语义。
- 覆盖 core、HTTP wrapper、WS wrapper 和 OpenAI Forward wrapper 的计数一致性。

### 非目标

- 本变更集不实施 commit/abort 状态机。
- 禁止简单执行 `attempts--`、恢复 `pendingFallback` 或复用旧 ordinal。
- 不在未定义认证与并发语义前引入 reservation token。

### 验收标准

- 规格、API 注释和测试对 CompilerRejected 的语义一致。
- 若契约选择可重试，测试证明合法 fallback 目标可按规定的 reason 和新认证继续使用；若契约
  选择 terminal failed，测试证明任何后续 attempt 都稳定 fail-close。
- service/core ordinal 在所有失败出口保持一致，或 invocation 明确终止。
- 若最终决定需要状态机，另建独立变更集并重新审核方案。

### 跟踪记录

- 契约决策：待补充
- 测试：待补充
- 是否需要后续实现：待补充
- 完成日期：待补充

---

## CS-03：版本中立架构 ADR

**实施收益：**完成后，薄网关、纯出口拦截、完整分流和 Sidecar 等方案的取舍会形成稳定决策
记录，后续 Codex 换版或上游合并时不必重新争论基础架构，也能避免在多个重构中造出短命 DTO
和重复边界。

**状态：**待启动  
**优先级：**高  
**依赖：**无

### 实施范围

- 记录目标、约束、候选方案、选择理由、已知后果和重审触发条件。
- 明确长期形态为进程内薄 Gateway、单一 Compiler 最终权威和最少上游 Hook。
- 明确业务归一化、persona 投影和最终 wire 编译的所有权。
- 明确现有 `ReleaseBundle` 是 invocation 冻结能力；除非证明不足，不新增平行 `ReleaseHandle` DTO。
- 明确 Endpoint、Transport 和 Lite 属于 attempt/fallback 事实，不在入口永久冻结。

### 非目标

- 不复制完整 wire 规格。
- 不在 ADR 中直接承诺删除仍有消费者的兼容层。
- 不把 Sidecar 永久排除；只记录重新评估条件。

### 验收标准

- ADR 覆盖至少四类候选方案及其权衡。
- CS-10、CS-11 以及触发后另行立项的观察项可引用同一所有权定义。
- ADR 与当前生产依赖方向和 §4.9 退休约束一致。

### 跟踪记录

- ADR 路径：待补充
- 审核结论：待补充
- 完成日期：待补充

---

## CS-04：上游入侵面 ratchet

**实施收益：**完成后，“小侵入”将从主观目标变成可持续执行的机器门禁；每次上游合并或新增
功能都能看到冲突面是否恶化，并阻止无审批增加上游文件改动、替换型 diff 和高 churn 接缝。

**状态：**待启动  
**优先级：**高  
**依赖：**无

### 实施范围

- 以机器 ledger 为事实源建立当前基线。
- 分别统计：上游已有文件数、Fork 自有文件数、增删行、替换率、churn 加权值、直接 import、
  共享 Hook 数量和双标签文件。
- 明确单标签、双标签、联集和 frontend/backend 的统计口径。
- 在工具和 schema 中明确 churn 窗口、替换率分母、Hook 定义、阈值与例外格式。
- CI 默认禁止指标恶化；只有提交经过审核的机器例外资产后才允许增长，指标下降时自动收紧新基线。
- 为确有必要的增长记录理由、所有者、期限和后续收敛计划。

### 非目标

- 不用单一 LOC 上限代替风险判断。
- 不把 Fork 自有文件行数直接等同为上游 Git 冲突成本。
- 不手工维护可由工具确定性生成的计数。

### 验收标准

- 当前 86 个 overlay（56 个上游已有文件、30 个 Fork 文件）、双标签文件和各 scope 可以无歧义复算。
- 任一指标恶化且没有有效例外资产时，门禁失败并输出具体文件和差值。
- 历史 ledger 不原位改写，新基线通过 transition/receipt 追加。
- 后续任何以“降低上游侵入”为理由的重构，都必须证明指标保持不变或下降。

### 跟踪记录

- 基线资产：待补充
- CI 门禁：待补充
- 初始指标：待补充
- 完成日期：待补充

---

## CS-05：Campaign/candidate 边界修订

**实施收益：**完成后，同一目标 Codex 版本下迭代 Sub2API 源码、镜像或构建时无需重复官方
客户端取证，同时仍能保证不同 Codex 二进制和画像不会复用旧验收结论，从而显著降低升级流程
摩擦且不削弱证据可信度。

**状态：**待启动  
**优先级：**高  
**依赖：**无

### 实施范围

- 新目标 Codex 版本、二进制、依赖事实或目标画像变化时新建 Campaign。
- 同一目标事实下，Sub2API 源码、候选镜像和构建 ID 变化使用新的 `candidate-id`。
- 明确 candidate 各自绑定源码、镜像、构建 ID、测试结果和最终身份。
- 修订规格 §4.1、升级工具帮助文本和相关 schema/门禁说明。
- 明确任何官方二进制变化都不得通过“patch 快速通道”复用旧 Campaign 结论。

### 非目标

- 不降低 Campaign `ready`、seal 或最终镜像身份要求。
- 不允许不同目标画像在同一 Campaign 中混用。
- 不删除历史 Campaign 或 candidate。

### 验收标准

- 文档与 `codex_upgrade.py --candidate-id` 的真实能力一致。
- 至少一个夹具证明同 Campaign 可以追加不同 Sub2API candidate。
- 至少一个负例证明目标 Codex 事实变化必须新建 Campaign。
- 现有已接受证据保持不可变。

### 跟踪记录

- 规格变更：待补充
- 工具/Schema 变更：待补充
- 测试：待补充
- 完成日期：待补充

---

## CS-06：Makefile 与工具链版本参数化

**实施收益：**完成后，新增 Codex 画像时可以通过 Campaign/Release 坐标驱动构建和验证，不再
需要在 Makefile 与脚本中机械替换 `0.145.0` 路径，减少漏改、误用默认版本和升级时的重复劳动。

**状态：**待启动  
**优先级：**中  
**依赖：**CS-05

### 实施范围

- 盘点 Makefile、升级脚本、默认清单和测试入口中的行为相关版本常量。
- 使用显式 Campaign、Release、version 和 digest 参数替代隐式固定路径。
- 缺少必要坐标时 fail-close，不回退到 `0.145.0`。
- 保留历史证据路径和版本化夹具，不机械改写注释、错误文案或冻结资产。
- 为常用命令提供可发现的帮助和示例。

### 非目标

- 不追求仓库内 `0.145.0` 文本命中归零。
- 不移动历史画像和证据目录。
- 不在本变更集中加入新 Codex 画像。

### 验收标准

- 工具可以分别对两个显式坐标夹具运行；真实第二版本由 CS-07 验收。
- 未传版本或 digest 时不存在隐式 active/0.145 fallback。
- 当前 0.145.0 工作流结果保持不变。
- 版本泄漏门禁禁止新增行为相关硬编码。

### 跟踪记录

- 参数清单：待补充
- 迁移命令：待补充
- 测试：待补充
- 完成日期：待补充

---

## CS-07：真实异画像 active/previous 演练

**实施收益：**完成后，active/previous 将真正证明不同 Codex 版本和不同画像摘要可以并存、
切换和回滚，而不只是同一 0.145.0 画像的两个发布节点；这会把“易换版”从设计承诺提升为生产
级证据。

**状态：**待启动  
**优先级：**高  
**依赖：**CS-05、CS-06

### 实施范围

- 准备 version、profile digest 和 release digest 均不同的 Active/Previous。
- 覆盖 Responses HTTP/WS、alpha、images、models、quota/WHAM、live、files 和 account-test。
- 覆盖 Body omit、Header、TLS、transport、连接生命周期和 fallback。
- 明确执行器兼容门禁：旧引擎必须拒绝无法理解的 schema、枚举或必需能力；是否增加显式
  `engine_api_version`/`required_capabilities` 由该变更集方案单独决定。
- 证明在途 invocation 保持旧 `ReleaseBundle`，新 invocation 使用新 Active。
- 证明 fallback 不跨 Bundle，WS pool 按 profile/pool digest 隔离。
- 执行 canary、切换、回滚和恢复后的 final-wire 验证。

### 非目标

- 合成异画像单元测试不能替代真实 Campaign 和回滚演练。
- 不覆盖或删除 Previous 画像。
- 不在证据不完整时把新画像设为 Active。

### 验收标准

- 两个真实画像均通过各自 Campaign full 门禁。
- Active→Previous→Active 切换不混搭版本事实或连接池。
- 两个画像分别与各自 Campaign 证据一致；同一画像切换前后使用空允许列表，不要求不同版本
  之间不存在画像声明的合法 wire 差异。
- 回滚不需要修改业务代码或手工替换固定版本常量。

### 跟踪记录

- Active 坐标：待补充
- Previous 坐标：待补充
- Campaign 与证据：待补充
- 外部候选版本/证据阻塞：待补充
- 回滚记录：待补充
- 完成日期：待补充

---

## CS-09：alpha-search mode/version 单一权威

**实施收益：**完成后，alpha-search 不再携带与真实 ReleaseBundle 无关的固定 `0.145.0` 死字段，
未来 Active/Previous 指向不同版本时不会形成误导性上下文或重新接入旧 resolver 后的定时故障。

**状态：**待启动  
**优先级：**低  
**依赖：**CS-07 为最终验证门禁

### 实施范围

- 删除无消费者的固定 `ProfileVersion`，或从冻结 ReleaseBundle 唯一派生。
- 保证 mode、version、endpoint 和 URL 都来自同一发布能力。
- 增加不同 Active/Previous 版本下的 alpha-search 测试。
- 更新误导性的 0.145 命名、注释或状态输出时，仅处理行为相关位置。

### 非目标

- 不把当前问题重新定级为跨版本立即失败的 P1。
- 不改 alpha-search 产品语义和 PAT Responses fallback。
- 不机械删除所有 `0.145.0` 文本。

### 验收标准

- alpha 生产路径不再同时持有 mode 与独立固定 version 权威。
- 两个不同版本夹具的 release mode 都能选择各自 endpoint/profile，并在 CS-07 中以真实画像复验。
- API Key 与 PAT 路径行为不变。

### 跟踪记录

- 旧字段消费者审计：待补充
- 实现：待补充
- 测试：待补充
- 完成日期：待补充

---

## CS-10：Body 差分测试与单一权威门禁

**实施收益：**完成后，可以用测试明确 service 与 core 两套 Body 处理在哪些输入上等价、在哪些
输入上分歧，防止新画像把当前潜在差异带入生产；只有证据证明重复 pass 确实可达且有维护成本时，
才进入生产收敛，避免为了减少一次解析而重构稳定链路。

**状态：**待启动  
**优先级：**中  
**依赖：**CS-03；CS-07 为旧路径退休门禁

### 当前实施范围

- 为 core/service 当前 Body 算法建立特征与差分测试，不先修改生产行为。
- 覆盖 `previous_response_id` 非 null 且 `PreviousResponseIDReusable=false`、全部已支持
  `OmitWhen`、null/empty、未知枚举和条件字段。
- 覆盖 Responses HTTP/WS/compact、alpha 和 images，明确哪些路径串联两套 pass、哪些路径互斥。
- 记录每条生产路径上的业务归一化、persona 投影、验证、omit、排序、JSON 解析与重编码次数。
- 建立门禁：画像新增 Body 条件或枚举时，必须先通过上述差分矩阵。

### 条件触发的后续收敛

只有测试、新画像或真实故障证明语义差异进入可达路径，或者重复 pass 已形成可量化维护成本时，
才另提独立方案，逐路径让 core 成为最终 Body 权威并退休对应 service pass。该方案必须保留数字、
原始 JSON、字段相对顺序和 single-use/replayable Body 语义，并执行 final-wire、性能与 §4.9 门禁。

### 非目标

- 不把 core switch 复制到 service 来“对齐”第二套实现。
- 不预先承诺删除 service validate/order pass。
- 不把所有未知业务字段一律 fail-close。
- 不在本变更集中重构 Header、TLS 或业务协议转换。

### 验收标准

- 差分矩阵可以稳定复现已知 `none_or_unreusable_prefix` 语义差异，并标明其当前可达性。
- Responses HTTP/WS/compact、alpha 和 images 的正负例全部通过。
- 新画像增加 Body 条件或枚举但未补相应测试时，门禁失败。
- 如未满足后续收敛触发条件，本变更集以“测试与门禁完成、生产不改动”结束。

### 跟踪记录

- 差分矩阵：待补充
- Body pass 调用图：待补充
- 触发生产收敛：否；待证据更新
- 若退休旧路径，收据：待补充
- 完成日期：待补充

---

## CS-11：service DTO 字段覆盖与条件退休

**实施收益：**完成后，新增 core 画像字段若未进入 service 投影会被测试立即发现，消除静默漏字段
这一真实升级风险；同时保留仍被 Previous、API Key mimic 或其他路径使用的 DTO，不为追求结构整洁
提前发动大规模迁移。

**状态：**待启动  
**优先级：**中  
**依赖：**CS-10；CS-07 为退休门禁

### 当前实施范围

- 盘点 DTO、projection、clone 和 facade 的全部生产消费者，并按 OAuth、Previous、API Key mimic、
  非 Codex persona 与辅助端点分类。
- 记录 DTO 定义、projection 与 clone 的精确行数和字段对应关系，避免混用函数与整体口径。
- 增加字段覆盖或逐叶变异测试：core 新增会影响执行的字段但 projection 未处理时，CI 必须失败。
- 将现有同源 digest 比较明确为来源一致性检查；不得把它宣称为字段完整性门禁。若会误导维护者，
  可在本变更集专项方案中删除或更名，但不得改变 wire 行为。

### 条件退休规则

本清单不设独立“CS-11B 迁移工程”。只有生产消费者自然收敛，并证明 Previous、API Key mimic、
非 Codex persona、DI/生成接线和业务读取面均不再依赖旧模型时，才按 §4.9 提交退休方案。届时再
根据实际消费者选择窄只读 accessor、代码生成或直接删除；不得提前预设技术方案。

### 非目标

- 不以“约 300 行重复”为理由立即删除 DTO、projection、clone 或 facade。
- 不预先建设 accessor/codegen 基础设施。
- 不直接比较不同 JSON 结构的 DTO digest 与 `ExecutableProfileDigest`。
- 不让 core 反向依赖 service。

### 验收标准

- 字段覆盖门禁能够捕获漏投影，且逐叶变异测试证明不会静默遗漏。
- 消费者清单能解释旧 DTO 当前为何保留，并标出真实退休阻塞项。
- API Key、Compatible、OAuth 和辅助端点行为不变。
- 若退休条件不成立，本变更集以“字段门禁完成、旧 DTO 保留”结束；若成立，则完整执行 §4.9。

### 跟踪记录

- 消费者清单：待补充
- 字段覆盖门禁：待补充
- 退休条件是否成立：否；待证据更新
- 退休方案与收据：待补充
- 完成日期：待补充

---

## 4. 触发式观察项

观察项不是待实施变更集，不分配负责人和目标日期，也不得仅凭“代码看起来重复”转为实施任务。
只有满足下述触发条件并提交新的原子方案后，才能进入主进度表。

### OBS-01：Forward 重复解析与接触点

**潜在收益：**如果真实证据表明 Forward 的重复解析持续制造故障或上游合并冲突，局部收敛可以
减少热点接触点；在证据出现前保持现状，则能避免引入新的 decision DTO、生命周期边界和回归面。

**当前结论：**仅观察，不安排重构。当前重复解析尚未证明造成故障、wire 漂移或显著合并成本。

**触发条件：**满足以下任一条件后再立项：

- CS-04 ratchet 或连续的真实上游合并证明 `openai_gateway_forward.go` 是主要冲突成本；
- 同一 invocation 的重复解析出现可复现的不一致或正确性故障；
- 能以实际调用图证明局部修改会净减少上游接触点，且不冻结 Endpoint、Transport、Lite 或
  fallback 等 attempt 事实。

触发后只针对已证明的热点提交原子方案，不预设 `officialEgressDecision` 或其他平行 DTO。

### OBS-02：account-test、images、quota 薄 Hook 可行性

**潜在收益：**若某个热点已被真实上游合并证明会反复产生高成本冲突，逐文件薄 Hook 化可能
降低替换型 diff；保留逐文件 no-go 结论，可以避免为了 LOC 指标复制上游 pipeline 或重写稳定实现。

**当前结论：**仅观察，三个文件均不安排迁移，也不得打包成一个重构项目。

**触发条件：**每个文件独立满足以下全部条件后，才可单独立项：

- 至少一次真实上游合并提供可复算的冲突时间、替换单元或 churn 证据；
- 可行性分析证明能保留上游 failover、计费、模型映射、SSE 和错误处理，不复制业务 pipeline；
- 专项方案预先冻结 ratchet 目标，并证明预期净收益高于迁移和回归成本。

任一条件不成立即记录 no-go，不把“薄 Hook”当作预定架构终点；若实际退休旧兼容路径，仍须
执行 §4.9。

---

## 5. 独立 Backlog：原 CS-15

### WS ordinal 与代理取消

**实施收益：**完成后，WS 帧序号的真实语义会被明确，未来并发写入不会依赖隐含假设；代理
建连也能及时响应 context 取消，减少卡死请求和资源占用。两项工作独立治理，不会污染伪装架构
主线或错误计入 Fork 入侵面。

**状态：**待启动  
**优先级：**分项定级  
**依赖：**无；不在主收敛关键路径

### 子任务 CS-15A：WS ordinal 语义

**实施收益：**明确 ordinal 是诊断信息还是发送约束，避免未来并发调用在没有 cancel/discard
协议时引入死锁或错误排序。  
**状态：**待启动  
**优先级：**低

- 先定义 ordinal 是诊断序号还是强制发送顺序。
- 盘点生产消费者、单写者、底层写锁以及 sideband/close 并发窗口。
- 若要求严格顺序，必须同时设计 prepared frame 的 cancel/discard，避免前帧放弃后永久阻塞。
- 在契约未确定前不直接增加“下一个可写 ordinal”检查。

### 子任务 CS-15B：代理 context 取消

**实施收益：**让代理 TCP、认证和 CONNECT 卡住时能够响应请求取消，减少长时间占用 goroutine、
连接和请求配额。  
**状态：**待启动  
**优先级：**中，按独立上游 P2 问题跟踪

- 作为独立上游 issue/patch 处理，不计入 official egress Fork 设计缺陷。
- SOCKS5 优先使用 `proxy.ContextDialer.DialContext`。
- 另行评估 HTTP/HTTPS proxy CONNECT 写入、响应读取和取消关闭连接；若需要修改，建立独立
  后续子任务，不与 SOCKS5 patch 混合。
- 补充取消、deadline、认证卡住和连接清理测试。

### 非目标

- 不把 WS ordinal 定级为当前已证明的生产 P2 故障。
- 不在 official egress 收敛提交中夹带上游代理修复。
- 不用裸 `accounts.Delete` 等方式处理无关状态表问题。

### 验收标准

- CS-15A 有明确序号契约及覆盖并发/cancel/discard 的测试，或明确记录保持诊断语义。
- CS-15B 达到专项方案预先冻结的取消时限，并通过连接/goroutine 清理测试。
- 两个子任务拥有独立提交、测试和回滚单元。

### 跟踪记录

- CS-15A：待补充
- CS-15B：待补充
- 完成日期：待补充

---

## 6. 每次更新本文的要求

每次推进一个变更集时，只更新对应状态和跟踪记录；观察项只有在新增触发证据或形成独立方案时
才更新。记录至少附上：

1. 已确认的方案或 ADR 链接；
2. 代码、文档和机器资产路径；
3. 测试命令与结果摘要；
4. final-wire、ratchet、Campaign 或 canary 证据；
5. 未解决风险和后续依赖；
6. 若有退休操作，附 §4.9 收据路径和摘要。

任何变更集不得仅凭“测试通过”标记完成；必须同时证明没有扩大上游冲突面、没有破坏
active/previous 回滚，并且没有将仍在使用的兼容能力提前退休。
