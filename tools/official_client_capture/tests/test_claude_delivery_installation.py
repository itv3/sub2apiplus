"""固定交付安装的局部测试，不运行 systemd、Docker 或远端。

安装器命令边界用显式替身，真实绑定、冻结顺序、安装解析和独立进程
来源分派保留。完整守卫原件与连续性另由既有 typed 集成测试覆盖；
这些测试不能作为 ARM64 生命周期或交付回退的现场认证。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_installation as installation
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.store import ControlStore


def local_private_file(path: Path, mode: int = 0o600) -> bytes:
    """仅去掉本机测试无法拥有的 root 要求，其余原件边界不变。"""
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise guard.GuardError("测试安装路径含符号链接")
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise guard.GuardError("测试安装原件权限不符")
    return guard.read_bytes(path)


class ManagedInstallationErrorTests(unittest.TestCase):
    """安装器自己的拒绝须进入守卫和来源消费端的既有错误合同。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-managed-install-errors-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        self.driver = self.root / "driver"
        self.data.mkdir()
        self.driver.mkdir()
        for patch in (mock.patch.object(installation, "DATA_ROOT", self.data),
                      mock.patch.object(installation, "DRIVER_ROOT", self.driver),
                      mock.patch.object(installation.platform, "system", return_value="Linux"),
                      mock.patch.object(installation.platform, "machine", return_value="aarch64"),
                      mock.patch.object(installation.os, "geteuid", return_value=0),
                      mock.patch.object(installation, "_check_directory")):
            self.enterContext(patch)

    def installer(self, stage, error_name="DriverError"):
        """仅替代安装器原件，各消费端仍走真实动态加载和异常转换。"""
        source = f'''
class DriverError(RuntimeError):
    pass
def reject(stage):
    if stage == {stage!r}:
        raise {error_name}("安装拒绝：" + stage)
def load_manifest(root):
    reject("load_manifest")
    return {{}}
def verify_tree(root, manifest, **kwargs):
    reject("verify_tree")
def verify_install(root, data, client):
    reject("verify_install")
'''.encode()
        return mock.patch.object(installation, "_private_file", return_value=source)

    def test_all_installer_rejections_keep_original_cause(self):
        for stage in ("load_manifest", "verify_tree", "verify_install"):
            with self.subTest(stage=stage), self.installer(stage):
                with self.assertRaisesRegex(guard.GuardError, "安装拒绝：" + stage) as raised:
                    installation._require_managed_install()
                self.assertEqual(type(raised.exception.__cause__).__name__, "DriverError")

    def test_single_and_batch_consumers_fail_with_control_error(self):
        for batch in (False, True):
            with self.subTest(batch=batch), self.installer("verify_tree"):
                with self.assertRaisesRegex(ControlError, "安装拒绝：verify_tree") as raised:
                    if batch:
                        installation.formal_source_verifier_batch(None, [])
                    else:
                        installation.formal_source_verifier(None, {}, {})
                self.assertIsInstance(raised.exception.__cause__, guard.GuardError)
                self.assertEqual(type(raised.exception.__cause__.__cause__).__name__, "DriverError")

    def test_unexpected_installer_failure_is_not_reclassified(self):
        with self.installer("verify_tree", "RuntimeError"):
            with self.assertRaisesRegex(RuntimeError, "安装拒绝：verify_tree") as raised:
                installation._require_managed_install()
        self.assertNotIsInstance(raised.exception, guard.GuardError)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-delivery-install-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.configuration, self.context, _ = fixture_world(self.root)
        self.instance = "candidate-fixture"
        self.unit_root = self.root / "systemd"
        self.unit_root.mkdir(mode=0o700)
        for name, original in (("executor.py", Path(executor.__file__)), ("guard.py", Path(guard.__file__))):
            guard.write_once(self.root / "managed" / name, original.read_bytes())
        module = SimpleNamespace(__file__=str(self.root / "managed/executor.py"),
                                 MATERIAL_CONFIGURATION_SCHEMA=executor.MATERIAL_CONFIGURATION_SCHEMA,
                                 validate_configuration=executor.validate_configuration,
                                 validate_execution_context=executor.validate_execution_context,
                                 create_executor=mock.Mock())
        self.module = module
        self.patches = [mock.patch.object(installation, "DATA_ROOT", self.root),
                        mock.patch.object(installation, "UNIT_ROOT", self.unit_root),
                        mock.patch.object(installation, "_require_managed_install"),
                        mock.patch.object(installation, "_private_file", side_effect=local_private_file),
                        mock.patch.object(installation, "_read_binding", side_effect=lambda item: guard.read_bytes(guard.check_binding(self.root, item))),
                        mock.patch.object(installation, "_executor", return_value=module),
                        mock.patch.object(installation, "_machine", return_value="f" * 64),
                        mock.patch.object(guard, "__file__", str(self.root / "managed/guard.py")),
                        mock.patch.object(installation, "_installation_lock", side_effect=lambda path: mock.patch.object(installation, "_machine", return_value="f" * 64))]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.systemctl = self.enterContext(mock.patch.object(installation, "_systemctl", return_value=""))
        self.kill = self.enterContext(mock.patch.object(installation.os, "kill"))
        self.alive = self.enterContext(mock.patch.object(guard, "owner_alive", return_value=True))
        self.probes = self.enterContext(mock.patch.object(installation, "_await_probe", side_effect=lambda instance, previous=None: self.sample(2 if previous else 1)))

    def sample(self, number):
        def process(pid):
            return {"boot_id": "test-boot", "pid": pid, "process_start": str(pid * 10), "process_group_id": number * 100}
        return {"systemd": {"MainPID": str(number * 100), "FragmentPath": str(self.unit_root / installation._service(self.instance)),
                             "KillMode": "control-group", "Restart": "on-failure", "NRestarts": str(number - 1),
                             "ControlGroup": "/system.slice/" + installation._service(self.instance)},
                "main": process(number * 100), "child": process(number * 100 + 1)}

    def prepare(self):
        return installation.prepare_installation(self.instance)

    def activate(self, *, fail_publish=False):
        receipt = self.prepare()
        configuration = copy.deepcopy(self.configuration)
        configuration.update(source_scope="formal", schema_version=executor.MATERIAL_CONFIGURATION_SCHEMA)
        qualification = self.root / "inputs/qualification-materials.json"
        guard.write_json_once(qualification, {"scope": "local-install-test"})
        configuration["qualification_materials_binding"] = guard.binding(self.root, qualification)
        configuration["guard"].update({key: receipt[key] for key in ("deployment_receipt_binding", "service_unit_binding", "executor_binding", "guard_binding")})
        configuration["guard"]["service_name"] = installation._service(self.instance)
        path = self.root / "inputs/formal-configuration.json"
        guard.write_json_once(path, configuration)
        context = {**self.context, "configuration_ref": guard.binding(self.root, path)}
        context_path = self.root / "inputs/formal-context.json"
        guard.write_json_once(context_path, context)
        ready = self.root / "inputs/ready.json"
        guard.write_json_once(ready, {"scope": "local-startup-boundary"})
        self.current_readiness = self.enterContext(mock.patch.object(installation, "_current_readiness", return_value=guard.binding(self.root, ready)))
        if fail_publish:
            original = guard.write_json_once
            def interrupted(path, value):
                if path.name == "enabled.json":
                    raise OSError("测试安装在启用发布前中断")
                return original(path, value)
            with mock.patch.object(guard, "write_json_once", side_effect=interrupted), self.assertRaisesRegex(OSError, "中断"):
                installation.activate_installation(self.instance, context_path)
        else:
            installation.activate_installation(self.instance, context_path)
        return configuration, context, context_path

    def test_unit_and_receipt_graph_has_no_configuration_cycle(self):
        unit = installation.render_unit(self.instance)
        self.assertNotIn(b"--configuration", unit)
        self.assertNotIn(b"sha256", unit)
        result = self.prepare()
        self.assertFalse(result["delivery_recovery_verified"])
        proof = guard.read_json(installation._directory(self.instance) / "lifecycle.json")
        self.assertEqual(proof["scope"], "systemd_process_lifecycle_only")
        self.assertNotIn("configuration_ref", proof["bootstrap"])
        self.kill.assert_called_once_with(100, installation.signal.SIGKILL)
        self.assertEqual([call.args[0] for call in self.systemctl.call_args_list], ["daemon-reload", "enable", "start", "stop"])

    def test_install_resume_reuses_finished_lifecycle_without_repeat_kill(self):
        original = guard.write_json_once
        def interrupted(path, value):
            if path.name == "deployment.json":
                raise OSError("部署收据写入前中断")
            return original(path, value)
        with mock.patch.object(guard, "write_json_once", side_effect=interrupted), self.assertRaises(OSError):
            self.prepare()
        proof = (installation._directory(self.instance) / "lifecycle.json").read_bytes()
        self.prepare()
        self.assertEqual((installation._directory(self.instance) / "lifecycle.json").read_bytes(), proof)
        self.kill.assert_called_once()

    def test_activation_interruption_resumes_but_service_never_falls_back(self):
        configuration, context, path = self.activate(fail_publish=True)
        with mock.patch.object(installation, "_bootstrap") as bootstrap, self.assertRaises(OSError):
            installation.serve_installation(self.instance)
        bootstrap.assert_not_called()
        installation.activate_installation(self.instance, path)
        loaded, current, _ = installation._load_activation(self.instance)
        self.assertEqual((loaded, current), (configuration, context))
        with self.assertRaisesRegex(guard.GuardError, "不得回退"):
            self.prepare()

    def test_enabled_configuration_damage_never_reenters_probe(self):
        configuration, context, _ = self.activate()
        path = self.root / context["configuration_ref"]["path"]
        path.write_bytes(b"broken")
        with mock.patch.object(installation, "_bootstrap") as bootstrap, self.assertRaises(guard.GuardError):
            installation.serve_installation(self.instance)
        bootstrap.assert_not_called()

    def test_activation_without_verified_readiness_never_publishes_success(self):
        _, _, path = self.activate(fail_publish=True)
        self.current_readiness.side_effect = guard.GuardError("注册链尚无有效 readiness")
        with mock.patch.object(installation, "STARTUP_TIMEOUT", 0), self.assertRaisesRegex(guard.GuardError, "启动复核超时"):
            installation.activate_installation(self.instance, path)
        directory = installation._directory(self.instance)
        self.assertTrue((directory / "activation-intent.json").is_file())
        self.assertFalse((directory / "verified.json").exists())
        with self.assertRaisesRegex(guard.GuardError, "不得回退"):
            self.prepare()

    def test_factory_binds_full_configuration_and_stable_context(self):
        configuration, context, _ = self.activate()
        installation.formal_factory("arm64-isolated", configuration, {**context, "segment": "dr1", "run_nonce": "run2"})
        self.module.create_executor.assert_called()
        for changed in ({**context, "delivery_id": "another"}, {**context, "configuration_ref": self.context["configuration_ref"]}):
            with self.assertRaisesRegex(guard.GuardError, "不一致"):
                installation.formal_factory("arm64-isolated", configuration, changed)
        changed = copy.deepcopy(configuration)
        changed["probes"]["account_ids"] = [99]
        with self.assertRaisesRegex(guard.GuardError, "不一致"):
            installation.formal_factory("arm64-isolated", changed, context)

    def test_lifecycle_rejects_self_reported_restart_or_wrong_group(self):
        self.prepare()
        bootstrap = installation._bootstrap(self.instance)
        proof = guard.read_json(installation._directory(self.instance) / "lifecycle.json")
        for mutation in (lambda value: value.update(scope="docker_rollback"),
                         lambda value: value.update(after=value["before"]),
                         lambda value: value["after"]["child"].update(process_group_id=555),
                         lambda value: value["after"]["systemd"].update(NRestarts=None)):
            changed = copy.deepcopy(proof)
            mutation(changed)
            with self.assertRaises(guard.GuardError):
                installation.validate_lifecycle(changed, bootstrap)

    def source_values(self):
        configuration, context, _ = self.activate()
        store = ControlStore.initialize(self.root / configuration["candidate"]["qualification"]["store_relative"], "2026-10-10T00:00:00Z")
        content = {"campaign_id": context["campaign_id"], "delivery_id": context["delivery_id"]}
        origin = {"authority": installation.AUTHORITY, "source_scope": "formal", "configuration_sha256": canonical_sha256(configuration),
                  "service_instance_id": "installation-dispatch-test", "operations_index_binding": None}
        return configuration, context, store, content, origin

    def test_sources_reject_cross_store_configuration_and_fixture_scope(self):
        configuration, context, store, content, origin = self.source_values()
        installation._source_reader(store, content, origin)
        cases = [(SimpleNamespace(root=self.root / "other-store"), content, origin),
                 (store, content, {**origin, "configuration_sha256": "0" * 64}),
                 (store, {**content, "campaign_id": "other"}, origin),
                 (store, content, {**origin, "source_scope": "regression_fixture"})]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(guard.GuardError):
                installation._source_reader(*arguments)

    def test_fixed_single_and_batch_routes_do_not_need_process_registry(self):
        configuration, context, store, content, origin = self.source_values()
        reader = SimpleNamespace(configuration=configuration, context=context)
        def verified(core, source):
            return {"content_sha256": canonical_sha256(core), "deployment_sha256": "f" * 64, "bytes_by_path": {},
                    "configuration_sha256": source["configuration_sha256"], "service_instance_id": source["service_instance_id"],
                    "trusted_data_root": self.root}
        reader.verify_origin_evidence = mock.Mock(side_effect=verified)
        reader.verify_origin_evidence_batch = mock.Mock(side_effect=lambda items: [verified(item["content"], item["origin"]) for item in items])
        self.module.create_executor.return_value = reader
        with mock.patch.dict(execution.SOURCE_VERIFIERS, {}, clear=True):
            verifier = execution._source_verifier(installation.AUTHORITY)
            self.assertEqual(verifier(store, content, origin).content_sha256, canonical_sha256(content))
            items = [{"content": content, "origin": origin}, {"content": {**content, "extra": 1}, "origin": origin}]
            with mock.patch.object(installation, "_require_managed_install") as verify_install, mock.patch.object(installation, "_load_activation", wraps=installation._load_activation) as load_activation:
                self.assertEqual(len(verifier.verify_batch(store, items)), 2)
                verify_install.assert_called_once()
                load_activation.assert_called_once()
            # 单项入口复用同一正式批量入口：单项一次、两项批量一次，共两次批量读取，不再走单项 RPC。
            self.assertEqual(reader.verify_origin_evidence_batch.call_count, 2)
            reader.verify_origin_evidence.assert_not_called()

    def test_fresh_process_opens_store_and_resolves_fixed_source_without_parent_registry(self):
        configuration, context, store, content, origin = self.source_values()
        input_path = self.root / "child-input.json"
        guard.write_json_once(input_path, {"root": str(self.root), "unit_root": str(self.unit_root), "store": str(store.root),
                                          "content": content, "origin": origin})
        program = '''
import json,sys
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
from tools.official_client_capture import claude_upgrade_delivery_installation as i
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as g
from tools.official_client_capture.tests.test_claude_delivery_installation import local_private_file
from tools.official_client_control import delivery_execution as e
from tools.official_client_control.store import ControlStore
from tools.official_client_control.canonical import canonical_sha256
v=json.loads(Path(sys.argv[1]).read_bytes()); root=Path(v['root'])
assert e.SOURCE_VERIFIERS == {}
# 只替代宿主安装与最终 RPC 边界；新进程实读原绑定、配置和真实 Store。
with mock.patch.object(i,'DATA_ROOT',root), mock.patch.object(i,'UNIT_ROOT',Path(v['unit_root'])), mock.patch.object(i,'_require_managed_install'), mock.patch.object(i,'_private_file',side_effect=local_private_file), mock.patch.object(i,'_read_binding',side_effect=lambda b:g.read_bytes(g.check_binding(root,b))), mock.patch.object(i,'validate_deployment_receipt'):
    store=ControlStore.open_read_only(Path(v['store']))
    configuration,context,_=i._load_activation('candidate-fixture')
    reader=SimpleNamespace(configuration=configuration,context=context,verify_origin_evidence_batch=lambda items:[dict(content_sha256=canonical_sha256(x['content']),deployment_sha256='f'*64,bytes_by_path={},configuration_sha256=x['origin']['configuration_sha256'],service_instance_id=x['origin']['service_instance_id'],trusted_data_root=root) for x in items])
    with mock.patch.object(i._executor(),'create_executor',return_value=reader):
        result=e._source_verifier(i.AUTHORITY)(store,v['content'],v['origin'])
        assert result.content_sha256 == canonical_sha256(v['content'])
print('独立进程固定来源分派通过；不计正式通道认证')
'''
        completed = subprocess.run([sys.executable, "-B", "-c", program, str(input_path)], cwd=Path(__file__).resolve().parents[3],
                                   capture_output=True, text=True, timeout=20, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class MissingInstallationTests(unittest.TestCase):
    def test_read_directory_validation_never_creates_or_repairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(FileNotFoundError):
                installation._check_directory(root / "missing")
            self.assertFalse((root / "missing").exists())
            root.chmod(0o755)
            with self.assertRaises(guard.GuardError):
                installation._check_directory(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            with mock.patch.object(installation, "DATA_ROOT", root):
                path = installation._directory("missing")
                self.assertFalse(path.exists())

    def test_unavailable_installation_fails_closed_without_dynamic_loading(self):
        with mock.patch.object(installation, "_require_managed_install", side_effect=guard.GuardError("没有受管安装")):
            with self.assertRaisesRegex(ControlError, "没有受管安装"):
                installation.formal_source_verifier(SimpleNamespace(root=Path("/untrusted")), {}, {"authority": installation.AUTHORITY})
            with self.assertRaisesRegex(delivery.ConfigurationError, "没有受管安装"):
                delivery._environment_executor("arm64-isolated", configuration={}, execution_context={})


if __name__ == "__main__":
    unittest.main()
