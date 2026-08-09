# `accept` 验收链路的六项接线缺陷与最小安全验收模型（已闭环）

> 从升级计划 §10.8 抽出。六项缺陷已全部修复并在 Campaign `…-k35` 的真实证据上
> 验证通过（ACC-01～07）；本文保留完整诊断与实施记录供追溯，升级计划正文只留结论。
>
> **本文的前提有局限**：它假设「证据齐备，只是接线错了」，而该假设的实证只覆盖
> 单条规则、单个证据根。修好接线后暴露的第二层问题（场景定义与采集能力错配）
> 见升级计划 §10.9。

## 背景

> **本节六项缺陷已全部修复并在 k35 真实证据上验证通过**（ACC-01～07，见 §10.8.11）。
> 验收模型（§10.8.10）成立，不需重做。
>
> 但本节有一个未经验证的前提：「证据齐备，只是接线错了」。§10.8.9 的实证只覆盖单条
> 规则、单个证据根，不足以支撑该前提。修好接线、真正跑通编目后暴露出**第二层问题**：
> 场景定义与采集能力错配，见 §10.9。两层是不同问题——接线不通就跑不到编目，跑不到
> 编目就看不见证据缺口。

## 10.8.1 阻断现象

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

## 10.8.2 已核实事实与尚未闭环的证据边界

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

## 10.8.3 缺陷一：capture-manifest 覆盖不全且标签语义错误

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

## 10.8.4 缺陷二：采集阶段是多根，断言阶段是单根

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

## 10.8.5 缺陷三：断言证据路径与 inventory 路径空间不一致

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

## 10.8.6 缺陷四：`seal` 不校验场景 artifact 覆盖

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

## 10.8.7 缺陷五：旧 `accept` 错把两类规则强制成同一种双侧模型

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

## 10.8.8 缺陷六：正负例字段没有可执行语义

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

## 10.8.9 已验证可行的部分

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

## 10.8.10 审核后的最小安全验收模型（未实施）

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

## 10.8.11 实施记录（全部完成）

七项变更集已实施并自复核，每项独立提交，`make test-capture-tools`（524 项）与
`make check-egress-spec` 全绿：

| 变更集 | 提交 | 交付物 |
|---|---|---|
| ACC-01 验收契约 | `c632e6155` | `acceptance_contract.py`、results v2 schema |
| ACC-02 证据包 | `f04f6830a` | `build_assertion_bundle.py` |
| ACC-02b 官方观测派生器 | `04b17f50a` | `derive_official_observations.py` |
| ACC-03 seal 门禁 | `e34051ccd` | `assertion_gate.py` 与 seal／stage 契约接线 |
| ACC-04 编排与 accept | `cd779ba71` | `build_rule_assertion_results.py` 重写、accept 分模式校验 |
| ACC-05 端到端复验 | `5440acf96` | `test_acceptance_end_to_end.py` |
| ACC-06 采集流程接线 | `9453131b3`、`e617ea4f2` | `assertion-bundle` 在真实 seal 路径上落位 |
| ACC-07 证据编目 | 待提交 | `build_evidence_catalog.py`、`codex_upgrade_evidence_labels_0_145_0.json` |

实施中另发现并修复四处真实缺陷：编排器自造断言命令（与 accept 复算的期望命令永不
相等）、编排器与 accept 的契约权威不同源、`assertion-bundle` 从未接进采集流程（任何
真实 seal 都会被门禁拒绝）、`str.splitlines()` 在 JSON 内的 `\x85`／`\u2028` 处误切
JSON Lines（真实 mitm 记录含 `\x85`，一条记录被截断成两条非法 JSON）。

**k35 实证**：编目 94 项收口、105 项派生、199 个 artifact；`SPEC-TLS-001` 三条 check
全部通过——历史上第一次有官方侧 wire 规则在真实 0.147 证据上跑出 pass。

**证据标签权威**（ACC-07 的核心边界）：manifest 的 `labels` 一律来自冻结声明，描述
采集时已确定的**实验条件**（CA 模式、协议面、residency 等）；编目器只做路径匹配，
不打开任何证据文件。判据独立性要求：标签不得由该标签所选中的判据正在验证的属性反推，
否则判据退化为同义反复。声明每条规则必须给出 `rationale` 说明标签如何由采集参数或
场景 precondition 得出。

**契约权威边界**：`seal` 门禁在 classify 之前执行，只能以仓库冻结画像做证据充分性
预检并证明仓库画像未漂移（`FROZEN_CONTRACT_SHA256 = 14e9e336…`，机器推导得
`dual_wire=25`／`candidate_profile=17`）；`compare`／`accept` 一律以本 Campaign 批准的
`assertion-profile.json` 推导契约——目标规则集允许相对基线增删，验收权威必须随批准
画像走。

**重新冻结的工具身份**：受管 89 个 `.py/.sh/.json`（k34 为 84 个，新增
`acceptance_contract.py`、`assertion_gate.py`、`build_assertion_bundle.py`、
`derive_official_observations.py`、`codex_upgrade_rule_assertions_v2.schema.json`），
集合摘要 `3b6c672ea8b6b2ee40018e2bc95dc1ceef4a53c3c2c2589273f21502d7d46709`
（本机与 Vircs `candidate-source-k35` 逐字一致，两侧 `make test-capture-tools` 507 项
全绿）。按 §6.1，新建 Campaign 后该摘要不得再变化。

原始清单（实施依据）：

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

当前状态：六项已实施且门禁全绿、工作区干净、工具摘要已重新冻结；**唯一未满足的开工
条件是第三方审核**。审核通过后即可按 §6.1 新建 Campaign。

## 10.8.12 结论

历史 25 个 Campaign 中无任何一个产出过 `assertions/<candidate-id>/results.json`，k34 是首个
到达 `compared` 的轮次。它暴露的不是 0.145 → 0.147 画像升级本身不可行，而是旧 `accept`
从未完成系统集成：**多根采集与单根断言不一致、17 条内部规则被错误要求双侧同构、正负例
契约没有权威语义、seal 又未提前拒绝不完整 manifest**。

k34 的官方／候选证据和 compare 结论仍有诊断价值，但因工具身份与验收契约将变化，不得迁移
为新 Campaign 的正式证据。后续只实现 10.8.10 定义的最小安全闭环，不借本次画像升级继续
扩张无关验收基础设施；新 Campaign 到达 `ready` 前，Active 始终保持 0.145。
