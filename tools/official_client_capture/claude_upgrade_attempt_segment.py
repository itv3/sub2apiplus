"""Claude 候选恢复段的写侧（B4-05b-3）。

恢复段只消费已提交 transient 裁定冻结的 J*；失败后继另消费直接前段的已批准恢复预览。预约与计时账本沿用共享协议，
预约后的执行体与普通 Claude 续跑共用，环境、网关与权限证据保留 Claude 的专属合同。成功段只能幂等重放。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_attempt_recovery as reader
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_candidate_seal as seal
from tools.official_client_capture import claude_upgrade_recovery as recovery_module
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane as control
from tools.official_client_capture.capturelib.model import ConfigurationError


def _scope() -> Any:
    return control.adapter_scope(upgrade_client_adapter.adapter_for("claude"))


def segment_path(campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str) -> Path:
    """写侧只构造并核验坐标，不要求段已存在；目录的唯一创建者是锁内预约函数。"""

    reader.require_recovery_revision(recovery_revision)
    if not upgrade.SAFE_ID_RE.fullmatch(candidate_id) or not upgrade.SAFE_ID_RE.fullmatch(attempt_id):
        raise ConfigurationError("恢复段候选或 attempt ID 非法。")
    root = upgrade._attempt_recovery_segment_root(campaign_dir / "candidates" / candidate_id / "attempts" / attempt_id, recovery_revision)
    upgrade._reject_symlink_components(root, campaign_dir, "Claude 恢复段写入坐标")
    return root


def current_baseline(
    campaign_dir: Path, candidate_id: str, recovery_revision: str, *,
    allow_successor: bool = False, recovery_preview: Path | None = None, require_preview: bool = False,
) -> tuple[int, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """当前 COMMIT → recovery 的授权闭包；后继段必须连续且前段均已失败对账，批准只能指向直接前段。"""

    reader.require_recovery_revision(recovery_revision)
    baseline, commit = upgrade._current_evaluation_baseline(campaign_dir, candidate_id)
    if baseline < 1 or commit is None:
        raise ConfigurationError("当前没有已提交的 attempt-recovery 评估基线，不能开恢复段。")
    recovery = upgrade._load_evaluation_baseline_recovery(campaign_dir, candidate_id, baseline)
    if recovery.get("kind") != "attempt-recovery" or recovery.get("recovery_sha256") != commit.get("recovery_sha256"):
        raise ConfigurationError("当前 COMMIT 没有授权这一份 attempt-recovery 裁定。")
    frozen = reader.require_recovery_revision(recovery.get("recovery_revision"))
    if recovery_revision == frozen:
        if require_preview:
            raise ConfigurationError("--rerun-failed 只用于失败段的后继，首段直接按裁定执行。")
        return baseline, commit, recovery, None
    if not allow_successor:
        raise ConfigurationError(f"裁定冻结的是 {frozen}，后继段必须显式 --rerun-failed。")
    attempt_id = str(recovery["attempt_id"])
    parent = segment_path(campaign_dir, candidate_id, attempt_id, frozen).parent
    if not parent.is_dir():
        raise ConfigurationError("裁定冻结的首段尚未预约，不能越过首段开后继。")
    existing = []
    for child in parent.iterdir():
        if child.name.startswith("."):
            continue
        reader.require_recovery_revision(child.name)
        if child.is_symlink() or not child.is_dir():
            raise ConfigurationError("恢复段目录含不可信条目。")
        existing.append(int(child.name[2:]))
    first, target = int(frozen[2:]), int(recovery_revision[2:])
    if (not existing or first not in existing or target <= first or target not in {max(existing), max(existing) + 1}
            or not set(range(first, target)) <= set(existing)):
        raise ConfigurationError("后继恢复段编号必须连续，不能跳段或回退。")
    for number in range(first, target):
        if not reader.terminal_failed(campaign_dir, candidate_id=candidate_id, attempt_id=attempt_id, segment_root=parent / f"ar{number}"):
            raise ConfigurationError(f"ar{number} 不是已对账的失败终态，不能开后继。")
    preview = None
    if require_preview:
        if recovery_preview is None:
            raise ConfigurationError("后继恢复段必须提供直接前段已批准的 --recovery-preview。")
        target_root = parent / recovery_revision
        if target_root.is_dir():
            frozen_reservation = reader.load_reservation(campaign_dir, target_root, candidate_id=candidate_id,
                                                        attempt_id=attempt_id, recovery_revision=recovery_revision)
            consumed = upgrade._read_json(recovery_preview, "已消费的恢复预览")
            core = {key: value for key, value in consumed.items() if key not in {"created_at_utc", "review_sha256"}}
            if (consumed.get("review_sha256") != frozen_reservation.get("recovery_review_sha256")
                    or upgrade._fingerprint(core) != consumed.get("review_sha256")
                    or consumed.get("source_attempt_id") != attempt_id or consumed.get("candidate_id") != candidate_id
                    or consumed.get("recovery_revision") != f"ar{target - 1}"):
                raise ConfigurationError("已存在恢复段与提供的批准预览不一致。")
            return baseline, commit, recovery, consumed
        with _scope():
            preview = reconciler.load_approved_recovery_preview(
                campaign_dir, recovery_preview, phase="candidate", candidate_id=candidate_id,
                recovery_revision=f"ar{target - 1}",
            )
        if preview.get("source_attempt_id") != attempt_id:
            raise ConfigurationError("后继段恢复预览绑定的原 attempt 不一致。")
        previous_reservation = reader.load_reservation(campaign_dir, parent / f"ar{target - 1}", candidate_id=candidate_id,
                                                       attempt_id=attempt_id, recovery_revision=f"ar{target - 1}")
        authoritative = reader.authoritative_execute_jobs(campaign_dir, candidate_id, previous_reservation)[2]
        if authoritative["recovery_sha256"] != recovery["recovery_sha256"]:
            raise ConfigurationError("前段预约与当前裁定不属于同一基线。")
        upgrade._require_recovery_preview_scope_equals_frozen_jobs(preview, authoritative["execute_jobs"], label="Claude 后继恢复段")
    return baseline, commit, recovery, preview


def previous_stage(campaign_dir: Path, candidate_id: str, baseline: int, manifest: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    """按基线权威读来源读取前序封存结果，不受当前 b<K> 是否已有输出影响。"""

    source = upgrade._stage_read_source(campaign_dir, candidate_id, baseline, "capture-candidate")
    if source["status"] != "complete":
        raise ConfigurationError(f"前序基线 b{baseline} 的候选证据尚未封存。")
    path = Path(source["path"])
    return path, seal.load_stage_result(campaign_dir, path, candidate_id, manifest=manifest)


def validate_recovery_admission(campaign_dir: Path, candidate_id: str, recovery_revision: str) -> None:
    """仅替代普通候选准入的『未封存』门；仍必须有当前裁定、已封存来源和同一原 attempt。"""

    _baseline, _commit, recovery, _preview = current_baseline(campaign_dir, candidate_id, recovery_revision, allow_successor=True)
    manifest = upgrade._require_formal_campaign(campaign_dir)
    _path, stage = previous_stage(campaign_dir, candidate_id, int(recovery["previous_baseline"]), manifest)
    root, attempt = seal.sealed_attempt_context(campaign_dir, stage, candidate_id=candidate_id)
    if root.name != recovery["attempt_id"] or attempt.get("status") != upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS:
        raise ConfigurationError("恢复裁定没有绑定已封存且成功收口的原 attempt。")


def run_segment(arguments: Any, *, _lease: Any | None = None, _manifest: dict[str, Any] | None = None, _deadline: Any | None = None) -> dict[str, Any]:
    """段命令共用单一 lease 和墙钟预算；预约之前完成授权、候选准入、执行树、二进制和环境检查。"""

    revision = reader.require_recovery_revision(getattr(arguments, "attempt_recovery", None))
    candidate_id = candidate_capture.require_candidate_id(arguments)
    if getattr(arguments, "zero_request_rehearsal", False) or getattr(arguments, "job_id", None):
        raise ConfigurationError("恢复段只接受正式裁定冻结的执行集合，不接受演练或单 Job 覆盖。")
    if not getattr(arguments, "acknowledge_live_requests", False):
        raise ConfigurationError("恢复段补跑必须确认 --acknowledge-live-requests。")
    campaign_dir = Path(arguments.campaign_dir)
    upgrade._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    manifest = _manifest or upgrade._require_formal_campaign(campaign_dir)
    active = upgrade._ACTIVE_CAMPAIGN_LEASE
    if _lease is None and active is not None:
        if not upgrade._lease_identity_matches(active, campaign_dir, phase="candidate", candidate_id=candidate_id):
            raise ConfigurationError("恢复段与当前 Campaign lease 身份不一致。")
        _lease = active
    deadline = capture._bind_attempt_deadline_metadata(_deadline or (_lease.deadline if _lease is not None else upgrade._attempt_deadline(arguments, "candidate")), "candidate")
    if _lease is None:
        with upgrade.CampaignLease(campaign_dir, phase="candidate", candidate_id=candidate_id, deadline=deadline,
                                   command="capture-candidate", allow_stale_recovery=bool(getattr(arguments, "rerun_failed", False)),
                                   campaign_id=str(manifest["campaign_id"])) as lease:
            return run_segment(arguments, _lease=lease, _manifest=manifest, _deadline=deadline)
    baseline, commit, recovery, preview = current_baseline(
        campaign_dir, candidate_id, revision, allow_successor=bool(getattr(arguments, "rerun_failed", False)),
        recovery_preview=getattr(arguments, "recovery_preview", None), require_preview=bool(getattr(arguments, "rerun_failed", False)),
    )
    attempt_id = str(recovery["attempt_id"])
    segment_root = segment_path(campaign_dir, candidate_id, attempt_id, revision)
    upgrade._reject_contaminated_campaign(campaign_dir)
    if segment_root.exists():
        _segment, _reservation, summary = reader.load_segment(campaign_dir, candidate_id, attempt_id, revision)
        if summary.get("status") != upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS or upgrade._failed_job_ids(summary.get("results")):
            raise ConfigurationError("已有恢复段未成功收口：先按 --recovery-revision 对账，再批准后继段，禁止原段续写。")
        return {"status": summary["status"], "reused": True, "phase": "candidate", "candidate_id": candidate_id,
                "attempt_id": attempt_id, "recovery_revision": revision, "evaluation_baseline": baseline,
                "attempt_recovery_digest": summary["attempt_recovery_digest"], "live_request_count": 0}
    current_tool = upgrade._tool_identity(include_git=False)
    upgrade._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action="capture-candidate run --attempt-recovery")
    hooks = candidate_capture.CandidateRunHooks(campaign_dir, candidate_id, capture.MODE_FORMAL, recovery_revision=revision)
    manifest = hooks.admit(manifest, deadline=deadline)
    _previous_path, prior_stage = previous_stage(campaign_dir, candidate_id, int(recovery["previous_baseline"]), manifest)
    attempt_root, original = seal.sealed_attempt_context(campaign_dir, prior_stage, candidate_id=candidate_id)
    original_reservation = upgrade._load_capture_reservation(campaign_dir, attempt_root, phase="candidate", candidate_id=candidate_id, _manifest=manifest)
    layout = capture._capture_layout(campaign_dir, manifest)
    identity = hooks.identity(manifest, layout)
    if identity != original.get("identity"):
        raise ConfigurationError("恢复段候选身份与原 attempt 漂移，不能复用原证据。")
    source_jobs = {job.job_id: job for job in upgrade._campaign_jobs(campaign_dir, manifest, "candidate", candidate_id=candidate_id)}
    execute, retained = set(recovery["execute_jobs"]), set(recovery["reuse_jobs"])
    if not execute or execute & retained or execute | retained != set(source_jobs):
        raise ConfigurationError("裁定执行／复用集合必须恰好分割候选 Job 全集。")
    with _scope():
        jobs = [control._relocate_job_for_recovery(source_jobs[job_id], revision) for job_id in sorted(execute)]
    execute_ids = set(preview["execute_job_ids"]) if preview is not None else execute
    execute_jobs = [job for job in jobs if job.job_id in execute_ids]
    for job in execute_jobs:
        capture._job_tool_dependency_files(job, current_tool)
        for logical in job.evidence_roots:
            path = capture._host_path(str(logical), layout)
            if path.exists() or path.is_symlink():
                raise ConfigurationError(f"恢复段新证据根已被占用，禁止覆盖：{path}")
    upgrade._verify_execution_tree(capture._host_path(layout.capture_tool_root, layout).parents[1])
    binary = upgrade._verify_official_binaries(manifest, deadline=deadline)
    preconditions = capture._container_preconditions(manifest, layout, mode=capture.MODE_FORMAL, jobs=execute_jobs,
                                                     deadline=deadline, heartbeat=None, phase="candidate")
    recovery_module._require_no_residual_processes(manifest, deadline)
    hooks.pre_reservation(manifest, layout, execute_jobs, deadline=deadline)
    capture._require_capture_budget(deadline, operation="attempt-recovery:reservation-admission", reservation=True)
    with _scope():
        segment_root, reservation = control._reserve_attempt_recovery(
            campaign_dir, manifest=manifest, candidate_id=candidate_id, attempt_root=attempt_root,
            original_attempt=original, original_reservation=original_reservation, recovery=recovery,
            recovery_revision=revision, baseline=baseline, baseline_commit_sha256=str(commit["commit_sha256"]), jobs=jobs,
            source_jobs=source_jobs, identity=identity, lease=_lease, deadline=deadline, reuse_preview=preview,
        )
        ledger_dir = upgrade._campaign_timing_ledger_dir(campaign_dir, manifest)
        candidate_revision = upgrade._current_candidate_revision(campaign_dir, manifest)
        with supervisor._timing_closeout_lock(ledger_dir):
            control._backfill_original_attempt_ledger_events(ledger_dir, attempt_id=attempt_id, live_request_count=int(prior_stage.get("live_request_count", 0)))
            control._append_attempt_recovery_ledger_event(ledger_dir, event_type="attempt_recovery_started", attempt_id=attempt_id,
                recovery_revision=revision, candidate_id=candidate_id, revision=candidate_revision,
                root_cause_id=str(recovery["root_cause_id"]), next_action=f"执行 Claude 恢复段 {revision}")
    reused_results = {job_id: reader.reused_job_result(campaign_dir, reservation["reuse_proofs"][job_id]) for job_id in reservation.get("reuse_job_ids", [])}
    return recovery_module._execute_recovery_attempt(
        campaign_dir, manifest, lease=_lease, deadline=deadline, phase="candidate", candidate_id=candidate_id,
        attempt_root=segment_root, reservation=reservation, recovery_source={}, hooks=hooks, jobs=jobs, execute_jobs=execute_jobs,
        identity=identity, current_tool=current_tool, layout=layout, binary_verification=binary, preconditions=preconditions,
        plan={"reuse_job_ids": sorted(reused_results), "reuse_results": reused_results}, source_id=attempt_id,
        segment={"ledger_dir": ledger_dir, "candidate_revision": candidate_revision, "recovery": recovery},
    )


def write_summary(campaign_dir: Path, manifest: Mapping[str, Any], segment_root: Path, reservation: Mapping[str, Any],
                  payload: dict[str, Any], *, segment: Mapping[str, Any]) -> dict[str, Any]:
    """写不可变段摘要；成功事件只登记状态，请求数由 seal 后的来源核算按身份键单独入账。"""

    if upgrade._fingerprint(dict(payload["identity"])) != reservation["identity_sha256"]:
        raise ConfigurationError("恢复段身份与预约摘要不一致。")
    payload["failure_observations"] = payload.pop("attempt_failure_observations", [])
    payload["next_gate"] = (
        f"capture-candidate seal --candidate-id {reservation['candidate_id']} --attempt-id {reservation['attempt_id']} --attempt-recovery {reservation['recovery_revision']}"
        if payload["status"] == upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS else
        f"reconcile-attempt --candidate-id {reservation['candidate_id']} --attempt-id {reservation['attempt_id']} --recovery-revision {reservation['recovery_revision']}"
    )
    with _scope():
        summary = control._write_attempt_recovery_summary(campaign_dir, segment_root, reservation, payload)
        if summary["status"] == upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS:
            with supervisor._timing_closeout_lock(segment["ledger_dir"]):
                control._append_attempt_recovery_ledger_event(segment["ledger_dir"], event_type="attempt_recovery_completed",
                    attempt_id=str(reservation["attempt_id"]), recovery_revision=str(reservation["recovery_revision"]),
                    candidate_id=str(reservation["candidate_id"]), revision=segment["candidate_revision"], live_request_count=0,
                    next_action="Claude 恢复段已完成：增量封存后按身份键入账")
    return summary


def segment_status(campaign_dir: Path, candidate_id: str) -> dict[str, str] | None:
    """当前 transient 基线尚未 seal 时给出精确停点；已完成本基线封存则由原评估状态机继续。"""

    baseline, commit = upgrade._current_evaluation_baseline(campaign_dir, candidate_id)
    if baseline < 1 or commit is None:
        return None
    recovery = upgrade._load_evaluation_baseline_recovery(campaign_dir, candidate_id, baseline)
    if recovery.get("kind") != "attempt-recovery":
        return None
    if upgrade._stage_read_source(campaign_dir, candidate_id, baseline, "capture-candidate")["status"] == "complete":
        return None
    frozen = reader.require_recovery_revision(recovery["recovery_revision"])
    attempt_id = str(recovery["attempt_id"])
    parent = segment_path(campaign_dir, candidate_id, attempt_id, frozen).parent
    existing = sorted((entry for entry in parent.iterdir() if not entry.name.startswith(".")),
                      key=lambda entry: int(reader.require_recovery_revision(entry.name)[2:])) if parent.is_dir() else []
    revision = existing[-1].name if existing else frozen
    run = f"capture-candidate run --candidate-id {candidate_id} --attempt-recovery {revision} --acknowledge-live-requests"
    if not existing:
        return {"status": "candidate_recovery_required", "next_command": run}
    _segment, _reservation, summary = reader.recovery_segment_summary(campaign_dir, candidate_id, attempt_id, revision)
    if summary is not None and summary.get("status") == upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS and not upgrade._failed_job_ids(summary.get("results")):
        return {"status": "candidate_recovery_awaiting_seal", "next_command":
                f"capture-candidate seal --candidate-id {candidate_id} --attempt-id {attempt_id} --attempt-recovery {revision}（先预览，再按摘要批准）"}
    if summary is None:
        return {"status": "candidate_recovery_interrupted", "next_command":
                f"reconcile-attempt --candidate-id {candidate_id} --attempt-id {attempt_id} --recovery-revision {revision}"}
    return {"status": "candidate_recovery_failed", "next_command":
            f"reconcile-attempt --candidate-id {candidate_id} --attempt-id {attempt_id} --recovery-revision {revision}；批准预览后以 --rerun-failed 开 ar{int(revision[2:]) + 1}"}
