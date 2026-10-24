"""候选密钥扫描：与 U-4 同一份 secret_scan 门禁，在全量门禁之前对候选工作树先跑一遍。

预检（主干只读检出）与本扫描共用 ``workflow._run_secret_scan_gate``，渲染、占位符与 cwd 校验不会出现两份实现。
测试里的命中串都在运行时拼接，源码里不出现完整的凭据形态字面量。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge import __main__ as cli
from tools.upstream_merge import workflow
from tools.upstream_merge.gate_guard import candidate_secret_scan_from_request
from tools.upstream_merge.tests.test_covered_tags import git
from tools.upstream_merge.tests.test_preflight_secret_scan import HIT, secret_gate, template_gates
from tools.upstream_merge.workflow import (
    CANDIDATE_SECRET_SCAN_SCHEMA,
    _parse_secret_scan_hits,
    scan_candidate_secrets,
)


class CandidateSecretScanTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "candidate"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Candidate Scan Test")
        git(self.root, "config", "user.email", "scan@example.invalid")
        git(self.root, "config", "commit.gpgsign", "false")
        self.write("README.md", "干净的候选树\n")
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

    def test_clean_candidate_passes_and_binds_the_head_commit(self) -> None:
        report = scan_candidate_secrets(self.root, self.gates)
        self.assertEqual(report["schema_version"], CANDIDATE_SECRET_SCAN_SCHEMA)
        self.assertEqual(report["result"], "passed")
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["source_commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(report["scan"]["status"], "passed")
        self.assertEqual(report["scan"]["gate_id"], "10-secret-scan")
        self.assertEqual(report["worktree_status_paths"], [])

    def test_hit_blocks_and_lists_file_and_rule_without_echoing_the_secret(self) -> None:
        self.write("src/leak.txt", f"token={HIT}\n")
        self.write("config/other.txt", f"another={HIT}\n")
        self.commit("命中")
        report = scan_candidate_secrets(self.root, self.gates)
        self.assertEqual(report["result"], "blocked")
        hits = report["scan"]["hits"]
        self.assertEqual(
            sorted((item["path"], item["pattern"]) for item in hits),
            [("config/other.txt", "AKIA[0-9A-Z]{16}"), ("src/leak.txt", "AKIA[0-9A-Z]{16}")],
        )
        (blocker,) = report["blockers"]
        self.assertIn("候选密钥扫描命中", blocker)
        self.assertIn("src/leak.txt（AKIA[0-9A-Z]{16}）", blocker)
        self.assertIn("config/other.txt", blocker)
        self.assertNotIn(HIT, json.dumps(report, ensure_ascii=False))

    def test_working_tree_content_of_tracked_files_is_what_gets_scanned(self) -> None:
        # 扫描对象是候选工作树里已跟踪文件的当前内容（与 U-4 在执行树上跑的一致）；未跟踪文件不在 git ls-files 里。
        self.write("src/app.txt", f"未提交的修改 {HIT}\n")
        self.write("src/untracked.txt", f"{HIT}\n")
        report = scan_candidate_secrets(self.root, self.gates)
        self.assertEqual(report["result"], "blocked")
        self.assertEqual([item["path"] for item in report["scan"]["hits"]], ["src/app.txt"])
        self.assertEqual(sorted(report["worktree_status_paths"]), ["src/app.txt", "src/untracked.txt"])

    def test_exact_digest_allowlist_comes_from_the_same_gate(self) -> None:
        self.write("src/synthetic.txt", f"key={HIT}\n")
        self.commit("合成凭据")
        gate = secret_gate(self.gates)
        gate["argv"][2] = gate["argv"][2].replace("allow={", 'allow={"' + hashlib.sha256(HIT.encode()).hexdigest() + '",', 1)
        self.assertEqual(scan_candidate_secrets(self.root, self.gates)["result"], "passed")

    def test_missing_or_broken_gate_blocks_instead_of_passing(self) -> None:
        without = [gate for gate in self.gates if gate["category"] != "secret_scan"]
        report = scan_candidate_secrets(self.root, without)
        self.assertEqual(report["result"], "blocked")
        self.assertIsNone(report["scan"]["exit_code"])
        self.assertIn("没有跑出结论", report["blockers"][0])
        self.assertIn("必须恰好有一个", report["blockers"][0])
        broken = template_gates()
        secret_gate(broken)["cwd"] = "missing-dir"
        report = scan_candidate_secrets(self.root, broken)
        self.assertEqual(report["result"], "blocked")
        self.assertIn("cwd", report["scan"]["reason"])

    def test_runs_exactly_the_u4_command_rendered_to_the_candidate_tree(self) -> None:
        captured: list[tuple[list[str], Path]] = []
        real = workflow.run_process

        def spy(argv, **kwargs):
            captured.append((list(argv), kwargs["cwd"]))
            return real(argv, **kwargs)

        with mock.patch.object(workflow, "run_process", side_effect=spy):
            self.assertEqual(scan_candidate_secrets(self.root, self.gates)["result"], "passed")
        ((argv, cwd),) = captured
        expected = secret_gate(template_gates())["argv"]
        self.assertEqual(argv[:3], expected[:3])
        self.assertEqual(Path(argv[3]).resolve(), self.root.resolve())
        self.assertEqual(Path(cwd).resolve(), self.root.resolve())

    def test_preflight_and_candidate_scan_share_one_implementation(self) -> None:
        # 两处入口都经过 _run_secret_scan_gate；实现只有一份，渲染与校验不会漂移。
        calls: list[str] = []
        real = workflow._run_secret_scan_gate

        def spy(*args, **kwargs):
            calls.append(kwargs["label"])
            return real(*args, **kwargs)

        work = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(work, ignore_errors=True))
        with mock.patch.object(workflow, "_run_secret_scan_gate", side_effect=spy):
            scan_candidate_secrets(self.root, self.gates)
            workflow._preflight_secret_scan(self.root, git(self.root, "rev-parse", "HEAD"), self.gates, work, dict(os.environ))
        self.assertEqual(calls, ["候选密钥扫描", "主干密钥扫描"])

    def test_hit_parsing_handles_garbage_and_truncation(self) -> None:
        text = "Traceback (most recent call last):\n  ...\nAssertionError: [('a/b.py', 'AKIA[0-9A-Z]{16}'), ('c.txt', 'x')]\n"
        self.assertEqual(
            _parse_secret_scan_hits(text),
            [{"path": "a/b.py", "pattern": "AKIA[0-9A-Z]{16}"}, {"path": "c.txt", "pattern": "x"}],
        )
        self.assertEqual(_parse_secret_scan_hits("AssertionError: [('a', 'b'"), [])
        self.assertEqual(_parse_secret_scan_hits("没有断言信息"), [])
        self.assertEqual(_parse_secret_scan_hits("AssertionError: [1, 2]"), [])

    def test_request_entry_point_reads_gates_from_the_request(self) -> None:
        self.write("src/leak.txt", f"token={HIT}\n")
        self.commit("命中")
        request_path = self.root.parent / "request.json"
        request_path.write_text("{}\n", encoding="utf-8")
        with mock.patch("tools.upstream_merge.gate_guard.load_request", return_value={"gates": self.gates}) as loader:
            report = candidate_secret_scan_from_request(request_path, self.root)
        loader.assert_called_once_with(request_path)
        self.assertEqual(report["result"], "blocked")


class CandidateSecretScanCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "candidate"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Candidate Scan Test")
        git(self.root, "config", "user.email", "scan@example.invalid")
        git(self.root, "config", "commit.gpgsign", "false")
        (self.root / "README.md").write_text("候选树\n", encoding="utf-8")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "base")
        self.request = Path(temporary.name) / "request.json"
        self.request.write_text("{}\n", encoding="utf-8")

    def invoke(self) -> tuple[int, str]:
        stdout = io.BytesIO()
        wrapper = io.TextIOWrapper(stdout, encoding="utf-8")
        argv = ["candidate-secret-scan", "--repository", str(self.root), "--request", str(self.request)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "stdout", wrapper))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch("tools.upstream_merge.gate_guard.load_request", return_value={"gates": template_gates()}))
            stack.enter_context(mock.patch.object(cli, "timing_ledger_path", return_value=(None, None)))
            code = cli.main(argv)
        return code, stdout.getvalue().decode("utf-8")

    def test_command_exit_status_follows_the_verdict(self) -> None:
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["result"], "passed")
        (self.root / "leak.txt").write_text(f"token={HIT}\n", encoding="utf-8")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-q", "-m", "命中")
        code, output = self.invoke()
        self.assertEqual(code, 5)
        document = json.loads(output)
        self.assertEqual(document["result"], "blocked")
        self.assertEqual(document["scan"]["hits"][0]["path"], "leak.txt")


if __name__ == "__main__":
    unittest.main()
