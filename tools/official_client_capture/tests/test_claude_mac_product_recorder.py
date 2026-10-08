"""真实临时文件系统与故障专项；产品 provider 只在 fixture 作用域替身。

不启动目标产品、不读环境／凭据、不联网、不发信号。StateJournal、文件原件、
锁、只写一次快照与未知状态拒绝均运行本体，不能以 mock 替代恢复证明。
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_recipe as recipes
from tools.official_client_capture import claude_mac_product_recorder as recorder_tools
from tools.official_client_capture import claude_mac_recorder as ordering
from tools.official_client_capture.claude_mac_state_journal import SnapshotLimits, StateConflict, StateJournal, file_state


def _write(path: Path, value: object) -> None:
    path.write_bytes(c.canonical(value))
    path.chmod(0o600)


class _FixtureProvider:
    """只写 temp 中明确标为回归的模拟观测，不代表真实产品参与。"""

    def __init__(self, task: dict, root: Path) -> None:
        self.task, self.root = task, root
        self.calls: list[str] = []
        self.failures: dict[str, BaseException] = {}
        self.stop_unknown = False
        self.instance = {"pid": 4242, "boot_id": "fixture-boot", "start_marker": "fixture-start"}

    def _reply(self, phase: str, facts: dict) -> dict:
        self.calls.append(phase)
        if phase in self.failures:
            raise self.failures[phase]
        directory = self.root / "provider-observations"
        directory.mkdir(mode=0o700, exist_ok=True)
        envelope = {"task_sha256": self.task["task_sha256"], "task_nonce": self.task["task_nonce"],
                    "producer_run_id": self.task["execution"]["producer_run_id"],
                    "deployment": self.task["execution"]["source_deployments"]["mac"], "phase": phase,
                    "facts": facts, "completeness": {"status": "unknown" if phase == "stop" and self.stop_unknown else "complete",
                                                       "first_sequence": len(self.calls), "last_sequence": len(self.calls)},
                    "unknown_reasons": ["fixture 原实例未知"] if phase == "stop" and self.stop_unknown else []}
        path = directory / f"{len(self.calls):03d}-{phase}.json"
        _write(path, envelope)
        return {**envelope, "raw_files": [c.binding(path, self.root)]}

    def start_monitoring(self, task, bundle_root):
        return self._reply("monitoring", {"started_at_utc": datetime.now(timezone.utc).isoformat()})

    def recheck_monitoring(self, task):
        return self._reply("recheck", {"active": True})

    def launch(self, task, plan):
        return self._reply("launch", {"process_instance": self.instance, "started_at_utc": datetime.now(timezone.utc).isoformat()})

    def drive(self, task, scenario):
        return self._reply("drive", {"scenario_id": scenario["id"], "input": scenario["challenge"], "output": scenario["response_marker"]})

    def stop_owned(self, task):
        return self._reply("stop", {"process_instance": self.instance, "stopped": not self.stop_unknown,
                                    "stopped_at_utc": datetime.now(timezone.utc).isoformat()})

    def close_observations(self, task):
        return self._reply("close", {"closed": True})

    def recover_owned(self, task):
        return self._reply("recovery_owned", {"process_instance": self.instance, "stopped": True})

    def observation_inventory(self, task, bundle_root):
        return self._reply("inventory", {"files_available": True})


class _FixtureAdapter:
    """只拥有本测试创建的实例，不用类型、属性或权限旗标授信。"""

    def owns_recorder(self, value):
        return value is self.recorder

    def owns_services(self, value):
        return value is self.services

    def owns_observation_provider(self, value, task, root):
        return value is self.provider and task == self.task and root == self.root

    def verify_isolation_recipe(self, task, recipe, root):
        if task != self.task or root != self.root or recipe != c.read_json(root / "isolation-recipe.json"):
            raise ValueError("fixture 隔离原件不符")


class ProductRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outer = Path(temporary.name).resolve()
        self.run = self.outer / "run"
        self.bundle = self.run / "source"
        self.session = self.run / "private"
        self.state = self.session / "config"
        self.capture = self.outer / "capture"
        for path in (self.run, self.bundle, self.session, self.state, self.capture):
            path.mkdir(mode=0o700)
        self.config = self.state / "config.json"
        self.config.write_bytes(b"original\x00\xff")
        self.config.chmod(0o600)
        self.original_mtime = self.config.stat().st_mtime_ns
        self.task = self._task("claude-vscode")
        self._freeze_recipe_and_plan()
        self._capture_material()
        self.provider = _FixtureProvider(self.task, self.bundle)
        self.services = mock.Mock(spec=["recheck_dispatch"])
        self.adapter = _FixtureAdapter()
        self.adapter.task, self.adapter.root = self.task, self.bundle
        self.adapter.provider, self.adapter.services = self.provider, self.services
        self.adapter.recorder = None
        patch = mock.patch.dict(c.SOURCE_ADAPTERS, {self.task["execution"]["recorder_id"]: self.adapter}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        template_patch = mock.patch.object(recipes, "_REVIEWED_TEMPLATES", (self.template,))
        template_patch.start()
        self.addCleanup(template_patch.stop)
        self.recorder = self.build()

    def _task(self, client_id: str) -> dict:
        product = "claude-desktop" if client_id == "claude-desktop" else "claude-code-vscode"
        roles = [("application", "app_bundle", "product"), ("application_executable", "host_executable", "host"),
                 ("embedded_cli", "embedded_cli", "embedded_cli")]
        if client_id == "claude-vscode":
            roles += [("extension_host", "host_executable", "host"), ("extension_package", "extension_package", "extension_package"),
                      ("extension_entry", "extension_entry", "extension_package")]
        artifacts = [{"artifact_id": role, "kind": kind, "product": product, "role": role,
                      "version": {"kind": version, "value": "fixture-cli" if role == "embedded_cli" else "fixture-version"},
                      "platform": "darwin/arm64", "content": {"kind": "directory" if kind in {"app_bundle", "extension_package"} else "file",
                          "path": str(self.outer / (role + ".fixture")), "sha256": "2" * 64 if role == "embedded_cli" else "1" * 64, "bytes": 1},
                      "official_source": {"path": "support.json", "sha256": "3" * 64, "bytes": 1}} for role, kind, version in roles]
        now = datetime.now(timezone.utc)
        return {"schema_version": c.TASK_SCHEMA, "client_id": client_id, "campaign_id": "campaign-fixture",
                "candidate_id": "candidate-fixture", "attempt_id": "attempt-fixture", "run_nonce": "run-fixture",
                "candidate_revision": 1, "candidate": {key: "a" * 64 for key in ("source_tree_sha256", "build_receipt_sha256", "image_digest", "profile_digest", "gateway_sha256")},
                "catalog": {"sha256": "a" * 64, "entry": {"client_id": client_id + "-fixture", "product": product,
                    "version": "fixture-cli", "platform": "darwin/arm64", "binary_sha256": "2" * 64}},
                "client_artifacts": artifacts, "scenarios": [{"id": "scenario-1", "model": "fixture-model",
                    "challenge": "fixture-challenge-12345678", "response_marker": "fixture-response", "minimum_requests": 1}],
                "scenario_reuse": [], "issued_at_utc": (now - timedelta(minutes=1)).isoformat(), "task_nonce": "task-fixture",
                "execution": {"endpoint": "http://fixture.invalid", "credential_ref": "private-ref:never-read",
                    "timeout_seconds": 600, "request_budget": 3, "recorder_id": "fixture-product-recorder", "capture_id": "capture-fixture",
                    "producer_run_id": "producer-fixture", "source_scope": "regression_fixture", "capture_root": str(self.capture),
                    "project_ledger": str(self.outer / "unused-fixture-ledger"),
                    "source_deployments": {side: {"deployment_id": side + "-fixture", "tool_identity_sha256": "a" * 64} for side in ("mac", "capture")},
                    "isolation_recipe": {}, "request_policy": {"methods": ["POST"], "model_paths": sorted(c.MODEL_ENDPOINTS),
                        "retry_upper_bound": 0, "max_concurrency": 1, "deadline_utc": (now + timedelta(minutes=5)).isoformat()}}, "task_sha256": ""}

    def _freeze_recipe_and_plan(self) -> None:
        _write(self.bundle / "support.json", {"fixture_only": True})
        execution = self.task["execution"]
        recipe = {"schema_version": "claude-upgrade-client-isolation-recipe/v2", "client_id": self.task["client_id"],
                  "deployment": execution["source_deployments"]["mac"], "official_support": c.binding(self.bundle / "support.json", self.bundle),
                  "endpoint": execution["endpoint"], "credential_ref": execution["credential_ref"],
                  "private_session_root": str(self.session), "state_paths": [str(self.config)],
                  "recipe_id": "fixture-private-recipe", "request_policy": execution["request_policy"]}
        _write(self.bundle / "isolation-recipe.json", recipe)
        execution["isolation_recipe"] = c.binding(self.bundle / "isolation-recipe.json", self.bundle)
        self.task["task_sha256"] = c.digest({key: value for key, value in self.task.items() if key != "task_sha256"})
        executable = next(row["content"]["path"] for row in self.task["client_artifacts"] if row["role"] == "application_executable")
        launch_plan = {"client_id": self.task["client_id"], "task_sha256": self.task["task_sha256"],
                       "recipe_sha256": execution["isolation_recipe"]["sha256"], "executable": executable,
                       "argv": [executable, "--fixture-private-config", str(self.state)], "env_names": ["CLAUDE_CONFIG_DIR"],
                       "private_roots": {"config": str(self.state)}, "installation_mode": "standard",
                       "official_support_bindings": [recipe["official_support"]]}
        self.template = recipes._ReviewedLaunchTemplate(recipe["recipe_id"], self.task["client_id"], "regression_fixture",
                    recipes._artifact_identity(self.task), recipe["official_support"]["sha256"], "standard",
                    ("{executable}", "--fixture-private-config", "{config}"), ("CLAUDE_CONFIG_DIR",), ("config",))
        self.plan = recorder_tools.RecorderStatePlan(self.state, ("config.json",), {}, SnapshotLimits(),
            (recorder_tools.StateSetupOperation("setup-fixture", {"config.json": file_state(b"changed", mode=0o640)}),), launch_plan)

    def _capture_material(self) -> None:
        execution = self.task["execution"]
        _write(self.capture / "budget.json", {"fixture_only": True})
        reservation = {"schema_version": source.RESERVATION_SCHEMA, "task_sha256": self.task["task_sha256"], "candidate": self.task["candidate"],
                       "capture_id": execution["capture_id"], "producer_run_id": execution["producer_run_id"], "endpoint": execution["endpoint"],
                       "request_budget": execution["request_budget"], "reserved_at_utc": self.task["issued_at_utc"],
                       "deadline_utc": execution["request_policy"]["deadline_utc"], "budget_reservation": c.binding(self.capture / "budget.json", self.capture),
                       "source_deployments": execution["source_deployments"]}
        _write(self.capture / "reservation.json", reservation)
        ready = {"schema_version": source.READY_SCHEMA, "task_sha256": self.task["task_sha256"], "capture_id": execution["capture_id"],
                 "reservation": c.binding(self.capture / "reservation.json", self.capture), "deployment": execution["source_deployments"]["capture"],
                 "ready_at_utc": self.task["issued_at_utc"]}
        _write(self.capture / "ready-observation.json", ready)
        _write(self.capture / "ready.json", {**ready, "observation": c.binding(self.capture / "ready-observation.json", self.capture)})

    def build(self, *, task=None, plan=None):
        result = recorder_tools.NativeProductRecorder(task or self.task, bundle_root=self.bundle, services=self.services,
                                                      provider=self.provider, state_plan=plan or self.plan)
        self.adapter.recorder = result
        return result

    def identity(self) -> dict:
        return {"task_sha256": self.task["task_sha256"], "task_nonce": self.task["task_nonce"],
                "producer_run_id": self.task["execution"]["producer_run_id"],
                "deadline_utc": self.task["execution"]["request_policy"]["deadline_utc"]}

    def assert_lock_released(self, previous=None) -> None:
        self.assertIsNone(self.recorder._journal)
        if previous is not None:
            self.assertIsNone(previous._lock_fd)
            self.assertIsNone(previous._state_fd)
        with StateJournal(self.identity(), self.bundle / "state-journal", self.state, ("config.json",)):
            pass

    def hashes(self, root: Path) -> dict:
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}

    def prepare(self):
        self.recorder.prepare_state(self.task, self.bundle)
        return self.recorder._journal

    def launch(self):
        self.prepare()
        self.recorder.start_monitoring(self.task, self.bundle)
        self.recorder.recheck_dispatch(self.task)
        self.recorder.launch_vscode(self.task, c.read_json(self.bundle / "isolation-recipe.json"))

    def first(self):
        return c.read_json(self.bundle / recorder_tools._FIRST)

    def partial_without_product(self):
        self.prepare()
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        return self.hashes(self.bundle / "diagnostic/first-partial")

    def test_constructor_is_pure_and_formal_refuses_before_effects(self):
        before = self.hashes(self.outer)
        with mock.patch.object(os, "open", side_effect=AssertionError("构造不打开FD")), mock.patch.object(Path, "mkdir", side_effect=AssertionError("构造不写目录")):
            self.recorder = self.build()
        self.assertEqual(self.hashes(self.outer), before)
        task = copy.deepcopy(self.task)
        task["execution"]["source_scope"] = "formal"
        task["task_sha256"] = c.digest({key: value for key, value in task.items() if key != "task_sha256"})
        formal = self.build(task=task)
        recipe = c.read_json(self.bundle / "isolation-recipe.json")
        calls = [lambda: formal.prepare_state(task, self.bundle), lambda: formal.start_monitoring(task, self.bundle),
                 lambda: formal.recheck_dispatch(task), lambda: formal.launch_desktop(task, recipe), lambda: formal.launch_vscode(task, recipe),
                 lambda: formal.drive_scenario(task, task["scenarios"][0]), lambda: formal.stop_product(task),
                 lambda: formal.restore_state(task, self.bundle), lambda: formal.seal_sources(task, self.bundle, status="complete"),
                 lambda: formal.recover(task, self.bundle)]
        with mock.patch.object(os, "open", side_effect=AssertionError("formal不取锁或打开文件")), mock.patch.object(self.adapter, "owns_recorder", side_effect=AssertionError("formal不调所有权回调")):
            for call in calls:
                with self.subTest(call=call), self.assertRaises(recorder_tools.ProductRecorderUnavailable):
                    call()
        self.assertEqual(self.hashes(self.outer), before)
        self.assertEqual(self.provider.calls, [])

    def test_formal_seal_preserves_matching_frozen_context_and_valid_wal(self):
        task = copy.deepcopy(self.task)
        task["execution"]["source_scope"] = "formal"
        task["task_sha256"] = c.digest({key: value for key, value in task.items() if key != "task_sha256"})
        formal = self.build(task=task)
        (self.bundle / "diagnostic").mkdir(mode=0o700)
        _write(self.bundle / recorder_tools._FROZEN, formal._frozen)
        body = {"sequence": 1, "previous_sha256": "0" * 64, "identity": formal._identity,
                "event": "prepare_intent", "payload": {}, "observed_at_utc": datetime.now(timezone.utc).isoformat()}
        row = {**body, "sha256": c.digest(body)}
        wal = self.bundle / recorder_tools._WAL
        wal.write_bytes(c.canonical(row) + b"\n")
        wal.chmod(0o600)
        # 准备与 formal 实例完整匹配的合法原件，不能靠损坏 WAL 掩盖异常路径写入。
        self.assertEqual(formal._rows(), [row])
        before_paths = set(self.outer.rglob("*"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before_paths if path.is_file()}
        with mock.patch.object(self.adapter, "owns_recorder", wraps=self.adapter.owns_recorder) as owns_recorder, \
             mock.patch.object(self.adapter, "owns_services", wraps=self.adapter.owns_services) as owns_services, \
             mock.patch.object(self.adapter, "owns_observation_provider", wraps=self.adapter.owns_observation_provider) as owns_provider:
            with self.assertRaises(recorder_tools.ProductRecorderUnavailable):
                formal.seal_sources(task, self.bundle, status="failed")
            owns_recorder.assert_not_called()
            owns_services.assert_not_called()
            owns_provider.assert_not_called()
        self.assertEqual(set(self.outer.rglob("*")), before_paths)
        for path, expected in before.items():
            with self.subTest(path=path.relative_to(self.outer)):
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(formal._failures, [])
        self.assertIsNone(formal._journal)

    def test_seal_ownership_refusal_releases_active_fds_without_failure_write(self):
        previous = self.prepare()
        descriptors = (previous._lock_fd, previous._state_fd)
        self.assertTrue(all(type(fd) is int for fd in descriptors))
        before_paths = set(self.outer.rglob("*"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before_paths if path.is_file()}
        with mock.patch.object(self.adapter, "owns_recorder", return_value=False), self.assertRaisesRegex(ValueError, "不属于"):
            self.recorder.seal_sources(self.task, self.bundle, status="failed")
        for fd in descriptors:
            with self.subTest(fd=fd), self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(set(self.outer.rglob("*")), before_paths)
        for path, expected in before.items():
            with self.subTest(path=path.relative_to(self.outer)):
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.recorder._failures, [])
        self.assert_lock_released(previous)

    def test_nine_method_signatures_match_existing_protocol(self):
        def methods(path, name):
            tree = ast.parse(path.read_text())
            node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name)
            return {item.name: item for item in node.body if isinstance(item, ast.FunctionDef)}
        originals = methods(Path(ordering.__file__), "ProductRecorder")
        actual = methods(Path(recorder_tools.__file__), "NativeProductRecorder")
        self.assertEqual(len(originals), 9)
        for name, original in originals.items():
            self.assertEqual(ast.dump(original.args), ast.dump(actual[name].args))
            self.assertEqual(ast.dump(original.returns), ast.dump(actual[name].returns))

    def test_foreign_provider_service_or_registry_never_gets_state_lock(self):
        for name in ("owns_recorder", "owns_services", "owns_observation_provider"):
            with self.subTest(name=name), mock.patch.object(self.adapter, name, return_value=False), self.assertRaisesRegex(ValueError, "不属于"):
                self.recorder.prepare_state(self.task, self.bundle)
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True), self.assertRaisesRegex(ValueError, "未登记"):
            self.recorder.prepare_state(self.task, self.bundle)
        self.assertFalse((self.bundle / "state-journal").exists())
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_state_plan_requires_exact_recipe_set_and_rejects_global_paths(self):
        plan = recorder_tools.RecorderStatePlan(self.state, ("other.json",), {}, self.plan.limits, (), self.plan.launch_plan)
        self.recorder = self.build(plan=plan)
        with self.assertRaisesRegex(ValueError, "准确全集"):
            self.recorder.prepare_state(self.task, self.bundle)
        self.assertFalse((self.bundle / "state-journal").exists())
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_before_bridge_create_once_restores_actual_bytes_mode_mtime_and_after(self):
        previous = self.prepare()
        before = (self.bundle / "state-before.bin").read_bytes()
        self.assertEqual(before, (self.bundle / "state-journal/state-before.bin").read_bytes())
        self.assertEqual(self.config.read_bytes(), b"changed")
        self.recorder.stop_product(self.task)
        self.recorder.restore_state(self.task, self.bundle)
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(self.config.stat().st_mtime_ns, self.original_mtime)
        self.assertEqual((self.bundle / "state-after.bin").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "只写一次"):
            self.recorder._create("state-before.bin", b"replacement")
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assert_lock_released(previous)
        self.assertFalse(self.first()["formal_ready"])
        self.assertEqual(self.first()["source_scope"], "internal_state_only")

    def test_unknown_write_preserves_all_paths_without_laundering(self):
        self.prepare()
        self.config.write_bytes(b"unknown external state")
        self.recorder.stop_product(self.task)
        with self.assertRaises(StateConflict):
            self.recorder.restore_state(self.task, self.bundle)
        self.recorder.seal_sources(self.task, self.bundle, status="truncated")
        self.assertEqual(self.config.read_bytes(), b"unknown external state")
        self.assertFalse((self.bundle / "state-after.bin").exists())
        self.assert_lock_released()

    def test_one_unknown_path_does_not_restore_any_other_known_path(self):
        second = self.state / "second.json"
        second.write_bytes(b"second original")
        second.chmod(0o600)
        recipe = c.read_json(self.bundle / "isolation-recipe.json")
        recipe["state_paths"].append(str(second))
        _write(self.bundle / "isolation-recipe.json", recipe)
        self.task["execution"]["isolation_recipe"] = c.binding(self.bundle / "isolation-recipe.json", self.bundle)
        self.task["task_sha256"] = c.digest({key: value for key, value in self.task.items() if key != "task_sha256"})
        launch_plan = dict(self.plan.launch_plan)
        launch_plan.update(task_sha256=self.task["task_sha256"], recipe_sha256=self.task["execution"]["isolation_recipe"]["sha256"])
        plan = recorder_tools.RecorderStatePlan(self.state, ("config.json", "second.json"), {}, self.plan.limits,
                    (recorder_tools.StateSetupOperation("two-paths", {"config.json": file_state(b"changed"),
                                                                    "second.json": file_state(b"second changed")}),), launch_plan)
        self.recorder = self.build(plan=plan)
        self.prepare()
        self.config.write_bytes(b"unknown first path")
        self.recorder.stop_product(self.task)
        with self.assertRaises(StateConflict):
            self.recorder.restore_state(self.task, self.bundle)
        self.assertEqual(self.config.read_bytes(), b"unknown first path")
        self.assertEqual(second.read_bytes(), b"second changed")
        self.recorder.seal_sources(self.task, self.bundle, status="truncated")
        self.assert_lock_released()

    def test_service_recheck_failure_is_original_and_launch_not_called(self):
        self.prepare()
        self.recorder.start_monitoring(self.task, self.bundle)
        original = ValueError("原 Candidate 身份漂移")
        self.services.recheck_dispatch.side_effect = original
        with self.assertRaises(ValueError) as caught:
            self.recorder.recheck_dispatch(self.task)
        self.assertIs(caught.exception, original)
        self.services.recheck_dispatch.assert_called_once_with(self.task)
        self.assertNotIn("launch", self.provider.calls)
        self.recorder.stop_product(self.task)
        self.recorder.restore_state(self.task, self.bundle)
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assert_lock_released()

    def test_fixture_lifecycle_cannot_return_complete_or_formal_bundle(self):
        with self.assertRaises(recorder_tools.ProductRecorderUnavailable):
            ordering.record_product(self.task, self.bundle, recorder=self.recorder)
        self.assertEqual(self.provider.calls, ["monitoring", "recheck", "launch", "drive", "stop", "close"])
        self.assertFalse((self.bundle / "source-manifest.json").exists())
        self.assertEqual(self.first()["diagnostic_status"], "incomplete")
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assert_lock_released()

    def test_desktop_and_inherited_scenario_delegate_without_resending(self):
        self.task = self._task("claude-desktop")
        self.task["scenario_reuse"] = [{"scenario_ids": ["scenario-1"]}]
        self._freeze_recipe_and_plan()
        self._capture_material()
        self.provider.task = self.task
        self.adapter.task = self.task
        with mock.patch.object(recipes, "_REVIEWED_TEMPLATES", (self.template,)):
            self.recorder = self.build()
            with self.assertRaises(recorder_tools.ProductRecorderUnavailable):
                ordering.record_product(self.task, self.bundle, recorder=self.recorder)
        self.assertNotIn("drive", self.provider.calls)
        self.assertIn("launch", self.provider.calls)
        self.assert_lock_released()

    def test_prepare_failure_releases_descriptors_without_rewriting_original(self):
        bad = recorder_tools.StateSetupOperation("bad-setup", {"config.json": {"kind": "invented"}})
        plan = recorder_tools.RecorderStatePlan(self.state, self.plan.frozen_paths, {}, self.plan.limits, (bad,), self.plan.launch_plan)
        self.recorder = self.build(plan=plan)
        with self.assertRaises(ValueError):
            self.recorder.prepare_state(self.task, self.bundle)
        self.assert_lock_released()
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.recorder.seal_sources(self.task, self.bundle, status="failed")

    def test_stop_unknown_never_restores_or_signals_and_releases_lock(self):
        self.launch()
        previous = self.recorder._journal
        self.provider.stop_unknown = True
        with mock.patch.object(os, "kill", side_effect=AssertionError("禁止信号")):
            with self.assertRaisesRegex(ValueError, "停止未知"):
                self.recorder.stop_product(self.task)
            with self.assertRaisesRegex(ValueError, "禁止恢复"):
                self.recorder.restore_state(self.task, self.bundle)
        self.assertEqual(self.config.read_bytes(), b"changed")
        self.assert_lock_released(previous)
        self.recorder.seal_sources(self.task, self.bundle, status="failed")

    def test_partial_write_failure_releases_descriptors_and_keeps_first_error(self):
        previous = self.prepare()
        original = OSError("模拟 partial 落盘失败")
        create = self.recorder._create
        def fail_manifest(relative, raw):
            if relative == recorder_tools._FIRST:
                raise original
            return create(relative, raw)
        with mock.patch.object(self.recorder, "_create", side_effect=fail_manifest), self.assertRaises(OSError) as caught:
            self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assertIs(caught.exception, original)
        self.assert_lock_released(previous)
        self.assertFalse((self.bundle / recorder_tools._FIRST).exists())

    def test_baseexception_keeps_primary_and_finally_releases_every_descriptor(self):
        original = SystemExit("模拟监测突然退出")
        self.provider.failures["monitoring"] = original
        self.provider.failures["close"] = RuntimeError("后续 close 错误")
        with self.assertRaises(SystemExit) as caught:
            ordering.record_product(self.task, self.bundle, recorder=self.recorder)
        self.assertIs(caught.exception, original)
        phases = {item["phase"] for item in self.first()["failure_types"]}
        self.assertTrue({"monitoring", "close"} <= phases)
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assert_lock_released()

    def test_prepare_baseexception_closes_actual_state_and_lock_fds(self):
        captured = []
        original = KeyboardInterrupt("准备期突然退出")
        def fail_prepare(journal):
            captured.append(journal)
            raise original
        with mock.patch.object(StateJournal, "prepare", fail_prepare), self.assertRaises(KeyboardInterrupt) as caught:
            self.prepare()
        self.assertIs(caught.exception, original)
        self.assertEqual(len(captured), 1)
        self.assert_lock_released(captured[0])
        self.recorder.seal_sources(self.task, self.bundle, status="failed")

    def test_missing_wal_is_unknown_and_cannot_close_or_recover(self):
        self.prepare()
        (self.bundle / recorder_tools._WAL).unlink()
        with self.assertRaises((ValueError, OSError)):
            self.recorder.stop_product(self.task)
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assertEqual(self.first()["lifecycle_status"], "unknown")
        self.assertEqual(self.provider.calls, [])
        self.recorder = self.build()
        with self.assertRaisesRegex(ValueError, "历史未知"):
            self.recorder.recover(self.task, self.bundle)
        self.assertEqual(self.config.read_bytes(), b"changed")
        self.assert_lock_released()

    def test_corrupt_state_wal_is_preserved_and_does_not_restore(self):
        first_hashes = self.partial_without_product()
        path = self.bundle / "state-journal/journal.jsonl"
        path.write_bytes(path.read_bytes()[:-4])
        corrupted = path.read_bytes()
        with self.assertRaises(ValueError):
            self.recorder.recover(self.task, self.bundle)
        self.assertEqual(path.read_bytes(), corrupted)
        self.assertEqual(self.config.read_bytes(), b"changed")
        self.assertEqual(self.hashes(self.bundle / "diagnostic/first-partial"), first_hashes)
        self.assert_lock_released()

    def test_missing_or_cleanly_truncated_wal_cannot_be_no_launch(self):
        self.launch()
        path = self.bundle / recorder_tools._WAL
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        truncated = rows[:next(index for index, row in enumerate(rows) if row["event"] == "launch_intent")]
        path.write_bytes(b"".join(c.canonical(row) + b"\n" for row in truncated))
        before = list(self.provider.calls)
        with self.assertRaisesRegex(ValueError, "截短"):
            self.recorder.stop_product(self.task)
        self.assertEqual(self.provider.calls, before)
        self.assertFalse(self.recorder._quiescent)
        self.assertEqual(self.config.read_bytes(), b"changed")
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assert_lock_released()
        self.recorder = self.build()
        # 首 partial 明确记录 WAL 损坏；没有合法原历史不能借新对象认 no-launch。
        with self.assertRaises(ValueError):
            self.recorder.recover(self.task, self.bundle)

    def test_first_partial_owns_every_snapshot_and_wal_growth_keeps_bindings(self):
        initial = self.partial_without_product()
        first = self.first()
        for item in first["files"]:
            self.assertTrue(item["snapshot"]["path"].startswith("diagnostic/first-partial/snapshots/"))
            self.recorder._bound(item["snapshot"])
        wal_before = (self.bundle / "state-journal/journal.jsonl").read_bytes()
        result = self.recorder.recover(self.task, self.bundle)
        self.assertEqual(result["status"], "restored")
        self.assertNotEqual((self.bundle / "state-journal/journal.jsonl").read_bytes(), wal_before)
        self.assertEqual(self.hashes(self.bundle / "diagnostic/first-partial"), initial)
        self.recorder._verify_partial()
        self.assertFalse((self.bundle / "state-after.bin").exists())
        self.assert_lock_released()

    def test_repeated_recover_rechecks_actual_state_and_new_unknown_preserves_history(self):
        first_hashes = self.partial_without_product()
        first_result = self.recorder.recover(self.task, self.bundle)
        recovery_directory = self.bundle / ("diagnostic/recovery-" + first_result["key"])
        history = self.hashes(recovery_directory)
        original_observe = StateJournal._observe
        actual_reads = []
        def observe(journal, paths=None):
            actual_reads.append(True)
            return original_observe(journal, paths)
        with mock.patch.object(StateJournal, "_observe", observe):
            second = self.recorder.recover(self.task, self.bundle)
        self.assertGreaterEqual(len(actual_reads), 2)
        self.assertEqual(second, first_result)
        self.assertEqual(self.provider.calls, ["close"])
        self.config.write_bytes(b"new unknown after successful recovery")
        with self.assertRaises(StateConflict):
            self.recorder.recover(self.task, self.bundle)
        self.assertEqual(self.config.read_bytes(), b"new unknown after successful recovery")
        self.assertEqual(self.hashes(recovery_directory), history)
        self.assertEqual(self.hashes(self.bundle / "diagnostic/first-partial"), first_hashes)
        self.assertEqual(second["identity"]["deadline_utc"], self.identity()["deadline_utc"])
        self.assert_lock_released()

    def test_repeated_recover_refuses_changed_task_paths_snapshot_and_root_instance(self):
        self.partial_without_product()
        result = self.recorder.recover(self.task, self.bundle)
        modified = copy.deepcopy(self.task)
        modified["execution"]["source_deployments"]["mac"]["tool_identity_sha256"] = "b" * 64
        modified["task_sha256"] = c.digest({key: value for key, value in modified.items() if key != "task_sha256"})
        with self.assertRaisesRegex(ValueError, "完整原 task"):
            self.recorder.recover(modified, self.bundle)
        item = self.first()["files"][0]["snapshot"]
        snapshot = self.bundle / item["path"]
        original = snapshot.read_bytes()
        snapshot.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "快照"):
            self.recorder.recover(self.task, self.bundle)
        snapshot.write_bytes(original)
        frozen_path = self.bundle / recorder_tools._FROZEN
        frozen = frozen_path.read_bytes()
        value = json.loads(frozen)
        value["state_plan"]["paths"] = ["other.json"]
        _write(frozen_path, value)
        with self.assertRaises(ValueError):
            self.recorder.recover(self.task, self.bundle)
        frozen_path.write_bytes(frozen)
        journal = self.bundle / "state-journal"
        old = self.bundle / "old-journal"
        journal.rename(old)
        shutil.copytree(old, journal)
        with self.assertRaisesRegex(ValueError, "dev/ino"):
            self.recorder.recover(self.task, self.bundle)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")

    def test_recovery_receipt_tamper_is_not_replayed_as_success(self):
        self.partial_without_product()
        result = self.recorder.recover(self.task, self.bundle)
        receipt = self.bundle / ("diagnostic/recovery-" + result["key"]) / "manifest.json"
        modified = json.loads(receipt.read_bytes())
        modified["formal_ready"] = True
        _write(receipt, modified)
        with self.assertRaisesRegex(ValueError, "锚点"):
            self.recorder.recover(self.task, self.bundle)
        self.assert_lock_released()

    def test_source_and_private_directory_instance_changes_refuse_recovery(self):
        self.partial_without_product()
        self.recorder.recover(self.task, self.bundle)
        previous_state = self.session / "old-config"
        self.state.rename(previous_state)
        shutil.copytree(previous_state, self.state)
        with self.assertRaisesRegex(ValueError, "dev/ino"):
            self.recorder.recover(self.task, self.bundle)
        shutil.rmtree(self.state)
        previous_state.rename(self.state)
        previous_bundle = self.run / "old-source"
        self.bundle.rename(previous_bundle)
        shutil.copytree(previous_bundle, self.bundle)
        with self.assertRaisesRegex(ValueError, "dev/ino"):
            self.recorder.recover(self.task, self.bundle)
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assertIsNone(self.recorder._journal)

    def test_recovery_of_original_owned_instance_never_launches_or_renews_deadline(self):
        self.launch()
        self.provider.stop_unknown = True
        with self.assertRaises(ValueError):
            self.recorder.stop_product(self.task)
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        initial = self.hashes(self.bundle / "diagnostic/first-partial")
        self.recorder = self.build()
        result = self.recorder.recover(self.task, self.bundle)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["identity"], self.identity())
        self.assertEqual(self.provider.calls.count("launch"), 1)
        self.assertEqual(self.provider.calls.count("recovery_owned"), 1)
        self.services.recheck_dispatch.assert_called_once()
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assertEqual(self.hashes(self.bundle / "diagnostic/first-partial"), initial)
        self.assertFalse((self.bundle / "source-manifest.json").exists())
        self.assert_lock_released()

    def test_failed_drive_intent_is_not_automatically_reissued(self):
        self.launch()
        original = ValueError("原场景驱动失败")
        self.provider.failures["drive"] = original
        with self.assertRaises(ValueError) as caught:
            self.recorder.drive_scenario(self.task, self.task["scenarios"][0])
        self.assertIs(caught.exception, original)
        with self.assertRaisesRegex(ValueError, "不自动重发"):
            self.recorder.drive_scenario(self.task, self.task["scenarios"][0])
        self.assertEqual(self.provider.calls.count("drive"), 1)
        self.recorder.stop_product(self.task)
        self.recorder.restore_state(self.task, self.bundle)
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assert_lock_released()

    def test_external_capture_barrier_error_remains_primary(self):
        original = RuntimeError("独立 capture 屏障失败")
        def fail_barrier():
            raise original
        with self.assertRaises(RuntimeError) as caught:
            ordering.record_product(self.task, self.bundle, recorder=self.recorder, before_seal=fail_barrier)
        self.assertIs(caught.exception, original)
        self.assertEqual(self.first()["upstream_status"], "truncated")
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assert_lock_released()

    def test_pending_setup_recovery_does_not_execute_change_again(self):
        error = SystemExit("意图已持久化但尚未执行")
        with mock.patch.object(StateJournal, "apply_intent", side_effect=error), self.assertRaises(SystemExit):
            self.prepare()
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        with mock.patch.object(StateJournal, "apply_intent", side_effect=AssertionError("恢复不得应用setup")):
            self.assertEqual(self.recorder.recover(self.task, self.bundle)["status"], "restored")
        self.assertEqual(self.config.read_bytes(), b"original\x00\xff")
        self.assert_lock_released()

    def test_new_instance_cannot_infer_no_launch_from_an_unowned_history(self):
        self.partial_without_product()
        self.recorder = self.build()
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ValueError, "没有完整调用历史"):
                self.recorder.stop_product(self.task)
        self.assertFalse(self.recorder._quiescent)
        self.assertEqual(self.config.read_bytes(), b"changed")
        self.assertEqual(self.recorder.recover(self.task, self.bundle)["status"], "restored")
        self.assert_lock_released()

    def test_directory_probe_baseexception_closes_every_new_descriptor(self):
        self.prepare()
        original_open, original_close, original_fstat = os.open, os.close, os.fstat
        opened, closed = [], []
        probes = []
        original = KeyboardInterrupt("目录核验突然中断")
        def opened_fd(*args, **kwargs):
            value = original_open(*args, **kwargs)
            opened.append(value)
            return value
        def closed_fd(value):
            closed.append(value)
            return original_close(value)
        def fstat(value):
            probes.append(value)
            if len(probes) == 2:
                raise original
            return original_fstat(value)
        with mock.patch.object(os, "open", opened_fd), mock.patch.object(os, "close", closed_fd), mock.patch.object(os, "fstat", fstat):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.recorder._read(recorder_tools._FROZEN)
        self.assertIs(caught.exception, original)
        self.assertEqual(len(opened), 2)
        self.assertCountEqual(opened, closed)
        self.recorder.stop_product(self.task)
        self.recorder.restore_state(self.task, self.bundle)
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assert_lock_released()

    def test_unknown_close_is_not_blindly_retried_and_first_partial_never_changes(self):
        self.prepare()
        original = SystemExit("未知 close 响应")
        self.provider.failures["close"] = original
        with self.assertRaises(SystemExit) as caught:
            self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assertIs(caught.exception, original)
        before = self.hashes(self.bundle / "diagnostic/first-partial")
        self.recorder.seal_sources(self.task, self.bundle, status="failed")
        self.assertEqual(self.provider.calls, ["close"])
        self.assertEqual(self.hashes(self.bundle / "diagnostic/first-partial"), before)
        self.assert_lock_released()

    def test_partial_is_rejected_by_existing_reader_without_success_receipt(self):
        self.partial_without_product()
        with self.assertRaises(ValueError):
            source.verify_client_bundle_v2(self.task, self.bundle, self.capture)
        self.assertFalse((self.bundle / "source-manifest.json").exists())

    def test_no_environment_account_network_launch_or_signal_side_effect(self):
        patches = [mock.patch.object(os, "getenv", side_effect=AssertionError("禁止环境读取")),
                   mock.patch.object(os, "kill", side_effect=AssertionError("禁止信号")),
                   mock.patch.object(subprocess, "Popen", side_effect=AssertionError("禁止产品／命令启动")),
                   mock.patch.object(socket, "socket", side_effect=AssertionError("禁止网络")),
                   mock.patch.object(socket, "create_connection", side_effect=AssertionError("禁止连接")),
                   mock.patch.object(Path, "home", side_effect=AssertionError("禁止账号根猜测"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.partial_without_product()
        self.recorder.recover(self.task, self.bundle)
        self.assertEqual(self.provider.calls, ["close"])
        self.assert_lock_released()


if __name__ == "__main__":
    unittest.main()
