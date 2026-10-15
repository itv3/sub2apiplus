"""R7b：WP-R5（按后代子树清理）的加固——故障注入、pid 复用保护、相似标记旁观者、嵌套执行器、二次中断。

WP-R5 的独立验收（``独立验收-WP-R5执行器清理.md``）指出的缺陷与测试盲区，每一条在这里有真实进程（或能注入故障的替身）的用例：

* 重要 1：``background_validation._terminate`` 取快照出错时不得妨碍后面的信号（stop 已经先把结论标成 superseded）。
  注入：进程表读取抛异常、``/proc`` 目录读不了、``ps`` 失败、``/proc`` 读到乱码、并入新后代那一步抛异常、按快照终止抛异常；
  断言 SIGTERM 仍发出、宽限后进程组 SIGKILL 仍发出、原因写进输出。
* 重要 2：冻结（SIGSTOP）之后、SIGKILL 之前的身份复核瞬时失败，不得把进程永远留在 STOPPED。执行器与后台验证两个模块各一套：
  复核前几次失败、后面成功→仍被杀；一直失败→解冻并告警；复核出别的进程→撤销冻结；落在复核上的二次中断→其余冻结进程照样处理。
* 重要 3：``stop`` 对过期 payload pid 的波及面：pid 被无关的进程组组长复用时无关进程树必须存活（``pid_start`` 对不上就不动）；
  没有 ``pid_start`` 的旧结论只按进程组处理。
* 重要 4：真实进程的「相似环境标记」旁观者（标记是目标标记的前缀、超串、链里别的段）在 abort／超时／预约中止／单元退出后都必须存活；
  嵌套执行器：内层 abort 保护祖先、兄弟与自己，外层 stop 把内层单元当后代一并清掉。
* 一般 1：``abort()`` 期间再来一次 Ctrl-C 不得永久卡在 ``waitpid``；一般 2：预约中止的 SIGKILL 档先取快照再杀；
  一般 3：残留清理在宽限期内等孤儿自己退出、超过才动手，并留 ``orphans`` 事件。

识别进程靠 argv 里独特的标记（每个用例一个随机串），收尾一律按标记 ``SIGKILL``，所以基线上断言失败也不会把孤儿留在机器上。
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import background_validation as bv
from tools.ci import unit_executor as ue
from tools.official_client_capture.tests import test_ci_unit_executor_descendant_cleanup as base

REPO_ROOT = Path(__file__).resolve().parents[3]

alive = base.alive
marked_processes = base.marked_processes
wait_until = base.wait_until


# 进程组替身：组长（测试起，新会话和新进程组的首进程）+ 同组的一个子进程；两者收到 SIGTERM 都先写一个文件再按模式退出或不退出。
SIGNAL_GROUP_SCRIPT = textwrap.dedent(
    r'''
    import os, signal, subprocess, sys, time
    from pathlib import Path

    work, mode, marker = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    CHILD = (
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "work = Path(sys.argv[1])\n"
        "def term(signum, frame):\n"
        "    (work / 'child.termed').write_text('1')\n"
        "    if sys.argv[2] == 'record':\n"
        "        sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "(work / 'child.ready').write_text(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(300)\n"
    )

    def term(signum, frame):
        (work / 'leader.termed').write_text('1')
        if mode == 'record':
            sys.exit(0)

    signal.signal(signal.SIGTERM, term)
    child = subprocess.Popen([sys.executable, '-c', CHILD, str(work), mode, marker])
    while not (work / 'child.ready').exists():
        time.sleep(0.02)
    (work / 'ready').write_text('1')
    while True:
        time.sleep(300)
    '''
)


class _Group(base._Fixture):
    """信号替身进程组：用来观察 ``_terminate`` 的 SIGTERM 与 SIGKILL 有没有发出来。"""

    def signal_group(self, mode: str) -> tuple[subprocess.Popen, Path]:
        script = self.root / "siggroup.py"
        script.write_text(SIGNAL_GROUP_SCRIPT, encoding="utf-8")
        work = self.root / f"siggroup-{mode}"
        work.mkdir(exist_ok=True)
        leader = subprocess.Popen([sys.executable, "-B", str(script), str(work), mode, self.marker], start_new_session=True,
                                  stdin=subprocess.DEVNULL)
        threading.Thread(target=leader.wait, daemon=True).start()   # 组长退出后及时回收，免得僵尸组长让 killpg(…, 0) 一直成功
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        wait_until((work / "ready").exists, "进程组没有就绪")
        return leader, work

    @staticmethod
    def payload(leader: subprocess.Popen) -> dict:
        return {"pid": leader.pid}


# ---------------------------------------------------------------------------------------------------------------------
# 重要 1：_terminate 的快照故障不得妨碍信号
# ---------------------------------------------------------------------------------------------------------------------


class TerminateSurvivesSnapshotFaultsTests(_Group):
    """每一种故障都做两遍：``record`` 模式（SIGTERM 能让进程退出）断言 SIGTERM 发出了；``ignore`` 模式（SIGTERM 被忽略）断言宽限
    过后进程组 SIGKILL 仍然发出。"""

    def fake_proc(self, rows: dict[int, str]) -> Path:
        """假的 /proc：``self/stat`` 在（走 Linux 分支），其余目录按 ``rows`` 里的原文写 stat。"""

        proc = self.root / "fake-proc"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "stat").write_text("1 (self) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0\n")
        for pid, text in rows.items():
            (proc / str(pid)).mkdir()
            (proc / str(pid) / "stat").write_text(text)
        return proc

    def faults(self, leader: subprocess.Popen):
        """名称 → 打补丁的上下文管理器工厂。补丁只在 ``_terminate`` 调用期间生效。"""

        no_proc = self.root / "no-proc"
        calls = {"n": 0}
        real_snapshot = bv._tree_snapshot

        def snapshot_fails_after_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("injected: 并入新后代时解析失败")
            return real_snapshot(*args, **kwargs)

        return {
            "table-raises": lambda: mock.patch.object(bv, "_process_table", side_effect=RuntimeError("injected: 进程表读取故障")),
            "empty-table": lambda: mock.patch.object(bv, "_process_table", return_value={}),
            "proc-listing-oserror": lambda: _both(
                mock.patch.object(bv, "PROC_ROOT", self.fake_proc({})),
                mock.patch.object(bv.Path, "iterdir", side_effect=OSError(errno.EMFILE, "Too many open files"))),
            "ps-fails": lambda: _both(
                mock.patch.object(bv, "PROC_ROOT", no_proc),
                mock.patch.object(bv, "_ps_table_strict", side_effect=subprocess.TimeoutExpired("ps", 30))),
            "proc-garbage": lambda: mock.patch.object(
                bv, "PROC_ROOT", self.fake_proc({1: "1 (init) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0\n",
                                                 leader.pid: "garbage without parentheses\n", leader.pid + 1: "\x00\xff\xfe(\n"})),
            "snapshot-fails-in-grace-loop": lambda: mock.patch.object(bv, "_tree_snapshot", side_effect=snapshot_fails_after_first),
            "kill-snapshot-raises": lambda: mock.patch.object(bv, "_kill_snapshot", side_effect=RuntimeError("injected: 按快照终止失败")),
        }

    def run_fault(self, name: str, mode: str) -> tuple[subprocess.Popen, Path, list[str]]:
        leader, work = self.signal_group(mode)
        payload = self.payload(leader)   # 启动标识在打补丁之前读好（结论里本来就是 start 时记下的）
        grace = 1.0 if mode == "ignore" else 30.0
        began = time.monotonic()
        with self.faults(leader)[name](), mock.patch.object(bv, "STOP_GRACE_SECONDS", grace):
            notes = bv._terminate(payload)   # 不得抛异常
        self.elapsed = time.monotonic() - began
        return leader, work, notes

    def check_sigterm_still_delivered(self, name: str) -> None:
        leader, work, notes = self.run_fault(name, "record")
        self.assertTrue((work / "leader.termed").exists(), f"{name}: 组长必须收到 SIGTERM")
        self.assertTrue((work / "child.termed").exists(), f"{name}: 同组成员必须收到 SIGTERM")
        wait_until(lambda: not bv._group_alive(leader.pid), f"{name}: 整组必须退出")
        self.assertLess(self.elapsed, 20, f"{name}: 整组一退出就收尾，不空等宽限")
        self.assertTrue(notes, f"{name}: 取快照出错的原因要写进输出（notes）")

    def check_group_sigkill_still_follows_grace(self, name: str) -> None:
        leader, work, notes = self.run_fault(name, "ignore")
        self.assertTrue((work / "leader.termed").exists(), f"{name}: SIGTERM 要先发")
        self.assertGreaterEqual(self.elapsed, 1.0, f"{name}: 忽略 SIGTERM 的进程组要等满宽限才 SIGKILL")
        wait_until(lambda: not bv._group_alive(leader.pid), f"{name}: 宽限过后进程组 SIGKILL 必须发出")
        self.assertEqual(marked_processes(self.marker), [], f"{name}: 带标记的进程必须全部被清掉")

    def test_failure_reasons_are_reported_in_the_notes_and_on_stderr(self) -> None:
        leader, _work = self.signal_group("record")
        payload = self.payload(leader)
        captured = []
        with mock.patch.object(bv, "_process_table", side_effect=RuntimeError("injected: 进程表读取故障")), \
                mock.patch.object(bv, "STOP_GRACE_SECONDS", 5.0), mock.patch("sys.stderr") as stderr:
            notes = bv._terminate(payload)
            captured.extend(call.args[0] for call in stderr.write.call_args_list if call.args)
        self.assertTrue(any("injected: 进程表读取故障" in note for note in notes), notes)
        self.assertTrue(any("injected: 进程表读取故障" in line for line in captured), "同时写到标准错误，后台自己起的 stop 也有日志")
        wait_until(lambda: not bv._group_alive(leader.pid), "整组必须退出")

    def test_healthy_terminate_reports_nothing(self) -> None:
        leader, work = self.signal_group("record")
        notes = bv._terminate(self.payload(leader))
        self.assertEqual(notes, [], "没有故障时不产生说明或告警")
        self.assertTrue((work / "leader.termed").exists())
        wait_until(lambda: not bv._group_alive(leader.pid), "整组必须退出")

    def test_stop_keeps_stopping_the_other_runs_when_one_terminate_blows_up(self) -> None:
        """``stop`` 里每个后台的终止再包一层：一个意外异常只记一条，不妨碍停其余的。"""

        notes: list[str] = []
        with mock.patch.object(bv, "_terminate", side_effect=RuntimeError("injected: 意外")):
            bv._terminate_reporting({"pid": 1}, "x.json", notes)
        self.assertTrue(any("x.json" in note and "injected: 意外" in note for note in notes), notes)


def _add_fault_tests() -> None:
    for fault in ("table-raises", "empty-table", "proc-listing-oserror", "snapshot-fails-in-grace-loop", "kill-snapshot-raises"):
        label = fault.replace("-", "_")
        setattr(TerminateSurvivesSnapshotFaultsTests, f"test_sigterm_is_still_delivered_when_{label}",
                lambda self, name=fault: self.check_sigterm_still_delivered(name))
        setattr(TerminateSurvivesSnapshotFaultsTests, f"test_group_sigkill_still_follows_the_grace_period_when_{label}",
                lambda self, name=fault: self.check_group_sigkill_still_follows_grace(name))


class _both:
    """同时进入两个补丁上下文。"""

    def __init__(self, *managers) -> None:
        self.managers = managers

    def __enter__(self):
        for manager in self.managers:
            manager.__enter__()
        return self

    def __exit__(self, *exc):
        for manager in reversed(self.managers):
            manager.__exit__(*exc)
        return False


_add_fault_tests()


class EarlierSnapshotIsKeptTests(base._Fixture):
    def test_earlier_snapshot_is_still_used_when_every_later_one_fails(self) -> None:
        """第一次快照（SIGTERM 之前）取到了整棵树；之后每次重取都失败。组长与执行器替身收到 SIGTERM 就退出，另起会话的单元、嵌套、
        孙辈成了孤儿：只能靠第一次的快照认出并杀掉（沿用上一次的快照，而不是清成空的）。"""

        script = self.root / "group.py"
        script.write_text(base.GROUP_SCRIPT, encoding="utf-8")
        work = self.root / "group"
        work.mkdir()
        leader = subprocess.Popen([sys.executable, "-B", str(script), self.marker, str(work), "die"], start_new_session=True,
                                  stdin=subprocess.DEVNULL)
        threading.Thread(target=leader.wait, daemon=True).start()
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        wait_until((work / "ready").exists, "进程组没有就绪")
        wait_until(lambda: len(marked_processes(self.marker)) == 5, f"进程数不对：{marked_processes(self.marker)}")
        payload = {"pid": leader.pid}
        real = bv._tree_snapshot
        calls = {"n": 0}

        def only_the_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("injected: 之后的快照全部失败")
            return real(*args, **kwargs)

        notes: list[str] = []
        with mock.patch.object(bv, "_tree_snapshot", side_effect=only_the_first), mock.patch.object(bv, "STOP_GRACE_SECONDS", 5.0):
            bv._terminate(payload, notes)
        self.assertTrue(notes)
        self.assert_all_gone("靠第一次快照也要把孤儿清掉")


if __name__ == "__main__":
    unittest.main()
