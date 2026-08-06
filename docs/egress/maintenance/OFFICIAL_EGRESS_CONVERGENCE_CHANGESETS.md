# Official Egress 当前必要变更与换版验收清单

> 文档状态：当前无待实施变更集；CHG-03 经审核撤销（见撤销记录）
> 最后更新：2026-08-06
> 当前代码基线：`ffdf43f30`
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

## 2. 当前变更集

当前无待实施变更集。CHG-03 已于 2026-08-06 经审核撤销，见下方撤销记录。

已完成的变更集从本表和正文中移除，实施记录以 git 历史为准。

“已完成”至少要求：

- 实施范围、非目标和验收标准全部满足；
- 相关单元、集成或 final-wire 测试通过；
- 没有扩大未授权的上游修改面；
- 涉及旧路径删除时，完整执行规格 §4.9 的退休流程。

---

## CHG-03：alpha-search 版本单一权威（已撤销）

**撤销日期：**2026-08-06（基线 `58d0827c3`，三方审核 `changes_requested`，复核逐条属实）

**撤销依据要点：**

1. alpha-search 的 target 解析与 Executor 的 bundle 解析查询的是同一个 `sync.OnceValues`
   进程级不可变单例 `DefaultReleaseCatalog()`，生产 wiring 无第二 catalog 注入口，mode 在
   请求内冻结。「URL 来自全局 catalog、bundle 来自 runtime catalog 且可能分叉」无生产可达
   失败链；换版在现有架构下是同一 catalog 装两个版本、按 mode 各查各槽位，不形成混搭。
2. 原方案需先新增公共导出与 runtime catalog 参数化，才能在测试中制造生产不存在的分叉条件，
   再以此证明修复必要——论证倒置，且与本文原则 3/4 冲突。
3. 原方案 §6 引用的 changeset3 final-wire 门禁在当前基线不可复现（生成器实跑失败，六个
   digest 与旧 approved delta 不符）；且其 alpha-search capture 由测试自建 target 直进
   `invocation.Execute`，不经过 `ForwardAlphaSearch`/`buildOpenAIAlphaSearchRequest`，
   不能作为被改路径的回归证据。
4. 固定 `ProfileVersion` 传参当前证据只支持「疑似无效兼容残留」，不构成「版本权威正确性
   修复」的必要性；其判定与退休作为独立遗留项跟踪。

**后续处置：**

- alpha-search target 与执行 bundle 的同源性验证降级为换版门禁 §3.2 的实证检查项。
- 固定 `ProfileVersion` 残留判定与退休流程见
  [CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md](CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md)
  （已改写为撤销记录与遗留判定项）。
- changeset3 final-wire 生成器基线漂移是独立现存问题，单独排查，不随本撤销关闭。

**复活条件：**出现真实 mixed-version wire、生产 catalog 可分叉路径，或 §3.2 实证检查失败时，
按当时事实重新立项最小修复。

---

## 3. 下一次 Codex CLI 换版验收门禁

本节不是当前待开发列表。只有出现第二个真实 Codex CLI 版本及其目标画像时才触发；每项先用
新版本事实验证是否存在问题，只有门禁失败时才提出最小修复方案。

| 门禁 | 触发条件 | 默认动作 |
|---|---|---|
| 工具链版本坐标 | 准备第二个真实画像 | 先运行现有工具；仅修复阻断新坐标的行为硬编码 |
| 异画像 Active/Previous | 新版本 Campaign 已达到可验收状态 | 执行切换和回滚演练，不预先改运行架构 |
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
- 覆盖 Responses HTTP/WS、compact、alpha、images、models、quota、live、files 和 account-test。
- 证明在途 invocation 保持旧 Bundle，新 invocation 使用新 Active。
- 证明 fallback 不跨 Bundle，连接池按画像身份隔离。
- 完成 canary、切换、回滚和恢复后的 final-wire 验证。
- alpha-search direct 路径逐 mode 实证 target（URL、method、Host）与 invocation bundle 编译
  产物（`version` header、User-Agent、TLS 摘要、release/profile digest）同源
  （CHG-03 撤销后降级至此；实证失败才立最小修复项）。
- 上述实证的捕获链必须真实经过 `ForwardAlphaSearch → buildOpenAIAlphaSearchRequest →
  invocation.Execute`，且所用 final-wire 生成器在当前基线可复现；绕过 service 层的通用
  Executor capture 不得作为证据。
- 若使用合成异版本画像，必须整体替换全部行为相关版本坐标（正式画像共 34 处 `0.145.0`，
  含 `version` header、User-Agent 与 `client_version` query）并自检无残留；`profilecontract`
  不做跨字段一致性校验，只改顶层 `Version` 会假绿。

### 3.3 Body 语义差分

**验收收益：**在新画像真正使用新 Body 条件前发现 service 与 core 的语义差异，避免为了消除
理论上的重复解析而提前重构稳定链路。

- 覆盖全部新旧 `OmitWhen`、条件字段、null/empty 和未知枚举。
- 覆盖 Responses HTTP/WS/compact，以及受新画像影响的辅助端点。
- 明确差异是否进入真实生产路径；不可达差异只记录测试，不修改生产代码。
- 只有可达差异或实际维护成本成立时，才另行确认单一权威收敛方案。

### 3.4 service 投影字段覆盖

**验收收益：**防止新画像字段在 service DTO/projection 中静默遗漏，同时避免在消费者仍存在时
提前删除兼容模型。

- 盘点新字段是否被 OAuth、Previous、API Key mimic、非 Codex persona 或辅助端点消费。
- 用字段覆盖或逐叶变异测试证明需要的字段均进入投影。
- 同源 digest 比较不能作为字段完整性证明。
- 若没有漏字段，保持现有 DTO；只有消费者归零时才按 §4.9 讨论退休。

## 4. 更新与执行规则

每次只推进一个当前变更集。状态更新必须附上：

1. 已确认的实施方案；
2. 修改文件和机器资产；
3. 测试命令与结果；
4. final-wire 或 Campaign 证据；
5. 未解决风险和回滚方式。

换版门禁只在真实新版本出现后记录结果。门禁通过但无需修改代码时，记录证据即可，不得为了
形成提交而制造重构。
