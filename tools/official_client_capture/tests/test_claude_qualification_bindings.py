"""资格绑定域与 Claude 冻结生产器的轻量回归；不启动守卫、Docker、网络或完整升级链。"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_candidate as candidate
from tools.official_client_capture import claude_upgrade_candidate_capture as capture
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_control import contracts
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control import qualification_bindings as bindings
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError
from tools.official_client_control.receipts import build_candidate_build_receipt
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import FileRuntime
from tools.official_client_capture.tests.test_claude_real_delivery_integration import (
    QualifiedFileRuntime, qualified_configuration_fixture,
)


class QualificationBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="claude-qdomains-", dir="/private/tmp")
        cls.root = Path(cls.temporary.name)
        cls.tree = ManagedToolTree(cls.root / "repo")
        cls.active = cls.tree.active()
        cls.active.__enter__()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.addClassCleanup(cls.active.__exit__, None, None, None)
        template_root = cls.root / "template"
        template_root.mkdir(mode=0o700)
        cls.template = SyntheticCampaign(template_root, schema="v2")
        fixture = cls.template
        fixture.bootstrap_and_campaign()
        fixture.discovery_and_evidence()
        fixture.profile_objects()
        fixture.profile_approve()
        fixture.strict_validate()
        cls.qualification = {"campaign_id": fixture.campaign_id, "candidate_id": "synthetic-candidate-strict",
                             "candidate_ref": fixture.references["candidate-strict"],
                             "build_receipt_ref": fixture.references["candidate-build-receipt"],
                             "validation_ref": fixture.references["validation-completed"],
                             "acceptance_ref": fixture.references["acceptance-strict"], "external_root_relative": "external"}

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-qdomain-case-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.world = Path(temporary.name)
        self.fixture = copy.copy(self.template)
        self.fixture.root = self.world / "campaign"
        shutil.copytree(self.template.root, self.fixture.root)
        self.fixture.references = copy.deepcopy(self.template.references)
        self.fixture.store_root = self.fixture.root / "store"
        self.fixture.store = ControlStore(self.fixture.store_root)
        self.fixture.external_root = self.fixture.root / "external"
        self.reader = ControlStore.open_read_only(self.fixture.store_root)

    def replay(self) -> dict:
        with mock.patch.object(guard, "GuardClient", side_effect=AssertionError("禁止启动守卫")):
            return executor.replay_qualification_graph(self.reader, self.qualification,
                                                      self.fixture.external_root, self.fixture.root)

    def receipt_with_parameters(self, parameters: dict, *, strict_artifacts: bool = False) -> dict:
        build_input = self.fixture.candidate_build_input()
        build_input["build_identity"]["parameters"] = parameters
        build_input["build_identity"]["parameters_sha256"] = canonical_sha256(parameters)
        if strict_artifacts:
            entries = []
            for name in ("container-healthcheck.sh", "docker-entrypoint.sh", "sub2api"):
                binding = self.fixture._write_external("artifacts/" + name, ("制品：" + name).encode())
                (self.fixture.external_root / binding["path"]).chmod(0o755)
                entries.append({"role": name, "binding": binding, "source_sha256": "7" * 64})
            build_input["artifact_inventory"] = {"entries": entries, "file_count": 3,
                                                  "total_bytes": sum(row["binding"]["bytes"] for row in entries),
                                                  "inventory_sha256": canonical_sha256(entries)}
        receipt = build_candidate_build_receipt(self.fixture.store, self.fixture.campaign_id, build_input)
        return self.fixture.store.write_receipt("candidate_build", receipt)

    def report(self, *, legacy: bool = False) -> dict:
        replay = self.replay()
        if legacy:
            replay = {key: value for key, value in replay.items() if key not in {"binding_projection", "qualification_roots", "external_bindings_base"}}
            replay["schema_version"] = bindings.LEGACY_REPLAY_SCHEMA
        material = {"qualification": copy.deepcopy(self.qualification)}
        report = {"schema_version": bindings.LEGACY_DELIVERY_SCHEMA if legacy else bindings.DELIVERY_SCHEMA,
                "candidate": material, "rollback": copy.deepcopy(material),
                "qualification_replays": [replay, copy.deepcopy(replay)], "candidate_compose": {},
                "rollback_compose": {}, "normalized_compose": {},
                "allowed_changes": ["application_image", "application_stage_label", "active_runtime_profile"], "result": "pass"}
        if not legacy:
            # 本类只隔离报告合同切面；完整双候选执行配置在下方专项独立验证。
            self.report_configuration = {"candidate": copy.deepcopy(material), "rollback": copy.deepcopy(material)}
            path = self.fixture.root / "inputs/qualification-configuration.json"
            guard.write_json_once(path, self.report_configuration)
            report["configuration_binding"] = guard.binding(self.fixture.root, path)
        return report

    def consume_original(self, report: dict, coordinate: str) -> None:
        """只读审计历史 v2 的完整外部绑定反例；当前 v3 消费另由真实来源专项验证。"""
        path = self.fixture.root / coordinate / "qualification.json"
        guard.write_json_once(path, report)
        binding = guard.binding(self.fixture.root, path)
        source = execution.VerifiedSource("a" * 64, "a" * 64,
            {binding["path"]: guard.check_binding(self.fixture.root, binding).read_bytes()},
            canonical_sha256(self.report_configuration), "fixture", self.fixture.root)
        evidence = {"evidence_type": "freeze", "entries": [{"facts": {
            "campaign_id": self.fixture.campaign_id, "result": "pass", "bindings": [binding]}}]}
        execution._validate_qualification_originals(self.fixture.store, evidence, source, current_execution=False)

    def current_world(self) -> "FrozenConfigurationConsumerTests":
        """复用当前双候选配置和实际 v3 导入，不降低策略或替换资格来源。"""

        helper = FrozenConfigurationConsumerTests()
        helper.tree = self.tree
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        return helper

    def test_unknown_top_level_and_nested_schema_do_not_authorize_metadata(self) -> None:
        projection = bindings.QualificationBindingProjection(self.reader)
        for document in ({"schema_version": "official-client-snapshot/v1", "document": {}},
                         {"schema_version": "unknown/v1", "nested": {"schema_version": "official-client-snapshot/v1"}},
                         {"schema_version": "official-client-control-object/v1", "object_kind": "unknown", "payload": {}},
                         {"schema_version": "official-client-control-object/v1", "object_kind": "snapshot",
                          "payload": {"schema_version": "official-client-snapshot/v1", "document": {}}}):
            with self.subTest(document=document), self.assertRaises(ControlError):
                projection.bindings(document)

    def test_other_parent_kind_cannot_hide_absolute_binding_behind_snapshot_string(self) -> None:
        document = {"schema_version": "official-client-control-object/v1", "object_kind": "operational_evidence",
                    "payload": {"schema_version": "official-client-operational-evidence/v1", "persona": self.fixture.persona,
                                "manifest_id": "bad-domain", "entries": [{"id": "row", "facts": {
                                    "nested_schema": "official-client-snapshot/v1",
                                    "source": {"path": "/not-authorized", "sha256": "a" * 64, "bytes": 1}}}]}}
        with self.assertRaises(ControlError):
            bindings.QualificationBindingProjection(self.reader).bindings(document)

    def test_snapshot_history_metadata_remains_bound_to_original_parent(self) -> None:
        payload = copy.deepcopy(self.reader.load_object(self.fixture.references["snapshot"])["payload"])
        payload["document"]["historical_source"] = {"path": "/never-open-this-host-file", "sha256": "a" * 64, "bytes": 19}
        reference = self.fixture.store.seal_object("snapshot", payload)
        document = self.reader.load_object(reference)
        projection = bindings.QualificationBindingProjection(self.reader)
        self.assertEqual(projection.bindings(document, reference), [])
        domain = projection.manifest()["parent_domains"][0]
        self.assertEqual(domain["parent_ref"], reference)
        self.assertEqual(domain["parent_sha256"], reference["sha256"])
        self.assertEqual(domain["domain_sha256"], canonical_sha256(payload["document"]))
        self.assertEqual(domain["domain_pointer"], "/payload/document")
        self.assertEqual(execution.typed_bindings(projection.manifest()), [])

    def test_snapshot_metadata_without_real_parent_ref_is_rejected(self) -> None:
        document = self.reader.load_object(self.fixture.references["snapshot"])
        with self.assertRaises(ControlError):
            bindings.QualificationBindingProjection(self.reader).bindings(document)

    def test_snapshot_parent_raw_tamper_fails_original_content_addressing(self) -> None:
        reference = self.fixture.references["snapshot"]
        document = self.reader.load_object(reference)
        path = self.reader.object_path(reference)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ControlError, "摘要漂移"):
            bindings.QualificationBindingProjection(self.reader).bindings(document, reference)

    def test_candidate_parameters_do_not_authorize_host_source_reads(self) -> None:
        parameters = {"origin": {"path": "/never-open-build-host", "sha256": "a" * 64, "bytes": 1}}
        reference = self.receipt_with_parameters(parameters)
        projection = bindings.QualificationBindingProjection(self.reader)
        external = projection.bindings(self.reader.load_receipt(reference), reference)
        self.assertIn(self.fixture.candidate_binding, external)
        self.assertNotIn(parameters["origin"], external)
        row = projection.manifest()["parent_domains"][0]
        self.assertEqual(row["domain_sha256"], canonical_sha256(parameters))
        self.assertEqual(row["parent_ref"], reference)

    def test_candidate_parameter_digest_and_parent_reference_must_match(self) -> None:
        reference = self.fixture.references["candidate-build-receipt"]
        document = self.reader.load_receipt(reference)
        document["build_identity"]["parameters"]["extra"] = True
        with self.assertRaisesRegex(ControlError, "parameters_sha256"):
            bindings.QualificationBindingProjection(self.reader).bindings(document, reference)
        original = self.reader.load_receipt(reference)
        with self.assertRaises(ControlError):
            bindings.QualificationBindingProjection(self.reader).bindings(original, {"receipt_kind": "candidate_build", "sha256": "a" * 64})

    def test_candidate_receipt_raw_tamper_is_rejected(self) -> None:
        reference = self.fixture.references["candidate-build-receipt"]
        document = self.reader.load_receipt(reference)
        path = self.reader.receipt_path(reference)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ControlError, "摘要漂移"):
            bindings.QualificationBindingProjection(self.reader).bindings(document, reference)

    def test_declared_artifact_inventory_remains_an_external_read(self) -> None:
        reference = self.fixture.references["candidate-build-receipt"]
        projection = bindings.QualificationBindingProjection(self.reader)
        external = projection.bindings(self.reader.load_receipt(reference), reference)
        self.assertIn(self.fixture.candidate_binding, external)
        path = self.fixture.external_root / self.fixture.candidate_binding["path"]
        path.unlink()
        with self.assertRaises((ControlError, guard.GuardError)):
            self.replay()

    def test_declared_four_source_files_cannot_use_another_root_or_wrong_bytes(self) -> None:
        for binding in (self.fixture.contract_binding, self.fixture.receipt_binding, self.fixture.catalog_binding,
                        self.fixture.official_binding):
            path = self.fixture.external_root / binding["path"]
            raw = path.read_bytes()
            # 另一个根里有完全相同原件，也不能自动承接。
            other = self.fixture.root / "other-root" / binding["path"]
            other.parent.mkdir(parents=True, exist_ok=True)
            other.write_bytes(raw)
            path.unlink()
            with self.subTest(path=binding["path"], case="错误根"), self.assertRaises((ControlError, guard.GuardError)):
                self.replay()
            path.write_bytes(raw + b"wrong")
            with self.subTest(path=binding["path"], case="字节漂移"), self.assertRaises((ControlError, guard.GuardError)):
                self.replay()
            path.write_bytes(raw)
        self.assertEqual(self.replay()["result"], "pass")

    def test_two_qualification_consumers_share_the_exact_parent_projection(self) -> None:
        report = self.replay()
        source = execution.VerifiedSource("a" * 64, "a" * 64, {}, "a" * 64, "fixture", self.fixture.root)
        control = execution._replay_qualification_subgraph(self.fixture.store, self.qualification, source)
        self.assertEqual(report["binding_projection"], control["binding_projection"])
        self.assertEqual(report["qualification_roots"], control["qualification_roots"])
        self.assertEqual(report["schema_version"], bindings.REPLAY_SCHEMA)
        self.assertEqual(report["external_bindings_base"], "data_root")
        self.assertEqual(control["external_bindings_base"], "external_root")
        bindings.validate_replay_report(control, self.qualification, self.reader,
            trusted_data_root=self.fixture.root, binding_base="external_root")

    def test_report_manifest_tamper_missing_domain_or_unknown_version_is_rejected(self) -> None:
        original = self.replay()
        for change in (lambda report: report["binding_projection"].update(schema_version="unknown/v9"),
                       lambda report: report["binding_projection"]["parent_domains"].clear(),
                       lambda report: report["binding_projection"]["parent_domains"][0].update(domain_sha256="f" * 64),
                       lambda report: report["binding_projection"]["parent_domains"][0].update(parent_sha256="f" * 64),
                       lambda report: report["binding_projection"]["parent_domains"][0].update(domain_kind="unknown")):
            report = copy.deepcopy(original)
            change(report)
            with self.subTest(report=report["binding_projection"]), self.assertRaises(ControlError):
                bindings.validate_replay_report(report, self.qualification, self.reader, trusted_data_root=self.fixture.root)

    def test_new_report_validates_and_old_report_has_readonly_permission_only(self) -> None:
        report = self.report()
        bindings.validate_delivery_report(report, self.reader, current_execution=False,
            configuration=self.report_configuration, trusted_data_root=self.fixture.root)
        with self.assertRaisesRegex(ControlError, "当前第二根能力要求资格 v3"):
            bindings.validate_delivery_report(report, self.reader, current_execution=True,
                configuration=self.report_configuration, trusted_data_root=self.fixture.root)
        current = self.current_world()
        bindings.validate_delivery_report(current.report, current.store, current_execution=True,
            configuration=current.configuration, trusted_data_root=current.root, source=current.qualification_source)
        with self.assertRaisesRegex(ControlError, "缺本次已核验"):
            bindings.validate_delivery_report(report, self.reader, current_execution=True, trusted_data_root=self.fixture.root)
        old = self.report(legacy=True)
        bindings.validate_delivery_report(old, self.reader, current_execution=False)
        with self.assertRaisesRegex(ControlError, "旧报告"):
            bindings.validate_delivery_report(old, self.reader, current_execution=True)

    def test_current_consumer_rejects_legacy_even_in_regression_source_scope(self) -> None:
        raw = canonical_json_bytes(self.report(legacy=True))
        binding = {"path": "freeze/qualification.json", "sha256": sha256_bytes(raw), "bytes": len(raw)}
        source = execution.VerifiedSource("a" * 64, "a" * 64, {binding["path"]: raw}, "a" * 64, "fixture", self.fixture.root)
        payload = {"evidence_type": "freeze", "entries": [{"facts": {"campaign_id": self.fixture.campaign_id,
                   "result": "pass", "origin": {"source_scope": "regression_fixture"}, "bindings": [binding]}}]}
        execution._validate_qualification_originals(self.fixture.store, payload, source, current_execution=False)
        with self.assertRaisesRegex(ControlError, "旧报告"):
            execution._validate_qualification_originals(self.fixture.store, payload, source, current_execution=True)

    def test_current_consumer_requires_current_effective_tool_identity(self) -> None:
        current = self.current_world()
        raw = canonical_json_bytes(current.report)
        binding = {"path": "freeze/qualification.json", "sha256": sha256_bytes(raw), "bytes": len(raw)}
        source = execution.VerifiedSource("a" * 64, "a" * 64, {binding["path"]: raw},
            canonical_sha256(current.configuration), "fixture", current.root)
        payload = {"evidence_type": "freeze", "entries": [{"facts": {"campaign_id": current.fixture.campaign_id,
                   "result": "pass", "bindings": [binding]}}]}
        key = "official_client_control/store.py"
        self.tree.write(key, self.tree.path(key).read_bytes() + "\n# 未登记的工具变更\n".encode())
        try:
            with self.assertRaisesRegex(ControlError, "工具身份"):
                execution._validate_qualification_originals(current.store, payload, source, current_execution=True)
        finally:
            self.tree.restore()
        execution._validate_qualification_originals(current.store, payload, source, current_execution=True)

    def test_current_pass_cannot_omit_report_but_failure_can_be_recorded(self) -> None:
        source = execution.VerifiedSource("a" * 64, "a" * 64, {}, "a" * 64, "fixture")
        payload = {"evidence_type": "stage_execution", "entries": [{"facts": {
            "campaign_id": self.fixture.campaign_id, "result": "pass", "bindings": []}}]}
        with self.assertRaisesRegex(ControlError, "唯一资格报告"):
            execution._validate_qualification_originals(self.fixture.store, payload, source, current_execution=True)
        payload["entries"][0]["facts"]["result"] = "failed"
        execution._validate_qualification_originals(self.fixture.store, payload, source, current_execution=True)

    def test_original_report_cannot_omit_root_snapshot_and_its_parent_domain_together(self) -> None:
        report = self.report()
        snapshot = self.reader.load_receipt(self.qualification["build_receipt_ref"])["profile_identity"]["snapshot_ref"]
        for replay in report["qualification_replays"]:
            replay["object_refs"] = [row for row in replay["object_refs"] if row != snapshot]
            replay["binding_projection"]["parent_domains"] = [row for row in replay["binding_projection"]["parent_domains"]
                                                               if row["parent_ref"] != snapshot]
        with self.assertRaisesRegex(ControlError, "必需 object_refs"):
            self.consume_original(report, "omit-snapshot-and-domain")

    def test_original_report_cannot_omit_any_declared_real_external_binding(self) -> None:
        original = self.report()
        for index, binding in enumerate(original["qualification_replays"][0]["external_bindings"]):
            report = copy.deepcopy(original)
            for replay in report["qualification_replays"]:
                replay["external_bindings"] = [row for row in replay["external_bindings"] if row != binding]
            with self.subTest(path=binding["path"]), self.assertRaisesRegex(ControlError, "真实外部绑定"):
                self.consume_original(report, "omit-binding-" + str(index))

    def test_original_report_cannot_omit_any_required_reference_category(self) -> None:
        original = self.report()
        replay = original["qualification_replays"][0]
        snapshot = self.reader.load_receipt(self.qualification["build_receipt_ref"])["profile_identity"]["snapshot_ref"]
        required = {"fact_refs": self.qualification["candidate_ref"], "object_refs": snapshot,
                    "receipt_refs": next(row for row in replay["receipt_refs"] if row["receipt_kind"] == "validation_gate")}
        for key, reference in required.items():
            report = copy.deepcopy(original)
            report["qualification_replays"][0][key] = [row for row in replay[key] if row != reference]
            with self.subTest(key=key), self.assertRaisesRegex(ControlError, "必需 " + key):
                self.consume_original(report, "omit-" + key)

    def test_qualification_roots_reject_wrong_fact_type_and_candidate_ownership(self) -> None:
        for key, value in (("validation_ref", self.qualification["acceptance_ref"]),
                           ("candidate_id", "another-candidate"), ("campaign_id", "another-campaign")):
            qualification = {**self.qualification, key: value}
            with self.subTest(key=key), self.assertRaises(ControlError):
                bindings.validate_qualification_roots(self.reader, qualification)

    def test_real_same_candidate_build_cannot_replace_the_frozen_build_root(self) -> None:
        alternate = self.receipt_with_parameters({"offline": True, "extra": "另一份真实构建合同"})
        qualification = {**self.qualification, "build_receipt_ref": alternate}
        with self.assertRaisesRegex(ControlError, "四根未闭合"):
            bindings.validate_qualification_roots(self.reader, qualification)

    def test_data_and_external_coordinates_are_explicit_and_not_interchangeable(self) -> None:
        replay = self.replay()
        source = execution.VerifiedSource("a" * 64, "a" * 64, {}, "a" * 64, "fixture", self.fixture.root)
        raw = execution._replay_qualification_subgraph(self.reader, self.qualification, source)
        self.assertIn(self.fixture.candidate_binding, raw["external_bindings"])
        rebased = {**self.fixture.candidate_binding, "path": "external/" + self.fixture.candidate_binding["path"]}
        self.assertIn(rebased, replay["external_bindings"])
        wrong = copy.deepcopy(replay)
        wrong["external_bindings_base"] = "external_root"
        with self.assertRaisesRegex(ControlError, "坐标基准"):
            bindings.validate_replay_report(wrong, self.qualification, self.reader, trusted_data_root=self.fixture.root)
        wrong = copy.deepcopy(replay)
        wrong["external_bindings"] = raw["external_bindings"]
        with self.assertRaisesRegex(ControlError, "真实外部绑定"):
            bindings.validate_replay_report(wrong, self.qualification, self.reader, trusted_data_root=self.fixture.root)
        with self.assertRaisesRegex(ControlError, "多根搜索"):
            bindings.validate_replay_report(replay, self.qualification, self.reader,
                trusted_data_root=self.fixture.root, external_root=self.fixture.root)

    def test_report_cannot_supply_its_own_trusted_absolute_data_root(self) -> None:
        report = self.report()
        report["candidate"]["qualification"]["trusted_data_root"] = str(self.fixture.root)
        with self.assertRaisesRegex(ControlError, "可信数据根"):
            bindings.validate_delivery_report(report, self.reader, current_execution=False)

    def test_explicit_root_must_match_an_existing_verified_source_root(self) -> None:
        source = execution.VerifiedSource("a" * 64, "a" * 64, {}, "a" * 64, "fixture", self.fixture.root)
        with self.assertRaisesRegex(ControlError, "受管来源不一致"):
            execution._replay_qualification_subgraph(self.reader, self.qualification, source,
                                                   trusted_data_root=self.fixture.external_root)

    def test_extra_reference_still_uses_the_original_store_validator(self) -> None:
        report = self.report()
        for replay in report["qualification_replays"]:
            replay["object_refs"].append({"object_kind": "snapshot", "sha256": "f" * 64})
            replay["object_refs"].sort(key=canonical_sha256)
        with self.assertRaises(ControlError):
            self.consume_original(report, "forged-extra-reference")

    def test_strict_production_inventory_checks_all_bytes_and_modes(self) -> None:
        reference = self.receipt_with_parameters({"schema_version": "sub2apiplus-candidate-build-parameters/v2"}, strict_artifacts=True)
        projection = bindings.QualificationBindingProjection(self.reader)
        external = projection.bindings(self.reader.load_receipt(reference), reference)
        requirements = projection.manifest()["artifact_file_requirements"]
        self.assertEqual(len(requirements), 3)
        for requirement in requirements:
            binding = next(row for row in external if row["path"] == requirement["relative_path"])
            self.reader._verify_external_binding(self.fixture.external_root, binding)
            projection.verify_external_mode(self.fixture.external_root, binding)
            path = self.fixture.external_root / binding["path"]
            path.chmod(0o600)
            with self.assertRaisesRegex(ControlError, "0755"):
                projection.verify_external_mode(self.fixture.external_root, binding)
            path.chmod(0o755)
            raw = path.read_bytes()
            path.write_bytes(raw + b"wrong")
            with self.assertRaises(ControlError):
                self.reader._verify_external_binding(self.fixture.external_root, binding)
            path.write_bytes(raw)

    def test_production_inventory_cannot_downgrade_when_parameter_schema_is_missing(self) -> None:
        for parameters in ({}, {"offline": True, "target": "linux/amd64"},
                           {"schema_version": "unknown/v1"}):
            reference = self.receipt_with_parameters(parameters, strict_artifacts=True)
            with self.subTest(parameters=parameters), self.assertRaisesRegex(ControlError, "不能降为合成库存"):
                bindings.QualificationBindingProjection(self.reader).bindings(self.reader.load_receipt(reference), reference)

    def test_strict_parameter_contract_cannot_use_the_historical_synthetic_inventory(self) -> None:
        reference = self.receipt_with_parameters({"schema_version": bindings.STRICT_BUILD_PARAMETERS_SCHEMA})
        with self.assertRaisesRegex(ControlError, "三件真实制品闭集"):
            bindings.QualificationBindingProjection(self.reader).bindings(self.reader.load_receipt(reference), reference)

    def test_qualified_file_runtime_archives_real_v2_originals_without_a_guard(self) -> None:
        # 资格图重放仍为 v2；当前执行总报告必须是实际导入所得的 v3。
        helper, configuration = qualified_configuration_fixture(self, self.tree)
        root, fixture = helper.root, helper.fixture
        configuration_path = root / "inputs/qualified-configuration.json"
        guard.write_json_once(configuration_path, configuration)
        candidate_id = "synthetic-candidate-strict"
        context = {"campaign_id": fixture.campaign_id, "candidate_id": candidate_id,
            "delivery_id": candidate_id + "-d1", "segment": "dr0", "run_nonce": candidate_id + "-d1-dr0",
            "data_root": str(root), "configuration_ref": guard.binding(root, configuration_path)}
        runtime = QualifiedFileRuntime(configuration, context)
        before_state = (root / "state.json").read_bytes()
        # 本专项只核原件生产与消费；守卫及父物理执行入口都明确禁止。
        with mock.patch.object(guard, "GuardClient", side_effect=AssertionError("本专项禁止启动守卫")), \
             mock.patch.object(guard, "serve", side_effect=AssertionError("本专项禁止启动守卫")):
            frozen = runtime.freeze({}, root / "freeze")
            freeze_binding = next(row for row in frozen["cores"]["freeze"]["before_bindings"]
                                  if Path(row["path"]).name == "qualification.json")
            self.assertTrue(all(freeze_binding in gate_row["facts"]["bindings"] for gate_row in frozen["gates"]))
            report = guard.read_json(guard.check_binding(root, freeze_binding))
            self.assertEqual(report["schema_version"], bindings.MATERIAL_DELIVERY_SCHEMA)
            self.assertEqual(len(report["qualification_replays"]), 2)
            self.assertNotEqual(report["qualification_replays"][0]["qualification_roots"],
                                report["qualification_replays"][1]["qualification_roots"])
            source = execution.VerifiedSource("a" * 64, "a" * 64,
                {freeze_binding["path"]: guard.check_binding(root, freeze_binding).read_bytes()},
                canonical_sha256(configuration), "fixture", root)
            evidence = {"evidence_type": "freeze", "entries": [{"facts": {
                "campaign_id": fixture.campaign_id, "result": "pass", "bindings": [freeze_binding]}}]}
            execution._validate_qualification_originals(fixture.store, evidence, source, current_execution=True)
            # 父入口在执行前故意拒绝；必须已归档资格原件且完全没有改变现场。
            stage_directory = root / "stage"
            with mock.patch.object(FileRuntime, "execute", side_effect=RuntimeError("轻专项停止于物理执行前")) as physical:
                with self.assertRaisesRegex(RuntimeError, "物理执行前"):
                    runtime.execute("candidate_active", {}, {}, stage_directory, lambda: None)
                physical.assert_called_once()
            stage_report = guard.read_json(stage_directory / "qualification.json")
            bindings.validate_delivery_report(stage_report, fixture.store, current_execution=True,
                configuration=configuration, trusted_data_root=root, source=runtime._qualification_source)
        self.assertEqual((root / "state.json").read_bytes(), before_state)


class FrozenConfigurationConsumerTests(unittest.TestCase):
    """两份真实同 Campaign 资格图核 freeze、stage 消费切面及完整 Plan 纯校验。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree_directory = tempfile.TemporaryDirectory(prefix="qualification-frozen-tools-", dir="/private/tmp")
        cls.addClassCleanup(cls.tree_directory.cleanup)
        cls.tree = ManagedToolTree(Path(cls.tree_directory.name) / "repo")

    def setUp(self) -> None:
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        helper, configuration = qualified_configuration_fixture(self, self.tree)
        self.root = helper.root
        os.chown(self.root, -1, os.getegid())
        fixture = helper.fixture
        environment = guard.read_json(self.root / "state.json")["environment"]
        configuration_path = self.root / "inputs/qualified-configuration.json"
        guard.write_json_once(configuration_path, configuration)
        candidate_id = "synthetic-candidate-strict"
        context = {"campaign_id": fixture.campaign_id, "candidate_id": candidate_id,
            "delivery_id": candidate_id + "-d1", "segment": "dr0", "run_nonce": candidate_id + "-d1-dr0",
            "data_root": str(self.root), "configuration_ref": guard.binding(self.root, configuration_path)}
        projection = executor.stable_environment_projection({"services": {"app": {"image": "fixture-candidate"},
            "db": {"image": "fixed-db"}, "redis": {"image": "fixed-redis"}}}, {
            "app": {"Config": {"Image": "fixture-candidate"}}, "db": {"Config": {"Image": "fixed-db"}},
            "redis": {"Config": {"Image": "fixed-redis"}}}, "app")
        environment.update(architecture="linux/arm64", configuration_sha256=canonical_sha256(configuration),
                           environment_sha256=canonical_sha256(projection))
        state = guard.read_json(self.root / "state.json")
        state["environment"] = environment
        guard.atomic_json(self.root / "state.json", state)
        self.configuration, self.context, self.fixture, self.store = configuration, context, fixture, fixture.store
        self.state_before = (self.root / "state.json").read_bytes()
        # 这里只提供 regression_fixture 的完整合同形状，明确不声称真实独立守卫已取证。
        self.identity = {"contract_version": "claude-upgrade-delivery-guard-identity/v1",
            "authority": "claude-arm64-recovery-guard", "source_scope": "regression_fixture", "host_id": "ARM64",
            "instance_id": configuration["instance_id"], "service_name": configuration["guard"]["service_name"],
            "service_instance_id": "qualification-fixture-only", "boot_id": "fixture-only", "pid": os.getpid(),
            "process_start": "fixture-only", "process_group_id": os.getpgrp(), "independent_service": False,
            **{key: configuration["guard"][key] for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding")}}
        self.origin = {"authority": self.identity["authority"], "source_scope": "regression_fixture",
            "configuration_sha256": canonical_sha256(configuration),
            "service_instance_id": self.identity["service_instance_id"], "operations_index_binding": None}
        channel = mock.patch.dict(execution.SOURCE_VERIFIERS, {self.identity["authority"]: self.verifier}, clear=True)
        channel.start()
        self.addCleanup(channel.stop)
        for name in ("GuardClient", "serve"):
            forbidden = mock.patch.object(guard, name, side_effect=AssertionError("本专项禁止启动守卫"))
            forbidden.start()
            self.addCleanup(forbidden.stop)
        runtime = QualifiedFileRuntime(configuration, context)
        frozen = runtime.freeze({}, self.root / "legal-freeze")
        self.qualification_source = runtime._qualification_source
        self.freeze_core = frozen["cores"]["freeze"]
        self.freeze_core["guard_identity"] = self.identity
        self.report = guard.read_json(self.root / "legal-freeze/qualification.json")
        self.references = {}
        for kind, core in frozen["cores"].items():
            self.references[kind] = self.store.seal_object("operational_evidence", self.manifest(kind, core, "legal-" + kind))
        q = configuration["candidate"]["qualification"]
        self.plan = delivery._with_identity({"schema_version": execution.PLAN_V2, "campaign_id": fixture.campaign_id,
            "persona": fixture.persona, "plan_id": "qualification-fixture-plan", "delivery_id": context["delivery_id"],
            "candidate_id": context["candidate_id"], "candidate_ref": q["candidate_ref"],
            "candidate_build_receipt_ref": q["build_receipt_ref"], "validation_completed_ref": q["validation_ref"],
            "acceptance_ref": q["acceptance_ref"],
            "candidate_evidence_package_ref": self.store.load_fact(q["acceptance_ref"])["payload"]["candidate_evidence_package_ref"],
            **environment, "runtime_profile_sha256": configuration["candidate"]["runtime_profile_sha256"],
            "rollback_runtime_profile_sha256": configuration["rollback"]["runtime_profile_sha256"],
            "candidate_image_digest": configuration["candidate"]["image_digest"],
            "rollback_image_digest": configuration["rollback"]["image_digest"], "observation_window_seconds": 1,
            "stage_checks": {stage: list(delivery.STAGE_CHECKS[stage]) for stage in delivery.STAGES},
            "vircs_state": dict(delivery.PRODUCTION_STATE), "environment_freeze_ref": self.references["freeze"],
            "rollback_material_refs": [self.references["rollback_state"]],
            "executor_configuration_ref": self.references["configuration"],
            "stable_environment_projection_ref": self.references["stable_projection"],
            "probe_recipe_ref": self.references["zero_model_probes"], "rollback_state_ref": self.references["rollback_state"],
            "environment_before_ref": self.references["freeze"], "deployment_configuration_ref": self.references["deployment_configuration"]})
        contracts._validate_candidate_delivery_plan(self.plan)
        self.plan_ref = self.store.seal_object("candidate_delivery_plan", self.plan)

    def verifier(self, _store: object, core: dict, _origin: dict) -> execution.VerifiedSource:
        originals = {row["path"]: guard.check_binding(self.root, row).read_bytes()
                     for row in execution.typed_bindings(core)}
        return execution.VerifiedSource(canonical_sha256(core),
            self.configuration["guard"]["deployment_receipt_binding"]["sha256"], originals,
            canonical_sha256(self.configuration), self.identity["service_instance_id"], self.root)

    def manifest(self, kind: str, core: dict, name: str) -> dict:
        return {"schema_version": execution.OPERATIONAL_V2, "persona": self.fixture.persona,
            "manifest_id": name.replace("_", "-"), "evidence_type": kind, "entries": [{"id": kind,
            "facts": {**copy.deepcopy(core), "origin": self.origin}}]}

    def original_report(self, report: dict, name: str) -> dict:
        path = self.root / "variants" / name / "qualification.json"
        guard.write_json_once(path, report)
        return guard.binding(self.root, path)

    def freeze_evidence(self, binding: dict, name: str) -> dict:
        core = copy.deepcopy(self.freeze_core)
        core["before_bindings"] = [binding]
        for row in core["gates"]:
            row["facts"]["bindings"] = [binding]
        return self.manifest("freeze", core, name + "-freeze")

    def stage_evidence(self, binding: dict, name: str, *, configuration_ref: dict | None = None) -> dict:
        path = self.root / "variants" / name / "stage-original.json"
        target = {key: self.configuration["candidate"][key] for key in ("image_digest", "runtime_profile_sha256")}
        guard.write_json_once(path, {**target, "physical_action": False})
        marker = guard.binding(self.root, path)
        core = {"contract_version": "claude-delivery-stage-execution/v1", "result": "pass",
            "campaign_id": self.fixture.campaign_id, "delivery_id": self.context["delivery_id"],
            "candidate_id": self.context["candidate_id"], "stage": "candidate_active", "run_nonce": "qualification-fixture-dr0",
            "operation_id": "qualification-fixture-operation", "plan_ref": self.plan_ref,
            "configuration_ref": configuration_ref or self.references["configuration"],
            "stable_projection_ref": self.references["stable_projection"], "probe_recipe_ref": self.references["zero_model_probes"],
            "target": target, "actual": {"image": {"state": "known", "value": target["image_digest"], "direct_binding": marker},
                "profile": {"state": "known", "value": target["runtime_profile_sha256"], "direct_binding": marker}},
            "environment": self.freeze_core["environment"], "checks": [{"id": "health", "result": "pass", "facts": {}}],
            "observed_seconds": 0, "live_request_count": 0, "request_records": [], "guard_identity": self.identity,
            "prepared_binding": marker, "physical_intent_binding": None, "guard_terminal_binding": marker,
            "journal_binding": marker, "before_bindings": [], "during_bindings": [binding], "after_bindings": [],
            "restoration": {"state": "not_required", "bindings": [], "reason": "本专项无物理动作"}}
        return self.manifest("stage_execution", core, name + "-stage")

    def stored_negative_evidence(self, payload: dict) -> dict:
        """模拟旧消费者曾接受的内容寻址原件，只用于新消费端拒绝回归，不替换任何校验器。"""
        document = {"schema_version": contracts.OBJECT_SCHEMA, "object_kind": "operational_evidence", "payload": payload}
        contracts.validate_object_document(document)
        raw = canonical_json_bytes(document)
        reference = {"object_kind": "operational_evidence", "sha256": sha256_bytes(raw)}
        guard.write_once(self.store.object_path(reference), raw)
        self.store.load_object(reference)
        return reference

    def assert_all_consumers_reject(self, report: dict, name: str) -> None:
        binding = self.original_report(report, name)
        freeze = self.freeze_evidence(binding, name)
        stage = self.stage_evidence(binding, name)
        for kind, payload in (("freeze", freeze), ("stage_execution", stage)):
            with self.subTest(consumer=kind, variant=name), self.assertRaises(ControlError):
                execution.validate_evidence_graph(self.store, payload, current_execution=True)
        stage_ref = self.stored_negative_evidence(stage)
        with self.subTest(consumer="StageFact 消费端", variant=name), self.assertRaises(ControlError):
            execution.validate_stage_execution(self.store, self.plan,
                {"schema_version": execution.FACT_V3, "execution_evidence_ref": stage_ref}, current_execution=True)
        freeze_ref = self.stored_negative_evidence(freeze)
        plan = copy.deepcopy(self.plan)
        plan.update(environment_before_ref=freeze_ref, environment_freeze_ref=freeze_ref)
        plan = delivery._with_identity(plan)
        contracts._validate_candidate_delivery_plan(plan)
        with self.subTest(consumer="Plan", variant=name), self.assertRaises(ControlError):
            execution.validate_plan(self.store, plan, current_execution=True)

    def test_legal_double_candidate_plan_and_consumer_slices_pass(self) -> None:
        self.assertNotEqual(self.configuration["candidate"]["qualification"]["candidate_ref"],
                            self.configuration["rollback"]["qualification"]["candidate_ref"])
        binding = self.original_report(self.report, "legal")
        execution.validate_evidence_graph(self.store, self.freeze_evidence(binding, "legal"), current_execution=True)
        execution.validate_evidence_graph(self.store, self.stage_evidence(binding, "legal"), current_execution=True)
        execution.validate_plan(self.store, self.plan, current_execution=True)
        self.assertEqual((self.root / "state.json").read_bytes(), self.state_before)

    def test_same_campaign_swap_and_replacement_are_rejected_by_all_consumers(self) -> None:
        swapped = copy.deepcopy(self.report)
        swapped["candidate"], swapped["rollback"] = swapped["rollback"], swapped["candidate"]
        swapped["qualification_replays"].reverse()
        swapped["candidate_compose"], swapped["rollback_compose"] = swapped["rollback_compose"], swapped["candidate_compose"]
        replaced = copy.deepcopy(self.report)
        replaced["candidate"] = copy.deepcopy(self.report["rollback"])
        replaced["qualification_replays"][0] = copy.deepcopy(self.report["qualification_replays"][1])
        replaced["candidate_compose"] = copy.deepcopy(self.report["rollback_compose"])
        for name, report in (("swap", swapped), ("replace", replaced)):
            # 两份原件内部仍是完整合法资格；拒绝原因必须是与本次冻结配置脱离。
            for index, material in enumerate((report["candidate"], report["rollback"])):
                bindings.validate_replay_report(report["qualification_replays"][index], material["qualification"],
                    self.store, trusted_data_root=self.root)
            self.assert_all_consumers_reject(report, name)
        self.assertEqual((self.root / "state.json").read_bytes(), self.state_before)

    def test_missing_configuration_binding_or_real_configuration_drift_are_rejected(self) -> None:
        missing = copy.deepcopy(self.report)
        missing.pop("configuration_binding")
        self.assert_all_consumers_reject(missing, "missing-configuration-binding")
        drift = copy.deepcopy(self.configuration)
        drift["limits"]["poll_seconds"] = 2
        executor.validate_configuration(drift)
        path = self.root / "inputs/different-configuration.json"
        guard.write_json_once(path, drift)
        report = {**self.report, "configuration_binding": guard.binding(self.root, path)}
        self.assert_all_consumers_reject(report, "configuration-drift")
        broken = copy.deepcopy(self.report)
        broken["configuration_binding"]["sha256"] = "f" * 64
        self.assert_all_consumers_reject(broken, "configuration-byte-drift")

    def test_stage_and_plan_require_the_exact_configuration_original_binding(self) -> None:
        # 同一配置值复制到另一个真实坐标仍不能顶替阶段和 Plan 指向的精确原件。
        path = self.root / "inputs/identical-configuration-copy.json"
        guard.write_json_once(path, self.configuration)
        core = copy.deepcopy(self.store.load_object(self.references["configuration"])["payload"]["entries"][0]["facts"])
        core["binding"] = guard.binding(self.root, path)
        other_ref = self.store.seal_object("operational_evidence", self.manifest("configuration", core, "other-configuration"))
        binding = self.original_report(self.report, "configuration-copy")
        with self.assertRaisesRegex(ControlError, "配置绑定"):
            execution.validate_evidence_graph(self.store,
                self.stage_evidence(binding, "configuration-copy", configuration_ref=other_ref), current_execution=True)
        plan = delivery._with_identity({**self.plan, "executor_configuration_ref": other_ref})
        contracts._validate_candidate_delivery_plan(plan)
        with self.assertRaisesRegex(ControlError, "配置绑定"):
            execution.validate_plan(self.store, plan, current_execution=True)


class ClaudeArtifactProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-artifact-copy-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.campaign = self.data / "evidence/campaigns/fixture"
        self.campaign.mkdir(parents=True, mode=0o700)
        self.context = self.root / "context"
        (self.context / "deploy").mkdir(parents=True)
        self.binary = self.root / "binary"
        self.sources = {"/app/sub2api": self.binary, "/app/docker-entrypoint.sh": self.context / "deploy/docker-entrypoint.sh",
                        "/app/container-healthcheck.sh": self.context / "deploy/container-healthcheck.sh"}
        files = {}
        for name, path in self.sources.items():
            raw = ("制品：" + name).encode()
            path.write_bytes(raw)
            path.chmod(0o755)
            files[name] = {"mode": "0755", "size": len(raw), "sha256": sha256_bytes(raw)}
        self.inspection = {"files": files}

    def inventory(self) -> tuple[dict, list]:
        return candidate._store_artifact_inventory(self.inspection, "a" * 64, campaign_dir=self.campaign,
                                                  candidate_id="fixture-candidate", binary_path=self.binary,
                                                  docker_context=self.context)

    def test_preview_is_zero_write_and_real_copies_have_data_root_coordinates(self) -> None:
        inventory, copies = self.inventory()
        self.assertFalse((self.campaign / "candidates").exists())
        self.assertEqual(inventory["inventory_sha256"], canonical_sha256(inventory["entries"]))
        for path, raw in copies:
            candidate._write_artifact_copy(path, raw, self.campaign)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            self.assertEqual(next(row for row in inventory["entries"] if row["role"] == path.name)["binding"]["path"], path.relative_to(self.data).as_posix())
        before = {path: path.stat().st_ino for path, _ in copies}
        self.assertEqual(self.inventory(), (inventory, copies))
        for path, raw in copies:
            candidate._write_artifact_copy(path, raw, self.campaign)
        self.assertEqual({path: path.stat().st_ino for path, _ in copies}, before)

    def test_existing_copy_byte_or_mode_drift_is_not_overwritten(self) -> None:
        _inventory, copies = self.inventory()
        path, raw = copies[0]
        candidate._write_artifact_copy(path, raw, self.campaign)
        path.chmod(0o600)
        with self.assertRaises(candidate.ConfigurationError):
            self.inventory()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o755)
        path.write_bytes(raw + b"changed")
        with self.assertRaises(candidate.ConfigurationError):
            candidate._write_artifact_copy(path, raw, self.campaign)
        self.assertEqual(path.read_bytes(), raw + b"changed")

    def test_source_bytes_permissions_and_inventory_closed_set_are_checked(self) -> None:
        self.binary.write_bytes(b"wrong")
        with self.assertRaises(candidate.ConfigurationError):
            self.inventory()
        self.assertFalse((self.campaign / "candidates").exists())
        self.binary.write_bytes(("制品：/app/sub2api").encode())
        self.binary.chmod(0o600)
        with self.assertRaises(candidate.ConfigurationError):
            self.inventory()
        self.binary.chmod(0o755)
        self.inspection["files"].pop("/app/sub2api")
        with self.assertRaises(candidate.ConfigurationError):
            self.inventory()

    def test_destination_symlink_is_rejected_without_writing_the_other_root(self) -> None:
        target = self.campaign / "candidates"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        target.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(candidate.ConfigurationError):
            self.inventory()
        self.assertEqual(list(elsewhere.iterdir()), [])


class SelectorProducerTests(unittest.TestCase):
    def test_store_snapshot_uses_explicit_data_root_and_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="claude-selector-root-", dir="/private/tmp") as directory:
            data = Path(directory) / "data"
            campaign = data / "evidence/campaigns/fixture"
            path = campaign / "inputs/active-release-catalog.json"
            path.parent.mkdir(parents=True)
            raw = b'{"selectors":{}}\n'
            path.write_bytes(raw)
            observed = {"source": {"path": "inputs/active-release-catalog.json", "sha256": sha256_bytes(raw), "bytes": len(raw)},
                        "observed_at_utc": "2026-10-08T00:00:00Z"}
            snapshot = capture.runtime_catalog_snapshot(observed, {}, campaign_dir=campaign)
            self.assertEqual(snapshot["source_ref"]["path"], "evidence/campaigns/fixture/inputs/active-release-catalog.json")
            self.assertEqual(observed["source"]["path"], "inputs/active-release-catalog.json")
            alternate = data / observed["source"]["path"]
            alternate.parent.mkdir()
            alternate.write_bytes(raw)
            path.unlink()
            with self.assertRaises(capture.ConfigurationError):
                capture.runtime_catalog_snapshot(observed, {}, campaign_dir=campaign)
            path.write_bytes(raw + b"wrong")
            with self.assertRaises(capture.ConfigurationError):
                capture.runtime_catalog_snapshot(observed, {}, campaign_dir=campaign)


if __name__ == "__main__":
    unittest.main()
