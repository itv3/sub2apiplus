"""第二轮独立验收后的追加修复（与 WP-R3 同一分支、独立提交）：openat2 的 ``resolve=`` 字段、空路径的 AT_EMPTY_PATH 写。

验收报告：``claude-control-plane/20261015/独立验收-解析器加固第二轮.md``（缺陷清单 M-1 与“一般 3”）。只在 v4 轨迹合同下生效。

* M-1 ``openat2`` 的 ``resolve=``：第二轮的新 strace 选项让 ``struct open_how`` 首次可见，解析器却只读了 ``flags=``。
  ``RESOLVE_IN_ROOT`` 让内核把 ``..`` 钳在 dirfd 根、绝对路径也相对 dirfd，而解析器仍按字面做 ``normpath(dirfd/相对路径)``；
  成功的调用有返回描述符的真实路径注解兜底，**失败（ENOENT）的调用没有**，于是内核真正探测的 ``<dirfd>/…`` 宿主路径被记成
  另一条（可能恰在私有盘根下）的合法缺失探测，审计通过。修法：``resolve=`` 只认 ``0``，或只含 ``RESOLVE_BENEATH``、
  ``RESOLVE_NO_SYMLINKS``、``RESOLVE_NO_MAGICLINKS``、``RESOLVE_NO_XDEV``（它们只会让调用更容易失败，不改变被探测的路径）；
  其余（``RESOLVE_IN_ROOT``、``RESOLVE_CACHED``、未知值、缺字段、重复字段）一律 ``open-flags-not-decoded:openat2``。
* 空路径的写（评估项）：``utimensat(fd, "", ts, AT_EMPTY_PATH)`` 与第二轮实现的 ``futimens``（``utimensat(fd, NULL, …)``）是同类操作——
  对 fd 自己改时间；基线却在空路径分支直接 ``continue``，既不登记写也不判未解析，宿主文件以只读打开后用它改时间不会出现 write。
  结论：安全且简单，一并做——v4 下空路径配 ``AT_EMPTY_PATH`` 的 ``utimensat``／``fchownat``／``fchmodat2`` 按“对 dirfd 标注路径的写”登记
  （AT_FDCWD 配空路径作用于工作目录，标注就是它的路径）；标注不是绝对路径（管道、套接字）判未解析；不带 ``AT_EMPTY_PATH`` 的空路径
  调用内核恒返回 ENOENT，保持基线不登记。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests.test_ci_read_audit_wp_r2 import WORK, CorpusFixture

SAFE_RESOLVE = ("RESOLVE_BENEATH", "RESOLVE_NO_SYMLINKS", "RESOLVE_NO_MAGICLINKS", "RESOLVE_NO_XDEV")


class Openat2ResolveTests(CorpusFixture):
    def line(self, resolve: str | None, *, flags: str = "O_RDONLY", path: str | None = None, result: str | None = None,
             extra: str = "") -> str:
        target = path or f"{self.root}/base/f"
        how = f"flags={flags}" + (f", resolve={resolve}" if resolve is not None else "") + extra
        outcome = result or f"3<{target}>"
        return f'1 openat2(AT_FDCWD</repo>, "{target}", {{{how}}}, 24) = {outcome}'

    def prepared(self, line: str) -> list[str]:
        """私有根下先建 base 目录与 base/f 文件，再跟一条待测的 openat2 读。"""
        return [self.mkdir_line("base"), self.create_line("base/f"), line]

    def test_zero_and_every_non_widening_combination_are_accepted(self) -> None:
        values = ["0", *SAFE_RESOLVE, "|".join(SAFE_RESOLVE), "RESOLVE_NO_SYMLINKS|RESOLVE_NO_XDEV"]
        for value in values:
            with self.subTest(resolve=value):
                document = self.document(self.prepared(self.line(value)))
                self.assertEqual(document["coverage"]["unresolved_reasons"], {})
                self.assertAccepted(document)

    def test_everything_that_can_move_the_probed_path_or_is_not_understood_is_unresolved(self) -> None:
        cases = {
            "RESOLVE_IN_ROOT": "RESOLVE_IN_ROOT", "RESOLVE_CACHED": "RESOLVE_CACHED",
            "安全值加 IN_ROOT": "RESOLVE_BENEATH|RESOLVE_IN_ROOT", "IN_ROOT 在前": "RESOLVE_IN_ROOT|RESOLVE_NO_XDEV",
            "全部四个安全值加 CACHED": "|".join(SAFE_RESOLVE) + "|RESOLVE_CACHED",
            "未知数值": "0x20", "未知数值与安全值": "RESOLVE_BENEATH|0x20", "未知符号": "RESOLVE_FOO", "非零数字": "1",
            "重复的符号": "RESOLVE_BENEATH|RESOLVE_BENEATH", "空值": "", "尾部竖线": "RESOLVE_BENEATH|", "头部竖线": "|RESOLVE_BENEATH",
            "小写": "resolve_beneath", "零与符号并用": "0|RESOLVE_BENEATH", "带空格": "RESOLVE_BENEATH | RESOLVE_NO_XDEV",
            "数字零带前缀": "00", "负数": "-1", "注释混入": "RESOLVE_BENEATH /* x */",
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                document = self.document(self.prepared(self.line(value)))
                self.assertEqual(document["coverage"]["unresolved_reasons"], {"open-flags-not-decoded:openat2": 1}, label)
                self.assertFalse(self.audit(document)["coverage_complete"], label)

    def test_missing_duplicated_or_hidden_resolve_field_is_unresolved(self) -> None:
        r = self.root
        base = f'1 openat2(AT_FDCWD</repo>, "{r}/base/f", @HOW@, 24) = 3<{r}/base/f>'
        cases = {
            "缺字段": "flags=O_RDONLY",
            "两个 resolve 字段": "flags=O_RDONLY, resolve=0, resolve=RESOLVE_IN_ROOT",
            "两个相同的 resolve 字段": "flags=O_RDONLY, resolve=0, resolve=0",
            "安全值在前、IN_ROOT 在后": "flags=O_RDONLY, resolve=RESOLVE_BENEATH, resolve=RESOLVE_IN_ROOT",
            "resolve 被省略号截断": "flags=O_RDONLY, ...",
            "字段名带后缀": "flags=O_RDONLY, resolve_x=0",
            "只有 mode 没有 resolve": "flags=O_RDONLY, mode=0644",
            "resolve 字段在引号里": 'flags=O_RDONLY, note="resolve=0"',
            "resolve 值是结构": "flags=O_RDONLY, resolve={a=0}",
        }
        for label, how in cases.items():
            with self.subTest(label=label):
                line = base.replace("@HOW@", "{" + how + "}")
                document = self.document([self.mkdir_line("base"), self.create_line("base/f"), line])
                self.assertEqual(document["coverage"]["unresolved_reasons"], {"open-flags-not-decoded:openat2": 1}, label)
                self.assertFalse(self.audit(document)["coverage_complete"], label)

    def test_the_pointer_form_and_other_shapes_keep_their_old_verdict(self) -> None:
        cases = ['1 openat2(AT_FDCWD</repo>, "/tmp/x", 0xb1b586a636a8, 24) = 3</tmp/x>',
                 '1 openat2(AT_FDCWD</repo>, "/tmp/x", {flags=O_RDONLY, resolve=0}) = 3</tmp/x>']
        for line in cases:
            self.assertEqual(self.reasons([line]), {"open-flags-not-decoded:openat2": 1}, line)

    def test_the_resolve_in_root_reproducer_from_the_acceptance_report(self) -> None:
        """验收报告的最小复现：jail 里用 RESOLVE_IN_ROOT 探测 ``../…/private/output/x``，失败的调用没有返回描述符可核对。
        内核实际探测 ``<jail>/…``，解析器按字面规范化成私有盘根下的 x——以前被记成合法缺失探测而通过。"""
        jail = f"{WORK}/jail"
        relative = os.path.relpath(f"{self.root}/x", jail)           # ../runtime/<hash>/x：规范化后恰好落在私有盘根下
        escape = f'1 openat2(3<{jail}>, "{relative}", {{flags=O_RDONLY, resolve=RESOLVE_IN_ROOT}}, 24) = -1 ENOENT (No such file or directory)'
        lines = [self.mkdir_line("base"), f'1 openat(AT_FDCWD</repo>, "{jail}", O_RDONLY|O_DIRECTORY) = 3<{jail}>', escape]
        document = self.document(lines)
        self.assertEqual(document["coverage"]["unresolved_reasons"], {"open-flags-not-decoded:openat2": 1})
        self.assertFalse(self.audit(document)["coverage_complete"])
        # 基线对照：把这一道检查关掉，同一份行就是“私有盘里的合法缺失探测”，覆盖完整性只差 jail 目录这条宿主读（本夹具未声明）。
        with mock.patch.object(ra, "_openat2_resolve_ok", return_value=True):
            baseline = self.document(lines)
        self.assertEqual(baseline["coverage"]["unresolved_reasons"], {})
        events = [e for e in baseline["runtime"]["output_events"] if e["name"] == "openat2"]
        self.assertEqual([(e["path"], e["kind"], e["errno"]) for e in events], [(f"{self.root}/x", "missing", "ENOENT")],
                         "基线把宿主上的探测记成了私有盘根下的缺失探测——这正是 M-1")

    def test_a_failed_probe_with_a_harmless_resolve_is_recorded_at_the_literal_path(self) -> None:
        for value in ("0", "RESOLVE_BENEATH", "RESOLVE_NO_SYMLINKS|RESOLVE_NO_XDEV"):
            with self.subTest(resolve=value):
                line = self.line(value, path=f"{self.root}/base/none", result="-1 ENOENT (No such file or directory)")
                document = self.document(self.prepared(line))
                events = [e for e in document["runtime"]["output_events"] if e["name"] == "openat2"]
                self.assertEqual([(e["path"], e["kind"]) for e in events], [(f"{self.root}/base/none", "missing")])
                self.assertEqual(document["coverage"]["unresolved_reasons"], {})

    def test_write_creating_forms_need_the_same_check(self) -> None:
        for value, clean in (("0", True), ("RESOLVE_NO_SYMLINKS", True), ("RESOLVE_IN_ROOT", False)):
            with self.subTest(resolve=value):
                line = self.line(value, flags="O_WRONLY|O_CREAT|O_EXCL", path=f"{self.root}/base/new", extra=", mode=0600")
                document = self.document([self.mkdir_line("base"), line])
                self.assertEqual(document["coverage"]["unresolved"] == 0, clean, document["coverage"]["unresolved_reasons"])

    def test_v3_keeps_the_legacy_criterion_and_the_helper_unit_table(self) -> None:
        line = self.line("RESOLVE_IN_ROOT")
        self.assertEqual(self.reasons([line], "v3"), {}, "v3 沿用“整行没有任何标志”的旧判据，不看 resolve=")
        table = [("{flags=O_RDONLY, resolve=0}", True), ("{flags=O_RDONLY, resolve=RESOLVE_BENEATH|RESOLVE_NO_XDEV}", True),
                 ("{flags=O_RDONLY, resolve=RESOLVE_IN_ROOT}", False), ("{flags=O_RDONLY}", False), ("0x1", False),
                 ("{flags=O_RDONLY, resolve=0, resolve=0}", False), ("{flags=O_RDONLY, resolve=RESOLVE_CACHED}", False),
                 ("{flags=O_RDONLY, resolve=0, ...}", False), ("{flags=O_RDONLY, resolve=RESOLVE_BENEATH, ...}", False)]
        for how, expected in table:
            with self.subTest(how=how):
                self.assertEqual(ra._openat2_resolve_ok(["AT_FDCWD</repo>", '"x"', how, "24"]), expected)
        self.assertFalse(ra._openat2_resolve_ok(["AT_FDCWD</repo>", '"x"', "{flags=O_RDONLY, resolve=0}"]), "参数个数不对")


class EmptyPathWriteTests(CorpusFixture):
    """``utimensat``／``fchownat``／``fchmodat2`` 带空路径与 AT_EMPTY_PATH：对 dirfd 标注路径的写。"""

    CALLS = {
        "utimensat": '1 utimensat({fd}, "", [{{tv_sec=1, tv_nsec=0}}, {{tv_sec=1, tv_nsec=0}}], {flags}) = 0',
        "fchownat": '1 fchownat({fd}, "", 0, 0, {flags}) = 0',
        "fchmodat2": '1 fchmodat2({fd}, "", 0600, {flags}) = 0',
    }

    def call(self, name: str, fd: str, flags: str = "AT_EMPTY_PATH") -> str:
        return self.CALLS[name].format(fd=fd, flags=flags)

    def prepared(self, line: str) -> list[str]:
        return [self.mkdir_line("d"), self.create_line("d/f"), line]

    def test_registered_as_a_write_to_the_descriptor_path_and_attributed_like_futimens(self) -> None:
        for name in self.CALLS:
            with self.subTest(name=name):
                document = self.document(self.prepared(self.call(name, f"5<{self.root}/d/f>")))
                self.assertEqual(document["coverage"]["unresolved_reasons"], {})
                last = document["runtime"]["output_events"][-1]
                self.assertEqual((last["name"], last["path"], last["kind"], last["succeeded"], last["role"]),
                                 (name, f"{self.root}/d/f", "write", True, None))
                self.assertIn("AT_EMPTY_PATH", last["flags"])
                self.assertAccepted(document)

    def test_cwd_with_an_empty_path_acts_on_the_working_directory(self) -> None:
        document = self.document(self.prepared(self.call("utimensat", f"AT_FDCWD<{self.root}/d>")))
        self.assertEqual(document["coverage"]["unresolved_reasons"], {})
        self.assertEqual(document["runtime"]["output_events"][-1]["path"], f"{self.root}/d")
        self.assertAccepted(document)

    def test_an_object_the_run_did_not_create_is_refused_by_the_tracker(self) -> None:
        document = self.document([self.mkdir_line("d"), self.call("utimensat", f"5<{self.root}/d/never>")])
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertTrue(any("未建立合同的输出操作" in text for text in document["runtime"]["outputs"]["problems"]))
        self.assertRejected(document)

    def test_a_host_descriptor_becomes_a_write_finding_the_audit_stops(self) -> None:
        for name in self.CALLS:
            with self.subTest(name=name):
                document = self.document([self.mkdir_line("d"), self.call(name, "5</etc/hosts>")])
                self.assertEqual(document["coverage"]["unresolved"], 0)
                self.assertEqual(dict((item[0], item[1]) for item in document["accesses"])["/etc/hosts"], ["write"])
                report = self.audit(document)
                self.assertFalse(report["coverage_complete"])
                self.assertIn(("/etc/hosts", ["write"]), [(item["path"], item["kinds"]) for item in report["undeclared"]])

    def test_a_descriptor_that_is_not_a_path_is_unresolved(self) -> None:
        for annotation in ("5<pipe:[123]>", "5<socket:[7]>", "5<anon_inode:[eventfd]>"):
            with self.subTest(annotation=annotation):
                document = self.document([self.mkdir_line("d"), self.call("utimensat", annotation)])
                self.assertEqual(document["coverage"]["unresolved_reasons"], {"empty-path-fd-not-a-path:utimensat": 1})
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_without_at_empty_path_the_baseline_is_unchanged(self) -> None:
        """空路径而没有 AT_EMPTY_PATH：内核恒返回 ENOENT，不碰任何对象，保持基线（不登记、不判未解析）。"""
        for flags in ("0", "AT_SYMLINK_NOFOLLOW"):
            with self.subTest(flags=flags):
                line = f'1 utimensat(5<{self.root}/d/f>, "", NULL, {flags}) = -1 ENOENT (No such file or directory)'
                parser = self.parse(self.prepared(line))
                self.assertEqual(parser.unresolved_reasons, {})
                self.assertEqual(parser.accesses.get(f"{self.root}/d/f"), {"write"}, "只有创建 d/f 的那次写，空路径调用没有新增登记")
                self.assertEqual([e["name"] for e in parser.output_events if e["name"] == "utimensat"], [])

    def test_other_empty_path_calls_are_untouched(self) -> None:
        """只处理三个会改 fd 对象元数据的调用；newfstatat／execveat／linkat 的空路径沿用旧规则（打开那一下已经记过）。"""
        lines = [f'1 newfstatat(5<{self.root}/d/f>, "", {{st_mode=S_IFREG|0644}}, AT_EMPTY_PATH) = 0',
                 f'1 execveat(5<{self.root}/d/f>, "", ["x"], 0x1, AT_EMPTY_PATH) = 0']
        parser = self.parse(self.prepared(lines[0]) + lines[1:])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(sorted(e["name"] for e in parser.output_events if e["name"] in {"newfstatat", "execveat"}), [])

    def test_v3_is_unchanged(self) -> None:
        line = self.call("utimensat", f"5<{self.root}/d/f>")
        parser = self.parse(self.prepared(line), "v3")
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual([e for e in parser.output_events if e["name"] == "utimensat"], [], "v3 不登记空路径的写（基线）")
        plain = ra.TraceParser(strict=True)
        plain.feed(f'1 utimensat(5</x/f>, "", NULL, AT_EMPTY_PATH) = 0')
        self.assertEqual((plain.unresolved_reasons, plain.accesses), ({}, {}))

    def test_a_failed_call_is_still_registered_conservatively(self) -> None:
        line = f'1 utimensat(5<{self.root}/d/f>, "", NULL, AT_EMPTY_PATH) = -1 EPERM (Operation not permitted)'
        document = self.document(self.prepared(line))
        last = document["runtime"]["output_events"][-1]
        self.assertEqual((last["name"], last["succeeded"], last["errno"]), ("utimensat", False, "EPERM"))
        self.assertRejected(document)       # 写类调用的 EPERM 不在“合法失败”表里


class V4LimitAgreementCommentTests(unittest.TestCase):
    """注释里写的“两端相等由测试守住”必须有对应测试：h1 守 v3 上限与 v4 上限（本类只核对注释点名的测试存在）。"""

    def test_the_comments_name_tests_that_exist(self) -> None:
        from pathlib import Path
        base = Path(ra.__file__).parent
        tests = Path(__file__).parent
        h1 = (tests / "test_ci_audit_hardening_h1.py").read_text(encoding="utf-8")
        self.assertIn("test_parser_and_replay_v4_limits_are_equal", h1)
        for module in (ra, runtime):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIn("test_parser_and_replay_v4_limits_are_equal", source)
            self.assertIn("test_ci_read_audit_wp_r2", source)
        self.assertTrue((base / "read_audit.py").exists())


if __name__ == "__main__":
    unittest.main()
