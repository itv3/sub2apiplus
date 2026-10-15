"""WP-R13（审计解析器加固第十三轮）：connect 的符号链接路径形态检查；失败的“引入”两路径调用；经自己的符号链接读盘外宿主文件。

来源
====

* **T1（独立验收阻断项 B-1）**：``connect`` 到 AF_UNIX 绝对路径得到 ENOENT 的放宽（WP-R2 第 4 项）没有经过符号链接路径形态检查。
  私有盘里先建一个指向宿主目录的符号链接 ``ln``，再连 ``<R>/ln/../zz-sock``：规范化成 ``<R>/zz-sock``，被登记成合法的盘内缺失探测，
  内核实际探测的却是宿主路径 ``/zz-sock``。同样写法的 ``newfstatat``／``openat2`` 早就得到 ``symlink-path-form-unsafe``。
* **T2（10-16 ARM64 收割 #9，``test_codex_upgrade_evaluation_builder`` 5 个用例全过，输出归属重放 664 条问题）**，两类：

  - 460 条“两路径调用的另一端不在私有临时盘内”：``prepare_copy_pycache`` 对共享层里的每个 .pyc 先 ``os.link(盘外源, 盘内目的)``，私有盘是独立 tmpfs，
    跨设备得到 EXDEV，再回退成复制。失败的调用没有任何命名空间效果，盘外一端只是对宿主缓存路径的存在性探测；
  - 204 条“路径的中间分量是符号链接，属于跟随式访问”：同一个函数把标准库缓存目录建成指向盘外共享层的符号链接（``symlinkat`` 成功），
    python 随后经它成功只读打开 .pyc；描述符的真实路径标注（``-y``）在盘外，已被解析器登记成宿主读取。

本模块
======

* ``RealEvidenceTests``：证据切片里的真实事件（symlinkat 窗口 2590～2605、linkat EXDEV 回退窗口 3060～3072、经链接的读取 4082）整段走完，
  修复前精确复现两类问题，修复后零问题，名字表不动、纪元不推进，整段通过完整重放；
* ``FailedImportRuleTests``：失败“引入”的放行条件与边界（成功的仍判、盘内来源盘外目的仍判、盘内目的 EEXIST 落未知路径仍判、其它错误码仍判……）；
* ``OwnLinkForeignReadTests``：经自己的符号链接读盘外的放行条件，每个条件各一个反例；
* ``KeyAndDedupTests``：放行结论进去重键；同键同判定的随机对拍；去重后的流与全量流结论相同、干净的流通过完整重放；
* ``ParserRegistrationTests``：解析器把盘外一端登记为宿主访问（失败时是 stat／missing，不再是永远不可声明的 write），宿主路径声明审计看得见；
* ``FullAuditAcceptanceTests``：两处新放行加上真实宿主文件的快照声明，整条严格审计（输出归属重放＋宿主路径声明）通过；退回修复前的 ``write`` 登记则永远过不了；
* ``OwnLinkE2ETests`` / ``ConnectPathFormTests``（T1）；
* ``ReplayTamperTests``：改纪元、删信息承载事件、伪造事件，重放必须拒绝。

两处设计取舍（必须读）：

1. **失败“引入”只放行盘外来源、盘内目的**。盘内来源、盘外目的的失败 link／rename 在本轮之前就有既有测试钉成“仍判问题”
   （``test_ci_read_audit_v4_tracker``、``test_ci_read_audit_v4_trace``），本轮不动，也不为了对称而放宽——证据里没有这个方向。
2. **经链接读取只放行“成功的只读打开”**。失败的打开没有描述符标注，内核实际探测的宿主路径没有被登记；stat／access／readlink／execve 同理。
   这些仍判“跟随式访问”。
"""

from __future__ import annotations

import copy
import os
import random
import unittest
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests import test_ci_read_audit_v4_tracker as T
from tools.official_client_capture.tests import test_ci_read_audit_wp_r8 as R8
from tools.official_client_capture.tests.test_ci_read_audit_v4_trace import AuditFixture

ROOT = T.ROOT
D = T.D
OUTSIDE_PEER = "两路径调用的另一端不在私有临时盘内："
FOLLOW = "路径的中间分量是符号链接，属于跟随式访问："
UNCONTRACTED = "未建立合同的失败调用"
EEXIST_UNKNOWN = "EEXIST 落在未知路径，与轨迹矛盾："
DESCRIPTOR = "文件描述符未绑定到私有临时盘："
UNSAFE = "symlink-path-form-unsafe"
HOST = "/host-cache"                      # 盘外共享层（夹具用的假想根）

# ---------------------------------------------------------------------------------------------------------------------
# 收割 #9 evaluation_builder 的真实事件切片（原始序号 2590～2605、3060～3072、4082；调用编号、字段取真实值）
# ---------------------------------------------------------------------------------------------------------------------

WORK_ID = "4c1b529f3d49e6ee9f68abfa4007e160d71f5991-cb73394623383b0679b57fd3482e64015e715158dcf59d17e3a506f1b46d0f5a"
WORK = "/root/docker/capture-cli/entry-gates-source-work/" + WORK_ID
EVAL = ROOT + "/eval-accept-09_xy2ft"
TREE = EVAL + "/tree-accept-fixed"
PREFIX = EVAL + "/.pycache-prefix"
MIRROR = PREFIX + TREE                                       # 副本树的字节码镜像：<前缀目录> + 树的绝对路径
SHARED = WORK + "/pycache/usr/lib/python3.12"                # 盘外共享层里的标准库缓存目录（符号链接的目标）
HOST_PYC = WORK + "/pycache" + WORK + "/test-tree/tools/official_client_capture"
STDLIB_LINK = PREFIX + "/usr/lib/python3.12"
PKG = MIRROR + "/tools/official_client_capture"


def ev(call: int, name: str, kind: str, ok: bool, errno: str | None, flags: tuple[str, ...], path: str, target: str | None,
       role: str | None, peer: str | None) -> dict[str, Any]:
    return {"path": path, "kind": kind, "name": name, "succeeded": ok, "flags": sorted(flags), "target": target, "errno": errno,
            "call": call, "role": role, "peer": peer, "epoch": 0}


# 原始序号 2590～2605（建 .pycache-prefix 与标准库缓存目录的符号链接）。
SYMLINK_WINDOW = [
    ev(14883, "openat", "write", True, None, ("O_CLOEXEC", "O_CREAT", "O_TRUNC", "O_WRONLY"), f"{TREE}/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
       f"{TREE}/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", None, None),
    ev(14883, "openat", "write", True, None, ("O_CLOEXEC", "O_CREAT", "O_TRUNC", "O_WRONLY"), f"{TREE}/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
       f"{TREE}/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", None, None),
    ev(14885, "utimensat", "write", True, None, (), f"{TREE}/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", None, None, None),
    ev(14887, "fchmodat", "write", True, None, (), f"{TREE}/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", None, None, None),
    ev(14888, "mkdirat", "write", True, None, (), PREFIX, None, None, None),
    ev(14907, "newfstatat", "missing", False, "ENOENT", (), f"{PREFIX}/usr/lib/python3.12", None, None, None),
    ev(14908, "mkdirat", "write", False, "ENOENT", (), f"{PREFIX}/usr/lib", None, None, None),
    ev(14909, "mkdirat", "write", True, None, (), f"{PREFIX}/usr", None, None, None),
    ev(14910, "mkdirat", "write", True, None, (), f"{PREFIX}/usr/lib", None, None, None),
    ev(14911, "symlinkat", "write", True, None, (), STDLIB_LINK, SHARED, None, None),
    ev(14920, "newfstatat", "stat", True, None, ("AT_SYMLINK_NOFOLLOW",), TREE, None, None, None),
    ev(14921, "newfstatat", "stat", True, None, (), TREE, None, None, None),
    ev(14923, "openat", "dir", True, None, ("O_CLOEXEC", "O_DIRECTORY", "O_NONBLOCK", "O_RDONLY"), f"{TREE}/tools/official_client_capture",
       f"{TREE}/tools/official_client_capture", None, None),
    ev(14924, "getdents64", "list", True, None, (), f"{TREE}/tools/official_client_capture", None, None, None),
    ev(14927, "mkdirat", "write", False, "ENOENT", (), f"{PKG}", None, None, None),
    ev(14928, "mkdirat", "write", False, "ENOENT", (), f"{MIRROR}/tools", None, None, None),
]
# 原始序号 3060～3072（两次 linkat 得到 EXDEV，随后回退成复制：缺失探测 → O_CREAT 创建 → utimensat → fchmodat）。
LINKAT_WINDOW = [
    ev(16029, "newfstatat", "missing", False, "ENOENT", (), f"{PKG}/claude_upgrade_candidate_capture.cpython-312.pyc", None, None, None),
    ev(16035, "openat", "write", True, None, ("O_CLOEXEC", "O_CREAT", "O_TRUNC", "O_WRONLY"), f"{PKG}/claude_upgrade_candidate_capture.cpython-312.pyc",
       f"{PKG}/claude_upgrade_candidate_capture.cpython-312.pyc", None, None),
    ev(16035, "openat", "write", True, None, ("O_CLOEXEC", "O_CREAT", "O_TRUNC", "O_WRONLY"), f"{PKG}/claude_upgrade_candidate_capture.cpython-312.pyc",
       f"{PKG}/claude_upgrade_candidate_capture.cpython-312.pyc", None, None),
    ev(16037, "utimensat", "write", True, None, (), f"{PKG}/claude_upgrade_candidate_capture.cpython-312.pyc", None, None, None),
    ev(16039, "fchmodat", "write", True, None, (), f"{PKG}/claude_upgrade_candidate_capture.cpython-312.pyc", None, None, None),
    ev(16043, "linkat", "write", False, "EXDEV", (), f"{PKG}/__init__.cpython-312.pyc", None, "destination", f"{HOST_PYC}/__init__.cpython-312.pyc"),
    ev(16044, "newfstatat", "missing", False, "ENOENT", (), f"{PKG}/__init__.cpython-312.pyc", None, None, None),
    ev(16050, "openat", "write", True, None, ("O_CLOEXEC", "O_CREAT", "O_TRUNC", "O_WRONLY"), f"{PKG}/__init__.cpython-312.pyc",
       f"{PKG}/__init__.cpython-312.pyc", None, None),
    ev(16050, "openat", "write", True, None, ("O_CLOEXEC", "O_CREAT", "O_TRUNC", "O_WRONLY"), f"{PKG}/__init__.cpython-312.pyc",
       f"{PKG}/__init__.cpython-312.pyc", None, None),
    ev(16052, "utimensat", "write", True, None, (), f"{PKG}/__init__.cpython-312.pyc", None, None, None),
    ev(16054, "fchmodat", "write", True, None, (), f"{PKG}/__init__.cpython-312.pyc", None, None, None),
    ev(16058, "linkat", "write", False, "EXDEV", (), f"{PKG}/mitm_scenario_checkpoint.cpython-312.pyc", None, "destination",
       f"{HOST_PYC}/mitm_scenario_checkpoint.cpython-312.pyc"),
    ev(16059, "newfstatat", "missing", False, "ENOENT", (), f"{PKG}/mitm_scenario_checkpoint.cpython-312.pyc", None, None, None),
]
# 原始序号 4082：经标准库缓存目录的符号链接成功只读打开 .pyc；事件的 target 是内核解析后的真实路径（盘外）。
THROUGH_READ = ev(18838, "openat", "read", True, None, ("O_CLOEXEC", "O_RDONLY"), f"{STDLIB_LINK}/__future__.cpython-312.pyc",
                  f"{SHARED}/__future__.cpython-312.pyc", None, None)


def by_call(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """相邻且调用编号相同的事件归为一组（openat 的两条事件）。"""
    groups: list[list[dict[str, Any]]] = []
    for event in events:
        if groups and groups[-1][0]["call"] == event["call"]:
            groups[-1].append(event)
        else:
            groups.append([event])
    return groups


def prelude_groups() -> list[list[dict[str, Any]]]:
    """切片开头之前早已存在的目录（夹具补齐，调用编号 1～5，小于任何切片事件）。"""
    b = T.Builder()
    return [b.mkdir(path) for path in (EVAL, TREE, TREE + "/docs", TREE + "/tools", TREE + "/tools/official_client_capture")]


def bridge_groups() -> list[list[dict[str, Any]]]:
    """两个窗口之间，镜像目录层级（``.pycache-prefix`` + 树的绝对路径 + ``tools/official_client_capture``）逐级建好（夹具补齐）。"""
    groups = []
    current = PREFIX
    for number, component in enumerate(TREE.strip("/").split("/") + ["tools", "official_client_capture"]):
        current += "/" + component
        groups.append([ev(15000 + number, "mkdir", "write", True, None, (), current, None, None, None)])
    return groups


def real_groups(*, with_read: bool = True) -> list[list[dict[str, Any]]]:
    groups = prelude_groups() + by_call(copy.deepcopy(SYMLINK_WINDOW)) + bridge_groups() + by_call(copy.deepcopy(LINKAT_WINDOW))
    if with_read:
        groups.append([copy.deepcopy(THROUGH_READ)])
    return groups


def feed(*groups: Any, tracker_class: Any = runtime.OutputTrackerV4) -> tuple[Any, list[bool]]:
    tracker = tracker_class(T.CONTRACT)
    changed = []
    for group in groups:
        for event in ([group] if isinstance(group, dict) else group):
            changed.append(tracker.observe_event(copy.deepcopy(event)))
    return tracker, changed


def problems_of(*groups: Any) -> list[str]:
    return feed(*groups)[0].report()["problems"]


def legacy(tracker_class: Any = runtime.OutputTrackerV4) -> Any:
    """本轮之前的行为：失败的“引入”一律判“另一端不在私有临时盘内”；经链接的读取一律判跟随式访问。"""
    def refuse_outside_peer(self_: Any, event: dict[str, Any]) -> None:
        self_._problem(OUTSIDE_PEER, event["path"])

    first = mock.patch.object(tracker_class, "_two_path_failed_import", refuse_outside_peer)
    second = mock.patch.object(tracker_class, "_own_link_foreign_read", lambda self_, event: False)
    return _Both(first, second)


class _Both:
    def __init__(self, *patchers: Any) -> None:
        self.patchers = patchers

    def __enter__(self) -> "_Both":
        for patcher in self.patchers:
            patcher.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        for patcher in reversed(self.patchers):
            patcher.__exit__(*exc)


# =====================================================================================================================
# 真实证据
# =====================================================================================================================


class RealEvidenceTests(unittest.TestCase):
    """证据切片整段走完。"""

    def test_the_fixture_carries_the_real_events(self) -> None:
        """夹具自检：30 条真实事件，两条 linkat 是盘内目的端（EXDEV，对端在盘外），一条符号链接创建，一条经链接的读取（target 在盘外）。"""
        self.assertEqual((len(SYMLINK_WINDOW), len(LINKAT_WINDOW)), (16, 13))
        linkats = [e for e in LINKAT_WINDOW if e["name"] == "linkat"]
        self.assertEqual([(e["role"], e["errno"], e["succeeded"], e["peer"].startswith(WORK)) for e in linkats],
                         [("destination", "EXDEV", False, True)] * 2)
        symlinks = [e for e in SYMLINK_WINDOW if e["name"] == "symlinkat"]
        self.assertEqual([(e["path"], e["target"]) for e in symlinks], [(STDLIB_LINK, SHARED)])
        self.assertTrue(THROUGH_READ["target"].startswith(SHARED + "/") and not THROUGH_READ["target"].startswith(ROOT))
        calls = [e["call"] for g in real_groups() for e in g]
        self.assertEqual(calls, sorted(calls), "调用编号单调不减（重放端要求严格递增的分组）")

    def test_the_whole_real_sequence_is_clean_after_the_fix(self) -> None:
        tracker, _ = feed(*real_groups())
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(tracker.awaiting, {})
        self.assertTrue(tracker.is_symlink(STDLIB_LINK), "符号链接对象本身仍是本轮创建的对象")
        self.assertIn(f"{PKG}/__init__.cpython-312.pyc", tracker.names, "回退复制创建的名字照常已证明")

    def test_before_the_fix_the_same_sequence_reproduces_exactly_the_two_reported_classes(self) -> None:
        with legacy():
            problems = problems_of(*real_groups())
        self.assertEqual(sorted(problems), sorted([
            OUTSIDE_PEER + f"{PKG}/__init__.cpython-312.pyc", OUTSIDE_PEER + f"{PKG}/mitm_scenario_checkpoint.cpython-312.pyc",
            FOLLOW + "openat:" + THROUGH_READ["path"]]))

    def test_each_class_is_independent(self) -> None:
        """只关一个放行：另一类问题不受影响，只剩各自那一类。"""
        with mock.patch.object(runtime.OutputTrackerV4, "_own_link_foreign_read", lambda self_, event: False):
            self.assertEqual(problems_of(*real_groups()), [FOLLOW + "openat:" + THROUGH_READ["path"]])
        with legacy():
            self.assertEqual(len(problems_of(*real_groups(with_read=False))), 2)

    def test_the_new_allowances_do_not_touch_the_name_table(self) -> None:
        """放行的事件既不改名字表、子项计数、版本号，也不把盘外路径记成本单元创建的对象；返回 False（不推进变更纪元）。"""
        groups = real_groups()
        tracker = runtime.OutputTrackerV4(T.CONTRACT)
        checked = 0
        for event in (event for group in groups for event in group):
            relevant = event["name"] == "linkat" or event is groups[-1][0] or event["path"] == THROUGH_READ["path"]
            if relevant:
                before = (dict(tracker.names), dict(tracker.entries), dict(tracker.versions), dict(tracker.link_text), dict(tracker.awaiting),
                          set(tracker.problems))
                self.assertFalse(tracker.observe_event(copy.deepcopy(event)), f"{event['name']}：不改状态")
                self.assertEqual(before, (dict(tracker.names), dict(tracker.entries), dict(tracker.versions), dict(tracker.link_text),
                                          dict(tracker.awaiting), set(tracker.problems)))
                checked += 1
            else:
                tracker.observe_event(copy.deepcopy(event))
        self.assertEqual(checked, 3)
        self.assertFalse(any(path.startswith(WORK) or path.startswith(HOST) for path in tracker.names), "盘外路径不是本单元创建的对象")

    def test_the_sequence_survives_a_full_replay_and_the_epoch_rule_is_unchanged(self) -> None:
        document = R8.document_of(*real_groups())
        self.assertEqual(document["outputs"]["problems"], [])
        replayed, _seen = runtime._replay_events_v4(document, T.CONTRACT)
        self.assertEqual(replayed.report(), document["outputs"])
        events = document["output_events"]
        advancing = [e["name"] for i, e in enumerate(events[:-1]) if events[i + 1]["epoch"] != e["epoch"]]
        self.assertNotIn("linkat", advancing, "失败的 linkat 不推进变更纪元")
        self.assertEqual(events[-1]["epoch"], events[-2]["epoch"], "经链接的读取不推进变更纪元（它前一条是不改状态的探测）")

    def test_the_link_object_can_still_be_unlinked_afterwards(self) -> None:
        b = T.Builder()
        b.call = 99999
        tracker, _ = feed(*real_groups(), b.unlink(STDLIB_LINK, name="unlinkat"))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertFalse(tracker.has_symlinks())


# =====================================================================================================================
# T2.1 失败的“引入”
# =====================================================================================================================

TWO_PATH_NAMES = ("link", "linkat", "rename", "renameat", "renameat2")
OUTSIDE_SOURCE = HOST + "/pycache/a.pyc"


def failed_import(b: T.Builder, name: str, destination: str, errno: str, *, peer: str | None = OUTSIDE_SOURCE) -> list[dict[str, Any]]:
    """盘外来源、盘内目的的失败两路径调用：解析器只为盘内一端产生一条事件（目的端，peer 是盘外来源）。"""
    return [b.event(name, destination, kind="write", ok=False, errno=errno, role="destination", peer=peer)]


class FailedImportRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.b = T.Builder()

    # ------------------------------------------------------------------ 放行的情形

    def test_exdev_import_is_a_legal_failure_for_every_two_path_call(self) -> None:
        for name in TWO_PATH_NAMES:
            with self.subTest(name=name):
                b = T.Builder()
                tracker, changed = feed(b.mkdir(D), failed_import(b, name, D + "/new.pyc", "EXDEV"))
                self.assertEqual(tracker.report()["problems"], [])
                self.assertFalse(changed[-1], "失败的调用不改状态")
                self.assertNotIn(D + "/new.pyc", tracker.names)
                self.assertIn(D + "/new.pyc", tracker.covered, "盘内一端仍登记为覆盖路径")

    def test_enoent_and_enotdir_are_legal_with_or_without_a_known_parent(self) -> None:
        """来源（盘外，宿主事实）不存在，或目的的父目录不存在：与盘内两端时一样，不判（盘外来源没有名字表可对照）。"""
        for errno in ("ENOENT", "ENOTDIR"):
            for known_parent in (True, False):
                with self.subTest(errno=errno, known_parent=known_parent):
                    b = T.Builder()
                    groups = [b.mkdir(D)] if known_parent else []
                    self.assertEqual(problems_of(*groups, failed_import(b, "link", D + "/n", errno)), [])

    def test_eexist_on_a_known_destination_is_legal(self) -> None:
        b = self.b
        self.assertEqual(problems_of(b.mkdir(D), b.create(D + "/n"), failed_import(b, "link", D + "/n", "EEXIST")), [])

    def test_the_fallback_copy_after_the_failed_import_is_ordinary(self) -> None:
        """EXDEV 之后的回退复制（缺失探测 → 创建 → 改时间 → 改权限）全是既有规则，整段零问题，目的名字随后已证明。"""
        b = self.b
        d = D + "/n.pyc"
        tracker, _ = feed(b.mkdir(D), failed_import(b, "linkat", d, "EXDEV"), b.probe(d),
                          b.create(d, extra=("O_TRUNC", "O_CLOEXEC")) * 1, [b.event("utimensat", d)], [b.event("fchmodat", d)], b.read(d))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertIn(d, tracker.names)

    # ------------------------------------------------------------------ 仍判问题的情形

    def test_eexist_on_an_unknown_destination_is_still_a_problem(self) -> None:
        b = self.b
        for known_parent in (True, False):
            with self.subTest(known_parent=known_parent):
                b = T.Builder()
                groups = [b.mkdir(D)] if known_parent else []
                self.assertEqual(problems_of(*groups, failed_import(b, "link", D + "/n", "EEXIST")), [EEXIST_UNKNOWN + D + "/n"])

    def test_other_errnos_keep_the_original_verdict(self) -> None:
        for name in ("link", "renameat2"):
            for errno in ("EACCES", "EPERM", "EROFS", "EMLINK", "ENOSPC", "EISDIR", "ENOTEMPTY", "EBUSY", "EINVAL", "EIO", "ELOOP"):
                with self.subTest(name=name, errno=errno):
                    b = T.Builder()
                    self.assertEqual(problems_of(b.mkdir(D), failed_import(b, name, D + "/n", errno)),
                                     [f"{UNCONTRACTED}（{errno}）：{name}:{D}/n"])

    def test_a_successful_call_with_an_outside_peer_is_still_a_problem(self) -> None:
        """成功的“引入”（把宿主文件带进私有盘）与成功的“送出”都仍判问题，两个角色、每个调用名。"""
        for name in TWO_PATH_NAMES:
            for role, path in (("destination", D + "/n"), ("source", D + "/a")):
                with self.subTest(name=name, role=role):
                    b = T.Builder()
                    event = b.event(name, path, kind="write", role=role, peer=OUTSIDE_SOURCE)
                    self.assertEqual(problems_of(b.mkdir(D), b.create(D + "/a"), [event]), [OUTSIDE_PEER + path])

    def test_an_in_root_source_with_an_outside_destination_is_still_a_problem(self) -> None:
        """失败的“送出”（盘内来源、盘外目的）不在本轮放行范围：既有测试钉着它，证据里也没有这个方向。任何错误码都仍判。"""
        for errno in ("EXDEV", "EEXIST", "ENOENT", "EACCES"):
            with self.subTest(errno=errno):
                b = T.Builder()
                event = b.event("rename", D + "/a", kind="write", ok=False, errno=errno, role="source", peer="/host/x")
                self.assertEqual(problems_of(b.mkdir(D), b.create(D + "/a"), [event]), [OUTSIDE_PEER + D + "/a"])

    def test_an_unresolvable_other_end_is_still_a_problem(self) -> None:
        for role in ("destination", "source"):
            with self.subTest(role=role):
                b = T.Builder()
                event = b.event("linkat", D + "/n", kind="write", ok=False, errno="EXDEV", role=role, peer=None)
                self.assertEqual(problems_of(b.mkdir(D), [event]), [OUTSIDE_PEER + D + "/n"])

    def test_exdev_between_two_in_root_ends_has_no_explanation(self) -> None:
        """两端都在私有盘内时 EXDEV 没有任何解释（同一个 tmpfs），仍是“未建立合同的失败调用”。"""
        b = self.b
        problems = problems_of(b.mkdir(D), b.create(D + "/a"), b.link(D + "/a", D + "/n", ok=False, errno="EXDEV"))
        self.assertEqual(problems, [f"{UNCONTRACTED}（EXDEV）：link:{D}/a"])

    def test_the_in_root_end_is_judged_like_any_other_event(self) -> None:
        """目的名字落在符号链接之下：先被“跟随式访问”拦下，失败的引入不能借此绕过（路径中间分量是链接的检查排在两路径规则之前）。"""
        b = self.b
        self.assertEqual(problems_of(b.mkdir(D), b.symlink(D + "/l", "/host/x"), failed_import(b, "link", D + "/l/n", "EXDEV")),
                         [FOLLOW + "link:" + D + "/l/n"])

    def test_dedup_key_keeps_every_such_event(self) -> None:
        b = self.b
        tracker = feed(b.mkdir(D))[0]
        self.assertIsNone(tracker.dedup_key(failed_import(b, "linkat", D + "/n", "EXDEV")[0]))


# =====================================================================================================================
# T2.2 经自己的符号链接读盘外宿主文件
# =====================================================================================================================

LINK = D + "/l"
LIB = HOST + "/lib"


def world(b: T.Builder, text: str = LIB, link: str = LINK) -> list[list[dict[str, Any]]]:
    return [b.mkdir(D), b.symlink(link, text)]


def through(b: T.Builder, rel: str = "x.pyc", *, link: str = LINK, target: str | None = None, name: str = "openat", kind: str = "read",
            ok: bool = True, errno: str | None = None, flags: tuple[str, ...] = ("O_CLOEXEC", "O_RDONLY"), base: str = LIB) -> list[dict[str, Any]]:
    """经链接打开 ``link/rel``：默认描述符的真实路径是 ``base/rel``（链接文本＋剩余分量）。"""
    if target is None and ok:
        target = base + "/" + rel
    return [b.event(name, link + "/" + rel, kind=kind, ok=ok, errno=errno, flags=flags, target=target if ok else None)]


class OwnLinkForeignReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.b = T.Builder()

    def assertFollow(self, problems: list[str], path: str, name: str = "openat") -> None:
        self.assertEqual(problems, [FOLLOW + name + ":" + path])

    # ------------------------------------------------------------------ 放行

    def test_the_read_through_the_own_link_is_allowed(self) -> None:
        b = self.b
        tracker, changed = feed(*world(b), through(b))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertFalse(changed[-1])
        self.assertEqual(tracker.awaiting, {})
        self.assertNotIn(LINK + "/x.pyc", tracker.names, "不记名字")
        self.assertIn(LINK + "/x.pyc", tracker.covered, "路径仍是覆盖路径（事件照常保留，访问集合才闭合）")

    def test_every_open_call_and_a_deep_remainder_are_allowed(self) -> None:
        for name in ("open", "openat", "openat2"):
            for rel in ("x.pyc", "a/b/c.pyc"):
                with self.subTest(name=name, rel=rel):
                    b = T.Builder()
                    self.assertEqual(problems_of(*world(b), through(b, rel, name=name)), [])

    def test_extra_read_only_flags_are_fine(self) -> None:
        b = self.b
        flags = ("O_CLOEXEC", "O_RDONLY", "O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_LARGEFILE")
        self.assertEqual(problems_of(*world(b), through(b, flags=flags)), [])

    def test_a_relative_link_text_is_resolved_against_the_links_directory(self) -> None:
        """相对链接文本按链接所在目录解析：``D/l -> ../../out`` 的真实目录是 ``<根的上两级>/out``。"""
        b = self.b
        out = "/var/tmp/read-audit-v4/run/private/out"
        self.assertEqual(problems_of(*world(b, text="../../out"), through(b, base=out)), [])
        # 文本里的 .. 把落点带回盘内：真实路径在盘内，不放行（链接目标在盘内）。
        b = T.Builder()
        inside = ROOT + "/x"
        self.assertFollow(problems_of(*world(b, text="../x"), through(b, base=inside)), LINK + "/x.pyc")

    def test_two_reads_through_two_links_are_each_matched_against_their_own_link(self) -> None:
        b = self.b
        other = D + "/m"
        groups = [*world(b), b.symlink(other, HOST + "/other"), through(b, "a.pyc"), through(b, "b.pyc", link=other, base=HOST + "/other")]
        self.assertEqual(problems_of(*groups), [])
        self.assertEqual(len(problems_of(*groups[:3], through(b, "b.pyc", link=other, base=LIB))), 1, "链接 m 的读取用链接 l 的目标比对不上")

    # ------------------------------------------------------------------ 保持原判定：目标比对

    def test_a_target_that_does_not_equal_the_link_text_plus_remainder_is_refused(self) -> None:
        b = self.b
        path = LINK + "/x.pyc"
        wrong_targets = {
            "别的目录": HOST + "/other/x.pyc", "别的文件名": LIB + "/y.pyc", "多一段": LIB + "/sub/x.pyc", "少一段": LIB,
            "仅前缀相同": LIB + "x.pyc", "链接文本本身": LIB, "多出结尾斜杠": LIB + "/x.pyc/", "大小写不同": LIB + "/X.pyc",
            "根": "/", "盘外的无关路径": "/etc/passwd",
        }
        for label, target in wrong_targets.items():
            with self.subTest(label=label):
                self.assertFollow(problems_of(*world(T.Builder()), through(T.Builder(), target=target)), path)

    def test_a_missing_or_malformed_target_is_refused(self) -> None:
        for label, target in {"没有标注": None, "相对路径": "lib/x.pyc", "未规范化": LIB + "/./x.pyc", "含 ..": LIB + "/../lib/x.pyc",
                              "双斜杠": HOST + "//lib/x.pyc", "空串": ""}.items():
            with self.subTest(label=label):
                b = T.Builder()
                event = through(b)
                event[0]["target"] = target
                self.assertEqual(len(problems_of(*world(b), event)), 1)

    def test_a_target_inside_the_private_root_is_refused(self) -> None:
        """真实路径落在盘内（链接文本指向盘内，或比对被伪造）：既不是盘外宿主读取，原判定不变。"""
        b = self.b
        self.assertFollow(problems_of(*world(b, text=D + "/real"), through(b, base=D + "/real")), LINK + "/x.pyc")
        b = T.Builder()
        self.assertFollow(problems_of(*world(b), through(b, target=D + "/real/x.pyc")), LINK + "/x.pyc")

    # ------------------------------------------------------------------ 保持原判定：调用形态

    def test_write_opens_are_refused(self) -> None:
        for flags in (("O_WRONLY",), ("O_RDWR",), ("O_WRONLY", "O_CREAT"), ("O_RDONLY", "O_CREAT"), ("O_RDONLY", "O_TRUNC"),
                      ("O_RDONLY", "O_APPEND"), ("O_RDONLY", "O_TMPFILE"), ("O_RDONLY", "O_PATH")):
            for kind in ("write", "read"):
                with self.subTest(flags=flags, kind=kind):
                    b = T.Builder()
                    self.assertFollow(problems_of(*world(b), through(b, flags=flags, kind=kind)), LINK + "/x.pyc")

    def test_creat_is_refused(self) -> None:
        b = self.b
        self.assertFollow(problems_of(*world(b), through(b, name="creat", flags=())), LINK + "/x.pyc", "creat")

    def test_failed_opens_are_refused(self) -> None:
        """失败的打开没有描述符标注：内核实际探测的宿主路径没有被登记，必须仍判问题。"""
        for errno, kind in (("ENOENT", "missing"), ("ENOTDIR", "missing"), ("EACCES", "stat"), ("ELOOP", "stat")):
            with self.subTest(errno=errno):
                b = T.Builder()
                self.assertFollow(problems_of(*world(b), through(b, ok=False, errno=errno, kind=kind)), LINK + "/x.pyc")

    def test_a_forged_failed_read_that_still_carries_a_matching_target_is_refused(self) -> None:
        """伪造：kind 仍是 read、真实路径标注也对得上，却是失败的调用（带错误码）。失败的调用不可能有描述符标注，必须仍判问题。"""
        b = self.b
        for errno in ("ENOENT", "EACCES"):
            with self.subTest(errno=errno):
                event = through(b)
                event[0].update(succeeded=False, errno=errno)
                self.assertFollow(problems_of(*world(T.Builder()), event), LINK + "/x.pyc")

    def test_directory_opens_and_non_open_calls_are_refused(self) -> None:
        b = self.b
        path = LINK + "/x.pyc"
        cases = {
            "O_DIRECTORY 打开（kind=dir）": through(b, kind="dir", flags=("O_DIRECTORY", "O_RDONLY")),
            "newfstatat": [b.event("newfstatat", path, kind="stat", target=LIB + "/x.pyc")],
            "statx": [b.event("statx", path, kind="stat", target=LIB + "/x.pyc")],
            "faccessat": [b.event("faccessat", path, kind="stat", target=LIB + "/x.pyc")],
            "readlinkat": [b.event("readlinkat", path, kind="stat", target=LIB + "/x.pyc")],
            "execve": [b.event("execve", path, kind="exec", target=LIB + "/x.pyc")],
            "chdir": [b.event("chdir", path, kind="dir", target=LIB + "/x.pyc")],
        }
        for label, group in cases.items():
            with self.subTest(label=label):
                problems = problems_of(*world(T.Builder()), group)
                self.assertEqual(problems, [FOLLOW + group[0]["name"] + ":" + path], problems)

    def test_the_link_name_itself_is_still_judged_by_the_name_rules(self) -> None:
        """打开链接名字本身（最后一个分量）会再跟随一层：不是本放行的对象，保持“跟随符号链接的访问”。"""
        b = self.b
        problems = problems_of(*world(b), [b.event("openat", LINK, kind="read", flags=("O_RDONLY",), target=LIB)])
        self.assertEqual(len(problems), 1)
        self.assertIn("跟随符号链接的访问", problems[0])

    # ------------------------------------------------------------------ 保持原判定：链接对象

    def test_two_link_ancestors_are_refused(self) -> None:
        """路径的真祖先里有两个符号链接名字：只有“恰好一个”才放行。名字表无法用事件造出这种形状，直接注入状态。"""
        b = self.b
        tracker, _ = feed(*world(b), b.mkdir(D + "/m"), b.symlink(D + "/m/n", HOST + "/n"))
        deep = D + "/m/n/x.pyc"
        self.assertTrue(tracker.is_symlink(D + "/m/n"))
        # 把 D/m 也变成符号链接名字（注入）：D/m/n/x.pyc 的真祖先里有 D/m 与 D/m/n 两个链接。
        obj = tracker.names[D + "/m"]
        tracker.link_text[obj] = HOST + "/m"
        # 真实路径分别等于“最近的链接（m/n）文本＋剩余分量”与“外层链接（m）文本＋剩余分量”：两种比对都不放行，因为链接祖先有两个。
        for label, target in {"最近的链接": HOST + "/n/x.pyc", "外层的链接": HOST + "/m/n/x.pyc"}.items():
            with self.subTest(label=label):
                event = b.event("openat", deep, kind="read", flags=("O_RDONLY",), target=target)
                self.assertFalse(tracker._own_link_foreign_read(event))
        tracker.observe_event(b.event("openat", deep, kind="read", flags=("O_RDONLY",), target=HOST + "/n/x.pyc"))
        self.assertEqual(tracker.report()["problems"], [FOLLOW + "openat:" + deep])

    def test_a_chain_of_links_whose_first_hop_stays_inside_is_refused(self) -> None:
        """第一个链接指向盘内的目录，目录里的第二个链接指向盘外：两层。真实路径在盘外，却不等于“第一个链接的文本＋剩余分量”。"""
        b = self.b
        groups = [b.mkdir(D), b.mkdir(D + "/real"), b.symlink(D + "/real/n", LIB), b.symlink(D + "/a", D + "/real")]
        self.assertFollow(problems_of(*groups, through(b, "n/x.pyc", link=D + "/a", base=HOST + "/nope")), D + "/a/n/x.pyc")
        self.assertFollow(problems_of(*groups, through(b, "n/x.pyc", link=D + "/a", target=LIB + "/x.pyc")), D + "/a/n/x.pyc")

    def test_a_link_this_run_did_not_create_is_refused(self) -> None:
        """没有创建事件的“链接”：跟踪器不认识它，路径只是盘内一个未证明的名字，描述符落在盘外——原有的“描述符未绑定”检查照旧判问题。"""
        b = self.b
        self.assertEqual(problems_of(b.mkdir(D), through(b)), [DESCRIPTOR + LINK + "/x.pyc"])

    def test_a_link_that_was_removed_or_failed_to_be_created_is_refused(self) -> None:
        b = self.b
        removed = problems_of(*world(b), b.unlink(LINK), through(b))
        self.assertTrue(any(DESCRIPTOR in p for p in removed), removed)
        b = T.Builder()
        failed = problems_of(b.mkdir(D), b.symlink(LINK, LIB, ok=False, errno="EACCES"), through(b))
        self.assertTrue(any(DESCRIPTOR in p for p in failed), failed)

    def test_a_replaced_link_is_matched_against_its_current_text(self) -> None:
        b = self.b
        groups = [*world(b), through(b), b.unlink(LINK), b.symlink(LINK, HOST + "/new")]
        self.assertEqual(problems_of(*groups, through(b, base=HOST + "/new")), [])
        self.assertFollow(problems_of(*groups, through(b)), LINK + "/x.pyc")

    def test_v3_tracker_is_untouched(self) -> None:
        """v3 跟踪器没有这条放行（也没有失败引入放行）：同样的输入仍判问题。"""
        tracker = runtime.OutputTracker(T.CONTRACT)
        tracker.observe(D, "write", "mkdir", "", True)
        tracker.observe(LINK + "/x.pyc", "read", "openat", "O_RDONLY", True, LIB + "/x.pyc")
        self.assertTrue(tracker.problems)


# =====================================================================================================================
# 去重键与随机对拍
# =====================================================================================================================


def link_stress(seed: int, steps: int = 140, clean: bool = False) -> list[list[dict[str, Any]]]:
    """随机历史：符号链接的创建／删除／重建（文本在几种之间切换）、经链接的读取、失败的“引入”（几种错误码）、普通探测与创建；再混入重复事件。

    * ``clean=False``：什么都可能发生——文本指向盘内、读取的真实路径对不上、读取的链接已被删除、EEXIST 落在未知名字、对已创建名字的缺失探测、
      对更早调用的原样重复（陈旧事件）……流里有大量问题，用来检验“去重不吞问题”“同键同判定”；
    * ``clean=True``：只做合法的事——链接只建在没有的名字上、只读还活着的链接且真实路径恰是“链接文本＋剩余分量”、EEXIST 只落在已创建的名字上、
      重复只取自上次改名字空间以来的读取／失败类调用——整条流零问题，用来检验“去重后的干净流能走完整重放”。
    """
    rng = random.Random(77000 + seed)
    b = T.Builder()
    links = [D + "/l1", D + "/l2", D + "/e/l3"]
    texts = [HOST + "/a", HOST + "/b", "../../out"] + ([] if clean else [D + "/e", "../x"])
    groups: list[list[dict[str, Any]]] = [b.mkdir(D), b.mkdir(D + "/e")]
    live: dict[str, str] = {}
    created: set[str] = set()
    recent: list[list[dict[str, Any]]] = []          # 自上次改名字空间以来的调用（clean 模式的重复来源）

    def expected(link: str, rel: str) -> str:
        text = live[link]
        base = text if text.startswith("/") else os.path.normpath(os.path.join(os.path.dirname(link), text))
        return base + "/" + rel

    def add(group: list[dict[str, Any]], *, changes: bool = False) -> None:
        groups.append(group)
        if changes:
            recent.clear()
        else:
            recent.append(group)

    names = [D + "/n1", D + "/n2", D + "/e/n3"]
    for _ in range(steps):
        roll = rng.random()
        link = rng.choice(links)
        rel = rng.choice(["x.pyc", "y.pyc", "s/z.pyc"])
        if roll < 0.12:
            if not clean or link not in live:
                text = rng.choice(texts)
                add(b.symlink(link, text), changes=True)
                live.setdefault(link, text)
        elif roll < 0.18 and live:
            victim = rng.choice(sorted(live))
            add(b.unlink(victim), changes=True)
            live.pop(victim, None)
        elif roll < 0.55:
            if clean and link not in live:
                continue
            if link in live and rng.random() < (1.0 if clean else 0.7):
                target = expected(link, rel) if clean or rng.random() < 0.75 else rng.choice([HOST + "/zz/" + rel, D + "/e/" + rel, None])
            else:
                target = HOST + "/" + rel
            add([b.event("openat", link + "/" + rel, kind="read", flags=("O_CLOEXEC", "O_RDONLY"), target=target)])
        elif roll < 0.7:
            dest = rng.choice(names)
            errno = rng.choice(["EXDEV", "EXDEV", "ENOENT", "ENOTDIR"] if clean else ["EXDEV", "EXDEV", "EEXIST", "ENOENT", "EACCES"])
            if clean and rng.random() < 0.2 and created:
                dest, errno = rng.choice(sorted(created)), "EEXIST"
            add(failed_import(b, rng.choice(["link", "linkat", "rename"]), dest, errno))
        elif roll < 0.78:
            name = rng.choice(names)
            if not clean or name not in created:
                add(b.create(name), changes=True)
                created.add(name)
        elif roll < 0.9:
            name = rng.choice(names)
            if not clean or name not in created:
                add(b.probe(name))
        else:
            add(b.stat(rng.choice([D, D + "/e"])))
        if rng.random() < 0.3:
            pool = recent if clean else groups
            for _ in range(rng.randint(1, 3) if pool else 0):
                groups.append(copy.deepcopy(rng.choice(pool)))
    return groups


class KeyAndDedupTests(R8.DedupHarness):
    """放行结论进去重键；同键同判定；去重后的流与全量流结论相同；干净的流通过完整重放，带问题的被拒。"""

    def setUp(self) -> None:
        super().setUp()
        self.b = T.Builder()

    def test_the_allowance_is_part_of_the_key_and_other_keys_are_unchanged(self) -> None:
        b = self.b
        tracker = feed(*world(b))[0]
        allowed = through(b)[0]
        refused = through(b, target=HOST + "/other/x.pyc")[0]
        self.assertEqual(tracker.dedup_key(dict(allowed))[-1], "own-link-foreign-read")
        self.assertNotEqual(tracker.dedup_key(dict(allowed)), tracker.dedup_key(dict(refused)))
        plain = feed(b.mkdir(D))[0]
        self.assertEqual(len(plain.dedup_key(b.read(D)[0])), len(tracker.dedup_key(b.read(D)[0])),
                         "没有被放行的事件，键的形状与以前相同（不追加标记）")

    def test_replacing_the_link_changes_the_key_even_though_the_name_version_does_not(self) -> None:
        """放行取决于链接对象的文本；链接被删除重建成别的文本后，事件本身与它路径的版本号都没变，所以放行结论必须在键里。"""
        b = self.b
        before = feed(*world(b))[0]
        after = feed(*world(b), b.unlink(LINK), b.symlink(LINK, HOST + "/new"))[0]
        event = through(b)[0]
        key_before, key_after = before.dedup_key(dict(event)), after.dedup_key(dict(event))
        self.assertEqual(key_before[:-1], key_after, "标记之外的键内容完全相同——正是需要这一位的原因")
        self.assertNotEqual(key_before, key_after)
        self.assertEqual(before.dedup_key(dict(event))[-1], "own-link-foreign-read")

    def test_events_with_equal_keys_always_have_equal_outcomes(self) -> None:
        repeats = allowed = refused = 0
        for seed in range(60):
            tracker = R8.SpyTracker(T.CONTRACT)
            by_key: dict[tuple, tuple] = {}
            for event in R8.renumber(link_stress(seed)):
                key = tracker.dedup_key(dict(event))
                outcome = tracker.outcome_of(dict(event))
                if key is None:
                    continue
                if key and key[-1] == "own-link-foreign-read":
                    allowed += 1
                elif event["kind"] == "read" and any(FOLLOW in text for text in outcome[1]):
                    refused += 1
                if key in by_key:
                    repeats += 1
                    if by_key[key] != outcome:
                        self.fail(f"seed={seed}：键相同、观察结果不同\n事件 {event}\n先前 {by_key[key]}\n现在 {outcome}")
                else:
                    by_key[key] = outcome
        self.assertGreater(repeats, 1000, "随机流必须真的产生大量同键事件")
        self.assertGreater(allowed, 100, "随机流必须真的出现被放行的读取")
        self.assertGreater(refused, 100, "随机流必须真的出现被拒绝的读取（目标对不上、链接换了文本……）")

    def test_deduplicated_streams_have_the_same_verdict_and_replay_agrees(self) -> None:
        dropped = clean = bad = 0
        for seed in range(60):
            stream = [self.translate(event) for event in R8.renumber(link_stress(seed, clean=seed % 2 == 0))]
            reference = self.reference(stream)
            parser = self.feed(stream)
            kept = parser.output_tracker
            context = f"seed={seed}"
            self.assertEqual(kept.report(), reference.report(), context)
            self.assertEqual((kept.names, kept.entries, kept.awaiting, kept.link_text),
                             (reference.names, reference.entries, reference.awaiting, reference.link_text), context)
            dropped += len(stream) - len(parser.output_events)
            self.assertEqual(self.reference(parser.output_events).report(), reference.report(), context + "：只重放保留事件得到不同的结论")
            runtime_part = {"output_events": parser.output_events, "outputs": kept.report()}
            if reference.report()["problems"]:
                bad += 1
                with self.assertRaises(runtime.RuntimeContractError, msg=context):
                    runtime._replay_events_v4(runtime_part, self.contract)
            else:
                clean += 1
                replayed, _seen = runtime._replay_events_v4(runtime_part, self.contract)
                self.assertEqual(replayed.names, reference.names, context)
        self.assertGreater(dropped, 500)
        self.assertGreater(bad, 5)
        self.assertGreater(clean, 3, "必须有干净的流，才能证明放行后的流能走完整重放")

    def test_a_key_without_the_allowance_bit_is_caught_by_the_comparison(self) -> None:
        """对拍有牙：去掉键末尾的放行标记，同一份随机流里立刻出现“同键不同结论”。"""
        original = runtime.OutputTrackerV4.dedup_key

        def without_bit(self_: Any, event: dict[str, Any]) -> tuple | None:
            key = original(self_, event)
            return key[:-1] if key and key[-1] == "own-link-foreign-read" else key

        differing = 0
        with mock.patch.object(runtime.OutputTrackerV4, "dedup_key", without_bit):
            for seed in range(60):
                tracker = R8.SpyTracker(T.CONTRACT)
                by_key: dict[tuple, tuple] = {}
                for event in R8.renumber(link_stress(seed)):
                    key = tracker.dedup_key(dict(event))
                    outcome = tracker.outcome_of(dict(event))
                    if key is None:
                        continue
                    if key in by_key and by_key[key] != outcome:
                        differing += 1
                    by_key.setdefault(key, outcome)
        self.assertGreater(differing, 0)


# =====================================================================================================================
# 端到端：strace 行 → 解析器 → 文档 → 重放
# =====================================================================================================================


class E2E(AuditFixture):
    """端到端夹具：本类没有 test_ 方法。"""

    def host_findings(self, document: dict[str, Any]) -> dict[str, list[str]]:
        report = self.audit(document)
        self.assertEqual([f for f in report["undeclared"] if f["root"] == "runtime"], [], "不应有输出归属（重放）方面的 finding")
        return {f["path"]: f["kinds"] for f in report["undeclared"] if f["root"] == "host"}

    def accesses(self, document: dict[str, Any]) -> dict[str, list[str]]:
        return dict(document["accesses"])

    def host_document(self, lines: list[str], paths: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """带宿主快照的 v4 文档：``paths`` 里的每个真实文件取现场 v3 快照作为登记条目，结束凭证按真实合同写好（与 R4 同构）。"""
        entries = [ra.host_snapshot_v3(str(path)) for path in paths]
        document = ra.filter_stream(lines, ["/"], strict=True, runtime_context=self.context, trace_contract="v4")
        extra: dict[str, Any] = {"host_entries_sha256": runtime.host_binding_digest(runtime._read_audit(), entries)}
        try:
            extra["host_path_modes"] = ra.host_path_modes(document, entries)
        except ra.AuditError:
            pass
        return self.completed(document, **extra), entries

    def full_report(self, lines: list[str], paths: list[str]) -> dict[str, Any]:
        document, entries = self.host_document(lines, paths)
        return ra.strict_audit_reads(document, [self.entry, *entries], repo_root="/repo", data_root=None)

    def host_file(self, directory: str, name: str) -> str:
        """盘外（私有根之外）的真实宿主文件，供快照登记。"""
        folder = self.base / directory
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(b"cache")
        return str(folder / name)

    def assertReplays(self, document: dict[str, Any]) -> None:
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])
        self.assertEqual(document["coverage"]["unresolved"], 0, document["coverage"]["unresolved_reasons"])
        self.assertIsNotNone(runtime.replay_runtime(document, [self.entry]))

    def import_line(self, name: str, source: str, destination: str, result: str = "-1 EXDEV (Invalid cross-device link)") -> str:
        d = self.root + "/d"
        if name == "linkat":
            return f'7 linkat(AT_FDCWD</repo>, "{source}", AT_FDCWD<{d}>, "{destination}", 0) = {result}'
        if name == "renameat2":
            return f'7 renameat2(AT_FDCWD</repo>, "{source}", AT_FDCWD<{d}>, "{destination}", RENAME_NOREPLACE) = {result}'
        return f'7 {name}("{source}", "{d}/{destination}") = {result}'

    def link_lines(self, text: str | None = None, *, name: str = "symlink") -> list[str]:
        link = f"{self.root}/d/l"
        text = text or f"{HOST}/lib"
        if name == "symlinkat":
            return [self.mkdir_line("d"), f'1 symlinkat("{text}", AT_FDCWD</repo>, "{link}") = 0']
        return [self.mkdir_line("d"), f'1 symlink("{text}", "{link}") = 0']

    def through_line(self, rel: str = "x.pyc", *, flags: str = "O_RDONLY|O_CLOEXEC", fd: str | None = None, result: str | None = None) -> str:
        path = f"{self.root}/d/l/{rel}"
        return f'1 openat(AT_FDCWD</repo>, "{path}", {flags}) = {result or "3<" + (fd or HOST + "/lib/" + rel) + ">"}'


class ParserRegistrationTests(E2E):
    """T2.1：解析器把盘外一端登记为宿主访问；失败的引入登记 stat／missing，其余登记 write；宿主路径声明审计看得见。"""

    def test_the_failed_import_is_registered_as_a_host_probe_and_the_run_replays(self) -> None:
        src = HOST + "/pycache/a.pyc"
        for name in ("linkat", "link", "renameat2", "rename", "renameat"):
            with self.subTest(name=name):
                lines = [self.mkdir_line("d"), self.import_line(name, src, "a.pyc" if name in {"linkat", "renameat2", "renameat"} else "a.pyc")]
                if name == "renameat":
                    lines[1] = f'7 renameat(AT_FDCWD</repo>, "{src}", AT_FDCWD<{self.root}/d>, "a.pyc") = -1 EXDEV (Invalid cross-device link)'
                document = self.document(lines)
                self.assertReplays(document)
                self.assertEqual(self.accesses(document)[src], ["stat"], "盘外来源：失败的查找，记 stat（不是永远不可声明的 write）")
                self.assertEqual(self.host_findings(document), {src: ["stat"]}, "宿主路径声明审计看得见：需要这条路径的快照")
                events = [e for e in self.shape(document) if e["name"] == name]
                self.assertEqual([(e["role"], e["peer"], e["succeeded"], e["errno"]) for e in events],
                                 [("destination", src, False, "EXDEV")], "盘内一端只有一条事件，对端是盘外来源")

    def test_enoent_is_registered_as_missing_and_other_errnos_as_stat(self) -> None:
        src = HOST + "/pycache/a.pyc"
        cases = {"-1 ENOENT (No such file or directory)": ("missing", True), "-1 ENOTDIR (Not a directory)": ("missing", True),
                 "-1 EEXIST (File exists)": ("stat", False)}
        for result, (kind, clean) in cases.items():
            with self.subTest(result=result):
                lines = [self.mkdir_line("d")]
                if "EEXIST" in result:
                    lines.append(self.create_line("d/a.pyc"))
                lines.append(self.import_line("linkat", src, "a.pyc", result))
                document = self.document(lines)
                self.assertEqual(self.accesses(document)[src], [kind])
                self.assertEqual(document["runtime"]["outputs"]["problems"], [], "EEXIST 落在已知名字，ENOENT／ENOTDIR 不判")

    def test_the_fallback_copy_sequence_replays_end_to_end(self) -> None:
        """EXDEV → 缺失探测 → 创建 → 改时间 → 改权限，真实回退序列整条走解析器与重放。"""
        d = self.root + "/d"
        lines = [self.mkdir_line("d"), self.import_line("linkat", HOST + "/pycache/a.pyc", "a.pyc"),
                 f'8 newfstatat(AT_FDCWD<{d}>, "a.pyc", 0x1, AT_SYMLINK_NOFOLLOW) = -1 ENOENT (No such file or directory)',
                 f'8 openat(AT_FDCWD<{d}>, "a.pyc", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 4<{d}/a.pyc>',
                 f'8 utimensat(AT_FDCWD<{d}>, "a.pyc", [{{tv_sec=1, tv_nsec=0}}, {{tv_sec=1, tv_nsec=0}}], 0) = 0',
                 f'8 fchmodat(AT_FDCWD<{d}>, "a.pyc", 0644) = 0']
        document = self.document(lines)
        self.assertReplays(document)
        self.assertEqual(set(self.host_findings(document)), {HOST + "/pycache/a.pyc"})

    def test_the_other_shapes_keep_write_and_stay_problems(self) -> None:
        src = HOST + "/pycache/a.pyc"
        d = self.root + "/d"
        shapes = {
            "成功的引入": self.import_line("linkat", src, "a.pyc", "0"),
            "盘内来源、盘外目的的失败送出": f'7 rename("{d}/a", "{HOST}/out") = -1 EXDEV (Invalid cross-device link)',
            "成功的送出": f'7 rename("{d}/a", "{HOST}/out") = 0',
        }
        for label, line in shapes.items():
            with self.subTest(label=label):
                document = self.document([self.mkdir_line("d"), self.create_line("d/a"), line])
                host = HOST + "/out" if "送出" in label else src
                self.assertIn(OUTSIDE_PEER, "".join(document["runtime"]["outputs"]["problems"]))
                self.assertEqual(self.accesses(document)[host], ["write"], "保持原类型：不放松对宿主路径的写")
                self.assertFalse(self.audit(document)["coverage_complete"])

class FullAuditAcceptanceTests(E2E):
    """两处新放行加上真实宿主文件的快照声明：整条严格审计（输出归属重放＋宿主路径声明）通过——这才是收割 #9 里 evaluation_builder 要走通的链路。"""

    def import_lines(self, source: str) -> list[str]:
        d = self.root + "/d"
        return [self.mkdir_line("d"), self.import_line("linkat", source, "a.pyc"),
                f'8 newfstatat(AT_FDCWD<{d}>, "a.pyc", 0x1, AT_SYMLINK_NOFOLLOW) = -1 ENOENT (No such file or directory)',
                f'8 openat(AT_FDCWD<{d}>, "a.pyc", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 4<{d}/a.pyc>']

    def test_a_failed_import_with_a_declared_host_file_passes_the_whole_audit(self) -> None:
        source = self.host_file("hostcache", "a.pyc")
        report = self.full_report(self.import_lines(source), [source])
        self.assertTrue(report["coverage_complete"], report["undeclared"])

    def test_the_old_write_classification_could_never_be_declared(self) -> None:
        """对照：把盘外来源的访问类型退回修复前的 ``write``，同样的声明也过不了——这就是不降级访问类型时单元永远不可承接的原因。"""
        source = self.host_file("hostcache", "a.pyc")
        with mock.patch.object(ra.TraceParser, "_two_path_end_kind", lambda self_, kind, slot, ends, failed, missing: kind):
            report = self.full_report(self.import_lines(source), [source])
        self.assertFalse(report["coverage_complete"])

    def test_a_missing_host_source_is_a_declarable_missing_probe(self) -> None:
        folder = self.base / "hostcache2"
        folder.mkdir()
        source = str(folder / "gone.pyc")                                     # 不存在的宿主路径：ENOENT
        lines = self.import_lines(source)
        lines[1] = lines[1].replace("EXDEV (Invalid cross-device link)", "ENOENT (No such file or directory)")
        document, entries = self.host_document(lines, [source])
        self.assertEqual(self.accesses(document)[source], ["missing"])
        self.assertTrue(ra.strict_audit_reads(document, [self.entry, *entries], repo_root="/repo", data_root=None)["coverage_complete"])

    def test_a_read_through_the_own_link_with_a_declared_host_file_passes_the_whole_audit(self) -> None:
        target = self.host_file("hostlib", "x.pyc")
        lines = [self.mkdir_line("d"), f'1 symlink("{self.base}/hostlib", "{self.root}/d/l") = 0',
                 f'1 openat(AT_FDCWD</repo>, "{self.root}/d/l/x.pyc", O_RDONLY|O_CLOEXEC) = 3<{target}>']
        report = self.full_report(lines, [target])
        self.assertTrue(report["coverage_complete"], report["undeclared"])
        # 没有声明那个宿主文件：宿主路径声明审计拦下，而不是输出归属。
        undeclared = self.full_report(lines, [])
        self.assertFalse(undeclared["coverage_complete"])
        self.assertEqual([(f["root"], f["path"]) for f in undeclared["undeclared"]], [("host", target)])


class OwnLinkE2ETests(E2E):
    """T2.2 端到端。"""

    def test_the_read_through_the_own_link_replays_and_the_host_read_is_visible(self) -> None:
        for creator in ("symlink", "symlinkat"):
            with self.subTest(creator=creator):
                document = self.document(self.link_lines(name=creator) + [self.through_line()])
                self.assertReplays(document)
                self.assertEqual(self.host_findings(document), {HOST + "/lib/x.pyc": ["read"]},
                                 "描述符的真实路径被登记为宿主读取，宿主路径声明审计看得见")
                self.assertIn(f"{self.root}/d/l/x.pyc", self.accesses(document))

    def test_unsafe_path_forms_after_the_link_fail_closed(self) -> None:
        """``..``、``.``、结尾斜杠：规范化后的路径与内核实际解析的不同，解析器登记 symlink-path-form-unsafe（整次审计失败关闭），不靠跟踪器。"""
        for label, rel in {"..": "sub/../x.pyc", "结尾 ..": "sub/..", "回到链接的父目录": "../x.pyc"}.items():
            with self.subTest(label=label):
                document = self.document(self.link_lines() + [self.through_line(rel)])
                self.assertIn(UNSAFE, document["coverage"]["unresolved_reasons"])
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_dot_components_that_do_not_change_the_target_are_plain(self) -> None:
        document = self.document(self.link_lines() + [self.through_line("./x.pyc")])
        self.assertNotIn(UNSAFE, document["coverage"]["unresolved_reasons"])

    def test_other_shapes_stay_follow_problems(self) -> None:
        d = self.root + "/d"
        lines = {
            "fd 标注对不上": self.through_line(fd=HOST + "/elsewhere/x.pyc"),
            "写打开": self.through_line(flags="O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0644"),
            "O_RDWR": self.through_line(flags="O_RDWR|O_CLOEXEC"),
            "打开失败": self.through_line(result="-1 ENOENT (No such file or directory)"),
            "stat 类": f'1 newfstatat(AT_FDCWD</repo>, "{d}/l/x.pyc", 0x1, 0) = -1 ENOENT (No such file or directory)',
            "stat 成功": f'1 newfstatat(AT_FDCWD</repo>, "{d}/l/x.pyc", {{st_mode=S_IFREG|0644}}, 0) = 0',
            "access": f'1 faccessat(AT_FDCWD</repo>, "{d}/l/x.pyc", R_OK) = 0',
        }
        for label, line in lines.items():
            with self.subTest(label=label):
                document = self.document(self.link_lines() + [line])
                problems = document["runtime"]["outputs"]["problems"]
                self.assertTrue(any(FOLLOW in p for p in problems), problems)
                self.assertFalse(self.audit(document)["coverage_complete"])
                with self.assertRaises(runtime.RuntimeContractError):
                    runtime.replay_runtime(document, [self.entry])

    def test_a_link_pointing_inside_the_root_and_a_link_not_created_stay_problems(self) -> None:
        inside = self.document([self.mkdir_line("d"), self.mkdir_line("d/real"),
                                f'1 symlink("{self.root}/d/real", "{self.root}/d/l") = 0',
                                self.through_line(fd=f"{self.root}/d/real/x.pyc")])
        self.assertTrue(any(FOLLOW in p for p in inside["runtime"]["outputs"]["problems"]))
        none = self.document([self.mkdir_line("d"), self.through_line()])
        problems = none["runtime"]["outputs"]["problems"]
        self.assertTrue(any(DESCRIPTOR in p for p in problems), problems)

    def test_without_the_allowance_the_end_to_end_run_is_rejected(self) -> None:
        """对照：关掉放行（修复前的行为），同一份行得到收割 #9 报告里的那类问题。"""
        lines = self.link_lines() + [self.through_line()]
        with legacy(ra._runtime_module().OutputTrackerV4):
            document = self.document(lines)
        self.assertTrue(any(FOLLOW in p for p in document["runtime"]["outputs"]["problems"]))
        self.assertFalse(self.audit(document)["coverage_complete"])


# =====================================================================================================================
# T1 connect
# =====================================================================================================================


class ConnectPathFormTests(E2E):
    NSCD = "/var/run/nscd/socket"

    def connect(self, path: str, result: str = "-1 ENOENT (No such file or directory)") -> str:
        return f'1 connect(3<socket:[1234]>, {{sa_family=AF_UNIX, sun_path="{path}"}}, 110) = {result}'

    def link_then_connect(self, path: str, text: str = "/etc") -> list[str]:
        return [f'1 symlink("{text}", "{self.root}/ln") = 0', self.connect(path)]

    def test_dotdot_through_a_link_is_unsafe_and_registers_no_inner_name(self) -> None:
        """验收者的复现：``ln -> /etc``，``<R>/ln/../zz-sock`` 规范化成 ``<R>/zz-sock``，内核实际探测的是宿主路径 ``/zz-sock``。"""
        document = self.document(self.link_then_connect(f"{self.root}/ln/../zz-sock"))
        self.assertEqual(document["coverage"]["unresolved_reasons"], {UNSAFE: 1})
        self.assertNotIn(f"{self.root}/zz-sock", self.accesses(document), "不登记规范化后的盘内名字")
        self.assertFalse(self.audit(document)["coverage_complete"])
        self.assertNotIn("/zz-sock", self.accesses(document))

    def test_the_old_behaviour_is_what_the_reviewer_reproduced(self) -> None:
        """对照：把形态检查关掉（本轮之前的行为），同样的两行得到验收者的结论——审计完整、名字被藏成盘内缺失探测。"""
        lines = self.link_then_connect(f"{self.root}/ln/../zz-sock")
        with mock.patch.object(ra.TraceParser, "_check_path_form", lambda self_, directory, path: False):
            document = self.document(lines)
        self.assertEqual(document["coverage"]["unresolved_reasons"], {})
        self.assertEqual(self.accesses(document)[f"{self.root}/zz-sock"], ["missing"])
        self.assertTrue(self.audit(document)["coverage_complete"], "旧行为：宿主探测被藏起来，审计反而通过")

    def test_trailing_slash_and_dot_are_unsafe(self) -> None:
        for suffix in ("/", "/."):
            with self.subTest(suffix=suffix):
                document = self.document(self.link_then_connect(f"{self.root}/ln{suffix}"))
                self.assertEqual(document["coverage"]["unresolved_reasons"], {UNSAFE: 1})
                self.assertNotIn(f"{self.root}/ln", [path for path, kinds in document["accesses"] if "missing" in kinds])
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_dotdot_that_returns_to_a_link_name_is_unsafe(self) -> None:
        """沿链接下降再用 .. 回到链接名字（WP-R7a S-1 同类）：``ln/x/..``。"""
        document = self.document(self.link_then_connect(f"{self.root}/ln/x/../zz"))
        self.assertEqual(document["coverage"]["unresolved_reasons"], {UNSAFE: 1})

    def test_an_intermediate_component_through_the_link_is_a_follow_problem(self) -> None:
        document = self.document(self.link_then_connect(f"{self.root}/ln/zz-sock"))
        self.assertEqual(document["coverage"]["unresolved_reasons"], {})
        self.assertTrue(any(FOLLOW in p for p in document["runtime"]["outputs"]["problems"]), document["runtime"]["outputs"]["problems"])
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_the_ordinary_nscd_probe_still_passes(self) -> None:
        for with_link in (False, True):
            with self.subTest(with_link=with_link):
                lines = ([f'1 symlink("/etc", "{self.root}/ln") = 0'] if with_link else [self.mkdir_line("d")]) + [self.connect(self.NSCD)]
                document = self.document(lines)
                self.assertEqual(document["coverage"]["unresolved_reasons"], {})
                self.assertEqual(self.accesses(document)[self.NSCD], ["missing"])
                self.assertEqual(self.host_findings(document), {self.NSCD: ["missing"]})

    def test_dotdot_without_any_link_object_is_unchanged(self) -> None:
        """没有符号链接对象时 ``..`` 的文本规范化与内核等价，行为同以前（登记规范化后的路径）。"""
        document = self.document([self.mkdir_line("d"), self.connect("/var/run/../run/nscd/socket")])
        self.assertEqual(document["coverage"]["unresolved_reasons"], {})
        self.assertEqual(self.accesses(document)[self.NSCD], ["missing"])

    def test_dotdot_that_never_touches_a_link_is_plain_even_with_links_present(self) -> None:
        document = self.document(self.link_then_connect("/var/run/../run/nscd/socket"))
        self.assertEqual(document["coverage"]["unresolved_reasons"], {})
        self.assertEqual(self.accesses(document)[self.NSCD], ["missing"])

    def test_the_extractors_old_interface_is_unchanged_and_the_raw_one_keeps_the_text(self) -> None:
        good = ["3<socket:[1]>", '{sa_family=AF_UNIX, sun_path="/a/../b/"}', "110"]
        self.assertEqual(ra._connect_unix_path(good), "/b")
        self.assertEqual(ra._connect_unix_raw_path(good), "/a/../b/")
        for bad in (["3<socket:[1]>", '{sa_family=AF_UNIX, sun_path="rel/x"}', "110"], ["3<socket:[1]>", '{sa_family=AF_INET, sun_path="/x"}', "110"],
                    ["3<socket:[1]>", '{sa_family=AF_UNIX, sun_path=@"/x"}', "110"]):
            self.assertIsNone(ra._connect_unix_raw_path(bad))
            self.assertIsNone(ra._connect_unix_path(bad))

    def test_check_path_form_reports_whether_it_registered_a_hazard(self) -> None:
        parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v4")
        parser.feed(f'1 symlink("/etc", "{self.root}/ln") = 0')
        self.assertTrue(parser._check_path_form(None, f"{self.root}/ln/../x"))
        self.assertFalse(parser._check_path_form(None, f"{self.root}/other/../x"))
        self.assertEqual(parser.unresolved_reasons, {UNSAFE: 1})

    def test_v3_and_non_enoent_results_are_untouched(self) -> None:
        # v3：connect 仍是 unsupported-call（不进新分支）；v4 的非 ENOENT 结果同样。
        lines = self.link_then_connect(f"{self.root}/ln/../zz-sock")
        v3 = self.document(lines, contract="v3")
        self.assertEqual(v3["coverage"]["unresolved_reasons"], {"unsupported-call:connect": 1})
        self.assertNotIn(f"{self.root}/zz-sock", self.accesses(v3))
        other = self.document([f'1 symlink("/etc", "{self.root}/ln") = 0', self.connect(f"{self.root}/ln/../zz-sock", "0")])
        self.assertEqual(other["coverage"]["unresolved_reasons"], {"unsupported-call:connect": 1})


# =====================================================================================================================
# 重放：改纪元、删信息承载事件、伪造事件
# =====================================================================================================================


class ReplayTamperTests(E2E):
    def good(self) -> dict[str, Any]:
        src = HOST + "/pycache/a.pyc"
        d = self.root + "/d"
        lines = self.link_lines() + [self.through_line(), self.import_line("linkat", src, "a.pyc"),
                                     f'8 newfstatat(AT_FDCWD<{d}>, "a.pyc", 0x1, AT_SYMLINK_NOFOLLOW) = -1 ENOENT (No such file or directory)',
                                     f'8 openat(AT_FDCWD<{d}>, "a.pyc", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 4<{d}/a.pyc>',
                                     self.through_line("y.pyc")]
        document = self.document(lines)
        self.assertReplays(document)
        return document

    def consistent(self, document: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """换上事件列表，并像解析器一样重算每条事件的纪元与 outputs：只留下跟踪器本身能拒绝的余地。"""
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

    def test_the_good_document_has_both_new_event_shapes(self) -> None:
        events = self.good()["runtime"]["output_events"]
        self.assertEqual(sum(1 for e in events if e["name"] == "linkat" and e["peer"] and not e["peer"].startswith(self.root)), 1)
        self.assertEqual(sum(1 for e in events if e["kind"] == "read" and e["target"] and not e["target"].startswith(self.root)), 2)

    def test_changing_any_epoch_is_rejected(self) -> None:
        document = self.good()
        for index in range(len(document["runtime"]["output_events"])):
            with self.subTest(index=index):
                tampered = copy.deepcopy(document)
                tampered["runtime"]["output_events"][index]["epoch"] += 1
                with self.assertRaises(runtime.RuntimeContractError):
                    runtime.replay_runtime(tampered, [self.entry])

    def test_dropping_the_information_carrying_events_is_rejected(self) -> None:
        document = self.good()
        events = document["runtime"]["output_events"]
        carriers = {"创建符号链接": next(i for i, e in enumerate(events) if e["name"] == "symlink"),
                    "创建目录": next(i for i, e in enumerate(events) if e["name"] == "mkdir"),
                    "回退复制的创建（一条）": next(i for i, e in enumerate(events) if e["name"] == "openat" and e["kind"] == "write")}
        for label, index in carriers.items():
            with self.subTest(label=label):
                tampered = copy.deepcopy(document)
                del tampered["runtime"]["output_events"][index]
                with self.assertRaises(runtime.RuntimeContractError):
                    runtime.replay_runtime(tampered, [self.entry])
        # 删掉之后如实重算纪元与 outputs：创建符号链接这条消失，经链接的读取立刻不再被放行，被跟踪器本身拒绝。
        index = carriers["创建符号链接"]
        rebuilt = self.consistent(document, [e for i, e in enumerate(copy.deepcopy(events)) if i != index])
        self.assertTrue(rebuilt["runtime"]["outputs"]["problems"])
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.replay_runtime(rebuilt, [self.entry])

    def test_forged_events_are_rejected(self) -> None:
        document = self.good()
        events = document["runtime"]["output_events"]
        link_index = next(i for i, e in enumerate(events) if e["name"] == "linkat")
        read_index = next(i for i, e in enumerate(events) if e["kind"] == "read")

        def forged(index: int, **changes: Any) -> dict[str, Any]:
            copies = copy.deepcopy(events)
            copies[index].update(changes)
            return self.consistent(document, copies)

        cases = {
            "失败的引入伪造成成功": forged(link_index, succeeded=True, errno=None),
            "失败的引入换成别的错误码": forged(link_index, errno="EACCES"),
            "失败的引入抹掉对端": forged(link_index, peer=None),
            "失败的引入改成来源端角色": forged(link_index, role="source"),
            "读取的真实路径改到别处": forged(read_index, target=HOST + "/elsewhere/x.pyc"),
            "读取的真实路径抹掉": forged(read_index, target=None),
            "读取伪造成带 O_CREAT 的": forged(read_index, flags=sorted(set(events[read_index]["flags"]) | {"O_CREAT"})),
            "读取伪造成失败": forged(read_index, succeeded=False, errno="ENOENT", kind="missing", target=None),
            "读取改成 stat 类调用": forged(read_index, name="newfstatat", kind="stat"),
        }
        for label, tampered in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(runtime.RuntimeContractError):
                    runtime.replay_runtime(tampered, [self.entry])

    def test_forging_a_clean_outputs_block_over_bad_events_is_rejected(self) -> None:
        document = self.good()
        tampered = copy.deepcopy(document)
        events = tampered["runtime"]["output_events"]
        events[next(i for i, e in enumerate(events) if e["kind"] == "read")]["target"] = "/etc/passwd"
        self.assertEqual(tampered["runtime"]["outputs"]["problems"], [], "outputs 仍是原来的干净块；只改了事件，没有如实重算")
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.replay_runtime(tampered, [self.entry])

    def test_v3_replay_does_not_accept_the_new_shapes(self) -> None:
        """v3 文档仍走旧跟踪器：带 v3 的同样内容仍有问题。"""
        document = self.document(self.link_lines() + [self.through_line()], contract="v3")
        self.assertTrue(document["runtime"]["outputs"]["problems"] or document["coverage"]["unresolved"])
        self.assertFalse(self.audit(document)["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
