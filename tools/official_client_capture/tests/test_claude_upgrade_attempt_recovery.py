"""B4-05b 恢复段协议回归：真实预约／摘要文件、权威基线绑定、污染事实与客户端复用证明分派。

本模块的控制协议夹具只替换 Campaign 加载和基线存储读取；预约、摘要、原 attempt 文件绑定与篡改拒绝均走生产函数。
共享预览校验从本批基点抽取原函数，Codex 的返回值、错误和扫描统计逐字对照。真实补跑与增量封存另在本模块后续场景覆盖。
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_attempt_recovery as recovery
from tools.official_client_capture import claude_upgrade_baseline as baseline_module
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import upgrade_client_adapter as adapters

BASELINE_COMMIT = "bfa0b1923e15d4c68b5fdfc1d721b6d04eaa1d7b"
REPO_ROOT = Path(__file__).resolve().parents[3]


class RecoveryReadProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.campaign = self.root / "data/evidence/campaigns/campaign"
        self.attempt = self.campaign / "candidates/candidate/attempts/attempt"
        self.segment = self.attempt / "recovery/ar1"
        self.segment.mkdir(parents=True)
        self.manifest = {"client": "claude", "campaign_id": "campaign", "campaign_mode": "formal", "campaign_purpose": "validation_only"}
        self.write(self.campaign / "campaign.json", self.manifest)
        self.original_reservation = {"client": "claude", "candidate_id": "candidate", "attempt_id": "attempt", "run_nonce": "a" * 64,
                                     "planned_jobs": [{"id": "job-a", "execution_sha256": "2" * 64}]}
        self.original_attempt = {"client": "claude", "candidate_id": "candidate", "attempt_id": "attempt", "attempt_digest": "b" * 64}
        self.write(self.attempt / "reservation.json", self.original_reservation)
        self.write(self.attempt / "attempt.json", self.original_attempt)
        self.reservation = {
            "schema_version": upgrade.ATTEMPT_RECOVERY_RESERVATION_SCHEMA, "phase": "candidate",
            "campaign_id": "campaign", "campaign_mode": "formal", "campaign_purpose": "validation_only",
            "campaign_manifest_sha256": upgrade.file_sha256(self.campaign / "campaign.json"),
            "candidate_id": "candidate", "candidate_purpose": "validation_only", "attempt_id": "attempt", "recovery_revision": "ar1",
            "run_nonce": "c" * 64, "identity_sha256": "d" * 64, "evaluation_baseline": 1,
            "baseline_commit_sha256": "e" * 64, "recovery_sha256": "f" * 64,
            "original_reservation": {**self.bound(self.attempt / "reservation.json"), "run_nonce": "a" * 64},
            "original_attempt": {**self.bound(self.attempt / "attempt.json"), "attempt_digest": "b" * 64},
            "planned_jobs": [{"id": "job-a", "required": True, "execution_sha256": "1" * 64, "source_execution_sha256": "2" * 64}],
            "reuse_jobs": ["job-b"],
        }
        self.save_reservation()
        self.summary = {
            "schema_version": upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA, "phase": "candidate", "candidate_id": "candidate",
            "attempt_id": "attempt", "recovery_revision": "ar1", "run_nonce": "c" * 64, "evaluation_baseline": 1,
            "baseline_commit_sha256": "e" * 64, "reservation": self.bound(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME),
            "status": "failed", "results": [{"id": "job-a", "status": "failed"}], "completed_at_utc": "2026-10-08T00:00:00Z",
        }
        self.save_summary()
        self.commit = {"commit_sha256": "e" * 64, "recovery_sha256": "f" * 64}
        self.recovery = {"recovery_sha256": "f" * 64, "kind": "attempt-recovery", "attempt_id": "attempt", "execute_jobs": ["job-a"]}
        for patch in (
            mock.patch.object(upgrade, "load_campaign_manifest", return_value=self.manifest),
            mock.patch.object(baseline_module, "read_evaluation_baseline_commit", side_effect=lambda *_args: dict(self.commit)),
            mock.patch.object(upgrade, "_load_evaluation_baseline_recovery", side_effect=lambda *_args: dict(self.recovery)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

    def bound(self, path: Path) -> dict:
        return {"path": path.relative_to(self.campaign).as_posix(), "sha256": upgrade.file_sha256(path)}

    def save_reservation(self) -> None:
        self.reservation.pop("reservation_digest", None)
        self.reservation["reservation_digest"] = upgrade._fingerprint(self.reservation)
        self.write(self.segment / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME, self.reservation)

    def save_summary(self) -> None:
        self.summary.pop("attempt_recovery_digest", None)
        self.summary["attempt_recovery_digest"] = upgrade._fingerprint(self.summary)
        self.write(self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, self.summary)

    def load_reservation(self) -> dict:
        return recovery.load_reservation(self.campaign, self.segment, candidate_id="candidate", attempt_id="attempt", recovery_revision="ar1")

    def test_valid_reservation_and_summary_are_read_only(self) -> None:
        before = {path: path.read_bytes() for path in self.campaign.rglob("*") if path.is_file()}
        self.assertEqual(self.load_reservation(), self.reservation)
        self.assertEqual(recovery.authoritative_execute_jobs(self.campaign, "candidate", self.reservation), (1, self.commit, self.recovery))
        segment, reservation, summary = recovery.recovery_segment_summary(self.campaign, "candidate", "attempt", "ar1")
        self.assertEqual((segment, reservation, summary), (self.segment, self.reservation, self.summary))
        self.assertEqual({path: path.read_bytes() for path in self.campaign.rglob("*") if path.is_file()}, before)

    def test_foreign_identity_rebound_original_and_duplicate_jobs_are_rejected(self) -> None:
        original = copy.deepcopy(self.reservation)
        cases = [
            {"candidate_id": "another"}, {"phase": "official"}, {"recovery_revision": "ar2"},
            {"campaign_manifest_sha256": "0" * 64}, {"planned_jobs": original["planned_jobs"] * 2},
            {"original_attempt": {**original["original_attempt"], "sha256": "0" * 64}},
            {"original_reservation": {**original["original_reservation"], "run_nonce": "0" * 64}},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.reservation = {**copy.deepcopy(original), **change}
                self.save_reservation()
                with self.assertRaises(upgrade.ConfigurationError):
                    self.load_reservation()

    def test_illegal_reuse_partition_is_rejected_even_with_new_self_digest(self) -> None:
        self.reservation.update(execute_job_ids=["job-a"], reuse_job_ids=["job-a"], reuse_proofs={"job-a": {}},
                                reuse_proofs_sha256=upgrade._fingerprint({"job-a": {}}), recovery_review_sha256="9" * 64)
        self.save_reservation()
        with self.assertRaisesRegex(upgrade.ConfigurationError, "执行／复用集合"):
            self.load_reservation()

    def test_summary_missing_is_interruption_but_bad_existing_summary_is_rejected(self) -> None:
        path = self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
        path.unlink()
        self.assertIsNone(recovery.recovery_segment_summary(self.campaign, "candidate", "attempt", "ar1")[2])
        self.summary["run_nonce"] = "0" * 64
        self.save_summary()
        with self.assertRaisesRegex(upgrade.ConfigurationError, "摘要身份"):
            recovery.recovery_segment_summary(self.campaign, "candidate", "attempt", "ar1")
        path.unlink()
        path.symlink_to(self.root / "missing")
        with self.assertRaises(upgrade.ConfigurationError):
            recovery.recovery_segment_summary(self.campaign, "candidate", "attempt", "ar1")

    def test_authoritative_scope_rejects_commit_recovery_or_job_drift(self) -> None:
        cases = (("commit", "commit_sha256", "0" * 64), ("commit", "recovery_sha256", "0" * 64),
                 ("recovery", "recovery_sha256", "0" * 64), ("recovery", "attempt_id", "another"),
                 ("recovery", "kind", "reevaluate"), ("recovery", "execute_jobs", ["job-b"]))
        for target, key, value in cases:
            current = getattr(self, target)
            saved = current[key]
            with self.subTest(target=target, key=key):
                current[key] = value
                with self.assertRaises(upgrade.ConfigurationError):
                    recovery.authoritative_execute_jobs(self.campaign, "candidate", self.reservation)
                current[key] = saved

    def test_contamination_is_bound_to_the_segment_and_summary(self) -> None:
        self.summary["status"] = "environment_contaminated"
        self.save_summary()
        facts = recovery.contamination_facts(self.campaign, self.attempt, candidate_id="candidate")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["record"], "candidate:attempt.ar1:recovery")
        self.assertEqual(facts[0]["recovery_revision"], "ar1")
        self.assertEqual(facts[0]["source"], self.bound(self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME))

    def test_successful_segment_cannot_be_treated_as_reconciled_failure(self) -> None:
        self.summary.update(status="awaiting_receipts", results=[{"id": "job-a", "status": "complete"}])
        self.save_summary()
        with mock.patch.object(supervisor, "verify_attempt_reconciliation_binding") as verify:
            self.assertFalse(recovery.terminal_failed(self.campaign, candidate_id="candidate", attempt_id="attempt", segment_root=self.segment))
            verify.assert_not_called()
        self.summary["status"] = "failed"
        self.save_summary()
        with mock.patch.object(supervisor, "verify_attempt_reconciliation_binding", return_value=None):
            self.assertFalse(recovery.terminal_failed(self.campaign, candidate_id="candidate", attempt_id="attempt", segment_root=self.segment))
        with mock.patch.object(supervisor, "verify_attempt_reconciliation_binding", return_value={"verified": True}):
            self.assertTrue(recovery.terminal_failed(self.campaign, candidate_id="candidate", attempt_id="attempt", segment_root=self.segment))


class SegmentPreviewDispatchTests(unittest.TestCase):
    def baseline(self):
        completed = subprocess.run(["git", "show", f"{BASELINE_COMMIT}:tools/official_client_capture/codex_upgrade_reconciler.py"],
                                   cwd=REPO_ROOT, text=True, capture_output=True, check=True)
        tree = ast.parse(completed.stdout)
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "validate_segment_reuse_preview")
        namespace = dict(vars(reconciler))
        exec(compile(ast.Module(body=[node], type_ignores=[]), "冻结基点的恢复预览校验", "exec"), namespace)
        return namespace["validate_segment_reuse_preview"], namespace

    def test_claude_preview_validation_calls_claude_proofs(self) -> None:
        preview = {"candidate_id": "candidate", "source_attempt_id": "attempt", "recovery_revision": "ar1",
                   "planned_job_ids": ["job-a"], "reuse_job_ids": ["job-a"], "reuse_proofs": {"job-a": {"verified": True}}}
        with mock.patch.object(reconciler, "_adapter", return_value=adapters.adapter_for("claude")), \
                mock.patch.object(upgrade, "segment_reuse_proofs", return_value=preview["reuse_proofs"]) as own, \
                mock.patch.object(reconciler, "segment_reuse_proofs", side_effect=AssertionError("误调 Codex 证明")):
            self.assertEqual(reconciler.validate_segment_reuse_preview(Path("/campaign"), preview), 0)
            own.assert_called_once()

    def test_codex_matches_frozen_function_return_error_and_scan_count(self) -> None:
        old, namespace = self.baseline()
        preview = {"candidate_id": "candidate", "source_attempt_id": "attempt", "recovery_revision": "ar1",
                   "planned_job_ids": ["job-a"], "reuse_job_ids": ["job-a"], "reuse_proofs": {"job-a": {"verified": True}}}
        def proofs(*_args, scan_stats=None, **_kwargs):
            scan_stats["scanned_bytes"] += 31
            return {"job-a": {"verified": True}}
        namespace["segment_reuse_proofs"] = proofs
        def observe(function, value):
            statistics = {"scanned_bytes": 2}
            try:
                outcome = ("返回", function(Path("/campaign"), value, scan_stats=statistics))
            except reconciler.ReconcilerError as error:
                outcome = ("拒绝", str(error))
            return outcome, statistics
        with mock.patch.object(reconciler, "_adapter", return_value=adapters.adapter_for("codex")), \
                mock.patch.object(reconciler, "segment_reuse_proofs", side_effect=proofs):
            for change in ({}, {"reuse_job_ids": [], "reuse_proofs": {}}, {"reuse_proofs": {}}, {"reuse_proofs": {"job-a": {"verified": False}}}):
                value = {**preview, **change}
                with self.subTest(change=change):
                    self.assertEqual(observe(reconciler.validate_segment_reuse_preview, value), observe(old, value))


if __name__ == "__main__":
    unittest.main()
