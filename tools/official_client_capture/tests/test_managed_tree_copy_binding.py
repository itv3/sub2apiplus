"""副本受管树基座：execution_tree_binding_required 的主机形态判定，与副本层字节码缓存（E2-02）。

CI runner 以非 root 用户运行，/root 存在但不可进入；此前 is_dir() 抛 PermissionError，
让依赖副本树的评估测试在 CI 上全部报错（本地 macOS 与 ARM64 都复现不了）。

副本层字节码缓存的用例用一个只有两个模块的假仓库和只编译了 json 包的共享层，验证：按内容复用共享层的 .pyc、
标准库经符号链接共用、副本树内不出现 __pycache__、共享层只读、同一秒内长度不变的源码修改后执行新代码、两棵副本树
并行准备和运行互不清对方的缓存、没有共享层时行为与原来相同。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import managed_tree_copy


class ExecutionTreeBindingRequiredTest(unittest.TestCase):
    def test_permission_denied_means_no_production_copy(self) -> None:
        with mock.patch.object(Path, "is_dir", side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(managed_tree_copy.execution_tree_binding_required())
            self.assertFalse(managed_tree_copy.execution_tree_binding_available())

    def test_missing_production_copy(self) -> None:
        with mock.patch.object(Path, "is_dir", return_value=False):
            self.assertFalse(managed_tree_copy.execution_tree_binding_required())

    def test_present_production_copy_still_requires_binding(self) -> None:
        with mock.patch.object(Path, "is_dir", return_value=True):
            self.assertTrue(managed_tree_copy.execution_tree_binding_required())

    def test_other_os_errors_are_not_swallowed(self) -> None:
        with mock.patch.object(Path, "is_dir", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                managed_tree_copy.execution_tree_binding_required()


# 子进程探针：导入副本里的模块与 json，报告取值、缓存路径与 -v 输出里这两个模块是从 .pyc 还是源码加载的。
PROBE = (
    "import json, os\n"
    "from tools.official_client_capture import probe\n"
    "print(json.dumps({'value': probe.VALUE, 'cached': probe.__cached__, 'cached_exists': os.path.isfile(probe.__cached__),\n"
    "                  'json_cached': json.__cached__, 'json_cached_exists': os.path.isfile(json.__cached__)}))\n"
)


class CopyPycacheTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        repo = self.root / "repo"
        package = repo / managed_tree_copy.PACKAGE_RELATIVE
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "probe.py").write_text('VALUE = "AAAA"\n', encoding="utf-8")
        for name, value in (("REPO_ROOT", repo), ("TOOL_ROOT", package)):
            patcher = mock.patch.object(managed_tree_copy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # 共享层：原树按内容摘要编译（与驱动 bytecode_cache.py 相同的模式），标准库只编译 json 包。
        self.shared = self.root / "pycache-shared"
        compile_script = (
            "import compileall, json, py_compile, sys\nfrom pathlib import Path\n"
            "ok = compileall.compile_dir(sys.argv[1], quiet=1, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)\n"
            "ok = compileall.compile_dir(str(Path(json.__file__).parent), quiet=1) and ok\n"
            "sys.exit(0 if ok else 1)\n"
        )
        compiled = subprocess.run([sys.executable, "-c", compile_script, str(repo / "tools")], capture_output=True, text=True,
                                  env={**os.environ, "PYTHONPYCACHEPREFIX": str(self.shared)}, timeout=120)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def _files(self, path: Path) -> list[str]:
        return sorted(str(item) for item in path.rglob("*") if item.is_file())

    def _probe(self, tree: Path) -> tuple[dict, str]:
        environment = {**managed_tree_copy.subprocess_env(tree), "PYTHONDONTWRITEBYTECODE": "1"}
        completed = subprocess.run([sys.executable, "-v", "-c", PROBE], cwd=tree, env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        loaded = [line for line in completed.stderr.splitlines() if "code object from" in line and "probe" in line]
        return json.loads(completed.stdout), "\n".join(loaded)

    def test_copy_reuses_shared_bytecode_by_content_and_falls_back_after_same_second_edit(self) -> None:
        shared_before = self._files(self.shared)
        with mock.patch.object(sys, "pycache_prefix", str(self.shared)):
            tree = managed_tree_copy.copy_managed_tree(self.root / "copies" / "copy-a", include_tests=False)
        prefix = managed_tree_copy.copy_pycache_prefix(tree)
        result, loaded = self._probe(tree)
        self.assertEqual(result["value"], "AAAA")
        self.assertTrue(result["cached"].startswith(str(prefix)) and result["cached_exists"], result)
        self.assertTrue(result["json_cached"].startswith(str(prefix)) and result["json_cached_exists"], "标准库经符号链接命中共享层")
        self.assertIn(".pyc", loaded, "未改动的副本模块从副本层 .pyc 加载")
        original = managed_tree_copy._cached_path(self.shared, managed_tree_copy.TOOL_ROOT / "probe.py")
        self.assertEqual(Path(result["cached"]).read_bytes(), original.read_bytes())
        # 同一秒内、长度不变地改副本源码：内容摘要对不上，回落为从源码编译，执行的是新代码。
        source = managed_tree_copy.tool_root(tree) / "probe.py"
        source.write_text(source.read_text(encoding="utf-8").replace("AAAA", "BBBB"), encoding="utf-8")
        result, loaded = self._probe(tree)
        self.assertEqual(result["value"], "BBBB")
        self.assertNotIn(".pyc", loaded, "改过的副本模块必须从源码编译")
        self.assertEqual(sorted(str(path) for path in tree.rglob("__pycache__")), [], "副本树内不得出现 __pycache__")
        self.assertEqual(self._files(self.shared), shared_before, "共享层只读：子进程不增删任何文件")

    def test_parallel_copies_prepare_and_run_without_clearing_each_other(self) -> None:
        trees = [self.root / "copies" / f"copy-{name}" for name in ("a", "b", "c")]
        errors: list[BaseException] = []

        def build(tree: Path) -> None:
            try:
                managed_tree_copy.copy_managed_tree(tree, include_tests=False)
                managed_tree_copy.prepare_copy_pycache(tree, self.shared)
            except BaseException as error:  # noqa: BLE001 - 线程里的失败带回主线程断言
                errors.append(error)

        threads = [threading.Thread(target=build, args=(tree,)) for tree in trees]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(errors, [])
        prefix = managed_tree_copy.copy_pycache_prefix(trees[0])
        self.assertTrue(all(managed_tree_copy.copy_pycache_prefix(tree) == prefix for tree in trees), "三棵副本树共用一个前缀根")
        # 再准备一次 a（幂等，只增不删），b、c 的缓存仍在；三棵并行运行都命中自己的副本层。
        managed_tree_copy.prepare_copy_pycache(trees[0], self.shared)
        outputs: dict[str, dict] = {}

        def run(tree: Path) -> None:
            outputs[tree.name] = self._probe(tree)[0]

        threads = [threading.Thread(target=run, args=(tree,)) for tree in trees]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        for tree in trees:
            self.assertTrue(outputs[tree.name]["cached_exists"], tree.name)
            self.assertTrue(outputs[tree.name]["cached"].startswith(str(prefix / tree.relative_to(tree.anchor))), outputs[tree.name])

    def test_children_of_the_original_repository_use_the_shared_layer_itself(self) -> None:
        """直接对原仓库根起子进程（真实链夹具有这种用法）：原树本来就在共享层里，前缀用共享层本身。"""

        with mock.patch.object(sys, "pycache_prefix", str(self.shared)):
            self.assertEqual(managed_tree_copy.subprocess_env(managed_tree_copy.REPO_ROOT)["PYTHONPYCACHEPREFIX"], str(self.shared))
        with mock.patch.object(sys, "pycache_prefix", None):
            fallback = managed_tree_copy.subprocess_env(managed_tree_copy.REPO_ROOT)["PYTHONPYCACHEPREFIX"]
        self.assertEqual(fallback, str(managed_tree_copy.copy_pycache_prefix(managed_tree_copy.REPO_ROOT)))

    def test_without_shared_layer_copy_prefix_stays_empty_and_children_still_run(self) -> None:
        with mock.patch.object(sys, "pycache_prefix", None):
            tree = managed_tree_copy.copy_managed_tree(self.root / "copies" / "copy-plain", include_tests=False)
        prefix = managed_tree_copy.copy_pycache_prefix(tree)
        self.assertEqual(self._files(prefix), [])
        result, _loaded = self._probe(tree)
        self.assertEqual(result["value"], "AAAA")
        self.assertFalse(result["cached_exists"])
        self.assertEqual(sorted(str(path) for path in tree.rglob("__pycache__")), [])

    def test_only_checked_hash_bytecode_is_reused(self) -> None:
        """共享层若是按时间戳编译的（旧版预编译工具），副本层不复用：时间戳校验挡不住同一秒内长度不变的修改。"""

        stale = self.root / "pycache-timestamp"
        script = ("import compileall, py_compile, sys\n"
                  "sys.exit(0 if compileall.compile_dir(sys.argv[1], quiet=1, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP) else 1)\n")
        compiled = subprocess.run([sys.executable, "-c", script, str(managed_tree_copy.REPO_ROOT / "tools")], capture_output=True, text=True,
                                  env={**os.environ, "PYTHONPYCACHEPREFIX": str(stale)}, timeout=120)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        tree = self.root / "copies" / "copy-ts"
        with mock.patch.object(sys, "pycache_prefix", None):
            managed_tree_copy.copy_managed_tree(tree, include_tests=False)
        self.assertEqual(managed_tree_copy.prepare_copy_pycache(tree, stale)["modules"], 0)


if __name__ == "__main__":
    unittest.main()
