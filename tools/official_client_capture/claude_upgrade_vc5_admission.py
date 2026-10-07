"""Claude 的 VC-5 准入受管事实（Claude 升级控制面设计 B5b-01，原 B4-01 d 段）。

驱动 ``vc5_admission`` 的统一准入（只读预演 → 显式批准补齐 → 派发前重放同一收据）两客户端共用，客户端差异只在两项受管
事实，经适配器成员取（``upgrade_client_adapter.ADAPTER_MEMBERS`` 的 ``catalog_stage_profile``、``vc5_official_campaign``）。
Codex 的实现是驱动原有的函数体（``codex_upgrade_client_adapter``）；本模块是 Claude 的实现。全部只读：不写 Campaign、不写
Store（Store 一律只读打开）、不发请求。事实不成立抛 ``Vc5AdmissionFactError``，消息只是静态错误码（准入报告据此记 blocked）；
编排器读侧自己的 ``ConfigurationError`` 原样抛出，驱动记为 ``catalog_replay_failed``（与 Codex 同一口径）。

一、目标画像（``catalog_stage_profile``）
=========================================
与设计签名 ``catalog_stage_profile(campaign_dir, classification)`` 的偏离（10-07 主会话认可）：重放 VC-4 候选构建收据与候选
源码树还要候选、版本、画像标签、源码目录、生命周期目录与候选提交，所以参数一律按关键字给出，分类结果由实现自己读。

* VC-3：总计划为 formal；VC 总计划与 VC-3 checkpoint 逐级重放，checkpoint 的阶段收据就是画像阶段结果
  ``profile/result.json``（摘要一致），阶段结果经编排器 ``_load_stage_result("stage-profile")`` 核对（准备结果、联合清单、
  暂存 Release 逐文件复算）；暂存 Release 收据的 Campaign 与目标版本等于本轮，Release 条目的画像就是阶段结果登记的目标画像。
* 画像文件：暂存 Release 的 ``catalogdata/claude/profiles/<版本>/<摘要>.json``，inventory 恰有一条、文件 SHA-256 等于条目
  登记的摘要。Claude 画像没有 Codex 那种内容 ``Digest`` 字段，Release 条目的画像摘要就是文件 SHA-256，所以
  ``profile_digest`` 与 ``profile_sha256`` 取同一个值（两项合同在 Claude 是同一摘要，不是互相替代）。
* VC-4：当前候选 revision（编排器侧与 Store 侧一致）属于本候选；VC-4 checkpoint 按该 revision 重放，阶段收据就是候选构建
  收据（客户端中立合同 ``official-client-upgrade-candidate-build-receipt/v1``，摘要一致、合同校验通过）；构建收据的
  Campaign、候选、revision、目标版本、Campaign 总计划摘要与本轮一致，用途等于**冻结的总计划** ``campaign_purpose``，
  画像与 Release 身份等于 VC-3 暂存的那一份。
* Store（只读）：本候选在当前 revision 的 ``candidate_frozen`` 存在，用途等于总计划用途，引用的 CandidateBuildReceipt 与
  构建收据登记的同一份，事实引用等于构建收据登记的 ``candidate_frozen_ref``。
* 候选源码树：构建收据的源码根就是本轮 ``B/source``、提交就是本轮 C；``<LIFECYCLE_DIR>/catalog-stage/release-stage-receipt.json``
  就是构建收据绑定的那份、与 Campaign 暂存收据逐字节相同；树内 ``backend/internal/officialegress/<画像路径>`` 与暂存画像逐字节
  相同。
* 画像标签：Claude 画像没有 profile_id，驱动参数 PROFILE_ID 只作标签（参数解析已核它等于 TARGET_PROFILE_ID），原样返回；
  准入绑定以画像摘要与 ``release_sha256`` 为准——后者经 ``extra_bindings`` 并入准入绑定（同时带上 Campaign 用途）。

二、官方证据所在的 Campaign（``vc5_official_campaign``）
====================================================
证据决定与总计划冻结的前序必须一致：recapture 时总计划不得冻结前序，官方证据就在本 Campaign；reuse 时总计划必须冻结
前序只读复用（``{mode: read_only_reuse, campaign_id, campaign_identity_sha256}``），参数 PREDECESSOR_CAMPAIGN（ID 或绝对路径）
解析到数据根 ``evidence/campaigns/<前序 ID>``，前序 Campaign 是同一目标版本的 formal Campaign，且本 Campaign 的 Store 里已有
这次只读复用的导入登记与收据、EvidencePackage 已获正式引用授权（``claude_upgrade_predecessor.vc1_reuse_plan``）。不成立时
仍返回这个路径并给出阻断原因 ``official_reuse_binding_invalid``（与 Codex 同一错误码）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_capture.upgrade_client_adapter import Vc5AdmissionFactError, vc5_plain_path, vc5_read_json

# 候选源码树里 Catalog stage 目录（相对 LIFECYCLE_DIR）与暂存收据文件名、Claude 目录数据在仓库里的父目录。
CATALOG_STAGE_DIRNAME = "catalog-stage"


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _fact(condition: bool, code: str) -> None:
    if not condition:
        raise Vc5AdmissionFactError(code)


def _read_only_store(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    """只读打开 Campaign 受管 Store（锁与写入一律拒绝），取与总计划同源的 Store Campaign。"""

    from tools.official_client_capture import claude_upgrade_store
    from tools.official_client_capture.capturelib.model import ConfigurationError
    from tools.official_client_control.errors import ControlError
    from tools.official_client_control.store import ControlStore

    root = claude_upgrade_store.campaign_store_root(campaign_dir)
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"Campaign 缺少受管 Store：{root}")
    try:
        store = ControlStore.open_read_only(root)
        store_campaign = store.load_campaign(str(manifest["campaign_id"]))
    except ControlError as error:
        raise ConfigurationError(f"Campaign 受管 Store 不可读：{error}") from error
    claude_upgrade_store.require_store_campaign_matches_plan(manifest, store_campaign)
    return store, dict(store_campaign)


def _store_candidate(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, revision: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """本候选在 ``revision`` 的 ``candidate_frozen`` 事实与它的引用（Store 只读）。"""

    from tools.official_client_capture import claude_upgrade_baseline

    store, store_campaign = _read_only_store(campaign_dir, manifest)
    fact = claude_upgrade_baseline._store_candidate_fact(store, store_campaign, candidate_id, revision)
    _fact(fact is not None, "store_candidate_binding_invalid")
    return dict(fact), dict(store.fact_ref(fact))


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
    """VC-3 暂存画像与 VC-4 候选构建收据的重放（见模块说明第一节）；返回与 Codex 同一组字段，另带 ``extra_bindings``。"""

    from tools.official_client_capture import claude_upgrade_candidate
    from tools.official_client_capture import claude_upgrade_profile
    from tools.official_client_capture import claude_upgrade_release
    from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts

    orchestrator = _orchestrator()
    campaign = Path(campaign_dir)
    manifest = orchestrator._require_formal_campaign(campaign)
    _fact(
        (manifest.get("campaign_id"), manifest.get("target_version")) == (campaign_id, target_version),
        "campaign_binding_invalid",
    )
    purpose = manifest.get("campaign_purpose")
    plan = orchestrator._vc_campaign_plan(campaign, manifest)

    # VC-3：checkpoint 的阶段收据就是画像阶段结果，暂存 Release 逐文件复算。
    _, checkpoint = orchestrator._replay_vc_checkpoint(campaign, plan, "VC-3")
    ref = checkpoint["stage_receipt"]
    receipt_path = Path(ref["path"])
    if not receipt_path.is_absolute():
        receipt_path = campaign / receipt_path
    vc5_plain_path(receipt_path)
    _fact(receipt_path == campaign / claude_upgrade_profile.STAGE_RESULT_RELATIVE, "vc3_receipt_binding_invalid")
    _fact(file_sha256(receipt_path) == ref["sha256"], "vc3_receipt_binding_invalid")
    stage = orchestrator._load_stage_result(campaign, "stage-profile", _verified_campaign_manifest=manifest)
    staged = claude_upgrade_release.load_staged_release(campaign, stage["staged_release"])
    entry = staged["release_entry"]
    profile_relative = str(entry["profile"]["path"])
    profile_digest = str(entry["profile"]["sha256"])
    _fact(
        (staged.get("campaign_id"), staged.get("target_version"), entry.get("version")) == (campaign_id, target_version, target_version)
        and stage.get("target_profile_sha256") == profile_digest
        and PurePosixPath(profile_relative).parts[:4] == ("catalogdata", "claude", "profiles", target_version),
        "vc3_profile_binding_invalid",
    )
    matching = [item for item in staged["inventory"] if item["path"] == profile_relative]
    _fact(len(matching) == 1 and matching[0]["sha256"] == profile_digest, "target_profile_inventory_invalid")
    staged_root = campaign / claude_upgrade_release.STAGED_RELEASE_ROOT
    profile_path = staged_root.joinpath(*PurePosixPath(profile_relative).parts)
    vc5_plain_path(profile_path)
    raw = profile_path.read_bytes()
    _fact(file_sha256(profile_path) == profile_digest, "target_profile_content_digest_invalid")

    # VC-4：当前 revision 属于本候选，checkpoint 的阶段收据就是候选构建收据。
    revision, record = orchestrator._current_candidate_revision_record(campaign, manifest)
    _fact(revision is not None and record is not None and record.get("candidate_id") == candidate_id, "active_candidate_revision_invalid")
    _, vc4 = orchestrator._replay_vc_checkpoint(campaign, plan, "VC-4", revision=revision)
    build_path = orchestrator._candidate_build_receipt_path(campaign, candidate_id)
    vc5_plain_path(build_path)
    build_sha256 = file_sha256(build_path)
    vc4_ref = vc4["stage_receipt"]
    vc4_path = Path(vc4_ref["path"])
    if not vc4_path.is_absolute():
        vc4_path = campaign / vc4_path
    _fact(vc4_path == build_path and vc4_ref["sha256"] == build_sha256, "vc4_receipt_binding_invalid")
    try:
        build = vc_artifacts.validate_official_client_candidate_build_receipt(vc5_read_json(build_path))
    except vc_artifacts.VCArtifactError as error:
        raise Vc5AdmissionFactError("build_receipt_contract_invalid") from error
    _fact(
        (
            build["client"], build["campaign_id"], build["candidate_id"], build["candidate_revision"], build["target_version"],
            build["candidate_purpose"], build["campaign_manifest_sha256"],
        )
        == ("claude", campaign_id, candidate_id, revision, target_version, purpose, file_sha256(campaign / "campaign.json")),
        "build_receipt_binding_invalid",
    )
    _fact(
        build["profile"]["profile_digest"] == profile_digest and build["profile"]["release_sha256"] == entry["release_sha256"],
        "build_profile_binding_invalid",
    )

    # Store（只读）：candidate_frozen 用途、CandidateBuildReceipt 引用与事实引用等于构建收据的登记。
    fact, fact_ref = _store_candidate(campaign, manifest, candidate_id, revision)
    _fact(
        fact["payload"].get("candidate_purpose") == purpose
        and fact["payload"].get("candidate_build_receipt_ref") == build["store"]["candidate_build_receipt_ref"]
        and fact_ref == build["store"]["candidate_frozen_ref"],
        "store_candidate_binding_invalid",
    )

    # 候选源码树：源码根与提交、树内 Catalog stage 收据与画像逐字节等于 VC-3 暂存。
    source = Path(candidate_source)
    _fact(build["source"]["root"] == str(source), "candidate_source_invalid")
    _fact(build["source"]["git_commit"] == candidate_commit, "candidate_commit_invalid")
    tree_receipt = source / lifecycle_dir / CATALOG_STAGE_DIRNAME / claude_upgrade_release.STAGED_RECEIPT_FILENAME
    vc5_plain_path(tree_receipt)
    staged_receipt_path = staged_root / claude_upgrade_release.STAGED_RECEIPT_FILENAME
    _fact(
        str(tree_receipt) == build["catalog_stage"]["path"]
        and tree_receipt.is_file()
        and tree_receipt.read_bytes() == staged_receipt_path.read_bytes()
        and file_sha256(tree_receipt) == build["catalog_stage"]["sha256"],
        "candidate_catalog_receipt_drift",
    )
    tree_profile = source.joinpath(*claude_upgrade_candidate.REPOSITORY_CATALOG_PARENT.parts, *PurePosixPath(profile_relative).parts)
    vc5_plain_path(tree_profile)
    _fact(tree_profile.is_file() and tree_profile.read_bytes() == raw, "candidate_catalog_receipt_drift")
    return {
        "profile_bytes": raw,
        "profile_sha256": profile_digest,
        "profile_digest": profile_digest,
        "profile_id": profile_id,
        "vc3_receipt_sha256": ref["sha256"],
        "build_receipt_sha256": build_sha256,
        "build_id": build["build"]["build_id"],
        "image_id": build["image"]["image_id"],
        "extra_bindings": {"release_sha256": str(entry["release_sha256"]), "campaign_purpose": str(purpose)},
    }


def vc5_official_campaign(
    data_root: Path,
    *,
    campaign_id: str,
    target_version: str,
    evidence_decision: str,
    predecessor_campaign: str,
) -> dict[str, Any]:
    """官方证据所在的 Campaign 与阻断原因（见模块说明第二节）；返回 ``{"path", "blocked_reason"}``。"""

    from tools.official_client_capture import claude_upgrade_predecessor

    orchestrator = _orchestrator()
    campaigns = Path(data_root) / "evidence" / "campaigns"
    current_dir = campaigns / campaign_id
    official = current_dir
    try:
        manifest = orchestrator._require_formal_campaign(current_dir)
        binding = claude_upgrade_predecessor.validate_predecessor_binding(manifest.get("predecessor"), campaign_id=campaign_id)
        if evidence_decision != "reuse":
            _fact(binding is None, "official_reuse_binding_invalid")
            return {"path": official, "blocked_reason": None}
        _fact(binding is not None, "official_reuse_binding_invalid")
        official = campaigns / str(binding["campaign_id"])
        given = Path(predecessor_campaign)
        if not given.is_absolute():
            given = campaigns / given
        vc5_plain_path(official)
        _fact(given == official and official.is_dir(), "official_reuse_binding_invalid")
        source = orchestrator._require_formal_campaign(official)
        _fact(
            (source.get("campaign_id"), source.get("target_version")) == (binding["campaign_id"], target_version),
            "official_reuse_binding_invalid",
        )
        store, store_campaign = _read_only_store(current_dir, manifest)
        claude_upgrade_predecessor.vc1_reuse_plan_from_store(store, store_campaign, manifest)
    except Exception:
        return {"path": official, "blocked_reason": "official_reuse_binding_invalid"}
    return {"path": official, "blocked_reason": None}
