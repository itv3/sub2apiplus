"""实际服务代际接线回归；真实子进程与文件现场，不登记正式来源。"""

from __future__ import annotations

import base64
import copy
import shutil
import unittest

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests import test_claude_upgrade_arm64_delivery_executor as fixtures
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError


class GuardEpochWiringTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.GuardTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.engine, self.root = self.case.engine, self.case.root

    def committed(self):
        ticket = self.engine.prepare(fixtures.prepare_request(self.case.environment), self.case.owner)
        observed = self.engine.run(ticket)
        return self.engine.finish(ticket, {
            "result": "pass", "observation_sha256": canonical_sha256(observed),
            "verifier_binding": self.case.configuration["guard"]["executor_binding"],
            "reason": "代际接线本机专项",
        })

    def exported(self, core):
        content = {key: value for key, value in core.items() if key != "origin"}
        return self.engine.export({"content": content, "origin": core["origin"]})

    def test_same_instance_startup_does_not_overwrite_original_boundary(self):
        ready = self.root / "guard/epochs/000001/readiness.json"
        original = guard.read_bytes(ready)
        core = self.committed()
        self.engine.complete_startup()
        self.assertEqual(guard.read_bytes(ready), original)
        raw = self.exported(core)
        self.assertEqual(len(raw["guard_continuity"]["epoch_bindings"]), 1)
        self.assertEqual(core["origin"]["service_instance_id"], raw["service_instance_id"])

    def test_nonempty_legacy_root_cannot_receive_backfilled_epoch(self):
        core = self.committed()
        operation = self.root / "guard/operations" / core["operation_id"] / "evidence-000001.json"
        original = guard.read_bytes(operation)
        shutil.rmtree(self.root / "guard/epochs")
        with self.assertRaisesRegex(guard.GuardError, "禁止倒填"):
            self.engine.complete_startup()
        self.assertEqual(guard.read_bytes(operation), original)
        self.assertFalse((self.root / "guard/epochs").exists())

    def test_current_ready_missing_does_not_inherit_the_source_pass(self):
        core = self.committed()
        original = copy.deepcopy(core)
        (self.root / "guard/epochs/000001/readiness.json").unlink()
        with self.assertRaises((ControlError, guard.GuardError)):
            self.exported(core)
        self.assertEqual(core, original)
        before = guard.read_json(self.root / "state.json")["switch_count"]
        with self.assertRaises(guard.GuardError):
            self.engine.prepare(fixtures.prepare_request(self.case.environment, nonce="missing-ready"), self.case.owner)
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], before)

    def test_ticket_to_first_run_rechecks_readiness_before_any_change(self):
        ticket = self.engine.prepare(fixtures.prepare_request(self.case.environment), self.case.owner)
        (self.root / "guard/epochs/000001/readiness.json").unlink()
        state = guard.read_bytes(self.root / "state.json")
        operation = self.engine._operation(ticket["operation_id"])
        originals = {path.relative_to(operation): guard.read_bytes(path) for path in operation.rglob("*") if path.is_file()}
        with self.assertRaises(guard.GuardError):
            self.engine.run(ticket)
        self.assertEqual(guard.read_bytes(self.root / "state.json"), state)
        self.assertEqual({path.relative_to(operation): guard.read_bytes(path) for path in operation.rglob("*") if path.is_file()}, originals)

    def test_ticket_to_first_run_rechecks_registration_before_any_change(self):
        ticket = self.engine.prepare(fixtures.prepare_request(self.case.environment), self.case.owner)
        (self.root / "guard/epochs/000001/registration.json").unlink()
        before = guard.read_json(self.root / "state.json")
        with self.assertRaises((guard.GuardError, ControlError)):
            self.engine.run(ticket)
        self.assertEqual(guard.read_json(self.root / "state.json"), before)
        self.assertNotIn("execution_started", {row["event"] for row in self.engine._journal(ticket["operation_id"])})

    def test_new_pass_finish_rechecks_readiness_without_false_commit_or_restore(self):
        ticket = self.engine.prepare(fixtures.prepare_request(self.case.environment), self.case.owner)
        observed = self.engine.run(ticket)
        decision = self.case.decision(observed)
        (self.root / "guard/epochs/000001/readiness.json").unlink()
        state = guard.read_bytes(self.root / "state.json")
        with self.assertRaises(guard.GuardError):
            self.engine.finish(ticket, decision)
        self.assertEqual(guard.read_bytes(self.root / "state.json"), state)
        self.assertFalse((self.engine._operation(ticket["operation_id"]) / "physical-intent.json").exists())

    def test_zero_operation_old_root_losing_epochs_and_head_cannot_reinitialize(self):
        self.assertEqual(self.engine._index()["operations"], [])
        self.assertTrue((self.root / "guard/index-snapshots").is_dir())
        shutil.rmtree(self.root / "guard/epochs")
        shutil.rmtree(self.root / "guard/epoch-registry")
        with self.assertRaisesRegex(guard.GuardError, "禁止倒填"):
            self.engine.complete_startup()
        self.assertFalse((self.root / "guard/epochs").exists())

    def test_current_proof_contains_and_revalidates_immutable_registry_head(self):
        core = self.committed()
        raw = self.exported(core)
        envelope = raw["guard_continuity"]
        from tools.official_client_capture import claude_upgrade_guard_epochs as epochs
        self.assertEqual(envelope["schema_version"], epochs.CONTINUITY_V2_SCHEMA)
        originals = {path: base64.b64decode(value, validate=True) for path, value in raw["bytes_by_path"].items()}
        head = envelope["registry_head_binding"]
        self.assertIn(head["path"], originals)
        content = {key: value for key, value in core.items() if key != "origin"}
        checked = epochs.validate_guard_continuity(self.case.configuration, self.case.context, envelope,
            lambda item: originals[item["path"]], source_core=content, source_origin=core["origin"],
            channel_identity=raw["guard_identity"], purpose="formed_replay")
        self.assertIn(head, [row.as_value() for row in checked.read_bindings])
        incomplete = dict(originals)
        del incomplete[head["path"]]
        with self.assertRaises((ControlError, KeyError)):
            checked.revalidate(lambda item: incomplete[item["path"]], purpose="formed_replay")

    def test_registration_gap_and_unknown_epoch_material_are_rejected(self):
        core = self.committed()
        epoch = self.root / "guard/epochs/000001"
        guard.write_json_once(epoch / "uploaded-proof.json", {"ready": True})
        with self.assertRaisesRegex(guard.GuardError, "未知原件"):
            self.exported(core)

    def test_missing_original_and_copied_source_coordinate_cannot_grant_archive_authority(self):
        core = self.committed()
        raw = self.exported(core)
        envelope = raw["guard_continuity"]
        originals = {path: base64.b64decode(value, validate=True) for path, value in raw["bytes_by_path"].items()}
        from tools.official_client_capture import claude_upgrade_guard_epochs as epochs
        content = {key: value for key, value in core.items() if key != "origin"}
        proof = epochs.validate_guard_continuity(
            self.case.configuration, self.case.context, envelope,
            lambda item: originals[item["path"]], source_core=content,
            source_origin=core["origin"], channel_identity=raw["guard_identity"], purpose="formed_replay")
        proof.revalidate(lambda item: originals[item["path"]], purpose="formed_replay")
        source = execution.VerifiedSource(
            canonical_sha256(content), raw["deployment_sha256"], originals,
            raw["configuration_sha256"], raw["service_instance_id"], guard_continuity=proof)
        rows = execution.complete_raw_bindings(source, [])
        self.assertIn(proof.source_record_binding.as_value(), rows)
        bad = dict(originals)
        bad[proof.source_record_binding.path] += b" "
        with self.assertRaises(ControlError):
            proof.revalidate(lambda item: bad[item["path"]], purpose="formed_replay")

    def test_json_continuity_and_one_sided_proof_are_rejected(self):
        source = execution.VerifiedSource("a" * 64, "b" * 64, {}, "c" * 64, "old",
                                          guard_continuity={"same_deployment": True})
        with self.assertRaisesRegex(ControlError, "内存合同"):
            execution.complete_raw_bindings(source, [])
        old = execution.VerifiedSource("a" * 64, "b" * 64, {}, "c" * 64, "old")
        other = execution.VerifiedSource("a" * 64, "b" * 64, {}, "c" * 64, "new")
        self.assertFalse(execution._same_verified_guard_source_deployment(old, other))


class GuardServiceEpochRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ProcessTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_failed_startup_keeps_diagnostics_but_blocks_new_actions_until_actual_recovery(self):
        server = self.case.start_guard()
        root = self.case.root
        guard.atomic_json(root / "fault.json", {"execute": "exception", "restore": "failed", "freeze": "normal"})
        client = executor.create_executor("arm64-isolated", self.case.configuration, self.case.context)
        ticket = client.prepare_stage(fixtures.prepare_request(self.case.environment))
        observed = client.run_stage(ticket)
        self.assertEqual(observed["result"], "failed")
        server.kill()
        server.join(timeout=3)
        self.case.start_guard()
        restarted = executor.create_executor("arm64-isolated", self.case.configuration, self.case.context)
        self.assertTrue(restarted._status()["recovery"]["blocked"])
        readiness = root / "guard/epochs/000002/readiness.json"
        self.assertFalse(readiness.exists())
        with self.assertRaisesRegex(guard.GuardError, "启动恢复"):
            restarted.prepare_stage(fixtures.prepare_request(self.case.environment, nonce="after-restart"))
        guard.atomic_json(root / "fault.json", {"execute": "normal", "restore": "normal", "freeze": "normal"})
        recovery = restarted.recover_pending({"reason": "resume"})
        self.assertFalse(recovery["blocked"])
        self.assertEqual(guard.read_json(readiness)["result"], "pass")
        self.assertEqual(recovery["operations"][0]["execution"]["result"], "failed")

    def test_removing_unused_tail_epoch_blocks_real_restart_without_reusing_number(self):
        first = self.case.start_guard()
        first.kill()
        first.join(timeout=3)
        second = self.case.start_guard()
        second.kill()
        second.join(timeout=3)
        root = self.case.root
        pointer = guard.read_bytes(root / "guard/epoch-registry/head.json")
        self.assertEqual(guard.read_json(root / "state.json")["switch_count"], 0)
        shutil.rmtree(root / "guard/epochs/000002")
        third = self.case.mp.Process(target=fixtures.guard_process, args=(self.case.configuration, self.case.context))
        third.start()
        self.case.processes.append(third)
        third.join(timeout=5)
        self.assertFalse(third.is_alive())
        self.assertNotEqual(third.exitcode, 0)
        self.assertFalse((root / "guard/epochs/000002").exists())
        self.assertEqual(guard.read_bytes(root / "guard/epoch-registry/head.json"), pointer)

    def test_normal_new_epoch_preserves_old_immutable_head_and_proof(self):
        first = self.case.start_guard()
        client = executor.create_executor("arm64-isolated", self.case.configuration, self.case.context)
        core = client.operations_index()
        content = {"campaign_id": self.case.context["campaign_id"], "delivery_id": self.case.context["delivery_id"],
                   "operation_index_binding": core["operation_index_binding"]}
        source = client.verify_origin_evidence(content, core["origin"])
        proof = source["guard_continuity"]
        before = guard.read_bytes(self.case.root / "guard/epoch-registry/head.json")
        first.kill()
        first.join(timeout=3)
        self.case.start_guard()
        self.assertNotEqual(guard.read_bytes(self.case.root / "guard/epoch-registry/head.json"), before)
        proof.revalidate(lambda item: guard.read_bytes(guard.check_binding(self.case.root, dict(item))), purpose="formed_replay")


if __name__ == "__main__":
    unittest.main()
