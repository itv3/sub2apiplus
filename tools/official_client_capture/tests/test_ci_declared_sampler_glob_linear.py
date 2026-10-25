"""采样器 ``PathMatcher`` 的 glob 匹配改成线性（步骤 5 工作包 WP1g，第 2 条）。

背景：采样器此前把 glob 译成 ``[^/]*``／``[^/]`` 的正则，通配符个数多了会回溯放大——``out/*a*a*a*b`` 对 1000 字符的路径要 25 秒以上，而 ``merge`` 对每条仓库内路径、
``draft`` 的覆盖自检对每个模式都会调用 ``PathMatcher.matches``。现在的实现与登记侧 F4 同一个算法：按 ``/`` 切段、段数必须相同、每段双指针匹配（``*`` 永远
不跨 ``/``），通配符上限仍是 4 个（卫生约束）。对外行为不变：``pattern``、``directory`` 属性，``matches(相对路径) -> bool``，目录前缀的语义不动。

本模块的测试：

1. 与旧正则实现对拍（随机 ≥6000 组模式×文本，加手写用例）0 差异；与登记侧的实现也一致（测试可同时 import 两边，被测模块互相不 import）；
2. ``*``／``?`` 不跨 ``/``、段数必须相同、返回值是 ``bool``、目录前缀语义（含目录自身）、属性与构造期校验不变；
3. 最坏情形的耗时：``out/*a*a*a*b`` 等模式对 120 到 3840 字符的路径（匹配与不匹配两种）耗时随长度加倍的增长比不超过二次方（相对比较，见 scaling_guard）；
4. ``merge_raws`` 链路上同样不慢：1000 字符的写后读回路径过 ``out/*a*a*a*b`` 的 transient 模式。
"""

from __future__ import annotations

import random
import re
import time
import unittest

from tools.ci import declared_sampler as ds
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import scaling_guard
from tools.official_client_capture.tests.test_ci_declared_sampler_followup import FollowupBase
from tools.official_client_capture.tests.test_ci_declared_sampler import open_line, write_line


def reference_regex(pattern: str) -> "re.Pattern[str]":
    """WP1f 及以前 ``PathMatcher`` 的实现（把 glob 译成正则），作对拍基准。"""

    return re.compile("".join("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in pattern))


def wildcard_count(pattern: str) -> int:
    return pattern.count("*") + pattern.count("?")


class EquivalenceTests(unittest.TestCase):
    def test_the_linear_matcher_agrees_with_the_old_regex_on_random_patterns(self) -> None:
        rng = random.Random(20261015)
        checked = 0
        for _ in range(20000):
            pattern = "".join(rng.choice("ab*?/.") for _ in range(rng.randint(1, 9)))
            if pattern.startswith("/") or pattern.endswith("/") or "//" in pattern or "**" in pattern or wildcard_count(pattern) > ds.MAX_GLOB_WILDCARDS:
                continue
            if any(part in (".", "..") for part in pattern.split("/")):
                continue
            matcher = ds.PathMatcher(pattern)
            old = reference_regex(pattern)
            for _inner in range(3):
                text = "".join(rng.choice("ab/.*?x") for _ in range(rng.randint(0, 11)))
                self.assertEqual(matcher.matches(text), old.fullmatch(text) is not None, (pattern, text))
                checked += 1
        self.assertGreaterEqual(checked, 6000, "随机对拍至少 6000 组")

    def test_the_linear_matcher_agrees_with_the_old_regex_and_with_the_record_layer_on_hand_written_cases(self) -> None:
        cases = {
            "frontend/*.mjs": ("frontend/a.mjs", "frontend/sub/a.mjs", "frontend/.mjs", "frontend/a.mjsx", "frontend/node_modules/x/y.mjs"),
            "frontend/tmp-?.json": ("frontend/tmp-1.json", "frontend/tmp-12.json", "frontend/tmp-/.json", "frontend/tmp-.json"),
            "frontend/vitest.config.ts.timestamp-*": ("frontend/vitest.config.ts.timestamp-1-a.mjs", "frontend/vitest.config.ts.timestamp-",
                                                      "frontend/x/vitest.config.ts.timestamp-1"),
            "a/b?c/*": ("a/bxc/f", "a/bxc/f/g", "a/b/c/f", "a/bc/f"), "*": ("top", "a/b", "", "."), "a/*/c": ("a/b/c", "a/b/b/c", "a/c", "a//c"),
            "*a*a*a*b": ("aaab", "aaaa", "baaab", "xaxaxab", "a" * 50 + "b", "a" * 50), "?*?": ("ab", "a", "abc", "a/b"), "a*": ("a", "ab", "a/b", "ba"),
            "*a": ("a", "ba", "ab", "b/a"), "x?y?z": ("xayaz", "xyz", "xaya", "xa/yaz"), "frontend/.eslintcache": ("frontend/.eslintcache", "frontend/.eslintcachex"),
            "a/b/c": ("a/b/c", "a/b", "a/b/c/d", "x/a/b/c"), "out/*a*a*a*b": ("out/aaab", "out/" + "a" * 40 + "b", "out/" + "a" * 40, "out/x/aaab"),
            "*.log": ("x.log", "sub/x.log", ".log", "x.logs"), "docs/*/*": ("docs/a/b", "docs/a", "docs/a/b/c"),
        }
        for pattern, texts in cases.items():
            matcher = ds.PathMatcher(pattern)
            for text in texts:
                with self.subTest(pattern=pattern, text=text):
                    expected = reference_regex(pattern).fullmatch(text) is not None
                    self.assertEqual(matcher.matches(text), expected)
                    self.assertEqual(ur._declared_glob(pattern).fullmatch(text) is not None, expected, "登记侧的匹配器也一致")

    def test_the_sampler_matcher_and_the_record_layer_matcher_agree_on_random_patterns(self) -> None:
        rng = random.Random(20261016)
        checked = 0
        for _ in range(8000):
            pattern = "".join(rng.choice("ab*?/") for _ in range(rng.randint(1, 8))).strip("/")
            if not pattern or "//" in pattern or "**" in pattern or wildcard_count(pattern) > ds.MAX_GLOB_WILDCARDS:
                continue
            text = "".join(rng.choice("ab/") for _ in range(rng.randint(0, 9)))
            self.assertEqual(ds.PathMatcher(pattern).matches(text), ur._declared_glob(pattern).fullmatch(text) is not None, (pattern, text))
            checked += 1
        self.assertGreater(checked, 2000)


class BehaviourTests(unittest.TestCase):
    def test_star_and_question_mark_never_cross_a_slash_and_segment_counts_must_agree(self) -> None:
        self.assertFalse(ds.PathMatcher("a/*").matches("a/b/c"))
        self.assertFalse(ds.PathMatcher("a*c").matches("a/c"))
        self.assertFalse(ds.PathMatcher("a?c").matches("a/c"))
        self.assertFalse(ds.PathMatcher("*").matches("a/b"))
        self.assertFalse(ds.PathMatcher("a/*").matches("a"))
        self.assertTrue(ds.PathMatcher("a/*/c").matches("a/b/c"))
        self.assertTrue(ds.PathMatcher("*").matches(""), "* 可以匹配空段")
        self.assertTrue(ds.PathMatcher("a/*").matches("a/"), "段数相同、* 匹配空段")

    def test_the_return_value_is_a_real_bool_and_the_public_attributes_are_unchanged(self) -> None:
        glob, directory = ds.PathMatcher("frontend/*.mjs"), ds.PathMatcher("scratch-out/")
        for matcher, text, expected in ((glob, "frontend/a.mjs", True), (glob, "frontend/a.js", False), (glob, "x", False), (directory, "scratch-out/a/b", True),
                                        (directory, "scratch-out", True), (directory, "other/a", False)):
            self.assertIs(matcher.matches(text), expected, (matcher.pattern, text))
        self.assertEqual((glob.pattern, glob.directory, directory.pattern, directory.directory), ("frontend/*.mjs", False, "scratch-out/", True))
        self.assertFalse(hasattr(glob, "_regex"), "不再有会回溯的正则")

    def test_directory_prefixes_keep_their_semantics(self) -> None:
        directory = ds.PathMatcher("a/b/")
        for text, expected in (("a/b", True), ("a/b/", True), ("a/b/c", True), ("a/b/c/d", True), ("a/bc", False), ("a", False), ("x/a/b", False), ("", False)):
            with self.subTest(text=text):
                self.assertIs(directory.matches(text), expected)

    def test_construction_still_validates_through_check_transient(self) -> None:
        for bad in ("a/*x*x*x*x*", "a/**", "a/[x]", "/abs", "a/../b", "", "a//b"):
            with self.subTest(bad=bad), self.assertRaises(ds.SamplerError):
                ds.PathMatcher(bad)
        self.assertEqual(ds.PathMatcher("a*a*a*b").pattern, "a*a*a*b")
        self.assertEqual(ds.MAX_GLOB_WILDCARDS, 4, "通配符上限仍是 4 个（卫生约束）")
        self.assertEqual(ds.MAX_GLOB_WILDCARDS, ur._DECLARED_MAX_GLOB_WILDCARDS)

    def test_the_matcher_does_not_mutate_or_depend_on_global_state(self) -> None:
        matcher = ds.PathMatcher("out/*a*a*a*b")
        before = (matcher.pattern, matcher.directory)
        for _ in range(3):
            self.assertTrue(matcher.matches("out/aaab"))
            self.assertFalse(matcher.matches("out/aaaa"))
        self.assertEqual((matcher.pattern, matcher.directory), before)


class WorstCaseTimingTests(unittest.TestCase):
    PATTERNS = ("out/*a*a*a*b", "out/*a*a*a*a", "out/?*a*a*b", "out/*a?a*a*b", "out/a*a*a*a*", "out/*?*?", "out/?a*?a*b", "*a*a*a*b")

    def test_the_worst_allowed_patterns_stay_fast_for_long_paths(self) -> None:
        """旧实现：``out/*a*a*a*b`` 对 120 字符 0.2 秒量级、600 字符 4.1 秒、1000 字符超过 25 秒。线性匹配对 5000 字符也是毫秒级。"""

        # 不用绝对阈值（满载下会抖红）：每个（模式，匹配与否）按长度加倍做相对比较，第一次越界就失败，更长的长度不会继续跑
        # （回溯实现遇到 5000 字符会算上几个小时）；见 scaling_guard 的说明。最长 3840 字符。
        for pattern in self.PATTERNS:
            matcher = ds.PathMatcher(pattern)
            prefix = "out/" if pattern.startswith("out/") else ""
            for label, suffix in (("不匹配", ""), ("匹配", "b")):
                scaling_guard.assert_growth_is_bounded(
                    self, lambda length, matcher=matcher, prefix=prefix, suffix=suffix: (lambda: matcher.matches(prefix + "a" * length + suffix)),
                    (120, 240, 480, 960, 1920, 3840), label=f"{pattern}（{label}）")
                self.assertIsInstance(matcher.matches(prefix + "a" * 120 + suffix), bool)

    def test_many_long_paths_through_one_matcher_stay_fast(self) -> None:
        matcher = ds.PathMatcher("out/*a*a*a*b")

        # 规模守卫放在最前面：真回归（退回回溯正则）时在 250→500 字符这一步、几秒内就失败，不会先把几百条 1000 字符的路径跑完
        # （那样每条要二十多秒，总共要算上一个多小时）。一批路径的总耗时随路径长度加倍做相对比较（每批 50 条），不用绝对阈值。
        def prepare(length: int):
            batch = [f"out/{'a' * length}{index}" for index in range(50)] + [f"out/{'a' * length}b"]
            return lambda: [matcher.matches(path) for path in batch]

        scaling_guard.assert_growth_is_bounded(self, prepare, (250, 500, 1000, 2000), label="一批长路径")

        # 正确性断言只用少量路径：5 条不匹配、1 条匹配（线性实现下都是毫秒级）。
        paths = [f"out/{'a' * 1000}{index}" for index in range(5)] + [f"out/{'a' * 1000}b"]
        self.assertEqual([matcher.matches(path) for path in paths], [False] * 5 + [True])


class MergeReachabilityTests(FollowupBase):
    """``merge_raws`` 对每条仓库内路径都过 transient 模式的匹配器：长路径加最坏模式也不慢，结果与短路径一致。"""

    def test_a_1000_character_written_and_read_back_path_goes_through_the_worst_allowed_pattern_quickly(self) -> None:
        # 1000 字符：旧实现要 25 秒以上，线性匹配是毫秒级；不用更长的，免得有人把实现改回回溯后这个用例算上几个小时。
        t = str(self.tree)
        long_path = "out/" + "a" * 1000 + "b"
        lines = self.good() + [write_line(t, f"{t}/{long_path}"), open_line(t, f"{t}/{long_path}")]
        raw = self.raw(lines)
        sample = self.merge(raw, transient=["out/*a*a*a*b"])
        # 不用 merge < 2 秒的绝对阈值：路径长度加倍的相对比较（500/1000/2000 字符）。

        def prepare(length: int):
            path = "out/" + "a" * length + "b"
            return (lambda raw=self.raw(self.good() + [write_line(t, f"{t}/{path}"), open_line(t, f"{t}/{path}")]):
                    self.merge(raw, transient=["out/*a*a*a*b"]))

        scaling_guard.assert_growth_is_bounded(self, prepare, (500, 1000, 2000), label="merge 长路径")
        row = sample["review"]["allowances"]["transient"][0]
        self.assertEqual((row["pattern"], row["paths"], row["read_back"], row["unused"]), ("out/*a*a*a*b", 1, 1, False))

    def test_a_path_that_does_not_match_leaves_the_pattern_unused(self) -> None:
        t = str(self.tree)
        long_path = "out/" + "a" * 1000
        sample = self.merge(self.raw(self.good() + [write_line(t, f"{t}/{long_path}")]), transient=["out/*a*a*a*b"])
        self.assertEqual(sample["review"]["allowances"]["transient"][0]["unused"], True)


if __name__ == "__main__":
    unittest.main()
