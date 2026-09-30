"""U-4 本机未执行项（integration）由同一候选提交的 CI 证据补齐：只用合成 Git 图与假 Actions 接口。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.canonical import bind_identity, write_json_once
from tools.upstream_merge.contracts import LoadedPlan
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.tests.test_workflow import SyntheticRepository, build_verification_plan, run
from tools.upstream_merge.workflow import (
    CANDIDATE_DISPOSITION_INPUT_SCHEMA,
    CI_REQUIRED_JOBS,
    CI_WORKFLOW_PATH,
    _load_candidate_disposition,
    delete_ci_branch,
    import_ci_evidence,
    load_verification_receipt,
    push_candidate_for_ci,
    run_verification_gates,
    seal_candidate_disposition,
)

INTEGRATION = "go-tests/go-test-integration"

# 假编排脚本：按场景把逐步结果写进 UPSTREAM_GATE_STATUS_FILE，并用相应退出码结束。
FAKE_RUNNER = r'''
import json, os, sys
scenario = sys.argv[1]
def step(lane, name, status, exit_code):
    return {"lane": lane, "step": name, "status": status, "exit_code": exit_code, "duration_seconds": 0.1,
            "reason": "本机 Docker 不可用" if status == "not_executed" else None}
steps = [step("go-tests", "go-test-default", "passed", 0), step("go-tests", "go-test-unit", "passed", 0)]
exit_code = 0
if scenario == "awaiting":
    steps.append(step("go-tests", "go-test-integration", "not_executed", None))
elif scenario == "passed":
    steps.append(step("go-tests", "go-test-integration", "passed", 0))
elif scenario == "failed":
    steps[1] = step("go-tests", "go-test-unit", "failed", 1)
    steps.append(step("go-tests", "go-test-integration", "not_executed", None))
    exit_code = 1
elif scenario == "uncoverable":
    steps.append(step("frontend", "frontend-lint", "not_executed", None))
elif scenario == "contradiction":
    steps.append(step("go-tests", "go-test-integration", "passed", 0))
    exit_code = 1
failed = [f"{s['lane']}/{s['step']}" for s in steps if s["status"] == "failed"]
skipped = [f"{s['lane']}/{s['step']}" for s in steps if s["status"] == "not_executed"]
result = "failed" if failed else ("awaiting_ci" if skipped else "passed")
document = {"schema_version": "official-egress-upstream-gate-runner-status/v1", "mode": "full", "jobs": 1,
            "elapsed_seconds": 0.3, "steps": steps, "failed": failed, "not_executed": skipped, "result": result}
with open(os.environ["UPSTREAM_GATE_STATUS_FILE"], "x", encoding="utf-8") as handle:
    json.dump(document, handle)
sys.exit(exit_code)
'''

INTEGRATION_LOG = (
    "2026-09-30T00:04:49.0996929Z ##[group]Run make test-unit\n"
    "2026-09-30T00:04:49.1173209Z go test -tags=unit ./...\n"
    "2026-09-30T00:04:52.0000000Z ok  \tgithub.com/Wei-Shaw/sub2api/internal/repository\t5.193s\n"
    "2026-09-30T00:05:00.0000000Z ##[group]Run make test-integration\n"
    "2026-09-30T00:05:00.1000000Z go test -tags=integration ./...\n"
    "2026-09-30T00:07:50.9226785Z ok  \tgithub.com/Wei-Shaw/sub2api/internal/repository\t25.361s\n"
)


class FakeActionsApi:
    """最小的 GitHub Actions 只读接口替身；参数控制各类不符情形。"""

    def __init__(self, head_sha: str, *, run: dict | None = None, drop_job: str | None = None,
                 failed_job: str | None = None, log: str = INTEGRATION_LOG) -> None:
        self.head_sha = head_sha
        self.run_overrides = run or {}
        self.drop_job = drop_job
        self.failed_job = failed_job
        self.log = log

    def find_run(self, repository: str, head_sha: str) -> int | None:
        return 101 if head_sha == self.head_sha else None

    def run(self, repository: str, run_id: int) -> dict:
        document = {
            "id": run_id,
            "head_sha": self.head_sha,
            "path": CI_WORKFLOW_PATH,
            "status": "completed",
            "conclusion": "success",
            "event": "push",
            "head_branch": "upstream-merge/synthetic",
            "run_attempt": 1,
        }
        document.update(self.run_overrides)
        return document

    def jobs(self, repository: str, run_id: int) -> list[dict]:
        jobs = []
        for index, name in enumerate(CI_REQUIRED_JOBS, start=1):
            if name == self.drop_job:
                continue
            jobs.append({"name": name, "id": 1000 + index, "conclusion": "failure" if name == self.failed_job else "success"})
        return jobs

    def job_log(self, repository: str, job_id: int) -> str:
        return self.log


class CiEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_root = Path(temporary.name)
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        self.addCleanup(self.fixture.cleanup_worktree)
        self.runner = self.temp_root / "fake_runner.py"
        self.runner.write_text(FAKE_RUNNER, encoding="utf-8")
        marker = self.temp_root / "never-created.marker"
        self.base_plan = build_verification_plan(self.fixture, marker)

    def plan(self, scenario: str) -> LoadedPlan:
        """12 类逻辑门禁合成一个执行组，统一执行假编排脚本。"""

        document = dict(self.base_plan.document)
        document["gates"] = [
            {**gate, "execution_group": "full-regression", "argv": ["python3", str(self.runner), scenario]}
            for gate in self.base_plan.document["gates"]
        ]
        return LoadedPlan(
            document=document,
            path=self.base_plan.path,
            repository_root=self.base_plan.repository_root,
            evidence_root=self.base_plan.evidence_root,
            worktree=self.base_plan.worktree,
        )

    def receipt_path(self, attempt: str) -> Path:
        return self.fixture.evidence / "u4/attempts" / attempt / "receipt.json"

    def api(self, **kwargs) -> FakeActionsApi:
        return FakeActionsApi(self.fixture.fork, **kwargs)

    def test_integration_not_executed_makes_attempt_awaiting_ci(self) -> None:
        plan = self.plan("awaiting")
        receipt = run_verification_gates(plan, "attempt-001")
        self.assertEqual(receipt["result"], "awaiting_ci")
        self.assertEqual(receipt["not_executed_checks"], [INTEGRATION])
        self.assertIsNone(receipt["ci_evidence"])
        self.assertTrue(all(gate["runner_status"] is not None for gate in receipt["gates"]))
        load_verification_receipt(plan, self.receipt_path("attempt-001"), require_passed=False)
        with self.assertRaisesRegex(UpstreamMergeError, "gates-import-ci"):
            load_verification_receipt(plan, self.receipt_path("attempt-001"), require_passed=True)

    def test_ci_evidence_completes_awaiting_attempt(self) -> None:
        plan = self.plan("awaiting")
        run_verification_gates(plan, "attempt-001")
        receipt = import_ci_evidence(plan, "attempt-002", "attempt-001", repository_slug="itv3/sub2apiplus", api=self.api())
        self.assertEqual(receipt["result"], "passed")
        self.assertEqual(receipt["ci_evidence"]["covers"], [INTEGRATION])
        self.assertEqual(receipt["ci_evidence"]["run_id"], 101)
        self.assertEqual(receipt["executed_gate_count"], 0)
        self.assertTrue(all(gate["execution_status"] == "attempt_reused" for gate in receipt["gates"]))
        loaded = load_verification_receipt(plan, self.receipt_path("attempt-002"), require_passed=True)
        self.assertEqual(loaded["result"], "passed")

    def test_local_failure_is_not_overridden_by_green_ci(self) -> None:
        plan = self.plan("failed")
        receipt = run_verification_gates(plan, "attempt-001")
        self.assertEqual(receipt["result"], "blocked")
        with self.assertRaisesRegex(UpstreamMergeError, "不能覆盖本机失败"):
            import_ci_evidence(plan, "attempt-002", "attempt-001", repository_slug="itv3/sub2apiplus", api=self.api())
        self.assertFalse(self.receipt_path("attempt-002").parent.exists())

    def test_mismatched_or_incomplete_ci_is_rejected_before_writing(self) -> None:
        plan = self.plan("awaiting")
        run_verification_gates(plan, "attempt-001")
        cases = {
            "候选提交": self.api(run={"head_sha": "0" * 40}),
            "缺少必需作业": self.api(drop_job="test"),
            "未成功的作业": self.api(failed_job="capture-tools (3)"),
            "未成功完成": self.api(run={"status": "in_progress", "conclusion": None}),
            "backend-ci.yml": self.api(run={"path": ".github/workflows/release.yml"}),
            "integration 真实执行": self.api(log=INTEGRATION_LOG.split("##[group]Run make test-integration")[0]),
        }
        for index, (message, api) in enumerate(cases.items(), start=2):
            attempt = f"attempt-{index:03d}"
            with self.subTest(message=message):
                with self.assertRaises(UpstreamMergeError) as caught:
                    import_ci_evidence(plan, attempt, "attempt-001", repository_slug="itv3/sub2apiplus", api=api)
                self.assertIn(message if message != "缺少必需作业" else "test", str(caught.exception))
                self.assertFalse(self.receipt_path(attempt).parent.exists())

    def test_passed_attempt_needs_no_import(self) -> None:
        plan = self.plan("passed")
        self.assertEqual(run_verification_gates(plan, "attempt-001")["result"], "passed")
        with self.assertRaisesRegex(UpstreamMergeError, "无需导入"):
            import_ci_evidence(plan, "attempt-002", "attempt-001", repository_slug="itv3/sub2apiplus", api=self.api())

    def test_tampered_ci_log_breaks_receipt(self) -> None:
        plan = self.plan("awaiting")
        run_verification_gates(plan, "attempt-001")
        import_ci_evidence(plan, "attempt-002", "attempt-001", repository_slug="itv3/sub2apiplus", api=self.api())
        log_path = self.receipt_path("attempt-002").parent / "ci" / "test-job.log"
        log_path.chmod(0o600)
        log_path.write_text(INTEGRATION_LOG.replace("25.361s", "25.362s"), encoding="utf-8")
        with self.assertRaises(UpstreamMergeError):
            load_verification_receipt(plan, self.receipt_path("attempt-002"), require_passed=True)

    def test_not_executed_outside_ci_coverage_is_rejected(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "CI 无法补齐"):
            run_verification_gates(self.plan("uncoverable"), "attempt-001")

    def test_status_file_contradicting_exit_code_is_rejected(self) -> None:
        with self.assertRaisesRegex(UpstreamMergeError, "退出码矛盾"):
            run_verification_gates(self.plan("contradiction"), "attempt-001")

    def test_u4_awaiting_ci_import_then_u5_disposition_chain(self) -> None:
        """合成链：U-4 本机未执行 → U-5 拒绝 → 导入同一提交的 CI 证据 → U-5 封存成功。"""

        plan = self.plan("awaiting")
        run_verification_gates(plan, "attempt-001")
        original_business = self.temp_root / "original-business-receipt.json"
        write_json_once(original_business, {"schema_version": "synthetic-original-business/v1", "result": "passed"})
        disposition_input = self.temp_root / "candidate-disposition-input.json"
        write_json_once(
            disposition_input,
            bind_identity(
                {
                    "schema_version": CANDIDATE_DISPOSITION_INPUT_SCHEMA,
                    "plan_id": plan.plan_id,
                    "plan_identity_sha256": plan.identity,
                    "source_tree": run(self.fixture.root, "git", "rev-parse", f"{self.fixture.fork}^{{tree}}"),
                    "purpose": "validation_only",
                    "clients": {
                        client: {
                            "mode": "none",
                            "campaign_path": None,
                            "candidate_path": None,
                            "approval_path": None,
                            "acceptance_path": None,
                        }
                        for client in ("claude", "codex")
                    },
                    "shared_contract_receipt_path": None,
                    "original_business_receipt_path": str(original_business),
                }
            ),
        )
        with self.assertRaisesRegex(UpstreamMergeError, "gates-import-ci"):
            seal_candidate_disposition(plan, disposition_input, self.receipt_path("attempt-001"))
        import_ci_evidence(plan, "attempt-002", "attempt-001", repository_slug="itv3/sub2apiplus", api=self.api())
        sealed = seal_candidate_disposition(plan, disposition_input, self.receipt_path("attempt-002"))
        self.assertEqual(sealed["result"], "closed")
        self.assertEqual(sealed["verification_receipt"]["path"], "u4/attempts/attempt-002/receipt.json")
        self.assertEqual(_load_candidate_disposition(plan)["result"], "closed")

    def test_ci_branch_push_and_cleanup_use_plan_scoped_branch(self) -> None:
        remote = self.temp_root / "origin.git"
        run(self.temp_root, "git", "init", "-q", "--bare", str(remote))
        run(self.fixture.root, "git", "remote", "add", "origin", str(remote))
        plan = self.plan("passed")
        pushed = push_candidate_for_ci(plan)
        branch = f"upstream-merge/{plan.plan_id}"
        self.assertEqual((pushed["branch"], pushed["commit"]), (branch, self.fixture.fork))
        listed = run(self.fixture.root, "git", "ls-remote", "origin", f"refs/heads/{branch}")
        self.assertTrue(listed.startswith(self.fixture.fork))
        self.assertEqual(delete_ci_branch(plan)["result"], "deleted")
        self.assertEqual(delete_ci_branch(plan)["result"], "absent")
        self.assertEqual(run(self.fixture.root, "git", "ls-remote", "origin", "refs/heads/main"), "")


if __name__ == "__main__":
    unittest.main()
