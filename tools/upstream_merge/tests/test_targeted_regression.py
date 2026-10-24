"""定向回归清单：冲突／实际修改／上游单侧改动与受影响包、测试的对应关系，以及退回全量的各种理由。

只用合成 Git 图和假的 ``go list``，不依赖本机 Go 工具链。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge import targeted_regression as tr
from tools.upstream_merge.canonical import bind_identity, write_json_once
from tools.upstream_merge.errors import UpstreamMergeError

SOURCE_ROOT = Path(__file__).resolve().parents[3]
MODULE = "github.com/Wei-Shaw/sub2api"

MAKEFILE = """EGRESS_SPEC_CI_CHECKS := \\
\ttest-upstream-merge-tools \\
\ttest-official-client-control \\
\tegress-spec-go-test
egress-spec-go-test:
\t@cd backend && go test \\
\t\t./internal/covered/... \\
\t\t./cmd/tool/ -count=1

# 注释
other:
\techo ok
"""
WORKFLOW = """name: CI
on: [push]
jobs:
  shell:
    runs-on: macos-15
    steps:
      - name: Check deployment scripts
        run: |
          /bin/bash -n deploy/apple-container.sh
          /bin/sh deploy/tests/docker-compose-security-test.sh
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Check simple mode
        run: /bin/sh deploy/tests/docker-compose-simple-mode-env-test.sh
"""


def sh(root: Path, *argv: str) -> str:
    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    completed = subprocess.run(list(argv), cwd=root, capture_output=True, text=True, env=environment, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"命令失败 {argv!r}: {completed.stderr or completed.stdout}")
    return completed.stdout.strip()


def package(import_dir: str, *, imports=(), tests=True, unit_only_tests=False, embed=()) -> dict:
    return {"dir": import_dir, "imports": list(imports), "tests": tests, "embed": list(embed), "unit_only_tests": unit_only_tests}


class FakeGoList:
    """按包描述表回答 go list：默认、unit、integration 三组标签。"""

    def __init__(self, root: Path, packages: dict[str, dict], *, deps: dict[str, list[str]] | None = None) -> None:
        self.root = root
        self.packages = packages
        self.deps = deps or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, backend: Path, tags: str, fields: str) -> list[dict]:
        self.calls.append((tags, fields))
        result = []
        for directory, info in self.packages.items():
            path = f"{MODULE}/{directory}" if directory else MODULE
            if fields == "ImportPath,Deps":
                result.append({"ImportPath": path, "Deps": self.deps.get(directory, [])})
                continue
            item = {
                "ImportPath": path,
                "Dir": str(backend / directory) if directory else str(backend),
                "GoFiles": [f"{Path(directory).name or 'main'}.go"],
                "Imports": [f"{MODULE}/{target}" for target in info["imports"]],
                "EmbedFiles": list(info["embed"]),
            }
            has_tests = info["tests"] and (tags == "unit" or not info["unit_only_tests"])
            if has_tests:
                item["TestGoFiles"] = [f"{Path(directory).name or 'main'}_test.go"]
            result.append(item)
        return result


class Scenario:
    """base → fork / upstream → candidate 的合成仓库。"""

    def __init__(self, temp: Path) -> None:
        self.root = temp / "repo"
        self.root.mkdir()
        sh(self.root, "git", "init", "-b", "main")
        self.base_files = {
            "Makefile": MAKEFILE,
            ".github/workflows/backend-ci.yml": WORKFLOW,
            "backend/go.mod": f"module {MODULE}\n\ngo 1.27.0\n\nrequire (\n\texample.com/dep v1.0.0\n)\n",
            "backend/internal/a/a.go": "package a\n",
            "backend/internal/b/b.go": "package b\n",
            "backend/internal/c/c.go": "package c\n",
            "backend/internal/covered/covered.go": "package covered\n",
            "backend/resources/data/prices.json": "{}\n",
            "frontend/src/x.ts": "export const x = 1\n",
            "frontend/src/y.ts": "export const y = 1\n",
            "deploy/run.sh": "echo 1\n",
            "docs/readme.md": "说明\n",
            "tools/official_client_capture/cap.py": "x = 1\n",
            "tools/upstream_merge/u.py": "y = 1\n",
        }
        self.write(self.base_files)
        self.commit("base")
        self.base = sh(self.root, "git", "rev-parse", "HEAD")

    def write(self, files: dict[str, str | None]) -> None:
        for relative, content in files.items():
            path = self.root / relative
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        sh(self.root, "git", "add", "--all")
        sh(self.root, "git", "commit", "-q", "-m", message, "--allow-empty")
        return sh(self.root, "git", "rev-parse", "HEAD")

    def build(self, fork_changes: dict, upstream_changes: dict, candidate_changes: dict) -> tuple[str, str]:
        """fork＝base＋fork_changes；upstream＝base＋upstream_changes；候选＝fork＋（上游改动逐字节带入）＋candidate_changes。"""

        sh(self.root, "git", "checkout", "-q", "-B", "upstream", self.base)
        self.write(upstream_changes)
        upstream = self.commit("upstream")
        sh(self.root, "git", "checkout", "-q", "-B", "fork", self.base)
        self.write(fork_changes)
        fork = self.commit("fork")
        sh(self.root, "git", "checkout", "-q", "-B", "candidate", fork)
        for relative, content in upstream_changes.items():
            self.write({relative: content})
        self.write(candidate_changes)
        self.commit("candidate")
        return fork, upstream

    def plan(self, fork: str, upstream: str, packages: dict[str, dict], **kwargs) -> dict:
        golist = FakeGoList(self.root / "backend", packages, deps=kwargs.pop("deps", None))
        return tr.plan_targeted_regression(self.root, fork, upstream, go_list=golist, **kwargs)


STANDARD_PACKAGES = {
    "internal/a": package("internal/a"),
    "internal/b": package("internal/b", imports=["internal/a"]),
    "internal/c": package("internal/c"),
    "internal/covered": package("internal/covered", imports=["internal/a"]),
}


class TargetedRegressionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.scenario = Scenario(self.temp)

    def basic_merge(self, **extra_upstream):
        """a.go 两侧都改（冲突），合并结果与两侧都不同；b.go 只有上游改并原样带入；c.go 只有 fork 改。"""

        fork_changes = {"backend/internal/a/a.go": "package a // fork\n", "backend/internal/c/c.go": "package c // fork\n"}
        upstream_changes = {"backend/internal/a/a.go": "package a // upstream\n", "backend/internal/b/b.go": "package b // up\n", **extra_upstream}
        candidate_changes = {"backend/internal/a/a.go": "package a // merged\n"}
        return self.scenario.build(fork_changes, upstream_changes, candidate_changes)


class GitFactsTest(TargetedRegressionTestCase):
    def test_conflict_actual_modified_and_upstream_only_are_told_apart(self) -> None:
        fork, upstream = self.basic_merge()
        facts = tr.collect_git_facts(self.scenario.root, fork, upstream)
        self.assertEqual(facts.conflict, {"backend/internal/a/a.go"})
        self.assertEqual(facts.actual_modified, {"backend/internal/a/a.go"})
        # b.go 只有上游改过且候选与上游逐字节相同：不算实际修改；c.go 只有 fork 改过：两者都不算。
        self.assertEqual(facts.upstream_only, {"backend/internal/b/b.go"})
        self.assertNotIn("backend/internal/c/c.go", facts.conflict | facts.actual_modified | facts.upstream_only)
        self.assertEqual(facts.merge_base, self.scenario.base)

    def test_post_merge_edit_of_a_one_sided_file_counts_as_actual_modification(self) -> None:
        fork, upstream = self.scenario.build(
            {"backend/internal/c/c.go": "package c // fork\n"},
            {"backend/internal/b/b.go": "package b // up\n"},
            {"backend/internal/b/b.go": "package b // adjusted after merge\n"},
        )
        facts = tr.collect_git_facts(self.scenario.root, fork, upstream)
        self.assertEqual(facts.conflict, frozenset())
        self.assertEqual(facts.actual_modified, {"backend/internal/b/b.go"})
        self.assertEqual(facts.upstream_only, frozenset())


class GoSelectionTest(TargetedRegressionTestCase):
    def test_packages_follow_files_and_direct_importers_with_reasons(self) -> None:
        fork, upstream = self.basic_merge()
        document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        affected = {item["dir"]: item for item in document["go"]["affected_packages"]}
        # a：冲突＋实际修改的所在包；b：上游单侧改动所在包，同时直接导入 a；covered：只因直接导入 a 而受影响；c 与改动无关。
        self.assertEqual(set(affected), {"internal/a", "internal/b", "internal/covered"})
        self.assertIn("冲突文件 1 个", affected["internal/a"]["reasons"][0])
        self.assertIn("backend/internal/a/a.go", affected["internal/a"]["reasons"][0])
        self.assertTrue(any("上游单侧改动 1 个" in reason for reason in affected["internal/b"]["reasons"]))
        self.assertTrue(any("直接导入受影响的包 internal/a" in reason for reason in affected["internal/b"]["reasons"]))
        self.assertEqual(affected["internal/covered"]["seed_files"], [])
        self.assertEqual(affected["internal/covered"]["importer_of"], [f"{MODULE}/internal/a"])
        self.assertFalse(document["go"]["fallback_full_go"])

    def test_packages_already_run_by_the_egress_spec_lane_are_not_repeated_under_default_tags(self) -> None:
        fork, upstream = self.basic_merge()
        document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        tag_sets = document["go"]["tag_sets"]
        self.assertEqual(tag_sets["default"]["packages"], ["internal/a", "internal/b"])
        self.assertEqual(tag_sets["default"]["covered_by_egress_spec_lane"], ["internal/covered"])
        # unit 标签组出站规格线没跑过，不能省。
        self.assertEqual(tag_sets["unit"]["packages"], ["internal/a", "internal/b", "internal/covered"])
        self.assertEqual(document["go"]["egress_spec_go_test_patterns"], ["internal/covered/...", "cmd/tool"])
        self.assertIn("integration", document["go"]["integration"]["reason"])
        self.assertFalse(document["go"]["integration"]["executed_in_fast"])

    def test_package_without_tests_under_a_tag_set_is_listed_not_run(self) -> None:
        packages = {**STANDARD_PACKAGES, "internal/b": package("internal/b", imports=["internal/a"], tests=False)}
        fork, upstream = self.basic_merge()
        document = self.scenario.plan(fork, upstream, packages)
        self.assertNotIn("internal/b", document["go"]["tag_sets"]["default"]["packages"])
        self.assertIn("internal/b", document["go"]["tag_sets"]["default"]["no_test_files"])

    def test_unit_only_tests_run_only_in_the_unit_tag_set(self) -> None:
        packages = {**STANDARD_PACKAGES, "internal/b": package("internal/b", imports=["internal/a"], unit_only_tests=True)}
        fork, upstream = self.basic_merge()
        document = self.scenario.plan(fork, upstream, packages)
        self.assertNotIn("internal/b", document["go"]["tag_sets"]["default"]["packages"])
        self.assertIn("internal/b", document["go"]["tag_sets"]["unit"]["packages"])

    def test_upstream_only_importers_switch_limits_the_dependency_spread(self) -> None:
        fork, upstream = self.scenario.build(
            {"backend/internal/c/c.go": "package c // fork\n"},
            {"backend/internal/a/a.go": "package a // up\n"},
            {},
        )
        wide = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        narrow = self.scenario.plan(fork, upstream, STANDARD_PACKAGES, upstream_only_importers=False)
        self.assertEqual({item["dir"] for item in wide["go"]["affected_packages"]}, {"internal/a", "internal/b", "internal/covered"})
        self.assertEqual({item["dir"] for item in narrow["go"]["affected_packages"]}, {"internal/a"})

    def test_deleted_last_go_file_means_the_package_is_gone_without_fallback(self) -> None:
        fork, upstream = self.scenario.build(
            {"backend/internal/c/c.go": "package c // fork\n"},
            {"backend/internal/a/a.go": "package a // up\n"},
            {"backend/internal/c/c.go": None},
        )
        packages = {key: value for key, value in STANDARD_PACKAGES.items() if key != "internal/c"}
        document = self.scenario.plan(fork, upstream, packages)
        self.assertEqual([item["path"] for item in document["go"]["removed_packages"]], ["backend/internal/c/c.go"])
        self.assertFalse(document["go"]["fallback_full_go"])

    def test_go_file_in_a_directory_no_tag_set_compiles_is_not_a_fallback_reason(self) -> None:
        fork, upstream = self.scenario.build(
            {"backend/internal/c/c.go": "package c // fork\n"},
            {"backend/internal/integration/e2e_test.go": "//go:build e2e\npackage integration\n"},
            {"backend/internal/integration/e2e_test.go": "//go:build e2e\npackage integration // adjusted\n"},
        )
        document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        self.assertFalse(document["go"]["fallback_full_go"])
        self.assertEqual([item["path"] for item in document["no_test_needed"]], ["backend/internal/integration/e2e_test.go"])
        self.assertIn("构建标签排除", document["no_test_needed"][0]["reason"])

    def test_data_file_is_attributed_to_the_package_that_reads_it_by_name(self) -> None:
        fork, upstream = self.scenario.build(
            {"backend/internal/b/b.go": "package b // fork\n"},
            {"backend/internal/c/c_test.go": 'package c\n\nvar _ = "../../resources/data/prices.json"\n'},
            {"backend/resources/data/prices.json": '{"changed": true}\n'},
        )
        document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        affected = {item["dir"]: item for item in document["go"]["affected_packages"]}
        self.assertIn("internal/c", affected)
        self.assertIn("数据文件按文件名被该包的 Go 代码引用", " ".join(affected["internal/c"]["reasons"]))
        self.assertFalse(document["go"]["fallback_full_go"])

    def test_unreferenced_backend_data_file_is_unresolved_and_forces_full_go(self) -> None:
        fork, upstream = self.scenario.build(
            {"backend/internal/c/c.go": "package c // fork\n"},
            {"backend/resources/data/prices.json": '{"up": 1}\n'},
            {"backend/resources/data/prices.json": '{"merged": 1}\n'},
        )
        document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        self.assertEqual([item["path"] for item in document["unresolved"]], ["backend/resources/data/prices.json"])
        self.assertTrue(document["unresolved"][0]["go_related"])
        self.assertTrue(document["go"]["fallback_full_go"])
        self.assertTrue(document["supplement_required"])


class FallbackTest(TargetedRegressionTestCase):
    def test_go_directive_change_falls_back_to_full(self) -> None:
        new_mod = f"module {MODULE}\n\ngo 1.27.2\n\nrequire (\n\texample.com/dep v1.0.0\n)\n"
        fork, upstream = self.scenario.build(
            {"backend/internal/c/c.go": "package c // fork\n"}, {"backend/go.mod": new_mod}, {}
        )
        document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        self.assertTrue(document["go"]["fallback_full_go"])
        self.assertTrue(any("go／toolchain 指令变化" in reason for reason in document["go"]["fallback_reasons"]))

    def test_dependency_version_bump_pulls_in_the_packages_depending_on_that_module(self) -> None:
        new_mod = f"module {MODULE}\n\ngo 1.27.0\n\nrequire (\n\texample.com/dep v1.1.0\n)\n"
        fork, upstream = self.scenario.build(
            {"backend/internal/b/b.go": "package b // fork\n"}, {"backend/go.mod": new_mod}, {}
        )
        document = self.scenario.plan(
            fork, upstream, STANDARD_PACKAGES, deps={"internal/c": ["example.com/dep/sub", "fmt"], "internal/a": ["fmt"]}
        )
        self.assertFalse(document["go"]["fallback_full_go"])
        self.assertEqual(document["go"]["go_mod"]["changed_modules"], {"example.com/dep": {"fork": "v1.0.0", "candidate": "v1.1.0"}})
        affected = {item["dir"]: item for item in document["go"]["affected_packages"]}
        self.assertEqual(affected["internal/c"]["dependency_modules"], ["example.com/dep"])
        self.assertNotIn("internal/a", affected)

    def test_go_sum_alone_changing_falls_back(self) -> None:
        fork, upstream = self.scenario.build({}, {"backend/go.sum": "example.com/dep v1.0.0 h1:x\n"}, {})
        document = self.scenario.plan(fork, upstream, STANDARD_PACKAGES)
        self.assertTrue(document["go"]["fallback_full_go"])
        self.assertTrue(any("go.sum" in reason for reason in document["go"]["fallback_reasons"]))

    def test_estimate_over_budget_and_package_cap_fall_back_with_reasons(self) -> None:
        fork, upstream = self.basic_merge()
        over_budget = self.scenario.plan(fork, upstream, STANDARD_PACKAGES, budget_seconds=1)
        self.assertTrue(over_budget["go"]["fallback_full_go"])
        self.assertTrue(any("超过预算" in reason for reason in over_budget["go"]["fallback_reasons"]))
        capped = self.scenario.plan(fork, upstream, STANDARD_PACKAGES, max_packages=1)
        self.assertTrue(capped["go"]["fallback_full_go"])
        self.assertTrue(any("超过上限" in reason for reason in capped["go"]["fallback_reasons"]))

    def test_estimate_uses_the_measured_heavy_package_table(self) -> None:
        self.assertEqual(tr.estimate_seconds(["internal/service", "internal/pkg/x"]), 250 + tr.GO_DEFAULT_PACKAGE_SECONDS)


class NonGoClassificationTest(TargetedRegressionTestCase):
    def merge_with(self, upstream_changes: dict, fork_changes: dict | None = None, candidate_changes: dict | None = None) -> dict:
        fork, upstream = self.scenario.build(
            {"backend/internal/c/c.go": "package c // fork\n", **(fork_changes or {})}, upstream_changes, candidate_changes or {}
        )
        # 上游改动逐字节带入候选，所以要让它们落在“实际修改”里，候选再改一次。
        return self.scenario.plan(fork, upstream, STANDARD_PACKAGES)

    def adjusted(self, files: dict[str, str]) -> dict:
        return self.merge_with({path: f"{content}// upstream\n" for path, content in files.items()}, candidate_changes={
            path: f"{content}// merged\n" for path, content in files.items()
        })

    def test_frontend_files_go_to_vitest_related_and_config_changes_run_everything(self) -> None:
        document = self.adjusted({"frontend/src/x.ts": "export const x = 1\n"})
        self.assertEqual(document["frontend"]["related_files"], ["src/x.ts"])
        self.assertFalse(document["frontend"]["run_all"])
        document = self.adjusted({"frontend/src/x.ts": "export const x = 1\n", "frontend/package.json": "{}\n"})
        self.assertTrue(document["frontend"]["run_all"])
        self.assertIn("package.json", document["frontend"]["run_all_reason"])

    def test_deploy_changes_execute_the_ci_shell_job_commands(self) -> None:
        document = self.adjusted({"deploy/run.sh": "echo 1\n"})
        self.assertEqual(
            document["shell"]["commands"],
            [
                ["/bin/bash", "-n", "deploy/apple-container.sh"],
                ["/bin/sh", "deploy/tests/docker-compose-security-test.sh"],
                ["/bin/sh", "deploy/tests/docker-compose-simple-mode-env-test.sh"],
            ],
        )
        self.assertEqual(document["shell"]["files"], ["deploy/run.sh"])

    def test_docs_need_no_test_with_a_reason(self) -> None:
        document = self.adjusted({"docs/readme.md": "说明\n"})
        item = document["no_test_needed"][0]
        self.assertEqual(item["path"], "docs/readme.md")
        self.assertTrue(item["reason"])
        self.assertEqual(document["unresolved"], [])

    def test_tool_packages_covered_by_the_egress_spec_lane_need_no_extra_test(self) -> None:
        document = self.adjusted({"tools/upstream_merge/u.py": "y = 1\n"})
        self.assertEqual(document["python"], [])
        self.assertIn("test-upstream-merge-tools", document["no_test_needed"][0]["reason"])

    def test_capture_tools_are_unresolved_without_forcing_full_go(self) -> None:
        document = self.adjusted({"tools/official_client_capture/cap.py": "x = 1\n"})
        self.assertEqual([item["path"] for item in document["unresolved"]], ["tools/official_client_capture/cap.py"])
        self.assertFalse(document["unresolved"][0]["go_related"])
        self.assertTrue(document["supplement_required"])
        self.assertFalse(document["go"]["fallback_full_go"])

    def test_unknown_top_level_files_are_unresolved(self) -> None:
        document = self.adjusted({"scripts/mystery.sh": "echo\n"})
        self.assertEqual(document["unresolved"][0]["path"], "scripts/mystery.sh")


class MakefileAndWorkflowParsingTest(unittest.TestCase):
    def test_real_makefile_lists_the_packages_the_egress_spec_lane_already_runs(self) -> None:
        text = (SOURCE_ROOT / "Makefile").read_text(encoding="utf-8")
        patterns = tr.egress_spec_go_test_patterns(text)
        self.assertIn("internal/officialegress/...", patterns)
        self.assertIn("cmd/egressscan", patterns)
        self.assertTrue(tr._pattern_covers("internal/officialegress/...", "internal/officialegress/bindingcontract"))
        self.assertFalse(tr._pattern_covers("cmd/egressscan", "cmd/egressscan/sub"))
        self.assertTrue({"test-upstream-merge-tools", "test-official-client-control", "egress-spec-go-test"} <= tr.egress_spec_checks(text))

    def test_real_workflow_yields_the_deploy_script_checks(self) -> None:
        text = (SOURCE_ROOT / ".github/workflows/backend-ci.yml").read_text(encoding="utf-8")
        commands = tr.deploy_commands_from_workflow(text)
        self.assertIn(["/bin/sh", "deploy/tests/docker-compose-security-test.sh"], commands)
        self.assertIn(["/bin/sh", "deploy/tests/docker-compose-simple-mode-env-test.sh"], commands)
        self.assertTrue(all(command[0] in ("/bin/sh", "/bin/bash") for command in commands))

    def test_go_mod_requirement_diff_and_directives(self) -> None:
        before = "module m\n\ngo 1.27.0\n\nrequire (\n\ta.com/x v1.0.0\n\tb.com/y v2.0.0 // indirect\n)\n"
        after = "module m\n\ngo 1.27.0\n\nrequire (\n\ta.com/x v1.1.0\n\tc.com/z v1.0.0\n)\n"
        changes, reasons = tr.changed_modules(before, after)
        self.assertEqual(changes, {"a.com/x": ("v1.0.0", "v1.1.0"), "b.com/y": ("v2.0.0", None), "c.com/z": (None, "v1.0.0")})
        self.assertEqual(reasons, [])
        _changes, reasons = tr.changed_modules(before, after.replace("go 1.27.0", "go 1.27.2"))
        self.assertTrue(reasons and "指令变化" in reasons[0])
        _changes, reasons = tr.changed_modules(before, after + "\nreplace a.com/x => ../x\n")
        self.assertTrue(any("replace" in reason for reason in reasons))

    def test_go_test_argv_shape(self) -> None:
        self.assertEqual(
            tr.go_test_argv("unit", ["internal/a", "cmd/b"]), ["go", "test", "-tags=unit", "-count=1", "./internal/a", "./cmd/b"]
        )
        self.assertEqual(tr.go_test_argv("", ["internal/a"]), ["go", "test", "-count=1", "./internal/a"])


class WriteAndLoadTest(TargetedRegressionTestCase):
    def planned(self) -> tuple[dict, str, str]:
        fork, upstream = self.basic_merge()
        return self.scenario.plan(fork, upstream, STANDARD_PACKAGES), fork, upstream

    def outside(self, name: str = "targeted.json") -> Path:
        directory = self.temp / "outside"
        directory.mkdir(exist_ok=True)
        return directory / name

    def test_document_binds_inputs_and_round_trips_through_load(self) -> None:
        document, fork, upstream = self.planned()
        self.assertEqual(document["schema_version"], tr.TARGETED_SCHEMA)
        self.assertEqual(document["inputs"]["fork_commit"], fork)
        self.assertEqual(document["inputs"]["upstream_commit"], upstream)
        self.assertEqual(document["inputs"]["candidate_commit"], sh(self.scenario.root, "git", "rev-parse", "HEAD"))
        output = self.outside()
        write_json_once(output, document)
        self.assertEqual(tr.load_targeted_regression(output)["identity_sha256"], document["identity_sha256"])

    def test_tampered_document_is_rejected(self) -> None:
        document, _fork, _upstream = self.planned()
        document["go"]["tag_sets"]["default"]["packages"] = ["internal/a", "internal/zzz"]
        output = self.outside()
        output.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(UpstreamMergeError):
            tr.load_targeted_regression(output)

    def test_unsafe_package_directories_and_shell_commands_are_rejected(self) -> None:
        document, _fork, _upstream = self.planned()
        for mutate in (
            lambda d: d["go"]["tag_sets"]["default"].update(packages=["../escape"]),
            lambda d: d["go"]["tag_sets"]["unit"].update(packages=["internal/a; rm -rf x"]),
            lambda d: d["shell"].update(commands=[["/usr/bin/curl", "http://x"]]),
            lambda d: d["shell"].update(commands=[["/bin/sh", "/etc/passwd"]]),
            lambda d: d["frontend"].update(related_files=["../../etc/passwd"]),
        ):
            tampered = json.loads(json.dumps(document))
            mutate(tampered)
            output = self.outside(f"tampered-{id(mutate)}.json")
            output.write_text(json.dumps(bind_identity({k: v for k, v in tampered.items() if k != "identity_sha256"})), encoding="utf-8")
            with self.subTest(), self.assertRaises(UpstreamMergeError):
                tr.load_targeted_regression(output)

    def test_write_is_once_only_and_must_stay_outside_the_worktree(self) -> None:
        fork, upstream = self.basic_merge()
        golist = FakeGoList(self.scenario.root / "backend", STANDARD_PACKAGES)
        output = self.outside()
        summary = tr.write_targeted_regression(self.scenario.root, fork, upstream, output, go_list=golist)
        self.assertEqual(summary["result"], "planned")
        self.assertFalse(summary["fallback_full_go"])
        with self.assertRaises(UpstreamMergeError):
            tr.write_targeted_regression(self.scenario.root, fork, upstream, output, go_list=golist)
        with self.assertRaises(UpstreamMergeError):
            tr.write_targeted_regression(self.scenario.root, fork, upstream, self.scenario.root / "inside.json", go_list=golist)

    def test_dirty_worktree_is_rejected(self) -> None:
        fork, upstream = self.basic_merge()
        (self.scenario.root / "backend/internal/a/a.go").write_text("package a // dirty\n", encoding="utf-8")
        with self.assertRaises(UpstreamMergeError):
            self.scenario.plan(fork, upstream, STANDARD_PACKAGES)


class CommandLineTest(unittest.TestCase):
    """子命令 targeted-regression-plan 端到端：不依赖 Go（合成仓库没有 backend），输出写一次。"""

    def test_cli_plans_once_and_refuses_to_overwrite(self) -> None:
        import io

        from tools.upstream_merge import __main__ as cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            sh(root, "git", "init", "-b", "main")
            (root / "frontend/src").mkdir(parents=True)
            (root / "frontend/src/x.ts").write_text("export const x = 1\n", encoding="utf-8")
            (root / "README.md").write_text("说明\n", encoding="utf-8")
            sh(root, "git", "add", "--all")
            sh(root, "git", "commit", "-q", "-m", "base")
            base = sh(root, "git", "rev-parse", "HEAD")
            sh(root, "git", "checkout", "-q", "-b", "upstream")
            (root / "frontend/src/x.ts").write_text("export const x = 2\n", encoding="utf-8")
            sh(root, "git", "commit", "-q", "-am", "up")
            upstream = sh(root, "git", "rev-parse", "HEAD")
            sh(root, "git", "checkout", "-q", "-b", "fork", base)
            (root / "frontend/src/x.ts").write_text("export const x = 3\n", encoding="utf-8")
            sh(root, "git", "commit", "-q", "-am", "fork")
            fork = sh(root, "git", "rev-parse", "HEAD")
            sh(root, "git", "checkout", "-q", "-b", "candidate")
            (root / "frontend/src/x.ts").write_text("export const x = 4\n", encoding="utf-8")
            sh(root, "git", "commit", "-q", "-am", "merged")
            output = Path(directory) / "outside" / "targeted.json"
            output.parent.mkdir()
            argv = ["targeted-regression-plan", "--repository", str(root), "--fork", fork, "--upstream", upstream, "--output", str(output)]
            from unittest import mock

            with mock.patch("sys.stdout", new=SimpleBuffer()) as captured:
                code = cli.main(argv)
            self.assertEqual(code, 0)
            result = json.loads(captured.buffer.getvalue().decode("utf-8"))
            self.assertEqual(result["result"], "planned")
            self.assertEqual(result["summary"]["frontend_related_file_count"], 1)
            document = tr.load_targeted_regression(output)
            self.assertEqual(document["frontend"]["related_files"], ["src/x.ts"])
            with mock.patch("sys.stdout", new=SimpleBuffer()), mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(cli.main(argv), 2)


class SimpleBuffer:
    """cli.main 往 sys.stdout.buffer 写字节；测试里用它接住。"""

    def __init__(self) -> None:
        import io

        self.buffer = io.BytesIO()

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
