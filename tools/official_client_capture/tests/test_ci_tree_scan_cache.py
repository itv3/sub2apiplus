"""测试侧受管树扫描缓存（OPT-02 第二轮）：``tests/tree_scan_cache.py``。

缓存只允许"省掉同一进程里对同一棵没变的树的重复扫描"，不允许改变任何一次扫描的结果，也不允许掩盖受管树的漂移。所以这里的用例分三类：

* 命中：同一棵没变的树，第二次不再调原函数，且结果与原函数逐项相同；调用方改返回值不污染缓存；
* 失效（变异）：内容变了（含大小不变、mtime 被改回去）、增删改名、权限变了，必须重算；被剪枝目录（tests／versions／__pycache__）里的变化不影响结果、也不让缓存失效；
  文件刚被写过（竞态窗口内）不缓存不命中；原函数抛异常不缓存；
* 改坏即红：用一个故意改坏的缓存（签名恒定／不看 ctime／不做竞态保护）跑同一批失效断言，断言必须失败——证明这批变异用例真能抓住缓存失效逻辑的缺陷；
* 真实受管树：两个被包装函数在真实受管树上的结果与原函数逐项相同；受管树副本里改一个文件，身份函数的结果必变（漂移照样被看见）。
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from tools.official_client_capture import codex_upgrade, codex_upgrade_tool_identity_policy as tip
from tools.official_client_capture.tests import tree_scan_cache as tsc

REAL_TOOL_ROOT = Path(codex_upgrade.__file__).resolve().parent
FAR_FUTURE_OFFSET_NS = 3600 * 10**9   # 把时钟拨到一小时后：刚写的文件对缓存来说已经"老"过竞态窗口


def scan_original(root: Path) -> dict[str, str]:
    """一个独立写的扫描函数（语义同 ``_managed_tree_digests``），当被缓存的"原函数"用，调用次数可数。"""

    scan_original.calls += 1   # type: ignore[attr-defined]
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix not in {".py", ".sh", ".json"}:
            continue
        parts = path.relative_to(root).parts
        if "tests" in parts or "versions" in parts or "__pycache__" in parts:
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


scan_original.calls = 0   # type: ignore[attr-defined]


class CacheCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "tool"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "versions").mkdir()
        (self.root / "a.py").write_text("A = 1\n", encoding="utf-8")
        (self.root / "sub" / "b.json").write_text('{"b": 1}\n', encoding="utf-8")
        (self.root / "run.sh").write_text("echo hi\n", encoding="utf-8")
        (self.root / "notes.md").write_text("忽略的后缀\n", encoding="utf-8")
        (self.root / "tests" / "t.py").write_text("T = 1\n", encoding="utf-8")
        (self.root / "versions" / "v.json").write_text("{}\n", encoding="utf-8")
        tsc.clear()
        scan_original.calls = 0   # type: ignore[attr-defined]
        self.wrapped = tsc.cached(scan_original, "test")
        # 时钟拨到一小时后：刚创建的文件对缓存来说不算"刚写过"。
        patcher = mock.patch.object(tsc, "clock_ns", lambda: __import__("time").time_ns() + FAR_FUTURE_OFFSET_NS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bump(self, rel: str, text: str, *, keep_mtime: bool = False) -> None:
        path = self.root / rel
        before = path.stat() if path.exists() else None
        path.write_text(text, encoding="utf-8")
        if keep_mtime and before is not None:
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


class HitTests(CacheCase):
    def test_an_unchanged_tree_is_scanned_once_and_the_result_equals_the_original(self) -> None:
        first = self.wrapped(self.root)
        second = self.wrapped(self.root)
        self.assertEqual(scan_original.calls, 1, "第二次命中缓存，不再调原函数")   # type: ignore[attr-defined]
        self.assertEqual(first, second)
        self.assertEqual(first, scan_original(self.root), "命中的结果与原函数现算的逐项相同")
        self.assertEqual(sorted(first), ["a.py", "run.sh", "sub/b.json"])
        self.assertEqual(tsc.stats["hits"], 1)

    def test_changing_the_returned_value_does_not_poison_the_cache(self) -> None:
        first = self.wrapped(self.root)
        first["a.py"] = "tampered"
        first["bogus"] = "x"
        self.assertEqual(self.wrapped(self.root), scan_original(self.root))

    def test_the_cache_is_keyed_by_the_real_path_so_two_trees_never_share_an_entry(self) -> None:
        other = self.root.parent / "other"
        shutil.copytree(self.root, other)
        (other / "a.py").write_text("A = 2\n", encoding="utf-8")
        self.assertNotEqual(self.wrapped(self.root), self.wrapped(other))
        link = self.root.parent / "link"
        link.symlink_to(self.root)
        calls = scan_original.calls   # type: ignore[attr-defined]
        self.assertEqual(self.wrapped(link), self.wrapped(self.root))
        self.assertEqual(scan_original.calls, calls, "符号链接指向同一棵树：同一个键")   # type: ignore[attr-defined]


class InvalidationTests(CacheCase):
    """变异：这些改动之后，缓存必须重算，且结果必须反映改动。"""

    def assert_recomputed(self, mutate: Callable[[], None], expect_change: bool = True) -> None:
        before = self.wrapped(self.root)
        calls = scan_original.calls   # type: ignore[attr-defined]
        mutate()
        after = self.wrapped(self.root)
        self.assertEqual(after, scan_original(self.root), "缓存给的结果必须等于原函数在改动后的树上现算的")
        if expect_change:
            self.assertNotEqual(before, after)
            self.assertGreater(scan_original.calls, calls + 1, "重算了（原函数被调用；这里再加上上面断言里的一次）")   # type: ignore[attr-defined]

    def test_content_change_with_the_same_size(self) -> None:
        self.assert_recomputed(lambda: self.bump("a.py", "A = 2\n"))

    def test_content_change_with_the_same_size_and_the_old_mtime_restored(self) -> None:
        """写完再把 mtime 改回去：ctime 改不回去，签名照样不同。"""

        self.assert_recomputed(lambda: self.bump("a.py", "A = 3\n", keep_mtime=True))

    def test_added_removed_and_renamed_files(self) -> None:
        self.assert_recomputed(lambda: (self.root / "new.py").write_text("N = 1\n", encoding="utf-8"))
        self.assert_recomputed(lambda: (self.root / "new.py").unlink())
        self.assert_recomputed(lambda: (self.root / "run.sh").rename(self.root / "run2.sh"))
        self.assert_recomputed(lambda: (self.root / "sub").rename(self.root / "sub2"))

    def test_a_file_replaced_by_a_symlink_and_a_permission_change(self) -> None:
        def swap() -> None:
            (self.root / "a.py").unlink()
            (self.root / "a.py").symlink_to(self.root / "run.sh")
        self.assert_recomputed(swap)
        self.assert_recomputed(lambda: os.chmod(self.root / "run.sh", 0o700), expect_change=False)
        self.assertEqual(scan_original.calls >= 3, True)   # type: ignore[attr-defined]

    def test_changes_under_ignored_directories_do_not_change_the_result_or_force_a_rescan(self) -> None:
        before = self.wrapped(self.root)
        calls = scan_original.calls   # type: ignore[attr-defined]
        self.bump("tests/t.py", "T = 2\n")
        (self.root / "versions" / "new.json").write_text("{}\n", encoding="utf-8")
        (self.root / "tests" / "deeper").mkdir()
        (self.root / "tests" / "deeper" / "x.py").write_text("X\n", encoding="utf-8")
        self.assertEqual(self.wrapped(self.root), before)
        self.assertEqual(scan_original.calls, calls, "原函数本来就忽略这些目录：缓存也不为它们失效")   # type: ignore[attr-defined]
        self.assertEqual(before, scan_original(self.root))

    def test_a_file_written_inside_the_racy_window_is_never_cached_or_served(self) -> None:
        """真实时钟：文件刚写过，同一个时钟刻度里再写一次 mtime 不变——此时不缓存也不命中，每次都现算。"""

        with mock.patch.object(tsc, "clock_ns", __import__("time").time_ns):
            calls = scan_original.calls   # type: ignore[attr-defined]
            self.bump("a.py", "A = 4\n")
            first = self.wrapped(self.root)
            self.bump("a.py", "A = 5\n")
            second = self.wrapped(self.root)
            self.assertNotEqual(first, second, "竞态窗口内每次都现算，不会把旧结果当新结果")
            self.assertEqual(scan_original.calls, calls + 2)   # type: ignore[attr-defined]
            self.assertGreaterEqual(tsc.stats["racy"], 2)

    def test_an_exception_from_the_original_is_not_cached(self) -> None:
        state = {"fail": True}

        def flaky(root: Path) -> dict[str, str]:
            if state["fail"]:
                raise OSError("boom")
            return {"ok": "1"}

        wrapped = tsc.cached(flaky, "flaky")
        with self.assertRaises(OSError):
            wrapped(self.root)
        state["fail"] = False
        self.assertEqual(wrapped(self.root), {"ok": "1"})

    def test_the_environment_switch_turns_the_cache_off(self) -> None:
        with mock.patch.dict(os.environ, {tsc.DISABLE_ENV: "off"}):
            self.wrapped(self.root)
            self.wrapped(self.root)
        self.assertEqual(scan_original.calls, 2)   # type: ignore[attr-defined]


def broken_cache(original: Callable[[Any], Any], *, mode: str) -> Callable[[Any], Any]:
    """故意改坏的缓存，用来证明失效用例抓得住：``constant`` 签名恒定；``no_ctime`` 签名不含 ctime；``no_racy`` 去掉竞态保护。"""

    store: dict[str, tuple[Any, Any]] = {}

    def signature(root: str) -> Any:
        if mode == "constant":
            return ()
        rows = []
        for path in sorted(Path(root).rglob("*")):
            info = path.lstat()
            rows.append((str(path.relative_to(root)), info.st_size, info.st_mtime_ns) + (() if mode == "no_ctime" else (info.st_ctime_ns,)))
        return tuple(rows)

    def wrapper(tool_root: Any) -> Any:
        key = os.path.realpath(os.fspath(tool_root))
        sig = signature(key)
        hit = store.get(key)
        if hit is not None and hit[0] == sig:
            return copy.deepcopy(hit[1])
        result = original(tool_root)
        store[key] = (sig, copy.deepcopy(result))
        return result

    return wrapper


class BrokenCacheIsCaughtTests(CacheCase):
    """改坏即红：同一批失效断言放到被故意改坏的缓存上，必须失败。"""

    def run_mutations(self, wrapped: Callable[[Any], Any]) -> None:
        wrapped(self.root)
        self.bump("a.py", "A = 9\n")
        self.assertEqual(wrapped(self.root), scan_original(self.root), "内容变了，缓存必须给出新结果")
        wrapped(self.root)
        self.bump("a.py", "A = 8\n", keep_mtime=True)
        self.assertEqual(wrapped(self.root), scan_original(self.root), "大小不变、mtime 被改回去，缓存必须给出新结果")

    def test_the_real_cache_passes_the_mutation_assertions(self) -> None:
        self.run_mutations(self.wrapped)

    def test_a_cache_with_a_constant_signature_is_caught(self) -> None:
        with self.assertRaises(AssertionError):
            self.run_mutations(broken_cache(scan_original, mode="constant"))

    def test_a_cache_that_ignores_ctime_is_caught_by_the_restored_mtime_mutation(self) -> None:
        with self.assertRaises(AssertionError):
            self.run_mutations(broken_cache(scan_original, mode="no_ctime"))


class InstallAndRealTreeTests(unittest.TestCase):
    def test_the_two_managed_functions_are_wrapped_and_keep_a_handle_to_the_original(self) -> None:
        for module, name in ((codex_upgrade, "_tool_tree_entries"), (tip, "_managed_tree_digests")):
            function = getattr(module, name)
            self.assertTrue(hasattr(function, "__wrapped__"), f"{module.__name__}.{name} 应已被测试包补丁包装（TREE_SCAN_CACHE=off 时跳过）" if os.environ.get(tsc.DISABLE_ENV) != "off" else name)

    def test_on_the_real_managed_tree_the_wrapped_functions_equal_the_originals_item_by_item(self) -> None:
        if not hasattr(codex_upgrade._tool_tree_entries, "__wrapped__"):
            self.skipTest("缓存被环境变量关闭")
        entries_original = codex_upgrade._tool_tree_entries.__wrapped__
        digests_original = tip._managed_tree_digests.__wrapped__
        for _ in range(2):   # 第二遍可能命中缓存：结果仍必须逐项相同
            self.assertEqual(codex_upgrade._tool_tree_entries(REAL_TOOL_ROOT), entries_original(REAL_TOOL_ROOT))
            self.assertEqual(tip._managed_tree_digests(REAL_TOOL_ROOT), digests_original(REAL_TOOL_ROOT))
        self.assertEqual({item["path"]: item["sha256"] for item in codex_upgrade._tool_tree_entries(REAL_TOOL_ROOT)},
                         tip._managed_tree_digests(REAL_TOOL_ROOT), "两个函数同一口径")

    def test_drift_in_a_copy_of_the_managed_tree_is_still_seen_through_the_cache(self) -> None:
        """受管树副本里改一个文件：两个函数的结果必须随之改变，被改文件的摘要必须是新内容的摘要（漂移照样被看见）。"""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        copy_root = Path(directory.name).resolve() / "tool"
        shutil.copytree(REAL_TOOL_ROOT, copy_root, ignore=shutil.ignore_patterns("tests", "__pycache__", "versions"))
        with mock.patch.object(tsc, "clock_ns", lambda: __import__("time").time_ns() + FAR_FUTURE_OFFSET_NS):
            tsc.clear()
            before_entries = codex_upgrade._tool_tree_entries(copy_root)
            before_digests = tip._managed_tree_digests(copy_root)
            self.assertEqual(codex_upgrade._tool_tree_entries(copy_root), before_entries)   # 第二次命中
            target = copy_root / "candidate_rule_assertion.py"
            target.write_text(target.read_text(encoding="utf-8") + "\n# 漂移\n", encoding="utf-8")
            after_entries = codex_upgrade._tool_tree_entries(copy_root)
            after_digests = tip._managed_tree_digests(copy_root)
            self.assertNotEqual(before_entries, after_entries)
            self.assertNotEqual(before_digests, after_digests)
            expected = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(after_digests["candidate_rule_assertion.py"], expected)
            self.assertEqual({item["path"]: item["sha256"] for item in after_entries}["candidate_rule_assertion.py"], expected)
            identity = tip.compute_identity_v2(tip.load_policy(copy_root / tip.POLICY_FILENAME), copy_root, after_entries)
            changed = tip.compute_identity_v2(tip.load_policy(copy_root / tip.POLICY_FILENAME), copy_root, before_entries)
            self.assertNotEqual((identity["evidence_semantics_sha256"], identity["wire_producer_sha256"]),
                                (changed["evidence_semantics_sha256"], changed["wire_producer_sha256"]),
                                "漂移文件在 evidence 层：用改后的条目算出的身份必须不同于用改前条目算出的")


if __name__ == "__main__":
    unittest.main()
