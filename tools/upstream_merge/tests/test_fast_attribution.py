"""试验区快线的失败归因：冻结摘要类只认显式白名单，其余（含名字或信息里带“漂移”的功能测试）一律功能失败。

夹具 ``data/v0217_trial_frozen_go_test.txt`` 是 v0.2.17 试验区全量门禁（gate-r2）go test 默认标签组日志的原文截取：
officialegress 包、service 包共 46 个冻结摘要类失败用例及其失败信息，不含改写。
守卫用例（``WhitelistAuditTest``）逐个重新核对白名单里每一项仍然满足登记依据，源码变了、条目陈旧就失败。
"""

from __future__ import annotations

import json
import re
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
from tools.upstream_merge.frozen_test_sources import (
    COMPOUND_HELPER_EVIDENCE,
    FROZEN_COMPOUND_SOURCES,
    FROZEN_SPECIAL_SOURCES,
    FROZEN_TEST_SOURCES,
    registered_markers,
)

SOURCE_ROOT = Path(__file__).resolve().parents[3]
BACKEND = SOURCE_ROOT / "backend"
FIXTURE = Path(__file__).resolve().parent / "data" / "v0217_trial_frozen_go_test.txt"
PACKAGE = "github.com/Wei-Shaw/sub2api/"

# 守卫用例用的审计判据，与 frozen_test_sources 模块说明里的登记依据一致。
AUDIT_WINDOW = 40
READ_RE = re.compile(r"os\.ReadFile\(|readFile\(|readCompatibilityClosureSource\(|RepoPath\(")
DIGEST_RE = re.compile(r"Digest\(|sha256|SHA256|sha256Hex\(")
WANT_RE = re.compile(r"ToSHA256|CurrentSHA256|expected")


def go_failure(package: str, test: str, file: str, message: str, line: int = 100) -> str:
    """一个包的 go test 输出：一个失败用例，失败信息首行带 ``文件:行:`` 前缀，以包结果行收尾。"""

    return (
        f"--- FAIL: {test} (0.00s)\n"
        f"    {file}:{line}: {message}\n"
        "FAIL\n"
        f"FAIL\t{PACKAGE}{package}\t0.500s\n"
    )


def frozen_block(package: str = "internal/officialegress") -> str:
    """白名单里确有登记的一条真实失败：officialegress 的 v0.2.3 台账测试。"""

    return go_failure(
        package,
        "TestUpstreamV023SourceTransitionIsFrozen",
        "upstream_v023_source_transition_test.go",
        "上游 v0.2.3 源码 transition 当前摘要不一致：README_CN.md",
        263,
    )


class RealTrialLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = FIXTURE.read_text(encoding="utf-8")

    def failures(self) -> dict[str, list[str]]:
        segments, _leftover = parse_go_test_output(self.text)
        return {
            segment.package.removeprefix(PACKAGE): [name for name, _message in segment.failures]
            for segment in segments
            if segment.verdict == "FAIL"
        }

    def test_fixture_has_the_real_frozen_failures_of_both_packages(self) -> None:
        failed = self.failures()
        self.assertEqual(set(failed), {"internal/officialegress", "internal/service"})
        self.assertEqual(sum(len(names) for names in failed.values()), 46)

    def test_every_real_v0217_frozen_failure_is_still_attributed_to_the_pending_ledger(self) -> None:
        result = classify_go_test_output(self.text)
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING, result.reason)
        self.assertEqual(len(result.failed_tests), 46)
        self.assertEqual(sorted(set(result.packages)), ["internal/officialegress", "internal/service"])

    def test_the_failures_with_vague_messages_are_covered_by_registered_compound_entries(self) -> None:
        # 失败信息只有“条目非法”“提交绑定不一致”“invalid argument”的 8 个真实失败，靠组合条件特例登记。
        result = classify_go_test_output(self.text)
        for name in (
            "TestClaudeOfficialClientOnlyTransitionIsFrozen",
            "TestClaudeOfflineCIStabilizationTransitionIsFrozen",
            "TestClaudeOfflineReleaseRepairTransitionIsFrozen",
            "TestUpstreamMergeFrameworkV4SuccessorIsFrozen",
            "TestUpstreamMergeFrameworkV4SuccessorServiceRejectsMutation",
            "TestUpstreamV0180SourceTransitionIsFrozen",
        ):
            self.assertIn(name, result.failed_tests)

    def test_without_the_compound_and_special_entries_exactly_those_failures_become_functional(self) -> None:
        # 去掉组合条件特例与 invalid argument 特例，v0.2.17 夹具里恰有这些用例变成功能失败（报告里要列数量与原因）。
        from tools.upstream_merge import fast_attribution, frozen_test_sources

        original = (dict(frozen_test_sources.FROZEN_COMPOUND_SOURCES), dict(frozen_test_sources.FROZEN_SPECIAL_SOURCES))
        try:
            frozen_test_sources.FROZEN_COMPOUND_SOURCES.clear()
            frozen_test_sources.FROZEN_SPECIAL_SOURCES.clear()
            segments, _ = parse_go_test_output(self.text)
            vague = []
            for segment in segments:
                if segment.verdict != "FAIL":
                    continue
                relative = segment.package.removeprefix(PACKAGE)
                for name, message in segment.failures:
                    if fast_attribution._whitelisted_failure(message, relative) is not None:
                        vague.append(name)
        finally:
            frozen_test_sources.FROZEN_COMPOUND_SOURCES.update(original[0])
            frozen_test_sources.FROZEN_SPECIAL_SOURCES.update(original[1])
        self.assertEqual(len(vague), 8)
        self.assertEqual(sum(name.startswith("TestUpstreamMergeFrameworkV4Successor") for name in vague), 4)

    def test_one_extra_functional_failure_turns_the_whole_step_functional(self) -> None:
        mixed = self.text.replace(
            "FAIL\tgithub.com/Wei-Shaw/sub2api/internal/officialegress",
            "--- FAIL: TestSomethingElseBreaks (0.00s)\n    other_test.go:9: expected 1, got 2\n"
            "FAIL\tgithub.com/Wei-Shaw/sub2api/internal/officialegress",
            1,
        )
        result = classify_go_test_output(mixed)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("TestSomethingElseBreaks", result.reason)

    def test_a_failure_in_another_package_makes_the_step_functional(self) -> None:
        result = classify_go_test_output(self.text + frozen_block("internal/handler"))
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("internal/handler", result.reason)


class MisclassificationTest(unittest.TestCase):
    """验收发现的四个误判反例：名字或信息里带“漂移”“Frozen”，实际是功能测试，必须判功能失败。"""

    def assert_functional(self, package: str, test: str, file: str, message: str) -> None:
        result = classify_go_test_output(go_failure(package, test, file, message))
        self.assertEqual(result.classification, CLASS_FUNCTIONAL, result.reason)
        self.assertIn(test, result.reason)

    def test_selector_fail_close_test_with_drift_in_name_and_message(self) -> None:
        self.assert_functional(
            "internal/officialegress",
            "TestClaudeProductionRejectsSharedSelectorCoordinateDrift",
            "claude_release_catalog_test.go",
            "共享 selector 漂移未 fail-close：<nil>",
        )

    def test_codex_identity_condition_drift_test(self) -> None:
        self.assert_functional(
            "internal/officialegress",
            "TestCodexIdentityFactsRejectConditionDrift",
            "executor_invocation_test.go",
            "SessionID 与 session_id_present 条件漂移未被拒绝",
        )

    def test_real_wire_comparator_test_whose_source_file_reads_the_ledger_tree(self) -> None:
        self.assert_functional(
            "internal/service",
            "TestChangeset5CurrentFinalWireComparatorRejectsWireDrift",
            "official_egress_changeset5_final_wire_test.go",
            "跨平台 current final-wire 比较器未拒绝真实 wire 字段漂移",
        )

    def test_final_wire_frozen_test_failing_with_a_bare_low_level_error(self) -> None:
        for message in ("invalid argument", "变更集 6 post final-wire 三件套摘要漂移"):
            with self.subTest(message=message):
                self.assert_functional(
                    "internal/service",
                    "TestChangeset6PostFinalWireIsFrozenAndMatchesChangeset5",
                    "official_egress_changeset6_final_wire_test.go",
                    message,
                )

    def test_frozen_sounding_name_and_digest_wording_are_no_evidence_in_an_unregistered_file(self) -> None:
        self.assert_functional(
            "internal/service", "TestSomethingIsFrozen", "billing_service_test.go", "某某 transition 当前摘要不一致：a.go"
        )

    def test_functional_tests_named_retirement_or_transition_are_functional(self) -> None:
        self.assert_functional(
            "internal/service", "TestGPT55RetirementCatalogs", "openai_model_retirement_test.go", "expected catalog to hide the model"
        )
        self.assert_functional(
            "internal/service", "TestCanTransitionBatchImageJob", "batch_image_test.go", "unexpected transition"
        )


class FrozenRuleTest(unittest.TestCase):
    def test_registered_file_and_marker_is_pending(self) -> None:
        result = classify_go_test_output(frozen_block())
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING)
        self.assertEqual(result.failed_tests, ["TestUpstreamV023SourceTransitionIsFrozen"])

    def test_test_name_is_irrelevant_only_the_registered_source_and_marker_count(self) -> None:
        text = go_failure(
            "internal/officialegress", "TestAnythingAtAll", "upstream_v023_source_transition_test.go",
            "上游 v0.2.3 源码 transition 当前摘要不一致：x",
        )
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FROZEN_PENDING)

    def test_registered_file_with_another_message_is_functional(self) -> None:
        # 同一个冻结测试文件里的变异测试：校验器错误地接受变异，信息不是摘要比对。
        text = go_failure(
            "internal/officialegress", "TestUpstreamV023SourceTransitionRejectsMutation",
            "upstream_v023_source_transition_test.go", "变异后的上游 v0.2.3 源码 transition 被错误接受",
        )
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_marker_from_another_file_or_another_package_does_not_transfer(self) -> None:
        message = "上游 v0.2.3 源码 transition 当前摘要不一致：x"
        # 同文件名但包不同的另一个登记文件有自己的标记集合；这里换成未登记的文件名
        text = go_failure("internal/officialegress", "TestX", "some_other_test.go", message)
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)
        text = go_failure("internal/handler", "TestX", "upstream_v023_source_transition_test.go", message)
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_every_message_entry_must_be_registered_not_just_the_first(self) -> None:
        text = (
            "--- FAIL: TestUpstreamV023SourceTransitionIsFrozen (0.00s)\n"
            "    upstream_v023_source_transition_test.go:263: 上游 v0.2.3 源码 transition 当前摘要不一致：README_CN.md\n"
            "    helper_test.go:12: expected 3 got 2\n"
            "FAIL\n"
            f"FAIL\t{PACKAGE}internal/officialegress\t0.5s\n"
        )
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_message_without_a_source_file_prefix_is_functional(self) -> None:
        text = (
            "--- FAIL: TestUpstreamV023SourceTransitionIsFrozen (0.00s)\n"
            "    上游 v0.2.3 源码 transition 当前摘要不一致：README_CN.md\n"
            "FAIL\n"
            f"FAIL\t{PACKAGE}internal/officialegress\t0.5s\n"
        )
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_continuation_lines_join_the_entry_they_follow(self) -> None:
        text = (
            "--- FAIL: TestUpstreamV023SourceTransitionIsFrozen (0.00s)\n"
            "    upstream_v023_source_transition_test.go:263: 上游 v0.2.3 源码 transition 当前摘要不一致：\n"
            "        README_CN.md\n"
            "FAIL\n"
            f"FAIL\t{PACKAGE}internal/officialegress\t0.5s\n"
        )
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
                self.assertEqual(classify_go_test_output(extra + frozen_block()).classification, CLASS_FUNCTIONAL)

    def test_build_and_setup_failures_are_functional(self) -> None:
        for suffix in ("[build failed]", "[setup failed]"):
            with self.subTest(suffix=suffix):
                text = f"FAIL\t{PACKAGE}internal/officialegress {suffix}\n"
                self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_failed_package_without_failed_test_lines_is_functional(self) -> None:
        text = f"some output\nFAIL\nFAIL\t{PACKAGE}internal/service\t1.0s\n"
        self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_empty_unparsable_or_passing_output_is_functional(self) -> None:
        for text in ("", "   \n", "not go test output\n", f"ok  \t{PACKAGE}internal/service\t1.0s\n"):
            with self.subTest(text=text[:20]):
                self.assertEqual(classify_go_test_output(text).classification, CLASS_FUNCTIONAL)

    def test_subtest_failures_do_not_count_as_top_level_names(self) -> None:
        text = (
            "--- FAIL: TestUpstreamV023SourceTransitionIsFrozen (0.00s)\n"
            "    upstream_v023_source_transition_test.go:263: 上游 v0.2.3 源码 transition 当前摘要不一致：x\n"
            "    --- FAIL: TestUpstreamV023SourceTransitionIsFrozen/sub (0.00s)\n"
            "FAIL\n"
            f"FAIL\t{PACKAGE}internal/officialegress\t0.5s\n"
        )
        result = classify_go_test_output(text)
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING)
        self.assertEqual(result.failed_tests, ["TestUpstreamV023SourceTransitionIsFrozen"])


class WhitelistAuditTest(unittest.TestCase):
    """白名单的登记依据逐项重新核对：陈旧或不再满足依据的条目让这里失败。"""

    def source_lines(self, source: str) -> list[str]:
        path = BACKEND / source
        self.assertTrue(path.is_file(), f"白名单里的测试源文件不存在：{source}")
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    def literal_sites(self, lines: list[str], marker: str) -> list[int]:
        """标记出现在字符串字面量里的行号（0 起）。"""

        sites = [
            index
            for index, line in enumerate(lines)
            if marker in line and re.search(r'"[^"]*' + re.escape(marker), line) and "name:" not in line and "mutate" not in line
        ]
        return sites

    def test_registry_shape(self) -> None:
        for source in [*FROZEN_TEST_SOURCES, *FROZEN_COMPOUND_SOURCES, *FROZEN_SPECIAL_SOURCES]:
            self.assertRegex(source, r"^internal/(officialegress|service)/[a-z0-9_]+_test\.go$", source)
        for markers in FROZEN_TEST_SOURCES.values():
            self.assertTrue(markers and all(marker.strip() for marker in markers))
        self.assertGreaterEqual(len(FROZEN_TEST_SOURCES), 80)

    def test_every_direct_entry_still_is_a_read_digest_compare_site(self) -> None:
        checked = 0
        for source, markers in FROZEN_TEST_SOURCES.items():
            lines = self.source_lines(source)
            for marker in markers:
                sites = self.literal_sites(lines, marker)
                self.assertTrue(sites, f"{source} 里找不到标记：{marker}")
                for site in sites:
                    context = "\n".join(lines[max(0, site - AUDIT_WINDOW) : site + 1])
                    after = "\n".join(lines[site : site + 5])
                    with self.subTest(source=source, marker=marker, line=site + 1):
                        self.assertRegex(context, READ_RE, "失败点前文没有读仓库文件")
                        self.assertRegex(context, DIGEST_RE, "失败点前文没有算摘要")
                        self.assertIn("!=", context, "失败点前文没有 != 比较")
                        if "path=%s got=%s want=%s" in "\n".join(lines[max(0, site - 1) : site + 2]) or "got=%s want=%s" in after:
                            self.assertRegex(after, WANT_RE, "got/want 信息里的 want 不是台账登记摘要")
                    checked += 1
        self.assertGreaterEqual(checked, 150)

    def test_compound_entries_call_a_digest_matching_helper_right_before_the_message(self) -> None:
        for source, entries in FROZEN_COMPOUND_SOURCES.items():
            package_dir = BACKEND / Path(source).parent
            package_text = {path: path.read_text(encoding="utf-8", errors="replace") for path in package_dir.glob("*_test.go")}
            lines = self.source_lines(source)
            for marker, helper in entries:
                with self.subTest(source=source, marker=marker):
                    sites = self.literal_sites(lines, marker)
                    self.assertTrue(sites, f"{source} 里找不到标记：{marker}")
                    for site in sites:
                        nearby = "\n".join(lines[max(0, site - 14) : site + 1])
                        self.assertIn(f"{helper}(", nearby, "失败点前文没有调用做摘要比对的函数")
                    defining = [text for text in package_text.values() if f"func {helper}(" in text]
                    self.assertEqual(len(defining), 1, f"{helper} 的定义应恰有一处")
                    for fragment in COMPOUND_HELPER_EVIDENCE[helper]:
                        self.assertIn(fragment, defining[0], f"{helper} 所在源文件缺少 {fragment}")
        self.assertEqual(
            set(COMPOUND_HELPER_EVIDENCE), {helper for entries in FROZEN_COMPOUND_SOURCES.values() for _marker, helper in entries}
        )

    def test_invalid_argument_special_has_exactly_the_two_audited_sources(self) -> None:
        source = "internal/service/upstream_v0180_source_transition_test.go"
        self.assertEqual(FROZEN_SPECIAL_SOURCES[source], ("invalid argument",))
        lines = self.source_lines(source)
        sites = [index for index, line in enumerate(lines) if "os.ErrInvalid" in line and "return" in line]
        self.assertEqual(len(sites), 2, "os.ErrInvalid 的来源数量变了，需要重新核对")
        # 第二处必须落在对回执里每个路径当前摘要的核对里
        tail = "\n".join(lines[sites[1] - 8 : sites[1] + 1])
        for fragment in ("os.ReadFile(", "Digest(", "auditedSourceCurrentStateAcceptedService("):
            self.assertIn(fragment, tail)
        # 第一处之前先做了回执自摘要校验（内容被改会先报自摘要不一致）
        head = "\n".join(lines[: sites[0]])
        self.assertIn("validateUpstreamSourceTransitionIdentity(raw, receipt.IdentitySHA256", head)

    def test_registered_markers_combine_all_three_registries(self) -> None:
        source = "internal/officialegress/claude_official_client_only_transition_test.go"
        self.assertIn("Claude official-client-only transition 条目非法", registered_markers(source))
        self.assertEqual(registered_markers("internal/service/billing_service_test.go"), ())
        self.assertEqual(registered_markers("internal/service/upstream_v0180_source_transition_test.go")[-1], "invalid argument")

    def test_known_functional_tests_are_not_registered(self) -> None:
        # 四个误判反例所在的文件，以及名字带 Retirement／Transition 的功能测试所在的文件，一律不在白名单里
        for source in (
            "internal/officialegress/claude_release_catalog_test.go",
            "internal/officialegress/executor_invocation_test.go",
            "internal/service/official_egress_changeset5_final_wire_test.go",
            "internal/service/official_egress_changeset6_final_wire_test.go",
            "internal/service/openai_model_retirement_test.go",
            "internal/service/batch_image_test.go",
        ):
            self.assertEqual(registered_markers(source), (), source)


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
        result = classify_egress_spec_summary(summary)
        self.assertEqual(result.classification, CLASS_FROZEN_PENDING, result.reason)

    def test_go_test_unit_failing_with_a_misclassification_prone_test_is_functional(self) -> None:
        log = go_failure(
            "internal/officialegress",
            "TestClaudeProductionRejectsSharedSelectorCoordinateDrift",
            "claude_release_catalog_test.go",
            "共享 selector 漂移未 fail-close：<nil>",
        )
        summary = self.write_summary([self.unit("egress-spec-go-test", passed=False, log=log)])
        self.assertEqual(classify_egress_spec_summary(summary).classification, CLASS_FUNCTIONAL)

    def test_another_failed_unit_makes_it_functional(self) -> None:
        summary = self.write_summary(
            [
                self.unit("egress-spec-go-test", passed=False, log=self.frozen_log()),
                self.unit("egress-spec-gofmt", passed=False, log="gofmt: bad file\n"),
            ]
        )
        result = classify_egress_spec_summary(summary)
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
        result = classify_egress_spec_summary(summary)
        self.assertEqual(result.classification, CLASS_FUNCTIONAL)
        self.assertIn("未执行", result.reason)

    def test_signal_or_timeout_on_the_go_test_unit_is_functional(self) -> None:
        for extra in ({"signal": 9}, {"timed_out": True}):
            with self.subTest(extra=extra):
                summary = self.write_summary(
                    [self.unit("egress-spec-go-test", passed=False, log=self.frozen_log(), **extra)]
                )
                self.assertEqual(classify_egress_spec_summary(summary).classification, CLASS_FUNCTIONAL)

    def test_real_v0217_summary_shape_with_diagnostic_rerun_is_pending(self) -> None:
        # 试验区真实形状：49 个单元 48 个通过，唯一失败的是 egress-spec-go-test，另有一条诊断重跑记录。
        units = [self.unit(f"egress-spec-check-{index}", passed=True) for index in range(48)]
        units.append(self.unit("egress-spec-go-test", passed=False, log=self.frozen_log()))
        summary = self.write_summary(
            units, status="failed", failed_units=["egress-spec:egress-spec-go-test"], units_not_run=[],
            diagnostic=[{"unit_id": "egress-spec:egress-spec-go-test", "kind": "diagnostic", "passed": False}],
        )
        self.assertEqual(classify_egress_spec_summary(summary).classification, CLASS_FROZEN_PENDING)


if __name__ == "__main__":
    unittest.main()
