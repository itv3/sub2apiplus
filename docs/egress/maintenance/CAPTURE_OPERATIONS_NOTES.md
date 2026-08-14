# Codex 官方出站采集操作要点

> 从升级计划 §10 抽出。这些是采集与封存时反复踩过的坑，属于操作手册，
> 不随单个 Campaign 变化。升级计划正文只引用本文，不再内联。

## 候选验收链路的正确顺序（此前一直做反）

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
时可行，见 §10.8.11。

## 候选源码树在 run 与 seal 之间不可触碰

`_directory_tree_digest` 扫描整个候选源码树，**不排除 `__pycache__`**，而 `.pyc` 内嵌源
文件的 mtime。因此在 `run` 之后向该目录写入任何文件——哪怕随后逐字还原——都会让摘要
永久漂移，`seal` 以"候选源码树在 run／seal 之间发生漂移"拒绝，只能重跑候选采集。

**收据生成器必须放在候选源码树之外运行**（例如 `runtime/build_kilo_receipts.py`），
产物写进 attempt 的 `evidence/receipts/`。

## 收据是两层结构

`build_*` 系列产出的是 **finalizer 的输入**，不是可直接提交给 seal 的收据：

| 环节 | 输入产出方 | 正式收据产出方 |
|---|---|---|
| 运行画像 | `build_observed_profile_runtime_audit.py` | `receipt_finalizer observed-profile` |
| Kilo 双协议 | `build_kilo_client_receipts.py` | `receipt_finalizer kilo-binding` |

直接把输入交给 seal 会得到"收据 schema_version 不受重放器支持"。另外 kilo-binding 要的
`runtime_audit` 与 observed-profile 那份**不是同一种**：前者描述单次调用的出站形态
（transport／upstream_endpoint／auth_mode／affected_branches），后者描述服务整体解析到
哪个画像。五份 Kilo 事实还要求时间非递减：安装 ≤ 入站 ≤ 出站 ≤ 响应 ≤ 用量。

## 采集期间的环境要求

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

## 本轮修复的缺陷

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
| Kilo 两条入口 UA 形态不同 | `88937e40b` | 详见本文档「采集期间的环境要求」 |
| 四类收据无产出方 | `65983ce15`、`0930a05aa` | capture-manifest、逐规则验收结果、Kilo 收据 |
| kilo-binding 缺单次调用 runtime_audit | `58cd4b670` | 与 observed-profile 的整体审计不同种，见本文档「收据是两层结构」；内容承接服务端记录，工具不推断 |
| 编排器自造断言命令 | `5440acf96` | 与 accept 用 `build_assertion_command` 复算的期望命令永不相等，results 无法通过 accept |
| 编排器与 accept 契约权威不同源 | `5440acf96` | 编排器取仓库冻结画像、accept 取批准画像，规则集增删时两端各说各话 |

## 候选画像下的 Go 验证：在本地组装等价副本

DMIT 编译不动 backend（`ent` 单包峰值 1.93 GB > 该机物理内存），要在候选画像
（`previous` = 目标版本）下验证 Go 行为，按下面四步在本地组装等价副本：

1. 复制本地 `backend/` 与 `docs/`——冻结线测试依赖 docs 的相对路径，只传 backend 会失败；
2. 从候选源码树取 4 个 stage 产物覆盖过去：`catalogdata/runtime/` 下的目标画像、
   `release-catalog.json`、`release-graphs/`、`snapshot-catalogs/`。**后两者是替换不是并存**，
   本地旧文件必须先删，否则 catalog 里会同时存在两份 graph；
3. 把工作区文件对齐到候选树的**实际**状态——候选树未必等于 HEAD，也未必等于工作区，
   只验一两个文件就外推会错（2026-08-12 即因此误判 k57 树为纯 HEAD 快照）；
4. 逐文件 sha256 比对全部 `.go` 确认零差异后再跑。**比对时按文件名 join，不要 diff 排序后的
   文本**——macOS 与 Linux 的 sort locale 不同，会产生上百条假差异淹没真差异。

DMIT 上保留了 `/root/oauth-capture/gocache-k57/mod`（872M module cache）备用。

候选树上 `go test ./internal/service/` 必然整包失败（`TestChangeset5CurrentFinalWireMatchesFrozenWireFields`
冻结的是 `previous=0.145` 的 final wire），属结构性现象；正式轮取 go test 日志必须用 `-run`
精确过滤到冻结映射内的测试。

## 候选 seal 链路：组件清单与踩过的坑

候选 seal 在本次升级之前**从未有人走通**（`build_capture_manifest.py` 的注释直说
「候选侧根本没有产出方，候选 seal 因此必然卡住」）。k56 作为试验田把这条链路逐段摸开，
本节记录它的真实构成——下一轮正式跑时按此清单准备，不必再逐个撞。

### 链路全貌

```
候选 run（9 job 证据）
  ├─ 证据标签声明 ──→ 编目 ──→ 收口 bundle(+provenance) ──→ capture manifest
  ├─ go test -json ──→ candidate_test_trace ──→ 结构化 trace ──→ 合并 manifest
  ├─ observed-profile 收据（服务自报候选身份）
  └─ Kilo 两入口绑定收据（真实第三方客户端）
                          ↓
              capture-candidate seal（断言门禁：kind 覆盖 + selector 可达）
```

四路输入缺一不可，且**彼此的路径基准不同**——这是最容易踩空的地方：

| 输入 | 路径基准 | 说明 |
|---|---|---|
| capture manifest／bundle | attempt 的 `evidence/` 根内 | 必须是已绑定证据根内名为 `assertion-bundle` 的子目录 |
| go test 日志 | **bundle 内相对路径** | 且必须在 base manifest 中以 `stdout_log/opaque_bound_source` 恰好声明一次，`scenario_ids` 覆盖全部冻结事实场景 |
| 冻结映射／画像 | **候选源码树内** | 默认值就是 `source_root/tools/official_client_capture/…`；这意味着 **0.147 映射必须在候选采集之前就存在于源码树里** |
| Kilo 绑定收据 | attempt 证据根内 | 字段须与本轮 campaign／attempt／run_nonce／身份七元组逐字一致 |

### 内部状态记录只能来自 go test，不能来自抓包

`transport_fallback`、`connection_lifecycle`、`turn_state_chain` 等 14 类
**internal record type** 在抓包面根本不存在。官方侧的派生器明令
「禁止一切内部 record type——官方侧永远不伪造 Sub2API 内部状态」，它们只能由
`candidate_test_trace.py` 从候选源码快照的 `go test -json` 日志、按冻结事实映射投影而来，
每条 observation 同时绑定测试日志与同场景原始 relay 字节。

k56 实测：11 个 `TestCandidateTrace*` 全通过，产出 31 条事实、覆盖 A03～A15 十一个场景，
`status: pass`。**这也解释了候选侧为何比官方侧严格**：官方侧的可达性检查会跳过非
`dual_wire` 规则，候选侧要验全部 42 条——`SPEC-PROTO-002` 这类只在候选侧暴露。

0.147 版映射由 0.145 基线派生：事实语义（fact_id／scenario_id／record_type／trace_kind／
data_keys）逐字不动，只改 `codex_version` 并按当前候选源码树重算 16 个文件哈希——其中
13 个源码文件里 **10 个哈希未变、3 个变了**（`official_egress_codex_integration.go`、
`openai_gateway_forward.go`、`openai_live.go`）。派生结果与候选构建树逐字核对通过。

### labels 语义错位：selector 全部选不中（已解除）

候选侧 36 条标签规则最初写完后，编目、kind 覆盖全过，但 **35 条 selector 不可达**。根因是
labels 的语义写错了：给所有 relay 规则统一标了 `ca_mode: custom`，而判据要验的恰恰是
**客户端发出的原始字节**，selector 写的是 `labels.ca_mode absent`——mitm 是代理重构的
（protocol 报 HTTP/2.0、host 落在 `:authority`），所以带 `ca_mode` 的观测必须被排除。

对照官方侧 44 条规则：**29 条根本不带 `ca_mode`**，只有 pcap（direct／mitm 的 TLS 面）
才标；relay 面带的是 `mode`／`track`／`provider`／`endpoint_class`／`variant` 这些判据
真正要筛的维度。

> 这个错误不会报错，只会表现为「选不中」。**更危险的反面是选中了错误的样本让判据虚假
> 通过**——所以 authority 才规定 labels 必须能由采集参数与场景 precondition 推出，
> 不得由被该标签选中的判据反推。重写时必须逐条对着 selector 核对，不能凑。

**重写结果（本轮）**：9 job 共 54 条规则，按各 job 驱动脚本的**轮次序列**逐连接拆分标注，
每条 rationale 写明该标签由哪个采集参数或场景 precondition 推出。不可达 **34 → 0**。
关键改动：

- relay 与 probe 面**不再标 `ca_mode`**，只有 pcap（TLS 面）与 mitm／ingress（重构面）标；
- 按驱动轮次拆 `variant`／`track`／`mode`／`residency`／`compaction_mode`／`compression`：
  例如 A04 的四个 POST 依次是 baseline（`residency=unset`）、residency-us（`residency=us`）、
  memgen、parent-thread；A09 的四轮 compact 依次是 prime、default、beta、turn_state；
- A03 的 `variant=http_default` 标在**首轮 Lite POST**（conn004）而非非 Lite 轮——该场景
  precondition 是 `use_responses_lite=true`，判据期望的正是 Lite 头序（含
  `x-openai-internal-codex-responses-lite`）。

三条测试把这套语义钉死在声明上（`test_build_evidence_catalog.py`）：
`ca_mode` 只允许出现在 pcap 与 `mitm/`／`ingress/` 路径；候选侧 relay／probe 的非 pcap 面
必须不带 `ca_mode`；`frame_labels` 只能挂在能产出 `websocket_frame` 观测的规则上。

### 本轮新增的工具修复

| 位置 | 修复 | 不修的后果 |
|---|---|---|
| `prepare_assertion_bundle.sh` | 候选根名多一段 `candidate_id`，按后缀归一化匹配 | 候选侧一个证据根都匹配不上，报「没有可编目的证据根」 |
| 同上 | mitm 系列 job 的证据根定义本身是通配模式（`…-mitm-core-*-run`），按 fnmatch 展开 | 这两个 job 整个从编目里消失，且不报错 |
| 同上 | 显式跳过 `*-setup-run` 根 | 该根只有代理自身运行日志、无 wire 证据：登记为 opaque 会被「无派生引用」拒绝，不登记又报「该根没有适用规则」 |
| `kilo-byte-capture-r12.sh` | 清理段补 `delete from usage_logs …`（见 §10.8.1） | Kilo 绑定收据永远拿不到 `usage_id` |

这三处都在 `tools/` 下、不参与工具身份；`candidate_test_fact_map_0_147_0.json` 与候选侧
标签声明在受管目录内，改动使工具身份变更、k56 随之作废。

### 打通 seal 门禁本体的四处修复

把候选 seal 的六道断言门禁跑通，除标签重写外还需要四处受管工具修复。四处都在 `tools/`
的受管目录内，因此工具身份随之变更（这正是 k56 不能再做 stage 级操作的原因）。

**① `frame_labels` 支持派生路径**（`candidate_rule_assertion.py`／`build_evidence_catalog.py`
／`candidate_capture_manifest.schema.json`）

帧级标签原先只支持 `h1_request_stream`——即直接解析 relay 原始字节。官方侧可以这么做，
候选侧不行：候选 relay 一律先以 `opaque_bound_source` 收口、再由 ACC-02b 派生器投影，
帧观测落在派生 jsonl 里。不修则 A06 的 `warmup` 帧标签无处可挂，`SPEC-WS-005`
的 `warmup-generate-false` 在候选侧永远选不中。

修复把 `observation_jsonl` 也纳入合法 parser，派生 artifact 承接声明里的 `frame_labels`，
并按派生记录 `data.frame_index` 叠加；帧索引不存在仍然失败关闭（与原路径同语义）。

**② 负向存在性判据豁免可达性预检**（`assertion_gate.py`）

`SPEC-EP-021` 的 `v2-no-legacy-call` 断言是 `count_equal: 0`——「默认 V2 批次**不得**出现
`/responses/compact` 请求」。它的通过形态恰恰是 selector 选不中任何观测，而可达性预检
要求「至少命中一条」，两者直接互斥。修复对 `count_equal: 0` 跳过可达性，判据本体仍由
accept 的离线重放评估。

**③ 验收契约支持侧别限定 check**（`acceptance_contract.py` 及三处消费者）

`SPEC-WS-002` 的 `optional-missing-covered` 要一份「相对默认握手缺少某个可选头」的 WS
扰动样本。官方侧由 CLI 启动参数造出（job `official-relay-ws-optional-missing` 用
`--disable remote_compaction_v2` 让 `x-codex-beta-features` 整条消失）；**候选侧结构性
造不出**：

- `responses_ws` 端点该头的槽位 condition 是 `remote_compaction_v2`，与入站头无关；
- `officialegress/compiler.go` 对该 condition 只返回 `features.RemoteCompactionV2`；
- 该值来自版本画像常量 `FeatureDefaults.RemoteCompactionV2`，0.145／0.147 均为 `true`，
  没有任何请求级、账号级或分组级开关可以翻转。

其余可选头（subagent／memgen／parent-thread／runtime-metrics／residency／fedramp）在默认
握手里本就缺席，构不成「相对默认少一个」的扰动。因此这是官方侧的采集覆盖要求。

修复在契约里新增 `side_restricted_checks` 载荷字段与 `SIDE_RESTRICTED_CHECKS` 登记表，
把该 check 登记为官方侧专属，并让 seal 门禁、`build_rule_assertion_results`、accept 三处
按侧复算 check 全集（断言器新增 `--side` 参数，命令形态随之变化，accept 复算同步）。
登记门槛写在表注释里：**必须能指出该侧没有任何产出路径的机器可核依据，采集遗漏必须
重采，只有结构性不可达才登记**。规则还在而 check 改名／被删时失败关闭，防止失效豁免
静默留存。契约摘要因此漂移
`1689ab30…` → `bd2ccc52…`；逐项复核 25／17 分组、`validation_modes`、`expected_check_ids`
与 `side_coverage` 全部逐字未变。同规则的 `remaining-lowercase` 与
`default-swap-remove-order` 仍在候选侧逐条执行，WS 线序的候选侧验收未因此变弱。

**④ 门禁承认 `candidate-trace/` 并强制重放其收据**（`assertion_gate.py`）

`candidate_test_trace.py` 的产物落在 bundle 的 `candidate-trace/` 下，它与 `derived/` 同类
——都是 bundle 内的派生产物，不由 ACC-02 收口 provenance 覆盖。原先该前缀不在
`allowed_extra_prefixes` 里，`verify_bundle` 直接以「bundle 内存在未登记文件」拒绝，
候选 seal 走不到后面任何一道检查。

只放行前缀等于在 bundle 里开一个不受约束的目录，因此配套新增 `_verify_candidate_trace`：
收据存在且 schema 正确、状态为 `pass`、声明的每份 trace 产物摘要逐字一致、这些产物都已
按同一摘要登记进 capture manifest、且目录内没有收据未声明的文件。gate 收据新增
`candidate_trace_receipt_sha256` 字段封存该收据摘要。七条测试覆盖正例与五类负例。

> 该门禁上线当场就抓到一个真实污染：排障期写进 `candidate-trace/` 的一份临时 manifest
> 副本被判为「收据未声明的文件」。这正是它要防的事——派生目录里任何未登记字节都可能
> 是夹带进来的证据。

**跑通结果**（k56 候选侧，0.147 批准画像）：

```
bundle provenance 重放 81 项，派生收据重放 62 项
capture manifest 154 artifact / 377 观测
分侧 kind 覆盖、wire 观测互斥、selector 可达全过
checked_rule_count=42  checked_check_count=108
```

## `SCN-REALITY-01` 收据最低字段

| 场景 | 最低字段 |
|---|---|
| A11 | `call_create_status`、`call_id_sha256`、`sdp_or_started_event`、`async_error_count`、`sideband_sni`、`final_state` |
| A13 | `trigger`、`token_request_method`、`token_request_path`、`oauth_sni`、`jwt_exp_observation`（自然窗口路径必填）、`capture_side_wrote_auth`、`rotated_by_refresh`、`final_state` |
| A14 | `tool_name`、`tool_call_id`、`create_request`、`upload_url_source_event`、`put_destination`、`upload_sequence`、`uploaded_event`、`regional_sni`、`final_state` |

每份收据还必须记录 `track`、`model_id`、`models_response_sha256`、`use_responses_lite`、
`model_fallback` 和 evidence root。字段值必须来自官方 CLI、relay、pcap 或驱动的原始事件；
编排器不得根据 job 退出码自行补写目标字段。缺字段、状态矛盾、异步 error、模型条件不符
或来源不一致均为失败关闭。

## 候选机环境前置清单

候选采集对环境的要求分散在多处门禁里，任一项缺失都不会在启动时报错，而是在采集中途以
404／503／1013／「无可编目证据根」等形态暴露。**开跑前逐项核对**：

| 项 | 要求 | 缺失时的症状 |
|---|---|---|
| 采集容器挂载 | 与 Vircs 逐项一致（13 项），含 `/root/oauth-capture` 与 `/capture` 双挂载 | tcpdump 无处落盘、日志为空 |
| 采集容器 PID 命名空间 | `--pid=host` | `nsenter: cannot open /proc/<pid>/ns/net` |
| 采集容器镜像 | 必须有与 campaign 冻结值逐字相同的 RepoDigests。**docker 29.x（containerd 镜像存储）下 `save`／`load` 会保留 digest，2026-08-14 实测 k72 镜像 load 后 RepoDigests 完好、`_image_repo_digests` 校验通过**；旧版本会丢失，那时需重新导入并保留 tag | `--runtime-image 不是运行镜像实际 RepoDigests 中的不可变引用` |
| 宿主目录 | `scripts/`、`addons/`、`tools/` 三个顶层目录 | `/capture/scripts/start_ingress.sh: no such file` |
| Codex CLI | 候选驱动客户端就是官方 codex 0.147.0，须挂载到 `/usr/local/bin/codex-capture` 等四处 | `FileNotFoundError: /usr/local/bin/codex-capture` |
| 候选镜像构建标签 | `BUILD_TAGS="embed candidatecapture"`——Linux 生不出 DeviceCheck，A11 靠受四元组约束的合成 provider | A11 第一跳被上游拒 |
| campaign 冻结项 | `live_attestation_compose_dir`／`_files` 非空，否则 A11 的 Live attestation 不注入 | A11 断言失败 |
| 账号模型 | 账号的 `model_mapping` 必须含主线与 Lite 轨模型 | `Model "x" is not supported by any configured account in this group` |
| 分组开关 | `groups.allow_live = true` | `API Key 分组未启用 Live，无法执行 A11` |
| 账号 WS 开关 | `extra.openai_oauth_responses_websockets_v2_enabled = true`／`_mode = ctx_pool` | 网关 WS 入口 `no available accounts` → WS 1013 |
| 管理凭据 | `ADMIN_BEARER_TOKEN[_FILE]`，用 `backend/cmd/jwtgen` 自签 | `必须通过 ADMIN_BEARER_TOKEN 或只读 token 文件提供管理凭据` |
| 候选身份注入 | 七个 `GATEWAY_EGRESS_ACTIVATION_*` ＋ `_FACT_PATH`（**完整事实只落盘，日志仅打印五个字段**） | observed-profile 收据报「运行时事实的 x 与候选身份不一致」 |
| 账号熔断 | 开跑前 `temp_unschedulable_until` 须为空 | 入口 503 |
| 账号配额 | 开跑前 `extra.codex_7d_used_percent` 须远低于 100；一轮 9 job 含重试足以打满一个 free 账号的 7 天窗口 | 入口 503，日志 `openai_429_7d_limit_exhausted`；`ops_error_logs` 记 `up=429`、`excluded_account_count=1` |
| 账号 Compact 能力 | `extra.openai_compact_supported = true` | compact 类场景选路失败 |
| 账号 extra 豁免完整性 | 服务端自动刷新的配额缓存键必须全部在探针的 `ACCOUNT_MUTABLE_EXTRA_KEY_PATTERNS` 内（含 `codex_reset_credit_snapshot`），**Python 常量与 SQL 条件两处同步** | 账号一被调用就写该键 → `extra_digest` 漂移 → `environment_contaminated`，且只改一处会「自查通过、探针判漂移」 |
| 采集端口 | **18080（mitm）与 18081（ingress）都须空闲**，且两个 pid_file 都不存在 | 只查一个时，另一轨会在采集中途才报「已有 MITM／ingress 进程运行」 |
| 同名 run 目录 | 同一 campaign 重跑前须归档 `runs/<campaign-id>-*`（含 `-setup-run` 等派生目录） | `输出目录已存在，为避免混入旧样本而拒绝启动` |
| `run_root` 标记 | `runs/official-client/.official-client-capture-root`，内容 `official-client-capture-root/v1`、权限 600 | 目录非空却无标记时 `capture.py` 直接拒绝启动，28 个 job 全败 |
| 候选树属主权限 | 全树 `root:root`，目录与 `.sh` 为 `700`、其余文件 `600` | 从 macOS 传文件会留下 `501:staff`／`644`，驱动脚本 `Permission denied` 废掉整个 attempt |

> 属主这一项每次从本机同步修复文件都要复核，**逐文件传也会中招**：k57 树在
> 2026-08-12 复核时有 39 个文件与 12 个目录是 `501:staff`／`755`，其中包括
> `run_candidate_core_capture.sh` 等驱动脚本，以及 `docs`／`tools` 两个顶层目录。
> 基准取自已跑通的 k56 树（`drwx------ root:root`）。用
> `find . ! -user root \( -type f -o -type d \) -exec chown root:root {} +` 收口，
> 再按类型补 `chmod`；`__pycache__` 不参与工具身份，可以不管。

## 改采集脚本的默认值对 Campaign 内的 job 无效

`codex_upgrade.py::_run_job_step` 的环境是
`environment = os.environ.copy()` 后 `environment.update(step["environment"])`——
**job 定义里的 `environment` 永远覆盖脚本默认值，也覆盖外部导出的同名变量**。

因此有两条推论，改任何采集参数前先确认落点：

- 改 `run_*.sh` 里 `${VAR:-默认值}` 的默认值，只对**手工直接执行**该脚本有效；
  受 Campaign 调度的 job 一律走 job 定义里的值。k61 实证：脚本默认场景已改成
  `s1 s2 s3 s4`，但三个 WS 相关 job 的 `SCENARIOS` 仍写死 `s1 s2 s4`，
  三轮采集从未跑过 s3（§10.8.8）。
- 同理，`ACCOUNT_ID` 之类也**不能靠外部环境变量覆盖**——采集账号只能来自
  `campaign.json` 的 `codex_account_id`，换账号的正确做法见计划文档 §10.8.7。

判断某个参数的实际生效值，直接读 attempt 里的 `results[].steps[].argv` 与 job 定义的
`environment`，不要看脚本源码的默认值。

## 官方证据同步到候选机：三处易漏

§10.0 要求候选机持有官方原始证据的完整副本，`_verify_stage_evidence` 按官方 `result.json`
里的**绝对路径**重算清单摘要，缺一个文件整条证据链在候选侧就无法自证。三处踩过的坑：

1. **两台采集机之间不能直连**。`ssh … DMIT` 只在本机 `~/.ssh/config` 里有别名，Vircs 上
   `Could not resolve hostname`。k59 的同步脚本正是这么写的，**当时就静默失败了**——
   候选侧因此少了 168 个文件，直到 seal 阶段才以「原始证据摘要在封存后发生变化」暴露。
   正确做法是走本机中转：`ssh Vircs 'tar -cf - …' > 本地包 && ssh DMIT 'tar -xf -' < 本地包`。
2. **`official-client` 下的两个 oauth 根容易漏**。证据根不止 `runs/<campaign-id>-official-*`，
   还有 `runs/official-client/oauth/oauth-<campaign-id>` 与 `-ws-repeat`。打包时按
   `result.json` 的 `evidence_roots` 逐条核对，不要凭前缀猜。
3. **目录自身的隐藏文件要一起带**。`cd <dir> && tar -cf - <子目录>` 不含 `<dir>` 本身的
   点文件，`runs/official-client/.official-client-capture-root` 会丢；重建目录后不补标记，
   `capture.py` 会以「已有 --run-root 缺少本工具专用标记」拒绝启动。

同步完成后在候选机上跑一次 `status`：它会重放 official stage 并重算证据清单，
通过即证明副本完整、`producer.tool.path` 两侧一致。

