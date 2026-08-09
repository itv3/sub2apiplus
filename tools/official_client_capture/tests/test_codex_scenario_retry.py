"""官方 CLI 的偶发波动可以重跑，行为层面的真实差异不行。

s4 偶尔多触发一次 sandbox 信任提示，hook_allowed_count 从 1 变成 2 导致整轮 official
采集（20 分钟）作废。这类波动重跑即消失；但 markers 缺失、密钥泄漏、上游报错是真实
结论，重试掉等于掩盖差异。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from capturelib import scenarios  # noqa: E402


def _summary(**overrides) -> dict:
    base = {
        "valid": False,
        "error_event_count": 0,
        "runtime_secret_exposed": False,
        "return_codes": [0],
        "markers_present": True,
        "unexpected_tool_item_count": 0,
    }
    base.update(overrides)
    return base


class TransientCodexFailureTest(unittest.TestCase):
    def test_hook_计数偏差可以重跑(self) -> None:
        self.assertTrue(scenarios._transient_codex_failure(_summary()))

    def test_已通过不重跑(self) -> None:
        self.assertFalse(scenarios._transient_codex_failure(_summary(valid=True)))

    def test_行为层面的真实差异不得被重试掩盖(self) -> None:
        for label, overrides in (
            ("上游报错", {"error_event_count": 1}),
            ("密钥泄漏", {"runtime_secret_exposed": True}),
            ("CLI 非零退出", {"return_codes": [1]}),
            ("标记缺失", {"markers_present": False}),
            ("出现意外工具项", {"unexpected_tool_item_count": 1}),
        ):
            with self.subTest(case=label):
                self.assertFalse(
                    scenarios._transient_codex_failure(_summary(**overrides))
                )

    def test_重试次数有上限(self) -> None:
        self.assertGreaterEqual(scenarios.CODEX_SCENARIO_RETRY_LIMIT, 1)
        self.assertLessEqual(scenarios.CODEX_SCENARIO_RETRY_LIMIT, 5)

    def test_单次执行入口仍然存在(self) -> None:
        """重试包装不得吃掉原来的单次执行语义。"""

        self.assertTrue(callable(scenarios._run_codex_scenario_once))
        self.assertTrue(callable(scenarios.run_codex_scenario))


if __name__ == "__main__":
    unittest.main()
