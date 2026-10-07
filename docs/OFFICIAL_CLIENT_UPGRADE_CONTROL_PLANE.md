# 官方客户端升级控制面

> **适用范围**：Codex CLI 与 Claude Code 两个官方客户端升级共用的控制面实现。
>
> **权威关系**：机制合同以 [Framework](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) §5.3.4（身份边界）与 §5.3.6
> （共享控制面合同）为准；本文写这些机制在代码里的落点、接口、合同版本与修改规则；命令参数、场景、预算、
> 失败矩阵与续跑入口见 [Codex 指南](CODEX_CLI_CLIENT_EMULATION_GUIDE.md)与
> [Claude 指南](CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md)第四部分。同一条规则只在一处维护。

---

## 1 组成与边界

两个客户端共用一份控制流程，客户端差异只经适配接口提供。共享代码里没有 `if client ==` 分支，按客户端分派
只发生在适配接口的注册表里。

| 模块（未注明目录的在 `tools/official_client_capture/` 下） | 职责 |
|---|---|
| `upgrade_client_adapter.py` | 适配协议、注册表、编排器服务面清单 `ORCHESTRATOR_SURFACE` 与绑定表 `ADAPTER_BINDINGS` |
| `codex_upgrade_client_adapter.py` | Codex 的适配器成员取值，逐字等于改造前写死在共享代码里的名字 |
| `claude_upgrade_client_adapter.py` | Claude 的适配器成员取值与编排器门面 `ORCHESTRATOR` |
| `upgrade_control_plane.py` | 从 Codex 编排器迁入的共享控制流程（108 个函数） |
| `codex_upgrade.py` | Codex 编排器；迁出的函数原位留同名薄壳 |
| `claude_upgrade.py` | Claude 编排器；服务面全部成员同名同签名 |
| 监督器、对账器、项目总账、计时账本等 `codex_upgrade_*` 共享 control 模块 | 文件名保留历史前缀，客户端差异经适配器取 |
| `tools/official_client_control/` | Claude 的事实存储（Store）：对象、事实、收据与重放 |
| `tools/arm64_supervised_deploy.py`（受管树之外） | 受监督部署：多根事务、写前意图日志与按客户端签收据（第 9 节） |
| `tools/ci/` 与驱动 `tools/arm64_capture_driver/driver/` 的入口基础设施（受管树之外） | 入口编排、入口门禁、空跑与后台验证；客户端差异只在客户端表（第 10 节） |

三条硬约束：

1. 监督器与 Claude 编排器不导入 `codex_upgrade`；共享代码对编排器的引用全部经适配接口。
2. 客户端身份贯穿整条摘要链（第 3 节），任一环缺失或不一致即拒绝，不默认成 Codex。
3. 公共恢复算法只有一份：admission、批次编译与 staging、对账判定、恢复预览、评估基线、候选 revision 两个
   编排器调用同一份实现。例外是被 Codex 身份闭包计入的读取、路径与校验函数，由两个编排器各自实现，以合同
   测试对齐（第 13 节）。

## 2 适配接口与服务面

### 2.1 注册表与归属

- `adapter_for(client)`：取 `codex` 或 `claude` 的适配器，未知客户端即抛 `ClientAdapterError`。
- `client_of(campaign_dir)`／`client_of_manifest(manifest)`：读 Campaign 总计划顶层 `client`。缺字段时只有
  `codex-upgrade-campaign/v*` 隐含 `codex`，其余一律拒绝。
- `require_consistent_client(expected, sources)`：核对 Campaign 总计划、Store Campaign、项目总账 plan、计时账本
  plan 四处的 `client`；任一缺失或不等即拒绝。
- `root_cause_dimensions(adapter, codes, code, dimensions)`：码表登记了 `client` 维度时补上客户端名，再调用适配器
  的易变维度检查。Codex 码表不含 `client`，根因 ID 与改造前逐字相同。

### 2.2 编排器服务面

`ORCHESTRATOR_SURFACE` 是共享代码可以经 `adapter.orchestrator.<原名>` 访问的编排器符号与签名文本，共 254 项：
209 个可调用成员（含 6 个类）、42 个常量、3 个导入名（`ConfigurationError`、`file_sha256`、`__file__`）。清单由
`tests/orchestrator_surface_inventory.py` 从基线提交提取，`--check` 可随时复算。迁入共享模块的函数之间也经
`adapter.orchestrator.<原名>` 互调，所以被其它迁入函数调用的迁入函数同样在服务面内：Codex 测试对编排器符号
打的替身经薄壳照常生效。

两个客户端的绑定：

| 客户端 | `orchestrator` | 服务面成员落点 |
|---|---|---|
| codex | 按包路径导入的 `codex_upgrade` 模块本身 | `codex_upgrade` 的同名符号 |
| claude | `claude_upgrade_client_adapter.ORCHESTRATOR`（只暴露服务面名字的只读门面） | `claude_upgrade` 的同名同签名实现 |

### 2.3 适配器成员

服务面之外，共享代码里确有客户端差异的名字登记为适配器成员（`ADAPTER_MEMBERS`），每个客户端都必须绑定：

| 成员 | 种类 | Codex 取值 | Claude 取值 |
|---|---|---|---|
| `orchestrator_basenames` | 值 | `codex_upgrade.py`、`codex-upgrade` | `claude_upgrade.py`、`claude-upgrade` |
| `forbidden_subcommands` | 值 | 15 个控制面与引导子命令 | 同 15 个，加 `freeze`、`import-predecessor-evidence`、`environment-cleanup` |
| `evaluation_builder_basename` | 值 | 监督器 `EVALUATION_ACTION_BUILDER_BASENAME` | `claude_build_rule_assertion_results.py` |
| `target_version_field` | 值 | `codex_version` | `claude_version` |
| `root_cause_codes_path` | 值 | `root_cause_codes.json` | `claude_root_cause_codes.json` |
| `deploy_receipt_glob` | 值 | `codex-*-supervisor-enable-*.json` | `claude-managed-deploy-*.json` |
| `project_ledger_dir_name` | 值 | `upgrade-project-ledger` | `upgrade-project-ledger-claude` |
| `project_ledger_plan_schema` | 值 | `upgrade-project-ledger-plan/v1` | `upgrade-project-ledger-plan/v2` |
| `candidate_probe_accounting_category` | 值 | `candidate_readiness_models_probe/v1` | `claude_candidate_readiness_probe/v1` |
| `host_layout` | 模块 | `codex_upgrade_vc0_closeout` | 同左（两客户端共用 ARM64 数据根与 Campaign 布局） |
| `provenance` | 模块 | `codex_upgrade_live_request_provenance` | `claude_upgrade_live_request_provenance` |
| `identity_policy` | 模块 | `codex_upgrade_tool_identity_policy` | `claude_tool_identity_policy` |
| `reject_volatile_dimensions` | 函数 | 无附加检查 | 拒绝含 Claude Campaign ID 的维度值 |
| `recovery_segment_summary`、`segment_reuse_proofs` | 函数 | 对账器原函数 | `claude_upgrade` 同签名成员；VC-1 官方路径不经过，候选恢复段调用即报待实现 |
| `requires_project_ledger` | 函数 | 0.154 起的 Formal Campaign 在项目总账内 | Formal Campaign 一律在项目总账内 |
| `attempt_environment_restored` | 函数 | after 探针加恢复收据 | after 探针在位，两个采集容器前后探针零漂移 |
| `deploy_receipt_identity` | 函数 | 部署收据顶层 | 部署收据的 `identity` 子键 |
| `effective_wire_identity` | 函数 | `codex_upgrade_wire_transition`：wire transition 与工具演进两条链 | `claude_upgrade_evolution`：只有工具演进链 |
| `job_rehearsal_profile` | 模块 | `codex_upgrade_job_rehearsal_receipt` | `claude_upgrade_job_rehearsal` |
| `build_job_contract`、`binary_facts`、`job_contract_replay` | 函数 | 委托原执行合同构造器与二进制核对 | Claude 执行合同 `official-client-job-rehearsal-contract/v1` 的构造、二进制表与重放 |
| `closeout_profile` | 模块 | `codex_upgrade_vc0_closeout` | `claude_upgrade_vc0_closeout` |

子命令名与阶段名两客户端相同（Claude 子命令与 Codex 同名），共享代码按字面书写，不经适配器。动作 ID 由各自的
动作计划给出：Claude 的 VC-1 封存批次用 `vc1-01-analyze-target`、`vc1-02-seal-preview`、`vc1-03-seal-approve`，不用
Codex 的 `seal-official-*`（监督器按那两个名字执行 Codex 的断言证据包门禁）。

### 2.4 访问规则

- 一律写字面形式 `adapter.orchestrator.<原名>`、`adapter.<成员>`。把 `orchestrator` 赋给变量、作为参数传递或用
  `getattr` 取成员，身份引擎无法解析，计算即失败。
- 适配器成员每次访问按绑定现取模块属性，不在构造时缓存；对原模块的 `mock.patch` 经适配器照常生效。
- 共享模块导入适配接口用包路径（`from tools.official_client_capture import …` 或 `from . import …`），不写扁平
  `import` 语句：作业依赖分析会把扁平 `import` 计入 Job 工具依赖。
- 共享代码要捕获的异常经模块型成员取（`adapter.identity_policy`、`adapter.host_layout`、`adapter.provenance`），
  使被调函数与被捕获的异常类来自同一份模块。

### 2.5 恢复上下文

恢复接口统一接收 `RecoveryContext`，失败源身份全部显式给出，不从当前默认值取：

| 字段 | 含义 |
|---|---|
| `campaign_dir`、`phase`、`source_attempt_id` | 失败源所在 Campaign、阶段与 attempt |
| `candidate` | `CandidateCoordinates`：从失败源 attempt 或 reservation 读出的候选坐标（候选 ID、运行镜像、画像、构建、部署版本、镜像 ID、源码树摘要、候选用途） |
| `manifest`、`attempt_root` | 工具演进影响与连续性预览需要；复用核对不需要 |
| `output_dir` | 连续性预览的探针输出目录 |

适配器提供三个恢复接口：`recovery_reuse_check`、`recovery_preview_continuity`、`attempt_evolution_impact`，
分别以字面 `self.orchestrator.<名字>` 转调编排器实现。

## 3 客户端身份贯穿链与合同版本

### 3.1 身份根

`client` 只显式写在以下身份根对象里，其余制品经不可变摘要链绑定到它们：

```text
Campaign 总计划 client → 项目总账 plan client（进入 plan 自摘要）→ 计时账本 plan client
→ 适配器注册表按 client 取实现并核对四处一致 → 身份策略文件 client（进入 policy_sha256）
→ 部署收据与发布认证 client
```

Codex 历史制品没有 `client` 时按 `codex` 只读解释，不补写。新合同只由 Claude 使用，Codex 继续用现行合同
新建与读取。

### 3.2 合同版本

| 对象 | Codex（现行，不变） | Claude |
|---|---|---|
| 父监督器运行清单 | `codex-upgrade-campaign-run/v1`～`v3` | `official-client-campaign-run/v1`（v2 结构加 `client`） |
| Campaign 总计划 | `codex-upgrade-campaign/v3` | `official-client-upgrade-campaign/v1` |
| 项目总账 plan | `upgrade-project-ledger-plan/v1` | `upgrade-project-ledger-plan/v2`（加 `client`，码表按客户端） |
| 计时账本 plan | `codex-upgrade-timing-ledger-plan/v1` | `upgrade-timing-ledger-plan/v2`（加 `client`） |
| 工具身份策略 | `tool-identity-policy/v2`（`policy_version` 8） | `tool-identity-policy/v3`（`policy_version` 2） |
| 环境收据 | `codex-upgrade-arm64-environment-receipt/v1` | `official-client-arm64-environment-receipt/v1`（含 `client`、`client_binary`） |
| 受管部署收据 | `codex-arm64-supervisor-enable/v1`（22 字段闭集） | `official-client-managed-deploy/v1`（工具身份在 `identity` 下） |
| 部署事务日志 | `managed-deploy-journal/v1`（一次事务一份，两客户端共用） | 同左 |
| 驱动安装收据 | `arm64-capture-driver-install/v1`（只有一个客户端纳管时） | 两客户端都纳管时 `…/v2`（`deployment_receipts` 按客户端绑定） |
| 策略兼容收据、策略激活认证 | `policy-compatibility-receipt/v1`、`policy-activation-certification/v1` | 同左（不加 `client`，身份按 Claude v3 引擎算；客户端由策略摘要与绑定的部署收据区分） |
| pre-A3 路径认证 | `pre-a3-path-certification/v2`（v1 只读重放） | `…/v3`（v2 结构加 `client`） |
| 发布认证 | `tool-release-certification/v1` | `…/v2`（v1 字段加 `client`） |
| 入口步骤记录、单元执行记录与运行清单、逐单元承接、后台验证与入口空跑结论、P0 收据与离线门禁证据 | `entry-step-record/v1`、`unit-execution-record/v1`、`unit-execution-manifest/v1`、`unit-inherit-request/v1`／`-context/v1`／`-decision/v1`、`background-validation/v1`、`entry-dryrun/v1`、`codex-upgrade-vc-receipt/v1`、`codex-p0-offline-gate-evidence/v1`／`v2` | 同左（客户端无关，或经计时账本 subject 与部署收据绑定） |
| 官方产物冻结 | `codex-upgrade-official-asset-receipt/v1` | `claude-code-fw-e-official-freeze/v2`（npm 与原生分发双来源；v1 只读） |
| VC-0 收口收据组（收口收据、失败诊断、现场、上限后恢复预览、首批交接） | `codex-upgrade-vc0-closeout-receipt/v1` 等 | `official-client-vc0-closeout-*/v1` 同组 |
| Job 演练收据组（facts、收据、执行合同、采集器） | `codex-upgrade-job-rehearsal-*`（执行合同 v2） | `official-client-job-rehearsal-*/v1` 同组 |
| 失败生命周期探针 | `codex-upgrade-failure-lifecycle-probe/v1` | `official-client-failure-lifecycle-probe/v1` |
| 编排器候选构建收据 | `codex-upgrade-candidate-build-receipt/v2` | `official-client-upgrade-candidate-build-receipt/v1`（带 `client`） |
| 候选 revision seal、被取代失效标记 | `codex-upgrade-candidate-revision-seal/v1`、`codex-upgrade-candidate-superseded/v1` | seal 同左；失效标记 `claude-upgrade-candidate-superseded/v1` |
| 请求账务 | `live-request-provenance/v2` | 同左（计量单位不同，见 Claude 指南） |
| 根因码表 | `root_cause_codes.json` | `claude_root_cause_codes.json`（同 schema，维度含 `client`） |
| 恢复预约、恢复段摘要、连续性漂移收据 | `codex-upgrade-*` | `claude-upgrade-*` |

Claude 的 Store 合同（Codex 不涉及）：

| 对象 | 旧版（只读重放） | 新版 |
|---|---|---|
| Store Campaign | `official-client-control-campaign/v1` | `…/v2`：加客户端、用途、账号组织、模型目录、执行权限、阶段依赖与预算、绝对截止、监督参数、项目总账引用与七项工具身份 |
| Store fact | `official-client-control-fact/v1` | `…/v2`：加 `idempotency_key`、`producer_run`、`formed_under` |
| Store 工具摘要 | `official-client-control-tool-identity/v1` | `…/v2` |
| 五类收据（晋升、激活、候选构建、验收门禁、候选交付） | `…/v1` | `…/v2`：加形成期坐标 |
| 新对象与查询 | — | `tool_evolution_registration`、`predecessor_import`、候选作废对象 `candidate_invalidation`（两步批准：`candidate-invalidate --preview`／`--approve-sha256`）；新事实 `candidate_revision_recorded`；新收据 `predecessor_import`（`official-client-predecessor-import-receipt/v1`）；`fact-lookup` 结果 `official-client-control-fact-lookup/v1` |

## 4 共享控制流程与两个编排器

### 4.1 共享控制流程

`upgrade_control_plane.py` 收录从 Codex 编排器迁入的 108 个函数：admission 与写入守卫、批次编译与 staging、
恢复预览与恢复段、评估基线、候选 revision、tool-evolution 与隔离、对账与入账命令壳。迁入函数签名逐字不变，
只在 `adapter_scope(adapter)` 作用域内执行，经 `_adapter()` 取当前适配器；嵌套作用域只能是同一客户端，否则
抛错。函数体里的客户端专属字面量经适配器成员或服务面常量取。

### 4.2 Codex 编排器

`codex_upgrade.py` 只新增两行导入、108 个薄壳与随迁常量的同名别名。薄壳形如：

```python
def compile_and_run_vc_batch(arguments):
    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("codex")):
        return upgrade_control_plane.compile_and_run_vc_batch(arguments)
```

11 个可迁函数留在原位，由 Claude 编排器各自实现：`recovery_reuse_check`（被测试按源码钉住调用顺序）、
`_campaign_stage_replay_facts`、`_reject_unparented_formal_write`、`_reject_campaign_run_legacy_write`、
`_seal_candidate_revision`、`_tool_evolution_preview`、`_policy_only_change_paths`、`_policy_evolution_transition`、
`_evaluator_entry_digests`、`_candidate_identity_snapshot`、`_derive_b0_evaluation_run`（函数体依赖 Codex 专属的
模块路径、文件名、历史版本、候选失效标记、v2 策略布局、构建参数或验收模式常量）。

### 4.3 Claude 编排器

`claude_upgrade.py` 以同名同签名提供服务面全部成员，不导入 `codex_upgrade`。函数成员分四档：

| 档位 | 含义 |
|---|---|
| 迁入薄壳 | 在 Claude 适配器作用域里转调 `upgrade_control_plane` |
| 照搬 | 两客户端语义相同的通用函数（读写、摘要、路径、租约读取、证据根取代归档与冲突隔离读取、阶段路径等），逐字取 Codex 实现，由 `tests/test_orchestrator_surface_contract.py` 的照搬清单锁定逐字一致；先照搬后因 Claude 语义分叉的成员另行登记 |
| 自实现 | Claude 语义不同的成员，实现在 `claude_upgrade_*` 子模块：总计划读取与建立、取证 attempt、封存、VC-1 恢复、工具演进、阶段结果与批次、分类、画像、候选 revision 与候选构建等 |
| 待实现 | 调用即报 `ClaudeOrchestratorPending` 并写明原因，不伪造行为；集中在候选采集与候选封存、评估基线与评估恢复、候选恢复段 |

另有 6 个类（`CampaignLease`、`Job` 与四个异常类）与 Codex 同名同语义。命令行子命令与 Codex 同名：13 个共享流程
子命令照搬 Codex 的参数定义并在 Claude 适配器作用域里转调共享实现，对账器同时进入 Claude 作用域；其中
`revision-open`、`invalidate-candidate` 另写 Store 侧登记（第 12 节）。`tool-evolution` 参数照搬、执行是 Claude 自己的
演进链（第 11.4 节）。Claude 专属子命令（`plan`、`freeze`、`import-predecessor-evidence`、`capture-official`、
`analyze-target`、`classify`、`prepare-profile`、`stage-profile`、`plan-candidate-gates`、`record-candidate-build`、对账与
恢复类）各有实现模块；`capture-candidate`、`compare`、`accept`、`account-sealed-candidate`、`deliver-candidate`、
`delivery-stage-invalidate` 执行即报未实现。两个编排器的行为一致性由 `tests/test_orchestrator_surface_contract.py` 对两个
适配器各跑同一组用例保证。

## 5 两层事务、崩溃分类与锁顺序

### 5.1 批次派发事务

由父监督器实现，两客户端共用且合同不变：父 run `prepared` → 写 `campaign-run-manifest.json` → 账本事件 →
发布 → COMMIT → 激活 → 才执行动作。只有完整 COMMIT 占用序号；中断边界与续派规则见 Framework §5.3.6。

### 5.2 动作结果 WAL

动作体写入 Store 时构成第二层事务，顺序固定为：

1. Store 追加事实，带幂等键：`contracts.fact_idempotency_key(logical_action, input_identity_sha256, fact_kind)`，
   即 `sha256(canonical({logical_action, input_identity_sha256, fact_kind}))`；`logical_action` 是跨 run 稳定的逻辑
   动作名；
2. 账本记账，payload 绑定事实引用；
3. 动作输出绑定（`action-outputs/<id>.json` 记录事实引用）。

Store 侧的实现：

- `append_fact_outcome` 在维度锁内先核对当前代码与有效工具身份（未登记演进时重试同样被拒），再按幂等键查本
  Campaign 全部事实：同键、同种类、同 payload 返回原事实引用并标 `reused: true`，不新增事实；同键异内容拒绝；
  无键照常追加。只比种类与 payload，不比 `formed_under`、签发时间与 `producer_run`，所以登记演进后或在重派的
  新 run 里重试仍是同一次逻辑写入，原事实保留自己的形成期坐标与原 `producer_run`。重放复核幂等键在 Campaign
  内唯一。
- `append_fact`、`seal_object`、`write_receipt` 照旧返回纯引用；需要知道是否复用时用对应的 `*_outcome` 接口。
  `seal_object`、`write_receipt`、`create_campaign` 加 `allow_existing=True` 时，同内容已存在即返回原结果，内容
  不同仍拒绝。
- `fact-lookup --store --campaign --idempotency-key` 输出 `official-client-control-fact-lookup/v1`：`found`、
  事实引用、种类、payload 摘要、签发时间、`formed_under` 与 `producer_run`，查不到时这些字段为 null。v1 Campaign、
  非法键或同键多条事实失败关闭，不把「查不到」当成「动作未执行」。
- `producer_run` 为 `{run_dir, owner_nonce, action_id}`：`run_dir` 是规范 POSIX 路径，后两项是安全标识符。
- `write_once` 写完文件后 fsync 父目录与本次新建目录的父目录，保证「Store 已写」在崩溃后仍可查到。
- 同一 Deployment 的 `selector_activated` 只能有一条；`selector_observed` 可以重复（同一快照的前后两次观察都是
  证据），重试由幂等键去重。

| 中断位置 | 分类与处理 |
|---|---|
| ① 之前 | 动作未执行，按批次重派 |
| ①② 之间 | Store 已写、账本未记：`reconcile-supervisor-run` 按幂等键查到事实后补账 |
| ②③ 之间 | 账本已记、输出绑定未写：按无复用依据（`reuse_authority=none`）处理 |

批次级标记（PREPARED／COMMIT）与动作级标记（`action-outputs`）不混用。目前 Store 侧的查找与复用接口已实现，
对账器按幂等键补账的接入尚未完成。

### 5.3 锁顺序

项目总账锁 → Campaign 账本锁 → Store 维度锁 → Store 对象锁。持 Store 锁时不得再取账本锁。

### 5.4 提交中崩溃的收敛（P1～P4）

中断边界与续派规则见 Framework §5.3.6。共享批次流程 `upgrade_control_plane.compile_and_run_vc_batch` 在
`control/vc/staging/NNNN-vc-x/attempt-K` 里准备批次，四个中断点两客户端同一份处理：

| 中断点 | 收敛方式 |
|---|---|
| P1：prepare 之后、父 run 之前失败 | 写 ABORT，入账一次根因，序号不占；同序号新 staging 重派 |
| P2：父 run 已 prepared、owner 在 COMMIT 之前被强杀 | monitor 封存 `aborted_prepared`；下次入口对账、ABORT，同序号重派 |
| P3：COMMIT 的发布步失败（账本事件已写、COMMIT 未写） | 对账、ABORT，归档已发布的半产物，同序号重派 |
| P4：COMMIT 已写、父 run 激活失败 | 账本进入 `recovery_required`，对账后以下一序号重派同一批次身份 |

两个中间态同样收敛：P1 链 ABORT 已写而总账未推送、P3 链对账收据已写而 outbox 未提交，下次入口都会闭合，不重复入账。

- **对账器作用域**：共享控制流程直接调用对账器的内部函数（staging 中止根因、总账推送、判定、身份事实等），它们按
  对账器自己的作用域取适配器。`upgrade_control_plane.adapter_scope` 进入时同时设定对账器作用域：同客户端嵌套照旧，
  跨客户端拒绝，对账器作用域已由公开入口设为另一客户端时同样拒绝。Codex 设定的就是对账器的缺省值，行为不变。
- **重派之后的同阶段后继**：只有正要派发的直接后继按实物复算，已执行过的历史后继只重放冻结许可；一个阶段失败、对账、
  重派成功之后，同一阶段的后续批次不会被误判为漂移。
- **Claude 的阶段幂等重派合同**在 `claude_upgrade_batch.STAGE_REPLAY_CONTRACTS`：VC-2 `classify`，VC-3 `prepare-profile`
  与 `stage-profile`，VC-4 `plan-candidate-gates` 与 `record-candidate-build`；VC-1 没有阶段重派合同。合同只认受管 Python
  对 `claude_upgrade.py` 的直接调用、旗标精确闭合；许可写进 `codex-upgrade-stage-replay/v1` 证明，监督器派发直接后继
  前再算一次、逐字相同才放行。VC-4 的判定交给共享的 `_candidate_stage_replay_action_facts`，Claude 在其上补 Store 侧：
  Store 已冻结本候选的另一次构建即拒绝。
- **动作结果 WAL 的 Claude 落点**：VC-2、VC-3、VC-4 的 Store 写入都带幂等键（第 5.2 节）。写入前崩溃时 N+1 重派写下唯一
  一条事实（`producer_run` 是重派的 run）；写入后崩溃时 N+1 按幂等键复用原事实（`producer_run` 仍是写下它的 run），
  事实数不增。

## 6 形成期重放与有效工具身份

- **有效工具身份**：Campaign 初始身份加最近一次 `tool_evolution_registration` 的 `to_identity`。登记 payload 为
  `{client, campaign_id, index, from_identity, to_identity, evolution_receipt{path, sha256}, approved_by,
  approved_at_utc, previous_ref}`，按 `previous_ref` 成链。
- **形成期坐标**：v2 事实与五类 v2 收据记录 `formed_under{tool_identity_ref, candidate_revision,
  evaluation_baseline}`。`tool_identity_ref` 为空表示 Campaign 初始身份，否则引用一个演进登记并取其
  `to_identity`。v1 事实没有该字段，按 Campaign `tool_bundle_sha256`、revision 1、无评估基线解释。
- **两类校验分开**：写入时必须匹配当前有效身份、当前 revision 与当前基线，不匹配即拒绝并提示先执行
  `tool-evolution`；重放（`replay`、`replay-receipt`、Store 整体重放）按每条事实或收据自己的 `formed_under`
  重跑门禁与重建收据，只证明形成时一致，不改变 `status` 对当前有效性的判定。
- **交付判定**：`status`、候选交付与晋升门禁只认当前 revision、当前基线下形成的 Acceptance；revision 切换后
  旧 revision 下的结果仍可重放，但不再算当前验收。
- **工具摘要口径**：v2 下各处工具摘要字段一律取形成期身份的 `tool_files_sha256`（覆盖 Store 全部文件）；
  `tool_bundle_sha256` 保留为 Store 自身摘要。Store Campaign v2 的 `tool_identity.tool_files_sha256` 与编排器
  总计划的 `tool_identity.files_sha256` 取同一指纹，键名不同、按值比对。
- **写入时复核**：`campaign-create` 用 Claude 策略现算受管清单，核对整树指纹与 Store 自身摘要；建 Campaign v2
  与登记工具演进时，七项身份全部与 v3 引擎现算值核对。

## 7 前序证据导入与消费端核验

Claude 的新 Campaign 可以引用前序 Store 的官方证据，但只能经「登记 → 签发收据」两步获得授权。编排器入口是
`claude_upgrade.py import-predecessor-evidence --mode read_only_reuse|regression_fixture`（`claude_upgrade_predecessor`，
Campaign 锁内、静止时执行）：两种模式共用一条链，选取前序最新一条 `evidence_recorded` 引用的 EvidencePackage 及其对象
闭包；两组七项声明一律从双方 Store Campaign 的冻结字段导出，编排器不替申请方声明；同内容的登记沿用，一份登记只签一份
收据。`read_only_reuse` 只在 Formal Campaign、且总计划 `predecessor` 冻结了该前序 Campaign 时允许，签发后供 VC-1 零请求
封存（第 11.3 节）。


- **登记**（`predecessor-import-register`，对象 `predecessor_import`）：payload 记录目标 Campaign、前序 Store 根
  （绝对路径，只作形成期出处）与前序 Campaign 身份摘要、被导入的对象与事实引用、申请方声明的两组七项身份
  （`target_version`、`official_artifacts`、`platform`、`entrypoint`、`privacy_mode`、`oauth_org_id`、
  `model_catalog_sha256`）、模式（`read_only_reuse` 或 `regression_fixture`）与批准信息。登记时以只读方式打开前序
  Store（任何写入都被拒），核对前序 Campaign 身份与两组声明，导入清单必须对对象引用闭合，然后按依赖顺序把对象
  逐字节复制进本 Store。绑定前序 Campaign 控制状态、或引用事实与收据的对象不可导入。登记本身不授予引用权限。
- **收据**（`predecessor-import-finalize`，合同 `official-client-predecessor-import-receipt/v1`）：从源与目标
  Campaign 的冻结字段独立导出七项——`platform`、`entrypoint` 取 `platforms`、`entrypoints` 唯一的一项，
  `privacy_mode` 取 `default_conditions` 里唯一的 `privacy=` 条件，`official_artifacts` 取官方产物绑定清单（含
  路径）的规范摘要，v1 源没有账号组织与模型目录，记为不可证。声明必须逐项等于导出值（不可证项只能声明
  `null`），任一不符即拒绝。`read_only_reuse` 另要求七项全部可证且源与目标逐项相等；`regression_fixture` 把差异
  与不可证项写入 `identity_differences`。收据自带源 Campaign 身份文档与导入事实记录，重放只用本 Store 内的内容，
  前序 Store 搬走或删除都不影响重放；一份登记只签一份收据，重复 finalize 返回原收据。
  `predecessor-import-verify` 对照前序 Store 现状复核，前序 Store 搬家后可另给位置。
- **消费端核验**：跨 Campaign 引用必须同时满足：① 引用登记在本 Campaign 某个 `predecessor_import` 里；② 该登记
  有导入收据且收据重放通过；③ 收据模式为 `read_only_reuse`；④ 被引用者是官方证据白名单内的对象类型
  （`evidence_package`、`capture_index`、`sink_inventory`、`discovery_inventory`、`cross_source_matrix`、
  `runtime_host_receipt`、`rule_migration_ledger`、`atomic_assertion_ledger`）。事实与收据不在白名单内，跨
  Campaign 引用永不获授权。EvidencePackage 没有所属 Campaign 字段，被本 Campaign 导入登记列出的包一律按这四项
  判定；登记前已被本 Campaign 事实当本地证据引用的包不得再登记为导入。`regression_fixture` 收据只供测试与回放
  读取；这类对象和尚未 finalize 的导入对象，其外部证据不计入本 Store 的外部核验。v1 Campaign 不接受跨 Campaign
  引用，判定与报错与改造前相同。

## 8 工具身份：多根清单与 v3 引擎

### 8.1 两份策略

| | Codex | Claude |
|---|---|---|
| 策略文件 | `tool_identity_policy_v2.json` | `claude_tool_identity_policy_v2.json` |
| schema／版本 | `tool-identity-policy/v2`，`policy_version` 8 | `tool-identity-policy/v3`，`client: claude`，`policy_version` 2 |
| 根 | `tools/official_client_capture` | `tools/official_client_capture`、`tools/official_client_control` |
| 条目键 | 根内相对路径 | `<根目录名>/<相对路径>`（如 `official_client_control/store.py`） |
| 后缀 | `.py`、`.sh`、`.json` | 另加 `.mjs` |
| 引擎 | `codex_upgrade_tool_identity_policy.py` | `claude_tool_identity_policy.py`（v3） |

Codex 策略 v8 只在 control 层登记三个共享新文件（`upgrade_client_adapter.py`、`codex_upgrade_client_adapter.py`、
`upgrade_control_plane.py`）。Claude 策略把 Codex 专属文件逐项列入 53 项 `ignored_prefixes`，未归层文件数必须为
零；`runtime_bindings` 登记两个受管运行时——`node`（`runtime/claude-tooling/node`）与 `typescript`
（`runtime/claude-tooling/typescript/lib/typescript.js`）的路径、版本与摘要，进入 `control_sha256`。两客户端共用的
文件在两份策略里同层，任一方修改都会同时改变双方身份，修改前按双方策略评估。

### 8.2 v3 引擎规则

- 编排器闭包与评估读侧闭包都跨模块按符号计入：control 层模块按符号，与根同层的模块整文件；所以
  `upgrade_control_plane.py` 里被 Claude 证据根可达的函数进入 Claude 的 evidence 闭包。
- 经适配器的间接调用可见：`<表达式>.orchestrator.<名字>`、`<表达式>.<适配器成员>` 与模块型成员后接属性，按
  `ADAPTER_BINDINGS` 解析到 Claude 的具体符号继续遍历；`ClientAdapter` 的恢复接口以字面
  `self.orchestrator.<名字>` 转调，遍历时一并计入。名字不在绑定表，或 `orchestrator` 被赋值、传递、`getattr`，
  判为无法解析并失败。
- 闭包到达被策略忽略的文件即失败：Claude 闭包实际依赖的 Codex 专属实现不能在身份里不可见。这条规则同时保证
  从 Claude 入口静态可达的函数不引用 `codex_upgrade`。
- wire 与 evidence 层整文件计入，这两层模块的导入不得落到被忽略的文件。
- 跨进程身份记忆化与 Codex 共用同一缓存目录，键含策略、算法与适配接口源码摘要，两客户端不会串用。

## 9 受监督部署与发布认证

### 9.1 部署事务

受管工具与活动文档只由 `tools/arm64_supervised_deploy.py` 在 ARM64 上以 root 部署；外部命令都经独立监督器执行，
不执行官方请求。一次部署是一个多根事务：

- `--managed-roots` 选择要交换的受管根（相对数据根，缺省只有 `tools/official_client_capture`）。交换
  `tools/official_client_capture` 影响两个客户端，交换 `tools/official_client_control` 只影响 Claude。
- `--client` 选择要签部署收据的客户端（可多值，缺省 codex）。签收据的客户端必须受本次交换影响，每个被交换的根至少
  有一个受它影响的客户端签收据；已纳管（control 目录里已有其部署收据）的受影响客户端必须一并签收据，否则它的最新
  收据会描述已被替换的树。尚未纳管的受影响客户端只记进意图。
- 签收据客户端的活动文档取并集：Codex 是 Framework 与 Codex 指南（`MANAGED_DOCUMENTS`），Claude 是 Claude 指南与
  本文（`CLAUDE_MANAGED_DOCUMENTS`），各自另有运行时依赖文档；暂存树的 `docs/repository-docs/` 放这些文档的归档副本。
  两组不合并：VC-0 收口要求 Codex 收据登记的切换文档恰好是 Codex 那两份。
- 客户端身份覆盖的受管根（Codex 一根，Claude 两根）在部署后都必须等于暂存树的同名根；未交换的根要求生产现状已与
  暂存树一致，收据才能如实描述生产身份。
- 同一 control 目录同时只允许一个部署进程（`managed-deploy.lock`，flock）。

### 9.2 写前意图日志与按内容恢复

正常顺序：准备全部根候选并预检 → 写意图 → 逐根切入 → 全局一致性核验 → 收据正文入日志 → 写收据 → 删日志。

意图日志 `control/managed-deploy-<stamp>.journal` 在交换任何对象之前写入完整意图并 fsync（文件与父目录）。意图有根、
断言预处理器、文档三组条目，每条记 `production_path`、`candidate_path`、`backup_path`、`old_exists`、`old_tree_sha256`、
`new_tree_sha256`；写入后不再修改，之后只追加阶段标记 `switch_begin`、`switch_done`、`verified`、`receipts_prepared`、
`rollback_begin`、`rollback_done`，其中只有 `receipts_prepared` 参与恢复判断。目录的新旧按完整内容摘要判定（含测试
目录，不计字节码与权限）。原路径存在时原子交换后把旧树移入备份；原路径不存在时用不覆盖的 rename 切入。

部署启动时（锁内）发现日志即按生产路径的实际内容恢复，不依据“进行到哪一步”：

| 前态 | 生产路径现状 | 判定 | 回滚动作 |
|---|---|---|---|
| 原路径存在 | 摘要等于旧版本 | 未交换或已回滚 | 无 |
| 原路径存在 | 摘要等于新版本 | 已交换 | 候选区或备份位置的摘要等于旧版本就与它换回；两处都找不到即失败关闭 |
| 原路径不存在 | 不存在 | 未切入或已回滚 | 无 |
| 原路径不存在 | 摘要等于新版本 | 已切入 | 把新产物移回候选区，恢复为不存在；候选区已被占用即失败关闭 |
| 任一 | 其它 | 无法判定 | 失败关闭，现场不动，交人工 |

三种残留：有日志、没有 `receipts_prepared`——全部对象恢复到部署前，日志改名为 `.rolled-back` 留证，拒绝本次部署；
有日志、有 `receipts_prepared`——核对每根等于新版本，按日志里的收据正文补齐缺失收据、核对已有收据，一致后删日志，
不回滚；没有日志——正常。重复恢复只会落到“无”的行，回滚因此幂等。日志存在期间驱动 `install.py verify` 与
`guard.sh` 拒绝派发。

### 9.3 按客户端签收据

| | Codex | Claude |
|---|---|---|
| 合同与文件名 | `codex-arm64-supervisor-enable/v1`，`codex-01600-supervisor-enable-<stamp>.json` | `official-client-managed-deploy/v1`，`claude-managed-deploy-<stamp>.json` |
| 工具身份 | 收据顶层的策略版本与五摘要，单根、`.py`／`.sh`／`.json` 三后缀口径 | `identity{policy_version, policy_sha256, wire_producer_sha256, evidence_semantics_sha256, control_sha256, tool_files_sha256}`，由 v3 引擎按 Claude 策略算两根（含 `.mjs`） |
| 其余字段 | 22 字段闭集不变 | `client`、`managed_roots[{root, tool_files_sha256, rollback_backup}]`、`runtime_bindings`、`switch_journal_sha256`（意图摘要）、`documents`（Claude 文档的生效摘要与回滚点）、`client_binaries`、`pre_a3`、`transaction_receipts`、`notes` 等 |

签收据顺序固定 Codex 在前：Claude 收据的 `transaction_receipts` 记录同一事务签出的其它客户端收据，消费方据此认定
两份收据属于同一次部署。收据只写一次。Claude 身份在部署时用暂存树里的 v3 引擎与策略算出预期值，切换后用生产树
复算，两者必须逐字相等。部署前核对 Claude 的 pre-A3 场景表与真实链入口随树部署（记入 `pre_a3`）；目标客户端冒烟
探针没有登记目标场景清单时如实记录跳过原因（`client_binaries`），不伪造结果。

### 9.4 部署收据的消费方

消费方都按客户端选最新收据：文件名模式与时间戳前的分隔串按客户端取，Claude 收据的五摘要在 `identity` 下。

| 消费方 | 用法 |
|---|---|
| 驱动 `install.py` | 安装收据绑定全部已纳管客户端各自最新的部署收据；`verify --client` 取该客户端最新收据判定 |
| 入口步骤 `entry_steps.py` 与入口门禁 | 按本轮 `CLIENT` 取收据与五摘要；部署一致性按客户端核对受管根、指南与部署脚本副本；`deploy-check` 核对其它已纳管客户端的部署也是同一测试树 |
| 后台验证 | 两客户端都纳管时只跑一次全量门禁，结论绑定部署收据集合 |
| 修复轮 | 部署范围按纳管情况取；比对前把 Claude 收据的 `identity` 投影到顶层 |
| 共享对账器 | 经适配器成员 `deploy_receipt_glob`、`deploy_receipt_identity` 读当前部署身份 |
| VC-0 收口、发布认证、策略激活 | 核对收据的客户端与五摘要等于当前工具身份 |

### 9.5 认证链与真实链

认证链：策略兼容（策略版本变化时）→ 当前部署的策略激活 → pre-A3 → 发布认证 issue／verify。三个认证模块
（`codex_upgrade_policy_certification`、`codex_upgrade_pre_a3_certification`、`certify_release`）都带可选 `--client`，缺省
codex，Codex 的命令行、合同与内容逐字不变；对编排器的引用（指纹、文件摘要、工具身份）经适配器取。

- 场景表与真实链按客户端登记（`SCENARIOS_BY_CLIENT`、`REAL_CHAIN_IDS_BY_CLIENT`，纯字面量，部署脚本只做静态求值）；
  一份 pre-A3 场景清单只属一个客户端。场景单元 ID：Codex 为 `pre-a3:<场景>`，Claude 为 `pre-a3:claude:<场景>`，同一
  记录库里互不承接。Claude 场景集合为空时规划、签发与核验一律拒绝；Claude 只从单元执行记录签发 v3。
- Claude 登记四条真实链（`tests/real_chains/test_claude_upgrade_control_chain.py`）：`vc-chain.claude-reconcile`、
  `vc-chain.claude-deadline`、`vc-chain.claude-root-cause`、`vc-chain.claude-tool-evolution`。四条链零请求，在两个受管根
  的副本树上运行；Linux root 上必须进入 `unshare -m -n` 私有命名空间（临时根 `/var/tmp`，显式传 TMPDIR，网络自检），
  不满足即失败。Claude 场景单元另声明第二受管根：只改 `tools/official_client_control` 时 Claude 场景单元失效，Codex
  场景单元不受影响。
- 发布认证绑定当前部署收据的五摘要、pre-A3 认证（跨部署沿用时另绑复用收据）、Job 演练与 atomic-double 收据；Job 演练
  按客户端合同重放，两客户端同样要求存储与失败生命周期两项探针摘要；只能在具备 Go 与 Docker 的 Linux ARM64 上签发。
  各客户端能重放的 atomic-double 合同登记在 `certify_release.CLIENT_ATOMIC_DOUBLE_REPLAY`：Claude 一项为假，Claude 签发
  在核完 pre-A3、复用收据与 Job 演练之后失败关闭，不拿 Codex 的演练合同顶替。pre-A3 认证可选绑定的分批演练收据只属于
  Codex，Claude 给出即拒绝。

## 10 升级入口

### 10.1 客户端表

入口基础设施（入口编排器、入口步骤判定、入口门禁与统一调度执行器、便宜检查、派发前守卫、入口空跑、后台验证与日常
维护、各阶段驱动脚本）两客户端共用一份，客户端差异只在客户端表 `client_table.py`：`tools/ci/` 与驱动目录各一份、
逐字相同，只用标准库（驱动在受管树之外运行，不导入受管工具）。本轮客户端取参数 `CLIENT`，缺省 codex，不认识即失败关闭。

- 表里只有两客户端各自的取值，没有逻辑，导入时核对两行键集合闭合。表项：参数键（二进制、摘要、账号，专属必填键与
  旧键别名，便宜检查、预检 plan 与 Job 演练登记的键）、派生模板（Campaign 前缀、清单文件名、生命周期目录、官方制品
  布局、认证文件名、宿主二进制路径）、模块（编排器、认证、收口、P0、环境收据、Job 演练、atomic-double、零请求 smoke、
  计时账本、项目总账、监督器等）、部署（收据文件名模式、时间戳分隔串、身份所在对象、合同、受管根、活动文档）、文档
  （指南、第二部分标题、入口步骤读的文档、部署一致性核对的部分）、预检 plan 的参数模板、各阶段派发脚本与 `pending`。
- 两客户端共用的模块同名，靠 `client_argv`（追加给共享模块命令行的 `--client claude`）与 `identity_args`（受管身份
  函数的位置参数）区分；Codex 两项都为空，命令行与调用和改造前逐字相同，Codex 一行由对照测试锁定。
- Codex 参数文件的通用键 `CLIENT_BIN`／`CLIENT_BIN_SHA256`／`CLIENT_ACCOUNT_ID` 解析后归一成旧键；Claude 只认通用键。
  另一客户端的专属键出现在本客户端参数文件里即拒绝，也从环境里清掉残留。
- `pending` 登记某客户端尚未接入的入口步骤（`step:`）、便宜检查项（`check:`）与驱动工具（`tool:`）：执行到即失败关闭
  并写明原因，`entry.sh --plan` 也列出，不拿另一客户端的实现顶替。
- 驱动 `lib.sh` 解析参数后输出 `CT_` 变量，只赋值不导出，子进程环境与步骤判定读的环境不变。
- 受管树里的适配接口与客户端表同名的取值（部署收据文件名模式、编排器 CLI 基名、项目总账目录名）由测试核对一致。驱动
  `install.py` 是驱动完整性的复验者，不在复验前加载被复验的驱动文件，所以自带一份客户端规则，由测试核对两份一致。

### 10.2 入口 16 步的两客户端实现

入口编排器 `entry.sh`（`entry_orchestrator.py`，步骤输入与判定在 `entry_steps.py`）的步骤、依赖、步骤记录、沿用／重做／
冻结／阻塞判定与续跑规则两客户端相同；Claude 的步骤输入另登记第二受管根、本文与客户端表。各步的实现：

| 步骤 | Codex | Claude |
|---|---|---|
| `entry-preflight` | 七项便宜检查；目标客户端核宿主与 `capture-cli`；官方包核两棵源码树；`codex_upgrade plan --audit-only` | 同七项；目标客户端另核 `capture-cli-synthetic`；官方包以 `claude_upgrade_freeze` 复核冻结目录；`claude_upgrade plan --audit-only`（参数取客户端表模板） |
| `policy-compatibility`、`policy-activation` | `codex_upgrade_policy_certification` | 同一模块加 `--client claude` |
| `entry-gates`、`pre-a3` | `entry-gates.sh` 同一次运行；pre-A3 v2 | 同一入口门禁；pre-A3 v3 |
| `zero-request-smoke` | `codex_upgrade_zero_request_smoke` | `claude_upgrade_zero_request_smoke` |
| `atomic-double` | `codex_upgrade_campaign_run_rehearsal_receipt atomic-double-collect` | 客户端表 `pending`：执行即失败 |
| `ledger` | `codex_upgrade_timing_ledger create`（计划 v1） | 同一模块加 `--client claude`（计划 v2）；项目总账目录 `upgrade-project-ledger-claude` |
| `environment-p0` | `codex_upgrade_arm64_environment_receipt`（`--rust-tls-codex-version`） | `claude_upgrade_arm64_environment_receipt`（`--claude-version`） |
| `ledger-checkpoint` | 计时账本 checkpoint | 同左 |
| `preflight-plan` | `codex_upgrade plan --campaign-mode preflight_only` | `claude_upgrade plan`，参数取客户端表模板；证据决定为 `reuse` 时追加前序 Store 与 Campaign |
| `job-rehearsal` | `codex_upgrade_job_rehearsal_receipt collect／finalize` | 同一模块加 `--client claude`，经适配器取 Claude 画像 `claude_upgrade_job_rehearsal` |
| `client-launch-probe` | `client_launch_probe.py`，运行器 `client_launch_probe_runner.py` | 同一入口加 `--client claude`，运行器 `claude_launch_probe_runner.py` |
| `release-certification` | `certify_release issue／verify`（v1） | 同一模块加 `--client claude`（v2） |
| `p0-receipt` | `codex_upgrade_vc_receipt finalize／replay` | 同一模块与合同 |
| `vc0-closeout` | `codex_upgrade_vc0_closeout` | `claude_upgrade_vc0_closeout`（共享收口流程的 Claude 画像） |

建账本之前的步骤一次报全，全部通过才建账本；建账本之后失败即停；Formal 建成后创建链冻结，只剩收口按判定续作。
产物坐标、只写一次坐标被占用时换 `-r2` 等规则见入口编排器的模块说明。

### 10.3 记录库、空跑与日常维护

- 两客户端的入口门禁写同一个单元执行记录库。门禁单元（采集工具测试、出站规格子检查、后端、lint、前端、部署脚本）的
  单元 ID 与输入声明不随客户端变化；pre-A3 场景单元按客户端加前缀，互不承接，Claude 场景单元的输入另含第二受管根。
  承接只经入口门禁的受控模式（全集通过、逐单元承接），由单元记录逐条核对。
- 逐单元承接两客户端共用：当前上下文含客户端与部署收据集合，批准绑定上下文摘要与来源集合摘要；消费端（P0 v2 证据与
  收据、pre-A3 v2／v3 签发）按决策收据重验形成期证明。
- 入口空跑（`entry-dryrun.sh`）按本轮客户端绑定其最新部署收据，结论文件名带客户端前缀（Codex 为空，Claude 为
  `claude-`），同一提交加同一部署两客户端各一份；两份空跑在入口门禁工作目录里经空跑串行锁排队。Claude 的结论另带没通过
  各步的分类（`registered_pending`、`scenario_pending`、`blocked`、`real`），`real_failed_steps` 为空即没有真实失败。
- 后台验证在两客户端都纳管时只跑一次仓库全量门禁，结论的 `deployments` 绑定部署收据集合；数据根
  `control/background-validation/` 登记“提交加收据集合 → 结论”，搭上的轮次在自己的运行根留 `attached/` 指针；
  `check-boundary`、`require-passed` 按本轮客户端取结论。`vc-batch.sh` 派发前的批次边界与整机预约两客户端共用。
- 日常维护（`entry_housekeeping.py`）清理记录库时，保留被 Codex pre-A3 v2、Claude pre-A3 v3 与两客户端 P0 v2 证据
  引用的运行。

## 11 官方取证链

本节写 Claude 取证链（VC-0 收口到 VC-1 封存）在共享控制面上的落点；操作顺序、批次与续跑入口见 Claude 指南
§4.0、§4.1。

### 11.1 VC-0 收口与 Campaign 建立

收口流程只有一份，在 `codex_upgrade_vc0_closeout`：只读现场判定（Formal 未建／已建未派发／已派发）、收口 attempt、
收据副本组、建 Formal、控制产物副本、总账注册推送、「VC-0 完成」「VC-1 开始」两条事件、首批派发、已派发之后的对账与
恢复批次、上限后的账本恢复与收口收据；失败留不可覆盖诊断，同一 Formal ID 重跑即续作。客户端差异收进收口画像
（适配器成员 `closeout_profile`）：

| | Codex | Claude |
|---|---|---|
| 画像与入口 | `codex_upgrade_vc0_closeout` 本身 | `claude_upgrade_vc0_closeout`：同时设定共享控制流程与收口流程的客户端作用域，再调用同一套流程 |
| 合同名 | `codex-upgrade-vc0-closeout-*` | `official-client-vc0-closeout-*`（收口收据、失败诊断、现场、上限后恢复预览、首批交接） |
| 四份输入 | Codex 合同 | ARM64 环境收据（Claude 合同）、Claude 部署收据、Claude 发布认证（其 Job 演练与预检总计划算出的执行合同兼容）、P0 门禁收据（两客户端同一合同） |
| Formal `plan` 参数 | 从预检清单恢复 Codex 参数 | 全部取自预检 Campaign：取证坐标、Store 身份四项与冻结收据坐标取 `configuration`，冻结输入取预检副本，前序复用沿用预检冻结的绑定 |
| 历史修复子命令 | `repair-failure-accounting`、`repair-failure-closure` | 明确拒绝 |

Claude 的 `create_campaign` 在 `claude_upgrade_campaign`（`plan` 带控制收据时建 `preflight_only`，Formal 只由收口经 Python
接口建立，命令行 `plan --campaign-mode formal` 一律拒绝）。输入全部先校验、任一不过零写入；Store Campaign v2
（`<campaign_dir>/control-store`）在总计划写出之前建立，并与总计划逐项核对同源。总计划冻结 `inputs`（场景清单、证据
标签、基线与目标规则清单、Active 目录与画像、静态基线两件、生产现状清单、Store 建立输入）、取自冻结收据的
`official_identity`、`configuration`、`control_receipts`（预检两件，Formal 另有 Job 演练、发布认证与 P0 门禁）、
`vc_control` 与 `predecessor`；`predecessor` 必须与计时账本的证据决定一致。生产现状清单（`claude-upgrade-production-inventory/v1`）
从前序 Store 最新一条 `profile_approved` 只读取出、冻结成文件，不搬进本 Campaign 的 Store。四处 `client`（总计划、Store
Campaign、项目总账计划、计时账本计划）必须一致。Formal 首批恰有一个 `capture-official` 动作：共享监督器的 VC-1 普通
恢复链与收口的恢复批次都要求首批恰有这一个采集动作；只读复用前序时是零请求 no-op 首批。

### 11.2 Job 演练与启动探测

完整 Job 离线演练的共享流程（增量计划、checkpoint、逐 Job 执行与复用、收据封存与重放、Formal 与恢复兼容）在
`codex_upgrade_job_rehearsal_receipt`，`--client claude` 经适配器取 Claude 画像 `claude_upgrade_job_rehearsal`：

- `build_job_contract`、`binary_facts`、`job_contract_replay` 构造并重放完整的 Claude 执行合同
  （`official-client-job-rehearsal-contract/v1`）：Claude 总计划没有 Codex 的官方包与 code-mode-host 字段，所以是另一份
  合同，不是在 Codex 合同上换名字；
- 模块型成员 `job_rehearsal_profile` 给出合同名组（`official-client-job-rehearsal-*`）、场景清单输入名、只展开 official
  阶段、两个采集容器各自的工具树、命令依赖与步骤启动器、执行副本坐标、运行时探针与采集器身份；
- 失败生命周期探针（`claude_upgrade_failure_lifecycle`，`official-client-failure-lifecycle-probe/v1`）在断网容器里用真实
  `campaign-run` 父监督器与 Claude 父租约演练一遍失败、补跑与归档，零请求；Formal 与发布认证都要求 Job 演练绑定这项摘要。

客户端启动探测的宿主侧入口是驱动 `client_launch_probe.py`，`--client claude` 时作业展开经适配器，运行器为
`claude_launch_probe_runner.py`：回环网络，本地替身终结 `api.anthropic.com` 与 `platform.claude.com`，在覆盖层下的私有
HOME 里启动 TUI，替身收到正文含口令的首个 `/v1/messages` 才算通过。TUI 驱动循环（信任框确认与主输入框就绪判定）在
`capturelib/claude_fw_f_v3.py`，中继与启动探测共用这一份。

### 11.3 取证 attempt 与封存

| 模块 | 职责 |
|---|---|
| `claude_upgrade_plan` | 场景清单展开与校验、wire 根 `_load_plan_jobs`／`_job_context`、运行期变量绑定、证据标签加载与匹配 |
| `claude_upgrade_capture` | `capture-official run` 的实现体与 wire 根 `_campaign_arguments`／`_run_job_impl`；预约、探针、补跑与归档、权限收口、checkpoint 与 attempt 收据 |
| `claude_upgrade_static` | VC-1 静态分析 `analyze-target`（调用 evidence 层的 `claude_fw_e` 与 `claude_sink_containment.mjs`）与 VC-1 封存批次的动作生成 |
| `claude_upgrade_seal` | `capture-official seal` 的控制流程：锁、总计划、Store 写入、checkpoint、计时账本与请求计量 |
| `claude_upgrade_evidence`、`claude_upgrade_discovery` | 封存的证据语义与发现补全；编排器的 evidence 根 `scan_evidence`、`_evidence_inventory`、`_seal_capture_attempt` 只转调它们 |
| `claude_upgrade_vc` | VC checkpoint 与阶段完成登记 |

- wire 根 `_run_job_impl` 只覆盖“一次执行”：运行期变量绑定（含宿主运行收据签发与容器内可见核对）、零请求演练的
  `--dry-run` 改写、经监督器执行步骤、证据根与 relay 清单核对。探针与漂移判定、补跑与归档、权限收口、checkpoint 与
  attempt 收据都在 wire 根之外，改它们不改变 Claude 的 wire 身份；seal 的控制流程同理不进入 evidence 闭包。
- 编排器自有对象一律用 `claude-upgrade-*` 合同名：预约 `claude-upgrade-capture-reservation/v1`、attempt
  `claude-upgrade-capture-attempt/v1`、Job checkpoint 与闭环收据、探针与二进制核验 v2、环境污染、静态分析收据、seal 草稿、
  预览与阶段结果等。
- 证据根写成容器坐标 `<capture_root>/runs/claude-official/<campaign_id>/<job_id>`；宿主侧一切读写经登记的 runs 别名表
  映射到 `<数据根>/runs`，收口参数写进 attempt 的 `evidence_permission_layout`，重放按同一组参数执行。
- 合成步骤用 `synthetic-job:` 操作名：监督器不为它建立出口准入下的停止绑定，也不改写 `docker exec` 的目标；`job:` 操作
  仍只绑定唯一准入的 `capture-cli`。
- seal 的草稿与预览按代号放在 `official/seal/<代号>/`，代号由 attempt 收据、Store Campaign 身份、Store 有效工具身份、
  evidence 摘要与静态和冻结输入导出。Store 写入经进程内 `official_client_control` CLI：对象 `--allow-existing`，两条事实
  带 `fact_idempotency_key(逻辑动作, payload 摘要, 事实种类)`、payload 只用冻结值，父监督器上下文里另带 `--producer-run`，
  即动作结果 WAL 的第一步（第 5.2 节）。只读复用路径一步写完两条事实、阶段结果与 VC-1 checkpoint，补三条计时事件（ID
  前缀 `predecessor-reuse-`），写入前做同 ID 冲突预检。
- seal、前序导入、`classify` 与 VC-3 运行在 Campaign 锁内并要求 Campaign 静止：active 租约拒绝；停线租约视为静止；
  run 被强杀留下的过期 active 租约先固化为不可覆盖的停线收据再继续；停线租约绑定的 attempt 已封存即视为了结。它们
  不取得 `CampaignLease`，被强杀后不留需要对账的过期租约。
- VC-1 checkpoint 的 `live_request_count` 由 `claude_upgrade_live_request_provenance` 核算：逐 probe 分支，中继清单登记
  的排除连接与零客户端字节连接计零并逐条留证。

### 11.4 VC-1 恢复与工具演进

对账、续跑闭集、resume、残留进程清理在 `claude_upgrade_recovery`，共享对账流程在 Claude 适配器作用域里转调：

- 有未收口预约：`reconcile-attempt` 先入账后判定，可恢复时由共享对账器写零请求恢复预览，并以 Claude 的
  `recovery_reuse_check` 按 resume 同一判定只读复算，不一致即拒绝批准；批准后 `resume --rerun-failed --recovery-preview`
  建新 attempt，在同一临时目录里原子发布 `reservation.json` 与 `recovery-source.json`
  （`claude-upgrade-recovery-source/v1`），复用已完成作业、只执行预览冻结的执行集合。复用集合是对账器判为 complete、
  attempt 收据存在、环境判为已恢复的作业，再去掉被工具演进作废的作业；每个复用作业逐项复核 Job 闭环收据、Job 级权限
  收口重放与 relay 清单摘要。
- 无预约：`reconcile-supervisor-run` 入账判定，按提示以 N+1 批次重派。
- 残留取证进程：对账后对两个采集容器只读探针留证；`environment-cleanup preview／apply` 在明确批准下先 TERM 后 KILL，
  前后探针并写清理收据 `control/environment/cleanup/cleanup-NN.json`；resume 预约之前再查一次，有残留即零写入拒绝。
- `harden-evidence-permissions` 与 `wire-transition-intent`／`wire-transition-final` 参数完整、执行即拒绝：Claude 的 run 在
  每个 Job 终态与 run 末各收口一次，wire 层变化一律经工具演进登记，不建 wire transition 链。

工具演进链在 `claude_upgrade_evolution`：合同 `tool-evolution/v1`、文件 `control/tool-evolution/evolution-NN.json`、自摘要与
衔接规则与共享实现相同，只是策略演进的变化路径按 Claude 策略文件的受管条目键核对、`to_identity` 必须属于 Claude（共享链
校验在 Codex 评估读侧闭包内，不改）。预览把 wire 层变化映射到作业（编排器 wire 闭包变化或映射不到作业时全部受影响），
evidence 层变化只记评估路径，control 层变化影响为空但同样要登记；官方已封存而仍有作业受影响时拒绝登记。批准写完
编排器收据后，在 Campaign Store 登记一份 `tool_evolution_registration`；两步之间中断时以同一批准重跑只补 Store 登记。
Store Campaign 未建立时零写入拒绝。

## 12 候选 revision 与候选构建

候选 revision 的通用规则见 Framework §5.3.6。`revision-open`、`invalidate-candidate` 是共享流程子命令，Claude 在共享流程
之外再写 Store 侧，实现在 `claude_upgrade_revision`；候选构建在 `claude_upgrade_candidate`。

| 登记 | 内容 | 写入方 |
|---|---|---|
| 编排器侧 | `control/vc/revisions/r<N>/revision.json` 与 `COMMIT`，计时账本的 `stage_revision` 事件（激活） | 共享流程 |
| Store 侧 | `candidate_revision_recorded` 事实（`revision`、`candidate_id`、`supersedes`、`invalidation_ref`），决定当前有效候选 | `claude_upgrade_revision` |

- **当前 revision**：账本最后一条 `stage_revision` 与目录 COMMIT 决定，并核对 Store 的当前 revision 与候选相同；编排器
  先写、Store 后写，两者之间被强杀时这里拒绝，重跑 `revision-open` 收敛。Claude 不认隐含 r1：没有 `stage_revision` 即
  没有当前候选，候选级动作要求先 `revision-open --initial`，服务面的 `_implicit_r1_candidate_id` 一律拒绝。
- **`revision-open`**：Store 预检（零写入）→ 共享流程写编排器侧 → Store `candidate_revision_recorded`（幂等键为逻辑动作
  `claude-upgrade:revision-open` 加 payload，签发时刻取 `revision.json` 的 `opened_at_utc`）→ 复核两份登记一致。`--initial`
  要求 Store 尚无 revision 事实（或恰是同一候选的 r1）；`--supersedes <旧候选>` 要求 Store 当前候选就是旧候选，且 Store
  已有该 revision 的作废对象。预检不过即拒绝，两份登记不会分叉。
- **`invalidate-candidate preview／apply`**：共享流程完成审核与编排器侧（诊断草案与 `review_sha256`、作废记录、根因
  入账、二次判定与账本 `candidate_invalidated`）；apply 通过后 Claude 写 Store 侧：证据清单先封存为
  `official-client-operational-evidence/v1`，再写作废对象 `candidate_invalidation`（批准人取诊断的审核人，批准时刻取
  作废记录的冻结值；Store 的两步批准由编排器以同一审核结论转写）。预算暂停与永久停线不写 Store；apply 与 Store 之间被
  强杀时以同一批准摘要重跑 apply 补写。Store 侧同一 revision 只作废一次，已有 `validation_completed` 的候选不得作废；
  作废冻结整个当前 revision，候选级事实、绑定候选的收据与新执行计划一律拒绝，直到开新 revision。
- **revision seal**：`record-candidate-build` 在 Store 冻结之后、候选构建收据之前写 `control/vc/revisions/r<N>/seal.json`
  （`codex-upgrade-candidate-revision-seal/v1`，显式 r1 也写）。r≥2 以被取代候选作废记录冻结的身份快照为基准，源码层与
  构建层全部相同即拒绝；被取代候选写失效标记 `candidates/<旧候选>/superseded-by.json`
  （`claude-upgrade-candidate-superseded/v1`，这是 Claude 自实现 `_seal_candidate_revision` 与 Codex 唯一的差别）。任何写入
  之前先以不写入方式预检一次，全同的候选不会留下 Store 冻结事实。
- **候选构建收据**：客户端中立合同 `official-client-upgrade-candidate-build-receipt/v1`（带 `client`）放在共享模块
  `codex_upgrade_vc_artifacts` 末尾，只由 Claude 编排器签发；Codex 的构建收据与读侧闭包内的校验函数原位不动。门禁计划
  沿用共享的 `build_gate_plan`，门禁需求合同与 Codex 相同（`codex-post-promotion-gate-requirements/v1`），需求从 VC-2、
  VC-3 阶段结果确定性推出，不另存文件。
- **重放**：事实与收据的形成期坐标里有 `candidate_revision`，旧 revision 下的结果按形成时重放（第 6 节），交付判定只认
  当前 revision。

## 13 Codex 身份守恒

本控制面改造对 Codex 只增不改：

1. Codex 的 wire 闭包、evidence 闭包与评估读侧闭包计入的符号一律原位不动；共享代码与编排器以适配器服务面
   为界，身份敏感函数不迁出、不复制。
2. Codex wire 层与 evidence 层文件零修改；wire 层前缀下不新增文件；Claude 新文件一律 `claude_` 前缀，不新增
   忽略规则。
3. 新能力命中上述符号时新增并行实现，不改原符号。共享 control 模块里被评估读侧闭包按符号计入的符号同样不得
   修改。

`tests/test_codex_identity_conservation.py` 守门：基线固化在 `fixtures/codex_identity_conservation_baseline.json`
（基线文件摘要钉在测试里，各批不得改动），要求 wire、evidence、两个编排器闭包与四项评估器摘要逐字相等；
control 层、策略与整树摘要的变化逐文件落在 `REGISTERED_CONTROL_CHANGES` 登记的修改清单里；没有按默认层归类
的文件；迁入共享模块的函数与常量不属于任何身份闭包。

留原位的函数分两类：被 Codex 身份闭包计入、本来就不可迁的函数（闭包名单随基线固化），以及 4.2 节列出的 11 个
可迁但留原位的函数。两个编排器各自实现的读取与校验函数以后合并为一份，需要 Codex 改用跨模块按符号的 v3 引擎，
并重算其 evidence 闭包与评估器摘要，时机放在 Codex 某次新目标版本升级的 VC-0 之前。

## 14 修改本控制面时

- 先判断改动落在哪个客户端的哪个身份层：改共享 control 模块要同时按两份策略评估，并在
  `REGISTERED_CONTROL_CHANGES` 登记；改到 Codex 身份闭包内的符号属于身份守恒违规，应改为新增并行实现。
- 新增客户端差异时加适配器成员或服务面成员，并为两个客户端都登记绑定；不在共享代码里写客户端分支。
- 新增或重命名服务面成员后运行 `tests/orchestrator_surface_inventory.py --check` 与两适配器合同测试。
- 改到计时账本工具时，在其 `PRODUCER_FREEZE_SUCCESSORS` 与部署脚本 `MANAGED_RUNTIME_DOCUMENTS` 登记本批承接
  收据，再生成冻结承接收据；改到监督器时重算部署脚本的 `DEFAULT_SUPERVISOR_DIGEST`。
- 正在进行的 Codex Campaign 遇到策略文件与其它文件同时变化会拒绝策略演进：先单独部署策略并登记演进，再部署
  其它文件。
