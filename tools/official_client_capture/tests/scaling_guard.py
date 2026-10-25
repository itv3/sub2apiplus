"""复杂度守卫的相对比较辅助（测试夹具，不进受管树）。

背景：几处测试用“最坏模式对 N 字符的路径要在 0.5 秒内跑完”来防止匹配实现退回回溯正则（回溯实现随长度呈 n 的三四次方以上增长）。
绝对时间阈值在满载机器上会抖红（同一份线性实现，被抢占几百毫秒就越界），而它真正要守的是“耗时随长度增长的阶数”，
所以改成长度加倍的相对比较：

    相邻两个长度（约 2 倍）的耗时比 ≤ slack × (长度比)²  （slack 缺省 1.5，加倍时上限是 6）

* 线性实现的比值约 2，二次实现约 4，都过；三次方（比值约 8）及更差的回溯实现在第一次加倍就被抓住。原先的绝对阈值
  也只能抓住三次方及以上（二次实现在 600 字符上只要几十毫秒），所以守护强度不降低。
* 每个长度取多遍中的最小值，抵消调度抖动；负载同时作用在相邻两个长度上，比值不受绝对速度影响。
* 前一个长度的耗时低于 ``floor``（缺省 1 毫秒）时按 ``floor`` 计：微秒级的计时噪声不会放大成比值。
* 第一个越界就失败，不会继续跑更长的长度（回溯实现遇到几千字符要算上几个小时）；单遍超过 0.25 秒时只再补一遍，
  不会把慢的退化版本重复很多遍。
"""

from __future__ import annotations

import time
import unittest
from typing import Callable, Sequence

SLOW_SAMPLE_SECONDS = 0.25
MAX_TOTAL_SAMPLE_SECONDS = 3.0


def assert_growth_is_bounded(
    case: unittest.TestCase,
    prepare: Callable[[int], Callable[[], object]],
    lengths: Sequence[int],
    *,
    label: str,
    repeats: int = 5,
    floor: float = 0.001,
    slack: float = 1.5,
    timer: Callable[[], float] = time.perf_counter,
) -> list[tuple[int, float]]:
    """按 ``lengths``（从小到大）逐个量耗时，相邻两项的比值超过 ``slack × (长度比)²`` 就失败。

    ``prepare(长度)`` 在每一遍计时之前调用，返回只含被测工作的无参函数（构造输入的开销不计入）。返回各长度的最小耗时，便于调用方补充断言。
    """

    measured: list[tuple[int, float]] = []
    for length in lengths:
        samples: list[float] = []
        for _ in range(repeats):
            work = prepare(length)
            start = timer()
            work()
            samples.append(timer() - start)
            # 单遍已经很慢：只再补一遍（排除偶发抢占），不把慢的退化版本重复很多遍；累计耗时过长也停。
            if (samples[-1] > SLOW_SAMPLE_SECONDS and len(samples) >= 2) or sum(samples) > MAX_TOTAL_SAMPLE_SECONDS:
                break
        best = min(samples)
        if measured:
            previous_length, previous_best = measured[-1]
            allowed = slack * (length / previous_length) ** 2
            ratio = best / max(previous_best, floor)
            case.assertLessEqual(
                ratio,
                allowed,
                f"{label}：长度 {previous_length}→{length}，耗时 {previous_best:.6f}→{best:.6f} 秒，"
                f"比值 {ratio:.1f} 超过 {allowed:.1f}（线性约 {length / previous_length:.1f}，二次约 {(length / previous_length) ** 2:.1f}）",
            )
        measured.append((length, best))
    return measured
