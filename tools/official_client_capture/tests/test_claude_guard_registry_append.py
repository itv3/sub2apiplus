"""注册追加崩溃恢复专项；仅启动真实本机子进程和私有文件现场。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import claude_upgrade_guard_epochs as epochs
from tools.official_client_capture.tests import test_claude_upgrade_arm64_delivery_executor as fixtures
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256
from tools.official_client_control.errors import ControlError


def startup_process(configuration, context, result_path, cutpoint, sequence, make_freeze, legacy=False):
    """在指定原件完成持久化后强退；身份取自本子进程，不能换成续跑进程。"""
    os.setsid()
    try:
        identity = executor.guard_identity(configuration, context, formal=False)
        engine = guard.GuardEngine(configuration, context, fixtures.FileRuntime(configuration, context), identity)
    except (ControlError, guard.GuardError) as error:
        Path(result_path).write_bytes(canonical_json_bytes({"error": str(error)}))
        raise SystemExit(71) from None
    original_write = guard.write_json_once
    original_atomic = guard.atomic_json
    root = Path(context["data_root"]) / configuration["paths"]["guard_root_relative"]

    def write(path, value):
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else ""
        if cutpoint == "before-registration" and relative == f"epochs/{sequence:06d}/registration.json":
            guard.private_directory(path.parent)
            os._exit(73)
        original_write(path, value)
        selected = (
            cutpoint == "intent" and relative == f"epoch-registry/appends/{sequence:06d}/intent.json"
            or cutpoint == "registration" and relative == f"epochs/{sequence:06d}/registration.json"
            or cutpoint == "snapshot" and relative.startswith("epoch-registry/snapshots/")
            and len(value.get("epoch_bindings", [])) == sequence
        )
        if selected:
            os._exit(73)

    def atomic(path, value):
        original_atomic(path, value)
        if cutpoint == "pointer" and path == root / "epoch-registry/head.json":
            snapshot = guard.read_json(Path(context["data_root"]) / value["snapshot_binding"]["path"])
            if len(snapshot["epoch_bindings"]) == sequence:
                os._exit(73)

    try:
        with mock.patch.object(guard, "write_json_once", side_effect=write), mock.patch.object(guard, "atomic_json", side_effect=atomic):
            if legacy:
                # 只构造真实进程的完整旧版前置；不能用删除新账本模拟合法旧链。
                stable = epochs.stable_deployment_identity(configuration, context, identity)
                registration_path = root / "epochs/000001/registration.json"
                guard.write_json_once(registration_path, {
                    "schema_version": epochs.EPOCH_SCHEMA, "epoch_sequence": 1,
                    "configuration_binding": context["configuration_ref"],
                    "configuration_sha256": canonical_sha256(configuration),
                    "context": engine.stable_context, "stable_deployment_identity_sha256": canonical_sha256(stable),
                    "guard_identity": identity, "previous_epoch_binding": None,
                })
                engine._seal_registry_head([guard.binding(Path(context["data_root"]), registration_path)])
            summary = engine.complete_startup()
            if make_freeze:
                engine.inspect({"run_nonce": "registry-producer"})
        Path(result_path).write_bytes(canonical_json_bytes({"identity": identity, "summary": summary}))
    except (ControlError, guard.GuardError) as error:
        Path(result_path).write_bytes(canonical_json_bytes({"error": str(error)}))
        raise SystemExit(71) from None
    finally:
        engine.close()


class RegistryAppendTests(unittest.TestCase):
    def setUp(self):
        self.worlds = []
        self.addCleanup(self.close_worlds)

    def close_worlds(self):
        for case in reversed(self.worlds):
            case.doCleanups()

    def world(self):
        case = fixtures.ProcessTests()
        case.setUp()
        self.worlds.append(case)
        return case

    def startup(self, case, *, cutpoint=None, sequence=1, make_freeze=False, success=True, legacy=False):
        result_path = case.root / f"startup-result-{len(case.processes):06d}.json"
        process = case.mp.Process(target=startup_process, args=(case.configuration, case.context, str(result_path), cutpoint, sequence, make_freeze, legacy))
        process.start()
        case.processes.append(process)
        process.join(timeout=8)
        self.assertFalse(process.is_alive(), "注册专项子进程没有按时退出")
        if cutpoint is not None:
            self.assertEqual(process.exitcode, 73)
            self.assertFalse(result_path.exists())
            return None
        result = guard.read_json(result_path)
        self.assertEqual(process.exitcode, 0 if success else 71, result)
        return result

    def originals(self, case):
        root = case.root / "guard"
        return {path.relative_to(root).as_posix(): guard.read_bytes(path) for path in root.rglob("*") if path.is_file() and path.name != ".service.lock"}

    def live_engine(self, case):
        identity = executor.guard_identity(case.configuration, case.context, formal=False)
        engine = guard.GuardEngine(case.configuration, case.context, fixtures.FileRuntime(case.configuration, case.context), identity)
        self.addCleanup(engine.close)
        self.assertFalse(engine.complete_startup()["blocked"])
        worker = subprocess.Popen([sys.executable, "-B", "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        def close_worker():
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=3)

        self.addCleanup(close_worker)
        self.assertEqual(worker.stdout.readline(), b"ready\n")
        return engine, guard.process_identity(worker.pid)

    def test_initial_sealed_intent_resumes_at_each_persisted_boundary(self):
        for cutpoint in ("intent", "before-registration", "registration", "snapshot", "pointer"):
            with self.subTest(cutpoint=cutpoint):
                case = self.world()
                self.startup(case, cutpoint=cutpoint)
                root = case.root / "guard"
                intent_path = root / "epoch-registry/appends/000001/intent.json"
                intent_raw = guard.read_bytes(intent_path)
                old_identity = guard.read_json(intent_path)["registration_document"]["guard_identity"]
                result = self.startup(case)
                self.assertEqual(guard.read_bytes(intent_path), intent_raw)
                self.assertEqual(guard.read_json(root / "epochs/000001/registration.json")["guard_identity"], old_identity)
                self.assertNotEqual(result["identity"], old_identity)
                self.assertTrue((root / "epoch-registry/appends/000001/completed.json").exists())
                self.assertFalse((root / "epochs/000001/readiness.json").exists())
                self.assertTrue((root / "epochs/000002/readiness.json").exists())
                self.assertEqual(guard.read_json(case.root / "state.json")["switch_count"], 0)

    def test_later_append_resumes_without_rewriting_any_previous_original(self):
        for cutpoint in ("intent", "before-registration", "registration", "snapshot", "pointer"):
            with self.subTest(cutpoint=cutpoint):
                case = self.world()
                first = self.startup(case, make_freeze=True)
                before = self.originals(case)
                self.startup(case, cutpoint=cutpoint, sequence=2)
                root = case.root / "guard"
                intent = guard.read_json(root / "epoch-registry/appends/000002/intent.json")
                result = self.startup(case)
                after = self.originals(case)
                for path, raw in before.items():
                    if path != "epoch-registry/head.json":
                        self.assertEqual(after[path], raw, path)
                self.assertEqual(guard.read_json(root / "epochs/000002/registration.json"), intent["registration_document"])
                self.assertNotEqual(intent["registration_document"]["guard_identity"], result["identity"])
                self.assertNotEqual(first["identity"], result["identity"])
                self.assertFalse((root / "epochs/000002/readiness.json").exists())
                self.assertTrue((root / "epochs/000003/readiness.json").exists())
                self.assertEqual(guard.read_json(case.root / "state.json")["switch_count"], 0)

    def test_pending_append_cannot_repair_a_missing_previous_original(self):
        for missing in ("registration", "head-snapshot", "deployment"):
            with self.subTest(missing=missing):
                case = self.world()
                self.startup(case)
                self.startup(case, cutpoint="registration", sequence=2)
                root = case.root / "guard"
                intent = guard.read_json(root / "epoch-registry/appends/000002/intent.json")
                paths = {
                    "registration": root / "epochs/000001/registration.json",
                    "head-snapshot": case.root / intent["previous_head_binding"]["path"],
                    "deployment": case.root / case.configuration["guard"]["deployment_receipt_binding"]["path"],
                }
                removed = paths[missing]
                removed.unlink()
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)
                self.assertFalse(removed.exists())
                self.assertFalse((root / "epoch-registry/appends/000002/completed.json").exists())

    def test_advanced_pointer_only_permits_completion_when_new_originals_still_exist(self):
        for missing in ("registration", "head-snapshot"):
            with self.subTest(missing=missing):
                case = self.world()
                self.startup(case)
                self.startup(case, cutpoint="pointer", sequence=2)
                root = case.root / "guard"
                intent = guard.read_json(root / "epoch-registry/appends/000002/intent.json")
                removed = root / "epochs/000002/registration.json" if missing == "registration" else case.root / intent["proposed_head_binding"]["path"]
                removed.unlink()
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)
                self.assertFalse(removed.exists())
                self.assertFalse((root / "epoch-registry/appends/000002/completed.json").exists())

    def test_completed_transactions_never_repair_deleted_history(self):
        for missing in ("tail", "pointer", "head-snapshot", "intent"):
            with self.subTest(missing=missing):
                case = self.world()
                self.startup(case)
                self.startup(case)
                root = case.root / "guard"
                head = guard.read_json(root / "epoch-registry/head.json")["snapshot_binding"]
                removed = {
                    "tail": root / "epochs/000002/registration.json",
                    "pointer": root / "epoch-registry/head.json",
                    "head-snapshot": case.root / head["path"],
                    "intent": root / "epoch-registry/appends/000002/intent.json",
                }[missing]
                removed.unlink()
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)
                self.assertFalse(removed.exists())

    def test_missing_intent_or_conflicting_new_original_never_authorizes_recovery(self):
        for drift in ("intent", "registration", "two-pending"):
            with self.subTest(drift=drift):
                case = self.world()
                self.startup(case)
                self.startup(case, cutpoint="registration", sequence=2)
                root = case.root / "guard"
                intent_path = root / "epoch-registry/appends/000002/intent.json"
                if drift == "intent":
                    intent_path.unlink()
                elif drift == "registration":
                    path = root / "epochs/000002/registration.json"
                    path.write_bytes(guard.read_bytes(path) + b" ")
                else:
                    guard.write_once(root / "epoch-registry/appends/000003/intent.json", guard.read_bytes(intent_path))
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)

    def test_pending_process_identity_cannot_be_backfilled_into_previous_producer(self):
        case = self.world()
        self.startup(case, make_freeze=True)
        self.startup(case, cutpoint="intent", sequence=2)
        root = case.root / "guard"
        intent = guard.read_json(root / "epoch-registry/appends/000002/intent.json")
        pending_identity = intent["registration_document"]["guard_identity"]
        snapshot_path = next((root / "freezes").rglob("snapshot.json"))
        snapshot = guard.read_json(snapshot_path)
        snapshot["cores"]["freeze"]["guard_identity"] = pending_identity
        for core in snapshot["cores"].values():
            core["origin"]["service_instance_id"] = pending_identity["service_instance_id"]
        snapshot_path.write_bytes(canonical_json_bytes(snapshot))
        before = self.originals(case)
        self.startup(case, success=False)
        self.assertEqual(self.originals(case), before)
        self.assertFalse((root / "epochs/000002").exists())

    def test_initial_pending_intent_does_not_authorize_nonempty_legacy_scaffold(self):
        for extra in ("index-snapshots/legacy.json", "freezes/legacy.json", "epoch-registry/uploaded.json"):
            with self.subTest(extra=extra):
                case = self.world()
                self.startup(case, cutpoint="intent")
                root = case.root / "guard"
                guard.write_json_once(root / extra, {"legacy": True})
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)
                self.assertFalse((root / "epochs").exists())

    def test_completed_pointer_rollback_cannot_reuse_previous_epoch_number(self):
        case = self.world()
        self.startup(case)
        self.startup(case)
        root = case.root / "guard"
        intent = guard.read_json(root / "epoch-registry/appends/000002/intent.json")
        guard.atomic_json(root / "epoch-registry/head.json", {
            "schema_version": guard.REGISTRY_POINTER_SCHEMA,
            "snapshot_binding": intent["previous_head_binding"],
            **({"append_intent_binding": guard.binding(case.root, root / "epoch-registry/appends/000001/intent.json")}
               if guard.REGISTRY_POINTER_SCHEMA.endswith("/v2") else {}),
        })
        before = self.originals(case)
        self.startup(case, success=False)
        self.assertEqual(self.originals(case), before)
        self.assertFalse((root / "epochs/000003").exists())

    def test_pending_append_rejects_a_deleted_completed_transaction_prefix(self):
        for cutpoint in ("intent", "pointer", None):
            with self.subTest(cutpoint=cutpoint):
                case = self.world()
                self.startup(case)
                self.startup(case, cutpoint=cutpoint, sequence=2)
                root = case.root / "guard"
                shutil.rmtree(root / "epoch-registry/appends/000001")
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)
                self.assertFalse((root / "epochs/000003").exists())

    def test_whole_append_root_deletion_is_not_a_legacy_initialization(self):
        for cutpoint in (None, "intent", "pointer"):
            with self.subTest(cutpoint=cutpoint):
                case = self.world()
                self.startup(case)
                if cutpoint is not None:
                    self.startup(case, cutpoint=cutpoint, sequence=2)
                root = case.root / "guard"
                shutil.rmtree(root / "epoch-registry/appends")
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)
                self.assertFalse((root / "epoch-registry/appends").exists())

    def test_complete_v1_legacy_head_can_continue_once_and_publish_v2_anchor(self):
        case = self.world()
        self.startup(case, legacy=True)
        root = case.root / "guard"
        old_pointer = guard.read_json(root / "epoch-registry/head.json")
        before = self.originals(case)
        self.assertTrue(old_pointer["schema_version"].endswith("/v1"))
        self.assertFalse((root / "epoch-registry/appends").exists())
        self.startup(case)
        current = guard.read_json(root / "epoch-registry/head.json")
        self.assertTrue(current["schema_version"].endswith("/v2"))
        self.assertEqual(current["append_intent_binding"], guard.binding(case.root, root / "epoch-registry/appends/000002/intent.json"))
        self.assertIsNone(guard.read_json(root / "epoch-registry/appends/000002/intent.json")["previous_append_completed_binding"])
        after = self.originals(case)
        for path, raw in before.items():
            if path != "epoch-registry/head.json":
                self.assertEqual(after[path], raw, path)
        self.startup(case)
        self.assertTrue((root / "epoch-registry/appends/000003/completed.json").exists())

    def test_v1_legacy_transition_resumes_at_all_new_persisted_boundaries(self):
        for cutpoint in ("intent", "before-registration", "registration", "snapshot", "pointer"):
            with self.subTest(cutpoint=cutpoint):
                case = self.world()
                self.startup(case, legacy=True)
                before = self.originals(case)
                self.startup(case, cutpoint=cutpoint, sequence=2)
                self.startup(case)
                after = self.originals(case)
                for path, raw in before.items():
                    if path != "epoch-registry/head.json":
                        self.assertEqual(after[path], raw, path)
                self.assertTrue(guard.read_json(case.root / "guard/epoch-registry/head.json")["schema_version"].endswith("/v2"))
                self.assertFalse((case.root / "guard/epochs/000002/readiness.json").exists())
                self.assertTrue((case.root / "guard/epochs/000003/readiness.json").exists())

    def test_v2_pointer_rejects_wrong_anchor_and_v1_downgrade_with_new_history(self):
        for drift in ("different-intent", "same-head-wrong-intent", "v1"):
            with self.subTest(drift=drift):
                case = self.world()
                self.startup(case)
                self.startup(case)
                root = case.root / "guard"
                pointer_path = root / "epoch-registry/head.json"
                pointer = guard.read_json(pointer_path)
                old = guard.binding(case.root, root / "epoch-registry/appends/000001/intent.json")
                if drift == "different-intent":
                    pointer["append_intent_binding"] = old
                elif drift == "same-head-wrong-intent":
                    pointer["append_intent_binding"] = {**pointer["append_intent_binding"], "path": old["path"]}
                else:
                    pointer = {"schema_version": "claude-upgrade-delivery-guard-registry-pointer/v1",
                               "snapshot_binding": pointer["snapshot_binding"]}
                guard.atomic_json(pointer_path, pointer)
                before = self.originals(case)
                self.startup(case, success=False)
                self.assertEqual(self.originals(case), before)

    def test_current_readiness_rechecks_whole_history_without_completion_or_mutation(self):
        for action in ("prepare", "run", "finish", "inspect"):
            for missing in ("whole-root", "old-prefix", "current-completed"):
                with self.subTest(action=action, missing=missing):
                    case = self.world()
                    self.startup(case)
                    self.startup(case)
                    engine, owner = self.live_engine(case)
                    request = fixtures.prepare_request(case.environment)
                    ticket = engine.prepare(request, owner) if action in {"run", "finish"} else None
                    observation = engine.run(ticket) if action == "finish" else None
                    root = case.root / "guard"
                    ledger = root / "epoch-registry/appends"
                    if missing == "whole-root":
                        shutil.rmtree(ledger)
                    elif missing == "old-prefix":
                        shutil.rmtree(ledger / "000001")
                    else:
                        (ledger / "000003/completed.json").unlink()
                    before = self.originals(case)
                    state = guard.read_bytes(case.root / "state.json")
                    with self.assertRaises(guard.GuardError):
                        if action == "prepare":
                            engine.prepare(request, owner)
                        elif action == "run":
                            engine.run(ticket)
                        elif action == "finish":
                            engine.finish(ticket, {"result": "pass", "observation_sha256": canonical_sha256(observation),
                                "verifier_binding": case.configuration["guard"]["executor_binding"], "reason": "历史完整性专项"})
                        else:
                            engine.inspect({"run_nonce": "history-recheck"})
                    self.assertEqual(self.originals(case), before)
                    self.assertEqual(guard.read_bytes(case.root / "state.json"), state)

    def test_history_loss_keeps_existing_terminal_reentry_and_failed_recovery_boundaries(self):
        for committed in (True, False):
            with self.subTest(committed=committed):
                case = self.world()
                self.startup(case)
                engine, owner = self.live_engine(case)
                ticket = engine.prepare(fixtures.prepare_request(case.environment), owner)
                observed = engine.run(ticket)
                decision = {"result": "pass", "observation_sha256": canonical_sha256(observed),
                            "verifier_binding": case.configuration["guard"]["executor_binding"], "reason": "恢复边界专项"}
                core = engine.finish(ticket, decision) if committed else None
                shutil.rmtree(case.root / "guard/epoch-registry/appends")
                before = self.originals(case)
                state = guard.read_bytes(case.root / "state.json")
                if committed:
                    self.assertEqual(engine.run(ticket), observed)
                    self.assertEqual(engine.finish(ticket, decision), core)
                    self.assertEqual(self.originals(case), before)
                    self.assertEqual(guard.read_bytes(case.root / "state.json"), state)
                else:
                    failed = engine.finish(ticket, {**decision, "result": "failed"})
                    self.assertEqual(failed["result"], "failed")
                    self.assertEqual(failed["restoration"]["state"], "verified")
                    self.assertEqual(guard.read_json(case.root / "state.json")["restore_count"], 1)
                with self.assertRaises(guard.GuardError):
                    engine.prepare(fixtures.prepare_request(case.environment, nonce="after-history-loss"), owner)

    def test_old_sealing_helper_cannot_downgrade_a_v2_pointer_after_history_loss(self):
        case = self.world()
        self.startup(case)
        engine, _owner = self.live_engine(case)
        root = case.root / "guard"
        registrations = [guard.binding(case.root, path) for path in sorted((root / "epochs").glob("*/registration.json"))]
        shutil.rmtree(root / "epoch-registry/appends")
        before = self.originals(case)
        with self.assertRaises(guard.GuardError):
            engine._seal_registry_head(registrations)
        self.assertEqual(self.originals(case), before)

    def test_normal_append_preserves_formed_proof_and_real_previous_producer(self):
        case = self.world()
        first = case.start_guard()
        client = executor.create_executor("arm64-isolated", case.configuration, case.context)
        ticket = client.prepare_stage(fixtures.prepare_request(case.environment))
        observation = client.run_stage(ticket)
        core = client.finish_stage(ticket, {
            "result": "pass", "observation_sha256": canonical_sha256(observation),
            "verifier_binding": case.configuration["guard"]["executor_binding"], "reason": "注册追加专项",
        })
        content = {key: value for key, value in core.items() if key != "origin"}
        source = client.verify_origin_evidence(content, core["origin"])
        proof = source["guard_continuity"]
        before = self.originals(case)
        first.kill()
        first.join(timeout=3)
        self.startup(case)
        after = self.originals(case)
        for path, raw in before.items():
            if path not in {"epoch-registry/head.json", "rpc.sock"}:
                self.assertEqual(after[path], raw, path)
        proof.revalidate(lambda item: guard.read_bytes(guard.check_binding(case.root, dict(item))), purpose="formed_replay")
        self.assertEqual(guard.read_json(case.root / "state.json")["switch_count"], 1)


if __name__ == "__main__":
    unittest.main()
