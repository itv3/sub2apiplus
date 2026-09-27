"""修好接着跑第 8 项后半：评估器读侧闭包收窄（evaluator-reader-closure/v2）。

旧口径下改一行监督器、账本或租约都会让 compare／accept reader 摘要变化，b≥1 时编译授权失配，而
evaluation-recover 又不受理非评估器缺陷——修一次基础设施就卡死。这里验证新口径：基础设施变化不动 reader，
评估逻辑与原盲区（延迟导入的恢复段复用证明）的变化照常改变 reader；屏障只收守卫，由调用点形态守护。
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip
from tools.official_client_capture.tests import managed_tree_copy

TOOL_ROOT = Path(__file__).resolve().parents[1]


def _synthetic_policy() -> dict:
    policy = json.loads(tip.DEFAULT_POLICY_PATH.read_text("utf-8"))
    policy["orchestrator"] = {
        "file": "codex_upgrade.py",
        "wire_roots": ["compare_campaign"],
        "evidence_roots": ["compare_campaign"],
        "dynamic_call_names": ["eval", "exec", "__import__", "globals", "locals", "vars"],
    }
    policy["policy_sha256"] = "f" * 64
    return policy


class SyntheticReaderClosureTests(unittest.TestCase):
    """合成树：屏障不计入、control 层按符号、evidence 层整文件、延迟导入与模块屏障。"""

    ORCHESTRATOR = (
        "from tools.official_client_capture import candidate_rule_assertion\n"
        "from tools.official_client_capture import codex_upgrade_timing_ledger\n"
        "LIMIT = 3\n"
        "def compare_campaign(campaign_dir, candidate_id):\n"
        "    _campaign_lock(campaign_dir)\n"
        "    from tools.official_client_capture import codex_upgrade_supervisor as sup\n"
        "    codex_upgrade_timing_ledger.replay(campaign_dir)\n"
        "    return evaluate(candidate_id) + [sup.used(), candidate_rule_assertion.X, LIMIT]\n"
        "def evaluate(candidate_id):\n"
        "    return [candidate_id]\n"
        "def _campaign_lock(campaign_dir):\n"
        "    return None\n"
    )
    SUPERVISOR = (
        "def used():\n    return helper()\n"
        "def helper():\n    return 1\n"
        "def unused():\n    return 2\n"
    )

    def _tree(self, root: Path, *, orchestrator: str | None = None, supervisor_source: str | None = None) -> dict:
        files = {
            "codex_upgrade.py": orchestrator or self.ORCHESTRATOR,
            "codex_upgrade_supervisor.py": supervisor_source or self.SUPERVISOR,
            "candidate_rule_assertion.py": "X = 1\n",
            "codex_upgrade_timing_ledger.py": "def replay(path):\n    return path\n",
        }
        for name, text in files.items():
            (root / name).write_text(text, "utf-8")
        return tip._managed_tree_digests(root)

    def _closure(self, root: Path, digests: dict) -> dict:
        return tip.evaluator_reader_closure(_synthetic_policy(), root, ["compare_campaign"], digests)

    def test_barriers_symbols_and_whole_modules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            closure = self._closure(root, self._tree(root))
            self.assertEqual(
                [(item["module"], item["name"]) for item in closure["symbols"]],
                [
                    ("codex_upgrade", "LIMIT"),
                    ("codex_upgrade", "compare_campaign"),
                    ("codex_upgrade", "evaluate"),
                    ("codex_upgrade_supervisor", "helper"),
                    ("codex_upgrade_supervisor", "used"),
                ],
            )
            # evidence 层整文件；账本 replay 是模块屏障，不计入。
            self.assertEqual([item["module"] for item in closure["modules"]], ["candidate_rule_assertion"])

    def test_infrastructure_changes_do_not_move_the_digest_but_reader_changes_do(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._closure(root, self._tree(root))["closure_sha256"]
            cases = {
                "屏障函数本体": (dict(orchestrator=self.ORCHESTRATOR.replace("return None", "return 'locked'")), False),
                "监督器无关函数": (dict(supervisor_source=self.SUPERVISOR.replace("return 2", "return 3")), False),
                "监督器读侧函数": (dict(supervisor_source=self.SUPERVISOR.replace("return 1", "return 4")), True),
                "评估函数": (dict(orchestrator=self.ORCHESTRATOR.replace("[candidate_id]", "[candidate_id, 0]")), True),
            }
            for label, (overrides, moves) in cases.items():
                with self.subTest(label=label), tempfile.TemporaryDirectory() as other:
                    mutated = Path(other)
                    digest = self._closure(mutated, self._tree(mutated, **overrides))["closure_sha256"]
                    self.assertEqual(digest != base, moves, label)
            # 账本 replay（模块屏障）与 evidence 模块（整文件）：
            with tempfile.TemporaryDirectory() as other:
                mutated = Path(other)
                digests = self._tree(mutated)
                (mutated / "codex_upgrade_timing_ledger.py").write_text("def replay(path):\n    return None\n", "utf-8")
                self.assertEqual(self._closure(mutated, tip._managed_tree_digests(mutated))["closure_sha256"], base)
                (mutated / "candidate_rule_assertion.py").write_text("X = 2\n", "utf-8")
                self.assertNotEqual(self._closure(mutated, tip._managed_tree_digests(mutated))["closure_sha256"], base)
                del digests

    def test_dynamic_dispatch_fails_in_orchestrator_but_is_whole_file_in_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orchestrator = self.ORCHESTRATOR.replace("return [candidate_id]", "return eval('1')")
            with self.assertRaisesRegex(tip.ToolIdentityPolicyError, "动态调用 eval"):
                self._closure(root, self._tree(root, orchestrator=orchestrator))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            closure = self._closure(root, self._tree(root, supervisor_source=self.SUPERVISOR.replace("return 1", "return globals()")))
            self.assertIn("codex_upgrade_supervisor", [item["module"] for item in closure["modules"]])


class RealTreeReaderClosureTests(unittest.TestCase):
    """真实受管树副本上的变异：基础设施不动 reader，评估逻辑与原盲区照常改变 reader。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        cls.tree = managed_tree_copy.copy_managed_tree(Path(cls._directory.name), include_tests=False)
        cls.root = managed_tree_copy.tool_root(cls.tree)
        cls.base = tip.evaluator_dependency_digests(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    def _mutated(self, relative: str, function: str) -> dict[str, str]:
        """在函数体首句前插入一条无副作用语句，计算四项后恢复原文件。"""

        path = self.root / relative
        original = path.read_text("utf-8")
        tree = ast.parse(original)
        node = next(
            item for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function
        )
        first = node.body[0]
        lines = original.splitlines(keepends=True)
        indent = " " * first.col_offset
        lines.insert(first.lineno - 1, f"{indent}_reader_mutation = 'm'  # 测试变异\n")
        try:
            path.write_text("".join(lines), "utf-8")
            return tip.evaluator_dependency_digests(self.root)
        finally:
            path.write_text(original, "utf-8")

    def test_infrastructure_mutations_keep_both_readers(self) -> None:
        for relative, function in (
            ("codex_upgrade.py", "_campaign_lock"),
            ("codex_upgrade.py", "_verify_plan_identity"),
            ("codex_upgrade_supervisor.py", "_close_failed_campaign_timing_ledger"),
            ("codex_upgrade_timing_ledger.py", "inspect_ledger"),
            ("codex_upgrade_project_ledger.py", "append_project_event"),
        ):
            with self.subTest(target=f"{relative}:{function}"):
                mutated = self._mutated(relative, function)
                for field in tip.READER_READER_FIELDS:
                    self.assertEqual(mutated[field], self.base[field], f"{relative}:{function} 不该改变 {field}")

    def test_evaluation_mutations_move_the_readers(self) -> None:
        for relative, function, fields in (
            ("codex_upgrade.py", "compare_campaign", ("compare_reader_sha256",)),
            ("codex_upgrade.py", "accept_campaign", ("accept_reader_sha256",)),
            ("codex_upgrade.py", "_verify_reader_runtime_baseline", tip.READER_READER_FIELDS),
            # 旧口径的盲区：延迟导入的恢复段复用证明决定 accept 承接哪些 Job 结果。
            ("codex_upgrade_reconciler.py", "segment_reuse_proofs", ("accept_reader_sha256",)),
            ("codex_upgrade_supervisor.py", "job_egress_trusted", tip.READER_READER_FIELDS),
        ):
            with self.subTest(target=f"{relative}:{function}"):
                mutated = self._mutated(relative, function)
                for field in fields:
                    self.assertNotEqual(mutated[field], self.base[field], f"{relative}:{function} 应改变 {field}")

    def test_checker_and_builder_stay_whole_file(self) -> None:
        digests = tip._managed_tree_digests(self.root)
        self.assertEqual(self.base["checker_sha256"], digests[tip.EVALUATOR_CHECKER_RELATIVE])
        self.assertEqual(self.base["builder_sha256"], digests[tip.EVALUATOR_BUILDER_RELATIVE])


class ReaderBarrierContractTests(unittest.TestCase):
    """屏障只收守卫：闭包内每个调用点的形态都在登记范围内；名单与模块屏障都指向真实函数。"""

    @staticmethod
    def _shape(parents: dict, call: ast.Call) -> str:
        parent = parents.get(call)
        if isinstance(parent, ast.Expr):
            return "discard"
        if isinstance(parent, ast.withitem):
            return "with"
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            return "assign"
        if isinstance(parent, ast.Return):
            return "return"
        if isinstance(parent, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            return "compare"
        return type(parent).__name__

    def test_every_barrier_call_site_keeps_its_registered_shape(self) -> None:
        policy = tip.load_policy()
        digests = tip._managed_tree_digests(TOOL_ROOT)
        source = (TOOL_ROOT / "codex_upgrade.py").read_text("utf-8")
        tree = ast.parse(source)
        functions, _constants, _imports = tip._module_symbols(tree)
        self.assertEqual(sorted(set(tip.READER_BARRIERS) - set(functions)), [], "屏障名单里有不存在的函数")
        for module, names in tip.READER_MODULE_BARRIERS.items():
            module_tree = ast.parse((TOOL_ROOT / f"{module}.py").read_text("utf-8"))
            module_functions, _c, _i = tip._module_symbols(module_tree)
            self.assertEqual(sorted(set(names) - set(module_functions)), [], module)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        seen: dict[str, set[str]] = {name: set() for name in tip.READER_BARRIERS}
        for roots in (tip.EVALUATOR_COMPARE_READER_ROOTS, tip.EVALUATOR_ACCEPT_READER_ROOTS):
            closure = tip.evaluator_reader_closure(policy, TOOL_ROOT, list(roots), digests)
            members = {item["name"] for item in closure["symbols"] if item["module"] == "codex_upgrade"}
            for name in members:
                for child in ast.walk(functions[name]) if name in functions else ():
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in seen:
                        seen[child.func.id].add(self._shape(parents, child))
        for name, shapes in seen.items():
            with self.subTest(barrier=name):
                self.assertLessEqual(
                    shapes,
                    set(tip.READER_BARRIERS[name]),
                    f"{name} 的调用点形态 {sorted(shapes)} 超出登记 {sorted(tip.READER_BARRIERS[name])}：屏障函数的返回值"
                    "开始进入读侧结论时必须移出屏障名单",
                )

    def test_wire_and_evidence_closures_are_untouched_by_reader_closure(self) -> None:
        # 读侧闭包只新增函数，不改 wire／evidence 所用的旧闭包函数：同一棵树上旧闭包按原算法计算。
        policy = tip.load_policy()
        digests = tip._managed_tree_digests(TOOL_ROOT)
        legacy = tip.legacy_evaluator_reader_digests(TOOL_ROOT, policy)
        for field, roots in (
            ("compare_reader_sha256", tip.EVALUATOR_COMPARE_READER_ROOTS),
            ("accept_reader_sha256", tip.EVALUATOR_ACCEPT_READER_ROOTS),
        ):
            self.assertEqual(
                legacy[field], tip.orchestrator_closure(policy, TOOL_ROOT, list(roots), digests, layer=None)["closure_sha256"]
            )


class EvaluatorAuthorizationCompatibilityTests(unittest.TestCase):
    """b≥1 授权冻结于旧读侧口径、核对值为新口径：指向同一棵读侧代码树时放行，任一侧真实变化仍拒绝。"""

    def _verify(self, authorized: dict, digests: dict) -> None:
        with mock.patch.object(codex_upgrade, "_authorized_evaluator_digests", return_value=authorized):
            codex_upgrade._verify_evaluator_digests_authorized(
                Path("/nonexistent"), {}, "cand", 1, digests, label="测试"
            )

    def test_cross_policy_authorization(self) -> None:
        current = tip.evaluator_dependency_digests()
        legacy = tip.legacy_evaluator_reader_digests()
        authorized = dict(current, **legacy)
        self._verify(authorized, dict(current))
        self._verify(dict(current), dict(current))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "compare_reader_sha256"):
            self._verify(dict(authorized, compare_reader_sha256="0" * 64), dict(current))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "accept_reader_sha256"):
            self._verify(authorized, dict(current, accept_reader_sha256="1" * 64))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "checker_sha256"):
            self._verify(dict(authorized, checker_sha256="2" * 64), dict(current))


class ReaderRuntimeBaselineTests(unittest.TestCase):
    """当前评估基线的运行时核对：父 run 冻结的基线与账本当前基线逐字相等才放行。"""

    def _run_dir(self, root: Path, inner: dict, *, tamper: bool = False) -> Path:
        run_dir = root / "run"
        run_dir.mkdir()
        record = {"manifest_sha256": supervisor._sha256(supervisor._canonical(inner)), "manifest": inner}
        if tamper:
            record["manifest"] = dict(inner, evaluation_baseline=9)
        path = run_dir / "campaign-run-manifest.json"
        path.write_text(json.dumps(record), "utf-8")
        path.chmod(0o600)  # 监督器只信任当前用户 0600 的清单
        return run_dir

    def _check(self, run_dir: Path | None, current: tuple) -> None:
        environment = {supervisor.CAMPAIGN_RUN_DIR_ENV: str(run_dir)} if run_dir is not None else {}
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            codex_upgrade, "_current_evaluation_baseline", return_value=current
        ):
            if run_dir is None:
                os.environ.pop(supervisor.CAMPAIGN_RUN_DIR_ENV, None)
            codex_upgrade._verify_reader_runtime_baseline(Path("/nonexistent"), "cand", label="accept")

    def test_runtime_baseline_must_equal_parent_run_freeze(self) -> None:
        commit = {"commit_sha256": "c" * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = self._run_dir(root, {"candidate_id": "cand", "evaluation_baseline": 1, "baseline_commit_sha256": "c" * 64})
            self._check(run_dir, (1, commit))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "与父 run 冻结的 b1"):
                self._check(run_dir, (2, {"commit_sha256": "d" * 64}))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不一致"):
                self._check(run_dir, (1, {"commit_sha256": "d" * 64}))
        with tempfile.TemporaryDirectory() as directory:
            # b0：清单冻结 null 基线，账本也在 b0。
            run_dir = self._run_dir(Path(directory), {"candidate_id": "cand", "evaluation_baseline": None, "baseline_commit_sha256": None})
            self._check(run_dir, (0, None))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "b0"):
                self._check(run_dir, (1, commit))
        with tempfile.TemporaryDirectory() as directory:
            # 清单没有冻结基线（非 VC-5 批次）或属于别的候选：不核对。
            self._check(self._run_dir(Path(directory), {"phase": "VC-6"}), (3, commit))
        with tempfile.TemporaryDirectory() as directory:
            self._check(self._run_dir(Path(directory), {"candidate_id": "other", "evaluation_baseline": 1}), (0, None))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "摘要不一致"):
                self._check(self._run_dir(Path(directory), {"candidate_id": "cand", "evaluation_baseline": 1}, tamper=True), (1, commit))
        # 不在父监督器下运行：不核对。
        self._check(None, (5, commit))


if __name__ == "__main__":
    unittest.main()
