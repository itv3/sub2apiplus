# Official Egress 当前必要变更与换版验收清单

> 文档状态：当前无待实施变更集；CHG-03 经审核撤销（墓碑见 §2.2）
> 最后更新：2026-08-06
> 最近验收的 production-code 基线：`ffdf43f30`
> 适用范围：Sub2API 上游小侵入、Codex CLI 画像升级与 official egress 兼容层

## 1. 文档目的

本文只跟踪当前已有明确证据、值得单独实施的小变更（当前为空），并记录下一次 Codex CLI 换版必须
通过的验收门禁。换版门禁不是当前开发任务，不分配负责人、排期或预先重构稳定代码。

全部工作遵守以下原则：

1. 一次只实施一个变更集；编码前单独提交方案并取得明确确认。
2. 优先修改 Fork 自有文件；触碰上游已有文件时只保留必要的薄接缝。
3. 不以减少行数、类型或解析次数为目标，只处理已证明的正确性、升级或维护问题。
4. 当前 wire 没有错误时，不因结构看起来重复而修改生产路径。
5. Previous 仍依赖的兼容能力不得提前退休。

本清单由原 15 项调研清单（git `600775f89`）经 2026-08-06 范围裁剪（git `01dbf1edf`）演化
而来，被裁剪条目的处置以该两个提交为准。

## 2. 当前变更集

当前无待实施变更集。

已完成的变更集从本表和正文中移除，实施记录以 git 历史为准。

“已完成”至少要求：

- 实施范围、非目标和验收标准全部满足；
- 相关单元、集成或 final-wire 测试通过；
- 没有扩大未授权的上游修改面；
- 涉及旧路径删除时，完整执行
  [docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md](../../CODEX_CLI_CLIENT_EMULATION_GUIDE.md) §5.2
  兼容代码退休流程。

### 2.1 非阻塞遗留判定

以下事项有明确证据但不构成当前待实施变更集；在触发条件出现前只跟踪，不启动修复。

| 项目 | 当前状态 | 触发条件 |
|---|---|---|
| alpha-search 固定 `ProfileVersion` 传参 | 待判定；定性为疑似无效兼容残留，不是正确性修复。四个 accessor 的生产消费者仍存在 | 下一真实版本换版时按 [CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md](CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md) 的遗留判定流程执行 mutation 判定与退休决策 |
| changeset3 历史生成入口与旧 approved-delta 契约漂移 | 不可复现的是旧生成入口（`TestGenerateChangeset3ProductionFinalWire`、`TestGenerateChangeset3ExactApprovedDeltas`）及其与旧 approved delta 的比较契约（2026-08-06 实跑确认：首个 capture 六个 digest 不符）；changeset6 生成器仍复用共享的 `changeset3BuildProductionFinalWireCaptures` 且可复现，是当前通用 56 面 final-wire authority，但不替代 §3.2 要求的 alpha-search 真实 service 链证据 | 重新引用旧生成入口前必须先修复；若决定删除旧入口或旧 approved-delta 契约，使用专用 gate/evidence retirement receipt（先例：`docs/egress/consolidation/pairing-gate-retirement.json`），并证明 changeset6 仍依赖的共享 capture builder 保持完整；规格 §5.2 面向生产兼容层，不适用于纯历史测试生成器。在此之前不为让历史工具变绿而修改代码或重建历史证据 |

### 2.2 CHG-03：alpha-search 版本单一权威（已撤销 · 墓碑）

- **撤销日期：**2026-08-06（基线 `58d0827c3`，三方审核 `changes_requested`，复核逐条属实）。
- **核心原因：**目标解析与 bundle 解析同源于进程级不可变单例 catalog，无生产可达分叉链；
  原方案需先制造生产不存在的条件才能证明修复必要，论证倒置。
- **完整撤销记录：**[CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md](CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md)
  （含全部撤销依据与遗留判定项）。
- **遗留判定项：**见 §2.1 与 §3.2 降级检查项。
- **复活条件：**出现真实 mixed-version wire、生产 catalog 可分叉路径，或 §3.2 实证检查失败时，
  按当时事实重新立项最小修复。

## 3. 下一次 Codex CLI 换版验收门禁

本节不是当前待开发列表。只有出现第二个真实 Codex CLI 版本及其目标画像时才触发；每项先用
新版本事实验证是否存在问题，只有门禁失败时才提出最小修复方案。

合成异版本画像只允许用于工具链预检、mutation 和门禁判据自测；正式换版验收必须使用两个
真实 Codex CLI 版本对应的完整画像与发布坐标。

| 门禁 | 触发条件 | 默认动作 |
|---|---|---|
| 工具链版本坐标 | 准备第二个真实画像 | 先运行现有工具；仅修复阻断新坐标的行为硬编码 |
| 异画像 Active/Previous | 新版本 Campaign 已达到可验收状态 | 执行切换和回滚演练，不预先改运行架构 |
| Compiler endpoint 封闭 | 准备第二个真实画像；endpoint、query 或值来源变化时，额外扩展结构与可信源负例 | 正例全量验证每次换版固定执行；语义未定义的 source fail-close |
| Body 语义差分 | 新画像增加或改变 Body 条件、枚举或字段 | 先补差分测试；只有可达差异才改生产路径 |
| service 投影字段覆盖 | 新画像增加 service 消费的执行字段 | 先证明投影完整；不以此启动 DTO 迁移工程 |

### 3.1 工具链版本坐标

**验收收益：**确保新画像可以通过显式 version、digest、Campaign 和 Release 坐标生成与校验，
避免构建工具静默回退到 0.145.0。

- 先使用第二版本运行 Makefile、dump、compare 和发布工具。
- 只修改实际阻断第二版本的行为相关硬编码。
- 历史画像、证据路径、注释和冻结夹具不要求文本归零。
- 缺少必要坐标时必须 fail-close。

### 3.2 异画像 Active/Previous

**验收收益：**证明不同 Codex CLI 版本、画像摘要和发布摘要可以并存、切换与回滚，而不是只对
同一 0.145.0 画像进行逻辑模拟。

- Active 和 Previous 必须具有不同 version、profile digest 和 release digest。
- 覆盖 ReleaseCatalog 中全部 Runtime Sink 与 endpoint ID；当前至少包括 Responses HTTP/WS、
  compact、alpha、images、models、OAuth refresh、quota/usage、live、files 和 admin-test。
  必须以机器断言收口：已验证 endpoint ID 集合 == 新旧 ReleaseCatalog endpoint ID 并集，
  防止未来新增端点因文档清单未更新而假绿。
- 证明在途 invocation 保持旧 Bundle，新 invocation 使用新 Active。
- 证明 fallback 不跨 Bundle，连接池按画像身份隔离。
- 完成 canary、切换、回滚和恢复后的 final-wire 验证。
- alpha-search direct 路径逐 mode 实证 target（URL、method、Host）与 invocation bundle 编译
  产物（`version` header、User-Agent、TLS 摘要、release/profile digest）同源
  （CHG-03 撤销后降级至此；实证失败才立最小修复项）。
- 上述实证的捕获链必须真实经过 `ForwardAlphaSearch → buildOpenAIAlphaSearchRequest →
  invocation.Execute`，且所用 final-wire 生成器在当前基线可复现；绕过 service 层的通用
  Executor capture 不得作为证据。
- 若使用合成异版本画像（用途限定见本节前言），必须整体替换全部行为相关版本坐标：当前每份正式 0.145.0 画像各有 34 处
  版本坐标（含 `version` header、User-Agent 与 `client_version` query），构造单个合成目标
  快照时必须替换对该快照机器扫描得到的全部命中，并在生成前后输出命中集合与集合摘要；
  「34」只是当前基线事实，不作为未来固定常量。`profilecontract` 不做跨字段一致性校验，
  只改顶层 `Version` 会假绿。

### 3.3 Compiler endpoint 封闭

**验收收益：**证明新画像全部端点仍处于 Compiler 静态 URL/query 封闭与 ReturnedURL 独立
验证模型之内，避免新增调用链绕过 `server_response` 可信值通道形成假绿。行为契约见
[docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md](../../CODEX_CLI_CLIENT_EMULATION_GUIDE.md) §3.3.2 与 §3.7；
[CHG-01_COMPILER_STATIC_URL_CLOSURE.md](CHG-01_COMPILER_STATIC_URL_CLOSURE.md) 只保留实施、测试与复审记录。

执行分层：正例全量验证（端点枚举、合法 URL 编译、静态/动态区分）在**每次真实换版固定执行**，
即使新画像 endpoint/query 定义与旧版完全相同；新增负例与生产可信链证明在 endpoint、query
或值来源发生变化时**增量执行**。

- 逐个枚举新旧 endpoint，区分静态 target 与 ReturnedURL 动态端点。
- Active/Previous 下所有合法静态 URL 均可编译。
- 新增或变化的 query source 必须有明确 Compiler 执行语义，否则 fail-close
  （当前有执行语义的闭集：`constant`、`server_response`）。
- 每个 `server_response` query 必须通过真实 service 生产链证明可信值来源与透传
  （如 realtime sideband 由 `dialLiveSideband` 提交 `record.CallID`）；通用 final-wire
  捕获器会为画像内 `server_response` query 自动合成 `ServerResponseQuery`，其通过不能
  单独作为证据。
- 覆盖缺失可信值、伪造可信值、画像外可信键，以及动态端点混入静态可信 map 的负例。
- URL tuple 至少覆盖 scheme、Host、显式端口、userinfo、fragment、EscapedPath、RawQuery
  和 ForceQuery。

### 3.4 Body 语义差分

**验收收益：**在新画像真正使用新 Body 条件前发现 service 与 core 的语义差异，避免为了消除
理论上的重复解析而提前重构稳定链路。

- 覆盖全部新旧 `OmitWhen`、条件字段、null/empty 和未知枚举。
- 覆盖 Responses HTTP/WS/compact，以及受新画像影响的辅助端点。
- 明确差异是否进入真实生产路径；不可达差异只记录测试，不修改生产代码。
- 只有可达差异或实际维护成本成立时，才另行确认单一权威收敛方案。

### 3.5 service 投影字段覆盖

**验收收益：**防止新画像字段在 service DTO/projection 中静默遗漏，同时避免在消费者仍存在时
提前删除兼容模型。

- 盘点新字段是否被 OAuth、Previous、API Key mimic、非 Codex persona 或辅助端点消费。
- 用字段覆盖或逐叶变异测试证明需要的字段均进入投影。
- 同源 digest 比较不能作为字段完整性证明。
- 若没有漏字段，保持现有 DTO；只有消费者归零时才按
  [docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md](../../CODEX_CLI_CLIENT_EMULATION_GUIDE.md) §5.2 讨论退休。

## 4. 更新与执行规则

每次只推进一个当前变更集。状态更新必须附上：

1. 已确认的实施方案；
2. 修改文件和机器资产；
3. 测试命令与结果；
4. final-wire 或 Campaign 证据；
5. 未解决风险和回滚方式。

换版门禁只在真实新版本出现后记录结果。门禁通过但无需修改代码时，记录证据即可，不得为了
形成提交而制造重构。
