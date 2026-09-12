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
