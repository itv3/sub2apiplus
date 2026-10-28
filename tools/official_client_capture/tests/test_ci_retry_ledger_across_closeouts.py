"""补跑累计账本跨收尾生效（优化第 2 项的验收修正 1）。

收尾门禁的记录库按方案独立，原来账本放在记录库里，每次收尾都是一本新账，“最近 5 轮同一单元只许补跑通过 1 次”跨不了收尾。修正后：

* ``codex_closeout`` 把持久账本目录（工作根的上一级）经环境变量 ``UNIT_EXECUTOR_RETRY_LEDGER`` 交给驱动；
* 驱动 ``entry-gates.sh`` 的白名单环境（``env -i``）显式转发这一个变量给执行器（用真实的 bash 执行驱动里那一段验证，不只是文本断言）；
* 执行器读到它就把账本放在那里：两次收尾各有各的记录库，第二次的补跑被第一次留下的账本拒绝。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import codex_closeout as closeout
from tools.ci import retry_policy as rp
from tools.official_client_capture import tests as test_package
from tools.official_client_capture.tests import test_ci_unit_executor_retry as base

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER_SCRIPT = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh"


class ClosureLedgerLocationTests(unittest.TestCase):
    def test_every_closeout_work_root_shares_one_ledger_outside_the_work_root(self) -> None:
        first = closeout.gate_retry_ledger({"work_root": "/data/closeouts/0163-a"})
        second = closeout.gate_retry_ledger({"work_root": "/data/closeouts/0164-b"})
        self.assertEqual(first, second, "两份收尾方案（各自的工作根）必须指向同一本账")
        self.assertEqual(first, Path("/data/closeouts/gate-retry-ledger"))
        self.assertFalse(first.is_relative_to(Path("/data/closeouts/0163-a")), "账本不在工作根里，工作根被清理也带不走它")


class DriverForwardsTheLedgerTests(unittest.TestCase):
    """真实执行驱动脚本里构造白名单环境（``EXEC_ENV``）的那一段 shell，看变量有没有被带进去。"""

    def build_exec_env(self, **environment: str) -> list[str]:
        lines = DRIVER_SCRIPT.read_text(encoding="utf-8").splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("EXEC_ENV=(env -i"))
        end = next(index for index, line in enumerate(lines) if index > start and line.startswith("EXEC_ENV+=(PYTHONPATH"))
        block = "\n".join(lines[start:end + 1])
        script = "EXEC_PATH=/usr/bin:/bin\nPYC=/pyc\nHISTORICAL_SOURCE_ROOT=/hist\nTS=/ts.js\n" + block + '\nprintf "%s\\n" "${EXEC_ENV[@]}"\n'
        completed = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                                   env={"PATH": "/usr/bin:/bin", "HOME": "/home/x", **environment})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.splitlines()

    def test_the_ledger_variable_is_forwarded_when_the_caller_sets_it(self) -> None:
        env = self.build_exec_env(UNIT_EXECUTOR_RETRY_LEDGER="/persist/gate-retry-ledger")
        self.assertIn("UNIT_EXECUTOR_RETRY_LEDGER=/persist/gate-retry-ledger", env)
        self.assertEqual(env[:2], ["env", "-i"], "仍是白名单环境，只多放行了这一个变量")

    def test_nothing_is_added_when_the_caller_does_not_set_it(self) -> None:
        env = self.build_exec_env()
        self.assertFalse([item for item in env if item.startswith("UNIT_EXECUTOR_")], "没设就不带，命令行与改造前相同")

    def test_other_custom_variables_stay_blocked(self) -> None:
        env = self.build_exec_env(UNIT_EXECUTOR_RETRY_LEDGER="/persist/x", UNIT_EXECUTOR_STATE_DIR="/nope", MY_SECRET="s")
        self.assertFalse([item for item in env if item.startswith(("MY_SECRET", "UNIT_EXECUTOR_STATE_DIR"))])


class GateExecutorVariablesDoNotLeakIntoNestedExecutorsTests(unittest.TestCase):
    """收尾门禁给执行器设的环境变量会被单元继承；测试包导入时必须统一剔除，否则测试里嵌套起的临时执行器会改用外层门禁的持久账本。"""

    def test_importing_the_test_package_removes_the_gate_ledger_variable(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", "-c",
             "import os, tools.official_client_capture.tests; print(os.environ.get('UNIT_EXECUTOR_RETRY_LEDGER', '<unset>'))"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UNIT_EXECUTOR_RETRY_LEDGER": "/persist/gate-retry-ledger"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "<unset>")

    def test_every_variable_the_closeout_sets_for_the_gates_is_scrubbed_by_the_test_package(self) -> None:
        """守卫：以后 codex_closeout.run_gates 给门禁进程再加环境变量，却忘了加进测试包的剔除清单，这里就红。"""

        tree = ast.parse(Path(closeout.__file__).read_text(encoding="utf-8"))
        run_gates = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_gates")
        names: set[str] = set()
        for node in ast.walk(run_gates):
            if isinstance(node, ast.Dict) and any(
                key is None and isinstance(value, ast.Attribute) and value.attr == "environ"
                for key, value in zip(node.keys, node.values)
            ):  # 含 **os.environ 的那个环境字典
                names |= {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        self.assertIn("UNIT_EXECUTOR_RETRY_LEDGER", names, "没找到 run_gates 的环境字典，守卫失效")
        # ARM64_VC_ENV 只给驱动自己用（驱动白名单不转发给单元），PYTHONDONTWRITEBYTECODE 无害；其余都会被单元继承，必须在剔除清单里。
        passthrough = {"ARM64_VC_ENV", "PYTHONDONTWRITEBYTECODE"}
        self.assertEqual(sorted(names - passthrough - set(test_package.GATE_EXECUTOR_ENV_NAMES)), [])


class SharedLedgerAcrossCloseoutsTests(base.RetryRunBase):
    """两次“收尾”：记录库各自独立（跟收尾工作根走），账本共用（环境变量指向同一目录）。"""

    def closeout_run(self, number: int, flag: str, ledger: Path, *, share: bool = True) -> tuple[int, dict]:
        self.store = self.root / f"gate-records-{number}"
        unit = self.command_unit("flaky:closeout", [str(self.root / "flaky.py"), str(self.root / flag)])
        extra = {"UNIT_EXECUTOR_RETRY_LEDGER": str(ledger)} if share else {}
        completed, summary, _manifest = self.run_gates([unit], out=f"closeout-{number}", env_extra=extra)
        return completed.returncode, summary

    def test_second_closeout_retry_is_denied_by_the_ledger_the_first_one_left(self) -> None:
        ledger = self.root / "gate-retry-ledger"
        code, first = self.closeout_run(1, "flag-1", ledger)
        self.assertEqual(code, 0)
        self.assertEqual(first["retry"]["retry_passed"], ["flaky:closeout"])
        self.assertTrue(list(ledger.glob("*.jsonl")), "账本写在共享目录里，不在本次的记录库下")
        self.assertFalse((self.root / "gate-records-1" / rp.LEDGER_DIRECTORY).exists())
        # 第二次收尾：另一个记录库、又是偶发失败——账本里已有一次补跑通过，直接判红，不补跑。
        code, second = self.closeout_run(2, "flag-2", ledger)
        self.assertEqual(code, 1)
        marker = second["units"][0]["retry"]
        self.assertEqual(marker["status"], "retry-denied")
        self.assertEqual(marker["ledger"]["prior_retry_passed"], 1)
        starts = [event["kind"] for event in self.events("closeout-2") if event["event"] == "start"]
        self.assertNotIn("retry", starts)

    def test_without_the_shared_ledger_each_closeout_would_start_a_fresh_account(self) -> None:
        """对照：不共享账本（旧行为）时，第二次收尾又能补跑通过——这正是要修的落空。"""

        code, _first = self.closeout_run(1, "flag-1", self.root / "unused", share=False)
        self.assertEqual(code, 0)
        code, second = self.closeout_run(2, "flag-2", self.root / "unused", share=False)
        self.assertEqual(code, 0)
        self.assertEqual(second["retry"]["retry_passed"], ["flaky:closeout"])


if __name__ == "__main__":
    unittest.main()
