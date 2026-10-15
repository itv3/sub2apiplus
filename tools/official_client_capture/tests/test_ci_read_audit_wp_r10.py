"""审计解析器加固第十轮（WP-R10）：v4 认得 strace 打在描述符／目录注解右尖括号**之后**的 ``(deleted)`` 后缀，修掉 openat 三联偶发未解析。

背景（10-15 ARM64 真实严格审计，strace 6.8，选项含 ``-y``）
=========================================================

``recovery`` 的分块、``compare_accept``、``sealed_gate_materials`` 等读 ``…/supervisor/run-<hash>/heartbeat.json`` 的单元，偶发
同一行同时登记 ``dirfd-path-not-resolved:openat``、``opened-fd-not-resolved:openat``、``result-or-string-incomplete:openat`` 三个
未解析原因（「openat 三联」），单元整体失败。调度者用诊断过滤器抓到了完整原始行（拆行再拼回后的整行）：

    3836609 <... openat resumed>)           = 3</root/docker/…/runtime/<hash>/tmp4brvyjrd/supervisor/run-9e484e75…/heartbeat.json>(deleted)
    … O_RDONLY|O_CLOEXEC )           = 3</root/docker/…/data/evidence/campaigns/upgrade-0154-vc-chain/.supervisor/run-3c9028f3…/heartbeat.json>(deleted)

共同点：返回的描述符注解右尖括号之后紧跟 ``(deleted)``。心跳文件走「写临时文件再 ``renameat`` 改名」发布，被打开的旧文件在 openat 返回前后被
别的进程原子替换，strace 读 /proc/<pid>/fd/N 得到「路径 (deleted)」时把后缀剥出来打在 ``>`` 之后。v3 的 ``_RESULT`` 要求注解后直接结尾，
``result`` 为 None，三个原因同时登记。

修法（``tools/ci/read_audit.py``，只在 v4 生效）：``_RESULT_V4``／``_ANNOTATION_V4``／``_RETURNED_FD_V4``／``_FD_ANNOTATION_V4`` 在注解右尖括号之后
另允许一个可选的紧跟的 ``(deleted)``（strace 上游源码与 NEWS 5.19 核对过：只有 ``>(deleted)``，无空格；带空格的写法没有任何版本的样本，不接受）；``TraceParser`` 在构造时按 ``trace_v4`` 选用，
v3 与 control 四链仍用原正则、一字不改。注解里的路径仍是该描述符当时指向的路径，``(deleted)`` 只说明路径上的对象之后被替换或删除：访问照原路径登记，
盘内／盘外仍由 ``under(path, root)`` 判。

本模块的测试分组
================

* ``RealRawLineTests``：两条真实原始行（拆行再拼回）端到端过 ``TraceParser``（严格、v4、带隔离合同）：私有盘上的心跳读取零未解析、零归属问题、整份轨迹验真通过；
  宿主上的心跳读取零未解析，但仍按宿主读登记并要求声明覆盖；v3 与 control 仍是三联。
* ``ArgumentAnnotationTests``：同样的后缀出现在参数里的 dirfd／目录／AT_FDCWD／fchdir／futimens／AT_EMPTY_PATH／lstat 路径还原上。
* ``NegativeShapeTests``：不是 strace 输出形态的写法仍判未解析（后缀后有垃圾、重复、任何空格、大小写不同、数字之前、没有注解……）。
* ``SecurityTests``：后缀不改变路径归属；注解指向盘外仍被拦；注解不能洗白引号里的宿主路径；旧形态（后缀在尖括号里面）保持字面路径、失败关闭；
  转义的 ``\\>`` 不会提前结束注解；``O_TMPFILE`` 形态仍无创建证明。
* ``MetamorphicTests``：对一批「正常＋恶意」场景，给每条返回注解后追加 ``(deleted)`` 后，未解析原因、访问集合、归属问题、审计结论逐项不变。
* ``RegexLevelTests``：正则层的性质检验——无后缀输入 v4 与 v3 的匹配完全一致，有后缀输入 v4 的各分组与去掉后缀后一致；v3 原正则文本一字未改。
* ``V3UnchangedTests``：v3、默认构造、control 子类选用的仍是原正则，行为仍是三联；整份语料（可选）v3 完整输出的 sha256 与改动前逐字相同。

安全分析（R10-2）
=================

1. **后缀不改变路径归属**：解析器只取右尖括号**之前**的路径参与 ``under(path, root)`` 与访问登记，后缀文字不进路径。所以宿主路径加上 ``(deleted)``
   仍是宿主访问（要求精确快照声明），私有盘路径加上 ``(deleted)`` 仍是盘内访问；``MetamorphicTests`` 对 8 类场景逐项验证了「加后缀前后结论一致」。
2. **不能把宿主文件伪装成已删除的私有盘文件**：返回描述符的真实路径（``target``）必须在私有盘内，否则输出归属判「文件描述符未绑定到私有临时盘」
   （``SecurityTests.test_a_descriptor_annotation_outside_the_private_root_is_still_refused``）。引号里的路径与注解路径各自登记，注解不能洗白引号里的宿主路径。
3. **不能把私有盘文件伪装成宿主文件**：反向没有收益——宿主访问只会多要求声明。
4. **名字里带 ``>(deleted)`` 的文件**：strace 把名字里的 ``>`` 转义成 ``\\>``，解析器的注解正则（``_ANN``）把 ``\\>`` 当成路径字符，不会提前结束注解；
   这样的名字解出来是字面路径，后缀只在真正的右尖括号之后识别。
5. **旧形态（``3</x (deleted)>``，后缀在尖括号里面）不规整**：旧版 strace 把后缀留在路径里，无法与一个真名为「x (deleted)」的文件区分，所以仍按字面路径登记，
   这个路径从未被创建／未声明，自然失败关闭（v3、v4 一致，``test_the_old_inside_form_stays_a_literal_path_and_fails_closed``）。
6. **dirfd 带后缀**：脱链的目录里内核的查找只能失败（目录已空、已死），不会触碰别的路径；``..`` 沿死目录项的父链走，与文本规范化一致。解析器按注解路径拼出
   的路径要么是内核真正探测的名字，要么是更保守的名字（宿主上可能被重建的同名目录），不会漏记。宿主文件在运行期间被替换，由宿主快照的前后比对（内容摘要、
   核心属性）发现。
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import random
import re
import unittest
from pathlib import Path
from typing import Any

from tools.ci import control_read_audit as control_audit
from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests.test_ci_read_audit_wp_r2 import CORPUS_ROOT, RAW_GIT_PROBE, WORK, CorpusFixture

R = CORPUS_ROOT                    # 私有临时根（真实长度）
TRIPLE = {"dirfd-path-not-resolved:openat": 1, "opened-fd-not-resolved:openat": 1, "result-or-string-incomplete:openat": 1}

# 真实心跳目录名（run-<64 位十六进制>；前 8 位取自真实原始行，其余补齐到真实长度）。
RUN_PRIVATE = "run-9e484e75" + hashlib.sha256(b"r10-private-run").hexdigest()[8:]
RUN_HOST = "run-3c9028f3" + hashlib.sha256(b"r10-host-run").hexdigest()[8:]
HB_PRIVATE = f"{R}/tmp4brvyjrd/supervisor/{RUN_PRIVATE}/heartbeat.json"
# 第二条真实行里的宿主数据目录（长度级别与真实相同）。
HOST_DIR = f"/root/docker/capture-cli/data/evidence/campaigns/upgrade-0154-vc-chain/.supervisor/{RUN_HOST}"
HB_HOST = HOST_DIR + "/heartbeat.json"


def private_heartbeat_lines(suffix: str = "(deleted)", spaces: str = " " * 11) -> list[str]:
    """第一条真实行的完整上下文：旧心跳存在 → 别的进程 openat 被打断（unfinished）→ 又一个进程 renameat 原子替换 → openat 续上（resumed），
    返回的描述符注解后追加 ``suffix``。``spaces`` 是 ``)`` 与 ``=`` 之间的空格（真实输出里是 11 个）。"""
    sup = HB_PRIVATE.rsplit("/", 1)[0]
    return [
        f'10 mkdir("{R}/tmp4brvyjrd", 0700) = 0',
        f'10 mkdir("{R}/tmp4brvyjrd/supervisor", 0700) = 0',
        f'10 mkdir("{sup}", 0700) = 0',
        f'10 openat(AT_FDCWD<{WORK}>, "{HB_PRIVATE}", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{HB_PRIVATE}>',
        f'10 openat(AT_FDCWD<{WORK}>, "{HB_PRIVATE}.tmp", O_WRONLY|O_CREAT|O_EXCL, 0600) = 4<{HB_PRIVATE}.tmp>',
        f'3836609 openat(AT_FDCWD<{WORK}>, "{HB_PRIVATE}", O_RDONLY|O_CLOEXEC <unfinished ...>',
        f'3782397 renameat(AT_FDCWD<{WORK}>, "{HB_PRIVATE}.tmp", AT_FDCWD<{WORK}>, "{HB_PRIVATE}") = 0',
        f'3836609 <... openat resumed>){spaces}= 3<{HB_PRIVATE}>{suffix}',
    ]


def host_heartbeat_lines(host_path: str = HB_HOST, suffix: str = "(deleted)") -> list[str]:
    """第二条真实行：宿主数据目录里的心跳，openat 被另一个进程的事件打断后续上，``O_CLOEXEC )`` 与真实行一致。"""
    return [
        f'10 mkdir("{R}/base", 0700) = 0',
        f'3836609 openat(AT_FDCWD<{WORK}>, "{host_path}", O_RDONLY|O_CLOEXEC <unfinished ...>',
        f'3782397 mkdir("{R}/base/other", 0700) = 0',
        f'3836609 <... openat resumed>)           = 3<{host_path}>{suffix}',
    ]


def with_suffix(lines: list[str], suffix: str = "(deleted)") -> list[str]:
    """给每条行尾带返回描述符注解的行追加后缀（行尾已有后缀的不重复加）。"""
    pattern = re.compile(r"(\)\s*=\s*\d+<(?:[^>\\]|\\.)*>)$")
    return [pattern.sub(lambda match: match.group(1) + suffix, line) for line in lines]


class Wp10Fixture(CorpusFixture):
    """沿用 R2 的语料夹具（真实长度的私有根）。本类没有 test_ 方法。"""

    def problems(self, parser: ra.TraceParser) -> list[str]:
        return parser.output_tracker.report()["problems"]

    def accesses(self, parser: ra.TraceParser) -> dict[str, list[str]]:
        return {path: sorted(kinds) for path, kinds in parser.accesses.items()}


# =====================================================================================================================
# 两条真实原始行
# =====================================================================================================================


class RealRawLineTests(Wp10Fixture):
    def test_the_private_heartbeat_line_is_resolved_under_v4(self) -> None:
        parser = self.parse(private_heartbeat_lines())
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.problems(parser), [])
        self.assertEqual(sorted(parser.accesses[HB_PRIVATE]), ["read", "write"], "按注解路径（去掉后缀）登记读取")
        self.assertNotIn(HB_PRIVATE + "(deleted)", parser.accesses)
        self.assertFalse([path for path in parser.accesses if "deleted" in path], "后缀不进任何路径")

    def test_the_private_heartbeat_event_carries_the_stripped_path_as_target(self) -> None:
        document = self.document(private_heartbeat_lines())
        reads = [event for event in document["runtime"]["output_events"] if event["name"] == "openat" and event["kind"] == "read"]
        self.assertEqual([(event["path"], event["target"], event["succeeded"]) for event in reads],
                         [(HB_PRIVATE, HB_PRIVATE, True)], "返回描述符的真实路径（target）是去掉后缀的路径，没有退化成 None")

    def test_the_whole_private_trace_is_accepted_and_replayed(self) -> None:
        document = self.document(private_heartbeat_lines())
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertEqual(document["coverage"]["unresolved_reasons"], {})
        self.assertAccepted(document)

    def test_the_unsplit_single_line_form_is_resolved_too(self) -> None:
        lines = private_heartbeat_lines()[:5] + [
            f'3836609 openat(AT_FDCWD<{WORK}>, "{HB_PRIVATE}", O_RDONLY|O_CLOEXEC)           = 3<{HB_PRIVATE}>(deleted)']
        parser = self.parse(lines)
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.problems(parser), [])
        self.assertAccepted(self.document(lines))

    def test_the_one_space_variant_is_not_a_strace_form_and_stays_unresolved(self) -> None:
        """strace 源码（``print_quoted_string_in_angle_brackets``）与 NEWS 5.19 都说明后缀紧跟右尖括号；带空格的写法没有任何版本的样本，不接受。"""
        for contract in ("v3", "v4"):
            with self.subTest(contract=contract):
                self.assertEqual(self.reasons(private_heartbeat_lines(suffix=" (deleted)"), contract), TRIPLE)

    def test_trailing_whitespace_after_the_suffix_is_tolerated_like_after_any_result(self) -> None:
        for tail in ("   ", "\t"):
            with self.subTest(tail=tail):
                self.assertEqual(self.reasons(private_heartbeat_lines(suffix="(deleted)" + tail)), {})

    def test_the_same_private_lines_without_the_suffix_give_the_same_verdict(self) -> None:
        with_it, without = self.parse(private_heartbeat_lines()), self.parse(private_heartbeat_lines(suffix=""))
        self.assertEqual(self.accesses(with_it), self.accesses(without))
        self.assertEqual(self.problems(with_it), self.problems(without))
        self.assertEqual(with_it.unresolved_reasons, without.unresolved_reasons)

    def test_the_host_heartbeat_line_is_resolved_but_stays_a_host_read(self) -> None:
        parser = self.parse(host_heartbeat_lines())
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.accesses(parser)[HB_HOST], ["read"], "宿主路径按宿主读登记，后缀不改变归属")
        self.assertEqual(self.problems(parser), [], "宿主读取本身不是归属问题，由读集审计要求声明")

    def test_the_host_heartbeat_still_requires_a_declared_snapshot(self) -> None:
        document = self.document(host_heartbeat_lines())
        report = self.audit(document)
        self.assertFalse(report["coverage_complete"])
        self.assertEqual([(item["root"], item["path"], item["kinds"]) for item in report["undeclared"]], [("host", HB_HOST, ["read"])])

    def test_a_declared_host_snapshot_covers_the_heartbeat_read(self) -> None:
        """同形态的行，宿主路径换成本机真实文件并取 v3 快照：声明后才通过（后缀不免除声明）。"""
        host_file = self.base / "host-data" / ".supervisor" / RUN_HOST / "heartbeat.json"
        host_file.parent.mkdir(parents=True)
        host_file.write_text('{"beat": 1}\n')
        lines = host_heartbeat_lines(str(host_file))
        document = self.document(lines)
        self.assertFalse(self.audit(document)["coverage_complete"], "没有声明：未声明项")
        entry = ra.host_snapshot_v3(str(host_file))
        declared = self.document(lines)                  # 带宿主快照声明的完成凭证（与 run_child 同一个函数生成）
        declared["runtime"]["completion"].update(
            runtime._host_completion(ra, {"host_entries": [entry], "host_paths": [str(host_file)]}, declared))
        report = ra.strict_audit_reads(declared, [self.entry, entry], repo_root="/repo", data_root=None)
        self.assertTrue(report["coverage_complete"], report["undeclared"])
        host_file.write_text('{"beat": 2}\n')           # 运行期间被替换成别的内容：快照比对发现，不因后缀放过
        drift = ra.strict_audit_reads(declared, [self.entry, entry], repo_root="/repo", data_root=None)
        self.assertFalse(drift["coverage_complete"])

    def test_v3_keeps_producing_exactly_the_triple_for_both_real_lines(self) -> None:
        for label, lines in (("私有盘", private_heartbeat_lines()), ("宿主", host_heartbeat_lines())):
            with self.subTest(label=label):
                self.assertEqual(self.reasons(lines, "v3"), TRIPLE)
                document = self.document(lines, contract="v3")
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_the_default_constructed_parser_is_still_v3_behaviour(self) -> None:
        """declared_sampler 复用的默认路径（不带隔离合同、v3）：同样的行仍是三联。"""
        parser = ra.TraceParser(strict=True)
        for line in private_heartbeat_lines()[-3:]:
            parser.feed(line)
        self.assertEqual(parser.unresolved_reasons["result-or-string-incomplete:openat"], 1)
        self.assertEqual(parser.unresolved_reasons["opened-fd-not-resolved:openat"], 1)

    def test_the_control_sidecar_parser_is_unchanged(self) -> None:
        parser = control_audit.TraceParser(strict=True, runtime_context=self.control_context())
        for line in private_heartbeat_lines()[-3:]:
            parser.feed(line)
        self.assertEqual(parser.unresolved_reasons["result-or-string-incomplete:openat"], 1)
        self.assertEqual(parser.unresolved_reasons["opened-fd-not-resolved:openat"], 1)

    def test_a_plain_open_call_returns_the_same_way(self) -> None:
        """ARM64 没有 open 系统调用，但解析器的 plain 分支共用同一个返回描述符判据，x86_64 上会走到。"""
        lines = [f'10 mkdir("{R}/p", 0700) = 0', f'10 open("{R}/p/f", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{R}/p/f>',
                 f'10 open("{R}/p/f", O_RDONLY) = 4<{R}/p/f>(deleted)']
        parser = self.parse(lines)
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.problems(parser), [])
        self.assertEqual(self.reasons(lines, "v3"), {"result-or-string-incomplete:open": 1, "plain-path-not-resolved:open": 1,
                                                     "opened-fd-not-resolved:open": 1})

    def test_other_successful_forms_of_the_line_are_still_parsed(self) -> None:
        """后缀只是多允许的一段：失败的调用、正常的错误码尾巴不受影响。"""
        lines = self.pad([f'10 openat(AT_FDCWD<{WORK}>, "{R}/no-such", O_RDONLY) = -1 ENOENT (No such file or directory)',
                          f'10 openat(AT_FDCWD<{WORK}>, "{R}/base", O_RDONLY|O_DIRECTORY) = 5<{R}/base>'])
        self.assertEqual(self.reasons(lines), {})


# =====================================================================================================================
# 参数里的 fd／目录注解
# =====================================================================================================================


class ArgumentAnnotationTests(Wp10Fixture):
    DIR = WORK + "/data/campaign"

    def test_a_deleted_dirfd_is_joined_with_its_annotation_path(self) -> None:
        line = f'9 openat(3<{self.DIR}>(deleted), "x", O_RDONLY) = -1 ENOENT (No such file or directory)'
        parser = self.parse([line])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.accesses(parser), {self.DIR + "/x": ["missing"]})
        self.assertEqual(self.reasons([line], "v3"), {"dirfd-path-not-resolved:openat": 1})

    def test_the_one_space_variant_is_not_accepted_on_arguments_either(self) -> None:
        line = f'9 openat(3<{self.DIR}> (deleted), "x", O_RDONLY) = -1 ENOENT (No such file or directory)'
        self.assertEqual(self.reasons([line]), {"dirfd-path-not-resolved:openat": 1})

    def test_getdents64_on_a_deleted_directory_registers_a_listing_of_its_path(self) -> None:
        line = f'9 getdents64(3<{self.DIR}>(deleted), 0xffff9a2b1000 /* 0 entries */, 32768) = 0'
        self.assertEqual(self.accesses(self.parse([line])), {self.DIR: ["list"]})
        self.assertEqual(self.reasons([line]), {})
        self.assertEqual(self.reasons([line], "v3"), {"directory-fd-not-annotated": 1})

    def test_fchdir_into_a_deleted_directory_updates_the_working_directory(self) -> None:
        parser = self.parse([f'9 fchdir(3<{self.DIR}>(deleted)) = 0'])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(parser.cwd["9"], self.DIR)
        self.assertEqual(self.accesses(parser), {self.DIR: ["dir"]})
        self.assertEqual(self.reasons([f'9 fchdir(3<{self.DIR}>(deleted)) = 0'], "v3"), {"directory-fd-not-resolved:fchdir": 1})

    def test_a_deleted_working_directory_annotation_sets_the_cwd_and_joins_relative_paths(self) -> None:
        line = f'9 openat(AT_FDCWD<{self.DIR}>(deleted), "f", O_RDONLY) = -1 ENOENT (No such file or directory)'
        parser = self.parse([line])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(parser.cwd["9"], self.DIR)
        self.assertEqual(self.accesses(parser), {self.DIR + "/f": ["missing"]})
        self.assertEqual(self.reasons([line], "v3"), {"dirfd-path-not-resolved:openat": 1})

    def test_futimens_on_a_deleted_file_registers_a_write_of_its_path(self) -> None:
        line = f'9 utimensat(5<{self.DIR}/f>(deleted), NULL, NULL, 0) = 0'
        self.assertEqual(self.accesses(self.parse([line])), {self.DIR + "/f": ["write"]})
        self.assertEqual(self.reasons([line]), {})
        self.assertEqual(self.reasons([line], "v3"), {"dirfd-path-not-resolved:utimensat": 1})

    def test_an_empty_path_write_on_a_deleted_file_registers_a_write_of_its_path(self) -> None:
        line = f'9 fchownat(5<{self.DIR}/f>(deleted), "", 0, 0, AT_EMPTY_PATH) = 0'
        self.assertEqual(self.accesses(self.parse([line])), {self.DIR + "/f": ["write"]})
        self.assertEqual(self.reasons([line]), {})
        self.assertEqual(self.reasons([line], "v3"), {"dirfd-path-not-resolved:fchownat": 1})

    def test_lstat_of_an_own_symlink_through_a_deleted_directory_annotation_is_still_recognised(self) -> None:
        """``_single_path`` 也用同一个注解正则：本单元自己创建的符号链接，经带后缀的目录注解 lstat，仍是已知链接的元数据。"""
        base = [f'1 mkdir("{R}/tmpwzmo6c4w", 0700) = 0', f'1 mkdir("{R}/tmpwzmo6c4w/history", 0700) = 0']
        probe = list(RAW_GIT_PROBE)
        self.assertIn("history>, ", probe[4])
        probe[4] = probe[4].replace("history>, ", "history>(deleted), ", 1)
        parser = self.parse(base + probe)
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.problems(parser), [])
        self.assertEqual(self.reasons(base + RAW_GIT_PROBE), {}, "对照：没有后缀的原始行本来就通过")
        self.assertIn("symlink-metadata-not-bound:newfstatat", self.reasons(base + probe, "v3"))

    def test_two_path_calls_with_deleted_annotations_resolve_both_ends(self) -> None:
        line = (f'9 renameat(AT_FDCWD<{self.DIR}>(deleted), "a", AT_FDCWD<{self.DIR}>(deleted), "b") = -1 ENOENT '
                f'(No such file or directory)')
        parser = self.parse([line])
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(sorted(parser.accesses), [self.DIR + "/a", self.DIR + "/b"])
        self.assertEqual(self.reasons([line], "v3"), {"dirfd-path-not-resolved:renameat": 1})


# =====================================================================================================================
# 反例：仍判未解析的写法
# =====================================================================================================================


class NegativeShapeTests(Wp10Fixture):
    BASE = '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = 3</tmp/f>'

    def assertTriple(self, line: str, contract: str = "v4") -> None:
        self.assertEqual(self.reasons([line], contract), TRIPLE, line)
        self.assertFalse(self.audit(self.document(self.pad([line]), contract=contract))["coverage_complete"], line)

    def test_tails_that_are_not_a_single_deleted_suffix_stay_unresolved_under_v4(self) -> None:
        tails = {
            "后缀之后有垃圾": "(deleted)garbage",
            "后缀之后有耗时尾巴": "(deleted) <0.000012>",
            "后缀重复": "(deleted)(deleted)",
            "后缀与空格后再有后缀": "(deleted) (deleted)",
            "一个空格": " (deleted)",
            "两个空格": "  (deleted)",
            "制表符": "\t(deleted)",
            "首字母大写": "(Deleted)",
            "全大写": "(DELETED)",
            "括号没闭合": "(deleted",
            "没有左括号": "deleted)",
            "括号多一个": "(deleted))",
            "括号里有空格": "( deleted)",
            "括号里尾部有空格": "(deleted )",
            "没有括号": " deleted",
            "换成方括号": "[deleted]",
            "后缀之后是逗号": "(deleted),",
            "注解后先跟数字再跟后缀": "0(deleted)",
        }
        for label, tail in tails.items():
            with self.subTest(label=label):
                self.assertTriple(self.BASE + tail)

    def test_a_suffix_without_an_annotation_stays_unresolved(self) -> None:
        for label, line in {
            "返回值后直接跟后缀": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = 3(deleted)',
            "返回值后空格再跟后缀": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = 3 (deleted)',
            "失败返回值后跟后缀": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = -1(deleted)',
        }.items():
            with self.subTest(label=label):
                self.assertTriple(line)

    def test_a_suffix_before_the_return_number_or_inside_the_annotation_slot_stays_unresolved(self) -> None:
        for label, line in {
            "后缀在数字之前": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = (deleted)3</tmp/f>',
            "后缀夹在数字与注解之间": '1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = 3(deleted)</tmp/f>',
        }.items():
            with self.subTest(label=label):
                self.assertTriple(line)

    def test_a_suffix_between_the_closing_parenthesis_and_the_equals_sign_is_not_a_result_segment(self) -> None:
        """后缀夹在右括号与等号之间：``_RESULT`` 以右括号加等号为起点，起点之前的 ``(deleted`` 成了多出来的参数文本，参数切分失败，仍判未解析。"""
        self.assertEqual(self.reasons(['1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) (deleted)= 3</tmp/f>']),
                         {"dirfd-path-not-resolved:openat": 1})

    def test_a_missing_annotation_still_stays_unresolved(self) -> None:
        self.assertEqual(self.reasons(['1 openat(AT_FDCWD</repo>, "/tmp/f", O_RDONLY) = 3']), {"opened-fd-not-resolved:openat": 1})

    def test_malformed_argument_annotations_stay_unresolved(self) -> None:
        for label, argument in {
            "后缀之后有垃圾": "3</d>(deleted)x",
            "后缀重复": "3</d>(deleted)(deleted)",
            "一个空格": "3</d> (deleted)",
            "两个空格": "3</d>  (deleted)",
            "大小写不同": "3</d>(Deleted)",
            "没有尖括号": "3(deleted)",
            "AT_FDCWD 没有尖括号": "AT_FDCWD(deleted)",
            "后缀在尖括号之前": "3(deleted)</d>",
        }.items():
            with self.subTest(label=label):
                self.assertEqual(self.reasons([f'1 openat({argument}, "f", O_RDONLY) = -1 ENOENT (No such file or directory)']),
                                 {"dirfd-path-not-resolved:openat": 1})
        for label, argument in {"getdents64 后缀之后有垃圾": "3</d>(deleted)x", "getdents64 没有尖括号": "3(deleted)"}.items():
            with self.subTest(label=label):
                self.assertEqual(self.reasons([f'1 getdents64({argument}, 0xffff9a2b1000 /* 0 entries */, 32768) = 0']),
                                 {"directory-fd-not-annotated": 1})
        self.assertEqual(self.reasons(['1 utimensat(5</d/f>(deleted)x, NULL, NULL, 0) = 0']), {"dirfd-path-not-resolved:utimensat": 1})

    def test_a_deleted_directory_annotation_that_is_not_an_absolute_path_is_still_not_a_working_directory(self) -> None:
        parser = self.parse(['9 fchdir(3<pipe:[123]>(deleted)) = 0'])
        self.assertEqual(parser.unresolved_reasons, {"directory-fd-not-resolved:fchdir": 1})
        self.assertNotIn("9", parser.cwd)

    def test_a_file_whose_real_name_ends_in_the_suffix_text_is_a_literal_name(self) -> None:
        """名字本身以 ``(deleted)`` 结尾的真实文件：注解里是字面路径（后缀文字在尖括号里面），后缀识别只发生在右尖括号之后。"""
        lines = self.pad([f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/f(deleted)", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{R}/base/f(deleted)>',
                          f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/f(deleted)", O_RDONLY) = 4<{R}/base/f(deleted)>'])
        parser = self.parse(lines)
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.problems(parser), [])
        self.assertIn(R + "/base/f(deleted)", parser.accesses)


# =====================================================================================================================
# 安全
# =====================================================================================================================


class SecurityTests(Wp10Fixture):
    def test_a_descriptor_annotation_outside_the_private_root_is_still_refused(self) -> None:
        """引号里的路径在盘内、返回描述符的真实路径在盘外（经链接重定向之类）：带不带后缀都判「未绑定到私有临时盘」，宿主路径照常登记为读。"""
        base = self.pad([])
        for suffix in ("", "(deleted)"):
            with self.subTest(suffix=suffix):
                lines = base + [f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/x", O_RDONLY) = 3</etc/shadow>{suffix}']
                parser = self.parse(lines)
                self.assertEqual(parser.unresolved_reasons, {})
                self.assertTrue([text for text in self.problems(parser) if "文件描述符未绑定到私有临时盘" in text], suffix)
                self.assertEqual(self.accesses(parser)["/etc/shadow"], ["read"])
                document = self.document(lines)
                self.assertFalse(self.audit(document)["coverage_complete"])
                self.assertIn("/etc/shadow", [item["path"] for item in self.audit(document)["undeclared"]])

    def test_the_annotation_cannot_launder_a_host_path_in_the_quotes(self) -> None:
        """引号里是宿主路径、注解声称在私有盘内：宿主路径照常登记为读并要求声明（注解与引号各自登记，后者不被前者覆盖）。"""
        for suffix in ("", "(deleted)"):
            with self.subTest(suffix=suffix):
                lines = self.pad([f'10 openat(AT_FDCWD<{WORK}>, "/etc/shadow", O_RDONLY) = 3<{R}/base/x>{suffix}'])
                parser = self.parse(lines)
                self.assertIn("/etc/shadow", parser.accesses)
                self.assertFalse(self.audit(self.document(lines))["coverage_complete"])

    def test_an_escaped_greater_than_in_a_name_does_not_end_the_annotation_early(self) -> None:
        """strace 把名字里的 ``>`` 转义成 ``\\>``：``a\\>(deleted)`` 是名字「a>(deleted)」，后缀只在真正的右尖括号之后。"""
        name = R + "/base/a>(deleted)"
        escaped = R + "/base/a\\>(deleted)"
        lines = self.pad([f'10 openat(AT_FDCWD<{WORK}>, "{escaped}", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{escaped}>',
                          f'10 openat(AT_FDCWD<{WORK}>, "{escaped}", O_RDONLY) = 4<{escaped}>(deleted)'])
        parser = self.parse(lines)
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertEqual(self.problems(parser), [])
        self.assertEqual(sorted(parser.accesses[name]), ["read", "write"], "名字是字面的 a>(deleted)，后缀没有被当成名字的一部分之外的东西")
        self.assertNotIn(R + "/base/a", parser.accesses)

    def test_a_name_that_only_looks_like_a_suffix_cannot_hide_a_second_path(self) -> None:
        """名字里没有转义的 ``>`` 不是 strace 的输出形态：注解提前结束，后面的内容成了多余尾巴，整行判未解析（而不是被当成后缀放过）。"""
        line = f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/x", O_RDONLY) = 3<{R}/base/x>(deleted)>/etc/shadow'
        self.assertEqual(self.reasons(self.pad([line])), TRIPLE)

    def test_the_old_inside_form_stays_a_literal_path_and_fails_closed(self) -> None:
        """旧版 strace 的形态（后缀在尖括号里面）：与真名为「x (deleted)」的文件无法区分，不规整；登记的是字面路径，它没被创建过，归属判未证明。"""
        lines = self.pad([f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/x", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{R}/base/x>',
                          f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/x", O_RDONLY) = 4<{R}/base/x (deleted)>'])
        for contract in ("v3", "v4"):
            with self.subTest(contract=contract):
                parser = self.parse(lines, contract)
                self.assertEqual(parser.unresolved_reasons, {}, "解析本身不失败")
                self.assertIn(R + "/base/x (deleted)", parser.accesses, "字面路径，后缀没有被剥掉")
                self.assertTrue([text for text in self.problems(parser) if R + "/base/x (deleted)" in text], "没有创建证明 → 失败关闭")
                document = self.document(lines, contract=contract)
                self.assertFalse(self.audit(document)["coverage_complete"])
                reads = [event for event in document["runtime"]["output_events"] if event["name"] == "openat" and event["kind"] == "read"]
                self.assertTrue(reads)
                self.assertEqual({event["target"] for event in reads}, {R + "/base/x (deleted)"}, "事件里的返回描述符路径也是原样的字面路径")

    def test_the_o_tmpfile_shape_is_accepted_by_the_parser_but_still_lacks_a_creation_proof(self) -> None:
        """``O_TMPFILE`` 返回的描述符注解就是 ``</dir/#ino>(deleted)``：解析不再失败，但那个名字从未被创建（内核里它没有名字），归属判「缺少创建证明」。"""
        lines = self.pad([f'10 openat(AT_FDCWD<{WORK}>, "{R}/base", O_RDWR|O_TMPFILE|O_CLOEXEC, 0600) = 3<{R}/base/#4242>(deleted)'])
        parser = self.parse(lines)
        self.assertEqual(parser.unresolved_reasons, {})
        self.assertTrue([text for text in self.problems(parser) if "缺少创建证明" in text], self.problems(parser))
        self.assertFalse(self.audit(self.document(lines))["coverage_complete"])

    def test_a_deleted_dirfd_never_hides_a_host_directory_from_the_access_set(self) -> None:
        """宿主目录脱链后的 dirfd：按注解路径拼出的宿主路径照常登记（保守地多要求一个声明），不会因后缀而漏记或改归盘内。"""
        line = '9 openat(3</root/host-dir>(deleted), "x", O_RDONLY) = -1 ENOENT (No such file or directory)'
        parser = self.parse([line])
        self.assertEqual(self.accesses(parser), {"/root/host-dir/x": ["missing"]})
        self.assertFalse(self.audit(self.document(self.pad([line])))["coverage_complete"])


# =====================================================================================================================
# 蜕变性质：加上后缀，结论不变
# =====================================================================================================================


class MetamorphicTests(Wp10Fixture):
    """每个场景是一组 strace 行；给每条行尾返回注解追加 ``(deleted)`` 之后，未解析原因、访问集合、归属问题、审计结论逐项相同。"""

    def scenarios(self) -> dict[str, list[str]]:
        b = f'10 mkdir("{R}/base", 0700) = 0'
        create = f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/f", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{R}/base/f>'
        return {
            "私有盘读已知文件": [b, create, f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/f", O_RDONLY) = 4<{R}/base/f>'],
            "私有盘创建": [b, f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/g", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{R}/base/g>'],
            "私有盘读未创建的名字": [b, f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/zzz", O_RDONLY) = 4<{R}/base/zzz>'],
            "宿主读": [b, f'10 openat(AT_FDCWD<{WORK}>, "/etc/hostname", O_RDONLY) = 3</etc/hostname>'],
            "注解重定向到盘外": [b, f'10 openat(AT_FDCWD<{WORK}>, "{R}/base/x", O_RDONLY) = 3</etc/shadow>'],
            "引号在宿主、注解在盘内": [b, f'10 openat(AT_FDCWD<{WORK}>, "/etc/shadow", O_RDONLY) = 3<{R}/base/x>'],
            "O_TMPFILE 形态": [b, f'10 openat(AT_FDCWD<{WORK}>, "{R}/base", O_RDWR|O_TMPFILE, 0600) = 3<{R}/base/#7>'],
            "plain open": [b, f'10 open("{R}/base/h", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{R}/base/h>',
                           f'10 open("{R}/base/h", O_RDONLY) = 4<{R}/base/h>'],
            "目录打开": [b, f'10 openat(AT_FDCWD<{WORK}>, "{R}/base", O_RDONLY|O_DIRECTORY) = 5<{R}/base>'],
            "非路径注解": [b, f'10 openat(AT_FDCWD<{WORK}>, "{R}/base", O_RDONLY) = 6<pipe:[77]>'],
        }

    def summary(self, lines: list[str]) -> tuple[Any, ...]:
        parser = self.parse(lines)
        document = self.document(lines)
        report = self.audit(document)
        return (dict(parser.unresolved_reasons), self.accesses(parser), self.problems(parser), report["coverage_complete"],
                sorted((item["root"], item["path"], tuple(item["kinds"])) for item in report["undeclared"]))

    def test_adding_the_suffix_to_every_return_annotation_changes_nothing(self) -> None:
        for label, lines in self.scenarios().items():
            with self.subTest(label=label):
                changed = with_suffix(lines)
                self.assertNotEqual(changed, lines, "场景里至少有一条行被加了后缀")
                self.assertEqual(self.summary(changed), self.summary(lines))

    def test_under_v3_the_suffixed_scenarios_are_all_unresolved(self) -> None:
        """对照：v3 对加了后缀的整行一律未解析（v3 一字不变）。"""
        for label, lines in self.scenarios().items():
            with self.subTest(label=label):
                changed = with_suffix(lines)
                self.assertFalse([key for key in self.reasons(lines, "v3") if key.startswith("result-or-string-incomplete")], label)
                reasons = self.reasons(changed, "v3")
                self.assertTrue([key for key in reasons if key.startswith("result-or-string-incomplete:open")], (label, reasons))


# =====================================================================================================================
# 正则层的性质检验
# =====================================================================================================================


class RegexLevelTests(unittest.TestCase):
    ANNOTATIONS = ["/a/b", "/a b/c d", "/tmp/x\\>y", "/a\\\\b", "/root/docker/capture-cli/" + "x" * 150, "socket:[123]", "pipe:[5]",
                   "anon_inode:[eventfd]", "/proc/self/fd/3", "/tmp/(deleted)", "/tmp/x (deleted)", ""]
    NUMBERS = ["3", "0", "-1", "?", "4294967295"]
    ERRNO_TAILS = ["", " ENOENT (No such file or directory)", " EACCES (Permission denied)", " ERESTARTSYS (To be restarted if SA_RESTART is set)"]

    def results(self) -> list[tuple[str, str]]:
        """（不带后缀的行, 带 ``(deleted)`` 的行）。"""
        rows = []
        for annotation in self.ANNOTATIONS:
            for number in self.NUMBERS:
                for tail in self.ERRNO_TAILS:
                    for spaces in (" ", "           "):
                        base = f'AT_FDCWD</r>, "/x", O_RDONLY){spaces}= {number}<{annotation}>'
                        rows.append((base + tail, base + "(deleted)" + tail))
        return rows

    def test_without_the_suffix_v4_matches_exactly_like_v3(self) -> None:
        for text, _ in self.results():
            old, new = ra._RESULT.search(text), ra._RESULT_V4.search(text)
            self.assertEqual(None if old is None else (old.span(), old.groups()), None if new is None else (new.span(), new.groups()), text)

    def test_with_the_suffix_only_v4_matches_and_the_groups_equal_the_suffix_free_ones(self) -> None:
        """后缀在注解之后、错误码之前时，错误码尾巴本身是另一回事（成功不带错误码），这里只验后缀在行尾的形态。"""
        for annotation in self.ANNOTATIONS:
            for number in self.NUMBERS:
                plain = f'AT_FDCWD</r>, "/x", O_RDONLY)           = {number}<{annotation}>'
                suffixed = plain + "(deleted)"
                self.assertIsNone(ra._RESULT.search(suffixed), suffixed)
                new, base = ra._RESULT_V4.search(suffixed), ra._RESULT_V4.search(plain)
                self.assertIsNotNone(new, suffixed)
                self.assertEqual((new.start(), new.groups()), (base.start(), base.groups()), suffixed)
                self.assertIsNone(ra._RESULT_V4.search(plain + " (deleted)"), "带空格的写法不是 strace 的输出形态")

    def test_the_returned_descriptor_pattern_follows_the_same_rule(self) -> None:
        for annotation in [item for item in self.ANNOTATIONS if item]:
            plain = f'AT_FDCWD</r>, "/x", O_RDONLY) = 3<{annotation}>'
            self.assertIsNone(ra._RETURNED_FD.search(plain + "(deleted)"))
            new = ra._RETURNED_FD_V4.search(plain + "(deleted)")
            self.assertIsNotNone(new)
            self.assertEqual(new.group(1), annotation)
            self.assertIsNone(ra._RETURNED_FD_V4.search(plain + " (deleted)"))
            self.assertEqual(ra._RETURNED_FD_V4.search(plain).group(1), ra._RETURNED_FD.search(plain).group(1))

    def test_the_annotation_patterns_follow_the_same_rule(self) -> None:
        for pattern_v3, pattern_v4, head in ((ra._ANNOTATION, ra._ANNOTATION_V4, "5"), (ra._ANNOTATION, ra._ANNOTATION_V4, "AT_FDCWD"),
                                              (ra._FD_ANNOTATION, ra._FD_ANNOTATION_V4, "5")):
            for annotation in self.ANNOTATIONS:
                plain = f"{head}<{annotation}>"
                self.assertEqual(None if pattern_v3.fullmatch(plain) is None else pattern_v3.fullmatch(plain).groups(),
                                 None if pattern_v4.fullmatch(plain) is None else pattern_v4.fullmatch(plain).groups(), plain)
                self.assertIsNone(pattern_v3.fullmatch(plain + "(deleted)"), plain)
                matched = pattern_v4.fullmatch(plain + "(deleted)")
                if pattern_v3.fullmatch(plain) is not None:
                    self.assertIsNotNone(matched, plain)
                    self.assertEqual(matched.groups(), pattern_v3.fullmatch(plain).groups())
                self.assertIsNone(pattern_v4.fullmatch(plain + " (deleted)"), "带空格的写法不是 strace 的输出形态")

    GARBAGE_TAILS = ("(deleted)x", "(deleted)(deleted)", " (deleted)", "  (deleted)", "\t(deleted)", "(Deleted)", "(deleted) <0.1>", "(deleted", "deleted)",
                     "(deleted))", " ", "x")

    def test_garbage_after_the_suffix_is_rejected_by_every_v4_pattern(self) -> None:
        for annotation in [item for item in self.ANNOTATIONS if item]:
            for tail in self.GARBAGE_TAILS:
                if tail.strip() == "":
                    continue
                with self.subTest(annotation=annotation[:20], tail=tail):
                    self.assertIsNone(ra._RETURNED_FD_V4.search(f'AT_FDCWD</r>, "/x", O_RDONLY) = 3<{annotation}>{tail}'))
                    self.assertIsNone(ra._RESULT_V4.search(f'AT_FDCWD</r>, "/x", O_RDONLY) = 3<{annotation}>{tail}'))
                    for head, pattern in (("5", ra._ANNOTATION_V4), ("AT_FDCWD", ra._ANNOTATION_V4), ("5", ra._FD_ANNOTATION_V4)):
                        self.assertIsNone(pattern.fullmatch(f"{head}<{annotation}>{tail}"))

    def test_the_suffix_needs_an_annotation_in_front_of_it_in_every_v4_pattern(self) -> None:
        for number in self.NUMBERS:
            self.assertIsNone(ra._RESULT_V4.search(f'AT_FDCWD</r>, "/x", O_RDONLY) = {number}(deleted)'))
            self.assertIsNone(ra._RESULT_V4.search(f'AT_FDCWD</r>, "/x", O_RDONLY) = {number} (deleted)'))
            self.assertIsNone(ra._RETURNED_FD_V4.search(f'AT_FDCWD</r>, "/x", O_RDONLY) = {number}(deleted)'))
        for head in ("5", "AT_FDCWD", "-1"):
            self.assertIsNone(ra._ANNOTATION_V4.fullmatch(head + "(deleted)"))
            self.assertIsNone(ra._FD_ANNOTATION_V4.fullmatch(head + "(deleted)"))

    def test_random_garbage_after_the_annotation_is_rejected_by_both_generations(self) -> None:
        rng = random.Random(1015)
        # 字母表里没有 ``=``：随机尾巴里不可能再拼出第二个「) = 值」候选位置，期望值只由尾巴本身是否「可选后缀＋空白」决定。
        alphabet = "(deleted) xyz<>),\t0123"
        checked = allowed_count = 0
        for _ in range(4000):
            garbage = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
            if garbage.strip() == "":
                continue                                  # 纯空白尾巴 v3 也认
            text = f'AT_FDCWD</r>, "/x", O_RDONLY) = 3</a/b>{garbage}'
            checked += 1
            allowed = re.fullmatch(r"(?:\(deleted\))?\s*", garbage) is not None
            allowed_count += allowed
            self.assertEqual(ra._RESULT_V4.search(text) is not None, allowed, repr(garbage))
            self.assertIsNone(ra._RESULT.search(text), repr(garbage))
        self.assertGreater(checked, 3500)
        # 随机尾巴几乎都不合法；再补一批「合法后缀＋随机空白」的正例，保证放行一侧也被随机覆盖。
        for _ in range(500):
            tail = "(deleted)" + "".join(rng.choice(" \t") for _ in range(rng.randint(0, 4)))
            text = f'AT_FDCWD</r>, "/x", O_RDONLY) = 3</a/b>{tail}'
            self.assertIsNotNone(ra._RESULT_V4.search(text), repr(tail))
            self.assertIsNone(ra._RESULT.search(text), repr(tail))

    def test_the_v3_patterns_are_unchanged_to_the_character(self) -> None:
        """v3／control 四链共用的原正则，文本与改动前逐字相同。"""
        self.assertEqual(ra._ANNOTATION.pattern, r"(AT_FDCWD|-?\d+)<((?:[^>\\]|\\.)*)>")
        self.assertEqual(ra._FD_ANNOTATION.pattern, r"(\d+)<((?:[^>\\]|\\.)*)>")
        self.assertEqual(ra._RESULT.pattern,
                         r"\)\s*=\s*(-?\d+|\?)(?:<(?:[^>\\]|\\.)*>)?(?:\s+(E[A-Z0-9]+)(?:\s+\([^)]*\))?)?\s*$")
        self.assertEqual(ra._RETURNED_FD.pattern, r"\)\s*=\s*\d+<((?:[^>\\]|\\.)*)>\s*$")

    def test_the_v4_patterns_are_exactly_the_v3_ones_plus_the_optional_suffix(self) -> None:
        suffix = r"(?:\(deleted\))?"
        self.assertEqual(ra._DELETED_SUFFIX, suffix)
        self.assertEqual(ra._ANNOTATION_V4.pattern, ra._ANNOTATION.pattern + suffix)
        self.assertEqual(ra._FD_ANNOTATION_V4.pattern, ra._FD_ANNOTATION.pattern + suffix)
        self.assertEqual(ra._RETURNED_FD_V4.pattern, ra._RETURNED_FD.pattern.removesuffix(r"\s*$") + suffix + r"\s*$")
        self.assertEqual(ra._RESULT_V4.pattern, ra._RESULT.pattern.replace(r"(?:<(?:[^>\\]|\\.)*>)?", r"(?:<(?:[^>\\]|\\.)*>" + suffix + ")?"))


# =====================================================================================================================
# v3 / 默认构造 / control 不变
# =====================================================================================================================


class V3UnchangedTests(Wp10Fixture):
    def test_the_parser_selects_the_patterns_by_contract(self) -> None:
        v4 = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v4")
        v3 = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v3")
        default = ra.TraceParser(strict=True)
        control = control_audit.TraceParser(strict=True, runtime_context=self.control_context())
        self.assertEqual((v4.annotation_pattern, v4.result_pattern, v4.returned_fd_pattern), (ra._ANNOTATION_V4, ra._RESULT_V4, ra._RETURNED_FD_V4))
        for parser in (v3, default):
            self.assertEqual((parser.annotation_pattern, parser.result_pattern, parser.returned_fd_pattern),
                             (ra._ANNOTATION, ra._RESULT, ra._RETURNED_FD))
        self.assertEqual((control.annotation_pattern.pattern, control.result_pattern.pattern, control.returned_fd_pattern.pattern),
                         (ra._ANNOTATION.pattern, ra._RESULT.pattern, ra._RETURNED_FD.pattern))

    def test_v3_documents_for_the_suffix_free_lines_are_identical_to_before(self) -> None:
        """没有后缀的行，v3 文档与 v4 之外的任何选择无关：同一份行在 v3 下的文档不含任何 R10 字段，schema 不变。"""
        lines = private_heartbeat_lines(suffix="")
        document = self.document(lines, contract="v3")
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
        self.assertTrue(all(set(event) == {"path", "kind", "name", "succeeded", "flags", "target"}
                            for event in document["runtime"]["output_events"]))

    def test_a_v3_trace_with_the_suffix_is_still_rejected_by_the_whole_audit(self) -> None:
        document = self.document(host_heartbeat_lines(), contract="v3")
        self.assertEqual(document["coverage"]["unresolved_reasons"], TRIPLE)
        self.assertFalse(self.audit(document)["coverage_complete"])


# =====================================================================================================================
# 整份语料（可选）：v3 完整输出的 sha256 与改动前逐字相同
# =====================================================================================================================

CORPUS_DIR = Path(os.environ["WP_R10_CORPUS_DIR"]) if os.environ.get("WP_R10_CORPUS_DIR") else None
# 改动前（第九轮集成分支 77fd36d04）三份真实 ARM64 原始 strace 语料经 ``filter_stream`` 的完整输出（json，sort_keys）sha256。
# 私有根固定为 DIGEST_ROOT。三份语料里都没有 ``(deleted)``：v3 的这些值永远不变；v4 的值随后续轮次可变，不在此钉。
DIGEST_ROOT = "/nonexistent-r10-digest/a/b/private/output"
V3_DIGESTS = {
    ("raw-diag-directed.gz", "v3-lenient"): "599426d995c79e2038c12b333ef22d6716d7b7248da61ae58c5ca470ee797a87",
    ("raw-diag-directed.gz", "v3-strict"): "647d66cae332317c5262dce8753bc88878403b2d533d4d5e9c8ffb0fa7191728",
    ("raw-diag-directed.gz", "v3-strict-ctx"): "548fdd7caf27bf3261b9a9a078df6794a3198dc4ccfc4b34096a5c4403d831f7",
    ("raw-diag-driverwait.gz", "v3-lenient"): "410e3e4514eb57091084abb3d9552ad5f3893a7ea0c148b09b9bc86fc82f4dfc",
    ("raw-diag-driverwait.gz", "v3-strict"): "f7822271070946bcf42fb22325830f4edd87daca0aa34f15f308786bdeed4bf1",
    ("raw-diag-driverwait.gz", "v3-strict-ctx"): "148aa1ccfb06d65c581c82964a34f28eab7e476259327afd977a5b67b0d3343b",
    ("raw-diag-ingress.gz", "v3-lenient"): "e1874130783bd03dd5081ddf38c45d3a49be9bb77304a83e0a794eed05d27049",
    ("raw-diag-ingress.gz", "v3-strict"): "7f3bcda895baf2bc576df2acffe1bd26e6ee97d9a408cf1968cc63e8c8efffdf",
    ("raw-diag-ingress.gz", "v3-strict-ctx"): "472c4b5235dc0b92510220fab4c587e8479526461597a2f7ebedf27c72fea822",
}


@unittest.skipUnless(CORPUS_DIR is not None and CORPUS_DIR.is_dir(),
                     "整份语料对拍默认不跑（约 1 分钟）：设置环境变量 WP_R10_CORPUS_DIR=<含 raw-diag-*.gz 的目录> 才启用")
class FullCorpusV3DigestTests(Wp10Fixture):
    def test_v3_complete_output_is_byte_identical_to_before(self) -> None:
        self.use_root(DIGEST_ROOT)
        modes = {"v3-lenient": dict(strict=False), "v3-strict": dict(strict=True),
                 "v3-strict-ctx": dict(strict=True, runtime_context=copy.deepcopy(self.context))}
        for (name, mode), expected in sorted(V3_DIGESTS.items()):
            with self.subTest(corpus=name, mode=mode):
                with gzip.open(CORPUS_DIR / name, "rt", encoding="utf-8", errors="surrogateescape") as stream:
                    document = ra.filter_stream(stream, ["/"], **modes[mode])
                encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogateescape")
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected)

    def test_the_corpora_contain_no_deleted_suffix_so_v4_has_nothing_to_relax_there(self) -> None:
        for name in sorted({key[0] for key in V3_DIGESTS}):
            with self.subTest(corpus=name):
                with gzip.open(CORPUS_DIR / name, "rt", encoding="utf-8", errors="surrogateescape") as stream:
                    self.assertEqual(sum(1 for line in stream if "(deleted)" in line), 0)


if __name__ == "__main__":
    unittest.main()
