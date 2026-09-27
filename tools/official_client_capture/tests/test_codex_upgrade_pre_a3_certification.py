"""A2.5：pre-A3 路径认证——staging 内 fixture_only 总账、网络守卫、场景全过才出收据。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
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
                    "vc-chain.full-validation-only",
                    "vc-chain.late-stage-faults",
                    "vc-chain.vc1-capture",
                    "vc-chain.vc1-recovery-chain",
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

    def test_registered_real_chain_requires_one_passed_matching_entry(self) -> None:
        registered = [next(row for row in certification.SCENARIOS if row[0] == name) for name in certification.REAL_CHAIN_IDS]
        rows_passed = [{"name": row[0], "test": f"{row[2]}:{row[3]}.{row[4]}", "status": "passed"} for row in registered]
        passed, others = rows_passed[0], rows_passed[1:]
        bound = {"scenarios": rows_passed, "real_chain_registration": certification.real_chain_registration()}
        self.assertEqual([item["id"] for item in certification.real_chain_coverage(bound)], list(certification.REAL_CHAIN_IDS))
        self.assertEqual([item["id"] for item in certification.real_chain_coverage(bound, historical=True)], list(certification.REAL_CHAIN_IDS))
        # 历史回放只核验当时绑定的集合：只登记第一条链的旧收据仍可回放。
        historical = {"scenarios": [passed], "real_chain_registration": bound["real_chain_registration"][:1]}
        self.assertEqual(certification.real_chain_coverage(historical, historical=True)[0]["id"], passed["name"])
        with self.assertRaises(certification.CertificationError):
            certification.real_chain_coverage(historical)
        for registration in (None, [], [{}], [bound["real_chain_registration"][0]] * 2):
            with self.subTest(registration=registration), self.assertRaises(certification.CertificationError):
                certification.real_chain_coverage({**bound, "real_chain_registration": registration}, historical=True)
        for rows in ([], [passed, passed], [{**passed, "status": "uncertified"}], [{**passed, "status": "failed"}], [{**passed, "test": "无关入口"}]):
            with self.subTest(rows=rows), self.assertRaises(certification.CertificationError):
                certification.real_chain_coverage({**bound, "scenarios": [*rows, *others]})
        # 任一登记链缺失都拒绝（新增的后段注入链不能被漏认证）。
        for index in range(len(rows_passed)):
            with self.subTest(missing=rows_passed[index]["name"]), self.assertRaises(certification.CertificationError):
                certification.real_chain_coverage({**bound, "scenarios": rows_passed[:index] + rows_passed[index + 1:]})

    def test_registered_real_chain_skip_is_uncertified(self) -> None:
        class SkippedChain(unittest.TestCase):
            def runTest(self):
                self.skipTest("夹具没有 Docker")
        scenario = next(row for row in certification.SCENARIOS if row[0] in certification.REAL_CHAIN_IDS)
        with mock.patch.object(certification, "_load_test_case", return_value=SkippedChain()):
            result = certification.run_scenario(scenario)
        self.assertEqual(result["status"], "uncertified")

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


class PreA3CertificationReuseTests(unittest.TestCase):
    """修好接着跑第 18、19 项：同一部署、同一激活认证、工具身份未变时复用最近一次认证；stage1 按同一口径核验。"""

    def _receipt(self, root: Path, name: str, *, deployment: Path, activation: Path, certified_at: str, identity: dict | None = None) -> Path:
        current = policy_certification.current_identity()
        payload = {
            "schema_version": certification.SCHEMA_VERSION,
            "status": "passed",
            "certified_at_utc": certified_at,
            "fixture_only": True,
            "identity": identity or {field: current[field] for field in policy_certification.IDENTITY_FIELDS},
            "policy_version": current["policy_version"],
            "deployment_receipt": {"path": str(deployment), "sha256": codex_upgrade.file_sha256(deployment)},
            "policy_activation": {"path": str(activation), "sha256": codex_upgrade.file_sha256(activation)},
            "real_chain_registration": certification.real_chain_registration(),
            "scenarios": [
                {"name": item["id"], "test": item["test"], "status": "passed"} for item in certification.real_chain_registration()
            ],
            "network_attempts": 0,
            "live_request_count": 0,
        }
        payload["receipt_sha256"] = certification._fingerprint(payload)
        path = root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_reuse_requires_same_deployment_activation_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "policy-certification"
            store.mkdir()
            deployment = root / "deploy.json"
            deployment.write_text('{"status": "passed"}\n', encoding="utf-8")
            activation = root / "activation.json"
            activation.write_text('{"activation": 1}\n', encoding="utf-8")
            other_deployment = root / "deploy-2.json"
            other_deployment.write_text('{"status": "passed", "n": 2}\n', encoding="utf-8")
            older = self._receipt(store, "a.json", deployment=deployment, activation=activation, certified_at="2026-09-27T01:00:00Z")
            newer = self._receipt(store, "b.json", deployment=deployment, activation=activation, certified_at="2026-09-27T02:00:00Z")
            self._receipt(store, "c.json", deployment=other_deployment, activation=activation, certified_at="2026-09-27T03:00:00Z")
            stale = dict(policy_certification.current_identity())
            stale_identity = {field: stale[field] for field in policy_certification.IDENTITY_FIELDS}
            stale_identity[policy_certification.IDENTITY_FIELDS[0]] = "0" * 64
            self._receipt(store, "d.json", deployment=deployment, activation=activation, certified_at="2026-09-27T04:00:00Z", identity=stale_identity)
            (store / "not-a-receipt.json").write_text("{}\n", encoding="utf-8")
            found = certification.find_reusable_certification(
                deployment_receipt=deployment, policy_activation=activation, search_root=store
            )
            # 最新一份绑定别的部署、再新一份工具身份已变：都不复用，取同一部署下最近的一份。
            self.assertEqual(found, newer)
            self.assertEqual(
                certification.find_reusable_certification(
                    deployment_receipt=deployment, policy_activation=activation, certification=older
                ),
                older,
            )
            self.assertIsNone(
                certification.find_reusable_certification(
                    deployment_receipt=other_deployment, policy_activation=activation, certification=older
                )
            )
            other_activation = root / "activation-2.json"
            other_activation.write_text('{"activation": 2}\n', encoding="utf-8")
            self.assertIsNone(
                certification.find_reusable_certification(
                    deployment_receipt=deployment, policy_activation=other_activation, search_root=store
                )
            )
            # 命令行：找到打印路径退出 0；没有则退出 1。
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(
                    certification.main(["find-reusable", "--search-root", str(store), "--deployment-receipt", str(deployment), "--policy-activation", str(activation)]),
                    0,
                )
            self.assertEqual(stdout.getvalue().strip(), str(newer))
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(
                    certification.main(["find-reusable", "--certification", str(older), "--deployment-receipt", str(deployment), "--policy-activation", str(other_activation)]),
                    1,
                )


if __name__ == "__main__":
    unittest.main()
