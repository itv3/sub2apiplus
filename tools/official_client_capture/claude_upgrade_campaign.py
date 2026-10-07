"""Claude Campaign 的建立、控制收据与客户端身份一致性（Claude 升级控制面设计 B2-11）。

``claude_upgrade.py plan`` 给出控制收据参数时落盘建立 ``preflight_only`` Campaign（编排器服务面 ``create_campaign`` 的
Claude 实现，入口编排器的 ``preflight-plan`` 用它）；不给时仍是 B2-05 的零写入展开报告。``formal`` 只由 VC-0 收口
（``claude_upgrade_vc0_closeout``）从预检清单恢复参数后经 Python 接口建立，命令行 ``plan --campaign-mode formal`` 一律
拒绝（与 Codex 0.154 起的口径相同：Formal 的建立与首批派发是同一个原子收口）。

一、控制收据（总计划 ``control_receipts``，形状与 Codex 相同）
============================================================
* ``upgrade_timing``：``ledger_dir``、``ledger_plan_sha256``、``receipt``（账本内 checkpoint 的 ``path``／``sha256``／
  ``bytes``）、``upgrade_id``、``evidence_decision``、``checkpoint_head_sha256``；
* ``arm64_environment``：``evidence_root``、``receipt``、``subject_id``、``contract_sha256``、``continuity_identity_sha256``；
* Formal 另有 ``job_rehearsal``（八个字段，与 Codex 同形）、``release_certification``（``path``、``sha256``、
  ``receipt_sha256``、``identity``）与 ``p0_gate``（``evidence_root``、``receipt``、``receipt_digest``、``upgrade_id``）。

建立时逐项重放：计时 checkpoint 可重放、账本仍可用（Formal 要求处在 VC-0）且属于 Claude；环境收据是当前 producer 采集的
通过 P0 收据、主体是账本的升级 ID、记录的目标客户端二进制就是本轮目标；Formal 的发布认证是 Claude 的 v2 认证，它绑定的
Job 演练收据按本轮执行合同兼容（共享 ``codex_upgrade_job_rehearsal_receipt`` 的 Claude 画像，B2-10），P0 门禁收据绑定本轮
升级、用途、版本与这份发布认证。

读回分两层：``validate_control_receipts_shape`` 只核形状（``load_campaign_manifest`` 每次调用），兼容 B2-08 以来测试夹具里
只有账本目录与计划摘要的计时绑定（带 checkpoint 时必须是全形）；``verify_control_receipts`` 逐项语义重放，
``require_complete`` 时要求按模式齐全、计时绑定是全形（建立后自检与 VC-0 收口用）。

二、冻结输入（总计划 ``inputs``，Campaign 内副本，只写一次）
==========================================================
``target_scenarios``、``evidence_labels``、``baseline_rules``、``target_rules``（B2-05 的四份清单）、``active_release_catalog``、
``active_profile``（VC-3 画像的 Active 输入，B3-03 优先取冻结副本）、``static_baseline_anchors``、
``static_baseline_reachability``（VC-1 静态分析的 2.1.220 词法基线，``claude_fw_e.analyze_bundles`` 必需）、
``production_inventory``（生产现状清单，第五节）、``store_bootstrap``／``store_campaign``（Store 建立输入，B2-07 的 seal
也按它们幂等建立）。Formal 的冻结输入全部取预检 Campaign 的副本（收口恢复参数时给出），两份 Campaign 逐字节同源。

三、总计划其余字段
==================
``official_identity`` 取 B2-01 冻结收据 ``freeze.json`` 的 ``npm``、``gcs``、``binary_sha256`` 与 ``binary_equivalence``（预检与
Formal 取同一份冻结，B2-10 的执行合同按整份官方身份摘要比对）；``configuration`` 是 plan 报告的 11 个取证坐标加
``platform``、``suite``、Store 身份四项（``entrypoint``、``privacy_mode``、``oauth_org_id``、``model_catalog_sha256``）、冻结
收据坐标（``official_freeze_path``、``official_freeze_sha256``）与 ``source_commit``；``jobs``、``coverage_plan`` 与
``required_rules`` 取 plan 展开；``vc_control`` 写 VC 总计划、VC-0 checkpoint，Formal 再写首批两件（第六节）；
``predecessor`` 是 null 或冻结的只读复用前序（B2-09 形状），两种模式都要求它与计时账本的证据决定一致（复用前序即
``reuse``，反之不得是 ``reuse``）。

四、Store Campaign v2（``<campaign_dir>/control-store``）
=======================================================
与总计划同源构造并冻结两份输入：bootstrap 的 ``contract_sources`` 是本 Campaign 冻结的四份清单（规则与取证合同），
``fw_c_receipts`` 是建立时的控制收据副本（计时 checkpoint、环境收据；Formal 另有 P0 门禁收据与发布认证），
``runtime_catalog`` 是冻结的 Active release catalog 与 profile，副本放在 ``inputs/store-bootstrap/``；外部绑定一律相对宿主
数据根（Store 外部核验的 external root）。Campaign v2 的七项身份与总计划同源：``official_artifacts`` 是冻结收据与它登记并
复核过的全部文件（registry 快照、npm 两个 tarball、原生分发 manifest 与平台二进制），``platforms``／``entrypoints`` 各一项，
``default_conditions`` 带唯一的 ``privacy=``；阶段依赖与预算取 Claude 计时账本计划，绝对截止取计时 checkpoint 的总截止，
项目总账引用取 Claude 总账计划。Store 在总计划写出之前建立并与总计划逐项核对
（``claude_upgrade_store.require_store_campaign_matches_plan``，含第七项官方制品的对应）。

五、生产现状清单（``inputs.production_inventory``，合同 ``claude-upgrade-production-inventory/v1``）
================================================================================================
权威来源是前序已批准的 ProfileApprovalFact（10-07 定）：从前序 Store 只读取出最新一条 ``profile_approved`` 事实，原样冻结
它引用的生产入口清单、出站处置清单与两份观测对象，以及它的入口、出站目标处置。现状＝前序目标处置
（``current_dispositions``），不另造守卫状态。冻结的是文件，不把前序对象搬进本 Campaign 的 Store：它们引用前序 Store 的
证据对象与外部根，搬过来对象图无法闭合（``seal_object`` 要求引用的对象都在同一 Store）。VC-1 seal 把这份文件绑定进
完整性清单，VC-2 分类据此校验 ``managed_egress_bound`` 的出站 ID。已知数据缺口：前序 ProfileApprovalFact（FW-G 08-21）
之后 FW-H 的生产批准（退役两个第三方入口、official-client-only）不在任何 ProfileApprovalFact 里，冻结出的现状仍把它们
记为前序目标处置（``retained_legacy``），由 VC-3 审核人的目标处置覆盖。

六、Formal 首批（VC-1 序号 1）
=============================
采集路径只有一个动作 ``capture-official``（``capture-official run --campaign-dir … --acknowledge-live-requests``，项为全部
official Job），与 Codex 同形：共享监督器的 VC-1 普通恢复链与 VC-0 收口的恢复批次都要求首批恰有这一个采集动作（父动作
前缀、项与执行项逐字对照）。VC-1 静态分析（``analyze-target``，``claude_upgrade_static``）是零请求的离线动作，放在首批
之后、seal 之前的 VC-1 批次里。只读复用前序的 Campaign 是零请求 no-op 首批（复用项为全部 official Job，没有动作）。
首批超时与 Codex 同一规则、同一取值：max(3600 秒, 作业数 × 240 秒)，不超过距截止的剩余时间。

七、客户端身份一致性
====================
``require_campaign_client_roots``：总计划、Store Campaign、项目总账计划与计时账本计划四处 ``client`` 一致（入口原先只核前
两处）。

八、Store 身份的模型目录摘要（``release_model_catalog_sha256``，10-08 定）
=====================================================================
``configuration.model_catalog_sha256``（预检 plan 的 ``--model-catalog-sha256``，轮次参数 ``MODEL_CATALOG_SHA256``）的定义：
Active Release 目录（``official-egress-claude-release-catalog/v1``）里 ``production_active`` 选择器指向的那个 Release 登记的
模型列表 ``models``，按 Store 规范 JSON 取 SHA-256（``canonical_sha256``，保留列表顺序）。它冻结进 Store Campaign v2 的身份，
前序只读复用按七项身份逐项比对，所以只有这一个算法：准备轮次参数的打包脚本调用本函数，不另算。建 Campaign 时仍只核格式，
不从冻结的 Active Release 目录复算比对。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层，不在 wire 与
evidence 闭包里（只调用 wire 根 ``_load_plan_jobs``，不被闭包根调用）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_project_ledger
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CLIENT = "claude"
SHA256_HEX = frozenset("0123456789abcdef")
PREFLIGHT_CONTROL_RECEIPTS = ("arm64_environment", "upgrade_timing")
FORMAL_CONTROL_RECEIPTS = ("arm64_environment", "job_rehearsal", "p0_gate", "release_certification", "upgrade_timing")
# 计时绑定：B2-08 起读回只要求账本目录与计划摘要（测试夹具的最小形）；建立时一律写全形。
TIMING_MINIMAL_FIELDS = frozenset({"ledger_dir", "ledger_plan_sha256"})
TIMING_FULL_FIELDS = frozenset(
    {"ledger_dir", "ledger_plan_sha256", "receipt", "upgrade_id", "evidence_decision", "checkpoint_head_sha256"}
)
ENVIRONMENT_FIELDS = frozenset({"evidence_root", "receipt", "subject_id", "contract_sha256", "continuity_identity_sha256"})
JOB_REHEARSAL_FIELDS = frozenset(
    {
        "evidence_root",
        "receipt",
        "preflight_campaign_id",
        "preflight_campaign_manifest_sha256",
        "execution_contract_sha256",
        "runtime_identity_sha256",
        "job_count",
        "job_set_sha256",
    }
)
P0_GATE_FIELDS = frozenset({"evidence_root", "receipt", "receipt_digest", "upgrade_id"})
RELEASE_CERTIFICATION_FIELDS = frozenset({"path", "sha256", "receipt_sha256", "identity"})
RECEIPT_BINDING_FIELDS = frozenset({"path", "sha256", "bytes"})
CONTROL_FIELDS = {
    "arm64_environment": ENVIRONMENT_FIELDS,
    "job_rehearsal": JOB_REHEARSAL_FIELDS,
    "p0_gate": P0_GATE_FIELDS,
    "release_certification": RELEASE_CERTIFICATION_FIELDS,
}

# plan 给出任一控制收据参数即进入建 Campaign 模式，其余必须按模式齐全。
PREFLIGHT_CONTROL_ARGUMENTS = ("timing_ledger_dir", "timing_receipt", "arm64_environment_root", "arm64_environment_receipt")
FORMAL_CONTROL_ARGUMENTS = (
    *PREFLIGHT_CONTROL_ARGUMENTS,
    "job_rehearsal_root",
    "job_rehearsal_receipt",
    "p0_gate_root",
    "p0_gate_receipt",
    "release_certification",
)
CREATE_REQUIRED_ARGUMENTS = (
    "target_package",
    "target_package_sha256",
    "entrypoint",
    "privacy_mode",
    "oauth_org_id",
    "model_catalog_sha256",
    "active_release_catalog",
    "active_profile",
    "static_baseline_anchors",
    "static_baseline_reachability",
)
# 冻结输入：名字 → Campaign 内文件名。
FROZEN_INPUT_FILES = {
    "target_scenarios": "inputs/target-scenarios.json",
    "evidence_labels": "inputs/evidence-labels.json",
    "baseline_rules": "inputs/baseline-rules.json",
    "target_rules": "inputs/target-rules.json",
    "active_release_catalog": "inputs/active-release-catalog.json",
    "active_profile": "inputs/active-profile.json",
    "static_baseline_anchors": "inputs/static-baseline-anchors.json",
    "static_baseline_reachability": "inputs/static-baseline-reachability.json",
    "production_inventory": "inputs/production-inventory.json",
    "store_bootstrap": "inputs/store-bootstrap.json",
    "store_campaign": "inputs/store-campaign.json",
}
# bootstrap 的合同来源与运行目录取哪几份冻结输入。
BOOTSTRAP_CONTRACT_INPUTS = ("baseline_rules", "evidence_labels", "target_rules", "target_scenarios")
BOOTSTRAP_RUNTIME_INPUTS = ("active_profile", "active_release_catalog")
STORE_BOOTSTRAP_SOURCE_DIR = "inputs/store-bootstrap"

# Claude Code 官方客户端的 persona（与 2.1.226 起各轮 Store 相同）。
CLAUDE_PERSONA = {
    "auth_family": "oauth",
    "official_product": "claude-code",
    "provider": "anthropic",
    "upstream_route_family": "anthropic-api",
}
# Store Campaign v2 的执行权限：ARM64 测试机上的官方取证、候选构建与候选取证（会动真实环境的三类动作）。
EXECUTION_PERMISSIONS = {"actions": ["candidate_build", "candidate_capture", "official_capture"], "hosts": ["arm64"]}
# Store Campaign v2 的默认条件里除入口、模型与隐私之外的两条（与 2.1.226 各轮 Store 相同）。
FIXED_DEFAULT_CONDITIONS = ("authentication=claude.ai-oauth", "provider=firstParty")

PRODUCTION_INVENTORY_SCHEMA = "claude-upgrade-production-inventory/v1"
PRODUCTION_INVENTORY_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "objects",
        "production_ingress_inventory_ref",
        "egress_disposition_inventory_ref",
        "ingress_target_dispositions",
        "egress_target_dispositions",
    }
)
PRODUCTION_OBJECT_KINDS = ("ingress_observation", "production_ingress_inventory", "egress_observation", "egress_disposition_inventory")
PRODUCTION_SOURCE_FIELDS = frozenset(
    {"store_campaign_id", "campaign_identity_sha256", "profile_approved_fact_ref", "approval_purpose", "issued_at_utc"}
)

PLAN_CREATED_SCHEMA = "claude-upgrade-plan-created/v1"
# Formal 首批（与 Codex 同形，见第六节）。
CAPTURE_ACTION_ID = "capture-official"
CAPTURE_OPERATION = "VC-1:capture-official"
FIRST_OFFICIAL_BATCH_MIN_TIMEOUT_SECONDS = 3600
FIRST_OFFICIAL_BATCH_SECONDS_PER_JOB = 240


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= SHA256_HEX


def _require_sha256(value: Any, label: str) -> str:
    if not _is_sha256(value):
        raise ConfigurationError(f"{label} 必须是 64 位小写十六进制摘要。")
    return str(value)


def _parse_utc(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ConfigurationError(f"{label} 不是合法时间：{value}") from error
    if parsed.tzinfo is None:
        raise ConfigurationError(f"{label} 必须带时区：{value}")
    return parsed


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 一、plan 参数
# ---------------------------------------------------------------------------


def add_campaign_arguments(parser: argparse.ArgumentParser) -> None:
    """``plan`` 建 Campaign 的参数：与 Codex plan 同名的控制收据参数，加 Claude 的 Store 身份、冻结输入、生产现状与前序。"""

    for flag, help_text in (
        ("--timing-ledger-dir", "建 Campaign：Claude 计时账本目录（UpgradeTimingLedger）"),
        ("--timing-receipt", "建 Campaign：账本内的 VC-0 checkpoint（相对账本目录或绝对路径）"),
        ("--arm64-environment-root", "建 Campaign：ARM64 P0 环境收据所在证据根"),
        ("--arm64-environment-receipt", "建 Campaign：P0 环境收据（相对证据根或绝对路径）"),
        ("--job-rehearsal-root", "Formal（只由 VC-0 收口给出）：完整 Job 演练收据所在证据根"),
        ("--job-rehearsal-receipt", "Formal（只由 VC-0 收口给出）：完整 Job 演练收据"),
        ("--p0-gate-root", "Formal（只由 VC-0 收口给出）：P0 门禁收据所在证据根"),
        ("--p0-gate-receipt", "Formal（只由 VC-0 收口给出）：P0 门禁收据"),
        ("--release-certification", "Formal（只由 VC-0 收口给出）：Claude 发布认证（tool-release-certification/v2）"),
        ("--active-release-catalog", "建 Campaign：production_active Release 的目录文件（冻结进 inputs，VC-3 用）"),
        ("--active-profile", "建 Campaign：production_active Release 的画像文件（冻结进 inputs，VC-3 用）"),
        ("--static-baseline-anchors", "建 Campaign：VC-1 静态分析的 2.1.220 词法锚点（冻结进 inputs）"),
        ("--static-baseline-reachability", "建 Campaign：VC-1 静态分析的 2.1.220 词法索引（冻结进 inputs）"),
        ("--production-inventory", "建 Campaign：已冻结的生产现状清单（Formal 由收口取预检副本）"),
        ("--production-inventory-store", "建 Campaign：前序 Store 根（取最新 ProfileApprovalFact 的生产现状清单，只读）"),
        ("--predecessor-store", "建 Campaign：只读复用的前序 Store 根（与 --predecessor-campaign 一起给出）"),
    ):
        parser.add_argument(flag, type=Path, default=None, help=help_text)
    parser.add_argument("--production-inventory-campaign", default=None, help="建 Campaign：前序 Store 里批准画像的 Campaign ID")
    parser.add_argument("--predecessor-campaign", default=None, help="建 Campaign：只读复用的前序 Store Campaign ID")
    parser.add_argument("--entrypoint", default=None, help="建 Campaign：Store 身份的入口（configuration.entrypoint）")
    parser.add_argument("--privacy-mode", default=None, help="建 Campaign：Store 身份的隐私模式（configuration.privacy_mode）")
    parser.add_argument("--oauth-org-id", default=None, help="建 Campaign：OAuth 账号组织（configuration.oauth_org_id）")
    parser.add_argument("--model-catalog-sha256", default=None, help="建 Campaign：模型目录摘要（configuration.model_catalog_sha256）")
    parser.add_argument("--source-commit", default=None, help="建 Campaign：受管工具来源提交（缺省取工具树的 git HEAD）")


def creating_campaign(arguments: argparse.Namespace) -> bool:
    """给出任一控制收据参数即建 Campaign；B2-05 的零写入展开不带控制收据。给出一部分或多出别的模式的参数即拒绝。"""

    given = [name for name in FORMAL_CONTROL_ARGUMENTS if getattr(arguments, name, None) is not None]
    if not given:
        return False
    expected = FORMAL_CONTROL_ARGUMENTS if getattr(arguments, "campaign_mode", None) == "formal" else PREFLIGHT_CONTROL_ARGUMENTS
    missing = [name for name in expected if getattr(arguments, name, None) is None]
    extra = [name for name in given if name not in expected]
    if missing or extra:
        details = [f"缺少 {missing}" if missing else "", f"不属于该模式 {extra}" if extra else ""]
        raise ConfigurationError("建 Campaign 的控制收据参数不完整：" + "；".join(item for item in details if item))
    return True


# ---------------------------------------------------------------------------
# 二、控制收据：建立时绑定
# ---------------------------------------------------------------------------


def _control_relative(root: Path, value: Path, label: str) -> str:
    """控制收据规范化为其证据根内的 POSIX 相对路径（与 Codex ``_control_receipt_relative`` 同一规则）。"""

    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"{label} 的证据根必须是现有非符号链接绝对目录。")
    resolved_root = root.resolve(strict=True)
    candidate = Path(value) if Path(value).is_absolute() else resolved_root / Path(value)
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(resolved_root).as_posix()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(f"{label} 必须位于其证据根内。") from error
    if candidate.is_symlink() or not resolved.is_file():
        raise ConfigurationError(f"{label} 必须是非符号链接普通文件。")
    return relative


def _receipt_binding(root: Path, relative: str) -> dict[str, Any]:
    path = Path(root).resolve(strict=True) / relative
    return {"path": relative, "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _environment_module() -> Any:
    from tools.official_client_capture import claude_upgrade_arm64_environment_receipt

    return claude_upgrade_arm64_environment_receipt


def _job_rehearsal_module() -> Any:
    from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt

    return codex_upgrade_job_rehearsal_receipt


def _timing_control(arguments: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """计时 checkpoint 重放并确认账本仍可用；返回 (控制绑定, 账本现状摘要, checkpoint 冻结摘要)。Formal 要求处在 VC-0。"""

    root = Path(arguments.timing_ledger_dir)
    relative = _control_relative(root, Path(arguments.timing_receipt), "UpgradeTimingLedger checkpoint")
    try:
        checkpoint = codex_upgrade_timing_ledger.replay(root, relative)
        summary = codex_upgrade_timing_ledger.assert_usable(
            root,
            relative,
            baseline_version=str(arguments.baseline_version),
            target_version=str(arguments.target_version),
            campaign_purpose=str(arguments.campaign_purpose),
            # Formal 只能在 VC-0 新建；preflight_only 不发请求、不推进阶段，允许在后续 active 阶段为工具修复重建（同 Codex）。
            required_phase="VC-0" if arguments.campaign_mode == "formal" else None,
        )
        plan, _raw = codex_upgrade_timing_ledger._load_json(root.resolve(strict=True) / "ledger.json", "计时账本计划")
        client = codex_upgrade_timing_ledger.plan_client(plan)
    except (OSError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise ConfigurationError(f"UpgradeTimingLedger 未通过：{error}") from error
    if client != CLIENT:
        raise ConfigurationError("UpgradeTimingLedger 不属于 Claude（计划 client 不是 claude）。")
    resolved = root.resolve(strict=True)
    control = {
        "ledger_dir": str(resolved),
        "ledger_plan_sha256": file_sha256(resolved / "ledger.json"),
        "receipt": _receipt_binding(resolved, relative),
        "upgrade_id": str(summary["upgrade_id"]),
        "evidence_decision": str(summary["evidence_decision"]),
        "checkpoint_head_sha256": str(checkpoint["summary"]["head_sha256"]),
    }
    return control, dict(summary), dict(checkpoint["summary"])


def _environment_control(arguments: argparse.Namespace, *, upgrade_id: str) -> dict[str, Any]:
    """Claude P0 环境收据：当前 producer、P0 阶段、通过、主体是本轮账本，记录的目标客户端二进制就是本轮目标版本与摘要。

    当前 producer 的要求与 Codex ``_require_current_p0_environment`` 同一口径（旧收据不能证明当前出口）；Claude 没有 Rust
    TLS 探针，改核收据记录的 ``client_binary``（采集容器里 ``/opt/claude-<v>/claude`` 的版本与摘要）。
    """

    module = _environment_module()
    root = Path(arguments.arm64_environment_root)
    relative = _control_relative(root, Path(arguments.arm64_environment_receipt), "ARM64 P0 环境收据")
    try:
        receipt = module.replay(root, relative)
    except (OSError, module.Arm64EnvironmentReceiptError) as error:
        raise ConfigurationError(f"ARM64 P0 环境收据未通过：{error}") from error
    if receipt.get("status") != "passed" or receipt.get("phase") != "p0" or receipt.get("subject_id") != upgrade_id:
        raise ConfigurationError("ARM64 P0 环境收据与 UpgradeTimingLedger 身份不一致（不是本轮升级的通过 P0 收据）。")
    producer = receipt.get("producer")
    if not isinstance(producer, Mapping) or producer.get("version") != module.PRODUCER_VERSION:
        raise ConfigurationError("新建 Campaign 必须使用当前环境 producer 采集的 P0 收据；历史收据只能只读回放。")
    binary = receipt.get("client_binary")
    if (
        not isinstance(binary, Mapping)
        or binary.get("version") != str(arguments.target_version)
        or binary.get("sha256") != str(arguments.target_sha256)
    ):
        raise ConfigurationError("ARM64 P0 环境收据记录的目标客户端二进制不是本轮目标版本与摘要。")
    resolved = root.resolve(strict=True)
    return {
        "evidence_root": str(resolved),
        "receipt": _receipt_binding(resolved, relative),
        "subject_id": str(receipt["subject_id"]),
        "contract_sha256": str(receipt["contract_sha256"]),
        "continuity_identity_sha256": str(receipt["continuity_identity_sha256"]),
    }


def expected_job_contract(manifest_view: Mapping[str, Any], target_scenario: Mapping[str, Any]) -> dict[str, Any]:
    """本轮 Job 执行合同：按 Claude 画像由总计划视图与目标场景清单构造（B2-10 ``build_job_contract``）。"""

    from tools.official_client_capture import claude_upgrade_job_rehearsal

    module = _job_rehearsal_module()
    try:
        return claude_upgrade_job_rehearsal.build_job_contract(manifest_view, target_scenario, None)
    except module.JobRehearsalReceiptError as error:
        raise ConfigurationError(f"无法构造本轮 Job 执行合同：{error}") from error


def job_rehearsal_contract(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    target_scenario_override: Mapping[str, Any] | None = None,
    tool_files_sha256_override: str | None = None,
) -> dict[str, Any]:
    """编排器服务面 ``_job_rehearsal_contract_from_manifest`` 的 Claude 实现：Campaign 冻结的目标场景清单副本加总计划。

    ``tool_files_sha256_override`` 只替换总计划冻结的整树摘要（与 Codex 同一语义）；场景覆盖给出时用覆盖的清单。
    """

    if target_scenario_override is not None:
        scenario = dict(target_scenario_override)
    else:
        from tools.official_client_capture import claude_upgrade_seal

        path = claude_upgrade_seal._frozen_input(Path(campaign_dir), manifest, "target_scenarios")
        scenario = _orchestrator()._read_json(path, "Campaign 冻结的目标场景清单")
    view = dict(manifest)
    if tool_files_sha256_override is not None:
        identity = manifest.get("tool_identity")
        view["tool_identity"] = {**(dict(identity) if isinstance(identity, Mapping) else {}), "files_sha256": tool_files_sha256_override}
    return expected_job_contract(view, scenario)


def _job_rehearsal_control(
    arguments: argparse.Namespace,
    *,
    expected_contract: Mapping[str, Any],
    release: Mapping[str, Any],
) -> dict[str, Any]:
    """完整 Job 演练收据：按 Claude 合同重放、与本轮执行合同兼容，且就是发布认证绑定的那一份。"""

    module = _job_rehearsal_module()
    root = Path(arguments.job_rehearsal_root)
    relative = _control_relative(root, Path(arguments.job_rehearsal_receipt), "ARM64 完整 Job 离线演练收据")
    try:
        receipt = module.replay(root, relative, client=CLIENT)
        module.assert_formal_compatible(receipt, expected_contract)
    except (OSError, module.JobRehearsalReceiptError) as error:
        raise ConfigurationError(f"ARM64 完整 Job 离线演练收据未通过：{error}") from error
    resolved = root.resolve(strict=True)
    binding = _receipt_binding(resolved, relative)
    bound = release.get("job_rehearsal") if isinstance(release.get("job_rehearsal"), Mapping) else {}
    if bound.get("sha256") != binding["sha256"]:
        raise ConfigurationError("发布认证绑定的 Job 演练收据不是本次 plan 给出的那一份。")
    preflight = receipt.get("preflight_campaign")
    if not isinstance(preflight, Mapping):
        raise ConfigurationError("Job 演练收据缺少预检 Campaign 绑定。")
    return {
        "evidence_root": str(resolved),
        "receipt": binding,
        "preflight_campaign_id": str(preflight.get("campaign_id")),
        "preflight_campaign_manifest_sha256": str(preflight.get("manifest_sha256")),
        "execution_contract_sha256": str(receipt["execution_contract_sha256"]),
        "runtime_identity_sha256": str(receipt["runtime_identity_sha256"]),
        "job_count": int(receipt["job_count"]),
        "job_set_sha256": str(receipt["job_set_sha256"]),
    }


def _release_and_p0_controls(arguments: argparse.Namespace, *, upgrade_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Formal：Claude 发布认证（v2）与 P0 门禁收据；返回 (发布认证控制, P0 控制, 发布认证原文)。"""

    from tools.official_client_capture import certify_release
    from tools.official_client_capture import codex_upgrade_vc_receipt

    release_path = Path(arguments.release_certification)
    if not release_path.is_absolute() or release_path.is_symlink() or not release_path.is_file():
        raise ConfigurationError("--release-certification 必须是非符号链接的绝对路径普通文件。")
    try:
        release = certify_release.verify(release_path, client=CLIENT)
    except (OSError, ValueError, certify_release.ReleaseCertificationError) as error:
        raise ConfigurationError(f"发布认证未通过：{error}") from error
    release_sha256 = file_sha256(release_path)
    p0_root = Path(arguments.p0_gate_root)
    p0_relative = _control_relative(p0_root, Path(arguments.p0_gate_receipt), "P0 门禁收据")
    try:
        p0 = codex_upgrade_vc_receipt.replay(p0_root, p0_relative)
    except (OSError, codex_upgrade_vc_receipt.VCReceiptError) as error:
        raise ConfigurationError(f"P0 门禁收据未通过：{error}") from error
    subject = p0.get("subject")
    assertions = p0.get("assertions")
    if (
        p0.get("kind") != "p0_gate"
        or p0.get("status") != "passed"
        or not isinstance(subject, Mapping)
        or subject.get("upgrade_id") != upgrade_id
        or subject.get("campaign_id") is not None
        or subject.get("baseline_version") != arguments.baseline_version
        or subject.get("target_version") != arguments.target_version
        or subject.get("campaign_purpose") != arguments.campaign_purpose
        or not isinstance(assertions, Mapping)
        or assertions.get("release_certification_sha256") != release_sha256
    ):
        raise ConfigurationError("P0 门禁收据未绑定当前升级、用途、版本或这份发布认证。")
    resolved = p0_root.resolve(strict=True)
    release_control = {
        "path": str(release_path.resolve(strict=True)),
        "sha256": release_sha256,
        "receipt_sha256": str(release["receipt_sha256"]),
        "identity": dict(release["identity"]),
    }
    p0_control = {
        "evidence_root": str(resolved),
        "receipt": _receipt_binding(resolved, p0_relative),
        "receipt_digest": str(p0["receipt_digest"]),
        "upgrade_id": str(subject["upgrade_id"]),
    }
    return release_control, p0_control, dict(release)


# ---------------------------------------------------------------------------
# 二（续）：控制收据读回
# ---------------------------------------------------------------------------


def _binding_shape(value: Any, label: str) -> tuple[str, str, int]:
    if not isinstance(value, Mapping) or set(value) != RECEIPT_BINDING_FIELDS:
        raise ConfigurationError(f"{label}绑定字段不闭合。")
    relative, digest, size = value.get("path"), value.get("sha256"), value.get("bytes")
    if (
        not isinstance(relative, str)
        or not relative
        or PurePosixPath(relative).is_absolute()
        or ".." in PurePosixPath(relative).parts
        or not _is_sha256(digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
    ):
        raise ConfigurationError(f"{label}绑定非法。")
    return relative, str(digest), size


def validate_control_receipts_shape(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """总计划 ``control_receipts`` 的形状（每次加载都核）：名字闭集按模式、各绑定字段闭合；计时绑定允许最小形。"""

    controls = manifest.get("control_receipts")
    if not isinstance(controls, Mapping):
        raise ConfigurationError("Campaign control_receipts 必须是对象。")
    allowed = FORMAL_CONTROL_RECEIPTS if manifest.get("campaign_mode") == "formal" else PREFLIGHT_CONTROL_RECEIPTS
    unknown = sorted(set(controls) - set(allowed))
    if unknown:
        raise ConfigurationError(f"Campaign control_receipts 含该模式不允许的项：{unknown}")
    timing = controls.get("upgrade_timing")
    if timing is not None:
        if not isinstance(timing, Mapping) or not TIMING_MINIMAL_FIELDS <= set(timing) <= TIMING_FULL_FIELDS:
            raise ConfigurationError("Campaign 计时控制绑定字段非法。")
        if "receipt" in timing or "checkpoint_head_sha256" in timing:
            if set(timing) != TIMING_FULL_FIELDS:
                raise ConfigurationError("Campaign 计时控制绑定带 checkpoint 时必须是全形。")
            _binding_shape(timing["receipt"], "计时 checkpoint ")
            _require_sha256(timing["checkpoint_head_sha256"], "计时 checkpoint head")
    for name, fields in CONTROL_FIELDS.items():
        value = controls.get(name)
        if value is None:
            continue
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ConfigurationError(f"Campaign 控制收据 {name} 字段不闭合。")
        if "receipt" in value:
            _binding_shape(value["receipt"], f"{name} 收据")
        root = value.get("evidence_root", value.get("path"))
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ConfigurationError(f"Campaign 控制收据 {name} 的根路径必须是绝对路径。")
    return dict(controls)


def _bound_receipt(root_value: Any, binding: Mapping[str, Any], label: str) -> tuple[Path, str]:
    relative, digest, size = _binding_shape(binding, label)
    root = Path(str(root_value))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"{label}的证据根不可信：{root}")
    path = root / relative
    if path.is_symlink() or not path.is_file() or file_sha256(path) != digest or path.stat().st_size != size:
        raise ConfigurationError(f"{label}绑定摘要漂移：{path}")
    return root, relative


def verify_control_receipts(
    manifest: Mapping[str, Any],
    *,
    require_complete: bool = True,
    campaign_dir: Path | None = None,
) -> dict[str, Any]:
    """逐项重放总计划的控制收据（建立后自检与 VC-0 收口用）；``require_complete`` 时按模式齐全且计时绑定是全形。

    历史 producer 的环境收据照常只读回放（重放本身按收据记录的 producer 判定），这里不再要求当前 producer。给出
    ``campaign_dir`` 时另核 VC 控制制品与控制收据的对应（``verify_vc_control_correspondence``）。
    """

    controls = validate_control_receipts_shape(manifest)
    mode = manifest.get("campaign_mode")
    expected = FORMAL_CONTROL_RECEIPTS if mode == "formal" else PREFLIGHT_CONTROL_RECEIPTS
    timing = controls.get("upgrade_timing")
    if require_complete and (
        set(controls) != set(expected) or not isinstance(timing, Mapping) or set(timing) != TIMING_FULL_FIELDS
    ):
        raise ConfigurationError(f"Campaign 缺少完整控制收据绑定（{mode} 需要 {list(expected)}，计时绑定须是全形）。")
    summary: dict[str, Any] | None = None
    if isinstance(timing, Mapping) and "receipt" in timing:
        root, relative = _bound_receipt(timing["ledger_dir"], timing["receipt"], "计时 checkpoint ")
        if file_sha256(root / "ledger.json") != timing["ledger_plan_sha256"]:
            raise ConfigurationError("UpgradeTimingLedger 计划摘要漂移。")
        try:
            checkpoint = codex_upgrade_timing_ledger.replay(root, relative)
        except (OSError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
            raise ConfigurationError(f"UpgradeTimingLedger 无法重放：{error}") from error
        summary = dict(checkpoint["summary"])
        if (
            summary.get("upgrade_id") != timing["upgrade_id"]
            or summary.get("evidence_decision") != timing["evidence_decision"]
            or summary.get("head_sha256") != timing["checkpoint_head_sha256"]
        ):
            raise ConfigurationError("UpgradeTimingLedger checkpoint 身份漂移。")
    environment = controls.get("arm64_environment")
    if isinstance(environment, Mapping):
        module = _environment_module()
        root, relative = _bound_receipt(environment["evidence_root"], environment["receipt"], "ARM64 P0 环境收据")
        try:
            receipt = module.replay(root, relative)
        except (OSError, module.Arm64EnvironmentReceiptError) as error:
            raise ConfigurationError(f"ARM64 P0 环境收据无法重放：{error}") from error
        if (
            receipt.get("status") != "passed"
            or receipt.get("phase") != "p0"
            or receipt.get("subject_id") != environment["subject_id"]
            or receipt.get("contract_sha256") != environment["contract_sha256"]
            or receipt.get("continuity_identity_sha256") != environment["continuity_identity_sha256"]
            or (isinstance(timing, Mapping) and timing.get("upgrade_id", environment["subject_id"]) != environment["subject_id"])
        ):
            raise ConfigurationError("ARM64 P0 环境收据身份漂移或与计时账本不一致。")
    rehearsal = controls.get("job_rehearsal")
    if isinstance(rehearsal, Mapping):
        module = _job_rehearsal_module()
        root, relative = _bound_receipt(rehearsal["evidence_root"], rehearsal["receipt"], "完整 Job 演练收据")
        try:
            receipt = module.replay(root, relative, client=CLIENT)
        except (OSError, module.JobRehearsalReceiptError) as error:
            raise ConfigurationError(f"完整 Job 演练收据无法重放：{error}") from error
        preflight = receipt.get("preflight_campaign") if isinstance(receipt.get("preflight_campaign"), Mapping) else {}
        if (
            receipt.get("status") != "passed"
            or receipt.get("execution_contract_sha256") != rehearsal["execution_contract_sha256"]
            or receipt.get("runtime_identity_sha256") != rehearsal["runtime_identity_sha256"]
            or receipt.get("job_count") != rehearsal["job_count"]
            or receipt.get("job_set_sha256") != rehearsal["job_set_sha256"]
            or preflight.get("campaign_id") != rehearsal["preflight_campaign_id"]
            or preflight.get("manifest_sha256") != rehearsal["preflight_campaign_manifest_sha256"]
        ):
            raise ConfigurationError("完整 Job 演练收据身份漂移。")
    release = controls.get("release_certification")
    if isinstance(release, Mapping):
        path = Path(str(release["path"]))
        if path.is_symlink() or not path.is_file() or file_sha256(path) != release["sha256"]:
            raise ConfigurationError("发布认证绑定摘要漂移。")
    p0_gate = controls.get("p0_gate")
    if isinstance(p0_gate, Mapping):
        from tools.official_client_capture import codex_upgrade_vc_receipt

        root, relative = _bound_receipt(p0_gate["evidence_root"], p0_gate["receipt"], "P0 门禁收据")
        try:
            receipt = codex_upgrade_vc_receipt.replay(root, relative)
        except (OSError, codex_upgrade_vc_receipt.VCReceiptError) as error:
            raise ConfigurationError(f"P0 门禁收据无法重放：{error}") from error
        subject = receipt.get("subject") if isinstance(receipt.get("subject"), Mapping) else {}
        assertions = receipt.get("assertions") if isinstance(receipt.get("assertions"), Mapping) else {}
        if (
            receipt.get("receipt_digest") != p0_gate["receipt_digest"]
            or subject.get("upgrade_id") != p0_gate["upgrade_id"]
            or (isinstance(release, Mapping) and assertions.get("release_certification_sha256") != release["sha256"])
        ):
            raise ConfigurationError("P0 门禁收据身份漂移或未绑定这份发布认证。")
    if campaign_dir is not None:
        verify_vc_control_correspondence(Path(campaign_dir), manifest)
    return {"controls": sorted(controls), "timing_summary": summary}


def verify_vc_control_correspondence(campaign_dir: Path, manifest: Mapping[str, Any]) -> None:
    """VC 总计划冻结的四个控制摘要等于总计划控制收据的收据摘要；Formal 的 VC-0 阶段收据就是 P0 门禁收据。

    ``claude_upgrade_vc.validate_vc_control`` 只核 VC 制品自身（总计划身份、批次模型、VC-0 与首批的编译关系），它与控制收据
    的对照在这里（``claude_upgrade_vc`` 模块说明第四节留给 B2-11 的部分）。没有 VC 总计划绑定的夹具总计划不核。
    """

    control = manifest.get("vc_control")
    if not isinstance(control, Mapping) or control.get("campaign_plan") is None:
        return
    controls = manifest.get("control_receipts") if isinstance(manifest.get("control_receipts"), Mapping) else {}

    def receipt_sha256(name: str) -> str | None:
        value = controls.get(name)
        receipt = value.get("receipt") if isinstance(value, Mapping) else None
        return str(receipt["sha256"]) if isinstance(receipt, Mapping) and "sha256" in receipt else None

    plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
    expected = {
        "timing_checkpoint_sha256": receipt_sha256("upgrade_timing"),
        "arm64_environment_sha256": receipt_sha256("arm64_environment"),
        "job_rehearsal_sha256": receipt_sha256("job_rehearsal"),
        "p0_gate_sha256": receipt_sha256("p0_gate"),
    }
    drifted = sorted(key for key, value in expected.items() if plan["controls"].get(key) != value)
    if drifted:
        raise ConfigurationError(f"VC 总计划冻结的控制摘要与总计划控制收据不一致：{drifted}")
    if manifest.get("campaign_mode") == "formal":
        _path, vc0 = claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-0")
        if vc0["stage_receipt"]["sha256"] != expected["p0_gate_sha256"]:
            raise ConfigurationError("Formal 的 VC-0 阶段收据不是总计划绑定的 P0 门禁收据。")


# ---------------------------------------------------------------------------
# 二（续）：冻结输入与官方身份
# ---------------------------------------------------------------------------


def _write_bytes_once(path: Path, data: bytes, label: str) -> None:
    """只写一次：目标不存在时以 0600 独占创建；已存在且字节相同即复用，不同即拒绝。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != data:
            raise ConfigurationError(f"{label}已存在且内容不同：{path}")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _freeze_file(source: Path, destination: Path, label: str) -> str:
    """把输入文件逐字节冻结进 Campaign（只写一次），返回摘要。"""

    source = Path(source)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ConfigurationError(f"{label}必须是非符号链接的绝对路径普通文件：{source}")
    data = source.read_bytes()
    _write_bytes_once(destination, data, label)
    return sha256_bytes(data)


def official_identity_from_freeze(freeze_path: Path) -> dict[str, Any]:
    """总计划 ``official_identity``：B2-01 冻结收据的 npm、gcs、binary_sha256 与 binary_equivalence（四项原样）。"""

    payload = json.loads(Path(freeze_path).read_text(encoding="utf-8"))
    return {key: payload[key] for key in ("npm", "gcs", "binary_sha256", "binary_equivalence")}


def official_artifact_bindings(verification: Mapping[str, Any], data_root: Path) -> list[dict[str, Any]]:
    """Store Campaign ``official_artifacts``：冻结收据与它登记并复核过的全部文件，相对宿主数据根、按路径排序。"""

    freeze_path = Path(str(verification["freeze_path"])).resolve(strict=True)
    root = freeze_path.parent
    members = {freeze_path.name, *(str(item["path"]) for item in verification["verified_files"])}
    rows = []
    for member in sorted(members):
        path = (root / member).resolve(strict=True)
        rows.append(
            {"path": _data_relative(path, data_root, "官方冻结目录"), "sha256": file_sha256(path), "bytes": path.stat().st_size}
        )
    return sorted(rows, key=lambda row: row["path"])


# ---------------------------------------------------------------------------
# 八、Store 身份的模型目录摘要
# ---------------------------------------------------------------------------


def release_model_catalog_sha256(catalog: Mapping[str, Any]) -> str:
    """Store 身份的模型目录摘要（定义见模块说明第八节）：``production_active`` Release 的 ``models`` 的规范 JSON 摘要。

    目录不是 ``official-egress-claude-release-catalog/v1``、``production_active`` 不是指向唯一一个 Release 的选择器、模型列表
    为空或含非字符串、空串与重复项时拒绝（失败关闭，不退回别的算法）。
    """

    from tools.official_client_capture import claude_upgrade_profile

    schema = claude_upgrade_profile.RELEASE_CATALOG_SCHEMA
    if not isinstance(catalog, Mapping) or catalog.get("schema_version") != schema or not isinstance(catalog.get("releases"), list):
        raise ConfigurationError(f"Active Release 目录不是 {schema}。")
    selectors = catalog.get("selectors")
    selector = selectors.get("production_active") if isinstance(selectors, Mapping) else None
    if not isinstance(selector, Mapping) or selector.get("kind") != "release":
        raise ConfigurationError("Active Release 目录的 production_active 选择器不是 Release。")
    matches = [
        item for item in catalog["releases"] if isinstance(item, Mapping) and item.get("release_sha256") == selector.get("release_sha256")
    ]
    if len(matches) != 1:
        raise ConfigurationError("production_active 选择器指向的 Release 不唯一或不存在。")
    models = matches[0].get("models")
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(model, str) or not model for model in models)
        or len(set(models)) != len(models)
    ):
        raise ConfigurationError("production_active Release 的模型列表必须是不重复的非空字符串列表。")
    return canonical_sha256(list(models))


# ---------------------------------------------------------------------------
# 五、生产现状清单
# ---------------------------------------------------------------------------


def extract_production_inventory(store_root: Path, campaign_id: str) -> dict[str, Any]:
    """从前序 Store 只读取出最新 ProfileApprovalFact 引用的生产入口清单、出站处置清单、两份观测对象与目标处置。"""

    from tools.official_client_control.store import ControlStore

    try:
        store = ControlStore.open_read_only(Path(store_root))
        campaign = store.load_campaign(str(campaign_id))
        facts = [fact for fact in store.list_facts(str(campaign_id)) if fact.get("fact_kind") == "profile_approved"]
        if not facts:
            raise ConfigurationError(f"前序 Store Campaign {campaign_id} 没有 ProfileApprovalFact。")
        fact = max(facts, key=lambda item: int(item["sequence"]))
        payload = fact["payload"]
        ingress = store.load_object(payload["production_ingress_inventory_ref"])
        egress = store.load_object(payload["egress_disposition_inventory_ref"])
        ingress_observation = store.load_object(ingress["payload"]["observation_ref"])
        egress_observation = store.load_object(egress["payload"]["observation_ref"])
        fact_ref = store.fact_ref(fact)
    except ControlError as error:
        raise ConfigurationError(f"前序 Store 读不出生产现状清单：{error}") from error
    inventory = {
        "schema_version": PRODUCTION_INVENTORY_SCHEMA,
        "source": {
            "store_campaign_id": str(campaign_id),
            "campaign_identity_sha256": str(campaign["identity_sha256"]),
            "profile_approved_fact_ref": dict(fact_ref),
            "approval_purpose": str(payload["approval_purpose"]),
            "issued_at_utc": str(fact["issued_at_utc"]),
        },
        "objects": [
            {"object_kind": document["object_kind"], "sha256": canonical_sha256(document), "payload": document["payload"]}
            for document in (ingress_observation, ingress, egress_observation, egress)
        ],
        "production_ingress_inventory_ref": dict(payload["production_ingress_inventory_ref"]),
        "egress_disposition_inventory_ref": dict(payload["egress_disposition_inventory_ref"]),
        "ingress_target_dispositions": [dict(item) for item in payload["ingress_target_dispositions"]],
        "egress_target_dispositions": [dict(item) for item in payload["egress_target_dispositions"]],
    }
    return validate_production_inventory(inventory)


def validate_production_inventory(payload: Any) -> dict[str, Any]:
    """生产现状清单的合同：四个对象按 Store 合同自洽且按内容寻址、两份清单引用就是其中两份、目标处置与清单条目逐项闭合。"""

    from tools.official_client_control.contracts import OBJECT_SCHEMA, validate_fact_ref, validate_object_document

    if not isinstance(payload, Mapping) or set(payload) != PRODUCTION_INVENTORY_FIELDS:
        raise ConfigurationError("生产现状清单字段不闭合。")
    if payload.get("schema_version") != PRODUCTION_INVENTORY_SCHEMA:
        raise ConfigurationError(f"生产现状清单合同不是 {PRODUCTION_INVENTORY_SCHEMA}。")
    source = payload["source"]
    if not isinstance(source, Mapping) or set(source) != PRODUCTION_SOURCE_FIELDS:
        raise ConfigurationError("生产现状清单的来源字段不闭合。")
    try:
        validate_fact_ref(source["profile_approved_fact_ref"], "production_inventory.source.profile_approved_fact_ref")
    except ControlError as error:
        raise ConfigurationError(f"生产现状清单的来源事实引用非法：{error}") from error
    if not _is_sha256(source["campaign_identity_sha256"]):
        raise ConfigurationError("生产现状清单的来源 Campaign 身份摘要非法。")
    objects = payload["objects"]
    if not isinstance(objects, list) or [item.get("object_kind") if isinstance(item, Mapping) else None for item in objects] != list(
        PRODUCTION_OBJECT_KINDS
    ):
        raise ConfigurationError("生产现状清单的对象必须是入口观测、入口清单、出站观测、出站清单四份（按此顺序）。")
    references: dict[str, dict[str, Any]] = {}
    for item in objects:
        if set(item) != {"object_kind", "sha256", "payload"}:
            raise ConfigurationError(f"生产现状清单的 {item['object_kind']} 字段不闭合。")
        document = {"schema_version": OBJECT_SCHEMA, "object_kind": item["object_kind"], "payload": item["payload"]}
        try:
            validate_object_document(document)
        except (ControlError, KeyError, TypeError, ValueError) as error:
            raise ConfigurationError(f"生产现状清单的 {item['object_kind']} 不满足 Store 合同：{error}") from error
        digest = canonical_sha256(document)
        if item["sha256"] != digest:
            raise ConfigurationError(f"生产现状清单的 {item['object_kind']} 摘要与内容不一致。")
        references[str(item["object_kind"])] = {"sha256": digest, "payload": item["payload"]}
    for key, kind, observation in (
        ("production_ingress_inventory_ref", "production_ingress_inventory", "ingress_observation"),
        ("egress_disposition_inventory_ref", "egress_disposition_inventory", "egress_observation"),
    ):
        if payload[key] != {"object_kind": kind, "sha256": references[kind]["sha256"]}:
            raise ConfigurationError(f"生产现状清单的 {key} 不是冻结的那份对象。")
        if references[kind]["payload"]["observation_ref"] != {"object_kind": observation, "sha256": references[observation]["sha256"]}:
            raise ConfigurationError(f"生产现状清单的 {kind} 观测引用不是冻结的那份观测对象。")
    ingress_ids = sorted(str(item["logical_ingress_id"]) for item in references["production_ingress_inventory"]["payload"]["entries"])
    egress_ids = sorted(str(item["egress_id"]) for item in references["egress_disposition_inventory"]["payload"]["entries"])
    ingress_targets = payload["ingress_target_dispositions"]
    egress_targets = payload["egress_target_dispositions"]
    if (
        not isinstance(ingress_targets, list)
        or not all(isinstance(item, Mapping) for item in ingress_targets)
        or sorted(str(item.get("logical_ingress_id")) for item in ingress_targets) != ingress_ids
    ):
        raise ConfigurationError("生产现状清单的入口目标处置没有恰好覆盖入口清单。")
    if (
        not isinstance(egress_targets, list)
        or not all(isinstance(item, Mapping) for item in egress_targets)
        or sorted(str(item.get("egress_id")) for item in egress_targets) != egress_ids
    ):
        raise ConfigurationError("生产现状清单的出站目标处置没有恰好覆盖出站清单。")
    return dict(payload)


def current_dispositions(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """现状视图：前序目标处置即当前生产处置（入口 → 处置；出站 → 处置、规则与受管策略），不另造守卫状态。"""

    validate_production_inventory(payload)
    return {
        "ingress": {
            str(item["logical_ingress_id"]): {"disposition": str(item["target_disposition"])}
            for item in payload["ingress_target_dispositions"]
        },
        "egress": {
            str(item["egress_id"]): {
                "disposition": str(item["target_disposition"]),
                "spec_ids": list(item.get("spec_ids") or []),
                "managed_policy": item.get("managed_policy"),
            }
            for item in payload["egress_target_dispositions"]
        },
    }


def production_egress_ids(payload: Mapping[str, Any]) -> frozenset[str]:
    """生产现状清单的出站 ID 全集（出站处置清单条目；VC-2 校验 ``managed_egress_bound`` 用）。"""

    validated = validate_production_inventory(payload)
    egress = next(item for item in validated["objects"] if item["object_kind"] == "egress_disposition_inventory")
    return frozenset(str(item["egress_id"]) for item in egress["payload"]["entries"])


# ---------------------------------------------------------------------------
# 四、Store Campaign v2
# ---------------------------------------------------------------------------


def _data_relative(path: Path, data_root: Path, label: str) -> str:
    try:
        return Path(path).resolve(strict=True).relative_to(Path(data_root).resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"{label}必须在宿主数据根内（Store 外部核验以数据根为根）：{path}") from error


def _external_binding(path: Path, data_root: Path, label: str) -> dict[str, Any]:
    resolved = Path(path).resolve(strict=True)
    return {"path": _data_relative(resolved, data_root, label), "sha256": file_sha256(resolved), "bytes": resolved.stat().st_size}


def store_bootstrap_payload(
    campaign_dir: Path,
    *,
    data_root: Path,
    source_commit: str,
    control_copies: Sequence[Path],
    tool_bundle_sha256: str,
) -> dict[str, Any]:
    """Store bootstrap：合同来源是冻结的四份清单，控制基座收据是建立时的控制收据副本，运行目录是冻结的 Active 两件。"""

    if not (isinstance(source_commit, str) and len(source_commit) == 40 and set(source_commit) <= SHA256_HEX):
        raise ConfigurationError("bootstrap source_commit 必须是 40 位提交号（--source-commit 或工具树 git HEAD）。")

    def bindings(paths: Sequence[Path], label: str) -> list[dict[str, Any]]:
        return sorted((_external_binding(path, data_root, label) for path in paths), key=lambda item: item["path"])

    contract_sources = bindings([Path(campaign_dir) / FROZEN_INPUT_FILES[name] for name in BOOTSTRAP_CONTRACT_INPUTS], "冻结输入")
    return {
        "schema_version": "official-client-control-bootstrap/v1",
        "source_commit": source_commit,
        "contract_sources": contract_sources,
        "contract_bundle_sha256": canonical_sha256(contract_sources),
        "fw_c_receipts": bindings(list(control_copies), "控制收据副本"),
        "runtime_catalog": bindings([Path(campaign_dir) / FROZEN_INPUT_FILES[name] for name in BOOTSTRAP_RUNTIME_INPUTS], "冻结输入"),
        "tool_bundle_sha256": tool_bundle_sha256,
        "result": "stable",
    }


def store_campaign_document(
    *,
    campaign_id: str,
    created_at_utc: str,
    target_version: str,
    campaign_purpose: str,
    configuration: Mapping[str, Any],
    official_artifacts: Sequence[Mapping[str, Any]],
    bootstrap_ref: Mapping[str, str],
    timing_plan: Mapping[str, Any],
    deadline_at_utc: str,
    project_ledger_ref: Mapping[str, str],
    store_tool_identity: Mapping[str, Any],
    tool_bundle_sha256: str,
) -> dict[str, Any]:
    """Store Campaign v2：七项身份与总计划 ``configuration``、冻结收据同源；阶段依赖与预算取 Claude 计时账本计划。"""

    from tools.official_client_capture.codex_upgrade_supervisor import (
        DEFAULT_HEARTBEAT_SECONDS,
        DEFAULT_LEDGER_INTERVAL_SECONDS,
        DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
    )
    from tools.official_client_control.contracts import campaign_identity_sha256

    phases = list(codex_upgrade_vc_artifacts.VC_PHASES)
    budgets = timing_plan.get("stage_budgets_minutes")
    if not isinstance(budgets, Mapping) or set(budgets) != set(phases):
        raise ConfigurationError("计时账本计划的阶段预算必须恰好覆盖 VC-0～VC-6。")
    stage_budgets = {phase: budgets[phase] for phase in phases}
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in stage_budgets.values()):
        raise ConfigurationError("计时账本计划的阶段预算必须是正整数分钟。")
    document = {
        "schema_version": "official-client-control-campaign/v2",
        "campaign_id": campaign_id,
        "persona": dict(CLAUDE_PERSONA),
        "target_version": target_version,
        "official_artifacts": [dict(item) for item in official_artifacts],
        "platforms": [str(configuration["platform"])],
        "entrypoints": [str(configuration["entrypoint"])],
        "default_conditions": sorted(
            {
                *FIXED_DEFAULT_CONDITIONS,
                f"entrypoint={configuration['entrypoint']}",
                f"model={configuration['model']}",
                f"privacy={configuration['privacy_mode']}",
            }
        ),
        "tool_bundle_sha256": tool_bundle_sha256,
        "bootstrap_ref": dict(bootstrap_ref),
        "created_at_utc": created_at_utc,
        "identity_sha256": "",
        "client": CLIENT,
        "campaign_purpose": campaign_purpose,
        "oauth_org_id": str(configuration["oauth_org_id"]),
        "model_catalog_sha256": str(configuration["model_catalog_sha256"]),
        "execution_permissions": {key: list(value) for key, value in EXECUTION_PERMISSIONS.items()},
        "stage_dependencies": {phase: list(codex_upgrade_vc_artifacts.VC_DEPENDENCIES[phase]) for phase in phases},
        "stage_budgets_minutes": stage_budgets,
        "absolute_deadline_utc": deadline_at_utc,
        "supervisor_parameters": {
            "heartbeat_seconds": DEFAULT_HEARTBEAT_SECONDS,
            "ledger_interval_seconds": DEFAULT_LEDGER_INTERVAL_SECONDS,
            "watchdog_timeout_seconds": DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
        },
        "project_ledger_ref": dict(project_ledger_ref),
        "tool_identity": dict(store_tool_identity),
    }
    document["identity_sha256"] = campaign_identity_sha256(document)
    return document


def ensure_store_campaign(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """按冻结的 ``inputs.store_bootstrap``／``inputs.store_campaign`` 幂等建立 Store Campaign v2，并与总计划逐项同源核对。

    建立与 seal 共用 ``claude_upgrade_seal.create_store_from_frozen_inputs``（同一组 Store 命令，``--allow-existing``）。
    """

    from tools.official_client_capture import claude_upgrade_seal

    if not claude_upgrade_seal.create_store_from_frozen_inputs(campaign_dir, manifest):
        raise ConfigurationError("总计划没有冻结 Store 建立输入（inputs.store_bootstrap／inputs.store_campaign）。")
    _store, campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    return campaign


# ---------------------------------------------------------------------------
# 七、客户端身份一致性
# ---------------------------------------------------------------------------


def require_campaign_client_roots(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    """总计划、Store Campaign、项目总账计划与计时账本计划四处 ``client`` 一致；返回核对过的身份根名称。"""

    _store, campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    ledger_root = codex_upgrade_project_ledger.find_project_ledger(Path(campaign_dir).parent, client=CLIENT)
    if ledger_root is None:
        raise ConfigurationError("Campaign 所在数据根没有 Claude 项目总账。")
    try:
        project_plan, _raw = codex_upgrade_project_ledger._load_plan(ledger_root)
    except codex_upgrade_project_ledger.ProjectLedgerError as error:
        raise ConfigurationError(f"Claude 项目总账计划不可读：{error}") from error
    ledger_dir = _orchestrator()._campaign_timing_ledger_dir(campaign_dir, manifest)
    try:
        timing_plan, _raw = codex_upgrade_timing_ledger._load_json(ledger_dir / "ledger.json", "计时账本计划")
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise ConfigurationError(f"计时账本计划不可读：{error}") from error
    sources: dict[str, Mapping[str, Any] | None] = {
        "Campaign 总计划": manifest,
        "Store Campaign": campaign,
        "项目总账计划": project_plan,
        "计时账本计划": timing_plan,
    }
    try:
        upgrade_client_adapter.require_consistent_client(CLIENT, sources)
    except upgrade_client_adapter.ClientAdapterError as error:
        raise ConfigurationError(f"Campaign 身份根的客户端不一致：{error}") from error
    return list(sources)


# ---------------------------------------------------------------------------
# 六、VC 控制制品
# ---------------------------------------------------------------------------


def first_batch_timeout(job_count: int, *, remaining_seconds: int) -> int:
    """首批动作超时：max(下限, 作业数 × 单作业预算)，不超过距截止的剩余时间（与 Codex 同一规则、同一取值）。"""

    timeout = max(FIRST_OFFICIAL_BATCH_MIN_TIMEOUT_SECONDS, job_count * FIRST_OFFICIAL_BATCH_SECONDS_PER_JOB)
    if remaining_seconds > 0:
        timeout = min(timeout, remaining_seconds)
    return max(60, timeout)


def first_batch_action(campaign_dir: Path, official_job_ids: Sequence[str], *, timeout_seconds: int) -> dict[str, Any]:
    """Formal 首批唯一的动作：受管 Python 直接调用 ``claude_upgrade.py capture-official run``（与 Codex 同形，见第六节）。"""

    from tools.official_client_capture import claude_upgrade_batch

    return claude_upgrade_batch.managed_action(
        CAPTURE_ACTION_ID,
        CAPTURE_OPERATION,
        ["capture-official", "run", "--campaign-dir", str(Path(campaign_dir).resolve(strict=False)), "--acknowledge-live-requests"],
        item_ids=list(official_job_ids),
        timeout_seconds=timeout_seconds,
    )


def create_vc_control(
    campaign_dir: Path,
    arguments: argparse.Namespace,
    controls: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    *,
    created_at_utc: str,
    reuse_official_jobs: bool,
) -> dict[str, Any]:
    """VC 总计划、VC-0 checkpoint；Formal 再写首批两件。与 Codex ``_create_initial_vc_control_artifacts`` 同一组合同。

    VC-0 的阶段收据：预检绑定 VC 总计划本身，Formal 绑定 P0 门禁收据（绝对路径，与 Codex 相同）。
    """

    timing = controls["upgrade_timing"]
    environment = controls["arm64_environment"]
    rehearsal = controls.get("job_rehearsal")
    p0_gate = controls.get("p0_gate")
    try:
        checkpoint = codex_upgrade_timing_ledger.replay(Path(timing["ledger_dir"]), str(timing["receipt"]["path"]))
        original_deadline = str(checkpoint["summary"]["total_deadline_at_utc"])
        plan = codex_upgrade_vc_artifacts.build_campaign_plan(
            campaign_id=str(arguments.campaign_id),
            campaign_mode=str(arguments.campaign_mode),
            campaign_purpose=str(arguments.campaign_purpose),
            baseline_version=str(arguments.baseline_version),
            target_version=str(arguments.target_version),
            created_at_utc=created_at_utc,
            original_deadline_at_utc=original_deadline,
            timing_checkpoint_sha256=str(timing["receipt"]["sha256"]),
            arm64_environment_sha256=str(environment["receipt"]["sha256"]),
            job_rehearsal_sha256=str(rehearsal["receipt"]["sha256"]) if isinstance(rehearsal, Mapping) else None,
            p0_gate_sha256=str(p0_gate["receipt"]["sha256"]) if isinstance(p0_gate, Mapping) else None,
        )
    except (KeyError, OSError, codex_upgrade_timing_ledger.TimingLedgerError, codex_upgrade_vc_artifacts.VCArtifactError) as error:
        raise ConfigurationError(f"无法生成 VC-0 Campaign 总计划：{error}") from error
    vc_root = ensure_private_directory(Path(campaign_dir) / claude_upgrade_vc.VC_DIRECTORY, Path(campaign_dir))
    plan_path = vc_root / "campaign-plan.json"
    claude_upgrade_vc.write_json_once(plan_path, plan)
    stage_receipt = claude_upgrade_vc.vc_control_binding(campaign_dir, plan_path)
    if arguments.campaign_mode == "formal":
        if not isinstance(p0_gate, Mapping):
            raise ConfigurationError("Formal VC-0 缺少 P0 门禁收据。")
        stage_receipt = {
            "path": str(Path(str(p0_gate["evidence_root"])) / str(p0_gate["receipt"]["path"])),
            "sha256": str(p0_gate["receipt"]["sha256"]),
        }
    try:
        vc0 = codex_upgrade_vc_artifacts.build_vc_checkpoint(
            campaign_plan=plan,
            phase="VC-0",
            status="complete",
            predecessor_checkpoint=None,
            stage_receipt=stage_receipt,
            completed_at_utc=created_at_utc,
            execute_item_ids=[],
            reuse_item_ids=[],
            live_request_count=0,
            scanned_bytes=0,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"无法生成 VC-0 checkpoint：{error}") from error
    vc0_path = claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-0")
    claude_upgrade_vc.write_json_once(vc0_path, vc0)
    result: dict[str, Any] = {
        "campaign_plan": claude_upgrade_vc.vc_control_binding(campaign_dir, plan_path),
        "vc0_checkpoint": claude_upgrade_vc.vc_control_binding(campaign_dir, vc0_path),
        "first_formal_batch": None,
        "first_campaign_run_manifest": None,
    }
    if arguments.campaign_mode != "formal":
        return result
    official = sorted(str(job["id"]) for job in jobs if isinstance(job, Mapping) and job.get("phase") == "official")
    if not official:
        raise ConfigurationError("Formal Campaign 首个 VC-1 批次没有 official Job。")
    created = _parse_utc(created_at_utc, "created_at_utc")
    deadline = _parse_utc(original_deadline, "原始截止")
    must_start = min(created + timedelta(seconds=60), deadline)
    if reuse_official_jobs:
        execute, reuse, actions = [], official, []
    else:
        timeout = first_batch_timeout(len(official), remaining_seconds=int((deadline - must_start).total_seconds()))
        execute, reuse, actions = official, [], [first_batch_action(campaign_dir, official, timeout_seconds=timeout)]
    result.update(
        claude_upgrade_vc.write_first_formal_batch(
            campaign_dir,
            plan=plan,
            execute_item_ids=execute,
            reuse_item_ids=reuse,
            actions=actions,
            compiled_at_utc=created_at_utc,
            must_start_by_utc=must_start.isoformat(timespec="seconds"),
        )
    )
    return result


# ---------------------------------------------------------------------------
# 建立 Campaign
# ---------------------------------------------------------------------------


def _require_create_arguments(arguments: argparse.Namespace) -> None:
    missing = [name for name in CREATE_REQUIRED_ARGUMENTS if getattr(arguments, name, None) in (None, "")]
    if missing:
        raise ConfigurationError("建 Campaign 缺少参数：" + "、".join("--" + name.replace("_", "-") for name in missing))
    _require_sha256(arguments.model_catalog_sha256, "--model-catalog-sha256")
    _require_sha256(arguments.target_package_sha256, "--target-package-sha256")
    _require_sha256(arguments.target_sha256, "--target-sha256")
    for flag, value in (("--entrypoint", arguments.entrypoint), ("--privacy-mode", arguments.privacy_mode), ("--oauth-org-id", arguments.oauth_org_id)):
        if not claude_upgrade_plan.SAFE_ID_RE.fullmatch(str(value)):
            raise ConfigurationError(f"{flag} 格式非法。")
    frozen = getattr(arguments, "production_inventory", None) is not None
    from_store = (
        getattr(arguments, "production_inventory_store", None) is not None
        or getattr(arguments, "production_inventory_campaign", None) is not None
    )
    if frozen == from_store:
        raise ConfigurationError(
            "生产现状清单必须二选一给出：--production-inventory（已冻结的清单），或 --production-inventory-store 与 "
            "--production-inventory-campaign（从前序 Store 的 ProfileApprovalFact 取）。"
        )
    if from_store and (arguments.production_inventory_store is None or not arguments.production_inventory_campaign):
        raise ConfigurationError("--production-inventory-store 与 --production-inventory-campaign 必须同时给出。")
    if (getattr(arguments, "predecessor_store", None) is None) != (getattr(arguments, "predecessor_campaign", None) is None):
        raise ConfigurationError("--predecessor-store 与 --predecessor-campaign 必须同时给出。")
    if getattr(arguments, "predecessor_store", None) is not None and getattr(arguments, "predecessor_binding", None) is not None:
        raise ConfigurationError("前序只能经 --predecessor-store／--predecessor-campaign 或收口冻结的绑定二选一给出。")


def _predecessor_binding(arguments: argparse.Namespace) -> dict[str, Any] | None:
    """只读复用前序：命令行给出前序 Store 时现读其 Campaign 身份；收口恢复 Formal 参数时直接沿用预检冻结的绑定。"""

    from tools.official_client_capture import claude_upgrade_predecessor

    frozen = getattr(arguments, "predecessor_binding", None)
    if frozen is not None:
        return claude_upgrade_predecessor.validate_predecessor_binding(dict(frozen), campaign_id=str(arguments.campaign_id))
    if getattr(arguments, "predecessor_store", None) is None:
        return None
    from tools.official_client_control.store import ControlStore

    try:
        campaign = ControlStore.open_read_only(Path(arguments.predecessor_store)).load_campaign(str(arguments.predecessor_campaign))
    except ControlError as error:
        raise ConfigurationError(f"前序 Store Campaign 不可读：{error}") from error
    return claude_upgrade_predecessor.validate_predecessor_binding(
        {
            "mode": claude_upgrade_predecessor.READ_ONLY_REUSE,
            "campaign_id": str(arguments.predecessor_campaign),
            "campaign_identity_sha256": str(campaign["identity_sha256"]),
        },
        campaign_id=str(arguments.campaign_id),
    )


def _source_commit(arguments: argparse.Namespace, tool_identity: Mapping[str, Any]) -> str:
    value = getattr(arguments, "source_commit", None) or tool_identity.get("git_commit")
    if not isinstance(value, str) or len(value) != 40 or not set(value) <= SHA256_HEX:
        raise ConfigurationError("建 Campaign 需要受管工具来源提交：给出 --source-commit（40 位），或在 git 工作树里运行。")
    return value


def create_campaign(arguments: argparse.Namespace) -> dict[str, Any]:
    """服务面 ``create_campaign`` 的 Claude 实现：在项目总账准入作用域内建立 Campaign 并注册（锁顺序同 Codex）。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(arguments.campaign_dir)
    mode = str(getattr(arguments, "campaign_mode", "") or "")
    target_version = str(getattr(arguments, "target_version", "") or "")
    try:
        with codex_upgrade_project_ledger.admission_scope(
            campaign_dir,
            campaign_id=str(getattr(arguments, "campaign_id", "") or ""),
            campaign_mode=mode,
            target_version=target_version,
            require=orchestrator._project_ledger_required(mode, target_version),
            client=CLIENT,
        ) as admission:
            manifest = _create_campaign_unadmitted(arguments)
            if admission is not None:
                admission.register(
                    campaign_dir,
                    campaign_id=str(manifest["campaign_id"]),
                    campaign_mode=str(manifest["campaign_mode"]),
                    target_version=str(manifest["target_version"]),
                    deadline_at_utc=orchestrator._campaign_plan_deadline(campaign_dir),
                )
    except codex_upgrade_project_ledger.ProjectLedgerError as error:
        raise ConfigurationError(f"项目总账拒绝建立 Campaign：{error}") from error
    return manifest


def created_report(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """``plan`` 建 Campaign 的结果摘要（命令行输出）。"""

    _store, campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    return {
        "schema_version": PLAN_CREATED_SCHEMA,
        "mode": "create",
        "campaign_created": True,
        "live_request_count": 0,
        "campaign_dir": str(campaign_dir),
        "campaign_id": manifest["campaign_id"],
        "campaign_mode": manifest["campaign_mode"],
        "campaign_purpose": manifest["campaign_purpose"],
        "baseline_version": manifest["baseline_version"],
        "target_version": manifest["target_version"],
        "manifest_sha256": file_sha256(Path(campaign_dir) / "campaign.json"),
        "store_campaign_identity_sha256": campaign["identity_sha256"],
        "control_receipts": sorted(manifest["control_receipts"]),
        "job_count": len(manifest["jobs"]),
        "predecessor": manifest["predecessor"],
    }


def _create_campaign_unadmitted(arguments: argparse.Namespace) -> dict[str, Any]:
    """建立只写一次的 Campaign：先校验与重放全部输入（任一不过即零写入），再冻结输入、写 VC 控制制品、建 Store，最后写总计划。"""

    from tools.official_client_capture import claude_upgrade_freeze
    from tools.official_client_control import receipts as store_receipts

    orchestrator = _orchestrator()
    scenario_payload = claude_upgrade_plan.prepare_plan_arguments(arguments)
    _require_create_arguments(arguments)
    campaign_dir = Path(arguments.campaign_dir)
    if campaign_dir.exists() or campaign_dir.is_symlink():
        raise ConfigurationError(f"Campaign 目录必须尚不存在（建立只写一次）：{campaign_dir}")
    if campaign_dir.name != arguments.campaign_id or campaign_dir.parent.name != "campaigns" or campaign_dir.parent.parent.name != "evidence":
        raise ConfigurationError("Campaign 目录必须是 <数据根>/evidence/campaigns/<campaign-id>。")
    data_root = campaign_dir.parent.parent.parent

    # ---- 控制收据：先重放全部控制输入。
    timing_control, timing_summary, checkpoint_summary = _timing_control(arguments)
    predecessor = _predecessor_binding(arguments)
    if (predecessor is not None) != (timing_summary.get("evidence_decision") == "reuse"):
        raise ConfigurationError("只读复用前序与计时账本的证据决定不一致（复用前序时账本必须是 reuse，反之不得是 reuse）。")
    controls: dict[str, Any] = {
        "upgrade_timing": timing_control,
        "arm64_environment": _environment_control(arguments, upgrade_id=str(timing_summary["upgrade_id"])),
    }

    # ---- 官方包：B2-01 冻结目录逐项复核。
    freeze_path = Path(arguments.target_package)
    try:
        verification = claude_upgrade_freeze.verify_freeze_receipt(
            freeze_path,
            expected_version=str(arguments.target_version),
            expected_binary_sha256=str(arguments.target_sha256),
            expected_freeze_sha256=str(arguments.target_package_sha256),
        )
    except claude_upgrade_freeze.FreezeError as error:
        raise ConfigurationError(f"官方冻结目录复核失败：{error}") from error
    official_identity = official_identity_from_freeze(freeze_path)
    official_artifacts = official_artifact_bindings(verification, data_root)

    # ---- 清单与 Job 展开（wire 根），证据标签在 wire 根之外校验。
    rules = claude_upgrade_plan.load_baseline_rules(arguments.rule_manifest, arguments.baseline_version)
    claude_upgrade_plan.verify_target_rules(arguments, rules)
    jobs, _plan_inputs = orchestrator._load_plan_jobs(arguments, rules)
    claude_upgrade_plan.evidence_labels_binding(arguments, rules)
    report = claude_upgrade_plan.plan_report(arguments, jobs, rules, {})
    # 显式给出来源提交时不读 git（ARM64 生产工具树不是 git 工作树）；总计划工具身份的 git_commit 记来源提交。
    tool_identity = orchestrator._tool_identity(include_git=getattr(arguments, "source_commit", None) is None)
    source_commit = _source_commit(arguments, tool_identity)
    tool_identity["git_commit"] = source_commit
    configuration = {
        **dict(report["configuration"]),
        "platform": orchestrator.CAMPAIGN_PLATFORM,
        "suite": str(arguments.suite),
        "entrypoint": str(arguments.entrypoint),
        "privacy_mode": str(arguments.privacy_mode),
        "oauth_org_id": str(arguments.oauth_org_id),
        "model_catalog_sha256": str(arguments.model_catalog_sha256),
        "official_freeze_path": str(freeze_path.resolve(strict=True)),
        "official_freeze_sha256": str(verification["freeze_sha256"]),
        "source_commit": source_commit,
    }
    if arguments.campaign_mode == "formal":
        release_control, p0_control, release = _release_and_p0_controls(arguments, upgrade_id=str(timing_summary["upgrade_id"]))
        manifest_view = {
            "target_version": str(arguments.target_version),
            "target_sha256": str(arguments.target_sha256),
            "official_identity": official_identity,
            "tool_identity": tool_identity,
            "configuration": configuration,
        }
        controls["job_rehearsal"] = _job_rehearsal_control(
            arguments, expected_contract=expected_job_contract(manifest_view, scenario_payload), release=release
        )
        controls["release_certification"] = release_control
        controls["p0_gate"] = p0_control

    # ---- 生产现状清单：已冻结的一份，或从前序 Store 的 ProfileApprovalFact 取。
    if getattr(arguments, "production_inventory", None) is not None:
        production_inventory = validate_production_inventory(
            orchestrator._read_json(Path(arguments.production_inventory), "生产现状清单")
        )
    else:
        production_inventory = extract_production_inventory(
            Path(arguments.production_inventory_store), str(arguments.production_inventory_campaign)
        )

    # ---- Store 身份输入：项目总账计划、计时账本计划与当前受管清单。
    ledger_root = codex_upgrade_project_ledger.find_project_ledger(campaign_dir.parent, client=CLIENT)
    if ledger_root is None:
        raise ConfigurationError("Campaign 所在数据根没有 Claude 项目总账：Store Campaign v2 必须绑定项目总账计划。")
    ledger_plan = ledger_root / "plan.json"
    try:
        timing_plan, _raw = codex_upgrade_timing_ledger._load_json(Path(timing_control["ledger_dir"]) / "ledger.json", "计时账本计划")
        store_manifest = store_receipts.current_tool_manifest(CLIENT)
        store_tool_identity = store_receipts.tool_identity_from_manifest(store_manifest)
        tool_bundle = store_receipts.store_bundle_from_manifest(store_manifest)
    except (codex_upgrade_timing_ledger.TimingLedgerError, ControlError) as error:
        raise ConfigurationError(f"Store Campaign 身份输入不可用：{error}") from error
    if store_manifest["tool_files_sha256"] != tool_identity["files_sha256"]:
        raise ConfigurationError("Store 现算的受管清单与编排器工具身份不一致（建立期间受管工具被改动？）。")
    deadline_at_utc = _utc_text(_parse_utc(checkpoint_summary["total_deadline_at_utc"], "计时 checkpoint 总截止"))

    # ---- 以下开始写盘（Campaign 目录此前不存在；中途失败留下的半成品没有 campaign.json，由收口归档）。
    ensure_private_directory(campaign_dir.parent)
    ensure_private_directory(campaign_dir)
    ensure_private_directory(campaign_dir / "inputs", campaign_dir)
    sources = {
        "target_scenarios": Path(arguments.scenario_manifest),
        "evidence_labels": Path(arguments.evidence_labels),
        "baseline_rules": Path(arguments.rule_manifest),
        "target_rules": claude_upgrade_plan.rule_manifest_path(str(arguments.target_version)),
        "active_release_catalog": Path(arguments.active_release_catalog),
        "active_profile": Path(arguments.active_profile),
        "static_baseline_anchors": Path(arguments.static_baseline_anchors),
        "static_baseline_reachability": Path(arguments.static_baseline_reachability),
    }
    inputs: dict[str, Any] = {}
    for name, source in sources.items():
        digest = _freeze_file(Path(source).resolve(strict=True), campaign_dir / FROZEN_INPUT_FILES[name], f"冻结输入 {name}")
        inputs[name] = {"path": FROZEN_INPUT_FILES[name], "sha256": digest}
    inventory_bytes = canonical_json_bytes(production_inventory)
    _write_bytes_once(campaign_dir / FROZEN_INPUT_FILES["production_inventory"], inventory_bytes, "生产现状清单副本")
    inputs["production_inventory"] = {"path": FROZEN_INPUT_FILES["production_inventory"], "sha256": sha256_bytes(inventory_bytes)}

    # Store bootstrap 与 Campaign v2：控制收据复制到 inputs/store-bootstrap/，外部绑定相对宿主数据根。
    bootstrap_dir = ensure_private_directory(campaign_dir / STORE_BOOTSTRAP_SOURCE_DIR, campaign_dir)
    control_sources = [
        ("timing-checkpoint.json", Path(timing_control["ledger_dir"]) / timing_control["receipt"]["path"]),
        ("arm64-environment.json", Path(controls["arm64_environment"]["evidence_root"]) / controls["arm64_environment"]["receipt"]["path"]),
    ]
    if arguments.campaign_mode == "formal":
        control_sources += [
            ("p0-gate.json", Path(controls["p0_gate"]["evidence_root"]) / controls["p0_gate"]["receipt"]["path"]),
            ("release-certification.json", Path(controls["release_certification"]["path"])),
        ]
    control_copies = []
    for name, source in control_sources:
        _freeze_file(source.resolve(strict=True), bootstrap_dir / name, f"控制收据副本 {name}")
        control_copies.append(bootstrap_dir / name)
    bootstrap = store_bootstrap_payload(
        campaign_dir, data_root=data_root, source_commit=source_commit, control_copies=control_copies, tool_bundle_sha256=tool_bundle
    )
    from tools.official_client_control.contracts import OBJECT_SCHEMA

    bootstrap_ref = {
        "object_kind": "bootstrap",
        "sha256": canonical_sha256({"schema_version": OBJECT_SCHEMA, "object_kind": "bootstrap", "payload": bootstrap}),
    }
    created_at_utc = orchestrator._utc_now()
    store_campaign = store_campaign_document(
        campaign_id=str(arguments.campaign_id),
        created_at_utc=created_at_utc,
        target_version=str(arguments.target_version),
        campaign_purpose=str(arguments.campaign_purpose),
        configuration=configuration,
        official_artifacts=official_artifacts,
        bootstrap_ref=bootstrap_ref,
        timing_plan=timing_plan,
        deadline_at_utc=deadline_at_utc,
        project_ledger_ref={"path": ledger_plan.relative_to(ledger_root.parent).as_posix(), "plan_sha256": file_sha256(ledger_plan)},
        store_tool_identity=store_tool_identity,
        tool_bundle_sha256=tool_bundle,
    )
    for name, payload in (("store_bootstrap", bootstrap), ("store_campaign", store_campaign)):
        data = canonical_json_bytes(payload)
        _write_bytes_once(campaign_dir / FROZEN_INPUT_FILES[name], data, f"冻结输入 {name}")
        inputs[name] = {"path": FROZEN_INPUT_FILES[name], "sha256": sha256_bytes(data)}

    vc_control = create_vc_control(
        campaign_dir,
        arguments,
        controls,
        report["jobs"],
        created_at_utc=created_at_utc,
        reuse_official_jobs=predecessor is not None,
    )
    manifest = {
        "schema_version": orchestrator.CAMPAIGN_SCHEMA,
        "client": CLIENT,
        "campaign_id": str(arguments.campaign_id),
        "created_at_utc": created_at_utc,
        "campaign_mode": str(arguments.campaign_mode),
        "campaign_purpose": str(arguments.campaign_purpose),
        "baseline_version": str(arguments.baseline_version),
        "target_version": str(arguments.target_version),
        "target_sha256": str(arguments.target_sha256),
        "official_identity": official_identity,
        "baseline_identity": {"baseline_version": str(arguments.baseline_version), "rules": dict(inputs["baseline_rules"])},
        "tool_identity": tool_identity,
        "control_receipts": controls,
        "inputs": inputs,
        "configuration": configuration,
        "required_rules": list(rules),
        "coverage_plan": report["coverage_plan"],
        "jobs": report["jobs"],
        "vc_control": vc_control,
        "predecessor": predecessor,
    }
    # Store 在总计划之前建立：建不成就没有 campaign.json，半成品由收口归档，不会留下「有总计划、没 Store」的 Campaign。
    ensure_store_campaign(campaign_dir, manifest)
    manifest_path = campaign_dir / "campaign.json"
    orchestrator._secure_write_json_once(manifest_path, manifest)
    _write_bytes_once(campaign_dir / "campaign.sha256", (file_sha256(manifest_path) + "\n").encode("ascii"), "Campaign 自摘要")
    loaded = orchestrator.load_campaign_manifest(campaign_dir)
    verify_control_receipts(loaded, require_complete=True, campaign_dir=campaign_dir)
    require_campaign_client_roots(campaign_dir, loaded)
    return loaded
