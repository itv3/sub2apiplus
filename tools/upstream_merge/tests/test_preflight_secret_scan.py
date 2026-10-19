"""预检第七项“主干密钥扫描”：与 U-4 同一份 secret_scan 门禁，对受维护分支 HEAD 的只读检出执行。

v0.2.17 合并时，U-4 的 10-secret-scan 不在基线 7 项里，合并时才第一次扫到主干上的合成凭据，作废了一个 Plan。
预检因此直接执行 request 里的同一份门禁，把问题前移到 plan-create 之前。

门禁 argv 一律取标准模板 request_template_v2.json 里 id 为 10-secret-scan 的那条，证明预检执行的就是 U-4 的
那一份扫描。测试里的命中串都在运行时拼接，源码里不出现完整的凭据形态字面量。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import workflow
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.tests.test_covered_tags import CoveredTagsFixture, git
from tools.upstream_merge.workflow import SECRET_SCAN_METHOD, _preflight_secret_scan

SOURCE_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = SOURCE_ROOT / "tools/upstream_merge/request_template_v2.json"
SECRET_SCAN_GATE_ID = "10-secret-scan"
# 运行时拼接的 AWS 访问密钥形态串，命中门禁里的 AKIA[0-9A-Z]{16}；它的摘要不在模板的精确白名单内。
HIT = "AKIA" + "Z" * 16


def template_gates() -> list[dict]:
    """标准模板里的全部 12 个门禁（深拷贝，测试可随意改）。"""

    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(template["request"]["gates"])


def secret_gate(gates: list[dict]) -> dict:
    return next(gate for gate in gates if gate["id"] == SECRET_SCAN_GATE_ID)


class PreflightSecretScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.work = Path(self.temporary.name) / "work"
        self.work.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Preflight Test")
        git(self.root, "config", "user.email", "preflight@example.invalid")
        git(self.root, "config", "commit.gpgsign", "false")
        self.write("README.md", "干净的仓库\n")
        self.write("src/app.txt", "没有凭据\n")
        self.commit("base")
        self.gates = template_gates()

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def scan(self, gates: list[dict] | None = None, *, fork_head: str | None = None) -> dict:
        head = fork_head or git(self.root, "rev-parse", "HEAD")
        return _preflight_secret_scan(
            self.root, head, self.gates if gates is None else gates, self.work, dict(os.environ)
        )

    def assert_checkout_removed(self) -> None:
        """扫描结束后：主仓库没有被改动、临时 worktree 已移除、检出目录不在了。"""

        self.assertEqual(git(self.root, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertFalse((self.work / "secret-scan-checkout").exists())

    def assert_clean_and_checkout_removed(self) -> None:
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=all"), "")
        self.assert_checkout_removed()

    def test_clean_head_passes(self) -> None:
        result = self.scan()
        self.assertEqual(
            result,
            {
                "status": "passed",
                "method": SECRET_SCAN_METHOD,
                "gate_id": SECRET_SCAN_GATE_ID,
                "source_commit": git(self.root, "rev-parse", "HEAD"),
                "exit_code": 0,
            },
        )
        self.assertIn("U-4", result["method"])
        self.assert_clean_and_checkout_removed()

    def test_committed_hit_fails_and_names_the_path(self) -> None:
        self.write("src/leak.txt", f"token={HIT}\n")
        head = self.commit("提交了命中串")
        result = self.scan()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_commit"], head)
        self.assertEqual(result["gate_id"], SECRET_SCAN_GATE_ID)
        self.assertIsInstance(result["exit_code"], int)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("src/leak.txt", result["output"])
        # 报告里只有路径与规则，不回显命中的凭据内容。
        self.assertNotIn(HIT, result["output"])
        self.assertLessEqual(len(result["output"]), 4000)
        self.assertNotIn("reason", result)
        self.assert_clean_and_checkout_removed()

    def test_uncommitted_hit_in_main_worktree_is_not_scanned(self) -> None:
        # 主仓库工作树里有未提交的命中串（已跟踪文件被改、另有未跟踪的新文件）：
        # 扫描对象是 fork_head 的只读检出，不受影响，也不会碰这些未提交的改动。
        self.write("src/app.txt", f"未提交的修改 {HIT}\n")
        self.write("src/untracked.txt", f"{HIT}\n")
        dirty = git(self.root, "status", "--porcelain", "--untracked-files=all")
        self.assertNotEqual(dirty, "")
        result = self.scan()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=all"), dirty)
        self.assert_checkout_removed()
        # 反证：同样的内容一旦提交进 HEAD，就必须被扫到。
        self.commit("提交命中串")
        self.assertEqual(self.scan()["status"], "failed")

    def test_exactly_one_secret_scan_gate_is_required(self) -> None:
        without = [gate for gate in self.gates if gate["category"] != "secret_scan"]
        duplicated = self.gates + [{**copy.deepcopy(secret_gate(self.gates)), "id": "10-secret-scan-copy"}]
        with mock.patch.object(workflow, "run_process", wraps=workflow.run_process) as run_process:
            for gates, actual in ((without, 0), ([], 0), (duplicated, 2)):
                result = self.scan(gates)
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["exit_code"])
                self.assertIsNone(result["gate_id"])
                self.assertIn(f"实际 {actual} 个", result["reason"])
                self.assertNotIn("output", result)
        run_process.assert_not_called()
        self.assert_clean_and_checkout_removed()

    def test_non_command_or_malformed_gate_fails_without_running(self) -> None:
        with mock.patch.object(workflow, "run_process", wraps=workflow.run_process) as run_process:
            for field, value, expected in (
                ("mode", "receipt_replay", "mode 必须是 command"),
                ("argv", [], "argv 必须是非空字符串数组"),
                ("argv", ["python3", 7], "argv 必须是非空字符串数组"),
                ("cwd", None, "cwd 必须是字符串"),
            ):
                gates = template_gates()
                secret_gate(gates)[field] = value
                result = self.scan(gates)
                self.assertEqual(result["status"], "failed", (field, value))
                self.assertIsNone(result["exit_code"])
                self.assertEqual(result["gate_id"], SECRET_SCAN_GATE_ID)
                self.assertIn(expected, result["reason"])
        run_process.assert_not_called()
        self.assert_clean_and_checkout_removed()

    def test_unknown_placeholder_is_rejected_without_running(self) -> None:
        for field in ("argv", "cwd"):
            with self.subTest(field=field):
                gates = template_gates()
                if field == "argv":
                    secret_gate(gates)["argv"].append("{unknown_placeholder}")
                else:
                    secret_gate(gates)["cwd"] = "{plan}"
                with mock.patch.object(workflow, "run_process", wraps=workflow.run_process) as run_process:
                    result = self.scan(gates)
                # 门禁没有被执行，连检出都没有建过。
                run_process.assert_not_called()
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["exit_code"])
                self.assertEqual(result["gate_id"], SECRET_SCAN_GATE_ID)
                self.assertIn("占位符", result["reason"])
                self.assertTrue("{unknown_placeholder}" in result["reason"] or "{plan}" in result["reason"])
                self.assert_clean_and_checkout_removed()

    def test_placeholders_render_to_the_checkout_not_the_main_repository(self) -> None:
        probe = (
            "import os,sys;"
            "a,b,main=sys.argv[1:4];"
            "assert a==b,(a,b);"
            "assert os.path.realpath(a)==os.path.realpath(os.getcwd()),(a,os.getcwd());"
            "assert os.path.realpath(a)!=os.path.realpath(main),(a,main);"
            "assert os.path.exists(os.path.join(a,'.git')),a;"
            "assert not os.path.exists(os.path.join(a,'untracked.txt')),a"
        )
        secret_gate(self.gates)["argv"] = ["python3", "-c", probe, "{repository}", "{source_repository}", str(self.root)]
        # 只存在于主仓库工作树的未跟踪文件：检出里不该有。
        self.write("untracked.txt", "只在主仓库工作树里\n")
        result = self.scan()
        self.assertEqual(result["status"], "passed", result)
        self.assert_checkout_removed()

    def test_relative_cwd_is_resolved_inside_the_checkout(self) -> None:
        secret_gate(self.gates)["argv"] = [
            "python3",
            "-c",
            "import os;assert os.path.basename(os.getcwd())=='src',os.getcwd()",
        ]
        secret_gate(self.gates)["cwd"] = "src"
        self.assertEqual(self.scan()["status"], "passed")
        self.assert_checkout_removed()

    def test_untrusted_cwd_is_rejected_without_running(self) -> None:
        # 越界、绝对路径、不存在的目录、指向文件：与 gates-run 同一套安全规则，都不执行。
        for cwd in ("../outside", "/etc", "missing-dir", "src/app.txt"):
            with self.subTest(cwd=cwd):
                secret_gate(self.gates)["cwd"] = cwd
                with mock.patch.object(workflow, "run_process", wraps=workflow.run_process) as run_process:
                    result = self.scan()
                run_process.assert_not_called()
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["exit_code"])
                self.assertIn("cwd", result["reason"])
                self.assert_clean_and_checkout_removed()

    def test_any_exception_becomes_a_failed_item_and_checkout_is_removed(self) -> None:
        with mock.patch.object(workflow, "run_process", side_effect=OSError("磁盘已满")):
            result = self.scan()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["exit_code"])
        self.assertIn("磁盘已满", result["reason"])
        self.assertEqual(result["gate_id"], SECRET_SCAN_GATE_ID)
        self.assert_clean_and_checkout_removed()
        # 建检出本身失败（提交不存在）同样只成项，不抛出。
        missing = self.scan(fork_head="0" * 40)
        self.assertEqual(missing["status"], "failed")
        self.assertEqual(missing["source_commit"], "0" * 40)
        self.assertIn("没有跑出结论", missing["reason"])
        self.assert_clean_and_checkout_removed()

    def test_runs_the_same_scan_as_u4(self) -> None:
        captured: list[tuple[list[str], Path]] = []
        real_run_process = workflow.run_process

        def spy(argv, **kwargs):
            captured.append((list(argv), kwargs["cwd"]))
            return real_run_process(argv, **kwargs)

        with mock.patch.object(workflow, "run_process", side_effect=spy):
            result = self.scan()
        self.assertEqual(result["status"], "passed")
        ((argv, cwd),) = captured
        expected = secret_gate(template_gates())["argv"]
        # 命令与扫描脚本（含精确摘要白名单）逐字取自模板，只有 {repository} 被渲染成检出路径。
        self.assertEqual(len(argv), len(expected))
        self.assertEqual(argv[:3], expected[:3])
        self.assertEqual(expected[3], "{repository}")
        self.assertEqual(Path(argv[3]), self.work / "secret-scan-checkout")
        self.assertEqual(Path(cwd), self.work / "secret-scan-checkout")


class PreflightSecretScanReportTest(CoveredTagsFixture):
    """经 run_preflight 端到端：第七项写进报告，不通过时进入 blockers。"""

    def run_report(self, gates: list[dict]) -> dict:
        request = {
            "plan_id": "preflight-secret-scan",
            "repository": {"managed_ref": "refs/heads/main"},
            "upstream": self.upstream(tag="v1.0.1"),
            "gates": gates,
        }
        request_path = self.root.parent / "request.json"
        request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
        # 保留真实临时 worktree 与试合并；测试仓库不含扫描器与 Go 模块，扫描类步骤一律 mock 成“没有扫描器”。
        with mock.patch.object(workflow, "load_request", return_value=request), mock.patch.object(
            workflow, "run_egress_snapshot", side_effect=UpstreamMergeError("测试仓库不包含扫描器")
        ), mock.patch.object(
            workflow, "_preflight_upstream_sink_scan", side_effect=UpstreamMergeError("测试仓库不包含扫描器")
        ), mock.patch.object(
            workflow, "tool_bundle_disturbance", return_value={"status": "passed", "changed_paths": []}
        ):
            return workflow.run_preflight(request_path, self.root)

    def assert_repository_untouched(self) -> None:
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(git(self.root, "worktree", "list", "--porcelain").count("worktree "), 1)

    @staticmethod
    def secret_blockers(result: dict) -> list[str]:
        return [item for item in result["blockers"] if "主干密钥扫描" in item]

    def test_clean_trunk_passes_and_adds_no_blocker(self) -> None:
        result = self.run_report(template_gates())
        secret_scan = result["report"]["secret_scan"]
        self.assertEqual(secret_scan["status"], "passed")
        self.assertEqual(secret_scan["gate_id"], SECRET_SCAN_GATE_ID)
        self.assertEqual(secret_scan["source_commit"], result["repository"]["fork_head"])
        self.assertEqual(self.secret_blockers(result), [])
        self.assert_repository_untouched()

    def test_hit_on_trunk_blocks_with_actionable_message(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src/leak.txt").write_text(f"token={HIT}\n", encoding="utf-8")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "主干上带了命中串")
        result = self.run_report(template_gates())
        secret_scan = result["report"]["secret_scan"]
        self.assertEqual(secret_scan["status"], "failed")
        self.assertIn("src/leak.txt", secret_scan["output"])
        self.assertEqual(result["result"], "blocked")
        (blocker,) = self.secret_blockers(result)
        self.assertIn(f"主干密钥扫描未通过（与 U-4 {SECRET_SCAN_GATE_ID} 同一门禁）", blocker)
        self.assertIn("须在 plan-create 前于主干处置", blocker)
        self.assertIn("删除真实凭据", blocker)
        self.assertIn("按摘要精确放行", blocker)
        self.assertIn("改门禁须经老板同意", blocker)
        self.assertIn("重封基线", blocker)
        self.assert_repository_untouched()

    def test_request_without_secret_scan_gate_blocks_with_reason(self) -> None:
        gates = [gate for gate in template_gates() if gate["category"] != "secret_scan"]
        result = self.run_report(gates)
        secret_scan = result["report"]["secret_scan"]
        self.assertEqual(secret_scan["status"], "failed")
        self.assertIsNone(secret_scan["gate_id"])
        (blocker,) = self.secret_blockers(result)
        # 没跑出结论时说明具体原因，不再误导成“删除真实凭据”。
        self.assertIn("主干密钥扫描未通过（与 U-4 secret_scan 同一门禁）", blocker)
        self.assertIn("必须恰好有一个", blocker)
        self.assertNotIn("删除真实凭据", blocker)
        self.assertEqual(result["result"], "blocked")
        self.assert_repository_untouched()


if __name__ == "__main__":
    unittest.main()
