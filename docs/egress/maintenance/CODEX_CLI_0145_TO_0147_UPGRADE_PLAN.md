# Codex CLI 0.145.0 → 0.147.0 Official Egress 升级计划（执行中）

> 状态：Campaign `…-k34` 已到 `compared`；§8 的 `accept` 实证阻断，根因见 §10.8，**待第三方审核后再动工**。Active 仍为 0.145
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

> 时间：2026-08-09。当前 Campaign `codex-0145-to-0147-20260809T101826Z-k34`，状态 `compared`。
> **Active 仍为 0.145，0.147 尚未替换。**

### 10.1 按章节的真实进度

| 章节 | 状态 | 依据 |
|---|---|---|
| §5 DOC-PRE 与 P0 | **完成** | 不依赖 Campaign，结论长期有效 |
| §6 Campaign／官方取证／分类 | **完成** | 官方采集 17/17 一次通过；封存 409 文件、findings 0；classify 42 规则 complete |
| §7 建立 0.147 画像 | **完成** | `stage-profile` 产出候选 RuntimeCatalog，`production_selector_changed: false` |
| §8 Candidate／比较／`ready` | **进行中** | 候选采集 8/8、候选封存 complete（528 文件、findings 0）、compare complete；**剩 accept → ready** |
| §9 生产启用与回滚 | **未开始** | |

0.147 画像 digest `0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f`，
经 k26／k27／k28／k29／k33／k34 六次独立产出逐字一致；compare 的
`profile_binding_matches` 为真。

### 10.2 候选验收链路的正确顺序（此前一直做反）

`capture-candidate seal` 第一次调用输出的"使用上述不可变边界生成收据"，指的是**用这个
边界去校验已经发生的请求**，不是"之后再发请求"。`client_checkpoint_at_utc` 来自一次
真实的 Kilo 后环境探针，第三方入站请求必须落在它**之前**：

```
capture-candidate run（8 个任务）
  → 人工用 ZLF Code 发 Compatible 一条、Responses 一条
  → capture-candidate seal（采 client-after 探针，得到 checkpoint）
  → 生成 observed-profile 与两份 Kilo 收据
  → capture-candidate seal --approve-seal-sha256
```

顺序反了会同时踩两个坑：入站请求晚于 checkpoint 被判"不在客户端证据时间窗内"；为迁就
它去刷新 checkpoint，又会把服务运行画像观测挤出窗口。两者只能靠正确的先后顺序同时满足。

被归档的 `client-after` 探针目录可以重采以推后 checkpoint——但这只在**没有动过候选源码树**
时可行，见 §10.3。

### 10.3 候选源码树在 run 与 seal 之间不可触碰

`_directory_tree_digest` 扫描整个候选源码树，**不排除 `__pycache__`**，而 `.pyc` 内嵌源
文件的 mtime。因此在 `run` 之后向该目录写入任何文件——哪怕随后逐字还原——都会让摘要
永久漂移，`seal` 以"候选源码树在 run／seal 之间发生漂移"拒绝，只能重跑候选采集。

**收据生成器必须放在候选源码树之外运行**（例如 `runtime/build_kilo_receipts.py`），
产物写进 attempt 的 `evidence/receipts/`。

### 10.4 收据是两层结构

`build_*` 系列产出的是 **finalizer 的输入**，不是可直接提交给 seal 的收据：

| 环节 | 输入产出方 | 正式收据产出方 |
|---|---|---|
| 运行画像 | `build_observed_profile_runtime_audit.py` | `receipt_finalizer observed-profile` |
| Kilo 双协议 | `build_kilo_client_receipts.py` | `receipt_finalizer kilo-binding` |

直接把输入交给 seal 会得到"收据 schema_version 不受重放器支持"。另外 kilo-binding 要的
`runtime_audit` 与 observed-profile 那份**不是同一种**：前者描述单次调用的出站形态
（transport／upstream_endpoint／auth_mode／affected_branches），后者描述服务整体解析到
哪个画像。五份 Kilo 事实还要求时间非递减：安装 ≤ 入站 ≤ 出站 ≤ 响应 ≤ 用量。

### 10.5 采集期间的环境要求

- **激活事实必须在采集窗口内落盘**。部署 override 需注入
  `GATEWAY_EGRESS_ACTIVATION_FACT_PATH` 与全套身份变量，其中 `SOURCE_TREE_SHA256` 必须
  取**候选 attempt** 的 `identity.source_tree_sha256`（不是 official attempt 的）；服务会
  用自己解析的 digest 与声明逐字比对，不一致拒绝落盘。
- **合成 relay 的固定响应 ID 每轮必撞计费去重**。`resp_candidate_core_*` 被服务当作计费
  request_id，上一轮的记录会让本轮 fingerprint 冲突、用量行不落盘，Kilo 收据因此缺
  `usage_id`。已在 `kilo-byte-capture-r12.sh` 启动时自动清理。
- **Codex Desktop 会污染采集**。它同样使用账号 90 并周期性访问 `/v1/responses`，
  其请求会被 relay 判为"抓到了"。采集前必须退出，并确认一段时间内零新增。
- **ZLF Code 两条入口的 User-Agent 形态不同**：Compatible 走 ai-sdk（UA 自报
  `Kilo-Code/<版本>`），Responses 的 WebSocket 走 OpenAI 官方 JS SDK（UA 为 `OpenAI/JS`）。
  收据工具对后者回落到本机安装事实取版本。
- **WS 入站的 access log 在连接关闭时才写**，其 `completed_at` 是连接生命周期结束时刻，
  不是响应完成时刻；response witness 应取用量记录时刻，否则与 usage 的时间顺序倒挂。

### 10.6 本轮修复的缺陷

| 缺陷 | 提交 | 说明 |
|---|---|---|
| A13：`req.Client` 链 Guard 包装顺序 | `31768d0a6` | 既有生产缺陷，OAuth token 刷新被自身 Guard 拒绝，详见 §7.7 |
| A11：Linux Live attestation 接线 | — | `candidatecapture` 分支引用不存在的函数，详见 §7.8 |
| 环境恢复把容器重建判成污染 | `e5717d678` | 容器实例 ID 与 hosts 自引用行由 Docker 生成，不表达采集副作用 |
| A12 期望计数写错 | `7178edbf9` | `ResetQuota` 必然再查一次用量，实际是 2/2/1 |
| images-wire 不自备模型映射 | `7178edbf9` | 生图请求在入口即 404、根本不出站 |
| 候选镜像不可复现 | `e78a7556a`、`ee7170c32` | `BUILD_TAGS` 与 `FRONTEND_SOURCE` 参数化 |
| 同 attempt 内无法补跑失败任务 | `3fcc465a9` | 上游波动导致整轮 20 分钟作废 |
| resume 丢弃上一轮成功任务 | `9bb0ec767` | 承接 + 环境连续性证明 |
| comp-hash-changed 依赖 gpt-5.6-luna | `f94efb85b` | 改用受控模型目录 |
| 官方 CLI 偶发波动无重跑 | `9ffb836ce` | s4 的 hook 计数偶发变 2 |
| Kilo 两条入口 UA 形态不同 | `88937e40b` | 详见 §10.5 |
| 四类收据无产出方 | `65983ce15`、`0930a05aa` | capture-manifest、逐规则验收结果、Kilo 收据 |
| kilo-binding 缺单次调用 runtime_audit | `58cd4b670` | 与 observed-profile 的整体审计不同种，见 §10.4；内容承接服务端记录，工具不推断 |

### 10.7 剩余路径

1. `accept`：按 §10.8.10 的 `dual_wire`／`candidate_profile` 两种模式执行 42 条必需规则并汇总，
   由 `build_rule_assertion_results.py` 编排；不再对 17 条内部事实规则强制执行官方侧同构断言；
2. `ready`：重放 accept、收据、安全与 evidence seal；
3. §9 生产启用：canary、切换、回滚演练、恢复后 final-wire，最终 Active=0.147 / Previous=0.145；
4. 收尾：恢复分组 9 的 `allow_image_generation`（原值 false）。

用户已授权按第 5→6→7→8→9 章连续执行；每个变更集完成并自复核后自动进入下一项。

### 10.8 `accept` 实证阻断与根因（审核后收敛，未修复）

> §10.7 第 1 项已在 k34 上实测阻断。第三方审核确认：多证据根接线缺陷属实，但原结论
> 把问题过度收窄为索引接线，遗漏了双侧证据模型和正负例契约缺陷。本节记录审核后的
> 完整结论与最小安全闭环，**尚未实施任何修复，也不授权继续推进 k34**。
>
> 本节的验收模型取代 §10.7 第 1 项“42 条规则双侧各执行一次（84 次）”的旧表述；旧表述
> 只保留为 k34 的阻断路径，不再作为后续 Campaign 的实施依据。
>
> 二次复核（2026-08-09）确认六项根因与验收模型方向成立，另发现官方侧 21／25 条 wire
> 规则的场景 artifact 覆盖依赖 `process_trace`／`websocket_trace` 结构化记录而无正式
> 产出器；已按复核结论增补 10.8.9 实证、10.8.10 分侧覆盖语义与 10.8.11 的 ACC-02b。

#### 10.8.1 阻断现象

在 k34（`compared`）上按 compare 阶段自动生成的
`assertions/<candidate-id>/results.template.json` 中的权威命令执行单规则断言，
SPEC-TLS-001 四个 check 全部失败，且 `actual.values` 为**空数组**——不是取值不符，
而是一条记录都没被选中：

```
scenario-artifact-coverage  FAIL  actual=null
http-clienthello-count      FAIL  actual=null
cipher-count                FAIL  actual=[]
alpn-absent                 FAIL  actual=[]
```

复现：取 `results.template.json` 中任一规则的 `official_command` 原样执行即可。
所用参数（manifest／evidence-root／profile／`--expected-profile-sha256`）与模板逐字一致，
排除调用方配置错误。

#### 10.8.2 已核实事实与尚未闭环的证据边界

初次排查曾误判为"证据缺失"，原因是只枚举了单个 run 目录。以 `official/result.json` 的
`evidence_inventory` 为准，409 条证据分布在 **20 个 run 目录**：

| run 目录 | 条目数 |
|---|---|
| `oauth-<campaign>` | 142 |
| `<campaign>-official-relay-compact` | 55 |
| `<campaign>-official-image-edit` | 17 |
| 其余 17 个 relay／辅助 run 目录 | 195 |

17 个官方 job 中 14 个为 relay job，k34 全部通过。relay 产物形如
`relay/conn022.client_to_upstream.bin`，内容为明文 HTTP/1.1：

```
GET /backend-api/codex/responses HTTP/1.1
Host: chatgpt.com
Connection: Upgrade
Upgrade: websocket
```

`candidate_rule_assertion.py` 的 `h1_request_stream` 与 `pcap_client_hello` 原始解析器可直接
产出 `http_request`、`websocket_frame` 与 `tls_client_hello`，能够完整支撑 42 条规则中的
**25 条 wire 可观测规则**。

其余 **17 条规则**还需要以下结构化记录类型：

```
alpha_search_flow              compaction_decision
conditional_header             connection_lifecycle
file_upload_chain              header_assembly
image_edit_encoding            image_tool_flow
lite_transform                 realtime_chain
response_prefix_reuse          serialization_boundary
surface_identity               transport_fallback
turn_state_chain               websocket_compression_context
```

仓库内现有正式产出器 `candidate_test_trace.py` 只把**候选部署源码的 Go 测试事实**转换为这些
记录；它不能给官方 Codex CLI 侧生成同类内部事实。当前也没有一套官方侧结构化事实产出器。
因此只能确认 25 条 wire 规则的原始证据路径可复用，不能据此宣称 42 条规则的双侧证据均已
齐备，也不能宣称“不需要新增派生器”。

k34 的原始证据是否足以离线派生全部官方内部事实不再作为必解问题：审核后的最小验收模型
不要求官方侧伪造 Sub2API 内部状态，见 10.8.10。由于修复必然改变受管工具身份，正式验收仍
必须按新 Campaign 重采；k34 证据只用于根因复现、只读分析和离线测试夹具。

#### 10.8.3 缺陷一：capture-manifest 覆盖不全且标签语义错误

两侧 `capture-manifest.json` 各只登记 6 个 pcap，未登记分散在其余 run 目录中的断言相关
relay／process／WebSocket 证据；且标签语义与断言画像不一致：

| | 取值 |
|---|---|
| 断言画像 `select.where` 要求 | `labels.transport == "http"`、`labels.ca_mode == "system"` |
| manifest 实际 | `transport: "direct"`，无 `ca_mode` |

`transport` 在两处不同义：manifest 用于表达"直连／代理"，断言画像用于表达协议面。
该 manifest 由本次执行者在 k33／k34 为通过 seal 临时编写的规则文件生成，只覆盖了主
run 目录，属操作失误。但 `evidence_inventory` 与 capture manifest 本来就不是全集等价关系：
恢复收据、配置和其他非断言文件只需进入 inventory，不应为了追求“409 条全部登记”而伪装
成断言 artifact。正确门禁是覆盖全部在用场景要求的**断言相关原始／派生证据**。

即使修正登记范围和标签，10.8.4～10.8.7 仍会使旧 `accept` 失败。

#### 10.8.4 缺陷二：采集阶段是多根，断言阶段是单根

`tools/official_client_capture/codex_upgrade.py:7389`：

```python
evidence_root=str(context.get("evidence_root", "")),
```

`_campaign_machine_command`（同文件 `:7368`）以**单数** `evidence_root` 构造断言命令，
而 official stage 实际封存的是 `evidence_roots`（复数，本轮 20 个，见
`official/result.json`）。因此模板为每条规则生成的命令只能看到约 1/20 的证据，relay
证据全部在断言器视野之外。

单根假设并不只存在于这一行，还贯穿以下入口：

- `codex_upgrade.py::_capture_assertion_context`；
- `candidate_rule_assertion.py::load_observations` 与命令行 `--evidence-root`；
- `build_capture_manifest.py::build_manifest`；
- `build_rule_assertion_results.py` 的两侧配置；
- `candidate_test_trace.py` 的原始来源绑定。

因此只修改 `_campaign_machine_command` 不能闭环；直接把 `--evidence-root` 改成可重复参数也会
扩大所有消费者的路径、摘要、重放和防逃逸边界。审核后选择更小的方案：**每侧在 seal 前
生成一个统一断言证据包**，见 10.8.10。

#### 10.8.5 缺陷三：断言证据路径与 inventory 路径空间不一致

`_validate_evidence_bindings`（`codex_upgrade.py:7338`）以
`inventory.get(path) != digest` 校验 results 文档提交的证据绑定：

- inventory 条目路径带 run 目录名前缀，如
  `oauth-<campaign>/mitm/codex-http/s1/models-http.jsonl`；
- `build_rule_assertion_results.py::collect_evidence_bindings` 写出的 evidence refs 相对
  单个 `--evidence-root`，如
  `mitm/codex-http/s1/models-http.jsonl`。

两者不在同一路径空间，校验必然失败。`_validate_machine_assertion` 后续会给机器 check 的
`evidence_paths` 加上 `evidence_prefix`，但这不能修复此前已经提交给
`_validate_evidence_bindings` 的无前缀 evidence refs。

修复必须让 results 文档从产出时就使用 inventory 的逻辑路径，不能在校验端模糊匹配或按
basename 回退，否则同名 run／文件会造成证据错绑。

#### 10.8.6 缺陷四：`seal` 不校验场景 artifact 覆盖

`codex_upgrade_scenarios_0_145_0.json` 同时定义了 `evidence_scenarios`（每个场景的
`required_artifact_kinds`）与 `capture_jobs`（每个 job 的 `scenario_ids`），但两者之间
没有任何强制校验，`seal` 也不检查 capture-manifest 是否满足各场景的
`required_artifact_kinds`。因此一份只登记 6 个 pcap 的 manifest 仍可通过封存，缺陷一直
到 accept 才暴露。

15 个在用场景的要求为：

```
A01 pcap,relay_binary          A09 relay_binary,process_trace
A02 pcap                        A10 relay_binary,process_trace
A03 relay_binary,process_trace  A11 pcap,relay_binary,process_trace
A04 relay_binary,process_trace  A12 relay_binary
A05 relay_binary,websocket_trace A13 pcap,relay_binary
A06 relay_binary,websocket_trace A14 pcap,relay_binary,process_trace
A07 relay_binary,process_trace  A15 relay_binary,process_trace
A08 relay_binary,process_trace
```

该门禁只能称为“artifact 类型覆盖”，不能单独宣称“证据充分”：文件类型齐备仍不代表解析
非空、selector 能命中或规则值正确。seal 至少还必须验证每个登记文件的摘要、解析非空、
structured trace 的 `source_artifacts` 闭合，以及 artifact 只来自本侧已封存的证据根。

#### 10.8.7 缺陷五：旧 `accept` 错把两类规则强制成同一种双侧模型

旧模板要求 42 条规则均在官方／候选两侧运行同一个 `candidate_rule_assertion.py`。这对 25 条
HTTP／TLS／WS wire 规则成立：两侧都能从原始字节产生同类观测并接受同一画像判据。

但其余 17 条规则描述的是调用链、状态链、连接生命周期或 Sub2API 实现事实。候选侧可以用
冻结 Go 测试日志、候选源码摘要和同场景 relay 生成结构化 trace；官方 CLI 侧没有 Go 实现，
也不应生成 Sub2API 内部事实。强迫官方侧提供同构 check，不会增加安全性，只会制造无法满足
或可被手工派生事实绕过的门禁。

正确边界是：

- 25 条 wire 规则执行官方／候选双侧机器断言；
- 17 条内部规则只在候选侧执行机器断言，并绑定批准画像、候选源码／测试摘要及同场景原始
  relay；
- 官方侧对这 17 条规则的权威来源是已封存官方证据经过 classify 形成的批准画像和联合摘要，
  不再伪造一份“官方内部状态”机器结果。

#### 10.8.8 缺陷六：正负例字段没有可执行语义

旧 `accept` 要求每条规则的 `positive_assertions` 与 `negative_assertions` 均非空、互不相交，
且为双侧 check ID 交集的子集。但冻结断言画像没有 `polarity` 字段，部分规则只有一个领域
check；机器结果另含通用 `scenario-artifact-coverage`，它也不是负例。

`build_rule_assertion_results.py` 又只接受 `status == "pass"` 的两侧结果，随后按 check 的
`passed` 真假分类。成功结果的全部 check 必然为真，所以 `negative_assertions` 必然为空；
现有单元测试甚至把该空数组写成期望值，未覆盖 builder → accept 的真实集成链。

审核后不再保留这个没有来源的人工分类。新 `accept` 应直接要求：

1. 本规则应执行的 check ID 与批准断言画像逐项一致；
2. `scenario-artifact-coverage` 存在且通过；
3. 全部 check 均为 `passed == true`，并由 accept 离线重放得到逐字段相同结果；
4. 断言画像中表达缺席、禁止值或条件关闭的 check 本身就是负向判据，不再另抄一份
   `negative_assertions` 清单。

若未来确需统计语义正负例，必须在批准断言画像中显式增加 `polarity` 并逐规则审核，不能从
执行结果真假或 check 名称猜测。

#### 10.8.9 已验证可行的部分

在不触碰封存证据的前提下（临时 evidence root，证据只读复制）验证过两点：

1. 由官方 mitm 流记录派生 `codex-candidate-observation/v1` 记录并登记 manifest 后，
   SPEC-EP-006 的 `models-method-path-query` **通过**；
2. observation 的 `source_artifacts` 必须同为 manifest 内登记项，否则报
   "结构化 trace 引用了 manifest 外证据"——原始证据需以 `opaque_bound_source` 一并登记。

该实验只证明以下局部链路正常：单根内的原始／派生证据解析、画像加载、
`--expected-profile-sha256` 冻结摘要覆盖和 source binding。它不能证明多根编排、42 条规则
双侧证据或 results → accept 已经闭环。

二次复核补充实证（2026-08-09，只读核查仓库内冻结画像、场景定义与断言器实现）：

- `scenario-artifact-coverage` 按规则绑定的每个场景要求其 `required_artifact_kinds`
  **全部**已在 manifest 登记（`issubset` 硬判）。机器统计批准断言画像：25 条 `dual_wire`
  规则中 **21 条**绑定的场景要求 `process_trace` 或 `websocket_trace`，仅
  `SPEC-TLS-001`、`SPEC-TLS-003`、`SPEC-PROTO-001`、`SPEC-EP-019` 四条可由纯
  pcap＋relay 满足覆盖；
- 这两种 kind 仅接受 `observation_json`／`observation_jsonl` parser，不能以 opaque 原件
  顶替，而官方侧没有任何正式的结构化观测产出器（`candidate_test_trace.py` 只收口候选
  Go 测试事实）；
- 本节实验中 SPEC-EP-006（绑定 A09，要求 `process_trace`）正是靠手工派生 observation
  才通过——它不是孤例，而是 21 条规则在官方侧的常态。且 A09 类以 mitm jsonl 为原始
  形态的场景，`http_request` 领域 check 本身也只有派生 observation 才能命中，mitmproxy
  原始记录不是 observation schema，无法被 selector 直接消费；
- 结论：官方侧断言要走通，必须有正式的确定性观测派生器（ACC-02b）。否则即使原
  ACC-01～05 五项全部完成，新 Campaign 的 accept 仍会在官方侧 21 条 wire 规则的
  `scenario-artifact-coverage` 上再次集体阻断。

#### 10.8.10 审核后的最小安全验收模型（未实施）

后续 Campaign 按以下模型收口，不再扩建一套全链路多根断言协议：

1. **官方权威不变**：官方 0.147 真实取证、inventory、secret scan、恢复、seal、classify
   与批准画像联合摘要继续作为规则权威；不降低任何现有门禁。
2. **每侧统一断言证据包**：在 seal 前，把各 job 根中与断言有关的原始文件以只读字节复制
   收口到本侧 attempt 下的独立 `assertion-bundle/`。禁止符号链接和硬链接；生成 provenance
   收据，逐项绑定来源 inventory 逻辑路径、来源摘要、目标相对路径和目标摘要，复制前后摘要
   必须一致。
3. **单根执行保持简单**：capture manifest、派生 trace 和其 source artifacts 全部位于
   `assertion-bundle/`；断言器继续只读取一个 `--evidence-root`，避免把路径防逃逸、重放和
   摘要协议扩展到任意多根。
4. **manifest 分侧精确覆盖**：只登记断言相关 artifact，不追求复制全部 inventory。每侧
   应覆盖的场景×artifact kind 矩阵由批准断言画像机器推导，不手写：候选侧取 42 条规则
   引用场景的全部 `required_artifact_kinds`；官方侧只取 25 条 `dual_wire` 规则引用场景的
   `required_artifact_kinds`。官方侧场景要求的 `process_trace`／`websocket_trace` 由
   ACC-02b 派生器从官方已封存原始记录确定性派生，不因官方侧无内部事实而放宽或删除
   场景 kind 要求。标签与批准断言画像的 selector 语义一致，所有 parser 均非空且
   structured trace 来源闭合。
5. **两种逐规则模式**：results 每条规则显式记录 `validation_mode`：25 条 wire 规则为
   `dual_wire`，要求官方／候选双侧命令和结果；17 条内部规则为 `candidate_profile`，要求
   候选机器结果并绑定批准画像、候选源码、冻结测试映射／日志及原始 relay。
   `candidate_profile` 行不携带官方侧命令、机器结果与证据引用三件套，改为逐字绑定批准
   断言画像 SHA-256、classification package digest 与联合 `review_sha256`，任一缺失或为
   空即拒绝；其官方权威即 10.8.7 所述批准画像链，不得以空数组或占位值伪装双侧结构。
6. **取消人工正负例抄写**：results 不再携带无权威来源的 `positive_assertions`／
   `negative_assertions`；accept 从批准画像复算应有 check ID，全量重放并要求全部通过。
7. **路径统一**：bundle 内 manifest 和机器结果使用相对 bundle 根的路径；写入 Campaign
   results 的 evidence refs 必须由 `assertion_context.evidence_prefix` 转换成 inventory 逻辑
   路径后再提交，校验端只做精确路径＋摘要匹配。
8. **`ready` 门禁不变**：逐规则验收通过后仍须重放收据、安全、恢复和 evidence seal；
   `ready` 仍不等于生产启用。

该模型保持升级目标不变：以真实官方 0.147 画像为权威，证明候选完整实现 42 条规则；只移除
“官方侧必须生成 Sub2API 内部事实”和“每条规则人工填写正负列表”两个没有事实来源的要求。

#### 10.8.11 实施变更集与开工条件

严格按以下顺序一次只实施一个变更集；每项完成测试与复核后才能进入下一项：

1. **ACC-01 验收契约**：冻结 25／17 规则分组、`validation_mode`、新 results schema（含
   `candidate_profile` 行的官方侧画像绑定字段，见 10.8.10 第 5 条）、旧 schema 拒绝策略及
   check ID 全集判据（批准画像 check 与通用 `scenario-artifact-coverage` 的并集）；分组与
   两侧应覆盖的场景×artifact kind 矩阵均由批准断言画像机器生成并固定摘要，不能手写
   25／17 清单或覆盖矩阵。
2. **ACC-02 证据包**：新增确定性 assertion-bundle／provenance 产出器；覆盖同名根／文件、
   路径逃逸、符号链接、硬链接、来源漂移、复制后漂移和缺失 artifact kind 的负例。
3. **ACC-02b 官方侧观测派生器**：新增确定性派生器，把官方已封存的 mitm／relay 原始记录
   投影为 `codex-candidate-observation/v1` wire 记录（仅 `http_request`／`websocket_frame`／
   `tls_client_hello`，禁止一切内部 record type），按场景要求的 kind 登记进 bundle
   manifest，`source_artifacts` 逐项绑定 bundle 内官方原件；同输入必须逐字节同输出。
   覆盖来源缺失、跨侧输入、内部 record type、来源未登记与派生结果漂移的负例。
4. **ACC-03 seal 门禁**：seal 重放 provenance，按 10.8.10 第 4 条的分侧矩阵校验 manifest
   artifact 类型覆盖、摘要、解析非空、标签契约及 structured source 闭合；任一缺失即失败
   关闭。
5. **ACC-04 结果编排与 accept**：按两种 validation mode 生成结果，统一 inventory 逻辑路径，
   accept 离线重放；删除无语义的正负例划分。
6. **ACC-05 端到端复验**：离线夹具完整覆盖 25 条 dual-wire、17 条 candidate-profile、42 条
   全集、双侧摘要错绑、规则错分、缺 check、伪造 pass、路径前缀碰撞及 builder → accept →
   status／ready 重放；夹具须复用 k34 只读证据结构，先在离线环境把新 accept 全链路跑绿，
   再允许新建 Campaign。

上述六项均会改变受管工具身份，k34 必须保持不可变并停在 `compared`。六项全部审核通过、
`make test-capture-tools` 与 `make check-egress-spec` 全绿、工作区干净且工具摘要重新冻结后，
才允许按 §6.1 新建 Campaign，重做官方取证、分类、画像、候选采集、比较和新 `accept`。

#### 10.8.12 结论

历史 25 个 Campaign 中无任何一个产出过 `assertions/<candidate-id>/results.json`，k34 是首个
到达 `compared` 的轮次。它暴露的不是 0.145 → 0.147 画像升级本身不可行，而是旧 `accept`
从未完成系统集成：**多根采集与单根断言不一致、17 条内部规则被错误要求双侧同构、正负例
契约没有权威语义、seal 又未提前拒绝不完整 manifest**。

k34 的官方／候选证据和 compare 结论仍有诊断价值，但因工具身份与验收契约将变化，不得迁移
为新 Campaign 的正式证据。后续只实现 10.8.10 定义的最小安全闭环，不借本次画像升级继续
扩张无关验收基础设施；新 Campaign 到达 `ready` 前，Active 始终保持 0.145。
