# Codex CLI 0.145.0 → 0.147.0 Official Egress 升级计划（执行中）

> 状态：当前 Campaign 已完成 `profile_approved`，但第 7 章候选画像生成因摘要不一致阻断；待修正画像摘要后重新开立 Campaign
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

本次已完成：Campaign `codex-0145-to-0147-20260808T054501Z-k22` 的
`official_sealed` 已完成，`official/result.json` 状态为 `complete`，
`stage=capture-official`，封存时间为 `2026-08-08T07:12:23Z`。官方二进制为 `0.147.0`，
CLI SHA `cb0a1556…`，Code Mode helper SHA `00ecf5d0…`，package asset SHA
`bd758d53…`；原始证据仍只保留在 Vircs 私有采集根。

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

本次已完成：Campaign `codex-0145-to-0147-20260808T054501Z-k22` 的分类结果为
`complete`，`classification/result.json` 状态为 `complete`，`stage=classify`，封存时间为
`2026-08-08T07:38:37Z`。联合摘要为
`61dc9a7422bb491c580b445ead8ad3ec7ae72c3bb1d47e29ead12ee0534ffafe`；42 条规则、2769 条
发现全部完成分类，`blocked=0`。批准清单及摘要如下：

| 清单 | 封存 SHA-256 |
|---|---|
| `target-rules.json` | `91616975b1a7e3e3717a99c72a937b8f6901a921c434decb2ee7fdab85c4f75b` |
| `rule-migration.json` | `4c8984faa65c7fe5d6eaad28067550b4323f3996d84adf30c363edb830454b04` |
| `scenarios.json` | `ea49de32cb5898547ae8ccf251ba0ecd24ad2ee27517a47ac1a95a7e365d1715` |
| `profile.json` | `fe7734954b12df6338f69cb659e334d280ae1629e3e2ac9197bcade9c0c2e038` |
| `assertion-profile.json` | `9e51781f6d2d92a7f8363e4859f8699c21290ae2cc01ab0539f69de17080feac` |

目标画像为 `codex-0.147.0-official-k22-v1`，Profile digest 为
`0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f`。五份清单已写入
Campaign 的 `classification/approved/`，并通过 stage-contract、文件摘要、画像 payload
摘要和场景覆盖复核；尚未写入仓库 Active selector。

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

> 时间：2026-08-09。当前 Campaign 为 `codex-0145-to-0147-20260809T042514Z-k29`，官方采集执行中。
> **Active 仍为 0.145，0.147 尚未替换。**

### 10.1 按章节的真实进度

| 章节 | 状态 | 说明 |
|---|---|---|
| §5 DOC-PRE 与 P0 | **完成** | 不依赖 Campaign，结论长期有效 |
| §6 Campaign／官方取证／分类 | **k29 进行中** | 官方采集执行中；k28 曾一次通过 17/17，封存与 classify 均 complete |
| §7 建立 0.147 画像 | **内容已定，k29 待重做** | 画像 digest 已三次独立产出一致；k29 需重新 `prepare-profile` → `stage-profile` |
| §8 Candidate／比较／`ready` | **采集链路已验证，seal 起从未执行** | k28 候选采集 8 个任务全部执行、A11–A14 全部产出 actions；`seal`／`compare`／`accept` 尚未跑过 |
| §9 生产启用与回滚 | **未开始** | |

**Campaign 产物不累积，知识与代码修复累积。** 每次修受管工具都会改变工具身份、使
在途 Campaign 作废（k22→k23…→k28 皆因此重来）。反复重来的代价换来的是下面这批
缺陷全部被找出并修复；到 k29 为止，所有已知阻塞点都已消除。

### 10.2 已确立的产物

0.147 画像已经确定，且经过三次交叉验证：k26、k27、k28 各自独立走完
`prepare-profile` → `stage-profile`，得到的 profile digest 逐字相同，`profile_id` 不参与
内容寻址。

| 项目 | 值 |
|---|---|
| 目标画像 digest | `0d86e033716ab2b7d2161a7015ad000bc0d7cedfaa9e130342eec4ba0637ef9f` |
| Snapshot 输入 | `/root/oauth-capture/profile-inputs/codex-0147-snapshot-223000Z.json`（16 endpoint） |
| 必需规则 | 42 条，`blocked=0` |
| 发现分类 | 约 2.76k 条，由 `runtime/classify_k26_discoveries.py` 确定性归类 |

k28 的候选采集是迄今最完整的一次，五个受控辅助场景全部产出预期动作：

| 场景 | 实测动作 |
|---|---|
| A09 | `alpha_search` 2、`images_edit` 1、`images_generation` 1、`legacy_compact` 4、`models_manifest` 1 |
| A11 | `realtime_first_hop` 1、`realtime_sideband` 1 |
| A12 | `wham_usage` 2、`wham_credit_details` 2、`wham_safe_consume` 1 |
| A13 | `oauth_dummy_invalid_grant` 1 |
| A14 | `files_create` 1、`files_blob_put` 1、`files_uploaded` 1 |

A13 出现 `oauth_dummy_invalid_grant` 证明 OAuth refresh 请求真正出站并被 relay 拦到，
§7.7 的修复成立。

### 10.3 本轮修复的缺陷

均已独立提交并通过 mutation 验证。

**A13：`req.Client` 链的 Guard 包装顺序（`31768d0a6`）**——既有缺陷，与本次升级无关，
详见 §7.7。生产上所有走 `req.Client` 且画像要求 lowercase wire 的官方出站端点都被自身
Guard 拒绝，OAuth token 刷新即在其中；此前没被发现，只因候选验收从未跑到 A13。

**A11：Linux 上的 Live attestation 接线**——详见 §7.8。`candidatecapture` 分支引用的
`candidateCaptureScopeFromContext` 根本不存在，该分支平时既不构建也不测试。

**环境恢复判据把容器重建判成污染（`e5717d678`）**——A11 注入 attestation 必须 compose
重建服务容器，而恢复检查对 containers 与 configuration 用 `byte_equal`：重建换实例 ID，
容器状态的 `id` 字段与 Docker 写入 `/etc/hosts` 的 `<容器 IP> <实例 ID 前 12 位>` 自引用行
都随之改写，必然让 Campaign 被判 `environment_contaminated`——**k27 正是这样整个作废的**。
修复为收窄判据而非放宽比较：容器状态不再记录实例 ID，hosts 摘要剔除自引用行；人为劫持
行仍会改变摘要。database 一项本就无碍，探针早已用 `before_subset` 允许 watermark 表增长。

**A12 期望计数写错（`7178edbf9`）**——期望每项 1 次，但 `ResetQuota` 消费额度后必然再调
一次 `QueryUsage` 刷新显示缓存（`openai_oauth_handler.go` 的 Step 2，用 `WithoutCancel` +
独立超时，不受入口 context 取消影响），两次入口调用共产生两轮 usage/credit_details。这是
Sub2API 自身行为、与 Codex 画像版本无关；A12 从未跑通，错误期望一直没暴露。已改为 2/2/1。

**images-wire 不自备图片模型映射（`7178edbf9`）**——`run_candidate_aux_capture.sh` 会临时把
图片模型写进账号的显式 `model_mapping` 白名单并由 EXIT 钩子恢复，images-wire 硬编码
`gpt-image-1` 却没有这一步，生图请求在入口就被判 `model_not_found`（404）、根本不出站，
h1 探针永远等不到数据。已补上与 aux 同构的准备与恢复：原值冻结成 hex 并区分
present/missing，**写入早于服务重启**（否则映射不进进程），EXIT 钩子逐字回写、失败关闭。

**候选镜像的可复现构建（`e78a7556a`、`ee7170c32`）**——此前候选镜像只能靠一份未入库的临时
Dockerfile 构建。现由标准 Dockerfile 的 `BUILD_TAGS`（默认 `embed`）与 `FRONTEND_SOURCE`
（默认 `build`）覆盖，两个默认值都有断言守护。

### 10.4 仍未修正的脚手架问题

**`ADMIN_BEARER_TOKEN` 不在 scenarios 的 environment 声明中**。编排器用
`os.environ.copy()` 继承调用进程环境，因此该变量必须由调用方显式带入，否则
`candidate-frozen-aux` 会在第 60 行直接退出。修它需要改 `variable_contract`，影响面大于
收益，暂按“调用时显式带入”处理。

另需注意：`sub2api-admin-token` 是 24 小时有效的 JWT，容器内 `ADMIN_PASSWORD` 为空、
走不了登录接口；用仓库自带的 `backend/cmd/jwtgen` 重签即可（按第一个 active admin 签发）。

### 10.5 运维要点

- **重跑必须全量归档**。`resume --rerun-failed` 始终重跑整个 attempt 的全部任务、不跨
  attempt 复用证据，因此必须把 `runs/<campaign>-*` 与
  `runs/official-client/oauth/oauth-<campaign>` **全部**移走。候选阶段的 resume 还须重申
  `--candidate-id` 与全套身份参数。
- **`official-relay-comp-hash-changed` 属概率性失败**，判据是 `exact_match_count=0`，靠重试。
- **候选采集跑在 previous 模式**。stage-profile 把 0.147 放在发布图的 `previous` 位、
  0.145 留在 `active`（`production_selector_changed: false`）；§9 验收通过后才对调指针。
- **Vircs 没有 Go**。`prepare-profile` 与 `stage-profile` 都要
  `go run ./cmd/egresscatalogstage`，交叉编译后用 shim 转发（`/root/oauth-capture/go-shim-k27/go`）。
- **证据权限**：official seal 要求目录 `0700`、文件 `0600`，归档或重跑后需复位。
- **capture-manifest 每轮自建**：由 `runtime/gen_official_k<N>_manifest.py` 生成（改路径即可）。
- **macOS 传输**：`tar` 会带出 AppleDouble 伴随文件，传完需 `find -name "._*" -delete`。

### 10.6 剩余路径

1. k29 官方采集 → seal → classify → `prepare-profile` → `stage-profile`；
2. 构建并推送候选镜像（`BUILD_TAGS=embed,candidatecapture`、`FRONTEND_SOURCE=prebuilt`），
   以 `repository@sha256:<manifest-digest>` 形式登记，部署为 `PROFILES_MODE=previous`；
3. `capture-candidate run` 覆盖 8 个必需任务；
4. `capture-candidate seal` 第一次调用返回 `client_checkpoint_created` 与不可变边界；
5. **需用户配合**：用 ZLF Code（Kilo 内核）各发一次请求——OpenAI Compatible →
   `/v1/chat/completions`，OpenAI Responses → `/v1/responses`，模型 `gpt-5.4`，API Key `id=15`；
6. 生成 observed-profile 与两份 Kilo 收据后再次 `seal` → compare → accept/ready；
7. §9 生产启用：canary、切换、回滚演练、恢复后 final-wire，最终 Active=0.147 / Previous=0.145；
8. 收尾：恢复分组 9 的 `allow_image_generation`（原值 false）。账号 90 的 `model_mapping`
   已于 k29 开始前复原，图片模型改由 images-wire 脚本自行临时写入并恢复。

用户已授权按第 5→6→7→8→9 章连续执行；每个变更集完成并自复核后自动进入下一项。
