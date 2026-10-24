"""U-4 启动前检查、失败项处置与结论读取：工具版本不符、候选密钥命中、上一轮失败未处置三种情况都必须
在任何执行组启动之前退出并列出原因；处置的定向复验由工具真实执行，非 0 不成立。

只用合成 Git 图、假编排脚本与假探测命令，不执行真实门禁，不依赖本机安装的 go／golangci-lint。测试里的命中串
在运行时拼接，源码里不出现完整的凭据形态字面量。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import __main__ as cli
from tools.upstream_merge import gate_guard
from tools.upstream_merge.canonical import artifact_binding, bind_identity, write_json_once
from tools.upstream_merge.contracts import LoadedPlan, latest_stage_path, next_stage_path
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.gate_guard import (
    FAILURE_DISPOSITION_INPUT_SCHEMA,
    attempt_status,
    check_failure_disposition,
    failure_items,
    read_attempt_receipt,
    record_failure_disposition,
    run_gates_with_prechecks,
    u4_result_line,
)
from tools.upstream_merge.gitops import commit_tree, rev_parse
from tools.upstream_merge.tests.test_preflight_secret_scan import secret_gate, template_gates
from tools.upstream_merge.tests.test_toolchain import GO_MOD, GOOD_WORKFLOW, fake_probe
from tools.upstream_merge.tests.test_workflow import SyntheticRepository, build_verification_plan, run
from tools.upstream_merge.workflow import load_verification_receipt, run_verification_gates

SOURCE_ROOT = Path(__file__).resolve().parents[3]

# 运行时拼接的 AWS 访问密钥形态串，命中模板扫描里的 AKIA[0-9A-Z]{16}，摘要不在精确白名单内。
HIT = "AKIA" + "Q" * 16

# 假的 full-regression 执行组：场景从文件里读（测试可在两次运行之间改写），按场景写出逐步状态文件。
GUARD_RUNNER = r'''
import json, os, sys
scenario = open(sys.argv[1], encoding="utf-8").read().strip()
log = os.environ.get("GUARD_ORDER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("full-regression\n")
def step(lane, name, status, exit_code):
    return {"lane": lane, "step": name, "status": status, "exit_code": exit_code, "duration_seconds": 0.1,
            "reason": "本机 Docker 不可用" if status == "not_executed" else None}
steps = [step("egress-spec", "check-egress-spec", "passed", 0),
         step("lint", "golangci-lint-default", "passed", 0),
         step("lint", "golangci-lint-unit", "passed", 0),
         step("go-tests", "go-test-default", "passed", 0),
         step("go-tests", "go-test-integration", "passed", 0)]
exit_code = 0
if scenario == "awaiting":
    steps[4] = step("go-tests", "go-test-integration", "not_executed", None)
elif scenario == "failed_steps":
    steps[0] = step("egress-spec", "check-egress-spec", "failed", 1)
    steps[2] = step("lint", "golangci-lint-unit", "failed", 1)
    steps[4] = step("go-tests", "go-test-integration", "not_executed", None)
    exit_code = 1
elif scenario == "crash":
    sys.exit(3)
failed = [f"{s['lane']}/{s['step']}" for s in steps if s["status"] == "failed"]
skipped = [f"{s['lane']}/{s['step']}" for s in steps if s["status"] == "not_executed"]
result = "failed" if failed else ("awaiting_ci" if skipped else "passed")
document = {"schema_version": "official-egress-upstream-gate-runner-status/v1", "mode": "full", "jobs": 1,
            "elapsed_seconds": 0.3, "steps": steps, "failed": failed, "not_executed": skipped, "result": result}
with open(os.environ["UPSTREAM_GATE_STATUS_FILE"], "x", encoding="utf-8") as handle:
    json.dump(document, handle)
sys.exit(exit_code)
'''

# 记录执行顺序的便宜执行组（inventory-closure，以及顺序用例里换掉的 secret-scan）。
ORDER_SCRIPT = r'''
import os, sys
log = os.environ.get("GUARD_ORDER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(sys.argv[1] + "\n")
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
'''


class GuardFixture(unittest.TestCase):
    """合成仓库 + 12 类门禁分成 secret-scan、inventory-closure、full-regression 三个执行组。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_root = Path(temporary.name)
        self.runner = self.temp_root / "guard_runner.py"
        self.runner.write_text(GUARD_RUNNER, encoding="utf-8")
        self.order_script = self.temp_root / "order_script.py"
        self.order_script.write_text(ORDER_SCRIPT, encoding="utf-8")
        self.scenario_file = self.temp_root / "scenario.txt"
        self.scenario_file.write_text("passed", encoding="utf-8")
        self.order_log = self.temp_root / "order.log"
        patcher = mock.patch.dict(os.environ, {"GUARD_ORDER_LOG": str(self.order_log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def scenario(self, name: str) -> None:
        self.scenario_file.write_text(name, encoding="utf-8")

    def make_fixture(self, extra_files: dict[str, str] | None = None) -> SyntheticRepository:
        files = {
            "backend/go.mod": GO_MOD,
            ".github/workflows/backend-ci.yml": GOOD_WORKFLOW,
            **(extra_files or {}),
        }
        self.fixture = SyntheticRepository(self.temp_root, conflict=False, extra_fork_files=files)
        self.addCleanup(self.fixture.cleanup_worktree)
        self.plan = self.build_plan()
        return self.fixture

    def build_plan(self, *, real_secret_scan: bool = True, inventory_exit: int = 0) -> LoadedPlan:
        # 前置制品只能写一次：第一次构造后缓存，之后只是换一组门禁定义。
        if getattr(self, "_base_plan", None) is None:
            self._base_plan = build_verification_plan(self.fixture, self.temp_root / "absent.marker")
        base = self._base_plan
        gates = []
        for gate in base.document["gates"]:
            category = gate["category"]
            if category == "secret_scan":
                group = "secret-scan"
                argv = (
                    secret_gate(template_gates())["argv"]
                    if real_secret_scan
                    else ["python3", str(self.order_script), "secret-scan"]
                )
            elif category == "inventory_closure":
                group = "inventory-closure"
                argv = ["python3", str(self.order_script), "inventory-closure", str(inventory_exit)]
            else:
                group = "full-regression"
                argv = ["python3", str(self.runner), str(self.scenario_file)]
            gates.append({**gate, "execution_group": group, "argv": argv})
        document = dict(base.document)
        document["gates"] = gates
        return LoadedPlan(
            document=document,
            path=base.path,
            repository_root=base.repository_root,
            evidence_root=base.evidence_root,
            worktree=base.worktree,
        )

    # ---- 公用动作 ----
    def attempt_dir(self, attempt: str) -> Path:
        return self.fixture.evidence / "u4/attempts" / attempt

    def good_probe(self):
        return fake_probe()

    def prechecked_run(self, attempt: str, **kwargs):
        kwargs.setdefault("probe", self.good_probe())
        kwargs.setdefault("environ", {"PATH": "/usr/bin", "UPSTREAM_GATE_GOLANGCI_LINT": "/opt/pinned/golangci-lint"})
        kwargs.setdefault("notify", lambda _message: None)
        return run_gates_with_prechecks(self.plan, attempt, **kwargs)

    def order(self) -> list[str]:
        return self.order_log.read_text(encoding="utf-8").split() if self.order_log.exists() else []

    def advance_candidate(self, files: dict[str, str] | None = None) -> str:
        """在合成仓库里提交一个修复，并追加下一轮 SourceCandidate（模拟 revision-advance 之后）。"""

        root = self.fixture.root
        for relative, content in (files or {"fix.txt": f"修复 {len(os.listdir(root))}\n"}).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        run(root, "git", "add", "--all")
        run(root, "git", "commit", "-m", "修复")
        commit = rev_parse(root, "HEAD^{commit}")
        previous_path = latest_stage_path(self.plan, "source_candidate")
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        number, path = next_stage_path(self.plan, "source_candidate")
        document = {key: value for key, value in previous.items() if key != "identity_sha256"}
        document.update(
            source_commit=commit,
            source_tree=commit_tree(root, commit),
            revision=number,
            predecessor=artifact_binding(self.fixture.evidence, previous_path),
        )
        write_json_once(path, bind_identity(document))
        return commit

    def write_input(self, entries: list[dict]) -> Path:
        self._input_count = getattr(self, "_input_count", 0) + 1
        path = self.temp_root / f"disposition-input-{self._input_count:03d}.json"
        write_json_once(path, {"schema_version": FAILURE_DISPOSITION_INPUT_SCHEMA, "items": entries})
        return path

    def passing_retest(self) -> dict:
        return {"argv": ["python3", "-c", "print('复验通过')"], "cwd": "."}


class OrderingAndPrecheckTest(GuardFixture):
    def test_cheap_groups_run_before_the_full_regression(self) -> None:
        self.make_fixture()
        self.plan = self.build_plan(real_secret_scan=False)
        receipt = run_verification_gates(self.plan, "attempt-001")
        self.assertEqual(self.order(), ["secret-scan", "inventory-closure", "full-regression"])
        # 仍然完整执行 12 类门禁，没有跳过。
        self.assertEqual(receipt["gate_count"], 12)
        self.assertEqual(receipt["skipped_gate_count"], 0)
        self.assertEqual(receipt["execution_group_count"], 3)
        self.assertEqual(receipt["result"], "passed")

    def test_unknown_groups_keep_alphabetical_order_between_cheap_and_full(self) -> None:
        from tools.upstream_merge.workflow import _ordered_group_names

        groups = {"full-regression": [], "zeta": [], "alpha": [], "secret-scan": [], "inventory-closure": []}
        self.assertEqual(
            _ordered_group_names(groups), ["secret-scan", "inventory-closure", "alpha", "zeta", "full-regression"]
        )
        self.assertEqual(_ordered_group_names({"b": [], "a": []}), ["a", "b"])

    def test_normal_path_forms_a_complete_u4_result_with_consistent_receipt_log_and_status(self) -> None:
        self.make_fixture()
        messages: list[str] = []
        receipt = self.prechecked_run("attempt-001", notify=messages.append)
        self.assertEqual(receipt["result"], "passed")
        self.assertEqual((receipt["gate_count"], receipt["skipped_gate_count"], receipt["failed_gate_ids"]), (12, 0, []))
        self.assertEqual(messages, [])
        loaded = load_verification_receipt(self.plan, self.attempt_dir("attempt-001") / "receipt.json", require_passed=True)
        self.assertEqual(loaded["identity_sha256"], receipt["identity_sha256"])
        status = attempt_status(self.plan, "attempt-001")
        self.assertEqual(status["result"], "passed")
        self.assertTrue(status["verified"])
        receipt_path = (self.attempt_dir("attempt-001") / "receipt.json").resolve()
        self.assertEqual(status["line"], f"U4-RESULT attempt=attempt-001 result=passed failed=- receipt={receipt_path}")
        self.assertEqual(self.order(), ["inventory-closure", "full-regression"])

    def test_three_blockers_exit_before_any_group_starts_and_list_every_reason(self) -> None:
        self.make_fixture({"src/leak.txt": f"token={HIT}\n"})
        self.scenario("failed_steps")
        real_plan = self.plan
        # 引擎本身不做启动前检查：先用假的密钥扫描造出一个只有两个失败步骤的上一轮，再换回真实门禁
        self.plan = self.build_plan(real_secret_scan=False)
        run_verification_gates(self.plan, "attempt-001")
        self.plan = real_plan
        self.order_log.unlink()
        before = sorted(path.name for path in (self.fixture.evidence / "u4/attempts").iterdir())
        with self.assertRaises(UpstreamMergeError) as caught:
            self.prechecked_run("attempt-002", probe=fake_probe(go="go1.27.2"))
        message = str(caught.exception)
        self.assertIn("U-4 启动前检查未通过（共 5 项", message)
        # 1. 上一轮失败未处置：逐项列出
        self.assertIn("full-regression/egress-spec/check-egress-spec 尚未处置", message)
        self.assertIn("full-regression/lint/golangci-lint-unit 尚未处置", message)
        # 2. 工具版本不符
        self.assertIn("候选工具链版本核对未通过", message)
        self.assertIn("期望 go1.27.3，本机 backend/ 下实际是 go1.27.2", message)
        # 3. 候选密钥命中：文件与规则
        self.assertIn("候选密钥扫描命中", message)
        self.assertIn("src/leak.txt", message)
        self.assertNotIn(HIT, message)
        # 没有创建 attempt，也没有任何执行组启动
        self.assertFalse(self.attempt_dir("attempt-002").exists())
        self.assertEqual(sorted(path.name for path in (self.fixture.evidence / "u4/attempts").iterdir()), before)
        self.assertEqual(self.order(), [])

    def test_toolchain_mismatch_alone_blocks_before_the_attempt_exists(self) -> None:
        self.make_fixture()
        with self.assertRaises(UpstreamMergeError) as caught:
            self.prechecked_run(
                "attempt-001",
                probe=fake_probe(lint="golangci-lint has version 2.13.2 built with go1.27.0 from abc on 2026-01-01"),
            )
        message = str(caught.exception)
        self.assertIn("共 1 项", message)
        self.assertIn("期望 golangci-lint v2.14.0", message)
        self.assertIn("实际是 2.13.2", message)
        self.assertIn("UPSTREAM_GATE_GOLANGCI_LINT", message)
        self.assertFalse(self.attempt_dir("attempt-001").exists())
        self.assertEqual(self.order(), [])

    def test_secret_hit_alone_blocks_before_the_attempt_exists(self) -> None:
        self.make_fixture({"config/settings.txt": f"key={HIT}\n"})
        with self.assertRaises(UpstreamMergeError) as caught:
            self.prechecked_run("attempt-001")
        message = str(caught.exception)
        self.assertIn("共 1 项", message)
        self.assertIn("候选密钥扫描命中", message)
        self.assertIn("config/settings.txt", message)
        self.assertIn("AKIA[0-9A-Z]{16}", message)
        self.assertFalse(self.attempt_dir("attempt-001").exists())
        self.assertEqual(self.order(), [])

    def test_secret_scan_uses_the_plan_gate_so_exact_digest_allowlist_applies(self) -> None:
        # 与正式验收同一份门禁（含摘要白名单）：把命中串的摘要加进该门禁的白名单后同样放行，证明没有另起一套规则。
        import hashlib

        self.make_fixture({"config/settings.txt": f"key={HIT}\n"})
        digest = hashlib.sha256(HIT.encode()).hexdigest()
        allowed = []
        for gate in self.plan.document["gates"]:
            if gate["category"] == "secret_scan":
                gate["argv"] = [
                    gate["argv"][0],
                    gate["argv"][1],
                    gate["argv"][2].replace("allow={", 'allow={"' + digest + '",', 1),
                    gate["argv"][3],
                ]
                allowed.append(gate["argv"][2])
        self.assertIn(digest, allowed[0])
        receipt = self.prechecked_run("attempt-001")
        self.assertEqual(receipt["result"], "passed")

    def test_undisposed_failure_blocks_and_names_each_failure_item(self) -> None:
        self.make_fixture()
        self.scenario("failed_steps")
        first = self.prechecked_run("attempt-001")
        self.assertEqual(first["result"], "blocked")
        self.order_log.unlink()
        with self.assertRaises(UpstreamMergeError) as caught:
            self.prechecked_run("attempt-002")
        message = str(caught.exception)
        self.assertIn("共 3 项", message)  # 两个失败步骤 + 如何处置的提示
        self.assertIn("上一轮 attempt attempt-001 的失败项 full-regression/egress-spec/check-egress-spec 尚未处置", message)
        self.assertIn("上一轮 attempt attempt-001 的失败项 full-regression/lint/golangci-lint-unit 尚未处置", message)
        self.assertIn("failure-disposition", message)
        self.assertFalse(self.attempt_dir("attempt-002").exists())
        self.assertEqual(self.order(), [])

    def test_partial_rerun_from_a_failed_attempt_is_gated_too(self) -> None:
        self.make_fixture()
        self.scenario("failed_steps")
        self.prechecked_run("attempt-001")
        with self.assertRaisesRegex(UpstreamMergeError, "尚未处置"):
            self.prechecked_run("attempt-002", from_attempt="attempt-001")
        self.assertFalse(self.attempt_dir("attempt-002").exists())

    def test_awaiting_ci_is_not_a_failure_and_needs_no_disposition(self) -> None:
        self.make_fixture()
        self.scenario("awaiting")
        first = self.prechecked_run("attempt-001")
        self.assertEqual(first["result"], "awaiting_ci")
        report = check_failure_disposition(self.plan)
        self.assertEqual((report["latest_result"], report["blockers"], report["failure_ids"]), ("awaiting_ci", [], []))
        status = attempt_status(self.plan, "attempt-001")
        self.assertEqual(status["result"], "awaiting_ci")
        self.assertEqual(status["not_executed_checks"], ["go-tests/go-test-integration"])
        self.assertTrue(status["line"].startswith("U4-RESULT attempt=attempt-001 result=awaiting_ci failed=- receipt="))
        # 继续走 ci-push／gates-import-ci 的路径不被启动前检查挡住：同一候选可再开新 attempt。
        self.scenario("passed")
        self.assertEqual(self.prechecked_run("attempt-002")["result"], "passed")

    def test_attempt_directory_without_receipt_only_warns(self) -> None:
        self.make_fixture()
        crashed = self.attempt_dir("attempt-000")
        crashed.mkdir(parents=True, mode=0o700)
        (crashed / "gate-00.stdout.txt").write_text("半截输出\n", encoding="utf-8")
        messages: list[str] = []
        receipt = self.prechecked_run("attempt-001", notify=messages.append)
        self.assertEqual(receipt["result"], "passed")
        self.assertEqual(len(messages), 1)
        self.assertIn("attempt-000", messages[0])
        self.assertIn("没有 receipt.json", messages[0])
        self.assertIn("不算已知失败", messages[0])
        status = attempt_status(self.plan, "attempt-000")
        self.assertEqual((status["result"], status["receipt"]), ("incomplete", None))

    def test_dirty_candidate_worktree_is_rejected_before_any_check(self) -> None:
        self.make_fixture()
        (self.fixture.root / "stray.txt").write_text("未跟踪\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
            self.prechecked_run("attempt-001")
        self.assertFalse(self.attempt_dir("attempt-001").exists())


class FailureDispositionTest(GuardFixture):
    def fail_first_attempt(self) -> dict:
        self.make_fixture()
        self.scenario("failed_steps")
        return self.prechecked_run("attempt-001")

    def entries(self, **overrides) -> list[dict]:
        base = {
            "egress": {
                "failure_id": "full-regression/egress-spec/check-egress-spec",
                "category": "environment",
                "cause": "本机 Go 补丁版本与主干声明不一致，冻结自检报错",
                "fix": "改用 GOTOOLCHAIN=go1.27.3 重跑",
                "retests": [self.passing_retest()],
            },
            "lint": {
                "failure_id": "full-regression/lint/golangci-lint-unit",
                "category": "environment",
                "cause": "lint 二进制版本过旧",
                "fix": "改用 v2.14.0",
                "retests": [self.passing_retest()],
            },
        }
        for key, patch in overrides.items():
            base[key] = {**base[key], **patch}
        return [base["egress"], base["lint"]]

    def record(self, entries: list[dict], **kwargs) -> dict:
        kwargs.setdefault("probe", self.good_probe())
        kwargs.setdefault("environ", {"PATH": "/usr/bin", "UPSTREAM_GATE_GOLANGCI_LINT": "/opt/pinned/golangci-lint"})
        return record_failure_disposition(self.plan, "attempt-001", self.write_input(entries), **kwargs)

    # ---- 失败项识别 ----
    def test_failure_items_refine_group_failures_to_runner_steps(self) -> None:
        self.fail_first_attempt()
        document = read_attempt_receipt(self.plan, self.attempt_dir("attempt-001") / "receipt.json")
        items = failure_items(self.plan, document)
        self.assertEqual(
            [item["id"] for item in items],
            ["full-regression/egress-spec/check-egress-spec", "full-regression/lint/golangci-lint-unit"],
        )
        self.assertEqual(len(items[0]["gate_ids"]), 10)  # full-regression 组内的 10 个逻辑门禁共享这次失败
        self.assertEqual({item["kind"] for item in items}, {"group_step"})

    def test_group_without_step_details_is_one_group_level_item(self) -> None:
        self.make_fixture()
        self.plan = self.build_plan(inventory_exit=1)
        self.scenario("crash")
        receipt = run_verification_gates(self.plan, "attempt-001")
        self.assertEqual(receipt["result"], "blocked")
        items = failure_items(self.plan, receipt)
        self.assertEqual([item["id"] for item in items], ["full-regression", "inventory-closure"])
        self.assertEqual({item["kind"] for item in items}, {"group"})

    def test_passed_and_awaiting_attempts_have_no_failure_items(self) -> None:
        self.make_fixture()
        receipt = run_verification_gates(self.plan, "attempt-001")
        self.assertEqual(failure_items(self.plan, receipt), [])

    # ---- 输入校验 ----
    def test_input_must_cover_exactly_the_failed_items(self) -> None:
        self.fail_first_attempt()
        with self.assertRaisesRegex(UpstreamMergeError, "缺失=\\['full-regression/lint/golangci-lint-unit'\\]"):
            self.record(self.entries()[:1])
        extra = self.entries() + [
            {"failure_id": "secret-scan", "cause": "x", "fix": "y", "retests": [self.passing_retest()]}
        ]
        with self.assertRaisesRegex(UpstreamMergeError, "不属于该 attempt=\\['secret-scan'\\]"):
            self.record(extra)
        duplicated = self.entries() + [self.entries()[0]]
        with self.assertRaisesRegex(UpstreamMergeError, "failure_id 重复"):
            self.record(duplicated)
        self.assertFalse((self.fixture.evidence / "u4/dispositions").exists() and list((self.fixture.evidence / "u4/dispositions").rglob("*.json")))

    def test_hand_filled_results_and_missing_retests_are_rejected(self) -> None:
        self.fail_first_attempt()
        forged = self.entries()
        forged[0]["retests"] = [{"argv": ["python3", "-c", "pass"], "cwd": ".", "exit_code": 0, "status": "passed"}]
        with self.assertRaisesRegex(UpstreamMergeError, "不接受手填"):
            self.record(forged)
        empty = self.entries()
        empty[1]["retests"] = []
        with self.assertRaisesRegex(UpstreamMergeError, "至少一条定向复验"):
            self.record(empty)
        with self.assertRaisesRegex(UpstreamMergeError, "cause 必须是无首尾空白的非空字符串"):
            self.record(self.entries(egress={"cause": ""}))

    def test_non_blocked_attempt_needs_no_disposition(self) -> None:
        self.make_fixture()
        run_verification_gates(self.plan, "attempt-001")
        with self.assertRaisesRegex(UpstreamMergeError, "只有 blocked 的失败需要处置"):
            record_failure_disposition(self.plan, "attempt-001", self.write_input(self.entries()))

    # ---- 定向复验由工具真实执行 ----
    def test_retests_are_really_executed_and_recorded_with_log_bindings(self) -> None:
        self.fail_first_attempt()
        proof = self.temp_root / "retest-proof.txt"
        entries = self.entries()
        entries[0]["retests"] = [
            {"argv": ["python3", "-c", f"import sys; open({str(proof)!r},'w').write('ran'); print('out-line'); print('err-line', file=sys.stderr)"], "cwd": "."}
        ]
        result = self.record(entries)
        self.assertEqual(proof.read_text(encoding="utf-8"), "ran")
        self.assertEqual(result["result"], "closed")
        self.assertEqual(result["retest_count"], 2)
        document = json.loads(Path(result["disposition"]).read_text(encoding="utf-8"))
        retest = document["items"][0]["retests"][0]
        self.assertEqual((retest["exit_code"], retest["status"], retest["timed_out"]), (0, "passed", False))
        self.assertIn("out-line", retest["stdout_tail"])
        self.assertIn("err-line", retest["stderr_tail"])
        stdout_log = self.fixture.evidence / retest["stdout"]["path"]
        self.assertEqual(stdout_log.read_text(encoding="utf-8"), "out-line\n")
        self.assertEqual(stat.S_IMODE(stdout_log.stat().st_mode), 0o600)
        self.assertEqual(document["covered_failure_ids"], sorted(document["covered_failure_ids"]))
        self.assertEqual(document["failed_attempt"]["attempt_id"], "attempt-001")
        self.assertEqual(document["items"][0]["cause"], entries[0]["cause"])
        # 之后启动前检查不再把这些失败项当成未处置
        self.assertEqual(check_failure_disposition(self.plan)["blockers"], [])

    def test_failing_retest_makes_the_disposition_invalid_and_writes_nothing(self) -> None:
        self.fail_first_attempt()
        entries = self.entries()
        entries[1]["retests"] = [{"argv": ["python3", "-c", "import sys; print('仍然失败', file=sys.stderr); sys.exit(7)"], "cwd": "."}]
        with self.assertRaises(UpstreamMergeError) as caught:
            self.record(entries)
        message = str(caught.exception)
        self.assertIn("处置不成立", message)
        self.assertIn("full-regression/lint/golangci-lint-unit", message)
        self.assertIn("退出码 7", message)
        directory = self.fixture.evidence / "u4/dispositions/attempt-001"
        self.assertEqual(list(directory.glob("*.json")), [])
        # 日志留作排查
        self.assertIn("仍然失败", next(directory.rglob("02-01.stderr.txt")).read_text(encoding="utf-8"))
        self.assertTrue(check_failure_disposition(self.plan)["blockers"])

    def test_timed_out_and_unstartable_retests_fail(self) -> None:
        self.fail_first_attempt()
        slow = self.entries()
        slow[0]["retests"] = [{"argv": ["python3", "-c", "import time; time.sleep(30)"], "cwd": ".", "timeout_seconds": 1}]
        with self.assertRaisesRegex(UpstreamMergeError, "超时"):
            self.record(slow)
        missing = self.entries()
        missing[0]["retests"] = [{"argv": ["/nonexistent/retest-binary"], "cwd": "."}]
        with self.assertRaisesRegex(UpstreamMergeError, "退出码 None"):
            self.record(missing)
        self.assertEqual(list((self.fixture.evidence / "u4/dispositions/attempt-001").glob("*.json")), [])

    def test_retest_that_dirties_the_candidate_tree_is_rejected(self) -> None:
        self.fail_first_attempt()
        entries = self.entries()
        entries[0]["retests"] = [{"argv": ["python3", "-c", "open('leftover.txt','w').write('x')"], "cwd": "."}]
        with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
            self.record(entries)
        (self.fixture.root / "leftover.txt").unlink()
        self.assertEqual(list((self.fixture.evidence / "u4/dispositions/attempt-001").glob("*.json")), [])

    def test_retest_cwd_follows_the_gate_cwd_safety_rules(self) -> None:
        self.fail_first_attempt()
        for bad in ("../outside", "/etc", "missing-dir"):
            with self.subTest(cwd=bad):
                entries = self.entries()
                entries[0]["retests"] = [{"argv": ["python3", "-c", "pass"], "cwd": bad}]
                with self.assertRaises(UpstreamMergeError):
                    self.record(entries)

    # ---- 处置类别 ----
    def test_functional_failure_requires_the_candidate_to_have_advanced(self) -> None:
        self.fail_first_attempt()
        functional = self.entries(egress={"category": "functional"}, lint={"category": "functional"})
        with self.assertRaisesRegex(UpstreamMergeError, "已在 attempt attempt-001 之后推进"):
            self.record(functional)
        # 省略类别＝归因不清＝按功能失败处理
        omitted = self.entries()
        for entry in omitted:
            del entry["category"]
        with self.assertRaisesRegex(UpstreamMergeError, "已在 attempt attempt-001 之后推进"):
            self.record(omitted)
        # 候选推进之后才成立
        new_commit = self.advance_candidate()
        result = self.record(omitted)
        self.assertEqual(result["source_commit"], new_commit)
        document = json.loads(Path(result["disposition"]).read_text(encoding="utf-8"))
        self.assertEqual({item["category"] for item in document["items"]}, {"functional"})
        self.assertNotEqual(document["failed_attempt"]["source_commit"], document["source_commit"])
        self.assertEqual(check_failure_disposition(self.plan)["blockers"], [])

    def test_disposition_is_bound_to_the_current_candidate_commit(self) -> None:
        self.fail_first_attempt()
        self.advance_candidate()
        self.record(self.entries(egress={"category": "functional"}, lint={"category": "functional"}))
        self.assertEqual(check_failure_disposition(self.plan)["blockers"], [])
        # 候选又推进了一轮：旧处置不再绑定当前候选提交，须重新处置
        self.advance_candidate({"fix2.txt": "又一轮修复\n"})
        report = check_failure_disposition(self.plan)
        self.assertEqual(len(report["blockers"]), 4)
        self.assertTrue(any("不是当前候选提交" in text for text in report["blockers"]), report["blockers"])
        second = self.record(self.entries(egress={"category": "functional"}, lint={"category": "functional"}))
        self.assertEqual(second["disposition_id"], "disposition-002")  # 追加新记录，不覆盖旧的
        self.assertEqual(check_failure_disposition(self.plan)["blockers"], [])

    def test_environment_failure_requires_the_current_toolchain_check_to_pass(self) -> None:
        self.fail_first_attempt()
        with self.assertRaisesRegex(UpstreamMergeError, "要求当前版本检查通过.*期望 go1.27.3"):
            self.record(self.entries(), probe=fake_probe(go="go1.27.2"))
        result = self.record(self.entries())
        document = json.loads(Path(result["disposition"]).read_text(encoding="utf-8"))
        self.assertEqual(document["toolchain"]["result"], "passed")
        self.assertEqual(document["items"][0]["category_checks"]["toolchain_result"], "passed")
        # 同一候选提交上的环境类处置成立，之后 gates-run 可以重新启动
        self.scenario("passed")
        self.assertEqual(self.prechecked_run("attempt-002", from_attempt="attempt-001")["result"], "passed")

    def test_frozen_digest_failure_requires_a_successor_revision_and_a_frozen_retest(self) -> None:
        self.fail_first_attempt()
        fake_bin = self.temp_root / "bin"
        fake_bin.mkdir()
        fake_go = fake_bin / "go"
        fake_go.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_go.chmod(0o755)
        frozen = {"argv": [str(fake_go), "test", "./internal/officialegress", "-run", "Frozen|Transition|Successor", "-count=1"], "cwd": "."}
        entries = self.entries(
            egress={"category": "frozen_digest", "retests": [frozen]},
            lint={"category": "environment"},
        )
        # 候选未推进
        with self.assertRaisesRegex(UpstreamMergeError, "已在 attempt attempt-001 之后推进"):
            self.record(entries)
        # 推进了但没有台账 revision（只改了普通文件）
        self.advance_candidate()
        with self.assertRaisesRegex(UpstreamMergeError, "已含台账 revision"):
            self.record(entries)
        # 台账 revision 已含，但复验不是冻结测试
        receipt = (
            '{\n  "schema_version": "official-egress-upstream-freeze-successor/v1",\n  "scope": "x"\n}\n'
        )
        self.advance_candidate({"docs/egress/maintenance/upstream-x-freeze-successor.json": receipt})
        not_frozen = self.entries(egress={"category": "frozen_digest"}, lint={"category": "environment"})
        with self.assertRaisesRegex(UpstreamMergeError, "必须有冻结测试"):
            self.record(not_frozen)
        # 台账 revision + 冻结复验都满足
        result = self.record(entries)
        document = json.loads(Path(result["disposition"]).read_text(encoding="utf-8"))
        checks = document["items"][0]["category_checks"]
        self.assertEqual(checks["successor_receipts"], ["docs/egress/maintenance/upstream-x-freeze-successor.json"])
        self.assertEqual(document["items"][0]["category"], "frozen_digest")

    def test_non_successor_maintenance_files_do_not_count_as_a_ledger_revision(self) -> None:
        self.fail_first_attempt()
        self.advance_candidate({"docs/egress/maintenance/some-notes.json": '{"schema_version": "notes/v1"}\n'})
        entries = self.entries(egress={"category": "frozen_digest"})
        with self.assertRaisesRegex(UpstreamMergeError, "已含台账 revision"):
            self.record(entries)

    def test_frozen_retest_recognition(self) -> None:
        recognized = [
            ["go", "test", "./internal/officialegress", "-run", "Frozen|Transition", "-count=1"],
            ["/usr/local/go/bin/go", "test", "-run=FrozenDigest", "./..."],
            ["make", "check-egress-spec"],
            ["make", "-k", "check-egress-spec-ci"],
        ]
        rejected = [
            ["go", "test", "./..."],
            ["go", "test", "-run", "Transition", "./..."],
            ["python3", "-c", "pass"],
            ["make", "test-unit"],
        ]
        for argv in recognized:
            self.assertTrue(gate_guard._is_frozen_retest(argv), argv)
        for argv in rejected:
            self.assertFalse(gate_guard._is_frozen_retest(argv), argv)

    # ---- 校验与篡改 ----
    def test_tampered_retest_log_invalidates_the_disposition(self) -> None:
        self.fail_first_attempt()
        result = self.record(self.entries())
        self.assertEqual(check_failure_disposition(self.plan)["blockers"], [])
        log = next((self.fixture.evidence / "u4/dispositions/attempt-001/disposition-001").glob("01-01.stdout.txt"))
        log.chmod(0o600)
        log.write_text("被改过\n", encoding="utf-8")
        report = check_failure_disposition(self.plan)
        self.assertEqual(len(report["blockers"]), 4, report["blockers"])
        self.assertTrue(any("已有处置记录无效" in text and "制品内容漂移" in text for text in report["blockers"]))
        self.assertEqual(result["result"], "closed")

    def test_disposition_must_cover_every_expected_failure_item(self) -> None:
        self.fail_first_attempt()
        result = self.record(self.entries())
        document = read_attempt_receipt(self.plan, self.attempt_dir("attempt-001") / "receipt.json")
        expected = failure_items(self.plan, document) + [{"id": "inventory-closure"}]
        current = _current_commit(self.plan)
        with self.assertRaisesRegex(UpstreamMergeError, "没有覆盖失败项：\\['inventory-closure'\\]"):
            gate_guard.validate_disposition(self.plan, Path(result["disposition"]), expected_items=expected, current_commit=current)

    def test_disposition_document_matches_the_registered_artifact_schema(self) -> None:
        self.fail_first_attempt()
        result = self.record(self.entries())
        document = json.loads(Path(result["disposition"]).read_text(encoding="utf-8"))
        schema = json.loads((SOURCE_ROOT / "tools/upstream_merge_artifacts.schema.json").read_text(encoding="utf-8"))
        self.assertIn({"$ref": "#/$defs/failureDisposition"}, schema["oneOf"])
        definition = schema["$defs"]["failureDisposition"]
        self.assertEqual(set(document), set(definition["required"]))
        self.assertEqual(set(document), set(definition["properties"]))
        self.assertEqual(document["schema_version"], definition["properties"]["schema_version"]["const"])
        self.assertEqual(set(document["failed_attempt"]), set(definition["properties"]["failed_attempt"]["required"]))
        item_schema = definition["properties"]["items"]["items"]
        self.assertEqual(set(document["items"][0]), set(item_schema["required"]))
        self.assertEqual(set(document["items"][0]["retests"][0]), set(item_schema["properties"]["retests"]["items"]["required"]))

    def test_disposition_files_are_write_once(self) -> None:
        self.fail_first_attempt()
        first = self.record(self.entries())
        second = self.record(self.entries())
        self.assertNotEqual(first["disposition"], second["disposition"])
        self.assertTrue(Path(first["disposition"]).is_file() and Path(second["disposition"]).is_file())
        self.assertEqual(stat.S_IMODE(Path(first["disposition"]).stat().st_mode), 0o600)


def _current_commit(plan: LoadedPlan) -> str:
    from tools.upstream_merge.workflow import _load_source_candidate

    return _load_source_candidate(plan)["source_commit"]


class AttemptStatusTest(GuardFixture):
    def test_blocked_attempt_reports_failed_items_and_receipt_path(self) -> None:
        self.make_fixture()
        self.scenario("failed_steps")
        self.prechecked_run("attempt-001")
        status = attempt_status(self.plan)
        receipt_path = (self.attempt_dir("attempt-001") / "receipt.json").resolve()
        self.assertEqual(status["result"], "blocked")
        self.assertEqual(
            status["line"],
            "U4-RESULT attempt=attempt-001 result=blocked "
            "failed=full-regression/egress-spec/check-egress-spec,full-regression/lint/golangci-lint-unit "
            f"receipt={receipt_path}",
        )
        self.assertEqual(status["failed_gate_ids"], sorted(status["failed_gate_ids"]))
        self.assertEqual(len(status["failed_gate_ids"]), 10)

    def test_latest_attempt_is_chosen_when_no_id_is_given(self) -> None:
        self.make_fixture()
        self.scenario("failed_steps")
        self.prechecked_run("attempt-001")
        self.scenario("awaiting")
        # attempt-001 的失败未处置，用引擎直接造第二个 attempt
        run_verification_gates(self.plan, "attempt-002")
        self.assertEqual(attempt_status(self.plan)["attempt_id"], "attempt-002")
        self.assertEqual(attempt_status(self.plan, "attempt-001")["result"], "blocked")

    def test_running_versus_incomplete_without_a_receipt(self) -> None:
        self.make_fixture()
        running = self.attempt_dir("attempt-running")
        running.mkdir(parents=True, mode=0o700)
        write_json_once(running / "gates-run.pid.json", {"pid": os.getpid(), "started_at_utc": "2026-10-25T00:00:00Z"})
        dead = self.attempt_dir("attempt-dead")
        dead.mkdir(parents=True, mode=0o700)
        write_json_once(dead / "gates-run.pid.json", {"pid": 999999, "started_at_utc": "2026-10-25T00:00:00Z"})
        bare = self.attempt_dir("attempt-bare")
        bare.mkdir(parents=True, mode=0o700)
        self.assertEqual(attempt_status(self.plan, "attempt-running", alive=lambda pid: pid == os.getpid())["result"], "running")
        self.assertEqual(attempt_status(self.plan, "attempt-dead", alive=lambda pid: pid == os.getpid())["result"], "incomplete")
        self.assertEqual(attempt_status(self.plan, "attempt-bare")["result"], "incomplete")
        line = attempt_status(self.plan, "attempt-bare")["line"]
        self.assertEqual(line, "U4-RESULT attempt=attempt-bare result=incomplete failed=- receipt=-")

    def test_engine_leaves_a_run_marker_that_shows_running_until_the_receipt_exists(self) -> None:
        self.make_fixture()
        observed: dict[str, str] = {}
        seen_path = self.temp_root / "observed-status.txt"
        # 首个执行组运行期间，从外部读 gates-status：此时有目录和运行标记、还没有收据。
        probe_code = (
            "import json,subprocess,sys,pathlib;"
            f"sys.path.insert(0,{str(Path(__file__).resolve().parents[3])!r});"
            "from tools.upstream_merge.gate_guard import _read_marker;"
            "import os;"
            "root=pathlib.Path(os.environ['UPSTREAM_GATE_STATUS_FILE']).parent;"
            f"pathlib.Path({str(seen_path)!r}).write_text(json.dumps({{'marker': _read_marker(root), 'receipt': (root/'receipt.json').exists()}}))"
        )
        for gate in self.plan.document["gates"]:
            if gate["category"] == "inventory_closure":
                gate["argv"] = ["python3", "-c", probe_code]
            if gate["category"] == "secret_scan":
                gate["argv"] = ["python3", "-c", "pass"]
        run_verification_gates(self.plan, "attempt-001")
        observed = json.loads(seen_path.read_text(encoding="utf-8"))
        self.assertEqual(observed["marker"]["pid"], os.getpid())
        self.assertFalse(observed["receipt"])
        self.assertEqual(attempt_status(self.plan, "attempt-001")["result"], "passed")

    def test_unknown_attempt_and_empty_plan_are_rejected(self) -> None:
        self.make_fixture()
        with self.assertRaisesRegex(UpstreamMergeError, "还没有任何 attempt"):
            attempt_status(self.plan)
        run_verification_gates(self.plan, "attempt-001")
        with self.assertRaisesRegex(UpstreamMergeError, "找不到 attempt"):
            attempt_status(self.plan, "attempt-404")

    def test_result_line_format_is_fixed(self) -> None:
        self.assertEqual(
            u4_result_line("a1", "blocked", ["x/y", "z"], Path("/tmp/receipt.json")),
            "U4-RESULT attempt=a1 result=blocked failed=x/y,z receipt=/tmp/receipt.json",
        )
        self.assertEqual(u4_result_line("a1", "running", [], None), "U4-RESULT attempt=a1 result=running failed=- receipt=-")

    def test_tampered_receipt_is_rejected_when_read(self) -> None:
        self.make_fixture()
        run_verification_gates(self.plan, "attempt-001")
        receipt = self.attempt_dir("attempt-001") / "receipt.json"
        document = json.loads(receipt.read_text(encoding="utf-8"))
        document["result"] = "passed" if document["result"] != "passed" else "blocked"
        receipt.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "identity_sha256 漂移"):
            attempt_status(self.plan, "attempt-001")


class CommandLineExitStatusTest(GuardFixture):
    """经 main() 走完整命令行路径：结论、stderr 结论行与退出码三者一致。"""

    def invoke(self, argv: list[str], *, patches: dict | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.BytesIO(), io.StringIO()
        # 真实的文本包装：main() 直接写 sys.stdout.buffer，argparse 还会探测 fileno。
        stdout_wrapper = io.TextIOWrapper(stdout, encoding="utf-8")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "stdout", stdout_wrapper))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            stack.enter_context(mock.patch.object(cli, "_loaded", return_value=self.plan))
            stack.enter_context(mock.patch.object(cli, "timing_ledger_path", return_value=(None, None)))
            for target, replacement in (patches or {}).items():
                stack.enter_context(mock.patch(target, replacement))
            code = cli.main(argv)
        return code, stdout.getvalue().decode("utf-8"), stderr.getvalue()

    def plan_args(self, command: str, *extra: str) -> list[str]:
        return [command, "--repository", str(self.fixture.root), "--plan", str(self.fixture.evidence / "plan.json"), *extra]

    def guarded(self) -> dict:
        return {
            "tools.upstream_merge.gate_guard.check_toolchain": lambda root, role=None, **_: _passing_toolchain(),
        }

    def test_blocked_gates_run_exits_nonzero_and_prints_the_matching_result_line(self) -> None:
        self.make_fixture()
        self.scenario("failed_steps")
        code, stdout, stderr = self.invoke(self.plan_args("gates-run", "--attempt-id", "attempt-001"), patches=self.guarded())
        receipt_path = (self.attempt_dir("attempt-001") / "receipt.json").resolve()
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(stdout)["result"], "blocked")
        lines = [line for line in stderr.splitlines() if line.startswith("U4-RESULT")]
        self.assertEqual(len(lines), 1)
        self.assertIn("result=blocked", lines[0])
        self.assertIn(f"receipt={receipt_path}", lines[0])
        # gates-status 输出同一行，退出码同为 5
        status_code, status_stdout, status_stderr = self.invoke(self.plan_args("gates-status", "--attempt-id", "attempt-001"))
        self.assertEqual(status_code, 5)
        self.assertEqual(status_stderr.strip(), lines[0])
        self.assertEqual(json.loads(status_stdout)["line"], lines[0])

    def test_passed_and_awaiting_gates_run_exit_zero(self) -> None:
        self.make_fixture()
        code, stdout, stderr = self.invoke(self.plan_args("gates-run", "--attempt-id", "attempt-001"), patches=self.guarded())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["result"], "passed")
        self.assertIn("result=passed failed=- receipt=", stderr)
        self.scenario("awaiting")
        code, stdout, stderr = self.invoke(self.plan_args("gates-run", "--attempt-id", "attempt-002"), patches=self.guarded())
        self.assertEqual(code, 0)
        self.assertIn("result=awaiting_ci", stderr)

    def test_precheck_rejection_exits_with_the_tool_rejection_code_and_creates_no_attempt(self) -> None:
        self.make_fixture({"src/leak.txt": f"token={HIT}\n"})
        code, stdout, stderr = self.invoke(self.plan_args("gates-run", "--attempt-id", "attempt-001"), patches=self.guarded())
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("上游合并工具拒绝：U-4 启动前检查未通过", stderr)
        self.assertIn("src/leak.txt", stderr)
        self.assertFalse(self.attempt_dir("attempt-001").exists())

    def test_gates_status_exit_codes_for_unfinished_attempts(self) -> None:
        self.make_fixture()
        bare = self.attempt_dir("attempt-bare")
        bare.mkdir(parents=True, mode=0o700)
        code, stdout, stderr = self.invoke(self.plan_args("gates-status", "--attempt-id", "attempt-bare"))
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(stdout)["result"], "incomplete")
        self.assertIn("result=incomplete", stderr)

    def test_failure_disposition_command_end_to_end(self) -> None:
        self.make_fixture()
        self.scenario("failed_steps")
        self.invoke(self.plan_args("gates-run", "--attempt-id", "attempt-001"), patches=self.guarded())
        entries = [
            {
                "failure_id": failure_id,
                "category": "environment",
                "cause": "环境",
                "fix": "改环境",
                "retests": [self.passing_retest()],
            }
            for failure_id in ("full-regression/egress-spec/check-egress-spec", "full-regression/lint/golangci-lint-unit")
        ]
        input_path = self.write_input(entries)
        with mock.patch("tools.upstream_merge.gate_guard.check_toolchain", return_value=_passing_toolchain()):
            code, stdout, stderr = self.invoke(
                self.plan_args("failure-disposition", "--attempt-id", "attempt-001", "--input", str(input_path))
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["result"], "closed")
        self.scenario("passed")
        code, stdout, stderr = self.invoke(self.plan_args("gates-run", "--attempt-id", "attempt-002"), patches=self.guarded())
        self.assertEqual(code, 0, stderr)


def _passing_toolchain() -> dict:
    return {"schema_version": "x", "role": "candidate", "repository": "x", "expected": {}, "actual": {}, "mismatches": [], "hints": [], "result": "passed"}


if __name__ == "__main__":
    unittest.main()
