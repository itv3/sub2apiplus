"""``tools/ci/time_jitter_stress.py`` 的专项回归：烧核进程在主进程被强杀、或超过最长寿命后必须自己退出，不能残留占着 CPU。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest

from tools.ci import time_jitter_stress as stress


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until_gone(pid: int, seconds: float = 40.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return False


class BurnerLifecycleTests(unittest.TestCase):
    def test_burner_exits_by_itself_when_its_parent_is_killed(self) -> None:
        # 中间进程起一个烧核进程后睡觉；kill -9 中间进程（模拟压测主进程被强杀，__exit__ 没机会运行），烧核进程应自己发现父进程没了并退出。
        launcher = textwrap.dedent(
            f"""
            import os, subprocess, sys, time
            source = {stress.BURNER_TEMPLATE!r}.format(lifetime=3600.0, parent=os.getpid())
            burner = subprocess.Popen([sys.executable, "-c", source])
            print(burner.pid, flush=True)
            time.sleep(600)
            """
        )
        parent = subprocess.Popen([sys.executable, "-c", launcher], stdout=subprocess.PIPE, text=True)
        burner_pid = int(parent.stdout.readline())
        self.addCleanup(lambda: alive(burner_pid) and os.kill(burner_pid, signal.SIGKILL))
        self.assertTrue(alive(burner_pid))
        parent.kill()  # 立刻强杀，不等烧核进程跑起来：父进程号是启动方写进去的，所以这个竞态也要能发现
        parent.wait()
        self.assertTrue(wait_until_gone(burner_pid), "主进程被强杀后烧核进程仍在运行")

    def test_burner_exits_after_its_maximum_lifetime(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", stress.burner_source(0.5)])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            self.fail("超过最长寿命后烧核进程仍在运行")
        self.assertEqual(process.returncode, 0)

    def test_context_manager_terminates_every_burner(self) -> None:
        with stress.Burners(2, lifetime=3600) as burners:
            pids = [process.pid for process in burners.processes]
            self.assertTrue(all(alive(pid) for pid in pids))
        self.assertTrue(all(wait_until_gone(pid, 10) for pid in pids))


if __name__ == "__main__":
    unittest.main()
