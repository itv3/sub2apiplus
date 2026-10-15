"""WP-R5：停止、超时与单元退出后的清理必须覆盖「自己另起会话的后代进程」。

背景（10-15 ARM64 实测）：执行器的单元是 ``posix_spawn(setsid=True)`` 起的，每个单元一个会话；原来的清理只按会话杀
（``Scheduler.abort`` 与 ``_clean_orphans`` 用 ``_session_members``）。``test_claude_real_delivery_integration`` 这类测试自己
再起嵌套的 ``unit_executor.py run-unit`` 并 ``start_new_session``，它不在单元会话里：``background_validation.py stop`` 之后它
成为 PPID=1 的孤儿继续持锁运行，要手工 ``kill -9``。``background_validation._terminate`` 同样只管进程组。

本文件用真实进程复现并锁定修复后的行为（本机 macOS 与 Linux 都能跑，进程表在 macOS 上走 ``ps`` 后备）：

* 单元里嵌套一个 ``start_new_session`` 的进程，它再嵌一层同样另起会话的孙辈，三层都要被清掉；
* 三条清理路径：``Scheduler.abort()``、单元超时清理（含根进程忽略 SIGTERM、要走 SIGKILL 的分支）、单元自己退出后留下孤儿；
  再加整条 CLI 路径（``run-commands`` 收到 SIGTERM）与 ``background_validation._terminate``（SIGTERM→宽限→SIGKILL）；
* 不误杀：测试进程自己、与单元无关的旁观进程、同时在跑的另一个单元；
* pid 复用保护：快照里记的启动时间与现存进程不符时不杀；
* 冻结再终止：持续 fork 新会话的单元被清掉后不留漏网的新进程。

识别进程靠 argv 里独特的标记（每个用例一个随机串），收尾一律按标记 ``SIGKILL``，所以基线上断言失败也不会把孤儿留在机器上。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools.ci import background_validation as bv
from tools.ci import unit_executor as ue

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"

# 单元脚本：单元根进程 → 嵌套进程（自己另起会话，相当于测试里的 ``unit_executor.py run-unit``）→ 孙辈（再另起会话）。
# 参数：标记、工作目录、模式。模式的前缀 ``scrub`` 表示嵌套进程用清空的环境启动（模拟 ``env -i``，标记变量随之丢失）。
NEST_SCRIPT = textwrap.dedent(
    r'''
    import os, signal, subprocess, sys, time
    from pathlib import Path

    marker, work, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    NESTED = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1] + '-grand'],\n"
        "                         start_new_session=True)\n"
        "Path(sys.argv[2]).write_text(str(grand.pid))\n"
        "time.sleep(300)\n"
    )
    if "ignore" in mode:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # 忽略的处置会经 fork／exec 继承给嵌套与孙辈
    env = {"PATH": os.environ.get("PATH", "")} if mode.startswith("scrub") else None
    if mode == "storm":
        # 持续 fork 新会话的睡眠进程，直到被杀：用来检验「先冻结再终止」不留漏网的新进程。
        (work / "ready").write_text("1")
        deadline = time.time() + 30
        while time.time() < deadline:
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", marker + "-storm"], start_new_session=True)
            time.sleep(0.003)
        sys.exit(0)
    nested = subprocess.Popen([sys.executable, "-c", NESTED, marker + "-nested", str(work / "grand.pid")],
                              start_new_session=True, env=env)
    (work / "nested.pid").write_text(str(nested.pid))
    while not (work / "grand.pid").exists():
        time.sleep(0.02)
    (work / "ready").write_text("1")
    if mode.endswith("exit"):
        sys.exit(0)          # 根进程自己先走，留下孤儿
    if mode.endswith("exit-late"):
        time.sleep(1.0)      # 让调度器在根进程在世时多轮询几次，再自己走
        sys.exit(0)
    time.sleep(300)
    '''
)

# bv._terminate 用的进程组：组长（测试起，自己是新会话和新进程组的首进程）→ 执行器替身（同组）→ 单元（另起会话）→ 嵌套 → 孙辈。
GROUP_SCRIPT = textwrap.dedent(
    r'''
    import os, signal, subprocess, sys, time
    from pathlib import Path

    marker, work, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    SLEEPER = "import time; time.sleep(300)"
    NESTED = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1] + '-grand'],\n"
        "                         start_new_session=True)\n"
        "Path(sys.argv[2]).write_text(str(grand.pid))\n"
        "time.sleep(300)\n"
    )
    UNIT = (
        "import subprocess, sys, time\n"
        "nested = subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[1] + '-nested', sys.argv[2]],\n"
        "                          start_new_session=True)\n"
        "time.sleep(300)\n"
    )
    EXECUTOR = (
        "import signal, subprocess, sys, time\n"
        "def late(signum, frame):\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1] + '-late'], start_new_session=True)\n"
        "    time.sleep(1.0)\n"
        "    sys.exit(0)\n"
        "if sys.argv[3] == 'late':\n"
        "    signal.signal(signal.SIGTERM, late)\n"
        "if sys.argv[3] == 'ignore':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "unit = subprocess.Popen([sys.executable, '-c', sys.argv[4], sys.argv[1] + '-unit', sys.argv[2], sys.argv[5]],\n"
        "                        start_new_session=True)\n"
        "time.sleep(300)\n"
    )
    if mode == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    executor = subprocess.Popen([sys.executable, "-c", EXECUTOR, marker + "-executor", str(work / "grand.pid"), mode, UNIT, NESTED])
    while not (work / "grand.pid").exists():
        time.sleep(0.02)
    (work / "ready").write_text("1")
    time.sleep(300)
    '''
)


def marked_processes(marker: str) -> list[int]:
    """argv 里带标记、且还没成僵尸的进程号（``ps`` 在 macOS 与 Linux 上都可用）。"""

    completed = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,stat=,command="], capture_output=True, text=True,
                               env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}, timeout=30)
    pids = []
    for line in completed.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and marker in parts[2] and not parts[1].startswith("Z"):
            pids.append(int(parts[0]))
    return pids


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # 已退出但还没被回收的僵尸不算活着（macOS 与 Linux 一致）。
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(state) and not state.startswith("Z")


def wait_until(condition, message: str, *, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.05)


class _Recorder:
    """调度器只需要 ``write``；这里不落记录库，只留结果。"""

    def __init__(self) -> None:
        self.currents: dict = {}
        self.written: list[ue.Outcome] = []

    def write(self, item: ue.Running, outcome: ue.Outcome) -> dict:
        outcome.extra.setdefault("record_path", "")
        outcome.extra.setdefault("record_sha256", "")
        self.written.append(outcome)
        return {}


class _Fixture(unittest.TestCase):
    """每个用例一个随机标记、一个临时目录；收尾无条件按标记 SIGKILL 残留进程。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.marker = "wpr5" + uuid.uuid4().hex
        self.bystander_marker = "wpr5by" + uuid.uuid4().hex
        self.bystanders: list[subprocess.Popen] = []
        self.addCleanup(self.sweep)

    def sweep(self) -> None:
        for marker in (self.marker, self.bystander_marker):
            for pid in marked_processes(marker):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        for process in self.bystanders:
            self.reap(process)

    @staticmethod
    def reap(process: subprocess.Popen) -> None:
        """杀掉并回收测试自己起的子进程（已经退出的只回收）。"""

        if process.poll() is None:
            process.kill()
        process.wait(timeout=20)

    def bystander(self) -> int:
        """与被测进程树无关的旁观者：测试进程的子进程、自己的会话；任何清理都不得动它。"""

        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.bystander_marker],
                                   start_new_session=True)
        self.bystanders.append(process)
        return process.pid

    def assert_all_gone(self, message: str = "带标记的进程必须全部被清掉") -> None:
        wait_until(lambda: not marked_processes(self.marker), message, seconds=15)
        self.assertEqual(marked_processes(self.marker), [], message)


class _SchedulerFixture(_Fixture):
    """进程内的真实 Scheduler：单元是真的子进程，嵌套与孙辈是真的另起会话的进程。"""

    def scheduler(self, *, grace: float = 0.0, parallelism: int = 2) -> ue.Scheduler:
        out = self.root / "out"
        out.mkdir(exist_ok=True)
        config = ue.ExecutorConfig(default_parallelism=parallelism, default_quota=ue.Quota(1, 128), quotas={}, splits={}, exclusive=(),
                                   unit_timeout_seconds=600, orphan_grace_seconds=grace, raw={})
        self.recorder = _Recorder()
        scheduler = ue.Scheduler(out_dir=out, state_dir=self.root / "state", parallelism=parallelism, machine_cores=4,
                                 machine_memory=4096, config=config, policy="p", start=self.root, recorder=self.recorder)
        self.addCleanup(self.kill_roots, scheduler)
        return scheduler

    def kill_roots(self, scheduler: ue.Scheduler) -> None:
        for pid in list(scheduler.running):
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass

    def unit(self, name: str, mode: str, *, timeout: float | None = None) -> tuple[ue.Unit, Path]:
        work = self.root / name
        work.mkdir()
        script = self.root / "nest.py"
        script.write_text(NEST_SCRIPT, encoding="utf-8")
        unit = ue.Unit(unit_id=name, module=name, test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                       command=(sys.executable, "-B", str(script), self.marker + "-" + name, str(work), mode),
                       cwd=str(self.root), timeout_seconds=timeout)
        return unit, work

    def start(self, scheduler: ue.Scheduler, name: str, mode: str, *, timeout: float | None = None) -> Path:
        unit, work = self.unit(name, mode, timeout=timeout)
        scheduler.launch(unit, "formal")
        wait_until((work / "ready").exists, f"单元 {name} 没有进入就绪状态")
        return work


class SchedulerCleanupTests(_SchedulerFixture):
    """三条清理路径（abort、超时、单元自己退出留下孤儿）加整条 CLI 路径。"""

    # -- 路径一：abort ---------------------------------------------------------------------------
    def test_abort_kills_nested_session_descendants_but_not_bystanders(self) -> None:
        scheduler = self.scheduler()
        bystander = self.bystander()
        works = [self.start(scheduler, name, "keep") for name in ("a", "b")]
        roots = list(scheduler.running)
        # 每个单元：根 + 嵌套 + 孙辈，嵌套与孙辈都在各自的会话里，不是单元会话的成员。
        self.assertEqual(len(marked_processes(self.marker)), 6)
        for work in works:
            nested = int((work / "nested.pid").read_text())
            self.assertEqual(os.getsid(nested), nested, "嵌套进程自成会话，旧的按会话清理够不着它")
        scheduler.abort()
        self.assert_all_gone("abort 之后嵌套与孙辈（各自另起会话）必须一并被杀")
        self.assertEqual(scheduler.running, {})
        for pid in roots:
            self.assertFalse(alive(pid))
        self.assertTrue(alive(bystander), "与单元无关的旁观进程不得被误杀")
        self.assertTrue(alive(os.getpid()))

    def test_abort_kills_descendants_that_scrubbed_the_run_tag(self) -> None:
        """嵌套进程用清空的环境启动（``env -i`` 一类），环境标记丢了；根进程还在世时靠 ppid 子树照样找得到。"""

        scheduler = self.scheduler()
        self.start(scheduler, "a", "scrub-keep")
        self.assertEqual(len(marked_processes(self.marker)), 3)
        scheduler.abort()
        self.assert_all_gone("环境被清空的后代也必须被杀")

    def test_abort_freezes_before_killing_so_a_fork_storm_cannot_leak(self) -> None:
        """单元在持续 fork 新会话的进程时被 abort：先冻结整棵树、直到不再出现新进程，再统一 SIGKILL，不留漏网的。"""

        scheduler = self.scheduler()
        self.start(scheduler, "storm", "storm")
        wait_until(lambda: len(marked_processes(self.marker)) >= 5, "fork 风暴没有起来")
        scheduler.abort()
        self.assert_all_gone("持续 fork 的单元被 abort 后不得留下新进程")

    # -- 路径二：单元超时 --------------------------------------------------------------------------
    def test_timeout_cleanup_kills_nested_session_descendants(self) -> None:
        scheduler = self.scheduler()
        bystander = self.bystander()
        self.start(scheduler, "slow", "keep", timeout=1.0)
        outcomes = scheduler.reap(block=True)
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].timed_out)
        self.assert_all_gone("超时清理之后嵌套与孙辈必须被杀")
        self.assertTrue(alive(bystander))

    def test_timeout_kill_stage_takes_the_whole_subtree_when_root_ignores_sigterm(self) -> None:
        """根进程忽略 SIGTERM，要等 SIGKILL 宽限过了才动手：SIGKILL 这一档也要带上另起会话的后代，不能只杀进程组。"""

        scheduler = self.scheduler(grace=30.0)
        with mock.patch.object(ue, "TIMEOUT_KILL_GRACE_SECONDS", 1.0, create=True):
            self.start(scheduler, "stubborn", "keep-ignore-term", timeout=1.0)
            started = time.monotonic()
            outcomes = scheduler.reap(block=True)
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].timed_out)
        self.assertEqual(outcomes[0].signal, signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 20, "SIGKILL 档已带走子树，根进程退出后的残留清理不该再空等 30 秒宽限")
        self.assert_all_gone("SIGKILL 档之后嵌套与孙辈必须被杀")

    def test_timeout_remembers_scrubbed_descendants_before_the_first_signal(self) -> None:
        """超时路径：嵌套进程环境被清空、周期性名册又来不及刷新（这里把刷新间隔拉到 1000 秒）；根进程收到 SIGTERM 一死 ppid 链就断，
        只有「发信号之前先记名册」才认得出它们。"""

        scheduler = self.scheduler()
        with mock.patch.object(ue, "LEDGER_REFRESH_SECONDS", 1000.0, create=True):
            self.start(scheduler, "scrubbed-slow", "scrub-keep", timeout=1.0)
            outcomes = scheduler.reap(block=True)
        self.assertTrue(outcomes[0].timed_out)
        self.assert_all_gone("超时前记下的名册必须能清掉环境被清空的另起会话后代")

    # -- 路径三：单元自己退出，留下孤儿 ----------------------------------------------------------------
    def test_orphans_left_by_an_exited_unit_are_cleaned_and_reported(self) -> None:
        scheduler = self.scheduler()
        bystander = self.bystander()
        self.start(scheduler, "leaver", "exit")
        outcomes = scheduler.reap(block=True)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].exit_code, 0)
        self.assertEqual(len(outcomes[0].orphans), 2, f"嵌套与孙辈都是孤儿：{outcomes[0].orphans}")
        self.assert_all_gone("单元退出后留下的另起会话的孤儿必须被清理")
        self.assertTrue(alive(bystander))

    def test_orphans_that_ignore_sigterm_are_killed_by_the_escalation(self) -> None:
        """孤儿全部忽略 SIGTERM（处置经 fork／exec 继承）：残留清理先 SIGTERM、一秒后仍在就冻结再 SIGKILL，不能只礼貌地发 SIGTERM。"""

        scheduler = self.scheduler()
        self.start(scheduler, "deaf", "ignore-exit")
        outcomes = scheduler.reap(block=True)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(outcomes[0].orphans), 2)
        self.assert_all_gone("忽略 SIGTERM 的孤儿必须被 SIGKILL 清掉")

    def test_cleanup_of_one_unit_leaves_another_running_unit_alone(self) -> None:
        scheduler = self.scheduler()
        self.start(scheduler, "stays", "keep")
        staying_nested = int((self.root / "stays" / "nested.pid").read_text())
        self.start(scheduler, "leaves", "exit")
        outcomes = []
        wait_until(lambda: outcomes.extend(scheduler.reap(block=False)) or outcomes, "离开的单元没有被回收")
        self.assertEqual([o.unit.unit_id for o in outcomes], ["leaves"])
        wait_until(lambda: not marked_processes(self.marker + "-leaves"), "离开单元的孤儿没有清掉")
        self.assertTrue(alive(staying_nested), "仍在运行的另一个单元的后代不得被误清")
        self.assertEqual(len(marked_processes(self.marker + "-stays")), 3)
        scheduler.abort()
        self.assert_all_gone()

    def test_ledger_catches_scrubbed_orphans_seen_while_the_unit_was_alive(self) -> None:
        """环境被清空的孤儿：根进程退出时 ppid 链已断、标记也没有；靠调度器在单元在世期间记下的后代名册（pid＋启动时间）找到。"""

        scheduler = self.scheduler()
        with mock.patch.object(ue, "LEDGER_REFRESH_SECONDS", 0.1, create=True):
            self.start(scheduler, "scrubbed", "scrub-exit-late")
            outcomes = scheduler.reap(block=True)
        self.assertEqual(len(outcomes), 1)
        self.assert_all_gone("名册里的后代（即使环境被清空、ppid 链已断）必须被清掉")

    def test_reservation_stop_signals_reach_nested_session_descendants(self) -> None:
        """预约中止（整机让给采集批次）用 ``_signal_session`` 先 SIGTERM、宽限后 SIGKILL：两档都要覆盖另起会话的后代，
        否则它们会带着锁留到采集批次里。"""

        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig.name):
                scheduler = self.scheduler()
                self.start(scheduler, "stop-" + sig.name.lower(), "keep")
                item = next(iter(scheduler.running.values()))
                scheduler._signal_session(item.pid, sig)
                self.assert_all_gone(f"{sig.name} 档要带走另起会话的后代")
                outcomes = scheduler.reap(block=True)
                self.assertEqual(len(outcomes), 1)

    # -- 整条 CLI 路径：run-commands 收到 SIGTERM（后台 stop 的第一档） ------------------------------------
    def test_sigterm_to_the_executor_cli_kills_nested_session_descendants(self) -> None:
        script = self.root / "nest.py"
        script.write_text(NEST_SCRIPT, encoding="utf-8")
        work = self.root / "cli"
        work.mkdir()
        manifest = self.root / "commands.json"
        manifest.write_text(json.dumps({"schema_version": ue.COMMANDS_SCHEMA, "units": [
            {"unit_id": "cli", "argv": [sys.executable, "-B", str(script), self.marker + "-cli", str(work), "keep"],
             "cwd": str(self.root), "cores": 1, "memory_mb": 128, "exclusive": False}]}), encoding="utf-8")
        config = self.root / "config.json"
        config.write_text(json.dumps({"schema_version": ue.CONFIG_SCHEMA, "default_parallelism": 1,
                                      "default_quota": {"cores": 1, "memory_mb": 128}, "quotas": {}, "splits": {},
                                      "exclusive": [], "unit_timeout_seconds": 120, "orphan_grace_seconds": 0}), encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-B", str(EXECUTOR), "run-commands", "--manifest", str(manifest), "--config", str(config),
             "--out-dir", str(self.root / "out"), "--state-dir", str(self.root / "state"), "--shared-caches", "off",
             "--cores", "1", "--parallel", "1"],
            cwd=self.root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        wait_until((work / "ready").exists, "CLI 里的单元没有就绪")
        process.terminate()
        _stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 143, stderr)
        self.assert_all_gone("执行器收到 SIGTERM 的中断清理必须带走另起会话的后代")


class ProcessTreePrimitiveTests(_Fixture):
    """进程表、子树、保护集合与「按快照终止」的基本件（含 pid 复用保护）。"""

    def spawn(self, *, new_session: bool = True) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.marker], start_new_session=new_session)

    def test_process_table_reports_parent_session_start_and_state(self) -> None:
        child = self.spawn()
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in ue.process_table(), "进程表里看不到刚起的子进程")
        table = ue.process_table()
        info = table[child.pid]
        self.assertEqual((info.pid, info.ppid, info.sid), (child.pid, os.getpid(), child.pid))
        self.assertTrue(info.start)
        self.assertNotEqual(info.state[:1], "Z")
        self.assertEqual(table[os.getpid()].ppid, os.getppid())

    def test_descendants_follow_ppid_across_sessions_and_protected_excludes_self_and_ancestors(self) -> None:
        child = self.spawn()
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in ue.process_table(), "进程表里看不到刚起的子进程")
        table = ue.process_table()
        self.assertEqual(ue.descendants_of(table, [os.getpid()]) & {child.pid}, {child.pid})
        protected = ue.protected_pids(table)
        self.assertIn(os.getpid(), protected)
        self.assertIn(os.getppid(), protected)
        self.assertNotIn(child.pid, protected)
        self.assertEqual(ue.descendants_of(table, [10 ** 9]), set(), "不存在的根进程给空集合")

    def test_kill_processes_kills_when_identity_matches(self) -> None:
        child = self.spawn()
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in ue.process_table(), "进程表里看不到刚起的子进程")
        killed = ue.kill_processes(lambda: {child.pid: ue.process_table()[child.pid]} if child.poll() is None else {})
        self.assertIn(child.pid, killed)
        self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)

    def test_kill_processes_refuses_when_start_time_does_not_match_pid_reuse_guard(self) -> None:
        """快照里记的启动时间与现存进程不符＝pid 已被别的进程复用：绝不能杀。"""

        child = self.spawn()
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in ue.process_table(), "进程表里看不到刚起的子进程")
        real = ue.process_table()[child.pid]
        forged = ue.ProcInfo(real.pid, real.ppid, real.sid, "forged-start-time", real.state)
        killed = ue.kill_processes(lambda: {child.pid: forged})
        self.assertEqual(killed, [])
        time.sleep(0.3)
        self.assertIsNone(child.poll(), "启动时间对不上的进程不得被杀")
        self.assertTrue(alive(child.pid))

    def test_kill_processes_ignores_zombies_and_vanished_pids(self) -> None:
        child = self.spawn()
        wait_until(lambda: child.pid in ue.process_table(), "进程表里看不到刚起的子进程")
        real_start = ue.process_table()[child.pid]
        child.kill()
        child.wait(timeout=10)
        self.assertEqual(ue.kill_processes(lambda: {child.pid: real_start}), [], "已退出（且已回收）的进程不发信号")

    def test_kill_processes_sweeps_a_child_forked_while_freezing(self) -> None:
        """find 第一轮只看到父进程；冻结期间父进程又 fork 出子进程，下一轮 find 才看到它：子进程也要被杀。"""

        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1] + '-kid'], "
             "start_new_session=True)\ntime.sleep(300)", self.marker], start_new_session=True)
        self.addCleanup(self.reap, parent)
        wait_until(lambda: len(marked_processes(self.marker)) == 2, "父子进程没有起来")
        rounds = []

        def find() -> dict[int, ue.ProcInfo]:
            table = ue.process_table()
            if not rounds:
                rounds.append(1)
                return {parent.pid: table[parent.pid]}
            return {pid: table[pid] for pid in ue.descendants_of(table, [parent.pid]) if table[pid].state[:1] != "Z"}

        ue.kill_processes(find)
        self.assert_all_gone("后一轮 find 才看到的子进程也要杀")

    def test_run_tag_is_found_in_the_environment_of_processes_started_by_the_executor(self) -> None:
        """单元进程的环境里有本次启动独有的标记（嵌套调度器时形成链），清理靠它认出不在 ppid 链上的孤儿。"""

        tag = uuid.uuid4().hex
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.marker],
                                 env={**os.environ, ue.RUN_TAG_ENV: "0a1b2c3d:" + tag}, start_new_session=True)
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in ue.process_table(), "进程表里看不到刚起的子进程")
        wait_until(lambda: child.pid in ue.tagged_pids(tag, ue.process_table()), "按标记找不到带标记的进程（标记链里的任一段都应命中）")
        self.assertNotIn(child.pid, ue.tagged_pids("deadbeef" + tag, ue.process_table()))



class SurvivorSelectionTests(_SchedulerFixture):
    """``Scheduler._survivors`` 的取舍：调用方自己和祖先永远不在内；名册里启动时间对不上的条目（pid 已被复用）不认；
    根进程回收之后不再按 pid 认（pid 可能已被复用），只认会话、名册和标记。"""

    def item(self, scheduler: ue.Scheduler, **fields) -> ue.Running:
        unit = ue.Unit(unit_id="sel", module="sel", test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                       command=("true",), cwd=str(self.root))
        return ue.Running(unit, fields.pop("pid", 2 ** 22 - 1), 0.0, self.root / "l", self.root / "r", self.root / "c", "formal", **fields)

    def test_never_selects_the_caller_or_its_ancestors_even_if_the_ledger_names_them(self) -> None:
        scheduler = self.scheduler()
        table = ue.process_table()
        item = self.item(scheduler, known={os.getpid(): table[os.getpid()].start, os.getppid(): table[os.getppid()].start})
        selected = scheduler._survivors(item, table)
        self.assertNotIn(os.getpid(), selected)
        self.assertNotIn(os.getppid(), selected)

    def test_ledger_entries_with_a_different_start_time_are_ignored(self) -> None:
        scheduler = self.scheduler()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.marker], start_new_session=True)
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in ue.process_table(), "进程表里看不到刚起的子进程")
        table = ue.process_table()
        stale = self.item(scheduler, known={child.pid: "some-other-start-time"})
        self.assertEqual(scheduler._survivors(stale, table), {})
        current = self.item(scheduler, known={child.pid: table[child.pid].start})
        self.assertIn(child.pid, scheduler._survivors(current, table))

    def test_a_reaped_root_pid_is_not_selected_by_number(self) -> None:
        """根进程已回收（不在 running 里）：pid 可能已被别的进程复用，不能再按 pid 认。这里拿一个活着的无关进程的 pid 当作
        「复用后的根 pid」，在 running 里它就被当根，不在 running 里就不被选中。"""

        scheduler = self.scheduler()
        # 不另起会话：它的会话号是测试进程的，只有「按 pid 认根进程」才可能选中它。
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.bystander_marker])
        self.addCleanup(self.reap, other)
        bystander = other.pid
        wait_until(lambda: bystander in ue.process_table(), "进程表里看不到旁观进程")
        table = ue.process_table()
        item = self.item(scheduler, pid=bystander)
        self.assertEqual(scheduler._survivors(item, table), {}, "已回收的根进程号不再按号认")
        scheduler.running[bystander] = item
        try:
            self.assertIn(bystander, scheduler._survivors(item, table), "仍在被跟踪的根进程按号认")
        finally:
            scheduler.running.pop(bystander, None)


class LaunchTagTests(_SchedulerFixture):
    def test_every_launch_gets_its_own_run_tag_in_the_unit_environment(self) -> None:
        sched = self.scheduler()
        sentinel = self.root / "env.txt"
        command = (sys.executable, "-B", "-c",
                   f"import os, pathlib; pathlib.Path({str(sentinel)!r}).write_text(os.environ.get({ue.RUN_TAG_ENV!r}, ''))")
        seen = []
        for name in ("tag1", "tag2"):
            unit = ue.Unit(unit_id=name, module=name, test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                           command=command, cwd=str(self.root))
            sched.launch(unit, "formal")
            tag = sched.running[next(iter(sched.running))].tag
            sched.reap(block=True)
            seen.append((tag, sentinel.read_text()))
            sentinel.unlink()
        self.assertTrue(seen[0][0] and seen[1][0] and seen[0][0] != seen[1][0], "每次启动一个新的随机标记")
        for tag, value in seen:
            self.assertEqual(value, tag, "单元进程的环境里就是这个标记")

    def test_nested_executor_extends_the_tag_chain_instead_of_replacing_it(self) -> None:
        """单元里再起的调度器（外层标记已在环境里）给自己的单元的标记是「外层标记:自己的标记」：外层按任一段都认得出。"""

        sched = self.scheduler()
        sentinel = self.root / "env.txt"
        command = (sys.executable, "-B", "-c",
                   f"import os, pathlib; pathlib.Path({str(sentinel)!r}).write_text(os.environ.get({ue.RUN_TAG_ENV!r}, ''))")
        unit = ue.Unit(unit_id="chain", module="chain", test_ids=(), quota=ue.Quota(1, 128), exclusive=False, weight=1.0,
                       command=command, cwd=str(self.root))
        with mock.patch.dict(os.environ, {ue.RUN_TAG_ENV: "0a1b2c3d"}):
            sched.launch(unit, "formal")
        tag = sched.running[next(iter(sched.running))].tag
        sched.reap(block=True)
        self.assertEqual(sentinel.read_text(), "0a1b2c3d:" + tag)


class FakeProcTests(_Fixture):
    """Linux 分支（读 /proc）在本机 macOS 上也要有覆盖：用假的 /proc 树（与真实 ``/proc/<pid>/stat`` 同样的字段布局）核对解析、
    标记匹配与「pid＋启动时间」判定；``ps`` 后备用假的 ``ps`` 输出核对。"""

    def make_proc(self, rows: dict[int, dict]) -> Path:
        proc = self.root / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "stat").write_text("1 (self) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0\n")
        for pid, row in rows.items():
            (proc / str(pid)).mkdir()
            # pid (comm) state ppid pgrp session tty_nr tpgid flags minflt cminflt majflt cmajflt utime stime cutime cstime
            # priority nice num_threads itrealvalue starttime vsize rss
            (proc / str(pid) / "stat").write_text(
                f"{pid} ({row.get('comm', 'cmd')}) {row.get('state', 'S')} {row['ppid']} {row.get('pgrp', pid)} {row.get('sid', pid)} 0 -1 "
                f"0 0 0 0 0 0 0 0 0 20 0 1 0 {row['start']} 100 200\n")
            if "env" in row:
                (proc / str(pid) / "environ").write_bytes(row["env"])
        return proc

    ROWS = {
        10: {"ppid": 1, "start": 1000, "env": b"A=1\0UNIT_EXECUTOR_RUN_TAG=aaaa0000\0B=2\0"},
        # 命令名里带空格和括号：解析必须按最后一个右括号切
        11: {"ppid": 10, "sid": 11, "start": 1100, "comm": "py (x) y", "env": b"UNIT_EXECUTOR_RUN_TAG=0a0a0a0a:bbbb1111\0"},
        12: {"ppid": 11, "sid": 12, "start": 1200, "env": b"UNIT_EXECUTOR_RUN_TAG_OTHER=bbbb1111\0"},
        13: {"ppid": 1, "start": 1300, "state": "Z", "env": b""},
        14: {"ppid": 1, "start": 1400},   # 没有 environ 文件（不可读）
        15: {"ppid": 1, "start": 1500, "env": b"UNIT_EXECUTOR_RUN_TAG=cccc2222:bbbb1111\0"},
    }

    def test_executor_parses_the_proc_layout_including_awkward_command_names(self) -> None:
        proc = self.make_proc(self.ROWS)
        with mock.patch.object(ue, "PROC_ROOT", proc):
            table = ue.process_table()
        self.assertEqual(set(table), set(self.ROWS))
        info = table[11]
        self.assertEqual((info.ppid, info.sid, info.start, info.state), (10, 11, "1100", "S"))
        self.assertEqual(table[13].state, "Z")
        self.assertEqual(ue.descendants_of(table, [10]), {10, 11, 12})

    def test_executor_matches_the_run_tag_chain_and_skips_zombies_unreadable_and_lookalikes(self) -> None:
        proc = self.make_proc(self.ROWS)
        with mock.patch.object(ue, "PROC_ROOT", proc):
            table = ue.process_table()
            self.assertEqual(ue.tagged_pids("aaaa0000", table), {10})
            self.assertEqual(ue.tagged_pids("bbbb1111", table), {11, 15}, "标记链里的任一段都命中；名字相近的变量、僵尸、读不到的不算")
            self.assertEqual(ue.tagged_pids("0a0a0a0a", table), {11})
            self.assertEqual(ue.tagged_pids("deadbeef", table), set())

    def test_executor_same_process_requires_matching_start_time_and_not_zombie(self) -> None:
        proc = self.make_proc(self.ROWS)
        with mock.patch.object(ue, "PROC_ROOT", proc):
            table = ue.process_table()
            self.assertTrue(ue._same_process(10, table[10]))
            self.assertFalse(ue._same_process(10, ue.ProcInfo(10, 1, 10, "999", "S")), "启动时间不符＝pid 被复用")
            self.assertFalse(ue._same_process(13, table[13]), "僵尸不算")
            self.assertFalse(ue._same_process(99, ue.ProcInfo(99, 1, 99, "1", "S")), "不存在的 pid")

    def test_validation_parses_the_same_layout(self) -> None:
        proc = self.make_proc(self.ROWS)
        with mock.patch.object(bv, "PROC_ROOT", proc):
            table = bv._process_table()
            self.assertEqual(set(table), set(self.ROWS))
            self.assertEqual(table[11], (10, 11, "1100", "S"))
            self.assertEqual(bv._tree_snapshot(10), {10: "1000", 11: "1100", 12: "1200"})
            self.assertEqual(bv._tree_snapshot(None, {11: "1100", 15: "wrong-start"}), {11: "1100", 12: "1200"},
                             "快照里启动时间对不上的条目不作种子")
            with mock.patch.object(bv.os, "kill") as kill:
                self.assertFalse(bv._signal_if_same(10, "999", signal.SIGKILL), "启动时间不符不发信号")
                self.assertTrue(bv._signal_if_same(10, "1000", signal.SIGKILL))
            kill.assert_called_once_with(10, signal.SIGKILL)

    def test_ps_fallback_is_used_without_proc_and_parses_lstart(self) -> None:
        output = ("    1     0 Ss   Sun Oct 11 11:44:58 2026\n"
                  "  200     1 S    Thu Oct 15 17:48:12 2026\n"
                  "  201   200 Z+   Thu Oct 15 17:48:13 2026\n"
                  " garbage line\n")
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with mock.patch.object(ue, "PROC_ROOT", self.root / "no-proc"), mock.patch.object(ue.subprocess, "run", return_value=completed):
            table = ue.process_table()
        self.assertEqual(set(table), {1, 200, 201})
        self.assertEqual((table[200].ppid, table[200].start, table[200].state), (1, "Thu Oct 15 17:48:12 2026", "S"))
        self.assertEqual(table[201].state[:1], "Z")
        validation = subprocess.CompletedProcess([], 0, stdout="  200     1   200 S    Thu Oct 15 17:48:12 2026\n", stderr="")
        with mock.patch.object(bv, "PROC_ROOT", self.root / "no-proc"), mock.patch.object(bv.subprocess, "run", return_value=validation):
            self.assertEqual(bv._process_table(), {200: (1, 200, "Thu Oct 15 17:48:12 2026", "S")})

    def test_empty_process_table_means_nothing_to_kill_not_a_crash(self) -> None:
        with mock.patch.object(ue, "PROC_ROOT", self.root / "no-proc"), mock.patch.object(ue.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(ue.process_table(), {})
        with mock.patch.object(bv, "PROC_ROOT", self.root / "no-proc"), mock.patch.object(bv.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(bv._process_table(), {})
            self.assertEqual(bv._tree_snapshot(12345), {})
            self.assertEqual(bv._kill_snapshot({12345: "x"}, 12345), [])


class BackgroundTerminateTests(_Fixture):
    """``background_validation._terminate``：先取整棵树（含另起会话的后代）的快照，再 SIGTERM→宽限→SIGKILL 快照里的存活进程。"""

    def group(self, mode: str) -> subprocess.Popen:
        script = self.root / "group.py"
        script.write_text(GROUP_SCRIPT, encoding="utf-8")
        work = self.root / "group"
        work.mkdir(exist_ok=True)
        leader = subprocess.Popen([sys.executable, "-B", str(script), self.marker, str(work), mode], start_new_session=True,
                                  stdin=subprocess.DEVNULL)
        # 组长退出后要被及时回收（Linux 上否则僵尸组长会让 killpg(…, 0) 一直成功）。
        threading.Thread(target=leader.wait, daemon=True).start()
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        wait_until((work / "ready").exists, "进程组没有就绪")
        # 组长 + 执行器替身 + 单元 + 嵌套 + 孙辈
        wait_until(lambda: len(marked_processes(self.marker)) == 5, f"进程数不对：{marked_processes(self.marker)}")
        return leader

    def test_group_dies_on_sigterm_but_nested_session_orphans_are_killed_at_once(self) -> None:
        """组长与执行器替身收到 SIGTERM 就退出（相当于执行器没来得及 abort 单元）：单元会话里的后代成了孤儿，
        ppid 链已断。快照在发 SIGTERM 之前取，所以仍能认出并杀掉；整组一退出就动手，不空等 60 秒宽限。"""

        leader = self.group("die")
        bystander = self.bystander()
        began = time.monotonic()
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 25.0):
            bv._terminate({"pid": leader.pid})
        self.assertLess(time.monotonic() - began, 12, "组已经没了就不该空等宽限期")
        self.assert_all_gone("另起会话的单元、嵌套与孙辈必须被清掉")
        self.assertTrue(alive(bystander), "旁观进程不得被误杀")

    def test_descendants_started_during_the_grace_period_are_merged_into_the_snapshot(self) -> None:
        """执行器收到 SIGTERM 后才新起一个另起会话的进程、再活 1 秒就退出（它成了孤儿）：宽限期内每轮并入新出现的后代，
        最终才杀得到它；只用最初的快照（或只在最后重算）找不到它。"""

        leader = self.group("late")
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 25.0):
            bv._terminate({"pid": leader.pid})
        self.assert_all_gone("SIGTERM 之后才出现的另起会话的后代也必须被清掉")

    def test_group_ignoring_sigterm_is_killed_after_grace_together_with_session_descendants(self) -> None:
        leader = self.group("ignore")
        bystander = self.bystander()
        began = time.monotonic()
        with mock.patch.object(bv, "STOP_GRACE_SECONDS", 1.0):
            bv._terminate({"pid": leader.pid})
        self.assertGreaterEqual(time.monotonic() - began, 1.0, "组还活着就要等满宽限期")
        self.assert_all_gone("宽限期过后 SIGKILL 要带上快照里的全部存活进程")
        self.assertTrue(alive(bystander))

    def test_snapshot_records_pid_and_start_time_and_covers_other_sessions(self) -> None:
        leader = self.group("die")
        snapshot = bv._tree_snapshot(leader.pid)
        marked = set(marked_processes(self.marker))
        self.assertTrue(marked <= set(snapshot), f"快照要覆盖整棵树（含另起会话的）：缺 {marked - set(snapshot)}")
        self.assertNotIn(os.getpid(), snapshot, "快照绝不含调用方自己")
        self.assertTrue(all(isinstance(start, str) and start for start in snapshot.values()))
        bv._kill_snapshot(snapshot, leader.pid)
        self.assert_all_gone()

    def test_kill_snapshot_refuses_pids_whose_start_time_changed(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.marker], start_new_session=True)
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in bv._process_table(), "进程表里看不到刚起的子进程")
        self.assertFalse(bv._signal_if_same(child.pid, "forged-start-time", signal.SIGKILL), "第二道保护：发信号前再核对一次")
        self.assertIsNone(child.poll())
        killed = bv._kill_snapshot({child.pid: "forged-start-time"})
        self.assertEqual(killed, [])
        time.sleep(0.3)
        self.assertIsNone(child.poll(), "启动时间对不上＝pid 被复用，不得误杀")
        killed = bv._kill_snapshot({child.pid: bv._process_table()[child.pid][2]})
        self.assertIn(child.pid, killed)
        self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)

    def test_snapshot_never_includes_the_caller_or_its_ancestors(self) -> None:
        snapshot = bv._tree_snapshot(os.getppid())
        self.assertNotIn(os.getpid(), snapshot)
        self.assertNotIn(os.getppid(), snapshot)

    def test_both_implementations_agree_on_the_process_tree(self) -> None:
        """background_validation 为保持 stop 路径独立不加载执行器，所以自带一份进程表；这里核对两份实现对同一时刻的进程表给出相同的子树。"""

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", self.marker], start_new_session=True)
        self.addCleanup(self.reap, child)
        wait_until(lambda: child.pid in bv._process_table(), "进程表里看不到刚起的子进程")
        executor_table = ue.process_table()
        validation_table = bv._process_table()
        self.assertEqual(executor_table[child.pid].ppid, validation_table[child.pid][0])
        self.assertEqual(executor_table[child.pid].start, validation_table[child.pid][2])
        mine = ue.descendants_of(executor_table, [os.getpid()]) - ue.protected_pids(executor_table)
        theirs = set(bv._tree_snapshot(os.getpid()))
        self.assertTrue(child.pid in mine and child.pid in theirs)


if __name__ == "__main__":
    unittest.main()
