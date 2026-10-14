"""宿主快照 v3（步骤 3）：按单元实际访问方式比较，宿主噪声不再让单元作废。

背景：v2 快照整份相等，含 atime／mtime／ctime／inode，目录还绑全部直接条目。ARM64 上监控程序在根目录写文件、relatime 的
24 小时 atime 刷新，都让“只 stat 了根目录判类型”的单元整次作废。v3 的口径（读集审计模块 ``read_audit`` 的 v3 一节）：

* 只 stat／打开的目录不绑目录项；枚举过（getdents64）的目录必比目录项；读或执行过的文件必比内容；
* 类型、符号链接链、祖先目录、权限、属主（文件加大小）无论怎样访问都比；
* atime／mtime／ctime／inode／device 只对策略 ``metadata_dependencies`` 声明了依赖的路径比；
* v2 条目与历史重放保持原样，control 四链继续产出并重放 v2。

本文件全部用真实临时目录与文件（os.utime、增删条目、chmod、改内容），每个口径正反各一；Linux 命名空间里的 run_child 无法在
本机实跑，其新增判定拆成纯函数在这里直接测。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import control_read_audit as control_audit
from tools.ci import entry_gates as eg
from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.ci import unit_executor as ue
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_read_audit as base_audit
from tools.official_client_capture.tests import unit_inherit_fixture as fixture
from tools.official_client_capture.tests.test_ci_entry_gates import LAUNCHER, _tree
from tools.official_client_capture.tests.test_ci_read_audit_v4_trace import AuditFixture

V2 = "read-audit-host-snapshot/v2"
V3 = "read-audit-host-snapshot/v3"


def snapshot(path: Path, dependency: dict | None = None) -> dict:
    """某路径现在的 v3 载荷（比较函数吃的是载荷，不是条目）。"""
    return ra.host_snapshot_v3(str(path), dependency)["detail"]["snapshot"]


def matches(planned: dict, current: dict, kinds, fields=()) -> bool:
    return ra.host_snapshot_matches(planned, current, kinds, fields)[0]


class TempTree(unittest.TestCase):
    """带一棵真实临时目录的测试基类：一个有两个条目的目录、一个 8 字节文件。"""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.dir = self.root / "dir"
        self.dir.mkdir()
        (self.dir / "a.txt").write_text("甲", encoding="utf-8")
        (self.dir / "b.txt").write_text("乙", encoding="utf-8")
        self.file = self.root / "input.txt"
        self.file.write_text("12345678", encoding="utf-8")


class HostSnapshotComparisonTests(TempTree):
    """比较函数 ``host_snapshot_matches`` 的口径：验收的四个例子，加祖先、符号链接与缺失路径。"""

    def test_stat_only_directory_ignores_entry_changes_and_time_noise(self) -> None:
        """例子一：单元只 stat（或打开、读）目录判类型——增删无关条目、监控程序改时间都不误伤。"""
        planned_entry = ra.host_snapshot_v3(str(self.dir))
        planned = planned_entry["detail"]["snapshot"]
        (self.dir / "unrelated.json").write_text("{}", encoding="utf-8")   # 像 komari 往根目录写 net_static.json
        (self.dir / "a.txt").unlink()
        os.utime(self.dir, ns=(1_000_000_000, 2_000_000_000))
        current_entry = ra.host_snapshot_v3(str(self.dir))
        current = current_entry["detail"]["snapshot"]
        self.assertNotEqual(planned["entries"], current["entries"], "噪声必须真的发生了")
        self.assertNotEqual(planned["volatile"], current["volatile"])
        for kinds in (["stat"], ["dir"], ["stat", "dir"], ["read"], ["stat", "read"], [], None):
            with self.subTest(kinds=kinds):
                self.assertEqual(ra.host_snapshot_matches(planned, current, kinds, ()), (True, ""))
        # 条目摘要只绑无条件比较集：噪声不改它，承接时 inputs_sha256 才不会在语义比较之前就失配。
        self.assertEqual(planned_entry["sha256"], current_entry["sha256"])
        os.chmod(self.dir, 0o750)
        for kinds in (["stat"], ["dir"], ["read"], None):
            with self.subTest(kinds=kinds, change="chmod"):
                ok, reason = ra.host_snapshot_matches(planned, snapshot(self.dir), kinds, ())
                self.assertFalse(ok)
                self.assertIn("权限", reason)

    def test_enumerated_directory_requires_identical_entries(self) -> None:
        """例子二：单元真正枚举目录（getdents64）——增删条目必失效；只改时间不失效。"""
        planned = snapshot(self.dir)
        self.assertTrue(matches(planned, snapshot(self.dir), ["list", "stat"]))
        os.utime(self.dir, ns=(3_000_000_000, 4_000_000_000))
        self.assertTrue(matches(planned, snapshot(self.dir), ["list"]), "只改目录时间，条目没变")
        (self.dir / "c.txt").write_text("丙", encoding="utf-8")
        ok, reason = ra.host_snapshot_matches(planned, snapshot(self.dir), ["list", "stat"], ())
        self.assertFalse(ok)
        self.assertIn("目录的直接条目", reason)
        (self.dir / "c.txt").unlink()
        (self.dir / "a.txt").unlink()
        self.assertFalse(matches(planned, snapshot(self.dir), ["list"]), "删除条目同样失效")
        (self.dir / "a.txt").write_text("甲", encoding="utf-8")
        (self.dir / "b.txt").rename(self.dir / "renamed.txt")
        self.assertFalse(matches(planned, snapshot(self.dir), ["list"]), "改名等于删一个加一个")
        self.assertTrue(matches(planned, snapshot(self.dir), ["stat"]), "同一份变化对只 stat 的单元不算数")

    def test_declared_mtime_dependency_invalidates_when_mtime_changes(self) -> None:
        """例子三：单元声明依赖某文件的 mtime——mtime 变则失效；没声明的字段（atime）变不失效。"""
        planned = snapshot(self.file)
        mtime = planned["volatile"]["mtime_ns"]
        os.utime(self.file, ns=(5_000_000_000, mtime + 7_000_000_000))
        current = snapshot(self.file)
        ok, reason = ra.host_snapshot_matches(planned, current, ["read"], ["mtime_ns"])
        self.assertFalse(ok)
        self.assertIn("mtime_ns", reason)
        self.assertTrue(matches(planned, current, ["read"], ()), "没有声明依赖时 mtime 变化不让单元作废")
        # 只改 atime（utime 把 mtime 设回原值）：声明 mtime 的单元不受影响；ctime 会随 utime 变，声明 ctime 的单元失效。
        # Linux 的 inode 时间戳是粗粒度的（一个时钟节拍，几毫秒），紧挨着的两次修改可能得到同一个 ctime；
        # 睡到 ctime 真的变了再断言，避免这个与被测逻辑无关的偶发红。
        baseline = snapshot(self.file)
        for _ in range(100):
            os.utime(self.file, ns=(9_000_000_000, baseline["volatile"]["mtime_ns"]))
            touched = snapshot(self.file)
            if touched["volatile"]["ctime_ns"] != baseline["volatile"]["ctime_ns"]:
                break
            time.sleep(0.02)
        self.assertNotEqual(touched["volatile"]["ctime_ns"], baseline["volatile"]["ctime_ns"])
        self.assertTrue(matches(baseline, touched, ["read"], ["mtime_ns"]))
        self.assertFalse(matches(baseline, touched, ["read"], ["atime_ns"]))
        self.assertFalse(matches(baseline, touched, ["read"], ["ctime_ns"]), "utime 会刷新 ctime")
        # 目录也一样：声明依赖目录 mtime 的单元，增加条目就失效，哪怕它只 stat 了这个目录。
        # 先把目录 mtime 设成固定的旧值，增加条目才一定让它变（同样是为了躲开粗粒度时间戳）。
        os.utime(self.dir, ns=(1_000_000_000, 1_000_000_000))
        directory = snapshot(self.dir)
        (self.dir / "late.txt").write_text("晚", encoding="utf-8")
        self.assertTrue(matches(directory, snapshot(self.dir), ["stat"], ()))
        self.assertFalse(matches(directory, snapshot(self.dir), ["stat"], ["mtime_ns"]))

    def test_declared_inode_dependency_sees_atomic_replacement(self) -> None:
        planned = snapshot(self.file)
        replacement = self.root / "input.new"
        replacement.write_text("12345678", encoding="utf-8")
        os.replace(replacement, self.file)
        current = snapshot(self.file)
        self.assertNotEqual(planned["volatile"]["inode"], current["volatile"]["inode"])
        self.assertTrue(matches(planned, current, ["read"], ()), "内容、权限、大小都没变，不声明就不比 inode")
        self.assertFalse(matches(planned, current, ["read"], ["inode"]))

    def test_content_change_invalidates_readers_but_not_stat_only_units(self) -> None:
        """例子四之一：文件内容变（大小不变，排除靠大小间接发现）让读取或执行过它的单元失效，只 stat 的单元不受影响。"""
        planned = snapshot(self.file)
        self.file.write_text("ABCDEFGH", encoding="utf-8")
        current = snapshot(self.file)
        self.assertEqual(planned["core"], current["core"], "同样大小，核心属性没变")
        for kinds in (["read"], ["exec"], ["read", "stat"], ["exec", "read"]):
            with self.subTest(kinds=kinds):
                ok, reason = ra.host_snapshot_matches(planned, current, kinds, ())
                self.assertFalse(ok)
                self.assertIn("文件内容", reason)
        for kinds in (["stat"], [], None):
            with self.subTest(kinds=kinds):
                self.assertTrue(matches(planned, current, kinds))
        self.file.write_text("更长的内容", encoding="utf-8")
        self.assertFalse(matches(planned, snapshot(self.file), ["stat"]), "大小属于核心属性，只 stat 也看得到")

    def test_permission_and_ownership_changes_invalidate_every_access_kind(self) -> None:
        """例子四之二：权限（以及属主）变了，相关单元无论怎样访问都失效。"""
        planned = snapshot(self.file)
        os.chmod(self.file, 0o600)
        changed = snapshot(self.file)
        for kinds in (["read"], ["stat"], [], None, ["exec"]):
            with self.subTest(kinds=kinds):
                ok, reason = ra.host_snapshot_matches(planned, changed, kinds, ())
                self.assertFalse(ok)
                self.assertIn("权限", reason)
        # 本机没法 chown 成别人：直接改载荷里的 uid／gid 模拟属主变化。
        for field in ("uid", "gid"):
            forged = copy.deepcopy(planned)
            forged["core"][field] += 1
            with self.subTest(owner=field):
                self.assertFalse(matches(planned, forged, ["stat"]))
                self.assertFalse(matches(forged, planned, None))

    def test_ancestors_symlinks_and_missing_state_are_always_compared(self) -> None:
        # 祖先目录的权限变化：路径解析要穿过它，所以不论访问方式都比。
        child = self.dir / "a.txt"
        planned = snapshot(child)
        os.chmod(self.dir, 0o750)
        for kinds in (["read"], ["stat"], None):
            with self.subTest(kinds=kinds, case="ancestor"):
                self.assertFalse(matches(planned, snapshot(child), kinds))
        os.chmod(self.dir, 0o755)
        self.assertTrue(matches(planned, snapshot(child), ["read"]))
        # 符号链接：目标变了失效；链接自身的时间变了不失效（解析路径刷新它们是常态）。
        first, second = self.root / "first.txt", self.root / "second.txt"
        first.write_text("相同字节", encoding="utf-8")
        second.write_text("相同字节", encoding="utf-8")
        link = self.root / "link"
        link.symlink_to(first)
        planned_link = snapshot(link)
        self.assertEqual(sorted(planned_link["links"][0]), ["gid", "mode", "path", "size", "target", "uid"],
                         "链接链不含自身时间、inode、device")
        os.utime(link, ns=(1_000_000_000, 2_000_000_000), follow_symlinks=False)
        self.assertTrue(matches(planned_link, snapshot(link), ["read"], ()), "链接自身时间变化不失效")
        link.unlink()
        link.symlink_to(second)
        ok, reason = ra.host_snapshot_matches(planned_link, snapshot(link), ["read"], ())
        self.assertFalse(ok, "链接改指向（内容相同的另一个文件）必须失效")
        self.assertTrue(reason)
        # 缺失路径：计划期缺失、运行后出现必须失效；仍然缺失不失效。
        absent = self.root / "absent.txt"
        planned_absent = snapshot(absent)
        self.assertEqual(planned_absent["kind"], "missing")
        self.assertTrue(matches(planned_absent, snapshot(absent), ["missing"]))
        absent.write_text("现在存在了", encoding="utf-8")
        ok, reason = ra.host_snapshot_matches(planned_absent, snapshot(absent), ["missing"], ())
        self.assertFalse(ok)
        self.assertIn("类型", reason)
        absent.unlink()
        # 祖先目录整个不存在：祖先后来出现（即使目标仍缺失）也算变化，从严。
        deep = self.root / "gone" / "sub" / "x"
        planned_deep = snapshot(deep)
        (self.root / "gone").mkdir()
        self.assertFalse(matches(planned_deep, snapshot(deep), ["missing"]))

    def test_unknown_declared_field_fails_closed_and_unknown_access_means_core_only(self) -> None:
        planned = snapshot(self.file)
        self.assertEqual(ra.host_snapshot_matches(planned, planned, ["read"], ["bogus_ns"])[0], False)
        self.file.write_text("ABCDEFGH", encoding="utf-8")
        content_changed = snapshot(self.file)
        self.assertTrue(matches(planned, content_changed, None), "访问方式未知（启动时）只比核心集，不比内容")
        self.assertFalse(matches(planned, content_changed, ["read"]))
        self.file.write_text("短", encoding="utf-8")
        self.assertFalse(matches(planned, snapshot(self.file), None), "核心集（含大小）启动时就要一致")

    def test_identical_comparison_is_the_strictest_fallback(self) -> None:
        planned = snapshot(self.file)
        self.assertTrue(ra.host_snapshot_identical(planned, snapshot(self.file))[0])
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        self.assertFalse(ra.host_snapshot_identical(planned, snapshot(self.file))[0], "拿不到完整轨迹时时间也要一致（失败关闭）")


class HostSnapshotEntryTests(TempTree):
    """v3 条目本身：摘要口径、合同版本、自洽复核、按条目版本重取快照。"""

    def test_entry_digest_binds_only_the_unconditional_comparison_set(self) -> None:
        entry = ra.host_snapshot_v3(str(self.file))
        self.assertEqual(entry["detail"]["schema_version"], V3)
        self.assertEqual(entry["name"], "host:" + str(self.file))
        self.assertNotEqual(entry["sha256"], ra.host_snapshot(str(self.file))["sha256"], "v3 摘要与 v2 摘要不会碰撞")
        # 噪声（时间、同样大小的内容变化、目录条目）不改摘要……
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        self.file.write_text("ABCDEFGH", encoding="utf-8")
        self.assertEqual(ra.host_snapshot_v3(str(self.file))["sha256"], entry["sha256"])
        # ……核心集的变化（权限、大小）改摘要。
        os.chmod(self.file, 0o600)
        self.assertNotEqual(ra.host_snapshot_v3(str(self.file))["sha256"], entry["sha256"])
        directory = ra.host_snapshot_v3(str(self.dir))["sha256"]
        (self.dir / "late.txt").write_text("晚", encoding="utf-8")
        self.assertEqual(ra.host_snapshot_v3(str(self.dir))["sha256"], directory, "目录项不进摘要")

    def test_declared_dependency_is_part_of_the_digest(self) -> None:
        plain = ra.host_snapshot_v3(str(self.file))
        declared = ra.host_snapshot_v3(str(self.file), {"fields": ["mtime_ns"], "basis": "tools/x.py:12 以 mtime 判断缓存新旧"})
        self.assertNotEqual(plain["sha256"], declared["sha256"], "声明进摘要：声明变化让旧记录失效")
        self.assertEqual(declared["detail"]["metadata_dependency"],
                         {"fields": ["mtime_ns"], "basis": "tools/x.py:12 以 mtime 判断缓存新旧"})
        other_basis = ra.host_snapshot_v3(str(self.file), {"fields": ["mtime_ns"], "basis": "另一个依据"})
        other_fields = ra.host_snapshot_v3(str(self.file), {"fields": ["mtime_ns", "inode"], "basis": "tools/x.py:12 以 mtime 判断缓存新旧"})
        self.assertEqual(len({declared["sha256"], other_basis["sha256"], other_fields["sha256"]}), 3)
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        moved = ra.host_snapshot_v3(str(self.file), {"fields": ["mtime_ns"], "basis": "tools/x.py:12 以 mtime 判断缓存新旧"})
        self.assertNotEqual(moved["sha256"], declared["sha256"], "被声明字段的值进摘要")
        self.assertEqual(ra.host_snapshot_v3(str(self.file))["sha256"], plain["sha256"], "没声明的 mtime 不进摘要")
        # 字段去重排序；非法声明拒绝。
        self.assertEqual(ra.host_snapshot_v3(str(self.file), {"fields": ["inode", "mtime_ns"], "basis": "x"})["detail"]
                         ["metadata_dependency"]["fields"], ["inode", "mtime_ns"])
        for invalid in ({"fields": [], "basis": "x"}, {"fields": ["mtime_ns"], "basis": ""}, {"fields": ["mtime_ns"], "basis": "  "},
                        {"fields": ["mtime"], "basis": "x"}, {"fields": ["mtime_ns", "mtime_ns"], "basis": "x"},
                        {"fields": "mtime_ns", "basis": "x"}, {"fields": ["mtime_ns"]}, {"fields": ["mtime_ns"], "basis": 1},
                        {"fields": ["mtime_ns"], "basis": "x", "path": "/x"}, "mtime_ns"):
            with self.subTest(invalid=invalid), self.assertRaises(ra.AuditError):
                ra.host_snapshot_v3(str(self.file), invalid)

    def test_v3_payload_groups_and_reuse_of_the_v2_path_checks(self) -> None:
        file_snapshot, directory_snapshot = snapshot(self.file), snapshot(self.dir)
        self.assertEqual(sorted(file_snapshot), ["ancestors", "content_sha256", "core", "kind", "links", "target", "volatile"])
        self.assertEqual(sorted(directory_snapshot), ["ancestors", "core", "entries", "kind", "links", "target", "volatile"])
        self.assertEqual(sorted(file_snapshot["core"]), ["gid", "mode", "size", "uid"])
        self.assertEqual(sorted(directory_snapshot["core"]), ["gid", "mode", "uid"], "目录大小随条目变化，不进核心集")
        self.assertEqual(sorted(file_snapshot["volatile"]), sorted(ra.VOLATILE_FIELDS))
        self.assertEqual(sorted(snapshot(self.root / "none")), ["ancestors", "kind", "links", "target"])
        v2 = ra.host_snapshot(str(self.file))["detail"]["snapshot"]
        self.assertEqual(file_snapshot["content_sha256"], v2["content_sha256"])
        self.assertEqual(file_snapshot["core"]["size"], v2["metadata"]["size"])
        for path in ("/proc/self/status", "/dev/null", "/sys/kernel", "relative", "/root/../etc/passwd"):
            with self.subTest(path=path), self.assertRaises(ra.AuditError):
                ra.host_snapshot_v3(path)

    def test_entry_consistency_check_rejects_tampered_or_malformed_entries(self) -> None:
        entry = ra.host_snapshot_v3(str(self.file), {"fields": ["mtime_ns"], "basis": "依据"})
        self.assertIs(type(ra.check_host_entry_v3(entry)), dict)
        mutations = {
            "核心属性": lambda value: value["detail"]["snapshot"]["core"].__setitem__("mode", 0),
            "条目摘要": lambda value: value.__setitem__("sha256", "0" * 64),
            "名字": lambda value: value.__setitem__("name", "host:/other"),
            "声明依据": lambda value: value["detail"]["metadata_dependency"].__setitem__("basis", "被改写的依据"),
            "声明字段": lambda value: value["detail"]["metadata_dependency"].__setitem__("fields", ["inode"]),
            "删掉声明": lambda value: value["detail"].pop("metadata_dependency"),
            "多余字段": lambda value: value["detail"]["snapshot"].__setitem__("extra", 1),
            "缺字段": lambda value: value["detail"]["snapshot"].pop("volatile"),
            "类型错": lambda value: value["detail"]["snapshot"]["core"].__setitem__("size", "8"),
            "布尔冒充整数": lambda value: value["detail"]["snapshot"]["core"].__setitem__("size", True),
        }
        for label, mutate in mutations.items():
            changed = copy.deepcopy(entry)
            mutate(changed)
            with self.subTest(label=label), self.assertRaises(ra.AuditError):
                ra.check_host_entry_v3(changed)
        # 条目摘要不含内容摘要、目录项和易变元数据（设计如此），所以条目层本身看不出这几项被改写：它们由记录自摘要
        # （sealed record 的 inputs 明细整体入摘要）兜底，见 HostSnapshotRecordTests 里的篡改用例。这里把这个边界写成断言，
        # 免得以后有人误以为条目自洽复核能防内容篡改。
        content_forged = copy.deepcopy(entry)
        content_forged["detail"]["snapshot"]["content_sha256"] = "0" * 64
        self.assertIs(type(ra.check_host_entry_v3(content_forged)), dict)
        volatile_forged = copy.deepcopy(entry)
        volatile_forged["detail"]["snapshot"]["volatile"]["atime_ns"] += 1
        self.assertIs(type(ra.check_host_entry_v3(volatile_forged)), dict)
        self.assertEqual(content_forged["sha256"], entry["sha256"])

    def test_snapshot_like_follows_entry_version_and_declaration(self) -> None:
        dependency = {"fields": ["mtime_ns"], "basis": "依据"}
        v3 = ra.host_snapshot_v3(str(self.file), dependency)
        v2 = ra.host_snapshot(str(self.file))
        again3, again2 = ra.host_snapshot_like(v3), ra.host_snapshot_like(v2)
        self.assertEqual(again3["detail"]["schema_version"], V3)
        self.assertEqual(again3["detail"]["metadata_dependency"], v3["detail"]["metadata_dependency"], "声明随条目沿用")
        self.assertEqual(again3["sha256"], v3["sha256"])
        self.assertEqual(again2, v2, "v2 条目与过去的 host_snapshot(name[5:]) 逐字相同")
        for bad in ({"name": "file:/x"}, {}, {"name": 1}):
            with self.subTest(bad=bad), self.assertRaises(ra.AuditError):
                ra.host_snapshot_like(bad)

    def test_entry_drift_is_core_only_for_v3_and_exact_for_v2(self) -> None:
        """预约中止后重派前的核对（执行器 validate_resume）：此刻没有轨迹，v3 只比核心集与声明字段，v2 整份相等。"""
        v3, v2 = ra.host_snapshot_v3(str(self.dir)), ra.host_snapshot(str(self.dir))
        self.assertIsNone(ra.host_entry_drift(v3))
        self.assertIsNone(ra.host_entry_drift(v2))
        (self.dir / "noise.json").write_text("{}", encoding="utf-8")
        os.utime(self.dir, ns=(1_000_000_000, 2_000_000_000))
        self.assertIsNone(ra.host_entry_drift(v3), "目录项与时间噪声不让预约重派失败")
        self.assertEqual(ra.host_entry_drift(v2), "宿主输入快照漂移", "v2 保持整份相等的旧口径")
        declared = ra.host_snapshot_v3(str(self.dir), {"fields": ["mtime_ns"], "basis": "依据"})
        os.utime(self.dir, ns=(3_000_000_000, 4_000_000_000))
        self.assertIn("mtime_ns", ra.host_entry_drift(declared))
        os.chmod(self.dir, 0o750)
        self.assertIn("权限", ra.host_entry_drift(v3))
        forged = copy.deepcopy(v3)
        forged["detail"]["snapshot"]["core"]["mode"] = 0
        self.assertTrue(ra.host_entry_drift(forged), "被改写的封存条目不自洽，按漂移处理")


class HostSnapshotPolicyTests(TempTree):
    """审计策略的 ``metadata_dependencies``：校验正反例、声明进入输入摘要、v3／v2 的选择点。"""

    def contract(self) -> dict:
        return {"schema_version": runtime.SCHEMA, "temporary_root": str(self.root / "private" / "output"),
                "mask_roots": [], "environment_paths": []}

    def declaration(self, **extra) -> dict:
        return {"host_paths": [str(self.file), str(self.dir)], "require_read_audit": True,
                "runtime_contract": self.contract(), **extra}

    def policy(self, declaration: dict, unit_id: str = "test_alpha") -> dict:
        return {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {unit_id: declaration}}

    def test_valid_dependencies_are_accepted_and_the_key_is_optional(self) -> None:
        plain = self.policy(self.declaration())
        self.assertEqual(ur.validate_audit_policy(plain, {"test_alpha"}), plain, "不带该键：与过去完全一致")
        good = [{"path": str(self.file), "fields": ["mtime_ns", "inode"], "basis": "tools/x.py:30 用 mtime 判断缓存新旧"},
                {"path": str(self.dir), "fields": ["ctime_ns"], "basis": "docs/contract.md 第 4 节：调用方比较目录 ctime"}]
        declared = self.policy(self.declaration(metadata_dependencies=good))
        self.assertEqual(ur.validate_audit_policy(declared, {"test_alpha"}), declared)
        empty = self.policy(self.declaration(metadata_dependencies=[]))
        self.assertEqual(ur.validate_audit_policy(empty, {"test_alpha"}), empty, "空数组等于没有依赖")
        resolved = ur.validate_metadata_dependencies(good, [str(self.file), str(self.dir)])
        self.assertEqual(resolved[str(self.file)], {"fields": ["inode", "mtime_ns"], "basis": good[0]["basis"]}, "字段规范化为有序")

    def test_invalid_dependencies_and_declaration_shapes_are_rejected(self) -> None:
        item = {"path": str(self.file), "fields": ["mtime_ns"], "basis": "依据"}
        invalid = {
            "路径不在 host_paths": [{**item, "path": str(self.root / "other.txt")}],
            "路径重复": [item, {**item, "fields": ["inode"]}],
            "路径不是字符串": [{**item, "path": 3}],
            "字段为空": [{**item, "fields": []}],
            "字段不认识": [{**item, "fields": ["mtime"]}],
            "字段重复": [{**item, "fields": ["mtime_ns", "mtime_ns"]}],
            "字段不是数组": [{**item, "fields": "mtime_ns"}],
            "没有依据": [{**item, "basis": ""}],
            "依据全是空白": [{**item, "basis": " \t\n"}],
            "依据不是字符串": [{**item, "basis": ["x"]}],
            "多余键": [{**item, "extra": 1}],
            "缺键": [{"path": str(self.file), "fields": ["mtime_ns"]}],
            "条目不是对象": ["mtime_ns"],
            "整体不是数组": {"path": str(self.file)},
            "整体是 None": None,
        }
        for label, dependencies in invalid.items():
            with self.subTest(label=label), self.assertRaises(ur.RecordsError):
                ur.validate_audit_policy(self.policy(self.declaration(metadata_dependencies=dependencies)), {"test_alpha"})
        # 键集合仍然严格：未知键、缺必备键、放宽审计要求都拒绝。
        for label, declaration in {
            "未知键": self.declaration(metadata_dependency=[item]),
            "缺 runtime_contract": {key: value for key, value in self.declaration().items() if key != "runtime_contract"},
            "缺 host_paths": {key: value for key, value in self.declaration().items() if key != "host_paths"},
            "不要求审计": self.declaration(**{"require_read_audit": False}),
        }.items():
            with self.subTest(label=label), self.assertRaises(ur.RecordsError):
                ur.validate_audit_policy(self.policy(declaration), {"test_alpha"})

    def test_declaration_enters_the_unit_input_digest(self) -> None:
        """声明变化让输入摘要变化：旧记录因此失效（承接先比 inputs_sha256）。"""
        base = {"host_paths": [str(self.file)], "require_read_audit": True}
        dependency = {"path": str(self.file), "fields": ["mtime_ns"], "basis": "依据甲"}
        digests = {}
        for label, declaration in {
            "没有声明": base,
            "声明 mtime": {**base, "metadata_dependencies": [dependency]},
            "改依据": {**base, "metadata_dependencies": [{**dependency, "basis": "依据乙"}]},
            "改字段": {**base, "metadata_dependencies": [{**dependency, "fields": ["mtime_ns", "inode"]}]},
        }.items():
            entries = ur.declared_inputs(mock.Mock(), declaration)
            digests[label] = ur.entries_sha256(entries)
            host = next(entry for entry in entries if entry["name"].startswith("host:"))
            self.assertEqual(host["detail"]["schema_version"], V3)
            self.assertEqual("metadata_dependency" in host["detail"], label != "没有声明")
        self.assertEqual(len(set(digests.values())), 4, digests)
        # 被声明字段的值进摘要；没声明的 mtime 不进。
        declared = {**base, "metadata_dependencies": [dependency]}
        before_declared, before_plain = ur.entries_sha256(ur.declared_inputs(mock.Mock(), declared)), ur.entries_sha256(ur.declared_inputs(mock.Mock(), base))
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        self.assertNotEqual(ur.entries_sha256(ur.declared_inputs(mock.Mock(), declared)), before_declared)
        self.assertEqual(ur.entries_sha256(ur.declared_inputs(mock.Mock(), base)), before_plain)
        # declared_inputs 自己也校验声明（不依赖策略校验先跑过）。
        with self.assertRaises(ur.RecordsError):
            ur.declared_inputs(mock.Mock(), {**base, "metadata_dependencies": [{**dependency, "path": str(self.dir)}]})

    def test_new_host_snapshot_picks_v3_for_plain_modules_and_v2_for_the_control_adapter(self) -> None:
        plain = ur.new_host_snapshot(str(self.file))
        self.assertEqual(plain["detail"]["schema_version"], V3)
        private = control_audit._records_module()
        controlled = private.new_host_snapshot(str(self.file))
        self.assertEqual(controlled["detail"]["schema_version"], V2)
        self.assertEqual(controlled, ra.host_snapshot(str(self.file)), "control 适配器路径与改动前的 host_snapshot 逐字相同")
        with self.assertRaisesRegex(private.RecordsError, "control"):
            private.new_host_snapshot(str(self.file), {"fields": ["mtime_ns"], "basis": "依据"})
        declared = ur.new_host_snapshot(str(self.file), {"fields": ["mtime_ns"], "basis": "依据"})
        self.assertIn("metadata_dependency", declared["detail"])


class HostSnapshotAuditTests(TempTree):
    """``strict_audit_reads`` 端到端：合成 strace 行 → 轨迹 → 与 v3 条目核对（不带隔离合同的严格轨迹）。"""

    def trace(self, *lines: str) -> dict:
        return ra.filter_stream(list(lines), ["/"], strict=True)

    @staticmethod
    def stat_line(path: Path) -> str:
        return f'1 newfstatat(AT_FDCWD</repo>, "{path}", 0x1, 0) = 0'

    @staticmethod
    def read_line(path: Path) -> str:
        return f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY) = 3<{path}>'

    @staticmethod
    def list_line(path: Path) -> str:
        return f'1 getdents64(3<{path}>, 0x1 /* 3 entries */, 32768) = 80'

    @staticmethod
    def audit(document: dict, entries: list, **options) -> dict:
        return ra.strict_audit_reads(document, entries, repo_root="/repo", data_root=None, **options)

    def test_stat_only_root_like_directory_survives_noise_and_still_binds_type_and_permissions(self) -> None:
        entries = [ra.host_snapshot_v3(str(self.dir))]
        document = self.trace(self.stat_line(self.dir))
        self.assertEqual(dict(document["accesses"])[str(self.dir)], ["stat"])
        self.assertTrue(self.audit(document, entries)["coverage_complete"])
        (self.dir / "net_static.json").write_text("{}", encoding="utf-8")
        (self.dir / "a.txt").unlink()
        os.utime(self.dir, ns=(1_000_000_000, 2_000_000_000))
        self.assertTrue(self.audit(document, entries)["coverage_complete"], "增删无关条目、改时间都不误伤")
        os.chmod(self.dir, 0o750)
        report = self.audit(document, entries)
        self.assertFalse(report["coverage_complete"], "权限变了必须失效")
        self.assertIn("权限", report["undeclared"][0]["suggestion"])

    def test_listed_directory_fails_when_entries_change(self) -> None:
        entries = [ra.host_snapshot_v3(str(self.dir))]
        document = self.trace(self.stat_line(self.dir), self.list_line(self.dir))
        self.assertEqual(dict(document["accesses"])[str(self.dir)], ["list", "stat"])
        self.assertTrue(self.audit(document, entries)["coverage_complete"])
        os.utime(self.dir, ns=(1_000_000_000, 2_000_000_000))
        self.assertTrue(self.audit(document, entries)["coverage_complete"], "只改时间：枚举过的目录也不失效")
        (self.dir / "added.txt").write_text("新", encoding="utf-8")
        report = self.audit(document, entries)
        self.assertFalse(report["coverage_complete"])
        self.assertIn("目录的直接条目", report["undeclared"][0]["suggestion"])

    def test_read_file_fails_on_content_or_permission_change_but_not_on_time_noise(self) -> None:
        entries = [ra.host_snapshot_v3(str(self.file))]
        document = self.trace(self.read_line(self.file))
        self.assertEqual(dict(document["accesses"])[str(self.file)], ["read"])
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        self.assertTrue(self.audit(document, entries)["coverage_complete"], "时间噪声不让读过它的单元作废")
        self.file.write_text("ABCDEFGH", encoding="utf-8")
        self.assertFalse(self.audit(document, entries)["coverage_complete"], "内容变了（大小不变）必须失效")
        self.file.write_text("12345678", encoding="utf-8")
        self.assertTrue(self.audit(document, entries)["coverage_complete"])
        os.chmod(self.file, 0o600)
        self.assertFalse(self.audit(document, entries)["coverage_complete"], "权限变了必须失效")

    def test_declared_mtime_dependency_fails_the_unit_when_mtime_changes(self) -> None:
        entries = [ra.host_snapshot_v3(str(self.file), {"fields": ["mtime_ns"], "basis": "tools/x.py:30 按 mtime 判缓存"})]
        document = self.trace(self.read_line(self.file))
        self.assertTrue(self.audit(document, entries)["coverage_complete"])
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        report = self.audit(document, entries)
        self.assertFalse(report["coverage_complete"])
        self.assertIn("mtime_ns", report["undeclared"][0]["suggestion"])

    def test_v2_and_v3_entries_coexist_and_v2_stays_exact(self) -> None:
        """版本分派：v2 条目走旧逻辑（整份相等，时间也算），v3 条目走新逻辑（噪声不算）；两者可以同处一份输入。"""
        other = self.root / "other.txt"
        other.write_text("v2 路径", encoding="utf-8")
        entries = [ra.host_snapshot(str(other)), ra.host_snapshot_v3(str(self.file))]
        document = self.trace(self.read_line(other), self.read_line(self.file))
        self.assertTrue(self.audit(document, entries)["coverage_complete"])
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        self.assertTrue(self.audit(document, entries)["coverage_complete"], "v3 路径的时间噪声不算")
        os.utime(other, ns=(1_000_000_000, 2_000_000_000))
        report = self.audit(document, entries)
        self.assertFalse(report["coverage_complete"], "v2 路径的时间变化照旧算漂移")
        # 漂移的路径既报一条“快照漂移”，也因为没能取得绑定而报一条“缺少精确快照”（v2 一直如此）；只有 v2 的路径出问题。
        self.assertEqual({item["path"] for item in report["undeclared"]}, {str(other)})
        self.assertTrue(any("漂移" in item["suggestion"] for item in report["undeclared"]))
        # 历史重放：两种封存条目都只校验自身，不读宿主。
        self.assertTrue(self.audit(document, entries, historical=True)["coverage_complete"])
        forged_v2 = copy.deepcopy(entries)
        forged_v2[0]["detail"]["snapshot"]["content_sha256"] = "0" * 64
        self.assertFalse(self.audit(document, forged_v2, historical=True)["coverage_complete"])
        forged_v3 = copy.deepcopy(entries)
        forged_v3[1]["detail"]["snapshot"]["core"]["mode"] = 0
        self.assertFalse(self.audit(document, forged_v3, historical=True)["coverage_complete"])
        forged_v3[1]["detail"]["schema_version"] = "read-audit-host-snapshot/v1"
        self.assertFalse(self.audit(document, forged_v3, historical=True)["coverage_complete"], "不认识的合同版本不能充当绑定")

    def test_access_kinds_beyond_the_binding_are_still_refused(self) -> None:
        """v3 放宽的是“比较什么”，不是“允许什么访问”：写、目录项枚举文件、读缺失路径等仍超出绑定。"""
        entries = [ra.host_snapshot_v3(str(self.file))]
        written = self.trace(f'1 openat(AT_FDCWD</repo>, "{self.file}", O_RDWR) = 3<{self.file}>')
        self.assertFalse(self.audit(written, entries)["coverage_complete"])
        listed = self.trace(self.list_line(self.file))
        self.assertFalse(self.audit(listed, entries)["coverage_complete"], "文件没有目录项可枚举")
        unbound = self.trace(self.read_line(self.file), self.read_line(self.dir / "a.txt"))
        report = self.audit(unbound, entries)
        self.assertFalse(report["coverage_complete"])
        self.assertEqual([item["path"] for item in report["undeclared"]], [str(self.dir / "a.txt")], "未登记路径不被已登记路径覆盖")
        absent = self.root / "absent.txt"
        probe = self.trace(f'1 openat(AT_FDCWD</repo>, "{absent}", O_RDONLY) = -1 ENOENT (No such file or directory)')
        self.assertTrue(self.audit(probe, [ra.host_snapshot_v3(str(absent))])["coverage_complete"])
        absent.write_text("出现了", encoding="utf-8")
        self.assertFalse(self.audit(probe, [ra.host_snapshot_v3(str(absent))])["coverage_complete"], "缺失路径必须仍然缺失")

    def test_incomplete_trace_never_relaxes_the_comparison(self) -> None:
        """轨迹不完整时访问方式不可信：整份相等的兜底，而且结论本来就是不可承接，不能抛异常。"""
        entries = [ra.host_snapshot_v3(str(self.dir))]
        document = self.trace(self.stat_line(self.dir))
        document["coverage"]["unresolved"] = 1
        with self.assertRaises(ra.AuditError):
            ra.trace_access_modes(document)
        self.assertFalse(self.audit(document, entries)["coverage_complete"])
        for broken in ({"roots": ["/repo"]}, {"lines": 0}, {"accesses": []}):
            partial = {**self.trace(self.stat_line(self.dir)), **broken}
            with self.subTest(broken=broken), self.assertRaises(ra.AuditError):
                ra.trace_access_modes(partial)
        duplicated = self.trace(self.stat_line(self.dir))
        duplicated["accesses"] = [*duplicated["accesses"], *duplicated["accesses"]]
        with self.assertRaises(ra.AuditError):
            ra.trace_access_modes(duplicated)
        complete = self.trace(self.stat_line(self.dir), self.read_line(self.file))
        self.assertEqual(ra.trace_access_modes(complete), {str(self.dir): ("stat",), str(self.file): ("read",)})
        modes = ra.host_path_modes(complete, [ra.host_snapshot_v3(str(self.dir)), ra.host_snapshot_v3(str(self.root / "unused"))])
        self.assertEqual(modes, {str(self.dir): ["stat"], str(self.root / "unused"): []}, "没被访问的路径记空列表")
        self.assertEqual(ra.host_path_modes(complete, [ra.host_snapshot(str(self.dir))]), {}, "纯 v2 条目没有访问类型要封存")
        with self.assertRaises(ra.AuditError):
            ra.host_path_modes(document, entries)


class HostSnapshotRuntimeTests(AuditFixture):
    """隔离运行合同里的宿主快照 v3：完成凭证、重放（v3／v4 两种轨迹合同）与 ``run_child`` 的启动／结束判定。

    ``run_child`` 要真实的 Linux 挂载命名空间，本机跑不了；它的新增判定拆成了纯函数 ``_host_baseline``／``_host_final``／
    ``_host_unchanged``，这里直接用真实文件与真实轨迹文档测。"""

    def setUp(self) -> None:
        super().setUp()
        self.host = self.base / "host"
        self.host.mkdir()
        self.dir = self.host / "dir"
        self.dir.mkdir()
        (self.dir / "a.txt").write_text("甲", encoding="utf-8")
        self.file = self.host / "input.txt"
        self.file.write_text("12345678", encoding="utf-8")
        self.audit_module = runtime._read_audit()

    def entries(self, dependency: dict | None = None) -> list:
        return [ra.host_snapshot_v3(str(self.dir)), ra.host_snapshot_v3(str(self.file), dependency)]

    @staticmethod
    def request(entries: list) -> dict:
        return {"host_entries": entries, "host_paths": [entry["detail"]["path"] for entry in entries]}

    def access_lines(self, *, listing: bool = False) -> list[str]:
        lines = [f'1 newfstatat(AT_FDCWD</repo>, "{self.dir}", 0x1, 0) = 0',
                 f'1 openat(AT_FDCWD</repo>, "{self.file}", O_RDONLY) = 3<{self.file}>']
        if listing:
            lines.append(f'1 getdents64(3<{self.dir}>, 0x1 /* 1 entries */, 32768) = 40')
        return lines

    def host_document(self, entries: list, *, contract: str = "v4", listing: bool = False) -> dict:
        """一份带完成凭证的隔离轨迹文档；宿主相关的凭证字段由 run_child 同一个函数 ``_host_completion`` 生成。"""
        document = self.document([*self.prelude(), *self.access_lines(listing=listing)], contract)
        document["runtime"]["completion"].update(runtime._host_completion(self.audit_module, self.request(entries), document))
        return document

    def replay(self, document: dict, entries: list) -> dict:
        return ra.strict_audit_reads(document, [self.entry, *entries], repo_root="/repo", data_root=None)

    def noise(self) -> None:
        (self.dir / "noise.json").write_text("{}", encoding="utf-8")
        os.utime(self.dir, ns=(1_000_000_000, 2_000_000_000))
        os.utime(self.file, ns=(3_000_000_000, 4_000_000_000))

    # ------------------------------------------------------------------ 完成凭证与重放

    def test_replay_accepts_v3_host_entries_under_both_trace_contracts_and_tolerates_noise(self) -> None:
        for contract in ("v3", "v4"):
            with self.subTest(contract=contract):
                entries = self.entries()
                document = self.host_document(entries, contract=contract)
                self.assertEqual(document["schema_version"],
                                 ra.RUNTIME_TRACE_SCHEMA_V4 if contract == "v4" else ra.RUNTIME_TRACE_SCHEMA)
                report = self.replay(document, entries)
                self.assertTrue(report["coverage_complete"], report["undeclared"])
                self.noise()
                self.assertTrue(self.replay(document, entries)["coverage_complete"], "宿主噪声不让单元作废")
                self.file.write_text("ABCDEFGH", encoding="utf-8")
                self.assertFalse(self.replay(document, entries)["coverage_complete"], "读过的文件内容变了必须失效")
                self.file.write_text("12345678", encoding="utf-8")
                (self.dir / "noise.json").unlink()

    def test_enumerated_directory_binds_entries_in_replay(self) -> None:
        entries = self.entries()
        document = self.host_document(entries, listing=True)
        self.assertEqual(document["runtime"]["completion"]["host_path_modes"][str(self.dir)], ["list", "stat"])
        self.assertTrue(self.replay(document, entries)["coverage_complete"])
        (self.dir / "added.txt").write_text("新", encoding="utf-8")
        self.assertFalse(self.replay(document, entries)["coverage_complete"], "枚举过的目录增加条目必须失效")

    def test_completion_must_carry_the_access_modes_the_trace_shows(self) -> None:
        entries = self.entries()
        document = self.host_document(entries)
        self.assertEqual(document["runtime"]["completion"]["host_path_modes"],
                         {str(self.dir): ["stat"], str(self.file): ["read"]})
        missing = copy.deepcopy(document)
        missing["runtime"]["completion"].pop("host_path_modes")
        forged = copy.deepcopy(document)
        forged["runtime"]["completion"]["host_path_modes"][str(self.file)] = ["stat"]   # 把读过的文件说成只 stat 的
        extra = copy.deepcopy(document)
        extra["runtime"]["completion"]["host_path_modes"]["/not/accessed"] = []
        for label, changed in (("缺失", missing), ("按更宽松的访问方式伪造", forged), ("多出路径", extra)):
            with self.subTest(label=label):
                with self.assertRaisesRegex(runtime.RuntimeContractError, "访问类型"):
                    runtime.replay_runtime(changed, [self.entry, *entries])
                self.assertFalse(self.replay(changed, entries)["coverage_complete"])
        for label, digest in (("过期摘要", "0" * 64), ("v2 口径的整份摘要", runtime.digest(entries))):
            changed = copy.deepcopy(document)
            changed["runtime"]["completion"]["host_entries_sha256"] = digest
            with self.subTest(label=label):
                with self.assertRaisesRegex(runtime.RuntimeContractError, "未绑定执行记录"):
                    runtime.replay_runtime(changed, [self.entry, *entries])
        for key in ("host_inputs_unchanged", "command_finished"):
            changed = copy.deepcopy(document)
            changed["runtime"]["completion"][key] = False
            with self.subTest(key=key):
                self.assertFalse(self.replay(changed, entries)["coverage_complete"])

    def test_binding_digest_ignores_detail_differences_but_not_identity(self) -> None:
        audit = self.audit_module
        old = self.entries()
        self.noise()
        self.file.write_text("ABCDEFGH", encoding="utf-8")
        fresh = self.entries()
        self.assertNotEqual(old[1]["detail"]["snapshot"], fresh[1]["detail"]["snapshot"], "详情确实不同")
        self.assertEqual(runtime.host_binding_digest(audit, old), runtime.host_binding_digest(audit, fresh),
                         "承接时新旧条目的详情可以不同，投影摘要相同就是同一份登记")
        os.chmod(self.dir, 0o750)
        self.assertNotEqual(runtime.host_binding_digest(audit, old), runtime.host_binding_digest(audit, self.entries()))
        # 全 v2（含空列表）与历史口径逐字相同。
        v2 = [ra.host_snapshot(str(self.file))]
        self.assertEqual(runtime.host_binding_digest(audit, v2), runtime.digest(v2))
        self.assertEqual(runtime.host_binding_digest(audit, []), runtime.digest([]))
        mixed = [v2[0], old[0]]
        self.assertEqual(runtime.host_binding_digest(audit, mixed),
                         runtime.digest([v2[0], {"name": old[0]["name"], "sha256": old[0]["sha256"], "schema_version": V3}]))

    def test_host_entry_filter_keeps_both_contracts_in_input_order(self) -> None:
        v3, v2 = ra.host_snapshot_v3(str(self.file)), ra.host_snapshot(str(self.dir))
        inputs = [self.entry, ur.value_entry("policy", "require-read-audit", "all-file-paths/v1"), v3, v2]
        self.assertEqual(runtime._host_entries(self.audit_module, inputs), [v3, v2])
        self.assertEqual(runtime._host_entries(self.audit_module, [self.entry]), [])

    # ------------------------------------------------------------------ run_child 的启动判定

    def test_start_of_run_compares_only_the_core_set_and_declared_fields(self) -> None:
        entries = self.entries()
        request = self.request(entries)
        before = runtime._host_baseline(self.audit_module, request)
        self.assertEqual([entry["sha256"] for entry in before], [entry["sha256"] for entry in entries])
        self.noise()
        self.file.write_text("ABCDEFGH", encoding="utf-8")         # 启动时不知道会不会读，所以内容不比
        runtime._host_baseline(self.audit_module, request)
        os.chmod(self.file, 0o600)
        with self.assertRaisesRegex(runtime.RuntimeContractError, r"input\.txt.*权限"):
            runtime._host_baseline(self.audit_module, request)
        os.chmod(self.file, 0o644)
        absent = self.host / "absent.txt"
        absent_request = self.request([ra.host_snapshot_v3(str(absent))])
        runtime._host_baseline(self.audit_module, absent_request)
        absent.write_text("现在存在了", encoding="utf-8")
        with self.assertRaisesRegex(runtime.RuntimeContractError, "类型"):
            runtime._host_baseline(self.audit_module, absent_request)
        declared = self.request(self.entries({"fields": ["mtime_ns"], "basis": "依据"}))
        runtime._host_baseline(self.audit_module, declared)
        os.utime(self.file, ns=(5_000_000_000, 6_000_000_000))
        with self.assertRaisesRegex(runtime.RuntimeContractError, "mtime_ns"):
            runtime._host_baseline(self.audit_module, declared)
        crooked = self.request(self.entries())
        crooked["host_paths"] = list(reversed(crooked["host_paths"]))
        with self.assertRaisesRegex(runtime.RuntimeContractError, "路径"):
            runtime._host_baseline(self.audit_module, crooked)

    def test_pure_v2_requests_keep_the_whole_snapshot_rule_at_start_and_end(self) -> None:
        entries = [ra.host_snapshot(str(self.file))]
        request = self.request(entries)
        before = runtime._host_baseline(self.audit_module, request)
        self.assertEqual(before, entries)
        self.assertEqual(runtime._host_unchanged(self.audit_module, request, before, runtime._host_final(self.audit_module, request), None),
                         (True, ""))
        os.utime(self.file, ns=(1_000_000_000, 2_000_000_000))
        with self.assertRaises(runtime.RuntimeContractError) as caught:
            runtime._host_baseline(self.audit_module, request)
        self.assertEqual(str(caught.exception), "实际命名空间中的输入与登记快照不同", "v2 的错误信息逐字不变")
        after = runtime._host_final(self.audit_module, request)
        self.assertEqual(runtime._host_unchanged(self.audit_module, request, before, after, None), (False, ""))

    # ------------------------------------------------------------------ run_child 的结束判定

    def end_check(self, entries: list, document: dict | None, mutate) -> tuple[bool, str]:
        request = self.request(entries)
        before = runtime._host_baseline(self.audit_module, request)
        mutate()
        return runtime._host_unchanged(self.audit_module, request, before, runtime._host_final(self.audit_module, request), document)

    def test_end_of_run_check_follows_the_access_kinds_from_the_trace(self) -> None:
        stat_and_read = ra.filter_stream(self.access_lines(), ["/"], strict=True)
        listed = ra.filter_stream(self.access_lines(listing=True), ["/"], strict=True)
        # 每一步都以当前现场重新登记（计划期快照），只让“启动之后、结束之前”的那一个变化发生。
        self.assertEqual(self.end_check(self.entries(), stat_and_read, self.noise), (True, ""), "噪声不让运行结束的核对失败")
        self.assertEqual(self.end_check(self.entries(), listed, lambda: os.utime(self.dir, ns=(5_000_000_000, 6_000_000_000))), (True, ""))
        ok, reason = self.end_check(self.entries(), listed, lambda: (self.dir / "added.txt").write_text("新", encoding="utf-8"))
        self.assertFalse(ok)
        self.assertIn("目录的直接条目", reason)
        self.assertTrue(self.end_check(self.entries(), stat_and_read, lambda: (self.dir / "added2.txt").write_text("新", encoding="utf-8"))[0],
                        "只 stat 的目录增加条目不算数")
        ok, reason = self.end_check(self.entries(), stat_and_read, lambda: self.file.write_text("ABCDEFGH", encoding="utf-8"))
        self.assertFalse(ok)
        self.assertIn("文件内容", reason)
        self.file.write_text("12345678", encoding="utf-8")
        ok, reason = self.end_check(self.entries(), stat_and_read, lambda: os.chmod(self.file, 0o600))
        self.assertFalse(ok)
        self.assertIn("权限", reason)

    def test_end_of_run_check_also_compares_registered_against_baseline(self) -> None:
        """登记（计划期）→ 基线（启动时）：启动只比核心集，读过的内容在结束时要补比，不能只比基线↔结束。"""
        planned = self.entries()
        self.file.write_text("ABCDEFGH", encoding="utf-8")        # 计划期之后、启动之前就变了，启动的核心集比较看不出
        stat_and_read = ra.filter_stream(self.access_lines(), ["/"], strict=True)
        ok, reason = self.end_check(planned, stat_and_read, lambda: None)
        self.assertFalse(ok)
        self.assertIn("文件内容", reason)
        stat_only = ra.filter_stream(self.access_lines()[:1], ["/"], strict=True)
        self.assertEqual(self.end_check(planned, stat_only, lambda: None), (True, ""), "没读它的单元不受这一变化影响")

    def test_end_of_run_check_without_a_complete_trace_falls_back_to_the_strictest_rule(self) -> None:
        entries = self.entries()
        incomplete = ra.filter_stream(self.access_lines(), ["/"], strict=True)
        incomplete["coverage"]["unresolved"] = 2
        for document in (None, incomplete):
            ok, reason = self.end_check(entries, document, lambda: os.utime(self.file, ns=(1_000_000_000, 2_000_000_000)))
            with self.subTest(document=document is not None):
                self.assertFalse(ok, "拿不到完整轨迹时时间噪声也算变化")
                self.assertIn("整份", reason)

    def test_end_of_run_check_honours_declared_fields_and_snapshot_failures(self) -> None:
        declared = self.entries({"fields": ["mtime_ns"], "basis": "依据"})
        document = ra.filter_stream(self.access_lines(), ["/"], strict=True)
        ok, reason = self.end_check(declared, document, lambda: os.utime(self.file, ns=(1_000_000_000, 2_000_000_000)))
        self.assertFalse(ok)
        self.assertIn("mtime_ns", reason)
        request = self.request(self.entries())
        before = runtime._host_baseline(self.audit_module, request)
        failing = [dict(item) for item in before]
        failing[1] = {"snapshot_error": "宿主文件在快照期间变化"}
        ok, reason = runtime._host_unchanged(self.audit_module, request, before, failing, document)
        self.assertFalse(ok)
        self.assertIn("取快照失败", reason)
        # 结束时路径被删：取快照得到 missing，类型变了 → 已变。
        ok, reason = self.end_check(self.entries(), document, self.file.unlink)
        self.assertFalse(ok)
        self.assertIn("类型", reason)


class HostSnapshotRecordTests(unittest.TestCase):
    """记录层：封存访问类型、承接时按访问方式补比、篡改与 v2 旧规则。

    用 ``unit_inherit_fixture.World``（一次严格读集审计过的合成来源世界）；来源记录的读集轨迹只读一个宿主文件。承接的做法与
    真实消费端相同：当前声明不含宿主快照，经 ``unit_inherit._carry_host_inputs`` 沿用来源登记的宿主路径并重新快照，再
    ``check_record``。"""

    UNIT = "test_alpha"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.world = fixture.World(Path(self.directory.name) / "world")
        self.host = self.world.host_input

    def v3_inputs(self, dependency: dict | None = None) -> list:
        return ur._unique([*self.world.base_inputs(self.UNIT), ra.host_snapshot_v3(str(self.host), dependency),
                           ur.value_entry("policy", "require-read-audit", "all-file-paths/v1")])

    def audit_block(self, inputs: list, *, trace_sha256: str | None = None, modes: dict | None = None, with_modes: bool = True) -> dict:
        block = {"status": "passed", "coverage_complete": True, "coverage_scope": "all-file-paths/v1", "undeclared_count": 0,
                 "undeclared": [], "inputs_sha256": ur.entries_sha256(inputs), "trace": {"sha256": trace_sha256 or self.world.trace_sha256},
                 "repo_root": str(self.world.root), "data_root": None}
        if with_modes:
            block["host_path_modes"] = modes if modes is not None else {str(self.host): ["read"]}
        return block

    def publish(self, *, inputs: list | None = None, run_id: str = "v3-run", **block_options) -> dict:
        """发布一条 v3 来源记录（封存的访问类型默认与世界里的读轨迹一致）和它所在运行的清单。"""
        inputs = inputs if inputs is not None else self.v3_inputs()
        record = self.world.source_record(self.UNIT, run_id=run_id, inputs=inputs, read_audit=self.audit_block(inputs, **block_options))
        self.world.publish_run(run_id, "re-execute", {self.UNIT: (record, "executed")}, decided=fixture.T0 - 3600)
        return record

    def check(self, record: dict, *, path: Path | None = None, **facts) -> tuple[list[str], ur.Current]:
        current = self.world.current(self.UNIT)
        ui._carry_host_inputs(current, record)
        path = path or self.world.store.record_path(self.UNIT, record["record_sha256"])
        return ur.check_record(self.world.store, path, record, current, self.world.facts(fixture.T0, **facts)), current

    def same_size_change(self) -> None:
        """内容变了、大小不变：核心集（因此条目摘要与 inputs_sha256）不变，只有按访问方式的补比能发现。"""
        data = self.host.read_bytes()
        self.host.write_bytes(bytes([data[0] ^ 1]) + data[1:])

    def test_noise_does_not_block_inheritance_but_an_observed_content_change_does(self) -> None:
        record = self.publish()
        problems, current = self.check(record)
        self.assertEqual(problems, [])
        self.assertEqual([entry["detail"]["schema_version"] for entry in current.inputs if entry["name"].startswith("host:")], [V3])
        os.utime(self.host, ns=(1_000_000_000, 2_000_000_000))
        (self.host.parent / "noise.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.check(record)[0], [], "宿主时间与目录噪声不让记录失去承接资格")
        self.same_size_change()
        problems, current = self.check(record)
        self.assertEqual(current.inputs_sha256, record["inputs_sha256"], "摘要相等——正因为如此承接必须按访问方式补比")
        self.assertTrue(any("文件内容" in problem for problem in problems), problems)

    def test_stat_only_unit_is_not_blocked_by_a_content_change(self) -> None:
        stat_trace = ra.filter_stream([f'1 newfstatat(AT_FDCWD<{self.world.root}>, "{self.host}", 0x1, 0) = 0'], ["/"], strict=True)
        trace_path = self.world.root / "trace-stat.json"
        trace_path.write_text(json.dumps(stat_trace, ensure_ascii=False), encoding="utf-8")
        digest = ur.file_sha256(trace_path)
        self.world.store.put_log(trace_path, digest)
        record = self.publish(trace_sha256=digest, modes={str(self.host): ["stat"]}, run_id="stat-run")
        self.assertEqual(self.check(record)[0], [])
        self.same_size_change()
        self.assertEqual(self.check(record)[0], [], "单元只 stat 了它：内容变化不相干")
        os.chmod(self.host, 0o600)
        self.assertTrue(self.check(record)[0], "权限变化相干")

    def test_sealed_access_modes_are_cross_checked_against_the_trace(self) -> None:
        """记录写入时就带错误的访问类型（比如把读过的文件封成只 stat）：与轨迹重算不符，承接失败关闭。"""
        path = str(self.host)
        cases = {"改成更宽松": {path: ["stat"]}, "多出路径": {path: ["read"], "/not/accessed": []},
                 "少了路径": {}, "类型多了": {path: ["read", "exec"]}, "不是列表": {path: "read"}}
        for index, (label, modes) in enumerate(cases.items()):
            record = self.publish(run_id=f"forged-{index}", modes=modes)
            with self.subTest(label=label):
                problems, _ = self.check(record)
                self.assertTrue(any("访问类型" in problem for problem in problems), problems)
        missing = self.publish(run_id="no-modes", with_modes=False)
        problems, _ = self.check(missing)
        self.assertTrue(any("访问类型" in problem for problem in problems), "v3 记录缺该字段：失败关闭")
        # 把改成更宽松的访问类型和内容变化组合起来也过不了：封存值与轨迹重算值不等，哪怕当前内容恰好“相符”。
        forged = self.publish(run_id="forged-and-changed", modes={path: ["stat"]})
        self.same_size_change()
        problems, _ = self.check(forged)
        self.assertTrue(any("访问类型" in problem for problem in problems), problems)

    def test_editing_the_sealed_snapshot_in_place_is_caught_by_the_record_seal(self) -> None:
        """篡改：把来源记录里 v3 条目封存的 content_sha256 改成当前值，不重算记录自摘要——必须被拒绝。

        条目摘要不含内容摘要（设计如此），所以防这种改写的是记录自摘要（inputs 明细整体入摘要）和 check_record 先验自摘要、
        再核文件名＝自摘要、再核原运行清单把这条记录列为正式执行。"""
        record = self.publish()
        path = self.world.store.record_path(self.UNIT, record["record_sha256"])
        self.same_size_change()
        problems, current = self.check(record)
        self.assertTrue(any("文件内容" in problem for problem in problems), "未篡改时补比能发现内容变了")
        now_content = next(entry for entry in current.inputs if entry["name"].startswith("host:"))["detail"]["snapshot"]["content_sha256"]
        # 原位改写记录库里的文件，并且不重算 record_sha256。
        tampered = json.loads(path.read_text(encoding="utf-8"))
        entry = next(item for item in tampered["inputs"] if item["name"].startswith("host:"))
        self.assertNotEqual(entry["detail"]["snapshot"]["content_sha256"], now_content)
        entry["detail"]["snapshot"]["content_sha256"] = now_content
        path.write_text(json.dumps(tampered, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        loaded = ur._read_json(path)
        self.assertEqual(loaded["record_sha256"], record["record_sha256"], "自摘要没有被重算")
        problems, current = self.check(loaded, path=path)
        self.assertTrue(any("自摘要不符" in problem for problem in problems), problems)
        # 对照：按访问方式的补比本身会被这种改写骗过（封存值已被改成当前值）——挡住它的正是记录自摘要，而不是补比。
        trace = json.loads(self.world.store.log_path(self.world.trace_sha256).read_text(encoding="utf-8"))
        self.assertEqual(ra.host_inputs_inherit_problems(trace, loaded["inputs"], current.inputs, loaded["read_audit"]), [])
        self.assertEqual([problem for problem in problems if "文件内容" in problem], [], "没有内容问题：补比被骗过了")
        # 改 entries（目录）同理；改易变元数据同理。
        for label, mutate in (("易变元数据", lambda item: item["detail"]["snapshot"]["volatile"].__setitem__("mtime_ns", 1)),
                              ("核心属性", lambda item: item["detail"]["snapshot"]["core"].__setitem__("mode", 0))):
            again = json.loads(json.dumps(loaded))
            mutate(next(item for item in again["inputs"] if item["name"].startswith("host:")))
            with self.subTest(label=label):
                self.assertTrue(any("自摘要不符" in problem for problem in self.check(again, path=path)[0]))
        # 改写后重算自摘要、另存为一条新记录：自摘要通过，但原运行清单仍列着旧摘要，不认这条记录。
        forged = ur.seal_record({key: value for key, value in tampered.items() if key != "record_sha256"})
        forged_path = self.world.store.put_record(forged)
        problems, _ = self.check(forged, path=forged_path)
        self.assertFalse(any("自摘要不符" in problem for problem in problems))
        self.assertTrue(any("清单没有把这条记录列为" in problem for problem in problems), problems)

    def test_historical_check_reads_no_host_but_still_checks_the_sealed_modes(self) -> None:
        record = self.publish()
        self.host.write_text("面目全非的新内容", encoding="utf-8")          # 宿主随便怎么变，历史重放只看封存内容
        current = ur.Current(self.UNIT, "test", record["spec"], record["spec_sha256"], list(record["inputs"]),
                             record["inputs_sha256"], True)
        facts = self.world.facts(fixture.T0, historical_read_audit=True)
        path = self.world.store.record_path(self.UNIT, record["record_sha256"])
        self.assertEqual(ur.check_record(self.world.store, path, record, current, facts), [])
        forged = self.publish(run_id="forged-history", modes={str(self.host): ["stat"]})
        forged_path = self.world.store.record_path(self.UNIT, forged["record_sha256"])
        current = ur.Current(self.UNIT, "test", forged["spec"], forged["spec_sha256"], list(forged["inputs"]), forged["inputs_sha256"], True)
        self.assertTrue(any("访问类型" in problem for problem in ur.check_record(self.world.store, forged_path, forged, current, facts)))

    def test_v2_record_keeps_the_old_whole_snapshot_rule(self) -> None:
        """没有 v3 条目的记录（World 默认：v2 条目，read_audit 无 host_path_modes）一律走旧规则：整份相等，时间也算。"""
        record = self.world.records[self.UNIT]
        problems, current = self.check(record)
        self.assertEqual(problems, [])
        self.assertEqual({entry["detail"]["schema_version"] for entry in current.inputs if entry["name"].startswith("host:")}, {V2},
                         "按来源条目的版本重取：v2 来源取 v2")
        self.assertEqual(ra.host_inputs_inherit_problems({}, record["inputs"], current.inputs, record["read_audit"]), [],
                         "补比函数不介入 v2 记录")
        os.utime(self.host, ns=(1_000_000_000, 2_000_000_000))
        problems, _ = self.check(record)
        self.assertTrue(any("输入变了" in problem for problem in problems), problems)

    def test_carry_keeps_the_declaration_of_the_source_entry(self) -> None:
        dependency = {"fields": ["mtime_ns"], "basis": "tools/x.py:30 按 mtime 判缓存"}
        record = self.publish(inputs=self.v3_inputs(dependency), run_id="declared-run")
        problems, current = self.check(record)
        self.assertEqual(problems, [])
        carried = next(entry for entry in current.inputs if entry["name"].startswith("host:"))
        self.assertEqual(carried["detail"]["metadata_dependency"], {"fields": ["mtime_ns"], "basis": dependency["basis"]})
        os.utime(self.host, ns=(1_000_000_000, 2_000_000_000))
        problems, _ = self.check(record)
        self.assertTrue(any("输入变了" in problem for problem in problems), "被声明依赖的 mtime 变了：摘要变，旧记录失效")

    def test_narrowed_scope_proposal_keeps_the_declared_dependencies(self) -> None:
        """收窄提案由来源记录生成；来源条目声明过元数据依赖，提案必须带着声明，否则收窄会悄悄放松对 mtime 的绑定。"""
        dependency = {"fields": ["mtime_ns"], "basis": "tools/x.py:30 按 mtime 判缓存"}
        spec = {"type": "test", "unit_id": self.UNIT, "argv": ["fixture", self.UNIT]}
        trace_path = self.world.store.log_path(self.world.trace_sha256)
        scopes = {}
        for label, declared in (("声明", dependency), ("无声明", None)):
            inputs = self.v3_inputs(declared)
            run_id = f"narrow-{len(scopes)}"
            record = self.world.source_record(self.UNIT, run_id=run_id, inputs=inputs, spec=spec, read_audit=self.audit_block(inputs))
            self.world.publish_run(run_id, "re-execute", {self.UNIT: (record, "executed")}, decided=fixture.T0 - 3600)
            scopes[label] = ra.narrow_input_scope(self.world.store.record_path(self.UNIT, record["record_sha256"]), trace_path)
            scopes[label + "条目"] = next(entry for entry in inputs if entry["name"].startswith("host:"))
        self.assertEqual(scopes["声明"]["inputs"]["metadata_dependencies"],
                         [{"path": str(self.host), "fields": ["mtime_ns"], "basis": dependency["basis"]}])
        self.assertEqual(scopes["声明"]["inputs"]["host_paths"], [str(self.host)])
        self.assertNotIn("metadata_dependencies", scopes["无声明"]["inputs"], "没声明就不带该键：提案形状与过去相同")
        # 提案按 declared_inputs 展开后，宿主条目与来源记录的声明条目摘要相同（声明和被声明字段的值都带过去了）。
        for label in ("声明", "无声明"):
            expanded = next(entry for entry in ur.declared_inputs(mock.Mock(), scopes[label]["inputs"]) if entry["name"].startswith("host:"))
            self.assertEqual(expanded["sha256"], scopes[label + "条目"]["sha256"], label)
            self.assertEqual("metadata_dependency" in expanded["detail"], label == "声明")

    def test_problem_helper_reports_missing_or_extra_v3_entries(self) -> None:
        record = self.publish()
        trace = json.loads(self.world.store.log_path(self.world.trace_sha256).read_text(encoding="utf-8"))
        host = next(entry for entry in record["inputs"] if entry["name"].startswith("host:"))
        others = [entry for entry in record["inputs"] if entry is not host]
        self.assertEqual(ra.host_inputs_inherit_problems(trace, record["inputs"], record["inputs"], record["read_audit"]), [])
        problems = ra.host_inputs_inherit_problems(trace, record["inputs"], others, record["read_audit"])
        self.assertTrue(any("集合不同" in problem for problem in problems), problems)
        problems = ra.host_inputs_inherit_problems(trace, others, record["inputs"], record["read_audit"])
        self.assertTrue(any("集合不同" in problem for problem in problems), problems)
        incomplete = copy.deepcopy(trace)
        incomplete["coverage"]["unresolved"] = 1
        problems = ra.host_inputs_inherit_problems(incomplete, record["inputs"], record["inputs"], record["read_audit"])
        self.assertTrue(any("无法确定访问方式" in problem for problem in problems), problems)


RAW_LINE_FAKE_STRACE = """#!{python}
# 假 strace（测试用）：同 test_ci_read_audit.FAKE_STRACE，但 FAKE_STRACE_READS 里的条目既可以是路径（合成一次只读打开），
# 也可以是整行 strace 输出（不以 / 开头），好让测试造出“只 stat”这类访问方式。
import json, os, subprocess, sys
args = sys.argv[1:]
pipe = args[args.index("-o") + 1]
command = args[args.index("--") + 1:]
items = json.loads(os.environ.get("FAKE_STRACE_READS") or "{{}}").get(os.environ.get("UNIT_EXECUTOR_UNIT", ""), [])
cwd = os.getcwd()
lines = "".join(('4242 openat(AT_FDCWD<%s>, "%s", O_RDONLY|O_CLOEXEC) = 3<%s>\\n' % (cwd, item, item)) if item.startswith("/")
                else item + "\\n" for item in items)
assert pipe.startswith("|")
subprocess.run(["/bin/sh", "-c", pipe[1:]], input=lines.encode("utf-8"), check=True)
sys.exit(subprocess.run(command).returncode)
"""


class HostSnapshotExecutorTests(unittest.TestCase):
    """真跑执行器（假 strace 按单元写合成读取）：普通运行取 v3、记录封存访问类型、噪声之后记录仍可承接。"""

    def test_ordinary_executor_run_seals_modes_and_the_record_survives_host_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            harness = base_audit.ReadAuditExecutorTests("test_audit_is_only_for_re_execute")
            repo = harness._repo(root)
            hostdir = root / "hostdir"
            hostdir.mkdir()
            (hostdir / "keep.txt").write_text("keep", encoding="utf-8")
            probed = hostdir / "input.txt"
            probed.write_text("12345678", encoding="utf-8")
            other = root / "host.txt"
            other.write_text("本轮宿主输入", encoding="utf-8")
            test_file = repo / "tools/official_client_capture/tests/test_alpha.py"
            reads = {"cmd:a": [f'4242 newfstatat(AT_FDCWD<{repo}>, "{hostdir}", 0x1, 0) = 0', str(probed)],
                     "cmd:all": [str(other)], "test_alpha": [str(test_file)]}
            hosts = {"cmd:a": [str(hostdir), str(probed)], "cmd:all": [str(other)], "test_alpha": [str(test_file)]}
            with mock.patch.object(base_audit, "FAKE_STRACE", RAW_LINE_FAKE_STRACE):
                code, summary, stderr = harness._run(root, repo, reads, strict=True, host_paths=hosts)
            self.assertEqual(code, 0, stderr[-3000:])
            self.assertTrue(summary["read_audit"]["coverage_complete"])
            store = ur.RecordStore(root / "store")
            path, record = store.candidates("cmd:a")[0]
            host_entries = [entry for entry in record["inputs"] if entry["name"].startswith("host:")]
            self.assertEqual({entry["detail"]["schema_version"] for entry in host_entries}, {V3}, "普通运行（无 control 适配器）取 v3")
            self.assertEqual(record["read_audit"]["host_path_modes"], {str(hostdir): ["stat"], str(probed): ["read"]},
                             "记录封存了每个宿主路径在已验证轨迹里的访问类型")

            def inherit_problems() -> list[str]:
                fresh = ur._unique([ra.host_snapshot_like(entry) if entry["name"].startswith("host:") else entry for entry in record["inputs"]])
                current = ur.Current("cmd:a", "command", record["spec"], record["spec_sha256"], fresh, ur.entries_sha256(fresh), True)
                facts = ur.RunFacts(record["policy_sha256"], record["environment"], record["environment_sha256"], record["executor"],
                                    1.0, ur.parse_utc(record["completed_at_utc"]), require_read_audit=True)
                return ur.check_record(store, path, record, current, facts)

            self.assertEqual(inherit_problems(), [])
            # 宿主噪声：只 stat 的目录增加条目、改时间；读过的文件改时间。
            (hostdir / "net_static.json").write_text("{}", encoding="utf-8")
            os.utime(hostdir, ns=(1_000_000_000, 2_000_000_000))
            os.utime(probed, ns=(3_000_000_000, 4_000_000_000))
            self.assertEqual(inherit_problems(), [], "噪声之后这条真实执行记录仍可承接")
            probed.write_text("ABCDEFGH", encoding="utf-8")
            self.assertTrue(any("文件内容" in problem for problem in inherit_problems()), "读过的文件内容变了不可承接")


class ControlRegressionTests(AuditFixture):
    """control 四链 sidecar 动态复用 read_audit.py／unit_records.py 的函数对象：既有默认行为字节不变，仍产出并重放 v2。"""

    def setUp(self) -> None:
        super().setUp()
        self.host = self.base / "host.txt"
        self.host.write_text("宿主输入", encoding="utf-8")

    def test_control_adapter_keeps_the_v2_names_next_to_the_new_ones(self) -> None:
        self.assertEqual(control_audit.HOST_SCHEMA, V2)
        v2 = control_audit.host_snapshot(str(self.host))
        self.assertEqual(v2["detail"]["schema_version"], V2)
        self.assertEqual(v2, ra.host_snapshot(str(self.host)), "control 的 host_snapshot 与 read_audit 的逐字相同")
        self.assertIn("metadata", v2["detail"]["snapshot"], "v2 载荷形状不变")
        self.assertEqual(control_audit.HOST_SCHEMA_V3, V3, "新名字被一起复制进来，但 control 入口不使用它")
        self.assertIsNot(control_audit.strict_audit_reads, ra.strict_audit_reads, "control 用自带的重放副本")

    def test_control_documents_replay_v2_snapshots_and_do_not_accept_v3_entries(self) -> None:
        v2 = control_audit.host_snapshot(str(self.host))
        document = control_audit.filter_stream(
            [*self.prelude(), f'1 openat(AT_FDCWD</repo>, "{self.host}", O_RDONLY) = 3<{self.host}>'],
            ["/"], strict=True, runtime_context=self.control_context())
        self.completed(document, control_isolation_unchanged=True, host_entries_sha256=runtime.digest([v2]))
        contract = self.control_context()["contract_entry"]

        def replay(entries: list, **options) -> dict:
            return control_audit.strict_audit_reads(document, [contract, *entries], repo_root="/repo", data_root=None, **options)

        report = replay([v2])
        self.assertTrue(report["coverage_complete"], report["undeclared"])
        self.assertTrue(replay([v2], historical=True)["coverage_complete"])
        self.assertFalse(replay([ra.host_snapshot_v3(str(self.host))])["coverage_complete"], "control 不把 v3 条目当绑定")
        os.utime(self.host, ns=(1_000_000_000, 2_000_000_000))
        self.assertFalse(replay([v2])["coverage_complete"], "control 保持 v2 的整份相等：时间噪声照旧让它失效")
        self.assertTrue(replay([v2], historical=True)["coverage_complete"], "历史重放只验封存内容")

    def test_executor_picks_v2_only_while_the_control_adapter_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = base_audit.ReadAuditExecutorTests("test_audit_is_only_for_re_execute")._repo(root)
            test_file = repo / "tools/official_client_capture/tests/test_alpha.py"
            contract = {"schema_version": runtime.SCHEMA, "temporary_root": str(root / "private/output"), "mask_roots": [],
                        "environment_paths": []}
            declaration = {"host_paths": [str(self.host)], "require_read_audit": True, "runtime_contract": contract}
            manifest = {"audit_policy": {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only",
                                         "units": {"test_alpha": declaration}}, "units": []}
            group = ue.DiscoverGroup("capture-tools", test_file.parent, "test_*.py", (), ())
            unit = ue.Unit("test_alpha", "test_alpha", ("test_alpha.AlphaTests.test_a",), ue.Quota(1, 128), False, 1)

            def host_schema(records) -> list[str]:
                with mock.patch.object(records.RepoIndex, "load", return_value=records.RepoIndex.load(repo)):
                    currents = ue._gate_currents(records, groups=[group], group_units={group.group_id: [unit]}, command_units=[],
                                                 manifest=manifest, timeout=120)
                return [entry["detail"]["schema_version"] for entry in currents["test_alpha"].inputs if entry["name"].startswith("host:")]

            self.assertEqual(host_schema(ue._records_module()), [V3], "普通运行取 v3")
            token = ue._CONTROL_READ_AUDIT.set(control_audit)
            try:
                self.assertEqual(host_schema(ue._records_module()), [V2], "control 适配器在位的整次运行取 v2")
            finally:
                ue._CONTROL_READ_AUDIT.reset(token)
            self.assertEqual(host_schema(ue._records_module()), [V3], "适配器只存活于那一次调用")

    def test_full_gates_plans_cannot_contain_control_units_so_the_public_background_gets_v3(self) -> None:
        """v3 在公共后台（full-gates）里一定生效：full-gates 计划不含 pre-A3 单元，策略也不能登记计划外的单元。"""
        self.assertIn("pre-a3", eg.PROFILES["entry"])
        self.assertNotIn("pre-a3", eg.PROFILES["full-gates"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = _tree(root, ["check-egress-spec-local-source", "test-official-client-control", "egress-spec-go-test",
                                "egress-spec-version-leak"])
            manifest = eg.plan_gates(tree, profile="full-gates", launcher=LAUNCHER, platform="linux")
            self.assertFalse([unit["unit_id"] for unit in manifest["units"] if unit["unit_id"].startswith("pre-a3:")])
            with self.assertRaises(ValueError):       # pre-A3 场景清单只许 entry、pre-a3 两种组合给
                eg.plan_gates(tree, profile="full-gates", launcher=LAUNCHER, platform="linux", pre_a3_units=root / "units.json")
            with self.assertRaises(ValueError):       # entry 组合必须给
                eg.plan_gates(tree, profile="entry", launcher=LAUNCHER, platform="linux")
            control_id = "pre-a3:claude:vc-chain.claude-reconcile"
            contract = {"schema_version": runtime.SCHEMA, "temporary_root": str(root / "private/output"), "mask_roots": [],
                        "environment_paths": []}
            declaration = {"host_paths": [], "require_read_audit": True, "runtime_contract": contract}
            policy = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {control_id: declaration}}
            with self.assertRaises(RuntimeError):     # 策略只能登记计划里真实存在的单元
                eg.plan_gates(tree, profile="full-gates", launcher=LAUNCHER, platform="linux", audit_policy=policy)
            # 没登记 control 单元的策略不选适配器；适配器只由“策略登记了 control 单元”触发。
            ordinary = {"audit_policy": {"units": {"test_alpha": declaration}}}
            self.assertIsNone(eg.control_read_audit_for_plan(ordinary))
            self.assertIsNone(eg.control_read_audit_for_plan({}))


if __name__ == "__main__":
    unittest.main()
