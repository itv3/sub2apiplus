"""守卫来源连续性的纯正负例；使用真实文件现场，不授正式来源资格。"""

from __future__ import annotations

import copy
from dataclasses import replace
import unittest

from tools.official_client_capture import claude_upgrade_guard_epochs as epochs
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests import test_claude_guard_epoch_readiness as readiness_tests
from tools.official_client_control.errors import ControlError


class ContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        readiness_tests.ReadinessTests.setUpClass()

    def setUp(self):
        self.fixture = readiness_tests.ReadinessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = self.fixture.case
        self.engine = self.fixture.engine
        _ready, self.ready_binding = self.fixture.readiness()
        self.operation_id = self.fixture.operation("continuity-first")
        path = self.fixture.root / "guard" / "operations" / self.operation_id / "evidence-000001.json"
        self.core = guard.read_json(path)
        self.origin = self.core.pop("origin")
        self.envelope = {
            "schema_version": epochs.CONTINUITY_SCHEMA,
            "source_service_instance_id": self.origin["service_instance_id"],
            "channel_service_instance_id": self.case.identities[-1]["service_instance_id"],
            "stable_deployment_identity_sha256": self.fixture.registered.stable_deployment_identity_sha256,
            "epoch_bindings": self.case.bindings, "readiness_bindings": [self.ready_binding],
            "source_record_binding": guard.binding(self.fixture.root, path), "source_json_pointer": "",
            "current_operations_index_binding": self.engine._snapshot_index(),
        }

    def validate(self, envelope=None, *, core=None, origin=None, purpose="formed_replay"):
        return epochs.validate_guard_continuity(
            self.case.configuration, self.case.context,
            self.envelope if envelope is None else envelope, self.case.read_raw,
            source_core=self.core if core is None else core, source_origin=self.origin if origin is None else origin,
            channel_identity=self.case.identities[-1], purpose=purpose,
        )

    def test_original_stage_content_and_producer_remain_unchanged(self):
        before = copy.deepcopy(self.core)
        result = self.validate(purpose="current_execution")
        self.assertEqual(self.core, before)
        self.assertEqual(result.source_service_instance_id, self.origin["service_instance_id"])
        self.assertIn(result.source_record_binding, result.read_bindings)
        self.assertTrue(epochs.same_verified_guard_deployment(result, self.validate()))

    def test_original_freeze_pointer_matches_exact_snapshot(self):
        snapshot = self.engine.inspect({"scope": "continuity-freeze"})
        path = next((self.engine.root / "freezes").rglob("snapshot.json"))
        source = dict(snapshot["cores"]["configuration"])
        origin = source.pop("origin")
        envelope = {**self.envelope, "source_record_binding": guard.binding(self.fixture.root, path),
                    "source_json_pointer": "/cores/configuration",
                    "current_operations_index_binding": self.engine._snapshot_index()}
        result = self.validate(envelope, core=source, origin=origin)
        self.assertEqual(result.source_service_instance_id, origin["service_instance_id"])

    def test_derived_operation_index_only_uses_current_verified_channel(self):
        origin = self.engine._origin()
        core = {"campaign_id": self.case.context["campaign_id"], "delivery_id": self.case.context["delivery_id"],
                "operation_index_binding": origin["operations_index_binding"]}
        envelope = {**self.envelope, "source_record_binding": origin["operations_index_binding"],
                    "source_json_pointer": "", "current_operations_index_binding": origin["operations_index_binding"]}
        self.validate(envelope, core=core, origin=origin)
        old_origin = {**origin, "service_instance_id": self.case.identities[0]["service_instance_id"]}
        changed = {**envelope, "source_service_instance_id": old_origin["service_instance_id"]}
        with self.assertRaises(ControlError):
            self.validate(changed, core=core, origin=old_origin)

    def test_same_sha_copy_at_an_unlisted_stage_path_is_rejected(self):
        original = self.envelope["source_record_binding"]
        copied = {**original, "path": original["path"].replace("evidence-000001", "evidence-000002")}
        self.case.raw_by_path[copied["path"]] = guard.read_bytes(self.fixture.root / original["path"])
        with self.assertRaisesRegex(ControlError, "原来源"):
            self.validate({**self.envelope, "source_record_binding": copied})

    def test_missing_current_startup_readiness_is_rejected(self):
        with self.assertRaisesRegex(ControlError, "启动完整恢复"):
            self.validate({**self.envelope, "readiness_bindings": []})

    def test_pass_with_an_old_producer_missing_ready_is_not_inherited(self):
        identity = self.case.identities[0]
        origin = {**self.origin, "service_instance_id": identity["service_instance_id"]}
        core = {**self.core, "guard_identity": identity}
        binding = self.fixture.put(self.envelope["source_record_binding"]["path"], {**core, "origin": origin})
        envelope = {**self.envelope, "source_service_instance_id": identity["service_instance_id"],
                    "source_record_binding": binding}
        with self.assertRaisesRegex(ControlError, "原通过来源缺"):
            self.validate(envelope, core=core, origin=origin)

    def test_source_channel_stable_context_and_contract_drift_are_rejected(self):
        for field, value in (("source_service_instance_id", "a" * 64),
                             ("channel_service_instance_id", "a" * 64),
                             ("stable_deployment_identity_sha256", "a" * 64),
                             ("schema_version", "unregistered/v1")):
            with self.subTest(field=field), self.assertRaises(ControlError):
                self.validate({**self.envelope, field: value})

    def test_uploaded_core_and_extra_envelope_field_cannot_grant_source_authority(self):
        with self.assertRaises(ControlError):
            self.validate(core={**self.core, "run_nonce": "copied-source"})
        with self.assertRaisesRegex(ControlError, "字段不闭合"):
            self.validate({**self.envelope, "ready": True})

    def test_unknown_pointer_purpose_and_json_origin_fields_are_rejected(self):
        with self.assertRaises(ControlError):
            self.validate({**self.envelope, "source_json_pointer": "/cores/configuration/anything"})
        with self.assertRaises(ControlError):
            self.validate(purpose="implicit-upgrade")
        with self.assertRaises(ControlError):
            self.validate(origin={**self.origin, "current_authority": True})

    def test_same_deployment_relation_retains_current_channel_and_complete_registration_chain(self):
        result = self.validate()
        self.assertFalse(epochs.same_verified_guard_deployment(result, {}))
        self.assertFalse(epochs.same_verified_guard_deployment(result, replace(result, channel_service_instance_id="a" * 64)))
        self.assertFalse(epochs.same_verified_guard_deployment(result, replace(result, epoch_bindings=result.epoch_bindings[1:])))


if __name__ == "__main__":
    unittest.main()
