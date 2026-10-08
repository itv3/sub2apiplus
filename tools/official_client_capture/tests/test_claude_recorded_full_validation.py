"""录制完整链准备的轻量拒绝例；不进入重链、不登记 VC-0→VC-6 认证。"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
