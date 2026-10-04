"""命令行入口自动时间账本的行为测试。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.upstream_merge.__main__ import (
    TIMING_LEDGER_NAME,
    TIMING_LEDGER_SCHEMA,
    build_parser,
    main,
    timing_ledger_path,
)


def _run_main(argv: list[str]) -> tuple[int, str]:
    """执行入口并捕获 stderr；stdout 需要带 buffer 的替身。"""

    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    stderr = io.StringIO()
    with mock.patch("sys.stdout", new=stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stderr.getvalue()


def _ledger_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TimingLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.draft = self.root / "draft.json"
        self.draft.write_text(json.dumps({"schema_version": "x/v1", "value": 1}) + "\n", encoding="utf-8")

    def test_every_subcommand_accepts_timing_ledger_option(self) -> None:
        parser = build_parser()
        subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        for name, subparser in subparsers.choices.items():
            options = {option for action in subparser._actions for option in action.option_strings}
            self.assertIn("--timing-ledger", options, name)

    def test_default_ledger_lands_next_to_output_and_appends(self) -> None:
        output = self.root / "evidence" / "sealed.json"
        output.parent.mkdir()
        code, _ = _run_main(["identity-seal", "--input", str(self.draft), "--output", str(output)])
        self.assertEqual(code, 0)
        ledger = output.parent / TIMING_LEDGER_NAME
        rows = _ledger_rows(ledger)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["schema_version"], TIMING_LEDGER_SCHEMA)
        self.assertEqual(row["command"], "identity-seal")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["exit_code"], 0)
        self.assertIsNone(row["error"])
        self.assertEqual(row["arguments"]["input"], str(self.draft))
        self.assertEqual(row["arguments"]["output"], str(output))
        self.assertNotIn("timing_ledger", row["arguments"])
        self.assertGreaterEqual(row["duration_seconds"], 0)
        self.assertTrue(row["started_at_utc"].endswith("Z"))
        # 第二次因禁止覆盖而被拒绝，账本仍追加一行且保留第一行。
        code, stderr = _run_main(["identity-seal", "--input", str(self.draft), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertIn("拒绝", stderr)
        rows = _ledger_rows(ledger)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[1]["status"], "rejected")
        self.assertIn("禁止覆盖", rows[1]["error"])

    def test_explicit_ledger_path_wins(self) -> None:
        output = self.root / "out.json"
        ledger = self.root / "ledgers" / "custom.jsonl"
        ledger.parent.mkdir()
        code, _ = _run_main(
            ["identity-seal", "--input", str(self.draft), "--output", str(output), "--timing-ledger", str(ledger)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(_ledger_rows(ledger)), 1)
        self.assertFalse((self.root / TIMING_LEDGER_NAME).exists())

    def test_unwritable_ledger_turns_success_into_system_error(self) -> None:
        output = self.root / "out2.json"
        ledger = self.root / "missing-parent" / "ledger.jsonl"
        code, stderr = _run_main(
            ["identity-seal", "--input", str(self.draft), "--output", str(output), "--timing-ledger", str(ledger)]
        )
        self.assertEqual(code, 3)
        self.assertIn("时间账本写入失败", stderr)
        self.assertTrue(output.exists(), "命令本身已完成，只是账本未落盘")

    def test_ledger_path_inference_order(self) -> None:
        parser = build_parser()
        plan_args = parser.parse_args(["plan-validate", "--plan", "/nonexistent-e/plan.json"])
        self.assertEqual(timing_ledger_path(plan_args), (Path("/nonexistent-e") / TIMING_LEDGER_NAME, None))
        transition_args = parser.parse_args(
            ["revision-preflight", "--plan", "/nonexistent-e/plan.json", "--transition", "/nonexistent-t/node.json"]
        )
        self.assertEqual(timing_ledger_path(transition_args)[0], Path("/nonexistent-e") / TIMING_LEDGER_NAME)
        dry_run_args = parser.parse_args(
            ["freeze-successor-generate", "--before", "a" * 40, "--tag", "t", "--dry-run"]
        )
        self.assertEqual(timing_ledger_path(dry_run_args)[0], None, "只读 dry-run 没有输出锚点时不写账本")

    def test_anchors_inside_a_plan_all_use_the_plan_root_ledger(self) -> None:
        # UM-25：同一 Plan 的命令无论锚点在 evidence、inputs 还是 Plan 工作树里，都写 Plan 根的账本。
        plan_root = self.root / "v0.2.13-20261004-001"
        worktree = plan_root / "worktree"
        worktree.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=worktree, check=True)
        parser = build_parser()
        expected = (plan_root / TIMING_LEDGER_NAME, None)
        plan_args = parser.parse_args(["plan-validate", "--plan", str(plan_root / "evidence" / "plan.json")])
        self.assertEqual(timing_ledger_path(plan_args), expected)
        request_args = parser.parse_args(
            ["preflight", "--request", str(plan_root / "inputs" / "request.json"), "--repository", str(self.root)]
        )
        self.assertEqual(timing_ledger_path(request_args), expected)
        output = worktree / "docs" / "egress" / "maintenance" / "receipt.json"
        output.parent.mkdir(parents=True)
        code, stderr = _run_main(["identity-seal", "--input", str(self.draft), "--output", str(output)])
        self.assertEqual(code, 0, stderr)
        # 收据写进 Plan 工作树时，账本落在 Plan 根（工作树之外），不再漏记。
        self.assertEqual([row["command"] for row in _ledger_rows(plan_root / TIMING_LEDGER_NAME)], ["identity-seal"])
        self.assertFalse((output.parent / TIMING_LEDGER_NAME).exists())
        # 只是名字像 Plan、并不存在的目录不算。
        ghost = parser.parse_args(["plan-validate", "--plan", "/nonexistent/v0.2.13-20261004-002/evidence/plan.json"])
        self.assertEqual(timing_ledger_path(ghost)[0], Path("/nonexistent/v0.2.13-20261004-002/evidence") / TIMING_LEDGER_NAME)

    def test_inferred_ledger_inside_git_worktree_is_skipped(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        output = repository / "docs" / "sealed.json"
        output.parent.mkdir(parents=True)
        code, stderr = _run_main(["identity-seal", "--input", str(self.draft), "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertIn("时间账本未写入", stderr)
        self.assertFalse((output.parent / TIMING_LEDGER_NAME).exists())
        # 显式指定时仍然写入，即使目标在工作树内也尊重用户。
        ledger = repository / "ledger.jsonl"
        output2 = repository / "docs" / "sealed2.json"
        code, _ = _run_main(
            ["identity-seal", "--input", str(self.draft), "--output", str(output2), "--timing-ledger", str(ledger)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(_ledger_rows(ledger)), 1)


if __name__ == "__main__":
    unittest.main()
