"""``scaling_guard.assert_growth_is_bounded`` 的专项回归。用注入的假计时器，不依赖真实耗时，所以本身不会抖。"""

from __future__ import annotations

import unittest
from typing import Callable

from tools.official_client_capture.tests import scaling_guard as guard


class FakeClock:
    """计时器：被测工作按给定代价函数推进假时间。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.prepared: list[int] = []

    def timer(self) -> float:
        return self.now

    def prepare(self, cost: Callable[[int], float]) -> Callable[[int], Callable[[], None]]:
        def prepare(length: int) -> Callable[[], None]:
            self.prepared.append(length)

            def work() -> None:
                self.now += cost(length)

            return work

        return prepare


class ScalingGuardTests(unittest.TestCase):
    LENGTHS = (120, 240, 480, 960, 1920)

    def run_guard(self, cost: Callable[[int], float], lengths=LENGTHS):
        clock = FakeClock()
        measured = guard.assert_growth_is_bounded(self, clock.prepare(cost), lengths, label="样例", timer=clock.timer)
        return clock, measured

    def test_linear_and_quadratic_growth_pass(self) -> None:
        self.run_guard(lambda length: length * 1e-5)
        self.run_guard(lambda length: (length ** 2) * 1e-8)

    def test_cubic_growth_fails_at_the_first_doubling_without_measuring_longer_inputs(self) -> None:
        clock = FakeClock()
        with self.assertRaises(AssertionError) as caught:
            guard.assert_growth_is_bounded(self, clock.prepare(lambda length: (length ** 3) * 1e-6), self.LENGTHS, label="三次方", timer=clock.timer)
        self.assertIn("三次方", str(caught.exception))
        self.assertIn("120→240", str(caught.exception))
        self.assertNotIn(480, clock.prepared, "第一个越界之后不再量更长的输入")

    def test_microsecond_noise_is_absorbed_by_the_floor(self) -> None:
        # 前一个长度 1 微秒、后一个 100 微秒：比值 100，但都远低于 floor（1 毫秒），按 floor 计，比值只有 0.1。
        self.run_guard(lambda length: 1e-6 if length == 120 else 1e-4, lengths=(120, 240))

    def test_exceeding_floor_is_judged_by_ratio(self) -> None:
        with self.assertRaises(AssertionError):
            self.run_guard(lambda length: 1e-3 if length == 120 else 0.1, lengths=(120, 240))

    def test_minimum_over_repeats_discards_one_off_spikes(self) -> None:
        clock = FakeClock()
        calls: dict[int, int] = {}

        def cost(length: int) -> float:
            calls[length] = calls.get(length, 0) + 1
            return 0.5 if (length == 240 and calls[length] == 1) else length * 1e-5

        guard.assert_growth_is_bounded(self, clock.prepare(cost), (120, 240), label="抢占", timer=clock.timer)
        self.assertGreater(calls[240], 1)

    def test_slow_samples_are_not_repeated_many_times(self) -> None:
        clock = FakeClock()
        with self.assertRaises(AssertionError):
            guard.assert_growth_is_bounded(self, clock.prepare(lambda length: 1.0 if length == 240 else 0.01), (120, 240), label="慢", timer=clock.timer)
        self.assertLessEqual(clock.prepared.count(240), 2)


if __name__ == "__main__":
    unittest.main()
