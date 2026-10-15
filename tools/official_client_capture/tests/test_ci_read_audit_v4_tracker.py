"""v4 输出归属跟踪器（``read_audit_runtime.OutputTrackerV4``）：按对象身份记账的正反例与随机历史对拍。

跟踪器只吃事件字典，不碰文件系统，所以这里不需要 Linux、strace 或真实挂载：事件按真实解析器产出的 11 键形状手工拼装，
调用编号自增、同一调用的事件共享编号。覆盖四类内容：

* 正例：合法的创建、rename、硬链接、失败探测，一个问题都不能报；
* 反例：符号链接、RENAME_EXCHANGE／WHITEOUT、另一端在盘外、来源未证明、目录覆盖非空目录等，必须报问题；
* 失败调用按错误码与已知状态的判定表；
* 随机历史对拍：独立实现的「真实文件系统」按 POSIX 语义生成合法历史，跟踪器必须零问题，且名字表、对象身份、子项计数
  与真实状态逐步一致，「状态是否改变」的返回值也必须与真实状态是否改变一致（解析器用它推进变更纪元并决定能否去重）。
  WP-R3 起合法历史里也有符号链接：创建（含 EEXIST／父目录缺失等失败）、unlink、rename 覆盖与被覆盖、lstat、readlink；
  “该拒绝的跟随式访问”另由 test_ci_read_audit_wp_r3 的差分对拍覆盖。
"""

from __future__ import annotations

import random
import unittest
from typing import Any

from tools.ci import read_audit_runtime as runtime

ROOT = "/var/tmp/read-audit-v4/run/private/output"
CONTRACT = {"schema_version": runtime.SCHEMA, "temporary_root": ROOT, "mask_roots": [], "environment_paths": []}
D = ROOT + "/d"


class Builder:
    """按解析器的产出形状拼 v4 事件；每次调用编号加一，同一调用的事件共享编号。"""

    def __init__(self) -> None:
        self.call = 0

    def event(self, name: str, path: str, *, kind: str = "write", ok: bool = True, flags: tuple[str, ...] = (),
              target: str | None = None, errno: str | None = None, role: str | None = None,
              peer: str | None = None, call: int | None = None) -> dict[str, Any]:
        if call is None:
            self.call += 1
            call = self.call
        return {"path": path, "kind": kind, "name": name, "succeeded": ok, "flags": sorted(flags), "target": target,
                "errno": errno, "call": call, "role": role, "peer": peer, "epoch": 0}

    def mkdir(self, path: str, **kw: Any) -> list[dict[str, Any]]:
        return [self.event("mkdir", path, **kw)]

    def create(self, path: str, extra: tuple[str, ...] = ("O_EXCL",), **kw: Any) -> list[dict[str, Any]]:
        ok = kw.get("ok", True)
        return [self.event("openat", path, flags=("O_WRONLY", "O_CREAT", *extra), target=path if ok else None, **kw)]

    def read(self, path: str, **kw: Any) -> list[dict[str, Any]]:
        return [self.event("openat", path, kind="read", flags=("O_RDONLY",), target=path, **kw)]

    def probe(self, path: str, errno: str = "ENOENT", name: str = "newfstatat") -> list[dict[str, Any]]:
        kind = "missing" if errno in {"ENOENT", "ENOTDIR"} else "stat"
        return [self.event(name, path, kind=kind, ok=False, errno=errno)]

    def stat(self, path: str) -> list[dict[str, Any]]:
        return [self.event("newfstatat", path, kind="stat")]

    def unlink(self, path: str, name: str = "unlink", **kw: Any) -> list[dict[str, Any]]:
        return [self.event(name, path, **kw)]

    def symlink(self, path: str, text: str | None = "testing", name: str = "symlinkat", **kw: Any) -> list[dict[str, Any]]:
        """符号链接创建：事件的 target 字段是链接文本（原样，不解引用）。"""
        return [self.event(name, path, target=text, **kw)]

    def lstat(self, path: str, name: str = "newfstatat", *, ok: bool = True, errno: str | None = None) -> list[dict[str, Any]]:
        """不跟随的元数据读取：lstat 本身，或带 AT_SYMLINK_NOFOLLOW 的 fstatat 系。失败时 ENOENT／ENOTDIR 是缺失探测。"""
        flags = () if name in {"lstat", "lstat64"} else ("AT_SYMLINK_NOFOLLOW",)
        kind = "missing" if not ok and errno in {"ENOENT", "ENOTDIR"} else "stat"
        return [self.event(name, path, kind=kind, flags=flags, ok=ok, errno=errno)]

    def readlink(self, path: str, name: str = "readlinkat", *, ok: bool = True, errno: str | None = None) -> list[dict[str, Any]]:
        kind = "missing" if not ok and errno in {"ENOENT", "ENOTDIR"} else "stat"
        return [self.event(name, path, kind=kind, ok=ok, errno=errno)]

    def pair(self, name: str, source: str, destination: str, *, ok: bool = True, errno: str | None = None,
             flags: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """两路径调用：来源端与目的端各一条事件，共享编号，互为 peer。"""
        self.call += 1
        common = dict(kind="write", ok=ok, errno=errno, flags=flags, call=self.call)
        return [self.event(name, source, role="source", peer=destination, **common),
                self.event(name, destination, role="destination", peer=source, **common)]

    def rename(self, source: str, destination: str, **kw: Any) -> list[dict[str, Any]]:
        return self.pair(kw.pop("name", "rename"), source, destination, **kw)

    def link(self, source: str, destination: str, **kw: Any) -> list[dict[str, Any]]:
        return self.pair(kw.pop("name", "link"), source, destination, **kw)


def run(*groups: list[dict[str, Any]]) -> tuple[runtime.OutputTrackerV4, list[bool]]:
    """依次喂入各组事件，返回跟踪器与每条事件「是否改变状态」的返回值。"""
    tracker = runtime.OutputTrackerV4(CONTRACT)
    changed = [tracker.observe_event(event) for group in groups for event in group]
    return tracker, changed


class TrackerPositiveTests(unittest.TestCase):
    """合法历史：一个问题都不能报。"""

    def setUp(self) -> None:
        self.b = Builder()

    def assertClean(self, tracker: runtime.OutputTrackerV4) -> None:
        self.assertEqual(tracker.report()["problems"], [])

    def test_create_then_rename_file_then_read_new_name(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"), b.rename(D + "/a", D + "/b"), b.read(D + "/b"))
        self.assertClean(tracker)
        self.assertIn(D + "/b", tracker.names)
        self.assertNotIn(D + "/a", tracker.names, "rename 之后旧名字必须解绑")
        self.assertEqual(tracker.entries[D], 1)

    def test_directory_rename_moves_the_whole_subtree(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.mkdir(D + "/s"), b.mkdir(D + "/s/t"), b.create(D + "/s/x"), b.create(D + "/s/t/y"),
                         b.rename(D + "/s", D + "/n"),
                         b.read(D + "/n/x"), b.read(D + "/n/t/y"),
                         [b.event("getdents64", D + "/n", kind="list")])
        self.assertClean(tracker)
        self.assertEqual({path for path in tracker.names if path.startswith(D)},
                         {D, D + "/n", D + "/n/t", D + "/n/x", D + "/n/t/y"})
        self.assertEqual((tracker.entries[D], tracker.entries[D + "/n"], tracker.entries[D + "/n/t"]), (1, 2, 1))
        self.assertNotIn(D + "/s", tracker.entries, "子项计数要跟着目录一起搬走，不能留下旧键")

    def test_directory_rename_over_empty_directory(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.mkdir(D + "/a"), b.create(D + "/a/f"), b.mkdir(D + "/b"),
                         b.rename(D + "/a", D + "/b"), b.read(D + "/b/f"))
        self.assertClean(tracker)
        self.assertEqual(tracker.entries[D], 1)

    def test_noreplace_success_is_handled_like_a_plain_rename(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"),
                         b.rename(D + "/a", D + "/b", name="renameat2", flags=("RENAME_NOREPLACE",)), b.read(D + "/b"))
        self.assertClean(tracker)
        self.assertNotIn(D + "/a", tracker.names)

    def test_rename_between_two_names_of_the_same_object_changes_nothing(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"), b.link(D + "/a", D + "/b"))
        changed = [tracker.observe_event(event) for event in b.rename(D + "/a", D + "/b") + b.read(D + "/a") + b.read(D + "/b")]
        self.assertClean(tracker)
        self.assertEqual(changed, [False] * 4, "同对象两个名字之间 rename：POSIX 成功但什么也不改，两个名字都还在")
        self.assertEqual(tracker.names[D + "/a"], tracker.names[D + "/b"])

    def test_rename_onto_itself_changes_nothing(self) -> None:
        b = self.b
        tracker, changed = run(b.mkdir(D), b.create(D + "/a"), b.rename(D + "/a", D + "/a"), b.read(D + "/a"))
        self.assertClean(tracker)
        self.assertEqual(changed[-3:], [False, False, False])

    def test_hardlink_then_delete_original_then_read_the_new_name(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"), b.link(D + "/a", D + "/b"), b.unlink(D + "/a"), b.read(D + "/b"))
        self.assertClean(tracker)
        obj = tracker.names[D + "/b"]
        self.assertEqual(tracker.links[obj], 1, "删掉一个名字只减一个计数，对象还活着")
        tracker.observe_event(b.unlink(D + "/b")[0])
        self.assertNotIn(obj, tracker.kinds, "最后一个名字也删掉后对象消亡")
        self.assertClean(tracker)

    def test_hardlink_names_share_one_object_and_both_can_be_read(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"), b.link(D + "/a", D + "/b"), b.read(D + "/a"), b.read(D + "/b"))
        self.assertClean(tracker)
        self.assertEqual(tracker.names[D + "/a"], tracker.names[D + "/b"])
        self.assertEqual(tracker.links[tracker.names[D + "/a"]], 2)

    def test_overwriting_rename_of_a_file_keeps_other_hardlink_alive(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"), b.create(D + "/b"), b.link(D + "/b", D + "/c"),
                         b.rename(D + "/a", D + "/b"), b.read(D + "/b"), b.read(D + "/c"))
        self.assertClean(tracker)
        self.assertNotEqual(tracker.names[D + "/b"], tracker.names[D + "/c"], "b 现在是原来 a 的对象，c 仍是旧对象")
        self.assertEqual(tracker.links[tracker.names[D + "/c"]], 1)

    def test_creation_refusal_on_known_names_is_ignored(self) -> None:
        b = self.b
        tracker, changed = run(b.mkdir(D), b.create(D + "/a"),
                               b.mkdir(D, ok=False, errno="EEXIST"),
                               b.create(D + "/a", ok=False, errno="EEXIST"),
                               b.link(D + "/a", D + "/a", ok=False, errno="EEXIST"),
                               b.rename(D + "/a", D + "/a", name="renameat2", ok=False, errno="EEXIST",
                                        flags=("RENAME_NOREPLACE",)))
        self.assertClean(tracker)
        self.assertEqual(changed[2:], [False] * (len(changed) - 2))

    def test_missing_probes_on_unknown_paths_are_ignored(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D),
                         b.probe(D + "/nope"), b.probe(D + "/nope", "ENOTDIR", "openat"),
                         b.mkdir(D + "/x/y", ok=False, errno="ENOENT"),
                         b.unlink(D + "/nope", ok=False, errno="ENOENT"),
                         b.create(D + "/x/y", ok=False, errno="ENOENT"),
                         b.rename(D + "/nope", D + "/other", ok=False, errno="ENOENT"))
        self.assertClean(tracker)

    def test_two_path_enoent_is_explained_by_a_missing_destination_parent(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"),
                         b.rename(D + "/a", D + "/gone/b", ok=False, errno="ENOENT"),
                         b.link(D + "/a", D + "/gone/b", ok=False, errno="ENOENT"))
        self.assertClean(tracker)

    def test_unlink_rmdir_and_recreate(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.mkdir(D + "/e"), b.create(D + "/f"),
                         b.unlink(D + "/f"), b.unlink(D + "/e", name="unlinkat"),
                         b.mkdir(D + "/e"), b.unlink(D + "/e", name="rmdir"), b.create(D + "/f"), b.read(D + "/f"))
        self.assertClean(tracker)
        self.assertEqual(tracker.entries[D], 1)

    def test_metadata_calls_on_proven_objects_are_allowed(self) -> None:
        b = self.b
        names = ("chmod", "fchmodat", "fchmodat2", "chown", "fchownat", "utime", "utimes", "utimensat", "truncate", "truncate64")
        tracker, changed = run(b.mkdir(D), b.create(D + "/a"), *[[b.event(name, D + "/a")] for name in names])
        self.assertClean(tracker)
        self.assertEqual(changed[2:], [False] * len(names))

    def test_reads_probes_and_exec_of_proven_objects(self) -> None:
        b = self.b
        directory_open = b.event("openat", D, kind="dir", flags=("O_RDONLY", "O_DIRECTORY"), target=D)
        tracker, changed = run(b.mkdir(D), b.create(D + "/a"), b.read(D + "/a"), b.stat(D + "/a"),
                               [b.event("execve", D + "/a", kind="exec")], [directory_open],
                               [b.event("getdents64", D, kind="list")], [b.event("newfstatat", ROOT, kind="stat")])
        self.assertClean(tracker)
        self.assertEqual(changed[2:], [False] * 6)

    def test_covered_paths_include_every_event_path_under_the_root(self) -> None:
        b = self.b
        tracker, _ = run(b.mkdir(D), b.create(D + "/a"), b.rename(D + "/a", D + "/b"))
        self.assertEqual(tracker.report()["paths"], sorted([D, D + "/a", D + "/b"]))
        self.assertEqual(tracker.report()["root"], ROOT)

    def test_events_outside_the_private_root_are_ignored(self) -> None:
        tracker, changed = run([self.b.event("openat", "/etc/passwd", kind="read")])
        self.assertEqual((changed, tracker.report()["paths"], tracker.report()["problems"]), ([False], [], []))


class TrackerChangedFlagTests(unittest.TestCase):
    """observe_event 的返回值决定变更纪元与能否去重：改名字空间的一律 True，其余一律 False。"""

    def test_only_namespace_changes_return_true(self) -> None:
        b = Builder()
        link, rename = b.link(D + "/a", D + "/l"), b.rename(D + "/l", D + "/m")
        expectations = [
            (b.mkdir(D)[0], True), (b.create(D + "/a")[0], True), (b.create(D + "/a", extra=())[0], False),
            (b.read(D + "/a")[0], False), (b.stat(D + "/a")[0], False),
            (b.mkdir(D, ok=False, errno="EEXIST")[0], False), (b.probe(D + "/zz")[0], False),
            (link[0], True), (link[1], False),       # 硬链接：来源端事件改状态，目的端事件只登记覆盖路径
            (rename[0], True), (rename[1], False),
            (b.unlink(D + "/m")[0], True),
        ]
        tracker = runtime.OutputTrackerV4(CONTRACT)
        for event, expected in expectations:
            with self.subTest(name=event["name"], role=event["role"], path=event["path"], expected=expected):
                self.assertEqual(tracker.observe_event(event), expected)
        self.assertEqual(tracker.report()["problems"], [])

    def test_rejected_events_do_not_claim_a_state_change(self) -> None:
        b = Builder()
        tracker, changed = run(b.mkdir(D), b.create(D + "/a"),
                               b.rename(D + "/a", D + "/b", name="renameat2", flags=("RENAME_EXCHANGE",)),
                               b.rename(D + "/nope", D + "/b"),
                               [b.event("symlink", D + "/s")])
        self.assertTrue(tracker.problems)
        self.assertEqual(changed[2:], [False] * (len(changed) - 2))
        self.assertIn(D + "/a", tracker.names, "被拒绝的 rename 不能改名字表")


class TrackerNegativeTests(unittest.TestCase):
    """保守拒绝：每个反例都必须报出问题，且问题文字指向对应原因。"""

    def setUp(self) -> None:
        self.b = Builder()

    def problems(self, *groups: list[dict[str, Any]]) -> list[str]:
        return run(*groups)[0].report()["problems"]

    def assertProblem(self, problems: list[str], fragment: str) -> None:
        self.assertTrue(any(fragment in item for item in problems), f"应报出「{fragment}」，实际：{problems}")

    def test_symbolic_link_creation_needs_a_recorded_text_a_free_name_and_a_proven_parent(self) -> None:
        """WP-R3：创建符号链接本身不再被一刀切拒绝，但缺链接文本、名字已被占用、父目录未证明时仍判问题。
        原先固定“符号链接一律拒绝”的断言在这里被有意识地改写；带完整信息的创建见 SymlinkTrackerTests。"""
        b = self.b
        for name in ("symlink", "symlinkat"):
            with self.subTest(name=name, case="没有链接文本"):
                self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), [b.event(name, D + "/s")]), "符号链接")
            with self.subTest(name=name, case="链接文本为空"):
                self.assertProblem(self.problems(b.mkdir(D), b.symlink(D + "/s", "", name=name)), "符号链接")
            with self.subTest(name=name, case="名字已被占用"):
                self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), b.symlink(D + "/a", "x", name=name)), "已知名字")
            with self.subTest(name=name, case="父目录未证明"):
                self.assertProblem(self.problems(b.mkdir(D), b.symlink(D + "/nodir/s", "x", name=name)), "父目录未证明")

    def test_exchange_and_whiteout_flags_are_refused(self) -> None:
        b = self.b
        for flag in ("RENAME_EXCHANGE", "RENAME_WHITEOUT"):
            with self.subTest(flag=flag):
                problems = self.problems(b.mkdir(D), b.create(D + "/a"), b.create(D + "/b"),
                                         b.rename(D + "/a", D + "/b", name="renameat2", flags=(flag,)))
                self.assertProblem(problems, "RENAME_EXCHANGE")

    def test_peer_outside_the_private_root_or_missing_is_refused(self) -> None:
        b = self.b
        for name in ("rename", "link"):
            for peer in ("/host/elsewhere", None):
                with self.subTest(name=name, peer=peer):
                    source_only = b.event(name, D + "/a", role="source", peer=peer)
                    self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), [source_only]), "另一端不在私有临时盘内")
                    destination_only = b.event(name, D + "/b", role="destination", peer=peer)
                    self.assertProblem(self.problems(b.mkdir(D), [destination_only]), "另一端不在私有临时盘内")

    def test_failed_two_path_call_with_outside_peer_is_still_refused(self) -> None:
        b = self.b
        event = b.event("rename", D + "/a", role="source", peer="/host/x", ok=False, errno="EXDEV")
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), [event]), "另一端不在私有临时盘内")

    def test_two_path_event_without_a_role_is_refused(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), [b.event("rename", D + "/a", peer=D + "/b")]), "缺少角色标记")

    def test_rename_source_must_be_proven(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.rename(D + "/a", D + "/b")), "来源未证明")

    def test_link_source_must_be_proven(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.link(D + "/a", D + "/b")), "来源未证明")

    def test_hardlink_of_a_directory_is_refused(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.mkdir(D + "/s"), b.link(D + "/s", D + "/t")), "不是普通文件")

    def test_hardlink_target_must_not_exist_and_needs_a_proven_parent(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), b.create(D + "/b"), b.link(D + "/a", D + "/b")), "已存在")
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), b.link(D + "/a", D + "/nodir/b")), "父目录未证明")

    def test_rename_target_needs_a_proven_parent(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), b.rename(D + "/a", D + "/nodir/b")), "父目录未证明")

    def test_directory_rename_over_a_non_empty_directory_is_refused(self) -> None:
        b = self.b
        problems = self.problems(b.mkdir(D), b.mkdir(D + "/a"), b.mkdir(D + "/b"), b.create(D + "/b/f"),
                                 b.rename(D + "/a", D + "/b"))
        self.assertProblem(problems, "非空目录")

    def test_directory_cannot_move_into_itself_or_over_its_ancestor(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.mkdir(D + "/a"), b.mkdir(D + "/a/b"), b.rename(D + "/a", D + "/a/b")), "子目录")
        self.assertProblem(self.problems(b.mkdir(D), b.mkdir(D + "/a"), b.mkdir(D + "/a/b"), b.rename(D + "/a/b", D)), "非空目录")

    def test_file_and_directory_cannot_replace_each_other(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/f"), b.mkdir(D + "/d2"), b.rename(D + "/f", D + "/d2")), "不同类型")
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/f"), b.mkdir(D + "/d2"), b.rename(D + "/d2", D + "/f")), "不同类型")

    def test_root_cannot_be_moved_or_overwritten(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.rename(ROOT, D + "/x")), "根")
        self.assertProblem(self.problems(b.mkdir(D), b.mkdir(D + "/a"), b.rename(D + "/a", ROOT)), "根")

    def test_old_names_are_not_proven_after_a_move(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), b.rename(D + "/a", D + "/b"), b.read(D + "/a")), "首次读取前未证明")
        subtree = [b.mkdir(D), b.mkdir(D + "/s"), b.create(D + "/s/x"), b.rename(D + "/s", D + "/n")]
        self.assertProblem(self.problems(*subtree, b.read(D + "/s/x")), "首次读取前未证明")

    def test_unproven_reads_and_writes_are_refused(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.read(D + "/old")), "首次读取前未证明")
        self.assertProblem(self.problems(b.mkdir(D), [b.event("openat", D + "/w", flags=("O_WRONLY",), target=D + "/w")]), "缺少创建证明")
        self.assertProblem(self.problems(b.create(D + "/orphan")), "缺少创建证明")
        self.assertProblem(self.problems(b.mkdir(D + "/x/y")), "父目录未证明")
        self.assertProblem(self.problems(b.mkdir(D), b.mkdir(D)), "已存在")

    def test_descriptor_outside_the_private_root_is_refused(self) -> None:
        b = self.b
        event = b.event("openat", D + "/link", kind="read", flags=("O_RDONLY",), target="/host/data")
        self.assertProblem(self.problems(b.mkdir(D), [event]), "文件描述符未绑定")
        event = b.event("openat", D + "/a", flags=("O_CREAT", "O_WRONLY"), target=None)
        self.assertProblem(self.problems(b.mkdir(D), [event]), "文件描述符未绑定")

    def test_deleting_unproven_wrong_kind_or_non_empty_is_refused(self) -> None:
        b = self.b
        self.assertProblem(self.problems(b.unlink(D + "/never")), "删除未证明")
        self.assertProblem(self.problems(b.mkdir(D), b.mkdir(D + "/e"), b.unlink(D + "/e")), "删除方式")
        self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/f"), b.unlink(D + "/f", name="rmdir")), "删除方式")
        self.assertProblem(self.problems(b.mkdir(D), b.mkdir(D + "/e"), b.create(D + "/e/f"), b.unlink(D + "/e", name="rmdir")), "仍有已知子项")
        self.assertProblem(self.problems(b.unlink(ROOT, name="rmdir")), "删除未证明")

    def test_unmodelled_write_operations_are_refused(self) -> None:
        b = self.b
        for name in ("mknod", "mknodat", "setxattr", "removexattr"):
            with self.subTest(name=name):
                self.assertProblem(self.problems(b.mkdir(D), b.create(D + "/a"), [b.event(name, D + "/a")]), "未建立合同的输出操作")
        self.assertProblem(self.problems(b.mkdir(D), [b.event("chmod", D + "/unknown")]), "未建立合同的输出操作")


class TrackerFailureRuleTests(unittest.TestCase):
    """失败调用按错误码与已知状态判。表里每一行都是一个明确的合同。"""

    def test_single_path_failure_table(self) -> None:
        known, unknown = D + "/known", D + "/unknown"
        table = [
            # (调用, 事件类别, 路径, 错误码, 路径是否已知, 是否应报问题)
            ("mkdir", "write", known, "EEXIST", True, False),          # 创建被拒绝：路径确实已存在
            ("openat", "write", known, "EEXIST", True, False),
            ("mkdir", "write", unknown, "EEXIST", False, True),        # 未知路径出现 EEXIST：有来源不明的对象
            ("openat", "write", unknown, "EEXIST", False, True),
            ("openat", "missing", unknown, "ENOENT", False, False),    # 合法的探测
            ("newfstatat", "missing", unknown, "ENOTDIR", False, False),
            ("unlink", "write", unknown, "ENOENT", False, False),
            ("mkdir", "write", unknown, "ENOENT", False, False),
            ("openat", "missing", known, "ENOENT", True, True),        # 已知名字报不存在：矛盾
            ("newfstatat", "missing", known, "ENOTDIR", True, True),
            ("unlink", "write", known, "ENOENT", True, True),
            # 写类调用的其余错误码一律保守判问题
            ("openat", "write", known, "EACCES", True, True),
            ("openat", "write", D, "EISDIR", True, True),
            ("unlinkat", "write", known, "EISDIR", True, True),
            ("rmdir", "write", D, "ENOTEMPTY", True, True),
            ("utimensat", "write", known, "EPERM", True, True),
            ("mkdir", "write", unknown, "EROFS", False, True),
            ("openat", "write", known, None, True, True),              # 失败却没有错误码
            # 读取、探测、执行、列目录类调用的其他失败沿用旧规则：只要求路径已证明，错误码不影响判定
            ("readlinkat", "stat", known, "EINVAL", True, False),      # python3 启动脚本时 glibc realpath 对每一级路径都会这样
            ("execve", "stat", known, "EACCES", True, False),          # 执行没有执行权限的假程序
            ("openat", "stat", known, "ELOOP", True, False),
            ("getdents64", "list", known, "EBADF", True, False),
            ("openat", "stat", unknown, "EACCES", False, True),        # 但路径未证明照样是问题
            ("readlinkat", "stat", unknown, "EINVAL", False, True),
        ]
        for name, kind, path, errno, is_known, expect_problem in table:
            with self.subTest(name=name, kind=kind, path=path.rsplit("/", 1)[-1], errno=errno, known=is_known):
                b = Builder()
                setup = [b.mkdir(D)] + ([b.create(known)] if is_known and path != D else [])
                tracker, changed = run(*setup, [b.event(name, path, kind=kind, ok=False, errno=errno)])
                self.assertEqual(bool(tracker.report()["problems"]), expect_problem, tracker.report()["problems"])
                self.assertFalse(changed[-1], "失败调用永远不改名字空间")

    def test_two_path_failure_rules(self) -> None:
        b = Builder()
        base = [b.mkdir(D), b.create(D + "/a"), b.create(D + "/b"), b.mkdir(D + "/sub")]

        def problems(errno: str, source: str, destination: str, name: str = "rename") -> list[str]:
            return run(*base, b.pair(name, source, destination, ok=False, errno=errno))[0].report()["problems"]

        # EEXIST：两端都必须是本轮已知的名字。
        self.assertEqual(problems("EEXIST", D + "/a", D + "/b", "link"), [])
        self.assertTrue(problems("EEXIST", D + "/a", D + "/unknown", "link"))
        self.assertTrue(problems("EEXIST", D + "/unknown", D + "/b", "link"))
        # ENOENT／ENOTDIR：来源不存在，或目标父目录不存在（不是目录），都能解释；
        # 来源已知且目标父目录是已知目录时无从解释。
        self.assertEqual(problems("ENOENT", D + "/unknown", D + "/b"), [])
        self.assertEqual(problems("ENOENT", D + "/a", D + "/nodir/x"), [])
        self.assertEqual(problems("ENOTDIR", D + "/a", D + "/b/x"), [])
        self.assertTrue(problems("ENOENT", D + "/a", D + "/sub/x"))
        # 其余错误码一律保守判问题。
        for errno in ("EXDEV", "EACCES", "ENOTEMPTY", "EISDIR", "EPERM", "EINVAL"):
            with self.subTest(errno=errno):
                self.assertTrue(problems(errno, D + "/a", D + "/b"))


class _SimNode:
    """独立实现的「真实文件系统」节点：目录有子项表，普通文件只有身份。硬链接就是同一个节点出现在两个父目录项里。"""

    __slots__ = ("kind", "children", "text")

    def __init__(self, kind: str, text: str | None = None) -> None:
        self.kind = kind          # "directory"、"file"，或 WP-R3 起的 "symlink"（text 是链接文本，不解引用）
        self.children: dict[str, _SimNode] = {}
        self.text = text


class _SimFs:
    """按 POSIX 语义执行操作的最小文件系统（树结构，和跟踪器的「名字表＋对象表」实现互相独立）。"""

    def __init__(self, root: str) -> None:
        self.root = root
        self.top = _SimNode("directory")
        self.prefix = [part for part in root.split("/") if part]

    def parts(self, path: str) -> list[str]:
        parts = [part for part in path.split("/") if part]
        assert parts[:len(self.prefix)] == self.prefix
        return parts[len(self.prefix):]

    def lookup(self, path: str) -> _SimNode | None:
        node: _SimNode | None = self.top
        for part in self.parts(path):
            if node is None or node.kind != "directory":
                return None
            node = node.children.get(part)
        return node

    def parent_of(self, path: str) -> tuple[_SimNode | None, str]:
        parts = self.parts(path)
        return self.lookup(self.root + "".join("/" + part for part in parts[:-1])), parts[-1]

    def through_symlink(self, path: str) -> bool:
        """路径的某个中间分量（不含最后一个）当前是符号链接：内核解析它时必然解引用——独立于跟踪器的“真实语义”判定。"""
        node: _SimNode | None = self.top
        for part in self.parts(path)[:-1]:
            node = node.children.get(part) if node is not None and node.kind == "directory" else None
            if node is None:
                return False
            if node.kind == "symlink":
                return True
        return False

    def failure_errno(self, path: str) -> str:
        """路径解析失败时内核会给的错误码：途中遇到普通文件是 ENOTDIR，遇到缺失的名字是 ENOENT。"""
        node = self.top
        for part in self.parts(path):
            if node.kind != "directory":
                return "ENOTDIR"
            child = node.children.get(part)
            if child is None:
                return "ENOENT"
            node = child
        return "ENOENT"

    def snapshot(self) -> dict[str, int]:
        """所有名字 → 节点身份（id）。"""
        result: dict[str, int] = {}

        def walk(path: str, node: _SimNode) -> None:
            result[path] = id(node)
            for name, child in node.children.items():
                walk(path + "/" + name, child)

        walk(self.root, self.top)
        return result

    def kinds(self) -> dict[str, str]:
        result: dict[str, str] = {}

        def walk(path: str, node: _SimNode) -> None:
            result[path] = node.kind
            for name, child in node.children.items():
                walk(path + "/" + name, child)

        walk(self.root, self.top)
        return result


def _inside(path: str, ancestor: str) -> bool:
    return path == ancestor or path.startswith(ancestor + "/")


class TrackerSimulationTests(unittest.TestCase):
    """随机历史对拍：合法历史零问题，名字表／对象身份／子项计数与真实状态逐步一致，状态改变的判定一致。"""

    NAMES = ("a", "b", "c")
    DIRS = ("d1", "d2", "d3")

    def random_path(self, rng: random.Random, depth_limit: int = 3) -> str:
        parts = [rng.choice(self.DIRS) for _ in range(rng.randint(0, depth_limit - 1))]
        parts.append(rng.choice(self.NAMES + self.DIRS))
        return ROOT + "".join("/" + part for part in parts)

    def existing_path(self, rng: random.Random, fs: "_SimFs") -> str:
        """多数时候选已存在的路径，让 rename／link／删除真正有对象可操作；偶尔选随机路径制造失败探测。"""
        names = [path for path in fs.snapshot() if path != ROOT]
        return rng.choice(names) if names and rng.random() < 0.85 else self.random_path(rng)

    def new_path(self, rng: random.Random, fs: "_SimFs") -> str:
        """已存在目录下的一个随机名字（可能撞上已有名字）：创建、移动目标用。"""
        directories = [path for path, kind in fs.kinds().items() if kind == "directory"]
        if rng.random() < 0.8:
            return rng.choice(directories) + "/" + rng.choice(self.NAMES + self.DIRS)
        return self.random_path(rng)

    def run_history(self, seed: int, steps: int) -> dict[str, int]:
        rng = random.Random(seed)
        fs = _SimFs(ROOT)
        tracker = runtime.OutputTrackerV4(CONTRACT)
        builder = Builder()
        statistics = {"calls": 0, "rename_ok": 0, "link_ok": 0, "overwrite": 0, "failed": 0, "dir_move": 0, "noop": 0,
                      "symlink_ok": 0, "symlink_failed": 0, "lstat_link": 0, "readlink_ok": 0, "unlink_link": 0,
                      "rename_link": 0, "rename_over_link": 0}
        for step in range(steps):
            before = fs.snapshot()
            events = self.apply_random_operation(rng, fs, builder, statistics)
            if not events:
                continue
            statistics["calls"] += 1
            changed = any([tracker.observe_event(event) for event in events])
            after = fs.snapshot()
            context = f"seed={seed} step={step} events={[(e['name'], e['path'][len(ROOT):], e['errno']) for e in events]}"
            self.assertEqual(tracker.report()["problems"], [], context)
            self.assertEqual(changed, before != after, "状态改变的返回值必须与真实状态是否改变一致：" + context)
            self.assert_state_matches(tracker, fs, context)
        return statistics

    def assert_state_matches(self, tracker: runtime.OutputTrackerV4, fs: _SimFs, context: str) -> None:
        snapshot, kinds = fs.snapshot(), fs.kinds()
        self.assertEqual(set(tracker.names), set(snapshot), "名字集合必须一致：" + context)
        for path, kind in kinds.items():
            self.assertEqual(tracker.kinds[tracker.names[path]], kind, f"类型不一致 {path}：" + context)
        # 对象身份：真实状态里指向同一节点的名字，在跟踪器里必须指向同一对象，反之亦然。
        groups_real: dict[int, set[str]] = {}
        groups_tracked: dict[int, set[str]] = {}
        for path, ident in snapshot.items():
            groups_real.setdefault(ident, set()).add(path)
            groups_tracked.setdefault(tracker.names[path], set()).add(path)
        self.assertEqual({frozenset(group) for group in groups_real.values()},
                         {frozenset(group) for group in groups_tracked.values()}, "对象身份分组必须一致：" + context)
        for ident, group in groups_tracked.items():
            self.assertEqual(tracker.links[ident], len(group), "名字个数必须一致：" + context)
        self.assertEqual(set(tracker.kinds), set(groups_tracked), "不应残留已消亡对象：" + context)
        # 符号链接对象：集合与链接文本必须和真实状态一致（文本原样，从不解引用），消亡的链接不应残留文本。
        self.assertEqual({tracker.names[path]: fs.lookup(path).text for path, kind in kinds.items() if kind == "symlink"},
                         tracker.link_text, "符号链接对象与链接文本必须一致：" + context)
        # 目录的直接子项个数；普通文件不应有子项计数。
        for path, kind in kinds.items():
            if kind == "directory":
                self.assertEqual(tracker.entries[path], len(fs.lookup(path).children), f"子项计数不一致 {path}：" + context)
        self.assertEqual(set(tracker.entries), {path for path, kind in kinds.items() if kind == "directory"}, context)

    LINK_TEXTS = ("testing", "../x", "/etc/passwd", "d1/a", "a b", "..")

    def apply_random_operation(self, rng: random.Random, fs: _SimFs, b: Builder,
                               statistics: dict[str, int]) -> list[dict[str, Any]]:
        """随机挑一个操作，按真实语义改 fs，返回应当产生的事件；返回空表示本轮跳过（不属于合法历史的操作不生成）。

        WP-R3 起合法历史里有符号链接：创建、被 unlink、被 rename 与覆盖别的名字、被 lstat／readlink；任何会解引用链接的操作
        （路径的中间分量是链接、对链接名字的 stat／open／chmod 等）都不属于合法历史，在这里跳过——它们由差分对拍专门检验。
        """
        operation = rng.choice(["mkdir", "create", "create", "reuse", "read", "stat", "unlink", "rmdir", "rename", "rename",
                                "rename", "link", "link", "meta", "noreplace", "symlink", "symlink", "lstat", "readlink"])
        path = self.new_path(rng, fs) if operation in {"mkdir", "create", "reuse", "symlink"} else self.existing_path(rng, fs)
        if fs.through_symlink(path):
            return []
        node = fs.lookup(path)
        parent, leaf = fs.parent_of(path)
        parent_is_dir = parent is not None and parent.kind == "directory"
        if operation == "mkdir":
            if node is not None:
                statistics["failed"] += 1
                return b.mkdir(path, ok=False, errno="EEXIST")
            if not parent_is_dir:
                statistics["failed"] += 1
                return b.mkdir(path, ok=False, errno=fs.failure_errno(path))
            parent.children[leaf] = _SimNode("directory")
            return b.mkdir(path)
        if operation in {"create", "reuse"}:
            extra = ("O_EXCL",) if operation == "create" else ("O_TRUNC",)
            if node is not None:
                if operation == "create":
                    statistics["failed"] += 1
                    return b.create(path, extra, ok=False, errno="EEXIST")     # O_EXCL 不跟随：符号链接同样得 EEXIST
                if node.kind != "file":
                    return []  # 对目录以写方式打开会得到 EISDIR；对符号链接会跟随它——都不属于合法历史
                return b.create(path, extra) + b.create(path, extra)
            if not parent_is_dir:
                statistics["failed"] += 1
                return b.create(path, extra, ok=False, errno=fs.failure_errno(path))
            parent.children[leaf] = _SimNode("file")
            return b.create(path, extra) + b.create(path, extra)
        if operation == "symlink":
            text = rng.choice(self.LINK_TEXTS)
            name = rng.choice(["symlink", "symlinkat"])
            if node is not None:
                statistics["symlink_failed"] += 1
                return b.symlink(path, text, name=name, ok=False, errno="EEXIST")
            if not parent_is_dir:
                statistics["symlink_failed"] += 1
                return b.symlink(path, text, name=name, ok=False, errno=fs.failure_errno(path))
            parent.children[leaf] = _SimNode("symlink", text)
            statistics["symlink_ok"] += 1
            return b.symlink(path, text, name=name)
        if operation in {"read", "stat", "lstat"}:
            lstat_name = rng.choice(["lstat", "lstat64", "newfstatat", "fstatat64", "statx"])
            if node is None:
                statistics["failed"] += 1
                errno = fs.failure_errno(path)
                return b.lstat(path, lstat_name, ok=False, errno=errno) if operation == "lstat" else b.probe(path, errno)
            if node.kind == "symlink":
                statistics["lstat_link"] += 1       # 对链接名字只有不跟随的 lstat 是合法的；read／stat 在这里一律改成 lstat
                return b.lstat(path, lstat_name)
            if operation == "lstat":
                return b.lstat(path, lstat_name)
            return b.read(path) if node.kind == "file" and operation == "read" else b.stat(path)
        if operation == "readlink":
            name = rng.choice(["readlink", "readlinkat"])
            if node is None:
                statistics["failed"] += 1
                return b.readlink(path, name, ok=False, errno=fs.failure_errno(path))
            if node.kind == "symlink":
                statistics["readlink_ok"] += 1
                return b.readlink(path, name)
            return b.readlink(path, name, ok=False, errno="EINVAL")           # 不是链接：glibc realpath 常见的 EINVAL
        if operation == "unlink":
            if node is None:
                statistics["failed"] += 1
                return b.unlink(path, ok=False, errno=fs.failure_errno(path))
            if node.kind not in {"file", "symlink"}:
                return []
            if node.kind == "symlink":
                statistics["unlink_link"] += 1
            del parent.children[leaf]
            return b.unlink(path, name=rng.choice(["unlink", "unlinkat"]))
        if operation == "rmdir":
            if node is None:
                statistics["failed"] += 1
                return b.unlink(path, name="rmdir", ok=False, errno=fs.failure_errno(path))
            if node.kind != "directory" or node.children:
                return []  # ENOTDIR／ENOTEMPTY 属于被拒绝的失败
            del parent.children[leaf]
            return b.unlink(path, name="rmdir")
        if operation == "meta":
            return [] if node is None or node.kind == "symlink" else [b.event("utimensat", path)]     # 对链接做 utimensat 会跟随
        # 两路径调用：rename／renameat2(NOREPLACE)／link。
        # 目标：约四成选已存在的路径（覆盖、同对象、NOREPLACE 被拒），其余选已存在目录下的新名字。
        source = path
        destination = self.existing_path(rng, fs) if rng.random() < 0.4 else self.new_path(rng, fs)
        if fs.through_symlink(destination):
            return []
        src, dst = node, fs.lookup(destination)
        dst_parent, dst_leaf = fs.parent_of(destination)
        dst_parent_is_dir = dst_parent is not None and dst_parent.kind == "directory"
        if operation == "link":
            if src is None:
                statistics["failed"] += 1
                return b.link(source, destination, ok=False, errno=fs.failure_errno(source))
            if src.kind != "file":
                return []                                    # 目录不能硬链接；硬链接符号链接这里一律不生成（跟踪器拒绝）
            if dst is not None:
                statistics["failed"] += 1
                return b.link(source, destination, ok=False, errno="EEXIST")
            if not dst_parent_is_dir:
                statistics["failed"] += 1
                return b.link(source, destination, ok=False, errno=fs.failure_errno(destination))
            dst_parent.children[dst_leaf] = src
            statistics["link_ok"] += 1
            return b.link(source, destination)
        noreplace = operation == "noreplace"
        flags = ("RENAME_NOREPLACE",) if noreplace else ()
        name = "renameat2" if noreplace else rng.choice(["rename", "renameat"])
        if src is None:
            statistics["failed"] += 1
            return b.rename(source, destination, name=name, flags=flags, ok=False, errno=fs.failure_errno(source))
        if noreplace and dst is not None:
            # 内核先查目标是否存在：存在就是 EEXIST，哪怕和来源是同一个对象。
            statistics["failed"] += 1
            return b.rename(source, destination, name=name, flags=flags, ok=False, errno="EEXIST")
        if not dst_parent_is_dir:
            statistics["failed"] += 1
            return b.rename(source, destination, name=name, flags=flags, ok=False, errno=fs.failure_errno(destination))
        if dst is src:
            statistics["noop"] += 1
            return b.rename(source, destination, name=name, flags=flags)   # 同一对象的两个名字之间：成功但什么也不改
        if src.kind == "directory" and _inside(destination, source):
            return []
        if dst is not None:
            # 目录只能覆盖空目录，非目录（普通文件或符号链接）只能覆盖非目录——普通文件与符号链接可以互相覆盖。
            if (dst.kind == "directory") != (src.kind == "directory") or (dst.kind == "directory" and dst.children):
                return []
            statistics["overwrite"] += 1
            if dst.kind == "symlink":
                statistics["rename_over_link"] += 1
        if src.kind == "directory":
            statistics["dir_move"] += 1
        if src.kind == "symlink":
            statistics["rename_link"] += 1
        src_parent, src_leaf = fs.parent_of(source)
        del src_parent.children[src_leaf]
        dst_parent.children[dst_leaf] = src
        statistics["rename_ok"] += 1
        return b.rename(source, destination, name=name, flags=flags)

    def test_random_valid_histories_match_the_real_filesystem_semantics(self) -> None:
        totals = {"calls": 0, "rename_ok": 0, "link_ok": 0, "overwrite": 0, "dir_move": 0, "noop": 0, "failed": 0,
                  "symlink_ok": 0, "symlink_failed": 0, "lstat_link": 0, "readlink_ok": 0, "unlink_link": 0, "rename_link": 0,
                  "rename_over_link": 0}
        for seed in range(40):
            statistics = self.run_history(seed, 400)
            for key in totals:
                totals[key] += statistics[key]
        # 随机序列必须真的覆盖到各类分支，否则这个对拍是空转。
        self.assertGreater(totals["rename_ok"], 300, totals)
        self.assertGreater(totals["link_ok"], 100, totals)
        self.assertGreater(totals["overwrite"], 20, totals)
        self.assertGreater(totals["dir_move"], 20, totals)
        self.assertGreater(totals["failed"], 1000, totals)
        # WP-R3：符号链接的各类分支也必须真的被覆盖到。
        self.assertGreater(totals["symlink_ok"], 300, totals)
        self.assertGreater(totals["symlink_failed"], 100, totals)
        self.assertGreater(totals["lstat_link"], 100, totals)
        self.assertGreater(totals["readlink_ok"], 50, totals)
        self.assertGreater(totals["unlink_link"], 50, totals)
        self.assertGreater(totals["rename_link"], 30, totals)
        self.assertGreater(totals["rename_over_link"], 5, totals)


if __name__ == "__main__":
    unittest.main()
