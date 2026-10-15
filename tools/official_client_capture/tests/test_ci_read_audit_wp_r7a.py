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
from tools.official_client_capture.tests.test_ci_read_audit_wp_r3 import R3Fixture, STAT_LNK


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
