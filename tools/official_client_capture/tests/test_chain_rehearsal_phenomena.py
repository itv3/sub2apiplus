"""第 13 项：``chain_rehearsal_phenomena``（还原被回归用例吞掉的缺陷现象）的单元测试。"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr

from tools.official_client_capture.tests import chain_rehearsal_phenomena as phenomena


class PhenomenaTest(unittest.TestCase):
    def test_kilo_check_passes_on_the_current_tree(self) -> None:
        self.assertEqual(phenomena.kilo_http_usage_order(), 0)

    def test_unknown_or_missing_check_name_is_a_usage_error(self) -> None:
        for argv in ([], ["no-such-check"], ["kilo-http-usage-order", "extra"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(phenomena.main(argv), 2)
                self.assertIn("用法", stderr.getvalue())

    def test_swallowed_stderr_is_exposed_when_the_wrapped_case_fails(self) -> None:
        """被测命令行拒绝并往标准错误写了原因、用例因此断言失败：原因必须出现在检查的标准错误里。"""

        def rejecting(_module, _arguments) -> int:
            sys.stderr.write("错误：Kilo 五份事实的时间顺序不成立\n")
            return 2

        with redirect_stderr(io.StringIO()) as stderr:
            code = phenomena._run_case_exposing_stderr(
                "tools.official_client_capture.tests.test_codex_upgrade_receipt_finalizer", "ReceiptFinalizerTests",
                "test_kilo_compatible_accepts_usage_recorded_before_http_completion", "_run", rejecting)
        self.assertEqual(code, 1)
        self.assertIn("Kilo 五份事实的时间顺序不成立", stderr.getvalue())
        self.assertIn("AssertionError: 2 != 0", stderr.getvalue())

    def test_skipped_case_is_not_reported_as_a_pass(self) -> None:
        with redirect_stderr(io.StringIO()):
            code = phenomena._run_case_exposing_stderr(
                "tools.official_client_capture.tests.chain_rehearsal_sample_cases", "Skipping", "test_skipped", "run_nothing",
                lambda _module, _arguments: 0)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
