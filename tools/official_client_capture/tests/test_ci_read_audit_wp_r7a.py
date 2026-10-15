"""审计解析器加固第七轮 a（WP-R7a）：落实第三、四轮两份独立验收报告对 ``tools/ci/read_audit.py`` 的全部发现。

证据：调度者证据目录 ``claude-control-plane/20261015/`` 下的《独立验收-解析器加固第三轮.md》《独立验收-解析器加固第四轮.md》。
全部放宽／收紧只在 v4 轨迹合同生效；v3、control 四链与 ``strace_argv`` 的 v3 命令行逐字节不变（三份真实语料的 v3 完整输出摘要
在交付报告里前后对比）。

========  ====================================================================  =====================================================
编号      发现                                                                  处置
========  ====================================================================  =====================================================
S-1       ``_path_form_hazard`` 只在 ``..`` 之前查“当前位置是不是链接”，          ``..`` 之后的落点是链接名字也判风险
          ``link/ssh/..`` 沿链接下降后文本回到链接名字，失败的 mkdir／rename／
          open(O_EXCL) 把宿主目录里 ``ssh`` 的存在性暴露出来
S-2       目录被改名后各 pid 的文本工作目录不更新，plain 系统调用走相对路径       成功的 rename 之后改写落在被改名路径之下的
          经链接时看不见链接                                                     各 pid 的工作目录（含 exchange 互换、覆盖不改写）
对拍      作者的差分对拍只在事件层、模拟器里没有宿主目录                         含宿主目录的端到端对拍（``HostWorld``）及“改宽
                                                                                会变红”的证明；readlink 返回文本、lstat 的
                                                                                st_size 与记录的链接文本交叉核对
CLONE_NEW* v4 的 unobservable-child 只认 CLONE_UNTRACED|CLONE_NEWNS，           任何 ``CLONE_NEW*`` 都判；之后 pid 空间不再可信
          CLONE_NEWPID 子进程里 clone 返回的小编号被当成本轨迹 pid
I-2       4 个非等价存活变异（/proc 自身、/proc/self/{root,cwd,exe,fd}、           补用例
          /proc/<p>/<非 task>/<已见 pid>/<叶>、task/<tid>/<叶> 的写与执行）
一般      超长数字串 ``int()`` 抛 ValueError；``/proc/self`` lstat 无真实样本；    加长度上限；删除 ``/proc/self`` lstat 豁免；
          P03／X05／X08／M15 存活变异                                            补用例
========  ====================================================================  =====================================================
"""

from __future__ import annotations

import os
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests.test_ci_read_audit_v4_tracker import Builder
from tools.official_client_capture.tests.test_ci_read_audit_wp_r3 import R3Fixture, STAT_LNK, STAT_REG


def _hazard_before_wp_r7a(tracker: Any, base: str | None, raw: str) -> bool:
    """WP-R7a 之前的 ``_path_form_hazard``（第三轮的原文，只在 ``..`` 之前查当前位置）。只用于证明 S-1 的复现行真的是绕过。"""
    if not raw or not tracker.has_symlinks():
        return False
    current = "/" if raw.startswith("/") else base
    if current is None:
        return False
    parts = raw.split("/")
    for component in parts:
        if component in ("", "."):
            continue
        if component == "..":
            if tracker.is_symlink(current):
                return True
            current = os.path.dirname(current) or "/"
        else:
            current = os.path.join(current, component)
    return (raw.endswith("/") or parts[-1] == ".") and tracker.is_symlink(current)


class R7aFixture(R3Fixture):
    """第三轮夹具的延伸：``self.lines_with_host_link()`` 建好私有盘内的 ``d`` 与指向宿主 ``/etc`` 的 ``ln``。本类没有 test_ 方法。"""

    def lines_with_host_link(self) -> list[str]:
        return self.base_lines() + [self.symlink_line("ln", "/etc")]

    def at(self, call: str, relative: str, tail: str) -> str:
        """``*at`` 调用：AT_FDCWD 标注 /repo；``relative`` 是相对私有根的原始路径写法（可含 ``..``）。"""
        return f'1 {call}(AT_FDCWD</repo>, "{self.root}/{relative}", {tail}'


# =====================================================================================================================
# S-1：沿链接下降后用 .. 回到链接名字
# =====================================================================================================================


class DotDotBackOntoTheLinkTests(R7aFixture):
    """``R/ln/ssh/..`` 在内核里是宿主 ``/etc`` 里的 ``ssh`` 再取父目录；文本规范化却得到 ``R/ln``（链接名字本身）。

    失败的 mkdir／symlink／mknod／open(O_CREAT|O_EXCL)（EEXIST）与 rename（ENOENT／ENOTDIR）落在链接名字上，跟踪器本来当作“不跟随的
    合法探测”放行；但内核实际查了宿主目录，宿主上 ``ssh`` 是否存在被暴露。判据：``..`` 的落点是符号链接名字。
    """

    HAZARD = "symlink-path-form-unsafe"

    def creation_probes(self, spelled: str) -> dict[str, str]:
        """同一个路径写法 ``spelled`` 上的各种“创建被拒绝（EEXIST）”调用。"""
        path = f"{self.root}/{spelled}"
        return {
            "mkdir": f'1 mkdir("{path}", 0777) = -1 EEXIST (File exists)',
            "mkdirat": f'1 mkdirat(AT_FDCWD</repo>, "{path}", 0777) = -1 EEXIST (File exists)',
            "symlink": f'1 symlink("t", "{path}") = -1 EEXIST (File exists)',
            "symlinkat": f'1 symlinkat("t", AT_FDCWD</repo>, "{path}") = -1 EEXIST (File exists)',
            "mknod": f'1 mknod("{path}", S_IFREG|0600, 0) = -1 EEXIST (File exists)',
            "mknodat": f'1 mknodat(AT_FDCWD</repo>, "{path}", S_IFREG|0600, 0) = -1 EEXIST (File exists)',
            "openat O_EXCL": f'1 openat(AT_FDCWD</repo>, "{path}", O_WRONLY|O_CREAT|O_EXCL, 0600) = -1 EEXIST (File exists)',
            "open O_EXCL": f'1 open("{path}", O_WRONLY|O_CREAT|O_EXCL, 0600) = -1 EEXIST (File exists)',
        }

    def test_the_minimal_reproduction_from_the_acceptance_report_fails_closed(self) -> None:
        lines = self.lines_with_host_link() + [f'1 mkdir("{self.root}/ln/ssh/..", 0777) = -1 EEXIST (File exists)']
        self.assertFailsClosed(lines, where="parser", fragment=self.HAZARD)

    def test_every_creation_probe_on_dotdot_back_onto_the_link_fails_closed(self) -> None:
        for spelled in ("ln/ssh/..", "ln/a/b/../..", "ln/ssh/../.", "ln/ssh/./..", "d/../ln/x/..", "ln/ssh/y/../.."):
            for label, line in self.creation_probes(spelled).items():
                with self.subTest(spelled=spelled, call=label):
                    self.assertFailsClosed(self.lines_with_host_link() + [line], where="parser", fragment=self.HAZARD)

    def test_rename_failures_with_either_end_spelled_that_way_fail_closed(self) -> None:
        r = self.root
        for error in ("ENOENT", "ENOTDIR"):
            spelled = f"{r}/ln/ssh/.."
            cases = {
                "rename 来源": f'1 rename("{spelled}", "{r}/d/y") = -1 {error} (x)',
                "rename 目的": f'1 rename("{r}/d", "{spelled}") = -1 {error} (x)',
                "renameat2 来源": f'1 renameat2(AT_FDCWD</repo>, "{spelled}", AT_FDCWD</repo>, "{r}/d/y", 0) = -1 {error} (x)',
                "renameat2 目的": f'1 renameat2(AT_FDCWD</repo>, "{r}/d", AT_FDCWD</repo>, "{spelled}", 0) = -1 {error} (x)',
                "renameat 目的": f'1 renameat(AT_FDCWD</repo>, "{r}/d", AT_FDCWD</repo>, "{spelled}") = -1 {error} (x)',
            }
            for label, line in cases.items():
                with self.subTest(error=error, call=label):
                    self.assertFailsClosed(self.lines_with_host_link() + [line], where="parser", fragment=self.HAZARD)

    def test_relative_spellings_through_the_working_directory_and_a_directory_descriptor_fail_closed(self) -> None:
        r = self.root
        relative = {
            "plain 相对写法（工作目录 d）": [f'1 chdir("{r}/d") = 0', '1 mkdir("../ln/sub/..", 0777) = -1 EEXIST (File exists)'],
            "dirfd 相对写法": [f'1 mkdirat(5<{r}/d>, "../ln/sub/..", 0777) = -1 EEXIST (File exists)'],
            "dirfd 写法里的 ln/x/..": [f'1 mkdirat(5<{r}>, "ln/x/..", 0777) = -1 EEXIST (File exists)'],
            "工作目录里的 ln/x/..": [f'1 chdir("{r}") = 0', '1 mkdir("ln/x/..", 0777) = -1 EEXIST (File exists)'],
        }
        for label, tail in relative.items():
            with self.subTest(label=label):
                self.assertFailsClosed(self.lines_with_host_link() + tail, where="parser", fragment=self.HAZARD)

    def test_the_bypass_is_real_under_the_pre_wp_r7a_rule(self) -> None:
        """旧判据下这些行整套审计通过（所以不是“本来就会被别处拦下”）；新判据下全部失败关闭。"""
        probes = [self.creation_probes("ln/ssh/..")["mkdir"], self.creation_probes("ln/ssh/..")["openat O_EXCL"],
                  f'1 rename("{self.root}/nobody", "{self.root}/ln/ssh/..") = -1 ENOENT (x)']   # 来源未知：旧判据下是“合法缺失探测”
        for line in probes:
            lines = self.lines_with_host_link() + [line]
            with self.subTest(line=line[:70]):
                with mock.patch.object(ra, "_path_form_hazard", _hazard_before_wp_r7a):
                    document, reasons, problems = self.outcome(lines)
                    self.assertTrue(self.audit(document)["coverage_complete"], (reasons, problems))
                self.assertFailsClosed(lines, where="parser", fragment=self.HAZARD)

    def test_neighbouring_legitimate_spellings_still_pass(self) -> None:
        """不经过链接的 ``a/b/..``、链接只作为末分量、链接在别处——全部照旧放行。"""
        r = self.root
        setup = self.lines_with_host_link() + [self.mkdir_line("d/a"), self.mkdir_line("d/a/b")]
        legitimate = {
            "a/b/.. 的 EEXIST": f'1 mkdir("{r}/d/a/b/..", 0777) = -1 EEXIST (File exists)',
            "d/a/../a/b": f'1 mkdir("{r}/d/a/../a/b", 0777) = -1 EEXIST (File exists)',
            "链接作为末分量的 EEXIST（第三轮已放行）": f'1 mkdir("{r}/ln", 0777) = -1 EEXIST (File exists)',
            "链接之前的 ..": f'1 mkdir("{r}/d/../d2", 0777) = 0',
            "x/.. 与链接同级": f'1 mkdir("{r}/d/a/../a2", 0777) = 0',
            "rename 不经链接": f'1 rename("{r}/d/a/b", "{r}/d/a/../b2") = 0',
        }
        for label, line in legitimate.items():
            with self.subTest(label=label):
                document, reasons, problems = self.outcome(setup + [line])
                self.assertNotIn(self.HAZARD, reasons, label)
                self.assertEqual(reasons, {}, label)
                self.assertEqual(problems, [], label)

    def test_hazard_helper_table_with_the_new_spellings(self) -> None:
        tracker = runtime.OutputTrackerV4(self.contract)
        for event in (Builder().mkdir(self.p("d"))[0], Builder().symlink(self.p("d/l"), "/etc")[0]):
            tracker.observe_event(event)
        base = self.p("d")
        cases = [("l/x/..", True), ("l/x/y/../..", True), ("l/x/../.", True), ("l/x/./..", True), ("l/x/y/..", False),
                 ("l/x/y/../z", False), ("./l/x/..", True), ("../d/l/x/..", True), (self.p("d/l") + "/x/..", True),
                 (self.p("d/l") + "/x/y/../..", True), ("x/y/..", False), ("x/y/../..", False), ("x/../y/..", False),
                 ("l", False), ("l/x", False)]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ra._path_form_hazard(tracker, base, raw), expected, raw)
        self.assertFalse(_hazard_before_wp_r7a(tracker, base, "l/x/.."), "旧判据看不见这种写法（这正是 S-1）")
        self.assertFalse(_hazard_before_wp_r7a(tracker, base, "l/x/y/../.."))

    def test_v3_is_not_affected(self) -> None:
        """v3 没有符号链接对象，路径形态检查本来就不运行；同一行在 v3 下的未解析原因与改动前相同（创建符号链接本身就被拒）。"""
        lines = self.lines_with_host_link() + [self.creation_probes("ln/ssh/..")["mkdir"]]
        document = self.document(lines, contract="v3")
        self.assertNotIn(self.HAZARD, document["coverage"]["unresolved_reasons"])
        self.assertTrue(any("链接或移动操作尚无输出归属证明" in text for text in document["runtime"]["outputs"]["problems"]))


# =====================================================================================================================
# S-2：目录被改名后，文本工作目录跟着走
# =====================================================================================================================


class WorkingDirectoryFollowsRenameTests(R7aFixture):
    """内核里工作目录是目录对象，跟着目录改名；解析器的文本工作目录原来只在 ``AT_FDCWD<…>`` 标注、``fchdir``、成功的 ``chdir`` 时更新。

    场景（验收 S-2）：``chdir`` 进 ``x``，在 ``x`` 里建链接 ``ln``→``/etc``，把 ``x`` 改名 ``y``，再用 plain 系统调用走相对路径经链接并失败
    （ENOENT）。旧行为：解析器还原出已经不存在的 ``…/x/ln/zzz``，看不见链接，通过。
    """

    def x_then_renamed(self, destination: str = "y") -> list[str]:
        r = self.root
        return [f'1 mkdir("{r}/x", 0777) = 0', f'1 chdir("{r}/x") = 0', f'1 symlink("/etc", "{r}/x/ln") = 0',
                f'1 rename("{r}/x", "{r}/{destination}") = 0']

    def test_the_acceptance_reproduction_fails_closed(self) -> None:
        lines = self.x_then_renamed() + ['1 execve("ln/zzz", ["zzz"], 0x7ffd /* 1 var */) = -1 ENOENT (No such file or directory)']
        self.assertFailsClosed(lines, where="tracker", fragment="中间分量是符号链接")

    def test_every_plain_call_through_the_relative_link_fails_closed_after_the_rename(self) -> None:
        probes = {
            "execve": '1 execve("ln/zzz", ["zzz"], 0x7ffd /* 1 var */) = -1 ENOENT (No such file or directory)',
            "chdir ENOENT": '1 chdir("ln/ssh") = -1 ENOENT (No such file or directory)',
            "chdir 成功": '1 chdir("ln/ssh") = 0',
            "truncate": '1 truncate("ln/passwd", 0) = -1 EACCES (Permission denied)',
            "statfs": '1 statfs("ln/ssh", 0x7ffd) = -1 ENOENT (No such file or directory)',
            "getxattr": '1 getxattr("ln/passwd", "user.x", 0x7ffd, 8) = -1 ENODATA (No data available)',
            "inotify_add_watch": '1 inotify_add_watch(4<anon_inode:inotify>, "ln/ssh", IN_ALL_EVENTS) = -1 ENOENT (No such file or directory)',
        }
        for label, line in probes.items():
            with self.subTest(label=label):
                self.assertFailsClosed(self.x_then_renamed() + [line], where="tracker", fragment="中间分量是符号链接")

    def test_the_same_probe_passed_before_the_working_directory_followed_the_rename(self) -> None:
        """对照：把改写关掉（旧行为），同一条轨迹整套审计通过——所以上面的失败关闭确实来自本轮的改写。"""
        lines = self.x_then_renamed() + ['1 execve("ln/zzz", ["zzz"], 0x7ffd /* 1 var */) = -1 ENOENT (No such file or directory)']
        with mock.patch.object(ra.TraceParser, "_relocate_working_directories", lambda self_, name, ends: None):
            document, reasons, problems = self.outcome(lines)
            self.assertTrue(self.audit(document)["coverage_complete"], (reasons, problems))

    def test_a_legitimate_relative_call_in_the_renamed_directory_passes(self) -> None:
        """旧行为在这里反而是假失败：相对路径被还原到已不存在的 x 之下，新建目录的父目录“未证明”。"""
        r = self.root
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 chdir("{r}/x") = 0', f'1 rename("{r}/x", "{r}/y") = 0',
                 '1 mkdir("sub", 0777) = 0', '1 mkdir("sub/inner", 0777) = 0', '1 truncate("nobody", 0) = -1 ENOENT (x)']
        document = self.assertPasses(lines)
        paths = {event["path"] for event in document["runtime"]["output_events"] if event["name"] == "mkdir"}
        self.assertEqual(paths, {f"{r}/x", f"{r}/y/sub", f"{r}/y/sub/inner"})

    def test_the_working_directory_text_itself(self) -> None:
        r = self.root
        parser = self.parse(self.x_then_renamed())
        self.assertEqual(parser.cwd, {"1": f"{r}/y"})
        self.assertEqual(parser.unresolved_reasons, {})

    def test_a_working_directory_below_the_renamed_directory_moves_with_it(self) -> None:
        r = self.root
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 mkdir("{r}/x/a", 0777) = 0', f'1 mkdir("{r}/x/a/b", 0777) = 0',
                 f'1 chdir("{r}/x/a/b") = 0', f'1 rename("{r}/x/a", "{r}/z") = 0']
        parser = self.parse(lines)
        self.assertEqual(parser.cwd["1"], f"{r}/z/b")
        # 与“前缀相同但不是子目录”的名字区分开：x/ab 不是 x/a 的子目录。
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 mkdir("{r}/x/a", 0777) = 0', f'1 mkdir("{r}/x/ab", 0777) = 0',
                 f'1 chdir("{r}/x/ab") = 0', f'1 rename("{r}/x/a", "{r}/z") = 0']
        self.assertEqual(self.parse(lines).cwd["1"], f"{r}/x/ab")

    def test_every_process_whose_working_directory_is_below_moves_and_the_others_do_not(self) -> None:
        r = self.root
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 mkdir("{r}/x/sub", 0777) = 0', f'1 mkdir("{r}/other", 0777) = 0',
                 f'1 chdir("{r}/x") = 0', f'2 chdir("{r}/x/sub") = 0', f'3 chdir("{r}/other") = 0', f'4 chdir("{r}") = 0',
                 f'3 rename("{r}/x", "{r}/y") = 0']
        parser = self.parse(lines)
        self.assertEqual(parser.cwd, {"1": f"{r}/y", "2": f"{r}/y/sub", "3": f"{r}/other", "4": r})

    def test_a_second_process_probing_through_the_link_after_the_rename_fails_closed(self) -> None:
        r = self.root
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 symlink("/etc", "{r}/x/ln") = 0', f'2 chdir("{r}/x") = 0',
                 f'1 rename("{r}/x", "{r}/y") = 0', '2 execve("ln/zzz", ["zzz"], 0x7ffd /* 1 var */) = -1 ENOENT (x)']
        self.assertFailsClosed(lines, where="tracker", fragment="中间分量是符号链接")

    def test_the_working_directory_equal_to_the_renamed_directory_and_a_chain_of_renames(self) -> None:
        r = self.root
        lines = [f'1 mkdir("{r}/a", 0777) = 0', f'1 chdir("{r}/a") = 0', f'1 rename("{r}/a", "{r}/b") = 0',
                 f'1 rename("{r}/b", "{r}/c") = 0', f'1 mkdir("{r}/d", 0777) = 0', f'1 rename("{r}/c", "{r}/d/e") = 0']
        parser = self.parse(lines)
        self.assertEqual(parser.cwd["1"], f"{r}/d/e")
        self.assertEqual(parser.unresolved_reasons, {})

    def test_a_failed_rename_changes_no_working_directory(self) -> None:
        r = self.root
        for error in ("ENOENT", "EEXIST", "ENOTEMPTY", "EXDEV", "EINVAL"):
            with self.subTest(error=error):
                lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 chdir("{r}/x") = 0', f'1 rename("{r}/x", "{r}/y") = -1 {error} (x)']
                self.assertEqual(self.parse(lines).cwd["1"], f"{r}/x")

    def test_rename_over_an_empty_directory_leaves_the_victims_working_directory_text_alone(self) -> None:
        """被覆盖的目录里的进程，工作目录成了已删除的目录；文本不改（见 ``_relocate_working_directories`` 的说明）。"""
        r = self.root
        lines = [f'1 mkdir("{r}/src", 0777) = 0', f'1 mkdir("{r}/victim", 0777) = 0', f'2 chdir("{r}/victim") = 0',
                 f'1 chdir("{r}/src") = 0', f'1 rename("{r}/src", "{r}/victim") = 0']
        parser = self.parse(lines)
        self.assertEqual(parser.cwd, {"1": f"{r}/victim", "2": f"{r}/victim"}, "改名者的工作目录跟着走；被覆盖目录里的进程文本不变")
        self.assertEqual(parser.unresolved_reasons, {})

    def test_the_conservative_direction_after_an_overwrite_is_never_a_pass_through_a_link(self) -> None:
        """被覆盖目录里的进程走相对路径，文本路径落在改名过来的子树里：有链接就保守地判问题，绝不会反过来放行。"""
        r = self.root
        lines = [f'1 mkdir("{r}/src", 0777) = 0', f'1 symlink("/etc", "{r}/src/ln") = 0', f'1 mkdir("{r}/victim", 0777) = 0',
                 f'2 chdir("{r}/victim") = 0', f'1 rename("{r}/src", "{r}/victim") = 0',
                 '2 execve("ln/zzz", ["zzz"], 0x7ffd /* 1 var */) = -1 ENOENT (x)']
        self.assertFailsClosed(lines, where="tracker", fragment="中间分量是符号链接")

    def test_the_dirfd_spellings_renameat_and_renameat2_follow_too(self) -> None:
        r = self.root
        base = [f'1 mkdir("{r}/x", 0777) = 0', f'1 chdir("{r}/x") = 0']
        cases = {
            "renameat": f'1 renameat(AT_FDCWD<{r}/x>, "{r}/x", AT_FDCWD<{r}/x>, "{r}/y") = 0',
            "renameat2 标志 0": f'1 renameat2(AT_FDCWD<{r}/x>, "{r}/x", AT_FDCWD<{r}/x>, "{r}/y", 0) = 0',
            "renameat2 NOREPLACE": f'1 renameat2(AT_FDCWD<{r}/x>, "{r}/x", AT_FDCWD<{r}/x>, "{r}/y", RENAME_NOREPLACE) = 0',
            "renameat2 WHITEOUT": f'1 renameat2(AT_FDCWD<{r}/x>, "{r}/x", AT_FDCWD<{r}/x>, "{r}/y", RENAME_WHITEOUT) = 0',
            "相对来源（dirfd 是根）": f'1 renameat(5<{r}>, "x", 5<{r}>, "y") = 0',
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                parser = self.parse(base + [line])
                # 注意：前面的 AT_FDCWD<…/x> 标注会把调用时刻的工作目录刷新成 x；调用之后工作目录应是 y。
                self.assertEqual(parser.cwd["1"], f"{r}/y", label)

    def test_exchange_swaps_both_sides(self) -> None:
        r = self.root
        lines = [f'1 mkdir("{r}/a", 0777) = 0', f'1 mkdir("{r}/b", 0777) = 0', f'1 mkdir("{r}/b/in", 0777) = 0',
                 f'1 chdir("{r}/a") = 0', f'2 chdir("{r}/b/in") = 0', f'3 chdir("{r}") = 0',
                 f'3 renameat2(AT_FDCWD<{r}>, "{r}/a", AT_FDCWD<{r}>, "{r}/b", RENAME_EXCHANGE) = 0']
        parser = self.parse(lines)
        self.assertEqual(parser.cwd, {"1": f"{r}/b", "2": f"{r}/a/in", "3": r})
        # 跟踪器仍拒绝 exchange（这里只固定文本工作目录与内核一致）。
        self.assertTrue(any("RENAME_EXCHANGE" in problem for problem in parser.output_tracker.problems))

    def test_a_clone_in_flight_inherits_the_moved_directory(self) -> None:
        """父进程的 clone 还没返回（unfinished）时，别的进程改名了它的工作目录；子进程继承的是改名后的目录。"""
        r = self.root
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 chdir("{r}/x") = 0', '1 clone(child_stack=NULL, flags=SIGCHLD) <unfinished ...>',
                 f'2 chdir("{r}") = 0', f'2 rename("{r}/x", "{r}/y") = 0', '1 <... clone resumed>) = 7']
        parser = self.parse(lines)
        self.assertEqual((parser.cwd["1"], parser.cwd["7"]), (f"{r}/y", f"{r}/y"))

    def test_a_link_created_before_the_rename_in_the_children_cwd_is_still_seen(self) -> None:
        r = self.root
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 symlink("/etc", "{r}/x/ln") = 0', f'1 chdir("{r}/x") = 0',
                 '1 clone(child_stack=NULL, flags=SIGCHLD) = 8', f'1 rename("{r}/x", "{r}/y") = 0',
                 '8 execve("ln/zzz", ["zzz"], 0x7ffd /* 1 var */) = -1 ENOENT (x)']
        self.assertFailsClosed(lines, where="tracker", fragment="中间分量是符号链接")

    def test_at_calls_never_needed_the_rewrite_and_still_see_the_link(self) -> None:
        r = self.root
        lines = self.x_then_renamed() + [f'1 openat(AT_FDCWD<{r}/y>, "ln/zzz", O_RDONLY) = -1 ENOENT (x)']
        self.assertFailsClosed(lines, where="tracker", fragment="中间分量是符号链接")

    def test_v3_does_not_touch_the_working_directories(self) -> None:
        r = self.root
        lines = [f'1 mkdir("{r}/x", 0777) = 0', f'1 chdir("{r}/x") = 0', f'1 rename("{r}/x", "{r}/y") = 0']
        for label, parser in (("v3 带隔离合同", ra.TraceParser(strict=True, runtime_context=self.context)),
                              ("默认解析器（声明级采样、control 四链）", ra.TraceParser(strict=True))):
            with self.subTest(label=label):
                for line in lines:
                    parser.feed(line)
                self.assertEqual(parser.cwd["1"], f"{r}/x", "v3 一字不变：工作目录不随 rename 改写")


# =====================================================================================================================
# I-1：CLONE_NEW*（尤其 CLONE_NEWPID）击穿“已见 pid 集合”
# =====================================================================================================================


class NamespaceClonesTests(R7aFixture):
    """``CLONE_NEWPID`` 的子进程活在新 PID 命名空间里：它再 ``clone`` 返回的是命名空间内的小编号（2、3…），strace 的行首 pid 却是宿主编号。
    解析器把这个小编号当作“本轨迹的 pid”，之后宿主 ``/proc/2/status`` 被当作单元内部状态放行（验收 I-1）。

    处置：v4 的 ``unobservable-child`` 判据从 ``CLONE_(UNTRACED|NEWNS)`` 扩到任何 ``CLONE_NEW*``（control 四链的 sidecar 早已这样判）；
    此外轨迹里出现过任何 ``CLONE_NEW*``、``unshare``、``setns`` 之后，pid 空间不再可信，/proc 内部状态放宽一律不适用。
    """

    NEWPID_REPRODUCTION = ['100 clone(child_stack=NULL, flags=CLONE_NEWPID|SIGCHLD) = 400',
                           '400 clone(child_stack=NULL, flags=SIGCHLD) = 2',
                           '100 openat(AT_FDCWD</repo>, "/proc/2/status", O_RDONLY|O_CLOEXEC) = 3</proc/2/status>']

    def test_the_acceptance_reproduction_is_unresolved_and_the_proc_read_stays_a_host_access(self) -> None:
        lines = self.pad(self.NEWPID_REPRODUCTION)
        parser = self.parse(lines)
        self.assertEqual(parser.unresolved_reasons, {"unobservable-child:clone": 1})
        self.assertEqual(parser.accesses["/proc/2/status"], {"read"}, "第二道防线：pid 空间不可信之后，/proc/2/status 仍是宿主访问")
        self.assertEqual(parser.unit_proc_accesses, 0)
        self.assertFailsClosed(lines, where="parser", fragment="unobservable-child")

    def test_the_baseline_before_this_round_accepted_it(self) -> None:
        """对照：用旧判据（只认 UNTRACED／NEWNS）并关掉第二道防线，复现行整套审计通过——证明验收者的绕过是真的。"""
        lines = self.pad(self.NEWPID_REPRODUCTION)
        with mock.patch.object(ra, "_CLONE_UNOBSERVABLE_V4", ra._CLONE_UNOBSERVABLE_V3), \
                mock.patch.object(ra, "_CLONE_NEW_ANY", ra.re.compile(r"(?!)")):
            document, reasons, problems = self.outcome(lines)
            self.assertEqual((reasons, problems), ({}, []))
            self.assertTrue(self.audit(document)["coverage_complete"])

    def test_the_second_line_of_defence_alone_keeps_the_host_proc_read_registered(self) -> None:
        """只关第一道（unobservable-child），第二道（pid 空间不可信）仍使 /proc/2/status 登记为宿主访问，从而该路径无法有 v3 快照、审计失败。"""
        lines = self.pad(self.NEWPID_REPRODUCTION)
        with mock.patch.object(ra, "_CLONE_UNOBSERVABLE_V4", ra._CLONE_UNOBSERVABLE_V3):
            document, reasons, _problems = self.outcome(lines)
            self.assertEqual(reasons, {})
            self.assertIn("/proc/2/status", {path for path, _kinds in document["accesses"]})
            self.assertFalse(self.audit(document)["coverage_complete"])

    def test_clone3_with_a_namespace_flag_is_the_same(self) -> None:
        lines = self.pad(['100 clone3({flags=CLONE_NEWPID|CLONE_PARENT_SETTID, exit_signal=SIGCHLD, stack=NULL, stack_size=0}, 88) = 400',
                          '400 clone(child_stack=NULL, flags=SIGCHLD) = 2',
                          '100 openat(AT_FDCWD</repo>, "/proc/2/status", O_RDONLY|O_CLOEXEC) = 3</proc/2/status>'])
        self.assertEqual(self.parse(lines).unresolved_reasons, {"unobservable-child:clone3": 1})
        self.assertFailsClosed(lines, where="parser", fragment="unobservable-child:clone3")

    def test_every_clone_new_flag_is_unobservable_under_v4(self) -> None:
        for flag in ("CLONE_NEWPID", "CLONE_NEWNS", "CLONE_NEWUSER", "CLONE_NEWNET", "CLONE_NEWIPC", "CLONE_NEWUTS", "CLONE_NEWCGROUP",
                     "CLONE_NEWTIME"):
            for call in ("clone", "clone3", "fork", "vfork"):
                body = f"{{flags={flag}|SIGCHLD}}, 88" if call == "clone3" else f"child_stack=NULL, flags={flag}|SIGCHLD"
                line = f"100 {call}({body}) = 400"
                with self.subTest(flag=flag, call=call):
                    self.assertEqual(self.reasons([line]), {f"unobservable-child:{call}": 1})

    def test_unfinished_and_resumed_clones_are_judged_on_the_joined_call(self) -> None:
        lines = ['100 clone(child_stack=NULL, flags=CLONE_NEWPID|SIGCHLD <unfinished ...>', '100 <... clone resumed>) = 400']
        self.assertEqual(self.reasons(lines), {"unobservable-child:clone": 1})

    def test_ordinary_clones_stay_observable_and_their_children_keep_the_proc_allowance(self) -> None:
        """正例：线程、vfork、普通 fork 的标志里没有 CLONE_NEW*，行为与改动前一致；子进程仍属于本轨迹。"""
        lines = ['100 clone(child_stack=0x7f00, flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM|CLONE_SETTLS|'
                 'CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID, parent_tid=[401]) = 401',
                 '100 clone(child_stack=NULL, flags=CLONE_VM|CLONE_VFORK|SIGCHLD) = 402',
                 '100 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, child_tidptr=0x7f) = 403',
                 '100 vfork() = 404', '100 fork() = 405',
                 '100 openat(AT_FDCWD</repo>, "/proc/401/status", O_RDONLY|O_CLOEXEC) = 3</proc/401/status>',
                 '100 openat(AT_FDCWD</repo>, "/proc/405/stat", O_RDONLY|O_CLOEXEC) = 3</proc/405/stat>']
        parser = self.parse(self.pad(lines))
        self.assertEqual((parser.unresolved_reasons, parser.pid_space_untrusted, parser.unit_proc_accesses), ({}, False, 4))   # 每次 openat 登记两次（请求路径＋描述符标注）
        self.assertNotIn("/proc/401/status", parser.accesses)

    def test_clone_untraced_is_still_unobservable(self) -> None:
        self.assertEqual(self.reasons(['100 clone(child_stack=NULL, flags=CLONE_UNTRACED|SIGCHLD) = 400']), {"unobservable-child:clone": 1})

    def test_unshare_and_setns_are_unsupported_and_make_the_pid_space_untrusted(self) -> None:
        """``unshare(CLONE_NEWPID)`` 之后调用者自己不变，但它的后续子进程在新 PID 命名空间里；``setns`` 同理。两者本来就是 unsupported-call。"""
        for label, namespace_call in (("unshare NEWPID", '100 unshare(CLONE_NEWPID) = 0'),
                                      ("unshare 全部", '100 unshare(CLONE_NEWNS|CLONE_NEWPID|CLONE_NEWUSER) = 0'),
                                      ("unshare 失败", '100 unshare(CLONE_NEWPID) = -1 EPERM (Operation not permitted)'),
                                      ("setns", '100 setns(5<pid:[4026531836]>, CLONE_NEWPID) = 0'),
                                      ("unshare 非命名空间标志", '100 unshare(CLONE_FS) = 0')):
            lines = self.pad([namespace_call, '100 clone(child_stack=NULL, flags=SIGCHLD) = 400',
                              '400 clone(child_stack=NULL, flags=SIGCHLD) = 2',
                              '100 openat(AT_FDCWD</repo>, "/proc/2/status", O_RDONLY|O_CLOEXEC) = 3</proc/2/status>'])
            with self.subTest(label=label):
                parser = self.parse(lines)
                name = namespace_call.split()[1].split("(")[0]
                self.assertEqual(parser.unresolved_reasons, {f"unsupported-call:{name}": 1})
                self.assertTrue(parser.pid_space_untrusted)
                self.assertEqual(parser.accesses["/proc/2/status"], {"read"})
                self.assertFailsClosed(lines, where="parser", fragment="unsupported-call")

    def test_reads_of_the_own_proc_state_before_any_namespace_change_are_still_internal(self) -> None:
        """只有“之后”才不可信：命名空间调用之前已经发生的、属于本轨迹的 /proc 读取不受影响（它们是当时的真实情况）。"""
        lines = self.pad(['100 clone(child_stack=NULL, flags=SIGCHLD) = 401',
                          '100 openat(AT_FDCWD</repo>, "/proc/401/status", O_RDONLY|O_CLOEXEC) = 3</proc/401/status>',
                          '100 unshare(CLONE_NEWPID) = 0',
                          '100 openat(AT_FDCWD</repo>, "/proc/401/stat", O_RDONLY|O_CLOEXEC) = 3</proc/401/stat>'])
        parser = self.parse(lines)
        self.assertNotIn("/proc/401/status", parser.accesses)
        self.assertEqual(parser.accesses["/proc/401/stat"], {"read"})

    def test_v3_is_unchanged_it_only_knows_untraced_and_newns(self) -> None:
        for flag, expected in (("CLONE_NEWNS", {"unobservable-child:clone": 1}), ("CLONE_UNTRACED", {"unobservable-child:clone": 1}),
                               ("CLONE_NEWPID", {}), ("CLONE_NEWUSER", {}), ("CLONE_NEWNET", {})):
            with self.subTest(flag=flag):
                self.assertEqual(self.reasons([f'100 clone(child_stack=NULL, flags={flag}|SIGCHLD) = 400'], "v3"), expected)
        parser = ra.TraceParser(strict=True, runtime_context=self.context)
        self.assertFalse(parser.pid_space_untrusted)
        for line in ('100 unshare(CLONE_NEWPID) = 0', '100 clone(child_stack=NULL, flags=CLONE_NEWPID|SIGCHLD) = 400'):
            parser.feed(line)
        self.assertFalse(parser.pid_space_untrusted, "v3 不维护 pid 空间状态")


# =====================================================================================================================
# 纵深防御：readlink 返回的文本、lstat 的 st_size 与记录的链接文本交叉核对
# =====================================================================================================================


class LinkRecordCrossCheckTests(R7aFixture):
    """跟踪器记着本单元写下的每个符号链接的文本；链接名字上的 readlink 返回什么、lstat 报告的 st_size 是多少，都必须与它一致。
    这些值本来只可能来自被测代码自己写入的链接，所以正常轨迹永远一致；一旦不一致，说明名字背后的对象已不是本单元写下的那个
    （例如被某个没观察到的路径替换成别的链接），名字表不能再相信，尽早失败关闭。
    """

    def link_lines(self, text: str = "testing") -> list[str]:
        return self.base_lines() + [self.symlink_line("d/l", text)]

    def test_matching_sizes_and_texts_pass_for_every_spelling(self) -> None:
        for text in ("testing", "/etc/passwd", "a b\\tc", "\\303\\251", "x" * 300):
            for call in ("lstat", "lstat64", "newfstatat", "fstatat64", "statx"):
                with self.subTest(text=text, call=call):
                    self.assertPasses(self.link_lines(text) + [self.lstat_line("d/l", call)])
            for call in ("readlink", "readlinkat"):
                with self.subTest(text=text, readlink=call):
                    self.assertPasses(self.link_lines(text) + [self.readlink_line("d/l", call)])

    def test_the_size_is_counted_in_bytes_after_unescaping(self) -> None:
        self.assertEqual(self.recorded_size("d/l"), 7, "夹具默认值")
        self.declare_link_text("d/l", "\\303\\251")                       # é：UTF-8 两个字节
        self.assertEqual(self.recorded_size("d/l"), 2)
        lines = self.link_lines("\\303\\251")
        self.assertPasses(lines + [self.lstat_line("d/l")])
        self.assertFailsClosed(lines + [self.lstat_line("d/l", struct=STAT_LNK.replace("st_size=7", "st_size=1"))], where="parser",
                               fragment="symlink-lstat-size-contradicts-text")

    def test_a_size_that_differs_from_the_recorded_text_is_refused(self) -> None:
        for call in ("lstat", "lstat64", "newfstatat", "fstatat64"):
            for wrong in ("0", "6", "8", "4096"):
                with self.subTest(call=call, size=wrong):
                    line = self.lstat_line("d/l", call, struct=STAT_LNK.replace("st_size=7", f"st_size={wrong}"))
                    document = self.assertFailsClosed(self.link_lines() + [line], where="parser", fragment="symlink-lstat-size-contradicts-text")
                    self.assertEqual(document["coverage"]["unresolved_reasons"], {f"symlink-lstat-size-contradicts-text:{call}": 1},
                                     "只登记这一条专门的原因，不重复登记 symlink-metadata-not-bound")

    def test_statx_size_is_checked_with_its_own_field_name(self) -> None:
        line = self.lstat_line("d/l", "statx")
        self.assertPasses(self.link_lines() + [line])
        wrong = line.replace("stx_size=7", "stx_size=70")
        self.assertFailsClosed(self.link_lines() + [wrong], where="parser", fragment="symlink-lstat-size-contradicts-text:statx")
        # st_size 不是 statx 的字段名：写成 st_size 等于没有 stx_size。
        renamed = line.replace("stx_size=7", "st_size=7")
        self.assertFailsClosed(self.link_lines() + [renamed], where="parser", fragment="symlink-lstat-size-not-decoded:statx")

    def test_a_size_that_cannot_be_read_unambiguously_is_refused(self) -> None:
        good = STAT_LNK
        variants = {
            "缺字段": good.replace(" st_size=7,", ""),
            "重复字段": good.replace("st_size=7,", "st_size=7, st_size=7,"),
            "十六进制": good.replace("st_size=7", "st_size=0x7"),
            "前导零": good.replace("st_size=7", "st_size=07"),
            "带符号": good.replace("st_size=7", "st_size=+7"),
            "负数": good.replace("st_size=7", "st_size=-7"),
            "名字": good.replace("st_size=7", "st_size=SEVEN"),
            "带注释": good.replace("st_size=7", "st_size=7 /* x */"),
            "二十一位数字": good.replace("st_size=7", "st_size=" + "9" * 21),
            "超长数字串不崩": good.replace("st_size=7", "st_size=" + "9" * 5000),
        }
        for label, struct in variants.items():
            with self.subTest(label=label):
                self.assertFailsClosed(self.link_lines() + [self.lstat_line("d/l", struct=struct)], where="parser",
                                       fragment="symlink-lstat-size-not-decoded")

    def test_the_other_lstat_rules_are_untouched_by_the_size_check(self) -> None:
        """缩略结构、非链接结构、返回非 0 的 lstat 仍走原来的路径（symlink-metadata-not-bound 或矛盾），不进新的核对。"""
        abbreviated = '{st_mode=S_IFLNK|0777, ...}'
        _document, reasons, _problems = self.outcome(self.link_lines() + [self.lstat_line("d/l", struct=abbreviated)])
        self.assertEqual(reasons, {"symlink-metadata-not-bound:newfstatat": 1})
        _document, reasons, _problems = self.outcome(self.link_lines() + [self.lstat_line("d/l", struct=STAT_REG)])
        self.assertEqual(reasons, {"symlink-state-contradicts-stat:newfstatat": 1})

    def test_a_readlink_that_returns_another_text_is_refused(self) -> None:
        for call in ("readlink", "readlinkat"):
            for label, text in (("完全不同", "elsewhere"), ("多一个字符", "testingx"), ("少一个字符", "testin"), ("大小写", "Testing"),
                                ("空串", "")):
                with self.subTest(call=call, label=label):
                    line = self.readlink_line("d/l", call, text=text)
                    self.assertFailsClosed(self.link_lines() + [line], where="parser", fragment="symlink-readlink-text-contradicts-record")

    def test_a_readlink_return_value_must_equal_the_number_of_bytes(self) -> None:
        for call, template in (("readlink", '1 readlink("{path}", "testing", 4096) = {n}'),
                               ("readlinkat", '1 readlinkat(AT_FDCWD</repo>, "{path}", "testing", 4096) = {n}')):
            self.assertPasses(self.link_lines() + [template.format(path=self.p("d/l"), n=7)])
            for wrong in (6, 8, 0, 4096):
                with self.subTest(call=call, n=wrong):
                    self.assertFailsClosed(self.link_lines() + [template.format(path=self.p("d/l"), n=wrong)], where="parser",
                                           fragment="symlink-readlink-text-contradicts-record")

    def test_a_small_buffer_truncates_the_text_and_the_prefix_is_what_must_come_back(self) -> None:
        path = self.p("d/l")
        for call, template in (("readlink", '1 readlink("{path}", "{buffer}", {size}) = {n}'),
                               ("readlinkat", '1 readlinkat(AT_FDCWD</repo>, "{path}", "{buffer}", {size}) = {n}')):
            ok = [("tes", 3, 3), ("t", 1, 1), ("testing", 7, 7), ("testing", 4096, 7), ("testing", 8, 7)]
            for buffer, size, n in ok:
                with self.subTest(call=call, ok=(buffer, size, n)):
                    self.assertPasses(self.link_lines() + [template.format(path=path, buffer=buffer, size=size, n=n)])
            bad = [("testing", 3, 7), ("testing", 3, 3), ("tes", 3, 7), ("tex", 3, 3), ("tes", 4096, 3)]
            for buffer, size, n in bad:
                with self.subTest(call=call, bad=(buffer, size, n)):
                    self.assertFailsClosed(self.link_lines() + [template.format(path=path, buffer=buffer, size=size, n=n)], where="parser",
                                           fragment="symlink-readlink-text-contradicts-record")

    def test_a_readlink_buffer_that_is_not_a_complete_string_cannot_be_confirmed(self) -> None:
        path = self.p("d/l")
        for label, line in (("指针", f'1 readlinkat(AT_FDCWD</repo>, "{path}", 0x7ffd1000, 4096) = 7'),
                            ("被截断", f'1 readlinkat(AT_FDCWD</repo>, "{path}", "testing"..., 4096) = 7'),
                            ("bufsiz 不是整数", f'1 readlinkat(AT_FDCWD</repo>, "{path}", "testing", sizeof(buf)) = 7'),
                            ("bufsiz 十六进制", f'1 readlinkat(AT_FDCWD</repo>, "{path}", "testing", 0x1000) = 7')):
            with self.subTest(label=label):
                document, reasons, _problems = self.outcome(self.link_lines() + [line])
                self.assertIn("symlink-readlink-text-contradicts-record:readlinkat", reasons)
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_readlink_with_a_relative_path_through_a_directory_descriptor_is_checked_too(self) -> None:
        d = self.p("d")
        self.assertPasses(self.link_lines() + [f'1 readlinkat(5<{d}>, "l", "testing", 4096) = 7'])
        self.assertFailsClosed(self.link_lines() + [f'1 readlinkat(5<{d}>, "l", "other", 4096) = 5'], where="parser",
                               fragment="symlink-readlink-text-contradicts-record")
        self.assertFailsClosed(self.link_lines() + [f'1 chdir("{d}") = 0', '1 readlink("l", "other", 4096) = 5'], where="parser",
                               fragment="symlink-readlink-text-contradicts-record:readlink")

    def test_failed_readlinks_and_other_names_are_not_this_check(self) -> None:
        """失败的 readlink 没有返回文本（EINVAL 在已证明的链接上是矛盾，由跟踪器判）；不是链接的名字由跟踪器一侧判。"""
        _document, reasons, problems = self.outcome(self.link_lines() + [f'1 readlinkat(AT_FDCWD</repo>, "{self.p("d/l")}", 0x1, 4096) = '
                                                                          '-1 EINVAL (Invalid argument)'])
        self.assertNotIn("symlink-readlink-text-contradicts-record:readlinkat", reasons)
        self.assertTrue(problems, "跟踪器：已证明的链接上不可能出现 EINVAL")
        _document, reasons, _problems = self.outcome(self.base_lines() + [self.create_line("d/f"),
                                                                          f'1 readlinkat(AT_FDCWD</repo>, "{self.p("d/f")}", "x", 4096) = 1'])
        self.assertNotIn("symlink-readlink-text-contradicts-record:readlinkat", reasons, "普通文件名字：不在记录里，核对不适用")

    def test_a_host_link_is_not_checked_against_any_record(self) -> None:
        """私有盘之外的链接是宿主访问，由快照绑定其文本；这里没有记录可核对。"""
        host = '1 readlinkat(AT_FDCWD</repo>, "/usr/bin/python3", "python3.12", 4096) = 10'
        _document, reasons, _problems = self.outcome(self.link_lines() + [host])
        self.assertNotIn("symlink-readlink-text-contradicts-record:readlinkat", reasons)

    def test_a_replaced_link_is_caught_before_the_names_table_is_trusted_again(self) -> None:
        """场景：同名链接被换成文本长度不同的另一个（rename 覆盖）——名字表随之更新，核对使用的是新文本，旧文本的 lstat 不再通过。"""
        r = self.root
        lines = self.link_lines("testing") + [self.symlink_line("d/m", "other-link"),
                                              f'1 rename("{r}/d/m", "{r}/d/l") = 0']
        self.declare_link_text("d/l", "other-link")
        self.assertPasses(lines + [self.lstat_line("d/l"), self.readlink_line("d/l")])
        self.declare_link_text("d/l", "testing")
        self.assertFailsClosed(lines + [self.lstat_line("d/l"), self.readlink_line("d/l")], where="parser",
                               fragment="contradicts")

    def test_v3_and_the_default_parser_do_not_run_the_checks(self) -> None:
        wrong = self.readlink_line("d/l", text="other")
        document = self.document(self.link_lines() + [wrong], contract="v3")
        self.assertNotIn("symlink-readlink-text-contradicts-record:readlinkat", document["coverage"]["unresolved_reasons"])
        parser = ra.TraceParser(strict=True)
        for line in self.link_lines() + [wrong, self.lstat_line("d/l", struct=STAT_LNK.replace("st_size=7", "st_size=70"))]:
            parser.feed(line)
        self.assertEqual({key for key in parser.unresolved_reasons if "contradicts" in key or "size" in key}, set())
