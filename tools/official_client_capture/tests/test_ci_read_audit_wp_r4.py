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
                                                         stat，本模块固定它，并固定 /dev/tty
                                                         成功打开、/dev/stdin、/proc/self/fd/N→/dev/null 别名仍被拒绝）
========  ===========================================  ==========================================================
"""

from __future__ import annotations

import os
import unittest
from typing import Any

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


# harvest5 里 compare_accept／vc6_batch 的 symlink-metadata-not-bound:newfstatat（同一路径至少 24 次）。样本在 600 字符处被诊断脚本截断，
# 结尾 ``AT_SYMLINK_NOFOLLOW) = 0`` 是按 strace 格式补全的；其余逐字取自样本。
RAW_LSTAT_PYTHON3 = (
    f'1130194 newfstatat(AT_FDCWD<{WORK}/test-tree>, "/usr/bin/python3", {{st_dev=makedev(0x8, 0x1), st_ino=4175, st_mode=S_IFLNK|0777, '
    'st_nlink=1, st_uid=0, st_gid=0, st_blksize=4096, st_blocks=0, st_size=10, st_atime=1792034259 /* 2026-10-15T03:17:39.027062149+0000 */, '
    'st_atime_nsec=27062149, st_mtime=1762949744 /* 2025-11-12T12:15:44+0000 */, st_mtime_nsec=0, '
    'st_ctime=1765539113 /* 2025-12-12T11:31:53.958971067+0000 */, st_ctime_nsec=958971067}, AT_SYMLINK_NOFOLLOW) = 0')


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

    def host_document(self, lines: list[str], paths: list[str] | tuple[str, ...] = ()) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """带宿主快照的 v4 文档：``paths`` 里的每个路径取现场 v3 快照作为登记条目，结束凭证按真实合同写好。"""
        entries = [ra.host_snapshot_v3(str(path)) for path in paths]
        document = ra.filter_stream(lines, ["/"], strict=True, runtime_context=self.context, trace_contract="v4")
        extra: dict[str, Any] = {"host_entries_sha256": runtime.host_binding_digest(runtime._read_audit(), entries)}
        try:
            extra["host_path_modes"] = ra.host_path_modes(document, entries)
        except ra.AuditError:
            pass   # 轨迹不完整：真实凭证同样不写访问类型，重放据此拒绝
        return self.completed(document, **extra), entries

    def host_report(self, lines: list[str], paths: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        document, entries = self.host_document(lines, paths)
        return ra.strict_audit_reads(document, [self.entry, *entries], repo_root="/repo", data_root=None)


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


# =====================================================================================================================
# R6-2  宿主上真符号链接的 lstat
# =====================================================================================================================


class HostSymlinkLstatTests(R4Fixture):
    """``os.path.realpath(sys.executable)``／``Path.resolve()`` 逐分量 lstat 到宿主真链接。

    结论：lstat 暴露的是链接自身的 mode／uid／gid／size（＝链接文本长度），正是 v3 快照 ``links`` 条目绑定的字段；dev／ino／时间／
    nlink／blocks 不在快照里（与普通文件 stat 同口径，区别见 ``read_audit.TraceParser._host_symlink_lstat``）。所以登记为对该路径的
    stat 访问，由覆盖检查要求该路径有 v3 快照。
    """

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.base / "usr" / "bin"
        self.bin.mkdir(parents=True)
        (self.bin / "python3.12").write_text("#!/bin/sh\n", encoding="utf-8")
        os.symlink("python3.12", self.bin / "python3")
        self.link = str(self.bin / "python3")
        self.target = str(self.bin / "python3.12")

    # ---- 行与结构的拼装 ----
    def link_struct(self, path: str | None = None, *, mode: str = "S_IFLNK|0777", drop: tuple[str, ...] = (), extra: tuple[str, ...] = (),
                    **override: Any) -> str:
        """lstat 返回结构：数值取自真实 ``os.lstat``（所以 uid／gid 是测试进程自己的，不是 0），可逐项覆盖、删除、追加。"""
        info = os.lstat(path or self.link)
        values = {"st_dev": "makedev(0x8, 0x1)", "st_ino": str(info.st_ino), "st_mode": mode, "st_nlink": "1", "st_uid": str(info.st_uid),
                  "st_gid": str(info.st_gid), "st_blksize": "4096", "st_blocks": "0", "st_size": str(info.st_size),
                  "st_atime": "1792034259 /* 2026-10-15T03:17:39.027062149+0000 */", "st_atime_nsec": "27062149",
                  "st_mtime": "1762949744 /* 2025-11-12T12:15:44+0000 */", "st_mtime_nsec": "0",
                  "st_ctime": "1765539113 /* 2025-12-12T11:31:53.958971067+0000 */", "st_ctime_nsec": "958971067"}
        values.update({key: str(value) for key, value in override.items()})
        fields = [f"{key}={value}" for key, value in values.items() if key not in drop] + list(extra)
        return "{" + ", ".join(fields) + "}"

    def statx_struct(self, path: str | None = None, *, mode: str = "S_IFLNK|0777") -> str:
        info = os.lstat(path or self.link)
        return (f"{{stx_mask=STATX_BASIC_STATS|STATX_MNT_ID, stx_blksize=4096, stx_attributes=0, stx_nlink=1, stx_uid={info.st_uid}, "
                f"stx_gid={info.st_gid}, stx_mode={mode}, stx_ino={info.st_ino}, stx_size={info.st_size}, stx_blocks=0, "
                "stx_attributes_mask=STATX_ATTR_COMPRESSED|STATX_ATTR_IMMUTABLE, "
                "stx_atime={tv_sec=1792047380, tv_nsec=696287249} /* 2026-10-15T06:56:20.696287249+0000 */, "
                "stx_rdev_major=0, stx_rdev_minor=0, stx_dev_major=0, stx_dev_minor=105}")

    def lstat_line(self, path: str | None = None, call: str = "newfstatat", *, struct: str | None = None, flags: str = "AT_SYMLINK_NOFOLLOW",
                   tail: str = "0", dirfd: str = "/repo") -> str:  # type: ignore[override]
        path = path or self.link
        if call in {"lstat", "lstat64"}:
            return f'1 {call}("{path}", {struct or self.link_struct(path)}) = {tail}'
        if call == "statx":
            return (f'1 statx(AT_FDCWD<{dirfd}>, "{path}", AT_STATX_SYNC_AS_STAT|{flags}, STATX_BASIC_STATS|STATX_MNT_ID, '
                    f'{struct or self.statx_struct(path)}) = {tail}')
        return f'1 {call}(AT_FDCWD<{dirfd}>, "{path}", {struct or self.link_struct(path)}, {flags}) = {tail}'

    def accepted_as_stat(self, line: str, path: str | None = None, *, reasons: dict[str, int] | None = None) -> None:
        parser = self.parse([line])
        self.assertEqual((parser.unresolved_reasons, parser.accesses), (reasons or {}, {path or self.link: {"stat"}}), line[:160])

    # ---- 正例 ----
    def test_the_harvest_line_is_a_stat_of_the_host_path_not_unresolved(self) -> None:
        parser = self.parse([RAW_LSTAT_PYTHON3])
        self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {"/usr/bin/python3": {"stat"}}))
        self.assertEqual(self.reasons_of([RAW_LSTAT_PYTHON3], "v3"), {"symlink-metadata-not-bound:newfstatat": 1}, "v3 一字不变")

    def test_every_lstat_spelling_is_accepted(self) -> None:
        for call in ("newfstatat", "fstatat64", "lstat", "lstat64", "statx"):
            with self.subTest(call=call):
                self.accepted_as_stat(self.lstat_line(call=call))

    def test_a_relative_path_resolves_against_the_annotated_directory_fd(self) -> None:
        line = f'1 newfstatat(3<{self.bin}>, "python3", {self.link_struct()}, AT_SYMLINK_NOFOLLOW) = 0'
        self.accepted_as_stat(line)

    def test_a_plain_lstat_resolves_relative_paths_against_the_working_directory(self) -> None:
        lines = [f'1 chdir("{self.bin}") = 0', f'1 lstat("python3", {self.link_struct()}) = 0']
        parser = self.parse(lines)
        self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {str(self.bin): {"dir"}, self.link: {"stat"}}))

    def test_uid_and_gid_need_not_be_zero_because_the_snapshot_binds_them(self) -> None:
        """不要求 uid／gid 为 0：快照 links 条目绑定了它们（见下面的 ``test_the_v3_snapshot_binds_every_field_lstat_can_observe``）。"""
        for uid, gid in ((0, 0), (501, 20), (65534, 65534), (4294967295, 4294967295)):
            with self.subTest(uid=uid, gid=gid):
                self.accepted_as_stat(self.lstat_line(struct=self.link_struct(st_uid=uid, st_gid=gid)))

    def test_lstat_of_a_regular_file_or_directory_is_not_this_class_and_unchanged(self) -> None:
        for mode in ("S_IFREG|0644", "S_IFDIR|0755"):
            with self.subTest(mode=mode):
                self.accepted_as_stat(self.lstat_line(struct=self.link_struct(mode=mode)))

    def test_a_following_stat_is_not_this_class(self) -> None:
        """没有 AT_SYMLINK_NOFOLLOW 的 stat 是跟随式，内核不会返回 S_IFLNK；这里即使硬造这样的结构也不走本类（不登记原因、也不放行）。"""
        line = f'1 newfstatat(AT_FDCWD</repo>, "{self.link}", {self.link_struct()}, 0) = 0'
        self.accepted_as_stat(line)

    def test_a_failed_lstat_is_still_a_missing_probe(self) -> None:
        line = f'1 newfstatat(AT_FDCWD</repo>, "{self.link}x", 0x7ffd1234, AT_SYMLINK_NOFOLLOW) = -1 ENOENT (No such file or directory)'
        parser = self.parse([line])
        self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {self.link + "x": {"missing"}}))

    # ---- 端到端：覆盖检查要求该路径有 v3 快照 ----
    def test_the_audit_needs_the_path_to_be_declared_and_then_passes(self) -> None:
        lines = self.pad([self.lstat_line()])
        report = self.host_report(lines)
        self.assertEqual((report["status"], [(item["root"], item["path"], item["kinds"]) for item in report["undeclared"]]),
                         ("failed", [("host", self.link, ["stat"])]), "没声明：未声明项（声明缺口由策略补）")
        report = self.host_report(lines, [self.link])
        self.assertEqual((report["status"], report["undeclared_count"]), ("passed", 0), report["undeclared"])

    def test_the_v3_snapshot_binds_every_field_lstat_can_observe(self) -> None:
        """核对放宽的依据：链接自己的 mode／uid／gid／size／链接文本都在 links 条目里，进条目摘要，比较时无条件比较。"""
        entry = ra.host_snapshot_v3(self.link)
        links = entry["detail"]["snapshot"]["links"]
        info = os.lstat(self.link)
        self.assertEqual(links, [{"path": self.link, "target": "python3.12", "mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid,
                                  "size": info.st_size}])
        self.assertEqual(tuple(links[0]), ra._V3_LINK_FIELDS)
        planned = entry["detail"]["snapshot"]
        for key, changed in (("mode", info.st_mode ^ 0o7), ("uid", info.st_uid + 1), ("gid", info.st_gid + 1), ("size", info.st_size + 1),
                             ("target", "python3.13")):
            with self.subTest(field=key):
                tampered = {**planned, "links": [{**links[0], key: changed}]}
                for kinds in (["stat"], None, []):
                    ok, reason = ra.host_snapshot_matches(planned, tampered, kinds, ())
                    self.assertEqual((ok, reason), (False, ra._V3_REASONS["links"]), (key, kinds))
                self.assertNotEqual(ra.host_entry_v3(self.link, tampered)["sha256"], entry["sha256"], "条目摘要随之改变")

    def test_replacing_the_link_after_the_snapshot_fails_the_audit_closed(self) -> None:
        lines = self.pad([self.lstat_line()])
        document, entries = self.host_document(lines, [self.link])
        os.unlink(self.link)
        (self.bin / "python3.13").write_text("x", encoding="utf-8")
        os.symlink("python3.13", self.link)
        report = ra.strict_audit_reads(document, [self.entry, *entries], repo_root="/repo", data_root=None)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(item["root"] == "host" and "漂移" in item["suggestion"] for item in report["undeclared"]), report["undeclared"])

    def test_a_dangling_link_has_a_missing_snapshot_and_stays_closed(self) -> None:
        dangling = str(self.bin / "dangling")
        os.symlink("nowhere", dangling)
        report = self.host_report(self.pad([self.lstat_line(dangling)]), [dangling])
        self.assertEqual((report["status"], [item["path"] for item in report["undeclared"]]), ("failed", [dangling]))

    def test_a_realpath_walk_needs_each_component_declared(self) -> None:
        """realpath(sys.executable)：逐级 lstat 目录（普通对象，沿用旧规则）、lstat 链接（本类）、再读目标。"""
        lines = self.pad([self.lstat_line(str(self.bin.parent), struct=self.link_struct(str(self.bin.parent), mode="S_IFDIR|0755")),
                          self.lstat_line(str(self.bin), struct=self.link_struct(str(self.bin), mode="S_IFDIR|0755")),
                          self.lstat_line(),
                          f'1 openat(AT_FDCWD</repo>, "{self.target}", O_RDONLY|O_CLOEXEC) = 3<{self.target}>'])
        declared = [str(self.bin.parent), str(self.bin), self.link, self.target]
        self.assertEqual(self.host_report(lines, declared)["status"], "passed")
        for missing in declared:
            with self.subTest(undeclared=missing):
                report = self.host_report(lines, [path for path in declared if path != missing])
                self.assertEqual((report["status"], [item["path"] for item in report["undeclared"]]), ("failed", [missing]))

    # ---- 反例：返回结构 ----
    def test_a_mode_other_than_exactly_lnk_0777_stays_unresolved(self) -> None:
        for mode in ("S_IFLNK|0755", "S_IFLNK|0666", "S_IFLNK|0777|S_ISVTX", "S_IFLNK", "S_IFLNK|0000", "S_IFLNK|777"):
            with self.subTest(mode=mode):
                self.assertEqual(self.reasons_of([self.lstat_line(struct=self.link_struct(mode=mode))]),
                                 {"symlink-metadata-not-bound:newfstatat": 1})

    def test_uid_gid_and_size_must_each_be_one_decimal_integer(self) -> None:
        cases = {
            "缺 uid": self.link_struct(drop=("st_uid",)), "缺 gid": self.link_struct(drop=("st_gid",)), "缺 size": self.link_struct(drop=("st_size",)),
            "重复 uid": self.link_struct(extra=("st_uid=0",)), "重复 gid": self.link_struct(extra=("st_gid=0",)),
            "重复 size": self.link_struct(extra=("st_size=10",)),
            "uid 是名字": self.link_struct(st_uid="root"), "gid 是名字": self.link_struct(st_gid="wheel"),
            "size 是十六进制": self.link_struct(st_size="0xa"), "size 是负数": self.link_struct(st_size="-1"),
            "uid 带前导零": self.link_struct(st_uid="00"), "size 为 0（procfs 魔法链接那种）": self.link_struct(st_size="0"),
            "size 带单位": self.link_struct(st_size="10B"),
        }
        for label, struct in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.reasons_of([self.lstat_line(struct=struct)]), {"symlink-metadata-not-bound:newfstatat": 1}, label)

    def test_truncated_pointer_or_missing_structures_stay_unresolved(self) -> None:
        cases = {"缩略结构": "{st_mode=S_IFLNK|0777, ...}", "返回结构是指针": "0x7ffd1234",
                 "只有 mode": "{st_mode=S_IFLNK|0777}", "类型是字符设备": self.link_struct(mode="S_IFCHR|0666"),
                 "类型是套接字": self.link_struct(mode="S_IFSOCK|0755")}
        for label, struct in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.reasons_of([self.lstat_line(struct=struct)]), {"symlink-metadata-not-bound:newfstatat": 1}, label)
        # 结构没有右括号：整个调用解析不出参数，除了本原因还会多一条 dirfd-path-not-resolved（都是未解析，整次审计失败关闭）。
        broken = self.reasons_of([self.lstat_line(struct="{st_mode=S_IFLNK|0777")])
        self.assertIn("symlink-metadata-not-bound:newfstatat", broken)

    def test_statx_structure_is_checked_with_stx_field_names(self) -> None:
        good = self.statx_struct()
        self.accepted_as_stat(self.lstat_line(call="statx", struct=good))
        for label, struct in {"类型是字符设备": self.statx_struct(mode="S_IFCHR|0666"),
                              "权限不是 0777": self.statx_struct(mode="S_IFLNK|0755"),
                              "缺 uid": good.replace(f"stx_uid={os.lstat(self.link).st_uid}, ", ""),
                              "用了 st_ 前缀的字段名": good.replace("stx_", "st_")}.items():
            with self.subTest(label=label):
                self.assertEqual(self.reasons_of([self.lstat_line(call="statx", struct=struct)]),
                                 {"symlink-metadata-not-bound:statx": 1}, label)

    # ---- 反例：路径写法 ----
    def test_path_spellings_that_can_make_the_normalised_path_wrong_stay_unresolved(self) -> None:
        cases = {
            "含 .. 分量": f"{self.bin}/../bin/python3", "含 . 分量": f"{self.bin}/./python3", "结尾斜杠": self.link + "/",
            "结尾 /.": self.link + "/.", "双斜杠": f"{self.bin}//python3", "以 .. 结尾": f"{self.link}/..",
        }
        for label, raw in cases.items():
            for call in ("newfstatat", "lstat"):
                with self.subTest(label=label, call=call):
                    line = self.lstat_line(raw, call=call, struct=self.link_struct())
                    self.assertEqual(self.reasons_of([line]), {f"symlink-metadata-not-bound:{call}": 1}, label)

    def test_relative_spellings_with_dot_components_stay_unresolved(self) -> None:
        for raw in ("../bin/python3", "./python3", "."):
            with self.subTest(raw=raw):
                line = f'1 newfstatat(3<{self.bin}>, "{raw}", {self.link_struct()}, AT_SYMLINK_NOFOLLOW) = 0'
                self.assertEqual(self.reasons_of([line]), {"symlink-metadata-not-bound:newfstatat": 1})

    def test_an_empty_path_with_at_empty_path_stays_unresolved(self) -> None:
        line = f'1 newfstatat(3<{self.link}>, "", {self.link_struct()}, AT_EMPTY_PATH|AT_SYMLINK_NOFOLLOW) = 0'
        self.assertEqual(self.reasons_of([line]), {"symlink-metadata-not-bound:newfstatat": 1})

    # ---- 反例：魔法链接与私有盘内 ----
    def test_procfs_magic_links_are_not_ordinary_host_links(self) -> None:
        """/proc/self、/proc/<pid>/exe、cwd、fd/N 是魔法链接：st_size 为 0（挡在本类之外），而且它们无法有 v3 快照（host_snapshot 拒绝 /proc）。"""
        for path in ("/proc/self", "/proc/self/exe", "/proc/self/cwd", "/proc/self/root", "/proc/self/fd/3", "/proc/1234/exe"):
            with self.subTest(path=path):
                line = self.lstat_line(path, struct=self.link_struct(st_size="0"))
                self.assertEqual(self.reasons_of([line]), {"symlink-metadata-not-bound:newfstatat": 1})
        with self.assertRaises(ra.AuditError):
            ra.host_snapshot_v3("/proc/self/exe")

    def test_a_link_in_the_private_root_that_this_unit_did_not_create_stays_unresolved(self) -> None:
        for path in (self.p("d/l"), self.p("d/never-created")):
            with self.subTest(path=path):
                line = f'1 newfstatat(AT_FDCWD</repo>, "{path}", {self.link_struct()}, AT_SYMLINK_NOFOLLOW) = 0'
                self.assertEqual(self.reasons_of(self.base_lines() + [line]), {"symlink-metadata-not-bound:newfstatat": 1})

    def test_a_link_this_unit_created_in_the_private_root_still_passes_via_the_tracker(self) -> None:
        lines = self.base_lines() + [self.symlink_line("d/l"), super().lstat_line("d/l")]
        self.assertPasses(lines)

    def test_the_mask_root_is_not_exempt(self) -> None:
        line = f'1 newfstatat(AT_FDCWD</repo>, "{runtime.MASK_ROOT}/link", {self.link_struct()}, AT_SYMLINK_NOFOLLOW) = 0'
        report = self.host_report(self.pad([line]))
        self.assertEqual(report["status"], "failed", "生产别名遮挡下只有根目录元数据与缺失探测被覆盖，链接的 stat 没有快照")

    def test_nothing_changes_for_v3_documents(self) -> None:
        for call in ("newfstatat", "lstat", "statx"):
            with self.subTest(call=call):
                self.assertEqual(self.reasons_of([self.lstat_line(call=call)], "v3"), {f"symlink-metadata-not-bound:{call}": 1})

    def test_the_host_rule_itself_requires_a_zero_return(self) -> None:
        """lstat 系成功只会返回 0，所以调用点不会传别的值；规则自己仍把“返回 0”写成条件（直接调用固定它，防止调用点以后变化）。"""
        parser = self.parse([])
        arguments = ["AT_FDCWD</repo>", f'"{self.link}"', self.link_struct(), "AT_SYMLINK_NOFOLLOW"]
        self.assertTrue(parser._host_symlink_lstat("newfstatat", arguments, "1", "0"))
        for returned in ("1", "-1", "3", "?"):
            with self.subTest(returned=returned):
                self.assertFalse(parser._host_symlink_lstat("newfstatat", arguments, "1", returned))


# =====================================================================================================================
# R4-1  自有进程树的 /proc/<pid>/… 与 /proc/self/…
# =====================================================================================================================

# 逐字取自 raw-diag-driverwait.gz／raw-diag-ingress.gz 的 /proc 访问（工作目录前缀写成 WORK）。
RAW_PROC_OWN_STAT = (f'3983820 openat(AT_FDCWD<{DRIVER_WAIT_ROOT}/tmpp0ryn47e>, "/proc/3983820/stat", O_RDONLY) = 3</proc/3983820/stat>')
RAW_PROC_OWN_STATUS = (f'3983820 openat(AT_FDCWD<{DRIVER_WAIT_ROOT}/tmpp0ryn47e>, "/proc/3983820/status", O_RDONLY) = 3</proc/3983820/status>')
RAW_PROC_SELF_STATUS = (f'3983820 openat(AT_FDCWD<{DRIVER_WAIT_ROOT}/tmpp0ryn47e>, "/proc/self/status", O_RDONLY) = 3</proc/3983820/status>')
RAW_PROC_SELF_MAPS = f'3965846 openat(AT_FDCWD<{WORK}/test-tree>, "/proc/self/maps", O_RDONLY) = 3</proc/3965846/maps>'
DIR_STRUCT = ('{st_dev=makedev(0, 0x1a), st_ino=811908183, st_mode=S_IFDIR|0555, st_nlink=9, st_uid=0, st_gid=0, st_blksize=1024, '
              'st_blocks=0, st_size=0, st_atime=1792047374 /* 2026-10-15T06:56:14.102163350+0000 */, st_atime_nsec=102163350, '
              'st_mtime=1792047374 /* 2026-10-15T06:56:14.102163350+0000 */, st_mtime_nsec=102163350, '
              'st_ctime=1792047374 /* 2026-10-15T06:56:14.102163350+0000 */, st_ctime_nsec=102163350}')
RAW_PROC_OWN_DIR = f'3983820 newfstatat(AT_FDCWD<{DRIVER_WAIT_ROOT}/tmpp0ryn47e>, "/proc/3983820", {DIR_STRUCT}, 0) = 0'
RAW_PROC_SELF_TASK = f'3983820 newfstatat(AT_FDCWD<{DRIVER_WAIT_ROOT}/tmpp0ryn47e>, "/proc/self/task", {DIR_STRUCT}, 0) = 0'
ENVIRONMENT_READS = ['/proc/filesystems', '/proc/uptime', '/proc/sys/crypto/fips_enabled', '/proc/self/mountinfo', '/proc/self/mounts',
                     '/proc/self/cgroup']


class ProcUnitInternalTests(R4Fixture):
    """``/proc/<pid>/{stat,status,maps,…}`` 只在 pid 属于本轨迹进程树时是单元内部状态。"""

    OWN = 100

    def open_line(self, pid: int, target: str, flags: str = "O_RDONLY|O_CLOEXEC", *, resolved: str | None = None, cwd: str = "/repo") -> str:
        resolved = target if resolved is None else resolved
        return f'{pid} openat(AT_FDCWD<{cwd}>, "{target}", {flags}) = 3<{resolved}>'

    def stat_line(self, pid: int, target: str, *, errno: str = "", cwd: str = "/repo") -> str:
        tail = f"= -1 {errno}" if errno else "= 0"
        return f'{pid} newfstatat(AT_FDCWD<{cwd}>, "{target}", {DIR_STRUCT if not errno else "0x7ffd1234"}, 0) {tail}'

    def clone_line(self, parent: int, child: int, flags: str = "CLONE_VM|CLONE_FS|CLONE_FILES|SIGCHLD") -> str:
        return f'{parent} clone(child_stack=NULL, flags={flags}) = {child}'

    def accesses(self, lines: list[str], contract: str = "v4") -> dict[str, set[str]]:
        parser = self.parse(lines, contract)
        self.assertEqual(parser.unresolved_reasons, {}, parser.unresolved_reasons)
        return parser.accesses

    # ---- 正例 ----
    def test_the_corpus_lines_of_the_own_process_are_not_host_accesses(self) -> None:
        lines = [RAW_PROC_OWN_STAT, RAW_PROC_OWN_STATUS, RAW_PROC_SELF_STATUS, RAW_PROC_OWN_DIR, RAW_PROC_SELF_TASK, RAW_PROC_SELF_MAPS]
        parser = self.parse(lines)
        self.assertEqual((parser.unresolved_reasons, parser.accesses), ({}, {}))
        self.assertEqual(parser.unit_proc_accesses, 2 + 2 + 2 + 1 + 1 + 2 - 0 + 0, "每条打开两次（请求路径＋描述符注解），目录 stat 一次")
        # 同样的行在 v3 下全部登记（一字不变）。
        legacy = self.parse(lines, "v3").accesses
        self.assertEqual(set(legacy), {"/proc/3983820/stat", "/proc/3983820/status", "/proc/self/status", "/proc/3983820", "/proc/self/task",
                                       "/proc/self/maps", "/proc/3965846/maps"})

    def test_the_whole_audit_passes_with_own_proc_reads_and_a_generated_output(self) -> None:
        lines = self.base_lines() + [self.create_line("d/a"), self.open_line(1, "/proc/self/stat", resolved="/proc/1/stat"),
                                     self.open_line(1, "/proc/1/status"), self.open_line(1, "/proc/self/maps", resolved="/proc/1/maps"),
                                     self.stat_line(1, "/proc/self/task"), self.stat_line(1, "/proc/1")]
        document = self.assertPasses(lines)
        self.assertEqual([path for path, _kinds in document["accesses"] if path.startswith("/proc")], [])

    def test_children_threads_and_resumed_clones_extend_the_traced_set(self) -> None:
        lines = [self.open_line(self.OWN, "/etc/hostname"),
                 self.clone_line(self.OWN, 200),                                          # fork
                 f'{self.OWN} clone(child_stack=0x7f00, flags=CLONE_VM|CLONE_THREAD|CLONE_SIGHAND) = 201',   # 线程
                 f'{self.OWN} clone(child_stack=NULL, flags=CLONE_VM|SIGCHLD <unfinished ...>',               # 被打断
                 f'{self.OWN + 1} chdir("/etc") = 0',
                 f'{self.OWN} <... clone resumed>) = 202',
                 f'{self.OWN} vfork() = 203',
                 self.open_line(self.OWN, "/proc/200/stat"), self.open_line(self.OWN, "/proc/201/comm"),
                 self.open_line(self.OWN, "/proc/202/status"), self.open_line(self.OWN, "/proc/203/cmdline"),
                 self.stat_line(self.OWN, "/proc/self/task/201"), self.open_line(self.OWN, "/proc/self/task/201/stat"),
                 self.open_line(self.OWN, "/proc/100/task/200/maps")]
        self.assertEqual(set(self.accesses(lines)), {"/etc/hostname", "/etc"})

    def test_only_positive_clone_results_are_pids(self) -> None:
        """clone 的返回值只有大于 0 才是子进程号（0 是子进程一侧、负数是失败）；/proc/0 不是本轨迹的进程。"""
        lines = [self.open_line(self.OWN, "/etc/hostname"), f'{self.OWN} clone(child_stack=NULL, flags=SIGCHLD) = 0',
                 f'{self.OWN} clone(child_stack=NULL, flags=SIGCHLD) = -1 EAGAIN (Resource temporarily unavailable)',
                 self.open_line(self.OWN, "/proc/0/stat"), self.open_line(self.OWN, "/proc/-1/stat")]
        accessed = self.accesses(lines)
        self.assertEqual(set(accessed) - {"/etc/hostname"}, {"/proc/0/stat", "/proc/-1/stat"})

    def test_a_pid_known_only_from_its_own_trace_lines_counts_too(self) -> None:
        """fork 的返回行可以晚于子进程自己的输出；任何一方先出现都进集合。"""
        lines = [self.open_line(300, "/etc/hostname"), self.open_line(self.OWN, "/proc/300/status")]
        self.assertEqual(set(self.accesses(lines)), {"/etc/hostname"})

    def test_pid_reuse_is_still_the_traced_process_tree(self) -> None:
        lines = [self.open_line(self.OWN, "/etc/hostname"), self.clone_line(self.OWN, 400), self.open_line(400, "/proc/400/stat"),
                 self.clone_line(self.OWN, 400), self.open_line(self.OWN, "/proc/400/stat")]
        self.assertEqual(set(self.accesses(lines)), {"/etc/hostname"})

    def test_all_whitelisted_leaves_and_kinds(self) -> None:
        leaves = ("stat", "statm", "status", "cmdline", "comm", "maps", "smaps", "smaps_rollup", "io", "limits", "oom_score", "oom_score_adj")
        lines = [self.open_line(self.OWN, "/etc/hostname")]
        for leaf in leaves:
            lines += [self.open_line(self.OWN, f"/proc/{self.OWN}/{leaf}"), self.open_line(self.OWN, f"/proc/self/{leaf}", resolved=f"/proc/{self.OWN}/{leaf}"),
                      self.open_line(self.OWN, f"/proc/thread-self/{leaf}", resolved=f"/proc/{self.OWN}/task/{self.OWN}/{leaf}"),
                      f'{self.OWN} openat(AT_FDCWD<{"/repo"}>, "/proc/{self.OWN}/{leaf}", O_RDONLY) = -1 EACCES (Permission denied)',
                      f'{self.OWN} openat(AT_FDCWD</repo>, "/proc/{self.OWN}/{leaf}", O_RDONLY) = -1 ENOENT (No such file or directory)']
        self.assertEqual(set(self.accesses(lines)), {"/etc/hostname"})

    def test_directories_allow_metadata_only(self) -> None:
        lines = [self.open_line(self.OWN, "/etc/hostname"), self.open_line(self.OWN, "/proc/self", "O_RDONLY|O_DIRECTORY", resolved=f"/proc/{self.OWN}"),
                 self.stat_line(self.OWN, "/proc/self"), self.stat_line(self.OWN, "/proc/thread-self"), self.stat_line(self.OWN, f"/proc/{self.OWN}/task"),
                 self.stat_line(self.OWN, f"/proc/{self.OWN}/task/{self.OWN}"), self.stat_line(self.OWN, f"/proc/{self.OWN}", errno="ENOENT")]
        self.assertEqual(set(self.accesses(lines)), {"/etc/hostname"})

    # ---- 反例：不属于本轨迹的 pid ----
    def test_other_processes_are_host_state(self) -> None:
        """整张进程表扫描（test_arm64_driver_wait 的 1…310，每个 pid 三次访问）：只有自己与子进程被放行，其余全部仍是宿主访问。"""
        lines = [self.open_line(self.OWN, "/etc/hostname"), self.clone_line(self.OWN, 150)]
        for pid in range(1, 311):
            lines += [self.stat_line(self.OWN, f"/proc/{pid}"), self.open_line(self.OWN, f"/proc/{pid}/stat"), self.open_line(self.OWN, f"/proc/{pid}/status")]
        accessed = self.accesses(lines)
        outside = {pid for pid in range(1, 311) if f"/proc/{pid}" in accessed}
        self.assertEqual(outside, set(range(1, 311)) - {self.OWN, 150}, "自己与子进程没登记，其余 308 个都登记")
        self.assertTrue(all(f"/proc/{pid}/{leaf}" in accessed for pid in outside for leaf in ("stat", "status")))
        document = self.document(lines)
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_a_pid_that_never_appeared_in_the_trace_is_not_internal_even_if_it_looks_plausible(self) -> None:
        for target in ("/proc/1/stat", "/proc/99999/status", f"/proc/{self.OWN + 1}/maps", f"/proc/{self.OWN}0/stat"):
            with self.subTest(target=target):
                lines = [self.open_line(self.OWN, "/etc/hostname"), self.open_line(self.OWN, target)]
                self.assertIn(target, self.accesses(lines))

    def test_a_pid_only_learned_after_the_read_is_not_retroactive(self) -> None:
        """集合按轨迹顺序增长：先读、后来才出现的 pid 不回溯放行（保守，宁可失败关闭）。"""
        lines = [self.open_line(self.OWN, "/proc/500/stat"), self.clone_line(self.OWN, 500)]
        self.assertIn("/proc/500/stat", self.accesses(lines))

    def test_leading_zero_and_non_canonical_pids_are_not_the_traced_pid(self) -> None:
        lines = [self.open_line(self.OWN, "/etc/hostname"), self.open_line(self.OWN, f"/proc/0{self.OWN}/stat"),
                 self.open_line(self.OWN, f"/proc/{self.OWN}.0/stat"), self.open_line(self.OWN, "/proc/-1/stat"), self.open_line(self.OWN, "/proc/task/stat")]
        accessed = self.accesses(lines)
        self.assertEqual(set(accessed) - {"/etc/hostname"}, {f"/proc/0{self.OWN}/stat", f"/proc/{self.OWN}.0/stat", "/proc/-1/stat", "/proc/task/stat"})

    def test_an_unknown_thread_id_is_not_internal(self) -> None:
        lines = [self.open_line(self.OWN, "/etc/hostname"), self.open_line(self.OWN, f"/proc/{self.OWN}/task/999/stat"),
                 self.open_line(self.OWN, "/proc/self/task/999/stat", resolved=f"/proc/{self.OWN}/task/999/stat")]
        self.assertEqual(set(self.accesses(lines)) - {"/etc/hostname"}, {f"/proc/{self.OWN}/task/999/stat", "/proc/self/task/999/stat"})

    # ---- 反例：不在白名单里的叶、魔法链接、其它 /proc 路径 ----
    def test_names_outside_the_whitelist_stay_host_accesses(self) -> None:
        names = ("root/etc/passwd", "cwd/x", "exe", "fd/3", "fdinfo/3", "environ", "ns/mnt", "mem", "net/dev", "mountinfo", "mounts", "cgroup",
                 "auxv", "wchan", "attr/current", "stack", "syscall", "personality", "pagemap", "root", "cwd", "fd", "ns", "map_files/1-2",
                 "task/%d/environ" % self.OWN, "task/%d/root/etc" % self.OWN, "task/%d/fd/3" % self.OWN, "task/%d/stat/x" % self.OWN, "stat/x")
        for who in (f"{self.OWN}", "self", "thread-self"):
            for name in names:
                with self.subTest(who=who, name=name):
                    target = f"/proc/{who}/{name}"
                    lines = [self.open_line(self.OWN, "/etc/hostname"), self.open_line(self.OWN, target, resolved=f"/proc/{self.OWN}/{name}")]
                    self.assertIn(target, self.accesses(lines))

    def test_the_target_of_a_magic_link_is_still_registered_from_the_descriptor_annotation(self) -> None:
        """/proc/self/root/etc/passwd 解引用到宿主文件：请求路径不放行，描述符注解里的真实路径 /etc/passwd 是普通宿主访问。"""
        lines = [self.open_line(self.OWN, "/etc/hostname"),
                 self.open_line(self.OWN, "/proc/self/root/etc/passwd", resolved="/etc/passwd"),
                 self.open_line(self.OWN, "/proc/self/cwd/secret", resolved="/home/u/secret")]
        accessed = self.accesses(lines)
        self.assertEqual(set(accessed), {"/etc/hostname", "/proc/self/root/etc/passwd", "/etc/passwd", "/proc/self/cwd/secret", "/home/u/secret"})

    def test_other_proc_paths_are_unchanged(self) -> None:
        for path in ENVIRONMENT_READS:
            with self.subTest(path=path):
                accessed = self.accesses([self.open_line(self.OWN, path), self.open_line(self.OWN, "/etc/hostname")])
                self.assertIn(path, accessed)
        lines = [f'{self.OWN} openat(AT_FDCWD</repo>, "/proc/sys/crypto/fips_enabled", O_RDONLY) = -1 ENOENT (No such file or directory)']
        self.assertEqual(self.accesses(lines), {"/proc/sys/crypto/fips_enabled": {"missing"}})

    def test_writes_executes_and_enumerations_are_not_internal(self) -> None:
        cases = {
            "写打开 oom_score_adj": self.open_line(self.OWN, "/proc/self/oom_score_adj", "O_WRONLY|O_CLOEXEC", resolved=f"/proc/{self.OWN}/oom_score_adj"),
            "读写打开 comm": self.open_line(self.OWN, "/proc/self/comm", "O_RDWR", resolved=f"/proc/{self.OWN}/comm"),
            "O_CREAT 打开": self.open_line(self.OWN, f"/proc/{self.OWN}/stat", "O_WRONLY|O_CREAT", resolved=f"/proc/{self.OWN}/stat"),
            "chmod": f'{self.OWN} chmod("/proc/self/stat", 0600) = -1 EPERM (Operation not permitted)',
            "成功的执行": f'{self.OWN} execve("/proc/self/stat", ["x"], 0x1) = 0',
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                accessed = self.accesses([self.open_line(self.OWN, "/etc/hostname"), line])
                self.assertTrue(any(path.startswith("/proc/") for path in accessed), (label, accessed))
        listing = [self.open_line(self.OWN, "/etc/hostname"), f'{self.OWN} getdents64(3</proc/{self.OWN}>, 0x1234, 32768) = 600',
                   f'{self.OWN} getdents64(3</proc/{self.OWN}/task>, 0x1234, 32768) = 100']
        accessed = self.accesses(listing)
        self.assertEqual({path: kinds for path, kinds in accessed.items() if path.startswith("/proc")},
                         {f"/proc/{self.OWN}": {"list"}, f"/proc/{self.OWN}/task": {"list"}}, "枚举（getdents64）不放行")

    def test_paths_spelled_with_dot_components_are_not_internal(self) -> None:
        """/proc/self/cwd/.. 在内核里回到工作目录的父目录，文本规范化却得到 /proc/self：写法带 .. 或 . 时整个调用不适用放宽。"""
        for raw, normalised in (("/proc/self/cwd/../stat", "/proc/self/stat"), (f"/proc/{self.OWN}/../{self.OWN}/stat", f"/proc/{self.OWN}/stat"),
                                (f"/proc/{self.OWN}/./stat", f"/proc/{self.OWN}/stat"), ("/proc/self/root/../stat", "/proc/self/stat")):
            with self.subTest(raw=raw):
                lines = [self.open_line(self.OWN, "/etc/hostname"), self.open_line(self.OWN, raw, resolved="/home/stat")]
                accessed = self.accesses(lines)
                self.assertEqual(normalised, os.path.normpath(raw))
                self.assertIn(normalised, accessed, "规范化后的路径被登记（没有因为白名单而丢掉）")
                self.assertIn("/home/stat", accessed)
        lines = [self.open_line(self.OWN, "/etc/hostname"), f'{self.OWN} openat(AT_FDCWD</proc/{self.OWN}>, "../{self.OWN}/stat", O_RDONLY) = 3</proc/{self.OWN}/stat>']
        self.assertIn(f"/proc/{self.OWN}/stat", self.accesses(lines))

    def test_relative_paths_against_an_annotated_proc_directory_fd(self) -> None:
        lines = [self.open_line(self.OWN, "/etc/hostname"), self.open_line(self.OWN, "status", cwd=f"/proc/{self.OWN}", resolved=f"/proc/{self.OWN}/status"),
                 self.open_line(self.OWN, "environ", cwd=f"/proc/{self.OWN}", resolved=f"/proc/{self.OWN}/environ")]
        self.assertEqual(set(self.accesses(lines)) - {"/etc/hostname"}, {f"/proc/{self.OWN}/environ"})

    def test_v3_registers_every_one_of_them(self) -> None:
        lines = [RAW_PROC_OWN_STAT, RAW_PROC_OWN_STATUS, RAW_PROC_SELF_STATUS, RAW_PROC_OWN_DIR]
        parser = self.parse(lines, "v3")
        self.assertEqual(parser.unit_proc_accesses, 0)
        self.assertEqual(self.parse(lines, "v3").accesses, self.parse(lines, "v3").accesses)
        self.assertTrue({"/proc/3983820/stat", "/proc/3983820/status", "/proc/self/status", "/proc/3983820"} <= set(parser.accesses))

    # ---- /proc/self 自己的 lstat：WP-R7a 起不再放行（原“内核常量”子规则已删除，理由见 ``_returned_plain_link_stat`` 的说明） ----
    def proc_self_lstat(self, path: str, *, flags: str = "AT_SYMLINK_NOFOLLOW") -> str:
        struct = ('{st_dev=makedev(0, 0x1a), st_ino=811905223, st_mode=S_IFLNK|0777, st_nlink=1, st_uid=0, st_gid=0, st_blksize=1024, st_blocks=0, st_size=0, '
                  'st_atime=1792047374 /* 2026-10-15T06:56:14.102163350+0000 */, st_atime_nsec=102163350, st_mtime=1792047374 /* 2026-10-15T06:56:14.102163350+0000 */, '
                  'st_mtime_nsec=102163350, st_ctime=1792047374 /* 2026-10-15T06:56:14.102163350+0000 */, st_ctime_nsec=102163350}')
        return f'{self.OWN} newfstatat(AT_FDCWD</repo>, "{path}", {struct}, {flags}) = 0'

    def test_lstat_of_proc_self_and_the_other_proc_magic_links_stays_unresolved(self) -> None:
        for path in ("/proc/self", "/proc/thread-self", "/proc/self/exe", "/proc/self/cwd", "/proc/self/root", "/proc/self/fd/3", "/proc/1/exe"):
            for contract in ("v3", "v4"):
                with self.subTest(path=path, contract=contract):
                    self.assertEqual(self.reasons_of([self.proc_self_lstat(path)], contract), {"symlink-metadata-not-bound:newfstatat": 1})


# =====================================================================================================================
# R4-2 / R4-3  /dev/tty 的失败写探测与 /dev/null 的读写：核对结论是“无需改动”，这里固定它并固定相邻形态仍被拒绝
# =====================================================================================================================


class DevNullAndTerminalTests(R4Fixture):
    """调度者的普查把 ``/dev/tty``（kinds ``["write"]``）和 ``/dev/null``（``write``）列为未声明读取，但**整份真实语料**走完整套
    严格审计（探查脚本在会话暂存目录 ``wp-r4/corpus_covered.py``）显示它们都已经被 ``replay_runtime`` 覆盖：

    * ``/dev/null``：``kinds ⊆ {read, write, stat}`` 即覆盖（``replay_runtime`` 末尾的固定条款）；
    * ``/dev/tty``：失败的 ``open/openat`` 且 ``ENXIO``（无控制终端）是合同允许的探测，``terminal_events`` 与访问集合互相印证即覆盖，
      访问类型叫 ``write`` 还是 ``stat`` 都不影响（事件自带 kind，两边同一个变量）。

    普查里出现在“未声明”清单，是 ``replay_runtime`` 没有成功时（别的缺陷让重放抛错，``runtime_paths`` 为空）所有靠重放覆盖的路径一并显示
    的假象，不是这两类本身的缺口。所以 R4-2、R4-3 不改任何代码：改了反而会动第二、三轮已固定的失败调用语义（私有盘内失败的创建、
    EEXIST／ENOENT 的归属判定）。语料里“带写标志却失败”的调用只有两类：``/dev/tty`` ENXIO 与私有盘内 ENOENT/EEXIST，宿主路径上
    没有任何实例，所以也没有把宿主路径上失败的写打开降级为探测的证据——保持失败关闭（见下面的固定测试）。
    """

    def chain(self, lines: list[str]) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
        document = self.document(self.pad(lines))
        report = self.audit(document)
        try:
            covered = runtime.replay_runtime(document, [self.entry])
        except runtime.RuntimeContractError:
            covered = set()      # 重放拒绝（例如终端访问超出合同）：没有任何路径被覆盖，整次审计另有 runtime 类 finding
        return document, report, covered

    def undeclared(self, lines: list[str]) -> list[tuple[str, list[str]]]:
        _document, report, _covered = self.chain(lines)
        return [(item["path"], item["kinds"]) for item in report["undeclared"]]

    def test_dev_null_reads_and_writes_are_covered(self) -> None:
        lines = ['1 openat(AT_FDCWD</repo>, "/dev/null", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3</dev/null>',        # shell 的 >/dev/null
                 '2 openat(AT_FDCWD</repo>, "/dev/null", O_RDWR|O_CLOEXEC) = 3</dev/null>',                      # subprocess.DEVNULL
                 '3 openat(AT_FDCWD</repo>, "/dev/null", O_RDONLY) = 3</dev/null>',                              # 读恒空
                 f'4 newfstatat(AT_FDCWD</repo>, "/dev/null", {DIR_STRUCT.replace("S_IFDIR|0555", "S_IFCHR|0666")}, 0) = 0']
        document, report, covered = self.chain(lines)
        self.assertEqual(dict(document["accesses"])["/dev/null"], ["read", "stat", "write"])
        self.assertIn("/dev/null", covered)
        self.assertEqual((report["status"], report["undeclared_count"]), ("passed", 0), report["undeclared"])

    def test_the_failed_terminal_probe_is_covered_whatever_its_kind_is_called(self) -> None:
        """open("/dev/tty", O_RDWR) 在无控制终端时 ENXIO：访问类型是 write（有写标志），重放的终端合同照样覆盖。"""
        probe = '1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR|O_NONBLOCK) = -1 ENXIO (No such device or address)'
        document, report, covered = self.chain([probe, '1 stat("/dev/tty", 0x1) = 0'])
        self.assertEqual(dict(document["accesses"])["/dev/tty"], ["stat", "write"])
        self.assertIn("/dev/tty", covered)
        self.assertEqual((report["status"], report["undeclared_count"]), ("passed", 0), report["undeclared"])

    def test_other_devices_and_aliases_are_still_refused(self) -> None:
        for path in ("/dev/full", "/dev/stdin", "/dev/stdout", "/dev/fd/0", "/dev/shm/x", "/dev/ptmx"):   # R16：/dev/zero、/dev/urandom、/dev/random 的只读已放行，见 test_ci_read_audit_wp_r16
            with self.subTest(path=path):
                self.assertEqual(self.undeclared([f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY) = 3<{path}>']), [(path, ["read"])])

    def test_paths_under_dev_null_are_a_known_gap_and_stay_closed(self) -> None:
        """git 的 core.hooksPath=/dev/null 会 faccessat("/dev/null/reference-transaction")（ENOTDIR，语料里出现过）：不是 /dev/null 本身，
        目前不覆盖，保持失败关闭（列入未解决问题，不在本轮范围）。"""
        line = '1 faccessat(AT_FDCWD</repo>, "/dev/null/reference-transaction", X_OK) = -1 ENOTDIR (Not a directory)'
        self.assertEqual(self.undeclared([line]), [("/dev/null/reference-transaction", ["missing"])])

    def test_a_successful_or_otherwise_failing_terminal_open_is_refused(self) -> None:
        for line in ('1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR) = 3</dev/tty>',
                     '1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR) = -1 EACCES (Permission denied)',
                     '1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR) = -1 ENOENT (No such file or directory)',
                     '1 chmod("/dev/tty", 0600) = 0'):
            with self.subTest(line=line):
                self.assertFalse(self.chain([line])[1]["coverage_complete"])

    def test_an_alias_through_proc_fd_or_a_host_symlink_is_not_the_exact_dev_null_path(self) -> None:
        """/proc/self/fd/1 → /dev/null：描述符注解里的 /dev/null 被覆盖，但请求路径 /proc/self/fd/1 不在 R4-1 的白名单里，仍是未声明项。
        指向 /dev/null 的宿主符号链接：host_snapshot 拒绝指向设备的链接，没法声明。"""
        line = '1 openat(AT_FDCWD</repo>, "/proc/self/fd/1", O_WRONLY) = 3</dev/null>'
        self.assertEqual(self.undeclared([line]), [("/proc/self/fd/1", ["write"])])
        alias = self.base / "null-alias"
        os.symlink("/dev/null", alias)
        with self.assertRaises(ra.AuditError):
            ra.host_snapshot_v3(str(alias))
        line = f'1 openat(AT_FDCWD</repo>, "{alias}", O_WRONLY) = 3</dev/null>'
        self.assertEqual(self.undeclared([line]), [(str(alias), ["write"])])

    def test_a_failed_write_open_on_a_host_path_is_still_a_write_no_evidence_to_relax(self) -> None:
        """没有证据就不放宽：宿主路径上失败的带写标志打开仍按 write 登记（语料里只有 /dev/tty 与私有盘内两类）。"""
        for errno_text in ("EACCES (Permission denied)", "EROFS (Read-only file system)", "ENOENT (No such file or directory)"):
            with self.subTest(errno=errno_text):
                line = f'1 openat(AT_FDCWD</repo>, "/etc/shadow", O_WRONLY|O_CREAT, 0600) = -1 {errno_text}'
                kinds = dict(self.chain([line])[0]["accesses"])["/etc/shadow"]
                self.assertEqual(kinds, ["write"])

    def test_v3_covers_the_same_two_paths_identically(self) -> None:
        """重放里的这两条固定条款 v3 本来就有（control 四链与历史凭证依赖它）；本轮没有改它们。"""
        lines = self.pad(['1 openat(AT_FDCWD</repo>, "/dev/null", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3</dev/null>',
                          '1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR|O_NONBLOCK) = -1 ENXIO (No such device or address)'])
        document = self.document(lines, contract="v3")
        self.assertTrue(self.audit(document)["coverage_complete"])


# =====================================================================================================================
# R4-4  identity-memo（调研结论的可行性证据；不改任何代码）
# =====================================================================================================================


class PrivateIdentityMemoTests(R4Fixture):
    """``CODEX_UPGRADE_IDENTITY_MEMO`` 指向私有临时盘内的目录时，memo 的读写是普通的“本单元自己生成的输出”。

    背景与结论见提交说明与交付报告：memo 是内容寻址的纯函数缓存（文件名＝键材料摘要，读取时核对键全等、结果摘要、普通文件、
    属主与 0600），共享时每个受审计单元对 ``<--out-dir>/identity-memo`` 的读写都落在宿主路径上，路径随每轮运行目录变，无法静态声明；
    推荐把受审计单元的 memo 目录定在各自私有临时盘里（``run_child`` 在子进程环境里把该变量指向 ``<temporary_root>/identity-memo``）。
    本类用 Python 实际会产生的系统调用序列（``Path.mkdir(parents=True, exist_ok=True)``、``NamedTemporaryFile(delete=False)``、
    ``os.replace``、``finally`` 里的 ``unlink(missing_ok=True)``、``os.open(O_RDONLY|O_NOFOLLOW|O_NONBLOCK)``）证明：这套读写在 v4 输出归属下
    整链通过（含同一单元的第二个进程命中、并发首次未命中各自发布），而共享在宿主路径上的同样序列整次失败。
    """

    DIGEST = "0123456789abcdef" * 4

    def memo(self, relative: str = "") -> str:
        return f"{self.root}/identity-memo" + (f"/{relative}" if relative else "")

    def sequence(self, pid: int, *, kind: str = "evaluator", suffix: str = "ab12cd34", directory: str | None = None, hit: bool = False,
                 directory_exists: bool = False) -> list[str]:
        """一个进程对 memo 的一次访问：未命中（ENOENT）→ 建目录（已存在则 EEXIST）→ 写临时文件 → 原子发布 → 清理 → 再读；命中则只读。"""
        base = directory or self.memo()
        final, temporary = f"{base}/{kind}-{self.DIGEST}.json", f"{base}/.{kind}-{self.DIGEST}.json.{suffix}.tmp"
        read = f'{pid} openat(AT_FDCWD</repo>, "{final}", O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC) = 3<{final}>'
        if hit:
            return [read]
        return [f'{pid} openat(AT_FDCWD</repo>, "{final}", O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC) = -1 ENOENT (No such file or directory)',
                f'{pid} mkdirat(AT_FDCWD</repo>, "{base}", 0700) = ' + ("-1 EEXIST (File exists)" if directory_exists else "0"),
                f'{pid} openat(AT_FDCWD</repo>, "{temporary}", O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0600) = 4<{temporary}>',
                f'{pid} renameat(AT_FDCWD</repo>, "{temporary}", AT_FDCWD</repo>, "{final}") = 0',
                f'{pid} unlinkat(AT_FDCWD</repo>, "{temporary}", 0) = -1 ENOENT (No such file or directory)', read]

    def test_first_miss_then_publish_then_a_second_process_hits(self) -> None:
        lines = self.sequence(1) + self.sequence(2, hit=True) + self.sequence(2, kind="identity-v2", directory_exists=True)
        document = self.assertPasses(lines)
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])
        paths = {event["path"] for event in document["runtime"]["output_events"]}
        self.assertIn(self.memo(f"evaluator-{self.DIGEST}.json"), paths)
        self.assertIn(self.memo(f"identity-v2-{self.DIGEST}.json"), paths)

    def test_two_processes_that_both_miss_publish_to_the_same_name(self) -> None:
        """并发首次未命中各自计算、各用独立临时文件原子发布：后到的 rename 覆盖先到的同类型文件，合法。"""
        final = self.memo(f"evaluator-{self.DIGEST}.json")
        first, second = self.memo(f".evaluator-{self.DIGEST}.json.ab12cd34.tmp"), self.memo(f".evaluator-{self.DIGEST}.json.zz99yy88.tmp")
        miss = 'openat(AT_FDCWD</repo>, "%s", O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC) = -1 ENOENT (No such file or directory)' % final
        lines = [f"1 {miss}", f"2 {miss}",                                     # 两个进程都没命中
                 f'1 mkdirat(AT_FDCWD</repo>, "{self.memo()}", 0700) = 0',
                 f'2 mkdirat(AT_FDCWD</repo>, "{self.memo()}", 0700) = -1 EEXIST (File exists)',
                 f'1 openat(AT_FDCWD</repo>, "{first}", O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0600) = 4<{first}>',
                 f'2 openat(AT_FDCWD</repo>, "{second}", O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0600) = 4<{second}>',
                 f'1 renameat(AT_FDCWD</repo>, "{first}", AT_FDCWD</repo>, "{final}") = 0',
                 f'2 renameat(AT_FDCWD</repo>, "{second}", AT_FDCWD</repo>, "{final}") = 0',     # 覆盖同类型文件
                 f'1 unlinkat(AT_FDCWD</repo>, "{first}", 0) = -1 ENOENT (No such file or directory)',
                 f'2 unlinkat(AT_FDCWD</repo>, "{second}", 0) = -1 ENOENT (No such file or directory)',
                 f'1 openat(AT_FDCWD</repo>, "{final}", O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC) = 3<{final}>']
        self.assertPasses(lines)

    def test_the_same_sequence_on_a_shared_host_directory_fails_the_audit(self) -> None:
        """共享时 memo 目录是 ``<out-dir>/identity-memo``（宿主路径）：每个文件的读、写、发布都是未声明的宿主访问，路径还随每轮变化。"""
        shared = "/root/docker/capture-cli/evidence/run-20261015T1000/executor/identity-memo"
        lines = self.sequence(1, directory=shared)
        document = self.document(self.pad(lines))
        report = self.audit(document)
        self.assertFalse(report["coverage_complete"])
        paths = {item["path"] for item in report["undeclared"]}
        self.assertIn(f"{shared}/evaluator-{self.DIGEST}.json", paths)
        self.assertIn(shared, paths)

    def test_a_cache_entry_nobody_in_this_unit_published_is_not_trusted(self) -> None:
        """私有盘是空盘：读一个本单元没发布过的 memo 文件（例如偷偷从别处复制进来）判“首次读取前未证明由本轮生成”。"""
        final = self.memo(f"evaluator-{self.DIGEST}.json")
        lines = self.base_lines() + [f'1 openat(AT_FDCWD</repo>, "{final}", O_RDONLY|O_NOFOLLOW|O_NONBLOCK) = 3<{final}>']
        self.assertFailsClosed(lines, where="tracker", fragment="首次读取前未证明由本轮生成")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
