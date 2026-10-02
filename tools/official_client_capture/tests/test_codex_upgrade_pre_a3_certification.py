"""A2.5：pre-A3 路径认证——staging 内 fixture_only 总账、网络守卫、场景全过才出收据。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
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


EXECUTOR = Path(__file__).resolve().parents[3] / "tools" / "ci" / "unit_executor.py"
REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_SCENARIOS = (
    ("fake.alpha", "替身场景一", "tools.fake", "FakeTests", "test_alpha"),
    ("fake.beta", "替身场景二", "tools.fake", "FakeTests", "test_beta"),
)


class PreA3ParallelCertificationTests(unittest.TestCase):
    """E2-03：场景交给执行器按单元并行，issue 核对全集与网络计数后按同一形状签发；没通过只写旁路文件。"""

    def _bindings(self, root: Path) -> tuple[Path, Path]:
        return PreA3CertificationTests._bindings(self, root)  # type: ignore[arg-type]

    def _issue(self, root: Path, staging: Path, summary: dict) -> dict:
        deployment, activation = self._bindings(root)
        summary_path = root / f"summary-{len(list(root.glob('summary-*.json')))}.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        return certification.issue_certification(staging, executor_summary=summary_path, deployment_receipt=deployment, policy_activation=activation)

    @staticmethod
    def _synthetic(staging: Path, names: list[str], *, network: dict[str, int] | None = None) -> dict:
        """合成每个场景一份通过的结果与一份执行器汇总（退出码与结论自洽），供变异用。"""

        results = staging / certification.RESULTS_DIR_NAME
        results.mkdir(parents=True, exist_ok=True)
        rows = []
        for name in names:
            count = (network or {}).get(name, 0)
            payload = {"schema_version": certification.SCENARIO_RESULT_SCHEMA, "name": name, "network_attempts": count,
                       "scenario": {"name": name, "description": "替身", "test": "tools.fake:FakeTests.x", "status": "passed", "seconds": 0.1},
                       "finished_at_utc": "2026-10-01T00:00:00.000Z"}
            (results / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            rows.append({"unit_id": f"{certification.SCENARIO_UNIT_PREFIX}{name}", "kind": "formal", "exit_code": 0 if count == 0 else 1,
                         "signal": None, "timed_out": False, "seconds": 0.1, "passed": count == 0})
        return {"schema_version": certification.EXECUTOR_SUMMARY_SCHEMA, "status": "passed", "units": rows, "diagnostic": []}

    def test_issue_refuses_missing_duplicate_killed_timed_out_stray_and_networked_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", FAKE_SCENARIOS):
            root = Path(directory).resolve()
            names = certification.scenario_names()
            self.assertEqual(names, ["fake.alpha", "fake.beta", certification.ACCOUNTING_SCENARIO_NAME])

            def case(label: str) -> tuple[Path, dict]:
                staging = root / "data" / "staging" / label
                return staging, self._synthetic(staging, names)

            staging, summary = case("baseline")
            receipt = self._issue(root, staging, summary)
            self.assertEqual((receipt["status"], receipt["failed_scenarios"], receipt["network_attempts"], receipt["scenario_count"]), ("passed", [], 0, 3))

            mutations = {}
            staging, summary = case("missing")
            (staging / "results" / "fake.beta.json").unlink()
            summary["units"] = [row for row in summary["units"] if not row["unit_id"].endswith("fake.beta")]
            mutations["少报一个场景"] = (staging, summary, "fake.beta", "缺报")
            staging, summary = case("duplicated")
            summary["units"].append(dict(summary["units"][0]))
            mutations["重复报一个"] = (staging, summary, "fake.alpha", "多次")
            staging, summary = case("killed")
            (staging / "results" / "fake.alpha.json").unlink()
            summary["units"][0].update(exit_code=None, signal=9, passed=False)
            mutations["杀掉一个场景的子进程"] = (staging, summary, "fake.alpha", "信号 9")
            staging, summary = case("timed-out")
            summary["units"][1].update(exit_code=None, signal=15, timed_out=True, passed=False)
            mutations["一个场景超时"] = (staging, summary, "fake.beta", "超时（执行器以信号 15 终止）")
            staging, summary = case("mismatch")
            summary["units"][0].update(exit_code=1, passed=False)
            mutations["退出码与结论不符"] = (staging, summary, "fake.alpha", "退出码 1 与场景结论不符")
            for label, (staging, summary, scenario, reason) in mutations.items():
                with self.subTest(label):
                    receipt = self._issue(root, staging, summary)
                    self.assertEqual(receipt["status"], "failed")
                    self.assertIn(scenario, receipt["failed_scenarios"])
                    row = next(item for item in receipt["scenarios"] if item["name"] == scenario)
                    self.assertIn(reason, row["error"])
            # 某个子进程上报网络计数 1：场景本身通过，但总和不为 0，不签发。
            staging = root / "data" / "staging" / "network"
            receipt = self._issue(root, staging, self._synthetic(staging, names, network={"fake.alpha": 1}))
            self.assertEqual((receipt["status"], receipt["network_attempts"]), ("failed", 1))
            # 结果里没有网络计数、或有全集之外的结果文件，都不签发。
            staging, summary = case("no-count")
            payload = json.loads((staging / "results" / "fake.alpha.json").read_text(encoding="utf-8"))
            payload.pop("network_attempts")
            (staging / "results" / "fake.alpha.json").write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn("fake.alpha", self._issue(root, staging, summary)["failed_scenarios"])
            staging, summary = case("stray")
            stray = json.loads((staging / "results" / "fake.alpha.json").read_text(encoding="utf-8"))
            (staging / "results" / "fake.gamma.json").write_text(json.dumps({**stray, "name": "fake.gamma"}), encoding="utf-8")
            receipt = self._issue(root, staging, summary)
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("全集之外的结果：fake.gamma.json", receipt["failed_scenarios"])
            # 同一场景换个编号又派发了一次（汇总里多出全集之外的单元）：即使结果只有一份也不签发。
            staging, summary = case("redispatched")
            summary["units"].append({**summary["units"][0], "unit_id": "pre-a3:fake.alpha#2"})
            receipt = self._issue(root, staging, summary)
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("全集之外的执行单元：pre-a3:fake.alpha#2", receipt["failed_scenarios"])
            # 汇总的 units 混入非正式执行的行：汇总本身非法，直接拒绝。
            staging, summary = case("bad-row")
            summary["units"].append({**summary["units"][0], "kind": "diagnostic"})
            with self.assertRaisesRegex(certification.CertificationError, "单元行非法"):
                self._issue(root, staging, summary)
            # 诊断执行的结果不参与签发。
            staging, summary = case("diagnostic")
            (staging / "results" / "fake.alpha.diagnostic.json").write_text("{坏", encoding="utf-8")
            self.assertEqual(self._issue(root, staging, summary)["status"], "passed")

    def test_scenario_result_is_published_once(self) -> None:
        """同一场景第二次上报（例如被重复派发）直接失败，第一次的结果原样保留；诊断重跑另写 .diagnostic.json。"""

        record = {"name": "fake.alpha", "description": "替身", "test": "tools.fake:FakeTests.x", "status": "passed", "seconds": 0.1}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", FAKE_SCENARIOS), \
                mock.patch.object(certification, "run_scenario", return_value=record), mock.patch.dict(os.environ, {}, clear=False):
            root = Path(directory).resolve()
            result = root / "data" / "staging" / "cert" / "results" / "fake.alpha.json"
            argv = ["run-scenario", "--name", "fake.alpha", "--staging-root", str(root / "data" / "staging" / "cert" / "scenarios" / "fake.alpha"),
                    "--result", str(result)]
            os.environ.pop("UNIT_EXECUTOR_KIND", None)
            previous_tempdir = tempfile.tempdir
            try:
                with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    self.assertEqual(certification.main(argv), 0)
                    first = result.read_bytes()
                    self.assertEqual(certification.main(argv), 2, "第二次上报必须失败")
                self.assertEqual(result.read_bytes(), first)
                self.assertEqual(sorted(path.name for path in result.parent.iterdir()), ["fake.alpha.json"], "不留临时文件")
                os.environ["UNIT_EXECUTOR_KIND"] = "diagnostic"
                with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    self.assertEqual(certification.main(argv), 0)
                self.assertTrue(result.with_name("fake.alpha.diagnostic.json").is_file())
                self.assertEqual(result.read_bytes(), first)
            finally:
                tempfile.tempdir = previous_tempdir
                os.environ.pop(certification.FIXTURE_ONLY_ENV, None)

    def test_failed_certification_goes_to_a_side_file_and_staging_roots_are_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            output = root / "control" / "pre-a3-certification-x.json"
            output.parent.mkdir()
            failed = {"schema_version": certification.SCHEMA_VERSION, "status": "failed", "receipt_sha256": "0" * 64}
            first = certification.write_certification(output, failed)
            second = certification.write_certification(output, failed)
            self.assertFalse(output.exists(), "没通过的认证不得写正式路径")
            self.assertNotEqual(first, second)
            self.assertTrue(all(path.name.startswith("pre-a3-certification-x.failed-") for path in (first, second)))
            passed = {**failed, "status": "passed"}
            self.assertEqual(certification.write_certification(output, passed), output)
            staging = root / "data" / "staging" / "pre-a3"
            (staging / "old").mkdir(parents=True)
            fresh = certification._fresh_staging_root(staging)
            self.assertNotEqual(fresh, staging)
            self.assertTrue(fresh.name.startswith("pre-a3-"))
            self.assertEqual(certification._fresh_staging_root(root / "data" / "staging" / "empty"), root / "data" / "staging" / "empty")

    def test_parallel_issue_matches_serial_run_and_verifies(self) -> None:
        """真跑几个快速场景：plan → 执行器 run-commands 并行 → issue，与串行 run 逐场景结论一致，签发的认证能通过核验。
        诊断重跑写到 .diagnostic.json，不覆盖正式结果。"""

        quick = tuple(scenario for scenario in certification.SCENARIOS if scenario[0] in {
            "harden-evidence-permissions.two-step", "batch.uncommitted-not-pushed", "vc-chain.ledger-events-derivation"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", quick):
            root = Path(directory).resolve()
            deployment, activation = self._bindings(root)
            manifest = certification.plan_scenario_units(root / "data" / "staging" / "parallel", cwd=REPO_ROOT)
            staging = Path(manifest["staging_root"])
            self.assertEqual([unit["unit_id"] for unit in manifest["units"]], [f"pre-a3:{name}" for name in certification.scenario_names()])
            manifest_path = root / "units.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(EXECUTOR), "run-commands", "--manifest", str(manifest_path), "--parallel", "3",
                 "--state-dir", str(root / "state"), "--out-dir", str(staging / "executor"), "--shared-caches", "off"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=900, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            receipt = certification.issue_certification(staging, executor_summary=staging / "executor" / "summary.json",
                                                        deployment_receipt=deployment, policy_activation=activation)
            self.assertEqual((receipt["status"], receipt["network_attempts"]), ("passed", 0), receipt["failed_scenarios"])
            serial = certification.run_certification(root / "data" / "staging" / "serial", deployment_receipt=deployment,
                                                     policy_activation=activation, scenarios=quick)
            self.assertEqual([(row["name"], row["status"]) for row in receipt["scenarios"]],
                             [(row["name"], row["status"]) for row in serial["scenarios"]], "并行与串行逐场景结论一致")
            self.assertEqual(set(receipt) ^ set(serial), set(), "收据形状不变")
            output = certification.write_certification(root / "pre-a3.json", receipt)
            self.assertEqual(certification.verify_certification(output)["scenario_count"], len(quick) + 1)
            # 执行器的诊断重跑用同一条命令：结果与临时目录另放，不覆盖正式执行。
            unit = next(item for item in manifest["units"] if item["unit_id"].endswith("batch.uncommitted-not-pushed"))
            formal = Path(unit["argv"][unit["argv"].index("--result") + 1])
            before = formal.read_bytes()
            rerun = subprocess.run(unit["argv"], cwd=unit["cwd"], capture_output=True, text=True, timeout=600,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UNIT_EXECUTOR_KIND": "diagnostic"})
            self.assertEqual(rerun.returncode, 0, rerun.stderr[-2000:])
            self.assertEqual(formal.read_bytes(), before)
            self.assertTrue(formal.with_name(formal.stem + ".diagnostic.json").is_file())


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


# ---------------------------------------------------------------------------
# E3-02：场景单元可承接，认证从单元执行记录组装（v2）
# ---------------------------------------------------------------------------


def _ci_module(name: str):
    """tools/ci 下的模块（不是受管代码）按路径加载：测试用它造记录库、跑执行器，并交叉核对认证模块照写的核验算法。"""

    spec = importlib.util.spec_from_file_location(f"{name}_for_pre_a3_tests", REPO_ROOT / "tools" / "ci" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


V1_FIELDS = {
    "schema_version", "status", "certified_at_utc", "staging_root", "fixture_only", "identity", "policy_version", "deployment_receipt",
    "policy_activation", "campaign_run_rehearsal_receipt", "real_chain_registration", "scenarios", "scenario_count", "failed_scenarios",
    "network_attempts", "live_request_count", "scanned_bytes", "receipt_sha256",
}


class _SyntheticStore:
    """合成记录库：每个场景一条通过的正式执行记录、日志里一行场景结果，运行清单发布进库；变异由调用方指定。"""

    def __init__(self, root: Path) -> None:
        self.ur = _ci_module("unit_records")
        self.root = root
        self.store = self.ur.RecordStore(root / "store")
        self.logs = 0

    def result(self, name: str, *, network: int = 0, status: str = "passed", kind: str = "formal") -> dict:
        return certification._sealed({
            "schema_version": certification.SCENARIO_RESULT_V2_SCHEMA, "name": name, "kind": kind,
            "scenario": {"name": name, "description": "替身", "test": "tools.fake:FakeTests.x", "status": status, "seconds": 0.1},
            "network_attempts": network, "finished_at_utc": "2026-10-02T00:00:00.000Z", "staging_root": "/staging/x",
        }, "result_sha256")

    @staticmethod
    def log_text(result: dict | None, *, copies: int = 1) -> str:
        lines = ["场景自己的输出，没有换行结尾"]
        lines += [certification.SCENARIO_RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True)] * (copies if result else 0)
        return "\n".join(lines) + "\n"

    def record(self, name: str, run_id: str, *, log_text: str | None = None, completed: str = "2026-10-02T00:00:00Z",
               **overrides: object) -> dict:
        self.logs += 1
        log = self.root / f"log-{self.logs}.log"
        log.write_text(self.log_text(self.result(name)) if log_text is None else log_text, encoding="utf-8")
        digest = self.ur.file_sha256(log)
        self.store.put_log(log, digest)
        body = {
            "unit_id": f"{certification.SCENARIO_UNIT_PREFIX}{name}", "unit_type": "command", "kind": "formal",
            "run": {"run_id": run_id, "mode": "full-set-pass", "out_dir": "/out"}, "executor": {"sha256": "e"}, "policy_sha256": "p",
            "environment": [], "environment_sha256": self.ur.entries_sha256([]), "spec": {}, "spec_sha256": f"spec-{name}",
            "inputs": [], "inputs_sha256": f"inputs-{name}", "inheritable": True, "not_inheritable_reason": None, "test_ids": [],
            "tests": None, "passed": True, "exit_code": 0, "signal": None, "timed_out": False, "seconds": 0.1,
            "log": {"path": str(log), "sha256": digest}, "started_at_utc": completed, "completed_at_utc": completed, **overrides,
        }
        record = self.ur.seal_record(body)
        self.store.put_record(record)
        return record

    @staticmethod
    def entry(record: dict, disposition: str = "executed") -> dict:
        return {"unit_id": record["unit_id"], "disposition": disposition, "record_sha256": record["record_sha256"],
                "record_path": "/不用这个路径", "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"]}

    def manifest(self, run_id: str, entries: list[dict], *, mode: str = "full-set-pass", decided: str = "2026-10-02T01:00:00Z",
                 publish: bool = True) -> Path:
        manifest = self.ur.build_manifest(
            run_id=run_id, mode=mode, planned_units=[entry["unit_id"] for entry in entries], units=entries, decided_at_utc=decided,
            inheritance_max_age_hours=168.0, policy_sha256="p", environment=[], environment_sha256=self.ur.entries_sha256([]),
            executor={"sha256": "e"}, test_groups={}, diagnostic=[])
        if publish:
            self.store.put_manifest(manifest)
        path = self.root / f"unit-manifest-{run_id}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    def run(self, run_id: str, names: list[str]) -> tuple[Path, list[dict]]:
        records = [self.record(name, run_id) for name in names]
        return self.manifest(run_id, [self.entry(record) for record in records]), records


class PreA3RecordCertificationTests(unittest.TestCase):
    """E3-02：场景单元命令稳定、结果写成日志里的一行；签发从运行清单与记录库组装 v2，核验逐条重验记录。"""

    def _bindings(self, root: Path) -> tuple[Path, Path]:
        return PreA3CertificationTests._bindings(self, root)  # type: ignore[arg-type]

    def _issue(self, store: _SyntheticStore, manifest: Path, deployment: Path, activation: Path) -> dict:
        return certification.issue_certification_from_records(
            store.root / "data" / "staging" / "pre-a3-scenarios", unit_manifest=manifest, record_store=store.store.root,
            deployment_receipt=deployment, policy_activation=activation)

    def test_verification_algorithm_matches_unit_records(self) -> None:
        ur = _ci_module("unit_records")
        value = {"b": [1, "中文"], "a": {"x": None, "y": 1.5}}
        self.assertEqual(certification._sha256_json(value), ur.sha256_json(value))
        self.assertEqual(certification._sealed(value, "k"), ur.seal(value, "k"))
        self.assertTrue(certification._seal_matches(ur.seal(value, "k"), "k"))
        with tempfile.TemporaryDirectory() as directory:
            store = ur.RecordStore(Path(directory))
            ours = certification._RecordStore(Path(directory))
            self.assertEqual(ours.record_path("pre-a3:x", "a" * 64), store.record_path("pre-a3:x", "a" * 64))
            self.assertEqual(ours.log_path("b" * 64), store.log_path("b" * 64))
            self.assertEqual(ours.manifest_path("run-1"), store.manifest_path("run-1"))
        good = {"unit_type": "command", "exit_code": 0, "signal": None, "timed_out": False, "passed": True}
        for record in (good, {**good, "exit_code": 1}, {**good, "signal": 9}, {**good, "timed_out": True}, {**good, "passed": False}):
            self.assertEqual(certification._record_passes(record)[0], ur.derived_pass(record)[0], record)

    def test_issue_from_records_signs_v2_and_verify_rechecks_every_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", FAKE_SCENARIOS):
            root = Path(directory).resolve()
            deployment, activation = self._bindings(root)
            store = _SyntheticStore(root)
            manifest, records = store.run("run-1", certification.scenario_names())
            receipt = self._issue(store, manifest, deployment, activation)
            self.assertEqual((receipt["schema_version"], receipt["status"], receipt["failed_scenarios"], receipt["network_attempts"]),
                             (certification.SCHEMA_VERSION_V2, "passed", [], 0))
            self.assertEqual(set(receipt), V1_FIELDS | {"unit_manifest", "record_store"}, "v1 字段一个不少，只多两项引用")
            self.assertEqual([row["unit_record"]["record_sha256"] for row in receipt["scenarios"]], [record["record_sha256"] for record in records])
            self.assertEqual({row["unit_record"]["disposition"] for row in receipt["scenarios"]}, {"executed"})
            self.assertEqual((receipt["unit_manifest"]["run_id"], receipt["unit_manifest"]["mode"]), ("run-1", "full-set-pass"))
            output = certification.write_certification(root / "pre-a3.json", receipt)
            self.assertEqual(certification.verify_certification(output)["schema_version"], certification.SCHEMA_VERSION_V2)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(certification.main(["verify", "--certification", str(output)]), 0)
            # 签发之后改了记录库里的一份日志：核验逐条重验，认证不再有效；改回来又有效。
            log = store.store.log_path(records[0]["log"]["sha256"])
            original = log.read_bytes()
            log.write_bytes(original + b"x")
            with self.assertRaisesRegex(certification.CertificationError, "重验不通过"):
                certification.verify_certification(output)
            log.write_bytes(original)
            certification.verify_certification(output)
            # 收据里的逐场景结果被改：与重验结果不一致。
            forged = dict(receipt, scenarios=[{**receipt["scenarios"][0], "seconds": 9.9}, *receipt["scenarios"][1:]])
            forged["receipt_sha256"] = certification._fingerprint({k: v for k, v in forged.items() if k != "receipt_sha256"})
            forged_path = certification.write_certification(root / "forged.json", forged)
            with self.assertRaisesRegex(certification.CertificationError, "不一致"):
                certification.verify_certification(forged_path)
            # 运行清单从记录库里删掉：核验不通过。
            store.store.manifest_path("run-1").unlink()
            with self.assertRaisesRegex(certification.CertificationError, "运行清单"):
                certification.verify_certification(output)

    def test_issue_from_records_refuses_bad_or_incomplete_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", FAKE_SCENARIOS):
            root = Path(directory).resolve()
            deployment, activation = self._bindings(root)
            names = certification.scenario_names()

            def build(label: str, mutate: str) -> tuple[_SyntheticStore, Path]:
                base = root / label
                base.mkdir()
                store = _SyntheticStore(base)
                records = []
                for name in names:
                    if name == "fake.alpha" and mutate == "not-passed":
                        records.append(store.record(name, "run-1", exit_code=1, passed=False))
                    elif name == "fake.alpha" and mutate in ("network", "duplicate-line", "no-line", "diagnostic", "scenario-failed"):
                        result = {"network": store.result(name, network=1), "diagnostic": store.result(name, kind="diagnostic"),
                                  "scenario-failed": store.result(name, status="failed")}.get(mutate, store.result(name))
                        text = store.log_text(None if mutate == "no-line" else result, copies=2 if mutate == "duplicate-line" else 1)
                        records.append(store.record(name, "run-1", log_text=text))
                    elif name == "fake.alpha" and mutate == "other-run":
                        records.append(store.record(name, "run-0"))
                    else:
                        records.append(store.record(name, "run-1"))
                entries = [store.entry(record) for record in records]
                if mutate == "missing":
                    entries = [entry for entry in entries if not entry["unit_id"].endswith("fake.beta")]
                if mutate == "stray":
                    entries.append(store.entry(store.record("fake.gamma", "run-1")))
                if mutate == "spec":
                    entries[0]["spec_sha256"] = "另一份规格"
                if mutate == "tampered":
                    path = store.store.record_path(records[0]["unit_id"], records[0]["record_sha256"])
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    path.write_text(json.dumps({**payload, "seconds": 0.2}), encoding="utf-8")
                return store, store.manifest("run-1", entries)

            cases = {
                "运行清单里少一个场景": ("missing", "fake.beta", "缺报"),
                "执行记录被改": ("tampered", "fake.alpha", "自摘要不符"),
                "执行没通过": ("not-passed", "fake.alpha", "执行记录不是通过"),
                "网络计数不为 0": ("network", "fake.alpha", "网络连接尝试 1 次"),
                "日志里有两行结果": ("duplicate-line", "fake.alpha", "不是恰好一行（2 行）"),
                "日志里没有结果行": ("no-line", "fake.alpha", "不是恰好一行（0 行）"),
                "结果来自诊断执行": ("diagnostic", "fake.alpha", "不是正式执行的结果"),
                "场景本身没通过": ("scenario-failed", "fake.alpha", "场景没通过"),
                "登记为本次执行却来自别的运行": ("other-run", "fake.alpha", "来自别的运行"),
                "规格与清单不符": ("spec", "fake.alpha", "规格或输入摘要"),
            }
            for label, (mutate, scenario, reason) in cases.items():
                with self.subTest(label):
                    store, manifest = build(mutate, mutate)
                    receipt = self._issue(store, manifest, deployment, activation)
                    self.assertEqual(receipt["status"], "failed")
                    self.assertIn(scenario, receipt["failed_scenarios"])
                    row = next(item for item in receipt["scenarios"] if item["name"] == scenario)
                    self.assertIn(reason, row["error"])
                    self.assertNotIn("unit_record", row)
            store, manifest = build("stray", "stray")
            self.assertIn("全集之外的执行单元：pre-a3:fake.gamma", self._issue(store, manifest, deployment, activation)["failed_scenarios"])
            # 清单没发布进记录库（执行器的清单自检没过时不发布）、或被改过：直接拒绝签发。
            store = _SyntheticStore(root / "unpublished")
            store.root.mkdir()
            records = [store.record(name, "run-1") for name in names]
            unpublished = store.manifest("run-1", [store.entry(record) for record in records], publish=False)
            with self.assertRaisesRegex(certification.CertificationError, "没有发布到记录库"):
                self._issue(store, unpublished, deployment, activation)
            store, manifest = build("edited", "none")
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            manifest.write_text(json.dumps({**payload, "mode": "re-execute"}), encoding="utf-8")
            with self.assertRaisesRegex(certification.CertificationError, "自摘要不符"):
                self._issue(store, manifest, deployment, activation)

    def test_inherited_scenarios_must_trace_to_a_formal_execution_within_the_age_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", FAKE_SCENARIOS):
            root = Path(directory).resolve()
            deployment, activation = self._bindings(root)
            names = certification.scenario_names()
            store = _SyntheticStore(root)
            _first, records = store.run("run-1", names)
            inherited = [store.entry(record, "inherited") for record in records]
            receipt = self._issue(store, store.manifest("run-2", inherited, decided="2026-10-02T05:00:00Z"), deployment, activation)
            self.assertEqual(receipt["status"], "passed", receipt["failed_scenarios"])
            self.assertEqual({(row["unit_record"]["disposition"], row["unit_record"]["run_id"]) for row in receipt["scenarios"]},
                             {("inherited", "run-1")})
            certification.verify_certification(certification.write_certification(root / "inherited.json", receipt))
            cases = {
                "超过承接期限": (store.manifest("run-3", inherited, decided="2026-10-09T01:00:01Z"), "承接期限"),
                "重新执行全集的清单里有承接项": (store.manifest("run-4", inherited, mode="re-execute"), "不得有承接项"),
            }
            orphan = store.record("fake.alpha", "run-never-published")
            cases["原运行清单不存在"] = (store.manifest("run-5", [store.entry(orphan, "inherited"), *inherited[1:]]), "原运行的清单缺失")
            unlisted = store.record("fake.alpha", "run-6")
            store.manifest("run-6", [store.entry(records[0]), *[store.entry(record) for record in records[1:]]])
            cases["原运行清单没把它列为正式执行"] = (store.manifest("run-7", [store.entry(unlisted, "inherited"), *inherited[1:]]),
                                                    "没有把这条记录列为该场景的正式执行")
            for label, (manifest, reason) in cases.items():
                with self.subTest(label):
                    receipt = self._issue(store, manifest, deployment, activation)
                    self.assertEqual(receipt["status"], "failed")
                    row = next(item for item in receipt["scenarios"] if item["name"] == "fake.alpha")
                    self.assertIn(reason, row["error"])

    def test_run_scenario_unit_has_a_stable_command_and_prints_one_sealed_result_line(self) -> None:
        passed = {"name": "fake.alpha", "description": "替身", "test": "tools.fake:FakeTests.test_alpha", "status": "passed", "seconds": 0.1}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", FAKE_SCENARIOS):
            root = Path(directory).resolve()
            parent = root / "data" / "staging" / "pre-a3-scenarios"
            first = certification.plan_scenario_commands(parent, python="/usr/bin/python3", cwd=REPO_ROOT)
            self.assertEqual(first, certification.plan_scenario_commands(parent, python="/usr/bin/python3", cwd=REPO_ROOT),
                             "两次规划的单元逐字相同（命令稳定，入口门禁才能承接）")
            self.assertEqual(first["units"][0]["argv"][-4:], ["--name", "fake.alpha", "--staging-parent", str(parent)])
            self.assertEqual([unit["scenario_test_file"] for unit in first["units"]],
                             ["tools/fake.py", "tools/fake.py", "tools/official_client_capture/tests/test_codex_upgrade.py"])
            with self.assertRaisesRegex(certification.CertificationError, "staging"):
                certification.plan_scenario_commands(root / "outside")
            previous_tempdir = tempfile.tempdir
            try:
                for label, record, kind, expected_code in (("通过", passed, None, 0), ("失败", {**passed, "status": "failed"}, None, 1),
                                                           ("诊断", passed, "diagnostic", 0)):
                    with self.subTest(label), mock.patch.object(certification, "run_scenario", return_value=record), \
                            mock.patch.dict(os.environ, {}, clear=False):
                        os.environ.pop("UNIT_EXECUTOR_KIND", None)
                        if kind:
                            os.environ["UNIT_EXECUTOR_KIND"] = kind
                        out = io.StringIO()
                        with contextlib.redirect_stdout(out), mock.patch("sys.stderr"):
                            code = certification.main(["run-scenario", "--name", "fake.alpha", "--staging-parent", str(parent)])
                        self.assertEqual(code, expected_code)
                        lines = [line for line in out.getvalue().splitlines() if line.startswith(certification.SCENARIO_RESULT_PREFIX)]
                        self.assertEqual(len(lines), 1, out.getvalue())
                        result = json.loads(lines[0][len(certification.SCENARIO_RESULT_PREFIX):])
                        self.assertTrue(certification._seal_matches(result, "result_sha256"))
                        self.assertEqual((result["schema_version"], result["name"], result["kind"], result["network_attempts"]),
                                         (certification.SCENARIO_RESULT_V2_SCHEMA, "fake.alpha", kind or "formal", 0))
                        staging_root = Path(result["staging_root"])
                        self.assertEqual(staging_root.parent, parent)
                        self.assertEqual(staging_root.exists(), record["status"] != "passed", "通过的临时根删掉，失败的保留供排查")
                        self.assertEqual(staging_root.name.startswith("diagnostic-"), bool(kind))
            finally:
                tempfile.tempdir = previous_tempdir
                os.environ.pop(certification.FIXTURE_ONLY_ENV, None)
            with mock.patch("sys.stderr"):
                self.assertEqual(certification.main(["run-scenario", "--name", "fake.alpha", "--staging-parent", str(parent),
                                                     "--result", str(root / "r.json")]), 2)
                self.assertEqual(certification.main(["run-scenario", "--name", "fake.alpha", "--result", str(root / "r.json")]), 2)

    def test_scenarios_run_as_gate_units_are_inherited_and_signed_end_to_end(self) -> None:
        """真跑：认证 plan → 入口门禁清单（pre-a3 组合）→ 执行器 run-gates（带记录库，全集通过）→ 签 v2；同一棵树再跑一轮，
        场景全部承接、一个不执行，照样签发并通过核验。执行器在临时 git 仓库里算输入，场景命令在本仓库里跑。"""

        quick = tuple(scenario for scenario in certification.SCENARIOS if scenario[0] in {
            "harden-evidence-permissions.two-step", "vc-chain.ledger-events-derivation"})
        entry_gates = _ci_module("entry_gates")
        executor = _ci_module("unit_executor")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(certification, "SCENARIOS", quick):
            base = Path(directory).resolve()
            deployment, activation = self._bindings(base)
            repo = base / "repo"
            repo.mkdir()
            modules = {certification.scenario_test_module(name).replace(".", "/") + ".py" for name in certification.scenario_names()}
            for relative in sorted(modules | {"tools/official_client_capture/tests/__init__.py"}):
                (repo / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO_ROOT / relative, repo / relative)
            git = ["git", "-C", str(repo), "-c", "user.name=e3", "-c", "user.email=e3@example.invalid", "-c", "commit.gpgsign=false"]
            subprocess.run([*git, "init", "-q"], check=True)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "场景测试模块"], check=True)
            parent = base / "data" / "staging" / "pre-a3-scenarios"
            units = base / "pre-a3-units.json"
            units.write_text(json.dumps(certification.plan_scenario_commands(parent, cwd=REPO_ROOT), ensure_ascii=False), encoding="utf-8")
            manifest = entry_gates.plan_gates(repo, profile="pre-a3", launcher=[], pre_a3_units=units,
                                              pre_a3_data_inputs=[{"category": "docs", "name": "tree:maintenance", "sha256": "0" * 64, "detail": {}}])
            self.assertTrue(all(unit.get("inheritable", True) and "inputs" in unit for unit in manifest["units"]))
            gates = base / "gates.json"
            gates.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            config = base / "config.json"
            config.write_text(json.dumps({"schema_version": executor.CONFIG_SCHEMA, "default_parallelism": 3,
                                          "default_quota": {"cores": 1, "memory_mb": 1024}, "quotas": {}, "splits": {}, "exclusive": [],
                                          "unit_timeout_seconds": 900, "orphan_grace_seconds": 1}), encoding="utf-8")
            store = base / "store"
            environment = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
            environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)})
            receipts = []
            for run in (1, 2):
                out = base / f"out-{run}"
                completed = subprocess.run(
                    [sys.executable, str(EXECUTOR), "run-gates", "--manifest", str(gates), "--config", str(config),
                     "--weights", str(base / "none.json"), "--durations", str(base / "none.json"), "--parallel", "3", "--cores", "4",
                     "--state-dir", str(base / "state"), "--out-dir", str(out), "--shared-caches", "off", "--record-store", str(store),
                     "--mode", "full-set-pass"], cwd=repo, capture_output=True, text=True, timeout=900, env=environment)
                self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
                counts = json.loads((out / "unit-manifest.json").read_text(encoding="utf-8"))["counts"]
                self.assertEqual((counts["executed"], counts["inherited"]), (len(quick) + 1, 0) if run == 1 else (0, len(quick) + 1))
                receipt = certification.issue_certification_from_records(
                    parent, unit_manifest=out / "unit-manifest.json", record_store=store, deployment_receipt=deployment,
                    policy_activation=activation)
                self.assertEqual((receipt["status"], receipt["network_attempts"]), ("passed", 0), receipt["failed_scenarios"])
                self.assertEqual({row["unit_record"]["disposition"] for row in receipt["scenarios"]}, {"executed" if run == 1 else "inherited"})
                receipts.append(certification.write_certification(base / f"pre-a3-{run}.json", receipt))
            for path in receipts:
                self.assertEqual(certification.verify_certification(path)["scenario_count"], len(quick) + 1)
            self.assertEqual(list(parent.iterdir()), [], "通过的场景不留临时根")


if __name__ == "__main__":
    unittest.main()
