"""跨基线成功段承接边界：真实基线／计时账本、预约／摘要和实际项目总账／outbox。

只替换最小 Campaign 的加载边界与昂贵的候选 seal／网关来源；承接判据、共享预约器、基线与恢复合同摘要校验、
项目事件和 COMMIT 链均执行产品代码。完整候选 seal 真链另由 claude_second_transient_chain_driver 连续验收。
"""

from __future__ import annotations

import copy
import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_attempt_recovery as reader
from tools.official_client_capture import claude_upgrade_candidate_capture as capture
from tools.official_client_capture import claude_upgrade_candidate_seal as seal
from tools.official_client_capture import claude_upgrade_recovery_history as history
from tools.official_client_capture import claude_upgrade_segment_seal as segment_seal
from tools.official_client_capture import codex_upgrade as codex
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture import upgrade_client_adapter as adapters
from tools.official_client_capture import upgrade_control_plane as control
from tools.official_client_capture.tests.test_claude_upgrade_baseline import ClaudeBaselineCampaign, CANDIDATE, DIGESTS, _at

ATTEMPT = "attempt-original"


class RecoveryHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-recovery-history-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.world = ClaudeBaselineCampaign(self.root)
        self.campaign = self.world.campaign_dir
        self.manifest = dict(self.world.manifest, client="claude", schema_version="official-client-upgrade-campaign/v1",
                             campaign_purpose="validation_only", target_version="2.1.285")
        self.write(self.campaign / "campaign.json", self.manifest)
        self.world.manifest = self.manifest
        self.attempt = self.campaign / "candidates" / CANDIDATE / "attempts" / ATTEMPT
        self.segment = self.attempt / "recovery" / "ar1"
        self.segment.mkdir(parents=True, mode=0o700)
        self.job = upgrade.Job("job-a", "candidate", (), "恢复测试来源", (), ("/roots/job-a-*",), ())
        self.job_sha256 = upgrade._job_execution_sha256(self.job)
        self.identity = {"host_id": "offline-test"}
        self.original_reservation = {"client": "claude", "candidate_id": CANDIDATE, "attempt_id": ATTEMPT,
            "run_nonce": "a" * 64, "planned_jobs": [{"id": "job-a", "execution_sha256": self.job_sha256}]}
        self.original_attempt = {"client": "claude", "candidate_id": CANDIDATE, "attempt_id": ATTEMPT,
                                 "attempt_digest": "b" * 64, "results": [{"id": "job-a", "status": "complete"}]}
        self.write(self.attempt / "reservation.json", self.original_reservation)
        self.write(self.attempt / "attempt.json", self.original_attempt)
        self.commit1, self.recovery1 = self.commit_baseline(1)
        self.reservation = {
            "schema_version": upgrade.ATTEMPT_RECOVERY_RESERVATION_SCHEMA, "phase": "candidate",
            "campaign_id": self.manifest["campaign_id"], "campaign_mode": "formal", "campaign_purpose": "validation_only",
            "campaign_manifest_sha256": upgrade.file_sha256(self.campaign / "campaign.json"),
            "candidate_id": CANDIDATE, "candidate_purpose": "validation_only", "attempt_id": ATTEMPT, "recovery_revision": "ar1",
            "run_nonce": "c" * 64, "identity_sha256": upgrade._fingerprint(self.identity), "evaluation_baseline": 1,
            "baseline_commit_sha256": self.commit1["commit_sha256"], "recovery_sha256": self.recovery1["recovery_sha256"],
            "original_reservation": {**self.binding(self.attempt / "reservation.json"), "run_nonce": "a" * 64},
            "original_attempt": {**self.binding(self.attempt / "attempt.json"), "attempt_digest": "b" * 64},
            "planned_jobs": [{"id": "job-a", "required": True, "execution_sha256": self.job_sha256,
                              "source_execution_sha256": self.job_sha256}], "reuse_jobs": [],
        }
        self.save(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME, self.reservation, "reservation_digest")
        self.summary = {
            "schema_version": upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA, "phase": "candidate", "candidate_id": CANDIDATE,
            "attempt_id": ATTEMPT, "recovery_revision": "ar1", "run_nonce": "c" * 64, "evaluation_baseline": 1,
            "baseline_commit_sha256": self.commit1["commit_sha256"],
            "reservation": self.binding(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME),
            "status": upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS, "identity": self.identity,
            "results": [{"id": "job-a", "status": "complete", "execution_sha256": self.job_sha256}],
        }
        self.summary_path = self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
        self.save(self.summary_path, self.summary, "attempt_recovery_digest")
        self.stage_path = self.campaign / self.commit1["stage_sources"]["capture-candidate"]["target"]
        self.stage = {"status": "complete", "attempt": self.binding(self.attempt / "attempt.json"),
            "recovery": {**self.binding(self.summary_path), "recovery_revision": "ar1", "evaluation_baseline": 1},
            "previous_stage": self.binding(self.world.candidate_result)}
        self.write(self.stage_path, self.stage)
        self.boundary = self.write(self.stage_path.parent / "sealed-evidence-manifest.json", {})
        self.commit2, self.recovery2 = self.commit_baseline(2, previous=self.commit1)
        for patcher in (
            mock.patch.object(upgrade, "load_campaign_manifest", side_effect=lambda path: upgrade._read_json(path / "campaign.json", "最小 Campaign")),
            mock.patch.object(upgrade, "_campaign_contamination_records", return_value=[]),
            mock.patch.object(upgrade, "_validate_existing_campaign_path"),
            mock.patch.object(upgrade, "_active_unsealed_attempts", return_value=[]),
            mock.patch.object(seal, "load_stage_result", side_effect=lambda _c, path, _id, **_k: upgrade._read_json(path, "候选 seal 来源")),
            mock.patch.object(seal, "sealed_attempt_context", side_effect=lambda *_a, **_k: (self.attempt, self.original_attempt)),
            mock.patch.object(capture, "verify_attempt_bindings"),
            mock.patch.object(segment_seal, "segment_boundary", return_value=self.boundary),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.project = self.root / ledger.ledger_dir_name("claude")
        ledger.create_project_ledger(self.project, project_id="history-test", absolute_deadline_utc="2027-01-31T00:00:00Z",
            deadline_approved_by="测试", estimation_policy="none", estimation_policy_approved_by="测试", client="claude", fixture_only=False)
        with ledger.project_lock(self.project):
            ledger.register_campaign(self.project, self.campaign, campaign_id=self.manifest["campaign_id"], campaign_mode="formal",
                target_version=self.manifest["target_version"], deadline_at_utc=None,
                admission_head_sha256=ledger.replay_head(self.project)["head_sha256"])
        self.operation = f"account-sealed-candidate:{CANDIDATE}:{ATTEMPT}:ar1"
        self.payload = {"campaign_id": self.manifest["campaign_id"], "subject_kind": "sealed_candidate_stage",
            "subject_id": f"{CANDIDATE}:{ATTEMPT}:ar1", "phase": "candidate", "candidate_id": CANDIDATE,
            "recovery_revision": "ar1", "stage_result_sha256": upgrade.file_sha256(self.stage_path),
            "attempt_sha256": self.stage["attempt"]["sha256"], "attempt_recovery_sha256": upgrade.file_sha256(self.summary_path),
            "request": {"status": "resolved", "identity_keys": ["offline-request-1"], "estimated_delta": 0}}

    def write(self, path: Path, value: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        path.chmod(0o600)
        return path

    def binding(self, path: Path) -> dict[str, Any]:
        return {"path": path.relative_to(self.campaign).as_posix(), "sha256": upgrade.file_sha256(path)}

    def save(self, path: Path, value: dict, digest_field: str) -> None:
        value.pop(digest_field, None)
        value[digest_field] = artifacts.digest(value) if digest_field in {"commit_sha256", "recovery_sha256"} else upgrade._fingerprint(value)
        self.write(path, value)

    def commit_baseline(self, number: int, *, previous: dict | None = None) -> tuple[dict, dict]:
        recovery = artifacts.build_evaluation_recovery(
            campaign_id=self.manifest["campaign_id"], candidate_id=CANDIDATE, candidate_revision=1, evaluation_baseline=number,
            kind="attempt-recovery", diagnosis={"path": f"diagnoses/b{number}.json", "sha256": "1" * 64},
            failure_source="assertion-failed", reuse_authority="anchored", root_cause_class="transient-environment",
            root_cause_id="rc1-" + "0" * 20, failed_step=f"transient-{number}",
            previous_baseline=0 if previous is None else previous["evaluation_baseline"],
            previous_baseline_commit_sha256=None if previous is None else previous["commit_sha256"],
            execute_rules=["SPEC-HDR-001"], reuse_rules=[], execute_jobs=["job-a"], reuse_jobs=[], attempt_id=ATTEMPT,
            recovery_revision=f"ar{number}", fix_commit=None, deployment_receipt=None, evaluation_epoch=None,
            failed_evaluator_digests=DIGESTS, current_evaluator_digests=DIGESTS, reviewer="测试", approved_at_utc=_at(self.world.minute))
        directory = self.world.baseline_dir(number)
        self.write(directory / "recovery.json", recovery)
        sources = {stage: {"source": "local", "target": f"candidates/{CANDIDATE}/revisions/b{number}/{stage}.json"}
                   for stage in artifacts.EVALUATION_STAGES}
        commit = artifacts.build_evaluation_baseline_commit(campaign_id=self.manifest["campaign_id"], candidate_id=CANDIDATE,
            candidate_revision=1, evaluation_baseline=number, kind="attempt-recovery", recovery_sha256=recovery["recovery_sha256"],
            authorization_sha256="4" * 64, stage_sources=sources, committed_at_utc=_at(self.world.minute + 1))
        self.write(directory / "COMMIT", commit)
        self.world.minute += 2
        timing.append_event(self.world.ledger_dir, event_id=f"history-b{number}", phase="VC-5", event_type="evaluation_baseline",
            candidate_id=CANDIDATE, evaluation_baseline=number, baseline_commit_sha256=commit["commit_sha256"],
            baseline_kind="attempt-recovery", recovery_revision=f"ar{number}", next_action="继续恢复",
            recorded_at_utc=_at(self.world.minute))
        return commit, recovery

    def account(self, *, push: bool = True) -> Path:
        with ledger.campaign_ledger_lock(self.campaign) as campaign_ledger:
            result = ledger.write_batch(campaign_ledger, operation_id=self.operation, event_type="reconciliation_committed",
                payload=self.payload, source={"kind": "sealed_candidate_accounting", "sha256": "a" * 64})
        if push:
            ledger.reconcile_project_ledger(self.project, campaign_dir=self.campaign)
        return Path(result["batch_dir"])

    def admitted(self, *, baseline: int = 2, commit: dict | None = None, recovery: dict | None = None) -> bool:
        with upgrade._campaign_lock(self.campaign):
            return history.sealed_predecessor_segment(self.campaign, manifest=self.manifest, candidate_id=CANDIDATE,
                attempt_id=ATTEMPT, segment_root=self.segment, baseline=baseline,
                baseline_commit_sha256=(commit or self.commit2)["commit_sha256"], recovery=recovery or self.recovery2)

    def snapshot(self) -> dict:
        return {str(path): (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def reserve(self, *, client: str = "claude") -> tuple[Path, dict]:
        with control.adapter_scope(adapters.adapter_for(client)):
            return control._reserve_attempt_recovery(self.campaign, manifest=self.manifest, candidate_id=CANDIDATE,
                attempt_root=self.attempt, original_attempt=self.original_attempt, original_reservation=self.original_reservation,
                recovery=self.recovery2, recovery_revision="ar2", baseline=2, baseline_commit_sha256=self.commit2["commit_sha256"],
                jobs=[self.job], source_jobs={"job-a": self.job}, identity=self.identity, lease=None,
                deadline=incremental_recovery.WallClockDeadline(30))

    def test_sealed_and_accounted_predecessor_is_admitted_without_any_write(self) -> None:
        self.account()
        with upgrade._campaign_lock(self.campaign):
            pass
        before = self.snapshot()
        with mock.patch.object(ledger, "project_lock", side_effect=AssertionError("锁顺序反转")), \
                mock.patch.object(ledger, "replay_head", side_effect=AssertionError("刷新缓存")), \
                mock.patch.object(ledger, "assert_campaign_admitted", side_effect=AssertionError("隐式补账")):
            self.assertTrue(self.admitted())
        self.assertEqual(self.snapshot(), before)

    def test_shared_reservation_checks_history_inside_actual_campaign_lock_and_creates_only_ar2(self) -> None:
        self.account()
        original = history.sealed_predecessor_segment
        def locked(*args, **kwargs):
            with (self.campaign / ".campaign.lock").open() as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args, **kwargs)
        with mock.patch.object(history, "sealed_predecessor_segment", side_effect=locked) as checked:
            segment, reservation = self.reserve()
        checked.assert_called_once()
        self.assertEqual((segment.name, reservation["evaluation_baseline"]), ("ar2", 2))
        self.assertNotEqual(reservation["run_nonce"], self.reservation["run_nonce"])
        self.assertEqual(reservation["original_attempt"]["sha256"], self.reservation["original_attempt"]["sha256"])
        self.assertTrue(reader.load_reservation(self.campaign, segment, candidate_id=CANDIDATE, attempt_id=ATTEMPT, recovery_revision="ar2"))

    def test_current_baseline_success_cannot_reopen(self) -> None:
        self.account()
        with mock.patch.object(history.baselines, "orchestrator_current_baseline", return_value=(1, self.commit1)):
            self.assertFalse(self.admitted(baseline=1, commit=self.commit1, recovery=self.recovery1))

    def test_success_without_accounting_or_without_project_event_is_rejected(self) -> None:
        self.assertFalse(self.admitted())
        self.account(push=False)
        self.assertFalse(self.admitted())
        self.assertFalse((self.attempt / "recovery" / "ar2").exists())

    def test_no_seal_stage_or_no_complete_boundary_is_rejected(self) -> None:
        self.account()
        with mock.patch.object(segment_seal, "segment_boundary", return_value=None), \
                mock.patch.object(upgrade, "_replay_attempt_recovery_evidence_permissions"):
            self.assertFalse(self.admitted())
        path, data = self.stage_path, self.stage_path.read_bytes()
        path.unlink()
        self.assertFalse(self.admitted())
        path.write_bytes(data)

    def test_successful_segment_remains_false_for_original_terminal_failed(self) -> None:
        self.assertFalse(reader.terminal_failed(self.campaign, candidate_id=CANDIDATE, attempt_id=ATTEMPT, segment_root=self.segment))

    def test_stale_current_context_and_foreign_attempt_are_rejected(self) -> None:
        self.account()
        with self.assertRaisesRegex(upgrade.ConfigurationError, "当前评估"):
            self.admitted(baseline=1, commit=self.commit1, recovery=self.recovery1)
        with self.assertRaisesRegex(upgrade.ConfigurationError, "原 attempt"):
            self.admitted(recovery={**self.recovery2, "attempt_id": "another"})

    def test_broken_current_recovery_hash_or_predecessor_commit_binding_is_rejected(self) -> None:
        self.account()
        recovery_path = self.world.baseline_dir(2) / "recovery.json"
        changed = dict(self.recovery2, previous_baseline_commit_sha256="0" * 64)
        self.save(recovery_path, changed, "recovery_sha256")
        with self.assertRaises(upgrade.ConfigurationError):
            self.admitted()
        commit = dict(self.commit2, recovery_sha256=changed["recovery_sha256"])
        self.save(self.world.baseline_dir(2) / "COMMIT", commit, "commit_sha256")
        with mock.patch.object(history.baselines, "orchestrator_current_baseline", return_value=(2, commit)):
            with self.assertRaisesRegex(upgrade.ConfigurationError, "前序 COMMIT"):
                self.admitted(commit=commit, recovery=changed)

    def test_successful_segment_outside_current_baseline_ancestry_is_rejected(self) -> None:
        self.account()
        self.commit2, self.recovery2 = self.commit_baseline(3)
        self.assertFalse(self.admitted(baseline=3))

    def test_contaminated_campaign_is_rejected_before_history_admission(self) -> None:
        self.account()
        with mock.patch.object(upgrade, "_campaign_contamination_records", return_value=[{"source": "pollution"}]), \
                self.assertRaisesRegex(upgrade.ConfigurationError, "污染"):
            self.reserve()
        self.assertFalse((self.attempt / "recovery" / "ar2").exists())

    def test_unknown_or_symlink_segment_entry_is_rejected(self) -> None:
        self.account()
        (self.segment.parent / "junk").write_text("untrusted")
        with self.assertRaisesRegex(upgrade.ConfigurationError, "不可信条目"):
            self.reserve()
        (self.segment.parent / "junk").unlink()
        (self.segment.parent / "ar0").mkdir()
        with self.assertRaises(upgrade.ConfigurationError):
            self.reserve()
        (self.segment.parent / "ar0").rmdir()
        (self.segment.parent / "ar9").symlink_to(self.segment, target_is_directory=True)
        with self.assertRaisesRegex(upgrade.ConfigurationError, "不可信条目"):
            self.reserve()

    def test_original_attempt_plan_or_success_summary_digest_drift_is_rejected(self) -> None:
        self.account()
        self.summary["results"][0]["execution_sha256"] = "0" * 64
        self.save(self.summary_path, self.summary, "attempt_recovery_digest")
        self.stage["recovery"] = {**self.stage["recovery"], **self.binding(self.summary_path)}
        self.write(self.stage_path, self.stage)
        with self.assertRaisesRegex(upgrade.ConfigurationError, "执行摘要"):
            self.admitted()
        self.original_reservation["planned_jobs"][0]["execution_sha256"] = "0" * 64
        self.write(self.attempt / "reservation.json", self.original_reservation)
        with self.assertRaises(upgrade.ConfigurationError):
            self.admitted()

    def test_wrong_accounting_subject_or_seal_digest_is_rejected_even_with_valid_event_chain(self) -> None:
        self.payload["stage_result_sha256"] = "0" * 64
        self.account()
        with self.assertRaisesRegex(upgrade.ConfigurationError, "入账事件"):
            self.admitted()

    def test_committed_batch_without_matching_event_source_or_with_broken_digest_is_rejected(self) -> None:
        batch = self.account(push=False)
        ledger.append_project_event(self.project, operation_id=self.operation, event_type="reconciliation_committed",
                                    payload=self.payload, source_batch_sha256="0" * 64)
        with self.assertRaisesRegex(upgrade.ConfigurationError, "outbox COMMIT"):
            self.admitted()
        entry = batch / "entry-01.json"
        data = json.loads(entry.read_bytes())
        data["payload_fragment"]["stage_result_sha256"] = "0" * 64
        self.write(entry, data)
        with self.assertRaisesRegex(upgrade.ConfigurationError, "无法只读重放"):
            self.admitted()

    def test_corrupt_project_event_cannot_be_trusted(self) -> None:
        self.account()
        event_path = sorted((self.project / "events").glob("*.json"))[-1]
        data = json.loads(event_path.read_bytes())
        data["payload"]["attempt_recovery_sha256"] = "0" * 64
        self.write(event_path, data)
        with self.assertRaisesRegex(upgrade.ConfigurationError, "无法只读重放"):
            self.admitted()

    def test_historical_accounting_correction_must_keep_seal_binding(self) -> None:
        self.account()
        corrected = copy.deepcopy(self.payload)
        corrected["attempt_recovery_sha256"] = "0" * 64
        ledger.record_historical_reconciliation_correction(self.project, original_operation_id=self.operation,
            corrected_payload=corrected, reason="离线夹具：测试更正后的有效账务绑定")
        with self.assertRaisesRegex(upgrade.ConfigurationError, "入账事件"):
            self.admitted()

    def test_accounting_only_correction_with_intact_seal_can_be_replayed(self) -> None:
        self.account()
        corrected = copy.deepcopy(self.payload)
        corrected["request"]["identity_keys"] = ["corrected-offline-request"]
        ledger.record_historical_reconciliation_correction(self.project, original_operation_id=self.operation,
            corrected_payload=corrected, reason="离线夹具：仅更正请求计量")
        self.assertTrue(self.admitted())

    def test_shared_codex_reservation_still_rejects_successful_segment(self) -> None:
        self.account()
        with mock.patch.object(codex, "_campaign_lock", side_effect=upgrade._campaign_lock), \
                mock.patch.object(codex, "_reject_contaminated_campaign"), \
                mock.patch.object(codex, "_attempt_recovery_segment_terminal_failed", return_value=False), \
                self.assertRaisesRegex(codex.ConfigurationError, "未收口或未对账"):
            self.reserve(client="codex")
        self.assertFalse((self.attempt / "recovery" / "ar2").exists())

    def test_failed_terminal_bypasses_extra_hook_and_codex_does_not_gain_success_admission(self) -> None:
        with mock.patch.object(upgrade, "_attempt_recovery_segment_terminal_failed", return_value=True), \
                mock.patch.object(history, "sealed_predecessor_segment", side_effect=AssertionError("失败判据已通过")):
            self.reserve()
        # 直接验证 Codex 的新成员恒为假；共享预约器中的旧失败判据保持原短路顺序。
        self.assertFalse(adapters.adapter_for("codex").sealed_predecessor_segment(self.campaign, manifest=self.manifest,
            candidate_id=CANDIDATE, attempt_id=ATTEMPT, segment_root=self.segment, baseline=2,
            baseline_commit_sha256=self.commit2["commit_sha256"], recovery=self.recovery2))


if __name__ == "__main__":
    unittest.main()
