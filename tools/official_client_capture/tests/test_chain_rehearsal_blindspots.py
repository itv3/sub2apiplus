"""整链空跑补盲区探针（chain_rehearsal_blindspots）的执行器测试。

三类上一版没被空跑拦住的问题（新版二进制拒收调用点参数、标签与判据对不上真实候选证据、收尾环境变量下的测试隔离）各有
一个探针；本文件测执行器本身的判定，探针放回历史缺陷后能否被报出（replay-history）见最后一个类。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import chain_rehearsal as engine
from tools.official_client_capture.tests import chain_rehearsal_blindspots as blindspots
from tools.official_client_capture.tests import chain_rehearsal_catalog as catalog

REPO = Path(__file__).resolve().parents[3]
TESTS_DIRECTORY = Path(__file__).resolve().parent
FAKE = TESTS_DIRECTORY / "fixtures" / "chain_rehearsal" / "fake_codex_daemon_gate.py"
BLINDSPOT_IDS = [probe.probe_id for probe in catalog.BLINDSPOT_PROBES]


class BinaryOracleTest(unittest.TestCase):
    """假客户端复刻“带功能开关覆盖必须 --no-daemon”的规则。"""

    def run_once(self, arguments: list[str]) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            return blindspots.run_binary_once(
                [sys.executable, str(FAKE)], arguments, window_seconds=3.0, isolate=False, home=Path(directory)
            )

    def test_override_without_no_daemon_is_rejected_with_the_reason(self) -> None:
        result = self.run_once(["--model", "m", "--disable", "plugins", "--disable", "apps"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["exited"], 1)
        self.assertIn("Cannot use the shared background server", result["output_head"])
        self.assertTrue(result["matched_patterns"])

    def test_override_with_no_daemon_and_plain_start_are_accepted(self) -> None:
        for arguments in (["--no-daemon", "--disable", "plugins"], ["--model", "m"], ["--no-daemon"]):
            with self.subTest(arguments=arguments):
                result = self.run_once(arguments)
                self.assertFalse(result["rejected"], result)
                self.assertIsNone(result["exited"])  # 窗口结束时仍在运行，被探测方杀掉


class ClassifyStartupTest(unittest.TestCase):
    def combo(self, *, rejected: bool, scenario: str = "compact-tui") -> dict:
        return {"scenario": scenario, "arguments": ["--model", "m"], "exited": 1 if rejected else None,
                "rejected": rejected, "matched_patterns": [], "output_head": "boom" if rejected else ""}

    def test_pass_fail_and_error_shapes(self) -> None:
        self.assertEqual(blindspots.classify_startup({"combos": [self.combo(rejected=False)]}).verdict, "pass")
        failed = blindspots.classify_startup({"combos": [self.combo(rejected=False), self.combo(rejected=True)]})
        self.assertEqual(failed.verdict, "fail")
        self.assertIn("1/2", failed.detail)
        self.assertEqual(blindspots.classify_startup({"combos": []}).verdict, "error")
        self.assertEqual(
            blindspots.classify_startup({"combos": [self.combo(rejected=False)], "scenarios_without_command": ["x"]}).verdict,
            "error",
        )


class StartupFlagRunnerTest(unittest.TestCase):
    def context(self, directory: str) -> engine.RunContext:
        return engine.RunContext(tree=REPO, work=Path(directory), capabilities={}, checkpoints={})

    def test_without_a_real_binary_the_probe_is_skipped_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, clear=False):
            os.environ.pop("REHEARSAL_CODEX_BIN", None)
            outcome = blindspots.StartupFlagRunner().run(self.context(directory))
        self.assertEqual(outcome.verdict, "skip")
        self.assertIn("REHEARSAL_CODEX_BIN", outcome.detail)

    def test_missing_binary_path_is_a_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"REHEARSAL_CODEX_BIN": "/nonexistent/codex"}):
            outcome = blindspots.StartupFlagRunner().run(self.context(directory))
        self.assertEqual(outcome.verdict, "error")

    def test_real_binary_requires_a_network_isolated_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"REHEARSAL_CODEX_BIN": str(FAKE)}):
            with mock.patch.object(blindspots, "unshare_network_available", return_value=(False, "测试：没有 unshare")):
                outcome = blindspots.StartupFlagRunner().run(self.context(directory))
        self.assertEqual(outcome.verdict, "skip")
        self.assertIn("无网络", outcome.detail)

    def test_current_tree_call_sites_are_accepted_by_the_fake_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outcome = blindspots.StartupFlagRunner(binary=(sys.executable, str(FAKE)), window_seconds=2.0).run(self.context(directory))
        self.assertEqual(outcome.verdict, "pass", outcome.detail + outcome.output)
        self.assertRegex(outcome.detail, r"\d+ 个客户端命令行都能启动")


class CloseoutEnvironmentTest(unittest.TestCase):
    def test_names_come_from_run_gates_source_and_include_the_retry_ledger(self) -> None:
        names = blindspots.closeout_gate_environment(REPO)
        self.assertIn("UNIT_EXECUTOR_RETRY_LEDGER", names)
        # 只取 ``{**os.environ, ...}`` 那一个字典的键：全是环境变量风格的大写名，不会混进 run_gates 里别的字典的键。
        self.assertTrue(all(re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in names), sorted(names))
        self.assertNotIn("ARM64_VC_ENV", names)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", names)

    def test_module_list_covers_every_test_that_nests_an_executor(self) -> None:
        """清单自检：含 ``**os.environ`` 且碰执行器／门禁／后台验证的 test_ci_* 模块都得在清单里，新增这类模块要补进来。"""

        listed = set(blindspots.CLOSEOUT_NESTED_EXECUTOR_MODULES)
        self.assertLessEqual(set(blindspots.CLOSEOUT_COPY_TREE_MODULES), listed)
        # 上一版实际红掉的三个模块任何清单里都必须有。
        self.assertLessEqual(
            {"test_ci_background_validation_retry", "test_ci_retry_ledger_across_closeouts", "test_ci_unit_executor_retry"},
            set(blindspots.CLOSEOUT_COPY_TREE_MODULES),
        )
        for module in listed:
            self.assertTrue((TESTS_DIRECTORY / f"{module}.py").is_file(), module)
        missing = []
        for path in sorted(TESTS_DIRECTORY.glob("test_ci_*.py")):
            text = path.read_text(encoding="utf-8")
            if "**os.environ" in text and re.search(r"unit_executor|run_gates|entry-gates|entry_gates|background_validation", text):
                if path.stem not in listed:
                    missing.append(path.stem)
        # 已登记为不嵌套执行器的例外（只读环境、不起执行器），新增例外要写明理由。
        exceptions: set[str] = set()
        self.assertEqual(sorted(set(missing) - exceptions), [], "这些模块嵌套起执行器却不在收尾环境探针的清单里")


class CatalogRegistrationTest(unittest.TestCase):
    def test_blindspot_probes_are_registered_and_have_self_checks(self) -> None:
        registry = catalog.registry()
        ids = {probe.probe_id for probe in registry.probes}
        self.assertEqual(set(BLINDSPOT_IDS) - ids, set())
        injected = {probe.probe_id for probe in catalog.BLINDSPOT_PROBES if probe.injections}
        # 三类问题各有一个能放回历史缺陷的自检探针；两个需要真实外部输入的探针（真二进制、上一版候选证据）只能如实“未执行”。
        self.assertEqual(
            injected,
            {"tui-callsite-flags-vs-binary-selfcheck", "closeout-env-nested-executor-isolation", "label-declaration-vs-real-candidate-stream"},
        )

    def test_scope_note_no_longer_claims_the_candidate_replay_is_missing(self) -> None:
        self.assertIn("prior-candidate-seal-assert-replay", catalog.CHAIN_SCOPE_NOTE)


class PriorCandidateReplayRunnerTest(unittest.TestCase):
    def test_without_evidence_the_probe_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, clear=False):
            os.environ.pop("REHEARSAL_PRIOR_CANDIDATE_EVIDENCE", None)
            context = engine.RunContext(tree=REPO, work=Path(directory), capabilities={}, checkpoints={})
            outcome = blindspots.PriorCandidateReplayRunner().run(context)
        self.assertEqual(outcome.verdict, "skip")

    def test_newest_target_files_pick_the_highest_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "tools" / "official_client_capture"
            base.mkdir(parents=True)
            for name in ("codex_upgrade_evidence_labels_0_9_0.json", "codex_upgrade_evidence_labels_0_10_0.json",
                         "candidate_rule_expectations_0_9_0.json", "candidate_rule_expectations_0_10_0.json"):
                (base / name).write_text("{}", encoding="utf-8")
            labels, profile = blindspots.newest_target_files(Path(directory))
        self.assertEqual((labels.name, profile.name), ("codex_upgrade_evidence_labels_0_10_0.json", "candidate_rule_expectations_0_10_0.json"))


if __name__ == "__main__":
    unittest.main()
