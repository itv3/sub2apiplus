"""第 13 项：把“被测试吞掉的缺陷现象”还原出来的小检查（测试夹具，不进受管身份）。

为什么需要它：历史重放要求探针失败时，失败信息里出现**只在这个缺陷里才有的现象**（见 ``chain_rehearsal`` 的签名判据）。
个别回归用例把工具的拒绝原因吞掉了——例如 Kilo 收据的用例用 ``redirect_stderr`` 把终结器的报错丢进 StringIO，
缺陷回来时用例只剩一句 ``AssertionError: 2 != 0``，任何失败都长这样，没法当签名。

这里的检查**直接复用原回归用例本身**（同一个夹具、同一个断言），只把被吞掉的标准错误捕获下来，失败时原样打到标准错误：

    python3 -m tools.official_client_capture.tests.chain_rehearsal_phenomena kilo-http-usage-order

退出码 0 通过；1 原用例失败（标准错误里带工具的拒绝原因）；2 用法错误。
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Callable


def _run_case_exposing_stderr(module_name: str, class_name: str, method: str, runner_attribute: str, call: Callable) -> int:
    """运行一个现成的回归用例，把它内部吞掉的终结器标准错误捕获下来；用例失败时把标准错误和断言一起打出来。

    ``runner_attribute`` 是用例里负责“跑被测命令行并吞掉输出”的实例方法名；这里在**实例**上换成同签名的版本
    （调用 ``call(arguments)``，标准错误写进共享缓冲），用例的其余部分一字不改。
    """

    module = __import__(module_name, fromlist=[class_name])
    case = getattr(module, class_name)(method)
    captured = io.StringIO()

    def exposing_run(arguments: list[str]) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(captured):
            return call(module, arguments)

    setattr(case, runner_attribute, exposing_run)
    result = unittest.TestResult()
    case.run(result)
    problems = result.failures + result.errors
    if not problems and not result.skipped:
        return 0
    if result.skipped:
        sys.stderr.write(f"用例被跳过：{result.skipped[0][1]}\n")
        return 1
    sys.stderr.write(f"终结器标准错误：{captured.getvalue().strip()}\n")
    sys.stderr.write(problems[0][1])
    return 1


def kilo_http_usage_order() -> int:
    """Kilo 收据：HTTP 用量早于响应完成（但晚于入站）必须被接受。缺陷时终结器拒绝并打印“Kilo 五份事实的时间顺序不成立”。"""

    return _run_case_exposing_stderr(
        "tools.official_client_capture.tests.test_codex_upgrade_receipt_finalizer", "ReceiptFinalizerTests",
        "test_kilo_compatible_accepts_usage_recorded_before_http_completion", "_run",
        lambda module, arguments: module.finalizer.main(arguments),
    )


CHECKS: dict[str, Callable[[], int]] = {"kilo-http-usage-order": kilo_http_usage_order}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in CHECKS:
        sys.stderr.write(f"用法：chain_rehearsal_phenomena <{'|'.join(sorted(CHECKS))}>\n")
        return 2
    return CHECKS[arguments[0]]()


if __name__ == "__main__":
    raise SystemExit(main())
