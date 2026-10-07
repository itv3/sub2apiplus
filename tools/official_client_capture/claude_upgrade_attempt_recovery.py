"""Claude 候选恢复段（B4-05b）：只读协议、对账与证据复用。

恢复段仍属于原采集 attempt，坐标是 ``attempts/<attempt>/recovery/ar<k>``。控制合同沿用共享恢复段合同，
权威执行集合只从预约绑定的 ``b<K>/COMMIT → recovery.json`` 取得。证据读取使用 Claude 的两级权限收口、
Job 闭环收据、relay 清单与网关恢复报告，不能套用 Codex 的环境报告格式。

只读摘要允许真实中断留下的「已有预约、没有摘要」；已有摘要非法必须拒绝。控制链完好而 Job 证据不足时，
该 Job 不进入复用证明集合，由后继恢复预览列入执行集合。所有历史文件只读，不自动修补或删除。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_baseline as baseline_module
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture import incremental_recovery

ConfigurationError = upgrade.ConfigurationError


def _binding(path: Path, root: Path) -> dict[str, Any]:
    return {"path": path.relative_to(root).as_posix(), "sha256": upgrade.file_sha256(path), "bytes": path.stat().st_size}


def require_recovery_revision(value: Any) -> str:
    if not isinstance(value, str) or not vc_artifacts.RECOVERY_REVISION_RE.fullmatch(value):
        raise ConfigurationError("恢复段编号必须是 ar<k>（k≥1）。")
    return value


def _segment_root(campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str) -> Path:
    revision = require_recovery_revision(recovery_revision)
    if not upgrade.SAFE_ID_RE.fullmatch(str(candidate_id)) or not upgrade.SAFE_ID_RE.fullmatch(str(attempt_id)):
        raise ConfigurationError("恢复段候选或 attempt ID 非法。")
    attempt = campaign_dir / "candidates" / candidate_id / "attempts" / attempt_id
    root = upgrade._attempt_recovery_segment_root(attempt, revision)
    upgrade._reject_symlink_components(root, campaign_dir, "Claude 候选恢复段")
    if not root.is_dir():
        raise ConfigurationError(f"候选恢复段不存在：{attempt_id}.{revision}")
    return root


def _bound_document(campaign_dir: Path, binding: Any, expected: Path, label: str) -> dict[str, Any]:
    if not isinstance(binding, Mapping) or binding.get("path") != expected.relative_to(campaign_dir).as_posix():
        raise ConfigurationError(f"{label}绑定的路径不是原 attempt 的文件。")
    upgrade._reject_symlink_components(expected, campaign_dir, label)
    if not expected.is_file() or upgrade.file_sha256(expected) != binding.get("sha256"):
        raise ConfigurationError(f"{label}缺失或摘要漂移。")
    return upgrade._read_json(expected, label)


def load_reservation(
    campaign_dir: Path, segment_root: Path, *, candidate_id: str, attempt_id: str, recovery_revision: str,
) -> dict[str, Any]:
    """只读重放段预约、原 attempt 绑定和执行／复用分割；损坏的控制链不能退化为全部重跑。"""

    expected_root = _segment_root(campaign_dir, candidate_id, attempt_id, recovery_revision)
    if segment_root != expected_root:
        raise ConfigurationError("恢复段预约目录与候选／attempt／段号不一致。")
    path = segment_root / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME
    upgrade._reject_symlink_components(path, campaign_dir, "恢复段预约")
    if not path.is_file():
        raise ConfigurationError(f"恢复段 {recovery_revision} 没有预约收据。")
    value = upgrade._read_json(path, "恢复段预约")
    manifest = upgrade.load_campaign_manifest(campaign_dir)
    unsigned = {key: item for key, item in value.items() if key != "reservation_digest"}
    expected = {
        "schema_version": upgrade.ATTEMPT_RECOVERY_RESERVATION_SCHEMA,
        "campaign_id": manifest["campaign_id"], "campaign_mode": manifest["campaign_mode"],
        "campaign_purpose": manifest["campaign_purpose"], "phase": "candidate",
        "candidate_id": candidate_id, "candidate_purpose": manifest["campaign_purpose"],
        "attempt_id": attempt_id, "recovery_revision": recovery_revision,
        "campaign_manifest_sha256": upgrade.file_sha256(campaign_dir / "campaign.json"),
    }
    if any(value.get(key) != item for key, item in expected.items()) or value.get("reservation_digest") != upgrade._fingerprint(unsigned):
        raise ConfigurationError("恢复段预约的 Campaign、候选、段号或自摘要不一致。")
    if not upgrade.SHA256_RE.fullmatch(str(value.get("run_nonce", ""))) or not upgrade.SHA256_RE.fullmatch(str(value.get("identity_sha256", ""))):
        raise ConfigurationError("恢复段预约缺少合法的运行随机数或身份摘要。")
    original_root = segment_root.parent.parent
    original = _bound_document(campaign_dir, value.get("original_reservation"), original_root / "reservation.json", "原预约")
    attempt = _bound_document(campaign_dir, value.get("original_attempt"), original_root / "attempt.json", "原 attempt")
    if (original.get("run_nonce") != value["original_reservation"].get("run_nonce")
            or attempt.get("attempt_digest") != value["original_attempt"].get("attempt_digest")
            or original.get("client") != "claude" or attempt.get("client") != "claude"
            or original.get("candidate_id") != candidate_id or attempt.get("candidate_id") != candidate_id
            or original.get("attempt_id") != attempt_id or attempt.get("attempt_id") != attempt_id):
        raise ConfigurationError("恢复段绑定的原预约／attempt 身份不一致。")
    planned = value.get("planned_jobs")
    if not isinstance(planned, list) or not planned:
        raise ConfigurationError("恢复段预约缺少计划 Job。")
    ids: list[str] = []
    source_execution = {str(item.get("id")): item.get("execution_sha256") for item in original.get("planned_jobs", []) if isinstance(item, Mapping)}
    for item in planned:
        if (not isinstance(item, Mapping) or not upgrade.SAFE_ID_RE.fullmatch(str(item.get("id", "")))
                or not isinstance(item.get("required"), bool)
                or any(not upgrade.SHA256_RE.fullmatch(str(item.get(key, ""))) for key in ("execution_sha256", "source_execution_sha256"))):
            raise ConfigurationError("恢复段预约的 Job 身份或执行摘要非法。")
        if item["source_execution_sha256"] != source_execution.get(item["id"]):
            raise ConfigurationError("恢复段预约的原执行摘要不属于原 attempt 预约。")
        ids.append(str(item["id"]))
    if len(ids) != len(set(ids)):
        raise ConfigurationError("恢复段预约 Job 重复。")
    reuse_fields = {"execute_job_ids", "reuse_job_ids", "reuse_proofs", "reuse_proofs_sha256", "recovery_review_sha256"}
    if reuse_fields & set(value):
        execute, reuse, proofs = value.get("execute_job_ids"), value.get("reuse_job_ids"), value.get("reuse_proofs")
        if (not reuse_fields <= set(value) or not isinstance(execute, list) or not isinstance(reuse, list)
                or not all(isinstance(item, str) for item in [*execute, *reuse]) or not isinstance(proofs, Mapping)
                or len(set(execute)) != len(execute) or len(set(reuse)) != len(reuse) or set(execute) & set(reuse)
                or set(execute) | set(reuse) != set(ids) or set(proofs) != set(reuse)
                or value["reuse_proofs_sha256"] != upgrade._fingerprint(proofs)
                or not upgrade.SHA256_RE.fullmatch(str(value["recovery_review_sha256"]))):
            raise ConfigurationError("恢复段预约的执行／复用集合或批准摘要非法。")
    return value


def authoritative_execute_jobs(campaign_dir: Path, candidate_id: str, reservation: Mapping[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """预约三元组沿 COMMIT 与 recovery.json 重放取得唯一权威 J*，不信任预约自行声明的执行集合。"""

    baseline = reservation.get("evaluation_baseline")
    if not isinstance(baseline, int) or isinstance(baseline, bool) or baseline < 1:
        raise ConfigurationError("恢复段预约必须绑定正整数评估基线。")
    commit = baseline_module.read_evaluation_baseline_commit(campaign_dir, candidate_id, baseline)
    if (commit.get("commit_sha256") != reservation.get("baseline_commit_sha256")
            or commit.get("recovery_sha256") != reservation.get("recovery_sha256")):
        raise ConfigurationError("恢复段预约的基线 COMMIT／recovery 摘要与权威基线不一致。")
    recovery = upgrade._load_evaluation_baseline_recovery(campaign_dir, candidate_id, baseline)
    if (recovery.get("recovery_sha256") != commit.get("recovery_sha256") or recovery.get("kind") != "attempt-recovery"
            or recovery.get("attempt_id") != reservation.get("attempt_id")):
        raise ConfigurationError("基线 recovery 不是该 attempt 的已提交恢复裁定。")
    planned = [str(item["id"]) for item in reservation["planned_jobs"]]
    if sorted(planned) != sorted(recovery.get("execute_jobs", [])):
        raise ConfigurationError("恢复段预约的计划 Job 与基线冻结的 J* 不一致。")
    return baseline, commit, recovery


def recovery_segment_summary(campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
    """对账只核控制摘要；证据不足交由逐 Job 证明判定，已有非法摘要不能当作中断缺失。"""

    segment = _segment_root(campaign_dir, candidate_id, attempt_id, recovery_revision)
    reservation = load_reservation(campaign_dir, segment, candidate_id=candidate_id, attempt_id=attempt_id, recovery_revision=recovery_revision)
    path = segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
    if not path.exists() and not path.is_symlink():
        return segment, reservation, None
    upgrade._reject_symlink_components(path, campaign_dir, "恢复段摘要")
    value = upgrade._read_json(path, "恢复段摘要")
    unsigned = {key: item for key, item in value.items() if key != "attempt_recovery_digest"}
    expected = {
        "schema_version": upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA, "attempt_id": attempt_id, "candidate_id": candidate_id,
        "recovery_revision": recovery_revision, "run_nonce": reservation["run_nonce"],
        "evaluation_baseline": reservation["evaluation_baseline"], "baseline_commit_sha256": reservation["baseline_commit_sha256"],
    }
    if (any(value.get(key) != item for key, item in expected.items()) or value.get("attempt_recovery_digest") != upgrade._fingerprint(unsigned)
            or value.get("reservation") != {"path": (segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME).relative_to(campaign_dir).as_posix(),
                                              "sha256": upgrade.file_sha256(segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME)}):
        raise ConfigurationError("恢复段摘要身份、预约绑定或自摘要不一致。")
    return segment, reservation, value


def load_segment(campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """消费恢复段时重放权限收口、候选网关文件与继承结果；中断段不能当成功段读取。"""

    segment, reservation, summary = recovery_segment_summary(campaign_dir, candidate_id, attempt_id, recovery_revision)
    if summary is None:
        raise ConfigurationError("恢复段尚未形成摘要，应先对账中断段。")
    authoritative_execute_jobs(campaign_dir, candidate_id, reservation)
    planned = {row["id"]: row["execution_sha256"] for row in reservation["planned_jobs"]}
    results = summary.get("results")
    if (not isinstance(results, list) or not all(isinstance(row, Mapping) for row in results)
            or len({row.get("id") for row in results}) != len(results)
            or any(planned.get(row.get("id")) != row.get("execution_sha256") for row in results)
            or upgrade._fingerprint(dict(summary.get("identity") or {})) != reservation["identity_sha256"]
            or (summary.get("status") == upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS and {row.get("id") for row in results} != set(planned))):
        raise ConfigurationError("恢复段摘要的身份、Job 集合或执行摘要与预约不一致。")
    from tools.official_client_capture import claude_upgrade_segment_seal

    sealed = claude_upgrade_segment_seal.segment_boundary(campaign_dir, segment, summary)
    if sealed is None:
        upgrade._replay_attempt_recovery_evidence_permissions(segment, summary)
    candidate_capture.verify_attempt_bindings(segment, summary)
    if reservation.get("reuse_job_ids") and sealed is None:
        prior = f"ar{int(recovery_revision[2:]) - 1}"
        proofs = segment_reuse_proofs(campaign_dir, candidate_id, attempt_id, prior, [item["id"] for item in reservation["planned_jobs"]])
        rows = {item["id"]: item for item in summary["results"]}
        for job_id in reservation["reuse_job_ids"]:
            if proofs.get(job_id) != reservation["reuse_proofs"][job_id] or rows.get(job_id) != reused_job_result(campaign_dir, proofs[job_id]):
                raise ConfigurationError("恢复段继承结果与已批准的来源证明不一致。")
    return segment, reservation, summary


def terminal_failed(campaign_dir: Path, *, candidate_id: str, attempt_id: str, segment_root: Path) -> bool:
    """成功段不能重开；失败或中断段必须已完成对账及总账绑定，才可作为后继段前序。"""

    _segment, _reservation, summary = recovery_segment_summary(campaign_dir, candidate_id, attempt_id, segment_root.name)
    if summary is not None and summary.get("status") == upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS and not upgrade._failed_job_ids(summary.get("results")):
        return False
    manifest = upgrade.load_campaign_manifest(campaign_dir)
    try:
        binding = supervisor.verify_attempt_reconciliation_binding(campaign_dir, campaign_id=str(manifest["campaign_id"]),
                                                                  candidate_id=candidate_id, attempt_root=segment_root, label=f"恢复段 {segment_root.name} 对账")
    except supervisor.SupervisorError:
        return False
    return binding is not None


def contamination_facts(campaign_dir: Path, attempt_root: Path, *, candidate_id: str) -> list[dict[str, Any]]:
    """恢复段污染状态按同一摘要读侧重放，并保留原 attempt 与段号，供隔离收据精确覆盖。"""

    root = attempt_root / upgrade.ATTEMPT_RECOVERY_DIRNAME
    if not root.exists() and not root.is_symlink():
        return []
    upgrade._reject_symlink_components(root, campaign_dir, "恢复段目录")
    if not root.is_dir():
        raise ConfigurationError("恢复段目录不是普通目录。")
    facts = []
    for segment in sorted(root.iterdir()):
        if segment.name.startswith("."):
            continue
        _segment, _reservation, summary = recovery_segment_summary(campaign_dir, candidate_id, attempt_root.name, segment.name)
        if summary is not None and summary.get("status") == "environment_contaminated":
            facts.append(upgrade._contamination_fact(
                campaign_dir, f"{candidate_id}:{attempt_root.name}.{segment.name}:recovery", "recovery",
                segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, phase="candidate", candidate_id=candidate_id,
                attempt_id=attempt_root.name, recovery_revision=segment.name, at_utc=summary.get("completed_at_utc"),
            ))
    return facts


def job_inventory(roots: Sequence[str], *, scan_stats: dict[str, int] | None = None) -> dict[str, Any]:
    """完成 checkpoint 冻结的逐文件内容；与共享协议同形，但 Claude 不进入 Codex 的评估读侧闭包。"""

    normalized = sorted(set(str(value) for value in roots))
    if not normalized:
        raise ConfigurationError("恢复段 Job 没有证据根。")
    files = []
    for value in normalized:
        root = Path(value)
        if not root.is_absolute() or not root.is_dir():
            raise ConfigurationError("恢复段 Job 证据根不是绝对路径目录。")
        upgrade._reject_symlink_components(root, Path(root.anchor), "恢复段 Job 证据根")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ConfigurationError("恢复段 Job 证据含非普通条目。")
            if path.is_file():
                before = path.stat()
                digest = upgrade.file_sha256(path)
                after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ConfigurationError("恢复段 Job 证据在复算时变化。")
                if scan_stats is not None:
                    scan_stats["scanned_bytes"] = scan_stats.get("scanned_bytes", 0) + before.st_size
                files.append({"path": str(path), "sha256": digest, "bytes": after.st_size})
    if not files or len(files) != len({row["path"] for row in files}):
        raise ConfigurationError("恢复段 Job 证据为空或重复。")
    return {"roots": normalized, "files": files, "content_sha256": upgrade._fingerprint(files)}


def _verified_probe(path: Path, phase: str) -> dict[str, Any]:
    value = upgrade._read_json(path, f"恢复段 {phase} 探针")
    rows = value.get("snapshots")
    if (value.get("schema_version") != capture.PROBE_MANIFEST_SCHEMA or value.get("client") != "claude"
            or value.get("phase") != phase or not isinstance(rows, list) or len(rows) != len(capture.PROBE_STATE_FILES)):
        raise ConfigurationError("恢复段采集容器探针形态非法。")
    kinds = []
    for row in rows:
        if not isinstance(row, Mapping) or capture.PROBE_STATE_FILES.get(str(row.get("kind"))) != row.get("path"):
            raise ConfigurationError("恢复段探针快照路径非法。")
        target = path.parent / row["path"]
        if target.is_symlink() or not target.is_file() or target.stat().st_size != row.get("bytes") or upgrade.file_sha256(target) != row.get("sha256"):
            raise ConfigurationError("恢复段探针快照缺失或摘要漂移。")
        kinds.append(row["kind"])
    if set(kinds) != set(capture.PROBE_STATE_FILES):
        raise ConfigurationError("恢复段探针快照种类重复或不全。")
    return value


def _reuse_environment(segment: Path, summary: Mapping[str, Any] | None, layout: Any) -> tuple[Path, Path, Path]:
    """复用判据的环境部分：权限收口覆盖实际文件，采集容器与网关前后均相等且无残留。

    摘要缺失时仍可按已发布的权限收口与文件实物重放；摘要存在时再核对其绑定。不会只按文件存在判成功。
    """

    permission_path = segment / permissions.RECEIPT_FILENAME
    value = upgrade._read_json(permission_path, "恢复段权限收口")
    binding = permissions.receipt_binding(segment, permission_path)
    if summary is None:
        permissions.replay_evidence_permission_closeout(segment, [Path(root) for root in value["evidence_roots"]], binding,
                                                       managed_data_root=layout.host_data_root, logical_runs_roots=(layout.host_runs_root,))
    else:
        if summary.get("status") == "environment_contaminated" or summary.get("restoration_error") is not None or summary.get("evidence_permission_closeout") != binding:
            raise ConfigurationError("恢复段环境污染、恢复失败或权限绑定不一致，不能复用。")
        upgrade._replay_attempt_recovery_evidence_permissions(segment, summary)
        candidate_capture.verify_attempt_bindings(segment, summary)
    environment = segment / "evidence" / "environment"
    before = environment / "before" / capture.PROBE_MANIFEST_NAME
    after = environment / "after" / capture.PROBE_MANIFEST_NAME
    before_value, after_value = _verified_probe(before, "before"), _verified_probe(after, "after")
    if capture._probe_drift(before_value, after_value) or before_value.get("capture_processes") or after_value.get("capture_processes"):
        raise ConfigurationError("恢复段采集容器前后漂移或残留取证进程。")
    gateway_before = environment / candidate_capture.GATEWAY_BEFORE_FILENAME
    gateway_after = environment / candidate_capture.GATEWAY_AFTER_FILENAME
    first = upgrade._read_json(gateway_before, "恢复段网关前态")
    last = upgrade._read_json(gateway_after, "恢复段网关后态")
    if (first.get("schema_version") != candidate_capture.GATEWAY_ENVIRONMENT_SCHEMA
            or last.get("schema_version") != candidate_capture.GATEWAY_ENVIRONMENT_SCHEMA
            or candidate_capture.gateway_drift(first, last) or candidate_capture.readiness_failures(first)
            or candidate_capture.readiness_failures(last)):
        raise ConfigurationError("恢复段网关未恢复或未就绪。")
    restoration = environment / candidate_capture.GATEWAY_RECOVERY_FILENAME
    recovery = upgrade._read_json(restoration, "恢复段网关恢复核验")
    if (recovery.get("schema_version") != candidate_capture.GATEWAY_RECOVERY_SCHEMA or recovery.get("status") != "passed"
            or recovery.get("environment_equal") is not True or recovery.get("drifted_fields") != []
            or recovery.get("gateway_before") != capture._probe_binding(segment / "evidence", gateway_before)
            or recovery.get("gateway_after") != capture._probe_binding(segment / "evidence", gateway_after)):
        raise ConfigurationError("恢复段网关恢复核验与前后快照不一致。")
    return permission_path, after, restoration


def segment_reuse_proofs(
    campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str, frozen_job_ids: Sequence[str],
    *, scan_stats: dict[str, int] | None = None, _inventory_memo: dict[tuple[str, ...], dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Claude 恢复段的四项复用证明：权威范围、完成 checkpoint、两级收口及内容、环境恢复。"""

    from tools.official_client_capture import claude_upgrade_recovery as recovery_module

    segment, reservation, summary = recovery_segment_summary(campaign_dir, candidate_id, attempt_id, recovery_revision)
    _baseline, _commit, recovery = authoritative_execute_jobs(campaign_dir, candidate_id, reservation)
    if sorted(recovery["execute_jobs"]) != sorted(frozen_job_ids):
        raise ConfigurationError("恢复段复用范围不是基线冻结的 J*。")
    layout = capture._capture_layout(campaign_dir, upgrade.load_campaign_manifest(campaign_dir))
    memo = {} if _inventory_memo is None else _inventory_memo
    _records, checkpoints, _chain = reconciler._checkpoint_facts(segment)
    try:
        permission_path, after, restoration = _reuse_environment(segment, summary, layout)
    except (OSError, KeyError, ValueError, ConfigurationError, permissions.EvidencePermissionError):
        return {}
    inherited = set(reservation.get("reuse_job_ids", []))
    previous = segment_reuse_proofs(campaign_dir, candidate_id, attempt_id, f"ar{int(recovery_revision[2:]) - 1}", frozen_job_ids,
                                   scan_stats=scan_stats, _inventory_memo=memo) if inherited else {}
    planned = {row["id"]: row for row in reservation["planned_jobs"]}
    results = {row["id"]: row for row in summary.get("results", [])} if summary else {}
    proofs = {}
    for job_id in sorted(frozen_job_ids):
        checkpoint, path = checkpoints.get(job_id), segment / f"job-{job_id}.json"
        if not checkpoint or not path.is_file() or path.is_symlink():
            continue
        result = upgrade._read_json(path, "恢复段 Job")
        if (result.get("status") != "complete" or result.get("id") != job_id or checkpoint.get("status") != "complete"
                or checkpoint.get("attempt_id") != f"{attempt_id}.{recovery_revision}" or checkpoint.get("phase") != "candidate"
                or checkpoint.get("run_nonce") != reservation["run_nonce"] or checkpoint.get("result") != result
                or checkpoint.get("result_sha256") != incremental_recovery.digest(result)
                or result.get("execution_sha256") != planned[job_id]["execution_sha256"]
                or result.get("source_execution_sha256", planned[job_id]["source_execution_sha256"]) != planned[job_id]["source_execution_sha256"]
                or (summary is not None and results.get(job_id) != result)):
            continue
        # 出口绑定损坏是控制链错误，必须直接拒绝；不吞成「该 Job 重跑」。
        if not reconciler._job_egress_trusted(result):
            continue
        if job_id in inherited and (job_id not in previous or reservation["reuse_proofs"].get(job_id) != previous[job_id]):
            continue
        try:
            source_revision = str(result.get("recovered_from", recovery_revision))
            source_root = _segment_root(campaign_dir, candidate_id, attempt_id, source_revision)
            closure = result.get("job_closure")
            if not isinstance(closure, Mapping):
                continue
            closure_path = source_root / str(closure.get("path", ""))
            producer = {"attempt_id": f"{attempt_id}.{source_revision}", "job_closure": _binding(closure_path, campaign_dir)}
            if producer["job_closure"]["sha256"] != closure.get("sha256"):
                continue
            recovery_module._verify_reusable_evidence(campaign_dir, result, producer, layout, phase="candidate", candidate_id=candidate_id,
                                                      _producer_root=source_root)
            roots = tuple(sorted(str(capture._host_path(str(root), layout)) for root in result.get("evidence_roots", [])))
            inventory = memo.get(roots)
            if inventory is None:
                inventory = job_inventory(roots, scan_stats=scan_stats)
                memo[roots] = inventory
            if checkpoint.get("recovery_evidence") != inventory:
                continue
            checkpoint_path = segment / "checkpoints" / f"{checkpoint['checkpoint_sequence']:08d}.json"
            proofs[job_id] = {
                "recovered_from": source_revision, "source_execution_sha256": planned[job_id]["source_execution_sha256"],
                "result": _binding(path, campaign_dir), "checkpoint": _binding(checkpoint_path, campaign_dir),
                "permission_closeout": _binding(permission_path, campaign_dir), "after_probe": _binding(after, campaign_dir),
                "restoration_report": _binding(restoration, campaign_dir), "content_sha256": inventory["content_sha256"],
            }
        except (OSError, KeyError, ValueError, ConfigurationError, permissions.EvidencePermissionError):
            continue
    return proofs


def reused_job_result(campaign_dir: Path, proof: Mapping[str, Any]) -> dict[str, Any]:
    """继承结果明确引用原结果和 checkpoint，不改变原始 Job 闭环身份，也不重复记录请求。"""

    binding = proof["result"]
    path = upgrade._campaign_file(campaign_dir, str(binding["path"]))
    if upgrade.file_sha256(path) != binding.get("sha256") or path.stat().st_size != binding.get("bytes"):
        raise ConfigurationError("恢复段复用来源收据漂移。")
    source = upgrade._read_json(path, "恢复段复用 Job")
    value = {key: item for key, item in source.items() if key not in {"started_at_utc", "completed_at_utc"}}
    value.update(disposition="reused", recovered_from=proof["recovered_from"], source_receipt=dict(binding),
                 recovery_checkpoint=dict(proof["checkpoint"]), source_execution_sha256=proof["source_execution_sha256"])
    return value
