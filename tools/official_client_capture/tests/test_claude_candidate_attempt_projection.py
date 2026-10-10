"""候选 attempt 归档坐标专项：只给原封存读取器验真的精确字段转根。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_qualification_material_source as material
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_vc5_chain_fixture as fixture
from tools.official_client_capture.tests import test_claude_candidate_draft_projection as draft_tests
from tools.official_client_control.errors import ControlError


class CandidateAttemptProjectionTests(unittest.TestCase):
    # 复用原专项的真实原件读取及通用归档遍历，只有封存 reader 边界用
    # 固定返回值替身；完整 reader 的接线另由原 revision 八场景覆盖。
    retain = draft_tests.CandidateDraftProjectionTests.retain
    project_parent = draft_tests.CandidateDraftProjectionTests.project_parent
    close = draft_tests.CandidateDraftProjectionTests.close

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="attempt-projection-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name) / "data"
        self.campaign = self.data / "evidence/campaigns/campaign-1"
        self.attempt = self.campaign / "candidates/candidate-1/attempts/attempt-1"
        self.evidence = self.attempt / "evidence"
        self.attempt.mkdir(parents=True)
        self.originals, self.parents = {}, {}
        self.snapshots = []

        def probe(relative):
            root = self.evidence / relative
            state = fixture._write_json(root / "state.json", {"state": "fixture"})
            self.snapshots.append(state)
            return fixture._write_json(root / "probe.json", {"schema_version": capture.PROBE_MANIFEST_SCHEMA,
                "snapshots": [{**fixture.binding(state, root), "kind": "client"}]})

        before, after = probe("before"), probe("after")
        gateway_before = fixture._write_json(self.evidence / "gateway-before.json", {"gateway": "before"})
        gateway_after = fixture._write_json(self.evidence / "gateway-after.json", {"gateway": "after"})
        environment = {"before": fixture.binding(gateway_before, self.evidence),
            "after": fixture.binding(gateway_after, self.evidence)}
        recovery = fixture._write_json(self.evidence / "recovery.json", {
            "schema_version": candidate.GATEWAY_RECOVERY_SCHEMA,
            "gateway_before": environment["before"], "gateway_after": environment["after"]})
        activation = fixture._write_json(self.evidence / "activation.json", {"activation": "fixture"})
        environment["activation_fact"] = {**fixture.binding(activation, self.evidence),
            "source_path": "/untrusted-host/never-read.json"}
        catalog = fixture._write_json(self.campaign / "inputs/catalog.json", {"selectors": {}})
        observation = fixture._write_json(self.evidence / "selector.json", {
            "schema_version": candidate.SELECTOR_OBSERVATION_SCHEMA, "source": fixture.binding(catalog, self.campaign)})
        self.runs = [{"before_probe": fixture.binding(before, self.attempt),
            "after_probe": fixture.binding(after, self.attempt)}]
        permission = fixture._write_json(self.evidence / "job/permission.json", {"permission": "fixture"})
        self.closure = fixture._write_json(self.evidence / "job/closure.json", {
            "schema_version": capture.JOB_CLOSURE_SCHEMA, "campaign_id": "campaign-1",
            "attempt_id": "attempt-1", "job_id": "job-1", "execution_sha256": "a" * 64,
            "environment_runs": self.runs,
            "evidence_permission_closeout": {**fixture.binding(permission, self.attempt), "entry_count": 1}})
        reservation = fixture._write_json(self.attempt / "reservation.json", {"reservation": "fixture"})
        heartbeat = fixture._write_json(self.attempt / "heartbeat.json", {"heartbeat": "fixture"})
        self.document = {"schema_version": capture.CAPTURE_ATTEMPT_SCHEMA, "campaign_id": "campaign-1",
            "client": "claude", "mode": "formal", "phase": "candidate", "run_nonce": "fixture-nonce",
            "candidate_id": "candidate-1", "attempt_id": "attempt-1",
            "environment": {"before_probe": fixture.binding(before, self.evidence),
                "after_probe": fixture.binding(after, self.evidence), "restoration_report": fixture.binding(recovery, self.evidence)},
            "gateway_environment": environment, "gateway_recovery": {"file": fixture.binding(recovery, self.evidence)},
            "selector_observed": {**fixture.binding(observation, self.evidence), "schema_version": candidate.SELECTOR_OBSERVATION_SCHEMA},
            "candidate_gateway": {"selector_before_snapshot": {"source_ref": fixture.binding(catalog, self.data)}},
            "reservation": fixture.binding(reservation, self.campaign, with_bytes=False),
            "watchdog": {"heartbeat": fixture.binding(heartbeat, self.campaign), "timeout_checkpoint": None},
            "results": [{"id": "job-1", "execution_sha256": "a" * 64, "environment_runs": self.runs,
                "job_closure": fixture.binding(self.closure, self.attempt)}]}
        self.write_job_materials()
        self.write_attempt()

    def write_job_materials(self, *, checkpoint_changes=None):
        """沿真实追加器形成 Job 与 checkpoint；只在本测试临时世界准备输入。"""
        result = self.document["results"][0]
        self.job_path = fixture._write_json(self.attempt / "job-job-1.json", result)
        store = incremental_recovery.CheckpointStore(self.attempt / "checkpoints")
        for path in store.root.glob("*.json"):
            path.unlink()
        store.append({"checkpoint_schema_version": capture.JOB_CHECKPOINT_SCHEMA,
            **{key: self.document[key] for key in ("client", "mode", "campaign_id", "phase", "attempt_id", "run_nonce")},
            "item_id": "job-1", "status": "complete", "disposition": "executed", "result": result,
            "result_sha256": incremental_recovery.digest(result), "result_key": None,
            "previous_checkpoint_sha256": None, **(checkpoint_changes or {})})
        record = store.records()[0]
        self.checkpoint_path = store.record_path(1)
        self.document["job_checkpoint"] = {"schema_version": capture.JOB_CHECKPOINT_SCHEMA,
            "path": store.root.relative_to(self.campaign).as_posix(), "record_count": 1,
            "last_sequence": 1, "last_sha256": record["checkpoint_sha256"]}

    def write_attempt(self):
        self.attempt_path = fixture._write_json(self.attempt / "attempt.json", self.document)
        self.round_one = {"candidate_id": "candidate-1", "attempt": fixture.binding(self.attempt_path, self.campaign)}

    def prove(self, *, verified=None):
        with mock.patch.object(upgrade, "_capture_stage_attempt_context",
                return_value=(self.attempt, self.document if verified is None else verified)) as reader:
            material._retain_candidate_attempt_materials(self.campaign, self.round_one, self.data,
                retain=self.retain, project_parent=self.project_parent)
            reader.assert_called_once_with(self.campaign, self.round_one, phase="candidate", candidate_id="candidate-1")

    def test_four_roots_and_annotated_attachments_close_without_host_read(self):
        self.prove()
        closed = {row["path"] for row in self.close([fixture.binding(self.attempt_path, self.data)])}
        expected = {path.relative_to(self.data).as_posix() for path in self.data.rglob("*") if path.is_file()}
        self.assertEqual(closed, expected)

    def test_job_and_checkpoint_parents_close_through_their_exact_result_fields(self):
        self.prove()
        closed = {row["path"] for row in self.close([fixture.binding(path, self.data)
            for path in (self.job_path, self.checkpoint_path)])}
        self.assertIn(self.closure.relative_to(self.data).as_posix(), closed)

    def test_job_result_mismatch_is_rejected_before_projection(self):
        fixture._write_json(self.job_path, {**self.document["results"][0], "execution_sha256": "b" * 64})
        with self.assertRaisesRegex(ControlError, "Job 原件.*同一结果"):
            self.prove()

    def test_checkpoint_rehashed_forgery_is_rejected(self):
        self.write_job_materials(checkpoint_changes={"result_sha256": "f" * 64})
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "checkpoint.*同源"):
            self.prove()

    def test_checkpoint_foreign_attempt_is_rejected(self):
        self.write_job_materials(checkpoint_changes={"attempt_id": "another-attempt"})
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "checkpoint.*同源"):
            self.prove()

    def test_job_and_checkpoint_unknown_result_binding_keeps_data_root(self):
        self.document["results"][0]["unknown"] = fixture.binding(self.closure, self.attempt)
        self.write_job_materials()
        self.write_attempt()
        self.prove()
        for path in (self.job_path, self.checkpoint_path):
            with self.subTest(parent=path.name), self.assertRaisesRegex(ControlError, "原件缺失"):
                self.close([fixture.binding(path, self.data)])

    def test_reader_document_mismatch_is_rejected_before_projection(self):
        with self.assertRaisesRegex(ControlError, "不是同一份"):
            self.prove(verified={**self.document, "attempt_id": "other"})
        self.assertFalse(self.parents)

    def test_attempt_bytes_drift_is_rejected(self):
        self.attempt_path.write_bytes(self.attempt_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ConfigurationError, "摘要漂移"):
            self.prove()

    def test_wrong_job_parent_rejected_even_with_updated_file_binding(self):
        value = json.loads(self.closure.read_bytes())
        fixture._write_json(self.closure, {**value, "job_id": "other"})
        self.document["results"][0]["job_closure"] = fixture.binding(self.closure, self.attempt)
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "Job 闭环原件"):
            self.prove()

    def test_same_hash_at_different_selector_coordinate_is_rejected(self):
        source = self.document["candidate_gateway"]["selector_before_snapshot"]["source_ref"]
        self.document["candidate_gateway"]["selector_before_snapshot"]["source_ref"] = {**source, "path": "another/catalog.json"}
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "selector 观察"):
            self.prove()

    def test_missing_or_changed_annotated_snapshot_is_rejected(self):
        self.snapshots[0].write_bytes(b"{}")
        with self.assertRaisesRegex(ControlError, "摘要漂移"):
            self.prove()

    def test_unknown_binding_still_uses_data_root_and_requires_original(self):
        # 同名文件虽在 attempt 根真实存在，未知字段不能继承其转根权限。
        self.document["unknown"] = fixture.binding(self.closure, self.attempt)
        self.write_attempt()
        self.prove()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close([fixture.binding(self.attempt_path, self.data)])

    def test_projection_rejects_changed_parent_bytes_at_consumption(self):
        self.prove()
        binding = fixture.binding(self.attempt_path, self.data)
        with self.assertRaisesRegex(ControlError, "漂移"):
            self.parents[binding["path"]].project(binding, self.attempt_path.read_bytes() + b" ")

    def test_historical_shape_does_not_get_new_coordinate_permissions(self):
        self.document["schema_version"] = "fixture-historical/v1"
        self.write_attempt()
        with mock.patch.object(upgrade, "_capture_stage_attempt_context") as reader:
            material._retain_candidate_attempt_materials(self.campaign, self.round_one, self.data,
                retain=self.retain, project_parent=self.project_parent)
            reader.assert_not_called()
        self.assertFalse(self.parents)


class RecoveryRunIdentityProjectionTests(unittest.TestCase):
    """摘要的原 attempt 身份与段执行身份不同，必须分别核对原件。"""

    retain = CandidateAttemptProjectionTests.retain
    project_parent = CandidateAttemptProjectionTests.project_parent
    close = CandidateAttemptProjectionTests.close
    write_job_materials = CandidateAttemptProjectionTests.write_job_materials
    write_attempt = CandidateAttemptProjectionTests.write_attempt

    def setUp(self):
        CandidateAttemptProjectionTests.setUp(self)
        self.execution_id = self.document["attempt_id"] + ".ar1"
        closure = json.loads(self.closure.read_bytes())
        fixture._write_json(self.closure, {**closure, "attempt_id": self.execution_id})
        self.document["schema_version"] = upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA
        self.document["results"][0]["job_closure"] = fixture.binding(self.closure, self.attempt)
        self.write_job_materials(checkpoint_changes={"attempt_id": self.execution_id})
        self.write_attempt()

    def prove(self, *, producer_id=None):
        return material._retain_candidate_run_materials(self.campaign, self.attempt_path,
            self.document, self.attempt, {"job-1": (self.attempt, producer_id or self.execution_id)},
            self.data, execution_attempt_id=self.execution_id, retain=self.retain,
            project_parent=self.project_parent)

    def test_summary_keeps_original_id_and_checkpoint_uses_execution_id(self):
        self.prove()
        closed = {row["path"] for row in self.close([fixture.binding(self.attempt_path, self.data)])}
        self.assertIn(self.closure.relative_to(self.data).as_posix(), closed)
        self.assertEqual(self.document["attempt_id"], "attempt-1")

    def test_rehashed_checkpoint_with_original_id_is_rejected(self):
        self.write_job_materials()
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "checkpoint.*同源"):
            self.prove()

    def test_original_id_cannot_authorize_segment_closure(self):
        with self.assertRaisesRegex(ControlError, "Job 闭环原件"):
            self.prove(producer_id=self.document["attempt_id"])
