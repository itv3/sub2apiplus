"""读集采样器（``tools/ci/declared_sampler.py``）对 strace“中途消失的线程”垃圾行的处理：步骤 5 的 WP4c 之 T1。

乱码事件的根因：不是过滤器 bug，是 strace 对 Go 运行时被杀或退出的线程打印的垃圾行——它把线程当作还在系统调用里，用残留寄存器解码出
名字、参数都乱七八糟的一行。四种形态（夹具里是 ARM64 对照实验抓到的原样真实行，见 ``declared_sampler_trace_fixture``）：

* A：名字是随机的系统调用名，以 ``<detached ...>`` 收尾；
* B：以 `` = ?`` 收尾，第一个参数是指针常量，第二个参数恰好像路径；
* C：路径参数是指针的 ``<unfinished ...>`` 起始行，永远没有 resumed——按 ``unfinished-calls-at-eof`` 容忍，不丢；
* D：不带名字的裸标记 ``???( <detached ...>``——一直按标记丢弃计数。

A、B、D 现在都在 filter 里丢弃并计入 ``coverage.detached_markers``（不新增 coverage 键：登记侧对它是闭合的），带名字的 A、B 另留原样样例
（raw 顶层 ``detached_samples``，merge 后进 ``review.detached_samples``，评审清单第 12 节列出）；合并后总数超过 64 判采样无效。

步骤 5 WP4d 的 S1 在此之上加两项（``SideEffectClassTests``、``DetachedNotTerminalTests``、``SideEffectMergeTests``）：

* 被丢弃的行里“名字属于副作用类”的条数单独计数（raw 顶层 ``detached_side_effects``，merge 后求和进 ``review.detached_side_effects``，只给评审看、不改有效性），
  ``detached_samples`` 的 8 条上限里副作用类优先留样（在前）；
* 被丢弃的、带 tid 前缀的垃圾行，它的 tid 之后又出现任何行，就不是“中途消失的线程”：记 ``sampler:detached-not-terminal:<tid>``（同一个 tid 只记一次，原因键最多 20 个，
  超出合并成 ``…:more``），采样无效。追踪表内存有界，不碰块排空，分块不变性（chunk_lines 取 0、1、3、7、100、999、100000）照旧成立。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


REQUIRED_SIDE_EFFECT_NAMES = frozenset({
    "unlink", "unlinkat", "rename", "renameat", "renameat2", "link", "linkat", "symlink", "symlinkat", "mkdir", "mkdirat", "rmdir", "truncate", "truncate64",
    "chmod", "fchmod", "fchmodat", "chown", "fchown", "lchown", "fchownat", "setxattr", "lsetxattr", "fsetxattr", "connect", "sendto"})


# 副作用类调用名的完整名单（钉住：加减任何一项都要有意识地改这里）。除调度者点名的 REQUIRED_SIDE_EFFECT_NAMES 之外，还有它们的 *at／64／32 变体、
# fchmodat2、setxattrat／removexattrat（新内核）、removexattr 系列、改时间戳的 utime* 、creat、会在文件系统里建节点的 mknod* 与 bind、sendmsg／sendmmsg、
# 改挂载的 mount／umount／umount2／pivot_root。
EXPECTED_SIDE_EFFECT_NAMES = frozenset({
    "unlink", "unlinkat", "rename", "renameat", "renameat2", "link", "linkat", "symlink", "symlinkat", "mkdir", "mkdirat", "rmdir", "mknod", "mknodat",
    "truncate", "truncate64", "ftruncate", "ftruncate64", "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "chown32", "fchown", "fchown32", "lchown", "lchown32",
    "fchownat", "setxattr", "lsetxattr", "fsetxattr", "setxattrat", "removexattr", "lremovexattr", "fremovexattr", "removexattrat", "utime", "utimes", "utimensat",
    "futimesat", "creat", "connect", "sendto", "sendmsg", "sendmmsg", "bind", "mount", "umount", "umount2", "pivot_root"})


class SideEffectClassTests(unittest.TestCase):
    """S1 (a)：被丢弃的垃圾行里“名字属于副作用类”的单独计数，副作用类优先留样。名字随机或解析不出来的不算。"""

    def test_the_required_names_are_in_the_set_and_every_listed_name_is_recognized(self) -> None:
        self.assertEqual(ds.SIDE_EFFECT_CALLS, EXPECTED_SIDE_EFFECT_NAMES, "名单逐项钉住")
        self.assertEqual(ds._OPEN_CALLS, frozenset({"open", "openat", "openat2"}))
        self.assertEqual(REQUIRED_SIDE_EFFECT_NAMES - ds.SIDE_EFFECT_CALLS, set(), "调度者点名的调用名一个都不能少")
        for name in sorted(ds.SIDE_EFFECT_CALLS):
            self.assertTrue(ds.side_effect_call(f"711353 {name}(0x1, 0x2 <detached ...>"), name)
            self.assertTrue(ds.side_effect_call(f'751440 {name}(0xffffffffffffff00, "/p", 0) = ?'), name)
        self.assertTrue(ds.SIDE_EFFECT_CALLS.isdisjoint({"openat", "open", "openat2", "newfstatat", "getdents64", "execve", "read"}), "open 族只看标志，读类不在集合里")

    def test_open_calls_count_only_with_a_write_flag(self) -> None:
        for name in ("open", "openat", "openat2"):
            for flags, expected in (("O_WRONLY", True), ("O_RDWR|O_CLOEXEC", True), ("O_RDONLY|O_CREAT", True), ("O_RDONLY|O_TRUNC", True),
                                    ("O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666", True), ("O_RDONLY|O_CLOEXEC", False), ("O_RDONLY", False),
                                    ("O_RDONLY|O_NOFOLLOW|O_DIRECTORY", False), ("0", False), ("O_RDONLYX", False), ("XO_WRONLY", False)):
                with self.subTest(name=name, flags=flags):
                    self.assertIs(ds.side_effect_call(f'711353 {name}(AT_FDCWD</x>, "/x/f", {flags}) = ?'), expected)
        self.assertTrue(ds.side_effect_call('711353 creat("/x/f", 0666) = ?'), "creat 一定创建")

    def test_random_unresolvable_and_look_alike_names_do_not_count(self) -> None:
        for line in ("711353 syscall_0x15a17b(0, 0x42ce66593f50 <detached ...>", "711353 ???(0x1 <detached ...>", "711353 ???( <detached ...>", "711353 newfstatat(AT_FDCWD</x>, 0x1) = ?",
                     "711353 getdents64(3</x>, 0x1, 8) = ?", '711353 execve("/usr/bin/git", 0x1, 0x2 <detached ...>', "711353 read(3</x>, 0x1, 8) = ?",
                     "711353 xunlink(0x1) = ?", "711353 unlinkx(0x1) = ?", "711353 connect_x(0x1) = ?", "711353 unlink-at(0x1) = ?", "711353 Unlinkat(0x1) = ?",
                     "unlinkat(0x1) = ?", "杂散输出 = ?", "", " 711353 unlinkat(0x1) = ?", "711353unlinkat(0x1) = ?", "711353 (0x1) = ?"):
            with self.subTest(line=line):
                self.assertFalse(ds.side_effect_call(line))

    def test_filter_counts_side_effect_lines_separately_and_keeps_every_other_number(self) -> None:
        lines = [
            '711001 unlinkat(AT_FDCWD</x>, "/x/f", 0) = ?',  # 副作用类（形态 B）
            '711002 renameat2(AT_FDCWD</x>, "a", AT_FDCWD</x>, "b", 0 <detached ...>',  # 副作用类（形态 A）
            '711003 openat(AT_FDCWD</x>, "/x/f", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = ?',  # open 族带写入标志
            '711004 openat(AT_FDCWD</x>, "/x/f", O_RDONLY|O_CLOEXEC) = ?',  # 读类
            '711005 connect(3<socket:[1]>, 0x7ffd, 16 <detached ...>',  # 对外连接
            '711006 syscall_0x15a17b(0, 0x42ce66593f50, 0, 0x8, 0, 0x2 <detached ...>',  # 随机名：不算
            '711007 ???(0x1 <detached ...>',  # 名字是 ???：不算
            '711008 lsetxattr(0xffffffffffffff00, "/p", "", 0, 0) = ?',  # 副作用类（真实垃圾行的名字就是它）
            '711009 ???( <detached ...>',  # 裸标记：没有名字，不算
            '711010 newfstatat(AT_FDCWD</x>, 0x1, 0x2, 0) = ?',  # 读类
        ]
        document = filtered(lines)
        self.assertEqual(document["coverage"]["detached_markers"], 10, "总数不变：副作用类是其中的一部分")
        self.assertEqual(document["detached_side_effects"], 5)
        self.assertEqual(set(document["coverage"]) - ds.COVERAGE_KEYS, set(), "不新增 coverage 键（登记侧对它是闭合的）")
        self.assertEqual(document["coverage"]["unresolved_reasons"], {}, "副作用类的行也只是计数留样，不产生未解析原因")
        self.assertEqual(document["detached_samples"], [lines[0], lines[1], lines[2], lines[4], lines[7], lines[3], lines[5], lines[6]],
                         "副作用类在前（各自按到达顺序），其它在后，合起来最多 8 条；裸标记没有样例")
        self.assertEqual(ds.validate_raw(document), [])

    def test_no_side_effect_line_means_no_key_and_the_output_is_what_it_was_before(self) -> None:
        document = filtered(["711006 syscall_0x15a17b(0x1 <detached ...>", "711007 ???(0x1 <detached ...>", "711009 ???( <detached ...>",
                             '711004 openat(AT_FDCWD</x>, "/x/f", O_RDONLY|O_CLOEXEC) = ?'])
        self.assertNotIn("detached_side_effects", document)
        self.assertEqual(document["coverage"]["detached_markers"], 4)
        self.assertEqual(document["detached_samples"], ["711006 syscall_0x15a17b(0x1 <detached ...>", "711007 ???(0x1 <detached ...>",
                                                        '711004 openat(AT_FDCWD</x>, "/x/f", O_RDONLY|O_CLOEXEC) = ?'], "没有副作用类时仍是先到先得")
        self.assertNotIn("detached_side_effects", filtered(real.GARBLED_BARE_MARKER_D))
        # 夹具里的两条真实垃圾行都是 lsetxattr：副作用类。
        self.assertEqual(filtered(real.GARBLED_DETACHED_A)["detached_side_effects"], 1)
        self.assertEqual(filtered(real.GARBLED_LOST_RESULT_B)["detached_side_effects"], 1)
        both = filtered(real.GARBLED_DETACHED_A + real.GARBLED_LOST_RESULT_B + real.GARBLED_BARE_MARKER_D)
        self.assertEqual((both["coverage"]["detached_markers"], both["detached_side_effects"]), (3, 2))

    def test_side_effect_lines_take_the_sample_slots_first_when_the_cap_is_reached(self) -> None:
        others = [f"{700 + index} syscall_0x{index:04x}(0x1 <detached ...>" for index in range(12)]
        side = [f'{800 + index} unlinkat(AT_FDCWD</x>, "/x/f{index}", 0) = ?' for index in range(3)]
        document = filtered(others + side)  # 副作用类排在流的最后，也要进样例
        self.assertEqual(document["detached_samples"], side + others[:5], "副作用类 3 条在前，剩下的 5 个名额给其它类（先到先得）")
        self.assertEqual((document["coverage"]["detached_markers"], document["detached_side_effects"]), (15, 3))
        many_side = [f'{900 + index} unlinkat(AT_FDCWD</x>, "/x/f{index}", 0) = ?' for index in range(12)]
        document = filtered(others[:4] + many_side)
        self.assertEqual(document["detached_samples"], many_side[:8], "副作用类自己超过 8 条：先到先得，其它类一条也挤不进来")
        self.assertEqual(document["detached_side_effects"], 12)
        self.assertEqual(ds.validate_raw(document), [])
        same = ['500 unlinkat(AT_FDCWD</x>, "/x/f", 0) = ?'] * 3
        document = filtered(same)
        self.assertEqual((document["detached_samples"], document["detached_side_effects"]), ([same[0]], 3), "同样的行计数三次、样例只留一条")

    def test_raw_validation_of_the_count_key(self) -> None:
        document = filtered(real.GARBLED_DETACHED_A)
        self.assertEqual(document["detached_side_effects"], 1)
        old = {key: value for key, value in document.items() if key != "detached_side_effects"}
        self.assertEqual(ds.validate_raw(old), [], "旧 raw 没有这个键照样通过")
        for name, value in (("负数", -1), ("字符串", "1"), ("布尔", True), ("小数", 1.5), ("列表", [1]), ("超过 detached_markers", 2)):
            with self.subTest(name):
                self.assertTrue(ds.validate_raw({**document, "detached_side_effects": value}), name)
        self.assertEqual(ds.validate_raw({**document, "detached_side_effects": 0}), [], "0 也是合法值（只是不会被写出来）")

    def test_the_command_line_filter_writes_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw.json"
            command = [sys.executable, "-B", str(ds.HERE), "filter", "--output", str(output), "--unit-id", UNIT]
            for name, value in ALIASES.items():
                command += ["--alias", f"{name}={value}"]
            completed = subprocess.run(command, input=(real.GARBLED_DETACHED_A + real.GARBLED_LOST_RESULT_B).encode(), capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            document = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual((document["detached_side_effects"], document["coverage"]["detached_markers"]), (2, 2))
            self.assertEqual(ds.validate_raw(document), [])


class DetachedNotTerminalTests(unittest.TestCase):
    """S1 (b)：被丢弃的、带 tid 前缀的垃圾行（含裸标记），它的 tid 之后又出现了任何行，就说明那个线程没有消失，记 sampler:detached-not-terminal:<tid>。"""

    TREE = real.TREE

    def later(self, tid: int, name: str = "backend/go.mod") -> str:
        return open_line(self.TREE, f"{self.TREE}/{name}", pid=tid)

    def test_a_tid_that_appears_again_is_reported_once_with_both_lines_as_samples(self) -> None:
        for label, garbage, tid in (("形态 A", GARBAGE_A, 711353), ("形态 B", GARBAGE_B, 751440), ("裸 detached 标记", BARE_D, 736020),
                                    ("裸 unfinished 标记", "736030 ???( <unfinished ...>", 736030)):
            with self.subTest(label):
                later = self.later(tid)
                document = filtered([garbage, later])
                reason = f"sampler:detached-not-terminal:{tid}"
                reasons = document["coverage"]["unresolved_reasons"]
                self.assertEqual(reasons, {reason: 1})
                self.assertEqual(document["coverage"]["unresolved"], 1)
                self.assertEqual(document["coverage"]["detached_markers"], 1, "垃圾行照旧丢弃并计数")
                shown = ds.AliasTable(ALIASES).apply_text
                self.assertEqual(document["coverage"]["unresolved_samples"][reason], [shown(garbage), shown(later)[:ds.SAMPLE_LINE_CHARS]], "样例：被丢弃的垃圾行，和它之后第一次再出现的那一行")
                self.assertFalse(ds.reason_tolerated(reason), "不在容忍集合内：采样判无效")
                self.assertEqual(ds.validate_raw(document), [])

    def test_only_the_same_tid_after_the_garbage_line_counts(self) -> None:
        later_normal = self.later(4242)
        cases = {
            "tid 从未再出现": [GARBAGE_A, later_normal, self.later(711354), self.later(71135), self.later(7113530)],
            "tid 只在垃圾行之前出现": [self.later(711353), self.later(711353), GARBAGE_A, later_normal],
            "前缀碰撞：另一个 tid 恰好以它开头或是它的开头": [GARBAGE_A, self.later(71135), self.later(7113531)],
            "tid 只作为内容出现在后面的行里": [GARBAGE_A, f'4242 openat(AT_FDCWD<{self.TREE}>, "/proc/711353/status", O_RDONLY) = 3</proc/711353/status>', "杂散 711353 输出"],
            "没有 tid 前缀的行": [GARBAGE_A, "711353", "=?", "<detached ...>"],
        }
        for label, lines in cases.items():
            with self.subTest(label):
                reasons = filtered(lines)["coverage"]["unresolved_reasons"]
                self.assertFalse([reason for reason in reasons if "detached-not-terminal" in reason], reasons)

    def test_any_kind_of_later_line_counts_and_a_tid_is_reported_once(self) -> None:
        for label, later in (("resumed 行", "711353 <... openat resumed>) = 3</x>"), ("退出提示", "711353 +++ exited with 0 +++"), ("信号行", "711353 --- SIGCHLD {si_signo=SIGCHLD} ---"),
                             ("另一条垃圾行", "711353 lsetxattr(0x1, 0x2 <detached ...>"), ("裸标记", "711353 ???( <detached ...>"), ("普通调用", self.later(711353))):
            with self.subTest(label):
                reasons = filtered([GARBAGE_A, later, self.later(711353), self.later(711353), later])["coverage"]["unresolved_reasons"]
                self.assertEqual({key: count for key, count in reasons.items() if "detached-not-terminal" in key}, {"sampler:detached-not-terminal:711353": 1},
                                 "不管之后出现多少行、哪一种行，同一个 tid 只记一次")
        three = ["500 lsetxattr(0x1, 0x2, 3, 4) = ?"] * 3  # 一个线程不会消失三次
        document = filtered(three)
        self.assertEqual((document["coverage"]["detached_markers"], document["coverage"]["unresolved_reasons"]), (3, {"sampler:detached-not-terminal:500": 1}))
        # 已经判定不是终结的 tid 不再重新登记：样例固定为“第一条垃圾行＋之后第一次再出现的那一行”，后面再来的垃圾行与再出现的行都不会覆盖它。
        shown = ds.AliasTable(ALIASES).apply_text
        first_later, second_garbage, second_later = self.later(711353), "711353 lsetxattr(0x9 <detached ...>", self.later(711353, "docs/other.md")
        document = filtered([GARBAGE_A, first_later, second_garbage, second_later])
        self.assertEqual(document["coverage"]["unresolved_reasons"], {"sampler:detached-not-terminal:711353": 1})
        self.assertEqual(document["coverage"]["unresolved_samples"]["sampler:detached-not-terminal:711353"], [shown(GARBAGE_A), shown(first_later)])
        self.assertEqual(document["coverage"]["detached_markers"], 2, "第二条垃圾行照旧丢弃计数")

    def test_reason_keys_are_bounded_and_the_total_still_counts_every_tid(self) -> None:
        count = ds.MAX_SAMPLE_REASONS + 5
        lines = [f"{1000 + index} lsetxattr(0x1, 0x2 <detached ...>" for index in range(count)] + [self.later(1000 + index) for index in range(count)]
        document = filtered(lines)
        reasons = document["coverage"]["unresolved_reasons"]
        individual = [key for key in reasons if key.startswith("sampler:detached-not-terminal:") and not key.endswith(":more")]
        self.assertEqual(len(individual), ds.MAX_SAMPLE_REASONS, "不同的原因键最多 20 个")
        self.assertEqual(reasons["sampler:detached-not-terminal:more"], 5, "超出的合并成 more，计数是超出的 tid 数")
        self.assertEqual(sorted(individual), sorted(f"sampler:detached-not-terminal:{1000 + index}" for index in range(ds.MAX_SAMPLE_REASONS)), "先出现的 20 个 tid")
        self.assertEqual((document["coverage"]["unresolved"], sum(reasons.values())), (count, count), "总计数仍是 tid 数，unresolved 等于各原因计数之和")
        samples = document["coverage"]["unresolved_samples"]
        self.assertLessEqual(len(samples), ds.MAX_SAMPLE_REASONS, "样例的原因数受现有上限约束")
        self.assertEqual(ds.validate_raw(document), [])

    def test_the_tid_table_is_bounded_and_overflow_fails_closed(self) -> None:
        lines = [f"{2000 + index} lsetxattr(0x1, 0x2 <detached ...>" for index in range(5)] + [self.later(2000), self.later(2004)]
        self.assertEqual(ds.MAX_DETACHED_TIDS, 1000, "默认上限钉住：每个 tid 存一条 ≤300 字符的别名化垃圾行，内存上限约 300 KB")
        with mock.patch.object(ds, "MAX_DETACHED_TIDS", 3):
            document = filtered(lines)
        reasons = document["coverage"]["unresolved_reasons"]
        self.assertEqual(reasons["sampler:limit:detached-tids"], 2, "追踪表满了：第 4、5 个 tid 没法核对，失败关闭")
        self.assertEqual(reasons["sampler:detached-not-terminal:2000"], 1)
        self.assertNotIn("sampler:detached-not-terminal:2004", reasons, "没追踪的 tid 不报具体原因，由上限原因兜底")
        self.assertEqual(document["coverage"]["detached_markers"], 5, "垃圾行照旧全部丢弃计数")
        self.assertEqual(ds.validate_raw(document), [])

    def test_clean_streams_never_pay_for_the_check_and_it_stops_when_nothing_is_tracked(self) -> None:
        aggregator = ds.StreamAggregator(ALIASES, unit_id=UNIT)
        clean = [self.later(4242, f"backend/f{index}.go") for index in range(300)]
        with mock.patch.object(ds.StreamAggregator, "_seen_again", autospec=True, side_effect=ds.StreamAggregator._seen_again) as check:
            for line in clean:
                aggregator.feed(line + "\n")
            self.assertEqual(check.call_count, 0, "没有垃圾行时一次也不进检查")
            aggregator.feed(GARBAGE_A + "\n")
            self.assertEqual(check.call_count, 0, "垃圾行自己进来时追踪表还是空的")
            for line in clean[:10]:
                aggregator.feed(line + "\n")
            self.assertEqual(check.call_count, 10, "见过垃圾行之后，每行问一次")
            aggregator.feed(self.later(711353) + "\n")  # 这个 tid 回来了：移出追踪表，表空了
            before = check.call_count
            for line in clean[:10]:
                aggregator.feed(line + "\n")
            self.assertEqual(check.call_count, before, "表空了就不再问")
        self.assertEqual(aggregator.finish()["coverage"]["unresolved_reasons"], {"sampler:detached-not-terminal:711353": 1})

    def test_chunk_size_changes_nothing_the_new_checks_report(self) -> None:
        """S1／S3 的新检查不碰块排空：分块不变性（chunk_lines 取 0、1、3、7、100、999、100000）下，coverage（含未解析原因与样例）、样例、副作用计数、读集逐项相同。"""
        t = self.TREE
        stream = [
            self.later(100), GARBAGE_A, GARBAGE_B, BARE_D, "736030 ???( <unfinished ...>", self.later(711353), self.later(100, "backend/a.go"),
            '800 unlinkat(AT_FDCWD</x>, "/x/f", 0) = ?', "801 syscall_0x15a17b(0x1 <detached ...>", '802 openat(AT_FDCWD</x>, "/x/g", O_WRONLY|O_CREAT, 0666) = ?',
            self.later(802), git_line(["git", "show", f"{SHA}:a.json"]),
            f'4243 execveat(3</usr/bin/git>, "", ["gitx", "show", "{SHA}"], 0x1, AT_EMPTY_PATH) = 0', '4244 execveat(3, "", ["sh", "-c", "x"], 0x1, AT_EMPTY_PATH) = 0',
            '4245 execve(0x7ffd1234, 0x1, 0x2) = 0', connect_line('{sa_family=AF_INET, sin_port=htons(9), sin_addr=inet_addr("127.0.0.1")}'),
            f'4246 connect(5<socket:[2]>, {{sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("10.0.0.2")}}, 16 <unfinished ...>',
            self.later(4247), "4246 <... connect resumed>) = -1 EINPROGRESS (Operation now in progress)",
            f'4248 openat(AT_FDCWD<{t}>, "{t}/backend/go.mod", O_RDONLY <unfinished ...>', self.later(4249, "docs/other.md"),
            f"4248 <... openat resumed>) = 3<{t}/backend/go.mod>",
        ] * 3
        results = []
        for chunk in (0, 1, 3, 7, 100, 999, 100000):
            document = ds.filter_stream([line + "\n" for line in stream], ALIASES, unit_id=UNIT, chunk_lines=chunk)
            self.assertEqual(ds.validate_raw(document), [])
            results.append((document["coverage"], document["detached_samples"], document["detached_side_effects"], accesses(document)))
        self.assertTrue(all(item == results[0] for item in results), "分块只影响内存，不影响任何新检查的结果")
        coverage = results[0][0]
        self.assertIn("sampler:detached-not-terminal:711353", coverage["unresolved_reasons"], "夹具里这个 tid 确实回来了：断言不是空转")
        self.assertIn("sampler:exec-unparsed", coverage["unresolved_reasons"])
        self.assertEqual(results[0][2], 3 * 4, "每份副本里 GARBAGE_A、GARBAGE_B、unlinkat、openat 写入各一条副作用类")


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


class SideEffectMergeTests(FollowupBase):
    """S1：副作用类计数与“线程消失核对”在 merge 之后的样子。"""

    def test_side_effect_counts_are_summed_across_runs_and_samples_are_ordered_side_effect_first(self) -> None:
        cold = self.raw(self.good() + ["900 syscall_0x1(0x1 <detached ...>", "901 syscall_0x2(0x1 <detached ...>", '902 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?'])
        warm = self.raw(self.good() + ['903 renameat2(AT_FDCWD</x>, "a", AT_FDCWD</x>, "b", 0) = ?', "904 ???(0x1 <detached ...>", '905 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?'],
                        label="warm", cache="warm")
        self.assertEqual((cold["detached_side_effects"], warm["detached_side_effects"]), (1, 2))
        sample = self.merge(cold, warm)
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        self.assertEqual(sample["coverage"]["detached_markers"], 6)
        self.assertEqual(sample["review"]["detached_side_effects"], 3, "各次运行求和")
        self.assertEqual(sample["review"]["detached_samples"], ['902 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?', '903 renameat2(AT_FDCWD</x>, "a", AT_FDCWD</x>, "b", 0) = ?',
                                                              '905 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?', "900 syscall_0x1(0x1 <detached ...>",
                                                              "901 syscall_0x2(0x1 <detached ...>", "904 ???(0x1 <detached ...>"],
                         "跨两次运行：副作用类在前（各自按到达顺序），其它在后；即使它们出现在后一次运行里")
        self.assertEqual(ds.validate_sample(sample), [])
        # 满额时副作用类挤掉其它类，而不是反过来：冷缓存 8 条随机名占满，热缓存里的副作用类照样进样例。
        full_cold = self.raw(self.good() + [f"{950 + index} syscall_0x{index:x}(0x1 <detached ...>" for index in range(8)])
        sample = self.merge(full_cold, warm)
        self.assertEqual(sample["review"]["detached_samples"][:3], ['903 renameat2(AT_FDCWD</x>, "a", AT_FDCWD</x>, "b", 0) = ?', '905 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?',
                                                                  "950 syscall_0x0(0x1 <detached ...>"], "副作用类优先")
        self.assertEqual(len(sample["review"]["detached_samples"]), 8)

    def test_the_same_sample_lines_in_two_runs_are_listed_once(self) -> None:
        lines = ["900 syscall_0x1(0x1 <detached ...>", '902 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?', "903 ???(0x1 <detached ...>"]
        sample = self.merge(self.raw(self.good() + lines), self.raw(self.good() + lines, label="warm", cache="warm"))
        self.assertEqual(sample["review"]["detached_samples"], ['902 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?', "900 syscall_0x1(0x1 <detached ...>", "903 ???(0x1 <detached ...>"],
                         "冷热两次相同的行只留一条（副作用类在前）")
        self.assertEqual((sample["coverage"]["detached_markers"], sample["review"]["detached_side_effects"]), (6, 2), "计数各次求和")

    def test_no_side_effect_line_means_no_review_key_and_old_raws_still_merge(self) -> None:
        sample = self.merge(self.raw(self.good() + ["900 syscall_0x1(0x1 <detached ...>", "901 ???( <detached ...>"]))
        self.assertNotIn("detached_side_effects", sample["review"])
        self.assertEqual(set(sample["review"]) - ds.REVIEW_KEYS, {"detached_samples"})
        raw = self.raw(self.good() + ['902 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?'])
        old = {key: value for key, value in raw.items() if key != "detached_side_effects"}
        merged = self.merge(old)
        self.assertNotIn("detached_side_effects", merged["review"], "旧 raw 没有计数：不凭样例猜")
        self.assertEqual(merged["review"]["detached_samples"], ['902 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?'])
        self.assertEqual(self.merge(raw)["review"]["detached_side_effects"], 1)

    def test_the_count_is_review_only_and_never_changes_validity(self) -> None:
        many = [f'{7000 + index} unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?' for index in range(64)]
        sample = self.merge(self.raw(self.good() + many))
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        self.assertEqual(sample["review"]["detached_side_effects"], 64, "64 条以内的副作用类垃圾行不判无效：只计数留样（名字随机，不能按名字判）")
        over = self.merge(self.raw(self.good() + many + ['7099 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?']))
        self.assertFalse(over["valid"])
        self.assertTrue(all(reason.startswith("[detached]") for reason in over["invalid_reasons"]), "超过 64 条仍只因总数上限判无效")

    def test_the_review_count_must_be_a_non_negative_integer_not_above_the_marker_total(self) -> None:
        sample = self.merge(self.raw(self.good() + ['902 unlinkat(AT_FDCWD</x>, "/x/a", 0) = ?', "903 ???( <detached ...>"]))
        self.assertEqual((sample["coverage"]["detached_markers"], sample["review"]["detached_side_effects"]), (2, 1))
        self.assertEqual(ds.validate_sample(sample), [])
        for name, value in (("负数", -1), ("字符串", "1"), ("布尔", True), ("小数", 0.5), ("超过 detached_markers", 3)):
            with self.subTest(name):
                broken = json.loads(json.dumps(sample))
                broken["review"]["detached_side_effects"] = value
                self.assertTrue(ds.validate_sample(broken), name)
        ok = json.loads(json.dumps(sample))
        ok["review"]["detached_side_effects"] = 2
        self.assertEqual(ds.validate_sample(ok), [], "恰好等于总数是合法的")

    def test_a_garbage_tid_that_comes_back_makes_the_sample_invalid_with_both_lines_as_evidence(self) -> None:
        t = str(self.tree)
        garbage = '7000 lsetxattr(0xffffffffffffff00, "/usr/local/go/src/internal/abi", "", 0, 0) = ?'
        later = open_line(f"{t}/backend", f"{t}/backend/a.go", pid=7000)
        sample = self.merge(self.raw(self.good() + [garbage, later]))
        self.assertFalse(sample["valid"])
        self.assertTrue(any(reason.startswith("[unresolved]") and "sampler:detached-not-terminal:7000×1" in reason for reason in sample["invalid_reasons"]),
                        sample["invalid_reasons"])
        evidence = sample["review"]["unresolved_samples"]["sampler:detached-not-terminal:7000"]
        self.assertEqual(evidence[0], garbage)
        self.assertIn('7000 openat(AT_FDCWD<{TREE}/backend>, "{TREE}/backend/a.go"', evidence[1], "第二条是它之后第一次再出现的那一行（别名化）")
        self.assertEqual(sample["coverage"]["detached_markers"], 1)
        self.assertEqual(ds.validate_sample(sample), [])
        # 对照：同一个垃圾行，tid 没有回来，采样有效。
        clean = self.merge(self.raw(self.good() + [garbage, open_line(f"{t}/backend", f"{t}/backend/a.go", pid=7001)]))
        self.assertTrue(clean["valid"], clean["invalid_reasons"])


class GarbledLineDraftTests(DraftBase):
    def test_the_review_points_out_side_effect_lines_and_sorts_them_first(self) -> None:
        lines = self.good() + ["900 syscall_0x1(0x1 <detached ...>", GARBAGE_A, GARBAGE_B, BARE_D]
        registry, review = self.draft(self.merge(self.raw(lines)))
        self.assertEqual(registry["units"][UNIT]["status"], "declared")
        section = review.split("### 12 ")[1]
        self.assertIn("共 4 条", section)
        self.assertIn("**其中名字属于副作用类的有 2 条**", section)
        self.assertIn("trace 里不会有任何痕迹", section)
        self.assertLess(section.index(GARBAGE_A), section.index("900 syscall_0x1"), "副作用类的样例排在前面")
        self.assertIn(f"`{GARBAGE_A}` ← 副作用类", section.replace("\\|", "|"))
        self.assertIn(f"`{GARBAGE_B}` ← 副作用类", section)
        self.assertNotIn("syscall_0x1(0x1 <detached ...>` ← 副作用类", section)
        # 没有副作用类（只有随机名与裸标记）时不写这一条。
        _registry, quiet = self.draft(self.merge(self.raw(self.good() + ["900 syscall_0x1(0x1 <detached ...>", BARE_D])))
        self.assertNotIn("其中名字属于副作用类的有", quiet)
        self.assertNotIn("← 副作用类", quiet)

    def test_a_comeback_tid_turns_the_unit_into_rerun_only_with_the_reason(self) -> None:
        t = str(self.tree)
        lines = self.good() + ["7000 lsetxattr(0x1, 0x2 <detached ...>", open_line(f"{t}/backend", f"{t}/backend/a.go", pid=7000)]
        registry, review = self.draft(self.merge(self.raw(lines)))
        entry = registry["units"][UNIT]
        self.assertEqual(entry["status"], "rerun-only")
        self.assertIn("sampler:detached-not-terminal:7000", entry["reason"])
        section = review.split("### 9 ")[1].split("### 10")[0]
        self.assertIn("`sampler:detached-not-terminal:7000`", section)
        self.assertIn("7000 lsetxattr(0x1, 0x2 <detached ...>", section, "评审清单第 9 节列出被丢弃的垃圾行样例")

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
