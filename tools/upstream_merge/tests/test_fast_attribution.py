"""试验区快线的失败归因：真实 v0.2.17 试验区日志做夹具，冻结摘要类记待台账闭合，混入功能失败或归因不清一律记功能失败。

夹具 ``data/v0217_trial_frozen_go_test.txt`` 是 v0.2.17 试验区全量门禁（gate-r2）go test 默认标签组日志的原文截取：
officialegress 包 29 个、service 包 17 个冻结摘要类失败用例及其失败信息，不含改写。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.fast_attribution import (
    CLASS_FROZEN_PENDING,
    CLASS_FUNCTIONAL,
    classify_egress_spec_summary,
    classify_go_test_output,
    parse_go_test_output,
)

SOURCE_ROOT = Path(__file__).resolve().parents[3]
BACKEND = SOURCE_ROOT / "backend"
FIXTURE = Path(__file__).resolve().parent / "data" / "v0217_trial_frozen_go_test.txt"
PACKAGE = "github.com/Wei-Shaw/sub2api/"


def frozen_block(package: str, test: str = "TestFooSourceTransitionIsFrozen", message: str = "foo.go 当前摘要不一致：a.go") -> str:
    """一个包的 go test 输出：一个冻结摘要类失败用例，以包结果行收尾。"""

    return (
        f"--- FAIL: {test} (0.00s)\n"
        f"    foo_transition_test.go:12: {message}\n"
        "FAIL\n"
        f"FAIL\t{PACKAGE}{package}\t0.500s\n"
    )


class RealTrialLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = FIXTURE.read_text(encoding="utf-8")

    def test_fixture_has_the_real_frozen_failures_of_both_packages(self) -> None:
        segments, _leftover = parse_go_test_output(self.text)
        failed = {segment.package.removeprefix(PACKAGE): len(segment.failures) for segment in segments if segment.verdict == "FAIL"}
        # 夹具截自 v0.2.17 试验区日志：两个包里都是冻结摘要类失败，包括失败信息只有 invalid argument 的那一条。
        self.assertEqual(set(failed), {"internal/officialegress", "internal/service"})
        self.assertGreaterEqual(failed["internal/officialegress"], 20)
        self.assertGreaterEqual(failed["internal/service"], 10)

    def test_real_frozen_failures_are_pending_ledger_with_source_evidence(self) -> None:
        result = classify_go_test_output(self.text, source_root=BACKEND)
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING, result.reason)
        self.assertIn("TestUpstreamV0180SourceTransitionIsFrozen", result.failed_tests)
        self.assertEqual(sorted(set(result.packages)), ["internal/officialegress", "internal/service"])

    def test_without_source_evidence_the_invalid_argument_failure_is_functional(self) -> None:
        # 失败信息只有 invalid argument 的那条，没有源文件证据就不能证明是台账比对：保守按功能失败。
        result = classify_go_test_output(self.text)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("TestUpstreamV0180SourceTransitionIsFrozen", result.reason)

    def test_one_extra_functional_failure_turns_the_whole_step_functional(self) -> None:
        mixed = self.text.replace(
            "FAIL\tgithub.com/Wei-Shaw/sub2api/internal/officialegress",
            "--- FAIL: TestSomethingElseBreaks (0.00s)\n    other_test.go:9: expected 1, got 2\n"
            "FAIL\tgithub.com/Wei-Shaw/sub2api/internal/officialegress",
            1,
        )
        result = classify_go_test_output(mixed, source_root=BACKEND)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("TestSomethingElseBreaks", result.reason)

    def test_a_failure_in_another_package_makes_the_step_functional(self) -> None:
        mixed = self.text + frozen_block("internal/handler")
        result = classify_go_test_output(mixed, source_root=BACKEND)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("internal/handler", result.reason)


class FrozenRuleTest(unittest.TestCase):
    def test_frozen_looking_name_and_digest_message_in_frozen_package_is_pending(self) -> None:
        result = classify_go_test_output(frozen_block("internal/officialegress"))
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING)
        self.assertEqual(result.failed_tests, ["TestFooSourceTransitionIsFrozen"])

    def test_functional_test_with_retirement_in_its_name_is_not_pending(self) -> None:
        # service 包里真实存在名字带 Retirement 的功能测试；它的测试文件不读冻结台账，失败信息也不是摘要比对。
        text = frozen_block(
            "internal/service",
            test="TestGPT55RetirementCatalogs",
            message="expected catalog to hide the retired model",
        ).replace("foo_transition_test.go", "openai_model_retirement_test.go")
        result = classify_go_test_output(text, source_root=BACKEND)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("TestGPT55RetirementCatalogs", result.reason)

    def test_test_name_without_frozen_wording_is_functional_even_in_frozen_package(self) -> None:
        text = frozen_block("internal/service", test="TestBillingRoundsUp", message="摘要不一致")
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_failure_message_without_digest_wording_or_source_evidence_is_functional(self) -> None:
        text = frozen_block("internal/officialegress", message="open x.json: no such file")
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_digest_wording_in_message_is_enough_without_source_root(self) -> None:
        text = frozen_block("internal/service", message="某某 transition 漂移：path=a got=1 want=2")
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FROZEN_PENDING)

    def test_crash_signs_make_the_step_functional(self) -> None:
        for extra in (
            "panic: runtime error: index out of range [3] with length 3\n",
            "fatal error: all goroutines are asleep - deadlock!\n",
            "signal: killed\n",
            "test timed out after 10m0s\n",
            "# github.com/Wei-Shaw/sub2api/internal/service\ninternal/service/x.go:12:3: undefined: foo\n",
        ):
            with self.subTest(extra=extra.splitlines()[0]):
                text = extra + frozen_block("internal/officialegress")
                self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_build_and_setup_failures_are_functional(self) -> None:
        for suffix in ("[build failed]", "[setup failed]"):
            with self.subTest(suffix=suffix):
                text = f"FAIL\t{PACKAGE}internal/officialegress {suffix}\n"
                self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_failed_package_without_failed_test_lines_is_functional(self) -> None:
        text = f"some output\nFAIL\n FAIL\t{PACKAGE}internal/service\t1.0s\n".replace(" FAIL", "FAIL")
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_empty_unparsable_or_passing_output_is_functional(self) -> None:
        for text in ("", "   \n", "not go test output\n", f"ok  \t{PACKAGE}internal/service\t1.0s\n"):
            with self.subTest(text=text[:20]):
                self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_subtest_failures_do_not_count_as_top_level_names(self) -> None:
        text = (
            "--- FAIL: TestFooSourceTransitionIsFrozen (0.00s)\n"
            "    foo_transition_test.go:12: 当前摘要不一致：a.go\n"
            "    --- FAIL: TestFooSourceTransitionIsFrozen/sub (0.00s)\n"
            "FAIL\n"
            f"FAIL\t{PACKAGE}internal/officialegress\t0.5s\n"
        )
        result = classify_go_test_output(text)
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING)
        self.assertEqual(result.failed_tests, ["TestFooSourceTransitionIsFrozen"])


class EgressSpecSummaryTest(unittest.TestCase):
    """出站规格线：只认 egress-spec-go-test 单元，且日志里的失败全是冻结摘要类。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def unit(self, name: str, *, passed: bool, log: str | None = None, **extra) -> dict:
        path = self.root / f"{name.replace(':', '_')}.log"
        if log is not None:
            path.write_text(log, encoding="utf-8")
        return {"unit_id": f"egress-spec:{name}", "passed": passed, "exit_code": 0 if passed else 2, "log": str(path), **extra}

    def write_summary(self, units: list[dict], **extra) -> Path:
        path = self.root / "summary.json"
        path.write_text(json.dumps({"units": units, "diagnostic": [], **extra}), encoding="utf-8")
        return path

    def frozen_log(self) -> str:
        return FIXTURE.read_text(encoding="utf-8")

    def test_only_go_test_unit_failed_with_frozen_log_is_pending(self) -> None:
        summary = self.write_summary(
            [self.unit("egress-spec-go-vet", passed=True), self.unit("egress-spec-go-test", passed=False, log=self.frozen_log())]
        )
        result = classify_egress_spec_summary(summary, source_root=BACKEND)
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING, result.reason)

    def test_another_failed_unit_makes_it_functional(self) -> None:
        summary = self.write_summary(
            [
                self.unit("egress-spec-go-test", passed=False, log=self.frozen_log()),
                self.unit("egress-spec-gofmt", passed=False, log="gofmt: bad file\n"),
            ]
        )
        result = classify_egress_spec_summary(summary, source_root=BACKEND)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("egress-spec-gofmt", result.reason)

    def test_python_transition_unit_is_not_whitelisted(self) -> None:
        # 摘要比对类的 Python 子检查没有真实日志证据，宁可按功能失败阻断，不放进白名单。
        summary = self.write_summary([self.unit("egress-spec-maintenance-transition", passed=False, log="FAIL: x\n")])
        self.assertEqual(classify_egress_spec_summary(summary).classification, CLASS_FUNCTIONAL)

    def test_unreadable_missing_or_malformed_inputs_are_functional(self) -> None:
        self.assertEqual(classify_egress_spec_summary(self.root / "absent.json").classification, CLASS_FUNCTIONAL)
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(classify_egress_spec_summary(broken).classification, CLASS_FUNCTIONAL)
        shapeless = self.root / "shapeless.json"
        shapeless.write_text(json.dumps({"units": [{"unit_id": 1}]}), encoding="utf-8")
        self.assertEqual(classify_egress_spec_summary(shapeless).classification, CLASS_FUNCTIONAL)

    def test_unreadable_unit_log_is_functional(self) -> None:
        summary = self.write_summary([self.unit("egress-spec-go-test", passed=False)])  # 日志文件根本没写
        result = classify_egress_spec_summary(summary)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("日志不可读", result.reason)

    def test_no_failed_unit_in_summary_is_functional(self) -> None:
        summary = self.write_summary([self.unit("egress-spec-go-test", passed=True)])
        self.assertEqual(classify_egress_spec_summary(summary).classification, CLASS_FUNCTIONAL)

    def test_units_that_never_ran_make_it_functional(self) -> None:
        summary = self.write_summary(
            [self.unit("egress-spec-go-test", passed=False, log=self.frozen_log())], units_not_run=["egress-spec-go-build"]
        )
        result = classify_egress_spec_summary(summary, source_root=BACKEND)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("未执行", result.reason)

    def test_signal_or_timeout_on_the_go_test_unit_is_functional(self) -> None:
        for extra in ({"signal": 9}, {"timed_out": True}):
            with self.subTest(extra=extra):
                summary = self.write_summary(
                    [self.unit("egress-spec-go-test", passed=False, log=self.frozen_log(), **extra)]
                )
                self.assertEqual(
                    classify_egress_spec_summary(summary, source_root=BACKEND).classification, CLASS_FUNCTIONAL
                )

    def test_real_v0217_summary_shape_with_diagnostic_rerun_is_pending(self) -> None:
        # 试验区真实形状：49 个单元 48 个通过，唯一失败的是 egress-spec-go-test，另有一条诊断重跑记录。
        units = [self.unit(f"egress-spec-check-{index}", passed=True) for index in range(48)]
        units.append(self.unit("egress-spec-go-test", passed=False, log=self.frozen_log()))
        summary = self.write_summary(
            units, status="failed", failed_units=["egress-spec:egress-spec-go-test"], units_not_run=[],
            diagnostic=[{"unit_id": "egress-spec:egress-spec-go-test", "kind": "diagnostic", "passed": False}],
        )
        self.assertEqual(classify_egress_spec_summary(summary, source_root=BACKEND).classification, CLASS_FROZEN_PENDING)


if __name__ == "__main__":
    unittest.main()
