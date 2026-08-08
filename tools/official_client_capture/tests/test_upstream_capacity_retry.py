"""上游临时无容量必须重试，其余失败必须原样上报。"""

from __future__ import annotations

import unittest

from tools.official_client_capture.capturelib.scenarios import (
    UPSTREAM_CAPACITY_MESSAGE,
    UPSTREAM_CAPACITY_RETRY_LIMIT,
    _upstream_capacity_error,
)


class UpstreamCapacityRetryTest(unittest.TestCase):
    def test_识别上游无容量错误(self) -> None:
        events = [
            {"type": "thread.started"},
            {"type": "error", "message": f"{UPSTREAM_CAPACITY_MESSAGE}. Please try a different model."},
        ]
        self.assertTrue(_upstream_capacity_error(events))

    def test_其它错误不触发重试(self) -> None:
        for message in (
            "Invalid API key provided",
            "You have hit your usage limit",
            "context_length_exceeded",
            "previous_response_not_found",
        ):
            with self.subTest(message=message):
                events = [{"type": "error", "message": message}]
                self.assertFalse(_upstream_capacity_error(events))

    def test_非错误事件不触发重试(self) -> None:
        events = [
            {"type": "item.completed", "message": UPSTREAM_CAPACITY_MESSAGE},
            {"type": "turn.completed"},
        ]
        self.assertFalse(_upstream_capacity_error(events))

    def test_空事件流不触发重试(self) -> None:
        self.assertFalse(_upstream_capacity_error([]))

    def test_重试次数有上限(self) -> None:
        # 无限重试会把上游长时间不可用拖成假死，必须有界。
        self.assertGreaterEqual(UPSTREAM_CAPACITY_RETRY_LIMIT, 1)
        self.assertLessEqual(UPSTREAM_CAPACITY_RETRY_LIMIT, 10)


if __name__ == "__main__":
    unittest.main()
