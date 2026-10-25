"""``claude_tool_snapshot_memo`` 的专项回归（只测测试夹具本身，不动受管工具）。

被测对象是 VC-6 链测试专用的“受管工具清单快照复用”：复用的前提是**文件状态完全没变**，任何一种变化都必须回退到
真实计算。这里逐种构造变化并核对：命中、内容改动（含“改内容后把 mtime 复原”）、新增、删除、改名、替换成符号链接、
排除目录内的变动（不应使缓存失效）、racy 状态不缓存、每个进程首次真实计算、核对异常回退、返回值不污染缓存，
以及与真实实现在真实受管树上逐字相同。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as identity
from tools.official_client_capture.tests import claude_tool_snapshot_memo as memo

# 文件系统时间戳在部分平台上粒度较粗：每次改动前先睡一小会儿，保证 mtime／ctime 一定与上一次不同。
# 睡眠只用于制造“时间一定前进”的前提，不对耗时做任何断言。
TICK = 0.05

# 记下“真实”实现（本模块被导入时尚未被任何用例替换），用来核对复用返回值与真实计算逐字相同。
memo_real = identity._managed_tree_snapshot

POLICY = {"roots": ["tools/alpha", "tools/beta"], "suffixes": [".py", ".json"]}


class SnapshotMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="tool-snapshot-memo-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("alpha", "beta"):
            (self.root / "tools" / name / "pkg").mkdir(parents=True)
            (self.root / "tools" / name / "tests").mkdir()
            (self.root / "tools" / name / "mod.py").write_text(f"VALUE = '{name}'\n", encoding="utf-8")
            (self.root / "tools" / name / "pkg" / "inner.py").write_text("X = 1\n", encoding="utf-8")
            (self.root / "tools" / name / "tests" / "test_x.py").write_text("Y = 2\n", encoding="utf-8")
        time.sleep(TICK)
        # 新建的文件离现在不到 RACY_NS，默认会被当成 racy；这里关闭 racy 门槛来测复用本身，racy 另有专项用例。
        patcher = mock.patch.object(memo, "RACY_NS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real_calls = 0
        real = identity._managed_tree_snapshot

        def counting(*arguments, **options):
            self.real_calls += 1
            return real(*arguments, **options)

        counting_patch = mock.patch.object(identity, "_managed_tree_snapshot", counting)
        counting_patch.start()
        self.addCleanup(counting_patch.stop)
        self.stats = Path(directory.name) / "stats"
        stack = memo.tool_snapshot_memo(self.stats)
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)

    def snapshot(self):
        return identity._managed_tree_snapshot(self.root, POLICY)

    def test_unchanged_tree_is_reused_and_equals_real_result(self) -> None:
        first = self.snapshot()
        second = self.snapshot()
        third = self.snapshot()
        self.assertEqual(self.real_calls, 1, "未变化的树只应真实计算一次")
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(first, memo_real(self.root, POLICY))
        self.assertEqual(memo.read_stats(self.stats), {"hit": 2, "run": 1})
        # 排除目录（tests）不进快照。
        self.assertFalse(any("/tests/" in key for key in first[0]))
        self.assertEqual(sorted(first[0]), ["alpha/mod.py", "alpha/pkg/inner.py", "beta/mod.py", "beta/pkg/inner.py"])

    def test_content_change_with_same_size_and_restored_mtime_is_detected(self) -> None:
        target = self.root / "tools/alpha/mod.py"
        first = self.snapshot()
        before = target.stat()
        time.sleep(TICK)
        target.write_text("VALUE = 'ALPHA'\n", encoding="utf-8")  # 与原内容等长
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))  # 把 mtime 复原；ctime 无法复原
        self.assertEqual(target.stat().st_size, before.st_size)
        second = self.snapshot()
        self.assertEqual(self.real_calls, 2)
        self.assertNotEqual(first[0]["alpha/mod.py"], second[0]["alpha/mod.py"])
        self.assertEqual(second, memo_real(self.root, POLICY))

    def test_size_change_added_removed_and_renamed_files_are_detected(self) -> None:
        self.snapshot()
        time.sleep(TICK)
        (self.root / "tools/alpha/mod.py").write_text("VALUE = 'longer content now'\n", encoding="utf-8")
        self.assertEqual(self.snapshot(), memo_real(self.root, POLICY))
        self.assertEqual(self.real_calls, 2)
        time.sleep(TICK)
        (self.root / "tools/alpha/pkg/new.py").write_text("N = 1\n", encoding="utf-8")  # 子目录里新增
        added = self.snapshot()
        self.assertIn("alpha/pkg/new.py", added[0])
        self.assertEqual(self.real_calls, 3)
        time.sleep(TICK)
        (self.root / "tools/beta/brand_new_dir").mkdir()  # 新增空目录：改变父目录状态，应触发重算
        (self.root / "tools/beta/brand_new_dir/m.py").write_text("M = 1\n", encoding="utf-8")
        self.assertIn("beta/brand_new_dir/m.py", self.snapshot()[0])
        self.assertEqual(self.real_calls, 4)
        time.sleep(TICK)
        (self.root / "tools/alpha/pkg/new.py").rename(self.root / "tools/alpha/pkg/renamed.py")
        renamed = self.snapshot()
        self.assertNotIn("alpha/pkg/new.py", renamed[0])
        self.assertIn("alpha/pkg/renamed.py", renamed[0])
        self.assertEqual(self.real_calls, 5)
        time.sleep(TICK)
        (self.root / "tools/beta/mod.py").unlink()
        removed = self.snapshot()
        self.assertNotIn("beta/mod.py", removed[0])
        self.assertEqual(self.real_calls, 6)
        self.assertEqual(removed, memo_real(self.root, POLICY))

    def test_symlink_replacement_is_detected_and_symlinked_tree_is_never_cached(self) -> None:
        self.snapshot()
        time.sleep(TICK)
        target = self.root / "tools/alpha/mod.py"
        original = target.read_bytes()
        target.unlink()
        other = self.root / "elsewhere.py"
        other.write_bytes(original)
        target.symlink_to(other)
        result = self.snapshot()
        self.assertNotIn("alpha/mod.py", result[0], "真实实现对符号链接文件的处理是跳过")
        self.assertEqual(self.real_calls, 2)
        # 树里有符号链接：不缓存，之后每次都真实计算。
        self.snapshot()
        self.snapshot()
        self.assertEqual(self.real_calls, 4)
        self.assertEqual(memo.read_stats(self.stats)["hit"], 0)

    def test_changes_inside_excluded_directories_do_not_invalidate(self) -> None:
        self.snapshot()
        time.sleep(TICK)
        (self.root / "tools/alpha/tests/test_x.py").write_text("Y = 3333\n", encoding="utf-8")
        (self.root / "tools/alpha/tests/new_test.py").write_text("Z = 1\n", encoding="utf-8")
        (self.root / "tools/alpha/tests/sub").mkdir()
        self.snapshot()
        self.assertEqual(self.real_calls, 1, "tests 目录整棵子树都不在快照里，里面的变动不应使缓存失效")

    def test_racy_fresh_files_are_not_cached(self) -> None:
        with mock.patch.object(memo, "RACY_NS", 3 * 10**9):
            self.snapshot()
            self.snapshot()
            self.assertEqual(self.real_calls, 2, "刚创建不足门槛的文件状态不可信，不缓存")

    def test_first_call_in_each_process_is_real(self) -> None:
        self.snapshot()
        self.snapshot()
        self.assertEqual(self.real_calls, 1)
        with mock.patch.object(os, "getpid", return_value=os.getpid() + 1):  # 模拟 fork 出的守卫进程
            self.snapshot()
            self.assertEqual(self.real_calls, 2, "新进程对同一输入的第一次调用必须真实计算")
            self.snapshot()
            self.assertEqual(self.real_calls, 2)

    def test_verification_failure_falls_back_to_real_computation(self) -> None:
        first = self.snapshot()
        with mock.patch.object(memo, "_verify", side_effect=RuntimeError("核对出错")):
            again = self.snapshot()
        self.assertEqual(self.real_calls, 2)
        self.assertEqual(first, again)
        with mock.patch.object(memo, "_verify", return_value=False):
            self.snapshot()
        self.assertEqual(self.real_calls, 3)

    def test_returned_dictionaries_do_not_alias_the_cache(self) -> None:
        digests, sources = self.snapshot()
        digests["alpha/mod.py"] = "tampered"
        sources.clear()
        again_digests, again_sources = self.snapshot()
        self.assertEqual(self.real_calls, 1)
        self.assertNotEqual(again_digests["alpha/mod.py"], "tampered")
        self.assertEqual(len(again_sources), 4)

    def test_different_policy_or_root_uses_separate_entries(self) -> None:
        self.snapshot()
        other = {"roots": ["tools/alpha"], "suffixes": [".py"]}
        self.assertEqual(sorted(identity._managed_tree_snapshot(self.root, other)[0]), ["alpha/mod.py", "alpha/pkg/inner.py"])
        self.assertEqual(self.real_calls, 2)
        self.assertEqual(sorted(identity._managed_tree_snapshot(self.root, other)[0]), ["alpha/mod.py", "alpha/pkg/inner.py"])
        self.assertEqual(self.real_calls, 2)

    def test_memo_is_removed_on_exit(self) -> None:
        """退出作用域后：函数还原成进入前的那一个，本进程缓存清空，再调用就是真实计算。"""

        before = identity._managed_tree_snapshot  # setUp 里已经装了计数包装和一层 memo
        self.snapshot()
        with memo.tool_snapshot_memo():
            inner = identity._managed_tree_snapshot
            self.assertIsNot(inner, before, "作用域内是新的复用包装")
            identity._managed_tree_snapshot(self.root, POLICY)
            self.assertTrue(memo._cache, "作用域内已缓存")
        self.assertIs(identity._managed_tree_snapshot, before, "退出后还原成进入前的函数")
        self.assertFalse(memo._cache, "退出时清空本进程缓存")
        calls = self.real_calls
        # 进入前那层 memo 的缓存也被一并清空（缓存是进程级的）：再调用必须真实计算一次。
        self.snapshot()
        self.assertEqual(self.real_calls, calls + 1)


class RealManagedTreeTests(unittest.TestCase):
    """在真实受管树上，复用结果与真实实现逐字相同（只读，不改仓库）。"""

    def test_reused_snapshot_equals_real_snapshot_on_repository_tree(self) -> None:
        policy = identity.load_policy()
        root = identity.REPOSITORY_ROOT
        expected = memo_real(root, policy)
        with mock.patch.object(memo, "RACY_NS", 0), memo.tool_snapshot_memo():
            first = identity._managed_tree_snapshot(root, policy)
            second = identity._managed_tree_snapshot(root, policy)
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)


if __name__ == "__main__":
    unittest.main()
