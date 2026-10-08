"""录制完整链准备的轻量拒绝例；不进入重链、不登记 VC-0→VC-6 认证。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_full_validation_driver as driver
from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as vc1


class RecordedFullPreparationTests(unittest.TestCase):
    """完整来源和门禁收据用正式只读合同重验，轻量测试不能制造完整链登记。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve() / "staging/data"
        vc1._create_world_roots(self.data)

    def test_mac_source_requires_actual_first_level_staging(self) -> None:
        driver._require_client_staging(self.data)
        with self.assertRaisesRegex(source.replay.ReplayError, "首层 <world>/staging"):
            driver._require_client_staging(self.data.parent.parent / "other-staging/data")

    def test_full_inputs_replay_real_policy_and_preserve_rule_denominator(self) -> None:
        from tools.official_client_capture import claude_upgrade_wire_projection as wire
        from tools.official_client_capture.tests import test_claude_upgrade_profile as fixture

        originals = {path: source.binding(path) for path in (fixture.ACTIVE_PROFILE, fixture.ACTIVE_WIRE, fixture.ACTIVE_CATALOG)}
        inputs = driver._inputs(self.data)
        policy = wire.load_projection_policy(source.bound(inputs["bindings"]["active_projection_policy"], root=self.data))
        self.assertEqual(policy["version"], vc1.BASELINE)
        self.assertEqual(len(source.read(source.bound(inputs["bindings"]["rules"], root=self.data))["required_rules"]), 40)
        self.assertEqual(len(source.read(source.bound(inputs["bindings"]["active_profile"], root=self.data))["rules"]), 40)
        origin = source.read(source.bound(inputs["bindings"]["active_projection_origin"], root=self.data))
        self.assertFalse(origin["formal_version_semantics_certified"])
        for path, expected in originals.items():
            self.assertEqual(source.binding(path), expected)

    def test_gate_environment_real_replay_and_tamper_rejection(self) -> None:
        from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm

        root = self.data / "control/gate"
        root.mkdir(mode=0o700)
        # 单测仅给真实 collector 提供 ARM64 宿主身份；完整链的 require_isolation 不允许这种替身。
        with mock.patch.object(arm.platform, "machine", return_value="aarch64"):
            bindings = driver._gate_environment(root, self.data, "fixture-gate")
        before = arm.replay(root, bindings["before"]["path"])
        after = arm.replay(root, bindings["after"]["path"])
        self.assertTrue(arm.receipts_equivalent(root, before, root, after))
        self.assertTrue(source.read(self.data / "control/fixture-gate-observation/origin.json")["fixture_only"])
        facts = root / "environment/after-facts.json"
        facts.write_bytes(facts.read_bytes() + b" ")
        with self.assertRaises(arm.Arm64EnvironmentReceiptError):
            arm.replay(root, bindings["after"]["path"])
        with self.assertRaises(arm.Arm64EnvironmentReceiptError):
            arm.receipts_equivalent(root, before, root, after)

    def test_pending_private_shell_delegates_cli_and_preserves_failure(self) -> None:
        from tools.official_client_capture.tests.real_chains import test_claude_vc1_recorded_chain as shell

        case = shell.ClaudeRecordedVC1ChainTests(methodName="_run_pending_full_validation")
        observed = {}
        original_temp = tempfile.gettempdir()
        original_readlink = os.readlink

        def external_command(argv, **options):
            if argv == ["unshare", "-m", "-n", "true"]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            observed["argv"], observed["options"] = argv, options
            data = Path(argv[argv.index("--data") + 1])
            observed["world"] = data.parent.parent
            self.addCleanup(shutil.rmtree, observed["world"])
            return subprocess.CompletedProcess(argv, 23, "真实外部边界 stdout 标记\n", "真实外部边界 stderr 标记\n")

        environment = {"CLAUDE_2_1_226_RELAY_FIXTURE": "/fixture/relay", "CLAUDE_2_1_226_FREEZE_FIXTURE": "/fixture/freeze",
                       shell.RECORD_DIR_ENV: original_temp, shell.RECORDS_ENV: ""}
        with mock.patch.dict(os.environ, environment), mock.patch.object(shell.platform, "system", return_value="Linux"), \
                mock.patch.object(shell.platform, "machine", return_value="aarch64"), \
                mock.patch.object(shell.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(shell.shutil, "which", return_value="/fixture/tool"), \
                mock.patch.object(shell.os, "readlink", side_effect=lambda path: "parent-" + path.rsplit("/", 1)[-1]
                                  if str(path).startswith("/proc/self/ns/") else original_readlink(path)), \
                mock.patch.object(shell.subprocess, "run", side_effect=external_command):
            with self.assertRaises(AssertionError):
                case._run_pending_full_validation()
        command = observed["argv"]
        self.assertIn(driver.ACTIVATION_MODULE, command)
        self.assertEqual(command[command.index("--through") + 1], "VC-6")
        self.assertEqual(Path(command[command.index("--data") + 1]), observed["world"] / "staging/data")
        record = source.read(observed["world"] / "record.json")
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["returncode"], 23)
        self.assertFalse(record["formal_version_semantics_certified"])
        self.assertIsNone(record["report_payload"])
        self.assertEqual(source.bound(record["stdout"]).read_text(encoding="utf-8"), "真实外部边界 stdout 标记\n")
        self.assertEqual(source.bound(record["stderr"]).read_text(encoding="utf-8"), "真实外部边界 stderr 标记\n")
        self.assertEqual(tempfile.gettempdir(), original_temp)
        self.assertNotIn("_run_pending_full_validation", unittest.defaultTestLoader.getTestCaseNames(shell.ClaudeRecordedVC1ChainTests))


if __name__ == "__main__":
    unittest.main()
