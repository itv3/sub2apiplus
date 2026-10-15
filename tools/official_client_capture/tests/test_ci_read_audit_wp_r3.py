"""审计解析器加固第三轮（WP-R3）：输出归属接受私有盘内“本单元自己创建的符号链接”，且一切跟随式访问仍然失败关闭。

背景：真实 ARM64 严格审计里，很多测试单元（``git init`` 在临时目录里是极常见的操作）会触发 git 的符号链接支持探测——
``openat(O_EXCL)`` 建同名文件、``unlinkat``、``symlinkat("testing", …)``、``newfstatat(…, AT_SYMLINK_NOFOLLOW)``、``unlinkat``
（见 ``test_ci_read_audit_wp_r2.RAW_GIT_PROBE``，语料里 19 次）。第二轮只放宽解析器那一条没用——输出归属跟踪器直接拒绝私有盘内创建
符号链接，整份轨迹仍被重放拒绝；本轮把两端一起做对，同时保证**任何会解引用链接的访问仍然失败关闭**。仅 v4 合同生效。

设计（实现见 ``tools/ci/read_audit_runtime.py`` 的 ``OutputTrackerV4`` 与 ``tools/ci/read_audit.py`` 的 ``TraceParser``）：

* 符号链接是跟踪器里的一种对象（类型 ``symlink``），链接文本原样记在 ``link_text``，**永远不解引用**。事件的 ``target`` 字段在
  symlink／symlinkat 事件里存链接文本；``flags`` 现在带 ``AT_*`` 标志（白名单 ``AT_FLAGS_V4``），所以 lstat 与 stat 能区分。
* 状态机：``symlink`` 成功（名字未知、父目录已证明、文本非空）→ 新建对象并使名字空间“改变”；失败（EEXIST／ENOENT 等）→ 不建对象；
  ``unlink``／``unlinkat`` 成功 → 解绑名字，对象随最后一个名字消亡；``rename`` 的来源、目的端 → 名字与对象一起迁移，覆盖按
  “目录只覆盖空目录、非目录（文件或符号链接）只覆盖非目录”判；硬链接符号链接、RENAME_EXCHANGE／WHITEOUT、移到树外 → 拒绝。
* 放行与拒绝（对链接名字的访问；每项写明能暴露什么宿主信息，只有暴露不了的才放行）：

  ======================  ====  =========================================================================================
  调用                    判定  理由
  ======================  ====  =========================================================================================
  symlink／symlinkat 创建  放行  不读取链接文本指向的任何东西；目标是否存在、在不在盘内都不影响结果
  unlink／unlinkat         放行  作用在目录项上，Linux 不解引用最后一个分量
  rename 两端              放行  同上（覆盖按类型规则判）
  lstat／lstat64           放行  只读链接自己的 inode 元数据（内容由被测代码自己写入）
  fstatat／statx＋NOFOLLOW 放行  同上；没有 AT_SYMLINK_NOFOLLOW 的 stat 是跟随，拒绝
  readlink／readlinkat     放行  读到的是链接文本，被测代码自己写的
  创建被拒绝 EEXIST        放行  mkdir／mknod／symlink 不解引用最后一个分量；open 必须 O_CREAT|O_EXCL（O_EXCL 不跟随）
  O_NOFOLLOW 打开得 ELOOP  放行  内核碰到链接就拒绝，没有读取目标的任何信息
  O_PATH|O_NOFOLLOW 打开   拒绝  得到指向链接本身的描述符，之后 openat(fd,"")、/proc/self/fd 能把它转成跟随，后续使用不可见
  open／stat／access／     拒绝  会解引用；解引用后的路径是宿主输入，strace 对非描述符调用不会打印它
  chdir／execve／chmod…
  路径的中间分量是链接     拒绝  同上（成功、失败都算：失败的 ENOENT 同样取决于链接目标那一端的状态）
  ``link/``、``link/.``、  拒绝  文本规范化会丢掉“结尾斜杠迫使内核解引用最后的链接”“链接后的 .. 回到目标的父目录”这类语义，
  ``link/..``                    解析器沿原始写法检查，登记 symlink-path-form-unsafe
  ======================  ====  =========================================================================================

本模块的内容：git 探测的端到端（含“放宽前”基线对照）、解析器侧的 lstat 豁免条件与路径形态检查、跟踪器侧的状态机与
放行／拒绝全表、重放篡改、R3-4 前置缺口（openat2、AT_* 标志）、与独立语义模型的差分对拍，以及 v3 不变性。
可选的整份语料重放（``FullCorpusTests``）与 WP-R2 相同，设置 ``WP_R2_CORPUS_DIR`` 才启用。
"""

from __future__ import annotations

import copy
import gzip
import os
import random
import re
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests import test_ci_read_audit_v4_tracker as tracker_tests
from tools.official_client_capture.tests.test_ci_read_audit_v4_tracker import Builder, CONTRACT, D, ROOT, _SimFs, _SimNode
from tools.official_client_capture.tests.test_ci_read_audit_wp_r2 import CORPUS_ROOT, RAW_GIT_PROBE, WORK, CorpusFixture

# 返回结构逐字取自语料（git 探测的 newfstatat）；普通文件版本只把类型换掉。
STAT_LNK = ('{st_dev=makedev(0, 0x69), st_ino=149, st_mode=S_IFLNK|0777, st_nlink=1, st_uid=0, st_gid=0, st_blksize=4096, st_blocks=0, '
            'st_size=7, st_atime=1792047380 /* 2026-10-15T06:56:20.696287249+0000 */, st_atime_nsec=696287249, '
            'st_mtime=1792047380 /* 2026-10-15T06:56:20.696287249+0000 */, st_mtime_nsec=696287249, '
            'st_ctime=1792047380 /* 2026-10-15T06:56:20.696287249+0000 */, st_ctime_nsec=696287249}')
STAT_REG = STAT_LNK.replace("S_IFLNK|0777", "S_IFREG|0644")
STATX_LNK = ('{stx_mask=STATX_BASIC_STATS|STATX_MNT_ID, stx_blksize=4096, stx_attributes=0, stx_nlink=1, stx_uid=0, stx_gid=0, '
             'stx_mode=S_IFLNK|0777, stx_ino=149, stx_size=7, stx_blocks=0, stx_attributes_mask=STATX_ATTR_COMPRESSED|STATX_ATTR_IMMUTABLE, '
             'stx_atime={tv_sec=1792047380, tv_nsec=696287249} /* 2026-10-15T06:56:20.696287249+0000 */, '
             'stx_rdev_major=0, stx_rdev_minor=0, stx_dev_major=0, stx_dev_minor=105}')


class R3Fixture(CorpusFixture):
    """语料里的真实私有根 + 一批拼装 strace 原始行的辅助方法。本类没有 test_ 方法。

    约定：所有行都用 pid 1、AT_FDCWD 标注 ``/repo``；``self.root`` 下先建目录 ``d``（``self.base_lines()``），后面的行都在它下面做。
    """

    def p(self, relative: str) -> str:
        return f"{self.root}/{relative}"

    def base_lines(self) -> list[str]:
        return [self.mkdir_line("d")]

    # ---- 创建 ----
    def symlink_line(self, relative: str, text: str = "testing", *, call: str = "symlinkat", result: str = "0") -> str:
        if call == "symlink":
            return f'1 symlink("{text}", "{self.p(relative)}") = {result}'
        return f'1 symlinkat("{text}", AT_FDCWD</repo>, "{self.p(relative)}") = {result}'

    # ---- 不跟随的元数据 ----
    def lstat_line(self, relative: str, call: str = "newfstatat", *, struct: str = STAT_LNK, tail: str = "0") -> str:
        path = self.p(relative)
        if call in {"lstat", "lstat64"}:
            return f'1 {call}("{path}", {struct}) = {tail}'
        if call == "statx":
            return f'1 statx(AT_FDCWD</repo>, "{path}", AT_STATX_SYNC_AS_STAT|AT_SYMLINK_NOFOLLOW, STATX_BASIC_STATS|STATX_MNT_ID, {STATX_LNK}) = {tail}'
        return f'1 {call}(AT_FDCWD</repo>, "{path}", {struct}, AT_SYMLINK_NOFOLLOW) = {tail}'

    def readlink_line(self, relative: str, call: str = "readlinkat", text: str = "testing") -> str:
        path = self.p(relative)
        if call == "readlink":
            return f'1 readlink("{path}", "{text}", 4096) = {len(text)}'
        return f'1 readlinkat(AT_FDCWD</repo>, "{path}", "{text}", 4096) = {len(text)}'

    def unlink_line(self, relative: str, call: str = "unlinkat", flags: str = "0") -> str:
        if call == "unlink":
            return f'1 unlink("{self.p(relative)}") = 0'
        return f'1 unlinkat(AT_FDCWD</repo>, "{self.p(relative)}", {flags}) = 0'

    # ---- 一份文档的三个结论 ----
    def outcome(self, lines: list[str]) -> tuple[dict[str, Any], dict[str, int], list[str]]:
        """(文档, 未解析原因, 归属问题)。"""
        document = self.document(lines)
        return document, dict(document["coverage"]["unresolved_reasons"]), list(document["runtime"]["outputs"]["problems"])

    def assertPasses(self, lines: list[str], *, events: bool = True) -> dict[str, Any]:
        """未解析为零、归属问题为空、整套严格审计与重放通过。"""
        document, reasons, problems = self.outcome(lines)
        self.assertEqual((reasons, problems), ({}, []), lines[-1][:200])
        self.assertAccepted(document)
        self.assertEqual(runtime.replay_runtime(document, [self.entry]) is not None, True)
        return document

    def assertFailsClosed(self, lines: list[str], *, where: str, fragment: str = "") -> dict[str, Any]:
        """整套审计不通过。``where``：``"parser"`` 解析器登记未解析原因（含片段）；``"tracker"`` 归属出问题；``"either"`` 任一。"""
        document, reasons, problems = self.outcome(lines)
        self.assertFalse(self.audit(document)["coverage_complete"], f"必须失败关闭：{lines[-1][:200]}")
        parser_hit = bool(reasons) and (not fragment or any(fragment in key for key in reasons))
        tracker_hit = bool(problems) and (not fragment or any(fragment in text for text in problems))
        expected = {"parser": parser_hit, "tracker": tracker_hit, "either": parser_hit or tracker_hit}[where]
        self.assertTrue(expected, f"{where} 应命中「{fragment}」：reasons={reasons} problems={problems} line={lines[-1][:160]}")
        return document


# =====================================================================================================================
# git 符号链接支持探测：端到端
# =====================================================================================================================


class GitProbeEndToEndTests(R3Fixture):
    """WP-R2 里固定“应被拒”的 git 探测序列，在本轮之后应当通过（那个类的断言被有意识地改写，见 test_ci_read_audit_wp_r2）。"""

    def probe_lines(self, suffix: str = "") -> list[str]:
        """语料原始行（RAW_GIT_PROBE）加上 tmp 目录的创建；``suffix`` 让同一份序列可以在不同名字下重复。"""
        base = [f'1 mkdir("{self.root}/tmpwzmo6c4w", 0700) = 0', f'1 mkdir("{self.root}/tmpwzmo6c4w/history", 0700) = 0']
        return base + [line.replace("tkkGrhW", "tkkGrhW" + suffix) for line in RAW_GIT_PROBE]

    def test_the_corpus_probe_passes_the_whole_chain(self) -> None:
        document = self.assertPasses(self.probe_lines())
        events = {(e["name"], e["path"].rsplit("/", 1)[-1], e["succeeded"]): e for e in document["runtime"]["output_events"]}
        symlinked = events[("symlinkat", "tkkGrhW", True)]
        self.assertEqual((symlinked["kind"], symlinked["target"], symlinked["errno"]), ("write", "testing", None),
                         "链接文本原样记在事件的 target 字段")
        lstat = events[("newfstatat", "tkkGrhW", True)]
        self.assertEqual((lstat["kind"], lstat["flags"]), ("stat", ["AT_SYMLINK_NOFOLLOW"]), "lstat 与 stat 靠 AT_* 标志区分")
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])

    def test_the_final_state_has_no_leftover_object(self) -> None:
        parser = self.parse(self.probe_lines())
        tracker = parser.output_tracker
        self.assertEqual(tracker.link_text, {}, "探测末尾 unlinkat 删掉了链接，对象消亡、文本不残留")
        self.assertNotIn(self.p("tmpwzmo6c4w/history/.git/tkkGrhW"), tracker.names)
        self.assertEqual(tracker.entries[self.p("tmpwzmo6c4w/history/.git")], 0)

    def test_nineteen_probes_in_a_row_like_the_corpus(self) -> None:
        lines = self.probe_lines("0")
        for index in range(1, 19):
            lines += [line.replace("tkkGrhW", "tkkGrhW" + str(index)) for line in RAW_GIT_PROBE[1:]]
        document = self.assertPasses(lines)
        self.assertEqual(len([e for e in document["runtime"]["output_events"] if e["name"] == "symlinkat"]), 19)

    def test_baseline_with_the_relaxations_switched_off_rejects_the_probe(self) -> None:
        """“放宽前”基线对照：关掉本轮的两处放宽（解析器的 lstat 豁免、跟踪器的创建接受），同一份行序列就判问题。

        跟踪器类在 read_audit 自己加载的实例里（与本测试导入的不是同一个对象），所以补丁打在 ``ra._runtime_module()`` 上。
        基线行为与第二轮固定的一致：解析器登记 symlink-metadata-not-bound，跟踪器报“私有临时盘内不允许创建符号链接”。
        """
        tracker_class = ra._runtime_module().OutputTrackerV4

        def old_create(self_, event):
            self_._problem("私有临时盘内不允许创建符号链接：", event["path"])
            return False

        with mock.patch.object(ra.TraceParser, "_tracked_symlink_lstat", return_value=False), \
                mock.patch.object(tracker_class, "_create_symlink", old_create):
            document, reasons, problems = self.outcome(self.probe_lines())
        self.assertEqual(reasons, {"symlink-metadata-not-bound:newfstatat": 1})
        self.assertTrue(any("私有临时盘内不允许创建符号链接" in text for text in problems), problems)
        self.assertTrue(any("首次读取前未证明由本轮生成" in text for text in problems), problems)
        self.assertTrue(any("删除未证明的输出" in text for text in problems), problems)
        self.assertFalse(self.audit(document)["coverage_complete"])
        # 同一份行序列、放宽之后：干净。
        self.assertPasses(self.probe_lines())

    def test_each_relaxation_alone_is_not_enough(self) -> None:
        tracker_class = ra._runtime_module().OutputTrackerV4

        def old_create(self_, event):
            self_._problem("私有临时盘内不允许创建符号链接：", event["path"])
            return False

        with mock.patch.object(ra.TraceParser, "_tracked_symlink_lstat", return_value=False):
            _document, reasons, problems = self.outcome(self.probe_lines())
        self.assertEqual((reasons, problems), ({"symlink-metadata-not-bound:newfstatat": 1}, []), "只有跟踪器放宽：解析器仍然拒绝")
        # 只有解析器放宽（豁免无条件成立），跟踪器仍按旧规则拒绝创建：重放照样失败。
        with mock.patch.object(tracker_class, "_create_symlink", old_create), \
                mock.patch.object(ra.TraceParser, "_tracked_symlink_lstat", return_value=True):
            _document, reasons, problems = self.outcome(self.probe_lines())
        self.assertEqual(reasons, {}, "解析器放宽了")
        self.assertTrue(problems, "跟踪器仍然拒绝")
        # 豁免是有条件的：跟踪器不认这个链接（创建被拒绝）时，真实的豁免判断仍登记 symlink-metadata-not-bound。
        with mock.patch.object(tracker_class, "_create_symlink", old_create):
            _document, reasons, _problems = self.outcome(self.probe_lines())
        self.assertEqual(reasons, {"symlink-metadata-not-bound:newfstatat": 1})

    def test_the_same_lines_under_v3_are_still_refused_exactly_as_before(self) -> None:
        document = self.document(self.probe_lines(), contract="v3")
        self.assertEqual(document["coverage"]["unresolved_reasons"], {"symlink-metadata-not-bound:newfstatat": 1})
        self.assertTrue(any("链接或移动操作尚无输出归属证明" in text for text in document["runtime"]["outputs"]["problems"]))
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_probe_lines_with_a_different_link_text_and_call_spelling(self) -> None:
        for text, call in (("testing", "symlinkat"), ("/etc/passwd", "symlinkat"), ("../../x", "symlink"), ("a b\\tc", "symlinkat")):
            with self.subTest(text=text, call=call):
                lines = self.base_lines() + [self.symlink_line("d/l", text, call=call), self.lstat_line("d/l"), self.unlink_line("d/l")]
                document = self.assertPasses(lines)
                created = [e for e in document["runtime"]["output_events"] if e["name"] == call]
                self.assertEqual(created[0]["target"], ra.unescape(text), "文本原样记录（转义还原，不解引用）")


# =====================================================================================================================
# 解析器侧：lstat 豁免条件、路径形态、链接文本、AT_* 标志
# =====================================================================================================================


class ParserSymlinkTests(R3Fixture):
    def test_lstat_variants_on_a_tracked_link_are_not_unresolved(self) -> None:
        for call in ("lstat", "lstat64", "newfstatat", "fstatat64", "statx"):
            with self.subTest(call=call):
                lines = self.base_lines() + [self.symlink_line("d/l"), self.lstat_line("d/l", call)]
                self.assertPasses(lines)

    def test_lstat_of_a_link_the_tracker_does_not_know_keeps_the_old_verdict(self) -> None:
        """宿主上的真链接、不在私有盘内的、不是本单元创建的：没有合同，仍登记 symlink-metadata-not-bound。"""
        cases = {
            "私有盘内但本单元没创建": self.base_lines() + [self.lstat_line("d/l")],
            "宿主路径": [self.mkdir_line("d"), '1 newfstatat(AT_FDCWD</repo>, "/usr/bin/python3", ' + STAT_LNK + ', AT_SYMLINK_NOFOLLOW) = 0'],
            "已经被删除的链接": self.base_lines() + [self.symlink_line("d/l"), self.unlink_line("d/l"), self.lstat_line("d/l")],
            "同名的普通文件": self.base_lines() + [self.create_line("d/l"), self.lstat_line("d/l")],
        }
        for label, lines in cases.items():
            with self.subTest(label=label):
                _document, reasons, _problems = self.outcome(lines)
                self.assertEqual(reasons, {"symlink-metadata-not-bound:newfstatat": 1}, label)

    def test_lstat_whose_result_is_not_a_fully_decoded_link_stat_is_not_exempt(self) -> None:
        """跟踪器说是链接，但内核返回的结构不是完整解码的 S_IFLNK：互相印证不了，保持原判。"""
        cases = {
            "缩略结构": '{st_mode=S_IFLNK|0777, ...}',
            "结构里是普通文件": STAT_REG,           # 跟踪器记着是链接而内核说是普通文件：名字表与真实状态脱节，见下面的单独判断
            "结构里是字符设备": STAT_LNK.replace("S_IFLNK|0777", "S_IFCHR|0666"),
            "结构里是命名管道": STAT_LNK.replace("S_IFLNK|0777", "S_IFIFO|0600"),
            "结构里是套接字": STAT_LNK.replace("S_IFLNK|0777", "S_IFSOCK|0755"),
            "缺 mode 字段": '{st_dev=makedev(0, 0x69), st_ino=149}',
            "返回结构是指针": "0x7ffd1234",
        }
        for label, struct in cases.items():
            with self.subTest(label=label):
                lines = self.base_lines() + [self.symlink_line("d/l"), self.lstat_line("d/l", struct=struct)]
                _document, reasons, _problems = self.outcome(lines)
                if label == "结构里是普通文件":
                    self.assertEqual(reasons, {"symlink-state-contradicts-stat:newfstatat": 1}, "互相印证失败：内核说普通文件、跟踪器说链接")
                else:
                    self.assertEqual(reasons, {"symlink-metadata-not-bound:newfstatat": 1}, label)

    def test_a_non_link_lstat_result_on_a_name_the_tracker_has_no_link_for_keeps_the_old_rule(self) -> None:
        """名字表和内核一致（都不是链接）时 S_IFREG 的 lstat 沿用旧规则：不登记任何原因。"""
        for lines in (self.base_lines() + [self.create_line("d/f"), self.lstat_line("d/f", struct=STAT_REG)],
                      self.base_lines() + [self.lstat_line("d/nothing", struct=STAT_REG)],
                      self.base_lines() + [self.symlink_line("d/l"), self.unlink_line("d/l"), self.create_line("d/l"),
                                           self.lstat_line("d/l", struct=STAT_REG)]):
            self.assertEqual(self.outcome(lines)[1].get("symlink-state-contradicts-stat:newfstatat"), None)

    def test_lstat_that_returns_something_other_than_zero_or_fails_is_not_exempt_or_is_a_tracker_problem(self) -> None:
        lines = self.base_lines() + [self.symlink_line("d/l"), self.lstat_line("d/l", tail="1")]
        self.assertEqual(self.outcome(lines)[1], {"symlink-metadata-not-bound:newfstatat": 1})
        failed = self.base_lines() + [self.symlink_line("d/l"),
                                      f'1 newfstatat(AT_FDCWD</repo>, "{self.p("d/l")}", 0x1, AT_SYMLINK_NOFOLLOW) = -1 ENOENT (No such file or directory)']
        document, reasons, problems = self.outcome(failed)
        self.assertEqual(reasons, {})
        self.assertTrue(any("跟随符号链接的访问或没有合同的调用" in text for text in problems), "已证明的链接上失败的 lstat 与轨迹矛盾")
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_the_o_path_o_nofollow_open_of_a_tracked_link_is_still_not_bound(self) -> None:
        """打开链接本身得到的描述符，后续可被 openat(fd, "")、/proc/self/fd 转成跟随，不可见——保持失败关闭。"""
        for call in ("openat", "openat2"):
            with self.subTest(call=call):
                path = self.p("d/l")
                if call == "openat":
                    line = f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY|O_NOFOLLOW|O_PATH) = 3<{path}>'
                else:
                    line = f'1 openat2(AT_FDCWD</repo>, "{path}", {{flags=O_RDONLY|O_NOFOLLOW|O_PATH, resolve=0}}, 24) = 3<{path}>'
                document = self.assertFailsClosed(self.base_lines() + [self.symlink_line("d/l"), line], where="either")
                self.assertIn(f"symlink-metadata-not-bound:{call}", document["coverage"]["unresolved_reasons"])
                self.assertTrue(any("跟随符号链接的访问或没有合同的调用" in text for text in document["runtime"]["outputs"]["problems"]),
                                "跟踪器也不认（防御纵深）")

    def test_the_exemption_does_not_apply_to_non_nofollow_stat(self) -> None:
        """没有 AT_SYMLINK_NOFOLLOW 的 stat 是跟随：解析器不登记（沿用旧规则），但跟踪器判问题。"""
        line = f'1 newfstatat(AT_FDCWD</repo>, "{self.p("d/l")}", {STAT_REG}, 0) = 0'
        self.assertFailsClosed(self.base_lines() + [self.symlink_line("d/l"), line], where="tracker", fragment="跟随")

    def test_plain_lstat_uses_the_process_working_directory(self) -> None:
        lines = self.base_lines() + [f'1 chdir("{self.p("d")}") = 0', '1 symlink("testing", "l") = 0',
                                     f'1 lstat("l", {STAT_LNK}) = 0', '1 unlink("l") = 0']
        self.assertPasses(lines)

    def test_symlink_text_is_recorded_verbatim_and_a_missing_text_is_unresolved(self) -> None:
        document = self.assertPasses(self.base_lines() + [self.symlink_line("d/l", "..\\/..\\/etc\\/passwd")])
        created = [e for e in document["runtime"]["output_events"] if e["name"] == "symlinkat"][0]
        self.assertEqual(created["target"], "../../etc/passwd")
        truncated = f'1 symlinkat("{"x" * 10}"..., AT_FDCWD</repo>, "{self.p("d/l")}") = 0'
        document, reasons, problems = self.outcome(self.base_lines() + [truncated])
        self.assertEqual(reasons["symlink-text-not-decoded:symlinkat"], 1)
        self.assertFalse(self.audit(document)["coverage_complete"])
        pointer = f'1 symlinkat(0x7ffd1234, AT_FDCWD</repo>, "{self.p("d/l")}") = 0'
        self.assertEqual(self.outcome(self.base_lines() + [pointer])[1], {"symlink-text-not-decoded:symlinkat": 1})

    def test_failed_symlink_calls_register_nothing_and_leave_the_state_alone(self) -> None:
        r = self.p
        cases = {
            "EEXIST 落在已知文件": (self.base_lines() + [self.create_line("d/f"), self.symlink_line("d/f", result="-1 EEXIST (File exists)")], True),
            "EEXIST 落在已知目录": (self.base_lines() + [self.symlink_line("d", result="-1 EEXIST (File exists)")], True),
            "ENOENT 父目录不存在": (self.base_lines() + [self.symlink_line("d/nodir/l", result="-1 ENOENT (No such file or directory)")], True),
            "EACCES": (self.base_lines() + [self.symlink_line("d/l", result="-1 EACCES (Permission denied)")], False),
            "EROFS": (self.base_lines() + [self.symlink_line("d/l", result="-1 EROFS (Read-only file system)")], False),
            "ENOSPC": (self.base_lines() + [self.symlink_line("d/l", result="-1 ENOSPC (No space left on device)")], False),
            "EEXIST 落在未知名字": (self.base_lines() + [self.symlink_line("d/never", result="-1 EEXIST (File exists)")], False),
        }
        for label, (lines, clean) in cases.items():
            with self.subTest(label=label):
                document, reasons, problems = self.outcome(lines)
                self.assertEqual(reasons, {})
                self.assertEqual(problems == [], clean, (label, problems))
                parser = self.parse(lines)
                self.assertEqual(parser.output_tracker.link_text, {}, "失败的 symlink 调用不创建对象")
                self.assertNotIn(r("d/l"), parser.output_tracker.names)
        lines = self.base_lines() + [self.symlink_line("d/l"), self.symlink_line("d/l", "other", result="-1 EEXIST (File exists)"),
                                     self.lstat_line("d/l")]
        parser = self.parse(lines)
        self.assertEqual(list(parser.output_tracker.link_text.values()), ["testing"], "重复创建 EEXIST 不改变已记录的文本与对象")
        self.assertEqual(parser.output_tracker.problems, set())

    def test_path_forms_that_make_normalisation_lie_are_unresolved_only_while_a_link_exists(self) -> None:
        r = self.root
        link = self.base_lines() + [self.symlink_line("d/l")]
        hazards = {
            "结尾斜杠 lstat": f'1 newfstatat(AT_FDCWD</repo>, "{self.p("d/l")}/", {STAT_LNK}, AT_SYMLINK_NOFOLLOW) = 0',
            "结尾点": f'1 readlinkat(AT_FDCWD</repo>, "{self.p("d/l")}/.", "x", 4096) = 1',
            "链接后接 ..": f'1 mkdir("{self.p("d/l")}/../sub", 0700) = 0',
            "中间的 ..": f'1 mkdir("{self.p("d/l")}/./../sub", 0700) = 0',
            "相对写法 unlink 结尾斜杠": f'1 unlinkat(5<{self.p("d")}>, "l/", 0) = -1 EISDIR (Is a directory)',
            "绝对路径里的 ..": f'1 mkdir("{r}/d/l/../../escape", 0700) = 0',
            "相对写法 ..": f'1 mkdir("d/l/../x", 0700) = 0',
        }
        for label, hazard in hazards.items():
            with self.subTest(label=label):
                lines = link + [f'1 chdir("{self.root}") = 0', hazard] if label == "相对写法 .." else link + [hazard]
                _document, reasons, _problems = self.outcome(lines)
                self.assertIn("symlink-path-form-unsafe", reasons, label)
        # 同样的写法在没有符号链接存活时与旧行为一致：不登记。
        gone = link + [self.unlink_line("d/l")]
        for label, hazard in hazards.items():
            if label in {"相对写法 ..", "相对写法 unlink 结尾斜杠"}:
                continue
            with self.subTest(label=label, state="链接已删除"):
                text = hazard.replace("/l/../", "/x/../").replace("/l/./../", "/x/./../").replace("d/l/", "d/x/").replace("d/l\"", "d/x\"")
                _document, reasons, _problems = self.outcome(self.base_lines() + [self.mkdir_line("d/x"), self.symlink_line("d/l"),
                                                                                     self.unlink_line("d/l"), text])
                self.assertNotIn("symlink-path-form-unsafe", reasons, label)
        _document, reasons, _problems = self.outcome(self.base_lines() + [f'1 mkdir("{self.p("d")}/", 0700) = -1 EEXIST (File exists)'])
        self.assertNotIn("symlink-path-form-unsafe", reasons, "从未创建过符号链接：结尾斜杠是普通写法")

    def test_a_dotdot_that_does_not_pass_through_a_link_is_not_a_hazard(self) -> None:
        lines = self.base_lines() + [self.mkdir_line("d/sub"), self.symlink_line("d/l"),
                                     f'1 mkdir("{self.p("d/sub")}/../sub2", 0700) = 0']
        _document, reasons, _problems = self.outcome(lines)
        self.assertEqual(reasons, {})

    def test_hazard_helper_directly(self) -> None:
        tracker_class = ra._runtime_module().OutputTrackerV4
        tracker = tracker_class(self.contract)
        for event in (Builder().mkdir(self.p("d"))[0], Builder().symlink(self.p("d/l"))[0]):
            tracker.observe_event(event)
        base = self.p("d")
        cases = [("l/", True), ("l/.", True), ("l/..", True), ("l/../x", True), ("x/../l/", True), ("./l/", True), ("l", False),
                 ("l/x", False), ("x/", False), ("x/..", False), ("", False), ("../d/l/", True), (self.p("d/l") + "/", True),
                 (self.p("d/l") + "/..", True), (self.p("d/x/../l"), False), ("//", False)]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ra._path_form_hazard(tracker, base, raw), expected, raw)
        self.assertFalse(ra._path_form_hazard(tracker_class(self.contract), base, "l/"), "没有符号链接时永远没有风险")
        self.assertFalse(ra._path_form_hazard(tracker, None, "l/"), "相对路径而没有基准目录：本来就无法还原")

    def test_at_flags_are_carried_into_events_and_unknown_ones_are_unresolved(self) -> None:
        lines = self.base_lines() + [self.mkdir_line("d/s"), self.create_line("d/s/f"),
                                     f'1 unlinkat(AT_FDCWD</repo>, "{self.p("d/s/f")}", 0) = 0',
                                     f'1 unlinkat(AT_FDCWD</repo>, "{self.p("d/s")}", AT_REMOVEDIR) = 0',
                                     f'1 mkdirat(AT_FDCWD</repo>, "{self.p("d/m")}", 0700) = 0',
                                     f'1 newfstatat(AT_FDCWD</repo>, "{self.p("d/m")}", {STAT_REG.replace("S_IFREG|0644", "S_IFDIR|0700")}, '
                                     'AT_SYMLINK_NOFOLLOW|AT_NO_AUTOMOUNT) = 0',
                                     f'1 faccessat2(AT_FDCWD</repo>, "{self.p("d/m")}", F_OK, AT_EACCESS|AT_SYMLINK_NOFOLLOW) = 0']
        document = self.assertPasses(lines)
        by_name: dict[str, list[list[str]]] = {}
        for event in document["runtime"]["output_events"]:
            by_name.setdefault(event["name"], []).append(event["flags"])
        self.assertIn(["AT_REMOVEDIR"], by_name["unlinkat"])
        self.assertIn([], by_name["unlinkat"], "flags 为 0 的 unlinkat 没有标志")
        self.assertEqual(by_name["mkdirat"], [[]], "AT_FDCWD 是描述符取值，不是标志，不进事件")
        self.assertEqual(by_name["newfstatat"], [["AT_NO_AUTOMOUNT", "AT_SYMLINK_NOFOLLOW"]])
        self.assertEqual(by_name["faccessat2"], [["AT_EACCESS", "AT_SYMLINK_NOFOLLOW"]])
        unknown = self.base_lines() + [f'1 newfstatat(AT_FDCWD</repo>, "{self.p("d")}", {STAT_REG}, AT_FOO_BAR) = 0']
        self.assertEqual(self.outcome(unknown)[1], {"at-flag-not-decoded:newfstatat": 1})
        self.assertFalse(self.audit(self.document(unknown))["coverage_complete"])

    def test_rename_and_link_flags_are_combined_with_at_flags(self) -> None:
        lines = self.base_lines() + [self.create_line("d/a"),
                                     f'1 linkat(AT_FDCWD</repo>, "{self.p("d/a")}", AT_FDCWD</repo>, "{self.p("d/b")}", AT_SYMLINK_FOLLOW) = 0',
                                     f'1 renameat2(AT_FDCWD</repo>, "{self.p("d/b")}", AT_FDCWD</repo>, "{self.p("d/c")}", RENAME_NOREPLACE) = 0']
        document = self.assertPasses(lines)
        flags = {e["name"]: e["flags"] for e in document["runtime"]["output_events"] if e["name"] in {"linkat", "renameat2"}}
        self.assertEqual(flags, {"linkat": ["AT_SYMLINK_FOLLOW"], "renameat2": ["RENAME_NOREPLACE"]})

    def test_the_two_at_flag_whitelists_are_equal(self) -> None:
        self.assertEqual(ra.AT_FLAGS_V4, runtime.AT_FLAGS_V4, "解析器与重放端各存一份白名单，必须相等")
        for flag in runtime.AT_FLAGS_V4:
            self.assertRegex(flag, r"^AT_[A-Z_]+$")
            self.assertTrue(runtime._V4_FLAG.fullmatch(flag))
        self.assertIsNone(runtime._V4_FLAG.fullmatch("AT_FDCWD"))
        self.assertIsNone(runtime._V4_FLAG.fullmatch("AT_FOO"))


# =====================================================================================================================
# 跟随式访问：一律失败关闭（端到端）
# =====================================================================================================================


class FollowingAccessIsRefusedTests(R3Fixture):
    """对本单元自己创建的链接 ``d/l``（文本分别指向盘外与盘内），每一种会解引用的访问都必须失败关闭；不跟随的访问通过。"""

    def with_link(self, text: str, *tail: str) -> list[str]:
        return self.base_lines() + [self.create_line("d/f"), self.symlink_line("d/l", text), *tail]

    def test_following_calls_on_the_link_name_are_refused(self) -> None:
        l, r = self.p("d/l"), self.root
        host_fd = "/etc/passwd"
        cases = {
            "open 读（盘外目标）": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY) = 3<{host_fd}>',
            "open 读（盘内目标）": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY) = 3<{self.p("d/f")}>',
            "open 无标志旧写法": f'1 open("{l}", O_RDONLY) = 3<{self.p("d/f")}>',
            "open 经链接创建悬空目标": f'1 openat(AT_FDCWD</repo>, "{l}", O_WRONLY|O_CREAT, 0600) = 3<{self.p("d/testing")}>',
            "open 目录": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY|O_DIRECTORY) = 3<{self.p("d")}>',
            "open 悬空失败 ENOENT": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY) = -1 ENOENT (No such file or directory)',
            "open 失败 ELOOP 但没有 O_NOFOLLOW": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY) = -1 ELOOP (Too many levels of symbolic links)',
            "creat": f'1 creat("{l}", 0600) = 3<{self.p("d/f")}>',
            "stat 跟随": f'1 newfstatat(AT_FDCWD</repo>, "{l}", {STAT_REG}, 0) = 0',
            "stat 悬空 ENOENT": f'1 newfstatat(AT_FDCWD</repo>, "{l}", 0x1, 0) = -1 ENOENT (No such file or directory)',
            "stat 旧写法": f'1 stat("{l}", {STAT_REG}) = 0',
            "statx 跟随": f'1 statx(AT_FDCWD</repo>, "{l}", AT_STATX_SYNC_AS_STAT, STATX_BASIC_STATS, {STATX_LNK.replace("S_IFLNK", "S_IFREG")}) = 0',
            "access": f'1 access("{l}", R_OK) = 0',
            "faccessat": f'1 faccessat(AT_FDCWD</repo>, "{l}", R_OK) = 0',
            "faccessat2 NOFOLLOW": f'1 faccessat2(AT_FDCWD</repo>, "{l}", R_OK, AT_SYMLINK_NOFOLLOW) = 0',
            "chdir": f'1 chdir("{l}") = 0',
            "execve": f'1 execve("{l}", ["x"], 0x7ffd /* 3 vars */) = 0',
            "chmod": f'1 chmod("{l}", 0600) = 0',
            "fchmodat": f'1 fchmodat(AT_FDCWD</repo>, "{l}", 0600) = 0',
            "chown": f'1 chown("{l}", 0, 0) = 0',
            "utimensat 跟随": f'1 utimensat(AT_FDCWD</repo>, "{l}", [{{tv_sec=1, tv_nsec=0}}, {{tv_sec=1, tv_nsec=0}}], 0) = 0',
            "utimensat NOFOLLOW": f'1 utimensat(AT_FDCWD</repo>, "{l}", [{{tv_sec=1, tv_nsec=0}}, {{tv_sec=1, tv_nsec=0}}], AT_SYMLINK_NOFOLLOW) = 0',
            "truncate": f'1 truncate("{l}", 0) = 0',
            "getxattr": f'1 getxattr("{l}", "user.x", 0x1, 0) = -1 ENODATA (No data available)',
            "statfs": f'1 statfs("{l}", {{f_type=TMPFS_MAGIC}}) = 0',
            "inotify_add_watch": f'1 inotify_add_watch(4, "{l}", IN_MODIFY) = 1',
            "rmdir": f'1 rmdir("{l}") = -1 ENOTDIR (Not a directory)',
            "unlinkat AT_REMOVEDIR 成功": f'1 unlinkat(AT_FDCWD</repo>, "{l}", AT_REMOVEDIR) = 0',
            "mkdir 成功": f'1 mkdir("{l}", 0700) = 0',
            "mknod": f'1 mknod("{l}", S_IFREG|0600, 0) = 0',
            "硬链接来源": f'1 link("{l}", "{self.p("d/hard")}") = 0',
            "硬链接来源 linkat AT_SYMLINK_FOLLOW": f'1 linkat(AT_FDCWD</repo>, "{l}", AT_FDCWD</repo>, "{self.p("d/hard")}", AT_SYMLINK_FOLLOW) = 0',
            "RENAME_EXCHANGE": f'1 renameat2(AT_FDCWD</repo>, "{l}", AT_FDCWD</repo>, "{self.p("d/f")}", RENAME_EXCHANGE) = 0',
            "RENAME_WHITEOUT": f'1 renameat2(AT_FDCWD</repo>, "{l}", AT_FDCWD</repo>, "{self.p("d/w")}", RENAME_WHITEOUT) = 0',
            "移到树外": f'1 rename("{l}", "/host/l") = 0',
            "从树外移入覆盖": f'1 rename("/host/l", "{l}") = 0',
            "O_PATH|O_NOFOLLOW": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY|O_NOFOLLOW|O_PATH) = 3<{l}>',
            "success 的 O_NOFOLLOW（不可能，防伪造）": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY|O_NOFOLLOW) = 3<{l}>',
            "O_NOFOLLOW|O_DIRECTORY 得 ENOTDIR": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY|O_NOFOLLOW|O_DIRECTORY) = -1 ENOTDIR (Not a directory)',
            "readlink 失败": f'1 readlink("{l}", 0x1, 4096) = -1 EINVAL (Invalid argument)',
            "getdents": f'1 getdents64(3<{l}>, 0x1, 32768) = 0',
        }
        for label, line in cases.items():
            for text in ("/etc/passwd", "testing"):
                with self.subTest(label=label, text=text):
                    self.assertFailsClosed(self.with_link(text, line), where="either")

    def test_non_following_calls_on_the_link_name_pass(self) -> None:
        l = self.p("d/l")
        cases = {
            "lstat": self.lstat_line("d/l", "lstat"), "newfstatat NOFOLLOW": self.lstat_line("d/l"),
            "statx NOFOLLOW": self.lstat_line("d/l", "statx"), "readlink": self.readlink_line("d/l", "readlink"),
            "readlinkat": self.readlink_line("d/l"),
            "mkdir EEXIST": f'1 mkdir("{l}", 0700) = -1 EEXIST (File exists)',
            "mkdirat EEXIST": f'1 mkdirat(AT_FDCWD</repo>, "{l}", 0700) = -1 EEXIST (File exists)',
            "symlink EEXIST": self.symlink_line("d/l", "other", result="-1 EEXIST (File exists)"),
            "mknod EEXIST": f'1 mknod("{l}", S_IFREG|0600, 0) = -1 EEXIST (File exists)',
            "O_EXCL 创建 EEXIST": f'1 openat(AT_FDCWD</repo>, "{l}", O_WRONLY|O_CREAT|O_EXCL, 0600) = -1 EEXIST (File exists)',
            "O_NOFOLLOW ELOOP": f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY|O_NOFOLLOW) = -1 ELOOP (Too many levels of symbolic links)',
            "O_NOFOLLOW|O_CREAT ELOOP": f'1 openat(AT_FDCWD</repo>, "{l}", O_WRONLY|O_CREAT|O_NOFOLLOW, 0600) = -1 ELOOP (Too many levels of symbolic links)',
            "unlink": self.unlink_line("d/l", "unlink"), "unlinkat": self.unlink_line("d/l"),
            "rename 出自": f'1 rename("{l}", "{self.p("d/m")}") = 0',
            "rename 覆盖普通文件": f'1 rename("{l}", "{self.p("d/f")}") = 0',
            "renameat2 NOREPLACE": f'1 renameat2(AT_FDCWD</repo>, "{l}", AT_FDCWD</repo>, "{self.p("d/m")}", RENAME_NOREPLACE) = 0',
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                self.assertPasses(self.with_link("/etc/passwd", line))

    def test_a_regular_file_renamed_over_the_link_replaces_it(self) -> None:
        lines = self.with_link("/etc/passwd", f'1 rename("{self.p("d/f")}", "{self.p("d/l")}") = 0', self.read_line("d/l"))
        document = self.assertPasses(lines)
        tracker = self.parse(lines).output_tracker
        self.assertEqual(tracker.link_text, {}, "链接被覆盖后对象消亡")
        self.assertNotIn(self.p("d/f"), tracker.names)
        self.assertEqual(document["coverage"]["unresolved"], 0)

    def test_accesses_through_the_link_as_an_intermediate_component_are_refused(self) -> None:
        l, r = self.p("d/l"), self.root
        cases = {
            "open 读": f'1 openat(AT_FDCWD</repo>, "{l}/x", O_RDONLY) = 3</etc/x>',
            "open 创建": f'1 openat(AT_FDCWD</repo>, "{l}/x", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{self.p("d/testing/x")}>',
            "open 失败 ENOENT": f'1 openat(AT_FDCWD</repo>, "{l}/x", O_RDONLY) = -1 ENOENT (No such file or directory)',
            "open 失败 ENOTDIR": f'1 openat(AT_FDCWD</repo>, "{l}/x", O_RDONLY) = -1 ENOTDIR (Not a directory)',
            "stat 失败 ENOENT": f'1 newfstatat(AT_FDCWD</repo>, "{l}/x", 0x1, 0) = -1 ENOENT (No such file or directory)',
            "lstat 成功": f'1 newfstatat(AT_FDCWD</repo>, "{l}/x", {STAT_REG}, AT_SYMLINK_NOFOLLOW) = 0',
            "lstat 失败": f'1 lstat("{l}/x", 0x1) = -1 ENOENT (No such file or directory)',
            "readlink": f'1 readlink("{l}/x", "t", 4096) = 1',
            "mkdir 成功": f'1 mkdir("{l}/sub", 0700) = 0',
            "mkdir 失败": f'1 mkdir("{l}/sub", 0700) = -1 ENOENT (No such file or directory)',
            "unlink 成功": f'1 unlink("{l}/x") = 0',
            "unlink 失败": f'1 unlink("{l}/x") = -1 ENOENT (No such file or directory)',
            "symlink 创建": self.symlink_line("d/l/inner"),
            "rename 来源": f'1 rename("{l}/x", "{self.p("d/y")}") = 0',
            "rename 目的": f'1 rename("{self.p("d/f")}", "{l}/y") = 0',
            "rename 目的失败": f'1 rename("{self.p("d/f")}", "{l}/y") = -1 ENOENT (No such file or directory)',
            "link 目的": f'1 link("{self.p("d/f")}", "{l}/h") = 0',
            "execve": f'1 execve("{l}/bin/sh", ["sh"], 0x7ffd /* 3 vars */) = 0',
            "chdir": f'1 chdir("{l}/sub") = 0',
            "更深的一层": f'1 openat(AT_FDCWD</repo>, "{l}/a/b/c", O_RDONLY) = -1 ENOENT (No such file or directory)',
            "dirfd 相对": f'1 openat(5<{l}>, "x", O_RDONLY) = -1 ENOENT (No such file or directory)',
        }
        for label, line in cases.items():
            for text in ("/etc/passwd", "testing"):
                with self.subTest(label=label, text=text):
                    self.assertFailsClosed(self.with_link(text, line), where="tracker", fragment="符号链接")

    def test_the_fd_annotation_still_exposes_a_host_path_reached_through_a_link(self) -> None:
        """核对既有“返回描述符标注路径”检查在经链接时仍起作用——三道独立的防线，缺一道其余仍拦得住：

        1. 跟踪器的跟随式访问检查（本轮新增）：对链接名字的 open 会解引用，直接判问题；
        2. 解析器把 strace ``-y`` 标注的真实路径（``= 3</etc/passwd>``）登记成读访问，读集审计要求宿主路径有精确快照；
        3. 跟踪器原有的“返回描述符落在私有盘外”检查：把第 1 道关掉，它仍然拦住。
        """
        l = self.p("d/l")
        line = f'1 openat(AT_FDCWD</repo>, "{l}", O_RDONLY) = 3</etc/passwd>'
        document = self.assertFailsClosed(self.with_link("/etc/passwd", line), where="tracker", fragment="跟随")
        self.assertEqual(dict((item[0], item[1]) for item in document["accesses"])["/etc/passwd"], ["read"])
        report = self.audit(document)
        self.assertIn("/etc/passwd", [item["path"] for item in report["undeclared"]], "宿主路径没有声明快照：读集审计另行拦下")
        tracker_class = ra._runtime_module().OutputTrackerV4
        with mock.patch.object(tracker_class, "_symlink_name_verdict", lambda self_, event: "generic"), \
                mock.patch.object(tracker_class, "_through_symlink", lambda self_, path: False):
            document = self.document(self.with_link("/etc/passwd", line))
        self.assertTrue(any("文件描述符未绑定到私有临时盘" in text for text in document["runtime"]["outputs"]["problems"]),
                        "第 1 道防线关闭后，原有的描述符落点检查仍然拦住经链接读盘外文件")

    def test_a_link_pointing_outside_is_not_a_read_by_itself(self) -> None:
        lines = self.with_link("/etc/passwd", self.lstat_line("d/l"), self.readlink_line("d/l", text="/etc/passwd"), self.unlink_line("d/l"))
        document = self.assertPasses(lines)
        self.assertNotIn("/etc/passwd", dict((item[0], item[1]) for item in document["accesses"]),
                         "创建／lstat／readlink／删除一个指向宿主路径的链接，不构成对该宿主路径的任何访问")

    def test_name_reuse_after_unlink_is_an_ordinary_name_again(self) -> None:
        cases = {
            "目录": [self.mkdir_line("d/l"), self.create_line("d/l/x"), self.read_line("d/l/x")],
            "普通文件": [self.create_line("d/l"), self.read_line("d/l")],
            "新的链接": [self.symlink_line("d/l", "other"), self.lstat_line("d/l")],
        }
        for label, tail in cases.items():
            with self.subTest(label=label):
                lines = self.with_link("/etc/passwd", self.unlink_line("d/l"), *tail)
                self.assertPasses(lines)
        # 反过来：链接存活时同名重建仍是矛盾。
        self.assertFailsClosed(self.with_link("/etc/passwd", self.mkdir_line("d/l")), where="tracker")
        self.assertFailsClosed(self.with_link("/etc/passwd", self.create_line("d/l")), where="tracker")

    def test_a_directory_containing_a_link_moves_with_it_and_old_names_are_not_proven(self) -> None:
        lines = self.base_lines() + [self.mkdir_line("d/s"), self.symlink_line("d/s/l", "/etc/passwd"),
                                     f'1 rename("{self.p("d/s")}", "{self.p("d/n")}") = 0', self.lstat_line("d/n/l")]
        self.assertPasses(lines)
        self.assertPasses(lines + [self.unlink_line("d/n/l"), f'1 rmdir("{self.p("d/n")}") = 0'])
        self.assertFailsClosed(lines + [self.lstat_line("d/s/l")], where="tracker", fragment="首次读取前未证明")
        # 目录里还有链接就不能 rmdir（内核会得到 ENOTEMPTY，轨迹却声称成功）。
        self.assertFailsClosed(self.base_lines() + [self.mkdir_line("d/s"), self.symlink_line("d/s/l"), f'1 rmdir("{self.p("d/s")}") = 0'],
                               where="tracker", fragment="仍有已知子项")

    def test_a_link_made_through_a_different_dirfd_and_a_relative_symlink_call(self) -> None:
        lines = self.base_lines() + [f'1 symlinkat("testing", 5<{self.p("d")}>, "l") = 0',
                                     f'1 newfstatat(5<{self.p("d")}>, "l", {STAT_LNK}, AT_SYMLINK_NOFOLLOW) = 0',
                                     f'1 unlinkat(5<{self.p("d")}>, "l", 0) = 0']
        self.assertPasses(lines)

    def test_hardlinks_and_exchange_of_links_are_refused(self) -> None:
        l = self.p("d/l")
        self.assertFailsClosed(self.with_link("t", f'1 link("{l}", "{self.p("d/h")}") = 0'), where="tracker", fragment="不是普通文件")
        self.assertFailsClosed(self.with_link("t", f'1 renameat2(AT_FDCWD</repo>, "{self.p("d/f")}", AT_FDCWD</repo>, "{l}", RENAME_EXCHANGE) = 0'),
                               where="tracker", fragment="RENAME_EXCHANGE")
        # 普通文件被硬链接是照旧可以的（对照）。
        self.assertPasses(self.with_link("t", f'1 link("{self.p("d/f")}", "{self.p("d/h")}") = 0'))


# =====================================================================================================================
# 跟踪器侧：状态机与放行／拒绝全表（直接喂事件）
# =====================================================================================================================


def run_events(*groups: list[dict[str, Any]]) -> tuple[runtime.OutputTrackerV4, list[bool]]:
    return tracker_tests.run(*groups)


class SymlinkTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.b = Builder()

    def link_tracker(self, text: str = "testing") -> runtime.OutputTrackerV4:
        b = self.b
        return run_events(b.mkdir(D), b.create(D + "/f"), b.symlink(D + "/l", text))[0]

    def problems(self, *groups: list[dict[str, Any]]) -> list[str]:
        return run_events(*groups)[0].report()["problems"]

    def test_creation_registers_an_object_with_the_text_and_changes_the_namespace(self) -> None:
        b = self.b
        tracker, changed = run_events(b.mkdir(D), b.symlink(D + "/l", "../x"), b.symlink(D + "/m", "/etc/passwd", name="symlink"))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(changed, [True, True, True])
        obj = tracker.names[D + "/l"]
        self.assertEqual((tracker.kinds[obj], tracker.link_text[obj], tracker.links[obj]), ("symlink", "../x", 1))
        self.assertEqual(sorted(tracker.link_text.values()), ["../x", "/etc/passwd"])
        self.assertEqual(tracker.entries[D], 2)
        self.assertTrue(tracker.has_symlinks() and tracker.is_symlink(D + "/l") and not tracker.is_symlink(D))

    def test_failed_creation_changes_nothing(self) -> None:
        b = self.b
        cases = [("EEXIST", D + "/f", False), ("EEXIST", D + "/l", False), ("ENOENT", D + "/nodir/x", False), ("ENOTDIR", D + "/f/x", False),
                 ("EACCES", D + "/x", True), ("EROFS", D + "/x", True), ("ENOSPC", D + "/x", True), ("EPERM", D + "/x", True),
                 ("ENAMETOOLONG", D + "/x", True), ("EEXIST", D + "/never", True)]
        for errno, path, is_problem in cases:
            with self.subTest(errno=errno, path=path.rsplit("/", 1)[-1]):
                tracker, changed = run_events(b.mkdir(D), b.create(D + "/f"), b.symlink(D + "/l", "keep"),
                                              b.symlink(path, "new", ok=False, errno=errno))
                self.assertEqual(changed[-1], False)
                self.assertEqual(bool(tracker.report()["problems"]), is_problem, tracker.report()["problems"])
                self.assertEqual(sorted(tracker.link_text.values()), ["keep"], "失败的创建不新增对象，也不改已有链接的文本")
                self.assertNotEqual(tracker.names.get(path), None if path in tracker.names else -1)
                if path not in {D + "/f", D + "/l"}:
                    self.assertNotIn(path, tracker.names)

    def test_creation_contradictions(self) -> None:
        b = self.b
        self.assertTrue(any("已知名字" in p for p in self.problems(b.mkdir(D), b.create(D + "/f"), b.symlink(D + "/f", "x"))))
        self.assertTrue(any("已知名字" in p for p in self.problems(b.mkdir(D), b.symlink(D + "/l", "x"), b.symlink(D + "/l", "x"))))
        self.assertTrue(any("父目录未证明" in p for p in self.problems(b.mkdir(D), b.symlink(D + "/a/b", "x"))))
        self.assertTrue(any("父目录未证明" in p for p in self.problems(b.mkdir(D), b.create(D + "/f"), b.symlink(D + "/f/b", "x"))))
        self.assertTrue(any("链接文本" in p for p in self.problems(b.mkdir(D), b.symlink(D + "/l", None))))
        self.assertTrue(any("链接文本" in p for p in self.problems(b.mkdir(D), b.symlink(D + "/l", ""))))
        self.assertTrue(any("链接文本" in p for p in self.problems(b.mkdir(D), [b.event("symlink", D + "/l", target=5)])))

    def test_unlink_removes_the_object_and_frees_the_name(self) -> None:
        b = self.b
        for name in ("unlink", "unlinkat"):
            with self.subTest(name=name):
                tracker, changed = run_events(b.mkdir(D), b.symlink(D + "/l"), b.unlink(D + "/l", name=name))
                self.assertEqual((tracker.report()["problems"], changed[-1]), ([], True))
                self.assertEqual((tracker.link_text, tracker.entries[D], D + "/l" in tracker.names), ({}, 0, False))
        tracker, _ = run_events(b.mkdir(D), b.symlink(D + "/l"), b.unlink(D + "/l"), b.mkdir(D + "/l"), b.create(D + "/l/x"))
        self.assertEqual(tracker.report()["problems"], [])

    def test_unlink_is_judged_by_the_kind_and_the_at_removedir_flag(self) -> None:
        b = self.b
        base = [b.mkdir(D), b.symlink(D + "/l"), b.mkdir(D + "/e"), b.create(D + "/f")]
        for label, event in {
            "rmdir 删链接": b.unlink(D + "/l", name="rmdir"),
            "unlinkat 带 AT_REMOVEDIR 删链接": [b.event("unlinkat", D + "/l", flags=("AT_REMOVEDIR",))],
            "unlinkat 带 AT_REMOVEDIR 删文件": [b.event("unlinkat", D + "/f", flags=("AT_REMOVEDIR",))],
            "unlink 删目录": b.unlink(D + "/e"),
        }.items():
            with self.subTest(label=label):
                self.assertTrue(self.problems(*base, event), label)
        self.assertEqual(self.problems(*base, [b.event("unlinkat", D + "/e", flags=("AT_REMOVEDIR",))]), [])
        self.assertEqual(self.problems(*base, [b.event("unlinkat", D + "/e")]), [], "不带 AT_REMOVEDIR 删目录：旧宽容保持（既有事件流不带标志）")
        self.assertEqual(self.problems(*base, [b.event("unlinkat", D + "/l", flags=())]), [])

    def test_rename_moves_the_object_and_overwrites_by_the_non_directory_rule(self) -> None:
        b = self.b
        base = [b.mkdir(D), b.create(D + "/f"), b.symlink(D + "/l", "txt"), b.mkdir(D + "/e")]
        tracker, changed = run_events(*base, b.rename(D + "/l", D + "/m"), b.lstat(D + "/m"))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(list(tracker.link_text.values()), ["txt"])
        self.assertNotIn(D + "/l", tracker.names)
        self.assertTrue(tracker.is_symlink(D + "/m"))
        # 链接覆盖普通文件、普通文件覆盖链接：两个都是非目录，POSIX 允许。
        tracker, _ = run_events(*base, b.rename(D + "/l", D + "/f"), b.lstat(D + "/f"))
        self.assertEqual((tracker.report()["problems"], tracker.is_symlink(D + "/f"), D + "/l" in tracker.names), ([], True, False))
        tracker, _ = run_events(*base, b.rename(D + "/f", D + "/l"), b.read(D + "/l"))
        self.assertEqual((tracker.report()["problems"], tracker.link_text, tracker.kinds[tracker.names[D + "/l"]]), ([], {}, "file"))
        # 链接覆盖链接。
        tracker, _ = run_events(*base, b.symlink(D + "/l2", "second"), b.rename(D + "/l", D + "/l2"))
        self.assertEqual((tracker.report()["problems"], list(tracker.link_text.values()), tracker.entries[D]), ([], ["txt"], 3))
        # 目录与非目录互相覆盖不行。
        self.assertTrue(self.problems(*base, b.rename(D + "/l", D + "/e")))
        self.assertTrue(self.problems(*base, b.rename(D + "/e", D + "/l")))
        # NOREPLACE 成功却目的名字已知：矛盾；目的名字空着则照常移动。
        self.assertTrue(self.problems(*base, b.rename(D + "/l", D + "/f", name="renameat2", flags=("RENAME_NOREPLACE",))))
        self.assertEqual(self.problems(*base, b.rename(D + "/l", D + "/n", name="renameat2", flags=("RENAME_NOREPLACE",))), [])

    def test_rename_out_of_the_tree_exchange_and_hardlink_are_refused(self) -> None:
        b = self.b
        base = [b.mkdir(D), b.create(D + "/f"), b.symlink(D + "/l")]
        self.assertTrue(any("另一端不在私有临时盘内" in p for p in self.problems(*base, [
            b.event("rename", D + "/l", role="source", peer="/host/x")])))
        self.assertTrue(any("另一端不在私有临时盘内" in p for p in self.problems(*base, [
            b.event("rename", D + "/l", role="destination", peer="/host/x")])))
        for flag in ("RENAME_EXCHANGE", "RENAME_WHITEOUT"):
            self.assertTrue(any("RENAME_EXCHANGE" in p for p in self.problems(*base, b.rename(
                D + "/l", D + "/f", name="renameat2", flags=(flag,)))))
        self.assertTrue(any("不是普通文件" in p for p in self.problems(*base, b.link(D + "/l", D + "/h"))))
        self.assertTrue(any("不是普通文件" in p for p in self.problems(*base, b.link(D + "/l", D + "/h", name="linkat"))))

    def test_directory_with_a_link_inside(self) -> None:
        b = self.b
        base = [b.mkdir(D), b.mkdir(D + "/s"), b.symlink(D + "/s/l", "t")]
        self.assertTrue(any("仍有已知子项" in p for p in self.problems(*base, b.unlink(D + "/s", name="rmdir"))))
        tracker, _ = run_events(*base, b.rename(D + "/s", D + "/n"), b.lstat(D + "/n/l"), b.unlink(D + "/n/l"), b.unlink(D + "/n", name="rmdir"))
        self.assertEqual((tracker.report()["problems"], tracker.link_text), ([], {}))
        self.assertTrue(self.problems(*base, b.rename(D + "/s", D + "/n"), b.lstat(D + "/s/l")))

    def test_allowed_and_refused_calls_on_the_link_name(self) -> None:
        """放行／拒绝全表（单事件）：每一行都是一个明确的合同。"""
        b = self.b
        link = D + "/l"
        e = b.event
        table = [
            # (标签, 事件, 是否应报问题)
            ("lstat", b.lstat(link, "lstat")[0], False), ("lstat64", b.lstat(link, "lstat64")[0], False),
            ("newfstatat NOFOLLOW", b.lstat(link, "newfstatat")[0], False), ("fstatat64 NOFOLLOW", b.lstat(link, "fstatat64")[0], False),
            ("statx NOFOLLOW", b.lstat(link, "statx")[0], False),
            ("readlink", b.readlink(link, "readlink")[0], False), ("readlinkat", b.readlink(link)[0], False),
            ("mkdir EEXIST", b.mkdir(link, ok=False, errno="EEXIST")[0], False),
            ("mkdirat EEXIST", e("mkdirat", link, ok=False, errno="EEXIST"), False),
            ("mknod EEXIST", e("mknod", link, ok=False, errno="EEXIST"), False),
            ("mknodat EEXIST", e("mknodat", link, ok=False, errno="EEXIST"), False),
            ("symlink EEXIST", b.symlink(link, "x", ok=False, errno="EEXIST")[0], False),
            ("open O_EXCL EEXIST", b.create(link, ok=False, errno="EEXIST")[0], False),
            ("openat2 O_EXCL EEXIST", e("openat2", link, flags=("O_WRONLY", "O_CREAT", "O_EXCL"), ok=False, errno="EEXIST"), False),
            ("open NOFOLLOW ELOOP", e("openat", link, kind="stat", flags=("O_RDONLY", "O_NOFOLLOW"), ok=False, errno="ELOOP"), False),
            ("open NOFOLLOW|CREAT ELOOP", e("openat", link, flags=("O_WRONLY", "O_CREAT", "O_NOFOLLOW"), ok=False, errno="ELOOP"), False),
            ("unlink", b.unlink(link)[0], False), ("unlinkat", b.unlink(link, name="unlinkat")[0], False),
            # ---- 拒绝 ----
            ("stat 跟随", b.stat(link)[0], True), ("stat 旧写法", e("stat", link, kind="stat"), True),
            ("stat64", e("stat64", link, kind="stat"), True),
            ("statx 无 NOFOLLOW", e("statx", link, kind="stat", flags=("AT_STATX_SYNC_AS_STAT",)), True),
            ("statx 只有 SYNC 标志", e("statx", link, kind="stat", flags=("AT_STATX_DONT_SYNC",)), True),
            ("lstat 失败", b.lstat(link, "lstat", ok=False, errno="EACCES")[0], True),
            ("lstat 缺失", b.lstat(link, "newfstatat", ok=False, errno="ENOENT")[0], True),
            ("lstat 事件类别不对", e("lstat", link, kind="read"), True),
            ("readlink 失败", b.readlink(link, ok=False, errno="EINVAL")[0], True),
            ("readlink 缺失", b.readlink(link, ok=False, errno="ENOENT")[0], True),
            ("open 读", b.read(link)[0], True), ("open 读 O_NOFOLLOW 成功", e("openat", link, kind="read", flags=("O_RDONLY", "O_NOFOLLOW"), target=link), True),
            ("open ELOOP 无 NOFOLLOW", e("openat", link, kind="stat", flags=("O_RDONLY",), ok=False, errno="ELOOP"), True),
            ("open NOFOLLOW ENOENT", e("openat", link, kind="missing", flags=("O_RDONLY", "O_NOFOLLOW"), ok=False, errno="ENOENT"), True),
            ("open NOFOLLOW ENOTDIR", e("openat", link, kind="missing", flags=("O_RDONLY", "O_NOFOLLOW", "O_DIRECTORY"), ok=False, errno="ENOTDIR"), True),
            ("open O_PATH NOFOLLOW 成功", e("openat", link, kind="read", flags=("O_RDONLY", "O_NOFOLLOW", "O_PATH"), target=link), True),
            ("open O_PATH NOFOLLOW ELOOP", e("openat", link, kind="stat", flags=("O_RDONLY", "O_NOFOLLOW", "O_PATH"), ok=False, errno="ELOOP"), True),
            ("open 写 CREAT", e("openat", link, flags=("O_WRONLY", "O_CREAT"), target=D + "/elsewhere"), True),
            ("open 无 EXCL 的 EEXIST", e("openat", link, flags=("O_WRONLY", "O_CREAT"), ok=False, errno="EEXIST"), True),
            ("open 只有 EXCL 的 EEXIST", e("openat", link, flags=("O_WRONLY", "O_EXCL"), ok=False, errno="EEXIST"), True),
            ("creat", e("creat", link, target=D + "/elsewhere"), True),
            ("creat EEXIST", e("creat", link, ok=False, errno="EEXIST"), True),
            ("open 目录", e("openat", link, kind="dir", flags=("O_RDONLY", "O_DIRECTORY"), target=D), True),
            ("openat2 读", e("openat2", link, kind="read", flags=("O_RDONLY",), target=D + "/f"), True),
            ("access", e("access", link, kind="stat"), True), ("faccessat2", e("faccessat2", link, kind="stat", flags=("AT_SYMLINK_NOFOLLOW",)), True),
            ("chdir", e("chdir", link, kind="dir"), True), ("execve", e("execve", link, kind="exec"), True),
            ("execveat", e("execveat", link, kind="exec", flags=("AT_SYMLINK_NOFOLLOW",)), True),
            ("getdents64", e("getdents64", link, kind="list"), True),
            ("chmod", e("chmod", link), True), ("fchmodat", e("fchmodat", link, flags=("AT_SYMLINK_NOFOLLOW",)), True),
            ("lchown", e("lchown", link), True), ("fchownat NOFOLLOW", e("fchownat", link, flags=("AT_SYMLINK_NOFOLLOW",)), True),
            ("utimensat", e("utimensat", link), True), ("utimensat NOFOLLOW", e("utimensat", link, flags=("AT_SYMLINK_NOFOLLOW",)), True),
            ("truncate", e("truncate", link), True), ("setxattr", e("setxattr", link), True), ("statfs", e("statfs", link, kind="stat"), True),
            ("getxattr", e("getxattr", link, kind="stat"), True), ("inotify_add_watch", e("inotify_add_watch", link, kind="stat"), True),
            ("mkdir 成功", b.mkdir(link)[0], True), ("mknod 成功", e("mknod", link), True), ("symlink 成功", b.symlink(link, "x")[0], True),
            ("mkdir ENOENT", b.mkdir(link, ok=False, errno="ENOENT")[0], True), ("mkdir EACCES", b.mkdir(link, ok=False, errno="EACCES")[0], True),
            ("rmdir 成功", b.unlink(link, name="rmdir")[0], True), ("rmdir ENOTDIR", b.unlink(link, name="rmdir", ok=False, errno="ENOTDIR")[0], True),
            ("unlink 失败 EACCES", b.unlink(link, ok=False, errno="EACCES")[0], True),
            ("unlink 失败 ENOENT", b.unlink(link, ok=False, errno="ENOENT")[0], True),
            ("unlinkat REMOVEDIR", e("unlinkat", link, flags=("AT_REMOVEDIR",)), True),
            ("未知调用名", e("some_new_syscall", link, kind="stat"), True),
        ]
        for label, event, expected in table:
            with self.subTest(label=label):
                tracker, changed = run_events(b.mkdir(D), b.create(D + "/f"), b.symlink(link, "testing"), [dict(event)])
                self.assertEqual(bool(tracker.report()["problems"]), expected, (label, tracker.report()["problems"]))
                if label not in {"unlink", "unlinkat"}:
                    self.assertFalse(changed[-1], "放行或拒绝都不改名字表（unlink 除外）")

    def test_accesses_through_a_link_are_refused_for_every_event_shape(self) -> None:
        b = self.b
        through = D + "/l/x"
        e = b.event
        shapes = [
            ("读", b.read(through)), ("读失败", b.probe(through)), ("probe ENOTDIR", b.probe(through, "ENOTDIR", "openat")),
            ("stat 成功", b.stat(through)), ("lstat 成功", b.lstat(through)), ("lstat 失败", b.lstat(through, ok=False, errno="ENOENT")),
            ("readlink", b.readlink(through)), ("创建", b.create(through)), ("创建失败 ENOENT", b.create(through, ok=False, errno="ENOENT")),
            ("创建失败 EEXIST", b.create(through, ok=False, errno="EEXIST")), ("mkdir", b.mkdir(through)),
            ("mkdir 失败", b.mkdir(through, ok=False, errno="ENOENT")), ("unlink", b.unlink(through)),
            ("unlink 失败", b.unlink(through, ok=False, errno="ENOENT")), ("symlink", b.symlink(through, "t")),
            ("chmod", [e("chmod", through)]), ("execve", [e("execve", through, kind="exec")]), ("getdents", [e("getdents64", through, kind="list")]),
            ("目录项", [e("openat", through, kind="dir", flags=("O_RDONLY", "O_DIRECTORY"), target=through)]),
            ("rename 来源", b.rename(through, D + "/y")), ("rename 目的", b.rename(D + "/f", through)),
            ("rename 目的失败", b.rename(D + "/f", through, ok=False, errno="ENOENT")),
            ("link 目的", b.link(D + "/f", through)), ("link 目的失败", b.link(D + "/f", through, ok=False, errno="EEXIST")),
            ("更深", b.read(D + "/l/a/b/c")),
        ]
        for label, group in shapes:
            with self.subTest(label=label):
                tracker, changed = run_events(b.mkdir(D), b.create(D + "/f"), b.symlink(D + "/l"), group)
                self.assertTrue(any("中间分量是符号链接" in p for p in tracker.report()["problems"]), (label, tracker.report()["problems"]))
                self.assertNotIn(through, tracker.names, "拒绝的事件不改名字表")
                self.assertEqual(changed[-1] and False, False)

    def test_only_proper_ancestors_count_and_a_deleted_link_is_no_ancestor(self) -> None:
        b = self.b
        self.assertEqual(self.problems(b.mkdir(D), b.mkdir(D + "/l2"), b.symlink(D + "/l"), b.create(D + "/l2/x"), b.read(D + "/l2/x")), [],
                         "名字前缀相同（l 与 l2）但不是祖先")
        self.assertEqual(self.problems(b.mkdir(D), b.symlink(D + "/l"), b.unlink(D + "/l"), b.mkdir(D + "/l"), b.create(D + "/l/x")), [])
        tracker, _ = run_events(b.mkdir(D), b.symlink(D), [])      # 链接不能建在已知目录名上
        self.assertTrue(tracker.report()["problems"])
        # 根下一层的链接：祖先检查要走到根为止。
        tracker, _ = run_events(b.symlink(ROOT + "/top"), b.read(ROOT + "/top/x"))
        self.assertEqual(sorted(tracker.link_text.values()), ["testing"])
        self.assertTrue(any("中间分量是符号链接" in p for p in tracker.report()["problems"]))

    def test_the_changed_flag_for_every_symlink_event_kind(self) -> None:
        b = self.b
        link = D + "/l"
        creation, lstat, readlink, unlink = b.symlink(link)[0], b.lstat(link)[0], b.readlink(link)[0], b.unlink(link)[0]
        follow = b.stat(link)[0]
        tracker = runtime.OutputTrackerV4(CONTRACT)
        for event, expected in [(b.mkdir(D)[0], True), (creation, True), (lstat, False), (readlink, False), (follow, False),
                                (b.symlink(link, ok=False, errno="EEXIST")[0], False), (unlink, True)]:
            with self.subTest(name=event["name"], path=event["path"].rsplit("/", 1)[-1]):
                self.assertEqual(tracker.observe_event(event), expected)

    def test_openat2_is_an_open_call_like_openat(self) -> None:
        """R3-4①：openat2 在 OPEN_NAMES 里——创建、返回描述符落点检查、读取都和 openat 一致。"""
        b = self.b
        self.assertIn("openat2", runtime.OPEN_NAMES)
        create = b.event("openat2", D + "/n", flags=("O_WRONLY", "O_CREAT", "O_EXCL"), target=D + "/n")
        read = b.event("openat2", D + "/n", kind="read", flags=("O_RDONLY",), target=D + "/n")
        tracker, changed = run_events(b.mkdir(D), [create], [read])
        self.assertEqual((tracker.report()["problems"], changed), ([], [True, True, False]))
        # 返回描述符落在私有盘外 / 没有标注：原先被整个跳过，现在判问题。
        for target in ("/host/data", None):
            event = b.event("openat2", D + "/n", flags=("O_WRONLY", "O_CREAT", "O_EXCL"), target=target)
            self.assertTrue(any("文件描述符未绑定" in p for p in self.problems(b.mkdir(D), [event])), target)
        outside_read = b.event("openat2", D + "/n", kind="read", flags=("O_RDONLY",), target="/etc/passwd")
        self.assertTrue(any("文件描述符未绑定" in p for p in self.problems(b.mkdir(D), [create], [outside_read])))
        # 写而没有创建证明；O_EXCL 成功落在已知名字上的矛盾；失败的创建按错误码判。
        self.assertTrue(any("缺少创建证明" in p for p in self.problems(b.mkdir(D), [b.event("openat2", D + "/w", flags=("O_WRONLY",), target=D + "/w")])))
        # 紧挨着创建事件的那一条同路径 O_EXCL 成功会被豁免（真实解析器一次调用产两条事件）；隔了别的事件就是矛盾。
        self.assertTrue(any("O_CREAT|O_EXCL" in p for p in self.problems(b.mkdir(D), [create], [read], [b.event(
            "openat2", D + "/n", flags=("O_WRONLY", "O_CREAT", "O_EXCL"), target=D + "/n")])))
        self.assertEqual(self.problems(b.mkdir(D), [create], [b.event("openat2", D + "/n", flags=("O_WRONLY", "O_CREAT", "O_EXCL"),
                                                                       ok=False, errno="EEXIST")]), [])
        self.assertTrue(self.problems(b.mkdir(D), [b.event("openat2", D + "/n", flags=("O_WRONLY", "O_CREAT"), ok=False, errno="EACCES")]))
        # 未知名字读：和 openat 一样判“首次读取前未证明”。
        self.assertTrue(any("首次读取前未证明" in p for p in self.problems(b.mkdir(D), [read])))

    def test_unknown_calls_on_proven_non_link_names_keep_their_old_verdicts(self) -> None:
        """符号链接检查不改变普通名字上的既有判定（零回归）：同样的事件序列没有链接时结论不变。"""
        b = self.b
        for event, expected in [(b.event("openat2", D + "/f"), True), (b.event("mknod", D + "/f"), True), (b.event("chmod", D + "/f"), False),
                                (b.lstat(D + "/f")[0], False), (b.stat(D + "/f")[0], False), (b.readlink(D + "/f", ok=False, errno="EINVAL")[0], False)]:
            self.assertEqual(bool(self.problems(b.mkdir(D), b.create(D + "/f"), [event])), expected, event["name"])


# =====================================================================================================================
# 重放：篡改任何一处都被拒绝
# =====================================================================================================================


class ReplayTamperTests(R3Fixture):
    def good(self) -> dict[str, Any]:
        lines = self.base_lines() + [self.symlink_line("d/l", "/etc/passwd"), self.lstat_line("d/l"), self.readlink_line("d/l"),
                                     self.unlink_line("d/l")]
        return self.assertPasses(lines)

    def tampered(self, mutate) -> dict[str, Any]:
        document = copy.deepcopy(self.good())
        mutate(document["runtime"]["output_events"])
        return document

    def test_the_untouched_document_replays(self) -> None:
        self.assertIsNotNone(runtime.replay_runtime(self.good(), [self.entry]))

    def test_tampering_with_the_events_is_rejected(self) -> None:
        def event(events, name):
            return next(item for item in events if item["name"] == name)

        def drop_nofollow(events):          # lstat 变成 stat：跟随
            event(events, "newfstatat")["flags"] = []

        def drop_creation(events):           # 链接来历不明
            events[:] = [item for item in events if item["name"] != "symlinkat"]

        def drop_text(events):
            event(events, "symlinkat")["target"] = None

        def empty_text(events):
            event(events, "symlinkat")["target"] = ""

        def unknown_flag(events):
            event(events, "newfstatat")["flags"] = ["AT_FOO"]

        def lstat_becomes_open(events):
            event(events, "newfstatat").update(name="openat", kind="read", flags=["O_RDONLY"], target="/etc/passwd")

        def path_through(events):
            event(events, "readlinkat")["path"] += "/x"

        def readlink_fails(events):
            item = event(events, "readlinkat")
            item.update(succeeded=False, errno="EINVAL")

        def lstat_fails(events):
            item = event(events, "newfstatat")
            item.update(succeeded=False, errno="EACCES")

        def create_has_peer(events):
            event(events, "symlinkat")["peer"] = "/host/x"

        def flag_not_sorted(events):
            event(events, "newfstatat")["flags"] = ["AT_SYMLINK_NOFOLLOW", "AT_EMPTY_PATH"]

        for mutate in (drop_nofollow, drop_creation, drop_text, empty_text, unknown_flag, lstat_becomes_open, path_through,
                       readlink_fails, lstat_fails, create_has_peer, flag_not_sorted):
            with self.subTest(mutation=mutate.__name__):
                self.assertRejected(self.tampered(mutate), mutate.__name__)

    def test_a_forged_clean_summary_cannot_hide_a_following_access(self) -> None:
        l = self.p("d/l")
        document = self.document(self.base_lines() + [self.symlink_line("d/l", "/etc/passwd"),
                                                       f'1 newfstatat(AT_FDCWD</repo>, "{l}", {STAT_REG}, 0) = 0'])
        self.assertTrue(document["runtime"]["outputs"]["problems"])
        document["runtime"]["outputs"]["problems"] = []
        self.assertRejected(document)

    def test_a_v4_event_with_an_unknown_at_flag_is_rejected_by_the_replay(self) -> None:
        document = copy.deepcopy(self.good())
        document["runtime"]["output_events"][0]["flags"] = ["AT_FDCWD"]
        self.assertRejected(document)


# =====================================================================================================================
# 与独立语义模型的差分对拍：该拒的绝不放行，该放的绝不拒
# =====================================================================================================================


class DifferentialOracleTests(unittest.TestCase):
    """独立的“内核语义”模型 ``_SimFs`` 逐步生成合法历史，并在每一步随机注入一个针对符号链接的事件；
    注入事件是否应被拒绝由**下面这张独立编码的政策表**决定（不看跟踪器的任何实现），再与跟踪器的实际反应逐个比对：

    * 政策说“会解引用或没有合同”→ 跟踪器必须报问题（在历史状态的深拷贝上试，不污染主流程）；
    * 政策说“不跟随”→ 跟踪器不得报新问题。

    任何一处放宽过头（把某个跟随式调用放进白名单、忘了检查中间分量……）或收得过紧，这里都会逐例对不上；
    变异检验见交付报告。
    """

    FINAL_REFUSED = "refused"          # 对链接名字本身的跟随式访问，或没有合同的调用
    FINAL_ALLOWED = "allowed"          # 不跟随，且不改名字表

    @staticmethod
    def final_cases(b: Builder, link: str, other_file: str, new_name: str) -> list[tuple[str, str, list[dict[str, Any]]]]:
        e = b.event
        refused, allowed = DifferentialOracleTests.FINAL_REFUSED, DifferentialOracleTests.FINAL_ALLOWED
        return [
            (allowed, "lstat", b.lstat(link, "lstat")), (allowed, "lstat64", b.lstat(link, "lstat64")),
            (allowed, "newfstatat nofollow", b.lstat(link, "newfstatat")), (allowed, "fstatat64 nofollow", b.lstat(link, "fstatat64")),
            (allowed, "statx nofollow", b.lstat(link, "statx")), (allowed, "readlink", b.readlink(link, "readlink")),
            (allowed, "readlinkat", b.readlink(link, "readlinkat")),
            (allowed, "mkdir EEXIST", b.mkdir(link, ok=False, errno="EEXIST")),
            (allowed, "symlink EEXIST", b.symlink(link, "t", ok=False, errno="EEXIST")),
            (allowed, "symlinkat EEXIST", b.symlink(link, "t", name="symlinkat", ok=False, errno="EEXIST")),
            (allowed, "open EXCL EEXIST", b.create(link, ok=False, errno="EEXIST")),
            (allowed, "open NOFOLLOW ELOOP", [e("openat", link, kind="stat", flags=("O_RDONLY", "O_NOFOLLOW"), ok=False, errno="ELOOP")]),
            (refused, "stat", b.stat(link)), (refused, "stat ENOENT", b.probe(link, "ENOENT")),
            (refused, "open 读", b.read(link)), (refused, "open ENOENT", b.probe(link, "ENOENT", "openat")),
            (refused, "open 写", [e("openat", link, flags=("O_WRONLY", "O_CREAT"), target=other_file)]),
            (refused, "open 目录", [e("openat", link, kind="dir", flags=("O_RDONLY", "O_DIRECTORY"), target=other_file)]),
            (refused, "open O_PATH", [e("openat", link, kind="read", flags=("O_RDONLY", "O_NOFOLLOW", "O_PATH"), target=link)]),
            (refused, "openat2 读", [e("openat2", link, kind="read", flags=("O_RDONLY",), target=other_file)]),
            (refused, "creat", [e("creat", link, target=other_file)]),
            (refused, "access", [e("access", link, kind="stat")]), (refused, "chdir", [e("chdir", link, kind="dir")]),
            (refused, "execve", [e("execve", link, kind="exec")]), (refused, "chmod", [e("chmod", link)]),
            (refused, "utimensat", [e("utimensat", link)]), (refused, "truncate", [e("truncate", link)]),
            (refused, "getxattr", [e("getxattr", link, kind="stat")]), (refused, "statx 跟随", [e("statx", link, kind="stat", flags=("AT_STATX_SYNC_AS_STAT",))]),
            (refused, "mkdir 成功", b.mkdir(link)), (refused, "rmdir", b.unlink(link, name="rmdir")),
            (refused, "unlinkat REMOVEDIR", [e("unlinkat", link, flags=("AT_REMOVEDIR",))]),
            (refused, "硬链接来源", b.link(link, new_name)),
            (refused, "exchange", b.rename(link, other_file, name="renameat2", flags=("RENAME_EXCHANGE",))),
            (refused, "移到树外", [e("rename", link, role="source", peer="/host/x")]),
            (refused, "readlink 失败", b.readlink(link, ok=False, errno="EINVAL")),
        ]

    @staticmethod
    def through_cases(b: Builder, through: str, other_file: str) -> list[list[dict[str, Any]]]:
        e = b.event
        return [b.read(through), b.probe(through), b.stat(through), b.lstat(through), b.readlink(through), b.create(through),
                b.create(through, ok=False, errno="ENOENT"), b.mkdir(through), b.mkdir(through, ok=False, errno="ENOENT"),
                b.unlink(through), b.unlink(through, ok=False, errno="ENOENT"), b.symlink(through, "t"),
                b.rename(through, other_file + "_x"), b.rename(other_file, through), b.link(other_file, through),
                [e("chmod", through)], [e("execve", through, kind="exec")], [e("chdir", through, kind="dir")]]

    def run_history(self, seed: int, steps: int, stats: dict[str, int]) -> None:
        rng = random.Random(30000 + seed)
        sim = tracker_tests.TrackerSimulationTests("test_random_valid_histories_match_the_real_filesystem_semantics")
        fs, builder = _SimFs(ROOT), Builder()
        tracker = runtime.OutputTrackerV4(CONTRACT)
        counters = {key: 0 for key in ("symlink_ok", "symlink_failed", "lstat_link", "readlink_ok", "unlink_link", "rename_link",
                                       "rename_over_link", "failed", "rename_ok", "link_ok", "overwrite", "dir_move", "noop")}
        for step in range(steps):
            events = sim.apply_random_operation(rng, fs, builder, counters)
            for event in events:
                tracker.observe_event(event)
            context = f"seed={seed} step={step}"
            self.assertEqual(tracker.report()["problems"], [], context)
            links = [path for path, kind in fs.kinds().items() if kind == "symlink"]
            files = [path for path, kind in fs.kinds().items() if kind == "file"]
            if not links or rng.random() > 0.7:
                continue
            link = rng.choice(links)
            other = rng.choice(files) if files else D + "/f"
            fresh = ROOT + "/d1/fresh" + str(step)
            for expectation, label, group in self.final_cases(builder, link, other, fresh):
                trial = copy.deepcopy(tracker)
                before = len(trial.problems)
                changed = [trial.observe_event(copy.deepcopy(event)) for event in group]
                reported = len(trial.problems) > before
                self.assertEqual(reported, expectation == self.FINAL_REFUSED, f"{context} {label} link={link} problems={sorted(trial.problems)}")
                self.assertEqual(any(changed), False, f"{context} {label}：放行或拒绝都不改名字表")
                stats["final_refused" if expectation == self.FINAL_REFUSED else "final_allowed"] += 1
            # 中间分量是链接的访问：不论是什么调用都必须拒绝。
            if rng.random() < 0.5:
                through = link + "/x"
                for group in self.through_cases(builder, through, other):
                    trial = copy.deepcopy(tracker)
                    before = len(trial.problems)
                    for event in group:
                        trial.observe_event(copy.deepcopy(event))
                    self.assertGreater(len(trial.problems), before, f"{context} through {group[0]['name']} {through}")
                    stats["through"] += 1
            # 对链接的祖先目录和普通名字做同样的事，不应出问题（防止“拒绝过头”）。
            if rng.random() < 0.3:
                parent = link.rsplit("/", 1)[0]
                trial = copy.deepcopy(tracker)
                before = len(trial.problems)
                for event in builder.stat(parent) + builder.lstat(parent):
                    trial.observe_event(event)
                self.assertEqual(len(trial.problems), before, f"{context} 对链接所在目录的 stat 不应被拒")
                stats["parent_ok"] += 1
        stats["symlink_ok"] += counters["symlink_ok"]

    def test_policy_table_and_tracker_agree_on_random_histories(self) -> None:
        stats = {"final_refused": 0, "final_allowed": 0, "through": 0, "parent_ok": 0, "symlink_ok": 0}
        for seed in range(30):
            self.run_history(seed, 300, stats)
        self.assertGreater(stats["final_refused"], 2000, stats)
        self.assertGreater(stats["final_allowed"], 1000, stats)
        self.assertGreater(stats["through"], 500, stats)
        self.assertGreater(stats["parent_ok"], 100, stats)

    def test_the_oracle_itself_detects_a_tracker_that_lets_a_following_call_through(self) -> None:
        """对拍必须“放宽前后都能发现该拒的被放行”：把跟踪器改成放行 chdir／放行经链接的访问，上面的对拍必须失败。"""
        original_verdict = runtime.OutputTrackerV4._symlink_name_verdict
        original_through = runtime.OutputTrackerV4._through_symlink

        def lenient_verdict(self_, event):
            return "ok" if event["name"] == "chdir" else original_verdict(self_, event)

        for label, patcher in (("放行 chdir", mock.patch.object(runtime.OutputTrackerV4, "_symlink_name_verdict", lenient_verdict)),
                               ("不查中间分量", mock.patch.object(runtime.OutputTrackerV4, "_through_symlink", lambda self_, path: False))):
            with self.subTest(label=label), patcher:
                with self.assertRaises(AssertionError):
                    self.run_history(0, 300, {"final_refused": 0, "final_allowed": 0, "through": 0, "parent_ok": 0, "symlink_ok": 0})
        self.assertIs(runtime.OutputTrackerV4._symlink_name_verdict, original_verdict)
        self.assertIs(runtime.OutputTrackerV4._through_symlink, original_through)


# =====================================================================================================================
# v3 与默认路径不变
# =====================================================================================================================


class V3UnchangedTests(R3Fixture):
    def test_v3_still_refuses_creation_and_has_no_new_state(self) -> None:
        lines = self.base_lines() + [self.symlink_line("d/l"), self.unlink_line("d/l")]
        document = self.document(lines, contract="v3")
        self.assertTrue(any("链接或移动操作尚无输出归属证明" in text for text in document["runtime"]["outputs"]["problems"]))
        self.assertTrue(all(set(e) == {"path", "kind", "name", "succeeded", "flags", "target"} for e in document["runtime"]["output_events"]))
        self.assertTrue(all(not flag.startswith("AT_") for e in document["runtime"]["output_events"] for flag in e["flags"]),
                        "v3 事件的 flags 仍只有 O_* 标志")
        tracker = ra._runtime_module().OutputTracker(self.contract)
        self.assertFalse(hasattr(tracker, "link_text"))

    def test_v3_and_default_parser_paths_do_not_use_the_new_checks(self) -> None:
        parser = ra.TraceParser(strict=True)       # declared_sampler 复用的默认路径（无隔离合同）
        line = f'1 newfstatat(AT_FDCWD</repo>, "/x/l", {STAT_LNK}, AT_SYMLINK_NOFOLLOW) = 0'
        parser.feed(line)
        self.assertEqual(parser.unresolved_reasons, {"symlink-metadata-not-bound:newfstatat": 1})
        for line in ('1 newfstatat(AT_FDCWD</repo>, "/x/l", 0x1, AT_FOO) = 0', '1 mkdir("/x/l/", 0700) = 0',
                     '1 symlinkat("a", AT_FDCWD</repo>, "/x/l") = 0'):
            plain = ra.TraceParser(strict=True)
            plain.feed(line)
            self.assertFalse(any(key.startswith(("at-flag", "symlink-path", "symlink-text")) for key in plain.unresolved_reasons), line)
            v3 = ra.TraceParser(strict=True, runtime_context=self.context)
            v3.feed(line)
            self.assertFalse(any(key.startswith(("at-flag", "symlink-path", "symlink-text")) for key in v3.unresolved_reasons), line)

    def test_new_parser_state_is_inert_under_v3(self) -> None:
        parser = ra.TraceParser(strict=True, runtime_context=self.context)
        parser.feed(self.symlink_line("d/l"))
        self.assertIsNone(parser.call_link)
        self.assertEqual(parser.call_flags, frozenset())

    def test_control_sidecar_files_are_not_touched_by_this_round(self) -> None:
        base = Path(ra.__file__).parent
        for name in ("control_read_audit.py", "control_read_audit_runtime.py"):
            text = (base / name).read_text(encoding="utf-8")
            for forbidden in ("link_text", "AT_FLAGS_V4", "_symlink_name_verdict", "_path_form_hazard"):
                self.assertNotIn(forbidden, text, name)


# =====================================================================================================================
# 整份语料重放（可选）
# =====================================================================================================================

CORPUS_DIR = Path(os.environ["WP_R2_CORPUS_DIR"]) if os.environ.get("WP_R2_CORPUS_DIR") else None


@unittest.skipUnless(CORPUS_DIR is not None and CORPUS_DIR.is_dir(),
                     "整份语料重放默认不跑：设置环境变量 WP_R2_CORPUS_DIR=<含 raw-diag-*.gz 的目录> 才启用")
class FullCorpusTests(R3Fixture):
    """driverwait 语料里 git 探测共 19 次：整份语料经 v4 解析后，符号链接相关的未解析原因与归属问题都必须为零。
    语料的 openat2 仍是未解码的指针（抓取时还没有新选项），所以仍有 3 条 open-flags-not-decoded:openat2；connect 同理（WP-R2）。"""

    POINTER_CONNECT = re.compile(r"(connect\(\d+<socket:\[\d+\]>, )0x[0-9a-f]+(, 110\b)")

    def feed_corpus(self, name: str, root_hash: str) -> ra.TraceParser:
        self.use_root(WORK + "/runtime/" + root_hash)
        parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v4")
        with gzip.open(CORPUS_DIR / name, "rt", encoding="utf-8", errors="surrogateescape") as stream:
            for line in stream:
                parser.feed(self.POINTER_CONNECT.sub(r'\1{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}\2', line.rstrip("\n")))
        return parser

    def test_driverwait_git_probes_all_pass(self) -> None:
        parser = self.feed_corpus("raw-diag-driverwait.gz", "3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1")
        self.assertEqual(parser.unresolved_reasons, {"open-flags-not-decoded:openat2": 3})
        self.assertEqual(sorted(parser.output_tracker.problems), [])
        self.assertEqual(len([e for e in parser.output_events if e["name"] == "symlinkat" and e["succeeded"]]), 19)
        created = {e["path"] for e in parser.output_events if e["name"] == "symlinkat" and e["succeeded"]}
        self.assertEqual(len([e for e in parser.output_events if e["name"] == "newfstatat" and e["path"] in created
                              and e["flags"] == ["AT_SYMLINK_NOFOLLOW"]]), 19, "每个被创建的链接各被 lstat 一次")
        self.assertEqual(parser.output_tracker.link_text, {}, "每次探测都以 unlinkat 收尾")

    def test_the_other_two_corpora_have_no_symlink_activity_and_stay_clean(self) -> None:
        for name, root_hash in (("raw-diag-ingress.gz", "fe5a72c41d005e9f10cd39d20108a19e014541545c03e43877b9a3c6a1ce3786"),
                                ("raw-diag-directed.gz", "b3331ceddec57943ade2119907d315d6d2a6b375869bc4322c023fb2bb84fc34")):
            with self.subTest(corpus=name):
                parser = self.feed_corpus(name, root_hash)
                self.assertEqual(parser.unresolved_reasons, {})
                self.assertEqual(sorted(parser.output_tracker.problems), [])


if __name__ == "__main__":
    unittest.main()
