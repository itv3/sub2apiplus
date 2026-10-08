"""启动恢复纯合同回归：真实文件现场与原 guard 索引，不授现场来源资格。"""

from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tools.official_client_capture import claude_upgrade_guard_epochs as epochs
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests import test_claude_guard_epochs as epoch_tests
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import FileRuntime
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        epoch_tests.RegistrationChainTests.setUpClass()

    def setUp(self):
        self.case = epoch_tests.RegistrationChainTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.configuration, self.context = self.case.configuration, self.case.context
        self.root = self.case.root
        # 守卫实例绑定本测试真实进程；原注册链两项仍保留不同旧进程身份。
        actual = guard.process_identity(os.getpid())
        identity = {**self.case.identities[-1], **actual}
        identity["service_instance_id"] = canonical_sha256({
            "configuration": canonical_sha256(self.configuration), "process": actual,
        })
        self.case.identities[-1] = identity
        document = {**self.case.documents[-1], "guard_identity": identity}
        self.case.bindings[-1] = self.case.store_registration(3, document)
        self.registered = self.case.validate()
        self.engine = guard.GuardEngine(
            self.configuration, self.context, FileRuntime(self.configuration, self.context), identity,
        )
        self.addCleanup(self.engine.close)
        self.worker = subprocess.Popen(
            [sys.executable, "-B", "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self.close_worker)
        self.assertEqual(self.worker.stdout.readline(), b"ready\n")
        self.owner = guard.process_identity(self.worker.pid)

    def close_worker(self):
        if self.worker.poll() is None:
            self.worker.terminate()
        self.worker.communicate(timeout=5)

    def put(self, path, value):
        raw = canonical_json_bytes(value)
        self.case.raw_by_path[path] = raw
        return {"path": path, "sha256": sha256_bytes(raw), "bytes": len(raw)}

    def operation(self, nonce, stage="candidate_active"):
        reference = {"object_kind": "operational_evidence", "sha256": "a" * 64}
        request = {
            "stage": stage, "run_nonce": nonce,
            "plan_ref": {"object_kind": "candidate_delivery_plan", "sha256": "b" * 64},
            "configuration_ref": reference, "stable_projection_ref": reference,
            "probe_recipe_ref": reference,
            "environment": guard.read_json(self.root / "state.json")["environment"],
        }
        ticket = self.engine.prepare(request, self.owner)
        observation = self.engine.run(ticket)
        self.assertEqual(observation["result"], "pass")
        self.engine.finish(ticket, {
            "result": "pass", "observation_sha256": canonical_sha256(observation),
            "verifier_binding": self.configuration["guard"]["executor_binding"], "reason": "本机文件现场专项",
        })
        return ticket["operation_id"]

    def readiness(self):
        index_binding = self.engine._snapshot_index()
        index = guard.read_json(self.root / index_binding["path"])
        rows = []
        for item in index["operations"]:
            number = Path(item["terminal_binding"]["path"]).stem
            prefix = self.root / "guard" / "operations" / item["operation_id"] / ("journal-prefix-" + number + ".json")
            rows.append({key: item[key] for key in ("operation_id", "prepared_binding", "terminal_binding", "status")}
                        | {"journal_prefix_binding": guard.binding(self.root, prefix)})
        value = {
            "schema_version": epochs.READINESS_SCHEMA, "epoch_binding": self.case.bindings[-1],
            "recovered_operations_index_binding": index_binding, "operations": rows, "result": "pass",
        }
        return value, self.put("guard/epochs/000003/readiness.json", value)

    def validate(self, binding):
        return epochs.validate_epoch_readiness(
            self.configuration, self.context, self.registered, binding, self.case.read_raw,
        )

    def test_empty_real_index_can_complete_startup_but_does_not_grant_later_admission(self):
        _document, binding = self.readiness()
        result = self.validate(binding)
        self.assertEqual(result.operation_ids, ())
        self.assertFalse(hasattr(result, "current_execution_allowed"))

    def test_two_actual_commits_use_complete_guard_index_and_journal_originals(self):
        ids = sorted([self.operation("first"), self.operation("second", "rollback_verified")])
        _document, binding = self.readiness()
        result = self.validate(binding)
        self.assertEqual(list(result.operation_ids), ids)
        self.assertEqual(result.service_instance_id, self.case.identities[-1]["service_instance_id"])
        self.assertGreater(len(result.read_bindings), 15)

    def test_one_missing_or_duplicated_operation_rejects_the_whole_startup_proof(self):
        self.operation("first")
        self.operation("second")
        original, _binding = self.readiness()
        for rows in (original["operations"][:-1], original["operations"] * 2, original["operations"][::-1]):
            with self.subTest(rows=rows):
                binding = self.put("guard/epochs/000003/readiness.json", {**original, "operations": rows})
                with self.assertRaises(ControlError):
                    self.validate(binding)

    def test_only_matching_registered_epoch_is_accepted(self):
        original, _binding = self.readiness()
        for path, epoch in (("guard/readiness.json", self.case.bindings[-1]),
                            ("guard/epochs/000003/readiness.json", self.case.bindings[0])):
            binding = self.put(path, {**original, "epoch_binding": epoch})
            with self.subTest(path=path), self.assertRaises(ControlError):
                self.validate(binding)

    def test_failed_unknown_and_extra_ready_fields_are_rejected(self):
        original, _binding = self.readiness()
        for value in ({**original, "result": "failed"}, {**original, "ready": True},
                      {**original, "operations": None}):
            binding = self.put("guard/epochs/000003/readiness.json", value)
            with self.subTest(value=value), self.assertRaises(ControlError):
                self.validate(binding)

    def test_terminal_and_journal_must_use_the_original_exact_coordinates(self):
        self.operation("first")
        original, _binding = self.readiness()
        for field in ("prepared_binding", "terminal_binding", "journal_prefix_binding"):
            changed = copy.deepcopy(original)
            changed["operations"][0][field]["path"] = "guard/copy.json"
            binding = self.put("guard/epochs/000003/readiness.json", changed)
            with self.subTest(field=field), self.assertRaises(ControlError):
                self.validate(binding)

    def test_one_damaged_original_blocks_complete_readiness(self):
        self.operation("first")
        document, binding = self.readiness()
        path = document["operations"][0]["prepared_binding"]["path"]
        self.case.raw_by_path[path] = guard.read_bytes(self.root / path) + b" "
        with self.assertRaisesRegex(ControlError, "字节漂移"):
            self.validate(binding)

    def test_old_ready_is_only_a_startup_boundary_not_the_current_operation_index(self):
        self.operation("first")
        _document, binding = self.readiness()
        old = self.validate(binding)
        self.operation("later")
        current = self.engine._snapshot_index()
        self.assertNotEqual(current, old.recovered_operations_index_binding.as_value())
        self.assertEqual(self.validate(binding), old)

    def test_unknown_readiness_operation_fields_cannot_hide_failed_operations(self):
        self.operation("first")
        original, _binding = self.readiness()
        changed = copy.deepcopy(original)
        changed["operations"][0]["skip_restore"] = True
        binding = self.put("guard/epochs/000003/readiness.json", changed)
        with self.assertRaisesRegex(ControlError, "字段不闭合"):
            self.validate(binding)


if __name__ == "__main__":
    unittest.main()
