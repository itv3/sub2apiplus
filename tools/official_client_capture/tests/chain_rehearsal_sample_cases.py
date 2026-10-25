"""引擎单测用的样例用例（文件名不以 test_ 开头，不会被默认发现，只被 ``test_chain_rehearsal`` 通过用例名显式加载）。

故意覆盖单元用例探针要区分的各种结局：通过、失败、出错、跳过、子测试失败。
"""

from __future__ import annotations

import unittest


class Passing(unittest.TestCase):
    def test_one(self) -> None:
        self.assertTrue(True)

    def test_two(self) -> None:
        self.assertEqual(1 + 1, 2)


class Failing(unittest.TestCase):
    def test_boom_marker(self) -> None:
        self.fail("样例缺陷：BOOM-SIGNATURE")

    def test_fine(self) -> None:
        self.assertTrue(True)


class Erroring(unittest.TestCase):
    def test_raises(self) -> None:
        raise RuntimeError("样例异常")


class Skipping(unittest.TestCase):
    @unittest.skip("样例：环境不具备")
    def test_skipped(self) -> None:
        self.fail("不该执行")

    def test_fine(self) -> None:
        self.assertTrue(True)


class AllSkipped(unittest.TestCase):
    @unittest.skip("样例：整组不具备")
    def test_skipped(self) -> None:
        self.fail("不该执行")


class WithSubtest(unittest.TestCase):
    def test_subtest_fails(self) -> None:
        for value in (1, 2, 3):
            with self.subTest(value=value):
                self.assertNotEqual(value, 2, "SUBTEST-SIGNATURE")
