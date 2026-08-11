# Codex CLI 0.145.0 → 0.147.0 Official Egress 升级计划（执行中）

> 状态：Campaign `codex-0145-to-0147-20260811T034436Z-k47` 于 2026-08-11 依次达成
> **`official_sealed`** 与 **`profile_approved`**，官方取证与分类全部完成。
> 当前 `next_command: capture-candidate`——**跨入服务部署域，须先按 §11.0 完成 ARM64
> 服务侧验证，不得直接动 Vircs 正式实例**。
> k34～k46 仅保留为历史诊断夹具，不迁移、不复用、不续跑（唯一例外见 §10.11.10 的
> source 发现继承）；**k47 的 sealed 证据与已批准清单不可变，其间不得修改任何受管工具**。
> 五轮作废的死因与由此固化的门禁见 §10.11.11。
> Active 仍为 0.145，0.147 尚未替换。
> 创建日期：2026-08-07
> 审阅基线：commit `abf236375f66aa096580092e646c4e33d37bb135`
> 基线画像：Codex CLI `0.145.0`
> 目标画像：Codex CLI `0.147.0`

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

只有 DOC-PRE、P0 和实证阻断修复均完成，工作区干净且工具摘要不再变化，才可创建
正式 Campaign。Campaign 目录必须持久、绝对、尚不存在，不能使用 `/tmp` 或符号链接。
创建后任何工具漂移都必须拒绝继续。

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
（`0d86e033…`）跨轮次恒定，这正是画像内容未漂移的证据。

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
Campaign `…-k22` 不可共存：按 §6.1 与 §7.3 先例重新建立独立 Campaign 并重做官方取证与
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

### 9.2 回滚

逐规则断言、digest/inventory/seal、Guard、账号、health、恢复或 secret scan 任一失败，
或出现旧画像静默兜底、跨 Bundle fallback、连接池混用，立即切回 Previous + 旧镜像。

回滚不删除 Campaign、不覆盖 0.147 Snapshot、不重建数据容器。回滚后重新检查服务、
挂载、账号、keeper、代理/CA、新旧入口和 final-wire，并保留失败证据。

## 10. 当前执行状态

> 时间：2026-08-11。当前 Campaign `codex-0145-to-0147-20260811T034436Z-k47`，
> campaign_id `codex-0_147_0-20260811T034439Z`，官方 attempt
> `20260811T034557Z-05b06ad48567bba8`，状态 **`profile_approved`**，
> `next_command: capture-candidate`。
> **Active 仍为 0.145，0.147 尚未替换。**

### 10.1 按章节的真实进度

**当前位置：§6.4 已完成，停在 §8 candidate 之前。** k47 是双轨采集下第一个走通的
Campaign：官方 28／28 job 一次跑通、九份收据齐备、seal 前 selector 缺口归零，随后
classify 以 `blocked=0` 达成 `profile_approved`。下一命令 `capture-candidate`
**跨入服务部署域**，须先按 §11.0 完成 ARM64 验证，且不得直接动 Vircs 正式实例。

| 章节 | 状态 | 依据 |
|---|---|---|
| §5 DOC-PRE 与 P0 | 完成 | 不依赖 Campaign，结论长期有效 |
| §6.1～6.3 Campaign／官方取证／seal | **完成** | k47 官方采集 28／28 job 全 `complete`；6 份模型条件收据（主线 3×`gpt-5.4`／非 Lite，专项 3×`gpt-5.6-luna`／Lite）＋ 3 份场景收据；秘密扫描 986 文件／163 MB 零命中；环境五项全 `restored`、数据库 426 主键零缺失；`official_sealed` 达成 |
| §6.4 classify | **完成** | 42 条规则 41 `inherit`＋1 `change`；2808 条发现 2604 `change`＋204 `condition_change`；`unclassified_count=0`、`blocked=0`；目标画像 57 PASS／0 FAIL／0 UNREACHABLE；`profile_approved` 达成，详见 §10.11.10 |
| §7 建立 0.147 画像 | **完成** | `profile_digest=0d86e033…`，与跨轮基准逐字一致（第七次独立复算）；五份清单已批准封存 |
| §8 Candidate／比较／`ready` | **当前** | `next_command: capture-candidate`；前置见 §11.0 |
| §9 生产启用与回滚 | 未开始 | |

k34 曾到达 `compared`，但 ACC-01～07 改变了受管工具身份，k34 已作废并停在 `compared`，
其证据只用于根因复现与离线夹具，不迁移。**ACC-01～07 全部发生在「§6.2 seal 之前」这
一个点上**，是让 seal 有可能通过的前置修复，不构成章节推进。

**k34～k46 全部作废、证据不迁移不复用**，各自死因与由此固化的不变量见 §10.11.11 的总表。
唯一例外是 k34 已批准的 `discovery_classifications`：k47 的 source 发现与它**指纹逐条
相同**（同一对源码树），故 classify 时直接继承并逐条校验命中，见 §10.11.10。

0.147 画像 digest `0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f`
由 k26～k34 六次独立产出逐字一致，k47 第七次复算仍逐字相同，是画像内容未漂移的直接证据。

### 10.2 采集操作要点

候选验收链路的正确顺序、源码树在 run／seal 之间不可触碰、收据的两层结构、采集期间的
环境要求，以及历轮已修复的缺陷清单，见
[`CAPTURE_OPERATIONS_NOTES.md`](CAPTURE_OPERATIONS_NOTES.md)。这些不随单个 Campaign
变化，采集前必读。

### 10.7 剩余路径（k47 之后）

前六步已随 k47 完成（双轨契约冻结 → 官方重采 → `seal` → classify → 画像批准）。剩余：

1. **保持 k47 的 sealed 证据与已批准清单不可变**，不在 k47 上续跑官方 `run` 或改受管工具
   ——工具漂移会让本轮 sealed 证据无法继续使用；
2. 按 §11.0 先在**独立 ARM64 环境**完成 Sub2API 服务侧验证（编译、单测、启动、健康检查、
   HTTP／WS、迁移与恢复、出站 TLS／代理／超时、并发与可回滚性）；
3. `stage-profile` 把已批准画像编译成**不切 Active** 的候选 RuntimeCatalog；
4. `capture-candidate run/seal`：在**独立候选实例**上采集，注意 §10.2 的三条硬约束
   ——验收链路顺序、源码树在 run／seal 之间不可触碰、收据的两层结构；
5. `compare` 离线比较官方与候选证据，`accept` 执行逐规则验收门禁，达成 `ready`；
6. 最后按 §9 在 Vircs 部署独立 canary，通过后才更新正式实例；失败保留证据并回滚。
   最终 Active=0.147 / Previous=0.145。

**待办（须等 k47 整条流程走完再动）**：`build_assertion_bundle.py:190` 把收口文件写成
`0400`，而 seal 的原始证据预检要求文件 `0600`／目录 `0700`，两处硬编码常量彼此矛盾
（详见 §10.11.9）。修它会改工具身份，因此**不能在 k47 的 candidate／compare／accept
走完之前进行**。

本节之前关于 k36→k37、k41→k42 的步骤保留为历史审核记录，不再作为当前执行指令。

### 10.8 第一层：验收链路接线（已闭环）

`accept` 从未走通的根因是六项接线缺陷：capture-manifest 覆盖不全且标签语义错位、
采集多根而断言单根、断言证据路径与 inventory 路径空间不一致、`seal` 不校验场景
artifact 覆盖、旧 `accept` 错把两类规则强制成同一种双侧模型、正负例字段没有可执行
语义。六项已全部修复（ACC-01～07），并在 k35 真实证据上验证：编目 94 项收口、
105 项派生、199 个 artifact，`SPEC-TLS-001` 三条 check 全部通过——历史上第一次有
官方侧 wire 规则在真实 0.147 证据上跑出 pass。

完整诊断、最小安全验收模型与实施记录见
[`ACCEPT_PIPELINE_DEFECTS.md`](ACCEPT_PIPELINE_DEFECTS.md)。

**该文有一个未经验证的前提**：「证据齐备，只是接线错了」——其实证只覆盖单条规则、
单个证据根。修好接线、真正跑通编目后暴露出第二层问题，见 §10.9。两层不是同一件事：
接线不通就跑不到编目，跑不到编目就看不见证据缺口。

### 10.9 第二、三层：采集覆盖、判据与场景真实性错配（已解除）

修好验收链路、真正跑通编目后先暴露第二层问题：**场景定义的
`required_artifact_kinds` 与 capture job 实际产出的证据类型系统性错配**。k36 又暴露
第三层问题：**job 脚本退出成功不等于目标协议分支已被触发**。与 §10.8 不是同一层——
接线不通就跑不到编目，证据类型不匹配就无法断言，而目标分支未触发则产出的只是无关证据。

根因与第一层相同：历史 25 个 Campaign 无一产出过 `results.json`，链路错配与证据错配
两层问题一直并存，只能逐层剥离。

> **✅ 已解除（k39，2026-08-10）**：8 个缺口全部解决。A11／A13／A14 在 §11 的
> R1～R4 完成后于 k39 首次同时成立，三份场景收据齐备（`sideband_established`、
> `token_refreshed`、`upload_chain_complete`），19／19 job 通过。原始诊断与方案见
> [`SPEC_EP_002_EVIDENCE_BLOCKER.md`](SPEC_EP_002_EVIDENCE_BLOCKER.md)。
> **但 `seal` 仍未执行**——k39 在 ACC-03 的 seal 预检处暴露了性质不同的第四层问题，
> 见 §10.10。

#### 10.9.1 缺口清单（k35 官方侧实测，8／15 场景）与当前状态

| 场景 | 要求 kinds | 实际可编目 | 缺 |
|---|---|---|---|
| A01 | pcap, relay_binary | pcap | `relay_binary` |
| A05 | relay_binary, websocket_trace | 无 | 两者 |
| A06 | relay_binary, websocket_trace | 无 | 两者 |
| A11 | pcap, process_trace, relay_binary | process_trace, relay_binary | `pcap` |
| A12 | relay_binary | pcap, process_trace, wire_dump | `relay_binary` |
| A13 | pcap, relay_binary | **无任何证据** | 两者 |
| A14 | pcap, process_trace, relay_binary | process_trace, relay_binary | `pcap` |
| A15 | process_trace, relay_binary | process_trace, wire_dump | `relay_binary` |

典型成因：

- **A01** 绑定 `official-core`，而该 job 只做 direct／mitm 采集，不产 relay 字节；
- **A05／A06** 要求 `websocket_trace`，但没有任何 job 产出该 kind；
- **A13**（OAuth refresh）官方侧完全没有 job 覆盖——§7.7 记录过它属候选侧辅助采集。

按 §10.9.3 定案实施并在 k36 重采后的状态：

| 场景 | 处置 | 结果 |
|---|---|---|
| A01／A05／A06／A12／A15 | 编目修正／场景定义修订 | **已解决** |
| A11／A13／A14 | 补采集（新 job ＋ 开启抓包） | **未解决**，见 §10.9.4 |

#### 10.9.2 SPEC-TLS-003 判据缺陷（同类问题的独立实例）

判据 `same_set_distinct_order` 要求全部选中记录的扩展集合相等、来自 ≥4 个 artifact。
实测：每份 `codex-ws` pcap 都同时含 native-tls（30 cipher／11 扩展）与 rustls
（10 cipher／10 扩展）两种握手——Codex CLI 每次启动都拉 models，那是 HTTP 握手，
必然与随后的 WS 握手落进同一份 pcap。集合不等，判据恒假；补采同构 pcap 无效。

**不能靠标签分流绕开**：可用于分流的信号（cipher 数、扩展集合、ALPN）恰好是共用
`labels.transport` 的 `SPEC-TLS-001`／`SPEC-PROTO-001` 正在验证的属性，用它分流会使
判据退化为同义反复。加密抓包下不存在第四个独立信号；抓包窗口覆盖整个 case，采集侧
也无法分离。

**定案**：走 §6.4 规则变更流程，classify 阶段标 `change`，把断言算子改为**存在性子集**
语义——在选中记录中存在一个大小 ≥`minimum_records`、来自 ≥`minimum_artifacts` 个
artifact、集合相同、顺序种类 ≥`minimum_distinct_orders` 的子集。不降低判据强度，
只是不再因混入其他栈的握手而恒假。完整实证见
[`SPEC_TLS_003_JUDGMENT_DEFECT.md`](SPEC_TLS_003_JUDGMENT_DEFECT.md)。

配套增量（已实施）：新增 `official-ws-handshake-repeat` 与
`candidate-ws-handshake-repeat` 两个 job，用独立 batch-id 与独立证据根重跑
`codex-ws direct`，把 A02 的 artifact 数由 3 提升到 6。

#### 10.9.3 逐场景定案（已实施）

每个缺口按三类判定，**不由执行者临时决定**（历轮 capture-manifest 由执行者手写正是
缺陷来源）：

| 场景 | 判定 | 处置 |
|---|---|---|
| A05／A06 | 编目漏标 | relay 字节里本就到处是 WS 升级（relay-compact 12 个、各 job 1-2 个）。`rsv1_deflate` 只能从原始帧得到（mitm 记录已解压），故绑到 turnstate-compact（Lite）与 runtime-metrics（非 Lite） |
| A12 | 编目 kind 选错 | `request.bin` 是明文 H1 请求字节，`wire_dump` → `relay_binary` |
| A01 | 场景定义与采集形态不符 | `[pcap, relay_binary]` → `[pcap, process_trace]`：official-core 是 direct+mitm 矩阵，不产字节中继，明文观测经 mitm 派生 |
| A15 | 同上 | `[process_trace, relay_binary]` → `[process_trace]` |
| A11／A14 | 真缺证据 | realtime-webrtc 与 file-upload 开启 `CAPTURE_CLIENT_HELLO` 抓包 |
| A13 | 真缺 job | 新增 `official-relay-oauth-refresh` |

**relay 劫持不影响 SNI 证据**：中继按 hosts 把域名劫持到 127.0.0.1，但 SNI 由客户端
填写、与实际 IP 无关，因此抓到的域名就是 CLI 真实意图连接的
`api.openai.com`／`auth.openai.com`／`*.oaiusercontent.com`——SPEC-EP-002 正是验这一点。

**k36 已推翻 A13 的原触发假设**：只把 `auth.json` 的 `last_refresh` 提前并不会触发正常
JWT 的刷新。0.147 源码先读取 access token JWT 的 `exp`，只有无法取得有效期时才回退到
`last_refresh`（正式冻结树 `login/src/auth/manager.rs:2762-2783`）。因此该操作虽会逐字还原凭据，
却没有建立 A13 场景，不能再标为有效的 I 类触发。

定案后 15 个场景全部有 job 绑定，工具身份随之变化，k35 作废并按 §6.1 新建 k36
（28 个 job）重做官方取证。

#### 10.9.4 A11／A13／A14：job 完成，但目标场景未成立

k36 attempt 的 19 个 job 都结束并由编排器记为 `complete`，但三个补采脚本会吞掉目标
分支失败，故 `19/19` 只能表述为 **job 编排完成**。逐场景审核结果：

| 场景 | 实际观测 | 源码核对 | 结论与后继成功条件 |
|---|---|---|---|
| A11 realtime sideband | `POST /backend-api/codex/realtime/calls` 返回 400：`invalid_quicksilver_alpha_header`；无 `call_id`，随后只有 `thread/realtime/error` | 0.147 版本映射和 V1/V3 header 分别见 `core/src/realtime_conversation.rs:1297-1335,1647-1661`；sideband 默认 API base 与 call-id join 见 `codex-api/src/endpoint/realtime_websocket/methods.rs:709-791,976-992` | 显式走官方 V3，等待异步 started/SDP 或 error；只有 call-create 2xx、取得 `call_id`/SDP 且出现 `api.openai.com` SNI 才算成功 |
| A13 OAuth refresh | 只发生 models／Responses／WHAM 业务请求，无 refresh 请求 | 正常 JWT 的 `exp` 优先于 `last_refresh`（`login/src/auth/manager.rs:2762-2783`） | 在隔离采集账号和隔离 `CODEX_HOME` 中等待自然进入 5 分钟刷新窗口；只有真实 `POST auth.openai.com/oauth/token` 与对应 SNI 同时出现才算成功 |
| A14 file upload | CLI 明确报告当前接口没有可调用的 `save_site_version`；无 `/backend-api/files` 请求 | 文件 create→PUT→uploaded 完整顺序见 `codex-api/src/files.rs:126-275` | 先冻结模型可见的 Apps 工具调用契约；只有 create、响应 host、区域 SNI、uploaded 2xx 和三事件顺序成立才算成功；官方 pcap 不声称取得加密 PUT URL |

pcap 与中继记录一致，只能证明**本次目标连接没有发生**，不能证明规则不可观测，也不能
证明当前抓包对任意区域上传主机都完备。A14 目前只在 loopback 抓包，且 `RELAY_HOSTS` 预列
固定区域主机；这与规格要求的“按端口捕获所有主机”仍有差距。

**审核决定**：

1. 保留 `SPEC-EP-002` 的三条必现 SNI 判据，不采用“未观测即通过”的条件化方案；
2. k36 作为诊断夹具保持 `awaiting_receipts`，不执行 `seal`，不把其中的 3 个 job 当作
   场景成功证据；
3. 后继独立变更集先实现 `SCN-REALITY-01`，把上表成功条件变成机器收据并失败关闭；
4. 再修正三个安全触发。任何受管工具或场景定义变化后都冻结新身份并创建 k37，完整重采；
5. 只有在正确触发仍被证明不可观测时，才回到 §6.4 讨论 `blocked` 或规则变更。

完整实证与执行方案见
[`SPEC_EP_002_EVIDENCE_BLOCKER.md`](SPEC_EP_002_EVIDENCE_BLOCKER.md)。

### 10.10 第四层：seal 预检的 selector 可达性（k39 暴露，已于 k47 归零）

k39 首次做到 19／19 且三份场景收据齐备，`seal` 仍未通过。拦截点是 ACC-03 引入的
`assertion_gate._verify_selector_reachability`：**每条 `dual_wire` 规则的每条 check
都必须在该侧命中至少一条观测**，命中不了即失败关闭。

这一层与前三层性质不同。前三层是「链路不通／证据类型不匹配／目标分支没触发」，这一层
是**判据的 selector 与采集侧的标签体系从未对齐**——历史上从没有 Campaign 走到过这道
门禁，所以两边的错位一直没有暴露。

用 k39 真实证据做全量扫描（不 fail-fast）：**48 条 check 命中、9 条选不到**，涉及 6 条
规则。逐条核对 0.147／0.145 源码后分四类：

| 类 | 性质 | 缺口 | 处置 |
|---|---|---|---|
| 一 | 标签 glob 漏覆盖：证据已采到但没进 manifest | `official-wham-get` 的 `misc-http.jsonl`（22 行，含 wham GET）未声明；A01 的 `models-http.jsonl` 同类 | 补标签声明，**不需重采** |
| 二 | 证据已采到，只差 `variant` 标签 | `SPEC-H1-004` 的 `models-order`／`responses-order` | 补标签声明，**不需重采** |
| 三 | 采集侧从未构造过该条件样本 | `SPEC-EP-014`×2、`SPEC-EP-020`、`SPEC-WS-002 optional-missing-covered`、`SPEC-EP-008 alpha-search-endpoint` | 改采集驱动，**必须重采** |
| 四 | 0.147 真实行为变化，补证据即伪造 | `SPEC-EP-019`×2 | 走画像更新，见下 |

**第一、二类已修复并离线验证**（用 k39 现成证据重跑编目→收口→派生→门禁）：
selector 缺口 9 → **5**，`dual_wire` 断言通过 52 → **54**，零回归。`variant` 按单个
relay 连接文件精确绑定——每个 `conn00N.client_to_upstream.bin` 只含一个请求，且
select 同时约束 `data.path`，标签贴错只会选不到而失败关闭，不会误判通过。

**第四类的实证**：`SPEC-EP-019` 断言 wham GET 路径集合恰好是
`{wham/usage, wham/rate-limit-reset-credits}`，而 0.147 实际发的是
`wham/settings/user`——该端点在 0.147 源码中出现 12 处、**0.145 中零处**，是 0.147 为
git-attribution 新增的。补标签后该 check 从「选不到」变为「选得到但断言失败」，真实
差异因此浮出。**不得为满足门禁去构造 0.147 根本不发的请求**；该条应在 R7 的 classify／
compare 阶段按规则变更处理。

> ⚠ 另需注意：`wham-get-headers` 的实际观测来自 mitm 面，而 mitm 记录不含 `host` 头，
> 因此该条的头序差异里混有采集面差异。**用 mitm 来源的观测做头序断言本身不可靠**，
> 修画像时须一并处理。

**第三类已按 §11 R6-B 补齐**（4 个样本，全部落在建 k40 之前以免工具身份漂移）：

| 判据 | 触发方式 | 性质 |
|---|---|---|
| `SPEC-EP-008` alpha-search | `--enable standalone_web_search` | 补触发条件。调用点 `ext/web-search/src/tool.rs:139`，注册条件见 `core/src/tools/spec_plan.rs:854`；该 feature 为 `UnderDevelopment`、默认关闭，官方集成测试同样显式打开 |
| `SPEC-EP-014` default／`SPEC-EP-020` | 走 legacy compact，不注入 turn-state、不开任何 `Stage::Experimental` feature | 自然基线，零干预 |
| `SPEC-EP-014` beta | 走 legacy compact ＋ `--enable network_proxy` | **I 类**，见下 |
| `SPEC-WS-002` optional-missing | `--disable remote_compaction_v2` 使 `x-codex-beta-features` 整条消失 | 关闭一个 Stable feature，不改 WS 传输参数 |

`x-codex-beta-features` 只收录 `Stage::Experimental` 的 feature 或 `RemoteCompactionV2`，
而 legacy compact 必须关掉后者；0.147 与 0.145 全树 `Stage::Experimental` **都只有
`network_proxy` 一个**，两版的 `build_model_client_beta_features_header` 逐字相同。
因此该 I 类干预**不是本次新增，而是复现 0.145 定基线时的同等条件**——不在同等条件下
采 0.147，「采不到」会被误读成行为变化。判据为 `all_list_prefix`，只断言槽位不断言头值；
`network_proxy` 的下游全在 `core/src/sandboxing/`，不改 API 出站路径。

**结果（k41）**：§10.10 的四类全部处置完毕，selector 缺口 **9 → 0**，`official_sealed`
首次达成。`alpha/search` 采到 4 条观测（前一轮为 0），两个新 compact job 的第三槽分别落在
`x-codex-window-id` 与 `x-codex-beta-features`，与设计一致。

### 10.11 第五层：判据 selector 缺陷与采集条件失真（已解除，k47 完成 classify）

seal 通过后 classify 才第一次真正评估 42 条规则的断言：起点**通过 59／失败 23**，
对目标画像做 17 处 select 修正后为**通过 70／失败 12**，selector 缺口全程为 0。

修正只做一件事——把 description 已写明、而 select 漏写的约束补回去：9 处补
`method=POST`（description 写的是「自动长度请求」「HTTP body 的」，原 select 只按
artifact 级标签选，把 GET 一并选中），6 处补 `labels.ca_mode absent`（判据要验的是客户端
发出的原始字节，而 mitm 是代理重构的——protocol 报 `HTTP/2.0`、`host` 落在 `:authority`
不在头列表；relay 规则的 labels 不带 `ca_mode`），5 处补端点／传输约束（排除 WS 握手、
排除走独立 backend-client 形态的 `wham/settings/user`）。**每处都先试选一次，加了约束会
导致「选不到」的一律不加**——`BODY-006/nonlite-*` 两条即因此被排除。

#### 10.11.1 双轨模型策略：主线 `gpt-5.4`，Lite 专项 `gpt-5.6-luna`

**4 条判据失败的共同根因，也是本层最重要的发现。**

`use_responses_lite` 既不由源码也不由 `config.toml` 决定，而是上游 models 响应下发的模型
元数据，没有任何配置键能覆盖。从 k41 的 models 响应原文读出：`gpt-5.6-*` 系列与
`codex-auto-review` 为 `true`，而 **Campaign 一直用的 `gpt-5.4` 为 `false`**。
场景 A03 的 precondition 却写着 `use_responses_lite=true`，标签据此打 `mode=lite`（涉及
6 个 job）——**这个条件从未成立**，违反标签声明自身的 authority 原则。

受影响的 4 条：`BODY-006/lite-*`×2、`EP-014/legacy-default-headers`（缺
`x-openai-internal-codex-responses-lite`）、`EP-020`。

> ⚠ 这 4 条一度被当成「0.147 的 Lite 语义变了」。核实后两版 lite 分支返回值**逐字相同**
> ——`(String::new(), None)`，顶层字段置空后靠 serde `skip_serializing_if` 省略。
> **判据失败要先查「条件是否真的成立」，再查「是不是新版行为变了」。**

本轮冻结双轨模型策略，不把整个 Campaign 切换到 `gpt-5.6-luna`：

1. **主升级线**：继续使用 `gpt-5.4`，只验收 `use_responses_lite=false` 的普通非 Lite 行为；
2. **Lite 专项**：仅为上述 4 条 Lite 判据增加独立 job，固定使用 `gpt-5.6-luna`，并要求
   每个 job 的 `/models` 原文实际证明 `use_responses_lite=true`；
3. 两条轨道必须使用独立的 `track` 标签、job、evidence root 和 receipt，禁止把 Lite 专项
   证据混入主线，也禁止把主线未触发 Lite 当成 Lite 失败；
4. 主线对 Lite-only 规则记录 `track_not_applicable`，Lite 专项必须对这 4 条规则分别给出
   `inherit`／`change`／`condition_change` 结论。联合 `classify` 仍要求两条轨道的适用规则
   均无 `blocked`；
5. 若 Lite 专项要证明“0.145→0.147 的版本差异”，则 0.145 与 0.147 两侧必须使用同一个
   `gpt-5.6-luna` 并保持同一采集条件；只有一侧使用 luna 时，结论只能是 0.147 的条件覆盖，
   不得写成纯版本差异。

#### 10.11.2 剩余 12 条与硬约束

| 需要的动作 | 条数 | 判据 | R8 处置 |
|---|---|---|---|
| 建立 Lite 专项模型轨道 | 4 | `BODY-006/lite-*`×2、`EP-014/legacy-default-headers`、`EP-020`；专项固定 `gpt-5.6-luna`，主线继续 `gpt-5.4` | 新增 `official-lite-http-response`、`official-lite-legacy-compact-default` 两个 lite job（独立 `track`／`RUN_ID`／证据根），四条判据 select 补 `labels.track=lite`；模型条件收据见 §10.11.3 |
| 补采集样本 | 4 | `BODY-006/nonlite-*`×2（无 non_lite 的 POST responses）、`PROTO-001/h1-wire`（A01 只有 mitm 证据）、`EP-019/wham-get-headers`（同上） | `official-relay-http-response` 改为主线双绑：`conn001` 供 A01 的原始 H1 请求行，其余连接供 A04 的非 Lite POST；新增 `official-relay-wham-get` 用字节中继取 Wham GET 原始头序 |
| 修采集脚本 | 2 | `EP-014/legacy-turn-state-slot`（`RELAY_INJECT_TURN_STATE` 注入失效）、`WS-002/default-swap-remove-order`（混入 runtime-metrics 的头） | 改用 `RELAY_INJECT_WS_TURN_STATE`，在 WS `response.metadata` 边界注入，让随后的真实 legacy compact 自然回送第三槽；新增 `official-relay-ws-default` 承担默认握手，runtime-metrics 改标 `variant=runtime_metrics` 不再冒充默认 |
| 实现新算子 | 1 | `TLS-003`：§10.9.2 定案的「存在性子集」算子至今未实现 | `same_set_distinct_order` 改为按扩展集合分组后逐组核对 record／artifact／排列数，任一组同时达标即通过；非列表记录一律计入 `invalid_record_ids` 并失败关闭 |
| ~~直接改期望~~ | 1 | `EP-019/wham-get-paths`：`wham/usage` → `wham/settings/user` | **判定被证伪，已撤销**，见 §10.11.4 |

**硬约束**：任一规则在其适用轨道上标 `blocked`，联合 classify 即为 `blocked` 状态，accept
的 `classification_unblocked` 门禁不通过，流程**永远走不到 `ready`**。Lite-only 规则在
主线只能标 `track_not_applicable`，不得伪造为通过；Lite 专项必须把这 4 条全部落在
`inherit`／`change`／`condition_change` 上。因此：

```
主线只采 gpt-5.4 → Lite 4 条在主线 `track_not_applicable`；Lite 专项用 gpt-5.6-luna
补齐 → 两条轨道联合无 blocked → 才能继续替换
```

**模型决策已冻结**：不全局切换模型；主线固定 `gpt-5.4`，Lite 专项固定 `gpt-5.6-luna`。
专项 job 必须把 `model_id`、`models_response_sha256`、`use_responses_lite` 和 fallback 状态
写入收据；任一值缺失或与预期不一致即失败关闭。

17 处修正原先只落在 draft 的 `assertion-profile.fixed.json`；R8 已连同下节的四项修正一并
合并进冻结基线画像并重算摘要。

#### 10.11.3 R8 复核时新发现的四项条件失真

12 项本身之外，R8 在逐项复核「标签声明 ↔ 采集坐标」一致性时又查出四处同源缺陷。它们与
§10.11.1 是同一个根因——**声明了一个采集侧从未成立的条件**——只是此前没人从这个方向扫过：

| # | 缺陷 | 后果 | 处置 |
|---|---|---|---|
| 1 | `official-relay-legacy-compact-beta` 标 `mode=lite`，实际走主线 `{model}`=`gpt-5.4` | 与 A03 的历史错标同源；Lite 判据一旦放宽 track 约束就会误选 | 改 `mode=non_lite` 并补 `track=main` |
| 2 | `official-http-fallback` 标 `mode=lite`，同样走 `gpt-5.4` | 同上 | 改 `mode=non_lite` 并补 `track=main` |
| 3 | `variable_contract.model` 的 default 仍是 `gpt-5.6-luna` | 不显式传 `--model` 时主线会静默变成 Lite 轨道，而标签仍按 `non_lite` 声明 | default 改 `gpt-5.4`，并写明 Lite 专项不经本变量 |
| 4 | `BODY-006/nonlite-*`×2 只按 `A04＋mode=non_lite` 选 | `residency-us`／`runtime-metrics` 用整目录 glob 绑 A04，其 relay 字节必含启动期 `GET /models`，body 断言必然失败——补出的非 Lite 样本会白采 | select 补 `method=POST` 与 `/backend-api/codex/responses` 路径约束 |

第 4 项是**这轮唯一会直接决定 k42 成败的一项**：§10.11 当初把这两条排除，理由是「加约束会
选不到」——那是因为当时根本没有非 Lite 的 HTTP POST 样本。R8 补出样本后约束才成立，若不补
约束，补采本身就没有意义。

配套的 `h1_wire_probe.py` 受控清单原先只声明 `gpt-5.6-luna`，主线模型查不到元数据会落到
默认值；已补 `gpt-5.4`（`use_responses_lite=false`），与 k41 的 `/models` 原文一致。**探针是
受控上游，元数据由它权威给出，写错即等于伪造 Lite 条件。**

四项均已补上防回归测试，其中「官方 job 想声明 Lite，就必须同时是 lite 轨道、固定
`gpt-5.6-luna` 且产出模型条件收据」是一条通用不变量，覆盖全部官方标签声明。

#### 10.11.4 `EP-019/wham-get-paths`：§10.10 第四类的判定被证伪

k42 启动后、正式采集展开前，用上一轮留在 Vircs 的 R8 预检 relay 证据做交叉复核，
发现 §10.10「第四类：0.147 真实行为变化」对本条的判定**是错的**，override 已撤销。

**实测**（同一批 0.147 预检，按 relay 原始字节逐连接解析）：

| 触发路径 | 观测到的 wham GET | 头序 |
|---|---|---|
| TUI 配额查询（`compact-tui`，即 `official-relay-wham-get`） | `wham/usage`、`wham/rate-limit-reset-credits` | `user-agent, authorization, chatgpt-account-id, accept, cookie, host` |
| 普通 turn 启动（`ws-default`／`http-response`） | `wham/settings/user` | `user-agent, authorization, chatgpt-account-id, **cache-control**, accept, cookie, host` |

**源码复核**：0.147 的 `backend-client/src/client/rate_limit_resets.rs:83` 仍然构造
`{}/wham/usage`，`:93` 构造 `{}/wham/rate-limit-reset-credits`；而 `settings/user` 在
`backend-client/src/client.rs:640`，是 git-attribution 用户设置查询的**另一个独立调用
点**。两者并存，不是替代关系。

**原判定错在哪**：§10.10 的观测来自 `official-wham-get` 的 `misc-http.jsonl`，那是
**mitm 面**，其采集窗口恰好只捕到 `settings/user`。R8 给该条 selector 补了
`labels.ca_mode absent`（排除 mitm 重构面）之后，实际选中的已经是 relay 面证据——
而 relay 面发的正是 `usage`＋`rate-limit-reset-credits`，**与 0.145 原期望逐字一致**。

也就是说，本条属于 §10.10 的**第二类（证据面／标签问题）**，R8 补 `ca_mode absent`
时就已经把它修好了；再叠一层 override 反而会把一条本该通过的判据改成必然失败。
画像别处早已用 `not_equal` 显式排除 `settings/user`（§10.11 的 17 处修正之一），
两处口径本就该一致，是 override 让它们各说各话。

**处置**：删除 `candidate_rule_expectation_overrides_0_147_0.json`，0.147 目前没有任何
期望覆盖；补两条回归测试锁定「无 override 时画像逐字不变」与「`wham-get-paths` 保持
0.145 原期望」。

> **教训与 §10.11.1 同源**：判据失败要先查「条件是否真的成立」「证据是不是取自正确的
> 采集面」，再查「是不是新版行为变了」。这次是**证据面**选错——mitm 面与 relay 面看到
> 的是同一个客户端的不同请求子集，拿其中一面的缺失去论证「版本行为变化」并不成立。

**曾被怀疑、后被 k44 证据推翻的一项**：R8 预检证据里 `EP-019/wham-get-headers` 的头序
含 `cookie`，而基线期望不含，一度被记为「基线画像按源码推导写漏了 cookie」。**k44 的
`official-relay-wham-get` 实测推翻了这个推测**：

| 判据 | 基线期望 | k44 实测 |
|---|---|---|
| `wham-get-paths` | `{wham/usage, wham/rate-limit-reset-credits}` | **完全一致** |
| `wham-get-headers` | `[user-agent, authorization, chatgpt-account-id, accept, host]` | **逐字相同，不含 cookie** |

差别只在 cookie store 的状态：预检那次发生在已建立 cloudflare cookie 的会话里，k44 的
采集环境是干净的。**两条判据都不需要任何期望变更**——这同时是本节「撤销 override」判定
的终局实证：A12 绑定的 relay 证据发的正是 `usage` ＋ `rate-limit-reset-credits`。

> 教训再补一条：**预检证据的会话状态未必等于正式采集的会话状态**。拿预检里的一次观测
> 去推断「基线画像写漏了」，与本节开头的 mitm 面误读是同一类错误——都是把某一次采集
> 条件下的观测当成了版本或画像的普遍属性。

#### 10.11.5 k43 的两项实证缺陷：压缩负样本缺失与中继侧无容量重试

k43 跑到第 18 个 job 时用同一批 relay 原始字节做了一次全量交叉复核，又查出两项**必然
让 `seal` 失败**的缺陷。两项都不是判据问题，而是采集侧从未产出对应条件的样本。

**其一：`BODY-002/responses-plain` 没有任何负样本。**
该规则要证明「Responses 尊重压缩开关」，需要正负两个样本：`responses-zstd`（A03，
压缩开启）有 Lite 专项样本；`responses-plain`（A04，压缩关闭）却**从来没有任何 job
真的关过压缩**——全部 relay 样本都是 zstd，标签也只有 `zstd` 一种取值。A04 的
precondition 原先写着 `enable_request_compression=false`，是又一处「声明了采集侧从未
成立的条件」。R7 给该条 select 补 `labels.compression=plain` 之后，它就从「选中 zstd
样本去断言无压缩」变成恒定选不到，而 `seal` 的 selector 可达性是失败关闭的。

处置：新增 `official-relay-http-response-plain`（主线 `gpt-5.4`，只 `covers` `SPEC-BODY-002`，
`--disable enable_request_compression`）。该 feature 是 `Stage::Stable`、
`default_enabled=true`（`features/src/lib.rs:1085`），关掉只影响请求体是否 zstd，不改
端点、头序或传输形态，与 `ws-optional-missing` 关 `remote_compaction_v2` 同款做法。

**其二：中继采集路径没有上游容量重试。**
`capturelib/scenarios.py` 早有 `UPSTREAM_CAPACITY_RETRY_LIMIT=4`，但它只覆盖
official-core 路径；`run_official_relay_scenario.sh` 一直没有。于是 Lite 专项固定的
`gpt-5.6-luna`——恰好是容量最紧张的一档——碰上一次波动就整轮打死：k43 的
`lite-legacy-compact-default` 连续三个 attempt 全部死于
`Selected model is at capacity`，**一个 compact 请求都没发出**，`EP-014/legacy-default-headers`
与 `EP-020/legacy-observed-subset` 因此仍然不可达。同一轮里同样用 luna 的
`lite-http-response` 却成功了，可见是间歇性的。

处置：给中继脚本加同款有限重试（4 次、间隔 20 秒），**只认这一条错误消息**，其余失败
一律原样上报，不放宽安全边界。

**k43 已实证有效、无需再改的部分**（同一批证据）：

| 验证项 | 实测结果 |
|---|---|
| 双轨模型策略 | Lite 轨道的 POST Responses 带 `x-openai-internal-codex-responses-lite`，主线同一请求没有——`gpt-5.6-luna` 与 `gpt-5.4` 的 Lite 差异被真实 wire 证实 |
| `EP-014/legacy-turn-state-slot` | 第三槽为 `x-codex-turn-state`，`RELAY_INJECT_WS_TURN_STATE` 改造生效 |
| `EP-014/legacy-beta-slot` | 第三槽为 `x-codex-beta-features` |
| `EP-014` 默认槽位（主线） | 第三槽为 `x-codex-window-id`，与设计一致 |
| `WS-002/optional-missing` | 握手整条不含 `x-codex-beta-features`，扰动成立 |
| `WS-002/default` | 握手含 `x-codex-beta-features` 且不含 runtime_metrics 条件头 |
| §10.11.3 第 4 项 | `official-relay-http-response` 的 `conn002` 实际是 wham GET；正是补上的 `method=POST` 与 responses 路径约束把它挡在 `BODY-006/nonlite-*` 之外 |

最后一行值得单独记：那条约束当初是按「description 已写明、select 漏写」补的，属于
推理；k43 的真实连接顺序把它变成了实证——**不补就一定失败**。

#### 10.11.6 k44：27/27 全成立，但 A05 在 R8 中丢失了全部证据

k44 是历史上第一次官方侧 27 个 job 全部 `complete`：五份模型条件收据（主线 3 份
`gpt-5.4`／`use_responses_lite=false`，Lite 专项 2 份 `gpt-5.6-luna`／`true`，均无
fallback）与三份场景真实性收据（A11 `sideband_established`、A13 `token_refreshed`、
A14 `upload_chain_complete`）全部齐备。A11 首轮因上游 WS `Connection reset without
closing handshake` 失败三次，`resume --rerun-failed` 补跑一次即成立，新 attempt 通过
`continuity` 承接了前一轮的 26 个成功 job。

seal 前的全量扫描（51 PASS／4 FAIL／2 UNREACHABLE，观测按 track 分为 main 59、
lite 13）暴露出**两条不可达**，它们会让 seal 失败关闭：

`SPEC-HDR-004/ws-beta` 与 `SPEC-HDR-006/ws-no-accept` 的 select 都指定 `A05`，而
**A05 在 R8 之后没有任何标签绑定**。根因是 §10.11.3 的连带效应：A05 原先绑在主线
`official-relay-turnstate-compact` 上并标 `mode=lite`，而该 job 用的是非 Lite 模型
——那是同一类「声明了从未成立的条件」。R8 撤销这个错误标注是对的，却没有补回 A05
的证据，于是场景**静默失去了全部来源**。契约的 `side_coverage` 仍要求官方侧 A05 提供
`relay_binary` ＋ `websocket_trace`，因此这不是可以放着不管的空场景。

处置：新增 `official-lite-ws-turnstate`——固定 `gpt-5.6-luna`、两轮 WS 会话、由中继在
`response.metadata` 边界回送 turn-state，让 A05 的 description（Lite WS ＋ turn-state
＋ 压缩上下文）第一次真正成立，而不是靠给别的 job 贴一个不成立的 `mode=lite`。

同时补一条测试，逐场景比对契约 `side_coverage` 与标签实际能产出的 kind：**场景失去
证据是静默的**，此前只有 seal 才会暴露，现在改标签时立刻可见。

**留待 classify 定性的 4 条 FAIL**（都不阻塞 seal——门禁只验 selector 可达性）：

| 判据 | 实测 | 性质 |
|---|---|---|
| `EP-019/wham-get-paths` | 实际发 `{usage, rate-limit-reset-credits, settings/user}` 三条 | **0.147 真实新增出站面**。§10.11.4 的结论仍成立——`usage` 没有被替代；但 A12 的路径集合确实多了一条，按 §6.4「新出站面不是失败」记 `change` |
| `EP-019/wham-get-headers` | `settings/user` 头序含 `cache-control`，另两条不含 | 同上，`settings/user` 走独立 backend-client 形态，画像别处已用 `not_equal` 排除它，本条待一并对齐 |
| `PROTO-001/h1-wire` | 命中 5 条 | 待逐条核对 |
| `H1-004/responses-order` | 命中 1 条 | 待逐条核对 |

#### 10.11.7 k45：WS 传输下的模型条件收据取不到模型

k45 采到 26/28，两个失败性质截然不同。

**其一（本轮新引入）：`official-lite-ws-turnstate` 三次重试全部死于**
`未见可绑定的 Responses／compact 原始请求模型`。§10.11.6 新增这个 A05 job 时直接套用了
Lite compact job 的定义，连 `REQUIRE_MODEL_CONDITION_RECEIPT=1` 一起带了过来；而
`model_condition_receipts` 的第二步只认 **HTTP POST** 到 `/responses`／`/responses/compact`
的请求体。WS 传输下握手是 `GET` ＋ `Upgrade`，真正的请求体在帧里，且官方协商了
`permessage-deflate`——18KB 帧区里搜不到 `gpt-5` 明文。**这恰恰是 A05 自己的
precondition**，所以这个 job 天生走不通旧路径。

处置：给收据生成器补 WS 帧路径。解法与 `relay_extract.parse_ws_frames` 逐字一致——
客户端帧带 mask 要先解掩码，permessage-deflate 默认**上下文接管**，整条连接只能用一个
解压器（逐帧新建会让第 2 帧起全部失败）。`relay_extract` 刻意只留结构不留取值，所以
不能直接复用，只能同款重写一遍并只取顶层 `model`。HTTP 与 WS 两路取并集后一起参与
fallback 判定，**判据强度不变**，只是把 WS 形态纳入了证明范围。已用 k45 的真实 WS 证据
验证：从 7 条连接取出 11 处 `gpt-5.6-luna`，去重唯一、无 fallback。

**其二：`official-core` 三次重试全挂在上游容量上**，而三次失败的场景各不相同
（`codex-http/s1` → `codex-ws/s1` → `codex-http/s2`），是波动而非固定缺陷。但根因值得
单独记：该 job 的 argv **从不传 `--codex-model`**，于是一直用 `capture.py` 的默认值
`gpt-5.6-luna`——既与「主线固定 `gpt-5.4`」矛盾，也让 A01／A02／A09／A15 的证据长期跑在
容量最紧张的那一档上。这不是 R8 引入的，k41 就是这样 seal 的。

处置：`official-core` 与 `official-ws-handshake-repeat` 显式绑定 `--codex-model {model}`，
并补一条测试禁止官方 job 靠工具默认值决定模型——**默认值是隐式的，清单改模型时不会
跟着变**。

> 这两项合起来印证同一件事：**收据与坐标都必须显式**。一个把「模型从哪来」交给了工具
> 默认值，一个把「模型怎么证明」限定在了单一传输形态上；两者都在真实采集里才暴露。

#### 10.11.8 k46：补强收据反而拆掉了一个隐含门禁

k46 做到 28/28 全 `complete`（A11 又遇一次 WS 重置，`resume --rerun-failed` 一次补回），
六份模型条件收据齐备，`official-core` 也确认跑在主线 `gpt-5.4` 上。但 seal 前扫描出现
两条**新的**不可达：`EP-014/legacy-default-headers` 与 `EP-020/legacy-observed-subset`
——它们在 k44 明明是 PASS。

**根因是 §10.11.7 那次修复的副作用**。对照两个 compact job 的 relay 字节：

| job | 实际请求 |
|---|---|
| `official-relay-legacy-compact-default`（主线） | 有 `POST /backend-api/codex/responses/compact` |
| `official-lite-legacy-compact-default`（Lite） | **只有 WS 握手，一个 compact 请求都没有** |

而后者的 job 状态是 `complete`。原因：这件事**从来没有过显式检查**，只是碰巧被模型条件
收据挡住——收据要从 HTTP POST 请求体里取 `model`，目标请求没发出时它自然报错，job 因此
判 failed（k43 的 lite compact 正是这样暴露的）。§10.11.7 给收据补上 WS 帧路径之后，WS
会话里取得到模型，**这个隐含门禁就没了**。

> **收据的语义是「模型条件成立」，不是「目标分支已触发」。** 两件事必须各自显式表达，
> 靠一个门禁副作用挡住另一类失败，迟早会在补强前者时失去后者。这与 `SCN-REALITY-01`
> 当初的定案同源：那次是「job 退出成功 ≠ 目标分支已触发」，这次是「收据生成成功 ≠
> 目标分支已触发」。

处置两条：

1. **采集脚本新增 `REQUIRE_REQUEST_PATH`／`REQUIRE_REQUEST_METHOD`**：声明了就在收尾时
   逐连接解析原始字节核对，缺失即非零退出、失败关闭。只给「存在的唯一理由就是该请求」
   的 8 个 job 声明（三个 compact 变体、三个 http-response 变体、Lite compact、wham-get），
   通用矩阵型 job（`official-core` 等）不加，避免把正常的多分支采集判成失败。
2. **`COMPACT_TOKEN_LIMIT` 参数化**：legacy compact 靠上下文越过门限自动触发，Lite 轨道
   的触发不如主线稳（k44 成功、k46 整轮未触发）。Lite 专项下调到 2000，提高确定性。

#### 10.11.9 k47：双轨采集首次 `official_sealed`

| 项 | 结果 |
|---|---|
| 官方 job | **28/28 全 `complete`**，一次跑通，无需 `resume` 补跑 |
| 模型条件收据 | 6 份：主线 3 份 `gpt-5.4`／`use_responses_lite=false`，Lite 专项 3 份 `gpt-5.6-luna`／`true`，均无 fallback |
| 场景真实性收据 | 3 份：A11 `sideband_established`、A13 `token_refreshed`、A14 `upload_chain_complete` |
| seal 前全量扫描 | **53 PASS／4 FAIL／`UNREACHABLE` 0**（观测按 track 分为 main 61、lite 23） |
| 环境恢复 | 五项全 `passed`，`restored`；数据库 426 主键零缺失 |
| 秘密扫描 | 986 文件／163 MB，findings **0** |
| Campaign 状态 | `official_sealed`，`next_command: classify` |

`UNREACHABLE` 归零是关键——k42～k46 五轮全部倒在这一项上。A11 这轮在 job 内三次重试中
即成立，没有触发 `resume`。

**seal 过程中发现一处环境级缺陷（不影响本轮结论）**：`build_assertion_bundle.py:190`
把收口的文件写成 `0400`，而 seal 的原始证据预检要求**文件 0600／目录 0700**，因此首次
seal 被拒。k41 之所以能过，是它的 bundle 当时被手工改成过 0600。已用 `chmod` 归一并
逐字校验内容未变（收口前后全量 SHA-256 一致），seal 随即通过。

> **权限与属主不进工具身份摘要**，所以这次修正没有作废 k47。但收口器的 `0400` 与 seal
> 预检的 `0600` 是两处硬编码常量彼此矛盾，**属于必须修的工具缺陷**——只是不能在 k47 的
> classify 走完之前改，否则工具漂移会让本轮 sealed 证据无法继续使用。登记为 R10 之后的
> 待办。

**留待 classify 定性的 4 条 FAIL**（seal 只验 selector 可达性，不评估断言）：
`EP-019/wham-get-paths`、`EP-019/wham-get-headers`（0.147 新增 `wham/settings/user`
调用点，属 `change`，源码依据见 §10.11.4）、`PROTO-001/h1-wire`、`H1-004/responses-order`
（命中数少，逐条核对 selector 精度即可）。

#### 10.11.10 k47 classify：`profile_approved`

五份 0.147 清单已批准封存，联合摘要
`2ff8f3358cd5870753dd81320e306ec6f1ba2b5649a75d65cf51d1734b9371ab`：

| 清单 | 封存 SHA-256 |
|---|---|
| `target-rules.json` | `91616975b1a7e3e3717a99c72a937b8f6901a921c434decb2ee7fdab85c4f75b` |
| `rule-migration.json` | `110c20f540949043dd458784fe2f762f96a775dbd8c814bb69fb366a750473d1` |
| `scenarios.json` | `4938ae56730bd4ebc9c4ad669a66de7a225798a112da244b6c641fb4f38df17b` |
| `profile.json` | `b41dd002e40e35f512d0960664290f8987d7592d148c560b94739be20a6eec70` |
| `assertion-profile.json` | `bb7d5b160ff24cf07d174c241d3577e9161e32bb296d17da5472d2d70d87af73` |

`target-rules.json` 的摘要与 k34 **逐字相同**——规则集本身未变。画像 `profile_digest`
为 `0d86e033…`，与 §10.1 记录的跨轮基准一致、payload 与 k34 逐字相同，这是第七次独立
复算出同一结果。

**分类结果**：42 条规则 41 `inherit` ＋ 1 `change`；2808 条发现 2604 `change` ＋
204 `condition_change`；`unclassified_count=0`、**`blocked=0`**。

- **source 发现（2604 条）**与 k34 指纹逐条相同（同一对源码树），直接继承其已批准分类，
  不重新臆断；继承时逐条校验指纹命中，缺一即失败关闭。
- **dynamic 发现（204 条，其中 153 条为 k34 所无）**是双轨采集与 R8 新增 job 采到的新
  出站面，按 kind／path 的出站子系统语义映射：`websocket_frame`→`WS-004`、
  `tls_client_hello`→`TLS-001`、`http_request` 按 path 映射。k47 新出现的
  `alpha/search`→`EP-008`、`files`／`oauth/token`→`EP-002`、`v1/live/`→`EP-012`
  依据各规则自身 description 补齐。

**目标画像的 4 处修正**（只落在目标版本画像，基线画像与受管工具零改动）：

| 判据 | 修正 | 依据 |
|---|---|---|
| `PROTO-001/h1-wire` | 补 `labels.ca_mode absent` | 5 条命中里 4 条报 `HTTP/2.0`，来自 mitm 面（代理把 h1 重构成 h2）。§10.11 当初没补是因为 A01 只有 mitm 证据，R8 补出 relay 原始请求行后才成立 |
| `H1-004/responses-order` | 期望补 `x-openai-internal-codex-responses-lite` | A03 的 precondition 就是 `use_responses_lite=true`；基线期望缺该头源于 §10.11.1「A03 长期被非 Lite 模型采集」。两版 `core/src/client.rs` 的 `add_responses_lite_header` 注入条件逐字相同（0.145:616／0.147:629），故属画像缺陷而非版本差异 |
| `EP-019/wham-get-paths` | 期望扩为三条路径 | 0.147 新增 `wham/settings/user`（`client.rs:640`，0.145 零处），`wham/usage` 仍在（`rate_limit_resets.rs:83`）——新增而非替代 |
| `EP-019/wham-get-headers` | 排除 `settings/user` | 该端点走独立 backend-client 形态（头序含 `cache-control`）；其线序覆盖属后续规则扩展，已在 `rule-migration` 中作为 `change` 登记 |

修正后在 k47 的 sealed 证据上重跑：**57 PASS／0 FAIL／0 UNREACHABLE**。

> 只有 `EP-019` 被定性为 `change`——它是本次唯一经源码与 wire 双重证实的 0.147 出站面
> 变化。其余两处是画像自身的缺陷修正，规则行为并未改变，故仍记 `inherit` 并在 rationale
> 中写明修正内容与依据。

**环境侧补充**：Vircs 未装 Go，而 `prepare-profile` 内部硬编码 `go run ./cmd/...`。
已用 `golang:1.26-bookworm` 容器提供 `go` 转发脚本（项目要求 1.26.5），并补传
`backend/`（不含构建产物）。两者都不进工具身份摘要，身份仍为 97 项 `cacf51be…`。

#### 10.11.11 k42～k47 六轮总表：每次作废换来一条不变量

六轮里有五轮作废，**每一次都是在 `seal` 之前用真实证据查出来的**，不是 seal 失败后回头
找。代价是重采，收益是每条缺陷都变成了机器可执行的门禁。

| 轮次 | 死因 | 性质 | 固化的不变量 |
|---|---|---|---|
| k42 | `EP-019` override 把 `wham/usage` 改成 `settings/user` | 证据面误读：原判定取自 mitm 面，而修正后的 selector 选的是 relay 面 | 无 override 时画像逐字不变；`wham-get-paths` 期望锁定 |
| k43 | ① `BODY-002/responses-plain` 从来没有负样本 ② 中继路径无上游容量重试 | 声明了采集侧从未成立的条件；`capturelib` 的重试不覆盖 relay | 逐场景比对契约 `side_coverage` 与标签产出；中继侧同款有限重试 |
| k44 | A05 失去全部标签绑定 | §10.11.3 撤销一个错误 `mode=lite` 标注时的连带效应，**场景静默失去证据** | 官方侧每个受契约要求的场景都必须有标签产出所需 kind |
| k45 | ① 模型收据不支持 WS 传输 ② `official-core` 靠 `capture.py` 默认值跑在 Lite 模型上 | 收据只认单一传输形态；模型坐标隐式 | WS 帧提取（含 deflate 上下文接管）；禁止官方 job 靠工具默认值决定模型 |
| k46 | Lite compact 整轮未触发目标请求，job 却判 `complete` | **补强收据反而拆掉了一个隐含门禁**——原先「收据生成失败」意外充当了「目标请求没发出」的检测器 | `REQUIRE_REQUEST_PATH` 显式失败关闭；`COMPACT_TOKEN_LIMIT` 参数化 |
| **k47** | — | — | **28/28 一次跑通 → `official_sealed` → `profile_approved`** |

**贯穿六轮的两条方法论**：

1. **判据失败要按固定顺序排查**：① 条件是否真的成立（采集侧有没有产出该条件的样本）
   → ② 证据面是否取对（mitm 面与 relay 面看到的是同一客户端的不同请求子集）
   → ③ 会话状态是否可比（cookie store 等在预检与正式采集里不同）
   → ④ **最后**才考虑「是不是新版行为变了」。k42 与「预检 cookie」两次误判都栽在跳步。
2. **每个门禁只能表达一件事**。k46 的教训最典型：收据的语义是「模型条件成立」，不是
   「目标分支已触发」；靠一个门禁的副作用挡住另一类失败，迟早会在补强前者时失去后者。
   这与 `SCN-REALITY-01` 当初的定案同源——那次是「job 退出成功 ≠ 目标分支已触发」。

期间受管工具测试由 601 增至 617 项，新增的 16 条全部来自这六轮的真实踩坑。

#### 10.11.12 R10-b 起步：候选采集迁至 ARM64

**判定依据**：`run_candidate_core_capture.sh`、`run_candidate_aux_capture.sh`、
`run_h1_wire_probe.sh`、`run_images_wire_probe.sh` 全部不引用 Codex CLI 二进制——候选侧
客户端是 `drive_candidate_gateway_ws.py`，配 `upstream_byte_relay.py` 做合成上游。
Codex CLI（x86_64）只在官方侧用到，那部分已在 Vircs 完成并 sealed。因此候选采集可以整体
放到 ARM64 测试环境，Vircs 的生产服务完全不必参与。

**campaign 可迁移**：`_verify_plan_identity` 校验的是 `target_source` 目录树摘要、
`target_package` 包摘要与工具身份摘要，**不绑定 campaign 目录路径**。把以下资产按原绝对
路径同步到 ARM64 后，`status` 在 ARM64 上复算通过，仍为 `profile_approved`：

| 资产 | 体积 | 用途 |
|---|---|---|
| campaign 目录 | 65 M | 已 sealed 的官方证据、classify 已批准清单 |
| 官方证据根（31 个） | 160 M | seal 记录的 inventory 要逐项复算 |
| 0.147 源码树 | 89 M | `target_source` 身份校验 |
| codex package | 114 M | `target_package` 身份校验 |
| 受管工具树 | 63 M | 工具身份必须逐字一致 |

Vircs 无到 ARM64 的私钥，经本地管道中转（数据流经不落盘）。

**账号坐标对齐**：campaign 冻结了 `codex_account_id: 90`，而 ARM64 上没有该 ID。按指示改用
ARM64 现有的 OAuth 账号（`c_zs@163.com`，原 ID 19），在事务内把其主键对齐到 90：外键
`update_rule` 均为 `NO ACTION`，故先摘 `account_groups` 关联、改主键、再按原值插回；改前已
`pg_dump` 备份 `accounts` 与 `account_groups`。该账号原本就在 group 2，与
`api_key_id=1` 同组，正是采集要求的配对；账号总数保持 19，未新增记录。

**候选镜像**：`deploy/Dockerfile` 的 `BUILD_TAGS` 需加 `candidatecapture`（Linux 生不出
DeviceCheck，改用受四元组约束的合成 provider）。候选源码树用干净副本，并把
`stage-profile` 产出的 staged catalog 覆盖到
`backend/internal/officialegress/catalogdata/`——覆盖后 `profiles/` 同时含 `0.145.0` 与
`0.147.0`，`release-catalog.json` 的 `source` 指向 k47 的 classification 摘要。

## 11. 后继实施计划（R0～R10）

本节是当前升级工作的**实施计划**。R0～R9 与 R10 的 classify／profile 已随 k47 完成，
本节保留完整的变更集定义与退出条件作为审核依据，当前有效的执行指令集中在 §11.5。
**不得在 k47 上续跑官方 `run`、追加证据或修改受管工具**——工具漂移会让本轮 sealed 证据
与已批准清单失效。

### 11.0 执行前置要求
Vircs 和 ARM64 都是远程服务器，使用 SSH 连接。

本升级包含两条相互独立的执行轨道，不能把官方 CLI 采集和 Sub2API 服务更新混为一件事：

1. **官方 CLI 采集轨道**：可以继续在远程服务器 Vircs 上进行。采集使用独立的 Campaign、relay、容器和
   evidence root；只要不修改 Sub2API 的服务进程、生产配置、生产端口、生产数据和
   Active/Previous，就不影响 Vircs 当前对外提供的 Sub2API 服务。
2. **Sub2API 服务验证轨道**：不得直接在正在对外服务的 Vircs 实例上试验。先在独立的
   ARM64 Sub2API 环境完成测试和验证，全部通过后再准备 Vircs 更新。

ARM64 环境的最低验证范围：

- 编译、单元测试、启动和健康检查；
- HTTP API、WebSocket、数据库迁移与恢复；
- 官方出站请求、TLS、代理、超时和错误恢复；
- 基本并发／资源使用检查以及可回滚性。

ARM64 全部通过后，**不得直接把 ARM64 结果视为 Vircs 上线通过**。上线前还必须：

1. 冻结源码、镜像、配置、数据库迁移和依赖摘要；
2. 确认 Vircs 实际架构与镜像架构（只读核对 `uname -m`、镜像 `Architecture` 和实际二进制）；
3. 在 Vircs 部署独立 canary／旁路实例，完成启动、健康、API、WebSocket、出站 TLS 和错误率检查；
4. canary 通过后才允许更新正式实例；失败则保留证据并回滚，不得直接替换生产实例。

架构边界必须明确：

- 当前冻结的官方 Codex CLI 资产是 Linux `x86_64`，正式官方采集仍须在 Ubuntu 24.04 / x86_64
  环境完成；ARM64 Sub2API 验证不能替代官方 CLI 采集验证；
- **但候选侧采集不受此约束**：`run_candidate_*` 全套脚本都不引用 Codex CLI 二进制，客户端是
  `drive_candidate_gateway_ws.py`（Python 实现的网关 WebSocket 驱动）配
  `upstream_byte_relay.py` 合成上游。因此候选采集可以在 ARM64 完成，官方采集留在 Vircs
  ——这正是两条轨道天然的分界；
- 如果 Vircs 实际为 `aarch64`，ARM64 验证是服务更新的必要前置，但仍需在 Vircs 做最终 canary；
- 如果 Vircs 实际为 `x86_64`，ARM64 验证属于额外兼容性验证，不能替代 Vircs x86_64 canary。

在 ARM64 staging 和 Vircs canary 阶段均使用独立账号、独立 `CODEX_HOME`、独立配置和独立
证据目录；不得触碰生产凭据、全局 `/etc/hosts`、生产 relay 或生产 Active/Previous。上述门禁
只约束 Sub2API 服务更新，不阻塞已经隔离的官方 CLI 采集轨道。

### 11.1 总体目标与不变约束

原目标是在不替换官方出站形态的前提下，证明 A11／A13／A14 真实进入目标协议分支，并为
`SPEC-EP-002` 的三条必现 SNI check 生成可封存证据——**已随 k47 达成**（三份场景收据齐备、
三条 SNI check 均有官方证据）。剩余目标是完成候选侧验收并安全切换 Active。

不变约束：

- Active/Previous 在 `ready` 之前继续保持 `0.145.0 / 0.145.0`；
- k34～k46 只作诊断夹具，证据不迁移、不复用、不封存；**k47 的 sealed 证据与已批准清单
  保持不可变**；
- 不伪造 JWT、上游响应、Realtime sideband 或文件上传 URL；
- 不修改日常登录目录，不使用未隔离的真实凭据做过期触发；
- 主升级线固定使用 `gpt-5.4`，只验证普通非 Lite 行为；
- Lite 专项固定使用 `gpt-5.6-luna`，只验证 `use_responses_lite=true` 的 Lite 判据；
- 两条轨道必须使用独立 job、`track` 标签、evidence root、模型收据和结果摘要；
- 任意工具身份、场景定义、抓包范围、模型坐标或收据 schema 变化，都必须冻结新摘要并
  创建新的 Campaign，**不得修改或续跑 k47**——那会让已 sealed 的证据与五份已批准清单
  一并失效，整条流程要从官方重采开始。

### 11.2 变更集与退出条件

| 变更集 | 实施内容 | 必交付物 | 退出条件 |
|---|---|---|---|
| R0 方案冻结 | 固定 A11／A13／A14 的成功事件、证据字段、超时、负例和恢复边界；定义 `scenario_receipt` 与 job 状态的关系 | `SCN-REALITY-01` 方案、字段表、状态机、测试矩阵 | **已完成**；无“脚本退出即成功”的路径 |
| R1 真实性门禁 | 记录原始事件、生成成功 receipt、job 缺 receipt 失败关闭；不在 R1 伪造或补写协议字段 | 门禁实现、正反测试、receipt schema 校验 | **已完成**；A11 400、A13 无 refresh、A14 无工具调用等负例均失败关闭，实施记录见 `SCN_REALITY_01_SCENARIO_REALITY_GATE.md` §12 |
| R2 A11 触发修正 | 使用官方 0.147 V3／`quicksilver=v2` 路径；只调用官方 CLI，不手工拼 sideband | realtime 成功 receipt、call_id/SDP/started 关联、api SNI 记录 | call-create 2xx、异步 started/SDP、sideband SNI 三者同时存在 |
| R3 A13 触发修正 | 隔离账号与 `CODEX_HOME`；优先走官方 `account/read {refreshToken:true}` 主动刷新，或按收据记录自然刷新窗口；采集前后核对隔离状态 | refresh request receipt、auth SNI、凭据摘要 | 真实 `POST /oauth/token` 与 `auth.openai.com` SNI 同时存在；刷新路径和凭据变化与收据一致 |
| R4 A14 触发与抓包修正 | 冻结 Apps 工具可见性与调用契约；要求 create→响应 `upload_url`→PUT→uploaded；按端口覆盖响应返回的所有上传主机 | tool-call receipt、URL provenance、PUT/uploaded receipt、动态 host inventory | 工具调用和完整上传链成立；区域 SNI 来自真实响应，不依赖硬编码主机 |
| R5 P0 与身份冻结 | 在临时目录做可丢弃预检，运行全套工具测试，冻结源码／工具／场景／镜像摘要 | P0 报告、inventory、工具身份 receipt | 工作区干净；身份校验通过；不得触碰 Active/Previous |
| R6 k41 官方重采 | **已完成**：k41 执行官方 `run`，逐 job 校验真实性 receipt 并执行 `seal` | k41 campaign、attempt、results、证据根、secret scan、seal receipt | 22/22 job 完成；三条 SNI check 均有官方证据；`official_sealed` 已达成 |
| R7 classify | 复核 17 处 selector 修正，并拆分主线与 Lite 专项的适用规则 | 双轨 classification draft、selector 测试、模型条件记录 | **已完成**；通过 59→70、失败 23→12、selector 缺口全程 0 |
| R8 双轨变更集 | 主线保持 `gpt-5.4`；新增 `gpt-5.6-luna` Lite 专项 job；补采样本、修脚本、实现 `TLS-003`、修正 `EP-019` 期望值，并更新场景／收据契约 | 变更集、测试、双轨场景清单、收据 schema、selector/profile 修正摘要 | **已完成**；§10.11.2 的 12 项与 §10.11.3 的 4 项全部处置，§10.11.4 撤销一条被证伪的 override；608 项测试通过、`check-egress-spec` 全绿、`backend/` 零改动；工具身份 97 项 `cacf51be…` |
| R9 官方双轨重采 | 创建新 Campaign，按双轨执行官方 `run`，逐 job 校验 receipt，再执行 `seal` | campaign、attempt、results、双证据根、secret scan、seal receipt | **已完成**（k47）：28/28 job、9 份收据、`UNREACHABLE` 归零、秘密扫描零命中、`official_sealed` 达成 |
| R10-a classify／画像 | 重新 classify、建立并批准 0.147 画像 | 五份已批准清单、profile digest | **已完成**（k47）：`blocked=0`、`unclassified_count=0`、画像 digest 与跨轮基准逐字一致、`profile_approved` 达成 |
| R10-b 候选验收（当前） | ARM64 服务侧验证 → `stage-profile` → candidate run/seal → compare → accept | ARM64 验证记录、RuntimeCatalog、candidate 收据、compare/accept 收据 | 无未登记漂移、双版本隔离、全部 seal 通过，达成 `ready` |
| R10-c 生产切换 | 按 §9 执行 canary、切换、回滚演练与恢复后 final-wire | canary 记录、切换收据、回滚演练记录 | Vircs canary 通过后才更新正式实例；失败保留证据并回滚 |

### 11.3 `SCN-REALITY-01` 收据最低字段

| 场景 | 最低字段 |
|---|---|
| A11 | `call_create_status`、`call_id_sha256`、`sdp_or_started_event`、`async_error_count`、`sideband_sni`、`final_state` |
| A13 | `trigger`、`token_request_method`、`token_request_path`、`oauth_sni`、`jwt_exp_observation`（自然窗口路径必填）、`capture_side_wrote_auth`、`rotated_by_refresh`、`final_state` |
| A14 | `tool_name`、`tool_call_id`、`create_request`、`upload_url_source_event`、`put_destination`、`upload_sequence`、`uploaded_event`、`regional_sni`、`final_state` |

每份收据还必须记录 `track`、`model_id`、`models_response_sha256`、`use_responses_lite`、
`model_fallback` 和 evidence root。字段值必须来自官方 CLI、relay、pcap 或驱动的原始事件；
编排器不得根据 job 退出码自行补写目标字段。缺字段、状态矛盾、异步 error、模型条件不符
或来源不一致均为失败关闭。

### 11.4 执行顺序

```text
R0 方案冻结                                          ✅
  ↓
R1 真实性门禁与负例                                  ✅
  ↓
R2 A11 V3 与最终事件等待                             ✅
  ↓
R3 A13 官方刷新路径与最终事件                        ✅
  ↓
R4 A14 Apps 工具与动态区域 SNI                       ✅
  ↓
R5 P0 预检与工具身份冻结                             ✅
  ↓
R6 k41 official_sealed                               ✅
  ↓
R7 classify 双轨拆分                                 ✅
  ↓
R8 双轨变更集与 P0                                   ✅
  ↓
R9 官方双轨 run → receipt 校验 → seal                ✅ k47（k42～k46 作废，见 §10.11.11）
  ↓
R10 ├─ classify → profile                            ✅ k47 profile_approved
    ├─ ARM64 服务侧验证（§11.0）                     ← 当前
    ├─ stage-profile → candidate run/seal
    ├─ compare → accept → ready
    └─ §9 canary → 切换 → 回滚演练                   ← 生产域，须逐步确认
```

任一阶段失败，保留原始失败证据并停止；不得通过改写判据、补写 receipt、复用 k34～k46
证据或静默回退 0.145 来推进流程。R10 后半段跨入生产域，**每一步都须先在隔离环境完成
验证**，不得直接更新正在对外服务的实例。

### 11.5 当前执行起点

**R0～R9 与 R10 的前半段（classify → profile）均已完成。** k47 依次达成
`official_sealed` 与 `profile_approved`，`next_command: capture-candidate`。模型策略保持
冻结：主线 `gpt-5.4`，Lite 专项 `gpt-5.6-luna`。

**当前停在服务部署域之前**。已完成的 ARM64 只读验证（独立临时源码树上的
`test-capture-tools`、`/health`、HTTP API 与 WebSocket 握手）只是兼容性确认，**不能替代
§11.0 要求的完整服务侧验证**——后者要覆盖编译、单测、启动、数据库迁移与恢复、出站
TLS／代理／超时、并发与可回滚性。

**k47 的证据与清单必须保持不可变**：在 candidate／compare／accept 走完之前不得修改任何
受管工具，否则工具漂移会让本轮 sealed 证据作废。§10.7 登记的收口器权限待办同受此约束。

| 阶段 | 状态 | 结果 |
|---|---|---|
| R0／R1 | 完成 | `SCN-REALITY-01` 场景真实性门禁落地，见下文 |
| R2 A11 | 完成 | 显式走官方 V3 并等待最终事件；call_id 判据改从 `Location` 头取（201＋`text/plain` SDP，非 JSON body） |
| R3 A13 | 完成 | 走 `account/read {refreshToken:true}`，绕开 `exp` 检查触发刷新 |
| R4 A14 | 完成 | 冻结 Apps 工具契约；工具调用事件字段是扁平 `item.{tool,server,status}`，非 Rust `ThreadItemDetails` 的嵌套形态 |
| R5 | 完成 | P0 预检与工具身份冻结 |
| R6 | 完成 | k41 达成 22／22、三份场景收据和 `official_sealed`；第四、第五层问题转入 §10.10～§10.11 |
| R6-A | 完成 | 9 条 selector 缺口逐条甄别，分四类 |
| R6-B | 完成 | 第一、二类补标签（缺口 9→5、断言 52→54、零回归）；第三类补 4 个条件／扰动样本 |
| k40 | 作废 | 采集与缺口修复均成功（缺口 0、`alpha/search` 采到），但 seal 的秘密扫描暴露工具缺陷（见下），修复即改工具身份 |
| **k41** | **`official_sealed`** | 22／22 job `complete`；秘密扫描 850 文件／148 MB 零命中；环境五项全 `restored`、数据库 426 主键零缺失；**历史首次达成** |
| **R7 classify** | 完成 | 主线固定 `gpt-5.4` 非 Lite；Lite-only 规则转入 `gpt-5.6-luna` 专项；17 处 select 修正后通过 59→70、失败 23→12、selector 缺口全程 0；剩 12 条见 §10.11.2 |
| **R8 双轨变更集** | **完成** | §10.11.2 的 12 项与 §10.11.3 的 4 项一次改完；608 项测试与 `check-egress-spec` 全绿 |
| k42～k46 | 全部**作废** | 五轮各自暴露一类缺陷并固化为门禁，逐轮死因与不变量见 §10.11.11 |
| **R9 / k47** | **`official_sealed`** | 28/28 一次跑通、九份收据齐备、`UNREACHABLE` 归零、秘密扫描 986 文件／163 MB 零命中（§10.11.9） |
| **R10 classify** | **`profile_approved`** | 42 条规则 41 `inherit`＋1 `change`；2808 条发现零 `blocked`；目标画像 57 PASS／0 FAIL／0 UNREACHABLE；画像 digest 与跨轮基准逐字一致（§10.11.10） |
| **R10 candidate 起** | **当前** | 跨入服务部署域，前置见 §11.0；工具身份锁定在 97 项 `cacf51be…` |

k40 的死因单独记一笔：`relay_extract.shape_value` 把 >24 字符的串降成 `str:<len=N>`，
而 `candidate_evidence_guard` 的 `json-secret-field` 白名单只认 `<redacted`／`<secret`，
于是把已脱敏的 `"refresh_token":"str:<len=211>"` 判成明文凭据——**两侧脱敏格式从未对齐**。
修法是给负向前瞻加「`str:<len=` ＋纯数字 ＋ `>`」一种确定形态，并用 8 例验证短值保留原文、
伪造长度、占位符后有残留三类仍照常命中，没有放宽安全边界。

**R8（已完成）**：一次性完成了 §10.11.2 的 12 项与 §10.11.3 的 4 项——建立 `gpt-5.6-luna`
Lite 专项而不改变 `gpt-5.4` 主线、补 `non_lite` 与 A01 relay 证据、改用 WS `response.metadata`
注入 turn-state、新增独立 WS 默认样本、实现 `TLS-003` 分组存在性算子、把 `EP-019` 期望值
变更收进目标版本 override，并合并 17 处 selector 修正。场景清单、`track`／模型字段和收据
schema 同步更新，受管工具由 96 项增至 98 项、身份为 `ac00085c…`。

**R9（已完成）**：k47 于 2026-08-11 达成 `official_sealed`，官方侧 28 个 job（3 个 Lite 专项）
一次跑通。**k34～k46 的证据一律不迁移、不复用**（唯一例外见 §10.11.10 的 source 发现继承）。

**「全量 selector 扫描先于 seal」这条纪律必须继续沿用到候选侧。** 它在 k42～k46 五轮里
每次都提前拦下了本会浪费整轮的缺陷——最省的一次（§10.11.4）只损失了一个 job 的采集量。
k47 官方侧的四项复核已全部通过：12 条判据转为可达且断言成立、两个 Lite 专项 job 的
`/models` 原文实证 `use_responses_lite=true`、连接序号假设成立、WS 默认握手不含
runtime_metrics 条件头。

**R10（进行中）**：classify 与画像已完成（§10.11.10）。候选侧执行前须复核 §11.0 的隔离
要求：独立账号、独立 `CODEX_HOME`、独立证据目录与独立实例，不触碰生产凭据、全局
`/etc/hosts`、生产 relay 或 Active/Previous。候选采集另有三条硬约束见 §10.2——验收链路
顺序、源码树在 run／seal 之间不可触碰、收据的两层结构。

k37～k39 的死因依次是：采集脚本三处缺陷（cleanup 在 `set -e` 下中止致 hosts 劫持残留、
A13 探针缺 `docker exec -i`、A11 call_id 取错来源）；判据过严加工具调用字段路径错误；
以及第四层的 selector 可达性。三者证据均不迁移。

k40 当时登记的三项待验（selector 缺口是否真转可达、`alpha/search` 是否真的发出、三个新
job 的 `variant` 是否落到正确请求上）**已在 k47 全部得到实证**：缺口归零、`alpha/search`
采到 3 条、compact 第三槽分别落在 `x-codex-window-id` 与 `x-codex-beta-features`。

**采集完成不等于缺口补上**——这条在 k43～k46 又反复应验了四次，任何一轮都须用与 §10.10
相同的全量扫描复核后才能进入 seal。
[`SCN_REALITY_01_SCENARIO_REALITY_GATE.md`](SCN_REALITY_01_SCENARIO_REALITY_GATE.md)
的 §1～§11 是 R0 冻结的契约，§12 是 R1 的实施记录。按仓库既有「收据只表达成功态」的
约定，判定为**缺收据即失败**，而不是读取收据里的成功标志——后者只需改一个字段值即可
伪造，前者要伪造整条 producer 链。

R0 登记的五项要求在 R1 已全部处置：两份场景清单的 `required_artifact_kinds` 已对齐并
补交叉一致性测试、三处冻结摘要按逐项复核后重算（25／17 分组未变）；A11／A13／A14 已
引入 `side_triggers` 分侧契约且不覆盖候选侧定义；收据固定路径、attempt 身份透传、
retry 独立目录与 A13 还原前后摘要证明均已闭合；A14 官方侧只声称响应 host、区域 SNI、
事件顺序与 uploaded 2xx；attempt `jobResult` schema 与不依赖 `jsonschema` 的运行时精确
校验已补齐。

R1 的效果已用 k36 的实际形态复现：子进程退出 0、证据目录非空、但目标协议分支一跳未发生
时，三个目标 job 全部判 `failed`。**这也意味着触发问题仍然存在**——R1 只保证「没触发」
不再被记成「完成」，真正的触发修正是 R2（A11 走官方 V3 并等待 started／SDP）、
R3（A13 走官方刷新路径并记录触发类型）、R4（A14 冻结 Apps 工具契约与动态区域 SNI）。

受管工具身份已随 R1～R6-B 多次变化，按 §11.1 每次都新建了 Campaign 完整重采；
k36～k39 不迁移、不复用。k40 因秘密扫描工具缺陷作废。当前身份冻结于 k41 的 plan，
**在 R8 一次性改完受管工具之前不得零散修改**——每改一次就要重建 Campaign 完整重采。R7 及后续阶段不得
直接替换 Vircs 正式服务，不执行未经 canary 验证的生产更新。

> 操作提醒：向 Vircs 传源码树时除 `chmod` 700／600 外还必须 `chown -R root:root`。
> 从 macOS 打包的 tar 会带着本地 uid／gid，解包后驱动脚本会以
> `[Errno 13] Permission denied` 失败，k40 的第一个 attempt 即因此全废。
> 权限与属主都不进工具身份摘要，修正不影响已建 Campaign。
