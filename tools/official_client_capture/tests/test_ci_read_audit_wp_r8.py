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

R9 部分见本文件后半（名字级去重）。
"""

from __future__ import annotations

import collections
import copy
import random
import unittest
from typing import Any

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


if __name__ == "__main__":
    unittest.main()
