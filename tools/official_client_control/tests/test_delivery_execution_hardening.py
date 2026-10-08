"""真实交付合同的嵌套闭集、回退资格子图和独立恢复原件专项验证。"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

SHA = "a" * 64
IMAGE = "sha256:" + "b" * 64
BINDING = {"path": "private/source.json", "sha256": SHA, "bytes": 1}
ORIGIN = {"authority": "local-hardening-fixture", "source_scope": "regression_fixture", "configuration_sha256": SHA, "service_instance_id": "guard-1", "operations_index_binding": None}
PERSONA = {"provider": "fixture", "official_product": "claude", "auth_family": "oauth", "upstream_route_family": "messages"}
ENVIRONMENT = {"host_id": "ARM64", "architecture": "arm64", **{key: SHA for key in execution.ENVIRONMENT_FIELDS - {"host_id", "architecture"}}}


def guard_identity():
    return {"contract_version": "claude-upgrade-delivery-guard-identity/v1", "authority": ORIGIN["authority"], "source_scope": ORIGIN["source_scope"], "host_id": "ARM64", "instance_id": "instance-1", "service_name": "guard-1", "service_instance_id": ORIGIN["service_instance_id"], "boot_id": "boot-1", "pid": 1, "process_start": "start-1", "process_group_id": 2, "independent_service": True, **{key: deepcopy(BINDING) for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding")}}


def checks(names):
    return [{"id": name, "result": "pass", "facts": {}} for name in sorted(names)]


def payload(kind, additions):
    facts = {"contract_version": f"claude-delivery-{kind.replace('_', '-')}/v1", "result": "pass", "campaign_id": "campaign-1", "delivery_id": "delivery-1", "origin": deepcopy(ORIGIN), **additions}
    return {"schema_version": execution.OPERATIONAL_V2, "persona": PERSONA, "manifest_id": "evidence-1", "evidence_type": kind, "entries": [{"id": kind, "facts": facts}]}


def shape_payloads():
    configuration = {"value": "frozen"}
    projection = {"schema_version": "claude-upgrade-stable-environment-projection/v1", "application_service": "sub2api", "compose": {}, "containers": {}}
    fact_ref = {"campaign_id": "campaign-1", "dimension": "validation", "sequence": 1, "sha256": SHA}
    ref = {"object_kind": "operational_evidence", "sha256": SHA}
    target = {"image_digest": IMAGE, "runtime_profile_sha256": SHA}
    actual = {key: {"state": "unknown", "reason": "实测不可用"} for key in ("image", "profile")}
    recipe = {"allowed_requests": [{"method": "GET", "path": "/v1/models"}]}
    return {
        "configuration": payload("configuration", {"configuration": configuration, "configuration_sha256": canonical_sha256(configuration), "binding": BINDING}),
        "stable_projection": payload("stable_projection", {"projection": projection, "projection_sha256": canonical_sha256(projection), "binding": BINDING}),
        "freeze": payload("freeze", {"environment": ENVIRONMENT, "before_bindings": [BINDING], "gates": checks(execution.FREEZE_GATES), "guard_identity": guard_identity()}),
        "rollback_state": payload("rollback_state", {"candidate_ref": fact_ref, "acceptance_ref": fact_ref, "build_receipt_ref": {"receipt_kind": "candidate_build", "sha256": SHA}, "evidence_package_ref": {"object_kind": "candidate_evidence_package", "sha256": SHA}, "image_digest": IMAGE, "runtime_profile_sha256": SHA, "bindings": [BINDING]}),
        "zero_model_probes": payload("zero_model_probes", {"mode": "recipe", "recipe": recipe, "request_records": [], "bindings": [BINDING]}),
        "deployment_configuration": payload("deployment_configuration", {"configuration_binding": BINDING, "deployment_sha256": SHA, "bindings": [BINDING]}),
        "stage_execution": payload("stage_execution", {"candidate_id": "candidate-1", "stage": "candidate_active", "run_nonce": "nonce-1", "operation_id": "operation-1", "plan_ref": {"object_kind": "candidate_delivery_plan", "sha256": SHA}, "configuration_ref": ref, "stable_projection_ref": ref, "probe_recipe_ref": ref, "target": target, "actual": actual, "environment": ENVIRONMENT, "checks": checks(["health"]), "observed_seconds": 0, "live_request_count": 0, "request_records": [], "guard_identity": guard_identity(), "prepared_binding": BINDING, "physical_intent_binding": None, "guard_terminal_binding": BINDING, "journal_binding": BINDING, "before_bindings": [], "during_bindings": [], "after_bindings": [], "restoration": {"state": "not_required", "bindings": [], "reason": "无需恢复"}}),
    }


class OperationalShapeTests(unittest.TestCase):
    def test_typed_required_values_reject_null(self):
        for kind, field in (("configuration", "binding"), ("stable_projection", "binding"), ("freeze", "guard_identity"), ("rollback_state", "build_receipt_ref"), ("zero_model_probes", "recipe"), ("deployment_configuration", "configuration_binding")):
            with self.subTest(kind=kind, field=field):
                value = deepcopy(shape_payloads()[kind])
                execution.validate_operational_evidence(value)
                value["entries"][0]["facts"][field] = None
                with self.assertRaises(ControlError):
                    execution.validate_operational_evidence(value)

    def test_environment_guard_and_restoration_reject_extra_fields(self):
        for key in ("environment", "guard_identity", "restoration"):
            with self.subTest(key=key):
                value = deepcopy(shape_payloads()["stage_execution"])
                value["entries"][0]["facts"][key]["undeclared"] = True
                with self.assertRaisesRegex(ControlError, "字段不闭合"):
                    execution.validate_operational_evidence(value)

    def test_only_failed_stage_can_record_unknown_environment(self):
        value = deepcopy(shape_payloads()["stage_execution"])
        facts = value["entries"][0]["facts"]
        facts.update(result="failed", environment=None)
        execution.validate_operational_evidence(value)
        facts["result"] = "pass"
        with self.assertRaisesRegex(ControlError, "实测环境未知"):
            execution.validate_operational_evidence(value)

    def test_failed_stage_preserves_real_environment_drift(self):
        value = deepcopy(shape_payloads()["stage_execution"])
        facts = value["entries"][0]["facts"]
        facts["result"] = "failed"
        facts["environment"]["host_id"] = "unexpected-host"
        facts["environment"]["architecture"] = "linux/amd64"
        execution.validate_operational_evidence(value)
        self.assertEqual(facts["environment"]["host_id"], "unexpected-host")

    def test_schema_uses_fact_and_receipt_references_and_closed_typed_fields(self):
        schema = json.loads(Path(execution.__file__).with_name("schemas").joinpath("delivery-execution.schema.json").read_bytes())
        by_kind = {row["properties"]["evidence_type"]["const"]: row["properties"]["entries"]["items"]["properties"]["facts"] for row in schema["oneOf"]}
        rollback = by_kind["rollback_state"]["properties"]
        self.assertEqual(set(rollback["candidate_ref"]["required"]), {"campaign_id", "dimension", "sequence", "sha256"})
        self.assertEqual(rollback["build_receipt_ref"]["properties"]["receipt_kind"], {"const": "candidate_build"})
        stage = by_kind["stage_execution"]
        for key in ("guard_identity", "restoration"):
            self.assertFalse(stage["properties"][key]["additionalProperties"])
        self.assertFalse(stage["properties"]["environment"]["oneOf"][0]["additionalProperties"])
        self.assertTrue(stage["allOf"])


class RestoreSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.originals = {}
        def verify(store, content, origin):
            return execution.VerifiedSource(canonical_sha256(content), SHA, self.originals, origin["configuration_sha256"], origin["service_instance_id"])
        self.scope = mock.patch.dict(execution.SOURCE_VERIFIERS, {ORIGIN["authority"]: verify})
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.plan = {"campaign_id": "campaign-1", "delivery_id": "delivery-1", "rollback_image_digest": IMAGE, "rollback_runtime_profile_sha256": SHA, "configuration_sha256": SHA}

    def bind(self, path, value):
        raw = canonical_json_bytes(value)
        self.originals[path] = raw
        return {"path": path, "sha256": sha256_bytes(raw), "bytes": len(raw)}

    def restore_payload(self, change=None):
        target = {"image_digest": IMAGE, "runtime_profile_sha256": SHA}
        actual = {"image": {"state": "known", "value": IMAGE, "direct_binding": self.bind("private/image.json", {"image_digest": IMAGE})}, "profile": {"state": "known", "value": SHA, "direct_binding": self.bind("private/profile.json", {"runtime_profile_sha256": SHA})}}
        observed = {"target": target, "actual": actual, "checks": checks(execution.RESTORE_CHECKS), "bindings": [], "result": "pass"}
        if change:
            change(observed)
        result_binding = self.bind("private/result.json", observed)
        terminal = {"schema_version": "claude-upgrade-delivery-guard-terminal/v1", "operation_id": "operation-1", "status": "restored", "prepared_sha256": SHA, "physical_intent_sha256": None, "physical_intent_binding": None, "restoration": {"state": "verified", "bindings": [result_binding], "reason": "回退验证通过"}, "guard_identity": guard_identity()}
        self.originals[BINDING["path"]] = b"x"
        # 测试来源中的部署／服务原件由实际字节建立，不用上传摘要替代通道绑定。
        for key in ("service_unit_binding", "executor_binding", "guard_binding"):
            terminal["guard_identity"][key] = self.bind(f"private/{key}.json", {"fixture": key})
        terminal["guard_identity"]["deployment_receipt_binding"] = self.bind("private/deployment.json", {"fixture": "deployment"})
        terminal_binding = self.bind("private/terminal.json", terminal)
        record = {"schema_version": "claude-upgrade-delivery-journal/v1", "operation_id": "operation-1", "sequence": 1, "event": "restored", "payload": {"terminal_binding": terminal_binding}, "previous_sha256": None}
        record["record_sha256"] = canonical_sha256(record)
        journal_binding = self.bind("private/journal.json", {"schema_version": "claude-upgrade-delivery-journal/v1", "operation_id": "operation-1", "records": [record]})
        facts = {"operation_id": "operation-1", **{key: observed[key] for key in ("target", "actual", "checks")}, "journal_binding": journal_binding, "terminal_binding": terminal_binding, "bindings": [result_binding]}
        value = payload("restore", facts)
        deployment_sha = terminal["guard_identity"]["deployment_receipt_binding"]["sha256"]
        def verify(store, content, origin):
            return execution.VerifiedSource(canonical_sha256(content), deployment_sha, self.originals, origin["configuration_sha256"], origin["service_instance_id"])
        execution.SOURCE_VERIFIERS[ORIGIN["authority"]] = verify
        return value

    def rebind(self, value, key, change):
        facts = value["entries"][0]["facts"]
        old = facts[key]
        content = json.loads(self.originals[old["path"]])
        change(content)
        facts[key] = self.bind(old["path"], content)

    def test_valid_restore_requires_actual_identity_and_five_checks(self):
        value = self.restore_payload()
        self.assertEqual(execution.validate_restore_evidence(None, self.plan, value)["result"], "pass")

    def test_unknown_actual_failed_check_and_wrong_target_are_rejected(self):
        for change in (lambda v: v["actual"].update(image={"state": "unknown", "reason": "镜像不可读"}), lambda v: v["checks"][0].update(result="failed"), lambda v: v["target"].update(image_digest="sha256:" + "c" * 64)):
            with self.subTest(change=change):
                value = self.restore_payload(change)
                with self.assertRaises(ControlError):
                    execution.validate_restore_evidence(None, self.plan, value)

    def test_wrong_terminal_and_journal_break_are_rejected(self):
        for key, change in (("terminal_binding", lambda v: v.update(status="restore_failed")), ("journal_binding", lambda v: v["records"][0].update(previous_sha256=SHA))):
            with self.subTest(key=key):
                value = self.restore_payload()
                self.rebind(value, key, change)
                with self.assertRaises(ControlError):
                    execution.validate_restore_evidence(None, self.plan, value)

    def test_rehashed_journal_cannot_bind_another_terminal(self):
        value = self.restore_payload()
        def change(journal):
            record = journal["records"][0]
            record["payload"] = {"terminal_binding": BINDING}
            record["record_sha256"] = canonical_sha256({k: v for k, v in record.items() if k != "record_sha256"})
        self.rebind(value, "journal_binding", change)
        with self.assertRaisesRegex(ControlError, "相同 guard 终态"):
            execution.validate_restore_evidence(None, self.plan, value)


class RollbackQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.tree = ManagedToolTree(cls.root / "repo")
        cls.active = cls.tree.active()
        cls.active.__enter__()
        (cls.root / "campaign").mkdir()
        cls.fixture = SyntheticCampaign(cls.root / "campaign", schema="v2")
        f = cls.fixture
        f.bootstrap_and_campaign()
        f.discovery_and_evidence()
        f.profile_objects()
        f.profile_approve()
        f.strict_validate()
        cls.qualification = {"store_relative": "store", "external_root_relative": "external", "campaign_id": f.campaign_id, "candidate_id": "synthetic-candidate-strict", "candidate_ref": f.references["candidate-strict"], "build_receipt_ref": f.references["candidate-build-receipt"], "validation_ref": f.references["validation-completed"], "acceptance_ref": f.references["acceptance-strict"], "sealed_result_binding": f._write_external("private/sealed.json", canonical_json_bytes({"schema_version": "claude-upgrade-acceptance/v1", "candidate_id": "synthetic-candidate-strict", "production_state": "not_activated", "store": {"acceptance_ref": f.references["acceptance-strict"], "validation_completed_ref": f.references["validation-completed"]}}))}
        candidate = f.store.load_fact(f.references["candidate-strict"])["payload"]
        cls.plan = {"candidate_id": "another-candidate", "architecture": candidate["target_architecture"]}
        cls.rollback = {"candidate_ref": cls.qualification["candidate_ref"], "build_receipt_ref": cls.qualification["build_receipt_ref"], "acceptance_ref": cls.qualification["acceptance_ref"], "evidence_package_ref": f.references["candidate-evidence-package"], "image_digest": candidate["image_digest"], "runtime_profile_sha256": f.store.load_receipt(cls.qualification["build_receipt_ref"])["profile_identity"]["profile_digest"]}
        binding = cls.qualification["sealed_result_binding"]
        cls.source = execution.VerifiedSource(SHA, SHA, {binding["path"]: (f.external_root / binding["path"]).read_bytes()}, SHA, "guard-1", f.root)

    @classmethod
    def tearDownClass(cls):
        cls.active.__exit__(None, None, None)
        cls.temporary.cleanup()

    def verify(self, qualification=None, rollback=None, source=None):
        return execution._validate_rollback_qualification(self.fixture.store, self.plan, {"rollback": {"qualification": qualification or self.qualification}}, rollback or self.rollback, source or self.source)

    def test_full_qualification_replays_reachable_graph_without_store_replay(self):
        with mock.patch.object(self.fixture.store, "replay", side_effect=AssertionError("禁止反向执行整个交付图")):
            self.verify()

    def test_qualification_can_bind_the_trusted_external_root_itself(self):
        qualification = deepcopy(self.qualification)
        qualification["external_root_relative"] = "."
        source = execution.VerifiedSource(SHA, SHA, self.source.bytes_by_path, SHA,
                                          "guard-1", self.fixture.external_root)
        # 使用同一真实 Store／资格子图和原件；不复制材料，也不跳过子图语义重放。
        self.verify(qualification=qualification, source=source)

    def test_root_coordinate_exception_does_not_allow_file_or_traversal_paths(self):
        from tools.official_client_control.canonical import resolve_relative

        with self.assertRaises(ControlError):
            resolve_relative(self.fixture.external_root, ".")
        for relative in ("", "./", "external/..", "../external", "/external"):
            qualification = deepcopy(self.qualification)
            qualification["external_root_relative"] = relative
            with self.subTest(relative=relative), self.assertRaises(ControlError):
                self.verify(qualification=qualification)

    def test_wrong_build_evidence_package_and_validation_are_rejected(self):
        for field, reference in (("build_receipt_ref", {"receipt_kind": "candidate_build", "sha256": "c" * 64}), ("evidence_package_ref", {"object_kind": "candidate_evidence_package", "sha256": "c" * 64}), ("acceptance_ref", self.qualification["validation_ref"])):
            with self.subTest(field=field):
                rollback = deepcopy(self.rollback)
                rollback[field] = reference
                with self.assertRaises(ControlError):
                    self.verify(rollback=rollback)
        qualification = deepcopy(self.qualification)
        qualification["validation_ref"] = qualification["acceptance_ref"]
        with self.assertRaises(ControlError):
            self.verify(qualification=qualification)

    def test_changed_sealed_result_is_rejected(self):
        binding = self.qualification["sealed_result_binding"]
        originals = dict(self.source.bytes_by_path)
        originals[binding["path"]] += b"tampered"
        source = execution.VerifiedSource(SHA, SHA, originals, SHA, "guard-1", self.fixture.root)
        with self.assertRaises(ControlError):
            self.verify(source=source)


if __name__ == "__main__":
    unittest.main()
