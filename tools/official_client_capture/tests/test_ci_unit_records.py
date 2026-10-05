"""单元执行记录与承接（E3-01，``tools/ci/unit_records.py`` 与执行器 ``run-gates`` 的承接）。

前半部分直接测记录、记录库、输入范围、静态依赖闭包、环境指纹、承接判定与清单自检；后半部分在临时 git 仓库里生成小测试
模块，用真实的执行器进程跑 ``run-gates``，按方案 E3-01 的验收逐条核对：失败修复后只补跑失败与受影响的单元、只改一个
测试文件只有它重跑、改受管文件全部重跑、诊断记录不承接、改独占名单全部重跑、篡改记录或改标诊断或改环境或过期都不承接、
重新执行全集模式塞进承接项自检失败。状态目录、记录库与输出目录都在临时目录里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
from pathlib import Path

from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"
RECORDS = REPO_ROOT / "tools" / "ci" / "unit_records.py"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(root), "-c", "user.name=e3", "-c", "user.email=e3@example.invalid",
                                "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return completed.stdout


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


PASS = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"
FAIL = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1, 2)\n"


def _repo(root: Path, extra: dict[str, str] | None = None) -> Path:
    """临时仓库：受管工具树 pkg/（含 tests/），docs/ 与仓库其余部分各一个文件；首次提交。"""

    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    files = {
        "pkg/mod.py": "VALUE = 1\n",
        "pkg/tests/__init__.py": "",
        "pkg/tests/helper_fixture.py": "HELPER = 1\n",
        "pkg/tests/fixtures/data.json": "{}\n",
        "pkg/tests/test_leaf.py": PASS,
        "pkg/tests/test_uses_helper.py": "import unittest\nimport helper_fixture\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
        "pkg/tests/test_other.py": PASS,
        "docs/guide.md": "指南\n",
        "Makefile": "all:\n",
        ".gitignore": "out*/\nstore/\nstate/\n*.json.tmp\n",
    }
    files.update(extra or {})
    _write(root, files)
    _commit(root, "初始")
    return root


class RecordAndStoreTests(unittest.TestCase):
    def test_seal_and_derived_pass_use_raw_fields(self) -> None:
        body = {"unit_id": "test_a", "unit_type": "test", "kind": "formal", "test_ids": ["test_a.T.test_ok"],
                "tests": {"test_a.T.test_ok": {"outcome": "passed"}}, "passed": True, "exit_code": 0, "signal": None, "timed_out": False}
        record = ur.seal_record(body)
        self.assertTrue(ur.seal_ok(record, "record_sha256"))
        self.assertEqual(ur.derived_pass(record), (True, ""))
        forged = dict(record, passed=True, tests={"test_a.T.test_ok": {"outcome": "failed"}})
        self.assertFalse(ur.derived_pass(forged)[0], "passed 字段不能掩盖失败的测试结论")
        self.assertFalse(ur.derived_pass(dict(record, tests={}))[0], "测试结果必须与测试 ID 集合一一对应")
        self.assertFalse(ur.derived_pass(dict(record, timed_out=None))[0], "超时字段缺失按不通过")
        self.assertFalse(ur.seal_ok(dict(record, kind="diagnostic"), "record_sha256"))

    def test_store_is_content_addressed_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ur.RecordStore(Path(directory))
            record = ur.seal_record({"unit_id": "test_a#1", "kind": "formal"})
            path = store.put_record(record)
            self.assertEqual(path.name, f"{record['record_sha256']}.json")
            self.assertEqual(store.put_record(record), path, "同内容再存是同一个文件")
            with self.assertRaises(ur.RecordsError):
                store.put_record(dict(record, kind="diagnostic"))
            self.assertEqual([item[0] for item in store.candidates("test_a#1")], [path])
            self.assertEqual(store.candidates("test_a-1"), [], "安全名相同的不同单元 ID 不会串")
            manifest = ur.build_manifest(run_id="r1", units=[])
            store.put_manifest(manifest)
            with self.assertRaises(FileExistsError):
                store.put_manifest(manifest)
            self.assertIsNone(store.manifest("../r1"))


class RepoIndexAndDependencyTests(unittest.TestCase):
    def test_ranges_dirty_tree_and_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _repo(Path(directory).resolve())
            repo = ur.RepoIndex.load(root)
            self.assertTrue(repo.clean)
            ranges = ur.standard_ranges("pkg")
            managed = repo.range_entry(ranges["managed"])
            tests = repo.range_entry(ranges["tests"])
            rest = repo.range_entry(ranges["rest"])
            self.assertEqual((managed["detail"]["files"], tests["detail"]["files"]), (1, 6))
            self.assertEqual(rest["detail"]["files"], 2, "其余部分＝受管工具树与 docs 之外（Makefile、.gitignore）")
            (root / "pkg" / "tests" / "test_leaf.py").write_text(PASS + "# 注释\n", encoding="utf-8")
            dirty = ur.RepoIndex.load(root)
            self.assertFalse(dirty.clean)
            self.assertEqual(dirty.range_entry(ranges["managed"])["sha256"], managed["sha256"], "测试目录的改动不影响受管工具树一项")
            self.assertNotEqual(dirty.range_entry(ranges["tests"])["sha256"], tests["sha256"])
            self.assertEqual(repo.head_entry()["detail"]["value"], _git(root, "rev-parse", "HEAD").strip())
            with self.assertRaises(ur.RecordsError):
                repo.range_entry({"category": "tests", "name": "x", "include": ["../etc/"]})

    def test_closure_follows_textual_mentions_and_real_chain_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _repo(Path(directory).resolve(), {
                "pkg/tests/test_subprocess.py": "SCRIPT = 'from tools.x.tests import helper_fixture'\n" + PASS,
                "pkg/tests/test_chain_user.py": "import unittest\n# 用到真实链 test_some_chain 的辅助函数\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
                "pkg/tests/real_chains/test_some_chain.py": "from managed_tree_copy import copy_managed_tree\ncopy_managed_tree('a', 'b')\n",
                "pkg/tests/managed_tree_copy.py": "def copy_managed_tree(src, dst, include_tests=True): pass\n",
                "pkg/tests/test_copies_without_tests.py": "from managed_tree_copy import copy_managed_tree\ncopy_managed_tree('a', 'b', include_tests=False)\n" + PASS,
                "pkg/tests/test_copies_closure.py": ("from managed_tree_copy import copy_managed_tree\n"
                                                     "copy_managed_tree('a', include_tests='closure', closure_of=__file__)\n" + PASS),
                "pkg/tests/test_discovers.py": "import unittest\nunittest.defaultTestLoader.discover('.')\n" + PASS,
                "pkg/tests/tree_helper.py": "from managed_tree_copy import copy_managed_tree\n\ndef full_copy():\n    copy_managed_tree('a', 'b')\n",
                "pkg/tests/test_uses_tree_helper.py": "import unittest\nimport tree_helper\n" + PASS,
            })
            deps = ur.TestDependencies(root / "pkg")
            tests = root / "pkg" / "tests"
            closure = lambda name: sorted(path.name for path in deps.closure(tests / name))  # noqa: E731
            self.assertEqual(closure("test_leaf.py"), ["test_leaf.py"])
            self.assertEqual(closure("test_uses_helper.py"), ["helper_fixture.py", "test_uses_helper.py"])
            self.assertEqual(closure("test_subprocess.py"), ["helper_fixture.py", "test_subprocess.py"], "字符串里的子进程脚本也算")
            self.assertEqual(closure("test_chain_user.py"), ["managed_tree_copy.py", "test_chain_user.py", "test_some_chain.py"])
            # 整目录读取的自动识别只看模块自身与辅助模块：真实链自己按默认参数复制受管树，不算到只引用它的模块头上
            # （10-02 实跑核查：闭包含真实链的评估类模块都不读整个测试目录）；辅助模块里这样复制的，用到它的模块算。
            self.assertFalse(deps.module_flags(tests / "test_chain_user.py").whole_tests)
            self.assertTrue(deps.module_flags(tests / "test_uses_tree_helper.py").whole_tests, "辅助模块按默认参数复制受管树（含测试目录）")
            self.assertTrue(deps.module_flags(tests / "real_chains" / "test_some_chain.py").whole_tests, "模块自身的调用照样识别")
            self.assertFalse(deps.module_flags(tests / "test_copies_without_tests.py").whole_tests)
            self.assertFalse(deps.module_flags(tests / "test_copies_closure.py").whole_tests, "按闭包复制只带已声明的那部分（E3-04）")
            self.assertTrue(deps.module_flags(tests / "test_discovers.py").whole_tests)
            repo = ur.RepoIndex.load(root)
            names = {entry["name"] for entry in ur.test_unit_inputs(repo, deps, tests / "test_chain_user.py")}
            self.assertIn("repo:tests-real-chains", names, "闭包里有真实链：另加真实链目录")
            self.assertIn("file:pkg/tests/helper_fixture.py", names, "闭包里有真实链：另加全部辅助模块")
            self.assertNotIn("repo:tests", names)
            self.assertIn("repo:tests", {entry["name"] for entry in ur.test_unit_inputs(repo, deps, tests / "test_uses_tree_helper.py")},
                          "整目录读取：另加整个测试目录")
            leaf = {entry["name"] for entry in ur.test_unit_inputs(repo, deps, tests / "test_leaf.py")}
            self.assertEqual(leaf, {"repo:managed", "repo:tests-fixtures", "repo:docs", "repo:rest", "file:pkg/tests/test_leaf.py",
                                    "file:pkg/tests/__init__.py"})
            with unittest.mock.patch.dict(ur.GIT_READERS, {"test_leaf": "示例：读真实仓库的提交历史"}):
                self.assertIn("head", {entry["name"] for entry in ur.test_unit_inputs(repo, deps, tests / "test_leaf.py")})
            with unittest.mock.patch.dict(ur.EXTRA_TEST_READS, {"test_leaf": {"helper_fixture.py": "示例：受管模块延迟导入链读到"}}):
                self.assertIn("file:pkg/tests/helper_fixture.py", {entry["name"] for entry in ur.test_unit_inputs(repo, deps, tests / "test_leaf.py")},
                              "读集审计实测、静态闭包漏掉的读取补登进输入（E3-04）")
            with unittest.mock.patch.dict(ur.HEAD_ID_ONLY_READERS, {"test_leaf": "示例：只读 HEAD 提交号"}):
                self.assertNotIn("head", {entry["name"] for entry in ur.test_unit_inputs(repo, deps, tests / "test_leaf.py")},
                                 "只读 HEAD 提交号、结论不随提交变的不加 HEAD 输入（10-02 按实跑证据收窄）")

    def test_managed_import_statements_join_the_closure_but_managed_function_level_chains_do_not(self) -> None:
        """受管模块按 import 语句连边：模块级导入的测试辅助模块、函数内按名字导入的测试模块都算进用到它的测试的闭包；受管
        模块之间的函数内导入不追（否则几乎每个测试都会连到场景测试）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = _repo(Path(directory).resolve(), {
                "pkg/loader.py": "from pkg.tests import helper_fixture\n\ndef run():\n    from pkg.tests import test_other\n",
                "pkg/orchestrator.py": "def lazy():\n    from pkg import loader\n",
                "pkg/tests/test_uses_loader.py": "import unittest\nfrom pkg import loader\n" + PASS,
                "pkg/tests/test_uses_orchestrator.py": "import unittest\nfrom pkg import orchestrator\n" + PASS,
            })
            deps = ur.TestDependencies(root / "pkg")
            tests = root / "pkg" / "tests"
            self.assertEqual(sorted(path.name for path in deps.closure(tests / "test_uses_loader.py")),
                             ["helper_fixture.py", "test_other.py", "test_uses_loader.py"])
            self.assertEqual(sorted(path.name for path in deps.closure(tests / "test_uses_orchestrator.py")), ["test_uses_orchestrator.py"],
                             "受管模块之间的函数内导入不追")
            self.assertTrue(all(deps.is_test_file(path) for path in deps.closure(tests / "test_uses_loader.py")), "闭包只取测试目录文件")

    def test_manual_lists_name_existing_modules_with_reasons(self) -> None:
        tests = REPO_ROOT / "tools" / "official_client_capture" / "tests"
        for table in (ur.WHOLE_TESTS_READERS, ur.GIT_READERS, ur.HEAD_ID_ONLY_READERS):
            for module, reason in table.items():
                with self.subTest(module):
                    self.assertTrue((tests / f"{module}.py").is_file(), "名单里的模块必须存在（改名或删除后同步名单）")
                    self.assertTrue(reason.strip())
        self.assertEqual(set(ur.GIT_READERS) & set(ur.HEAD_ID_ONLY_READERS), set(), "读 git 历史与只读 HEAD 提交号两张表不重叠")

    def test_environment_excludes_cache_locations_and_hides_values(self) -> None:
        base = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://user:secret@proxy:1"}
        entries = ur.executor_environment({**base, "PYTHONPYCACHEPREFIX": "/a", "CODEX_UPGRADE_IDENTITY_MEMO": "/b", "UNIT_EXECUTOR_KIND": "formal"})
        names = {entry["name"] for entry in entries}
        self.assertIn("envvar:PATH", names)
        self.assertFalse({"envvar:PYTHONPYCACHEPREFIX", "envvar:CODEX_UPGRADE_IDENTITY_MEMO", "envvar:UNIT_EXECUTOR_KIND"} & names)
        self.assertNotIn("secret", json.dumps(entries, ensure_ascii=False), "环境变量的值不落盘")
        changed = ur.executor_environment({**base, "PATH": "/opt/bin:/usr/bin"})
        self.assertNotEqual(ur.entries_sha256(entries), ur.entries_sha256(changed))
        self.assertEqual(ur.entries_sha256(ur.executor_environment({**base, "PYTHONPYCACHEPREFIX": "/elsewhere"})), ur.entries_sha256(entries))

    def test_declared_files_and_test_modules_expand_like_test_units_but_without_head(self) -> None:
        """E3-02：命令单元可以声明仓库里的单个文件与测试模块；测试模块按与采集测试单元相同的闭包展开（闭包里的测试文件、
        tests/__init__.py），但不加 HEAD——pre-A3 场景在数据根运行，读不到 git。路径非法、模块不在 tests 目录下都拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = _repo(Path(directory) / "repo", {"pkg/tests/test_codex_0151_worktree_successor.py": PASS})
            repo = ur.RepoIndex.load(root)
            cache: dict = {}
            declared = ur.declared_inputs(repo, {"files": [{"category": "managed", "path": "Makefile"}], "test_modules": [
                "pkg/tests/test_uses_helper.py", "pkg/tests/test_codex_0151_worktree_successor.py"]}, cache)
            names = {entry["name"] for entry in declared}
            self.assertLessEqual({"file:Makefile", "file:pkg/tests/test_uses_helper.py", "file:pkg/tests/helper_fixture.py",
                                  "file:pkg/tests/__init__.py", "file:pkg/tests/test_codex_0151_worktree_successor.py"}, names)
            self.assertNotIn("head", names, "人工名单里读 git 历史的模块，作为场景声明时也不加 HEAD")
            self.assertEqual(len(cache), 1, "同一受管树的依赖分析只建一次")
            deps = next(iter(cache.values()))
            as_test_unit = {entry["name"] for entry in ur.test_unit_inputs(repo, deps, root / "pkg/tests/test_codex_0151_worktree_successor.py")}
            self.assertIn("head", as_test_unit, "同一模块作采集测试单元时照旧加 HEAD")
            for bad in ({"test_modules": ["/abs/test_x.py"]}, {"test_modules": ["pkg/../pkg/tests/test_leaf.py"]},
                        {"test_modules": ["pkg/mod.py"]}, {"test_modules": ["pkg/tests/test_missing.py"]},
                        {"files": [{"path": "Makefile"}]}, {"files": [{"category": "managed", "path": "../x"}]}):
                with self.subTest(bad), self.assertRaises(ur.RecordsError):
                    ur.declared_inputs(repo, bad, cache)

    def test_unit_spec_ignores_cache_location_variables(self) -> None:
        """E3-02：只决定缓存位置的环境变量（字节码前缀、身份记忆目录）不进单元规格，与环境指纹同一口径；别的环境变量照算。"""

        def spec(env: dict[str, str]) -> dict:
            unit = ue.Unit("pre-a3:x", "pre-a3:x", (), ue.Quota(1, 128), False, 1.0, command=("true",), cwd="/d", env=tuple(sorted(env.items())))
            return ue.unit_spec(unit, start=None, pattern=None, timeout_seconds=60)

        base = spec({"PYTHONPATH": "."})
        self.assertEqual(spec({"PYTHONPATH": ".", "PYTHONPYCACHEPREFIX": "/pyc", "CODEX_UPGRADE_IDENTITY_MEMO": "/memo"}), base)
        self.assertNotEqual(spec({"PYTHONPATH": "/other"}), base)


class InheritanceDecisionTests(unittest.TestCase):
    """承接判定逐条拒绝理由：在临时记录库里放一条合格记录，再一项一项改。"""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.store = ur.RecordStore(self.root / "store")
        self.inputs = [{"category": "tests", "name": "file:t.py", "sha256": "a" * 64, "detail": {}}]
        self.environment = [{"category": "environment", "name": "executor:arch", "sha256": "b" * 64, "detail": {}}]
        self.facts = ur.RunFacts(policy_sha256="p", environment=self.environment, environment_sha256=ur.entries_sha256(self.environment),
                                 executor={"files": {"unit_executor.py": "e"}, "sha256": "x"}, max_age_hours=168, now=time.time())
        self.current = ur.Current(unit_id="test_a", unit_type="test", spec={}, spec_sha256="s", inputs=self.inputs,
                                  inputs_sha256=ur.entries_sha256(self.inputs), inheritable=True)
        self.log = self.root / "a.log"
        self.log.write_text("OK\n", encoding="utf-8")
        self.log_digest = ur.file_sha256(self.log)
        self.store.put_log(self.log, self.log_digest)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _fresh_store(self, name: str) -> None:
        self.store = ur.RecordStore(self.root / name)
        self.store.put_log(self.log, self.log_digest)

    def _record(self, run_id: str = "run-1", publish_run: bool = True, **overrides: object) -> dict:
        body = {"unit_id": "test_a", "unit_type": "test", "kind": "formal", "run": {"run_id": run_id}, "executor": self.facts.executor,
                "policy_sha256": "p", "environment": self.environment, "environment_sha256": self.facts.environment_sha256,
                "spec_sha256": "s", "inputs": self.inputs, "inputs_sha256": self.current.inputs_sha256, "test_ids": ["test_a.T.test_ok"],
                "tests": {"test_a.T.test_ok": {"outcome": "passed"}}, "passed": True, "exit_code": 0, "signal": None, "timed_out": False,
                "log": {"sha256": self.log_digest}, "started_at_utc": ur.utc_now(), "completed_at_utc": ur.utc_now(), "inheritable": True}
        body.update(overrides)
        record = ur.seal_record(body)
        self.store.put_record(record)
        if publish_run and self.store.manifest(run_id) is None:
            self.store.put_manifest(ur.build_manifest(run_id=run_id, units=[
                {"unit_id": "test_a", "disposition": "executed" if record["kind"] == "formal" else "diagnostic",
                 "record_sha256": record["record_sha256"]}]))
        return record

    def test_valid_record_is_inherited(self) -> None:
        record = self._record()
        decision = ur.evaluate(self.store, self.current, self.facts)
        self.assertTrue(decision.inherit, decision.reasons)
        self.assertEqual(decision.record["record_sha256"], record["record_sha256"])

    def test_each_rejection_reason(self) -> None:
        cases = {
            "诊断": ({"kind": "diagnostic"}, "诊断执行的记录永不承接"),
            "没通过": ({"passed": False, "exit_code": 1}, "该次执行没通过"),
            "规格": ({"spec_sha256": "other"}, "单元规格变了"),
            "输入": ({"inputs": [dict(self.inputs[0], sha256="c" * 64)], "inputs_sha256": "other"}, "输入变了：file:t.py"),
            "策略": ({"policy_sha256": "old"}, "调度策略版本变了"),
            "环境": ({"environment": [dict(self.environment[0], sha256="d" * 64)], "environment_sha256": "old"}, "环境变了：executor:arch"),
            "执行器": ({"executor": {"files": {"unit_executor.py": "old"}, "sha256": "old"}}, "执行器变了：unit_executor.py"),
            "过期": ({"completed_at_utc": "2026-01-01T00:00:00Z"}, "超过承接期限"),
            "日志": ({"log": {"sha256": "f" * 64}}, "日志不在记录库里"),
            "来源禁用": ({"inheritable": False}, "来源记录明确不可承接"),
        }
        for index, (label, (overrides, expected)) in enumerate(cases.items()):
            with self.subTest(label):
                self._fresh_store(f"store-{index}")
                self._record(run_id=f"run-{index}", **overrides)
                decision = ur.evaluate(self.store, self.current, self.facts)
                self.assertFalse(decision.inherit)
                self.assertIn(expected, decision.reasons[0])

    def test_tampered_or_relabelled_records_are_rejected(self) -> None:
        record = self._record(run_id="run-t")
        path = self.store.record_path("test_a", record["record_sha256"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["tests"]["test_a.T.test_ok"]["outcome"] = "passed"
        payload["seconds"] = 1
        path.chmod(0o600)
        path.write_text(json.dumps(payload), encoding="utf-8")
        decision = ur.evaluate(self.store, self.current, self.facts)
        self.assertFalse(decision.inherit)
        self.assertIn("自摘要不符", decision.reasons[0])
        # 诊断记录改标成正式并重新封装、改名：原运行的清单把它列为诊断执行，照样拒绝。
        self._fresh_store("store-relabel")
        diagnostic = self._record(run_id="run-d", kind="diagnostic")
        forged = ur.seal_record({key: value for key, value in diagnostic.items() if key not in {"record_sha256", "schema_version"}} | {"kind": "formal"})
        self.store.put_record(forged)
        decision = ur.evaluate(self.store, self.current, self.facts)
        self.assertFalse(decision.inherit)
        self.assertIn("原运行的清单没有把这条记录列为该单元的正式执行", decision.reasons[0])

    def test_missing_run_manifest_and_not_inheritable_unit(self) -> None:
        self._record(run_id="run-orphan", publish_run=False)
        decision = ur.evaluate(self.store, self.current, self.facts)
        self.assertFalse(decision.inherit)
        self.assertIn("原运行的清单缺失", decision.reasons[0])
        blocked = ur.Current(unit_id="test_a", unit_type="test", spec={}, spec_sha256="s", inputs=None, inputs_sha256=None,
                             inheritable=False, reason="示例：不可承接")
        self.assertEqual(ur.evaluate(self.store, blocked, self.facts).reasons, ["示例：不可承接"])


# ---------------------------------------------------------------------------
# 端到端：真实的执行器进程跑 run-gates
# ---------------------------------------------------------------------------


def _config(root: Path, **overrides: object) -> Path:
    payload = {"schema_version": ue.CONFIG_SCHEMA, "default_parallelism": 4, "default_quota": {"cores": 1, "memory_mb": 128},
               "quotas": {}, "splits": {}, "exclusive": [], "unit_timeout_seconds": 120, "orphan_grace_seconds": 1, **overrides}
    path = root / "state" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class InheritanceEndToEndTests(unittest.TestCase):
    """方案 E3-01 的验收在临时仓库里逐条走一遍：每次运行都是真实的执行器进程，记录库跨运行保留。"""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name).resolve()
        self.root = _repo(self.base / "repo", {
            "pkg/tests/test_fail_a.py": FAIL, "pkg/tests/test_fail_b.py": FAIL, "pkg/tests/test_fail_c.py": FAIL,
        })
        self.store = self.base / "store"
        self.config = _config(self.base)
        self.runs = 0

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _manifest(self) -> Path:
        ranges = ur.standard_ranges("pkg")
        payload = {
            "schema_version": ue.GATES_SCHEMA,
            "test_groups": [{"group_id": "capture", "start": "pkg/tests", "pattern": "test_*.py"}],
            "units": [{"unit_id": "spec:check", "argv": ["true"], "cwd": str(self.root),
                       "inputs": {"ranges": [ranges[key] for key in ur.COMMAND_RANGES], "head": True}},
                      {"unit_id": "pre-a3:scenario", "argv": ["true"], "cwd": str(self.root), "inheritable": False,
                       "not_inheritable_reason": "示例：pre-A3 暂不承接"}],
            "gates": [{"gate_id": "test-capture-tools", "test_groups": ["capture"]}, {"gate_id": "check", "units": ["spec:check"]},
                      {"gate_id": "pre-a3", "units": ["pre-a3:scenario"]}],
            "scheduling": {"example": 1},
        }
        path = self.base / "gates.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _run(self, *extra: str, mode: str = "full-set-pass", env: dict[str, str] | None = None) -> tuple[int, dict, dict]:
        self.runs += 1
        out = self.base / f"out-{self.runs}"
        command = [sys.executable, str(EXECUTOR), "run-gates", "--manifest", str(self._manifest()), "--config", str(self.config),
                   "--weights", str(self.base / "none.json"), "--durations", str(self.base / "none.json"), "--parallel", "4",
                   "--cores", "4", "--state-dir", str(self.base / "state"), "--out-dir", str(out), "--shared-caches", "off",
                   "--record-store", str(self.store), "--mode", mode, *extra]
        environment = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
        environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)}, **(env or {}))
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=300, env=environment)
        if not (out / "summary.json").exists():
            self.fail(completed.stderr[-3000:])
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        manifest = json.loads((out / "unit-manifest.json").read_text(encoding="utf-8"))
        return completed.returncode, summary, manifest

    @staticmethod
    def _executed(manifest: dict) -> set[str]:
        return {entry["unit_id"] for entry in manifest["units"] if entry["disposition"] == "executed"}

    @staticmethod
    def _inherited(manifest: dict) -> set[str]:
        return {entry["unit_id"] for entry in manifest["units"] if entry["disposition"] == "inherited"}

    def _fix(self, files: dict[str, str], message: str) -> None:
        _write(self.root, files)
        _commit(self.root, message)

    def test_failures_fixed_by_test_changes_rerun_only_those_and_affected_units(self) -> None:
        rc, summary, manifest = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(summary["unit_manifest"]["self_check"], "passed", summary["unit_manifest"]["problems"])
        self.assertEqual(sorted(summary["failed_units"]), ["test_fail_a", "test_fail_b", "test_fail_c"])
        self.assertEqual(self._inherited(manifest), set(), "记录库是空的：第一轮全部执行")
        # 只改测试修复，同时改了辅助模块：第二轮只执行这三个、引用辅助模块的单元，以及输入是整个仓库的命令单元。
        self._fix({"pkg/tests/test_fail_a.py": PASS, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS,
                   "pkg/tests/helper_fixture.py": "HELPER = 2\n"}, "修测试")
        rc, summary, manifest = self._run()
        self.assertEqual(rc, 0, summary.get("failed_units"))
        self.assertEqual(self._executed(manifest), {"test_fail_a", "test_fail_b", "test_fail_c", "test_uses_helper", "spec:check",
                                                    "pre-a3:scenario"})
        self.assertEqual(self._inherited(manifest), {"test_leaf", "test_other"})
        reasons = {entry["unit_id"]: entry.get("reasons") for entry in manifest["units"]}
        self.assertIn("file:pkg/tests/helper_fixture.py", " ".join(reasons["test_uses_helper"]))
        self.assertIn("示例：pre-A3 暂不承接", reasons["pre-a3:scenario"])
        verified = subprocess.run([sys.executable, str(RECORDS), "verify", "--manifest", summary["unit_manifest"]["path"]],
                                  capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(verified.returncode, 0, verified.stdout)
        group = summary["test_groups"]["capture"]
        self.assertEqual((group["status"], group["expected_tests"], group["reported_tests"]), ("passed", 6, 6), "承接的单元照样参加全集核对")
        gates = {gate["gate_id"]: gate for gate in summary["gates"]}
        self.assertEqual(gates["test-capture-tools"]["inherited_units"], ["test_leaf", "test_other"])

    def test_b09_invalid_full_set_request_reruns_all_and_records_fallback(self) -> None:
        self._fix({"pkg/tests/test_fail_a.py": PASS, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS}, "全绿")
        self.assertEqual(self._run()[0], 0)
        missing = self.base / "missing-full-set-request.json"
        rc, summary, manifest = self._run("--full-set-request", str(missing))
        self.assertEqual(rc, 0, summary)
        self.assertEqual(manifest["mode"], "re-execute")
        self.assertEqual(self._inherited(manifest), set(), "不得回到旧逐单元承接路径")
        self.assertEqual(self._executed(manifest), set(manifest["planned_units"]))
        self.assertEqual(manifest["full_set_decision"]["action"], "reexecute-all")
        self.assertEqual(summary["full_set_decision"], manifest["full_set_decision"])
        self.assertNotEqual(manifest["run_id"], json.loads((self.base / "out-1" / "unit-manifest.json").read_text())["run_id"])

    def test_comment_change_reruns_only_that_module_and_managed_change_reruns_all(self) -> None:
        self._fix({"pkg/tests/test_fail_a.py": PASS, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS}, "全绿")
        self.assertEqual(self._run()[0], 0)
        self._fix({"pkg/tests/test_leaf.py": PASS + "# 只改注释\n"}, "注释")
        rc, _summary, manifest = self._run()
        self.assertEqual(rc, 0)
        tests_executed = {unit for unit in self._executed(manifest) if unit.startswith("test_")}
        self.assertEqual(tests_executed, {"test_leaf"}, "采集测试单元里只有它重跑")
        self._fix({"pkg/mod.py": "VALUE = 2\n"}, "改受管文件")
        rc, _summary, manifest = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual({unit for unit in self._executed(manifest) if unit.startswith("test_")},
                         {"test_leaf", "test_uses_helper", "test_other", "test_fail_a", "test_fail_b", "test_fail_c"}, "受管文件变了全部重跑")
        rc, _summary, manifest = self._run()
        self.assertEqual(self._executed(manifest), {"pre-a3:scenario"}, "什么都没变：除了不可承接的单元全部承接")

    def test_diagnostic_pass_is_never_inherited_and_exclusive_registration_reruns_everything(self) -> None:
        flaky = ("import os, unittest\nclass T(unittest.TestCase):\n"
                 "    def test_ok(self): self.assertEqual(os.environ.get('UNIT_EXECUTOR_KIND'), 'diagnostic', '示例：只在单独诊断时通过')\n")
        self._fix({"pkg/tests/test_fail_a.py": flaky, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS}, "并行失败、诊断通过")
        rc, summary, _manifest = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual([(item["unit_id"], item["passed"]) for item in summary["diagnostic"]], [("test_fail_a", True)])
        rc, summary, manifest = self._run()
        self.assertEqual(rc, 1, "诊断通过的结论不能被下一轮当正式结果承接")
        self.assertIn("test_fail_a", self._executed(manifest))
        # 登记进独占名单：调度策略版本变了，全部单元重新执行。
        self.config = _config(self.base, exclusive=[{"tests": "test_fail_a", "reason": "示例：并行下失败、单独通过"}])
        rc, _summary, manifest = self._run()
        self.assertEqual(self._inherited(manifest), set())
        self.assertIn("调度策略版本变了", " ".join(next(entry for entry in manifest["units"] if entry["unit_id"] == "test_leaf")["reasons"]))

    def test_tampering_environment_and_expiry_force_execution(self) -> None:
        self._fix({"pkg/tests/test_fail_a.py": PASS, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS}, "全绿")
        _rc, _summary, first = self._run()
        leaf = next(entry for entry in first["units"] if entry["unit_id"] == "test_leaf")
        path = Path(leaf["record_path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["seconds"] = 0.001
        path.chmod(0o600)
        path.write_text(json.dumps(payload), encoding="utf-8")
        rc, _summary, manifest = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("test_leaf", self._executed(manifest), "篡改过的记录不承接、自动重跑")
        self.assertIn("自摘要不符", " ".join(next(entry for entry in manifest["units"] if entry["unit_id"] == "test_leaf")["reasons"]))
        # 篡改发生在清单写成之后：重验那次运行的清单被拒绝。
        verified = subprocess.run([sys.executable, str(RECORDS), "verify", "--manifest", str(self.base / "out-1" / "unit-manifest.json")],
                                  capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(verified.returncode, 1)
        rc, _summary, manifest = self._run(env={"E3_EXAMPLE_ENV": "changed"})
        self.assertEqual(self._inherited(manifest), set(), "环境指纹变了全部重跑")
        time.sleep(1.1)
        rc, _summary, manifest = self._run("--inheritance-max-age-hours", "0.0002")
        self.assertEqual(self._inherited(manifest), set(), "超过期限自动重跑")
        with self.assertRaises(AssertionError):
            self._run("--inheritance-max-age-hours", "200")

    def test_re_execute_mode_inherits_nothing_and_an_injected_inheritance_fails_self_check(self) -> None:
        self._fix({"pkg/tests/test_fail_a.py": PASS, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS}, "全绿")
        self._run()
        rc, summary, manifest = self._run(mode="re-execute")
        self.assertEqual(rc, 0)
        self.assertEqual((manifest["mode"], self._inherited(manifest)), ("re-execute", set()))
        self.assertEqual(ur.verify_manifest(manifest, store=ur.RecordStore(self.store)), [])
        # 塞进一条承接项（指向上一次运行的正式记录）并重新封装：自检失败。
        previous = json.loads((self.base / "out-1" / "unit-manifest.json").read_text(encoding="utf-8"))
        donor = next(entry for entry in previous["units"] if entry["unit_id"] == "test_leaf")
        forged = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        forged["units"] = [dict(donor, disposition="inherited") if entry["unit_id"] == "test_leaf" else entry for entry in manifest["units"]]
        forged = ur.seal(forged, "manifest_sha256")
        problems = ur.verify_manifest(forged, store=ur.RecordStore(self.store))
        self.assertTrue(any("重新执行全集模式不得有承接项" in problem for problem in problems), problems)
        self.assertTrue(ur.verify_manifest(dict(manifest, mode="full-set-pass"), store=ur.RecordStore(self.store)), "改了模式不重新封装：自摘要不符")

    def test_decide_only_reports_decisions_without_executing_or_recording(self) -> None:
        self._fix({"pkg/tests/test_fail_a.py": PASS, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS}, "全绿")
        self._run()
        records_before = sorted(self.store.rglob("*.json"))

        def decide(env: dict[str, str] | None = None) -> dict:
            out = self.base / f"decide-{len(list(self.base.glob('decide-*')))}"
            command = [sys.executable, str(EXECUTOR), "run-gates", "--manifest", str(self._manifest()), "--config", str(self.config),
                       "--weights", str(self.base / "none.json"), "--durations", str(self.base / "none.json"), "--parallel", "4",
                       "--cores", "4", "--state-dir", str(self.base / "state"), "--out-dir", str(out), "--shared-caches", "off",
                       "--record-store", str(self.store), "--mode", "full-set-pass", "--decide-only"]
            environment = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
            environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)}, **(env or {}))
            completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=300, env=environment)
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            self.assertFalse((out / "summary.json").exists() or (out / "unit-manifest.json").exists(), "只判定不执行")
            return json.loads((out / "decisions.json").read_text(encoding="utf-8"))

        same = decide()
        self.assertEqual((same["inherit"], same["execute"]), (7, 1), "什么都没变：只有不可承接的 pre-A3 要执行")
        changed = decide({"E3_EXAMPLE_ENV": "changed"})
        self.assertEqual(changed["inherit"], 0)
        self.assertIn("环境变了：envvar:E3_EXAMPLE_ENV", changed["units"]["test_leaf"]["reasons"][0])
        self.assertEqual(sorted(self.store.rglob("*.json")), records_before, "只判定不写记录")

    def test_dirty_tree_inherits_nothing(self) -> None:
        self._fix({"pkg/tests/test_fail_a.py": PASS, "pkg/tests/test_fail_b.py": PASS, "pkg/tests/test_fail_c.py": PASS}, "全绿")
        self._run()
        (self.root / "pkg" / "tests" / "untracked.txt").write_text("x", encoding="utf-8")
        _rc, _summary, manifest = self._run()
        self.assertEqual(self._inherited(manifest), set())
        self.assertIn("测试树不干净", " ".join(manifest["units"][0]["reasons"]))


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        """驱动随附一份（执行器在 ARM64 上从驱动目录运行，按路径加载同目录的 unit_records.py）。"""

        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "unit_records.py"
        self.assertEqual(driver.read_bytes(), RECORDS.read_bytes(), "驱动里的 unit_records.py 与 tools/ci 原件不一致")


if __name__ == "__main__":
    unittest.main()
