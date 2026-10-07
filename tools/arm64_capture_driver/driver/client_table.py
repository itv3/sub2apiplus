#!/usr/bin/env python3
"""驱动链与入口基础设施的客户端表（Claude 升级 B5a-06）。

为什么要有：入口编排器、入口步骤判定、入口门禁、便宜检查、派发前守卫、后台验证、入口空跑与各阶段驱动原来都按 Codex
写死——编排器 ``codex_upgrade``、各收据模块名、部署收据文件名、``CODEX_BIN`` 一类参数键、``codex-`` 前缀的清单文件名等。
设计 D11 定下 Claude 升级共用这套入口基础设施、不另写一份：客户端无关的流程（步骤记录与失效判定、续跑、一次运行与统一
调度、单元记录与承接）照旧，凡是随客户端变的绑定都按本轮参数 ``CLIENT`` 从这张表取。表里只有两客户端各自的取值，没有
逻辑；共享代码按表取值，不写「如果是某客户端」的分支。

表项分组（取值见 ``TABLE``；Codex 一行与改造前驱动里的字面逐字相同，``tests/test_driver_client_table_codex_parity.py``
对照改造前的源码核对；表的闭合与两客户端的展开由 ``tests/test_driver_client_table.py`` 核对）：

* 参数与派生：目标客户端二进制、摘要与账号的参数键（``binary_key`` 等）、专属必填键与旧键别名、便宜检查／预检 plan／
  Job 演练登记的参数键、Campaign 与轮次前缀、规则／场景／期望／画像补丁清单文件名、候选生命周期目录、官方制品布局、
  pre-A3 认证文件名、宿主机二进制路径（模板字段见 ``format_fields``）；
* 模块：编排器（模块名、文件名与 CLI 基名）、pre-A3 认证、策略认证、发布认证、VC-0 收口、P0 收据（与 facts 合同）、
  环境收据（与探针版本参数）、Job 演练、atomic-double（与演练根前缀）、零请求 smoke、计时账本、项目总账（与目录名）、
  门禁收据、VC 制品、监督器、对账器。两客户端共用的模块同名，客户端靠 ``client_argv``（追加给共享模块命令行的 ``--client``）
  与 ``identity_args``（受管身份函数 ``current_identity`` 的位置参数）区分；Codex 这两项都为空，命令行与调用和改造前
  逐字相同；
* 部署：部署收据的文件名模式、时间戳之前的分隔串、工具身份所在对象与合同名，客户端身份覆盖的受管根，部署事务归档的
  活动文档（与受管部署器 ``arm64_supervised_deploy`` 的同名表一致，测试核对）；
* 文档：指南文件名与第二部分标题、入口步骤读的文档、部署一致性核对的部分；
* 身份策略文件名、环境收据步骤登记的受管文件、Job 演练的容器、启动探测的运行器（B2-10）、VC-5 准入合同与运行画像路径；
* 入口便宜检查（B5a-09）：目标客户端除宿主机与 ``capture-cli`` 之外还要核对的容器、官方包的复核模块、``plan --audit-only``
  的参数模板（以 ``$`` 开头的项代入同名参数，其余照抄；Codex 为空，沿用脚本里的原形状），以及零请求 smoke 一步登记的
  工具身份摘要（smoke 依赖的模块落在哪几层，哪几层的摘要就要登记）；
* 预检 plan（B2-11 第二部分）：建 ``preflight_only`` Campaign 的参数模板（写法同 ``plan --audit-only`` 的模板；Codex 为空，
  入口编排器沿用原来的 Codex 参数形状）、证据决定为 ``reuse`` 时追加的前序参数模板，以及这一步另读的文件与目录
  （``kind:模板``，``file``／``tree``；Codex 为空，步骤表逐字不变）；
* 同目标复用旧链（B5b-01）：``stage2.sh`` 导入前序官方证据的编排器子命令（Claude 为空：同目标复用走 ``entry.sh``）；
* 入口空跑（B5a-10）：结论文件名的客户端前缀（Codex 为空，结论文件名与改造前逐字相同；同一提交＋同一部署两客户端各一份）；
* VC-4（B3-04a）：门禁需求的来源（``local-chain``：本机候选链经 ssh 在采集主机现算；``arm64-preview``：采集主机小步骤
  ``vc4-requirements.sh`` 经编排器的需求预览写进本轮 RUNROOT，本机候选链只读取）与 VC-4 总控最后一步的派发脚本；
* VC-1（B2-11 第二部分）：首批之后的 VC-1 封存批次（静态分析在 seal 预览之前、seal 批准）的派发脚本（Codex 为空：Codex 的
  VC-1 封存链不经这里）；VC-2／VC-3（B5b-01）的派发脚本（Codex 是 ``vc23.sh``，Claude 是 ``vc23-claude.sh``）；
* Campaign 用途（B5b-01）：选用途的参数键。Codex 为空——入口建计时账本与预检 Campaign 固定 ``production_replacement``，
  参数文件写了 ``CAMPAIGN_PURPOSE`` 即拒绝，步骤表与命令行逐字不变；Claude 是 ``CAMPAIGN_PURPOSE``（缺省
  ``production_replacement``，可选 ``validation_only``），计时账本与预检 plan 两步登记它；
* 收尾编排（B5b-06）：收尾配置与方案的合同名（``closeout_schema``，``tools/codex_closeout.py`` 按配置的合同名分派客户端）、
  候选分支名（``closeout_candidate_branch``）与终态收据文件名前缀（``terminal_receipt_prefix``）。Codex 三项与改造前收尾里的
  字面逐字相同；
* VC-5（B5b-01b）：一条龙 ``vc5-all.sh`` 按表调用各步的派发脚本——启动（派发前检查、run 计划、切候选网关、预检、后台 run
  批次）、seal 链、accept 与 canonical 交接（``vc5_start_script``／``vc5_seal_script``／``vc5_accept_script``／
  ``vc5_canonical_script``）。Codex 四项即原来的四个脚本；Claude 是 ``vc5-claude-*.sh``，canonical 为空（Claude 在 accept 时
  完成 VC-5，不做 canonical 交接，设计附录 G 例外①），``vc5-all.sh`` 跳过这一步。Codex 那几个脚本（及其辅助脚本）开头按
  这些表项核对本客户端的这一步就是自己，不是即在任何副作用之前拒绝（``lib.sh`` 的 ``CLIENT_STEP_REQUIRED``）；
* ``pending``：Claude 尚未接入的入口步骤（``step:``）、便宜检查项（``check:``）与驱动工具（``tool:``）→ 落实它的工作项。
  驱动遇到它们失败关闭并写明工作项，不拿 Codex 的实现顶替；工作项落实时从这里删去对应条目。

身份记忆化（``CODEX_UPGRADE_IDENTITY_MEMO``）的变量名与目录两客户端共用（B1-07），不在表里。

两处副本：``tools/ci/client_table.py`` 与驱动 ``tools/arm64_capture_driver/driver/client_table.py`` 逐字相同（驱动随附，
入口基础设施的其它模块同样两处各一份）。只用标准库：驱动在数据根与受管树之外运行，不导入受管工具。受管树里的客户端
适配器（``upgrade_client_adapter``）与这里同名的取值（部署收据文件名模式、编排器 CLI 基名、项目总账目录名）由测试核对一致。

子命令（驱动 ``lib.sh`` 解析完参数文件之后调用）：

* ``shell``：按环境变量 ``CLIENT``（缺省 codex）输出各脚本要用的表项（``SHELL_ENTRIES``），形如 ``CT_<表项>=<值>``（元组为
  bash 数组，``None`` 为空串），另为两客户端出现过的全部待接入条目各输出一个 ``CT_PENDING_<种类>_<名称>``（本客户端
  已接入时为空串，``set -u`` 下也能直接引用）；只赋值、不导出，子进程看不到，不改变各步骤判定读的环境；
* ``show [--client <客户端>]``：打印一个客户端的整行（JSON），供排查。

退出码：0 成功；2 客户端不认识或用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from types import MappingProxyType
from typing import Any, Mapping

PACKAGE = "tools.official_client_capture"
CLIENTS = ("codex", "claude")
DEFAULT_CLIENT = "codex"
# 本轮参数文件里决定客户端的键：缺省或空即 codex（改造前的全部参数文件都没有这个键）。
CLIENT_PARAM = "CLIENT"


class ClientTableError(ValueError):
    """客户端不认识，或表本身不闭合（改表时写错了键）。"""


def _module(name: str) -> str:
    return f"{PACKAGE}.{name}"


# 两客户端取值相同的表项：模块是共享 control 模块（加了 --client 或由客户端参数区分合同），文档与容器是同一数据根上的同一份。
_SHARED: Mapping[str, Any] = {
    "pre_a3_module": _module("codex_upgrade_pre_a3_certification"),
    "policy_certification_module": _module("codex_upgrade_policy_certification"),
    "release_certification_module": _module("certify_release"),
    # P0 收据两客户端沿用同一合同（设计 B2-11：codex-upgrade-vc-receipt/v1 与 P0 证据 v1／v2），subject 取各自的计时账本计划。
    "p0_receipt_module": _module("codex_upgrade_vc_receipt"),
    "p0_facts_schema": "codex-upgrade-vc-receipt-facts/v1",
    # 计时账本与项目总账是共享 control 模块：Claude 由 --client claude 选 v2 计划（B1-04、B1-05），总账目录名不同。
    "timing_ledger_module": _module("codex_upgrade_timing_ledger"),
    "project_ledger_module": _module("codex_upgrade_project_ledger"),
    "gate_receipt_module": _module("codex_upgrade_gate_receipt"),
    "vc_artifacts_module": _module("codex_upgrade_vc_artifacts"),
    "supervisor_module": _module("codex_upgrade_supervisor"),
    # 对账器两客户端共用（公开入口按 Campaign 归属设定适配器）；修复轮按表取它（B5b-01）。
    "reconciler_module": _module("codex_upgrade_reconciler"),
    # Job 演练与 atomic-double 的收据模块两客户端共用（加适配器参数，Claude 另签 official-client-* 合同：Job 演练 B2-10、
    # atomic-double B5a-09 续）。
    "job_rehearsal_module": _module("codex_upgrade_job_rehearsal_receipt"),
    "atomic_rehearsal_module": _module("codex_upgrade_campaign_run_rehearsal_receipt"),
    "framework_file": "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
}

_CODEX: Mapping[str, Any] = {
    **_SHARED,
    "client": "codex",
    # ---- 参数：目标客户端的二进制、摘要与账号键沿用旧名（改造前的参数文件与各阶段脚本都读它们）；通用键
    # CLIENT_BIN／CLIENT_BIN_SHA256／CLIENT_ACCOUNT_ID 在 Codex 参数文件里是它们的别名，解析后归一成旧名导出。
    "binary_key": "CODEX_BIN",
    "binary_sha256_key": "CODEX_BIN_SHA256",
    "account_key": "CODEX_ACCOUNT_ID",
    # 只属于本客户端的必填键：Kilo 是 Codex 画像的第三方客户端用例（VC-5 Kilo）。
    "exclusive_required_keys": ("KILO_BIN", "KILO_VERSION", "KILO_SHA256", "CODEX_BIN", "CODEX_BIN_SHA256", "CODEX_ACCOUNT_ID"),
    "key_aliases": {"CLIENT_BIN": "CODEX_BIN", "CLIENT_BIN_SHA256": "CODEX_BIN_SHA256", "CLIENT_ACCOUNT_ID": "CODEX_ACCOUNT_ID"},
    # 除二进制摘要、官方包与画像摘要外还要核对为完整 SHA-256 的必填键（顺序即报错顺序）。
    "extra_sha256_keys": ("KILO_SHA256",),
    # 入口便宜检查 required-parameters 逐个核对的键（entry-preflight.sh），与入口步骤登记的同一份。
    "preflight_required": (
        "TARGET_CODE_MODE_HOST_SHA256", "CAPTURE_RUNTIME_IMAGE", "CODEX_BIN", "CODEX_BIN_SHA256", "OFFICIAL_ASSET_SHA256",
        "TARGET_PACKAGE", "TARGET_SOURCE", "BASELINE_SOURCE", "ACTIVE_PROFILE", "MAIN_MODEL", "LITE_MODEL", "CODEX_ACCOUNT_ID",
        "API_KEY_ID", "COMPOSE_DIR", "PROJECT_DEADLINE_UTC", "STAGE_BUDGETS", "POLICY_COMPAT_RECEIPT", "POLICY_ACTIVATION",
        "PRE_A3_CERTIFICATION", "P0_ROLLBACK_EVIDENCE",
    ),
    # 预检 plan 读的参数键（入口步骤 preflight-plan 登记）。
    "plan_params": (
        "BASELINE_VERSION", "TARGET_VERSION", "BASELINE_SOURCE", "TARGET_SOURCE", "ACTIVE_PROFILE", "CODEX_BIN",
        "CODEX_BIN_SHA256", "TARGET_PACKAGE", "OFFICIAL_ASSET_SHA256", "TARGET_CODE_MODE_HOST_SHA256",
        "CAPTURE_RUNTIME_IMAGE", "MAIN_MODEL", "LITE_MODEL", "CODEX_ACCOUNT_ID", "API_KEY_ID", "COMPOSE_DIR",
        "CAMPAIGN_PREFIX", "UP",
    ),
    # Job 演练读的参数键（入口步骤 job-rehearsal 登记）。
    "job_rehearsal_params": ("CODEX_BIN", "TARGET_CODE_MODE_HOST_SHA256", "CAPTURE_RUNTIME_IMAGE"),
    # ---- 派生模板（字段见 format_fields：version 三段版本、compact 去点、underscored 点换下划线、stamp 轮次时间戳）。
    "campaign_prefix": "c{compact}",
    "target_tag": "codex-{compact}",
    "rules_manifest": "codex_upgrade_rules_{underscored}.json",
    "scenarios_manifest": "codex_upgrade_scenarios_{underscored}.json",
    "expectations_manifest": "candidate_rule_expectations_{underscored}.json",
    "profile_patch_manifest": "profile_rule_patches_{underscored}.json",
    "lifecycle_dir": "docs/egress/lifecycle/codex-{compact}-candidate",
    # 官方制品布局（相对数据根）：两棵官方源码树与官方安装包。
    "official_source": "official/codex-{version}/source-rust-v{version}/codex-rs",
    "official_package": "official/codex-{version}/assets/codex-package-aarch64-unknown-linux-musl.tar.gz",
    "pre_a3_certification_name": "pre-a3-path-certification-{stamp}.json",
    # 宿主机上的目标客户端（实体目录；采集容器内同路径由数据根 runtime 只读挂载）。
    "host_binary": "/opt/codex-{version}/bin/codex",
    # ---- 模块
    "orchestrator_module": _module("codex_upgrade"),
    "orchestrator_file": "codex_upgrade.py",
    "orchestrator_basenames": ("codex_upgrade.py", "codex-upgrade"),
    "client_argv": (),
    "identity_args": (),
    "closeout_module": _module("codex_upgrade_vc0_closeout"),
    "environment_receipt_module": _module("codex_upgrade_arm64_environment_receipt"),
    "environment_probe_version_option": "--rust-tls-codex-version",
    # 环境收据步骤登记的受管文件（采集器与增量恢复）。
    "environment_receipt_files": ("codex_upgrade_arm64_environment_receipt.py", "incremental_recovery.py"),
    "atomic_root_prefix": "codex-atomic-vc0-vc1-",
    "zero_request_smoke_module": _module("codex_upgrade_zero_request_smoke"),
    # 零请求 smoke 一步登记的工具身份摘要：Codex smoke 依赖的模块（provenance、账本与总账等）都在 control 层。
    "zero_request_smoke_identity": ("control_sha256",),
    # 入口空跑结论文件名的客户端前缀（B5a-10）：Codex 为空，与改造前同名。
    "dryrun_conclusion_prefix": "",
    "project_ledger_dir_name": "upgrade-project-ledger",
    # ---- 部署
    "deploy_receipt_glob": "codex-*-supervisor-enable-*.json",
    "deploy_receipt_stamp_separator": "-supervisor-enable-",
    "deploy_receipt_identity_key": None,
    "deploy_receipt_schema": "codex-arm64-supervisor-enable/v1",
    "managed_roots": ("tools/official_client_capture",),
    "repository_documents": ("OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"),
    # ---- 文档
    "guide_file": "CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "guide_part2_heading": "# 第二部分 Codex CLI 客户端规则画像",
    "entry_guides": ("CODEX_CLI_CLIENT_EMULATION_GUIDE.md", "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md"),
    "deploy_consistency_parts": (
        "tools/official_client_capture", "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
        "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "tools/arm64_supervised_deploy.py",
    ),
    # ---- 入口便宜检查（B5a-09）：Codex 只核宿主机与 capture-cli、核对两棵官方源码树、plan-audit 用脚本里的原形状。
    "binary_extra_containers": (),
    "official_package_verifier": None,
    "plan_audit_argv": (),
    # ---- 预检 plan（B2-11 第二部分）：Codex 三项为空，入口编排器的预检 plan 命令行与步骤输入和改造前逐字相同。
    "preflight_plan_argv": (),
    "preflight_plan_reuse_argv": (),
    "preflight_plan_inputs": (),
    # ---- 其它
    "identity_policy_file": "tool_identity_policy_v2.json",
    # Job 演练的五个容器（入口步骤 job-rehearsal 登记它们的镜像）。
    "job_containers": ("capture-cli", "sub2apiplus", "sub2apiplus-keeper", "sub2apiplus-postgres", "sub2apiplus-redis"),
    # 启动探测的运行器（B2-10）：宿主侧 client_launch_probe.py 按 --client 选它，入口步骤 client-launch-probe 登记它。
    "launch_probe_runner": "client_launch_probe_runner.py",
    # VC-4（B3-04a）：门禁需求由本机候选链 local-candidate-chain.sh 经 ssh 在采集主机现算；总控 vc4-all.sh 最后一步派发
    # vc4.sh（与改造前相同）。
    "vc4_requirements_source": "local-chain",
    "vc4_dispatch_script": "vc4.sh",
    # VC-1 首批之后的封存批次派发脚本（B2-11 第二部分）：Codex 没有这一项。
    "vc1_dispatch_script": None,
    # VC-2／VC-3 的派发脚本（B5b-01）：Codex 是 vc23.sh（流程不变）。
    "vc23_dispatch_script": "vc23.sh",
    "vc5_admission_schema": "codex-vc5-admission/v1",
    "vc5_admission_approval_schema": "codex-vc5-admission-approval/v1",
    "runtime_profile": "runtime/codex-profile-{version}.json",
    # Campaign 用途（B5b-01）：Codex 没有选用途的参数，入口固定 production_replacement（与改造前相同）。
    "campaign_purpose_key": None,
    # 同目标复用旧链（B5b-01）：stage2.sh 用编排器这个子命令把前序官方证据原子导入新 Campaign 并对齐账本（Codex 原样）。
    "official_reuse_subcommand": "reuse-official-evidence",
    # 收尾编排（B5b-06）：与改造前 codex_closeout 的合同名、候选分支名与终态收据前缀逐字相同。
    "closeout_schema": "codex-upgrade-closeout/v1",
    "closeout_candidate_branch": "codex-closeout-candidate",
    "terminal_receipt_prefix": "CODEX_CLI_",
    # VC-5（B5b-01b）：各步派发脚本即原来的四个脚本（vc5-all.sh 代入后逐字不变）。
    "vc5_start_script": "vc5-start.sh",
    "vc5_seal_script": "vc5-seal.sh",
    "vc5_accept_script": "vc5-accept.sh",
    "vc5_canonical_script": "vc5-canonical2.sh",
    "pending": {},
}

# Claude plan 的参数模板：plan-audit（B5a-09）与预检 plan（B2-11 第二部分）共用这一份，两处不会各改各的。
_CLAUDE_PLAN_ARGV = (
    "--campaign-mode", "preflight_only", "--campaign-purpose", "$CAMPAIGN_PURPOSE",
    "--baseline-version", "$BASELINE_VERSION", "--target-version", "$TARGET_VERSION",
    "--target-sha256", "$CLIENT_BIN_SHA256", "--target-package", "$TARGET_PACKAGE",
    "--target-package-sha256", "$OFFICIAL_ASSET_SHA256", "--runtime-image", "$CAPTURE_RUNTIME_IMAGE",
    "--scenario-manifest", "$SCENARIOS_JSON", "--claude-bin", "$CLIENT_BIN", "--model", "$MAIN_MODEL",
    "--upstream-ip", "$UPSTREAM_IP", "--suite", "full",
    # 候选阶段专用的验收账号与 ingress API Key（B4-01 d 段，B5b-01 落实）：预检 Campaign 冻结进 configuration，VC-0 收口按它
    # 恢复 Formal plan 参数，候选 run 之前由编排器核对非空。
    "--claude-account-id", "$CLIENT_ACCOUNT_ID", "--api-key-id", "$API_KEY_ID",
)

_CLAUDE: Mapping[str, Any] = {
    **_SHARED,
    "client": "claude",
    # ---- 参数：Claude 只认通用键；Codex 专属键（Kilo、CODEX_*）出现在 Claude 参数文件里即拒绝，防止两客户端混用。
    "binary_key": "CLIENT_BIN",
    "binary_sha256_key": "CLIENT_BIN_SHA256",
    "account_key": "CLIENT_ACCOUNT_ID",
    "exclusive_required_keys": ("CLIENT_BIN", "CLIENT_BIN_SHA256", "CLIENT_ACCOUNT_ID"),
    "key_aliases": {},
    "extra_sha256_keys": (),
    # Claude 没有 Codex 的 code-mode-host，不要求 TARGET_CODE_MODE_HOST_SHA256；其余与 Codex 同一份，二进制与账号换通用键；
    # 另要 UPSTREAM_IP（B5a-09）：Claude 场景清单的必需 Campaign 变量 upstream_ip（api.anthropic.com 的冻结公开上游 IP），
    # plan-audit 与之后的 plan 都要它，参数文件可缺省（同 Codex 的 CAPTURE_RUNTIME_IMAGE），在这里报缺。
    "preflight_required": (
        "CAPTURE_RUNTIME_IMAGE", "CLIENT_BIN", "CLIENT_BIN_SHA256", "OFFICIAL_ASSET_SHA256", "TARGET_PACKAGE",
        "TARGET_SOURCE", "BASELINE_SOURCE", "ACTIVE_PROFILE", "MAIN_MODEL", "LITE_MODEL", "CLIENT_ACCOUNT_ID", "API_KEY_ID",
        "COMPOSE_DIR", "PROJECT_DEADLINE_UTC", "STAGE_BUDGETS", "POLICY_COMPAT_RECEIPT", "POLICY_ACTIVATION",
        "PRE_A3_CERTIFICATION", "P0_ROLLBACK_EVIDENCE", "UPSTREAM_IP",
        # B2-11 第二部分：预检 plan 建 Campaign 必需的输入（Active Release 目录、静态分析的基线词法锚点与索引、生产现状清单所在的
        # 前序 Store 与批准画像的 Campaign、Store 身份四项）。只读复用的前序（PREDECESSOR_STORE／_CAMPAIGN）只在证据决定为
        # reuse 时需要，不在这里报缺，由预检 plan 一步报出。
        "ACTIVE_RELEASE_CATALOG", "STATIC_BASELINE_ANCHORS", "STATIC_BASELINE_REACHABILITY", "PRODUCTION_INVENTORY_STORE",
        "PRODUCTION_INVENTORY_CAMPAIGN", "ENTRYPOINT", "PRIVACY_MODE", "OAUTH_ORG_ID", "MODEL_CATALOG_SHA256",
    ),
    # 预检 plan 实际读的参数键（B2-11 第二部分核对）：参数模板代入的键、证据决定与前序、来源提交、Campaign 命名用的前缀与
    # 升级 ID，以及冻结目录两棵（官方包在目标冻结目录里）。B5b-01 起模板另带 Campaign 用途与候选阶段的账号、ingress API Key
    # （CAMPAIGN_PURPOSE、CLIENT_ACCOUNT_ID、API_KEY_ID），一并登记；Codex 那组里的 LITE_MODEL、COMPOSE_DIR 不被 Claude plan
    # 读取，不登记，免得无关参数变动让预检及其下游 Job 演练重做。
    "plan_params": (
        "BASELINE_VERSION", "TARGET_VERSION", "BASELINE_SOURCE", "TARGET_SOURCE", "ACTIVE_PROFILE", "CLIENT_BIN",
        "CLIENT_BIN_SHA256", "TARGET_PACKAGE", "OFFICIAL_ASSET_SHA256", "CAPTURE_RUNTIME_IMAGE", "SCENARIOS_JSON", "MAIN_MODEL",
        "UPSTREAM_IP", "ACTIVE_RELEASE_CATALOG", "STATIC_BASELINE_ANCHORS", "STATIC_BASELINE_REACHABILITY",
        "PRODUCTION_INVENTORY_STORE", "PRODUCTION_INVENTORY_CAMPAIGN", "ENTRYPOINT", "PRIVACY_MODE", "OAUTH_ORG_ID",
        "MODEL_CATALOG_SHA256", "ENTRY_COMMIT", "EVIDENCE_DECISION", "PREDECESSOR_STORE", "PREDECESSOR_STORE_CAMPAIGN",
        "CAMPAIGN_PREFIX", "UP", "CAMPAIGN_PURPOSE", "CLIENT_ACCOUNT_ID", "API_KEY_ID",
    ),
    "job_rehearsal_params": ("CLIENT_BIN", "CAPTURE_RUNTIME_IMAGE"),
    # ---- 派生模板。Campaign 前缀落在 Claude Campaign ID 的形态里（claude-code-<版本，点换下划线>，适配器据此把它认作
    # 易变维度，不进稳定根因输入）；清单文件名按设计 B2-05 的 claude_ 前缀；画像补丁清单与生命周期目录按同一惯例预定，
    # B3-03／B3-04 落实时核对。
    "campaign_prefix": "claude-code-{underscored}",
    "target_tag": "claude-{compact}",
    "rules_manifest": "claude_upgrade_rules_{underscored}.json",
    "scenarios_manifest": "claude_upgrade_scenarios_{underscored}.json",
    "expectations_manifest": "claude_rule_expectations_{underscored}.json",
    "profile_patch_manifest": "claude_profile_rule_patches_{underscored}.json",
    "lifecycle_dir": "docs/egress/lifecycle/claude-{compact}-candidate",
    # Claude 没有官方源码树：官方来源是 B2-01 冻结目录（claude_upgrade_freeze --output-root，npm 与原生分发双来源与
    # freeze.json），官方包取冻结收据，OFFICIAL_ASSET_SHA256 是冻结收据 freeze.json 的摘要。B5a-09 的 official-package
    # 检查与 plan-audit 按这个布局复核目标版本的冻结目录；B2-11 的 plan 参数按同一布局核对。
    "official_source": "official/claude-code-{version}",
    "official_package": "official/claude-code-{version}/freeze.json",
    # 与 Codex 认证同在 control/policy-certification 下，文件名带客户端前缀，互不占用坐标。
    "pre_a3_certification_name": "claude-pre-a3-path-certification-{stamp}.json",
    # 设计 D3／B5a-07：宿主机 /opt/claude-<v>/claude，采集容器内同路径（数据根 runtime/claude-<v> 只读挂载）。
    "host_binary": "/opt/claude-{version}/claude",
    # ---- 模块
    "orchestrator_module": _module("claude_upgrade"),
    "orchestrator_file": "claude_upgrade.py",
    "orchestrator_basenames": ("claude_upgrade.py", "claude-upgrade"),
    "client_argv": ("--client", "claude"),
    "identity_args": ("claude",),
    "closeout_module": _module("claude_upgrade_vc0_closeout"),
    "environment_receipt_module": _module("claude_upgrade_arm64_environment_receipt"),
    "environment_probe_version_option": "--claude-version",
    # Claude 采集器的通用校验调用 Codex 采集器的现有函数（设计 B1-10），两份都登记。
    "environment_receipt_files": (
        "claude_upgrade_arm64_environment_receipt.py", "codex_upgrade_arm64_environment_receipt.py", "incremental_recovery.py",
    ),
    "atomic_root_prefix": "claude-atomic-vc0-vc1-",
    "zero_request_smoke_module": _module("claude_upgrade_zero_request_smoke"),
    # Claude smoke 依赖的 provenance（claude_upgrade_live_request_provenance）在 Claude 身份的 evidence 层，其余在 control 层。
    "zero_request_smoke_identity": ("control_sha256", "evidence_semantics_sha256"),
    # 入口空跑结论文件名带客户端前缀（B5a-10）：同一提交＋同一部署与 Codex 的结论各一份，互不顶替。
    "dryrun_conclusion_prefix": "claude-",
    "project_ledger_dir_name": "upgrade-project-ledger-claude",
    # ---- 部署
    "deploy_receipt_glob": "claude-managed-deploy-*.json",
    "deploy_receipt_stamp_separator": "claude-managed-deploy-",
    "deploy_receipt_identity_key": "identity",
    "deploy_receipt_schema": "official-client-managed-deploy/v1",
    "managed_roots": ("tools/official_client_capture", "tools/official_client_control"),
    "repository_documents": ("CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md", "OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md"),
    # ---- 文档。入口步骤与部署一致性另加两客户端共用控制面的实现文档（设计 D10）与第二受管根。
    "guide_file": "CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
    "guide_part2_heading": "# 第二部分 Claude Code 客户端规则画像",
    "entry_guides": (
        "CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md", "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md",
    ),
    "deploy_consistency_parts": (
        "tools/official_client_capture", "tools/official_client_control", "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
        "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "docs/OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md",
        "tools/arm64_supervised_deploy.py",
    ),
    # ---- 入口便宜检查（B5a-09）。合成 probe 在断网容器 capture-cli-synthetic 里跑（B5a-07），容器内同一路径的二进制也要
    # 等于登记值；官方包按 B2-01 冻结合同复核；plan-audit 的参数形状是 Claude plan 的参数（campaign-dir 与 campaign-id
    # 两客户端同一形式，在脚本里给出）。
    "binary_extra_containers": ("capture-cli-synthetic",),
    "official_package_verifier": _module("claude_upgrade_freeze"),
    "plan_audit_argv": _CLAUDE_PLAN_ARGV,
    # ---- 预检 plan（B2-11 第二部分）：plan-audit 那份参数加建 Campaign 的参数（Active 两份、静态基线两份、生产现状清单
    # 来源、Store 身份四项、来源提交）；计时账本、checkpoint、环境收据与 campaign-dir/id 由入口编排器按前序步骤记录给出。
    # 证据决定为 reuse 时另带只读复用的前序 Store 与 Campaign。另读的文件与 Store 目录登记进预检 plan 一步的输入。
    "preflight_plan_argv": (
        *_CLAUDE_PLAN_ARGV,
        "--active-release-catalog", "$ACTIVE_RELEASE_CATALOG", "--active-profile", "$ACTIVE_PROFILE",
        "--static-baseline-anchors", "$STATIC_BASELINE_ANCHORS", "--static-baseline-reachability", "$STATIC_BASELINE_REACHABILITY",
        "--production-inventory-store", "$PRODUCTION_INVENTORY_STORE", "--production-inventory-campaign", "$PRODUCTION_INVENTORY_CAMPAIGN",
        "--entrypoint", "$ENTRYPOINT", "--privacy-mode", "$PRIVACY_MODE", "--oauth-org-id", "$OAUTH_ORG_ID",
        "--model-catalog-sha256", "$MODEL_CATALOG_SHA256", "--source-commit", "$ENTRY_COMMIT",
    ),
    "preflight_plan_reuse_argv": ("--predecessor-store", "$PREDECESSOR_STORE", "--predecessor-campaign", "$PREDECESSOR_STORE_CAMPAIGN"),
    "preflight_plan_inputs": (
        "file:{ACTIVE_RELEASE_CATALOG}", "file:{STATIC_BASELINE_ANCHORS}", "file:{STATIC_BASELINE_REACHABILITY}",
        "tree:{PRODUCTION_INVENTORY_STORE}", "tree:{PREDECESSOR_STORE}",
    ),
    # ---- 其它
    "identity_policy_file": "claude_tool_identity_policy_v2.json",
    # Job 演练的容器：Codex 的五个加断网合成容器（B2-06 起合成 probe 在那里跑；入口步骤 job-rehearsal 登记它们的镜像）。
    # B2-10 的 Claude Job 演练实际核对的是其中的抓包容器与断网容器（Claude 总计划只有这两个容器坐标，official Job 也只在
    # 它们里执行）；其余四个只作步骤输入，镜像变了这一步照样重做（从宽）。
    "job_containers": (
        "capture-cli", "capture-cli-synthetic", "sub2apiplus", "sub2apiplus-keeper", "sub2apiplus-postgres", "sub2apiplus-redis",
    ),
    # 启动探测的 Claude 运行器（B2-10）：经受管驱动入口 claude_drive_tui_cli.py 启动 TUI，私有 HOME 用合成凭据。
    "launch_probe_runner": "claude_launch_probe_runner.py",
    # VC-4（B3-04a，设计 B3-04「Claude 版候选链驱动」）：门禁需求由采集主机小步骤 vc4-requirements.sh 经 claude_upgrade 的需求
    # 预览（plan-candidate-gates 只给 Campaign 与候选，零写入）写进 $RUNROOT，本机候选链 local-claude-candidate-chain.sh
    # 只读取；总控 vc4-all.sh 最后一步派发 vc4-claude.sh（两个批次的动作计划经 claude_upgrade_batch 的受管动作生成，
    # record-candidate-build 用 --preview 零写入预览代替 Codex 那种替换内部写点的预演）。
    "vc4_requirements_source": "arm64-preview",
    "vc4_dispatch_script": "vc4-claude.sh",
    # VC-1（B2-11 第二部分）：VC-0 收口只派发首批 capture-official；首批之后由 vc1-claude.sh 派发两种 VC-1 封存批次——静态分析
    # vc1-01-analyze-target 在 seal 预览 vc1-02-seal-preview 之前，审核后 seal 批准 vc1-03-seal-approve（动作取受管
    # claude_upgrade_static.vc1_seal_actions，经 claude_vc1.py 写计划、vc-batch.sh 派发）。
    "vc1_dispatch_script": "vc1-claude.sh",
    # VC-2／VC-3（B5b-01）：vc23-claude.sh 一次派发一个批次（classify 草案／修订／批准，prepare-profile 草案／修订／批准，
    # stage-profile 预览／批准），动作取受管 claude_upgrade_batch 的受管动作（经 claude_vc23.py 写计划、vc-batch.sh 派发）。
    "vc23_dispatch_script": "vc23-claude.sh",
    # VC-5 准入（B4-01 d 段，B5b-01 落实）：Claude 签 official-client-* 合同（设计附录 B）。运行画像副本是 VC-3 暂存、VC-4
    # 进候选源码树的那份 Claude 画像（catalogdata/claude/profiles/<版本>/<摘要>.json）逐字节，准入补齐时装到数据根
    # runtime 下，采集容器经 /capture 同路径可读；候选 run（B4-01b）要用画像就读这份。
    "vc5_admission_schema": "official-client-vc5-admission/v1",
    "vc5_admission_approval_schema": "official-client-vc5-admission-approval/v1",
    "runtime_profile": "runtime/claude-profile-{version}.json",
    # Campaign 用途（B5b-01）：参数 CAMPAIGN_PURPOSE 选用途（缺省 production_replacement），入口建计时账本与预检 Campaign、
    # plan-audit 都按它；之后各阶段（VC-4 构建、VC-5 准入）一律从冻结的 Campaign 总计划读取。
    "campaign_purpose_key": "CAMPAIGN_PURPOSE",
    # 同目标复用旧链（B5b-01）：Claude 没有 reuse-official-evidence——同目标复用由 entry.sh 承担（证据决定 reuse 时预检 plan
    # 冻结前序只读复用，VC-0 收口按它建 Formal，VC-1 seal 一步只读封存），stage1.sh／stage2.sh 对 Claude 失败关闭并指向 entry.sh。
    "official_reuse_subcommand": None,
    # 收尾编排（B5b-06）：同一份 codex_closeout 按合同名分派到 tools/claude_closeout.py；终态收据合同见 tools/claude_terminal_state.py。
    "closeout_schema": "claude-upgrade-closeout/v1",
    "closeout_candidate_branch": "claude-closeout-candidate",
    "terminal_receipt_prefix": "CLAUDE_CODE_",
    # VC-5（B5b-01b）：Claude 的启动、seal 链与 accept 各有派发脚本（动作经 claude_vc5.py 用受管 claude_upgrade_batch 的受管
    # 动作生成）；没有 canonical 交接（accept 写 VC-5 checkpoint），vc5-all.sh 跳过这一步。
    "vc5_start_script": "vc5-claude-start.sh",
    "vc5_seal_script": "vc5-claude-seal.sh",
    "vc5_accept_script": "vc5-claude-accept.sh",
    "vc5_canonical_script": None,
    # 仍待接入的 VC-5 驱动工具：落实前一律失败关闭并写明工作项，不先猜旗标，也不拿 Codex 的形状顶替。
    # * 边界核对（B4-01 f 段）：命令形状已定（capture-candidate boundary-check --candidate-id --attempt-id --check <check_id>
    #   [--acknowledge-live-requests]，VC-5 批次动作、阶段项 candidate-seal），入口进主线前 seal 链在派发它之前停下；
    # * 候选恢复段与 VC-5 续跑计划（B4-05b）。
    "pending": {
        "tool:vc5-boundary-check": "B4-01 f 段（capture-candidate boundary-check：Codex final wire 隔离与第三方入口拒绝两项边界核对）",
        "tool:vc5-recover": "B4-05b 与 B4-01c（候选恢复段与 VC-5 续跑计划）",
    },
}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    return value


TABLE: Mapping[str, Mapping[str, Any]] = MappingProxyType({"codex": _freeze(_CODEX), "claude": _freeze(_CLAUDE)})
PENDING_KINDS = ("step", "check", "tool")
# shell 视图输出的表项（各驱动脚本实际用到的那些）；元组为 bash 数组。
SHELL_ENTRIES = (
    "client", "client_argv", "identity_args", "orchestrator_module", "orchestrator_file", "pre_a3_module",
    "policy_certification_module", "release_certification_module", "environment_receipt_module",
    "environment_probe_version_option", "timing_ledger_module", "project_ledger_module", "project_ledger_dir_name",
    "gate_receipt_module", "vc_artifacts_module", "binary_key", "binary_sha256_key", "account_key",
    "deploy_receipt_identity_key", "preflight_required", "binary_extra_containers", "official_package_verifier",
    "plan_audit_argv", "job_rehearsal_module", "vc4_requirements_source", "vc4_dispatch_script", "vc1_dispatch_script",
    "atomic_rehearsal_module", "atomic_root_prefix", "vc23_dispatch_script", "campaign_purpose_key", "preflight_plan_argv",
    "official_reuse_subcommand", "vc5_start_script", "vc5_seal_script", "vc5_accept_script", "vc5_canonical_script",
)


def _check_table() -> None:
    """表要闭合：两行键集合相同、客户端名与行一致、元组只含字符串、待接入条目的种类认识；改表写错即导入失败。"""

    if tuple(TABLE) != CLIENTS or DEFAULT_CLIENT not in CLIENTS:
        raise ClientTableError("客户端表的行与 CLIENTS 不一致")
    keys = set(TABLE[DEFAULT_CLIENT])
    for client, row in TABLE.items():
        if set(row) != keys:
            raise ClientTableError(f"客户端表 {client} 行的键与 {DEFAULT_CLIENT} 不同：{sorted(set(row) ^ keys)}")
        if row["client"] != client:
            raise ClientTableError(f"客户端表 {client} 行的 client 取值不对")
        for key, value in row.items():
            if isinstance(value, tuple) and not all(isinstance(item, str) and item for item in value):
                raise ClientTableError(f"客户端表 {client}.{key} 只能是非空字符串组成的元组")
        for name in row["pending"]:
            kind, _, rest = name.partition(":")
            if kind not in PENDING_KINDS or not rest:
                raise ClientTableError(f"客户端表 {client} 的待接入条目名不对：{name}")
    for key in SHELL_ENTRIES:
        if key not in keys:
            raise ClientTableError(f"shell 视图引用了表里没有的表项：{key}")


_check_table()


def resolve_client(value: Any) -> str:
    """参数 ``CLIENT`` 的取值：缺省或空即 codex；不认识的值失败关闭，不默认成 codex。"""

    if value is None or value == "":
        return DEFAULT_CLIENT
    if value not in TABLE:
        raise ClientTableError(f"参数 CLIENT 不认识：{value!r}（可选：{list(CLIENTS)}）")
    return str(value)


def client_of(params: Mapping[str, Any]) -> str:
    """一组参数（参数文件解析结果或环境变量）属于哪个客户端。"""

    return resolve_client(params.get(CLIENT_PARAM))


def spec(client: Any) -> Mapping[str, Any]:
    """客户端的整行取值。"""

    return TABLE[resolve_client(client)]


def format_fields(version: str, *, stamp: str | None = None) -> dict[str, str]:
    """派生模板的字段：三段版本、去点（Codex 的 c<版本> 前缀）、点换下划线（清单文件名后缀），以及轮次时间戳。"""

    fields = {"version": version, "compact": version.replace(".", ""), "underscored": version.replace(".", "_")}
    if stamp is not None:
        fields["stamp"] = stamp
    return fields


def pending(client: Any, name: str) -> str | None:
    """本客户端尚未接入的条目（``step:``／``check:``／``tool:`` 加名称）对应的工作项；已接入为 None。"""

    return spec(client)["pending"].get(name)


def foreign_keys(client: Any) -> tuple[str, ...]:
    """其它客户端的专属必填键里、在本客户端不作别名的那些：出现在本客户端的参数文件里即拒绝，也要从环境里清掉残留。"""

    own = spec(client)
    names: list[str] = []
    for other in CLIENTS:
        if other == own["client"]:
            continue
        for key in TABLE[other]["exclusive_required_keys"]:
            if key not in own["key_aliases"] and key not in own["exclusive_required_keys"] and key not in names:
                names.append(key)
    return tuple(names)


def _shell_name(name: str) -> str:
    return "CT_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def pending_names() -> tuple[str, ...]:
    """两客户端出现过的全部待接入条目名（按出现顺序去重）。"""

    names: list[str] = []
    for row in TABLE.values():
        names.extend(name for name in row["pending"] if name not in names)
    return tuple(names)


def shell_lines(client: Any) -> list[str]:
    """``shell`` 子命令的输出行：只赋值、不导出。"""

    row = spec(client)
    lines = []
    for key in SHELL_ENTRIES:
        value = row[key]
        if isinstance(value, tuple):
            lines.append(f"{_shell_name(key)}=(" + " ".join(shlex.quote(item) for item in value) + ")")
        else:
            lines.append(f"{_shell_name(key)}={shlex.quote('' if value is None else str(value))}")
    for name in pending_names():
        lines.append(f"{_shell_name('pending_' + name)}={shlex.quote(row['pending'].get(name) or '')}")
    return lines


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return list(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="驱动链与入口基础设施的客户端表（Claude 升级 B5a-06）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("shell", help="按环境变量 CLIENT 输出各脚本要用的表项（CT_ 前缀，只赋值不导出）")
    show = sub.add_parser("show", help="打印一个客户端的整行（JSON）")
    show.add_argument("--client", default=None, help="缺省取环境变量 CLIENT（再缺省为 codex）")
    args = parser.parse_args(argv)
    try:
        if args.command == "shell":
            sys.stdout.write("".join(line + "\n" for line in shell_lines(os.environ.get(CLIENT_PARAM))))
            return 0
        client = args.client if args.client is not None else os.environ.get(CLIENT_PARAM)
        print(json.dumps(_plain(spec(client)), ensure_ascii=False, indent=2))
        return 0
    except ClientTableError as error:
        print(f"客户端表：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
