"""候选 attempt 归档坐标专项：只给原封存读取器验真的精确字段转根。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_qualification_material_source as material
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate
from tools.official_client_capture import claude_upgrade_recovery as recovery
from tools.official_client_capture import claude_upgrade_attempt_recovery as segment_reader
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture import claude_upgrade_segment_seal as segment_seal
from tools.official_client_capture import codex_upgrade_evidence_manifest as boundary
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


class NormalResumeProducerProjectionTests(unittest.TestCase):
    """用不同字节的原生产者探针区分同相对路径，防止错根碰巧命中。"""

    retain = CandidateAttemptProjectionTests.retain
    project_parent = CandidateAttemptProjectionTests.project_parent
    close = CandidateAttemptProjectionTests.close
    write_job_materials = CandidateAttemptProjectionTests.write_job_materials
    write_attempt = CandidateAttemptProjectionTests.write_attempt

    def setUp(self):
        CandidateAttemptProjectionTests.setUp(self)
        self.origin = self.attempt.with_name("producer-1")
        shutil.copytree(self.attempt, self.origin)
        original_closure = self.origin / self.closure.relative_to(self.attempt)
        runs = []
        for name in ("before", "after"):
            probe_root = self.origin / "evidence" / name
            snapshot = fixture._write_json(probe_root / "state.json", {"state": "original-producer"})
            probe = fixture._write_json(probe_root / "probe.json", {"schema_version": capture.PROBE_MANIFEST_SCHEMA,
                "snapshots": [{**fixture.binding(snapshot, probe_root), "kind": "client"}]})
            runs.append(probe)
        self.origin_runs = [{"before_probe": fixture.binding(runs[0], self.origin),
            "after_probe": fixture.binding(runs[1], self.origin)}]
        closure = json.loads(original_closure.read_bytes())
        fixture._write_json(original_closure, {**closure, "attempt_id": "producer-1",
            "environment_runs": self.origin_runs})
        self.producer = {"attempt_id": "producer-1",
            "job_closure": fixture.binding(original_closure, self.campaign, with_bytes=False)}
        self.document["results"][0].update(disposition="reused", job_closure=None,
            environment_runs=self.origin_runs, reused_from={"producer": self.producer})
        self.write_job_materials(checkpoint_changes={"disposition": "reused"})
        self.write_attempt()

    def prove(self):
        with mock.patch.object(upgrade, "_capture_stage_attempt_context", return_value=(self.attempt, self.document)), \
                mock.patch.object(upgrade, "load_campaign_manifest", return_value={}), \
                mock.patch.object(capture, "_capture_layout", return_value=object()), \
                mock.patch.object(recovery, "_verify_reusable_evidence") as reader:
            material._retain_candidate_attempt_materials(self.campaign, self.round_one, self.data,
                retain=self.retain, project_parent=self.project_parent)
            reader.assert_called_once()

    def test_normal_resume_retains_original_closure_and_probe_bytes(self):
        self.prove()
        closed = {row["path"] for row in self.close([fixture.binding(self.attempt_path, self.data)])}
        for binding in self.origin_runs[0].values():
            self.assertIn((self.origin / binding["path"]).relative_to(self.data).as_posix(), closed)
        self.assertIn((self.campaign / self.producer["job_closure"]["path"]).relative_to(self.data).as_posix(), closed)

    def test_same_relative_path_in_current_attempt_cannot_replace_producer(self):
        self.prove()
        (self.origin / self.origin_runs[0]["before_probe"]["path"]).unlink()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close([fixture.binding(self.attempt_path, self.data)])

    def test_environment_from_other_producer_is_rejected(self):
        self.document["results"][0]["environment_runs"] = self.runs
        self.write_job_materials()
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "原复用读者.*不同源"):
            self.prove()

    def test_producer_outside_qualification_data_root_is_rejected_before_read(self):
        self.producer["campaign"] = {"campaign_dir": str(self.data.parent / "outside"),
            "campaign_id": "other", "candidate_id": "other", "campaign_manifest_sha256": "a" * 64}
        self.document["results"][0]["reused_from"]["producer"] = self.producer
        self.write_job_materials()
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "越过.*已有数据根"):
            self.prove()

    def test_producer_lexical_parent_escape_is_rejected_before_original_reader(self):
        self.producer["campaign"] = {"campaign_dir": str(self.data / "../outside"),
            "campaign_id": "other", "candidate_id": "other", "campaign_manifest_sha256": "a" * 64}
        self.document["results"][0]["reused_from"]["producer"] = self.producer
        self.write_job_materials()
        self.write_attempt()
        with mock.patch.object(recovery, "_verify_reusable_evidence") as original_reader:
            with self.assertRaisesRegex(ControlError, "越过.*已有数据根"):
                material._candidate_attempt_result_producers(self.campaign, self.document, self.attempt, self.data,
                    retain=self.retain, project_parent=self.project_parent)
            original_reader.assert_not_called()

    def test_producer_symlink_escape_is_rejected_before_original_reader(self):
        outside = self.data.parent / "outside-campaign"
        outside.mkdir()
        link = self.data / "linked-campaign"
        link.symlink_to(outside, target_is_directory=True)
        self.producer["campaign"] = {"campaign_dir": str(link),
            "campaign_id": "other", "candidate_id": "other", "campaign_manifest_sha256": "a" * 64}
        self.document["results"][0]["reused_from"]["producer"] = self.producer
        self.write_job_materials()
        self.write_attempt()
        with mock.patch.object(recovery, "_verify_reusable_evidence") as original_reader:
            with self.assertRaisesRegex(ConfigurationError, "符号链接"):
                material._candidate_attempt_result_producers(self.campaign, self.document, self.attempt, self.data,
                    retain=self.retain, project_parent=self.project_parent)
            original_reader.assert_not_called()

    def test_cover_outside_data_root_is_rejected_before_original_reader(self):
        self.producer["predecessor_sealed_cover"] = {
            "campaign": {"campaign_dir": str(self.data / "../outside-cover")}}
        with mock.patch.object(recovery, "_verify_reusable_evidence") as original_reader:
            with self.assertRaisesRegex(ControlError, "封存覆盖越过.*已有数据根"):
                material._candidate_attempt_result_producers(self.campaign, self.document, self.attempt, self.data,
                    retain=self.retain, project_parent=self.project_parent)
            original_reader.assert_not_called()

    def test_multiple_campaign_inheritance_keeps_the_first_producer(self):
        origin_campaign = self.data / "evidence/campaigns/original-campaign"
        origin_root = origin_campaign / "candidates/original-candidate/attempts/producer-1"
        shutil.copytree(self.origin, origin_root)
        closure_path = origin_root / self.closure.relative_to(self.attempt)
        closure = json.loads(closure_path.read_bytes())
        fixture._write_json(closure_path, {**closure, "campaign_id": "original-campaign"})
        manifest_path = fixture._write_json(origin_campaign / "campaign.json", {"campaign_id": "original-campaign"})
        self.producer.update(campaign={"campaign_dir": str(origin_campaign), "campaign_id": "original-campaign",
            "candidate_id": "original-candidate", "campaign_manifest_sha256": fixture.binding(manifest_path, self.data)["sha256"]},
            execution_sha256="a" * 64, job_closure=fixture.binding(closure_path, origin_campaign, with_bytes=False))
        self.document["results"][0]["reused_from"]["campaign"] = {"campaign_dir": str(self.campaign.with_name("middle-campaign"))}
        with mock.patch.object(upgrade, "load_campaign_manifest", return_value={}), \
                mock.patch.object(capture, "_capture_layout", return_value=object()), \
                mock.patch.object(recovery, "_verify_reusable_evidence") as original_reader:
            producers = material._candidate_attempt_result_producers(self.campaign, self.document, self.attempt,
                self.data, retain=self.retain, project_parent=self.project_parent)
            original_reader.assert_called_once()
        self.assertEqual(producers["job-1"], (origin_root, "producer-1"))
        self.assertIn(manifest_path.relative_to(self.data).as_posix(), self.originals)

    def test_original_cover_stage_does_not_grant_campaign_root_to_unknown_result(self):
        manifest = fixture._write_json(self.campaign / "campaign.json", {"campaign_id": "campaign-1"})
        origin = {"campaign_dir": str(self.campaign), "campaign_id": "campaign-1",
            "candidate_id": "candidate-1", "campaign_manifest_sha256": fixture.binding(manifest, self.data)["sha256"]}
        sealed = fixture._write_json(self.campaign / "source-sealed-manifest.json", {})
        attachment = fixture._write_json(self.campaign / "source-client-checkpoint.json", {})
        unknown = fixture._write_json(self.campaign / "unknown-cover-result.json", {"source": "unregistered"})
        stage = fixture._write_json(self.campaign / "source-cover-stage.json", {
            "attempt": fixture.binding(self.origin / "attempt.json", self.campaign, with_bytes=False),
            "seal": {"client_checkpoint": fixture.binding(attachment, self.campaign)},
            "results": [{"id": "job-1", "unknown": fixture.binding(unknown, self.campaign)}]})
        self.producer.update(campaign=origin, execution_sha256="a" * 64, predecessor_sealed_cover={
            "campaign": origin, "stage_result": fixture.binding(stage, self.campaign, with_bytes=False),
            "sealed_evidence_manifest": fixture.binding(sealed, self.campaign, with_bytes=False)})
        self.write_job_materials(checkpoint_changes={"disposition": "reused"})
        self.write_attempt()
        self.prove()
        self.assertIn((self.origin / "attempt.json").relative_to(self.data).as_posix(), self.originals)
        projection = self.parents[stage.relative_to(self.data).as_posix()]
        bindings = projection.project(fixture.binding(stage, self.data), stage.read_bytes())
        self.assertIn(attachment.relative_to(self.data).as_posix(), {row["path"] for row in bindings})
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close([fixture.binding(stage, self.data)])

    def test_cover_first_attempt_cannot_use_the_direct_source_coordinate(self):
        manifest = fixture._write_json(self.campaign / "campaign.json", {"campaign_id": "campaign-1"})
        origin = {"campaign_dir": str(self.campaign), "campaign_id": "campaign-1",
            "candidate_id": "candidate-1", "campaign_manifest_sha256": fixture.binding(manifest, self.data)["sha256"]}
        sealed = fixture._write_json(self.campaign / "source-sealed-manifest.json", {})
        direct_attempt = fixture._write_json(self.attempt.with_name("middle-source") / "attempt.json",
            {"source": "direct-source-only"})
        stage = fixture._write_json(self.campaign / "source-cover-stage.json", {
            "attempt": fixture.binding(direct_attempt, self.campaign, with_bytes=False)})
        self.producer.update(campaign=origin, execution_sha256="a" * 64, predecessor_sealed_cover={
            "campaign": origin, "stage_result": fixture.binding(stage, self.campaign, with_bytes=False),
            "sealed_evidence_manifest": fixture.binding(sealed, self.campaign, with_bytes=False)})
        self.write_job_materials(checkpoint_changes={"disposition": "reused"})
        self.write_attempt()
        with self.assertRaisesRegex(ControlError, "最初生产者的精确 attempt"):
            self.prove()


class CandidateBoundaryRetentionTests(unittest.TestCase):
    """用实际封存与重绑生产器验证归档原件，不替代原证据边界验证。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="boundary-retention-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.campaign = Path(temporary.name)
        self.evidence = self.campaign / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.source = fixture._write_json(self.evidence / "source.json", {"source": "fixture-only"})
        self.manifest = boundary.build_evidence_manifest([self.evidence],
            checkpoint_path=self.campaign / "scan-checkpoint.json")
        self.path = fixture._write_json(self.campaign / "sealed-evidence-manifest.json", self.manifest)
        self.originals = {}

    def retain(self, path, expected_sha256=None):
        raw = path.read_bytes()
        if expected_sha256 is not None and fixture.binding(path, self.campaign)["sha256"] != expected_sha256:
            raise ControlError("归档原件摘要漂移")
        self.originals[path] = raw
        return raw

    def prove(self):
        material._retain_candidate_sealed_boundary(self.campaign,
            fixture.binding(self.path, self.campaign, with_bytes=False), retain=self.retain)

    def rebind(self):
        replacement = self.evidence / ".replacement"
        replacement.write_bytes(self.source.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, self.source)
        receipt = boundary.build_boundary_rebind(self.manifest, self.path, [self.evidence],
            existing_rebinds=(), campaign_id="campaign-1", phase="candidate",
            candidate_id="candidate-1", attempt_id="attempt-1.ar2", operator="fixture-only")
        return fixture._write_json(self.path.parent / "evidence-manifest-rebind-01.json", receipt)

    def test_original_manifest_is_retained_by_two_field_binding(self):
        self.prove()
        self.assertEqual(self.originals[self.path], self.path.read_bytes())

    def test_actual_metadata_rebind_original_is_retained(self):
        receipt = self.rebind()
        self.prove()
        self.assertEqual(self.originals[receipt], receipt.read_bytes())

    def test_corrupt_rebind_is_rejected_instead_of_omitted(self):
        receipt = self.rebind()
        value = json.loads(receipt.read_bytes())
        fixture._write_json(receipt, {**value, "receipt_sha256": "0" * 64})
        with self.assertRaisesRegex(boundary.EvidenceManifestError, "自摘要不一致"):
            self.prove()


class CandidateRecoveryStageProjectionTests(unittest.TestCase):
    """真实文件、checkpoint 与阶段读取器专项；运行／封存边界用明确替身。

    这里不形成正式采集资格。替身只隔离账号和运行权限，规范轮次的闭集
    读取、精确文件摘要、归档递归与 checkpoint 追加仍消费实际原件。
    """

    retain = CandidateAttemptProjectionTests.retain
    project_parent = CandidateAttemptProjectionTests.project_parent
    close = CandidateAttemptProjectionTests.close
    write_job_materials = CandidateAttemptProjectionTests.write_job_materials
    write_attempt = CandidateAttemptProjectionTests.write_attempt

    def setUp(self):
        CandidateAttemptProjectionTests.setUp(self)
        self.manifest = {"campaign_id": "campaign-1"}
        self.segments = {}
        evaluation = fixture._write_json(self.campaign / "candidate-evaluation.json", {})
        sealed = fixture._write_json(self.campaign / "sealed-evidence-manifest.json", {})
        self.first = {
            "schema_version": candidate_seal.STAGE_RESULT_SCHEMA, "client": "claude",
            "campaign_id": "campaign-1", "stage": candidate_seal.STAGE_NAME,
            "candidate_id": "candidate-1", "status": "complete",
            "attempt": fixture.binding(self.attempt_path, self.campaign, with_bytes=False),
            "seal": {}, "store": {}, "client_evidence": {},
            "sealed_evidence_manifest": fixture.binding(sealed, self.campaign, with_bytes=False),
            "assertion_context": {"evaluation_manifest": fixture.binding(evaluation, self.campaign, with_bytes=False)},
            "execute_item_ids": ["job-1"], "reuse_item_ids": [], "live_request_count": 0,
            "scanned_bytes": 0, "sealed_at_utc": "2026-10-10T00:00:00Z",
        }
        self.first_path = fixture._write_json(self.campaign / "candidates/candidate-1/result.json", self.first)
        baseline = upgrade._evaluation_baseline_dir(self.campaign, "candidate-1", 1)
        self.commit, self.recovery = {"commit": "fixture-only"}, {"recovery": "fixture-only"}
        fixture._write_json(baseline / upgrade.EVALUATION_BASELINE_COMMIT_FILENAME, self.commit)
        fixture._write_json(baseline / upgrade.EVALUATION_BASELINE_RECOVERY_FILENAME, self.recovery)

    def make_segment(self, revision, *, result=None, status=None):
        root = self.attempt / upgrade.ATTEMPT_RECOVERY_DIRNAME / revision
        staging = self.data / "segment-staging"
        shutil.copytree(self.attempt, staging, ignore=shutil.ignore_patterns(upgrade.ATTEMPT_RECOVERY_DIRNAME))
        root.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(root)
        document = copy.deepcopy(self.document)
        document.update(schema_version=upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA,
            identity={"source": "fixture-only"}, status=status or upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS,
            recovery_revision=revision)
        if result is None:
            closure_path = root / self.closure.relative_to(self.attempt)
            closure = json.loads(closure_path.read_bytes())
            fixture._write_json(closure_path, {**closure, "attempt_id": "attempt-1." + revision})
            document["results"][0]["job_closure"] = fixture.binding(closure_path, root)
        else:
            document["results"] = [copy.deepcopy(result)]
        store = incremental_recovery.CheckpointStore(root / "checkpoints")
        for path in store.root.glob("*.json"):
            path.unlink()
        row = document["results"][0]
        fixture._write_json(root / "job-job-1.json", row)
        store.append({"checkpoint_schema_version": capture.JOB_CHECKPOINT_SCHEMA,
            **{key: document[key] for key in ("client", "mode", "campaign_id", "phase", "run_nonce")},
            "attempt_id": "attempt-1." + revision, "item_id": "job-1", "status": "complete",
            "disposition": row.get("disposition", "executed"), "result": row,
            "result_sha256": incremental_recovery.digest(row), "previous_checkpoint_sha256": None})
        record = store.records()[0]
        document["job_checkpoint"] = {"schema_version": capture.JOB_CHECKPOINT_SCHEMA,
            "path": store.root.relative_to(self.campaign).as_posix(), "record_count": 1,
            "last_sequence": 1, "last_sha256": record["checkpoint_sha256"]}
        reservation = {"planned_jobs": [{"id": "job-1", "execution_sha256": row["execution_sha256"]}],
            "identity_sha256": upgrade._fingerprint(document["identity"])}
        self.segments[revision] = (root, reservation, document)
        self.write_segment(revision)
        return root, reservation, document

    def write_segment(self, revision):
        root, reservation, document = self.segments[revision]
        reservation_path = fixture._write_json(root / upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME, reservation)
        document["reservation"] = fixture.binding(reservation_path, self.campaign, with_bytes=False)
        return fixture._write_json(root / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, document)

    def make_stage(self, revision):
        _root, _reservation, summary = self.segments[revision]
        stage = copy.deepcopy(self.first)
        stage.update(recovery={**fixture.binding(self.write_segment(revision), self.campaign, with_bytes=False),
                "recovery_revision": revision}, previous_stage=fixture.binding(self.first_path, self.campaign, with_bytes=False),
            results=copy.deepcopy(summary["results"]), root_sets={})
        effective_path = fixture._write_json(self.campaign / "effective-results.json", {"results": stage["results"]})
        stage["effective_results"] = fixture.binding(effective_path, self.campaign, with_bytes=False)
        stage_path = fixture._write_json(self.campaign / "recovery-result.json", stage)
        return stage_path, effective_path

    def prove(self, path):
        with mock.patch.object(upgrade, "_capture_stage_attempt_context", return_value=(self.attempt, self.document)), \
                mock.patch.object(segment_reader, "load_segment", side_effect=lambda _c, _cid, _aid, revision: self.segments[revision]), \
                mock.patch.object(segment_reader, "recovery_segment_summary", side_effect=lambda _c, _cid, _aid, revision: self.segments[revision]), \
                mock.patch.object(segment_reader, "authoritative_execute_jobs", return_value=(1, self.commit, self.recovery)), \
                mock.patch.object(segment_reader, "segment_reuse_proofs", side_effect=AssertionError("不得重算历史运行期资格")), \
                mock.patch.object(segment_seal, "segment_boundary", return_value=None), \
                mock.patch.object(segment_seal, "validate_stage", side_effect=lambda _c, stage, _cid:
                    (self.attempt, {**self.document, "results": stage["results"]})):
            material._retain_candidate_recovery_materials(self.campaign, self.manifest,
                fixture.binding(path, self.campaign, with_bytes=False), self.data, candidate_id="candidate-1",
                retain=self.retain, project_parent=self.project_parent)

    def make_inherited_stage(self):
        root, _reservation, summary = self.make_segment("ar1", status="failed")
        source = root / "job-job-1.json"
        closure = json.loads((root / summary["results"][0]["job_closure"]["path"]).read_bytes())
        proof = {"recovered_from": "ar1", "source_execution_sha256": "a" * 64,
            "result": fixture.binding(source, self.campaign),
            "checkpoint": fixture.binding(root / "checkpoints/00000001.json", self.campaign),
            "permission_closeout": fixture.binding(root / closure["evidence_permission_closeout"]["path"], self.campaign),
            "after_probe": fixture.binding(root / "evidence" / summary["environment"]["after_probe"]["path"], self.campaign),
            "restoration_report": fixture.binding(root / "evidence" / summary["environment"]["restoration_report"]["path"], self.campaign)}
        result = segment_reader.reused_job_result(self.campaign, proof)
        _root, reservation, _summary = self.make_segment("ar2", result=result)
        reservation.update(reuse_job_ids=["job-1"], reuse_proofs={"job-1": proof})
        return self.make_stage("ar2")[0]

    def test_recovery_stage_and_effective_results_keep_unknown_binding_at_data_root(self):
        foreign = fixture._write_json(self.campaign / "unknown-result.json", {"source": "not-a-result-contract"})
        root, _reservation, summary = self.make_segment("ar1")
        summary["results"][0]["unknown"] = fixture.binding(foreign, self.campaign)
        # 保持实际 Job/checkpoint 与结果同源，避免由另一处不一致抢先拒绝。
        fixture._write_json(root / "job-job-1.json", summary["results"][0])
        checkpoint = incremental_recovery.CheckpointStore(root / "checkpoints")
        record = checkpoint.records()[0]
        record = {key: value for key, value in record.items() if key not in {"checkpoint_sequence", "checkpoint_sha256"}}
        record.update(result=summary["results"][0], result_sha256=incremental_recovery.digest(summary["results"][0]))
        checkpoint.record_path(1).unlink()
        written = checkpoint.append(record)
        summary["job_checkpoint"]["last_sha256"] = written["checkpoint_sha256"]
        stage_path, effective_path = self.make_stage("ar1")
        self.prove(stage_path)
        for path in (stage_path, effective_path):
            with self.subTest(parent=path.name), self.assertRaisesRegex(ControlError, "原件缺失"):
                self.close([fixture.binding(path, self.data)])

    def test_known_stage_seal_binding_uses_campaign_and_unknown_result_does_not(self):
        attachment = fixture._write_json(self.campaign / "seal-client-checkpoint.json", {})
        self.first["seal"]["client_checkpoint"] = fixture.binding(attachment, self.campaign)
        fixture._write_json(self.first_path, self.first)
        self.prove(self.first_path)
        closed = {row["path"] for row in self.close([fixture.binding(self.first_path, self.data)])}
        self.assertIn(attachment.relative_to(self.data).as_posix(), closed)

    def test_retry_round_rejects_string_boolean_zero_and_negative_before_reader(self):
        for index, value in enumerate(("2", True, False, 0, -1)):
            with self.subTest(validation_round=value):
                path = fixture._write_json(self.campaign / f"invalid-round-{index}.json",
                    {**self.first, "validation_round": value})
                with mock.patch.object(candidate_seal, "_load_round_document") as reader:
                    with self.assertRaisesRegex(ControlError, "非布尔正整数"):
                        self.prove(path)
                    reader.assert_not_called()

    def test_exact_retry_rejects_different_first_attempt(self):
        other = fixture._write_json(self.attempt / "other-attempt.json", self.document)
        retry = {**self.first, "attempt": fixture.binding(other, self.campaign, with_bytes=False),
            "validation_round": 2, "round_one_result": fixture.binding(self.first_path, self.campaign, with_bytes=False)}
        path = fixture._write_json(candidate_seal._round_result_path(self.campaign, "candidate-1", 2,
            round_one_path=self.first_path), retry)
        with self.assertRaisesRegex(ControlError, "逐字承接.*首轮"):
            self.prove(path)

    def test_exact_retry_rejects_same_bytes_at_noncanonical_path(self):
        retry = {**self.first, "validation_round": 2,
            "round_one_result": fixture.binding(self.first_path, self.campaign, with_bytes=False)}
        path = fixture._write_json(self.campaign / "same-retry-at-wrong-coordinate.json", retry)
        with self.assertRaisesRegex(ControlError, "逐字承接.*首轮"):
            self.prove(path)

    def test_exact_retry_preserves_the_bound_first_round(self):
        retry = {**self.first, "validation_round": 2,
            "round_one_result": fixture.binding(self.first_path, self.campaign, with_bytes=False)}
        path = fixture._write_json(candidate_seal._round_result_path(self.campaign, "candidate-1", 2,
            round_one_path=self.first_path), retry)
        self.prove(path)
        self.assertIn(self.first_path.relative_to(self.data).as_posix(), self.originals)

    def test_inherited_frozen_proof_does_not_recompute_runtime_permissions(self):
        path = self.make_inherited_stage()
        self.prove(path)
        self.assertIn((self.segments["ar1"][0] / "job-job-1.json").relative_to(self.data).as_posix(), self.originals)

    def test_historical_summary_structure_rejects_rehashed_identity_and_result_forgery(self):
        for failure in ("identity", "execution", "duplicate", "unplanned", "unplanned-missing-execution", "complete-missing"):
            with self.subTest(failure=failure):
                # 每个负例复用已冻结 proof 的精确字节，只破坏历史控制摘要。
                path = self.make_inherited_stage() if not self.segments else self.campaign / "recovery-result.json"
                _root, reservation, summary = self.segments["ar1"]
                original = copy.deepcopy(summary)
                original_reservation = copy.deepcopy(reservation)
                if failure == "identity":
                    summary["identity"] = {"source": "forged"}
                elif failure == "execution":
                    summary["results"][0]["execution_sha256"] = "f" * 64
                elif failure == "duplicate":
                    summary["results"].append(copy.deepcopy(summary["results"][0]))
                elif failure == "unplanned":
                    summary["results"][0]["id"] = "outside-plan"
                elif failure == "unplanned-missing-execution":
                    summary["results"][0]["id"] = "outside-plan"
                    summary["results"][0].pop("execution_sha256")
                else:
                    summary["status"] = upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS
                    reservation["planned_jobs"].append({"id": "missing-job", "execution_sha256": "a" * 64})
                summary["attempt_recovery_digest"] = upgrade._fingerprint({key: value for key, value in summary.items()
                    if key != "attempt_recovery_digest"})
                self.write_segment("ar1")
                with self.assertRaisesRegex(ControlError, "历史恢复段摘要.*预约不一致"):
                    self.prove(path)
                summary.clear()
                summary.update(original)
                reservation.clear()
                reservation.update(original_reservation)
                self.write_segment("ar1")
                self.originals, self.parents = {}, {}
