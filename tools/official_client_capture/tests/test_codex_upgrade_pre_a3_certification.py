"""A2.5：pre-A3 路径认证——staging 内 fixture_only 总账、网络守卫、场景全过才出收据。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import test_codex_upgrade_policy_certification as policy_tests

QUICK_SCENARIOS = tuple(
    scenario
    for scenario in certification.SCENARIOS
    if scenario[0]
    in {
        "harden-evidence-permissions.two-step",
        "wire-transition.intent-final",
        "batch.uncommitted-not-pushed",
        "vc-chain.ledger-events-derivation",
    }
)


class PreA3CertificationTests(unittest.TestCase):
    def _bindings(self, root: Path) -> tuple[Path, Path]:
        identity = policy_certification.current_identity()
        previous = policy_tests._previous_policy(root)
        compatibility = policy_tests._write_json(
            root / "compat.json", policy_certification.build_compatibility_receipt(previous)
        )
        deployment = policy_tests._deployment_receipt(root, identity)
        activation = policy_tests._write_json(
            root / "activation.json",
            policy_certification.build_activation_certification(deployment, compatibility),
        )
        return deployment, activation

    def test_certification_runs_scenarios_in_staging_with_fixture_only_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            deployment, activation = self._bindings(root)
            staging = root / "data" / "staging" / "pre-a3"
            receipt = certification.run_certification(
                staging,
                deployment_receipt=deployment,
                policy_activation=activation,
                scenarios=QUICK_SCENARIOS,
            )
            self.assertEqual(receipt["status"], "passed", receipt["failed_scenarios"])
            self.assertEqual(receipt["network_attempts"], 0)
            self.assertTrue(receipt["fixture_only"])
            names = [item["name"] for item in receipt["scenarios"]]
            self.assertEqual(names[:-1], [scenario[0] for scenario in QUICK_SCENARIOS])
            self.assertEqual(names[-1], "accounting.resolved-unblocks")
            # VC-2～VC-6 派发链场景已并入路径认证，发布认证据此授权后续阶段。
            self.assertEqual(
                [scenario[0] for scenario in certification.SCENARIOS if scenario[0].startswith("vc-chain.")],
                [
                    "vc-chain.batches-through-vc6",
                    "vc-chain.stopped-ledger-rejected-before-write",
                    "vc-chain.failed-batch-abandons-stage",
                    "vc-chain.admission-before-any-write",
                    "vc-chain.ledger-events-derivation",
                    "vc-chain.ledger-budget-bound-to-project",
                    "vc-chain.draft-and-approval-preview-are-legal-stops",
                    "vc-chain.candidate-seal-and-canonical-advance-consumers",
                ],
            )
            self.assertTrue(all(item["status"] == "passed" for item in receipt["scenarios"]), receipt["scenarios"])
            self.assertEqual(receipt["identity"], {name: policy_certification.current_identity()[name] for name in policy_certification.IDENTITY_FIELDS})
            # 场景夹具全部落在 staging 根下，且总账都是 fixture_only。
            plans = list((staging / "tmp").rglob("upgrade-project-ledger/plan.json"))
            self.assertTrue(plans)
            self.assertTrue(all(json.loads(p.read_text(encoding="utf-8"))["fixture_only"] for p in plans))
            self.assertIsNone(os.environ.get(project_ledger_fixture.FIXTURE_ONLY_ENV))
            output = root / "receipt.json"
            policy_certification._write_once(output, receipt)
            verified = certification.verify_certification(output)
            self.assertEqual(verified["scenario_count"], receipt["scenario_count"])

    def test_certification_fails_closed_on_scenario_failure_or_stale_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            deployment, activation = self._bindings(root)
            staging = root / "data" / "staging" / "pre-a3"
            broken = (
                (
                    "broken.scenario",
                    "不存在的测试方法",
                    "tools.official_client_capture.tests.test_codex_upgrade_harden_evidence_permissions",
                    "HardenEvidencePermissionsTests",
                    "test_does_not_exist",
                ),
            )
            receipt = certification.run_certification(
                staging, deployment_receipt=deployment, policy_activation=activation, scenarios=broken
            )
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("broken.scenario", receipt["failed_scenarios"])
            self.assertEqual(certification.main(["verify", "--certification", str(policy_tests._write_json(root / "failed.json", receipt))]), 2)
            stale = policy_tests._deployment_receipt(root / "stale", policy_certification.current_identity(), control_sha256="0" * 64)
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "五摘要与当前工具身份不一致"):
                certification.run_certification(
                    root / "data" / "staging" / "pre-a3-2", deployment_receipt=stale, policy_activation=activation, scenarios=()
                )
            with self.assertRaisesRegex(certification.CertificationError, "staging 目录树内"):
                certification.run_certification(root / "outside", deployment_receipt=deployment, policy_activation=activation, scenarios=())


if __name__ == "__main__":
    unittest.main()
