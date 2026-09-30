"""上游 CI 作业差异（UM-14）的测试：工作流解析、合成仓库增删改、登记表校验与真实仓库验收。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.ci_jobs import (
    CI_JOB_COVERAGE_RELATIVE,
    CI_JOB_COVERAGE_SCHEMA,
    ci_job_coverage,
    ci_job_delta,
    fork_unregistered_jobs,
    load_ci_job_coverage,
    parse_workflow_jobs,
)
from tools.upstream_merge.errors import UpstreamMergeError

SOURCE_ROOT = Path(__file__).resolve().parents[3]

WORKFLOW = """name: CI

on:
  push:
    branches: [main]

jobs:
  # 注释行不影响作业划分
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: make lint
      - name: Quoted
        run: 'echo "a b"'
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Unit
        run: |
          go test ./...
          go vet ./...
      - name: Tail quote
        run: python -m unittest discover -p 'test_x.py'

concurrency: ci
"""


def git(root: Path, *argv: str) -> str:
    completed = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git {argv!r} 失败：{completed.stderr}")
    return completed.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def registry(workflows: dict) -> dict:
    return {"schema_version": CI_JOB_COVERAGE_SCHEMA, "workflows": workflows}


class ParseWorkflowTest(unittest.TestCase):
    def test_jobs_and_run_commands(self) -> None:
        jobs = parse_workflow_jobs(WORKFLOW)
        self.assertEqual(sorted(jobs), ["lint", "test"])
        self.assertEqual(jobs["lint"]["runs"], ["make lint", 'echo "a b"'])
        self.assertEqual(
            jobs["test"]["runs"],
            ["go test ./...\ngo vet ./...", "python -m unittest discover -p 'test_x.py'"],
        )
        # jobs: 之后的顶层键（concurrency）不属于最后一个作业。
        self.assertNotIn("concurrency", "".join(jobs["test"]["runs"]))

    def test_change_detection_ignores_trailing_whitespace_only(self) -> None:
        base = parse_workflow_jobs(WORKFLOW)
        spaced = parse_workflow_jobs(WORKFLOW.replace("make lint\n", "make lint   \n"))
        self.assertEqual(base["lint"]["sha256"], spaced["lint"]["sha256"])
        changed = parse_workflow_jobs(WORKFLOW.replace("make lint", "make lint-all"))
        self.assertNotEqual(base["lint"]["sha256"], changed["lint"]["sha256"])
        self.assertEqual(base["test"]["sha256"], changed["test"]["sha256"])

    def test_no_jobs_section_and_duplicate_job(self) -> None:
        self.assertEqual(parse_workflow_jobs("name: x\non: push\n"), {})
        with self.assertRaisesRegex(UpstreamMergeError, "重复"):
            parse_workflow_jobs("jobs:\n  a:\n    runs-on: x\n  a:\n    runs-on: y\n")


class CiJobDeltaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "CI Jobs Test")
        git(self.root, "config", "user.email", "ci@example.invalid")
        git(self.root, "config", "commit.gpgsign", "false")
        write(self.root / ".github/workflows/ci.yml", WORKFLOW)
        write(self.root / ".github/workflows/release.yml", "on:\n  push:\n    tags: ['v*']\njobs:\n  release:\n    steps:\n      - run: make release\n")
        self.write_registry(
            {
                ".github/workflows/ci.yml": {
                    "candidate_ci": True,
                    "jobs": {
                        "lint": {"coverage": "local", "lanes": ["lint"]},
                        "test": {"coverage": "local", "lanes": ["go-tests"]},
                        "helpers": {"coverage": "ci_only", "reason": "只在 CI 跑的发布工具测试"},
                    },
                },
                ".github/workflows/release.yml": {"candidate_ci": False, "reason": "只在推 tag 时运行"},
            }
        )
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def write_registry(self, workflows: dict) -> None:
        write(self.root / CI_JOB_COVERAGE_RELATIVE, json.dumps(registry(workflows), ensure_ascii=False, indent=2) + "\n")

    def upstream(self, workflow_text: str, *, extra: dict[str, str] | None = None) -> str:
        git(self.root, "checkout", "-q", "-b", f"upstream-{len(git(self.root, 'branch').splitlines())}", self.base)
        write(self.root / ".github/workflows/ci.yml", workflow_text)
        for relative, text in (extra or {}).items():
            write(self.root / relative, text)
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "upstream")
        commit = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", "main")
        return commit

    def test_delta_lists_added_changed_removed_and_new_workflow(self) -> None:
        # 上游：删掉 lint、改动 test 的命令、新增 helpers，并新增一个工作流 docs.yml。
        upstream_text = """name: CI

on:
  push:
    branches: [main]

jobs:
  helpers:
    steps:
      - run: python -m unittest discover -s tools
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Unit
        run: |
          go test ./...
          go vet -tags=unit ./...
      - name: Tail quote
        run: python -m unittest discover -p 'test_x.py'

concurrency: ci
"""
        upstream = self.upstream(
            upstream_text,
            extra={".github/workflows/docs.yml": "jobs:\n  docs:\n    steps:\n      - run: make docs\n"},
        )
        delta = {item["workflow"]: item for item in ci_job_delta(self.root, self.base, upstream)}
        self.assertEqual(sorted(delta), [".github/workflows/ci.yml", ".github/workflows/docs.yml"])
        changes = {job["job"]: job["change"] for job in delta[".github/workflows/ci.yml"]["jobs"]}
        self.assertEqual(changes, {"helpers": "added", "test": "changed", "lint": "removed"})
        self.assertEqual(delta[".github/workflows/docs.yml"]["status"], "added")

    def test_coverage_blocks_unregistered_and_lists_ci_only_commands(self) -> None:
        text = WORKFLOW.replace(
            "  test:\n",
            "  helpers:\n    steps:\n      - run: python -m unittest discover -s tools\n"
            "  surprise:\n    steps:\n      - run: make surprise\n  test:\n",
        )
        upstream = self.upstream(text, extra={".github/workflows/docs.yml": "jobs:\n  docs:\n    steps:\n      - run: make docs\n"})
        report = ci_job_coverage(self.root, self.base, upstream)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            sorted(report["unregistered_jobs"]),
            [".github/workflows/ci.yml#surprise", ".github/workflows/docs.yml#docs"],
        )
        trial = {(item["workflow"], item["job"]): item["runs"] for item in report["trial_commands"]}
        self.assertEqual(trial[(".github/workflows/ci.yml", "helpers")], ["python -m unittest discover -s tools"])
        self.assertIn((".github/workflows/ci.yml", "surprise"), trial)

    def test_registered_additions_pass_and_release_changes_are_informational(self) -> None:
        text = WORKFLOW.replace("  test:\n", "  helpers:\n    steps:\n      - run: python -m unittest discover -s tools\n  test:\n")
        upstream = self.upstream(
            text,
            extra={".github/workflows/release.yml": "jobs:\n  release:\n    steps:\n      - run: make release-all\n"},
        )
        report = ci_job_coverage(self.root, self.base, upstream)
        self.assertEqual(report["status"], "passed")
        coverage = {(item["workflow"].split("/")[-1], item["job"]): item["coverage"] for item in report["jobs"]}
        self.assertEqual(coverage[("ci.yml", "helpers")], "ci_only")
        self.assertEqual(coverage[("release.yml", "release")], "not_candidate")

    def test_fork_jobs_must_be_registered(self) -> None:
        write(self.root / ".github/workflows/ci.yml", WORKFLOW.replace("  test:\n", "  forkonly:\n    steps:\n      - run: make x\n  test:\n"))
        git(self.root, "commit", "-q", "-am", "fork 新增作业但未登记")
        registered = load_ci_job_coverage(self.root)
        self.assertEqual(fork_unregistered_jobs(self.root, registered), [".github/workflows/ci.yml#forkonly"])
        report = ci_job_coverage(self.root, self.base, self.base)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["fork_unregistered_jobs"], [".github/workflows/ci.yml#forkonly"])

    def test_registry_validation_fails_closed(self) -> None:
        bad_entries = {
            "unknown kind": {"candidate_ci": True, "jobs": {"x": {"coverage": "maybe"}}},
            "local without lanes": {"candidate_ci": True, "jobs": {"x": {"coverage": "local"}}},
            "unknown lane": {"candidate_ci": True, "jobs": {"x": {"coverage": "local", "lanes": ["nope"]}}},
            "ci_only without reason": {"candidate_ci": True, "jobs": {"x": {"coverage": "ci_only"}}},
            "not candidate without reason": {"candidate_ci": False},
        }
        for label, entry in bad_entries.items():
            with self.subTest(label=label):
                self.write_registry({".github/workflows/ci.yml": entry})
                with self.assertRaises(UpstreamMergeError):
                    load_ci_job_coverage(self.root)
                self.assertEqual(ci_job_coverage(self.root, self.base, self.base)["status"], "failed")


class RepositoryRegistryTest(unittest.TestCase):
    """真实仓库：登记表覆盖 fork 当前全部候选 CI 作业；v0.2.4→v0.2.10 列出 release-helpers。"""

    def test_registry_covers_current_fork_workflows(self) -> None:
        registered = load_ci_job_coverage(SOURCE_ROOT)
        self.assertEqual(fork_unregistered_jobs(SOURCE_ROOT, registered), [])

    def test_v024_to_v0210_lists_release_helpers(self) -> None:
        commits = {}
        for tag in ("v0.2.4", "v0.2.10"):
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
                cwd=SOURCE_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(f"本地没有上游 tag {tag}")
            commits[tag] = completed.stdout.strip()
        report = ci_job_coverage(SOURCE_ROOT, commits["v0.2.4"], commits["v0.2.10"])
        helpers = [
            item
            for item in report["jobs"]
            if item["workflow"] == ".github/workflows/backend-ci.yml" and item["job"] == "release-helpers"
        ]
        self.assertEqual(len(helpers), 1)
        self.assertEqual((helpers[0]["change"], helpers[0]["coverage"]), ("added", "ci_only"))
        trial = {item["job"]: item["runs"] for item in report["trial_commands"]}
        self.assertIn("python -m unittest discover -s .github/release-tools -p 'test_release_matrix.py'", trial["release-helpers"])


if __name__ == "__main__":
    unittest.main()
