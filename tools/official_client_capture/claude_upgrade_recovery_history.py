"""Claude 跨基线恢复段的只读承接判据。

共享预约器在 Campaign 锁内，且旧段未通过原 terminal_failed 时调用本模块。只有当前 COMMIT 的前序链中，
已成功封存并在实际项目总账入账的段可以承接；当前基线成功段仍不可重开。预约／摘要／seal／总账继续复用各自
既有读侧合同，不创建第二份预约器，不修补历史字节，不补账，也不获取项目锁（避免与 Campaign 锁反向等待）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_attempt_recovery as reader
from tools.official_client_capture import claude_upgrade_baseline as baselines
from tools.official_client_capture import claude_upgrade_candidate_seal as seal
from tools.official_client_capture import claude_upgrade_segment_seal as segment_seal
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.capturelib.model import ConfigurationError


def _baseline_chain(
    campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, baseline: int,
    baseline_commit_sha256: str, recovery: Mapping[str, Any],
) -> dict[int, dict[str, Any]]:
    """从计时账本当前 COMMIT 逐份核 recovery.previous_baseline；目录存在本身不构成授权。"""

    current, commit = baselines.orchestrator_current_baseline(campaign_dir, candidate_id)
    if current != baseline or commit is None or commit["commit_sha256"] != baseline_commit_sha256:
        raise ConfigurationError("锁内当前评估基线／COMMIT 已变化，禁止承接前序恢复段。")
    revision = commit["candidate_revision"]
    chain: dict[int, dict[str, Any]] = {}
    number, expected_sha256 = baseline, baseline_commit_sha256
    while number:
        directory = upgrade._evaluation_baseline_dir(campaign_dir, candidate_id, number)
        upgrade._reject_symlink_components(directory, campaign_dir, "恢复段前序基线")
        item = baselines.read_evaluation_baseline_commit(campaign_dir, candidate_id, number)
        frozen = baselines.load_evaluation_baseline_recovery(campaign_dir, candidate_id, number)
        if (item["commit_sha256"] != expected_sha256 or item["recovery_sha256"] != frozen["recovery_sha256"]
                or item["kind"] != frozen["kind"]
                or any(document["campaign_id"] != manifest["campaign_id"]
                       or document["candidate_revision"] != revision for document in (item, frozen))):
            raise ConfigurationError("恢复段前序 COMMIT 链的 Campaign、revision 或 recovery 绑定不一致。")
        if number == baseline and dict(frozen) != dict(recovery):
            raise ConfigurationError("预约参数不是当前 COMMIT 冻结的恢复裁定。")
        chain[number] = item
        previous = frozen["previous_baseline"]
        if previous in chain or previous >= number:
            raise ConfigurationError("恢复段前序基线链回环或没有严格递减。")
        number, expected_sha256 = previous, frozen["previous_baseline_commit_sha256"]
    return chain


def _bound_stage(campaign_dir: Path, binding: Any) -> Path:
    """只接受 Campaign 内规范阶段路径；摘要必须仍等于封存时的引用。"""

    if not isinstance(binding, Mapping):
        raise ConfigurationError("前序候选阶段缺少文件绑定。")
    path = upgrade._campaign_file(campaign_dir, str(binding.get("path", "")))
    upgrade._reject_symlink_components(path, campaign_dir, "前序候选阶段")
    if not path.is_file() or upgrade.file_sha256(path) != binding.get("sha256"):
        raise ConfigurationError("前序候选阶段缺失或摘要漂移。")
    return path


def _sealed_stage_for_segment(
    campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, attempt_id: str,
    summary_path: Path, summary: Mapping[str, Any], recovery: Mapping[str, Any],
    chain: Mapping[int, Mapping[str, Any]],
) -> tuple[Path, dict[str, Any]] | None:
    """从新裁定的真实前序阶段追溯 previous_stage，不接受游离的成功段或只写了一半的 seal。"""

    source = upgrade._stage_read_source(campaign_dir, candidate_id, int(recovery["previous_baseline"]), "capture-candidate")
    if source["status"] != "complete":
        return None
    path = _bound_stage(campaign_dir, {"path": Path(source["path"]).relative_to(campaign_dir).as_posix(),
                                       "sha256": source["sha256"]})
    visited: set[Path] = set()
    while path not in visited:
        visited.add(path)
        stage = seal.load_stage_result(campaign_dir, path, candidate_id, manifest=manifest)
        if stage.get("status") != "complete":
            return None
        binding = stage.get("recovery")
        if not isinstance(binding, Mapping):
            return None
        bound_summary = _bound_stage(campaign_dir, binding)
        segment_baseline = binding.get("evaluation_baseline")
        if segment_baseline not in chain:
            raise ConfigurationError("候选 seal 来源不属于当前 COMMIT 的前序链。")
        original_root, original = seal.sealed_attempt_context(campaign_dir, stage, candidate_id=candidate_id)
        expected_root = campaign_dir / "candidates" / candidate_id / "attempts" / attempt_id
        if original_root != expected_root or original.get("attempt_id") != attempt_id:
            raise ConfigurationError("前序 seal 不是当前裁定绑定的原 attempt。")
        if bound_summary == summary_path:
            if (binding.get("recovery_revision") != summary["recovery_revision"]
                    or segment_baseline != summary["evaluation_baseline"]):
                raise ConfigurationError("前序 seal 的段号或基线与成功段不一致。")
            return path, stage
        path = _bound_stage(campaign_dir, stage.get("previous_stage"))
    raise ConfigurationError("前序候选 seal 链发生回环。")


def _accounted_segment(
    campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, attempt_id: str,
    stage_path: Path, stage: Mapping[str, Any], summary_path: Path, summary: Mapping[str, Any],
) -> bool:
    """重放实际总账与 outbox COMMIT，核对同一个封存段的唯一入账事件；全程无写入、无项目锁。"""

    root = ledger.find_project_ledger(campaign_dir, client="claude")
    if root is None:
        return False
    try:
        plan, _raw = ledger._load_plan(root)
        # 事件只会追加且单份原子发布；复用既有只读重放器验证完整链，不调用会刷新缓存的 replay_head。
        events = ledger._load_events(root)
        head = ledger._replay(root, plan, events, rebuild_cache=False)
        campaign_plan = ledger._campaign_plan(campaign_dir / ledger.CAMPAIGN_LEDGER_DIR_NAME)
        registered = head["registered_campaigns"].get(manifest["campaign_id"])
        if (ledger._plan_client(plan) != "claude" or campaign_plan is None or registered is None
                or ledger._campaign_plan_client(campaign_plan) != "claude"
                or campaign_plan["campaign_id"] != manifest["campaign_id"]
                or campaign_plan["project_ledger"] != str(root)
                or registered.get("client") != "claude" or registered.get("campaign_dir") != str(campaign_dir)
                or registered.get("operation_id") != campaign_plan["registration_operation_id"]):
            raise ConfigurationError("前序成功段没有本客户端、同 Campaign 的实际项目总账登记。")
        subject = f"{candidate_id}:{attempt_id}:{summary['recovery_revision']}"
        operation = f"account-sealed-candidate:{subject}"
        matches = [event for event in events if event["operation_id"] == operation]
        if not matches:
            return False
        event = matches[0]
        expected = {
            "campaign_id": manifest["campaign_id"], "subject_kind": "sealed_candidate_stage", "subject_id": subject,
            "phase": "candidate", "candidate_id": candidate_id, "recovery_revision": summary["recovery_revision"],
            "stage_result_sha256": upgrade.file_sha256(stage_path), "attempt_sha256": stage["attempt"]["sha256"],
            "attempt_recovery_sha256": upgrade.file_sha256(summary_path),
        }
        overlays, _audit = ledger._correction_overlays(events)
        payload = overlays.get(operation, event["payload"])
        request = payload.get("request")
        if (event["event_type"] != "reconciliation_committed"
                or any(payload.get(key) != value for key, value in expected.items())
                or not isinstance(request, Mapping) or request.get("status") not in {"resolved", "estimated"}
                or not set(request.get("identity_keys", [])) <= set(head["accounted_identity_index"]) | ledger._initial_keys(root, plan)):
            raise ConfigurationError("前序段入账事件没有绑定完整 seal、原 attempt 或成功段摘要。")
        previous_sha256 = None
        outbox = campaign_dir / ledger.CAMPAIGN_LEDGER_DIR_NAME / "outbox"
        for sequence, batch_dir in ledger._batch_dirs(outbox):
            batch = ledger._read_batch(batch_dir)
            if not batch["committed"]:
                return False
            commit = batch["commit"]
            if commit["batch_sequence"] != sequence or commit["previous_batch_sha256"] != previous_sha256:
                raise ConfigurationError("前序段入账 outbox 的 COMMIT 链不连续。")
            previous_sha256 = commit["batch_sha256"]
            if commit["operation_id"] == operation:
                if (commit["event_type"] != event["event_type"] or commit["batch_sha256"] != event["source_batch_sha256"]
                        or ledger._merge_batch_payload(batch) != event["payload"]):
                    raise ConfigurationError("前序段入账事件与实际 outbox COMMIT／payload 不一致。")
                return True
        return False
    except ledger.ProjectLedgerError as error:
        raise ConfigurationError(f"前序段实际项目总账无法只读重放：{error}") from error


def sealed_predecessor_segment(
    campaign_dir: Path, *, manifest: Mapping[str, Any], candidate_id: str, attempt_id: str,
    segment_root: Path, baseline: int, baseline_commit_sha256: str, recovery: Mapping[str, Any],
) -> bool:
    """锁内额外准入：更早基线的成功段，必须同时在当前前序链、完整 seal 链和实际入账链中。

    本函数不能替代 terminal_failed；失败／中断段继续走原判据。成功但未 seal／未入账返回 False，非法既有
    文档或摘要漂移直接拒绝。Codex 同名适配成员恒为 False，Codex 成功段历史语义不变。
    """

    campaign_dir, segment_root = Path(campaign_dir), Path(segment_root)
    actual_manifest = upgrade.load_campaign_manifest(campaign_dir)
    if actual_manifest.get("client") != "claude" or dict(actual_manifest) != dict(manifest):
        raise ConfigurationError("跨基线恢复段承接的 Campaign／客户端或总计划已经变化。")
    upgrade._reject_contaminated_campaign(campaign_dir)
    if recovery.get("kind") != "attempt-recovery" or recovery.get("attempt_id") != attempt_id:
        raise ConfigurationError("新裁定没有绑定当前原 attempt。")
    revision = reader.require_recovery_revision(segment_root.name)
    expected = reader._segment_root(campaign_dir, candidate_id, attempt_id, revision)
    if segment_root != expected:
        raise ConfigurationError("待承接成功段不在原 attempt 的规范目录。")
    chain = _baseline_chain(campaign_dir, manifest, candidate_id, baseline, baseline_commit_sha256, recovery)
    _segment, reservation, summary = reader.recovery_segment_summary(campaign_dir, candidate_id, attempt_id, revision)
    if (summary is None or summary.get("status") != upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS
            or upgrade._failed_job_ids(summary.get("results"))):
        return False
    old_baseline, old_commit, _old_recovery = reader.authoritative_execute_jobs(campaign_dir, candidate_id, reservation)
    if old_baseline >= baseline or old_baseline not in chain:
        return False
    if chain[old_baseline]["commit_sha256"] != old_commit["commit_sha256"]:
        raise ConfigurationError("成功段预约绑定的 COMMIT 不是当前前序链中的同一份。")
    summary_path = segment_root / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
    found = _sealed_stage_for_segment(campaign_dir, manifest, candidate_id, attempt_id, summary_path, summary, recovery, chain)
    if found is None:
        return False
    # 完整 seal 读侧已重放原计划、执行集合与合并结果；这里再次核段摘要和原生段证据的完整封存边界。
    _segment, _reservation, verified = reader.load_segment(campaign_dir, candidate_id, attempt_id, revision)
    if segment_seal.segment_boundary(campaign_dir, segment_root, verified) is None:
        return False
    return _accounted_segment(campaign_dir, manifest, candidate_id, attempt_id, found[0], found[1], summary_path, verified)
