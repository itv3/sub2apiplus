# Codex 0.154.0 VC-2 前置：两个受控压缩 Job 的四次运行复盘与 GPT-6 Astra 语义分类输入

> 本文件是改造方案（2026-09-15 第十一版）D 批次的产物：只读取已封存证据出结论，不重放、不改写任何
> 历史 Campaign、attempt 或收据；不发送模型请求。结论供老板裁定与 VC-2 分类使用，不是放行收据。

## 1. 复盘对象与证据来源

| Campaign | attempt | Job | 证据根（宿主） | 采集时间 |
|---|---|---|---|---|
| `c0154-formal-vc1-bwg-new-window-20260914t100818z` | `20260914T102852Z-04996800fbbe4e94` | `official-relay-comp-hash-changed` | `data/runs/<campaign>-official-comp-hash-changed` | 2026-09-14 |
| 同上 | 同上 | `official-relay-model-downshift` | `data/runs/<campaign>-official-model-downshift` | 2026-09-14 |
| `c0154-formal-vc1-bwg-fresh-20260914t223835z` | `20260914T232051Z-f735b7996999027b` | `official-relay-comp-hash-changed` | `data/runs/<campaign>-official-comp-hash-changed` | 2026-09-14 |
| 同上 | 同上 | `official-relay-model-downshift` | `data/runs/<campaign>-official-model-downshift` | 2026-09-14 |

四次运行在 attempt 中均为 `status=complete`、`disposition=executed`。判定依据只有三类只读文件：
`compaction-reason.json`（压缩原因提取器输出）、`comp-hash-catalog.json`／`model-downshift-catalog.json`
（受控模型目录）、`relay/relay.json` 与 `relay/conn*.bin`（字节中继元数据与原始字节），以及 A1a 审计收据
`data/audit/official-attempt-audit-<campaign>-20260915t1540z.json`。

## 2. 四次运行的事实

四次运行的结构完全一致：

| 事实 | comp-hash-changed（两次） | model-downshift（两次） |
|---|---|---|
| 压缩原因提取 | `status=complete`，`exact_match_count=1`，`unexpected_reason_count=0`，命中 `comp_hash_changed` | 同左，命中 `model_downshift` |
| 触发请求 | `conn001` WebSocket `response.create`，`model=gpt-5.5`，`input_types=[compaction_trigger]`，`implementation=responses_compaction_v2`，`phase=pre_turn`，`strategy=memento` | 同左 |
| 中继连接完整性 | 6 条连接，`both=6`，`clean=true`，全部 `valid` | 同左 |
| 受控目录首模型 | `gpt-5.5`（`comp_hash=comp-hash-probe-first`，窗口 272000） | `gpt-5.5`（`comp_hash=downshift-probe`，窗口 272000，`auto_compact_token_limit=16000`） |
| 受控目录第二模型 | **`gpt-5.3-codex-spark`**（`comp_hash=comp-hash-probe-second`，窗口 128000） | **`gpt-5.3-codex-spark`**（`comp_hash=downshift-probe`，窗口 128000，`auto_compact_token_limit=8000`） |
| 线上请求模型（A1a `wire_models`） | `gpt-5.3-codex-spark`、`gpt-5.5` | `gpt-5.3-codex-spark`、`gpt-5.5` |
| A1a `models` 段 | 失败：「受控目录第二模型 `['gpt-5.3-codex-spark']` 不等于 Campaign Lite 模型 `gpt-6-astra`」；`wire_consistent=false` | 同左 |

两个 Campaign 冻结的配置均为 `model=gpt-5.5`、`lite_model=gpt-6-astra`；A1a 对其余 27 个 Job 的模型判定全部
通过（`expected_track_models`：main 实际 `gpt-5.5`，lite 实际 `gpt-6-astra`）。

## 3. 三类归因的逐项排除

| 归因 | 判据 | 结论 |
|---|---|---|
| 场景不稳定 | 四次运行是否出现不同的触发结果、连接残缺或额外压缩原因 | **排除**。2 个 Campaign × 2 个 Job 四次结果逐字一致：原因精确命中一次、无额外原因、6 条连接全部干净。触发条件（comp hash 变化；旧窗口 272000 > 新窗口 128000 且当前 token 超过 8000 阈值）按设计稳定成立。 |
| 上游行为变化 | 上游返回的模型目录是否缺少 Astra 或改变了压缩实现 | **排除**。同一 attempt 的 `models-http.jsonl`（`GET /backend-api/codex/models?client_version=0.154.0`，200）返回 8 个模型，`gpt-6-astra` 在列且 `use_responses_lite=true`；压缩实现仍是 `responses_compaction_v2`／`memento`，与 0.151 一致。第二模型不是上游选的，而是工具写进受控目录的。 |
| 工具缺陷 | 受控目录第二模型是否来自工具硬编码而非 Campaign 冻结值 | **成立**。四次运行时受管工具树的 `run_official_relay_scenario.sh` 对 0.154 目标把第二模型硬编码为 `gpt-5.3-codex-spark`；修复提交 `26ad5a72e`（2026-09-15 12:44 +0800，「闭合 0.154 Astra Lite 两项恢复」）才改为读取场景清单注入的 `COMPACTION_SECOND_MODEL={lite_model}`，缺省回退 `gpt-6-astra`，并新增 `build_compaction_model_catalog.py` 校验第二模型的 `use_responses_lite` 与轨道。四次运行全部发生在修复之前（2026-09-14）。 |

结论：**四次运行属于同一工具缺陷（受控目录第二模型硬编码），不是场景不稳定，也不是上游行为变化。** A1a 的
判定规则本身正确，不应放宽；它准确指出了证据与 Campaign 冻结 Lite 模型不一致。

## 4. 对 VC-2 的影响与可选处置（需老板裁定）

两个 Job 的证据分两部分看：

- **压缩触发语义**（SPEC-EP-023 覆盖的 `comp_hash_changed`／`model_downshift` 触发条件与
  `responses_compaction_v2` 请求形态）：触发请求的模型是主轨 `gpt-5.5`，四次一致，与第二模型无关，这部分
  证据可用。
- **降级目标**：`model-downshift` 降级后的后续请求确实发向了 `gpt-5.3-codex-spark`（`wire_models` 含
  spark），而不是 Campaign 冻结的 Lite 模型 `gpt-6-astra`；`comp-hash-changed` 的第二模型只作为目录 hash
  变化的对照项参与，线上仍出现了一次 spark 请求。VC-2 若要分类「降级与回退路径」到 Astra，这两份证据不足。

可选处置：

| 选项 | 内容 | 代价 | 备注 |
|---|---|---|---|
| A | A3b 复用导入时把这两个 Job 记为「压缩触发语义可用、降级目标为已登记的 I 类干预偏差」，VC-2 只用其触发语义，降级目标分类留空 | 零请求；需在 A1a 审计规则中为这两个 Job 增加显式的「偏差已登记」豁免（控制层改动，需重新签发发布认证） | 降级到 Astra 的线上形态没有证据 |
| B | 在 A3b 导入的后继 Campaign 内，以修复后的工具只补跑 `official-relay-model-downshift`（必要时含 `comp-hash-changed`），第二模型为 `gpt-6-astra` | 每个 Job 约 6 条中继连接、1 次压缩触发请求；须计入项目总账并经恢复预览批准 | 违反「不建新的 0.154 正式证据 Campaign」的字面约束，但补跑发生在 A3b 复用导入的 Campaign 内，是否允许由老板裁定 |
| C | 维持 A1a 失败，A3b 不导入，改为全新 Formal Campaign | 29 个 Job 全部重采（约 64 条以上模型请求） | 与已定事项 18 冲突，不推荐 |

建议：先按 A 完成 A3b 导入与 VC-2 的触发语义分类；「降级到 Astra」这一项在 VC-2 记为待补证据，
若老板批准 B，则在导入后的 Campaign 内单 Job 补跑，补跑前后由 reconciler 与项目总账记账。

## 5. GPT-6 Astra 完整语义分类（VC-2 输入）

来源：fresh attempt 的 `models-http.jsonl`（`GET /backend-api/codex/models?client_version=0.154.0`，请求头只有
`accept`、`authorization`、`chatgpt-account-id`、`originator`、`user-agent`、`version`；响应 200，8 个模型），
以及 A1a 审计对 Lite 轨的判定（实际请求模型 `gpt-6-astra`）。0.151 基线为同一响应中的 `gpt-5.6-luna` 条目与
0.151 证据标签（Lite 专项固定 `gpt-5.6-luna`，`use_responses_lite=true`）。

### 5.1 模型发现

| 项目 | 0.154 Astra | 0.151 Luna | 分类 |
|---|---|---|---|
| 目录位置 | `priority=1`，`visibility=list`，`supported_in_api=true` | `priority=8`，`visibility=list`，`supported_in_api=true` | Astra 成为目录首位 |
| 最低客户端版本 | `minimal_client_version=0.153.0` | `0.144.0` | 0.153 以下客户端不会发现 Astra；0.154 满足 |
| 上下文窗口 | `context_window=272000`，`max_context_window=872000` | 同值 | 不变 |
| 目录内其它 Lite 模型 | `gpt-reserve`（`visibility=hide`）、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`codex-auto-review`（hide）均 `use_responses_lite=true` | — | Lite 轨候选不止一个，Campaign 冻结值必须显式指定 |
| 非 Lite 模型 | `gpt-5.5`（`priority=12`）、`gpt-5.3-codex-spark`（`priority=26`，`supported_in_api=false`，窗口 128000，仅 text） | — | 主轨仍为 `gpt-5.5` |

### 5.2 请求模型名

| 项目 | 0.154 Astra | 0.151 Luna | 分类 |
|---|---|---|---|
| Lite 轨实际请求 `model` | `gpt-6-astra`（A1a：lite 轨实际 `["gpt-6-astra"]`，通过） | `gpt-5.6-luna` | 请求字段直接使用目录 slug，不做映射 |
| `comp_hash` | `3000` | `3000` | 相同；`gpt-5.5`／spark 为 `2911` |
| 传输偏好 | `prefer_websockets=true` | `prefer_websockets=true` | 不变 |

### 5.3 能力声明

| 项目 | 0.154 Astra | 0.151 Luna | 分类 |
|---|---|---|---|
| Lite 标记 | `use_responses_lite=true` | `true` | 不变，Lite 条件成立 |
| 推理档位 | `low/medium/high/xhigh/max/ultra`，默认 `medium` | `low/medium/high/xhigh/max`，默认 `medium` | 新增 `ultra` |
| 工具模式 | `tool_mode=code_mode_only`；`experimental_supported_tools=[send_user_message_async, clock]` | `code_mode_only`；无实验工具 | 新增两个实验工具 |
| 多代理 | `multi_agent_version=v2`，`multi_agent_reasoning_effort=xhigh` | `v1`，无 effort | 版本升级 |
| 模型消息 | `model_messages` 含 `approvals、auto_review、collaboration_modes、confirmation_policies、guardian_v2、instructions_template、multi_agent、persistent_instructions、token_budget` | 仅 `instructions_template、token_budget` | 新增七类客户端行为消息 |
| 指令模板 | 20919 字节（sha256 前缀 `dfbb810c`） | 17730 字节（`a91357a1`） | 内容不同 |
| 自动审查 | `node_repl_auto_review_required=true` | `false` | 新增 |
| 其它 | `service_tiers=[priority: 2x speed]`；`include_skills/plugin/apps_usage_instructions` 均 `false`；`supports_parallel_tool_calls=true`；`web_search_tool_type=text_and_image` | `priority: 1.5x`；plugin／apps 指令 `true`，skills `false` | 速度档与指令注入策略变化 |

### 5.4 降级与回退路径

| 项目 | 事实 | 分类 |
|---|---|---|
| 目录声明的升级／回退 | Astra 与 Luna 的 `upgrade` 均为 `null`，`availability_nux` 仅 Astra 有介绍文案 | 上游未声明自动降级目标 |
| 客户端降级实现 | `model_downshift` 压缩由客户端在目录旧窗口大于新窗口且当前 token 超过新模型阈值时触发（0.154 与 0.151 相同的 `responses_compaction_v2`） | 行为不变 |
| 降级到 Astra 的线上证据 | 无（见第 4 节，现有证据降级目标为 spark） | **待补** |

### 5.5 账号权限条件

| 项目 | 0.154 Astra | 0.151 Luna | 分类 |
|---|---|---|---|
| `available_in_plans` | 24 个计划（含 `free`、`plus`、`pro`、`team`、`enterprise` 等） | 同一集合 | 不变；本次账号（`chatgpt-account-id` 请求头）可见 |
| `available_access_programs` | `{cyber: [standard]}` | 同值 | 不变 |
| `guardian` | `null`（`model_messages.guardian_v2` 存在） | `null` | 客户端侧 guardian v2 消息新增，服务端未强制 |
| `requires_sandboxed_review` | `false` | `false` | 不变 |

## 6. 与方案的关系

- 本文件只做分类与归因，不改变任何证据或收据；A1a 审计结论保持「失败（models）」不变。
- A3b 的复用导入前置要求「A1a 五项通过」，因此第 4 节的处置选项必须先由老板裁定，A3b 才能开始。
- 若采用选项 A，需要的工具改动是控制层（审计规则豁免登记），按策略 v2 不改变 wire producer 身份，
  但须重新签发发布认证并重新部署后再执行 A3b。
