"""R2：构建身份、完整输入键和显式实现测试复用链的正负例。

这里使用真实文件与原构造／重放入口，Docker 实物另由 ARM64 真实链验证。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_candidate_build as build
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_vc_receipt as receipts
from tools.official_client_capture.tests import test_codex_upgrade_candidate_build as build_tests
from tools.official_client_capture.tests import test_codex_upgrade_vc_artifacts as artifact_tests

STAMP = "2026-09-24T00:00:00Z"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


def bind(path):
    return {"path": str(path), "sha256": receipts.file_sha256(path), "bytes": path.stat().st_size}


def resign(value, key):
    value[key] = artifacts.digest({name: row for name, row in value.items() if name != key})
    return value


class BuildRevisionTests(unittest.TestCase):
    def setUp(self):
        self.case = build_tests.CandidateBuildReceiptTests("test_frontend_provenance_replays_complete_proof_chain")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root.resolve()
        self.params = self.case.parameters
        self.candidate = self.params["candidate_id"]
        for path, content in ((self.case.source / "backend/go.mod", "module fixture\n"),
                              (self.case.source / "backend/go.sum", "fixture\n"),
                              (self.case.build_tree / "backend/vendor/modules.txt", "fixture\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.params["input_provenance"] = {"go_version": "go1.27.0", "base_images": {
            key: "registry/fixture@sha256:" + "a" * 64 for key in ("ALPINE_IMAGE", "POSTGRES_IMAGE", "NODE_IMAGE")}}
        self.inputs = build.collect_implementation_inputs(self.params, source_tree_sha256="4" * 64,
            requirements_sha256="3" * 64, go_version="go1.27.0")
        self.campaign = self.root / "campaign"
        self.source_root = self.root / "tests-r1"
        self.source_root.mkdir(mode=0o700)
        gates = [{"gate_id": gate_id, "kind": kind, "command": command, "exit_code": 0,
                  "passed": 1, "failed": 0, "approved_skip": 0, "unexpected_skip": 0}
                 for gate_id, kind, command in (("affected-fixture", "affected", ["go", "test", "./fixture"]),
                    ("check-egress-spec", "public", ["make", "check-egress-spec"]))]
        facts = {"schema_version": receipts.FACTS_SCHEMA, "kind": "implementation_tests",
            "subject": {"upgrade_id": "upgrade-fixture", "campaign_id": "codex-0_154_0-campaign",
                "campaign_purpose": "validation_only", "baseline_version": "0.151.0", "target_version": "0.154.0",
                "candidate_id": self.candidate, "attempt_id": None},
            "assertions": {"git_commit": build_tests.COMMIT, "source_tree_sha256": "4" * 64,
                "target_architecture": "linux/arm64", "gates": gates, "build_inputs": self.inputs},
            "evidence": [{"role": "check_egress_spec", "path": "check.log"}, {"role": "implementation_tests", "path": "gates.log"}]}
        for name in ("check.log", "gates.log"):
            (self.source_root / name).write_text("隔离夹具测试结果\n")
        write(self.source_root / "facts.json", facts)
        self.original = receipts.finalize(self.source_root, "facts.json", "receipt.json")
        sample = artifact_tests.CodexUpgradeVCArtifactsTests()._candidate_build_receipt()
        sample["candidate_id"] = self.candidate
        sample["source"] = {"root": str(self.case.source), "tree_sha256": "4" * 64, "git_commit": build_tests.COMMIT}
        sample["build"] = {"build_id": "build-r1", "parameters": self.params, "parameters_sha256": artifacts.digest(self.params), "inputs": self.inputs}
        sample["gate_requirements"]["requirements_sha256"] = "3" * 64
        sample["gate_plan"]["requirements_sha256"] = "3" * 64
        sample["implementation_tests"] = {"evidence_root": str(self.source_root), "receipt": {**bind(self.source_root / "receipt.json"), "path": "receipt.json"}, "receipt_digest": self.original["receipt_digest"]}
        self.previous_build = artifacts.validate_candidate_build_receipt(resign(sample, "receipt_digest"))
        self.build_path = write(self.campaign / "candidates" / self.candidate / "build-receipt.json", self.previous_build)
        self.r1 = self.revision(1, self.candidate)
        snapshot = upgrade._candidate_identity_snapshot(self.campaign, self.candidate, None)
        diagnosis = artifacts.build_candidate_invalidation_diagnosis(campaign_id=self.r1["campaign_id"],
            campaign_manifest_sha256="3" * 64, candidate_id=self.candidate, revision=1, reviewer="fixture",
            reviewed_at_utc=STAMP, evidence_refs=[], project_ledger_head_sha256="4" * 64, project_ledger_head_sequence=1,
            root_cause_id="rc1-" + "a" * 20, identity_snapshot=snapshot)
        invalidation = artifacts.build_candidate_invalidation(campaign_id=self.r1["campaign_id"], candidate_id=self.candidate,
            revision=1, diagnosis=diagnosis, recorded_at_utc=STAMP)
        invalidation_path = write(self.build_path.with_name("invalidation.json"), invalidation)
        self.r2 = self.revision(2, "candidate-r2", supersedes={"revision": 1, "candidate_id": self.candidate,
            "candidate_invalidated_event_sha256": "5" * 64,
            "invalidation_receipt": {"path": str(invalidation_path.relative_to(self.campaign)), "sha256": receipts.file_sha256(invalidation_path)}})
        self.target_path = self.campaign / "control/vc/revisions/r2/revision.json"

    def revision(self, number, candidate, supersedes=None):
        payload = artifacts.build_candidate_revision(campaign_id="codex-0_154_0-campaign", revision=number,
            candidate_id=candidate, opened_at_utc=STAMP,
            previous_revision_sha256=self.r1["record_sha256"] if number == 2 else None,
            vc3_checkpoint={"path": "control/vc/vc-3-checkpoint.json", "sha256": "1" * 64, "phase": "VC-3", "checkpoint_sha256": "2" * 64},
            vc3_stage_receipt={"path": "catalog/receipt.json", "sha256": "3" * 64}, supersedes=supersedes)
        path = write(self.campaign / "control/vc/revisions" / f"r{number}" / "revision.json", payload)
        write(path.with_name("COMMIT"), artifacts.build_candidate_revision_commit(campaign_id=payload["campaign_id"],
            revision=number, candidate_id=candidate, record_sha256=payload["record_sha256"], committed_at_utc=STAMP))
        return payload

    def reused(self, inputs=None, executed=()):
        root = self.root / "tests-r2"
        root.mkdir(mode=0o700, exist_ok=True)
        facts = receipts.build_reused_implementation_facts(self.target_path, inputs or self.inputs, executed)
        if facts["evidence"]:
            (root / "logs").mkdir(exist_ok=True)
            (root / "logs/implementation.log").write_text("当前目标平台隔离测试通过\n")
        write(root / "facts.json", facts)
        receipt = receipts.finalize(root, "facts.json", "receipt.json")
        return root, receipt

    def test_same_inputs_reuse_source_receipt_and_preserve_original_bytes(self):
        original = {path: path.read_bytes() for path in self.source_root.iterdir() if path.is_file()}
        root, receipt = self.reused()
        self.assertEqual(receipts.replay(root, "receipt.json"), receipt)
        self.assertEqual(receipt["assertions"]["reuse"]["reused_from"], "r1")
        self.assertEqual(receipt["assertions"]["reuse"]["execute_gate_ids"], [])
        self.assertEqual(receipt["subject"]["candidate_id"], "candidate-r2")
        self.assertEqual(original, {path: path.read_bytes() for path in original})
        self.assertEqual(receipt["evidence"], [])

    def test_each_actual_input_change_requires_appropriate_tests(self):
        for field in self.inputs:
            current = copy.deepcopy(self.inputs)
            if field == "base_images":
                current[field]["NODE_IMAGE"] = "registry/node@sha256:" + "b" * 64
            elif field.endswith("sha256"):
                current[field] = "f" * 64
            elif field == "target_architecture":
                current[field] = "linux/amd64"
            elif field == "go_version":
                current[field] = "go1.28.0"
            else:
                current[field] = "v21.1.0"
            with self.subTest(field=field):
                plan = receipts.plan_implementation_reuse(self.target_path, current)
                self.assertEqual(plan["mode"], "target_platform" if field == "parameters_sha256" else "full")
                with self.assertRaises(receipts.VCReceiptError):
                    receipts.build_reused_implementation_facts(self.target_path, current)

    def test_parameters_only_change_reuses_local_gate_and_runs_target_gates(self):
        current = {**self.inputs, "parameters_sha256": "f" * 64}
        root, receipt = self.reused(current, [self.original["assertions"]["gates"][0]])
        self.assertEqual(receipts.replay(root, "receipt.json"), receipt)
        reuse = receipt["assertions"]["reuse"]
        self.assertEqual(reuse["execute_gate_ids"], ["affected-fixture"])
        self.assertEqual(reuse["reuse_gate_ids"], ["check-egress-spec"])

    def test_replay_rejects_old_log_or_binding_tampering(self):
        root, _ = self.reused()
        (self.source_root / "gates.log").write_text("changed\n")
        with self.assertRaisesRegex(receipts.VCReceiptError, "摘要或大小漂移"):
            receipts.replay(root, "receipt.json")

    def test_uncommitted_revision_and_changed_build_binding_reject(self):
        commit_path = self.target_path.with_name("COMMIT")
        original = commit_path.read_bytes()
        commit_path.unlink()
        with self.assertRaises(receipts.VCReceiptError):
            receipts.plan_implementation_reuse(self.target_path, self.inputs)
        commit_path.write_bytes(original)
        old = json.loads(self.build_path.read_text())
        old["build"]["build_id"] = "different"
        write(self.build_path, resign(old, "receipt_digest"))
        with self.assertRaisesRegex(receipts.VCReceiptError, "作废时冻结"):
            receipts.plan_implementation_reuse(self.target_path, self.inputs)

    def test_read_only_validation_survives_moved_evidence_root_but_entries_verify_it(self):
        """只读校验与作废快照不读实现测试证据根；record 与 accept 入口的显式核对在证据根缺失时拒绝。"""

        moved = self.source_root.with_name("tests-r1-moved")
        self.source_root.rename(moved)
        self.addCleanup(lambda: moved.exists() and moved.rename(self.source_root))
        self.assertEqual(artifacts.validate_candidate_build_receipt(self.previous_build), self.previous_build)
        snapshot = upgrade._candidate_identity_snapshot(self.campaign, self.candidate, None)
        self.assertEqual(snapshot["build_receipt_sha256"], receipts.file_sha256(self.build_path))
        with self.assertRaisesRegex(artifacts.VCArtifactError, "实现测试输入绑定未通过"):
            artifacts.verify_candidate_build_implementation_evidence(self.previous_build)
        moved.rename(self.source_root)
        implementation = artifacts.verify_candidate_build_implementation_evidence(self.previous_build)
        self.assertEqual(implementation["receipt_digest"], self.original["receipt_digest"])

    def test_entry_verification_rejects_implementation_receipt_of_another_candidate(self):
        """实现测试收据属于别的 Candidate 时，只读校验照常通过，入口核对拒绝。"""

        other = json.loads(json.dumps(self.previous_build))
        other["candidate_id"] = "candidate-other"
        other = artifacts.validate_candidate_build_receipt(resign(other, "receipt_digest"))
        with self.assertRaisesRegex(artifacts.VCArtifactError, "身份不一致"):
            artifacts.verify_candidate_build_implementation_evidence(other)

    def test_replay_rejects_tampered_reuse_declaration(self):
        """封存只校验事实结构；篡改过的承接声明能被封存，但重放按真实输入重算后必须拒绝。"""

        for field in ("reused_from", "source_build_receipt"):
            with self.subTest(field=field):
                root = self.root / f"tests-tampered-{field.replace('_', '-')}"
                root.mkdir(mode=0o700)
                facts = receipts.build_reused_implementation_facts(self.target_path, self.inputs)
                reuse = facts["assertions"]["reuse"]
                if field == "reused_from":
                    reuse["reused_from"] = "r3"
                else:
                    reuse["source_build_receipt"] = {**reuse["source_build_receipt"], "sha256": "e" * 64}
                if facts["evidence"]:
                    (root / "logs").mkdir(exist_ok=True)
                    (root / "logs/implementation.log").write_text("当前目标平台隔离测试通过\n")
                write(root / "facts.json", facts)
                receipts.finalize(root, "facts.json", "receipt.json")
                with self.assertRaisesRegex(receipts.VCReceiptError, "复用声明"):
                    receipts.replay(root, "receipt.json")

    def test_legacy_v1_candidate_can_be_invalidated(self):
        """缺少四份机器收据的历史 v1 构建收据只读取身份字段，作废快照必须可取得。"""

        legacy = artifact_tests.CodexUpgradeVCArtifactsTests()._candidate_build_receipt()
        legacy["candidate_id"] = "candidate-legacy-v1"
        legacy["schema_version"] = artifacts.LEGACY_CANDIDATE_BUILD_SCHEMA
        for field in ("build_inventory", "frontend_provenance", "image_inspection", "capability_probe"):
            legacy.pop(field)
        legacy = resign(legacy, "receipt_digest")
        path = write(self.campaign / "candidates" / "candidate-legacy-v1" / "build-receipt.json", legacy)
        snapshot = upgrade._candidate_identity_snapshot(self.campaign, "candidate-legacy-v1", None)
        self.assertEqual(snapshot["build_receipt_sha256"], receipts.file_sha256(path))
        self.assertEqual(snapshot["binary_sha256"], legacy["binary"]["sha256"])
        self.assertEqual(snapshot["image_digest"], legacy["image"]["manifest_digest"])

    def test_build_input_binding_cannot_be_replaced_with_different_inputs(self):
        current = {**self.inputs, "parameters_sha256": "f" * 64}
        with self.assertRaisesRegex(receipts.VCReceiptError, "完整输入"):
            receipts.validate_build_input_binding(self.previous_build["implementation_tests"], current)

    def test_projection_ignores_only_output_identity_and_locations(self):
        current = copy.deepcopy(self.params)
        current["candidate_id"] = "candidate-r2"
        current["docker_build"]["image_id"] = "sha256:" + "1" * 64
        current["docker_build"]["labels"]["org.opencontainers.image.version"] = "0.2.4-4-candidate-r2"
        current["binary"]["sha256"] = "2" * 64
        self.assertEqual(build.implementation_parameter_projection(current), build.implementation_parameter_projection(self.params))
        current["go_build"]["environment"]["CGO_ENABLED"] = "1"
        self.assertNotEqual(build.implementation_parameter_projection(current), build.implementation_parameter_projection(self.params))

    def test_all_build_identity_fields_and_tampered_flags(self):
        old = {"revision": 1, "candidate_id": self.candidate, "git_commit": build_tests.COMMIT,
            "source_tree_sha256": "4" * 64, "image_id": "sha256:" + "5" * 64,
            "binary_sha256": "6" * 64, "image_digest": "sha256:" + "7" * 64, "build_parameters_sha256": "8" * 64}
        kwargs = {"campaign_id": self.r1["campaign_id"], "revision": 2, "candidate_id": "candidate-r2",
            "candidate_commit": old["git_commit"], "source_tree_sha256": old["source_tree_sha256"],
            **{key: old[key] for key in ("image_id", "binary_sha256", "image_digest", "build_parameters_sha256")},
            "build_receipt_sha256": "9" * 64, "vc3_stage_receipt_sha256": "a" * 64, "superseded": old, "sealed_at_utc": STAMP}
        with self.assertRaisesRegex(artifacts.VCArtifactError, "全部相同"):
            artifacts.build_candidate_revision_seal(**kwargs)
        for field in ("image_id", "binary_sha256", "image_digest", "build_parameters_sha256"):
            updated = {**kwargs, field: ("sha256:" if field in {"image_id", "image_digest"} else "") + "b" * 64}
            sealed = artifacts.build_candidate_revision_seal(**updated)
            self.assertEqual(sealed["changed_layers"], ["build"])
            altered = copy.deepcopy(sealed)
            altered["identity_change"]["source_tree_changed"] = True
            with self.assertRaisesRegex(artifacts.VCArtifactError, "变化标志"):
                artifacts.validate_candidate_revision_seal(resign(altered, "seal_sha256"))
        # 新 candidate_id 必然改变原始参数；输入投影相同时，该差异不足以建立重复候选。
        projected = {**old, "build_parameters_input_sha256": "c" * 64}
        with self.assertRaisesRegex(artifacts.VCArtifactError, "全部相同"):
            artifacts.build_candidate_revision_seal(**{**kwargs, "superseded": projected,
                "build_parameters_sha256": "b" * 64, "build_parameters_input_sha256": "c" * 64})

    def test_legacy_receipt_without_inputs_remains_readable(self):
        old = copy.deepcopy(self.previous_build)
        old["build"].pop("inputs")
        old["build"]["parameters"] = copy.deepcopy(old["build"]["parameters"])
        old["build"]["parameters"].pop("input_provenance")
        old["build"]["parameters_sha256"] = artifacts.digest(old["build"]["parameters"])
        self.assertEqual(artifacts.validate_candidate_build_receipt(resign(old, "receipt_digest")), old)

    def _driver(self):
        path = Path(__file__).parents[2] / "arm64_capture_driver/driver/vc4_resume.py"
        spec = importlib.util.spec_from_file_location("r2_resume_driver", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_driver_preserves_parallel_cold_path_and_defers_possible_reuse(self):
        driver = self._driver()
        current = {"tree_sha256": {"source": self.inputs["source_tree_sha256"]},
            "dependencies": {"go_mod": {"sha256": self.inputs["go_mod_sha256"]},
                "go_sum": {"sha256": self.inputs["go_sum_sha256"]}, "vendor_sha256": self.inputs["vendor_sha256"]},
            "toolchain": {"go_version": "go version go1.27.0 linux/arm64", "node_version": self.inputs["node_version"]},
            "base_images": {key: {"repo_digests": [value]} for key, value in self.inputs["base_images"].items()},
            "target_architecture": self.inputs["target_architecture"]}
        with mock.patch.object(driver, "context", return_value=(self.root, self.campaign, {}, {"requirements_sha256": "3" * 64})), \
             mock.patch.object(driver, "bound", return_value={"inputs": current}), \
             mock.patch.object(upgrade, "_current_candidate_revision_record", return_value=(2, self.r2)) as revision:
            self.assertEqual(driver.early_test_mode(self.root), "deferred")
            current["dependencies"]["vendor_sha256"] = "f" * 64
            self.assertEqual(driver.early_test_mode(self.root), "full")
            revision.return_value = (1, self.r1)
            self.assertEqual(driver.early_test_mode(self.root), "full")

    def test_driver_opens_superseding_revision_instead_of_forcing_initial(self):
        driver = self._driver()
        with mock.patch.dict(os.environ, {"CAND": "candidate-r2"}), \
             mock.patch.object(driver, "context", return_value=(self.root, self.campaign, {}, {})), \
             mock.patch.object(upgrade, "_current_candidate_revision_record", return_value=(1, self.r1)), \
             mock.patch.object(upgrade, "open_candidate_revision", return_value={"revision": 2, "candidate_id": "candidate-r2"}) as opened:
            driver.open_revision()
            args = opened.call_args.args[0]
            self.assertFalse(args.initial)
            self.assertEqual(args.supersedes, self.candidate)


if __name__ == "__main__":
    unittest.main()
