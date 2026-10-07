"""Claude VC-4 候选构建与冻结（Claude 升级控制面设计 B3-04）。

本文件现阶段落实第一步 ``plan-candidate-gates``。``record-candidate-build``、Store ``candidate-build-finalize`` 与
``candidate-freeze`` 是第三步：Store 的候选构建收据要引用 ``model_capability_catalog`` 对象，候选源码树要带新版本的
Wire 与暂存 Release，二者都等 B3-03 补充项产出之后再接。

一、门禁需求（``codex-post-promotion-gate-requirements/v1``，与 Codex 同一合同）
===============================================================================
从 VC-2、VC-3 的阶段结果确定性推出，不另存文件，任何时候重算都得到同一份字节：

* ``affected_rule_ids``／``inherited_rule_ids``：VC-2 阶段结果登记的 RuleMigrationLedger 分区（受影响规则含 delete
  决策的旧规则编号，与 Codex ``canonical_rule_partition`` 同口径），并核对 VC-3 阶段结果登记的是同一分区；
* ``joint_manifest_sha256``：VC-3 阶段结果的 ``joint_sha256``（ProfileApprovalFact 绑定的联合制品摘要），并由冻结的
  联合清单复算一遍；
* ``migration_manifest``：VC-2 已批准的迁移台账副本 ``classification/approved/rule-migration.json`` 的
  ``{path, sha256}``；
* ``campaign_id``、``target_version``：Campaign 总计划。

公共门禁四项（Catalog 投影与三项版本泄漏）由 ``build_gate_requirements`` 固定追加。推出后原样交给
``codex_upgrade_vc_artifacts.build_gate_plan``，该函数不改。

二、``plan-candidate-gates`` 的两种调用
=====================================
* **需求预览**（只给 ``--campaign-dir`` 与 ``--candidate-id``）：输出门禁需求全文、``requirements_sha256`` 与每项需求的
  ``requirement_sha256``，供映射作者（驱动的候选提交链或人工）编写 ``codex-post-promotion-gate-mapping/v2`` 映射；
  零写入，退出码 2。
* **生成计划**（再给 ``--candidate-source``、``--mapping``、``--output``）：旗标与 Codex 同名同义（共享的阶段重派证明按
  同一组旗标核对）；映射必须在候选源码树内，输出必须是候选源码树内尚不存在的路径；计划只写一次，落盘后读回再按
  VC-3 需求复算一遍；退出码 0。输出已存在即拒绝覆盖（与 Codex 同口径，重派证明也据此拒绝逐字重派）。

三、候选级前置门（与 Codex ``_require_candidate_in_current_revision`` 同口径，revision 以 Store 为准）
==================================================================================================
* Campaign 锁内、没有仍在执行的租约（B2-08 静止判据：停线租约视为静止）、没有环境污染标记；
* VC-3 checkpoint 可重放；Store 检查点是 ``profile_approved``；Store 唯一的 ProfileApprovalFact 就是 VC-3 阶段结果登记的
  那一条，用途等于 Campaign 用途，目标规则等于 VC-2 目标规则，批准核心与冻结联合清单逐字相同（Claude 指南 §4.4.1：
  开始实现前复算 ProfileApprovalFact、联合摘要与全部输入清单）；
* 候选 revision（B3-05 Store 部分）：Campaign 必须已有显式 revision 事实（``revision-open --initial`` 写 r1），候选等于
  当前 revision 的候选，当前 revision 未被作废（作废后处于 revision_required，只能开新 revision）；
* 计时账本：Campaign 总计划绑定的 UpgradeTimingLedger 状态为 ``active``（没有绑定或不可重放即拒绝）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层，不在 wire 与
evidence 闭包里。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.errors import ControlError

COMMAND_RESULT_SCHEMA = "claude-upgrade-candidate-result/v1"
# 候选级动作要求的 Store 检查点：当前候选 revision 内还没有 candidate_frozen（Store 只按当前 revision 计候选级事实）。
STORE_PROFILE_APPROVED = "profile_approved"
PLAN_GATES_ACTION = "plan-candidate-gates"


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _capture() -> Any:
    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def add_plan_gates_arguments(parser: argparse.ArgumentParser) -> None:
    """``plan-candidate-gates`` 的参数（``--campaign-dir``、``--candidate-id`` 由编排器统一注册）。"""

    parser.add_argument("--candidate-source", type=Path, help="候选源码树绝对目录（生成门禁计划时必需）。")
    parser.add_argument("--mapping", type=Path, help="候选源码树内的 codex-post-promotion-gate-mapping/v2 映射。")
    parser.add_argument("--output", type=Path, help="候选源码树内尚不存在的门禁执行计划路径。")


def plan_candidate_gates_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``plan-candidate-gates``：需求预览退出码 2；生成计划完成为 0。"""

    candidate_id = getattr(arguments, "candidate_id", None)
    if not candidate_id:
        raise ConfigurationError("plan-candidate-gates 必须给出 --candidate-id。")
    if not classify.SAFE_ID_RE.fullmatch(str(candidate_id)):
        raise ConfigurationError("--candidate-id 格式非法。")
    given = {name: getattr(arguments, name, None) for name in ("candidate_source", "mapping", "output")}
    if all(value is None for value in given.values()):
        return _locked(arguments.campaign_dir, lambda campaign_dir, manifest: (_requirements_preview(campaign_dir, manifest, str(candidate_id)), 2))
    if any(value is None for value in given.values()):
        raise ConfigurationError("生成门禁计划必须同时给出 --candidate-source、--mapping 与 --output。")
    return _locked(
        arguments.campaign_dir,
        lambda campaign_dir, manifest: (
            _plan_gates(campaign_dir, manifest, str(candidate_id), Path(given["candidate_source"]), Path(given["mapping"]), Path(given["output"])),
            0,
        ),
    )


def _locked(campaign_dir_value: Any, action: Callable[[Path, dict[str, Any]], tuple[dict[str, Any], int]]) -> tuple[dict[str, Any], int]:
    """Campaign 锁内执行：要求 Formal、Campaign 静止（B2-08 判据）、没有环境污染标记。"""

    orchestrator = _orchestrator()
    campaign_dir = Path(campaign_dir_value)
    orchestrator._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        orchestrator._require_quiescent_campaign_lease(campaign_dir, action="VC-4 候选动作")
        manifest = orchestrator._require_formal_campaign(campaign_dir)
        if (campaign_dir / _capture().CONTAMINATION_MARKER).exists():
            raise ConfigurationError("Campaign 已记录环境污染：先走环境隔离与恢复（B2-08），禁止 VC-4 候选动作。")
        return action(campaign_dir, manifest)


# ---------------------------------------------------------------------------
# VC-4 入口：复算 VC-3 批准、候选 revision 与计时账本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateEntry:
    """VC-4 候选级动作的已核对输入。"""

    campaign_id: str
    candidate_id: str
    candidate_revision: int
    classification: dict[str, Any]
    staged_profile: dict[str, Any]
    profile_approval_ref: dict[str, Any]
    requirements: dict[str, Any]


def candidate_entry(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, *, action: str) -> CandidateEntry:
    """候选级动作的统一入口门：VC-3 批准可复算、候选属于当前未作废 revision、计时账本 active，并推出门禁需求。"""

    plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
    claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-3")
    classification = classify.load_stage_result(campaign_dir, manifest)
    staged = profile.load_stage_result(campaign_dir, manifest)
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    status, facts = profile._store_state(store, str(store_campaign["campaign_id"]))
    if status != STORE_PROFILE_APPROVED:
        raise ConfigurationError(
            f"{action} 要求 Store 检查点为 {STORE_PROFILE_APPROVED}（当前 {status}）：VC-3 未批准，或当前候选 revision 已冻结候选。"
        )
    approval_ref = _require_profile_approval(store, facts, campaign_dir, manifest, classification, staged)
    revision = require_candidate_revision(store, store_campaign, candidate_id, action=action)
    require_timing_ledger_active(campaign_dir, manifest, action=action)
    return CandidateEntry(
        campaign_id=str(manifest["campaign_id"]),
        candidate_id=candidate_id,
        candidate_revision=revision,
        classification=classification,
        staged_profile=staged,
        profile_approval_ref=approval_ref,
        requirements=derive_gate_requirements(manifest, classification, staged),
    )


def _require_profile_approval(
    store: Any,
    facts: Mapping[str, list[dict[str, Any]]],
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    classification: Mapping[str, Any],
    staged: Mapping[str, Any],
) -> dict[str, Any]:
    """复算 ProfileApprovalFact：唯一、等于 VC-3 阶段结果登记的那条、用途与目标规则一致、批准核心与冻结联合清单相同。

    联合清单文件的摘要已由 ``profile.load_stage_result`` 核过；这里再由清单里的对象引用与批准核心复算联合摘要。
    """

    approvals = facts.get("profile_approved", [])
    if len(approvals) != 1:
        raise ConfigurationError("Store 里没有唯一的 ProfileApprovalFact。")
    approval = approvals[0]
    reference = dict(store.fact_ref(approval))
    if reference != dict(staged["store"]["profile_approval_fact_ref"]):
        raise ConfigurationError("Store 的 ProfileApprovalFact 不是 VC-3 阶段结果登记的那一条。")
    payload = approval["payload"]
    if payload.get("approval_purpose") != manifest.get("campaign_purpose"):
        raise ConfigurationError("ProfileApprovalFact 的 approval_purpose 与 Campaign 用途不一致。")
    if sorted(payload.get("target_spec_ids", [])) != sorted(classification["target_rules"]):
        raise ConfigurationError("ProfileApprovalFact 的目标规则与 VC-2 目标规则不一致。")
    joint = classify._read_json(campaign_dir / str(staged["joint_manifest"]["path"]), "联合清单")
    if (
        joint.get("joint_sha256") != staged["joint_sha256"]
        or profile._joint_sha256(joint["objects"], joint["approval_core"]) != staged["joint_sha256"]
    ):
        raise ConfigurationError("联合摘要无法由冻结的联合清单复算。")
    if any(payload.get(key) != value for key, value in joint["approval_core"].items()):
        raise ConfigurationError("ProfileApprovalFact 的批准核心与冻结联合清单不一致。")
    return reference


def require_candidate_revision(store: Any, store_campaign: Mapping[str, Any], candidate_id: str, *, action: str) -> int:
    """候选 revision 门（Store 为准）：必须已有显式 revision、候选等于当前 revision 的候选、当前 revision 未被作废。"""

    campaign = dict(store_campaign)
    if not store_receipts.candidate_revision_facts(store, campaign):
        raise ConfigurationError(
            f"{action} 需要激活的候选 revision：先执行 revision-open --initial，在 Store 写显式 r1（candidate-revision-record）。"
        )
    revision, current = store_receipts.current_candidate_revision(store, campaign)
    if current != candidate_id:
        raise ConfigurationError(
            f"{action} 拒绝：候选 {candidate_id} 不属于当前 revision r{revision}（当前候选 {current}）；被取代候选只读。"
        )
    try:
        store_receipts.require_revision_not_invalidated(store, campaign, revision, action)
    except ControlError as error:
        raise ConfigurationError(str(error)) from error
    return int(revision)


def require_timing_ledger_active(campaign_dir: Path, manifest: Mapping[str, Any], *, action: str) -> None:
    """候选级动作要求 Campaign 绑定的 UpgradeTimingLedger 为 active（与 Codex 候选级写入门同一判据，没有绑定即拒绝）。"""

    ledger_dir = _orchestrator()._campaign_timing_ledger_dir(campaign_dir, manifest)
    try:
        status = str(codex_upgrade_timing_ledger.inspect_ledger(ledger_dir).get("status"))
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise ConfigurationError(f"UpgradeTimingLedger 无法重放：{error}") from error
    if status != "active":
        raise ConfigurationError(f"{action} 拒绝：候选级动作要求 UpgradeTimingLedger 为 active（当前 {status}）。")


def derive_gate_requirements(manifest: Mapping[str, Any], classification: Mapping[str, Any], staged: Mapping[str, Any]) -> dict[str, Any]:
    """从 VC-2、VC-3 阶段结果确定性推出动态门禁需求（见文件头第一节）。"""

    affected = list(classification["affected_rules"])
    inherited = list(classification["inherited_rules"])
    for label, values in (("受影响规则", affected), ("继承规则", inherited)):
        if len(values) != len(set(values)):
            raise ConfigurationError(f"VC-2 {label}有重复编号。")
    if sorted(staged.get("affected_rules", [])) != sorted(affected) or sorted(staged.get("inherited_rules", [])) != sorted(inherited):
        raise ConfigurationError("VC-3 阶段结果登记的规则分区与 VC-2 不一致。")
    migration = classification["approved"]["rule_migration"]
    try:
        return vc_artifacts.build_gate_requirements(
            campaign_id=str(manifest["campaign_id"]),
            target_version=str(manifest["target_version"]),
            joint_manifest_sha256=str(staged["joint_sha256"]),
            affected_rule_ids=affected,
            inherited_rule_ids=inherited,
            migration_manifest={"path": str(migration["path"]), "sha256": str(migration["sha256"])},
        )
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"VC-3 门禁需求无法推出：{error}") from error


# ---------------------------------------------------------------------------
# plan-candidate-gates：需求预览与生成计划
# ---------------------------------------------------------------------------


def requirement_digests(requirements: Mapping[str, Any]) -> dict[str, str]:
    """每项门禁需求的 ``requirement_sha256``（映射逐项绑定它；与 ``build_gate_plan`` 同一摘要算法）。"""

    return {str(row["gate_id"]): vc_artifacts.digest(row) for row in requirements["requirements"]}


def _requirements_preview(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    entry = candidate_entry(campaign_dir, manifest, candidate_id, action=PLAN_GATES_ACTION)
    requirements = entry.requirements
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "awaiting_gate_mapping",
        "mode": "gate_requirements",
        "campaign_id": entry.campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": entry.candidate_revision,
        "requirements": requirements,
        "requirements_sha256": requirements["requirements_sha256"],
        "requirement_sha256s": requirement_digests(requirements),
        "live_request_count": 0,
        "next_command": (
            f"按需求编写 {vc_artifacts.GATE_MAPPING_SCHEMA} 映射并放进候选源码树，再执行 plan-candidate-gates "
            f"--campaign-dir {campaign_dir} --candidate-id {candidate_id} --candidate-source <候选源码树> "
            "--mapping <树内映射> --output <树内计划路径>"
        ),
    }


def _plan_gates(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    candidate_source: Path,
    mapping: Path,
    output: Path,
) -> dict[str, Any]:
    entry = candidate_entry(campaign_dir, manifest, candidate_id, action=PLAN_GATES_ACTION)
    source_root = trusted_candidate_source(candidate_source, campaign_dir)
    mapping_path = path_in_candidate_source(mapping, source_root, "门禁映射")
    output_path = _new_output_in_candidate_source(output, source_root)
    mapping_payload = classify._read_json(mapping_path, "post-promotion 门禁映射")
    mapping_sha256 = file_sha256(mapping_path)
    try:
        plan = vc_artifacts.build_gate_plan(entry.requirements, mapping_payload, mapping_sha256=mapping_sha256)
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    write_external_json_once(output_path, plan)
    # 复算：读回落盘的计划，按 VC-3 需求逐项校验（摘要、需求绑定与命令闭集）。
    try:
        stored = vc_artifacts.validate_gate_plan(classify._read_json(output_path, "门禁执行计划"), entry.requirements)
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"落盘的门禁执行计划复算未通过：{error}") from error
    if stored != plan:
        raise ConfigurationError("落盘的门禁执行计划与生成内容不一致。")
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "complete",
        "mode": "gate_plan",
        "campaign_id": entry.campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": entry.candidate_revision,
        "output": str(output_path),
        "gate_count": plan["gate_count"],
        "requirements_sha256": plan["requirements_sha256"],
        "mapping_sha256": plan["mapping_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "live_request_count": 0,
    }


# ---------------------------------------------------------------------------
# 候选源码树路径（与 Codex 同一判据）
# ---------------------------------------------------------------------------


def trusted_candidate_source(candidate_source: Path, campaign_dir: Path) -> Path:
    """候选源码树：可信绝对目录，与 Campaign 目录互不包含（计划输出写进源码树，不能落进不可变 Campaign）。"""

    source = Path(candidate_source)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ConfigurationError("--candidate-source 必须是可信绝对目录。")
    source_root = source.resolve(strict=True)
    campaign_root = campaign_dir.resolve(strict=True)
    if source_root.is_relative_to(campaign_root) or campaign_root.is_relative_to(source_root):
        raise ConfigurationError("Candidate 源码树与 Campaign 目录必须彼此独立。")
    return source_root


def path_in_candidate_source(path: Path, source_root: Path, label: str) -> Path:
    """解析 VC-4 源码制品，并保证它确实属于同一候选源码树（逐级拒绝符号链接；与 Codex 同名函数同一判据）。"""

    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError(f"{label}必须是非符号链接绝对路径。")
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(source_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"{label}不在候选源码树内。") from error
    current = source_root.resolve(strict=True)
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ConfigurationError(f"{label}路径包含符号链接：{current}")
    return resolved


def _new_output_in_candidate_source(output: Path, source_root: Path) -> Path:
    """门禁计划输出：候选源码树内尚不存在的绝对路径（父目录必须已存在且不是符号链接）。"""

    if not output.is_absolute():
        raise ConfigurationError("门禁执行计划输出必须是绝对路径。")
    if output.exists() or output.is_symlink():
        raise ConfigurationError("门禁执行计划输出已存在，禁止覆盖。")
    # 父目录逐级解析（拒绝符号链接、必须在源码树内）；文件名本身不能是 ``..`` 一类（那样 ``exists`` 已为真并在上面拒绝）。
    parent = path_in_candidate_source(output.parent, source_root, "门禁执行计划输出目录")
    if not parent.is_dir():
        raise ConfigurationError("门禁执行计划输出的父目录不存在。")
    return parent / output.name


def write_external_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    """在既有源码目录写一次 JSON（临时文件加硬链接发布，已存在即拒绝），不改变仓库目录权限；与 Codex 同一格式。"""

    if path.exists() or path.is_symlink():
        raise ConfigurationError(f"不可变文件已经存在，禁止覆盖：{path}")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ConfigurationError(f"输出父目录不存在或不可信：{parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ConfigurationError(f"不可变文件已经存在，禁止覆盖：{path}") from error
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
