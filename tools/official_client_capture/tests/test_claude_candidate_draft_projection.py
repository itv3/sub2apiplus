"""候选草稿的精确坐标证明：逐项重放原合同，未知附件仍严格实读。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_qualification_material_source as material
from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_seal as seal_contract
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

    def formal_extra_fields(self) -> dict:
        """按生产坐标准备合成附件，仅用于无写入的原读取合同测试。"""
        original = self.campaign / self.round_one["assertion_context"]["evaluation_manifest"]["path"]
        evaluation = fixture._write_json(self.draft_path.parent / "evaluation-manifest.json",
            json.loads(original.read_bytes()))
        self.round_one["assertion_context"]["evaluation_manifest"] = fixture.binding(
            evaluation, self.campaign, with_bytes=False)
        relay = fixture._write_json(self.draft_path.parent / "relay-view.json",
            {"label": "candidate", "probes": {}, "surfaces": []})
        return {"relay_view": fixture.binding(relay, self.campaign),
                "evaluation_manifest": fixture.binding(evaluation, self.campaign)}

    def test_original_reader_proves_two_campaign_leaves_and_full_raw_closure(self) -> None:
        original = compare.candidate_evaluation_source(self.campaign, self.manifest, self.round_one)
        self.prove()
        closed = {row["path"] for row in self.close()}
        for name in ("capture_manifest", "capture_index"):
            path = self.campaign / original[name]["path"]
            self.assertIn(path.relative_to(self.data).as_posix(), closed)
        self.assertTrue(any(path.endswith("conn002.client_to_upstream.bin") for path in closed))
        self.assertEqual(self.parents[self.draft_path.relative_to(self.data).as_posix()].metadata_domains, ())

    def add_preview(self, *, extra=None, wrong_attachment=False):
        """生成冻结批准引用的精确预览，用原预览摘要规则签定合成原件。"""
        from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
        fields = {"campaign_id": "claude-candidate-fixture", "candidate_id": "candidate-1",
            "attempt_id": "attempt-1", "generation": "generation-1"}
        self.change_draft({**fields, **self.formal_extra_fields()})
        checkpoint = fixture._write_json(self.draft_path.parent / "client-checkpoint.json", {"checkpoint": "fixture"})
        self.round_one["seal"]["client_checkpoint"] = fixture.binding(checkpoint, self.campaign)
        core = {"schema_version": candidate_seal.SEAL_PREVIEW_SCHEMA, **fields,
            **{name: self.round_one["seal"][name] for name in ("seal_draft", "evidence_manifest", "client_checkpoint")},
            "evaluation_manifest": fixture.binding(self.campaign / self.round_one["assertion_context"]["evaluation_manifest"]["path"], self.campaign),
            **(extra or {})}
        if wrong_attachment:
            other = fixture._write_json(self.draft_path.parent / "other-checkpoint.json", {"checkpoint": "fixture"})
            core["client_checkpoint"] = fixture.binding(other, self.campaign)
        digest = seal_contract._orchestrator()._fingerprint(core)
        preview = fixture._write_json(self.draft_path.parent / "seal-preview.json", {**core, "review_sha256": digest})
        self.round_one["seal"].update(preview=fixture.binding(preview, self.campaign), review_sha256=digest)
        return preview

    def test_approved_preview_closes_four_exact_campaign_coordinates(self):
        preview = self.add_preview()
        self.prove()
        closed = {row["path"] for row in self.close([fixture.binding(preview, self.data)])}
        self.assertIn(self.draft_path.relative_to(self.data).as_posix(), closed)
        self.assertIn(preview.relative_to(self.data).as_posix(), closed)
        self.assertEqual(len(self.parents[preview.relative_to(self.data).as_posix()].coordinate_roots), 4)

    def test_preview_same_bytes_at_other_coordinate_are_not_approved(self):
        self.add_preview(wrong_attachment=True)
        with self.assertRaisesRegex(ControlError, "阶段批准的附件不同源"):
            self.prove()

    def test_preview_modified_approval_digest_is_rejected(self):
        self.add_preview()
        self.round_one["seal"]["review_sha256"] = "0" * 64
        with self.assertRaisesRegex(ControlError, "批准的精确草稿不同源"):
            self.prove()

    def test_preview_unknown_campaign_binding_has_no_conversion_permission(self):
        preview = self.add_preview(extra={"unknown": fixture.binding(self.draft_path, self.campaign)})
        self.prove()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close([fixture.binding(preview, self.data)])

    def test_unknown_data_root_binding_is_still_fully_read(self) -> None:
        child = fixture._write_json(self.data / "unknown/child.json", {"fixture": "extra"})
        self.change_draft({"unknown": {"child": fixture.binding(child, self.data)}})
        self.prove()
        self.assertIn("unknown/child.json", {row["path"] for row in self.close()})
        child.unlink()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close()

    def test_other_campaign_relative_fields_receive_no_conversion_permission(self) -> None:
        # 原输入用已被正式 seal 定义的字段充当未知扩展，修为真正未知
        # 的附件字段；原件缺失的失败断言保持原样，不能自动转换任意叶。
        self.change_draft({"unknown_campaign_attachment": self.round_one["assertion_context"]["evaluation_manifest"] | {
            "bytes": (self.campaign / self.round_one["assertion_context"]["evaluation_manifest"]["path"]).stat().st_size}})
        self.prove()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close()

    def test_formal_fields_prove_exact_campaign_roots_and_full_raw_closure(self) -> None:
        fields = self.formal_extra_fields()
        for names in (("relay_view",), ("evaluation_manifest",), ("relay_view", "evaluation_manifest")):
            with self.subTest(fields=names):
                self.originals.clear()
                self.parents.clear()
                self.change_draft({name: fields[name] for name in names})
                self.prove()
                closed = {row["path"] for row in self.close()}
                parent = self.parents[self.draft_path.relative_to(self.data).as_posix()]
                campaign_relative = self.campaign.relative_to(self.data).as_posix()
                self.assertEqual(parent.base_relative, ".")
                self.assertEqual(parent.metadata_domains, ())
                expected_roots = {(("drafts", name), campaign_relative)
                    for name in ("capture_manifest", "capture_index")}
                expected_roots.update(((name,), campaign_relative) for name in names)
                self.assertEqual(set(parent.coordinate_roots), expected_roots)
                for name in names:
                    self.assertIn((self.campaign / fields[name]["path"]).relative_to(self.data).as_posix(), closed)
                self.assertTrue(any(path.endswith("conn002.client_to_upstream.bin") for path in closed))

    def test_top_evaluation_requires_exact_stage_path_and_digest(self) -> None:
        fields = self.formal_extra_fields()
        original = self.campaign / fields["evaluation_manifest"]["path"]
        other = self.draft_path.parent / "other-evaluation.json"
        other.write_bytes(original.read_bytes())
        for mismatch in ("same_bytes_other_path", "different_valid_manifest", "other_digest"):
            with self.subTest(mismatch=mismatch):
                self.originals.clear()
                self.parents.clear()
                changed = copy.deepcopy(fields)
                if mismatch == "other_digest":
                    changed["evaluation_manifest"]["sha256"] = "f" * 64
                else:
                    if mismatch == "different_valid_manifest":
                        document = json.loads(original.read_bytes())
                        document["capture_id"] = "other-capture"
                        assertion.validate_evaluation_manifest(document, expected_claude_version=self.manifest["target_version"])
                        fixture._write_json(other, document)
                    changed["evaluation_manifest"] = fixture.binding(other, self.campaign)
                self.change_draft(changed)
                with self.assertRaisesRegex(ControlError, "精确输入不同源"):
                    self.prove()
                self.assertEqual(self.parents, {})

    def test_formal_fields_reject_missing_drifted_and_wrong_length_before_projection(self) -> None:
        fields = self.formal_extra_fields()
        for name in ("relay_view", "evaluation_manifest"):
            path = self.campaign / fields[name]["path"]
            raw = path.read_bytes()
            for fault in ("missing", "drift", "length"):
                with self.subTest(field=name, fault=fault):
                    self.originals.clear()
                    self.parents.clear()
                    changed = copy.deepcopy(fields)
                    if fault == "missing":
                        path.unlink()
                    elif fault == "drift":
                        path.write_bytes(raw + b" ")
                    else:
                        changed[name]["bytes"] += 1
                    self.change_draft(changed)
                    try:
                        with self.assertRaises(ConfigurationError):
                            self.prove()
                        self.assertEqual(self.parents, {})
                    finally:
                        path.write_bytes(raw)

    def test_formal_fields_require_closed_three_field_bindings(self) -> None:
        fields = self.formal_extra_fields()
        for name in ("relay_view", "evaluation_manifest"):
            for fault in ("missing_bytes", "extra_key", "boolean_length", "negative_length", "outside_root"):
                with self.subTest(field=name, fault=fault):
                    self.originals.clear()
                    self.parents.clear()
                    changed = copy.deepcopy(fields)
                    if fault == "missing_bytes":
                        del changed[name]["bytes"]
                    elif fault == "extra_key":
                        changed[name]["unknown"] = "extra"
                    elif fault == "boolean_length":
                        changed[name]["bytes"] = True
                    elif fault == "negative_length":
                        changed[name]["bytes"] = -1
                    else:
                        changed[name]["path"] = "../outside.json"
                    self.change_draft(changed)
                    with self.assertRaises(ControlError):
                        self.prove()
                    self.assertEqual(self.parents, {})

    def test_formal_field_symlinks_do_not_grant_new_reading_roots(self) -> None:
        fields = self.formal_extra_fields()
        self.change_draft(fields)
        for name in ("relay_view", "evaluation_manifest"):
            path = self.campaign / fields[name]["path"]
            raw = path.read_bytes()
            for destination in ("campaign", "outside_campaign"):
                with self.subTest(field=name, destination=destination):
                    self.originals.clear()
                    self.parents.clear()
                    target = ((path.parent / ("copy-" + path.name)) if destination == "campaign"
                              else self.data / "outside-campaign" / path.name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    path.unlink()
                    path.symlink_to(target)
                    try:
                        with self.assertRaises(ConfigurationError):
                            self.prove()
                        self.assertEqual(self.parents, {})
                    finally:
                        path.unlink()
                        path.write_bytes(raw)

    def test_formal_field_drift_between_original_verification_and_retention_refuses(self) -> None:
        fields = self.formal_extra_fields()
        self.change_draft(fields)
        verifier = seal_contract._verify_bound_file
        for name in ("relay_view", "evaluation_manifest"):
            with self.subTest(field=name):
                self.originals.clear()
                self.parents.clear()
                target = self.campaign / fields[name]["path"]
                raw = target.read_bytes()
                def drift(campaign, binding, label):
                    path = verifier(campaign, binding, label)
                    if path == target:
                        path.write_bytes(raw + b" ")
                    return path
                try:
                    with mock.patch.object(seal_contract, "_verify_bound_file", side_effect=drift):
                        with self.assertRaisesRegex(ControlError, "验真原件摘要漂移"):
                            self.prove()
                    self.assertEqual(self.parents, {})
                finally:
                    target.write_bytes(raw)

    def test_unknown_child_of_proved_relay_still_uses_data_root_and_is_fully_read(self) -> None:
        fields = self.formal_extra_fields()
        child = fixture._write_json(self.data / "unknown/relay-child.json", {"fixture": "child"})
        relay = self.campaign / fields["relay_view"]["path"]
        document = json.loads(relay.read_bytes())
        document["unknown"] = fixture.binding(child, self.data)
        fixture._write_json(relay, document)
        fields["relay_view"] = fixture.binding(relay, self.campaign)
        self.change_draft(fields)
        self.prove()
        self.assertIn("unknown/relay-child.json", {row["path"] for row in self.close()})
        child.unlink()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close()

    def test_same_formal_parent_bytes_at_unverified_coordinate_receive_no_permission(self) -> None:
        self.change_draft(self.formal_extra_fields())
        self.prove()
        duplicate = self.data / "unverified/formal-seal-draft.json"
        duplicate.parent.mkdir()
        duplicate.write_bytes(self.draft_path.read_bytes())
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.close([fixture.binding(duplicate, self.data)])

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
