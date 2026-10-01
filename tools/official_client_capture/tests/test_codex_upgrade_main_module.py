"""编排器作为主程序运行时只加载一份（E2-02）。

``python3 -m tools.official_client_capture.codex_upgrade`` 或以脚本方式运行时，reconciler、vc0_closeout 等模块
在函数里延迟导入，它们又 ``from tools.official_client_capture import codex_upgrade``。主程序的模块名是
``__main__``，没有别名时这一步会把 6.3 万行的编排器再编译执行一遍（ARM64 约 1 秒），两份模块里的同名类、
异常与模块级状态也各成一套。本文件用 ``-X importtime`` 核对：两种运行方式下延迟导入 reconciler 确实发生，
而包名形式的编排器一次也没有被导入。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATOR = "tools.official_client_capture.codex_upgrade"


class MainModuleAliasTest(unittest.TestCase):
    def _imported(self, *head: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
        with tempfile.TemporaryDirectory() as directory:
            # 不存在的 Campaign 目录：reconcile-attempt 先延迟导入 reconciler，再在读目录时失败，零写入。
            missing = Path(directory) / "missing-campaign"
            completed = subprocess.run(
                [sys.executable, "-X", "importtime", *head, "reconcile-attempt", "--campaign-dir", str(missing), "--attempt-id", "x"],
                cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=300,
            )
        imported = [line.rsplit("|", 1)[-1].strip() for line in completed.stderr.splitlines() if line.startswith("import time:")]
        return completed, imported

    def test_lazy_imports_reuse_the_running_orchestrator(self) -> None:
        for label, head in (("-m", ("-m", ORCHESTRATOR)), ("脚本", (str(REPO_ROOT / "tools" / "official_client_capture" / "codex_upgrade.py"),))):
            with self.subTest(label):
                completed, imported = self._imported(*head)
                self.assertEqual(completed.returncode, 1, completed.stderr[-2000:])
                self.assertIn("No such file or directory", completed.stderr)
                self.assertTrue(f"{ORCHESTRATOR}_reconciler" in imported, "延迟导入 reconciler 必须真的发生，否则本用例验证不到")
                self.assertFalse(ORCHESTRATOR in imported, "主程序运行时不得再加载第二份编排器")


if __name__ == "__main__":
    unittest.main()
