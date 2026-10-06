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

三条硬约束：

1. 监督器与 Claude 编排器不导入 `codex_upgrade`；共享代码对编排器的引用全部经适配接口。
2. 客户端身份贯穿整条摘要链（第 3 节），任一环缺失或不一致即拒绝，不默认成 Codex。
3. 公共恢复算法只有一份：admission、批次编译与 staging、对账判定、恢复预览、评估基线、候选 revision 两个
   编排器调用同一份实现。例外是被 Codex 身份闭包计入的读取、路径与校验函数，由两个编排器各自实现，以合同
   测试对齐（第 9 节）。

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
| `forbidden_subcommands` | 值 | 15 个控制面与引导子命令 | 同 15 个，加 `freeze`、`import-predecessor-evidence` |
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
| `recovery_segment_summary`、`segment_reuse_proofs` | 函数 | 对账器原函数 | `claude_upgrade` 同签名实现 |
| `requires_project_ledger` | 函数 | 0.154 起的 Formal Campaign 在项目总账内 | Formal Campaign 一律在项目总账内 |

子命令名、阶段名与动作 ID 两客户端相同（Claude 子命令与 Codex 同名），共享代码按字面书写，不经适配器。

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
| 工具身份策略 | `tool-identity-policy/v2`（`policy_version` 8） | `tool-identity-policy/v3`（`policy_version` 1） |
| 环境收据 | `codex-upgrade-arm64-environment-receipt/v1` | `official-client-arm64-environment-receipt/v1`（含 `client`、`client_binary`） |
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
| 新对象与查询 | — | `tool_evolution_registration`、`predecessor_import`；新事实 `candidate_revision_recorded`；新收据 `predecessor_import`（`official-client-predecessor-import-receipt/v1`）；`fact-lookup` 结果 `official-client-control-fact-lookup/v1` |

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

| 档位 | 数量 | 含义 |
|---|---|---|
| 迁入薄壳 | 78 | 在 Claude 适配器作用域里转调 `upgrade_control_plane` |
| 照搬 | 41 | 两客户端语义相同的通用函数（读写、摘要、路径、租约读取），逐字取 Codex 实现，测试锁定逐字一致 |
| 自实现 | 6 | Claude 语义不同的成员：总计划读取、计时账本归属、两根工具清单与工具身份等 |
| 待实现 | 78 | 调用即报 `ClaudeOrchestratorPending`，写明由哪个后续阶段落实，不伪造行为 |

另有 6 个类（`CampaignLease`、`Job` 与四个异常类）与 Codex 同名同语义。命令行子命令与 Codex 同名：14 个共享
流程子命令照搬 Codex 的参数定义并在 Claude 作用域里转调共享实现，其余子命令的动作体落实前报未实现。
两个编排器的行为一致性由 `tests/test_orchestrator_surface_contract.py` 对两个适配器各跑同一组用例保证。

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

Claude 的新 Campaign 可以引用前序 Store 的官方证据，但只能经「登记 → 签发收据」两步获得授权。

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
| schema／版本 | `tool-identity-policy/v2`，`policy_version` 8 | `tool-identity-policy/v3`，`client: claude`，`policy_version` 1 |
| 根 | `tools/official_client_capture` | `tools/official_client_capture`、`tools/official_client_control` |
| 条目键 | 根内相对路径 | `<根目录名>/<相对路径>`（如 `official_client_control/store.py`） |
| 后缀 | `.py`、`.sh`、`.json` | 另加 `.mjs` |
| 引擎 | `codex_upgrade_tool_identity_policy.py` | `claude_tool_identity_policy.py`（v3） |

Codex 策略 v8 只在 control 层登记三个共享新文件（`upgrade_client_adapter.py`、`codex_upgrade_client_adapter.py`、
`upgrade_control_plane.py`）。Claude 策略把 Codex 专属文件逐项列入 53 项 `ignored_prefixes`，未归层文件数必须为
零；`runtime_bindings` 登记 TypeScript 运行时摘要并进入 `control_sha256`。两客户端共用的文件在两份策略里同层，
任一方修改都会同时改变双方身份，修改前按双方策略评估。

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

## 9 Codex 身份守恒

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

## 10 修改本控制面时

- 先判断改动落在哪个客户端的哪个身份层：改共享 control 模块要同时按两份策略评估，并在
  `REGISTERED_CONTROL_CHANGES` 登记；改到 Codex 身份闭包内的符号属于身份守恒违规，应改为新增并行实现。
- 新增客户端差异时加适配器成员或服务面成员，并为两个客户端都登记绑定；不在共享代码里写客户端分支。
- 新增或重命名服务面成员后运行 `tests/orchestrator_surface_inventory.py --check` 与两适配器合同测试。
- 改到计时账本工具时，在其 `PRODUCER_FREEZE_SUCCESSORS` 与部署脚本 `MANAGED_RUNTIME_DOCUMENTS` 登记本批承接
  收据，再生成冻结承接收据；改到监督器时重算部署脚本的 `DEFAULT_SUPERVISOR_DIGEST`。
- 正在进行的 Codex Campaign 遇到策略文件与其它文件同时变化会拒绝策略演进：先单独部署策略并登记演进，再部署
  其它文件。
