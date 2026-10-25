"""失败项的复验族与冻结摘要类判据：命令行解析（make／go test／golangci-lint／pnpm）、各族的接受与拒绝、
冻结复验的包覆盖、承接收据的真实性。纯函数测试，不执行任何命令。
"""

from __future__ import annotations

import copy
import hashlib
import unittest

from tools.upstream_merge.freeze import bind_gate_compatible_identity
from tools.upstream_merge.canonical import bind_identity
from tools.upstream_merge.gate_guard import successor_receipt_problems
from tools.upstream_merge.retest_family import (
    family_for_item,
    frozen_retests_satisfied,
    parse_go_test,
    parse_golangci_lint,
    parse_make,
    parse_pnpm,
)


def step(runner_step: str) -> dict:
    """full-regression 组里某个 runner 步骤（形如 lint/golangci-lint-unit）失败对应的失败项。"""

    return {
        "id": f"full-regression/{runner_step}",
        "kind": "group_step",
        "gate_group": "full-regression",
        "gate_ids": [],
        "runner_step": runner_step,
    }


def accepts(failure_id: str, *retests: tuple[list[str], str]) -> bool:
    return family_for_item(step(failure_id), {}).satisfied(list(retests))


class ParseMakeTest(unittest.TestCase):
    def test_flags_assignments_and_directories(self) -> None:
        cases = {
            ("make", "check-egress-spec"): ((".", ("check-egress-spec",), (), True)),
            ("make", "-k", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("/usr/bin/make", "--keep-going", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("make", "CODEX_SPEC_SOURCE_ROOT=/x/y", "check-egress-spec"): (".", ("check-egress-spec",), ("CODEX_SPEC_SOURCE_ROOT=/x/y",), True),
            ("make", "-k", "CODEX_SPEC_SOURCE_ROOT=/x", "check-egress-spec", "test-capture-tools"): (
                ".", ("check-egress-spec", "test-capture-tools"), ("CODEX_SPEC_SOURCE_ROOT=/x",), True),
            ("make", "-j", "4", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("make", "-j4", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("make", "-j", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("make", "--jobs=4", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("make", "-C", ".", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("make", "-C", "backend", "test-unit"): ("backend", ("test-unit",), (), True),
            ("make", "-Cbackend", "test-unit"): ("backend", ("test-unit",), (), True),
            ("make", "--directory=backend", "test-unit"): ("backend", ("test-unit",), (), True),
            ("make", "--directory", "backend", "test-unit"): ("backend", ("test-unit",), (), True),
            ("make", "-f", "Other.mk", "check-egress-spec"): (".", ("check-egress-spec",), (), True),
            ("make", "-n", "check-egress-spec"): (".", ("check-egress-spec",), (), False),
            ("make", "-nk", "check-egress-spec"): (".", ("check-egress-spec",), (), False),
            ("make", "--dry-run", "check-egress-spec"): (".", ("check-egress-spec",), (), False),
            ("make", "-q", "check-egress-spec"): (".", ("check-egress-spec",), (), False),
            ("make", "-t", "check-egress-spec"): (".", ("check-egress-spec",), (), False),
            ("make", "--", "-weird-target"): (".", ("-weird-target",), (), True),
        }
        for argv, (directory, targets, assignments, executes) in cases.items():
            with self.subTest(argv=argv):
                call = parse_make(list(argv), ".")
                self.assertEqual((call.directory, call.targets, call.assignments, call.executes), (directory, targets, assignments, executes))

    def test_working_directory_is_folded_into_the_effective_directory(self) -> None:
        self.assertEqual(parse_make(["make", "x"], "backend").directory, "backend")
        self.assertEqual(parse_make(["make", "-C", "..", "x"], "backend").directory, ".")
        self.assertIsNone(parse_make(["make", "-C", "..", "x"], ".").directory)  # 指向仓库外
        self.assertIsNone(parse_make(["make", "-C", "/abs/dir", "x"], ".").directory)

    def test_non_make_commands_are_not_parsed(self) -> None:
        self.assertIsNone(parse_make(["gmake", "x"], "."))
        self.assertIsNone(parse_make(["python3", "-c", "pass"], "."))
        self.assertIsNone(parse_make([], "."))


class ParseGoTestTest(unittest.TestCase):
    def test_tags_run_and_packages(self) -> None:
        call = parse_go_test(["go", "test", "-tags=unit", "-count=1", "-p", "3", "-run", "Frozen|X", "./internal/service", "./cmd/..."], "backend")
        self.assertEqual((call.directory, call.tags, call.run, call.packages, call.executes), ("backend", frozenset({"unit"}), "Frozen|X", ("./internal/service", "./cmd/..."), True))
        call = parse_go_test(["go", "test", "-tags", "unit,integration", "./..."], "backend")
        self.assertEqual(call.tags, frozenset({"unit", "integration"}))
        call = parse_go_test(["go", "test", "./..."], "backend")
        self.assertIsNone(call.tags)
        call = parse_go_test(["go", "-C", "backend", "test", "-run=Frozen", "./..."], ".")
        self.assertEqual((call.directory, call.run), ("backend", "Frozen"))

    def test_options_that_do_not_run_tests_are_flagged(self) -> None:
        for flag in (["-c"], ["-n"], ["-list", "Test.*"], ["-list=Test.*"]):
            with self.subTest(flag=flag):
                self.assertFalse(parse_go_test(["go", "test", *flag, "./..."], "backend").executes)

    def test_value_flags_do_not_leak_into_packages(self) -> None:
        call = parse_go_test(["go", "test", "-timeout", "10m", "-count", "1", "-run", "TestX", "./internal/service"], "backend")
        self.assertEqual(call.packages, ("./internal/service",))

    def test_not_go_test(self) -> None:
        self.assertIsNone(parse_go_test(["go", "build", "./..."], "backend"))
        self.assertIsNone(parse_go_test(["python3", "-c", "pass"], "backend"))
        self.assertIsNone(parse_go_test(["go", "vet", "./..."], "backend"))


class ParseOtherToolsTest(unittest.TestCase):
    def test_golangci_lint(self) -> None:
        call = parse_golangci_lint(["golangci-lint", "run", "--build-tags=unit", "--timeout=30m", "./..."], "backend")
        self.assertEqual((call.directory, call.build_tags, call.executes), ("backend", frozenset({"unit"}), True))
        call = parse_golangci_lint(["/opt/pinned/golangci-lint", "run", "--build-tags", "integration", "./internal/..."], "backend")
        self.assertEqual(call.build_tags, frozenset({"integration"}))
        self.assertEqual(parse_golangci_lint(["golangci-lint", "run", "./..."], "backend").build_tags, frozenset())
        self.assertIsNone(parse_golangci_lint(["golangci-lint", "version"], "backend"))
        self.assertIsNone(parse_golangci_lint(["python3", "run"], "backend"))

    def test_pnpm(self) -> None:
        call = parse_pnpm(["pnpm", "--dir", "frontend", "run", "lint:check"], ".")
        self.assertEqual((call.directory, call.command, call.argument), ("frontend", "run", "lint:check"))
        call = parse_pnpm(["pnpm", "--dir=frontend", "exec", "vitest", "run"], ".")
        self.assertEqual((call.directory, call.command, call.argument), ("frontend", "exec", "vitest"))
        call = parse_pnpm(["pnpm", "run", "typecheck"], "frontend")
        self.assertEqual((call.directory, call.argument), ("frontend", "typecheck"))
        self.assertIsNone(parse_pnpm(["npm", "run", "x"], "."))


class FamilyAcceptanceTest(unittest.TestCase):
    EGRESS = "egress-spec/check-egress-spec"

    def test_lint_family_matches_the_failed_tag(self) -> None:
        unit = ["golangci-lint", "run", "--build-tags=unit", "./..."]
        integration = ["golangci-lint", "run", "--build-tags=integration", "./..."]
        default = ["golangci-lint", "run", "./..."]
        self.assertTrue(accepts("lint/golangci-lint-unit", (unit, "backend")))
        self.assertTrue(accepts("lint/golangci-lint-unit", (["/opt/x/golangci-lint", "run", "--build-tags", "unit", "./internal/..."], "backend")))
        self.assertTrue(accepts("lint/golangci-lint-default", (default, "backend")))
        self.assertFalse(accepts("lint/golangci-lint-unit", (integration, "backend")))
        self.assertFalse(accepts("lint/golangci-lint-unit", (default, "backend")))
        self.assertFalse(accepts("lint/golangci-lint-default", (unit, "backend")))  # 默认组不带标签
        self.assertFalse(accepts("lint/golangci-lint-unit", (unit, ".")))  # 目录不对
        self.assertFalse(accepts("lint/golangci-lint-unit", (["python3", "-c", "print('ok')"], "backend")))

    def test_go_test_family_matches_the_failed_tag_and_directory(self) -> None:
        self.assertTrue(accepts("go-tests/go-test-default", (["go", "test", "-count=1", "./..."], "backend")))
        self.assertTrue(accepts("go-tests/go-test-default", (["go", "test", "./internal/service", "-run", "TestX"], "backend")))
        self.assertTrue(accepts("go-tests/go-test-unit", (["go", "test", "-tags=unit", "./..."], "backend")))
        self.assertTrue(accepts("go-tests/go-test-integration", (["go", "test", "-tags", "integration", "./..."], "backend")))
        self.assertFalse(accepts("go-tests/go-test-default", (["go", "test", "-tags=unit", "./..."], "backend")))
        self.assertFalse(accepts("go-tests/go-test-unit", (["go", "test", "./..."], "backend")))
        self.assertFalse(accepts("go-tests/go-test-unit", (["go", "test", "-tags=integration", "./..."], "backend")))
        self.assertFalse(accepts("go-tests/go-test-default", (["go", "test", "./..."], ".")))
        self.assertFalse(accepts("go-tests/go-test-default", (["go", "test", "-c", "./..."], "backend")))  # 只编译不运行
        self.assertFalse(accepts("go-tests/go-test-default", (["go", "test", "-list", ".", "./..."], "backend")))
        self.assertFalse(accepts("go-tests/go-test-default", (["go", "build", "./..."], "backend")))

    def test_egress_spec_family(self) -> None:
        for argv in (
            ["make", "check-egress-spec"],
            ["make", "-k", "check-egress-spec-ci"],
            ["make", "CODEX_SPEC_SOURCE_ROOT=/x", "check-egress-spec"],
            ["make", "-C", ".", "check-egress-spec"],
            ["make", "egress-spec-version-leak"],
            ["make", "egress-spec-go-test"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(accepts(self.EGRESS, (argv, ".")))
        for argv, cwd in (
            (["make", "-n", "check-egress-spec"], "."),
            (["make", "-C", "backend", "check-egress-spec"], "."),
            (["make", "check-egress-spec"], "backend"),
            (["make", "test-unit"], "."),
            (["python3", "-c", "print('ok')"], "."),
            (["sh", "-c", "make check-egress-spec"], "."),
        ):
            with self.subTest(argv=argv, cwd=cwd):
                self.assertFalse(accepts(self.EGRESS, (argv, cwd)))

    def test_egress_spec_family_accepts_frozen_go_tests_over_both_packages(self) -> None:
        both = (["go", "test", "./internal/officialegress", "./internal/service", "-run", "Frozen|Transition"], "backend")
        self.assertTrue(accepts(self.EGRESS, both))
        # 分成两条命令、合起来覆盖两个包同样成立
        first = (["go", "test", "./internal/officialegress", "-run", "Frozen"], "backend")
        second = (["go", "test", "./internal/service", "-run", "Frozen"], "backend")
        self.assertTrue(accepts(self.EGRESS, first, second))
        self.assertFalse(accepts(self.EGRESS, first))

    def test_frontend_family_uses_the_matching_script(self) -> None:
        lint = ["pnpm", "--dir", "frontend", "run", "lint:check"]
        typecheck = ["pnpm", "--dir", "frontend", "run", "typecheck"]
        vitest = ["pnpm", "--dir", "frontend", "exec", "vitest", "run", "src/a.spec.ts"]
        self.assertTrue(accepts("frontend/frontend-lint", (lint, ".")))
        self.assertTrue(accepts("frontend/frontend-typecheck", (typecheck, ".")))
        self.assertTrue(accepts("frontend/frontend-critical-vitest", (vitest, ".")))
        self.assertTrue(accepts("frontend/frontend-critical-vitest", (["pnpm", "--dir", "frontend", "run", "test:run"], ".")))
        for failure in ("frontend/frontend-lint", "frontend/frontend-typecheck", "frontend/frontend-critical-vitest"):
            self.assertTrue(accepts(failure, (["make", "test-frontend"], ".")), failure)
        self.assertTrue(accepts("frontend/frontend-critical-vitest", (["make", "test-frontend-critical"], ".")))
        # 对不上的脚本、只跑了关键测试却想关 lint、vitest 不带 run（会进 watch 模式）都不行
        self.assertFalse(accepts("frontend/frontend-lint", (typecheck, ".")))
        self.assertFalse(accepts("frontend/frontend-lint", (["make", "test-frontend-critical"], ".")))
        self.assertFalse(accepts("frontend/frontend-typecheck", (lint, ".")))
        self.assertFalse(accepts("frontend/frontend-critical-vitest", (["pnpm", "--dir", "frontend", "exec", "vitest"], ".")))
        self.assertFalse(accepts("frontend/frontend-lint", (["pnpm", "run", "lint:check"], ".")))  # 目录不是 frontend
        self.assertFalse(accepts("frontend/frontend-lint", (["make", "-n", "test-frontend"], ".")))

    def test_capture_tools_family(self) -> None:
        for target in ("test-capture-tools", "test-capture-tools-shard-check", "test-capture-tools-shard"):
            for failure in ("capture-tools/capture-tools", "capture-tools/capture-tools-shard-check"):
                self.assertTrue(accepts(failure, (["make", target], ".")), (failure, target))
        self.assertTrue(accepts("capture-tools/capture-tools", (["make", "-k", "CAPTURE_TEST_SHARDS=4", "test-capture-tools-shard", "SHARD_INDEX=1"], ".")))
        self.assertFalse(accepts("capture-tools/capture-tools", (["make", "test-frontend"], ".")))
        self.assertFalse(accepts("capture-tools/capture-tools", (["make", "-n", "test-capture-tools"], ".")))
        self.assertFalse(accepts("capture-tools/capture-tools", (["make", "test-capture-tools"], "backend")))

    def test_groups_without_step_details_need_the_gates_own_command(self) -> None:
        commands = {"secret-scan": (["python3", "-c", "scan", "/work/tree"], "."), "full-regression": (["make", "upstream-gate-full"], ".")}
        item = {"id": "secret-scan", "kind": "group", "gate_group": "secret-scan", "gate_ids": [], "runner_step": None}
        family = family_for_item(item, commands)
        self.assertTrue(family.satisfied([(["python3", "-c", "scan", "/work/tree"], ".")]))
        self.assertTrue(family.satisfied([(["python3", "-c", "scan", "/work/tree"], "./")]))
        self.assertFalse(family.satisfied([(["python3", "-c", "scan", "/other/tree"], ".")]))
        self.assertFalse(family.satisfied([(["python3", "-c", "scan", "/work/tree"], "backend")]))
        self.assertFalse(family.satisfied([(["python3", "-c", "print('ok')"], ".")]))
        self.assertIn("python3 -c scan /work/tree", family.description)
        # 组里没有可推导的命令：失败关闭
        missing = family_for_item({**item, "gate_group": "mystery"}, commands)
        self.assertFalse(missing.satisfied([(["python3", "-c", "scan", "/work/tree"], ".")]))

    def test_unknown_runner_steps_fall_back_to_the_groups_own_command(self) -> None:
        commands = {"full-regression": (["make", "upstream-gate-full"], ".")}
        item = {"id": "full-regression/new-lane/new-step", "kind": "group_step", "gate_group": "full-regression", "gate_ids": [], "runner_step": "new-lane/new-step"}
        family = family_for_item(item, commands)
        self.assertFalse(family.satisfied([(["make", "check-egress-spec"], ".")]))
        self.assertTrue(family.satisfied([(["make", "upstream-gate-full"], ".")]))

    def test_worktree_status_is_checked_by_the_tool(self) -> None:
        family = family_for_item({"id": "worktree-status", "kind": "worktree", "gate_group": None}, {})
        self.assertTrue(family.tool_checked)


class FrozenRetestCoverageTest(unittest.TestCase):
    def test_make_targets(self) -> None:
        for target in ("check-egress-spec", "check-egress-spec-ci", "egress-spec-go-test"):
            self.assertTrue(frozen_retests_satisfied([(["make", "-k", target], ".")]), target)
        self.assertFalse(frozen_retests_satisfied([(["make", "egress-spec-version-leak"], ".")]))
        self.assertFalse(frozen_retests_satisfied([(["make", "-n", "check-egress-spec"], ".")]))
        self.assertFalse(frozen_retests_satisfied([(["make", "test-unit"], ".")]))
        self.assertFalse(frozen_retests_satisfied([(["make", "check-egress-spec"], "backend")]))

    def test_go_tests_must_cover_both_packages_with_a_frozen_pattern(self) -> None:
        both = ["go", "test", "./internal/officialegress", "./internal/service", "-run", "Frozen|Transition|Successor|Drift|Retirement", "-count=1", "-p", "3"]
        self.assertTrue(frozen_retests_satisfied([(both, "backend")]))
        self.assertTrue(frozen_retests_satisfied([(["go", "test", "./...", "-run", "Frozen"], "backend")]))
        self.assertTrue(frozen_retests_satisfied([(["go", "test", "./internal/...", "-run=Frozen"], "backend")]))
        # 只覆盖一个包
        self.assertFalse(frozen_retests_satisfied([(["go", "test", "./internal/officialegress", "-run", "Frozen"], "backend")]))
        self.assertFalse(frozen_retests_satisfied([(["go", "test", "./internal/service", "-run", "Frozen"], "backend")]))
        # -run 不含冻结类模式，或根本没有 -run
        self.assertFalse(frozen_retests_satisfied([(["go", "test", "./...", "-run", "TestOther"], "backend")]))
        self.assertFalse(frozen_retests_satisfied([(["go", "test", "./..."], "backend")]))
        # 不是 backend 下执行，或者只是名字叫 go 的别的东西
        self.assertFalse(frozen_retests_satisfied([(both, ".")]))
        self.assertFalse(frozen_retests_satisfied([(["go", "build", "./...", "-run", "Frozen"], "backend")]))
        self.assertFalse(frozen_retests_satisfied([(["echo", "go", "test", "./...", "-run", "Frozen"], "backend")]))
        # 只编译不运行
        self.assertFalse(frozen_retests_satisfied([(["go", "test", "-c", "./...", "-run", "Frozen"], "backend")]))

    def test_two_commands_together_cover_both_packages(self) -> None:
        first = (["go", "test", "./internal/officialegress", "-run", "Frozen"], "backend")
        second = (["go", "test", "./internal/service", "-run", "Frozen"], "backend")
        self.assertTrue(frozen_retests_satisfied([first, second]))
        self.assertFalse(frozen_retests_satisfied([first, first]))


class SuccessorReceiptTest(unittest.TestCase):
    CONTENT = {"docs/a.md": b"current-a", "tools/b.py": b"current-b"}

    @staticmethod
    def digest(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def blob(self, path: str) -> str | None:
        return self.digest(self.CONTENT[path]) if path in self.CONTENT else None

    def receipt(self, **patch) -> dict:
        document = {
            "schema_version": "official-egress-upstream-freeze-successor/v1",
            "scope": "x",
            "transitions": [
                {"path": "docs/a.md", "predecessor_sha256s": ["1" * 64], "to_sha256": self.digest(self.CONTENT["docs/a.md"])},
                {"path": "tools/b.py", "predecessor_sha256": "2" * 64, "to_sha256": self.digest(self.CONTENT["tools/b.py"])},
            ],
            **patch,
        }
        return bind_gate_compatible_identity(document)

    def test_a_real_receipt_has_no_problems_under_either_identity_algorithm(self) -> None:
        self.assertEqual(successor_receipt_problems(self.receipt(), self.blob), [])
        canonical_style = bind_identity({k: v for k, v in self.receipt().items() if k != "identity_sha256"})
        self.assertEqual(successor_receipt_problems(canonical_style, self.blob), [])
        # transition 类的收据（schema 不含 successor）同样接受
        transition = self.receipt(schema_version="official-egress-upstream-source-transition/v3")
        self.assertEqual(successor_receipt_problems(transition, self.blob), [])

    def test_paths_that_no_longer_exist_are_skipped(self) -> None:
        document = self.receipt()
        document["transitions"].append({"path": "gone.txt", "predecessor_sha256s": ["3" * 64], "to_sha256": "4" * 64})
        document = bind_gate_compatible_identity({k: v for k, v in document.items() if k != "identity_sha256"})
        self.assertEqual(successor_receipt_problems(document, self.blob), [])

    def test_lookalike_json_is_rejected(self) -> None:
        cases = {
            "只有 schema_version": ({"schema_version": "x-freeze-successor/v1"}, "identity_sha256"),
            "schema 不含 successor／transition": (self.receipt(schema_version="notes/v1"), "schema_version"),
            "没有自摘要": ({k: v for k, v in self.receipt().items() if k != "identity_sha256"}, "identity_sha256"),
            "自摘要不对": ({**self.receipt(), "identity_sha256": "0" * 64}, "identity_sha256"),
            "改了内容没重算自摘要": ({**self.receipt(), "scope": "tampered"}, "identity_sha256"),
            "transitions 为空": (bind_gate_compatible_identity({"schema_version": "x-successor/v1", "transitions": []}), "transitions 为空"),
            "不是对象": (["x"], "不是 JSON 对象"),
        }
        for name, (document, expected) in cases.items():
            with self.subTest(name):
                problems = successor_receipt_problems(document, self.blob)
                self.assertTrue(any(expected in item for item in problems), problems)

    def test_transitions_need_path_and_both_digests(self) -> None:
        def sealed(transition: dict) -> dict:
            return bind_gate_compatible_identity({"schema_version": "x-successor/v1", "transitions": [transition]})

        good = {"path": "docs/a.md", "predecessor_sha256s": ["1" * 64], "to_sha256": self.digest(self.CONTENT["docs/a.md"])}
        self.assertEqual(successor_receipt_problems(sealed(good), self.blob), [])
        broken = {
            "缺 path": {k: v for k, v in good.items() if k != "path"},
            "缺前序摘要": {k: v for k, v in good.items() if k != "predecessor_sha256s"},
            "前序摘要为空": {**good, "predecessor_sha256s": []},
            "前序摘要格式不对": {**good, "predecessor_sha256s": ["abc"]},
            "缺后继摘要": {k: v for k, v in good.items() if k != "to_sha256"},
            "后继摘要格式不对": {**good, "to_sha256": "xyz"},
        }
        for name, transition in broken.items():
            with self.subTest(name):
                self.assertTrue(successor_receipt_problems(sealed(transition), self.blob), name)

    def test_receipt_that_no_longer_describes_the_current_content_is_rejected(self) -> None:
        document = copy.deepcopy(self.receipt())
        document["transitions"][0]["to_sha256"] = self.digest(b"older-a")
        document = bind_gate_compatible_identity({k: v for k, v in document.items() if k != "identity_sha256"})
        problems = successor_receipt_problems(document, self.blob)
        self.assertTrue(any("已被后来的改动取代" in item and "docs/a.md" in item for item in problems), problems)


if __name__ == "__main__":
    unittest.main()
