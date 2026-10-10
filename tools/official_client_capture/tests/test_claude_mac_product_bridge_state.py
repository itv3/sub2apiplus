"""认证桥与固定产品工厂的状态接线短验；不启动产品或取得现场资格。

路径映射、派发认证、工厂、scope、StateJournal和ECDSA原件均走真实合同。
只在测试作用域替身受管安装读取与原生观察接点，不替换产品来源工厂。
"""
from __future__ import annotations

import base64
import shutil
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_agent_bridge as bridge
from tools.official_client_capture import claude_mac_formal_capture_contract as capture
from tools.official_client_capture import claude_mac_product_authority as authority
from tools.official_client_capture import claude_mac_product_broker as broker
from tools.official_client_capture import claude_mac_product_completion as completion
from tools.official_client_capture import claude_mac_product_flows as flows
from tools.official_client_capture import claude_mac_product_origins as origins
from tools.official_client_capture import claude_mac_product_recipe as recipes
from tools.official_client_capture import claude_mac_product_source as products
from tools.official_client_capture import claude_mac_product_state as state
from tools.official_client_capture.claude_mac_state_journal import SnapshotLimits
from tools.official_client_capture.tests import test_claude_mac_formal_bridge as bridge_fixture
from tools.official_client_capture.tests import test_claude_mac_product_materials as material_fixture
from tools.official_client_capture.tests import test_claude_mac_product_state as state_fixture
from tools.official_client_capture.tests.test_claude_mac_product_sources import process


def write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)


class ProductBridgeStateTests(unittest.TestCase):
    def setUp(self):
        fixture = bridge_fixture.FormalBridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        material = material_fixture.ProductMaterialsTests()
        material.setUp()
        self.addCleanup(material.doCleanups)
        material.run.chmod(0o700)
        public_root, facts = material.freeze("claude-desktop", material.desktop())
        for path in public_root.rglob("*"):
            if path.is_file():
                write(fixture.bundle/path.relative_to(public_root), path.read_bytes())
        self.task = fixture.task
        self.task["client_artifacts"] = facts["client_artifacts"]
        embedded = next(row for row in facts["client_artifacts"] if row["role"] == "embedded_cli")
        self.task["catalog"]["entry"].update(product=embedded["product"], version=embedded["version"]["value"],
            binary_sha256=embedded["content"]["sha256"])
        recipe = c.read_json(fixture.bundle/"isolation-recipe.json")
        recipe.update(official_support=facts["official_support"], request_policy=self.task["execution"]["request_policy"])
        write(fixture.bundle/"isolation-recipe.json", c.canonical(recipe))
        self.task["execution"]["isolation_recipe"] = c.binding(fixture.bundle/"isolation-recipe.json", fixture.bundle)
        self.task["task_sha256"] = c.digest({key: value for key, value in self.task.items() if key != "task_sha256"})
        write(fixture.task_path, c.canonical(self.task))
        fixture.engine = bridge.MacBridgeEngine(self.task, fixture.mac_install)
        fixture.prepared()
        self.root, self.session, self.physical = fixture.engine.root, fixture.engine.session, fixture.physical
        self.root.parent.chmod(0o700)
        self.session.mkdir(mode=0o700)
        for name in ("home", "user_data", "config"):
            (self.session/name).mkdir(mode=0o700)
        for path in public_root.rglob("*"):
            if path.is_file():
                write(self.root/path.relative_to(public_root), path.read_bytes())
        self.file = Path(fixture.mapping_rows[0]["physical"])
        write(self.file, b"authenticated-original\x00\xff")
        self.wrong_copy = self.session/"config"/self.file.name
        write(self.wrong_copy, b"session-copy-must-not-change")
        self.fixture, self.recipe = fixture, recipe
        self.patch(bridge, "require_mac_bridge_installation", return_value=fixture.mac_install)
        self.patch(bridge, "require_mac_agent_installation", return_value=fixture.mac_install)
        self.context = bridge.verify_bridge_path_context(self.task, self.root)
        self.dispatch = capture._verify_dispatch_envelope(self.task, fixture.envelope(), fixture.key, bundle_root=self.root)
        signer = object.__new__(state_fixture.ProductStateTests)
        signer.root, signer.task = self.root, self.task
        signer.identity = {"producer_run_id": self.task["execution"]["producer_run_id"]}
        _raw, self.public_key = signer.signature([{"fixture_only": True}, {"fixture_only": True}])
        self.signer = signer
        self.native = authority.ProductInstallationAuthority(
            {"origin_public_key_base64": base64.b64encode(self.public_key).decode()}, "a"*64,
            Path("/fixture/unexecuted"), authority._TOKEN)
        self.patch(authority.ProductInstallationAuthority, "revalidate", return_value={"installation_sha256": "a"*64})
        self.patch(authority, "load_product_installation", return_value=self.native)
        self.patch(flows, "load_network_installation", return_value=SimpleNamespace(public_key=self.public_key,
            revalidate=lambda deployment: {"installation_sha256": "b"*64}))
        self.patch(broker, "load_product_broker", return_value=SimpleNamespace(
            revalidate=lambda deployment: {"installation_sha256": "c"*64}))
        self.patch(products.platform, "system", return_value="Darwin")
        self.patch(products.platform, "machine", return_value="arm64")
        policy, execution = self.task["execution"]["request_policy"], self.task["execution"]
        self.grant = {"schema_version": products.QUALIFICATION_SCHEMA, "source_deployments": execution["source_deployments"],
            "installation_sha256s": {"native": "a"*64, "network": "b"*64, "broker": "c"*64, "bridge": fixture.mac_install.sha256},
            "not_after_utc": fixture.at(600), "subjects": [{"client_id": self.task["client_id"],
                "artifact_identity_sha256": recipes._artifact_identity(self.task),
                "official_support_sha256": recipe["official_support"]["sha256"], "recipe_id": recipe["recipe_id"],
                "state_paths_relative": [self.file.name], "credential_refs": [execution["credential_ref"]],
                "max_timeout_seconds": execution["timeout_seconds"], "max_request_budget": execution["request_budget"],
                "retry_upper_bound": policy["retry_upper_bound"], "max_concurrency": policy["max_concurrency"],
                "snapshot_limits": asdict(SnapshotLimits())}]}
        read = authority.read_original
        def installed_read(path, **kwargs):
            if Path(path) == authority.INSTALL_ROOT/"product-qualification.json":
                self.assertIs(kwargs.get("root_owned"), True)
                return c.canonical(self.grant)
            return read(path, **kwargs)
        self.patch(authority, "read_original", side_effect=installed_read)
        adapters = mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True)
        owners = mock.patch.dict(products._MAC_SOURCE._owners, {}, clear=True)
        adapters.start(); self.addCleanup(adapters.stop)
        owners.start(); self.addCleanup(owners.stop)

    def patch(self, target, name, **kwargs):
        value = mock.patch.object(target, name, **kwargs)
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def factory(self, context=None):
        return products.create_qualified_source(self.task, bundle_root=self.root,
            materials_receipt=self.root/"product-materials.json", path_context=self.context if context is None else context,
            dispatch=self.dispatch)

    def test_authenticated_pairs_factory_scope_observation_and_restore_use_same_physical_files(self):
        before = self.file.read_bytes()
        item = self.factory()
        self.addCleanup(item._release)
        self.assertIs(type(item), completion.ProductEvidenceRecorder)
        self.assertTrue(products._MAC_SOURCE.owns_recorder(item))
        self.assertEqual(item._state, self.physical)
        self.assertEqual(item._plan.frozen_paths, (self.file.name,))
        self.assertEqual(recipes.material_state_paths(self.task, self.recipe, self.root, self.context), (self.file,))
        item.prepare_state(self.task, self.root)
        journal = item._journal
        self.assertEqual(journal.state_root/journal.paths[0], self.file)
        ready = {"kind": "ready", "task_sha256": self.task["task_sha256"], "run_id": item._identity["producer_run_id"],
            "boot_id": "fixture-boot", "observed_at_utc": self.fixture.at(-1), "accessibility_trusted": False,
            "socket_events_complete": False, "module_activation_verified": False,
            "state_observation_schema": state.SCHEMA, "state_paths": [str(self.file)]}
        self.patch(item._provider._monitor, "recheck", return_value=ready)
        def acknowledge(digest):
            return {"kind": "product_scope", "scope_sha256": digest, "observed_at_utc": bridge.c.utc_now()}
        self.patch(item._provider._monitor, "authorize_state_scope", side_effect=acknowledge)
        item._ready = True
        item._fixture_event("monitor_ready", bridge.c.utc_now())
        journal.authorize_product_scope(item)
        scope_record = next(row["payload"] for row in journal._rows if row["event"] == "product_scope_intent")
        scope = journal._load(scope_record["scope"])
        self.assertEqual(scope["state_root"], str(self.physical))
        self.assertEqual(scope["paths"], [self.file.name])
        self.assertEqual(scope["path_mapping"], self.context.mapping_binding)
        ack = next(row["payload"]["native_ack"] for row in journal._rows if row["event"] == "product_scope_ack")
        application = process(100, next(row["content"]["path"] for row in self.task["client_artifacts"]
            if row["role"] == "application_executable"))
        frames = [ready, ack]
        def process_event(name, sequence):
            return {"kind": "process", "event": name, "process": application, "observed_at_utc": bridge.c.utc_now(),
                "global_sequence": sequence, "event_sequence": sequence}
        frames.append(process_event("exec", 1))
        self.file.write_bytes(b"signed-product-fixture-change")
        actual = journal._snapshot()
        snapshot = state.flatten(actual, self.physical)
        for name, sequence in (("write", 2), ("close", 3)):
            info = self.file.stat()
            frames.append({"kind": "product_state", "event": name, "paths": [str(self.file)],
                "file": ({**snapshot[str(self.file)], "device": info.st_dev, "inode": info.st_ino} if name == "close" else None),
                "requested": None, "owned": True, "process": application, "observed_at_utc": bridge.c.utc_now(),
                "global_sequence": sequence, "event_sequence": sequence})
        frames.extend([process_event("exit", 4), {"kind": "product_state_snapshot", "states": snapshot,
            "observed_at_utc": bridge.c.utc_now()}, {"kind": "closed", "task_sha256": self.task["task_sha256"],
            "run_id": item._identity["producer_run_id"], "observed_at_utc": bridge.c.utc_now(), "close_requested": True,
            "unsubscribe_result": 0, "delete_result": 0, "unclosed_instances": 0, "first_error": None,
            "drain_status": "observed_unqualified", "socket_events_complete": False, "module_activation_verified": False}])
        raw, key = self.signer.signature(frames)
        self.assertEqual(key, self.public_key)
        write(self.root/"native-events.jsonl", raw)
        item._instance, item._quiescent = application, True
        item._fixture_event("product_started", frames[2]["observed_at_utc"])
        item._fixture_event("product_stopped", frames[-3]["observed_at_utc"])
        item._append("stop_result", {"facts": {"stopped": True}, "completeness": {"status": "complete"}})
        journal.observe_product_writes()
        item.restore_state(self.task, self.root)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.wrong_copy.read_bytes(), b"session-copy-must-not-change")
        observed = origins.verify_native_original(self.task, raw, self.public_key)
        state.verify_archived_restore(self.task, self.root, observed, path_context=self.context)
        # ARM64 重放只消费归档与认证字面；Mac活动状态目录不存在也不能写回或取锁。
        item._release()
        shutil.rmtree(self.physical)
        self.fixture.capture_install = self.fixture.installation("capture")
        for path in self.root.rglob("*"):
            if path.is_file():
                write(self.fixture.bundle/path.relative_to(self.root), path.read_bytes())
        with mock.patch.object(bridge, "require_mac_bridge_installation", return_value=self.fixture.capture_install):
            context = bridge.verify_bridge_path_context(self.task, self.fixture.bundle)
            state.verify_archived_restore(self.task, self.fixture.bundle, observed, path_context=context)
        self.assertFalse(self.physical.exists())

    def test_foreign_or_tampered_mapping_never_constructs_or_starts_product(self):
        foreign = replace(self.context, state_paths=((self.context.state_paths[0][0], str(self.session/"foreign")),))
        for context in ({"task_sha256": self.task["task_sha256"], "state_paths": self.context.state_paths}, foreign):
            with self.subTest(context=type(context).__name__), self.assertRaises(ValueError), \
                 mock.patch.object(products.native.subprocess, "Popen") as spawn:
                self.factory(context)
            spawn.assert_not_called()
        self.assertFalse((self.root/"recorder-frozen.json").exists())
        self.assertEqual(self.file.read_bytes(), b"authenticated-original\x00\xff")

    def test_original_nonbridge_state_mapping_behavior_is_preserved(self):
        recipe = {"private_session_root": str(self.session), "state_paths": [str(self.wrong_copy)]}
        self.assertEqual(recipes.material_state_paths(self.task, recipe, self.root), (self.wrong_copy,))
        self.assertEqual(recipes.material_state_root(self.task, recipe, self.root), self.session)
        with self.assertRaises(recipes.ProductRecipeError):
            recipes.material_state_paths(self.task, {**recipe, "state_paths": [str(self.file)]}, self.root)


if __name__ == "__main__":
    unittest.main()
