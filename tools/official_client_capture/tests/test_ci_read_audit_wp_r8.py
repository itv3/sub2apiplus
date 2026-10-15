"""WP-R8／WP-R9（审计解析器加固第八轮）：多进程 strace 乱序下的输出归属，以及 v4 输出事件的名字级去重。

R8——「先成功读到、后看到创建事件」不再立即判问题
================================================

背景（10-15 ARM64 真实后台验证，``test_codex_upgrade_staging_dispatch`` 26 项全过却导出失败）：``strace -f`` 按各被追踪进程
「系统调用返回被 tracer 处理的先后」逐行输出，不是因果顺序。supervisor 的心跳进程 ``openat(O_APPEND|O_CREAT)`` 创建
``minute-ledger.ndjson`` 已在内核里完成，主进程紧接着的 ``newfstatat`` 却先被打印，创建事件排在后面。旧跟踪器按
「轨迹顺序＝因果顺序」把那条成功的 stat 判为「首次读取前未证明由本轮生成」。

修法（``read_audit_runtime.OutputTrackerV4`` 的「待补证／销账」，完整论证见该类的说明）：私有盘内未证明名字上的、不改名字空间且要求
对象存在的访问（kind 不是 write／missing，成功与否都算）记入 ``awaiting``；之后**同一个名字**被创建（O_CREAT 的 open／creat、
mkdir、symlink、link 的目的名字、rename 带入）就销账；轨迹结束仍未销账的在 ``report()`` 里按原文字报为问题。

本模块的 R8 部分：

* ``AwaitingProofTrackerTests``：跟踪器层面——minute-ledger 形态的正例、每一种创建形态的销账、每一类可被待补证的访问、
  反例（从未创建、销账发生在别的名字上、rename 目标不是该名字、轨迹结束未销账、创建被拒绝不销账、宿主路径不适用、
  写类调用的未证明名字仍立即判问题）、清算的确定性与只读性；
* ``ReorderedLinesEndToEndTests``：strace 行 → 解析器 → 重放整条链，交错的两个 pid 复现 minute-ledger 形态；v3 合同不放宽；
  篡改（删创建事件、删读取事件、伪造干净的 outputs）都被重放拒绝；
* ``ReorderPropertyTests``：随机合法历史 + 合法的「创建事件延后上报」重排，必须零问题、名字表与原顺序一致；
  把创建事件整个删掉则必须报出问题。

R9——v4 输出事件的名字级去重
==========================

背景：10-15 ARM64，``test_claude_upgrade_compare_accept`` 的轨迹被截在 ``MAX_OUTPUT_EVENTS_V4``（1,000,000 条）后又丢 285,993 条，判
output-event-limit。保留下来的 100 万条里 88% 是对私有盘内路径的重复成功 stat／读。旧去重键含全局变更纪元，约 5.5 万条写／rename／unlink
把纪元切得很碎，同一个文件在每个纪元里都重新保留一条。

做法：``OutputTrackerV4`` 给每个名字一个版本号（任何改变该名字存在性、对象身份或待补证状态的操作才换，目录 rename 连同整棵子树），
``dedup_key`` 给出事件到达时的去重键 = 事件自身字段 + 它判定所依赖的那几个名字的状态，不含全局纪元；解析器按此键去重。
本模块的 R9 部分：

* ``NameLevelKeyExactnessTests``：随机对拍「同键事件的观察结果完全一致」（键的精确性）；不相干的写不使键变、名字自己（或祖先目录）变了键必变；
  事件每个字段都进键；两路径调用两端共用一份归一化状态；永不去重的事件清单；O_EXCL 的「刚被创建」位；版本号只在该变的名字上变、从不重复；
* ``DedupVerdictTests``：随机对拍「去重不改变判定」（全量事件的报告＝只重放保留事件的报告＝解析器自己的报告；干净的流通过完整重放；
  带问题的流被拒绝）；人为把键收窄，同一份对拍立刻变红；紧跟创建的事件必须保留；
* ``NameLevelDedupEndToEndTests``：strace 行端到端——对别的名字的写不使重复 stat 失效（旧规则 50 条，新规则 1 条）、目录 rename 之后新旧名字上的读取各保留一条；
* ``CompareAcceptShapedReductionTests``：与 compare_accept 同形态的合成负载（配比取自上面 100 万条保留事件的计数，读取类占 96%），
  去重前后相差约 10 倍、相对旧规则少 5～7 倍。

整链实测（macOS 本机，Python 3.14，strace 行 → 解析器 → 文档 → JSON → 重放；合成负载由 ``CompareAcceptShape`` 渲染成 strace 行；
旧树＝基线 51a7a93f4，新树＝本轮）：

    负载                                   树      行数      保留事件   解析耗时   轨迹 JSON   峰值内存   重放耗时
    compare_accept 形态，比例 0.1          旧     98,158      75,191     3.3 s     20.2 MiB    165 MiB     0.1 s
                                           新     98,158      11,734     3.3 s      4.2 MiB     98 MiB     0.0 s
    compare_accept 形态，比例 1.0          旧    981,617     929,619    35.1 s    257.6 MiB  1,306 MiB     1.9 s
    （约 124 万条事件；旧树保留数已逼近 100 万上限）  新    981,617     117,416    35.3 s     44.9 MiB    601 MiB     0.4 s
    最坏情形（25 万文件，各创建一次再各 stat    旧     250,000     750,001    70.2 s       —       1,393 MiB     2.4 s
    一次，事件互不相同、无法去重）            新     250,000     750,001    71.1 s       —       1,502 MiB     2.6 s

结论：形态相近的负载保留事件约少 8 倍（旧 92.9 万 → 新 11.7 万），解析耗时不变，内存与 JSON 约减半以上；事件互不相同的最坏情形下新增的去重键集
约多 8% 内存（每个不改状态的保留事件连同名字版本表约 450 字节），耗时不变。所以 ``MAX_OUTPUT_EVENTS_V4`` 不用再提。三份真实语料（ARM64 原始 strace，
约 25 万行）v4 保留事件数：ingress 973→856，directed 52→52，driverwait 79,913→23,808；三份的 v3 完整输出逐字节不变。
"""

from __future__ import annotations

import collections
import copy
import random
import unittest
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests import test_ci_read_audit_v4_tracker as T
from tools.official_client_capture.tests.test_ci_read_audit_v4_trace import AuditFixture

D = T.D
ROOT = T.ROOT
LEFTOVER = "首次读取前未证明由本轮生成："


def tracker_of(*groups: list[dict[str, Any]]) -> tuple[runtime.OutputTrackerV4, list[bool]]:
    return T.run(*groups)


def document_of(*groups: list[dict[str, Any]]) -> dict[str, Any]:
    """像解析器一样给事件标变更纪元，附上清算后的 outputs：供重放端单测用。"""
    events = [dict(event) for group in groups for event in group]
    tracker = runtime.OutputTrackerV4(T.CONTRACT)
    epoch = 0
    for event in events:
        event["epoch"] = epoch
        if tracker.observe_event(event):
            epoch += 1
    return {"output_events": events, "outputs": tracker.report()}


def ledger_events(b: T.Builder, name: str = "ledger") -> tuple[str, list[list[dict[str, Any]]]]:
    """10-15 实测的 minute-ledger 形态：四次缺失探测 → 一次成功 stat → O_APPEND|O_CREAT 创建 → 若干 stat → unlinkat。"""
    path = D + "/sup/" + name
    groups = [b.mkdir(D), b.mkdir(D + "/sup")]
    groups += [b.probe(path) for _ in range(4)]
    groups.append(b.stat(path))
    groups.append(b.create(path, extra=("O_APPEND", "O_CLOEXEC")) + b.create(path, extra=("O_APPEND", "O_CLOEXEC")))
    groups += [b.stat(path), b.stat(path), b.probe(D + "/sup/other")]
    groups.append(b.unlink(path, name="unlinkat"))
    return path, groups


class AwaitingProofTrackerTests(unittest.TestCase):
    """跟踪器层面的待补证／销账状态机。"""

    def setUp(self) -> None:
        self.b = T.Builder()

    def assertClean(self, tracker: runtime.OutputTrackerV4) -> None:
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(tracker.awaiting, {})

    # ------------------------------------------------------------------ 正例

    def test_the_minute_ledger_shape_is_accepted(self) -> None:
        """真实形态：missing×4 → stat 成功（此时名字未证明）→ openat(O_APPEND|O_CREAT) 创建 → stat 若干 → unlinkat。"""
        path, groups = ledger_events(self.b)
        tracker, changed = tracker_of(*groups)
        self.assertClean(tracker)
        self.assertNotIn(path, tracker.names, "末尾的 unlinkat 删掉了它")
        flat = [event for group in groups for event in group]
        stat_index = next(i for i, e in enumerate(flat) if e["name"] == "newfstatat" and e["succeeded"] and e["path"] == path)
        self.assertFalse(changed[stat_index], "待补证的读取不改变名字空间，返回 False（变更纪元不推进）")
        create_index = next(i for i, e in enumerate(flat) if e["name"] == "openat" and e["kind"] == "write")
        self.assertTrue(changed[create_index], "销账的创建本身照旧改变名字空间")

    def test_the_debt_is_visible_between_the_read_and_the_creation(self) -> None:
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.stat(D + "/p"))
        self.assertEqual(list(tracker.awaiting), [D + "/p"])
        self.assertEqual(tracker.problems, set(), "待补证期间不是立即问题")
        self.assertEqual(tracker.report()["problems"], [LEFTOVER + D + "/p"], "此刻清算就会报出")
        tracker.observe_event(b.create(D + "/p")[0])
        self.assertClean(tracker)

    def test_every_creation_form_settles_the_same_name(self) -> None:
        b = self.b
        p = D + "/p"
        forms = {
            "openat O_CREAT": lambda: b.create(p),
            "openat O_CREAT 无 EXCL": lambda: b.create(p, extra=("O_TRUNC",)),
            "openat2 O_CREAT": lambda: [b.event("openat2", p, flags=("O_CREAT", "O_WRONLY"), target=p)],
            "open O_CREAT": lambda: [b.event("open", p, flags=("O_CREAT", "O_WRONLY"), target=p)],
            "creat": lambda: [b.event("creat", p, flags=("O_CREAT", "O_TRUNC", "O_WRONLY"), target=p)],
            "mkdir": lambda: b.mkdir(p),
            "mkdirat": lambda: [b.event("mkdirat", p)],
            "symlink": lambda: b.symlink(p, "x", name="symlink"),
            "symlinkat": lambda: b.symlink(p, "x", name="symlinkat"),
            "link": lambda: b.link(D + "/src", p),
            "linkat": lambda: b.link(D + "/src", p, name="linkat"),
            "rename": lambda: b.rename(D + "/src", p),
            "renameat": lambda: b.rename(D + "/src", p, name="renameat"),
            "renameat2 NOREPLACE": lambda: b.rename(D + "/src", p, name="renameat2", flags=("RENAME_NOREPLACE",)),
        }
        for label, make in forms.items():
            with self.subTest(creation=label):
                b = self.b = T.Builder()
                tracker, _ = tracker_of(b.mkdir(D), b.create(D + "/src"), b.stat(p), make())
                self.assertClean(tracker)
                self.assertIn(p, tracker.names)

    def test_every_awaitable_access_form_is_settled_by_the_creation(self) -> None:
        """成功的读、stat、打开目录、列目录、执行，以及失败的读取类调用（EACCES、EINVAL）都意味着对象存在，同样待补证。"""
        b = self.b
        p = D + "/p"
        accesses = {
            "openat 读": lambda: b.read(p),
            "newfstatat": lambda: b.stat(p),
            "open 目录": lambda: [b.event("openat", p, kind="dir", flags=("O_DIRECTORY", "O_RDONLY"), target=p)],
            "getdents64": lambda: [b.event("getdents64", p, kind="list")],
            "execve": lambda: [b.event("execve", p, kind="exec")],
            "readlink EINVAL": lambda: b.readlink(p, ok=False, errno="EINVAL"),
            "openat EACCES": lambda: [b.event("openat", p, kind="stat", ok=False, errno="EACCES")],
            "execve EACCES": lambda: [b.event("execve", p, kind="stat", ok=False, errno="EACCES")],
        }
        for label, make in accesses.items():
            with self.subTest(access=label):
                b = self.b = T.Builder()
                tracker, changed = tracker_of(b.mkdir(D), make())
                self.assertEqual(list(tracker.awaiting), [p])
                self.assertFalse(changed[-1])
                tracker.observe_event(b.create(p)[0])
                self.assertClean(tracker)

    def test_directory_rename_carries_the_whole_subtree_and_settles_every_awaited_name(self) -> None:
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.mkdir(D + "/s"), b.mkdir(D + "/s/t"), b.create(D + "/s/x"), b.create(D + "/s/t/y"),
                                b.stat(D + "/n/x"), b.stat(D + "/n/t/y"), b.read(D + "/n"))
        self.assertEqual(set(tracker.awaiting), {D + "/n/x", D + "/n/t/y", D + "/n"})
        tracker.observe_event(b.rename(D + "/s", D + "/n")[0])
        self.assertClean(tracker)

    def test_a_name_awaited_again_after_deletion_needs_a_new_creation(self) -> None:
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.stat(D + "/p"), b.create(D + "/p"), b.unlink(D + "/p"), b.stat(D + "/p"))
        self.assertEqual(list(tracker.awaiting), [D + "/p"], "删除之后的又一次未证明读取是新的一笔债")
        self.assertEqual(tracker.report()["problems"], [LEFTOVER + D + "/p"])

    # ------------------------------------------------------------------ 反例

    def test_a_name_that_is_never_created_is_reported_at_the_end(self) -> None:
        b = self.b
        tracker, changed = tracker_of(b.mkdir(D), b.stat(D + "/ghost"), b.read(D + "/ghost"), b.stat(D + "/ghost"))
        self.assertEqual(tracker.report()["problems"], [LEFTOVER + D + "/ghost"], "同一个名字的多次读取只算一笔债")
        self.assertEqual(changed, [True, False, False, False])
        fresh = T.Builder()
        with self.assertRaisesRegex(runtime.RuntimeContractError, "来源不明"):
            runtime._replay_events_v4(document_of(fresh.mkdir(D), fresh.stat(D + "/ghost")), T.CONTRACT)

    def test_settling_is_by_exact_name_only(self) -> None:
        """销账只认同一个名字：同目录的别的名字、子项、父目录、前缀相同的名字、目的名字不同的 rename／link 都不销账。"""
        p = D + "/p"
        for label, make, still_awaiting in (
                ("同目录的另一个名字", lambda b: b.create(D + "/q"), p),
                ("名字的子项（父目录未证明，创建本身被拒）", lambda b: b.mkdir(D + "/p/c"), p),
                ("名字的父目录", lambda b: b.mkdir(D + "/dir"), D + "/dir/p"),
                ("前缀相同的名字", lambda b: b.create(D + "/p2"), p),
                ("目的名字不同的 rename", lambda b: b.rename(D + "/src", D + "/m"), p),
                ("目的名字不同的 link", lambda b: b.link(D + "/src", D + "/m"), p),
                ("rename 的来源端恰是该名字所在目录里的另一个名字", lambda b: b.rename(D + "/src", D + "/dir2"), p)):
            with self.subTest(case=label):
                b = T.Builder()
                tracker, _ = tracker_of(b.mkdir(D), b.create(D + "/src"), b.stat(p), b.stat(D + "/dir/p"), make(b))
                self.assertIn(still_awaiting, tracker.awaiting, label)
                self.assertIn(LEFTOVER + still_awaiting, tracker.report()["problems"], label)

    def test_a_rename_whose_old_name_is_the_awaited_one_does_not_settle_it(self) -> None:
        """rename 解绑的是来源名字：该名字被待补证、又被当作 rename 来源时来源未证明，两笔问题都在。"""
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.stat(D + "/p"), b.rename(D + "/p", D + "/q"))
        problems = tracker.report()["problems"]
        self.assertIn(LEFTOVER + D + "/p", problems)
        self.assertTrue(any("重命名的来源未证明" in text for text in problems), problems)

    def test_a_rejected_creation_does_not_settle(self) -> None:
        """创建本身被判问题（父目录未证明、名字已存在……）时名字没有绑定，债务保留。"""
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.stat(D + "/nodir/p"), b.create(D + "/nodir/p"))
        problems = tracker.report()["problems"]
        self.assertIn(LEFTOVER + D + "/nodir/p", problems)
        self.assertTrue(any("缺少创建证明" in text for text in problems), problems)
        b = self.b = T.Builder()
        tracker, _ = tracker_of(b.mkdir(D), b.stat(D + "/nodir/p"), b.mkdir(D + "/nodir/p"))
        self.assertIn(LEFTOVER + D + "/nodir/p", tracker.report()["problems"])
        b = self.b = T.Builder()
        tracker, _ = tracker_of(b.mkdir(D), b.stat(D + "/p"), b.create(D + "/p", ok=False, errno="EEXIST"))
        self.assertIn(D + "/p", tracker.awaiting, "EEXIST 的失败创建不绑定名字")

    def test_missing_probes_never_create_a_debt(self) -> None:
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.probe(D + "/a"), b.probe(D + "/b", "ENOTDIR"), b.probe(D + "/c", name="openat"))
        self.assertClean(tracker)

    def test_writes_on_unproven_names_are_still_immediate_problems(self) -> None:
        """待补证只给「不改名字空间且要求对象存在」的读取类访问；写类调用对未证明名字照旧立即判问题，也不记债。"""
        b = self.b
        p = D + "/p"
        cases = {
            "无 O_CREAT 的写打开": ("缺少创建证明", [b.event("openat", p, flags=("O_WRONLY",), target=p)]),
            "元数据写": ("未建立合同的输出操作", [b.event("chmod", p)]),
            "删除": ("删除未证明", b.unlink(p)),
            "rename 来源": ("来源未证明", b.rename(p, D + "/q")),
            "link 来源": ("来源未证明", b.link(p, D + "/q")),
            "新建目录的父目录未知": ("父目录未证明", b.mkdir(D + "/x/y")),
            "未知路径上的 EEXIST": ("EEXIST 落在未知路径", b.mkdir(p, ok=False, errno="EEXIST")),
        }
        for label, (fragment, events) in cases.items():
            with self.subTest(case=label):
                tracker, _ = tracker_of(T.Builder().mkdir(D), events)
                self.assertTrue(any(fragment in text for text in tracker.problems), (tracker.problems, label))
                self.assertEqual(tracker.awaiting, {}, "写类事件不记债")

    def test_host_paths_are_outside_the_rule(self) -> None:
        """私有盘之外的路径不适用待补证：既不记债，也不进覆盖集合（宿主路径由宿主快照与策略声明判定）。"""
        b = self.b
        tracker, changed = tracker_of(b.mkdir(D), [b.event("openat", "/etc/passwd", kind="read", flags=("O_RDONLY",), target="/etc/passwd")],
                                      [b.event("newfstatat", "/host/data/x", kind="stat")])
        self.assertEqual((changed, tracker.awaiting, tracker.report()["problems"]), ([True, False, False], {}, []))
        self.assertNotIn("/etc/passwd", tracker.report()["paths"])

    def test_through_a_symlink_is_still_an_immediate_problem(self) -> None:
        """路径的中间分量是符号链接属于跟随式访问，先于待补证判断，立即判问题（不能靠「以后会创建」放行）。"""
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.symlink(D + "/l", "x"), b.stat(D + "/l/p"))
        self.assertTrue(any("跟随式访问" in text for text in tracker.problems), tracker.problems)
        self.assertEqual(tracker.awaiting, {})

    # ------------------------------------------------------------------ 清算的性质

    def test_report_is_read_only_deterministic_and_order_independent(self) -> None:
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.stat(D + "/z"), b.stat(D + "/a"), b.stat(D + "/m"))
        before = (dict(tracker.awaiting), set(tracker.problems), set(tracker.covered))
        first, second = tracker.report(), tracker.report()
        self.assertEqual(first, second)
        self.assertEqual(first["problems"], sorted(LEFTOVER + D + "/" + n for n in "zam"), "按文字排序，与记债顺序无关")
        self.assertEqual((dict(tracker.awaiting), set(tracker.problems), set(tracker.covered)), before, "report() 不改状态")

    def test_many_reads_of_one_name_keep_one_debt(self) -> None:
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), *[b.stat(D + "/p") for _ in range(500)])
        self.assertEqual(len(tracker.awaiting), 1)

    def test_every_distinct_name_is_accounted_for_without_a_cap(self) -> None:
        """没有容量上限：成百上千个不同名字的债都要如数清算（上限会让多出来的债被静默放过）。"""
        b = self.b
        names = [f"{D}/n{index:04d}" for index in range(300)]
        tracker, _ = tracker_of(b.mkdir(D), *[b.stat(name) for name in names])
        self.assertEqual(tracker.report()["problems"], sorted(LEFTOVER + name for name in names))
        tracker.observe_event(b.create(names[0])[0])
        self.assertEqual(len(tracker.report()["problems"]), 299, "销一笔少一笔")

    def test_replay_rejects_what_the_parser_honestly_reports(self) -> None:
        """解析器如实报出的清算问题，重放端必须拒绝：只看 tracker.problems 属性（立即问题）会漏掉它。"""
        b = self.b
        document = document_of(b.mkdir(D), b.stat(D + "/ghost"))
        self.assertEqual(document["outputs"]["problems"], [LEFTOVER + D + "/ghost"])
        with self.assertRaisesRegex(runtime.RuntimeContractError, "来源不明"):
            runtime._replay_events_v4(document, T.CONTRACT)
        b = self.b = T.Builder()
        path, groups = ledger_events(b)
        clean = document_of(*groups)
        tracker, _ = runtime._replay_events_v4(clean, T.CONTRACT)
        self.assertEqual(tracker.report()["problems"], [])


class ReorderedLinesEndToEndTests(AuditFixture):
    """strace 行 → 解析器 → 重放：两个 pid 交错复现 minute-ledger 形态。"""

    def ledger_lines(self, create: bool = True) -> list[str]:
        r = self.root
        ledger = f"{r}/sup/minute-ledger.ndjson"
        missing = f'1 newfstatat(AT_FDCWD</repo>, "{ledger}", 0x1, AT_SYMLINK_NOFOLLOW) = -1 ENOENT (No such file or directory)'
        stat = f'1 newfstatat(AT_FDCWD</repo>, "{ledger}", {{st_mode=S_IFREG|0644}}, 0) = 0'
        lines = [self.mkdir_line("sup")] + [missing] * 4 + [stat]
        if create:
            lines.append(f'2 openat(AT_FDCWD</repo>, "{ledger}", O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC, 0644) = 5<{ledger}>')
        lines += [stat, stat]
        if create:
            lines.append(f'2 unlinkat(AT_FDCWD</repo>, "{ledger}", 0) = 0')
        return lines

    def test_the_interleaved_ledger_is_accepted_end_to_end(self) -> None:
        document = self.document(self.ledger_lines())
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertAccepted(document)
        self.assertIsNotNone(runtime.replay_runtime(document, [self.entry]))

    def test_the_variant_without_a_creation_is_rejected(self) -> None:
        document = self.document(self.ledger_lines(create=False))
        ledger = f"{self.root}/sup/minute-ledger.ndjson"
        self.assertEqual(document["runtime"]["outputs"]["problems"], [LEFTOVER + ledger])
        self.assertRejected(document)

    def test_the_epoch_is_not_advanced_by_the_awaited_read(self) -> None:
        events = self.shape(self.document(self.ledger_lines()))
        ledger = "<R>/sup/minute-ledger.ndjson"
        stat = next(e for e in events if e["name"] == "newfstatat" and e["succeeded"])
        opened = [e for e in events if e["name"] == "openat" and e["path"] == ledger]
        # mkdir 改状态（0→1）；缺失探测与成功的 stat 都不改状态，纪元停在 1；openat 创建改状态（1→2），其返回描述符那条事件在纪元 2。
        self.assertEqual(stat["epoch"], 1, "待补证的读取不推进纪元")
        self.assertEqual([e["epoch"] for e in opened], [1, 2])

    def test_v3_contract_is_not_relaxed(self) -> None:
        """v3 是历史合同，行为逐字不变：同样的行在 v3 下仍立即判「首次读取前未证明」。"""
        document = self.document(self.ledger_lines(), contract="v3")
        self.assertTrue(any(LEFTOVER in text for text in document["runtime"]["outputs"]["problems"]))
        self.assertFalse(self.audit(document)["coverage_complete"])

    def consistent(self, document: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """换上事件列表，并像解析器一样重算每条事件的纪元与 outputs：只留下「待补证／销账」逻辑能拒绝它的余地。"""
        tampered = copy.deepcopy(document)
        tracker = runtime.OutputTrackerV4(self.contract)
        epoch = 0
        for event in events:
            event["epoch"] = epoch
            if tracker.observe_event(event):
                epoch += 1
        tampered["runtime"]["output_events"] = events
        tampered["runtime"]["outputs"] = tracker.report()
        return tampered

    def test_dropping_the_creation_event_from_a_good_document_is_rejected(self) -> None:
        document = self.document(self.ledger_lines())
        self.assertAccepted(document)
        events = [dict(e) for e in document["runtime"]["output_events"]
                  if not (e["kind"] == "write" and e["path"].endswith("minute-ledger.ndjson"))]
        tampered = self.consistent(document, events)
        self.assertEqual(tampered["runtime"]["outputs"]["problems"], [LEFTOVER + f"{self.root}/sup/minute-ledger.ndjson"])
        self.assertRejected(tampered)

    def test_dropping_every_successful_stat_from_a_good_document_is_rejected(self) -> None:
        """成功的 stat 全删：完整访问集合里仍有 stat 类访问，事件里却一条没有，路径／访问类型集合不闭合，被拒。
        （只删最早那一条是删不出矛盾的：它的债已被销账，后面还有同类事件，删掉对任何结论都没有影响。）"""
        document = self.document(self.ledger_lines())
        events = [dict(e) for e in document["runtime"]["output_events"] if not (e["name"] == "newfstatat" and e["succeeded"])]
        self.assertRejected(self.consistent(document, events))

    def test_forging_a_clean_outputs_block_is_rejected(self) -> None:
        document = self.document(self.ledger_lines(create=False))
        forged = copy.deepcopy(document)
        forged["runtime"]["outputs"]["problems"] = []
        self.assertRejected(forged)


class ReorderPropertyTests(unittest.TestCase):
    """随机合法历史 + 合法的「创建延后上报」重排。

    合法重排的模型：创建调用 i 在内核里先完成、却晚于其后若干条事件才被打印；这些事件只能是不改名字空间、且与创建所涉名字
    （含祖先／子孙）互不相干的，并以对同一个新名字的成功读取收尾。把创建调用的整组事件挪到那条读取之后即可。
    """

    @staticmethod
    def related(a: str, b: str) -> bool:
        return a == b or a.startswith(b + "/") or b.startswith(a + "/")

    @staticmethod
    def companion(previous: dict[str, Any], event: dict[str, Any]) -> bool:
        """真实解析器对一次 openat 产生两条紧挨着的同路径事件（请求路径＋返回描述符路径）；随机模拟器把它们写成两个调用编号，
        这里并回同一组，让「创建」是一个不可拆的整体。"""
        keys = ("name", "path", "kind", "flags", "target", "succeeded", "errno")
        return (event["name"] in runtime.OPEN_NAMES and event["kind"] == "write"
                and all(previous[key] == event[key] for key in keys))

    def groups_of(self, stream: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        groups: list[list[dict[str, Any]]] = []
        for event in stream:
            if groups and (groups[-1][0]["call"] == event["call"] or self.companion(groups[-1][-1], event)):
                groups[-1].append(event)
            else:
                groups.append([event])
        return groups

    def history(self, seed: int, steps: int = 220) -> list[dict[str, Any]]:
        helper = T.TrackerSimulationTests("test_random_valid_histories_match_the_real_filesystem_semantics")
        rng = random.Random(9100 + seed)
        fs, builder = T._SimFs(ROOT), T.Builder()
        statistics: collections.Counter = collections.Counter()
        stream: list[dict[str, Any]] = []
        for _ in range(steps):
            stream.extend(helper.apply_random_operation(rng, fs, builder, statistics))
        return stream

    def replay(self, groups: list[list[dict[str, Any]]]) -> runtime.OutputTrackerV4:
        tracker = runtime.OutputTrackerV4(T.CONTRACT)
        for group in groups:
            for event in group:
                tracker.observe_event(dict(event))
        return tracker

    @staticmethod
    def created_name(group: list[dict[str, Any]], known_before: set[str]) -> str | None:
        """这一组调用新建（或经 rename／link 带入）的那个名字；不是「新名字的创建」返回 None。"""
        first = group[0]
        if first["kind"] != "write" or not first["succeeded"] or first["name"] in {"unlink", "unlinkat", "rmdir"}:
            return None
        path = first["peer"] if first["role"] == "source" else first["path"]
        if first["role"] == "destination" or path in known_before or first["name"] in runtime.METADATA_WRITE_NAMES:
            return None
        return path

    def delayed_creation_cases(self, groups: list[list[dict[str, Any]]]) -> list[tuple[int, int]]:
        """(创建组下标 i, 对同名字的成功读取组下标 j)，满足合法重排的全部条件。"""
        reference = runtime.OutputTrackerV4(T.CONTRACT)
        known_before: list[set[str]] = []
        changed_flags: list[bool] = []
        for group in groups:
            known_before.append(set(reference.names))
            changed_flags.append(any([reference.observe_event(dict(event)) for event in group]))
        cases = []
        for i, group in enumerate(groups):
            path = self.created_name(group, known_before[i]) if changed_flags[i] else None
            if path is None:
                continue
            touched = {event["path"] for event in group} | {event["peer"] for event in group if event["peer"]}
            for j in range(i + 1, len(groups)):
                head = groups[j][0]
                if head["path"] == path and head["kind"] in {"read", "stat", "dir", "list"} and head["succeeded"]:
                    cases.append((i, j))
                    break
                if changed_flags[j] or any(self.related(event["path"], p) or (event["peer"] and self.related(event["peer"], p))
                                           for event in groups[j] for p in touched):
                    break
        return cases

    @staticmethod
    def creates(event: dict[str, Any], path: str) -> bool:
        """这条事件之后，名字 path 是否可能被（重新）绑定：同名创建，或 rename 的目的名字是 path 本身／它的祖先（目录连同子树带入）。"""
        if event["kind"] != "write" or not event["succeeded"]:
            return False
        if event["role"] is not None:
            return event["role"] == "destination" and (path == event["path"] or path.startswith(event["path"] + "/"))
        return (event["path"] == path and event["name"] not in {"unlink", "unlinkat", "rmdir"}
                and event["name"] not in runtime.METADATA_WRITE_NAMES)

    def test_delayed_creation_is_accepted_and_reaches_the_same_final_state(self) -> None:
        cases_total = 0
        for seed in range(80):
            groups = self.groups_of(self.history(seed))
            reference = self.replay(groups)
            self.assertEqual(reference.report()["problems"], [], f"seed={seed}：原顺序必须干净")
            cases = self.delayed_creation_cases(groups)
            for i, j in cases[:12]:
                cases_total += 1
                moved = groups[:i] + groups[i + 1:j + 1] + [groups[i]] + groups[j + 1:]
                tracker = self.replay(moved)
                context = f"seed={seed} i={i} j={j} {groups[i][0]['name']} {groups[i][0]['path'][len(ROOT):]}"
                self.assertEqual(tracker.report()["problems"], [], context)
                self.assertEqual(tracker.awaiting, {}, context)
                self.assertEqual(tracker.names.keys(), reference.names.keys(), context)
                self.assertEqual(tracker.entries, reference.entries, context)
        self.assertGreater(cases_total, 100, "随机历史必须真的产出足够多的合法重排，否则这个性质测试是空转")

    def test_a_missing_creation_is_always_reported(self) -> None:
        reported = 0
        for seed in range(80):
            groups = self.groups_of(self.history(seed))
            for i, j in self.delayed_creation_cases(groups)[:6]:
                first = groups[i][0]
                path = first["peer"] if first["role"] == "source" else first["path"]
                if any(self.creates(event, path) for group in groups[j + 1:] for event in group):
                    continue            # 之后同名字又被创建过：那次创建会合法销账，不属于「从未创建」
                dropped = groups[:i] + groups[i + 1:]
                problems = self.replay(dropped).report()["problems"]
                reported += 1
                self.assertIn(LEFTOVER + path, problems, f"seed={seed} i={i} j={j}")
        self.assertGreater(reported, 30)


# =====================================================================================================================
# R9：名字级去重
# =====================================================================================================================
#
# 10-15 ARM64：test_claude_upgrade_compare_accept 跑了 9963 秒，轨迹被截在 1,000,000 条（MAX_OUTPUT_EVENTS_V4），又丢 285,993 条，
# 判 output-event-limit。保留下来的 100 万条里 88% 是对私有盘内路径的重复成功 stat／读（stat/newfstatat 637,074；read/openat 251,618），
# 因为旧去重按全局变更纪元：同键事件在两次状态变更之间只留一条，而写／rename／unlink 合计约 5.5 万条把纪元切得很碎。
#
# 新去重（read_audit_runtime.OutputTrackerV4.dedup_key，解析器 _record_output_event 使用）：键 = 事件自身字段 + 它判定所依赖的几个
# 名字的状态（名字的版本号、是否经符号链接、写类事件的父目录是否目录、两路径失败调用两端的版本……），不含全局纪元。
# 安全性质——「同一个键的所有事件，跟踪器对它们的观察结果完全一致」——由 NameLevelKeyExactnessTests 逐条对拍；
# 「去重不改变判定」由 DedupVerdictTests 对拍（全量事件的最终报告＝只重放保留事件的最终报告）。


class SpyTracker(runtime.OutputTrackerV4):
    """在跟踪器上记录每条事件的「观察结果」，供键精确性对拍：是否改状态、产生了哪些问题（含重复文字）、待补证的增减、隐藏状态。"""

    def __init__(self, contract: dict[str, Any]) -> None:
        super().__init__(contract)
        self.log: list[str] = []

    def _problem(self, text: str, path: str) -> None:
        self.log.append(text + path)
        super()._problem(text, path)

    def outcome_of(self, event: dict[str, Any]) -> tuple:
        before = set(self.awaiting)
        self.log = []
        changed = self.observe_event(event)
        after = set(self.awaiting)
        return (changed, tuple(self.log), frozenset(after - before), frozenset(before - after), self._created_by_previous is not None)


def renumber(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """按组顺序给调用重新编号（组内共享，编号严格递增），展平成事件列表：重放端要求调用编号严格递增。
    一个调用最多两条事件（真实解析器的形状）：合并后超过两条的组按每两条拆成两个调用。"""
    events = []
    number = 0
    for group in groups:
        for start in range(0, len(group), 2):
            number += 1
            events.extend(dict(event, call=number) for event in group[start:start + 2])
    return events


def by_call(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """相邻且调用编号相同的事件归为一组（两路径调用的两端、openat 的两条事件）。"""
    groups: list[list[dict[str, Any]]] = []
    for event in events:
        if groups and groups[-1][0]["call"] == event["call"]:
            groups[-1].append(event)
        else:
            groups.append([event])
    return groups


def stress_groups(seed: int, steps: int = 180, stale: bool = True) -> list[list[dict[str, Any]]]:
    """随机合法历史，加上一次合法的「创建延后上报」（待补证）和两种重复：

    * ``stale=True``：把**任意更早**的调用原样再来一遍（同样的字段，名字的状态却可能已经不同——过期读取、重复创建、对已改名
      目录的探测……），再偶尔插入从未创建过的名字上的成功读取：流里有大量问题，用来检验「去重不吞问题」；
    * ``stale=False``：只重复**自上次改名字空间以来**出现过的读取类／失败类调用（状态没变，重复仍然合法），流是干净的：
      用来检验「干净的流去重后仍能走完整重放」。"""
    helper = ReorderPropertyTests("test_a_missing_creation_is_always_reported")
    rng = random.Random(31000 + seed)
    groups = helper.groups_of(helper.history(seed + 500, steps))
    cases = helper.delayed_creation_cases(groups)
    if cases and rng.random() < 0.8:
        i, j = rng.choice(cases)
        groups = groups[:i] + groups[i + 1:j + 1] + [groups[i]] + groups[j + 1:]
    builder = T.Builder()
    result: list[list[dict[str, Any]]] = []
    reference = runtime.OutputTrackerV4(T.CONTRACT)
    recent: list[list[dict[str, Any]]] = []          # 自上次改名字空间以来的调用
    for group in groups:
        result.append(group)
        changed = any([reference.observe_event(dict(event)) for event in group])
        if changed:
            recent = []
        elif not (group[0]["name"] in runtime.OPEN_NAMES and group[0]["kind"] == "write"):
            recent.append(group)
        if rng.random() < 0.3:
            pool = result if stale else recent
            for _ in range(rng.randint(1, 3) if pool else 0):
                result.append(copy.deepcopy(rng.choice(pool)))
        if stale and rng.random() < 0.03:
            ghost = ROOT + "/" + rng.choice(["d1", "d2", "d3"]) + "/ghost" + str(rng.randrange(3))
            result.append([builder.event("openat", ghost, kind="read", flags=("O_RDONLY",), target=ghost)])
    return result


class NameLevelKeyExactnessTests(unittest.TestCase):
    """键相同 ⇒ 跟踪器对事件的观察结果相同（去重键的精确性）；名字级版本只因「这个名字自己变了」而变。"""

    def setUp(self) -> None:
        self.b = T.Builder()

    # ------------------------------------------------------------------ 随机对拍

    def test_events_with_equal_keys_always_have_equal_outcomes(self) -> None:
        repeats = 0
        outcomes_seen: collections.Counter = collections.Counter()
        for seed in range(80):
            tracker = SpyTracker(T.CONTRACT)
            by_key: dict[tuple, tuple] = {}
            for event in renumber(stress_groups(seed)):
                key = tracker.dedup_key(dict(event))
                outcome = tracker.outcome_of(dict(event))
                outcomes_seen[(outcome[0], bool(outcome[1]), bool(outcome[2]), bool(outcome[3]))] += 1
                if key is None:
                    continue
                if key in by_key:
                    repeats += 1
                    if by_key[key] != outcome:
                        self.fail(f"seed={seed}：键相同、观察结果不同\n事件 {event}\n先前 {by_key[key]}\n现在 {outcome}")
                else:
                    by_key[key] = outcome
        self.assertGreater(repeats, 3000, "随机流必须真的产生大量同键事件，否则这个性质测试是空转")
        # 观察结果的各种形态都要出现过：改状态、产生问题、记待补证、销账。
        self.assertGreater(outcomes_seen[(True, False, False, False)], 500, outcomes_seen)
        self.assertGreater(sum(count for (_c, problem, _a, _r), count in outcomes_seen.items() if problem), 100, outcomes_seen)
        self.assertGreater(sum(count for (_c, _p, added, _r), count in outcomes_seen.items() if added), 20, outcomes_seen)
        self.assertGreater(sum(count for (_c, _p, _a, removed), count in outcomes_seen.items() if removed), 10, outcomes_seen)

    # ------------------------------------------------------------------ 名字级：什么变、什么不变

    @staticmethod
    def key_after(*setup: list[dict[str, Any]], event: dict[str, Any]) -> tuple | None:
        return tracker_of(*setup)[0].dedup_key(dict(event))

    def test_key_ignores_unrelated_mutations(self) -> None:
        """对别的名字的写、rename、unlink、mkdir、chmod，以及同目录下子项个数的变化，都不改变这个名字上读取事件的键。"""
        b, a = self.b, D + "/a"
        probe = b.stat(a)[0]
        base = [b.mkdir(D), b.create(a)]
        baseline = self.key_after(*base, event=probe)
        unrelated = [b.create(D + "/q"), b.mkdir(D + "/sub"), b.create(D + "/sub/x"), b.create(D + "/y"), b.rename(D + "/y", D + "/z"),
                     b.link(D + "/q", D + "/q2"), b.unlink(D + "/q2"), [b.event("fchmodat", D + "/q")], b.unlink(D + "/z"),
                     b.rename(D + "/sub", D + "/sub2"), b.probe(D + "/nothing"), b.stat(D + "/q")]
        for index in range(len(unrelated)):
            with self.subTest(after=index):
                self.assertEqual(self.key_after(*base, *unrelated[:index + 1], event=probe), baseline)

    def test_key_changes_when_the_name_itself_changes(self) -> None:
        b, a = self.b, D + "/a"
        probe = b.stat(a)[0]
        base = [b.mkdir(D), b.create(a)]
        baseline = self.key_after(*base, event=probe)
        changes = {
            "改名走再改回": [b.rename(a, D + "/m"), b.rename(D + "/m", a)],
            "删除后重建": [b.unlink(a), b.create(a)],
            "被删除": [b.unlink(a)],
            "被别的名字覆盖": [b.create(D + "/o"), b.rename(D + "/o", a)],
        }
        for label, steps in changes.items():
            with self.subTest(change=label):
                self.assertNotEqual(self.key_after(*base, *steps, event=probe), baseline, label)

    def test_key_changes_when_an_ancestor_directory_is_moved_or_replaced(self) -> None:
        """目录改名：它名下整棵子树里每个名字的版本都变（来源端消失、目的端出现），不需要另记子树失效。"""
        b = self.b
        deep = D + "/s/t/x"
        base = [b.mkdir(D), b.mkdir(D + "/s"), b.mkdir(D + "/s/t"), b.create(deep)]
        probe_old = b.stat(deep)[0]
        before_old = self.key_after(*base, event=probe_old)
        moved = [b.rename(D + "/s", D + "/n")]
        self.assertNotEqual(self.key_after(*base, *moved, event=probe_old), before_old, "旧名字（现在已不存在）的键必须变")
        probe_new = b.stat(D + "/n/t/x")[0]
        self.assertNotEqual(self.key_after(*base, *moved, event=probe_new), self.key_after(*base, event=probe_new), "新名字（带入）的键必须变")
        # 目录被换成符号链接：子孙路径变成「经链接」，键里的这一位随之变化。
        replaced = [b.rename(D + "/s", D + "/n"), b.symlink(D + "/s", "n")]
        self.assertNotEqual(self.key_after(*base, *replaced, event=probe_old), self.key_after(*base, *moved, event=probe_old))

    def test_write_event_key_includes_the_parent_directory_state(self) -> None:
        b = self.b
        event = b.event("openat", D + "/d/f", flags=("O_WRONLY",), target=D + "/d/f")     # 无 O_CREAT 的写打开：未知名字上判问题
        base = [b.mkdir(D)]
        self.assertNotEqual(self.key_after(*base, event=event), self.key_after(*base, b.mkdir(D + "/d"), event=event))

    def test_two_path_pair_shares_one_normalised_state(self) -> None:
        """一次失败的两路径调用，来源端与目的端事件的键由同一份归一化状态生成：要么一起去重，要么一起保留。"""
        b = self.b
        base = [b.mkdir(D), b.create(D + "/a"), b.create(D + "/b")]
        tracker = tracker_of(*base)[0]
        pair = b.link(D + "/a", D + "/b", ok=False, errno="EEXIST")
        keys = [tracker.dedup_key(dict(event)) for event in pair]
        self.assertTrue(all(key is not None for key in keys))
        self.assertEqual(keys[0][9:], keys[1][9:], "归一化的状态部分相同")
        self.assertNotEqual(keys[0][:9], keys[1][:9], "事件自身字段（路径、角色、对端）当然不同")
        other = tracker_of(*base, b.unlink(D + "/b"))[0]
        self.assertNotEqual(other.dedup_key(dict(pair[0])), keys[0])
        self.assertNotEqual(other.dedup_key(dict(pair[1])), keys[1])

    def test_events_that_must_always_be_kept_have_no_key(self) -> None:
        b = self.b
        tracker = tracker_of(b.mkdir(D), b.create(D + "/a"))[0]
        for label, event in {
            "成功的 rename 来源端": b.rename(D + "/a", D + "/b")[0], "成功的 rename 目的端": b.rename(D + "/a", D + "/b")[1],
            "成功的 link": b.link(D + "/a", D + "/b")[0], "成功的 mkdir": b.mkdir(D + "/x")[0],
            "成功的 unlink": b.unlink(D + "/a")[0], "成功的 rmdir": b.unlink(D + "/x", name="rmdir")[0],
            "成功的 symlink": b.symlink(D + "/s", "x")[0],
            "另一端在盘外的两路径调用": b.pair("link", D + "/a", "/etc/passwd", ok=False, errno="EEXIST")[0],
        }.items():
            with self.subTest(event=label):
                self.assertIsNone(tracker.dedup_key(dict(event)))
        for label, event in {"stat": b.stat(D + "/a")[0], "读": b.read(D + "/a")[0], "缺失探测": b.probe(D + "/zz")[0],
                             "失败的 mkdir": b.mkdir(D + "/a", ok=False, errno="EEXIST")[0],
                             "已知名字上的写打开": b.create(D + "/a", extra=("O_TRUNC",))[0]}.items():
            with self.subTest(event=label):
                self.assertIsNotNone(tracker.dedup_key(dict(event)))

    def test_every_field_of_the_event_is_part_of_the_key(self) -> None:
        """事件自身的每个字段都进键：只改一个字段，键就必须变（否则两条判定可能不同的事件会被互相去重）。"""
        b = self.b
        tracker = tracker_of(b.mkdir(D), b.create(D + "/a"), b.create(D + "/c"))[0]
        base = b.stat(D + "/a")[0]
        variants = {
            "path": dict(base, path=D + "/c"), "kind": dict(base, kind="read"), "name": dict(base, name="statx"),
            "flags": dict(base, flags=["AT_SYMLINK_NOFOLLOW"]), "target": dict(base, target=D + "/a"),
            "succeeded（无错误码）": dict(base, succeeded=False),
        }
        baseline = tracker.dedup_key(dict(base))
        for label, variant in variants.items():
            with self.subTest(field=label):
                self.assertNotEqual(tracker.dedup_key(dict(variant)), baseline)
        failed = b.probe(D + "/zz", "ENOENT")[0]
        self.assertNotEqual(tracker.dedup_key(dict(failed)), tracker.dedup_key(dict(failed, errno="ENOTDIR")))
        pair = b.link(D + "/a", D + "/c", ok=False, errno="EEXIST")
        self.assertNotEqual(tracker.dedup_key(dict(pair[0])), tracker.dedup_key(dict(pair[1])), "角色与对端不同")
        self.assertNotEqual(tracker.dedup_key(dict(pair[0])), tracker.dedup_key(dict(pair[0], errno="ENOENT")))

    def test_exclusive_create_key_separates_the_companion_from_a_later_contradiction(self) -> None:
        """O_CREAT|O_EXCL 成功落在已知名字上：紧跟创建的那条（同一次调用的第二条事件）合法，之后再来一条就是矛盾——
        两者字段与名字版本都相同，只有「是不是刚被上一条事件创建」这一位不同，所以键不同、不会被互相去重。"""
        b = self.b
        tracker = tracker_of(b.mkdir(D))[0]
        creation = b.create(D + "/a", extra=("O_EXCL",))[0]
        tracker.observe_event(dict(creation))
        self.assertTrue(tracker.consumes_hidden_state())
        companion_key = tracker.dedup_key(dict(creation))
        tracker.observe_event(dict(creation))                         # 紧邻的第二条：豁免，不是问题
        self.assertEqual(tracker.report()["problems"], [])
        self.assertFalse(tracker.consumes_hidden_state())
        self.assertNotEqual(companion_key, tracker.dedup_key(dict(creation)))

    def test_versions_move_only_for_the_names_an_operation_touches(self) -> None:
        b = self.b
        tracker, _ = tracker_of(b.mkdir(D), b.mkdir(D + "/s"), b.create(D + "/s/x"), b.create(D + "/f"))

        def moved_names(event: dict[str, Any]) -> set[str]:
            snapshot = dict(tracker.versions)
            tracker.observe_event(event)
            return {path for path, version in tracker.versions.items() if snapshot.get(path) != version}

        self.assertEqual(moved_names(b.rename(D + "/s", D + "/n")[0]), {D + "/s", D + "/s/x", D + "/n", D + "/n/x"},
                         "目录改名：来源端与目的端的整棵子树，且只有它们")
        self.assertEqual(moved_names(b.link(D + "/f", D + "/g")[0]), {D + "/g"})
        self.assertEqual(moved_names(b.unlink(D + "/g")[0]), {D + "/g"})
        self.assertEqual(moved_names(b.stat(D + "/late")[0]), {D + "/late"}, "待补证的记账算该名字自己的变化")
        self.assertEqual(moved_names(b.create(D + "/late")[0]), {D + "/late"}, "销账也是")

    def test_a_names_versions_never_repeat(self) -> None:
        """同一个名字的两个时刻绝不同值：删除后重新创建成同样的对象、改走再改回，版本号都是新的。"""
        b, a = self.b, D + "/a"
        tracker = tracker_of(b.mkdir(D))[0]
        history: dict[str, list[int]] = collections.defaultdict(list)
        steps = [b.create(a), b.unlink(a), b.create(a), b.rename(a, D + "/m"), b.rename(D + "/m", a), b.stat(D + "/late"),
                 b.create(D + "/late"), b.unlink(D + "/late"), b.stat(D + "/late"), b.create(D + "/late")]
        for step in steps:
            for event in step:
                tracker.observe_event(dict(event))
            for path, version in tracker.versions.items():
                if not history[path] or history[path][-1] != version:
                    history[path].append(version)
        for path, values in history.items():
            self.assertEqual(values, sorted(set(values)), f"{path}：版本号严格递增、不重复")
        self.assertGreaterEqual(len(history[a]), 5)
        self.assertGreaterEqual(len(history[D + "/late"]), 5)


class DedupHarness(AuditFixture):
    """把事件流喂进真实解析器的 ``_record_output_event``（去重发生的地方）；没有 test_ 方法。"""

    def translate(self, event: dict[str, Any]) -> dict[str, Any]:
        result = dict(event)
        for key in ("path", "peer", "target"):
            if isinstance(result[key], str):
                result[key] = result[key].replace(ROOT, self.root)
        return result

    def feed(self, stream: list[dict[str, Any]]) -> ra.TraceParser:
        parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v4")
        for event in stream:
            parser.call_id, parser.call_errno = event["call"], event["errno"]
            parser.call_end = (event["role"], event["peer"]) if event["role"] else None
            parser.call_flags = frozenset(flag for flag in event["flags"] if flag.startswith("RENAME_"))
            parser._record_output_event(event["path"], event["kind"], event["name"], event["succeeded"],
                                        {flag for flag in event["flags"] if not flag.startswith("RENAME_")}, event["target"])
        return parser

    def reference(self, stream: list[dict[str, Any]]) -> runtime.OutputTrackerV4:
        tracker = runtime.OutputTrackerV4(self.contract)
        for event in stream:
            tracker.observe_event(dict(event))
        return tracker


class DedupVerdictTests(DedupHarness):
    """去重不改变判定：全量事件的最终报告 ＝ 只重放保留事件的最终报告 ＝ 解析器自己的报告；干净的流经完整重放通过。"""

    def test_deduplicated_streams_have_the_same_verdict_as_the_full_streams(self) -> None:
        dropped = clean = bad = awaiting_streams = 0
        for seed in range(80):
            stream = [self.translate(event) for event in renumber(stress_groups(seed, stale=seed % 2 == 0))]
            reference = self.reference(stream)
            parser = self.feed(stream)
            kept = parser.output_tracker
            context = f"seed={seed}"
            self.assertEqual(kept.report(), reference.report(), context)
            self.assertEqual((kept.names, kept.entries, kept.awaiting, kept.link_text),
                             (reference.names, reference.entries, reference.awaiting, reference.link_text), context)
            dropped += len(stream) - len(parser.output_events)
            only_kept = self.reference(parser.output_events)
            self.assertEqual(only_kept.report(), reference.report(), context + "：只重放保留事件得到不同的结论")
            self.assertEqual((only_kept.names, only_kept.awaiting), (reference.names, reference.awaiting), context)
            if reference.awaiting:
                awaiting_streams += 1
            runtime_part = {"output_events": parser.output_events, "outputs": kept.report()}
            if reference.report()["problems"]:
                bad += 1
                with self.assertRaises(runtime.RuntimeContractError, msg=context):
                    runtime._replay_events_v4(runtime_part, self.contract)
            else:
                clean += 1
                replayed, _seen = runtime._replay_events_v4(runtime_part, self.contract)
                self.assertEqual(replayed.names, reference.names, context)
        self.assertGreater(dropped, 5000, "必须真的丢掉了大量重复事件，否则对拍是空转")
        self.assertGreater(bad, 10, "必须有带问题的流，才能证明去重不会吞掉问题")
        self.assertGreater(clean, 5, "必须有干净的流，才能走完整重放")
        self.assertGreater(awaiting_streams, 10, "必须有留下待补证的流，才能证明待补证状态也不会被去重吞掉")

    def test_the_event_right_after_a_creation_is_always_kept(self) -> None:
        """紧跟在文件创建之后的事件会消耗隐藏状态 ``_created_by_previous``。它即便与更早的某条事件同键也必须保留，否则重放端
        （看不到它）的隐藏状态比解析端多留一条事件，之后的 O_CREAT|O_EXCL 矛盾会被重放端误放行。"""
        b = T.Builder()
        stream = [self.translate(event) for event in
                  b.mkdir(D) + b.create(D + "/q") + b.stat(D + "/q") + b.create(D + "/p", extra=("O_EXCL",))[:1] +
                  b.stat(D + "/q") + b.create(D + "/p", extra=("O_EXCL",))[:1]]
        reference = self.reference(stream)
        self.assertEqual(len(reference.report()["problems"]), 1, "第二次 O_EXCL 创建落在已知名字上：解析端判矛盾")
        parser = self.feed(stream)
        self.assertEqual(parser.output_tracker.report(), reference.report())
        self.assertEqual(self.reference(parser.output_events).report(), reference.report(), "只重放保留事件，结论必须相同")
        self.assertEqual(len([e for e in parser.output_events if e["kind"] == "stat"]), 2, "紧跟创建的那条 stat 虽与前一条同键，仍被保留")

    def test_a_narrowed_key_is_caught_by_this_comparison(self) -> None:
        """对拍确实有牙：把键人为收窄（不看名字版本和是否经链接），同一份随机流里立刻出现「保留事件的结论≠全量事件的结论」。"""
        # 解析器用的是 read_audit 自己按路径加载的那份跟踪器类（与本测试导入的不是同一个对象），补丁要打在它上面。
        tracker_class = ra._runtime_module().OutputTrackerV4
        original = tracker_class.dedup_key

        def narrowed(self: Any, event: dict[str, Any]) -> tuple | None:
            key = original(self, event)
            return None if key is None else key[:9] + key[11:]          # 去掉 (版本号, 是否经链接) 两位

        differing = 0
        with mock.patch.object(tracker_class, "dedup_key", narrowed):
            for seed in range(30):
                stream = [self.translate(event) for event in renumber(stress_groups(seed))]
                parser = self.feed(stream)
                if self.reference(parser.output_events).report() != self.reference(stream).report():
                    differing += 1
        self.assertGreater(differing, 3, "收窄后的键必须在随机流上暴露出差异")


class NameLevelDedupEndToEndTests(AuditFixture):
    """strace 行 → 解析器：名字级去重的可见效果，以及它保持的边界。"""

    def retained(self, lines: list[str]) -> list[tuple[str, str, int]]:
        document = self.document(lines)
        self.assertAccepted(document)
        return [(e["name"], e["path"].replace(self.root, "<R>"), e["epoch"]) for e in document["runtime"]["output_events"]]

    def test_repeated_reads_survive_unrelated_changes_as_one_event(self) -> None:
        """对别的名字的写不使这个名字上的重复 stat 失效：旧去重每个纪元留一条（这里 50 条），名字级只留 1 条。"""
        r = self.root
        stat_a = f'1 newfstatat(AT_FDCWD</repo>, "{r}/d/a", {{st_mode=S_IFREG|0644}}, 0) = 0'
        lines = self.prelude()
        builder = T.Builder()
        events = [builder.event("mkdirat", f"{ROOT}/d")] + builder.create(f"{ROOT}/d/a") + builder.create(f"{ROOT}/d/a")
        for index in range(50):
            lines += [stat_a, self.create_line(f"d/b{index}")]
            events += [builder.event("newfstatat", f"{ROOT}/d/a", kind="stat")] + builder.create(f"{ROOT}/d/b{index}") + builder.create(f"{ROOT}/d/b{index}")
        kept = self.retained(lines)
        self.assertEqual(len([item for item in kept if item[0] == "newfstatat"]), 1)
        legacy = [event for event in legacy_retained_events(events) if event["name"] == "newfstatat"]
        self.assertEqual(len(legacy), 50, "旧规则（按全局纪元）在同样的流里每个纪元保留一条")

    def test_a_directory_rename_re_keeps_reads_under_old_and_new_names(self) -> None:
        r = self.root
        deep = f"{r}/d/s/t/x"

        def stat(path: str) -> str:
            return f'1 newfstatat(AT_FDCWD</repo>, "{path}", {{st_mode=S_IFREG|0644}}, 0) = 0'

        def miss(path: str) -> str:
            return f'1 newfstatat(AT_FDCWD</repo>, "{path}", 0x1, 0) = -1 ENOENT (No such file or directory)'

        lines = self.prelude() + [f'1 mkdir("{r}/d/s", 0700) = 0', f'1 mkdir("{r}/d/s/t", 0700) = 0', self.create_line("d/s/t/x"),
                                  stat(deep), stat(deep), miss(f"{r}/d/n/t/x"), miss(f"{r}/d/n/t/x"),
                                  f'1 rename("{r}/d/s", "{r}/d/n") = 0',
                                  stat(f"{r}/d/n/t/x"), stat(f"{r}/d/n/t/x"), miss(deep), miss(deep)]
        kept = self.retained(lines)
        self.assertEqual(len([k for k in kept if k[0] == "newfstatat" and k[1] == "<R>/d/s/t/x"]), 2, "改名前的 stat 一条＋改名后的缺失探测一条")
        self.assertEqual(len([k for k in kept if k[0] == "newfstatat" and k[1] == "<R>/d/n/t/x"]), 2, "改名前的缺失探测一条＋改名后的 stat 一条")

    def test_the_epoch_field_is_unchanged_and_replay_still_recomputes_it(self) -> None:
        document = self.document(self.prelude() + [self.read_line("d/a")] * 3 + [self.create_line("d/b")])
        epochs = [e["epoch"] for e in document["runtime"]["output_events"]]
        self.assertEqual(epochs, sorted(epochs))
        tampered = copy.deepcopy(document)
        tampered["runtime"]["output_events"][-1]["epoch"] += 1
        self.assertRejected(tampered)


# =====================================================================================================================
# R9：与 compare_accept 统计形态相近的合成负载——去重到底去掉多少
# =====================================================================================================================


class CompareAcceptShape:
    """合成事件流生成器，配比取自 10-15 ARM64 compare_accept 前 100 万条保留事件的 (kind, name) 计数（见上），
    按 scale 缩放；只生成合法历史（跟踪器零问题）。读取目标按 skew 取热点（0 为均匀，越大越集中）。"""

    REAL = {"stat": 637074, "read": 251618, "missing": 23667, "create_events": 19326, "list": 18324, "dir": 18266, "mkdir": 9354,
            "chmod": 6520, "rename_events": 6458, "unlink": 6411, "link_events": 1422}

    def __init__(self, seed: int, scale: float, skew: float) -> None:
        self.rng, self.b, self.skew = random.Random(seed), T.Builder(), skew
        self.dirs, self.files = [ROOT + "/base"], []
        self.budget = {key: int(value * scale) for key, value in self.REAL.items()}
        self.events: list[dict[str, Any]] = list(self.b.mkdir(ROOT + "/base"))
        self.counter = 0

    def fresh(self, parent: str) -> str:
        self.counter += 1
        return f"{parent}/n{self.counter}"

    def pick(self, items: list[str]) -> str:
        if self.skew <= 0:
            return self.rng.choice(items)
        return items[min(int(len(items) * (self.rng.random() ** (1 + self.skew))), len(items) - 1)]

    def write_step(self) -> bool:
        rng, b, budget = self.rng, self.b, self.budget
        options = [("mkdir", budget["mkdir"]), ("create", budget["create_events"] // 3), ("chmod", budget["chmod"]),
                   ("rename", budget["rename_events"] // 2), ("unlink", budget["unlink"]), ("link", budget["link_events"] // 2)]
        if not self.files:
            options = [option for option in options if option[0] in {"mkdir", "create"}]
        weights = [max(weight, 0) for _name, weight in options]
        if not sum(weights):
            return False
        kind = rng.choices([name for name, _w in options], weights)[0]
        if kind == "mkdir":
            path = self.fresh(self.pick(self.dirs))
            self.events += b.mkdir(path)
            self.dirs.append(path)
            budget["mkdir"] -= 1
        elif kind == "create":
            path = self.fresh(self.pick(self.dirs))
            self.events += b.create(path) + b.create(path)
            self.files.append(path)
            budget["create_events"] -= 3
        elif kind == "chmod":
            self.events.append(b.event("fchmodat", self.pick(self.files)))
            budget["chmod"] -= 1
        elif kind == "unlink":
            path = self.files.pop(rng.randrange(len(self.files)))
            self.events += b.unlink(path, name="unlinkat")
            budget["unlink"] -= 1
        elif kind == "rename":
            source = self.files.pop(rng.randrange(len(self.files)))
            destination = self.fresh(self.pick(self.dirs))
            self.events += b.rename(source, destination, name="renameat")
            self.files.append(destination)
            budget["rename_events"] -= 2
        else:
            source = self.pick(self.files)
            destination = self.fresh(self.pick(self.dirs))
            self.events += b.link(source, destination, name="linkat")
            self.files.append(destination)
            budget["link_events"] -= 2
        return True

    def read_step(self) -> bool:
        rng, b, budget = self.rng, self.b, self.budget
        options = [(name, budget[name]) for name in ("stat", "read", "missing", "list", "dir")]
        weights = [max(weight, 0) for _name, weight in options]
        if not sum(weights) or not self.files:
            return False
        kind = rng.choices([name for name, _w in options], weights)[0]
        if kind == "stat":
            path = self.pick(self.files if rng.random() < 0.8 else self.dirs)
            flags = ("AT_SYMLINK_NOFOLLOW",) if rng.random() < 0.5 else ()
            self.events.append(b.event("newfstatat", path, kind="stat", flags=flags))
        elif kind == "read":
            path = self.pick(self.files)
            self.events += b.read(path) + b.read(path)
        elif kind == "missing":
            self.events += b.probe(f"{self.pick(self.dirs)}/absent{rng.randrange(10 ** 9)}")
        elif kind == "list":
            self.events.append(b.event("getdents64", self.pick(self.dirs), kind="list"))
        else:
            path = self.pick(self.dirs)
            self.events.append(b.event("openat", path, kind="dir", flags=("O_DIRECTORY", "O_RDONLY"), target=path))
        budget[kind] -= 1
        return True

    def generate(self) -> list[dict[str, Any]]:
        writes = (sum(self.budget[k] for k in ("mkdir", "unlink", "chmod")) + self.budget["create_events"] // 3
                  + self.budget["rename_events"] // 2 + self.budget["link_events"] // 2)
        reads = sum(self.budget[k] for k in ("stat", "read", "missing", "list", "dir"))
        probability = writes / max(writes + reads, 1)
        while True:
            progressed = self.write_step() if self.rng.random() < probability else self.read_step()
            if not progressed and not (self.write_step() or self.read_step()):
                break
        return self.events


def legacy_retained_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """基线规则（51a7a93f4 的 TraceParser._record_output_event）：事件没改状态且同键事件在当前全局纪元已出现过就丢；纪元一推进键集清空。"""
    tracker = runtime.OutputTrackerV4(T.CONTRACT)
    seen: set = set()
    epoch = 0
    kept = []
    for event in events:
        event = dict(event, epoch=epoch)
        if tracker.observe_event(event):
            epoch += 1
            seen.clear()
        else:
            key = (event["path"], event["kind"], event["name"], event["succeeded"], event["errno"], tuple(event["flags"]),
                   event["target"], event["role"], event["peer"], epoch)
            if key in seen:
                continue
            seen.add(key)
        kept.append(event)
    return kept


class CompareAcceptShapedReductionTests(DedupHarness):
    """合成负载：同一份事件流，旧规则、新规则各保留多少；新规则保留的流经完整重放通过。"""

    SCALE = 0.05

    def test_the_shaped_stream_loses_an_order_of_magnitude(self) -> None:
        for skew in (0.0, 1.0, 3.0):
            with self.subTest(skew=skew):
                events = CompareAcceptShape(7, self.SCALE, skew).generate()
                counts = collections.Counter(event["kind"] for event in events)
                reads = counts["stat"] + counts["read"] + counts["missing"] + counts["list"] + counts["dir"]
                self.assertGreater(reads / len(events), 0.9, counts)          # 配比对得上真实形态：读取类事件占绝大多数
                raw = [self.translate(event) for event in renumber(by_call(events))]
                parser = self.feed(raw)
                self.assertEqual(parser.output_tracker.report()["problems"], [], "合成历史必须合法")
                legacy = len(legacy_retained_events(events))
                new = len(parser.output_events)
                # 实测（3 个种子 × 3 种热点分布，比例 0.05／0.2）：去重前后相差 10.0～11.3 倍，相对旧规则少 4.8～7.2 倍；留一点余量。
                self.assertGreaterEqual(len(raw) / new, 9.5, (len(raw), legacy, new))
                self.assertGreaterEqual(legacy / new, 4.0, (len(raw), legacy, new))
                replayed, _seen = runtime._replay_events_v4({"output_events": parser.output_events,
                                                             "outputs": parser.output_tracker.report()}, self.contract)
                self.assertEqual(replayed.names, parser.output_tracker.names)


if __name__ == "__main__":
    unittest.main()
