"""读集采样器（``tools/ci/declared_sampler.py``）对 strace“中途消失的线程”垃圾行的处理：步骤 5 的 WP4c 之 T1。

乱码事件的根因：不是过滤器 bug，是 strace 对 Go 运行时被杀或退出的线程打印的垃圾行——它把线程当作还在系统调用里，用残留寄存器解码出
名字、参数都乱七八糟的一行。四种形态（夹具里是 ARM64 对照实验抓到的原样真实行，见 ``declared_sampler_trace_fixture``）：

* A：名字是随机的系统调用名，以 ``<detached ...>`` 收尾；
* B：以 `` = ?`` 收尾，第一个参数是指针常量，第二个参数恰好像路径；
* C：路径参数是指针的 ``<unfinished ...>`` 起始行，永远没有 resumed——按 ``unfinished-calls-at-eof`` 容忍，不丢；
* D：不带名字的裸标记 ``???( <detached ...>``——一直按标记丢弃计数。

A、B、D 现在都在 filter 里丢弃并计入 ``coverage.detached_markers``（不新增 coverage 键：登记侧对它是闭合的），带名字的 A、B 另留原样样例
（raw 顶层 ``detached_samples``，merge 后进 ``review.detached_samples``，评审清单第 12 节列出）；合并后总数超过 64 判采样无效。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.ci import declared_sampler as ds
from tools.official_client_capture.tests import declared_sampler_trace_fixture as real
from tools.official_client_capture.tests.test_ci_declared_sampler import SHA, UNIT, connect_line, git_line, make_raw, open_line
from tools.official_client_capture.tests.test_ci_declared_sampler_followup import DraftBase, FollowupBase

ALIASES = {"TREE": real.TREE, "WORK": real.ROOT, "HOME": "/root", "TMP": "/tmp"}
GARBAGE_A = real.GARBLED_DETACHED_A.splitlines()[2]
GARBAGE_B = real.GARBLED_LOST_RESULT_B.splitlines()[1]
BARE_D = real.GARBLED_BARE_MARKER_D.splitlines()[2]
POINTER_C = real.GARBLED_UNFINISHED_POINTER_C.splitlines()[1]


def filtered(lines: list[str] | str, *, unit_id: str = UNIT) -> dict:
    if isinstance(lines, str):
        lines = lines.splitlines(True)
    return ds.filter_stream([line if line.endswith("\n") else line + "\n" for line in lines], ALIASES, unit_id=unit_id)


def accesses(document: dict) -> tuple:
    """过滤器输出里与读集有关的四项（不含 coverage 与计数）。outside 的第三项是规模提示计数，分块边界会让它差 1，所以不比。"""
    return (document["repo_accesses"], [row[:2] for row in document["outside_accesses"]], document["network"], document["git_commands"])


class GarbledLineFilterTests(unittest.TestCase):
    def test_the_real_shapes_are_exactly_what_the_experiment_caught(self) -> None:
        """夹具就是 garble-hunt 抓到的原样行：先钉住它们的形状，免得夹具被改坏后下面的断言失去意义。"""
        self.assertEqual(GARBAGE_A, r'711353 lsetxattr("\270J\321", 0x17, 0x23fdca6b7008, 39572929737628, XATTR_CREATE <detached ...>')
        self.assertEqual(GARBAGE_B, '751440 lsetxattr(0xffffffffffffff00, "/usr/local/go/src/internal/abi", "", 0, 0) = ?')
        self.assertEqual(BARE_D, "736020 ???( <detached ...>")
        self.assertEqual(POINTER_C, "740774 newfstatat(AT_FDCWD</tmp/egressscan-selftest2393283390/case2>, 0x1948d3defda0,  <unfinished ...>")

    def test_shape_a_and_shape_b_are_dropped_counted_and_sampled_without_touching_anything_else(self) -> None:
        for name, garbage, expected_sample in (("GARBLED_DETACHED_A", GARBAGE_A, GARBAGE_A), ("GARBLED_LOST_RESULT_B", GARBAGE_B, GARBAGE_B)):
            with self.subTest(name):
                text = getattr(real, name)
                document = filtered(text)
                coverage = document["coverage"]
                self.assertEqual(coverage["detached_markers"], 1, "垃圾行计入已有的 detached_markers，不新增 coverage 键")
                self.assertEqual(coverage["unresolved_reasons"], {"unfinished-calls-at-eof": 1},
                                 "lsetxattr 的 plain-path-not-resolved、result-or-string-incomplete 等假原因没有了，只剩被容忍的悬着调用")
                self.assertEqual(set(coverage) - ds.COVERAGE_KEYS, {"unfinished_samples"}, "没有新增别的 coverage 键")
                self.assertEqual(document["detached_samples"], [expected_sample], "原样行（已别名化的话与原行一致，这两行里没有采样根路径）")
                self.assertEqual(ds.validate_raw(document), [])
                # 丢弃垃圾行不影响其它任何东西：去掉这一行再过滤，读集、网络、git 命令逐项相同。
                rest = [line for line in text.splitlines() if line != garbage]
                self.assertEqual(len(rest), len(text.splitlines()) - 1)
                baseline = filtered(rest)
                self.assertEqual(accesses(document), accesses(baseline))
                self.assertEqual(document["coverage"]["unresolved_reasons"], baseline["coverage"]["unresolved_reasons"])
                self.assertEqual(baseline["coverage"]["detached_markers"], 0)
                self.assertNotIn("detached_samples", baseline)

    def test_the_old_filter_reported_false_reasons_for_the_same_lines(self) -> None:
        """对照：不丢弃时，严格解析器把这两行记成假的未解析原因——这正是采样被判无效的根因。"""
        parser_a = ds.read_audit_module().TraceParser(strict=True)
        parser_a.feed(GARBAGE_A + "\n")
        self.assertIn("result-or-string-incomplete:lsetxattr", parser_a.unresolved_reasons)
        parser_b = ds.read_audit_module().TraceParser(strict=True)
        parser_b.feed(GARBAGE_B + "\n")
        self.assertIn("plain-path-not-resolved:lsetxattr", parser_b.unresolved_reasons)

    def test_shape_d_stays_a_bare_marker_and_shape_c_stays_a_tolerated_unfinished_call(self) -> None:
        d = filtered(real.GARBLED_BARE_MARKER_D)
        self.assertEqual((d["coverage"]["detached_markers"], d["coverage"]["unresolved_reasons"]), (1, {}))
        self.assertNotIn("detached_samples", d, "裸标记没有内容，只计数、不留样例")
        c = filtered(real.GARBLED_UNFINISHED_POINTER_C)
        self.assertEqual(c["coverage"]["detached_markers"], 0)
        self.assertEqual(c["coverage"]["unresolved_reasons"], {"symlink-metadata-not-bound:newfstatat": 1, "unfinished-calls-at-eof": 2})
        self.assertIn("{TMP}/egressscan-selftest2393283390/case2>, 0x1948d3defda0,  <unfinished ...>", " ".join(c["coverage"]["unfinished_samples"]))
        self.assertNotIn("detached_samples", c)
        self.assertEqual(ds.validate_raw(c), [])

    def test_all_four_shapes_together_leave_only_the_tolerated_reasons(self) -> None:
        text = real.GARBLED_DETACHED_A + real.GARBLED_LOST_RESULT_B + real.GARBLED_BARE_MARKER_D + real.GARBLED_UNFINISHED_POINTER_C
        document = filtered(text)
        self.assertEqual(document["coverage"]["detached_markers"], 3, "A、B、D 各一条")
        self.assertEqual(document["detached_samples"], [GARBAGE_A, GARBAGE_B])
        reasons = document["coverage"]["unresolved_reasons"]
        self.assertTrue(all(ds.reason_tolerated(reason) for reason in reasons), reasons)

    def test_names_are_arbitrary_for_shape_a_and_shape_b_has_exactly_four_exceptions(self) -> None:
        detached = ["711353 syscall_0x2a1b(0x1, 0x2 <detached ...>", "711353 ???(0x1 <detached ...>", '711353 openat(AT_FDCWD</x>, "/p", O_RDONLY <detached ...>',
                    "711353 connect(3<socket:[1]>, 0x7ffd, 16 <detached ...>", '711353 execve("/usr/bin/git", 0x1, 0x2 <detached ...>']
        for line in detached:
            with self.subTest(line):
                document = filtered([line])
                self.assertEqual((document["coverage"]["detached_markers"], document["coverage"]["unresolved_reasons"]), (1, {}), "形态 A：名字任意")
                self.assertEqual((document["network"], document["git_commands"]), ([], []), "垃圾行不走 connect、execve 旁路")
        lost = ['751440 openat(0xffffffffffffff00, "/usr/local/go/src/internal/abi", 0, 0) = ?', "751440 syscall_0x2a1b(0x1, 0x2) = ?", "751440 ???(0x1) = ?",
                '751440 newfstatat(AT_FDCWD</x>, 0x1, 0x2, 0) = ?']
        for line in lost:
            with self.subTest(line):
                document = filtered([line])
                self.assertEqual((document["coverage"]["detached_markers"], document["coverage"]["unresolved_reasons"]), (1, {}), "形态 B：openat 等名字也算")
                self.assertEqual(document["detached_samples"], [line])
        for name in ("execve", "execveat", "exit", "exit_group"):
            with self.subTest(name):
                document = filtered([f'4242 {name}("/usr/bin/true", [], 0x1) = ?'])
                self.assertEqual(document["coverage"]["detached_markers"], 0, "这四个本来就可以 = ?，不是垃圾行")
                self.assertNotIn("detached_samples", document)

    def test_a_real_execve_that_ends_with_unknown_result_is_still_parsed(self) -> None:
        line = f'4242 execve("/usr/bin/git", ["git", "show", "{SHA}:a.json"], 0x1 /* 17 vars */) = ?'
        document = filtered([line])
        self.assertEqual(document["coverage"]["detached_markers"], 0)
        self.assertEqual(document["git_commands"], [["git", "show", f"{SHA}:a.json"]], "execve 行照常收集 git 的 argv")
        self.assertEqual([row[:2] for row in document["outside_accesses"]], [["/usr/bin/", ["exec"]]])
        exit_group = filtered(["4242 exit_group(0)                    = ?"])
        self.assertEqual(exit_group["coverage"]["unresolved_reasons"], {"unsupported-call:exit_group": 1}, "与从前一样：exit_group 是未建模的调用")

    def test_ordinary_lines_pairs_and_look_alikes_are_untouched(self) -> None:
        t = real.TREE
        ordinary = [open_line(t, f"{t}/backend/go.mod"), f'4243 openat(AT_FDCWD<{t}>, "{t}/backend/a.go", O_RDONLY|O_CLOEXEC <unfinished ...>',
                    f"4243 <... openat resumed>) = 3<{t}/backend/a.go>"]
        document = filtered(ordinary)
        self.assertEqual((document["coverage"]["detached_markers"], document["coverage"]["unresolved_reasons"]), (0, {}))
        self.assertEqual(sorted(path for path, _kinds in document["repo_accesses"]), ["{TREE}/backend/a.go", "{TREE}/backend/go.mod"], "配对的调用不受影响")
        # 长得像但不是：resumed 行以 = ? 收尾；<detached ...> 或 = ? 出现在行中间；不是“<进程号> <名字>(”的形状；退出提示行。
        look_alikes = ["4242 <... openat resumed>)  = ?", "4242 openat(AT_FDCWD</x>, \"/a <detached ...> b\", O_RDONLY) = 3</a <detached ...> b>",
                       "4242 openat(AT_FDCWD</x>, \"/p = ? q\", O_RDONLY) = 3</p = ? q>", "杂散输出 = ?", "4242 +++ exited with 0 +++", "4242 --- SIGCHLD {si_signo=SIGCHLD} --- <detached ...>",
                       "= ?", "<detached ...>"]
        for line in look_alikes:
            with self.subTest(line):
                self.assertEqual(filtered([line])["coverage"]["detached_markers"], 0, line)

    def test_trailing_whitespace_is_ignored_when_recognizing_the_endings(self) -> None:
        for suffix in ("", " ", "  \t", "\r"):
            for line in ('711353 lsetxattr(0x1, 0x2 <detached ...>' + suffix, "751440 lsetxattr(0x1, \"/p\", \"\", 0, 0) = ?" + suffix):
                with self.subTest(line=line):
                    document = filtered([line.rstrip("\n") + "\n"])
                    self.assertEqual((document["coverage"]["detached_markers"], document["coverage"]["unresolved_reasons"]), (1, {}))
                    self.assertEqual(document["detached_samples"], [line.rstrip()])

    def test_samples_are_aliased_truncated_deduplicated_and_capped(self) -> None:
        t = real.TREE
        long_line = f'9 lsetxattr("{t}/backend/{"n" * 400}", 0x1, 0x2, 3, 4) = ?'
        document = filtered([long_line])
        sample, = document["detached_samples"]
        self.assertTrue(sample.startswith('9 lsetxattr("{TREE}/backend/nnnn'), sample[:60])
        self.assertEqual(len(sample), ds.SAMPLE_LINE_CHARS)
        self.assertNotIn(real.ROOT, sample)
        same = [f"{500} lsetxattr(0x1, 0x2, 3, 4) = ?"] * 3
        document = filtered(same)
        self.assertEqual((document["coverage"]["detached_markers"], document["detached_samples"]), (3, [same[0]]), "同样的行计数三次、样例只留一条")
        many = [f"{600 + index} lsetxattr(0x1, 0x2, 3, 4) = ?" for index in range(12)]
        document = filtered(many)
        self.assertEqual(document["coverage"]["detached_markers"], 12)
        self.assertEqual(document["detached_samples"], many[:8], "最多 8 条，先到先得")
        self.assertEqual(ds.validate_raw(document), [])

    def test_chunk_size_does_not_change_which_lines_are_dropped(self) -> None:
        text = real.GARBLED_DETACHED_A + real.GARBLED_LOST_RESULT_B + real.GARBLED_BARE_MARKER_D + real.GARBLED_UNFINISHED_POINTER_C
        results = []
        for chunk in (0, 1, 3, 100000):
            document = ds.filter_stream(text.splitlines(True), ALIASES, unit_id=UNIT, chunk_lines=chunk)
            results.append((document["coverage"], document["detached_samples"], accesses(document)))
        self.assertTrue(all(item == results[0] for item in results))

    def test_the_command_line_filter_writes_the_optional_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw.json"
            command = [sys.executable, "-B", str(ds.HERE), "filter", "--output", str(output), "--unit-id", UNIT]
            for name, value in ALIASES.items():
                command += ["--alias", f"{name}={value}"]
            completed = subprocess.run(command, input=(real.GARBLED_DETACHED_A + real.GARBLED_LOST_RESULT_B).encode(), capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            document = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(document["detached_samples"], [GARBAGE_A, GARBAGE_B])
            self.assertEqual(document["coverage"]["detached_markers"], 2)
            self.assertEqual(ds.validate_raw(document), [])

    def test_raw_validation_accepts_old_files_and_refuses_bad_samples(self) -> None:
        document = filtered(real.GARBLED_DETACHED_A)
        self.assertEqual(ds.validate_raw(document), [])
        old = {key: value for key, value in document.items() if key != "detached_samples"}
        self.assertEqual(ds.validate_raw(old), [], "旧 raw 没有这个键照样通过")
        for name, value in (("超过 8 条", ["x"] * 9), ("不是列表", "x"), ("有非字符串", [1]), ("超长", ["x" * 301])):
            with self.subTest(name):
                self.assertTrue(ds.validate_raw({**document, "detached_samples": value}))
        self.assertTrue(ds.validate_raw({**document, "mystery": 1}), "顶层未知键仍然拒绝")


class GarbledLineMergeTests(FollowupBase):
    def lines_with_markers(self, count: int, *, first_pid: int = 7000) -> list[str]:
        return [f"{first_pid + index} lsetxattr(0xffffffffffffff00, \"/usr/local/go/src/internal/abi\", \"\", 0, 0) = ?" for index in range(count)]

    def test_markers_are_tolerated_up_to_64_across_runs_and_the_samples_reach_the_review(self) -> None:
        for count, valid in ((1, True), (63, True), (64, True), (65, False)):
            with self.subTest(count=count):
                sample = self.merge(self.raw(self.good() + self.lines_with_markers(count)))
                self.assertEqual(sample["coverage"]["detached_markers"], count)
                self.assertEqual(sample["valid"], valid, sample["invalid_reasons"])
                self.assertEqual(ds.validate_sample(sample), [])
                self.assertEqual(len(sample["review"]["detached_samples"]), min(count, 8), "样例最多 8 条")
                if not valid:
                    self.assertTrue(any(reason.startswith("[detached]") and "共 65 条" in reason and "上限 64" in reason for reason in sample["invalid_reasons"]),
                                    sample["invalid_reasons"])
        half = self.merge(self.raw(self.good() + self.lines_with_markers(32)), self.raw(self.good() + self.lines_with_markers(32), label="warm", cache="warm"))
        self.assertTrue(half["valid"], half["invalid_reasons"])
        self.assertEqual(half["coverage"]["detached_markers"], 64, "上限按合并后的总数算")
        self.assertEqual(len(half["review"]["detached_samples"]), 8, "两次运行相同的样例行去重")
        over = self.merge(self.raw(self.good() + self.lines_with_markers(33)), self.raw(self.good() + self.lines_with_markers(33), label="warm", cache="warm"))
        self.assertFalse(over["valid"])
        self.assertTrue(any("共 66 条" in reason for reason in over["invalid_reasons"]))

    def test_bare_markers_count_toward_the_cap_too_but_leave_no_sample(self) -> None:
        bare = [f"{7000 + index} ???( <detached ...>" for index in range(65)]
        sample = self.merge(self.raw(self.good() + bare))
        self.assertEqual(sample["coverage"]["detached_markers"], 65)
        self.assertFalse(sample["valid"])
        self.assertNotIn("detached_samples", sample["review"])

    def test_a_clean_sample_has_no_new_review_key_and_old_raws_still_merge(self) -> None:
        sample = self.merge(self.raw(self.good()))
        self.assertEqual(set(sample["review"]), ds.REVIEW_KEYS)
        raw = self.raw(self.good() + self.lines_with_markers(1))
        self.assertIn("detached_samples", raw)
        old = {key: value for key, value in raw.items() if key != "detached_samples"}
        merged = self.merge(old)
        self.assertNotIn("detached_samples", merged["review"])
        self.assertEqual(merged["coverage"]["detached_markers"], 1, "计数在 coverage 里，样例缺了也不影响")

    def test_review_samples_must_be_a_short_list_of_short_strings(self) -> None:
        sample = self.merge(self.raw(self.good() + self.lines_with_markers(2)))
        for name, value in (("超过 8 条", ["x"] * 9), ("不是列表", {"a": 1}), ("超长", ["x" * 301])):
            with self.subTest(name):
                broken = json.loads(json.dumps(sample))
                broken["review"]["detached_samples"] = value
                self.assertTrue(ds.validate_sample(broken))

    def test_the_garbled_real_lines_no_longer_invalidate_a_sample(self) -> None:
        """端到端：夹具里的真实垃圾行混进一份本来有效的采样，merge 后仍然有效（之前会判 plain-path-not-resolved 等无效）。"""
        t = str(self.tree)
        garbage = [GARBAGE_A, GARBAGE_B, BARE_D]
        sample = self.merge(self.raw(self.good() + garbage + [open_line(t, f"{t}/backend/a.go")]))
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        self.assertEqual(sample["coverage"]["detached_markers"], 3)
        self.assertEqual(sample["review"]["detached_samples"], [GARBAGE_A, GARBAGE_B])
        self.assertEqual(ds.validate_sample(sample), [])
        # 对照：垃圾行以外，真正未建模的调用仍然判无效。
        bad = self.merge(self.raw(self.good() + garbage + ["4242 frobnicate(1) = 0"]))
        self.assertFalse(bad["valid"])
        self.assertTrue(any("unsupported-call:frobnicate" in reason for reason in bad["invalid_reasons"]))
        # connect 旁路：真实的 connect 行照常收集目的地，不会被垃圾行规则误吞。
        net = self.merge(self.raw(self.good() + [connect_line('{sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("1.2.3.4")}')]))
        self.assertFalse(net["valid"])
        self.assertTrue(any(reason.startswith("[network]") for reason in net["invalid_reasons"]))
        git = self.merge(self.raw(self.good() + [git_line(["git", "show", f"{SHA}:a.json"])]))
        self.assertEqual(git["review"]["network"]["violations"], [])


class GarbledLineDraftTests(DraftBase):
    def test_the_review_lists_the_dropped_marker_lines_and_the_count(self) -> None:
        lines = self.good() + [GARBAGE_A, GARBAGE_B, BARE_D]
        sample = self.merge(self.raw(lines))
        registry, review = self.draft(sample)
        self.assertEqual(registry["units"][UNIT]["status"], "declared")
        section = review.split("### 12 ")[1]
        self.assertIn("被丢弃的 strace 标记行", section.splitlines()[0])
        self.assertIn("共 3 条", section)
        self.assertIn(f"`{GARBAGE_A}`", section.replace("\\|", "|"))
        self.assertIn(f"`{GARBAGE_B}`", section)
        self.assertIn("容忍上限是 64", section)

    def test_only_bare_markers_say_so_and_no_marker_means_no_section(self) -> None:
        _registry, review = self.draft(self.merge(self.raw(self.good() + [BARE_D])))
        section = review.split("### 12 ")[1]
        self.assertIn("共 1 条", section)
        self.assertIn("没有带名字的垃圾行，只有裸标记", section)
        _registry, clean = self.draft(self.merge(self.raw(self.good())))
        self.assertNotIn("### 12 ", clean, "没有标记行就不写这一节")

    def test_a_sample_over_the_cap_is_rerun_only_with_the_reason(self) -> None:
        markers = [f"{7000 + index} lsetxattr(0xffffffffffffff00, \"/p\", \"\", 0, 0) = ?" for index in range(65)]
        registry, review = self.draft(self.merge(self.raw(self.good() + markers)))
        entry = registry["units"][UNIT]
        self.assertEqual(entry["status"], "rerun-only")
        self.assertIn("[detached]", entry["reason"])
        self.assertIn("上限 64", entry["reason"])
        self.assertIn("[detached]", review)


if __name__ == "__main__":
    unittest.main()
