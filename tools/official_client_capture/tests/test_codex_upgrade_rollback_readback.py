"""回退兼容：rollback_backup 旧工具副本与当前工具对同一账本的交叉只读回放。

改造建议第 1 节“回退”通用约定：
- schema 字段与账本事件闭集只增不改，无该字段的历史 Campaign 仍须能 status／replay；
- 引入新事件或新 schema 的变更集，要用 rollback_backup 里的旧工具副本，对写入新事件后的账本与收据做只读回放；
- 旧工具读不了新数据时不回退，保持暂停，用最小修复继续前进。

阶段 1 发布时 ARM64 的 rollback_backup 就是 main 基线的受管工具树。这里用 git archive 按固定提交导出同一棵树，
旧工具只在子进程里以 ``-m`` 运行，不与当前模块混用。CI 以 fetch-depth: 0 检出，基线提交必然可读；
没有 .git 的传输副本（ARM64 staging 定向运行）无法取历史，只在这种情况下跳过。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
# main 基线：阶段 1 分支的合并基点，也是阶段 1 发布前 ARM64 受管工具的来源。
ROLLBACK_BASELINE_COMMIT = "e6d53c1e9ccaf406f3bb2159ef2bac5c4cd7666e"
TIMING_LEDGER_MODULE = "tools.official_client_capture.codex_upgrade_timing_ledger"


def export_rollback_tool_tree(destination: Path, commit: str = ROLLBACK_BASELINE_COMMIT) -> Path:
    """把指定提交的 tools/ 目录原样导出到 destination，作为旧工具副本；返回导出根。"""

    completed = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "archive", "--format=tar", commit, "tools"],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"无法从 git 历史导出回退基线 {commit}（CI 必须全历史检出）：{completed.stderr.decode(errors='replace')}"
        )
    with tarfile.open(fileobj=io.BytesIO(completed.stdout)) as archive:
        archive.extractall(destination, filter="data")
    return destination


def run_rollback_tool(tree: Path, module: str, *argv: str) -> subprocess.CompletedProcess[str]:
    """在隔离环境里以旧工具副本运行一个模块入口；只继承最小环境，不读当前仓库。"""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(tree)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(tree),
    }
    return subprocess.run(
        [sys.executable, "-B", "-m", module, *argv],
        cwd=tree,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def ledger_snapshot(ledger_root: Path) -> dict[str, bytes]:
    """账本目录下全部普通文件的相对路径与字节，用来证明只读回放没有改写任何东西。"""

    return {
        path.relative_to(ledger_root).as_posix(): path.read_bytes()
        for path in sorted(ledger_root.rglob("*"))
        if path.is_file()
    }


@unittest.skipUnless(
    (REPOSITORY_ROOT / ".git").exists(),
    "传输副本没有 .git，无法导出回退基线；CI 与本机全历史检出必跑",
)
class TimingLedgerRollbackReadbackTests(unittest.TestCase):
    """R4：stage_review_required 进入计时账本后，旧工具与新工具各自能读什么、读不了时如何失败。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        root = Path(cls._directory.name).resolve()
        root.chmod(0o700)
        cls.tool_tree = root / "rollback-tool"
        cls.tool_tree.mkdir(mode=0o700)
        export_rollback_tool_tree(cls.tool_tree)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        root = Path(self._work.name).resolve()
        root.chmod(0o700)
        control = root / "data" / "control"
        control.mkdir(parents=True, mode=0o700)
        (root / "data").chmod(0o700)
        self.ledger_root = control / "timing-ledger"

    def tearDown(self) -> None:
        self._work.cleanup()

    def _legacy(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return run_rollback_tool(self.tool_tree, TIMING_LEDGER_MODULE, *argv)

    def _legacy_ok(self, *argv: str) -> dict[str, object]:
        completed = self._legacy(*argv)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def _legacy_ledger_at_vc1(self) -> dict[str, object]:
        """旧工具建账本、推进到 VC-1 已开始并封存 checkpoint，模拟阶段 1 发布前已存在的 Campaign。"""

        ledger = str(self.ledger_root)
        self._legacy_ok(
            "create", "--ledger-dir", ledger, "--upgrade-id", "upgrade-rollback",
            "--baseline-version", "0.151.0", "--target-version", "0.154.0",
            "--campaign-purpose", "production_replacement", "--evidence-decision", "recapture",
        )
        self._legacy_ok("append", "--ledger-dir", ledger, "--event-id", "vc0-completed", "--phase", "VC-0",
                        "--event-type", "stage_completed", "--next-action", "启动 VC-1")
        self._legacy_ok("append", "--ledger-dir", ledger, "--event-id", "vc1-started", "--phase", "VC-1",
                        "--event-type", "stage_started", "--next-action", "运行父批次")
        self._legacy_ok("checkpoint", "--ledger-dir", ledger, "--output", "receipts/before-r4.json")
        return self._legacy_ok("replay", "--ledger-dir", ledger, "--receipt", "receipts/before-r4.json")

    def _append_r4_review(self) -> None:
        """当前工具按 R4 收口写入 stage_abandoned 与 stage_review_required。"""

        cause = "rc-" + "a" * 8
        action = "stage_review_required：先按 reservation 分流对账"
        timing_ledger.append_event(self.ledger_root, event_id="vc1-abandoned", phase="VC-1",
                                   event_type="stage_abandoned", root_cause_id=cause,
                                   live_request_count=0, next_action=action)
        timing_ledger.append_event(self.ledger_root, event_id="vc1-review", phase="VC-1",
                                   event_type="stage_review_required", root_cause_id=cause,
                                   live_request_count=0, next_action=action)

    def test_current_tool_reads_legacy_ledger_and_checkpoint(self) -> None:
        """无 R4 字段的历史账本：当前工具 status 可读，旧 checkpoint 重放结果与旧工具逐字段一致，并可继续按 R4 收口。"""

        legacy_replay = self._legacy_ledger_at_vc1()
        current_replay = timing_ledger.replay(self.ledger_root, "receipts/before-r4.json")["summary"]
        self.assertEqual(current_replay, legacy_replay)
        summary = timing_ledger.inspect_ledger(self.ledger_root)
        self.assertEqual((summary["status"], summary["active_phase"]), ("active", "VC-1"))

        self._append_r4_review()
        summary = timing_ledger.inspect_ledger(self.ledger_root)
        self.assertEqual((summary["status"], summary["active_phase"], summary["review_phase"]),
                         ("stage_review_required", None, "VC-1"))

    def test_rollback_tool_fails_closed_on_review_events_without_touching_bytes(self) -> None:
        """写入 stage_review_required 后旧工具 status 必须失败关闭、不改一个字节；此前封存的 checkpoint 仍可由旧工具重放。

        这一条同时记录回退边界：账本一旦写入 R4 事件就不能直接回退到基线工具，只能保持暂停并向前修复。
        """

        legacy_replay = self._legacy_ledger_at_vc1()
        self._append_r4_review()
        before = ledger_snapshot(self.ledger_root)

        status = self._legacy("status", "--ledger-dir", str(self.ledger_root))
        self.assertEqual(status.returncode, 1, status.stdout)
        self.assertIn("event_type 非法", status.stderr)
        self.assertEqual(status.stdout, "")

        replay = self._legacy_ok("replay", "--ledger-dir", str(self.ledger_root), "--receipt", "receipts/before-r4.json")
        self.assertEqual(replay, legacy_replay)
        self.assertEqual(ledger_snapshot(self.ledger_root), before)


if __name__ == "__main__":
    unittest.main()
