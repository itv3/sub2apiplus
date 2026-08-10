# `SCN-REALITY-01`：场景真实性门禁 — R0 方案与 R1 实施

> 状态：**R0 已完成，R1 已实施**（§12 记录实施结果）。本文原为升级计划 §11.2 中 R0
> 的交付物，冻结契约与实现边界；R1 按 §6 的九项范围完成编码，尚未重采、未创建 k37、
> 未改变 Active/Previous。下一步是 R2（A11 触发修正）。
> 日期：2026-08-10
> 上游结论：升级计划 §10.9.4、[`SPEC_EP_002_EVIDENCE_BLOCKER.md`](SPEC_EP_002_EVIDENCE_BLOCKER.md) §5.1
> 适用 Campaign：k37（尚未创建）；k36 仅作诊断夹具，不适用本门禁
> 权威顺序：本文低于 [`CODEX_CLI_0145_EGRESS_SPEC.md`](../../CODEX_CLI_0145_EGRESS_SPEC.md) 第四部分与升级计划，冲突时以后者为准

## 1. 目标与定性

**目标**：让「job 完成」不再等同于「场景成立」。当前编排器只要子进程退出 0、证据目录里
有非空文件就判 `complete`，A11／A13／A14 因此在目标协议分支一次都没走到的情况下仍被记为
`19/19` 完成。本门禁要求每个目标场景额外提交一份由**原始证据派生**的机器收据，收据缺失、
结构不合法或身份不匹配一律失败关闭。

**定性**：这是验收链路的**第三层**修复，与前两层同源但不同层：

| 层 | 问题 | 状态 |
|---|---|---|
| 一 | 验收链路接线错误（ACC-01～07） | 已闭环，见 [`ACCEPT_PIPELINE_DEFECTS.md`](ACCEPT_PIPELINE_DEFECTS.md) |
| 二 | 场景 `required_artifact_kinds` 与实际产出的证据类型错配 | §10.9.3 已定案实施 |
| 三 | **job 脚本退出成功 ≠ 目标协议分支已触发** | 本方案 |

**本方案不解决触发问题本身**。A11 用错 quicksilver 版本、A13 改错过期权威、A14 模型未调用
工具，分别由 R2／R3／R4 修正；本门禁只保证「没触发」不再被记成「完成」，并为 R2～R4 提供
可执行的成功判据。

## 2. 已核实的现状事实

以下均为本轮只读复核所得，坐标可直接跳转。

### 2.1 完成判定只有两层

`run_job`（[`codex_upgrade.py:1392-1486`](../../../tools/official_client_capture/codex_upgrade.py)）
的状态判定在 `:1463-1470`：

```python
    steps_ok = len(step_results) == len(job.steps) and all(
        item["return_code"] == 0 for item in step_results
    )
    status = (
        "complete"
        if steps_ok and not missing_patterns and not empty_patterns
        else "failed"
    )
```

`missing_patterns`／`empty_patterns` 由 `:1440-1459` 计算，只判断 `evidence_roots` 的 glob
是否命中**存在且至少有一个非空文件**的路径，不读取内容、不校验 artifact kind、不关心场景。
三个 job 的 `evidence_roots` 都指向整个 run 目录，而 `tls/relay.crt`、`relay/` 必然存在，
因此**目标连接一跳未发生也判 `complete`**。

`required_artifact_kinds` 属场景不属 job，run 阶段完全不消费；它只在 seal 的断言门禁里
按 capture-manifest 的**声明**校验（`build_assertion_bundle.py:317-346`），而非执行事实。

### 2.2 驱动侧：目标分支失败被两层吞掉

**A11** 有两层独立吞噬：

1. `drive_codex_realtime.py:225` 把真正要测的那一跳的返回值整个丢弃——
   `srv.call("thread/realtime/start", params, timeout=60)` 未赋值、未判空，而同文件 `:192-207`
   的 `initialize` 与 `thread/start` 都有 `if ... return 2` 检查；`call()` 在 RPC 返回 error
   或超时时返回 `None`（`:137-145`），这里收不到。
2. `:231` 无条件 `return 0`；`thread/realtime/started`、`thread/realtime/error` 在整个
   `tools/` 下**无任何等待逻辑**，通知只被 append 进 list 并打印方法名（`:112-115`），无消费者。
3. 外层 `run_official_relay_scenario.sh:522` 再加 `| tail -10 || true`。

**A13／A14** 走通用 exec 分支 `run_official_relay_scenario.sh:709`，同样是
`$disable_args $extra_args "$prompt" 2>&1 | tail -3 || true`，退出码丢弃。

脚本内唯一的硬失败门是 `:717-720` 的 `relay.json` 非空检查。

### 2.3 pcap 证据全程无校验，且覆盖面低于候选侧

官方侧抓包（`run_official_relay_scenario.sh:262-264`）：

```bash
    "tcpdump -i lo -s 0 -U -w /capture/runs/$run_id/direct/traffic.pcap 'tcp port $relay_port' \
     > /capture/runs/$run_id/direct/tcpdump.log 2>&1"
```

- **`-i lo` 只能看到被 hosts 劫持到 127.0.0.1 的域名**。响应返回的区域上传主机不在
  `RELAY_HOSTS` 里就走真实网卡，这份 pcap 里根本不存在；
- `docker exec -d` 后无条件置 `tcpdump_started=1`，容器缺 `tcpdump`、缺 `CAP_NET_RAW`、
  写盘失败都会让它返回 0 而脚本继续；
- `direct/traffic.pcap` **没有任何后置校验**：无非空检查、无 ClientHello 计数，
  `pcap_clienthello.py` 从未被该脚本调用。三个 job 的 pcap 证据是「写了就算」。

同仓库**候选侧已有完整范本**，官方侧是明确倒退：

| 项 | 候选侧 `run_candidate_aux_capture.sh` | 官方侧 |
|---|---|---|
| tcpdump 预检 | `:667-668` 缺失即 `exit 1`，注释写明「无法形成 A11/A13/A14 的 SNI pcap」 | 无 |
| 抓包接口 | `:769` `-i any`（同时覆盖 lo 与 eth0） | `-i lo` |
| pcap 非空 | `:317-318` 大小 ≤ 24 字节（只有全局头）即 `return 1` | 无 |
| pcap 可解析 | `:322-325` `tcpdump -nn -r ... -c 1` 失败即 `return 1` | 无 |
| 排空窗口 | `:285-289` 停止前 `sleep 1`，避免空 pcap | 有（`:115-131`） |

规格侧也早有先例：`oauth-ep002-refresh`／`oauth-ep002-allhosts` 都是**无 host 过滤 pcap**
（`docs/EVIDENCE_INDEX.md:118-119`），`SPEC-TLS-003` 的运行证据明确记录
「BPF 均为 `tcp port 443`，未按 host 预过滤」（`docs/Claude_code_21220_EGRESS_SPEC.md:509-514`）。

### 2.4 收据体系没有 per-scenario 层

现有收据只有三类，全部是 attempt／环境／客户端级：`restoration`、`observed-profile`、
`kilo-binding`（`codex_upgrade_receipt_finalizer.py:1908-1963`）。run 阶段明确拒收所有 seal
收据参数（`codex_upgrade.py:5291-5311`），只生成环境恢复收据一份。**没有任何机制验证某个
job 真的触发了它声称的场景。**

### 2.5 A13 的凭据还原不保证

`run_official_relay_scenario.sh:160-165` 的还原是
`test -f && cat && chmod && rm` 短路链，末尾 `>/dev/null 2>&1 || true`：备份文件不存在时
**整条链静默跳过**，`auth.json` 永久停留在 `last_refresh=2020-01-01`，无任何告警。
`trap cleanup EXIT`（`:170`）未挂 `INT TERM`，编排器超时走 `_terminate_process`
（`codex_upgrade.py:1425`）时还原不保证执行；备份写在容器 `/tmp`，容器重启即丢。

## 3. R0 期间新发现的两处阻断

以下两项不在 §10.9.4 的审核结论内，是本轮复核新发现的，**必须在 R1 编码前处置**，
否则 k37 重采后仍会在下游失败。

### 3.1 两份场景清单的 `required_artifact_kinds` 已分叉

§10.9.3 的定案只改了 `codex_upgrade_scenarios_0_145_0.json`，没有同步
`candidate_rule_expectations_0_145_0.json`。两份仓库清单确实已经分叉；但正式 Campaign 的
验收契约由 classify 阶段批准的 `assertion-profile.json` 推导（`codex_upgrade.py:7214-7234`），
仓库冻结画像主要作为模板、预检和冻结摘要来源：

| 场景 | `codex_upgrade_scenarios_0_145_0.json` | `candidate_rule_expectations_0_145_0.json` |
|---|---|---|
| A01 | `["pcap", "process_trace"]` | `["pcap", "relay_binary"]` |
| A15 | `["process_trace"]` | `["relay_binary", "process_trace"]` |

其余 13 个场景一致。分叉来自 `9023af97c`（§10.9 定案实施）。`candidate_rule_assertion.py:1282-1312`
会自动在每条规则首位插入 `scenario-artifact-coverage` check，用 `issubset` 硬判；如果批准
画像沿用旧 kinds，**A01 与 A15 仍会按旧契约校验**，§10.9.3 的定案就不会传递到 accept。

没有任何测试交叉校验这两个文件。

**处置决定**：并入 R1。对齐两份仓库清单，新增交叉一致性测试，并在 classify 阶段逐场景
校验批准的 `scenarios.json` 与 `assertion-profile.json` 的 kinds 一致；同步重算
`acceptance_contract.FROZEN_CONTRACT_SHA256`，重新审核 side coverage 与 25／17 分组。

### 3.2 三个场景的 `trigger` 定义描述的是候选侧受控形态

场景清单里 A11／A13／A14 的单一 `trigger` 字段同时被 official 与 candidate 使用，原文实际
描述的是候选侧受控形态：

| 场景 | candidate 现有触发 | official 必须改为 |
|---|---|---|
| A11 | 受控第一跳返回 `call_id`，再接受 sideband WS | 官方 CLI V3／`quicksilver=v2`，真实 call-create、异步 started/SDP 与 sideband |
| A13 | raw refresh-token 管理入口 + dummy token + 受控 auth 端点 | 隔离账号中 JWT `exp` 自然进入刷新窗口，由官方 CLI 真实发送 refresh |
| A14 | Sub2API 生产 service 受控上传 | 官方 CLI Apps 工具调用，真实 create→响应 URL→PUT→uploaded |

也就是说，§10.9.3 把这三个场景绑到 official job 时，只加了 job，**没有把场景定义改成官方侧
真实触发形态**。这与 §7.7「A13 属候选侧辅助采集」的记录一致，也解释了 k36 为何用了一个
场景定义里根本没写的触发手法（改 `last_refresh`）。

历史上这三条 SNI 在 0.145 基线时代都有真实证据（`docs/EVIDENCE_INDEX.md:118-121`，
SPEC-EP-002 抓包判定为「✅ 充分 P+R」），其中 realtime 那份正是「受控第二跳」——
本轮 §11.1 已排除该手法，因此**官方侧必须重新定义触发形态**，不能沿用历史证据的取得方式。

**处置决定**：并入 R1，但**不得覆盖候选侧定义**。场景清单改为分侧触发契约（例如
`official_trigger`／`candidate_trigger` 及对应前置条件），或保留中立场景目标并增加
official/candidate 分侧前置条件；两侧均须与 §4.3 收据字段一一对应。场景定义属受管工具身份，
改动后必须冻结新摘要并新建 k37。

## 4. 契约设计

### 4.1 定位：收据只表达成功态，缺失即失败

仓库既有铁律是「只接受成功态」——`status` 用 `const`、`passed` 用 `{"const": true}`、
`exit_code` 用 `{"const": 0}`，把失败写进收据等于伪造结论。本方案沿用：

- **场景成功时才产出收据**，schema 在结构上只允许成功态；
- **场景失败时不产出收据**，写入按 job/run/retry 唯一命名的不可变
  `scenario-failure.json` 诊断记录（不进收据体系、不参与判定，仅供排障）；
- job 判定看的是「声明的收据是否齐备且合法」，**不看收据里的某个布尔值**。

这样伪造一次成功需要伪造整条 producer 链（工具 SHA、命令摘要、输入绑定），与既有防伪造
模型同强度；而「把 `final_state` 改成 success」这种一字之差的伪造被结构性排除。

**产出方与校验方分离**（§11.3 要求「编排器不得根据 job 退出码自行补写目标字段」）：

| 环节 | 承担者 |
|---|---|
| 事实提取 | 新增 `build_scenario_facts.py`，从 relay 字节、pcap、驱动事件日志解析 |
| run 阶段判定 | `run_job` 只读校验：文件存在、schema 合法、身份匹配 |
| seal 阶段复算 | `receipt_finalizer scenario` 独立重放，与 run 阶段收据逐字比对，不一致拒绝 seal |

驱动必须落一份原始事件日志（A11 为 JSON-RPC 通知流），收据构建器只做解析，不得推断。

### 4.2 顶层字段表

`schema_version` 取 `codex-egress-scenario-receipt/v1`，schema 文件
`codex_upgrade_scenario_receipt.schema.json`，`$id` 为
`https://sub2apiplus.local/schemas/codex-egress-scenario-receipt-v1.json`。
顶层与所有嵌套对象 `additionalProperties: false`。

| 字段 | 类型／约束 | 说明 |
|---|---|---|
| `schema_version` | `const "codex-egress-scenario-receipt/v1"` | 按既有约定用 const 钉死 |
| `scenario_id` | `^A[0-9]{2}$` | 目标场景 |
| `job_id` | `safeId` | 必须等于产出该收据的 job |
| `campaign_id` / `attempt_id` / `run_nonce` | 既有 `$defs` | attempt 身份三元，防跨轮复用 |
| `run_id` | 非空 | 采集脚本的 `RUN_ID`，绑定证据根 |
| `status` | `const "success"` | 唯一合法值 |
| `final_state` | `const`（逐场景取值见 §4.3） | 协议层终态，非 job 成败标志 |
| `observed_at_utc` | RFC 3339 | 事实观测时刻 |
| `evidence_bindings` | array，minItems 1，元素 `{root_role, path, sha256, bytes}` | 字段值所依据的原始证据；`path` 相对已批准 root，`root_role` 绑定 root 身份 |
| `facts` | object，逐场景必填字段见 §4.3 | 提取出的协议事实 |
| `producer` | 既有 `producer` `$defs` | 工具路径／SHA、子命令、规范化参数、`command_sha256` |

命名遵循既有习惯：摘要一律 `<对象>_sha256`，不用 `digest`；不新造 `generated_at_utc`。
原始事实先由场景脚本写入本次 job 的 evidence root；cleanup 完成后，外层 finalizer 注入
`campaign_id`／`attempt_id`／`run_nonce`，再写入
`<attempt>/evidence/receipts/scenarios/<job_id>/retry-<attempt_index>/<scenario_id>-scenario-receipt.json`。
每次补跑使用独立目录，按 `_write_json_once`（`codex_upgrade_receipt_finalizer.py:870-905`）
原子独占写入、0600，禁止旧收据参与新一轮判定。`evidence_bindings` 必须由 finalizer 对每个
已批准 job root 做路径、大小和 SHA-256 校验；不得把外部证据路径伪装成 attempt root 内路径。

### 4.3 逐场景必需字段

字段名承接升级计划 §11.3，并补齐来源约束。以下字段均位于顶层 `facts` 对象内（例如
`facts.call_create_status`）；所有布尔与计数字段在 schema 中按成功态 const 钉死。

**A11 realtime sideband**（`final_state` = `const "sideband_established"`）

| 字段 | 约束 | 来源 |
|---|---|---|
| `call_create_status` | 整数，`2xx`（schema 限 200–299） | relay 字节中 `POST /backend-api/codex/realtime/calls` 的响应状态行 |
| `call_id_sha256` | 64 位 SHA-256 | call-create 响应体；不落原始 `call_id` |
| `sdp_or_started_event` | 枚举 `sdp_answer` / `thread_realtime_started` | 驱动事件日志中的最终通知 |
| `async_error_count` | `const 0` | 驱动事件日志中 `thread/realtime/error` 计数 |
| `sideband_sni` | `const "api.openai.com"` | pcap ClientHello |
| `sideband_call_id_linked` | `const true` | sideband 请求与 `call_id_sha256` 关联 |

**A13 OAuth refresh**（`final_state` = `const "token_refreshed"`）

| 字段 | 约束 | 来源 |
|---|---|---|
| `token_request_method` | `const "POST"` | relay 字节请求行 |
| `token_request_path` | `const "/oauth/token"` | relay 字节请求行 |
| `oauth_sni` | `const "auth.openai.com"` | pcap ClientHello |
| `jwt_exp_observation` | 对象 `{exp_at_utc, observed_at_utc, within_refresh_window: const true, token_sha256}` | 采集前读取 access token JWT 的 `exp`；**只记 `exp` 与 token 摘要，绝不落 token 本体** |
| `credential_restore` | 对象 `{before_sha256, after_sha256, restored: const true}` | cleanup 完成后的 `auth.json` 原始字节摘要；`before_sha256 == after_sha256` 才能证明逐字还原 |

`jwt_exp_observation` 同时是 R3 的核心证据：它证明刷新是 JWT 自然进入 5 分钟窗口所致，
而不是靠改 `last_refresh` 伪造——后者已被 0.147 正式冻结树
（`login/src/auth/manager.rs:2762-2783`）证伪。

**A14 file upload**（`final_state` = `const "upload_chain_complete"`）

| 字段 | 约束 | 来源 |
|---|---|---|
| `tool_name` | 非空（预期 `save_site_version`） | 驱动／CLI 事件中的工具调用记录 |
| `tool_call_id` | 非空 | 同上 |
| `create_request` | 对象 `{method: const "POST", path: const "/backend-api/files", status_2xx: const true}` | relay 字节 |
| `upload_url_source_event` | 对象 `{event, host, url_sha256}` | **create 响应体**；`host` 必须由响应派生，不得来自配置 |
| `put_destination` | 对象 `{host, sni, first_seen_at_utc, last_seen_at_utc}` | `-i any` pcap 的区域 ClientHello；官方侧不声称从加密 pcap 取得 PUT URL |
| `uploaded_event` | 对象 `{method: const "POST", path_suffix: const "/uploaded", status_2xx: const true}` | relay 字节 |
| `regional_sni` | `^[a-z0-9.-]+\.oaiusercontent\.com$` | pcap ClientHello |
| `regional_host_from_response` | `const true` | 断言 `regional_sni` 等于 `upload_url_source_event.host` |
| `upload_sequence` | 对象 `{create_before_regional: const true, regional_before_uploaded: const true}` | relay 与 pcap 原始时间戳；结合冻结源码证明 uploaded 只在 PUT 成功后发出 |

`regional_host_from_response` 是规格「不得硬编码单一区域上传 host」
（`docs/CODEX_CLI_0145_EGRESS_SPEC.md:609`）的机器表达：只有当 pcap 里观测到的区域主机
**等于本轮响应返回的主机**时才成立，预列域名凑出的 SNI 无法满足。

### 4.4 抓包范围修正（A14 盲区）

现有 `-i lo` + `RELAY_HOSTS` 预列 `sdmntprwestus3.oaiusercontent.com` 的组合有两个问题：
硬编码单一区域分片违反规格；响应返回其他分片时流量走真实网卡，pcap 完全看不到。

**定案**：

1. 抓包改为 `-i any` + `tcp port 443`，与候选侧 `run_candidate_aux_capture.sh:769` 及
   `SPEC-TLS-003` 的既有先例一致；
2. `RELAY_HOSTS` **移除**预列的区域上传主机。PUT 直连真实区域主机，不劫持——
   这既避免了「先知道主机才能劫持」的时序死结，也保证观测到的是未被改变的真实出站形态；
3. 三跳证据分工：create（经中继，relay 字节可见 `upload_url`）→ PUT（直连，pcap 见 SNI）
   → uploaded（经中继，relay 字节可见）。官方侧只证明响应 host 与区域 SNI 一致及三事件顺序；
   `file-url-chain` check 的 `record_type` 是
   `file_upload_chain`，由候选侧产出，不在官方侧范围内。

技术核对：`-i any` 的 pcap linktype 为 LINUX_SLL／SLL2，`pcap_clienthello.py:33-35` 已支持
（16／20 字节链路层剥离），无需改解析器。`-i any` 下 loopback 流量可能出现进／出两份重复
ClientHello，对 `any_equal`／`count_at_least` 判据无影响；`same_set_distinct_order` 类判据
不适用于这三个场景。抓到的仍是加密流量，照走既有 `scrub_raw_bytes.py --verify` 与
post-run secret scan，不新增暴露面。

## 5. 状态机

### 5.1 job 完成判定的第四条件

`Job` 新增字段 `required_scenario_receipts: tuple[str, ...] = ()`，声明该 job 必须为哪些场景
产出真实性收据。**不复用 `scenario_ids`**：后者表达「该 job 覆盖哪些场景」，`official-core`
一个 job 就覆盖 9 个场景，不可能逐个产收据；真实性门禁只约束目标场景。

判定改为四条件合取：

```text
complete ⇔ steps_ok ∧ ¬missing_patterns ∧ ¬empty_patterns ∧ scenario_receipts_ok
```

`scenario_receipts_ok` 对每个声明的场景要求全部成立：收据文件存在且非软链 → schema 校验
通过 → `scenario_id`／`job_id`／`run_id`／attempt 身份三元与当前 job 逐字匹配 →
`evidence_bindings` 中每个 path 在本 job 的证据根内且 `sha256` 复核一致。

场景脚本不能直接伪造 attempt 身份。`run_job` 必须把当前 attempt 上下文和批准的 job root
传给外层 finalizer；finalizer 只承接脚本产出的原始事实，不根据退出码补写协议字段。运行时
不引入 `jsonschema` 时，必须新增与 schema 等价的精确字段校验和 mutation 测试。

任一不成立即 `failed`，并在结果中记录原因。**不新增 job 状态**，保持 `complete`／`failed`
二元，避免破坏 `_validate_capture_job_results`（`codex_upgrade.py:1575-1601`）与
`build_coverage`（`:1536-1572`）的既有语义。

job 结果新增两个字段，与既有 `missing_evidence_patterns`／`empty_evidence_patterns` 同构：

- `scenario_receipts`：`[{scenario_id, path, sha256, final_state}]`
- `scenario_receipt_failures`：`[{scenario_id, reason}]`

### 5.2 场景终态与 job 状态的关系

| 场景实际情况 | 收据 | job 状态 | 依据 |
|---|---|---|---|
| 目标分支成立 | 产出，结构合法 | `complete` | 四条件全满足 |
| 目标分支未触发（A13 无 refresh、A14 未调用工具） | 不产出 | `failed` | 收据缺失 |
| 上游拒绝（A11 call-create 400） | 不产出 | `failed` | 收据缺失 |
| 链路不完整（A14 有 create 无 PUT） | 不产出 | `failed` | 收据缺失 |
| 脚本崩溃／超时 | 不产出 | `failed` | 收据缺失 ∧ 退出码非 0 |
| 有 pcap 文件但无目标 ClientHello | 不产出 | `failed` | 收据构建器无法取得 SNI 字段 |
| 凭据还原失败（A13） | 不产出 | `failed` | `credential_restore.restored` 无法为 true |

最后一行同时修掉 §2.5 的静默还原缺陷：cleanup 先写出恢复结果，外层 finalizer 在确认
`before_sha256 == after_sha256` 后才允许生成 A13 成功收据。

### 5.3 与执行指纹、承接、补跑的关系

`_job_execution_sha256`（`codex_upgrade.py:227-239`）当前只指纹
`id/phase/suites/steps/evidence_roots/required`，刻意排除 `covers`／`scenario_ids`，
注释说明是为「允许批准场景重新映射规则与场景说明」。

**`required_scenario_receipts` 必须纳入该指纹**：它不是场景说明的重新映射，而是**改变了
这个 job 算不算成功的执行契约**。若不纳入，同一个 `execution_sha256` 下门禁要求可被悄改，
而 `_validate_capture_job_results` 与 `_prior_complete_results`（`:4569-4630`）都检测不到。

代价是全部 job 的 `execution_sha256` 改变，k36 的结果无法被 `resume --rerun-failed` 承接。
这与 §11.1「k36 只作诊断夹具，证据不迁移不复用」和「必须新建 k37」完全一致，不是副作用。

同 attempt 内补跑（`JOB_RETRY_LIMIT = 2`，`:1496-1497`）保持不变：因场景未成立而失败的 job
会被补跑至多两次；补跑前必须同时归档上一轮 job evidence、原始事实和 scenario receipt
目录（`_archive_failed_job_evidence`，`:1500-1518`）。这不掩盖真实差异——收据缺失是硬失败，
补跑两次仍缺就是 attempt 失败。

### 5.4 attempt 与 seal

attempt 状态机**不变**，仍是 `awaiting_receipts`／`failed`／`environment_contaminated`
（`:4833-4838`），迁移点 `:5526-5532` 不动。`required_jobs_ok`（`:5463-5469`）已依赖 job
status，门禁自动生效：任一必需 job 因缺收据失败，attempt 即为 `failed`，无法进入 seal。

seal 侧增加独立复算：`receipt_finalizer` 新增 `scenario` 子命令，从同一批原始证据重新提取
字段并与 run 阶段的收据逐字比对。需在五处同步登记，否则
`codex_upgrade_receipt_finalizer.py:1775` 会以「收据 schema_version 不受重放器支持」拒绝：

`RECEIPT_SUBCOMMAND_BY_SCHEMA`（`:74`）、`REPLAY_INPUT_NAMES`（`:79`）、
`REPLAY_CANONICAL_FIELDS`（`:101`）、`build_parser`（`:1908`）、`replay_receipt` 分派（`:1876`）。

## 6. 实施边界（R1 变更集的编码范围）

R1 只做门禁与真实性校验，**不改任何触发逻辑**（触发属 R2／R3／R4）。

1. 新增 `codex_upgrade_scenario_receipt.schema.json`，并同步更新
   `codex_upgrade_capture_attempt.schema.json` 的 `jobResult`；
2. 新增原始事实构建器与外层 scenario finalizer：按 `--scenario` 从 relay 字节、pcap、驱动
   事件日志提取字段；cleanup 未完成或任一必填字段缺失时退出非 0 且不写成功收据；
3. `Job` 增加 `required_scenario_receipts`，纳入 `_job_execution_sha256`；场景清单
   `capture_jobs` 的三个目标 job 增加该字段并同步 schema 与
   `_validate_scenario_manifest_shape`（`codex_upgrade.py:997`、`:1094-1106` 字段集合精确相等）；
4. `run_job` 增加第四条件、attempt 上下文传递、两个结果字段和等价的运行时精确 schema 校验；
5. `receipt_finalizer` 增加 `scenario` 子命令并完成五处登记；
6. 只删除 A11/A13/A14 目标驱动路径的 `|| true`，改为捕获退出码并交给外层 finalizer；不
   批量修改其他场景。`cleanup()` 可保留防止还原链中断，但必须记录每个恢复动作结果，失败
   不得生成 A13 成功收据；`trap` 补挂 `INT TERM`；
7. 抓包按 §4.4 改为 `-i any`，增加 tcpdump 可用性预检与 pcap 非空／可解析校验，
   直接对齐候选侧 `run_candidate_aux_capture.sh:667-668`、`:317-325` 的既有实现；
8. 驱动 `drive_codex_realtime.py` 落原始事件日志；R1 只负责日志、错误传播和缺收据失败，
   R2 再加入 V3 与 started/SDP 最终事件等待；
9. 按 §3.1 对齐两份场景清单的 kinds，增加批准 scenarios/assertion-profile 的 classify
   交叉校验，并按 §3.2 引入 official/candidate 分侧 trigger 契约。

## 7. 测试矩阵

新增 `tests/test_scenario_receipt.py`，按既有约定分 `ScenarioReceiptPassTest` 与
`ScenarioReceiptNegativeTest` 两个类共用夹具基类，`tempfile.mkdtemp` + `addCleanup`
（清理需先 `chmod 0644`，生产代码写的是 0600）。

| 层 | 用例 | 断言 |
|---|---|---|
| schema | `schema_version` 常量钉死；顶层与嵌套 `additionalProperties: false` | 照抄 `tests/test_candidate_rule_assertion.py:97-130` 的钉死写法 |
| 构建器 | A11／A13／A14 各一组真实字节夹具（relay `.bin` + 最小 pcap + 事件日志） | 产出收据字段与预期逐字相等 |
| 构建器负例 | **A11 call-create 400**：relay 中只有 400 与 `thread/realtime/error` | 退出非 0，**不写任何文件** |
| 构建器负例 | **A13 只改 `last_refresh`**：relay 中只有 models／Responses，无 `/oauth/token` | 同上 |
| 构建器负例 | **A14 工具未调用**：无 `/backend-api/files` create | 同上 |
| 构建器负例 | **只有 pcap 文件但缺目标记录**：pcap 仅含 `chatgpt.com` ClientHello | 同上 |
| 构建器负例 | A14 的 `regional_sni` 与响应返回 host 不一致 | 拒绝产出 |
| 构建器负例 | A14 的 `regional_sni` 不等于响应返回主机（模拟硬编码预列域名） | `regional_host_from_response` 无法为 true，拒绝产出 |
| 构建器负例 | A13 还原后摘要与还原前不等 | 拒绝产出 |
| 构建器负例 | pcap 只有 24 字节全局头 | 拒绝产出 |
| 生命周期负例 | 收据缺少 attempt 身份、引用外部未批准 root、固定路径重复或 retry 残留 | 拒绝产出 |
| 运行时校验 | 不依赖 `jsonschema` 的 schema 等价校验 | 逐字段 mutation 均失败关闭 |
| `run_job` | 声明 `required_scenario_receipts` 且收据齐备 | `complete`，`scenario_receipts` 有对应条目 |
| `run_job` 负例 | 收据缺失 / schema 非法 / `scenario_id` 不符 / `job_id` 不符 / attempt 身份不符 / `evidence_bindings` 的 sha256 不符 / 收据是软链 | 逐项 `failed`，`scenario_receipt_failures` 记录原因 |
| `run_job` 负例 | 退出码 0、证据目录非空、但收据缺失（**即 k36 的实际形态**） | `failed`——本门禁的核心回归用例 |
| 指纹 | `required_scenario_receipts` 改变时 `_job_execution_sha256` 必须改变 | 不等 |
| 承接 | 门禁上线后 k36 形态的旧结果不可被 `--rerun-failed` 承接 | 拒绝 |
| finalizer | `scenario` 子命令重放一致 | `replay_receipt` 通过 |
| finalizer 负例 | 手改收据任一字段后重放 | 报「收据内容与只读重放结果不一致」 |
| finalizer 负例 | 未在五处登记时重放 | 报「收据 schema_version 不受重放器支持」 |
| seal | 必需 job 因缺收据 `failed` | attempt 为 `failed`，seal 拒绝 |
| 交叉一致性 | 两份场景清单的 `required_artifact_kinds` 逐场景相等（§3.1） | 不等即失败 |

夹具一律用真实字节流构造（照 `tests/test_assertion_gate.py:27-39` 的 `H1_HTTP_STREAM` 与
手工掩码 WS 帧写法），不用 mock 替代解析。

## 8. 验收门禁

```bash
make test-capture-tools
```

```bash
make check-egress-spec
```

R1 完成的判据是：上表全绿，且用 **k36 的真实证据**作离线夹具重跑
`official-relay-realtime-webrtc`／`official-relay-oauth-refresh`／`official-relay-file-upload`
三个 job 的判定逻辑，三者**必须全部判 `failed`**。k36 只作诊断夹具输入，不迁移、不复用、
不封存，符合 §11.1。

## 9. 非目标

- 不修改 A11／A13／A14 的触发逻辑（属 R2／R3／R4）；
- 不修改 `SPEC-EP-002` 的判据语义，不引入「未观测即通过」的条件化；
- 不改动 `SPEC-TLS-003` 的判据（该项由 §10.9.2 的 classify `change` 流程单独处理）；
- 不为其余 12 个场景引入真实性收据——本轮只覆盖三个已证实失效的目标场景；
- 不改动 attempt 状态机、candidate 侧链路、Active/Previous 指针；
- 不引入 `jsonschema` 运行时依赖（仓库现状是 schema 只在测试中被读出断言，保持一致）。

## 10. 风险、侵入面与回滚

| 风险 | 处置 |
|---|---|
| `-i any` 抓到无关流量，扩大脱敏面 | 仍走既有 `scrub_raw_bytes.py --verify` 与 post-run secret scan；候选侧已长期使用 `-i any`，无新增暴露类型 |
| `-i any` 下 loopback ClientHello 重复计数 | 三个场景的判据均为 `any_equal`／`count_at_least`／`match`，重复不影响结论；已在 §4.4 记录 |
| 删除驱动路径 `|| true` 后，上游波动导致 job 失败率上升 | 同 attempt 内已有 `JOB_RETRY_LIMIT = 2` 补跑；真实上游拒绝本就应当失败关闭 |
| 误删 `cleanup()` 的 `|| true` 会中断还原链 | §6 第 6 条明确保留，只把还原结果纳入收据必填字段 |
| A13 需要等待 JWT 自然进入刷新窗口，job 超时 | `official-relay-oauth-refresh` 的 `timeout_seconds` 已是 3600；R3 另行确定等待策略 |
| 收据构建器本身成为新的单点 | 构建器与 seal 阶段 finalizer 独立复算并逐字比对，双侧不一致即拒绝 |
| 门禁上线使全部 job 指纹改变 | 与「必须新建 k37」的既定结论一致，见 §5.3 |

**回滚**：本变更集只新增校验与新文件，不删除既有判定逻辑。回滚方式是把
`required_scenario_receipts` 置空并撤销指纹纳入——但这会使门禁失效，因此只在证明门禁本身
有缺陷时使用，且必须重新走 R0 审核。

## 11. R0 完成结论与 R1 启动条件

按 §11.2，R0 的退出条件是「方案评审通过；无『脚本退出即成功』的路径」。本方案对后者的
覆盖是：§5.1 的四条件合取 + §5.2 的七种失败形态映射 + §7 中以 k36 真实形态为核心的回归
用例，三者共同保证退出码不再是成功的充分条件。

**首轮裁决**：

1. 清单 kinds 分叉并入 R1：**同意**，并增加批准画像交叉校验和冻结契约摘要重算；
2. 三个 trigger 修订并入 R1：**同意**，但必须保留 official/candidate 双侧语义；
3. A14 `-i any` 且不劫持区域主机：**有条件同意**，收据不得声称从加密 pcap 得到 PUT URL，
   只记录响应 host、区域 SNI、事件顺序和 uploaded 2xx。

本方案已补齐：收据生命周期与 retry 归档、cleanup 后 A13 原始字节恢复证明、attempt schema
与运行时精确校验、双侧 trigger、批准画像交叉校验及正式源码坐标。下一步进入 R1；R1 完成前
不重采、不创建 k37、不改变 Active/Previous。

## 12. R1 实施结果（2026-08-10）

§6 的九项实施范围已全部落地，§7 测试矩阵新增 40 条用例，本地与 ARM64 双侧门禁通过。
触发逻辑一律未改——A11／A13／A14 仍不会进入目标协议分支，这正是 R1 的预期状态：
门禁保证「没触发」不再被记成「完成」，真正的触发修正属 R2／R3／R4。

### 12.1 落地清单

| § | 内容 | 落点 |
|---|---|---|
| 6.1 | 收据 schema 与 attempt `jobResult` | 新增 `codex_upgrade_scenario_receipt.schema.json`；`codex_upgrade_capture_attempt.schema.json` 的 `jobResult` 增 `scenario_receipts`／`scenario_receipt_failures` |
| 6.2 | 原始事实构建器与外层 finalizer | 新增 `build_scenario_facts.py`、`scenario_receipts.py`；finalizer 新增 `scenario` 子命令 |
| 6.3 | `Job.required_scenario_receipts` 并入执行指纹 | `codex_upgrade.py` 的 `Job`、`_job_execution_sha256`、`_validate_scenario_manifest_shape`、`load_scenario_jobs` |
| 6.4 | `run_job` 第四条件与 attempt 上下文 | 新增 `ScenarioReceiptContext`，沿 `_run_capture_attempt` → `_run_job_with_retry` → `run_job` 透传 |
| 6.5 | finalizer 五处登记 | `RECEIPT_SUBCOMMAND_BY_SCHEMA`／`REPLAY_INPUT_NAMES`／`REPLAY_CANONICAL_FIELDS`／`build_parser`／`replay_receipt` 显式 `elif` |
| 6.6 | 目标驱动路径错误传播与还原证明 | `run_official_relay_scenario.sh`：`realtime_status`／`exec_status`、`restore_auth_json` 幂等函数、`trap cleanup EXIT INT TERM` |
| 6.7 | 抓包范围与 pcap 校验 | `-i lo` → `-i any`，新增 tcpdump 预检、`verify_pcap`（≤24 字节与不可解析均失败） |
| 6.8 | 驱动原始事件日志 | `drive_codex_realtime.py` 新增 `--events-output`，不再丢弃 `thread/realtime/start` 返回值 |
| 6.9 | 清单 kinds 对齐与双侧 trigger | 两份清单 A01／A15 对齐；三个目标场景新增 `side_triggers`；重算三处冻结摘要 |

`producer` 复用既有 `codex-egress-receipt-producer/v1`，不另造一套，重放器按同一规则复算。
收据落在 `<attempt>/evidence/receipts/scenarios/<job_id>/retry-<n>/`，补跑写独立目录，
旧收据不参与新一轮判定。

### 12.2 冻结摘要变更与审核

对齐 A01／A15 的 `required_artifact_kinds` 会改变验收契约摘要。逐项复核结果：
**25／17 分组不变**（`dual_wire=25`、`candidate_profile=17`），`validation_modes` 与
`expected_check_ids` 无任何变化，`side_coverage` 只有 A01（`pcap+relay_binary` →
`pcap+process_trace`）与 A15（`relay_binary+process_trace` → `process_trace`）两项按
§10.9.3 定案变化。旧摘要经复算与冻结值逐字相同，证明对比基准准确。

| 常量 | 旧值 | 新值 |
|---|---|---|
| `acceptance_contract.FROZEN_CONTRACT_SHA256` | `14e9e336…` | `1689ab30…` |
| `candidate_rule_assertion.FROZEN_PROFILE_SHA256` | `b52c11ea…` | `78a0ec3f…` |
| `candidate_test_trace.FROZEN_PROFILE_SHA256` | `b52c11ea…` | `78a0ec3f…` |

### 12.3 验收证据

`make test-capture-tools` 564 项通过（新增 40 项），`make check-egress-spec` 全绿。

§8 的核心判据已复现：用 k36 的实际形态（子进程退出 0、`tls/relay.crt` 与 `relay/`
存在且非空、无任何场景事实）重跑三个目标 job 的判定逻辑，
`official-relay-realtime-webrtc`／`official-relay-oauth-refresh`／`official-relay-file-upload`
**全部判 `failed`**，失败原因均为「未产出场景原始事实，目标协议分支未成立」。

按升级计划 §11.0，在 ARM64（`ss.3ab.in`，`aarch64`／Python 3.12.3）复验：Python 套件
564 项全过；容器内 `go build ./...` 成功，`internal/service`、`internal/repository`、
`internal/pkg/httpclient` 三包 `go test` 的 `--- FAIL` 落盘计数为 0（不经管道判定，
避免退出码被吞）。ARM64 结果不视为 Vircs 上线通过，本变更集也不触碰任何生产实例。

### 12.4 R1 未覆盖、留给后继阶段的部分

- A11 的 V3／`quicksilver=v2` 路径与 started／SDP 最终事件等待属 R2；
- A13 等待 JWT 自然进入刷新窗口的采集策略属 R3；
- A14 的 Apps 工具可见性与调用契约冻结属 R4。三者所需的采集侧观测文件
  （`A11-realtime-events.json`、`A13-jwt-exp.json`、`A14-tool-call.json`、
  `A14-upload-sequence.json`）契约已由构建器固定，缺失即失败关闭；
- 受管工具身份已变化，按 §11.1 必须新建 k37 完整重采，k36 不迁移、不复用。

## 13. R2 实施结果：A11 走官方 V3（2026-08-10）

### 13.1 根因（0.147 冻结树核实）

k36 的 A11 报 `400 invalid_quicksilver_alpha_header`，根因在版本默认值：

```rust
let version = params.version.unwrap_or(match &transport {
    ConversationStartTransport::Websocket => config.realtime.version,
    ConversationStartTransport::Webrtc { .. } => RealtimeWsVersion::V1,
});
```

（`core/src/realtime_conversation.rs:1170-1172`）驱动走 WebRTC 且从不声明 `version`，
于是落到 **V1**，发出 `openai-alpha: quicksilver=v1`，而上游已不再接受该值。

逐项核实的协议事实：

| 项 | V1（此前的实际路径） | V3（目标） |
|---|---|---|
| `openai-alpha` | `quicksilver=v1` | `quicksilver=v2`（命名错位，官方实现如此，`:1647-1661`） |
| event parser | `V1` | `FramelessBidi` |
| sideband call_id 位置 | query `?call_id=` | **路径末段** `/v1/live/{call_id}`（`codex-api/.../methods.rs:985-993`） |
| 默认模型 | `DEFAULT_REALTIME_MODEL` | `DEFAULT_FRAMELESS_REALTIME_MODEL` |

另有两条硬约束：WebRTC 只接受 v1／v3，v2 会被 `validate_avas_webrtc_start`
（`:1232-1247`）拒绝；v1／v3 **不能用 text 输出模态**，否则报
「text realtime output modality requires realtime v2」（`:1336-1342`）——现有采集已用
audio，无需改动。

### 13.2 同时修正 R1 的一处错误假设

`ThreadRealtimeStartResponse` 是**空对象**（`app-server-protocol/src/protocol/v2/realtime.rs:161-165`），
call_id 根本不在 start 响应里。R1 的事件日志从 `started.callId` 取值，结果恒为空，
收据构建器据此做的 `sideband_call_id_linked` 判据永远不可能成立。

改为：call_id 只从 relay 字节的 call-create 响应体取；`sideband_call_id_linked` 由
构建器在 relay 字节的 WS 升级请求里匹配——V3 认路径末段、V1／V2 认 query，两种形态
都接受，但都必须等于本轮 call-create 返回的那个 call_id。这样关联是从原始字节互相
印证出来的，而不是驱动自己声称的。

### 13.3 落地清单

| 内容 | 落点 |
|---|---|
| 显式声明版本 | `drive_codex_realtime.py` 新增 `--realtime-version`（默认 `v3`），写入 `thread/realtime/start` 的 `version` 字段 |
| 等待最终事件 | 新增 `wait_for_notification`，等 `started`／`sdp`／`error`／`closed`，不再无条件 sleep 到 hold 结束 |
| 事件日志升版 | `codex-egress-realtime-events/v2`：记 `requested_version`、`negotiated_version`、`realtime_session_id`，不再产出 call_id |
| 关联判据 | `build_scenario_facts.py` 新增 `_sideband_joins_call`，按 V3／V1 两种形态匹配 |
| 采集脚本 | 传 `--realtime-version "${REALTIME_VERSION:-v3}"` |

### 13.4 验收

`make test-capture-tools` 571 项通过（较 R1 新增 7 项），`make check-egress-spec` 全绿。
新增负例：sideband join 到别的 call_id、完全没有 sideband 连接，均拒绝产出事实。
端到端冒烟（假 app-server）确认 `"version": "v3"` 真实进入请求参数，驱动等到
`thread/realtime/started` 才继续，事件日志记录协商版本。

**仍未验证的部分**：真实上游是否接受 V3、是否返回 2xx 与 call_id，只能在 k37 的真实
官方采集中回答。R2 交付的是「按官方 V3 正确发起并等待最终事件」的能力，不是该场景
已成立的结论。
