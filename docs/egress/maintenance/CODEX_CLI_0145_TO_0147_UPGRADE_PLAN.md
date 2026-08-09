# Codex CLI 0.145.0 → 0.147.0 Official Egress 升级计划（执行中）

> 状态：当前 Campaign `…-k35`，位于 **§6 之内**（官方 `run` 17/17 完成，`seal` 未通过）。第一层的验收链路接线已闭环（§10.8，ACC-01～07）；当前阻塞项是第二层的采集覆盖缺口（§10.9，8／15 场景待定案）。k34 已作废停在 `compared`。Active 仍为 0.145
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

当前有效记录（Campaign `codex-0145-to-0147-20260809T101826Z-k34`）：17 个官方场景一次
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
必须 `blocked=0`。

新出站面不是失败；未发现、未分类或静默回落旧画像才失败。删除规则必须同时具备源码
不可达结论、正反场景、旧引用清单和 RemovalReceipt，否则保持 `blocked`。

当前有效记录（Campaign `codex-0145-to-0147-20260809T101826Z-k34`）：`classification/result.json`
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

> 时间：2026-08-10。当前 Campaign `codex-0145-to-0147-20260809T170000Z-k35`。
> **Active 仍为 0.145，0.147 尚未替换。**

### 10.1 按章节的真实进度

**当前位置：§6 之内，卡在 §6.2 `official_sealed` 之前。§6.4 classify 从未执行过。**

| 章节 | 状态 | 依据 |
|---|---|---|
| §5 DOC-PRE 与 P0 | 完成 | 不依赖 Campaign，结论长期有效 |
| §6 Campaign／官方取证／分类 | **进行中** | k35 `planned` 完成；官方 `run` 17/17 complete；`seal` 未通过，见 §10.9 |
| §7 建立 0.147 画像 | 未开始 | |
| §8 Candidate／比较／`ready` | 未开始 | |
| §9 生产启用与回滚 | 未开始 | |

k34 曾到达 `compared`，但 ACC-01～07 改变了受管工具身份，k34 已作废并停在 `compared`，
其证据只用于根因复现与离线夹具，不迁移。**ACC-01～07 全部发生在「§6.2 seal 之前」这
一个点上**，是让 seal 有可能通过的前置修复，不构成章节推进。

0.147 画像 digest `0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f`
由 k26～k34 六次独立产出逐字一致，可作为后续画像复算的对照。

### 10.2 采集操作要点

候选验收链路的正确顺序、源码树在 run／seal 之间不可触碰、收据的两层结构、采集期间的
环境要求，以及历轮已修复的缺陷清单，见
[`CAPTURE_OPERATIONS_NOTES.md`](CAPTURE_OPERATIONS_NOTES.md)。这些不随单个 Campaign
变化，采集前必读。

### 10.7 剩余路径

1. **§10.9 的采集覆盖缺口**：8／15 场景待逐项定案（补 job／改场景定义／classify 标
   `change`），这是当前唯一阻塞项；
2. `official_sealed`：编目 → 收口 → 派生 → seal 门禁 → 封存；
3. `profile_approved`：classify 42 规则，含 SPEC-TLS-003 的判据修订（§10.9.2）；
4. §7 建立画像 → §8 候选采集、compare、新 `accept` → `ready`；
5. §9 生产启用：canary、切换、回滚演练、恢复后 final-wire，最终 Active=0.147 / Previous=0.145；
6. 收尾：恢复分组 9 的 `allow_image_generation`（原值 false）。

用户已授权按第 5→6→7→8→9 章连续执行；涉及规则判据变更或场景定义变更时须先定案。

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

### 10.9 第二层：采集覆盖与判据错配（当前阻塞项）

修好验收链路、真正跑通编目后暴露的问题：**场景定义的 `required_artifact_kinds` 与
capture job 实际产出的证据类型系统性错配**。与 §10.8 不是同一层——接线不通就跑不到
编目，跑不到编目就看不见证据缺口。

根因与第一层相同：历史 25 个 Campaign 无一产出过 `results.json`，链路错配与证据错配
两层问题一直并存，只能逐层剥离。

#### 10.9.1 缺口清单（k35 官方侧实测，8／15 场景）

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

#### 10.9.3 处置原则与待定案项

每个缺口须逐项判定属于哪一类，**不得由执行者临时决定**（历轮 capture-manifest 由执行者手写正是缺陷来源）：

1. **该采而未采** → 补 capture job（如 A02 的处置）；
2. **场景定义写错** → 修订该场景的 `required_artifact_kinds`，附实证；
3. **判据与采集能力错配** → classify 标 `change` 并附实证（如 SPEC-TLS-003）。

8 个缺口尚未定案，是 §6.2 `seal` 的唯一阻塞项。定案并实施后，按 §10.7 顺序推进。
