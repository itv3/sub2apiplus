"""试验区快线：结论三态、失败归因落到状态文件、启动前检查、检查线编排、耗时对比字段，以及快线记录不能当 U-4 收据。

只用合成仓库与合成检查线（几行 python），不执行真实门禁。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.upstream_merge import fast_lane, gate_runner
from tools.upstream_merge.canonical import artifact_binding, file_binding, write_json_once
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.fast_attribution import CLASS_FROZEN_PENDING, CLASS_FUNCTIONAL
from tools.upstream_merge.fast_lane import (
    RESULT_FAILED,
    RESULT_PASSED,
    RESULT_PENDING,
    build_fast_lanes,
    fast_exit_code,
    fast_result,
    prepare_fast_run,
    render_checklist,
    run_fast,
    timing_comparison,
)
from tools.upstream_merge.gate_runner import (
    ATTRIBUTION_EGRESS_SPEC,
    ATTRIBUTION_GO_TEST,
    FAST_STATUS_SCHEMA,
    STATUS_SCHEMA,
    Lane,
    Step,
    StepResult,
)
from tools.upstream_merge.tests.test_targeted_regression import (
    STANDARD_PACKAGES,
    TargetedRegressionTestCase,
    sh,
)
from tools.upstream_merge.workflow import _load_runner_status

SOURCE_ROOT = Path(__file__).resolve().parents[3]
MODULE = "github.com/Wei-Shaw/sub2api"

# 冻结摘要类的 go test 输出（officialegress 包，失败信息是摘要比对）与一个功能失败（handler 包）。
FROZEN_GO_OUTPUT = (
    "--- FAIL: TestFooSourceTransitionIsFrozen (0.00s)\n"
    "    foo_transition_test.go:12: foo transition 当前摘要不一致：a.go\n"
    "FAIL\n"
    f"FAIL\t{MODULE}/internal/officialegress\t0.5s\n"
)
FUNCTIONAL_GO_OUTPUT = (
    "--- FAIL: TestBillingRoundsUp (0.00s)\n"
    "    billing_test.go:9: expected 3 got 2\n"
    "FAIL\n"
    f"FAIL\t{MODULE}/internal/handler\t0.5s\n"
)


def python_step(name: str, code: str, **kwargs) -> Step:
    return Step(name=name, argv=(sys.executable, "-c", code), **kwargs)


def failing_output_step(name: str, output: str, *, attribution: str | None = ATTRIBUTION_GO_TEST) -> Step:
    code = f"import sys; sys.stdout.write({output!r}); sys.exit(1)"
    return python_step(name, code, attribution=attribution)


class FastFixture(TargetedRegressionTestCase):
    """合成仓库（候选提交已知、树干净）+ 与它绑定的定向回归清单。"""

    def setUp(self) -> None:
        super().setUp()
        self.out = self.temp / "out"
        self.out.mkdir()
        fork, upstream = self.basic_merge()
        self.document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        self.manifest_path = self.out / "targeted.json"
        write_json_once(self.manifest_path, self.document)
        self.request = self.out / "request.json"
        self.request.write_text('{"request": {"gates": []}}\n', encoding="utf-8")

    def prepare(self, **overrides):
        arguments = dict(
            targeted=self.manifest_path,
            request=self.request,
            status_file=None,
            log_dir=self.out / "logs",
            compare_with=None,
            check_frontend=False,
        )
        arguments.update(overrides)
        return prepare_fast_run(self.scenario.root, Path("/nonexistent/codex"), **arguments)

    def run_synthetic(self, lanes: list[Lane], **overrides) -> tuple[int, dict, str, "fast_lane.FastContext"]:
        context = self.prepare(**overrides)
        stream = io.BytesIO()
        code, document = run_fast(context, lanes=lanes, stream=stream, integration=(True, None))
        return code, document, stream.getvalue().decode("utf-8"), context


class VerdictTest(unittest.TestCase):
    def result(self, status: str, classification: str | None = None) -> StepResult:
        return StepResult("lane", "step", status, 0 if status == "passed" else 1, 0.1, classification=classification)

    def test_three_verdicts(self) -> None:
        passed = [self.result("passed"), self.result("not_executed")]
        pending = [self.result("passed"), self.result("failed", CLASS_FROZEN_PENDING)]
        functional = [self.result("failed", CLASS_FROZEN_PENDING), self.result("failed", CLASS_FUNCTIONAL)]
        self.assertEqual(fast_result(passed), RESULT_PASSED)
        self.assertEqual(fast_result(pending), RESULT_PENDING)
        self.assertEqual(fast_result(functional), RESULT_FAILED)
        self.assertEqual([fast_exit_code(item) for item in (RESULT_PASSED, RESULT_PENDING, RESULT_FAILED)], [0, 0, 1])

    def test_a_failure_without_any_attribution_is_functional(self) -> None:
        self.assertEqual(fast_result([self.result("failed", None)]), RESULT_FAILED)
        self.assertEqual(fast_result([self.result("failed", "something-unknown")]), RESULT_FAILED)
        self.assertEqual(fast_lane.classification_of(self.result("failed", None)), CLASS_FUNCTIONAL)
        self.assertIsNone(fast_lane.classification_of(self.result("passed", CLASS_FROZEN_PENDING)))


class PreconditionTest(FastFixture):
    def test_valid_inputs_prepare_a_context_bound_to_head_and_manifest_hash(self) -> None:
        context = self.prepare()
        self.assertEqual(context.commit, sh(self.scenario.root, "git", "rev-parse", "HEAD"))
        self.assertEqual(context.manifest_sha256, file_binding(self.manifest_path)["sha256"])
        self.assertEqual(context.status_file, context.log_dir / "fast-status.json")
        self.assertTrue(context.log_dir.is_dir())

    def test_targeted_manifest_is_required(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "定向回归清单"):
            self.prepare(targeted=None)

    def test_manifest_for_another_candidate_commit_is_rejected(self) -> None:
        self.scenario.write({"backend/internal/c/c.go": "package c // later\n"})
        self.scenario.commit("候选又变了")
        with self.assertRaisesRegex(UpstreamMergeError, "重新生成"):
            self.prepare()
        self.assertFalse((self.out / "logs").exists())

    def test_dirty_worktree_is_rejected(self) -> None:
        (self.scenario.root / "backend/internal/c/c.go").write_text("package c // dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "未提交"):
            self.prepare()

    def test_existing_log_dir_status_file_or_missing_inputs_are_rejected_without_leftovers(self) -> None:
        (self.out / "logs").mkdir()
        with self.assertRaisesRegex(UpstreamMergeError, "日志目录已存在"):
            self.prepare()
        with self.assertRaisesRegex(UpstreamMergeError, "状态文件已存在"):
            self.prepare(log_dir=self.out / "fresh", status_file=self.manifest_path)
        self.assertFalse((self.out / "fresh").exists())
        with self.assertRaisesRegex(UpstreamMergeError, "状态文件所在目录不存在"):
            self.prepare(log_dir=self.out / "fresh", status_file=self.out / "no-such-dir" / "status.json")
        self.assertFalse((self.out / "fresh").exists())
        with self.assertRaisesRegex(UpstreamMergeError, "request"):
            self.prepare(log_dir=self.out / "fresh", request=self.out / "absent.json")
        with self.assertRaisesRegex(UpstreamMergeError, "对比用的全量状态文件"):
            self.prepare(log_dir=self.out / "fresh", compare_with=self.out / "absent.json")
        self.assertFalse((self.out / "fresh").exists())

    def test_tampered_manifest_is_rejected(self) -> None:
        tampered = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        tampered["go"]["fallback_full_go"] = True
        path = self.out / "tampered.json"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(UpstreamMergeError):
            self.prepare(targeted=path)


class RunFastTest(FastFixture):
    def test_all_passed_writes_a_complete_status_and_exits_zero(self) -> None:
        lanes = [Lane("alpha", (python_step("one", "print('第一步')"), python_step("two", "print('第二步')")))]
        code, document, output, context = self.run_synthetic(lanes)
        self.assertEqual(code, 0)
        self.assertEqual(document["result"], RESULT_PASSED)
        self.assertEqual(document["schema_version"], FAST_STATUS_SCHEMA)
        self.assertEqual(document["mode"], "fast")
        self.assertTrue(document["not_u4_receipt"])
        self.assertIn("不是 U-4 全量收据", document["notice"])
        self.assertEqual(document["executed"], ["alpha/one", "alpha/two"])
        self.assertEqual(document["failed_functional"], [])
        self.assertEqual(document["pending_ledger"], [])
        self.assertIn("快线结论：passed", output)
        self.assertIn("不是 U-4 全量收据", output)
        # 状态文件落盘一次，内容与返回一致；绑定清单文件的 sha256 与日志目录
        on_disk = json.loads(context.status_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, document)
        self.assertEqual(document["targeted"]["sha256"], file_binding(self.manifest_path)["sha256"])
        self.assertEqual(document["targeted"]["identity_sha256"], self.document["identity_sha256"])
        self.assertEqual(document["repository"]["commit"], context.commit)
        self.assertEqual(document["log_dir"], str(context.log_dir))
        with self.assertRaises(FileExistsError):
            fast_lane.write_fast_status(context.status_file, document)

    def test_each_step_records_status_exit_code_duration_and_a_log_range_that_holds_its_output(self) -> None:
        lanes = [Lane("alpha", (python_step("one", "print('第一步输出')"), python_step("two", "print('第二步输出')")))]
        _code, document, _output, context = self.run_synthetic(lanes)
        first, second = document["steps"]
        self.assertEqual((first["lane"], first["step"], first["status"], first["exit_code"]), ("alpha", "one", "passed", 0))
        self.assertIsInstance(first["duration_seconds"], float)
        data = Path(first["log"]["path"]).read_bytes()
        self.assertIn("第一步输出", data[first["log"]["start"] : first["log"]["end"]].decode("utf-8"))
        self.assertNotIn("第二步输出", data[first["log"]["start"] : first["log"]["end"]].decode("utf-8"))
        self.assertIn("第二步输出", data[second["log"]["start"] : second["log"]["end"]].decode("utf-8"))
        self.assertTrue(Path(first["log"]["path"]).is_relative_to(context.log_dir))

    def test_frozen_digest_failure_is_pending_ledger_and_does_not_block(self) -> None:
        lanes = [Lane("egress", (failing_output_step("go-test", FROZEN_GO_OUTPUT),)), Lane("lint", (python_step("ok", "pass"),))]
        code, document, output, _context = self.run_synthetic(lanes)
        self.assertEqual(code, 0)
        self.assertEqual(document["result"], RESULT_PENDING)
        step = document["steps"][0]
        self.assertEqual((step["status"], step["classification"]), ("failed", CLASS_FROZEN_PENDING))
        self.assertEqual(step["detail"]["failed_tests"], ["TestFooSourceTransitionIsFrozen"])
        self.assertEqual(document["pending_ledger"], ["egress/go-test"])
        self.assertEqual(document["failed_functional"], [])
        self.assertIn("待台账闭合", output)
        self.assertIn("passed_pending_ledger", output)

    def test_functional_failure_blocks_even_next_to_a_frozen_one(self) -> None:
        lanes = [
            Lane("egress", (failing_output_step("go-test", FROZEN_GO_OUTPUT),)),
            Lane("targeted", (failing_output_step("go-test-default-targeted", FUNCTIONAL_GO_OUTPUT),)),
        ]
        code, document, output, _context = self.run_synthetic(lanes)
        self.assertEqual(code, 1)
        self.assertEqual(document["result"], RESULT_FAILED)
        self.assertEqual(document["failed_functional"], ["targeted/go-test-default-targeted"])
        self.assertEqual(document["pending_ledger"], ["egress/go-test"])
        self.assertIn("不得建正式 Plan", output)

    def test_unattributed_failures_are_functional(self) -> None:
        lanes = [Lane("lint", (failing_output_step("golangci-lint-default", "lint error\n", attribution=None),))]
        code, document, _output, _context = self.run_synthetic(lanes)
        self.assertEqual(code, 1)
        self.assertEqual(document["steps"][0]["classification"], CLASS_FUNCTIONAL)
        self.assertEqual(document["result"], RESULT_FAILED)

    def test_failure_to_even_start_a_step_is_functional(self) -> None:
        lanes = [Lane("lint", (Step("missing", ("/nonexistent/binary-for-test",), attribution=ATTRIBUTION_GO_TEST),))]
        code, document, _output, _context = self.run_synthetic(lanes)
        self.assertEqual(code, 1)
        self.assertEqual(document["steps"][0]["classification"], CLASS_FUNCTIONAL)

    def test_egress_spec_step_is_attributed_through_the_executor_summary(self) -> None:
        # 假的统一调度执行器：把汇总与失败单元日志写到 $UNIT_EXECUTOR_OUT_DIR/check-egress-spec/ 下，与 entry_gates 一致。
        code = (
            "import json, os, pathlib, sys\n"
            "base = pathlib.Path(os.environ['UNIT_EXECUTOR_OUT_DIR']) / 'check-egress-spec'\n"
            "base.mkdir(parents=True)\n"
            f"log = base / 'unit.log'; log.write_text({FROZEN_GO_OUTPUT!r}, encoding='utf-8')\n"
            "units = [{'unit_id': 'egress-spec:egress-spec-go-vet', 'passed': True, 'log': str(base / 'v.log')},\n"
            "         {'unit_id': 'egress-spec:egress-spec-go-test', 'passed': False, 'exit_code': 2, 'log': str(log)}]\n"
            "(base / 'summary.json').write_text(json.dumps({'units': units, 'diagnostic': []}))\n"
            "sys.exit(2)\n"
        )
        step = python_step(
            "check-egress-spec", code, unit_executor=True, unit_summary_subdir="check-egress-spec", attribution=ATTRIBUTION_EGRESS_SPEC
        )
        exit_code, document, output, _context = self.run_synthetic([Lane("egress-spec", (step,))])
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["result"], RESULT_PENDING)
        self.assertEqual(document["steps"][0]["classification"], CLASS_FROZEN_PENDING)
        # 未通过单元的日志照例附进线日志（供事后查看）
        self.assertIn("附：未通过单元 egress-spec:egress-spec-go-test", output)

    def test_egress_spec_step_with_another_failed_unit_is_functional(self) -> None:
        code = (
            "import json, os, pathlib, sys\n"
            "base = pathlib.Path(os.environ['UNIT_EXECUTOR_OUT_DIR']) / 'check-egress-spec'\n"
            "base.mkdir(parents=True)\n"
            f"log = base / 'unit.log'; log.write_text({FROZEN_GO_OUTPUT!r}, encoding='utf-8')\n"
            "units = [{'unit_id': 'egress-spec:egress-spec-gofmt', 'passed': False, 'exit_code': 1, 'log': str(log)},\n"
            "         {'unit_id': 'egress-spec:egress-spec-go-test', 'passed': False, 'exit_code': 2, 'log': str(log)}]\n"
            "(base / 'summary.json').write_text(json.dumps({'units': units, 'diagnostic': []}))\n"
            "sys.exit(2)\n"
        )
        step = python_step(
            "check-egress-spec", code, unit_executor=True, unit_summary_subdir="check-egress-spec", attribution=ATTRIBUTION_EGRESS_SPEC
        )
        exit_code, document, _output, _context = self.run_synthetic([Lane("egress-spec", (step,))])
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["steps"][0]["classification"], CLASS_FUNCTIONAL)

    def test_unreadable_summary_is_functional(self) -> None:
        step = python_step("check-egress-spec", "import sys; sys.exit(2)", unit_executor=True,
                           unit_summary_subdir="check-egress-spec", attribution=ATTRIBUTION_EGRESS_SPEC)
        exit_code, document, _output, _context = self.run_synthetic([Lane("egress-spec", (step,))])
        self.assertEqual(exit_code, 1)
        self.assertIn("不可读", document["steps"][0]["classification_reason"])

    def test_steps_that_need_docker_are_listed_as_not_executed_not_as_failures(self) -> None:
        context = self.prepare()
        stream = io.BytesIO()
        lanes = [Lane("go", (Step("go-test-integration", ("go", "test"), requires_docker=True),))]
        code, document = run_fast(context, lanes=lanes, stream=stream, integration=(False, "本机 Docker 不可用"))
        self.assertEqual(code, 0)
        self.assertEqual(document["result"], RESULT_PASSED)
        self.assertEqual(document["not_executed"], ["go/go-test-integration"])
        self.assertEqual(document["executed"], [])
        self.assertIn("未执行 1 项", stream.getvalue().decode("utf-8"))

    def test_progress_line_tells_pending_ledger_apart_from_functional_failure(self) -> None:
        lanes = [Lane("egress", (failing_output_step("go-test", FROZEN_GO_OUTPUT),)), Lane("t", (failing_output_step("g", FUNCTIONAL_GO_OUTPUT),))]
        _code, _document, output, _context = self.run_synthetic(lanes)
        self.assertIn("待台账闭合 1 项", output)
        self.assertIn("功能失败 1 项", output)


class LaneBuildingTest(FastFixture):
    def test_lane_order_is_cheap_first_and_full_only_lanes_are_gone(self) -> None:
        lanes = build_fast_lanes(self.prepare())
        self.assertEqual(
            [lane.name for lane in lanes],
            ["toolchain", "secret-scan", "lint", "frontend", "targeted-regression", "egress-spec", "worktree"],
        )
        names = {lane.name for lane in lanes}
        self.assertNotIn("capture-tools", names)
        self.assertNotIn("go-tests", names)

    def test_egress_spec_and_lint_steps_match_the_full_mode_definitions(self) -> None:
        lanes = {lane.name: lane for lane in build_fast_lanes(self.prepare())}
        full = {lane.name: lane for lane in gate_runner.build_lanes("full", Path("/nonexistent/codex"))}
        spec = lanes["egress-spec"].steps[0]
        self.assertEqual(spec.argv[:2], ("make", "-k"))
        self.assertEqual(spec.argv[-1], "check-egress-spec")
        self.assertTrue(spec.unit_executor)
        self.assertEqual(spec.unit_summary_subdir, "check-egress-spec")
        self.assertEqual(spec.attribution, ATTRIBUTION_EGRESS_SPEC)
        self.assertEqual([step.argv for step in lanes["lint"].steps], [step.argv for step in full["lint"].steps])
        self.assertEqual([step.argv for step in lanes["frontend"].steps], [step.argv for step in full["frontend"].steps])

    def test_targeted_lane_runs_the_listed_packages_per_tag_set_with_a_checklist_first(self) -> None:
        lanes = {lane.name: lane for lane in build_fast_lanes(self.prepare())}
        steps = lanes["targeted-regression"].steps
        self.assertEqual(steps[0].name, "targeted-checklist")
        self.assertEqual(
            [step.argv for step in steps[1:3]],
            [
                ("go", "test", "-count=1", "./internal/a", "./internal/b"),
                ("go", "test", "-tags=unit", "-count=1", "./internal/a", "./internal/b", "./internal/covered"),
            ],
        )
        self.assertTrue(all(step.cwd == "backend" and step.attribution == ATTRIBUTION_GO_TEST for step in steps[1:3]))
        # integration 标签不在快线内
        self.assertFalse(any("integration" in " ".join(step.argv) for step in steps))

    def test_fallback_manifest_runs_the_three_full_go_test_sets(self) -> None:
        context = self.prepare()
        context.manifest["go"]["fallback_full_go"] = True
        steps = build_fast_lanes(context)[4].steps
        self.assertEqual(
            [step.argv for step in steps[1:]],
            [
                ("go", "test", "-count=1", "./..."),
                ("go", "test", "-tags=unit", "-count=1", "./..."),
                ("go", "test", "-tags=integration", "-count=1", "./..."),
            ],
        )
        self.assertTrue(steps[3].requires_docker)
        self.assertTrue(all(step.go_json_name is None for step in steps[1:]))

    def test_frontend_python_and_shell_entries_become_steps(self) -> None:
        context = self.prepare()
        manifest = context.manifest
        manifest["frontend"]["spec_files"] = ["src/__tests__/x.spec.ts", "src/__tests__/y.spec.ts"]
        manifest["python"] = [
            {"tests": "tools/upstream_merge/tests", "argv": ["python3", "-m", "unittest", "discover"], "cwd": ".", "files": [], "reason": "r"}
        ]
        manifest["shell"]["commands"] = [["/bin/sh", "deploy/tests/a.sh"], ["/bin/bash", "-n", "deploy/b.sh"]]
        steps = {step.name: step for step in build_fast_lanes(context)[4].steps}
        self.assertEqual(
            steps["frontend-vitest-specs"].argv,
            ("pnpm", "--dir", "frontend", "exec", "vitest", "run", "src/__tests__/x.spec.ts", "src/__tests__/y.spec.ts"),
        )
        self.assertEqual(steps["python-tests-1"].argv, ("python3", "-m", "unittest", "discover"))
        self.assertEqual(steps["deploy-script-2"].argv, ("/bin/bash", "-n", "deploy/b.sh"))

    def test_checklist_shows_the_correspondence_between_conflicts_and_targeted_regression(self) -> None:
        text = render_checklist(self.document)
        self.assertIn("冲突文件 1 个", text)
        self.assertIn("冲突文件 1 个（backend/internal/a/a.go）", text)
        self.assertIn("直接导入受影响的包", text)
        self.assertIn("不重复执行 internal/covered", text)
        self.assertIn("integration 不在快线内", text)

    def test_checklist_prompts_for_supplementary_verification_when_unresolved(self) -> None:
        document = json.loads(json.dumps(self.document))
        document["unresolved"] = [{"path": "scripts/x.sh", "reason": "无法归类", "go_related": False, "origin": ""}]
        text = render_checklist(document)
        self.assertIn("须补足验证", text)
        self.assertIn("scripts/x.sh", text)


class InlineStepTest(FastFixture):
    def run_step(self, step: Step) -> tuple[int, str]:
        assert step.inline is not None
        return step.inline()

    def lanes(self) -> dict[str, Lane]:
        return {lane.name: lane for lane in build_fast_lanes(self.prepare())}

    def test_toolchain_step_records_versions_and_fails_on_mismatch(self) -> None:
        context = self.prepare()
        step = build_fast_lanes(context)[0].steps[0]
        passing = {"result": "passed", "actual": {"go": {"version": "1.27.3"}, "golangci_lint": {"version": "2.14.0"}}, "expected": {}}
        with mock.patch.object(fast_lane, "check_toolchain", return_value=passing):
            self.assertEqual(self.run_step(step)[0], 0)
        self.assertEqual(context.records["toolchain"]["go"], {"version": "1.27.3"})
        failing = {**passing, "result": "blocked", "mismatches": [{"kind": "go", "expected": "1.27.3", "actual": "1.26.6", "hint": "升级"}]}
        with mock.patch.object(fast_lane, "check_toolchain", return_value=failing), mock.patch.object(
            fast_lane, "describe_mismatches", return_value=["Go 版本不一致"]
        ):
            code, text = self.run_step(step)
        self.assertEqual(code, 1)
        self.assertIn("Go 版本不一致", text)

    def test_secret_scan_step_uses_the_request_and_blocks_on_hits(self) -> None:
        context = self.prepare()
        step = build_fast_lanes(context)[1].steps[0]
        passed = {"result": "passed", "scan": {"gate_id": "10-secret-scan"}, "blockers": []}
        with mock.patch("tools.upstream_merge.gate_guard.candidate_secret_scan_from_request", return_value=passed) as scan:
            self.assertEqual(self.run_step(step)[0], 0)
        scan.assert_called_once_with(self.request, context.repository_root)
        self.assertEqual(context.records["secret_scan"]["request_sha256"], file_binding(self.request)["sha256"])
        blocked = {"result": "blocked", "scan": {"gate_id": "10-secret-scan"}, "blockers": ["命中 a.txt"]}
        with mock.patch("tools.upstream_merge.gate_guard.candidate_secret_scan_from_request", return_value=blocked):
            code, text = self.run_step(step)
        self.assertEqual(code, 1)
        self.assertIn("命中 a.txt", text)

    def test_inline_exception_becomes_a_failed_step_not_a_crash(self) -> None:
        def explode() -> tuple[int, str]:
            raise RuntimeError("boom")

        context = self.prepare()
        code, document = run_fast(
            context, lanes=[Lane("x", (Step("inline", ("(内联)",), inline=explode),))], stream=io.BytesIO(), integration=(True, None)
        )
        self.assertEqual(code, 1)
        self.assertEqual(document["steps"][0]["classification"], CLASS_FUNCTIONAL)

    def test_worktree_step_fails_when_a_step_leaves_the_tree_dirty(self) -> None:
        context = self.prepare()
        lanes = [
            Lane("pollute", (python_step("write", "open('stray.txt', 'w').write('x')"),)),
            Lane("worktree", (fast_lane._inline_step("clean-after-run", "x", fast_lane._worktree_inline(context)),)),
        ]
        code, document = run_fast(context, lanes=lanes, stream=io.BytesIO(), integration=(True, None))
        self.assertEqual(code, 1)
        self.assertEqual(document["failed_functional"], ["worktree/clean-after-run"])
        self.assertEqual(document["environment"]["worktree_after_run"], ["stray.txt"])


class TimingComparisonTest(unittest.TestCase):
    def test_full_status_gives_elapsed_difference_and_lane_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            full = Path(directory) / "full.json"
            full.write_text(
                json.dumps(
                    {
                        "schema_version": STATUS_SCHEMA,
                        "mode": "full",
                        "elapsed_seconds": 3869.2,
                        "steps": [
                            {"lane": "egress-spec", "step": "check-egress-spec", "status": "failed", "exit_code": 2, "duration_seconds": 911.4, "reason": None},
                            {"lane": "capture-tools", "step": "capture-tools", "status": "failed", "exit_code": 2, "duration_seconds": 2018.3, "reason": None},
                            {"lane": "go-tests", "step": "go-test-default", "status": "failed", "exit_code": 1, "duration_seconds": 431.1, "reason": None},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            fast = [
                StepResult("egress-spec", "check-egress-spec", "failed", 2, 900.0),
                StepResult("targeted-regression", "go-test-default-targeted", "passed", 0, 250.5),
            ]
            comparison = timing_comparison(fast, 1200.0, full)
        self.assertEqual(comparison["full_elapsed_seconds"], 3869.2)
        self.assertEqual(comparison["saved_seconds"], 2669.2)
        self.assertEqual(comparison["skipped_lanes"], ["capture-tools", "go-tests"])
        self.assertEqual(comparison["added_lanes"], ["targeted-regression"])
        self.assertEqual(comparison["targeted_regression_seconds"], 250.5)
        self.assertEqual(comparison["lane_seconds"]["full"]["capture-tools"], 2018.3)
        self.assertTrue(comparison["cache_notes"])

    def test_without_a_full_status_the_fields_exist_but_are_empty(self) -> None:
        comparison = timing_comparison([StepResult("a", "b", "passed", 0, 1.0)], 5.0, None)
        self.assertIsNone(comparison["full_elapsed_seconds"])
        self.assertIsNone(comparison["saved_seconds"])
        self.assertEqual(comparison["fast_elapsed_seconds"], 5.0)

    def test_a_non_full_status_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "other.json"
            other.write_text(json.dumps({"schema_version": FAST_STATUS_SCHEMA, "mode": "fast", "elapsed_seconds": 1, "steps": []}), encoding="utf-8")
            with self.assertRaises(UpstreamMergeError):
                timing_comparison([], 1.0, other)


class FastRecordIsNotAU4ReceiptTest(FastFixture):
    """快线状态文件走 U-4 读侧（_load_runner_status）必须被拒绝；full 模式的文件不受影响。"""

    def plan_with(self, path: Path):
        evidence = path.parent
        plan = SimpleNamespace(evidence_root=evidence)
        return plan, artifact_binding(evidence, path)

    def test_u4_reader_rejects_a_fast_status_file(self) -> None:
        _code, document, _output, context = self.run_synthetic([Lane("a", (python_step("one", "pass"),))])
        plan, binding = self.plan_with(context.status_file)
        with self.assertRaisesRegex(UpstreamMergeError, "试验区快线记录"):
            _load_runner_status(plan, binding, "门禁状态文件", exit_code=0)
        self.assertEqual(document["schema_version"], FAST_STATUS_SCHEMA)

    def test_even_a_forged_full_schema_name_with_fast_mode_is_rejected(self) -> None:
        forged = self.out / "forged.json"
        forged.write_text(
            json.dumps({"schema_version": STATUS_SCHEMA, "mode": "fast", "jobs": 1, "elapsed_seconds": 1, "steps": [],
                        "failed": [], "not_executed": [], "result": "passed"}),
            encoding="utf-8",
        )
        plan, binding = self.plan_with(forged)
        with self.assertRaisesRegex(UpstreamMergeError, "试验区快线记录"):
            _load_runner_status(plan, binding, "门禁状态文件", exit_code=0)

    def test_a_genuine_full_mode_status_is_still_accepted(self) -> None:
        results = [StepResult("lint", "golangci-lint-default", "passed", 0, 1.0)]
        document = gate_runner.status_document(results, mode="full", jobs=1, elapsed=1.0)
        path = self.out / "full-status.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        plan, binding = self.plan_with(path)
        self.assertEqual(_load_runner_status(plan, binding, "门禁状态文件", exit_code=0)["result"], "passed")

    def test_fast_and_full_schemas_are_different_identifiers(self) -> None:
        self.assertNotEqual(FAST_STATUS_SCHEMA, STATUS_SCHEMA)
        self.assertNotIn("runner-status", FAST_STATUS_SCHEMA)


class CommandLineTest(FastFixture):
    def test_fast_mode_without_a_targeted_manifest_refuses_to_start(self) -> None:
        with mock.patch.dict("os.environ", {"UPSTREAM_GATE_TARGETED": ""}), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = gate_runner.main(["fast"])
        self.assertEqual(code, 2)
        self.assertIn("快线拒绝启动", err.getvalue())
        self.assertIn("定向回归清单", err.getvalue())

    def test_make_target_requires_the_targeted_variable(self) -> None:
        completed = subprocess.run(
            ["make", "upstream-gate-fast", "UPSTREAM_GATE_TARGETED="],
            cwd=SOURCE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("UPSTREAM_GATE_TARGETED", completed.stdout + completed.stderr)

    def test_make_target_passes_variables_through_as_arguments(self) -> None:
        completed = subprocess.run(
            [
                "make", "-n", "upstream-gate-fast",
                "UPSTREAM_GATE_TARGETED=/x/t.json", "UPSTREAM_GATE_REQUEST=/x/r.json",
                "UPSTREAM_GATE_STATUS_FILE=/x/s.json", "UPSTREAM_GATE_LOG_DIR=/x/logs", "UPSTREAM_GATE_COMPARE_WITH=/x/f.json",
                "CODEX_0_149_1_SOURCE_ROOT=/x/codex",
            ],
            cwd=SOURCE_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        for expected in ('--targeted "/x/t.json"', '--request "/x/r.json"', '--status-file "/x/s.json"', '--log-dir "/x/logs"',
                         '--compare-with "/x/f.json"', '--codex-source-root "/x/codex"', "gate_runner fast"):
            self.assertIn(expected, completed.stdout)

    def test_a_previously_used_status_file_is_never_overwritten(self) -> None:
        context = self.prepare()
        run_fast(context, lanes=[Lane("a", (python_step("one", "pass"),))], stream=io.BytesIO(), integration=(True, None))
        with self.assertRaisesRegex(UpstreamMergeError, "状态文件已存在"):
            self.prepare(log_dir=self.out / "second", status_file=context.status_file)


if __name__ == "__main__":
    unittest.main()
