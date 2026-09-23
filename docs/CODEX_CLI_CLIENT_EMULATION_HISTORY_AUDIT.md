# Codex CLI 客户端仿真历史审计

> **定位**：本文件只保存历史版本、旧恢复机制和旧章节编号的审计索引，不是当前 Campaign 的执行流程。
>
> **当前执行入口**：[`CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](CODEX_CLI_CLIENT_EMULATION_GUIDE.md) 第四部分
> VC-0～VC-6；共享恢复算法以 [`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)
> §5.1.2 为准。

## 1. 0.149.1 与 0.147.0 版本证据沿革

历史证据可以继续支撑未变化规则，但不能决定当前 Runtime 角色；Active／Previous 只读取主手册 §3.2 和
Release Catalog。

| 版本 | 历史身份与证据入口 | 当前角色 |
|---|---|---|
| `0.149.1` | 官方 tag `rust-v0.149.1`（commit `ff29a44391deccde0aba0f8390337d7f3c319ea4`），Linux amd64 二进制 SHA-256 `e24fb784c7d71140d67afb620f56e9137496cf7f6c9e19217fa3666dcf306278`；HTTP／WS Main／WS Lite 原始 run 分别为 `codex-0_149_1-20260824T-http-main-r2`、`codex-0_149_1-20260824T-ws-main-r2`、`codex-0_149_1-20260824T-ws-lite-r1`；发布链见 [R28 Catalog 晋升](egress/maintenance/CODEX_CLI_0147_TO_01491_R28_CATALOG_PROMOTION_RECEIPT.json)、[R34 生产激活](egress/maintenance/CODEX_CLI_0147_TO_01491_R34_PRODUCTION_ACTIVATION_RECEIPT.json)和[终态收据](egress/maintenance/CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json) | Previous；只用于受控回滚、历史复算及继承规则证据 |
| `0.147.0` | 历史发布链见 [K83 Catalog 晋升](egress/maintenance/CODEX_CLI_0145_TO_0147_K83_CATALOG_PROMOTION_RECEIPT.json)与[K83 生产激活](egress/maintenance/CODEX_CLI_0145_TO_0147_K83_PRODUCTION_ACTIVATION_RECEIPT.json) | 已退出 Runtime Catalog；退休事实见 [0.151 Runtime Profile 退休收据](egress/maintenance/CODEX_CLI_01491_TO_0151_RUNTIME_PROFILE_REMOVAL_RECEIPT.json) |

<a id="codex-0151-historical-recovery"></a>
## 2. 0.151 历史恢复机制

0.151 升级曾使用多套恢复入口处理工具和控制链缺陷。它们只用于解释历史 Campaign、终态收据和旧
Schema；新 Campaign 不得调用下列入口。

该历史 Campaign 在 0.151 激活、回滚和目标恢复后，曾以 `codex-runtime-profile-removal/v1` 收据退休
0.147，并在 canonical checkpoint `00000009` 达到 `plan.execute_item_ids=[]`。这组版本号和 checkpoint
只解释 0.151 终态，不是后继 Campaign 的退休目标或执行参数。

| 历史机制 | 当时用途 | 当前处理 |
|---|---|---|
| `successor`：`candidate_runtime_identity_correction` | 更正候选运行坐标或执行身份 | 只读重放；当前按身份变化建立新 Campaign／candidate，或在首个 attempt 前登记坐标覆盖 |
| `successor`：`classification_fact_correction` | 保留官方证据并重新分类 | 一次性导入 canonical checkpoint，从 VC-2 继续 |
| `successor`：`candidate_failed_job_tool_recovery` | 修复产出工具后承接失败 Job | 禁止新建；当前按依赖图只执行失败、待执行和受变化影响的闭集 |
| `successor`：`candidate_recovery_control_refresh` | 旧恢复 Ledger 过期后的控制刷新 | 只读重放，不得用来重置预算 |
| `successor`：`sealed_stage_control_recovery` | 承接已封存 official 阶段的控制链 | 一次性导入 canonical checkpoint，从对应阶段继续，不扫描或重发官方证据 |
| `evaluation-transition` 与多槽 seal | 评估器修复、旧 attempt 恢复和追加式 seal | 仅由历史 reader 重放；当前改用 evaluator run 和单一 checkpoint |
| `control-epoch`／`runtime-repair` | 旧控制面或运行时修复分支 | 写入入口已禁止，只保留兼容读取 |

审计以 [`0.151 终态收据`](egress/maintenance/CODEX_CLI_01491_TO_0151_TERMINAL_STATE_RECEIPT.json)、
[`codex_upgrade_legacy_boundary.py`](../tools/official_client_capture/codex_upgrade_legacy_boundary.py) 及对应版本
Schema／测试为准。历史收据、attempt、Ledger 和 producer 字段保持生成时原文，不得用当前工具改写；
无法导入 canonical checkpoint 时保持停线。

## 3. 历史章节编号映射

历史退役收据和已冻结工具注释仍可能携带以下旧章节文字。此表只用于定位历史引用，不是执行入口；
新 Campaign 一律使用主手册第四部分开头的 VC-0～VC-6 执行导航。

| 历史章节文字 | 当前归属 |
|---|---|
| `4.2 规则比较、画像准备与批准` | §4.2 VC-2 与 §4.3 VC-3 |
| `4.3.2 入库与实现边界` | §4.4.1 |
| `4.4 候选验证与封存` | §4.4 VC-4 与 §4.5 VC-5 |
| `4.4.2 场景与第三方入口` | §4.5.2 |
| `4.4.3 四阶段封存` | §4.5.3 |
| `4.4.4 运行纪律与退出条件` | §4.5.1 与 §4.5.3 |
| `4.5 比较与验收` | §4.5 VC-5 |
| `4.5.2 逐规则机器断言` | §4.5.5 |
| `4.5.3 accept 前置与正式验收` | §4.5.6 |
| `4.6 生产启用与回滚` | §4.6 VC-6 |
| `第五部分 非版本变更维护` | 第五部分 Codex 兼容代码退休附加门禁 |

<a id="codex-0154-retired-entrypoints"></a>
## 4. 0.154 首轮事故链一次性入口的删除清单（2026-09-16）

0.154 首轮正式 VC-1 曾为一条具体事故链逐个增加 sequence 专用恢复入口。改造方案 B2～B6 在 reconciler
（`reconcile-supervisor-run`／`reconcile-attempt`）成为唯一对账入口后，把这些执行分支连同其硬编码锚点删除。
删除以带证明的 freeze successor（`passed_with_deletions`，逐路径无引用扫描与历史读取器）登记；历史 run、
attempt 与收据保持生成时原文，只读解释，不得用当前工具重放或改写。

| 已删除入口／文件 | 当时用途 | 删除理由 | 当前读取方式 |
|---|---|---|---|
| `codex_upgrade_vc1_permission_alias_predispatch_closeout.py` | sequence 4 在父 run 创建前被只读前检拒绝后的零请求封口 | 只服务一次事故，锚点为 15 个硬编码摘要；通用失败由 reconciler 对账 | 收据 `sequence4-predispatch-closeout-receipt.json` 只读；无历史 reader 需求 |
| `codex_upgrade_vc1_permission_alias_closeout.py` | 经宿主可写别名 `fchmod` 收口证据权限 | 已由通用 `harden-evidence-permissions` 两步式收口取代 | 收据 `sequence4/5-permission-alias-closeout-receipt.json` 只读 |
| 监督器 `VC1_PERMISSION_*` 37 个常量与三个专用后继验证器 | 逐字绑定 sequence 2～5 的失败父链 | 一次性锚点不可复用；失败父批次只允许普通零请求恢复预览或唯一 v3 | 历史 run 目录只读 |
| `compile-vc-interrupted-recovery-continuation`、`continue-vc1-interruption`、`campaign-run/v4`、`/v5` | 承接失败 v3／v4 的续接与收尾清单 | 只对两次确定性工具缺陷有效；v3 失败改为停线并由 reconciler 对账 | 历史清单与 transition 只读；`interrupted-recovery-transition.json` 由主编排器只读 loader 解释 |
| `finalize-vc1-deadline-orphan` 及 23 个 `_deadline_orphan_*` 执行 helper、VC-0 收尾的孤儿请求审计 | 原 deadline 到期后直接封口孤儿 attempt 并追加唯一停线事件 | 冻结了 `29/27/2/15/0/12` 与 `26+38=64` 等一次事故数量；deadline 到期由 reconciler 判定 `deadline_wall_clock` | `deadline_orphan_finalization` 字段、合同与审计由主编排器只读 loader 按结构自洽校验 |
| `repair-failure-closure`（指南 §4.0.4 段落） | 修复 VC-0 收口失败闭合的零请求计数缺陷 | 文档条款退役；代码入口只保留历史重放 | 审计目录只读 |

<a id="codex-0154-transitional-recovery-clauses"></a>
## 5. 0.154 首轮事故链的过渡恢复条款（只读，2026-09-16 自指南移入）

下列三段条款原在指南第四部分公共执行约定内，是 0.154.0 首轮正式 VC-1 事故链期间写入的过渡恢复规则。
改造方案 B0 的 reconciler 成为唯一对账入口后，它们不再是新 Campaign 的执行入口：`reconcile-attempt` 统一处理
reservation 之后的任何中断，真实补跑必须携带已批准的 `recovery-preview/v1`。对应代码入口
（`compile-vc-interrupted-recovery-batch`、`recover-vc1-interruption`、`codex-upgrade-campaign-run/v3`、
`_metadata_only_seal_repair_allowed`、`permanent-stop-*` 只读承接）按方案“保留 v3、不删 predispatch_stop”原文保留，
只服务历史回归与只读解析；退役时按 B1 带证明删除流程再登记一次 successor。以下为原文，不再维护。

### 5.1 metadata-only seal 例外与 `permanent-stop-*` 只读承接（原公共执行约定）

仅有一种 metadata-only seal 例外：Candidate 已进入 `awaiting_receipts`，全部 Candidate Job 均为
`reused/complete`，executed／failed／pending 集合为空，且尚未生成 evidence manifest、seal draft 或
seal preview；同时变化只能属于 `control／evaluator／orchestrator`。此时 `campaign-run` 才能登记
`metadata_only_seal_repair`，不得重发请求、创建旧 evaluation transition，或承接已有失败和已开始深度扫描
的 attempt。

若来源 Ledger 已因 `permanent-stop-*` 停线，只允许在冻结 checkpoint 仍为 active、停线后唯一新增事件使
`head_sequence` 恰好加一且 `live_request_count=0` 时只读承接。历史 control epoch 还必须满足 boundary
全零、仅因预算到期进入 `stop_required` 且没有同根因失败；其他 stopped／stop_required、多个新增事件、
非零边界或 live 请求全部失败关闭。该分支必须核验来源 attempt 的 `environment/after`、`after_probe`、
`probe-manifest.json` 和五份状态快照，以不可覆盖副本写入当前 attempt；不得重新探测环境、发送请求或改写
来源文件。

### 5.2 VC-1 `KeyboardInterrupt` 孤儿的单次恢复（原公共执行约定）

> 指南正文自 2026-09-22 起不再重复登记本条款的只读指针，统一指向本节。

只有同时满足下列条件才使用本分支：失败父批次是 v2 且终态为 `failed/KeyboardInterrupt`；官方 attempt
已有同一 reservation 和可重放 checkpoint，但没有 `attempt.json`；checkpoint 同时包含已完成项和
失败／待执行项；Campaign、目标产物、账号权限、模型可见性、环境语义及原始 deadline 未变化。工具变化
必须逐文件分类，评估／控制侧可离线承接；产出侧文件必须精确映射到 `failed ∪ pending`，不得触及
`complete`。

先直接编译一次性合同和 v3 清单：

```bash
python3 tools/official_client_capture/codex_upgrade.py \
  compile-vc-interrupted-recovery-batch \
  --campaign-dir /绝对路径/campaign \
  --sequence <失败批次序号+1> \
  --source-attempt /绝对路径/campaign/official/attempts/<attempt-id> \
  --failed-supervisor-run-dir /绝对路径/原state-dir/run-<owner-nonce> \
  --timing-ledger-dir /绝对路径/连续时间账本 \
  --deployment-receipt /绝对路径/当前ARM64工具部署收据
```

随后必须使用失败批次原来的 `state-dir` 立即执行生成的 v3 清单；改用空目录会因缺少唯一直接失败前序而
拒绝：

```bash
python3 tools/official_client_capture/codex_upgrade_supervisor.py campaign-run \
  --state-dir /绝对路径/原state-dir \
  --manifest /绝对路径/campaign/control/vc/run-manifests/<序号>-vc-1.json
```

v3 恰好包含一个 `recover-vc1-interruption` 动作。它只补齐原 attempt 的 after／ARM64 after／恢复收据，
写入不可变失败终态和专用 transition，再调用 `resume --rerun-failed --preview-recovery`；禁止携带
`--acknowledge-live-requests`。成功输出必须逐字证明 execute／reuse 集合与合同一致，并满足
`reservation_exists=false`、`live_request_count=0`、`scanned_bytes=0`。这一步不生成 VC-1 checkpoint，
也不表示 execute 项已经运行。

操作员确认预览后，按普通 action plan 编译下一序号的 VC-1 v2 批次，动作才可携带
`resume --rerun-failed --acknowledge-live-requests` 执行冻结的 execute 闭集；复用项继续只读承接。
合同、transition、源 attempt、失败 v2 和预览 v3 均只写追加，任一摘要、owner nonce、Ledger head、部署
工具或闭集漂移都停线，不得重编同一序号或新建 reservation 试探。

### 5.3 legacy 批次模型的预派发停线收据（原公共执行约定，2026-09-22 自指南移入）


以下条款只对总计划没有 `batch_model` 字段的历史 Campaign 有效：原子入口能捕获的失败会自动写收据；只有
进程被 `SIGKILL` 等不可捕获方式终止、且两个编译制品已经完整落盘而父 run 尚不存在时，才允许在原 Campaign
上补写一次。命令固定为：

```bash
CAMPAIGN_DIR=/绝对路径/campaign
STATE_DIR=/绝对路径/本轮Campaign-supervisor
BATCH_NAME=0002-vc-2.json

python3 -m tools.official_client_capture.codex_upgrade_predispatch_stop record \
  --campaign-dir "$CAMPAIGN_DIR" \
  --state-dir "$STATE_DIR" \
  --batch "$CAMPAIGN_DIR/control/vc/batches/$BATCH_NAME" \
  --manifest "$CAMPAIGN_DIR/control/vc/run-manifests/$BATCH_NAME" \
  --failure-kind operator-recovery \
  --error-type ProcessExit

python3 -m tools.official_client_capture.codex_upgrade_predispatch_stop replay \
  --campaign-dir "$CAMPAIGN_DIR" \
  --state-dir "$STATE_DIR" \
  --receipt "$CAMPAIGN_DIR/control/vc/predispatch-stops/$BATCH_NAME"
```

`BATCH_NAME` 的序号必须使用四位十进制，阶段使用小写形式。`record`
会非阻塞取得同一把 `.campaign-run.lock`，验证 Campaign plan、batch、manifest 和全部既有父 run；任何字段、
路径、摘要、历史或锁状态不一致均拒绝写入。补写和重放均不得运行 action，也不得恢复当前 Campaign。

`campaign-run` 必须向动作注入父 `run_dir`、Campaign 身份、owner nonce 和原始 deadline；动作内的
`codex_upgrade.py` 只能附加到该父监督器，不能再创建 `CampaignLease`、`.supervisor/run-*` 或重置计时。
清单分别声明 `execute_items` 和 `reuse_items`。普通执行／恢复批次的前者为空时，必须在 reservation 前
写入 `incremental-noop`，并以 `scanned_bytes=0`、`live_request_count=0` 退出；唯一不创建 reservation 的
情况是 `reuse-official-evidence` 引导出的首个 VC-1 no-op 批次，它由导入收据和 VC-1 checkpoint 直接证明
全部 official Job 已复用且请求、扫描、执行均为零。正式上下文在取得 lease 前拒绝 `successor`、
`control-epoch`、`evaluation-transition`、`terminal-transition-preflight` 和旧写入入口；0.151 formal 的
capture、classify、profile、compare、accept、resume 及 canonical 写命令没有父上下文时同样拒绝。

0.154.0 首轮事故链留下的两条过渡恢复条款（metadata-only seal 例外、`permanent-stop-*` 来源 Ledger 的
只读承接）已于 2026-09-16 移入[历史审计](CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md#codex-0154-transitional-recovery-clauses)，
只作只读解释，不是新 Campaign 的执行入口。

旧恢复机制及 Kilo 历史事实只按
[历史审计](CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md#codex-0151-historical-recovery)读取，不得成为新 Campaign
的前置条件。

<a id="codex-0154-release-certification-history"></a>
## 6. 0.154 工具改造期的发布认证沿革（只读，2026-09-23 自指南移入）

下列是 0.154 工具改造期间（改造阶段 A2.5～C3、工具身份策略 v5 之前）的认证形状，只用于读懂历史收据，
不是新 Campaign 的执行条件：

- 改造 C 阶段起，历史夹具回归、ARM64 实规模 Job 演练和 atomic-double 双跑不再逐项作为 P0 输入，改由发布认证
  在签发时重放合成。发布认证 `tool-release-certification/v1` 替换了 A2.6 的 `policy-activation-certification/v1`，
  成为 VC-0 收口、Formal `plan` 与 `reuse-official-evidence` 接受的工具就绪证明；策略激活认证此后只作为
  pre-A3 与发布认证的输入签发。
- C3 之前签发的历史 P0 收据（断言为 `job_rehearsal_sha256` 与 `campaign_run_rehearsal`，角色含
  `job_rehearsal` 与 `campaign_run_rehearsal`）只能由 `replay` 只读重放，用于加载策略 v5 之前创建的 0.154
  首轮 Formal Campaign 的冻结控制；`finalize` 不再签发该形状，策略 v5 起创建的 Formal Campaign 一律要求
  `release_certification` 绑定。
- 只有 storage 探针、不带 `failure_lifecycle_probe_sha256` 的历史 Job 演练收据仍可只读重放，但不得用于新
  Formal Campaign。
