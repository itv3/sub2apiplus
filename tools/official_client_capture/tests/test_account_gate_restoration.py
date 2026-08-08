"""劫持 chatgpt.com 的采集脚本必须恢复账号临时熔断状态。

合成 relay 与 HTTP/1.1 探针把 chatgpt.com 指向容器内端口；探针停止后仍在途的真实
出站会拿到 connection refused，Sub2API 据此写入 temp_unschedulable_until。该熔断是
脚手架自身的副作用，不恢复就会让同一 attempt 的后续任务全部拿到 503 no available
accounts —— k15～k18、k23 的候选采集都因此从中途开始连续失败。
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

SCRIPTS = (
    "run_h1_wire_probe.sh",
    "run_images_wire_probe.sh",
    "run_candidate_core_capture.sh",
    "run_candidate_aux_capture.sh",
)


class AccountGateRestorationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1]
        cls.sources = {
            name: (root / name).read_text(encoding="utf-8") for name in SCRIPTS
        }
        cls.paths = {name: root / name for name in SCRIPTS}

    def test_shell_syntax_is_valid(self) -> None:
        for name, path in self.paths.items():
            with self.subTest(script=name):
                result = subprocess.run(
                    ["bash", "-n", str(path)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_每个脚本都在运行前冻结调度门状态(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("account_gate_state()", source)
                self.assertIn("temp_unschedulable_until", source)
                self.assertIn("temp_unschedulable_reason", source)
                # 原值必须先读入变量，否则恢复无基准。
                self.assertRegex(source, r"(account_gate_before|original_gate_state)=\$\(account_gate_state\)")

    def test_恢复写回按运行前值而不是无条件清空(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("restore_account_gate()", source)
                self.assertIn(
                    "temp_unschedulable_until = nullif(convert_from(decode(",
                    source,
                )
                # 无条件置空会掩盖运行前就存在的真实熔断。
                self.assertNotIn(
                    "set temp_unschedulable_until = null, temp_unschedulable_reason = null",
                    source,
                )

    def test_恢复结果失败关闭(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertIn("restore_account_gate", source)
                if name.startswith("run_candidate_"):
                    self.assertIn("restored_gate_equal=true", source)
                    self.assertIn("restore_failed=1", source)
                else:
                    self.assertIn("status=97", source)

    def test_读取失败时拒绝继续(self) -> None:
        for name, source in self.sources.items():
            with self.subTest(script=name):
                self.assertRegex(source, r"调度门(初始)?状态")
                self.assertIn("exit 1", source)


if __name__ == "__main__":
    unittest.main()
