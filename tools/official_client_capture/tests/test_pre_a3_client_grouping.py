"""pre-A3 与发布认证按客户端分组（Claude 升级 B5a-04）。

* 场景表按客户端：Codex 的场景表、单元 ID、命令与改造前相同（与改造前脚本逐字对照见 ``test_pre_a3_codex_parity``）；Claude
  集合为空时规划、签发（含 ``issue --client claude``）、核验与串行认证一律拒绝；登记了 Claude 场景时单元 ID 带
  ``pre-a3:claude:``、命令带 ``--client claude``，两客户端的单元在同一份运行清单里互不承接、互不算作全集之外；
* 策略认证对 Claude 按 v3 引擎算身份：兼容收据、激活认证与 Claude 部署收据合同，客户端混用即拒绝；
* 从单元执行记录签发 Claude 的 pre-A3 v3（带 client）并核验、复用；发布认证 v2 的组装与核验；Claude 发布认证签发在核完
  pre-A3 之后因演练合同未落实而失败关闭，Claude 场景集合为空时更早被 pre-A3 拒绝；
* 入口门禁：pre-a3 门禁项的字面命令按客户端，混合客户端的场景清单拒绝；入口门禁脚本缺省不带 ``--client``；
* 解耦：三个认证模块不再直接引用 ``codex_upgrade``，经适配器取到的 Codex 值与原来相同。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.ci import entry_gates
from tools.official_client_capture import certify_release
from tools.official_client_capture import claude_tool_identity_policy as claude_policy
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture.tests import test_codex_upgrade_policy_certification as policy_tests
from tools.official_client_capture.tests import test_codex_upgrade_pre_a3_certification as pre_a3_tests

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = REPO_ROOT / "tools" / "official_client_capture"
ENTRY_GATES_SCRIPT = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh"
# Claude 替身场景：名字以 vc-chain. 开头（真实链登记的形状要求），测试入口与合成记录库的场景结果一致。
CLAUDE_FAKE_NAME = "vc-chain.claude-fake"
CLAUDE_FAKE = ((CLAUDE_FAKE_NAME, "Claude 替身链", "tools.fake", "FakeTests", "x"),)
STAGING_PARENT = Path("/root/docker/capture-cli/data/staging/pre-a3-scenarios")


@contextlib.contextmanager
def claude_registered():
    """临时登记一条 Claude 替身链（B5a-05 之前 Claude 集合为空）。"""

    with mock.patch.object(pre_a3, "CLAUDE_SCENARIOS", CLAUDE_FAKE), \
            mock.patch.object(pre_a3, "CLAUDE_REAL_CHAIN_IDS", (CLAUDE_FAKE_NAME,)):
        yield


def _claude_previous_policy(root: Path) -> Path:
    """Claude 旧策略：版本减一、少登记一个 control 层文件（模拟策略升级）。"""

    current = json.loads(claude_policy.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    previous = json.loads(json.dumps(current))
    previous["policy_version"] = current["policy_version"] - 1
    previous["layers"]["control"]["files"] = [
        name for name in previous["layers"]["control"]["files"] if name != "official_client_capture/certify_release.py"
    ]
    return policy_tests._write_json(root / "claude-previous-policy.json", previous)


def _claude_deployment_receipt(root: Path, identity: dict, *, name: str = "claude-deploy.json", **overrides: object) -> Path:
    """Claude 部署收据（official-client-managed-deploy/v1）的最小形状：身份在 identity 对象里。"""

    payload = {
        "schema_version": policy_certification.CLAUDE_DEPLOY_RECEIPT_SCHEMA,
        "status": "passed",
        "client": "claude",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "identity": {"policy_version": identity["policy_version"],
                     **{name: identity[name] for name in policy_certification.IDENTITY_FIELDS}},
        "supervisor_sha256": "1" * 64,
    }
    payload.update(overrides)
    return policy_tests._write_json(root / name, payload)


class _IdentityMemo(unittest.TestCase):
    """真实 v3 引擎的 Claude 身份每次要算两个编排器闭包：同一类用例共用一个记忆化目录（键含树与策略，结果逐字节相同）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._memo = tempfile.TemporaryDirectory()
        cls._memo_env = mock.patch.dict(os.environ, {"CODEX_UPGRADE_IDENTITY_MEMO": str(Path(cls._memo.name).resolve())})
        cls._memo_env.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._memo_env.stop()
        cls._memo.cleanup()

    def claude_bindings(self, root: Path) -> tuple[dict, Path, Path, Path]:
        identity = policy_certification.current_identity("claude")
        compatibility = policy_tests._write_json(
            root / "claude-compat.json",
            policy_certification.build_compatibility_receipt(_claude_previous_policy(root), client="claude"),
        )
        deployment = _claude_deployment_receipt(root, identity)
        activation = policy_tests._write_json(
            root / "claude-activation.json",
            policy_certification.build_activation_certification(deployment, compatibility, client="claude"),
        )
        return identity, compatibility, deployment, activation


class ScenarioTableTests(unittest.TestCase):
    """场景表按客户端分组；Claude 集合为空时一律拒绝；登记后单元 ID、命令与记录命名空间按客户端。"""

    def test_tables_group_by_client_and_read_current_attributes(self) -> None:
        self.assertIs(pre_a3.SCENARIOS_BY_CLIENT["codex"], pre_a3.SCENARIOS)
        self.assertIs(pre_a3.REAL_CHAIN_IDS_BY_CLIENT["codex"], pre_a3.REAL_CHAIN_IDS)
        self.assertEqual((pre_a3.SCENARIOS_BY_CLIENT["claude"], pre_a3.REAL_CHAIN_IDS_BY_CLIENT["claude"]), ((), ()),
                         "Claude 的四条链由 B5a-05 登记，此前为空")
        self.assertEqual(set(pre_a3.SCENARIOS_BY_CLIENT), set(upgrade_client_adapter.CLIENTS))
        self.assertIs(pre_a3.scenarios_for(), pre_a3.SCENARIOS)
        self.assertEqual(pre_a3.scenario_names(), pre_a3.scenario_names("codex"))
        with claude_registered():
            self.assertEqual(pre_a3.scenarios_for("claude"), CLAUDE_FAKE, "运行时现取模块属性，替换照常生效")
            self.assertEqual(pre_a3.scenario_names("claude"), [CLAUDE_FAKE_NAME], "Claude 没有补账一类的进程内场景")
        codex_names = set(pre_a3.scenario_names("codex"))
        self.assertFalse(any(name.startswith("claude:") for name in codex_names), "Codex 场景名不得与 Claude 单元前缀相撞")
        with self.assertRaisesRegex(pre_a3.CertificationError, "不认识的客户端"):
            pre_a3.scenarios_for("kilo")

    def test_empty_claude_set_refuses_plan_issue_verify_and_serial_run(self) -> None:
        with self.assertRaisesRegex(pre_a3.CertificationError, "场景集合为空"):
            pre_a3.plan_scenario_commands(STAGING_PARENT, client="claude")
        self.assertEqual(pre_a3.real_chain_registration("claude"), [])
        # 签发在读任何绑定之前拒绝：给不存在的路径也只报集合为空。
        with self.assertRaisesRegex(pre_a3.CertificationError, "场景集合为空"):
            pre_a3.issue_certification_from_records(
                STAGING_PARENT, unit_manifest=Path("/nonexistent/manifest.json"), record_store=Path("/nonexistent/store"),
                deployment_receipt=Path("/nonexistent/deploy.json"), policy_activation=Path("/nonexistent/activation.json"),
                client="claude")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pre-a3-claude.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = pre_a3.main(["issue", "--client", "claude", "--staging-root", str(STAGING_PARENT),
                                    "--unit-manifest", "/nonexistent/m.json", "--record-store", "/nonexistent/s",
                                    "--deployment-receipt", "/nonexistent/d.json", "--policy-activation", "/nonexistent/a.json",
                                    "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertIn("场景集合为空", stderr.getvalue())
            self.assertFalse(output.exists() or list(Path(directory).glob("*.failed-*")), "拒绝签发不写任何认证文件")
            # 核验：一份形状合法的 v3 认证在集合为空时同样无效（逐条重验会得到空报告，不能当成通过）。
            identity = {name: "0" * 64 for name in policy_certification.IDENTITY_FIELDS}
            payload = {"schema_version": pre_a3.SCHEMA_VERSION_V3, "status": "passed", "client": "claude", "identity": identity,
                       "network_attempts": 0, "live_request_count": 0, "scenarios": [], "scenario_count": 0}
            payload["receipt_sha256"] = pre_a3._fingerprint(payload, "claude")
            path = policy_tests._write_json(Path(directory) / "v3.json", payload)
            with self.assertRaisesRegex(pre_a3.CertificationError, "场景集合为空"):
                pre_a3.verify_certification(path, expected_identity=identity, client="claude")
            with self.assertRaisesRegex(pre_a3.CertificationError, "schema 非法"):
                pre_a3.verify_certification(path, expected_identity=identity)
        with self.assertRaisesRegex(pre_a3.CertificationError, "没有串行认证合同"):
            pre_a3.run_certification(STAGING_PARENT, deployment_receipt=Path("/x"), policy_activation=Path("/y"), client="claude")
        with contextlib.redirect_stderr(io.StringIO()) as stderr, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pre_a3.main(["plan", "--client", "claude", "--staging-root", str(STAGING_PARENT),
                                          "--output", "/nonexistent/units.json"]), 2)
        self.assertIn("E2-03 旧路径", stderr.getvalue())

    def test_claude_units_carry_their_prefix_and_client_argument(self) -> None:
        codex_before = pre_a3.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=Path("/data"))
        with claude_registered():
            plan = pre_a3.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=Path("/data"), client="claude")
            codex_after = pre_a3.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=Path("/data"), client="codex")
        self.assertEqual(codex_after, codex_before, "登记 Claude 场景不改变 Codex 的清单")
        self.assertTrue(all("--client" not in unit["argv"] for unit in codex_before["units"]))
        self.assertEqual([unit["unit_id"] for unit in codex_before["units"]],
                         [f"pre-a3:{name}" for name in pre_a3.scenario_names()])
        (unit,) = plan["units"]
        self.assertEqual(unit["unit_id"], f"pre-a3:claude:{CLAUDE_FAKE_NAME}")
        self.assertEqual(unit["argv"], ["python3", "-m", "tools.official_client_capture.codex_upgrade_pre_a3_certification",
                                        "run-scenario", "--client", "claude", "--name", CLAUDE_FAKE_NAME,
                                        "--staging-parent", str(STAGING_PARENT)])
        self.assertEqual(unit["scenario_test_file"], "tools/fake.py")
        self.assertEqual(pre_a3.unit_client(unit["unit_id"]), "claude")
        self.assertEqual(pre_a3.unit_client(codex_before["units"][0]["unit_id"]), "codex")
        self.assertIsNone(pre_a3.unit_client("capture:prerequisites"))
        self.assertEqual(entry_gates.PRE_A3_CLIENT_PREFIXES, dict(pre_a3.CLIENT_UNIT_PREFIXES), "入口门禁与认证模块同一套前缀")

    def test_record_namespaces_do_not_inherit_across_clients(self) -> None:
        """同一份运行清单里两客户端的单元互不承接、互不算作对方全集之外的单元。"""

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(pre_a3, "SCENARIOS", pre_a3_tests.FAKE_SCENARIOS), \
                claude_registered():
            root = Path(directory).resolve()
            store = pre_a3_tests._SyntheticStore(root)
            codex_records = [store.record(name, "run-mixed") for name in pre_a3.scenario_names("codex")]
            claude_record = store.record(CLAUDE_FAKE_NAME, "run-mixed", unit_id=f"pre-a3:claude:{CLAUDE_FAKE_NAME}")
            manifest_path = store.manifest("run-mixed", [store.entry(record) for record in [*codex_records, claude_record]])
            reader = pre_a3._RecordStore(store.store.root)
            manifest = pre_a3._published_manifest(reader, manifest_path)
            for client, expected in (("codex", pre_a3.scenario_names("codex")), ("claude", [CLAUDE_FAKE_NAME])):
                report, failed, attempts = pre_a3._scenarios_from_records(reader, manifest, client)
                self.assertEqual((failed, attempts), ([], 0), client)
                self.assertEqual([row["name"] for row in report], expected)
            # Codex 前缀下同名的记录不能顶替 Claude 的场景单元（单元 ID 不同即缺报）。
            impostor = store.record(CLAUDE_FAKE_NAME, "run-impostor")
            manifest = pre_a3._published_manifest(reader, store.manifest("run-impostor", [store.entry(impostor)]))
            report, failed, _attempts = pre_a3._scenarios_from_records(reader, manifest, "claude")
            self.assertEqual(failed, [CLAUDE_FAKE_NAME])
            self.assertIn("缺报", report[0]["error"])


class EntryGatesTests(unittest.TestCase):
    def test_pre_a3_gate_command_follows_the_units_client(self) -> None:
        codex_ids = [f"pre-a3:{name}" for name in pre_a3.scenario_names()]
        self.assertEqual(entry_gates.gate_literal_command("pre-a3", codex_ids), entry_gates.GATE_COMMANDS["pre-a3"])
        self.assertEqual(entry_gates.gate_literal_command("pre-a3", []), entry_gates.GATE_COMMANDS["pre-a3"])
        argv, cwd = entry_gates.gate_literal_command("pre-a3", [f"pre-a3:claude:{CLAUDE_FAKE_NAME}"])
        self.assertEqual((argv, cwd), ([*entry_gates.GATE_COMMANDS["pre-a3"][0], "--client", "claude"], "."))
        self.assertEqual(entry_gates.gate_literal_command("check-egress-spec"), entry_gates.GATE_COMMANDS["check-egress-spec"])

    def test_claude_units_plan_and_mixed_manifests_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory, claude_registered():
            root = Path(directory)
            claude_units = pre_a3.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=root, client="claude")
            codex_units = pre_a3.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=root)
            claude_file = root / "claude-units.json"
            claude_file.write_text(json.dumps(claude_units), encoding="utf-8")
            manifest = entry_gates.plan_gates(Path("."), profile="pre-a3", launcher=["launcher"], pre_a3_units=claude_file,
                                              pre_a3_data_inputs=[], platform="linux")
            self.assertEqual([unit["unit_id"] for unit in manifest["units"]], [f"pre-a3:claude:{CLAUDE_FAKE_NAME}"])
            self.assertEqual(manifest["gates"], [{"gate_id": "pre-a3", "units": [f"pre-a3:claude:{CLAUDE_FAKE_NAME}"]}])
            self.assertIn("--client", manifest["units"][0]["argv"])
            self.assertIn("inputs", manifest["units"][0], "Claude 场景单元同样按测试模块声明输入、可承接")
            mixed_file = root / "mixed-units.json"
            mixed_file.write_text(json.dumps({**codex_units, "units": [*codex_units["units"][:2], *claude_units["units"]]}),
                                  encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "混有多个客户端"):
                entry_gates.plan_gates(Path("."), profile="pre-a3", launcher=["launcher"], pre_a3_units=mixed_file,
                                       pre_a3_data_inputs=[], platform="linux")

    def test_entry_gates_script_passes_client_only_when_not_codex(self) -> None:
        lines = ENTRY_GATES_SCRIPT.read_text(encoding="utf-8").splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("PA3_CLIENT_ARGS=()"))
        snippet = "set -Eeuo pipefail\n" + "\n".join(lines[start:start + 2]) + '\nprintf "[%s]" ${PA3_CLIENT_ARGS[@]+"${PA3_CLIENT_ARGS[@]}"}\n'
        for client, expected in ((None, "[]"), ("codex", "[]"), ("claude", "[--client][claude]")):
            environment = {key: value for key, value in os.environ.items() if key != "CLIENT"}
            if client is not None:
                environment["CLIENT"] = client
            result = subprocess.run(["bash", "-c", snippet], env=environment, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (0, expected), client)
        calls = [line for line in lines if "codex_upgrade_pre_a3_certification" in line and "python3 -m" in line]
        self.assertEqual(len(calls), 6, "plan、find-reusable×3、issue、record-reuse")
        self.assertTrue(all('${PA3_CLIENT_ARGS[@]+"${PA3_CLIENT_ARGS[@]}"}' in line for line in calls))


class ClaudePolicyCertificationTests(_IdentityMemo):
    """策略认证按 Claude 策略与 v3 引擎算身份；部署收据合同按客户端；客户端混用即拒绝。"""

    def test_claude_identity_comes_from_the_v3_engine(self) -> None:
        identity = policy_certification.current_identity("claude")
        local = claude_policy.compute_identity(claude_policy.load_policy(), REPO_ROOT)
        for field in ("policy_version", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256",
                      "tool_files_sha256"):
            self.assertEqual(identity[field], local[field], field)
        self.assertNotEqual(identity["policy_sha256"], policy_certification.current_identity()["policy_sha256"])

    def test_claude_compatibility_activation_and_deployment_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, compatibility, deployment, activation = self.claude_bindings(root)
            receipt = json.loads(compatibility.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], policy_certification.POLICY_COMPATIBILITY_SCHEMA, "沿用 v1 合同")
            self.assertEqual((receipt["previous"]["policy_version"], receipt["current"]["policy_version"]),
                             (identity["policy_version"] - 1, identity["policy_version"]))
            self.assertEqual(receipt["current"]["policy_sha256"], identity["policy_sha256"])
            self.assertEqual(receipt["tool_files_sha256"], identity["tool_files_sha256"], "两根四后缀口径")
            self.assertIn("official_client_capture/certify_release.py", receipt["newly_registered_files"])
            self.assertIn("official_client_capture/certify_release.py", receipt["defaulted_paths_under_previous_policy"])
            self.assertEqual(receipt["defaulted_paths_under_current_policy"], [])
            self.assertEqual(receipt["rule"], policy_certification.COMPATIBILITY_RULES["claude"])
            self.assertEqual(policy_certification.load_compatibility_receipt(compatibility, client="claude"), receipt)
            payload = policy_certification.verify_activation_certification(activation, client="claude")
            self.assertEqual(payload["identity"], {name: str(identity[name]) for name in policy_certification.IDENTITY_FIELDS})
            self.assertEqual(payload["supersession_rule"], policy_certification.ACTIVATION_SUPERSESSION_RULES["claude"])
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "五摘要"):
                policy_certification.verify_activation_certification(activation)
            # 部署收据合同按客户端：Codex 形状、client 不符、身份漂移都拒绝。
            codex_shaped = policy_tests._deployment_receipt(root, identity)
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "schema 非法"):
                policy_certification.load_deployment_receipt(codex_shaped, expected_identity=identity, client="claude")
            wrong_client = _claude_deployment_receipt(root, identity, name="wrong-client.json", client="codex")
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "client 不是 claude"):
                policy_certification.load_deployment_receipt(wrong_client, expected_identity=identity, client="claude")
            drifted = _claude_deployment_receipt(root, {**identity, "control_sha256": "f" * 64}, name="drifted.json")
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "五摘要"):
                policy_certification.load_deployment_receipt(drifted, expected_identity=identity, client="claude")
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "schema 非法"):
                policy_certification.load_deployment_receipt(deployment, expected_identity=identity)
            # 命令行：--client claude 出具与核验。
            output = root / "cli-activation.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(policy_certification.main(["activation", "--client", "claude", "--deployment-receipt", str(deployment),
                                                            "--compatibility-receipt", str(compatibility), "--output", str(output)]), 0)
                self.assertEqual(policy_certification.main(["verify-activation", "--client", "claude", "--certification", str(output)]), 0)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(policy_certification.main(["verify-activation", "--certification", str(output)]), 2)


class ClaudeRecordCertificationTests(_IdentityMemo):
    """Claude 的 pre-A3 v3：从执行记录签发、核验、复用；发布认证 v2 的组装与核验；签发在演练合同落实前失败关闭。"""

    def _issue(self, root: Path, deployment: Path, activation: Path) -> tuple[Path, dict, pre_a3_tests._SyntheticStore]:
        store = pre_a3_tests._SyntheticStore(root)
        record = store.record(CLAUDE_FAKE_NAME, "run-claude", unit_id=f"pre-a3:claude:{CLAUDE_FAKE_NAME}")
        manifest = store.manifest("run-claude", [store.entry(record)])
        receipt = pre_a3.issue_certification_from_records(
            root / "data" / "staging" / "pre-a3-scenarios", unit_manifest=manifest, record_store=store.store.root,
            deployment_receipt=deployment, policy_activation=activation, client="claude")
        return pre_a3.write_certification(root / "pre-a3-claude.json", receipt), receipt, store

    def test_claude_v3_issue_verify_reuse_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _compatibility, deployment, activation = self.claude_bindings(root)
            with claude_registered():
                path, receipt, _store = self._issue(root, deployment, activation)
                self.assertEqual((receipt["schema_version"], receipt["client"], receipt["status"]),
                                 (pre_a3.SCHEMA_VERSION_V3, "claude", "passed"))
                self.assertEqual(set(receipt), pre_a3_tests.V1_FIELDS | {"unit_manifest", "record_store", "client"},
                                 "v3 = v2 结构加 client")
                self.assertEqual(receipt["identity"], {name: identity[name] for name in policy_certification.IDENTITY_FIELDS})
                self.assertEqual(receipt["real_chain_registration"], [{"id": CLAUDE_FAKE_NAME, "test": "tools.fake:FakeTests.x"}])
                self.assertEqual(pre_a3.verify_certification(path, client="claude")["client"], "claude")
                with self.assertRaisesRegex(pre_a3.CertificationError, "schema 非法"):
                    pre_a3.verify_certification(path)
                with self.assertRaisesRegex(pre_a3.CertificationError, "不属于客户端"):
                    pre_a3.real_chain_coverage(receipt)
                self.assertEqual(pre_a3.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation,
                                                                    certification=path, client="claude"), path)
                self.assertEqual(pre_a3.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation,
                                                                    search_root=root, client="claude"), path,
                                 "目录里其余 JSON（兼容收据、部署收据等）核验不过、被跳过")
                with self.assertRaises(policy_certification.PolicyCertificationError):
                    pre_a3.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation,
                                                       certification=path)
                reuse = pre_a3.record_reuse(path, deployment_receipt=deployment, policy_activation=activation, receipt_root=root,
                                            client="claude")
                self.assertEqual(reuse["status"], "not_needed", "同一部署下签发的认证不需要复用收据")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pre_a3.main(["verify", "--client", "claude", "--certification", str(path)]), 0)
                # 发布认证：Claude 签发在核完 pre-A3 后，因 Job 演练与 atomic-double 合同未落实而失败关闭。
                with mock.patch.object(certify_release, "_require_release_platform"):
                    with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "B2-10、B5a-09"):
                        certify_release.build_certification(
                            deployment_receipt=deployment, pre_a3_certification=path, policy_activation=activation,
                            job_rehearsal_root=root, job_rehearsal_receipt=Path("job.json"), atomic_rehearsal_root=root,
                            atomic_rehearsal_receipt=Path("atomic.json"), client="claude")
                # 发布认证 v2 的组装与核验（演练绑定用占位文件，校验只核路径与摘要）。
                release = self._compose_release(root, identity, deployment, path, receipt)
                self.assertEqual((release["schema_version"], release["client"]), (certify_release.SCHEMA_VERSION_V2, "claude"))
                release_path = policy_tests._write_json(root / "release-claude.json", release)
                self.assertEqual(certify_release.verify(release_path, client="claude")["receipt_sha256"], release["receipt_sha256"])
                with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "schema 非法"):
                    certify_release.verify(release_path)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(certify_release.main(["verify", "--client", "claude", "--certification", str(release_path)]), 0)
                forged = {**release, "client": "codex"}
                forged_path = policy_tests._write_json(root / "release-forged.json", forged)
                with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "不属于客户端|自摘要"):
                    certify_release.verify(forged_path, client="claude")
            # Claude 集合为空（本批的现状）：同一份 v3 认证不再有效，发布认证签发在 pre-A3 一步即拒绝。
            with self.assertRaisesRegex(pre_a3.CertificationError, "场景集合为空"):
                pre_a3.verify_certification(path, client="claude")
            with mock.patch.object(certify_release, "_require_release_platform"):
                with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "场景集合为空"):
                    certify_release.build_certification(
                        deployment_receipt=deployment, pre_a3_certification=path, policy_activation=activation,
                        job_rehearsal_root=root, job_rehearsal_receipt=Path("job.json"), atomic_rehearsal_root=root,
                        atomic_rehearsal_receipt=Path("atomic.json"), client="claude")
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    code = certify_release.main(["issue", "--client", "claude", "--deployment-receipt", str(deployment),
                                                 "--pre-a3-certification", str(path), "--policy-activation", str(activation),
                                                 "--job-rehearsal-root", str(root), "--job-rehearsal-receipt", "job.json",
                                                 "--atomic-rehearsal-root", str(root), "--atomic-rehearsal-receipt", "atomic.json",
                                                 "--output", str(root / "release-issued.json")])
            self.assertEqual(code, 2)
            self.assertIn("场景集合为空", stderr.getvalue())
            self.assertFalse((root / "release-issued.json").exists())

    @staticmethod
    def _compose_release(root: Path, identity: dict, deployment: Path, pre_a3_path: Path, receipt: dict) -> dict:
        job = policy_tests._write_json(root / "job.json", {"job": True})
        atomic = policy_tests._write_json(root / "atomic.json", {"atomic": True})
        binding = certify_release._bind_file
        return certify_release.compose_certification(
            identity=identity,
            deployment_receipt=binding(deployment, "部署收据", "claude"),
            pre_a3_certification={**binding(pre_a3_path, "pre-A3", "claude"), "receipt_sha256": receipt["receipt_sha256"],
                                  "scenario_count": receipt["scenario_count"]},
            policy_activation=None,
            job_rehearsal={"evidence_root": str(root), "receipt": job.name, "sha256": binding(job, "Job", "claude")["sha256"],
                           "failure_lifecycle_probe_sha256": "a" * 64},
            atomic_double_rehearsal={"evidence_root": str(root), "receipt": atomic.name,
                                     "sha256": binding(atomic, "atomic", "claude")["sha256"]},
            campaign_run_rehearsal=None,
            real_chain_coverage=pre_a3.real_chain_coverage(receipt, client="claude"),
            client="claude",
        )


class DecouplingTests(unittest.TestCase):
    """三个认证模块对编排器的引用全部经适配器：不导入、不直接引用 codex_upgrade；Codex 经适配器取到的值不变。"""

    MODULES = (pre_a3, policy_certification, certify_release)

    def test_modules_do_not_reference_codex_upgrade(self) -> None:
        for module in self.MODULES:
            with self.subTest(module.__name__):
                tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
                imported = [alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                            for alias in node.names]
                self.assertNotIn("codex_upgrade", imported)
                self.assertFalse([node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "codex_upgrade"])
                self.assertFalse(hasattr(module, "codex_upgrade"))

    def test_codex_values_through_the_adapter_are_unchanged(self) -> None:
        value = {"b": [1, "中文"], "a": None}
        for module in self.MODULES:
            self.assertEqual(module._fingerprint(value), codex_upgrade._fingerprint(value), module.__name__)
        self.assertEqual(policy_certification.current_identity(), policy_certification.current_identity("codex"))
        for client in upgrade_client_adapter.CLIENTS:
            self.assertEqual(certify_release.SHA256_RE.pattern, upgrade_client_adapter.adapter_for(client).orchestrator.SHA256_RE.pattern)
        self.assertEqual(policy_certification.DEPLOY_RECEIPT_SCHEMA, "codex-arm64-supervisor-enable/v1")
        self.assertEqual((pre_a3.CERTIFICATION_SCHEMAS, pre_a3.SCENARIO_UNIT_PREFIX, certify_release.SCHEMA_VERSION),
                         (("pre-a3-path-certification/v1", "pre-a3-path-certification/v2"), "pre-a3:", "tool-release-certification/v1"))


if __name__ == "__main__":
    unittest.main()
