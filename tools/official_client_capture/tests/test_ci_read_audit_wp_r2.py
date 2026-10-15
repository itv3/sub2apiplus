"""审计解析器加固第二轮（WP-R2）：把严格读集审计在真实 ARM64 环境下误判成“未解析”的正常行为放宽，且只在 v4 轨迹合同下放宽。

背景：10-15 ARM64 后台验证里，已完成的 13 个受审计测试单元有 11 个“测试本身通过，审计却判不通过”（普查表见调度者证据目录
``claude-control-plane/20261015/step10/census-bgrun.txt``，原始 strace 语料在同目录 ``raw-corpus``）。本模块逐类用语料里的
**原始行**做重放测试（下面的 ``RAW_*`` 常量逐字取自语料，只把很长的工作目录前缀写成 ``{W}``），每类都有：

* 正例：放宽后不再登记未解析、输出归属重放通过；
* 反例：相邻的、会暴露宿主信息的形态仍判未解析／被拒绝；
* 旧语料对照：同样的行在 v3 合同下的结论与改动前逐字相同（v3 一字不变）。

另有一组默认不跑的整份语料重放（``FullCorpusReplayTests``，约 25 万行、1 分钟）：设置 ``WP_R2_CORPUS_DIR`` 指向存放
``raw-diag-ingress.gz``／``raw-diag-directed.gz``／``raw-diag-driverwait.gz`` 的目录才启用，语料不进仓库。

各项的安全依据（这个系统调用在这种情形下能暴露什么宿主信息）写在 ``tools/ci/read_audit.py`` 对应辅助函数的说明里：

* R2-1 失败的 getcwd（ERANGE／ENOENT）：缓冲区是指针，内核没写任何路径 → 不暴露；
* R2-2 ``utimensat(fd, NULL, …, 0)``（futimens）：只对已打开对象改时间 → 按“对该路径的写”登记，归属照对已知对象的元数据写处理；
* R2-3 ``openat2``：strace 解码 ``struct open_how`` 后标志才可见；``O_PATH|O_NOFOLLOW`` 仍走 symlink-metadata-not-bound；
* R2-4 ``socket(AF_UNIX, SOCK_STREAM…)``：创建本身不碰路径；``connect`` 到 AF_UNIX 绝对路径且 ENOENT 登记为 missing 探测；
  其余结果／相对路径／抽象套接字／网络地址仍判 unsupported-call:connect；v4 同时跟踪 bind（否则放行 socket 后 bind 不可见）；
* R2-5 私有盘内符号链接探测：**未放宽**（依赖输出归属先接受符号链接创建，属于更大的改动，见 ``SymlinkProbeStillRejectedTests``）；
* R2-6 ``MAX_OUTPUT_EVENTS_V4``：见下表；
* R2-7 openat 三联未解析：无法复现，只固定“哪些形态会产生这三个原因”的假设（``OpenatTripleHypothesisTests``），不放宽。

R2-6 合成负载实测（macOS 本机，Python 3.14；路径长度取真实值约 190 字符，事件互不相同、无法去重，每条创建调用产生 2 条事件）：

    事件数     解析耗时   解析峰值内存   轨迹 JSON    读回+重放耗时   重放峰值内存
    200,001      15.7 s      667 MiB     230 MiB       0.5 + 0.9 s      1,028 MiB
    500,001      39.7 s    1,596 MiB     576 MiB       1.2 + 2.7 s      2,393 MiB
  1,000,001      79.2 s    3,166 MiB   1,152 MiB       2.8 + 5.5 s      3,838 MiB

线性约 3.2 KiB／事件（解析）、3.8 KiB／事件（重放）。vc6_batch 的真实需求是 200,000 + 358,504 = 558,504 条（census 里的
358,504 是“超出旧上限而被丢弃并登记 output-event-limit 的条数”，不是总数），所以 50 万的上限也不够；取 1,000,000，约有 1.8 倍余量。
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import os
import re
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests.test_ci_read_audit_v4_trace import AuditFixture

# 语料里的工作目录与本单元的私有临时根（tmpfs，st_dev=makedev(0, 0x69)）。
WORK = ("/root/docker/capture-cli/entry-gates-source-work/"
        "4c1b529f3d49e6ee9f68abfa4007e160d71f5991-cb73394623383b0679b57fd3482e64015e715158dcf59d17e3a506f1b46d0f5a")
CORPUS_ROOT = WORK + "/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1"


def raw(line: str) -> str:
    """语料原始行里 ``{W}`` 是上面的 WORK 常量；除此之外逐字不变。"""
    return line.replace("{W}", WORK)


# --- 逐字取自 raw-corpus 的原始行（census 里各类未解析原因的触发行） ---
RAW_GETCWD_ERANGE_129 = raw('3984812 getcwd(0xbebd6f63aba0, 129)     = -1 ERANGE (Numerical result out of range)')
RAW_GETCWD_ERANGE_257 = raw('3984916 getcwd(0xc429a0028ba0, 257)     = -1 ERANGE (Numerical result out of range)')
RAW_GETCWD_OK = raw('3983725 getcwd("{W}/test-tree", 4096) = 165')
RAW_SOCKET = raw('3965603 socket(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK, 0) = 3<socket:[811823840]>')
RAW_CONNECT_POINTER = raw('3965603 connect(3<socket:[811823840]>, 0xfffffa310de8, 110) = -1 ENOENT (No such file or directory)')
RAW_FUTIMENS = raw('3990340 utimensat(0<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/'
                   'tmpphejkk5_/remote/impl-logs/HEARTBEAT>, NULL, NULL, 0) = 0')
RAW_FUTIMENS_TIMES = raw('3990429 utimensat(5<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/'
                         'tmpphejkk5_/remote/impl-logs/upload-manifest.json>, NULL, 0xffffea80c848, 0) = 0')
RAW_OPENAT2_POINTER = raw('3990429 openat2(3<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/'
                          'tmpphejkk5_/remote>, "impl-logs/", 0xb1b586a636a8, 24) = 4<{W}/runtime/'
                          '3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpphejkk5_/remote/impl-logs>')
# git 的符号链接支持探测（先 O_EXCL 建同名文件再删，再 symlinkat、lstat、unlinkat）：19 条 symlink-metadata-not-bound 的来源。
RAW_GIT_PROBE = [
    raw('3984812 mkdirat(AT_FDCWD<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history>, '
        '"{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history/.git", 0777) = 0'),
    raw('3984812 openat(AT_FDCWD<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history>, '
        '"{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history/.git/tkkGrhW", '
        'O_RDWR|O_CREAT|O_EXCL, 0600) = 3<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/'
        'tmpwzmo6c4w/history/.git/tkkGrhW>'),
    raw('3984812 unlinkat(AT_FDCWD<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history>, '
        '"{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history/.git/tkkGrhW", 0) = 0'),
    raw('3984812 symlinkat("testing", AT_FDCWD<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/'
        'tmpwzmo6c4w/history>, "{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/'
        'history/.git/tkkGrhW") = 0'),
    raw('3984812 newfstatat(AT_FDCWD<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history>, '
        '"{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history/.git/tkkGrhW", '
        '{st_dev=makedev(0, 0x69), st_ino=149, st_mode=S_IFLNK|0777, st_nlink=1, st_uid=0, st_gid=0, st_blksize=4096, st_blocks=0, '
        'st_size=7, st_atime=1792047380 /* 2026-10-15T06:56:20.696287249+0000 */, st_atime_nsec=696287249, '
        'st_mtime=1792047380 /* 2026-10-15T06:56:20.696287249+0000 */, st_mtime_nsec=696287249, '
        'st_ctime=1792047380 /* 2026-10-15T06:56:20.696287249+0000 */, st_ctime_nsec=696287249}, AT_SYMLINK_NOFOLLOW) = 0'),
    raw('3984812 unlinkat(AT_FDCWD<{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history>, '
        '"{W}/runtime/3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1/tmpwzmo6c4w/history/.git/tkkGrhW", 0) = 0'),
]
# 调度者在 ARM64 上实测的、加了新 strace 选项（verbose 多解码 openat2 与 connect）之后的形态（语料里是未解码的指针）。
DECODED_OPENAT2_ARM64 = ('9 openat2(AT_FDCWD</tmp>, "s10o2/", {flags=O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_PATH|O_DIRECTORY, resolve=0}, 24) '
                         '= 4</tmp/s10o2>')
DECODED_CONNECT_ARM64 = ('9 connect(3<socket:[811894649]>, {sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}, 110) '
                         '= -1 ENOENT (No such file or directory)')
GETCWD_ENOENT_ARM64 = '9 getcwd(0x1591cd90, 1024)        = -1 ENOENT (No such file or directory)'


class CorpusFixture(AuditFixture):
    """把私有临时根换成语料里的真实根（路径长度与结构一致），其余沿用 v4 测试的夹具。本类没有 test_ 方法。"""

    def setUp(self) -> None:
        super().setUp()
        self.use_root(CORPUS_ROOT)

    def use_root(self, root: str) -> None:
        """重建以 ``root`` 为私有临时根的隔离合同、上下文（整份语料重放要按语料的三个不同根切换）。"""
        self.root = root
        self.contract = {"schema_version": runtime.SCHEMA, "temporary_root": self.root,
                         "mask_roots": [runtime.MASK_ROOT], "environment_paths": []}
        self.entry = runtime.contract_entry(self.contract)
        trace = ('1 mount("none", "/", NULL, MS_REC|MS_PRIVATE, NULL) = 0\n'
                 f'2 mount("tmpfs", "{runtime.MASK_ROOT}", "tmpfs", MS_RDONLY, "size=64k,mode=0755") = 0\n'
                 f'3 mount("tmpfs", "{self.root}", "tmpfs", MS_NOSUID|MS_NODEV, "size=512m,mode=0700") = 0\n')
        self.context = {"schema_version": runtime.CONTEXT_SCHEMA, "contract_entry": self.entry,
                        "parent_namespace": "mnt:[100]", "namespace": "mnt:[101]", "empty_temporary_root": True,
                        "private_mounts": True, "empty_masks": {runtime.MASK_ROOT: True}, "mounts": [
                            {"mount_id": 200, "target": runtime.MASK_ROOT, "filesystem": "tmpfs", "options": ["ro"]},
                            {"mount_id": 201, "target": self.root, "filesystem": "tmpfs", "options": ["rw", "nosuid", "nodev"]}],
                        "setup_commands": ["private", runtime.MASK_ROOT, self.root], "setup_success": True,
                        "setup_trace": trace, "setup_trace_sha256": hashlib.sha256(trace.encode()).hexdigest()}

    def parse(self, lines: list[str], contract: str = "v4") -> ra.TraceParser:
        parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract=contract)
        for line in lines:
            parser.feed(line)
        return parser

    def reasons(self, lines: list[str], contract: str = "v4") -> dict[str, int]:
        return dict(self.parse(lines, contract).unresolved_reasons)

    def pad(self, lines: list[str]) -> list[str]:
        """审计要求轨迹至少有一条访问；在私有根里建一个目录当底座。"""
        return [self.mkdir_line("base")] + lines


# =====================================================================================================================
# R2-1 getcwd
# =====================================================================================================================


class GetcwdTests(CorpusFixture):
    """失败的 getcwd（有 errno，缓冲区是指针）不读取任何宿主路径信息。"""

    def test_failed_getcwd_from_the_corpus_is_no_longer_unresolved_under_v4(self) -> None:
        for label, line in (("ERANGE 129", RAW_GETCWD_ERANGE_129), ("ERANGE 257", RAW_GETCWD_ERANGE_257),
                            ("ENOENT(调度者实测形态)", GETCWD_ENOENT_ARM64)):
            with self.subTest(label=label):
                parser = self.parse([line])
                self.assertEqual(parser.unresolved_reasons, {})
                self.assertEqual(parser.accesses, {}, "失败的 getcwd 不登记任何访问")

    def test_the_same_lines_are_unchanged_under_v3(self) -> None:
        for line in (RAW_GETCWD_ERANGE_129, RAW_GETCWD_ERANGE_257, GETCWD_ENOENT_ARM64):
            with self.subTest(line=line[:40]):
                self.assertEqual(self.reasons([line], "v3"), {"plain-path-not-resolved:getcwd": 1})
                parser = ra.TraceParser(strict=True)       # declared_sampler 复用的默认路径（不带隔离合同）同样不变
                parser.feed(line)
                self.assertEqual(parser.unresolved_reasons, {"plain-path-not-resolved:getcwd": 1})

    def test_thousands_of_failed_getcwd_calls_still_pass_the_whole_audit(self) -> None:
        lines = self.pad([RAW_GETCWD_ERANGE_129, RAW_GETCWD_ERANGE_257] * 2500)
        document = self.document(lines)
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertAccepted(document)

    def test_successful_getcwd_with_a_string_is_unchanged_and_updates_the_working_directory(self) -> None:
        parser = self.parse([RAW_GETCWD_OK])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(parser.accesses, {WORK + "/test-tree": {"dir"}})
        self.assertEqual(parser.cwd["3983725"], WORK + "/test-tree")

    def test_failed_getcwd_does_not_touch_the_working_directory(self) -> None:
        parser = self.parse([f'9 openat(AT_FDCWD<{WORK}/test-tree>, "{WORK}/test-tree/a", O_RDONLY) = -1 ENOENT (No such file or directory)',
                             GETCWD_ENOENT_ARM64])
        self.assertEqual(parser.cwd["9"], WORK + "/test-tree")

    def test_successful_getcwd_without_a_string_stays_unresolved(self) -> None:
        """成功却取不到字符串（缓冲区是指针）：看不到工作目录是什么，保持失败关闭。"""
        self.assertEqual(self.reasons(['9 getcwd(0x1591cd90, 1024)        = 5']), {"plain-path-not-resolved:getcwd": 1})

    def test_only_the_two_observed_errnos_are_accepted(self) -> None:
        for errno_name, text in (("EFAULT", "Bad address"), ("EINVAL", "Invalid argument"), ("EACCES", "Permission denied")):
            with self.subTest(errno=errno_name):
                self.assertEqual(self.reasons([f'9 getcwd(0x1591cd90, 1024)        = -1 {errno_name} ({text})']),
                                 {"plain-path-not-resolved:getcwd": 1})

    def test_a_failed_getcwd_that_does_carry_a_string_keeps_the_old_handling(self) -> None:
        """只放宽“缓冲区是指针”的失败；带字符串的失败行（不会出现，出现则说明格式变了）照旧登记路径。"""
        parser = self.parse(['9 getcwd("/x/y", 10) = -1 ERANGE (Numerical result out of range)'])
        self.assertIn("/x/y", parser.accesses)

    def test_chdir_failures_are_not_relaxed(self) -> None:
        """chdir 的失败有字符串参数，路径本来就可见；指针形态的 chdir 仍判未解析。"""
        self.assertEqual(self.reasons(['9 chdir(0x7ffd1234) = -1 ENOENT (No such file or directory)']),
                         {"plain-path-not-resolved:chdir": 1})
        parser = self.parse(['9 chdir("/nowhere") = -1 ENOENT (No such file or directory)'])
        self.assertEqual(parser.accesses, {"/nowhere": {"missing"}})
        self.assertEqual(parser.unresolved_reasons, {})


# =====================================================================================================================
# R2-2 utimensat(fd, NULL, …)
# =====================================================================================================================


class FutimensTests(CorpusFixture):
    """futimens(fd) 作用于 fd 自己：按对该路径的写登记，归属照对已知对象的元数据写处理。"""

    def prelude_heartbeat(self) -> list[str]:
        r = self.root
        return [f'1 mkdir("{r}/tmpphejkk5_", 0700) = 0', f'1 mkdir("{r}/tmpphejkk5_/remote", 0700) = 0',
                f'1 mkdir("{r}/tmpphejkk5_/remote/impl-logs", 0700) = 0',
                f'1 openat(AT_FDCWD<{WORK}/test-tree>, "{r}/tmpphejkk5_/remote/impl-logs/HEARTBEAT", O_WRONLY|O_CREAT|O_EXCL, 0600) '
                f'= 3<{r}/tmpphejkk5_/remote/impl-logs/HEARTBEAT>',
                f'1 openat(AT_FDCWD<{WORK}/test-tree>, "{r}/tmpphejkk5_/remote/impl-logs/upload-manifest.json", O_WRONLY|O_CREAT|O_EXCL, 0600) '
                f'= 5<{r}/tmpphejkk5_/remote/impl-logs/upload-manifest.json>']

    def test_corpus_futimens_lines_are_registered_as_writes_to_the_descriptor_path(self) -> None:
        for line in (RAW_FUTIMENS, RAW_FUTIMENS_TIMES):
            with self.subTest(line=line[:40]):
                document = self.document(self.prelude_heartbeat() + [line])
                self.assertEqual(document["coverage"]["unresolved"], 0)
                last = document["runtime"]["output_events"][-1]
                self.assertEqual((last["name"], last["kind"], last["succeeded"], last["errno"], last["role"], last["peer"]),
                                 ("utimensat", "write", True, None, None, None))
                self.assertTrue(last["path"].startswith(self.root + "/tmpphejkk5_/remote/impl-logs/"))
                self.assertAccepted(document)

    def test_the_same_line_under_v3_is_still_unresolved(self) -> None:
        self.assertEqual(self.reasons([RAW_FUTIMENS], "v3"), {"dirfd-path-not-resolved:utimensat": 1})

    def test_futimens_on_an_object_the_run_did_not_create_is_refused_by_the_tracker(self) -> None:
        """解析器登记了写，归属跟踪器不认来源不明的对象：重放拒绝（失败关闭）。"""
        document = self.document([self.mkdir_line("base"), RAW_FUTIMENS])
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertTrue(any("未建立合同的输出操作" in text for text in document["runtime"]["outputs"]["problems"]))
        self.assertRejected(document)

    def test_failed_futimens_is_refused(self) -> None:
        line = RAW_FUTIMENS.replace("= 0", "= -1 EBADF (Bad file descriptor)")
        document = self.document(self.prelude_heartbeat() + [line])
        self.assertRejected(document)

    def test_futimens_on_a_host_object_is_a_write_finding(self) -> None:
        """宿主文件上的 futimens：登记成对该宿主路径的写，读集审计照常拦下（写不是可绑定的访问类型）。"""
        document = self.document(self.pad(['9 utimensat(0</etc/hosts>, NULL, NULL, 0) = 0']))
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertEqual(dict((item[0], item[1]) for item in document["accesses"])["/etc/hosts"], ["write"])
        report = self.audit(document)
        self.assertFalse(report["coverage_complete"])
        self.assertIn(("/etc/hosts", ["write"]), [(item["path"], item["kinds"]) for item in report["undeclared"]])

    def test_shapes_that_are_not_a_plain_futimens_stay_unresolved(self) -> None:
        r = self.root
        cases = {
            "标志非零": f'9 utimensat(0<{r}/f>, NULL, NULL, AT_SYMLINK_NOFOLLOW) = -1 EINVAL (Invalid argument)',
            "AT_FDCWD 配 NULL 路径": f'9 utimensat(AT_FDCWD<{r}>, NULL, NULL, 0) = -1 EFAULT (Bad address)',
            "描述符没有路径标注": '9 utimensat(3, NULL, NULL, 0) = 0',
            "标注不是路径（管道）": '9 utimensat(3<pipe:[123]>, NULL, NULL, 0) = 0',
            "标注不是绝对路径": '9 utimensat(3<anon_inode:[eventfd]>, NULL, NULL, 0) = 0',
            "描述符是负数": f'9 utimensat(-1<{r}/f>, NULL, NULL, 0) = -1 EBADF (Bad file descriptor)',
            "路径参数是指针": f'9 utimensat(3<{r}/d>, 0xffff1234, NULL, 0) = 0',
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.reasons([line]), {"dirfd-path-not-resolved:utimensat": 1}, label)

    def test_utimensat_with_a_path_is_unchanged(self) -> None:
        """带路径的 utimensat（语料里最常见的形态）：登记目录描述符加路径还原出的路径，与改动前相同。"""
        line = (f'3983725 utimensat(AT_FDCWD<{WORK}/test-tree>, "{self.root}/d", 0xfffff80481b0, 0) = 0')
        for contract in ("v3", "v4"):
            with self.subTest(contract=contract):
                parser = self.parse([line], contract)
                self.assertEqual(parser.unresolved_reasons, {})
                self.assertEqual(parser.accesses, {f"{self.root}/d": {"write"}})
        line = f'9 utimensat(3<{self.root}/remote>, "local-gates", 0xffffea80c7b8, AT_SYMLINK_NOFOLLOW) = 0'
        self.assertEqual(self.parse([line]).accesses, {f"{self.root}/remote/local-gates": {"write"}})


# =====================================================================================================================
# R2-3 openat2
# =====================================================================================================================


class Openat2Tests(CorpusFixture):
    """openat2 的标志在 ``struct open_how`` 里；strace 解码之后才可见。"""

    def test_undecoded_pointer_form_from_the_corpus_stays_unresolved_in_both_contracts(self) -> None:
        for contract in ("v3", "v4"):
            with self.subTest(contract=contract):
                self.assertEqual(self.reasons([RAW_OPENAT2_POINTER], contract), {"open-flags-not-decoded:openat2": 1})

    def test_decoded_flags_are_read_from_the_struct(self) -> None:
        r = self.root
        line = f'9 openat2(AT_FDCWD<{r}>, "d/", {{flags=O_RDONLY|O_CLOEXEC|O_DIRECTORY, resolve=0}}, 24) = 4<{r}/d>'
        document = self.document([self.mkdir_line("d"), line])
        self.assertEqual(document["coverage"]["unresolved"], 0)
        events = [e for e in document["runtime"]["output_events"] if e["name"] == "openat2"]
        self.assertEqual({(e["path"], e["kind"]) for e in events}, {(f"{r}/d", "dir")})
        self.assertEqual({tuple(e["flags"]) for e in events}, {("O_CLOEXEC", "O_DIRECTORY", "O_RDONLY")})
        self.assertAccepted(document)

    def test_decoded_o_path_o_nofollow_still_goes_to_symlink_metadata_not_bound(self) -> None:
        """调度者在 ARM64 上实测的解码形态：标志现在可见，但 O_PATH|O_NOFOLLOW 打开的可能是链接自身，仍缺合同。"""
        parser = self.parse([DECODED_OPENAT2_ARM64])
        self.assertEqual(parser.unresolved_reasons, {"symlink-metadata-not-bound:openat2": 1})

    def test_decoded_write_flags_are_classified_as_writes(self) -> None:
        line = f'9 openat2(AT_FDCWD</tmp>, "x", {{flags=O_WRONLY|O_CREAT|O_EXCL, mode=0644, resolve=0}}, 24) = 3</tmp/x>'
        parser = self.parse([line])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(parser.accesses, {"/tmp/x": {"write"}})

    def test_struct_that_hides_the_flags_stays_unresolved(self) -> None:
        cases = {
            "省略号截断": '9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY, ...}, 24) = 3</tmp/x>',
            "没有 flags 字段": '9 openat2(AT_FDCWD</tmp>, "x", {resolve=0}, 24) = 3</tmp/x>',
            "标志里有十六进制残片": '9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY|0x4000000, resolve=0}, 24) = 3</tmp/x>',
            "两个 flags 字段": '9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY, flags=O_WRONLY, resolve=0}, 24) = 3</tmp/x>',
            "两个相同的 flags 字段": '9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY, flags=O_RDONLY, resolve=0}, 24) = 3</tmp/x>',
            "整行扫描比 flags 字段多出标志": '9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY, how=O_WRONLY}, 24) = 3</tmp/x>',
            "结构缺右括号": '9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY, 24) = 3</tmp/x>',
            "参数个数不对": '9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY, resolve=0}) = 3</tmp/x>',
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                self.assertIn("open-flags-not-decoded:openat2", self.reasons([line]), label)

    def test_decoded_form_under_v3_keeps_the_legacy_criterion(self) -> None:
        """v3 沿用“整行没有任何标志”的旧判据：不依赖本次新增的解码检查。"""
        self.assertEqual(self.reasons(['9 openat2(AT_FDCWD</tmp>, "x", {flags=O_RDONLY, how=O_WRONLY}, 24) = 3</tmp/x>'], "v3"), {})


# =====================================================================================================================
# R2-4 socket / connect
# =====================================================================================================================


class SocketConnectTests(CorpusFixture):
    """glibc 的 nscd／NSS 探测：socket(AF_UNIX) 创建 + connect 到 /var/run/nscd/socket 得到 ENOENT。"""

    NSCD = "/var/run/nscd/socket"

    def test_corpus_socket_line_is_accepted_under_v4_and_not_under_v3(self) -> None:
        parser = self.parse([RAW_SOCKET])
        self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {}), "只创建套接字：不登记未解析、不登记访问")
        self.assertEqual(self.reasons([RAW_SOCKET], "v3"), {"unsupported-call:socket": 1})

    def test_undecoded_connect_from_the_corpus_stays_unsupported(self) -> None:
        """旧语料（没解码 sockaddr，目的地是指针）：看不到连到哪里，两种合同都判不支持。"""
        for contract in ("v3", "v4"):
            with self.subTest(contract=contract):
                self.assertEqual(self.reasons([RAW_SOCKET, RAW_CONNECT_POINTER], contract),
                                 {"unsupported-call:socket": 1, "unsupported-call:connect": 1} if contract == "v3"
                                 else {"unsupported-call:connect": 1})

    def test_decoded_connect_enoent_is_a_missing_probe_of_that_absolute_path(self) -> None:
        parser = self.parse([RAW_SOCKET, DECODED_CONNECT_ARM64])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(parser.accesses, {self.NSCD: {"missing"}})

    def test_the_missing_probe_needs_a_declared_host_path_otherwise_the_audit_fails(self) -> None:
        """strict_audit_reads 对“策略没声明的 missing 访问”的处理：登记为“实际路径缺少精确快照”的未声明项，整次审计判失败；
        声明了该路径（host_paths 里给出 missing 快照）才通过。"""
        missing = self.base / "var-run" / "nscd" / "socket"
        line = DECODED_CONNECT_ARM64.replace(self.NSCD, str(missing))
        document = self.document(self.pad([RAW_SOCKET, line]))
        self.assertEqual(document["coverage"]["unresolved"], 0)
        report = ra.strict_audit_reads(document, [self.entry], repo_root="/repo", data_root=None)
        self.assertEqual(report["status"], "failed")
        self.assertEqual([(item["root"], item["path"], item["kinds"]) for item in report["undeclared"]],
                         [("host", str(missing), ["missing"])])
        self.assertIn("缺少精确快照", report["undeclared"][0]["suggestion"])
        declared = ra.host_snapshot(str(missing))
        self.assertEqual(declared["detail"]["snapshot"]["kind"], "missing")
        completed = self.completed(ra.filter_stream(self.pad([RAW_SOCKET, line]), ["/"], strict=True, runtime_context=self.context,
                                                    trace_contract="v4"), host_entries_sha256=runtime.digest([declared]))
        report = ra.strict_audit_reads(completed, [self.entry, declared], repo_root="/repo", data_root=None)
        self.assertEqual((report["status"], report["undeclared_count"]), ("passed", 0), report["undeclared"])

    def test_corpus_scale_every_probe_pair_is_registered_once(self) -> None:
        pairs = []
        for index in range(390):
            pairs += [f'{index + 10} socket(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK, 0) = 3<socket:[{index + 1000}]>',
                      DECODED_CONNECT_ARM64.replace("9 connect", f"{index + 10} connect").replace("811894649", str(index + 1000))]
        parser = self.parse(pairs)
        self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {self.NSCD: {"missing"}}))

    def test_other_socket_domains_and_types_stay_unsupported(self) -> None:
        cases = [
            "socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)", "socket(AF_INET6, SOCK_DGRAM, IPPROTO_UDP)",
            "socket(AF_NETLINK, SOCK_RAW|SOCK_CLOEXEC, NETLINK_ROUTE)", "socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL))",
            "socket(AF_UNIX, SOCK_DGRAM|SOCK_CLOEXEC, 0)", "socket(AF_UNIX, SOCK_SEQPACKET, 0)",
            "socket(AF_UNIX, SOCK_STREAM, 1)", "socket(AF_UNIX, SOCK_STREAM|SOCK_RAW, 0)",
            "socket(AF_UNIX, SOCK_CLOEXEC|SOCK_STREAM, 0)", "socket(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC|SOCK_CLOEXEC, 0)",
            "socket(AF_UNIX, SOCK_STREAM)", "socket(0x1, SOCK_STREAM, 0)",
        ]
        for call in cases:
            with self.subTest(call=call):
                self.assertEqual(self.reasons([f"9 {call} = 3<socket:[1]>"]), {"unsupported-call:socket": 1})

    def test_af_local_spelling_and_failed_creation_are_accepted(self) -> None:
        self.assertEqual(self.reasons(["9 socket(AF_LOCAL, SOCK_STREAM|SOCK_CLOEXEC, 0) = 3<socket:[1]>"]), {})
        self.assertEqual(self.reasons(["9 socket(AF_UNIX, SOCK_STREAM, 0) = -1 EMFILE (Too many open files)"]), {})

    def test_unfinished_and_resumed_socket_and_connect_lines(self) -> None:
        lines = ["5 socket(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK, 0 <unfinished ...>",
                 "5 <... socket resumed>)                    = 5<socket:[77]>",
                 '5 connect(5<socket:[77]>, {sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}, 110 <unfinished ...>',
                 "5 <... connect resumed>)                   = -1 ENOENT (No such file or directory)"]
        parser = self.parse(lines)
        self.assertEqual((parser.unresolved_reasons, parser.pending, parser.accesses), ({}, {}, {self.NSCD: {"missing"}}))

    def test_connect_results_other_than_enoent_stay_unsupported(self) -> None:
        template = '9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}}, 110) = {result}'
        for result in ("0", "-1 ECONNREFUSED (Connection refused)", "-1 EACCES (Permission denied)", "-1 ENOTDIR (Not a directory)",
                       "-1 EINPROGRESS (Operation now in progress)", "-1 EAGAIN (Resource temporarily unavailable)", "?"):
            with self.subTest(result=result):
                parser = self.parse([template.format(result=result)])
                self.assertEqual(parser.unresolved_reasons, {"unsupported-call:connect": 1})
                self.assertEqual(parser.accesses, {})

    def test_connect_addresses_that_are_not_an_absolute_unix_path_stay_unsupported(self) -> None:
        enoent = "= -1 ENOENT (No such file or directory)"
        cases = {
            "相对路径": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path="nscd/socket"}}, 110) {enoent}',
            "抽象套接字": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path=@"nscd"}}, 110) {enoent}',
            "抽象套接字（名字以斜杠开头）": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path=@"/var/run/nscd/socket"}}, 110) {enoent}',
            "路径含 NUL": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path="/var/run/\\0x"}}, 110) {enoent}',
            "空路径": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path=""}}, 110) {enoent}',
            "无名地址": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX}}, 2) {enoent}',
            "路径被截断": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path="/var/run/nscd/so"...}}, 110) {enoent}',
            "结构省略": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, ...}}, 110) {enoent}',
            "多余字段": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path="/x", extra=1}}, 110) {enoent}',
            "IPv4": f'9 connect(3<socket:[1]>, {{sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("10.0.0.1")}}, 16) {enoent}',
            "IPv6": f'9 connect(3<socket:[1]>, {{sa_family=AF_INET6, sin6_port=htons(53), sin6_flowinfo=htonl(0), '
                    f'inet_pton(AF_INET6, "::1", &sin6_addr), sin6_scope_id=0}}, 28) {enoent}',
            "netlink": f'9 connect(3<socket:[1]>, {{sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}}, 12) {enoent}',
            "描述符不是套接字标注": f'9 connect(3</tmp/x>, {{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}}, 110) {enoent}',
            "描述符没有标注": f'9 connect(3, {{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}}, 110) {enoent}',
            "地址长度不是数字": f'9 connect(3<socket:[1]>, {{sa_family=AF_UNIX, sun_path="/x"}}, sizeof) {enoent}',
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                parser = self.parse([line])
                self.assertIn("unsupported-call:connect", parser.unresolved_reasons, label)
                self.assertNotIn("/var/run/nscd/socket", parser.accesses, label)

    def test_the_unix_path_extractor_requires_the_unix_family_literally(self) -> None:
        """直接测提取函数：地址族必须是 AF_UNIX／AF_LOCAL；一个编造的“别的族却带 sun_path”也不放行。"""
        good = ['3<socket:[1]>', '{sa_family=AF_UNIX, sun_path="/a/b"}', "110"]
        self.assertEqual(ra._connect_unix_path(good), "/a/b")
        self.assertEqual(ra._connect_unix_path([good[0], '{sa_family=AF_LOCAL, sun_path="/a/../b"}', "110"]), "/b")
        for family in ("AF_INET", "AF_INET6", "AF_NETLINK", "AF_UNSPEC", "0x1"):
            with self.subTest(family=family):
                self.assertIsNone(ra._connect_unix_path([good[0], '{sa_family=' + family + ', sun_path="/a/b"}', "110"]))

    def test_connect_decoded_form_is_not_accepted_under_v3(self) -> None:
        self.assertEqual(self.reasons([DECODED_CONNECT_ARM64], "v3"), {"unsupported-call:connect": 1})

    def test_bind_and_the_other_socket_calls_stay_unsupported(self) -> None:
        """socket(AF_UNIX) 放行后，能携带路径或与外部通信的其余调用必须仍然失败关闭。"""
        for call in ('bind(3<socket:[1]>, {sa_family=AF_UNIX, sun_path="/tmp/s"}, 110) = 0',
                     "accept(3<socket:[1]>, NULL, NULL) = 4<socket:[2]>", "accept4(3<socket:[1]>, NULL, NULL, 0) = 4<socket:[2]>",
                     "sendmsg(3<socket:[1]>, {msg_name=NULL}, 0) = 1", "recvmsg(3<socket:[1]>, {msg_name=NULL}, 0) = 1"):
            with self.subTest(call=call[:12]):
                name = call.split("(")[0]
                self.assertEqual(self.reasons([f"9 {call}"]), {f"unsupported-call:{name}": 1})

    def test_connect_inside_the_private_root_is_an_ordinary_missing_probe(self) -> None:
        line = DECODED_CONNECT_ARM64.replace(self.NSCD, f"{self.root}/sock")
        document = self.document(self.pad([RAW_SOCKET, line]))
        self.assertEqual(document["coverage"]["unresolved"], 0)
        events = [e for e in document["runtime"]["output_events"] if e["name"] == "connect"]
        self.assertEqual([(e["path"], e["kind"], e["succeeded"], e["errno"]) for e in events],
                         [(f"{self.root}/sock", "missing", False, "ENOENT")])
        self.assertAccepted(document)

    def test_connect_to_a_path_the_run_created_in_the_private_root_contradicts_the_trace(self) -> None:
        """本轮已创建的名字却报 ENOENT：名字表与轨迹矛盾，归属重放拒绝。"""
        line = DECODED_CONNECT_ARM64.replace(self.NSCD, f"{self.root}/base")
        document = self.document(self.pad([line]))
        self.assertRejected(document)


# =====================================================================================================================
# strace 命令行
# =====================================================================================================================


class StraceArgvTests(CorpusFixture):
    """v3 命令行逐字节不变（黄金值取自改动前的实现）；新增选项只在 v4。"""

    OUTPUT = Path("/o/t.json")
    CONTEXT = Path("/o/ctx.json")
    PYTHON = "/usr/bin/python3"
    HEAD = ["strace", "-f", "-qq", "--seccomp-bpf", "-y", "-e", "verbose=none", "-e", "signal=none",
            "-e", "trace=%file,clone,clone3,fork,vfork"]
    STRICT_TRACE_V3 = ("trace=%file,getdents64,clone,clone3,fork,vfork,fchdir,unshare,setns,open_by_handle_at,io_uring_setup,"
                       "socket,connect,accept,accept4,sendmsg,recvmsg")

    def golden_v3_strict(self, *, extra: str = "") -> list[str]:
        pipe = (f"|{self.PYTHON} -B {ra.HERE} filter --output {self.OUTPUT} --strict --runtime-context {self.CONTEXT}{extra} --root /")
        return [*self.HEAD, "-s", "4096", "-e", "verbose=%%stat", "-e", "abbrev=!%%stat", "-e", self.STRICT_TRACE_V3,
                "-o", pipe, "--", "echo", "x"]

    def argv(self, **kwargs: Any) -> list[str]:
        return ra.strace_argv(["echo", "x"], output=self.OUTPUT, roots=["/"], python=self.PYTHON, strict=True,
                              runtime_context=self.CONTEXT, **kwargs)

    def test_v3_strict_command_line_is_byte_identical_to_the_golden(self) -> None:
        self.assertEqual(self.argv(), self.golden_v3_strict())
        self.assertEqual(self.argv(trace_contract="v3"), self.golden_v3_strict())

    def test_v3_non_strict_and_strict_without_runtime_are_byte_identical_to_the_golden(self) -> None:
        pipe = ("|grep --line-buffered -E '\"(/r)(/|\")|<(/r)[^>]*>, \"[^/\"]|^[0-9]+ (execve|execveat|chdir|clone|clone3|fork|vfork)\\("
                f"|<[.][.][.] (clone|clone3|fork|vfork) resumed>' | {self.PYTHON} -B {ra.HERE} filter --output {self.OUTPUT} --root /r")
        self.assertEqual(ra.strace_argv(["echo", "x"], output=self.OUTPUT, roots=["/r"], python=self.PYTHON),
                         [*self.HEAD, "-o", pipe, "--", "echo", "x"])
        strict_pipe = f"|{self.PYTHON} -B {ra.HERE} filter --output {self.OUTPUT} --strict --root /"
        self.assertEqual(ra.strace_argv(["echo", "x"], output=self.OUTPUT, roots=["/r"], python=self.PYTHON, strict=True),
                         [*self.HEAD, "-s", "4096", "-e", "verbose=%%stat", "-e", "abbrev=!%%stat", "-e", self.STRICT_TRACE_V3,
                          "-o", strict_pipe, "--", "echo", "x"])

    def test_v4_adds_exactly_the_decode_options_and_bind(self) -> None:
        v4 = self.argv(trace_contract="v4")
        expected = self.golden_v3_strict(extra=" --trace-contract v4")
        expected[expected.index("verbose=%%stat")] = "verbose=%%stat,openat2,connect"
        expected[expected.index(self.STRICT_TRACE_V3)] = self.STRICT_TRACE_V3 + ",bind"
        self.assertEqual(v4, expected)
        # 独立于上面的构造再核一遍字面量：%%stat 必须仍在同一个 verbose= 限定里（不依赖多个同类 -e 的叠加／覆盖语义）。
        self.assertIn("verbose=%%stat,openat2,connect", v4)
        self.assertEqual(sum(item.startswith("verbose=") for item in v4), 2, "只有 verbose=none 与这一个")
        self.assertTrue(v4[v4.index("verbose=%%stat,openat2,connect") - 1] == "-e")
        trace = [item for item in v4 if item.startswith("trace=") and "getdents64" in item]
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0].split(",")[-1], "bind")
        self.assertTrue({"socket", "connect", "accept", "accept4", "sendmsg", "recvmsg", "bind"} <= set(trace[0][len("trace="):].split(",")))

    def test_v4_requires_strict_with_a_runtime_context(self) -> None:
        with self.assertRaises(ra.AuditError):
            ra.strace_argv(["echo"], output=self.OUTPUT, roots=["/"], trace_contract="v4")
        with self.assertRaises(ra.AuditError):
            ra.strace_argv(["echo"], output=self.OUTPUT, roots=["/"], strict=True, trace_contract="v4")


# =====================================================================================================================
# R2-5 私有盘内符号链接探测（未放宽：依赖输出归属先接受符号链接创建）
# =====================================================================================================================


class SymlinkProbeStillRejectedTests(CorpusFixture):
    """git 在临时盘里做符号链接支持探测：O_EXCL 建同名文件再删，再 symlinkat、lstat、unlinkat（语料里 19 次）。

    这里**固定当前的失败关闭行为**，而不是期望它通过：解析器登记 symlink-metadata-not-bound；输出归属跟踪器另外拒绝
    私有盘内的符号链接创建（“私有临时盘内不允许创建符号链接”），于是链接名字未被证明、后续 lstat 与 unlinkat 也各被拒绝。
    只放宽解析器那一条没有意义——重放仍会因归属问题拒绝整份轨迹；要放行必须先让归属接受符号链接创建并保证之后没有任何
    “跟随链接”的访问（见调度者记忆里 10-14 对 v2 原型的结论），那是更大的、需要单独评审的改动。有人做了这个改动时，
    本类的断言应当被有意识地改写，而不是悄悄通过。
    """

    def lines(self) -> list[str]:
        base = [f'1 mkdir("{self.root}/tmpwzmo6c4w", 0700) = 0', f'1 mkdir("{self.root}/tmpwzmo6c4w/history", 0700) = 0']
        return base + RAW_GIT_PROBE

    def test_the_probe_is_unresolved_and_rejected_by_the_tracker(self) -> None:
        document = self.document(self.lines())
        self.assertEqual(document["coverage"]["unresolved_reasons"], {"symlink-metadata-not-bound:newfstatat": 1})
        problems = document["runtime"]["outputs"]["problems"]
        self.assertTrue(any("私有临时盘内不允许创建符号链接" in text for text in problems), problems)
        self.assertTrue(any("首次读取前未证明由本轮生成" in text for text in problems), problems)
        self.assertTrue(any("删除未证明的输出" in text for text in problems), problems)
        self.assertRejected(document)

    def test_the_same_probe_under_v3_has_the_same_unresolved_reason(self) -> None:
        self.assertEqual(self.reasons(self.lines(), "v3"), {"symlink-metadata-not-bound:newfstatat": 1})

    def test_lstat_of_a_regular_file_is_not_the_problem(self) -> None:
        """对照：lstat 一个普通文件（完整解码为 S_IFREG）早就不登记 symlink-metadata-not-bound，缺口只在真链接。"""
        line = RAW_GIT_PROBE[4].replace("S_IFLNK|0777", "S_IFREG|0644")
        self.assertEqual(self.reasons([line]), {})


# =====================================================================================================================
# R2-6 输出事件上限
# =====================================================================================================================


class OutputEventLimitTests(CorpusFixture):
    """v4 上限单列、有版本语义；v3 与 control 四链的上限不变；被旧上限截断的轨迹永远验真失败。"""

    OBSERVED_VC6_BATCH_EVENTS = 200_000 + 358_504

    def test_constants(self) -> None:
        self.assertEqual((ra.MAX_OUTPUT_EVENTS, runtime.MAX_OUTPUT_EVENTS), (200_000, 200_000), "v3 的上限不变")
        self.assertEqual(ra.MAX_OUTPUT_EVENTS_V4, runtime.MAX_OUTPUT_EVENTS_V4, "解析端与重放端的 v4 上限必须相等")
        self.assertEqual(ra.MAX_OUTPUT_EVENTS_V4, 1_000_000)
        self.assertGreater(ra.MAX_OUTPUT_EVENTS_V4, self.OBSERVED_VC6_BATCH_EVENTS, "必须容得下实测需求")
        for module in (ra, runtime):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertRegex(source, r"(?m)^MAX_OUTPUT_EVENTS_V4 = \d+$")

    def test_control_sidecar_limits_are_untouched(self) -> None:
        text = (Path(ra.__file__).with_name("control_read_audit.py")).read_text(encoding="utf-8")
        self.assertEqual(text.count("< 200000"), 2)
        self.assertNotIn("MAX_OUTPUT_EVENTS_V4", text)

    def distinct_reads(self, count: int) -> list[str]:
        """互不相同（无法去重）的输出事件：每条创建一个新名字。"""
        return [f'1 openat(AT_FDCWD</repo>, "{self.root}/base/f{i}", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{self.root}/base/f{i}>'
                for i in range(count)]

    def test_the_v4_parser_stops_at_its_own_limit_and_registers_output_event_limit(self) -> None:
        lines = self.pad(self.distinct_reads(30))
        with mock.patch.object(ra, "MAX_OUTPUT_EVENTS_V4", 20):
            document = self.document(lines)
            self.assertEqual(len(document["runtime"]["output_events"]), 20)
            self.assertGreater(document["coverage"]["unresolved_reasons"]["output-event-limit"], 0)
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_the_v3_parser_is_not_affected_by_the_v4_limit(self) -> None:
        lines = self.pad(self.distinct_reads(30))
        with mock.patch.object(ra, "MAX_OUTPUT_EVENTS_V4", 5):
            v3 = self.document(lines, contract="v3")
        self.assertGreater(len(v3["runtime"]["output_events"]), 5)
        self.assertNotIn("output-event-limit", v3["coverage"]["unresolved_reasons"])
        with mock.patch.object(ra, "MAX_OUTPUT_EVENTS", 7):
            v3 = self.document(lines, contract="v3")
        self.assertEqual(len(v3["runtime"]["output_events"]), 7, "v3 仍按 MAX_OUTPUT_EVENTS 截断")
        self.assertGreater(v3["coverage"]["unresolved_reasons"]["output-event-limit"], 0)

    def test_a_trace_truncated_by_an_old_limit_still_fails_after_the_limit_is_raised(self) -> None:
        """版本语义：被旧上限截断的轨迹带 output-event-limit（coverage.unresolved>0），验真判失败，与现行上限数值无关。"""
        lines = self.pad(self.distinct_reads(30))
        with mock.patch.object(ra, "MAX_OUTPUT_EVENTS_V4", 20):
            truncated = self.document(lines)
        self.assertGreater(truncated["coverage"]["unresolved"], 0)
        # 现行（调高之后的）上限下审计：整份轨迹仍判失败，不被重新解释为通过（覆盖信息里的未解析数先拦下；
        # 事件被截断后，归属重放也对不上解析器当时的归属报告，同样拒绝）。
        report = self.audit(truncated)
        self.assertFalse(report["coverage_complete"])
        self.assertIn("audit", {item["root"] for item in report["undeclared"]})
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.replay_runtime(truncated, [self.entry])
        # 与未被截断的同一流对照：现行上限下完整通过。
        self.assertAccepted(self.document(lines))

    def test_the_v4_replay_gate_uses_the_v4_limit_and_the_v3_gate_stays_200000(self) -> None:
        document = self.document(self.pad(self.distinct_reads(3)))
        template = document["runtime"]["output_events"][0]
        with mock.patch.object(runtime, "MAX_OUTPUT_EVENTS_V4", 50):
            over = copy.deepcopy(document)
            over["runtime"]["output_events"] = [dict(template, call=i + 1) for i in range(51)]
            with self.assertRaisesRegex(runtime.RuntimeContractError, "超过上限"):
                runtime.replay_runtime(over, [self.entry])
            at_limit = copy.deepcopy(document)
            at_limit["runtime"]["output_events"] = [dict(template, call=i + 1) for i in range(50)]
            with self.assertRaises(runtime.RuntimeContractError) as raised:
                runtime.replay_runtime(at_limit, [self.entry])
            self.assertNotIn("超过上限", str(raised.exception), "恰好等于上限不算超限（之后因别的校验失败是预期）")
        v3 = self.document(self.pad(self.distinct_reads(3)), contract="v3")
        v3_template = v3["runtime"]["output_events"][0]
        with mock.patch.object(runtime, "MAX_OUTPUT_EVENTS_V4", 10_000_000):
            v3["runtime"]["output_events"] = [dict(v3_template)] * 200_001
            with self.assertRaisesRegex(runtime.RuntimeContractError, "超过上限"):
                runtime.replay_runtime(v3, [self.entry])

    def test_a_v4_document_between_the_old_and_new_limit_is_accepted_by_the_size_gate(self) -> None:
        """旧上限 200,000 到新上限之间的条数：重放的大小门不再拒绝（调小常量验证机制，不真的构造 20 万条）。"""
        document = self.document(self.pad(self.distinct_reads(120)))
        count = len(document["runtime"]["output_events"])
        self.assertGreater(count, 100)
        with mock.patch.object(runtime, "MAX_OUTPUT_EVENTS", 100), mock.patch.object(runtime, "MAX_OUTPUT_EVENTS_V4", 100_000):
            runtime.replay_runtime(document, [self.entry])
        with mock.patch.object(runtime, "MAX_OUTPUT_EVENTS_V4", 100):
            with self.assertRaisesRegex(runtime.RuntimeContractError, "超过上限"):
                runtime.replay_runtime(document, [self.entry])


# =====================================================================================================================
# R2-7 openat 三联（无法复现；只固定产生这三个原因的行形态假设，不放宽）
# =====================================================================================================================


class OpenatTripleHypothesisTests(CorpusFixture):
    """census 里 openat 同时出现 dirfd-path-not-resolved、opened-fd-not-resolved、result-or-string-incomplete 各 1～5 次，
    语料里没有触发行。读代码得出：这三个原因同时出现，当且仅当该行末尾的返回值段没有被 ``_RESULT`` 匹配（result 为 None →
    ``result-or-string-incomplete``；参数切分把右括号当成多余 → 参数为空 → ``dirfd-path-not-resolved``；没有返回描述符标注
    → ``opened-fd-not-resolved``）。能让返回值段不匹配的形态假设：

    * A：流在一行中间被截断（strace 被杀、管道提前关闭，末行不完整）；
    * B：返回值段带了 ``_RESULT`` 不认识的尾巴或错误码拼写（例如含下划线的 ``ERRNO_n``）；
    * C：返回描述符标注里有未转义的 ``>``（strace 通常转义，若不转义就会触发）。

    三个都是“看不清到底打开了什么”，失败关闭本来就是对的，没有安全的放宽；这里只固定它们产生的原因集合，供下一次在 ARM64 上
    用钩子抓到真实原始行时对照，不改变任何判定。
    """

    TRIPLE = {"dirfd-path-not-resolved:openat": 1, "opened-fd-not-resolved:openat": 1, "result-or-string-incomplete:openat": 1}

    def test_hypothesis_lines_produce_exactly_the_triple_and_fail_closed(self) -> None:
        cases = {
            "A 末行被截断": '1 openat(AT_FDCWD</repo>, "/tmp/some/very/lo',
            "A2 截断在返回值之前": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY|O_CLOEXEC) =',
            "B 未知错误码拼写": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = -1 ERRNO_133 (errno 133)',
            "B2 返回值带多余尾巴": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = 3</tmp/f> <0.000012>',
            "C 返回标注含未转义 >": '1 openat(AT_FDCWD</repo>, "/tmp/a>b", O_RDONLY) = 3</tmp/a>b>',
        }
        for contract in ("v3", "v4"):
            for label, line in cases.items():
                with self.subTest(contract=contract, label=label):
                    self.assertEqual(self.reasons([line], contract), self.TRIPLE)
                    self.assertFalse(self.audit(self.document(self.pad([line]), contract=contract))["coverage_complete"])

    def test_neighbouring_shapes_give_only_a_subset(self) -> None:
        """排除法：这些形态只产生三个原因里的一部分，所以不是三联的来源。"""
        cases = {
            "字符串被 -s 4096 截断": ('1 openat(AT_FDCWD</repo>, "/tmp/' + "x" * 5000 + '"..., O_RDONLY) = -1 ENAMETOOLONG (File name too long)',
                                  {"dirfd-path-not-resolved:openat", "result-or-string-incomplete:openat"}),
            "成功但返回描述符无标注": ('1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = 3', {"opened-fd-not-resolved:openat"}),
            "路径参数是指针": ('1 openat(AT_FDCWD</repo>, 0xffffc0de, O_RDONLY) = -1 EFAULT (Bad address)',
                          {"dirfd-path-not-resolved:openat"}),
            "目录描述符无标注": ('1 openat(3, "f", O_RDONLY) = 4</tmp/f>', {"dirfd-path-not-resolved:openat"}),
        }
        for label, (line, expected) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(set(self.reasons([line])), expected, label)


# =====================================================================================================================
# 旧语料对照：同样的原始行，v3 的结论与改动前（census 表）逐项一致
# =====================================================================================================================


class LegacyCorpusUnchangedTests(CorpusFixture):
    def test_census_reasons_under_v3_are_exactly_the_old_ones(self) -> None:
        lines = [RAW_GETCWD_ERANGE_129, RAW_GETCWD_ERANGE_257, RAW_SOCKET, RAW_CONNECT_POINTER, RAW_FUTIMENS, RAW_OPENAT2_POINTER,
                 RAW_GIT_PROBE[4]]
        self.assertEqual(self.reasons(lines, "v3"), {
            "plain-path-not-resolved:getcwd": 2, "unsupported-call:socket": 1, "unsupported-call:connect": 1,
            "dirfd-path-not-resolved:utimensat": 1, "open-flags-not-decoded:openat2": 1, "symlink-metadata-not-bound:newfstatat": 1})

    def test_census_reasons_under_v4_keep_only_what_is_not_relaxed(self) -> None:
        lines = [RAW_GETCWD_ERANGE_129, RAW_GETCWD_ERANGE_257, RAW_SOCKET, RAW_CONNECT_POINTER, RAW_FUTIMENS, RAW_OPENAT2_POINTER,
                 RAW_GIT_PROBE[4]]
        self.assertEqual(self.reasons(lines, "v4"), {
            "unsupported-call:connect": 1, "open-flags-not-decoded:openat2": 1, "symlink-metadata-not-bound:newfstatat": 1})

    def test_v3_documents_are_still_produced_without_any_wp_r2_behaviour(self) -> None:
        """v3 轨迹（含 control 四链复用的默认路径）的文档 schema 与事件键不变。"""
        document = self.document(self.pad([RAW_GETCWD_ERANGE_129]), contract="v3")
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
        self.assertEqual(document["coverage"]["unresolved_reasons"], {"plain-path-not-resolved:getcwd": 1})
        self.assertTrue(all(set(e) == {"path", "kind", "name", "succeeded", "flags", "target"}
                            for e in document["runtime"]["output_events"]))


# =====================================================================================================================
# 整份语料重放（可选：证据目录在本机才跑）
# =====================================================================================================================

CORPUS_DIR = Path(os.environ["WP_R2_CORPUS_DIR"]) if os.environ.get("WP_R2_CORPUS_DIR") else None


@unittest.skipUnless(CORPUS_DIR is not None and CORPUS_DIR.is_dir(),
                     "整份语料重放默认不跑（约 1 分钟）：设置环境变量 WP_R2_CORPUS_DIR=<含 raw-diag-*.gz 的目录> 才启用")
class FullCorpusReplayTests(CorpusFixture):
    """把三份原始语料（约 25 万行，ARM64 上与审计同选项抓的）整份喂给 v3／v4 解析器，核对未解析原因集合。

    语料在调度者证据目录 ``claude-control-plane/20261015/step10/raw-corpus``（raw-diag-ingress／directed／driverwait.gz）；
    不进仓库，所以只在设置了 ``WP_R2_CORPUS_DIR`` 时运行。语料的 sockaddr 是未解码的指针（抓取时还没有新选项），所以 connect
    在 v4 下仍判不支持；另做一遍“把指针换成调度者实测的解码形态”的变换，验证加了新选项之后这批 connect 全部变成对
    /var/run/nscd/socket 的 missing 探测。
    """

    CASES = {
        "raw-diag-ingress.gz": ("fe5a72c41d005e9f10cd39d20108a19e014541545c03e43877b9a3c6a1ce3786",
                                {"unsupported-call:socket": 304, "unsupported-call:connect": 304},
                                {"unsupported-call:connect": 304}),
        "raw-diag-directed.gz": ("b3331ceddec57943ade2119907d315d6d2a6b375869bc4322c023fb2bb84fc34",
                                 {"unsupported-call:socket": 52, "unsupported-call:connect": 52},
                                 {"unsupported-call:connect": 52}),
        "raw-diag-driverwait.gz": ("3a7bcad2fb7a4709bed86600bf4f3c9b510f476f94b210d6df8da35370d3efd1",
                                   {"plain-path-not-resolved:getcwd": 4904, "unsupported-call:socket": 38, "unsupported-call:connect": 38,
                                    "symlink-metadata-not-bound:newfstatat": 19, "dirfd-path-not-resolved:utimensat": 9,
                                    "open-flags-not-decoded:openat2": 3},
                                   {"unsupported-call:connect": 38, "symlink-metadata-not-bound:newfstatat": 19,
                                    "open-flags-not-decoded:openat2": 3}),
    }
    POINTER_CONNECT = re.compile(r"(connect\(\d+<socket:\[\d+\]>, )0x[0-9a-f]+(, 110\b)")      # 含 unfinished 拆行的写法

    def corpus_lines(self, name: str, transform: bool = False):
        with gzip.open(CORPUS_DIR / name, "rt", encoding="utf-8", errors="surrogateescape") as stream:
            for line in stream:
                line = line.rstrip("\n")
                if transform:
                    line = self.POINTER_CONNECT.sub(r'\1{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}\2', line)
                yield line

    def run_corpus(self, name: str, root_hash: str, contract: str, transform: bool = False) -> ra.TraceParser:
        self.use_root(WORK + "/runtime/" + root_hash)
        parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract=contract)
        for line in self.corpus_lines(name, transform):
            parser.feed(line)
        return parser

    def test_v3_reasons_are_exactly_the_census_ones_and_v4_keeps_only_what_is_not_relaxed(self) -> None:
        for name, (root_hash, v3_expected, v4_expected) in self.CASES.items():
            with self.subTest(corpus=name, contract="v3"):
                self.assertEqual(self.run_corpus(name, root_hash, "v3").unresolved_reasons, v3_expected)
            with self.subTest(corpus=name, contract="v4"):
                self.assertEqual(self.run_corpus(name, root_hash, "v4").unresolved_reasons, v4_expected)

    def test_with_the_decoded_sockaddr_every_connect_becomes_one_missing_probe(self) -> None:
        for name, (root_hash, _v3, v4_expected) in self.CASES.items():
            with self.subTest(corpus=name):
                parser = self.run_corpus(name, root_hash, "v4", transform=True)
                expected = {key: value for key, value in v4_expected.items() if key != "unsupported-call:connect"}
                self.assertEqual(parser.unresolved_reasons, expected)
                self.assertEqual(parser.accesses["/var/run/nscd/socket"], {"missing"})


if __name__ == "__main__":
    unittest.main()
