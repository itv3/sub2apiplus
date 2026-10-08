"""ARM64交付模块的本机专项验证；只使用文件现场与独立守卫子进程。

这里不启动产品、Docker或真实账号，来源固定regression_fixture。测试核对
修改前落盘、非法观测／复核拒绝恢复、强杀、服务重启、幂等和索引遗漏。
"""

from __future__ import annotations

import copy
import hashlib
import http.server
import io
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import tarfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256


def fixture_world(root: Path) -> tuple[dict, dict, dict]:
    guard.private_directory(root)
    def material(relative: str, value: object) -> dict:
        path = guard.managed_path(root, relative)
        content = value if isinstance(value, bytes) else canonical_json_bytes(value)
        guard.write_once(path, content)
        return guard.binding(root, path)
    candidate_profile = material("inputs/candidate-profile.json", {"client": "claude", "mode": "candidate"})
    rollback_profile = material("inputs/rollback-profile.json", {"client": "claude", "mode": "baseline"})
    def qualification(candidate_id: str) -> dict:
        def fact(sequence: int, marker: str) -> dict:
            return {"campaign_id": "C-fixture", "dimension": "validation", "sequence": sequence, "sha256": marker * 64}
        return {"store_relative": "control/store", "external_root_relative": "evidence", "campaign_id": "C-fixture", "candidate_id": candidate_id, "candidate_ref": fact(1, "a"), "build_receipt_ref": {"receipt_kind": "candidate_build", "sha256": "b" * 64}, "validation_ref": fact(3, "c"), "acceptance_ref": fact(2, "d"), "sealed_result_binding": material("inputs/" + candidate_id + "-sealed.json", {"fixture": candidate_id})}
    script = material("inputs/docker-fixture", b"#!/bin/sh\nexit 9\n")
    (root / script["path"]).chmod(0o700)
    configuration = {
        "schema_version": executor.CONFIGURATION_SCHEMA, "client": "claude", "host_id": "ARM64", "instance_id": "candidate-fixture", "isolation": "isolated_candidate", "source_scope": "regression_fixture",
        "paths": {"guard_root_relative": "guard", "active_profile_relative": "runtime/profile.json", "compose_binding": material("inputs/base-compose.json", {"fixture": "compose"})},
        "runtime": {"docker_binding": {"absolute_path": str(root / script["path"]), "sha256": script["sha256"], "bytes": script["bytes"]}, "daemon_socket": "/var/run/docker.sock", "compose_project": "claude_fixture", "application_service": "app", "dependency_services": ["db", "redis"], "profile_container_path": "/app/profile.json", "expected_labels": {"sub2api.delivery.isolation": "isolated_candidate", "sub2api.delivery.instance": "candidate-fixture"}},
        "candidate": {"image_digest": "sha256:" + "a" * 64, "runtime_profile_sha256": candidate_profile["sha256"], "profile_binding": candidate_profile, "override_binding": material("inputs/candidate-override.json", {"image": "candidate"}), "qualification": qualification("candidate-current")},
        "rollback": {"image_digest": "sha256:" + "b" * 64, "runtime_profile_sha256": rollback_profile["sha256"], "profile_binding": rollback_profile, "override_binding": material("inputs/rollback-override.json", {"image": "baseline"}), "qualification": qualification("candidate-baseline")},
        "probes": {"base_url": "http://127.0.0.1:8099", "credential_binding": material("inputs/probe-key.private", b"fixture-private-key"), "expected_models": ["claude-fixture-model"], "group_id": 42, "account_ids": [8], "database": {"service": "db", "user": "fixture", "name": "fixture", "api_key_id": 7}, "redis": {"service": "redis"}, "timeout_seconds": 1, "max_requests": 30},
        "guard": {"service_name": "claude-delivery-fixture.service", "service_unit_binding": material("inputs/guard.service", b"fixture-service\n"), "executor_binding": material("inputs/executor.py", Path(executor.__file__).read_bytes()), "guard_binding": material("inputs/guard.py", Path(guard.__file__).read_bytes()), "deployment_receipt_binding": material("inputs/deployment.json", {"scope": "regression_fixture"}), "socket_relative": "guard/rpc.sock", "channel_key_binding": material("inputs/channel-key.private", b"fixture-channel-key-32-bytes-minimum")},
        "limits": {"lease_seconds": 3, "command_timeout_seconds": 2, "stage_timeout_seconds": 20, "observation_window_seconds": 1, "poll_seconds": 1},
    }
    executor.validate_configuration(configuration)
    context = {"campaign_id": "C-fixture", "candidate_id": "candidate-current", "delivery_id": "candidate-current-d0", "segment": "dr0", "run_nonce": "run-fixture", "data_root": str(root), "configuration_ref": material("inputs/configuration.json", configuration)}
    environment = {"host_id": "ARM64", "architecture": "arm64", "compose_sha256": configuration["paths"]["compose_binding"]["sha256"], "environment_sha256": "c" * 64, "configuration_sha256": canonical_sha256(configuration), "network_sha256": "d" * 64}
    state = {"image_digest": configuration["rollback"]["image_digest"], "runtime_profile_sha256": configuration["rollback"]["runtime_profile_sha256"], "environment": environment, "switch_count": 0, "restore_count": 0}
    material("state.json", state)
    material("fault.json", {"execute": "normal", "restore": "normal", "freeze": "normal"})
    return configuration, context, environment


class FileRuntime:
    """仅本测试显式注入的文件现场，不能通过正式CLI选择或登记。"""

    def __init__(self, configuration: dict, context: dict):
        self.configuration, self.context = configuration, context
        self.root = Path(context["data_root"])

    def check_qualification(self) -> None:
        if self.configuration["source_scope"] != "regression_fixture":
            raise guard.GuardError("文件现场只允许fixture")

    def before(self, directory: Path, expected_environment: dict | None = None) -> list[dict]:
        if expected_environment is not None and guard.read_json(self.root / "state.json")["environment"] != expected_environment:
            raise guard.GuardError("fixture armed前环境漂移")
        path = directory / "before" / "state.json"
        guard.write_once(path, guard.read_bytes(self.root / "state.json"))
        return [guard.binding(self.root, path)]

    def observation(self, stage: str, target: dict, directory: Path) -> dict:
        state = guard.read_json(self.root / "state.json")
        image_path, profile_path = directory / "image.json", directory / "profile.json"
        guard.write_json_once(image_path, {"image_digest": state["image_digest"]})
        guard.write_json_once(profile_path, {"runtime_profile_sha256": state["runtime_profile_sha256"]})
        return {"stage": stage, "target": target, "actual": {"image": {"state": "known", "value": state["image_digest"], "direct_binding": guard.binding(self.root, image_path)}, "profile": {"state": "known", "value": state["runtime_profile_sha256"], "direct_binding": guard.binding(self.root, profile_path)}}, "environment": state["environment"], "checks": [{"id": name, "result": "pass", "facts": {"scope": "regression_fixture"}} for name in guard.STAGE_CHECKS[stage]], "observed_seconds": 1 if stage == "stable_observed" else 0, "live_request_count": 0, "request_records": [], "before_bindings": [], "during_bindings": [guard.binding(self.root, image_path), guard.binding(self.root, profile_path)], "after_bindings": [], "result": "pass"}

    def execute(self, stage: str, target: dict, environment: dict, directory: Path, checkpoint: object) -> dict:
        checkpoint()
        # 在实际改变文件现场前，从磁盘核prepared、index、armed与journal顺序。
        prepared = guard.read_json(directory / "prepared.json")
        armed = guard.read_json(directory / "armed.json")
        index = guard.read_json(self.root / "guard" / "index.json")
        if prepared["operation_id"] != armed["operation_id"] or prepared["operation_id"] not in {row["operation_id"] for row in index["operations"]}:
            raise AssertionError("物理变更早于prepared/index/armed")
        journals = [guard.read_json(path) for path in sorted((directory / "journal").glob("*.json"))]
        if [row["event"] for row in journals[:3]] != ["prepared", "armed", "execution_started"]:
            raise AssertionError("物理变更早于持久化日志")
        state = guard.read_json(self.root / "state.json")
        state.update(target)
        state["switch_count"] += 1
        guard.atomic_json(self.root / "state.json", state)
        fault = guard.read_json(self.root / "fault.json")["execute"]
        if fault == "exception":
            raise RuntimeError("fixture故障")
        if fault == "wait":
            for _ in range(100):
                checkpoint()
                time.sleep(0.1)
        observation = self.observation(stage, target, directory / "during")
        if fault == "invalid":
            observation["unexpected"] = True
        if fault == "failed":
            observation["result"] = "failed"
            observation["checks"][0]["result"] = "failed"
        return observation

    def restore(self, target: dict, environment: dict, directory: Path) -> dict:
        state = guard.read_json(self.root / "state.json")
        state["restore_count"] += 1
        guard.atomic_json(self.root / "state.json", state)
        if guard.read_json(self.root / "fault.json")["restore"] == "failed":
            raise OSError("fixture Docker暂不可用")
        state.update(target)
        guard.atomic_json(self.root / "state.json", state)
        observed = self.observation("rollback_verified", target, directory / "observed")
        return {"target": target, "actual": observed["actual"], "checks": observed["checks"], "bindings": observed["during_bindings"], "result": "pass"}

    def freeze(self, request: dict, directory: Path) -> dict:
        self.check_qualification()
        state = guard.read_json(self.root / "state.json")
        before = self.before(directory)
        if guard.read_json(self.root / "fault.json")["freeze"] == "failed":
            raise guard.GuardError("fixture准入材料损坏")
        projection_path = directory / "projection.json"
        projection = {"scope": "regression_fixture", "environment": state["environment"]}
        guard.write_json_once(projection_path, projection)
        gates = [{"id": name, "result": "pass", "facts": {"bindings": before, "scope": "regression_fixture"}} for name in executor.DELIVERY_GATES]
        q = self.configuration["rollback"]["qualification"]
        common = {"campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"], "result": "pass"}
        cores = {
            "configuration": {"configuration": self.configuration, "configuration_sha256": canonical_sha256(self.configuration), "binding": self.context["configuration_ref"]},
            "stable_projection": {"projection": projection, "projection_sha256": canonical_sha256(projection), "binding": guard.binding(self.root, projection_path)},
            "freeze": {"environment": state["environment"], "before_bindings": before, "gates": gates, "guard_identity": {}},
            "rollback_state": {"candidate_ref": q["candidate_ref"], "build_receipt_ref": q["build_receipt_ref"], "acceptance_ref": q["acceptance_ref"], "evidence_package_ref": {"object_kind": "candidate_evidence_package", "sha256": "e" * 64}, "image_digest": self.configuration["rollback"]["image_digest"], "runtime_profile_sha256": self.configuration["rollback"]["runtime_profile_sha256"], "bindings": [q["sealed_result_binding"]]},
            "zero_model_probes": {"mode": "recipe", "recipe": {"scope": "regression_fixture"}, "request_records": [], "bindings": before},
            "deployment_configuration": {"configuration_binding": self.context["configuration_ref"], "deployment_sha256": self.configuration["guard"]["deployment_receipt_binding"]["sha256"], "bindings": [self.configuration["guard"]["deployment_receipt_binding"]]},
        }
        for kind, core in cores.items():
            core.update(common)
            core["contract_version"] = f"claude-delivery-{kind.replace('_', '-')}/v1"
        return {"environment": state["environment"], "rollback": {key: self.configuration["rollback"][key] for key in ("image_digest", "runtime_profile_sha256")}, "gates": gates, "cores": cores}


def guard_process(configuration: dict, context: dict) -> None:
    os.setsid()
    identity = executor.guard_identity(configuration, context, formal=False)
    engine = guard.GuardEngine(configuration, context, FileRuntime(configuration, context), identity)
    root = Path(context["data_root"])
    key = guard.read_bytes(guard.check_binding(root, configuration["guard"]["channel_key_binding"]))
    guard.serve(engine, root / configuration["guard"]["socket_relative"], key)


def worker_process(configuration: dict, context: dict, environment: dict) -> None:
    os.setsid()
    instance = executor.create_executor("arm64-isolated", configuration, context)
    ticket = instance.prepare_stage(prepare_request(environment))
    instance.run_stage(ticket)
    while True:
        time.sleep(1)


def prepare_request(environment: dict, stage: str = "candidate_active", nonce: str = "run-fixture") -> dict:
    def reference(kind: str, marker: str) -> dict:
        return {"object_kind": kind, "sha256": marker * 64}
    return {"stage": stage, "run_nonce": nonce, "plan_ref": reference("candidate_delivery_plan", "a"), "configuration_ref": reference("operational_evidence", "b"), "stable_projection_ref": reference("operational_evidence", "c"), "probe_recipe_ref": reference("operational_evidence", "d"), "environment": environment}


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cgd-", dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.configuration, self.context, self.environment = fixture_world(self.root)
        self.owner_process = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._close)
        self.identity = executor.guard_identity(self.configuration, self.context, formal=False)
        self.engine = guard.GuardEngine(self.configuration, self.context, FileRuntime(self.configuration, self.context), self.identity)
        self.owner = guard.process_identity(self.owner_process.pid)

    def _close(self) -> None:
        if getattr(self, "engine", None) is not None:
            self.engine.close()
        self.owner_process.terminate()
        self.owner_process.wait(timeout=3)
        self.temporary.cleanup()

    def fault(self, **values: str) -> None:
        fault = guard.read_json(self.root / "fault.json")
        fault.update(values)
        guard.atomic_json(self.root / "fault.json", fault)

    def decision(self, raw: dict, result: str = "pass") -> dict:
        verifier_path = self.root / "publication" / "verifier.json"
        guard.write_json_once(verifier_path, {"scope": "regression_fixture", "observation_sha256": canonical_sha256(raw)})
        return {"result": result, "observation_sha256": canonical_sha256(raw), "verifier_binding": guard.binding(self.root, verifier_path), "reason": "本机专项复核"}

    def test_prepared_fsync_and_armed_precede_change(self) -> None:
        synced = []
        original = guard.sync_directory
        def record(path: Path) -> None:
            synced.append(path)
            original(path)
        with mock.patch.object(guard, "sync_directory", side_effect=record):
            ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 0)
        operation = self.root / "guard" / "operations" / ticket["operation_id"]
        self.assertIn(operation, synced)
        self.assertIn(self.root / "guard", synced)
        self.assertEqual(self.engine.run(ticket)["result"], "pass")
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 1)

    def test_commit_and_reentry_preserve_bytes_and_do_not_repeat_action(self) -> None:
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        raw = self.engine.run(ticket)
        core = self.engine.finish(ticket, self.decision(raw))
        before = canonical_json_bytes(core)
        operation = self.root / "guard" / "operations" / ticket["operation_id"]
        journal = guard.read_bytes(guard.check_binding(self.root, core["journal_binding"]))
        self.assertEqual(core["result"], "pass")
        self.assertEqual(self.engine.prepare(prepare_request(self.environment), self.owner), ticket)
        self.engine.run(ticket)
        self.assertEqual(canonical_json_bytes(self.engine.finish(ticket, {})), before)
        self.assertEqual(guard.read_bytes(guard.check_binding(self.root, core["journal_binding"])), journal)
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 1)
        prepared, intent, terminal, prefix = [guard.read_json(guard.check_binding(self.root, core[name])) for name in ("prepared_binding", "physical_intent_binding", "guard_terminal_binding", "journal_binding")]
        self.assertTrue(all(document["operation_id"] == ticket["operation_id"] for document in (prepared, intent, terminal, prefix)))
        self.assertEqual(intent["prepared_sha256"], core["prepared_binding"]["sha256"])
        self.assertEqual(terminal["physical_intent_sha256"], core["physical_intent_binding"]["sha256"])
        self.assertNotIn("guard_terminal_binding", intent)
        self.assertNotIn("publication_intent", intent)

    def test_exception_and_invalid_observation_restore_fixed_baseline(self) -> None:
        for fault in ("exception", "invalid", "failed"):
            with self.subTest(fault=fault):
                self.fault(execute=fault)
                ticket = self.engine.prepare(prepare_request(self.environment, nonce="run-" + fault), self.owner)
                raw = self.engine.run(ticket)
                core = self.engine.finish(ticket, self.decision(raw, "failed")) if not (self.root / "publication" / "verifier.json").exists() else self.engine.finish(ticket, {})
                self.assertEqual(core["result"], "failed")
                self.assertEqual(core["restoration"]["state"], "verified")
                state = guard.read_json(self.root / "state.json")
                self.assertEqual(state["image_digest"], self.configuration["rollback"]["image_digest"])
                self.assertEqual(state["runtime_profile_sha256"], self.configuration["rollback"]["runtime_profile_sha256"])
        inventory = self.engine.inventory()
        self.assertEqual(len(inventory["executions"]), 3)
        self.assertEqual(len(inventory["restorations"]), 3)

    def test_verdict_reject_or_bad_prewriting_restores(self) -> None:
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        raw = self.engine.run(ticket)
        decision = self.decision(raw, "failed")
        decision["observation_sha256"] = "f" * 64
        core = self.engine.finish(ticket, decision)
        self.assertEqual(core["restoration"]["state"], "verified")
        self.assertEqual(core["result"], "failed")
        self.assertIsNone(core["physical_intent_binding"])

    def test_owner_loss_recovers_and_new_request_does_not_replay_old_mutation(self) -> None:
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        self.engine.run(ticket)
        self.owner_process.kill()
        self.owner_process.wait(timeout=2)
        summary = self.engine.recover_pending({"reason": "resume"})
        self.assertFalse(summary["blocked"])
        self.assertEqual(summary["operations"][0]["status"], "restored")
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 1)

    def test_unarmed_index_crash_gap_recovers_but_armed_omission_blocks(self) -> None:
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        operation = self.root / "guard" / "operations" / ticket["operation_id"]
        # 精确模拟prepared及首journal已fsync、index尚未写入的断点。
        (operation / "armed.json").unlink()
        (operation / "journal" / "000002.json").unlink()
        index_path = self.root / "guard" / "index.json"
        index = guard.read_json(index_path)
        guard.atomic_json(index_path, {**index, "operations": []})
        self.engine.owners.clear()
        summary = self.engine.recover_pending({"reason": "startup"}, startup=True)
        self.assertFalse(summary["blocked"])
        self.assertEqual(summary["operations"][0]["status"], "restored")
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 0)
        core = summary["operations"][0]["execution"]
        self.assertIsNone(core["environment"])
        self.assertEqual(core["actual"]["image"]["state"], "unknown")

    def test_before_drift_is_zero_change_and_repair_allows_same_request(self) -> None:
        state = guard.read_json(self.root / "state.json")
        state["environment"]["network_sha256"] = "e" * 64
        guard.atomic_json(self.root / "state.json", state)
        with self.assertRaisesRegex(guard.GuardError, "漂移"):
            self.engine.prepare(prepare_request(self.environment), self.owner)
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 0)
        self.assertEqual(self.engine.inventory()["executions"], [])
        state["environment"] = self.environment
        guard.atomic_json(self.root / "state.json", state)
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        self.assertEqual(self.engine.run(ticket)["result"], "pass")

    def test_restore_failed_blocks_then_recovery_keeps_first_execution(self) -> None:
        self.fault(execute="exception", restore="failed")
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        self.engine.run(ticket)
        first = self.engine.finish(ticket, {})
        self.assertEqual(first["restoration"]["state"], "restore_failed")
        with self.assertRaises(guard.GuardError):
            self.engine.prepare(prepare_request(self.environment, nonce="next-run"), self.owner)
        self.fault(restore="normal")
        recovered = self.engine.recover_pending({"reason": "resume"})
        self.assertFalse(recovered["blocked"])
        self.assertEqual(recovered["operations"][0]["execution"], first)
        inventory = self.engine.inventory()
        self.assertEqual(inventory["executions"], [first])
        self.assertEqual([row["result"] for row in inventory["restorations"]], ["failed", "pass"])
        index = guard.read_json(guard.check_binding(self.root, inventory["operations_index_binding"]))
        self.assertEqual(index["operations"][0]["status"], "restored")
        self.assertEqual(sum("/terminals/" in item["path"] for item in index["operations"][0]["bindings"]), 2)

    def test_guard_restart_recovers_all_unresolved_operations(self) -> None:
        self.fault(execute="exception", restore="failed")
        first = self.engine.prepare(prepare_request(self.environment), self.owner)
        self.engine.run(first)
        # 第二份prepared用于模拟重启接收了多份物理日志；第一份失败不会被忽略。
        self.fault(restore="normal", execute="normal")
        self.engine.recover_pending({"reason": "resume"})
        second = self.engine.prepare(prepare_request(self.environment, stage="candidate_restored", nonce="run-second"), self.owner)
        self.engine.run(second)
        self.engine.close()
        self.engine = guard.GuardEngine(self.configuration, self.context, FileRuntime(self.configuration, self.context), self.identity)
        summary = self.engine.recover_pending({"reason": "startup"}, startup=True)
        self.assertFalse(summary["blocked"])
        self.assertEqual(len(summary["operations"]), 2)
        self.assertTrue(all(row["status"] == "restored" for row in summary["operations"]))

    def test_corrupted_original_and_omitted_index_are_rejected(self) -> None:
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        index_path = self.root / "guard" / "index.json"
        original = guard.read_json(index_path)
        guard.atomic_json(index_path, {**original, "operations": []})
        with self.assertRaisesRegex(guard.GuardError, "遗漏"):
            self.engine.recover_pending({"reason": "resume"})
        guard.atomic_json(index_path, original)
        path = guard.check_binding(self.root, ticket["prepared_binding"])
        path.write_text("{}\n")
        with self.assertRaises(guard.GuardError):
            self.engine.run(ticket)

    def test_source_export_requires_guard_generated_content_and_full_index(self) -> None:
        ticket = self.engine.prepare(prepare_request(self.environment), self.owner)
        raw = self.engine.run(ticket)
        core = self.engine.finish(ticket, self.decision(raw))
        content = {key: value for key, value in core.items() if key != "origin"}
        verified = self.engine.export({"content": content, "origin": core["origin"]})
        self.assertEqual(verified["content_sha256"], canonical_sha256(content))
        self.assertEqual(verified["trusted_data_root"], str(self.root))
        fake = {**content, "observed_seconds": 99}
        with self.assertRaisesRegex(guard.GuardError, "不是"):
            self.engine.export({"content": fake, "origin": core["origin"]})
        inventory = self.engine.inventory()
        index_core = {"campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"], "operation_index_binding": inventory["operations_index_binding"]}
        source = self.engine.export({"content": index_core, "origin": inventory["origin"]})
        index = guard.read_json(guard.check_binding(self.root, inventory["operations_index_binding"]))
        self.assertTrue(all(item["path"] in source["bytes_by_path"] for item in index["operations"][0]["bindings"]))


class ProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cgp-", dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.configuration, self.context, self.environment = fixture_world(self.root)
        self.processes = []
        self.addCleanup(self.close)
        self.mp = multiprocessing.get_context("spawn")

    def close(self) -> None:
        for process in reversed(self.processes):
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
        self.temporary.cleanup()

    def start_guard(self) -> object:
        process = self.mp.Process(target=guard_process, args=(self.configuration, self.context))
        process.start()
        self.processes.append(process)
        path = self.root / "guard" / "rpc.sock"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if path.exists() and process.is_alive():
                try:
                    executor.create_executor("arm64-isolated", self.configuration, self.context)._status()
                    return process
                except (guard.GuardError, OSError):
                    pass
            time.sleep(0.05)
        self.fail("独立guard未就绪")

    def test_worker_sigkill_leaves_guard_alive_and_physically_restores(self) -> None:
        server = self.start_guard()
        guard.atomic_json(self.root / "fault.json", {"execute": "wait", "restore": "normal", "freeze": "normal"})
        worker = self.mp.Process(target=worker_process, args=(self.configuration, self.context, self.environment))
        worker.start()
        self.processes.append(worker)
        deadline = time.monotonic() + 6
        while guard.read_json(self.root / "state.json")["switch_count"] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 1)
        self.assertNotEqual(os.getpgid(server.pid), os.getpgid(worker.pid))
        os.kill(worker.pid, signal.SIGKILL)
        worker.join(timeout=2)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            state = guard.read_json(self.root / "state.json")
            if state["restore_count"] and state["image_digest"] == self.configuration["rollback"]["image_digest"]:
                break
            time.sleep(0.1)
        self.assertTrue(server.is_alive())
        state = guard.read_json(self.root / "state.json")
        self.assertEqual(state["image_digest"], self.configuration["rollback"]["image_digest"])
        client = executor.create_executor("arm64-isolated", self.configuration, self.context)
        recovered = client.recover_pending({"reason": "resume"})
        self.assertFalse(recovered["blocked"])
        self.assertEqual(recovered["operations"][0]["status"], "restored")

    def test_committed_service_restart_replays_core_without_switching(self) -> None:
        server = self.start_guard()
        client = executor.create_executor("arm64-isolated", self.configuration, self.context)
        ticket = client.prepare_stage(prepare_request(self.environment))
        raw = client.run_stage(ticket)
        path = self.root / "publication" / "verifier.json"
        guard.write_json_once(path, {"scope": "regression_fixture"})
        decision = {"result": "pass", "observation_sha256": canonical_sha256(raw), "verifier_binding": guard.binding(self.root, path), "reason": "专项复核"}
        core = client.finish_stage(ticket, decision)
        server.kill()
        server.join(timeout=3)
        self.start_guard()
        restarted = executor.create_executor("arm64-isolated", self.configuration, self.context)
        same_ticket = restarted.prepare_stage(prepare_request(self.environment))
        self.assertEqual(same_ticket, ticket)
        self.assertEqual(restarted.finish_stage(same_ticket, {}), core)
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 1)
        content = {key: value for key, value in core.items() if key != "origin"}
        source = restarted.verify_origin_evidence(content, core["origin"])
        self.assertEqual(source["content_sha256"], canonical_sha256(content))

    def test_freeze_second_run_rechecks_live_gate_and_zero_change(self) -> None:
        self.start_guard()
        client = executor.create_executor("arm64-isolated", self.configuration, self.context)
        request = {"campaign_id": self.context["campaign_id"], "candidate_id": self.context["candidate_id"], "delivery_id": self.context["delivery_id"], "candidate_image_digest": self.configuration["candidate"]["image_digest"], "architecture": "arm64", "runtime_profile_sha256": self.configuration["candidate"]["runtime_profile_sha256"], "observation_window_seconds": 1, "delivery_gates": list(executor.DELIVERY_GATES)}
        first = client.freeze(request)
        guard.atomic_json(self.root / "fault.json", {"execute": "normal", "restore": "normal", "freeze": "failed"})
        with self.assertRaises(guard.GuardError):
            client.freeze(request)
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 0)
        self.assertEqual(first["cores"]["freeze"]["origin"]["source_scope"], "regression_fixture")

    def test_new_delivery_cannot_reuse_old_fixed_guard_context(self) -> None:
        self.start_guard()
        changed = {**self.context, "delivery_id": "candidate-current-d1"}
        client = executor.create_executor("arm64-isolated", self.configuration, changed)
        with self.assertRaisesRegex(guard.GuardError, "另一Campaign或delivery"):
            client.prepare_stage(prepare_request(self.environment))
        self.assertEqual(guard.read_json(self.root / "state.json")["switch_count"], 0)


class ConfigurationTests(unittest.TestCase):
    def test_guard_cli_rejects_implicit_current_directory_before_reading(self) -> None:
        with self.assertRaisesRegex(guard.GuardError, "显式绝对路径"):
            guard.main(["--configuration", "relative.json", "--configuration-sha256", "a" * 64, "--execution-context", "relative-context.json"])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cgcfg-", dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.configuration, self.context, self.environment = fixture_world(self.root)

    def test_closed_configuration_rejects_wrong_host_production_shell_and_fake_profile(self) -> None:
        mutations = [lambda config: config.update(host_id="DMIT"), lambda config: config.update(command="echo fabricated"), lambda config: config["paths"].update(active_profile_relative="production_active/profile.json"), lambda config: config["runtime"].update(daemon_socket="tcp://example:2375"), lambda config: config["candidate"].update(runtime_profile_sha256="0" * 64), lambda config: config["rollback"]["qualification"].update(store_relative="other-store"), lambda config: config["probes"].update(method="POST", path="/v1/messages"), lambda config: config["probes"].update(base_url="http://user:secret@127.0.0.1:8099")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                config = copy.deepcopy(self.configuration)
                mutate(config)
                with self.assertRaises(guard.GuardError):
                    executor.validate_configuration(config)

    def test_fixture_cannot_claim_independent_formal_service(self) -> None:
        with self.assertRaises(guard.GuardError):
            executor.guard_identity(self.configuration, self.context, formal=True)
        self.assertFalse(executor.guard_identity(self.configuration, self.context, formal=False)["independent_service"])
        with self.assertRaises(guard.GuardError):
            executor.create_executor("arbitrary.module:factory", self.configuration, self.context)

    def test_qualification_view_filters_delivery_without_loading_its_semantics(self) -> None:
        old = {"object_kind": "operational_evidence", "sha256": "a" * 64}
        current = {"object_kind": "operational_evidence", "sha256": "b" * 64}
        source = mock.Mock(read_only=True, root=self.root)
        source.list_object_refs.return_value = [old, current]
        source.load_object.side_effect = lambda reference: {"payload": {"schema_version": "official-client-operational-evidence/v1" if reference == old else "official-client-operational-evidence/v2"}}
        view = executor._QualificationStoreView(source)
        self.assertEqual(view.list_object_refs("operational_evidence"), [old])
        self.assertEqual(view.list_receipt_refs("candidate_delivery"), [])
        with self.assertRaisesRegex(guard.GuardError, "交付执行证据"):
            view.load_object(current)
        with self.assertRaises(guard.GuardError):
            view.load_fact({"dimension": "deployment"})

    def inspected_runtime(self, platform_fields: dict) -> executor.DockerComposeRuntime:
        """以固定Docker原件驱动真实读取路径，不执行Docker或物理修改。"""
        runtime = executor.DockerComposeRuntime(self.configuration, self.context)
        ids = {"app": "a" * 64, "db": "b" * 64, "redis": "c" * 64}
        image_id = "sha256:" + "d" * 64
        image_name = "example/isolated@" + self.configuration["candidate"]["image_digest"]
        image = {"Id": image_id, "RepoDigests": [image_name], **platform_fields}
        containers = {}
        for service, identifier in ids.items():
            labels = {"com.docker.compose.project": self.configuration["runtime"]["compose_project"], "com.docker.compose.service": service}
            if service == "app":
                labels.update(self.configuration["runtime"]["expected_labels"])
            containers[identifier] = {"Id": identifier, "Image": image_id, "Config": {"Image": image_name, "Labels": labels}, "NetworkSettings": {"Networks": {}}, "HostConfig": {}}
        profile = guard.read_bytes(guard.check_binding(self.root, self.configuration["candidate"]["profile_binding"]))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo("profile.json")
            member.size = len(profile)
            archive.addfile(member, io.BytesIO(profile))
        runtime._compose_cache = {"services": {"app": {}, "db": {}, "redis": {}}}
        def command(arguments: list, **options: object) -> bytes:
            if arguments[0] == "compose" and "ps" in arguments:
                return ids[arguments[-1]].encode("ascii") + b"\n"
            if arguments[:2] == ["container", "inspect"]:
                return canonical_json_bytes([containers[arguments[-1]]])
            if arguments[:2] == ["image", "inspect"]:
                return canonical_json_bytes([image])
            if arguments[:2] == ["container", "cp"]:
                return buffer.getvalue()
            raise AssertionError("未允许的测试命令")
        runtime._command = mock.Mock(side_effect=command)
        return runtime

    def test_inspect_architecture_comes_from_real_image_platform(self) -> None:
        for index, architecture in enumerate(("arm64", "aarch64")):
            with self.subTest(architecture=architecture):
                runtime = self.inspected_runtime({"Os": "linux", "Architecture": architecture})
                directory = self.root / ("inspect-valid-" + str(index))
                actual, environment, originals, _ = runtime._inspect(directory)
                self.assertEqual(environment["architecture"], "linux/arm64")
                self.assertEqual(actual["image"]["state"], "known")
                original = json.loads(guard.read_bytes(directory / "application-image-inspect.json"))
                self.assertEqual(original[0]["Architecture"], architecture)
                self.assertIn(guard.binding(self.root, directory / "application-image-inspect.json"), originals)

    def test_inspect_missing_or_wrong_image_platform_refuses_without_changes(self) -> None:
        invalid = [{}, {"Os": "linux"}, {"Architecture": "arm64"}, {"Os": "windows", "Architecture": "arm64"}, {"Os": "linux", "Architecture": "amd64"}, {"Os": "linux", "Architecture": "ARM64"}]
        for index, platform_fields in enumerate(invalid):
            with self.subTest(platform=platform_fields):
                runtime = self.inspected_runtime(platform_fields)
                with self.assertRaisesRegex(guard.GuardError, "真实镜像原件"):
                    runtime._inspect(self.root / ("inspect-invalid-" + str(index)))
                arguments = [call.args[0] for call in runtime._command.call_args_list]
                self.assertFalse(any(command[:2] == ["container", "cp"] or "up" in command for command in arguments))

    def test_stable_projection_allows_application_identity_but_keeps_dependencies_and_security(self) -> None:
        compose = {"services": {"app": {"image": "app@sha256:a", "environment": {"MODE": "isolated"}, "networks": {"fixed": {"ipv4_address": "172.22.0.2"}}}, "db": {"image": "db@sha256:b", "volumes": [{"source": "db-volume", "target": "/data"}]}}}
        def container(identifier: str, image: str) -> dict:
            return {"Id": identifier, "Created": "now", "Image": image, "Config": {"Image": image, "Hostname": identifier[:12], "Env": ["ISOLATED=yes"], "Labels": {}}, "State": {"Pid": 1, "StartedAt": "now", "Status": "running"}, "HostConfig": {"Privileged": False, "Binds": ["/private/candidate:/data"]}, "NetworkSettings": {"SandboxID": identifier, "Networks": {"fixed": {"EndpointID": identifier, "NetworkID": "network-fixed", "IPAddress": "172.22.0.2", "Aliases": [identifier[:12], "fixed-service"]}}}}
        observations = {"app": container("a" * 64, "candidate-image"), "db": container("b" * 64, "db-image")}
        original = executor.stable_environment_projection(compose, observations, "app")
        changed_compose, changed_inspect = copy.deepcopy(compose), copy.deepcopy(observations)
        changed_compose["services"]["app"]["image"] = "baseline-image"
        changed_inspect["app"] = container("c" * 64, "baseline-image")
        self.assertEqual(original, executor.stable_environment_projection(changed_compose, changed_inspect, "app"))
        for target, key, value in (("db", "Image", "other-db"), ("db", "HostConfig", {"Privileged": True}), ("app", "NetworkSettings", {"Networks": {"fixed": {"IPAddress": "172.22.0.99"}}}), ("app", "Config", {"Env": ["ISOLATED=no"]})):
            modified = copy.deepcopy(observations)
            modified[target][key] = value
            self.assertNotEqual(original, executor.stable_environment_projection(compose, modified, "app"))

    def test_http_redirect_and_model_post_are_rejected_without_following(self) -> None:
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.command, self.path))
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/v1/messages")
                self.end_headers()
                self.wfile.write(b'{"status":"redirect"}')
            def log_message(self, format: str, *args: object) -> None:
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        runtime = object.__new__(executor.DockerComposeRuntime)
        runtime.root = self.root
        runtime.probes = {**self.configuration["probes"], "base_url": f"http://127.0.0.1:{server.server_port}"}
        runtime._credential = b"fixture-private-key"
        directory = self.root / "http"
        guard.private_directory(directory)
        records = []
        with self.assertRaisesRegex(guard.GuardError, "redirect"):
            runtime._http(directory, True, "/v1/models", records)
        self.assertEqual(requests, [("GET", "/v1/models")])
        with self.assertRaises(guard.GuardError):
            runtime._http(directory, True, "/v1/messages", records)
        self.assertEqual(requests, [("GET", "/v1/models")])


if __name__ == "__main__":
    unittest.main()
