# Codex CLI 0.145.0 → 0.147.0 Official Egress 升级计划（执行中）

> 状态：**k71 首次走完全链路**——官方采集 → classify → 画像 → 候选采集 → seal →
> compare → accept，是 k34 之后第一次。accept 实测候选侧 42 条规则、108 项 check，
> **35 pass / 7 fail**，7 条待解决见 §10.2。**Active 仍为 0.145，0.147 尚未替换。**
>
> 官方采集固定在 Vircs（Codex CLI 只在那里），候选采集固定在 DMIT（x86_64 测试机）。
> 进度与下一步见 §10.1，accept 待办清单见 §10.2，历轮死因与由此固化的不变量见 §10.8。
> 收据最低字段与候选机环境前置清单跨 Campaign 有效，在
> [`CAPTURE_OPERATIONS_NOTES.md`](CAPTURE_OPERATIONS_NOTES.md)。
>
> k34～k55 仅保留为历史诊断夹具，不迁移、不复用。
> 创建日期：2026-08-07 · 审阅基线 commit `abf236375f66aa096580092e646c4e33d37bb135`
> 基线画像 Codex CLI `0.145.0` → 目标画像 `0.147.0`

## 1. 目标与权威边界

本计划只回答“如何把 Sub2API 的 Official Egress 从 `0.145.0` 安全升级到
`0.147.0`”，不重复定义 wire 规则，也不充当 Campaign、Profile、Release 或验收事实源。

权威顺序：

1. [`CODEX_CLI_0145_EGRESS_SPEC.md`](../../CODEX_CLI_0145_EGRESS_SPEC.md) 第四部分；
2. [`OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md`](OFFICIAL_EGRESS_CONVERGENCE_CHANGESETS.md) §3；
3. [`CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md`](CHG-03_ALPHA_SEARCH_VERSION_AUTHORITY.md)；
4. 本计划。

冲突时停止实施并修订本计划，不得覆盖权威规格或历史证据。

升级不是原位覆盖：先追加完整 `0.147.0` Snapshot/Release，完成真实官方取证、候选对比
和机器验收；Campaign 到达 `ready` 后才切换为：

```text
Active   = 0.147.0
Previous = 0.145.0
```

`0.145.0` 的画像、规则、抓包、收据、测试和镜像必须保留为正式回滚点。

## 2. 已核实事实与待冻结坐标

### 2.1 已核实事实

| 项目 | 当前事实 |
|---|---|
| 官方版本 | `rust-v0.147.0` 已发布；目标 commit 为 `be6e8eac029b183056b7e4402879f15d2c85f61b` |
| 累计差异 | 必须覆盖 `0.146.0 + 0.147.0`，不能只看 0.147 release 摘要 |
| 当前画像 | Active/Previous 均为 `0.145.0`，共享 `e0b597…` Snapshot |
| 正式源码 | Vircs 已冻结 `formal-codex-cli-0.147.0/codex-rs`，commit `be6e8eac…`，源码树摘要 `1909e288…` |
| 本机 Codex | ChatGPT.app 内为 `0.147.0-alpha.6.5`，不得作为 stable 证据 |
| 工具身份 | P0 收口时受管 80 个 `.py/.sh/.json`，摘要 `fa8fc9fa…` |
| 基线回放 | 健康环境下通过：基线 180、补录 0、移除 22 |
| 当前门禁 | DOC-PRE/P0 与官方封存复核通过；`make check-egress-spec` 全绿 |

工具身份枚举必须递归扫描 `tools/official_client_capture/`，排除 `tests/`、`versions/`、
`__pycache__/`，并记录路径清单、文件数和集合摘要。

### 2.2 正式 `plan` 前必须冻结

- 0.147 tag/commit、源码树、`Cargo.lock` 和依赖摘要；
- Ubuntu 24.04 / x86_64 官方 stable asset，以及解压后真实 `codex` SHA-256；
- 不可变 runtime image `repository@sha256:<manifest-digest>`；
- official-only 的 0.145 baseline evidence root 及 inventory digest；
- 正式采集机、TLS 后端、模型、默认 feature/config、代理和恢复条件；
- CLI 登录态的非秘密身份、Sub2API OAuth/API Key 数据库 ID；
- Campaign ID/目录、candidate build/deploy/image 身份、canary 与回滚窗口。

SPEC 本次保留现有路径并原地演进；如需改名，另立 documentation transition。

## 3. 范围与禁止事项

本次范围：建立 0.145→0.147 独立 Campaign，完成官方/候选证据、完整 0.147 画像、
异版本共存、灰度切换、回滚和恢复后验收。

禁止事项：

- 不全局替换 `0.145.0 → 0.147.0`，不覆盖历史 JSON、证据和收据；
- 不把合成画像、Mac alpha 或通用 Executor capture 当正式证据；
- 不在 `ready` 前切 Active，不按入站版本选择画像；
- 不跨版本、candidate 或 attempt 复用抓包和结论；
- 不因一次未观察就删除规则，不手写“通过”结论；
- 不在抓包/部署中执行 `compose down/pull/prune` 或重建数据库、Redis、keeper、数据卷；
- 不在本变更集合并 Sub2API upstream，也不退休 Previous 仍依赖的兼容能力。

## 4. 单向执行顺序

一次只实施一个变更集：

| 阶段 | 必做动作 | 退出条件 |
|---|---|---|
| DOC-PRE | 登记并审核本文的后继 maintenance transition | 本文已合并，干净 HEAD 门禁通过 |
| P0 | 用现有工具做可丢弃预检 | 真实失败清单、分类和预检报告已审核 |
| 最小修复 | 逐个修复 P0 实证阻断 | 每项独立方案、测试和复验通过 |
| `planned` | 冻结版本、源码、二进制、镜像、平台、规则和场景 | 不可变 Campaign 建立 |
| `official_sealed` | 真实 0.147 CLI 取证、恢复、secret scan、seal | 官方证据不可变 |
| `profile_approved` | 逐规则分类并批准五份 0.147 清单 | `blocked=0`、联合摘要批准 |
| 建立画像 | 追加 Snapshot/Release 候选节点 | 可编译、可复算，Active 仍为 0.145 |
| `candidate_sealed` | 固定 candidate 身份并完成候选取证 | 候选证据不可变 |
| `compared` | 断网重哈希并逐规则双侧比较 | 无未登记漂移 |
| `ready` | 重放 accept、收据、安全和 evidence seal | 允许另行申请生产启用 |
| 启用 | canary、切换、回滚演练、恢复后 final-wire | Active=0.147、Previous=0.145 |

`ready` 不等于已部署、已备份或已完成生产回滚演练。

## 5. DOC-PRE 与 P0 预检

### 5.1 DOC-PRE

本文位于 maintenance 受管目录，必须先以独立变更集生成后继 transition。只允许新增
本文对应的确定性 entry，不得改写旧历史收据。合并后从干净 HEAD 开始 P0。

### 5.2 P0 边界

- 全部产物位于 `mktemp -d` 临时目录，并标记 `preflight-only`；
- 不发送真实请求，不执行任何带 `--acknowledge-live-requests` 的命令；
- 不创建正式 Campaign，不修改 Active/Previous、runtime catalog 或历史证据；
- 合成 0.147 只用于工具预检、mutation 和判据自测；
- P0 只记录问题，除预检报告外不修改仓库。

### 5.3 P0-A：基线和环境健康检查

先运行 `make test-capture-tools`、`make check-egress-spec`，记录 HEAD、工作区、平台、
命令、时间、退出码和原始错误。随后执行三组缓存 mutation：

1. 健康可读写 `GOCACHE`：证明真实基线结果；
2. 全新且完全不可写缓存：应在 `packages.Load` 顶层明确 fail-close；
3. 已存在但部分不可读缓存：验证 `go list -e` 返回 `p.Errors` 非空、
   `GoFiles/CompiledGoFiles=0` 时的 fallback、类型扫描和 route 输出。

每组记录：`go list` 退出码、package ID、原始错误、`p.Errors`、GoFiles、CompiledGoFiles、
Types/TypesInfo、fallback 文件集合、已加载包数、扫描器退出码和 route 输出。正常基线结论
只取第 1 组；第 2、3 组只用于判据 mutation。

### 5.4 P0-B：plan 加载预检

以 `baseline=0.145.0 / target=0.147.0` 调用
`tools/official_client_capture/codex_upgrade.py plan`，输入真实 baseline rules/scenarios、
临时 0.147 源码、解压后二进制摘要、official-only baseline evidence 和不可变镜像。

0.145 baseline 默认值是正确语义，不得预判为目标版本静默回退。首次失败原样记录；
后续探测只能修改临时副本，不得回写仓库或充当正式 Campaign。

### 5.5 P0-C/D：异版本与候选工具预检

- 机器扫描临时 0.145 Snapshot 的全部行为版本坐标，再完整变异为临时 0.147；
- 输出替换前后命中集合与摘要，不把当前“34 处”写成常量；
- 运行 Makefile、profile/runtime/release/binding dump、compare 和双版本门禁；
- 检查 candidate core/aux、WS gateway、relay、manifest、guard、trace 和 Schema；
- 对错误、空值、正确 target version 做 mutation，阻止 UA/header/query 继续生成 0.145；
- 检查 classify 草案是否只改顶层版本而遗漏行为字段；
- 若缺少从批准 Profile 生成内容寻址 RuntimeCatalog 的入口，登记阻断，不手拼正式 JSON。

### 5.6 P0 已确认阻断及处置

| ID | 已核实事实 | 处置状态 |
|---|---|---|
| P0-A-ENV01 | package load 不完整可假绿 | `e887085fb` 已改为失败关闭并补 mutation |
| P0-B01/D06 | scenario SPEC 与 test trace 摘要过期 | `1ccb981d3` 已统一真实摘要并补扇出测试 |
| P0-D01/D04 | candidate 工具存在 0.145 行为硬编码 | `4f187e5c0` 已建立 target-version 单一权威 |
| P0-D02 | classify 只改顶层版本可假绿 | `21c2ab2b9` 已增加全行为坐标校验 |
| P0-C01～C05 | Makefile、异版本、文件数、transport、Compiler 门禁不完整 | `15f23be87` 已补齐并通过真实异版本 mutation |
| P0-D03 | 缺少批准 Profile → RuntimeCatalog 安全入口 | `1bde77d62` 已新增确定性离线候选生成链 |

P0 报告必须包含所有命令和输入摘要、临时资产 inventory/hash、三组缓存 mutation、
失败分类、最小候选修复范围，以及“是否可创建正式 Campaign”的结论。

## 6. 正式 Campaign、官方取证与分类

### 6.1 `planned`

只有 DOC-PRE、P0 和实证阻断修复均完成，工作区干净且**产出侧**工具摘要不再变化，才可创建
正式 Campaign。Campaign 目录必须持久、绝对、尚不存在，不能使用 `/tmp` 或符号链接。

#### 6.1.1 工具漂移按证据影响面分级（2026-08-12 起）

原规则是「创建后任何工具漂移都必须拒绝继续」。该规则已按影响面分级——**不是放松证据
要求，而是把两件被混为一谈的事分开**：

| 侧别 | 含义 | 漂移后果 | 文件数 |
|---|---|---|---|
| **产出侧** | 决定证据字节：采集驱动、探针、中继、脱敏、收据生成、环境快照、编排器 | **拒绝继续**，整轮重建 | 90 |
| **评估侧** | 只读既有证据做判定／汇总／编目，不写证据 | **放行**，并把变化文件登记进台账 | 8 |

分级的依据是可验证的：评估侧文件改动后，已封存证据逐字节不变，重新评估一遍即可，
因此承接（`resume`）的证据前提仍然成立；产出侧任何改动都可能改变证据本身。

评估侧是**显式白名单**（`_EVALUATION_SIDE_FILES`），当前 8 项：`acceptance_contract.py`、
`assertion_gate.py`、`build_capture_manifest.py`、`build_evidence_catalog.py`、
`build_rule_assertion_results.py`、`candidate_rule_assertion.py`、
`check_sample_integrity.py`、`candidate_capture_manifest.schema.json`。
**未登记的文件一律按产出侧处理**，新增文件因此默认落到严格侧，不会因为忘记登记被静默放行。

三条配套约束：

1. **放行必须留痕**：评估侧漂移写入 Campaign 目录下的 `tool-evaluation-drift.json`，
   记录变化文件、放行时的分组摘要。另立文件是因为 `campaign.json` 由 `campaign.sha256`
   保护且一次封存不可变，追加内容会直接破坏完整性校验；
2. **旧 Campaign 退回严格模式**：plan 时未记录分组摘要的 Campaign 无法证明分级前提，
   一律按原规则拒绝；
3. **要动白名单必须逐条论证**，判据是「该文件的改动能否改变已封存证据的字节」。

> **为什么要做这件事**：k34～k56 中，可归因于验证工具自身缺陷的作废轮次占多数，
> 而其中相当一部分改的是判据与门禁语义（k43 的负样本契约、k46 的门禁语义、
> k56 打通 seal 门禁的四处修复）——全部落在评估侧。旧规则下，改一个字符串就要重采
> 官方 28 个场景。工具本身早已实现 `resume --rerun-failed`，甚至在任务失败时主动提示
> 「修复失败任务后使用 resume」，但只要修的是工具代码，身份校验就把 resume 挡死——
> **提示与校验自相矛盾**，这才是轮次爆炸的直接机制。

### 6.2 `official_sealed`

使用真实 0.147 stable CLI 执行 `capture-official run/seal`。真实请求必须显式提供
`--acknowledge-live-requests`。每次失败重跑新 attempt，不自动 resume，不跨 attempt
复用证据。必须封存：

- tag/commit/source tree/Cargo.lock/依赖/binary SHA/platform/config/runtime image；
- L1/L2 源码调用链、原始与脱敏抓包、inventory、attempt result；
- TLS/HTTP/WS/Body/endpoint/跨请求状态、恢复、secret scan 和 evidence seal。

历史记录（Campaign `codex-0145-to-0147-20260809T101826Z-k34`，已因后续受管工具身份变化
作废）：17 个官方场景一次
全过，`official/result.json` 状态为 `complete`，`stage=capture-official`，封存时间
`2026-08-09T10:37:18Z`，共 409 份证据、secret scan findings 0。官方二进制为 `0.147.0`，
CLI SHA `cb0a1556…`，Code Mode helper SHA `00ecf5d0…`，package asset SHA
`bd758d53…`；原始证据仍只保留在 Vircs 私有采集根。

k22 及其之前各轮的官方证据保持不可变，但因 §7.4 的工具身份变化已不可继续使用，
不迁移、不复用。

### 6.3 累计变化取证

官方 Full Changelog、合入 PR 和 0.147 tag 只证明源码级变化，不等于 wire 已确认：

| 领域 | 必须覆盖的变化 |
|---|---|
| HTTP/代理 | route-aware pool、按目标 URL 选代理、跨源重定向重新选路和敏感头清理 |
| OAuth/models/Responses | 共享 HTTP client、缓存/ETag、refresh、Body/metadata/compact、多轮状态 |
| WebSocket | `previous_response_not_found` 重试并重发完整请求 |
| Realtime/WebRTC | session/thread ID；sideband 默认 API base 与 provider query 变化 |
| Files | create/Blob PUT/finalize 使用 session 级 pool；PUT streaming body 与显式长度 |
| 条件功能 | MCP OAuth、Apps/插件、Cookie、PSP、套餐图片能力的正反条件 |

正式结论只来自 Campaign 本地源码 diff 与官方抓包；不得从 PR 推定最终
host/path/query/header、framing、代理、TLS 或连接复用。

### 6.4 `profile_approved`

运行 classify 后逐规则标记 `inherit/change/add/delete/condition_change/blocked`，并人工批准
五份完整 0.147 清单：`target-rules.json`、`rule-migration.json`、`scenarios.json`、
`profile.json`、`assertion-profile.json`。每份记录 SHA-256，并批准联合 `review_sha256`；
必须 `blocked=0`。双轨采集允许在轨道级事实中记录 `track_not_applicable`，但该状态不得
替代联合结论；每条 Lite-only 规则必须在 Lite 专项轨道取得最终分类。

新出站面不是失败；未发现、未分类或静默回落旧画像才失败。删除规则必须同时具备源码
不可达结论、正反场景、旧引用清单和 RemovalReceipt，否则保持 `blocked`。

历史记录（Campaign `codex-0145-to-0147-20260809T101826Z-k34`，不得迁移或复用）：
`classification/result.json`
状态为 `complete`，`stage=classify`，封存时间 `2026-08-09T10:40:03Z`。联合摘要为
`d762e73bc980b202d2d9387f3da8bb1b6c361cba8390a63e26be7ab6c5639cba`，classification package
digest 为 `16e36c8aaa818e3803edd05f66b971a5fe255c50170bb88bb28ba9977ef4e3f0`；42 条规则、
2750 条发现全部完成分类，`unclassified_count=0`、`blocked=false`。批准清单及摘要如下：

| 清单 | 封存 SHA-256 |
|---|---|
| `target-rules.json` | `91616975b1a7e3e3717a99c72a937b8f6901a921c434decb2ee7fdab85c4f75b` |
| `rule-migration.json` | `3e8f751eaaeeea6be35af792d7bcc642a5b7bc99c31f192a01a830e40044d792` |
| `scenarios.json` | `99d1202ea0500ffa96de74434426dac9095be89f932dc412f0642bf5aeb4606f` |
| `profile.json` | `d9cb89a3d930c5a5466079356cd43a3a8210e0f753aadd6e05606771331eecb6` |
| `assertion-profile.json` | `9e51781f6d2d92a7f8363e4859f8699c21290ae2cc01ab0539f69de17080feac` |

目标画像为 `codex-0.147.0-official-k34-v1`，Profile digest 为
`0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f`。五份清单已写入
Campaign 的 `classification/approved/`，并通过 stage-contract、文件摘要、画像 payload
摘要和场景覆盖复核；尚未写入仓库 Active selector。

`target-rules.json` 与 `assertion-profile.json` 的摘要与 k22 逐字相同——规则集本身未变；
`profile.json` 因 `profile_id` 随 Campaign 变化而摘要不同，但其 payload digest
（`0d86e033…`）跨轮次恒定。

> **⚠ 2026-08-12：上面这句「恒定即未漂移」的推论不成立，本节记录的 digest 已被取代。**
> 该画像缺 `wham_settings_user` 端点，而同一批清单里的判据已按该路径适配——十六次复算
> 一致只证明每轮算出同一个值，**不证明这个值覆盖了 0.147 的全部出站面**。
> 端点已补齐，`profile_digest` 由 `0d86e033…` 漂移至 **`94071c8e…`**、端点 16→17，
> 新画像已在 k59 的 `classification/approved/profile.json` 封存（`profile_id=codex-0.147.0-official-k59-v1`）。
> 本节表格保留 k34 的历史摘要不改，仅作追溯用；证据与处置见 §10.2.1。

## 7. 建立 0.147 画像与第二版本门禁

### 7.1 画像资产

- 新增完整、内容寻址的 0.147 Profile Snapshot；
- 新增 SnapshotCatalog、Release graph 候选节点及 profile/release contract 镜像；
- 建立候选阶段保持 Active=0.145；启用时 Previous 指向已验收 0.145；
- 不复制 `official_egress_codex_0145_profile.go`。只有 JSON/现有投影无法表达真实字段时，
  才另行批准版本中立的最小共享代码变更；
- 保留全部 0.145 Snapshot、fixtures、抓包、摘要、规则和收据；
- `/models` 行为变化时新增 0.147 fixture，不覆盖 0.145 fixture。

### 7.2 必过门禁

1. **工具坐标**：version/profile/release/Campaign 坐标缺失必须 fail-close，禁止静默回退；
2. **Compiler**：每次真实换版都枚举新旧完整 endpoint，并机器断言
   `validated endpoint IDs == 新旧 ReleaseCatalog endpoint ID 并集`；
3. **query/source**：新增来源必须有 Compiler 语义。`server_response` 必须走真实 service
   值来源与透传链；通用捕获器自动合成值不能单独作证；
4. **双版本**：Active/Previous 的 version、profile digest、release digest 均不同；
5. **调用隔离**：在途 invocation 保持旧 Bundle，新 invocation 使用新 Active；fallback
   不跨 Bundle，连接池按画像隔离；
6. **alpha-search**：真实经过
   `ForwardAlphaSearch → buildOpenAIAlphaSearchRequest → invocation.Execute`，逐 mode 核对
   URL/method/Host、version header、UA、TLS、release/profile digest；通用 Executor 无效；
7. **alpha 遗留判定**：对固定 ProfileVersion 做错误值、空值、正确值 mutation；有消费者
   才提 bundle 派生最小修复，无消费者才走规格 §4.9 退休；
8. **Body/投影**：只有新增或变化的条件、枚举、字段才触发生产改动；不可达差异只记录
   测试。新增 service 消费字段必须做字段覆盖或逐叶 mutation，digest 相同不能证明完整。

### 7.3 画像摘要阻断（已解除）

旧 Campaign `codex-0145-to-0147-20260807T170500Z` 对批准的 `profile.json` 执行
`egresscatalogstage` 时，机器复算为
`0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f`，而清单声明为
`5b209c2400321feda8c45592d80dcdf82dd4806235daf4bfdd30acba20becc43`。差异来自分类封存时
对画像嵌套 JSON 的键序规范化，而画像摘要仍按未排序源序计算；这不是版本行为证据，不能
通过只改顶层版本或放宽导入器校验解决。

最小工具修复为 `7de2404bd`：候选导入先压缩 RawMessage 空白并增加格式化批准副本测试。
按该结论重新建立的 Campaign `codex-0145-to-0147-20260808T054501Z-k22` 已用 Go 画像准备器
完成，批准清单声明与机器复算同为 `0d86e033…`，阻断解除；旧 Campaign 保持不可变，其目标
证据不迁移、不复用。

### 7.4 候选环境恢复判据修复

`codex_upgrade_environment_probe.py` 原先用容器内 `sha256sum /etc/hosts` 的原始字节摘要
参与 `configuration_state_restored` 的 `byte_equal` 比较。服务容器同时接入两个 Docker
网络，每次 `docker restart` 由 Docker 重建 `/etc/hosts`，两条地址行的先后顺序不确定，
同一环境连续两次重启即可得到不同字节序列。候选任务链路本身包含大量服务重启，导致
before/after 摘要随机不等。

实证：Campaign `k15`、`k16`、`k17`、`k18` 四次候选 run 全部以
`configuration_state_restored 恢复验证失败：环境未恢复：before 与 after 字节不一致`
被标记 `environment-contaminated` 而废弃，差异每次都只在 `sub2apiplus` 的 `hosts_sha256`；
`k19` 排障期同一容器 `73257dff9d3a` 的三份 hosts 快照出现 `172.21.0.4` 与 `172.18.1.2`
两种排列、三个不同摘要；`k18` attempt 全程 hosts 摘要均为 `16ae3f7d…`，仅在最后一个任务
cleanup 的重启后变为 `510664dc…`。

最小修复：探针改为对 `/etc/hosts` 按行排序后再哈希，并在快照中记录
`hosts_digest_mode = sorted_lines_sha256` 声明算法。条目的新增、删除、地址或主机名改写
仍然改变摘要并 fail-close，只有纯行顺序变化被吸收；CA bundle 与其余状态判据不变。已补
正反测试：顺序翻转必须逐字节相等，新增／删除／改写条目必须不等。

该修复改变工具身份摘要（80 项，`051a25d9…` → `725dbcbd…`）。`capture-candidate run` 入口
的 `_verify_plan_identity` 会以「升级工具摘要在 plan 后发生变化」拒绝继续，因此修复与
Campaign `…-k22` 不可共存（该修复动的是环境探针，属产出侧；按 §6.1.1 分级后此结论仍成立）：
按 §6.1 与 §7.3 先例重新建立独立 Campaign 并重做官方取证与
分类，k22 保持不可变、其目标证据不迁移不复用。

### 7.5 候选账号调度门恢复

`run_h1_wire_probe.sh`、`run_images_wire_probe.sh`、`run_candidate_core_capture.sh` 与
`run_candidate_aux_capture.sh` 会把 `chatgpt.com` 劫持到容器内探针或合成 relay。探针停止后
仍在途的真实出站拿到 `connection refused`，Sub2API 据此写入 `temp_unschedulable_until`
把账号临时熔断。四个脚本恢复了 hosts、CA、keeper 与账号代理，唯独没有恢复这个调度门。

实证：k23 候选采集的 `candidate-compact-mitm`、`candidate-frozen-core`、
`candidate-frozen-aux`、`candidate-images-wire` 连续失败，入站记录全部是
`503 Service temporarily unavailable`，服务日志为 `openai.account_select_failed`
（`no available accounts`、`excluded_account_count=0`），账号 #90 的
`temp_unschedulable_reason` 记录为
`Post "https://chatgpt.com/backend-api/codex/responses": dial tcp 172.21.0.6:443:
connect: connection refused`，`172.21.0.6` 即 capture-cli 容器。k17、k18 的失败模式相同。

修复（`c9d9e11eb`）：四个脚本运行前以 hex 冻结 `temp_unschedulable_until` 与
`temp_unschedulable_reason`，cleanup 按原值回写并复核，不一致即失败关闭。按原值回写而不是
无条件清空，运行前就存在的真实熔断不会被掩盖。

### 7.6 运行画像激活事实

`capture-candidate seal` 要求一份由运行中服务产生的画像观测收据，但服务此前没有任何代码
产出它；`egressruntimedump` 只能导出二进制内编译了哪些画像，证明不了当前发布指针实际解析
到哪个。k15～k23 的候选 seal 因此从未走通。

修复（`1bbebb8de`）分两侧：

- 服务侧在启动装配完成后按当前发布指针解析画像，记录 profile digest、release digest、
  codex 版本与画像模式；构建期身份由部署方经环境变量声明，服务把声明中的 profile digest
  与自己解析出的 digest 逐字比对，不一致拒绝落盘。`event_id` 用事实内容寻址而非随机数。
  生产默认只写启动日志，只有显式设置 `GATEWAY_EGRESS_ACTIVATION_FACT_PATH` 才落盘；
- 采集侧 `build_observed_profile_runtime_audit.py` 校验事实的 schema、来源、事件类型与全部
  身份字段后，补上 attempt 坐标（campaign／attempt／run_nonce／candidate）——这些坐标是采集
  侧自身的权威事实，服务启动时无从得知。工具不生成 `event_id` 与观测时刻，只承接服务事实。

### 7.7 A13 OAuth refresh 被自身 Guard 拒绝（既有缺陷）

候选辅助采集 A13（OAuth refresh）在 k26 被 `request_modified_after_finalize` 拒绝。用
0.145 画像做对照实验同样失败，确认是既有缺陷，与本次升级无关——它此前没被发现，只是因为
候选验收从未跑到过 A13。

根因是两条出站链的包装顺序不一致：

- `http_upstream` 链是 `lowercase(Guard(base))`，Guard 看到的是最终 wire；
- `req.Client` 链是 `Guard(lowercase(base))`，lowercase 在 Guard 内层。

OAuth refresh 端点画像声明 lowercase header wire 名（`accept` 等），`compiler.go` 用
`headers.Set()` 写入时被 Go 规范化成 `Accept`。`http_upstream` 链上 Guard 校验的是转换后的
最终 wire，能通过；`req.Client` 链上 Guard 先于 lowercase 执行，看到的仍是 `Accept`，判定
定型后被改写并拒绝。**这意味着生产环境里所有走 `req.Client` 且画像要求 lowercase wire 的
官方出站端点都不可用，OAuth token 刷新即在其中。**

修复（`31768d0a6`）把 `req.Client` 链的包装顺序对齐 `http_upstream`：
`instrumentReqClientWithGuard` 接收 `*tlsfingerprint.Profile`，lowercase 提到 Guard 之外。

同时给 `GuardDecision`／`GuardRejectionError` 增加 `Diagnostic` 字段，区分 normalization
不符与 digest 不符——原先两类失败共用同一个 reason，无法定位。诊断只输出 header 名，不输出值。

验证：修复前调用 refresh 端点返回 Guard 拒绝；修复后返回上游真实的
`401 token_expired`（用的是构造的无效 token），说明请求已真正送达 `auth.openai.com`。

### 7.8 A11 Live attestation 在 Linux 的接线

A11 需要 ChatGPT DeviceCheck attestation，而 `Provider` 只实现了 macOS。官方 Codex CLI 源码
没有平台门禁，因此这不是"Linux 跑不了"，只是 Sub2API 侧缺实现。仓库早有
`candidatecapture` 构建标签与合成 provider，但从未接通——`attestation_candidate_capture.go`
引用的 `candidateCaptureScopeFromContext` 根本不存在，该分支平时既不构建也不测试。

处置：补齐缺失函数；新增 `build_matrix_test.go` 对 linux/amd64 同时编译默认与
`candidatecapture` 两种配置，防止该分支再次腐烂；候选镜像以 `-tags "embed,candidatecapture"`
构建。合成 provider 只在进程环境声明的本轮四元组（api_key／group／account／临时代理）匹配时
才生成值，有效期上限 20 分钟。

采集侧 `run_candidate_aux_capture.sh` 在 A11 第一跳前按本轮四元组重建服务，采集结束按原
compose 拉回。两处易错点已加断言固化：compose 重建产生全新容器，必须补装抓包 CA 并重启，
否则第一跳在 TLS 阶段被自己的 relay 证书挡下；恢复部署必须早于 hosts 回灌，否则最终容器
hosts 与复核基线不一致。

## 8. Candidate、比较与 `ready`

Candidate 必须固定 commit/tree/build ID/deploy version/OCI digest/image ID/profile digest，
并在同一 Campaign/attempt/run_nonce 下覆盖：

- Codex CLI/Desktop、Compatible、Responses HTTP/WS、Kilo；
- models、Responses 首轮/多轮/恢复/compact/fallback；
- OAuth refresh、alpha-search、images、WHAM、Realtime/WebRTC、Files；
- OAuth direct/proxy、API Key mimic、辅助端点的 TLS/HTTP/连接生命周期。

执行 `capture-candidate run/seal` 后，断网重哈希，再逐规则比较官方与候选证据。每条规则
必须有官方证据、候选证据和机器断言。identity、client receipt、recovery、secret scan、
inventory 和 evidence seal 缺一不可。

compare 无未登记漂移后执行 accept/status 重放；只有摘要、收据、安全和 seal 全部通过才
可进入 `ready`。changeset3 旧生成入口不可复现，不能作 authority；changeset6 通用
final-wire 可用，但不能替代 alpha 真实 service 链或 `server_response` 生产信任链。

## 9. 生产启用与回滚

### 9.1 启用动作

1. 保留 0.145 Snapshot、Previous、旧镜像和 compose 回滚点；
2. 只按 release registry 指针做 canary，不按入站版本选画像；
3. 验证新旧 endpoint 并集、调用 Bundle、fallback、连接池隔离；
4. 依次记录 canary、切换、回滚演练、恢复后的 final-wire；
5. 验证 health、日志、`/v1/models`、OAuth/API Key 的 `response.completed`、数据库、Redis、
   keeper、挂载、代理和 CA；
6. 验收通过后保持 Active=0.147、Previous=0.145。

上线动作之前还必须：冻结源码／镜像／配置／迁移与依赖摘要；只读核对 Vircs 实际架构与镜像
架构一致；部署**独立 canary** 完成启动、健康、API、WebSocket、出站 TLS 与错误率检查，
canary 通过后才允许更新正式实例。全程使用独立账号、独立 `CODEX_HOME`、独立配置和独立
证据目录，不触碰生产凭据、全局 `/etc/hosts`、生产 relay 或生产 Active/Previous。

### 9.2 回滚

逐规则断言、digest/inventory/seal、Guard、账号、health、恢复或 secret scan 任一失败，
或出现旧画像静默兜底、跨 Bundle fallback、连接池混用，立即切回 Previous + 旧镜像。

回滚不删除 Campaign、不覆盖 0.147 Snapshot、不重建数据容器。回滚后重新检查服务、
挂载、账号、keeper、代理/CA、新旧入口和 final-wire，并保留失败证据。

## 10. 当前执行状态与跨轮知识

2026-08-14 · k71 · **Active 仍为 0.145，0.147 尚未替换**。

本章装着两类性质不同的内容，按需要读，不必通读：

| 分区 | 小节 | 什么时候读 | 更新频率 |
|---|---|---|---|
| **状态** | §10.0 执行环境与机器角色<br>§10.1 进度与下一步<br>§10.2 accept 待解决的 7 条 | 接手时、决定下一步做什么时 | **每轮都变**——只看这三节就知道现在在哪、该干什么 |
| **知识** | §10.3 双轨模型策略<br>§10.4 采集操作要点<br>§10.8 逐轮不变量总表 | 开跑前、跑挂了排查时 | 跨轮稳定，只在踩到新坑时追加 |

接手请从 §10.0 读起（机器角色与源码树纪律），再看 §10.1。

> **§10.5／§10.6／§10.7 是空号**，内容已并入他节（原 §10.3 执行前置并入 §10.0＋§10.4，
> 原 §10.5 收据字段转为直接指向操作笔记）。编号保留空位不重排，是因为
> `acceptance_contract.py`、`codex_upgrade.py` 等 **8 个受管工具文件的注释里有 14 处引用
> §10.8.x**——重排就得改这些文件，那会改动产出侧工具身份摘要并卡住下一轮 `plan`（§6.1.1）。
>
> 同一原因下有一处**已知失配**：`candidate_test_trace.py:52` 的注释引用「§10.2.1 第 4 条」，
> 那是 13 条根因时期的编号，现已对不上（内容指向仍正确，都是 `wham_settings_user` 端点定义）。
> **不修**——为一条注释付工具身份变更的代价不值得，在此登记备查。

### 10.0 执行环境与机器角色（接手前必读）

三台远程主机，全部经 SSH 连接（主机别名即下表「SSH」列，已配在 `~/.ssh/config`）。
**角色不可互换，搞反等于在生产机上做实验。**

| 别名 | 架构 | 角色 | 跑什么 | 碰它的后果 |
|---|---|---|---|---|
| **Vircs** | x86_64 | **生产服务器** | 对外提供服务的 Sub2API 实例；同时是**唯一**装有官方 Codex CLI 0.147.0 的机器 | 重启／换镜像／改 Active 都会中断真实用户；§9 之前**只允许**在其上做官方 CLI 采集，且必须走独立 Campaign／relay／容器，不碰服务进程、生产配置、生产端口、生产数据与 Active/Previous |
| **DMIT** | x86_64 | **测试服务器** | 独立的 Sub2API 实例（1 核 / 1.9G / 20G 盘），候选采集全部在此 | 可随意重启、换镜像、改数据库；不影响任何真实用户 |
| ARM64 | aarch64 | 测试机 | 另一套 Sub2API 测试环境 | **不能做采集，已实测确认失败**：采集必须在 amd64 容器内抓包，而该容器在 ARM64 上只能靠 qemu 用户态模拟运行，qemu 不翻译抓包所需的 `TPACKET_V3`／`SIOCETHTOOL` ioctl，tcpdump 直接起不来。换 tcpdump 版本、加 capability、改 privileged 均无效——缺的是内核接口翻译，不是权限。**别再试第二遍** |

**分工为什么固定成这样**：冻结的官方 Codex CLI 资产是 Linux `x86_64`（官方采集须在
Ubuntu 24.04 / x86_64 完成）；候选采集虽不引用该二进制，却同样绑定 amd64——脚本必须在
campaign 冻结的 amd64 capture 容器内抓包。而**官方与候选共用同一个 `runtime_image`，
一个 campaign 装不下两个架构**，所以只能是官方在 Vircs、候选在 DMIT。

官方与候选分处两机，但共用一个 Campaign，由此有两条硬性义务：

1. **候选机须持有官方原始证据的完整副本**（约 90M，`runs/` 下同名路径）——
   `_verify_stage_evidence` 按官方 `result.json` 的**绝对路径**重算摘要，缺副本则
   证据链在候选侧无法自证。
2. **两台机的候选树同名同路径，且与候选镜像同源**——树名进收据的 finalizer 路径，
   不一致则 `compare` 必失败；树里的画像须与镜像构建时一致，否则 go test 基于错误
   画像产出事实。k67～k70 四轮死于此（§10.8.11、§10.8.12）。

关键路径（两台机一致）：

```
/root/oauth-capture/                     采集根（campaigns/ runs/ runtime/ state/ scripts/ tools/）
/root/oauth-capture/campaigns/<campaign> Campaign 目录
/root/oauth-capture/candidate-source-k70 受管工具树（工具身份由此计算；当前轮次用 k70）
/root/oauth-capture/candidate-build-<k>  候选构建树（源码 + staged 画像，用于构建候选镜像）
/root/Docker/sub2apiplus/app             DMIT 的 compose 工作目录（候选部署 override 放这里引用）
```

开跑前还要定的另一件事是**用哪个模型**：主线 `gpt-5.5`、Lite 专项 `gpt-5.6-luna`，
权威定义在 `capturelib.model`，选定依据与双轨的五条硬约束见 §10.3。

### 10.1 进度与下一步

| 章 | 现状 | 下一步 | 机器 | 人工 |
|---|---|---|---|---|
| §5 DOC-PRE 与 P0 | ✅ 完成 | — | — | — |
| §6 Campaign / 官方取证 / classify | ✅ 完成 | — | — | — |
| §7 建立 0.147 画像 | ⚠️ 画像已批准，Snapshot/Release 资产**没建**（k71 漏做，见下） | **并入 B**：下一轮 classify 完成后、候选采集开始前执行 `stage-profile`。**不能单独补做** | DMIT | 否 |
| §8 Candidate / compare / accept | ⚠️ 首次跑通，35 pass / 7 fail | **B** 改 5 条行为差异 ＋ 两项工具前置检查（见下）→ 重建候选镜像（`EP-019` 的修复已就绪，只差这一步）→ 重跑一轮验证。7 条构成见 §10.2 | 本地＋DMIT | 是（Kilo 发两条请求） |
| §9 生产启用与回滚 | ❌ 未开始 | **C** 依赖 B 收口后才能动 | **Vircs 生产** | 是（每步确认） |

> **原先「A 与 B 互不依赖、可并行」的判断已作废（2026-08-14 核实）。** `stage-profile`
> 只允许从 `profile_approved` 状态执行，而 k71 现在是 `compared`——状态由不可变阶段收据
> 单向推导，回不去，**在 k71 上补不了**。全盘只有 k49 建过 staged 产物
> （`runtime/k49-staged-profile`），其 `campaign_id` 与画像都是补端点之前的，不可复用。
>
> **k71 为什么会漏**：`stage-profile` 在状态机里的位置是 classify 完成 →
> **`stage-profile`** → 候选采集，而 `campaign_status` 的 `stages` 字段只收录
> `capture-official` 与 `classify` 两项，跳过它不会留下任何缺失痕迹，`next_command`
> 也直接指向 `capture-candidate`。**下一轮必须在候选采集前手动确认这一步已执行。**

**B 重跑那一轮的前置门槛**（全部满足才可 `plan`）：**产出侧**工具摘要不再变化
（§6.1.1；评估侧修复不受此限）；**0.147 映射已在候选源码树内**——它的路径基准是源码树，
采集之后再放进去无效；操作笔记「候选机环境前置清单」13 项逐项核对（属主权限、账号模型
映射、分组开关、候选身份注入等），候选机已持有官方原始证据完整副本（§10.0）。
流程：官方采集（Vircs）→ classify → **`stage-profile`（k71 漏做的一步，§7 资产由此产出）**
→ 候选采集（DMIT）→ Kilo 两入口 → seal → compare → accept。

> **前置清单缺项不会在启动时报错**，而是在采集中途以 404／503／1013／「无可编目证据根」
> 等形态暴露——所以必须开跑前核对，不能靠跑起来验证。

**B 这一轮还要顺带落地两项采集工具的前置检查**。它们把 §10.4「开跑前必查」那两条从
「靠记得去查」变成「查不过就拒绝启动」——k61 与 k71 各因其中一条报废过一轮。两处都属
**产出侧**，会改动工具身份摘要，因此**必须赶在下一轮 `plan` 之前落地**（§6.1.1）：

| 项 | 现状（已核实） | 落点 |
|---|---|---|
| mitm 端口预检**时机太晚** | 检查本身已存在（`capturelib/lifecycle.py:327`，占用则报「MITM 端口已被其他进程占用」），但它在**每个 mitm case 启动时**才触发；mitm case 排在队列中间，撞上时前面的 direct case 已经跑掉了——k61「前轮残留 mitmdump 占 18080」正是中途才暴露 | `capture.py` 的 `_preflight_dependencies()`（"在任何模型请求前校验运行资产"）已有 `if "mitm" in evidence:` 分支，在其中调用现成的 `_port_is_open("127.0.0.1", mitm_port)`，整轮开跑前即拒绝 |
| admin token **不校验有效期** | `run_candidate_aux_capture.sh:61` 只校验 `^[A-Za-z0-9._~-]+$` 格式，不看 `exp`；过期时 A09／A11 正常而 A12／A13／A14 零流量，极具迷惑性——k71 因此半失败 | 同一处格式校验之后，用脚本已依赖的 `python3` 解 JWT payload 的 `exp`，剩余不足阈值即拒绝并打印过期时刻。阈值建议 **30 分钟**（k71 aux 实跑 12 分钟，留够余量又不至于挡掉刚签的 token） |

两处都复用已有机制、无新增依赖；改完补测试（`tests/test_lifecycle.py` 有 `mitm_port`
用例可扩）并跑工具测试全量。

> **「全部修复必须收口在 plan 之前」这条旧纪律已被 k71 推翻**——那 6 条挂着，全链路照样
> 跑到 accept。真正必须卡在 `plan` 之前的只有**会改动受管工具或候选镜像身份**的修改
> （§6.1.1）；判据类修正走 campaign 内的 classify `change`，行为类留到下一轮验证即可。

k71 是 k34 之后第一次走完全链路，各环节均已实跑验证（非预演），下一轮照做即可：
官方 28／28 与 seal、classify 继承脚本、候选 7／9 与三份收据、go test 结构化轨迹、
bundle → trace → seal 的顺序、`compare`、accept 重放。操作要点见 §10.4，坑见 §10.8。

> 查证据用 Campaign `codex-0145-to-0147-20260814T040000Z-k71`／
> campaign_id `codex-0_147_0-20260814T042234Z`／候选树 `candidate-source-k70`。
> 各阶段验证数据（扫描文件数、门禁 checks、恢复检查）以 `result.json` 为准，
> 本文档不复制——k59 的旧数字曾在此滞留十余轮。


### 10.2 accept 待解决的 7 条（k71 实测）

k71 是本次升级第一次拿到真实 accept 重放结论：**候选侧 42 条规则，35 `pass` / 7 `fail`**。
原先那份「13 条根因」是 k56 证据上的预演，逐条核对后 8 条已通过，本节只保留**仍未通过的**。

> 本节是 §10.1 表里 §8 那行「7 条待解决」的详表。**§8 收口 ＝ 本节清空**（`TLS-003` 那条挂账除外）。

> **预演不等于验收。** 原 13 条里，第 1、2 条长期记作「已闭环」——k69 真实重放里却失败，
> 一度让人以为修复失效，直到 k71 换同源树才转 pass（§10.8.11）；反过来，原记「B 类证据
> 损坏、未动」的三条（`BODY-001`／`BODY-005`／`EP-022 generation-body-order`）实测直接
> 通过，所谓「zstd 帧损坏」根本不成立，真实原因是判据选中了不产出 body 的探针面
> （§10.8.13）。**只有 accept 重放的结论算数**，基于旧证据的预演只能当线索。

**待解决 7 条**（8 个 check），按能否靠改判据解决分为两组：

| # | 判据／check | 实测现象 | 定性与下一步 |
|---|---|---|---|
| 1 | `SPEC-TLS-003`／`extension-order-diversity` | `artifact_count=3`，判据要 4 | **挂账**。`minimum_distinct_orders` 已按 §10.8.8 降为 1；`minimum_artifacts=4` 刻意保留——缺的那份 WS pcap 来自已登记的 mitm 缺口（§10.8.10）。**判据不迁就采集缺口**，缺口恢复后自然通过 |
| 2 | `SPEC-EP-015`／`search-header-order` | 出站无 `cookie`（官方侧每条都带） | 真实行为差异。relay prime 修复未生效，须查 relay 是否收到 `Set-Cookie`、网关是否透传 |
| 3 | `SPEC-EP-022`／`image-header-order` | 同上 | 与第 2 条同源，一处修复覆盖两条 |
| 4 | `SPEC-EP-014`／`legacy-default-headers` | 缺 `x-openai-internal-codex-responses-lite` | Lite 轨样本仍未产出。aux job 已写死 `gpt-5.6-luna`（§10.3），但头仍缺 |
| 5 | `SPEC-BODY-006`／`lite-top-level-omission`＋`lite-tools-omission` | k71 换同源画像后新暴露 | Lite 轨相关，与第 4 条可能同源，一并排查 |
| 6 | `SPEC-EP-019`／`wham-get-paths` | 只发 `usage`＋`rate-limit`，缺 `settings/user` | **修复已写好但没进镜像**（2026-08-14 查明）：镜像来自 `candidate-build-k57`，其门控是 `mode == previous`，候选采集时不成立故不发请求；工作区与 k70 采集树早已改成按端点存在性判断。B 轮重建镜像即解决。专述见 §10.2.1 |
| 7 | `SPEC-HDR-002`／`residency-positive` | `x-openai-internal-codex-residency` 为 `<absent>` | 新暴露。待确认是账号不具备 us residency（条件未造出）还是真实缺陷 |

第 2～7 条都**改判据解决不了**，必须动 `backend/` 并重新构建候选镜像——那是独立变更集。
其中**第 6 条 `EP-019` 的代码已经写好**（在工作区与 k70 采集树里，见 §10.2.1），
只差提交与重建镜像；**要新写代码的是余下 5 条**。注意「待解决」仍计 7 条——
`EP-019` 要等重建镜像后重跑才能转 `pass`。

**已通过的 8 条**（不再逐条展开）：`SPEC-HDR-005` ×2（测试按 `previous` 槽位解析，
需在同源树上跑 go test 才成立）、`SPEC-EP-014`／`legacy-beta-slot`、
`SPEC-EP-014`／`legacy-turn-state-slot`（依赖前者，链路自动接上）、
`SPEC-EP-020`／`legacy-observed-subset`（含 `text` 那处）、
`SPEC-BODY-001`／`SPEC-BODY-005`／`SPEC-EP-022 generation-body-order`（判据 selector 收紧）。
其中 5 条靠代码修复、3 条靠判据修正。

**两项收尾工作**（与上表 7 条并列，但性质是"已做完但没落到位"）：

| 事项 | 现状 | 待办 |
|---|---|---|
| 判据 4 条 selector 修正 | 只存在于 campaign 的 `classification/approved/assertion-profile.json`，靠逐轮继承传递 | **同步回工具树** `candidate_rule_expectations_0_147_0.json`，否则下一轮从工具树重新迁移时会丢 |
| 代码修复 | 8 个文件、125 行仍是工作区未提交状态。其中 `openai_quota_service.go` 那 26 行是 `EP-019` 的**已完成修复**（见 §10.2.1），k70 采集树已同步、候选镜像未同步 | 提交并**重建候选镜像**，否则 `EP-019` 下一轮照旧 fail；提交前跑一遍 §10.8.11 的三处摘要一致性检查 |

#### 10.2.1 `EP-019`：候选未触发 `wham/settings/user`（画像与镜像均已具备）

排查此条要用的官方基线（k56 证据，**受控出站面** `record_type=http_request` 的 34 次样本，
横跨 20 个官方场景——是高频出站面，不是边缘路径；同期 `wham/usage`、
`rate-limit-reset-credits` 各 1 次）：

| 项 | 观测值 | 一致性 |
|---|---|---|
| method / path / protocol | `GET /backend-api/wham/settings/user`，HTTP/1.1 | 34/34 |
| header 线序 | `user-agent, authorization, chatgpt-account-id, [x-openai-fedramp], cache-control, accept, [cookie], host` | 含 `cookie` 28 次／不含 6 次 |
| 与其余 wham GET 的差别 | **多一个 `cache-control`**（常量 `no-cache, no-store`），故 `wham-get-headers` 用 `not_equal` 明确排除它 | 34/34 |
| query / body | 均为空 | 34/34 |

> `x-openai-fedramp` 槽位**未在本轮观测中出现**（采集账号非 FedRAMP），是按同类 wham 端点
> 一致性推定的，此处显式登记以备复核。

**该端点已于 k59 补进画像**（16 → 17 个，`profile_digest` `0d86e033…` → `94071c8e…`，
经 `egresscatalogstage -prepare-snapshot` 规范化、`profilecontract` 校验通过），
镜像二进制也含 `wham_settings_user` 常量与路径（§10.8 有 `strings` 实证）。
**因此剩下的是候选驱动没造出触发条件**，与画像、编译产物都无关。

**候选镜像里的门控是错的，但修复已经写好了**（2026-08-14 逐树核对）：

| 树 | `openai_quota_service.go` 的门控 | 是否进了 k71 的镜像 |
|---|---|---|
| `candidate-build-k57`（镜像来源） | `mode == officialClientProfileModePrevious` | **是**——k71 跑的就是它 |
| 工作区 ／ `candidate-source-k70`（采集树） | `resolveCodexEndpointForMode(...)` 解析成功即支持，**按端点存在性判断** | 否 |

用发布槽位当版本开关本身就错：生产启用后 Active=0.147、Previous=0.145，该分支反而会去
0.145 画像里找这个端点；候选采集时 mode 也不是 `previous`，所以请求根本没发出去——
这就是 `wham-get-paths` 只看到 `usage`＋`rate-limit` 的全部原因。**修复无须再写，
B 轮把工作区代码提交并重建候选镜像即可**。

> 这条也解释了「镜像二进制含 `wham_settings_user` 常量」为什么不等于「会发这个请求」：
> k57 树里常量定义与调用点都在，只是调用被一个永不成立的条件挡住。
> **`strings` 能证明符号存在，不能证明代码路径可达。**

#### 10.2.2 Lite 轨样本：job 已改写死 `gpt-5.6-luna`，但样本仍未产出

对应待解决第 4、5 条。原断点已查明并修复，但 k71 实测头仍缺，**修复未生效或另有断点**。

已查明的机制：候选 aux job 的证据标签中 6 条规则标 `track=lite`，rationale 写着「A09 job
固定 Lite 模型（MODEL 默认 `gpt-5.6-luna`）」——标签按脚本默认值推出，**符合「labels 必须由采集参数推出、不得由判据反推」这条纪律**；
但 job 定义用 `"MODEL": "{model}"` 把它覆盖成 Campaign 主线模型（本轮 `gpt-5.5`）。
变量契约本就写明「Lite 专项 job 直接写死 `gpt-5.6-luna`，不经本变量」，官方侧 3 个 lite
job 都照做，唯独候选 aux 用了变量。**已改为写死**——下一步要查的是改后为何仍无 Lite 样本。

> **教训**：标签、脚本默认值、job 定义三者都在表达「这个 job 用什么模型」，任意两个不一致
> 就产生「选得中但选错样本」的失败——比选不中更难查，因为可达性检查会通过。在「labels 不得由判据
> 反推」之外，这里补一条对称约束：**job 定义不得静默覆盖标签所依据的采集参数**。

#### 10.2.3 cookie 链路：relay 判定已改按序号，但出站仍无 `cookie`

对应待解决第 2、3 条。同上——断点已查明并修复，k71 实测仍缺，需继续排查。

机制两端本就齐备：aux relay 在 compact prime 轮下发 `set-cookie`（`_cfuvid`，在生产
allowlist 内），生产侧 alpha-search 与 images 都调用了 `bindOpenAICookieJar`。断点在 relay
的 prime 判定曾写作 `if '"capture_variant":"prime"' in turn_metadata`——`capture_variant`
是采集脚本塞进**入站** `x-codex-turn-metadata` 的自造字段，而网关按画像**重新生成**该
header（k56 实测出站是 `{"installation_id":…,"session_id":…}`），该字段在出站字节里根本
不存在，判定恒为假。**已对齐 core 侧改按到达序号识别**（新增 `legacy_compact_ordinal`）。
turn-state 仍按 `x-codex-beta-features` 头下发——那是画像条件槽位的真实产物、出站确实存在。

> **旧测试为什么没拦住**：它直接构造带 `capture_variant` 的请求去断言下发 Cookie——喂了
> 真实链路上不可能出现的输入，于是测试常绿、采集常败。**构造输入时绕过了被测链路的改写
> 环节，就等于没测**——这与「测试脚手架自带预置模型清单、掩盖了第三方客户端 Lite 画像
> 失效」是同一种失效模式：测试通过不等于画像生效。

#### 10.2.4 legacy compact 条件头四条：根因各不相同

四条症状相同（compact 出站丢失条件头），根因四条各异。k71 实测已 pass 两条：
`legacy-beta-slot`（真缺陷——`officialCodexTrustedConditionalHeaders` 未登记
`x-codex-beta-features`，致 `runtimeState.ConditionalHeaders` 恒空；已修复）与
`legacy-turn-state-slot`（**不是缺陷、也无需改采集**——闭环机制完整，断点就是前者，
修好后自动接上）。**仍 fail 的两条根因都是 Lite 轨样本缺失**：

| 判据 | selector 要求 | 根因 |
|---|---|---|
| `EP-014`／`legacy-default-headers` | `variant=default` **＋`track=lite`** | 候选 compact 全程跑主线 `gpt-5.5`，selector 要的 `track=lite` 样本根本不存在 |
| `EP-020`／`legacy-observed-subset` | 同上 | 同上（k71 已 pass，随 selector 收紧解决）。另：`text` 被错列进「compiler 拥有的字段」而 omit、compiler 又不生成它，疑似真缺陷，`body_document.go` 那处改动**保留但记为待证**，判定权交给 Lite 样本采到后的 accept |

> 这两条与 `BODY-006/lite-*`×2 同属 §10.3 列明的「只在 Lite 专项轨验收的 4 条判据」。
> 实证：k56 含 A09 compact 的 `frozen-aux` **全程只有 `gpt-5.5`**（trigger body 逐字可查），
> 因此 compact 场景从未产生过任何 Lite 轨样本。

条件的真正取值点是 `official_egress_identity_authority.go` 的 `conditionalField`（读
`runtimeState.ConditionalHeaders`），**不是** `officialCodexConditionsFromHeaders`
（后者只服务 models 端点），也不是 `finalizeOfficialOpenAIHTTPHeaders`（只有测试引用）
——本节两次栽在「函数名与症状对得上就当成证据」。**先 grep 调用方，再读分支**，
§10.8.1 的排查纪律同样适用于读代码；另一条是**症状相同不等于根因相同**，
原先「四条全部指向同一处」的判断会把设计行为当 bug 改掉。

**动这几条生产代码会撞版本泄漏门禁**：新增分支的错误消息不能带三段版本字面量，
**注释也在扫描范围内**；也不可用 `UPDATE_VERSION_LEAK_AST_BASELINE=1` 绕过——
基线容忍旧的不等于允许新增（SPEC §3.1）。

### 10.3 双轨模型策略：主线 `gpt-5.5`，Lite 专项 `gpt-5.6-luna`

`use_responses_lite` 既不由源码也不由 `config.toml` 决定，而是上游 models 响应下发的模型
元数据，没有任何配置键能覆盖。从官方 `/models` 原文实测：`gpt-5.4`／`gpt-5.5`／
`gpt-5.4-mini`／`gpt-5.3-codex-spark` 为 `false`，`gpt-5.6-*` 全系为 `true`。

主线原为 `gpt-5.4`，因候选机账号是 free 套餐、上游不下发该模型而改为 `gpt-5.5`
（两者 `use_responses_lite` 同为 `false`，主线判据语义逐字不变）。权威定义在
`capturelib.model` 的 `MAIN_TRACK_MODELS`／`LITE_TRACK_MODELS`，
`tests/test_main_track_models.py` 锁定它与 h1 探针受控 `/models`、compact 白名单、
两个候选脚本默认值四处一致。

双轨的硬约束：

1. **主升级线**只验收 `use_responses_lite=false` 的普通非 Lite 行为；
2. **Lite 专项**仅为 4 条 Lite 判据（`BODY-006/lite-*`×2、`EP-014/legacy-default-headers`、
   `EP-020`）增加独立 job，并要求每个 job 的 `/models` 原文实际证明 `use_responses_lite=true`；
3. 两条轨道必须使用独立的 `track` 标签、job、evidence root 和 receipt，禁止把 Lite 专项
   证据混入主线，也禁止把主线未触发 Lite 当成 Lite 失败；
4. 主线对 Lite-only 规则记录 `track_not_applicable`；联合 `classify` 要求两条轨道的适用
   规则均无 `blocked`；
5. 若要证明「0.145→0.147 的版本差异」，两侧必须使用同一模型并保持同一采集条件；
   只有一侧使用 Lite 模型时，结论只能是 0.147 的条件覆盖，不得写成纯版本差异。

> 这 4 条判据一度被当成「0.147 的 Lite 语义变了」。核实后两版 lite 分支返回值**逐字相同**
> ——`(String::new(), None)`，顶层字段置空后靠 serde `skip_serializing_if` 省略。

### 10.4 采集操作要点

候选验收链路的正确顺序、源码树在 run／seal 之间不可触碰、收据的两层结构、采集期间的
环境要求，以及历轮已修复的缺陷清单，见
[`CAPTURE_OPERATIONS_NOTES.md`](CAPTURE_OPERATIONS_NOTES.md)。

> **禁令类条目不写在这里。** 「采集期间不得改动环境」写进文档后，k55 仍以改 `extra`
> 的形式重犯了 k47 的错误；k61 强杀、k71 `pgrep -f` 自匹配同理——这类失败的原因不是
> 不知道，是当下没想起来。真正防住它们的是工具门禁（`environment_contaminated` 锁定、
> `_verify_plan_identity`、环境恢复写在退出路径上），带上下文的历史记录见 §10.8 总表的
> k47／k55／k61 行。本节只留两类**当场用得上**的：开跑前要查的，和失败后按症状反查的。

**开跑前必查**（两项都该脚本化，靠记得去查不可靠）

1. **18080 与 18081 两个端口**。18081 是 ingress、18080 是 mitm；前轮异常收尾会同时留下
   两个 pid_file 与存活进程，只查其一会让 mitm 轨在采集中途才暴露。
2. **`secrets/admin-token` 的 `exp`**，有效期只有 24 小时。重签用
   `docker cp state/jwtgen-bin` 进服务容器执行（要在容器内才拿得到 `JWT_SECRET`），
   输出是 `ADMIN_EMAIL=`／`ADMIN_USER_ID=`／`JWT=` **三行且带 stderr 日志**，
   必须 `grep '^JWT=' | cut -d= -f2-` 提取。

**失败后按症状反查**

| 症状 | 根因 | 处置 |
|---|---|---|
| A09／A11 流量与 pcap 正常，**只有 A12／A13／A14 actions 全空、pcap 0～24 字节** | admin token 过期（依赖管理接口的场景才受影响，故极具迷惑性） | 重签后 `resume --rerun-failed` 只补跑失败 job，不必全量重采 |
| 候选树上整包跑 `./internal/service/` 失败 | `TestChangeset5CurrentFinalWireMatchesFrozenWireFields` 冻结 `previous=0.145`，而候选树 `previous` 是目标版本 | 与候选侧改动无关（已用「纯 HEAD ＋目标画像」复现）；用 `-run` 精确过滤 |
| 等待循环永远等不到采集进程退出 | `pgrep -f <串>` 自匹配到等待循环自身 | 改用 `ps -eo cmd \| grep "[c]odex_upgrade"` 首字符加括号的写法；`pkill -f` 同理会杀掉自己的 SSH 会话 |
| 下一轮报「环境恢复失败」 | 上一轮被强杀，hosts 劫持与临时 CA 残留在候选服务容器里 | 恢复动作写在退出路径上，只能等它自己退 |
| mitm 轨中途报「已有 MITM 进程运行」 | 前轮残留 mitmdump 占着 18080 | 见上「开跑前必查」第 1 条 |

> **确认必败就立刻停，别「让它跑完」**——失败 job 会自动重试 3 次、每次都发真实请求，
> 加上仍能跑通的 direct 轨，一轮足以打满一个 free 账号的 7 天配额（k61 因此连烧两个账号，
> 见 §10.8.7）。但**停之前不能强杀**，否则就撞上表里第 4 行。

**go test 轨迹生成**

轨迹不是采集产物，要单独跑并放进 `runs/<run_id>/candidate-go-test.jsonl`。采集脚本里没有
go test，DMIT 也编译不动 backend（ent 单包峰值 1.93G > 1.9G 物理内存），须在本地或 ARM64
上跑。三项要求缺一不可：

- **跑在与镜像同源的候选树上**（§10.8.11）；跑之前先核对 mapping 声明的 13 个源码快照与
  3 个测试文件摘要与该树逐字一致；
- **包路径用精确形式**（`./internal/repository ./internal/service`），带 `/...` 会多带空包、
  与历史轨迹不同构；
- **用 `-run` 精确过滤到冻结映射内的测试**；`candidate_test_trace.py` 禁止映射外的顶层测试
  出现，且要求每个映射包恰好一次包级 pass（k56 日志只有 11 个顶层测试正是此故）。

### 10.8 逐轮不变量总表

从 k34 起每一轮作废都是在 `seal` 之前用真实证据查出来的，不是 seal 失败后回头找。
代价是重采，收益是每条缺陷都变成了机器可执行的门禁。

> 本节由原 §10.8～§10.11（四层问题的逐节诊断，共 25 个小节）与原 §11（R0～R10 实施计划）
> 合并而来，逐轮流水账不再保留。历史诊断文档
> [`ACCEPT_PIPELINE_DEFECTS.md`](ACCEPT_PIPELINE_DEFECTS.md) 与
> [`SCN_REALITY_01_SCENARIO_REALITY_GATE.md`](SCN_REALITY_01_SCENARIO_REALITY_GATE.md)
> 里引用的 §10.9／§10.9.x／§11.2 等旧编号，对应本节表格的第一行（k34～k41）。
> 各轮 campaign 的完整证据仍在采集机上，教训本身已固化进受管工具的门禁与测试注释。

| 轮次 | 死因 | 固化的不变量 |
|---|---|---|
| k34～k41 | 验收链路接线、采集覆盖、场景真实性、selector 可达性四层问题逐层剥离 | ACC-01～07；`SCN-REALITY-01` 三份场景收据；seal 预检的 selector 可达性失败关闭 |
| k42 | `EP-019` override 把 `wham/usage` 改成 `settings/user`——原判定取自 mitm 面，而修正后的 selector 选的是 relay 面 | 无 override 时画像逐字不变；`wham-get-paths` 期望锁定。**但见下方注**：这一轮的结论只回答了「不是替代」，没回答「是不是新增」，画像缺口由此埋下 |
| k43 | ① `BODY-002/responses-plain` 从来没有负样本 ② 中继路径无上游容量重试 | 逐场景比对契约 `side_coverage` 与标签产出；中继侧同款有限重试 |
| k44 | A05 失去全部标签绑定——撤销一个错误 `mode=lite` 标注时的连带效应，**场景静默失去证据** | 官方侧每个受契约要求的场景都必须有标签产出所需 kind |
| k45 | ① 模型收据不支持 WS 传输 ② `official-core` 靠工具默认值跑在 Lite 模型上 | WS 帧提取（含 deflate 上下文接管）；禁止官方 job 靠工具默认值决定模型 |
| k46 | Lite compact 整轮未触发目标请求，job 却判 `complete`——**补强收据反而拆掉了一个隐含门禁**（原先「收据生成失败」意外充当了「目标请求没发出」的检测器） | `REQUIRE_REQUEST_PATH` 显式失败关闭；`COMPACT_TOKEN_LIMIT` 参数化 |
| k47 | 官方 28/28 达成 `profile_approved`，但**候选采集期间重建 capture 容器**致环境校验失败 | **采集期间不得改动任何环境**——哪怕修的正是当前 job 的失败原因，修复必须排队到本轮结束之后 |
| k48 | 候选采集与 privacy 托管字段死结：候选流量经服务重新评估授权并原子写回 `accounts.extra`，采集必然改 extra、门禁必然判污染 | 排除项逐字取自服务端 `openAIPrivacyManagedExtraKeys`，等值匹配不用通配；双向一致性测试 |
| k49 | 候选侧承接死锁：补挂载重建容器 → `containers` 探针漂移 → `resume` 拒绝承接，而 `run` 又被「存在失败 attempt」挡住，且每次 `resume` 都留下无 after 探针的失败 attempt，越陷越深 | 环境必须在开跑前一次备齐；中途补任何一项挂载都要整轮重来 |
| k50 | 官方 28/28，但 campaign 冻结时仍缺 Live attestation 参数，候选侧 A11 无解 | A11 的两项前置写入环境前置清单 |
| k51 | 候选首次打到网关，暴露候选机账号套餐不支持主线模型 gpt-5.4 | 主线改 gpt-5.5，见 §10.3 |
| k52 | 官方 28/28，seal 拦下上游**本轮才开始下发**的 identity-signal 令牌（`ois1.eyJ…`，在 `x-oai-is-update` 头与 `oai_is` 字段）明文入证据 | 脱敏补两条按字段名的等长替换；复扫前瞻加 `ois1\.` 与 guard 对齐 |
| k53 | 候选首次跑通 5/9；`frozen-core` 卡在脚本写死的 Lite 模型 `gpt-5.6-sol`（free 账号没有） | 模型改走 `${LITE_MODEL}`／`${MAIN_MODEL}` 变量，测试锁定默认值落在两条轨道的权威集合里 |
| k54 | `frozen-core` 推进到 A03/A04/A05 全部产出 pcap；`run_sub2api_openai_mitm_matrix.sh` 建代理时把 host **写死成 `capture-cli`**，而同一脚本的 DNS 检查用的是 `$capture_container` → 出站解析失败 → 账号熔断 → 后续 job 一路 503/1013 | 代理 host 必须用变量；HTTP 与 WS 两条触发路径都必须在驱动前清熔断 |
| k55 | 候选跑通 **7/9**，`frozen-aux` 的 A09～A14 全部通过；**采集期间改账号 extra 致污染** | 同 k47，**同一个错误的第二次发生**——上一轮把纪律写进文档并没有防住，真正锁死它的是工具的 `environment_contaminated` 判定 |
| k56 | — | 官方 28/28 → 候选 9/9 首次跑满 → Kilo 两入口证据齐备 → 候选 seal 断言门禁六道全通 |
| k57 | 未建 Campaign，仅作候选源码树 | — |
| k58 | **A11 判据把上游的连接收尾方式当成失败**：官方 27/28，`official-relay-realtime-webrtc` 连续 9 次同因失败，而 wire 与成功轮逐字等价 | 判据区分「会话期错误」与「收尾重置」；新增 `teardown_error_count` 单列计数 |
| k59 | 候选 7/9 后 `environment_contaminated`：**A11 的 hosts 回灌与环境探针的自引用行剔除相互抵消**，恢复动作本身制造污染 | 回灌改为仅在容器实例未被重建时执行，见 §10.8.5 |
| k60 | 建在 DMIT 上——**官方采集只能在 Vircs**，官方 CLI 只装在那里，官方 0/28 全败 | 机器角色见 §10.0，建 Campaign 前先确认在哪台机 |
| k61 | 候选 7/9 三轮：①前轮残留 mitmdump 占 18080 ②账号 7 天配额耗尽 ③换账号时连 `extra` 的 WS 开关一起换掉。末轮**被强杀**打断环境恢复 → 污染作废 | 操作纪律见 §10.4「确认必败就立刻停，但停之前不能强杀」；换账号要点见 §10.8.7 |
| **k62** | 候选 7/9，**两处新缺陷同时暴露**：`codex_reset_credit_snapshot` 未列入豁免致必然污染；mitm 轨被上游按 TLS 指纹拒绝 | **官方 28/28 → `official_sealed` → classify 已批准 → `profile_approved`；hosts 修复第二次验证有效**。两处缺陷已修，见 §10.8.9、§10.8.10 |
| k63～k66 | 批准清单与工具树 `required` 不同源（连踩两次）；`JOB_RETRY_LIMIT` 下调误伤 official | 批准清单权威值取自 `classification/approved/scenarios.json`；重试限值 official／candidate 共用，提速只能靠超时 |
| k67 | 候选 seal 卡在 trace 生成：0.147 判据体系没做完（`candidate_rule_expectations_0_147_0.json` 从未建过，`candidate_test_trace.py` 的 FROZEN 仍指 0_145_0 组） | 新建 0_147_0 判据画像＋同批更新两个 FROZEN 与两个 DEFAULT 路径；载荷审核确认验收契约摘要不漂移 |
| k68 | seal 断言门禁：`SPEC-H1-004`／`models-order` 在候选侧选不到观测——**标签体系两侧不对称**，官方 models 走 relay 轨有 `variant=no_cookie`，候选走 h1-wire 探针无该标签 | 声明文件给 `candidate-h1-wire` 补 `variant: no_cookie`；改前用门禁自身逻辑全量预检（107 个 check）证明只此一处、补后归零 |
| k69 | accept 首次跑通（33 pass／9 fail），但 go test 跑在 k57 树上——该树画像是 k48 旧版 | 判据 4 条 selector 收紧（详见 §10.8.13），33→35 |
| k70 | `compare` 失败：official 收据的 finalizer 路径是 `candidate-source-k57`、候选是 `k59`，**两侧路径必须一致** | 见 §10.8.12 |
| **k71** | — | **全链路首次跑通至 accept**：官方 28/28 → classify 2804 项零未分类 → 候选 7/9 → seal（42 rules／108 checks）→ `compare` `complete` → accept **35 pass／7 fail**。两侧统一用 `candidate-source-k70` 树 |

下面 13 个子节按**什么时候会翻开**分两类，不必通读：

**失败时按症状反查**

| 症状 | 去哪节 |
|---|---|
| 官方 seal 报「原始证据权限不是目录 0700／文件 0600」 | §10.8.4（真根因是 `runs/` 下的 tcpdump 产物，不在 bundle 里；附修复命令） |
| 候选侧入口 503 | §10.8.6（三种互不相同的根因，靠 `excluded_account_count` 分辨） |
| `compare` 失败但两侧收据看着都对 | §10.8.12（finalizer 路径必须逐字一致） |
| 账号刚被调用就判 `environment_contaminated` | §10.8.9（豁免清单漏了 `codex_reset_credit_snapshot`） |
| classify 产出 `blocked`／`target_rule` 全 null | §10.8.3（两个卡点与继承脚本） |
| 判据选中 0 条观测，或选中了不该选的面 | §10.8.13（selector 覆盖过宽的四条实例） |

**动手前必读的纪律**

| 要做什么 | 先看 |
|---|---|
| 建候选树、跑 go test | §10.8.11 源码树必须与镜像同源（k67～k70 四轮死于此） |
| 换采集账号 | §10.8.7（不必新建 Campaign，但要连 `extra` 一起核对） |
| 处理 `TLS-003` 挂账 | §10.8.10 mitm 缺口的登记口径、§10.8.8 该判据要求的行为在 0.147 上不存在 |
| 写或改判据 | §10.8.1 五条方法论、§10.8.2（判据只能表达它要验证的那件事） |
| 修环境恢复相关逻辑 | §10.8.5（恢复动作自己制造污染） |

#### 10.8.1 五条方法论（从上表各轮死因提炼）

1. **判据失败要按固定顺序排查**：① 条件是否真的成立（采集侧有没有产出该条件的样本）
   → ② 证据面是否取对（mitm 面与 relay 面看到的是同一客户端的不同请求子集）
   → ③ 会话状态是否可比（cookie store 等在预检与正式采集里不同）
   → ④ **最后**才考虑「是不是新版行为变了」。k42 与「预检 cookie」两次误判都栽在跳步。
2. **读代码同理：先 grep 调用方，再读分支**。「函数名与症状对得上」不是证据——
   C 类根因两次栽在这里（`if !isCompact` 与 `officialCodexConditionsFromHeaders`，
   前者只有测试引用、后者只服务 models 端点）。
3. **每个门禁只能表达一件事**。k46 最典型：收据的语义是「模型条件成立」，不是
   「目标分支已触发」；靠一个门禁的副作用挡住另一类失败，迟早会在补强前者时失去后者。
4. **症状相同不等于根因相同**。C 类四条判据表现完全一致，实际分属真缺陷、设计行为、
   环境未就绪三类——按同源处理会把设计行为当 bug 改掉。
5. **在共享唯一键的环境里做验证实验，实验本身就是污染源**。排查 Kilo 不计费时，用 curl
   做的验证恰好占掉了后续真实请求要用的 `request_id` 名额，由此自造出「curl 能计费、
   Kilo 不能」的假象。

两个高价值的取证位置：账号 `temp_unschedulable_reason` 里的原始错误串（k54 的症状与根因
隔三层，只有它没被转译过）；`pg_indexes`（Kilo 静默不计费的真凶是 `usage_logs` 上的
`UNIQUE(request_id, api_key_id)`，撞键即丢、不报错不打日志、HTTP 照样 200）。

期间受管工具测试由 601 增至 652 项，新增条目全部来自这些轮次的真实踩坑。

#### 10.8.2 k58 的 A11 判据缺陷：重试解决不了的失败

`official-relay-realtime-webrtc` 在 k58 连续失败 9 次（3 个 attempt × 3 次自动重试），
错误始终是 `WebSocket protocol error: Connection reset without closing handshake`。
k56 同一个 job 也失败过 2 次、第 3 次才成功，所以最初判成「上游波动，重试即可」——
**这个判断是错的**，9 次全败后改查证据才定案。

决定性对照（同场景两轮的 relay 连接记录）：

| | k56 成功 | k58 失败 |
|---|---|---|
| realtime WS | 20406ms / 上行 **15778** | 20233ms / 上行 **15778** |
| sideband（api.openai.com） | 185ms / 上行 **2348** | 203ms / 上行 **2348** |
| 连接数 | 4 | 4 |

**上行字节逐字相同**，sideband 都已建立，连接时长都是 `hold=20s` 后再多 200–400ms——
即 reset 发生在**保持期结束、会话收尾那一刻**。两次采集的 wire 实质等价，差别只是上游
一次走优雅关闭、一次直接 reset。

而判据是这样写的：

```python
errors = [x for x in notifications if _method_of(x) == "thread/realtime/error"]
if errors:
    raise ScenarioFactsError(...)   # 任何 error 都失败，不看发生在哪个阶段
```

它要验证的本是「realtime 会话能否建立、sideband 能否绑定 call_id」，却把「上游如何结束
连接」也纳入了失败条件。**缩短 `hold` 也绕不过去**：reset 发生在会话结束那一刻，与保持
多久无关——这正是重试无效的原因。

修复保持 fail-close，只豁免可证明无害的那一段：

- error 出现在 `started`／`sdp` **之前** → 会话没建起来，**仍然失败**；
- error 之后还有非关闭类通知 → 会话仍在进行，**仍然失败**；
- 仅当目标事实全部达成、其后只剩 `error`／`closed` → 计入 `teardown_error_count` 放行。

k59 的实际收据即 `async_error_count=0`、`teardown_error_count=1`、
`final_state=sideband_established`——**收尾重置被如实记录而非掩盖**。

> 由此固化一条：**判据只能表达它要验证的那件事**。A11 要证明的是会话与 sideband 的建立，
> 不是连接的结束方式；把后者写进失败条件，等价证据就会被拒，且重试永远无法通过。
> 这与 §10.8.1 第 3 条「每个门禁只能表达一件事」是同一条纪律的正面案例。

> **k42 那条的后续（2026-08-12）**：当时撤销 override 的结论是「0.147 并未用
> `settings/user` **替代** `wham/usage`」——这个结论本身没错，官方 0.147 实测三个路径并存。
> 但排查在此停住了：**否定了「替代」，没有人接着问「那是不是新增」**。于是判据侧后来
> 按新路径做了适配（`wham-get-paths` 纳入、`wham-get-headers` 排除），画像侧却一直没有
> 补端点，缺口从此埋了十几轮而每轮 digest 复算都「一致」。
>
> 由此多一条不变量：**推翻一个假设不等于查清事实**。撤销错误 override 之后，必须回到
> 官方证据把该路径的真实地位（替代／新增／无关）定下来，并同步落到画像与判据两侧；
> 只在判据一侧改，两份清单就会在同一次 classify 里互相矛盾，而这种跨清单矛盾此前
> 没有任何门禁在查。

#### 10.8.3 正式轮走 classify 的两个卡点（2026-08-12 实证）

**一、`dynamic` 发现不能跨轮继承，只有 `source` 可以。**

批准 classify 要求提交的分类逐条覆盖本轮草案的发现指纹。直接套用上一轮的
`rule-migration.json` 会被拒：

```
源码／动态增删形态未唯一分类；缺失=[136 条 dynamic]，多余=[135 条 dynamic]
```

原因在 `expected_discoveries` 的构成——`dynamic` 那部分取自
`official_diff`（本轮官方采集的 `baseline-to-target-official.json`），**指纹随本轮证据变化**；
只有 `source`（源码 diff）在源码树未变时逐条相同。§10.1 说的「指纹逐条相同可继承」只对
后者成立。

可行做法是从上一轮反推映射规则再套用（k56→k59 实测 202 条全中、零遗漏）：

| 差异 `kind` | 目标规则 |
|---|---|
| `tls_client_hello` | `SPEC-TLS-001` |
| `websocket_frame` | `SPEC-WS-004` |
| `http_request` | 按 path 细分：`/ps/*`→`SPEC-HDR-008`、`/codex/responses`→`SPEC-EP-021`、`/wham/*`→`SPEC-EP-019`、`/oauth/token`／`/files`→`SPEC-EP-002`、`/codex/models`→`SPEC-EP-006`、`/responses/compact`→`SPEC-EP-007`、`/alpha/search`→`SPEC-EP-008`、`/v1/live/{id}`→`SPEC-EP-012` |

`source` 的 2604 条按指纹继承上一轮结论，`entries`（42 条规则）同理。

**二、`profile_id` 必须三处一致。** `scenarios.json` 与 `profile.json` 的 `profile_id`
不一致会被 `目标场景清单 profile_id 与运行画像不一致` 拒绝。改 `profile_id` 只影响清单
文件摘要，**不影响 `profile_digest`**（后者是 payload 的摘要）——改完务必复核这一点。

#### 10.8.4 官方 seal 的权限门禁：文档原先记错了根因

原记「`build_assertion_bundle.py:190` 把收口文件写成 `0400`……k56 实测亦未阻断 seal 门禁，
不构成正式轮前置」——**两处都不准确**：

- 它**确实阻断**官方 seal（`原始证据权限不是目录 0700／文件 0600`）；
- 但 `0400` 并不是原因。校验实为 `st_mode & 0o077`（只看 group/other 位），`0400` 合法。

真正卡住的是另外 9 个条目：tcpdump／pcap 落盘的 `644` 文件与 2 个 `755` 目录，
分布在 `runs/<run-id>/` 下（**不在 attempt 的 `evidence/` 里**，只查 bundle 会漏掉）。

处置是只修已产出文件的权限位、**不动工具**——问题不在工具内容，改工具反而会让整轮作废：

```
find <runs>/<campaign-id>-* -type d -perm /077 -exec chmod 700 {} +
find <runs>/<campaign-id>-* -type f -perm /077 -exec chmod 600 {} +
```

#### 10.8.5 k59 作废的根因：恢复动作自己制造污染（已修复并实证）

`candidate-frozen-aux` 首次真正跑通后，attempt 立刻被判 `environment_contaminated`，
`configuration_state_restored` 一项失败，且**只有 service 容器的 `hosts_sha256` 偏离**
（keeper 与 capture 都仍等于基线）。

两处受管代码的假设正好相反：

- **环境探针**（`codex_upgrade_environment_probe.py::_container_hosts_digest`）靠
  「主机名等于当前容器 ID」识别并剔除 Docker 写的自引用行
  （`<容器 IP> <实例 ID 前 12 位>`），**专为吸收 A11 必然发生的 compose 重建**；
- **A11 的恢复路径**（`run_candidate_aux_capture.sh`）却把运行前的 `/etc/hosts`
  **按字节回灌**进重建后的新容器，注释理由是「否则复核必然不一致」。

回灌后自引用行带的是**旧**实例 ID，探针认不出、只能按「人为写入的劫持行」保留，
after 摘要必然偏离 before。**恢复动作本身就是污染源**，重试多少次都一样。

之所以拖到 k59 才暴露：`frozen-aux` 长期因缺 `ADMIN_BEARER_TOKEN` 而 rc=2 提前退出，
从未走到「compose 重建容器」这一步。

修复：仅在容器实例未被换掉时才按字节回灌；被 compose 重建时不回灌
（Docker 新生成的 hosts 天然不含采集期劫持行），并新增 `hosts_digest_excluding_self`
把自查改成与探针同语义——该函数与探针实现**实测逐字节等价**。

k61 实证：`frozen-aux` complete，三个容器的 before／after 摘要**全部等于基线**。

#### 10.8.6 candidate 侧的账号与配额：三个各自独立的失败面

k61 候选侧连续三轮停在 7/9，`core-mitm` 与 `compact-mitm` 始终未过。**三次的根因互不相同**，
都表现为入口 503，必须靠 `ops_error_logs` 的 `error_phase`／`account_id`／
`upstream_status_code` 三个字段分辨：

| 现象 | 定性字段 | 根因 |
|---|---|---|
| `已有 MITM 进程运行，PID=…` | — | 前轮异常收尾遗留的 mitmdump 占着 **18080**；前置检查只查 18081 会漏 |
| `routing`／`acct=90`／`up=429`／`excluded_account_count=1` | 账号被排除 | 采集账号 **7 天配额耗尽**（`openai_429_7d_limit_exhausted`），组内无备用账号 |
| `routing`／`acct=-`／`up=-`／`excluded_account_count=0` | **候选集为空** | 账号 `extra` 的 WS 开关被关（`…websockets_v2_enabled=false`／`_mode=off`） |

`excluded_account_count` 是区分后两者的关键：**1 = 有账号但被排除（配额／熔断）；
0 = 压根没有候选账号（开关、分组、映射类配置问题）**。

#### 10.8.7 换采集账号：不必新建 Campaign，但要连 `extra` 一起核对

`campaign.json` 的 `codex_account_id` 记的是**账号 ID**，不是凭据。因此换账号有两条路：

- **改 campaign**（新建一轮）——`campaign.json` 的摘要一变，`_load_stage_result` 会以
  「未绑定当前 Campaign」拒收已封存的 official／classify，**整轮重来**；
- **换该 ID 上的凭据**（推荐）——campaign 一个字不动，已封存阶段全部保留。

第二条路的两个要点：

1. **不要改主键 ID**。`accounts` 被 `usage_logs`／`account_groups`／
   `scheduled_test_plans` 以外键引用，改 ID 会牵连历史用量归属；对调两条记录的
   `name`／`credentials`／`extra`／限流状态即可达成完全相同的效果。
2. **`extra` 不能整体照搬**。它同时装着「账号自身的用量窗口」与「采集所需的开关」，
   后者必须按采集配置补回，否则网关在 routing 阶段直接 `no available accounts`：

   ```
   openai_oauth_responses_websockets_v2_enabled = true
   openai_oauth_responses_websockets_v2_mode    = ctx_pool
   openai_compact_supported                     = true
   ```

**换账号后必须立刻停掉正在跑的采集**。工具会正确判定
「上一轮结束后环境已漂移（account），被承接任务的证据前提不再成立，请不带
`--rerun-failed` 整轮重采」，而此时 `run` 又被「存在失败 attempt」挡住——两者互斥，
须先归档失败 attempt 再整轮重采。若放任那一轮跑完，它会**用新账号跑一轮注定失败的采集**：
每个 job 失败自动重试 3 次、每次都发真实请求，一轮足以打满一个 free 账号的 7 天配额。
k61 即因此连烧两个账号，两者均须等到配额窗口重置。

#### 10.8.8 第 3 条 `extension-order-diversity`：判据要求的行为在 0.147 上不存在

判据（`candidate_rule_expectations_0_145_0.json:145`）：

```
描述：四份独立 WS 抓包扩展集合一致且至少出现两种排列
select：record_type=tls_client_hello，labels.transport=websocket
assertion：same_set_distinct_order，minimum_records=4，minimum_artifacts=4，
           minimum_distinct_orders=2
```

k61 实证（`tshark -Y "tls.handshake.type==1" -e tls.handshake.extension.type`）：

| 侧 | 有效 WS 抓包 | ClientHello 扩展顺序 | 排列种数 |
|---|---|---|---|
| 候选（`direct-core` 3 ＋ `ws-repeat` 3） | 6 份 | `65281,0,11,10,35,22,23,13,43,45,51` | **1** |
| 官方（oauth 根 ＋ `-ws-repeat`） | 6 份 | `65281,0,11,10,35,22,23,13,43,45,51` | **1** |

两侧**逐字节一致**。`minimum_records`／`minimum_artifacts` 早已满足（各 6 份 ≥ 4），
唯一不满足的是 `minimum_distinct_orders=2`——**而官方 0.147 自己也只有一种排列**：
其 TLS 栈（rustls）不做扩展顺序随机化。

由此推翻原记载的两点：

- **根因不是「漏了 s3、份数不够」**。份数从来都够；
- **加 s3 也解决不了**。同一客户端同一 TLS 栈，s3 的 ClientHello 与 s1／s2／s4 完全相同，
  只会多跑一个场景、多消耗配额。

顺带查明：那处修复（`run_sub2api_direct_matrix.sh` 默认值改 `s1 s2 s3 s4`）**从未生效**——
三个 WS 相关 job（`candidate-core-direct`／`candidate-ws-handshake-repeat`／
`candidate-core-mitm`）的 `environment` 都写死 `SCENARIOS: "s1 s2 s4"`，
而 `_run_job_step` 是 `os.environ.copy()` 后再 `update(step["environment"])`，
**job 定义永远覆盖脚本默认值**。改脚本默认值对受 Campaign 调度的 job 一律无效。

处置：**不改 job 定义、不作废 k61**，按 §6.4 的 classify `change` 流程调整判据
（与 `SPEC-TLS-003` 此前的处理同源）——它属于评估侧，不触碰产出侧工具身份。
判据若维持原样，会把「候选与官方逐字节一致」判成失败。

#### 10.8.9 `codex_reset_credit_snapshot`：豁免清单漏一个键，账号一被调用就必然污染

k62 候选侧 7／9，`hosts` 三容器 before／after 全等（§10.8.5 的修复再次生效），
但 attempt 仍被判 `environment_contaminated`，`account-state` 有且只有一处差异：

```
extra_digest: ca7f7d34def0d6b4… → 9e87a9bf620d6dd3…
```

差异源是 `codex_reset_credit_snapshot`，值从 `{"available_count": 0}` 变为含 credits 明细。
它由 `OpenAIQuotaService.CacheResetCreditsSnapshot`（`openai_quota_service.go:311`）
写入 `accounts.extra`，与已豁免的 `codex_5h_*`／`codex_7d_*` 同类——**账号被真实调用后
由服务端自动刷新的配额缓存，不表达任何采集副作用**，但探针的
`ACCOUNT_MUTABLE_EXTRA_KEY_PATTERNS` 没有收录它。

实证排除了「人为操作导致」：采集窗口（10:02–10:47Z）内**没有任何管理端 quota reset
调用**，唯一的非 GET 请求是采集自己发的 `/v1/images/generations`，而账号
`updated_at` 落在窗口内（10:46:19Z）。**只要账号被用过，这一轮就必然被判污染**——
前几轮之所以没暴露，是都在更早的环节就挂了，没走到 after 探针。

修复：把该键加入豁免，**Python 常量与 SQL 排除条件两处必须同步**
（`codex_upgrade_environment_probe.py` 的 `ACCOUNT_MUTABLE_EXTRA_KEY_PATTERNS`
与同文件的 `extra_entry.key = …` 条件），只改一处会出现「自查通过、探针判漂移」。

#### 10.8.10 mitm 轨被上游按 TLS 指纹拒绝：登记为已知缺口，规则覆盖零损失

`candidate-core-mitm`／`candidate-compact-mitm` 在 k59～k62 从未跑通。k62 首次拿到
干净现场：两个 job 都是 `subprocess.TimeoutExpired`（CLI 300 秒被杀），而 mitm 侧日志显示

```
server closed connection : 19 次
Client disconnected      :  7 次
200 OK                   :  1 次      ← 27 次请求只成功 1 次
```

`server closed connection` 是**上游主动断开 TLS**，连状态码都没返回；TLS 握手本身成功
（`server connect chatgpt.com:443` 有记录）。同一账号、同一出口 IP、同一批场景下
**direct 轨四个 job 全部一次过**，唯一差别是 mitm 轨由 mitmproxy 代替 CLI 握手——
**TLS 指纹不同**，上游据此拒绝。链路延迟已排除（直连 0.025s vs 经隧道 0.036s，
差 10ms，而 300 秒是卡死不是慢）。

处置：两个 job 改 `required: false` 登记为已知缺口。**规则覆盖零损失**——
它们覆盖的 29 条规则与其余 7 个 job 的覆盖集做差集为空：

| | 规则数 |
|---|---|
| 两个 mitm job 覆盖 | 29 |
| 其余 7 个 job 覆盖 | 42 |
| **仅 mitm 独有** | **0** |

direct 轨采的是同场景的加密 wire 与 relay 字节，本就是同一批规则的证据来源。

#### 10.8.11 候选源码树必须与镜像同源——k67～k70 连续四轮的共同死因

`--candidate-source` 传的树决定三件事：attempt identity 的 `source_tree_sha256`、
go test 解析画像的 `previous` 槽位、以及收据里的 finalizer 路径。四轮里它一直指向
`candidate-source-k57`，而候选镜像 `sha256:fe601448` 是用 **k59 树**构建的
（`build_id=codex-0.147.0-official-k59-v1-94071c8eb93c`）。两棵树装的画像根本不同：

| 树 | `catalogdata/runtime/profiles/0.147.0/` | 端点数 | `0145_profile.go` | 能否编译 |
|---|---|---|---|---|
| k57 | `0d86e033…`（**k48 旧画像**） | 16 | 含 `WhamSettingsUser` 定义 | 能 |
| k59（原始） | `94071c8e…`（本轮画像） | 17 | **缺定义** | **不能** |
| k70＝k59＋补定义 | `94071c8e…` | 17 | 含定义 | 能 |

后果是 go test 在 k57 树上按 previous 槽位解析到 k48 旧画像，产出
`codex_exec/0.145.0`，`SPEC-HDR-005` 因此判失败——**整份 go test 轨迹都建立在错误画像上**，
不只那两条。k70 树上重跑立刻变成 `codex_exec/0.147.0`，两条转 pass。

判别方法（三者必须一致，缺一即停）：

1. `attempt.json` 的 `identity.source_tree_sha256`
2. 运行时 `/tmp/egress-activation-fact.json` 的 `source_tree_sha256`（由 override 声明注入）
3. `_directory_tree_digest(--candidate-source)` 实测

镜像里到底有什么，用 `strings` 直接验，别靠推断：本轮就是靠
`strings /app/sub2api | grep -c wham_settings_user` 得到 2 处、`settings/user` 路径 1 处，
才确认镜像含完整逻辑、现存 k59 树是被回退过的那一份。

#### 10.8.12 两侧 finalizer 路径必须一致——`compare` 的隐藏前置

收据的 `producer.tool.path` 记的是**生成它的 finalizer 绝对路径**，`compare` 会要求
它等于当前运行的那一个。official 在 Vircs 跑、候选在 DMIT 跑，只要两台机器用的树名不同，
路径就不同，`compare` 必然报「环境恢复报告无法由机器 finalizer 重放」，
**且无论在哪棵树上跑 compare 都会有一半对不上**。

两份 finalizer 内容完全一致（`06fe9886…`）也没用——校验的是路径字符串。

纪律：**两台机器用同名同路径的源码树**。本轮统一为
`/root/oauth-capture/candidate-source-k70`，两侧工具身份同为 `d2a3dae4…`（产出侧 91 项）。
树名按 campaign 轮次走容易混淆——树编号与 campaign 编号是两套，复用旧树时务必显式确认
它与镜像、与对侧机器是否一致。

#### 10.8.13 判据 selector 覆盖过宽：四条 check 的失败与画像无关

k69 accept 的 9 条失败里有 4 条并非画像行为问题，而是 selector 选错了证据：

| check | 选中了什么 | 事实 | 收紧方式 |
|---|---|---|---|
| `SPEC-TLS-001`／`cipher-count` | A01 direct 面全部 ClientHello | 15 条里 12 条是 `chatgpt.com`／30 cipher 全合规，3 条是容器里跑到 **`api.github.com`** 的无关连接（Go 标准库 13 cipher） | `where` 加 `data.sni = chatgpt.com` |
| `SPEC-TLS-003`／`extension-order-diversity` | WS 面全部 ClientHello | 每份 pcap 混入 1 条 github 噪声，把扩展集合分组打散 | 同上 |
| `SPEC-BODY-001`／`http-closed-fields` | A03/A04 的 responses 请求 | 10 条里 8 条完全合规，2 条 `<absent>` 来自 **h1 探针面——该面按设计只记请求行与头，不产出 body** | `where` 加 `labels.surface absent`（只保留字节中继面） |
| `SPEC-EP-022`／`generation-body-order` | images/generations 请求 | relay 轨那条 body 完整，probe 那条为空 | 同上 |

两个要点：

- **`not_equal` 在字段缺失时判为不匹配**（实现是 `actual is not MISSING and actual != expected`）。
  想排除 probe 面不能写 `surface != probe`——字节中继面根本没有 `surface` 标签，会被一起排除掉，
  结果选中 0 条。正确写法是 `absent`，与 §10.8.3 里 `SPEC-PROTO-001` 用 `ca_mode absent`
  只保留原始字节同构。
- **改判据前先用门禁自身的加载逻辑全量预检**，别靠手写脚本近似：
  `load_observations()` ＋ `_select_observations()` 遍历全部 check，改前改后各跑一次，
  确认目标 check 归零且不引入新的选不到项。本轮 k68 补标签、k69 收紧 selector 都是这么验的。

候选 seal 在本次升级之前从未有人走通，k56 作为试验田把这条链路逐段摸开。
**组件清单、四路输入的路径基准、labels 语义与打通门禁本体的四处修复，已移入**
[`CAPTURE_OPERATIONS_NOTES.md`](CAPTURE_OPERATIONS_NOTES.md)——它们是跨 Campaign 的操作
知识，不随本轮状态变化。下一轮正式跑按那份清单准备。

k56 跑通结果：bundle provenance 重放 81 项、派生收据重放 62 项、capture manifest
154 artifact／377 观测、分侧 kind 覆盖与 selector 可达全过，`checked_rule_count=42`、
`checked_check_count=108`。
