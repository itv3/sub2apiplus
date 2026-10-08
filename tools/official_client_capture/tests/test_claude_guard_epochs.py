"""注册链的纯合同回归；真实查询两个子进程，仍不授来源或 readiness。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.official_client_capture import claude_upgrade_guard_epochs as epochs
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError


class RegistrationChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只查询进程身份，不启动守卫、Docker、产品或任何网络连接。
        program = (
            "import json,os; from tools.official_client_capture "
            "import claude_upgrade_delivery_recovery_guard as g; "
            "print(json.dumps(g.process_identity(os.getpid())))"
        )
        cls.processes = [json.loads(subprocess.check_output(
            [sys.executable, "-B", "-c", program], text=True,
        )) for _ in range(3)]

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="guard-epoch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.configuration, self.context, _ = fixture_world(self.root)
        self.identities = []
        for process in self.processes:
            value = {
                "contract_version": epochs.IDENTITY_SCHEMA, "authority": "claude-arm64-recovery-guard",
                "source_scope": "regression_fixture", "host_id": "ARM64",
                "instance_id": self.configuration["instance_id"],
                "service_name": self.configuration["guard"]["service_name"],
                "service_instance_id": canonical_sha256({"configuration": canonical_sha256(self.configuration), "process": process}),
                "independent_service": False, **process,
                **{key: self.configuration["guard"][key] for key in epochs.DEPLOYMENT_FIELDS},
            }
            self.identities.append(value)
        self.documents = []
        self.bindings = []
        self.raw_by_path = {}
        stable = epochs.stable_deployment_identity(self.configuration, self.context, self.identities[-1])
        for number, identity in enumerate(self.identities, 1):
            value = {
                "schema_version": epochs.EPOCH_SCHEMA, "epoch_sequence": number,
                "configuration_binding": self.context["configuration_ref"],
                "configuration_sha256": canonical_sha256(self.configuration),
                "context": stable["context"], "stable_deployment_identity_sha256": canonical_sha256(stable),
                "guard_identity": copy.deepcopy(identity),
                "previous_epoch_binding": self.bindings[-1] if self.bindings else None,
            }
            self.documents.append(value)
            self.bindings.append(self.store_registration(number, value))

    def store_registration(self, number, document):
        relative = f"guard/epochs/{number:06d}/registration.json"
        raw = canonical_json_bytes(document)
        # 负例的上传内容在独立字节映射中准备，既有文件原件不覆盖。
        self.raw_by_path[relative] = raw
        return {"path": relative, "sha256": sha256_bytes(raw), "bytes": len(raw)}

    def read_raw(self, binding):
        relative = binding["path"]
        if relative in self.raw_by_path:
            return self.raw_by_path[relative]
        return guard.read_bytes(guard.check_binding(self.root, binding))

    def validate(self, bindings=None, identity=None):
        return epochs.validate_registration_chain(
            self.configuration, self.context, self.bindings if bindings is None else bindings,
            self.read_raw, channel_identity=self.identities[-1] if identity is None else identity,
        )

    def test_three_actual_process_instances_remain_distinct_without_ready_authority(self):
        result = self.validate()
        self.assertEqual(len(set(result.ordered_service_instance_ids)), 3)
        self.assertEqual(result.channel_service_instance_id, self.identities[-1]["service_instance_id"])
        self.assertEqual(len(result.read_bindings), 8)
        self.assertFalse(hasattr(result, "ready"))
        self.assertFalse(hasattr(result, "current_execution_allowed"))

    def test_process_and_segment_changes_do_not_change_stable_deployment(self):
        first = epochs.stable_deployment_identity(self.configuration, self.context, self.identities[0])
        changed_context = {**self.context, "segment": "dr1", "run_nonce": "another-run"}
        second = epochs.stable_deployment_identity(self.configuration, changed_context, self.identities[-1])
        self.assertEqual(first, second)

    def test_missing_or_reordered_middle_epoch_is_rejected(self):
        for rows in ([self.bindings[0], self.bindings[2]], self.bindings[::-1]):
            with self.subTest(rows=rows), self.assertRaises(ControlError):
                self.validate(rows)

    def test_empty_chain_is_rejected(self):
        with self.assertRaises(ControlError):
            self.validate([])

    def test_unknown_registration_field_is_rejected(self):
        document = {**self.documents[-1], "ready": True}
        rows = [*self.bindings[:-1], self.store_registration(3, document)]
        with self.assertRaisesRegex(ControlError, "字段不闭合"):
            self.validate(rows)

    def test_wrong_previous_binding_is_rejected(self):
        document = {**self.documents[-1], "previous_epoch_binding": self.bindings[0]}
        rows = [*self.bindings[:-1], self.store_registration(3, document)]
        with self.assertRaisesRegex(ControlError, "前序链"):
            self.validate(rows)

    def test_duplicate_process_registration_is_rejected(self):
        document = {**self.documents[-1], "guard_identity": self.identities[0]}
        rows = [*self.bindings[:-1], self.store_registration(3, document)]
        with self.assertRaisesRegex(ControlError, "重复登记"):
            self.validate(rows)

    def test_changed_context_is_rejected_even_with_recomputed_stable_digest(self):
        document = copy.deepcopy(self.documents[-1])
        document["context"]["delivery_id"] = "candidate-current-d1"
        document["stable_deployment_identity_sha256"] = canonical_sha256(epochs.stable_deployment_identity(
            self.configuration, document["context"], self.identities[-1],
        ))
        rows = [*self.bindings[:-1], self.store_registration(3, document)]
        with self.assertRaisesRegex(ControlError, "上下文漂移"):
            self.validate(rows)

    def test_wrong_current_channel_is_rejected(self):
        with self.assertRaisesRegex(ControlError, "当前通道"):
            self.validate(identity=self.identities[0])

    def test_pid_or_start_rebinding_cannot_keep_old_instance_id(self):
        for field, value in (("pid", self.identities[-1]["pid"] + 1), ("process_start", "different-start"), ("boot_id", "another-boot")):
            identity = {**self.identities[-1], field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ControlError, "实例摘要"):
                self.validate(identity=identity)

    def test_deployment_binding_change_is_rejected(self):
        identity = copy.deepcopy(self.identities[-1])
        identity["guard_binding"]["sha256"] = "1" * 64
        with self.assertRaisesRegex(ControlError, "部署绑定"):
            self.validate(identity=identity)

    def test_non_arm64_or_production_configuration_is_rejected(self):
        for field, value in (("host_id", "DMIT"), ("client", "codex"), ("isolation", "production")):
            configuration = {**self.configuration, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ControlError, "隔离候选"):
                epochs.stable_deployment_identity(configuration, self.context, self.identities[-1])

    def test_missing_or_damaged_original_is_rejected(self):
        self.raw_by_path[self.bindings[-1]["path"]] += b" "
        with self.assertRaisesRegex(ControlError, "原件缺失或字节漂移"):
            self.validate()

    def test_wrong_parent_path_and_boolean_sequence_are_rejected(self):
        for mutate in ("path", "sequence"):
            rows = copy.deepcopy(self.bindings)
            if mutate == "path":
                rows[-1]["path"] = "guard/epochs/000003/copy.json"
                self.raw_by_path[rows[-1]["path"]] = self.raw_by_path[self.bindings[-1]["path"]]
            else:
                document = {**self.documents[-1], "epoch_sequence": True}
                rows[-1] = self.store_registration(3, document)
            with self.subTest(mutate=mutate), self.assertRaises(ControlError):
                self.validate(rows)

    def test_result_does_not_keep_mutable_input_bindings(self):
        result = self.validate()
        original = result.epoch_bindings[0].path
        self.bindings[0]["path"] = "guard/unlisted.json"
        self.assertEqual(result.epoch_bindings[0].path, original)


if __name__ == "__main__":
    unittest.main()
