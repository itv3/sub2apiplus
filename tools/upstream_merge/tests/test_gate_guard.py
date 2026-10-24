"""U-4 启动前检查、失败项处置与结论读取：工具版本不符、候选密钥命中、上一轮失败未处置三种情况都必须
在任何执行组启动之前退出并列出原因；处置的定向复验由工具真实执行，非 0 不成立。

只用合成 Git 图、假编排脚本与假探测命令，不执行真实门禁，不依赖本机安装的 go／golangci-lint。测试里的命中串
在运行时拼接，源码里不出现完整的凭据形态字面量。
"""

from __future__ import annotations

import contextlib
import hashlib
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
from tools.upstream_merge.canonical import artifact_binding, bind_identity, canonical_bytes, write_json_once
from tools.upstream_merge.contracts import LoadedPlan, latest_stage_path, next_stage_path
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.freeze import bind_gate_compatible_identity
from tools.upstream_merge.gate_guard import (
    FAILURE_DISPOSITION_INPUT_SCHEMA,
    attempt_status,
    check_failure_disposition,
    failure_items,
    latest_receipt_attempt,
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

    def fake_tool(self, name: str, body: str = "") -> str:
        """在独立目录里造一个叫 name 的假可执行文件（复验族按基名识别 make／go／golangci-lint）；body 是脚本体。"""

        self._tool_count = getattr(self, "_tool_count", 0) + 1
        directory = self.temp_root / f"fake-bin-{self._tool_count:03d}"
        directory.mkdir()
        path = directory / name
        path.write_text(f"#!/bin/sh\n{body}\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def successor_receipt_text(self, tracked: str) -> str:
        """一份真正的冻结承接收据：自摘要用冻结承接收据的算法，后继摘要是 tracked 在当前候选里的内容摘要。"""

        digest = hashlib.sha256((self.fixture.root / tracked).read_bytes()).hexdigest()
        document = bind_gate_compatible_identity(
            {
                "schema_version": "official-egress-upstream-freeze-successor/v1",
                "scope": "upstream-test-freeze-successor",
                "transitions": [
                    {
                        "old_path": "",
                        "path": tracked,
                        "predecessor_sha256s": ["a" * 64],
                        "reason": "测试用承接",
                        "source_receipts": [],
                        "status": "modified",
                        "to_sha256": digest,
                    }
                ],
            }
        )
        return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def egress_retest(self, body: str = "", *, cwd: str = ".") -> dict:
        """属于 egress-spec 复验族的复验：仓库根下的 make check-egress-spec（假 make，可带副作用脚本体）。"""

        return {"argv": [self.fake_tool("make", body), "-k", "check-egress-spec"], "cwd": cwd}

    def lint_unit_retest(self, body: str = "") -> dict:
        """属于 lint-unit 复验族的复验：backend 下的 golangci-lint run --build-tags=unit（假二进制）。"""

        return {"argv": [self.fake_tool("golangci-lint", body), "run", "--build-tags=unit", "./..."], "cwd": "backend"}


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
        # 通过时只有一行启动前检查摘要，没有任何提示或阻断
        self.assertEqual(len(messages), 1)
        self.assertIn("启动前检查通过", messages[0])
        self.assertIn("没有上一轮 attempt", messages[0])
        self.assertIn("lint 2.14.0", messages[0])
        self.assertIn("候选密钥扫描通过", messages[0])
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
        # 第一条是对半截目录的提示，最后一条是启动前检查通过的摘要
        self.assertEqual(len(messages), 2)
        self.assertIn("attempt-000", messages[0])
        self.assertIn("没有 receipt.json", messages[0])
        self.assertIn("不算已知失败", messages[0])
        self.assertIn("启动前检查通过", messages[1])
        status = attempt_status(self.plan, "attempt-000")
        self.assertEqual((status["result"], status["receipt"]), ("incomplete", None))

    def test_dirty_candidate_worktree_is_rejected_before_any_check(self) -> None:
        self.make_fixture()
        (self.fixture.root / "stray.txt").write_text("未跟踪\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
            self.prechecked_run("attempt-001")
        self.assertFalse(self.attempt_dir("attempt-001").exists())


class FailureDispositionTest(GuardFixture):
    EGRESS = "full-regression/egress-spec/check-egress-spec"
    LINT_UNIT = "full-regression/lint/golangci-lint-unit"

    def fail_first_attempt(self) -> dict:
        self.make_fixture()
        self.scenario("failed_steps")
        return self.prechecked_run("attempt-001")

    def entries(self, **overrides) -> list[dict]:
        base = {
            "egress": {
                "failure_id": self.EGRESS,
                "category": "environment",
                "cause": "本机 Go 补丁版本与主干声明不一致，冻结自检报错",
                "fix": "改用 GOTOOLCHAIN=go1.27.3 重跑",
                "retests": [self.egress_retest()],
            },
            "lint": {
                "failure_id": self.LINT_UNIT,
                "category": "environment",
                "cause": "lint 二进制版本过旧",
                "fix": "改用 v2.14.0",
                "retests": [self.lint_unit_retest()],
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
        self.assertEqual([item["id"] for item in items], [self.EGRESS, self.LINT_UNIT])
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
            {"failure_id": "secret-scan", "cause": "x", "fix": "y", "retests": [self.egress_retest()]}
        ]
        with self.assertRaisesRegex(UpstreamMergeError, "不属于该 attempt=\\['secret-scan'\\]"):
            self.record(extra)
        duplicated = self.entries() + [self.entries()[0]]
        with self.assertRaisesRegex(UpstreamMergeError, "failure_id 重复"):
            self.record(duplicated)
        self.assertEqual(list((self.fixture.evidence / "u4").rglob("disposition-*.json")), [])

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

    # ---- 复验族：复验必须属于失败项自己，不能是任意一条通过的命令 ----
    def test_unrelated_passing_command_cannot_close_a_failure_item(self) -> None:
        """验收者的复现：functional＋推进一个无关提交＋复验 python3 -c "print('ok')" 曾经能把失败项关掉。"""

        self.fail_first_attempt()
        self.advance_candidate()  # 无关的候选推进
        unrelated = {"argv": ["python3", "-c", "print('ok')"], "cwd": "."}
        entries = self.entries(
            egress={"category": "functional", "retests": [unrelated]},
            lint={"category": "functional", "retests": [unrelated]},
        )
        with self.assertRaises(UpstreamMergeError) as caught:
            self.record(entries)
        message = str(caught.exception)
        self.assertIn(f"失败项 {self.EGRESS} 的复验里没有属于它的复验族的命令", message)
        self.assertIn("check-egress-spec", message)
        self.assertEqual(list((self.fixture.evidence / "u4").rglob("disposition-*.json")), [])
        # 工具没有执行这些无关命令，也没有留下复验日志目录
        self.assertEqual(list((self.fixture.evidence / "u4/dispositions").rglob("*.stdout.txt")), [])

    def test_a_retest_from_another_failure_items_family_does_not_count(self) -> None:
        self.fail_first_attempt()
        swapped = self.entries(egress={"retests": [self.lint_unit_retest()]}, lint={"retests": [self.egress_retest()]})
        with self.assertRaisesRegex(UpstreamMergeError, "复验里没有属于它的复验族的命令"):
            self.record(swapped)
        # 每个失败项至少有一条属于自己的即可，另附的辅助命令不碍事
        mixed = self.entries(egress={"retests": [self.egress_retest(), {"argv": ["python3", "-c", "pass"], "cwd": "."}]})
        self.assertEqual(self.record(mixed)["result"], "closed")

    def test_environment_category_still_needs_the_family(self) -> None:
        self.fail_first_attempt()
        unrelated = {"argv": ["python3", "-c", "print('ok')"], "cwd": "."}
        with self.assertRaisesRegex(UpstreamMergeError, "复验里没有属于它的复验族的命令"):
            self.record(self.entries(lint={"category": "environment", "retests": [unrelated]}))

    def test_forged_and_resealed_disposition_is_rejected_when_gates_run_starts(self) -> None:
        """改写已封存记录里的复验 argv 并重新封存，日志绑定仍然有效，但复验族不对，启动前检查必须判无效。"""

        self.fail_first_attempt()
        result = self.record(self.entries())
        path = Path(result["disposition"])
        document = json.loads(path.read_text(encoding="utf-8"))
        document["items"][0]["retests"][0]["argv"] = ["true"]
        document.pop("identity_sha256")
        path.write_bytes(canonical_bytes(bind_identity(document)))
        report = check_failure_disposition(self.plan)
        self.assertEqual(len(report["blockers"]), 4, report["blockers"])
        self.assertTrue(any("已有处置记录无效" in text and "复验族" in text for text in report["blockers"]), report["blockers"])
        with self.assertRaisesRegex(UpstreamMergeError, "尚未处置"):
            self.prechecked_run("attempt-002", from_attempt="attempt-001")

    def test_plain_group_failures_need_the_gates_own_expanded_command(self) -> None:
        self.make_fixture()
        self.plan = self.build_plan(inventory_exit=1)
        self.scenario("passed")
        receipt = run_verification_gates(self.plan, "attempt-001")
        self.assertEqual([item["id"] for item in failure_items(self.plan, receipt)], ["inventory-closure"])
        expected_argv = ["python3", str(self.order_script), "inventory-closure", "1"]
        entry = {"failure_id": "inventory-closure", "category": "environment", "cause": "c", "fix": "f"}
        # 别的命令不行
        with self.assertRaisesRegex(UpstreamMergeError, "门禁 inventory-closure 的展开命令"):
            self.record([{**entry, "retests": [{"argv": ["python3", "-c", "pass"], "cwd": "."}]}])
        # 门禁自己的展开命令（这里恰好会再次失败：退出码 1，处置不成立）
        with self.assertRaisesRegex(UpstreamMergeError, "定向复验未通过"):
            self.record([{**entry, "retests": [{"argv": expected_argv, "cwd": "."}]}])
        # 修好之后（门禁自己的命令这次通过）才成立：改成退出码 0 的同名脚本不行，必须逐字相同
        self.plan = self.build_plan(inventory_exit=0)
        passing = ["python3", str(self.order_script), "inventory-closure", "0"]
        result = self.record([{**entry, "retests": [{"argv": passing, "cwd": "."}]}])
        self.assertEqual(result["result"], "closed")

    def test_worktree_status_is_checked_by_the_tool_itself(self) -> None:
        self.make_fixture()
        # 造一个“执行后污染候选树”的 blocked attempt：门禁在候选树里留下未跟踪文件
        for gate in self.plan.document["gates"]:
            if gate["category"] == "inventory_closure":
                gate["argv"] = ["python3", "-c", "open('stray-from-gate.txt', 'w').write('x')"]
            if gate["category"] == "secret_scan":
                gate["argv"] = ["python3", "-c", "pass"]
        receipt = run_verification_gates(self.plan, "attempt-001")
        self.assertEqual(receipt["result"], "blocked")
        self.assertEqual(receipt["worktree_status_paths"], ["stray-from-gate.txt"])
        self.assertEqual([item["id"] for item in failure_items(self.plan, receipt)], ["worktree-status"])
        entry = {"failure_id": "worktree-status", "category": "environment", "cause": "门禁写了临时文件", "fix": "清理并改门禁"}
        # 候选树仍不干净：assert_clean 先拒绝
        with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
            self.record([entry])
        (self.fixture.root / "stray-from-gate.txt").unlink()
        # 填写者不必（也不能靠自报）提供复验命令；工具自己执行 git status 并记录
        result = self.record([entry])
        document = json.loads(Path(result["disposition"]).read_text(encoding="utf-8"))
        (retest,) = document["items"][0]["retests"]
        self.assertEqual(retest["argv"], ["git", "status", "--porcelain=v1", "--untracked-files=all"])
        self.assertEqual((retest["exit_code"], retest["status"]), (0, "passed"))
        self.assertEqual(self.fixture.evidence.joinpath(retest["stdout"]["path"]).read_text(encoding="utf-8"), "")

    # ---- 定向复验由工具真实执行 ----
    def test_retests_are_really_executed_and_recorded_with_log_bindings(self) -> None:
        self.fail_first_attempt()
        proof = self.temp_root / "retest-proof.txt"
        entries = self.entries()
        entries[0]["retests"] = [self.egress_retest(f"echo out-line\necho err-line >&2\nprintf ran > '{proof}'")]
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
        entries[1]["retests"] = [self.lint_unit_retest("echo 仍然失败 >&2\nexit 7")]
        with self.assertRaises(UpstreamMergeError) as caught:
            self.record(entries)
        message = str(caught.exception)
        self.assertIn("处置不成立", message)
        self.assertIn(self.LINT_UNIT, message)
        self.assertIn("退出码 7", message)
        directory = self.fixture.evidence / "u4/dispositions/attempt-001"
        self.assertEqual(list(directory.glob("*.json")), [])
        # 日志留作排查
        self.assertIn("仍然失败", next(directory.rglob("02-01.stderr.txt")).read_text(encoding="utf-8"))
        self.assertTrue(check_failure_disposition(self.plan)["blockers"])

    def test_timed_out_and_unstartable_retests_fail(self) -> None:
        self.fail_first_attempt()
        slow = self.entries()
        slow[0]["retests"] = [{**self.egress_retest("/bin/sleep 30"), "timeout_seconds": 1}]
        with self.assertRaisesRegex(UpstreamMergeError, "超时"):
            self.record(slow)
        missing = self.entries()
        missing[0]["retests"] = [{"argv": ["/nonexistent/make", "check-egress-spec"], "cwd": "."}]
        with self.assertRaisesRegex(UpstreamMergeError, "退出码 None"):
            self.record(missing)
        self.assertEqual(list((self.fixture.evidence / "u4/dispositions/attempt-001").glob("*.json")), [])

    def test_retest_that_dirties_the_candidate_tree_is_rejected(self) -> None:
        self.fail_first_attempt()
        entries = self.entries()
        entries[0]["retests"] = [self.egress_retest("touch leftover.txt")]
        with self.assertRaisesRegex(UpstreamMergeError, "干净工作树"):
            self.record(entries)
        (self.fixture.root / "leftover.txt").unlink()
        self.assertEqual(list((self.fixture.evidence / "u4/dispositions/attempt-001").glob("*.json")), [])

    def test_retest_cwd_follows_the_gate_cwd_safety_rules(self) -> None:
        self.fail_first_attempt()
        for bad in ("../outside", "/etc", "missing-dir"):
            with self.subTest(cwd=bad):
                entries = self.entries()
                entries[0]["retests"] = [self.egress_retest(cwd=bad)]
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

    def frozen_go_retest(self, *packages: str, run: str = "Frozen|Transition|Successor", cwd: str = "backend") -> dict:
        return {"argv": [self.fake_tool("go"), "test", *packages, "-run", run, "-count=1"], "cwd": cwd}

    def test_frozen_digest_failure_requires_a_real_successor_receipt_and_frozen_package_coverage(self) -> None:
        self.fail_first_attempt()
        both = self.frozen_go_retest("./internal/officialegress", "./internal/service")
        entries = self.entries(egress={"category": "frozen_digest", "retests": [both]}, lint={"category": "environment"})
        # 候选未推进
        with self.assertRaisesRegex(UpstreamMergeError, "已在 attempt attempt-001 之后推进"):
            self.record(entries)
        # 推进了但没有台账 revision（只改了普通文件）
        self.advance_candidate()
        with self.assertRaisesRegex(UpstreamMergeError, "已含台账 revision"):
            self.record(entries)
        # 两提交之间新增的 JSON 只是名字像、没有自摘要与 transitions：不算承接收据
        self.advance_candidate({"docs/egress/maintenance/upstream-x-freeze-successor.json": '{"schema_version": "x-freeze-successor/v1"}\n'})
        with self.assertRaisesRegex(UpstreamMergeError, "已含台账 revision"):
            self.record(entries)
        # 真正的承接收据：自摘要正确、transitions 非空、前序与后继摘要齐全、后继摘要等于当前内容
        self.advance_candidate({"docs/egress/maintenance/upstream-y-freeze-successor.json": self.successor_receipt_text("fix.txt")})
        # 复验只覆盖一个包、或 -run 不含冻结类模式：连 egress-spec 的复验族都不满足
        only_one = self.entries(
            egress={"category": "frozen_digest", "retests": [self.frozen_go_retest("./internal/officialegress")]},
            lint={"category": "environment"},
        )
        with self.assertRaisesRegex(UpstreamMergeError, "复验里没有属于它的复验族的命令"):
            self.record(only_one)
        wrong_run = self.entries(
            egress={"category": "frozen_digest", "retests": [self.frozen_go_retest("./...", run="TestSomethingElse")]},
            lint={"category": "environment"},
        )
        with self.assertRaisesRegex(UpstreamMergeError, "复验里没有属于它的复验族的命令"):
            self.record(wrong_run)
        # 属于 egress-spec 复验族、却不是冻结复验（某个 egress-spec-* 子目标）：冻结摘要类另要求覆盖冻结包
        sub_target = {"argv": [self.fake_tool("make"), "egress-spec-version-leak"], "cwd": "."}
        not_frozen = self.entries(egress={"category": "frozen_digest", "retests": [sub_target]}, lint={"category": "environment"})
        with self.assertRaisesRegex(UpstreamMergeError, "必须覆盖冻结包"):
            self.record(not_frozen)
        # 台账 revision + 两个包都覆盖的冻结复验
        result = self.record(entries)
        document = json.loads(Path(result["disposition"]).read_text(encoding="utf-8"))
        checks = document["items"][0]["category_checks"]
        self.assertEqual(checks["successor_receipts"], ["docs/egress/maintenance/upstream-y-freeze-successor.json"])
        self.assertEqual(document["items"][0]["category"], "frozen_digest")
        # 用 ./... 一条命令覆盖两个包同样成立
        everything = self.entries(
            egress={"category": "frozen_digest", "retests": [self.frozen_go_retest("./...")]}, lint={"category": "environment"}
        )
        self.assertEqual(self.record(everything)["result"], "closed")

    def test_stale_successor_receipt_that_no_longer_describes_the_candidate_does_not_count(self) -> None:
        self.fail_first_attempt()
        self.advance_candidate()
        receipt = self.successor_receipt_text("fix.txt")
        self.advance_candidate({"docs/egress/maintenance/upstream-z-freeze-successor.json": receipt})
        # 收据之后 fix.txt 又变了，收据登记的后继摘要已不是当前内容
        self.advance_candidate({"fix.txt": "收据之后又改了\n"})
        entries = self.entries(
            egress={"category": "frozen_digest", "retests": [self.frozen_go_retest("./...")]}, lint={"category": "environment"}
        )
        with self.assertRaisesRegex(UpstreamMergeError, "已含台账 revision"):
            self.record(entries)

    def test_non_successor_maintenance_files_do_not_count_as_a_ledger_revision(self) -> None:
        self.fail_first_attempt()
        self.advance_candidate({"docs/egress/maintenance/some-notes.json": '{"schema_version": "notes/v1"}\n'})
        entries = self.entries(egress={"category": "frozen_digest", "retests": [self.frozen_go_retest("./...")]})
        with self.assertRaisesRegex(UpstreamMergeError, "已含台账 revision"):
            self.record(entries)

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


class LatestAttemptOrderingTest(GuardFixture):
    """“上一轮”看收据内容（from_attempt 链、运行标记里的开始时间），不看文件修改时间。"""

    def set_started(self, attempt: str, value: str | None) -> None:
        marker = self.attempt_dir(attempt) / "gates-run.pid.json"
        marker.chmod(0o600)
        if value is None:
            marker.unlink()
        else:
            marker.write_text(json.dumps({"pid": os.getpid(), "started_at_utc": value}), encoding="utf-8")

    def two_independent_attempts(self) -> None:
        """attempt-001 blocked，随后一次全新的 attempt-002（不引用 001）passed；两者互不引用。"""

        self.make_fixture()
        self.scenario("failed_steps")
        run_verification_gates(self.plan, "attempt-001")
        self.scenario("passed")
        run_verification_gates(self.plan, "attempt-002")
        self.set_started("attempt-001", "2026-10-25T00:00:01Z")
        self.set_started("attempt-002", "2026-10-25T00:00:09Z")

    def test_touching_an_old_receipt_does_not_make_it_the_latest(self) -> None:
        self.two_independent_attempts()
        old = self.attempt_dir("attempt-001") / "receipt.json"
        future = old.stat().st_mtime + 86400
        os.utime(old, (future, future))  # 旧收据的修改时间比新的还晚
        latest, _without = latest_receipt_attempt(self.plan)
        self.assertEqual(latest.name, "attempt-002")
        report = check_failure_disposition(self.plan)
        self.assertEqual((report["latest_attempt"], report["latest_result"], report["blockers"]), ("attempt-002", "passed", []))

    def test_copying_an_old_receipt_does_not_change_the_order(self) -> None:
        self.two_independent_attempts()
        copied = self.attempt_dir("attempt-001")
        for path in copied.rglob("*"):
            os.utime(path, None)  # 全部文件的修改时间刷新成“现在”
        self.assertEqual(latest_receipt_attempt(self.plan)[0].name, "attempt-002")
        # 反过来：新收据被早期时间戳取代也不影响（顺序看开始时间，不看修改时间）
        self.set_started("attempt-001", "2026-10-25T00:00:20Z")
        self.assertEqual(latest_receipt_attempt(self.plan)[0].name, "attempt-001")

    def test_from_attempt_chain_decides_between_a_parent_and_its_rerun(self) -> None:
        self.make_fixture()
        self.scenario("failed_steps")
        run_verification_gates(self.plan, "attempt-001")
        self.scenario("passed")
        run_verification_gates(self.plan, "attempt-002", from_attempt="attempt-001")
        # 即使两个运行标记的时间被写成相反的顺序，重跑引用了上一轮，上一轮就不是最近的
        self.set_started("attempt-001", "2026-10-25T00:00:30Z")
        self.set_started("attempt-002", "2026-10-25T00:00:01Z")
        self.assertEqual(latest_receipt_attempt(self.plan)[0].name, "attempt-002")

    def test_missing_start_time_between_independent_attempts_fails_closed(self) -> None:
        self.two_independent_attempts()
        self.set_started("attempt-002", None)
        with self.assertRaisesRegex(UpstreamMergeError, "无法确定上一轮 attempt.*取不到开始时间"):
            latest_receipt_attempt(self.plan)
        with self.assertRaisesRegex(UpstreamMergeError, "无法确定上一轮"):
            check_failure_disposition(self.plan)
        with self.assertRaisesRegex(UpstreamMergeError, "无法确定上一轮"):
            self.prechecked_run("attempt-003")
        self.assertFalse(self.attempt_dir("attempt-003").exists())

    def test_tied_start_times_between_independent_attempts_fail_closed(self) -> None:
        self.two_independent_attempts()
        self.set_started("attempt-001", "2026-10-25T00:00:09Z")
        with self.assertRaisesRegex(UpstreamMergeError, "开始时间并列"):
            latest_receipt_attempt(self.plan)

    def test_attempts_without_a_marker_inherit_the_time_of_the_attempt_they_continue(self) -> None:
        self.make_fixture()
        self.scenario("awaiting")
        run_verification_gates(self.plan, "attempt-001")
        # 导入 CI 证据产生的 attempt 没有运行标记，靠 from_attempt 链排在被引用的 attempt 之后
        from tools.upstream_merge.tests.test_ci_evidence import FakeActionsApi
        from tools.upstream_merge.workflow import import_ci_evidence

        api = FakeActionsApi(self.fixture.fork)
        import_ci_evidence(self.plan, "attempt-002", "attempt-001", repository_slug="itv3/sub2apiplus", api=api)
        self.assertFalse((self.attempt_dir("attempt-002") / "gates-run.pid.json").exists())
        self.assertEqual(latest_receipt_attempt(self.plan)[0].name, "attempt-002")
        report = check_failure_disposition(self.plan)
        self.assertEqual((report["latest_attempt"], report["latest_result"]), ("attempt-002", "passed"))
        # 再来一次互不引用的全新 attempt-003：导入的 attempt-002 没有标记，沿用它所继续的 attempt-001 的开始时间
        run_verification_gates(self.plan, "attempt-003")
        self.set_started("attempt-001", "2026-10-25T00:00:05Z")
        self.set_started("attempt-003", "2026-10-25T00:00:09Z")
        self.assertEqual(latest_receipt_attempt(self.plan)[0].name, "attempt-003")
        self.set_started("attempt-003", "2026-10-25T00:00:02Z")
        self.assertEqual(latest_receipt_attempt(self.plan)[0].name, "attempt-002")


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
        retests = {
            "full-regression/egress-spec/check-egress-spec": [self.egress_retest()],
            "full-regression/lint/golangci-lint-unit": [self.lint_unit_retest()],
        }
        entries = [
            {"failure_id": failure_id, "category": "environment", "cause": "环境", "fix": "改环境", "retests": retest}
            for failure_id, retest in retests.items()
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
    actual = {"go": {"version": "go1.27.3"}, "golangci_lint": {"version": "2.14.0"}}
    return {"schema_version": "x", "role": "candidate", "repository": "x", "expected": {}, "actual": actual, "mismatches": [], "hints": [], "result": "passed"}


if __name__ == "__main__":
    unittest.main()
