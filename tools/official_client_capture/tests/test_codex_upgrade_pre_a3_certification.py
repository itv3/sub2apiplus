"""A2.5：pre-A3 路径认证——staging 内 fixture_only 总账、网络守卫、场景全过才出收据。"""

from __future__ import annotations

import io
import json
import os
import shutil
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
    """修好接着跑第 18、19 项：工具身份（五摘要）与策略未变即复用最近一次认证——重新部署一次（部署收据、激活认证
    的 sha 都变了）也复用；stage1 按同一口径核验；跨部署复用登记 write-once、按绑定内容幂等的复用收据。"""

    def _compatibility(self, root: Path) -> Path:
        previous = policy_tests._previous_policy(root)
        return policy_tests._write_json(root / "compat.json", policy_certification.build_compatibility_receipt(previous))

    def _deployment(self, root: Path, name: str, compatibility: Path, *, created_at: str, **overrides: object) -> tuple[Path, Path]:
        """一次部署：合法部署收据（五摘要等于当前身份）与绑定它的激活认证。"""

        identity = policy_certification.current_identity()
        deployment = policy_tests._deployment_receipt(root / name, identity, created_at_utc=created_at, **overrides)
        activation = policy_tests._write_json(
            root / f"{name}-activation.json", policy_certification.build_activation_certification(deployment, compatibility)
        )
        return deployment, activation

    def _receipt(
        self,
        root: Path,
        name: str,
        *,
        deployment: Path,
        activation: Path,
        certified_at: str,
        identity: dict | None = None,
        **overrides: object,
    ) -> Path:
        current = policy_certification.current_identity()
        payload = {
            "schema_version": certification.SCHEMA_VERSION,
            "status": "passed",
            "certified_at_utc": certified_at,
            "fixture_only": True,
            "identity": identity or {field: current[field] for field in policy_certification.IDENTITY_FIELDS},
            "policy_version": current["policy_version"],
            "deployment_receipt": {"path": str(deployment), "sha256": codex_upgrade.file_sha256(deployment)},
            "policy_activation": {
                "path": str(activation),
                "sha256": codex_upgrade.file_sha256(activation),
                "policy_sha256": current["policy_sha256"],
            },
            "real_chain_registration": certification.real_chain_registration(),
            "scenarios": [
                {"name": item["id"], "test": item["test"], "status": "passed"} for item in certification.real_chain_registration()
            ],
            "network_attempts": 0,
            "live_request_count": 0,
            **overrides,
        }
        payload["receipt_sha256"] = certification._fingerprint(payload)
        path = root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_reuse_requires_same_identity_and_policy(self) -> None:
        """同一部署下多份认证取最近一份；工具身份已变的不复用；本次部署收据不是当前工具树即明确报错。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            compatibility = self._compatibility(root)
            deployment, activation = self._deployment(root, "deploy-1", compatibility, created_at="2026-09-27T00:00:00.000Z")
            store = root / "policy-certification"
            store.mkdir(mode=0o700)
            older = self._receipt(store, "a.json", deployment=deployment, activation=activation, certified_at="2026-09-27T01:00:00Z")
            newer = self._receipt(store, "b.json", deployment=deployment, activation=activation, certified_at="2026-09-27T02:00:00Z")
            stale = dict(policy_certification.current_identity())
            stale_identity = {field: stale[field] for field in policy_certification.IDENTITY_FIELDS}
            stale_identity[policy_certification.IDENTITY_FIELDS[0]] = "0" * 64
            self._receipt(store, "d.json", deployment=deployment, activation=activation, certified_at="2026-09-27T04:00:00Z", identity=stale_identity)
            (store / "not-a-receipt.json").write_text("{}\n", encoding="utf-8")
            found = certification.find_reusable_certification(
                deployment_receipt=deployment, policy_activation=activation, search_root=store
            )
            # 最新一份工具身份已变：不复用，取身份相同的最近一份。
            self.assertEqual(found, newer)
            self.assertEqual(
                certification.find_reusable_certification(
                    deployment_receipt=deployment, policy_activation=activation, certification=older
                ),
                older,
            )
            # 本次部署收据五摘要不是当前工具树：不是"没有可复用的认证"，而是本次部署本身不能认证。
            stale_deployment = policy_tests._deployment_receipt(
                root / "stale", policy_certification.current_identity(), control_sha256="0" * 64
            )
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "五摘要与当前工具身份不一致"):
                certification.find_reusable_certification(
                    deployment_receipt=stale_deployment, policy_activation=activation, certification=older
                )
            # 命令行：找到打印路径退出 0；没有则退出 1；本次部署不合法退出 2。
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(
                    certification.main(["find-reusable", "--search-root", str(store), "--deployment-receipt", str(deployment), "--policy-activation", str(activation)]),
                    0,
                )
            self.assertEqual(stdout.getvalue().strip(), str(newer))
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(
                    certification.main(["find-reusable", "--certification", str(store / "d.json"), "--deployment-receipt", str(deployment), "--policy-activation", str(activation)]),
                    1,
                )
                self.assertEqual(
                    certification.main(["find-reusable", "--certification", str(older), "--deployment-receipt", str(stale_deployment), "--policy-activation", str(activation)]),
                    2,
                )

    def test_reuse_survives_redeploy_with_same_identity_and_records_receipt(self) -> None:
        """第 19 项缺陷：工具身份与策略都没变、只是重新部署一次（部署收据／激活认证 sha 变了）也要复用，并写复用收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            compatibility = self._compatibility(root)
            deployment_1, activation_1 = self._deployment(root, "deploy-1", compatibility, created_at="2026-09-27T01:00:00.000Z")
            deployment_2, activation_2 = self._deployment(root, "deploy-2", compatibility, created_at="2026-09-27T02:00:00.000Z")
            self.assertNotEqual(codex_upgrade.file_sha256(deployment_1), codex_upgrade.file_sha256(deployment_2))
            self.assertNotEqual(codex_upgrade.file_sha256(activation_1), codex_upgrade.file_sha256(activation_2))
            store = root / "policy-certification"
            store.mkdir(mode=0o700)
            certified = self._receipt(store, "a.json", deployment=deployment_1, activation=activation_1, certified_at="2026-09-27T01:30:00Z")
            certified_sha256 = codex_upgrade.file_sha256(certified)
            # 换部署仍复用（目录搜索与指定单份两种口径一致）。
            self.assertEqual(
                certification.find_reusable_certification(deployment_receipt=deployment_2, policy_activation=activation_2, search_root=store),
                certified,
            )
            self.assertEqual(
                certification.find_reusable_certification(deployment_receipt=deployment_2, policy_activation=activation_2, certification=certified),
                certified,
            )
            # 复用收据：绑定被复用认证的摘要、本次部署收据、本次激活认证、五摘要与复用时刻；认证文件本身不改。
            result = certification.record_reuse(
                certified, deployment_receipt=deployment_2, policy_activation=activation_2, receipt_root=store, observed_at_utc="2026-09-27T02:05:00.000Z"
            )
            self.assertEqual(result["status"], "recorded")
            receipt_path = Path(result["reuse_receipt"])
            self.assertEqual(receipt_path.parent, store)
            self.assertEqual(receipt_path.name, "pre-a3-reuse-20260927T020500000Z.json")
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(codex_upgrade.file_sha256(certified), certified_sha256)
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            current = policy_certification.current_identity()
            self.assertEqual(payload["schema_version"], certification.REUSE_SCHEMA_VERSION)
            self.assertEqual(payload["reused_at_utc"], "2026-09-27T02:05:00.000Z")
            self.assertEqual(payload["identity"], {field: current[field] for field in policy_certification.IDENTITY_FIELDS})
            self.assertEqual(payload["policy_version"], current["policy_version"])
            certified_payload = json.loads(certified.read_text(encoding="utf-8"))
            self.assertEqual(payload["reused_certification"]["path"], str(certified))
            self.assertEqual(payload["reused_certification"]["sha256"], certified_sha256)
            self.assertEqual(payload["reused_certification"]["receipt_sha256"], certified_payload["receipt_sha256"])
            self.assertEqual(payload["reused_certification"]["bound_deployment_receipt_sha256"], codex_upgrade.file_sha256(deployment_1))
            self.assertEqual(payload["reused_certification"]["bound_policy_activation_sha256"], codex_upgrade.file_sha256(activation_1))
            self.assertEqual(payload["deployment_receipt"]["path"], str(deployment_2))
            self.assertEqual(payload["deployment_receipt"]["sha256"], codex_upgrade.file_sha256(deployment_2))
            self.assertEqual(payload["deployment_receipt"]["created_at_utc"], "2026-09-27T02:00:00.000Z")
            self.assertEqual(payload["deployment_receipt"]["supervisor_sha256"], "1" * 64)
            self.assertEqual(payload["policy_activation"]["path"], str(activation_2))
            self.assertEqual(payload["policy_activation"]["sha256"], codex_upgrade.file_sha256(activation_2))
            self.assertEqual(payload["policy_activation"]["policy_sha256"], current["policy_sha256"])
            self.assertEqual(result["binding_sha256"], payload["binding_sha256"])
            self.assertEqual(result["reused_certification_receipt_sha256"], certified_payload["receipt_sha256"])
            # 只读校验：绑定到本次部署与这份认证；换部署、换认证、篡改都拒绝。
            verified = certification.verify_reuse_receipt(
                receipt_path,
                certification=certified_payload,
                deployment_receipt_sha256=codex_upgrade.file_sha256(deployment_2),
                policy_activation_sha256=codex_upgrade.file_sha256(activation_2),
            )
            self.assertEqual(verified["receipt_sha256"], payload["receipt_sha256"])
            with self.assertRaisesRegex(certification.CertificationError, "不是本次部署收据"):
                certification.verify_reuse_receipt(receipt_path, certification=certified_payload, deployment_receipt_sha256=codex_upgrade.file_sha256(deployment_1))
            with self.assertRaisesRegex(certification.CertificationError, "不是本次激活认证"):
                certification.verify_reuse_receipt(
                    receipt_path, certification=certified_payload, deployment_receipt_sha256=codex_upgrade.file_sha256(deployment_2),
                    policy_activation_sha256=codex_upgrade.file_sha256(activation_1),
                )
            with self.assertRaisesRegex(certification.CertificationError, "不是这份路径认证"):
                certification.verify_reuse_receipt(receipt_path, certification={"receipt_sha256": "0" * 64}, deployment_receipt_sha256=codex_upgrade.file_sha256(deployment_2))
            tampered = json.loads(json.dumps(payload))
            tampered["deployment_receipt"]["sha256"] = "0" * 64
            tampered_path = policy_tests._write_json(root / "tampered-reuse.json", tampered)
            with self.assertRaisesRegex(certification.CertificationError, "自摘要不一致"):
                certification.load_reuse_receipt(tampered_path)
            resigned = {key: value for key, value in tampered.items() if key != "receipt_sha256"}
            resigned["receipt_sha256"] = certification._fingerprint(resigned)
            resigned_path = policy_tests._write_json(root / "resigned-reuse.json", resigned)
            with self.assertRaisesRegex(certification.CertificationError, "判重键与内容不一致"):
                certification.load_reuse_receipt(resigned_path)
            # 命令行：record-reuse 打印 JSON 退出 0；同一组绑定第二次返回既有收据。
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(
                    certification.main([
                        "record-reuse", "--certification", str(certified), "--deployment-receipt", str(deployment_2),
                        "--policy-activation", str(activation_2), "--receipt-root", str(store),
                    ]),
                    0,
                )
            printed = json.loads(stdout.getvalue())
            self.assertEqual(printed["status"], "existing")
            self.assertEqual(printed["reuse_receipt"], str(receipt_path))

    def test_reuse_rejects_identity_or_policy_drift(self) -> None:
        """五摘要任一不同、策略版本不同、绑定的激活策略不同：都不复用。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            compatibility = self._compatibility(root)
            deployment, activation = self._deployment(root, "deploy-1", compatibility, created_at="2026-09-27T00:00:00.000Z")
            store = root / "policy-certification"
            store.mkdir(mode=0o700)
            current = policy_certification.current_identity()
            five = {field: current[field] for field in policy_certification.IDENTITY_FIELDS}
            for field in policy_certification.IDENTITY_FIELDS:
                path = self._receipt(
                    store, f"{field}.json", deployment=deployment, activation=activation, certified_at="2026-09-27T01:00:00Z",
                    identity={**five, field: "0" * 64},
                )
                with self.subTest(field=field):
                    self.assertIsNone(
                        certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation, certification=path)
                    )
            version = self._receipt(
                store, "policy-version.json", deployment=deployment, activation=activation, certified_at="2026-09-27T01:00:00Z",
                policy_version=current["policy_version"] + 1,
            )
            self.assertIsNone(
                certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation, certification=version)
            )
            policy = self._receipt(
                store, "activation-policy.json", deployment=deployment, activation=activation, certified_at="2026-09-27T01:00:00Z",
                policy_activation={"path": str(activation), "sha256": codex_upgrade.file_sha256(activation), "policy_sha256": "0" * 64},
            )
            self.assertIsNone(
                certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation, certification=policy)
            )
            # 整目录都是漂移件：没有可复用的认证，也不会写复用收据。
            self.assertIsNone(certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation, search_root=store))
            with self.assertRaises(certification.CertificationError):
                certification.record_reuse(version, deployment_receipt=deployment, policy_activation=activation, receipt_root=store)
            self.assertEqual(list(store.glob("pre-a3-reuse-*.json")), [])

    def test_reuse_rejects_invalid_activation(self) -> None:
        """激活认证校验失败（已被替换、自摘要不一致、绑定的部署收据漂移）：不复用、不写复用收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            compatibility = self._compatibility(root)
            deployment, activation = self._deployment(root, "deploy-1", compatibility, created_at="2026-09-27T00:00:00.000Z")
            store = root / "policy-certification"
            store.mkdir(mode=0o700)
            certified = self._receipt(store, "a.json", deployment=deployment, activation=activation, certified_at="2026-09-27T01:00:00Z")
            superseded = json.loads(activation.read_text(encoding="utf-8"))
            superseded["superseded_by"] = "tool-release-certification/v1"
            superseded.pop("receipt_sha256")
            superseded["receipt_sha256"] = codex_upgrade._fingerprint(superseded)
            superseded_path = policy_tests._write_json(root / "activation-superseded.json", superseded)
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "已被替换"):
                certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=superseded_path, certification=certified)
            tampered = json.loads(activation.read_text(encoding="utf-8"))
            tampered["authorized_scopes"] = ["A3b"]
            tampered_path = policy_tests._write_json(root / "activation-tampered.json", tampered)
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "自摘要不一致"):
                certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=tampered_path, search_root=store)
            original = deployment.read_bytes()
            deployment.write_bytes(original + b"\n")
            with self.assertRaisesRegex(policy_certification.PolicyCertificationError, "摘要漂移"):
                certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation, certification=certified)
            deployment.write_bytes(original)
            with self.assertRaises(policy_certification.PolicyCertificationError):
                certification.record_reuse(certified, deployment_receipt=deployment, policy_activation=tampered_path, receipt_root=store)
            self.assertEqual(list(store.glob("pre-a3-reuse-*.json")), [])
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(
                    certification.main(["find-reusable", "--certification", str(certified), "--deployment-receipt", str(deployment), "--policy-activation", str(tampered_path)]),
                    2,
                )
                self.assertEqual(
                    certification.main([
                        "record-reuse", "--certification", str(certified), "--deployment-receipt", str(deployment),
                        "--policy-activation", str(superseded_path), "--receipt-root", str(store),
                    ]),
                    2,
                )
            # 激活认证修好后同一份认证照常复用。
            self.assertEqual(
                certification.find_reusable_certification(deployment_receipt=deployment, policy_activation=activation, certification=certified),
                certified,
            )

    def test_reuse_receipt_is_idempotent(self) -> None:
        """同一组绑定只登记一次（逐字副本也命中既有收据）；认证就是本次部署下签发的不写；换部署才新写一份。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            compatibility = self._compatibility(root)
            deployment_1, activation_1 = self._deployment(root, "deploy-1", compatibility, created_at="2026-09-27T01:00:00.000Z")
            deployment_2, activation_2 = self._deployment(root, "deploy-2", compatibility, created_at="2026-09-27T02:00:00.000Z")
            deployment_3, activation_3 = self._deployment(root, "deploy-3", compatibility, created_at="2026-09-27T03:00:00.000Z")
            store = root / "policy-certification"
            store.mkdir(mode=0o700)
            certified = self._receipt(store, "a.json", deployment=deployment_1, activation=activation_1, certified_at="2026-09-27T01:30:00Z")
            same = certification.record_reuse(certified, deployment_receipt=deployment_1, policy_activation=activation_1, receipt_root=store)
            self.assertEqual(same["status"], "not_needed")
            self.assertIsNone(same["reuse_receipt"])
            self.assertEqual(list(store.glob("pre-a3-reuse-*.json")), [])
            first = certification.record_reuse(
                certified, deployment_receipt=deployment_2, policy_activation=activation_2, receipt_root=store, observed_at_utc="2026-09-27T02:05:00.000Z"
            )
            second = certification.record_reuse(
                certified, deployment_receipt=deployment_2, policy_activation=activation_2, receipt_root=store, observed_at_utc="2026-09-27T02:06:00.000Z"
            )
            self.assertEqual(first["status"], "recorded")
            self.assertEqual(second["status"], "existing")
            self.assertEqual(second["reuse_receipt"], first["reuse_receipt"])
            self.assertEqual(second["binding_sha256"], first["binding_sha256"])
            receipts = sorted(store.glob("pre-a3-reuse-*.json"))
            self.assertEqual(receipts, [Path(first["reuse_receipt"])])
            first_bytes = receipts[0].read_bytes()
            # 逐字副本（pre-a3.sh 复制到本轮坐标）内容摘要相同，命中同一份复用收据。
            copied = root / "round-2" / "pre-a3-path-certification-round-2.json"
            copied.parent.mkdir(mode=0o700)
            shutil.copy2(certified, copied)
            third = certification.record_reuse(copied, deployment_receipt=deployment_2, policy_activation=activation_2, receipt_root=store)
            self.assertEqual(third["status"], "existing")
            self.assertEqual(third["reuse_receipt"], first["reuse_receipt"])
            self.assertEqual(receipts[0].read_bytes(), first_bytes)
            # 再换一次部署：新写一份，旧的一份不动。
            fourth = certification.record_reuse(
                certified, deployment_receipt=deployment_3, policy_activation=activation_3, receipt_root=store, observed_at_utc="2026-09-27T03:05:00.000Z"
            )
            self.assertEqual(fourth["status"], "recorded")
            self.assertNotEqual(fourth["reuse_receipt"], first["reuse_receipt"])
            self.assertNotEqual(fourth["binding_sha256"], first["binding_sha256"])
            self.assertEqual(len(sorted(store.glob("pre-a3-reuse-*.json"))), 2)
            self.assertEqual(receipts[0].read_bytes(), first_bytes)
            self.assertEqual(
                certification.find_reuse_receipt(store, first["binding_sha256"]), Path(first["reuse_receipt"])
            )
            self.assertIsNone(certification.find_reuse_receipt(store, "0" * 64))


if __name__ == "__main__":
    unittest.main()
