"""回退兼容：rollback_backup 旧工具副本与当前工具对同一账本的交叉只读回放。

改造建议第 1 节“回退”通用约定：
- schema 字段与账本事件闭集只增不改，无该字段的历史 Campaign 仍须能 status／replay；
- 引入新事件或新 schema 的变更集，要用 rollback_backup 里的旧工具副本，对写入新事件后的账本与收据做只读回放；
- 旧工具读不了新数据时不回退，保持暂停，用最小修复继续前进。

覆盖：R4 计时账本审核事件（TimingLedgerRollbackReadbackTests）、R11 恢复段复用许可（SegmentReuseRollbackReadbackTests）。

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

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
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


def run_rollback_python(tree: Path, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """在隔离环境里以旧工具副本运行 Python（``-m 模块`` 或 ``-c 脚本``）；只继承最小环境，不读当前仓库。"""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(tree)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(tree),
    }
    return subprocess.run(
        [sys.executable, "-B", *argv],
        cwd=tree,
        env=environment,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


_TREE_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
ROLLBACK_TREE: Path | None = None


def setUpModule() -> None:
    """整个模块只导出一次基线工具树，各改造项的回退用例共用。"""

    global _TREE_DIRECTORY, ROLLBACK_TREE
    if not (REPOSITORY_ROOT / ".git").exists():
        return
    _TREE_DIRECTORY = tempfile.TemporaryDirectory()
    root = Path(_TREE_DIRECTORY.name).resolve()
    root.chmod(0o700)
    ROLLBACK_TREE = root / "rollback-tool"
    ROLLBACK_TREE.mkdir(mode=0o700)
    export_rollback_tool_tree(ROLLBACK_TREE)


def tearDownModule() -> None:
    if _TREE_DIRECTORY is not None:
        _TREE_DIRECTORY.cleanup()


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
        assert ROLLBACK_TREE is not None
        return run_rollback_python(ROLLBACK_TREE, "-m", TIMING_LEDGER_MODULE, *argv)

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


@unittest.skipUnless(
    (REPOSITORY_ROOT / ".git").exists(),
    "传输副本没有 .git，无法导出回退基线；CI 与本机全历史检出必跑",
)
class SegmentReuseRollbackReadbackTests(unittest.TestCase):
    """R11：恢复段复用许可（reuse_job_ids 非空）只能由当前工具执行；历史空复用许可新旧工具都接受。

    回退边界落在后继段派发前的范围核对：基线监督器要求执行集合等于冻结 J*、复用为空，
    因此回退后既有复用许可会被拒绝，需按全量执行重新对账批准，不会被静默执行。
    """

    FROZEN = ["candidate-frozen-aux", "candidate-frozen-core"]

    def _preview(self, *, reuse: list[str]) -> dict[str, object]:
        execute = [job for job in self.FROZEN if job not in reuse]
        return {
            "planned_job_ids": list(self.FROZEN),
            "execute_job_ids": execute,
            "reuse_job_ids": list(reuse),
            "reuse_proofs": {job: {"recovered_from": "ar1"} for job in reuse},
            "expected_new_requests": {"known_total": 0, "known_by_job": {job: 0 for job in execute}, "unknown_job_ids": []},
        }

    def _rollback_violation(self, preview: dict[str, object]) -> object:
        assert ROLLBACK_TREE is not None
        script = (
            "import json, sys\n"
            "from tools.official_client_capture import codex_upgrade_supervisor as supervisor\n"
            "payload = json.loads(sys.stdin.read())\n"
            "print(json.dumps(supervisor.recovery_preview_scope_violation(payload['preview'], payload['frozen']),"
            " ensure_ascii=False))\n"
        )
        completed = run_rollback_python(ROLLBACK_TREE, "-c", script,
                                        stdin=json.dumps({"preview": preview, "frozen": self.FROZEN}))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_historical_empty_reuse_permission_is_accepted_by_both_tools(self) -> None:
        preview = self._preview(reuse=[])
        self.assertIsNone(supervisor.recovery_preview_scope_violation(preview, self.FROZEN))
        self.assertIsNone(self._rollback_violation(preview))

    def test_new_reuse_permission_is_refused_by_rollback_tool(self) -> None:
        preview = self._preview(reuse=["candidate-frozen-aux"])
        self.assertIsNone(supervisor.recovery_preview_scope_violation(preview, self.FROZEN))
        self.assertIn("不等于基线冻结的 J*", str(self._rollback_violation(preview)))


if __name__ == "__main__":
    unittest.main()
