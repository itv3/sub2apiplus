"""WP-R11（审计解析器加固第十一轮）：已生成对象的「缺失探测」被 strace 多进程乱序记在创建事件之后，不再误判为轨迹矛盾。

背景（10-16 ARM64 收割 #9，``test_codex_upgrade_staging_dispatch`` 26 项全过，输出归属重放却失败）
==============================================================================================

跟踪器报告里只有一条问题：``已生成对象的缺失探测与轨迹矛盾：…/watchdog-heartbeats.ndjson``。该名字的 v4 事件序列：

    call 1100945  newfstatat(AT_SYMLINK_NOFOLLOW) → ENOENT      名字尚未生成（正常）
    call 1101716  newfstatat                      → ENOENT      名字尚未生成（正常，创建者自己的「先检查」）
    call 1101718  openat(O_APPEND|O_CREAT|…)      成功（两条事件） 创建
    call 1101719  newfstatat(AT_SYMLINK_NOFOLLOW) → ENOENT      ← 创建之后、调用号只差 1 的一次「文件不存在」
    call 1101882  newfstatat                      成功

最后一条 ENOENT 来自另一个进程（监督器 monitor）：它在内核里先于创建完成就拿到了 ENOENT，只是 strace -f 按各进程「系统调用返回被
tracer 处理的先后」打印，它的行排在创建行之后。同一文件名模式在另外三个 run 目录各出现一次，都是「探测在前、创建在后」，没出问题。

修法（``read_audit_runtime.OutputTrackerV4``，完整论证见该类说明的 WP-R11 段）：已知名字上的缺失探测，在两个条件**同时**成立时按乱序放过
（不判问题、不改名字表）：

* 窗口：探测调用编号 − 名字最近一次被绑定的调用编号 ∈ ``[0, LATE_MISSING_PROBE_WINDOW_CALLS]``；
* 先行探测证据：名字被绑定之前，轨迹里已经出现过对它的缺失探测（``probed_missing``）。

为什么要第二个条件（只靠调用编号窗口分不开乱序与真矛盾）：既有测试里「创建 → 紧接着同名 ENOENT 必须判矛盾」的夹具，调用编号差也是 1~3
（``test_ci_read_audit_v4_tracker`` 的失败判定表与两路径失败规则、``test_ci_read_audit_wp_r2`` 的 connect 矛盾用例），单靠调用编号窗口分不开
它们与真实的乱序；这些夹具的名字都从未被探测过缺失，而真实样本里创建者自己先探测过（轮询／检查后创建）。

本模块：

* ``RealSliceTests``：把收割 #9 的真实事件切片（32 条，真实调用编号）喂给跟踪器，修复后零问题；把窗口调成「永不放过」就精确复现
  那一条报告；名字表不变；整段能走完整重放；
* ``WindowRuleTests``：窗口边界、证据门槛、证据只属于同名且绑定时消耗、各种绑定方式（创建／硬链接目的／rename 带入／目录 rename 子树）、
  删除后合法缺失、真实矛盾照旧、调用编号缺失或类型不对时退化、其它矛盾类别不放宽、v3 跟踪器不变；
* ``TwoPathWindowTests``：rename／link 失败的「来源缺失却已知」同口径；
* ``ReportAndSchemaTests``：``report()`` 与事件键集合不变；
* ``EndToEndTests``：strace 行（多 pid 交错）→ 解析器 → 重放整条链；v3 不放宽；删掉先行探测事件的篡改被重放拒绝；
* ``KeyAndDedupTests``：去重键新增的一位、窗口外的同键探测不被丢、随机对拍（同键同结论、去重不改变判定、收窄键立刻变红）。
"""

from __future__ import annotations

import copy
import random
import unittest
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests import test_ci_read_audit_v4_tracker as T
from tools.official_client_capture.tests import test_ci_read_audit_wp_r8 as R8
from tools.official_client_capture.tests.test_ci_read_audit_v4_trace import AuditFixture

D = T.D
ROOT = T.ROOT
W = runtime.LATE_MISSING_PROBE_WINDOW_CALLS
CONTRADICTION = "已生成对象的缺失探测与轨迹矛盾："


# ---------------------------------------------------------------------------------------------------------------------
# 事件拼装：按指定调用编号造事件（T.Builder 的 probe／create 不暴露调用编号）
# ---------------------------------------------------------------------------------------------------------------------


def stamp(events: Any, call: Any) -> Any:
    """把调用编号原样盖到事件上（可以是 None、字符串等非法值：``T.Builder.event`` 遇到 None 会自动编号，所以事后覆盖）。"""
    for event in ([events] if isinstance(events, dict) else events):
        event["call"] = call
    return events


def probe_at(b: T.Builder, path: str, call: Any, *, errno: str = "ENOENT", name: str = "newfstatat",
             flags: tuple[str, ...] = ()) -> dict[str, Any]:
    """一次缺失探测（失败的 stat／open，ENOENT／ENOTDIR），调用编号由调用者指定。"""
    return stamp(b.event(name, path, kind="missing", ok=False, errno=errno, flags=flags), call)


def create_at(b: T.Builder, path: str, call: Any, *, extra: tuple[str, ...] = ("O_APPEND", "O_CLOEXEC")) -> list[dict[str, Any]]:
    """O_CREAT 创建：像真实解析器那样产出共享调用编号的两条事件（请求路径＋返回描述符路径）。"""
    flags = ("O_WRONLY", "O_CREAT", *extra)
    return stamp([b.event("openat", path, flags=flags, target=path) for _ in range(2)], call)


def mkdir_at(b: T.Builder, path: str, call: Any) -> dict[str, Any]:
    return stamp(b.event("mkdir", path), call)


def unlink_at(b: T.Builder, path: str, call: Any) -> dict[str, Any]:
    return stamp(b.event("unlink", path), call)


def pair_at(b: T.Builder, name: str, source: str, destination: str, call: Any, *, ok: bool = True,
            errno: str | None = None) -> list[dict[str, Any]]:
    """两路径调用的两端事件，共享调用编号。"""
    common = dict(kind="write", ok=ok, errno=errno)
    return stamp([b.event(name, source, role="source", peer=destination, **common),
                  b.event(name, destination, role="destination", peer=source, **common)], call)


def feed(*groups: Any) -> tuple[runtime.OutputTrackerV4, list[bool]]:
    """依次喂入事件（单个事件字典或事件列表都可以），返回跟踪器与每条事件的「改状态」返回值。"""
    tracker = runtime.OutputTrackerV4(T.CONTRACT)
    changed = []
    for group in groups:
        for event in ([group] if isinstance(group, dict) else group):
            changed.append(tracker.observe_event(event))
    return tracker, changed


def problems_of(*groups: Any) -> list[str]:
    return feed(*groups)[0].report()["problems"]


# ---------------------------------------------------------------------------------------------------------------------
# 收割 #9 的真实切片
# ---------------------------------------------------------------------------------------------------------------------

RUN = D + "/sup/run"
HEARTBEATS = RUN + "/watchdog-heartbeats.ndjson"
_TMP_HEARTBEAT = RUN + "/.heartbeat.json.hc3y5wr5.tmp"
_TMP_STOP = RUN + "/.stop-request.json.w8okytjk.tmp"
_TMP_RECEIPT = RUN + "/.stop-receipt.json.dhxukouh.tmp"
_EXCL = ("O_CLOEXEC", "O_CREAT", "O_EXCL", "O_NOFOLLOW", "O_RDWR")
_NOFOLLOW = ("AT_SYMLINK_NOFOLLOW",)


def real_slice(b: T.Builder) -> list[list[dict[str, Any]]]:
    """10-16 收割 #9 ``staging_dispatch`` 的 v4 输出事件切片（原始序号 18369..18400，共 32 条，调用编号取真实值；
    私有盘根换成测试根，三层目录前缀缩成 ``<D>/sup/run``）。切片之前 ``state.json`` 早已被创建，所以这里先补一条创建。
    """
    r = RUN

    def miss(path: str, call: int, flags: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        return [probe_at(b, path, call, flags=flags)]

    def stat(path: str, call: int, flags: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        return [b.event("newfstatat", path, kind="stat", flags=flags, call=call)]

    def read(path: str, call: int) -> list[dict[str, Any]]:
        return [b.event("openat", path, kind="read", flags=("O_CLOEXEC", "O_RDONLY"), target=path, call=call)]

    def exclusive(path: str, call: int) -> list[dict[str, Any]]:
        return [b.event("openat", path, flags=_EXCL, target=path, call=call) for _ in range(2)]

    return [
        [mkdir_at(b, D, 1)], [mkdir_at(b, D + "/sup", 2)], [mkdir_at(b, r, 3)],
        create_at(b, r + "/state.json", 4, extra=("O_EXCL",))[:1],
        stat(r + "/heartbeat.json", 1100915),                                              # 18369 名字未知的成功 stat（待补证）
        exclusive(_TMP_HEARTBEAT, 1100917),                                                # 18370/18371
        pair_at(b, "renameat", _TMP_HEARTBEAT, r + "/heartbeat.json", 1100919),            # 18372/18373 销账
        miss(r + "/stop-request.json", 1100924),                                           # 18374
        miss(r + "/stop-request.json", 1100935, _NOFOLLOW),                                # 18375
        exclusive(_TMP_STOP, 1100941),                                                     # 18376/18377
        pair_at(b, "linkat", _TMP_STOP, r + "/stop-request.json", 1100942),                # 18378/18379
        [b.event("unlinkat", _TMP_STOP, call=1100943)],                                    # 18380
        miss(HEARTBEATS, 1100945, _NOFOLLOW),                                              # 18381 名字尚未生成
        miss(r + "/minute-ledger.ndjson", 1100948, _NOFOLLOW),                             # 18382
        miss(r + "/stop-receipt.json", 1100949, _NOFOLLOW),                                # 18383
        stat(r + "/state.json", 1100950, _NOFOLLOW),                                       # 18384
        stat(r + "/state.json", 1101677),                                                  # 18385
        read(r + "/state.json", 1101680),                                                  # 18386
        stat(r + "/heartbeat.json", 1101681, _NOFOLLOW),                                   # 18387
        stat(r + "/heartbeat.json", 1101682),                                              # 18388
        read(r + "/heartbeat.json", 1101685),                                              # 18389
        miss(HEARTBEATS, 1101716),                                                         # 18390 创建者自己的先检查
        create_at(b, HEARTBEATS, 1101718),                                                 # 18391/18392 O_APPEND|O_CREAT 创建
        miss(HEARTBEATS, 1101719, _NOFOLLOW),                                              # 18393 ← 乱序的并发探测
        stat(HEARTBEATS, 1101882, _NOFOLLOW),                                              # 18394
        stat(r + "/stop-request.json", 1101895),                                           # 18395
        stat(r + "/stop-request.json", 1101896, _NOFOLLOW),                                # 18396
        read(r + "/stop-request.json", 1101900),                                           # 18397
        miss(r + "/stop-receipt.json", 1101955),                                           # 18398
        exclusive(_TMP_RECEIPT, 1101972),                                                  # 18399/18400
    ]


class RealSliceTests(unittest.TestCase):
    """收割 #9 的真实切片。"""

    def setUp(self) -> None:
        self.b = T.Builder()
        self.groups = real_slice(self.b)

    def test_the_slice_has_the_thirty_two_real_events(self) -> None:
        """夹具自检：除补的 4 条（三层 mkdir＋state.json 创建）外，恰是真实切片的 32 条事件，调用编号严格不减。"""
        events = [event for group in self.groups[4:] for event in group]
        self.assertEqual(len(events), 32)
        calls = [event["call"] for event in events]
        self.assertEqual(calls, sorted(calls))
        late = [event for event in events if event["path"] == HEARTBEATS and event["call"] == 1101719]
        self.assertEqual([(e["kind"], e["errno"], e["flags"]) for e in late], [("missing", "ENOENT", ["AT_SYMLINK_NOFOLLOW"])])

    def test_the_slice_is_clean_after_the_fix(self) -> None:
        tracker, _ = feed(*self.groups)
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(tracker.awaiting, {})
        self.assertIn(HEARTBEATS, tracker.names)

    def test_without_the_tolerance_the_slice_reproduces_exactly_the_one_reported_problem(self) -> None:
        """对照：把窗口调成「永远放不过」（等价于修复前的行为），同一份事件正好得到收割 #9 报告里的那一条问题，没有别的。"""
        with mock.patch.object(runtime, "LATE_MISSING_PROBE_WINDOW_CALLS", -1):
            tracker, _ = feed(*self.groups)
        self.assertEqual(tracker.report()["problems"], [CONTRADICTION + HEARTBEATS])

    def test_the_late_probe_changes_nothing(self) -> None:
        """乱序的探测既不改状态（返回 False、纪元不推进），也不动名字表、子项计数、版本号、绑定编号与待补证。"""
        tracker = runtime.OutputTrackerV4(T.CONTRACT)

        def snapshot() -> tuple:
            return (dict(tracker.names), dict(tracker.entries), dict(tracker.versions), dict(tracker.bound_call),
                    dict(tracker.awaiting), set(tracker.problems), set(tracker.covered))

        checked = False
        for event in (event for group in self.groups for event in group):
            if event["path"] == HEARTBEATS and event["call"] == 1101719:
                before = snapshot()
                self.assertFalse(tracker.observe_event(event))
                self.assertEqual(before, snapshot())
                checked = True
            else:
                tracker.observe_event(event)
        self.assertTrue(checked, "切片里必须有乱序的那条探测")

    def test_the_slice_survives_a_full_replay(self) -> None:
        """真实事件形状（调用编号、成对事件）整段走重放端的格式校验与纪元重算，结论干净。"""
        document = R8.document_of(*self.groups)
        self.assertEqual(document["outputs"]["problems"], [])
        replayed, _seen = runtime._replay_events_v4(document, T.CONTRACT)
        self.assertEqual(replayed.report(), document["outputs"])

    def test_the_ordinary_order_is_unaffected(self) -> None:
        """另外三个 run 目录里的普通顺序（探测在前、创建在后、成功 stat 在后）：没有乱序探测，同样干净。"""
        groups = [group for group in self.groups if not any(e["call"] == 1101719 for e in group)]
        self.assertEqual(problems_of(*groups), [])


class WindowRuleTests(unittest.TestCase):
    """窗口、证据门槛与各种绑定方式。"""

    def setUp(self) -> None:
        self.b = T.Builder()
        self.parent = [mkdir_at(self.b, D, 1)]
        self.path = D + "/target"

    def evidenced_create(self, path: str | None = None, probe_call: int = 10, create_call: int = 12) -> list[Any]:
        """名字被探测过缺失（先行探测证据），再被 O_CREAT 创建。"""
        path = path or self.path
        return [self.parent, probe_at(self.b, path, probe_call), create_at(self.b, path, create_call)]

    # ------------------------------------------------------------------ 窗口边界

    def test_a_probe_exactly_at_the_window_edge_is_accepted(self) -> None:
        late = probe_at(self.b, self.path, 12 + W)
        tracker, changed = feed(*self.evidenced_create(), late)
        self.assertEqual(tracker.report()["problems"], [])
        self.assertFalse(changed[-1], "放过的探测不改状态")

    def test_a_probe_one_call_past_the_window_edge_is_a_contradiction(self) -> None:
        self.assertEqual(problems_of(*self.evidenced_create(), probe_at(self.b, self.path, 12 + W + 1)),
                         [CONTRADICTION + self.path])

    def test_the_lower_edge_is_inclusive_and_an_earlier_number_is_outside(self) -> None:
        """差为 0 在窗口内（规格 [0, W]）；探测编号小于绑定编号（轨迹顺序与调用编号矛盾）不在窗口内，退化为判矛盾。"""
        self.assertEqual(problems_of(*self.evidenced_create(), probe_at(self.b, self.path, 12)), [])
        self.assertEqual(problems_of(*self.evidenced_create(), probe_at(self.b, self.path, 11)), [CONTRADICTION + self.path])

    def test_the_window_constant_is_the_documented_value(self) -> None:
        self.assertEqual(runtime.LATE_MISSING_PROBE_WINDOW_CALLS, 512)
        self.assertIs(type(runtime.LATE_MISSING_PROBE_WINDOW_CALLS), int)

    def test_every_missing_probe_form_is_covered(self) -> None:
        """ENOENT／ENOTDIR、各种调用名与标志，只要是 kind=missing 的失败探测，窗口内一视同仁地放过。"""
        for errno in ("ENOENT", "ENOTDIR"):
            for name, flags in (("newfstatat", ()), ("newfstatat", _NOFOLLOW), ("openat", ("O_RDONLY",)), ("statx", ()), ("access", ())):
                with self.subTest(errno=errno, name=name, flags=flags):
                    self.assertEqual(problems_of(*self.evidenced_create(), probe_at(self.b, self.path, 20, errno=errno, name=name, flags=flags)), [])

    # ------------------------------------------------------------------ 正常序与删除

    def test_probes_before_the_creation_are_ordinary_and_never_problems(self) -> None:
        tracker, changed = feed(self.parent, probe_at(self.b, self.path, 5), probe_at(self.b, self.path, 6, flags=_NOFOLLOW))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(changed, [True, False, False])
        self.assertEqual(tracker.probed_missing, {self.path}, "名字未知时的缺失探测记为先行探测证据")

    def test_a_missing_probe_on_a_deleted_name_is_always_legitimate(self) -> None:
        """删除之后的 ENOENT 一直合法，与窗口和证据无关；删除还会忘掉绑定编号。"""
        tracker, _ = feed(*self.evidenced_create(), unlink_at(self.b, self.path, 13))
        self.assertNotIn(self.path, tracker.bound_call)
        tracker.observe_event(probe_at(self.b, self.path, 14))
        tracker.observe_event(probe_at(self.b, self.path, 14 + W + 5000))
        self.assertEqual(tracker.report()["problems"], [])

    def test_a_real_contradiction_far_after_the_creation_is_still_reported(self) -> None:
        """创建之后隔很远又探测到 ENOENT、中间没有任何删除事件：名字表与真实状态脱节（有一次删除没被观察到），照旧判矛盾。"""
        events = [*self.evidenced_create(), self.b.event("newfstatat", self.path, kind="stat", call=100),
                  probe_at(self.b, self.path, 5000)]
        self.assertEqual(problems_of(*events), [CONTRADICTION + self.path])

    def test_a_tolerated_probe_does_not_extend_the_window(self) -> None:
        """窗口从绑定算起，不因中间又有放过的探测而顺延：边缘上放过一次之后，再晚一点的探测仍按绑定算超窗。"""
        tracker, _ = feed(*self.evidenced_create(), probe_at(self.b, self.path, 12 + W))
        self.assertEqual(tracker.report()["problems"], [])
        tracker.observe_event(probe_at(self.b, self.path, 12 + W + 1))
        self.assertEqual(tracker.report()["problems"], [CONTRADICTION + self.path])

    # ------------------------------------------------------------------ 先行探测证据

    def test_without_a_prior_probe_the_probe_right_after_creation_is_a_contradiction(self) -> None:
        """没有先行探测证据：创建之后紧跟（调用编号只差 1）的 ENOENT 照旧判矛盾——既有测试里的夹具正是这个形状。"""
        create = create_at(self.b, self.path, 12)
        self.assertEqual(problems_of(self.parent, create, probe_at(self.b, self.path, 13)), [CONTRADICTION + self.path])

    def test_the_evidence_belongs_to_the_exact_name(self) -> None:
        other = D + "/other"
        events = [self.parent, probe_at(self.b, other, 10), create_at(self.b, self.path, 12), probe_at(self.b, self.path, 13)]
        self.assertEqual(problems_of(*events), [CONTRADICTION + self.path], "别的名字的先行探测不能给这个名字作证")

    def test_a_prior_probe_on_the_parent_directory_is_not_evidence_for_the_child(self) -> None:
        events = [probe_at(self.b, D, 2), mkdir_at(self.b, D, 3), create_at(self.b, self.path, 12), probe_at(self.b, self.path, 13)]
        self.assertEqual(problems_of(*events), [CONTRADICTION + self.path])

    def test_the_evidence_is_consumed_by_the_binding(self) -> None:
        """名字被删除后重新创建：上一轮的先行探测不再作数，需要新的先行探测。"""
        base = [*self.evidenced_create(), unlink_at(self.b, self.path, 13)]
        without = [*base, create_at(self.b, self.path, 15), probe_at(self.b, self.path, 16)]
        self.assertEqual(problems_of(*without), [CONTRADICTION + self.path])
        with_probe = [*base, probe_at(self.b, self.path, 14), create_at(self.b, self.path, 15), probe_at(self.b, self.path, 16)]
        self.assertEqual(problems_of(*with_probe), [])

    def test_a_known_name_probe_is_not_recorded_as_evidence(self) -> None:
        """名字已知时（哪怕被放过）的探测不是先行探测：只有未知名字上的缺失探测才记证据。"""
        tracker, _ = feed(*self.evidenced_create(), probe_at(self.b, self.path, 13))
        self.assertEqual(tracker.probed_missing, set())

    # ------------------------------------------------------------------ 各种绑定方式

    def test_hardlink_destination_is_a_binding(self) -> None:
        source, destination = D + "/src", D + "/dst"
        events = [self.parent, create_at(self.b, source, 3), probe_at(self.b, destination, 5),
                  pair_at(self.b, "link", source, destination, 7), probe_at(self.b, destination, 8)]
        self.assertEqual(problems_of(*events), [])
        # 来源名字是更早绑定的、没有先行探测：它上面紧跟的探测仍判矛盾。
        self.assertEqual(problems_of(*events, probe_at(self.b, source, 9)), [CONTRADICTION + source])

    def test_rename_destination_is_a_binding_and_the_old_name_is_plain_unknown(self) -> None:
        source, destination = D + "/old", D + "/new"
        events = [self.parent, create_at(self.b, source, 3), probe_at(self.b, destination, 5),
                  pair_at(self.b, "rename", source, destination, 7), probe_at(self.b, destination, 8), probe_at(self.b, source, 9)]
        self.assertEqual(problems_of(*events), [], "新名字放过；旧名字已解绑，ENOENT 本来就合法")
        tracker, _ = feed(*events)
        self.assertEqual(tracker.bound_call, {destination: 7})
        self.assertNotIn(source, tracker.bound_call)

    def test_rename_without_a_prior_probe_gets_no_tolerance(self) -> None:
        source, destination = D + "/old", D + "/new"
        events = [self.parent, create_at(self.b, source, 3), pair_at(self.b, "rename", source, destination, 7),
                  probe_at(self.b, destination, 8)]
        self.assertEqual(problems_of(*events), [CONTRADICTION + destination])

    def test_directory_rename_rebinds_every_name_of_the_subtree(self) -> None:
        s, n = D + "/s", D + "/n"
        events = [self.parent, mkdir_at(self.b, s, 2), mkdir_at(self.b, s + "/t", 3), create_at(self.b, s + "/x", 4),
                  create_at(self.b, s + "/t/y", 5),
                  probe_at(self.b, n, 6), probe_at(self.b, n + "/x", 6 + 1),          # 目的端新名字 n、n/x 被探测过缺失；n/t/y 没有
                  pair_at(self.b, "rename", s, n, 20)]
        tracker, _ = feed(*events)
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual({k: v for k, v in tracker.bound_call.items() if k.startswith(n)}, {n: 20, n + "/x": 20})
        tracker.observe_event(probe_at(self.b, n, 21))
        tracker.observe_event(probe_at(self.b, n + "/x", 22))
        self.assertEqual(tracker.report()["problems"], [])
        tracker.observe_event(probe_at(self.b, n + "/t/y", 23))
        self.assertEqual(tracker.report()["problems"], [CONTRADICTION + n + "/t/y"], "n/t/y 没有先行探测，不享受容忍")

    def test_mkdir_and_symlink_creations_are_bindings_too(self) -> None:
        directory, link = D + "/dir", D + "/ln"
        events = [self.parent, probe_at(self.b, directory, 5), mkdir_at(self.b, directory, 6), probe_at(self.b, directory, 7),
                  probe_at(self.b, link, 8), self.b.event("symlinkat", link, target="somewhere", call=9)]
        self.assertEqual(problems_of(*events), [])
        # 符号链接名字上的失败 lstat 按另一套规则（_symlink_name_verdict）判，不在本规则内：照旧判问题。
        self.assertTrue(problems_of(*events, self.b.lstat(link, ok=False, errno="ENOENT")[0]))

    # ------------------------------------------------------------------ 调用编号缺失或类型不对：退化为原行为

    def test_missing_or_malformed_call_numbers_degrade_to_the_old_behaviour(self) -> None:
        # 绑定编号取 1：如果非整数值（浮点 1.0、布尔 True 的数值都是 1）被误当成整数，差为 0，会落进窗口被放过，这里就能抓出来。
        for label, value in (("None", None), ("字符串", "1"), ("浮点", 1.0), ("布尔", True), ("列表", [1])):
            with self.subTest(label=label):
                late = probe_at(self.b, self.path, value)
                self.assertEqual(problems_of(*self.evidenced_create(probe_call=0, create_call=1), late), [CONTRADICTION + self.path])
        with self.subTest(label="探测事件根本没有 call 键"):
            late = probe_at(self.b, self.path, 2)
            del late["call"]
            self.assertEqual(problems_of(*self.evidenced_create(probe_call=0, create_call=1), late), [CONTRADICTION + self.path])
        with self.subTest(label="对照：同样的整数编号在窗口内"):
            self.assertEqual(problems_of(*self.evidenced_create(probe_call=0, create_call=1), probe_at(self.b, self.path, 1)), [])

    def test_a_binding_without_an_integer_call_number_records_nothing(self) -> None:
        for label, value in (("None", None), ("字符串", "12"), ("布尔", True)):
            with self.subTest(label=label):
                create = create_at(self.b, self.path, value)
                tracker, _ = feed(self.parent, probe_at(self.b, self.path, 10), create)
                self.assertNotIn(self.path, tracker.bound_call)
                tracker.observe_event(probe_at(self.b, self.path, 13))
                self.assertEqual(tracker.report()["problems"], [CONTRADICTION + self.path])
        create = create_at(self.b, self.path, 12)
        for event in create:
            del event["call"]
        tracker, _ = feed(self.parent, probe_at(self.b, self.path, 10), create, probe_at(self.b, self.path, 13))
        self.assertEqual(tracker.report()["problems"], [CONTRADICTION + self.path], "旧测试里没有 call 键的夹具：原行为")

    def test_a_later_binding_without_a_call_number_forgets_the_old_one(self) -> None:
        """名字重新绑定时若新事件没有整数调用编号，旧绑定编号不能残留（否则会拿旧编号去放过新对象上的探测）。"""
        events = [*self.evidenced_create(), unlink_at(self.b, self.path, 13), probe_at(self.b, self.path, 14)]
        recreate = create_at(self.b, self.path, None)
        tracker, _ = feed(*events, recreate)
        self.assertNotIn(self.path, tracker.bound_call)
        tracker.observe_event(probe_at(self.b, self.path, 20))
        self.assertEqual(tracker.report()["problems"], [CONTRADICTION + self.path])

    # ------------------------------------------------------------------ 其它矛盾类别原样保留

    def test_other_contradiction_classes_are_not_relaxed(self) -> None:
        late = 13

        def run(*tail: Any) -> list[str]:
            return problems_of(*self.evidenced_create(), *tail)

        self.assertIn("创建目录时已存在：" + self.path, run(self.b.event("mkdir", self.path, call=late)))
        self.assertIn("O_CREAT|O_EXCL 创建成功却落在已知名字上，与轨迹矛盾：" + self.path,
                      run(self.b.event("openat", self.path, flags=("O_CREAT", "O_EXCL", "O_WRONLY"), target=self.path, call=late)))
        self.assertEqual(problems_of(self.parent, unlink_at(self.b, D + "/ghost", 5)), ["删除未证明的输出：" + D + "/ghost"])
        self.assertEqual(run(self.b.event("unlink", self.path, ok=False, errno="ENOENT", call=late)), [CONTRADICTION + self.path],
                         "写类调用（unlink）的 ENOENT 不在本轮范围，窗口与证据都不影响")
        self.assertEqual(run(self.b.event("openat", self.path, ok=False, errno="EACCES", call=late, kind="write")),
                         ["未建立合同的失败调用（EACCES）：openat:" + self.path])
        self.assertEqual(run(self.b.event("mkdir", self.path, ok=False, errno="EEXIST", call=late)), [], "创建被拒绝（EEXIST）本来合法")

    def test_the_v3_tracker_is_untouched(self) -> None:
        """v3 合同不动：旧跟踪器对已生成对象的缺失探测一律判矛盾，不认窗口与证据。"""
        tracker = runtime.OutputTracker(T.CONTRACT)
        tracker.observe(D, "write", "mkdir", "", True)
        tracker.observe(self.path, "missing", "newfstatat", "", False)
        tracker.observe(self.path, "write", "openat", "O_CREAT|O_WRONLY", True, self.path)
        tracker.observe(self.path, "missing", "newfstatat", "", False)
        self.assertEqual(sorted(tracker.problems), [CONTRADICTION + self.path])
        self.assertFalse(hasattr(tracker, "bound_call"))


class TwoPathWindowTests(unittest.TestCase):
    """rename／link 失败（ENOENT）时「来源已知却报缺失」的同口径窗口规则。"""

    def setUp(self) -> None:
        self.b = T.Builder()
        self.parent = [mkdir_at(self.b, D, 1), mkdir_at(self.b, D + "/dst", 2)]
        self.source, self.destination = D + "/src", D + "/dst/out"

    def failed(self, call: Any, name: str = "rename", errno: str = "ENOENT", destination: str | None = None) -> list[dict[str, Any]]:
        return pair_at(self.b, name, self.source, destination or self.destination, call, ok=False, errno=errno)

    def test_a_failed_rename_of_a_just_created_source_is_accepted_with_a_prior_failure(self) -> None:
        """先行证据可以来自同一个来源名字上更早的失败 rename（目的父目录已证明，所以 ENOENT 只能是来源缺失）。"""
        events = [self.parent, self.failed(5), create_at(self.b, self.source, 7), self.failed(8)]
        tracker, changed = feed(*events)
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(changed, [True, True, False, False, True, False, False, False],
                         "mkdir×2 改状态；失败的 rename 两端、创建的第二条事件、再次失败的 rename 两端都不改")

    def test_a_failed_link_with_a_single_path_probe_as_evidence(self) -> None:
        events = [self.parent, probe_at(self.b, self.source, 5), create_at(self.b, self.source, 7), self.failed(8, "link")]
        self.assertEqual(problems_of(*events), [])
        self.assertEqual(problems_of(*events[:-1], self.failed(8, "linkat", "ENOTDIR")), [])

    def test_the_window_edge_applies_to_the_source_name(self) -> None:
        base = [self.parent, probe_at(self.b, self.source, 5), create_at(self.b, self.source, 7)]
        self.assertEqual(problems_of(*base, self.failed(7 + W)), [])
        self.assertEqual(problems_of(*base, self.failed(7 + W + 1)), [CONTRADICTION + self.source])

    def test_without_a_prior_probe_the_old_rule_holds(self) -> None:
        events = [self.parent, create_at(self.b, self.source, 7), self.failed(8)]
        self.assertEqual(problems_of(*events), [CONTRADICTION + self.source])

    def test_a_missing_destination_parent_is_still_explained_by_the_old_rule(self) -> None:
        """目标父目录缺失造成的 ENOENT 本来就合法（来源已知也一样）；目标一端的乱序容忍不在本轮范围内。"""
        events = [self.parent, create_at(self.b, self.source, 7), self.failed(8, destination=D + "/nodir/out")]
        self.assertEqual(problems_of(*events), [])

    def test_call_numbers_missing_degrade(self) -> None:
        events = [self.parent, probe_at(self.b, self.source, 5), create_at(self.b, self.source, 7), self.failed(None)]
        self.assertEqual(problems_of(*events), [CONTRADICTION + self.source])

    def test_other_failure_codes_are_still_problems(self) -> None:
        events = [self.parent, probe_at(self.b, self.source, 5), create_at(self.b, self.source, 7), self.failed(8, errno="EXDEV")]
        self.assertEqual(problems_of(*events), ["未建立合同的失败调用（EXDEV）：rename:" + self.source])


class ReportAndSchemaTests(unittest.TestCase):
    """report() 与文档 schema 不变：生产端与重放端仍逐字节一致。"""

    def test_report_has_the_same_three_keys_and_no_new_fields(self) -> None:
        b = T.Builder()
        tracker, _ = feed([mkdir_at(b, D, 1)], probe_at(b, D + "/x", 2), create_at(b, D + "/x", 3), probe_at(b, D + "/x", 4))
        report = tracker.report()
        self.assertEqual(set(report), {"root", "paths", "problems"})
        self.assertEqual(report, {"root": ROOT, "paths": [D, D + "/x"], "problems": []})

    def test_the_event_schema_is_unchanged(self) -> None:
        self.assertEqual(runtime._V4_EVENT_KEYS,
                         frozenset({"path", "kind", "name", "succeeded", "flags", "target", "errno", "call", "role", "peer", "epoch"}))
        self.assertEqual(runtime.TRACE_SCHEMA_V4, "read-audit-trace/v4")

    def test_report_is_independent_of_the_tolerance_state(self) -> None:
        """放过一次乱序探测之后，report() 与「没有这条探测」的轨迹完全相同（covered 路径本来就包含该名字）。"""
        b = T.Builder()
        base = [[mkdir_at(b, D, 1)], probe_at(b, D + "/x", 2), create_at(b, D + "/x", 3)]
        with_late = feed(*base, probe_at(b, D + "/x", 4))[0].report()
        without = feed(*base)[0].report()
        self.assertEqual(with_late, without)
        self.assertEqual(with_late, feed(*base, probe_at(b, D + "/x", 4))[0].report(), "report() 只读、可重复")


class EndToEndTests(AuditFixture):
    """strace 行（多 pid 交错）→ 解析器 → 文档 → 重放整条链。"""

    FILE = "sup/heartbeats.ndjson"

    def miss(self, pid: int, flags: str = "AT_SYMLINK_NOFOLLOW") -> str:
        """对测试文件的 ENOENT 探测；flags 是第四个参数的原文（``0`` 表示无标志）。"""
        path = f"{self.root}/{self.FILE}"
        return f'{pid} newfstatat(AT_FDCWD</repo>, "{path}", 0x1, {flags}) = -1 ENOENT (No such file or directory)'

    def ok_stat(self, pid: int) -> str:
        path = f"{self.root}/{self.FILE}"
        return f'{pid} newfstatat(AT_FDCWD</repo>, "{path}", {{st_mode=S_IFREG|0644}}, AT_SYMLINK_NOFOLLOW) = 0'

    def create(self, pid: int) -> str:
        path = f"{self.root}/{self.FILE}"
        return f'{pid} openat(AT_FDCWD</repo>, "{path}", O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC, 0644) = 5<{path}>'

    def lines(self, *, pre_probe: bool = True, filler: int = 0) -> list[str]:
        """pid 2 是创建者（先检查再创建），pid 3 是并发的 monitor：它的 ENOENT 行被打印在创建行之后；pid 1 随后成功 stat。"""
        lines = [self.mkdir_line("sup")]
        if pre_probe:
            lines.append(self.miss(2, "0"))
        lines.append(self.create(2))
        lines += [f'4 newfstatat(AT_FDCWD</repo>, "{self.root}/zz/f{i}", 0x1, 0) = -1 ENOENT (No such file or directory)'
                  for i in range(filler)]
        lines += [self.miss(3), self.ok_stat(1)]
        return lines

    def test_the_interleaved_late_probe_is_accepted_end_to_end(self) -> None:
        document = self.document(self.lines())
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertAccepted(document)
        self.assertIsNotNone(runtime.replay_runtime(document, [self.entry]))
        events = [e for e in self.shape(document) if e["path"] == f"<R>/{self.FILE}"]
        late = events[-2]
        self.assertEqual((late["kind"], late["errno"], late["call"] - events[2]["call"]), ("missing", "ENOENT", 1),
                         "夹具复现的就是调用编号只差 1 的形状")

    def test_without_the_prior_probe_it_is_rejected(self) -> None:
        document = self.document(self.lines(pre_probe=False))
        self.assertEqual(document["runtime"]["outputs"]["problems"], [CONTRADICTION + f"{self.root}/{self.FILE}"])
        self.assertRejected(document)

    def test_a_probe_beyond_the_window_is_rejected(self) -> None:
        """填充调用（别的名字上的 ENOENT）把乱序探测与创建的调用编号差拉到边缘：差 = W 放过，差 = W+1 判矛盾。"""
        inside = self.document(self.lines(filler=W - 1))
        self.assertEqual(inside["runtime"]["outputs"]["problems"], [])
        self.assertAccepted(inside)
        document = self.document(self.lines(filler=W))
        self.assertEqual(document["runtime"]["outputs"]["problems"], [CONTRADICTION + f"{self.root}/{self.FILE}"])
        self.assertRejected(document)

    def test_the_v3_contract_is_not_relaxed(self) -> None:
        document = self.document(self.lines(), contract="v3")
        self.assertTrue(any(CONTRADICTION in text for text in document["runtime"]["outputs"]["problems"]))
        self.assertFalse(self.audit(document)["coverage_complete"])

    def consistent(self, document: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """换上事件列表，并像解析器一样重算每条事件的纪元与 outputs，只留下跟踪器本身能拒绝的余地。"""
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

    def test_dropping_the_prior_probe_event_from_a_good_document_is_rejected(self) -> None:
        """先行探测证据由重放端从事件里重新推出：把创建者的那条先行探测删掉（并如实重算纪元与 outputs），乱序探测立刻变成矛盾，被拒。"""
        document = self.document(self.lines())
        self.assertAccepted(document)
        target = f"{self.root}/{self.FILE}"
        events = [dict(e) for e in document["runtime"]["output_events"]]
        first_probe = next(i for i, e in enumerate(events) if e["path"] == target and e["kind"] == "missing")
        del events[first_probe]
        tampered = self.consistent(document, events)
        self.assertEqual(tampered["runtime"]["outputs"]["problems"], [CONTRADICTION + target])
        self.assertRejected(tampered)

    def test_forging_a_clean_outputs_block_over_a_contradiction_is_rejected(self) -> None:
        document = self.document(self.lines(pre_probe=False))
        forged = copy.deepcopy(document)
        forged["runtime"]["outputs"]["problems"] = []
        self.assertRejected(forged)

    def test_deduplicated_documents_replay_to_the_same_report(self) -> None:
        """反复的同键探测在解析端被去重，重放端只看到保留的事件，两端的证据与结论必须相同。"""
        target = f"{self.root}/{self.FILE}"
        repeated = ([self.mkdir_line("sup")] + [self.miss(2, "0")] * 5 + [self.create(2)] + [self.miss(3)] * 4 + [self.ok_stat(1)])
        document = self.document(repeated)
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])
        self.assertAccepted(document)
        kept = [e for e in document["runtime"]["output_events"] if e["path"] == target and e["kind"] == "missing"]
        self.assertEqual(len(kept), 2, "先行探测与窗口内的首条探测各留一条，其余 7 条重复被去重")
        self.assertIsNotNone(runtime.replay_runtime(document, [self.entry]))


# ---------------------------------------------------------------------------------------------------------------------
# 去重键与随机对拍
# ---------------------------------------------------------------------------------------------------------------------


def late_probe_stream(seed: int, steps: int = 170, clean: bool = False) -> list[list[dict[str, Any]]]:
    """随机事件流：一批名字在目录下被探测、创建（共享调用编号的两条事件）、再被探测（窗口内外都有）、删除、rename、硬链接、成功 stat、
    失败的 rename；调用编号随机跳跃，所以探测与绑定的差会自然越过窗口边缘。重复同一个探测（同键事件）很常见，用来检验去重。

    ``clean=True``：生成器自己镜像「先行探测证据」与绑定编号，只在跟踪器会放过的条件下（名字有证据且探测落在窗口内）才对已知名字
    发缺失探测／失败 rename，所以整条流零问题，可以走完整重放；``clean=False`` 不管这些，流里有大量被放过的探测，也有大量窗口外／无证据的矛盾。"""
    rng = random.Random(77000 + seed)
    b = T.Builder()
    number = 0
    parents = [D, D + "/p", D + "/q"]
    groups: list[list[dict[str, Any]]] = []
    known: dict[str, str] = {}
    probed: set[str] = set()                    # 镜像 probed_missing：目前未知、却已被探测过缺失的名字
    bound: dict[str, int] = {}                  # 镜像 bound_call：只记绑定前有过缺失探测的名字

    def advance(low: int = 1, high: int = 150) -> int:
        nonlocal number
        number += rng.randint(low, high)
        return number

    def bind(path: str, call: int) -> None:
        known[path] = "file"
        if path in probed:
            bound[path] = call
        else:
            bound.pop(path, None)
        probed.discard(path)

    def tolerated(path: str, call: int) -> bool:
        return path in bound and 0 <= call - bound[path] <= W

    for parent in parents:
        groups.append([mkdir_at(b, parent, advance(1, 3))])
    pool = [f"{parent}/n{i}" for parent in parents for i in range(4)]
    for _ in range(steps):
        path = rng.choice(pool)
        action = rng.choice(["probe", "probe", "create", "create", "late", "late", "late", "repeat", "stat", "unlink",
                             "rename", "link", "failed_rename"])
        if path in known:
            if action in ("probe", "late", "repeat"):
                flags = rng.choice([(), _NOFOLLOW])
                call = number + rng.randint(1, 90 if action == "late" else 400)
                if clean and not tolerated(path, call):
                    continue
                number = call
                groups.append([probe_at(b, path, call, flags=flags)])
                if action == "repeat":
                    again = number + rng.randint(1, 20)
                    if not clean or tolerated(path, again):
                        number = again
                        groups.append([copy.deepcopy(groups[-1][0]) | {"call": again}])
            elif action == "stat":
                groups.append([b.event("newfstatat", path, kind="stat", call=advance())])
            elif action == "unlink" and known[path] == "file":
                groups.append([unlink_at(b, path, advance())])
                del known[path]
                bound.pop(path, None)
            elif action in ("rename", "link"):
                destination = rng.choice(pool)
                if destination not in known and known[path] == "file":
                    call = advance()
                    groups.append(pair_at(b, action, path, destination, call))
                    bind(destination, call)
                    if action == "rename":
                        del known[path]
                        bound.pop(path, None)
            elif action == "failed_rename" and known[path] == "file":
                call = number + rng.randint(1, 90)
                if not clean or tolerated(path, call):
                    number = call
                    groups.append(pair_at(b, "rename", path, rng.choice(pool) + "x", call, ok=False, errno="ENOENT"))
        else:
            if action in ("probe", "late", "repeat", "stat"):
                groups.append([probe_at(b, path, advance(1, 60), flags=rng.choice([(), _NOFOLLOW]))])
                probed.add(path)
            elif action == "create":
                call = advance(1, 5)
                groups.append(create_at(b, path, call))
                bind(path, call)
            elif action == "failed_rename":
                groups.append(pair_at(b, "rename", path, rng.choice(parents) + "/zz", advance(), ok=False, errno="ENOENT"))
                probed.add(path)
    return groups


class KeyAndDedupTests(R8.DedupHarness):
    """去重键新增的一位，以及随机对拍。"""

    def setUp(self) -> None:
        super().setUp()
        self.b = T.Builder()
        self.path = D + "/target"

    def key_of(self, tracker: runtime.OutputTrackerV4, event: dict[str, Any]) -> tuple | None:
        return tracker.dedup_key(dict(event))

    def test_the_key_separates_probes_inside_and_outside_the_window(self) -> None:
        b = self.b
        tracker, _ = feed([mkdir_at(b, D, 1)], probe_at(b, self.path, 10), create_at(b, self.path, 12))
        inside = self.key_of(tracker, probe_at(b, self.path, 20))
        outside = self.key_of(tracker, probe_at(b, self.path, 12 + W + 1))
        self.assertNotEqual(inside, outside)
        self.assertEqual(inside[-1], True)
        self.assertEqual(outside[-1], False)
        self.assertEqual(inside[:-1], outside[:-1], "除了窗口位，其余键内容相同——正是需要这一位的原因")

    def test_the_key_bit_agrees_with_the_verdict_for_malformed_call_numbers(self) -> None:
        """键里的窗口位必须与 observe_event 的真实判定一致：非整数调用编号（含布尔、浮点）的探测，窗口位为假，判定为矛盾。"""
        b = self.b
        for label, value in (("None", None), ("字符串", "1"), ("浮点", 1.0), ("布尔", True)):
            with self.subTest(label=label):
                tracker, _ = feed([mkdir_at(b, D, 1)], probe_at(b, self.path, 0), create_at(b, self.path, 1))
                event = probe_at(b, self.path, value)
                self.assertIs(self.key_of(tracker, event)[-1], False)
                tracker.observe_event(dict(event))
                self.assertEqual(tracker.report()["problems"], [CONTRADICTION + self.path])
        tracker, _ = feed([mkdir_at(b, D, 1)], probe_at(b, self.path, 0), create_at(b, self.path, 1))
        no_call = probe_at(b, self.path, 2)
        del no_call["call"]
        self.assertIs(self.key_of(tracker, no_call)[-1], False, "没有 call 键的事件也能取键，窗口位为假")

    def test_a_far_probe_is_not_dropped_as_a_duplicate_of_a_tolerated_one(self) -> None:
        """解析器按键去重：窗口内被放过的首条探测，不能把之后窗口外、本该判矛盾的同字段探测当作重复丢掉。"""
        b = self.b
        stream = [self.translate(e) for e in [mkdir_at(b, D, 1), probe_at(b, self.path, 10), *create_at(b, self.path, 12),
                                               probe_at(b, self.path, 13), probe_at(b, self.path, 12 + W + 40)]]
        parser = self.feed(stream)
        self.assertEqual(parser.output_tracker.report()["problems"], [CONTRADICTION + stream[2]["path"]])
        self.assertEqual(self.reference(parser.output_events).report(), self.reference(stream).report(),
                         "只重放保留事件，结论与全量事件相同")
        self.assertEqual(len([e for e in parser.output_events if e["kind"] == "missing"]), 3, "先行探测＋窗口内一条＋窗口外一条，都保留")

    def test_a_repeated_probe_inside_the_window_is_deduplicated_as_before(self) -> None:
        b = self.b
        stream = [self.translate(e) for e in [mkdir_at(b, D, 1), probe_at(b, self.path, 10), *create_at(b, self.path, 12),
                                               probe_at(b, self.path, 13), probe_at(b, self.path, 14), probe_at(b, self.path, 15)]]
        parser = self.feed(stream)
        self.assertEqual(parser.output_tracker.report()["problems"], [])
        self.assertEqual(len([e for e in parser.output_events if e["kind"] == "missing"]), 2, "先行探测＋窗口内的首条探测；后两条是重复")
        self.assertEqual(self.reference(parser.output_events).report(), self.reference(stream).report())

    def test_the_stream_generator_exercises_every_branch(self) -> None:
        """对拍用的随机流自检：确实产生了被放过的探测、窗口外的矛盾、先行探测证据、失败的 rename 与大量重复。"""
        tolerated = contradictions = failed_two_path = failed_tolerated = repeats = 0
        for seed in range(40):
            tracker = R8.SpyTracker(T.CONTRACT)
            seen: set[tuple] = set()
            for group in late_probe_stream(seed):
                for event in group:
                    key = tracker.dedup_key(dict(event))
                    known_source = event["path"] in tracker.names
                    outcome = tracker.outcome_of(dict(event))
                    flagged = any(text.startswith(CONTRADICTION) for text in outcome[1])
                    if event["kind"] == "missing" and known_source and event["name"] not in runtime.TWO_PATH_NAMES:
                        contradictions += flagged
                        tolerated += not flagged
                    if event["name"] in runtime.TWO_PATH_NAMES and not event["succeeded"] and event["role"] == "source":
                        failed_two_path += 1
                        failed_tolerated += known_source and not flagged
                    if key is not None:
                        repeats += key in seen
                        seen.add(key)
        self.assertGreater(tolerated, 40, "必须有被放过的探测")
        self.assertGreater(contradictions, 40, "必须有窗口外／无证据而判矛盾的探测")
        self.assertGreater(failed_two_path, 40)
        self.assertGreater(failed_tolerated, 3, "必须有来源已知却被放过的失败 rename")
        self.assertGreater(repeats, 200)

    def test_events_with_equal_keys_always_have_equal_outcomes(self) -> None:
        """键精确性：同一个键的所有事件，跟踪器的观察结果（是否改状态、新增哪些问题、待补证增减）完全一致。"""
        repeats = flagged = 0
        for seed in range(80):
            tracker = R8.SpyTracker(T.CONTRACT)
            by_key: dict[tuple, tuple] = {}
            for group in late_probe_stream(seed, clean=seed % 3 == 0):
                for event in group:
                    key = tracker.dedup_key(dict(event))
                    outcome = tracker.outcome_of(dict(event))
                    flagged += bool(outcome[1])
                    if key is None:
                        continue
                    if key in by_key:
                        repeats += 1
                        if by_key[key] != outcome:
                            self.fail(f"seed={seed}：键相同、观察结果不同\n事件 {event}\n先前 {by_key[key]}\n现在 {outcome}")
                    else:
                        by_key[key] = outcome
        self.assertGreater(repeats, 1500, "随机流必须产生大量同键事件，否则这个性质测试是空转")
        self.assertGreater(flagged, 50, "必须有带问题的结果")

    def test_deduplicated_streams_have_the_same_verdict_as_the_full_streams(self) -> None:
        """去重不改变判定：全量事件的最终报告 ＝ 解析器自己的报告 ＝ 只重放保留事件的报告；干净的流走完整重放。"""
        dropped = clean = bad = tolerated_events = 0
        for seed in range(80):
            stream = [self.translate(e) for group in late_probe_stream(seed, clean=seed % 2 == 0) for e in group]
            reference = self.reference(stream)
            parser = self.feed(stream)
            context = f"seed={seed}"
            if seed % 2 == 0:
                self.assertEqual(reference.report()["problems"], [], context + "：clean 模式的流必须零问题")
                spy = R8.SpyTracker(self.contract)
                for e in stream:
                    known = e["path"] in spy.names
                    outcome = spy.outcome_of(dict(e))
                    tolerated_events += bool(e["kind"] == "missing" and known and e["name"] not in runtime.TWO_PATH_NAMES
                                             and not outcome[1])
            self.assertEqual(parser.output_tracker.report(), reference.report(), context)
            only_kept = self.reference(parser.output_events)
            self.assertEqual(only_kept.report(), reference.report(), context + "：只重放保留事件得到不同的结论")
            self.assertEqual((only_kept.names, only_kept.awaiting, only_kept.probed_missing, only_kept.bound_call),
                             (reference.names, reference.awaiting, reference.probed_missing, reference.bound_call), context)
            dropped += len(stream) - len(parser.output_events)
            runtime_part = {"output_events": parser.output_events, "outputs": parser.output_tracker.report()}
            if reference.report()["problems"]:
                bad += 1
                with self.assertRaises(runtime.RuntimeContractError, msg=context):
                    runtime._replay_events_v4(runtime_part, self.contract)
            else:
                clean += 1
                replayed, _seen = runtime._replay_events_v4(runtime_part, self.contract)
                self.assertEqual(replayed.names, reference.names, context)
        self.assertGreater(dropped, 1500, "必须真的丢掉了大量重复事件，否则对拍是空转")
        self.assertGreater(bad, 10)
        self.assertGreater(clean, 30, "必须有干净的流，才能走完整重放")
        self.assertGreater(tolerated_events, 30, "干净的流里必须真的含有被放过的乱序探测，完整重放才证明了重放端与解析端的证据一致")

    def test_a_narrowed_key_is_caught_by_this_comparison(self) -> None:
        """对拍有牙：把新增的窗口位从键里去掉（等价于忘了它），随机流里立刻出现「保留事件的结论≠全量事件的结论」。"""
        tracker_class = ra._runtime_module().OutputTrackerV4
        original = tracker_class.dedup_key

        def narrowed(self: Any, event: dict[str, Any]) -> tuple | None:
            key = original(self, event)
            if key is not None and event["kind"] == "missing" and event["name"] not in runtime.TWO_PATH_NAMES:
                return key[:-1]
            return key

        differing = 0
        with mock.patch.object(tracker_class, "dedup_key", narrowed):
            for seed in range(40):
                stream = [self.translate(e) for group in late_probe_stream(seed) for e in group]
                parser = self.feed(stream)
                if self.reference(parser.output_events).report() != self.reference(stream).report():
                    differing += 1
        self.assertGreater(differing, 2, "去掉窗口位后，必须在随机流上暴露出差异")


if __name__ == "__main__":
    unittest.main()
