"""候选草稿的精确坐标证明：原评估读者验真，两叶之外仍严格实读。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_qualification_material_source as material
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator
from tools.official_client_capture.tests import claude_vc5_chain_fixture as fixture
from tools.official_client_control.delivery_execution import VerifiedSource
from tools.official_client_control.errors import ControlError
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_control.sealed_gate_bindings import VerifiedGateProjection, _TOKEN, project_delivery_raw_bindings


class CandidateDraftProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="candidate-draft-projection-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name) / "data"
        self.campaign = self.data / "evidence/campaigns/claude-candidate-fixture"
        self.campaign.mkdir(parents=True)
        side = evaluator.write_side(Path(temporary.name) / "evaluation-input", capture_id="candidate-1")
        # 沿已有合成流生产器生成真实原件；这里只验证候选读者的绑定合同，
        # 不取官方客户端、不登记任何正式来源或 Store 资格。
        source = fixture.materialize_official_source(campaign=self.campaign, data_root=self.data,
            side=side, package_ref={"object_kind": "candidate_evidence_package", "sha256": "c" * 64})
        self.draft_path = self.campaign / "candidates/candidate-1/seal/seal-draft.json"
        self.draft = {"drafts": {name: source[name] for name in ("capture_manifest", "capture_index")}}
        fixture._write_json(self.draft_path, self.draft)
        self.round_one = {"candidate_id": "candidate-1", "store": {"candidate_evidence_package_ref": source["package_ref"]},
            "seal": {"seal_draft": fixture.binding(self.draft_path, self.campaign), "evidence_manifest": source["inventory"]},
            "assertion_context": {"evaluation_manifest": fixture.binding(
                self.campaign / "seam/official/evaluation-manifest.json", self.campaign)}}
        self.manifest = {"target_version": evaluator.TARGET_VERSION}
        self.originals, self.parents = {}, {}

    def retain(self, path: Path, expected_sha256: str | None = None) -> bytes:
        raw = path.read_bytes()
        row = fixture.binding(path, self.data)
        if expected_sha256 is not None and row["sha256"] != expected_sha256:
            raise ControlError("验真原件摘要漂移")
        if self.originals.setdefault(row["path"], row) != row:
            raise ControlError("已留存原件在登记投影时漂移")
        return raw

    def project_parent(self, path: Path, base: Path, *, coordinate_roots=()) -> None:
        self.retain(path)
        row = fixture.binding(path, self.data)
        self.parents[row["path"]] = material.VerifiedQualificationSubjectProjection(row,
            base.relative_to(self.data).as_posix(), (), tuple((coordinate, root.relative_to(self.data).as_posix())
                for coordinate, root in coordinate_roots), material._TOKEN)

    def prove(self) -> None:
        material._retain_candidate_evaluation_draft(self.campaign, self.manifest, self.round_one, self.data,
            retain=self.retain, project_parent=self.project_parent)

    def close(self, initial=None):
        source = VerifiedSource("0" * 64, "1" * 64, {}, "2" * 64, "fixture-only", self.data)
        projection = VerifiedGateProjection((), (), tuple(self.originals.values()), (), _TOKEN,
                                           tuple(self.parents.values()))
        return project_delivery_raw_bindings(source, initial or [fixture.binding(self.draft_path, self.data)],
                                            gate_projection=projection)

    def change_draft(self, extra) -> None:
        fixture._write_json(self.draft_path, {**self.draft, **extra})
        self.round_one["seal"]["seal_draft"] = fixture.binding(self.draft_path, self.campaign)

    def test_original_reader_proves_two_campaign_leaves_and_full_raw_closure(self) -> None:
        original = compare.candidate_evaluation_source(self.campaign, self.manifest, self.round_one)
        self.prove()
        closed = {row["path"] for row in self.close()}
        for name in ("capture_manifest", "capture_index"):
            path = self.campaign / original[name]["path"]
            self.assertIn(path.relative_to(self.data).as_posix(), closed)
        self.assertTrue(any(path.endswith("conn002.client_to_upstream.bin") for path in closed))
        self.assertEqual(self.parents[self.draft_path.relative_to(self.data).as_posix()].metadata_domains, ())

    def test_unknown_data_root_binding_is_still_fully_read(self) -> None:
        child = fixture._write_json(self.data / "unknown/child.json", {"fixture": "extra"})
        self.change_draft({"unknown": {"child": fixture.binding(child, self.data)}})
        self.prove()
        self.assertIn("unknown/child.json", {row["path"] for row in self.close()})
        child.unlink()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close()

    def test_other_campaign_relative_fields_receive_no_conversion_permission(self) -> None:
        self.change_draft({"evaluation_manifest": self.round_one["assertion_context"]["evaluation_manifest"] | {
            "bytes": (self.campaign / self.round_one["assertion_context"]["evaluation_manifest"]["path"]).stat().st_size}})
        self.prove()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close()

    def test_same_bytes_at_unverified_parent_coordinate_receive_no_permission(self) -> None:
        self.prove()
        duplicate = self.data / "unverified/seal-draft.json"
        duplicate.parent.mkdir()
        duplicate.write_bytes(self.draft_path.read_bytes())
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close([fixture.binding(duplicate, self.data)])

    def test_parent_drift_after_original_reader_refuses_before_projection(self) -> None:
        reader = compare.candidate_evaluation_source
        def drift(*args, **kwargs):
            result = reader(*args, **kwargs)
            self.draft_path.write_bytes(self.draft_path.read_bytes() + b" ")
            return result
        with mock.patch.object(compare, "candidate_evaluation_source", side_effect=drift):
            with self.assertRaisesRegex(ConfigurationError, "摘要漂移"):
                self.prove()
        self.assertEqual(self.parents, {})

    def test_second_parent_read_drift_refuses_registration(self) -> None:
        parent = self.project_parent
        def drift(path, base, **kwargs):
            path.write_bytes(path.read_bytes() + b" ")
            return parent(path, base, **kwargs)
        with self.assertRaisesRegex(ControlError, "登记投影时漂移"):
            material._retain_candidate_evaluation_draft(self.campaign, self.manifest, self.round_one, self.data,
                retain=self.retain, project_parent=drift)
        self.assertEqual(self.parents, {})

    def test_missing_or_drifted_leaf_and_declared_length_refuse_before_projection(self) -> None:
        for name in ("capture_manifest", "capture_index"):
            path = self.campaign / self.draft["drafts"][name]["path"]
            raw = path.read_bytes()
            with self.subTest(name=name, fault="missing"):
                path.unlink()
                try:
                    with self.assertRaises(ConfigurationError): self.prove()
                finally: path.write_bytes(raw)
            with self.subTest(name=name, fault="drift"):
                path.write_bytes(raw + b" ")
                try:
                    with self.assertRaises(ConfigurationError): self.prove()
                finally: path.write_bytes(raw)
            damaged = copy.deepcopy(self.draft)
            damaged["drafts"][name]["bytes"] += 1
            self.change_draft(damaged)
            with self.subTest(name=name, fault="length"), self.assertRaisesRegex(ConfigurationError, "字节数"):
                self.prove()
            self.change_draft(self.draft)
        self.assertEqual(self.parents, {})

    def test_stage_without_draft_reference_grants_no_new_projection(self) -> None:
        self.round_one["seal"] = {}
        self.prove()
        self.assertEqual((self.originals, self.parents), ({}, {}))


if __name__ == "__main__":
    unittest.main()
