"""U-4 前定向冻结检查：台账闭合才放行首次 U-4，不通过即阻断、不建 attempt，阻断信息写清分类处理规则。

冻结台账用合成仓库（一份 successor 收据登记一个受冻结文件）；Go 冻结测试用注入的假执行器，不依赖本机 Go。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.upstream_merge import gate_guard
from tools.upstream_merge.canonical import bind_identity, sha256_bytes
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.gate_guard import (
    FROZEN_PRECHECK_PACKAGES,
    FROZEN_PRECHECK_RUN,
    run_frozen_precheck,
    run_gates_with_prechecks,
    uncovered_frozen_paths,
)
from tools.upstream_merge.tests.test_gate_guard import GuardFixture
from tools.upstream_merge.workflow import run_verification_gates
from tools.upstream_merge.tests.test_targeted_regression import sh

FROZEN_FAIL_OUTPUT = (
    "--- FAIL: TestFooSourceTransitionIsFrozen (0.00s)\n"
    "    foo_transition_test.go:12: foo transition 当前摘要不一致：backend/a.go\n"
    "FAIL\n"
    "FAIL\tgithub.com/Wei-Shaw/sub2api/internal/officialegress\t0.5s\n"
)
FUNCTIONAL_FAIL_OUTPUT = (
    "--- FAIL: TestBillingRoundsUp (0.00s)\n"
    "    billing_test.go:9: expected 3 got 2\n"
    "FAIL\n"
    "FAIL\tgithub.com/Wei-Shaw/sub2api/internal/service\t0.5s\n"
)


def digest(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


class FrozenLedgerRepository:
    """一个受冻结的源文件 + 一份登记其摘要的 successor 收据。"""

    def __init__(self, temp: Path) -> None:
        self.root = temp / "repo"
        self.root.mkdir()
        sh(self.root, "git", "init", "-b", "main")
        (self.root / "backend/internal/officialegress").mkdir(parents=True)
        (self.root / "backend/internal/officialegress/.keep").write_text("", encoding="utf-8")
        self.maintenance = self.root / "docs/egress/maintenance"
        self.maintenance.mkdir(parents=True)
        self.source = self.root / "backend/a.go"
        self.source.write_text("package a\n", encoding="utf-8")
        self.second = self.root / "backend/b.go"
        self.second.write_text("package b\n", encoding="utf-8")
        self.register("package a\n", "base-successor.json", previous="0" * 64)
        self.register("package b\n", "base-b-successor.json", previous="0" * 64, path="backend/b.go")
        self.base = self.commit("base")

    def register(self, content: str, name: str, *, previous: str, path: str = "backend/a.go") -> None:
        # 带自摘要的真实承接收据形态：U-4 前检查只认自摘要可信、transitions 非空、后继摘要等于当前内容的收据。
        document = bind_identity(
            {
                "schema_version": "official-egress-example-successor/v1",
                "transitions": [
                    {"path": path, "predecessor_sha256s": [previous], "to_sha256": digest(content), "reason": "测试登记"}
                ],
            }
        )
        (self.maintenance / name).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def commit(self, message: str) -> str:
        sh(self.root, "git", "add", "--all")
        sh(self.root, "git", "commit", "-q", "-m", message, "--allow-empty")
        return sh(self.root, "git", "rev-parse", "HEAD")

    def drift(self) -> str:
        """改了受冻结文件但没有任何收据承接它的新摘要。"""

        self.source.write_text("package a // 合并后又改\n", encoding="utf-8")
        return self.commit("candidate drift")

    def close_ledger(self) -> str:
        self.register("package a // 合并后又改\n", "later-successor.json", previous=digest("package a\n"))
        return self.commit("ledger revision")


class FakeGoTests:
    def __init__(self, exit_code: int | None = 0, output: str = "ok\n") -> None:
        self.exit_code, self.output, self.calls = exit_code, output, 0

    def __call__(self, worktree: Path, environ) -> dict:
        self.calls += 1
        return {"argv": ["go", "test", *FROZEN_PRECHECK_PACKAGES, "-run", FROZEN_PRECHECK_RUN], "exit_code": self.exit_code,
                "duration_seconds": 1.5, "output": self.output}


class UncoveredPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = FrozenLedgerRepository(Path(temporary.name))

    def test_registered_current_digest_is_covered(self) -> None:
        self.assertEqual(uncovered_frozen_paths(self.repo.root), [])

    def test_changed_frozen_file_without_a_receipt_is_uncovered_until_the_ledger_revision_lands(self) -> None:
        self.repo.drift()
        self.assertEqual(uncovered_frozen_paths(self.repo.root), ["backend/a.go"])
        self.repo.close_ledger()
        self.assertEqual(uncovered_frozen_paths(self.repo.root), [])

    def test_deleted_frozen_file_is_left_to_the_retirement_checks(self) -> None:
        self.repo.source.unlink()
        self.assertEqual(uncovered_frozen_paths(self.repo.root), [])


class RunFrozenPrecheckTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.repo = FrozenLedgerRepository(self.temp)

    def plan(self) -> SimpleNamespace:
        return SimpleNamespace(fork_head=self.repo.base)

    def test_trees_without_frozen_ledgers_are_not_applicable(self) -> None:
        bare = self.temp / "bare"
        bare.mkdir()
        sh(bare, "git", "init", "-b", "main")
        (bare / "x.txt").write_text("x", encoding="utf-8")
        sh(bare, "git", "add", "--all")
        sh(bare, "git", "commit", "-q", "-m", "x")
        fake = FakeGoTests()
        report = run_frozen_precheck(bare, None, go_tests=fake)
        self.assertEqual(report["status"], "not_applicable")
        self.assertEqual(report["blockers"], [])
        self.assertEqual(fake.calls, 0)

    def test_closed_ledger_passes_after_running_the_frozen_go_tests(self) -> None:
        fake = FakeGoTests(0)
        report = run_frozen_precheck(self.repo.root, self.plan(), go_tests=fake)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["blockers"], [])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(report["go_test"]["argv"][-2:], ["-run", FROZEN_PRECHECK_RUN])
        self.assertEqual(list(FROZEN_PRECHECK_PACKAGES), ["./internal/officialegress", "./internal/service"])

    def test_unconsumed_frozen_digest_blocks_without_running_go_and_names_the_rules(self) -> None:
        self.repo.drift()
        fake = FakeGoTests(0)
        report = run_frozen_precheck(self.repo.root, self.plan(), go_tests=fake)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["classification"], "frozen_digest")
        self.assertEqual(report["uncovered_paths"], ["backend/a.go"])
        self.assertFalse(report["ledger_revision_present"])
        self.assertEqual(fake.calls, 0)
        message = report["blockers"][0]
        for expected in ("backend/a.go", "冻结摘要类", "尚未生成台账 revision", "freeze-successor-generate", "功能失败", "源码 revision", "停线复盘", "不要默认循环补台账"):
            self.assertIn(expected, message)

    def test_unclosed_ledger_after_a_ledger_revision_says_to_stop_and_review(self) -> None:
        # 两个冻结文件都被改了，台账 revision 只承接了 b.go：a.go 仍不闭合，而台账 revision 已经生成过。
        self.repo.drift()
        self.repo.second.write_text("package b // 合并后又改\n", encoding="utf-8")
        self.repo.register("package b // 合并后又改\n", "later-b-successor.json", previous=digest("package b\n"), path="backend/b.go")
        self.repo.commit("台账 revision 只承接了 b.go")
        report = run_frozen_precheck(self.repo.root, self.plan(), go_tests=FakeGoTests())
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["uncovered_paths"], ["backend/a.go"])
        self.assertTrue(report["ledger_revision_present"])
        self.assertIn("台账 revision 已生成过，仍不闭合应停线复盘", report["blockers"][0])

    def test_a_receipt_that_does_not_describe_the_current_content_is_not_a_ledger_revision(self) -> None:
        # 登记的后继摘要与当前候选内容不一致（被后来的改动取代）、或没有自摘要的文件，都不算台账 revision。
        self.repo.drift()
        self.repo.register("package a // 另一个摘要\n", "stale-successor.json", previous=digest("package a\n"))
        (self.repo.maintenance / "no-identity-successor.json").write_text(
            json.dumps({"schema_version": "x-successor/v1", "transitions": [{"path": "backend/a.go"}]}), encoding="utf-8"
        )
        self.repo.commit("看起来像台账 revision 但不是")
        report = run_frozen_precheck(self.repo.root, self.plan(), go_tests=FakeGoTests())
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["ledger_revision_present"])

    def test_go_frozen_test_failure_with_digest_mismatches_is_classified_frozen(self) -> None:
        fake = FakeGoTests(1, FROZEN_FAIL_OUTPUT)
        report = run_frozen_precheck(self.repo.root, self.plan(), go_tests=fake)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["classification"], "frozen_digest")
        self.assertIn("TestFooSourceTransitionIsFrozen", report["blockers"][0])
        self.assertIn("冻结摘要类", report["blockers"][0])
        self.assertIn("当前摘要不一致", report["go_test"]["output_tail"])

    def test_go_failure_that_is_not_provably_a_digest_mismatch_is_functional(self) -> None:
        report = run_frozen_precheck(self.repo.root, self.plan(), go_tests=FakeGoTests(1, FUNCTIONAL_FAIL_OUTPUT))
        self.assertEqual(report["classification"], "functional")
        self.assertIn("功能失败", report["blockers"][0])
        self.assertIn("源码 revision", report["blockers"][0])

    def test_timeout_or_unstartable_go_is_blocked_as_functional(self) -> None:
        report = run_frozen_precheck(self.repo.root, self.plan(), go_tests=FakeGoTests(None, "无法启动 go test"))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["classification"], "functional")


class PrecheckIntegrationTest(GuardFixture):
    """frozen_check 接进 run_gates_with_prechecks：与另外三项一起列出、不建 attempt；只在将执行 full-regression 组时才跑。"""

    def blocked(self) -> dict:
        return {"status": "blocked", "blockers": ["U-4 前定向冻结检查未通过：示例（冻结摘要类）"], "duration_seconds": 1.0}

    def passed(self) -> dict:
        return {"status": "passed", "blockers": [], "duration_seconds": 1.0}

    def test_blocked_frozen_check_stops_before_any_attempt_exists(self) -> None:
        self.make_fixture()
        calls = []

        def check(worktree, plan, *, environ=None):
            calls.append(plan)
            return self.blocked()

        with self.assertRaises(UpstreamMergeError) as caught:
            self.prechecked_run("attempt-001", frozen_check=check)
        self.assertIn("没有创建 attempt attempt-001", str(caught.exception))
        self.assertIn("U-4 前定向冻结检查未通过", str(caught.exception))
        self.assertFalse(self.attempt_dir("attempt-001").exists())
        self.assertEqual(len(calls), 1)

    def test_frozen_blocker_is_listed_together_with_the_other_blockers(self) -> None:
        self.make_fixture({"src/leak.txt": "token=" + "AKIA" + "Q" * 16 + "\n"})
        with self.assertRaises(UpstreamMergeError) as caught:
            self.prechecked_run("attempt-001", frozen_check=lambda worktree, plan, *, environ=None: self.blocked())
        text = str(caught.exception)
        self.assertIn("共 2 项", text)
        self.assertIn("候选密钥扫描命中", text)
        self.assertIn("定向冻结检查未通过", text)

    def test_passing_frozen_check_lets_the_run_start_and_is_reported_in_the_summary_line(self) -> None:
        self.make_fixture()
        messages: list[str] = []
        receipt = self.prechecked_run(
            "attempt-001", notify=messages.append, frozen_check=lambda worktree, plan, *, environ=None: self.passed()
        )
        self.assertEqual(receipt["result"], "passed")
        self.assertIn("定向冻结检查通过", messages[0])

    def test_selected_groups_follow_the_engine_rules(self) -> None:
        self.make_fixture()
        from tools.upstream_merge.gate_guard import _selected_group_names

        # 首次全量：三个执行组都要跑，包含 full-regression
        self.assertEqual(_selected_group_names(self.plan, None, None), {"secret-scan", "inventory-closure", "full-regression"})
        # 显式选择：只含被选门禁所在的组
        self.assertEqual(_selected_group_names(self.plan, "inventory_closure", None), {"inventory-closure"})
        # 参数不合法交给引擎报错：这里返回 None，保守地按“要跑冻结检查”处理
        self.assertIsNone(_selected_group_names(self.plan, "no-such-gate", None))
        self.assertIsNone(_selected_group_names(self.plan, None, "no-such-attempt"))

    def test_partial_rerun_skips_the_check_unless_the_full_regression_group_is_among_the_failures(self) -> None:
        from tools.upstream_merge.gate_guard import _selected_group_names

        self.make_fixture()
        self.plan = self.build_plan(real_secret_scan=False, inventory_exit=1)
        run_verification_gates(self.plan, "attempt-001")
        self.assertEqual(_selected_group_names(self.plan, None, "attempt-001"), {"inventory-closure"})
        self.fixture_rerun_plan = self.build_plan(real_secret_scan=False)
        self.scenario("failed_steps")
        run_verification_gates(self.fixture_rerun_plan, "attempt-002")
        self.assertEqual(_selected_group_names(self.fixture_rerun_plan, None, "attempt-002"), {"full-regression"})

    def test_check_runs_for_a_normal_full_run_and_receives_the_plan(self) -> None:
        self.make_fixture()
        seen = []

        def check(worktree, plan, *, environ=None):
            seen.append((worktree, plan.fork_head))
            return self.passed()

        self.prechecked_run("attempt-001", frozen_check=check)
        self.assertTrue(seen[0][0].is_dir())
        self.assertEqual(seen[0][1], self.plan.fork_head)

    def test_default_check_is_not_applicable_to_synthetic_trees_so_existing_flows_are_unchanged(self) -> None:
        self.make_fixture()
        messages: list[str] = []
        receipt = self.prechecked_run("attempt-001", notify=messages.append)
        self.assertEqual(receipt["result"], "passed")
        self.assertIn("定向冻结检查不适用", messages[0])

    def test_run_gates_with_prechecks_exposes_the_injection_point(self) -> None:
        import inspect

        self.assertIn("frozen_check", inspect.signature(run_gates_with_prechecks).parameters)
        self.assertIs(gate_guard.run_frozen_precheck, run_frozen_precheck)


if __name__ == "__main__":
    unittest.main()
