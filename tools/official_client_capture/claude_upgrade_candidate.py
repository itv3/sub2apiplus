"""Claude VC-4 候选构建与冻结（Claude 升级控制面设计 B3-04）。

两个零请求动作：``plan-candidate-gates``（门禁需求预览与门禁执行计划）与 ``record-candidate-build``（候选同源构建复算、
候选构建收据、Store ``candidate-build-finalize`` 与 ``candidate-freeze``、revision seal、VC-4 checkpoint）。两者都经
``compile-and-run-vc-batch`` 以 VC-4 批次派发，阶段幂等重派合同在 ``claude_upgrade_batch``。

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

三、候选级前置门（与 Codex ``_require_candidate_in_current_revision`` 同口径）
============================================================================
* Campaign 锁内、没有仍在执行的租约（B2-08 静止判据：停线租约视为静止）、没有环境污染标记；
* VC-3 checkpoint 可重放；Store 检查点是 ``profile_approved``；Store 唯一的 ProfileApprovalFact 就是 VC-3 阶段结果登记的
  那一条，用途等于 Campaign 用途，目标规则等于 VC-2 目标规则，批准核心与冻结联合清单逐字相同（Claude 指南 §4.4.1：
  开始实现前复算 ProfileApprovalFact、联合摘要与全部输入清单）；
* 候选 revision（B3-05）：编排器侧（账本 ``stage_revision``＋revision 目录）与 Store 侧（``candidate_revision_recorded``）
  两份登记一致且显式（``revision-open --initial`` 同时写两份 r1；Claude 不认隐含 r1），候选等于当前 revision 的候选，
  Store 侧当前 revision 未被作废（作废后处于 revision_required，只能开新 revision）；
* 计时账本：Campaign 总计划绑定的 UpgradeTimingLedger 状态为 ``active``（没有绑定或不可重放即拒绝）。

四、``record-candidate-build``
============================
参数与 Codex 同名同义（``--candidate-id``、``--candidate-purpose``、``--candidate-source``、``--candidate-binary``、
``--runtime-image``、``--candidate-image-id``、``--build-id``、``--deployed-version``、``--target-architecture``、
``--build-parameters``、``--build-tree``、``--docker-context``、``--frontend-dist-source``、``--catalog-stage-dir``、
``--source-transition``、``--gate-plan``、``--implementation-test-root``、``--implementation-test-receipt``），全部必需；另有
``--preview``（零写入预览，不进批次）。

* 复算（全部在写入之前）：用途等于 Campaign 用途、部署版本等于目标版本、架构等于 Campaign 平台、镜像是不可变引用；
  候选源码树是独立于 Campaign 的干净 Git 提交，记源码树、测试树（``*_test.go``、``tests``／``testdata`` 下、前端
  ``.test.``／``.spec.``）与依赖锁（go.mod／go.sum 必需，前端 package.json／pnpm-lock.yaml 存在才计入）三个摘要；严格
  构建参数 ``sub2apiplus-candidate-build-parameters/v2`` 与四棵互不嵌套的树、0755 二进制；候选树里的 Claude 目录数据
  逐字节来自 VC-3 暂存 Release（第五节）；source transition 在树外、可从 Git 两端复算（Codex 同一判据）；门禁计划在
  树内、按 VC-3 门禁需求校验；实现测试收据（``implementation_tests``）重放通过，绑定本 Campaign、本候选、源码树、架构与
  affected 闭集；构建实物（build inventory、前端来源、镜像检查、能力探针）经 docker／go 复算（执行器可注入）；
  revision seal 预检（r≥2 与被取代候选全同即拒绝）。
* 写入顺序：``candidates/<候选>/build-evidence/`` 四件机器收据 → Store CandidateBuildReceipt（内容寻址，同输入复用）→
  Store ``candidate_frozen``（``official-client-validation-candidate/v2``，幂等键）→ ``control/vc/revisions/r<N>/seal.json``
  （r≥2 另写 ``candidates/<旧候选>/superseded-by.json``）→ ``candidates/<候选>/build-receipt.json``（客户端中立合同
  ``official-client-upgrade-candidate-build-receipt/v1``）→ VC-4 checkpoint（r1 在 ``control/vc/vc-4-checkpoint.json``，
  r≥2 在 revision 目录；复用项是继承规则）。finalize 与 freeze 在同一个动作里，任一步被强杀后重跑同一命令收敛。
* ``--preview``：做完全部复算与 seal 预检，输出预先算出的 Store 收据引用、候选身份摘要、验证闭集摘要、seal 的变化层与
  将写的路径，零写入，退出码 2（驱动用它替代 Codex 那种替换内部写点的预演）。

五、候选源码树里的 Claude 目录数据（驱动的候选提交据此组织）
==========================================================
* ``backend/internal/officialegress/catalogdata/claude/profiles/<版本>/<摘要>.json`` 与 ``.../wire/<版本>/<摘要>.json``：
  VC-3 暂存 Release 的两个文件，逐字节；
* ``tools/official_client_capture/claude_upgrade_wire_projection_<版本下划线>.json``：投影策略进受管树（下一次升级的
  机器草案从这里取 Active 版本的投影策略）；
* ``backend/internal/officialegress/catalogdata/claude/release-catalog.json``：VC-3 冻结的 Active 目录逐项保留（Active、
  Rollback 选择器与历史 Release 不动），``releases`` 末尾追加暂存 Release 条目，``selectors.validation_candidate`` 指向它
  （``{kind: release, release_sha256, changeset}``，不带 approval，changeset 与 production_active 不同）；
* Catalog stage 目录（``--catalog-stage-dir``）只有 ``release-stage-receipt.json``（与 Campaign 的暂存收据逐字节相同）与
  ``release-entry.json`` 两个文件。与 Codex 驱动同一布局时放在 ``docs/egress/lifecycle/claude-<版本去点>-candidate/
  catalog-stage/``，门禁映射与门禁计划放在同一 lifecycle 目录的 ``gate-mapping.json``／``gate-plan.json``（代码只要求
  两者在源码树内、stage 目录恰好两个文件）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层，不在 wire 与
evidence 闭包里。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_release
from tools.official_client_capture import claude_upgrade_revision
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_candidate_build
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture import codex_upgrade_vc_receipt
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.contracts import candidate_identity_sha256
from tools.official_client_control.errors import ControlError

COMMAND_RESULT_SCHEMA = "claude-upgrade-candidate-result/v1"
# 候选级动作要求的 Store 检查点：当前候选 revision 内还没有 candidate_frozen（Store 只按当前 revision 计候选级事实）。
STORE_PROFILE_APPROVED = "profile_approved"
STORE_CANDIDATE_SEALED = "candidate_sealed"
PLAN_GATES_ACTION = "plan-candidate-gates"
RECORD_BUILD_ACTION = "record-candidate-build"


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
    revision_record: dict[str, Any]
    classification: dict[str, Any]
    staged_profile: dict[str, Any]
    profile_approval_ref: dict[str, Any]
    requirements: dict[str, Any]


def candidate_entry(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    *,
    action: str,
    allowed_status: frozenset[str] = frozenset({STORE_PROFILE_APPROVED}),
) -> CandidateEntry:
    """候选级动作的统一入口门：VC-3 批准可复算、候选属于当前未作废 revision、计时账本 active，并推出门禁需求。

    ``allowed_status``：record-candidate-build 在 candidate-freeze 之后被强杀时 Store 已到 ``candidate_sealed``，重跑原命令
    要能收敛，这时另核当前 revision 冻结的候选就是本候选。
    """

    plan = claude_upgrade_vc.vc_campaign_plan(campaign_dir, manifest)
    claude_upgrade_vc.replay_vc_checkpoint(campaign_dir, plan, "VC-3")
    classification = classify.load_stage_result(campaign_dir, manifest)
    staged = profile.load_stage_result(campaign_dir, manifest)
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    claude_upgrade_seal._require_store_tool_current(store, store_campaign)
    status, facts = profile._store_state(store, str(store_campaign["campaign_id"]))
    if status not in allowed_status:
        raise ConfigurationError(
            f"{action} 要求 Store 检查点为 {sorted(allowed_status)}（当前 {status}）：VC-3 未批准，或当前候选 revision 已冻结候选。"
        )
    approval_ref = _require_profile_approval(store, facts, campaign_dir, manifest, classification, staged)
    # 候选 revision：编排器侧（账本 stage_revision＋revision 目录）与 Store 侧两份登记一致，候选属于当前 revision；
    # Store 侧另核当前 revision 未作废。
    revision, record = claude_upgrade_revision.require_candidate_revision(campaign_dir, manifest, action=action)
    if record is None or record["candidate_id"] != candidate_id:
        raise ConfigurationError(
            f"{action} 拒绝：候选 {candidate_id} 不属于当前 revision r{revision}"
            f"（当前候选 {record['candidate_id'] if record else None}）；被取代候选只读。"
        )
    if require_candidate_revision(store, store_campaign, candidate_id, action=action) != revision:
        raise ConfigurationError(f"{action} 拒绝：Store 的当前候选 revision 与编排器激活的 r{revision} 不一致。")
    if status == STORE_CANDIDATE_SEALED:
        frozen = [fact for fact in facts.get("candidate_frozen", []) if fact["formed_under"]["candidate_revision"] == revision]
        if len(frozen) != 1 or frozen[0]["payload"]["candidate_id"] != candidate_id:
            raise ConfigurationError(f"{action} 拒绝：当前 revision r{revision} 冻结的不是候选 {candidate_id}。")
    require_timing_ledger_active(campaign_dir, manifest, action=action)
    return CandidateEntry(
        campaign_id=str(manifest["campaign_id"]),
        candidate_id=candidate_id,
        candidate_revision=revision,
        revision_record=dict(record),
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


# ---------------------------------------------------------------------------
# record-candidate-build：同源构建复算、候选构建收据、Store finalize 与 freeze
# ---------------------------------------------------------------------------

CANDIDATE_BUILD_RECEIPT_FILENAME = "build-receipt.json"
BUILD_EVIDENCE_DIRNAME = "build-evidence"
STORE_SCRATCH_DIRNAME = "store-inputs"
FREEZE_LOGICAL_ACTION = "claude-upgrade:record-candidate-build:candidate-freeze"
VALIDATION_CANDIDATE_SCHEMA = "official-client-validation-candidate/v2"
VALIDATION_SCOPE_SCHEMA = "claude-upgrade-validation-scope/v1"
IMMUTABLE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[a-f0-9]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
ARCHITECTURE_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# 候选源码树里 Claude 目录数据的落点：暂存 Release 的 ``catalogdata/claude/...`` 落在 ``backend/internal/officialegress/`` 下，
# 投影策略落在受管树（下一次升级的机器草案从这里取 Active 版本的投影策略）。
REPOSITORY_CATALOG_PARENT = PurePosixPath("backend") / "internal" / "officialegress"
RELEASE_CATALOG_RELATIVE = REPOSITORY_CATALOG_PARENT / "catalogdata" / "claude" / "release-catalog.json"
PROJECTION_POLICY_TREE_DIRECTORY = PurePosixPath("tools") / "official_client_capture"
# 候选树内 Catalog stage 目录（驱动放在 docs/egress/lifecycle/claude-<版本去点>-candidate）只能有这两个文件。
CATALOG_STAGE_FILES = frozenset({"release-stage-receipt.json", "release-entry.json"})
# 依赖锁：Go 与前端的锁文件（Go 两个必须存在）。
DEPENDENCY_LOCK_FILES = ("backend/go.mod", "backend/go.sum", "frontend/package.json", "frontend/pnpm-lock.yaml")
REQUIRED_DEPENDENCY_LOCKS = frozenset({"backend/go.mod", "backend/go.sum"})
# 构建实物复算调用 docker／go 的执行器；None 用默认执行器（真实命令），测试注入替身。
BUILD_COMMAND_RUNNER: Callable[..., Any] | None = None
MACHINE_RECEIPTS = (
    ("build_inventory", "build-inventory.json"),
    ("frontend_provenance", "frontend-provenance.json"),
    ("image_inspection", "image-inspection.json"),
    ("capability_probe", "capability-probe.json"),
)
RECORD_BUILD_FLAGS = (
    "candidate_source", "candidate_binary", "runtime_image", "candidate_image_id", "build_id", "deployed_version",
    "target_architecture", "build_parameters", "build_tree", "docker_context", "frontend_dist_source", "catalog_stage_dir",
    "source_transition", "gate_plan", "implementation_test_root", "implementation_test_receipt",
)


def add_record_build_arguments(parser: argparse.ArgumentParser) -> None:
    """``record-candidate-build`` 的参数：与 Codex 同名同义（共享的阶段重派证明按同一组旗标核对）；``--candidate-id`` 由编排器注册。"""

    parser.add_argument("--candidate-purpose", choices=("production_replacement", "validation_only"))
    path_flags = {
        "--candidate-source": "候选源码树绝对目录（干净 Git 提交）。",
        "--candidate-binary": "候选后端二进制（0755）。",
        "--build-parameters": "严格 sub2apiplus-candidate-build-parameters/v2 构建参数。",
        "--build-tree": "实际参与编译的 build tree 绝对目录。",
        "--docker-context": "最终传给 Docker 的 context 绝对目录。",
        "--frontend-dist-source": "前端 builder 产出的原始 dist 目录。",
        "--catalog-stage-dir": "候选源码树内的 Catalog stage 目录（含 VC-3 暂存 Release 收据与 Release 条目）。",
        "--source-transition": "候选源码树外可重放的 source transition（冻结后继收据）。",
        "--gate-plan": "plan-candidate-gates 生成的候选源码树内执行计划。",
        "--implementation-test-root": "VC-4 实现测试统一收据所在的 0700 绝对目录。",
        "--implementation-test-receipt": "kind=implementation_tests 的已通过收据。",
    }
    for flag, text in path_flags.items():
        parser.add_argument(flag, type=Path, help=text)
    for flag in ("--runtime-image", "--candidate-image-id", "--build-id", "--deployed-version", "--target-architecture"):
        parser.add_argument(flag)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="零写入预览：做完全部复算与 revision seal 预检，输出将写的收据、Store 引用与路径（退出码 2）；不进批次。",
    )


def record_candidate_build_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``record-candidate-build``：复算候选同源构建，写候选构建收据，Store 追加 CandidateBuildReceipt 与 candidate_frozen，
    写 revision seal 与 VC-4 checkpoint（退出码 0）；``--preview`` 零写入预览（退出码 2，父监督器上下文里拒绝）。"""

    candidate_id = getattr(arguments, "candidate_id", None)
    if not candidate_id or not classify.SAFE_ID_RE.fullmatch(str(candidate_id)):
        raise ConfigurationError("record-candidate-build 必须给出合法的 --candidate-id。")
    missing = [name for name in ("candidate_purpose", *RECORD_BUILD_FLAGS) if getattr(arguments, name, None) in (None, "")]
    if missing:
        raise ConfigurationError("record-candidate-build 缺少参数：" + "、".join("--" + name.replace("_", "-") for name in missing))
    preview = bool(getattr(arguments, "preview", False))
    if preview and os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV) == "1":
        raise ConfigurationError("record-candidate-build --preview 是批次之外的零写入预览，不能由 campaign-run 动作派发。")
    return _locked(
        arguments.campaign_dir,
        lambda campaign_dir, manifest: (
            _record_build(campaign_dir, manifest, str(candidate_id), arguments, preview=preview),
            2 if preview else 0,
        ),
    )


def validation_scope_digest(entry: CandidateEntry, gate_plan_sha256: str) -> str:
    """VC-4 验证闭集摘要（Store 的 ``validation_scope_sha256``，VC-5 执行计划须绑定同一值）：目标规则分区、门禁需求与门禁计划。"""

    return canonical_sha256(
        {
            "schema_version": VALIDATION_SCOPE_SCHEMA,
            "campaign_id": entry.campaign_id,
            "candidate_id": entry.candidate_id,
            "target_rules": sorted(entry.classification["target_rules"]),
            "affected_rules": sorted(entry.classification["affected_rules"]),
            "inherited_rules": sorted(entry.classification["inherited_rules"]),
            "requirements_sha256": entry.requirements["requirements_sha256"],
            "gate_plan_sha256": gate_plan_sha256,
        }
    )


def _tree_entries(root: Path, predicate: Callable[[PurePosixPath], bool]) -> list[dict[str, Any]]:
    skip = _orchestrator().SKIP_DIRECTORIES
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if any(part in skip for part in relative.parts) or not predicate(relative):
            continue
        entries.append({"path": relative.as_posix(), "size": path.stat().st_size, "sha256": file_sha256(path)})
    return entries


def source_test_tree_sha256(source_root: Path) -> str:
    """测试树摘要：Go 测试文件、``tests``／``testdata`` 目录下的文件与前端测试文件（跳过与源码树摘要相同的目录）。"""

    def is_test(relative: PurePosixPath) -> bool:
        name = relative.name
        return (
            name.endswith("_test.go")
            or "tests" in relative.parts
            or "testdata" in relative.parts
            or ".test." in name
            or ".spec." in name
        )

    return _orchestrator()._fingerprint({"entries": _tree_entries(source_root, is_test)})


def dependency_lock_sha256(source_root: Path) -> str:
    """依赖锁摘要：Go 的 go.mod／go.sum（必须存在）与前端 package.json／pnpm-lock.yaml（存在才计入）。"""

    files: list[dict[str, Any]] = []
    for relative in DEPENDENCY_LOCK_FILES:
        path = source_root / relative
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConfigurationError(f"依赖锁文件不可信：{relative}")
        if not path.is_file():
            if relative in REQUIRED_DEPENDENCY_LOCKS:
                raise ConfigurationError(f"候选源码树缺少依赖锁文件：{relative}")
            continue
        files.append({"path": relative, "sha256": file_sha256(path)})
    return _orchestrator()._fingerprint({"files": files})


def _repository_path(source_root: Path, relative: PurePosixPath, label: str) -> Path:
    path = source_root.joinpath(*relative.parts)
    return path_in_candidate_source(path, source_root, label)


def verify_candidate_catalog(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    staged: Mapping[str, Any],
    source_root: Path,
    catalog_stage_dir: Path,
) -> dict[str, Any]:
    """候选源码树里的 Claude 目录数据必须逐字节来自 VC-3 暂存 Release（指南 §4.4.1：只追加目标对象，不改 Active、Rollback
    与历史 Release）；返回 Catalog stage 绑定与 Release 条目。"""

    staged_receipt = claude_upgrade_release.load_staged_release(campaign_dir, staged["staged_release"])
    staged_root = campaign_dir / claude_upgrade_release.STAGED_RELEASE_ROOT
    stage_dir = path_in_candidate_source(catalog_stage_dir, source_root, "Catalog stage 目录")
    if not stage_dir.is_dir():
        raise ConfigurationError("--catalog-stage-dir 不是目录。")
    present = {child.name for child in stage_dir.iterdir()}
    if present != CATALOG_STAGE_FILES or any((stage_dir / name).is_symlink() or not (stage_dir / name).is_file() for name in present):
        raise ConfigurationError(f"Catalog stage 目录只能有 {sorted(CATALOG_STAGE_FILES)} 两个普通文件，实际 {sorted(present)}。")
    staged_receipt_path = staged_root / claude_upgrade_release.STAGED_RECEIPT_FILENAME
    tree_receipt = stage_dir / claude_upgrade_release.STAGED_RECEIPT_FILENAME
    if tree_receipt.read_bytes() != staged_receipt_path.read_bytes():
        raise ConfigurationError(
            "候选树内 release-stage-receipt.json 与 Campaign VC-3 暂存 Release 收据不一致：画像或目录变化走批准修订或后继 Campaign。"
        )
    entry = staged_receipt["release_entry"]
    version = str(manifest["target_version"])
    mapped: dict[PurePosixPath, bytes] = {}
    for item in staged_receipt["inventory"]:
        relative = PurePosixPath(str(item["path"]))
        data = staged_root.joinpath(*relative.parts).read_bytes()
        if relative.parts[0] == "catalogdata":
            mapped[REPOSITORY_CATALOG_PARENT / relative] = data
        elif relative.as_posix() == claude_upgrade_release.PROJECTION_POLICY_COPY_FILENAME:
            name = claude_upgrade_release.PROJECTION_POLICY_FILENAME.format(underscored=version.replace(".", "_"))
            mapped[PROJECTION_POLICY_TREE_DIRECTORY / name] = data
        elif relative.as_posix() == claude_upgrade_release.RELEASE_ENTRY_FILENAME:
            mapped[PurePosixPath(stage_dir.relative_to(source_root).as_posix()) / claude_upgrade_release.RELEASE_ENTRY_FILENAME] = data
        else:
            raise ConfigurationError(f"VC-3 暂存 Release 含未登记的文件：{relative}")
    files: list[dict[str, Any]] = []
    for relative, data in sorted(mapped.items(), key=lambda pair: pair[0].as_posix()):
        target = _repository_path(source_root, relative, f"候选树内 {relative}")
        if not target.is_file() or target.read_bytes() != data:
            raise ConfigurationError(f"候选树内 {relative} 缺失或与 VC-3 暂存 Release 字节不一致。")
        files.append({"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()})
    # release-catalog.json：Active 目录逐项保留，只追加暂存 Release 条目，validation_candidate 指向它。
    prepared = profile.load_prepare_result(campaign_dir, manifest, str(staged["prepared_revision"]))
    active_path = campaign_dir / str(prepared["prepared"][profile.PREPARED_EXTRA_FILES["active_release_catalog"]]["path"])
    active = classify._read_json(active_path, "VC-3 冻结的 Active Release 目录")
    expected = json.loads(json.dumps(active, ensure_ascii=False))
    expected["releases"] = [*expected["releases"], entry]
    expected["selectors"]["validation_candidate"] = dict(staged_receipt["validation_candidate_selector"])
    catalog_path = _repository_path(source_root, RELEASE_CATALOG_RELATIVE, "候选树内 release-catalog.json")
    actual = classify._read_json(catalog_path, "候选树内 release-catalog.json")
    if actual != expected:
        raise ConfigurationError(
            "候选树内 release-catalog.json 不是「Active 目录 + 追加暂存 Release + validation_candidate 指向它」：Active、Rollback 与历史"
            " Release 不得改动。"
        )
    if expected["selectors"]["validation_candidate"].get("changeset") == expected["selectors"]["production_active"].get("changeset"):
        raise ConfigurationError("validation_candidate 与 production_active 的 changeset 相同（Go 加载器会拒绝折叠）。")
    files.append({"path": RELEASE_CATALOG_RELATIVE.as_posix(), "sha256": file_sha256(catalog_path)})
    files.sort(key=lambda item: item["path"])
    return {
        "binding": {
            "path": str(tree_receipt),
            "sha256": file_sha256(tree_receipt),
            "catalog_tree_sha256": _orchestrator()._fingerprint({"files": files}),
            "release_catalog_sha256": file_sha256(catalog_path),
        },
        "entry": dict(entry),
        "staged_receipt": staged_receipt,
        "derivation_sha256": str(prepared["prepared"][profile.PREPARED_EXTRA_FILES["derivation"]]["sha256"]),
        "model_capability_catalog_sha256": str(prepared["release"]["model_capability_catalog_sha256"]),
    }


def _no_candidate_attempts(campaign_dir: Path) -> None:
    """VC-4 只登记构建：Campaign 里不得已有任何候选 attempt（候选采集属于 VC-5）。"""

    root = campaign_dir / "candidates"
    if not root.exists():
        return
    for candidate_root in root.iterdir():
        attempts = candidate_root / "attempts"
        if attempts.exists() and (attempts.is_symlink() or any(attempts.iterdir())):
            raise ConfigurationError("VC-4 已出现 Candidate attempt，禁止补写构建收据。")


def _implementation_binding(
    entry: CandidateEntry,
    manifest: Mapping[str, Any],
    root: Path,
    receipt_path: Path,
    *,
    git_commit: str,
    source_tree_sha256: str,
    target_architecture: str,
    upgrade_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """实现测试收据：重放后核对 Campaign、Candidate、源码树、架构与 affected 门禁闭集（与 Codex 同一判据）。"""

    relative = _control_receipt_relative(root, receipt_path, "VC-4 实现测试收据")
    try:
        receipt = codex_upgrade_vc_receipt.replay(root, relative)
    except (OSError, codex_upgrade_vc_receipt.VCReceiptError) as error:
        raise ConfigurationError(f"VC-4 实现测试收据未通过：{error}") from error
    subject = receipt.get("subject")
    assertions = receipt.get("assertions")
    expected_affected = {row["gate_id"] for row in entry.requirements["requirements"] if row["kind"] == "affected_rule"}
    actual_affected = {
        row["gate_id"] for row in (assertions.get("gates", []) if isinstance(assertions, Mapping) else []) if row.get("kind") == "affected"
    }
    if (
        receipt.get("kind") != "implementation_tests"
        or receipt.get("status") != "passed"
        or not isinstance(subject, Mapping)
        or subject.get("upgrade_id") != upgrade_id
        or subject.get("campaign_id") != manifest.get("campaign_id")
        or subject.get("campaign_purpose") != manifest.get("campaign_purpose")
        or subject.get("baseline_version") != manifest.get("baseline_version")
        or subject.get("target_version") != manifest.get("target_version")
        or subject.get("candidate_id") != entry.candidate_id
        or subject.get("attempt_id") is not None
        or not isinstance(assertions, Mapping)
        or assertions.get("git_commit") != git_commit
        or assertions.get("source_tree_sha256") != source_tree_sha256
        or assertions.get("target_architecture") != target_architecture
        or actual_affected != expected_affected
    ):
        raise ConfigurationError("VC-4 实现测试收据未绑定当前 Campaign、Candidate、源码树、架构或 affected 闭集。")
    resolved = root.resolve(strict=True)
    path = resolved / relative
    binding = {
        "evidence_root": str(resolved),
        "receipt": {"path": relative, "sha256": file_sha256(path), "bytes": path.stat().st_size},
        "receipt_digest": receipt["receipt_digest"],
    }
    return binding, receipt


def _write_or_verify_machine_receipt(path: Path, payload: Mapping[str, Any], campaign_dir: Path) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or classify._read_json(path, "构建机器收据") != dict(payload):
            raise ConfigurationError(f"构建机器收据已经存在且内容不一致：{path}")
    else:
        claude_upgrade_vc.write_json_once(path, payload)
    return {
        "path": path.relative_to(campaign_dir).as_posix(),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
        "receipt_digest": payload["receipt_digest"],
    }


def _store_artifact_inventory(image_inspection: Mapping[str, Any], source_tree_sha256: str) -> dict[str, Any]:
    """Store CandidateBuildReceipt 的制品清单：镜像内逐文件路径（去掉开头的 /）、摘要、大小，绑定同一源码树。"""

    entries = [
        {
            "role": PurePosixPath(path).name,
            "binding": {"path": path.lstrip("/"), "sha256": row["sha256"], "bytes": row["size"]},
            "source_sha256": source_tree_sha256,
        }
        for path, row in sorted(image_inspection["files"].items(), key=lambda pair: pair[0].lstrip("/"))
    ]
    return {
        "entries": entries,
        "file_count": len(entries),
        "total_bytes": sum(item["binding"]["bytes"] for item in entries),
        "inventory_sha256": canonical_sha256(entries),
    }


def _store_finalize(session: Any, store: Any, campaign_id: str, build_input: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Store CandidateBuildReceipt：同输入重跑复用已写收据（CLI 没有 allow-existing，按内容寻址预先算出引用核对）。"""

    expected = store_receipts.build_candidate_build_receipt(store, campaign_id, dict(build_input))
    reference = {"receipt_kind": "candidate_build", "sha256": canonical_sha256(expected)}
    path = store.receipt_path(reference)
    if path.is_file() and not path.is_symlink():
        store.load_receipt(reference)
        return reference, True
    outcome = session._with_input(
        build_input,
        lambda input_path: claude_upgrade_seal.store_cli(
            ["candidate-build-finalize", "--store", session.root, "--campaign", session.campaign_id, "--input", input_path]
        ),
    )
    if dict(outcome) != reference:
        raise ConfigurationError("Store 写入的 CandidateBuildReceipt 引用与按输入预先算出的不一致。")
    return reference, False


def _record_build(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    arguments: argparse.Namespace,
    *,
    preview: bool = False,
) -> dict[str, Any]:
    """record-candidate-build 的动作体（见模块说明第四节）；``preview`` 为真时做完全部复算与 revision seal 预检后返回，零写入。"""

    orchestrator = _orchestrator()
    purpose = str(arguments.candidate_purpose)
    target_version = str(manifest["target_version"])
    platform = str((manifest.get("configuration") or {}).get("platform", ""))
    if purpose != manifest.get("campaign_purpose"):
        raise ConfigurationError("Candidate 构建用途与 Campaign 冻结用途不一致。")
    if arguments.deployed_version != target_version:
        raise ConfigurationError("--deployed-version 必须等于 Campaign 目标版本。")
    architecture = str(arguments.target_architecture)
    if not ARCHITECTURE_RE.fullmatch(architecture) or architecture != platform:
        raise ConfigurationError(f"--target-architecture 必须等于 Campaign 平台 {platform}。")
    if not classify.SAFE_ID_RE.fullmatch(str(arguments.build_id)):
        raise ConfigurationError("--build-id 格式非法。")
    runtime_image = str(arguments.runtime_image)
    image_id = str(arguments.candidate_image_id)
    if not IMMUTABLE_IMAGE_RE.fullmatch(runtime_image):
        raise ConfigurationError("--runtime-image 必须是 repository@sha256:<digest>。")
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise ConfigurationError("--candidate-image-id 格式非法。")
    entry = candidate_entry(
        campaign_dir, manifest, candidate_id, action=RECORD_BUILD_ACTION, allowed_status=frozenset({STORE_PROFILE_APPROVED, STORE_CANDIDATE_SEALED})
    )
    _no_candidate_attempts(campaign_dir)

    # 源码树：独立于 Campaign 的干净 Git 提交。
    source_root = trusted_candidate_source(Path(arguments.candidate_source), campaign_dir)
    git_commit = orchestrator._git_commit(source_root)
    if git_commit is None or not re.fullmatch(r"[0-9a-f]{40}", git_commit):
        raise ConfigurationError("候选源码树缺少可复算的完整 Git commit。")
    _require_clean_candidate_source(source_root)
    source_tree = orchestrator._directory_tree_digest(source_root)
    source_identity = {
        "root": str(source_root),
        "git_commit": git_commit,
        "tree_sha256": source_tree,
        "test_tree_sha256": source_test_tree_sha256(source_root),
        "dependency_lock_sha256": dependency_lock_sha256(source_root),
        "clean": True,
    }

    # 构建参数与四棵树（与 Codex 同一组严格判据，复用 sub2apiplus 的构建复算）。
    binary_binding = _external_file_binding(Path(arguments.candidate_binary), "Candidate 二进制")
    binary_path = Path(binary_binding["path"])
    if stat.S_IMODE(binary_path.stat().st_mode) != 0o755:
        raise ConfigurationError("Candidate 二进制必须严格为 0755。")
    build_parameters, _parameters_binding = _external_json_object(Path(arguments.build_parameters), "Candidate 构建参数")
    roots: list[Path] = []
    for value, label in (
        (Path(arguments.build_tree), "--build-tree"),
        (Path(arguments.docker_context), "--docker-context"),
        (Path(arguments.frontend_dist_source), "--frontend-dist-source"),
    ):
        if not value.is_absolute() or value.is_symlink() or not value.is_dir():
            raise ConfigurationError(f"{label} 必须是可信绝对目录。")
        roots.append(value.resolve(strict=True))
    build_tree, docker_context, frontend_dist_source = roots
    build_roots = (source_root, build_tree, docker_context, frontend_dist_source)
    if len(set(build_roots)) != len(build_roots) or any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(build_roots)
        for right in build_roots[index + 1 :]
    ):
        raise ConfigurationError("源码树、build tree、Docker context 与原始 dist 必须是互不嵌套的独立目录。")
    try:
        build_parameters = codex_upgrade_candidate_build.validate_build_parameters(
            build_parameters,
            candidate_id=candidate_id,
            source_root=source_root,
            git_commit=git_commit,
            binary_path=binary_path,
            binary_sha256=binary_binding["sha256"],
            binary_bytes=binary_binding["bytes"],
            build_tree=build_tree,
            docker_context=docker_context,
            frontend_dist_source=frontend_dist_source,
            target_architecture=architecture,
            image_id=image_id,
        )
    except (OSError, codex_upgrade_candidate_build.CandidateBuildError) as error:
        raise ConfigurationError(f"Candidate 严格构建参数未通过：{error}") from error

    # Claude 目录数据、source transition、门禁计划与实现测试。
    catalog = verify_candidate_catalog(campaign_dir, manifest, entry.staged_profile, source_root, Path(arguments.catalog_stage_dir))
    transition, transition_binding = _external_json_object(Path(arguments.source_transition), "Candidate source transition")
    transition_path = Path(transition_binding["path"])
    _require_outside_candidate_source(source_root, transition_path, "Candidate source transition")
    _validate_candidate_source_transition(source_root, transition, git_commit=git_commit)
    gate_plan_path = path_in_candidate_source(Path(arguments.gate_plan), source_root, "post-promotion 门禁执行计划")
    try:
        gate_plan = vc_artifacts.validate_gate_plan(classify._read_json(gate_plan_path, "门禁执行计划"), entry.requirements)
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    ledger_dir = orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    upgrade_id = str(codex_upgrade_timing_ledger.inspect_ledger(ledger_dir).get("upgrade_id"))
    implementation_binding, implementation = _implementation_binding(
        entry,
        manifest,
        Path(arguments.implementation_test_root),
        Path(arguments.implementation_test_receipt),
        git_commit=git_commit,
        source_tree_sha256=source_tree,
        target_architecture=architecture,
        upgrade_id=upgrade_id,
    )

    # 构建实物：build inventory、前端来源、镜像检查与能力探针（docker／go 命令经可注入的执行器）。
    try:
        inventory_receipt = codex_upgrade_candidate_build.build_inventory_receipt(
            build_parameters, candidate_id=candidate_id, image_id=image_id, source_root=source_root,
            build_tree=build_tree, docker_context=docker_context, binary_path=binary_path,
        )
        frontend_receipt = codex_upgrade_candidate_build.build_frontend_provenance(
            build_parameters, candidate_id=candidate_id, image_id=image_id, source_root=source_root,
            git_commit=git_commit, build_tree=build_tree, frontend_dist_source=frontend_dist_source,
        )
        image_receipt = codex_upgrade_candidate_build.build_image_inspection(
            build_parameters, candidate_id=candidate_id, runtime_image=runtime_image, image_id=image_id,
            binary_path=binary_path, docker_context=docker_context, git_commit=git_commit,
            target_architecture=architecture, runner=BUILD_COMMAND_RUNNER,
        )
        capability_receipt = codex_upgrade_candidate_build.build_capability_probe(
            candidate_id=candidate_id, image_id=image_id, runner=BUILD_COMMAND_RUNNER
        )
    except (OSError, codex_upgrade_candidate_build.CandidateBuildError) as error:
        raise ConfigurationError(f"Candidate 构建实物复算未通过：{error}") from error
    build_inputs = None
    if "input_provenance" in build_parameters:
        try:
            build_inputs = codex_upgrade_candidate_build.collect_implementation_inputs(
                build_parameters,
                source_tree_sha256=source_tree,
                requirements_sha256=entry.requirements["requirements_sha256"],
                go_version=image_receipt["go_build_info"]["go_version"],
            )
            codex_upgrade_vc_receipt.validate_build_input_binding(implementation_binding, build_inputs)
        except (OSError, codex_upgrade_candidate_build.CandidateBuildError, codex_upgrade_vc_receipt.VCReceiptError) as error:
            raise ConfigurationError(f"实现测试构建输入复核未通过：{error}") from error
    elif "reuse" in implementation["assertions"]:
        raise ConfigurationError("复用实现测试的候选必须冻结完整构建输入来源。")

    candidate_root = campaign_dir / "candidates" / candidate_id
    evidence_root = candidate_root / BUILD_EVIDENCE_DIRNAME
    machine_payloads = {
        "build_inventory": inventory_receipt,
        "frontend_provenance": frontend_receipt,
        "image_inspection": image_receipt,
        "capability_probe": capability_receipt,
    }

    # Store 输入（纯计算）：CandidateBuildReceipt 的输入与按内容寻址预先算出的引用、candidate_frozen 的 payload。
    # 时刻取实现测试收据的签发时刻，重跑逐字相同。
    stage_store = entry.staged_profile["store"]
    approval_refs = {key: dict(stage_store[key]) for key in ("profile_schema_ref", "snapshot_ref", "release_artifact_ref", "support_envelope_ref")}
    catalog_ref = dict(stage_store["model_capability_catalog_ref"])
    toolchain = sorted(
        {
            f"go={image_receipt['go_build_info']['go_version']}",
            f"node={build_parameters['frontend']['node_version']}",
            f"pnpm={build_parameters['frontend']['pnpm_version']}",
        }
    )
    scope = validation_scope_digest(entry, str(gate_plan["plan_sha256"]))
    manifest_digest = "sha256:" + runtime_image.rsplit("sha256:", 1)[-1]
    completed_at = str(implementation["issued_at_utc"])
    parameters_copy = json.loads(json.dumps(build_parameters, ensure_ascii=False))
    build_input = {
        "candidate_id": candidate_id,
        "candidate_purpose": purpose,
        "profile_approval_ref": dict(entry.profile_approval_ref),
        "release_artifact_ref": approval_refs["release_artifact_ref"],
        "support_envelope_ref": approval_refs["support_envelope_ref"],
        "source_identity": {
            "git_commit": git_commit,
            "source_tree_sha256": source_tree,
            "test_tree_sha256": source_identity["test_tree_sha256"],
            "dependency_lock_sha256": source_identity["dependency_lock_sha256"],
            "clean": True,
        },
        "profile_identity": {
            **approval_refs,
            "model_capability_catalog_ref": catalog_ref,
            "profile_digest": str(catalog["entry"]["profile"]["sha256"]),
        },
        "validation_scope_sha256": scope,
        "build_identity": {
            "build_id": str(arguments.build_id),
            "deployed_version": target_version,
            "target_architecture": architecture,
            "toolchain": toolchain,
            "parameters": parameters_copy,
            "parameters_sha256": canonical_sha256(parameters_copy),
        },
        "artifact_inventory": _store_artifact_inventory(image_receipt, source_tree),
        "image": {"reference": runtime_image, "image_digest": manifest_digest, "image_id": image_id, "manifest_digest": manifest_digest},
        "completed_at_utc": completed_at,
    }
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    store_campaign_id = str(store_campaign["campaign_id"])
    try:
        expected_build_ref = {
            "receipt_kind": "candidate_build",
            "sha256": canonical_sha256(store_receipts.build_candidate_build_receipt(store, store_campaign_id, dict(build_input))),
        }
    except ControlError as error:
        raise ConfigurationError(f"Store 会拒绝 CandidateBuildReceipt：{error}") from error
    freeze = {
        "schema_version": VALIDATION_CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_purpose": purpose,
        "candidate_build_receipt_ref": expected_build_ref,
        "profile_approval_ref": dict(entry.profile_approval_ref),
        "release_artifact_ref": approval_refs["release_artifact_ref"],
        "support_envelope_ref": approval_refs["support_envelope_ref"],
        "source_tree_sha256": source_tree,
        "test_tree_sha256": source_identity["test_tree_sha256"],
        "dependency_lock_sha256": source_identity["dependency_lock_sha256"],
        "target_architecture": architecture,
        "build_id": str(arguments.build_id),
        "image_digest": manifest_digest,
        "identity_sha256": "",
    }
    freeze["identity_sha256"] = candidate_identity_sha256(freeze)

    # revision seal 预检（任何写入之前）：r≥2 与被取代候选的源码层、构建层全部相同即拒绝，不留 Store 冻结事实。
    seal_identity = {
        "candidate_commit": git_commit,
        "source_tree_sha256": source_tree,
        "image_id": image_id,
        "binary_sha256": binary_binding["sha256"],
        "image_digest": manifest_digest,
        "build_parameters_sha256": vc_artifacts.digest(build_parameters),
        "build_parameters_input_sha256": codex_upgrade_candidate_build.digest(
            codex_upgrade_candidate_build.implementation_parameter_projection(build_parameters)
        ),
        "vc3_stage_receipt_sha256": str(entry.revision_record["vc3_stage_receipt"]["sha256"]),
    }
    seal_check = claude_upgrade_revision.seal_candidate_revision(
        campaign_dir, entry.revision_record, build_receipt_sha256="0" * 64, write=False, **seal_identity
    )
    checkpoint_path = claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-4", revision=entry.candidate_revision)
    revision_dir = orchestrator._candidate_revision_dir(campaign_dir, entry.candidate_revision)
    if preview:
        writes = [
            *(f"candidates/{candidate_id}/{BUILD_EVIDENCE_DIRNAME}/{filename}" for _name, filename in MACHINE_RECEIPTS),
            f"candidates/{candidate_id}/{CANDIDATE_BUILD_RECEIPT_FILENAME}",
            (revision_dir / claude_upgrade_revision.REVISION_SEAL_FILENAME).relative_to(campaign_dir).as_posix(),
            checkpoint_path.relative_to(campaign_dir).as_posix(),
        ]
        supersedes = entry.revision_record.get("supersedes")
        if isinstance(supersedes, Mapping):
            writes.append(f"candidates/{supersedes['candidate_id']}/{claude_upgrade_revision.SUPERSEDED_MARKER_FILENAME}")
        return {
            "schema_version": COMMAND_RESULT_SCHEMA,
            "status": "preview",
            "mode": "record_build_preview",
            "campaign_id": entry.campaign_id,
            "candidate_id": candidate_id,
            "candidate_revision": entry.candidate_revision,
            "source": source_identity,
            "catalog_stage": catalog["binding"],
            "release_sha256": str(catalog["entry"]["release_sha256"]),
            "gate_plan": {"path": str(gate_plan_path), "sha256": file_sha256(gate_plan_path), "plan_sha256": gate_plan["plan_sha256"]},
            "implementation_tests": implementation_binding,
            "machine_receipt_digests": {name: payload["receipt_digest"] for name, payload in machine_payloads.items()},
            "store": {
                "candidate_build_receipt_ref": expected_build_ref,
                "candidate_identity_sha256": freeze["identity_sha256"],
                "validation_scope_sha256": scope,
                "facts_to_append": ["candidate_frozen"],
            },
            "revision_seal": {"changed_layers": seal_check["changed_layers"], "superseded": seal_check["superseded"]},
            "writes": sorted(writes),
            "live_request_count": 0,
        }

    # 写入：构建机器收据 → Store CandidateBuildReceipt 与 candidate_frozen → revision seal → 候选构建收据 → VC-4 checkpoint。
    ensure_private_directory(evidence_root, campaign_dir)
    machine = {
        name: _write_or_verify_machine_receipt(evidence_root / filename, machine_payloads[name], campaign_dir)
        for name, filename in MACHINE_RECEIPTS
    }
    session = claude_upgrade_seal._StoreSession(Path(store.root), store_campaign_id, candidate_root / STORE_SCRATCH_DIRNAME)
    try:
        build_ref, build_reused = _store_finalize(session, store, store_campaign_id, build_input)
    except ControlError as error:
        raise ConfigurationError(f"Store 拒绝 CandidateBuildReceipt：{error}") from error
    if build_ref != expected_build_ref:
        raise ConfigurationError("Store 写入的 CandidateBuildReceipt 引用与预先算出的不一致。")
    fact = session.record_fact("candidate-freeze", "candidate_frozen", freeze, issued_at=completed_at, logical_action=FREEZE_LOGICAL_ACTION)
    status = session.status()
    if status.get("checkpoint") != STORE_CANDIDATE_SEALED:
        raise ConfigurationError(f"candidate_frozen 之后 Store 检查点应为 {STORE_CANDIDATE_SEALED}，实际 {status.get('checkpoint')}。")

    # 编排器候选构建收据（客户端中立合同，带 client）；重跑只豁免签发时刻与自摘要。
    try:
        receipt = vc_artifacts.build_official_client_candidate_build_receipt(
            client="claude",
            campaign_id=entry.campaign_id,
            campaign_manifest_sha256=file_sha256(campaign_dir / "campaign.json"),
            candidate_id=candidate_id,
            candidate_revision=entry.candidate_revision,
            candidate_purpose=purpose,
            target_version=target_version,
            deployed_version=target_version,
            target_architecture=architecture,
            source=source_identity,
            binary=binary_binding,
            image={"reference": runtime_image, "manifest_digest": manifest_digest, "image_id": image_id},
            build_id=str(arguments.build_id),
            toolchain=toolchain,
            build_parameters=build_parameters,
            profile={
                "profile_digest": str(catalog["entry"]["profile"]["sha256"]),
                "derivation_receipt_sha256": catalog["derivation_sha256"],
                "wire_sha256": str(catalog["entry"]["wire"]["sha256"]),
                "model_capability_catalog_sha256": catalog["model_capability_catalog_sha256"],
                "release_sha256": str(catalog["entry"]["release_sha256"]),
                "bundle_sha256": str(catalog["entry"]["bundle_sha256"]),
            },
            catalog_stage=catalog["binding"],
            source_transition={"path": str(transition_path), "sha256": transition_binding["sha256"]},
            gate_requirements={
                "requirements_sha256": entry.requirements["requirements_sha256"],
                "vc2_stage_result": {"path": classify.STAGE_RESULT_RELATIVE.as_posix(), "sha256": file_sha256(campaign_dir / classify.STAGE_RESULT_RELATIVE)},
                "vc3_stage_result": {"path": profile.STAGE_RESULT_RELATIVE.as_posix(), "sha256": file_sha256(campaign_dir / profile.STAGE_RESULT_RELATIVE)},
            },
            gate_plan={
                "path": str(gate_plan_path),
                "sha256": file_sha256(gate_plan_path),
                "plan_sha256": gate_plan["plan_sha256"],
                "requirements_sha256": gate_plan["requirements_sha256"],
            },
            implementation_tests=implementation_binding,
            build_inventory=machine["build_inventory"],
            frontend_provenance=machine["frontend_provenance"],
            image_inspection=machine["image_inspection"],
            capability_probe=machine["capability_probe"],
            validation_scope_sha256=scope,
            store={
                "profile_approval_ref": dict(entry.profile_approval_ref),
                "release_artifact_ref": approval_refs["release_artifact_ref"],
                "support_envelope_ref": approval_refs["support_envelope_ref"],
                "model_capability_catalog_ref": catalog_ref,
                "candidate_build_receipt_ref": build_ref,
                "candidate_frozen_ref": dict(fact["fact_ref"]),
            },
            built_at_utc=orchestrator._utc_now(),
            build_inputs=build_inputs,
        )
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"候选构建收据无法生成：{error}") from error
    # revision seal（与 Codex 同序：先 seal、再构建收据与 checkpoint）：绑定构建收据去掉签发时刻与自摘要的摘要。
    seal = claude_upgrade_revision.seal_candidate_revision(
        campaign_dir,
        entry.revision_record,
        build_receipt_sha256=vc_artifacts.digest({key: value for key, value in receipt.items() if key not in {"built_at_utc", "receipt_digest"}}),
        **seal_identity,
    )
    output = candidate_root / CANDIDATE_BUILD_RECEIPT_FILENAME
    orchestrator._write_or_verify_json_ignoring(output, receipt, volatile=("built_at_utc", "receipt_digest"))
    stored = vc_artifacts.validate_official_client_candidate_build_receipt(classify._read_json(output, "候选构建收据"))
    # VC-4 checkpoint：阶段收据是候选构建收据，完成时刻取它的签发时刻（冻结值，重跑逐字相同）；继承规则的门禁记为复用项。
    checkpoint = claude_upgrade_vc.complete_candidate_phase(
        campaign_dir,
        manifest,
        phase="VC-4",
        revision=entry.candidate_revision,
        stage_receipt_path=output,
        completed_at_utc=str(stored["built_at_utc"]),
        execute_item_ids=[],
        reuse_item_ids=list(entry.requirements.get("inherited_rule_ids", [])),
        live_request_count=0,
        scanned_bytes=0,
    )
    return {
        "schema_version": COMMAND_RESULT_SCHEMA,
        "status": "complete",
        "mode": "record_build",
        "campaign_id": entry.campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": entry.candidate_revision,
        "build_receipt": str(output),
        "receipt_digest": stored["receipt_digest"],
        "source_tree_sha256": source_tree,
        "image_reference": runtime_image,
        "release_sha256": str(catalog["entry"]["release_sha256"]),
        "revision_seal": {
            "path": (revision_dir / claude_upgrade_revision.REVISION_SEAL_FILENAME).relative_to(campaign_dir).as_posix(),
            "seal_sha256": seal["seal_sha256"],
            "changed_layers": seal["changed_layers"],
        },
        "vc4_checkpoint": {
            "path": checkpoint_path.relative_to(campaign_dir).as_posix(),
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        },
        "store": {
            "checkpoint": STORE_CANDIDATE_SEALED,
            "candidate_build_receipt_ref": build_ref,
            "candidate_build_receipt_reused": build_reused,
            "candidate_frozen_ref": dict(fact["fact_ref"]),
            "candidate_frozen_reused": bool(fact["reused"]),
        },
        "live_request_count": 0,
    }


# ---------------------------------------------------------------------------
# 逐字照搬自 codex_upgrade.py 的通用函数与常量（record-candidate-build 用；测试锁定与 Codex 源码逐字一致，
# Codex 改动时那条测试会提醒同步或登记分叉）：Candidate source transition（冻结后继收据）的 Git 两端复算、
# 源码树 clean 检查、外部小文件绑定与控制收据相对路径。
# ---------------------------------------------------------------------------

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
FREEZE_SUCCESSOR_RESULT_PASSED = "passed_local_evidence_successor"


FREEZE_SUCCESSOR_RESULT_PASSED_WITH_DELETIONS = "passed_with_deletions"


FREEZE_DELETION_PROOF_ALGORITHM = "deletion-proof/v1"


FREEZE_REFERENCE_SCAN_ALGORITHM = "reference-scan/v1"


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_blob_sha256(root: Path, revision: str, relative: str) -> str | None:
    """返回某次提交内普通 blob 的 SHA-256；不存在或非 blob 时返回空。"""

    try:
        object_id = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"{revision}:{relative}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        ).stdout.strip()
        object_type = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-t", object_id],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        ).stdout.strip()
        if object_type != "blob":
            return None
        content = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", object_id],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return hashlib.sha256(content).hexdigest()


def _validate_freeze_deletion_proof(
    transition: Mapping[str, Any],
    *,
    repository_root: Path,
    label: str,
) -> None:
    """B1：删除冻结路径的后继收据必须携带三项证明。

    三项分别是：非空删除原因；每个被删冻结路径的工作树无引用扫描
    （``reference-scan/v1``，Python import／属性引用、Shell 调用、JSON 动作命令
    字段均为空）；历史读取兼容证明（承接历史收据读取的 Python 模块仍存在于
    仓库且摘要一致）。没有删除冻结路径的收据不得携带 ``deletion_proof``。
    """

    deleted = transition.get("deleted_frozen_paths")
    proof = transition.get("deletion_proof")
    if not isinstance(deleted, list) or any(
        not isinstance(item, str) or not item for item in deleted
    ):
        raise ConfigurationError(f"{label} deleted_frozen_paths 非法。")
    if not deleted and proof is None:
        return
    if deleted and transition.get("result") != FREEZE_SUCCESSOR_RESULT_PASSED_WITH_DELETIONS:
        raise ConfigurationError(
            f"{label} 删除了冻结路径，result 必须是 {FREEZE_SUCCESSOR_RESULT_PASSED_WITH_DELETIONS}。"
        )
    # 未登记路径的删除也可以携带证明；此时证明里不得声称任何路径是冻结路径。
    if (
        not isinstance(proof, Mapping)
        or set(proof) != {"algorithm", "reason", "deleted_paths", "historical_readers"}
        or proof.get("algorithm") != FREEZE_DELETION_PROOF_ALGORITHM
        or not isinstance(proof.get("reason"), str)
        or not proof["reason"].strip()
        or not isinstance(proof.get("deleted_paths"), list)
        or not isinstance(proof.get("historical_readers"), list)
        or not proof["historical_readers"]
    ):
        raise ConfigurationError(f"{label} deletion_proof 结构、原因或历史读取器缺失。")
    covered: dict[str, bool] = {}
    for item in proof["deleted_paths"]:
        scan = item.get("reference_scan") if isinstance(item, Mapping) else None
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path", "frozen", "last_sha256", "reference_scan"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or not isinstance(item.get("frozen"), bool)
            or not isinstance(scan, Mapping)
            or set(scan) != {"algorithm", "patterns", "scopes", "references"}
            or scan.get("algorithm") != FREEZE_REFERENCE_SCAN_ALGORITHM
            or not isinstance(scan.get("patterns"), list)
            or not scan["patterns"]
            or scan.get("references") != []
        ):
            raise ConfigurationError(f"{label} 删除路径的无引用扫描证明非法。")
        covered[str(item["path"])] = bool(item["frozen"])
    if any(covered.get(path) is not True for path in deleted):
        raise ConfigurationError(f"{label} 存在没有无引用证明的冻结删除路径。")
    if any(frozen and path not in deleted for path, frozen in covered.items()):
        raise ConfigurationError(f"{label} deletion_proof 声称的冻结删除路径与 deleted_frozen_paths 不一致。")
    for reader in proof["historical_readers"]:
        relative = reader.get("path") if isinstance(reader, Mapping) else None
        if (
            not isinstance(reader, Mapping)
            or set(reader) != {"path", "sha256"}
            or not isinstance(relative, str)
            or not relative.endswith(".py")
            or relative in deleted
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not SHA256_RE.fullmatch(str(reader.get("sha256", "")))
        ):
            raise ConfigurationError(f"{label} 历史读取器登记非法。")
        # 记录的 sha256 只是签发时的快照；读取器后续按通用 successor 图演进，
        # 这里只要求它仍是源码树内存在的普通文件。
        candidate = repository_root / Path(*relative.split("/"))
        if candidate.is_symlink() or not candidate.is_file():
            raise ConfigurationError(f"{label} 历史读取器不存在：{relative}")


def _validate_candidate_source_transition(
    source_root: Path,
    transition: Mapping[str, Any],
    *,
    git_commit: str,
) -> None:
    """从 Git 两端与文件字节重放 Candidate 的冻结 successor。"""

    required = {
        "schema_version",
        "issued_at_utc",
        "base_commit",
        "current_commit",
        "scope",
        "mode",
        "extra_worktree_paths",
        "frozen_path_count",
        "frozen_edge_count",
        "changed_path_count",
        "transitions",
        "unregistered_path_count",
        "unregistered_paths",
        "deleted_frozen_paths",
        "required_manual_actions",
        "verification",
        "safety",
        "result",
        "identity_sha256",
    }
    if not isinstance(transition, Mapping) or set(transition) - {"deletion_proof"} != required:
        raise ConfigurationError("Candidate source transition 字段不闭合。")
    if (
        transition.get("schema_version")
        != "official-egress-upstream-freeze-successor/v1"
        or transition.get("mode") != "commit"
        or transition.get("current_commit") != git_commit
        or transition.get("extra_worktree_paths") != []
        or not isinstance(transition.get("base_commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", transition["base_commit"])
    ):
        raise ConfigurationError(
            "Candidate source transition 必须是绑定当前 commit 的提交模式收据。"
        )
    expected_result = (
        FREEZE_SUCCESSOR_RESULT_PASSED_WITH_DELETIONS
        if transition.get("deleted_frozen_paths")
        else FREEZE_SUCCESSOR_RESULT_PASSED
    )
    if (
        transition.get("result") != expected_result
        or transition.get("required_manual_actions") != []
        or transition.get("unregistered_path_count") != 0
        or transition.get("unregistered_paths") != []
    ):
        raise ConfigurationError(
            "Candidate source transition 仍有人工待办、未登记路径或失败状态；"
            "删除冻结路径必须以 passed_with_deletions 携带删除证明。"
        )
    _validate_freeze_deletion_proof(
        transition,
        repository_root=source_root,
        label="Candidate source transition",
    )
    verification = transition.get("verification")
    safety = transition.get("safety")
    if (
        not isinstance(verification, list)
        or not verification
        or not all(isinstance(item, str) and item.strip() for item in verification)
        or not any("check-egress-spec" in item for item in verification)
        or not isinstance(safety, Mapping)
        or any(
            safety.get(field) is not False
            for field in (
                "deployment_performed",
                "live_account_used",
                "official_egress_profile_changed",
                "production_config_changed",
                "wire_or_persona_selection_changed",
            )
        )
    ):
        raise ConfigurationError(
            "Candidate source transition 未证明本地验证通过或安全边界保持不变。"
        )
    unsigned = dict(transition)
    recorded_identity = unsigned.pop("identity_sha256")
    if (
        not SHA256_RE.fullmatch(str(recorded_identity))
        or _fingerprint(unsigned) != recorded_identity
    ):
        raise ConfigurationError("Candidate source transition 自摘要不一致。")
    ancestry = subprocess.run(
        [
            "git",
            "-C",
            str(source_root),
            "merge-base",
            "--is-ancestor",
            str(transition["base_commit"]),
            git_commit,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    if ancestry.returncode != 0:
        raise ConfigurationError("Candidate source transition 的 base 不是当前 commit 前序。")
    rows = transition.get("transitions")
    if not isinstance(rows, list) or not rows:
        raise ConfigurationError("Candidate source transition 没有可重放摘要边。")
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping) or set(row) != {
            "path",
            "old_path",
            "status",
            "predecessor_sha256s",
            "to_sha256",
            "source_receipts",
            "reason",
        }:
            raise ConfigurationError(f"Candidate source transition 第 {index} 项字段不闭合。")
        relative = row.get("path")
        old_relative = row.get("old_path") or relative
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
            or relative in seen
            or not isinstance(old_relative, str)
        ):
            raise ConfigurationError(f"Candidate source transition 第 {index} 项路径非法。")
        seen.add(relative)
        predecessors = row.get("predecessor_sha256s")
        if (
            row.get("status") not in {"A", "M", "R", "C", "T"}
            or not isinstance(predecessors, list)
            or not predecessors
            or not all(SHA256_RE.fullmatch(str(value)) for value in predecessors)
            or not SHA256_RE.fullmatch(str(row.get("to_sha256")))
            or not isinstance(row.get("reason"), str)
            or not row["reason"].strip()
        ):
            raise ConfigurationError(f"Candidate source transition 第 {index} 项摘要或状态非法。")
        before_sha256 = _git_blob_sha256(
            source_root,
            str(transition["base_commit"]),
            str(old_relative),
        )
        after_sha256 = _git_blob_sha256(source_root, git_commit, relative)
        if before_sha256 not in predecessors or after_sha256 != row["to_sha256"]:
            raise ConfigurationError(
                f"Candidate source transition 第 {index} 项无法从 Git 两端复算。"
            )


def _require_clean_candidate_source(source_root: Path) -> None:
    """确保 Candidate 构建输入逐字属于已登记 Git commit。"""

    try:
        status_output = subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError("无法复算 Candidate Git 工作树状态。") from error
    if status_output.strip():
        raise ConfigurationError(
            "Candidate 源码树不是干净 commit；必须先提交画像、测试和门禁计划。"
        )


def _external_file_binding(path: Path, label: str) -> dict[str, Any]:
    """为 Campaign 外的小型不可变文件形成绝对路径绑定。"""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}必须是可信绝对普通文件。")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    size = metadata.st_size
    if size < 1:
        raise ConfigurationError(f"{label}不能为空。")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ConfigurationError(f"{label}不得允许 group／other 写入。")
    return {
        "path": str(resolved),
        "sha256": file_sha256(resolved),
        "bytes": size,
    }


def _external_json_object(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取绝对、非符号链接且不可被其他用户改写的小型 JSON。"""

    binding = _external_file_binding(path, label)
    if binding["bytes"] > 16 * 1024 * 1024:
        raise ConfigurationError(f"{label}超过 16 MiB 小型控制制品上限。")
    return _read_json(Path(binding["path"]), label), binding


def _require_outside_candidate_source(
    source_root: Path,
    artifact_path: Path,
    label: str,
) -> None:
    """拒绝把描述候选源码树的控制制品放回该树形成自引用。"""

    try:
        artifact_path.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    except ValueError:
        return
    raise ConfigurationError(f"{label}必须位于源码树外，避免自引用改变构建树。")


def _control_receipt_relative(root: Path, value: Path, label: str) -> str:
    """把控制收据规范化为其受管根内的 POSIX 相对路径。"""

    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ConfigurationError(f"{label} 的 evidence root 必须是现有非符号链接绝对目录。")
    resolved_root = root.resolve(strict=True)
    candidate = value if value.is_absolute() else resolved_root / value
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(resolved_root).as_posix()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(f"{label} 必须位于其受管根内。") from error
    if candidate.is_symlink() or not resolved.is_file():
        raise ConfigurationError(f"{label} 必须是非符号链接普通文件。")
    return relative



def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取{label} {path}：{error}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}必须是 JSON 对象：{path}")
    return payload
