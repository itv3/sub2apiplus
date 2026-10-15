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


def stat_of(pid: int) -> str:
    """``ps`` 看到的进程状态（T 开头＝STOPPED；空串＝进程已不在）。macOS 与 Linux 一致。"""

    try:
        completed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                                   env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return "?"
    return completed.stdout.strip()


def stopped(pid: int) -> bool:
    return stat_of(pid)[:1] == "T"


def flaky_reader(real, victim_pid: int, *, failures: int | None, sticky: bool = True, forge=None):
    """包一层「读单个进程启动标识」的函数，模拟冻结之后复核的瞬时故障。

    冻结之前（受害者还没被 SIGSTOP）一律放行，否则根本冻不住；从读到它处于 STOPPED 状态那一次起开始注入：注入 ``failures`` 次
    （``None``＝一直）。``sticky``＝一旦开始注入，即使之后它被解冻（SIGCONT）也继续注入——模拟「故障不是瞬时的」；非 sticky 只在它
    停着的时候注入。``forge`` 给了就不抛异常，改成把读到的结果交给它改写（模拟读到的是别的进程）。返回 (读取函数, 计数器)。"""

    counter = {"n": 0, "armed": False}

    def reader(pid):
        if pid == victim_pid:
            frozen_now = stopped(pid)
            if frozen_now:
                counter["armed"] = True
            if (frozen_now or (sticky and counter["armed"])) and (failures is None or counter["n"] < failures):
                counter["n"] += 1
                if forge is None:
                    raise OSError(errno.EMFILE, "Too many open files (injected)")
                return forge(real(pid))
        return real(pid)

    return reader, counter


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
        return {"pid": leader.pid, "pid_start": bv.process_identity(leader.pid)}


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
    for fault in ("table-raises", "empty-table", "proc-listing-oserror", "ps-fails", "proc-garbage", "snapshot-fails-in-grace-loop",
                  "kill-snapshot-raises"):
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


# ---------------------------------------------------------------------------------------------------------------------
# 重要 2：冻结后的身份复核瞬时失败不得留下 STOPPED 的进程（执行器与后台验证各一套）
# ---------------------------------------------------------------------------------------------------------------------


class _FrozenVerifyCases:
    """两个模块共用的用例；子类给出：``reader_module``／``reader_name``（注入点）、``kill(victims, notes)``。"""

    def setUp(self) -> None:   # noqa: N802
        super().setUp()
        self.victims: list[subprocess.Popen] = []
        patcher = mock.patch.object(self.module, "KILL_VERIFY_RETRY_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spawn(self) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.marker], start_new_session=True)
        self.victims.append(process)
        self.addCleanup(self.reap, process)
        wait_until(lambda: self.known(process.pid), "进程表里看不到刚起的进程")
        return process

    def flaky(self, victim: subprocess.Popen, *, failures: int | None, sticky: bool = True, forged: str | None = None):
        reader, counter = flaky_reader(getattr(self.module, self.reader_name), victim.pid, failures=failures, sticky=sticky,
                                       forge=(lambda real: self.forge(real, forged)) if forged is not None else None)
        return mock.patch.object(self.module, self.reader_name, side_effect=reader), counter

    def assert_not_stopped(self, *victims: subprocess.Popen) -> None:
        for victim in victims:
            wait_until(lambda: not stopped(victim.pid), f"进程 {victim.pid} 被留在 STOPPED（T）状态：{stat_of(victim.pid)!r}", seconds=10)

    def test_verification_fails_twice_then_succeeds_the_process_is_still_killed(self) -> None:
        victim = self.spawn()
        patch, counter = self.flaky(victim, failures=2)
        notes: list[str] = []
        with patch:
            killed = self.kill([victim], notes)
        self.assertEqual(counter["n"], 2, "复核真的失败了两次")
        self.assertIn(victim.pid, killed)
        self.assertEqual(victim.wait(timeout=10), -signal.SIGKILL)
        self.assertEqual(notes, [], "重试成功就没有告警")

    def test_verification_fails_until_the_last_attempt_then_succeeds(self) -> None:
        victim = self.spawn()
        patch, counter = self.flaky(victim, failures=self.module.KILL_VERIFY_ATTEMPTS - 1)
        notes: list[str] = []
        with patch:
            killed = self.kill([victim], notes)
        self.assertIn(victim.pid, killed, "第 N 次（最后一次）才成功也算成功")
        self.assertEqual(victim.wait(timeout=10), -signal.SIGKILL)

    def test_verification_that_never_succeeds_unfreezes_and_warns_instead_of_leaving_stopped(self) -> None:
        victim = self.spawn()
        patch, _counter = self.flaky(victim, failures=None, sticky=True)   # 冻结之后读它的启动标识永远失败，解冻后也一样
        notes: list[str] = []
        began = time.monotonic()
        with patch:
            self.kill([victim], notes)
        self.assertLess(time.monotonic() - began, 10, "重试有界")
        self.assert_not_stopped(victim)
        self.assertIsNone(victim.poll(), "核对不了身份就不盲杀（保护 pid 复用的前提），它只是被解冻")
        self.assertTrue(any(str(victim.pid) in note and "SIGCONT" in note for note in notes), f"要记告警：{notes}")

    def test_process_unfrozen_after_failed_verification_is_swept_once_it_can_be_verified_again(self) -> None:
        """冻结期间读不了、解冻之后又读得了（复核故障是瞬时的）：留下的 SIGCONT 不是终点，后面的扫荡轮次把它杀掉。"""

        victim = self.spawn()
        patch, _counter = self.flaky(victim, failures=None, sticky=False)   # 只在它停着的时候读不了
        notes: list[str] = []
        with patch:
            killed = self.kill([victim], notes)
        self.assertEqual(victim.wait(timeout=10), -signal.SIGKILL)
        self.assertIn(victim.pid, killed)
        self.assertTrue(any("SIGCONT" in note for note in notes), "中间解冻过的要有告警")

    def test_verification_that_finds_another_process_cancels_the_freeze(self) -> None:
        """核对出 pid 已经是别的进程（冻结时被复用）：被冻住的是无辜进程，撤销冻结，更不能杀。"""

        victim = self.spawn()
        patch, _counter = self.flaky(victim, failures=None, sticky=True, forged="forged-start-time")
        notes: list[str] = []
        with patch:
            self.kill([victim], notes)
        self.assert_not_stopped(victim)
        self.assertIsNone(victim.poll(), "启动标识对不上的进程不得被杀")

    def test_only_the_unverifiable_process_is_spared_the_others_are_killed(self) -> None:
        good, flaky = self.spawn(), self.spawn()
        patch, _counter = self.flaky(flaky, failures=None, sticky=True)
        notes: list[str] = []
        with patch:
            killed = self.kill([good, flaky], notes)
        self.assertIn(good.pid, killed)
        self.assertEqual(good.wait(timeout=10), -signal.SIGKILL)
        self.assert_not_stopped(flaky)
        self.assertIsNone(flaky.poll())

    def test_second_interrupt_during_verification_still_finishes_every_frozen_process(self) -> None:
        """第二次 Ctrl-C 落在复核上：中断先记下，其余冻结的进程照常处理，最后才把 KeyboardInterrupt 抛出去，不留 STOPPED。"""

        first, second = self.spawn(), self.spawn()
        real = getattr(self.module, self.reader_name)
        state = {"fired": False}

        def reader(pid):
            if pid == first.pid and stopped(first.pid) and not state["fired"]:
                state["fired"] = True
                raise KeyboardInterrupt
            return real(pid)

        notes: list[str] = []
        with mock.patch.object(self.module, self.reader_name, side_effect=reader), self.assertRaises(KeyboardInterrupt):
            self.kill([first, second], notes)
        self.assertTrue(state["fired"])
        for victim in (first, second):
            self.assertEqual(victim.wait(timeout=10), -signal.SIGKILL, "被冻结的进程在中断之后也要被杀")


_add_fault_tests()


class ExecutorFrozenVerifyTests(_FrozenVerifyCases, base._Fixture):
    module = ue
    reader_name = "_read_start"

    @staticmethod
    def known(pid):
        return pid in ue.process_table()

    @staticmethod
    def forge(real, forged):
        return None if real is None else (forged, real[1])

    def kill(self, victims, notes):
        table = ue.process_table()
        infos = {victim.pid: table[victim.pid] for victim in victims}
        return ue.kill_processes(lambda: {pid: info for pid, info in infos.items() if self.state_alive(pid)}, notes)

    @staticmethod
    def state_alive(pid):
        state = stat_of(pid)
        return bool(state) and not state.startswith("Z")


class ValidationFrozenVerifyTests(_FrozenVerifyCases, base._Fixture):
    module = bv
    reader_name = "_probe"

    @staticmethod
    def known(pid):
        return pid in bv._process_table()

    @staticmethod
    def forge(real, forged):
        return None if real is None else (real[0], real[1], forged, real[3])

    def kill(self, victims, notes):
        starts = {victim.pid: bv._process_table()[victim.pid][2] for victim in victims}
        return bv._kill_snapshot(starts, None, notes)


class SchedulerKillItemsWarnsTests(base._SchedulerFixture):
    """调度器层：冻结后核对不了身份而改发 SIGCONT 的，在事件流里留 ``tree-kill-warning``，且 abort 之后没有进程停在 STOPPED。"""

    def events(self) -> list[dict]:
        path = self.root / "out" / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_abort_leaves_nothing_stopped_and_records_a_warning_when_verification_keeps_failing(self) -> None:
        scheduler = self.scheduler()
        work = self.start(scheduler, "a", "keep")
        grand = int((work / "grand.pid").read_text())
        reader, _counter = flaky_reader(ue._read_start, grand, failures=None, sticky=True)
        with mock.patch.object(ue, "_read_start", side_effect=reader), mock.patch.object(ue, "KILL_VERIFY_RETRY_SECONDS", 0.01):
            scheduler.abort()
        wait_until(lambda: not stopped(grand), "孙辈不能留在 STOPPED", seconds=10)
        self.assertTrue(alive(grand), "核对不了身份的孙辈只被解冻，没有被盲杀")
        warnings = [event for event in self.events() if event["event"] == "tree-kill-warning"]
        self.assertTrue(warnings and any(str(grand) in text for event in warnings for text in event["warnings"]), self.events())
        for process_id in marked_processes(self.marker):
            if process_id != grand:
                self.assertFalse(stopped(process_id), f"进程 {process_id} 停在 STOPPED")
        # 收尾：故障消失后（不再注入）再清一次，孙辈被杀。
        os.kill(grand, signal.SIGKILL)
        self.assert_all_gone()


# ---------------------------------------------------------------------------------------------------------------------
# 重要 3：pid 复用保护（pid_start）
# ---------------------------------------------------------------------------------------------------------------------


class StalePidTests(_Group):
    PARENT = textwrap.dedent(
        '''
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", sys.argv[1] + "-child"], start_new_session=True)
        time.sleep(300)
        '''
    )

    def unrelated_group(self) -> subprocess.Popen:
        """无关的进程组组长，它下面带一个另起会话的子进程（正是旧版只动进程组、新版会连子树一起杀的那种对象）。"""

        parent = subprocess.Popen([sys.executable, "-c", self.PARENT, self.marker], start_new_session=True)
        threading.Thread(target=parent.wait, daemon=True).start()
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        wait_until(lambda: len(marked_processes(self.marker)) == 2, "无关的进程组没有起来")
        return parent

    def test_a_reused_pid_with_a_different_start_identity_is_left_alone(self) -> None:
        parent = self.unrelated_group()
        notes: list[str] = []
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 1.0):
            bv._terminate({"pid": parent.pid, "pid_start": "an-older-process-that-held-this-pid"}, notes)
        time.sleep(0.5)
        self.assertEqual(len(marked_processes(self.marker)), 2, "无关进程组及其另起会话的子进程必须存活")
        self.assertIsNone(parent.poll())
        self.assertTrue(any("复用" in note or "另一个进程" in note for note in notes), f"输出里写明没有动手：{notes}")

    def test_legacy_payload_without_pid_start_only_gets_the_process_group_treatment(self) -> None:
        """旧结论（没有 pid_start）：只按旧行为处理进程组，绝不做整棵子树清理——另起会话的子进程存活。"""

        parent = self.unrelated_group()
        notes: list[str] = []
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 1.0):
            bv._terminate({"pid": parent.pid}, notes)
        wait_until(lambda: parent.poll() is not None, "旧行为：进程组组长被 SIGTERM／SIGKILL")
        self.assertEqual(len(marked_processes(self.marker)), 1, "另起会话的子进程不在进程组里，旧行为不动它")
        self.assertTrue(any("pid_start" in note for note in notes), notes)

    def test_matching_start_identity_cleans_the_whole_tree(self) -> None:
        parent = self.unrelated_group()
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 1.0):
            notes = bv._terminate({"pid": parent.pid, "pid_start": bv.process_identity(parent.pid)})
        self.assertEqual(notes, [])
        wait_until(lambda: not marked_processes(self.marker), "核对通过后整棵子树（含另起会话的）被清掉")

    def test_leader_gone_but_group_alive_still_cleans_the_tree(self) -> None:
        """run 进程已经退出而同组成员还在（10-14 事故的形态）：内核在进程组存在期间不会复用这个 pid 号，放行子树清理。"""

        member = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1] + '-detached'], start_new_session=True)\n"
            "time.sleep(300)\n"
        )
        script = textwrap.dedent(
            '''
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1] + "-member"])
            time.sleep(0.8)
            '''
        )
        leader = subprocess.Popen([sys.executable, "-c", script, self.marker, member], start_new_session=True)
        identity = bv.process_identity(leader.pid)
        wait_until(lambda: len(marked_processes(self.marker)) == 3, "成员没有起来")
        leader.wait(timeout=20)   # 组长走了并被回收，同组成员还在
        self.assertTrue(bv._group_alive(leader.pid))
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 1.0):
            notes = bv._terminate({"pid": leader.pid, "pid_start": identity})
        self.assertEqual(notes, [])
        wait_until(lambda: not marked_processes(self.marker), "组长已走时同组成员与另起会话的后代都要清掉")

    def test_unreadable_start_identity_falls_back_to_the_group_with_a_note(self) -> None:
        leader, work = self.signal_group("record")
        payload = self.payload(leader)
        with mock.patch.object(bv, "_probe", side_effect=OSError(errno.EMFILE, "Too many open files")):
            notes = bv._terminate(payload)
        self.assertTrue((work / "leader.termed").exists())
        wait_until(lambda: not bv._group_alive(leader.pid), "整组退出")
        self.assertTrue(any("核对不了身份" in note for note in notes), notes)

    def test_process_identity_is_stable_and_none_for_missing_pids(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.marker], start_new_session=True)
        self.addCleanup(self.reap, child)
        wait_until(lambda: bv.process_identity(child.pid) is not None, "读不到刚起的进程的启动标识")
        first = bv.process_identity(child.pid)
        self.assertTrue(first)
        self.assertEqual(bv.process_identity(child.pid), first)
        self.assertIsNone(bv.process_identity(2 ** 22 + 4321))


# ---------------------------------------------------------------------------------------------------------------------
# 重要 4：真实进程的「相似环境标记」旁观者
# ---------------------------------------------------------------------------------------------------------------------


class LookalikeMarkerBystanderTests(base._SchedulerFixture):
    """标记按 ``:`` 分段、整段精确匹配。旁观者（同 uid、不是单元的后代）的环境标记是目标标记的前缀、超串、截断、链里别的段……
    在 abort、超时、预约中止（SIGKILL 档）、单元退出后的残留清理之后都必须存活；对照组（环境里带完全相同的标记，或标记链里有一段相同）
    必须被清掉——否则「旁观者存活」可能只是因为根本没有按标记找过。"""

    def lookalike_values(self, tag: str) -> dict[str, str]:
        # 全部带一个非十六进制字符或长度不同，保证不可能恰好等于随机生成的十六进制标记
        return {
            "prefix": tag[:8],
            "truncated": tag[:-1],
            "superstring": tag + "x",
            "prepended": "x" + tag,
            "other-chain": "deadbeefdeadbeef:cafebabecafebabe",
            "chain-with-superstring-segments": f"deadbeefdeadbeef:{tag}x:x{tag}",
        }

    def spawn_with_tag(self, name: str, value: str, marker: str) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", marker + "-" + name], start_new_session=True,
                                   env={**os.environ, ue.RUN_TAG_ENV: value})
        self.bystanders.append(process)
        return process

    def arrange(self, scheduler: ue.Scheduler, mode: str, *, timeout: float | None = None):
        work = self.start(scheduler, "u", mode, timeout=timeout)
        item = next(iter(scheduler.running.values()))
        lookalikes = {name: self.spawn_with_tag(name, value, self.bystander_marker) for name, value in self.lookalike_values(item.tag).items()}
        controls = {"exact": self.spawn_with_tag("exact", item.tag, self.marker),
                    "exact-in-chain": self.spawn_with_tag("chain", f"deadbeefdeadbeef:{item.tag}", self.marker)}
        everyone = [process.pid for process in (*lookalikes.values(), *controls.values())]
        wait_until(lambda: all(pid in ue.process_table() for pid in everyone), "旁观者没有进入进程表")
        wait_until(lambda: {process.pid for process in controls.values()} <= ue.tag_index(ue.process_table()).get(item.tag, set()),
                   "对照组的标记没有被进程表读到（环境读取有问题，旁观者存活就不能说明什么）")
        return work, item, lookalikes, controls

    def check_survivors(self, lookalikes, controls) -> None:
        for name, process in lookalikes.items():
            self.assertTrue(alive(process.pid), f"相似标记的旁观者 {name} 被误杀")
        self.assert_all_gone("单元树和带完全相同标记的对照组必须被清掉")
        for name, process in controls.items():
            wait_until(lambda: process.poll() is not None or not alive(process.pid), f"对照组 {name} 应被清掉")

    def test_abort_spares_lookalike_markers(self) -> None:
        scheduler = self.scheduler()
        _work, _item, lookalikes, controls = self.arrange(scheduler, "keep")
        scheduler.abort()
        self.check_survivors(lookalikes, controls)

    def test_timeout_cleanup_spares_lookalike_markers(self) -> None:
        scheduler = self.scheduler()
        _work, _item, lookalikes, controls = self.arrange(scheduler, "keep", timeout=1.0)
        outcomes = scheduler.reap(block=True)
        self.assertTrue(outcomes[0].timed_out)
        self.check_survivors(lookalikes, controls)

    def test_reservation_sigkill_stage_spares_lookalike_markers(self) -> None:
        scheduler = self.scheduler()
        _work, item, lookalikes, controls = self.arrange(scheduler, "keep")
        scheduler._signal_session(item.pid, signal.SIGKILL)
        scheduler.reap(block=True)
        self.check_survivors(lookalikes, controls)

    def test_orphan_cleanup_after_the_unit_exits_spares_lookalike_markers(self) -> None:
        scheduler = self.scheduler()
        _work, _item, lookalikes, controls = self.arrange(scheduler, "exit")
        outcomes = scheduler.reap(block=True)
        self.assertEqual(len(outcomes), 1)
        self.check_survivors(lookalikes, controls)


# ---------------------------------------------------------------------------------------------------------------------
# 重要 4：嵌套执行器——内层 abort 保护祖先、兄弟与自己；外层 stop 把内层单元当后代清掉
# ---------------------------------------------------------------------------------------------------------------------

# 外层单元的根：除了启动内层调度器的进程外，还有两个与内层无关的「兄弟」（一个另起会话、一个同会话）。
SHIM_SCRIPT = textwrap.dedent(
    r'''
    import json, os, subprocess, sys, time
    from pathlib import Path

    repo, marker, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    SLEEP = "import time; time.sleep(300)"
    sibling = subprocess.Popen([sys.executable, "-c", SLEEP, marker + "-sibling"], start_new_session=True)
    plain = subprocess.Popen([sys.executable, "-c", SLEEP, marker + "-plain"])
    driver = subprocess.Popen([sys.executable, "-B", str(work / "driver.py"), repo, marker, str(work), str(os.getpid()), str(sibling.pid),
                               str(plain.pid)], start_new_session=True)
    (work / "shim.json").write_text(json.dumps({"shim": os.getpid(), "sibling": sibling.pid, "plain": plain.pid, "driver": driver.pid}))
    while True:
        time.sleep(300)
    '''
)

# 内层调度器：相当于测试里自己起的 ``unit_executor.py run-unit``（另起会话的进程），里面有一个内层单元（嵌套＋孙辈）。
DRIVER_SCRIPT = textwrap.dedent(
    r'''
    import json, os, subprocess, sys, time
    from pathlib import Path

    repo, marker, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    shim_pid, sibling_pid, plain_pid = (int(value) for value in sys.argv[4:7])
    INNER_ABORTS = __INNER_ABORTS__
    sys.path.insert(0, repo)
    from tools.ci import unit_executor as ue


    class Recorder:
        def __init__(self):
            self.currents = {}

        def write(self, item, outcome):
            outcome.extra.setdefault("record_path", "")
            outcome.extra.setdefault("record_sha256", "")
            return {}


    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")


    out = work / "inner-out"
    out.mkdir()
    config = ue.ExecutorConfig(default_parallelism=1, default_quota=ue.Quota(1, 128), quotas={}, splits={}, exclusive=(),
                               unit_timeout_seconds=600, orphan_grace_seconds=0.0, raw={})
    scheduler = ue.Scheduler(out_dir=out, state_dir=work / "inner-state", parallelism=1, machine_cores=4, machine_memory=4096,
                             config=config, policy="p", start=work, recorder=Recorder())
    inner_work = work / "inner"
    inner_work.mkdir()
    unit = ue.Unit(unit_id="inner", module="inner", test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                   command=(sys.executable, "-B", str(work / "nest.py"), marker + "-inner", str(inner_work), "keep"), cwd=str(work))
    scheduler.launch(unit, "formal")
    deadline = time.time() + 30
    while not (inner_work / "ready").exists():
        assert time.time() < deadline, "inner unit did not become ready"
        time.sleep(0.05)
    tracked = {"inner_root": next(iter(scheduler.running)), "nested": int((inner_work / "nested.pid").read_text()),
               "grand": int((inner_work / "grand.pid").read_text())}
    (work / "inner.ready").write_text(json.dumps(tracked))
    if INNER_ABORTS:
        while not (work / "abort-now").exists():
            time.sleep(0.05)
        scheduler.abort()
        survivors = {"inner_root": tracked["inner_root"], "nested": tracked["nested"], "grand": tracked["grand"],
                     "shim": shim_pid, "sibling": sibling_pid, "plain": plain_pid, "driver": os.getpid()}
        (work / "after.json").write_text(json.dumps({name: alive(pid) for name, pid in survivors.items()}))
    while True:
        time.sleep(300)
    '''
)


class NestedExecutorTests(base._SchedulerFixture):
    def launch_nested(self, inner_aborts: bool):
        scheduler = self.scheduler()
        work = self.root / "outer"
        work.mkdir()
        (work / "driver.py").write_text(DRIVER_SCRIPT.replace("__INNER_ABORTS__", repr(inner_aborts)), encoding="utf-8")
        (work / "nest.py").write_text(base.NEST_SCRIPT, encoding="utf-8")
        (work / "shim.py").write_text(SHIM_SCRIPT, encoding="utf-8")
        unit = ue.Unit(unit_id="outer", module="outer", test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                       command=(sys.executable, "-B", str(work / "shim.py"), str(REPO_ROOT), self.marker, str(work)), cwd=str(self.root))
        scheduler.launch(unit, "formal")
        wait_until((work / "inner.ready").exists, "内层单元没有就绪", seconds=40)
        wait_until((work / "shim.json").exists, "外层根没有写出兄弟进程号", seconds=10)
        return scheduler, work, json.loads((work / "inner.ready").read_text()), json.loads((work / "shim.json").read_text())

    def test_inner_abort_protects_its_ancestors_siblings_and_itself(self) -> None:
        scheduler, work, _inner, _outer = self.launch_nested(True)
        bystander = self.bystander()
        (work / "abort-now").touch()
        wait_until((work / "after.json").exists, "内层 abort 没有完成", seconds=40)
        after = json.loads((work / "after.json").read_text())
        for name in ("inner_root", "nested", "grand"):
            self.assertFalse(after[name], f"内层单元的 {name} 应被内层 abort 清掉")
        for name in ("shim", "sibling", "plain", "driver"):
            self.assertTrue(after[name], f"内层 abort 不得动外层的 {name}（祖先、兄弟、自己）")
        self.assertTrue(alive(bystander))
        self.assertTrue(alive(os.getpid()))
        # 外层 abort 再把外层单元里剩下的（兄弟、内层调度器自己）全部带走
        scheduler.abort()
        self.assert_all_gone("外层 abort 之后外层单元里的全部进程都要清掉")
        self.assertTrue(alive(bystander))

    def test_outer_abort_takes_the_inner_units_as_descendants(self) -> None:
        scheduler, _work, inner, outer = self.launch_nested(False)
        bystander = self.bystander()
        # 外层根＋兄弟 2＋内层调度器＋内层单元的根、嵌套、孙辈
        self.assertEqual(len(marked_processes(self.marker)), 7, marked_processes(self.marker))
        scheduler.abort()
        self.assert_all_gone("外层 stop 时内层单元（另起会话的嵌套、孙辈、内层调度器）算外层的后代，必须一并清掉")
        for pid in (*inner.values(), *outer.values()):
            self.assertFalse(alive(pid), f"进程 {pid} 应已被清掉")
        self.assertTrue(alive(bystander))


# ---------------------------------------------------------------------------------------------------------------------
# 一般：abort 的二次中断、预约中止先取快照、冻结防漏网、残留清理的宽限与留证、僵尸与轮数上限
# ---------------------------------------------------------------------------------------------------------------------

# 单元根起一个另起会话的「风暴进程」，它持续 fork 另起会话的睡眠进程；根本身不 fork。杀掉根不会让风暴停下来，
# 必须把风暴进程冻结住才收得干净（对应「预约 SIGKILL 档不走冻结」「stop 路径冻结」这类变异）。
DETACHED_STORM_SCRIPT = textwrap.dedent(
    r'''
    import subprocess, sys, time
    from pathlib import Path

    marker, work = sys.argv[1], Path(sys.argv[2])
    STORMER = (
        "import subprocess, sys, time\n"
        "deadline = time.time() + 40\n"
        "while time.time() < deadline:\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1] + '-storm'], start_new_session=True)\n"
        "    time.sleep(0.003)\n"
    )
    stormer = subprocess.Popen([sys.executable, "-c", STORMER, marker + "-stormer"], start_new_session=True)
    (work / "ready").write_text(str(stormer.pid))
    time.sleep(300)
    '''
)

# 单元根起一个另起会话的孩子后自己立刻退出。孩子收到 SIGTERM 只写个文件不退出，``delay`` 秒后自己退出。
GRACE_SCRIPT = textwrap.dedent(
    r'''
    import os, signal, subprocess, sys, time
    from pathlib import Path

    marker, work, delay = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    CHILD = (
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "work = Path(sys.argv[1])\n"
        "def term(signum, frame):\n"
        "    (work / 'termed').write_text('1')\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "(work / 'child.pid').write_text(str(os.getpid()))\n"
        "end = time.time() + float(sys.argv[2])\n"
        "while time.time() < end:\n"
        "    time.sleep(0.05)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(work), delay, marker + "-child"], start_new_session=True)
    while not (work / "child.pid").exists():
        time.sleep(0.02)
    (work / "ready").write_text("1")
    sys.exit(0)
    '''
)


class _ScriptUnits(base._SchedulerFixture):
    def start_script(self, scheduler: ue.Scheduler, name: str, script: str, *extra: str, timeout: float | None = None) -> Path:
        work = self.root / name
        work.mkdir()
        path = self.root / f"{name}.py"
        path.write_text(script, encoding="utf-8")
        unit = ue.Unit(unit_id=name, module=name, test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                       command=(sys.executable, "-B", str(path), self.marker + "-" + name, str(work), *extra), cwd=str(self.root),
                       timeout_seconds=timeout)
        scheduler.launch(unit, "formal")
        wait_until((work / "ready").exists, f"单元 {name} 没有进入就绪状态")
        return work

    def events(self) -> list[dict]:
        path = self.root / "out" / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ReservationSigkillSnapshotFirstTests(_ScriptUnits):
    def test_a_detached_fork_storm_is_frozen_not_chased(self) -> None:
        for path in ("reservation-sigkill", "abort", "orphan-cleanup"):
            with self.subTest(path=path):
                self.setUp()
                scheduler = self.scheduler()
                work = self.start_script(scheduler, "storm", DETACHED_STORM_SCRIPT)
                wait_until(lambda: len(marked_processes(self.marker)) >= 8, "fork 风暴没有起来")
                item = next(iter(scheduler.running.values()))
                if path == "reservation-sigkill":
                    scheduler._signal_session(item.pid, signal.SIGKILL)
                    scheduler.reap(block=True)
                elif path == "abort":
                    scheduler.abort()
                else:
                    os.kill(item.pid, signal.SIGTERM)   # 单元根自己退出，风暴进程成了孤儿，走残留清理
                    scheduler.reap(block=True)
                self.assert_all_gone(f"{path}: 风暴进程必须被冻住再杀，不能留下新 fork 出来的进程")
                self.assertFalse(alive(int((work / "ready").read_text())))


class OrphanCleanupTests(_ScriptUnits):
    def test_orphans_that_exit_within_the_grace_period_are_neither_signalled_nor_counted(self) -> None:
        scheduler = self.scheduler(grace=10.0)
        work = self.start_script(scheduler, "polite", GRACE_SCRIPT, "1.5")
        began = time.monotonic()
        outcomes = scheduler.reap(block=True)
        self.assertEqual(outcomes[0].orphans, [], "宽限期内自己退出的不算孤儿")
        self.assertFalse((work / "termed").exists(), "宽限期内自己退出的不该收到 SIGTERM")
        self.assertGreaterEqual(time.monotonic() - began, 1.0, "要等它自己退出，而不是立刻动手")
        self.assertLess(time.monotonic() - began, 9.0)
        self.assert_all_gone()

    def test_orphans_that_outlive_the_grace_period_are_counted_signalled_then_killed(self) -> None:
        scheduler = self.scheduler(grace=1.0)
        work = self.start_script(scheduler, "stubborn", GRACE_SCRIPT, "300")
        outcomes = scheduler.reap(block=True)
        self.assertEqual(len(outcomes[0].orphans), 1)
        self.assertTrue((work / "termed").exists(), "宽限期过后先礼貌地发 SIGTERM")
        self.assert_all_gone("不退出的孤儿最终被 SIGKILL")


class SurvivorSemanticsTests(_ScriptUnits):
    def test_zombies_are_never_survivors(self) -> None:
        """僵尸（已退出、等父进程回收）不是活着的残留：预约中止后核对「不再有活跃进程」时若把它算进去会误拒授予整机。"""

        scheduler = self.scheduler()
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])   # 不 wait：留成僵尸
        self.addCleanup(zombie.wait)
        wait_until(lambda: stat_of(zombie.pid)[:1] == "Z", "没有造出僵尸")
        table = ue.process_table()
        item = ue.Running(ue.Unit(unit_id="z", module="z", test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                                  command=("true",), cwd=str(self.root)), 2 ** 22 - 1, 0.0, self.root / "l", self.root / "r",
                          self.root / "c", "formal", known={zombie.pid: table[zombie.pid].start})
        self.assertEqual(scheduler._survivors(item, table), {}, "名册里的僵尸不算存活")

    def test_tag_index_skips_zombies_even_when_their_environment_is_readable(self) -> None:
        proc = self.root / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "stat").write_text("1 (self) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0\n")
        for pid, state in ((20, "Z"), (21, "S")):
            (proc / str(pid)).mkdir()
            (proc / str(pid) / "stat").write_text(f"{pid} (cmd) {state} 1 {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {pid}00 100 200\n")
            (proc / str(pid) / "environ").write_bytes(b"UNIT_EXECUTOR_RUN_TAG=feedface\0")
        with mock.patch.object(ue, "PROC_ROOT", proc):
            self.assertEqual(ue.tagged_pids("feedface", ue.process_table()), {21})


class FreezeRoundsAreBoundedTests(base._Fixture):
    """快照永远收敛不了（每轮冒出一个新进程）时，冻结阶段也只做有限轮——不能让 stop／abort 因此永远不返回。"""

    def bounded(self, function, seconds: float = 30.0) -> None:
        thread = threading.Thread(target=function, daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), "冻结轮数没有上限，调用永不返回")

    def test_executor_kill_processes_stops_after_freeze_rounds(self) -> None:
        calls = {"n": 0}

        def endless() -> dict:
            calls["n"] += 1
            pid = 2 ** 22 + 1000 + calls["n"]   # 不存在的 pid：冻结不了，所以下一轮又当「新进程」
            return {pid: ue.ProcInfo(pid, 1, pid, "fake", "S")}

        self.bounded(lambda: ue.kill_processes(endless))
        self.assertLessEqual(calls["n"], ue.FREEZE_ROUNDS + 3, "冻结最多 FREEZE_ROUNDS 轮，扫荡最多 3 轮")

    def test_validation_kill_snapshot_stops_after_freeze_rounds(self) -> None:
        calls = {"n": 0}

        def endless(root, previous, notes):
            calls["n"] += 1
            return {**previous, 2 ** 22 + 1000 + calls["n"]: "fake"}

        with mock.patch.object(bv, "_refresh_snapshot", side_effect=endless):
            self.bounded(lambda: bv._kill_snapshot({}, None, []))
        self.assertLessEqual(calls["n"], bv.FREEZE_ROUNDS + 3)


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
        payload = {"pid": leader.pid, "pid_start": bv.process_identity(leader.pid)}
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


class ValidationDetachedStormTests(base._Fixture):
    def test_terminate_freezes_a_detached_stormer_that_keeps_forking_after_the_leader_dies(self) -> None:
        stormer = (
            "import subprocess, sys, time\n"
            "deadline = time.time() + 40\n"
            "while time.time() < deadline:\n"
            "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1] + '-storm'], start_new_session=True)\n"
            "    time.sleep(0.003)\n"
        )
        script = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1] + '-stormer'], start_new_session=True)\n"
                  "time.sleep(300)\n")
        leader = subprocess.Popen([sys.executable, "-c", script, self.marker, stormer], start_new_session=True)
        threading.Thread(target=leader.wait, daemon=True).start()
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        wait_until(lambda: len(marked_processes(self.marker)) >= 8, "fork 风暴没有起来")
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 5.0):
            bv._terminate({"pid": leader.pid, "pid_start": bv.process_identity(leader.pid)})
        self.assert_all_gone("组长一死风暴进程成了孤儿仍在 fork：必须冻住再杀，不留新 fork 出来的进程")


if __name__ == "__main__":
    unittest.main()
