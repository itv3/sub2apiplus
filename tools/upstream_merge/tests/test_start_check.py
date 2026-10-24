"""开工检查：主干 HEAD 的功能 CI 结果必须绑定当前 HEAD 且全绿；状态缺失或仍在运行都算未通过。

用本地裸仓库充当 origin、假的 GitHub Actions 接口，不联网；基线工具链用假探测命令。
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.upstream_merge import __main__ as cli
from tools.upstream_merge.canonical import validate_identity
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.start_check import ACCEPTED_CI_EVENTS, start_check
from tools.upstream_merge.tests.test_toolchain import GO_MOD, GOOD_WORKFLOW, fake_probe
from tools.upstream_merge.workflow import CI_REQUIRED_JOBS, CI_WORKFLOW_PATH

SLUG = "itv3/sub2apiplus"
ENVIRON = {"PATH": "/usr/bin", "UPSTREAM_GATE_GOLANGCI_LINT": "/opt/pinned/golangci-lint"}


def git(root: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {argv!r} 失败：{completed.stderr or completed.stdout}")
    return completed.stdout.strip()


class FakeActionsApi:
    """GitHub Actions 只读接口替身：按提交返回预置的运行列表与作业列表，并记录调用。"""

    def __init__(self, runs: list[dict], *, jobs: list[dict] | None = None, error: str | None = None) -> None:
        self.runs = runs
        self.job_list = jobs
        self.error = error
        self.calls: list[tuple] = []

    def runs_for_commit(self, repository: str, head_sha: str) -> list[dict]:
        self.calls.append(("runs", repository, head_sha))
        if self.error:
            raise UpstreamMergeError(self.error)
        return [item for item in self.runs if item.get("head_sha") == head_sha]

    def jobs(self, repository: str, run_id: int) -> list[dict]:
        self.calls.append(("jobs", repository, run_id))
        if self.job_list is not None:
            return self.job_list
        return all_green_jobs()


def all_green_jobs(*, failed: str | None = None, dropped: str | None = None) -> list[dict]:
    return [
        {
            "name": name,
            "id": 1000 + index,
            "status": "completed",
            "conclusion": "failure" if name == failed else "success",
            "started_at": "2026-10-25T01:00:00Z",
            "completed_at": "2026-10-25T01:20:00Z",
        }
        for index, name in enumerate(CI_REQUIRED_JOBS, start=1)
        if name != dropped
    ]


def run_document(head: str, **overrides) -> dict:
    document = {
        "id": 101,
        "run_attempt": 1,
        "path": CI_WORKFLOW_PATH,
        "event": "push",
        "head_branch": "main",
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-25T00:50:00Z",
        "updated_at": "2026-10-25T01:21:00Z",
    }
    document.update(overrides)
    return document


class StartCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_root = Path(temporary.name)
        self.origin = self.temp_root / "origin.git"
        self.root = self.temp_root / "repository"
        git(self.temp_root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Start Check Test")
        git(self.root, "config", "user.email", "start-check@example.invalid")
        git(self.root, "config", "commit.gpgsign", "false")
        for relative, content in (
            ("backend/go.mod", GO_MOD),
            (".github/workflows/backend-ci.yml", GOOD_WORKFLOW),
            ("README.md", "主干\n"),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "主干")
        git(self.root, "remote", "add", "origin", str(self.origin))
        git(self.root, "push", "-q", "origin", "main")
        self.head = git(self.root, "rev-parse", "HEAD")
        self.fixed_now = datetime(2026, 10, 25, 2, 0, 0, tzinfo=timezone.utc)

    def check(self, api: FakeActionsApi, **kwargs) -> dict:
        kwargs.setdefault("probe", fake_probe())
        kwargs.setdefault("environ", ENVIRON)
        kwargs.setdefault("repository_slug", SLUG)
        kwargs.setdefault("now", lambda: self.fixed_now)
        return start_check(self.root, api=api, **kwargs)

    def green_api(self, **run_overrides) -> FakeActionsApi:
        return FakeActionsApi([run_document(self.head, **run_overrides)])

    # ---- 通过 ----
    def test_green_ci_bound_to_head_is_ready_and_recorded(self) -> None:
        api = self.green_api()
        report = self.check(api)
        self.assertEqual(report["result"], "ready", report["blockers"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["head"]["commit"], self.head)
        self.assertEqual(report["head"]["tree"], git(self.root, "rev-parse", "HEAD^{tree}"))
        self.assertTrue(report["head"]["clean"] and report["head"]["origin_matches"])
        run = report["ci"]["run"]
        self.assertEqual((run["id"], run["run_attempt"], run["event"], run["head_sha"]), (101, 1, "push", self.head))
        self.assertEqual((run["status"], run["conclusion"]), ("completed", "success"))
        self.assertTrue(report["ci"]["bound_to_head"] and report["ci"]["all_jobs_success"])
        self.assertEqual([job["name"] for job in report["ci"]["jobs"]], sorted(CI_REQUIRED_JOBS))
        self.assertTrue(all(job["conclusion"] == "success" and job["completed_at"] for job in report["ci"]["jobs"]))
        self.assertEqual(report["checked_at_utc"], "2026-10-25T02:00:00Z")
        self.assertEqual(report["toolchain"]["role"], "baseline")
        self.assertEqual(report["toolchain"]["result"], "passed")
        self.assertTrue(report["non_authoritative"])
        validate_identity(report, "start-check 报告")
        self.assertEqual(api.calls[0], ("runs", SLUG, self.head))
        self.assertEqual(api.calls[1], ("jobs", SLUG, 101))

    def test_workflow_dispatch_run_is_accepted_as_the_head_result(self) -> None:
        # 发版回写 VERSION 的提交由 GITHUB_TOKEN 推送，不触发 push；release 工作流随后显式调起一次 CI。
        report = self.check(self.green_api(event="workflow_dispatch"))
        self.assertEqual(report["result"], "ready", report["blockers"])
        self.assertEqual(report["ci"]["run"]["event"], "workflow_dispatch")
        self.assertEqual(ACCEPTED_CI_EVENTS, ("push", "workflow_dispatch"))

    def test_rerun_of_a_failed_run_counts_by_the_latest_run(self) -> None:
        older_failed = run_document(self.head, id=100, conclusion="failure", created_at="2026-10-25T00:00:00Z")
        newer_ok = run_document(self.head, id=101, created_at="2026-10-25T00:50:00Z")
        self.assertEqual(self.check(FakeActionsApi([older_failed, newer_ok]))["result"], "ready")
        # 反过来，旧的绿不能掩盖新的红
        older_ok = run_document(self.head, id=100, created_at="2026-10-25T00:00:00Z")
        newer_failed = run_document(self.head, id=101, conclusion="failure", created_at="2026-10-25T00:50:00Z")
        report = self.check(FakeActionsApi([older_ok, newer_failed]))
        self.assertEqual(report["result"], "blocked")
        self.assertEqual(report["ci"]["run"]["id"], 101)

    def test_security_scan_is_informational_only(self) -> None:
        security = run_document(
            self.head, id=202, path=".github/workflows/security-scan.yml", conclusion="failure", created_at="2026-10-25T00:51:00Z"
        )
        report = self.check(FakeActionsApi([run_document(self.head), security]))
        self.assertEqual(report["result"], "ready", report["blockers"])
        self.assertEqual(report["security_scan"]["run"]["id"], 202)
        self.assertEqual(report["security_scan"]["run"]["conclusion"], "failure")
        self.assertTrue(report["security_scan"]["informational"])

    # ---- 未通过：CI ----
    def test_missing_run_is_blocked_not_assumed_green(self) -> None:
        for runs in ([], [run_document("0" * 40)], [run_document(self.head, event="pull_request")], [run_document(self.head, path=".github/workflows/other.yml")]):
            with self.subTest(runs=len(runs)):
                report = self.check(FakeActionsApi(runs))
                self.assertEqual(report["result"], "blocked")
                self.assertIsNone(report["ci"]["run"])
                self.assertTrue(any("状态缺失不算通过" in item for item in report["blockers"]), report["blockers"])

    def test_run_still_in_progress_is_blocked(self) -> None:
        for status in ("in_progress", "queued", "waiting"):
            with self.subTest(status=status):
                report = self.check(self.green_api(status=status, conclusion=None))
                self.assertEqual(report["result"], "blocked")
                self.assertTrue(any("尚未结束" in item and status in item for item in report["blockers"]), report["blockers"])
                self.assertEqual(report["ci"]["jobs"], [])

    def test_failed_or_cancelled_run_is_blocked(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", None):
            with self.subTest(conclusion=conclusion):
                report = self.check(self.green_api(conclusion=conclusion))
                self.assertEqual(report["result"], "blocked")
                self.assertTrue(any("没有成功" in item for item in report["blockers"]), report["blockers"])

    def test_green_run_with_a_failed_or_missing_job_is_blocked(self) -> None:
        failed = self.check(FakeActionsApi([run_document(self.head)], jobs=all_green_jobs(failed="frontend")))
        self.assertEqual(failed["result"], "blocked")
        self.assertTrue(any("未成功的作业：['frontend']" in item for item in failed["blockers"]), failed["blockers"])
        self.assertFalse(failed["ci"]["all_jobs_success"])
        missing = self.check(FakeActionsApi([run_document(self.head)], jobs=all_green_jobs(dropped="golangci-lint")))
        self.assertEqual(missing["result"], "blocked")
        self.assertEqual(missing["ci"]["required_jobs_missing"], ["golangci-lint"])
        self.assertTrue(any("缺少必需作业" in item for item in missing["blockers"]))

    def test_api_failure_is_blocked_with_the_reason(self) -> None:
        report = self.check(FakeActionsApi([], error="gh api 失败：HTTP 502"))
        self.assertEqual(report["result"], "blocked")
        self.assertTrue(any("查询 HEAD 的 CI 结果失败：gh api 失败：HTTP 502" in item for item in report["blockers"]))

    def test_result_of_another_commit_is_never_bound_to_head(self) -> None:
        # 接口替身按提交过滤；再手工塞一个 head_sha 不符的条目也不会被采信。
        stray = run_document("f" * 40)
        api = FakeActionsApi([stray])
        api.runs_for_commit = lambda repository, head_sha: [stray]  # type: ignore[method-assign]
        report = self.check(api)
        self.assertEqual(report["result"], "blocked")
        self.assertIsNone(report["ci"]["run"])

    # ---- 未通过：仓库状态 ----
    def test_head_ahead_of_origin_is_blocked_without_querying_ci(self) -> None:
        (self.root / "README.md").write_text("未推送\n", encoding="utf-8")
        git(self.root, "commit", "-q", "-am", "本地提交")
        api = self.green_api()
        report = self.check(api)
        self.assertEqual(report["result"], "blocked")
        self.assertFalse(report["head"]["origin_matches"])
        self.assertTrue(any("本地领先，尚未推送" in item for item in report["blockers"]), report["blockers"])
        self.assertEqual(api.calls, [])
        self.assertEqual(report["ci"]["status"], "skipped")

    def test_head_behind_origin_is_blocked(self) -> None:
        other = self.temp_root / "other"
        git(self.temp_root, "clone", "-q", str(self.origin), str(other))
        git(other, "config", "user.name", "Other")
        git(other, "config", "user.email", "other@example.invalid")
        (other / "README.md").write_text("别处推送\n", encoding="utf-8")
        git(other, "commit", "-q", "-am", "别处提交")
        git(other, "push", "-q", "origin", "main")
        api = self.green_api()
        report = self.check(api)
        self.assertEqual(report["result"], "blocked")
        self.assertTrue(any("不等于 origin" in item for item in report["blockers"]), report["blockers"])
        self.assertEqual(api.calls, [])

    def test_dirty_worktree_and_wrong_branch_are_blocked(self) -> None:
        (self.root / "untracked.txt").write_text("脏\n", encoding="utf-8")
        report = self.check(self.green_api())
        self.assertEqual(report["result"], "blocked")
        self.assertTrue(any("不是干净状态" in item for item in report["blockers"]))
        (self.root / "untracked.txt").unlink()
        git(self.root, "checkout", "-q", "-b", "feature")
        report = self.check(self.green_api())
        self.assertEqual(report["result"], "blocked")
        self.assertTrue(any("不在受维护分支 refs/heads/main（实际 refs/heads/feature）" in item for item in report["blockers"]))
        git(self.root, "checkout", "-q", "--detach")
        report = self.check(self.green_api())
        self.assertTrue(any("游离 HEAD" in item for item in report["blockers"]))

    def test_unreachable_remote_is_blocked(self) -> None:
        git(self.root, "remote", "set-url", "origin", str(self.temp_root / "missing.git"))
        api = self.green_api()
        report = self.check(api)
        self.assertEqual(report["result"], "blocked")
        self.assertTrue(any("无法确认 HEAD 是否已在 origin 上" in item for item in report["blockers"]))
        self.assertEqual(api.calls, [])

    # ---- 未通过：基线工具链 ----
    def test_baseline_toolchain_mismatch_blocks_even_with_green_ci(self) -> None:
        report = self.check(
            self.green_api(),
            probe=fake_probe(lint="golangci-lint has version 2.13.2 built with go1.27.0 from abc on 2026-01-01"),
        )
        self.assertEqual(report["result"], "blocked")
        self.assertEqual(report["ci"]["status"], "passed")
        self.assertEqual(report["toolchain"]["result"], "blocked")
        self.assertTrue(any("基线工具链核对未通过" in item and "2.13.2" in item for item in report["blockers"]), report["blockers"])

    # ---- 报告文件 ----
    def test_report_is_written_once_outside_the_repository_and_never_overwritten(self) -> None:
        output = self.temp_root / "reports" / "start-check.json"
        output.parent.mkdir(mode=0o700)
        report = self.check(self.green_api(), output=output)
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(written["identity_sha256"], report["identity_sha256"])
        self.assertEqual(written["head"]["commit"], self.head)
        with self.assertRaisesRegex(UpstreamMergeError, "禁止覆盖"):
            self.check(self.green_api(), output=output)
        inside = self.root / "start-check.json"
        with self.assertRaisesRegex(UpstreamMergeError, "必须位于主仓库之外"):
            self.check(self.green_api(), output=inside)
        self.assertFalse(inside.exists())

    def test_blocked_report_is_also_written_for_the_record(self) -> None:
        output = self.temp_root / "blocked.json"
        report = self.check(FakeActionsApi([]), output=output)
        self.assertEqual(report["result"], "blocked")
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["result"], "blocked")


class CommandVerdictTest(unittest.TestCase):
    """命令行层：blocked 结论退出码为 5 且结果照常输出，ready／passed 为 0。"""

    def invoke(self, argv: list[str], patches: dict) -> tuple[int, dict]:
        stdout = io.BytesIO()
        wrapper = io.TextIOWrapper(stdout, encoding="utf-8")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "stdout", wrapper))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.object(cli, "timing_ledger_path", return_value=(None, None)))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(cli, name, value))
            code = cli.main(argv)
        return code, json.loads(stdout.getvalue().decode("utf-8"))

    def test_start_check_exit_status_follows_the_verdict(self) -> None:
        repository = str(Path(__file__).resolve().parents[3])
        blocked = {"result": "blocked", "blockers": ["CI 仍在运行"]}
        code, output = self.invoke(["start-check", "--repository", repository], {"start_check": lambda *a, **k: blocked})
        self.assertEqual((code, output["result"]), (5, "blocked"))
        code, output = self.invoke(["start-check", "--repository", repository], {"start_check": lambda *a, **k: {"result": "ready"}})
        self.assertEqual((code, output["result"]), (0, "ready"))

    def test_toolchain_check_exit_status_follows_the_verdict(self) -> None:
        repository = str(Path(__file__).resolve().parents[3])
        seen: list[tuple] = []

        def fake(root, role=None):
            seen.append((root, role))
            return {"result": "blocked", "mismatches": [{"item": "go.version"}]}

        code, output = self.invoke(["toolchain-check", "--repository", repository, "--role", "baseline"], {"check_toolchain": fake})
        self.assertEqual((code, output["result"]), (5, "blocked"))
        self.assertEqual(seen, [(Path(repository), "baseline")])
        code, _output = self.invoke(["toolchain-check", "--repository", repository], {"check_toolchain": lambda root, role=None: {"result": "passed"}})
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
