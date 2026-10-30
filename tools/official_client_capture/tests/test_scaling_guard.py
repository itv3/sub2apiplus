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

    def test_linear_growth_passes_in_both_bands(self) -> None:
        self.run_guard(lambda length: length * 1e-4)  # 120 字符 12 毫秒…1920 字符 192 毫秒：后面几步两端都 ≥ 20 毫秒进紧档，比值 2 ≤ 3.2
        self.run_guard(lambda length: length * 1e-7)  # 全程在微秒级：宽档

    def test_tiny_quadratic_stays_in_the_wide_band_and_passes(self) -> None:
        # 常数很小的二次实现（最大长度上只要 3.7 毫秒、没进紧档）抓不住：模块说明里写明的局限。
        self.run_guard(lambda length: (length ** 2) * 1e-9)

    def test_quadratic_that_reaches_the_precise_band_is_caught(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            self.run_guard(lambda length: (length ** 2) * 3e-6)  # 120 字符 43 毫秒，已在紧档：比值 4 > 3.2
        self.assertIn("紧档", str(caught.exception))

    def test_noise_up_to_the_precise_limit_is_tolerated(self) -> None:
        # 线性实现两端都在 20 毫秒以上，后一端有 2.8 的比值（噪声）仍低于 3.2；持续 3.33 倍就是退化，失败。
        self.run_guard(lambda length: 0.03 if length == 120 else 0.084, lengths=(120, 240))
        with self.assertRaises(AssertionError):
            self.run_guard(lambda length: 0.03 if length == 120 else 0.100, lengths=(120, 240))

    def test_ratio_just_over_the_old_limit_with_one_end_near_five_milliseconds_is_not_precise(self) -> None:
        # CI 偶发红的真实数字：500→1000 耗时 0.005635→0.018274 秒，比值 3.24。上一端不足 20 毫秒，现在走宽档（上限 6），通过。
        self.run_guard(lambda length: 0.005635 if length == 500 else 0.018274, lengths=(500, 1000))
        # 反过来，两端都够长的 3.24 倍仍然是紧档越界（且复测后依旧越界）。
        with self.assertRaises(AssertionError) as caught:
            self.run_guard(lambda length: 0.030 if length == 500 else 0.0972, lengths=(500, 1000))
        self.assertIn("紧档", str(caught.exception))

    def test_one_off_preemption_in_the_precise_band_is_absorbed_by_the_retry(self) -> None:
        # 长度 1000 的第 1 轮 5 遍都被抢占（均 0.1 秒，比值 4 > 3.2），复测那一轮跑回正常 0.06 秒（比值 2）：合并取最小值后通过。
        clock = FakeClock()
        calls: dict[int, int] = {}

        def cost(length: int) -> float:
            calls[length] = calls.get(length, 0) + 1
            return 0.1 if (length == 1000 and calls[length] <= 5) else length * 6e-5

        measured = guard.assert_growth_is_bounded(self, clock.prepare(cost), (500, 1000), label="抢占", timer=clock.timer)
        self.assertEqual(calls[1000], 10, "越界后只复测一轮（再采 5 遍）")
        self.assertEqual(calls[500], 10, "相邻的前一个长度也同步复测")
        self.assertAlmostEqual(measured[1][1], 0.06)

    def test_persistent_regression_is_still_caught_after_the_retry_and_retries_only_once(self) -> None:
        clock = FakeClock()
        with self.assertRaises(AssertionError):
            guard.assert_growth_is_bounded(self, clock.prepare(lambda length: (length ** 2) * 3e-6), (120, 240, 480), label="持续二次方", timer=clock.timer)
        self.assertEqual(clock.prepared.count(120), 10)
        self.assertEqual(clock.prepared.count(240), 10, "只复测一轮，不无限重试")
        self.assertNotIn(480, clock.prepared, "第一个越界之后不再量更长的输入")

    def test_big_regression_with_slow_samples_is_not_retried(self) -> None:
        clock = FakeClock()
        with self.assertRaises(AssertionError):
            guard.assert_growth_is_bounded(self, clock.prepare(lambda length: 1.0 if length == 240 else 0.01), (120, 240), label="慢", timer=clock.timer)
        self.assertEqual(clock.prepared.count(120), 5, "明显的大退化不复测")

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
