"""Mac 客户端离线闭环的完整来源夹具与拒绝路径；本模块不启动客户端、不联网。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import claude_upgrade_client_receipts as importer
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_segment_seal as segment_seal
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture.tests import claude_client_receipt_fixture as fixture
from tools.official_client_capture.tests import test_claude_upgrade_candidate_seal_evidence as seal_fixture


class ClientReceiptContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.campaign = self.root / "staging" / "data" / "evidence" / "campaigns" / "campaign-1"
        self.attempt = self.campaign / "candidates" / "candidate-1" / "attempts" / "attempt-1"
        self.values = {"campaign_id": "campaign-1", "candidate_id": "candidate-1", "attempt_id": "attempt-1", "run_nonce": "nonce-1"}
        fixture.write(self.attempt / "attempt.json", self.values)
        fixture.write(self.campaign / "campaign.json", {"campaign_id": "campaign-1", "client": "claude"})
        self.task_path, self.bundle, self.capture = fixture.material(self.attempt, "claude-desktop", self.values)
        self.task = contract.read_json(self.task_path)
        source = fixture.source_patch()
        source.start()
        self.addCleanup(source.stop)

    def verify(self) -> dict:
        return contract.verify_client_bundle(self.task, self.bundle, self.capture)

    def rewrite_source(self, name: str, payload: dict) -> None:
        fixture.write(self.bundle / name, payload)
        source = contract.read_json(self.bundle / "source-manifest.json")
        source["files"] = [contract.binding(self.bundle / row["path"], self.bundle) for row in source["files"]]
        fixture.write(self.bundle / "source-manifest.json", source)

    def test_complete_attachment_receipt_and_non_anthropic_candidate_accounting(self) -> None:
        receipt = fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        result = contract.verify_client_receipt(self.attempt, "claude-desktop", receipt)
        self.assertEqual((result["status"], result["accounting_status"], len(result["requests"])), ("verified", "resolved", 1))
        request = result["requests"][0]
        self.assertEqual(request["host"], "candidate.invalid")
        self.assertEqual(request["identity_key"], provenance.identity_key(request["producer_run_id"], provenance.SOURCE_MAC_CANDIDATE, request["native_coordinate"]))

    def test_receipt_rejects_self_consistent_accounting_in_foreign_project(self) -> None:
        project = fixture.fixture_ledger(self.attempt)
        head = ledger.replay_head(project)
        other = self.root / "staging" / "other-project"
        other.mkdir(mode=0o700)
        foreign = ledger.create_fixture_ledger(other, client="claude")
        # 旁账内仍走真实 append/replay，使 task、accounting 与请求键彼此完全自洽；独立读侧必须查真实 Campaign 归属。
        with mock.patch.object(fixture, "fixture_ledger", return_value=foreign):
            receipt = fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        self.assertEqual(len(ledger.replay_head(foreign)["accounted_identity_index"]), 1)
        with self.assertRaisesRegex(ValueError, "不是本 Campaign 的总账"):
            contract.verify_client_receipt(self.attempt, "claude-desktop", receipt)
        self.assertEqual(ledger.replay_head(project)["head_sha256"], head["head_sha256"])

    def test_scenario_reuse_rejects_previous_task_accounted_in_foreign_project(self) -> None:
        other = self.root / "staging" / "other-project"
        other.mkdir(mode=0o700)
        foreign = ledger.create_fixture_ledger(other, client="claude")
        with mock.patch.object(fixture, "fixture_ledger", return_value=foreign):
            fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        reuse = [{"root": str(self.attempt), "task": contract.binding(self.task_path, self.attempt), "source_manifest": contract.binding(self.bundle / "source-manifest.json", self.attempt), "scenario_ids": ["client-smoke"]}]
        task_path, bundle, capture = fixture.material(self.attempt, "claude-desktop", self.values, nonce="reuse-foreign-accounting", scenario_reuse=reuse, issued_at_utc="2026-10-08T00:01:00Z")
        with self.assertRaisesRegex(ValueError, "不是本 Campaign 的总账"):
            contract.verify_client_bundle(contract.read_json(task_path), bundle, capture)

    def test_fixture_reuses_campaign_ledger_without_creating_side_ledger(self) -> None:
        project = ledger.find_project_ledger(self.campaign, client="claude")
        self.assertIsNotNone(project)
        self.assertEqual(fixture.fixture_ledger(self.attempt), project)
        self.assertFalse((self.root / "staging" / "mac-client-fixture").exists())

    def test_fixture_reentry_preserves_published_attempt_bytes_and_extra_bindings(self) -> None:
        from tools.official_client_capture import claude_upgrade, claude_upgrade_capture

        path = self.attempt / "attempt.json"
        published = contract.read_json(path)
        published["schema_version"] = claude_upgrade_capture.CAPTURE_ATTEMPT_SCHEMA
        published["identity"]["build_receipt"]["receipt_digest"] = "c" * 64
        published["attempt_digest"] = claude_upgrade._fingerprint(published)
        fixture.write(path, published)
        before = path.read_bytes()
        metadata = path.stat()
        fixture.material(self.attempt, "claude-desktop", self.values)
        fixture.write_receipt(self.attempt, "claude-vscode", self.values)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (metadata.st_ino, metadata.st_mtime_ns))
        self.assertEqual(contract.read_json(path)["identity"]["build_receipt"]["receipt_digest"], "c" * 64)
        self.assertEqual(contract.read_json(path)["attempt_digest"], published["attempt_digest"])
        with self.assertRaisesRegex(ValueError, "run_nonce"):
            fixture.candidate_inputs(self.attempt, {**self.values, "run_nonce": "wrong-nonce"}, "claude-desktop")
        self.assertEqual(path.read_bytes(), before)

    def test_fixture_refuses_to_repair_incomplete_published_attempt(self) -> None:
        from tools.official_client_capture import claude_upgrade, claude_upgrade_capture

        path = self.attempt / "attempt.json"
        original = contract.read_json(path)
        for missing in ("build_receipt", "profile_digest"):
            with self.subTest(missing=missing):
                published = copy.deepcopy(original)
                published["schema_version"] = claude_upgrade_capture.CAPTURE_ATTEMPT_SCHEMA
                del published["identity"][missing]
                published["attempt_digest"] = claude_upgrade._fingerprint(published)
                fixture.write(path, published)
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "构建绑定|profile_digest"):
                    fixture.candidate_inputs(self.attempt, self.values, "claude-desktop")
                self.assertEqual(path.read_bytes(), before)

    def test_old_success_placeholder_is_rejected(self) -> None:
        receipt = fixture.write(self.attempt / "client-evidence" / "claude-desktop.json", {"client_id": "claude-desktop", **self.values, "status": "success"})
        with self.assertRaisesRegex(ValueError, "拒绝占位成功"):
            contract.verify_client_receipt(self.attempt, "claude-desktop", receipt)

    def test_formal_source_requires_explicit_registered_adapter(self) -> None:
        adapter = contract.SOURCE_ADAPTERS.pop(fixture.RECORDER)
        try:
            with self.assertRaisesRegex(ValueError, "真实来源适配器尚未登记"):
                importer.verify_import_inputs(self.attempt, "claude-desktop", self.task_path, self.bundle)
        finally:
            contract.SOURCE_ADAPTERS[fixture.RECORDER] = adapter

    def test_fixture_adapter_requires_fixture_only_ledger(self) -> None:
        root = fixture.fixture_ledger(self.attempt)
        plan = root / "plan.json"
        raw = plan.read_bytes()
        plan.unlink()
        try:
            with self.assertRaises((ValueError, OSError)):
                contract.verify_origin(self.task, self.bundle, self.capture)
        finally:
            plan.write_bytes(raw)
            plan.chmod(0o600)

    def test_wrong_client_and_wrong_candidate_and_independent_versions(self) -> None:
        wrong = copy.deepcopy(self.task)
        wrong["client_id"] = "claude-vscode"
        wrong["task_sha256"] = contract.digest({k: v for k, v in wrong.items() if k != "task_sha256"})
        with self.assertRaisesRegex(ValueError, "Catalog 客户端"):
            contract.validate_task(wrong)
        identity = contract.read_json(self.bundle / "client-identity.json")
        identity["artifacts"][0]["version"] = self.task["catalog"]["entry"]["version"]
        self.rewrite_source("client-identity.json", identity)
        with self.assertRaisesRegex(ValueError, "制品、版本"):
            self.verify()

    def test_header_only_and_missing_process_or_session_are_rejected(self) -> None:
        process = contract.read_json(self.bundle / "process-session.json")
        process["processes"] = []
        self.rewrite_source("process-session.json", process)
        with self.assertRaisesRegex(ValueError, "CLI headers"):
            self.verify()

    def test_attachment_drift_symlink_and_unlisted_file_are_rejected(self) -> None:
        path = self.bundle / "client-identity.json"
        raw = path.read_bytes()
        path.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ValueError, "摘要或字节数漂移"):
            self.verify()
        path.write_bytes(raw)
        (self.bundle / "extra").mkdir()
        fixture.write(self.bundle / "extra" / "source-manifest.json", {})
        with self.assertRaisesRegex(ValueError, "未登记"):
            self.verify()
        (self.bundle / "extra" / "source-manifest.json").unlink()
        path.unlink()
        path.symlink_to(self.task_path)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.verify()

    def test_server_original_cannot_be_substituted_by_mac_report(self) -> None:
        with self.assertRaisesRegex(ValueError, "任务预约的独立捕获根"):
            contract.verify_client_bundle(self.task, self.bundle, self.bundle)
        capture = contract.read_json(self.capture / "capture-manifest.json")
        capture["candidate"]["image_digest"] = "f" * 64
        fixture.write(self.capture / "capture-manifest.json", capture)
        with self.assertRaisesRegex(ValueError, "本轮任务与候选"):
            self.verify()

    def test_failure_keeps_requests_and_truncation_remains_unresolved(self) -> None:
        task, bundle, capture = fixture.material(self.attempt, "claude-desktop", self.values, status="failed", nonce="failed-task")
        failed = contract.verify_client_bundle(contract.read_json(task), bundle, capture)
        self.assertEqual((failed["status"], failed["accounting_status"], len(failed["requests"])), ("failed", "resolved", 1))
        task, bundle, capture = fixture.material(self.attempt, "claude-desktop", self.values, status="truncated", nonce="truncated-task")
        truncated = contract.verify_client_bundle(contract.read_json(task), bundle, capture)
        self.assertEqual((truncated["status"], truncated["accounting_status"], len(truncated["requests"])), ("failed", "unresolved", 1))

    def test_fixture_import_is_idempotent_but_never_publishes_success(self) -> None:
        task, bundle, _capture = fixture.material(self.attempt, "claude-desktop", self.values, scope="regression_fixture", nonce="historical-task")
        first = importer.import_bundle(self.campaign, self.attempt, "claude-desktop", task, bundle)
        second = importer.import_bundle(self.campaign, self.attempt, "claude-desktop", task, bundle)
        self.assertEqual(first, second)
        self.assertFalse(first["formal_reference_authorized"])
        self.assertFalse((self.attempt / "client-evidence" / "claude-desktop.json").exists())
        discovered = importer.collect_campaign_runs(self.campaign)
        self.assertEqual(len(discovered), 1)
        self.assertTrue(discovered[0]["job_id"].startswith("mac-unimported-"))

    def test_existing_provenance_accounting_deduplicates_import_and_reevaluation(self) -> None:
        verified = self.verify()
        source = {"requests": [{**row, "job_id": "mac-run-1"} for row in verified["requests"]], "jobs": [{"job_id": "mac-run-1", "phase": "candidate", "status": "resolved", "roots": [], "precise_count": 1, "estimated_count": 0}], "unresolved_job_ids": []}
        project = fixture.fixture_ledger(self.attempt)
        head = ledger.replay_head(project)
        accounting = reconciler.request_accounting(source, head=head, initial_keys=set(), campaign_id="campaign-1")
        self.assertEqual(len(accounting["new_keys"]), 1)
        ledger.append_project_event(project, operation_id="fixture-mac-first", event_type="reconciliation_committed", payload={"campaign_id": "campaign-1", "request": {"status": "resolved", "identity_keys": accounting["new_keys"], "estimated_delta": 0}}, source_batch_sha256=None)
        second = reconciler.request_accounting(source, head=ledger.replay_head(project), initial_keys=set(), campaign_id="campaign-1")
        self.assertEqual(second["new_keys"], [])

    def test_sealed_receipt_rechecks_all_attachments_and_accounting(self) -> None:
        receipt = fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        document = contract.read_json(receipt)
        accounting = contract.bound_file(self.attempt, document["accounting_binding"])
        data = contract.read_json(accounting)
        data["batch_sha256"] = "f" * 64
        fixture.write(accounting, data)
        document["accounting_binding"] = contract.binding(accounting, self.attempt)
        fixture.write(receipt, document)
        with self.assertRaisesRegex(ValueError, "入账事件与任务"):
            contract.verify_client_receipt(self.attempt, "claude-desktop", receipt)

    def test_candidate_rebinding_and_ancestor_symlink_are_rejected(self) -> None:
        receipt = fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        attempt = contract.read_json(self.attempt / "attempt.json")
        attempt["identity"]["manifest_digest"] = "sha256:" + "f" * 64
        fixture.write(self.attempt / "attempt.json", attempt)
        with self.assertRaisesRegex(ValueError, "manifest_digest"):
            contract.verify_client_receipt(self.attempt, "claude-desktop", receipt)
        link = self.root / "linked-evidence"
        link.symlink_to(self.attempt)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            contract.read_json(link / "attempt.json")

    def test_reserved_but_unimported_capture_is_unresolved(self) -> None:
        runs = importer.collect_campaign_runs(self.campaign)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "unresolved")
        self.assertEqual(runs[0]["precise_count"], 0)
        self.assertIn("尚未导入", runs[0]["reason"])

    def test_producer_cannot_be_reallocated_to_another_task(self) -> None:
        duplicate = copy.deepcopy(self.task)
        duplicate["task_nonce"] = "other-task"
        duplicate["task_sha256"] = contract.digest({key: value for key, value in duplicate.items() if key != "task_sha256"})
        with self.assertRaisesRegex(ValueError, "重复分配 producer"):
            importer._require_unique_task_producer(self.campaign, duplicate)

    def test_http_retries_are_counted_by_distinct_native_ordinal(self) -> None:
        first = json.loads((self.capture / "requests.jsonl").read_bytes())
        second = {**first, "request_ordinal": 2}
        (self.capture / "requests.jsonl").write_bytes(contract.canonical(first) + b"\n" + contract.canonical(second) + b"\n")
        capture = contract.read_json(self.capture / "capture-manifest.json")
        capture["requests"] = contract.binding(self.capture / "requests.jsonl", self.capture)
        fixture.write(self.capture / "capture-manifest.json", capture)
        rows = self.verify()["requests"]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["identity_key"], rows[1]["identity_key"])

    def test_only_passed_scenario_is_reused_without_resending(self) -> None:
        fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        scenarios = [self.task["scenarios"][0], {"id": "client-followup", "model": "fixture-model", "challenge": "fixture-followup-challenge-5678", "response_marker": "fixture-followup-response", "minimum_requests": 1}]
        reuse = [{"root": str(self.attempt), "task": contract.binding(self.task_path, self.attempt), "source_manifest": contract.binding(self.bundle / "source-manifest.json", self.attempt), "scenario_ids": ["client-smoke"]}]
        task_path, bundle, capture = fixture.material(self.attempt, "claude-desktop", self.values, nonce="followup-task", scenarios=scenarios, scenario_reuse=reuse, issued_at_utc="2026-10-08T00:01:00Z")
        current = contract.read_json(task_path)
        verified = contract.verify_client_bundle(current, bundle, capture)
        self.assertEqual(verified["status"], "verified")
        self.assertEqual([row["scenario_id"] for row in verified["requests"]], ["client-followup"])
        self.assertEqual(verified["scenario_results"][0]["reused_from_task_sha256"], self.task["task_sha256"])
        current["scenarios"][0]["model"] = "changed-model"
        current["task_sha256"] = contract.digest({key: value for key, value in current.items() if key != "task_sha256"})
        # 先拒来源绑定；重新冻结其他条件不能沿用旧源包。
        with self.assertRaisesRegex(ValueError, "来源清单不是本任务"):
            contract.verify_client_bundle(current, bundle, capture)

    def test_segment_inherits_original_client_receipt_without_writing_or_recounting(self) -> None:
        receipt = fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        bindings = {"client_bindings": {"claude-desktop": {"path": receipt.relative_to(self.campaign).as_posix(), "sha256": contract.binding(receipt, self.campaign)["sha256"]}}, "boundary_checks": {}}
        project = fixture.fixture_ledger(self.attempt)
        head = ledger.replay_head(project)
        raw = receipt.read_bytes()
        for _baseline in (1, 2):
            result = segment_seal.inherited_client_files(self.campaign, self.attempt, bindings)
            self.assertEqual(result["client_bindings"]["claude-desktop"], receipt)
        self.assertEqual(receipt.read_bytes(), raw)
        self.assertEqual(ledger.replay_head(project)["head_sha256"], head["head_sha256"])

    def test_segment_rejects_tampered_attachment_with_unchanged_top_receipt(self) -> None:
        receipt = fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        bindings = {"client_bindings": {"claude-desktop": {"path": receipt.relative_to(self.campaign).as_posix(), "sha256": contract.binding(receipt, self.campaign)["sha256"]}}, "boundary_checks": {}}
        (self.bundle / "process-session.json").write_bytes(b"{}\n")
        with self.assertRaisesRegex(ValueError, "摘要或字节数漂移"):
            segment_seal.inherited_client_files(self.campaign, self.attempt, bindings)

    def test_segment_rejects_rebound_placeholder_and_foreign_attempt_receipt(self) -> None:
        receipt = fixture.write_receipt(self.attempt, "claude-desktop", self.values)
        fixture.write(receipt, {"client_id": "claude-desktop", **self.values, "status": "success"})
        bindings = {"client_bindings": {"claude-desktop": {"path": receipt.relative_to(self.campaign).as_posix(), "sha256": contract.binding(receipt, self.campaign)["sha256"]}}, "boundary_checks": {}}
        with self.assertRaisesRegex(ValueError, "拒绝占位成功"):
            segment_seal.inherited_client_files(self.campaign, self.attempt, bindings)
        other = self.attempt.parent / "other-attempt"
        foreign = fixture.write_receipt(other, "claude-desktop", {**self.values, "attempt_id": "other-attempt", "run_nonce": "other-nonce"})
        bindings["client_bindings"]["claude-desktop"] = {"path": foreign.relative_to(self.campaign).as_posix(), "sha256": contract.binding(foreign, self.campaign)["sha256"]}
        with self.assertRaisesRegex(ValueError, "原 attempt 客户端证据目录"):
            segment_seal.inherited_client_files(self.campaign, self.attempt, bindings)


class FormalClientImportTests(seal_fixture._CandidateSealCase):
    """使用既有冻结 Campaign 输入源夹具；导入、outbox、项目总账和收据读侧全部执行真实代码。"""

    def setUp(self) -> None:
        super().setUp()
        self.attempt = self.campaign / "candidates" / seal_fixture.CANDIDATE / "attempts" / seal_fixture.ATTEMPT
        self.values = {"campaign_id": self.manifest["campaign_id"], "candidate_id": seal_fixture.CANDIDATE, "attempt_id": seal_fixture.ATTEMPT, "run_nonce": seal_fixture.RUN_NONCE}
        fixture.write(self.attempt / "attempt.json", {**self.values, "status": "awaiting_receipts", "evidence_roots": []})
        self.closeout_snapshot = {}
        self.task_path, self.bundle, self.capture = fixture.material(self.attempt, "claude-desktop", self.values)
        self.project = fixture.fixture_ledger(self.attempt)

    def importing(self) -> dict:
        with self.seal_patched():
            return importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)

    def test_complete_reimport_reuses_batch_and_receipt_without_new_requests(self) -> None:
        first = self.importing()
        self.assertEqual(first["status"], "imported")
        head = ledger.replay_head(self.project)
        raw = Path(first["receipt"]).read_bytes()
        second = self.importing()
        self.assertEqual(first, second)
        self.assertEqual(Path(second["receipt"]).read_bytes(), raw)
        self.assertEqual(ledger.replay_head(self.project)["head_sha256"], head["head_sha256"])
        self.assertEqual(head["precise_total"], 1)

    def test_resume_after_committed_outbox_before_project_push(self) -> None:
        with mock.patch.object(ledger, "reconcile_project_ledger", side_effect=RuntimeError("模拟 outbox 已提交后中断")):
            with self.assertRaisesRegex(RuntimeError, "已提交后中断"):
                self.importing()
        self.assertFalse((self.attempt / "client-evidence" / "claude-desktop.json").exists())
        restored = self.importing()
        self.assertEqual(restored["status"], "imported")
        self.assertEqual(ledger.replay_head(self.project)["precise_total"], 1)

    def test_resume_after_project_push_before_success_receipt(self) -> None:
        original = importer._write

        def write(path, value):
            if Path(path).name == "accounting.json":
                raise RuntimeError("模拟总账已入账后中断")
            original(path, value)

        with mock.patch.object(importer, "_write", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "已入账后中断"):
                self.importing()
        before = ledger.replay_head(self.project)
        self.assertEqual(before["precise_total"], 1)
        self.assertEqual(self.importing()["status"], "imported")
        self.assertEqual(ledger.replay_head(self.project)["head_sha256"], before["head_sha256"])


if __name__ == "__main__":
    unittest.main()
