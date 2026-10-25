"""``tools/ci/time_threshold_lint.py`` 的专项回归：规则命中、下界不报、宽裕上界不报、豁免标记、基线与退出码。

脚本暂未接入门禁，这里只保证它自己的行为稳定，接入时由执行器那一路另行登记。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.ci import time_threshold_lint as lint


def _scan(files: dict[str, str]) -> list[lint.Finding]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        return lint.scan([root], root)


class TimeThresholdLintTests(unittest.TestCase):
    def run_main(self, *arguments: str) -> int:
        """跑命令行入口并吞掉它的输出。"""

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return lint.main(list(arguments))

    def test_builtin_self_test_passes(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lint.self_test(), 0)

    def test_python_rules_and_exemptions(self) -> None:
        source = (
            "import time, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        start = time.monotonic()\n"
            "        self.assertLess(time.monotonic() - start, 0.5)\n"                       # 5 命中
            "        self.assertLessEqual(elapsed, 1)\n"                                     # 6 命中
            "        self.assertGreater(2, elapsed)\n"                                       # 7 命中（常数在左 = 上界）
            "        self.assertGreaterEqual(elapsed, 0.5)\n"                                # 8 下界，不报
            "        self.assertLess(elapsed, 0)\n"                                          # 9 常数 0，不报
            "        self.assertLess(elapsed, 30)\n"                                         # 10 宽裕，不报
            "        self.assertLess(contract['a'], contract['b'])\n"                        # 11 没有常数，不报
            "        assert time.monotonic() - start < 3\n"                                  # 12 命中
            "        worker.join(0.3)\n"                                                     # 13 命中
            "        event.wait(0.1)\n"                                                      # 14 单独成句的等待是睡一拍，不报
            "        ok = event.wait(0.1)\n"                                                 # 15 取了返回值，命中
            "        runner(timeout=0.4)\n"                                                  # 16 命中
            "        self.assertLess(elapsed, 0.2)  # time-threshold: allow 验证超时语义\n"  # 17 已豁免
            "        self.assertLess(elapsed, 0.2)  # time-threshold: allow\n"               # 18 没有原因，仍然命中
        )
        findings = {item.line: item for item in _scan({"test_sample.py": source})}
        self.assertEqual(sorted(findings), [5, 6, 7, 12, 13, 15, 16, 17, 18])
        self.assertIsNone(findings[5].exempt_reason)
        self.assertEqual(findings[17].exempt_reason, "验证超时语义")
        self.assertIsNone(findings[18].exempt_reason, "没有原因的标记无效")
        self.assertEqual({item.test for item in findings.values()}, {"T.test_a"})

    def test_python_function_level_and_file_level_exemptions(self) -> None:
        function_level = (
            "class T:\n"
            "    # time-threshold: allow 整个用例在验证超时\n"
            "    def test_b(self):\n"
            "        self.assertLess(elapsed, 0.1)\n"
        )
        file_level = "# time-threshold: allow-file 全文件验证超时语义\nclass T:\n    def test_c(self):\n        self.assertLess(elapsed, 0.1)\n"
        for text in (function_level, file_level):
            findings = _scan({"test_x.py": text})
            self.assertEqual(len(findings), 1)
            self.assertIsNotNone(findings[0].exempt_reason, text)

    def test_go_rules_and_exemptions(self) -> None:
        source = (
            "package x\n"
            "func TestA(t *testing.T) {\n"
            "\trequire.Less(t, time.Since(start), 50*time.Millisecond)\n"                    # 3 命中
            "\trequire.LessOrEqual(t, elapsed, time.Second)\n"                               # 4 命中
            "\trequire.Greater(t, 2*time.Second, elapsed)\n"                                  # 5 命中（常数在左）
            "\trequire.GreaterOrEqual(t, elapsed, 40*time.Millisecond)\n"                     # 6 下界，不报
            "\trequire.Less(t, elapsed, 30*time.Second)\n"                                    # 7 宽裕，不报
            "\tif elapsed > 2*time.Second {\n"                                                # 8 命中
            "\t}\n"
            "\tif elapsed < 100*time.Millisecond {\n"                                         # 10 下界，不报
            "\t}\n"
            "\tctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)\n"  # 12 命中
            "\tctx2, cancel2 := context.WithTimeout(context.Background(), 10*time.Second)\n"     # 13 不报
            "\tselect {\n"
            "\tcase <-time.After(100 * time.Millisecond):\n"                                  # 15 命中
            "\t}\n"
            "\trequire.Eventually(t, f, 300*time.Millisecond, 10*time.Millisecond)\n"          # 17 命中（总等待上限）
            "\trequire.Eventually(t, f, 5*time.Second, 10*time.Millisecond)\n"                 # 18 不报
            "\trequire.Never(t, f, 150*time.Millisecond, 10*time.Millisecond)\n"               # 19 命中
            "\tallocs := testing.AllocsPerRun(10, f)\n"                                        # 20 命中（非零上限）
            "\trequire.LessOrEqual(t, allocs, float64(3))\n"
            "\tzero := testing.AllocsPerRun(10, f)\n"                                          # 22 零分配，不报
            "\trequire.Equal(t, float64(0), zero)\n"
            "\trequire.Less(t, elapsed, 100*time.Millisecond) // time-threshold: allow 区分立即返回与完整等待\n"  # 24 已豁免
            "}\n"
        )
        findings = {item.line: item for item in _scan({"sample_test.go": source})}
        self.assertEqual(sorted(findings), [3, 4, 5, 8, 12, 15, 17, 19, 20, 24])
        self.assertEqual({item.rule for item in (findings[3], findings[4], findings[5], findings[8], findings[24])}, {"elapsed-bound"})
        self.assertEqual({findings[12].rule, findings[15].rule, findings[17].rule, findings[19].rule}, {"short-timeout"})
        self.assertEqual(findings[20].rule, "alloc-bound")
        self.assertIsNotNone(findings[24].exempt_reason)
        self.assertEqual({item.test for item in findings.values()}, {"TestA"})

    def test_python_named_constant_and_subtraction_bounds(self) -> None:
        source = (
            "import unittest\n"
            "TIMEOUT = 1.5\n"
            "HUGE = 60\n"
            "class T(unittest.TestCase):\n"
            "    LIMIT = 0.8\n"
            "    def test_a(self):\n"
            "        self.assertLess(elapsed, TIMEOUT)\n"              # 7 命中：同文件常量 1.5
            "        self.assertLess(elapsed, TIMEOUT + 1)\n"          # 8 命中：2.5
            "        self.assertLess(elapsed, self.LIMIT)\n"           # 9 命中：类级常量 0.8
            "        self.assertLess(elapsed, HUGE)\n"                 # 10 宽裕，不报
            "        self.assertLess(elapsed, other.DEFAULT_SECONDS)\n"  # 11 命中：未解析的命名值
            "        self.assertLess(finished_at - started_at, 0.5)\n"  # 12 命中：不含 elapsed 字样的减法
            "        self.assertLess(end - start, TIMEOUT)\n"          # 13 命中
            "        self.assertLess(count - total, 0.5)\n"            # 14 不是时间点相减，不报
            "        self.assertLess(elapsed, previous_elapsed)\n"     # 15 上界本身是实测值，不报
            "        self.assertLess(time.monotonic(), deadline)\n"    # 16 轮询截止时间，不报
        )
        found = {item.line: item for item in _scan({"test_named.py": source})}
        self.assertEqual(sorted(found), [7, 8, 9, 11, 12, 13])
        self.assertIn("命名常量 TIMEOUT＝1.5", found[7].detail)
        self.assertIn("未解析的命名值 other.DEFAULT_SECONDS", found[11].detail)

    def test_go_named_constant_and_subtraction_bounds(self) -> None:
        source = (
            "package x\n"
            "const retryDelay = 500 * time.Millisecond\n"
            "const hugeWait = 30 * time.Second\n"
            "func TestN(t *testing.T) {\n"
            "\trequire.Less(t, elapsed, retryDelay)\n"                 # 5 命中：同文件常量
            "\trequire.Less(t, elapsed, 2*retryDelay)\n"               # 6 命中：1s
            "\trequire.Less(t, elapsed, hugeWait)\n"                   # 7 宽裕，不报
            "\trequire.Less(t, elapsed, otherPackageDelay)\n"          # 8 命中：未解析
            "\trequire.Less(t, end.Sub(start), 300*time.Millisecond)\n"  # 9 命中：end.Sub(start)
            "\tif finishedAt.Sub(startedAt) > retryDelay {\n"          # 10 命中
            "\t}\n"
            "\trequire.Less(t, elapsed, other.elapsed)\n"              # 12 上界本身是实测值，不报
            "}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "n_test.go").write_text(source, encoding="utf-8")
            found = {item.line: item for item in lint.scan([root], root)}
        self.assertEqual(sorted(found), [5, 6, 8, 9, 10])
        self.assertIn("命名常量 retryDelay＝0.5 秒", found[5].detail)
        self.assertIn("未解析的命名值 otherPackageDelay", found[8].detail)

    def test_baseline_keys_carry_a_per_text_ordinal(self) -> None:
        # 同一文件里写法逐字相同的两处命中，基线里是两个键；删掉其中一处后另一处不会被“一个键盖住”。
        source = "class T:\n    def test_a(self):\n        self.assertLess(elapsed, 0.5)\n    def test_b(self):\n        self.assertLess(elapsed, 0.5)\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "test_dup.py"
            sample.write_text(source, encoding="utf-8")
            baseline = root / "baseline.json"
            self.assertEqual(self.run_main("--root", str(root), "--write-baseline", str(baseline), str(sample)), 0)
            entries = json.loads(baseline.read_text(encoding="utf-8"))["entries"]
            self.assertEqual(len(entries), 2)
            self.assertTrue(entries[0].endswith("#1") and entries[1].endswith("#2"))
            self.assertEqual(self.run_main("--root", str(root), "--baseline", str(baseline), str(sample)), 0)
            # 新增第三处同样写法：没有第三个键，所以是新增命中。
            sample.write_text(source + "    def test_c(self):\n        self.assertLess(elapsed, 0.5)\n", encoding="utf-8")
            self.assertEqual(self.run_main("--root", str(root), "--baseline", str(baseline), str(sample)), 1)

    def test_positive_wait_rule_is_opt_in(self) -> None:
        source = (
            "package x\n"
            "func TestW(t *testing.T) {\n"
            "\tselect {\n"
            "\tcase <-done:\n"
            "\tcase <-time.After(2 * time.Second):\n"      # 5 正向等待，超时即失败
            "\t\tt.Fatal(\"等待超时\")\n"
            "\t}\n"
            "\tselect {\n"
            "\tcase <-done:\n"
            "\tcase <-time.After(2 * time.Second):\n"      # 10 反向窗口，不失败
            "\t}\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample_test.go").write_text(source, encoding="utf-8")
            self.assertEqual(lint.scan([root], root), [])
            found = lint.scan([root], root, positive_wait=3.0)
        self.assertEqual([(item.line, item.rule) for item in found], [(5, "positive-wait")])

    def test_baseline_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "test_sample.py"
            sample.write_text("class T:\n    def test_a(self):\n        self.assertLess(elapsed, 0.5)\n", encoding="utf-8")
            baseline = root / "baseline.json"
            self.assertEqual(self.run_main("--root", str(root), str(sample)), 1)
            self.assertEqual(self.run_main("--root", str(root), "--write-baseline", str(baseline), str(sample)), 0)
            self.assertEqual(self.run_main("--root", str(root), "--baseline", str(baseline), str(sample)), 0)
            # 基线键不含行号：在命中行上方插入无关代码，基线仍然有效。
            sample.write_text("X = 1\n\n" + sample.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(self.run_main("--root", str(root), "--baseline", str(baseline), str(sample)), 0)
            # 新增一处不在基线里的命中：失败。
            sample.write_text(sample.read_text(encoding="utf-8") + "    def test_b(self):\n        self.assertLess(took, 0.1)\n", encoding="utf-8")
            self.assertEqual(self.run_main("--root", str(root), "--baseline", str(baseline), str(sample)), 1)
            self.assertEqual(json.loads(baseline.read_text(encoding="utf-8"))["schema_version"], "time-threshold-baseline/v2")
            self.assertEqual(self.run_main("--root", str(root), "--baseline", str(root / "missing.json"), str(sample)), 2)


if __name__ == "__main__":
    unittest.main()
