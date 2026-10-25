"""第 13 项：整链空跑引擎的单元测试（全部本机可跑，不依赖 ARM64）。

验证引擎的语义，而不是某个具体缺陷：
* 探针各自独立、一个失败不影响别的探针；
* 依赖被阻断（能力缺失、检查点建不出来、前序步骤没通过）一律标“未执行”并写明阻断来源，绝不标通过；
* 用例被跳过也是“未执行”；
* 完整链遇首个失败即停，后面标“未执行”；
* 历史重放：基线必须通过、注入后必须失败并命中签名，否则分别记失败／漏报；注入不上标“未执行”；
* 汇总结论的优先级、退出码、表格输出。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.official_client_capture.tests import chain_rehearsal as engine
from tools.official_client_capture.tests.chain_rehearsal import (
    CallableRunner, Checkpoint, CommandRunner, Outcome, Probe, Registry, RunContext, UnittestRunner,
)

SAMPLES = "tools.official_client_capture.tests.chain_rehearsal_sample_cases"


def _passing(_context: RunContext) -> Outcome:
    return Outcome("pass", "ok")


def _failing(_context: RunContext) -> Outcome:
    return Outcome("fail", "缺陷：MARK-A")


def _probe(probe_id: str, runner=None, **kwargs) -> Probe:
    return Probe(probe_id, f"探针 {probe_id}", kwargs.pop("phase", "VC-1"), kwargs.pop("step", "某步骤"),
                 runner or CallableRunner(_passing), **kwargs)


def _caps(**available: bool) -> dict[str, engine.Capability]:
    return {name: engine.Capability(name, value, "测试注入") for name, value in available.items()}


class ClassifyTestsTest(unittest.TestCase):
    def test_all_pass(self) -> None:
        outcome = engine.classify_tests([{"id": "a", "outcome": "ok"}], [])
        self.assertEqual(outcome.verdict, "pass")

    def test_failure_beats_skip(self) -> None:
        outcome = engine.classify_tests([{"id": "a", "outcome": "skip", "detail": "r"}, {"id": "b", "outcome": "fail", "detail": "boom"}], [])
        self.assertEqual(outcome.verdict, "fail")
        self.assertIn("b", outcome.detail)

    def test_any_skip_without_failure_is_skip_not_pass(self) -> None:
        outcome = engine.classify_tests([{"id": "a", "outcome": "ok"}, {"id": "b", "outcome": "skip", "detail": "没有 Docker"}], [])
        self.assertEqual(outcome.verdict, "skip")
        self.assertIn("没有 Docker", outcome.detail)

    def test_missing_cases_are_errors_not_failures(self) -> None:
        self.assertEqual(engine.classify_tests([], ["x.y.Z（没有匹配到任何用例）"]).verdict, "error")

    def test_no_case_ran_is_an_error(self) -> None:
        self.assertEqual(engine.classify_tests([], []).verdict, "error")


class UnittestRunnerSubprocessTest(unittest.TestCase):
    """端到端：真的起子进程，用样例用例验证逐用例结果的翻译。"""

    def _run(self, *ids: str) -> Outcome:
        with tempfile.TemporaryDirectory() as work:
            context = RunContext(tree=engine.REPO_ROOT, work=Path(work), capabilities={}, checkpoints={})
            return UnittestRunner(ids, timeout_seconds=120).run(context)

    def test_passing(self) -> None:
        outcome = self._run(f"{SAMPLES}.Passing")
        self.assertEqual((outcome.verdict, len(outcome.tests or [])), ("pass", 2), outcome.detail)

    def test_failing_reports_the_failing_case_id_and_message(self) -> None:
        outcome = self._run(f"{SAMPLES}.Failing")
        self.assertEqual(outcome.verdict, "fail")
        self.assertIn("test_boom_marker", outcome.detail)
        self.assertIn("BOOM-SIGNATURE", json.dumps(outcome.tests, ensure_ascii=False))

    def test_erroring_case_is_a_failure(self) -> None:
        self.assertEqual(self._run(f"{SAMPLES}.Erroring").verdict, "fail")

    def test_skipped_case_is_not_a_pass(self) -> None:
        outcome = self._run(f"{SAMPLES}.Skipping")
        self.assertEqual(outcome.verdict, "skip")
        self.assertIn("环境不具备", outcome.detail)

    def test_everything_skipped_is_skip(self) -> None:
        self.assertEqual(self._run(f"{SAMPLES}.AllSkipped").verdict, "skip")

    def test_subtest_failure_is_a_failure(self) -> None:
        outcome = self._run(f"{SAMPLES}.WithSubtest")
        self.assertEqual(outcome.verdict, "fail")

    def test_unknown_case_is_reported_as_error(self) -> None:
        outcome = self._run(f"{SAMPLES}.NoSuchClass")
        self.assertEqual(outcome.verdict, "error")
        self.assertIn("不存在", outcome.detail)

    def test_unknown_module_is_reported_as_error(self) -> None:
        self.assertEqual(self._run("tools.official_client_capture.tests.no_such_module_xyz").verdict, "error")

    def test_timeout_is_an_error(self) -> None:
        code = "import time; time.sleep(30)"
        with tempfile.TemporaryDirectory() as work:
            context = RunContext(tree=engine.REPO_ROOT, work=Path(work), capabilities={}, checkpoints={})
            outcome = CommandRunner([sys.executable, "-c", code], timeout_seconds=1).run(context)
        self.assertEqual(outcome.verdict, "error")
        self.assertIn("超时", outcome.detail)


class CommandRunnerTest(unittest.TestCase):
    def _run(self, runner: CommandRunner) -> Outcome:
        with tempfile.TemporaryDirectory() as work:
            context = RunContext(tree=engine.REPO_ROOT, work=Path(work), capabilities={}, checkpoints={}, origin_tree=engine.REPO_ROOT)
            return runner.run(context)

    def test_exit_codes(self) -> None:
        self.assertEqual(self._run(CommandRunner([sys.executable, "-c", "raise SystemExit(0)"])).verdict, "pass")
        self.assertEqual(self._run(CommandRunner([sys.executable, "-c", "raise SystemExit(3)"])).verdict, "fail")
        self.assertEqual(self._run(CommandRunner([sys.executable, "-c", "raise SystemExit(77)"], skip_codes=(77,))).verdict, "skip")

    def test_placeholders_in_argv_and_environment(self) -> None:
        code = "import os, sys; assert sys.argv[1] == os.environ['OUT_TREE'] == os.environ['PYTHONPATH']"
        outcome = self._run(CommandRunner([sys.executable, "-c", code, "{tree}"], extra_env={"OUT_TREE": "{tree}"}))
        self.assertEqual(outcome.verdict, "pass", outcome.output)

    def test_child_environment_never_writes_bytecode_and_pins_pythonpath(self) -> None:
        env = engine.child_environment(Path("/x/tree"), {"A": "1"})
        self.assertEqual((env["PYTHONPATH"], env["PYTHONDONTWRITEBYTECODE"], env["A"]), ("/x/tree", "1", "1"))
        self.assertNotIn("PYTHONPYCACHEPREFIX", env)


class ValidateProbesTest(unittest.TestCase):
    def test_duplicate_ids_rejected(self) -> None:
        with self.assertRaises(engine.RehearsalError):
            engine.validate_probes([_probe("a"), _probe("a")])

    def test_unknown_capability_rejected(self) -> None:
        with self.assertRaises(engine.RehearsalError):
            engine.validate_probes([_probe("a", needs=("no-such-capability",))])

    def test_unknown_checkpoint_rejected(self) -> None:
        with self.assertRaises(engine.RehearsalError):
            engine.validate_probes([_probe("a", checkpoint="nope")])

    def test_unknown_required_probe_rejected(self) -> None:
        with self.assertRaises(engine.RehearsalError):
            engine.validate_probes([_probe("a", requires_probes=("ghost",))])

    def test_cycle_rejected(self) -> None:
        with self.assertRaises(engine.RehearsalError):
            engine.validate_probes([_probe("a", requires_probes=("b",)), _probe("b", requires_probes=("a",))])


class RunRoundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="chain-rehearsal-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.work, ignore_errors=True))

    def run_round(self, probes, **kwargs):
        kwargs.setdefault("capabilities", {})
        return {item.probe_id: item for item in engine.run_round(probes, tree=engine.REPO_ROOT, work=self.work, **kwargs)}

    def test_probes_are_independent_one_failure_does_not_stop_the_rest(self) -> None:
        results = self.run_round([_probe("a"), _probe("b", CallableRunner(_failing)), _probe("c")])
        self.assertEqual({key: value.status for key, value in results.items()},
                         {"a": engine.PASSED, "b": engine.FAILED, "c": engine.PASSED})
        self.assertIn("MARK-A", results["b"].detail)

    def test_exception_in_a_probe_is_a_failure_not_a_crash(self) -> None:
        def boom(_context):
            raise ValueError("探针内部异常")
        results = self.run_round([_probe("a", CallableRunner(boom)), _probe("b")])
        self.assertEqual((results["a"].status, results["b"].status), (engine.FAILED, engine.PASSED))

    def test_skip_test_exception_is_not_a_pass(self) -> None:
        def skip(_context):
            raise unittest.SkipTest("条件不满足")
        result = self.run_round([_probe("a", CallableRunner(skip))])["a"]
        self.assertEqual(result.status, engine.NOT_RUN)
        self.assertEqual(result.blocked_by["type"], "skipped")

    def test_missing_capability_marks_not_run_with_blocker_never_passed(self) -> None:
        results = self.run_round([_probe("a", needs=("docker",)), _probe("b")], capabilities=_caps(docker=False, git=True))
        self.assertEqual(results["a"].status, engine.NOT_RUN)
        self.assertEqual((results["a"].blocked_by["type"], results["a"].blocked_by["id"]), ("capability", "docker"))
        self.assertIn("未执行", results["a"].detail)
        self.assertEqual(results["b"].status, engine.PASSED)

    def test_available_capability_runs_the_probe(self) -> None:
        results = self.run_round([_probe("a", needs=("docker",))], capabilities=_caps(docker=True))
        self.assertEqual(results["a"].status, engine.PASSED)

    def test_checkpoint_built_once_and_shared(self) -> None:
        built = []

        def builder(directory: Path, _context: RunContext) -> None:
            built.append(directory.name)
            (directory / "x.txt").write_text("ok", encoding="utf-8")

        def needs_file(context: RunContext) -> Outcome:
            return Outcome("pass") if (context.checkpoints["cp"] / "x.txt").is_file() else Outcome("fail", "检查点没传进来")

        checkpoints = {"cp": Checkpoint("cp", "检查点", builder)}
        results = self.run_round([_probe("a", CallableRunner(needs_file), checkpoint="cp"),
                                  _probe("b", CallableRunner(needs_file), checkpoint="cp")], checkpoints=checkpoints)
        self.assertEqual((results["a"].status, results["b"].status), (engine.PASSED, engine.PASSED), results["a"].detail)
        self.assertEqual(built, ["cp"])

    def test_failed_checkpoint_blocks_every_dependent_with_one_blocker(self) -> None:
        def broken(_directory: Path, _context: RunContext) -> None:
            raise RuntimeError("前序产物建不出来")

        checkpoints = {"cp": Checkpoint("cp", "检查点", broken)}
        results = self.run_round([_probe("a", checkpoint="cp"), _probe("b", checkpoint="cp"), _probe("c")], checkpoints=checkpoints)
        for name in ("a", "b"):
            self.assertEqual(results[name].status, engine.NOT_RUN)
            self.assertEqual((results[name].blocked_by["type"], results[name].blocked_by["id"]), ("checkpoint", "cp"))
            self.assertIn("前序产物建不出来", results[name].blocked_by["reason"])
        self.assertEqual(results["c"].status, engine.PASSED)

    def test_checkpoint_needing_a_missing_capability_blocks_dependents(self) -> None:
        checkpoints = {"cp": Checkpoint("cp", "检查点", lambda *_: None, needs=("docker",))}
        result = self.run_round([_probe("a", checkpoint="cp")], checkpoints=checkpoints, capabilities=_caps(docker=False))["a"]
        self.assertEqual((result.status, result.blocked_by["type"]), (engine.NOT_RUN, "checkpoint"))
        self.assertIn("docker", result.blocked_by["reason"])

    def test_required_probe_must_have_passed(self) -> None:
        results = self.run_round([
            _probe("a", CallableRunner(_failing)),
            _probe("b", requires_probes=("a",)),
            _probe("c", requires_probes=("b",)),
            _probe("d"),
        ])
        self.assertEqual(results["a"].status, engine.FAILED)
        self.assertEqual((results["b"].status, results["b"].blocked_by["id"]), (engine.NOT_RUN, "a"))
        self.assertEqual((results["c"].status, results["c"].blocked_by["id"]), (engine.NOT_RUN, "b"))
        self.assertEqual(results["d"].status, engine.PASSED)

    def test_stop_on_first_failure_marks_the_rest_not_run(self) -> None:
        probes = [_probe("a"), _probe("b", CallableRunner(_failing)), _probe("c"), _probe("d")]
        results = self.run_round(probes, stop_on_first_failure=True)
        self.assertEqual([results[name].status for name in "abcd"],
                         [engine.PASSED, engine.FAILED, engine.NOT_RUN, engine.NOT_RUN])
        self.assertEqual(results["c"].blocked_by["id"], "b")
        self.assertIn("停止", results["c"].detail)

    def test_capability_gap_does_not_stop_independent_chain_steps(self) -> None:
        probes = [_probe("a", needs=("docker",)), _probe("b"), _probe("c", CallableRunner(_failing)), _probe("d")]
        results = self.run_round(probes, stop_on_first_failure=True, capabilities=_caps(docker=False))
        self.assertEqual([results[name].status for name in "abcd"],
                         [engine.NOT_RUN, engine.PASSED, engine.FAILED, engine.NOT_RUN])
        self.assertEqual(results["d"].blocked_by["id"], "c")

    def test_progress_callback_sees_every_probe_in_order(self) -> None:
        seen = []
        engine.run_round([_probe("a"), _probe("b")], tree=engine.REPO_ROOT, work=self.work, capabilities={}, progress=lambda r: seen.append(r.probe_id))
        self.assertEqual(seen, ["a", "b"])

    def test_signature_is_checked_on_failures_only(self) -> None:
        result = self.run_round([_probe("a", CallableRunner(_failing), signature=("MARK-A",))])["a"]
        self.assertTrue(result.signature_hit)
        result = self.run_round([_probe("a", CallableRunner(_failing), signature=("OTHER",))])["a"]
        self.assertFalse(result.signature_hit)


class ReplayHistoryTest(unittest.TestCase):
    """历史重放：用一棵只有一个文件的小树，注入器把文件内容改坏。"""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="chain-rehearsal-replay-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.work, ignore_errors=True))
        self.tree = self.work / "origin"
        self.tree.mkdir()
        (self.tree / "value.txt").write_text("good\n", encoding="utf-8")

    def replay(self, probes, *, inject=None, copy_tree=None, capabilities=None):
        def default_copy(destination: Path, _extras=()) -> Path:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "value.txt").write_text((self.tree / "value.txt").read_text(encoding="utf-8"), encoding="utf-8")
            return destination

        def default_inject(root: Path, injection) -> None:
            if injection == "break":
                (root / "value.txt").write_text("bad\n", encoding="utf-8")
            elif injection == "noop":
                return
            else:
                raise RuntimeError(f"注入器应用不上：{injection}")

        return {item.probe_id: item for item in engine.replay_history(
            probes, tree=self.tree, work=self.work / "w", copy_tree=copy_tree or default_copy, inject=inject or default_inject,
            capabilities=capabilities or {})}

    @staticmethod
    def check_value(context: RunContext) -> Outcome:
        value = (context.tree / "value.txt").read_text(encoding="utf-8").strip()
        return Outcome("pass") if value == "good" else Outcome("fail", f"值被改坏：{value}  SIG-VALUE")

    def probe(self, probe_id: str, *injections, **kwargs) -> Probe:
        return _probe(probe_id, CallableRunner(self.check_value), injections=injections, signature=kwargs.pop("signature", ("SIG-VALUE",)), **kwargs)

    def test_defect_is_reproduced_when_baseline_passes_and_injection_fails_with_signature(self) -> None:
        result = self.replay([self.probe("a", "break")])["a"]
        self.assertEqual((result.status, result.signature_hit, result.baseline_status), (engine.REPRODUCED, True, engine.PASSED))

    def test_probe_without_injection_does_not_take_part(self) -> None:
        self.assertEqual(self.replay([self.probe("a"), self.probe("b", "break")]).keys(), {"b"})

    def test_missed_when_injection_does_not_change_the_outcome(self) -> None:
        result = self.replay([self.probe("a", "noop")])["a"]
        self.assertEqual(result.status, engine.MISSED)
        self.assertIn("盲区", result.detail)

    def test_missed_when_failure_does_not_carry_the_signature(self) -> None:
        result = self.replay([self.probe("a", "break", signature=("别的缺陷",))])["a"]
        self.assertEqual((result.status, result.signature_hit), (engine.MISSED, False))

    def test_baseline_failure_is_reported_and_probe_is_not_credited(self) -> None:
        (self.tree / "value.txt").write_text("bad\n", encoding="utf-8")
        result = self.replay([self.probe("a", "break")])["a"]
        self.assertEqual((result.status, result.baseline_status), (engine.FAILED, engine.FAILED))
        self.assertIn("基线", result.detail)

    def test_injection_that_cannot_be_applied_is_not_run_not_missed(self) -> None:
        result = self.replay([self.probe("a", "unknown-injection")])["a"]
        self.assertEqual((result.status, result.blocked_by["type"]), (engine.NOT_RUN, "injection"))
        self.assertIn("无法重新引入", result.detail)

    def test_missing_capability_blocks_baseline_so_replay_is_not_run(self) -> None:
        result = self.replay([self.probe("a", "break", needs=("docker",))], capabilities=_caps(docker=False))["a"]
        self.assertEqual((result.status, result.blocked_by["type"]), (engine.NOT_RUN, "capability"))

    def test_load_failure_after_injection_is_a_report_not_a_harness_error(self) -> None:
        """回归用例按修复后的接口写：缺陷回来时它可能连导入都过不了。基线能加载、注入后加载失败＝缺陷被报出。"""

        def loader(context: RunContext) -> Outcome:
            if (context.tree / "value.txt").read_text(encoding="utf-8").strip() == "good":
                return Outcome("pass")
            return Outcome("error", "探针引用的用例不存在或无法导入：ImportError(cannot import name 'new_api')", load_error=True)

        probe = _probe("a", CallableRunner(loader), injections=("break",), signature=("new_api",))
        result = self.replay([probe])["a"]
        self.assertEqual(result.status, engine.REPRODUCED)
        self.assertIn("无法加载", result.detail)

    def test_load_failure_on_the_clean_tree_is_still_a_harness_error(self) -> None:
        def loader(_context: RunContext) -> Outcome:
            return Outcome("error", "探针引用的用例不存在或无法导入", load_error=True)

        result = self.replay([_probe("a", CallableRunner(loader), injections=("break",))])["a"]
        self.assertEqual(result.status, engine.ERROR)

    def test_copy_extra_is_passed_to_the_tree_copier(self) -> None:
        seen = []

        def copy_tree(destination: Path, extras=()) -> Path:
            seen.append(tuple(extras))
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "value.txt").write_text("good\n", encoding="utf-8")
            return destination

        self.replay([self.probe("a", "break", copy_extra=("docs/x",))], copy_tree=copy_tree)
        self.assertEqual(seen, [("docs/x",)])

    def test_each_probe_gets_its_own_tree_so_injections_do_not_leak(self) -> None:
        destinations = []

        def copy_tree(destination: Path, _extras=()) -> Path:
            destinations.append(destination)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "value.txt").write_text("good\n", encoding="utf-8")
            return destination

        results = self.replay([self.probe("a", "break"), self.probe("b", "noop")], copy_tree=copy_tree)
        self.assertEqual((results["a"].status, results["b"].status), (engine.REPRODUCED, engine.MISSED))
        self.assertEqual(len(set(destinations)), 2)

    def test_probe_tree_copy_is_removed_after_the_probe(self) -> None:
        destinations = []

        def copy_tree(destination: Path, _extras=()) -> Path:
            destinations.append(destination)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "value.txt").write_text("good\n", encoding="utf-8")
            return destination

        self.replay([self.probe("a", "break")], copy_tree=copy_tree)
        self.assertTrue(destinations and not destinations[0].exists())

    def test_origin_tree_is_never_modified(self) -> None:
        self.replay([self.probe("a", "break")])
        self.assertEqual((self.tree / "value.txt").read_text(encoding="utf-8"), "good\n")


class SummaryTest(unittest.TestCase):
    def _result(self, probe_id: str, status: str, **kwargs) -> engine.ProbeResult:
        return engine.ProbeResult(probe_id, f"标题 {probe_id}", "VC-5", "步骤", "defect", status, **kwargs)

    def test_verdict_priority(self) -> None:
        verdict = engine.verdict_of
        mode = engine.MODE_ROUND
        passed, failed = self._result("a", engine.PASSED), self._result("b", engine.FAILED)
        not_run, error = self._result("c", engine.NOT_RUN), self._result("d", engine.ERROR)
        self.assertEqual(verdict([passed], mode=mode), engine.VERDICT_CLEAN)
        self.assertEqual(verdict([passed, not_run], mode=mode), engine.VERDICT_INCOMPLETE)
        self.assertEqual(verdict([passed, not_run, failed], mode=mode), engine.VERDICT_DEFECTS)
        self.assertEqual(verdict([failed, error], mode=mode), engine.VERDICT_HARNESS_ERROR)
        self.assertEqual(verdict([], mode=mode), engine.VERDICT_INCOMPLETE)
        self.assertEqual(verdict([self._result("a", engine.REPRODUCED)], mode=engine.MODE_REPLAY), engine.VERDICT_CLEAN)
        self.assertEqual(verdict([self._result("a", engine.MISSED)], mode=engine.MODE_REPLAY), engine.VERDICT_DEFECTS)

    def test_not_run_is_never_counted_as_passed_in_totals(self) -> None:
        rows = [self._result("a", engine.PASSED), self._result("b", engine.NOT_RUN, blocked_by={"type": "capability", "id": "docker", "reason": "缺少 docker"})]
        summary = engine.build_summary(rows, mode=engine.MODE_ROUND, tree=Path("/t"), capabilities={}, started_at="s", finished_at="f")
        self.assertEqual((summary["totals"]["passed"], summary["totals"]["not_run"]), (1, 1))
        self.assertEqual(summary["not_run_sources"], {"capability:docker": ["b"]})
        self.assertEqual(summary["verdict"], engine.VERDICT_INCOMPLETE)
        self.assertEqual(engine.exit_code(summary), 3)

    def test_summary_digest_is_stable_and_detects_tampering(self) -> None:
        rows = [self._result("a", engine.PASSED)]
        first = engine.build_summary(rows, mode=engine.MODE_ROUND, tree=Path("/t"), capabilities={}, started_at="s", finished_at="f")
        second = engine.build_summary(rows, mode=engine.MODE_ROUND, tree=Path("/t"), capabilities={}, started_at="s", finished_at="f")
        self.assertEqual(first["summary_sha256"], second["summary_sha256"])
        third = engine.build_summary([self._result("a", engine.FAILED)], mode=engine.MODE_ROUND, tree=Path("/t"), capabilities={}, started_at="s", finished_at="f")
        self.assertNotEqual(first["summary_sha256"], third["summary_sha256"])

    def test_table_shows_blocker_and_labels(self) -> None:
        rows = [self._result("a", engine.PASSED), self._result("b", engine.NOT_RUN, blocked_by={"type": "step", "id": "a", "reason": "前序步骤未通过"})]
        summary = engine.build_summary(rows, mode=engine.MODE_ROUND, tree=Path("/t"), capabilities={}, started_at="s", finished_at="f")
        table = engine.render_table(summary)
        self.assertIn("未执行", table)
        self.assertIn("被阻断（step:a）", table)
        self.assertIn("合计：通过 1，未执行 1", table)


class CommandLineTest(unittest.TestCase):
    """用合成登记表走完整的命令行：退出码、汇总文件、先查全再跑完整链的联动。"""

    def setUp(self) -> None:
        self.out = Path(tempfile.mkdtemp(prefix="chain-rehearsal-cli-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.out, ignore_errors=True))

    def registry(self, *, defect: bool = False, chain_fails: bool = False, capability_gap: bool = False) -> Registry:
        probes = (_probe("d1", CallableRunner(_failing if defect else _passing)), _probe("d2", needs=("docker",) if capability_gap else ()))
        chain = (_probe("c1", phase="VC-1"), _probe("c2", CallableRunner(_failing if chain_fails else _passing)), _probe("c3", phase="VC-6"))

        def copy_tree(destination: Path, _extras=()) -> Path:
            destination.mkdir(parents=True, exist_ok=True)
            return destination

        return Registry(probes=probes, chain=chain, checkpoints={}, copy_tree=copy_tree, inject=lambda *_: None)

    def run_cli(self, *argv: str, registry: Registry) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = engine.main([*argv, "--out", str(self.out), "--work", str(self.out / "work")], registry=registry)
        return code, buffer.getvalue()

    def test_clean_round_exit_zero_and_writes_summary(self) -> None:
        code, output = self.run_cli("round", registry=self.registry())
        self.assertEqual(code, 0, output)
        data = json.loads((self.out / "round.json").read_text(encoding="utf-8"))
        self.assertEqual((data["schema_version"], data["verdict"], data["total"]), (engine.SCHEMA, "clean", 2))
        self.assertTrue((self.out / "round.txt").is_file())

    def test_round_with_defect_exits_one(self) -> None:
        code, _output = self.run_cli("round", registry=self.registry(defect=True))
        self.assertEqual(code, 1)

    def test_round_with_capability_gap_exits_three_incomplete(self) -> None:
        code, output = self.run_cli("round", registry=self.registry(capability_gap=True))
        self.assertEqual(code, 3)
        self.assertIn("未执行", output)

    def test_full_chain_stops_at_first_failure(self) -> None:
        code, _ = self.run_cli("full-chain", registry=self.registry(chain_fails=True))
        data = json.loads((self.out / "full-chain.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual([row["status"] for row in data["rows"]], ["passed", "failed", "not_run"])
        self.assertEqual(data["rows"][2]["blocked_by"]["id"], "c2")

    def test_phase_filter(self) -> None:
        self.run_cli("full-chain", "--phase", "VC-6", registry=self.registry())
        data = json.loads((self.out / "full-chain.json").read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in data["rows"]], ["c3"])

    def test_selection_pulls_in_required_predecessors_in_registration_order(self) -> None:
        probes = [_probe("a"), _probe("b", requires_probes=("a",)), _probe("c", requires_probes=("b",)), _probe("d")]
        selected = engine._select(probes, ["c"], None)
        self.assertEqual([probe.probe_id for probe in selected], ["a", "b", "c"])
        self.assertEqual([probe.probe_id for probe in engine._select(probes, None, ["VC-1"])], ["a", "b", "c", "d"])

    def test_unknown_probe_is_a_usage_error(self) -> None:
        code, _ = self.run_cli("round", "--probe", "nope", registry=self.registry())
        self.assertEqual(code, 2)

    def test_rehearse_runs_chain_after_a_clean_round(self) -> None:
        code, _ = self.run_cli("rehearse", registry=self.registry())
        combined = json.loads((self.out / "rehearse.json").read_text(encoding="utf-8"))
        self.assertEqual((code, combined["verdict"], combined["chain_verdict"]), (0, "clean", "clean"))

    def test_rehearse_skips_chain_when_round_found_defects_and_says_so(self) -> None:
        code, _ = self.run_cli("rehearse", registry=self.registry(defect=True))
        chain = json.loads((self.out / "full-chain.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertTrue(all(row["status"] == "not_run" and row["blocked_by"]["type"] == "round" for row in chain["rows"]))
        self.assertEqual(chain["verdict"], engine.VERDICT_INCOMPLETE)

    def test_rehearse_chain_anyway_runs_the_chain(self) -> None:
        code, _ = self.run_cli("rehearse", "--chain-anyway", registry=self.registry(defect=True))
        chain = json.loads((self.out / "full-chain.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual([row["status"] for row in chain["rows"]], ["passed", "passed", "passed"])

    def test_list_prints_probes_and_capabilities(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = engine.main(["list"], registry=self.registry())
        self.assertEqual(code, 0)
        self.assertIn("d1", buffer.getvalue())
        self.assertIn("能力：", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
