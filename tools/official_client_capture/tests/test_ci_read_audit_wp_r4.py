"""审计解析器加固第四轮（WP-R4／R6）：结构性读取／写入的归属语义，只在 v4 轨迹合同下放宽，危险形态仍失败关闭。

背景：第二～五轮候选解析器在真实 ARM64 全集收割（harvest5）里剩下几类“结构性”条目，不是笔误或遗漏，而是要先定语义的问题。
证据：调度者证据目录 ``claude-control-plane/20261015/step10/`` 的 ``census-harvest5-partial.txt``、``harvest5-samples/*.jsonl`` 与
``raw-corpus/raw-diag-*.gz``。本模块里的 ``RAW_*`` 常量逐字取自这些样本（工作目录前缀写成 ``WORK``）。

每类的原则：这个操作在该情形下能暴露什么宿主信息——暴露不了才放宽；拿不准保持失败关闭。每类都有正例与反例，v3 一字不变。

========  ===========================================  ==========================================================
编号      放行                                          仍然拒绝（失败关闭）
========  ===========================================  ==========================================================
R6-1      成功的 O_PATH|O_NOFOLLOW|O_DIRECTORY 打开      缺 O_DIRECTORY；v3；私有盘内已知符号链接名字（归属一侧判问题）；
          （open／openat／openat2）：内核保证最后一个     结尾斜杠形态（symlink-path-form-unsafe）
          分量是目录，不是链接自身
R6-2      宿主上真符号链接的 lstat：返回结构完整解码为    非 0777／uid／gid／size 缺失或非整数／被截断或缩略；
          S_IFLNK|0777，路径无 ``..``／``.``／结尾斜杠，   非 AT_SYMLINK_NOFOLLOW（跟随式，不在此类）；私有盘内不是本单元
          登记为对该路径的 stat 访问，由覆盖检查要求该      创建的链接；空路径（AT_EMPTY_PATH）；v3；该路径没有 v3
          路径有 v3 快照（links 条目绑定 mode／uid／gid／   快照时审计失败（声明缺口由调度者补，不是解析器的活）
          size／链接文本）
R4-1      本轨迹内进程树的 /proc/<pid>/{stat,status,…}    非本轨迹的 pid；/proc/<pid>/{root,cwd,exe,fd,environ,ns,mem,
          白名单叶文件、/proc/self、/proc/thread-self、    net,mountinfo,cgroup…}；写；枚举（list）；原始路径带 ``..``／``.``；
          task/<tid> 同样               前导零 pid；v3
R4-2      失败的带写标志 open（/dev/tty 的 ENXIO）         （无需改动：``replay_runtime`` 的终端探测合同已覆盖，本模块固定
                                                         它仍成立，并固定相邻形态仍被拒绝）
R4-3      /dev/null 的读写                                 （无需改动：``replay_runtime`` 已覆盖 /dev/null 的 read／write／
                                                         stat，本模块固定它，并固定 /dev/zero、/dev/urandom、/dev/tty
                                                         成功打开、/dev/stdin、/proc/self/fd/N→/dev/null 别名仍被拒绝）
========  ===========================================  ==========================================================
"""

from __future__ import annotations

import unittest

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests.test_ci_read_audit_wp_r2 import DECODED_OPENAT2_ARM64, WORK
from tools.official_client_capture.tests.test_ci_read_audit_wp_r3 import R3Fixture

# --- 逐字取自 harvest5-samples 的原始行（test_arm64_driver_wait 的 symlink-metadata-not-bound:openat2 ×3） ---
DRIVER_WAIT_ROOT = f"{WORK}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1"
RAW_OPENAT2_IMPL_LOGS = (
    f'1347269 openat2(3<{DRIVER_WAIT_ROOT}/tmpxrrn675b/remote>, "impl-logs/", {{flags=O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_PATH|O_DIRECTORY, '
    f'resolve=RESOLVE_BENEATH}}, 24 )          = 4<{DRIVER_WAIT_ROOT}/tmpxrrn675b/remote/impl-logs>')
RAW_OPENAT2_CROSS_CHECK = (
    f'1347269 openat2(4<{DRIVER_WAIT_ROOT}/tmpxrrn675b/remote/impl-logs>, "cross-check/", {{flags=O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_PATH|'
    f'O_DIRECTORY, resolve=RESOLVE_BENEATH}}, 24) = 5<{DRIVER_WAIT_ROOT}/tmpxrrn675b/remote/impl-logs/cross-check>')


class R4Fixture(R3Fixture):
    """第三轮夹具的延伸：私有根用语料里 test_arm64_driver_wait 的真实根。本类没有 test_ 方法。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_root(DRIVER_WAIT_ROOT)

    def tree_lines(self) -> list[str]:
        """语料样本需要的目录树：tmpxrrn675b/remote/impl-logs/cross-check，全部由本单元自己创建。"""
        return [self.mkdir_line(relative) for relative in ("tmpxrrn675b", "tmpxrrn675b/remote", "tmpxrrn675b/remote/impl-logs",
                                                           "tmpxrrn675b/remote/impl-logs/cross-check")]

    def reasons_of(self, lines: list[str], contract: str = "v4") -> dict[str, int]:
        return dict(self.parse(lines, contract).unresolved_reasons)


# =====================================================================================================================
# R6-1  O_PATH|O_NOFOLLOW|O_DIRECTORY
# =====================================================================================================================


class OPathDirectoryTests(R4Fixture):
    """成功的 ``O_PATH|O_NOFOLLOW|O_DIRECTORY`` 打开是“打开目录”，不是“链接自身的元数据”。"""

    def test_the_three_harvest_samples_pass_the_whole_chain_under_v4(self) -> None:
        """语料里的三条原始行（外加它们依赖的目录创建）：零未解析、归属零问题、整套严格审计与重放通过。"""
        lines = self.tree_lines() + [RAW_OPENAT2_IMPL_LOGS, RAW_OPENAT2_CROSS_CHECK]
        document = self.assertPasses(lines)
        kinds = {(event["name"], event["path"].rsplit("/", 1)[-1], event["kind"]) for event in document["runtime"]["output_events"]
                 if event["name"] == "openat2"}
        self.assertEqual(kinds, {("openat2", "impl-logs", "dir"), ("openat2", "cross-check", "dir")}, "登记为打开目录")

    def test_the_same_samples_under_v3_keep_the_old_verdict(self) -> None:
        for line in (RAW_OPENAT2_IMPL_LOGS, RAW_OPENAT2_CROSS_CHECK):
            with self.subTest(line=line[:60]):
                self.assertEqual(self.reasons_of(self.tree_lines() + [line], "v3"), {"symlink-metadata-not-bound:openat2": 1})

    def test_arm64_decoded_form_from_the_dispatcher(self) -> None:
        """调度者在 ARM64 上实测的 tar 形态（resolve=0）：现在登记为对目录的打开。"""
        parser = self.parse([DECODED_OPENAT2_ARM64])
        self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {"/tmp/s10o2": {"dir"}}))
        self.assertEqual(self.reasons_of([DECODED_OPENAT2_ARM64], "v3"), {"symlink-metadata-not-bound:openat2": 1})

    def test_open_and_openat_have_the_same_rule(self) -> None:
        cases = {
            "openat": '9 openat(AT_FDCWD</tmp>, "d", O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_PATH|O_DIRECTORY) = 3</tmp/d>',
            "open": '9 open("/tmp/d", O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY) = 3</tmp/d>',
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                parser = self.parse([line])
                self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {"/tmp/d": {"dir"}}))
                self.assertEqual(self.reasons_of([line], "v3"), {"symlink-metadata-not-bound:" + label: 1})

    def test_without_o_directory_it_may_open_the_link_itself_and_stays_unresolved(self) -> None:
        cases = {
            "openat": '9 openat(AT_FDCWD</tmp>, "l", O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_PATH) = 3</tmp/l>',
            "open": '9 open("/tmp/l", O_RDONLY|O_NOFOLLOW|O_PATH) = 3</tmp/l>',
            "openat2": '9 openat2(AT_FDCWD</tmp>, "l", {flags=O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_PATH, resolve=0}, 24) = 3</tmp/l>',
        }
        for label, line in cases.items():
            for contract in ("v3", "v4"):
                with self.subTest(label=label, contract=contract):
                    self.assertEqual(self.reasons_of([line], contract), {"symlink-metadata-not-bound:" + label: 1})

    def test_o_directory_alone_or_o_nofollow_alone_never_was_the_problem(self) -> None:
        for flags in ("O_RDONLY|O_DIRECTORY", "O_RDONLY|O_NOFOLLOW", "O_PATH|O_DIRECTORY", "O_PATH"):
            with self.subTest(flags=flags):
                line = f'9 openat(AT_FDCWD</tmp>, "d", {flags}) = 3</tmp/d>'
                self.assertEqual(self.reasons_of([line], "v4"), {})

    def test_failed_calls_are_not_in_this_class_and_their_classification_is_unchanged(self) -> None:
        """失败的调用从来不进 symlink-metadata-not-bound 的分支；这里固定它们的访问类型没被改动。"""
        cases = {
            "ENOTDIR": ('9 openat(AT_FDCWD</tmp>, "l", O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY) = -1 ENOTDIR (Not a directory)', "missing"),
            "ENOENT": ('9 openat(AT_FDCWD</tmp>, "l", O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY) = -1 ENOENT (No such file or directory)',
                       "missing"),
            "ELOOP": ('9 openat(AT_FDCWD</tmp>, "l", O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY) = -1 ELOOP (Too many levels of symbolic links)',
                      "stat"),
        }
        for label, (line, kind) in cases.items():
            with self.subTest(label=label):
                parser = self.parse([line])
                self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {"/tmp/l": {kind}}))

    def test_undecoded_openat2_structure_still_hides_the_flags(self) -> None:
        """结构是指针（标志不可见）：先是 open-flags-not-decoded，O_DIRECTORY 的放宽不会替它放行。"""
        line = '9 openat2(AT_FDCWD</tmp>, "d/", 0xb1b586a636a8, 24) = 4</tmp/d>'
        self.assertEqual(self.reasons_of([line]), {"open-flags-not-decoded:openat2": 1})

    def test_resolve_in_root_still_hides_the_resolved_path(self) -> None:
        line = ('9 openat2(AT_FDCWD</tmp>, "d/", {flags=O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY, resolve=RESOLVE_IN_ROOT}, 24) = 4</tmp/d>')
        self.assertEqual(self.reasons_of([line]), {"open-flags-not-decoded:openat2": 1})

    def test_a_tracked_symlink_name_is_still_refused_by_the_tracker(self) -> None:
        """私有盘内本单元自己创建的符号链接：解析器侧不再登记，但归属一侧对链接名字的 O_PATH 成功打开一律判问题。
        内核对链接名字上的 O_DIRECTORY|O_NOFOLLOW 返回 ENOTDIR，轨迹却声称成功，说明名字表与真实状态已脱节。"""
        lines = self.base_lines() + [self.symlink_line("d/l"),
                                     f'1 openat(AT_FDCWD</repo>, "{self.p("d/l")}", O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY) = 3<{self.p("d/l")}>']
        document, reasons, problems = self.outcome(lines)
        self.assertEqual(reasons, {})
        self.assertTrue(any("跟随符号链接的访问或没有合同的调用" in text for text in problems), problems)
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_trailing_slash_on_a_tracked_symlink_is_flagged_as_unsafe_path_form(self) -> None:
        """结尾斜杠会迫使内核跟随最后一个链接（O_NOFOLLOW 也一样）：沿用 WP-R3 的路径形态检查登记 symlink-path-form-unsafe。"""
        lines = self.base_lines() + [self.symlink_line("d/l", "/etc"),
                                     f'1 openat(AT_FDCWD</repo>, "{self.p("d/l")}/", O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY) = 3</etc>']
        self.assertFailsClosed(lines, where="parser", fragment="symlink-path-form-unsafe")

    def test_an_untracked_name_in_the_private_root_needs_the_tracker_to_have_created_it(self) -> None:
        """目录必须是本单元创建的：打开一个没证明过的私有盘内目录仍判“首次读取前未证明由本轮生成”。"""
        lines = self.base_lines() + [
            f'1 openat(AT_FDCWD</repo>, "{self.p("nobody-made-this")}", O_RDONLY|O_NOFOLLOW|O_PATH|O_DIRECTORY) = 3<{self.p("nobody-made-this")}>']
        self.assertFailsClosed(lines, where="tracker", fragment="首次读取前未证明由本轮生成")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
