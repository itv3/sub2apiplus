"""门禁可执行文件身份复核（UM-21）：PATH 命令按收据记录的解析路径复核，不随调用者 PATH 漂移。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.gitops import executable_identity
from tools.upstream_merge.workflow import _expected_gate_executable


class GateExecutableTest(unittest.TestCase):
    """两个目录各有一个同名命令：门禁按其中一个执行，之后的复核在另一个 PATH 下进行。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.first = self._tool(self.tmp / "bin-a", "echo a")
        self.second = self._tool(self.tmp / "bin-b", "echo b")
        self.worktree = self.tmp / "worktree"
        self.worktree.mkdir()
        self.plan = SimpleNamespace(worktree=self.worktree, repository_root=self.tmp)
        self.planned = {"argv": ["gate-tool", "run"], "cwd": "."}

    @staticmethod
    def _tool(directory: Path, body: str) -> Path:
        directory.mkdir()
        path = directory / "gate-tool"
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def _recorded(self, directory: Path) -> dict:
        with mock.patch.dict(os.environ, {"PATH": str(directory)}):
            return {"executable": executable_identity("gate-tool", self.worktree)}

    def test_path_command_is_checked_against_recorded_resolution(self) -> None:
        actual = self._recorded(self.first.parent)
        with mock.patch.dict(os.environ, {"PATH": str(self.second.parent)}):
            expected = _expected_gate_executable(self.plan, self.planned, actual, "gate")
        self.assertEqual(expected, actual["executable"])
        self.assertEqual(expected["resolved_path"], str(self.first))

    def test_changed_or_missing_recorded_executable_is_rejected(self) -> None:
        actual = self._recorded(self.first.parent)
        self.first.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "漂移"):
            _expected_gate_executable(self.plan, self.planned, actual, "gate")
        self.first.unlink()
        with self.assertRaisesRegex(UpstreamMergeError, "漂移"):
            _expected_gate_executable(self.plan, self.planned, actual, "gate")

    def test_command_mismatch_and_relative_command_are_still_recomputed(self) -> None:
        actual = self._recorded(self.first.parent)
        with self.assertRaisesRegex(UpstreamMergeError, "command 与计划不一致"):
            _expected_gate_executable(self.plan, {"argv": ["other-tool"], "cwd": "."}, actual, "gate")
        # 相对路径命令仍在执行树里重新解析，内容变化即拒绝。
        scripts = self.worktree / "scripts"
        scripts.mkdir()
        tool = scripts / "run.sh"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
        relative = {"executable": executable_identity("scripts/run.sh", self.worktree)}
        planned = {"argv": ["scripts/run.sh"], "cwd": "."}
        self.assertEqual(_expected_gate_executable(self.plan, planned, relative, "gate")["resolved_path"], str(tool))
        tool.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "漂移"):
            _expected_gate_executable(self.plan, planned, relative, "gate")


if __name__ == "__main__":
    unittest.main()
