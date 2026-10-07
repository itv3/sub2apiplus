#!/usr/bin/env python3
"""Claude 后继候选复用（Claude 升级控制面设计 B4-01 c3）：``resume --candidate-reuse-source-*``。

与 Codex 同名机制对齐：后继 Campaign 的候选在首个 attempt 时，只读承接直接前序 Campaign 里同一候选构建的已完成候选 Job，
能复用的不重采，其余（执行定义或工具依赖变了的 Job）在同一个新 attempt 里执行。复用判定与 c1 候选续跑同源
（``claude_upgrade_recovery``：证据逐项复核、复用结果的 ``reused_from``、恢复 attempt 主流程），这里只放跨 Campaign
多出来的部分。

一、命令
========
``resume --candidate-id <cid> --rerun-failed --candidate-reuse-source-campaign-dir <前序 Campaign>
--candidate-reuse-source-candidate-id <前序候选> --candidate-reuse-source-attempt-id <前序 attempt>``

* 加 ``--preview-recovery``：只读输出复用预览（``claude-upgrade-candidate-reuse-preview/v1``，零写入、零请求），其中
  ``preview_sha256`` 是批准摘要；
* 加 ``--approve-candidate-reuse-sha256 <预览摘要>``：在 Campaign 租约下重算预览，逐字等于批准的那份才预约新 attempt；
  执行集合非空时另需 ``--acknowledge-live-requests``（会经候选网关发真实请求）。
* 当前候选必须是 ``profile_approved``（VC-4 已构建、还没有任何 attempt）；跨 revision 复用（来源是本 Campaign 的旧候选）
  一律拒绝：revision 已换构建，旧候选证据不复用（与 Codex 一致，见第四节）。

二、复用判据
============
* **前序**：当前是 Formal Campaign，总计划冻结了 ``predecessor``（只读复用前序，``{mode, campaign_id,
  campaign_identity_sha256}``）；来源目录是同一宿主数据根下的另一个 Campaign，``campaign.json`` 的 ID 与它受管 Store 的
  Campaign 身份摘要都等于这份冻结绑定（复用来源在 VC-0 建 Campaign 时就定了，不能事后挑选）；两边 ``capture_root`` 相同
  （证据根的容器坐标映射到同一宿主路径）。前序 Campaign 没有未隔离的环境污染。
* **来源 attempt**（c3a 只认未封存）：前序该候选没有阶段结果；attempt 是 ``awaiting_receipts``、是前序唯一待封存的候选
  attempt、没有失败 Job、没有被隔离、冲突隔离或按工具演进作废对账、没有 ``seal-failure.json``，网关隔离恢复核验
  ``passed``（激活事实副本由 attempt 读侧强制）。
* **候选身份**：``REUSE_IDENTITY_FIELDS``（跨 Campaign 不变的子集）逐项相等——目标版本与官方二进制、采集镜像与容器、
  官方客户端路径、网关坐标（服务／keeper／postgres、账号、API Key）、候选用途、部署版本、候选镜像引用／image ID／
  manifest 摘要、源码树摘要与提交、画像／Release／wire 摘要。不比 Campaign 专属字段（revision、构建收据绑定、Store 的
  两个引用）与路径类字段（源码根、工具执行副本、构建 ID 标签）。这在实际上要求后继 VC-4 用的是同一个镜像。
* **逐 Job**（满足才复用，其余进执行集合并在预览里列原因）：① 来源结果 complete；② 把当前 Job 的执行定义里的
  Campaign／候选 ID 换回来源坐标后，执行摘要等于来源结果记录的摘要（同 Codex ``_successor_job_execution_matches``；脚本、
  参数、环境或证据根的其他变化都会使摘要不等）；③ 来源结果的 ``tool_dependency_files`` 逐文件等于当前工具算出的值；
  ④ 证据逐项复核（c1 的 ``_verify_reusable_evidence``：Job 闭环收据、Job 级权限收口重放、relay 清单、逐步恢复报告），
  不通过即失败关闭，不退化为重采。
* **执行集合非空时**另核连续性（与 c1 同一判据：来源 attempt 的 after 探针、网关 after 快照分别等于本轮 before 探针与
  网关 before 快照）；执行集合为空时不发任何请求、不新增证据，不做连续性核对（``recovery.continuity`` 记 null），被复用
  证据的环境前提由来源 attempt 自己的前后探针与网关恢复核验证明，绑定在复用来源收据里。

三、登记形状
============
* 新 attempt 与首个候选 attempt 同一合同；复用结果沿用 c1 形状（``disposition=reused``、``job_closure=null``、
  ``reused_from={attempt_id, attempt, job_receipt, producer}``），另加 ``reused_from.campaign``（来源 Campaign：
  ``campaign_id``、``campaign_dir``、``campaign_manifest_sha256``、``candidate_id``；``attempt``／``job_receipt`` 的路径
  相对它）与 ``reused_from.rebased_from``（改绑前的 ``execution_sha256``、``environment_sha256``、
  ``incremental_result_key``）。结果本身的执行摘要与增量元数据按本轮 Job、身份与工具改绑（同 Codex
  ``_rebase_reused_result``），``evidence_roots`` 不变（仍指向前序的证据根）。
* ``producer`` 始终指向最初执行它的 attempt；在别的 Campaign 时带 ``campaign``（同上四项）与 ``execution_sha256``（产出时的
  执行摘要，Job 闭环收据按它核对）。后来的同 Campaign 续跑与 seal 的坐标迁移都读 ``producer.campaign``。
* 新 attempt 根下与预约一起原子发布 ``candidate-reuse-source.json``（``claude-upgrade-candidate-reuse-source/v1``）：批准的
  预览全文、批准摘要与时刻、复用与执行集合、来源 attempt 的候选网关运行条件与恢复核验绑定；attempt 的 ``recovery.source``
  绑定它，``recovery.source_campaign_id`` 记来源 Campaign。

四、与 B4-05 candidate-source 改道
==================================
``invalidate-candidate`` → ``revision-open --supersedes`` 之后，新 revision 的构建身份必然变化（revision seal 强制证明
``changed_layers`` 非空），候选身份子集不可能相等：来源是本 Campaign 的旧候选时直接拒绝，``resume --candidate-id <新候选>``
照常起首个候选 run（c1）。

本文件在 Claude 身份策略里按 ``claude_upgrade_`` 前缀归 control 层，不进 wire 闭包。
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture

PREVIEW_SCHEMA = "claude-upgrade-candidate-reuse-preview/v1"
SOURCE_SCHEMA = "claude-upgrade-candidate-reuse-source/v1"
SOURCE_FILENAME = "candidate-reuse-source.json"
CANDIDATE_PHASE = "candidate"
SEAL_FAILURE_FILENAME = "seal-failure.json"
# 跨 Campaign 不变的候选身份子集（见文件头第二节）；其余字段是 Campaign 专属或路径类，不比。
REUSE_IDENTITY_FIELDS = (
    "schema_version",
    "client",
    "phase",
    "mode",
    "candidate_purpose",
    "target_version",
    "target_sha256",
    "official_identity_sha256",
    "runtime_image",
    "capture_container",
    "claude_bin",
    "service_container",
    "keeper_container",
    "postgres_container",
    "account_id",
    "api_key_id",
    "deployed_version",
    "image_reference",
    "image_id",
    "manifest_digest",
    "source_tree_sha256",
    "git_commit",
    "profile_digest",
    "release_sha256",
    "wire_sha256",
)
SOURCE_ARGUMENTS = (
    ("candidate_reuse_source_campaign_dir", "--candidate-reuse-source-campaign-dir"),
    ("candidate_reuse_source_candidate_id", "--candidate-reuse-source-candidate-id"),
    ("candidate_reuse_source_attempt_id", "--candidate-reuse-source-attempt-id"),
)
PREVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "campaign_id",
        "campaign_manifest_sha256",
        "candidate_id",
        "source_campaign",
        "source_candidate_id",
        "source_attempt",
        "identity_sha256",
        "planned_job_ids",
        "reuse_job_ids",
        "execute_job_ids",
        "not_reusable",
        "result_bindings",
        "evidence_roots",
        "tool_files_sha256",
        "continuity_required",
        "live_request_count",
        "preview_sha256",
    }
)
SOURCE_FIELDS = frozenset(
    {
        "schema_version",
        "client",
        "campaign_id",
        "attempt_id",
        "reservation_digest",
        "preview",
        "approval_sha256",
        "approved_at_utc",
        "source_gateway",
        "reuse_job_ids",
        "execute_job_ids",
        "recorded_at_utc",
        "reuse_source_sha256",
    }
)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def requested(arguments: argparse.Namespace) -> bool:
    """是否给出了后继候选复用的参数（任一项）；给了就必须成套（见 ``validate_arguments``）。"""

    return any(getattr(arguments, name, None) is not None for name, _flag in SOURCE_ARGUMENTS) or getattr(
        arguments, "approve_candidate_reuse_sha256", None
    ) is not None


def validate_arguments(arguments: argparse.Namespace) -> None:
    missing = [flag for name, flag in SOURCE_ARGUMENTS if getattr(arguments, name, None) is None]
    if missing:
        raise ConfigurationError("后继候选复用缺少显式来源参数：" + "、".join(missing))
    if not getattr(arguments, "rerun_failed", False):
        raise ConfigurationError("后继候选复用必须显式使用 --rerun-failed（与 Codex 同一命令形态）。")
    preview = bool(getattr(arguments, "preview_recovery", False))
    approval = getattr(arguments, "approve_candidate_reuse_sha256", None)
    if preview == (approval is not None):
        raise ConfigurationError("后继候选复用必须且只能给出 --preview-recovery（只读预览）或 --approve-candidate-reuse-sha256（批准执行）之一。")
    if approval is not None and not capture.SHA256_RE.fullmatch(str(approval)):
        raise ConfigurationError("--approve-candidate-reuse-sha256 格式非法。")
    if getattr(arguments, "recovery_preview", None) is not None:
        raise ConfigurationError("后继候选复用不经 reconcile-attempt 的恢复预览，不接受 --recovery-preview。")
    for name, flag in SOURCE_ARGUMENTS[1:]:
        if not capture.SAFE_ID_RE.fullmatch(str(getattr(arguments, name))):
            raise ConfigurationError(f"{flag} 非法。")


REVISION_REUSE_REFUSAL = "revision 已换构建，旧候选证据不复用"


def reject_same_campaign(campaign_dir: Path, arguments: argparse.Namespace, candidate_id: str) -> None:
    """来源是本 Campaign 自己（换 revision 之后的旧候选）：在看候选状态之前就拒绝（文件头第四节）。"""

    raw = getattr(arguments, "candidate_reuse_source_campaign_dir", None)
    try:
        same = Path(raw).resolve(strict=True) == Path(campaign_dir).resolve(strict=True)
    except OSError:
        return
    if same:
        raise ConfigurationError(
            f"{REVISION_REUSE_REFUSAL}：后继候选复用只承接直接前序 Campaign 的候选；本 Campaign 换 revision 后"
            f"用 resume --candidate-id {candidate_id} 起首个候选 run。"
        )


# ---------------------------------------------------------------------------
# 来源（只读）
# ---------------------------------------------------------------------------


def _source_store_identity(source_dir: Path, campaign_id: str) -> str:
    """来源 Campaign 受管 Store 的 Campaign 身份摘要（只读打开；离线测试替换它）。"""

    from tools.official_client_capture import claude_upgrade_store
    from tools.official_client_control.errors import ControlError
    from tools.official_client_control.store import ControlStore

    try:
        campaign = ControlStore.open_read_only(claude_upgrade_store.campaign_store_root(source_dir)).load_campaign(str(campaign_id))
    except ControlError as error:
        raise ConfigurationError(f"来源 Campaign 的受管 Store 不可读：{error}") from error
    return str(campaign["identity_sha256"])


def _campaign_object(source_dir: Path, source_manifest: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    return {
        "campaign_id": str(source_manifest["campaign_id"]),
        "campaign_dir": str(source_dir),
        "campaign_manifest_sha256": file_sha256(source_dir / "campaign.json"),
        "candidate_id": candidate_id,
    }


def source_context(campaign_dir: Path, manifest: Mapping[str, Any], arguments: argparse.Namespace, candidate_id: str) -> dict[str, Any]:
    """解析并核对来源（文件头第二节的前序与来源 attempt 两项）；只读：不取租约、不写文件、不发请求。"""

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    raw = getattr(arguments, "candidate_reuse_source_campaign_dir")
    source_value = Path(raw)
    if not source_value.is_absolute() or source_value.is_symlink() or not source_value.is_dir():
        raise ConfigurationError("复用来源 Campaign 必须是已存在、非符号链接的绝对路径。")
    source_dir = source_value.resolve(strict=True)
    if str(source_dir) != str(source_value):
        raise ConfigurationError("复用来源 Campaign 必须是规范绝对路径。")
    source_candidate = str(arguments.candidate_reuse_source_candidate_id)
    source_attempt_id = str(arguments.candidate_reuse_source_attempt_id)
    if source_dir == campaign_dir:
        reject_same_campaign(campaign_dir, arguments, candidate_id)
    if source_dir.parent != campaign_dir.parent:
        raise ConfigurationError("复用来源 Campaign 必须与当前 Campaign 在同一宿主数据根的 evidence/campaigns 下。")
    predecessor = manifest.get("predecessor")
    if not isinstance(predecessor, Mapping):
        raise ConfigurationError("当前 Campaign 总计划没有冻结前序（predecessor）：后继候选复用的来源必须在 VC-0 建 Campaign 时冻结。")
    source_manifest = claude_upgrade.load_campaign_manifest(source_dir)
    if source_manifest.get("campaign_id") != predecessor.get("campaign_id"):
        raise ConfigurationError(
            f"复用来源 Campaign {source_manifest.get('campaign_id')} 不是当前 Campaign 冻结的前序 {predecessor.get('campaign_id')}。"
        )
    claude_upgrade._require_formal_campaign(source_dir, source_manifest)
    if _source_store_identity(source_dir, str(source_manifest["campaign_id"])) != predecessor.get("campaign_identity_sha256"):
        raise ConfigurationError("复用来源 Campaign 的 Store 身份与冻结的前序绑定不一致（前序被替换或重建）。")
    current_layout = capture._capture_layout(campaign_dir, manifest)
    source_layout = capture._capture_layout(source_dir, source_manifest)
    if (current_layout.host_data_root, current_layout.capture_root) != (source_layout.host_data_root, source_layout.capture_root):
        raise ConfigurationError("复用来源 Campaign 与当前 Campaign 的宿主数据根或 capture_root 不同，证据根坐标无法一一对应。")
    if claude_upgrade._campaign_contamination_records(source_dir, _manifest=source_manifest):
        raise ConfigurationError("复用来源 Campaign 有未隔离的环境污染：先在前序完成环境隔离，再复用其候选证据。")
    # 来源 attempt（c3a 只认未封存）。
    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

    result_path = candidate_capture.legacy_stage_result_path(source_dir, source_candidate)
    if result_path.exists() or result_path.is_symlink():
        raise claude_upgrade._pending("复用已封存的前序候选证据", "B4-01 c3b（已封存来源与 seal 坐标迁移）")
    key = (CANDIDATE_PHASE, source_candidate, source_attempt_id)
    if key in capture._invalidated_attempts(source_dir) or key in claude_upgrade._conflict_quarantined_attempts(source_dir):
        raise ConfigurationError("复用来源 attempt 已被环境隔离或证据根冲突隔离作废，结果永不复用。")
    source_root, source_attempt = claude_upgrade._load_capture_attempt(
        source_dir, CANDIDATE_PHASE, source_candidate, source_attempt_id, _verified_campaign_manifest=source_manifest,
        _replay_evidence_permissions=False,
    )
    if source_attempt.get("status") != "awaiting_receipts":
        raise ConfigurationError(f"复用来源 attempt 必须是 awaiting_receipts（当前 {source_attempt.get('status')}）。")
    if (source_root / SEAL_FAILURE_FILENAME).exists() or (source_root / SEAL_FAILURE_FILENAME).is_symlink():
        raise ConfigurationError("复用来源 attempt 有 seal 失败收据，结果永不复用（先人工审计）。")
    capture_reconciled = capture._attempt_reconciled_terminal(source_dir, source_attempt_id)
    if isinstance(capture_reconciled, Mapping) and isinstance(capture_reconciled.get("tool_evolution_invalidation"), Mapping):
        raise ConfigurationError("复用来源 attempt 已按工具演进作废对账，不再是待封存 attempt。")
    if claude_upgrade._failed_job_ids(source_attempt.get("results")):
        raise ConfigurationError("复用来源 attempt 仍有失败 Job，不是完整的待封存 attempt。")
    active = claude_upgrade._active_unsealed_attempts(source_dir, CANDIDATE_PHASE, _manifest=source_manifest)
    if active != [f"{source_candidate}:{source_attempt_id}"]:
        raise ConfigurationError(f"复用来源不是前序唯一的待封存候选 attempt（前序待封存：{active}）。")
    recovery = source_attempt.get("gateway_recovery")
    if not isinstance(recovery, Mapping) or recovery.get("status") != "passed":
        raise ConfigurationError("复用来源 attempt 的网关隔离恢复核验没有通过，结果不复用。")
    identity = source_attempt.get("identity")
    if not isinstance(identity, Mapping):
        raise ConfigurationError("复用来源 attempt 缺少候选运行身份。")
    return {
        "source_dir": source_dir,
        "source_manifest": source_manifest,
        "source_candidate_id": source_candidate,
        "source_attempt_id": source_attempt_id,
        "source_root": source_root,
        "source_attempt": dict(source_attempt),
        "source_campaign": _campaign_object(source_dir, source_manifest, source_candidate),
        "store_identity_sha256": str(predecessor.get("campaign_identity_sha256")),
    }


# ---------------------------------------------------------------------------
# 复用闭集（只读）
# ---------------------------------------------------------------------------


def identity_subset(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {name: identity.get(name) for name in REUSE_IDENTITY_FIELDS}


def _relocated_execution_sha256(job: Any, *, current: Mapping[str, str], source: Mapping[str, str]) -> str:
    """当前 Job 执行定义里的 Campaign／候选 ID 换回来源坐标后的执行摘要（长 ID 先换，与 Codex 同一做法）。"""

    replacements = [(current["campaign_id"], source["campaign_id"])]
    if current["candidate_id"] != source["candidate_id"]:
        replacements.append((current["candidate_id"], source["candidate_id"]))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    def relocate(value: Any) -> Any:
        if isinstance(value, str):
            for old, new in replacements:
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, dict):
            return {relocate(key): relocate(item) for key, item in value.items()}
        return value

    return claude_upgrade._fingerprint(relocate(capture._job_execution_payload(job)))


def cross_campaign_producer(context: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """来源结果最初的产出（见文件头第三节）：在来源 Campaign 里产出的补上 ``campaign`` 与 ``execution_sha256``。"""

    from tools.official_client_capture import claude_upgrade_recovery as recovery

    producer = recovery._producer(Path(context["source_dir"]), Path(context["source_root"]), result)
    if "campaign" not in producer:
        producer["campaign"] = dict(context["source_campaign"])
        producer["execution_sha256"] = str(result.get("execution_sha256"))
    return producer


def reuse_plan(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    candidate_id: str,
    jobs: Sequence[Any],
    identity: Mapping[str, Any],
    current_tool: Mapping[str, Any],
    layout: Any,
) -> dict[str, Any]:
    """复用闭集（文件头第二节的身份与逐 Job 判据）与改绑后的复用结果；只读。"""

    from tools.official_client_capture import claude_upgrade_recovery as recovery

    source_attempt = context["source_attempt"]
    if identity_subset(identity) != identity_subset(source_attempt["identity"]) or set(identity) != set(source_attempt["identity"]):
        drift = sorted(name for name in REUSE_IDENTITY_FIELDS if identity.get(name) != source_attempt["identity"].get(name))
        raise ConfigurationError(f"来源 attempt 的候选身份与当前候选不同（{drift}），不是同一个候选构建，证据不复用。")
    coordinates = {"campaign_id": str(manifest["campaign_id"]), "candidate_id": candidate_id}
    source_coordinates = {"campaign_id": str(context["source_campaign"]["campaign_id"]), "candidate_id": str(context["source_candidate_id"])}
    source_results = {str(item.get("id")): item for item in source_attempt.get("results") or [] if isinstance(item, Mapping)}
    source_dir, source_root = Path(context["source_dir"]), Path(context["source_root"])
    reuse_results: dict[str, dict[str, Any]] = {}
    not_reusable: list[dict[str, Any]] = []
    for job in jobs:
        result = source_results.get(job.job_id)
        reasons: list[str] = []
        if result is None:
            reasons.append("source-job-missing")
        else:
            if result.get("status") != "complete":
                reasons.append("source-job-not-complete")
            if _relocated_execution_sha256(job, current=coordinates, source=source_coordinates) != result.get("execution_sha256"):
                reasons.append("execution-definition-changed")
            if capture._job_tool_dependency_files(job, current_tool) != result.get("tool_dependency_files"):
                reasons.append("tool-dependencies-changed")
        if reasons:
            not_reusable.append({"job_id": job.job_id, "reasons": reasons})
            continue
        assert result is not None
        producer = cross_campaign_producer(context, result)
        recovery._verify_reusable_evidence(
            Path(campaign_dir), result, producer, layout, phase=CANDIDATE_PHASE, candidate_id=candidate_id
        )
        incremental = capture._job_incremental_metadata(job, identity=identity, tool_identity=current_tool)
        receipt_path = source_root / f"job-{job.job_id}.json"
        rebased = copy.deepcopy(dict(result))
        rebased.update(
            {
                "execution_sha256": incremental["input_sha256"],
                "tool_dependency_files": dict(incremental["tool_dependency_files"]),
                "input_sha256": incremental["input_sha256"],
                "environment_sha256": incremental["environment_sha256"],
                "dependency_sha256": incremental["dependency_sha256"],
                "incremental_result_key": incremental["result_key"],
                "disposition": "reused",
                "job_closure": None,
                "reused_from": {
                    "attempt_id": str(context["source_attempt_id"]),
                    "attempt": recovery._binding(source_dir, source_root / "attempt.json"),
                    "job_receipt": recovery._binding(source_dir, receipt_path) if receipt_path.is_file() else None,
                    "producer": producer,
                    "campaign": dict(context["source_campaign"]),
                    "rebased_from": {
                        "execution_sha256": result.get("execution_sha256"),
                        "environment_sha256": result.get("environment_sha256"),
                        "incremental_result_key": result.get("incremental_result_key"),
                    },
                },
            }
        )
        reuse_results[job.job_id] = rebased
    planned = sorted(job.job_id for job in jobs)
    reuse = sorted(reuse_results)
    execute = sorted(set(planned) - set(reuse))
    if not reuse:
        raise ConfigurationError(f"来源 attempt 没有任何可复用的候选 Job（{not_reusable}）：直接 resume --candidate-id {candidate_id} 起首个候选 run。")
    return {
        "planned_job_ids": planned,
        "reuse_job_ids": reuse,
        "execute_job_ids": execute,
        "not_reusable": not_reusable,
        "reuse_results": reuse_results,
        "source_results": {job_id: source_results[job_id] for job_id in reuse},
    }


def build_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    context: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    candidate_id: str,
    identity: Mapping[str, Any],
    current_tool: Mapping[str, Any],
) -> dict[str, Any]:
    """零写入、零请求的复用预览（确定性：同一现场重算逐字相同，``preview_sha256`` 即批准摘要）。"""

    source_dir, source_root = Path(context["source_dir"]), Path(context["source_root"])
    source_attempt = context["source_attempt"]
    attempt_path = source_root / "attempt.json"
    result_bindings = [
        {
            "id": job_id,
            "source_result_sha256": claude_upgrade._fingerprint(dict(plan["source_results"][job_id])),
            "evidence_roots": sorted(str(item) for item in plan["source_results"][job_id].get("evidence_roots") or []),
        }
        for job_id in plan["reuse_job_ids"]
    ]
    preview: dict[str, Any] = {
        "schema_version": PREVIEW_SCHEMA,
        "status": "approval_required",
        "campaign_id": str(manifest["campaign_id"]),
        "campaign_manifest_sha256": file_sha256(Path(campaign_dir) / "campaign.json"),
        "candidate_id": candidate_id,
        "source_campaign": {**dict(context["source_campaign"]), "store_identity_sha256": context["store_identity_sha256"]},
        "source_candidate_id": str(context["source_candidate_id"]),
        "source_attempt": {
            "path": attempt_path.relative_to(source_dir).as_posix(),
            "sha256": file_sha256(attempt_path),
            "attempt_id": str(source_attempt["attempt_id"]),
            "attempt_digest": str(source_attempt["attempt_digest"]),
            "status": str(source_attempt["status"]),
        },
        "identity_sha256": claude_upgrade._fingerprint(identity_subset(identity)),
        "planned_job_ids": list(plan["planned_job_ids"]),
        "reuse_job_ids": list(plan["reuse_job_ids"]),
        "execute_job_ids": list(plan["execute_job_ids"]),
        "not_reusable": [dict(item) for item in plan["not_reusable"]],
        "result_bindings": result_bindings,
        "evidence_roots": sorted({root for row in result_bindings for root in row["evidence_roots"]}),
        "tool_files_sha256": str(current_tool.get("files_sha256")),
        "continuity_required": bool(plan["execute_job_ids"]),
        "live_request_count": 0,
    }
    preview["preview_sha256"] = claude_upgrade._fingerprint(preview)
    return validate_preview(preview)


def validate_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = {key: value for key, value in preview.items() if key != "preview_sha256"}
    if (
        set(preview) != PREVIEW_FIELDS
        or preview.get("schema_version") != PREVIEW_SCHEMA
        or preview.get("status") != "approval_required"
        or preview.get("live_request_count") != 0
        or not preview.get("reuse_job_ids")
        or sorted(set(preview["reuse_job_ids"]) | set(preview["execute_job_ids"])) != preview["planned_job_ids"]
        or set(preview["reuse_job_ids"]) & set(preview["execute_job_ids"])
        or claude_upgrade._fingerprint(unsigned) != preview.get("preview_sha256")
    ):
        raise ConfigurationError("后继候选复用预览字段、集合或自摘要非法。")
    return dict(preview)


def source_gateway_bindings(context: Mapping[str, Any]) -> dict[str, Any]:
    """被复用证据的环境前提：来源 attempt 的候选网关运行条件、网关前后快照与隔离恢复核验绑定（路径相对来源 attempt 的
    ``evidence``）。"""

    attempt = context["source_attempt"]
    facts = attempt.get("candidate_gateway") if isinstance(attempt.get("candidate_gateway"), Mapping) else {}
    environment = attempt.get("gateway_environment") if isinstance(attempt.get("gateway_environment"), Mapping) else {}
    recovery = attempt.get("gateway_recovery") if isinstance(attempt.get("gateway_recovery"), Mapping) else {}
    return {
        "run_conditions": dict(facts.get("run_conditions") or {}),
        "gateway_before": environment.get("before"),
        "gateway_after": environment.get("after"),
        "gateway_recovery": recovery.get("file"),
        "drifted_fields": list(environment.get("drifted_fields") or []),
    }


def load_reuse_source(attempt_root: Path) -> dict[str, Any] | None:
    """新 attempt 的复用来源收据（没有即 None）：字段闭集、自摘要、与本 attempt 的预约摘要一致、预览合法。"""

    path = Path(attempt_root) / SOURCE_FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("复用来源收据路径不可信。")
    payload = claude_upgrade._read_json(path, "复用来源收据")
    unsigned = {key: value for key, value in payload.items() if key != "reuse_source_sha256"}
    reservation = claude_upgrade._read_json(Path(attempt_root) / "reservation.json", "抓包预约收据")
    if (
        set(payload) != SOURCE_FIELDS
        or payload.get("schema_version") != SOURCE_SCHEMA
        or payload.get("client") != capture.CLIENT
        or payload.get("attempt_id") != Path(attempt_root).name
        or payload.get("reservation_digest") != reservation.get("reservation_digest")
        or claude_upgrade._fingerprint(unsigned) != payload.get("reuse_source_sha256")
    ):
        raise ConfigurationError("复用来源收据形态、自摘要或预约绑定不一致。")
    preview = validate_preview(payload["preview"])
    if payload.get("approval_sha256") != preview["preview_sha256"]:
        raise ConfigurationError("复用来源收据的批准摘要不是它记录的预览。")
    return payload


# ---------------------------------------------------------------------------
# 入口（claude_upgrade_recovery._resume_candidate 转调）
# ---------------------------------------------------------------------------


class CandidateReuse:
    """一次后继候选复用：预约前在恢复 attempt 主流程里算复用闭集（替代恢复预览的消费与 c1 续跑闭集）。"""

    def __init__(self, campaign_dir: Path, manifest: Mapping[str, Any], arguments: argparse.Namespace, candidate_id: str) -> None:
        self.campaign_dir = Path(campaign_dir)
        self.candidate_id = candidate_id
        self.arguments = arguments
        self.context = source_context(self.campaign_dir, manifest, arguments, candidate_id)
        self.preview: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None

    @property
    def source_root(self) -> Path:
        return Path(self.context["source_root"])

    @property
    def source_attempt_id(self) -> str:
        return str(self.context["source_attempt_id"])

    def compute(self, manifest: Mapping[str, Any], *, jobs: Sequence[Any], identity: Mapping[str, Any], current_tool: Mapping[str, Any], layout: Any) -> dict[str, Any]:
        self.plan = reuse_plan(
            self.campaign_dir, manifest, self.context, candidate_id=self.candidate_id, jobs=jobs, identity=identity,
            current_tool=current_tool, layout=layout,
        )
        self.preview = build_preview(
            self.campaign_dir, manifest, self.context, self.plan, candidate_id=self.candidate_id, identity=identity,
            current_tool=current_tool,
        )
        return self.plan

    def approve(self) -> None:
        """批准：现场重算的预览必须逐字等于批准摘要；执行集合非空时要求显式确认真实请求。"""

        if self.preview is None or self.plan is None:
            raise ConfigurationError("复用闭集尚未计算。")
        if self.arguments.approve_candidate_reuse_sha256 != self.preview["preview_sha256"]:
            raise ConfigurationError(
                "批准摘要与现场重算的复用预览不一致（来源、身份、工具或 Job 定义已变化）：重新 --preview-recovery 后再批准。"
            )
        if self.plan["execute_job_ids"] and not getattr(self.arguments, "acknowledge_live_requests", False):
            raise ConfigurationError(
                f"复用之外还要执行 {self.plan['execute_job_ids']}，会经候选网关发真实请求，必须确认 --acknowledge-live-requests。"
            )

    def source_receipt(self, *, attempt_id: str, reservation: Mapping[str, Any], campaign_id: str) -> dict[str, Any]:
        assert self.preview is not None and self.plan is not None
        payload: dict[str, Any] = {
            "schema_version": SOURCE_SCHEMA,
            "client": capture.CLIENT,
            "campaign_id": campaign_id,
            "attempt_id": attempt_id,
            "reservation_digest": reservation["reservation_digest"],
            "preview": dict(self.preview),
            "approval_sha256": str(self.preview["preview_sha256"]),
            "approved_at_utc": str(reservation["started_at_utc"]),
            "source_gateway": source_gateway_bindings(self.context),
            "reuse_job_ids": list(self.plan["reuse_job_ids"]),
            "execute_job_ids": list(self.plan["execute_job_ids"]),
            "recorded_at_utc": str(reservation["started_at_utc"]),
        }
        payload["reuse_source_sha256"] = claude_upgrade._fingerprint(payload)
        return payload


def preview_command(campaign_dir: Path, manifest: dict[str, Any], arguments: argparse.Namespace, candidate_id: str) -> dict[str, Any]:
    """``--preview-recovery``：零写入、零请求的复用预览（入口门与身份照 run 的只读部分走一遍）。"""

    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

    reuse = CandidateReuse(campaign_dir, manifest, arguments, candidate_id)
    claude_upgrade._reject_contaminated_campaign(Path(campaign_dir))
    current_tool = claude_upgrade._tool_identity(include_git=False)
    claude_upgrade._require_tool_evolution_registered(Path(campaign_dir), manifest, current=current_tool, action="resume --candidate-reuse-source")
    hooks = candidate_capture.CandidateRunHooks(Path(campaign_dir), candidate_id, capture.MODE_FORMAL)
    effective = hooks.admit(manifest, deadline=None)
    jobs = claude_upgrade._campaign_jobs(Path(campaign_dir), effective, CANDIDATE_PHASE, candidate_id=candidate_id)
    layout = capture._capture_layout(Path(campaign_dir), effective)
    identity = hooks.identity(effective, layout)
    reuse.compute(effective, jobs=jobs, identity=identity, current_tool=current_tool, layout=layout)
    assert reuse.preview is not None
    return {
        **reuse.preview,
        "next_command": (
            f"resume --candidate-id {candidate_id} --rerun-failed --candidate-reuse-source-campaign-dir {reuse.context['source_dir']} "
            f"--candidate-reuse-source-candidate-id {reuse.context['source_candidate_id']} --candidate-reuse-source-attempt-id "
            f"{reuse.source_attempt_id} --approve-candidate-reuse-sha256 {reuse.preview['preview_sha256']}"
            + (" --acknowledge-live-requests" if reuse.preview["execute_job_ids"] else "")
        ),
    }
