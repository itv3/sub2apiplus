"""Codex 客户端适配器：Codex 专属的适配器成员取值（Claude 升级控制面设计 D4、B1-01）。

Codex 的编排器服务面就是按包路径导入的 ``codex_upgrade`` 模块本身，原样导出、不包装；本模块只承载监督器、对账器
等共享模块原先写死的 Codex 名字与小函数，取值与改造前逐字相同。其余成员直接绑定到原模块的原符号（见
``upgrade_client_adapter.ADAPTER_BINDINGS["codex"]``）。共享代码经 ``adapter_for("codex")`` 访问，不直接导入本模块。

B5b-01 起另承载 VC-5 准入的两项受管事实（``catalog_stage_profile``、``vc5_official_campaign``）：函数体是驱动
``vc5_admission`` 原写在 ``ManagedFacts.catalog`` 与 ``Admission.inspect`` 里的 Codex 逻辑，原样搬入（候选用途照旧写死
``production_replacement``，错误码不变），驱动经适配器调用，准入报告逐字不变。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

# 监督器 ``_campaign_run_manifest`` 对编排器 CLI 预声明动作拒绝的基础子命令（原写死在该函数里的 15 项）。
# 按清单版本追加 ``recover-vc1-interruption``、对监督器 CLI 追加的 7 项与客户端无关，仍留在监督器。
FORBIDDEN_SUBCOMMANDS = frozenset({
    "successor",
    "recover-candidate-failed-jobs",
    "control-epoch",
    "evaluation-transition",
    "terminal-transition-preflight",
    "plan",
    "reuse-official-evidence",
    "harden-evidence-permissions",
    "reconcile-supervisor-run",
    "reconcile-attempt",
    "compile-vc-batch",
    "compile-vc-interrupted-recovery-batch",
    "compile-vc-interrupted-recovery-continuation",
    "continue-vc1-interruption",
    "finalize-vc1-deadline-orphan",
})
# 断言包取证清单（``<attempt>/evidence/assertion-bundle/capture-manifest.json``）里记录目标版本的字段名；
# Campaign 一侧的字段始终叫 ``target_version``。
TARGET_VERSION_FIELD = "codex_version"
# 候选就绪探针（``/models`` wire dispatch）在项目总账里的请求计量类别：原写死在项目总账的入账
# （``RuntimeAdmission.account_candidate_probe``）与重放校验两处，取值逐字不变。
CANDIDATE_PROBE_ACCOUNTING_CATEGORY = "candidate_readiness_models_probe/v1"


def reject_volatile_dimensions(dimensions: Mapping[str, str]) -> None:
    """Codex 的易变形态已全部由根因模块的六类模式覆盖，没有附加检查。"""

    del dimensions


def attempt_environment_restored(attempt: Mapping[str, Any]) -> bool:
    """对账器判定 attempt 环境已恢复：after 探针与恢复收据都在位（原写死在对账器 ``_environment_facts`` 里，逐字不变）。"""

    environment = attempt.get("environment") if isinstance(attempt.get("environment"), Mapping) else {}
    return environment.get("after_probe") is not None and environment.get("restoration_report") is not None


def deploy_receipt_identity(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Codex 部署收据的工具五摘要就在收据顶层（原写死在对账器 ``_deployment_receipt`` 里，取值逐字不变）。"""

    return payload


def official_attempt_free_post_run_action(action: Any, campaign_dir: Any) -> bool:
    """VC-1 官方零请求后处理批次里不绑定 attempt 的动作：Codex 没有（断言包与 seal 动作都指向同一个官方 attempt），恒为假。

    监督器 post-run-tooling 判据与 seal 链续派协议经适配器调用它；恒为假时两处判定与改造前逐字相同。
    """

    del action, campaign_dir
    return False


def candidate_post_run_extra_action(manifest: Any, action: Any) -> bool:
    """候选级（VC-5／VC-6）共享闭集之外的零请求后处理动作：Codex 没有，恒为假（B4-07 收尾）。

    监督器 ``candidate_post_run_recovery_action`` 经适配器调用它；恒为假时额外项为空，判定与改造前逐字相同（Codex 的
    VC-6 生产步骤本来就在共享闭集的 canonical 项里，validation_only 的 deliver 动作照旧进候选审核）。
    """

    del manifest, action
    return False


# ---------------------------------------------------------------------------
# VC-5 准入的受管事实（B5b-01）：驱动 vc5_admission 原有的 Codex 逻辑，原样搬入
# ---------------------------------------------------------------------------


def catalog_stage_profile(
    campaign_dir: Path,
    *,
    campaign_id: str,
    candidate_id: str,
    target_version: str,
    profile_id: str,
    candidate_source: Path,
    lifecycle_dir: str,
    candidate_commit: str,
) -> dict[str, Any]:
    """VC-3 暂存收据与 VC-4 构建收据重放出的目标画像（原驱动 ``ManagedFacts.catalog`` 的函数体，逐字搬入）。

    参数与原函数读的驱动参数一一对应：``campaign_id``＝NEW、``candidate_id``＝CAND、``target_version``＝TARGET_VERSION、
    ``profile_id``＝PROFILE_ID、``candidate_source``＝B/source、``lifecycle_dir``＝LIFECYCLE_DIR、``candidate_commit``＝C。
    """

    from tools.official_client_capture import codex_upgrade as cu
    from tools.official_client_capture.upgrade_client_adapter import Vc5AdmissionFactError, vc5_plain_path, vc5_read_json

    campaign = Path(campaign_dir)
    manifest = cu._require_formal_campaign(campaign)
    classification = cu._load_stage_result(campaign, "classify")
    plan = cu._vc_campaign_plan(campaign, manifest)
    _, checkpoint = cu._replay_vc_checkpoint(campaign, plan, "VC-3")
    ref = checkpoint["stage_receipt"]
    receipt_path = Path(ref["path"])
    if not receipt_path.is_absolute():
        receipt_path = campaign / receipt_path
    vc5_plain_path(receipt_path)
    if cu.file_sha256(receipt_path) != ref["sha256"]:
        raise Vc5AdmissionFactError("vc3_receipt_binding_invalid")
    stage = vc5_read_json(receipt_path)
    cu._verify_catalog_stage_output(receipt_path.parent, stage)
    bound_profile_id, profile_digest = cu._profile_binding_from_manifest(campaign, classification)
    if (stage.get("campaign_id"), stage.get("target_version"), stage.get("profile_id"), stage.get("target_profile_digest")) != (
        campaign_id, target_version, profile_id, profile_digest,
    ) or bound_profile_id != profile_id or stage.get("classification_sha256") != classification.get("joint_manifest_sha256"):
        raise Vc5AdmissionFactError("vc3_profile_binding_invalid")
    build_path = campaign / "candidates" / candidate_id / "build-receipt.json"
    build, binding = cu._replay_candidate_build_receipt(campaign, manifest, candidate_id, build_path)
    revision, record = cu._current_candidate_revision_record(campaign, manifest)
    if revision is None or (record is not None and record.get("candidate_id") != candidate_id):
        raise Vc5AdmissionFactError("active_candidate_revision_invalid")
    cu._replay_vc_checkpoint(campaign, plan, "VC-4", revision=revision)
    args = argparse.Namespace(
        campaign_dir=campaign, candidate_id=candidate_id, runtime_image=build["image"]["reference"],
        candidate_image_id=build["image"]["image_id"], candidate_source=Path(candidate_source),
        build_id=build["build"]["build_id"], deployed_version=target_version,
        profile_id=bound_profile_id, profile_digest=profile_digest, candidate_purpose="production_replacement",
    )
    identity = cu._candidate_identity_for_run(args, manifest, classification, verify_image=False)
    identity = cu._bind_candidate_identity_to_build_receipt(args, identity, build, binding)
    if identity["git_commit"] != candidate_commit:
        raise Vc5AdmissionFactError("candidate_commit_invalid")
    source_receipt = Path(candidate_source) / lifecycle_dir / "catalog-stage/catalog-stage-receipt.json"
    vc5_plain_path(source_receipt)
    if source_receipt.read_bytes() != receipt_path.read_bytes():
        raise Vc5AdmissionFactError("candidate_catalog_receipt_drift")
    relative = f"catalogdata/runtime/profiles/{target_version}/{profile_digest}.json"
    matching = [item for item in stage["inventory"] if item["path"] == relative]
    if len(matching) != 1:
        raise Vc5AdmissionFactError("target_profile_inventory_invalid")
    raw = (receipt_path.parent / relative).read_bytes()
    if json.loads(raw).get("Digest") != profile_digest:
        raise Vc5AdmissionFactError("target_profile_content_digest_invalid")
    # 内容 Digest 和文件 SHA-256 是两项独立合同，不能互相替代。
    return {
        "profile_bytes": raw, "profile_sha256": matching[0]["sha256"], "profile_digest": profile_digest,
        "profile_id": bound_profile_id, "vc3_receipt_sha256": ref["sha256"],
        "build_receipt_sha256": cu.file_sha256(build_path), "build_id": args.build_id,
        "image_id": args.candidate_image_id,
    }


def vc5_official_campaign(
    data_root: Path,
    *,
    campaign_id: str,
    target_version: str,
    evidence_decision: str,
    predecessor_campaign: str,
) -> dict[str, Any]:
    """官方证据所在的 Campaign（原驱动 ``Admission.inspect`` 里的 reuse 分支，逐字搬入）。

    返回 ``{"path": 路径, "blocked_reason": None 或静态错误码}``：证据决定为 recapture 时就是本 Campaign；为 reuse 时是
    参数给出的前序 Campaign，须与本 Campaign 总计划冻结的前序（目录、ID、总计划摘要）一致且目标版本相同，否则照旧返回
    这个路径并给出阻断原因 ``official_reuse_binding_invalid``（准入报告据此记 blocked，路径仍进有效参数）。
    """

    from tools.official_client_capture import codex_upgrade as cu
    from tools.official_client_capture.upgrade_client_adapter import vc5_plain_path

    official = Path(data_root) / "evidence/campaigns" / campaign_id
    if evidence_decision != "reuse":
        return {"path": official, "blocked_reason": None}
    official = Path(predecessor_campaign)
    if not official.is_absolute():
        official = Path(data_root) / "evidence/campaigns" / official
    try:
        vc5_plain_path(official)
        source = cu._require_formal_campaign(official)
        if source.get("target_version") != target_version:
            raise ValueError("official_reuse_target_mismatch")
        current_manifest = cu._require_formal_campaign(Path(data_root) / "evidence/campaigns" / campaign_id)
        predecessor = current_manifest.get("predecessor") or {}
        if (Path(str(predecessor.get("campaign_dir", ""))) != official
                or predecessor.get("campaign_id") != source.get("campaign_id")
                or predecessor.get("campaign_manifest_sha256") != cu.file_sha256(official / "campaign.json")):
            raise ValueError("official_reuse_not_current_predecessor")
    except Exception:
        return {"path": official, "blocked_reason": "official_reuse_binding_invalid"}
    return {"path": official, "blocked_reason": None}
