"""产品工程专项：真实私有复制和状态恢复，夹具原件完整消费与缺件拒绝。

本测试不启动官方产品、不访问账号、不授正式来源资格。正例明确为 fixture；
实际 Darwin helper 单列私有编译，不能拿 fixture 的通过代替现场认证。
"""

from __future__ import annotations

import base64
import copy
import json
import os
import time
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_authority as authority
from tools.official_client_capture import claude_mac_product_completion as completion
from tools.official_client_capture import claude_mac_product_logs as logs
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_provider as providers
from tools.official_client_capture import claude_mac_product_recipe as recipes
from tools.official_client_capture import claude_mac_product_recorder as recorder
from tools.official_client_capture import claude_mac_product_state as product_state
from tools.official_client_capture.claude_mac_state_journal import StateJournal, SnapshotLimits, file_state
from tools.official_client_capture.tests import test_claude_mac_product_materials as material_fixture


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else c.canonical(value))
    path.chmod(0o600)
    return path


class FixtureProvider:
    """仅本测试的受管来源，原件全部真实落盘；没有 formal 旗标或执行能力。"""

    def __init__(self, task, root):
        self.task, self.root = task, root
        self.calls = []
        self.unknown_close = False
        self.missing = None
        self.instance = {"fixture_only": True, "pid": 100, "boot_id": "fixture-boot", "start_marker": "fixture-start"}

    def reply(self, phase, facts):
        self.calls.append(phase)
        value = {"task_sha256": self.task["task_sha256"], "task_nonce": self.task["task_nonce"],
            "producer_run_id": self.task["execution"]["producer_run_id"], "phase": phase,
            "deployment": self.task["execution"]["source_deployments"]["mac"], "facts": facts,
            "completeness": {"status": "unknown" if phase == "close" and self.unknown_close else "complete",
                             "first_sequence": len(self.calls), "last_sequence": len(self.calls)},
            "unknown_reasons": ["fixture-unknown-tail"] if phase == "close" and self.unknown_close else []}
        path = write(self.root / "provider-observations" / f"{len(self.calls):04d}-{phase}.json", value)
        return {**value, "raw_files": [c.binding(path, self.root)]}

    def start_monitoring(self, task, bundle_root):
        return self.reply("monitoring", {"started_at_utc": now()})

    def recheck_monitoring(self, task):
        return self.reply("recheck", {"active": True})

    def launch(self, task, plan):
        self.started = now()
        return self.reply("launch", {"process_instance": self.instance, "started_at_utc": self.started})

    def drive(self, task, scenario):
        self.scenario, self.requested = scenario, now()
        return self.reply("drive", {"scenario_id": scenario["id"]})

    def stop_owned(self, task):
        self.stopped = now()
        return self.reply("stop", {"process_instance": self.instance, "stopped": True, "stopped_at_utc": self.stopped})

    def recover_owned(self, task):
        return self.reply("recovery_owned", {"process_instance": self.instance, "stopped": True})

    def close_observations(self, task):
        return self.reply("close", {"closed": not self.unknown_close})

    def observed(self, root, name, value):
        return {**value, "observation": c.binding(write(root / "provider-observations" / name, value), root)}

    def lines(self, root, name, rows):
        return c.binding(write(root / name, b"".join(c.canonical(row) + b"\n" for row in rows)), root)

    def observation_inventory(self, task, bundle_root):
        artifacts = {row["role"]: row for row in task["client_artifacts"]}
        processes = []
        roles = ["application", *(("extension_host",) if task["client_id"] == "claude-vscode" else ()), "embedded_cli"]
        for index, role in enumerate(roles):
            artifact = artifacts["application_executable" if role == "application" else role]
            pid = 100 + index
            value = {"process_instance_id": source.process_instance_id(pid, "fixture-boot", "fixture-" + str(pid)),
                "pid": pid, "boot_id": "fixture-boot", "start_marker": "fixture-" + str(pid),
                "parent_instance_id": processes[-1]["process_instance_id"] if processes else None,
                "executable_artifact_id": artifact["artifact_id"], "executable_sha256": artifact["content"]["sha256"],
                "role": role, "session_id": "fixture-session", "started_at_utc": self.started, "exited_at_utc": self.stopped}
            processes.append(self.observed(self.root, f"process-{pid}.json", value))
        modules = []
        if task["client_id"] == "claude-vscode":
            artifact = artifacts["extension_entry"]
            value = {"module_instance_id": "fixture-extension", "artifact_id": artifact["artifact_id"],
                "host_process_instance_id": processes[1]["process_instance_id"], "entrypoint": artifact["content"]["path"],
                "loaded_at_utc": self.started, "activated_at_utc": self.started}
            modules.append(self.observed(self.root, "extension.json", value))
        write(self.root / "process-session.json", {"client_id": task["client_id"], "session_id": "fixture-session",
            "started_at_utc": self.started, "completed_at_utc": self.stopped, "processes": processes, "loaded_modules": modules})
        self.lines(self.root, "session-events.jsonl", [{"session_id": "fixture-session", "scenario_id": self.scenario["id"],
            "kind": kind, "text": text} for kind, text in (("input", self.scenario["challenge"]), ("completed", self.scenario["response_marker"]))])
        call = {"socket_call_id": "fixture-socket", "process_instance_id": processes[-1]["process_instance_id"],
            "module_instance_ids": [row["module_instance_id"] for row in modules], "session_id": "fixture-session",
            "scenario_id": self.scenario["id"], "client_endpoint": "127.0.0.1:3000", "server_endpoint": "127.0.0.1:8080",
            "opened_at_utc": self.requested, "closed_at_utc": self.stopped, "request_ordinals": [1]}
        self.lines(self.root, "socket-calls.jsonl", [self.observed(self.root, "socket.json", call)])
        capture = Path(task["execution"]["capture_root"])
        connection = {"connection_id": "fixture-connection", "client_endpoint": call["client_endpoint"],
            "server_endpoint": call["server_endpoint"], "opened_at_utc": self.requested, "closed_at_utc": self.stopped}
        connections = self.lines(capture, "connections.jsonl", [self.observed(capture, "connection.json", connection)])
        mapping = {"socket_call_id": "fixture-socket", "connection_id": "fixture-connection", "hops": [], "observed_at_utc": self.requested}
        transport = self.lines(capture, "transport-map.jsonl", [self.observed(capture, "mapping.json", mapping)])
        body = c.canonical({"model": self.scenario["model"], "messages": [{"role": "user", "content": self.scenario["challenge"]}]})
        request = {"connection_id": "fixture-connection", "request_ordinal": 1, "observed_at_utc": self.requested,
            "host": "candidate.invalid", "method": "POST", "path": "/v1/messages",
            "request_body_base64": base64.b64encode(body).decode(), "response_body_base64": base64.b64encode(self.scenario["response_marker"].encode()).decode()}
        requests = self.lines(capture, "requests.jsonl", [request])
        origins = {}
        for root, side in ((self.root, "mac"), (capture, "capture")):
            attestation = c.binding(write(root / "provider-observations/source-attestation.json", {"fixture_only": True,
                         "task_sha256": task["task_sha256"], "side": side}), root)
            origins[side] = {"task_sha256": task["task_sha256"], "deployment": task["execution"]["source_deployments"][side], "attestation": attestation}
            write(root / "source-origin.json", origins[side])
        write(capture / "capture-manifest.json", {"schema_version": c.CAPTURE_SCHEMA, "task_sha256": task["task_sha256"],
            "capture_id": task["execution"]["capture_id"], "producer_run_id": task["execution"]["producer_run_id"],
            "candidate": task["candidate"], "endpoint": task["execution"]["endpoint"], "status": "complete",
            "connections": connections, "transport_map": transport, "requests": requests,
            "reservation": c.binding(capture / "reservation.json", capture), "ready": c.binding(capture / "ready.json", capture),
            "source_origin": c.binding(capture / "source-origin.json", capture)})
        names = ["process-session.json", "session-events.jsonl", "socket-calls.jsonl", "source-origin.json"]
        if self.missing:
            names.remove(self.missing)
        return self.reply("inventory", {"files_available": True, "files": [c.binding(self.root / name, self.root) for name in names], "origin": origins["mac"]})


class FixtureSource:
    """来源只拥有本测试的原实例；换用途或换对象都拒绝。"""

    def owns_services(self, value):
        return value is self.services

    def owns_recorder(self, value):
        return value is self.recorder

    def owns_observation_provider(self, value, task, root):
        return value is self.provider and task == self.task and root == self.root

    def verify_isolation_recipe(self, task, recipe, root):
        c.require(task == self.task and task["execution"]["source_scope"] == "regression_fixture"
                  and root == self.root and recipe == c.read_json(root / "isolation-recipe.json"), "fixture 原隔离计划不符")

    def verify_budget_reservation(self, task, reservation, capture_root):
        c.require(task == self.task and capture_root == Path(task["execution"]["capture_root"]), "fixture 捕获不符")

    def verify_product_origin(self, task, root, provider, origin):
        c.require(task == self.task and root == self.root and provider is self.provider
                  and c.read_json(c.bound_file(root, origin["attestation"])) == {
                  "fixture_only": True, "task_sha256": task["task_sha256"], "side": "mac"}, "fixture 真实原件不符")


class ProductExecutionTests(unittest.TestCase):
    def setup_product(self, client_id="claude-desktop"):
        fixture = material_fixture.ProductMaterialsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.run.chmod(0o700)
        roots = fixture.desktop() if client_id == "claude-desktop" else fixture.vscode()
        original, original_material = fixture.freeze(client_id, roots, "-original")
        session = fixture.run / (client_id + "-private-session")
        staged = recipes.stage_private_product_materials(original / "product-materials.json", private_session_root=session,
                                                         output_root=fixture.run / (client_id + "-staged"))
        root = fixture.run / (client_id + "-staged")
        task = fixture.task(root, staged["material"])
        moment = datetime.now(timezone.utc)
        task["issued_at_utc"] = (moment - timedelta(minutes=1)).isoformat()
        task["execution"]["request_policy"]["deadline_utc"] = (moment + timedelta(minutes=5)).isoformat()
        config = write(session / "config/config.json", b"original-state\x00\xff")
        recipe = c.read_json(root / "isolation-recipe.json")
        recipe.update(state_paths=[str(config)], request_policy=task["execution"]["request_policy"])
        write(root / "isolation-recipe.json", recipe)
        task["execution"]["isolation_recipe"] = c.binding(root / "isolation-recipe.json", root)
        task["task_sha256"] = c.digest({key: value for key, value in task.items() if key != "task_sha256"})
        plan = recipes.derive_material_private_launch_plan(task, bundle_root=root, materials_receipt=root / "product-materials.json")
        capture = Path(task["execution"]["capture_root"])
        capture.mkdir(mode=0o700)
        budget = c.binding(write(capture / "budget.json", {"fixture_only": True}), capture)
        reserve = {"schema_version": source.RESERVATION_SCHEMA, "task_sha256": task["task_sha256"], "candidate": task["candidate"],
            "capture_id": task["execution"]["capture_id"], "producer_run_id": task["execution"]["producer_run_id"],
            "endpoint": task["execution"]["endpoint"], "request_budget": task["execution"]["request_budget"],
            "reserved_at_utc": now(), "deadline_utc": task["execution"]["request_policy"]["deadline_utc"],
            "budget_reservation": budget, "source_deployments": task["execution"]["source_deployments"]}
        reservation = c.binding(write(capture / "reservation.json", reserve), capture)
        ready = {"schema_version": source.READY_SCHEMA, "task_sha256": task["task_sha256"], "capture_id": task["execution"]["capture_id"],
                 "reservation": reservation, "deployment": task["execution"]["source_deployments"]["capture"], "ready_at_utc": now()}
        proof = c.binding(write(capture / "ready-original.json", ready), capture)
        write(capture / "ready.json", {**ready, "observation": proof})
        adapter = FixtureSource()
        adapter.task, adapter.root, adapter.services = task, root, object()
        adapter.provider = FixtureProvider(task, root)
        patch = mock.patch.dict(c.SOURCE_ADAPTERS, {task["execution"]["recorder_id"]: adapter}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        state_plan = recorder.RecorderStatePlan(session, ("config/config.json",), {}, SnapshotLimits(),
                    (recorder.StateSetupOperation("fixture-setup", {"config/config.json": file_state(b"changed-state", mode=0o600)}),), plan["plan"])
        adapter.recorder = completion.ProductEvidenceRecorder(task, bundle_root=root, services=adapter.services,
                   provider=adapter.provider, state_plan=state_plan, materials_receipt=root / "product-materials.json")
        return SimpleNamespace(fixture=fixture, material=staged, original_material=original_material, task=task,
                               root=root, session=session, config=config, plan=plan, adapter=adapter,
                               recorder=adapter.recorder, provider=adapter.provider)

    def lifecycle(self, product):
        r, task, root = product.recorder, product.task, product.root
        r.prepare_state(task, root)
        self.assertEqual(product.config.read_bytes(), b"changed-state")
        r.start_monitoring(task, root)
        # 候选总账重验的测试由原 Candidate services 专项负责，这里仅核记录器接点。
        product.adapter.services = mock.Mock()
        r._services = product.adapter.services
        r.recheck_dispatch(task)
        getattr(r, "launch_desktop" if task["client_id"] == "claude-desktop" else "launch_vscode")(task, c.read_json(root / "isolation-recipe.json"))
        r.drive_scenario(task, task["scenarios"][0])
        r.stop_product(task)
        r.restore_state(task, root)
        self.assertEqual(product.config.read_bytes(), b"original-state\x00\xff")

    def test_private_dependencies_preserve_roles_versions_and_exact_bytes(self):
        for client in ("claude-desktop", "claude-vscode"):
            with self.subTest(client=client):
                p = self.setup_product(client)
                old = {row["role"]: row for row in p.original_material["client_artifacts"]}
                new = {row["role"]: row for row in p.material["material"]["client_artifacts"]}
                self.assertEqual({role: (row["version"], row["content"]["sha256"]) for role, row in old.items()},
                                 {role: (row["version"], row["content"]["sha256"]) for role, row in new.items()})
                self.assertTrue(Path(new["embedded_cli"]["content"]["path"]).is_relative_to(p.session))
                self.assertFalse(p.material["formal_ready"])
                self.assertFalse(p.material["launch_allowed"])

    def test_bridge_preseal_never_consumes_arm_capture_or_publishes_complete_manifest(self):
        p = self.setup_product()
        self.lifecycle(p)
        def mac_inventory(task, root):
            for name, value in (("process-session.json", {"fixture_only": True}),):
                write(root/name, value)
            write(root/"session-events.jsonl", b"{\"fixture_only\":true}\n")
            attestation = c.binding(write(root/"provider-observations/mac-only-origin.json", {
                "fixture_only": True, "task_sha256": task["task_sha256"], "side": "mac"}), root)
            origin = {"task_sha256": task["task_sha256"], "deployment": task["execution"]["source_deployments"]["mac"], "attestation": attestation}
            write(root/"source-origin.json", origin)
            return p.provider.reply("inventory", {"files_available": True,
                "files": [c.binding(root/name, root) for name in ("process-session.json", "session-events.jsonl", "source-origin.json")], "origin": origin})
        p.provider.observation_inventory = mac_inventory
        with mock.patch.object(source, "verify_client_bundle_v2", side_effect=AssertionError("不得读 ARM 捕获")):
            first = p.recorder.seal_for_bridge(p.task, p.root, status="complete")
            second = p.recorder.seal_for_bridge(p.task, p.root, status="complete")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "complete")
        self.assertTrue((p.root/"product-preseal.json").is_file())
        self.assertFalse((p.root/"source-manifest.json").exists())
        raw = (p.root/first["preseal"]["path"]).read_bytes()
        self.assertEqual(len(raw), first["preseal"]["bytes"])
        self.assertNotIn("socket-calls.jsonl", {row["path"] for row in first["files"]})

    def test_unknown_bridge_tail_is_failed_without_success_origin_or_manifest(self):
        p = self.setup_product()
        self.lifecycle(p)
        p.provider.unknown_close = True
        result = p.recorder.seal_for_bridge(p.task, p.root, status="complete")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["origin"])
        self.assertFalse((p.root/"source-manifest.json").exists())

    def test_complete_fixture_bundle_replays_and_repeated_seal_never_redispatches(self):
        for client in ("claude-desktop", "claude-vscode"):
            with self.subTest(client=client):
                p = self.setup_product(client)
                self.lifecycle(p)
                p.recorder.seal_sources(p.task, p.root, status="complete")
                verdict = source.verify_client_bundle_v2(p.task, p.root, Path(p.task["execution"]["capture_root"]))
                self.assertEqual(verdict["status"], "verified")
                self.assertEqual(verdict["source_scope"], "regression_fixture")
                original, calls = (p.root / "source-manifest.json").read_bytes(), list(p.provider.calls)
                p.recorder.seal_sources(p.task, p.root, status="complete")
                self.assertEqual((p.root / "source-manifest.json").read_bytes(), original)
                self.assertEqual(p.provider.calls, calls)
                self.assertIsNone(p.recorder._journal)
                self.assertFalse(any(path.is_symlink() for path in p.root.rglob("*")))

    def test_missing_product_original_or_unknown_tail_never_publishes_success(self):
        for missing in ("process-session.json", "socket-calls.jsonl", None):
            with self.subTest(missing=missing):
                p = self.setup_product()
                self.lifecycle(p)
                p.provider.missing, p.provider.unknown_close = missing, missing is None
                with self.assertRaises(ValueError):
                    p.recorder.seal_sources(p.task, p.root, status="complete")
                self.assertFalse((p.root / "source-manifest.json").exists())
                self.assertTrue((p.root / recorder._FIRST).exists())
                self.assertIsNone(p.recorder._journal)

    def test_fixture_cannot_be_renamed_formal_or_execute_before_registration(self):
        p = self.setup_product()
        task = copy.deepcopy(p.task)
        task["execution"]["source_scope"] = "formal"
        task["task_sha256"] = c.digest({key: value for key, value in task.items() if key != "task_sha256"})
        formal = completion.ProductEvidenceRecorder(task, bundle_root=p.root, services=p.adapter.services,
                    provider=p.provider, state_plan=p.recorder._plan, materials_receipt=p.root / "product-materials.json")
        p.adapter.recorder = formal
        p.adapter.task = task
        with mock.patch.object(os, "open", side_effect=AssertionError("不得先取锁")), self.assertRaisesRegex(ValueError, "formal 权"):
            formal.prepare_state(task, p.root)
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True), mock.patch.object(os, "open", side_effect=AssertionError("不得先取锁")), self.assertRaises(ValueError):
            p.recorder.prepare_state(p.task, p.root)
        self.assertEqual(p.provider.calls, [])

    def test_current_source_tampering_fails_read_only_repeat(self):
        p = self.setup_product()
        self.lifecycle(p)
        p.recorder.seal_sources(p.task, p.root, status="complete")
        calls = list(p.provider.calls)
        write(p.root / "session-events.jsonl", b"{}\n")
        with self.assertRaises(ValueError):
            p.recorder.seal_sources(p.task, p.root, status="complete")
        self.assertEqual(p.provider.calls, calls)

    def test_forged_installation_authority_refuses_before_system_tools(self):
        forged = authority.ProductInstallationAuthority({}, "a" * 64, Path("/tmp/arbitrary"), object())
        with mock.patch.object(authority.subprocess, "run", side_effect=AssertionError("不得执行系统工具")), self.assertRaises(ValueError):
            forged.revalidate({})

    def test_material_plan_cannot_add_argv_environment_or_replace_application(self):
        p = self.setup_product("claude-vscode")
        for mutate in (lambda plan: plan["argv"].append("--inspect-extensions=12345"),
                       lambda plan: plan["env_names"].append("DYLD_INSERT_LIBRARIES"),
                       lambda plan: plan.update(executable="/bin/sh")):
            plan = copy.deepcopy(p.plan["plan"])
            mutate(plan)
            with self.assertRaises(ValueError):
                recipes.validate_material_private_launch_plan(p.task, plan, bundle_root=p.root,
                                                               materials_receipt=p.root / "product-materials.json")

    def native_process(self, p):
        artifact = next(row for row in p.task["client_artifacts"] if row["role"] == "embedded_cli")
        info = Path(artifact["content"]["path"]).stat()
        return {"pid": 101, "ppid": 100, "pidversion": 8, "boot_id": "fixture-boot", "start_seconds": 100,
            "start_microseconds": 1, "executable": artifact["content"]["path"], "cdhash": "a" * 40,
            "device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "audit_token": [0, 0, 0, 0, 0, 101, 0, 8], "parent_audit_token": [0, 0, 0, 0, 0, 100, 0, 7]}

    def product_log(self, p):
        scenario = p.task["scenarios"][0]
        path = p.session / "config/projects/fixture/session.jsonl"
        rows = [{"type": kind, "sessionId": "fixture-session", "cwd": p.plan["plan"]["private_roots"]["home"],
            "timestamp": now(), "message": {"role": kind, "content": [{"type": "text", "text": text}],
            **({"stop_reason": "end_turn"} if kind == "assistant" else {})}}
            for kind, text in (("user", scenario["challenge"]), ("assistant", scenario["response_marker"]))]
        write(path, b"".join(c.canonical(row) + b"\n" for row in rows))
        info = path.stat()
        process = self.native_process(p)
        frames = [{"kind": "process", "event": "exec", "process": process, "observed_at_utc": now(),
                   "global_sequence": 1, "event_sequence": 1},
                  {"kind": "product_file", "process": process, "path": str(path), "device": info.st_dev,
                   "inode": info.st_ino, "size": info.st_size, "observed_at_utc": now(), "global_sequence": 2, "event_sequence": 1}]
        return path, frames

    def test_product_transcript_requires_real_writer_exec_and_original_file_instance(self):
        p = self.setup_product()
        path, frames = self.product_log(p)
        observed = logs.read_product_scenario(p.task, bundle_root=p.root, plan=p.plan["plan"],
                                             scenario=p.task["scenarios"][0], native_frames=frames)
        self.assertEqual([row["kind"] for row in observed["session_events"]], ["input", "completed"])
        with self.assertRaises(logs.ProductLogUnavailable):
            logs.read_product_scenario(p.task, bundle_root=p.root, plan=p.plan["plan"],
                                       scenario=p.task["scenarios"][0], native_frames=frames[1:])
        replacement = path.with_suffix(".replacement")
        write(replacement, path.read_bytes())
        os.replace(replacement, path)
        with self.assertRaises(logs.ProductLogError):
            logs.read_product_scenario(p.task, bundle_root=p.root, plan=p.plan["plan"],
                                       scenario=p.task["scenarios"][0], native_frames=frames)

    def test_native_samples_cannot_promote_complete_socket_or_module_capability(self):
        p = self.setup_product()
        ready = {"kind": "ready", "task_sha256": p.task["task_sha256"], "run_id": p.task["execution"]["producer_run_id"],
            "boot_id": "fixture-boot", "observed_at_utc": now(), "accessibility_trusted": False,
            "socket_events_complete": False, "module_activation_verified": False}
        self.assertEqual(native.validate_native_frame(ready, p.task), ready)
        for key in ("socket_events_complete", "module_activation_verified"):
            forged = {**ready, key: True}
            with self.assertRaises(native.ProductNativeError):
                native.validate_native_frame(forged, p.task)

    def test_native_invalid_or_oversized_output_preserves_raw_and_closes_control(self):
        for raw in (b'{"kind":"forged"}\n', b"x" * 1_100_000):
            with self.subTest(bytes=len(raw)):
                p = self.setup_product()
                monitor = native.DarwinProductMonitor(p.task, bundle_root=p.root,
                    authority=authority.ProductInstallationAuthority({}, "a" * 64, Path("/tmp/unexecuted"), object()))
                read_fd, write_fd = os.pipe()
                input_fd, control_fd = os.pipe()
                output = os.fdopen(read_fd, "rb", buffering=0)
                control = os.fdopen(control_fd, "wb", buffering=0)
                self.addCleanup(output.close)
                self.addCleanup(control.close)
                self.addCleanup(os.close, input_fd)
                monitor.process = SimpleNamespace(stdout=output, stdin=control)
                monitor._clock_deadline = time.monotonic() + 10
                monitor._fd = os.open(p.root / "native-events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                def writer():
                    try:
                        view = memoryview(raw)
                        while view:
                            count = os.write(write_fd, view)
                            view = view[count:]
                    except BrokenPipeError:
                        pass
                    finally:
                        os.close(write_fd)
                thread = threading.Thread(target=writer, daemon=True)
                thread.start()
                monitor._read()
                output.close()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(control.closed)
                self.assertIsNotNone(monitor._failure)
                self.assertTrue((p.root / "native-events.jsonl").read_bytes())
                self.assertEqual(monitor.records, [])

    def test_same_nonce_recovery_uses_state_journal_and_never_relaunches(self):
        p = self.setup_product()
        p.recorder.prepare_state(p.task, p.root)
        p.recorder.stop_product(p.task)
        p.recorder.seal_sources(p.task, p.root, status="failed")
        before = list(p.provider.calls)
        recovered = p.recorder.recover(p.task, p.root)
        self.assertEqual(recovered["status"], "restored")
        self.assertEqual(p.config.read_bytes(), b"original-state\x00\xff")
        self.assertEqual(p.provider.calls, before)
        self.assertFalse((p.root / "source-manifest.json").exists())

    def test_source_revocation_releases_state_lock_without_writing_success(self):
        p = self.setup_product()
        p.recorder.prepare_state(p.task, p.root)
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True), self.assertRaises(ValueError):
            p.recorder.seal_sources(p.task, p.root, status="complete")
        self.assertIsNone(p.recorder._journal)
        self.assertFalse((p.root / "source-manifest.json").exists())

    def test_bridge_preseal_revocation_releases_state_lock_without_preseal(self):
        p = self.setup_product()
        p.recorder.prepare_state(p.task, p.root)
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True), self.assertRaises(ValueError):
            p.recorder.seal_for_bridge(p.task, p.root, status="complete")
        self.assertIsNone(p.recorder._journal)
        self.assertFalse((p.root / "product-preseal.json").exists())

    def test_repeated_recovery_result_rehydrates_only_and_never_reissues_owned_call(self):
        p = self.setup_product()
        self.lifecycle(p)
        original = p.provider.reply("recovery_owned", {"process_instance": p.provider.instance,
            "stopped": True, "stopped_at_utc": p.provider.stopped})
        p.recorder._append("recovery_owned_intent", {})
        p.recorder._append("recovery_owned_result", original)
        p.recorder._release()
        p.provider.rehydrate_recovery = mock.Mock()
        before = list(p.provider.calls)
        # 原件为 fixture，只有固定工厂所有权接点替身化；StateJournal 和旧返回
        # 原件绑定仍运行本体，直接检查正式恢复工程分支，不登记正式来源。
        with mock.patch.object(product_state, "scope_context", return_value={"fixture_only": True}), \
             mock.patch.object(StateJournal, "observe_product_writes"), \
             mock.patch.object(p.provider, "recover_owned", side_effect=AssertionError("历史回收不得重发")):
            first = p.recorder._recover_formal(p.task, p.root)
            second = p.recorder._recover_formal(p.task, p.root)
        self.assertEqual(first["status"], "restored")
        self.assertEqual(second["status"], "restored")
        self.assertEqual(p.provider.calls, before)
        self.assertEqual(p.provider.rehydrate_recovery.call_count, 2)
        p.provider.rehydrate_recovery.assert_called_with(p.task, original)
        self.assertFalse(first["launch_allowed"])
        self.assertFalse(first["new_request_dispatched"])

    def test_unverified_product_origin_is_rejected_before_manifest_publication(self):
        p = self.setup_product()
        self.lifecycle(p)
        with mock.patch.object(p.adapter, "verify_product_origin", side_effect=ValueError("fixture 来源拒绝")), self.assertRaises(ValueError):
            p.recorder.seal_sources(p.task, p.root, status="complete")
        self.assertFalse((p.root / "source-manifest.json").exists())
        self.assertTrue((p.root / recorder._FIRST).exists())

    def test_registered_but_unqualified_provider_refuses_before_native_or_product_start(self):
        p = self.setup_product()
        provider = providers.DarwinProductObservationProvider(p.task, bundle_root=p.root,
             materials_receipt=p.root / "product-materials.json",
             authority=authority.ProductInstallationAuthority({}, "a" * 64, Path("/tmp/unexecuted"), object()))
        p.adapter.provider = provider
        with mock.patch.object(providers.subprocess, "Popen", side_effect=AssertionError("不能启动")), self.assertRaises(providers.ProductProviderUnavailable):
            provider.start_monitoring(p.task, p.root)
        self.assertFalse((p.root / "native-events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
