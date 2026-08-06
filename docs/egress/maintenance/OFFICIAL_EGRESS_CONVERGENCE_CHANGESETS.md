# Official Egress 当前必要变更与换版验收清单

> 文档状态：范围裁剪已确认；两个变更集的具体实施方案尚未确认
> 最后更新：2026-08-06
> 当前代码基线：`58d0827c3`
> 适用范围：Sub2API 上游小侵入、Codex CLI 画像升级与 official egress 兼容层

## 1. 文档目的

本文只跟踪当前已有明确证据、值得单独实施的小变更（当前为两个），并记录下一次 Codex CLI 换版必须
通过的验收门禁。换版门禁不是当前开发任务，不分配负责人、排期或预先重构稳定代码。

全部工作遵守以下原则：

1. 一次只实施一个变更集；编码前单独提交方案并取得明确确认。
2. 优先修改 Fork 自有文件；触碰上游已有文件时只保留必要的薄接缝。
3. 不以减少行数、类型或解析次数为目标，只处理已证明的正确性、升级或维护问题。
4. 当前 wire 没有错误时，不因结构看起来重复而修改生产路径。
5. Previous 仍依赖的兼容能力不得提前退休。

## 2. 当前变更集

| 序 | ID | 变更集 | 优先级 | 依赖 | 状态 |
|---:|---|---|---:|---|---|
| 1 | CHG-03 | alpha-search 版本单一权威 | 中 | 无 | 待启动 |
| 2 | CHG-01 | Compiler 静态 URL 封闭 | 高 | 无 | 待启动 |

执行顺序为 CHG-03 → CHG-01，与优先级次序不同：CHG-01 优先级高是因为它关系最终权威边界，
但它当前不可从公网利用，且回归面覆盖全部静态端点，需要独立的验证窗口；CHG-03 影响面限于
单端点，先做可以在低风险改动上先跑通流程。两项都必须在下一次 Codex CLI 换版之前完成——
换版后 Active/Previous 变为异版本，两项的验证成本都会翻倍。

已完成的变更集从本表和正文中移除，实施记录以 git 历史为准。

“已完成”至少要求：

- 实施范围、非目标和验收标准全部满足；
- 相关单元、集成或 final-wire 测试通过；
- 没有扩大未授权的上游修改面；
- 涉及旧路径删除时，完整执行规格 §4.9 的退休流程。

---

## CHG-03：alpha-search 版本单一权威

**实施收益：**完成后，alpha-search 的出站 target 与 Executor 编译的 header、body、TLS 和执行
release 都来自同一个已冻结 ReleaseBundle，不会在未来 Active/Previous 使用不同 Codex CLI 版本时
出现「请求 URL 来自全局 catalog、bundle 来自 runtime catalog」的分叉。该调整只消除一个明确的
版本权威分叉，不改动 alpha-search 业务语义。

**状态：**待启动
**优先级：**中
**依据：**OAuth alpha-search 已按 mode 解析 runtime state 和 endpoint，但创建
`OfficialEgressContext` 时仍提交固定 `officialCodexProfileVersion`，且 endpoint 与 URL 各查一次
全局 catalog，与 Executor 持有的 bundle 之间只有 mode 字符串这一层弱等价。

### 实施范围

- 删除 alpha-search Executor 路径中无消费者的固定 `ProfileVersion`；若前置判定证明该兼容字段
  仍有消费者，则改为从已冻结 ReleaseBundle 唯一派生。
- 保证 mode、endpoint、URL 与 Executor 执行来自同一个 bundle 实例。
- 增加 Active/Previous 版本不同的 alpha-search 夹具测试；合成画像必须内部一致，不得只改顶层
  `Version`（其余行为相关版本坐标不同步会形成假绿）。
- 只处理 alpha-search 生产路径中的行为相关固定版本。

### 非目标

- 不将当前同版本 Active/Previous 下的潜在问题定级为生产故障。
- 不改 alpha-search、PAT Responses fallback 或 API Key 的产品语义。
- 不机械删除历史证据、画像和测试夹具中的 `0.145.0`。
- 不重构 alpha-search 的 ingress runtime 绑定与 body 预投影（二者仍按冻结 mode 消费全局
  catalog，与出站 bundle 无关）。

### 验收标准

- alpha-search 生产路径不再同时持有 mode 与独立固定 version 权威。
- 两个不同版本夹具可以分别解析自己的 endpoint/profile，且合成画像通过内部一致性自检。
- 异版本断言绑定到执行期 bundle 编译出的 wire 事实（`version` header、User-Agent、TLS 摘要），
  而非 `bundle.Version()` 这一个标量。
- 当前 0.145.0 OAuth、API Key 和 PAT 行为不变，final-wire 空允许列表零差异。

### 跟踪记录

- 方案：[CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md](CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md)（待审核）
- 实现：待补充
- 测试：待补充
- 完成日期：待补充

---

## CHG-01：Compiler 静态 URL 封闭

**实施收益：**完成后，Compiler 不会为 scheme、端口、userinfo、fragment 或 query 偏离画像的
静态 URL 签发有效执行结果。即使未来新增调用方漏做 service 前置校验，最终权威边界仍能拒绝
错误目标。

**状态：**待启动
**优先级：**高
**依据：**当前 `validateCompilerTarget` 对静态 endpoint 直接复制调用方 URL，路由解析只匹配
method、hostname、path 和 protocol，未完整封闭静态 URL。

### 实施范围

- 在 Compiler 内从画像生成静态 URL，或精确验证调用方 URL 与画像契约一致。
- 校验 scheme、hostname、显式端口、userinfo、fragment、固定 path 和固定 query。
- 保留 ReturnedURL 动态端点的独立验证模型。
- Guard 继续独立复核签发后的请求没有被修改。

### 非目标

- 不将该问题描述为当前已可从公网利用的漏洞。
- 不修改 OAuth custom base URL 或第三方 API Key 的产品语义。
- 不调整 Header、Body、TLS 或业务路由。

### 验收标准

- `http`/`ws` 降级、非画像端口、额外或改写 query、userinfo 和 fragment 均被拒绝。
- 合法静态端点以及合法 ReturnedURL 全部通过。
- 被拒绝请求不签发执行结果，也不调用 adapter。
- Active/Previous 的合法 final-wire 基线不变。

### 跟踪记录

- 方案：待补充
- 实现：待补充
- 测试与证据：待补充
- 完成日期：待补充

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
