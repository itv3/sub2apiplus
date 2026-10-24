"""工具链版本核对与 lint 版本单一来源：真实仓库文件必须通过，各种反例必须被拦下。

CI 的出站规格作业会跑到本文件：它读取仓库里真实的 ``backend-ci.yml`` 与 ``backend/go.mod``，所以以后谁改 lint
步骤的写法、增删标签、改 Go 版本而没有同步，这里会先红。探测命令（go、golangci-lint）一律用假实现，不依赖本机安装。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge import toolchain
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.toolchain import (
    CI_WORKFLOW_PATH,
    LINT_BINARY_ENV,
    check_toolchain,
    declared_toolchain,
    go_versions_agree,
    parse_go_mod,
    resolve_lint_binary,
    workflow_findings,
)

SOURCE_ROOT = Path(__file__).resolve().parents[3]

# 与真实 backend-ci.yml 同形的最小工作流：一个定义点、三个步骤引用它、标签恰为默认／unit／integration。
GOOD_WORKFLOW = """\
name: CI

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: Verify Go version
        run: |
          go version | grep -q 'go1.27.3'

  golangci-lint:
    runs-on: ubuntu-latest
    env:
      GOLANGCI_LINT_VERSION: v2.14.0
    steps:
      - uses: actions/checkout@v6
      - name: Verify Go version
        run: |
          go version | grep -q 'go1.27.3'
      - name: golangci-lint
        uses: golangci/golangci-lint-action@v9
        with:
          version: ${{ env.GOLANGCI_LINT_VERSION }}
          args: --timeout=30m
          working-directory: backend
      - name: golangci-lint (unit)
        uses: golangci/golangci-lint-action@v9
        with:
          version: ${{ env.GOLANGCI_LINT_VERSION }}
          args: --timeout=30m --build-tags=unit
          working-directory: backend
      - name: golangci-lint (integration)
        uses: golangci/golangci-lint-action@v9
        with:
          version: ${{ env.GOLANGCI_LINT_VERSION }}
          args: --timeout=30m --build-tags=integration
          working-directory: backend
"""

GO_MOD = "module example.com/backend\n\ngo 1.27.3\n"


def fake_probe(go: str = "go1.27.3", lint: str | None = "golangci-lint has version 2.14.0 built with go1.27.3 from abc on 2026-01-01"):
    """假探测：按命令返回预置输出；lint 为 None 表示命令不存在。"""

    def probe(argv, cwd):
        if list(argv[:3]) == ["go", "env", "GOVERSION"]:
            return 0, go + "\n", ""
        if len(argv) == 2 and argv[1] == "--version":
            if lint is None:
                return 127, "", "命令不存在"
            return 0, lint + "\n", ""
        raise AssertionError(f"意外的探测命令：{argv!r}")

    return probe


class WorkflowFindingsTest(unittest.TestCase):
    def findings(self, text: str) -> list[str]:
        return workflow_findings(text)[0]

    def test_real_repository_workflow_passes(self) -> None:
        text = (SOURCE_ROOT / CI_WORKFLOW_PATH).read_text(encoding="utf-8")
        findings, facts = workflow_findings(text)
        self.assertEqual(findings, [])
        self.assertRegex(facts["lint_version"], r"^v[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual(facts["lint_version_source"], "variable")
        # 三个步骤，三种标签各一次；全部引用单一来源。
        self.assertEqual(sorted(step["build_tags"] for step in facts["lint_steps"]), ["", "integration", "unit"])
        self.assertEqual({step["version"] for step in facts["lint_steps"]}, {"${{ env.GOLANGCI_LINT_VERSION }}"})
        # lint 步骤不再出现字面版本：由上面的 findings 为空保证（只看 lint 步骤，不对整个文件做正则，
        # 免得上游为别的 action 加了 version: vX.Y.Z 时误报）。
        self.assertIn("GOLANGCI_LINT_VERSION: v", text)

    def test_real_repository_declaration_is_consistent_with_go_mod(self) -> None:
        declared = declared_toolchain(SOURCE_ROOT)
        self.assertEqual(declared["declaration_findings"], [])
        go_mod = parse_go_mod((SOURCE_ROOT / "backend/go.mod").read_text(encoding="utf-8"))
        expected_go = declared["go"]["version"]
        self.assertEqual(expected_go, go_mod["toolchain"] or f"go{go_mod['go']}")
        self.assertEqual(declared["go"]["ci_verify_versions"], [expected_go])
        self.assertEqual(declared["golangci_lint"]["build_tags"], ["", "integration", "unit"])

    def test_good_fixture_passes(self) -> None:
        findings, facts = workflow_findings(GOOD_WORKFLOW)
        self.assertEqual(findings, [])
        self.assertEqual(facts["lint_version"], "v2.14.0")
        self.assertEqual([item["version"] for item in facts["go_versions"]], ["go1.27.3", "go1.27.3"])

    def test_literal_version_left_in_a_step_is_rejected(self) -> None:
        text = GOOD_WORKFLOW.replace(
            "          version: ${{ env.GOLANGCI_LINT_VERSION }}\n          args: --timeout=30m --build-tags=unit",
            "          version: v2.14.0\n          args: --timeout=30m --build-tags=unit",
        )
        findings = self.findings(text)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("golangci-lint (unit)", findings[0])
        self.assertIn("字面版本残留", findings[0])

    def test_all_three_literals_is_the_old_layout_and_still_yields_an_expected_value(self) -> None:
        # 旧写法（三处字面版本、没有单一来源）：违规，但字面版本唯一时仍可作为期望值，开工检查才能比对本机 lint。
        text = GOOD_WORKFLOW.replace("    env:\n      GOLANGCI_LINT_VERSION: v2.14.0\n", "").replace(
            "${{ env.GOLANGCI_LINT_VERSION }}", "v2.14.0"
        )
        findings, facts = workflow_findings(text)
        self.assertTrue(any("恰好定义一次，实际 0 次" in item for item in findings), findings)
        self.assertEqual(sum("字面版本残留" in item for item in findings), 3)
        self.assertEqual(facts["lint_version"], "v2.14.0")
        self.assertEqual(facts["lint_version_source"], "literal")

    def test_missing_build_tags_are_rejected(self) -> None:
        text = GOOD_WORKFLOW.replace("--timeout=30m --build-tags=integration", "--timeout=30m")
        findings = self.findings(text)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("默认／unit／integration", findings[0])
        self.assertIn("['', '', 'unit']", findings[0])

    def test_extra_or_wrong_build_tags_are_rejected(self) -> None:
        wrong = GOOD_WORKFLOW.replace("--build-tags=integration", "--build-tags=e2e")
        self.assertTrue(any("默认／unit／integration" in item for item in self.findings(wrong)))
        duplicate = GOOD_WORKFLOW.replace("--build-tags=integration", "--build-tags=unit")
        self.assertTrue(any("默认／unit／integration" in item for item in self.findings(duplicate)))

    def test_version_defined_twice_is_rejected(self) -> None:
        text = GOOD_WORKFLOW.replace(
            "  test:\n    runs-on: ubuntu-latest\n",
            "  test:\n    runs-on: ubuntu-latest\n    env:\n      GOLANGCI_LINT_VERSION: v2.13.2\n",
        )
        findings = self.findings(text)
        self.assertTrue(any("恰好定义一次，实际 2 次" in item for item in findings), findings)

    def test_definition_in_another_job_is_not_visible_to_the_lint_steps(self) -> None:
        text = GOOD_WORKFLOW.replace("    env:\n      GOLANGCI_LINT_VERSION: v2.14.0\n", "").replace(
            "  test:\n    runs-on: ubuntu-latest\n",
            "  test:\n    runs-on: ubuntu-latest\n    env:\n      GOLANGCI_LINT_VERSION: v2.14.0\n",
        )
        findings = self.findings(text)
        self.assertEqual(sum("看不到 GOLANGCI_LINT_VERSION" in item for item in findings), 3, findings)

    def test_workflow_level_env_is_visible_to_every_job(self) -> None:
        text = GOOD_WORKFLOW.replace("    env:\n      GOLANGCI_LINT_VERSION: v2.14.0\n", "").replace(
            "\njobs:\n", "\nenv:\n  GOLANGCI_LINT_VERSION: v2.14.0\n\njobs:\n", 1
        )
        self.assertEqual(self.findings(text), [])

    def test_malformed_version_value_is_rejected(self) -> None:
        text = GOOD_WORKFLOW.replace("GOLANGCI_LINT_VERSION: v2.14.0", "GOLANGCI_LINT_VERSION: latest")
        findings = self.findings(text)
        self.assertTrue(any("必须形如 v2.14.0" in item for item in findings), findings)

    def test_no_lint_steps_at_all_is_rejected(self) -> None:
        text = GOOD_WORKFLOW.split("      - name: golangci-lint\n")[0]
        findings = self.findings(text)
        self.assertTrue(any("没有 golangci/golangci-lint-action 步骤" in item for item in findings), findings)

    def test_missing_or_inconsistent_go_verification_is_rejected(self) -> None:
        without = GOOD_WORKFLOW.replace("go version | grep -q 'go1.27.3'", "go version")
        self.assertTrue(any("没有 Verify Go version" in item for item in self.findings(without)))
        inconsistent = GOOD_WORKFLOW.replace("grep -q 'go1.27.3'", "grep -q 'go1.27.2'", 1)
        self.assertTrue(any("Go 校验版本不一致" in item for item in self.findings(inconsistent)))

    def test_quoted_and_commented_values_are_read_literally(self) -> None:
        text = GOOD_WORKFLOW.replace(
            "GOLANGCI_LINT_VERSION: v2.14.0", 'GOLANGCI_LINT_VERSION: "v2.14.0"  # 唯一定义点'
        )
        findings, facts = workflow_findings(text)
        self.assertEqual(findings, [])
        self.assertEqual(facts["lint_version"], "v2.14.0")


class DeclaredToolchainTest(unittest.TestCase):
    def tree(self, workflow: str = GOOD_WORKFLOW, go_mod: str | None = GO_MOD) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / ".github/workflows").mkdir(parents=True)
        (root / ".github/workflows/backend-ci.yml").write_text(workflow, encoding="utf-8")
        (root / "backend").mkdir()
        if go_mod is not None:
            (root / "backend/go.mod").write_text(go_mod, encoding="utf-8")
        return root

    def test_consistent_tree_has_no_findings(self) -> None:
        declared = declared_toolchain(self.tree())
        self.assertEqual(declared["declaration_findings"], [])
        self.assertEqual(declared["go"]["version"], "go1.27.3")
        self.assertEqual(declared["golangci_lint"]["version"], "v2.14.0")

    def test_ci_go_version_must_match_go_mod(self) -> None:
        root = self.tree(go_mod="module example.com/backend\n\ngo 1.27.2\n")
        findings = declared_toolchain(root)["declaration_findings"]
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("go1.27.3", findings[0])
        self.assertIn("go1.27.2", findings[0])

    def test_toolchain_directive_wins_over_go_directive(self) -> None:
        root = self.tree(go_mod="module example.com/backend\n\ngo 1.27.0\n\ntoolchain go1.27.3\n")
        declared = declared_toolchain(root)
        self.assertEqual(declared["declaration_findings"], [])
        self.assertEqual(declared["go"]["version"], "go1.27.3")
        self.assertEqual(declared["go"]["toolchain_directive"], "go1.27.3")
        # toolchain 指令与 CI 不一致同样被拦下。
        stale = self.tree(go_mod="module example.com/backend\n\ngo 1.27.3\n\ntoolchain go1.27.4\n")
        self.assertTrue(declared_toolchain(stale)["declaration_findings"])

    def test_missing_files_are_findings_not_crashes(self) -> None:
        root = self.tree(go_mod=None)
        (root / ".github/workflows/backend-ci.yml").unlink()
        findings = declared_toolchain(root)["declaration_findings"]
        self.assertTrue(any("缺少 backend/go.mod" in item for item in findings))
        self.assertTrue(any("缺少 .github/workflows/backend-ci.yml" in item for item in findings))

    def test_go_versions_agree_semantics(self) -> None:
        self.assertTrue(go_versions_agree("go1.27.3", "go1.27.3"))
        self.assertFalse(go_versions_agree("go1.27.3", "go1.27.2"))
        self.assertFalse(go_versions_agree("go1.27.3", "go1.26.6"))
        # 期望只写到主次版本时，补丁号不限。
        self.assertTrue(go_versions_agree("go1.27", "go1.27.9"))
        self.assertFalse(go_versions_agree("go1.27", "go1.28.0"))


class CheckToolchainTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".github/workflows/backend-ci.yml").write_text(GOOD_WORKFLOW, encoding="utf-8")
        (self.root / "backend").mkdir()
        (self.root / "backend/go.mod").write_text(GO_MOD, encoding="utf-8")
        self.environ = {"PATH": "/usr/bin", LINT_BINARY_ENV: "/opt/pinned/golangci-lint"}

    def check(self, role: str | None = "candidate", **probe_arguments) -> dict:
        return check_toolchain(self.root, role, probe=fake_probe(**probe_arguments), environ=self.environ)

    def test_matching_toolchain_passes_and_reports_expected_and_actual(self) -> None:
        report = self.check()
        self.assertEqual(report["result"], "passed")
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["expected"]["go"]["version"], "go1.27.3")
        self.assertEqual(report["expected"]["golangci_lint"]["version"], "v2.14.0")
        self.assertEqual(report["actual"]["go"]["version"], "go1.27.3")
        self.assertEqual(report["actual"]["golangci_lint"]["version"], "2.14.0")
        self.assertEqual(report["actual"]["golangci_lint"]["built_with_go"], "go1.27.3")
        self.assertEqual(report["actual"]["golangci_lint"]["path"], "/opt/pinned/golangci-lint")
        self.assertEqual(Path(report["actual"]["go"]["cwd"]), self.root / "backend")

    def test_go_version_mismatch_blocks_with_a_gotoolchain_hint(self) -> None:
        report = self.check(go="go1.27.2")
        self.assertEqual(report["result"], "blocked")
        (mismatch,) = report["mismatches"]
        self.assertEqual((mismatch["item"], mismatch["expected"], mismatch["actual"]), ("go.version", "go1.27.3", "go1.27.2"))
        self.assertTrue(any("GOTOOLCHAIN=go1.27.3" in hint for hint in report["hints"]), report["hints"])

    def test_lint_version_mismatch_blocks_and_hints_the_override_variable(self) -> None:
        report = self.check(lint="golangci-lint has version 2.13.2 built with go1.27.3 from abc on 2026-01-01")
        self.assertEqual(report["result"], "blocked")
        (mismatch,) = report["mismatches"]
        self.assertEqual((mismatch["item"], mismatch["expected"], mismatch["actual"]), ("golangci_lint.version", "v2.14.0", "2.13.2"))
        self.assertTrue(any(LINT_BINARY_ENV in hint and "v2.14.0" in hint for hint in report["hints"]), report["hints"])

    def test_lint_built_with_an_older_go_minor_blocks(self) -> None:
        report = self.check(lint="golangci-lint has version 2.14.0 built with go1.26.6 from abc on 2026-01-01")
        self.assertEqual(report["result"], "blocked")
        (mismatch,) = report["mismatches"]
        self.assertEqual(mismatch["item"], "golangci_lint.built_with_go")
        self.assertEqual(mismatch["actual"], "go1.26.6")

    def test_lint_built_with_an_older_patch_of_the_same_minor_is_allowed(self) -> None:
        report = self.check(lint="golangci-lint has version 2.14.0 built with go1.27.0 from abc on 2026-01-01")
        self.assertEqual(report["result"], "passed", report["mismatches"])

    def test_missing_lint_binary_blocks(self) -> None:
        report = self.check(lint=None)
        self.assertEqual(report["result"], "blocked")
        self.assertEqual(report["mismatches"][0]["item"], "golangci_lint.version")
        self.assertIsNone(report["actual"]["golangci_lint"]["version"])

    def test_unrunnable_go_blocks(self) -> None:
        def probe(argv, cwd):
            if list(argv[:2]) == ["go", "env"]:
                return 127, "", "go: command not found"
            return fake_probe()(argv, cwd)

        report = check_toolchain(self.root, "baseline", probe=probe, environ=self.environ)
        self.assertEqual(report["result"], "blocked")
        self.assertEqual(report["mismatches"][0]["item"], "go.version")
        self.assertIn("go: command not found", report["actual"]["go"]["error"])

    def test_inconsistent_declaration_blocks_even_if_the_machine_matches(self) -> None:
        (self.root / "backend/go.mod").write_text("module example.com/backend\n\ngo 1.27.2\n", encoding="utf-8")
        report = check_toolchain(self.root, "baseline", probe=fake_probe(go="go1.27.2"), environ=self.environ)
        self.assertEqual(report["result"], "blocked")
        self.assertEqual([item["item"] for item in report["mismatches"]], ["declaration"])

    def test_baseline_and_candidate_use_each_trees_own_declaration(self) -> None:
        # 基线取主干声明的版本，候选取合并后声明的版本：同一台机器对两棵树可以得出相反的结论。
        candidate = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(candidate, ignore_errors=True))
        (candidate / ".github/workflows").mkdir(parents=True)
        (candidate / ".github/workflows/backend-ci.yml").write_text(
            GOOD_WORKFLOW.replace("go1.27.3", "go1.27.4").replace("v2.14.0", "v2.15.0"), encoding="utf-8"
        )
        (candidate / "backend").mkdir()
        (candidate / "backend/go.mod").write_text("module example.com/backend\n\ngo 1.27.4\n", encoding="utf-8")
        machine = fake_probe(go="go1.27.3")
        baseline = check_toolchain(self.root, "baseline", probe=machine, environ=self.environ)
        upgraded = check_toolchain(candidate, "candidate", probe=machine, environ=self.environ)
        self.assertEqual(baseline["result"], "passed")
        self.assertEqual(upgraded["result"], "blocked")
        self.assertEqual({item["item"] for item in upgraded["mismatches"]}, {"go.version", "golangci_lint.version"})
        self.assertEqual((baseline["role"], upgraded["role"]), ("baseline", "candidate"))

    def test_invalid_role_and_missing_directory_are_rejected(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "role 非法"):
            check_toolchain(self.root, "trunk", probe=fake_probe())
        with self.assertRaisesRegex(UpstreamMergeError, "仓库目录不存在"):
            check_toolchain(self.root / "missing", "baseline", probe=fake_probe())

    def test_relative_lint_override_is_rejected(self) -> None:
        report = check_toolchain(
            self.root, "candidate", probe=fake_probe(), environ={"PATH": "/usr/bin", LINT_BINARY_ENV: "golangci-lint"}
        )
        self.assertEqual(report["result"], "blocked")
        self.assertIn("必须是绝对路径", report["actual"]["golangci_lint"]["error"])


class LintBinaryResolutionTest(unittest.TestCase):
    def test_explicit_variable_wins_over_path(self) -> None:
        self.assertEqual(resolve_lint_binary({"PATH": "/usr/bin", LINT_BINARY_ENV: "/opt/x/golangci-lint"}), "/opt/x/golangci-lint")

    def test_blank_variable_falls_back_to_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "golangci-lint"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            self.assertEqual(resolve_lint_binary({"PATH": directory, LINT_BINARY_ENV: "  "}), str(binary))
        self.assertIsNone(resolve_lint_binary({"PATH": "/nonexistent-dir"}))

    def test_default_probe_survives_missing_commands(self) -> None:
        code, _stdout, stderr = toolchain.run_probe(["/nonexistent/binary-for-test"], Path("/"))
        self.assertEqual(code, 127)
        self.assertIn("命令不存在", stderr)


if __name__ == "__main__":
    unittest.main()
