"""四条完整链入口的登记与拒绝合同；只用轻量夹具，不运行或签发真实链。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import certify_release as release
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as faults
from tools.official_client_capture.tests.real_chains import test_claude_vc1_recorded_chain as recorded


RECORDED_MODULE = "tools.official_client_capture.tests.real_chains.test_claude_vc1_recorded_chain"
FAULT_MODULE = "tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains"
NEW_ENTRIES = {
    "vc-chain.claude-vc1-capture": (RECORDED_MODULE, "ClaudeRecordedVC1ChainTests",
                                  "test_real_vc0_closeout_supervised_copy_and_vc1_seal"),
    "vc-chain.claude-full-validation-only": (RECORDED_MODULE, "ClaudeRecordedVC1ChainTests",
                                          "test_real_vc0_through_vc6_recorded_validation"),
    "vc-chain.claude-late-stage-faults": (FAULT_MODULE, "ClaudeFaultChainTests",
                                       "test_late_stage_faults_resume_seven_scenarios"),
    "vc-chain.claude-revision-evaluator-recovery": (FAULT_MODULE, "ClaudeFaultChainTests",
                                                  "test_revision_evaluator_faults_resume_eight_scenarios"),
}


class ClaudeChainRegistrationTests(unittest.TestCase):
    """固定全集、真实入口与未验证状态，不能靠新增登记抹除未执行或失败记录。"""

    def payload(self) -> dict:
        """仅构造内存中的覆盖校验夹具，不写认证收据，也不运行任何链。"""

        registration = pre_a3.real_chain_registration("claude")
        return {"schema_version": pre_a3.SCHEMA_VERSION_V3, "client": "claude",
                "real_chain_registration": registration,
                "scenarios": [{"name": row["id"], "test": row["test"], "status": "passed"}
                              for row in registration]}

    def test_registered_set_matches_required_ids_and_existing_methods(self) -> None:
        self.assertEqual(pre_a3.CLAUDE_REAL_CHAIN_IDS, release.RELEASE_REQUIRED_CHAINS["claude"])
        self.assertEqual(tuple(row[0] for row in pre_a3.CLAUDE_SCENARIOS), pre_a3.CLAUDE_REAL_CHAIN_IDS)
        self.assertEqual(len(pre_a3.CLAUDE_REAL_CHAIN_IDS), 9)
        self.assertEqual(len(set(pre_a3.CLAUDE_REAL_CHAIN_IDS)), 9)
        rows = {row[0]: row for row in pre_a3.CLAUDE_SCENARIOS}
        for name, expected in NEW_ENTRIES.items():
            with self.subTest(chain=name):
                self.assertEqual(rows[name][2:], expected)
                case = pre_a3._load_test_case(*expected)
                self.assertTrue(callable(getattr(case, expected[2])))
                self.assertIn(expected[2], unittest.defaultTestLoader.getTestCaseNames(type(case)))

    def test_full_public_entry_uses_existing_cli_shell_and_propagates_failure(self) -> None:
        case = recorded.ClaudeRecordedVC1ChainTests("test_real_vc0_through_vc6_recorded_validation")
        with mock.patch.object(case, "_run_pending_full_validation", side_effect=AssertionError("完整 CLI 实际失败")) as run:
            with self.assertRaisesRegex(AssertionError, "完整 CLI 实际失败"):
                case.test_real_vc0_through_vc6_recorded_validation()
        run.assert_called_once_with()
        self.assertFalse(hasattr(case, "real_chain_metrics"))

    def test_late_public_entry_uses_exact_seven_scene_chain(self) -> None:
        case = faults.ClaudeFaultChainTests("test_late_stage_faults_resume_seven_scenarios")
        with mock.patch.object(faults.sys, "platform", "linux"), \
                mock.patch.object(faults.os, "geteuid", return_value=0), \
                mock.patch.object(faults.os, "uname", return_value=SimpleNamespace(machine="aarch64")), \
                mock.patch.object(case, "_run_chain") as run:
            case.test_late_stage_faults_resume_seven_scenarios()
        run.assert_called_once_with("late-continuous")
        self.assertEqual(faults.CHAINS["late-continuous"]["expected"], faults.LATE_CONTINUOUS_SCENARIOS)
        self.assertEqual(len(faults.LATE_CONTINUOUS_SCENARIOS), 7)
        self.assertNotIn("late-continuous", faults.PENDING_CONTINUOUS_CHAINS)

    def test_revision_public_entry_delegates_original_shell_and_propagates_first_failure(self) -> None:
        """登记只增加宿主边界；八场景仍走原外壳，真实首败不得被包装为通过。"""

        method = NEW_ENTRIES["vc-chain.claude-revision-evaluator-recovery"][2]
        case = faults.ClaudeFaultChainTests(method)
        with mock.patch.object(faults.sys, "platform", "linux"), \
                mock.patch.object(faults.os, "geteuid", return_value=0), \
                mock.patch.object(faults.os, "uname", return_value=SimpleNamespace(machine="aarch64")), \
                mock.patch.object(case, "_run_pending_revision_continuous", side_effect=AssertionError("原八场景真实首败")) as run:
            with self.assertRaisesRegex(AssertionError, "原八场景真实首败"):
                case.test_revision_evaluator_faults_resume_eight_scenarios()
        run.assert_called_once_with()
        self.assertFalse(hasattr(case, "real_chain_metrics"))
        self.assertEqual(faults.CHAINS["revision-continuous"]["expected"], faults.REVISION_CONTINUOUS_SCENARIOS)
        self.assertEqual(len(faults.REVISION_CONTINUOUS_SCENARIOS), 8)
        self.assertEqual((faults.CHAIN_BUDGET_SECONDS, faults.CHAIN_TIMEOUT_SECONDS), (900, 870))

    def test_revision_wrong_os_user_or_architecture_remains_uncertified(self) -> None:
        """本机、非 root 与非 ARM64 结果均不可进入认证，且不启动原链。"""

        rows = {row[0]: row for row in pre_a3.CLAUDE_SCENARIOS}
        for platform, user_id, architecture in (("darwin", 0, "arm64"), ("linux", 1000, "aarch64"), ("linux", 0, "x86_64")):
            with self.subTest(platform=platform, user_id=user_id, architecture=architecture), \
                    mock.patch.object(faults.sys, "platform", platform), \
                    mock.patch.object(faults.os, "geteuid", return_value=user_id), \
                    mock.patch.object(faults.os, "uname", return_value=SimpleNamespace(machine=architecture)), \
                    mock.patch.object(faults.ClaudeFaultChainTests, "_run_pending_revision_continuous") as run:
                result = pre_a3.run_scenario(rows["vc-chain.claude-revision-evaluator-recovery"])
                self.assertEqual(result["status"], "uncertified")
                self.assertNotIn("metrics", result)
                run.assert_not_called()

    def test_late_degraded_host_and_missing_recorded_sources_are_uncertified(self) -> None:
        rows = {row[0]: row for row in pre_a3.CLAUDE_SCENARIOS}
        with mock.patch.object(faults.sys, "platform", "darwin"), \
                mock.patch.object(faults.ClaudeFaultChainTests, "_run_chain") as run:
            result = pre_a3.run_scenario(rows["vc-chain.claude-late-stage-faults"])
        self.assertEqual(result["status"], "uncertified")
        run.assert_not_called()
        with mock.patch.dict(os.environ, {"CLAUDE_2_1_226_RELAY_FIXTURE": "", "CLAUDE_2_1_226_FREEZE_FIXTURE": ""}):
            for name in ("vc-chain.claude-vc1-capture", "vc-chain.claude-full-validation-only"):
                with self.subTest(chain=name):
                    result = pre_a3.run_scenario(rows[name])
                    self.assertEqual(result["status"], "uncertified")
                    self.assertNotIn("metrics", result)

    def test_registration_alone_and_every_new_nonpass_result_reject_coverage(self) -> None:
        payload = self.payload()
        payload["scenarios"] = [row for row in payload["scenarios"] if row["name"] not in NEW_ENTRIES]
        with self.assertRaisesRegex(pre_a3.CertificationError, "真实链未认证"):
            pre_a3.real_chain_coverage(payload, client="claude")
        for name in NEW_ENTRIES:
            for status in ("unverified", "uncertified", "failed", "skipped"):
                with self.subTest(chain=name, status=status):
                    payload = self.payload()
                    for row in payload["scenarios"]:
                        if row["name"] == name:
                            row["status"] = status
                    with self.assertRaisesRegex(pre_a3.CertificationError, "真实链未认证"):
                        pre_a3.real_chain_coverage(payload, client="claude")

    def test_complete_fixture_coverage_is_checked_but_wrong_entry_or_old_set_is_rejected(self) -> None:
        payload = self.payload()
        coverage = pre_a3.real_chain_coverage(payload, client="claude")
        self.assertEqual(tuple(row["id"] for row in coverage), release.RELEASE_REQUIRED_CHAINS["claude"])
        payload["scenarios"][-1]["test"] = "旧失败世界:私有入口"
        with self.assertRaisesRegex(pre_a3.CertificationError, "入口与登记不一致"):
            pre_a3.real_chain_coverage(payload, client="claude")
        payload = self.payload()
        payload["real_chain_registration"] = [row for row in payload["real_chain_registration"] if row["id"] not in NEW_ENTRIES]
        with self.assertRaisesRegex(pre_a3.CertificationError, "与当前发布包不一致"):
            pre_a3.real_chain_coverage(payload, client="claude")


class RegisteredLateIntegrityTests(unittest.TestCase):
    """七场景与八场景分别保留六项／同 Campaign 校验；替身只测读侧，不产生真实链结果。"""

    def run_outer(self, records: list[dict], *, chain: str = "late-continuous") -> None:
        metrics = {"chain": chain, "steps": ["轻量夹具步骤"], "upstream_requests": 0, "scenarios": records}
        completed = subprocess.CompletedProcess(["轻量替身"], 0, faults.METRICS_PREFIX + json.dumps(metrics) + "\n", "")
        method = NEW_ENTRIES[faults.CHAINS[chain]["scenario"]][2]
        case = faults.ClaudeFaultChainTests(method)
        with tempfile.TemporaryDirectory(prefix="registered-late-integrity-") as directory, \
                mock.patch.object(case, "_isolation_mode", return_value=faults.shell.ISOLATION_NAMESPACE), \
                mock.patch.object(faults.shell, "_scratch_root", return_value=directory), \
                mock.patch.object(faults, "_copy_tree", side_effect=lambda destination: destination), \
                mock.patch.object(faults.trees, "subprocess_env", return_value={}), \
                mock.patch.object(faults.shell, "_namespace_argv", side_effect=lambda argv: argv), \
                mock.patch.object(faults.subprocess, "run", return_value=completed) as command:
            case._run_chain(chain)
        self.assertEqual(command.call_args.kwargs["timeout"], 870, "新增登记不能放宽原子进程超时")

    def records(self, chain: str = "late-continuous") -> list[dict]:
        return [{"scenario": name, "campaign_id": "同一夹具Campaign",
                 "checks": {key: {"passed": True} for key in faults.SIX_CHECKS}}
                for name in faults.CHAINS[chain]["expected"]]

    def test_exact_ordered_six_checks_from_one_campaign_pass_the_outer_validator(self) -> None:
        for chain in ("late-continuous", "revision-continuous"):
            with self.subTest(chain=chain):
                self.run_outer(self.records(chain), chain=chain)

    def test_mixed_or_missing_campaigns_are_rejected_after_formal_registration(self) -> None:
        for chain in ("late-continuous", "revision-continuous"):
            for value in ("另一个Campaign", ""):
                with self.subTest(chain=chain, campaign=value):
                    rows = self.records(chain)
                    rows[-1]["campaign_id"] = value
                    with self.assertRaises(AssertionError):
                        self.run_outer(rows, chain=chain)

    def test_missing_or_nonpass_check_is_rejected(self) -> None:
        for chain in ("late-continuous", "revision-continuous"):
            with self.subTest(chain=chain):
                rows = self.records(chain)
                del rows[-1]["checks"]["history"]
                with self.assertRaisesRegex(AssertionError, "六项真实通过记录"):
                    self.run_outer(rows, chain=chain)
                rows = self.records(chain)
                rows[-1]["checks"]["history"]["passed"] = False
                with self.assertRaisesRegex(AssertionError, "六项真实通过记录"):
                    self.run_outer(rows, chain=chain)

    def test_missing_extra_or_reordered_scenarios_cannot_pass_after_registration(self) -> None:
        """入口登记后仍要求原闭集与顺序，不能删场景、拼场景或重复记录。"""

        for chain in ("late-continuous", "revision-continuous"):
            for mutation in ("missing", "extra", "reordered"):
                with self.subTest(chain=chain, mutation=mutation):
                    rows = self.records(chain)
                    if mutation == "missing":
                        rows.pop()
                    elif mutation == "extra":
                        rows.append(rows[-1])
                    else:
                        rows.reverse()
                    with self.assertRaisesRegex(AssertionError, "六项真实通过记录"):
                        self.run_outer(rows, chain=chain)


if __name__ == "__main__":
    unittest.main()
