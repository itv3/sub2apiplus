"""复杂度守卫的相对比较辅助（测试夹具，不进受管树）。

背景：几处测试用“最坏模式对 N 字符的路径要在 0.5 秒内跑完”来防止匹配实现退回回溯正则（回溯实现随长度呈 n 的三四次方以上增长）。
绝对时间阈值在满载机器上会抖红（同一份线性实现，被抢占几百毫秒就越界），而它真正要守的是“耗时随长度增长的阶数”，
所以改成长度加倍的相对比较。相邻两个长度的耗时比（取多遍最小值）按两档判定：

* 宽档（任一端低于 ``precise_floor``，缺省 5 毫秒）：比值 ≤ ``slack × (长度比)²``，加倍时上限 6。微秒到毫秒级的计时本身有噪声，
  只能放得宽：线性（比值约 2）和二次（约 4）都过，三次方（约 8）及更差的回溯实现在第一次加倍就被抓住。
* 紧档（两端都 ≥ ``precise_floor``）：比值 ≤ ``precise_slack × 长度比``（缺省 1.6，加倍时上限 3.2）。两端都在 5 毫秒以上时，
  计时噪声相对很小，二次方（约 4）超过上限会被抓住，线性（约 2）有 1.6 倍的余量。

**实话**：二次方实现能不能被抓住，取决于它在测试用的长度上是否已经慢到 5 毫秒以上。更慢的二次实现（常数大）会进紧档被抓；
常数很小的二次实现（例如在最大长度上仍只要几毫秒）一直留在宽档，抓不住。原先的绝对阈值（0.2／0.5／2 秒）同样抓不住这种
“二次但在这些长度上仍然很快”的实现，所以守护强度不降低；要覆盖它，需要加大测试长度或换成按步数计数的断言。

其他约定：

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
    precise_floor: float = 0.005,
    precise_slack: float = 1.6,
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
            growth = length / previous_length
            precise = previous_best >= precise_floor and best >= precise_floor
            allowed = precise_slack * growth if precise else slack * growth ** 2
            ratio = best / max(previous_best, floor)
            case.assertLessEqual(
                ratio,
                allowed,
                f"{label}：长度 {previous_length}→{length}，耗时 {previous_best:.6f}→{best:.6f} 秒，"
                f"比值 {ratio:.1f} 超过 {allowed:.1f}（{'紧档' if precise else '宽档'}；线性约 {growth:.1f}，二次约 {growth ** 2:.1f}）",
            )
        measured.append((length, best))
    return measured
